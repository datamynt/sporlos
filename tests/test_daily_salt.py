"""Stdlib unittest for the random daily salt, the hashed throttle keys and the
one-off blinding of legacy visitor hashes. SQLite backend, same setup as
tests/test_forgot_throttle.py.

Run with:
    .venv/bin/python3 -m unittest tests.test_daily_salt -v < /dev/null
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

_TMP_DB = tempfile.NamedTemporaryFile(prefix="sporlos_test_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ.setdefault("SPORLOS_DB", _TMP_DB.name)
os.environ.pop("DATABASE_URL", None)

from app import privacy, store  # noqa: E402  (must follow the env setup above)

SECRET = "test-secret"


def _reset():
    store.init_db()
    with store._cursor() as cur:
        for t in ("daily_salts", "signup_throttle", "forgot_throttle"):
            cur.execute(f"DROP TABLE IF EXISTS {t}")
        cur.execute("DELETE FROM event_items")
        cur.execute("DELETE FROM events")
    store._daily_salts_ready = False
    store._signup_throttle_ready = False
    store._forgot_throttle_ready = False
    store._salt_cache.clear()


def _site() -> int:
    tenant = store.create_tenant("Salt test")
    return store.create_site(tenant, f"salt-{os.urandom(4).hex()}.example")["id"]


def _event(site_id: int, vhash: str, ts: str) -> None:
    with store._cursor() as cur:
        cur.execute(
            "INSERT INTO events (site_id, ts, name, path, visitor_hash) VALUES (?, ?, 'pageview', '/', ?)",
            (site_id, ts, vhash),
        )


class DailySaltTest(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_salt_is_random_and_stable_within_the_day(self):
        now = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)
        a = store.daily_salt(now)
        self.assertEqual(len(a), 64)
        self.assertNotEqual(a, "2026-09-21")
        self.assertEqual(a, store.daily_salt(now + timedelta(hours=9)))
        # A second process (empty cache) reads the same row instead of making a new salt.
        store._salt_cache.clear()
        self.assertEqual(a, store.daily_salt(now))

    def test_new_day_discards_the_old_salt(self):
        day1 = datetime(2026, 9, 21, 23, 59, tzinfo=timezone.utc)
        a = store.daily_salt(day1)
        b = store.daily_salt(day1 + timedelta(minutes=2))
        self.assertNotEqual(a, b)
        with store._cursor() as cur:
            cur.execute("SELECT day, salt FROM daily_salts")
            rows = [dict(r) for r in cur.fetchall()]
        self.assertEqual(rows, [{"day": "2026-09-22", "salt": b}])
        self.assertNotIn("2026-09-21", store._salt_cache)

    def test_purge_old_salts(self):
        store.daily_salt()
        with store._cursor() as cur:
            cur.execute("INSERT INTO daily_salts (day, salt) VALUES ('2020-01-01', 'old')")
        self.assertEqual(store.purge_old_salts(), 1)
        with store._cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM daily_salts")
            self.assertEqual(cur.fetchone()["n"], 1)

    def test_switch_day_keeps_the_legacy_hash_when_events_exist_today(self):
        site_id = _site()
        now = datetime.now(timezone.utc)
        today = now.strftime("%Y-%m-%d")
        legacy_salt = hashlib.sha256(f"{SECRET}:{today}".encode()).hexdigest()
        legacy = hashlib.sha256(f"{legacy_salt}|1.2.3.4|UA|{site_id}".encode()).hexdigest()
        _event(site_id, legacy, now.strftime("%Y-%m-%d %H:%M:%S"))

        salt = store.daily_salt()
        self.assertEqual(salt, today)
        got = privacy.visitor_hash("1.2.3.4", "UA", str(site_id), secret=SECRET, day_salt=salt)
        self.assertEqual(got, legacy)

    def test_fresh_install_gets_a_random_salt(self):
        self.assertNotEqual(store.daily_salt(), datetime.now(timezone.utc).strftime("%Y-%m-%d"))

    def test_visitor_hash_requires_a_salt(self):
        with self.assertRaises(ValueError):
            privacy.visitor_hash("1.2.3.4", "UA", "1", secret=SECRET, day_salt="")
        with self.assertRaises(TypeError):
            privacy.visitor_hash("1.2.3.4", "UA", "1", secret=SECRET)  # type: ignore[call-arg]

    def test_hash_cannot_be_rebuilt_from_secret_and_date(self):
        now = datetime(2026, 9, 21, 10, 0, tzinfo=timezone.utc)
        h = privacy.visitor_hash("1.2.3.4", "UA", "1", secret=SECRET, day_salt=store.daily_salt(now))
        legacy_salt = hashlib.sha256(f"{SECRET}:2026-09-21".encode()).hexdigest()
        legacy = hashlib.sha256(f"{legacy_salt}|1.2.3.4|UA|1".encode()).hexdigest()
        self.assertNotEqual(h, legacy)


class ThrottleKeyTest(unittest.TestCase):
    def setUp(self):
        _reset()

    def test_no_raw_ip_reaches_the_throttle_tables(self):
        store.signup_bump("203.0.113.7")
        store.forgot_bump("203.0.113.7", "victim@example.no")
        with store._cursor() as cur:
            cur.execute("SELECT ip FROM signup_throttle")
            keys = [r["ip"] for r in cur.fetchall()]
            cur.execute("SELECT key FROM forgot_throttle")
            keys += [r["key"] for r in cur.fetchall()]
        self.assertTrue(keys)
        for k in keys:
            self.assertEqual(len(k), 64)
            self.assertNotIn("203.0.113.7", k)

    def test_counts_still_follow_the_ip(self):
        store.signup_bump("203.0.113.7")
        store.signup_bump("203.0.113.7")
        store.signup_bump("198.51.100.1")
        self.assertEqual(store.signup_attempts("203.0.113.7"), (2, 3))
        self.assertEqual(store.signup_attempts("192.0.2.9"), (0, 3))

    def test_raw_rows_from_before_are_removed(self):
        store.signup_bump("203.0.113.7")
        hour = store._signup_hour()
        with store._cursor() as cur:
            cur.execute("INSERT INTO signup_throttle (hour, ip, n) VALUES (?, '198.51.100.1', 4)", (hour,))
        store._signup_throttle_ready = False
        store.signup_attempts("203.0.113.7")
        with store._cursor() as cur:
            cur.execute("SELECT ip FROM signup_throttle")
            self.assertEqual([len(r["ip"]) for r in cur.fetchall()], [64])


class BlindLegacyHashesTest(unittest.TestCase):
    def setUp(self):
        _reset()
        self.site_id = _site()
        self.today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Yesterday-and-older: visitor A twice within a session, visitor B once.
        _event(self.site_id, "a" * 64, "2026-08-01 10:00:00")
        _event(self.site_id, "a" * 64, "2026-08-01 10:05:00")
        _event(self.site_id, "b" * 64, "2026-08-01 11:00:00")
        _event(self.site_id, "c" * 64, f"{self.today} 00:00:01")

    def _hashes(self):
        with store._cursor() as cur:
            cur.execute("SELECT visitor_hash FROM events ORDER BY ts")
            return [r["visitor_hash"] for r in cur.fetchall()]

    def test_dry_run_changes_nothing(self):
        before = self._hashes()
        self.assertEqual(
            store.blind_legacy_hashes(), {"days": 1, "events": 3, "applied": False}
        )
        self.assertEqual(self._hashes(), before)

    def test_apply_blinds_old_days_and_keeps_the_numbers(self):
        rollup_before = store.compute_rollup(self.site_id, "2026-08-01")
        res = store.blind_legacy_hashes(apply=True)
        self.assertEqual(res, {"days": 1, "events": 3, "applied": True})
        a1, a2, b, today = self._hashes()
        self.assertEqual(a1, a2)
        self.assertNotEqual(a1, b)
        self.assertNotIn(a1, ("a" * 64, "b" * 64))
        self.assertEqual(today, "c" * 64)  # today is never touched
        rollup_after = store.compute_rollup(self.site_id, "2026-08-01")
        self.assertEqual(rollup_after, rollup_before)


if __name__ == "__main__":
    unittest.main()
