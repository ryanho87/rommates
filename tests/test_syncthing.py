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

    def test_share_device_folder_adds_remote_to_existing_folder(self):
        remote_id = "AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF-GGGGGGG-HHHHHHH"
        writes = []

        def fake_urlopen(request, timeout):
            path = request.full_url.removeprefix("http://syncthing:8384")
            if request.get_method() == "GET":
                payload = {
                    f"/rest/svc/deviceid?id={remote_id}": {"id": remote_id},
                    "/rest/config/folders": [
                        {
                            "id": "retroid-roms",
                            "label": "Retroid Pocket 5 ROMs",
                            "path": "/media/Emulation/devices/retroid-pocket-5/roms",
                            "devices": [{"deviceID": "NUC-ID"}],
                        }
                    ],
                    "/rest/config/devices": [{"deviceID": remote_id, "name": "Retroid"}],
                    "/rest/system/status": {"myID": "NUC-ID"},
                }[path]
                return FakeResponse(json.dumps(payload).encode())
            writes.append((request.get_method(), path, json.loads(request.data) if request.data else None))
            return FakeResponse(b"")

        service = SyncthingService(self.settings(devices_root=Path("/emulation/devices")))
        with patch("app.syncthing.urlopen", side_effect=fake_urlopen):
            result = service.share_device_folder(
                "retroid-pocket-5", remote_id, folder_id="rommates-device-5"
            )

        self.assertFalse(result["created"])
        self.assertEqual(result["folder_id"], "retroid-roms")
        folder_write = next(item for item in writes if item[1] == "/rest/config/folders")
        self.assertIn({"deviceID": remote_id}, folder_write[2]["devices"])
        self.assertTrue(any(item[1].startswith("/rest/db/scan?") for item in writes))

    def test_share_device_folder_creates_folder_in_inferred_syncthing_namespace(self):
        remote_id = "AAAAAAA-BBBBBBB-CCCCCCC-DDDDDDD-EEEEEEE-FFFFFFF-GGGGGGG-HHHHHHH"
        writes = []

        def fake_urlopen(request, timeout):
            path = request.full_url.removeprefix("http://syncthing:8384")
            if request.get_method() == "GET":
                payloads = {
                    f"/rest/svc/deviceid?id={remote_id}": {"id": remote_id},
                    "/rest/config/folders": [
                        {
                            "id": "rotate-roms",
                            "path": "/media/Emulation/devices/rg-rotate/roms",
                        }
                    ],
                    "/rest/config/devices": [],
                    "/rest/system/status": {"myID": "NUC-ID"},
                    "/rest/config/defaults/device": {"addresses": ["dynamic"]},
                    "/rest/config/defaults/folder": {"type": "sendreceive", "devices": []},
                }
                return FakeResponse(json.dumps(payloads[path]).encode())
            writes.append((path, json.loads(request.data) if request.data else None))
            return FakeResponse(b"")

        service = SyncthingService(self.settings(devices_root=Path("/emulation/devices")))
        with patch("app.syncthing.urlopen", side_effect=fake_urlopen):
            result = service.share_device_folder(
                "new-handheld", remote_id, folder_id="rommates-device-12"
            )

        self.assertTrue(result["created"])
        self.assertEqual(result["folder_path"], "/media/Emulation/devices/new-handheld/roms")
        folder_write = next(payload for path, payload in writes if path == "/rest/config/folders")
        self.assertEqual(folder_write["id"], "rommates-device-12")
        self.assertIn({"deviceID": remote_id}, folder_write["devices"])

    def test_device_sync_status_reports_completion_and_last_sync(self):
        remote_id = "REMOTE-ID"
        service = SyncthingService(self.settings())
        payloads = {
            "/rest/config/folders": [
                {
                    "id": "odin-roms",
                    "path": "/devices/odin/roms",
                    "devices": [{"deviceID": "NUC-ID"}, {"deviceID": remote_id}],
                }
            ],
            f"/rest/db/completion?folder=odin-roms&device={remote_id}": {
                "completion": 100,
                "needBytes": 0,
                "needItems": 0,
                "needDeletes": 0,
                "remoteState": "valid",
                "sequence": 42,
            },
            "/rest/db/status?folder=odin-roms": {
                "state": "idle",
                "stateChanged": "2026-09-02T12:00:00Z",
            },
        }

        def fake_get(path):
            return payloads[path]

        with patch.object(service, "_get", side_effect=fake_get), patch.object(
            service,
            "status",
            return_value={
                "local_device_id": "NUC-ID",
                "devices": [{"device_id": remote_id, "connected": True}],
            },
        ):
            status = service.device_sync_status("odin")

        self.assertTrue(status["linked"])
        self.assertEqual(status["status"], "Up to date")
        self.assertEqual(status["last_sync"], "2026-09-02T12:00:00Z")
        self.assertEqual(status["sequence"], 42)
        self.assertEqual(status["remote_state"], "valid")

    def test_device_sync_status_never_rounds_incomplete_work_to_100_percent(self):
        remote_id = "REMOTE-ID"
        service = SyncthingService(self.settings())
        payloads = {
            "/rest/config/folders": [{
                "id": "odin-roms",
                "path": "/devices/odin/roms",
                "devices": [{"deviceID": "NUC-ID"}, {"deviceID": remote_id}],
            }],
            f"/rest/db/completion?folder=odin-roms&device={remote_id}": {
                "completion": 99.9999,
                "needBytes": 23,
                "needItems": 1,
                "needDeletes": 0,
                "remoteState": "valid",
                "sequence": 42,
            },
            "/rest/db/status?folder=odin-roms": {
                "state": "idle",
                "stateChanged": "2026-09-02T12:00:00Z",
            },
        }

        with patch.object(service, "_get", side_effect=lambda path: payloads[path]), patch.object(
            service,
            "status",
            return_value={
                "local_device_id": "NUC-ID",
                "devices": [{"device_id": remote_id, "connected": True}],
            },
        ):
            status = service.device_sync_status("odin")

        self.assertEqual(status["status"], "Syncing · 99%")
        self.assertEqual(status["need_items"], 1)
        self.assertEqual(status["need_bytes"], 23)

    def test_folder_sequence_reads_local_index_checkpoint(self):
        service = SyncthingService(self.settings())
        with patch.object(service, "_get", return_value={"sequence": 73}) as get:
            self.assertEqual(service.folder_sequence("odin-roms"), 73)
        get.assert_called_once_with("/rest/db/status?folder=odin-roms")


if __name__ == "__main__":
    unittest.main()
