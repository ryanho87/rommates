from __future__ import annotations

import hashlib
import errno
import json
import os
import re
import shlex
import shutil
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .config import Settings
from .db import Database


COPY_SUFFIX = ".rommates-copy"
LEGACY_COPY_SUFFIX = ".rommanager-copy"
DEVICE_INVENTORY_CACHE_SECONDS = 5.0

CUE_FILE_RE = re.compile(r'^\s*FILE\s+"([^"]+)"', re.IGNORECASE)
GDI_FILE_RE = re.compile(r'^(\s*\d+\s+\d+\s+\d+\s+\d+\s+)(?:"([^"]+)"|(\S+))(\s+\d+.*)$')
DESCRIPTOR_EXTENSIONS = frozenset({".cue", ".gdi", ".m3u"})
TAG_RE = re.compile(r"\s*[\(\[].*?[\)\]]")
NON_WORD_RE = re.compile(r"[^a-z0-9]+")
PACK_NUMBER_RE = re.compile(r"^\s*(?:\[\d{1,4}\]|\d{1,4}[.)])\s*")
HASH_CACHE_BATCH_FILES = 16
HASH_CACHE_BATCH_BYTES = 128 * 1024 * 1024

ProgressCallback = Callable[[int, str], None]
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


@dataclass
class ScanProgress:
    total_files: int
    total_bytes: int
    callback: ProgressCallback | None
    processed_files: int = 0
    processed_bytes: int = 0
    cached_files: int = 0

    def _emit(self) -> None:
        if not self.callback:
            return
        if self.total_bytes:
            fraction = self.processed_bytes / self.total_bytes
        elif self.total_files:
            fraction = self.processed_files / self.total_files
        else:
            fraction = 1
        percent = min(90, 1 + int(max(0, min(fraction, 1)) * 89))
        detail = f"Hashing {self.processed_files:,} of {self.total_files:,} files"
        if self.total_bytes:
            detail += f" · {_format_bytes(self.processed_bytes)} of {_format_bytes(self.total_bytes)}"
        if self.cached_files:
            detail += f" · {self.cached_files:,} cached"
        self.callback(percent, detail)

    def advance(self, byte_count: int) -> None:
        self.processed_bytes += byte_count
        self._emit()

    def finish_file(self, cached: bool = False) -> None:
        self.processed_files += 1
        if cached:
            self.cached_files += 1
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
    ) -> tuple[str, os.stat_result]:
        if cancel_check:
            cancel_check()
        stat = path.stat()
        cached = cache.get(relpath)
        if cached and cached[0] == stat.st_size and cached[1] == stat.st_mtime_ns:
            if progress:
                progress.advance(stat.st_size)
                progress.finish_file(cached=True)
            return cached[2], stat

        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
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
                    refs.append(descriptor.parent / match.group(1).replace("\\", "/"))
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
    ) -> GameCandidate:
        root = self.settings.library_root.resolve()
        paths = paths if paths is not None else self._bundle_paths(primary)
        records: list[FileRecord] = []
        for path in paths:
            relpath = _rel(root, path)
            sha256, stat = self._hash_file(path, relpath, cache, cache_updates, progress, cancel_check)
            kind = "descriptor" if path.suffix.lower() in DESCRIPTOR_EXTENSIONS else "content"
            records.append(FileRecord(relpath, stat.st_size, sha256, kind, stat.st_mtime_ns))
        if not records:
            raise LibraryError(f"No readable files found for {primary}")
        aggregate = hashlib.sha256()
        hash_records = [record for record in records if record.kind == "content"] or records
        for record in sorted(hash_records, key=lambda item: (item.sha256, item.size)):
            aggregate.update(record.sha256.encode("ascii"))
            aggregate.update(str(record.size).encode("ascii"))
        folder_extension = ""
        display_name = primary.stem
        if primary.is_dir():
            expected_suffix = f".{platform.casefold()}"
            folder_extension = primary.suffix.lower() if primary.suffix.lower() == expected_suffix else ""
            display_name = primary.stem if folder_extension else primary.name
        return GameCandidate(
            platform=platform,
            primary_relpath=_rel(root, primary),
            display_name=display_name,
            extension=folder_extension if primary.is_dir() else primary.suffix.lower(),
            size=sum(record.size for record in records),
            bundle_hash=aggregate.hexdigest(),
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
        for platform_dir in sorted((item for item in root.iterdir() if item.is_dir()), key=lambda p: p.name.lower()):
            if cancel_check:
                cancel_check()
            platform_paths: list[Path] = []
            for path in platform_dir.rglob("*"):
                if cancel_check:
                    cancel_check()
                platform_paths.append(path)
            claimed: set[Path] = set()
            bundle_paths: dict[Path, tuple[Path, ...]] = {}
            primaries: list[Path] = []

            if platform_dir.name.casefold() in self.settings.folder_bundle_platforms:
                for folder in sorted(
                    (path for path in platform_dir.iterdir() if path.is_dir()),
                    key=lambda path: path.name.casefold(),
                ):
                    files: list[Path] = []
                    for path in sorted(folder.rglob("*"), key=lambda item: item.as_posix().casefold()):
                        if cancel_check:
                            cancel_check()
                        if path.name.startswith("._") or path.name == ".DS_Store":
                            continue
                        if path.is_symlink():
                            record_issue(path, "symbolic links are not indexed")
                            continue
                        if path.is_file():
                            files.append(path)
                    if files:
                        primaries.append(folder)
                        bundle_paths[folder] = tuple(files)
                        claimed.update(path.resolve() for path in files)

            descriptors = sorted(
                (
                    path for path in platform_paths
                    if path.is_file()
                    and path.resolve() not in claimed
                    and path.suffix.lower() in DESCRIPTOR_EXTENSIONS
                    and not path.name.startswith("._")
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
                referenced.update(path for path in bundle if path != resolved)

            for path in sorted(platform_paths, key=lambda p: p.as_posix().lower()):
                if cancel_check:
                    cancel_check()
                if not path.is_file() or path.name.startswith("._") or path.name == ".DS_Store":
                    continue
                if path.suffix.lower() not in self.settings.extensions:
                    continue
                if path.is_symlink():
                    record_issue(path, "symbolic links are not indexed")
                    continue
                if path.resolve() in claimed or path.resolve() in referenced or path in primaries:
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
        for _, _, paths in work:
            for path in paths:
                if cancel_check:
                    cancel_check()
                try:
                    total_bytes += path.stat().st_size
                    total_files += 1
                except OSError:
                    # Candidate construction records the useful per-file error.
                    pass
        progress = ScanProgress(total_files, total_bytes, progress_callback)
        progress._emit()
        try:
            for primary, platform, paths in work:
                if cancel_check:
                    cancel_check()
                try:
                    candidates.append(
                        self._candidate(primary, platform, cache, cache_updates, paths, progress, cancel_check)
                    )
                except (OSError, LibraryError) as exc:
                    record_issue(primary, exc)
        finally:
            # Normal Python exceptions still preserve everything hashed so far. A hard
            # container stop can lose at most the current bounded batch.
            self._persist_hash_cache(cache_updates)
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
            # Check before writing anything: the guard aborts the whole scan.
            self._guard_prune(known_relpaths, seen, skipped, force_prune)
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
                        "INSERT INTO game_files(game_id,relpath,size,sha256,kind) VALUES(?,?,?,?,?)",
                        ((game_id, item.relpath, item.size, item.sha256, item.kind) for item in candidate.files),
                    )
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
                connection.execute("DELETE FROM file_cache WHERE relpath NOT IN (SELECT relpath FROM game_files)")
            if progress_callback:
                progress_callback(99, "Finalizing scan")
            detail = f"Indexed {len(candidates)} games"
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
            }

    def _guard_prune(
        self,
        known_relpaths: set[str],
        seen: set[str],
        skipped: list[str],
        force_prune: bool,
    ) -> None:
        """Refuse to reconcile when a scan would delete an implausible share of the catalog.

        Deleting a ``games`` row cascades into ``device_selections`` and ``deployments``,
        so a library root that is present but empty or partially readable (an unmounted
        volume, a mount race at boot, a disconnected network share) would otherwise wipe
        every device selection without warning.
        """
        if force_prune or not known_relpaths:
            return
        missing = known_relpaths - seen
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
            if exc.errno == errno.EXDEV:
                raise LibraryError(
                    "ROM library, device folders, and trash must be on the same mounted filesystem"
                ) from exc
            raise

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
                        "UPDATE game_files SET relpath=? WHERE game_id=? AND relpath=?",
                        (_rel(root, target), game_id, _rel(root, old)),
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
                    "SELECT d.path, dp.relpath FROM deployments dp JOIN devices d ON d.id=dp.device_id WHERE dp.game_id=?",
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
                    f"SELECT game_id,relpath FROM game_files WHERE game_id IN ({placeholders})",
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

    def set_selection(self, device_id: int, game_id: int, selected: bool) -> None:
        with self.db.write() as connection:
            if not connection.execute("SELECT 1 FROM devices WHERE id=?", (device_id,)).fetchone():
                raise LibraryError("Device was not found")
            if not connection.execute("SELECT 1 FROM games WHERE id=?", (game_id,)).fetchone():
                raise LibraryError("Game was not found")
            if selected:
                connection.execute(
                    "INSERT OR IGNORE INTO device_selections(device_id,game_id) VALUES(?,?)", (device_id, game_id)
                )
            else:
                connection.execute(
                    "DELETE FROM device_selections WHERE device_id=? AND game_id=?", (device_id, game_id)
                )

    def device_inventory(self, device_id: int, *, refresh: bool = False) -> set[str]:
        """Return actual device ROM paths without treating them as managed copies."""
        cached = self._device_inventory_cache.get(device_id)
        if not refresh and cached and cached[0] > time.monotonic():
            return set(cached[1])
        with self.db.connect() as connection:
            device = connection.execute("SELECT path FROM devices WHERE id=?", (device_id,)).fetchone()
        if not device:
            raise LibraryError("Device was not found")
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
            if not connection.execute("SELECT 1 FROM devices WHERE id=?", (device_id,)).fetchone():
                raise LibraryError("Device was not found")
            placeholders = ",".join("?" for _ in ids)
            valid_ids = [
                row["id"] for row in connection.execute(
                    f"SELECT id FROM games WHERE id IN ({placeholders})", ids
                )
            ]
            if selected:
                connection.executemany(
                    "INSERT OR IGNORE INTO device_selections(device_id,game_id) VALUES(?,?)",
                    ((device_id, game_id) for game_id in valid_ids),
                )
            elif valid_ids:
                valid_placeholders = ",".join("?" for _ in valid_ids)
                connection.execute(
                    f"DELETE FROM device_selections WHERE device_id=? AND game_id IN ({valid_placeholders})",
                    [device_id, *valid_ids],
                )
        return len(valid_ids)

    def _record_deployment(self, device_id: int, game_id: int, relpath: str) -> None:
        with self.db.write() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO deployments(device_id,game_id,relpath) VALUES(?,?,?)",
                (device_id, game_id, relpath),
            )

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
                    "SELECT g.id AS game_id,gf.relpath FROM device_selections ds "
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
            desired = {(row["game_id"], row["relpath"]) for row in selected}
            existing = {(row["game_id"], row["relpath"]) for row in deployed}
            copied = skipped = removed = metadata_removed = 0
            source_root = self.settings.library_root.resolve()
            copy_plan: list[tuple[int, str, Path, Path]] = []
            required_bytes = 0
            for game_id, relpath in sorted(desired):
                if cancel_check:
                    cancel_check()
                source = _inside(source_root, source_root / relpath)
                target = _inside(device_root, device_root / relpath)
                target.parent.mkdir(parents=True, exist_ok=True)
                source_stat = source.stat()
                if target.exists():
                    target_stat = target.stat()
                    if target_stat.st_size == source_stat.st_size and target_stat.st_mtime_ns == source_stat.st_mtime_ns:
                        skipped += 1
                        continue
                copy_plan.append((game_id, relpath, source, target))
                required_bytes += source_stat.st_size
            free_bytes = shutil.disk_usage(device_root).free
            if required_bytes > free_bytes:
                raise LibraryError(
                    f"Device needs {required_bytes} bytes but only {free_bytes} bytes are available"
                )
            # Files already present and identical are still part of the managed set.
            for game_id, relpath in sorted(desired - {(g, r) for g, r, _, _ in copy_plan}):
                if cancel_check:
                    cancel_check()
                self._record_deployment(device_id, game_id, relpath)
            # Record every copy as it lands. Batching this until the end would leave
            # files on the device that no deployment row claims, making them permanently
            # unmanaged if the job fails or the container stops partway through.
            for game_id, relpath, source, target in copy_plan:
                if cancel_check:
                    cancel_check()
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
            for game_id, relpath in sorted(existing - desired):
                if cancel_check:
                    cancel_check()
                target = _inside(device_root, device_root / relpath)
                if target.is_file():
                    target.unlink()
                    removed += 1
                self._forget_deployment(device_id, game_id, relpath)
            for pattern in ("._*", ".DS_Store", f"*{COPY_SUFFIX}", f"*{LEGACY_COPY_SUFFIX}"):
                for metadata in device_root.rglob(pattern):
                    if cancel_check:
                        cancel_check()
                    if metadata.is_file():
                        metadata.unlink()
                        metadata_removed += 1
            detail = f"Applied {device['name']}: {copied} copied, {removed} removed"
            self._device_inventory_cache.pop(device_id, None)
            self.db.activity("device_apply", detail)
            return {
                "copied": copied,
                "unchanged": skipped,
                "removed": removed,
                "metadata_removed": metadata_removed,
            }
