"""Tests for the AI-assistant visit counters (store.ai_referrals & friends).

ChatGPT & co. tag outbound links with utm_source and often send no referrer, so
the counters must match on referrer host OR utm_source — otherwise those visits
only show up under Campaigns.

Same shape as tests/test_api_sites.py: stdlib unittest, real SQLite backend.

Run:
    .venv/bin/python3 -m unittest tests.test_ai_referrals -v < /dev/null
"""

from __future__ import annotations

import os
import tempfile
import unittest

# Backend is selected at import time from these env vars, so set them BEFORE
# `app.store` is imported.
os.environ.pop("DATABASE_URL", None)

from app import store  # noqa: E402


class AiReferralsTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._prev_db = store._DB_PATH
        store._DB_PATH = os.path.join(self._tmpdir.name, "test.db")
        store.init_db()
        self.tenant_id = store.create_tenant("Bygger")
        self.site_id = store.create_site(self.tenant_id, "a.no")["id"]

    def tearDown(self):
        store._DB_PATH = self._prev_db  # ikke lek pekeren til andre testfiler
        self._tmpdir.cleanup()

    def _view(self, visitor, referrer=None, utm=None, name="pageview"):
        store.insert_event(self.site_id, {
            "name": name, "path": "/", "visitor_hash": visitor,
            "referrer_src": referrer, "utm_source": utm,
        })

    def test_referrer_only_is_counted(self):
        self._view("v1", referrer="www.perplexity.ai")
        self.assertEqual(store.ai_referrals(self.site_id), {"visitors": 1, "views": 1})

    def test_utm_source_without_referrer_is_counted(self):
        # The regression: this was 0 and the visit appeared only under Campaigns.
        self._view("v1", utm="chatgpt.com")
        self.assertEqual(store.ai_referrals(self.site_id), {"visitors": 1, "views": 1})

    def test_utm_source_short_forms_and_case(self):
        for i, utm in enumerate(("ChatGPT.com", "perplexity", "copilot.com", "www.claude.ai")):
            self._view(f"v{i}", utm=utm)
        self.assertEqual(store.ai_referrals(self.site_id)["visitors"], 4)

    def test_ordinary_campaigns_and_referrers_are_not_counted(self):
        self._view("v1", utm="nyhetsbrev")
        self._view("v2", referrer="google.com")
        self._view("v3", utm="chatgpt-tips")  # contains the word, is not the source
        self._view("v4")
        self.assertEqual(store.ai_referrals(self.site_id), {"visitors": 0, "views": 0})

    def test_only_pageviews_count(self):
        self._view("v1", utm="chatgpt.com", name="signup")
        self.assertEqual(store.ai_referrals(self.site_id)["visitors"], 0)

    def test_sources_fold_referrer_and_utm_spellings_into_one_row(self):
        self._view("v1", utm="chatgpt.com")
        self._view("v1", referrer="chatgpt.com", utm="chatgpt.com")  # same visitor, both signals
        self._view("v2", referrer="chat.openai.com")
        self._view("v3", utm="ChatGPT")
        self._view("v4", referrer="www.perplexity.ai")
        rows = store.ai_referral_sources(self.site_id)
        self.assertEqual(rows[0], {"k": "chatgpt.com", "u": 2, "n": 3})
        by_key = {r["k"]: r for r in rows}
        self.assertEqual(by_key["chat.openai.com"]["u"], 1)
        self.assertEqual(by_key["perplexity.ai"], {"k": "perplexity.ai", "u": 1, "n": 1})

    def test_sources_respect_limit(self):
        self._view("v1", utm="chatgpt.com")
        self._view("v2", utm="perplexity")
        self.assertEqual(len(store.ai_referral_sources(self.site_id, limit=1)), 1)

    def test_fleet_overview_counts_utm_tagged_visits(self):
        self._view("v1", utm="chatgpt.com")
        self._view("v2", referrer="claude.ai")
        row = next(s for s in store.seo_overview(self.tenant_id) if s["id"] == self.site_id)
        self.assertEqual(row["ai"], 2)


if __name__ == "__main__":
    unittest.main()
