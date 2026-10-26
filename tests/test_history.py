"""Per-day breakdowns (daily_breakdowns, app/history.py) kept after the raw events are gone.

Covered: the rollup writes a day's breakdowns idempotently and never wipes a day whose
events are gone; the cap per dim; the backfill is a dry run by default and idempotent
with --apply; retention deletes events but not breakdowns (and fills in days that lack
them first); deleting a site removes them; the table holds no visitor hash; the 1/7/30/90
day numbers are what they were; the 6- and 12-month views over stored days plus recent
events give exactly what the same windows gave on the raw events; the API reads old months.

All data is synthetic. Run ONE FILE PER PROCESS (the backend is chosen at import). SQLite
by default; against a throwaway Postgres with SPORLOS_TEST_PG (see tests/test_pg_pool.py):
    .venv/bin/python3 -m pytest -q tests/test_history.py
"""

from __future__ import annotations

import contextlib
import io
import os
import random
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

_PG = os.environ.get("SPORLOS_TEST_PG")
_TMP_DB = tempfile.NamedTemporaryFile(prefix="sporlos_test_", suffix=".db", delete=False)
_TMP_DB.close()
if _PG:
    os.environ["DATABASE_URL"] = _PG
else:
    os.environ.pop("DATABASE_URL", None)
    os.environ["SPORLOS_DB"] = _TMP_DB.name

from starlette.applications import Starlette  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from app import api, botcount, history, main, manage, store  # noqa: E402

P = store.P
SPAN = 420  # days of synthetic history: covers a 365-day window and part of the one before


def _today():
    return datetime.now(timezone.utc).date()


def _day(ago: int) -> str:
    return (_today() - timedelta(days=ago)).isoformat()


def setUpModule():
    store.init_db()


def tearDownModule():
    os.unlink(_TMP_DB.name)


def _wipe():
    with store._cursor() as cur:  # a shared Postgres may hold other test modules' accounts
        cur.execute("SELECT id FROM tenants")
        ids = [int(r["id"]) for r in cur.fetchall()]
    for tid in ids:
        store.delete_tenant(tid)
    with store._cursor() as cur:
        for t in ("event_items", "events", "daily_rollups", "daily_breakdowns", "bot_daily",
                  "goals", "funnels", "api_keys", "sites", "tenants"):
            cur.execute(f"DELETE FROM {t}")


def _insert(site_id: int, ts: str, name: str, path: str, vh: str, items=None, **cols) -> None:
    keys = ["site_id", "ts", "name", "path", "visitor_hash", *cols]
    vals = [site_id, ts, name, path, vh, *cols.values()]
    sql = f"INSERT INTO events ({', '.join(keys)}) VALUES ({', '.join([P] * len(keys))})"
    with store._cursor() as cur:
        if items:
            if store._USE_PG:
                cur.execute(sql + " RETURNING id", vals)
                eid = cur.fetchone()["id"]
            else:
                cur.execute(sql, vals)
                eid = cur.lastrowid
            for it in items:
                cur.execute(
                    f"INSERT INTO event_items (site_id, event_id, ts, name, qty, unit_price_cents, currency) "
                    f"VALUES ({P}, {P}, {P}, {P}, {P}, {P}, {P})",
                    (site_id, eid, ts, it[0], it[1], it[2], cols.get("currency")),
                )
        else:
            cur.execute(sql, vals)


PATHS = ["/", "/priser", "/om", "/blogg/a", "/blogg/b", "/kontakt", "/takk"]
SOURCES = [None, "google.com", "duckduckgo.com", "chatgpt.com", "nyhetsbrev.example"]
COUNTRIES = ["NO", "SE", "DK", "DE"]
REGIONS = ["Oslo", "Viken", "Vestland", None]
DEVICES = ["desktop", "mobil", "tablet"]
BROWSERS = ["Firefox", "Chrome", "Safari"]
OSES = ["Linux", "Android", "iOS", "Windows"]


def seed(domain: str = "historikk.example", span: int = SPAN, seed_no: int = 7) -> dict:
    """Synthetic traffic every day for `span` days, up to and including today."""
    rnd = random.Random(seed_no)
    tid = store.create_tenant(f"T {domain}")
    site = store.create_site(tid, domain)
    sid = site["id"]
    store.create_goal(sid, "Kontakt", "path", "/kontakt")
    store.create_goal(sid, "Påmelding", "event", "signup")
    for ago in range(span - 1, -1, -1):
        day = _day(ago)
        for v in range(rnd.randint(1, 5)):
            vh = f"h{ago}-{v}"  # a new hash every day, like the daily salt
            src = rnd.choice(SOURCES)
            utm = rnd.random() < 0.2
            hour = rnd.randint(0, 22)
            n_views = rnd.randint(1, 3)
            for i in range(n_views):
                minute = i * (40 if rnd.random() < 0.3 else 5)  # some views start a new session
                ts = f"{day} {hour:02d}:{minute % 60:02d}:{i:02d}"
                if minute >= 60:
                    ts = f"{day} {hour + 1:02d}:{minute % 60:02d}:{i:02d}"
                _insert(
                    sid, ts, "pageview", rnd.choice(PATHS), vh,
                    referrer_src=src if i == 0 else domain,  # later views: own site = navigation
                    country=rnd.choice(COUNTRIES), region=rnd.choice(REGIONS),
                    device=rnd.choice(DEVICES), browser=rnd.choice(BROWSERS), os=rnd.choice(OSES),
                    utm_source=("chatgpt" if src is None else "nyhetsbrev") if utm else None,
                    utm_medium="epost" if utm else None,
                    utm_campaign=f"kampanje-{ago % 3}" if utm else None,
                )
            if rnd.random() < 0.3:
                _insert(sid, f"{day} {hour + 1:02d}:50:00", "signup", "/kontakt", vh)
            if rnd.random() < 0.2:
                cents = rnd.choice([9900, 24900])
                _insert(
                    sid, f"{day} {hour + 1:02d}:55:00", "purchase", "/takk", vh,
                    items=[("Produkt A", 1, cents)],
                    revenue_cents=cents, currency=rnd.choice(["NOK", "NOK", "EUR"]),
                    payment_method=rnd.choice(["vipps", "kort", None]),
                )
        if ago % 4 == 0:
            store.add_bot_counts({
                (sid, day, "ai", "GPTBot", "/blogg/a"): 3,
                (sid, day, "search", "Googlebot", ""): 2,
            })
    return {**site, "tid": tid}


def rollup_all_days(site_id: int, span: int = SPAN) -> None:
    for ago in range(span - 1, 0, -1):
        store.compute_rollup(site_id, _day(ago))


def bd_rows(site_id: int, day: str | None = None) -> list[tuple]:
    q = (f"SELECT day, dim, key, currency, n, visitors, revenue_cents FROM daily_breakdowns "
         f"WHERE site_id = {P}")
    args = [site_id]
    if day:
        q += f" AND day = {P}"
        args.append(day)
    with store._cursor() as cur:
        cur.execute(q + " ORDER BY day, dim, key, currency", tuple(args))
        return [(str(r["day"])[:10], r["dim"], r["key"], r["currency"], int(r["n"]),
                 None if r["visitors"] is None else int(r["visitors"]),
                 None if r["revenue_cents"] is None else int(r["revenue_cents"]))
                for r in cur.fetchall()]


def views(site_id: int, days: int) -> dict:
    """Everything the site dashboard reads for a period (plus the API breakdown)."""
    ec = store.ecommerce_stats(site_id, days)
    return {
        "stats": store.stats(site_id, days),
        "kpis_prev": store.kpis(site_id, days, offset=1),
        "flow": store.flow_stats(site_id, days),
        "goals": store.goal_stats(site_id, days),
        "events": store.top_events(site_id, days),
        "ecom": ec,
        "ecom_prev": store.ecommerce_stats(site_id, days, offset=1),
        "products": store.top_products(site_id, days, "NOK"),
        "rev_src": store.revenue_by_source(site_id, days, "NOK"),
        "rev_pay": store.revenue_by_payment(site_id, days, "NOK"),
        "pages": store.api_breakdown(site_id, days, "pages", 1000),
        "sources": store.api_breakdown(site_id, days, "sources", 1000),
        "ai": store.ai_referrals(site_id, days),
        "ai_src": store.ai_referral_sources(site_id, days),
        "bots": store.bot_summary(site_id, days),
    }


def normalize(v):
    """Lists of dicts compared as sorted lists: ties in a top-10 have no defined order."""
    if isinstance(v, dict):
        return {k: normalize(x) for k, x in v.items()}
    if isinstance(v, list):
        return sorted((normalize(x) for x in v), key=repr)
    return int(v) if hasattr(v, "as_integer_ratio") and not isinstance(v, (bool, float)) else v


def pageview_series(site_id: int, days: int) -> dict[str, tuple[int, int]]:
    start, end = store._period_window(days)
    with store._cursor() as cur:
        cur.execute(
            f"SELECT {store._bucket_expr('day')} AS b, COUNT(*) AS pv, COUNT(DISTINCT visitor_hash) AS u "
            f"FROM events WHERE site_id = {P} AND ts >= {P} AND ts < {P} AND name = 'pageview' GROUP BY b",
            (site_id, start, end),
        )
        return {str(r["b"]): (int(r["pv"]), int(r["u"])) for r in cur.fetchall()}


class RollupTest(unittest.TestCase):
    def setUp(self):
        _wipe()
        self.site = seed(span=3)
        self.sid = self.site["id"]

    def test_rollup_writes_the_day_and_is_idempotent(self):
        day = _day(1)
        store.compute_rollup(self.sid, day)
        first = bd_rows(self.sid, day)
        dims = {r[1] for r in first}
        for dim in ("kpi", "page", "source", "country", "region", "device", "browser", "os",
                    "entry", "exit"):
            self.assertIn(dim, dims)
        store.compute_rollup(self.sid, day)
        store.compute_rollup(self.sid, day)
        self.assertEqual(bd_rows(self.sid, day), first)
        # kpi rows match the sealed rollup of the same day
        kpi = {r[2]: r[4] for r in first if r[1] == "kpi"}
        r = store.get_rollup(self.sid, day)
        self.assertEqual((kpi["pageviews"], kpi["visitors"], kpi["sessions"]),
                         (int(r["pageviews"]), int(r["visitors"]), int(r["sessions"])))

    def test_a_day_without_events_is_never_wiped(self):
        day = _day(1)
        store.compute_rollup(self.sid, day)
        before = bd_rows(self.sid, day)
        self.assertTrue(before)
        with store._cursor() as cur:
            cur.execute(f"DELETE FROM event_items WHERE site_id = {P}", (self.sid,))
            cur.execute(f"DELETE FROM events WHERE site_id = {P}", (self.sid,))
            cur.execute(f"DELETE FROM bot_daily WHERE site_id = {P}", (self.sid,))
        self.assertIsNone(history.compute_breakdowns(self.sid, day))
        store.compute_rollup(self.sid, day)
        self.assertEqual(bd_rows(self.sid, day), before)

    def test_each_dim_is_capped_but_totals_are_not(self):
        day = _day(1)
        for i in range(12):
            _insert(self.sid, f"{day} 12:00:{i:02d}", "pageview", f"/ekstra/{i}", f"cap{i}")
        with mock.patch.object(history, "CAP", 4):
            store.compute_rollup(self.sid, day)
        rows = bd_rows(self.sid, day)
        self.assertEqual(sum(1 for r in rows if r[1] == "page"), 4)
        kpi = {r[2]: r[4] for r in rows if r[1] == "kpi"}
        self.assertEqual(kpi["pageviews"], int(store.get_rollup(self.sid, day)["pageviews"]))

    def test_no_visitor_hash_in_the_table(self):
        store.compute_rollup(self.sid, _day(1))
        with store._cursor() as cur:
            if store._USE_PG:
                cur.execute("SELECT column_name AS c FROM information_schema.columns "
                            "WHERE table_name = 'daily_breakdowns'")
                cols = {r["c"] for r in cur.fetchall()}
            else:
                cur.execute("PRAGMA table_info(daily_breakdowns)")
                cols = {r["name"] for r in cur.fetchall()}
            cur.execute(f"SELECT DISTINCT visitor_hash AS v FROM events WHERE site_id = {P}", (self.sid,))
            hashes = {r["v"] for r in cur.fetchall()}
        self.assertEqual(cols, {"site_id", "day", "dim", "key", "currency", "n", "visitors",
                                "revenue_cents"})
        stored = {r[2] for r in bd_rows(self.sid)} | {r[3] for r in bd_rows(self.sid)}
        self.assertFalse(stored & hashes)
        self.assertFalse(any(h in k for k in stored for h in hashes))


class BackfillRetentionDeletionTest(unittest.TestCase):
    def setUp(self):
        _wipe()
        self.site = seed(span=100, seed_no=3)
        self.sid = self.site["id"]

    def _run(self, *args) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(manage.main(["backfill-breakdowns", *args]), 0)
        return out.getvalue()

    def test_backfill_dry_run_then_apply_then_again(self):
        out = self._run()
        self.assertIn("dry run", out)
        self.assertIn("99 site-days", out)  # every past day; today is left out
        self.assertEqual(bd_rows(self.sid), [])
        out = self._run("--apply")
        self.assertIn("computed 99 site-days", out)
        rows = bd_rows(self.sid)
        self.assertEqual({r[0] for r in rows}, {_day(a) for a in range(1, 100)})
        self.assertIn("99 already present", self._run())
        self._run("--apply")
        self.assertEqual(bd_rows(self.sid), rows)

    def test_retention_keeps_breakdowns_and_fills_in_missing_days(self):
        self._run("--apply")
        old_day = _day(96)  # a robot day too (every 4th)
        kept = [r for r in bd_rows(self.sid) if r[0] != old_day]
        with store._cursor() as cur:  # a day the nightly run missed
            cur.execute(f"DELETE FROM daily_breakdowns WHERE site_id = {P} AND day = {P}",
                        (self.sid, old_day))
        store.retention_sweep(90)
        with store._cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM events WHERE site_id = {P} AND ts < {P}",
                        (self.sid, f"{_day(90)} 00:00:00"))
            self.assertEqual(int(cur.fetchone()["n"]), 0)
        rows = bd_rows(self.sid)
        self.assertEqual([r for r in rows if r[0] != old_day], kept)
        self.assertTrue([r for r in rows if r[0] == old_day and r[1] == "page"])
        self.assertTrue([r for r in rows if r[0] == old_day and r[1] == "bot"])

    def test_site_deletion_removes_breakdowns(self):
        self._run("--apply")
        other = seed("annen.example", span=3, seed_no=9)
        store.compute_rollup(other["id"], _day(1))
        self.assertIn("daily_breakdowns", store.SITE_TABLES)
        counts = store.delete_site(self.sid, self.site["tid"])
        self.assertGreater(counts["daily_breakdowns"], 0)
        self.assertEqual(bd_rows(self.sid), [])
        self.assertTrue(bd_rows(other["id"]))  # another site's rows untouched


class LongViewTest(unittest.TestCase):
    """One seeded site: views before vs after rollup + retention."""

    @classmethod
    def setUpClass(cls):
        botcount.flush()
        _wipe()
        cls.site = seed()
        cls.sid = cls.site["id"]
        # The truth: every window computed straight from the raw events, as the code
        # did before (no window is "long" while all the events still exist).
        with mock.patch.object(store, "_long", lambda start: False):
            cls.raw = {d: normalize(views(cls.sid, d)) for d in (1, 7, 30, 90, 180, 365)}
            cls.raw_series = {d: pageview_series(cls.sid, d) for d in (180, 365)}
        rollup_all_days(cls.sid)
        cls.short_after_rollup = {d: normalize(views(cls.sid, d)) for d in (1, 7, 30, 90)}
        store.retention_sweep(90)

    def test_short_periods_never_read_the_new_table(self):
        for d in (1, 7, 30, 90):
            self.assertFalse(store._long(store._period_window(d)[0]), d)

    def test_short_periods_unchanged(self):
        for d in (1, 7, 30, 90):
            after = normalize(views(self.sid, d))
            for name, v in self.raw[d].items():
                if d == 90 and name in ("kpis_prev", "ecom_prev"):
                    continue  # the window before 90 days was half deleted; see below
                with self.subTest(days=d, view=name):
                    self.assertEqual(self.short_after_rollup[d][name], v)
                    self.assertEqual(after[name], v)

    def test_previous_90_days_now_has_data(self):
        # Before: the comparison window for «90 dager» lay almost entirely past the raw
        # window, so its numbers were near zero. Now it is read from the stored days.
        self.assertEqual(normalize(store.kpis(self.sid, 90, offset=1)), self.raw[90]["kpis_prev"])
        self.assertGreater(store.kpis(self.sid, 90, offset=1)["pageviews"], 0)

    def test_long_periods_match_the_raw_events(self):
        for d in (180, 365):
            after = normalize(views(self.sid, d))
            for name, v in self.raw[d].items():
                with self.subTest(days=d, view=name):
                    self.assertEqual(after[name], v)

    def test_long_period_has_real_old_numbers(self):
        s = store.stats(self.sid, 365)
        recent = store.stats(self.sid, 90)
        self.assertGreater(s["pageviews"], recent["pageviews"] * 3)
        self.assertGreater(store.kpis(self.sid, 365, offset=1)["pageviews"], 0)

    def test_long_timeseries(self):
        for d in (180, 365):
            daily = store.timeseries(self.sid, d, unit="day")
            self.assertEqual(len(daily), d)
            got = {b["bucket"]: (b["pageviews"], b["visitors"]) for b in daily if b["pageviews"]}
            self.assertEqual(got, self.raw_series[d])
            weekly = store.timeseries(self.sid, d)
            self.assertEqual(sum(b["pageviews"] for b in weekly), sum(v[0] for v in got.values()))

    def test_dashboard_and_demo_offer_long_periods(self):
        client = TestClient(main.app)
        with mock.patch.dict(os.environ, {"SPORLOS_DEMO_SITE": self.site["public_id"]}):
            r = client.get("/demo", params={"period": "365"})
        self.assertEqual(r.status_code, 200)
        self.assertIn('class="on">12 måneder</a>', r.text)
        self.assertIn("6 måneder", r.text)
        self.assertIn("siste 12 måneder", r.text)
        self.assertIn("Enkelthendelsene er slettet", r.text)
        s = store.stats(self.sid, 365)
        self.assertIn(main._fmt_n(s["visitors"]), r.text)
        with mock.patch.dict(os.environ, {"SPORLOS_DEMO_SITE": self.site["public_id"]}):
            r7 = client.get("/demo", params={"period": "7"})
        self.assertNotIn("Enkelthendelsene er slettet", r7.text)

    def test_api_long_period_and_old_month(self):
        key = store.create_api_key(self.site["tid"], "test")["key"]
        h = {"Authorization": f"Bearer {key}"}
        c = TestClient(Starlette(routes=[Route("/api/v1/stats", api.stats),
                                         Route("/api/v1/breakdown", api.breakdown)]))
        r = c.get("/api/v1/stats", params={"site": self.site["public_id"], "period": "365"}, headers=h)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["stats"]["pageviews"], self.raw[365]["stats"]["pageviews"])
        old = (_today() - timedelta(days=200)).strftime("%Y-%m")
        r = c.get("/api/v1/breakdown", params={"site": self.site["public_id"], "month": old,
                                               "prop": "pages"}, headers=h)
        self.assertEqual(r.status_code, 200)
        rows = r.json()["breakdown"]
        self.assertTrue(rows)
        win = store.month_window(old)
        with store._cursor() as cur:
            cur.execute(f"SELECT COUNT(*) AS n FROM daily_rollups WHERE site_id = {P} AND day >= {P} "
                        f"AND day < {P}", (self.sid, win[0][:10], win[1][:10]))
            self.assertGreater(int(cur.fetchone()["n"]), 0)
            cur.execute(f"SELECT SUM(pageviews) AS n FROM daily_rollups WHERE site_id = {P} "
                        f"AND day >= {P} AND day < {P}", (self.sid, win[0][:10], win[1][:10]))
            self.assertEqual(sum(x["pageviews"] for x in rows), int(cur.fetchone()["n"]))


class PreTableHistoryTest(unittest.TestCase):
    """Days rolled up before daily_breakdowns existed: totals from daily_rollups."""

    def test_totals_fall_back_to_daily_rollups(self):
        _wipe()
        site = seed("gammel.example", span=3)
        day = _day(200)
        with store._cursor() as cur:
            cur.execute(
                f"INSERT INTO daily_rollups (site_id, day, pageviews, visitors, sessions, bounce_rate) "
                f"VALUES ({P}, {P}, 40, 10, 20, 50)", (site["id"], day))
        k = store.kpis(site["id"], 365)
        recent = store.kpis(site["id"], 90)
        self.assertEqual(k["pageviews"], recent["pageviews"] + 40)
        self.assertEqual(k["visitors"], recent["visitors"] + 10)
        self.assertEqual(k["sessions"], recent["sessions"] + 20)
        self.assertEqual(store.stats(site["id"], 365)["pageviews"], recent["pageviews"] + 40)


if __name__ == "__main__":
    unittest.main()
