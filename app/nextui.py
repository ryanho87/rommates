from __future__ import annotations

import re
from pathlib import PurePosixPath


# NextUI (a MinUI fork) names each system folder "<Display Name> (TAG)", where the
# parenthesised tag selects the emulator pak. The tags are not derivable from the
# system name -- NES is (FC), SNES is (SFC), Genesis is (MD), PlayStation is (PS) --
# so these strings are transcribed from the folder skeleton NextUI ships:
# https://github.com/LoveRetro/NextUI -> skeleton/{BASE,EXTRAS}/Roms/
#
# Where NextUI offers two emulators for one system, the BASE folder is the default
# because it works on a stock install: (GBA) rather than (MGBA), (SFC) rather than
# (SUPA).
_FOLDERS = {
    "amiga": "Amiga (PUAE)",
    "amstradcpc": "Amstrad CPC (CPC)",
    "cpc": "Amstrad CPC (CPC)",
    "arcade": "Arcade (FBN)",
    "fbn": "Arcade (FBN)",
    "neogeo": "Arcade (FBN)",
    "atari2600": "Atari 2600 (A2600)",
    "a2600": "Atari 2600 (A2600)",
    "atari5200": "Atari 5200 (A5200)",
    "a5200": "Atari 5200 (A5200)",
    "atari7800": "Atari 7800 (A7800)",
    "a7800": "Atari 7800 (A7800)",
    "atarilynx": "Atari Lynx (LYNX)",
    "lynx": "Atari Lynx (LYNX)",
    "colecovision": "Colecovision (COLECO)",
    "coleco": "Colecovision (COLECO)",
    "c128": "Commodore 128 (C128)",
    "commodore128": "Commodore 128 (C128)",
    "c64": "Commodore 64 (C64)",
    "commodore64": "Commodore 64 (C64)",
    "commodorepet": "Commodore PET (PET)",
    "pet": "Commodore PET (PET)",
    "plus4": "Commodore Plus4 (PLUS4)",
    "commodoreplus4": "Commodore Plus4 (PLUS4)",
    "vic20": "Commodore VIC20 (VIC)",
    "commodorevic20": "Commodore VIC20 (VIC)",
    "doom": "Doom (PRBOOM)",
    "prboom": "Doom (PRBOOM)",
    "fds": "Famicom Disk System (FDS)",
    "famicomdisksystem": "Famicom Disk System (FDS)",
    "gb": "Game Boy (GB)",
    "gameboy": "Game Boy (GB)",
    "nintendogameboy": "Game Boy (GB)",
    "gba": "Game Boy Advance (GBA)",
    "gameboyadvance": "Game Boy Advance (GBA)",
    "nintendogameboyadvance": "Game Boy Advance (GBA)",
    "gbc": "Game Boy Color (GBC)",
    "gameboycolor": "Game Boy Color (GBC)",
    "nintendogameboycolor": "Game Boy Color (GBC)",
    "msx": "Microsoft MSX (MSX)",
    "microsoftmsx": "Microsoft MSX (MSX)",
    "ngp": "Neo Geo Pocket (NGP)",
    "neogeopocket": "Neo Geo Pocket (NGP)",
    "ngpc": "Neo Geo Pocket Color (NGPC)",
    "neogeopocketcolor": "Neo Geo Pocket Color (NGPC)",
    "nes": "Nintendo Entertainment System (FC)",
    "famicom": "Nintendo Entertainment System (FC)",
    "nintendoentertainmentsystem": "Nintendo Entertainment System (FC)",
    "pico8": "Pico-8 (P8)",
    "p8": "Pico-8 (P8)",
    "pokemonmini": "Pokémon mini (PKM)",
    "pokemini": "Pokémon mini (PKM)",
    "sega32x": "Sega 32X (32X)",
    "32x": "Sega 32X (32X)",
    "segacd": "Sega CD (SEGACD)",
    "megacd": "Sega CD (SEGACD)",
    "gamegear": "Sega Game Gear (GG)",
    "segagamegear": "Sega Game Gear (GG)",
    "gg": "Sega Game Gear (GG)",
    "genesis": "Sega Genesis (MD)",
    "segagenesis": "Sega Genesis (MD)",
    "megadrive": "Sega Genesis (MD)",
    "segamegadrive": "Sega Genesis (MD)",
    "md": "Sega Genesis (MD)",
    "mastersystem": "Sega Master System (SMS)",
    "sms": "Sega Master System (SMS)",
    "segamastersystem": "Sega Master System (SMS)",
    "sg1000": "Sega SG-1000 (SG1000)",
    "segasg1000": "Sega SG-1000 (SG1000)",
    "psx": "Sony PlayStation (PS)",
    "ps1": "Sony PlayStation (PS)",
    "playstation": "Sony PlayStation (PS)",
    "sonyplaystation": "Sony PlayStation (PS)",
    "sgb": "Super Game Boy (SGB)",
    "supergameboy": "Super Game Boy (SGB)",
    "snes": "Super Nintendo Entertainment System (SFC)",
    "sfc": "Super Nintendo Entertainment System (SFC)",
    "superfamicom": "Super Nintendo Entertainment System (SFC)",
    "supernintendo": "Super Nintendo Entertainment System (SFC)",
    "supernintendoentertainmentsystem": "Super Nintendo Entertainment System (SFC)",
    "pcengine": "TurboGrafx-16 (PCE)",
    "turbografx16": "TurboGrafx-16 (PCE)",
    "pce": "TurboGrafx-16 (PCE)",
    "virtualboy": "Virtual Boy (VB)",
    "vb": "Virtual Boy (VB)",
}


def _key(platform: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", platform.casefold())


def nextui_supports(platform: str) -> bool:
    """Whether NextUI ships a system folder for this library platform.

    NextUI targets low-power handhelds, so whole shelves have no destination:
    DS, N64, PSP, Dreamcast, GameCube, PS2 and newer have no emulator at all.
    """
    return _key(platform) in _FOLDERS


def nextui_system_name(platform: str) -> str:
    """Return NextUI's folder for a known platform, else the platform unchanged.

    Unknown systems keep their library folder rather than being guessed at, which
    matches the ES-DE mapping. NextUI ignores a folder it has no emulator for, so
    the files are inert there rather than misfiled.
    """
    value = platform.strip()
    return _FOLDERS.get(_key(value), value)


def nextui_device_relpath(platform: str, library_relpath: str) -> str:
    """Translate a canonical-library path to its path below a device's roms/ root."""
    path = PurePosixPath(library_relpath)
    parts = path.parts
    if not parts:
        return library_relpath
    # Scanned library files always begin with the platform directory. Preserve
    # every nested bundle path and replace only that first component.
    return PurePosixPath(nextui_system_name(platform), *parts[1:]).as_posix()
