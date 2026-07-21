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


def _gsc_token(sa: dict) -> str:
    """OAuth2 JWT-bearer for service account — Authlib signerer RS256 (ingen ny dep)."""
    from authlib.jose import jwt  # allerede i requirements via Authlib

    now = int(time.time())
    assertion = jwt.encode(
        {"alg": "RS256", "typ": "JWT"},
        {"iss": sa["client_email"], "scope": GSC_SCOPE, "aud": sa["token_uri"],
         "iat": now, "exp": now + 3600},
        sa["private_key"],
    ).decode()
    r = httpx.post(sa["token_uri"], data={
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": assertion,
    }, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


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


def sync(days: int = 30) -> str:
    """Synk alle sites mot GSC/Bing over et rullerende vindu. Returnerer CLI-oppsummering.

    Upsert er idempotent — GSC etterjusterer ferske dager, så vi re-henter alltid
    hele vinduet og overskriver. Feil per site logges og stopper ikke resten.
    """
    sa, bing_key = _sa(), _bing_key()
    if not sa and not bing_key:
        return "ingen nøkler: sett GSC_SERVICE_ACCOUNT og/eller BING_WEBMASTER_API_KEY"

    store.ensure_search_schema()
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

    lines = []
    total = 0
    for site in store.seo_sites():
        parts = []
        rows: list[tuple] = []
        if sa:
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
