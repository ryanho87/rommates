from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode

from .config import Settings
from .db import Database
from .syncthing import SyncthingService


STALE_AFTER_DAYS = 7


def _parse_when(value: object) -> datetime | None:
    """Best-effort parse of Syncthing's lastSeen; unknown or epoch-zero -> None."""
    text = str(value or "").strip()
    if not text or text.startswith("0001-") or text.startswith("1969-") or text.startswith("1970-"):
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class SaveSyncMonitor:
    """Watch whether each handheld's save folder is caught up, and say so.

    A stale local save that gets written on a handheld wins Syncthing's
    newest-timestamp tiebreak and overwrites real progress everywhere. The
    only prevention is knowing a device is behind before it writes, so this
    records per-device completion on the save folder and raises two nudges:

    - a device reconnects while still behind: "wait before playing";
    - a device has not synced for STALE_AFTER_DAYS: its saves may be stale.

    Each nudge fires once per episode and clears when the device catches up or
    reconnects, so a long-offline handheld is not nagged every minute.
    """

    def __init__(
        self,
        settings: Settings,
        db: Database,
        syncthing: SyncthingService,
        notifications: Any,
        *,
        poll_seconds: float = 60.0,
        stale_after_days: int = STALE_AFTER_DAYS,
    ) -> None:
        self.settings = settings
        self.db = db
        self.syncthing = syncthing
        self.notifications = notifications
        self.poll_seconds = max(5.0, float(poll_seconds))
        self.stale_after_days = max(1, int(stale_after_days))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._legacy_folder_id: str | None = None

    # -- lifecycle ---------------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="rommates-save-sync", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.check_once()
            except Exception as exc:
                # Syncthing being down is routine; the next tick retries.
                self.db.activity("save_sync", f"Save sync check failed: {exc}")
            self._wake.wait(self.poll_seconds)

    # -- folder resolution ----------------------------------------------------------

    def _folder_for(self, device_id: int, vault_folders: dict[int, str]) -> str:
        if device_id in vault_folders:
            return vault_folders[device_id]
        if self._legacy_folder_id is None:
            self._legacy_folder_id = self.syncthing._save_folder_id(self.settings.saves_root)
        return self._legacy_folder_id

    def _completion(self, folder_id: str, device_id: str) -> dict[str, object]:
        query = urlencode({"folder": folder_id, "device": device_id})
        payload = self.syncthing._get(f"/rest/db/completion?{query}")
        if not isinstance(payload, dict):
            return {}
        return payload

    # -- the check ------------------------------------------------------------------

    def check_once(self, now: datetime | None = None) -> int:
        if not self.syncthing.configured:
            return 0
        now = now or datetime.now(timezone.utc)
        status = self.syncthing.status(refresh=True)
        live = {
            str(item.get("device_id") or ""): item
            for item in status.get("devices", [])
            if isinstance(item, dict) and item.get("device_id")
        }
        with self.db.connect() as connection:
            devices = [dict(row) for row in connection.execute(
                "SELECT id,name,syncthing_device_id FROM devices WHERE syncthing_device_id<>''"
            )]
            vault_folders = {
                row["device_id"]: f"rommates-saves-{row['storage_key']}"
                for row in connection.execute(
                    "SELECT vd.device_id,v.storage_key FROM save_vault_devices vd "
                    "JOIN save_vaults v ON v.id=vd.vault_id"
                )
            }
            previous = {
                row["device_id"]: dict(row)
                for row in connection.execute("SELECT * FROM device_save_sync")
            }

        checked = 0
        for device in devices:
            remote_id = device["syncthing_device_id"]
            entry = live.get(remote_id) or next(
                (item for key, item in live.items() if key.startswith(remote_id) or remote_id.startswith(key)),
                None,
            )
            folder_id = self._folder_for(device["id"], vault_folders)
            if not folder_id:
                continue
            try:
                completion = self._completion(folder_id, remote_id)
            except Exception:
                completion = {}
            connected = bool(entry and entry.get("connected"))
            last_seen = _parse_when(entry.get("last_seen")) if entry else None
            if connected:
                last_seen = now
            need_items = int(completion.get("needItems") or 0)
            need_bytes = int(completion.get("needBytes") or 0)
            percent = float(completion.get("completion") or 0.0)
            self._record(
                device, folder_id, connected, last_seen, percent, need_items, need_bytes,
                previous.get(device["id"]), now,
            )
            checked += 1
        return checked

    def _record(
        self,
        device: dict[str, Any],
        folder_id: str,
        connected: bool,
        last_seen: datetime | None,
        percent: float,
        need_items: int,
        need_bytes: int,
        previous: dict[str, Any] | None,
        now: datetime,
    ) -> None:
        behind = need_items > 0
        was_connected = bool(previous and previous.get("connected"))
        behind_since = (previous or {}).get("behind_since") if behind else None
        if behind and not behind_since:
            behind_since = now.isoformat()
        reconnect_notified_at = (previous or {}).get("reconnect_notified_at")
        stale_notified_at = (previous or {}).get("stale_notified_at")

        if connected and not was_connected:
            # Opt-in "it's online" alert; carries the save state so it is useful on its own.
            state = (
                f"{need_items} save {'file' if need_items == 1 else 'files'} still to download; "
                "wait before playing" if behind else "saves up to date"
            )
            self._notify(
                "device_online",
                f"{device['name']} came online",
                f"Connected to Syncthing — {state}.",
                dedupe_key=f"device-online:{device['id']}:{now.strftime('%Y%m%d%H%M')}",
            )
        if connected and not was_connected and behind and not reconnect_notified_at:
            self._notify(
                "save_sync_behind",
                f"{device['name']} is back online but still behind on saves",
                f"{need_items} save {'file' if need_items == 1 else 'files'} still to download. "
                "Wait for Syncthing to finish before launching a game on it, or an old save "
                "may overwrite newer progress.",
                dedupe_key=f"save-sync-behind:{device['id']}",
            )
            reconnect_notified_at = now.isoformat()
        if not behind:
            reconnect_notified_at = None

        days_dark = None if last_seen is None else (now - last_seen).total_seconds() / 86400
        if connected:
            stale_notified_at = None
        elif days_dark is not None and days_dark >= self.stale_after_days and not stale_notified_at:
            self._notify(
                "save_sync_stale",
                f"{device['name']} has not synced saves for {int(days_dark)} days",
                f"Last seen {last_seen.strftime('%Y-%m-%d') if last_seen else 'unknown'}. Its saves may be "
                "stale: let it finish syncing before playing on it.",
                dedupe_key=f"save-sync-stale:{device['id']}",
            )
            stale_notified_at = now.isoformat()

        with self.db.write() as connection:
            connection.execute(
                "INSERT INTO device_save_sync(device_id,folder_id,connected,last_seen,completion,"
                "need_items,need_bytes,checked_at,behind_since,reconnect_notified_at,stale_notified_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET "
                "folder_id=excluded.folder_id,connected=excluded.connected,last_seen=excluded.last_seen,"
                "completion=excluded.completion,need_items=excluded.need_items,need_bytes=excluded.need_bytes,"
                "checked_at=excluded.checked_at,behind_since=excluded.behind_since,"
                "reconnect_notified_at=excluded.reconnect_notified_at,stale_notified_at=excluded.stale_notified_at",
                (
                    device["id"], folder_id, int(connected),
                    last_seen.isoformat() if last_seen else None,
                    percent, need_items, need_bytes, now.isoformat(),
                    behind_since, reconnect_notified_at, stale_notified_at,
                ),
            )

    def _notify(self, event: str, title: str, detail: str, *, dedupe_key: str) -> None:
        try:
            if not self.notifications.event_enabled(event):
                return
            self.notifications.notify(event, title, detail, "devices", dedupe_key=dedupe_key)
        except Exception:
            pass

    # -- read side --------------------------------------------------------------------

    def latest(self, device_id: int) -> dict[str, object] | None:
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM device_save_sync WHERE device_id=?", (device_id,)
            ).fetchone()
        return dict(row) if row else None
