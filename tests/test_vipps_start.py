"""GET /billing/vipps/start must not replace a live Vipps agreement.

Vipps is faked (no network, no money).
Run ONE FILE PER PROCESS:
    .venv/bin/python3 -m pytest -q tests/test_vipps_start.py < /dev/null
"""

from __future__ import annotations

import os
import tempfile
import unittest

_TMP_DB = tempfile.NamedTemporaryFile(prefix="sporlos_test_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ["SPORLOS_DB"] = _TMP_DB.name
os.environ.pop("DATABASE_URL", None)

from starlette.testclient import TestClient  # noqa: E402

from app import main, store, vipps  # noqa: E402
from app.auth import hash_password  # noqa: E402

CREATED: list[str] = []
STATUS: dict[str, str] = {}


def setUpModule():
    store.init_db()
    vipps.configured = lambda: True

    def create(plan, base):
        CREATED.append(plan)
        return {"agreementId": f"agr_new{len(CREATED)}", "vippsConfirmationUrl": "https://vipps.test/confirm"}

    vipps.create_agreement = create
    vipps.get_agreement = lambda agid: {"status": STATUS.get(agid, "PENDING")}


def tearDownModule():
    os.unlink(_TMP_DB.name)


def account(email: str):
    tid, uid = store.create_account("Vipps AS", email, hash_password("passord-123"))
    store.set_email_verified(uid)
    c = TestClient(main.app)
    c.post("/login", data={"email": email, "password": "passord-123"})
    return tid, c


class VippsStartTest(unittest.TestCase):
    def setUp(self):
        CREATED.clear()
        STATUS.clear()

    def test_first_agreement_is_created(self):
        tid, c = account("forst@example.no")
        r = c.get("/billing/vipps/start?plan=vekst", follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        t = store.get_tenant(tid)
        self.assertEqual((t["vipps_agreement_id"], t["vipps_pending_plan"]), ("agr_new1", "vekst"))

    def test_live_agreement_is_not_replaced(self):
        tid, c = account("aktiv@example.no")
        store.set_vipps_pending(tid, "agr_live", "liten")
        store.activate_vipps(tid, "liten", "2026-11-17")
        STATUS["agr_live"] = "ACTIVE"
        r = c.get("/billing/vipps/start?plan=pro", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(CREATED, [])
        t = store.get_tenant(tid)
        self.assertEqual((t["vipps_agreement_id"], t["plan"]), ("agr_live", "liten"))

    def test_approved_but_not_picked_up_goes_to_return_flow(self):
        tid, c = account("ventende@example.no")
        store.set_vipps_pending(tid, "agr_p", "vekst")
        STATUS["agr_p"] = "ACTIVE"
        r = c.get("/billing/vipps/start?plan=vekst", follow_redirects=False)
        self.assertEqual(r.headers["location"], "/billing/vipps/retur")
        self.assertEqual(CREATED, [])
        self.assertEqual(store.get_tenant(tid)["vipps_agreement_id"], "agr_p")

    def test_abandoned_pending_agreement_can_be_restarted(self):
        tid, c = account("avbrutt@example.no")
        store.set_vipps_pending(tid, "agr_old", "vekst")
        STATUS["agr_old"] = "EXPIRED"
        c.get("/billing/vipps/start?plan=vekst", follow_redirects=False)
        self.assertEqual(store.get_tenant(tid)["vipps_agreement_id"], "agr_new1")

    def test_status_lookup_failure_keeps_the_stored_agreement(self):
        tid, c = account("feil@example.no")
        store.set_vipps_pending(tid, "agr_x", "liten")
        store.activate_vipps(tid, "liten", "2026-11-17")

        def boom(agid):
            raise RuntimeError("vipps down")

        real = vipps.get_agreement
        vipps.get_agreement = boom
        try:
            r = c.get("/billing/vipps/start?plan=pro", follow_redirects=False)
        finally:
            vipps.get_agreement = real
        self.assertIn("vipps=feil", r.headers["location"])
        self.assertEqual(CREATED, [])
        self.assertEqual(store.get_tenant(tid)["vipps_agreement_id"], "agr_x")


if __name__ == "__main__":
    unittest.main()
