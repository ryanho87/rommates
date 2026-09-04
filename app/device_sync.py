from __future__ import annotations

import threading
from typing import Any

from .db import Database
from .syncthing import SyncthingService


ACTIVE_SYNC_STATES = ("pending", "syncing", "offline")


class DeviceSyncMonitor:
    """Persist and reconcile Syncthing delivery after a device apply finishes."""

    def __init__(
        self,
        db: Database,
        syncthing: SyncthingService,
        notifications: Any,
        *,
        mobile_push: Any | None = None,
        poll_seconds: float = 10.0,
    ) -> None:
        self.db = db
        self.syncthing = syncthing
        self.notifications = notifications
        self.mobile_push = mobile_push
        self.poll_seconds = max(1.0, float(poll_seconds))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="rommates-device-sync",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=min(self.poll_seconds + 1, 3))
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception as exc:
                # A temporary Syncthing or database failure must not kill the
                # persistent watcher. The next interval retries active runs.
                self.db.activity("device_sync", f"Sync status check failed: {exc}")
            self._wake.wait(self.poll_seconds)
            self._wake.clear()

    @staticmethod
    def _payload(row: Any | None) -> dict[str, object] | None:
        if not row:
            return None
        item = dict(row)
        return {
            "id": int(item["id"]),
            "state": str(item["status"]),
            "completion": float(item["completion"] or 0),
            "need_bytes": int(item["need_bytes"] or 0),
            "need_items": int(item["need_items"] or 0),
            "need_deletes": int(item["need_deletes"] or 0),
            "target_sequence": int(item["target_sequence"] or 0),
            "live_sequence": int(item["live_sequence"] or 0),
            "added": int(item["added_count"] or 0),
            "removed": int(item["removed_count"] or 0),
            "detail": str(item["detail"] or ""),
            "started_at": item["started_at"],
            "updated_at": item["updated_at"],
            "completed_at": item["completed_at"],
        }

    def latest(self, device_id: int) -> dict[str, object] | None:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM device_sync_runs WHERE device_id=? "
                "ORDER BY CASE WHEN status IN ('pending','syncing','offline') THEN 0 ELSE 1 END,id DESC LIMIT 1",
                (device_id,),
            ).fetchone()
        return self._payload(row)

    def remember_link(self, device_id: int, live: dict[str, object]) -> bool:
        """Persist a Syncthing link that was inferred from the device folder path."""
        if not live.get("linked"):
            return False
        folder_id = str(live.get("folder_id") or "").strip()
        remote_device_id = str(live.get("device_id") or "").strip()
        if not folder_id or not remote_device_id:
            return False
        with self.db.write() as connection:
            cursor = connection.execute(
                "UPDATE devices SET syncthing_folder_id=?,syncthing_device_id=?,"
                "syncthing_ready_at=COALESCE(syncthing_ready_at,CURRENT_TIMESTAMP) "
                "WHERE id=? AND (COALESCE(syncthing_folder_id,'')<>? "
                "OR COALESCE(syncthing_device_id,'')<>? OR syncthing_ready_at IS NULL)",
                (folder_id, remote_device_id, device_id, folder_id, remote_device_id),
            )
        return bool(cursor.rowcount)

    def track(
        self,
        device_id: int,
        job_id: int,
        requested_by: int | None,
        *,
        added: int,
        removed: int,
        folder_id: str = "",
        remote_device_id: str = "",
    ) -> dict[str, object] | None:
        """Start tracking the remote folder after a successful local rescan request."""
        if added <= 0 and removed <= 0:
            return None
        with self.db.connect() as connection:
            device = connection.execute(
                "SELECT id,name,owner_user_id,delivery_mode,syncthing_device_id,syncthing_folder_id "
                "FROM devices WHERE id=?",
                (device_id,),
            ).fetchone()
        if not device or device["delivery_mode"] != "syncthing":
            return None
        resolved_folder_id = str(folder_id or device["syncthing_folder_id"] or "").strip()
        resolved_device_id = str(
            remote_device_id or device["syncthing_device_id"] or ""
        ).strip()
        if not resolved_folder_id:
            return None
        if not resolved_device_id:
            live = self.syncthing.device_sync_status(
                str(device["name"]), folder_id=resolved_folder_id
            )
            if live.get("linked"):
                resolved_folder_id = str(live.get("folder_id") or resolved_folder_id)
                resolved_device_id = str(live.get("device_id") or "")
                self.remember_link(device_id, live)
        recipient = requested_by or device["owner_user_id"]
        target_sequence = self.syncthing.folder_sequence(resolved_folder_id)
        with self.db.write() as connection:
            connection.execute(
                "UPDATE device_sync_runs SET status='superseded',detail='Replaced by a newer device sync',"
                "updated_at=CURRENT_TIMESTAMP,completed_at=CURRENT_TIMESTAMP "
                "WHERE device_id=? AND status IN ('pending','syncing','offline')",
                (device_id,),
            )
            connection.execute(
                "INSERT INTO device_sync_runs("
                "device_id,job_id,requested_by,folder_id,remote_device_id,status,target_sequence,"
                "added_count,removed_count,detail) VALUES(?,?,?,?,?,'pending',?,?,?,?)",
                (
                    device_id,
                    job_id,
                    recipient,
                    resolved_folder_id,
                    resolved_device_id,
                    target_sequence,
                    max(0, int(added)),
                    max(0, int(removed)),
                    "Waiting for Syncthing to report the device folder",
                ),
            )
            run_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            row = connection.execute(
                "SELECT * FROM device_sync_runs WHERE id=?", (run_id,)
            ).fetchone()
        self._wake.set()
        return self._payload(row)

    def check_once(self) -> int:
        """Refresh active runs once. Exposed separately for deterministic tests."""
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT r.*,d.name AS device_name,d.syncthing_device_id AS current_device_id,"
                "d.syncthing_folder_id AS current_folder_id "
                "FROM device_sync_runs r JOIN devices d ON d.id=r.device_id "
                "WHERE r.status IN ('pending','syncing','offline') ORDER BY r.id"
            ).fetchall()
        completed = 0
        for row in rows:
            if self._stop.is_set():
                break
            try:
                live = self.syncthing.device_sync_status(
                    str(row["device_name"]),
                    remote_device_id=str(row["current_device_id"] or row["remote_device_id"]),
                    folder_id=str(row["current_folder_id"] or row["folder_id"]),
                )
            except Exception as exc:
                with self.db.write() as connection:
                    connection.execute(
                        "UPDATE device_sync_runs SET detail=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (f"Syncthing status temporarily unavailable: {exc}", row["id"]),
                    )
                continue
            live_folder_id = str(live.get("folder_id") or "")
            live_device_id = str(live.get("device_id") or "")
            if live.get("linked") and live_folder_id and live_device_id:
                if (
                    live_folder_id != str(row["current_folder_id"] or row["folder_id"])
                    or live_device_id != str(row["current_device_id"] or row["remote_device_id"])
                ):
                    self.remember_link(int(row["device_id"]), live)
                    with self.db.write() as connection:
                        connection.execute(
                            "UPDATE device_sync_runs SET folder_id=?,remote_device_id=? WHERE id=?",
                            (live_folder_id, live_device_id, row["id"]),
                        )
            completion = float(live.get("completion") or 0)
            need_bytes = int(live.get("need_bytes") or 0)
            need_items = int(live.get("need_items") or 0)
            need_deletes = int(live.get("need_deletes") or 0)
            remote_state = str(live.get("remote_state") or "")
            live_sequence = int(live.get("sequence") or 0)
            target_sequence = int(row["target_sequence"] or 0)
            is_complete = (
                bool(live.get("linked"))
                and completion >= 99.999
                and need_bytes == 0
                and need_items == 0
                and need_deletes == 0
                and remote_state in {"", "valid"}
                and (not target_sequence or live_sequence >= target_sequence)
            )
            if is_complete:
                if self._complete(row, live):
                    completed += 1
                continue
            if live.get("linked") and live.get("connected"):
                state = "syncing"
                detail = (
                    f"Syncing with {need_items + need_deletes:,} items and {need_bytes:,} bytes remaining"
                    if need_items or need_deletes or need_bytes
                    else "Confirming the device has the latest folder index"
                )
            elif live.get("linked"):
                state = "offline"
                detail = "Waiting for the device to reconnect"
            else:
                state = "pending"
                detail = str(live.get("status") or "Waiting for Syncthing status")
            with self.db.write() as connection:
                connection.execute(
                    "UPDATE device_sync_runs SET status=?,completion=?,need_bytes=?,need_items=?,"
                    "need_deletes=?,live_sequence=?,detail=?,updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=? AND status IN ('pending','syncing','offline')",
                    (state, completion, need_bytes, need_items, need_deletes, live_sequence, detail, row["id"]),
                )
        return completed

    def _complete(self, row: Any, live: dict[str, object]) -> bool:
        detail = "Syncthing confirms this device has the current ROM folder"
        with self.db.write() as connection:
            cursor = connection.execute(
                "UPDATE device_sync_runs SET status='complete',completion=100,need_bytes=0,"
                "need_items=0,need_deletes=0,live_sequence=?,detail=?,updated_at=CURRENT_TIMESTAMP,"
                "completed_at=CURRENT_TIMESTAMP WHERE id=? AND status IN ('pending','syncing','offline')",
                (int(live.get("sequence") or 0), detail, row["id"]),
            )
            if not cursor.rowcount:
                return False
            if row["requested_by"]:
                changes = []
                if row["added_count"]:
                    changes.append(f"{int(row['added_count']):,} added")
                if row["removed_count"]:
                    changes.append(f"{int(row['removed_count']):,} removed")
                change_label = ", ".join(changes) or "Device changes"
                connection.execute(
                    "INSERT INTO user_notifications(user_id,kind,title,detail,path,dedupe_key) "
                    "VALUES(?,?,?,?,?,?) ON CONFLICT(user_id,dedupe_key) DO NOTHING",
                    (
                        row["requested_by"],
                        "device_sync",
                        f"{row['device_name']} finished syncing",
                        f"{change_label}. Syncthing confirms the device ROM folder is up to date.",
                        f"devices?device={row['device_id']}",
                        f"device-sync:{row['id']}:complete",
                    ),
                )
        try:
            self.notifications.notify(
                "device",
                f"{row['device_name']} finished syncing",
                detail,
                f"devices?device={row['device_id']}",
                dedupe_key=f"device-sync:{row['id']}:complete",
            )
        except Exception:
            # Sync completion and the in-app notification remain authoritative.
            pass
        if self.mobile_push and row["requested_by"]:
            try:
                self.mobile_push.enqueue_existing(
                    int(row["requested_by"]),
                    f"device-sync:{row['id']}:complete",
                )
            except Exception:
                # The durable in-app notification remains available even if
                # queueing APNs is temporarily unavailable.
                pass
        self.db.activity("device_sync", f"{row['device_name']} finished syncing")
        return True
