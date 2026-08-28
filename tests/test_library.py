from __future__ import annotations

import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.db import Database
from app.library import COPY_SUFFIX, JobCancelled, LibraryError, LibraryService, normalize_name


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
            database_path=root / "data" / "rommates.db",
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
        self.assertEqual(result["games"], 1)
        self.assertEqual(result["platforms"], 1)
        self.assertEqual(result["skipped_count"], 0)
        game, files = self.service.game_bundle(self.game_id("Game (USA)"))
        self.assertEqual(game["extension"], ".cue")
        self.assertEqual({Path(item["relpath"]).suffix for item in files}, {".cue", ".bin"})

    def test_scan_groups_gdi_tracks_as_one_bundle(self):
        self.write("dreamcast/Alienfront Online (USA)/track01.bin", b"track-one")
        self.write("dreamcast/Alienfront Online (USA)/track02.raw", b"track-two")
        self.write("dreamcast/Alienfront Online (USA)/track03.bin", b"track-three")
        self.write(
            "dreamcast/Alienfront Online (USA)/Alienfront Online (USA).gdi",
            "3\n"
            "1 0 4 2352 track01.bin 0\n"
            "2 45000 0 2352 track02.raw 0\n"
            "3 45000 4 2352 track03.bin 0\n",
        )

        result = self.service.scan()

        self.assertEqual(result["games"], 1)
        game, files = self.service.game_bundle(self.game_id("Alienfront Online (USA)"))
        self.assertEqual(game["extension"], ".gdi")
        self.assertEqual(len(files), 4)
        self.assertEqual(
            {Path(item["relpath"]).name for item in files},
            {"Alienfront Online (USA).gdi", "track01.bin", "track02.raw", "track03.bin"},
        )

    def test_gdi_unquoted_filenames_with_spaces_do_not_become_track_games(self):
        for title, unique in (("Rez (Europe)", b"rez"), ("Rayman 2 (Europe)", b"rayman")):
            self.write(f"dreamcast/{title}/{title} (Track 1).bin", unique + b"-one")
            self.write(f"dreamcast/{title}/{title} (Track 2).bin", b"shared-audio-track")
            self.write(f"dreamcast/{title}/{title} (Track 3).bin", unique + b"-three")
            self.write(
                f"dreamcast/{title}/{title}.gdi",
                "3\n"
                f"1 0 4 2352 {title} (Track 1).bin 0\n"
                f"2 45000 0 2352 {title} (Track 2).bin 0\n"
                f"3 45150 4 2352 {title} (Track 3).bin 0\n",
            )

        result = self.service.scan()

        self.assertEqual(result["games"], 2)
        with self.db.connect() as connection:
            games = connection.execute(
                "SELECT id,display_name,extension,bundle_hash FROM games ORDER BY display_name"
            ).fetchall()
        self.assertEqual({game["display_name"] for game in games}, {"Rez (Europe)", "Rayman 2 (Europe)"})
        self.assertTrue(all(game["extension"] == ".gdi" for game in games))
        self.assertEqual(len({game["bundle_hash"] for game in games}), 2)
        for game in games:
            _, files = self.service.game_bundle(game["id"])
            self.assertEqual(len(files), 4)

    def test_ps3_directory_trees_are_complete_folder_bundles(self):
        for release in ("BLES01756", "BLUS31059"):
            base = f"ps3/Zone of the Enders HD Collection [{release}].ps3/PS3_GAME"
            self.write(f"{base}/PARAM.SFO", b"same-metadata")
            self.write(f"{base}/USRDIR/data/amabs.bin", b"same-content")

        result = self.service.scan()

        self.assertEqual(result["games"], 2)
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT display_name,primary_relpath,bundle_hash,normalized_name,size "
                "FROM games ORDER BY display_name"
            ).fetchall()
        self.assertTrue(all(row["primary_relpath"].endswith(".ps3") for row in rows))
        self.assertEqual(len({row["bundle_hash"] for row in rows}), 1)
        self.assertEqual({row["normalized_name"] for row in rows}, {"zone of the enders hd collection"})
        self.assertTrue(all(row["size"] == len(b"same-metadata") + len(b"same-content") for row in rows))
        game, files = self.service.game_bundle(self.game_id("Zone of the Enders HD Collection [BLES01756]"))
        self.assertEqual(game["extension"], ".ps3")
        self.assertEqual(len(files), 2)

    def test_cartridge_collection_folders_remain_individual_games(self):
        self.write("gba/gba-top100/First Game.gba", b"first")
        self.write("gba/gba-top100/Second Game.gba", b"second")

        result = self.service.scan()

        self.assertEqual(result["games"], 2)
        with self.db.connect() as connection:
            names = {row["display_name"] for row in connection.execute("SELECT display_name FROM games")}
        self.assertEqual(names, {"First Game", "Second Game"})

    def test_folder_bundle_rename_moves_the_complete_tree(self):
        self.write("ps3/Old Folder.ps3/PS3_GAME/PARAM.SFO", b"metadata")
        self.write("ps3/Old Folder.ps3/PS3_GAME/USRDIR/game.bin", b"content")
        self.service.scan()

        game_id = self.game_id("Old Folder")
        self.service.rename_bundle(game_id, "New Folder")

        self.assertFalse((self.roms / "ps3/Old Folder.ps3").exists())
        self.assertTrue((self.roms / "ps3/New Folder.ps3/PS3_GAME/PARAM.SFO").is_file())
        self.assertTrue((self.roms / "ps3/New Folder.ps3/PS3_GAME/USRDIR/game.bin").is_file())
        with self.db.connect() as connection:
            game = connection.execute("SELECT primary_relpath,display_name FROM games").fetchone()
        self.assertEqual(game["primary_relpath"], "ps3/New Folder.ps3")
        self.assertEqual(game["display_name"], "New Folder")

    def test_folder_bundle_delete_and_restore_moves_the_complete_tree(self):
        self.write("ps3/Restore Folder.ps3/PS3_GAME/PARAM.SFO", b"metadata")
        self.write("ps3/Restore Folder.ps3/PS3_GAME/USRDIR/game.bin", b"content")
        self.service.scan()

        deleted = self.service.delete_bundle(self.game_id("Restore Folder"))
        self.assertFalse((self.roms / "ps3/Restore Folder.ps3").exists())

        self.service.restore_trash(deleted["trash_id"])
        self.assertTrue((self.roms / "ps3/Restore Folder.ps3/PS3_GAME/PARAM.SFO").is_file())
        self.assertTrue((self.roms / "ps3/Restore Folder.ps3/PS3_GAME/USRDIR/game.bin").is_file())

    def test_scan_reports_byte_and_file_progress(self):
        self.write("gba/One.gba", b"a" * (2 * 1024 * 1024))
        self.write("gba/Two.gba", b"two")
        updates: list[tuple[int, str]] = []

        self.service.scan(progress_callback=lambda percent, detail: updates.append((percent, detail)))

        self.assertEqual(updates[0], (0, "Discovering library files"))
        self.assertTrue(any("Hashing" in detail and "of 2 files" in detail for _, detail in updates))
        self.assertTrue(any(percent == 92 and "2 games" in detail for percent, detail in updates))
        self.assertEqual(updates[-1], (99, "Finalizing scan"))

    def test_interrupted_scan_preserves_completed_hash_batches(self):
        self.write("gba/A.gba", b"first")
        self.write("gba/B.gba", b"second")
        original_hash = self.service._hash_file

        def interrupt_on_second(path, *args, **kwargs):
            if path.name == "B.gba":
                raise RuntimeError("simulated interruption")
            return original_hash(path, *args, **kwargs)

        with (
            patch("app.library.HASH_CACHE_BATCH_FILES", 1),
            patch.object(self.service, "_hash_file", side_effect=interrupt_on_second),
            self.assertRaisesRegex(RuntimeError, "simulated interruption"),
        ):
            self.service.scan()

        with self.db.connect() as connection:
            cached = connection.execute("SELECT relpath FROM file_cache ORDER BY relpath").fetchall()
        self.assertEqual([row["relpath"] for row in cached], ["gba/A.gba"])

        # The next scan reuses the durable hash instead of reading the first ROM again.
        reused: list[str] = []
        original_open = Path.open

        def track_open(path, *args, **kwargs):
            if path.suffix == ".gba":
                reused.append(path.name)
            return original_open(path, *args, **kwargs)

        with patch("pathlib.Path.open", new=track_open):
            self.service.scan()
        self.assertEqual(reused, ["B.gba"])

    def test_cancelled_scan_keeps_hash_checkpoint_without_reconciling_catalog(self):
        self.write("gba/A.gba", b"first")
        self.write("gba/B.gba", b"second")
        cancel_requested = False

        def progress(_, detail):
            nonlocal cancel_requested
            if "Hashing 1 of 2 files" in detail:
                cancel_requested = True

        def check_cancelled():
            if cancel_requested:
                raise JobCancelled("Stopped by user")

        with patch("app.library.HASH_CACHE_BATCH_FILES", 1), self.assertRaises(JobCancelled):
            self.service.scan(progress_callback=progress, cancel_check=check_cancelled)

        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS c FROM games").fetchone()["c"], 0)
            cached = connection.execute("SELECT relpath FROM file_cache ORDER BY relpath").fetchall()
        self.assertEqual([row["relpath"] for row in cached], ["gba/A.gba"])

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

    def test_scan_refuses_to_prune_a_catalog_when_the_library_disappears(self):
        # An unmounted or still-mounting library root looks like an empty one. Pruning
        # it would cascade through games -> device_selections -> deployments.
        self.write("gba/One.gba", b"one")
        self.write("gba/Two.gba", b"two")
        (self.devices / "handheld" / "roms").mkdir(parents=True)
        self.service.scan()
        with self.db.connect() as connection:
            device_id = connection.execute("SELECT id FROM devices").fetchone()["id"]
            game_ids = [row["id"] for row in connection.execute("SELECT id FROM games")]
        self.service.set_selections(device_id, game_ids, True)
        self.service.apply_device(device_id)

        shutil.rmtree(self.roms / "gba")
        with self.assertRaisesRegex(LibraryError, "Scan would remove 2 of 2"):
            self.service.scan()
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS c FROM games").fetchone()["c"], 2)
            self.assertEqual(
                connection.execute("SELECT COUNT(*) AS c FROM device_selections").fetchone()["c"], 2
            )
            self.assertEqual(
                connection.execute("SELECT COUNT(*) AS c FROM deployments").fetchone()["c"], 2
            )

    def test_confirmed_scan_prunes_the_catalog(self):
        self.write("gba/One.gba", b"one")
        self.service.scan()
        shutil.rmtree(self.roms / "gba")
        self.assertEqual(self.service.scan(force_prune=True)["games"], 0)
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS c FROM games").fetchone()["c"], 0)

    def test_small_removals_prune_without_confirmation(self):
        for index in range(10):
            self.write(f"gba/Game {index}.gba", f"rom-{index}".encode())
        self.service.scan()
        (self.roms / "gba/Game 3.gba").unlink()
        self.assertEqual(self.service.scan()["games"], 9)

    def test_stale_devices_are_removed_but_an_empty_devices_root_is_not_trusted(self):
        self.write("gba/One.gba", b"one")
        (self.devices / "keep" / "roms").mkdir(parents=True)
        (self.devices / "gone" / "roms").mkdir(parents=True)
        self.service.scan()
        shutil.rmtree(self.devices / "gone")
        self.assertEqual(self.service.scan()["removed_devices"], ["gone"])
        with self.db.connect() as connection:
            self.assertEqual([row["name"] for row in connection.execute("SELECT name FROM devices")], ["keep"])
        shutil.rmtree(self.devices / "keep")
        self.assertEqual(self.service.scan()["removed_devices"], [])
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS c FROM devices").fetchone()["c"], 1)

    def test_failed_apply_still_records_the_files_it_copied(self):
        # Files on the device that no deployment row claims can never be cleaned up,
        # so a partial apply must leave the catalog describing what actually landed.
        for name in ("A", "B", "C"):
            self.write(f"gba/{name}.gba", name.encode() * 8)
        device_roms = self.devices / "handheld" / "roms"
        device_roms.mkdir(parents=True)
        self.service.scan()
        with self.db.connect() as connection:
            device_id = connection.execute("SELECT id FROM devices").fetchone()["id"]
            game_ids = [row["id"] for row in connection.execute("SELECT id FROM games ORDER BY id")]
        self.service.set_selections(device_id, game_ids, True)

        original_copy = shutil.copy2
        calls = {"count": 0}

        def failing_copy(source, target, *args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 2:
                original_copy(source, target)
                raise OSError("simulated I/O failure")
            return original_copy(source, target, *args, **kwargs)

        with patch("app.library.shutil.copy2", side_effect=failing_copy):
            with self.assertRaises(OSError):
                self.service.apply_device(device_id)

        on_disk = sorted(p.name for p in device_roms.rglob("*") if p.is_file())
        with self.db.connect() as connection:
            recorded = connection.execute("SELECT COUNT(*) AS c FROM deployments").fetchone()["c"]
        self.assertEqual(len(on_disk), recorded)

        # Everything that landed is managed, so unselecting removes it.
        self.service.set_selections(device_id, game_ids, False)
        self.service.apply_device(device_id)
        self.assertEqual([p.name for p in device_roms.rglob("*") if p.is_file()], [])

    def test_apply_clears_interrupted_copy_temp_files(self):
        self.write("gba/Real.gba", b"real")
        device_roms = self.devices / "handheld" / "roms" / "gba"
        device_roms.mkdir(parents=True)
        (device_roms / ".Orphan.gba.rommates-copy").write_bytes(b"partial")
        self.service.scan()
        with self.db.connect() as connection:
            device_id = connection.execute("SELECT id FROM devices").fetchone()["id"]
        result = self.service.apply_device(device_id)
        self.assertEqual(result["metadata_removed"], 1)
        self.assertEqual(list(device_roms.rglob("*rommates-copy")), [])

    def test_cancelled_apply_removes_partial_copy_and_records_nothing(self):
        self.write("gba/Large.gba", b"x" * (2 * 1024 * 1024))
        device_roms = self.devices / "handheld" / "roms"
        device_roms.mkdir(parents=True)
        self.service.scan()
        with self.db.connect() as connection:
            device_id = connection.execute("SELECT id FROM devices").fetchone()["id"]
        self.service.set_selection(device_id, self.game_id("Large"), True)

        def cancel_during_copy():
            if list(device_roms.rglob(f"*{COPY_SUFFIX}")):
                raise JobCancelled("Stopped by user")

        with self.assertRaises(JobCancelled):
            self.service.apply_device(device_id, cancel_check=cancel_during_copy)

        self.assertFalse((device_roms / "gba/Large.gba").exists())
        self.assertEqual(list(device_roms.rglob("*rommates-copy")), [])
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS c FROM deployments").fetchone()["c"], 0)

    def test_apply_also_clears_legacy_rom_manager_temp_files(self):
        device_roms = self.devices / "handheld" / "roms" / "gba"
        device_roms.mkdir(parents=True)
        (device_roms / ".Orphan.gba.rommanager-copy").write_bytes(b"partial")
        self.service.scan()
        with self.db.connect() as connection:
            device_id = connection.execute("SELECT id FROM devices").fetchone()["id"]
        self.assertEqual(self.service.apply_device(device_id)["metadata_removed"], 1)

    def test_symlinked_roms_are_reported_rather_than_silently_dropped(self):
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        (outside / "Escaped.gba").write_bytes(b"escaped")
        self.write("gba/Normal.gba", b"normal")
        (self.roms / "gba" / "Linked.gba").symlink_to(outside / "Escaped.gba")
        result = self.service.scan()
        self.assertEqual(result["games"], 1)
        self.assertEqual(result["skipped_count"], 1)
        self.assertIn("Linked.gba", result["skipped"][0])

    def test_history_tables_are_trimmed(self):
        with self.db.write() as connection:
            connection.executemany(
                "INSERT INTO activity(action,detail) VALUES('scan',?)",
                ((f"entry {index}",) for index in range(40)),
            )
        self.db.prune_history(keep_jobs=5, keep_activity=10)
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) AS c FROM activity").fetchone()["c"], 10)
            newest = connection.execute("SELECT detail FROM activity ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(newest["detail"], "entry 39")

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
