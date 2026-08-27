from __future__ import annotations

import json
import secrets
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .config import Settings
from .db import Database
from .library import LibraryError, LibraryService


MINIMUM_TOKEN_LENGTH = 16

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


def job_result_detail(kind: str, result: object, fallback: str) -> str:
    if not isinstance(result, dict):
        return fallback
    if kind == "scan":
        summary = f"Indexed {result.get('games', 0)} games across {result.get('platforms', 0)} platforms"
        if result.get("skipped_count"):
            summary += f", skipped {result['skipped_count']} unreadable files"
        if result.get("removed_devices"):
            summary += f", removed device {', '.join(result['removed_devices'])}"
        return summary
    if kind == "rename":
        return f"Renamed {result.get('old_name', 'game')} to {result.get('new_name', 'game')}"
    if kind == "device_apply":
        return (
            f"Copied {result.get('copied', 0)}, removed {result.get('removed', 0)}, "
            f"left {result.get('unchanged', 0)} unchanged"
        )
    if kind == "restore":
        return f"Restored {result.get('restored', 'trash item')}"
    if kind == "purge":
        return f"Permanently deleted {result.get('purged', 'trash item')}"
    return fallback


def run_job(job_id: int, kind: str, detail: str, operation, *args) -> None:
    try:
        with db.write() as connection:
            connection.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
        last_progress_update = 0.0

        def report_progress(progress: int, progress_detail: str) -> None:
            nonlocal last_progress_update
            now = time.monotonic()
            progress = max(0, min(int(progress), 99))
            # Hashing reports every MiB so a huge image can show movement. Keep those
            # callbacks cheap by committing UI state at most twice per second.
            if progress not in {0, 99} and now - last_progress_update < 0.5:
                return
            with db.write() as connection:
                connection.execute(
                    "UPDATE jobs SET progress=?,detail=? WHERE id=?",
                    (progress, progress_detail, job_id),
                )
            last_progress_update = now

        if kind == "scan":
            result = operation(*args, progress_callback=report_progress)
        else:
            result = operation(*args)
        with db.write() as connection:
            connection.execute(
                "UPDATE jobs SET status='complete',progress=100,detail=?,result_json=?,completed_at=CURRENT_TIMESTAMP WHERE id=?",
                (job_result_detail(kind, result, detail), json.dumps(result), job_id),
            )
    except Exception as exc:
        with db.write() as connection:
            connection.execute(
                "UPDATE jobs SET status='failed',detail=?,completed_at=CURRENT_TIMESTAMP WHERE id=?",
                (str(exc), job_id),
            )


def enqueue_job(kind: str, detail: str, operation, *args) -> int:
    with db.write() as connection:
        connection.execute(
            "INSERT INTO jobs(kind,status,detail) VALUES(?,'queued',?)",
            (kind, detail),
        )
        job_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    db.prune_history()
    threading.Thread(target=run_job, args=(job_id, kind, detail, operation, *args), daemon=True).start()
    return job_id


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.initialize()
    with db.write() as connection:
        connection.execute(
            "UPDATE jobs SET status='failed',detail='Interrupted by application restart',completed_at=CURRENT_TIMESTAMP "
            "WHERE status IN ('queued','running')"
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
    if settings.scan_on_start:
        enqueue_job("scan", "Indexing library", library.scan)
    yield


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


class RenameRequest(BaseModel):
    name: str = Field(min_length=1, max_length=255)


class SelectionRequest(BaseModel):
    game_id: int
    selected: bool


class BulkSelectionRequest(BaseModel):
    game_ids: list[int] = Field(max_length=1000)
    selected: bool


@app.middleware("http")
async def protect_private_api(request: Request, call_next):
    if request.url.path.startswith("/api/") and request.url.path != "/api/health":
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
        "default-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'"
    )
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(LibraryError)
async def library_error_handler(_: Request, exc: LibraryError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.get("/")
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
    return {
        **dict(counts),
        "devices": devices,
        "trash": trash,
        "duplicates": duplicates,
        "job": dict(current_job) if current_job else None,
        "roots": {
            "library": str(settings.library_root),
            "devices": str(settings.devices_root),
            "trash": str(settings.trash_root),
        },
    }


@app.post("/api/scan", status_code=202)
def start_scan(confirm_prune: bool = False):
    with db.write() as connection:
        active = connection.execute(
            "SELECT id FROM jobs WHERE kind='scan' AND status IN ('queued','running') ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if active:
            return {"job_id": active["id"], "already_running": True}
    job_id = enqueue_job("scan", "Indexing library", library.scan, confirm_prune)
    return {"job_id": job_id, "already_running": False}


@app.get("/api/platforms")
def platforms():
    with db.connect() as connection:
        rows = connection.execute(
            "SELECT platform,COUNT(*) AS count FROM games GROUP BY platform ORDER BY platform COLLATE NOCASE"
        ).fetchall()
    return [dict(row) for row in rows]


@app.get("/api/games")
def games(
    search: str = "",
    platform: str = "",
    duplicate: str = Query("all", pattern="^(all|exact|possible|unique)$"),
    device_id: int | None = None,
    limit: int = Query(200, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
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
    if device_id is not None:
        selected_expr = "EXISTS(SELECT 1 FROM device_selections ds WHERE ds.device_id=? AND ds.game_id=g.id)"
        params_for_select: list[object] = [device_id]
    else:
        params_for_select = []
    where_sql = " AND ".join(where)
    with db.connect() as connection:
        total = connection.execute(f"SELECT COUNT(*) AS count FROM games g WHERE {where_sql}", params).fetchone()["count"]
        rows = connection.execute(
            f"SELECT g.*,({status_expr}) AS duplicate_status,"
            f"(SELECT COUNT(*) FROM game_files gf WHERE gf.game_id=g.id) AS file_count,"
            f"(SELECT COUNT(*) FROM device_selections ds WHERE ds.game_id=g.id) AS device_count,"
            f"({selected_expr}) AS selected FROM games g WHERE {where_sql} "
            "ORDER BY g.display_name COLLATE NOCASE,g.platform LIMIT ? OFFSET ?",
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
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.get("/api/games/{game_id}")
def game_detail(game_id: int):
    game, files = library.game_bundle(game_id)
    with db.connect() as connection:
        devices = [dict(row) for row in connection.execute(
            "SELECT d.id,d.name,EXISTS(SELECT 1 FROM device_selections ds WHERE ds.device_id=d.id AND ds.game_id=?) AS selected "
            "FROM devices d ORDER BY d.name", (game_id,)
        )]
    return {"game": game, "files": files, "devices": devices}


@app.patch("/api/games/{game_id}/rename", status_code=202)
def rename_game(game_id: int, payload: RenameRequest):
    job_id = enqueue_job("rename", f"Renaming game {game_id}", library.rename_bundle, game_id, payload.name)
    return {"job_id": job_id}


@app.delete("/api/games/{game_id}", status_code=202)
def delete_game(game_id: int):
    job_id = enqueue_job("delete", f"Moving game {game_id} to trash", library.delete_bundle, game_id)
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


@app.get("/api/devices/{device_id}/preview")
def device_preview(device_id: int):
    with db.connect() as connection:
        device = connection.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        if not device:
            raise HTTPException(status_code=404, detail="Device was not found")
        desired = connection.execute(
            "SELECT COUNT(DISTINCT game_id) AS games,COUNT(*) AS files FROM "
            "(SELECT ds.game_id,gf.relpath FROM device_selections ds JOIN game_files gf ON gf.game_id=ds.game_id WHERE ds.device_id=?)",
            (device_id,),
        ).fetchone()
        additions = connection.execute(
            "SELECT COUNT(*) AS count FROM device_selections ds JOIN game_files gf ON gf.game_id=ds.game_id "
            "WHERE ds.device_id=? AND NOT EXISTS(SELECT 1 FROM deployments dp WHERE dp.device_id=ds.device_id "
            "AND dp.game_id=ds.game_id AND dp.relpath=gf.relpath)", (device_id,)
        ).fetchone()["count"]
        removals = connection.execute(
            "SELECT COUNT(*) AS count FROM deployments dp WHERE dp.device_id=? "
            "AND NOT EXISTS(SELECT 1 FROM device_selections ds WHERE ds.device_id=dp.device_id AND ds.game_id=dp.game_id)",
            (device_id,),
        ).fetchone()["count"]
    return {"device": dict(device), "games": desired["games"], "files": desired["files"], "additions": additions, "removals": removals}


@app.post("/api/devices/{device_id}/apply", status_code=202)
def apply_device(device_id: int):
    job_id = enqueue_job("device_apply", f"Applying device {device_id}", library.apply_device, device_id)
    return {"job_id": job_id}


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


@app.get("/api/jobs/{job_id}")
def job(job_id: int):
    with db.connect() as connection:
        row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Job was not found")
    result = dict(row)
    result_json = result.pop("result_json", None)
    result["result"] = json.loads(result_json) if result_json else None
    return result


@app.get("/api/jobs")
def jobs():
    with db.connect() as connection:
        rows = connection.execute("SELECT * FROM jobs ORDER BY id DESC LIMIT 100").fetchall()
    return [dict(row) for row in rows]


@app.get("/api/activity")
def activity():
    with db.connect() as connection:
        rows = connection.execute("SELECT * FROM activity ORDER BY id DESC LIMIT 100").fetchall()
    return [dict(row) for row in rows]
