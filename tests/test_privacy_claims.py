"""The public pages and docs describe what Sporløs actually stores.

Every hit is its own event row for 90 days (store.retention_sweep), with a
visitor hash made from IP + user agent + a daily salt that is deleted
(app/privacy.py, store.daily_salt). Visitors with DNT/GPC send nothing
(tracker/sporlos.js). The site itself uses localStorage for UI preferences,
and the chat goes to a Gemini model through our gateway (app/assist.py).
These tests keep the old, untrue phrases from coming back.

Run ONE FILE PER PROCESS:
    .venv/bin/python3 -m pytest -q tests/test_privacy_claims.py < /dev/null
"""

from __future__ import annotations

import inspect
import os
import tempfile
import unittest
from pathlib import Path

_TMP_DB = tempfile.NamedTemporaryFile(prefix="sporlos_test_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ["SPORLOS_DB"] = _TMP_DB.name
os.environ.pop("DATABASE_URL", None)

from starlette.testclient import TestClient  # noqa: E402

from app import assist, main, store  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

PAGES = ["/", "/sporsmal", "/utviklere", "/personvern", "/vilkar", "/google-analytics-alternativ"]

# Phrases that claimed less storage, or more coverage, than the code has.
FALSE_PHRASES = [
    "rådataene ikke finnes",
    "ingen identifikatorer",
    "Kun aggregater",
    "kun aggregater, aldri rådata",
    "aldri rådata",
    "måler alle besøk",
    "måles alle besøk",
    "Alle, faktiske tall",
    "uten persondata",
    "ingen persondata",
    "samler ingen personopplysninger",
    "samler ikke personopplysninger",
    "uten å samle\n  personopplysninger",
    "Analysehendelser inneholder ingen personopplysninger",
    "eneste vi noensinne lagrer",
    "engangs-hash",
    "flyktig hash",
    "vår KI-tjeneste",
    "anonymt, slettes daglig",
]


def setUpModule():
    store.init_db()


def tearDownModule():
    os.unlink(_TMP_DB.name)


class PrivacyClaimsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        client = TestClient(main.app)
        cls.html = {p: client.get(p).text for p in PAGES}

    def test_pages_render(self):
        for path, html in self.html.items():
            self.assertIn("<html", html, path)

    def test_false_phrases_are_gone_from_pages(self):
        for path, html in self.html.items():
            for phrase in FALSE_PHRASES:
                self.assertNotIn(phrase, html, f"{path}: {phrase!r}")

    def test_retention_copy_matches_code(self):
        days = inspect.signature(store.retention_sweep).parameters["days"].default
        self.assertEqual(days, 90)
        for path in ("/personvern", "/sporsmal", "/vilkar"):
            self.assertIn(f"{days} dager", self.html[path], path)
        self.assertIn("bare dagstall", self.html["/personvern"])
        # The per-day breakdowns (store daily_breakdowns) outlive the 90 days: say so,
        # and that they go with the account.
        for path in ("/personvern", "/sporsmal", "/vilkar"):
            self.assertIn("kontoen finnes", self.html[path], path)
            self.assertRegex(self.html[path], r"uten (besøks-hash|besøkskoden|hash)", path)
        self.assertIn("fordelt på side", self.html["/personvern"])

    def test_personvern_states_what_is_stored(self):
        html = self.html["/personvern"]
        self.assertIn("pseudonyme opplysninger", html)
        self.assertIn("salt som byttes og slettes hver natt", html)
        self.assertIn("lagres aldri", html)
        self.assertIn("UTM-kampanjeverdier", html)

    def test_personvern_names_browser_storage(self):
        html = self.html["/personvern"]
        self.assertIn("localStorage", html)
        self.assertIn("eneste cookien vi setter", html)

    def test_personvern_names_chat_model_provider(self):
        # The default model is a Gemini model through our gateway. If it ever
        # changes, the privacy policy has to change with it.
        default = inspect.getsource(assist).split("ASSIST_MODEL", 1)[1].split("\n", 1)[0]
        self.assertIn("gemini", default)
        html = self.html["/personvern"]
        self.assertIn("Google (Gemini)", html)
        self.assertGreaterEqual(html.count("Google (Gemini)"), 2)  # §2 text and §4 processor table

    def test_dnt_and_gpc_exceptions_are_stated(self):
        for path in ("/", "/sporsmal", "/google-analytics-alternativ", "/personvern"):
            self.assertIn("Do Not Track", self.html[path], path)
            self.assertIn("Global Privacy Control", self.html[path], path)

    def test_utviklere_says_api_returns_only_totals(self):
        self.assertIn("aldri enkeltregistreringer", self.html["/utviklere"])

    def test_docs_drop_false_claims(self):
        mcp = (ROOT / "integrations/mcp/README.md").read_text()
        self.assertNotIn("rådataene ikke finnes", mcp)
        onepager = (ROOT / "docs/personvern-onepager.md").read_text()
        self.assertNotIn("samler ingen personopplysninger", onepager)
        self.assertNotIn("uten personopplysninger", onepager)
        self.assertIn("pseudonyme", onepager)
        readme = (ROOT / "README.md").read_text()
        self.assertNotIn("måler **alle** besøk", readme)
        self.assertNotIn("every visit is measured", readme)
        self.assertIn("90 dager", readme)
        wp = (ROOT / "integrations/wordpress/sporlos-analytics/readme.txt").read_text()
        self.assertNotIn("collects no", wp)
        self.assertNotIn("without collecting any personal data", wp)


if __name__ == "__main__":
    unittest.main()
