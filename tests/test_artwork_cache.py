from __future__ import annotations

import hashlib
import tempfile
import time
import unittest
from pathlib import Path

from PIL import Image

from app.artwork_cache import ArtworkThumbnailCache, THUMBNAIL_MAX_SIZE
from app.config import Settings
from app.db import Database


class ArtworkThumbnailCacheTests(unittest.TestCase):
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
        )
        self.db = Database(self.settings.database_path)
        self.db.initialize()
        with self.db.write() as connection:
            connection.execute(
                "INSERT INTO games(platform,display_name,extension,primary_relpath,size,bundle_hash,normalized_name) "
                "VALUES('gba','Test Game','.gba','gba/Test Game.gba',1,'game-hash','test game')"
            )
            game_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        source = self.settings.media_root / str(game_id) / "cover.jpg"
        source.parent.mkdir(parents=True)
        Image.new("RGB", (1200, 1800), "purple").save(source, format="JPEG", quality=90)
        payload = source.read_bytes()

        with self.db.write() as connection:
            connection.execute(
                "INSERT INTO game_assets(game_id,source,kind,media_type,local_relpath,content_type,size,sha256) "
                "VALUES(?,'test','cover','box-2D',?,'image/jpeg',?,?)",
                (
                    game_id,
                    source.relative_to(self.settings.media_root).as_posix(),
                    len(payload),
                    hashlib.sha256(payload).hexdigest(),
                ),
            )
            self.asset_id = connection.execute(
                "SELECT last_insert_rowid() AS id"
            ).fetchone()["id"]
        self.cache = ArtworkThumbnailCache(self.settings, self.db)

    def tearDown(self):
        self.cache.close()
        self.temp.cleanup()

    def test_start_hydrates_all_existing_covers_asynchronously(self):
        self.cache.start()
        for _ in range(100):
            status = self.cache.status()
            if status["state"] == "complete":
                break
            time.sleep(0.02)
        else:
            self.fail(f"Thumbnail hydration did not complete: {self.cache.status()}")
        self.assertEqual(status["total"], 1)
        self.assertEqual(status["ready"], 1)
        self.assertEqual(status["generated"], 1)
        thumbnail, content_type = self.cache.ensure(self.asset_id)
        self.assertEqual(content_type, "image/webp")
        self.assertTrue(thumbnail.is_file())
        with Image.open(thumbnail) as image:
            self.assertLessEqual(image.width, THUMBNAIL_MAX_SIZE[0])
            self.assertLessEqual(image.height, THUMBNAIL_MAX_SIZE[1])

    def test_ensure_reuses_the_content_versioned_thumbnail(self):
        first, _ = self.cache.ensure(self.asset_id)
        first_mtime = first.stat().st_mtime_ns
        second, _ = self.cache.ensure(self.asset_id)
        self.assertEqual(first, second)
        self.assertEqual(first_mtime, second.stat().st_mtime_ns)
