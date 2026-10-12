from __future__ import annotations

from .batocera import batocera_device_relpath, batocera_supports, batocera_system_name
from .esde import esde_device_relpath, esde_system_name
from .nextui import nextui_device_relpath, nextui_supports, nextui_system_name


# A device's ROM folder layout. ES-DE remains the default so existing devices
# keep the paths they already have on disk.
ESDE = "esde"
NEXTUI = "nextui"
BATOCERA = "batocera"
ROM_LAYOUTS = (ESDE, NEXTUI, BATOCERA)

LAYOUT_LABELS = {
    ESDE: "ES-DE",
    NEXTUI: "NextUI / MinUI",
    BATOCERA: "Batocera / Knulli",
}

# Each layout's denormalised column on game_files. The device path only ever
# rewrites the first path component, so it is derivable from the platform, but
# storing it keeps every device query a plain indexed column read.
_COLUMNS = {
    ESDE: "device_relpath",
    NEXTUI: "nextui_relpath",
    BATOCERA: "batocera_relpath",
}

_RELPATH = {
    ESDE: esde_device_relpath,
    NEXTUI: nextui_device_relpath,
    BATOCERA: batocera_device_relpath,
}

_SYSTEM = {
    ESDE: esde_system_name,
    NEXTUI: nextui_system_name,
    BATOCERA: batocera_system_name,
}


def normalize_layout(layout: object) -> str:
    value = str(layout or "").strip().casefold()
    return value if value in ROM_LAYOUTS else ESDE


def relpath_column(layout: object) -> str:
    """The game_files column holding this layout's device path.

    Returned values are fixed identifiers from _COLUMNS, never caller text, so
    they are safe to interpolate into SQL.
    """
    return _COLUMNS[normalize_layout(layout)]


def relpath_case(device_alias: str = "d", files_alias: str = "gf") -> str:
    """A SQL expression selecting the right device path for each row's device.

    For queries that span devices with different layouts, so one column name
    cannot serve every row. Both aliases are fixed identifiers chosen in code,
    never caller text, so the result is safe to interpolate.
    """
    return (
        f"CASE {device_alias}.rom_layout WHEN '{NEXTUI}' THEN {files_alias}.nextui_relpath "
        f"WHEN '{BATOCERA}' THEN {files_alias}.batocera_relpath "
        f"ELSE {files_alias}.device_relpath END"
    )


def device_relpath_for(layout: object, platform: str, library_relpath: str) -> str:
    return _RELPATH[normalize_layout(layout)](platform, library_relpath)


def system_name_for(layout: object, platform: str) -> str:
    return _SYSTEM[normalize_layout(layout)](platform)


def layout_supports(layout: object, platform: str) -> bool:
    """Whether the layout has a real destination for this platform.

    ES-DE keeps unknown systems under their library folder and is happy to show
    them, so it is treated as supporting everything. NextUI only lists folders it
    has an emulator for.
    """
    normalized = normalize_layout(layout)
    if normalized == NEXTUI:
        return nextui_supports(platform)
    if normalized == BATOCERA:
        return batocera_supports(platform)
    return True
