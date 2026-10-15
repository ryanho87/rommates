from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from app.config import Settings
from app.db import Database
from app.save_sync import SaveSyncMonitor


class FakeSyncthing:
    configured = True

    def __init__(self):
        self.devices: dict[str, dict] = {}
        self.completion: dict[tuple[str, str], dict] = {}
        self.stats: dict[str, dict] = {}
        self.members: set[str] = set()

    def status(self, refresh: bool = False):
        return {"devices": list(self.devices.values())}

    def _save_folder_id(self, root):
        return "saves-folder"

    def _get(self, path):
        if path == "/rest/stats/device":
            return self.stats
        if path.startswith("/rest/config/folders/"):
            return {"devices": [{"deviceID": device_id} for device_id in sorted(self.members)]}
        query = parse_qs(urlparse(path).query)
        return self.completion.get((query["folder"][0], query["device"][0]), {"needItems": 0, "needBytes": 0, "completion": 100})

    # helpers for the scenarios
    def set(self, device_id, *, connected, last_seen=None, need=0, member=True, stats_seen=None):
        # The connections list only knows a last-seen for the current session;
        # the real value lives in /rest/stats/device, which is what the monitor uses.
        self.devices[device_id] = {"device_id": device_id, "name": device_id[:7], "connected": connected, "last_seen": ""}
        self.stats[device_id] = {"lastSeen": stats_seen if stats_seen is not None else (last_seen or "0001-01-01T00:00:00Z")}
        if member:
            self.members.add(device_id)
        else:
            self.members.discard(device_id)
        self.completion[("saves-folder", device_id)] = {"needItems": need, "needBytes": need * 131072, "completion": 100 if not need else 80}


class FakeNotifications:
    def __init__(self):
        self.sent: list[tuple[str, str, str, str, str]] = []

    def event_enabled(self, event):
        return True

    def notify(self, event, title, detail, path="", *, dedupe_key=""):
        self.sent.append((event, title, detail, path, dedupe_key))

    def events(self):
        return [item[0] for item in self.sent]


class SaveSyncMonitorTests(unittest.TestCase):
    RG = "F6G43OM-TNYC6PJ-SPRCS6F-WLR4EAI"

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.settings = Settings(
            library_root=root / "roms", devices_root=root / "devices", trash_root=root / "trash",
            database_path=root / "db.sqlite", saves_root=root / "saves", snapshots_root=root / "snap",
            scan_on_start=False,
        )
        self.db = Database(self.settings.database_path)
        self.db.initialize()
        with self.db.write() as connection:
            connection.execute(
                "INSERT INTO devices(name,path,syncthing_device_id) VALUES(?,?,?)",
                ("rg_rotate", "device-rg", self.RG),
            )
            self.device_id = connection.execute("SELECT id FROM devices").fetchone()["id"]
        self.syncthing = FakeSyncthing()
        self.notifications = FakeNotifications()
        self.monitor = SaveSyncMonitor(self.settings, self.db, self.syncthing, self.notifications)
        self.now = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)

    def tick(self, minutes=1):
        self.now += timedelta(minutes=minutes)
        return self.monitor.check_once(now=self.now)

    def test_reconnecting_while_behind_warns_once_and_clears_on_catch_up(self):
        self.syncthing.set(self.RG, connected=False, last_seen="2026-10-14T12:00:00Z", need=3)
        self.tick()
        self.assertEqual(self.notifications.events(), [])

        self.syncthing.set(self.RG, connected=True, need=3)
        self.tick()
        self.assertEqual(self.notifications.events(), ["device_online", "save_sync_behind"])
        self.assertIn("3 save files still to download", self.notifications.sent[1][2])
        self.assertIn("wait before playing", self.notifications.sent[0][2].casefold())

        # Still connected and still behind: no nagging.
        self.tick()
        self.assertEqual(len(self.notifications.sent), 2)

        # Caught up: state clears so a later episode can warn again.
        self.syncthing.set(self.RG, connected=True, need=0)
        self.tick()
        row = self.monitor.latest(self.device_id)
        self.assertEqual(row["need_items"], 0)
        self.assertIsNone(row["reconnect_notified_at"])
        self.assertIsNone(row["behind_since"])

        self.syncthing.set(self.RG, connected=False, last_seen=self.now.isoformat(), need=2)
        self.tick()
        self.syncthing.set(self.RG, connected=True, need=2)
        self.tick()
        self.assertEqual(self.notifications.events().count("save_sync_behind"), 2)

    def test_online_alert_says_saves_are_up_to_date_when_nothing_is_pending(self):
        self.syncthing.set(self.RG, connected=False, last_seen="2026-10-15T11:00:00Z", need=0)
        self.tick()
        self.syncthing.set(self.RG, connected=True, need=0)
        self.tick()
        self.assertEqual(self.notifications.events(), ["device_online"])
        self.assertIn("saves up to date", self.notifications.sent[0][2])

    def test_a_device_dark_for_a_week_is_flagged_once_until_it_returns(self):
        self.syncthing.set(self.RG, connected=False, last_seen=(self.now - timedelta(days=8)).isoformat(), need=0)
        self.tick()
        self.assertEqual(self.notifications.events(), ["save_sync_stale"])
        self.assertIn("8 days", self.notifications.sent[0][1])
        self.tick(minutes=60)
        self.assertEqual(len(self.notifications.sent), 1)

        self.syncthing.set(self.RG, connected=True, need=0)
        self.tick()
        self.assertIsNone(self.monitor.latest(self.device_id)["stale_notified_at"])
        self.assertEqual(self.monitor.latest(self.device_id)["connected"], 1)

    def test_recently_seen_offline_device_is_not_stale(self):
        self.syncthing.set(self.RG, connected=False, last_seen=(self.now - timedelta(days=2)).isoformat(), need=0)
        self.tick()
        self.assertEqual(self.notifications.events(), [])

    def test_unknown_last_seen_never_counts_as_stale(self):
        self.syncthing.set(self.RG, connected=False, last_seen="0001-01-01T00:00:00Z", need=0)
        self.tick()
        self.assertEqual(self.notifications.events(), [])
        self.assertIsNone(self.monitor.latest(self.device_id)["last_seen"])

    def test_a_device_that_does_not_share_the_save_folder_is_neutral(self):
        # Syncthing reports every file as missing for a non-member; that is not
        # "behind", and a dark non-member must not be flagged as stale either.
        self.syncthing.set(self.RG, connected=False, last_seen=(self.now - timedelta(days=30)).isoformat(), need=623, member=False)
        self.tick()
        row = self.monitor.latest(self.device_id)
        self.assertEqual(row["folder_id"], "")
        self.assertEqual(row["need_items"], 0)
        self.assertEqual(self.notifications.events(), [])

        self.syncthing.set(self.RG, connected=True, need=623, member=False)
        self.tick()
        self.assertEqual(self.notifications.events(), ["device_online"])
        self.assertIn("not syncing saves", self.notifications.sent[0][2])

    def test_last_seen_comes_from_device_stats_not_the_connections_list(self):
        # The status entry carries the session zero-time; stats has the truth.
        self.syncthing.set(self.RG, connected=False, need=0, stats_seen=(self.now - timedelta(days=9)).isoformat())
        self.tick()
        self.assertEqual(self.notifications.events(), ["save_sync_stale"])
        self.assertIn("9 days", self.notifications.sent[0][1])
        self.assertTrue(self.monitor.latest(self.device_id)["last_seen"].startswith((self.now - timedelta(days=9)).strftime("%Y-%m-%d")))

    def test_nothing_happens_without_syncthing(self):
        self.syncthing.configured = False
        self.assertEqual(self.tick(), 0)
        self.assertIsNone(self.monitor.latest(self.device_id))


if __name__ == "__main__":
    unittest.main()
