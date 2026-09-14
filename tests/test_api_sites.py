"""Tests for POST /api/v1/sites (opprett-eller-hent nettsted under API-nøkkel).

Same shape as tests/test_forgot_throttle.py: stdlib unittest, real SQLite
backend (store.init_db()), no new dependencies. Requests go through Starlette's
TestClient so routing/auth/JSON-shape is exercised end to end, not just the
handler function.

Run:
    .venv/bin/python3 -m unittest tests.test_api_sites -v < /dev/null
"""

from __future__ import annotations

import os
import tempfile
import unittest

# Backend is selected at import time from these env vars, so set them BEFORE
# `app.store` (and therefore app.main) is imported.
os.environ.pop("DATABASE_URL", None)

from app import api, store  # noqa: E402

try:
    from starlette.testclient import TestClient
except ImportError:  # pragma: no cover - httpx is in requirements.txt
    TestClient = None

from starlette.applications import Starlette  # noqa: E402
from starlette.routing import Route  # noqa: E402


def _app():
    """Minimal app med kun det under test — main.py's fulle rute-liste drar inn
    hele dashbordet (og lastet tid) for ingenting."""
    return Starlette(
        routes=[
            Route("/api/v1/sites", api.sites),
            Route("/api/v1/sites", api.create_site, methods=["POST"]),
        ]
    )


@unittest.skipIf(TestClient is None, "starlette TestClient requires httpx")
class CreateSiteApiTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._prev_db = store._DB_PATH
        store._DB_PATH = os.path.join(self._tmpdir.name, "test.db")
        store.init_db()
        self.tenant_id = store.create_tenant("Bygger")
        self.key = store.create_api_key(self.tenant_id, "test")["key"]
        self.client = TestClient(_app())
        self.h = {"Authorization": f"Bearer {self.key}"}

    def tearDown(self):
        store._DB_PATH = self._prev_db  # ikke lek pekeren til andre testfiler
        self._tmpdir.cleanup()

    def _post(self, payload, headers=None):
        return self.client.post("/api/v1/sites", json=payload, headers=headers or self.h)

    # --- auth -------------------------------------------------------------

    def test_missing_key_is_401(self):
        r = self.client.post("/api/v1/sites", json={"domain": "a.no"})
        self.assertEqual(r.status_code, 401)
        self.assertIn("error", r.json())

    def test_bad_key_is_401(self):
        r = self._post({"domain": "a.no"}, headers={"Authorization": "Bearer sl_nope"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json(), {"error": "ugyldig eller manglende API-nøkkel"})

    def test_non_bearer_header_is_401(self):
        r = self._post({"domain": "a.no"}, headers={"Authorization": "Basic abc"})
        self.assertEqual(r.status_code, 401)

    # --- body validation --------------------------------------------------

    def test_missing_domain_is_400(self):
        self.assertEqual(self._post({}).status_code, 400)

    def test_empty_and_blank_domain_are_400(self):
        for bad in ("", "   ", "/", "http://"):
            with self.subTest(bad=bad):
                self.assertEqual(self._post({"domain": bad}).status_code, 400)

    def test_domain_over_253_chars_is_400(self):
        r = self._post({"domain": "a" * 254 + ".no"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("error", r.json())

    def test_domain_at_253_chars_is_accepted(self):
        # Grensen skal være «> 253», ikke «>= 253».
        self.assertEqual(self._post({"domain": "a" * 249 + ".com"}).status_code, 201)

    def test_malformed_json_is_400(self):
        r = self.client.post(
            "/api/v1/sites", content=b"{not json", headers={**self.h, "content-type": "application/json"}
        )
        self.assertEqual(r.status_code, 400)
        self.assertIn("error", r.json())

    def test_json_array_body_is_400(self):
        self.assertEqual(self._post(["a.no"]).status_code, 400)

    def test_non_string_domain_is_400(self):
        self.assertEqual(self._post({"domain": 123}).status_code, 400)

    def test_error_body_uses_err_shape(self):
        r = self._post({})
        self.assertEqual(list(r.json().keys()), ["error"])

    # --- create / idempotency --------------------------------------------

    def test_first_create_is_201_with_created_true(self):
        r = self._post({"domain": "kunde.sidebygger.no/s/første"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(
            r.json(),
            {"public_id": r.json()["public_id"], "domain": "kunde.sidebygger.no/s/første", "created": True},
        )

    def test_repeat_is_200_same_public_id(self):
        first = self._post({"domain": "kunde.sidebygger.no/s/andre"})
        second = self._post({"domain": "kunde.sidebygger.no/s/andre"})
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertFalse(second.json()["created"])
        self.assertEqual(first.json()["public_id"], second.json()["public_id"])

    def test_repeat_does_not_create_a_second_row(self):
        self._post({"domain": "en.no"})
        self._post({"domain": "en.no"})
        self.assertEqual(len(store.list_sites(self.tenant_id)), 1)

    def test_public_id_is_the_tracker_id(self):
        # Samme id som snippeten bruker = den resolve_site slår opp på.
        pid = self._post({"domain": "tracker.no"}).json()["public_id"]
        site = store.resolve_site(pid)
        self.assertIsNotNone(site)
        self.assertEqual(site["tenant_id"], self.tenant_id)

    def test_domain_is_normalised(self):
        r = self._post({"domain": "  HTTPS://WWW.Eksempel.NO/Sti/  "})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["domain"], "eksempel.no/sti")
        # Idempotensen må treffe samme rad uansett hvordan domenet skrives.
        again = self._post({"domain": "eksempel.no/sti"})
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["public_id"], r.json()["public_id"])

    def test_hostname_only_still_works(self):
        r = self._post({"domain": "bare.vert.no"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["domain"], "bare.vert.no")

    def test_other_tenants_same_domain_is_a_separate_site(self):
        # Unikheten er (tenant_id, domain) — to kontoer kan måle samme domene.
        other = store.create_tenant("Annen")
        other_key = store.create_api_key(other, "test")["key"]
        mine = self._post({"domain": "delt.no"})
        theirs = self._post({"domain": "delt.no"}, headers={"Authorization": f"Bearer {other_key}"})
        self.assertEqual((mine.status_code, theirs.status_code), (201, 201))
        self.assertNotEqual(mine.json()["public_id"], theirs.json()["public_id"])

    def test_create_then_list_shows_the_site(self):
        self._post({"domain": "listet.no"})
        r = self.client.get("/api/v1/sites", headers=self.h)
        self.assertIn("listet.no", [s["domain"] for s in r.json()["sites"]])

    # --- plan limit -------------------------------------------------------

    def test_plan_limit_is_403_with_dashboard_wording(self):
        # «liten» har plass til 1 nettsted (store.PLAN_LIMITS).
        store.set_tenant_plan(self.tenant_id, "liten")
        self.assertEqual(self._post({"domain": "en.no"}).status_code, 201)
        r = self._post({"domain": "to.no"})
        self.assertEqual(r.status_code, 403)
        self.assertEqual(
            r.json(),
            {"error": "Planen din har plass til 1 nettsted — oppgrader for å legge til flere."},
        )

    def test_plan_limit_plural_wording(self):
        store.set_tenant_plan(self.tenant_id, "pro")  # 15 nettsteder
        for n in range(15):
            self.assertEqual(self._post({"domain": f"s{n}.no"}).status_code, 201)
        r = self._post({"domain": "over.no"})
        self.assertEqual(r.status_code, 403)
        self.assertIn("15 nettsteder", r.json()["error"])

    def test_existing_site_is_returned_even_when_at_limit(self):
        # Idempotent kall skal lykkes selv om grensen er nådd — ellers ville en
        # bygger som publiserer på nytt få 403 på en side som allerede finnes.
        store.set_tenant_plan(self.tenant_id, "liten")
        first = self._post({"domain": "en.no"})
        second = self._post({"domain": "en.no"})
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["public_id"], first.json()["public_id"])

    def test_unlimited_plan_has_no_limit(self):
        store.set_tenant_plan(self.tenant_id, "byra")  # (None, None)
        for n in range(12):
            self.assertEqual(self._post({"domain": f"u{n}.no"}).status_code, 201)


class NormalizeDomainTest(unittest.TestCase):
    """Normaliseringen deles med dashbordets /app/sites — lås reglene her."""

    def test_strips_scheme_lowercases_strips_trailing_slash(self):
        self.assertEqual(store.normalize_domain("https://Eksempel.NO/"), "eksempel.no")

    def test_drops_www(self):
        self.assertEqual(store.normalize_domain("www.eksempel.no"), "eksempel.no")

    def test_keeps_path_for_builder_style_hosts(self):
        self.assertEqual(store.normalize_domain("sidebygger.no/s/Kunde-1"), "sidebygger.no/s/kunde-1")

    def test_drops_query_and_fragment_boundary(self):
        self.assertEqual(store.normalize_domain("a.no/x?y=1"), "a.no/x")

    def test_rejects_empty(self):
        self.assertEqual(store.normalize_domain(""), "")
        self.assertEqual(store.normalize_domain("   "), "")
        self.assertEqual(store.normalize_domain(None), "")
        self.assertEqual(store.normalize_domain(123), "")

    def test_253_boundary(self):
        self.assertEqual(len(store.normalize_domain("a" * 253)), 253)
        self.assertEqual(store.normalize_domain("a" * 254), "")

    def test_dashboard_split_gives_hostname_only(self):
        # main.create_site_post gjør .split("/")[0] på resultatet — regresjonslås
        # på at dashbordet fortsatt registrerer kun verten.
        self.assertEqual(store.normalize_domain("https://www.a.no/sti/").split("/")[0], "a.no")


if __name__ == "__main__":
    unittest.main()
