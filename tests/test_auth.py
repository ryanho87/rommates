from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.auth import AuthService, hash_password, verify_password
from app.db import Database
from app.library import LibraryError


class AuthServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "rommates.db")
        self.db.initialize()
        self.auth = AuthService(self.db)
        self.auth.initialize()

    def tearDown(self):
        self.temp.cleanup()

    def test_passwords_are_salted_and_verified(self):
        first = hash_password("a-long-test-password")
        second = hash_password("a-long-test-password")
        self.assertNotEqual(first, second)
        self.assertTrue(verify_password("a-long-test-password", first))
        self.assertFalse(verify_password("wrong-password", first))

    def test_account_login_session_and_disable(self):
        user = self.auth.create_user(
            "brother", "Brother", "a-long-test-password", "contributor"
        )
        principal, token, _ = self.auth.authenticate(
            "BROTHER", "a-long-test-password", "127.0.0.1"
        )
        self.assertEqual(principal.role, "contributor")
        self.assertTrue(principal.must_change_password)
        self.assertEqual(self.auth.from_session(token).id, user["id"])
        updated = self.auth.change_password(
            user["id"], "a-long-test-password", "a-new-long-test-password", token
        )
        self.assertFalse(updated.must_change_password)
        self.assertFalse(self.auth.from_session(token).must_change_password)
        with self.assertRaisesRegex(LibraryError, "not accepted"):
            self.auth.authenticate("brother", "a-long-test-password", "old-password")
        self.assertEqual(
            self.auth.authenticate(
                "brother", "a-new-long-test-password", "new-password"
            )[0].id,
            user["id"],
        )
        self.auth.update_user(user["id"], active=False)
        self.assertIsNone(self.auth.from_session(token))

    def test_rejects_short_passwords_and_duplicate_usernames(self):
        with self.assertRaisesRegex(LibraryError, "at least 12"):
            self.auth.create_user("short", "Short", "too-short", "viewer")
        self.auth.create_user("Ryan", "Ryan", "a-long-test-password", "admin")
        with self.assertRaisesRegex(LibraryError, "already exists"):
            self.auth.create_user("ryan", "Other", "another-test-password", "viewer")

    def test_roles_combine_without_granting_admin(self):
        user = self.auth.create_user(
            "friend",
            "Friend",
            "a-long-test-password",
            ["contributor", "member"],
        )
        self.assertEqual(user["roles"], ["contributor", "member"])
        principal, _, _ = self.auth.authenticate(
            "friend", "a-long-test-password", "multi-role"
        )
        self.assertTrue(principal.has_role("contributor"))
        self.assertTrue(principal.has_role("member"))
        self.assertFalse(principal.has_role("admin"))
        self.auth.update_user(user["id"], roles=["viewer", "member"])
        updated = next(item for item in self.auth.list_users() if item["id"] == user["id"])
        self.assertEqual(updated["roles"], ["viewer", "member"])


if __name__ == "__main__":
    unittest.main()
