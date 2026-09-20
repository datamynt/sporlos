"""Cookieløs, samtykke-fri besøkende-identifikasjon.

Dette er kjernen i hele personvern-løftet. Vi lagrer ALDRI rå-IP — kun en
enveis-hash som inkluderer en server-side hemmelighet og et salt som roterer
DAGLIG. Når dagen er omme er gårsdagens salt forkastet, så hashene kan verken
reverseres eller lenkes på tvers av dager. Det er dette som gjør at data ikke
er re-identifiserbar => ingen samtykke-banner trengs under GDPR/ePrivacy.

`site_id` inngår i hashen med vilje: samme besøkende på to ulike byrå-kunder
gir to ulike hasher (cross-site non-linkability), viktig for multi-tenant.

The daily salt is RANDOM and lives in the `daily_salts` table (store.daily_salt),
which deletes every row that is not today's. It used to be derived as
sha256(secret:date) — that value can be recomputed for any past date by whoever
holds the secret, so nothing was ever discarded and stored hashes stayed
testable ("was IP x here on day d?") for the whole retention window. The secret
is still mixed in as a pepper: a leaked database alone is not enough.
"""

from __future__ import annotations

import hashlib


def _effective_salt(day_salt: str, secret: str) -> str:
    """Mix the day's random salt with the server-side secret (pepper).

    With `day_salt` set to the date string this equals the legacy derived salt,
    which store.daily_salt uses once, on the day of the switch, so that day's
    visitors are not counted twice.
    """
    return hashlib.sha256(f"{secret}:{day_salt}".encode()).hexdigest()


def visitor_hash(
    ip: str,
    user_agent: str,
    site_id: str,
    *,
    secret: str,
    day_salt: str,
) -> str:
    """Stabil-innen-dagen, ikke-reversibel besøkende-identifikator.

    Samme (ip, ua, site) gir samme hash gjennom hele dagen (slik at vi kan
    telle unike og sesjoner), men en helt ny hash neste dag.

    `day_salt` is required on purpose: there is no fallback that could quietly
    bring back a salt derivable from the secret.
    """
    if not day_salt:
        raise ValueError("visitor_hash requires the day's random salt")
    salt = _effective_salt(day_salt, secret)
    raw = f"{salt}|{ip}|{user_agent}|{site_id}"
    return hashlib.sha256(raw.encode()).hexdigest()


def client_ip(headers: dict, fallback: str = "") -> str:
    """Hent klient-IP fra proxy-headere. Brukes KUN til hashing, lagres aldri."""
    xff = headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return headers.get("x-real-ip", "") or fallback
