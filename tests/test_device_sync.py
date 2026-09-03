from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.db import Database
from app.device_sync import DeviceSyncMonitor


class FakeSyncthing:
    def __init__(self, statuses):
        self.statuses = list(statuses)
        self.calls = 0

    def folder_sequence(self, _folder_id):
        return 20

    def device_sync_status(self, *_args, **_kwargs):
        status = self.statuses[min(self.calls, len(self.statuses) - 1)]
        self.calls += 1
        return status


class FakeNotifications:
    def __init__(self):
        self.calls = []

    def notify(self, *args, **kwargs):
        self.calls.append((args, kwargs))


class DeviceSyncMonitorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temporary.name) / "rommates.db")
        self.db.initialize()
        with self.db.write() as connection:
            connection.execute(
                "INSERT INTO users(username,username_normalized,display_name,password_hash,role) "
                "VALUES('jennifer','jennifer','Jennifer','hash','member')"
            )
            self.user_id = int(connection.execute("SELECT last_insert_rowid() id").fetchone()["id"])
            connection.execute(
                "INSERT INTO devices(name,path,owner_user_id,syncthing_device_id,syncthing_folder_id) "
                "VALUES('rg406v','rg406v',?,'REMOTE-ID','rg406v-roms')",
                (self.user_id,),
            )
            self.device_id = int(connection.execute("SELECT last_insert_rowid() id").fetchone()["id"])
            connection.execute(
                "INSERT INTO jobs(kind,status,detail,requested_by) VALUES('device_apply','complete','done',?)",
                (self.user_id,),
            )
            self.job_id = int(connection.execute("SELECT last_insert_rowid() id").fetchone()["id"])

    def tearDown(self):
        self.temporary.cleanup()

    def test_tracks_progress_and_notifies_once_after_remote_sequence_catches_up(self):
        syncthing = FakeSyncthing([
            {
                "linked": True, "connected": True, "completion": 70,
                "need_bytes": 3000, "need_items": 2, "need_deletes": 0,
                "remote_state": "valid", "sequence": 18,
            },
            {
                "linked": True, "connected": True, "completion": 100,
                "need_bytes": 0, "need_items": 0, "need_deletes": 0,
                "remote_state": "valid", "sequence": 18,
            },
            {
                "linked": True, "connected": True, "completion": 100,
                "need_bytes": 0, "need_items": 0, "need_deletes": 0,
                "remote_state": "valid", "sequence": 20,
            },
        ])
        notifications = FakeNotifications()
        monitor = DeviceSyncMonitor(self.db, syncthing, notifications)

        created = monitor.track(
            self.device_id, self.job_id, self.user_id, added=3, removed=1
        )
        self.assertEqual(created["state"], "pending")
        self.assertEqual(created["target_sequence"], 20)

        self.assertEqual(monitor.check_once(), 0)
        progressing = monitor.latest(self.device_id)
        self.assertEqual(progressing["state"], "syncing")
        self.assertEqual(progressing["completion"], 70)

        # A stale 100% response cannot finish a run until the remote has seen
        # the local sequence captured after this apply's rescan.
        self.assertEqual(monitor.check_once(), 0)
        self.assertEqual(monitor.latest(self.device_id)["state"], "syncing")

        self.assertEqual(monitor.check_once(), 1)
        completed = monitor.latest(self.device_id)
        self.assertEqual(completed["state"], "complete")
        self.assertIsNotNone(completed["completed_at"])
        with self.db.connect() as connection:
            inbox = connection.execute(
                "SELECT * FROM user_notifications WHERE user_id=?", (self.user_id,)
            ).fetchall()
        self.assertEqual(len(inbox), 1)
        self.assertIn("finished syncing", inbox[0]["title"])
        self.assertEqual(len(notifications.calls), 1)
        self.assertEqual(monitor.check_once(), 0)
        self.assertEqual(len(notifications.calls), 1)

    def test_offline_device_remains_pending_for_a_later_retry(self):
        syncthing = FakeSyncthing([{
            "linked": True, "connected": False, "completion": 15,
            "need_bytes": 9000, "need_items": 4, "need_deletes": 0,
            "remote_state": "unknown", "sequence": 4,
        }])
        monitor = DeviceSyncMonitor(self.db, syncthing, FakeNotifications())
        monitor.track(self.device_id, self.job_id, self.user_id, added=1, removed=0)
        self.assertEqual(monitor.check_once(), 0)
        self.assertEqual(monitor.latest(self.device_id)["state"], "offline")


if __name__ == "__main__":
    unittest.main()
