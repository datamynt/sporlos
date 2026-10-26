"""Datasenter-deteksjon fra IP — fanger crawlere med «vanlig» User-Agent.

UA-filteret (useragent.is_bot) tar bots som identifiserer seg. Dette laget tar
resten: trafikk fra sky/hosting-nettverk (AWS, GCP, Hetzner …) der ekte
besøkende praktisk talt aldri kommer fra. Oppslaget skjer i ingest-øyeblikket
og IP-en forkastes etterpå — nøyaktig som geo-oppslaget. Ingenting nytt lagres.

Bevisst IKKE på listen: CDN-er og privacy-proxyer (Cloudflare, Fastly, Akamai,
Apple Private Relay-egress) — der sitter det ekte mennesker bak.

Bruker DB-IP ASN Lite (gratis, CC-BY, samme kilde som geo): se DEPLOY.md.
Degraderer pent: mangler ASN_DB-fila → alt slipper gjennom (som før).
"""

from __future__ import annotations

import os

_DB_PATH = os.environ.get("ASN_DB", "/data/asn.mmdb")
_reader = None  # None=ikke prøvd, False=utilgjengelig, ellers reader-objekt

# Kuratert liste over rene hosting/sky-ASN — ikke utfyllende, men dekker
# de store kildene til «direkte desktop-trafikk fra USA rett på /».
_DATACENTER_NAMES: dict[int, str] = {
    16509: "AWS",            # Amazon AWS
    14618: "AWS",            # Amazon AWS (us-east)
    396982: "Google Cloud",  # IKKE 15169 — der ligger også ekte brukere bak Google-proxyer
    8075: "Microsoft Azure",
    14061: "DigitalOcean",
    24940: "Hetzner",
    213230: "Hetzner",       # Hetzner Cloud
    16276: "OVH",
    63949: "Linode",         # Linode / Akamai Connected Cloud
    20473: "Vultr",          # Vultr / Choopa
    31898: "Oracle Cloud",
    45102: "Alibaba Cloud",
    132203: "Tencent Cloud",
    12876: "Scaleway",
    51167: "Contabo",
    197540: "netcup",
}
_DATACENTER_ASNS = frozenset(_DATACENTER_NAMES)


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


def datacenter_asn(ip: str) -> int | None:
    """The ASN number when the IP belongs to a listed hosting/cloud network,
    else None. An ASN identifies a network, not a person; it is what the drop
    log may show so a wrongly listed network can be spotted."""
    reader = _get_reader()
    if not reader or not ip:
        return None
    try:
        rec = reader.get(ip)
    except Exception:
        return None
    if not rec:
        return None
    asn = rec.get("autonomous_system_number")
    return asn if asn in _DATACENTER_ASNS else None


def datacenter_name(asn: int) -> str:
    """Provider name for a listed ASN (what the bot count stores as `agent`)."""
    return _DATACENTER_NAMES.get(asn, f"AS{asn}")


def is_datacenter(ip: str) -> bool:
    """True hvis IP-en hører til et kjent hosting/sky-ASN. Usikkert → False."""
    return datacenter_asn(ip) is not None
