"""/webhooks/stripe verifies the signature with the real stripe library (no fake).

An unset STRIPE_WEBHOOK_SECRET must refuse events instead of "verifying" against an
empty HMAC key, which anyone can compute.

Run ONE FILE PER PROCESS:
    .venv/bin/python3 -m pytest -q tests/test_stripe_webhook.py < /dev/null
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest

_TMP_DB = tempfile.NamedTemporaryFile(prefix="sporlos_test_", suffix=".db", delete=False)
_TMP_DB.close()
os.environ["SPORLOS_DB"] = _TMP_DB.name
os.environ.pop("DATABASE_URL", None)
os.environ["STRIPE_SECRET_KEY"] = "sk_test_not_a_real_key"
for k in ("STRIPE_WEBHOOK_SECRET", "STRIPE_WEBHOOK_SECRET_TEST", "STRIPE_WEBHOOK_SECRET_LIVE"):
    os.environ.pop(k, None)

from starlette.testclient import TestClient  # noqa: E402

from app import main, store  # noqa: E402
from app.auth import hash_password  # noqa: E402


def setUpModule():
    store.init_db()


def tearDownModule():
    os.unlink(_TMP_DB.name)


def _signed(payload: str, secret: str) -> dict:
    ts = int(time.time())
    sig = hmac.new(secret.encode(), f"{ts}.{payload}".encode(), hashlib.sha256).hexdigest()
    return {"stripe-signature": f"t={ts},v1={sig}"}


def _event(tid: int) -> str:
    return json.dumps({
        "id": "evt_1", "object": "event", "type": "checkout.session.completed",
        "data": {"object": {"client_reference_id": str(tid), "metadata": {"plan": "pro"},
                            "customer": "cus_x", "subscription": "sub_x"}},
    })


class WebhookSecretTest(unittest.TestCase):
    def setUp(self):
        self.tid, _ = store.create_account("Hook AS", f"hook{time.time_ns()}@example.no",
                                           hash_password("passord-123"))
        self.client = TestClient(main.app)
        self.assertIsNotNone(main.stripe)  # STRIPE_SECRET_KEY was set before import

    def test_unset_secret_refuses_even_a_forged_signature(self):
        main.STRIPE_WEBHOOK_SECRET = ""
        body = _event(self.tid)
        r = self.client.post("/webhooks/stripe", content=body, headers=_signed(body, ""))
        self.assertEqual(r.status_code, 503)
        self.assertEqual(store.get_tenant(self.tid)["plan"], "trial")

    def test_wrong_signature_is_rejected(self):
        main.STRIPE_WEBHOOK_SECRET = "whsec_real"
        body = _event(self.tid)
        r = self.client.post("/webhooks/stripe", content=body, headers=_signed(body, "whsec_other"))
        self.assertEqual(r.status_code, 400)
        self.assertEqual(store.get_tenant(self.tid)["plan"], "trial")

    def test_valid_signature_still_sets_the_plan(self):
        main.STRIPE_WEBHOOK_SECRET = "whsec_real"
        body = _event(self.tid)
        real_modify = main.stripe.Customer.modify
        main.stripe.Customer.modify = staticmethod(lambda *a, **kw: None)  # no network
        try:
            r = self.client.post("/webhooks/stripe", content=body, headers=_signed(body, "whsec_real"))
        finally:
            main.stripe.Customer.modify = real_modify
        self.assertEqual(r.status_code, 200)
        self.assertEqual(store.get_tenant(self.tid)["plan"], "pro")


if __name__ == "__main__":
    unittest.main()
