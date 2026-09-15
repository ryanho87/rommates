from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.auth import AuthService
from app.config import Settings
from app.db import Database
from app.library import LibraryError
from app.saves import SaveSnapshotService
from app.save_vaults import SaveVaultService


class PrivateSaveVaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = Settings(
            library_root=root / "roms", devices_root=root / "devices", trash_root=root / "trash",
            database_path=root / "db.sqlite", saves_root=root / "saves", snapshots_root=root / "snapshots",
            save_snapshot_quiet_seconds=0, save_retention_daily=0, save_retention_weekly=0, save_retention_monthly=0,
        )
        self.settings.saves_root.mkdir()
        self.db = Database(self.settings.database_path)
        self.db.initialize()
        auth = AuthService(self.db)
        auth.initialize()
        self.alice = auth.create_user("alice", "Alice", "test-password-long", "member")["id"]
        self.bob = auth.create_user("bob", "Bob", "test-password-long", "member")["id"]
        self.vaults = SaveVaultService(self.settings, self.db)
        self.legacy = SaveSnapshotService(self.settings, self.db)
        self.legacy.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, service, content):
        path = service.settings.saves_root / "retroarch/Pokemon.srm"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_independent_histories_restore_and_retention_leave_legacy_untouched(self):
        legacy_path = self.write(self.legacy, b"existing progress")
        legacy_snapshot = self.legacy.create_snapshot()["snapshot_id"]
        legacy_settings = self.legacy.settings_payload()
        alice_vault = self.vaults.create(self.alice)
        bob_vault = self.vaults.create(self.bob)
        alice = self.vaults.service(alice_vault["id"])
        bob = self.vaults.service(bob_vault["id"])
        alice_path = self.write(alice, b"alice first")
        bob_path = self.write(bob, b"bob first")
        alice_first = alice.create_snapshot()["snapshot_id"]
        bob_first = bob.create_snapshot()["snapshot_id"]
        alice_path.write_bytes(b"alice second")
        alice_second = alice.create_snapshot()["snapshot_id"]
        self.assertEqual(bob.create_snapshot()["snapshot_id"], bob_first)
        self.assertEqual(self.legacy.create_snapshot()["snapshot_id"], legacy_snapshot)
        for foreign in (bob_first, legacy_snapshot):
            for operation in (alice.snapshot_detail, alice.compare, lambda sid: alice.pin(sid, True), lambda sid: alice.restore_snapshot(sid, "")):
                with self.assertRaisesRegex(LibraryError, "not found"):
                    operation(foreign)
        alice.restore_snapshot(alice_first, alice.compare(alice_first)["current_tree_hash"])
        self.assertEqual(alice_path.read_bytes(), b"alice first")
        self.assertEqual(bob_path.read_bytes(), b"bob first")
        self.assertEqual(legacy_path.read_bytes(), b"existing progress")
        self.assertEqual(self.legacy.list_snapshots()["total"], 1)
        self.assertEqual(bob.list_snapshots()["total"], 1)
        alice.update_settings({"retention_recent": 1})
        alice.prune_retention()
        bob_file = bob.snapshot_detail(bob_first)["files"][0]
        self.assertTrue(bob._blob_path(bob_file["sha256"]).is_file())
        self.assertEqual(self.legacy.settings_payload()["retention_recent"], legacy_settings["retention_recent"])
        self.assertEqual(alice.list_snapshots()["items"][0]["id"], alice_second)

    def test_vault_identity_survives_renames_and_restart_without_creating_admin_vault(self):
        self.assertEqual(self.vaults.list(), [])
        vault = self.vaults.create(self.alice)
        service = self.vaults.service(vault["id"])
        self.write(service, b"alice")
        with self.db.write() as connection:
            connection.execute("UPDATE users SET username='renamed',display_name='New name' WHERE id=?", (self.alice,))
        restarted = SaveVaultService(self.settings, self.db)
        self.assertEqual(restarted.create(self.alice)["storage_key"], vault["storage_key"])
        self.assertEqual(restarted.service(vault["id"]).current_files()["total"], 1)
        self.assertEqual(self.legacy.current_files()["total"], 0)
        self.assertEqual(len(restarted.list()), 1)
        self.assertFalse(self.settings.saves_root in service.settings.saves_root.parents)

    def test_conflicts_and_symlinks_cannot_escape_private_vault(self):
        vault = self.vaults.create(self.alice)
        service = self.vaults.service(vault["id"])
        path = self.write(service, b"current")
        conflict = path.with_name("Pokemon.sync-conflict-20260914-120000-DEVICE.srm")
        conflict.write_bytes(b"other")
        record = service.conflicts()["items"][0]
        service.resolve_conflict(record["conflict_relpath"], "conflict", record["canonical_sha256"], record["conflict_sha256"])
        self.assertEqual(path.read_bytes(), b"other")
        self.assertEqual(len(service.conflicts()["history"]), 1)
        self.assertEqual(self.legacy.conflicts()["history"], [])
        bob = self.vaults.service(self.vaults.create(self.bob)["id"])
        bob_path = self.write(bob, b"private")
        path.unlink()
        path.symlink_to(bob_path)
        conflict.write_bytes(b"attack")
        with self.assertRaisesRegex(LibraryError, "escaped"):
            service.conflicts()
        self.assertEqual(bob_path.read_bytes(), b"private")
        with self.assertRaisesRegex(LibraryError, "escaped"):
            service.resolve_conflict("../other.srm", "current", "", "")
