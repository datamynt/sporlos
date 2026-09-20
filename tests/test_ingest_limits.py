"""Stdlib unittest for POST /api/event limits and for keeping a site's own host
out of its traffic sources. SQLite backend, same setup as the other suites.

Run with:
    .venv/bin/python3 -m unittest tests.test_ingest_limits -v < /dev/null
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

_TMP_DB = tempfile.NamedTemporaryFile(prefix="sporlos_test_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ.setdefault("SPORLOS_DB", _TMP_DB.name)
os.environ.pop("DATABASE_URL", None)

from starlette.testclient import TestClient  # noqa: E402

from app import main, store  # noqa: E402

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"


class IngestTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store.init_db()
        cls.client = TestClient(main.app)

    def setUp(self):
        tenant = store.create_tenant("Ingest test")
        self.site = store.create_site(tenant, f"shop-{os.urandom(4).hex()}.example")
        store._site_cache.clear()
        main._rate_counts.clear()

    def post(self, body, ip="84.208.10.20", raw: bytes | None = None):
        data = raw if raw is not None else json.dumps(body).encode()
        return self.client.post(
            "/api/event", content=data, headers={"user-agent": UA, "x-forwarded-for": ip}
        )

    def rows(self):
        with store._cursor() as cur:
            cur.execute(
                "SELECT name, path, referrer_src FROM events WHERE site_id = ? ORDER BY id",
                (self.site["id"],),
            )
            return [dict(r) for r in cur.fetchall()]


class IngestLimitsTest(IngestTestCase):
    def test_normal_pageview_is_stored(self):
        r = self.post({"s": self.site["public_id"], "n": "pageview", "p": "/priser",
                       "r": "https://www.google.com/search?q=hemmelig"})
        self.assertEqual(r.status_code, 204)
        self.assertEqual(self.rows(), [{"name": "pageview", "path": "/priser",
                                        "referrer_src": "www.google.com"}])

    def test_non_object_json_is_a_400_not_a_500(self):
        for raw in (b"[1,2,3]", b'"text"', b"42", b"null", b"{not json"):
            with self.subTest(raw=raw):
                self.assertEqual(self.post(None, raw=raw).status_code, 400)
        self.assertEqual(self.rows(), [])

    def test_oversized_body_is_rejected(self):
        r = self.post({"s": self.site["public_id"], "p": "/" + "x" * 20_000})
        self.assertEqual(r.status_code, 413)
        self.assertEqual(self.rows(), [])

    def test_query_string_and_fragment_never_reach_the_database(self):
        sid = self.site["public_id"]
        self.post({"s": sid, "p": "/takk?email=ola@example.no&token=abc#del"})
        self.post({"s": sid, "p": "https://shop.example/konto/reset?token=abc"})
        self.post({"s": sid, "p": "ikke-en-sti"})
        self.post({"s": sid, "p": {"nested": "object"}})
        self.post({"s": sid, "p": "/" + "a" * 900})
        paths = [r["path"] for r in self.rows()]
        self.assertEqual(paths[:4], ["/takk", "/konto/reset", "/", "/"])
        self.assertEqual(len(paths[4]), 512)
        self.assertNotIn("ola@example.no", json.dumps(paths))

    def test_event_name_is_a_short_string_or_a_pageview(self):
        sid = self.site["public_id"]
        self.post({"s": sid, "n": {"x": 1}, "p": "/"})
        self.post({"s": sid, "n": "  ", "p": "/"})
        self.post({"s": sid, "n": "k" * 500, "p": "/"})
        names = [r["name"] for r in self.rows()]
        self.assertEqual(names[:2], ["pageview", "pageview"])
        self.assertEqual(len(names[2]), 64)

    def test_referrer_keeps_only_the_host(self):
        self.post({"s": self.site["public_id"], "p": "/",
                   "r": "https://ola:passord@Partner.Example:8443/side?x=1"})
        self.assertEqual(self.rows()[0]["referrer_src"], "partner.example")

    def test_site_id_must_be_a_string(self):
        self.assertEqual(self.post({"s": ["a"], "p": "/"}).status_code, 404)
        self.assertEqual(self.post({"s": "x" * 500, "p": "/"}).status_code, 404)

    def test_one_visitor_cannot_burn_the_quota(self):
        sid = self.site["public_id"]
        for _ in range(main._MAX_EVENTS_PER_MINUTE + 30):
            self.assertEqual(self.post({"s": sid, "p": "/loop"}).status_code, 204)
        self.assertEqual(len(self.rows()), main._MAX_EVENTS_PER_MINUTE)
        # Another visitor is not affected.
        self.post({"s": sid, "p": "/annen"}, ip="84.208.99.99")
        self.assertEqual(len(self.rows()), main._MAX_EVENTS_PER_MINUTE + 1)


class SelfReferralTest(IngestTestCase):
    def test_own_host_is_not_a_traffic_source(self):
        sid, domain = self.site["public_id"], self.site["domain"]
        self.post({"s": sid, "p": "/", "r": "https://www.google.com/"})
        self.post({"s": sid, "p": "/priser", "r": f"https://{domain}/"})
        self.post({"s": sid, "p": "/kasse", "r": f"https://www.{domain}/priser"})
        self.post({"s": sid, "p": "/", "r": None}, ip="84.208.1.1")

        s = store.stats(self.site["id"], 1)
        self.assertEqual(s["pageviews"], 4)  # totals are never filtered
        self.assertEqual(
            {r["src"]: r["n"] for r in s["top_sources"]}, {"www.google.com": 1, "direkte": 1}
        )
        api = {r["value"]: r["pageviews"] for r in store.api_breakdown(self.site["id"], 1, "sources")}
        self.assertEqual(api, {"www.google.com": 1, "direkte": 1})
        csv = {r["k"]: r["n"] for r in store.export_breakdown(self.site["id"], 1, "kilder")}
        self.assertEqual(csv, {"www.google.com": 1, "direkte": 1})
        # Other breakdowns still see every pageview.
        pages = sum(r["pageviews"] for r in store.api_breakdown(self.site["id"], 1, "pages"))
        self.assertEqual(pages, 4)


class CsvExportTest(unittest.TestCase):
    def test_formula_cells_are_neutralised(self):
        for bad in ("=HYPERLINK(\"http://x\")", "+1+1", "-2+3", "@SUM(A1)", "\tx"):
            self.assertEqual(main._csv_cell(bad), "'" + bad)
        for ok in ("/priser", "www.google.com", "direkte", 42, None):
            self.assertEqual(main._csv_cell(ok), ok)


if __name__ == "__main__":
    unittest.main()
