"""Behaviour tests for integrations/shopify/sporlos-pixel.js, run in node against a fake
Shopify Web Pixels sandbox (tests/shopify_pixel_harness.js).

The privacy policy promises that visitors with Global Privacy Control or Do Not Track
are never counted on any site using Sporløs. The Shopify pixel has to honour that too.

Run with:
    .venv/bin/python3 -m unittest tests.test_shopify_pixel -v < /dev/null
"""

from __future__ import annotations

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PIXEL = ROOT / "integrations" / "shopify" / "sporlos-pixel.js"
HARNESS = Path(__file__).resolve().parent / "shopify_pixel_harness.js"
NODE = shutil.which("node")


def run(scenario: str) -> list[dict]:
    out = subprocess.run(
        [NODE, str(HARNESS), str(PIXEL), scenario],
        capture_output=True, stdin=subprocess.DEVNULL, check=True, timeout=30,
    )
    return json.loads(out.stdout)


@unittest.skipUnless(NODE, "node not installed")
class ShopifyPixelPrivacyTest(unittest.TestCase):
    def test_visitor_without_signals_is_counted(self):
        for scenario in ("plain", "dnt-off", "sale-on", "no-navigator"):
            with self.subTest(scenario=scenario):
                (ev,) = run(scenario)
                self.assertEqual((ev["s"], ev["n"], ev["p"]), ("SITE", "pageview", "/products/x"))
                self.assertEqual(ev["us"], "nyhetsbrev")
                self.assertNotIn("wd", ev)

    def test_global_privacy_control_sends_nothing(self):
        self.assertEqual(run("gpc"), [])
        self.assertEqual(run("ctx-gpc"), [])

    def test_do_not_track_sends_nothing(self):
        for scenario in ("dnt", "dnt-yes", "dnt-ms", "dnt-window", "ctx-dnt"):
            with self.subTest(scenario=scenario):
                self.assertEqual(run(scenario), [])

    def test_shopify_sale_of_data_opt_out_sends_nothing(self):
        # Shopify honours GPC by setting saleOfDataAllowed=false.
        self.assertEqual(run("sale-off"), [])

    def test_consent_change_after_load_is_respected(self):
        # pageview goes out, then the visitor opts out: the next event is dropped.
        (ev,) = run("consent-event")
        self.assertEqual(ev["n"], "pageview")

    def test_opt_out_also_blocks_purchase(self):
        self.assertEqual(run("gpc-purchase"), [])

    def test_purchase_still_carries_revenue(self):
        evs = run("purchase")
        purchase = [e for e in evs if e["n"] == "purchase"][0]
        self.assertEqual((purchase["rv"], purchase["cur"]), (1000, "NOK"))

    def test_automated_browser_is_flagged_not_dropped(self):
        (ev,) = run("webdriver")
        self.assertEqual(ev["wd"], 1)


if __name__ == "__main__":
    unittest.main()
