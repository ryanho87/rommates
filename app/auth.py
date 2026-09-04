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
ROLE_PRIORITY = {role: index for index, role in enumerate(ROLES)}
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
    roles: tuple[str, ...] = ()
    impersonator_id: int | None = None
    impersonator_username: str = ""
    impersonator_display_name: str = ""
    # Bound to the server-issued session, not supplied by the client. This lets
    # the public native surface reject browser cookies and bootstrap credentials.
    session_kind: str = ""

    def has_role(self, role: str) -> bool:
        return role in (self.roles or (self.role,))

    def payload(self) -> dict[str, object]:
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "role": self.role,
            "roles": list(self.roles or (self.role,)),
            "bootstrap": self.bootstrap,
            "must_change_password": self.must_change_password,
            "impersonation": {
                "admin_id": self.impersonator_id,
                "admin_username": self.impersonator_username,
                "admin_display_name": self.impersonator_display_name,
            } if self.impersonator_id is not None else None,
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

    def authenticate(
        self,
        username: str,
        password: str,
        rate_key: str,
        *,
        client_name: str = "",
        session_kind: str = "web",
    ) -> tuple[Principal, str, int]:
        if session_kind not in {"web", "mobile"}:
            raise ValueError("session_kind must be web or mobile")
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
                "INSERT INTO auth_sessions(token_hash,user_id,expires_at,client_name,session_kind) "
                "VALUES(?,?,?,?,?)",
                (token_hash, row["id"], expires_at, client_name.strip()[:100], session_kind),
            )
            connection.execute(
                "UPDATE users SET last_login_at=CURRENT_TIMESTAMP WHERE id=?", (row["id"],)
            )
        principal = self._principal(row)
        return self._session_principal(principal, session_kind), token, expires_at

    def from_session(self, token: str) -> Principal | None:
        if not token:
            return None
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        now = int(time.time())
        impersonator = None
        with self.db.write() as connection:
            session = connection.execute(
                "SELECT user_id,impersonated_user_id,session_kind FROM auth_sessions "
                "WHERE token_hash=? AND expires_at>=?",
                (token_hash, now),
            ).fetchone()
            row = None
            if session:
                original = connection.execute(
                    "SELECT * FROM users WHERE id=? AND active=1", (session["user_id"],)
                ).fetchone()
                target_id = session["impersonated_user_id"]
                if target_id is not None and original and original["role"] == "admin":
                    row = connection.execute(
                        "SELECT * FROM users WHERE id=? AND active=1", (target_id,)
                    ).fetchone()
                    if row:
                        impersonator = original
                if not row:
                    row = original
                    if target_id is not None:
                        connection.execute(
                            "UPDATE auth_sessions SET impersonated_user_id=NULL WHERE token_hash=?",
                            (token_hash,),
                        )
            if row:
                connection.execute(
                    "UPDATE auth_sessions SET last_seen_at=CURRENT_TIMESTAMP WHERE token_hash=?",
                    (token_hash,),
                )
        if not row:
            return None
        principal = self._principal(row)
        if not impersonator:
            return self._session_principal(principal, str(session["session_kind"]))
        return Principal(
            id=principal.id,
            username=principal.username,
            display_name=principal.display_name,
            role=principal.role,
            bootstrap=principal.bootstrap,
            must_change_password=principal.must_change_password,
            roles=principal.roles,
            impersonator_id=int(impersonator["id"]),
            impersonator_username=str(impersonator["username"]),
            impersonator_display_name=str(impersonator["display_name"]),
            session_kind=str(session["session_kind"]),
        )

    @staticmethod
    def _session_principal(principal: Principal, session_kind: str) -> Principal:
        return Principal(
            id=principal.id,
            username=principal.username,
            display_name=principal.display_name,
            role=principal.role,
            bootstrap=principal.bootstrap,
            must_change_password=principal.must_change_password,
            roles=principal.roles,
            impersonator_id=principal.impersonator_id,
            impersonator_username=principal.impersonator_username,
            impersonator_display_name=principal.impersonator_display_name,
            session_kind=session_kind,
        )

    def begin_impersonation(self, token: str, actor_id: int, target_user_id: int) -> Principal:
        if not token:
            raise LibraryError("Sign in with an administrator account to use test view")
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.db.write() as connection:
            session = connection.execute(
                "SELECT user_id FROM auth_sessions WHERE token_hash=? AND expires_at>=?",
                (token_hash, int(time.time())),
            ).fetchone()
            if not session or int(session["user_id"]) != actor_id:
                raise LibraryError("Your administrator session could not be verified")
            actor = connection.execute(
                "SELECT * FROM users WHERE id=? AND active=1", (actor_id,)
            ).fetchone()
            target = connection.execute(
                "SELECT * FROM users WHERE id=? AND active=1", (target_user_id,)
            ).fetchone()
            if not actor or actor["role"] != "admin":
                raise LibraryError("Administrator access is required")
            if not target:
                raise LibraryError("The account is disabled or no longer exists")
            target_roles = {
                row["role"]
                for row in connection.execute(
                    "SELECT role FROM user_roles WHERE user_id=?", (target_user_id,)
                )
            } or {str(target["role"])}
            if "admin" in target_roles:
                raise LibraryError("Choose a non-administrator account for test view")
            connection.execute(
                "UPDATE auth_sessions SET impersonated_user_id=? WHERE token_hash=?",
                (target_user_id, token_hash),
            )
        principal = self.from_session(token)
        if not principal or principal.impersonator_id != actor_id:
            raise LibraryError("Could not enter test view")
        return principal

    def end_impersonation(self, token: str) -> Principal:
        if not token:
            raise LibraryError("The administrator session could not be found")
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with self.db.write() as connection:
            session = connection.execute(
                "SELECT impersonated_user_id FROM auth_sessions WHERE token_hash=? AND expires_at>=?",
                (token_hash, int(time.time())),
            ).fetchone()
            if not session or session["impersonated_user_id"] is None:
                raise LibraryError("Test view is not active")
            connection.execute(
                "UPDATE auth_sessions SET impersonated_user_id=NULL WHERE token_hash=?",
                (token_hash,),
            )
        principal = self.from_session(token)
        if not principal:
            raise LibraryError("The administrator session could not be restored")
        return principal

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
            roles_by_user: dict[int, list[str]] = {int(row["id"]): [] for row in rows}
            for role_row in connection.execute(
                "SELECT user_id,role FROM user_roles ORDER BY user_id,role"
            ):
                roles_by_user.setdefault(int(role_row["user_id"]), []).append(role_row["role"])
        result = []
        for row in rows:
            item = dict(row)
            roles = self._normalize_roles(roles_by_user.get(int(row["id"])) or [row["role"]])
            item.update(
                roles=roles,
                active=bool(row["active"]),
                must_change_password=bool(row["must_change_password"]),
            )
            result.append(item)
        return result

    def create_user(
        self,
        username: str,
        display_name: str,
        password: str,
        roles: str | list[str],
    ) -> dict[str, object]:
        username = username.strip()
        normalized = username.casefold()
        if not username or len(username) > 64 or any(char.isspace() for char in username):
            raise LibraryError("Username must be 1 to 64 characters without spaces")
        normalized_roles = self._normalize_roles([roles] if isinstance(roles, str) else roles)
        primary_role = max(normalized_roles, key=ROLE_PRIORITY.__getitem__)
        password_hash = hash_password(password)
        with self.db.write() as connection:
            try:
                connection.execute(
                    "INSERT INTO users(username,username_normalized,display_name,password_hash,role,must_change_password) "
                    "VALUES(?,?,?,?,?,1)",
                    (username, normalized, display_name.strip()[:100] or username, password_hash, primary_role),
                )
            except Exception as exc:
                if "UNIQUE" in str(exc).upper():
                    raise LibraryError("That username already exists") from exc
                raise
            user_id = connection.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            connection.executemany(
                "INSERT INTO user_roles(user_id,role) VALUES(?,?)",
                ((user_id, role) for role in normalized_roles),
            )
        return next(item for item in self.list_users() if item["id"] == user_id)

    def update_user(
        self,
        user_id: int,
        *,
        role: str | None = None,
        roles: list[str] | None = None,
        active: bool | None = None,
        password: str = "",
        actor_id: int | None = None,
    ) -> dict[str, object]:
        if role is not None and roles is not None:
            raise LibraryError("Choose roles once")
        requested_roles = roles if roles is not None else ([role] if role is not None else None)
        normalized_roles = self._normalize_roles(requested_roles) if requested_roles is not None else None
        with self.db.write() as connection:
            current = connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            if not current:
                raise LibraryError("User was not found")
            current_roles = {
                row["role"]
                for row in connection.execute(
                    "SELECT role FROM user_roles WHERE user_id=?", (user_id,)
                )
            } or {str(current["role"])}
            next_roles = set(normalized_roles) if normalized_roles is not None else current_roles
            next_role = max(next_roles, key=ROLE_PRIORITY.__getitem__)
            next_active = bool(current["active"]) if active is None else active
            if actor_id == user_id and (not next_active or "admin" not in next_roles):
                raise LibraryError("You cannot remove your own administrator access")
            if "admin" in current_roles and current["active"] and (
                not next_active or "admin" not in next_roles
            ):
                remaining = connection.execute(
                    "SELECT COUNT(DISTINCT u.id) AS count FROM users u "
                    "JOIN user_roles ur ON ur.user_id=u.id "
                    "WHERE ur.role='admin' AND u.active=1 AND u.id<>?",
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
            if normalized_roles is not None:
                connection.execute("DELETE FROM user_roles WHERE user_id=?", (user_id,))
                connection.executemany(
                    "INSERT INTO user_roles(user_id,role) VALUES(?,?)",
                    ((user_id, assigned_role) for assigned_role in normalized_roles),
                )
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

    def update_profile(self, user_id: int, username: str, display_name: str) -> Principal:
        username = username.strip()
        normalized = username.casefold()
        if not username or len(username) > 64 or any(char.isspace() for char in username):
            raise LibraryError("Username must be 1 to 64 characters without spaces")
        display_name = display_name.strip()[:100] or username
        with self.db.write() as connection:
            current = connection.execute(
                "SELECT * FROM users WHERE id=? AND active=1", (user_id,)
            ).fetchone()
            if not current:
                raise LibraryError("User was not found")
            try:
                connection.execute(
                    "UPDATE users SET username=?,username_normalized=?,display_name=? WHERE id=?",
                    (username, normalized, display_name, user_id),
                )
            except Exception as exc:
                if "UNIQUE" in str(exc).upper():
                    raise LibraryError("That username already exists") from exc
                raise
            updated = connection.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return self._principal(updated)

    @staticmethod
    def _normalize_roles(roles) -> list[str]:
        unique = {str(role) for role in roles} if roles else set()
        if not unique or any(role not in ROLES for role in unique):
            raise LibraryError("Choose at least one valid role")
        return sorted(unique, key=ROLE_PRIORITY.__getitem__)

    def _principal(self, row) -> Principal:
        with self.db.connect() as connection:
            roles = [
                role_row["role"]
                for role_row in connection.execute(
                    "SELECT role FROM user_roles WHERE user_id=?", (row["id"],)
                )
            ]
        normalized_roles = self._normalize_roles(roles or [row["role"]])
        primary_role = max(normalized_roles, key=ROLE_PRIORITY.__getitem__)
        return Principal(
            id=int(row["id"]),
            username=str(row["username"]),
            display_name=str(row["display_name"]),
            role=primary_role,
            must_change_password=bool(row["must_change_password"]),
            roles=tuple(normalized_roles),
        )
