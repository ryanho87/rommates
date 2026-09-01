from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import Settings
from .db import Database
from .library import LibraryError


EVENTS: dict[str, dict[str, object]] = {
    "device_setup_required": {"label": "Device setup requests", "description": "A member creates a device that needs administrator Syncthing setup.", "default": True},
    "upload": {"label": "Uploads", "description": "An upload needs review or is added to the library.", "default": True},
    "save_conflict": {"label": "Save conflicts", "description": "Syncthing preserves a competing save version.", "default": True},
    "job_failed": {"label": "Job failures", "description": "A scan or filesystem operation fails.", "default": True},
    "device": {"label": "Device changes", "description": "A device reconciliation completes.", "default": False},
    "scan": {"label": "Library scans", "description": "A library scan completes.", "default": False},
    "save": {"label": "Save snapshots", "description": "A snapshot, restore, or conflict resolution completes.", "default": False},
    "trash": {"label": "Trash changes", "description": "ROMs are trashed, restored, or permanently removed.", "default": False},
}


class NotificationService:
    """Asynchronous, best-effort Discord webhook delivery."""

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="rommates-notify")
        self._closed = False
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        return self.settings.discord_webhook_url.startswith("https://discord.com/api/webhooks/") or self.settings.discord_webhook_url.startswith("https://discordapp.com/api/webhooks/")

    def initialize(self) -> None:
        # Lifespan may be entered more than once by integration tests or an in-process
        # server harness. Recreate the worker after a prior orderly shutdown.
        with self._lock:
            if self._closed:
                self._executor = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="rommates-notify"
                )
                self._closed = False
        defaults = {key: bool(value["default"]) for key, value in EVENTS.items()}
        with self.db.write() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO notification_settings(id,enabled,events_json) VALUES(1,1,?)",
                (json.dumps(defaults, sort_keys=True),),
            )

    def close(self) -> None:
        with self._lock:
            self._closed = True
        self._executor.shutdown(wait=False, cancel_futures=False)

    def settings_payload(self) -> dict[str, object]:
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM notification_settings WHERE id=1").fetchone()
            deliveries = [dict(item) for item in connection.execute(
                "SELECT id,event,title,status,attempts,error,created_at,sent_at "
                "FROM notification_deliveries ORDER BY id DESC LIMIT 50"
            )]
        try:
            selected = json.loads(row["events_json"] or "{}") if row else {}
        except (TypeError, json.JSONDecodeError):
            selected = {}
        events = [
            {
                "key": key,
                "label": value["label"],
                "description": value["description"],
                "enabled": bool(selected.get(key, value["default"])),
            }
            for key, value in EVENTS.items()
        ]
        return {
            "enabled": bool(row["enabled"]) if row else True,
            "configured": self.configured,
            "webhook_hint": "Configured by ROMMATES_DISCORD_WEBHOOK_URL" if self.configured else "Not configured",
            "public_url": self.settings.public_url,
            "events": events,
            "deliveries": deliveries,
        }

    def update_settings(self, enabled: bool, events: dict[str, bool]) -> dict[str, object]:
        selected = {
            key: bool(events.get(key, value["default"]))
            for key, value in EVENTS.items()
        }
        with self.db.write() as connection:
            connection.execute(
                "UPDATE notification_settings SET enabled=?,events_json=?,updated_at=CURRENT_TIMESTAMP WHERE id=1",
                (int(enabled), json.dumps(selected, sort_keys=True)),
            )
        return self.settings_payload()

    def _event_enabled(self, event: str) -> bool:
        with self.db.connect() as connection:
            row = connection.execute("SELECT enabled,events_json FROM notification_settings WHERE id=1").fetchone()
        if not row or not row["enabled"]:
            return False
        try:
            selected = json.loads(row["events_json"] or "{}")
        except (TypeError, json.JSONDecodeError):
            selected = {}
        return bool(selected.get(event, EVENTS.get(event, {}).get("default", False)))

    def event_enabled(self, event: str) -> bool:
        """Return whether polling work for an event is worth performing."""
        return self.configured and self._event_enabled(event)

    def notify(
        self,
        event: str,
        title: str,
        detail: str,
        path: str = "",
        *,
        dedupe_key: str = "",
        force: bool = False,
    ) -> int | None:
        if not self.configured:
            if force:
                raise LibraryError("Set ROMMATES_DISCORD_WEBHOOK_URL before sending a test")
            return None
        if not force and not self._event_enabled(event):
            return None
        with self.db.write() as connection:
            if dedupe_key:
                existing = connection.execute(
                    "SELECT id FROM notification_deliveries WHERE event=? AND dedupe_key=? LIMIT 1",
                    (event, dedupe_key),
                ).fetchone()
                if existing:
                    return int(existing["id"])
            connection.execute(
                "INSERT INTO notification_deliveries(event,title,detail,path,dedupe_key,status) "
                "VALUES(?,?,?,?,?,'queued')",
                (event, title[:255], detail[:2000], path[:1000], dedupe_key[:1000]),
            )
            delivery_id = int(connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"])
        with self._lock:
            if self._closed:
                return delivery_id
            self._executor.submit(self._deliver, delivery_id)
        return delivery_id

    def _payload(self, row: dict[str, Any]) -> dict[str, object]:
        url = ""
        if self.settings.public_url and row.get("path"):
            url = f"{self.settings.public_url.rstrip('/')}/{str(row['path']).lstrip('/')}"
        fields = []
        if url:
            fields.append({"name": "Open in ROMmates", "value": url, "inline": False})
        return {
            "username": "ROMmates",
            "allowed_mentions": {"parse": []},
            "embeds": [{
                "title": str(row["title"]),
                "description": str(row["detail"]),
                "color": 0x8F7AEA if row["event"] != "job_failed" else 0xE06060,
                "fields": fields,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }],
        }

    def _deliver(self, delivery_id: int) -> None:
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM notification_deliveries WHERE id=?", (delivery_id,)).fetchone()
        if not row:
            return
        payload = self._payload(dict(row))
        last_error = ""
        for attempt in range(1, 4):
            try:
                request = Request(
                    self.settings.discord_webhook_url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json", "User-Agent": "ROMmates/0.1"},
                    method="POST",
                )
                with urlopen(request, timeout=self.settings.discord_timeout_seconds) as response:
                    response.read(1024)
                with self.db.write() as connection:
                    connection.execute(
                        "UPDATE notification_deliveries SET status='sent',attempts=?,error='',sent_at=CURRENT_TIMESTAMP WHERE id=?",
                        (attempt, delivery_id),
                    )
                return
            except HTTPError as exc:
                last_error = f"Discord returned HTTP {exc.code}"
                if exc.code == 429:
                    try:
                        retry = json.loads(exc.read().decode("utf-8")).get("retry_after", 1)
                        time.sleep(min(max(float(retry), 0.25), 10.0))
                        continue
                    except (ValueError, TypeError, json.JSONDecodeError):
                        pass
            except (URLError, TimeoutError, OSError) as exc:
                last_error = f"Discord could not be reached: {exc}"
            if attempt < 3:
                time.sleep(attempt)
        with self.db.write() as connection:
            connection.execute(
                "UPDATE notification_deliveries SET status='failed',attempts=3,error=? WHERE id=?",
                (last_error[:1000] or "Discord delivery failed", delivery_id),
            )

    def test(self) -> dict[str, object]:
        delivery_id = self.notify(
            "test", "Discord notifications are connected",
            "ROMmates can deliver notifications to this channel.",
            "notifications", force=True,
        )
        return {"delivery_id": delivery_id, "status": "queued"}
