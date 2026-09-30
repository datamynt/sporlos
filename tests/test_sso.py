"""Google/Microsoft login through Datamynt ID, with the Zitadel calls faked.

Covers the binding rules in main.sso_callback / sso_confirm: logins bind to the
provider's user id; Google's verified address may attach to an existing account;
a Microsoft address must be confirmed by mail, in the same browser; a squatter's
unverified account is taken back by the proven owner.

Run ONE FILE PER PROCESS (the backend and the innlogg env are read at import):
    .venv/bin/python3 -m pytest -q tests/test_sso.py < /dev/null
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from urllib.parse import parse_qs, urlparse

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

SENT: list[tuple[str, str, str]] = []
NEXT: dict = {}


async def _fake_start(provider, success_url, failure_url):
    NEXT["success_url"] = success_url
    return f"https://provider.example/{provider}"


async def _fake_finish(provider, intent_id, intent_token):
    who = NEXT["who"]
    if who.provider != provider:
        raise innlogg.IdpError("idp mismatch")
    return who


def setUpModule():
    store.init_db()
    innlogg.start = _fake_start
    innlogg.finish = _fake_finish
    mailer.send = lambda to, subject, body: SENT.append((to, subject, body))


def tearDownModule():
    os.unlink(_TMP_DB.name)


def google(sub, email, name="Kari"):
    return innlogg.IdpLogin("google", "idp-google", sub, email, True, name)


def microsoft(sub, email, name="Ola"):
    return innlogg.IdpLogin("microsoft", "idp-microsoft", sub, email, False, name)


class SsoTest(unittest.TestCase):
    def setUp(self):
        SENT.clear()

    def _run(self, client, who, provider=None):
        """Button click -> provider -> callback. Returns the callback response."""
        NEXT["who"] = who
        r = client.get(f"/auth/sso/start/{provider or who.provider}", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["location"].startswith("https://provider.example/"))
        cb = urlparse(NEXT["success_url"])
        return client.get(f"{cb.path}?{cb.query}&id=intent1&token=tok1", follow_redirects=False)

    def _in(self, client):
        return client.get("/app", follow_redirects=False).status_code == 200

    def test_unknown_provider_is_refused(self):
        r = TestClient(main.app).get("/auth/sso/start/facebook", follow_redirects=False)
        self.assertIn("sso=feil", r.headers["location"])

    def test_forged_state_is_refused(self):
        c = TestClient(main.app)
        NEXT["who"] = google("g-forged", "forged@example.no")
        c.get("/auth/sso/start/google", follow_redirects=False)
        r = c.get("/auth/sso/callback?state=wrong&id=i&token=t", follow_redirects=False)
        self.assertIn("sso=feil", r.headers["location"])
        self.assertIsNone(store.get_user_by_email("forged@example.no"))

    def test_google_new_account_then_login_by_subject(self):
        c = TestClient(main.app)
        r = self._run(c, google("g-new", "ny@example.no"))
        self.assertEqual(r.headers["location"], "/app")
        self.assertTrue(self._in(c))
        u = store.get_user_by_email("ny@example.no")
        self.assertEqual(store.user_by_identity("idp-google", "g-new")["id"], u["id"])
        # Next login finds the account by subject, even if Google now reports another address.
        c2 = TestClient(main.app)
        self._run(c2, google("g-new", "endret@example.no"))
        self.assertTrue(self._in(c2))
        self.assertIsNone(store.get_user_by_email("endret@example.no"))

    def test_google_attaches_to_existing_verified_account(self):
        tid, uid = store.create_account("Eier AS", "eier@example.no", hash_password("passord-123"))
        store.set_email_verified(uid)
        c = TestClient(main.app)
        self._run(c, google("g-eier", "eier@example.no"))
        self.assertTrue(self._in(c))
        self.assertEqual(store.user_by_identity("idp-google", "g-eier")["id"], uid)
        # The password still works for a verified account.
        c2 = TestClient(main.app)
        c2.post("/login", data={"email": "eier@example.no", "password": "passord-123"})
        self.assertTrue(self._in(c2))

    def test_google_takes_back_a_squatted_unverified_account(self):
        tid, uid = store.create_account("Squat AS", "offer@example.no", hash_password("squatter-pw"))
        key = store.create_api_key(tid, "squatter")
        squatter = TestClient(main.app)
        squatter.post("/login", data={"email": "offer@example.no", "password": "squatter-pw"})
        self.assertTrue(self._in(squatter))

        owner = TestClient(main.app)
        self._run(owner, google("g-offer", "offer@example.no"))
        self.assertTrue(self._in(owner))
        self.assertFalse(self._in(squatter))                     # session ended
        self.assertIsNone(store.resolve_api_key(key["key"]))     # API key revoked
        again = TestClient(main.app)
        again.post("/login", data={"email": "offer@example.no", "password": "squatter-pw"})
        self.assertFalse(self._in(again))                        # password gone

    def test_microsoft_never_logs_in_on_email_alone(self):
        tid, uid = store.create_account("Mål AS", "maal@example.no", hash_password("passord-123"))
        store.set_email_verified(uid)
        attacker = TestClient(main.app)
        r = self._run(attacker, microsoft("ms-evil", "maal@example.no"))
        self.assertEqual(r.status_code, 200)
        self.assertIn("Sjekk e-posten", r.text)
        self.assertFalse(self._in(attacker))
        self.assertIsNone(store.user_by_identity("idp-microsoft", "ms-evil"))
        # The owner gets the mail; clicking it in THEIR browser does nothing.
        self.assertEqual(SENT[-1][0], "maal@example.no")
        link = re.search(r"https?://\S+/auth/sso/bekreft\?n=\S+", SENT[-1][2]).group(0)
        path = urlparse(link).path + "?" + urlparse(link).query
        owner = TestClient(main.app)
        r = owner.get(path, follow_redirects=False)
        self.assertIn("Lenken virker ikke", r.text)
        self.assertIsNone(store.user_by_identity("idp-microsoft", "ms-evil"))

    def test_microsoft_confirmed_in_same_browser(self):
        tid, uid = store.create_account("MS AS", "ms@example.no", hash_password("passord-123"))
        store.set_email_verified(uid)
        c = TestClient(main.app)
        self._run(c, microsoft("ms-real", "ms@example.no"))
        n = parse_qs(urlparse(re.search(r"https?://\S+bekreft\?n=\S+", SENT[-1][2]).group(0)).query)["n"][0]
        r = c.get(f"/auth/sso/bekreft?n={n}", follow_redirects=False)
        self.assertEqual(r.headers["location"], "/app")
        self.assertTrue(self._in(c))
        self.assertEqual(store.user_by_identity("idp-microsoft", "ms-real")["id"], uid)
        # Single use.
        c.post("/logout")
        r = c.get(f"/auth/sso/bekreft?n={n}", follow_redirects=False)
        self.assertIn("Lenken virker ikke", r.text)

    def test_microsoft_new_account_after_confirmation(self):
        c = TestClient(main.app)
        self._run(c, microsoft("ms-new", "helt-ny@example.no"))
        self.assertIsNone(store.get_user_by_email("helt-ny@example.no"))
        n = parse_qs(urlparse(re.search(r"https?://\S+bekreft\?n=\S+", SENT[-1][2]).group(0)).query)["n"][0]
        c.get(f"/auth/sso/bekreft?n={n}", follow_redirects=False)
        self.assertTrue(self._in(c))
        self.assertIsNotNone(store.get_user_by_email("helt-ny@example.no"))

    def test_link_from_account_page_and_refuse_someone_elses(self):
        tid, uid = store.create_account("Kobler AS", "kobler@example.no", hash_password("passord-123"))
        store.set_email_verified(uid)
        c = TestClient(main.app)
        c.post("/login", data={"email": "kobler@example.no", "password": "passord-123"})
        r = self._run(c, google("g-kobler", "annen-adresse@example.no"))
        self.assertIn("sso=koblet", r.headers["location"])
        self.assertEqual(store.user_by_identity("idp-google", "g-kobler")["id"], uid)
        # A login already bound to another user can't be taken over by linking.
        other_tid, other_uid = store.create_account("Annen AS", "annen@example.no", hash_password("passord-456"))
        store.set_email_verified(other_uid)
        o = TestClient(main.app)
        o.post("/login", data={"email": "annen@example.no", "password": "passord-456"})
        r = self._run(o, google("g-kobler", "x@example.no"))
        self.assertIn("sso=opptatt", r.headers["location"])
        self.assertEqual(store.user_by_identity("idp-google", "g-kobler")["id"], uid)

    def test_buttons_render_on_login_and_signup(self):
        c = TestClient(main.app)
        for path in ("/login", "/signup"):
            html = c.get(path).text
            self.assertIn("Fortsett med Google", html)
            self.assertIn("Fortsett med Microsoft", html)
        self.assertIn('href="/auth/sso/start/google?plan=vekst"', c.get("/signup?plan=vekst").text)


if __name__ == "__main__":
    unittest.main()
