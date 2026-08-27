from __future__ import annotations

import hashlib
import os
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
from .library import JobCancelled, LibraryError


ProgressCallback = Callable[[int, str], None]
CancelCheck = Callable[[], None]


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
                f"RetroArch save source is unavailable at {root}. Mount its WebDAV backing directory there."
            )
        paths: list[Path] = []
        try:
            for path in root.rglob("*"):
                if path.is_symlink():
                    continue
                if path.is_file():
                    paths.append(path)
        except OSError as exc:
            raise LibraryError(f"Could not read the RetroArch save source: {exc}") from exc
        return sorted(paths, key=lambda item: item.relative_to(root).as_posix())

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
        force_record: bool = False,
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
                raise LibraryError("RetroArch saves kept changing; close content and try the snapshot again")
            try:
                files, new_bytes = self._capture_files(progress_callback, cancel_check)
                if self._quick_signature() != after:
                    raise LibraryError(
                        "RetroArch saves changed before the snapshot could finish; no snapshot was published"
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
            if latest and latest["tree_hash"] == tree_hash and not force_record:
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
                    "trigger,note,tree_hash,file_count,logical_bytes,new_bytes,added_count,changed_count,removed_count"
                    ") VALUES(?,?,?,?,?,?,?,?,?)",
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

    def current_files(self, search: str = "", limit: int = 250, offset: int = 0) -> dict[str, object]:
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
                items.append({"relpath": relpath, "size": stat.st_size, "mtime_ns": stat.st_mtime_ns})
        total = len(items)
        return {
            "items": items[offset:offset + limit],
            "total": total,
            "limit": limit,
            "offset": offset,
            "available": self.available(),
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
            if not connection.execute("SELECT 1 FROM save_snapshots WHERE id=?", (snapshot_id,)).fetchone():
                raise LibraryError("Save snapshot was not found")
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
                    force_record=True,
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
        keep: set[int] = {row["id"] for row in rows if row["pinned"]}
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
