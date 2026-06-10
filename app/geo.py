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


# Engelsk → norsk for landnavn (DB-IP gir engelske navn; vi viser norsk).
# Oversettes i VISNINGS-laget så lagrede data forblir kanoniske.
COUNTRY_NO = {
    "Norway": "Norge", "Sweden": "Sverige", "Denmark": "Danmark", "Finland": "Finland",
    "Iceland": "Island", "Germany": "Tyskland", "United States": "USA",
    "United Kingdom": "Storbritannia", "Netherlands": "Nederland", "Belgium": "Belgia",
    "France": "Frankrike", "Spain": "Spania", "Portugal": "Portugal", "Italy": "Italia",
    "Ireland": "Irland", "Austria": "Østerrike", "Switzerland": "Sveits", "Poland": "Polen",
    "Czechia": "Tsjekkia", "Czech Republic": "Tsjekkia", "Estonia": "Estland",
    "Latvia": "Latvia", "Lithuania": "Litauen", "Russia": "Russland", "Ukraine": "Ukraina",
    "Greece": "Hellas", "Turkey": "Tyrkia", "Türkiye": "Tyrkia", "China": "Kina",
    "Japan": "Japan", "South Korea": "Sør-Korea", "Republic of Korea": "Sør-Korea",
    "India": "India", "Brazil": "Brasil", "Mexico": "Mexico", "Canada": "Canada",
    "Australia": "Australia", "New Zealand": "New Zealand", "South Africa": "Sør-Afrika",
    "Croatia": "Kroatia", "Hungary": "Ungarn", "Romania": "Romania", "Bulgaria": "Bulgaria",
    "Slovakia": "Slovakia", "Slovenia": "Slovenia", "Serbia": "Serbia",
    "Thailand": "Thailand", "Vietnam": "Vietnam", "Philippines": "Filippinene",
    "Indonesia": "Indonesia", "United Arab Emirates": "De forente arabiske emirater",
    "Saudi Arabia": "Saudi-Arabia", "Israel": "Israel", "Egypt": "Egypt",
    "Argentina": "Argentina", "Chile": "Chile", "Colombia": "Colombia",
    "Singapore": "Singapore", "Hong Kong": "Hongkong", "Taiwan": "Taiwan",
    "Luxembourg": "Luxembourg", "Cyprus": "Kypros", "Malta": "Malta",
    "North Macedonia": "Nord-Makedonia", "Bosnia and Herzegovina": "Bosnia-Hercegovina",
    "Albania": "Albania", "Moldova": "Moldova", "Belarus": "Hviterussland",
    "Georgia": "Georgia", "Armenia": "Armenia", "Azerbaijan": "Aserbajdsjan",
    "Kazakhstan": "Kasakhstan", "Pakistan": "Pakistan", "Bangladesh": "Bangladesh",
    "Sri Lanka": "Sri Lanka", "Nepal": "Nepal", "Morocco": "Marokko",
    "Algeria": "Algerie", "Tunisia": "Tunisia", "Nigeria": "Nigeria", "Kenya": "Kenya",
    "Ethiopia": "Etiopia", "Ghana": "Ghana", "Peru": "Peru", "Venezuela": "Venezuela",
    "Ecuador": "Ecuador", "Uruguay": "Uruguay", "Bolivia": "Bolivia",
    "Costa Rica": "Costa Rica", "Panama": "Panama", "Cuba": "Cuba",
    "Dominican Republic": "Den dominikanske republikk", "Greenland": "Grønland",
    "Faroe Islands": "Færøyene",
}


def country_no(name: str | None) -> str | None:
    """Norsk visningsnavn for land; ukjente navn passerer uendret."""
    return COUNTRY_NO.get(name, name) if name else name


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
