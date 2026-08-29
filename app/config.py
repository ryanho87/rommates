from __future__ import annotations

import os
import json
from dataclasses import dataclass
from pathlib import Path


DEFAULT_EXTENSIONS = frozenset(
    {
        ".7z", ".a26", ".a52", ".a78", ".bin", ".chd", ".col", ".cue",
        ".3ds", ".cci", ".d64", ".fds", ".gb", ".gba", ".gbc", ".gg", ".iso", ".lnx",
        ".gdi", ".m3u", ".md", ".n64", ".nds", ".nes", ".ngc", ".pbp", ".pce",
        ".nsp", ".rvz", ".sfc", ".sg", ".smc", ".sms", ".swc", ".v64", ".vpk", ".wad",
        ".wbfs", ".ws", ".wsc", ".xci", ".z64", ".zip",
    }
)


def _env(name: str, legacy_name: str, default: str) -> str:
    value = os.getenv(name)
    if value is not None:
        return value
    return os.getenv(legacy_name, default)


def _bool_env(name: str, legacy_name: str, default: bool) -> bool:
    return _env(name, legacy_name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


def _float_env(name: str, legacy_name: str, default: float) -> float:
    try:
        value = float(_env(name, legacy_name, "").strip() or default)
    except ValueError:
        return default
    return min(max(value, 0.0), 1.0)


def _int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)).strip())
    except ValueError:
        return default
    return min(max(value, minimum), maximum)


def _number_env(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)).strip())
    except ValueError:
        return default
    return min(max(value, minimum), maximum)


@dataclass(frozen=True)
class Settings:
    library_root: Path
    devices_root: Path
    trash_root: Path
    database_path: Path
    scan_on_start: bool = True
    access_token: str = ""
    # Deliberate opt-out for instances already behind an authenticated reverse proxy.
    # Without it a missing token fails startup instead of silently opening the API.
    allow_anonymous: bool = False
    require_existing_roots: bool = False
    extensions: frozenset[str] = DEFAULT_EXTENSIONS
    # These platforms store one game as a directory tree rather than one launch
    # file. Each immediate child directory is indexed as a complete bundle.
    folder_bundle_platforms: frozenset[str] = frozenset({"ps3", "wiiu"})
    # New or changed files above this size are indexed structurally instead of
    # blocking every scan on a full content read. A valid cached hash is retained.
    # Set to 0 to restore legacy full hashing for every file.
    hash_max_bytes: int = 512 * 1024 * 1024
    # Largest share of the indexed catalog a single scan may prune without an
    # explicit confirmation. Guards against an unmounted library root cascading
    # into every device selection.
    scan_prune_limit: float = 0.5
    saves_root: Path = Path("/saves")
    snapshots_root: Path = Path("/snapshots")
    save_snapshot_interval_minutes: int = 360
    save_snapshot_quiet_seconds: int = 2
    save_retention_recent: int = 24
    save_retention_daily: int = 30
    save_retention_weekly: int = 12
    save_retention_monthly: int = 12
    media_root: Path = Path("/data/media")
    upload_root: Path = Path("/emulation/.rommates-uploads")
    upload_max_bytes: int = 128 * 1024 * 1024 * 1024
    upload_chunk_bytes: int = 8 * 1024 * 1024
    upload_expiry_hours: int = 24
    download_ticket_seconds: int = 300
    screenscraper_dev_id: str = ""
    screenscraper_dev_password: str = ""
    screenscraper_softname: str = "ROMmates"
    screenscraper_user: str = ""
    screenscraper_password: str = ""
    screenscraper_system_map: dict[str, int] | None = None
    rawg_api_key: str = ""
    syncthing_url: str = ""
    syncthing_api_key: str = ""
    syncthing_timeout_seconds: float = 2.0
    syncthing_cache_seconds: int = 10

    @classmethod
    def from_env(cls) -> "Settings":
        extension_value = _env("ROMMATES_EXTENSIONS", "ROM_EXTENSIONS", "")
        extensions = DEFAULT_EXTENSIONS
        if extension_value.strip():
            extensions = frozenset(
                value.strip().lower() if value.strip().startswith(".") else f".{value.strip().lower()}"
                for value in extension_value.split(",")
                if value.strip()
            )
        folder_platform_value = os.getenv("ROMMATES_FOLDER_BUNDLE_PLATFORMS", "ps3,wiiu")
        folder_bundle_platforms = frozenset(
            value.strip().casefold()
            for value in folder_platform_value.split(",")
            if value.strip()
        )
        system_map: dict[str, int] = {}
        raw_system_map = os.getenv("ROMMATES_SCREENSCRAPER_SYSTEM_MAP", "").strip()
        if raw_system_map:
            try:
                parsed = json.loads(raw_system_map)
                if isinstance(parsed, dict):
                    system_map = {
                        str(key).strip().casefold(): int(value)
                        for key, value in parsed.items()
                        if str(key).strip()
                    }
            except (ValueError, TypeError, json.JSONDecodeError):
                system_map = {}
        library_root = Path(_env("ROMMATES_LIBRARY_ROOT", "ROM_LIBRARY_ROOT", "/roms"))
        devices_root = Path(_env("ROMMATES_DEVICES_ROOT", "ROM_DEVICES_ROOT", "/devices"))
        trash_root = Path(_env("ROMMATES_TRASH_ROOT", "ROM_TRASH_ROOT", "/trash"))
        return cls(
            library_root=library_root,
            devices_root=devices_root,
            trash_root=trash_root,
            database_path=Path(_env("ROMMATES_DATABASE_PATH", "ROM_DATABASE_PATH", "/data/rommates.db")),
            scan_on_start=_bool_env("ROMMATES_SCAN_ON_START", "ROM_SCAN_ON_START", True),
            access_token=_env("ROMMATES_ACCESS_TOKEN", "ROM_ACCESS_TOKEN", "").strip(),
            allow_anonymous=_bool_env("ROMMATES_ALLOW_ANONYMOUS", "ROM_ALLOW_ANONYMOUS", False),
            require_existing_roots=_bool_env(
                "ROMMATES_REQUIRE_EXISTING_ROOTS", "ROM_REQUIRE_EXISTING_ROOTS", False
            ),
            extensions=extensions,
            folder_bundle_platforms=folder_bundle_platforms,
            hash_max_bytes=_int_env(
                "ROMMATES_HASH_MAX_BYTES", 512 * 1024 * 1024, 0, 1024 * 1024 * 1024 * 1024
            ),
            scan_prune_limit=_float_env("ROMMATES_SCAN_PRUNE_LIMIT", "ROM_SCAN_PRUNE_LIMIT", 0.5),
            saves_root=Path(os.getenv("ROMMATES_SAVES_ROOT", "/saves")),
            snapshots_root=Path(os.getenv("ROMMATES_SNAPSHOTS_ROOT", "/snapshots")),
            save_snapshot_interval_minutes=_int_env(
                "ROMMATES_SAVE_SNAPSHOT_INTERVAL_MINUTES", 360, 0, 10080
            ),
            save_snapshot_quiet_seconds=_int_env(
                "ROMMATES_SAVE_SNAPSHOT_QUIET_SECONDS", 2, 0, 30
            ),
            save_retention_recent=_int_env("ROMMATES_SAVE_RETENTION_RECENT", 24, 1, 1000),
            save_retention_daily=_int_env("ROMMATES_SAVE_RETENTION_DAILY", 30, 0, 3650),
            save_retention_weekly=_int_env("ROMMATES_SAVE_RETENTION_WEEKLY", 12, 0, 520),
            save_retention_monthly=_int_env("ROMMATES_SAVE_RETENTION_MONTHLY", 12, 0, 240),
            media_root=Path(os.getenv("ROMMATES_MEDIA_ROOT", "/data/media")),
            upload_root=Path(
                os.getenv("ROMMATES_UPLOAD_ROOT", str(trash_root.parent / ".rommates-uploads"))
            ),
            upload_max_bytes=_int_env(
                "ROMMATES_UPLOAD_MAX_BYTES", 128 * 1024 * 1024 * 1024, 1, 8 * 1024**5
            ),
            upload_chunk_bytes=_int_env(
                "ROMMATES_UPLOAD_CHUNK_BYTES", 8 * 1024 * 1024, 1024 * 1024, 64 * 1024 * 1024
            ),
            upload_expiry_hours=_int_env("ROMMATES_UPLOAD_EXPIRY_HOURS", 24, 1, 168),
            download_ticket_seconds=_int_env(
                "ROMMATES_DOWNLOAD_TICKET_SECONDS", 300, 30, 3600
            ),
            screenscraper_dev_id=os.getenv("ROMMATES_SCREENSCRAPER_DEV_ID", "").strip(),
            screenscraper_dev_password=os.getenv(
                "ROMMATES_SCREENSCRAPER_DEV_PASSWORD", ""
            ).strip(),
            screenscraper_softname=os.getenv(
                "ROMMATES_SCREENSCRAPER_SOFTNAME", "ROMmates"
            ).strip() or "ROMmates",
            rawg_api_key=(
                os.getenv("ROMMATES_RAWG_API_KEY") or os.getenv("RAWG_API_KEY", "")
            ).strip(),
            syncthing_url=os.getenv("ROMMATES_SYNCTHING_URL", "").strip().rstrip("/"),
            syncthing_api_key=os.getenv("ROMMATES_SYNCTHING_API_KEY", "").strip(),
            syncthing_timeout_seconds=_number_env(
                "ROMMATES_SYNCTHING_TIMEOUT_SECONDS", 2.0, 0.25, 10.0
            ),
            syncthing_cache_seconds=_int_env(
                "ROMMATES_SYNCTHING_CACHE_SECONDS", 10, 1, 300
            ),
            screenscraper_user=os.getenv("ROMMATES_SCREENSCRAPER_USER", "").strip(),
            screenscraper_password=os.getenv(
                "ROMMATES_SCREENSCRAPER_PASSWORD", ""
            ).strip(),
            screenscraper_system_map=system_map,
        )
