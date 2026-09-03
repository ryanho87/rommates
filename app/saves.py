from __future__ import annotations

import hashlib
import os
import re
import shutil
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .config import Settings
from .db import Database
from .library import JobCancelled, LibraryError, normalize_name


ProgressCallback = Callable[[int, str], None]
CancelCheck = Callable[[], None]

SAVE_EXTENSIONS = frozenset({
    ".srm", ".sav", ".dsv", ".rtc", ".eep", ".fla", ".sra", ".mpk", ".nv", ".fs",
    ".gci", ".raw",
})
STATE_NAME = re.compile(r"^(?P<name>.+)\.state(?:\d+|\.auto)?(?:\.png)?$", re.IGNORECASE)
SYNC_CONFLICT_NAME = re.compile(
    r"^(?P<base>.+)\.sync-conflict-(?P<date>\d{8})-(?P<time>\d{6})-"
    r"(?P<device>[^.]+)(?P<extension>\.[^/]*)?$",
    re.IGNORECASE,
)
CORE_PLATFORMS = {
    "mgba": frozenset({"gba", "gb", "gbc"}),
    "gpsp": frozenset({"gba"}),
    "gambatte": frozenset({"gb", "gbc"}),
    "sameboy": frozenset({"gb", "gbc"}),
    "snes9x": frozenset({"snes"}),
    "bsnes": frozenset({"snes"}),
    "mesen": frozenset({"nes", "snes"}),
    "quicknes": frozenset({"nes"}),
    "nestopia": frozenset({"nes"}),
    "genesisplusgx": frozenset({"megadrive", "genesis", "mastersystem", "sms", "gamegear", "gg", "segacd"}),
    "picodrive": frozenset({"megadrive", "genesis", "mastersystem", "sms", "gamegear", "gg", "segacd", "32x"}),
    "mupen64plusnext": frozenset({"n64"}),
    "paralleln64": frozenset({"n64"}),
    "desmume": frozenset({"nds"}),
    "melonds": frozenset({"nds"}),
    "beetlepsxhw": frozenset({"psx", "ps1"}),
    "pcsxrearmed": frozenset({"psx", "ps1"}),
    "ppsspp": frozenset({"psp"}),
    "flycast": frozenset({"dreamcast"}),
}

# Standalone emulators whose save filenames normally retain the ROM stem. Other
# emulators (notably Dolphin and Ryujinx) use game/title identifiers and are
# inventoried and snapshotted, but deliberately excluded from filename matching.
EMULATOR_PLATFORMS = {
    "melonds": frozenset({"nds"}),
    "mgba": frozenset({"gba", "gb", "gbc"}),
    "duckstation": frozenset({"psx", "ps1"}),
    "pcsx2": frozenset({"ps2"}),
    "ppsspp": frozenset({"psp"}),
    "rmg": frozenset({"n64"}),
    "flycast": frozenset({"dreamcast"}),
}

IGNORED_SAVE_NAMES = frozenset({".ds_store", ".stfolder", ".stfolder (1)", ".stignore"})


@dataclass(frozen=True)
class SaveFile:
    relpath: str
    size: int
    mtime_ns: int
    sha256: str


class SaveSnapshotService:
    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self._operation_lock = threading.RLock()

    @property
    def blob_root(self) -> Path:
        return self.settings.snapshots_root / "blobs" / "sha256"

    @property
    def staging_root(self) -> Path:
        return self.settings.snapshots_root / "staging"

    def initialize(self) -> None:
        self.settings.snapshots_root.mkdir(parents=True, exist_ok=True)
        self.blob_root.mkdir(parents=True, exist_ok=True)
        self.staging_root.mkdir(parents=True, exist_ok=True)
        saves = self.settings.saves_root.resolve()
        snapshots = self.settings.snapshots_root.resolve()
        if saves == snapshots or saves in snapshots.parents or snapshots in saves.parents:
            raise LibraryError("Save source and snapshot storage must be separate directories")
        with self.db.write() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO save_settings("
                "id,enabled,interval_minutes,retention_recent,retention_daily,retention_weekly,retention_monthly"
                ") VALUES(1,1,?,?,?,?,?)",
                (
                    self.settings.save_snapshot_interval_minutes,
                    self.settings.save_retention_recent,
                    self.settings.save_retention_daily,
                    self.settings.save_retention_weekly,
                    self.settings.save_retention_monthly,
                ),
            )

    def available(self) -> bool:
        return self.settings.saves_root.is_dir()

    def settings_payload(self) -> dict[str, object]:
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM save_settings WHERE id=1").fetchone()
        payload = dict(row) if row else {}
        payload.update(
            {
                "source_root": str(self.settings.saves_root),
                "snapshots_root": str(self.settings.snapshots_root),
                "available": self.available(),
            }
        )
        return payload

    def update_settings(self, values: dict[str, object]) -> dict[str, object]:
        allowed = {
            "enabled": (0, 1),
            "interval_minutes": (0, 10080),
            "retention_recent": (1, 1000),
            "retention_daily": (0, 3650),
            "retention_weekly": (0, 520),
            "retention_monthly": (0, 240),
        }
        normalized: dict[str, int] = {}
        for key, value in values.items():
            if key not in allowed:
                continue
            number = int(bool(value)) if key == "enabled" else int(value)
            minimum, maximum = allowed[key]
            if number < minimum or number > maximum:
                raise LibraryError(f"{key.replace('_', ' ').title()} is outside the allowed range")
            normalized[key] = number
        if normalized:
            assignments = ",".join(f"{key}=?" for key in normalized)
            with self.db.write() as connection:
                connection.execute(
                    f"UPDATE save_settings SET {assignments},updated_at=CURRENT_TIMESTAMP WHERE id=1",
                    [*normalized.values()],
                )
        return self.settings_payload()

    def _source_paths(self) -> list[Path]:
        root = self.settings.saves_root
        if not root.is_dir():
            raise LibraryError(
                f"Save vault is unavailable at {root}. Mount the shared Emulation directory there."
            )
        paths: list[Path] = []
        try:
            for path in root.rglob("*"):
                if path.is_symlink():
                    continue
                relative = path.relative_to(root)
                if self._ignored_relative(relative):
                    continue
                if path.is_file():
                    paths.append(path)
        except OSError as exc:
            raise LibraryError(f"Could not read the save vault: {exc}") from exc
        return sorted(paths, key=lambda item: item.relative_to(root).as_posix())

    @staticmethod
    def _ignored_relative(relative: Path) -> bool:
        """Exclude Syncthing markers, version stores, and Finder metadata."""
        for part in relative.parts:
            folded = part.casefold()
            if folded in IGNORED_SAVE_NAMES or folded == ".stversions":
                return True
            if folded.startswith("._"):
                return True
        return False

    def _classify(self, relpath: str) -> dict[str, object]:
        """Describe both the legacy WebDAV tree and the shared emulator vault."""
        parts = Path(relpath).parts
        if not parts:
            return {"emulator": "Unknown", "core": "", "kind": "other", "matchable": False}
        first = parts[0]
        first_key = self._core_key(first)
        filename = parts[-1]
        suffix = Path(filename).suffix.casefold()
        state_match = STATE_NAME.match(filename)

        # Legacy RetroArch WebDAV layout: saves/<core>/... and states/<core>/...
        if first.casefold() in {"saves", "states"}:
            kind = "save" if first.casefold() == "saves" else "state"
            core = parts[1] if len(parts) >= 3 else ""
            content_name = state_match.group("name") if state_match else Path(filename).stem
            return {
                "emulator": "RetroArch",
                "core": core,
                "kind": kind,
                "content_name": content_name,
                "matchable": bool(state_match or suffix in SAVE_EXTENSIONS),
                "platforms": CORE_PLATFORMS.get(self._core_key(core)),
                "match_strategy": "filename",
            }

        emulator = first
        core = ""
        if first_key == "retroarch":
            core = parts[1] if len(parts) >= 3 else ""
            allowed = CORE_PLATFORMS.get(self._core_key(core))
            strategy = "filename"
        else:
            allowed = EMULATOR_PLATFORMS.get(first_key)
            strategy = "filename" if allowed else "game_id"

        folded_parts = {part.casefold() for part in parts[1:-1]}
        if state_match or "states" in folded_parts:
            kind = "state"
            content_name = state_match.group("name") if state_match else Path(filename).stem
        elif suffix in SAVE_EXTENSIONS or "saves" in folded_parts or first_key == "retroarch":
            kind = "save"
            content_name = Path(filename).stem
        else:
            kind = "other"
            content_name = Path(filename).stem
        return {
            "emulator": "RetroArch" if first_key == "retroarch" else emulator,
            "core": core,
            "kind": kind,
            "content_name": content_name,
            "matchable": strategy == "filename" and kind in {"save", "state"},
            "platforms": allowed,
            "match_strategy": strategy,
        }

    def _quick_signature(self) -> tuple[tuple[str, int, int], ...]:
        root = self.settings.saves_root
        signature: list[tuple[str, int, int]] = []
        for path in self._source_paths():
            try:
                stat = path.stat()
            except OSError as exc:
                raise LibraryError(f"Could not inspect {path.name}: {exc}") from exc
            signature.append((path.relative_to(root).as_posix(), stat.st_size, stat.st_mtime_ns))
        return tuple(signature)

    @staticmethod
    def _tree_hash(files: list[SaveFile]) -> str:
        digest = hashlib.sha256()
        for item in sorted(files, key=lambda file: file.relpath):
            digest.update(item.relpath.encode("utf-8"))
            digest.update(b"\0")
            digest.update(item.sha256.encode("ascii"))
            digest.update(str(item.size).encode("ascii"))
            digest.update(b"\0")
        return digest.hexdigest()

    def _blob_path(self, sha256: str) -> Path:
        return self.blob_root / sha256[:2] / sha256[2:4] / sha256

    @staticmethod
    def _hash_path(path: Path, cancel_check: CancelCheck | None = None) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                if cancel_check:
                    cancel_check()
                digest.update(chunk)
        return digest.hexdigest()

    def _capture_files(
        self,
        progress_callback: ProgressCallback | None,
        cancel_check: CancelCheck | None,
    ) -> tuple[list[SaveFile], int]:
        root = self.settings.saves_root
        paths = self._source_paths()
        total_bytes = sum(path.stat().st_size for path in paths)
        processed_bytes = 0
        new_bytes = 0
        files: list[SaveFile] = []
        for index, path in enumerate(paths, start=1):
            if cancel_check:
                cancel_check()
            before = path.stat()
            temp = self.staging_root / f"capture-{uuid.uuid4().hex}.tmp"
            digest = hashlib.sha256()
            try:
                with path.open("rb") as source, temp.open("xb") as target:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        if cancel_check:
                            cancel_check()
                        digest.update(chunk)
                        target.write(chunk)
                        processed_bytes += len(chunk)
                        if progress_callback:
                            fraction = processed_bytes / total_bytes if total_bytes else index / max(len(paths), 1)
                            progress_callback(
                                min(88, 5 + int(fraction * 83)),
                                f"Snapshotting {index:,} of {len(paths):,} files",
                            )
                after = path.stat()
                if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                    raise LibraryError(f"Save source changed while reading {path.relative_to(root)}")
                sha256 = digest.hexdigest()
                blob = self._blob_path(sha256)
                if blob.exists():
                    temp.unlink(missing_ok=True)
                else:
                    blob.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        os.replace(temp, blob)
                        new_bytes += before.st_size
                    except FileExistsError:
                        temp.unlink(missing_ok=True)
                files.append(
                    SaveFile(
                        path.relative_to(root).as_posix(),
                        before.st_size,
                        before.st_mtime_ns,
                        sha256,
                    )
                )
            finally:
                temp.unlink(missing_ok=True)
        return files, new_bytes

    def create_snapshot(
        self,
        trigger: str = "manual",
        note: str = "",
        progress_callback: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> dict[str, object]:
        with self._operation_lock:
            with self.db.write() as connection:
                connection.execute(
                    "UPDATE save_settings SET last_attempt_at=CURRENT_TIMESTAMP WHERE id=1"
                )
            if progress_callback:
                progress_callback(1, "Waiting for the save source to become quiet")
            stable = False
            for _ in range(3):
                if cancel_check:
                    cancel_check()
                before = self._quick_signature()
                if self.settings.save_snapshot_quiet_seconds:
                    time.sleep(self.settings.save_snapshot_quiet_seconds)
                after = self._quick_signature()
                if before == after:
                    stable = True
                    break
            if not stable:
                raise LibraryError("Save files kept changing; close all emulators and try the snapshot again")
            try:
                files, new_bytes = self._capture_files(progress_callback, cancel_check)
                if self._quick_signature() != after:
                    raise LibraryError(
                        "Save files changed before the snapshot could finish; no snapshot was published"
                    )
            except Exception:
                # A failed capture may have completed new immutable blobs before a
                # later file changed. No manifest references them, so reclaim them.
                self.garbage_collect_blobs()
                raise
            tree_hash = self._tree_hash(files)
            with self.db.connect() as connection:
                latest = connection.execute(
                    "SELECT * FROM save_snapshots ORDER BY id DESC LIMIT 1"
                ).fetchone()
                previous_files = {
                    row["relpath"]: row["sha256"]
                    for row in connection.execute(
                        "SELECT relpath,sha256 FROM save_snapshot_files WHERE snapshot_id=?",
                        (latest["id"],),
                    )
                } if latest else {}
            # Snapshot manifests are immutable, so an identical latest tree is
            # already a complete safety point. Reuse it for manual, scheduled,
            # and pre-operation snapshots instead of publishing duplicate
            # manifests when no save content changed.
            if latest and latest["tree_hash"] == tree_hash:
                return {
                    "snapshot_id": latest["id"],
                    "unchanged": True,
                    "files": len(files),
                    "logical_bytes": sum(item.size for item in files),
                    "new_bytes": 0,
                }
            current_files = {item.relpath: item.sha256 for item in files}
            added = len(current_files.keys() - previous_files.keys())
            removed = len(previous_files.keys() - current_files.keys())
            changed = sum(
                1 for path in current_files.keys() & previous_files.keys()
                if current_files[path] != previous_files[path]
            )
            if progress_callback:
                progress_callback(92, "Publishing snapshot manifest")
            with self.db.write() as connection:
                connection.execute(
                    "INSERT INTO save_snapshots("
                    "trigger,note,tree_hash,file_count,logical_bytes,new_bytes,added_count,changed_count,removed_count,source_root"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        trigger,
                        note.strip()[:500],
                        tree_hash,
                        len(files),
                        sum(item.size for item in files),
                        new_bytes,
                        added,
                        changed,
                        removed,
                        str(self.settings.saves_root.resolve()),
                    ),
                )
                snapshot_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                connection.executemany(
                    "INSERT INTO save_snapshot_files(snapshot_id,relpath,size,mtime_ns,sha256) "
                    "VALUES(?,?,?,?,?)",
                    (
                        (snapshot_id, item.relpath, item.size, item.mtime_ns, item.sha256)
                        for item in files
                    ),
                )
            pruned = self.prune_retention()
            self.db.activity(
                "save_snapshot",
                f"Created save snapshot #{snapshot_id}: {len(files)} files, {added} added, {changed} changed, {removed} removed",
            )
            return {
                "snapshot_id": snapshot_id,
                "unchanged": False,
                "files": len(files),
                "logical_bytes": sum(item.size for item in files),
                "new_bytes": new_bytes,
                "added": added,
                "changed": changed,
                "removed": removed,
                "pruned": pruned,
            }

    def list_snapshots(self, limit: int = 100, offset: int = 0) -> dict[str, object]:
        with self.db.connect() as connection:
            total = connection.execute("SELECT COUNT(*) AS count FROM save_snapshots").fetchone()["count"]
            rows = connection.execute(
                "SELECT * FROM save_snapshots ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return {"items": [dict(row) for row in rows], "total": total, "limit": limit, "offset": offset}

    def current_files(
        self,
        search: str = "",
        limit: int = 250,
        offset: int = 0,
        sort: str = "modified_desc",
    ) -> dict[str, object]:
        root = self.settings.saves_root
        items: list[dict[str, object]] = []
        if self.available():
            for path in self._source_paths():
                relpath = path.relative_to(root).as_posix()
                if search.strip() and search.strip().lower() not in relpath.lower():
                    continue
                try:
                    stat = path.stat()
                except OSError:
                    continue
                items.append({
                    "relpath": relpath,
                    "size": stat.st_size,
                    "mtime_ns": stat.st_mtime_ns,
                    **self._classify(relpath),
                })
        text_key = lambda item, field: str(item.get(field) or "").casefold()
        sort_keys = {
            "path_asc": lambda item: (text_key(item, "relpath"),),
            "path_desc": lambda item: (text_key(item, "relpath"),),
            "emulator_asc": lambda item: (text_key(item, "emulator"), text_key(item, "relpath")),
            "emulator_desc": lambda item: (text_key(item, "emulator"), text_key(item, "relpath")),
            "type_asc": lambda item: (text_key(item, "kind"), text_key(item, "relpath")),
            "type_desc": lambda item: (text_key(item, "kind"), text_key(item, "relpath")),
            "size_asc": lambda item: (int(item.get("size") or 0), text_key(item, "relpath")),
            "size_desc": lambda item: (int(item.get("size") or 0), text_key(item, "relpath")),
            "modified_asc": lambda item: (int(item.get("mtime_ns") or 0), text_key(item, "relpath")),
            "modified_desc": lambda item: (int(item.get("mtime_ns") or 0), text_key(item, "relpath")),
        }
        selected_sort = sort if sort in sort_keys else "modified_desc"
        items.sort(key=sort_keys[selected_sort], reverse=selected_sort.endswith("_desc"))
        total = len(items)
        return {
            "items": items[offset:offset + limit],
            "total": total,
            "limit": limit,
            "offset": offset,
            "available": self.available(),
        }

    def _conflict_record(
        self, path: Path, device_names: dict[str, str] | None = None
    ) -> dict[str, object] | None:
        path = path.resolve()
        match = SYNC_CONFLICT_NAME.match(path.name)
        if not match:
            return None
        root = self.settings.saves_root.resolve()
        conflict_relpath = path.relative_to(root).as_posix()
        canonical = path.with_name(f"{match.group('base')}{match.group('extension') or ''}")
        try:
            conflict_stat = path.stat()
            conflict_hash = self._hash_path(path)
        except OSError:
            return None
        canonical_exists = canonical.is_file()
        canonical_hash = ""
        canonical_size = 0
        canonical_mtime_ns = 0
        if canonical_exists:
            try:
                canonical_stat = canonical.stat()
                canonical_hash = self._hash_path(canonical)
                canonical_size = canonical_stat.st_size
                canonical_mtime_ns = canonical_stat.st_mtime_ns
            except OSError:
                canonical_exists = False
                canonical_hash = ""
        device_id = match.group("device")
        device_name = ""
        for prefix, name in (device_names or {}).items():
            if prefix.casefold().startswith(device_id.casefold()) or device_id.casefold().startswith(prefix.casefold()):
                device_name = name
                break
        classification = self._classify(canonical.relative_to(root).as_posix())
        return {
            "conflict_relpath": conflict_relpath,
            "canonical_relpath": canonical.relative_to(root).as_posix(),
            "canonical_exists": canonical_exists,
            "device_id": device_id,
            "device_name": device_name,
            "conflict_at": f"{match.group('date')[0:4]}-{match.group('date')[4:6]}-{match.group('date')[6:8]}T{match.group('time')[0:2]}:{match.group('time')[2:4]}:{match.group('time')[4:6]}",
            "conflict_size": conflict_stat.st_size,
            "conflict_mtime_ns": conflict_stat.st_mtime_ns,
            "conflict_sha256": conflict_hash,
            "canonical_size": canonical_size,
            "canonical_mtime_ns": canonical_mtime_ns,
            "canonical_sha256": canonical_hash,
            "identical": bool(canonical_hash and canonical_hash == conflict_hash),
            **classification,
        }

    def conflicts(
        self,
        search: str = "",
        limit: int = 100,
        offset: int = 0,
        device_names: dict[str, str] | None = None,
    ) -> dict[str, object]:
        items: list[dict[str, object]] = []
        if self.available():
            needle = search.strip().casefold()
            for path in self._source_paths():
                if ".sync-conflict-" not in path.name.casefold():
                    continue
                item = self._conflict_record(path, device_names)
                if not item:
                    continue
                if needle and not any(
                    needle in str(item.get(field, "")).casefold()
                    for field in ("conflict_relpath", "canonical_relpath", "device_id", "device_name", "emulator")
                ):
                    continue
                items.append(item)
        items.sort(key=lambda item: (str(item["conflict_at"]), str(item["conflict_relpath"])), reverse=True)
        total = len(items)
        with self.db.connect() as connection:
            history = [dict(row) for row in connection.execute(
                "SELECT * FROM save_conflict_resolutions ORDER BY id DESC LIMIT 50"
            )]
        return {
            "items": items[offset:offset + limit],
            "total": total,
            "limit": limit,
            "offset": offset,
            "available": self.available(),
            "identical": sum(bool(item["identical"]) for item in items),
            "history": history,
        }

    def resolve_conflict(
        self,
        conflict_relpath: str,
        decision: str,
        expected_canonical_sha256: str,
        expected_conflict_sha256: str,
        device_id: str = "",
        device_name: str = "",
        progress_callback: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> dict[str, object]:
        if decision not in {"current", "conflict"}:
            raise LibraryError("Choose either the current or conflict version")
        with self._operation_lock:
            root = self.settings.saves_root.resolve()
            conflict = (root / conflict_relpath).resolve()
            if conflict == root or root not in conflict.parents:
                raise LibraryError("Conflict path escaped the configured save vault")
            record = self._conflict_record(conflict)
            if not record:
                raise LibraryError("The Syncthing conflict no longer exists; refresh conflicts")
            if record["conflict_sha256"] != expected_conflict_sha256:
                raise LibraryError("The conflict version changed after review; refresh conflicts")
            if str(record["canonical_sha256"]) != expected_canonical_sha256:
                raise LibraryError("The current version changed after review; refresh conflicts")
            if cancel_check:
                cancel_check()
            if progress_callback:
                progress_callback(2, "Creating a safety snapshot of both save versions")
            snapshot = self.create_snapshot(
                trigger="pre_conflict_resolution",
                note=f"Before resolving {record['canonical_relpath']}",
                progress_callback=(
                    (lambda progress, detail: progress_callback(min(90, progress), detail))
                    if progress_callback else None
                ),
                cancel_check=cancel_check,
            )
            canonical = (root / str(record["canonical_relpath"])).resolve()
            if cancel_check:
                cancel_check()
            if decision == "current":
                if not canonical.is_file():
                    raise LibraryError("The current version is missing; choose the conflict version")
                conflict.unlink()
            else:
                canonical.parent.mkdir(parents=True, exist_ok=True)
                staged = canonical.with_name(f".{canonical.name}.rommates-conflict-{uuid.uuid4().hex}.tmp")
                try:
                    shutil.copy2(conflict, staged)
                    if self._hash_path(staged, cancel_check) != expected_conflict_sha256:
                        raise LibraryError("The staged conflict version failed its integrity check")
                    os.replace(staged, canonical)
                    conflict.unlink()
                finally:
                    staged.unlink(missing_ok=True)
            with self.db.write() as connection:
                connection.execute(
                    "INSERT INTO save_conflict_resolutions("
                    "canonical_relpath,conflict_relpath,device_id,device_name,decision,"
                    "canonical_sha256,conflict_sha256,safety_snapshot_id"
                    ") VALUES(?,?,?,?,?,?,?,?)",
                    (
                        record["canonical_relpath"], record["conflict_relpath"],
                        device_id[:64], device_name[:255], decision,
                        expected_canonical_sha256, expected_conflict_sha256,
                        snapshot["snapshot_id"],
                    ),
                )
            if progress_callback:
                progress_callback(100, "Resolved save conflict")
            self.db.activity(
                "save_conflict",
                f"Resolved {record['canonical_relpath']} using the {decision} version after snapshot #{snapshot['snapshot_id']}",
            )
            return {
                "canonical_relpath": record["canonical_relpath"],
                "decision": decision,
                "safety_snapshot_id": snapshot["snapshot_id"],
                "device_name": device_name,
            }

    def source_summary(self) -> dict[str, object]:
        """Return lightweight live-tree totals for the collection dashboard."""
        if not self.available():
            return {
                "available": False,
                "files": 0,
                "bytes": 0,
                "save_files": 0,
                "state_files": 0,
                "latest_mtime_ns": 0,
                "emulators": [],
            }
        root = self.settings.saves_root
        files = total_bytes = save_files = state_files = latest_mtime_ns = 0
        emulators: dict[str, dict[str, object]] = {}
        for path in self._source_paths():
            try:
                stat = path.stat()
            except OSError:
                continue
            relpath = path.relative_to(root).as_posix()
            classification = self._classify(relpath)
            files += 1
            total_bytes += stat.st_size
            latest_mtime_ns = max(latest_mtime_ns, stat.st_mtime_ns)
            if classification["kind"] == "save":
                save_files += 1
            elif classification["kind"] == "state":
                state_files += 1
            emulator = str(classification["emulator"])
            summary = emulators.setdefault(emulator, {
                "emulator": emulator, "files": 0, "bytes": 0,
                "save_files": 0, "state_files": 0, "latest_mtime_ns": 0,
            })
            summary["files"] += 1
            summary["bytes"] += stat.st_size
            summary["latest_mtime_ns"] = max(int(summary["latest_mtime_ns"]), stat.st_mtime_ns)
            if classification["kind"] == "save":
                summary["save_files"] += 1
            elif classification["kind"] == "state":
                summary["state_files"] += 1
        return {
            "available": True,
            "files": files,
            "bytes": total_bytes,
            "save_files": save_files,
            "state_files": state_files,
            "latest_mtime_ns": latest_mtime_ns,
            "emulators": sorted(
                emulators.values(), key=lambda item: (-int(item["files"]), str(item["emulator"]).casefold())
            ),
        }

    @staticmethod
    def _core_key(value: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", value.casefold())

    def _save_groups(self) -> list[dict[str, object]]:
        if not self.available():
            return []
        root = self.settings.saves_root
        groups: dict[tuple[str, str], dict[str, object]] = {}
        for path in self._source_paths():
            relpath = path.relative_to(root).as_posix()
            classification = self._classify(relpath)
            if not classification["matchable"]:
                continue
            content_name = str(classification["content_name"])
            kind = str(classification["kind"])
            core = str(classification["core"] or classification["emulator"])
            scope_key = self._core_key(core)
            key = (scope_key, content_name.casefold())
            group = groups.setdefault(
                key,
                {
                    "key": "\x1f".join(key),
                    "core": core,
                    "emulator": classification["emulator"],
                    "platforms": classification["platforms"],
                    "content_name": content_name,
                    "files": [],
                    "save_files": 0,
                    "state_files": 0,
                    "bytes": 0,
                    "latest_mtime_ns": 0,
                },
            )
            try:
                stat = path.stat()
            except OSError:
                continue
            group["files"].append(
                {"relpath": relpath, "kind": kind, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
            )
            group[f"{kind}_files"] += 1
            group["bytes"] += stat.st_size
            group["latest_mtime_ns"] = max(group["latest_mtime_ns"], stat.st_mtime_ns)
        return sorted(groups.values(), key=lambda item: (str(item["content_name"]).casefold(), str(item["core"]).casefold()))

    def _matched_save_groups(self) -> tuple[list[dict[str, object]], dict[int, dict[str, object]]]:
        with self.db.connect() as connection:
            games = [dict(row) for row in connection.execute(
                "SELECT id,platform,display_name,primary_relpath,normalized_name FROM games"
            )]
        exact_map: dict[str, list[dict[str, object]]] = {}
        normalized_map: dict[str, list[dict[str, object]]] = {}
        for game in games:
            names = {str(game["display_name"]).casefold(), Path(game["primary_relpath"]).stem.casefold()}
            for name in names:
                exact_map.setdefault(name, []).append(game)
            normalized = str(game["normalized_name"] or normalize_name(str(game["display_name"])))
            if normalized:
                normalized_map.setdefault(normalized, []).append(game)

        impacts: dict[int, dict[str, object]] = {
            game["id"]: {
                "status": "none", "groups": 0, "files": 0, "save_files": 0,
                "state_files": 0, "paths": [], "content_names": [],
            }
            for game in games
        }
        rank = {"none": 0, "possible": 1, "exact": 2, "ambiguous": 3}
        groups = self._save_groups()
        for group in groups:
            allowed = group.get("platforms")

            def narrow(candidates: list[dict[str, object]]) -> list[dict[str, object]]:
                if not allowed:
                    return candidates
                filtered = [game for game in candidates if str(game["platform"]).casefold() in allowed]
                return filtered or candidates

            candidates = narrow(exact_map.get(str(group["content_name"]).casefold(), []))
            if len(candidates) == 1:
                status = "exact"
            elif len(candidates) > 1:
                status = "ambiguous"
            else:
                normalized = normalize_name(str(group["content_name"]))
                candidates = narrow(normalized_map.get(normalized, [])) if normalized else []
                status = "possible" if len(candidates) == 1 else "ambiguous" if candidates else "orphan"
            group["status"] = status
            group["games"] = [
                {"id": game["id"], "name": game["display_name"], "platform": game["platform"]}
                for game in candidates
            ]
            for game in candidates:
                impact = impacts[game["id"]]
                if rank.get(status, 0) > rank.get(str(impact["status"]), 0):
                    impact["status"] = status
                impact["groups"] += 1
                impact["files"] += len(group["files"])
                impact["save_files"] += group["save_files"]
                impact["state_files"] += group["state_files"]
                impact["content_names"].append(group["content_name"])
                remaining = max(0, 12 - len(impact["paths"]))
                impact["paths"].extend(item["relpath"] for item in group["files"][:remaining])
        return groups, impacts

    def save_impacts(self, game_ids: list[int] | None = None) -> dict[int, dict[str, object]]:
        _, impacts = self._matched_save_groups()
        if game_ids is None:
            return impacts
        wanted = set(game_ids)
        return {game_id: impact for game_id, impact in impacts.items() if game_id in wanted}

    def match_summary(self) -> dict[str, int]:
        groups, _ = self._matched_save_groups()
        return {
            "groups": len(groups),
            "exact": sum(group["status"] == "exact" for group in groups),
            "possible": sum(group["status"] == "possible" for group in groups),
            "ambiguous": sum(group["status"] == "ambiguous" for group in groups),
            "orphan": sum(group["status"] == "orphan" for group in groups),
        }

    def unmatched_groups(
        self, search: str = "", status: str = "all", limit: int = 200, offset: int = 0
    ) -> dict[str, object]:
        groups, _ = self._matched_save_groups()
        items = [group for group in groups if group["status"] != "exact"]
        if status != "all":
            items = [group for group in items if group["status"] == status]
        if search.strip():
            needle = search.strip().casefold()
            items = [
                group for group in items
                if needle in str(group["content_name"]).casefold()
                or needle in str(group["core"]).casefold()
                or any(needle in item["relpath"].casefold() for item in group["files"])
            ]
        total = len(items)
        summary = {
            "groups": len(groups),
            "exact": sum(group["status"] == "exact" for group in groups),
            "possible": sum(group["status"] == "possible" for group in groups),
            "ambiguous": sum(group["status"] == "ambiguous" for group in groups),
            "orphan": sum(group["status"] == "orphan" for group in groups),
        }
        return {
            "items": items[offset:offset + limit],
            "total": total,
            "limit": limit,
            "offset": offset,
            "summary": summary,
            "available": self.available(),
        }

    def delete_orphan_group(
        self,
        group_key: str,
        progress_callback: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> dict[str, object]:
        """Remove one revalidated orphan group after publishing a full safety snapshot."""
        with self._operation_lock:
            groups, _ = self._matched_save_groups()
            group = next((item for item in groups if item["key"] == group_key), None)
            if not group:
                raise LibraryError("The save group no longer exists; refresh Save matching")
            if group["status"] != "orphan":
                raise LibraryError("Only save groups with no ROM match can be deleted")
            if progress_callback:
                progress_callback(1, "Creating a safety snapshot before deleting saves")
            snapshot = self.create_snapshot(
                trigger="pre_save_delete",
                note=f"Before deleting orphan saves for {group['content_name']}",
                progress_callback=(
                    (lambda progress, detail: progress_callback(min(90, progress), detail))
                    if progress_callback else None
                ),
                cancel_check=cancel_check,
            )
            snapshot_id = int(snapshot["snapshot_id"])
            root = self.settings.saves_root.resolve()
            expected = {item["relpath"]: item for item in group["files"]}
            targets: list[tuple[Path, dict[str, object]]] = []
            for relpath, item in expected.items():
                target = (root / relpath).resolve()
                if target != root and root not in target.parents:
                    raise LibraryError("A save path escaped the configured source root")
                try:
                    stat = target.stat()
                except OSError as exc:
                    raise LibraryError(f"Could not revalidate {relpath}: {exc}") from exc
                if stat.st_size != item["size"] or stat.st_mtime_ns != item["mtime_ns"]:
                    raise LibraryError(f"{relpath} changed after review; refresh Save matching")
                targets.append((target, item))
            with self.db.connect() as connection:
                snapshot_files = {
                    row["relpath"]: dict(row)
                    for row in connection.execute(
                        "SELECT relpath,size,mtime_ns,sha256 FROM save_snapshot_files "
                        "WHERE snapshot_id=? AND relpath IN (%s)" % ",".join("?" for _ in expected),
                        [snapshot_id, *expected],
                    )
                }
            if len(snapshot_files) != len(expected):
                raise LibraryError("The safety snapshot did not capture every selected save file")
            deleted: list[tuple[Path, dict[str, object]]] = []
            try:
                for target, item in targets:
                    target.unlink()
                    deleted.append((target, snapshot_files[str(item["relpath"])]))
            except Exception as exc:
                for target, snapshot_file in deleted:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(self._blob_path(str(snapshot_file["sha256"])), target)
                    os.utime(target, ns=(int(snapshot_file["mtime_ns"]), int(snapshot_file["mtime_ns"])))
                raise LibraryError(f"Could not delete the complete save group; deleted files were restored: {exc}") from exc
            if progress_callback:
                progress_callback(100, f"Deleted {len(deleted)} orphan save files")
            self.db.activity(
                "save_delete",
                f"Deleted {len(deleted)} orphan save files for {group['content_name']} after snapshot #{snapshot_id}",
            )
            return {
                "group": group["content_name"],
                "files": len(deleted),
                "bytes": sum(int(item["size"]) for _, item in targets),
                "safety_snapshot_id": snapshot_id,
            }

    def snapshot_detail(
        self, snapshot_id: int, search: str = "", limit: int = 250, offset: int = 0
    ) -> dict[str, object]:
        with self.db.connect() as connection:
            snapshot = connection.execute(
                "SELECT * FROM save_snapshots WHERE id=?", (snapshot_id,)
            ).fetchone()
            if not snapshot:
                raise LibraryError("Save snapshot was not found")
            params: list[object] = [snapshot_id]
            where = "snapshot_id=?"
            if search.strip():
                where += " AND relpath LIKE ? ESCAPE '\\'"
                escaped = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                params.append(f"%{escaped}%")
            total = connection.execute(
                f"SELECT COUNT(*) AS count FROM save_snapshot_files WHERE {where}", params
            ).fetchone()["count"]
            rows = connection.execute(
                f"SELECT relpath,size,mtime_ns,sha256 FROM save_snapshot_files WHERE {where} "
                "ORDER BY relpath LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return {
            "snapshot": dict(snapshot),
            "files": [dict(row) for row in rows],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def _current_hashed(self, cancel_check: CancelCheck | None = None) -> list[SaveFile]:
        files: list[SaveFile] = []
        root = self.settings.saves_root
        for path in self._source_paths():
            if cancel_check:
                cancel_check()
            stat = path.stat()
            files.append(
                SaveFile(
                    path.relative_to(root).as_posix(),
                    stat.st_size,
                    stat.st_mtime_ns,
                    self._hash_path(path, cancel_check),
                )
            )
        return files

    def compare(self, snapshot_id: int) -> dict[str, object]:
        with self.db.connect() as connection:
            snapshot = connection.execute(
                "SELECT source_root FROM save_snapshots WHERE id=?", (snapshot_id,)
            ).fetchone()
            if not snapshot:
                raise LibraryError("Save snapshot was not found")
            current_root = str(self.settings.saves_root.resolve())
            if not snapshot["source_root"] or snapshot["source_root"] != current_root:
                return {
                    "snapshot_id": snapshot_id,
                    "compatible": False,
                    "reason": "This snapshot belongs to a previous save source and is download-only.",
                    "restore": [], "overwrite": [], "delete": [], "unchanged": 0,
                    "current_tree_hash": "",
                }
            desired = {
                row["relpath"]: dict(row)
                for row in connection.execute(
                    "SELECT relpath,size,mtime_ns,sha256 FROM save_snapshot_files WHERE snapshot_id=?",
                    (snapshot_id,),
                )
            }
        current_list = self._current_hashed()
        current = {item.relpath: item for item in current_list}
        restore = sorted(desired.keys() - current.keys())
        delete = sorted(current.keys() - desired.keys())
        overwrite = sorted(
            path for path in desired.keys() & current.keys()
            if desired[path]["sha256"] != current[path].sha256
        )
        return {
            "snapshot_id": snapshot_id,
            "compatible": True,
            "current_tree_hash": self._tree_hash(current_list),
            "restore": restore,
            "overwrite": overwrite,
            "delete": delete,
            "unchanged": len(desired) - len(restore) - len(overwrite),
        }

    def restore_snapshot(
        self,
        snapshot_id: int,
        expected_tree_hash: str,
        progress_callback: ProgressCallback | None = None,
        cancel_check: CancelCheck | None = None,
    ) -> dict[str, object]:
        with self._operation_lock:
            with self.db.connect() as connection:
                snapshot = connection.execute(
                    "SELECT source_root FROM save_snapshots WHERE id=?", (snapshot_id,)
                ).fetchone()
            if not snapshot:
                raise LibraryError("Save snapshot was not found")
            if (
                not snapshot["source_root"]
                or snapshot["source_root"] != str(self.settings.saves_root.resolve())
            ):
                raise LibraryError("This snapshot belongs to a previous save source and cannot be restored")
            current = self._current_hashed(cancel_check)
            if self._tree_hash(current) != expected_tree_hash:
                raise LibraryError("Save files changed after the restore preview. Refresh the comparison and try again.")
            with self.db.write() as connection:
                target = connection.execute(
                    "SELECT pinned FROM save_snapshots WHERE id=?", (snapshot_id,)
                ).fetchone()
                if not target:
                    raise LibraryError("Save snapshot was not found")
                was_pinned = bool(target["pinned"])
                connection.execute("UPDATE save_snapshots SET pinned=1 WHERE id=?", (snapshot_id,))
            try:
                if progress_callback:
                    progress_callback(8, "Creating pre-restore safety snapshot")
                safety = self.create_snapshot(
                    trigger="pre_restore",
                    note=f"Before restoring snapshot #{snapshot_id}",
                    progress_callback=lambda progress, detail: progress_callback(
                        min(35, 8 + progress // 4), detail
                    ) if progress_callback else None,
                    cancel_check=cancel_check,
                )
            finally:
                if not was_pinned:
                    with self.db.write() as connection:
                        connection.execute(
                            "UPDATE save_snapshots SET pinned=0 WHERE id=?", (snapshot_id,)
                        )
            with self.db.connect() as connection:
                desired_rows = connection.execute(
                    "SELECT relpath,size,mtime_ns,sha256 FROM save_snapshot_files WHERE snapshot_id=? ORDER BY relpath",
                    (snapshot_id,),
                ).fetchall()
                if not desired_rows and not connection.execute(
                    "SELECT 1 FROM save_snapshots WHERE id=?", (snapshot_id,)
                ).fetchone():
                    raise LibraryError("Save snapshot was not found")
            stage = self.staging_root / f"restore-{uuid.uuid4().hex}"
            desired_root = stage / "desired"
            backup_root = stage / "backup"
            desired_root.mkdir(parents=True)
            backup_root.mkdir(parents=True)
            root = self.settings.saves_root
            current_paths = self._source_paths()
            try:
                for index, row in enumerate(desired_rows, start=1):
                    if cancel_check:
                        cancel_check()
                    blob = self._blob_path(row["sha256"])
                    if not blob.is_file():
                        raise LibraryError(f"Snapshot blob is missing for {row['relpath']}")
                    target = desired_root / row["relpath"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(blob, target)
                    if self._hash_path(target, cancel_check) != row["sha256"]:
                        raise LibraryError(f"Snapshot integrity check failed for {row['relpath']}")
                    os.utime(target, ns=(row["mtime_ns"], row["mtime_ns"]))
                    if progress_callback:
                        progress_callback(
                            min(62, 35 + int(index / max(len(desired_rows), 1) * 27)),
                            f"Preparing {index:,} of {len(desired_rows):,} files",
                        )
                for path in current_paths:
                    relative = path.relative_to(root)
                    backup = backup_root / relative
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, backup)
                if self._tree_hash(self._current_hashed(cancel_check)) != expected_tree_hash:
                    raise LibraryError("Save files changed while the restore was being prepared. Nothing was changed.")
                desired_paths = {row["relpath"] for row in desired_rows}
                current_sizes = {
                    path.relative_to(root).as_posix(): path.stat().st_size for path in current_paths
                }
                missing_bytes = sum(
                    row["size"] for row in desired_rows if row["relpath"] not in current_sizes
                )
                largest_overwrite = max(
                    (
                        row["size"] for row in desired_rows
                        if row["relpath"] in current_sizes
                    ),
                    default=0,
                )
                required_bytes = missing_bytes + largest_overwrite
                free_bytes = shutil.disk_usage(root).free
                if required_bytes > free_bytes:
                    raise LibraryError(
                        f"Restore needs up to {required_bytes} temporary bytes but only "
                        f"{free_bytes} are available in the live save filesystem"
                    )
                if progress_callback:
                    progress_callback(66, "Applying restore")
                try:
                    for index, row in enumerate(desired_rows, start=1):
                        if cancel_check:
                            cancel_check()
                        target = root / row["relpath"]
                        target.parent.mkdir(parents=True, exist_ok=True)
                        temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.rommates-restore")
                        shutil.copy2(desired_root / row["relpath"], temp)
                        os.replace(temp, target)
                        if progress_callback:
                            progress_callback(
                                min(90, 66 + int(index / max(len(desired_rows), 1) * 24)),
                                f"Restoring {index:,} of {len(desired_rows):,} files",
                            )
                    for path in current_paths:
                        if path.relative_to(root).as_posix() not in desired_paths:
                            if cancel_check:
                                cancel_check()
                            path.unlink(missing_ok=True)
                    restored = self._current_hashed(cancel_check)
                    desired_hash = self._tree_hash(
                        [
                            SaveFile(row["relpath"], row["size"], row["mtime_ns"], row["sha256"])
                            for row in desired_rows
                        ]
                    )
                    if self._tree_hash(restored) != desired_hash:
                        raise LibraryError("Restored save files did not pass integrity verification")
                except Exception:
                    for path in self._source_paths():
                        path.unlink(missing_ok=True)
                    for backup in sorted(backup_root.rglob("*")):
                        if backup.is_file():
                            target = root / backup.relative_to(backup_root)
                            target.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(backup, target)
                    raise
                self._remove_empty_directories(root)
            finally:
                shutil.rmtree(stage, ignore_errors=True)
            self.db.activity(
                "save_restore",
                f"Restored save snapshot #{snapshot_id}; safety snapshot #{safety['snapshot_id']}",
            )
            return {
                "snapshot_id": snapshot_id,
                "safety_snapshot_id": safety["snapshot_id"],
                "files": len(desired_rows),
            }

    @staticmethod
    def _remove_empty_directories(root: Path) -> None:
        for directory in sorted(
            (path for path in root.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            try:
                directory.rmdir()
            except OSError:
                pass

    def pin(self, snapshot_id: int, pinned: bool) -> dict[str, object]:
        with self.db.write() as connection:
            changed = connection.execute(
                "UPDATE save_snapshots SET pinned=? WHERE id=?", (int(pinned), snapshot_id)
            ).rowcount
        if not changed:
            raise LibraryError("Save snapshot was not found")
        return {"snapshot_id": snapshot_id, "pinned": pinned}

    def prune_retention(self) -> int:
        settings = self.settings_payload()
        with self.db.connect() as connection:
            rows = [dict(row) for row in connection.execute(
                "SELECT id,pinned,created_at FROM save_snapshots ORDER BY id DESC"
            )]
            resolution_snapshots = {
                row["safety_snapshot_id"]
                for row in connection.execute(
                    "SELECT DISTINCT safety_snapshot_id FROM save_conflict_resolutions"
                )
            }
        keep: set[int] = {row["id"] for row in rows if row["pinned"]}
        # A conflict resolution promises that both branches remain recoverable
        # through its safety snapshot. Retention must therefore treat snapshots
        # referenced by resolution history as pinned; attempting to prune one is
        # also rejected by SQLite's foreign-key constraint.
        keep.update(resolution_snapshots)
        recent = int(settings["retention_recent"])
        keep.update(row["id"] for row in rows[:recent])
        now = datetime.now(timezone.utc)
        tiers = [
            ("day", int(settings["retention_daily"]), timedelta(days=1)),
            ("week", int(settings["retention_weekly"]), timedelta(weeks=1)),
            ("month", int(settings["retention_monthly"]), timedelta(days=30)),
        ]
        parsed = [
            (row, datetime.fromisoformat(row["created_at"].replace(" ", "T") + "+00:00"))
            for row in rows
        ]
        for _, count, width in tiers:
            buckets: set[int] = set()
            for row, created in parsed:
                age = now - created
                if age.total_seconds() < 0:
                    bucket = 0
                else:
                    bucket = int(age / width)
                if bucket < count and bucket not in buckets:
                    keep.add(row["id"])
                    buckets.add(bucket)
        remove = [row["id"] for row in rows if row["id"] not in keep]
        if remove:
            placeholders = ",".join("?" for _ in remove)
            with self.db.write() as connection:
                connection.execute(f"DELETE FROM save_snapshots WHERE id IN ({placeholders})", remove)
            self.garbage_collect_blobs()
        return len(remove)

    def garbage_collect_blobs(self) -> int:
        with self.db.connect() as connection:
            referenced = {
                row["sha256"] for row in connection.execute("SELECT DISTINCT sha256 FROM save_snapshot_files")
            }
        removed = 0
        if self.blob_root.exists():
            for path in self.blob_root.rglob("*"):
                if path.is_file() and path.name not in referenced:
                    path.unlink(missing_ok=True)
                    removed += 1
        return removed

    def due_for_automatic_snapshot(self) -> bool:
        settings = self.settings_payload()
        if not settings.get("enabled") or not settings.get("available"):
            return False
        interval = int(settings["interval_minutes"])
        if interval <= 0:
            return False
        with self.db.connect() as connection:
            attempt = connection.execute(
                "SELECT last_attempt_at FROM save_settings WHERE id=1"
            ).fetchone()
        if not attempt or not attempt["last_attempt_at"]:
            return True
        created = datetime.fromisoformat(attempt["last_attempt_at"].replace(" ", "T") + "+00:00")
        return datetime.now(timezone.utc) - created >= timedelta(minutes=interval)
