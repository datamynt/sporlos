"""sporlos-mcp — MCP-server oppå Sporløs Stats-API (read-only).

Gir AI-assistenter (Claude, ChatGPT m.fl.) direkte tilgang til analysetallene
dine. Trygt by design: API-et serverer kun aggregater — rådata finnes ikke.

Oppsett:
    pip install -r requirements.txt
    SPORLOS_API_KEY=sl_...  (lag nøkkel under «API-tilgang» i sporlos.no/app)
    SPORLOS_API_BASE=https://sporlos.no  (default; sett for self-host)

Registrer i Claude Code:
    claude mcp add sporlos -e SPORLOS_API_KEY=sl_... -- python3 server.py
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from mcp.server.fastmcp import FastMCP

API_BASE = os.environ.get("SPORLOS_API_BASE", "https://sporlos.no").rstrip("/")
API_KEY = os.environ.get("SPORLOS_API_KEY", "")

mcp = FastMCP("sporlos")


def _get(path: str, **params) -> dict:
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{API_BASE}{path}" + (f"?{qs}" if qs else "")
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {API_KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"error": f"HTTP {e.code}"}


@mcp.tool()
def list_sites() -> dict:
    """Nettstedene på kontoen (domene + site-ID). Start her for å finne site-ID-er."""
    return _get("/api/v1/sites")


@mcp.tool()
def get_stats(site: str, period: int = 7) -> dict:
    """KPI-er (unike, visninger, økter, fluktrate) + topplister for et nettsted,
    med forrige periode til sammenligning. period = 1, 7 eller 30 dager."""
    return _get("/api/v1/stats", site=site, period=period)


@mcp.tool()
def get_timeseries(site: str, period: int = 7) -> dict:
    """Unike + visninger per dag (per time når period=1)."""
    return _get("/api/v1/timeseries", site=site, period=period)


@mcp.tool()
def get_breakdown(site: str, prop: str = "pages", period: int = 7, limit: int = 20) -> dict:
    """Full liste per dimensjon: prop = pages | sources | countries | regions |
    devices | browsers | os. Land er ISO-koder."""
    return _get("/api/v1/breakdown", site=site, prop=prop, period=period, limit=limit)


@mcp.tool()
def get_goals(site: str, period: int = 7) -> dict:
    """Mål/konverteringer med fullføringer og rate."""
    return _get("/api/v1/goals", site=site, period=period)


@mcp.tool()
def get_events(site: str, period: int = 7) -> dict:
    """Egendefinerte hendelser (alt som ikke er sidevisning)."""
    return _get("/api/v1/events", site=site, period=period)


@mcp.tool()
def get_anchors(site: str) -> dict:
    """Forseglede dags-aggregater: sha256-hash + blokkjede-txid. Bevis på at
    historiske tall ikke er endret i etterkant."""
    return _get("/api/v1/anchors", site=site)


if __name__ == "__main__":
    mcp.run()
