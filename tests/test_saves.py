from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.config import Settings
from app.db import Database
from app.library import LibraryError, normalize_name
from app.saves import SaveSnapshotService


class SaveSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.saves = root / "saves"
        self.snapshots = root / "snapshots"
        self.saves.mkdir()
        settings = Settings(
            library_root=root / "roms",
            devices_root=root / "devices",
            trash_root=root / "trash",
            database_path=root / "rommates.db",
            saves_root=self.saves,
            snapshots_root=self.snapshots,
            save_snapshot_quiet_seconds=0,
            save_retention_recent=20,
            save_retention_daily=0,
            save_retention_weekly=0,
            save_retention_monthly=0,
        )
        self.db = Database(settings.database_path)
        self.db.initialize()
        self.service = SaveSnapshotService(settings, self.db)
        self.service.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relpath: str, content: bytes) -> Path:
        path = self.saves / relpath
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def add_game(self, platform: str, name: str, relpath: str | None = None) -> int:
        relpath = relpath or f"{platform}/{name}.rom"
        with self.db.write() as connection:
            connection.execute(
                "INSERT INTO games(platform,primary_relpath,display_name,extension,size,bundle_hash,normalized_name,mtime_ns) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (platform, relpath, name, Path(relpath).suffix, 1, f"hash-{platform}-{name}-{relpath}", normalize_name(name), 1),
            )
            return connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]

    def test_save_matching_reports_exact_possible_ambiguous_and_orphan_groups(self):
        pokemon_id = self.add_game("gba", "Pokemon Emerald (USA)", "gba/Pokemon Emerald (USA).gba")
        self.add_game("nds", "Advance Wars - Days of Ruin (USA)", "nds/Advance Wars - Days of Ruin (USA).nds")
        self.add_game("gba", "Shared Game (USA)", "gba/Shared Game (USA).gba")
        self.add_game("gba", "Shared Game (Europe)", "gba/Shared Game (Europe).gba")
        self.write("saves/mGBA/Pokemon Emerald (USA).srm", b"save")
        self.write("states/mGBA/Pokemon Emerald (USA).state1", b"state")
        self.write("saves/melonDS/Advance Wars - Days of Ruin.sav", b"possible")
        self.write("saves/mGBA/Shared Game.srm", b"ambiguous")
        self.write("saves/mGBA/Unknown Adventure.srm", b"orphan")
        self.write("config/mGBA/mGBA.opt", b"ignored")
        self.write("manifest.server", b"ignored")

        impacts = self.service.save_impacts([pokemon_id])
        self.assertEqual(impacts[pokemon_id]["status"], "exact")
        self.assertEqual(impacts[pokemon_id]["save_files"], 1)
        self.assertEqual(impacts[pokemon_id]["state_files"], 1)

        report = self.service.unmatched_groups()
        self.assertEqual(report["summary"], {"groups": 4, "exact": 1, "possible": 1, "ambiguous": 1, "orphan": 1})
        self.assertEqual({item["status"] for item in report["items"]}, {"possible", "ambiguous", "orphan"})
        orphan = next(item for item in report["items"] if item["status"] == "orphan")
        self.assertEqual(orphan["content_name"], "Unknown Adventure")
        self.assertEqual(orphan["games"], [])

    def test_core_directory_narrows_same_filename_to_the_correct_platform(self):
        gba_id = self.add_game("gba", "Core Test", "gba/Core Test.gba")
        snes_id = self.add_game("snes", "Core Test", "snes/Core Test.sfc")
        self.write("saves/mGBA/Core Test.srm", b"save")

        impacts = self.service.save_impacts()
        self.assertEqual(impacts[gba_id]["status"], "exact")
        self.assertEqual(impacts[snes_id]["status"], "none")

    def test_shared_vault_classifies_emulators_and_ignores_transport_metadata(self):
        self.write("retroarch/mGBA/Pokemon Emerald (USA).srm", b"retroarch-save")
        self.write("melonds/saves/Advance Wars.sav", b"standalone-save")
        self.write("melonds/states/Advance Wars.state1", b"standalone-state")
        self.write("dolphin/saves/USA/Card A/GALE01.gci", b"game-id-save")
        self.write("ryujinx/user/save/profile/title/main", b"title-id-save")
        self.write("._Finder metadata", b"ignored")
        self.write(".stfolder/marker", b"ignored")

        current = self.service.current_files(limit=100)
        self.assertEqual(current["total"], 5)
        by_path = {item["relpath"]: item for item in current["items"]}
        self.assertEqual(by_path["retroarch/mGBA/Pokemon Emerald (USA).srm"]["emulator"], "RetroArch")
        self.assertEqual(by_path["retroarch/mGBA/Pokemon Emerald (USA).srm"]["core"], "mGBA")
        self.assertEqual(by_path["melonds/states/Advance Wars.state1"]["kind"], "state")
        self.assertEqual(by_path["dolphin/saves/USA/Card A/GALE01.gci"]["match_strategy"], "game_id")

        summary = self.service.source_summary()
        self.assertEqual(summary["files"], 5)
        self.assertEqual(summary["save_files"], 3)
        self.assertEqual(summary["state_files"], 1)
        self.assertEqual({item["emulator"] for item in summary["emulators"]}, {
            "RetroArch", "melonds", "dolphin", "ryujinx",
        })

    def test_current_files_sort_before_paginating_and_default_to_newest(self):
        oldest = self.write("melonds/saves/Alpha.sav", b"1")
        middle = self.write("retroarch/mGBA/Beta.state1", b"22")
        newest = self.write("dolphin/config/Charlie.bin", b"333")
        os.utime(oldest, ns=(1_000_000_000, 1_000_000_000))
        os.utime(middle, ns=(2_000_000_000, 2_000_000_000))
        os.utime(newest, ns=(3_000_000_000, 3_000_000_000))

        self.assertEqual(
            [item["relpath"] for item in self.service.current_files(limit=2)["items"]],
            ["dolphin/config/Charlie.bin", "retroarch/mGBA/Beta.state1"],
        )
        self.assertEqual(
            [item["relpath"] for item in self.service.current_files(sort="path_asc")["items"]],
            ["dolphin/config/Charlie.bin", "melonds/saves/Alpha.sav", "retroarch/mGBA/Beta.state1"],
        )
        self.assertEqual(
            [item["size"] for item in self.service.current_files(sort="size_desc")["items"]],
            [3, 2, 1],
        )
        self.assertEqual(
            [item["kind"] for item in self.service.current_files(sort="type_asc")["items"]],
            ["other", "save", "state"],
        )
        self.assertEqual(
            [item["emulator"] for item in self.service.current_files(sort="emulator_asc")["items"]],
            ["dolphin", "melonds", "RetroArch"],
        )
        self.assertEqual(
            [item["relpath"] for item in self.service.current_files(sort="modified_asc")["items"]],
            ["melonds/saves/Alpha.sav", "retroarch/mGBA/Beta.state1", "dolphin/config/Charlie.bin"],
        )

    def test_shared_vault_matches_only_filename_based_save_layouts(self):
        gba_id = self.add_game("gba", "Pokemon Emerald (USA)", "gba/Pokemon Emerald (USA).gba")
        nds_id = self.add_game("nds", "Advance Wars", "nds/Advance Wars.nds")
        self.add_game("gc", "Super Smash Bros. Melee", "gc/Super Smash Bros. Melee.iso")
        self.write("retroarch/mGBA/Pokemon Emerald (USA).srm", b"retroarch-save")
        self.write("melonds/saves/Advance Wars.sav", b"standalone-save")
        self.write("dolphin/saves/USA/Card A/GALE01.gci", b"game-id-save")

        impacts = self.service.save_impacts()
        self.assertEqual(impacts[gba_id]["status"], "exact")
        self.assertEqual(impacts[nds_id]["status"], "exact")
        summary = self.service.match_summary()
        self.assertEqual(summary["groups"], 2)
        self.assertEqual(summary["orphan"], 0)

    def test_snapshot_excludes_syncthing_and_finder_metadata(self):
        self.write("retroarch/mGBA/Game.srm", b"save")
        self.write(".DS_Store", b"finder")
        self.write("retroarch/._Game.srm", b"apple-double")
        self.write(".stignore", b"ignore-rules")

        snapshot = self.service.create_snapshot()
        detail = self.service.snapshot_detail(snapshot["snapshot_id"])
        self.assertEqual(detail["total"], 1)
        self.assertEqual(detail["files"][0]["relpath"], "retroarch/mGBA/Game.srm")

    def test_snapshot_from_previous_source_is_download_only(self):
        self.write("retroarch/mGBA/Game.srm", b"save")
        snapshot = self.service.create_snapshot()
        with self.db.write() as connection:
            connection.execute(
                "UPDATE save_snapshots SET source_root='' WHERE id=?", (snapshot["snapshot_id"],)
            )

        comparison = self.service.compare(snapshot["snapshot_id"])
        self.assertFalse(comparison["compatible"])
        with self.assertRaisesRegex(LibraryError, "previous save source"):
            self.service.restore_snapshot(snapshot["snapshot_id"], "")

    def test_syncthing_conflicts_are_detected_and_attributed(self):
        current = self.write("retroarch/mGBA/Pokemon Pinball.srm", b"ryan-progress")
        conflict = self.write(
            "retroarch/mGBA/Pokemon Pinball.sync-conflict-20260829-183000-ABC1234.srm",
            b"brother-progress",
        )

        report = self.service.conflicts(device_names={"ABC1234-LONG-ID": "Brother's Retroid"})
        self.assertEqual(report["total"], 1)
        item = report["items"][0]
        self.assertEqual(item["canonical_relpath"], "retroarch/mGBA/Pokemon Pinball.srm")
        self.assertEqual(item["device_name"], "Brother's Retroid")
        self.assertFalse(item["identical"])
        self.assertEqual(current.read_bytes(), b"ryan-progress")
        self.assertEqual(conflict.read_bytes(), b"brother-progress")

    @staticmethod
    def _gen3_save(save_index: int) -> bytes:
        """A minimal Pokémon Gen 3 save: two 14-sector slots with valid footers."""
        data = bytearray(b"\xff" * 131072)
        for slot in (0, 1):
            index = save_index if slot == 0 else max(save_index - 1, 0)
            for section in range(14):
                offset = slot * 57344 + section * 4096
                data[offset + 0xFF4:offset + 0xFF6] = section.to_bytes(2, "little")
                data[offset + 0xFF8:offset + 0xFFC] = (0x08012025).to_bytes(4, "little")
                data[offset + 0xFFC:offset + 0x1000] = index.to_bytes(4, "little")
        return bytes(data)

    def test_snapshots_record_who_wrote_each_file_and_carry_it_forward(self):
        calls = []

        def lookup(entries):
            calls.append(sorted(relpath for relpath, _ in entries))
            return {relpath: {"retroarch/mGBA/A.srm": "RETROID", "retroarch/mGBA/B.srm": "ROTATE"}.get(relpath, "") for relpath, _ in entries}

        self.service.origin_lookup = lookup
        self.write("retroarch/mGBA/A.srm", b"a1")
        self.write("retroarch/mGBA/B.srm", b"b1")
        first = self.service.create_snapshot()["snapshot_id"]
        self.write("retroarch/mGBA/A.srm", b"a2")
        second = self.service.create_snapshot()["snapshot_id"]

        with self.db.connect() as connection:
            rows = {
                (row["snapshot_id"], row["relpath"]): row["origin_device"]
                for row in connection.execute("SELECT snapshot_id,relpath,origin_device FROM save_snapshot_files")
            }
        self.assertEqual(rows[(first, "retroarch/mGBA/A.srm")], "RETROID")
        self.assertEqual(rows[(first, "retroarch/mGBA/B.srm")], "ROTATE")
        self.assertEqual(rows[(second, "retroarch/mGBA/A.srm")], "RETROID")
        self.assertEqual(rows[(second, "retroarch/mGBA/B.srm")], "ROTATE")
        # Only the changed file is asked of Syncthing the second time.
        self.assertEqual(calls, [["retroarch/mGBA/A.srm", "retroarch/mGBA/B.srm"], ["retroarch/mGBA/A.srm"]])

    def test_snapshot_survives_a_failing_origin_lookup(self):
        def lookup(entries):
            raise OSError("syncthing down")

        self.service.origin_lookup = lookup
        self.write("retroarch/mGBA/A.srm", b"a1")
        result = self.service.create_snapshot()
        self.assertFalse(result["unchanged"])
        with self.db.connect() as connection:
            row = connection.execute("SELECT origin_device FROM save_snapshot_files").fetchone()
        self.assertEqual(row["origin_device"], "")

    def test_history_traces_versions_forks_and_resolutions(self):
        import hashlib

        origins = {}
        self.service.origin_lookup = lambda entries: {relpath: origins.get(relpath, "") for relpath, _ in entries}
        names = {"RETROID-LONG-ID": "Retroid Pocket 5", "REWXJAI-LONG-ID": "rg_rotate"}

        old = self._gen3_save(13)
        new = self._gen3_save(55)
        origins["retroarch/mGBA/Emerald.srm"] = "RETROID"
        self.write("retroarch/mGBA/Emerald.srm", old)
        self.service.create_snapshot(trigger="automatic")
        # rg_rotate's stale write won the tiebreak: the real progress is demoted to a copy.
        copy_relpath = "retroarch/mGBA/Emerald.sync-conflict-20261016-113000-REWXJAI.srm"
        origins[copy_relpath] = "RETROID"
        self.write(copy_relpath, new)
        self.service.create_snapshot(trigger="automatic")

        history = self.service.history(copy_relpath, names)
        self.assertEqual(history["relpath"], "retroarch/mGBA/Emerald.srm")
        self.assertEqual(history["content_name"], "Emerald")
        self.assertEqual([event["type"] for event in history["events"]], ["live", "fork"])
        live, fork = history["events"]
        self.assertEqual(live["origin_name"], "Retroid Pocket 5")
        self.assertEqual(live["save_counter"], 13)
        self.assertEqual(fork["winner_name"], "rg_rotate")
        self.assertEqual(fork["conflict_at"], "2026-10-16 11:30:00")
        self.assertEqual(fork["save_counter"], 55)
        self.assertEqual(fork["live_sha256"], hashlib.sha256(old).hexdigest())
        self.assertEqual(history["unresolved_conflicts"], [copy_relpath])
        self.assertEqual(history["current"]["sha256"], hashlib.sha256(old).hexdigest())
        self.assertTrue(history["origins_recorded"])

        self.service.resolve_conflict(
            copy_relpath, "conflict",
            hashlib.sha256(old).hexdigest(), hashlib.sha256(new).hexdigest(),
            "REWXJAI", "rg_rotate",
        )
        history = self.service.history("retroarch/mGBA/Emerald.srm", names)
        self.assertEqual([event["type"] for event in history["events"]], ["live", "fork", "resolved"])
        resolved = history["events"][-1]
        self.assertEqual(resolved["decision"], "conflict")
        self.assertEqual(resolved["sha256"], hashlib.sha256(new).hexdigest())
        self.assertEqual(history["current"]["sha256"], hashlib.sha256(new).hexdigest())
        self.assertEqual(history["current"]["save_counter"], 55)
        self.assertEqual(history["unresolved_conflicts"], [])
        by_hash = {item["sha256"]: item for item in history["versions"]}
        self.assertEqual(by_hash[hashlib.sha256(new).hexdigest()]["seen_as"], ["conflict"])
        self.assertEqual(by_hash[hashlib.sha256(old).hexdigest()]["seen_as"], ["live"])
        self.assertEqual(len(history["versions"]), 2)

    def test_history_rejects_paths_outside_the_save_folder(self):
        with self.assertRaises(LibraryError):
            self.service.history("../etc/passwd")
        with self.assertRaises(LibraryError):
            self.service.history("/retroarch/x.srm")

    def test_history_of_an_unknown_save_is_empty_but_well_formed(self):
        history = self.service.history("retroarch/mGBA/Nothing.srm")
        self.assertEqual(history["versions"], [])
        self.assertEqual(history["events"], [])
        self.assertIsNone(history["current"])
        self.assertFalse(history["origins_recorded"])

    def test_conflict_shows_save_counters_history_and_a_progress_hint(self):
        # The live save is an old one (13 saves) that was snapshotted while live;
        # the preserved copy holds the real progress (55 saves) and was never live.
        self.write("retroarch/mGBA/Emerald.srm", self._gen3_save(13))
        self.service.create_snapshot(trigger="automatic")
        self.write(
            "retroarch/mGBA/Emerald.sync-conflict-20261010-224753-REWXJAI.srm",
            self._gen3_save(55),
        )

        item = self.service.conflicts(device_names={"REWXJAI-LONG-ID": "Retroid Pocket 5"})["items"][0]

        self.assertEqual(item["canonical_save_counter"], 13)
        self.assertEqual(item["conflict_save_counter"], 55)
        self.assertEqual(item["canonical_history"]["count"], 1)
        self.assertIsNone(item["conflict_history"])
        self.assertIn("more in-game saves (55 vs 13)", item["hint"])
        self.assertIn("older one that came back", item["hint"])
        # Attribution still names the device whose write produced the conflict.
        self.assertEqual(item["device_name"], "Retroid Pocket 5")

    def test_conflict_hint_falls_back_to_snapshot_timing_for_other_games(self):
        # Not a Gen 3 save, so no counters: the only signal is that the current
        # content was last live in a snapshot before the preserved copy was written.
        current = self.write("retroarch/Snes9x/Chrono.srm", b"old-progress")
        self.service.create_snapshot(trigger="automatic")
        conflict = self.write(
            "retroarch/Snes9x/Chrono.sync-conflict-20261010-224753-REWXJAI.srm", b"new-progress"
        )
        import os, time
        later = time.time() + 86400 * 30
        os.utime(conflict, (later, later))

        item = self.service.conflicts()["items"][0]

        self.assertIsNone(item["canonical_save_counter"])
        self.assertIsNone(item["conflict_save_counter"])
        self.assertEqual(item["canonical_history"]["count"], 1)
        self.assertIn("older save that came back", item["hint"])

    def test_conflict_with_no_signal_has_no_hint(self):
        self.write("retroarch/Snes9x/Chrono.srm", b"a")
        self.write("retroarch/Snes9x/Chrono.sync-conflict-20261010-224753-REWXJAI.srm", b"b")
        item = self.service.conflicts()["items"][0]
        self.assertEqual(item["hint"], "")
        self.assertIsNone(item["canonical_history"])

    def test_restoring_a_conflict_stamps_the_live_file_with_the_current_time(self):
        # Syncthing settles concurrent changes by newest modification time. A
        # restore that keeps the copy's old stamp loses to any stale save a
        # handheld writes later, which is exactly how a restore gets undone.
        import os, time
        current = self.write("retroarch/mGBA/Emerald.srm", self._gen3_save(13))
        conflict = self.write(
            "retroarch/mGBA/Emerald.sync-conflict-20261016-113000-F6G43OM.srm", self._gen3_save(55)
        )
        old = time.time() - 30 * 86400
        os.utime(conflict, (old, old))
        item = self.service.conflicts()["items"][0]
        started = time.time()

        self.service.resolve_conflict(
            item["conflict_relpath"], "conflict", item["canonical_sha256"], item["conflict_sha256"],
        )

        self.assertEqual(self.service._gen3_save_counter(current), 55)
        self.assertGreaterEqual(current.stat().st_mtime, started - 1)
        self.assertGreater(current.stat().st_mtime, old + 86400)

    def test_a_conflict_copy_that_comes_back_after_resolution_is_flagged(self):
        # Resolving deletes the copy, but a handheld that still holds it can
        # re-announce it on reconnect and a modification beats a deletion.
        self.write("retroarch/mGBA/Emerald.srm", self._gen3_save(55))
        self.write("retroarch/mGBA/Emerald.sync-conflict-20261009-224819-REWXJAI.srm", self._gen3_save(40))
        item = self.service.conflicts()["items"][0]
        self.service.resolve_conflict(
            item["conflict_relpath"], "current", item["canonical_sha256"], item["conflict_sha256"],
        )
        self.assertEqual(self.service.conflicts()["total"], 0)

        self.write("retroarch/mGBA/Emerald.sync-conflict-20261009-224819-REWXJAI.srm", self._gen3_save(40))

        again = self.service.conflicts()["items"][0]
        self.assertEqual(again["previously_resolved_decision"], "current")
        self.assertTrue(again["previously_resolved_at"])
        self.assertIn("already resolved", again["hint"])
        self.assertIn("kept the current version", again["hint"])
        self.assertIn("sent it back", again["hint"])

    def test_conflict_resolution_snapshots_both_versions_before_using_conflict(self):
        current = self.write("retroarch/mGBA/Pokemon Pinball.srm", b"ryan-progress")
        conflict = self.write(
            "retroarch/mGBA/Pokemon Pinball.sync-conflict-20260829-183000-ABC1234.srm",
            b"brother-progress",
        )
        item = self.service.conflicts()["items"][0]

        result = self.service.resolve_conflict(
            item["conflict_relpath"], "conflict",
            item["canonical_sha256"], item["conflict_sha256"],
            item["device_id"], "Brother's Retroid",
        )

        self.assertEqual(current.read_bytes(), b"brother-progress")
        self.assertFalse(conflict.exists())
        snapshot = self.service.snapshot_detail(result["safety_snapshot_id"])
        self.assertEqual(snapshot["snapshot"]["trigger"], "pre_conflict_resolution")
        self.assertEqual(snapshot["total"], 2)
        with self.db.connect() as connection:
            history = connection.execute("SELECT * FROM save_conflict_resolutions").fetchone()
        self.assertEqual(history["decision"], "conflict")

    def test_conflict_resolution_reuses_identical_latest_safety_snapshot(self):
        self.write("retroarch/mGBA/Pokemon Pinball.srm", b"ryan-progress")
        self.write(
            "retroarch/mGBA/Pokemon Pinball.sync-conflict-20260829-183000-ABC1234.srm",
            b"brother-progress",
        )
        baseline = self.service.create_snapshot(trigger="automatic")
        item = self.service.conflicts()["items"][0]

        result = self.service.resolve_conflict(
            item["conflict_relpath"], "current",
            item["canonical_sha256"], item["conflict_sha256"],
        )

        self.assertEqual(result["safety_snapshot_id"], baseline["snapshot_id"])
        self.assertEqual(self.service.list_snapshots()["total"], 1)
        with self.db.connect() as connection:
            history = connection.execute(
                "SELECT safety_snapshot_id FROM save_conflict_resolutions"
            ).fetchone()
        self.assertEqual(history["safety_snapshot_id"], baseline["snapshot_id"])

    def test_conflict_resolution_rejects_stale_review(self):
        self.write("retroarch/mGBA/Game.srm", b"current")
        conflict = self.write(
            "retroarch/mGBA/Game.sync-conflict-20260829-183000-ABC1234.srm", b"conflict"
        )
        item = self.service.conflicts()["items"][0]
        conflict.write_bytes(b"changed")
        with self.assertRaisesRegex(LibraryError, "changed after review"):
            self.service.resolve_conflict(
                item["conflict_relpath"], "current",
                item["canonical_sha256"], item["conflict_sha256"],
            )

    def test_retention_preserves_snapshots_referenced_by_conflict_history(self):
        current = self.write("retroarch/mGBA/Game.srm", b"current")
        self.write(
            "retroarch/mGBA/Game.sync-conflict-20260829-183000-ABC1234.srm", b"conflict"
        )
        item = self.service.conflicts()["items"][0]
        result = self.service.resolve_conflict(
            item["conflict_relpath"], "current",
            item["canonical_sha256"], item["conflict_sha256"],
        )
        safety_snapshot_id = result["safety_snapshot_id"]

        self.service.update_settings({
            "retention_recent": 1,
            "retention_daily": 0,
            "retention_weekly": 0,
            "retention_monthly": 0,
        })
        current.write_bytes(b"newer-1")
        self.service.create_snapshot()
        current.write_bytes(b"newer-2")
        self.service.create_snapshot()

        with self.db.connect() as connection:
            safety_snapshot = connection.execute(
                "SELECT id FROM save_snapshots WHERE id=?", (safety_snapshot_id,)
            ).fetchone()
            resolution = connection.execute(
                "SELECT safety_snapshot_id FROM save_conflict_resolutions"
            ).fetchone()
        self.assertIsNotNone(safety_snapshot)
        self.assertEqual(resolution["safety_snapshot_id"], safety_snapshot_id)

    def test_orphan_delete_creates_safety_snapshot_and_removes_only_the_group(self):
        orphan = self.write("saves/mGBA/Old Game.srm", b"old-save")
        state = self.write("states/mGBA/Old Game.state", b"old-state")
        unrelated = self.write("saves/mGBA/Keep Me.srm", b"keep")
        group = next(
            item for item in self.service.unmatched_groups()["items"]
            if item["content_name"] == "Old Game"
        )

        result = self.service.delete_orphan_group(group["key"])

        self.assertFalse(orphan.exists())
        self.assertFalse(state.exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual(result["files"], 2)
        snapshot = self.service.snapshot_detail(result["safety_snapshot_id"])
        self.assertEqual(snapshot["snapshot"]["trigger"], "pre_save_delete")
        self.assertEqual(snapshot["total"], 3)

    def test_orphan_delete_refuses_a_group_that_matches_a_rom(self):
        self.add_game("gba", "Protected Game", "gba/Protected Game.gba")
        self.write("saves/mGBA/Protected Game.srm", b"save")
        groups, _ = self.service._matched_save_groups()
        group = next(item for item in groups if item["content_name"] == "Protected Game")

        with self.assertRaisesRegex(LibraryError, "Only save groups with no ROM match"):
            self.service.delete_orphan_group(group["key"])

        self.assertTrue((self.saves / "saves/mGBA/Protected Game.srm").exists())

    def test_snapshots_deduplicate_and_report_changes(self):
        self.write("saves/Pokemon.srm", b"first")
        first = self.service.create_snapshot()
        self.assertFalse(first["unchanged"])
        self.assertEqual(first["added"], 1)
        self.assertEqual(first["new_bytes"], 5)

        unchanged = self.service.create_snapshot()
        self.assertTrue(unchanged["unchanged"])
        self.assertEqual(self.service.list_snapshots()["total"], 1)

        self.write("saves/Pokemon.srm", b"second")
        self.write("states/Pokemon.state", b"state")
        second = self.service.create_snapshot(note="After a boss")
        self.assertEqual(second["changed"], 1)
        self.assertEqual(second["added"], 1)
        self.assertEqual(self.service.list_snapshots()["total"], 2)
        detail = self.service.snapshot_detail(second["snapshot_id"])
        self.assertEqual(detail["snapshot"]["note"], "After a boss")
        self.assertEqual(detail["total"], 2)

    def test_restore_creates_safety_snapshot_and_restores_complete_tree(self):
        self.write("manifest.server", b"manifest-one")
        self.write("saves/Game.srm", b"save-one")
        original = self.service.create_snapshot()
        self.write("manifest.server", b"manifest-two")
        self.write("saves/Game.srm", b"save-two")
        self.write("states/Game.state", b"new-state")
        current = self.service.create_snapshot()

        comparison = self.service.compare(original["snapshot_id"])
        restored = self.service.restore_snapshot(
            original["snapshot_id"], comparison["current_tree_hash"]
        )
        self.assertEqual((self.saves / "manifest.server").read_bytes(), b"manifest-one")
        self.assertEqual((self.saves / "saves/Game.srm").read_bytes(), b"save-one")
        self.assertFalse((self.saves / "states/Game.state").exists())
        self.assertNotEqual(restored["safety_snapshot_id"], original["snapshot_id"])
        self.assertEqual(restored["safety_snapshot_id"], current["snapshot_id"])
        self.assertEqual(self.service.list_snapshots()["total"], 2)

    def test_restore_aborts_when_preview_is_stale(self):
        self.write("saves/Game.srm", b"one")
        snapshot = self.service.create_snapshot()
        preview = self.service.compare(snapshot["snapshot_id"])
        self.write("saves/Game.srm", b"changed-after-preview")
        with self.assertRaisesRegex(LibraryError, "changed after the restore preview"):
            self.service.restore_snapshot(snapshot["snapshot_id"], preview["current_tree_hash"])
        self.assertEqual((self.saves / "saves/Game.srm").read_bytes(), b"changed-after-preview")

    def test_restore_checks_live_filesystem_space_before_mutation(self):
        self.write("save.srm", b"old-version")
        snapshot = self.service.create_snapshot()
        self.write("save.srm", b"current-version")
        preview = self.service.compare(snapshot["snapshot_id"])
        with patch("app.saves.shutil.disk_usage", return_value=SimpleNamespace(free=0)):
            with self.assertRaisesRegex(LibraryError, "temporary bytes"):
                self.service.restore_snapshot(snapshot["snapshot_id"], preview["current_tree_hash"])
        self.assertEqual((self.saves / "save.srm").read_bytes(), b"current-version")

    def test_retention_preserves_pinned_snapshot(self):
        self.service.update_settings(
            {
                "retention_recent": 1,
                "retention_daily": 0,
                "retention_weekly": 0,
                "retention_monthly": 0,
            }
        )
        self.write("save.srm", b"one")
        first = self.service.create_snapshot()
        self.service.pin(first["snapshot_id"], True)
        self.write("save.srm", b"two")
        second = self.service.create_snapshot()
        self.write("save.srm", b"three")
        third = self.service.create_snapshot()
        ids = {item["id"] for item in self.service.list_snapshots()["items"]}
        self.assertEqual(ids, {first["snapshot_id"], third["snapshot_id"]})
        self.assertNotIn(second["snapshot_id"], ids)

    def test_schedule_can_be_disabled_in_ui_settings(self):
        self.assertTrue(self.service.due_for_automatic_snapshot())
        self.write("save.srm", b"one")
        self.service.create_snapshot()
        self.assertFalse(self.service.due_for_automatic_snapshot())
        updated = self.service.update_settings(
            {
                "enabled": False,
                "interval_minutes": 60,
                "retention_recent": 24,
                "retention_daily": 30,
                "retention_weekly": 12,
                "retention_monthly": 12,
            }
        )
        self.assertFalse(updated["enabled"])
        self.assertFalse(self.service.due_for_automatic_snapshot())

    def test_cancelled_restore_rolls_back_live_files(self):
        self.write("save.srm", b"one")
        self.write("other.srm", b"other-one")
        snapshot = self.service.create_snapshot()
        self.write("save.srm", b"two")
        self.write("other.srm", b"other-two")
        preview = self.service.compare(snapshot["snapshot_id"])
        phase = 0
        apply_checks = 0

        def report(progress, _detail):
            nonlocal phase
            phase = progress

        def cancel_during_restore():
            nonlocal apply_checks
            if phase >= 66:
                apply_checks += 1
            # Allow one restored file to land, then force the transactional rollback.
            if apply_checks >= 2:
                from app.library import JobCancelled
                raise JobCancelled("Stopped by user")

        with self.assertRaisesRegex(Exception, "Stopped by user"):
            self.service.restore_snapshot(
                snapshot["snapshot_id"],
                preview["current_tree_hash"],
                progress_callback=report,
                cancel_check=cancel_during_restore,
            )
        self.assertEqual((self.saves / "save.srm").read_bytes(), b"two")
        self.assertEqual((self.saves / "other.srm").read_bytes(), b"other-two")

    def test_pre_restore_retention_cannot_prune_target_snapshot(self):
        self.write("save.srm", b"target")
        target = self.service.create_snapshot()
        self.write("save.srm", b"current")
        self.service.create_snapshot()
        with self.db.write() as connection:
            connection.execute(
                "UPDATE save_settings SET retention_recent=1,retention_daily=0,"
                "retention_weekly=0,retention_monthly=0 WHERE id=1"
            )
        preview = self.service.compare(target["snapshot_id"])
        self.service.restore_snapshot(target["snapshot_id"], preview["current_tree_hash"])
        self.assertEqual((self.saves / "save.srm").read_bytes(), b"target")


if __name__ == "__main__":
    unittest.main()


class DraSticMatchingTests(unittest.TestCase):
    """DraStic ships on most Android handhelds and keeps the ROM stem."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.settings = Settings(
            library_root=root / "roms",
            devices_root=root / "devices",
            trash_root=root / "trash",
            database_path=root / "db.sqlite",
            saves_root=root / "saves",
            snapshots_root=root / "snapshots",
            scan_on_start=False,
        )
        self.db = Database(self.settings.database_path)
        self.db.initialize()
        self.service = SaveSnapshotService(self.settings, self.db)

    def test_drastic_saves_are_matchable_by_filename(self):
        for relpath in (
            "drastic/Pokemon HeartGold.dsv",
            "drastic/backup/Pokemon HeartGold.dsv",
        ):
            with self.subTest(relpath=relpath):
                record = self.service._classify(relpath)
                self.assertEqual(record["kind"], "save")
                self.assertEqual(record["match_strategy"], "filename")
                self.assertTrue(record["matchable"])
                self.assertEqual(record["content_name"], "Pokemon HeartGold")
                self.assertIn("nds", record["platforms"])

    def test_resolve_relpath_rejects_paths_outside_the_source(self):
        (self.settings.saves_root / "drastic").mkdir(parents=True)
        real = self.settings.saves_root / "drastic" / "Game.dsv"
        real.write_bytes(b"save")
        self.assertEqual(self.service.resolve_relpath("drastic/Game.dsv"), real.resolve())
        self.assertIsNone(self.service.resolve_relpath("../escape.dsv"))
        self.assertIsNone(self.service.resolve_relpath("drastic/missing.dsv"))
        self.assertIsNone(self.service.resolve_relpath("drastic"))
