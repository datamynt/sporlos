"""IndexNow: varsle Bing/Seznam/Yandex m.fl. umiddelbart om nye/endrede URL-er.

Nøkkel: env INDEXNOW_KEY (8–128 tegn hex). Nøkkelfila serveres på
/<nøkkel>.txt (rute i main.py). Ping skjer ved sitemap-diff:

    .venv/bin/python3 -m app.manage indexnow-ping < /dev/null   # daglig cron

Første kjøring ser hele sitemapen som «ny» — det er greit (engangs-ping av
alt innhold er gyldig bruk). Deretter pinges kun nye URL-er.
"""

from __future__ import annotations

import logging
import os
import re

import httpx

from app import store

log = logging.getLogger("sporlos.indexnow")

ENDPOINT = "https://api.indexnow.org/indexnow"
_LOC = re.compile(r"<loc>\s*([^<\s]+)\s*</loc>")


def key() -> str | None:
    k = os.environ.get("INDEXNOW_KEY", "").strip()
    return k if re.fullmatch(r"[A-Za-z0-9-]{8,128}", k or "") else None


def ping(base_url: str) -> str:
    """Sitemap-diff → ping. Returnerer CLI-oppsummering."""
    k = key()
    if not k:
        return "INDEXNOW_KEY ikke satt (eller ugyldig) — hopper over"
    r = httpx.get(f"{base_url}/sitemap.xml", timeout=30, follow_redirects=True)
    r.raise_for_status()
    host = base_url.split("://", 1)[-1].split("/")[0]
    urls = [u for u in _LOC.findall(r.text) if host in u]
    fresh = store.indexnow_filter_new(urls)
    if not fresh:
        return f"indexnow: {len(urls)} URL-er i sitemap, ingen nye"
    resp = httpx.post(ENDPOINT, json={
        "host": host,
        "key": k,
        "keyLocation": f"{base_url}/{k}.txt",
        "urlList": fresh[:10000],
    }, timeout=30)
    return (f"indexnow: pinget {len(fresh)} nye URL-er (av {len(urls)}) — "
            f"HTTP {resp.status_code}")
