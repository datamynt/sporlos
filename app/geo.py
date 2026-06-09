"""Geo-oppslag fra IP — LOKALT, ingen tredjepart, IP forkastes.

Bruker en lokal DB-IP Lite City .mmdb (CC-BY, gratis, ingen signup) via maxminddb.
Vi leser KUN land + fylke (subdivision). By-nivå brukes ALDRI — det er
personvern-cappen (se IN_FLIGHT/README): finere geo + lav trafikk = re-identifiserbar.

Degraderer pent: mangler DB-fila (f.eks. lokal dev) → (None, None), ingen feil.
GEOIP_DB peker på .mmdb-fila (default /data/geoip.mmdb i prod-containeren).
"""

from __future__ import annotations

import os

_DB_PATH = os.environ.get("GEOIP_DB", "/data/geoip.mmdb")
_reader = None  # None=ikke prøvd, False=utilgjengelig, ellers reader-objekt


def _get_reader():
    global _reader
    if _reader is False:
        return None
    if _reader is None:
        try:
            import maxminddb

            _reader = maxminddb.open_database(_DB_PATH)
        except Exception:
            _reader = False
            return None
    return _reader


def lookup(ip: str) -> tuple[str | None, str | None]:
    """(land, fylke) eller (None, None). By-nivå leses bevisst IKKE."""
    reader = _get_reader()
    if not reader or not ip:
        return None, None
    try:
        rec = reader.get(ip)
    except Exception:
        return None, None
    if not rec:
        return None, None
    country = (rec.get("country") or {}).get("names", {}).get("en") or (
        rec.get("country") or {}
    ).get("iso_code")
    region = None
    subs = rec.get("subdivisions") or []
    if subs:
        names = subs[0].get("names") or {}
        region = names.get("en") or next(iter(names.values()), None)
    return country, region
