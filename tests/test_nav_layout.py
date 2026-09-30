"""The header and the page frame must be identical on every page.

Thomas' complaint (2026-09-30): moving between pages made the whole page jump sideways and
the navbar change. Cause: each page family had its own container width, side padding and its
own hand-written <nav>. These tests pin the fix: ONE nav component (two variants) and ONE
frame definition, so a new page cannot quietly bring its own.

Run (one file per process, the DB is chosen at import time):
    .venv/bin/python3 -m pytest -q tests/test_nav_layout.py < /dev/null
"""

from __future__ import annotations

import os
import re
import tempfile
import unittest

_TMP_DB = tempfile.NamedTemporaryFile(prefix="sporlos_navtest_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ["SPORLOS_DB"] = _TMP_DB.name
os.environ.pop("DATABASE_URL", None)

from starlette.testclient import TestClient  # noqa: E402

from app import blogg, store  # noqa: E402
from app import main  # noqa: E402

_NAV_RE = re.compile(r"<nav class=site.*?</nav>", re.S)
_WRAP_RULE = ".wrap{max-width:980px;margin:0 auto;padding:0 1.3rem}"

_PUBLIC_PATHS = [
    "/",
    "/demo",
    "/google-analytics-alternativ",
    "/sporsmal",
    "/integrasjoner",
    "/integrasjoner/wix",
    "/shopify",
    "/utviklere",
    "/blogg",
    "/personvern",
    "/vilkar",
    "/login",
    "/signup",
    "/forgot",
    "/reset?token=x",
]


def _nav(html: str) -> str:
    found = _NAV_RE.findall(html)
    assert len(found) == 1, f"expected exactly one shared nav, found {len(found)}"
    # aria-current marks the page you are on, so it is the one legitimate difference.
    return found[0].replace(" aria-current=page", "")


def setUpModule():
    store.init_db()
    tid = store.create_tenant("Demo AS")
    site = store.create_site(tid, "demo.example.no")
    os.environ["SPORLOS_DEMO_SITE"] = site["public_id"]


class NavLayoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.slug = next(iter(blogg.POSTS))
        cls.paths = _PUBLIC_PATHS + [f"/blogg/{cls.slug}"]
        # One account for the whole class: /signup is throttled per IP (5 per hour).
        cls.member = TestClient(main.app)
        r = cls.member.post(
            "/signup",
            data={"company": "Nav Test AS", "email": "nav@example.no", "password": "passord123"},
            follow_redirects=False,
        )
        assert r.status_code == 302, r.text[:200]

    def setUp(self):
        self.anon = TestClient(main.app)

    def _login(self) -> TestClient:
        return self.member

    # -- one nav ---------------------------------------------------------

    def test_logged_out_nav_is_identical_on_every_public_page(self):
        navs = {}
        for path in self.paths:
            r = self.anon.get(path)
            self.assertEqual(r.status_code, 200, path)
            navs[path] = _nav(r.text)
        self.assertEqual(len(set(navs.values())), 1, {p: n[:80] for p, n in navs.items()})
        nav = next(iter(navs.values()))
        self.assertIn("Prøv gratis", nav)
        self.assertIn('href="/login"', nav)
        self.assertNotIn("/logout", nav)

    def test_logged_in_nav_is_identical_on_app_and_public_pages(self):
        c = self._login()
        paths = ["/app", "/app/seo", "/blogg", "/demo", "/sporsmal", f"/blogg/{self.slug}"]
        navs = {p: _nav(c.get(p).text) for p in paths}
        self.assertEqual(len(set(navs.values())), 1, {p: n[:80] for p, n in navs.items()})
        nav = next(iter(navs.values()))
        self.assertIn("Mine nettsteder", nav)
        self.assertNotIn("Mine sites", nav)
        self.assertIn(main._logout_link(), nav)
        self.assertIn("byttTema()", nav)
        self.assertNotIn("Prøv gratis", nav)

    def test_payment_page_uses_the_shared_nav(self):
        # /betal only renders when a payment method is configured, so pretend Vipps is.
        c = self._login()
        original = main.vipps.configured
        main.vipps.configured = lambda: True
        try:
            r = c.get("/betal?plan=liten")
        finally:
            main.vipps.configured = original
        self.assertEqual(r.status_code, 200)
        self.assertEqual(_nav(r.text), _nav(c.get("/app").text))

    def test_per_site_dashboard_uses_the_shared_nav_too(self):
        c = self._login()
        c.post("/app/sites", data={"domain": "kunde.example.no"}, follow_redirects=False)
        html = c.get("/app").text
        pid = re.search(r"/app\?site=([\w-]+)&", html).group(1)
        page = c.get(f"/app?site={pid}").text
        self.assertEqual(_nav(page), _nav(html))

    def test_logout_control_comes_from_the_single_helper(self):
        self.assertEqual(main._logout_link().count("/logout"), 1)
        c = self._login()
        self.assertEqual(c.get("/app").text.count("/logout"), 1)

    def test_no_page_keeps_a_hand_written_nav(self):
        # Every <nav> on every page must be the shared component.
        c = self._login()
        for client, paths in ((self.anon, self.paths), (c, ["/app", "/app/seo"])):
            for path in paths:
                html = client.get(path).text
                self.assertEqual(html.count("<nav"), 1, path)
                self.assertIn("<nav class=site", html, path)

    # -- one frame -------------------------------------------------------

    def test_every_page_defines_the_same_single_frame(self):
        c = self._login()
        pages = [(self.anon, p) for p in self.paths] + [(c, "/app"), (c, "/app/seo")]
        for client, path in pages:
            html = client.get(path).text
            # Rules that start a line; the footer's tweak is "footer.site .wrap{..}" (not at line start).
            rules = re.findall(r"(?m)^\.wrap\{[^}]*\}", html)
            self.assertEqual(rules, [_WRAP_RULE], path)

    def test_scrollbar_gutter_is_stable_on_every_page(self):
        c = self._login()
        for client, path in [(self.anon, p) for p in self.paths] + [(c, "/app"), (c, "/app/seo")]:
            self.assertIn("scrollbar-gutter:stable", client.get(path).text, path)

    def test_no_bare_nav_rules_left_to_fight_the_shared_header(self):
        c = self._login()
        for client, path in [(self.anon, p) for p in self.paths] + [(c, "/app"), (c, "/app/seo")]:
            html = client.get(path).text
            self.assertNotRegex(html, r"(?<![\w.-])nav\{", path)
            self.assertNotRegex(html, r"(?<![\w.-])nav a\.ut", path)

    # -- errors and caching ----------------------------------------------

    def test_404_page_has_the_shared_header(self):
        r = self.anon.get("/finnes-ikke")
        self.assertEqual(r.status_code, 404)
        self.assertIn("Fant ikke siden", r.text)
        self.assertEqual(_nav(r.text), _nav(self.anon.get("/").text))

    def test_unknown_public_dashboard_is_the_branded_404(self):
        r = self.anon.get("/p/ukjent")
        self.assertEqual(r.status_code, 404)
        self.assertIn("<nav class=site", r.text)

    def test_api_404_stays_plain(self):
        r = self.anon.get("/api/finnes-ikke")
        self.assertEqual(r.status_code, 404)
        self.assertNotIn("<nav", r.text)

    def test_html_varies_on_cookie_because_the_header_depends_on_the_session(self):
        r = self.anon.get("/blogg")
        self.assertIn("cookie", r.headers.get("vary", "").lower())


if __name__ == "__main__":
    unittest.main()
