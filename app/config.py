from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_EXTENSIONS = frozenset(
    {
        ".7z", ".a26", ".a52", ".a78", ".bin", ".chd", ".col", ".cue",
        ".d64", ".fds", ".gb", ".gba", ".gbc", ".gg", ".iso", ".lnx",
        ".m3u", ".md", ".n64", ".nds", ".nes", ".ngc", ".pbp", ".pce",
        ".rvz", ".sfc", ".sg", ".smc", ".sms", ".swc", ".v64", ".wad",
        ".wbfs", ".ws", ".wsc", ".z64", ".zip",
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
    # Largest share of the indexed catalog a single scan may prune without an
    # explicit confirmation. Guards against an unmounted library root cascading
    # into every device selection.
    scan_prune_limit: float = 0.5

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
        return cls(
            library_root=Path(_env("ROMMATES_LIBRARY_ROOT", "ROM_LIBRARY_ROOT", "/roms")),
            devices_root=Path(_env("ROMMATES_DEVICES_ROOT", "ROM_DEVICES_ROOT", "/devices")),
            trash_root=Path(_env("ROMMATES_TRASH_ROOT", "ROM_TRASH_ROOT", "/trash")),
            database_path=Path(_env("ROMMATES_DATABASE_PATH", "ROM_DATABASE_PATH", "/data/rommates.db")),
            scan_on_start=_bool_env("ROMMATES_SCAN_ON_START", "ROM_SCAN_ON_START", True),
            access_token=_env("ROMMATES_ACCESS_TOKEN", "ROM_ACCESS_TOKEN", "").strip(),
            allow_anonymous=_bool_env("ROMMATES_ALLOW_ANONYMOUS", "ROM_ALLOW_ANONYMOUS", False),
            require_existing_roots=_bool_env(
                "ROMMATES_REQUIRE_EXISTING_ROOTS", "ROM_REQUIRE_EXISTING_ROOTS", False
            ),
            extensions=extensions,
            scan_prune_limit=_float_env("ROMMATES_SCAN_PRUNE_LIMIT", "ROM_SCAN_PRUNE_LIMIT", 0.5),
        )
