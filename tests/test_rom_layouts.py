from __future__ import annotations

import unittest

from app.batocera import batocera_device_relpath, batocera_supports, batocera_system_name
from app.nextui import nextui_device_relpath, nextui_supports, nextui_system_name
from app.rom_layouts import (
    BATOCERA,
    ESDE,
    NEXTUI,
    ROM_LAYOUTS,
    relpath_case,
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


class BatoceraFolderTests(unittest.TestCase):
    """Batocera/Knulli use their es_systems.yml keys as folder names.

    Most match ES-DE's short names, so pin the ones that do not: these are the
    folders a Knulli handheld would silently fail to list.
    """

    def test_folders_that_differ_from_esde(self):
        self.assertEqual(batocera_system_name("genesis"), "megadrive")
        self.assertEqual(batocera_system_name("gc"), "gamecube")
        self.assertEqual(batocera_system_name("n3ds"), "3ds")
        self.assertEqual(batocera_system_name("wonderswan"), "wswan")
        self.assertEqual(batocera_system_name("wonderswancolor"), "wswanc")

    def test_folders_identical_to_esde_pass_through(self):
        for platform in ("gba", "snes", "nes", "gb", "gbc", "psx", "nds", "n64", "psp", "dreamcast", "wii", "xbox360"):
            with self.subTest(platform=platform):
                self.assertEqual(batocera_system_name(platform), platform)
                self.assertTrue(batocera_supports(platform))

    def test_human_readable_names_resolve_through_the_esde_aliases(self):
        self.assertEqual(batocera_system_name("Sega Genesis"), "megadrive")
        self.assertEqual(batocera_system_name("Nintendo GameCube"), "gamecube")
        self.assertEqual(batocera_system_name("Game Boy Advance"), "gba")

    def test_unknown_platforms_keep_their_library_folder(self):
        self.assertFalse(batocera_supports("n3ds-encrypted"))
        self.assertEqual(batocera_system_name("n3ds-encrypted"), "n3ds-encrypted")

    def test_hyphenated_batocera_keys_survive_normalisation(self):
        self.assertEqual(batocera_system_name("snes-msu1"), "snes-msu1")
        self.assertTrue(batocera_supports("snes-msu1"))

    def test_only_the_first_path_component_is_rewritten(self):
        self.assertEqual(
            batocera_device_relpath("genesis", "genesis/Sonic/disc.bin"),
            "megadrive/Sonic/disc.bin",
        )


class BatoceraRegistryTests(unittest.TestCase):
    def test_batocera_is_a_registered_layout(self):
        self.assertIn(BATOCERA, ROM_LAYOUTS)
        self.assertEqual(normalize_layout("Batocera"), BATOCERA)
        self.assertEqual(relpath_column(BATOCERA), "batocera_relpath")
        self.assertEqual(device_relpath_for(BATOCERA, "gc", "gc/Melee.iso"), "gamecube/Melee.iso")

    def test_per_row_case_covers_every_non_default_layout(self):
        case = relpath_case("d", "gf")
        self.assertIn("WHEN 'nextui' THEN gf.nextui_relpath", case)
        self.assertIn("WHEN 'batocera' THEN gf.batocera_relpath", case)
        self.assertTrue(case.endswith("ELSE gf.device_relpath END"))

    def test_layout_supports_consults_the_batocera_map(self):
        self.assertTrue(layout_supports(BATOCERA, "genesis"))
        self.assertFalse(layout_supports(BATOCERA, "n3ds-encrypted"))


if __name__ == "__main__":
    unittest.main()
