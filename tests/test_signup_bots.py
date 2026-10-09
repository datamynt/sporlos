"""Signup bot guard: a filled honeypot, a missing or forged form stamp, a form sent
back within seconds, or the «Test Company» placeholder all get the fake «check your
e-mail» page — no account is created and no verification mail is sent.

Run ONE FILE PER PROCESS:
    .venv/bin/python3 -m pytest -q tests/test_signup_bots.py < /dev/null
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest

_TMP_DB = tempfile.NamedTemporaryFile(prefix="sporlos_test_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ["SPORLOS_DB"] = _TMP_DB.name
os.environ.pop("DATABASE_URL", None)

from starlette.testclient import TestClient  # noqa: E402

from app import main, notify, store  # noqa: E402

SENT: list[str] = []


def setUpModule():
    store.init_db()
    notify.send_verification = lambda uid, email: SENT.append(email) or True


def tearDownModule():
    os.unlink(_TMP_DB.name)


def _form(email: str, **over) -> dict:
    data = {"company": "Ekte AS", "email": email, "password": "passord-123",
            "t": main._signup_stamp(time.time() - 10)}
    data.update(over)
    return data


class SignupBotTest(unittest.TestCase):
    def setUp(self):
        SENT.clear()

    def _assert_stopped(self, email: str, **over):
        r = TestClient(main.app).post("/signup", data=_form(email, **over), follow_redirects=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Sjekk e-posten din", r.text)
        self.assertIsNone(store.get_user_by_email(email))
        self.assertEqual(SENT, [])

    def test_form_carries_a_stamp(self):
        html = TestClient(main.app).get("/signup").text
        self.assertIn('<input type=hidden name=t value="', html)

    def test_honeypot(self):
        self._assert_stopped("hp@example.no", website="https://spam.example")

    def test_missing_stamp(self):
        self._assert_stopped("nostamp@example.no", t="")

    def test_forged_stamp(self):
        self._assert_stopped("forged@example.no", t=f"{int(time.time()) - 10}.deadbeefdeadbeefdead")

    def test_too_fast(self):
        self._assert_stopped("fast@example.no", t=main._signup_stamp())

    def test_placeholder_company(self):
        self._assert_stopped("tc@example.no", company="Test Company")

    def test_human_gets_through(self):
        r = TestClient(main.app).post("/signup", data=_form("human@example.no"), follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertIsNotNone(store.get_user_by_email("human@example.no"))
        self.assertEqual(SENT, ["human@example.no"])


if __name__ == "__main__":
    unittest.main()
