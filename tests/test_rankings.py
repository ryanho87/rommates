from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.db import Database
from app.library import LibraryService
from app.rankings import RankingService


class RankingServiceTests(unittest.TestCase):
    def test_rawg_top_hundred_reports_owned_possible_and_missing_games(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = Settings(
                library_root=root / "roms",
                devices_root=root / "devices",
                trash_root=root / "trash",
                database_path=root / "data/rommates.db",
                scan_on_start=False,
                rawg_api_key="rawg-key",
            )
            (root / "roms/gba").mkdir(parents=True)
            (root / "devices").mkdir()
            (root / "roms/gba/Metroid Fusion (USA).gba").write_bytes(b"metroid")
            (root / "roms/gba/Advance Warz.gba").write_bytes(b"wars")
            db = Database(settings.database_path)
            db.initialize()
            library = LibraryService(settings, db)
            library.prepare_roots()
            library.scan()
            service = RankingService(settings, db)
            responses = [
                {"results": [{"id": 24, "slug": "game-boy-advance"}]},
                {"results": [
                    {"id": 1, "slug": "metroid-fusion", "name": "Metroid Fusion", "metacritic": 92, "rating": 4.4, "ratings_count": 100, "released": "2002-11-17"},
                    {"id": 2, "slug": "advance-wars", "name": "Advance Wars", "metacritic": 91, "rating": 4.3, "ratings_count": 90, "released": "2001-09-10"},
                    {"id": 3, "slug": "golden-sun", "name": "Golden Sun", "metacritic": 90, "rating": 4.2, "ratings_count": 80, "released": "2001-11-11"},
                ]},
            ]
            with patch.object(service, "_request", side_effect=responses) as request:
                result = service.refresh(
                    "gba", progress_callback=lambda *_: None, cancel_check=lambda: None
                )
            self.assertEqual(result["games"], 3)
            self.assertEqual(request.call_args_list[1].kwargs["page_size"], 100)

            coverage = service.coverage("gba")

            self.assertEqual(coverage["counts"], {"owned": 1, "possible": 1, "missing": 1})
            self.assertEqual(
                [item["status"] for item in coverage["items"]],
                ["owned", "possible", "missing"],
            )
            self.assertEqual(coverage["items"][0]["match"]["display_name"], "Metroid Fusion (USA)")
            self.assertEqual(coverage["items"][1]["match"]["display_name"], "Advance Warz")
            with db.connect() as connection:
                exact = connection.execute(
                    "SELECT matched_game_id,match_method FROM platform_rankings "
                    "WHERE platform='gba' AND rank=1"
                ).fetchone()
                possible = connection.execute(
                    "SELECT matched_game_id,match_method FROM platform_rankings "
                    "WHERE platform='gba' AND rank=2"
                ).fetchone()
            self.assertIsNotNone(exact["matched_game_id"])
            self.assertEqual(exact["match_method"], "exact")
            self.assertIsNone(possible["matched_game_id"])
            self.assertEqual(possible["match_method"], "")


if __name__ == "__main__":
    unittest.main()
