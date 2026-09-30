"""Account security: reset links hashed at rest, single use, killed by a password
change; logout only on POST.

Stdlib unittest against a throwaway SQLite file, like tests/test_forgot_throttle.py.
Run ONE FILE PER PROCESS (the backend is chosen at import time):
    .venv/bin/python3 -m pytest -q tests/test_account_security.py < /dev/null
"""

from __future__ import annotations

import os
import tempfile
import unittest

_TMP_DB = tempfile.NamedTemporaryFile(prefix="sporlos_test_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ["SPORLOS_DB"] = _TMP_DB.name
os.environ.pop("DATABASE_URL", None)

from starlette.testclient import TestClient  # noqa: E402

from app import store  # noqa: E402  (must follow the env setup above)
from app.auth import hash_password  # noqa: E402
from app.main import app  # noqa: E402

EMAIL = "eier@example.no"
PASSWORD = "riktig-hest-batteri"


def setUpModule():
    store.init_db()
    store.create_account("Test AS", EMAIL, hash_password(PASSWORD))


def tearDownModule():
    os.unlink(_TMP_DB.name)


class ResetTokenTest(unittest.TestCase):
    def _rows(self):
        with store._cursor() as cur:
            cur.execute("SELECT token FROM reset_tokens")
            return [r["token"] for r in cur.fetchall()]

    def test_token_is_not_stored_in_plaintext(self):
        token = store.create_reset_token(EMAIL)
        rows = self._rows()
        self.assertNotIn(token, rows)
        self.assertIn(store._reset_token_key(token), rows)

    def test_token_works_once(self):
        token = store.create_reset_token(EMAIL)
        self.assertEqual(store.pop_reset_token(token), EMAIL)
        self.assertIsNone(store.pop_reset_token(token))

    def test_stored_hash_is_not_a_valid_token(self):
        token = store.create_reset_token(EMAIL)
        self.assertIsNone(store.pop_reset_token(store._reset_token_key(token)))

    def test_password_change_invalidates_outstanding_links(self):
        token = store.create_reset_token(EMAIL)
        store.invalidate_reset_tokens(EMAIL)
        self.assertIsNone(store.pop_reset_token(token))

    def test_init_db_drops_legacy_plaintext_rows(self):
        with store._cursor() as cur:
            cur.execute(
                "INSERT INTO reset_tokens (token, email, expires_at) VALUES (?, ?, ?)",
                ("legacy-plaintext-token-43-chars-long-xxxxx", EMAIL, "2999-01-01 00:00:00"),
            )
        store.init_db()
        self.assertNotIn("legacy-plaintext-token-43-chars-long-xxxxx", self._rows())


class LogoutTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        r = self.client.post(
            "/login", data={"email": EMAIL, "password": PASSWORD}, follow_redirects=False
        )
        self.assertIn(r.status_code, (302, 303))

    def _logged_in(self):
        return self.client.get("/app", follow_redirects=False).status_code == 200

    def test_get_does_not_log_out(self):
        self.assertTrue(self._logged_in())
        r = self.client.get("/logout", follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn('method=post action="/logout"', r.text)
        self.assertTrue(self._logged_in())

    def test_post_logs_out(self):
        self.assertTrue(self._logged_in())
        r = self.client.post("/logout", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        self.assertFalse(self._logged_in())


if __name__ == "__main__":
    unittest.main()
