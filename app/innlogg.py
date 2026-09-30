"""Google/Microsoft login through Datamynt ID (Zitadel IdP intents).

Zitadel holds the Google and Microsoft client credentials. Sporløs only starts an
intent and reads its result, server to server, with a login-client PAT
(IAM_LOGIN_CLIENT). No Zitadel user or session is created, so Zitadel's own
account linking never decides who a visitor is: Sporløs binds each login to
(IdP id, the provider's own user id) itself, see store.user_identities.

Why not trust the e-mail: the shared Microsoft IdP is multi-tenant and reads the
address from Microsoft Graph, where any tenant admin can type any address. Only
Google asserts a verified address. main.py therefore links an existing account,
or creates a new one, from a Microsoft login only after a confirmation e-mail.

Env: DATAMYNT_LOGIN_PAT, DATAMYNT_ID_ISSUER, IDP_GOOGLE_ID, IDP_MICROSOFT_ID.
Without the PAT or the IdP ids the buttons don't render and the routes redirect.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

import httpx

ISSUER = os.environ.get("DATAMYNT_ID_ISSUER", "https://id.datamynt.no").rstrip("/")
_PAT = os.environ.get("DATAMYNT_LOGIN_PAT", "").strip()
_ORG_ID = os.environ.get("DATAMYNT_LOGIN_ORG_ID", "").strip()

# provider name in our URLs -> Zitadel IdP id (shared, instance level)
PROVIDERS = {
    name: idp
    for name, idp in (
        ("google", os.environ.get("IDP_GOOGLE_ID", "").strip()),
        ("microsoft", os.environ.get("IDP_MICROSOFT_ID", "").strip()),
    )
    if idp
}
LABELS = {"google": "Google", "microsoft": "Microsoft"}

# Zitadel ids are alphanumeric. The intent id comes from the callback URL and is
# interpolated into an API path, so anything else (e.g. "../") is refused.
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class IdpError(Exception):
    """Login through the provider failed. Show a generic message to the user."""


@dataclass
class IdpLogin:
    provider: str          # "google" | "microsoft"
    idp_id: str            # Zitadel IdP id
    subject: str           # the provider's own, stable user id
    email: str | None
    email_verified: bool   # only ever True for Google, see module docstring
    name: str | None


def enabled(provider: str | None = None) -> bool:
    if not _PAT:
        return False
    return provider in PROVIDERS if provider else bool(PROVIDERS)


def _headers() -> dict:
    h = {"Authorization": f"Bearer {_PAT}", "Content-Type": "application/json"}
    if _ORG_ID:
        h["x-zitadel-orgid"] = _ORG_ID
    return h


async def start(provider: str, success_url: str, failure_url: str) -> str:
    """Start an intent and return the provider URL to send the browser to."""
    if not enabled(provider):
        raise IdpError("provider not enabled")
    body = {"idpId": PROVIDERS[provider], "urls": {"successUrl": success_url, "failureUrl": failure_url}}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{ISSUER}/v2/idp_intents", headers=_headers(), json=body)
    except httpx.HTTPError as e:
        raise IdpError(f"start: {type(e).__name__}") from e
    if r.status_code not in (200, 201) or not r.json().get("authUrl"):
        raise IdpError(f"start: HTTP {r.status_code}")
    return r.json()["authUrl"]


def _truthy(v) -> bool:
    # Some IdPs serialise email_verified as the string "false"; bool("false") is True.
    return v is True or str(v).strip().lower() in ("true", "1")


def _pick(raw: dict, *keys):
    nested = raw.get("User") or raw.get("user") or {}
    for src in (raw, nested):
        if isinstance(src, dict):
            for k in keys:
                if src.get(k):
                    return src[k]
    return None


async def finish(provider: str, intent_id: str, intent_token: str) -> IdpLogin:
    """Read a finished intent (single-use token) and return who logged in."""
    if not enabled(provider):
        raise IdpError("provider not enabled")
    if not _ID.match(intent_id or "") or not intent_token:
        raise IdpError("malformed callback")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{ISSUER}/v2/idp_intents/{intent_id}",
                headers=_headers(),
                json={"idpIntentToken": intent_token},
            )
    except httpx.HTTPError as e:
        raise IdpError(f"finish: {type(e).__name__}") from e
    if r.status_code != 200:
        raise IdpError(f"finish: HTTP {r.status_code}")
    info = r.json().get("idpInformation") or {}
    idp_id = str(info.get("idpId") or "")
    # The intent must come from the provider this flow was started for.
    if idp_id != PROVIDERS[provider]:
        raise IdpError("idp mismatch")
    subject = str(info.get("userId") or "").strip()
    if not subject:
        raise IdpError("no subject")
    raw = info.get("rawInformation") or {}
    if provider == "google":
        email = _pick(raw, "email", "Email")
        verified = _truthy(_pick(raw, "email_verified", "EmailVerified"))
    else:
        # Graph /me: `mail` is admin-editable in the user's own tenant, never verified.
        email = _pick(raw, "mail", "email", "userPrincipalName")
        verified = False
    name = _pick(raw, "name", "displayName", "given_name") or info.get("userName")
    email = (str(email).strip().lower() or None) if email else None
    if email and "@" not in email:
        email = None
    return IdpLogin(provider, idp_id, subject, email, bool(email) and verified,
                    str(name)[:80] if name else None)
