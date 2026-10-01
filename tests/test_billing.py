"""Billing: VAT on every charge, both prices shown, company details in Stripe Checkout,
plan changes and cancellation from the Stripe portal, and the annual invoice for businesses.

Stripe, Vipps and mail are faked: nothing here touches money or sends mail.
Run ONE FILE PER PROCESS:
    .venv/bin/python3 -m pytest -q tests/test_billing.py < /dev/null
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

_TMP_DB = tempfile.NamedTemporaryFile(prefix="sporlos_test_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ["SPORLOS_DB"] = _TMP_DB.name
os.environ.pop("DATABASE_URL", None)
os.environ["MAIL_FROM"] = "post@sporlos.no"

from starlette.testclient import TestClient  # noqa: E402

from app import main, mailer, notify, pricing, store, vipps  # noqa: E402
from app.auth import hash_password  # noqa: E402

SENT: list[tuple[str, str, str]] = []
CHECKOUTS: list[dict] = []
MODIFIED: list[tuple] = []
CREATED: list[dict] = []
PORTALS: list[dict] = []
SUBS: dict[str, dict] = {}  # what Subscription.retrieve answers, by id

OCT_30 = 1793370445  # 2026-10-30, the cancel_at Stripe set on the live test subscription


class _FakeStripe:
    class checkout:
        class Session:
            @staticmethod
            def create(**kw):
                CHECKOUTS.append(kw)

                class S:
                    url = "https://checkout.stripe.test/s"
                return S()

    class Webhook:
        @staticmethod
        def construct_event(payload, sig, secret):
            return None  # signature check is covered elsewhere; here we test the handling

    class billing_portal:
        class Session:
            @staticmethod
            def create(**kw):
                PORTALS.append(kw)

                class S:
                    url = "https://billing.stripe.test/p"
                return S()

    class Subscription:
        @staticmethod
        def retrieve(sid):
            class S:
                @staticmethod
                def to_dict():
                    return SUBS[sid]
            return S()

    class Customer:
        @staticmethod
        def modify(cid, **kw):
            MODIFIED.append((cid, kw))

        @staticmethod
        def create(**kw):
            CREATED.append(kw)
            return {"id": f"cus_new{len(CREATED)}"}


def setUpModule():
    store.init_db()
    mailer.send = lambda to, subject, body: SENT.append((to, subject, body)) or True
    main.stripe = _FakeStripe
    main.STRIPE_PRICES = {"liten": "price_liten", "vekst": "price_vekst", "pro": "price_pro"}
    main.STRIPE_TAX_RATE = "txr_mva25"


def tearDownModule():
    os.unlink(_TMP_DB.name)


def account(email: str, company: str = "Firma AS") -> tuple[int, int, TestClient]:
    tid, uid = store.create_account(company, email, hash_password("passord-123"))
    store.set_email_verified(uid)
    c = TestClient(main.app)
    c.post("/login", data={"email": email, "password": "passord-123"})
    return tid, uid, c


class PricingTest(unittest.TestCase):
    def test_vat_and_formatting(self):
        self.assertEqual(pricing.incl(9900), 12375)
        self.assertEqual(pricing.kr(12375), "123,75")
        self.assertEqual(pricing.kr(99000), "990")
        self.assertEqual(pricing.kr(123750), "1 237,50")
        self.assertEqual(pricing.annual_ore("liten"), 99000)

    def test_org_nr_check_digit(self):
        self.assertTrue(main._valid_org_nr("936017207"))  # Datamynt AS
        self.assertFalse(main._valid_org_nr("936017208"))
        self.assertFalse(main._valid_org_nr("12345678"))


class PublicPricesTest(unittest.TestCase):
    def test_landing_shows_both_prices(self):
        html = TestClient(main.app).get("/").text
        for ex, inkl in (("99", "123,75"), ("249", "311,25"), ("599", "748,75")):
            self.assertIn(f"{ex} kr<small>/mnd</small>", html)
            self.assertIn(f"{inkl} kr inkl. mva", html)
        self.assertIn("årlig mot", html)

    def test_terms_state_vat_registration(self):
        html = TestClient(main.app).get("/vilkar").text
        self.assertNotIn("ikke mva-registrert", html)
        self.assertIn("936 017 207 MVA", html)


class VippsVatTest(unittest.TestCase):
    def test_agreement_and_charges_include_vat(self):
        sent = []

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"agreementId": "a1", "vippsConfirmationUrl": "https://vipps.test", "chargeId": "c1"}

        real_post, real_headers = vipps.httpx.post, vipps._headers
        vipps.httpx.post = lambda url, json=None, headers=None, timeout=None: sent.append(json) or R()
        vipps._headers = lambda idempotency_key=None: {}
        try:
            vipps.create_agreement("liten", "https://sporlos.no")
            import datetime as dt
            vipps.create_charge(1, "a1", "vekst", dt.date(2026, 11, 1))
        finally:
            vipps.httpx.post, vipps._headers = real_post, real_headers
        self.assertEqual(sent[0]["pricing"]["amount"], 12375)
        self.assertEqual(sent[0]["initialCharge"]["amount"], 12375)
        self.assertEqual(sent[1]["amount"], 31125)


class StripeCheckoutTest(unittest.TestCase):
    def test_checkout_adds_vat_and_asks_for_company_details(self):
        tid, uid, c = account("kort@example.no")
        CHECKOUTS.clear()
        r = c.get("/billing/checkout?plan=vekst", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        kw = CHECKOUTS[-1]
        self.assertEqual(kw["subscription_data"]["default_tax_rates"], ["txr_mva25"])
        self.assertEqual(kw["tax_id_collection"], {"enabled": True})
        self.assertEqual(kw["billing_address_collection"], "required")
        self.assertEqual(kw["locale"], "nb")
        # The customer is created first, with the seller footer, so the FIRST invoice has it.
        made = CREATED[-1]
        self.assertIn("936 017 207 MVA", made["invoice_settings"]["footer"])
        self.assertEqual(made["email"], "kort@example.no")
        self.assertEqual(kw["customer"], store.get_tenant(tid)["stripe_customer_id"])
        self.assertEqual(kw["customer_update"], {"name": "auto", "address": "auto"})
        self.assertEqual(store.get_tenant(tid)["plan"], "trial")  # the webhook sets the plan
        # A returning customer: the footer is refreshed, no second customer is made.
        n = len(CREATED)
        MODIFIED.clear()
        store.set_tenant_plan(tid, "cancelled", customer_id="cus_1")
        c.get("/billing/checkout?plan=liten", follow_redirects=False)
        self.assertEqual(len(CREATED), n)
        self.assertEqual(MODIFIED[-1][0], "cus_1")
        self.assertEqual(CHECKOUTS[-1]["customer"], "cus_1")

    def _hook(self, typ: str, obj: dict):
        body = json.dumps({"type": typ, "data": {"object": obj}})
        return TestClient(main.app).post("/webhooks/stripe", content=body,
                                         headers={"stripe-signature": "t"})

    def test_completed_sets_plan_and_invoice_footer(self):
        tid, uid, c = account("kjop@example.no")
        MODIFIED.clear()
        self._hook("checkout.session.completed", {
            "client_reference_id": str(tid), "metadata": {"plan": "pro"},
            "customer": "cus_kjop", "subscription": "sub_1"})
        self.assertEqual(store.get_tenant(tid)["plan"], "pro")
        self.assertEqual(MODIFIED[-1][0], "cus_kjop")
        self.assertIn("936 017 207 MVA", MODIFIED[-1][1]["invoice_settings"]["footer"])

    def test_plan_change_in_portal_follows_the_price(self):
        tid, uid, c = account("bytt@example.no")
        store.set_tenant_plan(tid, "liten", customer_id="cus_bytt", sub_id="sub_b")
        self._hook("customer.subscription.updated", {
            "customer": "cus_bytt", "status": "active",
            "items": {"data": [{"price": {"id": "price_pro"}}]}})
        self.assertEqual(store.get_tenant(tid)["plan"], "pro")
        # Unknown price or inactive status: leave the plan alone.
        self._hook("customer.subscription.updated", {
            "customer": "cus_bytt", "status": "incomplete_expired",
            "items": {"data": [{"price": {"id": "price_liten"}}]}})
        self.assertEqual(store.get_tenant(tid)["plan"], "pro")


class CancelTest(unittest.TestCase):
    """Cancelling in the Stripe portal: the portal speaks Norwegian, and Sporløs shows the
    cancellation and its end date instead of a plain «Plan: Liten»."""

    def _hook(self, typ: str, obj: dict):
        body = json.dumps({"type": typ, "data": {"object": obj}})
        return TestClient(main.app).post("/webhooks/stripe", content=body,
                                         headers={"stripe-signature": "t"})

    def _sub(self, sid: str, cust: str, **kw) -> dict:
        return {"id": sid, "customer": cust, "status": "active",
                "items": {"data": [{"price": {"id": "price_liten"}, "current_period_end": OCT_30}]}, **kw}

    def test_portal_is_norwegian_and_comes_back_to_app(self):
        tid, uid, c = account("portal@example.no")
        store.set_tenant_plan(tid, "liten", customer_id="cus_p", sub_id="sub_p")
        PORTALS.clear()
        r = c.get("/billing/portal", follow_redirects=False)
        self.assertEqual(r.headers["location"], "https://billing.stripe.test/p")
        self.assertEqual(PORTALS[-1]["locale"], "nb")
        self.assertTrue(PORTALS[-1]["return_url"].endswith("/app?fra=stripe"))

    def test_cancel_shows_end_date_and_resume_clears_it(self):
        tid, uid, c = account("sioppp@example.no")
        store.set_tenant_plan(tid, "liten", customer_id="cus_s", sub_id="sub_s")
        self._hook("customer.subscription.updated", self._sub("sub_s", "cus_s", cancel_at=OCT_30))
        t = store.get_tenant(tid)
        self.assertEqual(t["plan"], "liten")  # paid for: keeps the plan until the end
        self.assertEqual(t["plan_ends_at"], "2026-10-30")
        page = c.get("/app").text
        self.assertIn("sagt opp, gjelder til 30.10.2026", page)
        self.assertIn("Fortsett abonnementet", page)
        self._hook("customer.subscription.updated", self._sub("sub_s", "cus_s", cancel_at=None))
        self.assertIsNone(store.get_tenant(tid)["plan_ends_at"])
        self.assertIn("Administrer abonnement", c.get("/app").text)

    def test_older_api_flag_uses_the_period_end(self):
        tid, uid, c = account("flagg@example.no")
        store.set_tenant_plan(tid, "liten", customer_id="cus_f", sub_id="sub_f")
        self._hook("customer.subscription.updated",
                   self._sub("sub_f", "cus_f", cancel_at=None, cancel_at_period_end=True))
        self.assertEqual(store.get_tenant(tid)["plan_ends_at"], "2026-10-30")

    def test_return_from_portal_reads_stripe_and_confirms(self):
        tid, uid, c = account("retur@example.no")
        store.set_tenant_plan(tid, "vekst", customer_id="cus_r", sub_id="sub_r")
        SUBS["sub_r"] = self._sub("sub_r", "cus_r", cancel_at=OCT_30,
                                  items={"data": [{"price": {"id": "price_vekst"}}]})
        page = c.get("/app?fra=stripe").text  # no webhook yet
        self.assertIn("Abonnementet er sagt opp.", page)
        self.assertIn("Du beholder Vekst til 30.10.2026", page)
        self.assertEqual(store.get_tenant(tid)["plan_ends_at"], "2026-10-30")

    def test_end_cancels_plan_but_stale_subscription_is_ignored(self):
        tid, uid, c = account("slutt@example.no")
        store.set_tenant_plan(tid, "liten", customer_id="cus_e", sub_id="sub_new")
        self._hook("customer.subscription.deleted", self._sub("sub_old", "cus_e", status="canceled"))
        self.assertEqual(store.get_tenant(tid)["plan"], "liten")
        self._hook("customer.subscription.deleted", self._sub("sub_new", "cus_e", status="canceled"))
        self.assertEqual(store.get_tenant(tid)["plan"], "cancelled")

    def test_cancelled_subscription_no_longer_blocks_deletion(self):
        tid, uid, c = account("slett@example.no", "Slett AS")
        store.set_tenant_plan(tid, "liten", customer_id="cus_x", sub_id="sub_x")
        page = c.get("/app").text
        self.assertIn("Si det opp under", page)
        self.assertNotIn('action="/app/account/delete"', page)
        store.set_plan_ends_at(tid, "2026-10-30")
        page = c.get("/app").text
        self.assertIn('action="/app/account/delete"', page)
        self.assertIn("mister du resten av perioden", page)
        c.post("/app/account/delete", data={"confirm": "Slett AS", "password": "passord-123"})
        self.assertIsNone(store.get_tenant(tid))
        # Stripe ends the subscription on 30.10; the event then finds no tenant and is a no-op.
        r = self._hook("customer.subscription.deleted", self._sub("sub_x", "cus_x", status="canceled"))
        self.assertEqual(r.status_code, 200)


class InvoiceTest(unittest.TestCase):
    def test_betal_offers_invoice_and_shows_vat(self):
        tid, uid, c = account("velg@example.no")
        html = c.get("/betal?plan=liten").text
        self.assertIn("123,75 kr/mnd inkl. mva", html)
        self.assertIn('href="/betal/faktura?plan=liten"', html)
        self.assertIn("1 237,50 kr inkl. mva", html)

    def test_invoice_order_starts_the_plan_and_mails_both(self):
        tid, uid, c = account("faktura@example.no", "Faktura AS")
        form = c.get("/betal/faktura?plan=vekst").text
        self.assertIn("2 490 kr eks. mva", form)
        SENT.clear()
        bad = c.post("/betal/faktura", data={"plan": "vekst", "company": "Faktura AS", "org_nr": "123",
                                             "address": "Gate 1", "postal": "0461 Oslo",
                                             "invoice_email": "regnskap@example.no"})
        self.assertIn("Organisasjonsnummeret", bad.text)
        self.assertEqual(SENT, [])
        r = c.post("/betal/faktura", data={"plan": "vekst", "company": "Faktura AS", "org_nr": "936 017 207",
                                           "address": "Gate 1", "postal": "0461 Oslo",
                                           "invoice_email": "regnskap@example.no", "ehf": "1",
                                           "reference": "PO-7"}, follow_redirects=False)
        self.assertIn("faktura=ok", r.headers["location"])
        t = store.get_tenant(tid)
        self.assertEqual(t["plan"], "vekst")
        details = json.loads(t["invoice_details"])
        self.assertEqual(details["org_nr"], "936017207")
        self.assertEqual(details["amount_incl_vat_ore"], 311250)
        self.assertTrue(details["ehf"])
        to = sorted(s[0] for s in SENT)
        self.assertEqual(to, ["post@sporlos.no", "regnskap@example.no"])
        admin = next(s for s in SENT if s[0] == "post@sporlos.no")
        self.assertIn("3 112,50", admin[2])
        self.assertIn("EHF", admin[2])
        page = c.get("/app").text
        self.assertIn("årlig faktura", page)
        self.assertIn("skriv til post@sporlos.no, så avslutter vi avtalen".lower(), page.lower())

    def test_existing_card_subscription_is_not_doubled(self):
        tid, uid, c = account("dobbel@example.no")
        store.set_tenant_plan(tid, "liten", customer_id="cus_d", sub_id="sub_d")
        SENT.clear()
        r = c.post("/betal/faktura", data={"plan": "vekst", "company": "D AS", "org_nr": "936017207",
                                           "address": "Gate 1", "postal": "0461 Oslo",
                                           "invoice_email": "d@example.no"})
        self.assertIn("allerede et abonnement", r.text)
        self.assertEqual(store.get_tenant(tid)["plan"], "liten")
        self.assertEqual(SENT, [])


class ReminderTextTest(unittest.TestCase):
    def test_trial_reminder_is_proper_norwegian(self):
        SENT.clear()
        tid, uid = store.create_account("Snart AS", "snart@example.no", hash_password("passord-123"),
                                        trial_days=1)
        notify.send_trial_reminders(within_days=3)
        mail = next(s for s in SENT if s[0] == "snart@example.no")
        self.assertIn("Prøveperioden", mail[1])
        self.assertIn("Sporløs", mail[2])


if __name__ == "__main__":
    unittest.main()
