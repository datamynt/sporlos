"""First contact: the login page points new people to the free trial, a new account
is welcomed and says how it was created, an empty account gets a «Kom i gang» card,
and upgrading shows one card per plan instead of a grid of plan × payment buttons.

Run ONE FILE PER PROCESS:
    .venv/bin/python3 -m pytest -q tests/test_onboarding.py < /dev/null
"""

from __future__ import annotations

import os
import tempfile
import unittest

_TMP_DB = tempfile.NamedTemporaryFile(prefix="sporlos_test_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ["SPORLOS_DB"] = _TMP_DB.name
os.environ.pop("DATABASE_URL", None)
os.environ["DATAMYNT_LOGIN_PAT"] = "test-pat"
os.environ["IDP_GOOGLE_ID"] = "idp-google"
os.environ["IDP_MICROSOFT_ID"] = "idp-microsoft"

from starlette.testclient import TestClient  # noqa: E402

from app import mailer, main, store  # noqa: E402
from app.auth import hash_password  # noqa: E402


def setUpModule():
    store.init_db()
    mailer.send = lambda *a, **k: True


def tearDownModule():
    os.unlink(_TMP_DB.name)


class LoginPageTest(unittest.TestCase):
    def test_new_people_are_pointed_to_the_trial(self):
        html = TestClient(main.app).get("/login").text
        self.assertIn('class=btn-sec href="/signup">Prøv gratis i 30 dager', html)
        self.assertIn('href="/forgot"', html)
        self.assertIn("Første gang med Google eller Microsoft?", html)
        self.assertNotIn("Ny her?", html)


class WelcomeTest(unittest.TestCase):
    def test_password_signup_is_welcomed_with_a_start_card(self):
        c = TestClient(main.app)
        r = c.post("/signup", data={"company": "Ny AS", "email": "ny@example.no",
                                    "password": "passord-123"}, follow_redirects=False)
        self.assertEqual(r.headers["location"], "/app?ny=1")
        html = c.get("/app?ny=1").text
        self.assertIn("Velkommen til Sporløs!", html)
        self.assertIn("Kom i gang på tre minutter", html)
        self.assertIn('action="/app/sites"', html)
        self.assertNotIn("Det fantes ingen konto", html)

    def test_account_with_a_site_gets_the_table_not_the_start_card(self):
        tid, uid = store.create_account("Med AS", "med@example.no", hash_password("passord-123"))
        store.create_site(tid, "med.example.no")
        c = TestClient(main.app)
        c.post("/login", data={"email": "med@example.no", "password": "passord-123"})
        html = c.get("/app").text
        self.assertNotIn("Kom i gang på tre minutter", html)
        self.assertIn("med.example.no", html)


class UpgradeTest(unittest.TestCase):
    def test_one_card_per_plan_payment_method_chosen_later(self):
        tid, uid = store.create_account("Trial AS", "trial@example.no", hash_password("passord-123"))
        c = TestClient(main.app)
        c.post("/login", data={"email": "trial@example.no", "password": "passord-123"})
        html = c.get("/app").text
        for plan in ("liten", "vekst", "pro"):
            self.assertEqual(html.count(f'href="/betal?plan={plan}"'), 1)
        self.assertIn("123,75 kr inkl. mva", html)
        self.assertNotIn("/billing/checkout?plan=", html)
        self.assertNotIn("/billing/vipps/start?plan=", html)
        self.assertNotIn("Faktura/EHF for byrå", html)


if __name__ == "__main__":
    unittest.main()
