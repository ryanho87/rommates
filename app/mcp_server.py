from __future__ import annotations

import hashlib
import json
import secrets
from typing import Callable, Literal

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from .db import Database
from .library import LibraryError, LibraryService


READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
SAFE_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)
QUEUED_WRITE = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=True,
    idempotentHint=False,
    openWorldHint=False,
)


class RommatesMCPService:
    """A bounded MCP facade over ROMmates' indexed state and job queue."""

    def __init__(
        self,
        db: Database,
        library: LibraryService,
        enqueue_scan: Callable[[], dict[str, object]],
        enqueue_device_apply: Callable[[int, str], dict[str, object]],
        cancel_job: Callable[[int], dict[str, object]],
    ):
        self.db = db
        self.library = library
        self._enqueue_scan = enqueue_scan
        self._enqueue_device_apply = enqueue_device_apply
        self._cancel_job = cancel_job

    @staticmethod
    def _job(row) -> dict[str, object]:
        payload = dict(row)
        for source, target in (("result_json", "result"), ("progress_json", "telemetry")):
            raw = payload.pop(source, None)
            try:
                payload[target] = json.loads(raw) if raw else None
            except (TypeError, json.JSONDecodeError):
                payload[target] = None
        return payload

    def library_status(self) -> dict[str, object]:
        """Summarize the indexed collection, platforms, active jobs, and latest scan."""
        with self.db.connect() as connection:
            collection = dict(
                connection.execute(
                    "SELECT COUNT(*) AS games,COUNT(DISTINCT platform) AS platforms,"
                    "COALESCE(SUM(size),0) AS bytes,(SELECT COUNT(*) FROM game_files) AS files "
                    "FROM games"
                ).fetchone()
            )
            platform_rows = connection.execute(
                "SELECT platform,COUNT(*) AS games,COALESCE(SUM(size),0) AS bytes "
                "FROM games GROUP BY platform ORDER BY games DESC,platform COLLATE NOCASE"
            ).fetchall()
            active_rows = connection.execute(
                "SELECT * FROM jobs WHERE status IN ('queued','running','paused','cancelling') "
                "ORDER BY id"
            ).fetchall()
            last_scan = connection.execute(
                "SELECT * FROM jobs WHERE kind='scan' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return {
            "collection": collection,
            "platforms": [dict(row) for row in platform_rows],
            "active_jobs": [self._job(row) for row in active_rows],
            "last_scan": self._job(last_scan) if last_scan else None,
            "hash_max_bytes": self.library.settings.hash_max_bytes,
        }

    def search_games(
        self,
        query: str = "",
        platform: str = "",
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, object]:
        """Search indexed games by title or relative path, optionally within one platform."""
        query = query.strip()
        platform = platform.strip()
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        where = ["1=1"]
        params: list[object] = []
        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("(g.display_name LIKE ? ESCAPE '\\' OR g.primary_relpath LIKE ? ESCAPE '\\')")
            params.extend((f"%{escaped}%", f"%{escaped}%"))
        if platform:
            where.append("g.platform=?")
            params.append(platform)
        where_sql = " AND ".join(where)
        with self.db.connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) AS count FROM games g WHERE {where_sql}", params
            ).fetchone()["count"]
            rows = connection.execute(
                "SELECT g.id,g.display_name,g.platform,g.primary_relpath,g.size,g.extension,"
                "g.bundle_hash,(SELECT COUNT(*) FROM game_files gf WHERE gf.game_id=g.id) AS file_count,"
                "(SELECT GROUP_CONCAT(d.name,char(31)) FROM device_selections ds "
                "JOIN devices d ON d.id=ds.device_id WHERE ds.game_id=g.id) AS devices "
                f"FROM games g WHERE {where_sql} "
                "ORDER BY g.display_name COLLATE NOCASE,g.platform,g.id LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["devices"] = item["devices"].split("\x1f") if item["devices"] else []
            items.append(item)
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    def get_game_bundle(self, game_id: int) -> dict[str, object]:
        """Inspect one indexed game, every file in its bundle, and its device assignments."""
        game, files = self.library.game_bundle(game_id)
        with self.db.connect() as connection:
            devices = connection.execute(
                "SELECT d.id,d.name,"
                "EXISTS(SELECT 1 FROM device_selections ds WHERE ds.device_id=d.id AND ds.game_id=?) AS selected,"
                "EXISTS(SELECT 1 FROM deployments dp WHERE dp.device_id=d.id AND dp.game_id=?) AS managed "
                "FROM devices d ORDER BY d.name COLLATE NOCASE",
                (game_id, game_id),
            ).fetchall()
        return {
            "game": dict(game),
            "files": [dict(item) for item in files],
            "devices": [dict(item) for item in devices],
        }

    def find_duplicates(
        self,
        kind: Literal["exact", "possible"] = "exact",
        platform: str = "",
        limit: int = 20,
        offset: int = 0,
    ) -> dict[str, object]:
        """List exact-content or similar-name duplicate groups without modifying files."""
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        params: list[object] = []
        platform_where = ""
        if platform.strip():
            platform_where = "WHERE g.platform=?"
            params.append(platform.strip())
        if kind == "exact":
            groups_sql = (
                "SELECT g.bundle_hash AS group_key,MIN(g.display_name) AS label,COUNT(*) AS copies,"
                "SUM(g.size) AS bytes FROM games g "
                f"{platform_where} GROUP BY g.bundle_hash HAVING COUNT(*)>1"
            )
            membership = "g.bundle_hash=?"
        else:
            groups_sql = (
                "SELECT g.platform || char(31) || g.normalized_name AS group_key,"
                "MIN(g.display_name) AS label,COUNT(*) AS copies,SUM(g.size) AS bytes "
                f"FROM games g {platform_where} "
                "GROUP BY g.platform,g.normalized_name HAVING g.normalized_name<>'' "
                "AND COUNT(DISTINCT g.bundle_hash)>1"
            )
            membership = "g.platform || char(31) || g.normalized_name=?"
        with self.db.connect() as connection:
            total = connection.execute(
                f"SELECT COUNT(*) AS count FROM ({groups_sql})", params
            ).fetchone()["count"]
            groups = [
                dict(row)
                for row in connection.execute(
                    f"{groups_sql} ORDER BY label COLLATE NOCASE,group_key LIMIT ? OFFSET ?",
                    [*params, limit, offset],
                )
            ]
            for group in groups:
                members = connection.execute(
                    "SELECT g.id,g.display_name,g.platform,g.primary_relpath,g.size,"
                    "(SELECT GROUP_CONCAT(d.name,char(31)) FROM device_selections ds "
                    "JOIN devices d ON d.id=ds.device_id WHERE ds.game_id=g.id) AS devices "
                    f"FROM games g WHERE {membership} ORDER BY g.primary_relpath COLLATE NOCASE",
                    (group["group_key"],),
                ).fetchall()
                group["items"] = []
                for member in members:
                    item = dict(member)
                    item["devices"] = item["devices"].split("\x1f") if item["devices"] else []
                    group["items"].append(item)
        return {"items": groups, "total": total, "limit": limit, "offset": offset}

    def list_devices(self) -> list[dict[str, object]]:
        """List managed devices with selected and currently managed game counts."""
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT d.id,d.name,d.deployment_mode,"
                "COUNT(DISTINCT ds.game_id) AS selected_games,"
                "COUNT(DISTINCT dp.game_id) AS managed_games "
                "FROM devices d LEFT JOIN device_selections ds ON ds.device_id=d.id "
                "LEFT JOIN deployments dp ON dp.device_id=d.id "
                "GROUP BY d.id ORDER BY d.name COLLATE NOCASE"
            ).fetchall()
        return [dict(row) for row in rows]

    def _device_preview(self, device_id: int) -> dict[str, object]:
        inventory = sorted(self.library.device_inventory(device_id, refresh=True))
        with self.db.connect() as connection:
            device = connection.execute(
                "SELECT id,name,path,deployment_mode FROM devices WHERE id=?", (device_id,)
            ).fetchone()
            if not device:
                raise LibraryError("Device was not found")
            selected = connection.execute(
                "SELECT g.id,g.display_name,g.platform,g.primary_relpath,g.size,g.mtime_ns "
                "FROM device_selections ds JOIN games g ON g.id=ds.game_id "
                "WHERE ds.device_id=? ORDER BY g.id",
                (device_id,),
            ).fetchall()
            desired_files = connection.execute(
                "SELECT ds.game_id,gf.device_relpath,gf.size,gf.sha256 FROM device_selections ds "
                "JOIN game_files gf ON gf.game_id=ds.game_id WHERE ds.device_id=? "
                "ORDER BY ds.game_id,gf.device_relpath",
                (device_id,),
            ).fetchall()
            deployments = connection.execute(
                "SELECT dp.game_id,dp.relpath FROM deployments dp WHERE dp.device_id=? "
                "ORDER BY dp.game_id,dp.relpath",
                (device_id,),
            ).fetchall()
            additions = connection.execute(
                "SELECT g.id,g.display_name,g.platform,COUNT(*) AS files FROM device_selections ds "
                "JOIN games g ON g.id=ds.game_id JOIN game_files gf ON gf.game_id=g.id "
                "WHERE ds.device_id=? AND NOT EXISTS(SELECT 1 FROM deployments dp "
                "WHERE dp.device_id=ds.device_id AND dp.game_id=ds.game_id "
                "AND dp.relpath=gf.device_relpath) GROUP BY g.id ORDER BY g.display_name COLLATE NOCASE",
                (device_id,),
            ).fetchall()
            removals = connection.execute(
                "SELECT g.id,g.display_name,g.platform,COUNT(*) AS files FROM deployments dp "
                "LEFT JOIN games g ON g.id=dp.game_id WHERE dp.device_id=? "
                "AND NOT EXISTS(SELECT 1 FROM device_selections ds "
                "WHERE ds.device_id=dp.device_id AND ds.game_id=dp.game_id) "
                "GROUP BY dp.game_id ORDER BY g.display_name COLLATE NOCASE",
                (device_id,),
            ).fetchall()
        storage = self.library.device_storage_summary(device_id)
        state = {
            "device": dict(device),
            "selected": [dict(row) for row in selected],
            "desired_files": [dict(row) for row in desired_files],
            "deployments": [dict(row) for row in deployments],
            "inventory": inventory,
            "storage": storage,
        }
        token = hashlib.sha256(
            json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return {
            "device": dict(device),
            "selected_games": len(selected),
            "desired_files": len(desired_files),
            "current_files": len(inventory),
            "storage": storage,
            "additions": [dict(row) for row in additions],
            "removals": [dict(row) for row in removals],
            "preview_token": token,
        }

    def inspect_device(self, device_id: int) -> dict[str, object]:
        """Inspect a device's desired state, current storage relationship, and pending changes."""
        return self._device_preview(device_id)

    def preview_device_changes(self, device_id: int) -> dict[str, object]:
        """Create a fresh device plan and token required by apply_device_changes."""
        return self._device_preview(device_id)

    def list_jobs(self, limit: int = 20) -> list[dict[str, object]]:
        """List recent background jobs, including live scan telemetry when available."""
        limit = max(1, min(int(limit), 100))
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM jobs ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [self._job(row) for row in rows]

    def get_job_report(self, job_id: int) -> dict[str, object]:
        """Return one job's result, telemetry, and up to 100 captured issues."""
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise LibraryError("Job was not found")
            issue_total = connection.execute(
                "SELECT COUNT(*) AS count FROM job_issues WHERE job_id=?", (job_id,)
            ).fetchone()["count"]
            issues = connection.execute(
                "SELECT detail FROM job_issues WHERE job_id=? ORDER BY id LIMIT 100", (job_id,)
            ).fetchall()
        payload = self._job(row)
        payload["issue_count"] = issue_total
        payload["issues"] = [item["detail"] for item in issues]
        payload["issues_truncated"] = issue_total > len(issues)
        return payload

    def start_scan(self) -> dict[str, object]:
        """Queue a safe library scan, or return the scan already in progress."""
        result = self._enqueue_scan()
        self.db.activity("mcp", f"MCP requested library scan job #{result['job_id']}")
        return result

    def set_device_games(
        self,
        device_id: int,
        game_ids: list[int],
        selected: bool,
    ) -> dict[str, object]:
        """Select or unselect up to 500 indexed games for a device without applying files."""
        if not game_ids:
            raise LibraryError("Choose at least one game")
        if len(game_ids) > 500 or len(set(game_ids)) > 500:
            raise LibraryError("Change at most 500 games per call")
        updated = self.library.set_selections(device_id, game_ids, selected)
        self.db.activity(
            "mcp",
            f"MCP {'selected' if selected else 'unselected'} {updated} games for device {device_id}",
        )
        return {"updated": updated, "selected": selected, "preview": self._device_preview(device_id)}

    def apply_device_changes(self, device_id: int, preview_token: str) -> dict[str, object]:
        """Queue a reviewed device plan; rejects stale or mismatched preview tokens."""
        preview = self._device_preview(device_id)
        if not secrets.compare_digest(preview_token, str(preview["preview_token"])):
            raise LibraryError(
                "The device plan changed after it was reviewed. Call preview_device_changes again."
            )
        result = self._enqueue_device_apply(device_id, preview_token)
        self.db.activity("mcp", f"MCP queued device apply job #{result['job_id']} for device {device_id}")
        return {**result, "reviewed_preview": preview}

    def execute_reviewed_device_apply(
        self, device_id: int, preview_token: str, cancel_check=None
    ) -> dict[str, object]:
        """Revalidate a reviewed plan inside the serialized filesystem worker."""
        preview = self._device_preview(device_id)
        if not secrets.compare_digest(preview_token, str(preview["preview_token"])):
            raise LibraryError(
                "The device plan changed while the job was queued. Review and apply it again."
            )
        return self.library.apply_device(device_id, cancel_check=cancel_check)

    def stop_job(self, job_id: int) -> dict[str, object]:
        """Request cooperative cancellation of a queued or cancellable background job."""
        result = self._cancel_job(job_id)
        self.db.activity("mcp", f"MCP requested stop for job #{job_id}")
        return result


def create_mcp_server(service: RommatesMCPService) -> MCPServer:
    server = MCPServer(
        "ROMmates",
        description="Private ROM library and handheld deployment manager",
        instructions=(
            "Use read tools to inspect the catalog and preview changes before writing. "
            "Device application requires the exact preview_token returned by the latest preview. "
            "All filesystem work is queued; inspect the returned job_id for completion."
        ),
        version="0.1.0",
    )

    server.tool(name="library_status", annotations=READ_ONLY)(service.library_status)
    server.tool(name="search_games", annotations=READ_ONLY)(service.search_games)
    server.tool(name="get_game_bundle", annotations=READ_ONLY)(service.get_game_bundle)
    server.tool(name="find_duplicates", annotations=READ_ONLY)(service.find_duplicates)
    server.tool(name="list_devices", annotations=READ_ONLY)(service.list_devices)
    server.tool(name="inspect_device", annotations=READ_ONLY)(service.inspect_device)
    server.tool(name="preview_device_changes", annotations=READ_ONLY)(service.preview_device_changes)
    server.tool(name="list_jobs", annotations=READ_ONLY)(service.list_jobs)
    server.tool(name="get_job_report", annotations=READ_ONLY)(service.get_job_report)
    server.tool(name="start_scan", annotations=QUEUED_WRITE)(service.start_scan)
    server.tool(name="set_device_games", annotations=SAFE_WRITE)(service.set_device_games)
    server.tool(name="apply_device_changes", annotations=QUEUED_WRITE)(service.apply_device_changes)
    server.tool(name="stop_job", annotations=SAFE_WRITE)(service.stop_job)
    return server
