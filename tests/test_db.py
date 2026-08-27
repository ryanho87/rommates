from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.db import Database


class DatabaseMigrationTests(unittest.TestCase):
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
            self.assertEqual(versions, {1, 2, 3, 4, 5})

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
