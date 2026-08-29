from __future__ import annotations

import importlib
import io
import os
import tempfile
import threading
import time
import unittest
import zipfile
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
                "ROMMATES_UPLOAD_ROOT": str(root / "uploads"),
                "ROMMATES_UPLOAD_CHUNK_BYTES": "4",
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

    def test_ui_pages_support_direct_navigation(self):
        for path in (
            "/",
            "/library",
            "/transfers",
            "/duplicates",
            "/naming",
            "/devices",
            "/saves",
            "/jobs",
            "/trash",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn('<nav id="navigation">', response.text)

        self.assertEqual(self.client.get("/not-a-rommates-page").status_code, 404)

    def test_upload_resumes_finalizes_and_downloads_without_bearer_token(self):
        manifest = {
            "platform": "gba",
            "bundle_name": "",
            "folder_mode": False,
            "files": [{"relative_path": "Uploaded Game.gba", "size": 11}],
        }
        created = self.client.post("/api/uploads", headers=self.headers, json=manifest)
        self.assertEqual(created.status_code, 201, created.text)
        session = created.json()
        first = self.client.put(
            f"/api/uploads/{session['id']}/files/0",
            headers={**self.headers, "Upload-Offset": "0", "Content-Type": "application/octet-stream"},
            content=b"upld",
        )
        self.assertEqual(first.status_code, 200, first.text)
        resumed = self.client.post("/api/uploads", headers=self.headers, json=manifest)
        self.assertEqual(resumed.json()["files"][0]["received_size"], 4)
        for offset, chunk in ((4, b"-rom"), (8, b"-ok")):
            response = self.client.put(
                f"/api/uploads/{session['id']}/files/0",
                headers={**self.headers, "Upload-Offset": str(offset), "Content-Type": "application/octet-stream"},
                content=chunk,
            )
            self.assertEqual(response.status_code, 200, response.text)
        finalized = self.client.post(f"/api/uploads/{session['id']}/finalize", headers=self.headers)
        job = self.wait_for_job(finalized.json()["job_id"])
        self.assertEqual(job["status"], "complete", job)
        self.assertEqual((self.root / "roms/gba/Uploaded Game.gba").read_bytes(), b"upld-rom-ok")
        games = self.client.get("/api/games?search=Uploaded%20Game", headers=self.headers).json()["items"]
        self.assertEqual(len(games), 1)
        ticket = self.client.post(
            f"/api/games/{games[0]['id']}/download-ticket", headers=self.headers
        )
        self.assertEqual(ticket.status_code, 200, ticket.text)
        download = self.client.get(ticket.json()["url"])
        self.assertEqual(download.status_code, 200)
        self.assertEqual(download.content, b"upld-rom-ok")
        self.assertEqual(self.client.get(ticket.json()["url"]).status_code, 200)

    def test_upload_rejects_path_traversal(self):
        response = self.client.post(
            "/api/uploads",
            headers=self.headers,
            json={
                "platform": "gba",
                "bundle_name": "",
                "folder_mode": False,
                "files": [{"relative_path": "../escape.gba", "size": 1}],
            },
        )
        self.assertEqual(response.status_code, 400)

    def test_multifile_download_is_a_streamed_zip(self):
        cue = b'FILE "Track 01.bin" BINARY\n  TRACK 01 MODE1/2352\n'
        track = b"disc-track"
        manifest = {
            "platform": "gba",
            "bundle_name": "Disc Upload",
            "folder_mode": True,
            "files": [
                {"relative_path": "Disc Upload.cue", "size": len(cue)},
                {"relative_path": "Track 01.bin", "size": len(track)},
            ],
        }
        session = self.client.post("/api/uploads", headers=self.headers, json=manifest).json()
        for index, content in enumerate((cue, track)):
            response = self.client.put(
                f"/api/uploads/{session['id']}/files/{index}",
                headers={**self.headers, "Upload-Offset": "0", "Content-Type": "application/octet-stream"},
                content=content,
            )
            self.assertEqual(response.status_code, 200, response.text)
        queued = self.client.post(f"/api/uploads/{session['id']}/finalize", headers=self.headers)
        self.assertEqual(self.wait_for_job(queued.json()["job_id"])["status"], "complete")
        game = self.client.get("/api/games?search=Disc%20Upload", headers=self.headers).json()["items"][0]
        ticket = self.client.post(f"/api/games/{game['id']}/download-ticket", headers=self.headers).json()
        self.assertTrue(ticket["archive"])
        response = self.client.get(ticket["url"])
        self.assertEqual(response.status_code, 200, response.text)
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(set(archive.namelist()), {"Disc Upload/Disc Upload.cue", "Disc Upload/Track 01.bin"})
            self.assertEqual(archive.read("Disc Upload/Track 01.bin"), track)
        for path in (self.root / "roms/gba/Disc Upload").iterdir():
            path.unlink()
        (self.root / "roms/gba/Disc Upload").rmdir()
        scan = self.client.post("/api/scan?confirm_prune=true", headers=self.headers)
        self.assertEqual(self.wait_for_job(scan.json()["job_id"])["status"], "complete")

    def test_multiple_trash_items_are_purged_in_one_job(self):
        paths = [
            self.root / "roms/gba/Bulk Purge One.gba",
            self.root / "roms/gba/Bulk Purge Two.gba",
        ]
        for index, path in enumerate(paths):
            path.write_bytes(f"purge-{index}".encode())
        scan = self.client.post("/api/scan?confirm_prune=true", headers=self.headers)
        self.assertEqual(self.wait_for_job(scan.json()["job_id"])["status"], "complete")
        games = self.client.get("/api/games?search=Bulk%20Purge", headers=self.headers).json()["items"]
        self.assertEqual(len(games), 2)
        for game in games:
            queued = self.client.delete(f"/api/games/{game['id']}", headers=self.headers)
            self.assertEqual(self.wait_for_job(queued.json()["job_id"])["status"], "complete")
        trash = self.client.get("/api/trash", headers=self.headers).json()
        selected = [item["id"] for item in trash if item["game_name"].startswith("Bulk Purge")]
        self.assertEqual(len(selected), 2)
        queued = self.client.post(
            "/api/trash/purge", headers=self.headers, json={"trash_ids": selected}
        )
        job = self.wait_for_job(queued.json()["job_id"])
        self.assertEqual(job["status"], "complete", job)
        self.assertEqual(job["result"]["purged"], 2)

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

    def test_library_sorts_by_screenscraper_rating_and_reports_platform_rank(self):
        paths = [
            self.root / "roms/gba/Rating High.gba",
            self.root / "roms/gba/Rating Low.gba",
            self.root / "roms/gba/Rating Unknown.gba",
        ]
        for index, path in enumerate(paths):
            path.write_bytes(f"rating-{index}".encode())
        try:
            scan = self.client.post("/api/scan?confirm_prune=true", headers=self.headers)
            self.assertEqual(self.wait_for_job(scan.json()["job_id"])["status"], "complete")
            games = self.client.get(
                "/api/games?platform=gba&search=Rating%20&limit=100", headers=self.headers
            ).json()["items"]
            ids = {game["display_name"]: game["id"] for game in games}
            with self.main.db.write() as connection:
                for name, rating, staff in (
                    ("Rating High", 19.0, 1),
                    ("Rating Low", 8.0, 0),
                ):
                    connection.execute(
                        "INSERT INTO game_metadata(game_id,source,source_game_id,source_system_id,match_method,rating,top_staff) "
                        "VALUES(?,'screenscraper',?,12,'hash',?,?)",
                        (ids[name], str(ids[name]), rating, staff),
                    )

            response = self.client.get(
                "/api/games?platform=gba&search=Rating%20&sort=rating_desc&limit=100",
                headers=self.headers,
            )
            self.assertEqual(response.status_code, 200)
            items = response.json()["items"]
            self.assertEqual(
                [item["display_name"] for item in items],
                ["Rating High", "Rating Low", "Rating Unknown"],
            )
            self.assertEqual(items[0]["rating"], 19.0)
            self.assertEqual(items[0]["platform_rank"], 1)
            self.assertEqual(items[0]["top_staff"], 1)
            self.assertIsNone(items[-1]["rating"])
            platforms = self.client.get("/api/platforms", headers=self.headers).json()
            gba = next(item for item in platforms if item["platform"] == "gba")
            self.assertGreaterEqual(gba["rated_count"], 2)
        finally:
            for path in paths:
                path.unlink(missing_ok=True)
            scan = self.client.post("/api/scan?confirm_prune=true", headers=self.headers)
            self.assertEqual(self.wait_for_job(scan.json()["job_id"])["status"], "complete")

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

    def test_reviewed_duplicate_groups_are_trashed_in_one_job(self):
        paths = [
            self.root / "roms/gba/Alpha Keeper.gba",
            self.root / "roms/gba/Alpha Copy.gba",
            self.root / "roms/gba/Beta Keeper.gba",
            self.root / "roms/gba/Beta Copy.gba",
        ]
        paths[0].write_bytes(b"alpha")
        paths[1].write_bytes(b"alpha")
        paths[2].write_bytes(b"beta")
        paths[3].write_bytes(b"beta")
        scan = self.client.post("/api/scan?confirm_prune=true", headers=self.headers)
        self.assertEqual(self.wait_for_job(scan.json()["job_id"])["status"], "complete")
        groups = self.client.get("/api/duplicates?kind=exact", headers=self.headers).json()["items"]
        reviewed = []
        for expected in ({"Alpha Keeper", "Alpha Copy"}, {"Beta Keeper", "Beta Copy"}):
            group = next(
                item for item in groups
                if {game["display_name"] for game in item["items"]} == expected
            )
            keeper = next(game for game in group["items"] if game["display_name"].endswith("Keeper"))
            reviewed.append({"kind": "exact", "group_key": group["key"], "keeper_id": keeper["id"]})

        response = self.client.post(
            "/api/duplicates/trash",
            headers=self.headers,
            json={"items": reviewed},
        )

        self.assertEqual(response.status_code, 202)
        job = self.wait_for_job(response.json()["job_id"])
        self.assertEqual(job["status"], "complete")
        self.assertEqual(job["result"]["groups"], 2)
        self.assertEqual(job["result"]["trashed"], 2)
        names = {
            item["display_name"]
            for item in self.client.get("/api/games?limit=200", headers=self.headers).json()["items"]
        }
        self.assertIn("Alpha Keeper", names)
        self.assertIn("Beta Keeper", names)
        self.assertNotIn("Alpha Copy", names)
        self.assertNotIn("Beta Copy", names)
        for path in paths:
            path.unlink(missing_ok=True)
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
            library_game = next(
                item for item in self.client.get(
                    "/api/games?limit=1000", headers=self.headers
                ).json()["items"]
                if item["id"] == game["id"]
            )
            self.assertEqual(
                library_game["devices"],
                [{"id": device["id"], "name": device["name"], "state": "present"}],
            )
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

    def test_device_deployment_mode_is_configurable_and_previewed(self):
        device = self.client.get("/api/devices", headers=self.headers).json()[0]
        try:
            updated = self.client.put(
                f"/api/devices/{device['id']}/deployment-mode",
                headers=self.headers,
                json={"mode": "hardlink"},
            )
            self.assertEqual(updated.status_code, 200)
            self.assertEqual(updated.json()["mode"], "hardlink")
            preview = self.client.get(
                f"/api/devices/{device['id']}/preview", headers=self.headers
            )
            self.assertEqual(preview.status_code, 200)
            self.assertEqual(preview.json()["device"]["deployment_mode"], "hardlink")
            self.assertIn("conversions", preview.json())
            self.assertIn("hardlinked", preview.json())
            self.assertIn("copied", preview.json())
        finally:
            self.client.put(
                f"/api/devices/{device['id']}/deployment-mode",
                headers=self.headers,
                json={"mode": "copy"},
            )

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

    def test_duplicate_atomic_jobs_stay_queued_and_coalesce(self):
        calls = {"count": 0}

        def operation():
            calls["count"] += 1
            return {"done": True}

        self.assertTrue(self.main.library_job_lock.acquire(timeout=1))
        try:
            first = self.main.enqueue_job(
                "delete", "Synthetic queued deletion", operation, coalesce=True
            )
            second = self.main.enqueue_job(
                "delete", "Synthetic queued deletion", operation, coalesce=True
            )
            self.assertEqual(first, second)
            queued = self.client.get(f"/api/jobs/{first}", headers=self.headers).json()
            self.assertEqual(queued["status"], "queued")
            self.assertTrue(queued["cancellable"])

            response = self.client.post(f"/api/jobs/{first}/cancel", headers=self.headers)
            self.assertEqual(response.status_code, 202)
            self.assertEqual(self.wait_for_job(first)["status"], "cancelled")
            self.assertEqual(calls["count"], 0)
        finally:
            self.main.library_job_lock.release()

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
