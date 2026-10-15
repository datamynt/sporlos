"""?month=YYYY-MM on /api/v1/stats and /api/v1/breakdown (UTC calendar month).

tilse's monthly operations report reads last month's traffic from here, so the
window must be the exact UTC month, with the month before as previous_period.

Run ONE FILE PER PROCESS:
    .venv/bin/python3 -m pytest -q tests/test_api_month.py < /dev/null
"""

from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

os.environ.pop("DATABASE_URL", None)

from starlette.applications import Starlette  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from app import api, store  # noqa: E402


def _month(offset: int) -> str:
    """Label for the month `offset` months before the current UTC month."""
    now = datetime.now(timezone.utc)
    y, m = now.year, now.month - offset
    while m < 1:
        y, m = y - 1, m + 12
    return f"{y:04d}-{m:02d}"


class ApiMonthTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._prev_db = store._DB_PATH
        store._DB_PATH = os.path.join(self._tmpdir.name, "test.db")
        store.init_db()
        tid = store.create_tenant("Månedsrapport")
        site = store.create_site(tid, "kunde.example")
        self.public_id = site["public_id"]
        key = store.create_api_key(tid, "tilse")["key"]
        self.h = {"Authorization": f"Bearer {key}"}
        self.client = TestClient(Starlette(routes=[
            Route("/api/v1/stats", api.stats),
            Route("/api/v1/breakdown", api.breakdown),
        ]))
        self.last, self.before = _month(1), _month(2)
        rows = [
            (f"{self.last}-01 00:00:00", "/", "a"),      # first instant: inside
            (f"{self.last}-15 12:00:00", "/priser", "a"),
            (f"{self.last}-28 23:59:59", "/priser", "b"),
            (f"{self.before}-10 08:00:00", "/", "c"),    # previous_period
            (f"{_month(0)}-01 00:00:00", "/", "d"),      # this month: outside
        ]
        with store._cursor() as cur:
            for ts, path, vh in rows:
                cur.execute(
                    "INSERT INTO events (site_id, ts, name, path, visitor_hash) "
                    "VALUES (?, ?, 'pageview', ?, ?)",
                    (site["id"], ts, path, vh),
                )

    def tearDown(self):
        store._DB_PATH = self._prev_db
        self._tmpdir.cleanup()

    def _get(self, path, **params):
        return self.client.get(path, params={"site": self.public_id, **params}, headers=self.h)

    def test_stats_for_a_calendar_month(self):
        r = self._get("/api/v1/stats", month=self.last)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["month"], self.last)
        self.assertEqual(body["stats"]["pageviews"], 3)
        self.assertEqual(body["stats"]["visitors"], 2)
        self.assertEqual(body["previous_period"]["pageviews"], 1)

    def test_breakdown_for_a_calendar_month(self):
        r = self._get("/api/v1/breakdown", month=self.last, prop="pages")
        self.assertEqual(r.status_code, 200)
        pages = {row["value"]: row["pageviews"] for row in r.json()["breakdown"]}
        self.assertEqual(pages, {"/priser": 2, "/": 1})

    def test_period_still_works_without_month(self):
        r = self._get("/api/v1/stats", period="30")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["period_days"], 30)

    def test_rejects_bad_future_and_too_old_months(self):
        future = (datetime.now(timezone.utc) + timedelta(days=40)).strftime("%Y-%m")
        too_old = (datetime.now(timezone.utc) - timedelta(days=130)).strftime("%Y-%m")
        for bad in ("2026-13", "2026-9", "sept", future, too_old):
            with self.subTest(month=bad):
                self.assertEqual(self._get("/api/v1/stats", month=bad).status_code, 400)

    def test_month_window_crosses_the_year(self):
        self.assertEqual(store.month_window("2026-12"), ("2026-12-01 00:00:00", "2027-01-01 00:00:00"))
        self.assertEqual(store.month_window("2026-01", offset=1), ("2025-12-01 00:00:00", "2026-01-01 00:00:00"))


if __name__ == "__main__":
    unittest.main()
