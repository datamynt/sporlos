"""Behaviour tests for tracker/sporlos.js, run in node against a fake browser
(tests/tracker_harness.js). Every scenario runs against BOTH the readable source
and the minified build that /sporlos.js actually serves.

Run with:
    .venv/bin/python3 -m unittest tests.test_tracker -v < /dev/null
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "tracker" / "sporlos.js"
MIN = ROOT / "tracker" / "sporlos.min.js"
HARNESS = Path(__file__).resolve().parent / "tracker_harness.js"
NODE = shutil.which("node")


def run(build: Path, scenario: str) -> list[dict]:
    out = subprocess.run(
        [NODE, str(HARNESS), str(build), scenario],
        capture_output=True, stdin=subprocess.DEVNULL, check=True, timeout=30,
    )
    return json.loads(out.stdout)


class MinifiedBuildTest(unittest.TestCase):
    def test_minified_build_matches_the_source(self):
        """Fails when sporlos.js was edited without `python3 scripts/build_tracker.py`."""
        want = "src:" + hashlib.sha256(SRC.read_bytes()).hexdigest()[:16]
        self.assertIn(want, MIN.read_text().split("\n", 1)[0])

    def test_app_serves_the_minified_build(self):
        from app import main

        self.assertEqual(main._TRACKER, MIN.read_text())
        self.assertLess(len(main._TRACKER), len(main._TRACKER_SRC))


@unittest.skipUnless(NODE, "node not installed")
class TrackerBehaviourTest(unittest.TestCase):
    def each_build(self):
        for build in (SRC, MIN):
            with self.subTest(build=build.name):
                yield build

    def test_first_pageview(self):
        for build in self.each_build():
            (ev,) = run(build, "plain")
            self.assertEqual((ev["s"], ev["n"], ev["p"]), ("SITE", "pageview", "/"))
            self.assertEqual(ev["r"], "https://www.google.com/search?q=secret+query")

    def test_only_whitelisted_utm_keys_leave_the_page(self):
        for build in self.each_build():
            (ev,) = run(build, "utm")
            self.assertEqual((ev["us"], ev["um"], ev["uc"]), ("nyhetsbrev", "epost", None))
            self.assertNotIn("ola@example.no", json.dumps(ev))
            self.assertEqual(ev["p"], "/")

    def test_spa_counts_each_page_once_and_does_not_repeat_the_referrer(self):
        for build in self.each_build():
            events = run(build, "spa")
            self.assertEqual([e["p"] for e in events], ["/", "/produkter", "/kasse"])
            self.assertEqual(events[0]["r"], "https://www.google.com/search?q=secret+query")
            self.assertEqual(events[1]["r"], "https://shop.example/")
            self.assertEqual(events[2]["r"], "https://shop.example/produkter")

    def test_popstate_is_a_pageview(self):
        for build in self.each_build():
            self.assertEqual([e["p"] for e in run(build, "popstate")], ["/", "/a", "/"])

    def test_page_restored_from_bfcache_counts_again(self):
        for build in self.each_build():
            self.assertEqual([e["p"] for e in run(build, "bfcache")], ["/", "/"])

    def test_prerendered_page_counts_on_activation_only(self):
        for build in self.each_build():
            events = run(build, "prerender")
            self.assertEqual(events[0], {"marker": "before-activation"})
            self.assertEqual([e.get("n") for e in events[1:]], ["pageview"])

    def test_automated_browsers_send_nothing(self):
        for build in self.each_build():
            self.assertEqual(run(build, "webdriver"), [])

    def test_custom_events_and_ecommerce(self):
        for build in self.each_build():
            _, purchase, signup = run(build, "custom")
            self.assertEqual(purchase["n"], "purchase")
            self.assertEqual((purchase["rv"], purchase["cur"], purchase["pm"]), (119800, "NOK", "vipps"))
            self.assertEqual(purchase["it"], [{"n": "eSIM Europa 10 GB", "q": 2, "p": 59900}])
            self.assertEqual(signup["n"], "signup")
            self.assertNotIn("rv", signup)


if __name__ == "__main__":
    unittest.main()
