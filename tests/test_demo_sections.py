"""The public demo (/demo) shows what a customer gets beyond traffic: goals, funnels,
e-commerce and the robot section. Customers' own opt-in public dashboards (/p/<site>) must
NOT get them: sharing your traffic must never publish your revenue.

All data here is synthetic. Run ONE FILE PER PROCESS (the module owns its SQLite file):
    .venv/bin/python3 -m pytest -q tests/test_demo_sections.py < /dev/null
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

_TMP_DB = tempfile.NamedTemporaryFile(prefix="sporlos_test_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ["SPORLOS_DB"] = _TMP_DB.name
os.environ.pop("DATABASE_URL", None)

from starlette.testclient import TestClient  # noqa: E402

from app import botcount, main, store  # noqa: E402

TODAY = lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d")  # noqa: E731

# What each new section renders as its heading, and one synthetic marker from its data.
SECTIONS = {
    "goals": ("<h3>Mål</h3>", "Ny konto"),
    "funnels": ("<h3>Trakter</h3>", "Forside til kjøp"),
    "ecom": ("<h3>E-handel</h3>", "Eksempelprodukt"),
    "bots": ("Roboter og KI-agenter", "GPTBot"),
}


def setUpModule():
    store.init_db()


def tearDownModule():
    os.unlink(_TMP_DB.name)


def _ev(sid, name, path, visitor, **kw):
    store.insert_event(sid, {"name": name, "path": path, "visitor_hash": visitor, "referrer_src": "www.example.org", **kw})


def seed_site(domain: str) -> dict:
    """A site with traffic, two goals, a funnel, two orders on two payment methods and robot rows."""
    tid = store.create_tenant(f"T {domain}")
    site = {**store.create_site(tid, domain), "tid": tid}
    sid = site["id"]
    for v in ("a1", "a2", "a3", "a4"):
        _ev(sid, "pageview", "/", v)
    _ev(sid, "pageview", "/signup", "a1")
    _ev(sid, "signup_complete", "/signup", "a1")
    _ev(sid, "pageview", "/signup", "a2")
    _ev(sid, "signup_complete", "/signup", "a2")
    _ev(sid, "checkout_submit", "/betal", "a1")
    items = [{"name": "Eksempelprodukt", "qty": 1, "unit_price_cents": 12375}]
    _ev(sid, "purchase", "/takk", "a1", revenue_cents=12375, currency="NOK", payment_method="kort")
    store.insert_event(sid, {"name": "purchase", "path": "/takk", "visitor_hash": "a2",
                             "revenue_cents": 12375, "currency": "NOK", "payment_method": "vipps"}, items)
    store.create_goal(sid, "Ny konto", "event", "signup_complete")
    store.create_goal(sid, "Kjøp", "event", "purchase")
    store.create_funnel(sid, "Forside til kjøp", [
        {"type": "path", "value": "/"},
        {"type": "path", "value": "/signup"},
        {"type": "event", "value": "signup_complete"},
        {"type": "event", "value": "purchase"},
    ])
    store.add_bot_counts({
        (sid, TODAY(), "ai", "GPTBot", "/guide"): 6,
        (sid, TODAY(), "search", "Googlebot", ""): 4,
    })
    return site


def seed_traffic_only(domain: str) -> dict:
    tid = store.create_tenant(f"T {domain}")
    site = {**store.create_site(tid, domain), "tid": tid}
    for v in ("b1", "b2"):
        _ev(site["id"], "pageview", "/", v)
    return site


class DemoSectionsTest(unittest.TestCase):
    def setUp(self):
        botcount.flush()
        self.client = TestClient(main.app)

    def demo(self, site, **params):
        with mock.patch.dict(os.environ, {"SPORLOS_DEMO_SITE": site["public_id"]}):
            r = self.client.get("/demo", params=params)
        self.assertEqual(r.status_code, 200)
        return r.text

    def test_demo_shows_all_four_sections(self):
        html = self.demo(seed_site("demo-full.example"))
        for name, (heading, marker) in SECTIONS.items():
            with self.subTest(section=name):
                self.assertIn(heading, html)
                self.assertIn(marker, html)
        # the synthetic numbers: two orders, 2 x 123,75 kr, split per payment method and source
        self.assertIn("kort", html)
        self.assertIn("vipps", html)
        self.assertIn("Betalingsmåte", html)
        self.assertIn("Kilde", html)
        self.assertIn('class="card block bots"', html)
        self.assertIn("De fleste KI-crawlere gjør ikke det", html)  # the honesty note

    def test_sections_sit_after_the_traffic_and_before_the_verifiable_numbers(self):
        html = self.demo(seed_site("demo-order.example"))
        pos = lambda s: html.index(s)  # noqa: E731
        self.assertLess(pos("<h3>Operativsystemer</h3>"), pos("<h3>Mål</h3>"))
        self.assertLess(pos("<h3>Mål</h3>"), pos("<h3>Trakter</h3>"))
        self.assertLess(pos("<h3>Trakter</h3>"), pos("<h3>E-handel</h3>"))
        self.assertLess(pos("<h3>E-handel</h3>"), pos("Roboter og KI-agenter"))
        self.assertLess(pos("Roboter og KI-agenter"), pos("Verifiserbare tall</span>"))

    def test_demo_is_read_only_no_edit_forms(self):
        html = self.demo(seed_site("demo-ro.example"))
        for action in ("/app/goals", "/app/funnels", "/app/goals/delete", "/app/funnels/delete"):
            with self.subTest(action=action):
                self.assertNotIn(f'action="{action}"', html)
        self.assertNotIn("Legg til mål", html)
        self.assertNotIn("Lag trakt", html)
        self.assertNotIn("<form", html.split("<h3>Mål</h3>")[1].split("Verifiserbare tall</span>")[0])

    def test_demo_sets_no_cookie(self):
        site = seed_site("demo-clean.example")
        with mock.patch.dict(os.environ, {"SPORLOS_DEMO_SITE": site["public_id"]}):
            r = self.client.get("/demo")
        self.assertNotIn("set-cookie", r.headers)

    def test_public_customer_dashboard_never_shows_the_new_sections(self):
        """The core gate: a customer's opt-in /p/<site> page has the same traffic cards,
        and none of goals, funnels, revenue or robots, even with all that data present."""
        cust = seed_site("kunde-butikk.example")
        store.set_public_dash(cust["public_id"], cust["tid"], True)
        other = seed_site("demo-other.example")  # the env points at a different site
        with mock.patch.dict(os.environ, {"SPORLOS_DEMO_SITE": other["public_id"]}):
            for period in ("1", "7", "30", "90"):
                r = self.client.get(f"/p/{cust['public_id']}", params={"period": period})
                self.assertEqual(r.status_code, 200)
                html = r.text
                self.assertIn("Topp sider", html)  # it is the real page
                for name, (heading, marker) in SECTIONS.items():
                    with self.subTest(section=name, period=period):
                        self.assertNotIn(heading, html)
                        self.assertNotIn(marker, html)
                for leak in ("123,75", "12375", "vipps", "Eksempelprodukt", "Betalingsmåte"):
                    self.assertNotIn(leak, html)

    def test_demo_site_shared_as_customer_page_also_has_no_sections(self):
        """The gate is the route, not the site id: /p/<demo id> is a plain public dashboard."""
        site = seed_site("demo-p.example")
        store.set_public_dash(site["public_id"], site["tid"], True)
        with mock.patch.dict(os.environ, {"SPORLOS_DEMO_SITE": site["public_id"]}):
            html = self.client.get(f"/p/{site['public_id']}").text
        self.assertNotIn("Roboter og KI-agenter", html)
        self.assertNotIn("<h3>E-handel</h3>", html)

    def test_ecommerce_empty_state_is_a_plain_line(self):
        site = seed_traffic_only("demo-noshop.example")
        html = self.demo(site)
        self.assertIn("<h3>E-handel</h3>", html)
        self.assertIn("Ingen kjøp i perioden.", html)
        self.assertNotIn("Betalingsmåte", html)
        # the other sections still render, without data
        self.assertIn("ingen mål enda", html)
        self.assertIn("Ingen trakter ennå", html)
        self.assertIn("Roboter og KI-agenter", html)
        self.assertIn("Ingen roboter registrert", html)

    def test_period_switch(self):
        """An order from 20 days ago counts in 30 and 90 days, not in today or 7 days."""
        site = seed_traffic_only("demo-period.example")
        sid = site["id"]
        _ev(sid, "purchase", "/takk", "old1", revenue_cents=50000, currency="NOK", payment_method="faktura")
        old = (datetime.now(timezone.utc) - timedelta(days=20)).strftime("%Y-%m-%d 12:00:00")
        with store._cursor() as cur:
            cur.execute(f"UPDATE events SET ts = {store.P} WHERE site_id = {store.P} AND name = 'purchase'", (old, sid))
        store.add_bot_counts({(sid, (datetime.now(timezone.utc) - timedelta(days=20)).strftime("%Y-%m-%d"),
                               "ai", "ClaudeBot", "/old"): 3})
        for period, shown in (("1", False), ("7", False), ("30", True), ("90", True)):
            with self.subTest(period=period):
                html = self.demo(site, period=period)
                self.assertEqual("faktura" in html, shown)
                self.assertEqual("ClaudeBot" in html, shown)
                self.assertEqual("Ingen kjøp i perioden." in html, not shown)
                self.assertIn("Roboter og KI-agenter", html)

    def test_bad_period_falls_back_to_default(self):
        html = self.demo(seed_site("demo-badperiod.example"), period="999")
        self.assertIn("<h3>E-handel</h3>", html)

    def test_owner_dashboard_keeps_its_forms_and_gates(self):
        """The refactor shares renderers with /app: editable there, gated as before."""
        from app.auth import hash_password
        tid, _uid = store.create_account("Eier AS", "eier-demo@example.no", hash_password("passord-123"))
        site = store.create_site(tid, "eier-demo.example")
        c = TestClient(main.app)
        c.post("/login", data={"email": "eier-demo@example.no", "password": "passord-123"})
        html = c.get(f"/app?site={site['public_id']}&period=7").text
        self.assertIn('action="/app/goals"', html)
        self.assertIn('action="/app/funnels"', html)
        self.assertNotIn("<h3>E-handel</h3>", html)          # no purchase ever: no section
        self.assertNotIn("Roboter og KI-agenter", html)      # no robots ever: no section
        # the owner sees the same Norwegian labels as the demo
        self.assertIn("Lag trakt", html)
        self.assertIn("<th>Andel</th>", html)
        self.assertIn("<h3>Trakter</h3>", html)
        self.assertIn("Vis sporingskode", html)


class DemoCopyTest(unittest.TestCase):
    """The demo's intro, labels and number formats (the owner dashboard shares the renderers)."""

    @classmethod
    def setUpClass(cls):
        botcount.flush()
        cls.client = TestClient(main.app)
        cls.site = seed_site("demo-copy.example")
        # a tablet and a desktop visit, stored with the canonical device values
        _ev(cls.site["id"], "pageview", "/", "d1", device="tablet")
        _ev(cls.site["id"], "pageview", "/", "d2", device="desktop")
        _ev(cls.site["id"], "pageview", "/", "d3", device="mobil")

    def demo(self, **params):
        with mock.patch.dict(os.environ, {"SPORLOS_DEMO_SITE": self.site["public_id"]}):
            r = self.client.get("/demo", params=params)
        self.assertEqual(r.status_code, 200)
        return r.text

    def test_intro_text(self):
        html = self.demo()
        self.assertIn(
            "Dette er ekte tall for <b>sporlos.no</b>, målt med Sporløs uten cookies og uten "
            "samtykkebanner. Kundene våre får det samme dashbordet.", html)
        for old in ("levende tall", "Det du ser her, er det kundene får", "uten samtykke.",
                    "Live demo", "live demo"):
            self.assertNotIn(old, html)
        self.assertIn("<title>Demo: ekte tall for sporlos.no | Sporløs</title>", html)

    def test_goal_and_funnel_labels(self):
        html = self.demo()
        self.assertIn("<h3>Trakter</h3>", html)
        self.assertIn("<th>Andel</th>", html)
        self.assertIn("(hendelse: signup_complete)", html)
        self.assertIn("(side)", html)
        self.assertIn("(hendelse)", html)
        visible = re.sub(r"<(style|script)\b.*?</\1>", "", html, flags=re.S)
        for old in ("Funnels", "funnel", "<th>Rate</th>", "Rate =", "(path)", "(event)", "event:"):
            self.assertNotIn(old, visible)

    def test_device_labels_are_norwegian_but_storage_is_not(self):
        html = self.demo()
        for label in ("Datamaskin", "Mobil", "Nettbrett"):
            self.assertIn(label, html)
        grid = html.split("<h3>Enheter</h3>")[1].split("</table>")[0]
        for raw in (">desktop<", ">tablet<", ">mobil<"):
            self.assertNotIn(raw, grid)
        with store._cursor() as cur:
            cur.execute(f"SELECT DISTINCT device FROM events WHERE site_id = {store.P}", (self.site["id"],))
            stored = {r["device"] for r in cur.fetchall()}
        self.assertTrue({"desktop", "tablet", "mobil"} <= stored)

    def test_norwegian_number_format(self):
        html = self.demo()
        # goal share with a decimal comma and a no-break space before %
        self.assertRegex(html, r"<td>\d+,\d\u00a0%</td>")
        self.assertNotRegex(html, r"<td>\d+\.\d%</td>")
        # bounce rate and pages per visit
        self.assertRegex(html, r"<b>\d+\u00a0%</b><span>fluktfrekvens</span>")
        self.assertRegex(html, r"<b>\d+,\d</b><span>sider per besøk</span>")
        self.assertNotIn("visn. per besøk", html)

    def test_multi_day_visitors_are_labelled_as_summed(self):
        self.assertIn("unike besøkende (summert per dag)", self.demo(period="7"))
        today = self.demo(period="1")
        self.assertIn("<span>unike besøkende</span>", today)


if __name__ == "__main__":
    unittest.main()
