"""Self-serve data deletion: remove one site, or the whole account.

Covers: deleting a site removes every row that belongs to it and nothing of other
sites or tenants; a site of another tenant can't be deleted; account deletion is
refused while a paid subscription runs, needs the password (or, for an account
that only logs in with Google/Microsoft, a fresh login), removes every row of the
tenant and ends the session.

Run ONE FILE PER PROCESS (the backend and the innlogg env are read at import):
    .venv/bin/python3 -m pytest -q tests/test_account_data.py < /dev/null
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlparse

_TMP_DB = tempfile.NamedTemporaryFile(prefix="sporlos_test_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ["SPORLOS_DB"] = _TMP_DB.name
os.environ.pop("DATABASE_URL", None)
os.environ["DATAMYNT_LOGIN_PAT"] = "test-pat"
os.environ["IDP_GOOGLE_ID"] = "idp-google"
os.environ["IDP_MICROSOFT_ID"] = "idp-microsoft"

from starlette.testclient import TestClient  # noqa: E402

from app import innlogg, main, mailer, store  # noqa: E402
from app.auth import hash_password  # noqa: E402

PW = "passord-123"
SENT: list[tuple[str, str, str]] = []
NEXT: dict = {}


async def _fake_start(provider, success_url, failure_url):
    NEXT["success_url"] = success_url
    return f"https://provider.example/{provider}"


async def _fake_finish(provider, intent_id, intent_token):
    return NEXT["who"]


def _fake_send(to, subject, body):
    SENT.append((to, subject, body))
    return True


def setUpModule():
    store.init_db()
    # `anchors` exists only in the Postgres schema; mirror it so its delete path runs here too.
    with store._cursor() as cur:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS anchors (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "site_id INTEGER NOT NULL, period TEXT, rollup_hash TEXT NOT NULL, txid TEXT, anchored_at TEXT)"
        )
    innlogg.start = _fake_start
    innlogg.finish = _fake_finish
    mailer.send = _fake_send


def tearDownModule():
    os.unlink(_TMP_DB.name)


def account(company: str, email: str, password: str = PW) -> tuple[int, int]:
    tid, uid = store.create_account(company, email, hash_password(password))
    store.set_email_verified(uid)
    return tid, uid


def login(email: str, password: str = PW) -> TestClient:
    c = TestClient(main.app)
    r = c.post("/login", data={"email": email, "password": password}, follow_redirects=False)
    assert r.headers["location"] == "/app", r.headers.get("location")
    return c


def logged_in(c: TestClient) -> bool:
    return c.get("/app", follow_redirects=False).status_code == 200


def seed_site(tid: int, domain: str) -> dict:
    """A site with a row in every table that can hold site data."""
    site = store.create_site(tid, domain)
    sid = site["id"]
    ev = {"name": "purchase", "path": "/kasse", "visitor_hash": "v1", "revenue_cents": 9900, "currency": "NOK"}
    store.insert_event(sid, ev, items=[{"name": "Kaffe", "qty": 1, "unit_price_cents": 9900}])
    store.insert_event(sid, {"name": "pageview", "path": "/", "visitor_hash": "v1"})
    store.create_goal(sid, "Kjøp", "event", "purchase")
    store.create_funnel(sid, "Trakt", [{"type": "path", "value": "/"}, {"type": "event", "value": "purchase"}])
    store.compute_rollup(sid, "2026-09-01")
    store.record_anchor([{"site_id": sid, "day": "2026-09-01", "root": "ab" * 32,
                          "proof": "[]", "txid": "cd" * 32, "anchored_at": "2026-09-02 00:00:00"}])
    store.upsert_search_rows([(sid, "2026-09-01", "google", "total", "", 3, 40, 7.5)])
    store.set_search_connection(sid, "encrypted-token", "sc-domain:" + domain, "owner@example.no")
    with store._cursor() as cur:
        cur.execute("INSERT INTO anchors (site_id, rollup_hash) VALUES (?, ?)", (sid, "ef" * 32))
    return site


def site_rows(site_id: int) -> dict[str, int]:
    out = {}
    with store._cursor() as cur:
        for table in store.SITE_TABLES:
            cur.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE site_id = ?", (site_id,))
            out[table] = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM sites WHERE id = ?", (site_id,))
        out["sites"] = cur.fetchone()["n"]
    return out


def tenant_rows(tid: int) -> dict[str, int]:
    out = {}
    with store._cursor() as cur:
        for table, where in (("tenants", "id = ?"), ("users", "tenant_id = ?"),
                             ("sites", "tenant_id = ?"), ("api_keys", "tenant_id = ?")):
            cur.execute(f"SELECT COUNT(*) AS n FROM {table} WHERE {where}", (tid,))
            out[table] = cur.fetchone()["n"]
    return out


class SiteDeletionTest(unittest.TestCase):
    def test_deleting_a_site_removes_all_its_rows_and_nothing_else(self):
        tid, _ = account("Kaffe AS", "kaffe@example.no")
        gone, kept = seed_site(tid, "kaffe.no"), seed_site(tid, "blogg.kaffe.no")
        other_tid, _ = account("Annen AS", "annen-site@example.no")
        other = seed_site(other_tid, "annen.no")
        self.assertTrue(all(n == 1 for t, n in site_rows(gone["id"]).items() if t not in ("events",)))

        c = login("kaffe@example.no")
        r = c.post("/app/sites/delete", data={"site": gone["public_id"], "confirm": "Kaffe.no "},
                   follow_redirects=False)
        self.assertEqual(r.headers["location"], "/app")
        self.assertEqual(set(site_rows(gone["id"]).values()), {0})
        self.assertIsNone(store.resolve_site_cached(gone["public_id"]))
        for s in (kept, other):
            rows = site_rows(s["id"])
            self.assertEqual(rows["events"], 2)
            self.assertTrue(all(n >= 1 for n in rows.values()), rows)
        # One-time confirmation on the overview, then gone.
        self.assertIn("kaffe.no</b> er slettet", c.get("/app").text)
        self.assertNotIn("er slettet, med all statistikk", c.get("/app").text)

    def test_site_is_kept_when_the_typed_domain_is_wrong(self):
        tid, _ = account("Feilskrift AS", "feilskrift@example.no")
        site = seed_site(tid, "feilskrift.no")
        c = login("feilskrift@example.no")
        for typed in ("", "feilskrift", "annen.no"):
            r = c.post("/app/sites/delete", data={"site": site["public_id"], "confirm": typed},
                       follow_redirects=False)
            self.assertIn("slett=feil", r.headers["location"])
        self.assertEqual(site_rows(site["id"])["sites"], 1)
        self.assertEqual(site_rows(site["id"])["events"], 2)

    def test_another_tenants_site_cant_be_deleted(self):
        victim_tid, _ = account("Offer AS", "offer-site@example.no")
        site = seed_site(victim_tid, "offer.no")
        account("Angriper AS", "angriper-site@example.no")
        c = login("angriper-site@example.no")
        r = c.post("/app/sites/delete", data={"site": site["public_id"], "confirm": "offer.no"},
                   follow_redirects=False)
        self.assertEqual(r.headers["location"], "/app")
        self.assertEqual(site_rows(site["id"])["events"], 2)
        # The store refuses it too, whatever the caller does.
        attacker = store.get_user_by_email("angriper-site@example.no")
        self.assertIsNone(store.delete_site(site["id"], attacker["tenant_id"]))
        self.assertEqual(site_rows(site["id"])["sites"], 1)

    def test_logged_out_post_does_nothing(self):
        tid, _ = account("Utlogget AS", "utlogget@example.no")
        site = seed_site(tid, "utlogget.no")
        r = TestClient(main.app).post("/app/sites/delete", data={"site": site["public_id"], "confirm": "utlogget.no"},
                                      follow_redirects=False)
        self.assertEqual(r.headers["location"], "/login")
        self.assertEqual(site_rows(site["id"])["sites"], 1)


class AccountDeletionTest(unittest.TestCase):
    def setUp(self):
        SENT.clear()

    def test_refused_while_a_paid_subscription_runs(self):
        for n, kwargs in enumerate((
            {"plan": "vekst", "customer_id": "cus_1", "sub_id": "sub_1"},   # card (Stripe)
            {"plan": "liten", "vipps": "agr_1"},                           # Vipps
        )):
            email = f"betaler{n}@example.no"
            tid, _ = account(f"Betaler {n} AS", email)
            if "vipps" in kwargs:
                store.set_vipps_pending(tid, kwargs["vipps"], kwargs["plan"])
                store.activate_vipps(tid, kwargs["plan"], "2026-10-30")
            else:
                store.set_tenant_plan(tid, kwargs["plan"], customer_id=kwargs["customer_id"], sub_id=kwargs["sub_id"])
            c = login(email)
            page = c.get("/app").text
            self.assertNotIn('action="/app/account/delete"', page)  # no form, the reason instead
            self.assertIn("/billing/portal" if "sub_id" in kwargs else "/billing/vipps/avslutt", page)
            r = c.post("/app/account/delete", data={"confirm": email, "password": PW}, follow_redirects=False)
            self.assertIn("konto=abonnement", r.headers["location"])
            self.assertEqual(tenant_rows(tid)["tenants"], 1)
            self.assertTrue(logged_in(c))
        self.assertEqual(SENT, [])

    def test_cancelled_plan_can_be_deleted(self):
        tid, _ = account("Sagt opp AS", "sagtopp@example.no")
        store.set_tenant_plan(tid, "cancelled", customer_id="cus_2", sub_id="sub_2")
        c = login("sagtopp@example.no")
        c.post("/app/account/delete", data={"confirm": "Sagt opp AS", "password": PW})
        self.assertEqual(tenant_rows(tid)["tenants"], 0)

    def test_needs_the_password_and_the_typed_confirmation(self):
        tid, _ = account("Passord AS", "passord@example.no")
        c = login("passord@example.no")
        r = c.post("/app/account/delete", data={"confirm": "Passord AS", "password": "feil-passord"},
                   follow_redirects=False)
        self.assertIn("konto=passord", r.headers["location"])
        r = c.post("/app/account/delete", data={"confirm": "Noe annet AS", "password": PW},
                   follow_redirects=False)
        self.assertIn("konto=feil", r.headers["location"])
        self.assertEqual(tenant_rows(tid)["tenants"], 1)
        self.assertTrue(logged_in(c))

    def test_removes_every_row_of_the_tenant_and_logs_out(self):
        tid, uid = account("Hele AS", "hele@example.no")
        a, b = seed_site(tid, "hele.no"), seed_site(tid, "hele.com")
        store.create_api_key(tid, "Claude")
        store.link_identity(uid, "idp-google", "g-hele", "hele@gmail.com")
        store.create_reset_token("hele@example.no")
        other_tid, _ = account("Nabo AS", "nabo@example.no")
        neighbour = seed_site(other_tid, "nabo.no")
        store.create_api_key(other_tid, "Nabo")

        c = login("hele@example.no")
        second = login("hele@example.no")  # another browser of the same user
        r = c.post("/app/account/delete", data={"confirm": "HELE@example.no", "password": PW})
        self.assertIn("Kontoen er slettet", r.text)

        self.assertEqual(set(tenant_rows(tid).values()), {0})
        for s in (a, b):
            self.assertEqual(set(site_rows(s["id"]).values()), {0})
        self.assertIsNone(store.user_by_identity("idp-google", "g-hele"))
        with store._cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM reset_tokens WHERE email = ?", ("hele@example.no",))
            self.assertEqual(cur.fetchone()["n"], 0)
        self.assertFalse(logged_in(c))
        self.assertFalse(logged_in(second))
        # The neighbour is untouched.
        self.assertEqual(tenant_rows(other_tid), {"tenants": 1, "users": 1, "sites": 1, "api_keys": 1})
        self.assertEqual(site_rows(neighbour["id"])["events"], 2)
        # A short Norwegian confirmation to the account's address.
        self.assertEqual([s[0] for s in SENT], ["hele@example.no"])
        self.assertIn("Kontoen og alle data er slettet", SENT[0][2])

    def test_logged_out_post_does_nothing(self):
        tid, _ = account("Ikke innlogget AS", "ikke-innlogget@example.no")
        r = TestClient(main.app).post("/app/account/delete", data={"confirm": "Ikke innlogget AS", "password": PW},
                                      follow_redirects=False)
        self.assertEqual(r.headers["location"], "/login")
        self.assertEqual(tenant_rows(tid)["tenants"], 1)


class SsoOnlyAccountDeletionTest(unittest.TestCase):
    """No password to ask for: the proof is a login from the last 10 minutes."""

    def _sso(self, client, sub, email, query=""):
        NEXT["who"] = innlogg.IdpLogin("google", "idp-google", sub, email, True, "Kari")
        client.get(f"/auth/sso/start/google{query}", follow_redirects=False)
        cb = urlparse(NEXT["success_url"])
        return client.get(f"{cb.path}?{cb.query}&id=intent1&token=tok1", follow_redirects=False)

    def test_fresh_login_required_then_deleted(self):
        c = TestClient(main.app)
        self._sso(c, "g-sso-only", "sso-only@example.no")
        u = store.get_user_by_email("sso-only@example.no")
        self.assertTrue(u["password_hash"].startswith("!"))

        with mock.patch.object(main, "_REAUTH_SECONDS", -1):  # the login is now "too old"
            page = c.get("/app").text
            self.assertIn("/auth/sso/start/google?reauth=1", page)
            self.assertNotIn('action="/app/account/delete"', page)
            r = c.post("/app/account/delete", data={"confirm": "sso-only@example.no"}, follow_redirects=False)
            self.assertIn("konto=logginn", r.headers["location"])
            self.assertIsNotNone(store.get_user_by_email("sso-only@example.no"))

        # Someone else's Google account doesn't count as a fresh login.
        store.link_identity(store.create_account("X", "x-sso@example.no", "!sso")[1], "idp-google", "g-x", None)
        r = self._sso(c, "g-x", "x@gmail.com", "?reauth=1")
        self.assertIn("konto=annen", r.headers["location"])

        r = self._sso(c, "g-sso-only", "sso-only@example.no", "?reauth=1")
        self.assertIn("konto=klar", r.headers["location"])
        self.assertIn('action="/app/account/delete"', c.get("/app").text)
        c.post("/app/account/delete", data={"confirm": "sso-only@example.no"})
        self.assertIsNone(store.get_user_by_email("sso-only@example.no"))
        self.assertIsNone(store.user_by_identity("idp-google", "g-sso-only"))
        self.assertFalse(logged_in(c))


if __name__ == "__main__":
    unittest.main()
