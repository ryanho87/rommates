from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.db import Database


class DatabaseMigrationTests(unittest.TestCase):
    def test_existing_game_files_receive_esde_device_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE games (id INTEGER PRIMARY KEY,platform TEXT NOT NULL,"
                "primary_relpath TEXT NOT NULL UNIQUE,display_name TEXT NOT NULL,"
                "extension TEXT NOT NULL,size INTEGER NOT NULL DEFAULT 0,bundle_hash TEXT NOT NULL,"
                "normalized_name TEXT NOT NULL,mtime_ns INTEGER NOT NULL DEFAULT 0,"
                "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            connection.execute(
                "CREATE TABLE game_files (game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,"
                "relpath TEXT NOT NULL,size INTEGER NOT NULL,sha256 TEXT NOT NULL,kind TEXT NOT NULL,"
                "PRIMARY KEY(game_id,relpath))"
            )
            connection.execute(
                "INSERT INTO games(id,platform,primary_relpath,display_name,extension,bundle_hash,normalized_name) "
                "VALUES(1,'Nintendo Game Boy','Nintendo Game Boy/Tetris.gb','Tetris','.gb','hash','tetris')"
            )
            connection.execute(
                "INSERT INTO game_files(game_id,relpath,size,sha256,kind) "
                "VALUES(1,'Nintendo Game Boy/Tetris.gb',6,'hash','primary')"
            )
            connection.commit()
            connection.close()

            db = Database(path)
            db.initialize()

            with db.connect() as upgraded:
                row = upgraded.execute(
                    "SELECT device_relpath FROM game_files WHERE game_id=1"
                ).fetchone()
            self.assertEqual(row["device_relpath"], "gb/Tetris.gb")

    def test_existing_database_receives_job_result_column(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE jobs (id INTEGER PRIMARY KEY,kind TEXT,status TEXT,detail TEXT,progress INTEGER,created_at TEXT,completed_at TEXT)"
            )
            connection.commit()
            connection.close()
            db = Database(path)
            db.initialize()
            with db.connect() as upgraded:
                columns = {row["name"] for row in upgraded.execute("PRAGMA table_info(jobs)")}
                versions = {row["version"] for row in upgraded.execute("SELECT version FROM schema_migrations")}
            self.assertIn("result_json", columns)
            self.assertEqual(versions, {1, 2, 3, 4, 5, 6, 7, 8, 9})

    def test_screenscraper_ratings_are_backfilled_from_cached_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.db"
            db = Database(path)
            db.initialize()
            with db.write() as connection:
                connection.execute(
                    "INSERT INTO games(id,platform,primary_relpath,display_name,extension,bundle_hash,normalized_name) "
                    "VALUES(1,'gba','gba/Test.gba','Test','.gba','hash','test')"
                )
                connection.execute(
                    "INSERT INTO game_metadata(game_id,source,source_game_id,source_system_id,match_method,raw_json) "
                    "VALUES(1,'screenscraper','42',12,'hash',?)",
                    ('{"note":"18.5","topstaff":"1"}',),
                )

            db.initialize()

            with db.connect() as connection:
                metadata = connection.execute(
                    "SELECT rating,top_staff FROM game_metadata WHERE game_id=1"
                ).fetchone()
            self.assertEqual(metadata["rating"], 18.5)
            self.assertEqual(metadata["top_staff"], 1)

    def test_legacy_scan_issues_are_recovered_from_job_result(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.db"
            connection = sqlite3.connect(path)
            connection.execute(
                "CREATE TABLE jobs (id INTEGER PRIMARY KEY,kind TEXT,status TEXT,detail TEXT,progress INTEGER,result_json TEXT,created_at TEXT,completed_at TEXT)"
            )
            connection.execute(
                "INSERT INTO jobs(id,kind,status,detail,progress,result_json) VALUES(1,'scan','complete','done',100,?)",
                ('{"skipped":["gba/bad.gba: denied"],"skipped_count":392}',),
            )
            connection.commit()
            connection.close()

            db = Database(path)
            db.initialize()
            with db.connect() as upgraded:
                issues = upgraded.execute(
                    "SELECT detail FROM job_issues WHERE job_id=1"
                ).fetchall()
            self.assertEqual([row["detail"] for row in issues], ["gba/bad.gba: denied"])


if __name__ == "__main__":
    unittest.main()
