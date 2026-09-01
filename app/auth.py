from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass

from .db import Database
from .library import LibraryError


ROLES = ("viewer", "contributor", "member", "admin")
SESSION_SECONDS = 30 * 24 * 60 * 60
PASSWORD_MIN_LENGTH = 12


@dataclass(frozen=True)
class Principal:
    id: int | None
    username: str
    display_name: str
    role: str
    bootstrap: bool = False
    must_change_password: bool = False

    def payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
            "bootstrap": self.bootstrap,
            "must_change_password": self.must_change_password,
        }


def hash_password(password: str) -> str:
    if len(password) < PASSWORD_MIN_LENGTH:
        raise LibraryError(f"Passwords must contain at least {PASSWORD_MIN_LENGTH} characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$16384$8$1$" + base64.urlsafe_b64encode(salt).decode() + "$" + base64.urlsafe_b64encode(digest).decode()


def verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(),
            salt=base64.urlsafe_b64decode(salt),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=32,
        )
        return hmac.compare_digest(digest, base64.urlsafe_b64decode(expected))
    except (ValueError, TypeError):
        return False


class AuthService:
    def __init__(self, db: Database):
        self.db = db
        self._attempts: dict[str, list[float]] = {}
        self._attempt_lock = threading.Lock()

    def initialize(self) -> None:
        with self.db.write() as connection:
            connection.execute("DELETE FROM auth_sessions WHERE expires_at<?", (int(time.time()),))

    def _check_rate_limit(self, key: str) -> None:
        now = time.monotonic()
        with self._attempt_lock:
            recent = [stamp for stamp in self._attempts.get(key, []) if now - stamp < 900]
            if len(recent) >= 10:
                raise LibraryError("Too many login attempts. Try again in 15 minutes")
            recent.append(now)
            self._attempts[key] = recent

    def _clear_attempts(self, key: str) -> None:
        with self._attempt_lock:
            self._attempts.pop(key, None)

    def authenticate(self, username: str, password: str, rate_key: str) -> tuple[Principal, str, int]:
        self._check_rate_limit(rate_key)
        normalized = username.strip().casefold()
        with self.db.connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE username_normalized=? AND active=1", (normalized,)
            ).fetchone()
        if not row or not verify_password(password, row["password_hash"]):
            raise LibraryError("Username or password was not accepted")
        self._clear_attempts(rate_key)
        token = secrets.token_urlsafe(32)
        expires_at = int(time.time()) + SESSION_SECONDS
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.db.write() as connection:
            connection.execute(
                "INSERT INTO auth_sessions(token_hash,user_id,expires_at) VALUES(?,?,?)",
                (token_hash, row["id"], expires_at),
            )
            connection.execute(
                "UPDATE users SET last_login_at=CURRENT_TIMESTAMP WHERE id=?", (row["id"],)
            )
        return self._principal(row), token, expires_at

    def from_session(self, token: str) -> Principal | None:
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now = int(time.time())
        with self.db.write() as connection:
            row = connection.execute(
                "SELECT u.* FROM auth_sessions s JOIN users u ON u.id=s.user_id "
                "WHERE s.token_hash=? AND s.expires_at>=? AND u.active=1",
                (token_hash, now),
            ).fetchone()
            if row:
                connection.execute(
                    "UPDATE auth_sessions SET last_seen_at=CURRENT_TIMESTAMP WHERE token_hash=?",
                    (token_hash,),
                )
        return self._principal(row) if row else None

    def logout(self, token: str) -> None:
        if not token:
            return
        with self.db.write() as connection:
            connection.execute(
                "DELETE FROM auth_sessions WHERE token_hash=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            )

    def list_users(self) -> list[dict[str, object]]:
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT id,username,display_name,role,active,must_change_password,created_at,last_login_at "
                "FROM users ORDER BY active DESC,username COLLATE NOCASE"
            ).fetchall()
        return [
            {
                **dict(row),
                "active": bool(row["active"]),
                "must_change_password": bool(row["must_change_password"]),
            }
            for row in rows
        ]

    def create_user(self, username: str, display_name: str, password: str, role: str) -> dict[str, object]:
        username = username.strip()
        normalized = username.casefold()
        if not username or len(username) > 64 or any(char.isspace() for char in username):
            raise LibraryError("Username must be 1 to 64 characters without spaces")
        if role not in ROLES:
            raise LibraryError("Choose a valid role")
        password_hash = hash_password(password)
        with self.db.write() as connection:
            try:
                connection.execute(
                    "INSERT INTO users(username,username_normalized,display_name,password_hash,role,must_change_password) "
                    "VALUES(?,?,?,?,?,1)",
                    (username, normalized, display_name.strip()[:100] or username, password_hash, role),
                )
            except Exception as exc:
                if "UNIQUE" in str(exc).upper():
                    raise LibraryError("That username already exists") from exc
                raise
            user_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        return next(item for item in self.list_users() if item["id"] == user_id)

    def update_user(
        self,
        user_id: int,
        *,
        role: str | None = None,
        active: bool | None = None,
        password: str = "",
        actor_id: int | None = None,
    ) -> dict[str, object]:
        if role is not None and role not in ROLES:
            raise LibraryError("Choose a valid role")
        with self.db.write() as connection:
            current = connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not current:
                raise LibraryError("User was not found")
            next_role = role or current["role"]
            next_active = bool(current["active"]) if active is None else active
            if actor_id == user_id and (not next_active or next_role != "admin"):
                raise LibraryError("You cannot remove your own administrator access")
            if current["role"] == "admin" and current["active"] and (not next_active or next_role != "admin"):
                remaining = connection.execute(
                    "SELECT COUNT(*) AS count FROM users WHERE role='admin' AND active=1 AND id<>?",
                    (user_id,),
                ).fetchone()["count"]
                if not remaining and actor_id is not None:
                    raise LibraryError("At least one active administrator account is required")
            updates = ["role=?", "active=?"]
            values: list[object] = [next_role, int(next_active)]
            if password:
                updates.append("password_hash=?")
                values.append(hash_password(password))
                updates.append("must_change_password=1")
            values.append(user_id)
            connection.execute(f"UPDATE users SET {','.join(updates)} WHERE id=?", values)
            if not next_active or password:
                connection.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
        return next(item for item in self.list_users() if item["id"] == user_id)

    def change_password(
        self,
        user_id: int,
        current_password: str,
        new_password: str,
        session_token: str,
    ) -> Principal:
        if not session_token:
            raise LibraryError("Sign in with your ROMmates account to change its password")
        with self.db.write() as connection:
            current = connection.execute(
                "SELECT * FROM users WHERE id=? AND active=1", (user_id,)
            ).fetchone()
            if not current or not verify_password(current_password, current["password_hash"]):
                raise LibraryError("Current password was not accepted")
            if verify_password(new_password, current["password_hash"]):
                raise LibraryError("Choose a password you have not already been using")
            password_hash = hash_password(new_password)
            connection.execute(
                "UPDATE users SET password_hash=?,must_change_password=0 WHERE id=?",
                (password_hash, user_id),
            )
            current_token_hash = hashlib.sha256(session_token.encode()).hexdigest()
            connection.execute(
                "DELETE FROM auth_sessions WHERE user_id=? AND token_hash<>?",
                (user_id, current_token_hash),
            )
            updated = connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return self._principal(updated)

    @staticmethod
    def _principal(row) -> Principal:
        return Principal(
            id=int(row["id"]),
            username=str(row["username"]),
            display_name=str(row["display_name"]),
            role=str(row["role"]),
            must_change_password=bool(row["must_change_password"]),
        )
