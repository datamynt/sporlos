"""Stats-API v1 — read-only programmatisk tilgang for AI-verktøy og integrasjoner.

Auth: `Authorization: Bearer sl_...` (nøkkel lages under «API-tilgang» i /app).
Alt er aggregater — det finnes ingen rådata å hente, by design (se app/privacy.py).

Designvalg:
  - site identifiseres med public_id (samme som i tracker-snippet)
  - period = 1 | 7 | 30 (kalenderdager t.o.m. i dag, UTC — samme som dashboardet)
  - land returneres som ISO-koder (maskinvennlig; dashboardet oversetter til norsk)
"""

from __future__ import annotations

import json

from starlette.responses import JSONResponse

from app import store

_PERIOD_DAYS = {"1": 1, "7": 7, "30": 30}

# Tak på body — domenet er en kort streng; alt større er søppel eller misbruk.
_MAX_BODY = 8192


def _err(msg: str, status: int) -> JSONResponse:
    return JSONResponse({"error": msg}, status_code=status)


def _auth(request) -> dict | None:
    """API-nøkkel fra Authorization-header → {id, tenant_id}, eller None."""
    h = request.headers.get("authorization", "")
    if not h.lower().startswith("bearer "):
        return None
    return store.resolve_api_key(h[7:].strip())


def _site_and_days(request, key) -> tuple[dict | None, int, JSONResponse | None]:
    """Felles parsing: ?site=<public_id> (må tilhøre nøkkelens tenant) + ?period=."""
    public_id = request.query_params.get("site") or ""
    site = store.resolve_site(public_id) if public_id else None
    if not site or site["tenant_id"] != key["tenant_id"]:
        return None, 0, _err("ukjent site — list dine med GET /api/v1/sites", 404)
    period = request.query_params.get("period", "7")
    if period not in _PERIOD_DAYS:
        return None, 0, _err("period må være 1, 7 eller 30", 400)
    return site, _PERIOD_DAYS[period], None


def sites(request):
    key = _auth(request)
    if not key:
        return _err("ugyldig eller manglende API-nøkkel", 401)
    out = [
        {"domain": s["domain"], "site": s["public_id"]}
        for s in store.list_sites(key["tenant_id"])
    ]
    return JSONResponse({"sites": out})


async def create_site(request):
    """Opprett (eller hent) ett nettsted for nøkkelens tenant — idempotent.

    Samme domene to ganger gir samme public_id (200 fra andre kall), så en
    bygger som publiserer en side om gangen kan kalle dette fritt uten å
    etterlate duplikater. Krever Bearer-nøkkel (samme som resten av API-et);
    nøkkelen er tenant-scopet, så en kaller kan aldri røre andres sites.

    Status: 201 ny, 200 fantes, 400 ugyldig domene/kropp, 401 ugyldig nøkkel,
    403 planens nettsted-grense nådd.
    """
    key = _auth(request)
    if not key:
        return _err("ugyldig eller manglende API-nøkkel", 401)
    body = await request.body()
    if len(body) > _MAX_BODY:
        return _err("for stor forespørsel", 400)
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        return _err('ugyldig JSON — send {"domain": "dittdomene.no"}', 400)
    domain = store.normalize_domain(data.get("domain")) if isinstance(data, dict) else ""
    if not domain:
        return _err("domain må være et gyldig domenenavn (maks 253 tegn)", 400)
    tenant_id = key["tenant_id"]
    existing = store.get_site_by_domain(tenant_id, domain)
    if existing:
        # Allerede registrert under denne kontoen — returner den, ikke en kopi.
        return JSONResponse(
            {"public_id": existing["public_id"], "domain": existing["domain"], "created": False}
        )
    tenant = store.get_tenant(tenant_id) or {}
    _, site_lim = store.plan_limits(tenant.get("plan") or "trial")
    if site_lim is not None and store.monthly_usage(tenant_id)["sites"] >= site_lim:
        # Samme grense og samme norske ordlyd som dashbordet (/app?limit=sites).
        return _err(
            f"Planen din har plass til {site_lim} nettsted"
            f"{'er' if site_lim != 1 else ''} — oppgrader for å legge til flere.",
            403,
        )
    try:
        site = store.create_site(tenant_id, domain)
    except Exception:
        # Tapt kappløp (to samtidige kall med samme domene): raden finnes nå —
        # hent den i stedet for å svare 500 på noe som faktisk lyktes.
        raced = store.get_site_by_domain(tenant_id, domain)
        if raced:
            return JSONResponse(
                {"public_id": raced["public_id"], "domain": raced["domain"], "created": False}
            )
        raise
    return JSONResponse(
        {"public_id": site["public_id"], "domain": site["domain"], "created": True},
        status_code=201,
    )


def stats(request):
    """KPI-er + topp-lister for perioden, med forrige periode til sammenligning."""
    key = _auth(request)
    if not key:
        return _err("ugyldig eller manglende API-nøkkel", 401)
    site, days, err = _site_and_days(request, key)
    if err:
        return err
    s = store.stats(site["id"], days)
    prev = store.kpis(site["id"], days, offset=1)
    return JSONResponse(
        {"site": request.query_params.get("site"), "period_days": days,
         "stats": s, "previous_period": prev}
    )


def timeseries(request):
    key = _auth(request)
    if not key:
        return _err("ugyldig eller manglende API-nøkkel", 401)
    site, days, err = _site_and_days(request, key)
    if err:
        return err
    return JSONResponse(
        {"site": request.query_params.get("site"), "period_days": days,
         "timeseries": store.timeseries(site["id"], days)}
    )


def breakdown(request):
    key = _auth(request)
    if not key:
        return _err("ugyldig eller manglende API-nøkkel", 401)
    site, days, err = _site_and_days(request, key)
    if err:
        return err
    prop = request.query_params.get("prop", "pages")
    if prop not in store._API_DIMS:
        return _err(f"prop må være en av: {', '.join(store._API_DIMS)}", 400)
    try:
        limit = min(1000, max(1, int(request.query_params.get("limit", "100"))))
    except ValueError:
        return _err("limit må være et tall", 400)
    return JSONResponse(
        {"site": request.query_params.get("site"), "period_days": days, "prop": prop,
         "breakdown": store.api_breakdown(site["id"], days, prop, limit)}
    )


def goals(request):
    key = _auth(request)
    if not key:
        return _err("ugyldig eller manglende API-nøkkel", 401)
    site, days, err = _site_and_days(request, key)
    if err:
        return err
    return JSONResponse(
        {"site": request.query_params.get("site"), "period_days": days,
         "goals": store.goal_stats(site["id"], days)}
    )


def events(request):
    """Egendefinerte hendelser (alt som ikke er pageview)."""
    key = _auth(request)
    if not key:
        return _err("ugyldig eller manglende API-nøkkel", 401)
    site, days, err = _site_and_days(request, key)
    if err:
        return err
    out = [
        {"name": e["k"], "total": e["n"], "visitors": e["u"]}
        for e in store.top_events(site["id"], days)
    ]
    return JSONResponse(
        {"site": request.query_params.get("site"), "period_days": days, "events": out}
    )


def ecommerce(request):
    """E-handel: ordrer/omsetning per valuta + toppprodukter + kilde + betalingsmåte.

    Beløp i øre (heltall). products/sources/payment_methods gjelder dominerende valuta
    (flest ordrer); kilde = besøkerens første kilde samme dag (hashen roterer daglig);
    betalingsmåte = slug butikken selv sendte i purchase-kallet ('ukjent' når utelatt)."""
    key = _auth(request)
    if not key:
        return _err("ugyldig eller manglende API-nøkkel", 401)
    site, days, err = _site_and_days(request, key)
    if err:
        return err
    try:
        limit = min(1000, max(1, int(request.query_params.get("limit", "100"))))
    except ValueError:
        return _err("limit må være et tall", 400)
    ec = store.ecommerce_stats(site["id"], days)
    dom = ec["by_currency"][0]["currency"] if ec["by_currency"] else "NOK"
    return JSONResponse(
        {"site": request.query_params.get("site"), "period_days": days,
         "orders": ec["orders"], "revenue": ec["by_currency"], "currency": dom,
         "products": store.top_products(site["id"], days, dom, limit),
         "sources": store.revenue_by_source(site["id"], days, dom, limit),
         "payment_methods": store.revenue_by_payment(site["id"], days, dom, limit)}
    )


def anchors(request):
    """Dags-aggregater m/ sha256 + ev. BSV-txid — bevis på at tallene ikke er etterjustert."""
    key = _auth(request)
    if not key:
        return _err("ugyldig eller manglende API-nøkkel", 401)
    site, _, err = _site_and_days(request, key)
    if err:
        return err
    out = [
        {"day": str(r["day"]), "pageviews": r["pageviews"], "visitors": r["visitors"],
         "rollup_hash": r["rollup_hash"], "txid": r["txid"]}
        for r in store.recent_rollups(site["id"], limit=30)
    ]
    return JSONResponse({"site": request.query_params.get("site"), "anchors": out})
