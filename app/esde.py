from __future__ import annotations

import re
from pathlib import PurePosixPath


# ES-DE's system paths are intentionally short and case-sensitive.  Only map
# unambiguous, human-readable aliases here; unknown/custom system names retain
# their library folder so ROMmates never guesses destructively.
_ALIASES = {
    "gb": "gb",
    "gameboy": "gb",
    "nintendogameboy": "gb",
    "gbc": "gbc",
    "gameboycolor": "gbc",
    "nintendogameboycolor": "gbc",
    "gba": "gba",
    "gameboyadvance": "gba",
    "nintendogameboyadvance": "gba",
    "n64": "n64",
    "nintendo64": "n64",
    "nds": "nds",
    "nintendods": "nds",
    "nintendodsi": "nds",
    "3ds": "n3ds",
    "n3ds": "n3ds",
    "nintendo3ds": "n3ds",
    "gc": "gc",
    "gamecube": "gc",
    "nintendogamecube": "gc",
    "nes": "nes",
    "snes": "snes",
    "supernintendo": "snes",
    "supernintendoentertainmentsystem": "snes",
    "nintendoentertainmentsystem": "nes",
    "ps1": "psx",
    "psx": "psx",
    "playstation": "psx",
    "playstation1": "psx",
    "sonyplaystation": "psx",
    "ps2": "ps2",
    "playstation2": "ps2",
    "sonyplaystation2": "ps2",
    "ps3": "ps3",
    "playstation3": "ps3",
    "sonyplaystation3": "ps3",
    "psp": "psp",
    "playstationportable": "psp",
    "sonyplaystationportable": "psp",
    "dreamcast": "dreamcast",
    "segadreamcast": "dreamcast",
    "gamegear": "gamegear",
    "segagamegear": "gamegear",
    "mastersystem": "mastersystem",
    "sms": "mastersystem",
    "segamastersystem": "mastersystem",
    "megadrive": "megadrive",
    "segamegadrive": "megadrive",
    "genesis": "genesis",
    "segagenesis": "genesis",
    "saturn": "saturn",
    "segasaturn": "saturn",
    "sega32x": "sega32x",
    "segacd": "segacd",
    "pcengine": "pcengine",
    "pcenginecd": "pcenginecd",
    "turbografx16": "pcengine",
    "turbografxcd": "pcenginecd",
    "neogeopocket": "ngp",
    "ngp": "ngp",
    "neogeopocketcolor": "ngpc",
    "ngpc": "ngpc",
    "wonderswan": "wonderswan",
    "wonderswancolor": "wonderswancolor",
    "wii": "wii",
    "wiiu": "wiiu",
    "switch": "switch",
    "xbox": "xbox",
    "xbox360": "xbox360",
}


def esde_system_name(platform: str) -> str:
    """Return ES-DE's canonical folder for a known platform alias."""
    value = platform.strip()
    key = re.sub(r"[^a-z0-9]+", "", value.casefold())
    return _ALIASES.get(key, value)


def esde_device_relpath(platform: str, library_relpath: str) -> str:
    """Translate a canonical-library path to its path below a device's roms/ root."""
    path = PurePosixPath(library_relpath)
    parts = path.parts
    if not parts:
        return library_relpath
    # Scanned library files always begin with the platform directory. Preserve
    # every nested bundle path and replace only that first component.
    return PurePosixPath(esde_system_name(platform), *parts[1:]).as_posix()
