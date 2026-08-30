from __future__ import annotations

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
        self.service.create_snapshot()

        comparison = self.service.compare(original["snapshot_id"])
        restored = self.service.restore_snapshot(
            original["snapshot_id"], comparison["current_tree_hash"]
        )
        self.assertEqual((self.saves / "manifest.server").read_bytes(), b"manifest-one")
        self.assertEqual((self.saves / "saves/Game.srm").read_bytes(), b"save-one")
        self.assertFalse((self.saves / "states/Game.state").exists())
        self.assertNotEqual(restored["safety_snapshot_id"], original["snapshot_id"])
        safety = self.service.snapshot_detail(restored["safety_snapshot_id"])["snapshot"]
        self.assertEqual(safety["trigger"], "pre_restore")

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
