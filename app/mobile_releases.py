from __future__ import annotations

from typing import Any

from .db import Database
from .mobile_push import MobilePushService


class MobileReleaseService:
    """Stores native releases and announces newly published builds."""

    def __init__(self, db: Database, push: MobilePushService) -> None:
        self.db = db
        self.push = push

    @staticmethod
    def _payload(row: Any | None) -> dict[str, object] | None:
        return dict(row) if row is not None else None

    def manifest(self, current_build: int) -> dict[str, object]:
        with self.db.connect() as connection:
            latest = connection.execute(
                "SELECT build,version,notes,released_at FROM mobile_releases "
                "ORDER BY build DESC LIMIT 1"
            ).fetchone()
            current = connection.execute(
                "SELECT build,version,notes,released_at FROM mobile_releases WHERE build=?",
                (current_build,),
            ).fetchone()
        return {
            "latest": self._payload(latest),
            "current": self._payload(current),
        }

    def publish(self, build: int, version: str, notes: str) -> dict[str, object]:
        clean_version = version.strip()
        clean_notes = notes.strip()
        with self.db.write() as connection:
            existing = connection.execute(
                "SELECT 1 FROM mobile_releases WHERE build=?", (build,)
            ).fetchone()
            connection.execute(
                "INSERT INTO mobile_releases(build,version,notes) VALUES(?,?,?) "
                "ON CONFLICT(build) DO UPDATE SET version=excluded.version,notes=excluded.notes",
                (build, clean_version, clean_notes),
            )
            row = connection.execute(
                "SELECT build,version,notes,released_at FROM mobile_releases WHERE build=?",
                (build,),
            ).fetchone()
            user_ids = [
                int(item["user_id"])
                for item in connection.execute(
                    "SELECT DISTINCT i.user_id FROM mobile_installations i "
                    "JOIN users u ON u.id=i.user_id WHERE u.active=1"
                )
            ]

        notified = 0
        if existing is None:
            summary = next(
                (line.strip().lstrip("-• ") for line in clean_notes.splitlines() if line.strip()),
                "See what changed in this build.",
            )
            for user_id in user_ids:
                notification_id = self.push.notify_user(
                    user_id,
                    "new_build",
                    f"ROMmates build {build} is ready",
                    summary[:240],
                    f"release?build={build}",
                    f"mobile-release:{build}",
                )
                notified += int(notification_id is not None)

        return {
            "release": self._payload(row),
            "created": existing is None,
            "notified_users": notified,
        }
