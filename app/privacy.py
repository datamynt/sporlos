"""Cookieløs, samtykke-fri besøkende-identifikasjon.

Dette er kjernen i hele personvern-løftet. Vi lagrer ALDRI rå-IP — kun en
enveis-hash som inkluderer en server-side hemmelighet og et salt som roterer
DAGLIG. Når dagen er omme er gårsdagens salt forkastet, så hashene kan verken
reverseres eller lenkes på tvers av dager. Det er dette som gjør at data ikke
er re-identifiserbar => ingen samtykke-banner trengs under GDPR/ePrivacy.

`site_id` inngår i hashen med vilje: samme besøkende på to ulike byrå-kunder
gir to ulike hasher (cross-site non-linkability), viktig for multi-tenant.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone


def _daily_salt(date_str: str, secret: str) -> str:
    """Avled dagens salt fra en server-side hemmelighet + dato.

    `secret` bør komme fra KMS/Secret Manager, ikke ligge i koden.
    Roterer automatisk når `date_str` skifter ved midnatt UTC.
    """
    return hashlib.sha256(f"{secret}:{date_str}".encode()).hexdigest()


def visitor_hash(
    ip: str,
    user_agent: str,
    site_id: str,
    *,
    secret: str,
    now: datetime | None = None,
) -> str:
    """Stabil-innen-dagen, ikke-reversibel besøkende-identifikator.

    Samme (ip, ua, site) gir samme hash gjennom hele dagen (slik at vi kan
    telle unike og sesjoner), men en helt ny hash neste dag.
    """
    now = now or datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    salt = _daily_salt(date_str, secret)
    raw = f"{salt}|{ip}|{user_agent}|{site_id}"
    return hashlib.sha256(raw.encode()).hexdigest()


def client_ip(headers: dict, fallback: str = "") -> str:
    """Hent klient-IP fra proxy-headere. Brukes KUN til hashing, lagres aldri."""
    xff = headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return headers.get("x-real-ip", "") or fallback
