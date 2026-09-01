from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


JOB_HISTORY_LIMIT = 500
ACTIVITY_HISTORY_LIMIT = 1000

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
    device_relpath TEXT NOT NULL DEFAULT '',
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

CREATE TABLE IF NOT EXISTS game_fingerprints (
    game_id INTEGER PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
    bundle_hash TEXT NOT NULL,
    crc32 TEXT NOT NULL,
    md5 TEXT NOT NULL,
    sha1 TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS game_metadata (
    game_id INTEGER PRIMARY KEY REFERENCES games(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    source_game_id TEXT NOT NULL,
    source_system_id INTEGER NOT NULL,
    match_method TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    release_date TEXT NOT NULL DEFAULT '',
    developer TEXT NOT NULL DEFAULT '',
    publisher TEXT NOT NULL DEFAULT '',
    players TEXT NOT NULL DEFAULT '',
    rating REAL,
    top_staff INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS game_assets (
    id INTEGER PRIMARY KEY,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    source TEXT NOT NULL,
    kind TEXT NOT NULL,
    media_type TEXT NOT NULL,
    local_relpath TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(game_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_game_assets_game ON game_assets(game_id);

CREATE TABLE IF NOT EXISTS platform_rankings (
    platform TEXT NOT NULL,
    rank INTEGER NOT NULL,
    source TEXT NOT NULL,
    source_game_id TEXT NOT NULL,
    slug TEXT NOT NULL,
    title TEXT NOT NULL,
    score REAL,
    rating REAL,
    ratings_count INTEGER NOT NULL DEFAULT 0,
    released TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(platform, rank),
    UNIQUE(platform, source, source_game_id)
);
CREATE INDEX IF NOT EXISTS idx_platform_rankings_platform ON platform_rankings(platform, rank);

CREATE TABLE IF NOT EXISTS upload_sessions (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    bundle_name TEXT NOT NULL DEFAULT '',
    folder_mode INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'uploading',
    total_size INTEGER NOT NULL,
    received_size INTEGER NOT NULL DEFAULT 0,
    file_count INTEGER NOT NULL,
    manifest_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_upload_sessions_status ON upload_sessions(status, updated_at);

CREATE TABLE IF NOT EXISTS upload_files (
    session_id TEXT NOT NULL REFERENCES upload_sessions(id) ON DELETE CASCADE,
    file_index INTEGER NOT NULL,
    relative_path TEXT NOT NULL,
    size INTEGER NOT NULL,
    received_size INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY(session_id, file_index),
    UNIQUE(session_id, relative_path)
);

CREATE TABLE IF NOT EXISTS download_tickets (
    token_hash TEXT PRIMARY KEY,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL,
    used_at INTEGER,
    requested_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_download_tickets_expiry ON download_tickets(expires_at);

CREATE TABLE IF NOT EXISTS naming_catalogs (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    platform TEXT NOT NULL,
    entry_count INTEGER NOT NULL DEFAULT 0,
    imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS naming_entries (
    id INTEGER PRIMARY KEY,
    catalog_id INTEGER NOT NULL REFERENCES naming_catalogs(id) ON DELETE CASCADE,
    canonical_name TEXT NOT NULL,
    extension TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    size INTEGER,
    sha256 TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_naming_entries_catalog ON naming_entries(catalog_id);
CREATE INDEX IF NOT EXISTS idx_naming_entries_hash ON naming_entries(sha256);
CREATE INDEX IF NOT EXISTS idx_naming_entries_name ON naming_entries(normalized_name);

CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    path TEXT NOT NULL UNIQUE,
    deployment_mode TEXT NOT NULL DEFAULT 'copy' CHECK(deployment_mode IN ('copy','hardlink')),
    owner_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    syncthing_ready_at TEXT,
    syncthing_ready_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS device_selections (
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    PRIMARY KEY(device_id, game_id)
);
CREATE INDEX IF NOT EXISTS idx_device_selections_game ON device_selections(game_id, device_id);

CREATE TABLE IF NOT EXISTS deployments (
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    relpath TEXT NOT NULL,
    PRIMARY KEY(device_id, game_id, relpath)
);
CREATE INDEX IF NOT EXISTS idx_deployments_game ON deployments(game_id, device_id);

CREATE TABLE IF NOT EXISTS device_inventory_files (
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    relpath TEXT NOT NULL,
    observed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(device_id, relpath)
);
CREATE INDEX IF NOT EXISTS idx_device_inventory_relpath ON device_inventory_files(relpath, device_id);

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
    progress_json TEXT,
    requested_by INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS job_issues (
    id INTEGER PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    detail TEXT NOT NULL,
    UNIQUE(job_id, detail)
);
CREATE INDEX IF NOT EXISTS idx_job_issues_job ON job_issues(job_id, id);

CREATE TABLE IF NOT EXISTS artwork_bulk_runs (
    id INTEGER PRIMARY KEY,
    asset_mode TEXT NOT NULL CHECK(asset_mode IN ('cover','full')),
    scope_type TEXT NOT NULL DEFAULT 'library' CHECK(scope_type IN ('library','platforms','games')),
    scope_label TEXT NOT NULL DEFAULT 'Entire library',
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','running','paused','complete','cancelled','failed')),
    total_games INTEGER NOT NULL DEFAULT 0,
    processed_games INTEGER NOT NULL DEFAULT 0,
    matched_games INTEGER NOT NULL DEFAULT 0,
    downloaded_assets INTEGER NOT NULL DEFAULT 0,
    skipped_games INTEGER NOT NULL DEFAULT 0,
    job_id INTEGER,
    last_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_artwork_bulk_runs_status ON artwork_bulk_runs(status, id);

CREATE TABLE IF NOT EXISTS artwork_bulk_items (
    run_id INTEGER NOT NULL REFERENCES artwork_bulk_runs(id) ON DELETE CASCADE,
    game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','complete','skipped')),
    issue TEXT NOT NULL DEFAULT '',
    PRIMARY KEY(run_id, game_id)
);
CREATE INDEX IF NOT EXISTS idx_artwork_bulk_items_pending ON artwork_bulk_items(run_id, status, game_id);

CREATE TABLE IF NOT EXISTS save_settings (
    id INTEGER PRIMARY KEY CHECK(id=1),
    enabled INTEGER NOT NULL DEFAULT 1,
    interval_minutes INTEGER NOT NULL DEFAULT 360,
    retention_recent INTEGER NOT NULL DEFAULT 24,
    retention_daily INTEGER NOT NULL DEFAULT 30,
    retention_weekly INTEGER NOT NULL DEFAULT 12,
    retention_monthly INTEGER NOT NULL DEFAULT 12,
    last_attempt_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS save_snapshots (
    id INTEGER PRIMARY KEY,
    trigger TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    tree_hash TEXT NOT NULL,
    file_count INTEGER NOT NULL,
    logical_bytes INTEGER NOT NULL,
    new_bytes INTEGER NOT NULL DEFAULT 0,
    added_count INTEGER NOT NULL DEFAULT 0,
    changed_count INTEGER NOT NULL DEFAULT 0,
    removed_count INTEGER NOT NULL DEFAULT 0,
    source_root TEXT NOT NULL DEFAULT '',
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_save_snapshots_created ON save_snapshots(id DESC);

CREATE TABLE IF NOT EXISTS save_snapshot_files (
    snapshot_id INTEGER NOT NULL REFERENCES save_snapshots(id) ON DELETE CASCADE,
    relpath TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime_ns INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    PRIMARY KEY(snapshot_id, relpath)
);
CREATE INDEX IF NOT EXISTS idx_save_snapshot_files_hash ON save_snapshot_files(sha256);

CREATE TABLE IF NOT EXISTS save_conflict_resolutions (
    id INTEGER PRIMARY KEY,
    canonical_relpath TEXT NOT NULL,
    conflict_relpath TEXT NOT NULL,
    device_id TEXT NOT NULL DEFAULT '',
    device_name TEXT NOT NULL DEFAULT '',
    decision TEXT NOT NULL CHECK(decision IN ('current','conflict')),
    canonical_sha256 TEXT NOT NULL DEFAULT '',
    conflict_sha256 TEXT NOT NULL,
    safety_snapshot_id INTEGER NOT NULL REFERENCES save_snapshots(id),
    resolved_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_save_conflict_resolutions_time
ON save_conflict_resolutions(id DESC);

CREATE TABLE IF NOT EXISTS notification_settings (
    id INTEGER PRIMARY KEY CHECK(id=1),
    enabled INTEGER NOT NULL DEFAULT 1,
    events_json TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS notification_deliveries (
    id INTEGER PRIMARY KEY,
    event TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL,
    path TEXT NOT NULL DEFAULT '',
    dedupe_key TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN ('queued','sent','failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_notification_deliveries_time
ON notification_deliveries(id DESC);
CREATE INDEX IF NOT EXISTS idx_notification_deliveries_dedupe
ON notification_deliveries(event,dedupe_key);

CREATE TABLE IF NOT EXISTS user_notifications (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    path TEXT NOT NULL DEFAULT '',
    dedupe_key TEXT NOT NULL DEFAULT '',
    read_at TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(user_id,dedupe_key)
);
CREATE INDEX IF NOT EXISTS idx_user_notifications_inbox
ON user_notifications(user_id,read_at,id DESC);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    username_normalized TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('viewer','contributor','member','admin')),
    active INTEGER NOT NULL DEFAULT 1,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS user_roles (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('viewer','contributor','member','admin')),
    PRIMARY KEY(user_id,role)
);
CREATE INDEX IF NOT EXISTS idx_user_roles_role ON user_roles(role,user_id);

CREATE TABLE IF NOT EXISTS user_onboarding (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tour_key TEXT NOT NULL,
    tour_version INTEGER NOT NULL DEFAULT 1,
    current_step INTEGER NOT NULL DEFAULT 0,
    dismissed INTEGER NOT NULL DEFAULT 0,
    completed INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(user_id,tour_key)
);

CREATE TABLE IF NOT EXISTS auth_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_auth_sessions_expiry ON auth_sessions(expires_at);

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
            if "progress_json" not in job_columns:
                connection.execute("ALTER TABLE jobs ADD COLUMN progress_json TEXT")
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(2)")
            # Older scanners kept at most 50 issue strings inside result_json. Preserve
            # those available details when upgrading; future scans write every issue
            # directly to job_issues as it is discovered.
            for row in connection.execute(
                "SELECT id,result_json FROM jobs WHERE kind='scan' AND result_json IS NOT NULL"
            ):
                try:
                    result = json.loads(row["result_json"])
                except (TypeError, json.JSONDecodeError):
                    continue
                skipped = result.get("skipped", []) if isinstance(result, dict) else []
                connection.executemany(
                    "INSERT OR IGNORE INTO job_issues(job_id,detail) VALUES(?,?)",
                    ((row["id"], str(detail)) for detail in skipped),
                )
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(3)")
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(4)")
            save_setting_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(save_settings)")
            }
            if "last_attempt_at" not in save_setting_columns:
                connection.execute("ALTER TABLE save_settings ADD COLUMN last_attempt_at TEXT")
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(5)")
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(6)")
            game_file_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(game_files)")
            }
            if "device_relpath" not in game_file_columns:
                connection.execute(
                    "ALTER TABLE game_files ADD COLUMN device_relpath TEXT NOT NULL DEFAULT ''"
                )
            # Populate both upgraded databases and any rows created by an older
            # application process before this migration completed.
            from .esde import esde_device_relpath

            rows = connection.execute(
                "SELECT gf.rowid,g.platform,gf.relpath FROM game_files gf "
                "JOIN games g ON g.id=gf.game_id WHERE gf.device_relpath=''"
            ).fetchall()
            connection.executemany(
                "UPDATE game_files SET device_relpath=? WHERE rowid=?",
                (
                    (esde_device_relpath(row["platform"], row["relpath"]), row["rowid"])
                    for row in rows
                ),
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_game_files_device_relpath ON game_files(device_relpath)"
            )
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(7)")
            metadata_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(game_metadata)")
            }
            if "rating" not in metadata_columns:
                connection.execute("ALTER TABLE game_metadata ADD COLUMN rating REAL")
            if "top_staff" not in metadata_columns:
                connection.execute(
                    "ALTER TABLE game_metadata ADD COLUMN top_staff INTEGER NOT NULL DEFAULT 0"
                )
            from .ratings import screenscraper_rating, screenscraper_top_staff

            metadata_rows = connection.execute(
                "SELECT game_id,raw_json FROM game_metadata WHERE source='screenscraper' AND rating IS NULL"
            ).fetchall()
            backfill: list[tuple[float | None, int, int]] = []
            for row in metadata_rows:
                try:
                    raw = json.loads(row["raw_json"] or "{}")
                except (TypeError, json.JSONDecodeError):
                    raw = {}
                if not isinstance(raw, dict):
                    raw = {}
                backfill.append(
                    (
                        screenscraper_rating(raw),
                        int(screenscraper_top_staff(raw)),
                        row["game_id"],
                    )
                )
            connection.executemany(
                "UPDATE game_metadata SET rating=?,top_staff=? WHERE game_id=?", backfill
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_game_metadata_rating ON game_metadata(rating)"
            )
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(8)")
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(9)")
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(10)")
            device_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(devices)")
            }
            if "deployment_mode" not in device_columns:
                connection.execute(
                    "ALTER TABLE devices ADD COLUMN deployment_mode TEXT NOT NULL DEFAULT 'copy'"
                )
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(11)")
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(12)")
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(13)")
            artwork_run_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(artwork_bulk_runs)")
            }
            if "scope_type" not in artwork_run_columns:
                connection.execute(
                    "ALTER TABLE artwork_bulk_runs ADD COLUMN scope_type TEXT NOT NULL DEFAULT 'library'"
                )
            if "scope_label" not in artwork_run_columns:
                connection.execute(
                    "ALTER TABLE artwork_bulk_runs ADD COLUMN scope_label TEXT NOT NULL DEFAULT 'Entire library'"
                )
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(14)")
            save_snapshot_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(save_snapshots)")
            }
            if "source_root" not in save_snapshot_columns:
                connection.execute(
                    "ALTER TABLE save_snapshots ADD COLUMN source_root TEXT NOT NULL DEFAULT ''"
                )
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(15)")
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(16)")
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(17)")
            upload_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(upload_sessions)")
            }
            for name, declaration in (
                ("owner_user_id", "INTEGER REFERENCES users(id)"),
                ("submitted_at", "TEXT"),
                ("reviewed_by", "INTEGER REFERENCES users(id)"),
                ("reviewed_at", "TEXT"),
                ("review_note", "TEXT NOT NULL DEFAULT ''"),
            ):
                if name not in upload_columns:
                    connection.execute(f"ALTER TABLE upload_sessions ADD COLUMN {name} {declaration}")
            download_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(download_tickets)")
            }
            if "requested_by" not in download_columns:
                connection.execute(
                    "ALTER TABLE download_tickets ADD COLUMN requested_by INTEGER REFERENCES users(id)"
                )
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(18)")
            user_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(users)")
            }
            if "must_change_password" not in user_columns:
                connection.execute(
                    "ALTER TABLE users ADD COLUMN must_change_password INTEGER NOT NULL DEFAULT 0"
                )
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(19)")

            # SQLite cannot alter a CHECK constraint in place. Rebuild only older
            # user tables so the new member role can be persisted without losing
            # accounts or their stable ids.
            users_sql_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
            ).fetchone()
            users_sql = str(users_sql_row["sql"] or "") if users_sql_row else ""
            if "'member'" not in users_sql:
                connection.commit()
                connection.execute("PRAGMA foreign_keys=OFF")
                connection.executescript(
                    """
                    BEGIN;
                    CREATE TABLE users_member_migration (
                        id INTEGER PRIMARY KEY,
                        username TEXT NOT NULL,
                        username_normalized TEXT NOT NULL UNIQUE,
                        display_name TEXT NOT NULL,
                        password_hash TEXT NOT NULL,
                        role TEXT NOT NULL CHECK(role IN ('viewer','contributor','member','admin')),
                        active INTEGER NOT NULL DEFAULT 1,
                        must_change_password INTEGER NOT NULL DEFAULT 0,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        last_login_at TEXT
                    );
                    INSERT INTO users_member_migration(
                        id,username,username_normalized,display_name,password_hash,role,
                        active,must_change_password,created_at,last_login_at
                    ) SELECT id,username,username_normalized,display_name,password_hash,role,
                        active,must_change_password,created_at,last_login_at FROM users;
                    DROP TABLE users;
                    ALTER TABLE users_member_migration RENAME TO users;
                    COMMIT;
                    """
                )
                connection.execute("PRAGMA foreign_keys=ON")

            device_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(devices)")
            }
            if "owner_user_id" not in device_columns:
                connection.execute(
                    "ALTER TABLE devices ADD COLUMN owner_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL"
                )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_devices_owner ON devices(owner_user_id,name)"
            )
            job_columns = {row["name"] for row in connection.execute("PRAGMA table_info(jobs)")}
            if "requested_by" not in job_columns:
                connection.execute(
                    "ALTER TABLE jobs ADD COLUMN requested_by INTEGER REFERENCES users(id) ON DELETE SET NULL"
                )
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(20)")

            # Roles are composable from version 21 onward. Keep users.role as a
            # compatibility summary while user_roles is the authorization source.
            connection.execute(
                "CREATE TABLE IF NOT EXISTS user_roles ("
                "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,"
                "role TEXT NOT NULL CHECK(role IN ('viewer','contributor','member','admin')),"
                "PRIMARY KEY(user_id,role))"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_roles_role ON user_roles(role,user_id)"
            )
            connection.execute(
                "INSERT OR IGNORE INTO user_roles(user_id,role) SELECT id,role FROM users"
            )
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(21)")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS user_onboarding ("
                "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,"
                "tour_key TEXT NOT NULL,tour_version INTEGER NOT NULL DEFAULT 1,"
                "current_step INTEGER NOT NULL DEFAULT 0,dismissed INTEGER NOT NULL DEFAULT 0,"
                "completed INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,"
                "PRIMARY KEY(user_id,tour_key))"
            )
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(22)")
            device_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(devices)")
            }
            if "syncthing_ready_at" not in device_columns:
                connection.execute("ALTER TABLE devices ADD COLUMN syncthing_ready_at TEXT")
            if "syncthing_ready_by" not in device_columns:
                connection.execute(
                    "ALTER TABLE devices ADD COLUMN syncthing_ready_by INTEGER REFERENCES users(id) ON DELETE SET NULL"
                )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS user_notifications ("
                "id INTEGER PRIMARY KEY,user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,"
                "kind TEXT NOT NULL,title TEXT NOT NULL,detail TEXT NOT NULL DEFAULT '',"
                "path TEXT NOT NULL DEFAULT '',dedupe_key TEXT NOT NULL DEFAULT '',read_at TEXT,"
                "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,UNIQUE(user_id,dedupe_key))"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_user_notifications_inbox "
                "ON user_notifications(user_id,read_at,id DESC)"
            )
            connection.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES(23)")

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

    def prune_history(self, keep_jobs: int = JOB_HISTORY_LIMIT, keep_activity: int = ACTIVITY_HISTORY_LIMIT) -> None:
        """Trim the jobs and activity logs so they cannot grow without bound.

        Both tables are append-only and the UI only ever shows the newest 100 rows,
        so anything past the retention window is unreachable history.
        """
        with self.write() as connection:
            connection.execute(
                "DELETE FROM jobs WHERE id NOT IN (SELECT id FROM jobs ORDER BY id DESC LIMIT ?)",
                (keep_jobs,),
            )
            connection.execute(
                "DELETE FROM activity WHERE id NOT IN (SELECT id FROM activity ORDER BY id DESC LIMIT ?)",
                (keep_activity,),
            )
