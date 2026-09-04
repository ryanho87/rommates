from __future__ import annotations

import hashlib
import json
import re
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field, ValidationError

from .artwork_cache import ArtworkThumbnailCache, THUMBNAIL_CACHE_VERSION
from .auth import AuthService, PASSWORD_MIN_LENGTH, Principal, ROLES
from .config import Settings
from .db import Database
from .device_sync import DeviceSyncMonitor
from .library import JobCancelled, LibraryError, LibraryService
from .mobile_push import MobilePushService, PUSH_EVENTS
from .mobile_releases import MobileReleaseService
from .mcp_server import RommatesMCPService, create_mcp_server
from .naming import NamingService
from .notifications import NotificationService
from .rankings import RankingService
from .saves import SaveSnapshotService
from .screenscraper import ScreenScraperService
from .syncthing import SyncthingService
from .transfers import MAX_MANIFEST_BYTES, TransferError, TransferService


MINIMUM_TOKEN_LENGTH = 16
CANCELLABLE_JOB_KINDS = frozenset({"scan", "device_apply", "device_export", "save_snapshot", "save_restore", "save_delete", "save_conflict", "artwork_scrape", "artwork_bulk", "rating_scrape", "ranking_refresh", "upload_finalize"})

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
artwork_thumbnails = ArtworkThumbnailCache(settings, db)
ranking_service = RankingService(settings, db)
transfers = TransferService(settings, db, library)
syncthing = SyncthingService(settings)
notifications = NotificationService(settings, db)
mobile_push = MobilePushService(settings, db)
mobile_releases = MobileReleaseService(db, mobile_push)
device_syncs = DeviceSyncMonitor(
    db,
    syncthing,
    notifications,
    mobile_push=mobile_push,
    poll_seconds=max(3, settings.syncthing_cache_seconds),
)
auth = AuthService(db)
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
        summary = (
            f"Linked {result.get('linked', 0)}, converted {result.get('converted', 0)}, "
            f"copied {result.get('copied', 0)}, removed {result.get('removed', 0)}, "
            f"left {result.get('unchanged', 0)} unchanged"
        )
        rescan = result.get("syncthing_rescan")
        if isinstance(rescan, dict):
            if rescan.get("requested"):
                summary += "; requested Syncthing rescan"
            elif rescan.get("error"):
                summary += f"; Syncthing rescan skipped: {rescan['error']}"
        sync_run = result.get("device_sync")
        if isinstance(sync_run, dict):
            summary += "; tracking delivery to the remote device"
        return summary
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
    if kind == "save_conflict":
        return (
            f"Resolved {result.get('canonical_relpath', 'save conflict')} using the "
            f"{result.get('decision', 'selected')} version; safety snapshot "
            f"#{result.get('safety_snapshot_id')}"
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


def safe_notify(
    event: str,
    title: str,
    detail: str,
    path: str = "",
    *,
    dedupe_key: str = "",
) -> None:
    """Queue a best-effort notification without affecting the originating operation."""
    try:
        notifications.notify(event, title, detail, path, dedupe_key=dedupe_key)
    except Exception as exc:
        try:
            db.activity("notification", f"Discord notification could not be queued: {exc}")
        except Exception:
            pass


def notify_job_result(kind: str, detail: str, result: object) -> None:
    routes = {
        "upload_finalize": ("upload", "ROM upload completed", "transfers"),
        "save_conflict": ("save", "Save conflict resolved", "saves"),
        "save_snapshot": ("save", "Save snapshot completed", "saves"),
        "save_restore": ("save", "Save restore completed", "saves"),
        "save_delete": ("save", "Save cleanup completed", "saves"),
        "device_apply": ("device", "Device changes applied", "devices"),
        "scan": ("scan", "Library scan completed", "library"),
        "delete": ("trash", "ROM moved to trash", "trash"),
        "bulk_delete": ("trash", "Duplicate cleanup completed", "trash"),
        "restore": ("trash", "ROM restored from trash", "trash"),
        "purge": ("trash", "Trash permanently deleted", "trash"),
        "bulk_purge": ("trash", "Selected trash permanently deleted", "trash"),
    }
    route = routes.get(kind)
    if not route:
        return
    event, title, path = route
    safe_notify(event, title, job_result_detail(kind, result, detail), path)


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
            elif kind in {"device_export", "save_snapshot", "save_restore", "save_conflict", "artwork_scrape", "artwork_bulk", "rating_scrape", "ranking_refresh", "upload_finalize"}:
                result = operation(
                    *args, progress_callback=report_progress, cancel_check=check_cancelled
                )
            else:
                result = operation(*args)
            if kind == "scan":
                try:
                    ranking_service.reconcile_all()
                except Exception as exc:
                    # A healthy library scan must not fail because optional ranking
                    # metadata could not be reconciled. The next ranking request can retry.
                    db.activity("ranking", f"Ranking matches could not be refreshed: {exc}")
            if kind in {"artwork_scrape", "artwork_bulk"} and isinstance(result, dict):
                job_issues.extend(str(issue) for issue in result.get("issues", []))
            if kind == "device_apply" and isinstance(result, dict):
                rescan = result.get("syncthing_rescan")
                transferred = int(result.get("linked") or 0) + int(result.get("copied") or 0)
                removed = int(result.get("removed") or 0)
                if isinstance(rescan, dict) and rescan.get("requested") and (transferred or removed):
                    folder_id = next(
                        (
                            str(item.get("folder_id") or "")
                            for item in rescan.get("folders", [])
                            if isinstance(item, dict) and item.get("folder_id")
                        ),
                        "",
                    )
                    with db.connect() as connection:
                        request_row = connection.execute(
                            "SELECT requested_by FROM jobs WHERE id=?", (job_id,)
                        ).fetchone()
                    try:
                        sync_run = device_syncs.track(
                            int(args[0]),
                            job_id,
                            request_row["requested_by"] if request_row else None,
                            added=transferred,
                            removed=removed,
                            folder_id=folder_id,
                        )
                        if sync_run:
                            result["device_sync"] = sync_run
                    except Exception as exc:
                        # The local apply is already committed. A monitoring
                        # problem must be visible without falsely failing it.
                        result["device_sync_error"] = str(exc)
                        db.activity("device_sync", f"Could not track device job {job_id}: {exc}")
            # Cooperative operations check cancellation before their final commit. A
            # stop request arriving after the operation returns must not relabel a
            # successfully committed filesystem change as cancelled.
            persist_issues()
            with db.write() as connection:
                connection.execute(
                    "UPDATE jobs SET status='complete',progress=100,detail=?,result_json=?,completed_at=CURRENT_TIMESTAMP WHERE id=?",
                    (job_result_detail(kind, result, detail), json.dumps(result), job_id),
                )
            notify_job_result(kind, detail, result)
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
        safe_notify(
            "job_failed",
            f"{kind.replace('_', ' ').title()} failed",
            str(exc),
            f"jobs?job={job_id}",
            dedupe_key=f"job:{job_id}:failed",
        )
    finally:
        # Also covers a failure raised while constructing the terminal job result.
        persist_issues()
        with job_cancellations_lock:
            job_cancellations.pop(job_id, None)


def enqueue_job(
    kind: str,
    detail: str,
    operation,
    *args,
    coalesce: bool = False,
    requested_by: int | None = None,
) -> int:
    with db.write() as connection:
        if coalesce:
            active = connection.execute(
                "SELECT id FROM jobs WHERE kind=? AND detail=? "
                "AND requested_by IS ? AND status IN ('queued','running','paused','cancelling') "
                "ORDER BY id LIMIT 1",
                (kind, detail, requested_by),
            ).fetchone()
            if active:
                return active["id"]
        active_count = connection.execute(
            "SELECT COUNT(*) AS count FROM jobs WHERE status IN ('queued','running','paused','cancelling')"
        ).fetchone()["count"]
        if active_count >= 25:
            raise LibraryError("Too many jobs are already queued; wait for one to finish")
        connection.execute(
            "INSERT INTO jobs(kind,status,detail,requested_by) VALUES(?,'queued',?,?)",
            (kind, detail, requested_by),
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


def queue_device_apply_job(
    device_id: int, requested_by: int | None = None
) -> dict[str, object]:
    with db.connect() as connection:
        if not connection.execute("SELECT 1 FROM devices WHERE id=?", (device_id,)).fetchone():
            raise LibraryError("Device was not found")
    job_id = enqueue_job(
        "device_apply",
        f"Applying device {device_id}",
        apply_device_and_rescan,
        device_id,
        requested_by=requested_by,
    )
    return {"job_id": job_id}


def apply_device_and_rescan(
    device_id: int, cancel_check=None
) -> dict[str, object]:
    result: dict[str, object] = dict(library.apply_device(device_id, cancel_check=cancel_check))
    with db.connect() as connection:
        device = connection.execute("SELECT name FROM devices WHERE id=?", (device_id,)).fetchone()
    if device:
        result["syncthing_rescan"] = syncthing.rescan_device(device["name"])
    return result


def apply_reviewed_device_and_rescan(
    device_id: int, preview_token: str, cancel_check=None
) -> dict[str, object]:
    result = dict(
        mcp_service.execute_reviewed_device_apply(
            device_id, preview_token, cancel_check=cancel_check
        )
    )
    with db.connect() as connection:
        device = connection.execute("SELECT name FROM devices WHERE id=?", (device_id,)).fetchone()
    if device:
        result["syncthing_rescan"] = syncthing.rescan_device(device["name"])
    return result


def queue_reviewed_device_apply_job(device_id: int, preview_token: str) -> dict[str, object]:
    with db.connect() as connection:
        if not connection.execute("SELECT 1 FROM devices WHERE id=?", (device_id,)).fetchone():
            raise LibraryError("Device was not found")
    job_id = enqueue_job(
        "device_apply",
        f"Applying reviewed MCP plan for device {device_id}",
        apply_reviewed_device_and_rescan,
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
    next_conflict_check = 0.0
    while not stop.wait(30):
        try:
            if saves.due_for_automatic_snapshot():
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
                    )
        except Exception as exc:
            db.activity("save_snapshot", f"Scheduled snapshot could not start: {exc}")
        if time.monotonic() < next_conflict_check:
            continue
        next_conflict_check = time.monotonic() + 60
        if not notifications.event_enabled("save_conflict"):
            continue
        try:
            conflict_report = saves.conflicts(
                limit=500,
                device_names=_syncthing_device_names(),
            )
            for conflict in conflict_report["items"]:
                source = conflict.get("device_name") or conflict.get("device_id") or "another device"
                safe_notify(
                    "save_conflict",
                    "Save conflict needs review",
                    f"{conflict['canonical_relpath']} has a competing version from {source}.",
                    "saves",
                    dedupe_key=(
                        f"save-conflict:{conflict['conflict_relpath']}:"
                        f"{conflict['conflict_sha256']}"
                    ),
                )
        except Exception as exc:
            db.activity("notification", f"Save conflicts could not be checked: {exc}")


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
    ranking_service.reconcile_all()
    saves.initialize()
    transfers.initialize()
    notifications.initialize()
    auth.initialize()
    mobile_push.start()
    device_syncs.start()
    scheduler_stop = threading.Event()
    scheduler_thread = threading.Thread(target=save_scheduler, args=(scheduler_stop,), daemon=True)
    scheduler_thread.start()
    artwork_thumbnails.start()
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
        artwork_thumbnails.close()
        device_syncs.close()
        mobile_push.close()
        notifications.close()


app = FastAPI(
    title="ROMmates",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
STATIC_DIR = Path(__file__).parent / "static"
_asset_digest = hashlib.sha256()
for _asset_name in ("styles.css", "app.js"):
    _asset_digest.update((STATIC_DIR / _asset_name).read_bytes())
ASSET_VERSION = _asset_digest.hexdigest()[:12]
INDEX_HTML = (
    (STATIC_DIR / "index.html")
    .read_text(encoding="utf-8")
    .replace("/static/styles.css", f"/static/styles.css?v={ASSET_VERSION}")
    .replace("/static/app.js", f"/static/app.js?v={ASSET_VERSION}")
)
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
    mode: str = Field(pattern="^hardlink$")


class DeviceCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    deployment_mode: str = Field(default="hardlink", pattern="^hardlink$")
    delivery_mode: str = Field(default="syncthing", pattern="^(syncthing|download)$")
    clone_device_id: int | None = Field(default=None, ge=1)
    keep_in_sync: bool = False
    storage_capacity_bytes: int = Field(default=0, ge=0, le=1_125_899_906_842_624)


class DeviceOwnerRequest(BaseModel):
    owner_user_id: int | None = Field(default=None, ge=1)


class DeviceStorageCapacityRequest(BaseModel):
    storage_capacity_bytes: int = Field(ge=0, le=1_125_899_906_842_624)


class DeviceSyncthingReadyRequest(BaseModel):
    ready: bool = True


class DeviceSyncthingShareRequest(BaseModel):
    device_id: str = Field(min_length=7, max_length=80)


class DeviceRosterLinkRequest(BaseModel):
    target_device_ids: list[int] = Field(min_length=1, max_length=20)


class DeviceRosterCloneRequest(BaseModel):
    source_device_id: int = Field(ge=1)


class DeviceGroupUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class DeviceGroupCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    source_device_id: int = Field(ge=1)
    member_device_ids: list[int] = Field(min_length=1, max_length=20)


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


class SaveConflictResolveRequest(BaseModel):
    conflict_relpath: str = Field(min_length=1, max_length=4096)
    decision: str = Field(pattern="^(current|conflict)$")
    expected_canonical_sha256: str = Field(default="", max_length=64, pattern="^$|^[a-f0-9]{64}$")
    expected_conflict_sha256: str = Field(min_length=64, max_length=64, pattern="^[a-f0-9]{64}$")
    device_id: str = Field(default="", max_length=64)
    device_name: str = Field(default="", max_length=255)


class ArtworkScrapeRequest(BaseModel):
    game_ids: list[int] = Field(min_length=1, max_length=500)
    missing_only: bool = True


class ArtworkBulkRequest(BaseModel):
    asset_mode: str = Field(default="cover", pattern="^(cover|full)$")
    platforms: list[str] = Field(default_factory=list, max_length=100)
    game_ids: list[int] = Field(default_factory=list, max_length=500)


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


class NotificationSettingsRequest(BaseModel):
    enabled: bool
    events: dict[str, bool]


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=1024)


class MobileLoginRequest(LoginRequest):
    client_name: str = Field(default="ROMmates for iOS", min_length=1, max_length=100)


class MobileInstallationRequest(BaseModel):
    installation_id: str = Field(
        min_length=16,
        max_length=64,
        pattern="^[A-Za-z0-9._-]+$",
    )
    device_token: str = Field(
        min_length=64,
        max_length=512,
        pattern="^[A-Fa-f0-9]+$",
    )
    app_version: str = Field(default="", max_length=50)
    notifications_enabled: bool = True


class MobilePushPreferencesRequest(BaseModel):
    events: dict[str, bool]


class MobileReleaseRequest(BaseModel):
    build: int = Field(ge=1)
    version: str = Field(min_length=1, max_length=50)
    notes: str = Field(min_length=1, max_length=4000)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=1024)


class ProfileUpdateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    display_name: str = Field(min_length=1, max_length=100)


class OnboardingUpdateRequest(BaseModel):
    tour_key: str = Field(min_length=1, max_length=64, pattern="^[a-z0-9_-]+$")
    tour_version: int = Field(default=1, ge=1, le=1000)
    current_step: int = Field(default=0, ge=0, le=100)
    dismissed: bool = False
    completed: bool = False


class UserCreateRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    display_name: str = Field(default="", max_length=100)
    password: str = Field(min_length=PASSWORD_MIN_LENGTH, max_length=1024)
    roles: list[str] = Field(default_factory=list, max_length=len(ROLES))
    role: str | None = None


class UserUpdateRequest(BaseModel):
    roles: list[str] | None = Field(default=None, max_length=len(ROLES))
    role: str | None = None
    active: bool | None = None
    password: str = Field(default="", max_length=1024)


class UploadReviewRequest(BaseModel):
    note: str = Field(default="", max_length=500)


def request_principal(request: Request) -> Principal:
    principal = getattr(request.state, "principal", None)
    if not principal:
        raise HTTPException(status_code=401, detail="Sign in to continue")
    return principal


def request_session_token(request: Request) -> str:
    return str(getattr(request.state, "session_token", ""))


def can_manage_devices(principal: Principal) -> bool:
    return principal.has_role("member") or principal.has_role("admin")


def permission_payload(principal: Principal) -> dict[str, bool]:
    ready = not principal.must_change_password
    return {
        "admin": principal.has_role("admin") and ready,
        "manage_devices": can_manage_devices(principal) and ready,
        "upload": (
            principal.has_role("admin") or principal.has_role("contributor")
        ) and ready,
        "download": ready,
    }


def require_device_access(device_id: int, principal: Principal):
    with db.connect() as connection:
        row = connection.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
    if not row or (
        not principal.has_role("admin")
        and (principal.id is None or row["owner_user_id"] != principal.id)
    ):
        raise HTTPException(status_code=404, detail="Device was not found")
    return row


def require_device_group_access(group_id: int, principal: Principal):
    with db.connect() as connection:
        group = connection.execute(
            "SELECT * FROM device_roster_groups WHERE id=?", (group_id,)
        ).fetchone()
        members = connection.execute(
            "SELECT * FROM devices WHERE roster_group_id=? ORDER BY name COLLATE NOCASE",
            (group_id,),
        ).fetchall()
    if not group or len(members) < 2:
        raise HTTPException(status_code=404, detail="Device group was not found")
    if not principal.has_role("admin") and (
        principal.id is None or group["owner_user_id"] != principal.id
    ):
        raise HTTPException(status_code=404, detail="Device group was not found")
    return group, members


def require_job_access(job_id: int, principal: Principal):
    with db.connect() as connection:
        row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row or (
        not principal.has_role("admin")
        and (principal.id is None or row["requested_by"] != principal.id)
    ):
        raise HTTPException(status_code=404, detail="Job was not found")
    return row


def role_allows(principal: Principal, method: str, path: str) -> bool:
    if principal.has_role("admin"):
        return True
    if path in {
        "/api/auth/me",
        "/api/auth/logout",
        "/api/auth/impersonation/end",
        "/api/auth/password",
        "/api/auth/profile",
        "/api/account/summary",
        "/api/onboarding",
        "/api/v1/mobile/bootstrap",
        "/api/v1/mobile/push-installation",
        "/api/v1/mobile/push-preferences",
        "/api/v1/mobile/releases",
    }:
        return True
    if path.startswith("/api/inbox"):
        return method in {"GET", "POST"}
    if path.startswith("/api/v1/mobile/push-installation"):
        return method in {"PUT", "DELETE"}
    if method == "GET" and (
        path in {"/api/status", "/api/platforms"}
        or path.startswith("/api/games")
        or path == "/api/artwork/manifest"
        or path.startswith("/api/artwork/assets/")
        or path.startswith("/api/artwork/thumbnails/")
        or path.startswith("/api/rankings/")
    ):
        return True
    if principal.has_role("member"):
        if method == "GET" and (
            path == "/api/devices"
            or path.startswith("/api/device-groups")
            or path.startswith("/api/devices/")
            or path.startswith("/api/jobs/")
            or path == "/api/syncthing/status"
        ):
            return True
        if method == "POST" and (
            path == "/api/devices"
            or path == "/api/device-groups"
            or (path.startswith("/api/device-groups/") and path.endswith("/apply"))
            or (path.startswith("/api/devices/") and path.endswith("/apply"))
            or (path.startswith("/api/devices/") and path.endswith("/discard-changes"))
            or (path.startswith("/api/devices/") and path.endswith("/export-ticket"))
            or (path.startswith("/api/devices/") and path.endswith("/roster-clone"))
            or (path.startswith("/api/devices/") and path.endswith("/roster-link"))
            or (path.startswith("/api/devices/") and path.endswith("/syncthing-share"))
            or (path.startswith("/api/jobs/") and path.endswith("/cancel"))
        ):
            return True
        if method == "DELETE" and path.startswith("/api/devices/") and path.endswith("/roster-link"):
            return True
        if method == "DELETE" and path.startswith("/api/device-groups/"):
            return True
        if method == "PUT" and path.startswith("/api/device-groups/"):
            return True
        if (
            method == "PUT"
            and path.startswith("/api/devices/")
            and not path.endswith(("/owner", "/syncthing-ready"))
        ):
            return True
    if method == "POST" and path.startswith("/api/games/") and path.endswith("/download-ticket"):
        return True
    if (
        principal.has_role("contributor")
        and path.startswith("/api/uploads")
        and not path.endswith(("/approve", "/reject"))
    ):
        return True
    return False


_MOBILE_ID = r"[A-Za-z0-9._~-]+"


def mobile_public_route_allowed(method: str, path: str) -> bool:
    """Keep the native hostname smaller than the full browser/admin API.

    This is intentionally independent of role checks: a route must first be a
    native-client capability, then the authenticated user's role and ownership
    are evaluated by the normal authorization layer.
    """
    if method in {"GET", "HEAD"} and path == "/api/health":
        return True
    if method == "POST" and path == "/api/v1/mobile/session":
        return True
    if method in {"GET", "HEAD"} and path.startswith("/api/downloads/"):
        return True
    exact = {
        ("GET", "/api/v1/mobile/bootstrap"),
        ("GET", "/api/v1/mobile/releases"),
        ("PUT", "/api/v1/mobile/push-installation"),
        ("PUT", "/api/v1/mobile/push-preferences"),
        ("POST", "/api/auth/logout"),
        ("POST", "/api/auth/password"),
        ("PATCH", "/api/auth/profile"),
        ("GET", "/api/account/summary"),
        ("GET", "/api/onboarding"),
        ("PATCH", "/api/onboarding"),
        ("GET", "/api/platforms"),
        ("GET", "/api/games"),
        ("GET", "/api/devices"),
        ("POST", "/api/devices"),
        ("GET", "/api/device-groups"),
        ("POST", "/api/device-groups"),
        ("GET", "/api/uploads"),
        ("POST", "/api/uploads"),
        ("GET", "/api/inbox"),
        ("POST", "/api/inbox/read-all"),
    }
    if (method, path) in exact:
        return True
    patterns = {
        "GET": (
            rf"/api/games/\d+",
            rf"/api/artwork/thumbnails/\d+",
            rf"/api/devices/\d+/sync-status",
        ),
        "POST": (
            rf"/api/games/\d+/download-ticket",
            rf"/api/device-groups/\d+/apply",
            rf"/api/devices/\d+/(?:apply|discard-changes)",
            rf"/api/uploads/{_MOBILE_ID}/finalize",
            rf"/api/inbox/\d+/read",
        ),
        "PUT": (
            rf"/api/device-groups/\d+",
            rf"/api/devices/\d+/selection",
            rf"/api/uploads/{_MOBILE_ID}/files/\d+",
        ),
        "DELETE": (
            rf"/api/device-groups/\d+",
            rf"/api/v1/mobile/push-installation/{_MOBILE_ID}",
        ),
    }
    return any(re.fullmatch(pattern, path) for pattern in patterns.get(method, ()))


def is_mobile_public_request(request: Request) -> bool:
    host = (request.url.hostname or "").casefold().rstrip(".")
    return bool(settings.mobile_public_hosts and host in settings.mobile_public_hosts)


@app.middleware("http")
async def protect_private_api(request: Request, call_next):
    public_download = (
        request.url.path.startswith(("/api/downloads/", "/api/device-downloads/"))
        and request.method in {"GET", "HEAD"}
    )
    path = request.url.path
    method = request.method
    mobile_public = is_mobile_public_request(request)
    if mobile_public and not mobile_public_route_allowed(method, path):
        # Avoid advertising the existence of admin and MCP routes on the public
        # native hostname.
        return JSONResponse(status_code=404, content={"detail": "Not found"})
    private_path = path.startswith("/api/") or path.startswith("/mcp")
    if private_path and request.method not in {"GET", "HEAD", "OPTIONS"}:
        if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
            return JSONResponse(status_code=403, content={"detail": "Cross-site requests are not allowed"})
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            return JSONResponse(status_code=403, content={"detail": "Request origin is not allowed"})
    public_api = path in {
        "/api/health",
        "/api/auth/login",
        "/api/v1/mobile/session",
    } or public_download
    if private_path and not public_api:
        principal = None
        authorization = request.headers.get("authorization", "")
        expected = f"Bearer {settings.access_token}"
        if (
            not mobile_public
            and settings.access_token
            and secrets.compare_digest(authorization, expected)
        ):
            principal = Principal(None, "bootstrap-admin", "Bootstrap administrator", "admin", True)
        if principal is None:
            bearer_token = (
                authorization.removeprefix("Bearer ").strip()
                if authorization.startswith("Bearer ")
                else ""
            )
            session_token = bearer_token or (
                "" if mobile_public else request.cookies.get("rommates_session", "")
            )
            principal = auth.from_session(session_token)
            if principal is not None:
                request.state.session_token = session_token
        if mobile_public and principal is not None and (
            principal.session_kind != "mobile" or principal.has_role("admin")
        ):
            principal = None
        if principal is None and settings.allow_anonymous and not mobile_public:
            principal = Principal(None, "proxy-admin", "Authenticated proxy user", "admin", True)
        if principal is None:
            return JSONResponse(status_code=401, content={"detail": "Sign in to continue"})
        request.state.principal = principal
        if principal.must_change_password and path not in {
            "/api/auth/me",
            "/api/auth/logout",
            "/api/auth/impersonation/end",
            "/api/auth/password",
            "/api/status",
            "/api/v1/mobile/bootstrap",
        }:
            return JSONResponse(
                status_code=403,
                content={
                    "detail": "Change your temporary password to continue",
                    "password_change_required": True,
                },
            )
        if path.startswith("/mcp") and not principal.has_role("admin"):
            return JSONResponse(status_code=403, content={"detail": "Administrator access is required"})
        if path.startswith("/api/") and not role_allows(principal, method, path):
            return JSONResponse(status_code=403, content={"detail": "Your role does not allow this action"})
    response = await call_next(request)
    if mobile_public:
        response.headers["X-ROMmates-Surface"] = "mobile"
    return response


@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; img-src 'self' blob: data:; base-uri 'none'; form-action 'self'; "
        "frame-ancestors 'none'; object-src 'none'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if (
        request.url.path.startswith(
            ("/api/artwork/assets/", "/api/artwork/thumbnails/")
        )
        and response.status_code == 200
    ):
        # Artwork is immutable at a versioned client URL and safe to retain in a
        # private browser cache. Keep this exception ahead of the general API
        # no-store policy or every thumbnail is downloaded again on each render.
        response.headers.setdefault(
            "Cache-Control", "private, max-age=31536000, immutable"
        )
        response.headers.setdefault("Vary", "Authorization, Cookie")
    elif request.url.path.startswith(("/api/", "/mcp")):
        response.headers["Cache-Control"] = "no-store"
    elif request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.exception_handler(LibraryError)
async def library_error_handler(_: Request, exc: LibraryError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(TransferError)
async def transfer_error_handler(_: Request, exc: TransferError):
    return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


@app.get("/", include_in_schema=False)
@app.get("/library", include_in_schema=False)
@app.get("/artwork", include_in_schema=False)
@app.get("/transfers", include_in_schema=False)
@app.get("/duplicates", include_in_schema=False)
@app.get("/naming", include_in_schema=False)
@app.get("/devices", include_in_schema=False)
@app.get("/saves", include_in_schema=False)
@app.get("/jobs", include_in_schema=False)
@app.get("/trash", include_in_schema=False)
@app.get("/notifications", include_in_schema=False)
@app.get("/account", include_in_schema=False)
@app.get("/users", include_in_schema=False)
def index():
    return HTMLResponse(INDEX_HTML, headers={"Cache-Control": "no-store"})


@app.get("/api/health")
def health():
    return {"status": "ok"}


@app.post("/api/auth/login")
def login(payload: LoginRequest, request: Request):
    principal, token, expires_at = auth.authenticate(
        payload.username,
        payload.password,
        request.client.host if request.client else "unknown",
        client_name="ROMmates web",
    )
    response = JSONResponse({"user": principal.payload(), "expires_at": expires_at})
    response.set_cookie(
        "rommates_session",
        token,
        max_age=expires_at - int(time.time()),
        httponly=True,
        secure=(
            settings.public_url.startswith("https://")
            or request.url.scheme == "https"
            or request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip() == "https"
        ),
        samesite="strict",
        path="/",
    )
    return response


@app.post("/api/v1/mobile/session")
def create_mobile_session(payload: MobileLoginRequest, request: Request):
    principal, token, expires_at = auth.authenticate(
        payload.username,
        payload.password,
        f"{request.client.host if request.client else 'unknown'}:ios",
        client_name=payload.client_name,
        session_kind="mobile",
    )
    return {
        "session_token": token,
        "expires_at": expires_at,
        "user": principal.payload(),
        "permissions": permission_payload(principal),
    }


@app.post("/api/auth/logout")
def logout(request: Request):
    auth.logout(request_session_token(request))
    response = JSONResponse({"signed_out": True})
    response.delete_cookie("rommates_session", path="/", samesite="strict")
    return response


@app.post("/api/auth/impersonate/{user_id}")
def impersonate_user(user_id: int, request: Request):
    principal = request_principal(request)
    if principal.id is None or principal.bootstrap or not principal.has_role("admin"):
        raise HTTPException(
            status_code=400,
            detail="Sign in with a named administrator account to use test view",
        )
    target = auth.begin_impersonation(
        request.cookies.get("rommates_session", ""), principal.id, user_id
    )
    db.activity(
        "user",
        f"Administrator {principal.username} entered test view as {target.username}",
    )
    return {"user": target.payload(), "impersonating": True}


@app.post("/api/auth/impersonation/end")
def end_impersonation(request: Request):
    principal = request_principal(request)
    if principal.impersonator_id is None:
        raise HTTPException(status_code=400, detail="Test view is not active")
    target_username = principal.username
    restored = auth.end_impersonation(request.cookies.get("rommates_session", ""))
    db.activity(
        "user",
        f"Administrator {restored.username} left test view for {target_username}",
    )
    return {"user": restored.payload(), "impersonating": False}


@app.post("/api/auth/password")
def change_password(payload: PasswordChangeRequest, request: Request):
    principal = request_principal(request)
    if principal.impersonator_id is not None:
        raise HTTPException(status_code=403, detail="Return to your administrator account to change credentials")
    if principal.id is None or principal.bootstrap:
        raise HTTPException(
            status_code=400,
            detail="Bootstrap access uses ROMMATES_ACCESS_TOKEN rather than an account password",
        )
    session_token = request_session_token(request)
    updated = auth.change_password(
        principal.id,
        payload.current_password,
        payload.new_password,
        session_token,
    )
    updated = auth.from_session(session_token) or updated
    db.activity("user", f"Changed password for account {updated.username}")
    return {"user": updated.payload(), "changed": True}


@app.patch("/api/auth/profile")
def update_profile(payload: ProfileUpdateRequest, request: Request):
    principal = request_principal(request)
    if principal.impersonator_id is not None:
        raise HTTPException(status_code=403, detail="Return to your administrator account to edit this profile")
    if principal.id is None or principal.bootstrap:
        raise HTTPException(
            status_code=400,
            detail="Bootstrap access does not have a personal profile",
        )
    updated = auth.update_profile(
        principal.id,
        payload.username,
        payload.display_name,
    )
    updated = auth.from_session(request_session_token(request)) or updated
    db.activity("user", f"Updated profile for account {updated.username}")
    return {"user": updated.payload(), "changed": True}


@app.get("/api/auth/me")
def current_user(request: Request):
    principal = request_principal(request)
    return {
        "user": principal.payload(),
        "roles": list(ROLES),
        "permissions": permission_payload(principal),
    }


@app.get("/api/v1/mobile/bootstrap")
def mobile_bootstrap(request: Request):
    principal = request_principal(request)
    if principal.id is None:
        raise HTTPException(status_code=403, detail="A personal account is required")
    return {
        "api_version": 1,
        "user": principal.payload(),
        "permissions": permission_payload(principal),
        "push": {
            "configured": mobile_push.configured,
            "bundle_id": settings.apns_bundle_id,
            "events": mobile_push.preferences(principal.id),
        },
    }


@app.put("/api/v1/mobile/push-installation")
def register_mobile_installation(payload: MobileInstallationRequest, request: Request):
    principal = request_principal(request)
    if principal.id is None:
        raise HTTPException(status_code=403, detail="A personal account is required")
    installation = mobile_push.register(
        principal.id,
        payload.installation_id,
        payload.device_token,
        payload.app_version,
        payload.notifications_enabled,
    )
    mobile_releases.announce_available(principal.id, payload.app_version)
    return installation


@app.delete("/api/v1/mobile/push-installation/{installation_id}")
def unregister_mobile_installation(installation_id: str, request: Request):
    principal = request_principal(request)
    if principal.id is None:
        raise HTTPException(status_code=403, detail="A personal account is required")
    return {
        "unregistered": mobile_push.unregister(principal.id, installation_id),
        "installation_id": installation_id,
    }


@app.put("/api/v1/mobile/push-preferences")
def update_mobile_push_preferences(
    payload: MobilePushPreferencesRequest,
    request: Request,
):
    principal = request_principal(request)
    if principal.id is None:
        raise HTTPException(status_code=403, detail="A personal account is required")
    return {
        "events": mobile_push.update_preferences(principal.id, payload.events),
        "supported_events": list(PUSH_EVENTS),
    }


@app.get("/api/v1/mobile/releases")
def mobile_release_manifest(request: Request, build: int = Query(ge=1)):
    principal = request_principal(request)
    if principal.id is None:
        raise HTTPException(status_code=403, detail="A personal account is required")
    return mobile_releases.manifest(build)


@app.post("/api/mobile/releases", status_code=201)
def publish_mobile_release(payload: MobileReleaseRequest):
    result = mobile_releases.publish(payload.build, payload.version, payload.notes)
    db.activity(
        "mobile_release",
        f"Published ROMmates {payload.version.strip()} build {payload.build}",
    )
    return result


@app.get("/api/account/summary")
def account_summary(request: Request):
    principal = request_principal(request)
    if principal.id is None:
        return {
            "user": principal.payload(),
            "devices": [],
            "platforms": [],
            "total_synced_roms": 0,
            "unique_synced_roms": 0,
        }
    with db.connect() as connection:
        devices = [
            dict(row)
            for row in connection.execute(
                "SELECT d.id,d.name,d.delivery_mode,d.syncthing_ready_at,"
                "rg.id AS group_id,rg.name AS group_name,"
                "COUNT(DISTINCT ds.game_id) AS selected_roms,"
                "COUNT(DISTINCT dp.game_id) AS synced_roms "
                "FROM devices d "
                "LEFT JOIN device_roster_groups rg ON rg.id=d.roster_group_id "
                "LEFT JOIN device_selections ds ON ds.device_id=d.id "
                "LEFT JOIN deployments dp ON dp.device_id=d.id "
                "WHERE d.owner_user_id=? "
                "GROUP BY d.id,d.name,d.delivery_mode,d.syncthing_ready_at,rg.id,rg.name "
                "ORDER BY d.name COLLATE NOCASE",
                (principal.id,),
            )
        ]
        platforms = [
            dict(row)
            for row in connection.execute(
                "SELECT g.platform,COUNT(*) AS synced_roms "
                "FROM (SELECT DISTINCT dp.device_id,dp.game_id FROM deployments dp "
                "JOIN devices d ON d.id=dp.device_id WHERE d.owner_user_id=?) deployed "
                "JOIN games g ON g.id=deployed.game_id "
                "GROUP BY g.platform ORDER BY synced_roms DESC,g.platform COLLATE NOCASE",
                (principal.id,),
            )
        ]
        unique_synced_roms = connection.execute(
            "SELECT COUNT(DISTINCT dp.game_id) AS count FROM deployments dp "
            "JOIN devices d ON d.id=dp.device_id WHERE d.owner_user_id=?",
            (principal.id,),
        ).fetchone()["count"]
    return {
        "user": principal.payload(),
        "devices": devices,
        "platforms": platforms,
        "total_synced_roms": sum(int(item["synced_roms"]) for item in platforms),
        "unique_synced_roms": int(unique_synced_roms),
    }


@app.get("/api/onboarding")
def onboarding_progress(request: Request, tour_key: str = Query(default="getting-started", min_length=1, max_length=64, pattern="^[a-z0-9_-]+$")):
    principal = request_principal(request)
    if principal.id is None:
        return {"tour_key": tour_key, "tour_version": 1, "current_step": 0, "dismissed": False, "completed": False, "persistent": False}
    with db.connect() as connection:
        row = connection.execute(
            "SELECT tour_key,tour_version,current_step,dismissed,completed,updated_at "
            "FROM user_onboarding WHERE user_id=? AND tour_key=?",
            (principal.id, tour_key),
        ).fetchone()
    if not row:
        return {"tour_key": tour_key, "tour_version": 1, "current_step": 0, "dismissed": False, "completed": False, "persistent": True}
    payload = dict(row)
    payload["dismissed"] = bool(payload["dismissed"])
    payload["completed"] = bool(payload["completed"])
    payload["persistent"] = True
    return payload


@app.patch("/api/onboarding")
def update_onboarding_progress(payload: OnboardingUpdateRequest, request: Request):
    principal = request_principal(request)
    result = {**payload.model_dump(), "persistent": principal.id is not None}
    if principal.id is None:
        return result
    with db.write() as connection:
        connection.execute(
            "INSERT INTO user_onboarding(user_id,tour_key,tour_version,current_step,dismissed,completed,updated_at) "
            "VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(user_id,tour_key) DO UPDATE SET "
            "tour_version=excluded.tour_version,current_step=excluded.current_step,dismissed=excluded.dismissed,"
            "completed=excluded.completed,updated_at=CURRENT_TIMESTAMP",
            (principal.id, payload.tour_key, payload.tour_version, payload.current_step, int(payload.dismissed), int(payload.completed)),
        )
    return result


@app.get("/api/users")
def users():
    return {"items": auth.list_users(), "roles": list(ROLES)}


@app.post("/api/users", status_code=201)
def create_user(payload: UserCreateRequest):
    roles = payload.roles or ([payload.role] if payload.role else [])
    user = auth.create_user(
        payload.username, payload.display_name, payload.password, roles
    )
    db.activity("user", f"Created account {user['username']} ({', '.join(user['roles'])})")
    return user


@app.patch("/api/users/{user_id}")
def update_user(user_id: int, payload: UserUpdateRequest, request: Request):
    principal = request_principal(request)
    user = auth.update_user(
        user_id,
        role=payload.role,
        roles=payload.roles,
        active=payload.active,
        password=payload.password,
        actor_id=principal.id,
    )
    db.activity("user", f"Updated account {user['username']} ({', '.join(user['roles'])})")
    return user


@app.get("/api/notifications")
def notification_settings():
    return notifications.settings_payload()


@app.put("/api/notifications/settings")
def update_notification_settings(payload: NotificationSettingsRequest):
    return notifications.update_settings(payload.enabled, payload.events)


@app.post("/api/notifications/test", status_code=202)
def test_notification():
    return notifications.test()


@app.get("/api/inbox")
def user_inbox(request: Request, limit: int = Query(30, ge=1, le=100)):
    principal = request_principal(request)
    if principal.id is None:
        return {"items": [], "unread": 0}
    with db.connect() as connection:
        items = [dict(row) for row in connection.execute(
            "SELECT id,kind,title,detail,path,read_at,created_at FROM user_notifications "
            "WHERE user_id=? AND kind<>'new_build' ORDER BY id DESC LIMIT ?", (principal.id, limit)
        )]
        unread = connection.execute(
            "SELECT COUNT(*) AS count FROM user_notifications "
            "WHERE user_id=? AND kind<>'new_build' AND read_at IS NULL",
            (principal.id,),
        ).fetchone()["count"]
    return {"items": items, "unread": unread}


@app.post("/api/inbox/{notification_id}/read")
def read_user_notification(notification_id: int, request: Request):
    principal = request_principal(request)
    if principal.id is None:
        raise HTTPException(status_code=404, detail="Notification was not found")
    with db.write() as connection:
        row = connection.execute(
            "SELECT id FROM user_notifications WHERE id=? AND user_id=?",
            (notification_id, principal.id),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Notification was not found")
        connection.execute(
            "UPDATE user_notifications SET read_at=COALESCE(read_at,CURRENT_TIMESTAMP) WHERE id=?",
            (notification_id,),
        )
    return {"read": notification_id}


@app.post("/api/inbox/read-all")
def read_all_user_notifications(request: Request):
    principal = request_principal(request)
    if principal.id is None:
        return {"updated": 0}
    with db.write() as connection:
        cursor = connection.execute(
            "UPDATE user_notifications SET read_at=CURRENT_TIMESTAMP "
            "WHERE user_id=? AND kind<>'new_build' AND read_at IS NULL", (principal.id,)
        )
    return {"updated": cursor.rowcount}


@app.get("/api/status")
def status(request: Request):
    principal = request_principal(request)
    with db.connect() as connection:
        counts = connection.execute(
            "SELECT COUNT(*) AS games,COUNT(DISTINCT platform) AS platforms,COALESCE(SUM(size),0) AS bytes FROM games"
        ).fetchone()
        if principal.has_role("admin"):
            devices = connection.execute("SELECT COUNT(*) AS count FROM devices").fetchone()["count"]
            current_job = connection.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        elif principal.has_role("member") and principal.id is not None:
            devices = connection.execute(
                "SELECT COUNT(*) AS count FROM devices WHERE owner_user_id=?", (principal.id,)
            ).fetchone()["count"]
            current_job = connection.execute(
                "SELECT * FROM jobs WHERE requested_by=? ORDER BY id DESC LIMIT 1",
                (principal.id,),
            ).fetchone()
        else:
            devices = 0
            current_job = None
        trash = connection.execute("SELECT COUNT(*) AS count FROM trash_items").fetchone()["count"]
        duplicates = connection.execute(
            "SELECT COALESCE(SUM(item_count),0) AS count FROM "
            "(SELECT COUNT(*) AS item_count FROM games GROUP BY bundle_hash HAVING COUNT(*)>1)"
        ).fetchone()["count"]
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
            "library": str(settings.library_root) if principal.has_role("admin") else "ROM library",
            "devices": str(settings.devices_root) if principal.has_role("admin") else "Device library",
            "trash": str(settings.trash_root) if principal.has_role("admin") else "Trash",
            "saves": str(settings.saves_root) if principal.has_role("admin") else "Save vault",
            "snapshots": str(settings.snapshots_root) if principal.has_role("admin") else "Snapshots",
            "media": str(settings.media_root) if principal.has_role("admin") else "Artwork cache",
        },
        "screenscraper": screenscraper.status(),
        "rawg": {"configured": ranking_service.configured},
        "syncthing": {"configured": syncthing.configured},
        "user": principal.payload(),
    }


@app.get("/api/syncthing/status")
def syncthing_status(request: Request, refresh: bool = False):
    principal = request_principal(request)
    payload = dict(syncthing.status(refresh=refresh))
    if principal.has_role("admin"):
        return payload
    with db.connect() as connection:
        owned_names = {
            str(row["name"]).casefold()
            for row in connection.execute(
                "SELECT name FROM devices WHERE owner_user_id=?", (principal.id,)
            )
        }
    visible = [
        item
        for item in payload.get("devices", [])
        if isinstance(item, dict) and str(item.get("name") or "").casefold() in owned_names
    ]
    payload["devices"] = visible
    payload["online"] = sum(1 for item in visible if item.get("connected"))
    payload["total"] = len(visible)
    payload.pop("local_device_id", None)
    return payload


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
        "syncthing": syncthing.peek(),
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
    request: Request,
    search: str = "",
    platform: str = "",
    duplicate: str = Query("all", pattern="^(all|exact|possible|unique)$"),
    device_id: int | None = None,
    device_scope: str = Query("all", pattern="^(all|selected|on_device|changes)$"),
    refresh_device_inventory: bool = False,
    sort: str = Query(
        "name_asc",
        pattern="^(name_asc|name_desc|rank_asc|rating_desc|rating_asc|size_desc|size_asc)$",
    ),
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    principal = request_principal(request)
    if device_id is not None:
        if not can_manage_devices(principal):
            raise HTTPException(status_code=403, detail="Device management access is required")
        require_device_access(device_id, principal)
    if device_scope != "all" and device_id is None:
        raise HTTPException(status_code=400, detail="A device is required for this view")
    def visible_device(alias: str) -> str:
        if principal.has_role("admin"):
            return "1=1"
        if principal.has_role("member") and principal.id is not None:
            return f"{alias}.owner_user_id={int(principal.id)}"
        return "0=1"
    # A requested refresh walks the filesystem once and publishes its result to
    # device_inventory_files. Normal reads consume that table directly instead
    # of pulling thousands of paths into Python and inserting them back into a
    # temporary table one row at a time.
    if device_id is not None and refresh_device_inventory and offset == 0:
        library.device_inventory(device_id, refresh=True)
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
        elif device_scope == "selected":
            where.append(f"({selected_expr})")
        elif device_scope == "changes":
            where.append(f"(({selected_expr}) != ({synced_expr}))")
    else:
        params_for_select = []
    where_sql = " AND ".join(where)
    order_sql = {
        "name_asc": "g.display_name COLLATE NOCASE ASC,g.platform ASC,g.id ASC",
        "name_desc": "g.display_name COLLATE NOCASE DESC,g.platform ASC,g.id ASC",
        "rank_asc": "pr.rank IS NULL ASC,pr.rank ASC,g.display_name COLLATE NOCASE ASC,g.id ASC",
        "rating_desc": "gm.rating IS NULL ASC,gm.rating DESC,g.display_name COLLATE NOCASE ASC,g.id ASC",
        "rating_asc": "gm.rating IS NULL ASC,gm.rating ASC,g.display_name COLLATE NOCASE ASC,g.id ASC",
        "size_desc": "g.size DESC,g.display_name COLLATE NOCASE ASC,g.id ASC",
        "size_asc": "g.size ASC,g.display_name COLLATE NOCASE ASC,g.id ASC",
    }[sort]
    platform_rank_expr = (
        "CASE WHEN gm.rating IS NULL THEN NULL ELSE 1+(SELECT COUNT(*) FROM game_metadata rm "
        "JOIN games rg ON rg.id=rm.game_id WHERE rg.platform=g.platform AND rm.rating>gm.rating) END"
    )
    present_device_states: list[dict[str, object]] = []
    with db.connect() as connection:
        if device_id is not None:
            connection.execute("CREATE TEMP TABLE device_present(relpath TEXT PRIMARY KEY)")
            connection.execute("CREATE TEMP TABLE device_selected(game_id INTEGER PRIMARY KEY)")
            connection.execute("CREATE TEMP TABLE device_managed(game_id INTEGER,relpath TEXT,PRIMARY KEY(game_id,relpath))")
            connection.execute(
                "INSERT OR IGNORE INTO device_present(relpath) "
                "SELECT relpath FROM device_inventory_files WHERE device_id=?",
                (device_id,),
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
            f"(SELECT COUNT(*) FROM device_selections ds JOIN devices dc ON dc.id=ds.device_id "
            f"WHERE ds.game_id=g.id AND {visible_device('dc')}) AS device_count,"
            f"(SELECT id FROM game_assets ga WHERE ga.game_id=g.id AND ga.kind='cover' LIMIT 1) AS cover_asset_id,"
            f"(SELECT substr(sha256,1,16) FROM game_assets ga WHERE ga.game_id=g.id AND ga.kind='cover' LIMIT 1) AS cover_asset_version,"
            f"(SELECT COUNT(*) FROM game_assets ga WHERE ga.game_id=g.id) AS artwork_count,"
            f"gm.rating AS rating,gm.top_staff AS top_staff,({platform_rank_expr}) AS platform_rank,"
            f"pr.rank AS rawg_rank,pr.score AS rawg_score,"
            f"({selected_expr}) AS selected,({present_expr}) AS on_device,"
            f"({managed_expr}) AS managed,({synced_expr}) AS synced FROM games g "
            f"LEFT JOIN game_metadata gm ON gm.game_id=g.id "
            f"LEFT JOIN platform_rankings pr ON pr.matched_game_id=g.id "
            f"AND pr.match_method='exact' WHERE {where_sql} "
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
                f"WHERE ds.game_id IN ({placeholders}) AND {visible_device('d')} "
                "ORDER BY d.name COLLATE NOCASE",
                game_ids,
            ).fetchall()
            removal_states = connection.execute(
                f"SELECT DISTINCT dp.game_id,d.id AS device_id,d.name,'pending_remove' AS state "
                "FROM deployments dp JOIN devices d ON d.id=dp.device_id "
                "WHERE NOT EXISTS(SELECT 1 FROM device_selections ds WHERE ds.device_id=dp.device_id AND ds.game_id=dp.game_id) "
                f"AND dp.game_id IN ({placeholders}) AND {visible_device('d')} "
                "ORDER BY d.name COLLATE NOCASE",
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
                present_device_states = [
                    dict(row)
                    for row in connection.execute(
                        "SELECT DISTINCT gf.game_id,dif.device_id,d.name "
                        "FROM game_files gf JOIN device_inventory_files dif "
                        "ON dif.relpath=gf.device_relpath "
                        "JOIN devices d ON d.id=dif.device_id "
                        f"WHERE gf.game_id IN ({placeholders}) AND {visible_device('d')} "
                        "ORDER BY d.name COLLATE NOCASE",
                        game_ids,
                    )
                ]
        device_inventory = None
        if device_id is not None:
            scope_expr = {
                "on_device": present_expr,
                "selected": selected_expr,
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
            inventory_stats = connection.execute(
                "SELECT COUNT(*) AS files,COALESCE(SUM(size),0) AS bytes "
                "FROM device_inventory_files "
                "WHERE device_id=?",
                (device_id,),
            ).fetchone()
            device_inventory = {
                "present_games": present_games,
                "changes": changes,
                "unmatched_files": unmatched_files,
                "files": int(inventory_stats["files"] or 0),
                "bytes": int(inventory_stats["bytes"] or 0),
                "platforms": [
                    dict(row)
                    for row in connection.execute(
                        f"SELECT g.platform,COUNT(*) AS count FROM games g "
                        f"WHERE {scope_expr} GROUP BY g.platform "
                        "ORDER BY g.platform COLLATE NOCASE"
                    )
                ],
                "present_platforms": [
                    dict(row)
                    for row in connection.execute(
                        f"SELECT g.platform,COUNT(*) AS count,COALESCE(SUM(g.size),0) AS bytes "
                        f"FROM games g WHERE {present_expr} GROUP BY g.platform "
                        "ORDER BY g.platform COLLATE NOCASE"
                    )
                ],
                "selected_platforms": [
                    dict(row)
                    for row in connection.execute(
                        f"SELECT g.platform,COUNT(*) AS count,COALESCE(SUM(g.size),0) AS bytes "
                        f"FROM games g WHERE {selected_expr} GROUP BY g.platform "
                        "ORDER BY g.platform COLLATE NOCASE"
                    )
                ],
            }
    if items and device_id is None and present_device_states:
        items_by_id = {int(item["id"]): item for item in items}
        states_by_game = {
            int(item["id"]): {int(device["id"]): device for device in item["devices"]}
            for item in items
        }
        for present_state in present_device_states:
            game_id = int(present_state["game_id"])
            device_key = int(present_state["device_id"])
            existing = states_by_game[game_id].get(device_key)
            if existing:
                if existing["state"] == "pending_add":
                    existing["state"] = "present"
                continue
            present = {
                "id": device_key,
                "name": present_state["name"],
                "state": "present",
            }
            items_by_id[game_id]["devices"].append(present)
            states_by_game[game_id][device_key] = present
        for item in items:
            item["devices"].sort(key=lambda device: str(device["name"]).casefold())
            item["device_count"] = len(item["devices"])
    if not can_manage_devices(principal):
        for item in items:
            item["devices"] = []
            item["device_count"] = 0
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
        device["name"]: library.device_inventory(device["id"], refresh=False)
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
def game_detail(game_id: int, request: Request):
    principal = request_principal(request)
    game, files = library.game_bundle(game_id)
    devices: list[dict[str, object]] = []
    impact = {"status": "none", "groups": 0, "files": 0, "save_files": 0, "state_files": 0, "paths": [], "content_names": []}
    if can_manage_devices(principal):
        with db.connect() as connection:
            owner_filter = "" if principal.has_role("admin") else "WHERE d.owner_user_id=?"
            owner_params = [] if principal.has_role("admin") else [principal.id]
            devices = [dict(row) for row in connection.execute(
                "SELECT d.id,d.name,EXISTS(SELECT 1 FROM device_selections ds WHERE ds.device_id=d.id AND ds.game_id=?) AS selected "
                f"FROM devices d {owner_filter} ORDER BY d.name", [game_id, *owner_params]
            )]
    if principal.has_role("admin"):
        impact = saves.save_impacts([game_id]).get(game_id, impact)
    return {"game": game, "files": files, "devices": devices, "artwork": screenscraper.detail(game_id), "save_impact": impact}


@app.get("/api/artwork/status")
def artwork_status():
    return {**screenscraper.status(), "thumbnail_cache": artwork_thumbnails.status()}


@app.get("/api/artwork/manifest")
def artwork_manifest():
    with db.connect() as connection:
        summary = connection.execute(
            "SELECT COUNT(*) AS count,COALESCE(SUM(size),0) AS bytes FROM game_assets "
            "WHERE kind='cover'"
        ).fetchone()
    return {
        "covers": summary["count"],
        "original_bytes": summary["bytes"],
        "thumbnail_version": THUMBNAIL_CACHE_VERSION,
        "thumbnail_cache": artwork_thumbnails.status(),
    }


@app.get("/api/artwork/bulk")
def artwork_bulk_status():
    return screenscraper.bulk_status()


@app.get("/api/artwork/runs")
def artwork_runs(limit: int = Query(100, ge=1, le=500)):
    return screenscraper.bulk_runs(limit)


@app.post("/api/artwork/scrape-all", status_code=202)
def scrape_all_artwork(payload: ArtworkBulkRequest):
    if not screenscraper.configured:
        raise HTTPException(
            status_code=400,
            detail="ScreenScraper developer credentials are not configured",
        )
    if payload.platforms and payload.game_ids:
        raise HTTPException(status_code=400, detail="Choose platforms or ROMs, not both")
    run, created = screenscraper.create_bulk_run(
        payload.asset_mode,
        platforms=payload.platforms,
        game_ids=payload.game_ids,
    )
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
            f"Downloading missing {'covers' if payload.asset_mode == 'cover' else 'artwork'} for "
            f"{run['total_games']} games: {run['scope_label']}",
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
    return FileResponse(
        path,
        media_type=content_type,
        headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            "Vary": "Authorization, Cookie",
        },
    )


@app.get("/api/artwork/thumbnails/{asset_id}")
def artwork_thumbnail(asset_id: int):
    path, content_type = artwork_thumbnails.ensure(asset_id)
    return FileResponse(
        path,
        media_type=content_type,
        headers={
            "Cache-Control": "private, max-age=31536000, immutable",
            "Vary": "Authorization, Cookie",
        },
    )


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
def devices(request: Request):
    principal = request_principal(request)
    with db.connect() as connection:
        owner_filter = "" if principal.has_role("admin") else "WHERE d.owner_user_id=?"
        params = [] if principal.has_role("admin") else [principal.id]
        rows = connection.execute(
            "SELECT d.*,rg.name AS roster_group_name,"
            "rg.owner_user_id AS roster_group_owner_user_id,"
            "u.username AS owner_username,u.display_name AS owner_display_name,"
            "(SELECT COUNT(*) FROM device_selections ds WHERE ds.device_id=d.id) AS selected_games,"
            "(SELECT COUNT(DISTINCT dp.game_id) FROM deployments dp WHERE dp.device_id=d.id) AS deployed_games "
            "FROM devices d "
            "LEFT JOIN device_roster_groups rg ON rg.id=d.roster_group_id "
            "LEFT JOIN users u ON u.id=d.owner_user_id "
            f"{owner_filter} ORDER BY d.name COLLATE NOCASE",
            params,
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/device-groups")
def device_groups(request: Request):
    principal = request_principal(request)
    with db.connect() as connection:
        owner_filter = "" if principal.has_role("admin") else "WHERE rg.owner_user_id=?"
        params = [] if principal.has_role("admin") else [principal.id]
        groups = connection.execute(
            "SELECT rg.*,u.username AS owner_username,u.display_name AS owner_display_name,"
            "COUNT(DISTINCT d.id) AS device_count,"
            "COUNT(DISTINCT ds.game_id) AS selected_games "
            "FROM device_roster_groups rg "
            "LEFT JOIN users u ON u.id=rg.owner_user_id "
            "LEFT JOIN devices d ON d.roster_group_id=rg.id "
            "LEFT JOIN device_selections ds ON ds.device_id=d.id "
            f"{owner_filter} GROUP BY rg.id ORDER BY rg.name COLLATE NOCASE",
            params,
        ).fetchall()
        result = []
        for group in groups:
            members = connection.execute(
                "SELECT id,name,path,deployment_mode,delivery_mode,syncthing_ready_at "
                "FROM devices WHERE roster_group_id=? ORDER BY name COLLATE NOCASE",
                (group["id"],),
            ).fetchall()
            result.append({**dict(group), "members": [dict(member) for member in members]})
    return result


@app.post("/api/device-groups", status_code=201)
def create_device_group(payload: DeviceGroupCreateRequest, request: Request):
    principal = request_principal(request)
    device_ids = sorted({payload.source_device_id, *payload.member_device_ids})
    for device_id in device_ids:
        require_device_access(device_id, principal)
    return library.create_device_group(
        payload.name, payload.source_device_id, payload.member_device_ids
    )


@app.post("/api/devices", status_code=201)
def create_device(payload: DeviceCreateRequest, request: Request):
    principal = request_principal(request)
    if payload.keep_in_sync and payload.clone_device_id is None:
        raise HTTPException(status_code=400, detail="Choose a device to clone before linking rosters")
    source_device = None
    if payload.clone_device_id is not None:
        source_device = require_device_access(payload.clone_device_id, principal)
    owner_user_id = (
        principal.id
        if principal.has_role("member") and not principal.has_role("admin")
        else source_device["owner_user_id"] if source_device is not None else None
    )
    device = library.create_device(
        payload.name,
        "hardlink",
        owner_user_id,
        payload.delivery_mode,
        payload.storage_capacity_bytes,
    )
    owner_label = principal.display_name or principal.username or "An administrator"
    if payload.delivery_mode == "syncthing":
        notifications.notify(
            "device_setup_required",
            f"Syncthing setup needed for {device['name']}",
            f"{owner_label} created this device. Add {device['roms_path']} to Syncthing, then mark the device ready in ROMmates.",
            "devices",
            dedupe_key=f"device:{device['id']}:setup-required",
        )
    if payload.clone_device_id is not None:
        clone = library.clone_device_roster(
            payload.clone_device_id, int(device["id"]), payload.keep_in_sync
        )
        with db.connect() as connection:
            refreshed = connection.execute(
                "SELECT * FROM devices WHERE id=?", (device["id"],)
            ).fetchone()
        device = {**device, **dict(refreshed), "cloned_games": clone["games"]}
    return device


@app.post("/api/devices/{device_id}/roster-link")
def link_device_rosters(
    device_id: int, payload: DeviceRosterLinkRequest, request: Request
):
    principal = request_principal(request)
    require_device_access(device_id, principal)
    target_ids = sorted(set(payload.target_device_ids))
    for target_id in target_ids:
        require_device_access(target_id, principal)
    return library.link_device_rosters(device_id, target_ids)


@app.post("/api/devices/{device_id}/roster-clone")
def clone_device_roster(
    device_id: int, payload: DeviceRosterCloneRequest, request: Request
):
    principal = request_principal(request)
    require_device_access(device_id, principal)
    require_device_access(payload.source_device_id, principal)
    if payload.source_device_id == device_id:
        raise HTTPException(status_code=400, detail="Choose a different source device")
    return library.clone_device_roster(payload.source_device_id, device_id)


@app.delete("/api/devices/{device_id}/roster-link")
def unlink_device_roster(device_id: int, request: Request):
    require_device_access(device_id, request_principal(request))
    return library.unlink_device_roster(device_id)


@app.put("/api/device-groups/{group_id}")
def update_device_group(
    group_id: int, payload: DeviceGroupUpdateRequest, request: Request
):
    principal = request_principal(request)
    require_device_group_access(group_id, principal)
    name = payload.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Enter a group name")
    with db.write() as connection:
        connection.execute(
            "UPDATE device_roster_groups SET name=? WHERE id=?", (name, group_id)
        )
    db.activity("device", f"Renamed device group {group_id} to {name}")
    return {"id": group_id, "name": name}


@app.delete("/api/device-groups/{group_id}")
def delete_device_group(group_id: int, request: Request):
    require_device_group_access(group_id, request_principal(request))
    return library.delete_device_group(group_id)


@app.post("/api/device-groups/{group_id}/apply", status_code=202)
def apply_device_group(group_id: int, request: Request):
    principal = request_principal(request)
    group, members = require_device_group_access(group_id, principal)
    jobs = [queue_device_apply_job(int(member["id"]), principal.id) for member in members]
    db.activity("device_apply", f"Queued all devices in {group['name']}")
    return {
        "group_id": group_id,
        "devices": len(members),
        "job_ids": [int(job["job_id"]) for job in jobs],
    }


@app.put("/api/devices/{device_id}/syncthing-ready")
def update_device_syncthing_ready(
    device_id: int, payload: DeviceSyncthingReadyRequest, request: Request
):
    principal = request_principal(request)
    require_device_access(device_id, principal)
    with db.write() as connection:
        device = connection.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        if not device:
            raise HTTPException(status_code=404, detail="Device was not found")
        if device["delivery_mode"] != "syncthing":
            raise HTTPException(status_code=400, detail="This device uses manual downloads")
        if payload.ready and not device["owner_user_id"]:
            raise HTTPException(status_code=400, detail="Assign an owner before marking Syncthing ready")
        connection.execute(
            "UPDATE devices SET syncthing_ready_at=CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END,"
            "syncthing_ready_by=CASE WHEN ? THEN ? ELSE NULL END WHERE id=?",
            (int(payload.ready), int(payload.ready), principal.id, device_id),
        )
        if payload.ready:
            connection.execute(
                "INSERT INTO user_notifications(user_id,kind,title,detail,path,dedupe_key) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(user_id,dedupe_key) DO UPDATE SET "
                "title=excluded.title,detail=excluded.detail,path=excluded.path,read_at=NULL,created_at=CURRENT_TIMESTAMP",
                (
                    device["owner_user_id"], "device_ready",
                    f"{device['name']} is ready to sync",
                    "An administrator finished the Syncthing setup. You can now select games and apply changes from Devices.",
                    "devices", f"device:{device_id}:ready",
                ),
            )
    if payload.ready:
        mobile_push.enqueue_existing(
            int(device["owner_user_id"]),
            f"device:{device_id}:ready",
        )
    db.activity(
        "device",
        f"Marked {device['name']} Syncthing {'ready' if payload.ready else 'pending'}",
    )
    return {"device_id": device_id, "ready": payload.ready}


@app.get("/api/devices/{device_id}/sync-status")
def device_syncthing_status(
    device_id: int,
    request: Request,
    tracked_only: bool = Query(False),
):
    device = require_device_access(device_id, request_principal(request))
    sync_run = device_syncs.latest(device_id)
    if tracked_only and sync_run and str(sync_run["state"]) in {"pending", "syncing", "offline"}:
        state = str(sync_run["state"])
        return {
            "configured": syncthing.configured,
            "linked": True,
            "connected": state != "offline",
            "completion": sync_run["completion"],
            "need_bytes": sync_run["need_bytes"],
            "need_items": sync_run["need_items"],
            "need_deletes": sync_run["need_deletes"],
            "status": sync_run["detail"],
            "last_sync": sync_run["completed_at"],
            "sync_run": sync_run,
        }
    if device["delivery_mode"] != "syncthing":
        return {
            "configured": syncthing.configured,
            "linked": False,
            "status": "Manual download",
            "last_sync": None,
            "sync_run": sync_run,
        }
    result = dict(syncthing.device_sync_status(
        str(device["name"]),
        remote_device_id=str(device["syncthing_device_id"] or ""),
        folder_id=str(device["syncthing_folder_id"] or ""),
    ))
    if result.get("linked"):
        device_syncs.remember_link(device_id, result)
    result["sync_run"] = sync_run
    return result


@app.post("/api/devices/{device_id}/syncthing-share")
def share_device_with_syncthing(
    device_id: int, payload: DeviceSyncthingShareRequest, request: Request
):
    principal = request_principal(request)
    device = require_device_access(device_id, principal)
    if device["delivery_mode"] != "syncthing":
        raise HTTPException(status_code=400, detail="This device uses manual downloads")
    try:
        result = syncthing.share_device_folder(
            str(device["name"]),
            payload.device_id,
            folder_id=f"rommates-device-{device_id}",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with db.write() as connection:
        connection.execute(
            "UPDATE devices SET syncthing_device_id=?,syncthing_folder_id=?,"
            "syncthing_ready_at=CURRENT_TIMESTAMP,syncthing_ready_by=? WHERE id=?",
            (result["device_id"], result["folder_id"], principal.id, device_id),
        )
    db.activity(
        "device",
        f"Shared {device['name']} with Syncthing device {str(result['device_id'])[:7]}",
    )
    return result


@app.put("/api/devices/{device_id}/owner")
def update_device_owner(device_id: int, payload: DeviceOwnerRequest, request: Request):
    principal = request_principal(request)
    require_device_access(device_id, principal)
    with db.write() as connection:
        current_device = connection.execute(
            "SELECT roster_group_id,owner_user_id FROM devices WHERE id=?", (device_id,)
        ).fetchone()
        if (
            current_device
            and current_device["roster_group_id"]
            and payload.owner_user_id != current_device["owner_user_id"]
        ):
            raise HTTPException(
                status_code=409,
                detail="Unlink this device's shared roster before changing its owner",
            )
        owner = None
        if payload.owner_user_id is not None:
            owner = connection.execute(
                "SELECT id,username,display_name,role,active FROM users WHERE id=?",
                (payload.owner_user_id,),
            ).fetchone()
            owner_roles = {
                row["role"]
                for row in connection.execute(
                    "SELECT role FROM user_roles WHERE user_id=?", (payload.owner_user_id,)
                )
            } if owner else set()
            if not owner or not owner["active"] or not owner_roles.intersection({"member", "admin"}):
                raise HTTPException(
                    status_code=400,
                    detail="Choose an active member or administrator",
                )
        connection.execute(
            "UPDATE devices SET owner_user_id=? WHERE id=?",
            (payload.owner_user_id, device_id),
        )
    owner_label = owner["display_name"] if owner else "administrators"
    db.activity("device", f"Assigned device {device_id} to {owner_label}")
    return {"device_id": device_id, "owner_user_id": payload.owner_user_id}


@app.put("/api/devices/{device_id}/storage-capacity")
def update_device_storage_capacity(
    device_id: int, payload: DeviceStorageCapacityRequest, request: Request
):
    device = require_device_access(device_id, request_principal(request))
    with db.write() as connection:
        connection.execute(
            "UPDATE devices SET storage_capacity_bytes=? WHERE id=?",
            (payload.storage_capacity_bytes, device_id),
        )
    detail = (
        f"Set {device['name']} ROM storage capacity to {payload.storage_capacity_bytes} bytes"
        if payload.storage_capacity_bytes
        else f"Cleared {device['name']} ROM storage capacity"
    )
    db.activity("device", detail)
    return {
        "device_id": device_id,
        "storage_capacity_bytes": payload.storage_capacity_bytes,
    }


@app.put("/api/devices/{device_id}/selection")
def update_selection(device_id: int, payload: SelectionRequest, request: Request):
    require_device_access(device_id, request_principal(request))
    library.set_selection(device_id, payload.game_id, payload.selected)
    return {"selected": payload.selected}


@app.put("/api/devices/{device_id}/selections")
def update_selections(device_id: int, payload: BulkSelectionRequest, request: Request):
    require_device_access(device_id, request_principal(request))
    updated = library.set_selections(device_id, payload.game_ids, payload.selected)
    return {"selected": payload.selected, "updated": updated}


@app.post("/api/devices/{device_id}/discard-changes")
def discard_device_changes(device_id: int, request: Request):
    require_device_access(device_id, request_principal(request))
    return library.discard_device_changes(device_id)


@app.put("/api/devices/{device_id}/deployment-mode")
def update_device_deployment_mode(
    device_id: int, payload: DeviceDeploymentModeRequest, request: Request
):
    require_device_access(device_id, request_principal(request))
    library.set_device_deployment_mode(device_id, payload.mode)
    return {"mode": payload.mode}


def device_summary_payload(device_id: int) -> dict[str, object]:
    """Return the database-backed device summary without touching ROM files."""
    with db.connect() as connection:
        device = connection.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        if not device:
            raise HTTPException(status_code=404, detail="Device was not found")
        desired = connection.execute(
            "SELECT COUNT(DISTINCT game_id) AS games,COUNT(*) AS files,"
            "COALESCE(SUM(size),0) AS bytes FROM "
            "(SELECT ds.game_id,gf.device_relpath,gf.size FROM device_selections ds "
            "JOIN game_files gf ON gf.game_id=ds.game_id WHERE ds.device_id=?)",
            (device_id,),
        ).fetchone()
        managed = connection.execute(
            "SELECT COUNT(*) AS files,COALESCE(SUM(gf.size),0) AS bytes FROM deployments dp "
            "JOIN game_files gf ON gf.game_id=dp.game_id AND gf.device_relpath=dp.relpath "
            "WHERE dp.device_id=?",
            (device_id,),
        ).fetchone()
        inventory_bytes = connection.execute(
            "SELECT COALESCE(SUM(size),0) AS bytes FROM device_inventory_files WHERE device_id=?",
            (device_id,),
        ).fetchone()["bytes"]
        retained_unmanaged_bytes = connection.execute(
            "SELECT COALESCE(SUM(dif.size),0) AS bytes FROM device_inventory_files dif "
            "LEFT JOIN (SELECT relpath FROM deployments WHERE device_id=? UNION "
            "SELECT gf.device_relpath AS relpath FROM device_selections ds "
            "JOIN game_files gf ON gf.game_id=ds.game_id WHERE ds.device_id=?) accounted "
            "ON accounted.relpath=dif.relpath "
            "WHERE dif.device_id=? AND accounted.relpath IS NULL",
            (device_id, device_id, device_id),
        ).fetchone()["bytes"]
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
    current_rom_bytes = max(int(managed["bytes"] or 0), int(inventory_bytes or 0))
    unmanaged_rom_bytes = int(retained_unmanaged_bytes or 0)
    projected_rom_bytes = int(desired["bytes"] or 0) + unmanaged_rom_bytes
    storage_capacity_bytes = int(device["storage_capacity_bytes"] or 0)
    return {
        "device": dict(device),
        "games": desired["games"],
        "files": desired["files"],
        "managed_files": int(managed["files"] or 0),
        "current_rom_bytes": current_rom_bytes,
        "desired_rom_bytes": int(desired["bytes"] or 0),
        "projected_rom_bytes": projected_rom_bytes,
        "unmanaged_rom_bytes": unmanaged_rom_bytes,
        "storage_capacity_bytes": storage_capacity_bytes,
        "over_capacity": bool(
            storage_capacity_bytes and projected_rom_bytes > storage_capacity_bytes
        ),
        "additions": additions,
        "removals": removals,
        "conversions": 0,
        "hardlinked": None,
        "copied": None,
        "missing": None,
        "unknown": None,
        "storage_inspected": False,
    }


@app.get("/api/devices/{device_id}/summary")
def device_summary(device_id: int, request: Request):
    require_device_access(device_id, request_principal(request))
    return device_summary_payload(device_id)


@app.get("/api/devices/{device_id}/preview")
def device_preview(device_id: int, request: Request):
    require_device_access(device_id, request_principal(request))
    summary = device_summary_payload(device_id)
    with db.connect() as connection:
        addition_games = connection.execute(
            "SELECT g.id,g.display_name,g.platform,COUNT(*) AS files,"
            "COALESCE(SUM(gf.size),0) AS bytes FROM device_selections ds "
            "JOIN games g ON g.id=ds.game_id JOIN game_files gf ON gf.game_id=ds.game_id "
            "WHERE ds.device_id=? AND NOT EXISTS(SELECT 1 FROM deployments dp WHERE dp.device_id=ds.device_id "
            "AND dp.game_id=ds.game_id AND dp.relpath=gf.device_relpath) "
            "GROUP BY g.id,g.display_name,g.platform ORDER BY g.display_name COLLATE NOCASE",
            (device_id,),
        ).fetchall()
        removal_games = connection.execute(
            "SELECT g.id,g.display_name,g.platform,COUNT(*) AS files,"
            "COALESCE(SUM(gf.size),0) AS bytes FROM deployments dp "
            "JOIN games g ON g.id=dp.game_id LEFT JOIN game_files gf ON gf.game_id=dp.game_id "
            "AND gf.device_relpath=dp.relpath WHERE dp.device_id=? "
            "AND NOT EXISTS(SELECT 1 FROM device_selections ds WHERE ds.device_id=dp.device_id "
            "AND ds.game_id=dp.game_id) GROUP BY g.id,g.display_name,g.platform "
            "ORDER BY g.display_name COLLATE NOCASE",
            (device_id,),
        ).fetchall()
    inspection = library.device_storage_inspection(device_id)
    storage = inspection["summary"]
    return {
        **summary,
        "conversions": storage["conversions"],
        "hardlinked": storage["hardlinked"],
        "copied": storage["copied"],
        "missing": storage["missing"],
        "unknown": storage["unknown"],
        "storage_inspected": True,
        "changes": {
            "additions": [dict(row) for row in addition_games],
            "conversions": inspection["conversions"],
            "removals": [dict(row) for row in removal_games],
        },
    }


@app.post("/api/devices/{device_id}/apply", status_code=202)
def apply_device(device_id: int, request: Request):
    principal = request_principal(request)
    require_device_access(device_id, principal)
    return queue_device_apply_job(device_id, principal.id)


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
def upload_sessions(request: Request):
    principal = request_principal(request)
    return transfers.list_sessions(principal.id, principal.has_role("admin"))


@app.post("/api/uploads", status_code=201)
async def create_upload(request: Request):
    principal = request_principal(request)
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
        principal.id,
    )


@app.put("/api/uploads/{session_id}/files/{file_index}")
async def upload_chunk(session_id: str, file_index: int, request: Request):
    principal = request_principal(request)
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
        session_id,
        file_index,
        offset,
        request.stream(),
        principal.id,
        principal.has_role("admin"),
    )


@app.post("/api/uploads/{session_id}/finalize")
def finalize_upload(session_id: str, request: Request):
    principal = request_principal(request)
    if principal.has_role("contributor") and not principal.has_role("admin"):
        upload = transfers.submit(session_id, principal.id)
        safe_notify(
            "upload",
            "ROM upload awaiting review",
            f"{upload['platform']}/{upload['bundle_name'] or upload['files'][0]['relative_path']} was submitted by {principal.display_name}.",
            "transfers",
            dedupe_key=f"upload-review:{session_id}",
        )
        return {"submitted": True, "session": upload}
    job_id = enqueue_job(
        "upload_finalize",
        f"Finalizing upload {session_id}",
        transfers.finalize,
        session_id,
        coalesce=True,
    )
    return {"job_id": job_id}


@app.post("/api/uploads/{session_id}/approve", status_code=202)
def approve_upload(session_id: str, request: Request):
    principal = request_principal(request)
    with db.connect() as connection:
        upload = connection.execute(
            "SELECT owner_user_id,platform,bundle_name FROM upload_sessions WHERE id=?",
            (session_id,),
        ).fetchone()
    if not upload:
        raise HTTPException(status_code=404, detail="Upload session was not found")
    job_id = enqueue_job(
        "upload_finalize",
        f"Approving upload {session_id}",
        transfers.finalize,
        session_id,
        coalesce=True,
        requested_by=principal.id,
    )
    with db.write() as connection:
        connection.execute(
            "UPDATE upload_sessions SET reviewed_by=?,reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
            (principal.id, session_id),
        )
    mobile_push.notify_user(
        upload["owner_user_id"],
        "upload_approved",
        "Your ROM upload was approved",
        f"{upload['platform']}/{upload['bundle_name'] or 'Upload'} is being added to the library.",
        "transfers",
        f"upload:{session_id}:approved",
    )
    return {"job_id": job_id}


@app.post("/api/uploads/{session_id}/reject")
def reject_upload(session_id: str, payload: UploadReviewRequest, request: Request):
    principal = request_principal(request)
    result = transfers.reject(session_id, principal.id, payload.note)
    mobile_push.notify_user(
        result.get("owner_user_id"),
        "upload_rejected",
        "Your ROM upload was not approved",
        payload.note.strip() or "An administrator declined this upload.",
        "transfers",
        f"upload:{session_id}:rejected",
    )
    return result


@app.delete("/api/uploads/{session_id}")
def cancel_upload(session_id: str, request: Request):
    principal = request_principal(request)
    return transfers.cancel(session_id, principal.id, principal.has_role("admin"))


@app.post("/api/games/{game_id}/download-ticket")
def game_download_ticket(game_id: int, request: Request):
    principal = request_principal(request)
    ticket = transfers.create_download_ticket(game_id, principal.id)
    db.activity("download", f"{principal.display_name} requested game {game_id}")
    return ticket


@app.post("/api/devices/{device_id}/export-ticket", status_code=202)
def device_export_ticket(device_id: int, request: Request):
    principal = request_principal(request)
    device = require_device_access(device_id, principal)
    job_id = enqueue_job(
        "device_export",
        f"Preparing {device['name']} ROM package",
        transfers.create_device_export_ticket,
        device_id,
        principal.id,
        requested_by=principal.id,
    )
    return {"job_id": job_id}


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


@app.get("/api/device-downloads/{token}")
def download_device_export(token: str):
    download = transfers.resolve_device_export(token)
    disposition = f"attachment; filename*=UTF-8''{quote(download['filename'])}"
    return StreamingResponse(
        transfers.stream_device_zip(download),
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
    conflicts = saves.conflicts(limit=1, device_names=_syncthing_device_names())
    return {
        "settings": saves.settings_payload(),
        "inventory": saves.source_summary(),
        "latest_snapshot": snapshots["items"][0] if snapshots["items"] else None,
        "snapshot_count": snapshots["total"],
        "matching": saves.match_summary(),
        "conflicts": {"total": conflicts["total"], "identical": conflicts["identical"]},
    }


def _syncthing_device_names(refresh_if_empty: bool = False) -> dict[str, str]:
    status = syncthing.peek()
    if refresh_if_empty and syncthing.configured and status.get("checking"):
        status = syncthing.status()
    return {
        str(device.get("device_id") or ""): str(device.get("name") or "")
        for device in status.get("devices", [])
        if isinstance(device, dict) and device.get("device_id")
    }


@app.get("/api/saves/current")
def current_saves(
    search: str = "",
    limit: int = Query(250, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    sort: str = Query(
        "modified_desc",
        pattern="^(path|emulator|type|size|modified)_(asc|desc)$",
    ),
):
    return saves.current_files(search, limit, offset, sort)


@app.get("/api/saves/conflicts")
def save_conflicts(
    search: str = "",
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    return saves.conflicts(search, limit, offset, _syncthing_device_names(refresh_if_empty=True))


@app.post("/api/saves/conflicts/resolve", status_code=202)
def resolve_save_conflict(payload: SaveConflictResolveRequest):
    job_id = enqueue_job(
        "save_conflict",
        f"Resolving save conflict for {Path(payload.conflict_relpath).name}",
        saves.resolve_conflict,
        payload.conflict_relpath,
        payload.decision,
        payload.expected_canonical_sha256,
        payload.expected_conflict_sha256,
        payload.device_id,
        payload.device_name,
    )
    return {"job_id": job_id}


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
            detail="Confirm that every emulator is closed and Syncthing has finished before restoring",
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
def job(job_id: int, request: Request):
    principal = request_principal(request)
    require_job_access(job_id, principal)
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
    request: Request,
    limit: int = Query(250, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    require_job_access(job_id, request_principal(request))
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
def cancel_job(job_id: int, request: Request):
    require_job_access(job_id, request_principal(request))
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
