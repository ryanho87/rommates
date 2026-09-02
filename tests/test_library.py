from __future__ import annotations

import errno
import os
import shutil
import struct
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.db import Database
from app.library import COPY_SUFFIX, LINK_SUFFIX, JobCancelled, LibraryError, LibraryService, normalize_name


def make_param_sfo(title: str) -> bytes:
    key = b"TITLE\0"
    value = title.encode("utf-8") + b"\0"
    key_start = 36
    value_start = (key_start + len(key) + 3) & ~3
    header = b"\x00PSF" + struct.pack("<IIII", 0x101, key_start, value_start, 1)
    entry = struct.pack("<HHIII", 0, 0x0204, len(value), len(value), 0)
    return header + entry + key + b"\0" * (value_start - key_start - len(key)) + value


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

    def test_create_device_makes_roms_directory_and_registers_immediately(self):
        created = self.service.create_device("retroid-pocket-6", "hardlink")

        self.assertEqual(created["name"], "retroid-pocket-6")
        self.assertEqual(created["deployment_mode"], "hardlink")
        self.assertTrue((self.devices / "retroid-pocket-6" / "roms").is_dir())
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT name,path FROM devices WHERE id=?", (created["id"],)
            ).fetchone()
        self.assertEqual(dict(row), {"name": "retroid-pocket-6", "path": "retroid-pocket-6"})

    def test_create_device_rejects_unsafe_and_duplicate_names(self):
        for unsafe in ("../outside", "nested/device", "device name", ".hidden"):
            with self.subTest(unsafe=unsafe), self.assertRaises(LibraryError):
                self.service.create_device(unsafe)
        self.service.create_device("odin2")
        with self.assertRaises(LibraryError):
            self.service.create_device("odin2")

    def test_linked_device_rosters_propagate_selections_and_unlink_safely(self):
        self.write("gba/Alpha.gba", b"alpha")
        self.write("gba/Beta.gba", b"beta")
        self.service.scan()
        alpha = self.game_id("Alpha")
        beta = self.game_id("Beta")
        with self.db.write() as connection:
            connection.execute(
                "INSERT INTO users(username,username_normalized,display_name,password_hash,role) "
                "VALUES('roster-owner','roster-owner','Roster Owner','unused','member')"
            )
            owner_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        source = self.service.create_device("rg-rotate", owner_user_id=owner_id)
        target = self.service.create_device("rg-sp", owner_user_id=owner_id)
        clone = self.service.create_device("travel-sp", owner_user_id=owner_id)
        self.service.set_selection(source["id"], alpha, True)
        self.service.set_selection(target["id"], beta, True)

        linked = self.service.link_device_rosters(source["id"], [target["id"]])
        self.assertEqual(linked["devices"], 2)
        with self.db.connect() as connection:
            target_games = {
                row["game_id"] for row in connection.execute(
                    "SELECT game_id FROM device_selections WHERE device_id=?", (target["id"],)
                )
            }
        self.assertEqual(target_games, {alpha})

        self.service.set_selection(target["id"], beta, True)
        self.service.set_selection(source["id"], alpha, False)
        for device_id in (source["id"], target["id"]):
            with self.db.connect() as connection:
                games = {
                    row["game_id"] for row in connection.execute(
                        "SELECT game_id FROM device_selections WHERE device_id=?", (device_id,)
                    )
                }
            self.assertEqual(games, {beta})

        cloned = self.service.clone_device_roster(source["id"], clone["id"], True)
        self.assertEqual(cloned, {"games": 1, "linked": True})
        self.service.set_selection(clone["id"], alpha, True)
        self.service.unlink_device_roster(target["id"])
        self.service.set_selection(source["id"], beta, False)
        with self.db.connect() as connection:
            source_games = {
                row["game_id"] for row in connection.execute(
                    "SELECT game_id FROM device_selections WHERE device_id=?", (source["id"],)
                )
            }
            clone_games = {
                row["game_id"] for row in connection.execute(
                    "SELECT game_id FROM device_selections WHERE device_id=?", (clone["id"],)
                )
            }
            target_games = {
                row["game_id"] for row in connection.execute(
                    "SELECT game_id FROM device_selections WHERE device_id=?", (target["id"],)
                )
            }
        self.assertEqual(source_games, {alpha})
        self.assertEqual(clone_games, {alpha})
        self.assertEqual(target_games, {alpha, beta})

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

    def test_optical_disc_folders_claim_unreferenced_component_files(self):
        self.write("dreamcast/Rez (USA)/track01.bin", b"unique-dreamcast-data")
        self.write("dreamcast/Rez (USA)/track02.raw", b"shared-audio")
        self.write(
            "dreamcast/Rez (USA)/Rez (USA).gdi",
            "1\n1 0 4 2352 track01.bin 0\n",
        )
        self.write("psx/Colony Wars (USA)/track01.bin", b"unique-playstation-data")
        self.write("psx/Colony Wars (USA)/track02.bin", b"shared-audio")
        self.write(
            "psx/Colony Wars (USA)/Colony Wars (USA).cue",
            "FILE track01.bin BINARY\n  TRACK 01 MODE2/2352\n",
        )

        result = self.service.scan()

        self.assertEqual(result["games"], 2)
        with self.db.connect() as connection:
            games = connection.execute(
                "SELECT id,platform,display_name,bundle_hash FROM games ORDER BY platform"
            ).fetchall()
        self.assertEqual({game["platform"] for game in games}, {"dreamcast", "psx"})
        self.assertEqual(len({game["bundle_hash"] for game in games}), 2)
        for game in games:
            _, files = self.service.game_bundle(game["id"])
            self.assertEqual(len(files), 3)

    def test_rescan_merges_legacy_disc_components_and_preserves_device_state(self):
        self.write("dreamcast/Legacy Game/track01.bin", b"game-data")
        self.write("dreamcast/Legacy Game/track02.bin", b"audio-data")
        (self.devices / "handheld/roms").mkdir(parents=True)
        self.service.scan()
        with self.db.connect() as connection:
            device_id = connection.execute("SELECT id FROM devices").fetchone()["id"]
            legacy_ids = [row["id"] for row in connection.execute("SELECT id FROM games")]
        self.service.set_selections(device_id, legacy_ids, True)
        self.service.apply_device(device_id)
        self.write(
            "dreamcast/Legacy Game/Legacy Game.gdi",
            "2\n1 0 4 2352 track01.bin 0\n2 45000 0 2352 track02.bin 0\n",
        )

        result = self.service.scan()

        self.assertEqual(result["games"], 1)
        with self.db.connect() as connection:
            game = connection.execute("SELECT id,extension FROM games").fetchone()
            selections = connection.execute(
                "SELECT game_id FROM device_selections WHERE device_id=?", (device_id,)
            ).fetchall()
            deployments = connection.execute(
                "SELECT DISTINCT game_id FROM deployments WHERE device_id=?", (device_id,)
            ).fetchall()
        self.assertEqual(game["extension"], ".gdi")
        self.assertEqual([row["game_id"] for row in selections], [game["id"]])
        self.assertEqual([row["game_id"] for row in deployments], [game["id"]])
        with self.db.connect() as connection:
            detail = connection.execute(
                "SELECT detail FROM activity ORDER BY id DESC LIMIT 1"
            ).fetchone()["detail"]
        self.assertIn("merged 2 legacy bundle components", detail)

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
        self.assertEqual(len({row["bundle_hash"] for row in rows}), 2)
        self.assertTrue(all(row["bundle_hash"].startswith("metadata:") for row in rows))
        self.assertEqual({row["normalized_name"] for row in rows}, {"zone of the enders hd collection"})
        self.assertTrue(all(row["size"] == len(b"same-metadata") + len(b"same-content") for row in rows))
        game, files = self.service.game_bundle(self.game_id("Zone of the Enders HD Collection [BLES01756]"))
        self.assertEqual(game["extension"], ".ps3")
        self.assertEqual(len(files), 2)

    def test_wiiu_directory_trees_are_complete_folder_bundles(self):
        for title, unique in (("Mario Kart 8", b"kart"), ("Super Mario 3D World", b"world")):
            base = f"wiiu/{title}/code"
            self.write(f"{base}/main.rpx", unique)
            self.write(f"wiiu/{title}/content/common.pack", b"shared-content")

        result = self.service.scan()

        self.assertEqual(result["games"], 2)
        with self.db.connect() as connection:
            games = connection.execute(
                "SELECT id,display_name,bundle_hash FROM games ORDER BY display_name"
            ).fetchall()
        self.assertEqual(
            {game["display_name"] for game in games},
            {"Mario Kart 8", "Super Mario 3D World"},
        )
        self.assertEqual(len({game["bundle_hash"] for game in games}), 2)
        for game in games:
            _, files = self.service.game_bundle(game["id"])
            self.assertEqual(len(files), 2)

    def test_folder_bundles_are_metadata_indexed_without_content_hashing(self):
        self.write("ps3/Fast Folder.ps3/PS3_GAME/PARAM.SFO", b"metadata")
        self.write("ps3/Fast Folder.ps3/PS3_GAME/USRDIR/large.bin", b"large-content")

        with patch.object(self.service, "_hash_file", side_effect=AssertionError("must not hash")):
            result = self.service.scan()

        self.assertEqual(result["games"], 1)
        game, files = self.service.game_bundle(self.game_id("Fast Folder"))
        self.assertTrue(game["bundle_hash"].startswith("metadata:"))
        self.assertEqual({item["sha256"] for item in files}, {""})

    def test_new_large_single_file_is_indexed_without_content_hashing(self):
        large = self.write("ps2/Large Game.iso", b"large-content")
        service = LibraryService(replace(self.settings, hash_max_bytes=4), self.db)

        original_hash = service._hash_file

        def reject_large_hash(path, *args, **kwargs):
            if path == large:
                raise AssertionError("large file content must not be hashed")
            return original_hash(path, *args, **kwargs)

        with patch.object(service, "_hash_file", side_effect=reject_large_hash):
            result = service.scan()

        game, files = service.game_bundle(self.game_id("Large Game"))
        self.assertTrue(game["bundle_hash"].startswith("metadata:"))
        self.assertEqual(files[0]["sha256"], "")
        self.assertGreaterEqual(result["metadata_files"], 1)

    def test_valid_cached_hash_for_large_file_is_reused(self):
        large = self.write("ps2/Cached Large Game.iso", b"large-content")
        full_hash_service = LibraryService(replace(self.settings, hash_max_bytes=0), self.db)
        full_hash_service.scan()
        game_id = self.game_id("Cached Large Game")
        original_game, original_files = full_hash_service.game_bundle(game_id)
        self.assertFalse(original_game["bundle_hash"].startswith("metadata:"))
        self.assertTrue(original_files[0]["sha256"])

        limited_service = LibraryService(replace(self.settings, hash_max_bytes=4), self.db)
        with patch.object(Path, "open", side_effect=AssertionError("cached file must not be read")):
            limited_service.scan()

        game, files = limited_service.game_bundle(game_id)
        self.assertEqual(game["bundle_hash"], original_game["bundle_hash"])
        self.assertEqual(files[0]["sha256"], original_files[0]["sha256"])

    def test_changed_large_file_discards_stale_cached_hash(self):
        large = self.write("ps2/Changed Large Game.iso", b"old-content")
        full_hash_service = LibraryService(replace(self.settings, hash_max_bytes=0), self.db)
        full_hash_service.scan()
        large.write_bytes(b"new-content")
        stat = large.stat()
        os.utime(large, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))

        limited_service = LibraryService(replace(self.settings, hash_max_bytes=4), self.db)
        with patch.object(Path, "open", side_effect=AssertionError("changed large file must be deferred")):
            limited_service.scan()

        game, files = limited_service.game_bundle(self.game_id("Changed Large Game"))
        self.assertTrue(game["bundle_hash"].startswith("metadata:"))
        self.assertEqual(files[0]["sha256"], "")
        with self.db.connect() as connection:
            cached = connection.execute(
                "SELECT 1 FROM file_cache WHERE relpath='ps2/Changed Large Game.iso'"
            ).fetchone()
        self.assertIsNone(cached)

    def test_folder_bundle_tree_is_walked_only_once(self):
        self.write("ps3/Game [TEST00001].ps3/PS3_GAME/USRDIR/data.bin", b"payload")
        original_rglob = Path.rglob
        walked: list[Path] = []

        def tracking_rglob(path: Path, pattern: str):
            walked.append(path)
            return original_rglob(path, pattern)

        with patch.object(Path, "rglob", tracking_rglob):
            self.service.scan()

        self.assertEqual(walked, [(self.settings.library_root / "ps3").resolve()])

    def test_vita_title_id_is_one_cross_root_deployable_bundle(self):
        title_id = "PCSE00001"
        self.write(f"vita/app/{title_id}/sce_sys/param.sfo", make_param_sfo("Gravity Rush"))
        app_file = self.write(f"vita/app/{title_id}/game.bin", b"base")
        patch_file = self.write(f"vita/patch/{title_id}/game.bin", b"patch")
        dlc_file = self.write(f"vita/addcont/{title_id}/DLC0001/content.bin", b"dlc")
        license_file = self.write(f"vita/license/app/{title_id}/license.rif", b"license")
        self.write("vita/patch/PCSE99999/orphan.bin", b"orphan-patch")
        device_roms = self.devices / "odin" / "roms"
        device_roms.mkdir(parents=True)

        with patch.object(self.service, "_hash_file", side_effect=AssertionError("must not hash")):
            result = self.service.scan()

        self.assertEqual(result["games"], 1)
        game_id = self.game_id("Gravity Rush")
        game, files = self.service.game_bundle(game_id)
        self.assertEqual(game["primary_relpath"], f"vita/app/{title_id}")
        self.assertTrue(game["bundle_hash"].startswith("metadata:"))
        self.assertEqual(
            {item["relpath"] for item in files},
            {
                f"vita/app/{title_id}/sce_sys/param.sfo",
                f"vita/app/{title_id}/game.bin",
                f"vita/patch/{title_id}/game.bin",
                f"vita/addcont/{title_id}/DLC0001/content.bin",
                f"vita/license/app/{title_id}/license.rif",
            },
        )
        with self.db.connect() as connection:
            device_id = connection.execute("SELECT id FROM devices WHERE name='odin'").fetchone()["id"]
        self.service.set_device_deployment_mode(device_id, "hardlink")
        self.service.set_selection(device_id, game_id, True)
        applied = self.service.apply_device(device_id)
        self.assertEqual(applied["linked"], 5)
        for source in (app_file, patch_file, dlc_file, license_file):
            target = device_roms / source.relative_to(self.roms)
            self.assertTrue(source.samefile(target))

        with self.assertRaisesRegex(LibraryError, "spans multiple system directories"):
            self.service.preview_rename(game_id, "Different Name")

    def test_psvita_esde_alias_uses_vita_title_id_bundles(self):
        title_id = "PCSE00003"
        self.write(f"psvita/app/{title_id}/sce_sys/param.sfo", make_param_sfo("Vita Alias"))
        self.write(f"psvita/app/{title_id}/eboot.bin", b"base")
        self.write(f"psvita/patch/{title_id}/patch.bin", b"patch")

        with patch.object(self.service, "_hash_file", side_effect=AssertionError("must not hash")):
            result = self.service.scan()

        self.assertEqual(result["games"], 1)
        game, files = self.service.game_bundle(self.game_id("Vita Alias"))
        self.assertEqual(game["platform"], "psvita")
        self.assertEqual(game["primary_relpath"], f"psvita/app/{title_id}")
        self.assertTrue(game["bundle_hash"].startswith("metadata:"))
        self.assertEqual(
            {item["relpath"] for item in files},
            {
                f"psvita/app/{title_id}/sce_sys/param.sfo",
                f"psvita/app/{title_id}/eboot.bin",
                f"psvita/patch/{title_id}/patch.bin",
            },
        )

    def test_vita_scan_merges_legacy_internal_games_and_preserves_device_state(self):
        title_id = "PCSE00002"
        paths = [
            f"vita/app/{title_id}/eboot.bin",
            f"vita/patch/{title_id}/patch.bin",
        ]
        for path in paths:
            self.write(path, path.encode())
        (self.devices / "odin" / "roms").mkdir(parents=True)
        with self.db.write() as connection:
            connection.execute("INSERT INTO devices(name,path) VALUES('odin','odin')")
            device_id = connection.execute("SELECT id FROM devices WHERE name='odin'").fetchone()["id"]
            for index, path in enumerate(paths, start=1):
                connection.execute(
                    "INSERT INTO games(platform,primary_relpath,display_name,extension,bundle_hash,normalized_name) "
                    "VALUES('vita',?,?,'.bin',?,?)",
                    (path, Path(path).stem, f"legacy-{index}", Path(path).stem),
                )
                game_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
                connection.execute(
                    "INSERT INTO game_files(game_id,relpath,device_relpath,size,sha256,kind) "
                    "VALUES(?,?,?,?,?,'content')",
                    (game_id, path, path, len(path.encode()), f"sha-{index}"),
                )
                connection.execute(
                    "INSERT INTO device_selections(device_id,game_id) VALUES(?,?)",
                    (device_id, game_id),
                )
                connection.execute(
                    "INSERT INTO deployments(device_id,game_id,relpath) VALUES(?,?,?)",
                    (device_id, game_id, path),
                )

        result = self.service.scan()

        self.assertEqual(result["games"], 1)
        with self.db.connect() as connection:
            game = connection.execute("SELECT id,primary_relpath FROM games").fetchone()
            selected = connection.execute(
                "SELECT DISTINCT game_id FROM device_selections WHERE device_id=?", (device_id,)
            ).fetchall()
            deployed = connection.execute(
                "SELECT DISTINCT game_id FROM deployments WHERE device_id=?", (device_id,)
            ).fetchall()
        self.assertEqual(game["primary_relpath"], f"vita/app/{title_id}")
        self.assertEqual([row["game_id"] for row in selected], [game["id"]])
        self.assertEqual([row["game_id"] for row in deployed], [game["id"]])

    def test_cartridge_collection_folders_remain_individual_games(self):
        self.write("gba/gba-top100/First Game.gba", b"first")
        self.write("gba/gba-top100/Second Game.gba", b"second")

        result = self.service.scan()

        self.assertEqual(result["games"], 2)
        with self.db.connect() as connection:
            names = {row["display_name"] for row in connection.execute("SELECT display_name FROM games")}
        self.assertEqual(names, {"First Game", "Second Game"})

    def test_switch_indexes_base_games_but_excludes_updates_and_support_trees(self):
        self.write("switch/Astral Chain [01007300020FA000].xci", b"base")
        self.write("switch/Astral Chain Update [01007300020FA800].nsp", b"update")
        self.write("switch/Oddly Named Update [001000C001F82A000][196608].nsp", b"update")
        self.write("switch/Updates/Another Game [0100123412345800].nsp", b"update")
        self.write("switch/dlc/Astral Chain Pack [01007300020FA001].nsp", b"dlc")
        self.write("switch/cheats/Astral Chain/60fps.txt", b"cheat")

        result = self.service.scan()

        self.assertEqual(result["games"], 1)
        with self.db.connect() as connection:
            game = connection.execute(
                "SELECT display_name,extension FROM games"
            ).fetchone()
        self.assertEqual(game["display_name"], "Astral Chain [01007300020FA000]")
        self.assertEqual(game["extension"], ".xci")

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

    def test_trash_move_safely_falls_back_across_filesystems(self):
        source = self.write("gba/Cross Mount.gba", b"complete-rom-data")
        target = self.trash / "batch/gba/Cross Mount.gba"

        with patch.object(Path, "rename", side_effect=OSError(errno.EXDEV, "cross-device link")):
            self.service._atomic_move(source, target)

        self.assertFalse(source.exists())
        self.assertEqual(target.read_bytes(), b"complete-rom-data")
        self.assertEqual(list(target.parent.glob(".rommates-move-*")), [])

    def test_scan_reports_byte_and_file_progress(self):
        self.write("gba/One.gba", b"a" * (2 * 1024 * 1024))
        self.write("gba/Two.gba", b"two")
        updates: list[tuple[int, str]] = []

        self.service.scan(progress_callback=lambda percent, detail: updates.append((percent, detail)))

        self.assertEqual(updates[0], (0, "Discovering library files"))
        self.assertTrue(any("Scanning" in detail and "of 2 files" in detail for _, detail in updates))
        self.assertTrue(any(percent == 92 and "2 games" in detail for percent, detail in updates))
        self.assertEqual(updates[-1], (99, "Finalizing scan"))

    def test_scan_reports_physical_read_and_platform_telemetry(self):
        self.write("gba/Read.gba", b"read-me")
        self.write("ps3/Folder.ps3/PS3_GAME/USRDIR/content.bin", b"metadata-only")
        updates: list[tuple[object, ...]] = []

        def progress(*values):
            updates.append(values)

        progress.supports_telemetry = True
        self.service.scan(progress_callback=progress)

        telemetry = [values[3] for values in updates if len(values) == 4]
        self.assertTrue(telemetry)
        final = telemetry[-1]
        self.assertEqual(final["bytes_read"], len(b"read-me"))
        self.assertEqual(final["bytes_to_hash"], len(b"read-me"))
        self.assertEqual(final["hashed_files"], 1)
        self.assertEqual(final["metadata_files"], 1)
        self.assertEqual(final["platforms"]["gba"]["processed_hash_files"], 1)
        self.assertEqual(final["platforms"]["ps3"]["processed_metadata_files"], 1)

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
            if "Scanning 1 of 2 files" in detail:
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

    def test_device_apply_links_unselects_and_cleans_appledouble(self):
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
        self.assertEqual(applied["linked"], 1)
        self.assertEqual(applied["metadata_removed"], 1)
        self.assertTrue((device_roms / "gba/Metroid.gba").exists())
        self.service.set_selection(device_id, game_id, False)
        removed = self.service.apply_device(device_id)
        self.assertEqual(removed["removed"], 1)
        self.assertFalse((device_roms / "gba/Metroid.gba").exists())

    def test_hardlink_device_deploys_without_duplicate_storage(self):
        source = self.write("gba/Zero Copy.gba", b"linked-rom")
        device_roms = self.devices / "handheld" / "roms"
        device_roms.mkdir(parents=True)
        self.service.scan()
        game_id = self.game_id("Zero Copy")
        with self.db.connect() as connection:
            device_id = connection.execute("SELECT id FROM devices").fetchone()["id"]
        self.service.set_device_deployment_mode(device_id, "hardlink")
        self.service.set_selection(device_id, game_id, True)

        result = self.service.apply_device(device_id)

        target = device_roms / "gba/Zero Copy.gba"
        self.assertEqual(result["linked"], 1)
        self.assertEqual(result["copied"], 0)
        self.assertTrue(source.samefile(target))
        self.assertEqual(self.service.device_storage_summary(device_id)["hardlinked"], 1)

        self.service.set_selection(device_id, game_id, False)
        self.service.apply_device(device_id)
        self.assertFalse(target.exists())
        self.assertEqual(source.read_bytes(), b"linked-rom")

    def test_device_storage_summary_reflects_external_file_replacement(self):
        source = self.write("gba/Filesystem Truth.gba", b"linked-rom")
        device_roms = self.devices / "handheld" / "roms"
        device_roms.mkdir(parents=True)
        self.service.scan()
        game_id = self.game_id("Filesystem Truth")
        with self.db.connect() as connection:
            device_id = connection.execute("SELECT id FROM devices").fetchone()["id"]
        self.service.set_device_deployment_mode(device_id, "hardlink")
        self.service.set_selection(device_id, game_id, True)
        self.service.apply_device(device_id)
        target = device_roms / "gba/Filesystem Truth.gba"
        self.assertEqual(self.service.device_storage_summary(device_id)["hardlinked"], 1)

        target.unlink()
        shutil.copy2(source, target)

        summary = self.service.device_storage_summary(device_id)
        self.assertEqual(summary["hardlinked"], 0)
        self.assertEqual(summary["copied"], 1)
        self.assertEqual(summary["conversions"], 1)

    def test_hardlink_mode_atomically_converts_existing_copy(self):
        source = self.write("gba/Convert Me.gba", b"convert-rom")
        device_roms = self.devices / "handheld" / "roms"
        device_roms.mkdir(parents=True)
        self.service.scan()
        game_id = self.game_id("Convert Me")
        with self.db.connect() as connection:
            device_id = connection.execute("SELECT id FROM devices").fetchone()["id"]
        self.service.set_selection(device_id, game_id, True)
        target = device_roms / "gba/Convert Me.gba"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        self.service._record_deployment(device_id, game_id, "gba/Convert Me.gba")
        self.assertFalse(source.samefile(target))

        result = self.service.apply_device(device_id)

        self.assertEqual(result["converted"], 1)
        self.assertTrue(source.samefile(target))
        self.assertEqual(list(device_roms.rglob(f"*{LINK_SUFFIX}")), [])

    def test_hardlink_mode_falls_back_to_copy_on_cross_device_error(self):
        source = self.write("gba/Fallback.gba", b"fallback-rom")
        device_roms = self.devices / "handheld" / "roms"
        device_roms.mkdir(parents=True)
        self.service.scan()
        game_id = self.game_id("Fallback")
        with self.db.connect() as connection:
            device_id = connection.execute("SELECT id FROM devices").fetchone()["id"]
        self.service.set_device_deployment_mode(device_id, "hardlink")
        self.service.set_selection(device_id, game_id, True)

        with patch("app.library.os.link", side_effect=OSError(errno.EXDEV, "cross-device")):
            result = self.service.apply_device(device_id)

        target = device_roms / "gba/Fallback.gba"
        self.assertEqual(result["link_fallbacks"], 1)
        self.assertEqual(result["copied"], 1)
        self.assertEqual(target.read_bytes(), source.read_bytes())
        self.assertFalse(source.samefile(target))

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

    def test_apply_creates_missing_esde_system_folder_for_known_alias(self):
        self.write("Nintendo Game Boy/Tetris.gb", b"blocks")
        device_roms = self.devices / "handheld" / "roms"
        device_roms.mkdir(parents=True)
        self.service.scan()
        with self.db.connect() as connection:
            device_id = connection.execute("SELECT id FROM devices").fetchone()["id"]
            game_id = connection.execute("SELECT id FROM games").fetchone()["id"]
            device_relpath = connection.execute(
                "SELECT device_relpath FROM game_files WHERE game_id=?", (game_id,)
            ).fetchone()["device_relpath"]
        self.assertEqual(device_relpath, "gb/Tetris.gb")
        self.service.set_selection(device_id, game_id, True)

        result = self.service.apply_device(device_id)

        self.assertEqual(result["linked"], 1)
        self.assertTrue((device_roms / "gb/Tetris.gb").is_file())
        self.assertFalse((device_roms / "Nintendo Game Boy").exists())
        with self.db.connect() as connection:
            deployed = connection.execute(
                "SELECT relpath FROM deployments WHERE device_id=?", (device_id,)
            ).fetchone()["relpath"]
        self.assertEqual(deployed, "gb/Tetris.gb")

    def test_apply_preserves_unknown_custom_platform_folder(self):
        self.write("gba-top100/Metroid.gba", b"samus")
        device_roms = self.devices / "handheld" / "roms"
        device_roms.mkdir(parents=True)
        self.service.scan()
        with self.db.connect() as connection:
            device_id = connection.execute("SELECT id FROM devices").fetchone()["id"]
            game_id = connection.execute("SELECT id FROM games").fetchone()["id"]
        self.service.set_selection(device_id, game_id, True)

        self.service.apply_device(device_id)

        self.assertTrue((device_roms / "gba-top100/Metroid.gba").is_file())

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

        with patch("app.library.os.link", side_effect=OSError(errno.EXDEV, "cross-device")), patch("app.library.shutil.copy2", side_effect=blocking_copy):
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

        with patch("app.library.os.link", side_effect=OSError(errno.EXDEV, "cross-device")), patch("app.library.shutil.copy2", side_effect=failing_copy):
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

        with patch("app.library.os.link", side_effect=OSError(errno.EXDEV, "cross-device")):
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
