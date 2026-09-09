"""Stdlib unittest for /forgot's throttle store functions, run against a
throwaway SQLite file (same idea as store.py's own "backend velges av
DATABASE_URL" fallback — no Postgres needed for this).

There is no pytest suite in this repo (see CONTRIBUTING.md: the accepted gate
is `python -m py_compile`), so this mirrors what /signup's throttle would need
if it had a test: a plain unittest against the store functions.

Run with:
    .venv/bin/python3 -m unittest tests.test_forgot_throttle -v < /dev/null
"""

from __future__ import annotations

import os
import tempfile
import unittest

# Backend is selected at import time from these env vars (store.py: _DSN/_DB_PATH),
# so they must be set BEFORE `app.store` is imported anywhere in the process.
_TMP_DB = tempfile.NamedTemporaryFile(prefix="sporlos_test_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ["SPORLOS_DB"] = _TMP_DB.name
os.environ.pop("DATABASE_URL", None)

from app import store  # noqa: E402  (must follow the env setup above)


class ForgotThrottleTest(unittest.TestCase):
    def setUp(self):
        # Fresh table per test so counts don't leak across tests in this hour bucket.
        with store._cursor() as cur:
            cur.execute("DROP TABLE IF EXISTS forgot_throttle")
        store._forgot_throttle_ready = False

    def test_fresh_state_is_zero(self):
        used_ip, used_email, total = store.forgot_attempts("1.2.3.4", "victim@example.no")
        self.assertEqual((used_ip, used_email, total), (0, 0, 0))

    def test_bump_increments_ip_and_email_buckets(self):
        store.forgot_bump("1.2.3.4", "victim@example.no")
        used_ip, used_email, total = store.forgot_attempts("1.2.3.4", "victim@example.no")
        self.assertEqual((used_ip, used_email, total), (1, 1, 1))

    def test_per_ip_cap_independent_of_email(self):
        # Same attacker IP, different (guessed) target addresses — the IP bucket
        # climbs regardless of which address was targeted.
        for n, addr in enumerate(["a@example.no", "b@example.no", "c@example.no"], start=1):
            store.forgot_bump("9.9.9.9", addr)
            used_ip, _, total = store.forgot_attempts("9.9.9.9", addr)
            self.assertEqual(used_ip, n)
            self.assertEqual(total, n)

    def test_per_email_cap_independent_of_ip(self):
        # One victim hammered from many source IPs (distributed) — the per-email
        # bucket is what actually catches this, since per-IP alone would not.
        for n, ip in enumerate(["10.0.0.1", "10.0.0.2", "10.0.0.3"], start=1):
            store.forgot_bump(ip, "victim@example.no")
            _, used_email, total = store.forgot_attempts(ip, "victim@example.no")
            self.assertEqual(used_email, n)
            self.assertEqual(total, n)

    def test_email_key_is_case_and_whitespace_insensitive(self):
        store.forgot_bump("1.2.3.4", "Victim@Example.NO")
        used_ip, used_email, total = store.forgot_attempts("1.2.3.4", " victim@example.no ")
        self.assertEqual(used_email, 1, "same address should hit the same bucket regardless of case/whitespace")

    def test_email_hash_never_contains_raw_address(self):
        h = store.forgot_email_hash("victim@example.no")
        self.assertNotIn("victim", h)
        self.assertNotIn("example.no", h)
        self.assertEqual(len(h), 64, "sha256 hexdigest")
        # Deterministic, so the DB key and a log line computed separately still match.
        self.assertEqual(h, store.forgot_email_hash("victim@example.no"))

    def test_old_hour_rows_are_purged_on_read(self):
        store._ensure_forgot_throttle()  # table is created lazily; force it before the raw INSERT
        with store._cursor() as cur:
            cur.execute(
                "INSERT INTO forgot_throttle (hour, kind, key, n) VALUES (?, 'ip', '5.5.5.5', 99)",
                ("2000-01-01T00",),
            )
        used_ip, _, total = store.forgot_attempts("5.5.5.5", "someone@example.no")
        self.assertEqual(used_ip, 0, "stale hour bucket must not leak into the current hour's count")
        self.assertEqual(total, 0)

    def test_signup_throttle_unaffected_by_forgot_table(self):
        # Regression: the two throttles are separate tables (forgot_throttle is
        # NOT a reuse of signup_throttle), so bumping one must not move the other.
        store.forgot_bump("1.2.3.4", "victim@example.no")
        used, total = store.signup_attempts("1.2.3.4")
        self.assertEqual((used, total), (0, 0))


if __name__ == "__main__":
    try:
        unittest.main()
    finally:
        try:
            os.unlink(_TMP_DB.name)
        except OSError:
            pass
