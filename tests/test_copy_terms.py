"""One term per concept, one trial CTA, and «verifiserbare tall» in every plan.

The glossary (fluktfrekvens, personopplysninger, samtykkefri, trakt, sporingskode,
nettsted-ID, demo, KI) is applied on every public page, and anchoring is not sold as a
Pro feature: anchor.py seals every site's daily totals regardless of plan. These tests
keep the old words and the «(Pro)» tag from coming back.

Run ONE FILE PER PROCESS:
    .venv/bin/python3 -m pytest -q tests/test_copy_terms.py < /dev/null
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest
from unittest import mock

_TMP_DB = tempfile.NamedTemporaryFile(prefix="sporlos_test_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ["SPORLOS_DB"] = _TMP_DB.name
os.environ.pop("DATABASE_URL", None)

from starlette.testclient import TestClient  # noqa: E402

from app import blogg, main, pricing, store  # noqa: E402

PAGES = [
    "/", "/sporsmal", "/utviklere", "/personvern", "/vilkar", "/google-analytics-alternativ",
    "/integrasjoner", "/shopify", "/signup", "/login", "/blogg", "/demo", "/llms.txt",
    *[f"/integrasjoner/{slug}" for slug in main._GUIDES],
    *[f"/blogg/{slug}" for slug in blogg.POSTS],
]

# Words the glossary replaced, as they appear in visible text.
OLD_TERMS = [
    "fluktrate",
    "persondata",
    "samtykke-fri",
    "Live demo",
    "live demo",
    "live-demo",
    "Funnels",
    "funnels med",
    "drop-off",
    "sporings-kode",
    "sporings-snippet",
    "site-ID",
    "siten",
    "Sporingsscript",
    "sporingsscript",
    "lim-inn-guide",
    "Lim-inn-guide",
    "toppprodukter",
    "self-host",
    " (Pro)",  # «Ghost(Pro)» is Ghost's product name and stays
    "Ja (Pro)",
    "levende tall",
    "andelssøyler",
    "visn. per besøk",
]

OLD_CTAS = ["Start gratis prøve", "Prøv Sporløs gratis", "30 dager gratis prøve", ">Kom i gang</a>"]


def _visible(html: str) -> str:
    """Page text without <style>/<script> blocks (CSS comments and JS are not copy)."""
    return re.sub(r"<(style|script)\b.*?</\1>", "", html, flags=re.S)


def setUpModule():
    store.init_db()


def tearDownModule():
    os.unlink(_TMP_DB.name)


class CopyTermsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        tid = store.create_tenant("Demo AS")
        cls.demo_site = store.create_site(tid, "sporlos.example")
        store.insert_event(cls.demo_site["id"], {"name": "pageview", "path": "/", "visitor_hash": "v1"})
        cls.client = TestClient(main.app)
        with mock.patch.dict(os.environ, {"SPORLOS_DEMO_SITE": cls.demo_site["public_id"]}):
            cls.html = {p: cls.client.get(p).text for p in PAGES}

    def test_pages_render(self):
        for path, html in self.html.items():
            with self.subTest(path=path):
                self.assertTrue(html.strip(), path)

    def test_old_terms_are_gone(self):
        for path, html in self.html.items():
            text = _visible(html)
            for term in OLD_TERMS:
                with self.subTest(path=path, term=term):
                    self.assertNotIn(term, text)

    def test_one_trial_cta(self):
        for path, html in self.html.items():
            text = _visible(html)
            for cta in OLD_CTAS:
                with self.subTest(path=path, cta=cta):
                    self.assertNotIn(cta, text)
        for path in ("/", "/sporsmal", "/google-analytics-alternativ", "/login", "/demo"):
            with self.subTest(path=path):
                self.assertIn("Prøv gratis i 30 dager", self.html[path])

    def test_plan_cards_say_choose_plan(self):
        for plan in ("Liten", "Vekst", "Pro"):
            self.assertIn(f">Velg {plan}</a>", self.html["/"])

    def test_anchoring_is_in_every_plan(self):
        home = self.html["/"]
        pro_card = home.split("<b>Pro</b>")[1].split("</div>")[0]
        self.assertNotIn("verifiserbare tall", pro_card)
        self.assertIn("Alle planer har alle funksjoner, også forseglede, verifiserbare tall.", home)
        self.assertIn("Med i alle planer.", home)  # the feature card
        self.assertIn("Forseglingen er med i alle planer.", self.html["/sporsmal"])
        self.assertIn("Ja, i alle planer", self.html["/google-analytics-alternativ"])
        self.assertIn("i alle planer", self.html["/llms.txt"])
        self.assertNotIn("premium", self.html["/llms.txt"])

    def test_byra_card_shows_price_incl_vat_and_one_site_threshold(self):
        home = self.html["/"]
        byra = home.split("<b>Byrå</b>")[1].split("</div>")[0]
        self.assertIn(f"fra {pricing.kr(pricing.BYRA_FROM_ORE)} kr", byra)
        self.assertIn(f"{pricing.kr(pricing.incl(pricing.BYRA_FROM_ORE))} kr inkl. mva", byra)
        self.assertEqual(pricing.kr(pricing.incl(pricing.BYRA_FROM_ORE)), "1 862,50")
        self.assertIn("fra 25 nettsteder", byra)
        self.assertIn("pris etter avtale", byra)
        for path, html in self.html.items():
            with self.subTest(path=path):
                self.assertNotIn("over 25 nettsteder", html)
                self.assertNotIn("kundenettsteder", html)

    def test_app_plan_picker_matches_the_public_page(self):
        from app.auth import hash_password
        store.create_account("Prøve AS", "prove-terms@example.no", hash_password("passord-123"))
        c = TestClient(main.app)
        c.post("/login", data={"email": "prove-terms@example.no", "password": "passord-123"})
        html = c.get("/app").text
        self.assertIn("Alle planer har alle funksjoner, også forseglede", html)
        self.assertIn("Byrå, fra 25 nettsteder, har pris etter avtale", html)
        self.assertNotIn("over 25 nettsteder", html)

    def test_signup_states_trial_limits(self):
        self.assertIn("Prøveperioden gir opptil 100 000 sidevisninger og 5 nettsteder.", self.html["/signup"])
        self.assertIn("<label>Firmanavn</label>", self.html["/signup"])

    def test_number_helpers(self):
        self.assertEqual(main._fmt_pct(74), "74 %")
        self.assertEqual(main._fmt_pct(0.0, 1), "0,0 %")
        self.assertEqual(main._fmt_pct(12.5, 1), "12,5 %")
        self.assertEqual(main._fmt_dec(1.8), "1,8")
        self.assertEqual(main._fmt_n(12345), "12 345")


if __name__ == "__main__":
    unittest.main()
