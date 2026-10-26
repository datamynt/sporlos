"""sporlos.no measures its own signup and purchase funnel with Sporløs.

The logged-in /app is not tracked, so signup_complete and purchase are reported once from
/takk (a tracked page the server sends the browser to only after it has verified the
moment). These tests render the pages and check that the calls are there, that they are
gated on the real success signal, and that nothing personal is in them.

Stripe, Vipps and mail are faked.
Run ONE FILE PER PROCESS:
    .venv/bin/python3 -m pytest -q tests/test_own_funnel.py < /dev/null
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import unittest

_TMP_DB = tempfile.NamedTemporaryFile(prefix="sporlos_test_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ["SPORLOS_DB"] = _TMP_DB.name
os.environ.pop("DATABASE_URL", None)
os.environ["MAIL_FROM"] = "post@sporlos.no"

from starlette.testclient import TestClient  # noqa: E402

from app import mailer, main, manage, store, vipps  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.pathmask import mask_label  # noqa: E402

SELF_SITE = 'data-site="6LIACtOSP-S7"'
SESSIONS: dict[str, dict] = {}   # what stripe.checkout.Session.retrieve answers
AGREEMENTS: dict[str, str] = {}  # Vipps agreement id -> status


class _FakeStripe:
    class checkout:
        class Session:
            @staticmethod
            def create(**kw):
                class S:
                    url = "https://checkout.stripe.test/s"
                return S()

            @staticmethod
            def retrieve(sid):
                if sid not in SESSIONS:
                    raise RuntimeError("no such session")

                class S:
                    @staticmethod
                    def to_dict():
                        return SESSIONS[sid]
                return S()

    class Customer:
        @staticmethod
        def create(**kw):
            return {"id": "cus_x"}

        @staticmethod
        def modify(cid, **kw):
            return None


def setUpModule():
    store.init_db()
    mailer.send = lambda *a, **k: True
    main.stripe = _FakeStripe
    main.STRIPE_PRICES = {"liten": "price_liten", "vekst": "price_vekst", "pro": "price_pro"}
    main.STRIPE_TAX_RATE = "txr_mva25"
    vipps.configured = lambda: True
    vipps.get_agreement = lambda agid: {"status": AGREEMENTS.get(agid, "PENDING")}


def tearDownModule():
    os.unlink(_TMP_DB.name)


def account(email: str):
    tid, uid = store.create_account("Funnel AS", email, hash_password("passord-123"))
    store.set_email_verified(uid)
    c = TestClient(main.app)
    c.post("/login", data={"email": email, "password": "passord-123"})
    return tid, c


def reported(c: TestClient, follow_to: str | None = None) -> list:
    """GET /takk and return the events its inline script fires ([] when it redirects)."""
    r = c.get("/takk", follow_redirects=False)
    if r.status_code != 200:
        assert r.headers["location"] == "/app"
        return []
    m = re.search(r"var E=(\[.*?\]);", r.text)
    assert m, r.text
    if follow_to is not None:
        assert f'href="{follow_to}"' in r.text
    return json.loads(m.group(1))


def stripe_session(tid, plan="liten", total=12375, tax=2475, **over):
    s = {"status": "complete", "payment_status": "paid", "client_reference_id": str(tid),
         "metadata": {"plan": plan}, "amount_total": total, "total_details": {"amount_tax": tax}}
    s.update(over)
    return s


class SignupTest(unittest.TestCase):
    def test_signup_page_loads_the_tracker_and_reports_the_submit(self):
        html = TestClient(main.app).get("/signup").text
        self.assertIn(SELF_SITE, html)
        self.assertIn("sporlos('signup_submit')", html)
        # guarded: if the tracker is blocked, submitting the form must still work
        self.assertIn("window.sporlos&amp;&amp;sporlos(", html)

    def _signup(self, c, email, plan=""):
        return c.post("/signup", data={"company": "Ny AS", "email": email, "password": "passord-123",
                                       "plan": plan, "t": main._signup_stamp(time.time() - 10)},
                      follow_redirects=False)

    def test_signup_complete_is_reported_once_then_the_visitor_moves_on(self):
        c = TestClient(main.app)
        r = self._signup(c, "en@example.no")
        self.assertEqual(r.headers["location"], "/takk")
        page = c.get("/takk").text
        self.assertIn(SELF_SITE, page)  # /takk is tracked, /app is not
        self.assertIn('href="/app?ny=1"', page)
        self.assertEqual(json.loads(re.search(r"var E=(\[.*?\]);", page).group(1)),
                         [["signup_complete", None]])
        self.assertEqual(reported(c), [])  # a reload reports nothing

    def test_signup_with_a_plan_continues_to_the_checkout(self):
        c = TestClient(main.app)
        self._signup(c, "plan@example.no", plan="vekst")
        self.assertEqual(reported(c, "/betal?plan=vekst"), [["signup_complete", None]])

    def test_rejected_signups_report_nothing(self):
        c = TestClient(main.app)
        r = c.post("/signup", data={"company": "", "email": "x", "password": "kort",
                                    "t": main._signup_stamp(time.time() - 10)}, follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(reported(c), [])
        # a bot caught by the honeypot looks like success to the bot, but is not a signup
        r = c.post("/signup", data={"company": "Bot AS", "email": "bot@example.no",
                                    "password": "passord-123", "website": "http://spam.test",
                                    "t": main._signup_stamp(time.time() - 10)}, follow_redirects=False)
        self.assertEqual(reported(c), [])
        self.assertIsNone(store.get_user_by_email("bot@example.no"))

    def test_takk_cannot_be_triggered_by_hand(self):
        c = TestClient(main.app)
        self.assertEqual(c.get("/takk?e=purchase", follow_redirects=False).headers["location"], "/app")
        self.assertEqual(reported(c), [])

    def test_the_logged_in_dashboard_is_not_tracked(self):
        tid, c = account("dash@example.no")
        for url in ("/app", "/app?ny=1"):
            self.assertNotIn(SELF_SITE, c.get(url).text)


class CheckoutTest(unittest.TestCase):
    def test_each_payment_button_reports_checkout_submit_and_its_method(self):
        tid, c = account("betal@example.no")
        html = c.get("/betal?plan=vekst").text
        self.assertIn(SELF_SITE, html)
        for href, method in (("/billing/checkout?plan=vekst", "card"),
                             ("/billing/vipps/start?plan=vekst", "vipps"),
                             ("/betal/faktura?plan=vekst", "faktura")):
            tag = re.search(r'<a href="%s"[^>]*>' % re.escape(href), html).group(0)
            self.assertIn("sporlos('checkout_submit')", tag, href)
            self.assertIn(f"sporlos('checkout_{method}')", tag, href)
            self.assertIn("window.sporlos&amp;&amp;(", tag, href)  # guarded against a blocked tracker

    def test_event_names_survive_the_id_masking(self):
        for n in ("signup_submit", "signup_complete", "checkout_submit", "checkout_card",
                  "checkout_vipps", "checkout_faktura", "purchase"):
            self.assertEqual(mask_label(n), n)


class PurchaseTest(unittest.TestCase):
    def test_card_purchase_is_reported_once_from_a_paid_session(self):
        tid, c = account("kort@example.no")
        SESSIONS["cs_test_paid1"] = stripe_session(tid, "liten")  # 99 kr + 24,75 kr mva
        r = c.get("/billing/checkout/retur?session_id=cs_test_paid1", follow_redirects=False)
        self.assertEqual(r.headers["location"], "/takk")
        self.assertEqual(reported(c, "/app"), [["purchase", {
            "revenue": 99, "currency": "NOK", "payment": "stripe_card",
            "items": [{"name": "Sporløs Liten", "qty": 1, "price": 99}]}]])
        # the return URL replayed (reload, back button, bookmark): nothing more is counted
        r = c.get("/billing/checkout/retur?session_id=cs_test_paid1", follow_redirects=False)
        self.assertEqual(r.headers["location"], "/app")
        self.assertEqual(reported(c), [])

    def test_revenue_is_what_was_charged_excluding_vat(self):
        tid, c = account("rabatt@example.no")
        SESSIONS["cs_test_disc1"] = stripe_session(tid, "vekst", total=15563, tax=3113)  # 20 % off
        c.get("/billing/checkout/retur?session_id=cs_test_disc1", follow_redirects=False)
        (name, data), = reported(c)
        self.assertEqual((name, data["revenue"], data["items"][0]["price"]), ("purchase", 124.5, 124.5))

    def test_checkout_success_url_comes_back_through_the_verifying_route(self):
        tid, c = account("url@example.no")
        seen = {}
        real = _FakeStripe.checkout.Session.create
        _FakeStripe.checkout.Session.create = staticmethod(
            lambda **kw: seen.update(kw) or type("S", (), {"url": "https://checkout.stripe.test/s"})())
        try:
            c.get("/billing/checkout?plan=pro", follow_redirects=False)
        finally:
            _FakeStripe.checkout.Session.create = real
        self.assertTrue(seen["success_url"].endswith(
            "/billing/checkout/retur?session_id={CHECKOUT_SESSION_ID}"), seen["success_url"])

    def test_nothing_is_reported_for_unpaid_foreign_or_unknown_sessions(self):
        tid, c = account("nei@example.no")
        other, _ = account("annen@example.no")
        SESSIONS["cs_test_open1"] = stripe_session(tid, status="open", payment_status="unpaid")
        SESSIONS["cs_test_unpd1"] = stripe_session(tid, payment_status="unpaid")
        SESSIONS["cs_test_theirs"] = stripe_session(other)
        SESSIONS["cs_test_noplan"] = stripe_session(tid, "gratis")
        for sid in ("cs_test_open1", "cs_test_unpd1", "cs_test_theirs", "cs_test_noplan",
                    "cs_test_missing", "not-a-session", ""):
            with self.subTest(sid=sid):
                r = c.get(f"/billing/checkout/retur?session_id={sid}", follow_redirects=False)
                self.assertEqual(r.headers["location"], "/app")
                self.assertEqual(reported(c), [])
        anon = TestClient(main.app).get("/billing/checkout/retur?session_id=cs_test_paid1",
                                        follow_redirects=False)
        self.assertEqual(anon.headers["location"], "/login")

    def test_vipps_purchase_only_when_the_agreement_is_active(self):
        for status, expect_event in (("PENDING", False), ("STOPPED", False), ("ACTIVE", True)):
            with self.subTest(status=status):
                tid, c = account(f"vipps-{status.lower()}@example.no")
                store.set_vipps_pending(tid, f"agr_{status}", "vekst")
                AGREEMENTS[f"agr_{status}"] = status
                real = main.asyncio.sleep
                main.asyncio.sleep = lambda s: real(0)
                try:
                    r = c.get("/billing/vipps/retur", follow_redirects=False)
                finally:
                    main.asyncio.sleep = real
                self.assertEqual(r.headers["location"], "/takk" if expect_event else r.headers["location"])
                if expect_event:
                    self.assertEqual(reported(c, "/app?vipps=ok"), [["purchase", {
                        "revenue": 249, "currency": "NOK", "payment": "vipps",
                        "items": [{"name": "Sporløs Vekst", "qty": 1, "price": 249}]}]])
                    # the agreement is now live and no longer pending: a reload counts nothing
                    c.get("/billing/vipps/retur", follow_redirects=False)
                    self.assertEqual(reported(c), [])
                else:
                    self.assertNotEqual(r.headers["location"], "/takk")
                    self.assertEqual(reported(c), [])

    def test_invoice_order_reports_the_annual_amount_excluding_vat(self):
        tid, c = account("faktura@example.no")
        data = {"plan": "vekst", "company": "Faktura AS", "org_nr": "936017207", "address": "Gate 1",
                "postal": "0461 Oslo", "invoice_email": "regnskap@example.no"}
        bad = c.post("/betal/faktura", data={**data, "org_nr": "123"}, follow_redirects=False)
        self.assertEqual(bad.status_code, 200)
        self.assertEqual(reported(c), [])  # a rejected form is not an order
        r = c.post("/betal/faktura", data=data, follow_redirects=False)
        self.assertEqual(r.headers["location"], "/takk")
        events = reported(c, "/app?faktura=ok")
        self.assertEqual(events, [["purchase", {
            "revenue": 2490, "currency": "NOK", "payment": "faktura",
            "items": [{"name": "Sporløs Vekst (årlig)", "qty": 1, "price": 2490}]}]])
        blob = json.dumps(events).lower()
        for pii in ("faktura as", "regnskap", "936017207", "gate 1", "example.no"):
            self.assertNotIn(pii, blob)

    def test_takk_page_is_not_cached_or_indexed_and_escapes_what_it_prints(self):
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route

        evil = {"e": [["purchase", {"items": [{"name": "</script><img src=x>"}]}]],
                "next": "//evil.test/x"}

        def plant(request):  # stands in for a handler that has verified a conversion
            request.session["track"] = evil
            return PlainTextResponse("planted")

        route = Route("/_plant", plant)
        main.app.router.routes.insert(0, route)
        try:
            c = TestClient(main.app)
            c.get("/_plant")
        finally:
            main.app.router.routes.remove(route)
        r = c.get("/takk", follow_redirects=False)
        self.assertEqual(r.headers["x-robots-tag"], "noindex")
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertNotIn("</script><img", r.text)       # cannot close the script tag
        self.assertNotIn("evil.test", r.text)           # a protocol-relative next is never followed
        self.assertIn('href="/app"', r.text)

    def test_tracker_turns_the_call_into_whole_ore_and_a_payment_slug(self):
        """The purchase body shape, end to end through the real ingest."""
        tenant = store.create_tenant("Ingest")
        site = store.create_site(tenant, "ingest.example")
        ua = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
        c = TestClient(main.app)
        r = c.post("/api/event", headers={"user-agent": ua, "x-forwarded-for": "84.208.10.21"},
                   content=json.dumps({"s": site["public_id"], "n": "purchase", "p": "/takk",
                                       "rv": 9900, "cur": "NOK", "pm": "stripe_card",
                                       "it": [{"n": "Sporløs Liten", "q": 1, "p": 9900}]}))
        self.assertEqual(r.status_code, 204)
        ec = store.ecommerce_stats(site["id"], 7)
        self.assertEqual(ec["by_currency"][0]["revenue_cents"], 9900)
        self.assertEqual(store.revenue_by_payment(site["id"], 7)[0]["method"], "stripe_card")


class SeedOwnGoalsTest(unittest.TestCase):
    def setUp(self):
        self.tenant = store.create_tenant("Self")
        self.site = store.create_site(self.tenant, f"self-{os.urandom(3).hex()}.example")

    def test_creates_goals_and_funnels_and_is_idempotent(self):
        pid = self.site["public_id"]
        self.assertEqual(manage.seed_own_goals(pid), (2, 2))
        goals = {g["name"]: g for g in store.goal_stats(self.site["id"], 7)}
        self.assertEqual({(g["match_type"], g["match_value"]) for g in goals.values()},
                         {("event", "signup_complete"), ("event", "purchase")})
        funnels = {f["name"]: f for f in store.funnel_stats(self.site["id"], 7)}
        steps = [(s["type"], s["value"]) for s in funnels["Forside til kjøp"]["steps"]]
        self.assertEqual(steps, [("path", "/"), ("path", "/signup"), ("event", "signup_complete"),
                                 ("event", "checkout_submit"), ("event", "purchase")])
        self.assertEqual(manage.seed_own_goals(pid), (0, 0))  # second run: nothing new
        self.assertEqual(len(store.goal_stats(self.site["id"], 7)), 2)
        self.assertEqual(len(store.funnel_stats(self.site["id"], 7)), 2)

    def test_leaves_a_hand_made_goal_alone(self):
        store.create_goal(self.site["id"], "Kjøp", "event", "purchase")
        self.assertEqual(manage.seed_own_goals(self.site["public_id"]), (1, 2))

    def test_cli_targets_the_demo_site_only_and_fails_on_an_unknown_one(self):
        old = os.environ.get("SPORLOS_DEMO_SITE")
        try:
            os.environ["SPORLOS_DEMO_SITE"] = "finnes-ikke"
            self.assertEqual(manage.main(["seed-own-goals"]), 1)
            os.environ["SPORLOS_DEMO_SITE"] = self.site["public_id"]
            self.assertEqual(manage.main(["seed-own-goals"]), 0)
        finally:
            if old is None:
                os.environ.pop("SPORLOS_DEMO_SITE", None)
            else:
                os.environ["SPORLOS_DEMO_SITE"] = old
        self.assertEqual(len(store.goal_stats(self.site["id"], 7)), 2)
