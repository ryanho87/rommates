from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY,
    platform TEXT NOT NULL,
    primary_relpath TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    extension TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    bundle_hash TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    mtime_ns INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_games_platform ON games(platform);
CREATE INDEX IF NOT EXISTS idx_games_hash ON games(bundle_hash);
CREATE INDEX IF NOT EXISTS idx_games_normalized ON games(normalized_name);

CREATE TABLE IF NOT EXISTS game_files (
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    relpath TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    kind TEXT NOT NULL,
    PRIMARY KEY(game_id, relpath)
);

CREATE TABLE IF NOT EXISTS file_cache (
    relpath TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    sha256 TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS device_selections (
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    PRIMARY KEY(device_id, game_id)
);

CREATE TABLE IF NOT EXISTS deployments (
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    relpath TEXT NOT NULL,
    PRIMARY KEY(device_id, game_id, relpath)
);

CREATE TABLE IF NOT EXISTS trash_items (
    id INTEGER PRIMARY KEY,
    original_relpath TEXT NOT NULL,
    trash_relpath TEXT NOT NULL,
    game_name TEXT NOT NULL,
    platform TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    deleted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    progress INTEGER NOT NULL DEFAULT 0,
    result_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS activity (
    id INTEGER PRIMARY KEY,
    action TEXT NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class Database:
    def __init__(self, path: Path):
        self.path = path
        self._write_lock = threading.RLock()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
            )
            connection.executescript(SCHEMA)
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(1)")
            job_columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
            if "result_json" not in job_columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN result_json TEXT")
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(2)")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        with self._write_lock:
            with self.connect() as connection:
                yield connection

    def activity(self, action: str, detail: str) -> None:
        with self.write() as connection:
            connection.execute(
                "INSERT INTO activity(action, detail) VALUES (?, ?)",
                (action, detail),
            )
