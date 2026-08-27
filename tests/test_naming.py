from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from app.config import Settings
from app.db import Database
from app.library import LibraryError, LibraryService, normalize_name
from app.naming import NamingService


class NamingServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.roms = root / "roms"
        self.settings = Settings(
            library_root=self.roms,
            devices_root=root / "devices",
            trash_root=root / "trash",
            database_path=root / "data" / "rommates.db",
            scan_on_start=False,
        )
        self.db = Database(self.settings.database_path)
        self.db.initialize()
        self.library = LibraryService(self.settings, self.db)
        self.library.prepare_roots()
        self.naming = NamingService(self.db, self.roms, self.library)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, relative: str, content: bytes) -> None:
        path = self.roms / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def dat(self, rom_name: str, content: bytes, include_hash: bool = True) -> str:
        digest = hashlib.sha256(content).hexdigest() if include_hash else ""
        hash_attribute = f' sha256="{digest}"' if digest else ""
        return f'<datafile><game name="Game"><rom name="{rom_name}" size="{len(content)}"{hash_attribute}/></game></datafile>'

    def test_exact_dat_match_suggests_canonical_name(self):
        content = b"rom-content"
        self.write("gba/bad_name.gba", content)
        self.library.scan()
        self.naming.import_dat("Nintendo GBA.dat", "gba", self.dat("Good Name (USA).gba", content))

        result = self.naming.suggestions()

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["suggested_name"], "Good Name (USA)")
        self.assertEqual(result["items"][0]["confidence"], "exact")

    def test_unique_normalized_dat_name_is_a_strong_match(self):
        self.write("gba/03. Pokemon Emerald (USA).gba", b"different-content")
        self.library.scan()
        self.naming.import_dat(
            "GBA.dat", "gba", self.dat("Pokemon Emerald (USA).gba", b"catalog-content", include_hash=False)
        )

        suggestion = self.naming.suggestions()["items"][0]

        self.assertEqual(suggestion["confidence"], "strong")
        self.assertEqual(suggestion["suggested_name"], "Pokemon Emerald (USA)")
        self.assertEqual(normalize_name("03. Pokemon Emerald (USA).gba"), "pokemon emerald")

    def test_cleanup_and_bulk_apply_preserve_hash_cache(self):
        self.write("gba/10. Metroid_Fusion (USA).gba", b"metroid")
        self.library.scan()
        suggestion = self.naming.suggestions()["items"][0]
        self.assertEqual(suggestion["confidence"], "cleanup")

        result = self.library.bulk_rename([(suggestion["game_id"], suggestion["suggested_name"])])

        self.assertEqual(result["renamed"], 1)
        self.assertTrue((self.roms / "gba/Metroid Fusion (USA).gba").exists())
        with self.db.connect() as connection:
            cached = connection.execute("SELECT relpath FROM file_cache").fetchone()["relpath"]
        self.assertEqual(cached, "gba/Metroid Fusion (USA).gba")

    def test_dat_rejects_entity_declarations(self):
        with self.assertRaisesRegex(LibraryError, "DTD or entity"):
            self.naming.import_dat("unsafe.dat", "gba", '<!DOCTYPE x [<!ENTITY x "bad">]><datafile/>')


if __name__ == "__main__":
    unittest.main()
