from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from mcp import Client

from app.config import Settings
from app.db import Database
from app.library import LibraryError, LibraryService
from app.mcp_server import RommatesMCPService, create_mcp_server


class MCPServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = Settings(
            library_root=root / "roms",
            devices_root=root / "devices",
            trash_root=root / "trash",
            database_path=root / "data/rommates.db",
            scan_on_start=False,
        )
        self.db = Database(self.settings.database_path)
        self.db.initialize()
        self.library = LibraryService(self.settings, self.db)
        self.library.prepare_roots()
        (self.settings.library_root / "gba").mkdir()
        (self.settings.library_root / "gba/Test Game.gba").write_bytes(b"test-rom")
        (self.settings.devices_root / "handheld/roms").mkdir(parents=True)
        self.library.scan()
        with self.db.connect() as connection:
            self.game_id = connection.execute("SELECT id FROM games").fetchone()["id"]
            self.device_id = connection.execute("SELECT id FROM devices").fetchone()["id"]
        self.scan_requests = 0
        self.apply_requests: list[int] = []
        self.cancel_requests: list[int] = []

        def enqueue_scan():
            self.scan_requests += 1
            return {"job_id": 91, "already_running": False}

        def enqueue_apply(device_id: int, preview_token: str):
            self.apply_requests.append(device_id)
            return {"job_id": 92}

        def cancel(job_id: int):
            self.cancel_requests.append(job_id)
            return {"job_id": job_id, "status": "cancelling"}

        self.service = RommatesMCPService(
            self.db, self.library, enqueue_scan, enqueue_apply, cancel
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_search_and_bundle_tools_use_indexed_state(self):
        search = self.service.search_games("Test", "gba")
        self.assertEqual(search["total"], 1)
        self.assertEqual(search["items"][0]["id"], self.game_id)
        bundle = self.service.get_game_bundle(self.game_id)
        self.assertEqual(bundle["game"]["display_name"], "Test Game")
        self.assertEqual(len(bundle["files"]), 1)

    def test_device_apply_requires_current_preview_token(self):
        selected = self.service.set_device_games(self.device_id, [self.game_id], True)
        preview = selected["preview"]
        self.assertEqual(preview["selected_games"], 1)
        self.assertEqual(preview["additions"][0]["id"], self.game_id)

        self.library.set_device_deployment_mode(self.device_id, "hardlink")
        with self.assertRaisesRegex(LibraryError, "changed after it was reviewed"):
            self.service.apply_device_changes(self.device_id, preview["preview_token"])

        current = self.service.preview_device_changes(self.device_id)
        queued = self.service.apply_device_changes(self.device_id, current["preview_token"])
        self.assertEqual(queued["job_id"], 92)
        self.assertEqual(self.apply_requests, [self.device_id])

        self.library.set_selection(self.device_id, self.game_id, False)
        with self.assertRaisesRegex(LibraryError, "changed while the job was queued"):
            self.service.execute_reviewed_device_apply(
                self.device_id, current["preview_token"]
            )

    def test_mcp_protocol_exposes_bounded_tool_surface(self):
        server = create_mcp_server(self.service)

        async def exercise():
            async with Client(server) as client:
                tools = await client.list_tools()
                result = await client.call_tool("search_games", {"query": "Test"})
                return tools, result

        tools, result = asyncio.run(exercise())
        names = {tool.name for tool in tools.tools}
        self.assertEqual(
            names,
            {
                "library_status",
                "search_games",
                "get_game_bundle",
                "find_duplicates",
                "list_devices",
                "inspect_device",
                "preview_device_changes",
                "list_jobs",
                "get_job_report",
                "start_scan",
                "set_device_games",
                "apply_device_changes",
                "stop_job",
            },
        )
        apply_tool = next(tool for tool in tools.tools if tool.name == "apply_device_changes")
        self.assertTrue(apply_tool.annotations.destructive_hint)
        self.assertEqual(result.structured_content["items"][0]["display_name"], "Test Game")
