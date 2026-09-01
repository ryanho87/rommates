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
            self.assertIn("progress_json", columns)
            self.assertEqual(versions, set(range(1, 22)))
            with db.connect() as upgraded:
                tables = {
                    row["name"]
                    for row in upgraded.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                indexes = {
                    row["name"]
                    for row in upgraded.execute(
                        "SELECT name FROM sqlite_master WHERE type='index'"
                    )
                }
            self.assertIn("device_inventory_files", tables)
            self.assertIn("idx_device_selections_game", indexes)
            self.assertIn("idx_deployments_game", indexes)

    def test_member_and_device_ownership_migration_preserves_accounts_and_sessions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "accounts.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE users (
                    id INTEGER PRIMARY KEY, username TEXT NOT NULL,
                    username_normalized TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL,
                    password_hash TEXT NOT NULL,
                    role TEXT NOT NULL CHECK(role IN ('viewer','contributor','admin')),
                    active INTEGER NOT NULL DEFAULT 1,
                    must_change_password INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_login_at TEXT
                );
                CREATE TABLE auth_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    expires_at INTEGER NOT NULL
                );
                CREATE TABLE devices (
                    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, path TEXT NOT NULL UNIQUE,
                    deployment_mode TEXT NOT NULL DEFAULT 'copy',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE jobs (
                    id INTEGER PRIMARY KEY, kind TEXT, status TEXT, detail TEXT, progress INTEGER,
                    created_at TEXT, completed_at TEXT
                );
                INSERT INTO users(id,username,username_normalized,display_name,password_hash,role)
                VALUES(7,'ryan','ryan','Ryan','hash','admin');
                INSERT INTO auth_sessions(token_hash,user_id,expires_at) VALUES('token',7,9999999999);
                INSERT INTO devices(id,name,path) VALUES(3,'handheld','handheld');
                """
            )
            connection.commit()
            connection.close()

            db = Database(path)
            db.initialize()
            with db.write() as upgraded:
                account = upgraded.execute("SELECT id,role FROM users WHERE id=7").fetchone()
                session = upgraded.execute(
                    "SELECT user_id FROM auth_sessions WHERE token_hash='token'"
                ).fetchone()
                migrated_roles = [
                    row["role"]
                    for row in upgraded.execute(
                        "SELECT role FROM user_roles WHERE user_id=7 ORDER BY role"
                    )
                ]
                device = upgraded.execute(
                    "SELECT owner_user_id FROM devices WHERE id=3"
                ).fetchone()
                upgraded.execute(
                    "INSERT INTO users(username,username_normalized,display_name,password_hash,role) "
                    "VALUES('brother','brother','Brother','hash','member')"
                )
                foreign_key_issues = upgraded.execute("PRAGMA foreign_key_check").fetchall()
            self.assertEqual((account["id"], account["role"]), (7, "admin"))
            self.assertEqual(session["user_id"], 7)
            self.assertEqual(migrated_roles, ["admin"])
            self.assertIsNone(device["owner_user_id"])
            self.assertEqual(foreign_key_issues, [])

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
