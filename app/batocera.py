from __future__ import annotations

import re
from pathlib import PurePosixPath

from .esde import esde_system_name

# Batocera (and Knulli, which forks it) uses each system key from its own
# es_systems.yml as the folder under roms/. The keys below are transcribed from
# Knulli's shipped definitions, so a platform whose name is already one of them
# maps to itself. Most of ROMmates' ES-DE-style names are identical; the handful
# that are not are aliased explicitly.
# https://github.com/knulli-cfw/distribution -> package/batocera/emulationstation/batocera-es-system/es_systems.yml
_KEYS = frozenset({
    "3do", "3ds", "abuse", "adam", "advision", "amiga", "amiga1200", "amiga500", "amigacd32",
    "amigacdtv", "amstradcpc", "apfm1000", "apple2", "apple2gs", "arcadia", "archimedes",
    "arduboy", "astrocde", "atari2600", "atari5200", "atari7800", "atari800", "atarist", "atom",
    "atomiswave", "bbc", "c128", "c20", "c64", "camplynx", "cannonball", "cave3rd", "cavestory",
    "cdi", "cdogs", "cgenius", "channelf", "chihiro", "coco", "colecovision", "commanderx16",
    "corsixth", "cplus4", "crvision", "daphne", "devilutionx", "doom3", "dos", "dreamcast",
    "dxx-rebirth", "easyrpg", "ecwolf", "eduke32", "electron", "emulators", "etlegacy",
    "fallout1-ce", "fallout2-ce", "fbneo", "fds", "flash", "flatpak", "fm7", "fmtowns",
    "fpinball", "fury", "gaelco", "gamate", "gameandwatch", "gamecom", "gamecube", "gamegear",
    "gamepock", "gb", "gb2players", "gba", "gbc", "gbc2players", "gmaster", "gong", "gp32",
    "gx4000", "gzdoom", "hcl", "hikaru", "hurrican", "ikemen", "imageviewer", "intellivision",
    "iortcw", "j2me", "jaguar", "jaguarcd", "jazz2", "laser310", "lcdgames", "library",
    "lowresnx", "lutro", "lynx", "macintosh", "mame", "mastersystem", "megadrive", "megaduck",
    "model2", "model3", "moonlight", "mpv", "mrboom", "msu-md", "msx", "msx1", "msx2",
    "msxturbor", "mugen", "multivision", "n64", "n64dd", "namco2x6", "naomi", "naomi2", "nds",
    "neogeo", "neogeocd", "nes", "ngage", "ngp", "ngpc", "o2em", "odcommander", "openbor",
    "openjazz", "openlara", "pc88", "pc98", "pcengine", "pcenginecd", "pcfx", "pdp1", "pet",
    "pico", "pico8", "plugnplay", "pokemini", "ports", "prboom", "ps2", "ps3", "ps4", "psp",
    "psvita", "psx", "pv1000", "pygame", "pyxel", "quake3", "raze", "recordings",
    "reminiscence", "rott", "samcoupe", "satellaview", "saturn", "scummvm", "scv", "sdlpop",
    "sega32x", "segacd", "sg1000", "sgb", "singe", "snes", "snes-msu1", "socrates", "solarus",
    "sonic-mania", "sonic3-air", "sonicretro", "spectravideo", "steam", "sufami",
    "superbroswar", "supergrafx", "supervision", "supracan", "switch", "systemsp",
    "theforceengine", "thextech", "thomson", "ti99", "tic80", "tools", "triforce", "tutor",
    "tyrian", "tyrquake", "uqm", "uzebox", "vc4000", "vectrex", "vemulator", "vgmplay",
    "videopacplus", "vircon32", "virtualboy", "vis", "vitaquake2", "vpinball", "vsmile",
    "wasm4", "wii", "wiiu", "windows", "windows_installers", "wswan", "wswanc", "x1", "x68000",
    "xash3d_fwgs", "xbox", "xbox360", "xegs", "xrick", "zc210", "zx81", "zxspectrum",
})

# ES-DE-style names whose Batocera folder is spelled differently.
_ALIASES = {
    "genesis": "megadrive",
    "gc": "gamecube",
    "n3ds": "3ds",
    "wonderswan": "wswan",
    "wonderswancolor": "wswanc",
}

_NORMALIZED = {re.sub(r"[^a-z0-9]+", "", key): key for key in _KEYS}


def _key(platform: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", platform.casefold())


def batocera_supports(platform: str) -> bool:
    """Whether Batocera/Knulli defines a system folder for this platform."""
    # Human-readable names ("Sega Genesis") canonicalise through the ES-DE map
    # first, so every alias ES-DE understands works here too.
    key = _key(esde_system_name(platform))
    return key in _NORMALIZED or key in _ALIASES


def batocera_system_name(platform: str) -> str:
    """Return the Batocera folder for a known platform, else the platform unchanged.

    Unknown systems keep their library folder rather than being guessed at, which
    matches the ES-DE and NextUI mappings.
    """
    value = platform.strip()
    key = _key(esde_system_name(value))
    if key in _ALIASES:
        return _ALIASES[key]
    return _NORMALIZED.get(key, value)


def batocera_device_relpath(platform: str, library_relpath: str) -> str:
    """Translate a canonical-library path to its path below a device's roms/ root."""
    parts = PurePosixPath(library_relpath).parts
    if not parts:
        return library_relpath
    # Scanned library files always begin with the platform directory. Preserve
    # every nested bundle path and replace only that first component.
    return PurePosixPath(batocera_system_name(platform), *parts[1:]).as_posix()
