"""Account data: delete a site or the whole account yourself; invite colleagues.

Deletion: removing a site deletes every row that belongs to it and nothing of other
sites or tenants; a site of another tenant can't be deleted; account deletion is
refused while a paid subscription runs, needs the password (or, for an account
that only logs in with Google/Microsoft, a fresh login), removes every row of the
tenant and ends the session.

Users: an invite is stored hashed, works once, expires and is replaced by a new one;
it can't be sent to an address that already has an account; accepting it (password
or Google/Microsoft) creates a verified user in the inviting account, never in the
account the provider's e-mail points to; removing a user ends their sessions; all of
it only within your own account.

Run ONE FILE PER PROCESS (the backend and the innlogg env are read at import):
    .venv/bin/python3 -m pytest -q tests/test_account_data.py < /dev/null
"""

from __future__ import annotations

import hashlib
import os
import re
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


def invite(client: TestClient, email: str) -> tuple[str, str]:
    """POST the invite form. Returns (redirect location, token from the mail or "")."""
    before = len(SENT)
    r = client.post("/app/users/invite", data={"email": email}, follow_redirects=False)
    token = ""
    if len(SENT) > before:
        token = re.search(r"/invitasjon\?t=(\S+)", SENT[-1][2]).group(1)
    return r.headers["location"], token


def invites_of(tid: int) -> list[dict]:
    with store._cursor() as cur:
        cur.execute("SELECT * FROM invites WHERE tenant_id = ?", (tid,))
        return [dict(r) for r in cur.fetchall()]


class InviteTest(unittest.TestCase):
    def setUp(self):
        SENT.clear()

    def test_invite_then_join_with_a_password(self):
        tid, _ = account("Lag AS", "lag@example.no")
        store.create_site(tid, "lag.no")
        owner = login("lag@example.no")
        loc, token = invite(owner, " Ola@Example.no ")
        self.assertIn("brukere=invitert", loc)
        self.assertEqual(SENT[-1][0], "ola@example.no")
        self.assertIn("lag@example.no har invitert deg til Lag AS", SENT[-1][2])
        # Stored as sha256 only.
        rows = invites_of(tid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["token_hash"], hashlib.sha256(token.encode()).hexdigest())
        self.assertNotIn(token, " ".join(str(v) for v in rows[0].values()))
        self.assertIn("Invitert", owner.get("/app").text)

        page = TestClient(main.app).get(f"/invitasjon?t={token}").text
        self.assertIn("Bli med i Lag AS", page)
        self.assertIn("lag@example.no", page)
        self.assertIn(f'href="/invitasjon/sso/google?t={token}"', page)

        ola = TestClient(main.app)
        r = ola.post("/invitasjon", data={"t": token, "password": "kort"})
        self.assertIn("minst 8 tegn", r.text)
        self.assertEqual(len(invites_of(tid)), 1)
        r = ola.post("/invitasjon", data={"t": token, "password": "ola-passord"}, follow_redirects=False)
        self.assertEqual(r.headers["location"], "/app")
        u = store.get_user_by_email("ola@example.no")
        self.assertEqual(u["tenant_id"], tid)
        self.assertEqual(store.get_user(u["id"])["email_verified"], 1)
        self.assertIn("lag.no", ola.get("/app").text)  # same sites as the owner
        self.assertEqual(invites_of(tid), [])
        # Single use.
        again = TestClient(main.app)
        self.assertIn("Invitasjonen virker ikke", again.get(f"/invitasjon?t={token}").text)
        r = again.post("/invitasjon", data={"t": token, "password": "annet-passord"})
        self.assertIn("Invitasjonen virker ikke", r.text)
        self.assertFalse(logged_in(again))
        self.assertTrue(logged_in(login("ola@example.no", "ola-passord")))

    def test_invite_expires(self):
        tid, _ = account("Utløpt AS", "utlopt@example.no")
        _, token = invite(login("utlopt@example.no"), "sen@example.no")
        with store._cursor() as cur:
            cur.execute("UPDATE invites SET expires_at = '2000-01-01 00:00:00' WHERE tenant_id = ?", (tid,))
        c = TestClient(main.app)
        self.assertIn("Invitasjonen virker ikke", c.get(f"/invitasjon?t={token}").text)
        c.post("/invitasjon", data={"t": token, "password": "sen-passord"})
        self.assertIsNone(store.get_user_by_email("sen@example.no"))
        self.assertNotIn("sen@example.no", login("utlopt@example.no").get("/app").text)

    def test_a_new_invite_replaces_the_old_one(self):
        tid, _ = account("Igjen AS", "igjen@example.no")
        owner = login("igjen@example.no")
        _, first = invite(owner, "to-ganger@example.no")
        _, second = invite(owner, "to-ganger@example.no")
        self.assertEqual(len(invites_of(tid)), 1)
        self.assertIn("virker ikke", TestClient(main.app).get(f"/invitasjon?t={first}").text)
        self.assertIn("Bli med", TestClient(main.app).get(f"/invitasjon?t={second}").text)

    def test_an_address_with_an_account_cant_be_invited(self):
        account("Eksisterende AS", "har-konto@example.no")
        tid, _ = account("Inviterer AS", "inviterer@example.no")
        owner = login("inviterer@example.no")
        loc, token = invite(owner, "Har-Konto@example.no")
        self.assertIn("brukere=finnes", loc)
        self.assertEqual((token, invites_of(tid)), ("", []))
        self.assertIn("har allerede en Sporløs-konto", owner.get("/app?brukere=finnes").text)
        # And if the address gets an account after the invite went out:
        _, token = invite(owner, "kom-foran@example.no")
        account("Foran AS", "kom-foran@example.no")
        r = TestClient(main.app).post("/invitasjon", data={"t": token, "password": "foran-passord"})
        self.assertIn("har allerede en Sporløs-konto", r.text)
        self.assertNotEqual(store.get_user_by_email("kom-foran@example.no")["tenant_id"], tid)

    def test_unverified_address_cant_invite(self):
        store.create_account("Ubekreftet AS", "ubekreftet@example.no", hash_password(PW))
        loc, token = invite(login("ubekreftet@example.no"), "noen@example.no")
        self.assertIn("brukere=ubekreftet", loc)
        self.assertEqual(SENT, [])

    def test_invites_are_throttled(self):
        account("Mye AS", "mye@example.no")
        owner = login("mye@example.no")
        for _ in range(main.INVITES_PER_ADDRESS_HOURLY):
            self.assertIn("invitert", invite(owner, "samme@example.no")[0])
        self.assertIn("for-mange", invite(owner, "samme@example.no")[0])
        for n in range(main.INVITES_PER_ACCOUNT_HOURLY - main.INVITES_PER_ADDRESS_HOURLY):
            self.assertIn("invitert", invite(owner, f"n{n}@example.no")[0])
        self.assertIn("for-mange", invite(owner, "en-til@example.no")[0])

    def test_revoke_an_invite_only_in_your_own_account(self):
        tid, _ = account("Angre AS", "angre@example.no")
        owner = login("angre@example.no")
        _, token = invite(owner, "angret@example.no")
        inv_id = invites_of(tid)[0]["id"]
        account("Fremmed AS", "fremmed@example.no")
        stranger = login("fremmed@example.no")
        stranger.post("/app/users/invite/revoke", data={"invite_id": inv_id})
        self.assertEqual(len(invites_of(tid)), 1)
        r = owner.post("/app/users/invite/revoke", data={"invite_id": inv_id}, follow_redirects=False)
        self.assertIn("brukere=trukket", r.headers["location"])
        self.assertIn("virker ikke", TestClient(main.app).get(f"/invitasjon?t={token}").text)


class RemoveUserTest(unittest.TestCase):
    def _team(self, prefix: str):
        tid, owner_uid = account(f"{prefix} AS", f"{prefix}-eier@example.no")
        owner = login(f"{prefix}-eier@example.no")
        _, token = invite(owner, f"{prefix}-kollega@example.no")
        colleague = TestClient(main.app)
        colleague.post("/invitasjon", data={"t": token, "password": "kollega-pw"})
        self.assertTrue(logged_in(colleague))
        return tid, owner, colleague, store.get_user_by_email(f"{prefix}-kollega@example.no")

    def test_removing_a_user_ends_their_session(self):
        tid, owner, colleague, col = self._team("fjern")
        store.link_identity(col["id"], "idp-google", "g-kollega", None)
        _, their_token = invite(colleague, "via-kollega@example.no")  # dies with them
        r = owner.post("/app/users/remove", data={"user_id": col["id"]}, follow_redirects=False)
        self.assertIn("brukere=fjernet", r.headers["location"])
        self.assertFalse(logged_in(colleague))
        self.assertIsNone(store.get_user_by_email("fjern-kollega@example.no"))
        self.assertIsNone(store.user_by_identity("idp-google", "g-kollega"))
        self.assertIsNone(store.get_invite(their_token))
        self.assertTrue(logged_in(owner))
        again = TestClient(main.app)
        again.post("/login", data={"email": "fjern-kollega@example.no", "password": "kollega-pw"})
        self.assertFalse(logged_in(again))

    def test_only_within_your_own_account(self):
        tid, owner, colleague, col = self._team("isolert")
        account("Utenfor AS", "utenfor@example.no")
        outsider = login("utenfor@example.no")
        r = outsider.post("/app/users/remove", data={"user_id": col["id"]}, follow_redirects=False)
        self.assertEqual(r.headers["location"], "/app#brukere")
        self.assertTrue(logged_in(colleague))
        self.assertIsNotNone(store.get_user_by_email("isolert-kollega@example.no"))
        self.assertFalse(store.remove_user(col["id"], store.get_user_by_email("utenfor@example.no")["tenant_id"]))
        # Logged out: nothing.
        r = TestClient(main.app).post("/app/users/remove", data={"user_id": col["id"]}, follow_redirects=False)
        self.assertEqual(r.headers["location"], "/login")
        self.assertTrue(logged_in(colleague))

    def test_you_cant_remove_yourself(self):
        tid, owner, colleague, col = self._team("selv")
        me = store.get_user_by_email("selv-eier@example.no")
        r = owner.post("/app/users/remove", data={"user_id": me["id"]}, follow_redirects=False)
        self.assertIn("brukere=deg", r.headers["location"])
        self.assertTrue(logged_in(owner))

    def test_the_owner_cant_be_removed_by_a_colleague(self):
        tid, owner, colleague, col = self._team("eierfjern")
        owner_row = store.get_user_by_email("eierfjern-eier@example.no")
        r = colleague.post("/app/users/remove", data={"user_id": owner_row["id"]}, follow_redirects=False)
        self.assertIn("brukere=eier", r.headers["location"])
        self.assertTrue(logged_in(owner))
        self.assertNotIn("Fjern</button>", colleague.get("/app").text.split('id=brukere')[1].split("</table>")[0])

    def test_only_the_owner_can_delete_the_account(self):
        tid, owner, colleague, col = self._team("eierslett")
        page = colleague.get("/app").text
        self.assertIn("som opprettet kontoen, kan slette den", page)
        r = colleague.post("/app/account/delete", data={"confirm": "eierslett AS", "password": "kollega-pw"},
                           follow_redirects=False)
        self.assertIn("konto=ikke-eier", r.headers["location"])
        self.assertEqual(tenant_rows(tid)["users"], 2)
        self.assertTrue(logged_in(owner))

    def test_account_deletion_takes_colleagues_and_invites_along(self):
        tid, owner, colleague, col = self._team("alle")
        invite(owner, "alle-apen@example.no")
        SENT.clear()
        owner.post("/app/account/delete", data={"confirm": "alle AS", "password": PW})
        self.assertEqual(tenant_rows(tid)["users"], 0)
        self.assertEqual(invites_of(tid), [])
        self.assertFalse(logged_in(colleague))
        self.assertEqual(sorted(s[0] for s in SENT), ["alle-eier@example.no", "alle-kollega@example.no"])


class InviteWithGoogleTest(unittest.TestCase):
    def _join(self, client, token, sub, email):
        NEXT["who"] = innlogg.IdpLogin("google", "idp-google", sub, email, True, "Kari")
        r = client.get(f"/invitasjon/sso/google?t={token}", follow_redirects=False)
        self.assertTrue(r.headers["location"].startswith("https://provider.example/"))
        cb = urlparse(NEXT["success_url"])
        return client.get(f"{cb.path}?{cb.query}&id=intent1&token=tok1", follow_redirects=False)

    def test_the_invite_decides_the_account_not_the_providers_email(self):
        # The Google address belongs to an existing account elsewhere; the invite still wins.
        elsewhere_tid, _ = account("Et annet sted AS", "privat@gmail.example")
        tid, _ = account("Google-lag AS", "glag@example.no")
        _, token = invite(login("glag@example.no"), "kari@jobb.example")
        c = TestClient(main.app)
        r = self._join(c, token, "g-kari", "privat@gmail.example")
        self.assertEqual(r.headers["location"], "/app")
        u = store.get_user_by_email("kari@jobb.example")
        self.assertEqual(u["tenant_id"], tid)
        self.assertTrue(u["password_hash"].startswith("!"))
        self.assertEqual(store.user_by_identity("idp-google", "g-kari")["id"], u["id"])
        self.assertEqual(store.get_user_by_email("privat@gmail.example")["tenant_id"], elsewhere_tid)
        self.assertTrue(logged_in(c))
        self.assertIsNone(store.get_invite(token))
        # Next time the same Google login lands in the invited account.
        c2 = TestClient(main.app)
        NEXT["who"] = innlogg.IdpLogin("google", "idp-google", "g-kari", "privat@gmail.example", True, "Kari")
        c2.get("/auth/sso/start/google", follow_redirects=False)
        cb = urlparse(NEXT["success_url"])
        c2.get(f"{cb.path}?{cb.query}&id=i&token=t", follow_redirects=False)
        self.assertEqual(c2.get("/app").status_code, 200)

    def test_a_login_already_bound_to_someone_is_refused(self):
        _, uid = account("Bundet AS", "bundet@example.no")
        store.link_identity(uid, "idp-google", "g-bundet", "bundet@gmail.example")
        tid, _ = account("Vil ha AS", "vilha@example.no")
        _, token = invite(login("vilha@example.no"), "ny-kollega@example.no")
        c = TestClient(main.app)
        r = self._join(c, token, "g-bundet", "bundet@gmail.example")
        self.assertIn("allerede i bruk", r.text)
        self.assertIsNone(store.get_user_by_email("ny-kollega@example.no"))
        self.assertFalse(logged_in(c))
        self.assertIsNotNone(store.get_invite(token))  # still usable with a password

    def test_a_dead_invite_cant_start_the_flow(self):
        r = TestClient(main.app).get("/invitasjon/sso/google?t=feil", follow_redirects=False)
        self.assertIn("Invitasjonen virker ikke", r.text)


if __name__ == "__main__":
    unittest.main()
