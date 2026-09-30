"""Databehandleravtale: published page, accepted at signup (password and SSO),
accepted later from the account page.

Run ONE FILE PER PROCESS:
    .venv/bin/python3 -m pytest -q tests/test_dpa.py < /dev/null
"""

from __future__ import annotations

import os
import tempfile
import unittest

_TMP_DB = tempfile.NamedTemporaryFile(prefix="sporlos_test_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ["SPORLOS_DB"] = _TMP_DB.name
os.environ.pop("DATABASE_URL", None)

from starlette.testclient import TestClient  # noqa: E402

from app import dpa, innlogg, main, store  # noqa: E402
from app.auth import hash_password  # noqa: E402


def setUpModule():
    store.init_db()


def tearDownModule():
    os.unlink(_TMP_DB.name)


class DpaTest(unittest.TestCase):
    def test_page_is_published_with_its_version(self):
        r = TestClient(main.app).get("/databehandleravtale")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Databehandleravtale", r.text)
        self.assertIn(f"versjon {dpa.VERSION}", r.text)
        self.assertNotIn("utkast", r.text.lower())
        self.assertIn('href="/databehandleravtale"', TestClient(main.app).get("/").text)
        self.assertIn("/databehandleravtale", TestClient(main.app).get("/sitemap.xml").text)

    def test_signup_records_acceptance(self):
        c = TestClient(main.app)
        page = c.get("/signup").text
        self.assertIn("godtar du", page)
        c.post("/signup", data={"company": "Signup AS", "email": "signup@example.no",
                                "password": "passord-123"})
        u = store.get_user_by_email("signup@example.no")
        st = store.dpa_status(u["tenant_id"])
        self.assertEqual(st["dpa_version"], dpa.VERSION)
        self.assertTrue(st["dpa_accepted_at"])

    def test_sso_account_records_acceptance(self):
        who = innlogg.IdpLogin("google", "idp-google", "g-dpa", "sso-dpa@example.no", True, "Sso AS")
        tid, uid = main._sso_new_account(who)
        self.assertEqual(store.dpa_status(tid)["dpa_version"], dpa.VERSION)

    def test_existing_account_accepts_from_account_page(self):
        tid, uid = store.create_account("Gammel AS", "gammel@example.no", hash_password("passord-123"))
        self.assertIsNone(store.dpa_status(tid)["dpa_version"])
        c = TestClient(main.app)
        c.post("/login", data={"email": "gammel@example.no", "password": "passord-123"})
        self.assertIn(f"Godta versjon {dpa.VERSION}", c.get("/app").text)
        r = c.post("/app/dpa/accept", follow_redirects=False)
        self.assertIn("dpa=ok", r.headers["location"])
        self.assertEqual(store.dpa_status(tid)["dpa_version"], dpa.VERSION)
        self.assertIn("Godtatt", c.get("/app").text)

    def test_accept_requires_login(self):
        r = TestClient(main.app).post("/app/dpa/accept", follow_redirects=False)
        self.assertIn("/login", r.headers["location"])


if __name__ == "__main__":
    unittest.main()
