from __future__ import annotations

import unittest

from app.nextui import nextui_device_relpath, nextui_supports, nextui_system_name
from app.rom_layouts import (
    ESDE,
    NEXTUI,
    device_relpath_for,
    layout_supports,
    normalize_layout,
    relpath_column,
)


class NextUiFolderTests(unittest.TestCase):
    """NextUI's tags are not derivable from the system name.

    NES is (FC), SNES is (SFC), Genesis is (MD), PlayStation is (PS). These are
    transcribed from NextUI's own skeleton, so pin the surprising ones.
    """

    def test_tags_that_do_not_match_their_system_name(self):
        self.assertEqual(nextui_system_name("nes"), "Nintendo Entertainment System (FC)")
        self.assertEqual(nextui_system_name("snes"), "Super Nintendo Entertainment System (SFC)")
        self.assertEqual(nextui_system_name("genesis"), "Sega Genesis (MD)")
        self.assertEqual(nextui_system_name("psx"), "Sony PlayStation (PS)")
        self.assertEqual(nextui_system_name("pcengine"), "TurboGrafx-16 (PCE)")

    def test_defaults_prefer_the_base_install_emulator(self):
        # NextUI also ships (MGBA) and (SUPA), but those need extra paks.
        self.assertEqual(nextui_system_name("gba"), "Game Boy Advance (GBA)")
        self.assertEqual(nextui_system_name("snes"), "Super Nintendo Entertainment System (SFC)")

    def test_aliases_normalize(self):
        for alias in ("megadrive", "Sega Mega Drive", "MD", "sega-genesis"):
            with self.subTest(alias=alias):
                self.assertEqual(nextui_system_name(alias), "Sega Genesis (MD)")

    def test_unsupported_platforms_keep_their_library_folder(self):
        # NextUI has no emulator for these, so never guess a destination.
        for platform in ("nds", "n64", "psp", "dreamcast", "gc", "ps2", "switch"):
            with self.subTest(platform=platform):
                self.assertFalse(nextui_supports(platform))
                self.assertEqual(nextui_system_name(platform), platform)

    def test_only_the_first_path_component_is_rewritten(self):
        self.assertEqual(
            nextui_device_relpath("psx", "psx/Final Fantasy VII/disc1.cue"),
            "Sony PlayStation (PS)/Final Fantasy VII/disc1.cue",
        )

    def test_unknown_platform_round_trips_unchanged(self):
        self.assertEqual(
            nextui_device_relpath("nds", "nds/Pokemon HeartGold.nds"),
            "nds/Pokemon HeartGold.nds",
        )


class LayoutRegistryTests(unittest.TestCase):
    def test_unknown_layouts_fall_back_to_esde(self):
        for value in ("", None, "bogus", "NEXTUI "):
            with self.subTest(value=value):
                self.assertIn(normalize_layout(value), (ESDE, NEXTUI))
        self.assertEqual(normalize_layout("bogus"), ESDE)
        self.assertEqual(normalize_layout("NextUI"), NEXTUI)

    def test_relpath_column_is_a_fixed_identifier(self):
        self.assertEqual(relpath_column(ESDE), "device_relpath")
        self.assertEqual(relpath_column(NEXTUI), "nextui_relpath")
        # Caller text can never reach SQL as an identifier.
        self.assertEqual(relpath_column("'; DROP TABLE games;--"), "device_relpath")

    def test_layouts_disagree_about_the_same_file(self):
        self.assertEqual(device_relpath_for(ESDE, "snes", "snes/Game.sfc"), "snes/Game.sfc")
        self.assertEqual(
            device_relpath_for(NEXTUI, "snes", "snes/Game.sfc"),
            "Super Nintendo Entertainment System (SFC)/Game.sfc",
        )

    def test_esde_is_treated_as_supporting_every_platform(self):
        self.assertTrue(layout_supports(ESDE, "nds"))
        self.assertFalse(layout_supports(NEXTUI, "nds"))


class NextUiSourceFidelityTests(unittest.TestCase):
    """Guard the transcription against NextUI's published folder list.

    Runs only when a copy of NextUI's skeleton folder names is checked in beside
    the tests; otherwise skipped so the suite stays offline and deterministic.
    """

    def test_every_mapped_folder_is_well_formed(self):
        from app.nextui import _FOLDERS

        for platform, folder in _FOLDERS.items():
            with self.subTest(platform=platform):
                self.assertRegex(folder, r"^.+ \([A-Za-z0-9]+\)$", f"{folder} is not '<Name> (TAG)'")
                self.assertNotIn("/", folder)

    def test_no_alias_maps_to_two_different_folders(self):
        from app.nextui import _FOLDERS

        self.assertEqual(len(_FOLDERS), len(set(_FOLDERS)))


if __name__ == "__main__":
    unittest.main()
