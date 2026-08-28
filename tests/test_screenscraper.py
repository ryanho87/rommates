from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.db import Database
from app.library import LibraryService
from app.screenscraper import ScreenScraperService


class _Headers:
    def __init__(self, content_type: str):
        self.content_type = content_type

    def get_content_type(self):
        return self.content_type


class _Response(io.BytesIO):
    def __init__(self, data: bytes, content_type: str = "application/json"):
        super().__init__(data)
        self.headers = _Headers(content_type)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class ScreenScraperTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.settings = Settings(
            library_root=root / "roms",
            devices_root=root / "devices",
            trash_root=root / "trash",
            database_path=root / "data/rommates.db",
            scan_on_start=False,
            access_token="test-token-long-enough",
            media_root=root / "media",
            screenscraper_dev_id="developer",
            screenscraper_dev_password="secret",
            screenscraper_system_map={"gba": 12},
        )
        (root / "roms/gba").mkdir(parents=True)
        (root / "devices").mkdir()
        (root / "roms/gba/Test Game.gba").write_bytes(b"test-rom")
        self.db = Database(self.settings.database_path)
        self.db.initialize()
        self.library = LibraryService(self.settings, self.db)
        self.library.prepare_roots()
        self.library.scan()
        with self.db.connect() as connection:
            self.game_id = connection.execute("SELECT id FROM games").fetchone()["id"]

    def tearDown(self):
        self.temp.cleanup()

    def test_scrape_caches_hash_metadata_and_assets(self):
        systems = b'{"response":{"systemes":[]}}'
        game = (
            b'{"response":{"jeu":{"id":"42","noms":[{"region":"us","text":"Test Game"}],'
            b'"synopsis":[{"langue":"en","text":"A test."}],"medias":['
            b'{"type":"box-2D","region":"us","url":"https://media.example/cover.png"},'
            b'{"type":"ss","region":"wor","url":"https://media.example/screen.jpg"}]}}}'
        )
        image = b"not-a-real-image-but-the-api-only-caches-bytes"

        def open_url(request, timeout=0):
            url = request.full_url
            if "systemesListe.php" in url:
                self.assertNotIn("secret", request.headers.values())
                return _Response(systems)
            if "jeuInfos.php" in url:
                self.assertIn("crc=", url)
                self.assertIn("sha1=", url)
                return _Response(game)
            if url.endswith("cover.png"):
                return _Response(image, "image/png")
            if url.endswith("screen.jpg"):
                return _Response(image, "image/jpeg")
            raise AssertionError(url)

        service = ScreenScraperService(self.settings, self.db)
        with patch("urllib.request.urlopen", side_effect=open_url):
            result = service.scrape(
                [self.game_id], progress_callback=lambda *_: None, cancel_check=lambda: None
            )
        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["downloaded"], 2)
        detail = service.detail(self.game_id)
        self.assertEqual(detail["metadata"]["match_method"], "hash")
        self.assertEqual({item["kind"] for item in detail["assets"]}, {"cover", "screenshot"})
        with self.db.connect() as connection:
            fingerprint = connection.execute(
                "SELECT * FROM game_fingerprints WHERE game_id=?", (self.game_id,)
            ).fetchone()
        self.assertEqual(len(fingerprint["sha1"]), 40)
        for asset in detail["assets"]:
            self.assertTrue((self.settings.media_root / asset["local_relpath"]).is_file())


if __name__ == "__main__":
    unittest.main()
