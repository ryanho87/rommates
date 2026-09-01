from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import Settings


class SyncthingService:
    """Cached Syncthing status plus bounded, best-effort folder rescans."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = threading.Lock()
        self._cached_at = 0.0
        self._cached: dict[str, object] | None = None

    @property
    def configured(self) -> bool:
        return bool(self.settings.syncthing_url and self.settings.syncthing_api_key)

    def _get(self, path: str) -> Any:
        request = Request(
            f"{self.settings.syncthing_url}{path}",
            headers={
                "Accept": "application/json",
                "X-API-Key": self.settings.syncthing_api_key,
                "User-Agent": "ROMmates/0.1",
            },
            method="GET",
        )
        with urlopen(request, timeout=self.settings.syncthing_timeout_seconds) as response:
            return json.load(response)

    def _post(self, path: str) -> None:
        request = Request(
            f"{self.settings.syncthing_url}{path}",
            data=b"",
            headers={
                "Accept": "application/json",
                "X-API-Key": self.settings.syncthing_api_key,
                "User-Agent": "ROMmates/0.1",
            },
            method="POST",
        )
        with urlopen(request, timeout=self.settings.syncthing_timeout_seconds):
            pass

    @staticmethod
    def _normalized_path(value: object) -> str:
        return str(value or "").replace("\\", "/").rstrip("/").casefold()

    @staticmethod
    def _normalized_label(value: object) -> str:
        return "".join(character for character in str(value or "").casefold() if character.isalnum())

    def _folder_scan_target(
        self, folder: dict[str, Any], device_name: str
    ) -> tuple[str, str] | None:
        """Return the Syncthing folder id and optional subpath for a ROMmates device."""
        folder_id = str(folder.get("id") or "").strip()
        if not folder_id:
            return None
        folder_path = self._normalized_path(folder.get("path"))
        device_key = device_name.casefold()
        device_suffix = f"devices/{device_key}/roms"
        if folder_path.endswith(f"/{device_suffix}") or folder_path == device_suffix:
            return folder_id, ""
        if folder_path.endswith(f"/devices/{device_key}"):
            return folder_id, "roms"
        if folder_path.endswith("/devices"):
            return folder_id, f"{device_name}/roms"
        emulation_name = self.settings.devices_root.parent.name.casefold()
        if emulation_name and folder_path.endswith(f"/{emulation_name}"):
            return folder_id, f"devices/{device_name}/roms"

        expected_labels = {
            self._normalized_label(device_name),
            self._normalized_label(f"{device_name} roms"),
        }
        if self._normalized_label(folder.get("label")) in expected_labels:
            return folder_id, ""
        return None

    def rescan_device(self, device_name: str) -> dict[str, object]:
        """Ask Syncthing to rescan folders that contain one device's ROM directory.

        A deployment has already committed by the time this runs, so connectivity or
        mapping failures are reported in the job result rather than raised.
        """
        if not self.configured:
            return {
                "requested": False,
                "folders": [],
                "error": "Syncthing API is not configured",
            }
        try:
            configured_folders = self._get("/rest/config/folders")
            if not isinstance(configured_folders, list):
                raise ValueError("Syncthing returned an invalid folder list")
            targets = [
                target
                for folder in configured_folders
                if isinstance(folder, dict)
                for target in [self._folder_scan_target(folder, device_name)]
                if target is not None
            ]
            if not targets:
                return {
                    "requested": False,
                    "folders": [],
                    "error": "No Syncthing folder matched this device ROM path",
                }
            rescanned = []
            for folder_id, subpath in targets:
                parameters = {"folder": folder_id}
                if subpath:
                    parameters["sub"] = subpath
                self._post(f"/rest/db/scan?{urlencode(parameters)}")
                rescanned.append({"folder_id": folder_id, "subpath": subpath})
            return {"requested": True, "folders": rescanned, "error": None}
        except HTTPError as exc:
            error = (
                "Syncthing rejected the API key"
                if exc.code in {401, 403}
                else f"Syncthing API returned HTTP {exc.code}"
            )
        except (URLError, TimeoutError):
            error = "Syncthing could not be reached"
        except (OSError, ValueError, json.JSONDecodeError):
            error = "Syncthing returned an unreadable folder configuration"
        return {"requested": False, "folders": [], "error": error}

    @staticmethod
    def _connection_type(connection: dict[str, Any]) -> str:
        value = str(connection.get("type") or "").strip()
        if value:
            return value
        address = str(connection.get("address") or "")
        if address.startswith("relay://"):
            return "Relay"
        return "Direct" if address else ""

    @staticmethod
    def _last_seen(connection: dict[str, Any]) -> str | None:
        value = connection.get("at")
        if isinstance(value, str) and value and not value.startswith("0001-"):
            return value
        return None

    def _fetch(self) -> dict[str, object]:
        if not self.configured:
            missing = []
            if not self.settings.syncthing_url:
                missing.append("ROMMATES_SYNCTHING_URL")
            if not self.settings.syncthing_api_key:
                missing.append("ROMMATES_SYNCTHING_API_KEY")
            return {
                "configured": False,
                "available": False,
                "error": f"Set {' and '.join(missing)} to enable Syncthing status.",
                "devices": [],
                "online": 0,
                "total": 0,
                "checked_at": None,
            }

        try:
            # Keep an unavailable endpoint bounded by one timeout window rather
            # than making dashboard navigation wait for three serial timeouts.
            with ThreadPoolExecutor(max_workers=3, thread_name_prefix="syncthing-status") as executor:
                configured_future = executor.submit(self._get, "/rest/config/devices")
                connections_future = executor.submit(self._get, "/rest/system/connections")
                status_future = executor.submit(self._get, "/rest/system/status")
                configured_devices = configured_future.result()
                connection_data = connections_future.result()
                local_status = status_future.result()
            if not isinstance(configured_devices, list):
                raise ValueError("Syncthing returned an invalid device list")
            connections = connection_data.get("connections", {}) if isinstance(connection_data, dict) else {}
            if not isinstance(connections, dict):
                connections = {}
            devices = []
            for configured in configured_devices:
                if not isinstance(configured, dict):
                    continue
                device_id = str(configured.get("deviceID") or "")
                connection = connections.get(device_id, {})
                if not isinstance(connection, dict):
                    connection = {}
                connected = bool(connection.get("connected"))
                devices.append(
                    {
                        "device_id": device_id,
                        "name": str(configured.get("name") or device_id[:7] or "Unknown device"),
                        "connected": connected,
                        "paused": bool(configured.get("paused") or connection.get("paused")),
                        "address": str(connection.get("address") or ""),
                        "connection_type": self._connection_type(connection),
                        "client_version": str(connection.get("clientVersion") or ""),
                        "last_seen": self._last_seen(connection),
                    }
                )
            devices.sort(key=lambda item: (not bool(item["connected"]), str(item["name"]).casefold()))
            checked_at = datetime.now(timezone.utc).isoformat()
            return {
                "configured": True,
                "available": True,
                "error": None,
                "local_device_id": str(local_status.get("myID") or "") if isinstance(local_status, dict) else "",
                "devices": devices,
                "online": sum(1 for item in devices if item["connected"]),
                "total": len(devices),
                "checked_at": checked_at,
            }
        except HTTPError as exc:
            if exc.code in {401, 403}:
                error = "Syncthing rejected the API key."
            else:
                error = f"Syncthing API returned HTTP {exc.code}."
        except (URLError, TimeoutError):
            error = "Syncthing could not be reached."
        except (OSError, ValueError, json.JSONDecodeError):
            error = "Syncthing returned an unreadable response."
        return {
            "configured": True,
            "available": False,
            "error": error,
            "devices": [],
            "online": 0,
            "total": 0,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        }

    def status(self, refresh: bool = False) -> dict[str, object]:
        now = time.monotonic()
        with self._lock:
            if (
                not refresh
                and self._cached is not None
                and now - self._cached_at < self.settings.syncthing_cache_seconds
            ):
                return self._cached
            self._cached = self._fetch()
            self._cached_at = time.monotonic()
            return self._cached

    def peek(self) -> dict[str, object]:
        """Return cached state without ever performing network I/O."""
        if not self.configured:
            return self.status()
        with self._lock:
            if self._cached is None:
                return {
                    "configured": True,
                    "available": False,
                    "error": None,
                    "devices": [],
                    "online": 0,
                    "total": 0,
                    "checked_at": None,
                    "checking": True,
                    "stale": False,
                }
            payload = dict(self._cached)
            payload["checking"] = False
            payload["stale"] = (
                time.monotonic() - self._cached_at >= self.settings.syncthing_cache_seconds
            )
            return payload
