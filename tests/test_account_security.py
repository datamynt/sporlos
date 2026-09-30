"""Account security: reset links hashed at rest, single use, killed by a password
change; logout only on POST; session versions and max age; failed-login throttle;
client IP taken from the proxy-appended X-Forwarded-For entry only.

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


class SessionVersionTest(unittest.TestCase):
    def _client(self):
        c = TestClient(app)
        r = c.post("/login", data={"email": EMAIL, "password": PASSWORD}, follow_redirects=False)
        self.assertIn(r.status_code, (302, 303))
        return c

    def _in(self, c):
        return c.get("/app", follow_redirects=False).status_code == 200

    def test_password_change_ends_other_sessions_but_not_this_one(self):
        global PASSWORD
        a, b = self._client(), self._client()
        self.assertTrue(self._in(a) and self._in(b))
        new = PASSWORD + "-ny"
        r = a.post("/app/password", data={"old": PASSWORD, "new": new}, follow_redirects=False)
        self.assertIn("pw=ok", r.headers["location"])
        PASSWORD = new
        self.assertTrue(self._in(a))
        self.assertFalse(self._in(b))

    def test_reset_ends_every_session(self):
        global PASSWORD
        a = self._client()
        token = store.create_reset_token(EMAIL)
        new = PASSWORD + "-r"
        r = TestClient(app).post("/reset", data={"token": token, "password": new},
                                 follow_redirects=False)
        self.assertIn("reset=1", r.headers["location"])
        PASSWORD = new
        self.assertFalse(self._in(a))

    def test_session_older_than_max_age_is_rejected(self):
        import time as _time
        from app import main
        a = self._client()
        real = _time.time
        try:
            main.time.time = lambda: real() + main.SESSION_MAX_AGE + 60
            self.assertFalse(self._in(a))
        finally:
            main.time.time = real


class LoginThrottleTest(unittest.TestCase):
    def test_locks_after_repeated_failures_for_one_address(self):
        from app import main
        c = TestClient(app)
        target = "stranger@example.no"
        for _ in range(main.LOGIN_FAILS_PER_EMAIL_HOURLY):
            r = c.post("/login", data={"email": target, "password": "feil"})
            self.assertIn("Feil e-post eller passord", r.text)
        r = c.post("/login", data={"email": target, "password": "feil"})
        self.assertIn("For mange mislykkede", r.text)


class ClientIpTest(unittest.TestCase):
    def test_only_the_proxy_appended_entry_counts(self):
        from app.privacy import client_ip
        self.assertEqual(client_ip({"x-forwarded-for": "6.6.6.6, 203.0.113.9"}), "203.0.113.9")
        self.assertEqual(client_ip({"x-forwarded-for": "203.0.113.9"}), "203.0.113.9")
        self.assertEqual(client_ip({"x-real-ip": "6.6.6.6"}, fallback="10.0.0.1"), "10.0.0.1")


if __name__ == "__main__":
    unittest.main()
