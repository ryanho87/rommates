from __future__ import annotations

import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.syncthing import SyncthingService


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class SyncthingServiceTests(unittest.TestCase):
    def settings(self, **overrides):
        values = {
            "library_root": Path("/roms"),
            "devices_root": Path("/devices"),
            "trash_root": Path("/trash"),
            "database_path": Path("/data/db"),
            "syncthing_url": "http://syncthing:8384",
            "syncthing_api_key": "secret-api-key",
            "syncthing_cache_seconds": 30,
        }
        values.update(overrides)
        return Settings(**values)

    def test_unconfigured_status_explains_missing_values(self):
        status = SyncthingService(
            self.settings(syncthing_url="", syncthing_api_key="")
        ).status()
        self.assertFalse(status["configured"])
        self.assertIn("ROMMATES_SYNCTHING_URL", status["error"])
        self.assertIn("ROMMATES_SYNCTHING_API_KEY", status["error"])

    def test_reports_and_caches_remote_device_connections(self):
        payloads = {
            "/rest/config/devices": [
                {"deviceID": "OFFLINE-ID", "name": "Odin 2", "paused": False},
                {"deviceID": "ONLINE-ID", "name": "Retroid Pocket 5", "paused": False},
            ],
            "/rest/system/connections": {
                "connections": {
                    "ONLINE-ID": {
                        "connected": True,
                        "address": "tcp://10.0.0.2:22000",
                        "type": "TCP WAN",
                        "clientVersion": "v2.1.3",
                        "at": "2026-08-29T12:00:00Z",
                    },
                    "OFFLINE-ID": {"connected": False, "at": "2026-08-28T12:00:00Z"},
                }
            },
            "/rest/system/status": {"myID": "NUC-ID"},
        }
        calls = []

        def fake_urlopen(request, timeout):
            calls.append((request.full_url, request.headers.get("X-api-key"), timeout))
            path = request.full_url.removeprefix("http://syncthing:8384")
            return FakeResponse(json.dumps(payloads[path]).encode())

        service = SyncthingService(self.settings())
        with patch("app.syncthing.urlopen", side_effect=fake_urlopen):
            first = service.status()
            second = service.status()

        self.assertIs(first, second)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(call[1] == "secret-api-key" for call in calls))
        self.assertTrue(first["available"])
        self.assertEqual(first["online"], 1)
        self.assertEqual(first["total"], 2)
        self.assertEqual(first["local_device_id"], "NUC-ID")
        self.assertEqual(first["devices"][0]["name"], "Retroid Pocket 5")
        self.assertTrue(first["devices"][0]["connected"])

    def test_peek_never_performs_network_io(self):
        service = SyncthingService(self.settings())
        with patch("app.syncthing.urlopen") as mocked:
            status = service.peek()
        mocked.assert_not_called()
        self.assertTrue(status["checking"])
        self.assertFalse(status["available"])

    def test_rescan_device_matches_device_folder_and_requests_scan(self):
        calls = []

        def fake_urlopen(request, timeout):
            calls.append((request.get_method(), request.full_url, timeout))
            if request.get_method() == "GET":
                return FakeResponse(
                    json.dumps(
                        [
                            {
                                "id": "retroid-roms",
                                "label": "Retroid Pocket 5 ROMs",
                                "path": "/media/Emulation/devices/retroid-pocket-5/roms",
                            }
                        ]
                    ).encode()
                )
            return FakeResponse(b"")

        service = SyncthingService(self.settings())
        with patch("app.syncthing.urlopen", side_effect=fake_urlopen):
            result = service.rescan_device("retroid-pocket-5")

        self.assertTrue(result["requested"])
        self.assertEqual(result["folders"], [{"folder_id": "retroid-roms", "subpath": ""}])
        self.assertEqual(calls[1][0], "POST")
        self.assertIn("/rest/db/scan?folder=retroid-roms", calls[1][1])

    def test_rescan_device_can_target_a_subpath_of_emulation_share(self):
        def fake_urlopen(request, timeout):
            if request.get_method() == "GET":
                return FakeResponse(
                    json.dumps([{"id": "emulation", "path": "/media/Emulation"}]).encode()
                )
            self.assertIn("sub=devices%2Fodin2%2Froms", request.full_url)
            return FakeResponse(b"")

        service = SyncthingService(self.settings(devices_root=Path("/emulation/devices")))
        with patch("app.syncthing.urlopen", side_effect=fake_urlopen):
            result = service.rescan_device("odin2")

        self.assertTrue(result["requested"])
        self.assertEqual(result["folders"][0]["subpath"], "devices/odin2/roms")

    def test_rescan_device_is_best_effort_when_no_folder_matches(self):
        service = SyncthingService(self.settings())
        with patch(
            "app.syncthing.urlopen",
            return_value=FakeResponse(json.dumps([{"id": "saves", "path": "/saves"}]).encode()),
        ):
            result = service.rescan_device("odin2")
        self.assertFalse(result["requested"])
        self.assertIn("No Syncthing folder matched", result["error"])


if __name__ == "__main__":
    unittest.main()
