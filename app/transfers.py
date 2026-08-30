from __future__ import annotations

import hashlib
import json
import os
import queue
import re
import secrets
import shutil
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import AsyncIterator, Iterator

from .config import Settings
from .db import Database
from .library import DESCRIPTOR_EXTENSIONS, LibraryError, LibraryService, _inside


MAX_UPLOAD_FILES = 20_000
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
PLATFORM_RE = re.compile(r"^[^/\\\x00-\x1f]{1,100}$")


class TransferError(LibraryError):
    def __init__(self, detail: str, status_code: int = 400):
        super().__init__(detail)
        self.status_code = status_code


def _safe_segment(value: str, label: str) -> str:
    value = value.strip()
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or any(ord(char) < 32 for char in value)
        or value.casefold().startswith(".rommates-")
    ):
        raise TransferError(f"{label} is not a safe filename")
    if len(value.encode("utf-8")) > 255:
        raise TransferError(f"{label} is too long")
    return value


def _safe_relative(value: str) -> str:
    if "\\" in value or len(value.encode("utf-8")) > 1024:
        raise TransferError("Upload paths must use short forward-slash paths")
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts:
        raise TransferError("Upload path must be relative")
    parts = [_safe_segment(part, "Upload path") for part in path.parts]
    return PurePosixPath(*parts).as_posix()


def _download_name(value: str) -> str:
    cleaned = "".join(char for char in value if ord(char) >= 32 and char not in "/\\").strip()
    return cleaned[:240] or "rommates-download"


class _QueueWriter:
    def __init__(self, output: queue.Queue, stop: threading.Event):
        self.output = output
        self.stop = stop
        self.position = 0

    def write(self, data: bytes) -> int:
        if not data:
            return 0
        while not self.stop.is_set():
            try:
                self.output.put(bytes(data), timeout=0.25)
                self.position += len(data)
                return len(data)
            except queue.Full:
                continue
        raise BrokenPipeError("Download was closed")

    def tell(self) -> int:
        return self.position

    def flush(self) -> None:
        return None

    def seekable(self) -> bool:
        return False


class TransferService:
    def __init__(self, settings: Settings, db: Database, library: LibraryService):
        self.settings = settings
        self.db = db
        self.library = library
        self._writes_lock = threading.Lock()
        self._active_writes: set[tuple[str, int]] = set()

    def initialize(self) -> None:
        self.settings.upload_root.mkdir(parents=True, exist_ok=True)
        upload_root = self.settings.upload_root.resolve()
        library_root = self.settings.library_root.resolve()
        if upload_root == library_root or library_root in upload_root.parents:
            raise LibraryError("ROMMATES_UPLOAD_ROOT must be outside the ROM library root")
        self.cleanup_expired()

    def _platform_root(self, platform: str) -> Path:
        if not PLATFORM_RE.fullmatch(platform.strip()):
            raise TransferError("Choose a valid existing platform")
        root = self.settings.library_root.resolve()
        target = _inside(root, root / platform.strip())
        if not target.is_dir() or target.parent != root:
            raise TransferError("Choose a platform directory that already exists", 404)
        return target

    def _session_root(self, session_id: str) -> Path:
        if not SESSION_ID_RE.fullmatch(session_id):
            raise TransferError("Upload session was not found", 404)
        root = self.settings.upload_root.resolve()
        return _inside(root, root / session_id)

    def _load_session(
        self,
        session_id: str,
        owner_user_id: int | None = None,
        admin: bool = True,
    ) -> tuple[dict, list[dict]]:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT s.*,u.username AS owner_username,u.display_name AS owner_display_name "
                "FROM upload_sessions s LEFT JOIN users u ON u.id=s.owner_user_id WHERE s.id=?",
                (session_id,),
            ).fetchone()
            if not row:
                raise TransferError("Upload session was not found", 404)
            if not admin and row["owner_user_id"] != owner_user_id:
                raise TransferError("Upload session was not found", 404)
            files = connection.execute(
                "SELECT * FROM upload_files WHERE session_id=? ORDER BY file_index",
                (session_id,),
            ).fetchall()
        return dict(row), [dict(item) for item in files]

    def _payload(
        self, session_id: str, owner_user_id: int | None = None, admin: bool = True
    ) -> dict[str, object]:
        session, files = self._load_session(session_id, owner_user_id, admin)
        session["folder_mode"] = bool(session["folder_mode"])
        session["files"] = files
        session["chunk_bytes"] = self.settings.upload_chunk_bytes
        return session

    def list_sessions(
        self, owner_user_id: int | None = None, admin: bool = True
    ) -> dict[str, object]:
        self.cleanup_expired()
        with self.db.connect() as connection:
            if admin:
                rows = connection.execute(
                    "SELECT id FROM upload_sessions WHERE status IN "
                    "('uploading','pending_review','finalizing','rejected') "
                    "ORDER BY updated_at DESC LIMIT 100"
                )
            else:
                rows = connection.execute(
                    "SELECT id FROM upload_sessions WHERE owner_user_id=? AND status IN "
                    "('uploading','pending_review','finalizing','rejected') "
                    "ORDER BY updated_at DESC LIMIT 20",
                    (owner_user_id,),
                )
            ids = [row["id"] for row in rows]
        return {
            "items": [self._payload(session_id, owner_user_id, admin) for session_id in ids],
            "max_bytes": self.settings.upload_max_bytes,
            "chunk_bytes": self.settings.upload_chunk_bytes,
        }

    def create_session(
        self,
        platform: str,
        bundle_name: str,
        folder_mode: bool,
        files: list[dict[str, object]],
        owner_user_id: int | None = None,
    ) -> dict[str, object]:
        self.cleanup_expired()
        platform_root = self._platform_root(platform)
        if not files or len(files) > MAX_UPLOAD_FILES:
            raise TransferError(f"Choose between 1 and {MAX_UPLOAD_FILES:,} files")
        normalized: list[dict[str, object]] = []
        paths: set[str] = set()
        total_size = 0
        for item in files:
            relpath = _safe_relative(str(item.get("relative_path") or ""))
            try:
                size = int(item.get("size"))
            except (TypeError, ValueError):
                raise TransferError(f"Invalid size for {relpath}") from None
            if size < 0 or size > self.settings.upload_max_bytes:
                raise TransferError(f"{relpath} exceeds the configured upload limit", 413)
            if relpath in paths:
                raise TransferError(f"Upload path is repeated: {relpath}")
            paths.add(relpath)
            total_size += size
            normalized.append({"relative_path": relpath, "size": size})
        if total_size > self.settings.upload_max_bytes:
            raise TransferError("The complete upload exceeds the configured size limit", 413)

        folder_mode = bool(folder_mode or len(normalized) > 1 or any("/" in p for p in paths))
        if folder_mode:
            bundle_name = _safe_segment(bundle_name, "Bundle name")
            descriptor_present = any(
                PurePosixPath(item["relative_path"]).suffix.casefold() in DESCRIPTOR_EXTENSIONS
                for item in normalized
            )
            if platform.casefold() not in self.settings.folder_bundle_platforms and not descriptor_present:
                raise TransferError(
                    "Multi-file uploads need a CUE, GDI, or M3U descriptor on this platform"
                )
            destination = _inside(platform_root, platform_root / bundle_name)
        else:
            bundle_name = ""
            relpath = normalized[0]["relative_path"]
            if "/" in relpath:
                raise TransferError("A single-file ROM cannot contain a folder path")
            if PurePosixPath(relpath).suffix.casefold() not in self.settings.extensions:
                raise TransferError("That file extension is not enabled for ROM scanning")
            destination = _inside(platform_root, platform_root / relpath)
        if destination.exists():
            raise TransferError(f"A library item already exists at {destination.name}", 409)

        manifest = {
            "platform": platform,
            "bundle_name": bundle_name,
            "folder_mode": folder_mode,
            "files": normalized,
        }
        encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        manifest_hash = hashlib.sha256(encoded).hexdigest()
        with self.db.connect() as connection:
            existing = connection.execute(
                "SELECT id FROM upload_sessions WHERE manifest_hash=? AND status='uploading' "
                "AND owner_user_id IS ? "
                "ORDER BY created_at DESC LIMIT 1",
                (manifest_hash, owner_user_id),
            ).fetchone()
        if existing:
            return self._payload(existing["id"], owner_user_id, owner_user_id is None)

        session_id = secrets.token_urlsafe(18)
        session_root = self._session_root(session_id)
        session_root.mkdir(parents=True, exist_ok=False)
        try:
            with self.db.write() as connection:
                connection.execute(
                    "INSERT INTO upload_sessions(id,platform,bundle_name,folder_mode,total_size,file_count,manifest_hash,owner_user_id) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        session_id,
                        platform,
                        bundle_name,
                        int(folder_mode),
                        total_size,
                        len(normalized),
                        manifest_hash,
                        owner_user_id,
                    ),
                )
                connection.executemany(
                    "INSERT INTO upload_files(session_id,file_index,relative_path,size) VALUES(?,?,?,?)",
                    (
                        (session_id, index, item["relative_path"], item["size"])
                        for index, item in enumerate(normalized)
                    ),
                )
        except Exception:
            shutil.rmtree(session_root, ignore_errors=True)
            raise
        self.db.activity("upload", f"Started upload to {platform}/{bundle_name or normalized[0]['relative_path']}")
        return self._payload(session_id, owner_user_id, owner_user_id is None)

    async def write_chunk(
        self,
        session_id: str,
        file_index: int,
        offset: int,
        stream: AsyncIterator[bytes],
        owner_user_id: int | None = None,
        admin: bool = True,
    ) -> dict[str, object]:
        key = (session_id, file_index)
        with self._writes_lock:
            if key in self._active_writes:
                raise TransferError("That upload file already has a chunk in progress", 409)
            self._active_writes.add(key)
        try:
            session, files = self._load_session(session_id, owner_user_id, admin)
            if session["status"] != "uploading":
                raise TransferError("Upload session is no longer writable", 409)
            file = next((item for item in files if item["file_index"] == file_index), None)
            if not file:
                raise TransferError("Upload file was not found", 404)
            if offset != file["received_size"]:
                raise TransferError(
                    f"Upload offset changed; resume at {file['received_size']}", 409
                )
            staging = _inside(
                self._session_root(session_id), self._session_root(session_id) / f"{file_index}.part"
            )
            actual_size = staging.stat().st_size if staging.exists() else 0
            if actual_size != offset:
                raise TransferError("Staged upload size failed its consistency check", 409)
            received = 0
            staging.parent.mkdir(parents=True, exist_ok=True)
            try:
                with staging.open("ab") as handle:
                    async for chunk in stream:
                        received += len(chunk)
                        if received > self.settings.upload_chunk_bytes:
                            raise TransferError("Upload chunk exceeds the configured limit", 413)
                        if offset + received > file["size"]:
                            raise TransferError("Upload exceeds the declared file size", 413)
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
            except Exception:
                if staging.exists():
                    with staging.open("r+b") as handle:
                        handle.truncate(offset)
                raise
            if received == 0 and offset < file["size"]:
                raise TransferError("Upload chunk was empty")
            with self.db.write() as connection:
                connection.execute(
                    "UPDATE upload_files SET received_size=received_size+? "
                    "WHERE session_id=? AND file_index=?",
                    (received, session_id, file_index),
                )
                connection.execute(
                    "UPDATE upload_sessions SET received_size=received_size+?,updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=?",
                    (received, session_id),
                )
            return self._payload(session_id, owner_user_id, admin)
        finally:
            with self._writes_lock:
                self._active_writes.discard(key)

    def finalize(self, session_id: str, *, progress_callback, cancel_check) -> dict[str, object]:
        session, files = self._load_session(session_id)
        if session["status"] not in {"uploading", "pending_review"}:
            raise TransferError("Upload session is not ready to finalize", 409)
        if any(item["received_size"] != item["size"] for item in files):
            raise TransferError("Upload is incomplete", 409)
        platform_root = self._platform_root(session["platform"])
        if session["folder_mode"]:
            destination = _inside(platform_root, platform_root / session["bundle_name"])
            temporary = _inside(platform_root, platform_root / f".rommates-upload-{session_id}")
        else:
            destination = _inside(platform_root, platform_root / files[0]["relative_path"])
            temporary = _inside(platform_root, platform_root / f".rommates-upload-{session_id}")
        if destination.exists() or temporary.exists():
            raise TransferError(f"A library item already exists at {destination.name}", 409)

        headroom = max(64 * 1024 * 1024, min(1024**3, max(1, session["total_size"] // 50)))
        required = headroom
        if self._session_root(session_id).stat().st_dev != platform_root.stat().st_dev:
            required += session["total_size"]
        if shutil.disk_usage(platform_root).free < required:
            raise TransferError("Not enough free space to finalize this upload", 507)

        previous_status = session["status"]
        with self.db.write() as connection:
            connection.execute(
                "UPDATE upload_sessions SET status='finalizing',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (session_id,),
            )
        moved: list[tuple[Path, Path]] = []
        committed = False
        try:
            if session["folder_mode"]:
                temporary.mkdir(parents=False, exist_ok=False)
            for position, item in enumerate(files, 1):
                cancel_check()
                source = _inside(
                    self._session_root(session_id),
                    self._session_root(session_id) / f"{item['file_index']}.part",
                )
                if not source.is_file() or source.stat().st_size != item["size"]:
                    raise TransferError(f"Staged file failed its size check: {item['relative_path']}")
                target = (
                    _inside(temporary, temporary / item["relative_path"])
                    if session["folder_mode"]
                    else temporary
                )
                self.library._atomic_move(source, target)
                moved.append((source, target))
                progress_callback(
                    5 + int(position / max(len(files), 1) * 65),
                    f"Finalizing {position:,} of {len(files):,} uploaded files",
                )
            cancel_check()
            temporary.rename(destination)
            committed = True
        except Exception:
            if not committed:
                for source, target in reversed(moved):
                    if target.exists() and not source.exists():
                        self.library._atomic_move(target, source)
                if temporary.is_dir():
                    shutil.rmtree(temporary, ignore_errors=True)
                else:
                    temporary.unlink(missing_ok=True)
                with self.db.write() as connection:
                    connection.execute(
                        "UPDATE upload_sessions SET status=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (previous_status, session_id),
                    )
            raise

        with self.db.write() as connection:
            connection.execute(
                "UPDATE upload_sessions SET status='complete',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (session_id,),
            )
        shutil.rmtree(self._session_root(session_id), ignore_errors=True)
        scan_error = ""
        progress_callback(75, "Indexing the uploaded bundle")
        try:
            scan_result = self.library.scan(
                progress_callback=lambda percent, detail: progress_callback(
                    75 + int(percent * 0.24), detail
                ),
                cancel_check=cancel_check,
            )
        except Exception as exc:
            scan_result = None
            scan_error = str(exc)
        relative_destination = destination.relative_to(self.settings.library_root.resolve()).as_posix()
        self.db.activity("upload", f"Added {relative_destination}")
        return {
            "session_id": session_id,
            "destination": relative_destination,
            "files": len(files),
            "bytes": session["total_size"],
            "scan": scan_result,
            "scan_error": scan_error,
        }

    def submit(self, session_id: str, owner_user_id: int) -> dict[str, object]:
        session, files = self._load_session(session_id, owner_user_id, admin=False)
        if session["status"] != "uploading":
            raise TransferError("Upload is not ready for review", 409)
        if any(item["received_size"] != item["size"] for item in files):
            raise TransferError("Upload is incomplete", 409)
        with self.db.write() as connection:
            connection.execute(
                "UPDATE upload_sessions SET status='pending_review',submitted_at=CURRENT_TIMESTAMP,"
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (session_id,),
            )
        self.db.activity("upload", f"Submitted upload {session_id} for review")
        return self._payload(session_id, owner_user_id, admin=False)

    def reject(self, session_id: str, reviewer_id: int | None, note: str) -> dict[str, object]:
        session, _ = self._load_session(session_id)
        if session["status"] != "pending_review":
            raise TransferError("Upload is not awaiting review", 409)
        shutil.rmtree(self._session_root(session_id), ignore_errors=True)
        with self.db.write() as connection:
            connection.execute(
                "UPDATE upload_sessions SET status='rejected',reviewed_by=?,reviewed_at=CURRENT_TIMESTAMP,"
                "review_note=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (reviewer_id, note.strip()[:500], session_id),
            )
        self.db.activity("upload", f"Rejected upload {session_id}")
        return self._payload(session_id)

    def cancel(
        self, session_id: str, owner_user_id: int | None = None, admin: bool = True
    ) -> dict[str, object]:
        session, _ = self._load_session(session_id, owner_user_id, admin)
        if session["status"] in {"finalizing", "pending_review"} and not admin:
            raise TransferError("This upload is awaiting administrator review", 409)
        if session["status"] == "finalizing":
            raise TransferError("Upload is currently finalizing", 409)
        shutil.rmtree(self._session_root(session_id), ignore_errors=True)
        with self.db.write() as connection:
            connection.execute("DELETE FROM upload_sessions WHERE id=?", (session_id,))
        self.db.activity("upload", f"Cancelled upload {session_id}")
        return {"cancelled": session_id}

    def cleanup_expired(self) -> None:
        with self.db.connect() as connection:
            stale = [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM upload_sessions WHERE status<>'finalizing' AND "
                    "updated_at < datetime('now', ?)",
                    (f"-{self.settings.upload_expiry_hours} hours",),
                )
            ]
        for session_id in stale:
            shutil.rmtree(self._session_root(session_id), ignore_errors=True)
        if stale:
            with self.db.write() as connection:
                placeholders = ",".join("?" for _ in stale)
                connection.execute(
                    f"DELETE FROM upload_sessions WHERE id IN ({placeholders})", stale
                )
        with self.db.write() as connection:
            connection.execute("DELETE FROM download_tickets WHERE expires_at<?", (int(time.time()),))

    def create_download_ticket(
        self, game_id: int, requested_by: int | None = None
    ) -> dict[str, object]:
        self.cleanup_expired()
        with self.db.connect() as connection:
            game = connection.execute("SELECT * FROM games WHERE id=?", (game_id,)).fetchone()
            if not game:
                raise TransferError("Game was not found", 404)
            files = connection.execute(
                "SELECT relpath,size FROM game_files WHERE game_id=? ORDER BY relpath", (game_id,)
            ).fetchall()
        if not files:
            raise TransferError("Game bundle has no downloadable files", 409)
        for item in files:
            path = _inside(
                self.settings.library_root,
                self.settings.library_root / item["relpath"],
            )
            if not path.is_file() or path.stat().st_size != item["size"]:
                raise TransferError(f"Library file failed its storage check: {path.name}", 409)
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        expires_at = int(time.time()) + self.settings.download_ticket_seconds
        archive = len(files) > 1
        filename = _download_name(game["display_name"] + (".zip" if archive else game["extension"]))
        with self.db.write() as connection:
            connection.execute(
                "INSERT INTO download_tickets(token_hash,game_id,expires_at,requested_by) VALUES(?,?,?,?)",
                (token_hash, game_id, expires_at, requested_by),
            )
        return {
            "url": f"/api/downloads/{token}",
            "filename": filename,
            "files": len(files),
            "bytes": sum(item["size"] for item in files),
            "archive": archive,
            "expires_at": expires_at,
        }

    def resolve_download(self, token: str) -> dict[str, object]:
        if not SESSION_ID_RE.fullmatch(token):
            raise TransferError("Download ticket was not found", 404)
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now = int(time.time())
        with self.db.write() as connection:
            ticket = connection.execute(
                "SELECT * FROM download_tickets WHERE token_hash=? AND expires_at>=? AND used_at IS NULL",
                (token_hash, now),
            ).fetchone()
            if not ticket:
                raise TransferError("Download ticket expired or was not found", 404)
            connection.execute(
                "UPDATE download_tickets SET used_at=? WHERE token_hash=?", (now, token_hash)
            )
            game = connection.execute("SELECT * FROM games WHERE id=?", (ticket["game_id"],)).fetchone()
            files = connection.execute(
                "SELECT relpath,size FROM game_files WHERE game_id=? ORDER BY relpath",
                (ticket["game_id"],),
            ).fetchall()
        if not game or not files:
            raise TransferError("Download is no longer available", 404)
        paths = []
        for item in files:
            path = _inside(self.settings.library_root, self.settings.library_root / item["relpath"])
            if not path.is_file() or path.stat().st_size != item["size"]:
                raise TransferError("Download failed its storage check", 409)
            paths.append((path, item["relpath"], item["size"]))
        archive = len(paths) > 1
        return {
            "game": dict(game),
            "paths": paths,
            "archive": archive,
            "filename": _download_name(
                game["display_name"] + (".zip" if archive else game["extension"])
            ),
        }

    def stream_zip(self, download: dict[str, object]) -> Iterator[bytes]:
        output: queue.Queue = queue.Queue(maxsize=8)
        stop = threading.Event()
        sentinel = object()
        platform = str(download["game"]["platform"])

        def worker() -> None:
            writer = _QueueWriter(output, stop)
            try:
                with zipfile.ZipFile(
                    writer, mode="w", compression=zipfile.ZIP_STORED, allowZip64=True
                ) as archive:
                    for path, relpath, size in download["paths"]:
                        if stop.is_set():
                            break
                        relative = PurePosixPath(relpath)
                        if relative.parts and relative.parts[0] == platform:
                            relative = PurePosixPath(*relative.parts[1:])
                        with path.open("rb") as source, archive.open(
                            relative.as_posix(), "w", force_zip64=size >= 2 * 1024**3
                        ) as target:
                            while not stop.is_set():
                                chunk = source.read(1024 * 1024)
                                if not chunk:
                                    break
                                target.write(chunk)
            except BrokenPipeError:
                pass
            except Exception as exc:
                if not stop.is_set():
                    output.put(exc)
            finally:
                if not stop.is_set():
                    output.put(sentinel)

        thread = threading.Thread(target=worker, name="rommates-download", daemon=True)
        thread.start()
        try:
            while True:
                item = output.get()
                if item is sentinel:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            stop.set()
