from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from app.config import Settings


class ConfigurationCompatibilityTests(unittest.TestCase):
    def test_new_rommates_names_take_precedence(self):
        with patch.dict(
            os.environ,
            {
                "ROMMATES_DATABASE_PATH": "/new/rommates.db",
                "ROM_DATABASE_PATH": "/legacy/rommanager.db",
                "ROMMATES_ACCESS_TOKEN": "new-token-123456",
                "ROM_ACCESS_TOKEN": "legacy-token-123456",
                "ROMMATES_SAVES_ROOT": "/srv/webdav/RetroArch",
                "ROMMATES_SNAPSHOTS_ROOT": "/srv/rommates-snapshots",
                "ROMMATES_SAVE_SNAPSHOT_INTERVAL_MINUTES": "90",
            },
            clear=True,
        ):
            settings = Settings.from_env()
        self.assertEqual(str(settings.database_path), "/new/rommates.db")
        self.assertEqual(settings.access_token, "new-token-123456")
        self.assertEqual(str(settings.saves_root), "/srv/webdav/RetroArch")
        self.assertEqual(str(settings.snapshots_root), "/srv/rommates-snapshots")
        self.assertEqual(settings.save_snapshot_interval_minutes, 90)

    def test_legacy_rom_manager_environment_still_works(self):
        with patch.dict(
            os.environ,
            {
                "ROM_DATABASE_PATH": "/legacy/rommanager.db",
                "ROM_ACCESS_TOKEN": "legacy-token-123456",
                "ROM_SCAN_PRUNE_LIMIT": "0.25",
            },
            clear=True,
        ):
            settings = Settings.from_env()
        self.assertEqual(str(settings.database_path), "/legacy/rommanager.db")
        self.assertEqual(settings.access_token, "legacy-token-123456")
        self.assertEqual(settings.scan_prune_limit, 0.25)


if __name__ == "__main__":
    unittest.main()
