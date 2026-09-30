"""Stdlib unittest for masking ID-like path segments (app/pathmask.py), at ingest
and retroactively (app/mask_stored.py). SQLite backend, same setup as the other
suites. All IDs below are synthetic.

Run with:
    .venv/bin/python3 -m unittest tests.test_pathmask -v < /dev/null
"""

from __future__ import annotations

import csv
import json
import os
import stat
import tempfile
import unittest

_TMP_DB = tempfile.NamedTemporaryFile(prefix="sporlos_test_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ.setdefault("SPORLOS_DB", _TMP_DB.name)
os.environ.pop("DATABASE_URL", None)

from starlette.testclient import TestClient  # noqa: E402

from app import main, mask_stored, store  # noqa: E402
from app.pathmask import is_id_like, mask_label, mask_path  # noqa: E402

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"

HEX16 = "3f2a9b1c7d4e5f60"
HEX64 = "9c1f" * 16
UUID = "123e4567-e89b-42d3-a456-426614174000"
ICCID = "8900000000000000017"
B64URL = "Zq3_Xk9-pL2mN8vR4tY7wQ"  # separators split it into runs shorter than 16
B58 = "1BoatSLRHtKNngkdXEeobR76b53LETtpyT"


class MaskPathTest(unittest.TestCase):
    def test_id_like_segments_are_masked(self):
        cases = {
            f"/order/{HEX16}": "/order/:id",
            f"/order/{HEX16}/receipt.pdf": "/order/:id/receipt.pdf",
            f"/tx/{HEX64}": "/tx/:id",
            f"/files/{UUID}": "/files/:id",
            f"/files/report-{UUID}.pdf": "/files/:id",
            f"/esim/topup/{ICCID}": "/esim/topup/:id",
            "/n/123456789012": "/n/:id",
            "/a/0a1b2c3d4e5f": "/a/:id",
            f"/reset/{B64URL}": "/reset/:id",
            f"/support/{HEX16}/1787554372:{HEX16}:{HEX64}": "/support/:id/:id",
            "/reset/MQ/c3x5g9-8f1e0b6f0a7c9d8e2f1a": "/reset/MQ/:id",
            f"/u/{B58}/someone": "/u/:id/someone",
            "/p/k7x2m9q4w8z3n6v1": "/p/:id",
            "/unsubscribe/ola.nordmann@example.no": "/unsubscribe/:id",
            "/unsubscribe/ola%40example.no": "/unsubscribe/:id",
            f"/a/{HEX16}/b/{UUID}/c": "/a/:id/b/:id/c",
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(mask_path(raw), want)

    def test_readable_paths_are_kept(self):
        for p in (
            "/", "", "/pakker/TR", "/en/pakker/US", "/guide/esim-tyrkia",
            "/region/balkan-utvalg", "/2026/09/30", "/arkiv/2026-09-30",
            "/blogg/20260930", "/n/12345678901", "/deadbeef", "/bestill/EU_1_Month_3Days",
            "/iphone-15-pro-max-256gb", "/iPhone-15-Pro-Max-256GB", "/wiki/Apollo_11_Mission_Report",
            "/guide/esim-tyrkia-10gb-30-dager", "/internationalization", "/@handle",
            "/mp3-to-wav-converter-2025", "/artikler/slik-virker-det.html", "/v2/api/stats",
            "/s%C3%B8k/%C3%A6%C3%B8%C3%A5", "/sider/side-2",
        ):
            with self.subTest(path=p):
                self.assertEqual(mask_path(p), p)

    def test_masking_is_idempotent(self):
        for p in (f"/order/{HEX16}", "/order/:id", f"/x/{B64URL}/y", "/guide/esim-tyrkia"):
            with self.subTest(path=p):
                self.assertEqual(mask_path(mask_path(p)), mask_path(p))
        self.assertFalse(is_id_like(":id"))

    def test_labels(self):
        self.assertEqual(mask_label("purchase"), "purchase")
        self.assertEqual(mask_label("payment_method_selected_stripe"), "payment_method_selected_stripe")
        self.assertEqual(mask_label(f"order {HEX16}"), "order :id")
        self.assertEqual(mask_label(f"order_{HEX16}"), ":id")
        self.assertIsNone(mask_label(None))


class IngestMaskingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store.init_db()
        cls.client = TestClient(main.app)

    def setUp(self):
        tenant = store.create_tenant("Mask test")
        self.site = store.create_site(tenant, f"mask-{os.urandom(4).hex()}.example")
        store._site_cache.clear()
        main._rate_counts.clear()

    def rows(self):
        with store._cursor() as cur:
            cur.execute("SELECT name, path FROM events WHERE site_id = ? ORDER BY id",
                        (self.site["id"],))
            return [dict(r) for r in cur.fetchall()]

    def post(self, body):
        return self.client.post("/api/event", content=json.dumps(body).encode(),
                                headers={"user-agent": UA, "x-forwarded-for": "84.208.10.20"})

    def test_pageview_custom_and_purchase_paths_are_masked(self):
        sid = self.site["public_id"]
        self.post({"s": sid, "n": "pageview", "p": f"/order/{HEX16}"})
        self.post({"s": sid, "n": "checkout_start", "p": f"/pay/{HEX16}?x=1"})
        self.post({"s": sid, "n": "purchase", "p": f"https://shop.example/topup/{ICCID}",
                   "rv": 59900, "cur": "NOK", "it": [{"n": "eSIM 10 GB", "q": 1, "p": 59900}]})
        self.post({"s": sid, "n": f"order {HEX16}", "p": "/takk"})
        self.post({"s": sid, "n": "pageview", "p": "/guide/esim-tyrkia"})
        self.assertEqual(self.rows(), [
            {"name": "pageview", "path": "/order/:id"},
            {"name": "checkout_start", "path": "/pay/:id"},
            {"name": "purchase", "path": "/topup/:id"},
            {"name": "order :id", "path": "/takk"},
            {"name": "pageview", "path": "/guide/esim-tyrkia"},
        ])


class GoalFunnelMaskingTest(unittest.TestCase):
    """Goals and funnel steps are compared with stored (masked) events, so they
    are masked the same way when they are created."""

    def test_goal_and_funnel_values_are_masked(self):
        store.init_db()
        email = f"mask-{os.urandom(4).hex()}@example.no"
        tenant_id, _ = store.create_account("Mask AS", email, main.hash_password("passord123"))
        site = store.create_site(tenant_id, f"goals-{os.urandom(4).hex()}.example")
        client = TestClient(main.app)
        r = client.post("/login", data={"email": email, "password": "passord123"},
                        follow_redirects=False)
        self.assertEqual(r.status_code, 302, r.text[:200])
        pid = site["public_id"]
        client.post("/app/goals", data={"site": pid, "name": "g1", "match_type": "path",
                                        "match_value": f"/order/{HEX16}"}, follow_redirects=False)
        client.post("/app/goals", data={"site": pid, "name": "g2", "match_type": "event",
                                        "match_value": "signup"}, follow_redirects=False)
        client.post("/app/funnels", data={"site": pid, "name": "f",
                                          "steps": f"/pay/{UUID}\npurchase"}, follow_redirects=False)
        with store._cursor() as cur:
            cur.execute("SELECT match_value FROM goals WHERE site_id = ? ORDER BY id", (site["id"],))
            self.assertEqual([r["match_value"] for r in cur.fetchall()], ["/order/:id", "signup"])
            cur.execute("SELECT steps FROM funnels WHERE site_id = ?", (site["id"],))
            self.assertEqual(json.loads(cur.fetchone()["steps"]),
                             [{"type": "path", "value": "/pay/:id"},
                              {"type": "event", "value": "purchase"}])


class MaskStoredTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store.init_db()

    def setUp(self):
        with store._cursor() as cur:
            for t in ("event_items", "events", "goals", "funnels"):
                cur.execute(f"DELETE FROM {t}")
        tenant = store.create_tenant("Retro test")
        self.site = store.create_site(tenant, f"retro-{os.urandom(4).hex()}.example")
        sid = self.site["id"]
        with store._cursor() as cur:
            for name, path in (("pageview", f"/order/{HEX16}"), ("pageview", f"/order/{HEX16}"),
                               ("pageview", "/pakker/TR"), (f"order {HEX16}", "/takk")):
                cur.execute("INSERT INTO events (site_id, ts, name, path, visitor_hash) "
                            "VALUES (?, '2026-01-01 10:00:00', ?, ?, 'v')", (sid, name, path))
        store.create_goal(sid, "raw", "path", f"/order/{HEX16}")
        store.create_goal(sid, "fine", "path", "/takk")
        store.create_funnel(sid, "f", [{"type": "path", "value": f"/order/{HEX16}"},
                                       {"type": "event", "value": "purchase"}])
        self.rollup = store.compute_rollup(sid, "2026-01-01")
        self.backup_dir = tempfile.mkdtemp(prefix="sporlos_mask_")

    def snapshot(self):
        with store._cursor() as cur:
            cur.execute("SELECT name, path FROM events ORDER BY id")
            ev = [(r["name"], r["path"]) for r in cur.fetchall()]
            cur.execute("SELECT match_value FROM goals ORDER BY id")
            goals = [r["match_value"] for r in cur.fetchall()]
            cur.execute("SELECT steps FROM funnels ORDER BY id")
            funnels = [json.loads(r["steps"]) for r in cur.fetchall()]
            cur.execute("SELECT rollup_hash FROM daily_rollups ORDER BY site_id, day")
            rollups = [r["rollup_hash"] for r in cur.fetchall()]
        return ev, goals, funnels, rollups

    def test_dry_run_counts_and_changes_nothing(self):
        before = self.snapshot()
        res = mask_stored.run(apply=False, backup_dir=self.backup_dir)
        self.assertEqual(res["rows"], {"events.path": 2, "events.name": 1,
                                       "goals.match_value": 1, "funnels.steps": 1})
        self.assertEqual(res["examples"], ["/order/:id"])
        self.assertFalse(res["applied"])
        self.assertIsNone(res["backup"])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(os.listdir(self.backup_dir), [])

    def test_apply_backs_up_then_masks_and_keeps_rollup_hashes(self):
        _, _, _, rollups_before = self.snapshot()
        res = mask_stored.run(apply=True, backup_dir=self.backup_dir)
        self.assertTrue(res["applied"])
        ev, goals, funnels, rollups = self.snapshot()
        self.assertEqual(ev, [("pageview", "/order/:id"), ("pageview", "/order/:id"),
                              ("pageview", "/pakker/TR"), ("order :id", "/takk")])
        self.assertEqual(goals, ["/order/:id", "/takk"])
        self.assertEqual(funnels, [[{"type": "path", "value": "/order/:id"},
                                    {"type": "event", "value": "purchase"}]])
        self.assertEqual(rollups, rollups_before)
        # The sealed hash covers counts only: recomputing after masking gives the same one.
        self.assertEqual(self.rollup["pageviews"], 3)
        self.assertEqual(store.compute_rollup(self.site["id"], "2026-01-01"), self.rollup)

        self.assertEqual(stat.S_IMODE(os.stat(res["backup"]).st_mode), 0o600)
        with open(res["backup"], newline="") as f:
            rows = list(csv.DictReader(f))
        self.assertEqual(len(rows), 5)
        self.assertEqual({r["old_value"] for r in rows if r["column"] == "path"}, {f"/order/{HEX16}"})

        again = mask_stored.run(apply=True, backup_dir=self.backup_dir)
        self.assertEqual(again["rows"], {})
        self.assertIsNone(again["backup"])


if __name__ == "__main__":
    unittest.main()
