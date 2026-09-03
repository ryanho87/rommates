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

    def _send_json(self, path: str, payload: dict[str, Any], method: str = "POST") -> Any:
        request = Request(
            f"{self.settings.syncthing_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-API-Key": self.settings.syncthing_api_key,
                "User-Agent": "ROMmates/0.1",
            },
            method=method,
        )
        with urlopen(request, timeout=self.settings.syncthing_timeout_seconds) as response:
            body = response.read()
        return json.loads(body) if body else None

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

    def _syncthing_device_path(
        self, device_name: str, configured_folders: list[dict[str, Any]]
    ) -> str:
        """Resolve the device path in Syncthing's filesystem namespace."""
        if self.settings.syncthing_devices_root is not None:
            return str(self.settings.syncthing_devices_root / device_name / "roms")
        for folder in configured_folders:
            raw_path = str(folder.get("path") or "").replace("\\", "/").rstrip("/")
            folded = raw_path.casefold()
            marker = "/devices/"
            marker_index = folded.rfind(marker)
            if marker_index >= 0 and folded.endswith("/roms"):
                return f"{raw_path[:marker_index]}/devices/{device_name}/roms"
            if folded.endswith("/devices"):
                return f"{raw_path}/{device_name}/roms"
            if folded.endswith("/emulation"):
                return f"{raw_path}/devices/{device_name}/roms"
        return str(self.settings.devices_root / device_name / "roms")

    @staticmethod
    def _folder_remote_ids(folder: dict[str, Any], local_device_id: str) -> list[str]:
        result = []
        for item in folder.get("devices", []):
            if not isinstance(item, dict):
                continue
            device_id = str(item.get("deviceID") or "").strip()
            if device_id and device_id != local_device_id:
                result.append(device_id)
        return result

    def share_device_folder(
        self,
        device_name: str,
        remote_device_id: str,
        *,
        folder_id: str,
    ) -> dict[str, object]:
        """Create (or update) a Syncthing folder and share it with one device."""
        if not self.configured:
            raise ValueError("Syncthing API is not configured")
        candidate = remote_device_id.strip()
        if not candidate:
            raise ValueError("Enter a Syncthing device ID")
        try:
            validated = self._get(f"/rest/svc/deviceid?{urlencode({'id': candidate})}")
            normalized_id = str(validated.get("id") or "") if isinstance(validated, dict) else ""
            if not normalized_id:
                raise ValueError("Syncthing did not recognize that device ID")
            with ThreadPoolExecutor(max_workers=3, thread_name_prefix="syncthing-share") as executor:
                folders_future = executor.submit(self._get, "/rest/config/folders")
                devices_future = executor.submit(self._get, "/rest/config/devices")
                status_future = executor.submit(self._get, "/rest/system/status")
                configured_folders = folders_future.result()
                configured_devices = devices_future.result()
                local_status = status_future.result()
            if not isinstance(configured_folders, list) or not isinstance(configured_devices, list):
                raise ValueError("Syncthing returned an invalid configuration")
            configured_folders = [item for item in configured_folders if isinstance(item, dict)]
            configured_devices = [item for item in configured_devices if isinstance(item, dict)]
            local_device_id = str(local_status.get("myID") or "") if isinstance(local_status, dict) else ""
            if normalized_id == local_device_id:
                raise ValueError("Use the handheld's Syncthing device ID, not this server's ID")

            if not any(str(item.get("deviceID") or "") == normalized_id for item in configured_devices):
                template = self._get("/rest/config/defaults/device")
                if not isinstance(template, dict):
                    raise ValueError("Syncthing returned an invalid device template")
                template.update({"deviceID": normalized_id, "name": device_name})
                self._send_json("/rest/config/devices", template)

            folder = next(
                (
                    item
                    for item in configured_folders
                    if self._folder_scan_target(item, device_name) is not None
                    or str(item.get("id") or "") == folder_id
                ),
                None,
            )
            created = folder is None
            if folder is None:
                template = self._get("/rest/config/defaults/folder")
                if not isinstance(template, dict):
                    raise ValueError("Syncthing returned an invalid folder template")
                folder = template
                folder.update(
                    {
                        "id": folder_id,
                        "label": f"{device_name} ROMs",
                        "path": self._syncthing_device_path(device_name, configured_folders),
                    }
                )
            folder_devices = [item for item in folder.get("devices", []) if isinstance(item, dict)]
            if local_device_id and not any(
                str(item.get("deviceID") or "") == local_device_id for item in folder_devices
            ):
                folder_devices.append({"deviceID": local_device_id})
            if not any(str(item.get("deviceID") or "") == normalized_id for item in folder_devices):
                folder_devices.append({"deviceID": normalized_id})
            folder["devices"] = folder_devices
            self._send_json("/rest/config/folders", folder)
            self._post(f"/rest/db/scan?{urlencode({'folder': str(folder['id'])})}")
            with self._lock:
                self._cached = None
                self._cached_at = 0.0
            return {
                "device_id": normalized_id,
                "folder_id": str(folder["id"]),
                "folder_path": str(folder.get("path") or ""),
                "created": created,
            }
        except HTTPError as exc:
            if exc.code in {401, 403}:
                raise ValueError("Syncthing rejected the configured API key") from exc
            raise ValueError(f"Syncthing API returned HTTP {exc.code}") from exc
        except (URLError, TimeoutError) as exc:
            raise ValueError("Syncthing could not be reached") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("Syncthing returned an unreadable response") from exc

    def device_sync_status(
        self,
        device_name: str,
        *,
        remote_device_id: str = "",
        folder_id: str = "",
    ) -> dict[str, object]:
        """Return connection, completion, and last completed activity for one device."""
        if not self.configured:
            return {"configured": False, "linked": False, "status": "Not configured", "last_sync": None}
        try:
            folders = self._get("/rest/config/folders")
            if not isinstance(folders, list):
                raise ValueError("Invalid folder list")
            folder = next(
                (
                    item for item in folders if isinstance(item, dict) and (
                        (folder_id and str(item.get("id") or "") == folder_id)
                        or self._folder_scan_target(item, device_name) is not None
                    )
                ),
                None,
            )
            if not folder:
                return {"configured": True, "linked": False, "status": "Setup needed", "last_sync": None}
            service_status = self.status()
            local_device_id = str(service_status.get("local_device_id") or "")
            candidate_ids = self._folder_remote_ids(folder, local_device_id)
            resolved_id = remote_device_id if remote_device_id in candidate_ids else ""
            if not resolved_id and len(candidate_ids) == 1:
                resolved_id = candidate_ids[0]
            if not resolved_id:
                return {
                    "configured": True,
                    "linked": False,
                    "folder_id": str(folder.get("id") or ""),
                    "status": "Choose Syncthing device",
                    "last_sync": None,
                }
            devices = {
                str(item.get("device_id") or ""): item
                for item in service_status.get("devices", [])
                if isinstance(item, dict)
            }
            remote = devices.get(resolved_id, {})
            parameters = urlencode({"folder": str(folder.get("id") or ""), "device": resolved_id})
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="syncthing-device") as executor:
                completion_future = executor.submit(self._get, f"/rest/db/completion?{parameters}")
                folder_status_future = executor.submit(
                    self._get, f"/rest/db/status?{urlencode({'folder': str(folder.get('id') or '')})}"
                )
                completion = completion_future.result()
                folder_status = folder_status_future.result()
            percentage = float(completion.get("completion") or 0) if isinstance(completion, dict) else 0.0
            connected = bool(remote.get("connected"))
            need_bytes = int(completion.get("needBytes") or 0) if isinstance(completion, dict) else 0
            need_items = int(completion.get("needItems") or 0) if isinstance(completion, dict) else 0
            need_deletes = int(completion.get("needDeletes") or 0) if isinstance(completion, dict) else 0
            remote_state = str(completion.get("remoteState") or "") if isinstance(completion, dict) else ""
            sequence = int(completion.get("sequence") or 0) if isinstance(completion, dict) else 0
            synced = percentage >= 99.999 and not (need_bytes or need_items or need_deletes)
            status_label = "Up to date" if synced else f"Syncing · {percentage:.0f}%" if connected else "Offline"
            last_sync = None
            if synced and isinstance(folder_status, dict):
                last_sync = folder_status.get("stateChanged")
            return {
                "configured": True,
                "linked": True,
                "device_id": resolved_id,
                "folder_id": str(folder.get("id") or ""),
                "connected": connected,
                "completion": percentage,
                "need_bytes": need_bytes,
                "need_items": need_items,
                "need_deletes": need_deletes,
                "remote_state": remote_state,
                "sequence": sequence,
                "folder_state": str(folder_status.get("state") or "") if isinstance(folder_status, dict) else "",
                "status": status_label,
                "last_sync": last_sync,
            }
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
            return {"configured": True, "linked": False, "status": "Status unavailable", "last_sync": None}

    def folder_sequence(self, folder_id: str) -> int:
        """Return the local index sequence that a remote device must acknowledge."""
        if not self.configured or not folder_id:
            return 0
        try:
            status = self._get(f"/rest/db/status?{urlencode({'folder': folder_id})}")
            return int(status.get("sequence") or 0) if isinstance(status, dict) else 0
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
            return 0

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
