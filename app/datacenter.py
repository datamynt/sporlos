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
    # Budget hosting that rotating-proxy pools and scrapers rent by the IP (added
    # 27.10.2026 after a price scraper came in through 36352/18779/46261/62874 with
    # a fresh human-looking UA per hit). Names as in DB-IP ASN Lite. None of these
    # sells residential or mobile access, so no real visitor sits behind them.
    36352: "ColoCrossing/HostPapa",
    18779: "EGIHosting",
    46261: "QuickPacket",
    62874: "Web2Objects",
    40021: "Contabo",        # Contabo Inc. (US); 51167 is the German entity above
    8100: "QuadraNet",
    53667: "FranTech/BuyVM",
    35916: "MULTACOM",
    55286: "B2 Net/ServerMania",
    397423: "Tier.Net",
    32097: "WholeSale Internet",
    23470: "ReliableSite",
    14576: "Hosting Solution",
    20454: "Secured Servers",
    29802: "Hivelocity",
    40676: "Psychz",
    30058: "FDCservers",
    25820: "IT7 Networks",
    19318: "Interserver",
    30633: "Leaseweb USA",
}
# Deliberately NOT listed, although they are mostly hosting: M247 (9009) and
# Datacamp (60068, 212238) carry the exit nodes of consumer VPNs, so real people
# browse from them. Same reasoning as for Cloudflare and Apple Private Relay above.
# Carriers and ISPs (Cogent 174, T-Mobile, Verizon, Windstream ...) are never listed:
# real end users sit behind them. The residential part of a proxy pool is caught
# by the behavioural burst filter (app/burst.py) instead.
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
