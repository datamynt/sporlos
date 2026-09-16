"""Tests for SecurityHeadersMiddleware (baseline headers, 2026-09-16).

Same shape as tests/test_api_sites.py: stdlib unittest, Starlette TestClient,
a minimal app wrapping only the middleware under test rather than the full
main.py route list.

Run:
    .venv/bin/python3 -m unittest tests.test_security_headers -v < /dev/null
"""

from __future__ import annotations

import unittest

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from app.main import SecurityHeadersMiddleware


def _app():
    async def ok(request):
        return PlainTextResponse("ok")

    return Starlette(
        routes=[Route("/", ok)],
        middleware=[Middleware(SecurityHeadersMiddleware)],
    )


class SecurityHeadersTest(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(_app())

    def test_sets_baseline_headers(self):
        resp = self.client.get("/")
        self.assertEqual(resp.headers["Strict-Transport-Security"], "max-age=31536000")
        self.assertEqual(resp.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(resp.headers["X-Frame-Options"], "DENY")
        self.assertEqual(resp.headers["Referrer-Policy"], "strict-origin-when-cross-origin")

    def test_no_includesubdomains_or_preload(self):
        # Deliberately conservative this round — see the middleware docstring.
        hsts = self.client.get("/").headers["Strict-Transport-Security"]
        self.assertNotIn("includeSubDomains", hsts)
        self.assertNotIn("preload", hsts)

    def test_headers_present_on_error_responses_too(self):
        resp = self.client.get("/does-not-exist")
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(resp.headers["X-Frame-Options"], "DENY")


if __name__ == "__main__":
    unittest.main()
