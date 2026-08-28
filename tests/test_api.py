from __future__ import annotations

import importlib
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


class ApiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        root = Path(cls.temp.name)
        cls.root = root
        (root / "roms/gba").mkdir(parents=True)
        (root / "devices/handheld/roms").mkdir(parents=True)
        (root / "roms/gba/Test Game.gba").write_bytes(b"test-rom")
        cls.token = "integration-test-token-123456"
        cls.environment = patch.dict(
            os.environ,
            {
                "ROMMATES_LIBRARY_ROOT": str(root / "roms"),
                "ROMMATES_DEVICES_ROOT": str(root / "devices"),
                "ROMMATES_TRASH_ROOT": str(root / "trash"),
                "ROMMATES_DATABASE_PATH": str(root / "data/rommates.db"),
                "ROMMATES_SAVES_ROOT": str(root / "saves"),
                "ROMMATES_SNAPSHOTS_ROOT": str(root / "snapshots"),
                "ROMMATES_SAVE_SNAPSHOT_QUIET_SECONDS": "0",
                "ROMMATES_SCAN_ON_START": "false",
                "ROMMATES_REQUIRE_EXISTING_ROOTS": "true",
                "ROMMATES_ACCESS_TOKEN": cls.token,
            },
        )
        cls.environment.start()
        (root / "saves").mkdir()
        # Reload rather than import: app.main reads settings at module scope, so a
        # module already imported under different environment variables would be reused.
        cls.main = importlib.reload(importlib.import_module("app.main"))
        cls.client_context = TestClient(cls.main.app)
        cls.client = cls.client_context.__enter__()
        cls.headers = {"Authorization": f"Bearer {cls.token}"}

    @classmethod
    def tearDownClass(cls):
        cls.client_context.__exit__(None, None, None)
        cls.environment.stop()
        cls.temp.cleanup()

    def wait_for_job(self, job_id: int):
        for _ in range(100):
            response = self.client.get(f"/api/jobs/{job_id}", headers=self.headers)
            self.assertEqual(response.status_code, 200)
            job = response.json()
            if job["status"] in {"complete", "failed", "cancelled"}:
                return job
            time.sleep(0.02)
        self.fail("Job did not complete")

    def test_private_api_requires_token(self):
        unauthorized = self.client.get("/api/status")
        self.assertEqual(unauthorized.status_code, 401)
        self.assertEqual(unauthorized.headers["cache-control"], "no-store")
        self.assertIn("frame-ancestors 'none'", unauthorized.headers["content-security-policy"])
        self.assertEqual(self.client.get("/api/health").status_code, 200)

    def test_artwork_api_reports_missing_credentials_without_exposing_secrets(self):
        status = self.client.get("/api/artwork/status", headers=self.headers)
        self.assertEqual(status.status_code, 200)
        self.assertFalse(status.json()["configured"])
        games = self.client.get("/api/games", headers=self.headers).json()["items"]
        if games:
            self.assertIn("cover_asset_id", games[0])
            self.assertIn("artwork_count", games[0])
            response = self.client.post(
                "/api/artwork/scrape",
                headers=self.headers,
                json={"game_ids": [games[0]["id"]], "missing_only": True},
            )
            self.assertEqual(response.status_code, 400)
            self.assertNotIn("password", response.text.casefold())

    def test_dashboard_summarizes_collection_work_queues(self):
        scan = self.client.post("/api/scan", headers=self.headers)
        self.assertEqual(self.wait_for_job(scan.json()["job_id"])["status"], "complete")
        response = self.client.get("/api/dashboard", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        dashboard = response.json()
        self.assertGreaterEqual(dashboard["collection"]["games"], 1)
        self.assertGreaterEqual(dashboard["collection"]["files"], 1)
        self.assertTrue(any(item["platform"] == "gba" for item in dashboard["platforms"]))
        self.assertIn("reclaimable_bytes", dashboard["cleanup"])
        self.assertIn("games", dashboard["artwork"])
        self.assertIn("save_files", dashboard["saves"])
        self.assertIsNotNone(dashboard["last_scan"])
        self.assertGreaterEqual(len(dashboard["recent_jobs"]), 1)
        self.assertNotIn("result_json", dashboard["recent_jobs"][0])

    def test_cross_site_mutation_is_rejected(self):
        response = self.client.post(
            "/api/scan",
            headers={**self.headers, "Origin": "https://attacker.example"},
        )
        self.assertEqual(response.status_code, 403)

    def test_duplicate_endpoint_groups_complete_matching_sets(self):
        first = self.root / "roms/gba/Original Name.gba"
        second = self.root / "roms/gba/Differently Named Copy.gba"
        usa = self.root / "roms/gba/Variant Game (USA).gba"
        europe = self.root / "roms/gba/Variant Game (Europe).gba"
        device_first = self.root / "devices/handheld/roms/gba/Original Name.gba"
        device_second = self.root / "devices/handheld/roms/gba/Differently Named Copy.gba"
        first.write_bytes(b"same-rom-content")
        second.write_bytes(b"same-rom-content")
        usa.write_bytes(b"usa-content")
        europe.write_bytes(b"europe-content")
        try:
            scan = self.client.post("/api/scan?confirm_prune=true", headers=self.headers)
            self.assertEqual(self.wait_for_job(scan.json()["job_id"])["status"], "complete")
            device_first.parent.mkdir(parents=True, exist_ok=True)
            device_first.write_bytes(b"same-rom-content")
            response = self.client.get(
                "/api/duplicates?kind=exact&search=Original%20Name",
                headers=self.headers,
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            matching = [
                group for group in data["items"]
                if {item["display_name"] for item in group["items"]}
                == {"Original Name", "Differently Named Copy"}
            ]
            self.assertEqual(len(matching), 1)
            self.assertEqual(matching[0]["copies"], 2)
            self.assertEqual(matching[0]["kind"], "exact")
            self.assertEqual(len(matching[0]["key"]), 64)
            original = next(
                item for item in matching[0]["items"] if item["display_name"] == "Original Name"
            )
            self.assertEqual(matching[0]["recommended_keeper_id"], original["id"])
            self.assertEqual(original["present_devices"], ["handheld"])
            self.assertFalse(matching[0]["device_conflict"])
            device_second.write_bytes(b"same-rom-content")
            conflicted = self.client.get(
                "/api/duplicates?kind=exact&search=Original%20Name",
                headers=self.headers,
            ).json()["items"][0]
            self.assertIsNone(conflicted["recommended_keeper_id"])
            self.assertTrue(conflicted["device_conflict"])
            possible = self.client.get(
                "/api/duplicates?kind=possible&search=Variant%20Game",
                headers=self.headers,
            ).json()
            self.assertEqual(possible["total"], 1)
            self.assertEqual(
                {item["display_name"] for item in possible["items"][0]["items"]},
                {"Variant Game (USA)", "Variant Game (Europe)"},
            )
        finally:
            first.unlink(missing_ok=True)
            second.unlink(missing_ok=True)
            usa.unlink(missing_ok=True)
            europe.unlink(missing_ok=True)
            device_first.unlink(missing_ok=True)
            device_second.unlink(missing_ok=True)
            scan = self.client.post("/api/scan?confirm_prune=true", headers=self.headers)
            self.assertEqual(self.wait_for_job(scan.json()["job_id"])["status"], "complete")

    def test_device_view_uses_actual_files_and_reports_selection_state(self):
        scan = self.client.post("/api/scan", headers=self.headers)
        self.assertEqual(self.wait_for_job(scan.json()["job_id"])["status"], "complete")
        game = self.client.get("/api/games", headers=self.headers).json()["items"][0]
        device = self.client.get("/api/devices", headers=self.headers).json()[0]
        source = self.root / "roms" / game["primary_relpath"]
        target = self.root / "devices" / device["path"] / "roms" / game["primary_relpath"]
        unknown = target.parent / "Unknown.gba"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        unknown.write_bytes(b"unknown")
        try:
            response = self.client.get(
                f"/api/games?device_id={device['id']}&device_scope=on_device",
                headers=self.headers,
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["total"], 1)
            self.assertEqual(data["items"][0]["device_state"], "unmanaged")
            self.assertEqual(data["device_inventory"]["present_games"], 1)
            self.assertEqual(data["device_inventory"]["unmatched_files"], 1)

            selected = self.client.put(
                f"/api/devices/{device['id']}/selection",
                headers=self.headers,
                json={"game_id": game["id"], "selected": True},
            )
            self.assertEqual(selected.status_code, 200)
            updated = self.client.get(
                f"/api/games?device_id={device['id']}&device_scope=on_device",
                headers=self.headers,
            ).json()
            self.assertEqual(updated["items"][0]["device_state"], "pending_update")
            self.assertEqual(updated["device_inventory"]["changes"], 1)
        finally:
            self.client.put(
                f"/api/devices/{device['id']}/selection",
                headers=self.headers,
                json={"game_id": game["id"], "selected": False},
            )
            target.unlink(missing_ok=True)
            unknown.unlink(missing_ok=True)

    def test_running_scan_can_be_cancelled(self):
        started = threading.Event()

        def slow_scan(*_, progress_callback=None, cancel_check=None, issue_callback=None, **__):
            if progress_callback:
                progress_callback(10, "Hashing a test file")
            if issue_callback:
                issue_callback("gba/unreadable.gba: permission denied")
            started.set()
            while True:
                cancel_check()
                time.sleep(0.01)

        with patch.object(self.main.library, "scan", side_effect=slow_scan):
            response = self.client.post("/api/scan", headers=self.headers)
            self.assertEqual(response.status_code, 202)
            job_id = response.json()["job_id"]
            self.assertTrue(started.wait(timeout=1))
            cancelled = self.client.post(f"/api/jobs/{job_id}/cancel", headers=self.headers)
            self.assertEqual(cancelled.status_code, 202)
            job = self.wait_for_job(job_id)

        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(job["detail"], "Stopped by user")
        self.assertFalse(job["cancellable"])
        issues = self.client.get(f"/api/jobs/{job_id}/issues", headers=self.headers).json()
        self.assertEqual(issues["total"], 1)
        self.assertEqual(issues["items"][0]["detail"], "gba/unreadable.gba: permission denied")

    def test_save_snapshot_compare_download_and_restore(self):
        save = self.root / "saves/saves/Test Game.srm"
        manifest = self.root / "saves/manifest.server"
        save.parent.mkdir(parents=True, exist_ok=True)
        save.write_bytes(b"version-one")
        manifest.write_bytes(b"manifest-one")
        created = self.client.post(
            "/api/saves/snapshots", headers=self.headers, json={"note": "Before testing"}
        )
        self.assertEqual(created.status_code, 202)
        job = self.wait_for_job(created.json()["job_id"])
        self.assertEqual(job["status"], "complete")
        snapshot_id = job["result"]["snapshot_id"]

        save.write_bytes(b"version-two")
        extra = self.root / "saves/states/Test Game.state"
        extra.parent.mkdir(parents=True)
        extra.write_bytes(b"state")
        comparison = self.client.get(
            f"/api/saves/snapshots/{snapshot_id}/compare", headers=self.headers
        ).json()
        self.assertEqual(comparison["overwrite"], ["saves/Test Game.srm"])
        self.assertEqual(comparison["delete"], ["states/Test Game.state"])

        downloaded = self.client.get(
            f"/api/saves/snapshots/{snapshot_id}/files/saves/Test%20Game.srm",
            headers=self.headers,
        )
        self.assertEqual(downloaded.content, b"version-one")
        restored = self.client.post(
            f"/api/saves/snapshots/{snapshot_id}/restore",
            headers=self.headers,
            json={
                "expected_tree_hash": comparison["current_tree_hash"],
                "retroarch_closed": True,
            },
        )
        restore_job = self.wait_for_job(restored.json()["job_id"])
        self.assertEqual(restore_job["status"], "complete")
        self.assertEqual(save.read_bytes(), b"version-one")
        self.assertEqual(manifest.read_bytes(), b"manifest-one")
        self.assertFalse(extra.exists())

        overview = self.client.get("/api/saves", headers=self.headers).json()
        self.assertGreaterEqual(overview["snapshot_count"], 2)

    def test_orphan_save_cleanup_is_snapshot_backed(self):
        orphan = self.root / "saves/saves/mGBA/Abandoned Game.srm"
        orphan.parent.mkdir(parents=True, exist_ok=True)
        orphan.write_bytes(b"orphan")
        report = self.client.get(
            "/api/saves/unmatched?status=orphan&search=Abandoned%20Game",
            headers=self.headers,
        ).json()
        self.assertEqual(report["total"], 1)

        response = self.client.post(
            "/api/saves/orphans/delete",
            headers=self.headers,
            json={"group_key": report["items"][0]["key"]},
        )
        self.assertEqual(response.status_code, 202)
        job = self.wait_for_job(response.json()["job_id"])

        self.assertEqual(job["status"], "complete")
        self.assertEqual(job["result"]["files"], 1)
        self.assertIn("safety_snapshot_id", job["result"])
        self.assertFalse(orphan.exists())

    def test_scan_report_lists_every_unreadable_path(self):
        outside = self.root / "outside.gba"
        outside.write_bytes(b"outside")
        links = [self.root / "roms/gba" / f"Broken {index}.gba" for index in range(55)]
        for link in links:
            link.symlink_to(outside)
        try:
            response = self.client.post("/api/scan", headers=self.headers)
            job = self.wait_for_job(response.json()["job_id"])
        finally:
            for link in links:
                link.unlink(missing_ok=True)

        self.assertEqual(job["status"], "complete")
        self.assertEqual(job["reported_issue_count"], 55)
        self.assertEqual(job["issue_count"], 55)
        issues = self.client.get(
            f"/api/jobs/{job['id']}/issues?limit=20&offset=40", headers=self.headers
        ).json()
        self.assertTrue(issues["captured_all"])
        self.assertEqual(issues["total"], 55)
        self.assertEqual(len(issues["items"]), 15)
        self.assertTrue(issues["items"][0]["detail"].startswith("gba/Broken "))

        report = self.client.get(f"/api/jobs/{job['id']}", headers=self.headers).json()
        self.assertEqual(report["result"]["games"], 1)
        self.assertEqual(report["result"]["skipped_count"], 55)

    def test_naming_catalog_suggestions_apply_as_background_job(self):
        scan = self.client.post("/api/scan", headers=self.headers)
        self.assertEqual(self.wait_for_job(scan.json()["job_id"])["status"], "complete")
        imported = self.client.post(
            "/api/naming/catalogs",
            headers=self.headers,
            json={
                "source_name": "GBA.dat",
                "platform": "gba",
                "content": '<datafile><game name="Test"><rom name="Test Game (USA).gba" size="8"/></game></datafile>',
            },
        )
        self.assertEqual(imported.status_code, 200)
        suggestions = self.client.get("/api/naming/suggestions", headers=self.headers).json()
        self.assertEqual(suggestions["total"], 1)
        suggestion = suggestions["items"][0]
        self.assertEqual(suggestion["confidence"], "strong")
        applied = self.client.post(
            "/api/naming/apply",
            headers=self.headers,
            json={"items": [{"game_id": suggestion["game_id"], "name": suggestion["suggested_name"]}]},
        )
        self.assertEqual(applied.status_code, 202)
        self.assertEqual(self.wait_for_job(applied.json()["job_id"])["status"], "complete")
        self.assertEqual(
            self.client.delete(
                f"/api/naming/catalogs/{imported.json()['catalog_id']}", headers=self.headers
            ).status_code,
            200,
        )

    def test_scan_and_rename_complete_as_background_jobs(self):
        scan = self.client.post("/api/scan", headers=self.headers)
        self.assertEqual(scan.status_code, 202)
        scan_job = self.wait_for_job(scan.json()["job_id"])
        self.assertEqual(scan_job["status"], "complete")
        games = self.client.get("/api/games", headers=self.headers).json()["items"]
        self.assertEqual(len(games), 1)
        rename = self.client.patch(
            f"/api/games/{games[0]['id']}/rename",
            headers=self.headers,
            json={"name": "Renamed Game"},
        )
        self.assertEqual(rename.status_code, 202)
        rename_job = self.wait_for_job(rename.json()["job_id"])
        self.assertEqual(rename_job["status"], "complete")
        self.assertEqual(rename_job["result"]["new_name"], "Renamed Game")


class StartupTokenTests(unittest.TestCase):
    """A missing token disables authentication entirely, so startup must reject it.

    This previously depended on the existing-roots setting, which meant a bare
    `docker run` — or turning that flag off to debug a mount — silently exposed every
    destructive endpoint.
    """

    def _start(self, **overrides):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        (root / "roms").mkdir()
        (root / "devices").mkdir()
        environment = {
            "ROMMATES_LIBRARY_ROOT": str(root / "roms"),
            "ROMMATES_DEVICES_ROOT": str(root / "devices"),
            "ROMMATES_TRASH_ROOT": str(root / "trash"),
            "ROMMATES_DATABASE_PATH": str(root / "data/rommates.db"),
            "ROMMATES_SAVES_ROOT": str(root / "saves"),
            "ROMMATES_SNAPSHOTS_ROOT": str(root / "snapshots"),
            "ROMMATES_SAVE_SNAPSHOT_QUIET_SECONDS": "0",
            "ROMMATES_SCAN_ON_START": "false",
            "ROMMATES_REQUIRE_EXISTING_ROOTS": "false",
            "ROMMATES_ACCESS_TOKEN": "",
            "ROMMATES_ALLOW_ANONYMOUS": "false",
            **overrides,
        }
        with patch.dict(os.environ, environment):
            (root / "saves").mkdir()
            main = importlib.reload(importlib.import_module("app.main"))
            return TestClient(main.app)

    def tearDown(self):
        # Leave the module bound to this class's environment rather than a stale one.
        importlib.reload(importlib.import_module("app.main"))

    def test_missing_token_fails_startup(self):
        with self.assertRaisesRegex(Exception, "ROMMATES_ACCESS_TOKEN must contain at least"):
            with self._start():
                pass

    def test_short_token_fails_startup(self):
        with self.assertRaisesRegex(Exception, "ROMMATES_ACCESS_TOKEN must contain at least"):
            with self._start(ROMMATES_ACCESS_TOKEN="too-short"):
                pass

    def test_anonymous_access_requires_an_explicit_opt_in(self):
        with self._start(ROMMATES_ALLOW_ANONYMOUS="true") as client:
            self.assertEqual(client.get("/api/status").status_code, 200)


if __name__ == "__main__":
    unittest.main()
