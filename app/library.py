from __future__ import annotations

import hashlib
import errno
import json
import os
import re
import shlex
import shutil
import stat as stat_module
import struct
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .config import Settings
from .db import Database
from .esde import esde_device_relpath


COPY_SUFFIX = ".rommates-copy"
LEGACY_COPY_SUFFIX = ".rommanager-copy"
LINK_SUFFIX = ".rommates-link"
DEVICE_INVENTORY_CACHE_SECONDS = 5.0

CUE_FILE_RE = re.compile(
    r'^\s*FILE\s+(?:"([^"]+)"|(.+?))\s+(?:BINARY|MOTOROLA|WAVE|AIFF|MP3)\s*$',
    re.IGNORECASE,
)
GDI_FILE_RE = re.compile(r'^(\s*\d+\s+\d+\s+\d+\s+\d+\s+)(?:"([^"]+)"|(\S+))(\s+\d+.*)$')
DESCRIPTOR_EXTENSIONS = frozenset({".cue", ".gdi", ".m3u"})
DISC_FOLDER_PLATFORMS = frozenset({"dreamcast", "psx"})
SWITCH_SUPPORT_DIRECTORIES = frozenset(
    {"update", "updates", "dlc", "cheat", "cheats", "mod", "mods", "firmware"}
)
SWITCH_TITLE_ID_RE = re.compile(r"[\[(]([0-9a-f]{16})[\])]", re.IGNORECASE)
SWITCH_UPDATE_MARKER_RE = re.compile(r"(?:^|[\s._\-\[(])(update|upd)(?:$|[\s._\-\])])", re.IGNORECASE)
SWITCH_NONZERO_VERSION_RE = re.compile(
    r"\[[0-9a-f]{16,17}\]\[v?([1-9]\d*)\]", re.IGNORECASE
)
VITA_TITLE_ID_RE = re.compile(r"^[A-Z]{4}\d{5}$", re.IGNORECASE)
VITA_CONTENT_ROOTS = frozenset({"app", "patch", "addcont", "license"})
VITA_PLATFORM_NAMES = frozenset({"vita", "psvita"})
TAG_RE = re.compile(r"\s*[\(\[].*?[\)\]]")
NON_WORD_RE = re.compile(r"[^a-z0-9]+")
PACK_NUMBER_RE = re.compile(r"^\s*(?:\[\d{1,4}\]|\d{1,4}[.)])\s*")
HASH_CACHE_BATCH_FILES = 16
HASH_CACHE_BATCH_BYTES = 128 * 1024 * 1024
HASH_READ_CHUNK_BYTES = 8 * 1024 * 1024

ProgressCallback = Callable[..., None]
CancelCheck = Callable[[], None]
IssueCallback = Callable[[str], None]


class LibraryError(RuntimeError):
    pass


class JobCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class FileRecord:
    relpath: str
    size: int
    sha256: str
    kind: str
    mtime_ns: int


@dataclass(frozen=True)
class GameCandidate:
    platform: str
    primary_relpath: str
    display_name: str
    extension: str
    size: int
    bundle_hash: str
    normalized_name: str
    mtime_ns: int
    files: tuple[FileRecord, ...]


def _format_bytes(value: int) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit in {"B", "KB"} else f"{size:.1f} {unit}"
        size /= 1024
    return f"{value} B"


def _is_switch_support_path(path: Path, platform_dir: Path) -> bool:
    """Keep emulator support content and update packages out of the game catalog."""
    try:
        relative = path.relative_to(platform_dir)
    except ValueError:
        return False
    if any(part.casefold() in SWITCH_SUPPORT_DIRECTORIES for part in relative.parts[:-1]):
        return True
    name = path.name
    if SWITCH_UPDATE_MARKER_RE.search(name):
        return True
    if SWITCH_NONZERO_VERSION_RE.search(name):
        return True
    return any(match.group(1).casefold().endswith("800") for match in SWITCH_TITLE_ID_RE.finditer(name))


def _read_sfo_title(path: Path) -> str:
    """Read the default TITLE value from a PlayStation PARAM.SFO file."""
    try:
        data = path.read_bytes()
        if len(data) < 20 or data[:4] != b"\x00PSF":
            return ""
        key_start, value_start, count = struct.unpack_from("<III", data, 8)
        if count > 4096 or key_start >= len(data) or value_start >= len(data):
            return ""
        values: dict[str, str] = {}
        for index in range(count):
            offset = 20 + index * 16
            if offset + 16 > len(data):
                return ""
            key_offset, _, value_length, _, value_offset = struct.unpack_from("<HHIII", data, offset)
            key_position = key_start + key_offset
            value_position = value_start + value_offset
            if key_position >= len(data) or value_position + value_length > len(data):
                continue
            key_end = data.find(b"\0", key_position)
            if key_end < 0:
                continue
            key = data[key_position:key_end].decode("utf-8", "replace")
            value = data[value_position : value_position + value_length]
            values[key] = value.rstrip(b"\0").decode("utf-8", "replace").strip()
        return values.get("TITLE", "")
    except (OSError, ValueError, struct.error):
        return ""


@dataclass
class ScanProgress:
    total_files: int
    total_bytes: int
    callback: ProgressCallback | None
    platforms: dict[str, dict[str, int]] = field(default_factory=dict)
    processed_files: int = 0
    processed_bytes: int = 0
    hashed_files: int = 0
    cached_files: int = 0
    cached_bytes: int = 0
    metadata_files: int = 0
    metadata_bytes: int = 0
    current_platform: str = ""
    current_relpath: str = ""
    current_mode: str = ""
    current_size: int = 0
    current_bytes: int = 0
    started_at: float = field(default_factory=time.monotonic)
    current_started_at: float = 0.0
    slow_files: list[dict[str, object]] = field(default_factory=list)

    def start_file(self, platform: str, relpath: str, size: int, mode: str) -> None:
        self.current_platform = platform
        self.current_relpath = relpath
        self.current_mode = mode
        self.current_size = size
        self.current_bytes = 0
        self.current_started_at = time.monotonic()
        # Only a physical read can sit on one file long enough to benefit from a
        # separate "starting" update. Cache and metadata entries finish immediately.
        if mode == "hashing":
            self._emit()

    def _telemetry(self, final: bool = False) -> dict[str, object]:
        elapsed = max(time.monotonic() - self.started_at, 0.001)
        current_elapsed = (
            max(time.monotonic() - self.current_started_at, 0)
            if self.current_started_at
            else 0
        )
        return {
            "phase": "hashing" if self.total_bytes else "cataloging",
            "total_files": self.total_files,
            "processed_files": self.processed_files,
            "bytes_to_hash": self.total_bytes,
            "bytes_read": self.processed_bytes,
            "read_rate": int(self.processed_bytes / elapsed),
            "hashed_files": self.hashed_files,
            "cached_files": self.cached_files,
            "cached_bytes": self.cached_bytes,
            "metadata_files": self.metadata_files,
            "metadata_bytes": self.metadata_bytes,
            "current": {
                "platform": self.current_platform,
                "relpath": self.current_relpath,
                "mode": self.current_mode,
                "size": self.current_size,
                "bytes_read": self.current_bytes,
                "elapsed_seconds": round(current_elapsed, 1),
            },
            "platforms": self.platforms,
            "slow_files": self.slow_files,
            "final": final,
        }

    def _emit(self, final: bool = False) -> None:
        if not self.callback:
            return
        if self.total_bytes:
            file_fraction = self.processed_files / self.total_files if self.total_files else 1
            byte_fraction = self.processed_bytes / self.total_bytes
            # Metadata and cache validation are real work, but physical reads dominate
            # scan duration. Keep the percentage honest when thousands of metadata-only
            # files finish before a handful of large images.
            fraction = (file_fraction * 0.1) + (byte_fraction * 0.9)
        elif self.total_files:
            fraction = self.processed_files / self.total_files
        else:
            fraction = 1
        percent = min(90, 1 + int(max(0, min(fraction, 1)) * 89))
        detail = f"Scanning {self.processed_files:,} of {self.total_files:,} files"
        if self.current_platform:
            detail += f" in {self.current_platform}"
        if self.total_bytes:
            detail += (
                f" · {_format_bytes(self.processed_bytes)} of "
                f"{_format_bytes(self.total_bytes)} physically read"
            )
        if self.cached_files:
            detail += f" · {self.cached_files:,} cached"
        if self.metadata_files:
            detail += f" · {self.metadata_files:,} metadata-only"
        if getattr(self.callback, "supports_telemetry", False):
            self.callback(percent, detail, "running", self._telemetry(final))
        else:
            self.callback(percent, detail)

    def advance(self, byte_count: int) -> None:
        self.processed_bytes += byte_count
        self.current_bytes += byte_count
        if self.current_platform in self.platforms:
            self.platforms[self.current_platform]["read_bytes"] += byte_count
        self._emit()

    def finish_file(
        self,
        cached: bool = False,
        metadata_only: bool = False,
        byte_count: int = 0,
    ) -> None:
        self.processed_files += 1
        platform = self.platforms.get(self.current_platform)
        if platform:
            platform["processed_files"] += 1
        if cached:
            self.cached_files += 1
            self.cached_bytes += byte_count
            if platform:
                platform["processed_cached_files"] += 1
        if metadata_only:
            self.metadata_files += 1
            self.metadata_bytes += byte_count
            if platform:
                platform["processed_metadata_files"] += 1
        if not cached and not metadata_only:
            self.hashed_files += 1
            if platform:
                platform["processed_hash_files"] += 1
            elapsed = max(time.monotonic() - self.current_started_at, 0.001)
            self.slow_files.append(
                {
                    "platform": self.current_platform,
                    "relpath": self.current_relpath,
                    "size": self.current_size,
                    "seconds": round(elapsed, 1),
                    "rate": int(self.current_size / elapsed),
                }
            )
            self.slow_files = sorted(
                self.slow_files, key=lambda item: float(item["seconds"]), reverse=True
            )[:5]
        self._emit()


def normalize_name(name: str) -> str:
    stem = Path(name).stem.lower().replace("_", " ")
    stem = PACK_NUMBER_RE.sub("", stem)
    stem = stem.replace(".", " ")
    stem = TAG_RE.sub(" ", stem)
    return NON_WORD_RE.sub(" ", stem).strip()


def cleanup_name(name: str) -> str:
    """Conservative filename cleanup that does not invent title capitalization."""
    cleaned = PACK_NUMBER_RE.sub("", name.strip())
    cleaned = cleaned.replace("_", " ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"\s+-\s+", " - ", cleaned)
    return cleaned.strip().rstrip(".")


def _inside(root: Path, candidate: Path) -> Path:
    root = root.resolve()
    resolved = candidate.resolve()
    if resolved != root and root not in resolved.parents:
        raise LibraryError(f"Path escapes configured root: {candidate}")
    return resolved


def _rel(root: Path, path: Path) -> str:
    return path.resolve().relative_to(root.resolve()).as_posix()


def _scan_rel(root: Path, path: Path) -> str:
    """Fast relative path for entries already discovered beneath a resolved root."""
    return path.relative_to(root).as_posix()


class LibraryService:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self._operation_lock = threading.RLock()
        self._device_inventory_cache: dict[int, tuple[float, frozenset[str]]] = {}

    def prepare_roots(self) -> None:
        if self.settings.require_existing_roots:
            missing = [
                str(path)
                for path in (self.settings.library_root, self.settings.devices_root)
                if not path.is_dir()
            ]
            if missing:
                raise LibraryError(
                    "Required mounted directories do not exist: " + ", ".join(missing)
                )
        else:
            self.settings.library_root.mkdir(parents=True, exist_ok=True)
            self.settings.devices_root.mkdir(parents=True, exist_ok=True)
        self.settings.trash_root.mkdir(parents=True, exist_ok=True)

    def _hash_file(
        self,
        path: Path,
        relpath: str,
        cache: dict[str, tuple[int, int, str]],
        cache_updates: dict[str, tuple[int, int, str]],
        progress: ScanProgress | None = None,
        cancel_check: CancelCheck | None = None,
        stat_result: os.stat_result | None = None,
    ) -> tuple[str, os.stat_result]:
        if cancel_check:
            cancel_check()
        stat = stat_result or path.stat()
        cached = cache.get(relpath)
        if cached and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
            if progress:
                progress.finish_file(cached=True, byte_count=stat.st_size)
            return cached[2], stat

        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(HASH_READ_CHUNK_BYTES), b""):
                if cancel_check:
                    cancel_check()
                digest.update(chunk)
                if progress:
                    progress.advance(len(chunk))
        value = digest.hexdigest()
        cache_updates[relpath] = (stat.st_size, stat.st_mtime_ns, value)
        # Make a repeated reference within this scan a cache hit too.
        cache[relpath] = (stat.st_size, stat.st_mtime_ns, value)
        pending_bytes = sum(values[0] for values in cache_updates.values())
        if len(cache_updates) >= HASH_CACHE_BATCH_FILES or pending_bytes >= HASH_CACHE_BATCH_BYTES:
            # Checkpoint at file boundaries, including within a multi-disc bundle.
            # A restart never needs to rehash an already-checkpointed large image.
            self._persist_hash_cache(cache_updates)
        if progress:
            progress.finish_file()
        return value, stat

    def _descriptor_refs(self, descriptor: Path) -> list[Path]:
        refs: list[Path] = []
        try:
            lines = descriptor.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            return refs
        if descriptor.suffix.lower() == ".cue":
            for line in lines:
                match = CUE_FILE_RE.match(line)
                if match:
                    filename = (match.group(1) or match.group(2) or "").strip()
                    if filename:
                        refs.append(descriptor.parent / filename.replace("\\", "/"))
        elif descriptor.suffix.lower() == ".m3u":
            for line in lines:
                value = line.strip()
                if value and not value.startswith("#"):
                    refs.append(descriptor.parent / value.replace("\\", "/"))
        elif descriptor.suffix.lower() == ".gdi":
            # GDI track rows are: track lba type sector-size filename offset.
            # shlex handles quoted filenames. Real-world dumps also commonly leave
            # filenames containing spaces unquoted, so everything between the four
            # numeric fields and the final offset belongs to the filename.
            for line in lines[1:]:
                try:
                    fields = shlex.split(line, posix=True)
                except ValueError:
                    continue
                if len(fields) >= 6:
                    filename = " ".join(fields[4:-1])
                    refs.append(descriptor.parent / filename.replace("\\", "/"))
        return refs

    def _bundle_paths(self, primary: Path, cancel_check: CancelCheck | None = None) -> tuple[Path, ...]:
        root = self.settings.library_root.resolve()
        found: list[Path] = []
        seen: set[Path] = set()

        def visit(path: Path) -> None:
            if cancel_check:
                cancel_check()
            try:
                resolved = _inside(root, path)
            except (LibraryError, FileNotFoundError):
                return
            if resolved in seen or not resolved.is_file():
                return
            seen.add(resolved)
            found.append(resolved)
            if resolved.suffix.lower() in DESCRIPTOR_EXTENSIONS:
                for ref in self._descriptor_refs(resolved):
                    visit(ref)

        visit(primary)
        return tuple(found)

    def _candidate(
        self,
        primary: Path,
        platform: str,
        cache: dict[str, tuple[int, int, str]],
        cache_updates: dict[str, tuple[int, int, str]],
        paths: tuple[Path, ...] | None = None,
        progress: ScanProgress | None = None,
        cancel_check: CancelCheck | None = None,
        hash_contents: bool = True,
        display_name_override: str = "",
        path_stats: dict[str, os.stat_result] | None = None,
    ) -> GameCandidate:
        root = self.settings.library_root.resolve()
        paths = paths if paths is not None else self._bundle_paths(primary)
        records: list[FileRecord] = []
        for path in paths:
            relpath = _scan_rel(root, path)
            known_stat = path_stats.get(relpath) if path_stats else None
            stat = known_stat or path.stat()
            cached = cache.get(relpath)
            cache_hit = bool(
                cached
                and cached[0] == stat.st_size
                and cached[1] == stat.st_mtime_ns
            )
            if progress:
                progress.start_file(
                    platform,
                    relpath,
                    stat.st_size,
                    "cached" if hash_contents and cache_hit else "hashing" if hash_contents else "metadata",
                )
            if hash_contents:
                sha256, stat = self._hash_file(
                    path, relpath, cache, cache_updates, progress, cancel_check, stat
                )
            else:
                if cancel_check:
                    cancel_check()
                sha256 = ""
                if progress:
                    progress.finish_file(metadata_only=True, byte_count=stat.st_size)
            kind = "descriptor" if path.suffix.lower() in DESCRIPTOR_EXTENSIONS else "content"
            records.append(FileRecord(relpath, stat.st_size, sha256, kind, stat.st_mtime_ns))
        if not records:
            raise LibraryError(f"No readable files found for {primary}")
        aggregate = hashlib.sha256()
        if hash_contents:
            hash_records = [record for record in records if record.kind == "content"] or records
            for record in sorted(hash_records, key=lambda item: (item.sha256, item.size)):
                aggregate.update(record.sha256.encode("ascii"))
                aggregate.update(str(record.size).encode("ascii"))
            bundle_hash = aggregate.hexdigest()
        else:
            # Folder bundles are indexed from metadata so normal scans never read
            # terabytes of internal game data. Include the primary path to keep this
            # structural fingerprint out of exact-content duplicate groups.
            aggregate.update(b"metadata-only\0")
            aggregate.update(_scan_rel(root, primary).encode("utf-8"))
            for record in sorted(records, key=lambda item: item.relpath.casefold()):
                aggregate.update(record.relpath.encode("utf-8"))
                aggregate.update(str(record.size).encode("ascii"))
                aggregate.update(str(record.mtime_ns).encode("ascii"))
            bundle_hash = f"metadata:{aggregate.hexdigest()}"
        folder_extension = ""
        display_name = primary.stem
        if primary.is_dir():
            expected_suffix = f".{platform.casefold()}"
            folder_extension = primary.suffix.lower() if primary.suffix.lower() == expected_suffix else ""
            display_name = primary.stem if folder_extension else primary.name
        if display_name_override.strip():
            display_name = display_name_override.strip()
        return GameCandidate(
            platform=platform,
            primary_relpath=_scan_rel(root, primary),
            display_name=display_name,
            extension=folder_extension if primary.is_dir() else primary.suffix.lower(),
            size=sum(record.size for record in records),
            bundle_hash=bundle_hash,
            normalized_name=normalize_name(display_name),
            mtime_ns=max(record.mtime_ns for record in records),
            files=tuple(records),
        )

    def discover_candidates(
        self,
        cache: dict[str, tuple[int, int, str]] | None = None,
        cache_updates: dict[str, tuple[int, int, str]] | None = None,
        skipped: list[str] | None = None,
        progress_callback: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
        issue_callback: IssueCallback | None = None,
    ) -> list[GameCandidate]:
        root = self.settings.library_root.resolve()
        if cache is None:
            with self.db.connect() as connection:
                cache = {
                    row["relpath"]: (row["size"], row["mtime_ns"], row["sha256"])
                    for row in connection.execute("SELECT relpath,size,mtime_ns,sha256 FROM file_cache")
                }
        if cache_updates is None:
            cache_updates = {}
        if skipped is None:
            skipped = []

        def record_issue(path: Path, reason: object) -> None:
            try:
                relpath = path.relative_to(root).as_posix()
            except ValueError:
                relpath = path.name
            detail = f"{relpath}: {reason}"
            skipped.append(detail)
            if issue_callback:
                issue_callback(detail)

        candidates: list[GameCandidate] = []
        if not root.exists():
            return candidates
        if progress_callback:
            progress_callback(0, "Discovering library files")
        work: list[tuple[Path, str, tuple[Path, ...]]] = []
        metadata_only_primaries: set[Path] = set()
        display_overrides: dict[Path, str] = {}
        discovered_stats: dict[str, os.stat_result] = {}
        for platform_dir in sorted((item for item in root.iterdir() if item.is_dir()), key=lambda p: p.name.lower()):
            if cancel_check:
                cancel_check()
            platform_paths: list[Path] = []
            paths_by_top_level: dict[str, list[Path]] = {}
            for path in platform_dir.rglob("*"):
                if cancel_check:
                    cancel_check()
                try:
                    relative_parts = path.relative_to(platform_dir).parts
                except ValueError:
                    relative_parts = ()
                if any(part.startswith(".rommates-upload-") for part in relative_parts):
                    continue
                if platform_dir.name.casefold() == "switch" and _is_switch_support_path(path, platform_dir):
                    continue
                try:
                    path_stat = path.stat()
                except OSError:
                    continue
                if not stat_module.S_ISREG(path_stat.st_mode):
                    continue
                platform_paths.append(path)
                discovered_stats[_scan_rel(root, path)] = path_stat
                if relative_parts:
                    paths_by_top_level.setdefault(relative_parts[0], []).append(path)
            claimed: set[Path] = set()
            bundle_paths: dict[Path, tuple[Path, ...]] = {}
            primaries: list[Path] = []

            if platform_dir.name.casefold() in VITA_PLATFORM_NAMES:
                app_root = platform_dir / "app"
                app_ids = {
                    child.name.upper(): child
                    for child in (app_root.iterdir() if app_root.is_dir() else ())
                    if child.is_dir() and VITA_TITLE_ID_RE.fullmatch(child.name)
                }
                grouped: dict[str, list[Path]] = {title_id: [] for title_id in app_ids}
                for path in platform_paths:
                    relative = path.relative_to(platform_dir)
                    if not relative.parts or relative.parts[0].casefold() not in VITA_CONTENT_ROOTS:
                        continue
                    if path.name.startswith("._") or path.name == ".DS_Store":
                        claimed.add(path)
                        continue
                    if path.is_symlink():
                        record_issue(path, "symbolic links are not indexed")
                        claimed.add(path)
                        continue
                    claimed.add(path)
                    title_id = next(
                        (
                            part.upper()
                            for part in relative.parts[1:]
                            if VITA_TITLE_ID_RE.fullmatch(part)
                        ),
                        "",
                    )
                    if title_id in grouped:
                        grouped[title_id].append(path)
                for title_id, primary in sorted(app_ids.items()):
                    files = tuple(
                        sorted(grouped[title_id], key=lambda item: item.as_posix().casefold())
                    )
                    if not files:
                        continue
                    primaries.append(primary)
                    bundle_paths[primary] = files
                    metadata_only_primaries.add(primary)
                    sfo = next(
                        (
                            path for path in files
                            if path.name.casefold() == "param.sfo"
                            and "sce_sys" in {part.casefold() for part in path.parts}
                        ),
                        None,
                    )
                    display_overrides[primary] = _read_sfo_title(sfo) if sfo else title_id

            elif platform_dir.name.casefold() in self.settings.folder_bundle_platforms:
                for folder in sorted(
                    (
                        path for path in platform_dir.iterdir()
                        if path.is_dir() and not path.name.startswith(".rommates-upload-")
                        and not (
                            platform_dir.name.casefold() == "switch"
                            and _is_switch_support_path(path, platform_dir)
                        )
                    ),
                    key=lambda path: path.name.casefold(),
                ):
                    files: list[Path] = []
                    for path in sorted(
                        paths_by_top_level.get(folder.name, ()),
                        key=lambda item: item.as_posix().casefold(),
                    ):
                        if cancel_check:
                            cancel_check()
                        if path.name.startswith("._") or path.name == ".DS_Store":
                            continue
                        if path.is_symlink():
                            record_issue(path, "symbolic links are not indexed")
                            continue
                        files.append(path)
                    if files:
                        primaries.append(folder)
                        bundle_paths[folder] = tuple(files)
                        metadata_only_primaries.add(folder)
                        claimed.update(files)

            descriptors = sorted(
                (
                    path for path in platform_paths
                    if path.suffix.lower() in DESCRIPTOR_EXTENSIONS
                    and not path.name.startswith("._")
                    and path not in claimed
                ),
                key=lambda p: (
                    0 if p.suffix.lower() == ".m3u" else 1,
                    p.as_posix().lower(),
                ),
            )
            referenced: set[Path] = set()
            for descriptor in descriptors:
                resolved = descriptor.resolve()
                if resolved in referenced:
                    continue
                primaries.append(descriptor)
                bundle = self._bundle_paths(descriptor, cancel_check)
                relative = descriptor.relative_to(platform_dir)
                if (
                    platform_dir.name.casefold() in DISC_FOLDER_PLATFORMS
                    and len(relative.parts) > 1
                ):
                    # Optical-disc dumps commonly keep one game beneath one folder.
                    # Claim the complete folder so malformed/missing descriptor rows,
                    # repeated audio tracks, and multi-disc subfolders never become
                    # standalone games or cross-title duplicate groups.
                    game_folder = platform_dir / relative.parts[0]
                    folder_files: list[Path] = []
                    for path in sorted(
                        paths_by_top_level.get(relative.parts[0], ()),
                        key=lambda item: item.as_posix().casefold(),
                    ):
                        if cancel_check:
                            cancel_check()
                        if path.name.startswith("._") or path.name == ".DS_Store":
                            continue
                        if path.is_symlink():
                            record_issue(path, "symbolic links are not indexed")
                            continue
                        folder_files.append(path.resolve())
                    if folder_files:
                        bundle = tuple(dict.fromkeys([*bundle, *folder_files]))
                bundle_paths[descriptor] = bundle
                referenced.update(path for path in bundle if path != resolved)

            for path in sorted(platform_paths, key=lambda p: p.as_posix().lower()):
                if cancel_check:
                    cancel_check()
                if path.name.startswith("._") or path.name == ".DS_Store":
                    continue
                if path.suffix.lower() not in self.settings.extensions:
                    continue
                if path.is_symlink():
                    record_issue(path, "symbolic links are not indexed")
                    continue
                if path in claimed or path.resolve() in referenced or path in primaries:
                    continue
                primaries.append(path)

            for primary in primaries:
                if cancel_check:
                    cancel_check()
                work.append(
                    (
                        primary,
                        platform_dir.name,
                        bundle_paths.get(primary) or self._bundle_paths(primary, cancel_check),
                    )
                )

        total_bytes = 0
        total_files = 0
        path_stats: dict[str, os.stat_result] = {}
        platform_plans: dict[str, dict[str, int]] = {}
        prepared_work: list[tuple[Path, str, tuple[Path, ...], bool]] = []
        for primary, platform, paths in work:
            candidate_stats: list[tuple[str, os.stat_result]] = []
            for path in paths:
                if cancel_check:
                    cancel_check()
                try:
                    relpath = _scan_rel(root, path)
                    stat = discovered_stats.get(relpath) or path.stat()
                    path_stats[relpath] = stat
                    candidate_stats.append((relpath, stat))
                    total_files += 1
                except OSError:
                    # Candidate construction records the useful per-file error.
                    pass
            hash_contents = primary not in metadata_only_primaries
            if hash_contents and self.settings.hash_max_bytes > 0:
                hash_contents = all(
                    stat.st_size <= self.settings.hash_max_bytes
                    or (
                        (cached := cache.get(relpath)) is not None
                        and cached[0] == stat.st_size
                        and cached[1] == stat.st_mtime_ns
                    )
                    for relpath, stat in candidate_stats
                )
            if hash_contents:
                total_bytes += sum(
                    stat.st_size
                    for relpath, stat in candidate_stats
                    if not (
                        (cached := cache.get(relpath)) is not None
                        and cached[0] == stat.st_size
                        and cached[1] == stat.st_mtime_ns
                    )
                )
            platform_plan = platform_plans.setdefault(
                platform,
                {
                    "total_files": 0,
                    "hash_files": 0,
                    "hash_bytes": 0,
                    "cached_files": 0,
                    "cached_bytes": 0,
                    "metadata_files": 0,
                    "metadata_bytes": 0,
                    "processed_files": 0,
                    "processed_hash_files": 0,
                    "processed_cached_files": 0,
                    "processed_metadata_files": 0,
                    "read_bytes": 0,
                },
            )
            for relpath, stat in candidate_stats:
                platform_plan["total_files"] += 1
                cached = cache.get(relpath)
                cache_hit = bool(
                    cached
                    and cached[0] == stat.st_size
                    and cached[1] == stat.st_mtime_ns
                )
                if not hash_contents:
                    platform_plan["metadata_files"] += 1
                    platform_plan["metadata_bytes"] += stat.st_size
                elif cache_hit:
                    platform_plan["cached_files"] += 1
                    platform_plan["cached_bytes"] += stat.st_size
                else:
                    platform_plan["hash_files"] += 1
                    platform_plan["hash_bytes"] += stat.st_size
            prepared_work.append((primary, platform, paths, hash_contents))
        progress = ScanProgress(total_files, total_bytes, progress_callback, platform_plans)
        progress._emit()
        try:
            for primary, platform, paths, hash_contents in prepared_work:
                if cancel_check:
                    cancel_check()
                try:
                    candidates.append(
                        self._candidate(
                            primary,
                            platform,
                            cache,
                            cache_updates,
                            paths,
                            progress,
                            cancel_check,
                            hash_contents=hash_contents,
                            display_name_override=display_overrides.get(primary, ""),
                            path_stats=path_stats,
                        )
                    )
                except (OSError, LibraryError) as exc:
                    record_issue(primary, exc)
        finally:
            # Normal Python exceptions still preserve everything hashed so far. A hard
            # container stop can lose at most the current bounded batch.
            self._persist_hash_cache(cache_updates)
        progress._emit(final=True)
        return candidates

    def _persist_hash_cache(self, cache_updates: dict[str, tuple[int, int, str]]) -> None:
        if not cache_updates:
            return
        with self.db.write() as connection:
            connection.executemany(
                "INSERT INTO file_cache(relpath,size,mtime_ns,sha256) VALUES(?,?,?,?) "
                "ON CONFLICT(relpath) DO UPDATE SET size=excluded.size,mtime_ns=excluded.mtime_ns,sha256=excluded.sha256",
                (
                    (relpath, values[0], values[1], values[2])
                    for relpath, values in cache_updates.items()
                ),
            )
        cache_updates.clear()

    def _discover_devices(self, connection) -> list[str]:
        """Register device folders and drop ones whose directory is gone.

        Returns the names of removed devices. Pruning is skipped entirely when the
        devices root looks empty or unreadable, so an unmounted volume cannot
        cascade-delete every device selection.
        """
        root = self.settings.devices_root.resolve()
        if not root.exists():
            return []
        try:
            directories = sorted(item for item in root.iterdir() if item.is_dir())
        except OSError:
            return []
        present: list[str] = []
        for directory in directories:
            roms = directory / "roms"
            if roms.is_dir():
                present.append(directory.name)
                connection.execute(
                    "INSERT INTO devices(name,path) VALUES(?,?) "
                    "ON CONFLICT(name) DO UPDATE SET path=excluded.path",
                    (directory.name, directory.name),
                )
        known = [row["name"] for row in connection.execute("SELECT name FROM devices")]
        if not present:
            # Nothing discovered: treat as an unavailable mount rather than a deletion.
            return []
        stale = [name for name in known if name not in present]
        if stale:
            placeholders = ",".join("?" for _ in stale)
            connection.execute(f"DELETE FROM devices WHERE name IN ({placeholders})", stale)
        return stale

    def create_device(
        self,
        name: str,
        deployment_mode: str = "hardlink",
        owner_user_id: int | None = None,
        delivery_mode: str = "syncthing",
    ) -> dict[str, object]:
        """Create and register a device directory without requiring a library scan."""
        device_name = name.strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", device_name):
            raise LibraryError(
                "Device names must be 1 to 64 characters using letters, numbers, dots, dashes, or underscores"
            )
        if device_name in {".", ".."}:
            raise LibraryError("Choose a different device name")
        if deployment_mode != "hardlink":
            raise LibraryError("ROMmates always deploys device ROMs as hardlinks where supported")
        if delivery_mode not in {"syncthing", "download"}:
            raise LibraryError("Delivery mode must be syncthing or download")

        root = self.settings.devices_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        device_root = _inside(root, root / device_name)
        if device_root.is_symlink():
            raise LibraryError("Device folders cannot be symbolic links")
        if device_root.exists() and not device_root.is_dir():
            raise LibraryError("A file already uses that device name")

        with self.db.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM devices WHERE name=? OR path=?", (device_name, device_name)
            ).fetchone()
        if existing:
            raise LibraryError("A device with that name already exists")

        roms_root = device_root / "roms"
        try:
            roms_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LibraryError(f"Could not create the device folder: {exc}") from exc
        if roms_root.is_symlink() or not roms_root.is_dir():
            raise LibraryError("The device ROM path is not a regular directory")

        with self.db.write() as connection:
            connection.execute(
                "INSERT INTO devices(name,path,deployment_mode,owner_user_id,delivery_mode) VALUES(?,?,?,?,?)",
                (device_name, device_name, "hardlink", owner_user_id, delivery_mode),
            )
            row = connection.execute(
                "SELECT * FROM devices WHERE id=last_insert_rowid()"
            ).fetchone()
        self.db.activity("device", f"Created device {device_name}")
        return {
            **dict(row),
            "roms_path": str(roms_root),
            "relative_path": f"devices/{device_name}/roms",
        }

    def scan(
        self,
        force_prune: bool = False,
        progress_callback: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
        issue_callback: IssueCallback | None = None,
    ) -> dict[str, object]:
        with self._operation_lock:
            with self.db.connect() as connection:
                cache = {
                    row["relpath"]: (row["size"], row["mtime_ns"], row["sha256"])
                    for row in connection.execute("SELECT relpath,size,mtime_ns,sha256 FROM file_cache")
                }
                known_relpaths = {
                    row["primary_relpath"]
                    for row in connection.execute("SELECT primary_relpath FROM games")
                }
            cache_updates: dict[str, tuple[int, int, str]] = {}
            skipped: list[str] = []
            candidates = self.discover_candidates(
                cache, cache_updates, skipped, progress_callback, cancel_check, issue_callback
            )
            if cancel_check:
                cancel_check()
            seen = {candidate.primary_relpath for candidate in candidates}
            component_owners = {
                item.relpath: candidate.primary_relpath
                for candidate in candidates
                for item in candidate.files
                if item.relpath != candidate.primary_relpath
            }
            covered_relpaths = set(component_owners)
            # Check before writing anything: the guard aborts the whole scan.
            self._guard_prune(
                known_relpaths,
                seen,
                skipped,
                force_prune,
                covered_relpaths,
            )
            if progress_callback:
                progress_callback(92, f"Updating catalog with {len(candidates):,} games")
            with self.db.write() as connection:
                if cancel_check:
                    cancel_check()
                removed_devices = self._discover_devices(connection)
                for candidate in candidates:
                    if cancel_check:
                        cancel_check()
                    connection.execute(
                        "INSERT INTO games(platform,primary_relpath,display_name,extension,size,bundle_hash,normalized_name,mtime_ns) "
                        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(primary_relpath) DO UPDATE SET "
                        "platform=excluded.platform,display_name=excluded.display_name,extension=excluded.extension,"
                        "size=excluded.size,bundle_hash=excluded.bundle_hash,normalized_name=excluded.normalized_name,"
                        "mtime_ns=excluded.mtime_ns,updated_at=CURRENT_TIMESTAMP",
                        (
                            candidate.platform, candidate.primary_relpath, candidate.display_name,
                            candidate.extension, candidate.size, candidate.bundle_hash,
                            candidate.normalized_name, candidate.mtime_ns,
                        ),
                    )
                    game_id = connection.execute(
                        "SELECT id FROM games WHERE primary_relpath=?", (candidate.primary_relpath,)
                    ).fetchone()["id"]
                    connection.execute("DELETE FROM game_files WHERE game_id=?", (game_id,))
                    connection.executemany(
                        "INSERT INTO game_files(game_id,relpath,device_relpath,size,sha256,kind) VALUES(?,?,?,?,?,?)",
                        (
                            (
                                game_id,
                                item.relpath,
                                esde_device_relpath(candidate.platform, item.relpath),
                                item.size,
                                item.sha256,
                                item.kind,
                            )
                            for item in candidate.files
                        ),
                    )
                reconciled_components = 0
                if component_owners:
                    connection.execute(
                        "CREATE TEMP TABLE IF NOT EXISTS bundle_ownership("
                        "component_relpath TEXT PRIMARY KEY,owner_relpath TEXT NOT NULL)"
                    )
                    connection.execute("DELETE FROM bundle_ownership")
                    connection.executemany(
                        "INSERT OR REPLACE INTO bundle_ownership(component_relpath,owner_relpath) "
                        "VALUES(?,?)",
                        component_owners.items(),
                    )
                    reconciled_components = connection.execute(
                        "SELECT COUNT(*) AS count FROM games old "
                        "JOIN bundle_ownership bo ON bo.component_relpath=old.primary_relpath "
                        "WHERE old.primary_relpath<>bo.owner_relpath"
                    ).fetchone()["count"]
                    # Preserve device intent and managed-copy history when upgrading
                    # a catalog that previously treated bundle components as games.
                    connection.execute(
                        "INSERT OR IGNORE INTO device_selections(device_id,game_id) "
                        "SELECT ds.device_id,owner.id FROM device_selections ds "
                        "JOIN games old ON old.id=ds.game_id "
                        "JOIN bundle_ownership bo ON bo.component_relpath=old.primary_relpath "
                        "JOIN games owner ON owner.primary_relpath=bo.owner_relpath"
                    )
                    connection.execute(
                        "INSERT OR IGNORE INTO deployments(device_id,game_id,relpath) "
                        "SELECT dp.device_id,owner.id,dp.relpath FROM deployments dp "
                        "JOIN games old ON old.id=dp.game_id "
                        "JOIN bundle_ownership bo ON bo.component_relpath=old.primary_relpath "
                        "JOIN games owner ON owner.primary_relpath=bo.owner_relpath"
                    )
                    connection.execute("DROP TABLE bundle_ownership")
                if seen:
                    connection.execute("CREATE TEMP TABLE IF NOT EXISTS scan_seen(relpath TEXT PRIMARY KEY)")
                    connection.execute("DELETE FROM scan_seen")
                    connection.executemany(
                        "INSERT OR IGNORE INTO scan_seen(relpath) VALUES(?)",
                        ((relpath,) for relpath in seen),
                    )
                    connection.execute(
                        "DELETE FROM games WHERE primary_relpath NOT IN (SELECT relpath FROM scan_seen)"
                    )
                    connection.execute("DROP TABLE scan_seen")
                else:
                    connection.execute("DELETE FROM games")
                # A changed large file may still have an older full hash in the
                # cache. Once it is deliberately deferred, remove that stale value
                # so a later timestamp rollback cannot revive the wrong digest.
                connection.execute(
                    "DELETE FROM file_cache WHERE relpath IN ("
                    "SELECT relpath FROM game_files WHERE sha256='')"
                )
                connection.execute("DELETE FROM file_cache WHERE relpath NOT IN (SELECT relpath FROM game_files)")
            if progress_callback:
                progress_callback(99, "Finalizing scan")
            metadata_files = sum(
                1 for candidate in candidates for item in candidate.files if not item.sha256
            )
            metadata_games = sum(
                1 for candidate in candidates if candidate.bundle_hash.startswith("metadata:")
            )
            detail = f"Indexed {len(candidates)} games"
            if metadata_files:
                detail += (
                    f", used metadata for {metadata_files} files across "
                    f"{metadata_games} large or folder-based games"
                )
            if reconciled_components:
                detail += f", merged {reconciled_components} legacy bundle components"
            if skipped:
                detail += f", skipped {len(skipped)} unreadable files"
            if removed_devices:
                detail += f", removed device {', '.join(removed_devices)}"
            self._device_inventory_cache.clear()
            self.db.activity("scan", detail)
            self.db.prune_history()
            return {
                "games": len(candidates),
                "platforms": len({candidate.platform for candidate in candidates}),
                "skipped": skipped[:50],
                "skipped_count": len(skipped),
                "removed_devices": removed_devices,
                "metadata_files": metadata_files,
                "metadata_games": metadata_games,
            }

    def _guard_prune(
        self,
        known_relpaths: set[str],
        seen: set[str],
        skipped: list[str],
        force_prune: bool,
        covered_relpaths: set[str] | None = None,
    ) -> None:
        """Refuse to reconcile when a scan would delete an implausible share of the catalog.

        Deleting a ``games`` row cascades into ``device_selections`` and ``deployments``,
        so a library root that is present but empty or partially readable (an unmounted
        volume, a mount race at boot, a disconnected network share) would otherwise wipe
        every device selection without warning.
        """
        if force_prune or not known_relpaths:
            return
        # Old scanner versions indexed individual BIN/RAW/PS3 component files as
        # games. They are not missing when a new logical bundle now owns the same
        # path, so allow that catalog repair without a destructive-prune warning.
        missing = known_relpaths - seen - (covered_relpaths or set())
        if not missing:
            return
        share = len(missing) / len(known_relpaths)
        if share < self.settings.scan_prune_limit:
            return
        reason = (
            f"Scan would remove {len(missing)} of {len(known_relpaths)} indexed games "
            f"({share:.0%} of the catalog) and every device selection that depends on them. "
            "This usually means the library volume is unmounted, still mounting, or only "
            "partially readable, so nothing was changed."
        )
        if skipped:
            reason += f" {len(skipped)} files could not be read, for example: {skipped[0]}"
        reason += " Confirm the library root is fully mounted, then rescan with pruning confirmed."
        raise LibraryError(reason)

    @staticmethod
    def _atomic_move(source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            source.rename(target)
        except OSError as exc:
            if exc.errno != errno.EXDEV:
                raise

            # A library can contain nested mounts even when its roots share one
            # Docker bind mount. Build the destination completely before removing
            # the source so a failed copy never destroys the only good ROM copy.
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".rommates-move-", dir=target.parent
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            try:
                shutil.copy2(source, temporary)
                with temporary.open("rb") as handle:
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
                try:
                    source.unlink()
                except OSError:
                    target.unlink(missing_ok=True)
                    raise
            finally:
                temporary.unlink(missing_ok=True)

    @staticmethod
    def _remove_empty_tree(root: Path) -> None:
        """Remove only empty directories, deepest first, leaving unknown files alone."""
        if not root.is_dir():
            return
        directories = sorted(
            (path for path in root.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        )
        for directory in [*directories, root]:
            try:
                directory.rmdir()
            except OSError:
                pass

    def game_bundle(self, game_id: int):
        with self.db.connect() as connection:
            game = connection.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
            if not game:
                raise LibraryError("Game was not found")
            files = connection.execute(
                "SELECT * FROM game_files WHERE game_id=? ORDER BY kind DESC, relpath", (game_id,)
            ).fetchall()
            return dict(game), [dict(row) for row in files]

    def _rewrite_descriptor(
        self, old_path: Path, new_path: Path, mapping: dict[Path, Path], original: str
    ) -> str:
        lines = original.splitlines(keepends=True)
        output: list[str] = []
        for line in lines:
            if old_path.suffix.lower() == ".cue":
                match = CUE_FILE_RE.match(line)
                if match:
                    old_ref = (old_path.parent / match.group(1).replace("\\", "/")).resolve()
                    if old_ref in mapping:
                        new_ref = os.path.relpath(mapping[old_ref], new_path.parent).replace(os.sep, "/")
                        line = line[: match.start(1)] + new_ref + line[match.end(1) :]
            elif old_path.suffix.lower() == ".m3u":
                value = line.strip()
                if value and not value.startswith("#"):
                    old_ref = (old_path.parent / value.replace("\\", "/")).resolve()
                    if old_ref in mapping:
                        newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
                        line = os.path.relpath(mapping[old_ref], new_path.parent).replace(os.sep, "/") + newline
            elif old_path.suffix.lower() == ".gdi":
                newline = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
                body = line[: -len(newline)] if newline else line
                match = GDI_FILE_RE.match(body)
                if match:
                    value = match.group(2) or match.group(3)
                    old_ref = (old_path.parent / value.replace("\\", "/")).resolve()
                    if old_ref in mapping:
                        new_ref = os.path.relpath(mapping[old_ref], new_path.parent).replace(os.sep, "/")
                        if match.group(2) is not None or " " in new_ref:
                            new_ref = f'"{new_ref}"'
                        line = match.group(1) + new_ref + match.group(4) + newline
            output.append(line)
        return "".join(output)

    def _rename_plan(self, game_id: int, requested_name: str):
        game, files = self.game_bundle(game_id)
        requested_name = requested_name.strip()
        if not requested_name or requested_name in {".", ".."} or "/" in requested_name or "\\" in requested_name:
            raise LibraryError("Enter a filename without folders or path separators")
        root = self.settings.library_root.resolve()
        primary = _inside(root, root / game["primary_relpath"])
        extension = primary.suffix
        new_stem = (
            Path(requested_name).stem
            if extension and Path(requested_name).suffix.lower() == extension.lower()
            else requested_name
        )
        new_stem = new_stem.strip().rstrip(".")
        if not new_stem:
            raise LibraryError("Enter a valid filename")
        paths = [_inside(root, root / item["relpath"]) for item in files]
        mapping: dict[Path, Path] = {}
        if primary.is_dir():
            if any(primary not in old.parents for old in paths):
                raise LibraryError(
                    "This title-ID bundle spans multiple system directories and cannot be renamed"
                )
            primary_target = _inside(root, primary.with_name(new_stem + extension))
            if primary_target.exists() and primary_target != primary:
                raise LibraryError(f"A folder named {primary_target.name} already exists")
            for old in paths:
                mapping[old] = _inside(root, primary_target / old.relative_to(primary))
        else:
            primary_target = primary.with_name(new_stem + extension)
            old_stem = primary.stem
            for old in paths:
                if old == primary:
                    target_name = new_stem + old.suffix
                elif old.stem.startswith(old_stem):
                    target_name = new_stem + old.stem[len(old_stem) :] + old.suffix
                else:
                    target_name = old.name
                mapping[old] = _inside(root, old.with_name(target_name))
        targets = list(mapping.values())
        if len(set(targets)) != len(targets):
            raise LibraryError("The rename would give two bundle files the same name")
        for old, target in mapping.items():
            if target.exists() and target not in mapping:
                raise LibraryError(f"A file named {target.name} already exists")
        return game, files, root, primary, primary_target, new_stem, paths, mapping

    def preview_rename(self, game_id: int, requested_name: str) -> dict[str, object]:
        game, _, root, _, _, new_stem, _, mapping = self._rename_plan(game_id, requested_name)
        return {
            "game_id": game_id,
            "old_name": game["display_name"],
            "new_name": new_stem,
            "targets": [_rel(root, target) for target in mapping.values()],
        }

    def rename_bundle(self, game_id: int, requested_name: str, rescan: bool = True) -> dict[str, str]:
        with self._operation_lock:
            game, files, root, primary, primary_target, new_stem, paths, mapping = self._rename_plan(game_id, requested_name)

            descriptor_original: dict[Path, str] = {}
            for old in paths:
                if old.suffix.lower() in DESCRIPTOR_EXTENSIONS:
                    descriptor_original[old] = old.read_text(encoding="utf-8-sig", errors="replace")

            moved: list[tuple[Path, Path, Path]] = []
            try:
                for index, (old, target) in enumerate(mapping.items()):
                    if old == target:
                        continue
                    temp = old.with_name(f".rommates-rename-{game_id}-{index}{old.suffix}")
                    if temp.exists():
                        raise LibraryError(f"Temporary rename path already exists: {temp.name}")
                    old.rename(temp)
                    moved.append((old, temp, target))
                for _, temp, target in moved:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    temp.rename(target)
                for old_descriptor, original in descriptor_original.items():
                    new_descriptor = mapping[old_descriptor]
                    updated = self._rewrite_descriptor(old_descriptor, new_descriptor, mapping, original)
                    new_descriptor.write_text(updated, encoding="utf-8")
                if primary.is_dir() and primary != primary_target:
                    self._remove_empty_tree(primary)
            except Exception:
                for old, temp, target in reversed(moved):
                    current = target if target.exists() else temp
                    if current.exists() and not old.exists():
                        current.rename(old)
                for old_descriptor, original in descriptor_original.items():
                    if old_descriptor.exists():
                        old_descriptor.write_text(original, encoding="utf-8")
                raise

            new_primary_relpath = _rel(root, primary_target)
            with self.db.write() as connection:
                connection.execute(
                    "UPDATE games SET primary_relpath=?,display_name=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (new_primary_relpath, new_stem, game_id),
                )
                for old, target in mapping.items():
                    connection.execute(
                        "UPDATE game_files SET relpath=?,device_relpath=? WHERE game_id=? AND relpath=?",
                        (
                            _rel(root, target),
                            esde_device_relpath(game["platform"], _rel(root, target)),
                            game_id,
                            _rel(root, old),
                        ),
                    )
                    connection.execute(
                        "DELETE FROM file_cache WHERE relpath=? AND relpath<>?",
                        (_rel(root, target), _rel(root, old)),
                    )
                    connection.execute(
                        "UPDATE file_cache SET relpath=? WHERE relpath=?",
                        (_rel(root, target), _rel(root, old)),
                    )
                connection.execute("DELETE FROM file_cache WHERE relpath NOT IN (SELECT relpath FROM game_files)")
            if rescan:
                self.scan()
            self.db.activity("rename", f"Renamed {game['display_name']} to {new_stem}")
            return {"old_name": game["display_name"], "new_name": new_stem}

    def bulk_rename(self, renames: list[tuple[int, str]]) -> dict[str, object]:
        if not renames:
            raise LibraryError("Select at least one naming suggestion")
        if len(renames) > 500:
            raise LibraryError("Apply at most 500 naming suggestions at once")
        with self._operation_lock:
            # Validate every target before touching the filesystem. Reusing the same
            # validation in rename_bundle keeps collision and bundle rules identical.
            seen_ids: set[int] = set()
            all_targets: list[Path] = []
            for game_id, requested_name in renames:
                if game_id in seen_ids:
                    raise LibraryError("A game can only appear once in a rename batch")
                seen_ids.add(game_id)
                *_, mapping = self._rename_plan(game_id, requested_name)
                all_targets.extend(mapping.values())
            if len(set(all_targets)) != len(all_targets):
                raise LibraryError("Two naming suggestions would create the same target file")
            results = [self.rename_bundle(game_id, name, rescan=False) for game_id, name in renames]
            self.scan()
            self.db.activity("bulk_rename", f"Applied {len(results)} naming suggestions")
            return {"renamed": len(results), "items": results}

    def delete_bundle(self, game_id: int) -> dict[str, object]:
        with self._operation_lock:
            game, files = self.game_bundle(game_id)
            root = self.settings.library_root.resolve()
            primary = _inside(root, root / game["primary_relpath"])
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            batch_root = _inside(self.settings.trash_root, self.settings.trash_root / stamp)
            batch_root.mkdir(parents=True, exist_ok=False)
            with self.db.connect() as connection:
                selections = [
                    row["name"] for row in connection.execute(
                        "SELECT d.name FROM device_selections ds JOIN devices d ON d.id=ds.device_id WHERE ds.game_id=?",
                        (game_id,),
                    )
                ]
                deployments = [dict(row) for row in connection.execute(
                    "SELECT d.path,dp.relpath FROM deployments dp "
                    "JOIN devices d ON d.id=dp.device_id WHERE dp.game_id=?",
                    (game_id,),
                )]
            moved: list[tuple[Path, Path]] = []
            moved_devices: list[dict[str, str]] = []
            try:
                for item in files:
                    source = _inside(root, root / item["relpath"])
                    target = _inside(batch_root, batch_root / item["relpath"])
                    self._atomic_move(source, target)
                    moved.append((source, target))
                if primary.is_dir():
                    self._remove_empty_tree(primary)
                for deployment in deployments:
                    source = _inside(
                        self.settings.devices_root,
                        self.settings.devices_root / deployment["path"] / "roms" / deployment["relpath"],
                    )
                    if source.is_file():
                        trash_relpath = Path("__devices__") / deployment["path"] / "roms" / deployment["relpath"]
                        target = _inside(batch_root, batch_root / trash_relpath)
                        self._atomic_move(source, target)
                        moved.append((source, target))
                        moved_devices.append(
                            {
                                "device_path": deployment["path"],
                                "relpath": deployment["relpath"],
                                "trash_relpath": trash_relpath.as_posix(),
                            }
                        )
            except Exception:
                for source, target in reversed(moved):
                    if target.exists() and not source.exists():
                        self._atomic_move(target, source)
                raise

            manifest = {
                "primary_relpath": game["primary_relpath"],
                "files": [item["relpath"] for item in files],
                "selected_devices": selections,
                "device_files": moved_devices,
            }
            try:
                with self.db.write() as connection:
                    connection.execute(
                        "INSERT INTO trash_items(original_relpath,trash_relpath,game_name,platform,manifest_json) VALUES(?,?,?,?,?)",
                        (game["primary_relpath"], stamp, game["display_name"], game["platform"], json.dumps(manifest)),
                    )
                    trash_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                    connection.execute("DELETE FROM games WHERE id=?", (game_id,))
            except Exception:
                for source, target in reversed(moved):
                    if target.exists() and not source.exists():
                        self._atomic_move(target, source)
                raise
            self.db.activity("delete", f"Moved {game['display_name']} to trash")
            return {"trash_id": trash_id, "files": len(files), "devices_affected": len(set(selections))}

    def bulk_delete_duplicates(self, decisions: list[dict[str, object]]) -> dict[str, object]:
        if not decisions:
            raise LibraryError("Choose at least one duplicate keeper")
        if len(decisions) > 500:
            raise LibraryError("Review at most 500 duplicate groups at once")
        with self._operation_lock:
            kinds = {str(item.get("kind") or "") for item in decisions}
            if len(kinds) != 1 or not kinds.issubset({"exact", "possible"}):
                raise LibraryError("A duplicate cleanup batch must use one duplicate type")
            plans: list[dict[str, object]] = []
            seen_keys: set[str] = set()
            planned_removals: set[int] = set()
            with self.db.connect() as connection:
                for decision in decisions:
                    kind = str(decision["kind"])
                    key = str(decision.get("group_key") or "")
                    keeper_id = int(decision.get("keeper_id") or 0)
                    if not key or key in seen_keys:
                        raise LibraryError("Each duplicate group can only appear once")
                    seen_keys.add(key)
                    if kind == "exact":
                        members = [dict(row) for row in connection.execute(
                            "SELECT id,display_name,primary_relpath FROM games WHERE bundle_hash=? ORDER BY id",
                            (key,),
                        )]
                    else:
                        if "\x1f" not in key:
                            raise LibraryError("A similarly named duplicate group is invalid")
                        platform, normalized_name = key.split("\x1f", 1)
                        members = [dict(row) for row in connection.execute(
                            "SELECT id,display_name,primary_relpath FROM games "
                            "WHERE platform=? AND normalized_name=? ORDER BY id",
                            (platform, normalized_name),
                        )]
                        if len({row["id"] for row in members}) > 1:
                            distinct_hashes = connection.execute(
                                "SELECT COUNT(DISTINCT bundle_hash) AS count FROM games "
                                "WHERE platform=? AND normalized_name=?",
                                (platform, normalized_name),
                            ).fetchone()["count"]
                            if distinct_hashes < 2:
                                members = []
                    member_ids = {member["id"] for member in members}
                    if len(members) < 2 or keeper_id not in member_ids:
                        raise LibraryError("A duplicate group changed after review; refresh and choose its keeper again")
                    removals = [member for member in members if member["id"] != keeper_id]
                    overlap = planned_removals.intersection(member["id"] for member in removals)
                    if overlap:
                        raise LibraryError("A ROM cannot be removed by two duplicate decisions")
                    planned_removals.update(member["id"] for member in removals)
                    plans.append({"key": key, "keeper_id": keeper_id, "members": members, "removals": removals})

                all_ids = [member["id"] for plan in plans for member in plan["members"]]
                placeholders = ",".join("?" for _ in all_ids)
                selected_ids = {
                    row["game_id"] for row in connection.execute(
                        f"SELECT DISTINCT game_id FROM device_selections WHERE game_id IN ({placeholders})",
                        all_ids,
                    )
                }
                files_by_game: dict[int, list[str]] = {game_id: [] for game_id in all_ids}
                for row in connection.execute(
                    f"SELECT game_id,device_relpath AS relpath FROM game_files WHERE game_id IN ({placeholders})",
                    all_ids,
                ):
                    files_by_game[row["game_id"]].append(row["relpath"])
                device_ids = [row["id"] for row in connection.execute("SELECT id FROM devices")]

            inventory_paths: set[str] = set()
            for device_id in device_ids:
                inventory_paths.update(self.device_inventory(device_id, refresh=True))
            present_ids = {
                game_id for game_id, relpaths in files_by_game.items()
                if any(relpath in inventory_paths for relpath in relpaths)
            }
            in_use_ids = selected_ids | present_ids
            for plan in plans:
                used = [member for member in plan["members"] if member["id"] in in_use_ids]
                if len(used) > 1:
                    raise LibraryError(
                        "Multiple copies in one reviewed group are used by devices; resolve that group before bulk cleanup"
                    )

            if len(planned_removals) > 2000:
                raise LibraryError("Trash at most 2,000 duplicate bundles in one job")
            results: list[dict[str, object]] = []
            try:
                for plan in plans:
                    for member in plan["removals"]:
                        results.append(self.delete_bundle(member["id"]))
            except Exception as exc:
                raise LibraryError(
                    f"Moved {len(results)} duplicate bundles to recoverable Trash before stopping: {exc}"
                ) from exc
            self.db.activity(
                "bulk_delete",
                f"Kept {len(plans)} reviewed copies and moved {len(results)} duplicate bundles to trash",
            )
            return {
                "groups": len(plans),
                "trashed": len(results),
                "trash_ids": [result["trash_id"] for result in results],
            }

    def restore_trash(self, trash_id: int) -> dict[str, object]:
        with self._operation_lock:
            with self.db.connect() as connection:
                row = connection.execute("SELECT * FROM trash_items WHERE id=?", (trash_id,)).fetchone()
            if not row:
                raise LibraryError("Trash item was not found")
            manifest = json.loads(row["manifest_json"])
            batch_root = _inside(self.settings.trash_root, self.settings.trash_root / row["trash_relpath"])
            root = self.settings.library_root.resolve()
            restore_pairs: list[tuple[Path, Path]] = []
            for relpath in manifest["files"]:
                source = _inside(batch_root, batch_root / relpath)
                target = _inside(root, root / relpath)
                restore_pairs.append((source, target))
            for device_file in manifest.get("device_files", []):
                source = _inside(batch_root, batch_root / device_file["trash_relpath"])
                target = _inside(
                    self.settings.devices_root,
                    self.settings.devices_root / device_file["device_path"] / "roms" / device_file["relpath"],
                )
                restore_pairs.append((source, target))
            for source, target in restore_pairs:
                if not source.is_file():
                    raise LibraryError(f"Cannot restore because a trash file is missing: {source.name}")
                if target.exists():
                    raise LibraryError(f"Cannot restore because {target} already exists")
            moved: list[tuple[Path, Path]] = []
            try:
                for source, target in restore_pairs:
                    self._atomic_move(source, target)
                    moved.append((source, target))
            except Exception:
                for source, target in reversed(moved):
                    if target.exists() and not source.exists():
                        self._atomic_move(target, source)
                raise
            self.scan()
            with self.db.write() as connection:
                game = connection.execute(
                    "SELECT id FROM games WHERE primary_relpath=?", (manifest["primary_relpath"],)
                ).fetchone()
                if game:
                    for device_name in manifest.get("selected_devices", []):
                        device = connection.execute("SELECT id FROM devices WHERE name=?", (device_name,)).fetchone()
                        if device:
                            connection.execute(
                                "INSERT OR IGNORE INTO device_selections(device_id,game_id) VALUES(?,?)",
                                (device["id"], game["id"]),
                            )
                    for device_file in manifest.get("device_files", []):
                        device = connection.execute(
                            "SELECT id FROM devices WHERE path=?",
                            (device_file["device_path"],),
                        ).fetchone()
                        if device:
                            connection.execute(
                                "INSERT OR IGNORE INTO deployments(device_id,game_id,relpath) VALUES(?,?,?)",
                                (device["id"], game["id"], device_file["relpath"]),
                            )
                connection.execute("DELETE FROM trash_items WHERE id=?", (trash_id,))
            self.db.activity("restore", f"Restored {row['game_name']}")
            return {"restored": row["game_name"]}

    def purge_trash(self, trash_id: int) -> dict[str, object]:
        with self._operation_lock:
            with self.db.connect() as connection:
                row = connection.execute("SELECT * FROM trash_items WHERE id=?", (trash_id,)).fetchone()
            if not row:
                raise LibraryError("Trash item was not found")
            manifest = json.loads(row["manifest_json"])
            batch_root = _inside(self.settings.trash_root, self.settings.trash_root / row["trash_relpath"])
            removed = 0
            paths = [batch_root / relpath for relpath in manifest["files"]]
            paths.extend(batch_root / item["trash_relpath"] for item in manifest.get("device_files", []))
            for candidate in paths:
                path = _inside(batch_root, candidate)
                if path.is_file():
                    path.unlink()
                    removed += 1
            for directory in sorted(
                (p for p in batch_root.rglob("*") if p.is_dir()),
                key=lambda path: len(path.parts),
                reverse=True,
            ):
                try:
                    directory.rmdir()
                except OSError:
                    pass
            try:
                batch_root.rmdir()
            except OSError:
                pass
            with self.db.write() as connection:
                connection.execute("DELETE FROM trash_items WHERE id=?", (trash_id,))
            self.db.activity("purge", f"Permanently deleted {row['game_name']}")
            return {"purged": row["game_name"], "files": removed}

    def bulk_purge_trash(self, trash_ids: list[int]) -> dict[str, object]:
        unique_ids = list(dict.fromkeys(int(trash_id) for trash_id in trash_ids))
        if not unique_ids or len(unique_ids) > 1000 or any(trash_id <= 0 for trash_id in unique_ids):
            raise LibraryError("Choose between 1 and 1,000 valid trash items")
        with self._operation_lock:
            placeholders = ",".join("?" for _ in unique_ids)
            with self.db.connect() as connection:
                existing = connection.execute(
                    f"SELECT id FROM trash_items WHERE id IN ({placeholders})", unique_ids
                ).fetchall()
            if len(existing) != len(unique_ids):
                raise LibraryError("One or more selected trash items no longer exist")
            removed_files = 0
            for trash_id in unique_ids:
                removed_files += int(self.purge_trash(trash_id)["files"])
            self.db.activity(
                "bulk_purge", f"Permanently deleted {len(unique_ids)} trashed bundles"
            )
            return {"purged": len(unique_ids), "files": removed_files}

    @staticmethod
    def _roster_device_ids(connection, device_id: int) -> list[int]:
        device = connection.execute(
            "SELECT id,roster_group_id FROM devices WHERE id=?", (device_id,)
        ).fetchone()
        if not device:
            raise LibraryError("Device was not found")
        if not device["roster_group_id"]:
            return [device_id]
        return [
            row["id"]
            for row in connection.execute(
                "SELECT id FROM devices WHERE roster_group_id=? ORDER BY id",
                (device["roster_group_id"],),
            )
        ]

    def set_selection(self, device_id: int, game_id: int, selected: bool) -> list[int]:
        with self.db.write() as connection:
            device_ids = self._roster_device_ids(connection, device_id)
            if not connection.execute("SELECT 1 FROM games WHERE id=?", (game_id,)).fetchone():
                raise LibraryError("Game was not found")
            if selected:
                connection.executemany(
                    "INSERT OR IGNORE INTO device_selections(device_id,game_id) VALUES(?,?)",
                    ((member_id, game_id) for member_id in device_ids),
                )
            else:
                connection.executemany(
                    "DELETE FROM device_selections WHERE device_id=? AND game_id=?",
                    ((member_id, game_id) for member_id in device_ids),
                )
        return device_ids

    def device_inventory(self, device_id: int, *, refresh: bool = False) -> set[str]:
        """Return actual device ROM paths without treating them as managed copies."""
        cached = self._device_inventory_cache.get(device_id)
        if not refresh and cached and cached[0] > time.monotonic():
            return set(cached[1])
        with self.db.connect() as connection:
            device = connection.execute("SELECT path FROM devices WHERE id=?", (device_id,)).fetchone()
            persisted = {
                row["relpath"]
                for row in connection.execute(
                    "SELECT relpath FROM device_inventory_files WHERE device_id=?", (device_id,)
                )
            }
        if not device:
            raise LibraryError("Device was not found")
        # Read paths such as the Library page must never block on a recursive
        # filesystem walk. Explicit device inspection passes refresh=True and
        # publishes a new inventory for every other view to reuse.
        if not refresh:
            self._device_inventory_cache[device_id] = (
                time.monotonic() + DEVICE_INVENTORY_CACHE_SECONDS,
                frozenset(persisted),
            )
            return persisted
        device_root = _inside(
            self.settings.devices_root,
            self.settings.devices_root / device["path"] / "roms",
        )
        if not device_root.is_dir():
            return set()
        inventory: set[str] = set()
        try:
            paths = device_root.rglob("*")
            for path in paths:
                try:
                    if path.is_symlink() or not path.is_file():
                        continue
                    if path.name.startswith("._") or path.name == ".DS_Store":
                        continue
                    if path.name.endswith((COPY_SUFFIX, LEGACY_COPY_SUFFIX)):
                        continue
                    inventory.add(path.relative_to(device_root).as_posix())
                except OSError:
                    continue
        except OSError:
            pass
        with self.db.write() as connection:
            connection.execute("DELETE FROM device_inventory_files WHERE device_id=?", (device_id,))
            connection.executemany(
                "INSERT INTO device_inventory_files(device_id,relpath) VALUES(?,?)",
                ((device_id, relpath) for relpath in sorted(inventory)),
            )
        self._device_inventory_cache[device_id] = (
            time.monotonic() + DEVICE_INVENTORY_CACHE_SECONDS,
            frozenset(inventory),
        )
        return inventory

    def set_selections(self, device_id: int, game_ids: Iterable[int], selected: bool) -> int:
        ids = sorted(set(int(game_id) for game_id in game_ids))
        if not ids:
            return 0
        with self.db.write() as connection:
            device_ids = self._roster_device_ids(connection, device_id)
            placeholders = ",".join("?" for _ in ids)
            valid_ids = [
                row["id"] for row in connection.execute(
                    f"SELECT id FROM games WHERE id IN ({placeholders})", ids
                )
            ]
            if selected:
                connection.executemany(
                    "INSERT OR IGNORE INTO device_selections(device_id,game_id) VALUES(?,?)",
                    ((member_id, game_id) for member_id in device_ids for game_id in valid_ids),
                )
            elif valid_ids:
                valid_placeholders = ",".join("?" for _ in valid_ids)
                connection.executemany(
                    f"DELETE FROM device_selections WHERE device_id=? AND game_id IN ({valid_placeholders})",
                    ([member_id, *valid_ids] for member_id in device_ids),
                )
        return len(valid_ids)

    def clone_device_roster(
        self, source_device_id: int, target_device_id: int, keep_in_sync: bool = False
    ) -> dict[str, object]:
        with self.db.write() as connection:
            source = connection.execute(
                "SELECT id,name,roster_group_id,owner_user_id FROM devices WHERE id=?", (source_device_id,)
            ).fetchone()
            target = connection.execute(
                "SELECT id,name,roster_group_id,owner_user_id FROM devices WHERE id=?", (target_device_id,)
            ).fetchone()
            if not source or not target:
                raise LibraryError("Device was not found")
            if target["roster_group_id"] and not keep_in_sync:
                raise LibraryError(
                    "Unlink the target device before replacing its roster"
                )
            if keep_in_sync and source["owner_user_id"] != target["owner_user_id"]:
                raise LibraryError("Linked rosters must have the same owner")
            if keep_in_sync and source["owner_user_id"] is None:
                raise LibraryError("Assign an owner to both devices before creating a group")
            connection.execute("DELETE FROM device_selections WHERE device_id=?", (target_device_id,))
            connection.execute(
                "INSERT INTO device_selections(device_id,game_id) "
                "SELECT ?,game_id FROM device_selections WHERE device_id=?",
                (target_device_id, source_device_id),
            )
            if keep_in_sync:
                group_id = source["roster_group_id"]
                if group_id:
                    group_owner = connection.execute(
                        "SELECT owner_user_id FROM device_roster_groups WHERE id=?", (group_id,)
                    ).fetchone()
                    if not group_owner or group_owner["owner_user_id"] != source["owner_user_id"]:
                        raise LibraryError("The source device group has a different owner")
                if not group_id:
                    connection.execute(
                        "INSERT INTO device_roster_groups(name,owner_user_id) VALUES(?,?)",
                        (f"{source['name']} group", source["owner_user_id"]),
                    )
                    group_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                    connection.execute(
                        "UPDATE devices SET roster_group_id=? WHERE id=?", (group_id, source_device_id)
                    )
                connection.execute(
                    "UPDATE devices SET roster_group_id=? WHERE id=?", (group_id, target_device_id)
                )
            count = connection.execute(
                "SELECT COUNT(*) AS count FROM device_selections WHERE device_id=?", (target_device_id,)
            ).fetchone()["count"]
        self.db.activity(
            "device",
            f"Cloned {count} selections from {source['name']} to {target['name']}"
            + (" and linked their rosters" if keep_in_sync else ""),
        )
        return {"games": count, "linked": keep_in_sync}

    def create_device_group(
        self, name: str, source_device_id: int, member_device_ids: Iterable[int]
    ) -> dict[str, object]:
        group_name = name.strip()
        if not group_name:
            raise LibraryError("Enter a group name")
        device_ids = sorted({source_device_id, *(int(value) for value in member_device_ids)})
        if len(device_ids) < 2:
            raise LibraryError("Choose at least two devices")
        placeholders = ",".join("?" for _ in device_ids)
        with self.db.write() as connection:
            devices = connection.execute(
                f"SELECT id,name,owner_user_id,roster_group_id FROM devices "
                f"WHERE id IN ({placeholders}) ORDER BY name COLLATE NOCASE",
                device_ids,
            ).fetchall()
            if len(devices) != len(device_ids):
                raise LibraryError("One or more devices were not found")
            source = next((item for item in devices if item["id"] == source_device_id), None)
            if not source:
                raise LibraryError("Source device was not found")
            if source["owner_user_id"] is None:
                raise LibraryError("Assign an owner to the devices before creating a group")
            if any(item["owner_user_id"] != source["owner_user_id"] for item in devices):
                raise LibraryError("Every device in a group must have the same owner")
            grouped = [item["name"] for item in devices if item["roster_group_id"]]
            if grouped:
                raise LibraryError(
                    "Remove these devices from their current group first: " + ", ".join(grouped)
                )
            connection.execute(
                "INSERT INTO device_roster_groups(name,owner_user_id) VALUES(?,?)",
                (group_name, source["owner_user_id"]),
            )
            group_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            for device_id in device_ids:
                if device_id != source_device_id:
                    connection.execute("DELETE FROM device_selections WHERE device_id=?", (device_id,))
                    connection.execute(
                        "INSERT INTO device_selections(device_id,game_id) "
                        "SELECT ?,game_id FROM device_selections WHERE device_id=?",
                        (device_id, source_device_id),
                    )
                connection.execute(
                    "UPDATE devices SET roster_group_id=? WHERE id=?", (group_id, device_id)
                )
            games = connection.execute(
                "SELECT COUNT(*) AS count FROM device_selections WHERE device_id=?",
                (source_device_id,),
            ).fetchone()["count"]
        self.db.activity(
            "device", f"Created device group {group_name} with {len(device_ids)} devices"
        )
        return {
            "id": group_id,
            "name": group_name,
            "owner_user_id": source["owner_user_id"],
            "member_ids": device_ids,
            "devices": len(device_ids),
            "games": games,
        }

    def delete_device_group(self, group_id: int) -> dict[str, object]:
        with self.db.write() as connection:
            group = connection.execute(
                "SELECT id,name FROM device_roster_groups WHERE id=?", (group_id,)
            ).fetchone()
            if not group:
                raise LibraryError("Device group was not found")
            members = connection.execute(
                "SELECT COUNT(*) AS count FROM devices WHERE roster_group_id=?", (group_id,)
            ).fetchone()["count"]
            connection.execute(
                "UPDATE devices SET roster_group_id=NULL WHERE roster_group_id=?", (group_id,)
            )
            connection.execute("DELETE FROM device_roster_groups WHERE id=?", (group_id,))
        self.db.activity("device", f"Deleted device group {group['name']}")
        return {"deleted": True, "devices": members}

    def link_device_rosters(
        self, source_device_id: int, target_device_ids: Iterable[int]
    ) -> dict[str, object]:
        target_ids = sorted({int(value) for value in target_device_ids if int(value) != source_device_id})
        if not target_ids:
            raise LibraryError("Choose at least one other device")
        with self.db.write() as connection:
            source = connection.execute(
                "SELECT id,name,roster_group_id,owner_user_id FROM devices WHERE id=?", (source_device_id,)
            ).fetchone()
            placeholders = ",".join("?" for _ in target_ids)
            targets = connection.execute(
                f"SELECT id,name,roster_group_id,owner_user_id FROM devices WHERE id IN ({placeholders})",
                target_ids,
            ).fetchall()
            if not source or len(targets) != len(target_ids):
                raise LibraryError("One or more devices were not found")
            different_owner = [
                row["name"] for row in targets if row["owner_user_id"] != source["owner_user_id"]
            ]
            if different_owner:
                raise LibraryError(
                    "Linked rosters must have the same owner: " + ", ".join(different_owner)
                )
            if source["owner_user_id"] is None:
                raise LibraryError("Assign an owner to the devices before creating a group")
            conflicting = [row["name"] for row in targets if row["roster_group_id"] and row["roster_group_id"] != source["roster_group_id"]]
            if conflicting:
                raise LibraryError(
                    "Unlink these devices from their current roster first: " + ", ".join(conflicting)
                )
            group_id = source["roster_group_id"]
            if group_id:
                group_owner = connection.execute(
                    "SELECT owner_user_id FROM device_roster_groups WHERE id=?", (group_id,)
                ).fetchone()
                if not group_owner or group_owner["owner_user_id"] != source["owner_user_id"]:
                    raise LibraryError("The source device group has a different owner")
            if not group_id:
                connection.execute(
                    "INSERT INTO device_roster_groups(name,owner_user_id) VALUES(?,?)",
                    (f"{source['name']} group", source["owner_user_id"]),
                )
                group_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                connection.execute(
                    "UPDATE devices SET roster_group_id=? WHERE id=?", (group_id, source_device_id)
                )
            for target_id in target_ids:
                connection.execute("DELETE FROM device_selections WHERE device_id=?", (target_id,))
                connection.execute(
                    "INSERT INTO device_selections(device_id,game_id) "
                    "SELECT ?,game_id FROM device_selections WHERE device_id=?",
                    (target_id, source_device_id),
                )
                connection.execute(
                    "UPDATE devices SET roster_group_id=? WHERE id=?", (group_id, target_id)
                )
            games = connection.execute(
                "SELECT COUNT(*) AS count FROM device_selections WHERE device_id=?", (source_device_id,)
            ).fetchone()["count"]
            member_count = connection.execute(
                "SELECT COUNT(*) AS count FROM devices WHERE roster_group_id=?", (group_id,)
            ).fetchone()["count"]
        self.db.activity(
            "device", f"Linked {source['name']} with {len(target_ids)} device rosters"
        )
        return {"group_id": group_id, "devices": member_count, "games": games}

    def unlink_device_roster(self, device_id: int) -> dict[str, object]:
        with self.db.write() as connection:
            device = connection.execute(
                "SELECT id,name,roster_group_id FROM devices WHERE id=?", (device_id,)
            ).fetchone()
            if not device:
                raise LibraryError("Device was not found")
            group_id = device["roster_group_id"]
            if not group_id:
                return {"unlinked": False}
            connection.execute("UPDATE devices SET roster_group_id=NULL WHERE id=?", (device_id,))
            remaining = [
                row["id"] for row in connection.execute(
                    "SELECT id FROM devices WHERE roster_group_id=?", (group_id,)
                )
            ]
            if len(remaining) < 2:
                connection.execute(
                    "UPDATE devices SET roster_group_id=NULL WHERE roster_group_id=?", (group_id,)
                )
                connection.execute("DELETE FROM device_roster_groups WHERE id=?", (group_id,))
        self.db.activity("device", f"Unlinked {device['name']} from its shared roster")
        return {"unlinked": True}

    def set_device_deployment_mode(self, device_id: int, mode: str) -> None:
        if mode != "hardlink":
            raise LibraryError("ROMmates always deploys device ROMs as hardlinks where supported")
        with self.db.write() as connection:
            updated = connection.execute(
                "UPDATE devices SET deployment_mode=? WHERE id=?", (mode, device_id)
            ).rowcount
        if not updated:
            raise LibraryError("Device was not found")

    def _record_deployment(self, device_id: int, game_id: int, relpath: str) -> None:
        with self.db.write() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO deployments(device_id,game_id,relpath) VALUES(?,?,?)",
                (device_id, game_id, relpath),
            )

    def device_storage_summary(self, device_id: int) -> dict[str, int]:
        """Inspect deployed files and report their current on-disk storage relationship."""
        with self.db.connect() as connection:
            device = connection.execute(
                "SELECT path FROM devices WHERE id=?", (device_id,)
            ).fetchone()
            if not device:
                raise LibraryError("Device was not found")
            rows = connection.execute(
                "SELECT dp.game_id,dp.relpath,gf.relpath AS source_relpath,"
                "EXISTS(SELECT 1 FROM device_selections ds WHERE ds.device_id=dp.device_id "
                "AND ds.game_id=dp.game_id) AS selected "
                "FROM deployments dp LEFT JOIN game_files gf ON gf.game_id=dp.game_id "
                "AND gf.device_relpath=dp.relpath WHERE dp.device_id=?",
                (device_id,),
            ).fetchall()
        source_root = self.settings.library_root.resolve()
        device_root = _inside(
            self.settings.devices_root,
            self.settings.devices_root / device["path"] / "roms",
        )
        summary = {"hardlinked": 0, "copied": 0, "missing": 0, "unknown": 0, "conversions": 0}
        for row in rows:
            if not row["source_relpath"]:
                summary["unknown"] += 1
                continue
            source = _inside(source_root, source_root / row["source_relpath"])
            target = _inside(device_root, device_root / row["relpath"])
            try:
                if not target.is_file() or not source.is_file():
                    summary["missing"] += 1
                elif os.path.samestat(source.stat(), target.stat()):
                    summary["hardlinked"] += 1
                else:
                    summary["copied"] += 1
                    if row["selected"]:
                        summary["conversions"] += 1
            except OSError:
                summary["unknown"] += 1
        return summary

    def _forget_deployment(self, device_id: int, game_id: int, relpath: str) -> None:
        with self.db.write() as connection:
            connection.execute(
                "DELETE FROM deployments WHERE device_id=? AND game_id=? AND relpath=?",
                (device_id, game_id, relpath),
            )

    def apply_device(
        self, device_id: int, cancel_check: CancelCheck | None = None
    ) -> dict[str, int]:
        with self._operation_lock:
            with self.db.connect() as connection:
                device = connection.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
                if not device:
                    raise LibraryError("Device was not found")
                selected = connection.execute(
                    "SELECT g.id AS game_id,gf.relpath AS source_relpath,gf.device_relpath AS relpath,gf.sha256 FROM device_selections ds "
                    "JOIN games g ON g.id=ds.game_id JOIN game_files gf ON gf.game_id=g.id "
                    "WHERE ds.device_id=? ORDER BY gf.relpath",
                    (device_id,),
                ).fetchall()
                deployed = connection.execute(
                    "SELECT game_id,relpath FROM deployments WHERE device_id=?", (device_id,)
                ).fetchall()
            device_root = _inside(
                self.settings.devices_root,
                self.settings.devices_root / device["path"] / "roms",
            )
            device_root.mkdir(parents=True, exist_ok=True)
            desired = {
                (row["game_id"], row["relpath"]): (row["source_relpath"], row["sha256"])
                for row in selected
            }
            existing = {(row["game_id"], row["relpath"]) for row in deployed}
            # Device storage is derived from the filesystem, not a user preference.
            # Always attempt a hardlink first and retain the existing safe copy
            # fallback for cross-filesystem or unsupported targets.
            mode = "hardlink"
            copied = linked = converted = link_fallbacks = skipped = removed = metadata_removed = 0
            source_root = self.settings.library_root.resolve()
            deploy_plan: list[tuple[int, str, Path, Path, bool]] = []
            required_bytes = 0
            target_owners: dict[str, tuple[int, str]] = {}
            for (game_id, relpath), (source_relpath, _) in sorted(desired.items()):
                if cancel_check:
                    cancel_check()
                owner = target_owners.get(relpath)
                if owner and owner != (game_id, source_relpath):
                    raise LibraryError(
                        f"Two selected ROM files resolve to the same ES-DE path: {relpath}"
                    )
                target_owners[relpath] = (game_id, source_relpath)
                source = _inside(source_root, source_root / source_relpath)
                target = _inside(device_root, device_root / relpath)
                target.parent.mkdir(parents=True, exist_ok=True)
                source_stat = source.stat()
                if target.exists():
                    if not target.is_file():
                        raise LibraryError(f"Device target is not a regular file: {relpath}")
                    target_stat = target.stat()
                    same_inode = os.path.samestat(source_stat, target_stat)
                    if same_inode:
                        skipped += 1
                        self._record_deployment(device_id, game_id, relpath)
                        continue
                    content_matches = (
                        target_stat.st_size == source_stat.st_size
                        and target_stat.st_mtime_ns == source_stat.st_mtime_ns
                    )
                    if content_matches and mode == "copy":
                        skipped += 1
                        self._record_deployment(device_id, game_id, relpath)
                        continue
                    deploy_plan.append((game_id, relpath, source, target, content_matches))
                    if mode == "copy":
                        required_bytes += source_stat.st_size
                    continue
                deploy_plan.append((game_id, relpath, source, target, False))
                if mode == "copy":
                    required_bytes += source_stat.st_size
            free_bytes = shutil.disk_usage(device_root).free
            if mode == "copy" and required_bytes > free_bytes:
                raise LibraryError(
                    f"Device needs {required_bytes} bytes but only {free_bytes} bytes are available"
                )
            # Record every deployment as it lands. Batching this until the end would leave
            # files on the device that no deployment row claims, making them permanently
            # unmanaged if the job fails or the container stops partway through.
            for game_id, relpath, source, target, existing_matches in deploy_plan:
                if cancel_check:
                    cancel_check()
                linked_file = False
                if mode == "hardlink":
                    link_temp = target.with_name(f".{target.name}{LINK_SUFFIX}")
                    link_temp.unlink(missing_ok=True)
                    try:
                        os.link(source, link_temp, follow_symlinks=False)
                        os.replace(link_temp, target)
                        linked_file = True
                    except OSError as exc:
                        link_temp.unlink(missing_ok=True)
                        if exc.errno not in {
                            errno.EXDEV, errno.EPERM, errno.EACCES,
                            getattr(errno, "EOPNOTSUPP", errno.ENOTSUP), errno.ENOSYS,
                        }:
                            raise
                        link_fallbacks += 1
                if linked_file:
                    self._record_deployment(device_id, game_id, relpath)
                    if existing_matches:
                        converted += 1
                    else:
                        linked += 1
                    continue
                if existing_matches:
                    # The requested hardlink was unavailable, but the existing copy is
                    # already current. Keep it intact instead of rewriting it pointlessly.
                    self._record_deployment(device_id, game_id, relpath)
                    skipped += 1
                    continue
                source_size = source.stat().st_size
                if source_size > shutil.disk_usage(device_root).free:
                    raise LibraryError(
                        f"Device needs {source_size} bytes but only "
                        f"{shutil.disk_usage(device_root).free} bytes are available"
                    )
                temp = target.with_name(f".{target.name}{COPY_SUFFIX}")
                try:
                    if cancel_check:
                        with source.open("rb") as source_handle, temp.open("wb") as target_handle:
                            for chunk in iter(lambda: source_handle.read(1024 * 1024), b""):
                                cancel_check()
                                target_handle.write(chunk)
                        shutil.copystat(source, temp)
                    else:
                        shutil.copy2(source, temp)
                    os.replace(temp, target)
                except Exception:
                    temp.unlink(missing_ok=True)
                    raise
                self._record_deployment(device_id, game_id, relpath)
                copied += 1
            for game_id, relpath in sorted(existing - set(desired)):
                if cancel_check:
                    cancel_check()
                target = _inside(device_root, device_root / relpath)
                if target.is_file():
                    target.unlink()
                    removed += 1
                self._forget_deployment(device_id, game_id, relpath)
            for pattern in (
                "._*", ".DS_Store", f"*{COPY_SUFFIX}", f"*{LEGACY_COPY_SUFFIX}", f"*{LINK_SUFFIX}"
            ):
                for metadata in device_root.rglob(pattern):
                    if cancel_check:
                        cancel_check()
                    if metadata.is_file():
                        metadata.unlink()
                        metadata_removed += 1
            detail = (
                f"Applied {device['name']}: {linked} linked, {converted} converted, "
                f"{copied} copied, {removed} removed"
            )
            with self.db.write() as connection:
                connection.executemany(
                    "INSERT OR REPLACE INTO device_inventory_files(device_id,relpath,observed_at) "
                    "VALUES(?,?,CURRENT_TIMESTAMP)",
                    ((device_id, relpath) for _, relpath in desired),
                )
                removed_relpaths = [relpath for _, relpath in existing - set(desired)]
                connection.executemany(
                    "DELETE FROM device_inventory_files WHERE device_id=? AND relpath=?",
                    ((device_id, relpath) for relpath in removed_relpaths),
                )
            self._device_inventory_cache.pop(device_id, None)
            self.db.activity("device_apply", detail)
            return {
                "copied": copied,
                "linked": linked,
                "converted": converted,
                "link_fallbacks": link_fallbacks,
                "unchanged": skipped,
                "removed": removed,
                "metadata_removed": metadata_removed,
            }
