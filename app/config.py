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


def _bool_env(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    library_root: Path
    devices_root: Path
    trash_root: Path
    database_path: Path
    scan_on_start: bool = True
    access_token: str = ""
    require_existing_roots: bool = False
    extensions: frozenset[str] = DEFAULT_EXTENSIONS

    @classmethod
    def from_env(cls) -> "Settings":
        extension_value = os.getenv("ROM_EXTENSIONS", "")
        extensions = DEFAULT_EXTENSIONS
        if extension_value.strip():
            extensions = frozenset(
                value.strip().lower() if value.strip().startswith(".") else f".{value.strip().lower()}"
                for value in extension_value.split(",")
                if value.strip()
            )
        return cls(
            library_root=Path(os.getenv("ROM_LIBRARY_ROOT", "/roms")),
            devices_root=Path(os.getenv("ROM_DEVICES_ROOT", "/devices")),
            trash_root=Path(os.getenv("ROM_TRASH_ROOT", "/trash")),
            database_path=Path(os.getenv("ROM_DATABASE_PATH", "/data/rommanager.db")),
            scan_on_start=_bool_env("ROM_SCAN_ON_START", True),
            access_token=os.getenv("ROM_ACCESS_TOKEN", "").strip(),
            require_existing_roots=_bool_env("ROM_REQUIRE_EXISTING_ROOTS", False),
            extensions=extensions,
        )
