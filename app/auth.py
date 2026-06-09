"""Passord-hashing (stdlib pbkdf2) + enkle session-hjelpere.

Ingen tunge avhengigheter — hashlib.pbkdf2_hmac holder for vår skala.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, _ITERATIONS)
    return f"pbkdf2_sha256${_ITERATIONS}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt_b64, dk_b64 = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(dk_b64)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, int(iters))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


def _secret() -> bytes:
    return (os.environ.get("SESSION_SECRET") or "dev-secret").encode()


def sign_token(purpose: str, value: str) -> str:
    """Kort signert token (hmac) for lenker uten DB-oppslag, f.eks. avmelding."""
    return hmac.new(_secret(), f"{purpose}:{value}".encode(), hashlib.sha256).hexdigest()[:32]


def check_token(purpose: str, value: str, token: str) -> bool:
    return hmac.compare_digest(sign_token(purpose, value), token or "")
