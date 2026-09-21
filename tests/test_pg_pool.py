"""Postgres-only tests for the connection pool in app/store.py.

Skipped unless SPORLOS_TEST_PG points at a throwaway database, e.g.

    docker run -d --rm --name sporlos-test-pg -e POSTGRES_PASSWORD=t \\
        -e POSTGRES_USER=sporlos -e POSTGRES_DB=sporlos -p 127.0.0.1:55439:5432 postgres:16
    SPORLOS_TEST_PG=postgresql://sporlos:t@127.0.0.1:55439/sporlos \\
        .venv/bin/python3 -m unittest tests.test_pg_pool -v < /dev/null

Must run in its own process: store.py picks its backend at import time, and the
rest of the suite imports it with SQLite.
"""

from __future__ import annotations

import os
import threading
import unittest

_PG = os.environ.get("SPORLOS_TEST_PG")
if _PG:
    os.environ["DATABASE_URL"] = _PG
    from app import store  # noqa: E402


def _backend_pid() -> int:
    with store._cursor() as cur:
        cur.execute("SELECT pg_backend_pid() AS pid")
        return cur.fetchone()["pid"]


@unittest.skipUnless(_PG, "SPORLOS_TEST_PG not set")
class PoolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store.init_db()

    def test_connections_are_reused(self):
        self.assertEqual(_backend_pid(), _backend_pid())

    def test_timezone_is_utc_on_every_checkout(self):
        with store._cursor() as cur:
            cur.execute("SET TIME ZONE 'Europe/Oslo'")
        with store._cursor() as cur:
            cur.execute("SHOW TIME ZONE")
            self.assertEqual(cur.fetchone()["TimeZone"], "UTC")

    def test_error_rolls_back_and_the_connection_stays_usable(self):
        with self.assertRaises(Exception):
            with store._cursor() as cur:
                cur.execute("CREATE TABLE pool_rollback_probe (x INT)")
                cur.execute("SELECT * FROM does_not_exist")
        with store._cursor() as cur:
            cur.execute("SELECT to_regclass('pool_rollback_probe') AS t")
            self.assertIsNone(cur.fetchone()["t"])

    def test_recovers_after_the_server_kills_idle_connections(self):
        """What a Postgres restart looks like from the pool's side."""
        victim = _backend_pid()
        import psycopg2

        admin = psycopg2.connect(_PG)
        admin.autocommit = True
        with admin.cursor() as cur:
            cur.execute("SELECT pg_terminate_backend(%s)", (victim,))
        admin.close()
        # The very next call must succeed, on a new backend.
        fresh = _backend_pid()
        self.assertNotEqual(fresh, victim)
        self.assertTrue(store.ping())

    def test_exhausted_pool_falls_back_instead_of_failing(self):
        pool = store._get_pool()
        held = []
        try:
            while True:
                held.append(pool.getconn())
        except Exception:
            pass
        try:
            self.assertGreaterEqual(len(held), 1)
            self.assertTrue(store.ping())
        finally:
            for c in held:
                pool.putconn(c)

    def test_concurrent_threads(self):
        errors: list[Exception] = []

        def work():
            try:
                for _ in range(25):
                    with store._cursor() as cur:
                        cur.execute("SELECT 1 AS x")
                        assert cur.fetchone()["x"] == 1
            except Exception as e:  # noqa: BLE001
                errors.append(e)

        threads = [threading.Thread(target=work) for _ in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])

    def test_site_cache(self):
        tenant = store.create_tenant("pool test")
        site = store.create_site(tenant, f"pool-{os.urandom(4).hex()}.example")
        store._site_cache.clear()
        a = store.resolve_site_cached(site["public_id"])
        self.assertEqual(a["id"], site["id"])
        self.assertIs(store.resolve_site_cached(site["public_id"]), a)  # served from cache
        self.assertIsNone(store.resolve_site_cached("no-such-site"))
        self.assertIn("no-such-site", store._site_cache)


if __name__ == "__main__":
    unittest.main()
