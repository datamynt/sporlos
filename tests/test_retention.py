"""Retention job (`manage purge-dormant`): accounts whose customer relationship ended are
deleted within the 90 days the terms (§10) and the DPA (§8) promise.

Covered: the cutoff boundaries per category; a dry run changes nothing; enforce deletes
exactly the dormant accounts and keeps every account with money moving or a live plan;
one account's deletion never touches another's rows; a second run is a no-op; output
and the audit row carry counts only.

Run ONE FILE PER PROCESS (the backend is chosen at import). SQLite by default; against a
throwaway Postgres with SPORLOS_TEST_PG (see tests/test_pg_pool.py):
    .venv/bin/python3 -m pytest -q tests/test_retention.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

_PG = os.environ.get("SPORLOS_TEST_PG")
_TMP_DB = tempfile.NamedTemporaryFile(prefix="sporlos_test_", suffix=".db", delete=False)
_TMP_DB.close()
if _PG:
    os.environ["DATABASE_URL"] = _PG
else:
    os.environ.pop("DATABASE_URL", None)
    os.environ["SPORLOS_DB"] = _TMP_DB.name

from app import manage, store  # noqa: E402

P = store.P
NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)
ENDED_CUT = NOW - timedelta(days=store.RETENTION_ENDED_DAYS)
UNVERIFIED_CUT = NOW - timedelta(days=store.RETENTION_UNVERIFIED_DAYS)
SECOND = timedelta(seconds=1)


def ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def setUpModule():
    store.init_db()


def tearDownModule():
    os.unlink(_TMP_DB.name)


def _set(tid: int, **cols) -> None:
    sets = ", ".join(f"{k} = {P}" for k in cols)
    with store._cursor() as cur:
        cur.execute(f"UPDATE tenants SET {sets} WHERE id = {P}", (*cols.values(), tid))


_n = [0]


def account(*, verified: bool = True, created: datetime | None = None,
            trial_ends: datetime | None = None, **cols) -> int:
    _n[0] += 1
    tid, uid = store.create_account(f"Firma {_n[0]}", f"u{_n[0]}@example.no", "x")
    if verified:
        store.set_email_verified(uid)
    _set(tid, created_at=ts(created or NOW - timedelta(days=200)),
         trial_ends_at=ts(trial_ends or NOW + timedelta(days=10)), **cols)
    return tid


def with_site(tid: int, events: bool = True) -> int:
    _n[0] += 1
    site = store.create_site(tid, f"site{_n[0]}.no")
    if events:
        store.insert_event(site["id"], {"name": "pageview", "path": "/", "visitor_hash": "v1"})
    return site["id"]


def exists(tid: int) -> bool:
    with store._cursor() as cur:
        cur.execute(f"SELECT 1 FROM tenants WHERE id = {P}", (tid,))
        return cur.fetchone() is not None


def rows(tid: int) -> dict[str, int]:
    out = {}
    with store._cursor() as cur:
        for table, where in (("tenants", "id"), ("users", "tenant_id"), ("sites", "tenant_id")):
            cur.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {where} = {P}", (tid,))
            out[table] = int(cur.fetchone()["n"])
        cur.execute(
            f"SELECT COUNT(*) AS n FROM events e JOIN sites s ON s.id = e.site_id "
            f"WHERE s.tenant_id = {P}", (tid,))
        out["events"] = int(cur.fetchone()["n"])
    return out


class Base(unittest.TestCase):
    def setUp(self):
        store.ensure_retention_schema()
        with store._cursor() as cur:
            cur.execute("SELECT id FROM tenants")
            ids = [int(r["id"]) for r in cur.fetchall()]
        for tid in ids:
            store.delete_tenant(tid)
        with store._cursor() as cur:
            cur.execute("DELETE FROM retention_runs")
            cur.execute("DELETE FROM reset_tokens")


class CutoffTest(Base):
    def test_trial_expired_boundary(self):
        just_in = account(trial_ends=ENDED_CUT - SECOND)
        on_edge = account(trial_ends=ENDED_CUT)
        res = store.dormant_tenant_ids(NOW)
        self.assertIn(just_in, res["trial_expired"])
        self.assertNotIn(on_edge, res["trial_expired"])

    def test_cancelled_boundary(self):
        just_in = account(plan="cancelled", cancelled_at=ts(ENDED_CUT - SECOND))
        on_edge = account(plan="cancelled", cancelled_at=ts(ENDED_CUT))
        res = store.dormant_tenant_ids(NOW)
        self.assertIn(just_in, res["cancelled"])
        self.assertNotIn(on_edge, res["cancelled"])

    def test_unverified_boundary_and_usage(self):
        old = account(verified=False, created=UNVERIFIED_CUT - SECOND)
        young = account(verified=False, created=UNVERIFIED_CUT)
        used = account(verified=False, created=UNVERIFIED_CUT - SECOND)
        with_site(used, events=True)
        empty_site = account(verified=False, created=UNVERIFIED_CUT - SECOND)
        with_site(empty_site, events=False)
        verified = account(verified=True, created=UNVERIFIED_CUT - SECOND)
        res = store.dormant_tenant_ids(NOW)
        self.assertIn(old, res["unverified"])
        self.assertIn(empty_site, res["unverified"])
        for tid in (young, used, verified):
            self.assertNotIn(tid, res["unverified"])

    def test_promise_kept_with_margin(self):
        # Terms §10 / DPA §8: deleted within 90 days after the relationship ends.
        self.assertLess(store.RETENTION_ENDED_DAYS, 90)

    def test_account_without_users_is_never_unverified(self):
        tid = store.create_tenant("Eier-site")  # `manage create-site` makes these
        _set(tid, created_at=ts(NOW - timedelta(days=400)))
        self.assertNotIn(tid, sum(store.dormant_tenant_ids(NOW).values(), []))


class EnforceTest(Base):
    def _mix(self) -> tuple[list[int], list[int]]:
        gone = [
            account(trial_ends=ENDED_CUT - timedelta(days=1)),
            account(plan="cancelled", cancelled_at=ts(ENDED_CUT - timedelta(days=1))),
            account(verified=False, created=NOW - timedelta(days=30)),
        ]
        with_site(gone[0])
        with_site(gone[1])
        long_ago = NOW - timedelta(days=400)
        kept = [
            account(),                                           # trial still running
            account(plan="vekst", trial_ends=long_ago),          # paying
            account(plan="owner", trial_ends=long_ago),
            account(plan="byra", trial_ends=long_ago),
            account(plan="cancelled", cancelled_at=ts(NOW - timedelta(days=10))),
            account(plan="cancelled", cancelled_at=ts(long_ago), vipps_agreement_id="agr-1"),
            account(trial_ends=long_ago, vipps_pending_plan="vekst"),
            account(plan="cancelled", cancelled_at=ts(long_ago), invoice_details="{}"),
            account(trial_ends=long_ago, stripe_subscription_id="sub_1"),
        ]
        for tid in kept:
            with_site(tid)
        return gone, kept

    def test_dry_run_changes_nothing(self):
        gone, kept = self._mix()
        before = {tid: rows(tid) for tid in gone + kept}
        res = store.purge_dormant(enforce=False, now=NOW)
        self.assertEqual(res["counts"]["accounts"],
                         {"unverified": 1, "trial_expired": 1, "cancelled": 1})
        self.assertEqual(res["counts"]["deleted_accounts"], 0)
        self.assertEqual(res["counts"]["deleted_rows"], {})
        self.assertEqual({tid: rows(tid) for tid in gone + kept}, before)

    def test_enforce_deletes_exactly_the_dormant_and_keeps_the_rest(self):
        gone, kept = self._mix()
        kept_before = {tid: rows(tid) for tid in kept}
        res = store.purge_dormant(enforce=True, now=NOW)
        self.assertEqual(res["counts"]["deleted_accounts"], 3)
        for tid in gone:
            self.assertEqual(rows(tid), {"tenants": 0, "users": 0, "sites": 0, "events": 0})
        self.assertEqual({tid: rows(tid) for tid in kept}, kept_before)

    def test_tenant_isolation(self):
        dormant = account(trial_ends=ENDED_CUT - timedelta(days=5))
        live = account()
        with_site(dormant)
        with_site(live)
        with_site(live)
        live_before = rows(live)
        store.purge_dormant(enforce=True, now=NOW)
        self.assertFalse(exists(dormant))
        self.assertEqual(rows(live), live_before)

    def test_idempotent(self):
        self._mix()
        first = store.purge_dormant(enforce=True, now=NOW)
        second = store.purge_dormant(enforce=True, now=NOW)
        self.assertEqual(first["counts"]["deleted_accounts"], 3)
        self.assertEqual(second["counts"]["deleted_accounts"], 0)
        self.assertEqual(sum(second["counts"]["accounts"].values()), 0)
        with store._cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM retention_runs")
            self.assertEqual(int(cur.fetchone()["n"]), 2)

    def test_batch_limit_leaves_the_rest_for_the_next_run(self):
        for _ in range(3):
            account(trial_ends=ENDED_CUT - timedelta(days=1))
        res = store.purge_dormant(enforce=True, now=NOW, batch=2)
        self.assertEqual(res["counts"]["deleted_accounts"], 2)
        self.assertEqual(res["counts"]["remaining_accounts"], 1)
        res = store.purge_dormant(enforce=True, now=NOW, batch=2)
        self.assertEqual(res["counts"]["deleted_accounts"], 1)

    def test_account_that_paid_since_selection_survives(self):
        tid = account(trial_ends=ENDED_CUT - timedelta(days=1))
        real = store.dormant_tenant_ids

        def stale(now=None):  # selection made before the customer bought a plan
            out = real(now)
            store.set_tenant_plan(tid, "vekst")
            return out

        store.dormant_tenant_ids = stale
        try:
            res = store.purge_dormant(enforce=True, now=NOW)
        finally:
            store.dormant_tenant_ids = real
        self.assertTrue(exists(tid))
        self.assertEqual(res["counts"]["deleted_accounts"], 0)

    def test_expired_reset_links_go_only_with_enforce(self):
        store.create_reset_token("glemt@example.no")
        with store._cursor() as cur:
            cur.execute(f"UPDATE reset_tokens SET expires_at = {P}", (ts(NOW - timedelta(hours=1)),))
        store.create_reset_token("fersk@example.no")
        with store._cursor() as cur:
            cur.execute(f"UPDATE reset_tokens SET expires_at = {P} WHERE email = {P}",
                        (ts(NOW + timedelta(hours=1)), "fersk@example.no"))
        dry = store.purge_dormant(enforce=False, now=NOW)
        self.assertEqual(dry["counts"]["expired_links"]["reset_tokens"], 1)
        store.purge_dormant(enforce=True, now=NOW)
        with store._cursor() as cur:
            cur.execute("SELECT email FROM reset_tokens")
            self.assertEqual([r["email"] for r in cur.fetchall()], ["fersk@example.no"])


class CancelClockTest(Base):
    def test_cancel_stamps_once_and_a_new_plan_clears_it(self):
        tid = account(plan="vekst")
        store.set_tenant_plan(tid, "cancelled")
        first = store.get_tenant(tid)
        with store._cursor() as cur:
            cur.execute(f"SELECT cancelled_at FROM tenants WHERE id = {P}", (tid,))
            stamp = cur.fetchone()["cancelled_at"]
        self.assertTrue(stamp)
        self.assertEqual(first["plan"], "cancelled")
        store.set_tenant_plan(tid, "cancelled")  # a repeated webhook keeps the first stamp
        with store._cursor() as cur:
            cur.execute(f"SELECT cancelled_at FROM tenants WHERE id = {P}", (tid,))
            self.assertEqual(cur.fetchone()["cancelled_at"], stamp)
        store.set_tenant_plan(tid, "liten")
        with store._cursor() as cur:
            cur.execute(f"SELECT cancelled_at FROM tenants WHERE id = {P}", (tid,))
            self.assertIsNone(cur.fetchone()["cancelled_at"])

    def test_vipps_stop_stamps_the_clock(self):
        tid = account(plan="vekst", vipps_agreement_id="agr-2")
        store.clear_vipps(tid, new_plan="cancelled")
        with store._cursor() as cur:
            cur.execute(f"SELECT cancelled_at FROM tenants WHERE id = {P}", (tid,))
            self.assertTrue(cur.fetchone()["cancelled_at"])

    def test_legacy_cancelled_rows_get_a_clock_not_a_deletion(self):
        paid_through = account(plan="cancelled", plan_ends_at="2026-01-31")
        unknown = account(plan="cancelled")
        _set(paid_through, cancelled_at=None)
        _set(unknown, cancelled_at=None)
        store._retention_ready = False
        store.ensure_retention_schema()
        with store._cursor() as cur:
            cur.execute(f"SELECT id, cancelled_at FROM tenants WHERE id IN ({P}, {P})",
                        (paid_through, unknown))
            got = {int(r["id"]): r["cancelled_at"] for r in cur.fetchall()}
        self.assertEqual(got[paid_through], "2026-01-31")
        self.assertGreater(got[unknown], ts(datetime.now(timezone.utc) - timedelta(minutes=5)))
        res = store.dormant_tenant_ids(datetime.now(timezone.utc))
        self.assertIn(paid_through, res["cancelled"])
        self.assertNotIn(unknown, res["cancelled"])


class OutputTest(Base):
    def test_manage_prints_counts_only_and_is_dry_by_default(self):
        tid = account(trial_ends=datetime.now(timezone.utc) - timedelta(days=200))
        os.environ.pop("SPORLOS_RETENTION_ENFORCE", None)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(manage.main(["purge-dormant"]), 0)
        out = buf.getvalue()
        self.assertTrue(exists(tid))
        self.assertNotIn("@", out)
        self.assertNotIn("Firma", out)
        payload = json.loads(out.split(" ", 1)[1])
        self.assertFalse(payload["enforce"])
        self.assertEqual(payload["counts"]["accounts"]["trial_expired"], 1)
        with store._cursor() as cur:
            cur.execute("SELECT enforce, counts FROM retention_runs")
            run = cur.fetchone()
        self.assertEqual(int(run["enforce"]), 0)
        self.assertNotIn("@", run["counts"])

    def test_manage_enforces_only_with_the_flag(self):
        tid = account(trial_ends=datetime.now(timezone.utc) - timedelta(days=200))
        os.environ["SPORLOS_RETENTION_ENFORCE"] = "1"
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                manage.main(["purge-dormant"])
        finally:
            os.environ.pop("SPORLOS_RETENTION_ENFORCE", None)
        self.assertFalse(exists(tid))


if __name__ == "__main__":
    unittest.main()
