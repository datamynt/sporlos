"""Stdlib unittest: every dropped ingest event leaves one log line with the reason
and coarse categories, and never the IP, the UA string or the visitor hash.

Run with:
    .venv/bin/python3 -m unittest tests.test_drop_log -v
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from tests.test_ingest_limits import UA, IngestTestCase

from app import main
from app.datacenter import datacenter_asn
from app.useragent import bot_match

IP = "84.208.10.20"
BOT_UA = "curl/8.5.0 (secret-build-9f3a)"


class DropLogTest(IngestTestCase):
    def setUp(self):
        super().setUp()
        with main._drop_lock:
            main._drop_counts.clear()

    def send(self, ua=UA, ip=IP):
        return self.client.post(
            "/api/event",
            json={"s": self.site["public_id"], "n": "pageview", "p": "/x"},
            headers={"user-agent": ua, "x-forwarded-for": ip},
        )

    def assertClean(self, text, ua=UA, ip=IP):
        self.assertNotIn(ip, text)
        self.assertNotIn(ua, text)
        self.assertNotIn("secret-build", text)
        self.assertNotRegex(text, r"[0-9a-f]{32}")  # no visitor hash

    def test_bot_drop_logs_reason_and_match(self):
        with self.assertLogs("sporlos.ingest", "INFO") as cm:
            r = self.send(ua=BOT_UA)
        self.assertEqual(r.status_code, 204)
        self.assertEqual(self.rows(), [])
        self.assertEqual(len(cm.output), 1)
        line = cm.output[0]
        self.assertIn("ingest_drop reason=bot", line)
        self.assertIn("match=curl", line)
        self.assertIn(f"site={self.site['id']}", line)
        self.assertClean(line, ua=BOT_UA)
        self.assertEqual(main.drop_counts(), {(self.site["id"], "bot"): 1})

    def test_empty_ua_is_a_bot_with_match_empty(self):
        with self.assertLogs("sporlos.ingest", "INFO") as cm:
            self.send(ua="")
        self.assertIn("match=empty", cm.output[0])

    def test_datacenter_drop_logs_asn(self):
        with mock.patch.object(main, "datacenter_asn", return_value=24940), \
                self.assertLogs("sporlos.ingest", "INFO") as cm:
            r = self.send()
        self.assertEqual(r.status_code, 204)
        self.assertEqual(self.rows(), [])
        line = cm.output[0]
        self.assertIn("ingest_drop reason=datacenter", line)
        self.assertIn("asn=24940", line)
        self.assertIn("device=desktop", line)
        self.assertIn("browser=Chrome", line)
        self.assertClean(line)
        self.assertEqual(main.drop_counts(), {(self.site["id"], "datacenter"): 1})

    def test_rate_drop_logs_once_per_visitor_per_window(self):
        with mock.patch.object(main, "_MAX_EVENTS_PER_MINUTE", 2), \
                self.assertLogs("sporlos.ingest", "INFO") as cm:
            for _ in range(6):
                self.send()
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(len(cm.output), 1)  # 4 drops, one line
        self.assertIn("ingest_drop reason=rate", cm.output[0])
        self.assertClean(cm.output[0])
        self.assertEqual(main.drop_counts(), {(self.site["id"], "rate"): 4})

    def test_stored_event_logs_nothing(self):
        with self.assertNoLogs("sporlos.ingest", "INFO"):
            self.send()
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(main.drop_counts(), {})


class HelpersTest(unittest.TestCase):
    def test_bot_match(self):
        self.assertEqual(bot_match(""), "empty")
        self.assertEqual(bot_match("Slackbot-LinkExpanding"), "bot")
        self.assertEqual(bot_match("Mozilla/5.0 LinkPreview"), "preview")
        self.assertIsNone(bot_match(UA))

    def test_datacenter_asn_without_database_is_none(self):
        with mock.patch("app.datacenter._get_reader", return_value=None):
            self.assertIsNone(datacenter_asn("1.2.3.4"))

    def test_datacenter_asn_returns_listed_asn_only(self):
        class R:
            def get(self, ip):
                return {"autonomous_system_number": {"1.1.1.1": 24940, "2.2.2.2": 3303}[ip]}
        with mock.patch("app.datacenter._get_reader", return_value=R()):
            self.assertEqual(datacenter_asn("1.1.1.1"), 24940)
            self.assertIsNone(datacenter_asn("2.2.2.2"))


if __name__ == "__main__":
    unittest.main()
