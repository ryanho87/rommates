from __future__ import annotations

import importlib
import hashlib
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
from PIL import Image


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
                "ROMMATES_MEDIA_ROOT": str(root / "media"),
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

        mcp_unauthorized = self.client.post("/mcp/", json={})
        self.assertEqual(mcp_unauthorized.status_code, 401)
        self.assertEqual(mcp_unauthorized.headers["cache-control"], "no-store")

        mcp_authorized = self.client.post(
            "/mcp/",
            headers={
                **self.headers,
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
            },
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "rommates-test", "version": "1"},
                },
            },
        )
        self.assertEqual(mcp_authorized.status_code, 200, mcp_authorized.text)
        self.assertEqual(mcp_authorized.json()["result"]["serverInfo"]["name"], "ROMmates")

    def test_onboarding_progress_is_persisted_per_account(self):
        created = self.client.post(
            "/api/users",
            headers=self.headers,
            json={
                "username": "tour-test",
                "display_name": "Tour Test",
                "password": "tour-test-password",
                "role": "viewer",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        login = self.client.post(
            "/api/auth/login",
            json={"username": "tour-test", "password": "tour-test-password"},
        )
        self.assertEqual(login.status_code, 200, login.text)
        changed = self.client.post(
            "/api/auth/password",
            json={
                "current_password": "tour-test-password",
                "new_password": "tour-test-password-changed",
            },
        )
        self.assertEqual(changed.status_code, 200, changed.text)

        initial = self.client.get(
            "/api/onboarding", params={"tour_key": "getting-started-viewer"}
        )
        self.assertEqual(initial.status_code, 200, initial.text)
        self.assertTrue(initial.json()["persistent"])
        self.assertFalse(initial.json()["completed"])

        updated = self.client.patch(
            "/api/onboarding",
            json={
                "tour_key": "getting-started-viewer",
                "tour_version": 1,
                "current_step": 1,
                "dismissed": False,
                "completed": True,
            },
        )
        self.assertEqual(updated.status_code, 200, updated.text)
        stored = self.client.get(
            "/api/onboarding", params={"tour_key": "getting-started-viewer"}
        ).json()
        self.assertEqual(stored["current_step"], 1)
        self.assertTrue(stored["completed"])
        self.client.post("/api/auth/logout")

    def test_account_profile_devices_and_platform_stats_are_self_service(self):
        scan = self.client.post("/api/scan", headers=self.headers)
        self.assertEqual(self.wait_for_job(scan.json()["job_id"])["status"], "complete")
        created = self.client.post(
            "/api/users",
            headers=self.headers,
            json={
                "username": "account-test",
                "display_name": "Account Test",
                "password": "account-test-password",
                "role": "member",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        user_id = created.json()["id"]
        with self.main.db.write() as connection:
            game_id = connection.execute(
                "SELECT id FROM games WHERE primary_relpath='gba/Test Game.gba'"
            ).fetchone()["id"]
            connection.execute(
                "INSERT INTO devices(name,path,owner_user_id) VALUES('account-test-device','account-test-device/roms',?)",
                (user_id,),
            )
            device_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            connection.execute(
                "INSERT INTO device_selections(device_id,game_id) VALUES(?,?)",
                (device_id, game_id),
            )
            connection.execute(
                "INSERT INTO deployments(device_id,game_id,relpath) VALUES(?,?,?)",
                (device_id, game_id, "gba/Test Game.gba"),
            )
        try:
            login = self.client.post(
                "/api/auth/login",
                json={"username": "account-test", "password": "account-test-password"},
            )
            self.assertEqual(login.status_code, 200, login.text)
            changed = self.client.post(
                "/api/auth/password",
                json={
                    "current_password": "account-test-password",
                    "new_password": "account-test-password-changed",
                },
            )
            self.assertEqual(changed.status_code, 200, changed.text)
            summary = self.client.get("/api/account/summary")
            self.assertEqual(summary.status_code, 200, summary.text)
            self.assertEqual(summary.json()["user"]["username"], "account-test")
            self.assertEqual(summary.json()["devices"][0]["name"], "account-test-device")
            self.assertEqual(summary.json()["devices"][0]["synced_roms"], 1)
            self.assertEqual(summary.json()["platforms"], [{"platform": "gba", "synced_roms": 1}])
            self.assertEqual(summary.json()["total_synced_roms"], 1)

            profile = self.client.patch(
                "/api/auth/profile",
                json={"username": "account-renamed", "display_name": "Renamed Account"},
            )
            self.assertEqual(profile.status_code, 200, profile.text)
            self.assertEqual(profile.json()["user"]["username"], "account-renamed")
            self.assertEqual(profile.json()["user"]["display_name"], "Renamed Account")
            self.assertEqual(self.client.get("/api/auth/me").json()["user"]["username"], "account-renamed")
            self.client.post("/api/auth/logout")
        finally:
            with self.main.db.write() as connection:
                connection.execute("DELETE FROM devices WHERE id=?", (device_id,))
                connection.execute("DELETE FROM users WHERE id=?", (user_id,))

    def test_ui_pages_support_direct_navigation(self):
        for path in (
            "/",
            "/library",
            "/artwork",
            "/transfers",
            "/duplicates",
            "/naming",
            "/devices",
            "/saves",
            "/jobs",
            "/notifications",
            "/account",
            "/users",
            "/trash",
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn('<nav id="navigation">', response.text)
                self.assertIn('id="mobile-menu-button"', response.text)
                self.assertIn('id="header-account"', response.text)
                self.assertNotIn('id="header-tour-button"', response.text)
                self.assertIn('id="guided-tour-button"', response.text)
                self.assertIn('id="tour-launcher"', response.text)
                self.assertIn('id="mobile-session-link"', response.text)
                self.assertIn('<div class="nav-backdrop" id="nav-backdrop"', response.text)
                self.assertIn('/static/styles.css?v=', response.text)
                self.assertIn('/static/app.js?v=', response.text)
                self.assertEqual(response.headers["cache-control"], "no-store")

        self.assertEqual(self.client.get("/not-a-rommates-page").status_code, 404)
        static = self.client.get("/static/app.js")
        self.assertEqual(static.status_code, 200)
        self.assertEqual(static.headers["cache-control"], "no-cache")

    def test_notification_preferences_are_private_and_test_requires_webhook(self):
        self.assertEqual(self.client.get("/api/notifications").status_code, 401)
        response = self.client.get("/api/notifications", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["configured"])
        self.assertTrue(any(event["key"] == "save_conflict" for event in payload["events"]))

        update = self.client.put(
            "/api/notifications/settings",
            headers=self.headers,
            json={"enabled": False, "events": {"save_conflict": True}},
        )
        self.assertEqual(update.status_code, 200)
        self.assertFalse(update.json()["enabled"])
        test = self.client.post("/api/notifications/test", headers=self.headers)
        self.assertEqual(test.status_code, 400)
        self.assertIn("ROMMATES_DISCORD_WEBHOOK_URL", test.json()["detail"])

    def test_viewer_can_browse_and_download_but_cannot_mutate(self):
        scan = self.client.post("/api/scan", headers=self.headers)
        self.assertEqual(self.wait_for_job(scan.json()["job_id"])["status"], "complete")
        created = self.client.post(
            "/api/users",
            headers=self.headers,
            json={
                "username": "viewer-test",
                "display_name": "Viewer Test",
                "password": "viewer-test-password",
                "role": "viewer",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        login = self.client.post(
            "/api/auth/login",
            json={"username": "viewer-test", "password": "viewer-test-password"},
        )
        self.assertEqual(login.status_code, 200, login.text)
        self.assertTrue(login.cookies.get("rommates_session"))
        self.assertTrue(login.json()["user"]["must_change_password"])
        self.assertEqual(self.client.get("/api/games").status_code, 403)
        changed = self.client.post(
            "/api/auth/password",
            json={
                "current_password": "viewer-test-password",
                "new_password": "viewer-test-password-changed",
            },
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertFalse(changed.json()["user"]["must_change_password"])
        games = self.client.get("/api/games")
        self.assertEqual(games.status_code, 200)
        self.assertEqual(games.json()["items"][0]["devices"], [])
        self.assertEqual(games.json()["items"][0]["device_count"], 0)
        game_id = games.json()["items"][0]["id"]
        detail = self.client.get(f"/api/games/{game_id}")
        self.assertEqual(detail.status_code, 200)
        self.assertEqual(detail.json()["devices"], [])
        self.assertEqual(detail.json()["save_impact"]["status"], "none")
        self.assertEqual(
            self.client.get("/api/games", params={"device_id": 1}).status_code, 403
        )
        self.assertEqual(
            self.client.post(f"/api/games/{game_id}/download-ticket").status_code, 200
        )
        self.assertEqual(self.client.post("/api/scan").status_code, 403)
        self.assertEqual(self.client.get("/api/users").status_code, 403)
        self.client.post("/api/auth/logout")

    def test_contributor_upload_waits_for_administrator_approval(self):
        created = self.client.post(
            "/api/users",
            headers=self.headers,
            json={
                "username": "contributor-test",
                "display_name": "Contributor Test",
                "password": "contributor-test-password",
                "role": "contributor",
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        login = self.client.post(
            "/api/auth/login",
            json={"username": "contributor-test", "password": "contributor-test-password"},
        )
        self.assertEqual(login.status_code, 200, login.text)
        self.assertEqual(self.client.post("/api/uploads", json={}).status_code, 403)
        changed = self.client.post(
            "/api/auth/password",
            json={
                "current_password": "contributor-test-password",
                "new_password": "contributor-test-password-changed",
            },
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        created_upload = self.client.post(
            "/api/uploads",
            json={
                "platform": "gba",
                "files": [{"relative_path": "Contributor Upload.gba", "size": 4}],
            },
        )
        self.assertEqual(created_upload.status_code, 201, created_upload.text)
        session_id = created_upload.json()["id"]
        chunk = self.client.put(
            f"/api/uploads/{session_id}/files/0",
            headers={"Content-Type": "application/octet-stream", "Upload-Offset": "0"},
            content=b"safe",
        )
        self.assertEqual(chunk.status_code, 200, chunk.text)
        submitted = self.client.post(f"/api/uploads/{session_id}/finalize")
        self.assertEqual(submitted.status_code, 200, submitted.text)
        self.assertTrue(submitted.json()["submitted"])
        self.assertFalse((self.root / "roms/gba/Contributor Upload.gba").exists())
        self.assertEqual(
            self.client.post(f"/api/uploads/{session_id}/approve").status_code, 403
        )
        approved = self.client.post(
            f"/api/uploads/{session_id}/approve", headers=self.headers
        )
        self.assertEqual(approved.status_code, 202, approved.text)
        job = self.wait_for_job(approved.json()["job_id"])
        self.assertEqual(job["status"], "complete", job)
        self.assertTrue((self.root / "roms/gba/Contributor Upload.gba").exists())
        self.client.post("/api/auth/logout")
        (self.root / "roms/gba/Contributor Upload.gba").unlink()
        cleanup = self.client.post("/api/scan?confirm_prune=true", headers=self.headers)
        self.assertEqual(self.wait_for_job(cleanup.json()["job_id"])["status"], "complete")

    def test_member_can_only_manage_owned_devices_and_jobs(self):
        created_user = self.client.post(
            "/api/users",
            headers=self.headers,
            json={
                "username": "member-test",
                "display_name": "Member Test",
                "password": "member-test-password",
                "roles": ["contributor", "member"],
            },
        )
        self.assertEqual(created_user.status_code, 201, created_user.text)
        member_id = created_user.json()["id"]
        self.assertEqual(created_user.json()["roles"], ["contributor", "member"])
        admin_device = self.client.post(
            "/api/devices",
            headers=self.headers,
            json={"name": "admin-only-device", "deployment_mode": "hardlink"},
        ).json()
        scan = self.client.post("/api/scan", headers=self.headers)
        self.assertEqual(self.wait_for_job(scan.json()["job_id"])["status"], "complete")

        login = self.client.post(
            "/api/auth/login",
            json={"username": "member-test", "password": "member-test-password"},
        )
        self.assertEqual(login.status_code, 200, login.text)
        changed = self.client.post(
            "/api/auth/password",
            json={
                "current_password": "member-test-password",
                "new_password": "member-test-password-changed",
            },
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(self.client.get("/api/uploads").status_code, 200)
        self.assertEqual(self.client.get("/api/devices").json(), [])

        created_device = self.client.post(
            "/api/devices",
            json={"name": "member-handheld", "deployment_mode": "hardlink"},
        )
        self.assertEqual(created_device.status_code, 201, created_device.text)
        owned = created_device.json()
        self.assertEqual(owned["owner_user_id"], member_id)
        self.assertIsNone(owned["syncthing_ready_at"])
        self.assertEqual(
            self.client.put(
                f"/api/devices/{owned['id']}/syncthing-ready", json={"ready": True}
            ).status_code,
            403,
        )
        listed = self.client.get("/api/devices").json()
        self.assertEqual([item["name"] for item in listed], ["member-handheld"])
        self.assertEqual(
            self.client.get(f"/api/devices/{admin_device['id']}/preview").status_code,
            404,
        )
        self.assertEqual(
            self.client.post(f"/api/devices/{admin_device['id']}/export-ticket").status_code,
            404,
        )
        self.assertEqual(
            self.client.post(f"/api/devices/{admin_device['id']}/discard-changes").status_code,
            404,
        )
        self.assertEqual(
            self.client.post(
                f"/api/devices/{owned['id']}/roster-link",
                json={"target_device_ids": [admin_device["id"]]},
            ).status_code,
            404,
        )

        games = self.client.get("/api/games").json()["items"]
        game_id = games[0]["id"]
        selected = self.client.put(
            f"/api/devices/{owned['id']}/selection",
            json={"game_id": game_id, "selected": True},
        )
        self.assertEqual(selected.status_code, 200, selected.text)
        detail = self.client.get(f"/api/games/{game_id}").json()
        self.assertEqual([item["name"] for item in detail["devices"]], ["member-handheld"])
        self.assertTrue(detail["devices"][0]["selected"])
        discarded = self.client.post(f"/api/devices/{owned['id']}/discard-changes")
        self.assertEqual(discarded.status_code, 200, discarded.text)
        self.assertEqual(discarded.json()["games"], 0)
        selected = self.client.put(
            f"/api/devices/{owned['id']}/selection",
            json={"game_id": game_id, "selected": True},
        )
        self.assertEqual(selected.status_code, 200, selected.text)
        applied = self.client.post(f"/api/devices/{owned['id']}/apply")
        self.assertEqual(applied.status_code, 202, applied.text)
        job_id = applied.json()["job_id"]
        for _ in range(100):
            own_job = self.client.get(f"/api/jobs/{job_id}")
            self.assertEqual(own_job.status_code, 200, own_job.text)
            if own_job.json()["status"] in {"complete", "failed", "cancelled"}:
                break
            time.sleep(0.02)
        self.assertEqual(own_job.json()["status"], "complete", own_job.text)
        self.assertEqual(own_job.json()["requested_by"], member_id)

        admin_scan = self.client.post("/api/scan", headers=self.headers)
        self.assertEqual(admin_scan.status_code, 202, admin_scan.text)
        self.assertEqual(
            self.client.get(f"/api/jobs/{admin_scan.json()['job_id']}").status_code,
            404,
        )
        self.assertEqual(
            self.client.put(
                f"/api/devices/{admin_device['id']}/selection",
                json={"game_id": game_id, "selected": True},
            ).status_code,
            404,
        )

        assigned = self.client.put(
            f"/api/devices/{admin_device['id']}/owner",
            headers=self.headers,
            json={"owner_user_id": member_id},
        )
        self.assertEqual(assigned.status_code, 200, assigned.text)
        self.assertIn(
            "admin-only-device",
            [item["name"] for item in self.client.get("/api/devices").json()],
        )
        ready = self.client.put(
            f"/api/devices/{owned['id']}/syncthing-ready",
            headers=self.headers,
            json={"ready": True},
        )
        self.assertEqual(ready.status_code, 200, ready.text)
        inbox = self.client.get("/api/inbox").json()
        self.assertEqual(inbox["unread"], 1)
        self.assertEqual(inbox["items"][0]["kind"], "device_ready")
        self.assertIn("member-handheld", inbox["items"][0]["title"])
        marked = self.client.post(f"/api/inbox/{inbox['items'][0]['id']}/read")
        self.assertEqual(marked.status_code, 200, marked.text)
        self.assertEqual(self.client.get("/api/inbox").json()["unread"], 0)
        self.client.post("/api/auth/logout")

    def test_device_creation_requests_discord_setup_notification(self):
        with patch("app.main.notifications.notify") as notify:
            response = self.client.post(
                "/api/devices",
                headers=self.headers,
                json={"name": "zz-discord-setup-device", "deployment_mode": "hardlink"},
            )
        self.assertEqual(response.status_code, 201, response.text)
        notify.assert_called_once()
        args, kwargs = notify.call_args
        self.assertEqual(args[0], "device_setup_required")
        self.assertIn("zz-discord-setup-device", args[1])
        self.assertEqual(args[3], "devices")
        self.assertEqual(kwargs["dedupe_key"], f"device:{response.json()['id']}:setup-required")

    def test_syncthing_status_is_private_and_explains_unconfigured_state(self):
        self.assertEqual(self.client.get("/api/syncthing/status").status_code, 401)
        response = self.client.get("/api/syncthing/status", headers=self.headers)
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertFalse(payload["configured"])
        self.assertFalse(payload["available"])
        self.assertEqual(payload["devices"], [])

    def test_create_device_registers_it_without_a_library_scan(self):
        unauthorized = self.client.post(
            "/api/devices", json={"name": "zz-new-device", "deployment_mode": "hardlink"}
        )
        self.assertEqual(unauthorized.status_code, 401)

        response = self.client.post(
            "/api/devices",
            headers=self.headers,
            json={"name": "zz-new-device", "deployment_mode": "hardlink"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        created = response.json()
        self.assertEqual(created["name"], "zz-new-device")
        self.assertEqual(created["relative_path"], "devices/zz-new-device/roms")
        self.assertTrue((self.root / "devices/zz-new-device/roms").is_dir())

        listed = self.client.get("/api/devices", headers=self.headers).json()
        self.assertIn("zz-new-device", [device["name"] for device in listed])

    def test_device_owner_can_stream_selected_roms_as_one_package(self):
        with patch("app.main.notifications.notify") as notify:
            device = self.client.post(
                "/api/devices",
                headers=self.headers,
                json={
                    "name": "zz-export-device",
                    "deployment_mode": "hardlink",
                    "delivery_mode": "download",
                },
            ).json()
        notify.assert_not_called()
        self.assertEqual(device["delivery_mode"], "download")
        game = self.client.get("/api/games", headers=self.headers).json()["items"][0]
        selected = self.client.put(
            f"/api/devices/{device['id']}/selection",
            headers=self.headers,
            json={"game_id": game["id"], "selected": True},
        )
        self.assertEqual(selected.status_code, 200, selected.text)

        queued = self.client.post(
            f"/api/devices/{device['id']}/export-ticket", headers=self.headers
        )
        self.assertEqual(queued.status_code, 202, queued.text)
        job = self.wait_for_job(queued.json()["job_id"])
        self.assertEqual(job["status"], "complete", job)
        ticket = job["result"]
        self.assertEqual(ticket["games"], 1)
        self.assertEqual(ticket["files"], 1)
        self.assertEqual(ticket["filename"], "zz-export-device-roms.zip")

        response = self.client.get(ticket["url"])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["content-type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertEqual(archive.namelist(), ["gba/Test Game.gba"])
            self.assertEqual(archive.read("gba/Test Game.gba"), b"test-rom")
        self.assertEqual(self.client.get(ticket["url"]).status_code, 404)
        with self.main.db.write() as connection:
            connection.execute("DELETE FROM devices WHERE id=?", (device["id"],))
        (self.root / "devices/zz-export-device/roms").rmdir()
        (self.root / "devices/zz-export-device").rmdir()

    def test_device_creation_can_clone_and_link_an_existing_roster(self):
        if not self.client.get("/api/games", headers=self.headers).json()["items"]:
            self.main.library.scan(force_prune=True)
        source = self.client.post(
            "/api/devices",
            headers=self.headers,
            json={"name": "zz-roster-source", "deployment_mode": "hardlink"},
        ).json()
        with self.main.db.connect() as connection:
            owner = connection.execute(
                "SELECT id FROM users WHERE username_normalized='roster-owner-test'"
            ).fetchone()
        if owner is None:
            owner = self.main.auth.create_user(
                "roster-owner-test",
                "Roster Owner Test",
                "roster-owner-test-password",
                ["member"],
            )
        owner_id = owner["id"]
        assigned = self.client.put(
            f"/api/devices/{source['id']}/owner",
            headers=self.headers,
            json={"owner_user_id": owner_id},
        )
        self.assertEqual(assigned.status_code, 200, assigned.text)
        game = self.client.get("/api/games", headers=self.headers).json()["items"][0]
        self.client.put(
            f"/api/devices/{source['id']}/selection",
            headers=self.headers,
            json={"game_id": game["id"], "selected": True},
        )
        clone = self.client.post(
            "/api/devices",
            headers=self.headers,
            json={
                "name": "zz-roster-clone",
                "deployment_mode": "hardlink",
                "clone_device_id": source["id"],
                "keep_in_sync": True,
            },
        )
        self.assertEqual(clone.status_code, 201, clone.text)
        clone_payload = clone.json()
        self.assertEqual(clone_payload["cloned_games"], 1)
        devices = self.client.get("/api/devices", headers=self.headers).json()
        source_row = next(item for item in devices if item["id"] == source["id"])
        clone_row = next(item for item in devices if item["id"] == clone_payload["id"])
        self.assertEqual(source_row["roster_group_id"], clone_row["roster_group_id"])
        self.assertIsNotNone(source_row["roster_group_id"])
        group_id = source_row["roster_group_id"]
        renamed = self.client.put(
            f"/api/device-groups/{group_id}",
            headers=self.headers,
            json={"name": "Travel handhelds"},
        )
        self.assertEqual(renamed.status_code, 200, renamed.text)
        self.assertEqual(renamed.json()["name"], "Travel handhelds")
        devices = self.client.get("/api/devices", headers=self.headers).json()
        self.assertEqual(
            {item["roster_group_name"] for item in devices if item["roster_group_id"] == group_id},
            {"Travel handhelds"},
        )
        with patch.object(
            self.main,
            "queue_device_apply_job",
            side_effect=[{"job_id": 901}, {"job_id": 902}],
        ):
            applied = self.client.post(
                f"/api/device-groups/{group_id}/apply", headers=self.headers
            )
        self.assertEqual(applied.status_code, 202, applied.text)
        self.assertEqual(applied.json()["job_ids"], [901, 902])

        self.client.put(
            f"/api/devices/{clone_payload['id']}/selection",
            headers=self.headers,
            json={"game_id": game["id"], "selected": False},
        )
        detail = self.client.get(f"/api/games/{game['id']}", headers=self.headers).json()
        selection = {item["id"]: bool(item["selected"]) for item in detail["devices"]}
        self.assertFalse(selection[source["id"]])
        self.assertFalse(selection[clone_payload["id"]])

        with self.main.db.write() as connection:
            connection.execute(
                "DELETE FROM devices WHERE id IN (?,?)", (source["id"], clone_payload["id"])
            )
        for name in ("zz-roster-source", "zz-roster-clone"):
            (self.root / "devices" / name / "roms").rmdir()
            (self.root / "devices" / name).rmdir()

    def test_existing_device_can_clone_another_roster_once(self):
        if not self.client.get("/api/games", headers=self.headers).json()["items"]:
            self.main.library.scan(force_prune=True)
        source = self.client.post(
            "/api/devices", headers=self.headers,
            json={"name": "zz-existing-clone-source", "deployment_mode": "hardlink"},
        ).json()
        target = self.client.post(
            "/api/devices", headers=self.headers,
            json={"name": "zz-existing-clone-target", "deployment_mode": "hardlink"},
        ).json()
        game = self.client.get("/api/games", headers=self.headers).json()["items"][0]
        self.client.put(
            f"/api/devices/{source['id']}/selection", headers=self.headers,
            json={"game_id": game["id"], "selected": True},
        )

        response = self.client.post(
            f"/api/devices/{target['id']}/roster-clone", headers=self.headers,
            json={"source_device_id": source["id"]},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"games": 1, "linked": False})
        selected = self.client.get(
            f"/api/games?device_id={target['id']}&device_scope=selected",
            headers=self.headers,
        )
        self.assertEqual(selected.status_code, 200, selected.text)
        self.assertEqual([item["id"] for item in selected.json()["items"]], [game["id"]])
        devices = self.client.get("/api/devices", headers=self.headers).json()
        target_row = next(item for item in devices if item["id"] == target["id"])
        self.assertEqual(target_row["selected_games"], 1)
        self.assertIsNone(target_row["roster_group_id"])

        with self.main.db.write() as connection:
            connection.execute("DELETE FROM devices WHERE id IN (?,?)", (source["id"], target["id"]))
        for name in ("zz-existing-clone-source", "zz-existing-clone-target"):
            (self.root / "devices" / name / "roms").rmdir()
            (self.root / "devices" / name).rmdir()

    def test_device_group_crud_persists_owner_and_preserves_members(self):
        if not self.client.get("/api/games", headers=self.headers).json()["items"]:
            self.main.library.scan(force_prune=True)
        with self.main.db.connect() as connection:
            owner = connection.execute(
                "SELECT id FROM users WHERE username_normalized='group-owner-test'"
            ).fetchone()
        if owner is None:
            owner = self.main.auth.create_user(
                "group-owner-test",
                "Group Owner Test",
                "group-owner-test-password",
                ["member"],
            )
        owner_id = owner["id"]
        devices = []
        for name in ("zz-group-crud-source", "zz-group-crud-target"):
            device = self.client.post(
                "/api/devices", headers=self.headers,
                json={"name": name, "deployment_mode": "hardlink"},
            ).json()
            self.client.put(
                f"/api/devices/{device['id']}/owner", headers=self.headers,
                json={"owner_user_id": owner_id},
            )
            devices.append(device)
        game = self.client.get("/api/games", headers=self.headers).json()["items"][0]
        self.client.put(
            f"/api/devices/{devices[0]['id']}/selection", headers=self.headers,
            json={"game_id": game["id"], "selected": True},
        )

        created = self.client.post(
            "/api/device-groups", headers=self.headers,
            json={
                "name": "Owned handhelds",
                "source_device_id": devices[0]["id"],
                "member_device_ids": [devices[1]["id"]],
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        group = created.json()
        self.assertEqual(group["owner_user_id"], owner_id)
        self.assertEqual(group["games"], 1)
        listed = self.client.get("/api/device-groups", headers=self.headers)
        self.assertEqual(listed.status_code, 200, listed.text)
        listed_group = next(item for item in listed.json() if item["id"] == group["id"])
        self.assertEqual(listed_group["name"], "Owned handhelds")
        self.assertEqual(listed_group["owner_user_id"], owner_id)
        self.assertEqual({item["id"] for item in listed_group["members"]}, {item["id"] for item in devices})

        renamed = self.client.put(
            f"/api/device-groups/{group['id']}", headers=self.headers,
            json={"name": "Pocket systems"},
        )
        self.assertEqual(renamed.status_code, 200, renamed.text)
        deleted = self.client.delete(
            f"/api/device-groups/{group['id']}", headers=self.headers
        )
        self.assertEqual(deleted.status_code, 200, deleted.text)
        self.assertEqual(deleted.json(), {"deleted": True, "devices": 2})
        current = self.client.get("/api/devices", headers=self.headers).json()
        for device in devices:
            row = next(item for item in current if item["id"] == device["id"])
            self.assertIsNone(row["roster_group_id"])
            self.assertEqual(row["selected_games"], 1)

        with self.main.db.write() as connection:
            connection.execute(
                "DELETE FROM devices WHERE id IN (?,?)", (devices[0]["id"], devices[1]["id"])
            )
        for device in devices:
            (self.root / "devices" / device["name"] / "roms").rmdir()
            (self.root / "devices" / device["name"]).rmdir()

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
        self.assertEqual(self.client.get(ticket.json()["url"]).status_code, 404)

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

        bulk = self.client.get("/api/artwork/bulk", headers=self.headers)
        self.assertEqual(bulk.status_code, 200)
        self.assertFalse(bulk.json()["configured"])
        self.assertIn("missing_covers", bulk.json())
        response = self.client.post(
            "/api/artwork/scrape-all",
            headers=self.headers,
            json={"asset_mode": "cover"},
        )
        self.assertEqual(response.status_code, 400)
        self.assertNotIn("password", response.text.casefold())

    def test_artwork_assets_use_versioned_private_browser_cache(self):
        scan = self.client.post("/api/scan", headers=self.headers)
        self.assertEqual(self.wait_for_job(scan.json()["job_id"])["status"], "complete")
        game = self.client.get("/api/games", headers=self.headers).json()["items"][0]
        cover = io.BytesIO()
        Image.new("RGB", (1200, 1800), "purple").save(cover, format="JPEG", quality=90)
        payload = cover.getvalue()
        digest = hashlib.sha256(payload).hexdigest()
        asset_path = self.main.settings.media_root / str(game["id"]) / "cover.jpg"
        asset_path.parent.mkdir(parents=True, exist_ok=True)
        asset_path.write_bytes(payload)
        with self.main.db.write() as connection:
            connection.execute(
                "INSERT INTO game_assets(game_id,source,kind,media_type,local_relpath,content_type,size,sha256) "
                "VALUES(?,'test','cover','box-2D',?,'image/jpeg',?,?) "
                "ON CONFLICT(game_id,kind) DO UPDATE SET local_relpath=excluded.local_relpath,"
                "content_type=excluded.content_type,size=excluded.size,sha256=excluded.sha256",
                (
                    game["id"],
                    asset_path.relative_to(self.main.settings.media_root).as_posix(),
                    len(payload),
                    digest,
                ),
            )
            asset_id = connection.execute(
                "SELECT id FROM game_assets WHERE game_id=? AND kind='cover'", (game["id"],)
            ).fetchone()["id"]

        games = self.client.get("/api/games", headers=self.headers).json()["items"]
        cached_game = next(item for item in games if item["id"] == game["id"])
        self.assertEqual(cached_game["cover_asset_version"], digest[:16])
        response = self.client.get(
            f"/api/artwork/assets/{asset_id}?v={digest[:16]}", headers=self.headers
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, payload)
        self.assertEqual(
            response.headers["cache-control"], "private, max-age=31536000, immutable"
        )
        self.assertIn("Authorization", response.headers["vary"])
        self.assertIn("Cookie", response.headers["vary"])

        thumbnail = self.client.get(
            f"/api/artwork/thumbnails/{asset_id}?v={digest[:16]}-v1",
            headers=self.headers,
        )
        self.assertEqual(thumbnail.status_code, 200)
        self.assertEqual(thumbnail.headers["content-type"], "image/webp")
        self.assertEqual(
            thumbnail.headers["cache-control"], "private, max-age=31536000, immutable"
        )
        with Image.open(io.BytesIO(thumbnail.content)) as image:
            self.assertLessEqual(image.width, 160)
            self.assertLessEqual(image.height, 160)

        manifest = self.client.get("/api/artwork/manifest", headers=self.headers)
        self.assertEqual(manifest.status_code, 200)
        self.assertGreaterEqual(manifest.json()["covers"], 1)
        self.assertEqual(manifest.json()["thumbnail_version"], "v1")
        self.assertIn("thumbnail_cache", manifest.json())

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
                connection.execute(
                    "INSERT INTO platform_rankings(platform,rank,source,source_game_id,slug,title,score,rating,ratings_count,released,matched_game_id,match_method) "
                    "VALUES('gba',2,'rawg','rating-low','rating-low','Rating Low',91,4.3,10,'',?,'exact')",
                    (ids["Rating Low"],),
                )
                connection.execute(
                    "INSERT INTO platform_rankings(platform,rank,source,source_game_id,slug,title,score,rating,ratings_count,released,matched_game_id,match_method) "
                    "VALUES('gba',20,'rawg','rating-high','rating-high','Rating High',80,4.0,8,'',?,'exact')",
                    (ids["Rating High"],),
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
            ranked = self.client.get(
                "/api/games?platform=gba&search=Rating%20&sort=rank_asc&limit=100",
                headers=self.headers,
            ).json()["items"]
            self.assertEqual(
                [item["display_name"] for item in ranked],
                ["Rating Low", "Rating High", "Rating Unknown"],
            )
            self.assertEqual(ranked[0]["rawg_rank"], 2)
            self.assertIsNone(ranked[-1]["rawg_rank"])
            platforms = self.client.get("/api/platforms", headers=self.headers).json()
            gba = next(item for item in platforms if item["platform"] == "gba")
            self.assertGreaterEqual(gba["rated_count"], 2)
        finally:
            with self.main.db.write() as connection:
                connection.execute(
                    "DELETE FROM platform_rankings WHERE platform='gba' "
                    "AND source_game_id IN ('rating-low','rating-high')"
                )
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
            device = self.client.get("/api/devices", headers=self.headers).json()[0]
            self.main.library.device_inventory(device["id"], refresh=True)
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
            self.main.library.device_inventory(device["id"], refresh=True)
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
            # Explicit device inspection refreshes and persists actual filesystem
            # presence. General Library reads reuse that inventory without walking
            # every device directory in the request path.
            response = self.client.get(
                f"/api/games?device_id={device['id']}&device_scope=on_device"
                "&refresh_device_inventory=true",
                headers=self.headers,
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            with patch.object(
                self.main.library,
                "device_inventory",
                wraps=self.main.library.device_inventory,
            ) as inventory:
                library_game = next(
                    item for item in self.client.get(
                        "/api/games?limit=1000", headers=self.headers
                    ).json()["items"]
                    if item["id"] == game["id"]
                )
            inventory.assert_not_called()
            self.assertEqual(
                library_game["devices"],
                [{"id": device["id"], "name": device["name"], "state": "present"}],
            )
            self.assertEqual(data["total"], 1)
            self.assertEqual(data["items"][0]["device_state"], "unmanaged")
            self.assertEqual(data["device_inventory"]["present_games"], 1)
            self.assertEqual(data["device_inventory"]["unmatched_files"], 1)
            self.assertEqual(
                data["device_inventory"]["platforms"],
                [{"platform": "gba", "count": 1}],
            )
            self.assertEqual(
                data["device_inventory"]["present_platforms"],
                [{"platform": "gba", "count": 1, "bytes": len(b"test-rom")}],
            )

            selected = self.client.put(
                f"/api/devices/{device['id']}/selection",
                headers=self.headers,
                json={"game_id": game["id"], "selected": True},
            )
            self.assertEqual(selected.status_code, 200)
            with patch.object(
                self.main.library,
                "device_inventory",
                wraps=self.main.library.device_inventory,
            ) as inventory:
                updated = self.client.get(
                    f"/api/games?device_id={device['id']}&device_scope=on_device",
                    headers=self.headers,
                ).json()
            inventory.assert_called_once_with(device["id"], refresh=False)
            self.assertEqual(updated["items"][0]["device_state"], "pending_update")
            self.assertEqual(updated["device_inventory"]["changes"], 1)
            pending = self.client.get(
                f"/api/games?device_id={device['id']}&device_scope=changes",
                headers=self.headers,
            ).json()
            self.assertEqual(
                pending["device_inventory"]["platforms"],
                [{"platform": "gba", "count": 1}],
            )
        finally:
            self.client.put(
                f"/api/devices/{device['id']}/selection",
                headers=self.headers,
                json={"game_id": game["id"], "selected": False},
            )
            target.unlink(missing_ok=True)
            unknown.unlink(missing_ok=True)

    def test_device_owner_can_provision_syncthing_share_without_supplying_a_path(self):
        device = self.client.get("/api/devices", headers=self.headers).json()[0]
        result = {
            "device_id": "REMOTE-DEVICE-ID",
            "folder_id": f"rommates-device-{device['id']}",
            "folder_path": "/media/Emulation/devices/handheld/roms",
            "created": True,
        }
        try:
            with patch.object(
                self.main.syncthing, "share_device_folder", return_value=result
            ) as share:
                response = self.client.post(
                    f"/api/devices/{device['id']}/syncthing-share",
                    headers=self.headers,
                    json={"device_id": "REMOTE-DEVICE-ID"},
                )
            self.assertEqual(response.status_code, 200, response.text)
            share.assert_called_once_with(
                device["name"],
                "REMOTE-DEVICE-ID",
                folder_id=f"rommates-device-{device['id']}",
            )
            with self.main.db.connect() as connection:
                stored = connection.execute(
                    "SELECT syncthing_device_id,syncthing_folder_id,syncthing_ready_at "
                    "FROM devices WHERE id=?",
                    (device["id"],),
                ).fetchone()
            self.assertEqual(stored["syncthing_device_id"], "REMOTE-DEVICE-ID")
            self.assertEqual(stored["syncthing_folder_id"], result["folder_id"])
            self.assertIsNotNone(stored["syncthing_ready_at"])
        finally:
            with self.main.db.write() as connection:
                connection.execute(
                    "UPDATE devices SET syncthing_device_id='',syncthing_folder_id='',"
                    "syncthing_ready_at=NULL,syncthing_ready_by=NULL WHERE id=?",
                    (device["id"],),
                )

    def test_device_deployment_mode_is_always_hardlink(self):
        device = self.client.get("/api/devices", headers=self.headers).json()[0]
        rejected = self.client.put(
            f"/api/devices/{device['id']}/deployment-mode",
            headers=self.headers,
            json={"mode": "copy"},
        )
        self.assertEqual(rejected.status_code, 422)
        preview = self.client.get(
            f"/api/devices/{device['id']}/preview", headers=self.headers
        )
        self.assertEqual(preview.status_code, 200)
        self.assertEqual(preview.json()["device"]["deployment_mode"], "hardlink")
        self.assertIn("conversions", preview.json())
        self.assertIn("hardlinked", preview.json())
        self.assertIn("copied", preview.json())
        self.assertEqual(
            set(preview.json()["changes"]),
            {"additions", "conversions", "removals"},
        )

    def test_device_preview_lists_affected_roms(self):
        scan = self.client.post("/api/scan", headers=self.headers)
        self.assertEqual(self.wait_for_job(scan.json()["job_id"])["status"], "complete")
        game = next(
            item for item in self.client.get(
                "/api/games?limit=1000", headers=self.headers
            ).json()["items"]
            if item["display_name"] == "Test Game"
        )
        device = self.client.get("/api/devices", headers=self.headers).json()[0]
        self.client.put(
            f"/api/devices/{device['id']}/selection",
            headers=self.headers,
            json={"game_id": game["id"], "selected": True},
        )
        try:
            preview = self.client.get(
                f"/api/devices/{device['id']}/preview", headers=self.headers
            ).json()
            self.assertEqual(preview["additions"], 1)
            self.assertEqual(
                preview["changes"]["additions"],
                [{
                    "id": game["id"],
                    "display_name": "Test Game",
                    "platform": "gba",
                    "files": 1,
                    "bytes": len(b"test-rom"),
                }],
            )
            self.assertEqual(preview["current_rom_bytes"], 0)
            self.assertEqual(preview["desired_rom_bytes"], len(b"test-rom"))
            self.assertEqual(preview["projected_rom_bytes"], len(b"test-rom"))
            self.assertEqual(preview["storage_capacity_bytes"], 0)
            self.assertFalse(preview["over_capacity"])
        finally:
            self.client.put(
                f"/api/devices/{device['id']}/selection",
                headers=self.headers,
                json={"game_id": game["id"], "selected": False},
            )

    def test_device_storage_capacity_and_projected_rom_usage(self):
        scan = self.client.post("/api/scan", headers=self.headers)
        self.assertEqual(self.wait_for_job(scan.json()["job_id"])["status"], "complete")
        game = next(
            item for item in self.client.get(
                "/api/games?limit=1000", headers=self.headers
            ).json()["items"]
            if item["display_name"] == "Test Game"
        )
        device = self.client.get("/api/devices", headers=self.headers).json()[0]
        unknown = self.root / "devices" / device["path"] / "roms" / "gba" / "Unknown.gba"
        unknown.parent.mkdir(parents=True, exist_ok=True)
        unknown.write_bytes(b"unknown-rom")
        try:
            inventory = self.client.get(
                f"/api/games?device_id={device['id']}&device_scope=all"
                "&refresh_device_inventory=true",
                headers=self.headers,
            )
            self.assertEqual(inventory.status_code, 200, inventory.text)
            self.assertEqual(inventory.json()["device_inventory"]["bytes"], len(b"unknown-rom"))

            capacity = self.client.put(
                f"/api/devices/{device['id']}/storage-capacity",
                headers=self.headers,
                json={"storage_capacity_bytes": 12},
            )
            self.assertEqual(capacity.status_code, 200, capacity.text)
            self.assertEqual(capacity.json()["storage_capacity_bytes"], 12)
            self.client.put(
                f"/api/devices/{device['id']}/selection",
                headers=self.headers,
                json={"game_id": game["id"], "selected": True},
            )

            preview = self.client.get(
                f"/api/devices/{device['id']}/preview", headers=self.headers
            ).json()
            self.assertEqual(preview["current_rom_bytes"], len(b"unknown-rom"))
            self.assertEqual(preview["unmanaged_rom_bytes"], len(b"unknown-rom"))
            self.assertEqual(
                preview["projected_rom_bytes"], len(b"unknown-rom") + len(b"test-rom")
            )
            self.assertEqual(preview["storage_capacity_bytes"], 12)
            self.assertTrue(preview["over_capacity"])

            rejected = self.client.put(
                f"/api/devices/{device['id']}/storage-capacity",
                headers=self.headers,
                json={"storage_capacity_bytes": -1},
            )
            self.assertEqual(rejected.status_code, 422)
        finally:
            self.client.put(
                f"/api/devices/{device['id']}/selection",
                headers=self.headers,
                json={"game_id": game["id"], "selected": False},
            )
            self.client.put(
                f"/api/devices/{device['id']}/storage-capacity",
                headers=self.headers,
                json={"storage_capacity_bytes": 0},
            )
            unknown.unlink(missing_ok=True)

    def test_device_changes_can_be_discarded_without_touching_files(self):
        scan = self.client.post("/api/scan", headers=self.headers)
        self.assertEqual(self.wait_for_job(scan.json()["job_id"])["status"], "complete")
        game = next(
            item for item in self.client.get(
                "/api/games?limit=1000", headers=self.headers
            ).json()["items"]
            if item["display_name"] == "Test Game"
        )
        device = self.client.get("/api/devices", headers=self.headers).json()[0]
        self.client.put(
            f"/api/devices/{device['id']}/selection",
            headers=self.headers,
            json={"game_id": game["id"], "selected": True},
        )

        response = self.client.post(
            f"/api/devices/{device['id']}/discard-changes", headers=self.headers
        )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["devices"], 1)
        self.assertEqual(response.json()["games"], 0)
        preview = self.client.get(
            f"/api/devices/{device['id']}/preview", headers=self.headers
        ).json()
        self.assertEqual(preview["additions"], 0)
        self.assertEqual(preview["removals"], 0)
        self.assertFalse(
            (self.root / "devices" / device["path"] / "roms" / game["primary_relpath"]).exists()
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
        self.assertTrue(overview["inventory"]["available"])
        self.assertIn("emulators", overview["inventory"])

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

    def test_save_conflict_can_be_reviewed_and_resolved(self):
        current = self.root / "saves/retroarch/mGBA/Conflict API Test.srm"
        conflict = self.root / (
            "saves/retroarch/mGBA/"
            "Conflict API Test.sync-conflict-20260829-183000-ABC1234.srm"
        )
        current.parent.mkdir(parents=True, exist_ok=True)
        current.write_bytes(b"current-progress")
        conflict.write_bytes(b"other-progress")

        report = self.client.get(
            "/api/saves/conflicts?search=Conflict%20API%20Test", headers=self.headers
        )
        self.assertEqual(report.status_code, 200)
        item = report.json()["items"][0]
        response = self.client.post(
            "/api/saves/conflicts/resolve",
            headers=self.headers,
            json={
                "conflict_relpath": item["conflict_relpath"],
                "decision": "current",
                "expected_canonical_sha256": item["canonical_sha256"],
                "expected_conflict_sha256": item["conflict_sha256"],
                "device_id": item["device_id"],
                "device_name": "Brother's Retroid",
            },
        )
        self.assertEqual(response.status_code, 202)
        job = self.wait_for_job(response.json()["job_id"])
        self.assertEqual(job["status"], "complete")
        self.assertEqual(current.read_bytes(), b"current-progress")
        self.assertFalse(conflict.exists())
        self.assertIn("safety_snapshot_id", job["result"])

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
        self.assertIn("bytes_read", report["telemetry"])
        gba_progress = report["telemetry"]["platforms"]["gba"]
        self.assertEqual(gba_progress["processed_files"], 1)
        self.assertEqual(
            gba_progress["processed_hash_files"] + gba_progress["processed_cached_files"],
            1,
        )

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
