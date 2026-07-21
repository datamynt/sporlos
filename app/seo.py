"""Søkedata-sync: Google Search Console + Bing Webmaster → search_stats.

Kjøres fra CLI/cron (aldri i request-sti):
    .venv/bin/python3 -m app.manage seo-sync            < /dev/null   # rullerende 30 d
    .venv/bin/python3 -m app.manage seo-sync 480        < /dev/null   # backfill (GSC husker ~16 mnd)

Kobling per site skjer ved AUTOMATCH mot instansens nøkler — ingen konfig per site:
  - GSC: service account (GSC_SERVICE_ACCOUNT = sti til JSON, eller rå JSON).
    Kontoen må gis lesetilgang («Full»/«Begrenset») på hver property i
    Search Console-UI-et. Vi matcher sc-domain:<domene> og http(s)-prefiks-
    varianter (med/uten www) mot property-lista kontoen ser.
  - Bing: BING_WEBMASTER_API_KEY. Matcher mot GetUserSites.

Multi-tenant-merknad: nøklene er instans-globale (self-host-modellen — den som
drifter instansen eier koblingen). Per-tenant OAuth er bevisst utsatt; ikke
gjenbruk disse nøklene på en delt instans med fremmede tenants.

Google: dagstotaler + søkeord + sider (dimensions=[date,…]).
Bing:   kun dagstotaler (GetRankAndTrafficStats) — query-API-et har kjente
        kvirker (posisjon ×10 m.m.) og Bing er en liten andel; bevisst utelatt.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote, urlparse

import httpx

from app import store

log = logging.getLogger("sporlos.seo")

GSC_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
GSC_API = "https://searchconsole.googleapis.com/webmasters/v3"
BING_API = "https://ssl.bing.com/webmaster/api.svc/json"

# Beholder maks så mange søkeord/sider per site per dag — bounder veksten
# (19 sites × 2 dim × 100 × 365 ≈ 1,4 M rader/år — helt greit for PG/SQLite).
TOP_PER_DAY = 100


def _sa() -> dict | None:
    """Service account-JSON fra env: sti til fil, eller rå JSON-streng."""
    raw = os.environ.get("GSC_SERVICE_ACCOUNT", "").strip()
    if not raw:
        return None
    try:
        if raw.startswith("{"):
            return json.loads(raw)
        with open(raw, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log.error("GSC_SERVICE_ACCOUNT ugyldig (%s)", e)
        return None


def _bing_key() -> str | None:
    return os.environ.get("BING_WEBMASTER_API_KEY", "").strip() or None


def configured() -> bool:
    return bool(_sa() or _bing_key())


def _sa_token(sa: dict, scope: str) -> str:
    """OAuth2 JWT-bearer for service account — Authlib signerer RS256 (ingen ny dep)."""
    from authlib.jose import jwt  # allerede i requirements via Authlib

    now = int(time.time())
    assertion = jwt.encode(
        {"alg": "RS256", "typ": "JWT"},
        {"iss": sa["client_email"], "scope": scope, "aud": sa["token_uri"],
         "iat": now, "exp": now + 3600},
        sa["private_key"],
    ).decode()
    r = httpx.post(sa["token_uri"], data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": assertion,
    }, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def _gsc_token(sa: dict) -> str:
    return _sa_token(sa, GSC_SCOPE)


def _gsc_properties(token: str) -> set[str]:
    r = httpx.get(f"{GSC_API}/sites", headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    return {e["siteUrl"] for e in r.json().get("siteEntry", [])}


def _match_gsc(domain: str, props: set[str]) -> str | None:
    """Kandidater i prioritert rekkefølge — domain-property dekker www + subdomener."""
    for cand in (
        f"sc-domain:{domain}",
        f"https://{domain}/", f"https://www.{domain}/",
        f"http://{domain}/", f"http://www.{domain}/",
    ):
        if cand in props:
            return cand
    return None


def _gsc_query(token: str, prop: str, body: dict) -> list[dict]:
    """Henter ALLE rader: GSC gir maks rowLimit per kall, så vi paginerer med
    startRow til siste side er kort — ellers trunkeres travle siter/backfill
    stille ved 25k og eldre dager mister søkeord (review-funn 21.07)."""
    out: list[dict] = []
    limit = body.get("rowLimit", 25000)
    while True:
        r = httpx.post(
            f"{GSC_API}/sites/{quote(prop, safe='')}/searchAnalytics/query",
            headers={"Authorization": f"Bearer {token}"},
            json={**body, "startRow": len(out)}, timeout=60,
        )
        r.raise_for_status()
        rows = r.json().get("rows", [])
        out.extend(rows)
        if len(rows) < limit or len(out) >= 200_000:  # runaway-vern
            return out


def _gsc_fetch(token: str, prop: str, site_id: int, start: str, end: str) -> list[tuple]:
    """Rader for upsert: (site_id, day, source, dim, key, clicks, impressions, position)."""
    rows: list[tuple] = []
    # Dagstotaler
    for r in _gsc_query(token, prop, {
        "startDate": start, "endDate": end, "dimensions": ["date"], "rowLimit": 1000,
    }):
        rows.append((site_id, r["keys"][0], "google", "total", "",
                     int(r["clicks"]), int(r["impressions"]), r.get("position")))
    # Søkeord + sider per dag — API-et sorterer på klikk; vi capper per dag.
    for dim in ("query", "page"):
        raw = _gsc_query(token, prop, {
            "startDate": start, "endDate": end, "dimensions": ["date", dim],
            "rowLimit": 25000,
        })
        per_day: dict[str, list] = {}
        for r in raw:
            day, key = r["keys"][0], r["keys"][1]
            if dim == "page":
                # Lagre sti, ikke full URL — kortere nøkler, penere UI.
                key = urlparse(key).path or "/"
            per_day.setdefault(day, []).append(
                (site_id, day, "google", dim, key[:500],
                 int(r["clicks"]), int(r["impressions"]), r.get("position")))
        for day, lst in per_day.items():
            lst.sort(key=lambda t: (-t[5], -t[6]))
            rows.extend(lst[:TOP_PER_DAY])
    return rows


_BING_DATE = re.compile(r"/Date\((\d+)")


def _bing_day(raw) -> str | None:
    """Bing serialiserer datoer som '/Date(1600000000000-0700)/' (epoch ms).
    Tolkes i UTC — lese-vinduene er UTC, og lokal TZ ville forskjøvet dagen."""
    m = _BING_DATE.search(str(raw or ""))
    if not m:
        return None
    return datetime.fromtimestamp(int(m.group(1)) / 1000, tz=timezone.utc).date().isoformat()


def _bing_get(method: str, key: str, **params) -> list[dict]:
    r = httpx.get(f"{BING_API}/{method}", params={"apikey": key, **params}, timeout=60)
    r.raise_for_status()
    d = r.json().get("d")
    return d if isinstance(d, list) else []


def _match_bing(domain: str, urls: set[str]) -> str | None:
    for u in urls:
        host = urlparse(u).netloc.lower().removeprefix("www.")
        if host == domain.lower().removeprefix("www."):
            return u
    return None


def _bing_fetch(key: str, site_url: str, site_id: int, start: str) -> list[tuple]:
    rows = []
    for r in _bing_get("GetRankAndTrafficStats", key, siteUrl=site_url):
        day = _bing_day(r.get("Date"))
        if day and day >= start:
            rows.append((site_id, day, "bing", "total", "",
                         int(r.get("Clicks") or 0), int(r.get("Impressions") or 0), None))
    return rows


# --- Per-site GSC-kobling (OAuth, «Koble til Search Console»-knappen) --------
# Kunden godkjenner med egen Google-konto; vi lagrer KUN refresh-tokenet
# (Fernet-kryptert med nøkkel avledet av SPORLOS_SALT_SECRET) + valgt property.
# Dette er hosted-modellen; instans-SA-en over er self-host-modellen.

def _fernet():
    import base64
    import hashlib

    from cryptography.fernet import Fernet  # transitivt via Authlib

    secret = os.environ.get("SPORLOS_SALT_SECRET", "dev-secret-change-me")
    key = base64.urlsafe_b64encode(hashlib.sha256(f"gsc-conn:{secret}".encode()).digest())
    return Fernet(key)


def encrypt_token(raw: str) -> str:
    return _fernet().encrypt(raw.encode()).decode()


def decrypt_token(enc: str) -> str:
    return _fernet().decrypt(enc.encode()).decode()


def user_access_token(refresh_token: str) -> str:
    """Ferskt access-token fra brukerens refresh-token (OAuth-koblede sites)."""
    r = httpx.post("https://oauth2.googleapis.com/token", data={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": os.environ.get("GOOGLE_CLIENT_ID", ""),
        "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET", ""),
    }, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def gsc_properties_for_token(token: str) -> set[str]:
    """Property-lista et vilkårlig (bruker-)token ser — brukes av callback + sync."""
    return _gsc_properties(token)


def match_property(domain: str, props: set[str]) -> str | None:
    return _match_gsc(domain, props)


# --- Google Merchant Center (Shopping-tall) ----------------------------------
# Merchant API v1 (v1beta ble avviklet 28.02.2026). Krav utenom nøkkelen:
# SA-en må ha Standard-tilgang i Merchant Center, og GCP-prosjektet må være
# registrert mot merchant-kontoen (developerRegistration:registerGcp — engangs).

MERCHANT_API = "https://merchantapi.googleapis.com"


def _gmc_token(sa: dict) -> str:
    return _sa_token(sa, "https://www.googleapis.com/auth/content")


def _gmc_accounts(token: str) -> list[str]:
    r = httpx.get(f"{MERCHANT_API}/accounts/v1/accounts",
                  headers={"Authorization": f"Bearer {token}"}, timeout=30)
    r.raise_for_status()
    return [a["name"] for a in r.json().get("accounts", [])]  # "accounts/123"


def _gmc_homepage(token: str, account: str) -> str | None:
    try:
        r = httpx.get(f"{MERCHANT_API}/accounts/v1/{account}/homepage",
                      headers={"Authorization": f"Bearer {token}"}, timeout=30)
        r.raise_for_status()
        return urlparse(r.json().get("uri", "")).netloc.lower().removeprefix("www.") or None
    except Exception:
        return None


def _gmc_day(raw) -> str | None:
    """Merchant API serialiserer dato som google.type.Date ({year,month,day})
    eller ISO-streng avhengig av felt — tåler begge."""
    if isinstance(raw, dict):
        try:
            return f"{int(raw['year']):04d}-{int(raw['month']):02d}-{int(raw['day']):02d}"
        except Exception:
            return None
    s = str(raw or "")[:10]
    return s if len(s) == 10 else None


def _gmc_fetch(token: str, account: str, site_id: int, start: str, end: str) -> list[tuple]:
    """Dagstotaler for produktvisninger/-klikk (free listings + Shopping samlet)."""
    rows: list[tuple] = []
    body = {"query": (
        "SELECT date, clicks, impressions FROM product_performance_view "
        f"WHERE date BETWEEN '{start}' AND '{end}'"
    )}
    page_token = None
    while True:
        if page_token:
            body["pageToken"] = page_token
        r = httpx.post(f"{MERCHANT_API}/reports/v1/{account}/reports:search",
                       headers={"Authorization": f"Bearer {token}"}, json=body, timeout=60)
        r.raise_for_status()
        data = r.json()
        for res in data.get("results", []):
            v = res.get("productPerformanceView") or {}
            day = _gmc_day(v.get("date"))
            if day:
                rows.append((site_id, day, "gmc", "total", "",
                             int(v.get("clicks") or 0), int(v.get("impressions") or 0), None))
        page_token = data.get("nextPageToken")
        if not page_token:
            return rows


def _gmc_account_map(token: str, domains: dict[str, int]) -> dict[str, int]:
    """account-ressurs → site_id. Auto via homepage-URI; GMC_ACCOUNT_MAP
    («123:merdata.no,456:annen.no») som manuell overstyring/fallback."""
    out: dict[str, int] = {}
    manual = {}
    for pair in os.environ.get("GMC_ACCOUNT_MAP", "").split(","):
        if ":" in pair:
            acc_id, dom = pair.split(":", 1)
            manual[acc_id.strip()] = dom.strip().lower()
    try:
        accounts = _gmc_accounts(token)
    except Exception as e:
        log.error("GMC accounts.list feilet: %s", e)
        return out
    for acc in accounts:
        acc_id = acc.split("/")[-1]
        dom = manual.get(acc_id) or _gmc_homepage(token, acc)
        if dom and dom in domains:
            out[acc] = domains[dom]
    return out


def sync(days: int = 30) -> str:
    """Synk alle sites mot GSC/Bing over et rullerende vindu. Returnerer CLI-oppsummering.

    Upsert er idempotent — GSC etterjusterer ferske dager, så vi re-henter alltid
    hele vinduet og overskriver. Feil per site logges og stopper ikke resten.
    """
    sa, bing_key = _sa(), _bing_key()
    store.ensure_search_schema()
    has_connections = bool(store.search_connections_all())
    if not sa and not bing_key and not has_connections:
        return ("ingen kilder: sett GSC_SERVICE_ACCOUNT / BING_WEBMASTER_API_KEY, "
                "eller koble en site til Search Console i UI-et")

    end = datetime.now(timezone.utc).date() - timedelta(days=1)  # GSC har sjelden data for i dag
    start = (end - timedelta(days=days - 1)).isoformat()

    gsc_props: set[str] = set()
    token = ""
    if sa:
        try:
            token = _gsc_token(sa)
            gsc_props = _gsc_properties(token)
        except Exception as e:
            log.error("GSC-auth feilet: %s", e)
            sa = None
    bing_urls: set[str] = set()
    if bing_key:
        try:
            bing_urls = {s["Url"] for s in _bing_get("GetUserSites", bing_key) if s.get("Url")}
        except Exception as e:
            log.error("Bing GetUserSites feilet: %s", e)
            bing_key = None

    # OAuth-koblede sites synces med kundens eget token og har FORRANG over
    # instans-SA-en (samme site skal aldri hentes dobbelt med ulik property).
    connections = {c["site_id"]: c for c in store.search_connections_all()}

    # GMC: kontoer mappes mot site-domener én gang per kjøring.
    gmc_map: dict[str, int] = {}
    gmc_token = ""
    if sa:
        try:
            gmc_token = _gmc_token(_sa())
            gmc_map = _gmc_account_map(
                gmc_token, {s["domain"].lower(): s["id"] for s in store.seo_sites()})
        except Exception as e:
            log.info("GMC hoppes over: %s", e)

    lines = []
    total = 0
    for site in store.seo_sites():
        parts = []
        rows: list[tuple] = []
        conn = connections.get(site["id"])
        if conn:
            try:
                utok = user_access_token(decrypt_token(conn["refresh_token"]))
                prop = conn.get("gsc_property") or _match_gsc(
                    site["domain"], _gsc_properties(utok))
                if prop:
                    got = _gsc_fetch(utok, prop, site["id"], start, end.isoformat())
                    rows += got
                    parts.append(f"gsc(kunde) {len(got)}")
                else:
                    parts.append("gsc(kunde) ingen property-match")
            except Exception as e:
                parts.append(f"gsc(kunde) FEIL ({e})")
        elif sa:
            prop = _match_gsc(site["domain"], gsc_props)
            if prop:
                try:
                    got = _gsc_fetch(token, prop, site["id"], start, end.isoformat())
                    rows += got
                    parts.append(f"gsc {len(got)}")
                except Exception as e:
                    parts.append(f"gsc FEIL ({e})")
            else:
                parts.append("gsc –")
        if bing_key:
            burl = _match_bing(site["domain"], bing_urls)
            if burl:
                try:
                    got = _bing_fetch(bing_key, burl, site["id"], start)
                    rows += got
                    parts.append(f"bing {len(got)}")
                except Exception as e:
                    parts.append(f"bing FEIL ({e})")
            else:
                parts.append("bing –")
        acc = next((a for a, sid in gmc_map.items() if sid == site["id"]), None)
        if acc:
            try:
                got = _gmc_fetch(gmc_token, acc, site["id"], start, end.isoformat())
                rows += got
                parts.append(f"gmc {len(got)}")
            except Exception as e:
                parts.append(f"gmc FEIL ({e})")
        if rows:
            try:
                store.upsert_search_rows(rows)
                total += len(rows)
            except Exception as e:
                parts.append(f"db FEIL ({e})")
        lines.append(f"  {site['domain']}: {', '.join(parts) or 'ingen kilder'}")

    return (f"seo-sync {start} → {end.isoformat()}: {total} rader "
            f"({len(gsc_props)} GSC-properties, {len(bing_urls)} Bing-sites synlige)\n"
            + "\n".join(lines))
