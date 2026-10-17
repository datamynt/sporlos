"""The production start-up guard (SPORLOS_HTTPS=true) must refuse the placeholder
secrets that .env.example ships with. Runs `import app.main` in a subprocess because
the guard executes at import time.

Run:
    .venv/bin/python3 -m pytest -q tests/test_prod_guard.py < /dev/null
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
GOOD = {"SPORLOS_SALT_SECRET": "a" * 64, "SPORLOS_SESSION_SECRET": "b" * 64}


def _import_main(**env) -> subprocess.CompletedProcess:
    with tempfile.TemporaryDirectory() as tmp:
        full = {k: v for k, v in os.environ.items() if not k.startswith(("SPORLOS_", "DATABASE_URL"))}
        full.update(SPORLOS_HTTPS="true", SPORLOS_DOMAIN="sporlos.test",
                    SPORLOS_DB=str(Path(tmp) / "t.db"), MAIL_FROM="post@sporlos.test",
                    SMTP_HOST="smtp.test.invalid", **GOOD)
        full.update(env)
        return subprocess.run([sys.executable, "-c", "import app.main"], cwd=ROOT, env=full,
                              capture_output=True, text=True, timeout=60)


class ProdGuardTest(unittest.TestCase):
    def test_real_secrets_boot(self):
        r = _import_main()
        self.assertEqual(r.returncode, 0, r.stderr[-500:])

    def test_env_example_salt_placeholder_is_refused(self):
        r = _import_main(SPORLOS_SALT_SECRET="change-me-openssl-rand-hex-32")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("SPORLOS_SALT_SECRET", r.stderr)

    def test_env_example_session_placeholder_is_refused(self):
        r = _import_main(SPORLOS_SESSION_SECRET="change-me-openssl-rand-hex-32")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("SPORLOS_SESSION_SECRET", r.stderr)

    def test_old_default_is_still_refused(self):
        r = _import_main(SPORLOS_SALT_SECRET="dev-secret-change-me")
        self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
