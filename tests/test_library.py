from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.db import Database
from app.library import LibraryService, normalize_name


class LibraryServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.roms = root / "roms"
        self.devices = root / "devices"
        self.trash = root / "trash"
        self.settings = Settings(
            library_root=self.roms,
            devices_root=self.devices,
            trash_root=self.trash,
            database_path=root / "data" / "rommanager.db",
            scan_on_start=False,
        )
        self.db = Database(self.settings.database_path)
        self.db.initialize()
        self.service = LibraryService(self.settings, self.db)
        self.service.prepare_roots()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relative: str, content: bytes | str) -> Path:
        path = self.roms / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
        return path

    def game_id(self, name: str) -> int:
        with self.db.connect() as connection:
            return connection.execute("SELECT id FROM games WHERE display_name=?", (name,)).fetchone()["id"]

    def test_name_normalization_removes_release_tags(self):
        self.assertEqual(normalize_name("Final.Fantasy_VII (USA) [Rev 1].chd"), "final fantasy vii")

    def test_scan_groups_cue_and_bin_as_one_bundle(self):
        self.write("psx/Game (USA).bin", b"disc-data")
        self.write("psx/Game (USA).cue", 'FILE "Game (USA).bin" BINARY\n  TRACK 01 MODE2/2352\n')
        result = self.service.scan()
        self.assertEqual(result, {"games": 1, "platforms": 1})
        game, files = self.service.game_bundle(self.game_id("Game (USA)"))
        self.assertEqual(game["extension"], ".cue")
        self.assertEqual({Path(item["relpath"]).suffix for item in files}, {".cue", ".bin"})

    def test_rename_bundle_updates_cue_reference(self):
        self.write("psx/Old Name.bin", b"disc-data")
        self.write("psx/Old Name.cue", 'FILE "Old Name.bin" BINARY\n')
        self.service.scan()
        game_id = self.game_id("Old Name")
        self.service.rename_bundle(game_id, "New Name")
        self.assertTrue((self.roms / "psx/New Name.bin").exists())
        self.assertEqual((self.roms / "psx/New Name.cue").read_text(), 'FILE "New Name.bin" BINARY\n')
        with self.db.connect() as connection:
            row = connection.execute("SELECT id,display_name FROM games").fetchone()
        self.assertEqual(row["id"], game_id)
        self.assertEqual(row["display_name"], "New Name")

    def test_equal_content_is_exact_duplicate_and_names_are_possible_match(self):
        self.write("gba/Game (USA).gba", b"same")
        self.write("gba/Game (Europe).gba", b"same")
        self.write("gba/Game [Hack].gba", b"different")
        self.service.scan()
        with self.db.connect() as connection:
            rows = connection.execute("SELECT bundle_hash,normalized_name FROM games ORDER BY display_name").fetchall()
        self.assertEqual(rows[0]["bundle_hash"], rows[1]["bundle_hash"])
        self.assertEqual({row["normalized_name"] for row in rows}, {"game"})

    def test_delete_and_restore_complete_bundle(self):
        self.write("psx/Restore Me.bin", b"disc-data")
        self.write("psx/Restore Me.cue", 'FILE "Restore Me.bin" BINARY\n')
        self.service.scan()
        result = self.service.delete_bundle(self.game_id("Restore Me"))
        self.assertFalse((self.roms / "psx/Restore Me.cue").exists())
        self.service.restore_trash(result["trash_id"])
        self.assertTrue((self.roms / "psx/Restore Me.cue").exists())
        self.assertTrue((self.roms / "psx/Restore Me.bin").exists())

    def test_device_apply_copies_unselects_and_cleans_appledouble(self):
        self.write("gba/Metroid.gba", b"rom-data")
        device_roms = self.devices / "retroid" / "roms"
        device_roms.mkdir(parents=True)
        (device_roms / "._junk").write_bytes(b"metadata")
        self.service.scan()
        game_id = self.game_id("Metroid")
        with self.db.connect() as connection:
            device_id = connection.execute("SELECT id FROM devices WHERE name='retroid'").fetchone()["id"]
        self.service.set_selection(device_id, game_id, True)
        applied = self.service.apply_device(device_id)
        self.assertEqual(applied["copied"], 1)
        self.assertEqual(applied["metadata_removed"], 1)
        self.assertTrue((device_roms / "gba/Metroid.gba").exists())
        self.service.set_selection(device_id, game_id, False)
        removed = self.service.apply_device(device_id)
        self.assertEqual(removed["removed"], 1)
        self.assertFalse((device_roms / "gba/Metroid.gba").exists())

    def test_bulk_device_selection_updates_visible_set(self):
        self.write("gba/One.gba", b"one")
        self.write("gba/Two.gba", b"two")
        (self.devices / "handheld" / "roms").mkdir(parents=True)
        self.service.scan()
        with self.db.connect() as connection:
            device_id = connection.execute("SELECT id FROM devices WHERE name='handheld'").fetchone()["id"]
            game_ids = [row["id"] for row in connection.execute("SELECT id FROM games ORDER BY id")]
        self.assertEqual(self.service.set_selections(device_id, game_ids, True), 2)
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS count FROM device_selections").fetchone()["count"], 2)
        self.assertEqual(self.service.set_selections(device_id, [game_ids[0]], False), 1)
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS count FROM device_selections").fetchone()["count"], 1)

    def test_delete_and_restore_also_restores_managed_device_copy(self):
        self.write("gba/Portable.gba", b"portable")
        device_roms = self.devices / "handheld" / "roms"
        device_roms.mkdir(parents=True)
        self.service.scan()
        game_id = self.game_id("Portable")
        with self.db.connect() as connection:
            device_id = connection.execute("SELECT id FROM devices WHERE name='handheld'").fetchone()["id"]
        self.service.set_selection(device_id, game_id, True)
        self.service.apply_device(device_id)
        deployed = device_roms / "gba/Portable.gba"
        self.assertTrue(deployed.exists())
        deleted = self.service.delete_bundle(game_id)
        self.assertFalse(deployed.exists())
        self.service.restore_trash(deleted["trash_id"])
        self.assertTrue(deployed.exists())
        with self.db.connect() as connection:
            restored = connection.execute(
                "SELECT COUNT(*) AS count FROM deployments WHERE device_id=?",
                (device_id,),
            ).fetchone()["count"]
        self.assertEqual(restored, 1)

    def test_apply_holds_operation_lock_for_entire_copy(self):
        self.write("gba/Locked.gba", b"locked")
        (self.devices / "handheld" / "roms").mkdir(parents=True)
        self.service.scan()
        game_id = self.game_id("Locked")
        with self.db.connect() as connection:
            device_id = connection.execute("SELECT id FROM devices WHERE name='handheld'").fetchone()["id"]
        self.service.set_selection(device_id, game_id, True)
        entered = threading.Event()
        release = threading.Event()
        scan_finished = threading.Event()
        original_copy = __import__("shutil").copy2

        def blocking_copy(source, target):
            entered.set()
            self.assertTrue(release.wait(timeout=2))
            return original_copy(source, target)

        with patch("app.library.shutil.copy2", side_effect=blocking_copy):
            apply_thread = threading.Thread(target=self.service.apply_device, args=(device_id,))
            apply_thread.start()
            self.assertTrue(entered.wait(timeout=2))
            scan_thread = threading.Thread(target=lambda: (self.service.scan(), scan_finished.set()))
            scan_thread.start()
            time.sleep(0.05)
            self.assertFalse(scan_finished.is_set())
            release.set()
            apply_thread.join(timeout=2)
            scan_thread.join(timeout=2)
        self.assertTrue(scan_finished.is_set())

    def test_required_roots_fail_fast(self):
        root = Path(self.temp.name) / "missing"
        settings = Settings(
            library_root=root / "roms",
            devices_root=root / "devices",
            trash_root=root / "trash",
            database_path=root / "db.sqlite",
            scan_on_start=False,
            require_existing_roots=True,
        )
        service = LibraryService(settings, Database(settings.database_path))
        with self.assertRaisesRegex(Exception, "Required mounted directories"):
            service.prepare_roots()


if __name__ == "__main__":
    unittest.main()
