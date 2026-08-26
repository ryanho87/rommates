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
            self.assertEqual(versions, {1, 2})


if __name__ == "__main__":
    unittest.main()
