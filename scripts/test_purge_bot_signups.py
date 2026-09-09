"""Tests for scripts/purge_bot_signups.py.

Runs against app.store's real SQLite backend (db/schema.sql's own SQLite
mirror via store.init_db()) so fixtures can't drift from the actual schema —
no hand-rolled mock tables. There is no existing test suite/framework
pinned in this repo (no pytest in requirements.txt), so this uses stdlib
unittest only — zero new dependencies.

Run:
    python3 -m unittest scripts.test_purge_bot_signups -v
"""

from __future__ import annotations

import argparse
import io
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

# Force the SQLite backend for these tests regardless of the ambient shell.
os.environ.pop("DATABASE_URL", None)

_REPO_ROOT = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = Path(__file__).resolve().parent
for p in (str(_REPO_ROOT), str(_SCRIPTS_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from app import store  # noqa: E402
import purge_bot_signups as purge  # noqa: E402


def _args(**overrides) -> argparse.Namespace:
    base = dict(
        min_id=purge.DEFAULT_MIN_ID,
        max_id=purge.DEFAULT_MAX_ID,
        start=purge.DEFAULT_START,
        end=purge.DEFAULT_END,
        apply=False,
        backup_dir="backups",
    )
    base.update(overrides)
    return argparse.Namespace(**base)


class PurgeBotSignupsTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._db_path = os.path.join(self._tmpdir.name, "test.db")
        self._backup_dir = os.path.join(self._tmpdir.name, "backups")
        store._DB_PATH = self._db_path
        store.init_db()

    def tearDown(self):
        self._tmpdir.cleanup()

    # -- fixture helpers ---------------------------------------------------

    def _insert_tenant(self, cur, tid: int, name: str = "t") -> None:
        cur.execute(f"INSERT INTO tenants (id, name) VALUES ({store.P}, {store.P})", (tid, name))

    def _insert_user(
        self,
        cur,
        uid: int,
        tenant_id: int,
        email: str,
        created_at: str,
        email_verified: int = 0,
    ) -> None:
        cur.execute(
            f"INSERT INTO users (id, tenant_id, email, password_hash, email_verified, created_at) "
            f"VALUES ({store.P}, {store.P}, {store.P}, {store.P}, {store.P}, {store.P})",
            (uid, tenant_id, email, "x", email_verified, created_at),
        )

    def _insert_site(self, cur, sid: int, tenant_id: int, domain: str) -> None:
        cur.execute(
            f"INSERT INTO sites (id, tenant_id, domain, public_id) VALUES "
            f"({store.P}, {store.P}, {store.P}, {store.P})",
            (sid, tenant_id, domain, f"pub{sid}"),
        )

    def _insert_event(self, cur, eid: int, site_id: int) -> None:
        cur.execute(
            f"INSERT INTO events (id, site_id, path, visitor_hash) VALUES "
            f"({store.P}, {store.P}, {store.P}, {store.P})",
            (eid, site_id, "/", "hash"),
        )

    def _full_bot_row(self, cur, uid: int, email: str, created_at: str) -> None:
        """A bot signup that also has a site + full FK graph under it, to
        exercise every delete path. Deliberately has NO `events` row — a
        tenant WITH events is real usage and must be refused (see
        test_refuses_when_tenant_has_recorded_events), so that path can't be
        exercised through --apply. event_items can still exist standalone
        (event_id is nullable)."""
        self._insert_tenant(cur, uid)
        self._insert_user(cur, uid, uid, email, created_at)
        self._insert_site(cur, uid, uid, f"bot{uid}.example")
        cur.execute(
            f"INSERT INTO event_items (site_id, event_id, name) VALUES ({store.P}, {store.P}, {store.P})",
            (uid, None, "widget"),
        )
        cur.execute(
            f"INSERT INTO goals (site_id, name, match_type, match_value) VALUES "
            f"({store.P}, {store.P}, {store.P}, {store.P})",
            (uid, "signup", "path", "/takk"),
        )
        cur.execute(
            f"INSERT INTO funnels (site_id, name, steps) VALUES ({store.P}, {store.P}, {store.P})",
            (uid, "f", "[]"),
        )
        cur.execute(
            f"INSERT INTO daily_rollups (site_id, day, pageviews) VALUES ({store.P}, {store.P}, {store.P})",
            (uid, "2026-07-20", 1),
        )
        cur.execute(
            f"INSERT INTO search_stats (site_id, day, source, dim) VALUES "
            f"({store.P}, {store.P}, {store.P}, {store.P})",
            (uid, "2026-07-20", "gsc", "total"),
        )
        cur.execute(
            f"INSERT INTO api_keys (tenant_id, label, prefix, key_hash) VALUES "
            f"({store.P}, {store.P}, {store.P}, {store.P})",
            (uid, "k", "pfx", f"hash{uid}"),
        )
        cur.execute(
            f"INSERT INTO reset_tokens (token, email, expires_at) VALUES ({store.P}, {store.P}, {store.P})",
            (f"tok{uid}", email, "2027-01-01 00:00:00"),
        )

    # -- selection ------------------------------------------------------

    def test_selects_only_the_matching_bot_range(self):
        with store._cursor() as cur:
            self._insert_tenant(cur, 1)
            self._insert_user(cur, 1, 1, "real@datamynt.no", "2026-07-01 00:00:00", email_verified=1)
            for uid in (2, 3, 4, 5):
                self._insert_tenant(cur, uid)
                self._insert_user(cur, uid, uid, f"bot{uid}@victim.example", "2026-07-21 12:00:00")
            # verified, otherwise in range -> must be excluded
            self._insert_tenant(cur, 6)
            self._insert_user(cur, 6, 6, "verified6@victim.example", "2026-07-21 12:00:00", email_verified=1)
            # unverified but outside the date window -> excluded
            self._insert_tenant(cur, 7)
            self._insert_user(cur, 7, 7, "early7@victim.example", "2026-07-19 12:00:00")
            # unverified, in window, but outside the id range -> excluded
            self._insert_tenant(cur, 70)
            self._insert_user(cur, 70, 70, "late70@victim.example", "2026-07-21 12:00:00")

        with store._cursor() as cur:
            got = purge.select_candidates(cur, 2, 69, "2026-07-20", "2026-07-22")
        self.assertEqual([u["id"] for u in got], [2, 3, 4, 5])

    def test_end_date_is_inclusive_of_the_whole_day(self):
        with store._cursor() as cur:
            self._insert_tenant(cur, 1)
            self._insert_user(cur, 1, 1, "real@datamynt.no", "2026-01-01 00:00:00", email_verified=1)
            self._insert_tenant(cur, 2)
            self._insert_user(cur, 2, 2, "start@victim.example", "2026-07-20 00:00:00")
            self._insert_tenant(cur, 3)
            self._insert_user(cur, 3, 3, "before-start@victim.example", "2026-07-19 23:59:59")
            self._insert_tenant(cur, 4)
            self._insert_user(cur, 4, 4, "end@victim.example", "2026-07-22 23:59:59")
            self._insert_tenant(cur, 5)
            self._insert_user(cur, 5, 5, "after-end@victim.example", "2026-07-23 00:00:00")

        with store._cursor() as cur:
            got = purge.select_candidates(cur, 2, 69, "2026-07-20", "2026-07-22")
        self.assertEqual([u["id"] for u in got], [2, 4])

    # -- safety net -------------------------------------------------------

    def test_refuses_if_id_1_is_in_the_selection(self):
        with store._cursor() as cur:
            self._insert_tenant(cur, 1)
            self._insert_user(cur, 1, 1, "real@datamynt.no", "2026-07-21 00:00:00")

        rc = purge.run(_args(min_id=1, backup_dir=self._backup_dir))
        self.assertEqual(rc, 1)
        with store._cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM users")
            self.assertEqual(dict(cur.fetchone())["n"], 1)

    def test_refuses_a_verified_user_passed_in_directly(self):
        with store._cursor() as cur:
            self._insert_tenant(cur, 6)
            self._insert_user(cur, 6, 6, "v@victim.example", "2026-07-21 00:00:00", email_verified=1)
            fake_candidate = [{"id": 6, "tenant_id": 6, "email": "v@victim.example", "email_verified": 1}]
            with self.assertRaises(purge.UnsafeSelection):
                purge.assert_safe_to_delete(cur, fake_candidate)

    def test_refuses_when_tenant_has_recorded_events(self):
        with store._cursor() as cur:
            self._insert_tenant(cur, 2)
            self._insert_user(cur, 2, 2, "used@victim.example", "2026-07-21 00:00:00")
            self._insert_site(cur, 2, 2, "used.example")
            self._insert_event(cur, 2, 2)

        rc = purge.run(_args(apply=True, backup_dir=self._backup_dir))
        self.assertEqual(rc, 1)
        with store._cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM users")
            self.assertEqual(dict(cur.fetchone())["n"], 1)
        self.assertFalse(os.path.isdir(self._backup_dir), "must not write a backup when refusing")

    def test_refuses_when_tenant_is_shared_with_a_user_outside_the_selection(self):
        with store._cursor() as cur:
            self._insert_tenant(cur, 2)
            self._insert_user(cur, 2, 2, "bot@victim.example", "2026-07-21 00:00:00")
            # a second user on the SAME tenant, outside the id/date selection
            self._insert_user(cur, 200, 2, "teammate@datamynt.no", "2026-01-01 00:00:00", email_verified=1)

        rc = purge.run(_args(backup_dir=self._backup_dir))
        self.assertEqual(rc, 1)

    # -- dry run vs apply ---------------------------------------------------

    def test_dry_run_changes_nothing(self):
        with store._cursor() as cur:
            self._full_bot_row(cur, 2, "bot2@victim.example", "2026-07-21 00:00:00")

        def _count(table):
            with store._cursor() as cur:
                cur.execute(f"SELECT COUNT(*) AS n FROM {table}")
                return dict(cur.fetchone())["n"]

        before = {t: _count(t) for t in ("tenants", "users", "sites", "events", "event_items",
                                          "goals", "funnels", "daily_rollups", "search_stats",
                                          "api_keys", "reset_tokens")}
        rc = purge.run(_args(apply=False, backup_dir=self._backup_dir))
        self.assertEqual(rc, 0)
        after = {t: _count(t) for t in before}
        self.assertEqual(before, after)
        self.assertFalse(os.path.isdir(self._backup_dir))

    def test_apply_deletes_the_full_fk_graph_and_only_the_matched_tenant(self):
        with store._cursor() as cur:
            self._full_bot_row(cur, 2, "bot2@victim.example", "2026-07-21 00:00:00")
            # control tenant: real usage, must survive untouched (also proves
            # cross-tenant isolation of the DELETE ... IN (...) clauses)
            self._full_bot_row(cur, 99, "real99@datamynt.no", "2026-01-01 00:00:00")
            cur.execute(f"UPDATE users SET email_verified = 1 WHERE id = {store.P}", (99,))
            self._insert_event(cur, 99, 99)  # real usage on the control tenant

        rc = purge.run(_args(apply=True, backup_dir=self._backup_dir))
        self.assertEqual(rc, 0)

        # (table, filter column) — all keyed off the bot fixture's id (2),
        # which _full_bot_row() used for both the tenant/user id and the
        # site id (site_id == tenant_id == 2 by construction above).
        checks = [
            ("tenants", "id"),
            ("users", "id"),
            ("api_keys", "tenant_id"),
            ("sites", "id"),
            ("events", "site_id"),
            ("event_items", "site_id"),
            ("goals", "site_id"),
            ("funnels", "site_id"),
            ("daily_rollups", "site_id"),
            ("search_stats", "site_id"),
        ]
        with store._cursor() as cur:
            for table, column in checks:
                cur.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {column} = {store.P}", (2,))
                self.assertEqual(dict(cur.fetchone())["n"], 0, f"{table} row for bot tenant 2 should be gone")
            cur.execute(f"SELECT COUNT(*) AS n FROM reset_tokens WHERE email = {store.P}", ("bot2@victim.example",))
            self.assertEqual(dict(cur.fetchone())["n"], 0)

            # control tenant untouched
            cur.execute(f"SELECT COUNT(*) AS n FROM users WHERE id = {store.P}", (99,))
            self.assertEqual(dict(cur.fetchone())["n"], 1)
            cur.execute(f"SELECT COUNT(*) AS n FROM events WHERE site_id = {store.P}", (99,))
            self.assertEqual(dict(cur.fetchone())["n"], 1)

    def test_apply_writes_a_0600_backup_before_deleting(self):
        with store._cursor() as cur:
            self._full_bot_row(cur, 2, "bot2@victim.example", "2026-07-21 00:00:00")

        rc = purge.run(_args(apply=True, backup_dir=self._backup_dir))
        self.assertEqual(rc, 0)

        files = list(Path(self._backup_dir).glob("purge_bot_signups_*.json"))
        self.assertEqual(len(files), 1)
        mode = stat.S_IMODE(files[0].stat().st_mode)
        self.assertEqual(mode, 0o600)
        content = files[0].read_text()
        self.assertIn("bot2@victim.example", content)

    # -- reporting never leaks raw e-mails ---------------------------------

    def test_email_digest_is_order_independent(self):
        users_a = [{"email": "b@x.example"}, {"email": "a@x.example"}]
        users_b = [{"email": "a@x.example"}, {"email": "b@x.example"}]
        self.assertEqual(purge.email_digest(users_a), purge.email_digest(users_b))

    def test_dry_run_report_never_prints_raw_emails(self):
        with store._cursor() as cur:
            self._full_bot_row(cur, 2, "bot2@victim.example", "2026-07-21 00:00:00")

        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = purge.run(_args(apply=False, backup_dir=self._backup_dir))
        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertNotIn("bot2@victim.example", out)
        self.assertNotIn("@victim.example", out)
        self.assertIn(purge.email_digest([{"email": "bot2@victim.example"}]), out)


if __name__ == "__main__":
    unittest.main()
