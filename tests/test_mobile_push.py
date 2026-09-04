from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.config import Settings
from app.db import Database
from app.mobile_push import APNsResult, MobilePushService


class FakeProvider:
    configured = True

    def __init__(self, results=None):
        self.results = list(results or [APNsResult(200)])
        self.calls = []

    def send(self, token, payload):
        self.calls.append((token, payload))
        return self.results.pop(0)

    def close(self):
        return None


class MobilePushServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.db = Database(root / "rommates.db")
        self.db.initialize()
        with self.db.write() as connection:
            connection.execute(
                "INSERT INTO users(username,username_normalized,display_name,password_hash,role) "
                "VALUES('viewer','viewer','Viewer','hash','viewer')"
            )
            self.user_id = int(
                connection.execute("SELECT last_insert_rowid() id").fetchone()["id"]
            )
        self.settings = Settings(
            library_root=root / "roms",
            devices_root=root / "devices",
            trash_root=root / "trash",
            database_path=root / "rommates.db",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def register(self, service):
        service.register(
            self.user_id,
            "550e8400-e29b-41d4-a716-446655440000",
            "ab" * 32,
            "1.0 (1)",
            True,
        )

    def test_notification_is_queued_and_delivered_once(self):
        provider = FakeProvider()
        service = MobilePushService(self.settings, self.db, provider=provider)
        self.register(service)
        notification_id = service.notify_user(
            self.user_id,
            "device_sync",
            "Handheld finished syncing",
            "Three games were added.",
            "devices?device=1",
            "device-sync:1:complete",
        )
        self.assertTrue(service.drain_once())
        self.assertFalse(service.drain_once())
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(provider.calls[0][1]["notification_id"], notification_id)
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM mobile_push_outbox").fetchone()
        self.assertEqual(row["status"], "sent")
        self.assertEqual(row["attempt_count"], 1)

    def test_disabled_preference_does_not_queue_event(self):
        service = MobilePushService(self.settings, self.db, provider=FakeProvider())
        self.register(service)
        service.update_preferences(self.user_id, {"device_sync": False})
        service.notify_user(
            self.user_id, "device_sync", "Done", "Done", "devices", "disabled-event"
        )
        with self.db.connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) count FROM mobile_push_outbox"
            ).fetchone()["count"]
        self.assertEqual(count, 0)

    def test_invalid_apns_token_disables_installation(self):
        provider = FakeProvider([APNsResult(410, "Unregistered")])
        service = MobilePushService(self.settings, self.db, provider=provider)
        self.register(service)
        service.notify_user(
            self.user_id,
            "device_ready",
            "Ready",
            "Ready to sync",
            "devices",
            "device-ready",
        )
        self.assertTrue(service.drain_once())
        with self.db.connect() as connection:
            installation = connection.execute(
                "SELECT notifications_enabled FROM mobile_installations"
            ).fetchone()
            outbox = connection.execute("SELECT status FROM mobile_push_outbox").fetchone()
        self.assertEqual(installation["notifications_enabled"], 0)
        self.assertEqual(outbox["status"], "failed")


if __name__ == "__main__":
    unittest.main()
