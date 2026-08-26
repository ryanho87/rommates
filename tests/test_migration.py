from __future__ import annotations

import tempfile
import unittest
from pathlib import Path


class StorageRenameTests(unittest.TestCase):
    def test_legacy_database_and_sidecars_move_to_rommates_names(self):
        # Import lazily so the API tests retain control of app.main's environment.
        from app.main import migrate_legacy_path

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "rommanager.db"
            current = root / "rommates.db"
            legacy.write_bytes(b"database")
            Path(f"{legacy}-wal").write_bytes(b"wal")
            self.assertTrue(migrate_legacy_path(current, current, legacy))
            self.assertEqual(current.read_bytes(), b"database")
            self.assertEqual(Path(f"{current}-wal").read_bytes(), b"wal")
            self.assertFalse(legacy.exists())

    def test_custom_paths_are_never_migrated(self):
        from app.main import migrate_legacy_path

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy = root / "rommanager.db"
            configured = root / "custom.db"
            legacy.write_bytes(b"database")
            self.assertFalse(migrate_legacy_path(configured, root / "rommates.db", legacy))
            self.assertTrue(legacy.exists())


if __name__ == "__main__":
    unittest.main()
