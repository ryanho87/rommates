from __future__ import annotations

import json
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field, ValidationError

from .config import Settings
from .db import Database
from .library import JobCancelled, LibraryError, LibraryService
from .mcp_server import RommatesMCPService, create_mcp_server
from .naming import NamingService
from .rankings import RankingService
from .saves import SaveSnapshotService
from .screenscraper import ScreenScraperService
from .transfers import MAX_MANIFEST_BYTES, TransferError, TransferService


MINIMUM_TOKEN_LENGTH = 16
CANCELLABLE_JOB_KINDS = frozenset({"scan", "device_apply", "save_snapshot", "save_restore", "save_delete", "artwork_scrape", "artwork_bulk", "rating_scrape", "ranking_refresh", "upload_finalize"})

settings = Settings.from_env()


def migrate_legacy_path(configured: Path, new_default: Path, legacy: Path) -> bool:
    if configured != new_default or configured.exists() or not legacy.exists():
        return False
    configured.parent.mkdir(parents=True, exist_ok=True)
    legacy.replace(configured)
    if configured.suffix == ".db":
        for suffix in ("-wal", "-shm"):
            legacy_sidecar = Path(f"{legacy}{suffix}")
            if legacy_sidecar.exists():
                legacy_sidecar.replace(Path(f"{configured}{suffix}"))
    return True


def migrate_legacy_storage() -> None:
    """Move default ROM Manager storage to ROMmates without losing deployed state."""
    migrations = [
        (settings.database_path, Path("/data/rommates.db"), Path("/data/rommanager.db")),
        (settings.trash_root, Path("/emulation/.rommates-trash"), Path("/emulation/.rommanager-trash")),
    ]
    for configured, new_default, legacy in migrations:
        migrate_legacy_path(configured, new_default, legacy)


migrate_legacy_storage()
db = Database(settings.database_path)
library = LibraryService(settings, db)
saves = SaveSnapshotService(settings, db)
naming = NamingService(db, settings.library_root, library, saves)
screenscraper = ScreenScraperService(settings, db)
ranking_service = RankingService(settings, db)
transfers = TransferService(settings, db, library)
job_cancellations: dict[int, threading.Event] = {}
job_cancellations_lock = threading.Lock()
library_job_lock = threading.Lock()
job_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="rommates-job")
LIBRARY_JOB_KINDS = frozenset(
    {"scan", "rename", "bulk_rename", "delete", "bulk_delete", "device_apply", "restore", "purge", "bulk_purge", "upload_finalize"}
)


@contextmanager
def job_execution_slot(kind: str, cancellation: threading.Event):
    """Serialize catalog/filesystem jobs while leaving their status queued."""
    if kind not in LIBRARY_JOB_KINDS:
        yield
        return
    acquired = False
    try:
        while not acquired:
            if cancellation.is_set():
                raise JobCancelled("Stopped before the job started")
            acquired = library_job_lock.acquire(timeout=0.25)
        if cancellation.is_set():
            raise JobCancelled("Stopped before the job started")
        yield
    finally:
        if acquired:
            library_job_lock.release()


def job_payload(row) -> dict[str, object]:
    payload = dict(row)
    result_json = payload.get("result_json")
    progress_json = payload.pop("progress_json", None)
    try:
        result = json.loads(result_json) if result_json else None
    except (TypeError, json.JSONDecodeError):
        result = None
    captured = int(payload.get("issue_count") or 0)
    reported = captured
    if isinstance(result, dict):
        reported = max(reported, int(result.get("skipped_count") or 0))
    try:
        telemetry = json.loads(progress_json) if progress_json else None
    except (TypeError, json.JSONDecodeError):
        telemetry = None
    payload["telemetry"] = telemetry if isinstance(telemetry, dict) else None
    payload["issue_count"] = captured
    payload["reported_issue_count"] = reported
    payload["cancellable"] = payload["status"] == "queued" or (
        payload["kind"] in CANCELLABLE_JOB_KINDS
        and payload["status"] in {"running", "paused", "cancelling"}
    )
    return payload


def job_result_detail(kind: str, result: object, fallback: str) -> str:
    if not isinstance(result, dict):
        return fallback
    if kind == "scan":
        summary = f"Indexed {result.get('games', 0)} games across {result.get('platforms', 0)} platforms"
        if result.get("metadata_files"):
            summary += (
                f", used metadata for {result['metadata_files']} files in "
                f"{result.get('metadata_games', 0)} large or folder-based games"
            )
        if result.get("skipped_count"):
            summary += f", skipped {result['skipped_count']} unreadable files"
        if result.get("removed_devices"):
            summary += f", removed device {', '.join(result['removed_devices'])}"
        return summary
    if kind == "rename":
        return f"Renamed {result.get('old_name', 'game')} to {result.get('new_name', 'game')}"
    if kind == "bulk_rename":
        return f"Applied {result.get('renamed', 0)} naming suggestions"
    if kind == "bulk_delete":
        return (
            f"Kept one copy in {result.get('groups', 0)} duplicate groups and moved "
            f"{result.get('trashed', 0)} bundles to trash"
        )
    if kind == "device_apply":
        return (
            f"Linked {result.get('linked', 0)}, converted {result.get('converted', 0)}, "
            f"copied {result.get('copied', 0)}, removed {result.get('removed', 0)}, "
            f"left {result.get('unchanged', 0)} unchanged"
        )
    if kind == "save_snapshot":
        if result.get("unchanged"):
            return f"Save files unchanged; retained snapshot #{result.get('snapshot_id')}"
        return (
            f"Created save snapshot #{result.get('snapshot_id')}: "
            f"{result.get('files', 0)} files, {result.get('added', 0)} added, "
            f"{result.get('changed', 0)} changed, {result.get('removed', 0)} removed"
        )
    if kind == "save_restore":
        return (
            f"Restored save snapshot #{result.get('snapshot_id')}: {result.get('files', 0)} files; "
            f"safety snapshot #{result.get('safety_snapshot_id')}"
        )
    if kind == "save_delete":
        return (
            f"Deleted {result.get('files', 0)} orphan save files for {result.get('group', 'save group')}; "
            f"safety snapshot #{result.get('safety_snapshot_id')}"
        )
    if kind == "artwork_scrape":
        return (
            f"Matched {result.get('matched', 0)} games and downloaded "
            f"{result.get('downloaded', 0)} visual assets; skipped {result.get('skipped', 0)}"
        )
    if kind == "artwork_bulk":
        mode = "covers" if result.get("asset_mode") == "cover" else "complete artwork sets"
        return (
            f"Bulk {mode}: matched {result.get('matched', 0)} games and downloaded "
            f"{result.get('downloaded', 0)} visual assets; skipped {result.get('skipped', 0)}"
        )
    if kind == "rating_scrape":
        return (
            f"Matched ratings for {result.get('matched', 0)} games; "
            f"{result.get('skipped', 0)} skipped"
        )
    if kind == "ranking_refresh":
        return f"Cached RAWG's top {result.get('games', 0)} games for {result.get('platform', 'platform')}"
    if kind == "upload_finalize":
        detail = f"Added {result.get('destination', 'uploaded bundle')}"
        if result.get("scan_error"):
            detail += "; upload is safe but indexing needs a manual scan"
        return detail
    if kind == "bulk_purge":
        return f"Permanently deleted {result.get('purged', 0)} trashed bundles"
    if kind == "restore":
        return f"Restored {result.get('restored', 'trash item')}"
    if kind == "purge":
        return f"Permanently deleted {result.get('purged', 'trash item')}"
    return fallback


def run_job(
    job_id: int,
    kind: str,
    detail: str,
    operation,
    cancellation: threading.Event,
    *args,
) -> None:
    job_issues: list[str] = []

    def persist_issues() -> None:
        if not job_issues:
            return
        with db.write() as connection:
            connection.executemany(
                "INSERT OR IGNORE INTO job_issues(job_id,detail) VALUES(?,?)",
                ((job_id, issue) for issue in job_issues),
            )

    try:
        last_progress_update = 0.0

        def check_cancelled() -> None:
            if cancellation.is_set():
                raise JobCancelled("Stopped by user")

        def report_progress(
            progress: int,
            progress_detail: str,
            status: str = "running",
            telemetry: dict[str, object] | None = None,
        ) -> None:
            nonlocal last_progress_update
            now = time.monotonic()
            progress = max(0, min(int(progress), 99))
            # Hashing reports every MiB so a huge image can show movement. Keep those
            # callbacks cheap by committing UI state at most twice per second.
            if (
                status == "running"
                and progress not in {0, 99}
                and now - last_progress_update < 0.5
                and not (telemetry and telemetry.get("final"))
            ):
                return
            with db.write() as connection:
                connection.execute(
                    "UPDATE jobs SET status=?,progress=?,detail=?,progress_json=COALESCE(?,progress_json) WHERE id=?",
                    (
                        status if status in {"running", "paused"} else "running",
                        progress,
                        progress_detail,
                        json.dumps(telemetry, separators=(",", ":")) if telemetry else None,
                        job_id,
                    ),
                )
            last_progress_update = now

        report_progress.supports_telemetry = True

        with job_execution_slot(kind, cancellation):
            check_cancelled()
            with db.write() as connection:
                connection.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
            if kind == "scan":
                result = operation(
                    *args,
                    progress_callback=report_progress,
                    cancel_check=check_cancelled,
                    issue_callback=job_issues.append,
                )
            elif kind == "device_apply":
                result = operation(*args, cancel_check=check_cancelled)
            elif kind in {"save_snapshot", "save_restore", "artwork_scrape", "artwork_bulk", "rating_scrape", "ranking_refresh", "upload_finalize"}:
                result = operation(
                    *args, progress_callback=report_progress, cancel_check=check_cancelled
                )
            else:
                result = operation(*args)
            if kind in {"artwork_scrape", "artwork_bulk"} and isinstance(result, dict):
                job_issues.extend(str(issue) for issue in result.get("issues", []))
            # Cooperative operations check cancellation before their final commit. A
            # stop request arriving after the operation returns must not relabel a
            # successfully committed filesystem change as cancelled.
            persist_issues()
            with db.write() as connection:
                connection.execute(
                    "UPDATE jobs SET status='complete',progress=100,detail=?,result_json=?,completed_at=CURRENT_TIMESTAMP WHERE id=?",
                    (job_result_detail(kind, result, detail), json.dumps(result), job_id),
                )
    except JobCancelled as exc:
        persist_issues()
        with db.write() as connection:
            connection.execute(
                "UPDATE jobs SET status='cancelled',detail=?,completed_at=CURRENT_TIMESTAMP WHERE id=?",
                (str(exc), job_id),
            )
    except Exception as exc:
        persist_issues()
        with db.write() as connection:
            connection.execute(
                "UPDATE jobs SET status='failed',detail=?,completed_at=CURRENT_TIMESTAMP WHERE id=?",
                (str(exc), job_id),
            )
    finally:
        # Also covers a failure raised while constructing the terminal job result.
        persist_issues()
        with job_cancellations_lock:
            job_cancellations.pop(job_id, None)


def enqueue_job(kind: str, detail: str, operation, *args, coalesce: bool = False) -> int:
    with db.write() as connection:
        if coalesce:
            active = connection.execute(
                "SELECT id FROM jobs WHERE kind=? AND detail=? "
                "AND status IN ('queued','running','paused','cancelling') ORDER BY id LIMIT 1",
                (kind, detail),
            ).fetchone()
            if active:
                return active["id"]
        active_count = connection.execute(
            "SELECT COUNT(*) AS count FROM jobs WHERE status IN ('queued','running','paused','cancelling')"
        ).fetchone()["count"]
        if active_count >= 25:
            raise LibraryError("Too many jobs are already queued; wait for one to finish")
        connection.execute(
            "INSERT INTO jobs(kind,status,detail) VALUES(?,'queued',?)",
            (kind, detail),
        )
        job_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    cancellation = threading.Event()
    with job_cancellations_lock:
        job_cancellations[job_id] = cancellation
    db.prune_history()
    job_executor.submit(run_job, job_id, kind, detail, operation, cancellation, *args)
    return job_id


def queue_scan_job() -> dict[str, object]:
    """Queue a protected scan without allowing an MCP caller to override prune safety."""
    with db.write() as connection:
        active = connection.execute(
            "SELECT id FROM jobs WHERE kind='scan' AND status IN ('queued','running','cancelling') "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if active:
            return {"job_id": active["id"], "already_running": True}
    job_id = enqueue_job("scan", "Indexing library", library.scan, False)
    return {"job_id": job_id, "already_running": False}


def queue_device_apply_job(device_id: int) -> dict[str, object]:
    with db.connect() as connection:
        if not connection.execute("SELECT 1 FROM devices WHERE id=?", (device_id,)).fetchone():
            raise LibraryError("Device was not found")
    job_id = enqueue_job(
        "device_apply", f"Applying device {device_id}", library.apply_device, device_id
    )
    return {"job_id": job_id}


def queue_reviewed_device_apply_job(device_id: int, preview_token: str) -> dict[str, object]:
    with db.connect() as connection:
        if not connection.execute("SELECT 1 FROM devices WHERE id=?", (device_id,)).fetchone():
            raise LibraryError("Device was not found")
    job_id = enqueue_job(
        "device_apply",
        f"Applying reviewed MCP plan for device {device_id}",
        mcp_service.execute_reviewed_device_apply,
        device_id,
        preview_token,
    )
    return {"job_id": job_id}


def request_job_cancel(job_id: int) -> dict[str, object]:
    with db.connect() as connection:
        row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise LibraryError("Job was not found")
    if row["status"] in {"complete", "failed", "cancelled"}:
        return {"job_id": job_id, "status": row["status"], "already_finished": True}
    if row["status"] != "queued" and row["kind"] not in CANCELLABLE_JOB_KINDS:
        raise LibraryError("This filesystem operation is too short or atomic to stop safely")
    with job_cancellations_lock:
        cancellation = job_cancellations.get(job_id)
        if cancellation:
            cancellation.set()
    if not cancellation:
        raise LibraryError("The job is no longer running")
    with db.write() as connection:
        connection.execute(
            "UPDATE jobs SET status='cancelling',detail='Stopping safely at the next checkpoint' "
            "WHERE id=? AND status IN ('queued','running','paused')",
            (job_id,),
        )
    return {"job_id": job_id, "status": "cancelling", "already_finished": False}


mcp_service = RommatesMCPService(
    db,
    library,
    queue_scan_job,
    queue_reviewed_device_apply_job,
    request_job_cancel,
)
mcp_server = create_mcp_server(mcp_service)
mcp_http_app = mcp_server.streamable_http_app(
    streamable_http_path="/",
    stateless_http=True,
    json_response=True,
    # ROMmates already authenticates every MCP request and rejects cross-origin
    # writes. Traefik owns the public Host header, so a second static host allowlist
    # would make custom domains needlessly brittle.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


def save_scheduler(stop: threading.Event) -> None:
    while not stop.wait(30):
        try:
            if not saves.due_for_automatic_snapshot():
                continue
            with db.connect() as connection:
                active = connection.execute(
                    "SELECT 1 FROM jobs WHERE kind IN ('save_snapshot','save_restore') "
                    "AND status IN ('queued','running','cancelling') LIMIT 1"
                ).fetchone()
            if not active:
                enqueue_job(
                    "save_snapshot",
                    "Creating scheduled save snapshot",
                    saves.create_snapshot,
                    "automatic",
                    "",
                    False,
                )
        except Exception as exc:
            db.activity("save_snapshot", f"Scheduled snapshot could not start: {exc}")


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.initialize()
    with db.write() as connection:
        connection.execute(
            "UPDATE jobs SET status='failed',detail='Interrupted by application restart',completed_at=CURRENT_TIMESTAMP "
            "WHERE status IN ('queued','running','paused','cancelling')"
        )
    # Validated unconditionally: an unset token disables authentication entirely, so
    # this must never depend on an unrelated flag or on which launcher started the app.
    if not settings.allow_anonymous and len(settings.access_token) < MINIMUM_TOKEN_LENGTH:
        raise LibraryError(
            f"ROMMATES_ACCESS_TOKEN must contain at least {MINIMUM_TOKEN_LENGTH} characters. "
            "Generate one with 'openssl rand -hex 32', or set ROMMATES_ALLOW_ANONYMOUS=true "
            "if this instance is already protected by an authenticated reverse proxy."
        )
    library.prepare_roots()
    saves.initialize()
    transfers.initialize()
    scheduler_stop = threading.Event()
    scheduler_thread = threading.Thread(target=save_scheduler, args=(scheduler_stop,), daemon=True)
    scheduler_thread.start()
    if screenscraper.configured:
        for run_id in screenscraper.resumable_bulk_runs():
            job_id = enqueue_job(
                "artwork_bulk",
                f"Resuming bulk artwork run #{run_id}",
                screenscraper.scrape_bulk,
                run_id,
            )
            screenscraper.attach_bulk_job(run_id, job_id)
    if settings.scan_on_start:
        enqueue_job("scan", "Indexing library", library.scan)
    try:
        async with mcp_server.session_manager.run():
            yield
    finally:
        scheduler_stop.set()
        scheduler_thread.join(timeout=2)


app = FastAPI(
    title="ROMmates",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
app.mount("/mcp", mcp_http_app, name="mcp")


class RenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class SelectionRequest(BaseModel):
    game_id: int
    selected: bool


class BulkSelectionRequest(BaseModel):
    game_ids: list[int] = Field(max_length=1000)
    selected: bool


class DeviceDeploymentModeRequest(BaseModel):
    mode: str = Field(pattern="^(copy|hardlink)$")


class DatImportRequest(BaseModel):
    source_name: str = Field(min_length=1, max_length=255)
    platform: str = Field(min_length=1, max_length=100)
    content: str


class NamingRenameItem(BaseModel):
    game_id: int
    name: str = Field(min_length=1, max_length=255)


class BulkRenameRequest(BaseModel):
    items: list[NamingRenameItem] = Field(min_length=1, max_length=500)


class DuplicateKeeperDecision(BaseModel):
    kind: str = Field(pattern="^(exact|possible)$")
    group_key: str = Field(min_length=1, max_length=1000)
    keeper_id: int = Field(gt=0)


class BulkDuplicateTrashRequest(BaseModel):
    items: list[DuplicateKeeperDecision] = Field(min_length=1, max_length=500)


class SaveSnapshotRequest(BaseModel):
    note: str = Field(default="", max_length=500)


class SaveRestoreRequest(BaseModel):
    expected_tree_hash: str = Field(min_length=64, max_length=64, pattern="^[a-f0-9]{64}$")
    retroarch_closed: bool


class SaveSettingsRequest(BaseModel):
    enabled: bool
    interval_minutes: int = Field(ge=0, le=10080)
    retention_recent: int = Field(ge=1, le=1000)
    retention_daily: int = Field(ge=0, le=3650)
    retention_weekly: int = Field(ge=0, le=520)
    retention_monthly: int = Field(ge=0, le=240)


class SavePinRequest(BaseModel):
    pinned: bool


class SaveImpactRequest(BaseModel):
    game_ids: list[int] = Field(min_length=1, max_length=500)


class SaveOrphanDeleteRequest(BaseModel):
    group_key: str = Field(min_length=1, max_length=1000)


class ArtworkScrapeRequest(BaseModel):
    game_ids: list[int] = Field(min_length=1, max_length=500)
    missing_only: bool = True


class ArtworkBulkRequest(BaseModel):
    asset_mode: str = Field(default="cover", pattern="^(cover|full)$")


class RatingScrapeRequest(BaseModel):
    platform: str = Field(min_length=1, max_length=100)
    search: str = Field(default="", max_length=255)


class UploadFileSpec(BaseModel):
    relative_path: str = Field(min_length=1, max_length=1024)
    size: int = Field(ge=0)


class UploadCreateRequest(BaseModel):
    platform: str = Field(min_length=1, max_length=100)
    bundle_name: str = Field(default="", max_length=255)
    folder_mode: bool = False
    files: list[UploadFileSpec] = Field(min_length=1, max_length=20_000)


class BulkPurgeRequest(BaseModel):
    trash_ids: list[int] = Field(min_length=1, max_length=1000)


@app.middleware("http")
async def protect_private_api(request: Request, call_next):
    public_download = (
        request.url.path.startswith("/api/downloads/")
        and request.method in {"GET", "HEAD"}
    )
    private_path = request.url.path.startswith("/api/") or request.url.path.startswith("/mcp")
    if private_path and request.url.path != "/api/health" and not public_download:
        if settings.access_token:
            authorization = request.headers.get("authorization", "")
            expected = f"Bearer {settings.access_token}"
            if not secrets.compare_digest(authorization, expected):
                return JSONResponse(status_code=401, content={"detail": "Access token is required"})
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
                return JSONResponse(status_code=403, content={"detail": "Cross-site requests are not allowed"})
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
                return JSONResponse(status_code=403, content={"detail": "Request origin is not allowed"})
    return await call_next(request)


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' blob: data:; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'; object-src 'none'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if request.url.path.startswith(("/api/", "/mcp")):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(LibraryError)
async def library_error_handler(_: Request, exc: LibraryError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(TransferError)
async def transfer_error_handler(_: Request, exc: TransferError):
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


@app.get("/", include_in_schema=False)
@app.get("/library", include_in_schema=False)
@app.get("/transfers", include_in_schema=False)
@app.get("/duplicates", include_in_schema=False)
@app.get("/naming", include_in_schema=False)
@app.get("/devices", include_in_schema=False)
@app.get("/saves", include_in_schema=False)
@app.get("/jobs", include_in_schema=False)
@app.get("/trash", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.get("/api/status")
def status():
    with db.connect() as connection:
        counts = connection.execute(
            "SELECT COUNT(*) AS games,COUNT(DISTINCT platform) AS platforms,COALESCE(SUM(size),0) AS bytes FROM games"
        ).fetchone()
        devices = connection.execute("SELECT COUNT(*) AS count FROM devices").fetchone()["count"]
        trash = connection.execute("SELECT COUNT(*) AS count FROM trash_items").fetchone()["count"]
        duplicates = connection.execute(
            "SELECT COALESCE(SUM(item_count),0) AS count FROM "
            "(SELECT COUNT(*) AS item_count FROM games GROUP BY bundle_hash HAVING COUNT(*)>1)"
        ).fetchone()["count"]
        current_job = connection.execute(
            "SELECT * FROM jobs ORDER BY id DESC LIMIT 1"
        ).fetchone()
        save_snapshots = connection.execute(
            "SELECT COUNT(*) AS count FROM save_snapshots"
        ).fetchone()["count"]
    return {
        **dict(counts),
        "devices": devices,
        "trash": trash,
        "duplicates": duplicates,
        "save_snapshots": save_snapshots,
        "job": job_payload(current_job) if current_job else None,
        "roots": {
            "library": str(settings.library_root),
            "devices": str(settings.devices_root),
            "trash": str(settings.trash_root),
            "saves": str(settings.saves_root),
            "snapshots": str(settings.snapshots_root),
            "media": str(settings.media_root),
        },
        "screenscraper": screenscraper.status(),
        "rawg": {"configured": ranking_service.configured},
    }


@app.get("/api/dashboard")
def dashboard():
    with db.connect() as connection:
        collection = dict(connection.execute(
            "SELECT COUNT(*) AS games,COUNT(DISTINCT platform) AS platforms,"
            "COALESCE(SUM(size),0) AS bytes,(SELECT COUNT(*) FROM game_files) AS files FROM games"
        ).fetchone())
        platform_rows = connection.execute(
            "SELECT platform,COUNT(*) AS games,COALESCE(SUM(size),0) AS bytes "
            "FROM games GROUP BY platform ORDER BY games DESC,platform COLLATE NOCASE"
        ).fetchall()
        duplicate_summary = dict(connection.execute(
            "SELECT COUNT(*) AS groups,COALESCE(SUM(copies-1),0) AS extra_copies,"
            "COALESCE(SUM(total_bytes-keep_bytes),0) AS reclaimable_bytes FROM ("
            "SELECT COUNT(*) AS copies,SUM(size) AS total_bytes,MAX(size) AS keep_bytes "
            "FROM games GROUP BY bundle_hash HAVING COUNT(*)>1)"
        ).fetchone())
        possible_groups = connection.execute(
            "SELECT COUNT(*) AS count FROM (SELECT platform,normalized_name FROM games "
            "WHERE normalized_name<>'' GROUP BY platform,normalized_name "
            "HAVING COUNT(DISTINCT bundle_hash)>1)"
        ).fetchone()["count"]
        artwork = dict(connection.execute(
            "SELECT COUNT(DISTINCT game_id) AS games,COUNT(*) AS assets,"
            "COALESCE(SUM(size),0) AS bytes,"
            "COUNT(DISTINCT CASE WHEN kind='cover' THEN game_id END) AS covers FROM game_assets"
        ).fetchone())
        devices = [dict(row) for row in connection.execute(
            "SELECT d.id,d.name,"
            "(SELECT COUNT(*) FROM device_selections ds WHERE ds.device_id=d.id) AS selected_games,"
            "(SELECT COUNT(DISTINCT game_id) FROM deployments dp WHERE dp.device_id=d.id) AS deployed_games,"
            "(SELECT COUNT(*) FROM device_selections ds JOIN game_files gf ON gf.game_id=ds.game_id "
            " WHERE ds.device_id=d.id AND NOT EXISTS(SELECT 1 FROM deployments dp WHERE dp.device_id=d.id "
            " AND dp.game_id=ds.game_id AND dp.relpath=gf.device_relpath)) AS additions,"
            "(SELECT COUNT(*) FROM deployments dp WHERE dp.device_id=d.id AND NOT EXISTS("
            " SELECT 1 FROM device_selections ds WHERE ds.device_id=dp.device_id AND ds.game_id=dp.game_id)) AS removals "
            "FROM devices d ORDER BY d.name COLLATE NOCASE"
        )]
        recent_jobs = [job_payload(row) for row in connection.execute(
            "SELECT j.*,(SELECT COUNT(*) FROM job_issues i WHERE i.job_id=j.id) AS issue_count "
            "FROM jobs j ORDER BY j.id DESC LIMIT 5"
        )]
        last_scan_row = connection.execute(
            "SELECT j.*,(SELECT COUNT(*) FROM job_issues i WHERE i.job_id=j.id) AS issue_count "
            "FROM jobs j WHERE kind='scan' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        trash_count = connection.execute("SELECT COUNT(*) AS count FROM trash_items").fetchone()["count"]
        catalog_count = connection.execute("SELECT COUNT(*) AS count FROM naming_catalogs").fetchone()["count"]
        latest_snapshot = connection.execute(
            "SELECT * FROM save_snapshots ORDER BY id DESC LIMIT 1"
        ).fetchone()
        snapshot_count = connection.execute(
            "SELECT COUNT(*) AS count FROM save_snapshots"
        ).fetchone()["count"]
    try:
        save_source = saves.source_summary()
        save_matching = saves.match_summary()
    except LibraryError as exc:
        save_source = {
            "available": False,
            "files": 0,
            "bytes": 0,
            "save_files": 0,
            "state_files": 0,
            "latest_mtime_ns": 0,
            "error": str(exc),
        }
        save_matching = {"groups": 0, "exact": 0, "possible": 0, "ambiguous": 0, "orphan": 0}
    for job in recent_jobs:
        job.pop("result_json", None)
    last_scan = job_payload(last_scan_row) if last_scan_row else None
    if last_scan:
        last_scan.pop("result_json", None)
    return {
        "collection": collection,
        "platforms": [dict(row) for row in platform_rows],
        "cleanup": {
            **duplicate_summary,
            "possible_groups": possible_groups,
            "trash": trash_count,
            "naming_catalogs": catalog_count,
        },
        "artwork": artwork,
        "screenscraper_configured": screenscraper.configured,
        "devices": devices,
        "saves": {
            **save_source,
            "matching": save_matching,
            "snapshots": snapshot_count,
            "latest_snapshot": dict(latest_snapshot) if latest_snapshot else None,
        },
        "last_scan": last_scan,
        "recent_jobs": recent_jobs,
    }


@app.post("/api/scan", status_code=202)
def start_scan(confirm_prune: bool = False):
    if not confirm_prune:
        return queue_scan_job()
    with db.write() as connection:
        active = connection.execute(
            "SELECT id FROM jobs WHERE kind='scan' AND status IN ('queued','running','cancelling') ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if active:
            return {"job_id": active["id"], "already_running": True}
    job_id = enqueue_job("scan", "Indexing library", library.scan, confirm_prune)
    return {"job_id": job_id, "already_running": False}


@app.get("/api/platforms")
def platforms():
    with db.connect() as connection:
        rows = connection.execute(
            "SELECT g.platform,COUNT(*) AS count,COUNT(gm.rating) AS rated_count "
            "FROM games g LEFT JOIN game_metadata gm ON gm.game_id=g.id "
            "GROUP BY g.platform ORDER BY g.platform COLLATE NOCASE"
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/games")
def games(
    search: str = "",
    platform: str = "",
    duplicate: str = Query("all", pattern="^(all|exact|possible|unique)$"),
    device_id: int | None = None,
    device_scope: str = Query("all", pattern="^(all|on_device|changes)$"),
    sort: str = Query(
        "name_asc", pattern="^(name_asc|name_desc|rating_desc|rating_asc|size_desc|size_asc)$"
    ),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    if device_scope != "all" and device_id is None:
        raise HTTPException(status_code=400, detail="A device is required for this view")
    present_relpaths = library.device_inventory(device_id, refresh=True) if device_id is not None else set()
    where = ["1=1"]
    params: list[object] = []
    if search.strip():
        where.append("(g.display_name LIKE ? ESCAPE '\\' OR g.primary_relpath LIKE ? ESCAPE '\\')")
        escaped = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params.extend([f"%{escaped}%", f"%{escaped}%"])
    if platform:
        where.append("g.platform=?")
        params.append(platform)
    status_expr = (
        "CASE WHEN (SELECT COUNT(*) FROM games x WHERE x.bundle_hash=g.bundle_hash)>1 THEN 'exact' "
        "WHEN g.normalized_name<>'' AND (SELECT COUNT(*) FROM games x WHERE x.platform=g.platform "
        "AND x.normalized_name=g.normalized_name)>1 THEN 'possible' ELSE 'unique' END"
    )
    if duplicate != "all":
        where.append(f"({status_expr})=?")
        params.append(duplicate)
    selected_expr = "0"
    present_expr = "0"
    managed_expr = "0"
    synced_expr = "0"
    if device_id is not None:
        selected_expr = "EXISTS(SELECT 1 FROM device_selected ds WHERE ds.game_id=g.id)"
        present_expr = (
            "EXISTS(SELECT 1 FROM game_files dgf JOIN device_present df ON df.relpath=dgf.device_relpath "
            "WHERE dgf.game_id=g.id)"
        )
        managed_expr = "EXISTS(SELECT 1 FROM device_managed dm WHERE dm.game_id=g.id)"
        synced_expr = (
            "((SELECT COUNT(*) FROM device_managed dm WHERE dm.game_id=g.id)="
            "(SELECT COUNT(*) FROM game_files sgf WHERE sgf.game_id=g.id) AND "
            "(SELECT COUNT(*) FROM game_files sgf WHERE sgf.game_id=g.id)>0)"
        )
        params_for_select: list[object] = []
        if device_scope == "on_device":
            where.append(f"({present_expr})")
        elif device_scope == "changes":
            where.append(f"(({selected_expr}) != ({synced_expr}))")
    else:
        params_for_select = []
    where_sql = " AND ".join(where)
    order_sql = {
        "name_asc": "g.display_name COLLATE NOCASE ASC,g.platform ASC,g.id ASC",
        "name_desc": "g.display_name COLLATE NOCASE DESC,g.platform ASC,g.id ASC",
        "rating_desc": "gm.rating IS NULL ASC,gm.rating DESC,g.display_name COLLATE NOCASE ASC,g.id ASC",
        "rating_asc": "gm.rating IS NULL ASC,gm.rating ASC,g.display_name COLLATE NOCASE ASC,g.id ASC",
        "size_desc": "g.size DESC,g.display_name COLLATE NOCASE ASC,g.id ASC",
        "size_asc": "g.size ASC,g.display_name COLLATE NOCASE ASC,g.id ASC",
    }[sort]
    platform_rank_expr = (
        "CASE WHEN gm.rating IS NULL THEN NULL ELSE 1+(SELECT COUNT(*) FROM game_metadata rm "
        "JOIN games rg ON rg.id=rm.game_id WHERE rg.platform=g.platform AND rm.rating>gm.rating) END"
    )
    actual_devices: list[dict[str, object]] = []
    page_files: dict[int, list[str]] = {}
    with db.connect() as connection:
        if device_id is not None:
            connection.execute("CREATE TEMP TABLE device_present(relpath TEXT PRIMARY KEY)")
            connection.execute("CREATE TEMP TABLE device_selected(game_id INTEGER PRIMARY KEY)")
            connection.execute("CREATE TEMP TABLE device_managed(game_id INTEGER,relpath TEXT,PRIMARY KEY(game_id,relpath))")
            connection.executemany(
                "INSERT OR IGNORE INTO device_present(relpath) VALUES(?)",
                ((relpath,) for relpath in present_relpaths),
            )
            connection.execute(
                "INSERT INTO device_selected(game_id) SELECT game_id FROM device_selections WHERE device_id=?",
                (device_id,),
            )
            connection.execute(
                "INSERT INTO device_managed(game_id,relpath) SELECT game_id,relpath FROM deployments WHERE device_id=?",
                (device_id,),
            )
        total = connection.execute(f"SELECT COUNT(*) AS count FROM games g WHERE {where_sql}", params).fetchone()["count"]
        rows = connection.execute(
            f"SELECT g.*,({status_expr}) AS duplicate_status,"
            f"(SELECT COUNT(*) FROM game_files gf WHERE gf.game_id=g.id) AS file_count,"
            f"(SELECT COUNT(*) FROM device_selections ds WHERE ds.game_id=g.id) AS device_count,"
            f"(SELECT id FROM game_assets ga WHERE ga.game_id=g.id AND ga.kind='cover' LIMIT 1) AS cover_asset_id,"
            f"(SELECT COUNT(*) FROM game_assets ga WHERE ga.game_id=g.id) AS artwork_count,"
            f"gm.rating AS rating,gm.top_staff AS top_staff,({platform_rank_expr}) AS platform_rank,"
            f"({selected_expr}) AS selected,({present_expr}) AS on_device,"
            f"({managed_expr}) AS managed,({synced_expr}) AS synced FROM games g "
            f"LEFT JOIN game_metadata gm ON gm.game_id=g.id WHERE {where_sql} "
            f"ORDER BY {order_sql} LIMIT ? OFFSET ?",
            params_for_select + params + [limit, offset],
        ).fetchall()
        items = [dict(row) for row in rows]
        if items:
            game_ids = [item["id"] for item in items]
            placeholders = ",".join("?" for _ in game_ids)
            selected_states = connection.execute(
                f"SELECT ds.game_id,d.id AS device_id,d.name,"
                "CASE WHEN (SELECT COUNT(*) FROM deployments dp WHERE dp.device_id=ds.device_id AND dp.game_id=ds.game_id) "
                "= (SELECT COUNT(*) FROM game_files gf WHERE gf.game_id=ds.game_id) "
                "AND (SELECT COUNT(*) FROM game_files gf WHERE gf.game_id=ds.game_id)>0 "
                "THEN 'synced' ELSE 'pending_add' END AS state "
                "FROM device_selections ds JOIN devices d ON d.id=ds.device_id "
                f"WHERE ds.game_id IN ({placeholders}) ORDER BY d.name COLLATE NOCASE",
                game_ids,
            ).fetchall()
            removal_states = connection.execute(
                f"SELECT DISTINCT dp.game_id,d.id AS device_id,d.name,'pending_remove' AS state "
                "FROM deployments dp JOIN devices d ON d.id=dp.device_id "
                "WHERE NOT EXISTS(SELECT 1 FROM device_selections ds WHERE ds.device_id=dp.device_id AND ds.game_id=dp.game_id) "
                f"AND dp.game_id IN ({placeholders}) ORDER BY d.name COLLATE NOCASE",
                game_ids,
            ).fetchall()
            by_game: dict[int, list[dict[str, object]]] = {game_id: [] for game_id in game_ids}
            for device_state in [*selected_states, *removal_states]:
                by_game[device_state["game_id"]].append(
                    {
                        "id": device_state["device_id"],
                        "name": device_state["name"],
                        "state": device_state["state"],
                    }
                )
            for item in items:
                item["devices"] = by_game[item["id"]]
                if device_id is not None:
                    item["device_state"] = (
                        "on_device" if item["on_device"] and item["selected"] and item["synced"]
                        else "pending_update" if item["on_device"] and item["selected"]
                        else "pending_remove" if item["on_device"] and item["managed"]
                        else "unmanaged" if item["on_device"]
                        else "pending_add" if item["selected"]
                        else "available"
                    )
            if device_id is None:
                actual_devices = [dict(row) for row in connection.execute(
                    "SELECT id,name FROM devices ORDER BY name COLLATE NOCASE"
                )]
                page_files = {game_id: [] for game_id in game_ids}
                for row in connection.execute(
                    f"SELECT game_id,device_relpath AS relpath FROM game_files WHERE game_id IN ({placeholders})",
                    game_ids,
                ):
                    page_files[row["game_id"]].append(row["relpath"])
        device_inventory = None
        if device_id is not None:
            scope_expr = {
                "on_device": present_expr,
                "changes": f"({selected_expr}) != ({synced_expr})",
                "all": "1=1",
            }[device_scope]
            present_games = connection.execute(
                f"SELECT COUNT(*) AS count FROM games g WHERE {present_expr}"
            ).fetchone()["count"]
            changes = connection.execute(
                f"SELECT COUNT(*) AS count FROM games g WHERE ({selected_expr}) != ({synced_expr})"
            ).fetchone()["count"]
            unmatched_files = connection.execute(
                "SELECT COUNT(*) AS count FROM device_present df WHERE NOT EXISTS("
                "SELECT 1 FROM game_files gf WHERE gf.device_relpath=df.relpath)"
            ).fetchone()["count"]
            device_inventory = {
                "present_games": present_games,
                "changes": changes,
                "unmatched_files": unmatched_files,
                "files": len(present_relpaths),
                "platforms": [
                    dict(row)
                    for row in connection.execute(
                        f"SELECT g.platform,COUNT(*) AS count FROM games g "
                        f"WHERE {scope_expr} GROUP BY g.platform "
                        "ORDER BY g.platform COLLATE NOCASE"
                    )
                ],
            }
    if items and device_id is None and actual_devices:
        inventories = {
            int(device["id"]): library.device_inventory(int(device["id"]), refresh=offset == 0)
            for device in actual_devices
        }
        for item in items:
            relpaths = page_files[item["id"]]
            states_by_device = {device["id"]: device for device in item["devices"]}
            for device in actual_devices:
                device_key = int(device["id"])
                if not any(relpath in inventories[device_key] for relpath in relpaths):
                    continue
                existing = states_by_device.get(device_key)
                if existing:
                    if existing["state"] == "pending_add":
                        existing["state"] = "present"
                else:
                    present = {
                        "id": device_key,
                        "name": device["name"],
                        "state": "present",
                    }
                    item["devices"].append(present)
                    states_by_device[device_key] = present
            item["devices"].sort(key=lambda device: str(device["name"]).casefold())
            item["device_count"] = len(item["devices"])
    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "device_inventory": device_inventory,
    }


@app.get("/api/duplicates")
def duplicate_groups(
    kind: str = Query("exact", pattern="^(exact|possible)$"),
    search: str = "",
    platform: str = "",
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
):
    """Return complete duplicate sets, paginated by group instead of game row."""
    status_expr = (
        "CASE WHEN (SELECT COUNT(*) FROM games x WHERE x.bundle_hash=g.bundle_hash)>1 THEN 'exact' "
        "WHEN g.normalized_name<>'' AND (SELECT COUNT(*) FROM games x WHERE x.platform=g.platform "
        "AND x.normalized_name=g.normalized_name)>1 THEN 'possible' ELSE 'unique' END"
    )
    group_expr = "g.bundle_hash" if kind == "exact" else "g.platform || char(31) || g.normalized_name"
    if kind == "exact":
        where = ["(SELECT COUNT(*) FROM games x WHERE x.bundle_hash=g.bundle_hash)>1"]
    else:
        # Filename review is useful even when one of the variants also has exact
        # copies. Require at least two distinct contents so exact-only sets do not
        # appear a second time in the possible-duplicate view.
        where = [
            "g.normalized_name<>'' AND (SELECT COUNT(DISTINCT x.bundle_hash) FROM games x "
            "WHERE x.platform=g.platform AND x.normalized_name=g.normalized_name)>1"
        ]
    params: list[object] = []
    if search.strip():
        escaped = search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("(g.display_name LIKE ? ESCAPE '\\' OR g.primary_relpath LIKE ? ESCAPE '\\')")
        params.extend([f"%{escaped}%", f"%{escaped}%"])
    if platform:
        where.append("g.platform=?")
        params.append(platform)
    where_sql = " AND ".join(where)
    grouped_sql = (
        f"SELECT {group_expr} AS group_key,MIN(g.display_name) AS sort_name "
        f"FROM games g WHERE {where_sql} GROUP BY group_key"
    )
    with db.connect() as connection:
        total = connection.execute(
            f"SELECT COUNT(*) AS count FROM ({grouped_sql})", params
        ).fetchone()["count"]
        group_rows = connection.execute(
            f"{grouped_sql} ORDER BY sort_name COLLATE NOCASE,group_key LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        keys = [row["group_key"] for row in group_rows]
        if not keys:
            return {"items": [], "total": total, "limit": limit, "offset": offset}
        placeholders = ",".join("?" for _ in keys)
        item_group_expr = (
            "g.bundle_hash" if kind == "exact" else "g.platform || char(31) || g.normalized_name"
        )
        rows = connection.execute(
            f"SELECT g.*,({item_group_expr}) AS group_key,({status_expr}) AS duplicate_status,"
            "(SELECT COUNT(*) FROM game_files gf WHERE gf.game_id=g.id) AS file_count,"
            "(SELECT COUNT(*) FROM device_selections ds WHERE ds.game_id=g.id) AS device_count "
            f"FROM games g WHERE ({item_group_expr}) IN ({placeholders}) "
            "ORDER BY group_key,g.primary_relpath COLLATE NOCASE",
            keys,
        ).fetchall()
        items = [dict(row) for row in rows]
        game_ids = [item["id"] for item in items]
        game_placeholders = ",".join("?" for _ in game_ids)
        device_rows = connection.execute(
            "SELECT ds.game_id,d.name FROM device_selections ds JOIN devices d ON d.id=ds.device_id "
            f"WHERE ds.game_id IN ({game_placeholders}) ORDER BY d.name COLLATE NOCASE",
            game_ids,
        ).fetchall()
        devices = [dict(row) for row in connection.execute(
            "SELECT id,name FROM devices ORDER BY name COLLATE NOCASE"
        )]
        file_rows = connection.execute(
            f"SELECT game_id,device_relpath AS relpath FROM game_files WHERE game_id IN ({game_placeholders})",
            game_ids,
        ).fetchall()
    devices_by_game: dict[int, list[str]] = {game_id: [] for game_id in game_ids}
    for row in device_rows:
        devices_by_game[row["game_id"]].append(row["name"])
    device_inventories = {
        device["name"]: library.device_inventory(device["id"], refresh=True)
        for device in devices
    }
    files_by_game: dict[int, list[str]] = {game_id: [] for game_id in game_ids}
    for row in file_rows:
        files_by_game[row["game_id"]].append(row["relpath"])
    save_impacts = saves.save_impacts(game_ids)
    items_by_group: dict[str, list[dict[str, object]]] = {key: [] for key in keys}
    for item in items:
        selected_devices = devices_by_game[item["id"]]
        present_devices = [
            name for name, inventory in device_inventories.items()
            if any(relpath in inventory for relpath in files_by_game[item["id"]])
        ]
        item["devices"] = selected_devices
        item["selected_devices"] = selected_devices
        item["present_devices"] = present_devices
        item["save_impact"] = save_impacts.get(
            item["id"],
            {"status": "none", "groups": 0, "files": 0, "save_files": 0, "state_files": 0, "paths": [], "content_names": []},
        )
        items_by_group[item.pop("group_key")].append(item)
    groups = []
    for row in group_rows:
        key = row["group_key"]
        members = items_by_group[key]
        in_use = [
            member for member in members
            if member["present_devices"] or member["selected_devices"]
        ]
        recommended = in_use[0] if len(in_use) == 1 else None
        recommendation_reason = ""
        if recommended:
            if recommended["present_devices"]:
                recommendation_reason = "Already present on " + ", ".join(recommended["present_devices"])
            else:
                recommendation_reason = "Selected for " + ", ".join(recommended["selected_devices"])
        groups.append(
            {
                "key": key,
                "kind": kind,
                "label": row["sort_name"],
                "copies": len(members),
                "bytes": sum(member["size"] for member in members),
                "recommended_keeper_id": recommended["id"] if recommended else None,
                "recommendation_reason": recommendation_reason,
                "device_conflict": len(in_use) > 1,
                "items": members,
            }
        )
    return {"items": groups, "total": total, "limit": limit, "offset": offset}


@app.post("/api/duplicates/trash", status_code=202)
def trash_duplicate_groups(payload: BulkDuplicateTrashRequest):
    decisions = [item.model_dump() for item in payload.items]
    removals = 0
    with db.connect() as connection:
        for item in decisions:
            if item["kind"] == "exact":
                count = connection.execute(
                    "SELECT COUNT(*) AS count FROM games WHERE bundle_hash=?",
                    (item["group_key"],),
                ).fetchone()["count"]
            else:
                if "\x1f" not in item["group_key"]:
                    raise HTTPException(status_code=400, detail="Invalid duplicate group")
                platform, normalized_name = item["group_key"].split("\x1f", 1)
                count = connection.execute(
                    "SELECT COUNT(*) AS count FROM games WHERE platform=? AND normalized_name=?",
                    (platform, normalized_name),
                ).fetchone()["count"]
            removals += max(count - 1, 0)
    job_id = enqueue_job(
        "bulk_delete",
        f"Moving {removals} non-keeper duplicate bundles to trash",
        library.bulk_delete_duplicates,
        decisions,
    )
    return {"job_id": job_id}


@app.get("/api/games/{game_id}")
def game_detail(game_id: int):
    game, files = library.game_bundle(game_id)
    with db.connect() as connection:
        devices = [dict(row) for row in connection.execute(
            "SELECT d.id,d.name,EXISTS(SELECT 1 FROM device_selections ds WHERE ds.device_id=d.id AND ds.game_id=?) AS selected "
            "FROM devices d ORDER BY d.name", (game_id,)
        )]
    impact = saves.save_impacts([game_id]).get(
        game_id,
        {"status": "none", "groups": 0, "files": 0, "save_files": 0, "state_files": 0, "paths": [], "content_names": []},
    )
    return {"game": game, "files": files, "devices": devices, "artwork": screenscraper.detail(game_id), "save_impact": impact}


@app.get("/api/artwork/status")
def artwork_status():
    return screenscraper.status()


@app.get("/api/artwork/bulk")
def artwork_bulk_status():
    return screenscraper.bulk_status()


@app.post("/api/artwork/scrape-all", status_code=202)
def scrape_all_artwork(payload: ArtworkBulkRequest):
    if not screenscraper.configured:
        raise HTTPException(
            status_code=400,
            detail="ScreenScraper developer credentials are not configured",
        )
    run, created = screenscraper.create_bulk_run(payload.asset_mode)
    if not created:
        return {
            "run_id": run["id"],
            "job_id": run.get("job_id"),
            "already_running": True,
            "requested": run["total_games"],
        }
    if run["status"] == "complete":
        return {
            "run_id": run["id"],
            "job_id": None,
            "already_complete": True,
            "requested": 0,
        }
    try:
        job_id = enqueue_job(
            "artwork_bulk",
            f"Downloading missing {'covers' if payload.asset_mode == 'cover' else 'artwork'} for {run['total_games']} games",
            screenscraper.scrape_bulk,
            run["id"],
        )
    except Exception as exc:
        with db.write() as connection:
            connection.execute(
                "UPDATE artwork_bulk_runs SET status='failed',last_error=?,completed_at=CURRENT_TIMESTAMP WHERE id=?",
                (str(exc), run["id"]),
            )
        raise
    screenscraper.attach_bulk_job(run["id"], job_id)
    return {
        "run_id": run["id"],
        "job_id": job_id,
        "already_running": False,
        "requested": run["total_games"],
    }


@app.get("/api/games/{game_id}/artwork")
def game_artwork(game_id: int):
    with db.connect() as connection:
        if not connection.execute("SELECT 1 FROM games WHERE id=?", (game_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Game was not found")
    return screenscraper.detail(game_id)


@app.get("/api/artwork/assets/{asset_id}")
def artwork_asset(asset_id: int):
    path, content_type = screenscraper.asset_path(asset_id)
    return FileResponse(path, media_type=content_type, headers={"Cache-Control": "private, max-age=86400"})


@app.post("/api/artwork/scrape", status_code=202)
def scrape_artwork(payload: ArtworkScrapeRequest):
    if not screenscraper.configured:
        raise HTTPException(
            status_code=400,
            detail="ScreenScraper developer credentials are not configured",
        )
    with db.connect() as connection:
        active = connection.execute(
            "SELECT id FROM jobs WHERE kind IN ('artwork_scrape','artwork_bulk') "
            "AND status IN ('queued','running','paused','cancelling') "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if active:
        return {"job_id": active["id"], "already_running": True}
    job_id = enqueue_job(
        "artwork_scrape",
        f"Scraping artwork for {len(payload.game_ids)} games",
        screenscraper.scrape,
        payload.game_ids,
        payload.missing_only,
    )
    return {"job_id": job_id, "already_running": False}


@app.post("/api/ratings/scrape", status_code=202)
def scrape_ratings(payload: RatingScrapeRequest):
    if not screenscraper.configured:
        raise HTTPException(
            status_code=400,
            detail="ScreenScraper developer credentials are not configured",
        )
    where = ["g.platform=?", "gm.rating IS NULL"]
    params: list[object] = [payload.platform]
    if payload.search.strip():
        escaped = payload.search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("(g.display_name LIKE ? ESCAPE '\\' OR g.primary_relpath LIKE ? ESCAPE '\\')")
        params.extend([f"%{escaped}%", f"%{escaped}%"])
    with db.connect() as connection:
        game_ids = [
            row["id"] for row in connection.execute(
                "SELECT g.id FROM games g LEFT JOIN game_metadata gm ON gm.game_id=g.id WHERE "
                + " AND ".join(where)
                + " ORDER BY g.display_name COLLATE NOCASE LIMIT 5000",
                params,
            )
        ]
        active = connection.execute(
            "SELECT id FROM jobs WHERE kind IN ('rating_scrape','artwork_scrape','artwork_bulk') "
            "AND status IN ('queued','running','paused','cancelling') ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if active:
        return {"job_id": active["id"], "already_running": True}
    job_id = enqueue_job(
        "rating_scrape",
        f"Fetching ScreenScraper ratings for {len(game_ids)} {payload.platform} games",
        screenscraper.scrape,
        game_ids,
        True,
        False,
    )
    return {"job_id": job_id, "already_running": False, "requested": len(game_ids)}


@app.get("/api/rankings/{platform}")
def platform_ranking(platform: str):
    with db.connect() as connection:
        if not connection.execute("SELECT 1 FROM games WHERE platform=?", (platform,)).fetchone():
            raise HTTPException(status_code=404, detail="Platform was not found")
    return ranking_service.coverage(platform)


@app.post("/api/rankings/{platform}/refresh", status_code=202)
def refresh_platform_ranking(platform: str):
    if not ranking_service.configured:
        raise HTTPException(status_code=400, detail="RAWG API key is not configured")
    with db.connect() as connection:
        if not connection.execute("SELECT 1 FROM games WHERE platform=?", (platform,)).fetchone():
            raise HTTPException(status_code=404, detail="Platform was not found")
        active = connection.execute(
            "SELECT id FROM jobs WHERE kind='ranking_refresh' AND status IN ('queued','running','cancelling') "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if active:
        return {"job_id": active["id"], "already_running": True}
    job_id = enqueue_job(
        "ranking_refresh",
        f"Refreshing RAWG top 100 for {platform}",
        ranking_service.refresh,
        platform,
    )
    return {"job_id": job_id, "already_running": False}


@app.patch("/api/games/{game_id}/rename", status_code=202)
def rename_game(game_id: int, payload: RenameRequest):
    job_id = enqueue_job("rename", f"Renaming game {game_id}", library.rename_bundle, game_id, payload.name)
    return {"job_id": job_id}


@app.get("/api/naming/catalogs")
def naming_catalogs():
    return naming.catalogs()


@app.post("/api/naming/catalogs")
def import_naming_catalog(payload: DatImportRequest):
    return naming.import_dat(payload.source_name, payload.platform, payload.content)


@app.delete("/api/naming/catalogs/{catalog_id}")
def delete_naming_catalog(catalog_id: int):
    naming.delete_catalog(catalog_id)
    return {"deleted": catalog_id}


@app.get("/api/naming/suggestions")
def naming_suggestions(
    search: str = "",
    platform: str = "",
    confidence: str = Query("all", pattern="^(all|exact|strong|metadata|cleanup)$"),
    save_impact: str = Query("all", pattern="^(all|has_saves|no_saves|review)$"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    return naming.suggestions(search, platform, confidence, save_impact, limit, offset)


@app.post("/api/naming/apply", status_code=202)
def apply_naming_suggestions(payload: BulkRenameRequest):
    renames = [(item.game_id, item.name) for item in payload.items]
    job_id = enqueue_job("bulk_rename", f"Applying {len(renames)} naming suggestions", library.bulk_rename, renames)
    return {"job_id": job_id}


@app.delete("/api/games/{game_id}", status_code=202)
def delete_game(game_id: int):
    job_id = enqueue_job(
        "delete", f"Moving game {game_id} to trash", library.delete_bundle, game_id, coalesce=True
    )
    return {"job_id": job_id}


@app.get("/api/devices")
def devices():
    with db.connect() as connection:
        rows = connection.execute(
            "SELECT d.*,COUNT(DISTINCT ds.game_id) AS selected_games,COUNT(DISTINCT dp.game_id) AS deployed_games "
            "FROM devices d LEFT JOIN device_selections ds ON ds.device_id=d.id "
            "LEFT JOIN deployments dp ON dp.device_id=d.id GROUP BY d.id ORDER BY d.name COLLATE NOCASE"
        ).fetchall()
    return [dict(row) for row in rows]


@app.put("/api/devices/{device_id}/selection")
def update_selection(device_id: int, payload: SelectionRequest):
    library.set_selection(device_id, payload.game_id, payload.selected)
    return {"selected": payload.selected}


@app.put("/api/devices/{device_id}/selections")
def update_selections(device_id: int, payload: BulkSelectionRequest):
    updated = library.set_selections(device_id, payload.game_ids, payload.selected)
    return {"selected": payload.selected, "updated": updated}


@app.put("/api/devices/{device_id}/deployment-mode")
def update_device_deployment_mode(device_id: int, payload: DeviceDeploymentModeRequest):
    library.set_device_deployment_mode(device_id, payload.mode)
    return {"mode": payload.mode}


@app.get("/api/devices/{device_id}/preview")
def device_preview(device_id: int):
    with db.connect() as connection:
        device = connection.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        if not device:
            raise HTTPException(status_code=404, detail="Device was not found")
        desired = connection.execute(
            "SELECT COUNT(DISTINCT game_id) AS games,COUNT(*) AS files FROM "
            "(SELECT ds.game_id,gf.device_relpath FROM device_selections ds JOIN game_files gf ON gf.game_id=ds.game_id WHERE ds.device_id=?)",
            (device_id,),
        ).fetchone()
        additions = connection.execute(
            "SELECT COUNT(*) AS count FROM device_selections ds JOIN game_files gf ON gf.game_id=ds.game_id "
            "WHERE ds.device_id=? AND NOT EXISTS(SELECT 1 FROM deployments dp WHERE dp.device_id=ds.device_id "
            "AND dp.game_id=ds.game_id AND dp.relpath=gf.device_relpath)", (device_id,)
        ).fetchone()["count"]
        removals = connection.execute(
            "SELECT COUNT(*) AS count FROM deployments dp WHERE dp.device_id=? "
            "AND NOT EXISTS(SELECT 1 FROM device_selections ds WHERE ds.device_id=dp.device_id AND ds.game_id=dp.game_id)",
            (device_id,),
        ).fetchone()["count"]
    storage = library.device_storage_summary(device_id)
    return {
        "device": dict(device),
        "games": desired["games"],
        "files": desired["files"],
        "additions": additions,
        "removals": removals,
        "conversions": storage["conversions"] if device["deployment_mode"] == "hardlink" else 0,
        "hardlinked": storage["hardlinked"],
        "copied": storage["copied"],
        "missing": storage["missing"],
        "unknown": storage["unknown"],
    }


@app.post("/api/devices/{device_id}/apply", status_code=202)
def apply_device(device_id: int):
    return queue_device_apply_job(device_id)


@app.get("/api/trash")
def trash():
    with db.connect() as connection:
        rows = connection.execute("SELECT * FROM trash_items ORDER BY deleted_at DESC").fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["file_count"] = len(json.loads(item.pop("manifest_json"))["files"])
        result.append(item)
    return result


@app.post("/api/trash/{trash_id}/restore", status_code=202)
def restore(trash_id: int):
    job_id = enqueue_job("restore", f"Restoring trash item {trash_id}", library.restore_trash, trash_id)
    return {"job_id": job_id}


@app.delete("/api/trash/{trash_id}", status_code=202)
def purge(trash_id: int):
    job_id = enqueue_job("purge", f"Permanently deleting trash item {trash_id}", library.purge_trash, trash_id)
    return {"job_id": job_id}


@app.post("/api/trash/purge", status_code=202)
def bulk_purge(payload: BulkPurgeRequest):
    job_id = enqueue_job(
        "bulk_purge",
        f"Permanently deleting {len(set(payload.trash_ids))} trashed bundles",
        library.bulk_purge_trash,
        payload.trash_ids,
    )
    return {"job_id": job_id}


@app.get("/api/uploads")
def upload_sessions():
    return transfers.list_sessions()


@app.post("/api/uploads", status_code=201)
async def create_upload(request: Request):
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_MANIFEST_BYTES:
                raise HTTPException(status_code=413, detail="Upload manifest is too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from None
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_MANIFEST_BYTES:
            raise HTTPException(status_code=413, detail="Upload manifest is too large")
    try:
        payload = UploadCreateRequest.model_validate_json(bytes(body))
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail="Upload manifest is invalid") from exc
    return transfers.create_session(
        payload.platform,
        payload.bundle_name,
        payload.folder_mode,
        [item.model_dump() for item in payload.files],
    )


@app.put("/api/uploads/{session_id}/files/{file_index}")
async def upload_chunk(session_id: str, file_index: int, request: Request):
    try:
        offset = int(request.headers.get("upload-offset", ""))
    except ValueError:
        raise HTTPException(status_code=400, detail="Upload-Offset header is required") from None
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > settings.upload_chunk_bytes:
                raise HTTPException(status_code=413, detail="Upload chunk is too large")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length") from None
    return await transfers.write_chunk(
        session_id, file_index, offset, request.stream()
    )


@app.post("/api/uploads/{session_id}/finalize", status_code=202)
def finalize_upload(session_id: str):
    job_id = enqueue_job(
        "upload_finalize",
        f"Finalizing upload {session_id}",
        transfers.finalize,
        session_id,
        coalesce=True,
    )
    return {"job_id": job_id}


@app.delete("/api/uploads/{session_id}")
def cancel_upload(session_id: str):
    return transfers.cancel(session_id)


@app.post("/api/games/{game_id}/download-ticket")
def game_download_ticket(game_id: int):
    return transfers.create_download_ticket(game_id)


@app.get("/api/downloads/{token}")
def download_game(token: str):
    download = transfers.resolve_download(token)
    if not download["archive"]:
        path = download["paths"][0][0]
        return FileResponse(
            path,
            filename=download["filename"],
            media_type="application/octet-stream",
            headers={"Cache-Control": "no-store"},
        )
    disposition = f"attachment; filename*=UTF-8''{quote(download['filename'])}"
    return StreamingResponse(
        transfers.stream_zip(download),
        media_type="application/zip",
        headers={
            "Content-Disposition": disposition,
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/api/saves")
def save_overview():
    snapshots = saves.list_snapshots(limit=1)
    return {
        "settings": saves.settings_payload(),
        "latest_snapshot": snapshots["items"][0] if snapshots["items"] else None,
        "snapshot_count": snapshots["total"],
        "matching": saves.match_summary(),
    }


@app.get("/api/saves/current")
def current_saves(
    search: str = "",
    limit: int = Query(250, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    return saves.current_files(search, limit, offset)


@app.get("/api/saves/unmatched")
def unmatched_saves(
    search: str = "",
    status: str = Query("all", pattern="^(all|orphan|possible|ambiguous)$"),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    return saves.unmatched_groups(search, status, limit, offset)


@app.post("/api/saves/impacts")
def save_impacts(payload: SaveImpactRequest):
    return {"items": saves.save_impacts(payload.game_ids)}


@app.post("/api/saves/orphans/delete", status_code=202)
def delete_orphan_saves(payload: SaveOrphanDeleteRequest):
    job_id = enqueue_job(
        "save_delete",
        "Creating safety snapshot before deleting orphan saves",
        saves.delete_orphan_group,
        payload.group_key,
    )
    return {"job_id": job_id}


@app.get("/api/saves/settings")
def save_settings():
    return saves.settings_payload()


@app.put("/api/saves/settings")
def update_save_settings(payload: SaveSettingsRequest):
    result = saves.update_settings(payload.model_dump())
    pruned = saves.prune_retention()
    return {**result, "pruned": pruned}


@app.post("/api/saves/snapshots", status_code=202)
def create_save_snapshot(payload: SaveSnapshotRequest):
    with db.connect() as connection:
        active = connection.execute(
            "SELECT id FROM jobs WHERE kind IN ('save_snapshot','save_restore') "
            "AND status IN ('queued','running','cancelling') ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if active:
        return {"job_id": active["id"], "already_running": True}
    job_id = enqueue_job(
        "save_snapshot",
        "Creating save snapshot",
        saves.create_snapshot,
        "manual",
        payload.note,
        False,
    )
    return {"job_id": job_id, "already_running": False}


@app.get("/api/saves/snapshots")
def save_snapshots(
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    return saves.list_snapshots(limit, offset)


@app.get("/api/saves/snapshots/{snapshot_id}")
def save_snapshot_detail(
    snapshot_id: int,
    search: str = "",
    limit: int = Query(250, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    return saves.snapshot_detail(snapshot_id, search, limit, offset)


@app.get("/api/saves/snapshots/{snapshot_id}/compare")
def compare_save_snapshot(snapshot_id: int):
    return saves.compare(snapshot_id)


@app.get("/api/saves/snapshots/{snapshot_id}/files/{relpath:path}")
def download_save_snapshot_file(snapshot_id: int, relpath: str):
    with db.connect() as connection:
        row = connection.execute(
            "SELECT sha256 FROM save_snapshot_files WHERE snapshot_id=? AND relpath=?",
            (snapshot_id, relpath),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Snapshot file was not found")
    blob = saves._blob_path(row["sha256"])
    if not blob.is_file():
        raise HTTPException(status_code=409, detail="Snapshot file failed its storage check")
    return FileResponse(blob, filename=Path(relpath).name, media_type="application/octet-stream")


@app.put("/api/saves/snapshots/{snapshot_id}/pin")
def pin_save_snapshot(snapshot_id: int, payload: SavePinRequest):
    return saves.pin(snapshot_id, payload.pinned)


@app.post("/api/saves/snapshots/{snapshot_id}/restore", status_code=202)
def restore_save_snapshot(snapshot_id: int, payload: SaveRestoreRequest):
    if not payload.retroarch_closed:
        raise HTTPException(
            status_code=400,
            detail="Confirm that RetroArch is closed on every device before restoring",
        )
    with db.connect() as connection:
        if not connection.execute(
            "SELECT 1 FROM save_snapshots WHERE id=?", (snapshot_id,)
        ).fetchone():
            raise HTTPException(status_code=404, detail="Save snapshot was not found")
        active = connection.execute(
            "SELECT id FROM jobs WHERE kind IN ('save_snapshot','save_restore') "
            "AND status IN ('queued','running','cancelling') ORDER BY id DESC LIMIT 1"
        ).fetchone()
    if active:
        return {"job_id": active["id"], "already_running": True}
    job_id = enqueue_job(
        "save_restore",
        f"Restoring save snapshot #{snapshot_id}",
        saves.restore_snapshot,
        snapshot_id,
        payload.expected_tree_hash,
    )
    return {"job_id": job_id, "already_running": False}


@app.get("/api/jobs/{job_id}")
def job(job_id: int):
    with db.connect() as connection:
        row = connection.execute(
            "SELECT j.*,(SELECT COUNT(*) FROM job_issues i WHERE i.job_id=j.id) AS issue_count "
            "FROM jobs j WHERE j.id=?",
            (job_id,),
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Job was not found")
    result = job_payload(row)
    result_json = result.pop("result_json", None)
    result["result"] = json.loads(result_json) if result_json else None
    return result


@app.get("/api/jobs")
def jobs():
    with db.connect() as connection:
        rows = connection.execute(
            "SELECT j.*,(SELECT COUNT(*) FROM job_issues i WHERE i.job_id=j.id) AS issue_count "
            "FROM jobs j ORDER BY j.id DESC LIMIT 100"
        ).fetchall()
    return [job_payload(row) for row in rows]


@app.get("/api/jobs/{job_id}/issues")
def job_issues(
    job_id: int,
    limit: int = Query(250, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    with db.connect() as connection:
        job_row = connection.execute(
            "SELECT j.*,(SELECT COUNT(*) FROM job_issues i WHERE i.job_id=j.id) AS issue_count "
            "FROM jobs j WHERE j.id=?",
            (job_id,),
        ).fetchone()
        if not job_row:
            raise HTTPException(status_code=404, detail="Job was not found")
        rows = connection.execute(
            "SELECT id,detail FROM job_issues WHERE job_id=? ORDER BY id LIMIT ? OFFSET ?",
            (job_id, limit, offset),
        ).fetchall()
    payload = job_payload(job_row)
    total = int(payload["issue_count"])
    reported_total = int(payload["reported_issue_count"])
    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "reported_total": reported_total,
        "captured_all": total >= reported_total,
        "limit": limit,
        "offset": offset,
    }


@app.post("/api/jobs/{job_id}/cancel", status_code=202)
def cancel_job(job_id: int):
    try:
        return request_job_cancel(job_id)
    except LibraryError as exc:
        status_code = 404 if str(exc) == "Job was not found" else 409
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc


@app.get("/api/activity")
def activity():
    with db.connect() as connection:
        rows = connection.execute("SELECT * FROM activity ORDER BY id DESC LIMIT 100").fetchall()
    return [dict(row) for row in rows]
