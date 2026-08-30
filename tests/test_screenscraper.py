from __future__ import annotations

import io
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from app.config import Settings
from app.db import Database
from app.library import LibraryError, LibraryService
from app.screenscraper import ScreenScraperDailyQuota, ScreenScraperService


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
            b'{"response":{"jeu":{"id":"42","note":"17.5","topstaff":"1","noms":[{"region":"us","text":"Test Game"}],'
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
        self.assertEqual(detail["metadata"]["rating"], 17.5)
        self.assertEqual(detail["metadata"]["top_staff"], 1)
        self.assertEqual({item["kind"] for item in detail["assets"]}, {"cover", "screenshot"})
        with self.db.connect() as connection:
            fingerprint = connection.execute(
                "SELECT * FROM game_fingerprints WHERE game_id=?", (self.game_id,)
            ).fetchone()
        self.assertEqual(len(fingerprint["sha1"]), 40)
        for asset in detail["assets"]:
            self.assertTrue((self.settings.media_root / asset["local_relpath"]).is_file())

    def test_response_quota_is_enforced_before_another_request(self):
        service = ScreenScraperService(self.settings, self.db)
        service._update_quota({
            "response": {"ssuser": {
                "requeststoday": "50",
                "maxrequestsperday": "50",
                "requestskotoday": "4",
                "maxrequestskoperday": "5",
                "maxrequestspermin": "10",
                "maxthreads": "2",
            }}
        })
        with self.assertRaisesRegex(LibraryError, "daily request quota"):
            service._before_request()
        self.assertEqual(service.status()["quota"]["max_threads"], 2)

    def test_bulk_cover_run_persists_progress_and_uses_cover_only(self):
        service = ScreenScraperService(self.settings, self.db)
        run, created = service.create_bulk_run("cover")
        self.assertTrue(created)
        self.assertEqual(run["total_games"], 1)
        self.assertEqual(service.resumable_bulk_runs(), [run["id"]])

        with patch.object(
            service,
            "_scrape_locked",
            return_value={"matched": 1, "downloaded": 1, "skipped": 0, "issues": []},
        ) as scrape:
            result = service.scrape_bulk(
                run["id"], progress_callback=lambda *_: None, cancel_check=lambda: None
            )

        self.assertEqual(result["asset_mode"], "cover")
        self.assertEqual(result["downloaded"], 1)
        self.assertEqual(scrape.call_args.args[3], ("cover",))
        self.assertEqual(service.resumable_bulk_runs(), [])
        status = service.bulk_status()["run"]
        self.assertEqual(status["status"], "complete")
        self.assertEqual(status["processed_games"], 1)

    def test_bulk_run_can_target_platforms_or_selected_games(self):
        service = ScreenScraperService(self.settings, self.db)
        platform_run, created = service.create_bulk_run("cover", platforms=["gba"])
        self.assertTrue(created)
        self.assertEqual(platform_run["scope_type"], "platforms")
        self.assertEqual(platform_run["scope_label"], "gba")
        self.assertEqual(platform_run["total_games"], 1)
        with self.db.write() as connection:
            connection.execute(
                "UPDATE artwork_bulk_runs SET status='cancelled',completed_at=CURRENT_TIMESTAMP WHERE id=?",
                (platform_run["id"],),
            )
        game_run, created = service.create_bulk_run("full", game_ids=[self.game_id])
        self.assertTrue(created)
        self.assertEqual(game_run["scope_type"], "games")
        self.assertEqual(game_run["scope_label"], "1 selected ROM")
        self.assertEqual([run["id"] for run in service.bulk_runs()], [game_run["id"], platform_run["id"]])

    def test_bulk_run_pauses_for_daily_quota_then_resumes(self):
        service = ScreenScraperService(self.settings, self.db)
        run, _ = service.create_bulk_run("cover")
        statuses = []
        with (
            patch.object(service, "_seconds_until_quota_reset", return_value=0),
            patch.object(
                service,
                "_scrape_locked",
                side_effect=[
                    ScreenScraperDailyQuota("quota"),
                    {"matched": 1, "downloaded": 1, "skipped": 0, "issues": []},
                ],
            ),
        ):
            result = service.scrape_bulk(
                run["id"],
                progress_callback=lambda _progress, _detail, status="running": statuses.append(status),
                cancel_check=lambda: None,
            )

        self.assertIn("paused", statuses)
        self.assertEqual(result["matched"], 1)
        self.assertEqual(service.bulk_status()["run"]["status"], "complete")

    def test_rating_scrape_caches_metadata_without_downloading_media(self):
        systems = b'{"response":{"systemes":[]}}'
        game = (
            b'{"response":{"jeu":{"id":"42","note":"16","noms":'
            b'[{"region":"us","text":"Test Game"}],"medias":['
            b'{"type":"box-2D","region":"us","url":"https://media.example/cover.png"}]}}}'
        )

        def open_url(request, timeout=0):
            if "systemesListe.php" in request.full_url:
                return _Response(systems)
            if "jeuInfos.php" in request.full_url:
                return _Response(game)
            raise AssertionError(f"Metadata-only scrape downloaded media: {request.full_url}")

        service = ScreenScraperService(self.settings, self.db)
        with patch("urllib.request.urlopen", side_effect=open_url):
            result = service.scrape(
                [self.game_id],
                download_media=False,
                progress_callback=lambda *_: None,
                cancel_check=lambda: None,
            )

        self.assertEqual(result["matched"], 1)
        self.assertEqual(result["downloaded"], 0)
        detail = service.detail(self.game_id)
        self.assertEqual(detail["metadata"]["rating"], 16)
        self.assertEqual(detail["assets"], [])

    def test_deferred_catalog_hash_uses_name_matching_without_reading_rom(self):
        service = ScreenScraperService(self.settings, self.db)
        with self.db.write() as connection:
            connection.execute(
                "UPDATE games SET bundle_hash='metadata:test' WHERE id=?", (self.game_id,)
            )
            connection.execute(
                "UPDATE game_files SET sha256='' WHERE game_id=?", (self.game_id,)
            )
            game = dict(connection.execute("SELECT * FROM games WHERE id=?", (self.game_id,)).fetchone())
            files = [
                dict(row) for row in connection.execute(
                    "SELECT * FROM game_files WHERE game_id=?", (self.game_id,)
                )
            ]

        with patch.object(Path, "open", side_effect=AssertionError("deferred ROM must not be read")):
            self.assertEqual(service._fingerprint(game, files, lambda: None), {})

    def test_unmatched_quota_only_blocks_match_requests(self):
        service = ScreenScraperService(self.settings, self.db)
        service._update_quota({
            "requeststoday": 1,
            "maxrequestsperday": 100,
            "requestskotoday": 5,
            "maxrequestskoperday": 5,
        })
        service._before_request(may_miss=False)
        with self.assertRaisesRegex(LibraryError, "unmatched-ROM quota"):
            service._before_request(may_miss=True)

    def test_screen_scraper_limit_statuses_have_actionable_errors(self):
        service = ScreenScraperService(self.settings, self.db)
        error = urllib.error.HTTPError("https://api.example", 429, "limit", {}, None)
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaisesRegex(LibraryError, "concurrency limit"):
                service._request_json("systemesListe.php")

    def test_system_list_is_cached_for_subsequent_jobs(self):
        service = ScreenScraperService(self.settings, self.db)
        body = b'{"response":{"systemes":[{"id":"12","nom":"Game Boy Advance"}]}}'
        with patch("urllib.request.urlopen", return_value=_Response(body)) as open_url:
            first = service._systems()
            second = service._systems()
        self.assertEqual(first, second)
        self.assertEqual(open_url.call_count, 1)

    def test_current_system_name_object_is_mapped(self):
        service = ScreenScraperService(self.settings, self.db)
        body = (
            b'{"response":{"systemes":[{"id":1,"noms":{'
            b'"nom_eu":"Megadrive","nom_us":"Genesis",'
            b'"nom_retropie":"genesis,megadrive",'
            b'"noms_commun":"Sega Megadrive,Sega Genesis"}}]}}'
        )
        with patch("urllib.request.urlopen", return_value=_Response(body)):
            systems = service._systems()

        self.assertEqual(systems["megadrive"], 1)
        self.assertEqual(systems["genesis"], 1)
        self.assertEqual(systems["sega megadrive"], 1)
        self.assertEqual(systems["sega genesis"], 1)


if __name__ == "__main__":
    unittest.main()
