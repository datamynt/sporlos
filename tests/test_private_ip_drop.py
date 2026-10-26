"""Stdlib unittest: ingest drops events whose client IP is private, loopback,
link-local or otherwise non-global (hairpin NAT from the operator's own network),
never stores them, and does not count them as robots. SPORLOS_COUNT_PRIVATE_IPS=1
restores counting for self-hosters on an intranet.

Run with:
    .venv/bin/python3 -m unittest tests.test_private_ip_drop -v < /dev/null
"""

from __future__ import annotations

import os
import unittest
from unittest import mock

from tests.test_ingest_limits import IngestTestCase

from app import botcount, main, store

PUBLIC = "84.208.10.20"
PRIVATE = [
    "10.1.2.3",
    "172.16.5.4",
    "192.168.1.50",
    "127.0.0.1",
    "169.254.10.10",
    "::1",
    "fe80::1",
    "fd12:3456:789a::1",  # ULA fc00::/7
    "::ffff:192.168.1.50",  # IPv4-mapped IPv6
    "::ffff:10.0.0.7",
]


class PrivateIpDropTest(IngestTestCase):
    def setUp(self):
        super().setUp()
        with main._drop_lock:
            main._drop_counts.clear()
        self._env = mock.patch.dict(os.environ)
        self._env.start()
        os.environ.pop("SPORLOS_COUNT_PRIVATE_IPS", None)

    def tearDown(self):
        self._env.stop()

    def send(self, ip):
        return self.post({"s": self.site["public_id"], "n": "pageview", "p": "/x"}, ip=ip)

    def test_private_addresses_are_dropped_and_never_stored(self):
        for ip in PRIVATE:
            with self.subTest(ip=ip):
                r = self.send(ip)
                self.assertEqual(r.status_code, 204)
        self.assertEqual(self.rows(), [])
        self.assertEqual(main.drop_counts(), {(self.site["id"], "private"): len(PRIVATE)})

    def test_drop_logs_reason_without_the_ip(self):
        with self.assertLogs("sporlos.ingest", "INFO") as cm:
            self.send("192.168.1.50")
        self.assertEqual(len(cm.output), 1)
        self.assertIn("ingest_drop reason=private", cm.output[0])
        self.assertNotIn("192.168.1.50", cm.output[0])

    def test_private_drop_is_not_a_bot_count(self):
        with mock.patch.object(botcount, "note") as note:
            self.send("10.1.2.3")
        note.assert_not_called()

    def test_public_ip_is_stored_as_before(self):
        r = self.send(PUBLIC)
        self.assertEqual(r.status_code, 204)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(main.drop_counts(), {})

    def test_mapped_public_ip_is_stored(self):
        self.send("::ffff:" + PUBLIC)
        self.assertEqual(len(self.rows()), 1)

    def test_unparseable_ip_is_not_treated_as_private(self):
        self.send("not-an-ip")
        self.assertEqual(len(self.rows()), 1)

    def test_env_flag_restores_counting(self):
        for value in ("1", "true"):
            with self.subTest(value=value), mock.patch.dict(
                os.environ, {"SPORLOS_COUNT_PRIVATE_IPS": value}
            ):
                before = len(self.rows())
                self.send("192.168.1.50")
                self.assertEqual(len(self.rows()), before + 1)
        self.assertEqual(main.drop_counts(), {})

    def test_env_flag_off_values_still_drop(self):
        with mock.patch.dict(os.environ, {"SPORLOS_COUNT_PRIVATE_IPS": "0"}):
            self.send("192.168.1.50")
        self.assertEqual(self.rows(), [])

    def test_is_private_ip_helper(self):
        for ip in PRIVATE:
            self.assertTrue(main._is_private_ip(ip), ip)
        for ip in (PUBLIC, "2606:4700:4700::1111", "::ffff:" + PUBLIC, "", "garbage"):
            self.assertFalse(main._is_private_ip(ip), ip)


if __name__ == "__main__":
    unittest.main()
