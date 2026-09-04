from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database
from .library import LibraryError


PUSH_EVENTS = (
    "new_build",
    "device_ready",
    "device_sync",
    "device_apply",
    "upload_approved",
    "upload_rejected",
)
MAX_ATTEMPTS = 6


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class APNsResult:
    status_code: int
    reason: str = ""

    @property
    def sent(self) -> bool:
        return self.status_code == 200

    @property
    def invalid_token(self) -> bool:
        return self.status_code == 410 or self.reason in {
            "BadDeviceToken",
            "DeviceTokenNotForTopic",
            "Unregistered",
        }

    @property
    def retryable(self) -> bool:
        return self.status_code in {429, 500, 503}


class APNsProvider:
    """Small token-authenticated APNs HTTP/2 client with no app-level SDK."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._jwt = ""
        self._jwt_created_at = 0
        self._client: Any | None = None
        self._lock = threading.Lock()

    @property
    def configured(self) -> bool:
        key_path = self.settings.apns_key_path
        return bool(
            key_path
            and key_path.is_file()
            and self.settings.apns_key_id
            and self.settings.apns_team_id
            and self.settings.apns_bundle_id
        )

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    def _token(self) -> str:
        now = int(time.time())
        if self._jwt and now - self._jwt_created_at < 50 * 60:
            return self._jwt
        key_path = self.settings.apns_key_path
        if not key_path:
            raise LibraryError("APNs signing key is not configured")
        try:
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import ec
            from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

            private_key = serialization.load_pem_private_key(Path(key_path).read_bytes(), password=None)
            header = _base64url(
                json.dumps(
                    {"alg": "ES256", "kid": self.settings.apns_key_id},
                    separators=(",", ":"),
                ).encode()
            )
            claims = _base64url(
                json.dumps(
                    {"iss": self.settings.apns_team_id, "iat": now},
                    separators=(",", ":"),
                ).encode()
            )
            unsigned = f"{header}.{claims}"
            der_signature = private_key.sign(unsigned.encode("ascii"), ec.ECDSA(hashes.SHA256()))
            r, s = decode_dss_signature(der_signature)
            signature = _base64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
        except (OSError, ValueError, TypeError) as exc:
            raise LibraryError(f"APNs signing key could not be loaded: {exc}") from exc
        self._jwt = f"{unsigned}.{signature}"
        self._jwt_created_at = now
        return self._jwt

    def send(self, device_token: str, payload: dict[str, object]) -> APNsResult:
        if not self.configured:
            raise LibraryError("APNs is not configured")
        with self._lock:
            import httpx

            if self._client is None:
                self._client = httpx.Client(
                    http2=True,
                    timeout=self.settings.apns_timeout_seconds,
                )
            host = (
                "https://api.sandbox.push.apple.com"
                if self.settings.apns_environment == "sandbox"
                else "https://api.push.apple.com"
            )
            response = self._client.post(
                f"{host}/3/device/{device_token}",
                headers={
                    "authorization": f"bearer {self._token()}",
                    "apns-topic": self.settings.apns_bundle_id,
                    "apns-push-type": "alert",
                    "apns-priority": "10",
                },
                content=json.dumps(payload, separators=(",", ":")).encode(),
            )
        reason = ""
        if response.content:
            try:
                reason = str(response.json().get("reason") or "")
            except (ValueError, AttributeError):
                reason = response.text[:200]
        return APNsResult(response.status_code, reason)


class MobilePushService:
    """Owns native installations and drains a durable SQLite APNs outbox."""

    def __init__(
        self,
        settings: Settings,
        db: Database,
        *,
        provider: APNsProvider | None = None,
        poll_seconds: float = 5.0,
    ) -> None:
        self.settings = settings
        self.db = db
        self.provider = provider or APNsProvider(settings)
        self.poll_seconds = max(0.1, float(poll_seconds))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def configured(self) -> bool:
        return self.provider.configured

    def start(self) -> None:
        with self.db.write() as connection:
            connection.execute(
                "UPDATE mobile_push_outbox SET status='pending',next_attempt_at=0 "
                "WHERE status='sending'"
            )
        if not self.configured or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="rommates-mobile-push",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=min(self.poll_seconds + 1, 3))
        self._thread = None
        self.provider.close()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.drain_once()
            except Exception as exc:
                self.db.activity("mobile_push", f"APNs outbox check failed: {exc}")
            self._wake.wait(self.poll_seconds)
            self._wake.clear()

    def preferences(self, user_id: int) -> dict[str, bool]:
        with self.db.connect() as connection:
            disabled = {
                str(row["kind"])
                for row in connection.execute(
                    "SELECT kind FROM mobile_push_preferences WHERE user_id=? AND enabled=0",
                    (user_id,),
                )
            }
        return {event: event not in disabled for event in PUSH_EVENTS}

    def update_preferences(self, user_id: int, events: dict[str, bool]) -> dict[str, bool]:
        unknown = sorted(set(events) - set(PUSH_EVENTS))
        if unknown:
            raise LibraryError(f"Unknown push notification event: {unknown[0]}")
        with self.db.write() as connection:
            for event, enabled in events.items():
                connection.execute(
                    "INSERT INTO mobile_push_preferences(user_id,kind,enabled,updated_at) "
                    "VALUES(?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(user_id,kind) DO UPDATE SET "
                    "enabled=excluded.enabled,updated_at=CURRENT_TIMESTAMP",
                    (user_id, event, int(enabled)),
                )
        return self.preferences(user_id)

    def register(
        self,
        user_id: int,
        installation_id: str,
        device_token: str,
        app_version: str,
        notifications_enabled: bool,
    ) -> dict[str, object]:
        token = device_token.strip().lower()
        with self.db.write() as connection:
            # An APNs token identifies one app install. Transfer it to the current
            # signed-in account if the user changed accounts on the same phone.
            connection.execute(
                "DELETE FROM mobile_installations WHERE bundle_id=? AND environment=? "
                "AND apns_token=? AND id<>?",
                (
                    self.settings.apns_bundle_id,
                    self.settings.apns_environment,
                    token,
                    installation_id,
                ),
            )
            connection.execute(
                "INSERT INTO mobile_installations("
                "id,user_id,apns_token,bundle_id,environment,app_version,notifications_enabled,updated_at) "
                "VALUES(?,?,?,?,?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(id) DO UPDATE SET "
                "user_id=excluded.user_id,apns_token=excluded.apns_token,bundle_id=excluded.bundle_id,"
                "environment=excluded.environment,app_version=excluded.app_version,"
                "notifications_enabled=excluded.notifications_enabled,updated_at=CURRENT_TIMESTAMP",
                (
                    installation_id,
                    user_id,
                    token,
                    self.settings.apns_bundle_id,
                    self.settings.apns_environment,
                    app_version.strip()[:50],
                    int(notifications_enabled),
                ),
            )
            row = connection.execute(
                "SELECT id,app_version,notifications_enabled,created_at,updated_at "
                "FROM mobile_installations WHERE id=?",
                (installation_id,),
            ).fetchone()
        result = dict(row)
        result["notifications_enabled"] = bool(result["notifications_enabled"])
        result["push_configured"] = self.configured
        return result

    def unregister(self, user_id: int, installation_id: str) -> bool:
        with self.db.write() as connection:
            cursor = connection.execute(
                "DELETE FROM mobile_installations WHERE id=? AND user_id=?",
                (installation_id, user_id),
            )
        return bool(cursor.rowcount)

    def notify_user(
        self,
        user_id: int | None,
        kind: str,
        title: str,
        detail: str,
        path: str,
        dedupe_key: str,
    ) -> int | None:
        if not user_id:
            return None
        with self.db.write() as connection:
            connection.execute(
                "INSERT INTO user_notifications(user_id,kind,title,detail,path,dedupe_key) "
                "VALUES(?,?,?,?,?,?) ON CONFLICT(user_id,dedupe_key) DO UPDATE SET "
                "title=excluded.title,detail=excluded.detail,path=excluded.path,"
                "read_at=NULL,created_at=CURRENT_TIMESTAMP",
                (user_id, kind, title, detail, path, dedupe_key),
            )
            notification_id = int(
                connection.execute(
                    "SELECT id FROM user_notifications WHERE user_id=? AND dedupe_key=?",
                    (user_id, dedupe_key),
                ).fetchone()["id"]
            )
            self._enqueue(connection, user_id, kind, notification_id)
        self._wake.set()
        return notification_id

    def enqueue_existing(self, user_id: int | None, dedupe_key: str) -> int:
        if not user_id:
            return 0
        with self.db.write() as connection:
            notification = connection.execute(
                "SELECT id,kind FROM user_notifications WHERE user_id=? AND dedupe_key=?",
                (user_id, dedupe_key),
            ).fetchone()
            if not notification:
                return 0
            queued = self._enqueue(
                connection,
                user_id,
                str(notification["kind"]),
                int(notification["id"]),
            )
        if queued:
            self._wake.set()
        return queued

    @staticmethod
    def _enqueue(connection: Any, user_id: int, kind: str, notification_id: int) -> int:
        preference = connection.execute(
            "SELECT enabled FROM mobile_push_preferences WHERE user_id=? AND kind=?",
            (user_id, kind),
        ).fetchone()
        if preference and not preference["enabled"]:
            return 0
        cursor = connection.execute(
            "INSERT OR IGNORE INTO mobile_push_outbox(notification_id,installation_id) "
            "SELECT ?,id FROM mobile_installations WHERE user_id=? AND notifications_enabled=1",
            (notification_id, user_id),
        )
        return max(0, int(cursor.rowcount))

    @staticmethod
    def _payload(row: Any) -> dict[str, object]:
        return {
            "aps": {
                "alert": {
                    "title": str(row["title"])[:120],
                    "body": str(row["detail"])[:240],
                },
                "sound": "default",
                "thread-id": str(row["kind"]),
            },
            "notification_id": int(row["notification_id"]),
            "kind": str(row["kind"]),
            "path": str(row["path"]),
        }

    def drain_once(self) -> bool:
        if not self.configured:
            return False
        now = int(time.time())
        with self.db.write() as connection:
            row = connection.execute(
                "SELECT o.id,o.notification_id,o.installation_id,o.attempt_count,"
                "i.apns_token,n.kind,n.title,n.detail,n.path "
                "FROM mobile_push_outbox o "
                "JOIN mobile_installations i ON i.id=o.installation_id "
                "JOIN user_notifications n ON n.id=o.notification_id "
                "WHERE o.status='pending' AND o.next_attempt_at<=? "
                "AND i.notifications_enabled=1 ORDER BY o.id LIMIT 1",
                (now,),
            ).fetchone()
            if not row:
                return False
            claimed = connection.execute(
                "UPDATE mobile_push_outbox SET status='sending',attempt_count=attempt_count+1,"
                "updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",
                (row["id"],),
            )
            if not claimed.rowcount:
                return False
        try:
            result = self.provider.send(str(row["apns_token"]), self._payload(row))
        except Exception as exc:
            result = APNsResult(503, str(exc)[:200])
        attempt_count = int(row["attempt_count"]) + 1
        with self.db.write() as connection:
            if result.sent:
                connection.execute(
                    "UPDATE mobile_push_outbox SET status='sent',response_code=200,error='',"
                    "sent_at=CURRENT_TIMESTAMP,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (row["id"],),
                )
            elif result.invalid_token:
                connection.execute(
                    "UPDATE mobile_installations SET notifications_enabled=0,updated_at=CURRENT_TIMESTAMP "
                    "WHERE id=?",
                    (row["installation_id"],),
                )
                connection.execute(
                    "UPDATE mobile_push_outbox SET status='failed',response_code=?,error=?,"
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (result.status_code, result.reason or "APNs rejected the device token", row["id"]),
                )
            elif result.retryable and attempt_count < MAX_ATTEMPTS:
                delay = min(15 * (2 ** (attempt_count - 1)), 15 * 60)
                connection.execute(
                    "UPDATE mobile_push_outbox SET status='pending',response_code=?,error=?,"
                    "next_attempt_at=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (result.status_code, result.reason, now + delay, row["id"]),
                )
            else:
                connection.execute(
                    "UPDATE mobile_push_outbox SET status='failed',response_code=?,error=?,"
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (result.status_code, result.reason or "APNs request failed", row["id"]),
                )
        return True
