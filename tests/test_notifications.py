from __future__ import annotations

import io
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.db import Database
from app.library import LibraryError
from app.notifications import NotificationService


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class NotificationServiceTests(unittest.TestCase):
    def make_service(self, directory: str, webhook: str = "https://discord.com/api/webhooks/1/secret"):
        database = Database(Path(directory) / "rommates.db")
        database.initialize()
        settings = Settings(
            library_root=Path(directory) / "roms",
            devices_root=Path(directory) / "devices",
            trash_root=Path(directory) / "trash",
            database_path=database.path,
            discord_webhook_url=webhook,
            public_url="https://rommates.example.test",
        )
        service = NotificationService(settings, database)
        service.initialize()
        return database, service

    def test_unconfigured_test_explains_required_setting(self):
        with tempfile.TemporaryDirectory() as directory:
            _, service = self.make_service(directory, webhook="")
            with self.assertRaisesRegex(LibraryError, "ROMMATES_DISCORD_WEBHOOK_URL"):
                service.test()
            service.close()

    def test_delivery_uses_embed_without_mentions_and_records_success(self):
        with tempfile.TemporaryDirectory() as directory:
            database, service = self.make_service(directory)
            payloads = []

            def fake_urlopen(request, timeout):
                payloads.append((json.loads(request.data), timeout))
                return FakeResponse(b"")

            with patch("app.notifications.urlopen", side_effect=fake_urlopen):
                delivery_id = service.notify("upload", "Upload complete", "Added Tetris", "transfers")
                deadline = time.monotonic() + 2
                row = None
                while time.monotonic() < deadline:
                    with database.connect() as connection:
                        row = connection.execute(
                            "SELECT * FROM notification_deliveries WHERE id=?", (delivery_id,)
                        ).fetchone()
                    if row and row["status"] == "sent":
                        break
                    time.sleep(0.01)

            service.close()
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "sent")
            self.assertEqual(payloads[0][0]["allowed_mentions"], {"parse": []})
            self.assertIn("https://rommates.example.test/transfers", json.dumps(payloads[0][0]))

    def test_preferences_and_dedupe_suppress_extra_deliveries(self):
        with tempfile.TemporaryDirectory() as directory:
            database, service = self.make_service(directory)
            service.update_settings(True, {"upload": False, "save_conflict": True})
            self.assertIsNone(service.notify("upload", "Upload", "Skipped"))
            with patch("app.notifications.urlopen", return_value=FakeResponse(b"")):
                first = service.notify(
                    "save_conflict", "Conflict", "Needs review", dedupe_key="save:one"
                )
                second = service.notify(
                    "save_conflict", "Conflict", "Needs review", dedupe_key="save:one"
                )
            service.close()
            self.assertEqual(first, second)
            with database.connect() as connection:
                count = connection.execute(
                    "SELECT COUNT(*) AS count FROM notification_deliveries"
                ).fetchone()["count"]
            self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()
