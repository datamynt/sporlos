"""The «Betal med Vipps» button on /betal is Vipps' own approved file (brand guidelines).

Vipps: "Use only the approved buttons, and don't change their design". We serve their file
byte-for-byte from /static/vipps/ and render it as <img> at natural size (250 x 44) inside
a bare link. Nothing is loaded from Vipps' servers.
Run ONE FILE PER PROCESS:
    .venv/bin/python3 -m pytest -q tests/test_vipps_official_button.py < /dev/null
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import unittest
from pathlib import Path

_TMP_DB = tempfile.NamedTemporaryFile(prefix="sporlos_test_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ["SPORLOS_DB"] = _TMP_DB.name
os.environ.pop("DATABASE_URL", None)

from starlette.testclient import TestClient  # noqa: E402

from app import main, store, vipps  # noqa: E402
from app.auth import hash_password  # noqa: E402

BUTTON = Path(__file__).resolve().parent.parent / "static" / "vipps" / "vipps-betal-med-no.svg"
# sha256 of the file as published by Vipps (static/vipps/README.md has the source).
OFFICIAL_SHA256 = "d553aed7645c628fc5075e960921855e854ed23a413cc91c065bbab0b3765762"


def setUpModule():
    store.init_db()
    vipps.configured = lambda: True


def tearDownModule():
    os.unlink(_TMP_DB.name)


class OfficialVippsButtonTest(unittest.TestCase):
    def test_vendored_file_is_vipps_own_bytes(self):
        self.assertEqual(hashlib.sha256(BUTTON.read_bytes()).hexdigest(), OFFICIAL_SHA256)

    def test_betal_page_uses_the_approved_button(self):
        _tid, uid = store.create_account("Vipps AS", "knapp@example.no", hash_password("passord-123"))
        store.set_email_verified(uid)
        c = TestClient(main.app)
        c.post("/login", data={"email": "knapp@example.no", "password": "passord-123"})
        r = c.get("/betal?plan=vekst")
        self.assertEqual(r.status_code, 200)
        m = re.search(r'<a href="/billing/vipps/start\?plan=vekst"[^>]*>(.*?)</a>', r.text, re.S)
        self.assertTrue(m, "Vipps link missing")
        self.assertIn('src="/static/vipps/vipps-betal-med-no.svg"', m.group(1))
        self.assertIn('width="250" height="44"', m.group(1))
        self.assertIn('alt="Betal med Vipps"', m.group(1))
        # no background/border/colour of our own on the button
        self.assertNotIn("background", m.group(0))
        self.assertIsNone(re.search(r"""(?:src|href)=["']https?://[^"']*(?:vippsmobilepay\.com|vipps\.no)""", r.text))
        self.assertEqual(c.get("/static/vipps/vipps-betal-med-no.svg").status_code, 200)


if __name__ == "__main__":
    unittest.main()
