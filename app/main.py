"""Sporløs — ingestion + dashboard.

Lokal dogfood (SQLite):
    .venv/bin/python3 -m app.manage init                               < /dev/null
    .venv/bin/python3 -m app.manage create-site "Datamynt" merdata.no  < /dev/null
    .venv/bin/uvicorn app.main:app                                     < /dev/null

Prod-lik (Postgres i Docker): se docker-compose.yml / DEPLOY.md.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from app import api, assist, blogg, icons, mailer, notify, store, vipps
from app.auth import check_token, hash_password, verify_password
from app.datacenter import is_datacenter
from app.geo import country_no
from app.geo import lookup as geo_lookup
from app.privacy import client_ip, visitor_hash
from app.useragent import is_bot, parse_ua

SECRET = os.environ.get("SPORLOS_SALT_SECRET", "dev-secret-change-me")
SESSION_SECRET = os.environ.get("SPORLOS_SESSION_SECRET") or SECRET
HTTPS_ONLY = os.environ.get("SPORLOS_HTTPS", "").lower() in ("1", "true", "yes")
_DOMAIN = os.environ.get("SPORLOS_DOMAIN", "")
PUBLIC_BASE = f"https://{_DOMAIN}" if _DOMAIN and "FYLL" not in _DOMAIN else "http://localhost:8000"

log = logging.getLogger("sporlos")

# Prod-vakt: nekt oppstart i prod-modus (SPORLOS_HTTPS=true) hvis kritisk konfig mangler.
# Disse feilet alle STILLE før (kunde-klarhets-revisjon 2026-06-15): default-secret =
# re-derivbare visitor-hash + forfalskbare sesjoner; tomt domene = localhost-snippeter +
# døde reset-lenker; manglende SMTP = passord-reset som «lykkes» uten å sende. En uoppmerksom
# redeploy skal krasje høylytt her, ikke kjøre videre med en av disse aktive.
if HTTPS_ONLY:
    # HARD-FAIL kun på det som gjør produktet ØDELAGT/ulovlig: default-salt = re-derivbare
    # visitor-hash (samtykke-fritaket faller), og tomt domene = hver kunde-snippet + reset-lenke
    # peker på localhost. Disse skal krasje boot høylytt.
    _fatal = []
    if SECRET == "dev-secret-change-me":
        _fatal.append("SPORLOS_SALT_SECRET (visitor-hash + samtykke-fritak)")
    if not _DOMAIN or "FYLL" in _DOMAIN:
        _fatal.append("SPORLOS_DOMAIN (ellers peker snippet + reset-lenker på localhost)")
    if _fatal:
        _msg = (
            "Sporløs nekter å starte i prod-modus (SPORLOS_HTTPS=true) — mangler kritisk konfig:\n  - "
            + "\n  - ".join(_fatal)
        )
        log.critical(_msg)
        raise RuntimeError(_msg)
    # Disse degraderer KUN delvis → høylytt logg, men IKKE boot-stopp (skal aldri ta ned ingest):
    # sjekk rå-env (ikke den fallback'ede SESSION_SECRET, som ellers maskerer en manglende nøkkel).
    if not os.environ.get("SPORLOS_SESSION_SECRET"):
        log.critical("SPORLOS_SESSION_SECRET ikke satt — sesjoner bruker salt-nøkkelen som reserve. Sett egen.")
    if not mailer.configured():
        log.critical("SMTP (SMTP_HOST + MAIL_FROM) ikke satt — passord-reset/verifisering feiler STILLE.")


def _user(request):
    """Innlogget bruker fra session, eller None."""
    uid, tid = request.session.get("uid"), request.session.get("tid")
    return {"uid": uid, "tid": tid} if uid and tid else None


# Ekstern innlogging (OpenID Connect) — hver leverandør aktiveres kun når
# credentials er satt. «innlogg» = innlogg.no, Datamynt-flåtens felles innlogging
# (Zitadel) — registrer sporløs som OIDC-app der og sett INNLOGG_CLIENT_ID/SECRET.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
INNLOGG_ISSUER = os.environ.get("INNLOGG_ISSUER", "https://id.datamynt.no")
INNLOGG_CLIENT_ID = os.environ.get("INNLOGG_CLIENT_ID")
INNLOGG_CLIENT_SECRET = os.environ.get("INNLOGG_CLIENT_SECRET")
_HAS_GOOGLE = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
_HAS_INNLOGG = bool(INNLOGG_CLIENT_ID and INNLOGG_CLIENT_SECRET)
oauth = None
if _HAS_GOOGLE or _HAS_INNLOGG:
    from authlib.integrations.starlette_client import OAuth

    oauth = OAuth()
    if _HAS_GOOGLE:
        oauth.register(
            name="google",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )
    if _HAS_INNLOGG:
        oauth.register(
            name="innlogg",
            client_id=INNLOGG_CLIENT_ID,
            client_secret=INNLOGG_CLIENT_SECRET,
            server_metadata_url=f"{INNLOGG_ISSUER}/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )


def _sso_buttons():
    """«Fortsett med …»-knapper for aktiverte leverandører — tomt hvis ingen."""
    btn = (
        '<a href="{href}" style="display:block;text-align:center;border:1px solid var(--line);'
        'border-radius:8px;padding:.6rem;margin-top:1rem;text-decoration:none;color:var(--ink)">'
        "Fortsett med {navn}</a>"
    )
    out = ""
    if _HAS_GOOGLE:
        out += btn.format(href="/auth/google", navn="Google")
    if _HAS_INNLOGG:
        out += btn.format(href="/auth/innlogg", navn="innlogg.no")
    return out


# Stripe (kort-betaling) — mode-bevisst: STRIPE_MODE=test|live velger _TEST/_LIVE-nøkler.
# Faller tilbake til usuffikset variabel hvis den finnes. Aktiveres kun når secret er satt.
STRIPE_MODE = os.environ.get("STRIPE_MODE", "test").lower()


def _stripe_env(base):
    return os.environ.get(f"{base}_{STRIPE_MODE.upper()}") or os.environ.get(base)


STRIPE_SECRET = _stripe_env("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = _stripe_env("STRIPE_WEBHOOK_SECRET") or ""
STRIPE_PRICES = {
    "liten": _stripe_env("STRIPE_PRICE_LITEN"),
    "vekst": _stripe_env("STRIPE_PRICE_VEKST"),
    "pro": _stripe_env("STRIPE_PRICE_PRO"),
}
_PLAN_LABELS = {"liten": "Liten · 99/mnd", "vekst": "Vekst · 249/mnd", "pro": "Pro · 599/mnd"}
stripe = None
if STRIPE_SECRET:
    import stripe as _stripe

    _stripe.api_key = STRIPE_SECRET
    stripe = _stripe

# Tracker-scriptet leses én gang ved oppstart og serveres på /sporlos.js.
_TRACKER = (Path(__file__).resolve().parent.parent / "tracker" / "sporlos.js").read_text()
# Assistent-widgeten — samme mønster (egen fil, ikke inline-JS).
_ASSIST_JS = (Path(__file__).resolve().parent.parent / "assist" / "widget.js").read_text()
# Shopify Custom Pixel (Fase 1) — leses én gang, vises på /shopify til kopiering.
_SHOPIFY_PIXEL = (
    Path(__file__).resolve().parent.parent / "integrations" / "shopify" / "sporlos-pixel.js"
).read_text()
# Favicon-pakke (generert av scripts/make_favicons.py). Google leter spesifikt etter
# /favicon.ico; nettlesere etter /apple-touch-icon.png. Leses én gang ved oppstart.
_STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
_FAVICON_ICO = (_STATIC_DIR / "brand" / "favicon.ico").read_bytes()
_APPLE_ICON = (_STATIC_DIR / "brand" / "apple-touch-icon.png").read_bytes()
_WEBMANIFEST = json.dumps({
    "name": "Sporløs",
    "short_name": "Sporløs",
    "description": "Cookieløs, samtykke-fri webanalyse bygget i Norge.",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#faf9f6",
    "theme_color": "#faf9f6",
    "icons": [
        {"src": "/static/brand/icon-192.png", "sizes": "192x192", "type": "image/png"},
        {"src": "/static/brand/icon-512.png", "sizes": "512x512", "type": "image/png"},
    ],
}, ensure_ascii=False)


async def healthz(request):
    return PlainTextResponse("ok")


async def healthz_db(request):
    """Hele kjeden inkl. database — målet for «Datainnsamling»-monitoren.
    Forsiden trenger ikke DB, så uten denne kan innsamlingen dø «usynlig»."""
    if store.ping():
        return PlainTextResponse("ok")
    return PlainTextResponse("db unavailable", status_code=503)


async def tracker(request):
    return Response(
        _TRACKER,
        media_type="application/javascript",
        headers={"cache-control": "public, max-age=86400"},
    )


_MND = ["", "januar", "februar", "mars", "april", "mai", "juni", "juli",
        "august", "september", "oktober", "november", "desember"]


# Under denne grensen (unike besøkende / 7 dager) er ukesvinduet mer anti-proof
# enn proof (12 besøkende + fluktfrekvens over en håndfull sesjoner = støy).
# Da viser heroen i stedet 30-dagers aggregat med sidevisninger — fortsatt EKTE
# tall fra samme kilde, aldri pyntet, bare et ærligere utsnitt for lav trafikk.
_HERO_MIN_WEEK_VISITORS = 100


async def hero_stats(request):
    """Ekte tall til forsidens hero — sporlos.no målt med Sporløs. Ingen pynt:
    rullende vindu (ikke «i dag», som blir 0 på stille dager), samme kilde som /demo.
    7-dagers vindu m/ fluktfrekvens ved nok trafikk; ellers 30 dager m/ sidevisninger
    (fluktfrekvens over få sesjoner er støy, ikke innsikt). `period` styrer etikettene
    i widgeten så visningen aldri påstår et annet vindu enn tallene kommer fra."""
    site = store.resolve_site(os.environ.get("SPORLOS_DEMO_SITE", "6LIACtOSP-S7"))
    if not site:
        return JSONResponse({}, status_code=404)
    days = 7
    stats = store.stats(site["id"], days)
    if stats["visitors"] < _HERO_MIN_WEEK_VISITORS:
        days = 30
        stats = store.stats(site["id"], days)
    series = store.timeseries(site["id"], days)
    frist = ""
    if series:
        d = str(series[0]["bucket"])[:10]
        try:
            frist = f"{int(d[8:10])}. {_MND[int(d[5:7])]}"
        except (ValueError, IndexError):
            frist = d
    payload = {
        "visitors": stats["visitors"],
        "spark": [p["visitors"] for p in series],
        "from": frist,
        "period": days,
    }
    if days == 7:
        payload["bounce"] = stats["bounce_rate"]
    else:
        payload["pageviews"] = stats["pageviews"]
    return JSONResponse(payload, headers={"cache-control": "public, max-age=60"})


# Strukturert data (JSON-LD) for Google: hva Sporløs ER + prisspenn.
_LD_LANDING = (
    '<script type="application/ld+json">'
    + json.dumps(
        {
            "@context": "https://schema.org",
            "@graph": [
                {
                    "@type": "Organization",
                    "name": "Datamynt AS",
                    "url": "https://datamynt.no",
                    "logo": "https://sporlos.no/static/brand/app-ikon.png",
                },
                {
                    "@type": "SoftwareApplication",
                    "name": "Sporløs",
                    "url": "https://sporlos.no",
                    "applicationCategory": "BusinessApplication",
                    "operatingSystem": "Web",
                    "description": "Cookieløs, samtykkefri webanalyse bygget i Norge — "
                    "uten cookie-banner, uten IP-lagring, med data i Norge.",
                    "offers": {
                        "@type": "AggregateOffer",
                        "priceCurrency": "NOK",
                        "lowPrice": "99",
                        "highPrice": "599",
                        "offerCount": "3",
                    },
                },
            ],
        },
        ensure_ascii=False,
    )
    + "</script>"
)

# Spørsmålssiden: HTML og FAQPage-JSON-LD genereres fra SAMME liste — alltid i sync.
# Svarene er ærlige (GA-lovlighet er nyansert, ikke «forbudt») — det er SEO-vinkelen vår.
_FAQ = [
    (
        "Trenger nettstedet mitt cookie-banner?",
        "Bare hvis nettstedet lagrer eller leser noe på besøkerens enhet (ekomloven § 3-15) "
        "eller behandler personopplysninger som krever samtykke. Bruker du verktøy uten cookies "
        "og uten persondata — som Sporløs — utløses ikke kravet, og banneret kan fjernes for "
        "analysens del. Husk at andre verktøy på siden (annonser, embeds) kan kreve banner uansett.",
    ),
    (
        "Er Google Analytics lovlig i Norge?",
        "Det er omdiskutert, og vi skal være ærlige: GA er ikke «forbudt» i Norge. Men GA krever "
        "cookies, og cookies krever samtykke — altså banner. I tillegg har overføringen av data til "
        "USA vært tema hos europeiske datatilsyn i flere år. Med Sporløs slipper du hele diskusjonen: "
        "ingen cookies, ingen persondata, data i Norge.",
    ),
    (
        "Hva er cookieløs webanalyse?",
        "Måling som aldri lagrer noe i besøkerens nettleser. Sporløs teller besøk med en "
        "daglig-roterende engangs-hash som forkastes — ingen kan gjenkjennes på tvers av dager "
        "eller nettsteder. Du får trafikk, kilder, geografi, enheter og konverteringer; du får "
        "ikke sporing av enkeltpersoner. Det er poenget.",
    ),
    (
        "Blir tallene mindre nøyaktige uten cookies?",
        "Mer nøyaktige, faktisk. Verktøy med samtykkebanner mister alle som trykker «avvis» eller "
        "ignorerer banneret — ofte 30–50 % av trafikken. Sporløs måler alle besøk. Forskjellen: "
        "«unike besøkende» betyr unike per dag, ikke per måned, siden vi ikke følger folk over tid.",
    ),
    (
        "Hva koster Sporløs?",
        "Fra 99 kr/mnd (10 000 sidevisninger) til 599 kr/mnd (1 million). 30 dager gratis prøve "
        "uten kort. Vi slutter aldri å måle om du passerer grensen, og sender aldri "
        "overraskelsesregninger — du får et varsel og velger selv om du vil oppgradere.",
    ),
    (
        "Hvordan installerer jeg Sporløs?",
        "Ett script på siden din — eller WordPress-pluginen vår: søk «Sporløs Analytics» i "
        "plugin-katalogen, aktiver, lim inn site-ID. Ferdig. Ingen cookies betyr også: ingen "
        "samtykke-oppsett å konfigurere.",
    ),
    (
        "Hva betyr «verifiserbare tall»?",
        "Hvert dagstall forsegles med en kryptografisk hash som forankres i en uavhengig, offentlig "
        "logg. Endres tallet i ettertid, stemmer ikke seglet. Rapporterer du besøkstall til styre, "
        "annonsører eller tilskuddsgivere, er det dokumentasjon som holder.",
    ),
    (
        "Lagrer dere noe om mine besøkende?",
        "Ingen IP-adresser, ingen cookies, ingen identifikatorer. Kun aggregater: antall besøk, "
        "hvilke sider, hvilket land/fylke, hvilken nettlesertype. Geografisk stopper vi bevisst "
        "på fylkesnivå. Hele tilnærmingen er beskrevet åpent i personvernerklæringen — og "
        "sporingsscriptet er åpen kildekode, så du kan etterprøve selv.",
    ),
]


async def sporsmal(request):
    """SEO-side: spørsmålene folk faktisk googler, med ærlige svar + FAQPage-schema."""
    ld = json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": [
                {
                    "@type": "Question",
                    "name": q,
                    "acceptedAnswer": {"@type": "Answer", "text": a},
                }
                for q, a in _FAQ
            ],
        },
        ensure_ascii=False,
    )
    items = "".join(
        f"<details><summary>{escape(q)}</summary><p>{escape(a)}</p></details>" for q, a in _FAQ
    )
    return HTMLResponse(
        f"""<!doctype html><html lang="no"><head><meta charset="utf-8">
<title>Spørsmål og svar om cookieløs webanalyse | Sporløs</title>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta name="description" content="Trenger du cookie-banner? Er Google Analytics lovlig i Norge? Ærlige svar om cookieløs, samtykkefri webanalyse.">
<link rel="canonical" href="https://sporlos.no/sporsmal">
{_BRAND_HEAD}{_OG_META}
<script type="application/ld+json">{ld}</script>
<style>{_BRAND_CSS}{_CHROME_CSS}
h1{{font-size:2rem;letter-spacing:-.025em;margin:2.2rem 0 .4rem}}
.lede{{color:var(--muted);max-width:46em;margin:0 0 1.8rem}}
details{{background:var(--card);border:1px solid var(--line);border-radius:12px;
padding:1rem 1.25rem;margin:.6rem 0}}
summary{{cursor:pointer;font-weight:600;font-size:1.02rem}}
details p{{color:var(--muted);margin:.7rem 0 .2rem;max-width:60em}}
.cta{{text-align:center;padding:2.4rem 0 1rem}}</style>
{_SELF_SNIPPET}</head><body>
<div class=wrap>
{_SITE_NAV}
<h1>Spørsmål og svar</h1>
<p class=lede>Det folk lurer på om cookieløs webanalyse, samtykkekrav og Sporløs — uten skjønnmaling.</p>
{items}
<div class=cta><a class="btn btn-accent" href="/signup">Prøv Sporløs gratis i 30 dager</a>
<p style="color:var(--muted);font-size:.85rem">uten kort · <a href="/demo">se live-demoen først</a></p></div>
</div>
{_SITE_FOOTER}</body></html>"""
    )


async def assist_js(request):
    if not assist.configured():
        return PlainTextResponse("", status_code=404)
    return Response(
        _ASSIST_JS,
        media_type="application/javascript",
        headers={"cache-control": "public, max-age=3600"},
    )


async def assist_api(request):
    """POST /api/assist {q, history} → {a}. Samtalen lagres aldri — kun teller."""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"a": "Ugyldig forespørsel."}, status_code=400)
    history = data.get("history") if isinstance(data.get("history"), list) else []
    ip = client_ip(request.headers, request.client.host if request.client else "")
    ua = request.headers.get("user-agent", "")
    visitor = visitor_hash(ip, ua, "assist", secret=SECRET)
    # LLM-kallet tar sekunder — av tråden så event-loopen ikke blokkerer ingest.
    ans, status = await asyncio.to_thread(assist.answer, str(data.get("q", "")), history, visitor)
    return JSONResponse({"a": ans}, status_code=status)


async def ingest(request):
    """POST /api/event — fra tracker-snippet. Beregner cookieløs hash, lagrer."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)

    public_id = payload.get("s")
    try:
        site = store.resolve_site(public_id) if public_id else None
    except Exception:
        # DB nede e.l. — ikke la beaconen få 500; vi kan uansett ikke lagre nå.
        log.exception("ingest: resolve_site feilet")
        return PlainTextResponse("", status_code=204)
    if not site:
        return JSONResponse({"error": "unknown site"}, status_code=404)

    ua = request.headers.get("user-agent", "")
    # Bots/scripts telles ikke — aksepter stille (204) men lagre ingenting.
    if is_bot(ua):
        return PlainTextResponse("", status_code=204)

    ip = client_ip(dict(request.headers), fallback=request.client.host or "")
    # Datasenter-trafikk (crawlere m/ vanlig UA) telles heller ikke.
    if is_datacenter(ip):
        return PlainTextResponse("", status_code=204)
    vhash = visitor_hash(ip, ua, str(site["id"]), secret=SECRET)
    device, browser, os_ = parse_ua(ua)
    country, region = geo_lookup(ip)  # land + fylke, by-nivå brukes aldri
    # ip og ua brukes KUN her (hash + kategorisering + geo) — aldri lagret.

    name = payload.get("n", "pageview")
    # E-handel: valgfri ordresum/produktlinjer på egendefinerte hendelser (aldri pageview).
    revenue, currency, items = None, None, []
    if name != "pageview":
        revenue = _clean_money(payload.get("rv"))
        items = _clean_items(payload.get("it"))
        if revenue is not None or items:
            currency = _clean_currency(payload.get("cur"))
            if currency is None:
                # Oppgitt men UGYLDIG valuta: ærlig bortfall av beløpene er bedre
                # enn å blande dem inn i NOK. Hendelsen og produktnavnene beholdes.
                revenue = None
                for it in items:
                    it["unit_price_cents"] = 0
            elif revenue is None and items:
                # Uten ordresum avledes den av linjene — ellers ville produkt-
                # tabellen vist omsetning som ikke fantes i ordre-KPI-ene.
                derived = sum(it["qty"] * it["unit_price_cents"] for it in items)
                if 0 < derived <= _MAX_MONEY:
                    revenue = derived

    try:
        store.insert_event(
            site["id"],
            {
                "name": name,
                "path": payload.get("p", "/"),
                "referrer_src": _normalize_referrer(payload.get("r")),
                "utm_source": _clean_utm(payload.get("us")),
                "utm_medium": _clean_utm(payload.get("um")),
                "utm_campaign": _clean_utm(payload.get("uc")),
                "country": country,
                "region": region,
                "device": device,
                "browser": browser,
                "os": os_,
                "visitor_hash": vhash,
                "revenue_cents": revenue,
                "currency": currency,
            },
            items=items,
        )
    except Exception:
        # Aldri 500 til tracker (den re-sender ikke). Logg så VI ser det.
        log.exception("ingest: insert_event feilet for site %s", site["id"])
    return PlainTextResponse("", status_code=204)


def _clean_utm(v) -> str | None:
    """Kampanjeparameter fra tracker: trim + lengde-cap. Kun hvitlistede nøkler når hit."""
    if not v or not isinstance(v, str):
        return None
    return v.strip()[:120] or None


_MAX_MONEY = 100_000_000  # 1 mill. kr i øre — beløp over dette er åpenbart søppel


def _clean_money(v) -> int | None:
    """Beløp i øre fra tracker: heltall i [0, cap], ellers None. Aldri avvis hendelsen."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if isinstance(v, float) and not v.is_integer():
        return None
    v = int(v)
    return v if 0 <= v <= _MAX_MONEY else None


def _clean_currency(v) -> str | None:
    """ISO 4217-kode. Utelatt/tom → NOK (sitene våre er norske først).
    Oppgitt men UGYLDIG («KR», «€», tall) → None: kallstedet dropper beløpene —
    å tvangskonvertere en oppgitt fremmed valuta til NOK ville blandet valutaer."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return "NOK"
    if isinstance(v, str):
        c = v.strip().upper()
        if len(c) == 3 and c.isalpha() and c.isascii():
            return c
    return None


def _clean_items(raw) -> list[dict]:
    """Produktlinjer fra tracker → validert liste. Ugyldige linjer droppes stille.
    Caps: 25 linjer, navn 160 tegn, qty 1–999, enhetspris [0, cap] øre.
    KUN navn/antall/pris tas imot — ingen sku/id-felt (ubrukte fritekstfelt er
    nøyaktig der ordre-ID-er og kundedata ville havnet; dataminimering)."""
    if not isinstance(raw, list):
        return []
    out = []
    for x in raw[:25]:
        if not isinstance(x, dict):
            continue
        name = x.get("n")
        if not isinstance(name, str) or not name.strip():
            continue
        qty = x.get("q", 1)
        if isinstance(qty, float) and qty.is_integer():
            qty = int(qty)
        if isinstance(qty, bool) or not isinstance(qty, int) or not 1 <= qty <= 999:
            qty = 1
        price = _clean_money(x.get("p", 0))
        out.append(
            {
                "name": name.strip()[:160],
                "qty": qty,
                "unit_price_cents": price if price is not None else 0,
            }
        )
    return out


def _normalize_referrer(ref: str | None) -> str | None:
    """Reduser referrer til ren kilde-host (ingen query/PII)."""
    if not ref:
        return None
    try:
        from urllib.parse import urlparse

        return urlparse(ref).netloc or None
    except Exception:
        return None


# --- Brand: Sporløs designspråk (2026-06-10) ---------------------------------
# Konsept: ø-en i «sporløs» = sirkel med strek = «ingen sporing»-merket.
# Palett: varm papir-bakgrunn, marine blekk, én klar blå aksent. System-fonter
# (ingen Google Fonts — et personvernprodukt lekker ikke besøk til tredjepart).

# «Blekk»-merket (design-runde 2) overalt: solid disk m/ utstanset strek —
# solide flater vinner over strek i små størrelser. Favicon = mini-app-ikon.
_FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<rect width="64" height="64" rx="14" fill="#17263e"/>
<circle cx="32" cy="32" r="22" fill="#2f6fed"/>
<line x1="18.5" y1="49" x2="45.5" y2="15" stroke="#17263e" stroke-width="7" stroke-linecap="round"/>
</svg>"""

# Ordmerket: aksent-disk m/ strek i flatens farge — --mark-gap følger
# konteksten (papir i nav, blekk i mørk footer = utstanset-effekt).
_WORDMARK = (
    '<a class=brand href="/"><svg viewBox="0 0 64 64" aria-hidden=true>'
    '<circle cx="32" cy="32" r="26" fill="currentColor"/>'
    '<line x1="16" y1="52" x2="48" y2="12" stroke="var(--mark-gap,var(--bg))" stroke-width="8" stroke-linecap="round"/>'
    "</svg>sporløs</a>"
)

_BRAND_HEAD = (
    # Modern: skarp SVG. Fallback: ICO (Google) + PNG (crawlere uten SVG). Apple + PWA.
    '<link rel="icon" type="image/svg+xml" href="/favicon.svg">'
    '<link rel="icon" href="/favicon.ico" sizes="48x48">'
    '<link rel="icon" type="image/png" sizes="48x48" href="/static/brand/favicon-48.png">'
    '<link rel="icon" type="image/png" sizes="96x96" href="/static/brand/favicon-96.png">'
    '<link rel="apple-touch-icon" href="/apple-touch-icon.png">'
    '<link rel="manifest" href="/site.webmanifest">'
    '<meta name=theme-color content="#faf9f6">'
)

_BRAND_CSS = """
@font-face{font-family:'Schibsted Grotesk';font-style:normal;font-weight:400 900;
font-display:swap;src:url(/static/schibsted-grotesk.woff2) format('woff2')}
:root{--bg:#faf9f6;--ink:#17263e;--footer:#17263e;--muted:#5f6b7d;--accent:#2f6fed;--accent-deep:#1d4ed8;
--line:#e8e6e0;--card:#ffffff;--ok:#15803d;
--bar:#e9effd;--ok-bg:#ecfdf5;--ok-ink:#065f46;--err:#b91c1c;--err-bg:#fef2f2;
--info:#3730a3;--info-bg:#eef2ff;--warn:#a16207;--warn-bg:#fff7ed;
--btn-bg:#17263e;--btn-bg-h:#0e1a2e;--accent-fill:#2f6fed;--accent-fill-h:#1d4ed8;
font:17px/1.65 'Schibsted Grotesk',system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--ink)}
html{overflow-y:scroll}
body{margin:0;background:var(--bg);-webkit-font-smoothing:antialiased}
body::before{content:'';display:block;height:3px;
background:linear-gradient(90deg,var(--accent-deep),var(--accent) 45%,#8fb3ff)}
a{color:var(--accent-deep)}
.brand{display:inline-flex;align-items:center;gap:.45rem;font-weight:700;font-size:1.15rem;
letter-spacing:-.02em;color:var(--ink);text-decoration:none}
.brand svg{width:1.12em;height:1.12em;color:var(--accent);transform:translateY(-.02em)}
.btn{display:inline-block;background:var(--btn-bg);color:#fff;text-decoration:none;
padding:.7rem 1.4rem;border-radius:9px;font-weight:600;border:0;font-size:1rem;cursor:pointer;
transition:background .15s,transform .15s,box-shadow .15s}
.btn:hover{background:var(--btn-bg-h);transform:translateY(-1px)}
.btn-accent{background:var(--accent-fill);box-shadow:0 8px 20px -10px rgba(47,111,237,.55)}
.btn-accent:hover{background:var(--accent-fill-h)}
.muted{color:var(--muted)}
"""

# Sporløs måler sporlos.no med Sporløs — definert ÉN gang, brukt i alle templates.
_SELF_SNIPPET = (
    '<script defer data-site="6LIACtOSP-S7" data-api="https://sporlos.no/api/event" '
    'src="https://sporlos.no/sporlos.js"></script>'
)
# Assistenten rir på samme injeksjonspunkt — vises kun når LLM-nøkkel er satt.
# ?v= buster 1t-cachen ved widget-endringer — bump ved endring i assist/widget.js.
if assist.configured():
    _SELF_SNIPPET += '<script defer src="/assist.js?v=3"></script>'

# Felles header/footer for alle offentlige sider — samme ramme overalt,
# så ingen side føles som å «dette ut» av nettstedet.
_CHROME_CSS = """
.wrap{max-width:980px;margin:0 auto;padding:0 1.3rem}
nav.site{display:flex;align-items:center;justify-content:space-between;padding:1.4rem 0;gap:.8rem}
nav.site .links{display:flex;gap:1.2rem;align-items:center;font-size:.95rem;flex-wrap:wrap}
nav.site .links a{color:var(--muted);text-decoration:none}
nav.site .links a:hover{color:var(--ink)}
nav.site .links a.btn{color:#fff;padding:.5rem 1rem}
/* Footer = blekk-panel i BEGGE moduser, så --footer holdes mørk og flipper IKKE
   slik --ink gjør i mørk modus (ellers lys-på-lys = usynlig, jf. knapp-fellen). */
footer.site{background:var(--footer);color:#aeb9cb;font-size:.85rem;line-height:1.9;margin-top:4rem}
footer.site .wrap{padding-top:2.6rem;padding-bottom:3rem}
footer.site a{color:#cdd6e4}
footer.site .brand{color:#fff;margin-bottom:.6rem}
footer.site .brand svg{color:var(--accent);--mark-gap:var(--footer)}
@media(max-width:640px){
nav.site{flex-wrap:wrap;row-gap:.6rem;padding:1.1rem 0}
nav.site .links{width:100%;justify-content:flex-start;gap:.55rem 1.1rem;font-size:.9rem}
nav.site .links a.btn{margin-left:auto}
}
"""

_SITE_NAV = (
    "<nav class=site>" + _WORDMARK + '<div class=links>'
    '<a href="/demo">Live demo</a>'
    '<a href="/#priser">Priser</a>'
    '<a href="/google-analytics-alternativ">Mot Google Analytics</a>'
    '<a href="/blogg">Blogg</a>'
    '<a href="/login">Logg inn</a>'
    '<a class="btn btn-accent" href="/signup">Prøv gratis</a></div></nav>'
)

_SITE_FOOTER = (
    "<footer class=site><div class=wrap>" + _WORDMARK + "<br>"
    "Personvernvennlig webanalyse, bygget i Norge.<br><br>"
    '<a href="/demo">Live demo</a> · '
    '<a href="/google-analytics-alternativ">Sporløs mot Google Analytics</a> · '
    '<a href="/integrasjoner">Integrasjoner</a> · '
    '<a href="/sporsmal">Spørsmål og svar</a> · '
    '<a href="/blogg">Blogg</a> · '
    '<a href="https://status.sporlos.no">Status</a> · '
    '<a href="/vilkar">Salgsbetingelser</a> · <a href="/personvern">Personvern</a><br>'
    '<a href="https://datamynt.no">Datamynt AS</a> · org.nr 936 017 207 · '
    "Maridalsveien 163, 0461 Oslo · post@sporlos.no"
    '<hr style="border:0;border-top:1px solid rgba(255,255,255,.13);margin:22px 0 16px">'
    '<div style="text-align:center">'
    '<span style="display:block;font-size:10px;letter-spacing:.16em;text-transform:uppercase;opacity:.55;margin-bottom:7px">En del av</span>'
    '<a href="https://datamynt.no" aria-label="En del av Datamynt" style="display:inline-block">'
    '<img src="/static/datamynt-logo.svg" alt="Datamynt" height="24"></a>'
    '</div>'
    "</div></footer>"
)


async def favicon(request):
    return Response(_FAVICON_SVG, media_type="image/svg+xml",
                    headers={"cache-control": "public, max-age=604800"})


async def favicon_ico(request):
    # Google og eldre nettlesere ber om /favicon.ico ved roten, uavhengig av <link>.
    return Response(_FAVICON_ICO, media_type="image/x-icon",
                    headers={"cache-control": "public, max-age=604800"})


async def apple_icon(request):
    return Response(_APPLE_ICON, media_type="image/png",
                    headers={"cache-control": "public, max-age=604800"})


async def webmanifest(request):
    return Response(_WEBMANIFEST, media_type="application/manifest+json",
                    headers={"cache-control": "public, max-age=604800"})


# Schibsted Grotesk (SIL OFL, norsk) — self-hostet: et personvernprodukt laster
# ikke fonter fra tredjepart. Latin-subset m/ æøå, variabel 400–900, ~46 kB.
_FONT_PATH = Path(__file__).resolve().parent.parent / "static" / "schibsted-grotesk.woff2"
_FONT = _FONT_PATH.read_bytes() if _FONT_PATH.exists() else b""


async def brand_font(request):
    if not _FONT:
        return PlainTextResponse("not found", status_code=404)
    return Response(_FONT, media_type="font/woff2",
                    headers={"cache-control": "public, max-age=2592000, immutable"})


# Delebilde for sosiale medier (1200x630). Regenerer: scripts/make_og.py
_OG_PATH = Path(__file__).resolve().parent.parent / "static" / "og.png"
_OG = _OG_PATH.read_bytes() if _OG_PATH.exists() else b""


async def og_image(request):
    if not _OG:
        return PlainTextResponse("not found", status_code=404)
    return Response(_OG, media_type="image/png",
                    headers={"cache-control": "public, max-age=86400"})


# NB: attributt-verdier MÅ stå i anførselstegn — gyldig HTML5 uten, men LinkedIns
# parser hopper over uquotede property=og:* og viser ingen thumbnail (verifisert
# via Post Inspector 2026-06-12).
_OG_META = (
    '<meta property="og:image" content="https://sporlos.no/static/og.png">'
    '<meta property="og:image:width" content="1200">'
    '<meta property="og:image:height" content="630">'
    '<meta property="og:image:type" content="image/png">'
    '<meta name="twitter:card" content="summary_large_image">'
)


async def landing(request):
    """Offentlig landingsside (§3-15-budskapet)."""
    return HTMLResponse(
        """<!doctype html><html lang="no"><head><meta charset="utf-8">
<title>Sporløs — webanalyse uten cookie-banner</title>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta name="description" content="Cookieløs, samtykke-fri webanalyse bygget i Norge. Ingen cookie-banner. Data på norsk-eid infrastruktur.">
<link rel="canonical" href="https://sporlos.no/">
<meta property="og:title" content="Sporløs — webanalyse uten cookie-banner">
<meta property="og:description" content="Cookieløs, samtykke-fri webanalyse bygget i Norge. Ingen cookie-banner. Data på norsk-eid infrastruktur.">
<meta property="og:type" content="website">
<meta property="og:url" content="https://sporlos.no/">
<meta property="og:locale" content="nb_NO">
"""
        + _BRAND_HEAD
        + _OG_META
        + _LD_LANDING
        + "<style>"
        + _BRAND_CSS
        + _CHROME_CSS
        + """
body{background:radial-gradient(1100px 480px at 78% -120px,rgba(47,111,237,.08),transparent 70%),var(--bg)}
header.hero{display:grid;grid-template-columns:1.15fr .85fr;gap:2.6rem;align-items:center;
padding:3.5rem 0 1.4rem}
@media(max-width:880px){header.hero{grid-template-columns:1fr}}
.tag{display:inline-block;color:var(--accent-deep);font-size:.78rem;font-weight:600;
letter-spacing:.09em;text-transform:uppercase;margin-bottom:1.2rem}
h1{font-size:clamp(2.2rem,5.5vw,3.2rem);line-height:1.06;margin:0 0 1.1rem;
letter-spacing:-.03em;font-weight:800}
.lede{font-size:1.2rem;color:var(--muted);max-width:36em}
.hero-ctas{margin:1.8rem 0 .6rem;display:flex;gap:1rem;align-items:center;flex-wrap:wrap}
.fine{font-size:.85rem;color:var(--muted)}
.live{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:1.2rem 1.3rem;box-shadow:0 18px 50px -28px rgba(23,38,62,.28)}
.live-top{display:flex;justify-content:space-between;align-items:center;font-size:.78rem;
color:var(--muted);margin-bottom:.9rem}
.live-top b{color:var(--ink);font-size:.92rem;letter-spacing:-.01em}
.live-top .na{display:inline-flex;align-items:center;gap:.4rem;white-space:nowrap}
.livedot{width:8px;height:8px;border-radius:50%;background:var(--ok);display:inline-block}
.live-kpis{display:flex;gap:2rem;margin-bottom:.5rem}
.live-kpis b{font-size:2.1rem;font-weight:800;letter-spacing:-.02em;display:block;
font-variant-numeric:tabular-nums;line-height:1.15}
.live-kpis span{font-size:.72rem;color:var(--muted);white-space:nowrap}
.live svg{width:100%;height:70px;display:block}
.demo-axis{display:flex;justify-content:space-between;color:var(--muted);font-size:.68rem;margin:.3rem 0 .6rem}
@media (prefers-reduced-motion:no-preference){
@keyframes pulsdot{0%,100%{opacity:1}50%{opacity:.35}}
.livedot{animation:pulsdot 2.4s ease-in-out infinite}
@keyframes inn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
.inn1{animation:inn .6s .1s both}.inn2{animation:inn .6s .25s both}.inn3{animation:inn .6s .4s both}
/* Scroll-reveal under folden: skjules KUN når observeren faktisk kjører (body.io),
   så uten JS / uten IntersectionObserver vises alt som normalt. */
body.io .reveal{opacity:0}
body.io .reveal.vist{animation:inn .6s both}
}
.lov{display:grid;grid-template-columns:1fr 1.05fr;gap:2.6rem;align-items:center}
@media(max-width:820px){.lov{grid-template-columns:1fr}}
ul.aldri{list-style:none;margin:0;padding:0;display:grid;gap:.55rem}
ul.aldri li{display:flex;gap:.65rem;align-items:flex-start;font-size:.92rem}
ul.aldri svg{flex:none;margin-top:.22rem}
.ark{background:var(--card);border:1px solid var(--line);border-radius:4px;padding:1.8rem 2rem 1.6rem;
box-shadow:0 1px 0 var(--line),0 14px 30px -18px rgba(23,38,62,.25);position:relative}
.ark::before{content:"";position:absolute;inset:10px;border:1px solid var(--line);border-radius:2px;pointer-events:none}
.arkhode{display:flex;justify-content:space-between;font-size:.66rem;font-weight:700;letter-spacing:.12em;
text-transform:uppercase;color:var(--muted);border-bottom:1px solid var(--line);padding-bottom:.7rem;margin-bottom:1rem}
.sitat{font-size:1.06rem;line-height:1.75;margin:0}
.sitat mark{background:none;color:inherit;font-weight:700;
text-decoration:underline;text-decoration-color:var(--accent);text-decoration-thickness:3px;text-underline-offset:4px}
.fri{font-size:.66rem;color:var(--muted);margin-top:.8rem}
.dom{display:flex;align-items:center;gap:.6rem;border-top:1px solid var(--line);margin-top:1.1rem;padding-top:1rem;font-size:.85rem;font-weight:600}
.dom small{display:block;font-weight:400;font-size:.74rem;color:var(--muted)}
.loft{display:flex;align-items:center;gap:1.2rem;border-top:1px solid var(--line);border-bottom:1px solid var(--line);
padding:1.4rem .2rem;margin:1.8rem 0 .4rem}
.loft svg{flex:none}
.loft b{font-size:1.25rem;font-weight:800;letter-spacing:-.025em;line-height:1.25;display:block}
.loft small{color:var(--muted);font-size:.88rem;display:block;margin-top:.2rem}
section{padding:3rem 0;border-bottom:1px solid var(--line)}
h2{font-size:1.9rem;letter-spacing:-.02em;margin:0 0 1.2rem}
.kicker{display:block;margin-bottom:.3rem}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:1rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:1.2rem 1.3rem;
transition:transform .15s,box-shadow .15s}
.card:hover{transform:translateY(-2px);box-shadow:0 14px 30px -18px rgba(23,38,62,.35)}
.kode{background:var(--footer);color:#dbe4f2;border-radius:12px;padding:1.1rem 1.3rem;
overflow-x:auto;font-size:.86rem;line-height:1.6;margin:0}
.kode code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;white-space:pre}
.card h3{margin:0 0 .4rem;font-size:1.02rem}
.card p{margin:0;font-size:.92rem;color:var(--muted)}
.law{max-width:42em}
ul{padding-left:1.2rem;margin:.5rem 0}li{margin:.35rem 0}
.plans{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:1rem;margin:1.4rem 0}
.plan{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:1.2rem 1.3rem;display:flex;flex-direction:column}
.plan.hl{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent)}
.plan b{font-size:1.05rem}.plan .pris{font-size:1.5rem;font-weight:700;margin:.5rem 0 .2rem;letter-spacing:-.02em}
.plan small{color:var(--muted);line-height:1.5}
.plan .hva{margin-top:.4rem;flex:1}
.plan .velg{display:block;text-align:center;margin-top:1rem;padding:.5rem;border-radius:8px;
border:1px solid var(--line);color:var(--ink);text-decoration:none;font-size:.9rem;font-weight:600}
.plan .velg:hover{border-color:var(--accent);color:var(--accent-deep)}
.plan .velg-hl{background:var(--accent);border-color:var(--accent);color:#fff}
.plan .velg-hl:hover{background:var(--accent-deep);color:#fff}
</style>
"""
        + _SELF_SNIPPET
        + "</head><body>"  # eksplisitt head/body — LinkedIn-parseren er pirkete
        + """<div class=wrap>
"""
        + _SITE_NAV
        + """
<header class=hero>
<div>
  <span class="tag inn1">Norsk · cookieløs · samtykkefri</span>
  <h1 class=inn2>Webanalyse uten cookie&#8209;banner.</h1>
  <p class="lede inn3">Sporløs måler nettstedet ditt uten cookies, uten å lagre IP, og uten å samle
  personopplysninger. Tallene til høyre er ekte — denne siden, målt med Sporløs.</p>
  <div class="hero-ctas inn3">
    <a class=btn href="/signup">Start gratis prøve</a>
    <a href="/google-analytics-alternativ" style="font-size:.95rem">Ærlig sammenligning med GA →</a>
  </div>
  <p class="fine inn3">30 dager gratis · uten kort · åpen kildekode</p>
</div>
<div class="live inn3" aria-label="Sporløs-tall for sporlos.no">
  <div class=live-top><b>sporlos.no</b><span class=na><i class=livedot></i><span id=lper>&nbsp;</span></span></div>
  <div class=live-kpis>
    <div><b id=lv>&nbsp;</b><span>unike besøkende</span></div>
    <div><b id=lb>&nbsp;</b><span id=lblab>fluktfrekvens</span></div>
  </div>
  <svg id=lspark viewBox="0 0 340 70" preserveAspectRatio="none" aria-hidden=true></svg>
  <div class=demo-axis><span id=lfrom></span><span>i dag</span></div>
  <p class=fine style="margin:.2rem 0 0;text-align:right"><a href="/demo">Hele dashbordet →</a></p>
</div>
</header>

<section>
  <span class="tag kicker reveal">Hvorfor</span>
  <h2 class=reveal>Hvorfor slipper du banner?</h2>
  <div class="lov reveal">
  <div>
  <p style="color:var(--muted);max-width:40ch;margin:.2rem 0 1.1rem">Samtykkekravet utløses av det
  som skjer på besøkerens enhet. Sporløs rører den aldri.</p>
  <ul class=aldri>
    <li><svg width=17 height=17 viewBox="0 0 64 64"><circle cx=32 cy=32 r=26 fill="var(--accent)"/><line x1=16 y1=52 x2=48 y2=12 stroke="var(--card)" stroke-width=8 stroke-linecap=round/></svg>Setter aldri cookies eller lagrer noe i nettleseren</li>
    <li><svg width=17 height=17 viewBox="0 0 64 64"><circle cx=32 cy=32 r=26 fill="var(--accent)"/><line x1=16 y1=52 x2=48 y2=12 stroke="var(--card)" stroke-width=8 stroke-linecap=round/></svg>Lagrer aldri IP-adresser — flyktig hash, så forkastet</li>
    <li><svg width=17 height=17 viewBox="0 0 64 64"><circle cx=32 cy=32 r=26 fill="var(--accent)"/><line x1=16 y1=52 x2=48 y2=12 stroke="var(--card)" stroke-width=8 stroke-linecap=round/></svg>Fingerprinter aldri, følger aldri på tvers av dager og nettsteder</li>
  </ul>
  </div>
  <div class=ark>
    <div class=arkhode><span>Ekomloven · § 3-15</span><span>i kraft 2025</span></div>
    <p class=sitat>Samtykke kreves for å <mark>lagre eller lese</mark> opplysninger i brukerens
    kommunikasjonsutstyr.</p>
    <p class=fri>Fri gjengivelse — les hele bestemmelsen på lovdata.no</p>
    <div class=dom>
      <svg width=26 height=26 viewBox="0 0 64 64" style="flex:none"><circle cx=32 cy=32 r=26 fill="var(--accent)"/><line x1=16 y1=52 x2=48 y2=12 stroke="var(--card)" stroke-width=8 stroke-linecap=round/></svg>
      <div>Sporløs gjør ingen av delene.<small>Kravet utløses ikke — og uten personopplysninger
      utløses heller ikke GDPR-samtykke.</small></div>
    </div>
  </div>
  </div>
</section>

<section>
  <span class="tag kicker reveal">Funksjoner</span>
  <h2 class=reveal>Alt du faktisk trenger</h2>
  <div class=cards>
    <div class="card reveal"><h3>Hele bildet, ikke et utvalg</h3><p>Uten samtykkekrav måles alle besøk —
    ikke bare de som trykker «godta». Tallene blir mer riktige enn med GA, ikke mindre.</p></div>
    <div class="card reveal"><h3>Mål, funnels og kampanjer</h3><p>Egendefinerte hendelser, konverteringsrate,
    funnels med drop-off og UTM-kampanjer. Uten at noen blir identifisert.</p></div>
    <div class="card reveal"><h3>Data i Norge</h3><p>Norsk-eid drift på servere i Stavanger, utenfor
    rekkevidden til US CLOUD Act. Sporingsscriptet er
    <a href="https://github.com/datamynt/sporlos-tracker">åpen kildekode</a> — etterprøv selv.</p></div>
    <div class="card reveal"><h3>Lett som en fjær</h3><p>Sporingsscriptet er ~1,5 kB komprimert — rundt en
    sekstidel av Google Analytics. Siden din merker det ikke.</p></div>
    <div class="card reveal"><h3>Inngang, utgang og stier</h3><p>Hvor folk lander, hvor de forsvinner og
    hvordan de beveger seg — som aggregat, aldri som enkeltpersoner.</p></div>
    <div class="card reveal"><h3>Verifiserbare tall</h3><p>Dagstallene forsegles i en uavhengig offentlig
    logg, så de kan ikke pyntes i etterkant. Dokumentasjon som holder. (Pro)</p></div>
  </div>
</section>

<section>
  <span class="tag kicker reveal">Kom i gang</span>
  <h2 class=reveal>Én linje, ferdig</h2>
  <p class="muted reveal" style="margin:0 0 1.1rem;max-width:42em">Lim inn før
  <code>&lt;/head&gt;</code> — det er hele installasjonen. Ingen cookies å konfigurere,
  ingen banner å sette opp. Du får din egen site-ID når du registrerer deg.</p>
  <pre class="kode reveal"><code>"""
        + escape(_SNIPPET_TPL)
        + """</code></pre>
  <p class="fine reveal" style="margin-top:.8rem">Bruker du WordPress, Shopify, Wix eller lignende?
  <a href="/integrasjoner">Se lim-inn-guidene →</a></p>
</section>

<section id=priser>
  <span class="tag kicker reveal">Priser</span>
  <h2 class=reveal>Forutsigbare priser</h2>
  <p class=muted style="margin:0">Etter sidevisninger per måned (totale visninger, ikke unike
  besøkende) · eks. mva · årlig = 2 måneder gratis.</p>
  <div class=plans>
    <div class="plan reveal"><b>Liten</b><span class=pris>99 kr<small>/mnd</small></span>
      <small class=hva>10 000 visninger<br>1 nettsted</small>
      <a class=velg href="/signup?plan=liten">Kom i gang</a></div>
    <div class="plan hl reveal"><b>Vekst</b><span class=pris>249 kr<small>/mnd</small></span>
      <small class=hva>100 000 visninger<br>10 nettsteder</small>
      <a class="velg velg-hl" href="/signup?plan=vekst">Kom i gang</a></div>
    <div class="plan reveal"><b>Pro</b><span class=pris>599 kr<small>/mnd</small></span>
      <small class=hva>1 mill. visninger<br>15 nettsteder<br>verifiserbare tall</small>
      <a class=velg href="/signup?plan=pro">Kom i gang</a></div>
    <div class="plan reveal"><b>Byrå</b><span class=pris>fra 1 490 kr</span>
      <small class=hva>fra 25 kundenettsteder<br>forsegling inkludert</small>
      <a class=velg href="mailto:post@sporlos.no?subject=Byr%C3%A5-avtale">Ta kontakt</a></div>
  </div>
  <div class="loft reveal">
    <svg width=44 height=44 viewBox="0 0 64 64"><circle cx=32 cy=32 r=26 fill="var(--accent)"/><line x1=16 y1=52 x2=48 y2=12 stroke="var(--bg)" stroke-width=8 stroke-linecap=round/></svg>
    <div>
      <b>Vi slutter aldri å måle — og sender aldri overraskelsesregninger.</b>
      <small>Over grensen? Du får et varsel og velger selv om du vil oppgradere.</small>
    </div>
  </div>
  <p class=fine>Prøv hostet gratis i 30 dager — uten kort. Vil du ha det helt gratis?
  Sporløs er åpen kildekode — kjør det på egen server. Enterprise/kommune: ta kontakt.</p>
  <a class=btn href="/signup" style="margin-top:.8rem">Start gratis prøve</a>
  <p class=fine style="margin-top:.7rem"><a href="/login">Har du konto? Logg inn</a></p>
</section>

</div>
<script>
(function () {
  var lv = document.getElementById('lv');
  if (!lv) return;
  function fmt(x) { return String(x).replace(/\\B(?=(\\d{3})+(?!\\d))/g, '\\u00a0'); }
  function last(d) {
    lv.textContent = fmt(d.visitors || 0);
    // Serveren velger vindu (7 el. 30 dager) etter trafikkmengde — etikettene følger med.
    document.getElementById('lper').textContent = 'siste ' + (d.period || 7) + ' dager';
    if (d.pageviews != null) {
      document.getElementById('lb').textContent = fmt(d.pageviews);
      document.getElementById('lblab').textContent = 'sidevisninger';
    } else {
      document.getElementById('lb').textContent = (d.bounce || 0) + ' %';
      document.getElementById('lblab').textContent = 'fluktfrekvens';
    }
    document.getElementById('lfrom').textContent = d.from || '';
    var s = d.spark || [];
    if (s.length < 2) return;
    var mx = Math.max.apply(null, s.concat([1]));
    var pts = s.map(function (v, i) {
      return (i * (340 / (s.length - 1))).toFixed(1) + ',' + (64 - (v / mx) * 54 + 3).toFixed(1);
    }).join(' ');
    var svg = document.getElementById('lspark');
    svg.innerHTML = '<polyline fill="none" style="stroke:var(--accent)" stroke-width="2.5" ' +
      'stroke-linecap="round" stroke-linejoin="round" points="' + pts + '"/>';
    var p = svg.querySelector('polyline');
    if (p.getTotalLength && window.matchMedia('(prefers-reduced-motion: no-preference)').matches) {
      var L = p.getTotalLength();
      p.style.strokeDasharray = L; p.style.strokeDashoffset = L;
      p.getBoundingClientRect();
      p.style.transition = 'stroke-dashoffset 1.4s ease-out';
      p.style.strokeDashoffset = '0';
    }
  }
  function hent() {
    fetch('/api/hero').then(function (r) { return r.json(); }).then(last).catch(function () {});
  }
  hent();
  setInterval(hent, 60000);
})();
// Scroll-reveal: gjenbruker `inn`-keyframen på .reveal under folden. Gated på
// prefers-reduced-motion; body.io settes kun her, så uten JS/IO skjules ingenting.
(function () {
  if (!('IntersectionObserver' in window) ||
      !window.matchMedia('(prefers-reduced-motion: no-preference)').matches) return;
  document.body.classList.add('io');
  var io = new IntersectionObserver(function (entries) {
    entries.forEach(function (e) {
      if (e.isIntersecting) { e.target.classList.add('vist'); io.unobserve(e.target); }
    });
  }, { rootMargin: '0px 0px -8% 0px' });
  document.querySelectorAll('.reveal').forEach(function (el) { io.observe(el); });
})();
</script>
"""
        + _SITE_FOOTER
        + "</body></html>"
    )


def _shell(title, inner):
    return HTMLResponse(
        f"""<!doctype html><html lang=no><meta charset=utf-8>
<title>{escape(title)} — Sporløs</title>
<meta name=viewport content="width=device-width, initial-scale=1">
{_BRAND_HEAD}
<style>{_BRAND_CSS}{_CHROME_CSS}
.auth{{font-size:16px;max-width:380px;margin:1.5rem auto 0;padding:0 1rem 3rem}}
h1{{font-size:1.5rem;letter-spacing:-.02em}}
label{{display:block;margin:.8rem 0 .2rem;font-size:.9rem;color:var(--muted)}}
input{{width:100%;padding:.6rem;border:1px solid var(--line);border-radius:8px;font-size:1rem;
box-sizing:border-box;background:var(--card);font:inherit}}
form .btn{{margin-top:1.2rem;width:100%}}
button{{margin-top:1.2rem;width:100%;background:var(--ink);color:#fff;border:0;padding:.7rem;
border-radius:8px;font-size:1rem;cursor:pointer;font:inherit;font-weight:600}}
.err{{background:#fee;color:#900;padding:.6rem;border-radius:8px;font-size:.9rem;margin:.5rem 0}}
.muted{{margin-top:1.2rem;font-size:.85rem}}</style>
{_SELF_SNIPPET}
<div class=wrap>{_SITE_NAV}</div>
<div class=auth>
{inner}
</div>
{_SITE_FOOTER}"""
    )


async def signup(request):
    # ?plan=liten|vekst|pro: bruker valgte plan på forsiden og vil betale med
    # en gang — sendes til /betal etter kontoopprettelse i stedet for trial-/app.
    plan = request.query_params.get("plan", "")
    if plan not in _PLAN_LABELS:
        plan = ""
    if _user(request):
        return RedirectResponse(f"/betal?plan={plan}" if plan else "/app", status_code=302)
    err = ""
    if request.method == "POST":
        f = await request.form()
        company = (f.get("company") or "").strip()
        email = (f.get("email") or "").strip().lower()
        pw = f.get("password") or ""
        plan = f.get("plan") if f.get("plan") in _PLAN_LABELS else ""
        if not company or "@" not in email or len(pw) < 8:
            err = "Fyll inn firma, gyldig e-post og passord (min. 8 tegn)."
        elif store.get_user_by_email(email):
            err = "Det finnes allerede en konto med denne e-posten."
        else:
            try:
                tid, uid = store.create_account(company, email, hash_password(pw))
                request.session["uid"], request.session["tid"] = uid, tid
                try:
                    notify.send_verification(uid, email)
                except Exception:
                    pass
                return RedirectResponse(
                    f"/betal?plan={plan}" if plan else "/app", status_code=302
                )
            except Exception:
                err = "Kunne ikke opprette konto. Prøv igjen."
    chosen = (
        f'<p class=muted>Du har valgt <b>{escape(_PLAN_LABELS[plan])}</b> — betaling rett '
        "etter registrering. Du kan også ombestemme deg og prøve gratis først.</p>"
        if plan
        else "<p class=muted>30 dager gratis · uten kort.</p>"
    )
    eb = f'<div class=err>{escape(err)}</div>' if err else ""
    return _shell(
        "Opprett konto",
        f"""<h1>Opprett konto</h1>{chosen}{eb}
<form method=post>
  <input type=hidden name=plan value="{escape(plan)}">
  <label>Firma</label><input name=company required>
  <label>E-post</label><input name=email type=email required>
  <label>Passord</label><input name=password type=password required minlength=8>
  <button>{"Fortsett til betaling" if plan else "Start gratis prøve"}</button>
</form>
{_sso_buttons()}
<p class=muted>Har du konto? <a href="/login">Logg inn</a></p>""",
    )


async def login(request):
    if _user(request):
        return RedirectResponse("/app", status_code=302)
    err = ""
    if request.method == "POST":
        f = await request.form()
        email = (f.get("email") or "").strip().lower()
        pw = f.get("password") or ""
        u = store.get_user_by_email(email)
        if u and verify_password(pw, u["password_hash"]):
            request.session["uid"], request.session["tid"] = u["id"], u["tenant_id"]
            return RedirectResponse("/app", status_code=302)
        err = "Feil e-post eller passord."
    eb = f'<div class=err>{escape(err)}</div>' if err else ""
    if request.query_params.get("reset"):
        eb += '<p style="color:#0a0;font-size:.9rem">Passordet er oppdatert — logg inn.</p>'
    return _shell(
        "Logg inn",
        f"""<h1>Logg inn</h1>{eb}
<form method=post>
  <label>E-post</label><input name=email type=email required>
  <label>Passord</label><input name=password type=password required>
  <button>Logg inn</button>
</form>
{_sso_buttons()}
<p class=muted>Ny her? <a href="/signup">Opprett konto</a> · <a href="/forgot">Glemt passord?</a></p>""",
    )


async def unsubscribe(request):
    tid = request.query_params.get("tid") or ""
    token = request.query_params.get("t") or ""
    if tid and check_token("unsub", tid, token):
        try:
            store.set_email_optout(int(tid), True)
        except Exception:
            pass
        return _shell(
            "Avmeldt",
            "<h1>Du er avmeldt</h1><p class=muted>Du får ikke flere ukerapporter på e-post. "
            'Vil du ha dem tilbake, kontakt oss på post@sporlos.no.</p>'
            '<p class=muted><a href="/app">Til Sporløs</a></p>',
        )
    return _shell(
        "Ugyldig lenke",
        '<h1>Ugyldig avmeldings-lenke</h1><p class=muted><a href="/">Til forsiden</a></p>',
    )


async def verify_email(request):
    uid = request.query_params.get("uid") or ""
    token = request.query_params.get("t") or ""
    if uid and check_token("verify", uid, token):
        try:
            store.set_email_verified(int(uid))
        except Exception:
            pass
        return _shell(
            "Bekreftet",
            '<h1>E-posten er bekreftet ✓</h1><p class=muted><a href="/app">Til Sporløs</a></p>',
        )
    return _shell(
        "Ugyldig lenke",
        '<h1>Ugyldig bekreftelseslenke</h1><p class=muted><a href="/app">Til Sporløs</a></p>',
    )


async def resend_verify(request):
    u = _user(request)
    if not u:
        return RedirectResponse("/login", status_code=302)
    usr = store.get_user(u["uid"])
    if usr and not usr["email_verified"]:
        try:
            notify.send_verification(usr["id"], usr["email"])
        except Exception:
            pass
    return RedirectResponse("/app?vsent=1", status_code=302)


async def forgot(request):
    if request.method == "POST":
        f = await request.form()
        email = (f.get("email") or "").strip().lower()
        u = store.get_user_by_email(email) if email else None
        # «!»-prefiks = SSO-sentinel (Google/innlogg) — passordet styres hos leverandøren
        if u and not str(u["password_hash"]).startswith("!"):
            token = store.create_reset_token(email)
            link = f"{PUBLIC_BASE}/reset?token={token}"
            mailer.send(
                email,
                "Tilbakestill passordet ditt – Sporløs",
                f"Hei,\n\nKlikk for å velge nytt passord (gyldig i 1 time):\n{link}\n\n"
                "Ba du ikke om dette, kan du se bort fra e-posten.\n\nSporløs",
            )
        # alltid samme svar (ingen e-post-enumerering)
        return _shell(
            "Sjekk e-posten",
            "<h1>Sjekk e-posten din</h1><p class=muted>Hvis det finnes en konto på adressen, "
            "har vi sendt en lenke for å tilbakestille passordet. Lenken er gyldig i én time.</p>"
            '<p class=muted><a href="/login">Tilbake til innlogging</a></p>',
        )
    return _shell(
        "Glemt passord",
        """<h1>Glemt passord</h1>
<p class=muted>Skriv inn e-posten din, så sender vi en lenke for å velge nytt passord.</p>
<form method=post>
  <label>E-post</label><input name=email type=email required>
  <button>Send lenke</button>
</form>
<p class=muted><a href="/login">Tilbake</a></p>""",
    )


async def reset(request):
    token = (request.query_params.get("token") or "")
    if request.method == "POST":
        f = await request.form()
        token = f.get("token") or ""
        pw = f.get("password") or ""
        email = store.pop_reset_token(token) if token else None
        if not email:
            return _shell(
                "Lenke utløpt",
                "<h1>Lenken er ugyldig eller utløpt</h1>"
                '<p class=muted><a href="/forgot">Be om en ny</a></p>',
            )
        if len(pw) < 8:
            new = store.create_reset_token(email)  # ny token, prøv igjen
            return _shell(
                "For kort passord",
                f"""<h1>Velg nytt passord</h1><div class=err>Passordet må være minst 8 tegn.</div>
<form method=post>
  <input type=hidden name=token value="{escape(new)}">
  <label>Nytt passord</label><input name=password type=password required minlength=8>
  <button>Lagre passord</button>
</form>""",
            )
        store.set_password(email, hash_password(pw))
        return RedirectResponse("/login?reset=1", status_code=302)
    return _shell(
        "Velg nytt passord",
        f"""<h1>Velg nytt passord</h1>
<form method=post>
  <input type=hidden name=token value="{escape(token)}">
  <label>Nytt passord</label><input name=password type=password required minlength=8>
  <button>Lagre passord</button>
</form>""",
    )


async def logout(request):
    request.session.clear()
    return RedirectResponse("/", status_code=302)


async def google_login(request):
    if not _HAS_GOOGLE:
        return RedirectResponse("/login", status_code=302)
    return await oauth.google.authorize_redirect(request, f"{PUBLIC_BASE}/auth/google/callback")


async def innlogg_login(request):
    if not _HAS_INNLOGG:
        return RedirectResponse("/login", status_code=302)
    return await oauth.innlogg.authorize_redirect(request, f"{PUBLIC_BASE}/auth/innlogg/callback")


async def _oidc_callback(request, provider: str, sentinel: str):
    """Felles OIDC-retur: verifisert e-post → eksisterende konto eller ny.
    Sentinel-hash => kontoen kan ikke passord-logge (styres hos leverandøren)."""
    client = getattr(oauth, provider, None) if oauth else None
    if client is None:
        return RedirectResponse("/login", status_code=302)
    try:
        token = await client.authorize_access_token(request)
    except Exception:
        return RedirectResponse("/login", status_code=302)
    info = token.get("userinfo") or {}
    email = (info.get("email") or "").strip().lower()
    # Fail-closed: krev POSITIVT verifisert e-post. Kontolinking skjer på e-post
    # alene (ingen provider+sub-binding enda), så en leverandør som asserter en
    # uverifisert adresse ville ellers kunne overta en eksisterende konto.
    if not email or info.get("email_verified") is not True:
        return RedirectResponse("/login", status_code=302)
    u = store.get_user_by_email(email)
    if u:
        request.session["uid"], request.session["tid"] = u["id"], u["tenant_id"]
    else:
        name = info.get("name") or email.split("@")[0]
        tid, uid = store.create_account(name, email, sentinel)
        store.set_email_verified(uid)  # leverandøren har allerede bekreftet e-posten
        request.session["uid"], request.session["tid"] = uid, tid
    return RedirectResponse("/app", status_code=302)


async def google_callback(request):
    return await _oidc_callback(request, "google", "!google-oauth")


async def innlogg_callback(request):
    return await _oidc_callback(request, "innlogg", "!innlogg-oidc")


async def billing_checkout(request):
    """Start Stripe Checkout (abonnement) for valgt plan."""
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    plan = request.query_params.get("plan", "")
    price = STRIPE_PRICES.get(plan)
    if not stripe or not price:
        return RedirectResponse("/app", status_code=302)
    tenant = store.get_tenant(user["tid"]) or {}
    try:
        session = stripe.checkout.Session.create(
            mode="subscription",
            line_items=[{"price": price, "quantity": 1}],
            client_reference_id=str(user["tid"]),
            customer=tenant.get("stripe_customer_id") or None,
            metadata={"tenant_id": str(user["tid"]), "plan": plan},
            subscription_data={"metadata": {"tenant_id": str(user["tid"]), "plan": plan}},
            success_url=f"{PUBLIC_BASE}/app",
            cancel_url=f"{PUBLIC_BASE}/app",
            allow_promotion_codes=True,
        )
    except Exception:
        return RedirectResponse("/app", status_code=302)
    return RedirectResponse(session.url, status_code=303)


SHOPIFY_API_SECRET = os.environ.get("SHOPIFY_API_SECRET", "")


async def shopify_compliance(request):
    """Shopifys påkrevde GDPR-webhooks (customers/data_request, customers/redact, shop/redact).
    Sporløs lagrer INGEN Shopify-kunde-PII (kun anonyme aggregater via pixelen) → ingenting å
    utlevere eller slette. Men endepunktet MÅ verifisere HMAC og svare 200 for App Store-review."""
    body = await request.body()
    sig = request.headers.get("x-shopify-hmac-sha256", "")
    if not SHOPIFY_API_SECRET:
        return PlainTextResponse("not configured", status_code=503)
    digest = base64.b64encode(
        hmac.new(SHOPIFY_API_SECRET.encode(), body, hashlib.sha256).digest()
    ).decode()
    if not hmac.compare_digest(digest, sig):
        return PlainTextResponse("bad hmac", status_code=401)
    log.info("shopify compliance webhook: %s", request.headers.get("x-shopify-topic", "?"))
    return PlainTextResponse("", status_code=200)


async def stripe_webhook(request):
    """Stripe webhook — oppdaterer tenant-plan ved kjøp/oppsigelse. Offentlig (signatur-verifisert)."""
    if not stripe:
        return PlainTextResponse("", status_code=200)
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    try:
        stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)  # verifiser signatur
    except Exception:
        return PlainTextResponse("bad signature", status_code=400)
    # Les feltene fra rå-JSON (vanlig dict) — robust på tvers av stripe-versjoner.
    data = json.loads(payload)
    typ = data.get("type")
    obj = data.get("data", {}).get("object", {})
    if typ == "checkout.session.completed":
        tid = obj.get("client_reference_id")
        plan = (obj.get("metadata") or {}).get("plan")
        if tid and plan:
            store.set_tenant_plan(
                int(tid), plan, customer_id=obj.get("customer"), sub_id=obj.get("subscription")
            )
    elif typ == "customer.subscription.deleted":
        cust = obj.get("customer")
        ten = store.get_tenant_by_customer(cust) if cust else None
        if ten:
            store.set_tenant_plan(ten["id"], "cancelled")
    return PlainTextResponse("ok", status_code=200)


async def billing_portal(request):
    """Redirect til Stripe Customer Portal (administrer/si opp abonnement)."""
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    tenant = store.get_tenant(user["tid"]) or {}
    cust = tenant.get("stripe_customer_id")
    if not stripe or not cust:
        return RedirectResponse("/app", status_code=302)
    try:
        sess = stripe.billing_portal.Session.create(customer=cust, return_url=f"{PUBLIC_BASE}/app")
    except Exception:
        return RedirectResponse("/app", status_code=302)
    return RedirectResponse(sess.url, status_code=303)


async def vipps_start(request):
    """Start Vipps-abonnement: opprett avtale (m/ første måned) og send bruker til Vipps."""
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    plan = request.query_params.get("plan", "")
    if not vipps.configured() or plan not in vipps.PLAN_ORE:
        return RedirectResponse("/app", status_code=302)
    try:
        ag = vipps.create_agreement(plan, PUBLIC_BASE)
    except Exception:
        return RedirectResponse("/app?vipps=feil", status_code=302)
    store.set_vipps_pending(user["tid"], ag["agreementId"], plan)
    return RedirectResponse(ag["vippsConfirmationUrl"], status_code=303)


async def vipps_return(request):
    """Bruker kommer tilbake fra Vipps-appen. Aktivering er ikke garantert ferdig
    ved redirect (Vipps-dokumentert) — poll kort, ellers tar nattlig sweep det."""
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    tenant = store.get_tenant(user["tid"]) or {}
    agid = tenant.get("vipps_agreement_id")
    pending = tenant.get("vipps_pending_plan")
    if not (vipps.configured() and agid and pending):
        return RedirectResponse("/app", status_code=302)
    status = ""
    for _ in range(6):
        try:
            status = vipps.get_agreement(agid).get("status", "")
        except Exception:
            status = ""
        if status in ("ACTIVE", "STOPPED", "EXPIRED"):
            break
        await asyncio.sleep(1)
    if status == "ACTIVE":
        store.activate_vipps(user["tid"], pending, vipps.next_month(date.today()).isoformat())
        return RedirectResponse("/app?vipps=ok", status_code=302)
    if status in ("STOPPED", "EXPIRED"):
        store.clear_vipps(user["tid"])
        return RedirectResponse("/app?vipps=avbrutt", status_code=302)
    return RedirectResponse("/app?vipps=venter", status_code=302)


async def vipps_cancel(request):
    """Stopp Vipps-avtalen. Planen beholdes ut betalt periode — nattlig sweep
    setter cancelled når forfallet passeres."""
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    tenant = store.get_tenant(user["tid"]) or {}
    agid = tenant.get("vipps_agreement_id")
    if not (vipps.configured() and agid):
        return RedirectResponse("/app", status_code=302)
    try:
        vipps.stop_agreement(agid)
    except Exception:
        return RedirectResponse("/app?vipps=feil", status_code=302)
    return RedirectResponse("/app?vipps=stoppet", status_code=302)


async def betal(request):
    """Velg betalingsmåte for valgt plan — landingspunkt for «betal med en gang»-
    flyten fra forsiden. Trial er fortsatt default for de som ikke velger plan."""
    user = _user(request)
    plan = request.query_params.get("plan", "")
    if not user:
        return RedirectResponse(f"/signup?plan={plan}", status_code=302)
    if plan not in _PLAN_LABELS:
        return RedirectResponse("/app", status_code=302)
    knapper = ""
    if stripe and STRIPE_PRICES.get(plan):
        knapper += (
            f'<a href="/billing/checkout?plan={plan}" style="display:block;text-align:center;'
            "background:var(--accent);color:#fff;padding:.75rem;border-radius:9px;"
            'text-decoration:none;font-weight:600;margin:.5rem 0">Betal med kort</a>'
        )
    if vipps.configured():
        knapper += (
            f'<a href="/billing/vipps/start?plan={plan}" style="display:block;text-align:center;'
            "background:#ff5b24;color:#fff;padding:.75rem;border-radius:9px;"
            'text-decoration:none;font-weight:600;margin:.5rem 0">Betal med Vipps</a>'
        )
    if not knapper:
        return RedirectResponse("/app", status_code=302)
    return _shell(
        "Betaling",
        f"""<h1>Nesten i mål</h1>
<p class=muted>Du har valgt <b>{escape(_PLAN_LABELS[plan])}</b>. Velg betalingsmåte —
abonnementet starter med en gang, og du kan si opp når som helst.</p>
{knapper}
<p class=muted style="margin-top:1rem"><a href="/app">Eller start 30 dagers gratis prøve først →</a></p>""",
    )


async def create_site_post(request):
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    # Plan-grense på antall nettsteder (eneste harde grensen — data kastes aldri)
    tenant = store.get_tenant(user["tid"]) or {}
    _, site_lim = _plan_limits(tenant.get("plan") or "trial")
    if _trial_expired(tenant):
        site_lim = 0
    if site_lim is not None and store.monthly_usage(user["tid"])["sites"] >= site_lim:
        return RedirectResponse("/app?limit=sites", status_code=302)
    f = await request.form()
    domain = (f.get("domain") or "").strip().lower()
    # Normaliser: dropp scheme/www/sti — domenet er kun visningsetikett, men stygt
    # input forvirrer (og duplikat skal ikke svelges stille).
    domain = re.sub(r"^https?://", "", domain).split("/")[0].strip()
    if domain.startswith("www."):
        domain = domain[4:]
    if not domain:
        return RedirectResponse("/app?err=domain", status_code=302)
    try:
        site = store.create_site(user["tid"], domain)
    except Exception:
        return RedirectResponse("/app?err=dup", status_code=302)  # f.eks. duplikat under samme konto
    # Send til per-site-dashbordet (ikke lista) — der venter «Steg 2: lim inn koden»-kortet.
    return RedirectResponse(f"/app?site={site['public_id']}", status_code=302)


def _own_site(request, form):
    """Hent site fra form 'site' (public_id) hvis den tilhører innlogget tenant."""
    user = _user(request)
    if not user:
        return None, None
    pid = (form.get("site") or "").strip()
    site = store.resolve_site(pid) if pid else None
    if site and site["tenant_id"] == user["tid"]:
        return site, pid
    return None, pid


async def goal_create(request):
    f = await request.form()
    site, pid = _own_site(request, f)
    if site:
        name = (f.get("name") or "").strip()
        mtype = f.get("match_type") if f.get("match_type") in ("event", "path") else "event"
        mval = (f.get("match_value") or "").strip()
        if name and mval:
            store.create_goal(site["id"], name, mtype, mval)
    return RedirectResponse(f"/app?site={pid}" if pid else "/app", status_code=302)


async def goal_delete(request):
    f = await request.form()
    site, pid = _own_site(request, f)
    if site:
        try:
            store.delete_goal(int(f.get("goal_id") or 0), site["id"])
        except Exception:
            pass
    return RedirectResponse(f"/app?site={pid}" if pid else "/app", status_code=302)


async def funnel_create(request):
    f = await request.form()
    site, pid = _own_site(request, f)
    if site:
        name = (f.get("name") or "").strip()
        steps = []
        for line in (f.get("steps") or "").splitlines():
            line = line.strip()
            if not line:
                continue
            steps.append({"type": "path" if line.startswith("/") else "event", "value": line})
        if name and len(steps) >= 2:
            store.create_funnel(site["id"], name, steps)
    return RedirectResponse(f"/app?site={pid}" if pid else "/app", status_code=302)


async def funnel_delete(request):
    f = await request.form()
    site, pid = _own_site(request, f)
    if site:
        try:
            store.delete_funnel(int(f.get("funnel_id") or 0), site["id"])
        except Exception:
            pass
    return RedirectResponse(f"/app?site={pid}" if pid else "/app", status_code=302)


async def change_password(request):
    """Bytt passord innlogget — krever gammelt passord (selvbetjent, ingen e-postrunde)."""
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    f = await request.form()
    me = store.get_user(user["uid"])
    u = store.get_user_by_email(me["email"]) if me else None
    if not u or not verify_password(f.get("old") or "", u["password_hash"]):
        return RedirectResponse("/app?pw=feil", status_code=302)
    new = f.get("new") or ""
    if len(new) < 8:
        return RedirectResponse("/app?pw=kort", status_code=302)
    store.set_password(me["email"], hash_password(new))
    return RedirectResponse("/app?pw=ok", status_code=302)


async def api_key_create(request):
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    f = await request.form()
    label = ((f.get("label") or "").strip() or "Uten navn")[:60]
    new = store.create_api_key(user["tid"], label)
    request.session["new_api_key"] = new["key"]  # vises én gang på /app
    return RedirectResponse("/app", status_code=302)


async def api_key_revoke(request):
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    f = await request.form()
    try:
        store.revoke_api_key(int(f.get("key_id") or 0), user["tid"])
    except Exception:
        pass
    return RedirectResponse("/app", status_code=302)


async def utviklere(request):
    """API-dokumentasjon — kort nok til å limes inn i en AI-chat i sin helhet."""
    return HTMLResponse(
        f"""<!doctype html><html lang=no><meta charset=utf-8>
<title>API — Sporløs</title>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta name=description content="Read-only Stats-API for AI-verktøy og integrasjoner. Kun aggregater — aldri rådata.">
<link rel="canonical" href="https://sporlos.no/utviklere">
{_BRAND_HEAD}
<style>{_BRAND_CSS}{_CHROME_CSS}
.content{{max-width:680px;margin:0 auto;padding-bottom:1rem}}
h1{{font-size:2rem;letter-spacing:-.02em}}h2{{font-size:1.15rem;margin-top:2rem}}
pre{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:.8rem;overflow-x:auto;font-size:.82rem}}
code{{font-size:.88em}}
table{{border-collapse:collapse;width:100%}}td{{padding:.3rem .5rem;border-bottom:1px solid var(--line);vertical-align:top;font-size:.9rem}}
.muted{{font-size:.85rem;color:var(--muted)}}</style>
{_SELF_SNIPPET}
<div class=wrap>
{_SITE_NAV}
<div class=content>
<h1>API for utviklere og AI-verktøy</h1>
<p>Sporløs har et read-only Stats-API så du kan hente tallene dine inn i rapporter, regneark
og AI-verktøy. Det er trygt å dele en nøkkel med en AI-assistent: API-et serverer kun
<b>aggregater</b> — enkeltpersoner kan ikke slås opp, fordi rådataene ikke finnes
(<a href="/personvern">ingen IP, ingen cookie, daglig-roterende hash</a>).</p>

<h2>Kom i gang</h2>
<p>Lag en nøkkel under «API-tilgang» i <a href="/app">dashbordet</a>, og send den som Bearer-token:</p>
<pre>curl -H "Authorization: Bearer sl_..." \\
  "https://sporlos.no/api/v1/stats?site=DIN_SITE_ID&amp;period=7"</pre>
<p class=muted><code>site</code> er nettstedets public-ID (samme som i sporings-snippeten —
eller hent alle med <code>/api/v1/sites</code>). <code>period</code> er 1, 7 eller 30 dager.</p>

<h2>Endepunkter</h2>
<table>
<tr><td><code>GET /api/v1/sites</code></td><td>nettstedene dine (domene + site-ID)</td></tr>
<tr><td><code>GET /api/v1/stats</code></td><td>KPI-er (unike, visninger, økter, fluktrate) + topplister, med forrige periode til sammenligning</td></tr>
<tr><td><code>GET /api/v1/timeseries</code></td><td>per dag (per time når period=1)</td></tr>
<tr><td><code>GET /api/v1/breakdown</code></td><td>full liste per dimensjon: <code>prop=pages|sources|countries|regions|devices|browsers|os</code> (+ <code>limit</code>, maks 1000)</td></tr>
<tr><td><code>GET /api/v1/goals</code></td><td>mål/konverteringer med rate</td></tr>
<tr><td><code>GET /api/v1/events</code></td><td>egendefinerte hendelser</td></tr>
<tr><td><code>GET /api/v1/ecommerce</code></td><td>e-handel: ordrer + omsetning per valuta, toppprodukter og omsetning per kilde (beløp i øre)</td></tr>
<tr><td><code>GET /api/v1/anchors</code></td><td>forseglede dags-aggregater: sha256-hash + blokkjede-txid — bevis på at historiske tall ikke er endret i etterkant</td></tr>
</table>
<p class=muted>Alle svar er JSON. Land returneres som ISO-koder. Feil gir
<code>{{"error": "..."}}</code> med 400/401/404. Nøkler kan trekkes tilbake når som helst i dashbordet.</p>

<h2>E-handel: send kjøp</h2>
<p>Kall <code>sporlos('purchase', …)</code> fra ordrebekreftelsen, så får du omsetning,
ordrer, snittordre, toppprodukter og omsetning per kilde i dashbordet:</p>
<pre>sporlos('purchase', {{
  revenue: 1198,           // ordresum i kroner
  currency: 'NOK',         // valgfri, NOK er standard
  items: [
    {{ name: 'eSIM Europa 10 GB', qty: 2, price: 599 }}
  ]
}});</pre>
<p class=muted>Kun beløp og produktnavn sendes — vi <b>ber aldri om ordre-ID eller
kundedata</b>, og det finnes ikke felt for dem. Ikke send ordrenummer eller
personaliserte produktnavn (gravering o.l.) i navnefeltet. Fyr kallet én gang per
fullført ordre (typisk gated på en parameter fra betalings-redirecten, ikke på hver
visning av kvitteringssiden). Tallene rapporteres av kundens nettleser og er
veiledende — bruk ordresystemet, ikke analysen, som regnskaps- og avregningsgrunnlag.</p>

<h2>Eksempel: spør en AI om tallene dine</h2>
<p>Lim denne siden + nøkkelen din inn i Claude eller ChatGPT og be den f.eks.
«hent siste 30 dager for nettstedet mitt og forklar hva som driver trafikken».
Verktøy som kan gjøre HTTP-kall trenger ikke mer enn dette.</p>
</div></div>
{_SITE_FOOTER}"""
    )


async def shopify_guide(request):
    """Installasjonsguide for Shopify Custom Pixel (Fase 1) — kopier-og-lim."""
    return HTMLResponse(
        f"""<!doctype html><html lang=no><meta charset=utf-8>
<title>Shopify — Sporløs</title>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta name=description content="Cookieløs, samtykke-fri webanalyse for Shopify — uten cookie-banner. Måler også checkout. Lim inn én egendefinert pixel.">
<link rel="canonical" href="https://sporlos.no/shopify">
<meta property="og:title" content="Sporløs på Shopify — cookieløs analyse uten cookie-banner">
<meta property="og:description" content="Lim inn én egendefinert pixel. Måler også checkout-stegene. Ingen cookies, ingen cookie-banner.">
<meta property="og:type" content="website">
<meta property="og:url" content="https://sporlos.no/shopify">
{_BRAND_HEAD}{_OG_META}
<style>{_BRAND_CSS}{_CHROME_CSS}
.content{{max-width:680px;margin:0 auto;padding-bottom:1rem}}
h1{{font-size:2rem;letter-spacing:-.02em}}h2{{font-size:1.15rem;margin-top:2rem}}
ol{{padding-left:1.2rem}}ol li{{margin:.4rem 0}}
pre{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:.8rem;overflow-x:auto;font-size:.78rem;max-height:340px}}
code{{font-size:.88em}}
table{{border-collapse:collapse;width:100%}}td{{padding:.3rem .5rem;border-bottom:1px solid var(--line);vertical-align:top;font-size:.9rem}}
.muted{{font-size:.85rem;color:var(--muted)}}
.note{{background:var(--info-bg);color:var(--info);border-radius:8px;padding:.7rem .9rem;font-size:.88rem}}</style>
{_SELF_SNIPPET}
<div class=wrap>
{_SITE_NAV}
<div class=content>
<h1>Sporløs på Shopify</h1>
<p>Cookieløs, samtykke-fri webanalyse for Shopify-butikker — <b>uten cookie-banner</b>,
uten å lekke besøkende til tredjepart. Bonus: dette måler også checkout-stegene, som
vanlige tema-snippets ikke får tilgang til (Shopify-checkout ligger på et låst domene).</p>

<h2>Installer på to minutter</h2>
<ol>
<li>Shopify-admin → <b>Innstillinger → Kundehendelser</b>.</li>
<li>Klikk <b>«Legg til egendefinert pixel»</b>, gi den navnet <code>Sporløs</code>.</li>
<li>Kopier <b>hele</b> koden under og lim den inn.</li>
<li>Bytt <code>DITT_SITE_ID_HER</code> med din egen site-ID — finn den i
    <a href="/app">dashbordet</a> under «Vis sporings-kode».</li>
<li>Klikk <b>Lagre</b> → <b>Koble til</b>. Ferdig.</li>
</ol>
<pre>{escape(_SHOPIFY_PIXEL)}</pre>

<h2>Hva som måles</h2>
<table>
<tr><td><code>page_viewed</code></td><td>sidevisninger</td></tr>
<tr><td><code>product_viewed</code></td><td>produktvisning</td></tr>
<tr><td><code>product_added_to_cart</code></td><td>lagt i handlekurv</td></tr>
<tr><td><code>checkout_started</code></td><td>påbegynt checkout</td></tr>
<tr><td><code>checkout_completed</code></td><td>fullført kjøp — med ordresum og produktlinjer:
gir omsetning, snittordre og toppprodukter under «E-handel» i dashbordet</td></tr>
<tr><td><code>search_submitted</code></td><td>butikksøk</td></tr>
</table>
<p class=muted>Ved kjøp sendes kun beløp og produktnavn/antall — vi <b>ber aldri om ordre-ID
eller kundedata</b>, og lagrer ingenting som identifiserer kjøperen. Ingen cookies, ingen
<code>localStorage</code>, ingen fingerprinting. Derfor: ingen cookie-banner for Sporløs.</p>

<div class=note>Tipset gjelder kun <b>app-pixler</b> (ikke denne): Shopifys «Optimized»-modus
struper aldri en egendefinert pixel som denne. Du er trygg.</div>

<p class=muted style="margin-top:1.4rem">Bruker du WordPress i stedet?
<a href="https://wordpress.org/plugins/sporlos-analytics/">Sporløs-pluginen ligger i katalogen</a>.
Annen plattform? Lim inn <a href="/utviklere">sporings-snippeten</a> rett i temaet.</p>
</div></div>
{_SITE_FOOTER}"""
    )


def _legal(title, inner, path="", desc=""):
    canon = f'<link rel="canonical" href="https://sporlos.no{path}">' if path else ""
    meta_desc = f'<meta name="description" content="{escape(desc)}">' if desc else ""
    return HTMLResponse(
        f"""<!doctype html><html lang=no><meta charset=utf-8>
<title>{escape(title)} — Sporløs</title>
<meta name=viewport content="width=device-width, initial-scale=1">
{meta_desc}{canon}
{_BRAND_HEAD}
<style>{_BRAND_CSS}{_CHROME_CSS}
.content{{max-width:680px;margin:0 auto;padding-bottom:1rem}}
h1{{font-size:2rem;letter-spacing:-.02em}}h2{{font-size:1.15rem;margin-top:2rem}}
table{{border-collapse:collapse;width:100%}}td{{padding:.3rem .5rem;border-bottom:1px solid var(--line);vertical-align:top}}
.muted{{font-size:.85rem}}</style>
{_SELF_SNIPPET}
<div class=wrap>
{_SITE_NAV}
<div class=content>
{inner}
<p class=muted style="margin-top:3rem">Datamynt AS · org.nr 936 017 207 · Maridalsveien 163, 0461 Oslo · post@sporlos.no<br>
Sist oppdatert 2026-06-10 · utkast, kvalitetssikres av jurist.</p>
</div></div>
{_SITE_FOOTER}"""
    )


# ── Integrasjonsguider ────────────────────────────────────────────────────
# Verifiserte «lim inn snippet»-steg per plattform (research-workflow 2026-06-15).
# WordPress (plugin) + Shopify (pixel) har egne flater; disse er kodefrie lim-inn.
_SNIPPET_TPL = (
    '<script defer data-site="DITT_SITE_ID" '
    'data-api="https://sporlos.no/api/event" src="https://sporlos.no/sporlos.js"></script>'
)
_GUIDES = {
    "wix": {
        "navn": "Wix",
        "krav": "Krever en betalt Premium-plan med eget domene — verktøyet Custom Code er låst på gratis wixsite.com-adresser.",
        "intro": "Lim inn Sporløs i Wix sitt Custom Code-verktøy (ikke «Header Code» under SEO — det blokkerer script-tagger).",
        "steg": [
            "Åpne nettstedet i Wix-dashbordet (My Sites → velg siten).",
            "Klikk <b>Settings</b> nederst i venstremenyen.",
            "Under <b>Development &amp; integrations</b>, klikk <b>Custom Code</b>.",
            "Klikk <b>+ Add Custom Code</b>, og lim inn koden under.",
            "Velg <b>All pages</b> og plassering <b>Head</b>. Gi den et navn (f.eks. «Sporløs»).",
            "Klikk <b>Apply</b>, så <b>Publish</b> øverst til høyre.",
        ],
        "sjekk": "Åpne det publiserte nettstedet (eget domene, ikke editor-preview) — besøkene dukker opp i dashbordet ditt innen kort tid.",
        "feller": [
            "Ikke bruk «Header Code» under SEO-innstillingene — det avviser script-tagger. Bruk Settings → Custom Code.",
            "Scriptet kjører ikke i Wix-editorens preview — test alltid på den live siden.",
        ],
    },
    "squarespace": {
        "navn": "Squarespace",
        "krav": "Krever Core-plan eller høyere (Code Injection finnes ikke på Basic).",
        "intro": "Lim inn Sporløs i Header-feltet under Code Injection — det legges i &lt;head&gt; på alle sider.",
        "steg": [
            "Logg inn og åpne nettstedet.",
            "Gå til <b>Website → Website Tools → Code Injection</b> (eldre grensesnitt: <b>Settings → Advanced → Code Injection</b>).",
            "Lim inn koden under i <b>Header</b>-feltet (det øverste).",
            "Klikk <b>Save</b>. Koden er live på hele siten umiddelbart.",
        ],
        "sjekk": "Åpne siten i et inkognitovindu, klikk gjennom et par sider, og se besøkene i dashbordet.",
        "feller": [
            "Lim i <b>Header</b>, ikke Footer (Footer laster for sent).",
            "Code Injection lagres ikke automatisk — husk <b>Save</b>.",
        ],
    },
    "webflow": {
        "navn": "Webflow",
        "krav": "Site-wide kode krever et betalt Site-plan (Basic/CMS/Business).",
        "intro": "Lim inn Sporløs i site-wide Head Code — gjelder hele nettstedet.",
        "steg": [
            "Åpne prosjektet → <b>Site settings</b> (tannhjulet).",
            "Klikk fanen <b>Custom code</b>.",
            "Lim inn koden under nederst i <b>Head code</b>-feltet (ikke Footer code).",
            "Klikk <b>Save changes</b>.",
            "Klikk <b>Publish</b> og velg domenet ditt — koden går ikke live før du publiserer.",
        ],
        "sjekk": "Verifiser på ditt eget domene (ikke .webflow.io) — site-wide kode kjører ikke på staging-domenet.",
        "feller": [
            "Endringer vises i Preview, men går aldri live før du trykker <b>Publish</b>.",
            "Gratis/Starter-plan: feltet er låst — du trenger et betalt Site-plan.",
        ],
    },
    "framer": {
        "navn": "Framer",
        "krav": "Custom code finnes på alle planer, men eget domene krever Basic eller høyere.",
        "intro": "Lim inn Sporløs på site-nivå i «End of &lt;head&gt;» — Framer-sider bytter side client-side, så velg å kjøre på hver visning.",
        "steg": [
            "Åpne prosjektet → <b>Site Settings</b> (tannhjulet — ikke Page Settings).",
            "Velg fanen <b>General</b> og bla til <b>Custom Code</b>.",
            "Lim inn koden under i feltet <b>End of &lt;head&gt; tag</b> (klikk «Show Advanced» om du bare ser to felter).",
            "Finnes en kjørings-bryter, velg <b>On Every Page Visit</b>.",
            "Klikk <b>Publish</b> — custom code legges kun på det live nettstedet.",
        ],
        "sjekk": "Test på den live, publiserte URL-en — custom code vises ikke i editor-preview.",
        "feller": [
            "Site Settings, ikke Page Settings (Page gjelder kun én side).",
            "Velg «On Every Page Visit», ellers telles bare første sidelasting.",
        ],
    },
    "ghost": {
        "navn": "Ghost",
        "krav": "Ingen ekstra plan — Code Injection finnes på alle Ghost(Pro)-planer og selvhostet Ghost.",
        "intro": "Lim inn Sporløs i «Site Header» under Code Injection — tema-uavhengig, live umiddelbart.",
        "steg": [
            "Logg inn i Ghost-admin (ditt-domene<b>/ghost</b>) som Owner eller Administrator.",
            "Gå til <b>Settings → Advanced → Code injection</b>.",
            "Lim inn koden under i <b>Site Header</b>-feltet.",
            "Klikk <b>Save</b>. Endringen er live på hele nettstedet.",
        ],
        "sjekk": "Åpne forsiden, «Vis sidekilde» og søk etter «sporlos.js» — den skal ligge i &lt;head&gt;.",
        "feller": [
            "Bruk <b>Site Header</b>, ikke code injection per innlegg (som bare sporer ett innlegg).",
            "Har du CDN/cache foran Ghost, kan det ta noen minutter før snippeten vises.",
        ],
    },
    "gtm": {
        "navn": "Google Tag Manager",
        "krav": "Gratis. Forutsetter at GTM-container-snippeten allerede ligger på nettstedet.",
        "intro": "Legg Sporløs inn som en Custom HTML-tag i GTM, utløst på alle sider.",
        "steg": [
            "Åpne <b>tagmanager.google.com</b> og velg containeren for nettstedet.",
            "Klikk <b>Tags → New</b>, gi taggen navnet «Sporløs».",
            "<b>Tag Configuration → Custom HTML</b>, og lim inn koden under (ikke huk av «Support document.write»).",
            "<b>Triggering → All Pages</b>.",
            "Klikk <b>Save</b>, så <b>Submit → Publish</b> — taggen er ikke live før du publiserer.",
        ],
        "sjekk": "Bruk GTM <b>Preview</b> (Tag Assistant) og bekreft at «Sporløs» står under «Tags Fired» på første sidevisning.",
        "feller": [
            "Ikke live før <b>Submit → Publish</b> — vanligste feil.",
            "Ikke lim Sporløs både i GTM <i>og</i> direkte i &lt;head&gt; — da telles besøk dobbelt.",
        ],
    },
}


def _render_guide(slug):
    g = _GUIDES[slug]
    steg = "".join(f"<li>{s}</li>" for s in g["steg"])
    feller = "".join(f"<li>{f}</li>" for f in g["feller"])
    return HTMLResponse(
        f"""<!doctype html><html lang="no"><head><meta charset="utf-8">
<title>Sporløs på {escape(g['navn'])} — installasjonsguide</title>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta name="description" content="Slik installerer du Sporløs cookieløs webanalyse på {escape(g['navn'])} — uten cookie-banner. {escape(g['krav'])}">
<link rel="canonical" href="https://sporlos.no/integrasjoner/{slug}">
{_BRAND_HEAD}{_OG_META}
<style>{_BRAND_CSS}{_CHROME_CSS}
.content{{max-width:680px;margin:0 auto;padding-bottom:1rem}}
h1{{font-size:2rem;letter-spacing:-.02em}}h2{{font-size:1.15rem;margin-top:2rem}}
ol{{padding-left:1.2rem}}ol li{{margin:.45rem 0}}ul{{padding-left:1.2rem}}ul li{{margin:.3rem 0;color:var(--muted);font-size:.92rem}}
pre{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:.8rem;overflow-x:auto;font-size:.78rem}}
.muted{{font-size:.85rem;color:var(--muted)}}
.note{{background:var(--info-bg);color:var(--info);border-radius:8px;padding:.7rem .9rem;font-size:.88rem;margin:1rem 0}}</style>
{_SELF_SNIPPET}</head><body>
<div class=wrap>
{_SITE_NAV}
<div class=content>
<p class=muted style="margin:0"><a href="/integrasjoner">← Alle integrasjoner</a></p>
<h1>Sporløs på {escape(g['navn'])}</h1>
<p>{g['intro']}</p>
<div class=note>{escape(g['krav'])}</div>
<h2>Slik gjør du det</h2>
<ol>{steg}</ol>
<p class=muted>Lim inn denne — bytt <code>DITT_SITE_ID</code> med din egen ID fra
<a href="/app">dashbordet</a> (under «Vis sporings-kode»):</p>
<pre>{escape(_SNIPPET_TPL)}</pre>
<h2>Sjekk at det virker</h2>
<p>{g['sjekk']}</p>
<h2>Verdt å vite</h2>
<ul>{feller}</ul>
<p class=muted style="margin-top:1.4rem">Står du fast? Send oss en e-post på
<a href="mailto:post@sporlos.no">post@sporlos.no</a> — vi hjelper deg i gang.</p>
</div></div>
{_SITE_FOOTER}</body></html>"""
    )


async def platform_guide(request):
    slug = request.path_params.get("slug", "")
    if slug not in _GUIDES:
        return RedirectResponse("/integrasjoner", status_code=302)
    return _render_guide(slug)


async def integrasjoner(request):
    """Hub: alle plattformer Sporløs fungerer med — tier-et ærlig (plugin/app/guide)."""
    guide_kort = "".join(
        f'<a class=intk href="/integrasjoner/{slug}"><b>{escape(g["navn"])}</b>'
        f'<span>Lim-inn-guide</span></a>'
        for slug, g in _GUIDES.items()
    )
    return HTMLResponse(
        f"""<!doctype html><html lang="no"><head><meta charset="utf-8">
<title>Integrasjoner — Sporløs fungerer med plattformen din</title>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta name="description" content="Sporløs cookieløs webanalyse fungerer med WordPress, Shopify, Wix, Squarespace, Webflow, Framer, Ghost og Google Tag Manager — eller hvilken som helst side der du kan lime inn en kodesnutt.">
<link rel="canonical" href="https://sporlos.no/integrasjoner">
{_BRAND_HEAD}{_OG_META}
<style>{_BRAND_CSS}{_CHROME_CSS}
.content{{max-width:760px;margin:0 auto;padding-bottom:1rem}}
h1{{font-size:2.1rem;letter-spacing:-.025em}}h2{{font-size:1.05rem;margin:2rem 0 .8rem;color:var(--muted)}}
.lede{{font-size:1.15rem;color:var(--muted);max-width:42em}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:.7rem}}
.intk{{display:flex;flex-direction:column;gap:.2rem;border:1px solid var(--line);border-radius:12px;
padding:1rem 1.1rem;text-decoration:none;background:var(--card);transition:border-color .2s}}
.intk:hover{{border-color:var(--accent)}}
.intk b{{color:var(--ink);font-size:1rem}}.intk span{{color:var(--muted);font-size:.8rem}}
.intk.dedikert span{{color:var(--accent-deep)}}
.cta{{border:1px solid var(--line);border-radius:14px;padding:1.6rem;margin-top:2rem;background:var(--card)}}
.cta b{{font-size:1.1rem}}</style>
{_SELF_SNIPPET}</head><body>
<div class=wrap>
{_SITE_NAV}
<div class=content>
<h1>Fungerer med plattformen din</h1>
<p class=lede>Sporløs er én liten kodesnutt — den virker på alt som lar deg legge til kode i
&lt;head&gt;. For de vanligste plattformene har vi laget ferdige guider.</p>

<h2>Dedikert plugin / app</h2>
<div class=grid>
  <a class="intk dedikert" href="https://wordpress.org/plugins/sporlos-analytics/"><b>WordPress</b><span>Offisiell plugin →</span></a>
  <a class="intk dedikert" href="/shopify"><b>Shopify</b><span>Pixel — måler også checkout →</span></a>
</div>

<h2>Kodefrie lim-inn-guider</h2>
<div class=grid>{guide_kort}</div>

<h2>Alt annet</h2>
<p class=muted>Egen nettside eller et rammeverk? Lim
<a href="/utviklere">sporings-snippeten</a> rett inn i &lt;head&gt; — det er alt som skal til.</p>

<div class=cta>
<b>Mangler integrasjonen du trenger?</b>
<p class=muted style="margin:.4rem 0 0">Si fra på <a href="mailto:post@sporlos.no?subject=Integrasjon">post@sporlos.no</a>
— trenger du en integrasjon vi ikke har ennå, fikser vi det.</p>
</div>
</div></div>
{_SITE_FOOTER}</body></html>"""
    )


async def vilkar(request):
    return _legal(
        "Salgsbetingelser",
        path="/vilkar",
        desc="Salgsbetingelser for webanalysetjenesten Sporløs — utformet etter Forbrukertilsynets anbefalinger.",
        inner="""<h1>Salgsbetingelser</h1>
<p>Disse salgsbetingelsene gjelder kjøp av abonnement på webanalysetjenesten Sporløs, og er utformet
etter Forbrukertilsynets anbefalinger for forbrukerkjøp over internett. Tjenesten selges også til
næringsdrivende; enkelte forbrukerrettigheter (f.eks. angrerett) gjelder kun forbrukere.</p>

<h2>1. Selger (avtalepart)</h2>
<p><b>Datamynt AS</b>, org.nr 936 017 207<br>Maridalsveien 163, 0461 Oslo<br>
E-post: <b>post@sporlos.no</b> · Telefon: +47 48 27 99 19</p>

<h2>2. Tjenesten og priser</h2>
<p>Sporløs er personvernvennlig webanalyse. Planer og priser fremgår av <a href="/">sporlos.no</a>,
oppgitt i NOK. (Datamynt er foreløpig ikke mva-registrert; mva tilkommer fra registreringstidspunktet.)</p>

<h2>3. Avtaleinngåelse</h2>
<p>Avtalen er bindende når bestillingen er sendt og bekreftet. Du må være myndig for å inngå avtale.</p>

<h2>4. Betaling</h2>
<p>Betaling skjer med Vipps eller betalingskort, forskuddsvis per betalingsperiode. Næringsdrivende
kan etter avtale betale mot faktura/EHF (post@sporlos.no).</p>

<h2>5. Levering</h2>
<p>Tjenesten gjøres tilgjengelig umiddelbart etter at avtalen er inngått.</p>

<h2>6. Løpetid, fornyelse og oppsigelse</h2>
<p><b>Ingen bindingstid.</b> Abonnementet løper fortløpende og fornyes automatisk for en ny periode
(måned eller år) til gjeldende pris inntil det sies opp. <b>Du kan si opp når som helst</b>, med
virkning fra utløpet av inneværende betalte periode.</p>
<p><b>Slik sier du opp:</b> betaler du med Vipps, kan du se og avslutte den faste avtalen direkte i
Vipps-appen. Ellers avslutter du i tjenesten eller ved å kontakte oss på <b>post@sporlos.no</b>.
Allerede betalt periode refunderes ikke, men du belastes ikke videre.</p>
<p><b>Prisendringer</b> varsles på e-post minst 30 dager før de trer i kraft, og gjelder først fra
neste betalingsperiode. Er du uenig, kan du si opp før endringen trer i kraft.</p>

<h2>7. Angrerett (forbrukere)</h2>
<p>Som forbruker har du 14 dagers angrerett etter angrerettloven. For digitale tjenester som leveres
umiddelbart ber vi om ditt uttrykkelige samtykke til at leveringen starter før angrefristen utløper;
du erkjenner da at angreretten bortfaller når tjenesten er levert. Den gratis prøveperioden lar deg
uansett teste kostnadsfritt før kjøp. (Angrerett gjelder ikke ved salg til næringsdrivende.)</p>

<h2>8. Prøveperiode</h2>
<p>Nye kunder får 30 dager gratis, uten betalingskort og uten bindingstid. Prøveperioden går ikke
over til betalt abonnement uten at du aktivt velger en plan.</p>

<h2>9. Reklamasjon</h2>
<p>Ved feil eller mangel, kontakt oss på post@sporlos.no. Forbrukere har rettigheter etter
forbrukerkjøpsloven.</p>

<h2>10. Behandling av data — og dine data</h2>
<p>Sporløs samler ikke personopplysninger om dine besøkende. Se <a href="/personvern">personvernerklæringen</a>;
for næringsdrivende gjelder i tillegg databehandleravtale (på forespørsel).</p>
<p><b>Analysedataene for ditt nettsted er dine.</b> Du kan når som helst eksportere dem (CSV i
tjenesten). Vi selger eller deler dem aldri med tredjepart. Ved opphør av kundeforholdet slettes
innsamlede analysedata innen 90 dager.</p>

<h2>11. Tilgjengelighet og ansvar</h2>
<p>Vi tilstreber høy oppetid og tar jevnlige sikkerhetskopier. Planlagt vedlikehold som påvirker
tjenesten varsles. Hvis sporingsscriptet er utilgjengelig, påvirkes ikke nettstedet ditt —
scriptet feiler stille uten å forstyrre siden.</p>
<p>Tjenesten leveres "som den er". For næringsdrivende er vårt samlede ansvar begrenset til vederlag
betalt siste 12 måneder; forbrukeres ufravikelige rettigheter berøres ikke.</p>

<h2>12. Endringer i vilkårene</h2>
<p>Vesentlige endringer i disse vilkårene varsles på e-post i rimelig tid før de trer i kraft.
Fortsatt bruk etter varslet ikrafttredelse regnes som aksept; du kan alltid si opp i stedet.</p>

<h2>13. Klage og konfliktløsning</h2>
<p>Ta først kontakt med oss på post@sporlos.no. Forbrukere kan klage til Forbrukertilsynet/Forbrukerrådet.
Avtalen reguleres av norsk rett.</p>""",
    )


async def personvern(request):
    return _legal(
        "Personvernerklæring",
        path="/personvern",
        desc="Slik behandler Sporløs personopplysninger: ingen IP-lagring, ingen cookies, kun daglig-roterende hash.",
        inner="""<h1>Personvernerklæring</h1>
<p>Denne erklæringen beskriver hvordan Datamynt AS behandler personopplysninger som
behandlingsansvarlig for kunder og besøkende på sporlos.no.</p>

<h2>1. Hva vi samler om kunder</h2>
<p>Når du oppretter konto lagrer vi e-post, firmanavn og et kryptert passord (eller pålogging via
Google). Faktureringsopplysninger håndteres av vår betalingspartner (Stripe/Vipps); vi lagrer ikke
kortnummer.</p>
<p>Når du logger inn, settes én <b>nødvendig innloggings-cookie</b> (sesjon). Den brukes kun til å
holde deg innlogget, deles ikke med noen, og er unntatt samtykkekravet (strengt nødvendig).
Den er det eneste vi noensinne lagrer i nettleseren din — og kun for innloggede kunder.</p>

<h2>2. Besøkende på sporlos.no</h2>
<p>Vi måler vårt eget nettsted med Sporløs — cookieløst, uten å lagre IP og uten
personopplysninger. Derfor settes ingen sporings-cookies og det kreves ikke samtykke.
Vi bruker ingen tredjeparts sporings- eller analyseverktøy, og laster ingen ressurser
(fonter, scripts) fra tredjepart på offentlige sider.</p>
<p><b>Nettside-assistenten (chat):</b> Hvis du velger å bruke chatten, behandles meldingene
dine av vår KI-tjeneste for å generere svar. Samtalen lagres ikke hos oss og kobles ikke
til deg — vi teller kun antall meldinger (anonymt, slettes daglig) for å hindre misbruk.
Ikke del personopplysninger i chatten; den trenger dem ikke for å hjelpe deg.</p>

<h2>3. Formål og grunnlag</h2>
<p>Vi behandler kontoopplysninger for å levere og fakturere tjenesten (avtale, personvern­forordningen
art. 6 nr. 1 b) og for support. Vi sender ikke markedsføring uten samtykke.</p>

<h2>4. Databehandlere og lagring</h2>
<table>
<tr><td><b>UpCloud</b></td><td>Hosting — servere i Stavanger, Norge (EU-eid)</td></tr>
<tr><td><b>Stripe / Vipps</b></td><td>Betaling</td></tr>
<tr><td><b>Google Workspace</b></td><td>E-post (support og transaksjonsmeldinger til kunder)</td></tr>
</table>
<p><b>Lagringstider:</b> Kontoopplysninger lagres så lenge du er kunde, og slettes innen rimelig
tid etter at kundeforholdet opphører (regnskapsplikt kan kreve lengre lagring av fakturadata).
Analysehendelser inneholder ingen personopplysninger og lagres for statistikkformål;
ved opphør slettes de innen 90 dager.</p>

<h2>5. Dine rettigheter</h2>
<p>Du har rett til innsyn, retting, sletting og dataportabilitet. Kontakt oss på
post@sporlos.no. Du kan klage til Datatilsynet (datatilsynet.no).</p>

<h2>6. Analyse på vegne av kunder</h2>
<p>Når du bruker Sporløs på ditt eget nettsted, er du behandlingsansvarlig og vi er
databehandler. Da gjelder databehandleravtalen, ikke denne erklæringen.</p>""",
    )


async def ga_alternativ(request):
    """Ærlig sammenligning mot Google Analytics. Content/SEO-side, offentlig."""
    return HTMLResponse(
        """<!doctype html><html lang="no"><head><meta charset="utf-8">
<title>Norsk alternativ til Google Analytics — ærlig sammenligning | Sporløs</title>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta name="description" content="Hva mister du og hva får du ved å bytte fra Google Analytics til Sporløs? Ærlig sammenligning: cookie-banner, datakvalitet, Google Ads, SEO og pris.">
<link rel="canonical" href="https://sporlos.no/google-analytics-alternativ">
<meta property="og:title" content="Norsk alternativ til Google Analytics — ærlig sammenligning">
<meta property="og:description" content="Hva mister du og hva får du ved å bytte fra Google Analytics? Ærlig sammenligning uten skjønnmaling.">
<meta property="og:type" content="article">
<meta property="og:url" content="https://sporlos.no/google-analytics-alternativ">
<meta property="og:locale" content="nb_NO">
"""
        + _BRAND_HEAD
        + _OG_META
        + "<style>"
        + _BRAND_CSS
        + _CHROME_CSS
        + """
.content{max-width:680px;margin:0 auto}
header{padding:2.5rem 0 2rem}
h1{font-size:2.1rem;line-height:1.15;margin:0 0 1rem;letter-spacing:-.02em}
.lede{font-size:1.15rem;color:var(--muted)}
section{padding:1.6rem 0;border-top:1px solid var(--line)}
h2{font-size:1.25rem;margin:0 0 .6rem}
ul{padding-left:1.2rem;margin:.5rem 0}li{margin:.35rem 0}
table{border-collapse:collapse;width:100%;font-size:.95rem;margin:1rem 0}
td,th{padding:.5rem .6rem;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}
th{font-size:.85rem;color:var(--muted);font-weight:600}
.ja{color:#15803d}.nei{color:#b91c1c}.delvis{color:#a16207}
.cta{display:inline-block;background:var(--ink);color:#fff;text-decoration:none;padding:.7rem 1.3rem;border-radius:8px;margin-top:1rem}
.fine{font-size:.85rem;color:var(--muted)}
</style>
"""
        + _SELF_SNIPPET
        + "</head><body>"  # eksplisitt head/body — LinkedIn-parseren er pirkete
        + """<div class=wrap>
"""
        + _SITE_NAV
        + """<div class=content>
<header>
  <h1>Bytte fra Google Analytics? Her er den ærlige sammenligningen.</h1>
  <p class=lede>Sporløs er ikke en kopi av Google Analytics, og later ikke som. Her er hva du faktisk
  mister, hva du får — og hva du tror du mister, men ikke gjør.</p>
</header>

<section>
  <h2>Det viktigste først: cookie-banneret</h2>
  <p>Google Analytics krever samtykke, altså banner. Sporløs setter ingen cookies, lagrer ingenting i
  nettleseren og samler ingen personopplysninger — da utløses verken samtykkekravet i ekomloven § 3-15
  eller GDPR. <b>Banneret kan rett og slett fjernes.</b></p>
  <p>Det gir en roligere og raskere side, og et førsteinntrykk uten juridisk støy. Og det har en
  målbar bieffekt folk undervurderer:</p>
  <p><b>Tallene dine blir mer riktige, ikke mindre.</b> GA måler bare de som trykker «godta» og slipper
  gjennom annonseblokkere — en stor andel gjør ikke det, og hullene fylles delvis med modellerte
  estimater. Sporløs trenger ikke samtykke og måler dermed alle besøk, som faktiske tall.</p>
</section>

<section>
  <h2>Dette mister du — ærlig talt</h2>
  <ul>
    <li><b>Google Ads-integrasjonen.</b> Konverteringsimport, remarketing-målgrupper og smart
    bidding-signaler finnes ikke hos oss. Kjører du tung Google Ads-annonsering, bør du beholde GA
    ved siden av (eller koble Ads-konvertering direkte i Ads).</li>
    <li><b>Demografi og interesser.</b> GA gjetter alder/interesser via Googles annonsenettverk.
    Sporløs vet ikke hvem folk er — det er hele poenget.</li>
    <li><b>Bruker-nivå analyse.</b> Utforskninger, segmenter på enkeltbrukere, reiser på tvers av
    enheter og dager, BigQuery-eksport. Sporløs viser aggregater, aldri enkeltpersoner.</li>
    <li><b>Avansert kampanjeattribusjon.</b> UTM-kampanjer (kilde/medium/kampanje) måles, men
    fler-stegs attribusjonsmodeller og <code>utm_content</code>/<code>utm_term</code> finnes ikke ennå.</li>
    <li><b>Prisen.</b> GA er gratis. Sporløs koster fra 99 kr/mnd — eller null, hvis du kjører
    åpen kildekode-versjonen på egen server. Du betaler for at <i>du</i> er kunden, ikke produktet.</li>
  </ul>
</section>

<section>
  <h2>Dette tror mange at de mister — men ikke gjør</h2>
  <ul>
    <li><b>SEO- og søkeordsdata.</b> Den kommer fra Google Search Console, ikke fra Analytics — og
    Search Console beholder du uansett. Sporløs viser hva folk gjør på siden; Search Console viser
    hvordan de fant den. Komplementært.</li>
    <li><b>Mål og konvertering.</b> Egendefinerte hendelser, mål med konverteringsrate og funnels med
    drop-off finnes i Sporløs.</li>
    <li><b>E-handel på produktnivå.</b> Omsetning, ordrer, snittordre, toppprodukter og omsetning
    per kilde — med ett <code>purchase</code>-kall fra ordrebekreftelsen. Forskjellen fra GA: vi
    ber aldri om ordre-ID eller kundedata, og lagrer ingenting som identifiserer kjøperen.</li>
    <li><b>Kampanjemåling.</b> UTM-merkede lenker (kilde, medium, kampanje) måles — uten at hele
    URL-en med potensielt personidentifiserende parametre noensinne lagres.</li>
    <li><b>Kilder, enheter, geografi.</b> Hvor trafikken kommer fra, mobil/desktop, nettleser og
    fylke — uten å identifisere noen.</li>
    <li><b>Inngangs- og utgangssider, navigasjonsstier.</b> Hvor folk lander, hvor de forsvinner og
    hvordan de beveger seg.</li>
  </ul>
</section>

<section>
  <h2>Side om side</h2>
  <table>
    <tr><th></th><th>Google Analytics</th><th>Sporløs</th></tr>
    <tr><td>Cookie-banner nødvendig</td><td class=nei>Ja</td><td class=ja>Nei</td></tr>
    <tr><td>Måler besøkende uten samtykke</td><td class=delvis>Delvis (modellert)</td><td class=ja>Alle, faktiske tall</td></tr>
    <tr><td>Scriptvekt</td><td class=nei>~90 kB+</td><td class=ja>~1,5 kB komprimert</td></tr>
    <tr><td>Datalagring</td><td class=nei>Google (USA-tilknyttet)</td><td class=ja>Norge, norsk-eid drift</td></tr>
    <tr><td>Google Ads-integrasjon</td><td class=ja>Ja</td><td class=nei>Nei</td></tr>
    <tr><td>Bruker-/segmentanalyse, BigQuery</td><td class=ja>Ja</td><td class=nei>Nei (kun aggregater)</td></tr>
    <tr><td>Mål, funnels, kilder, enheter</td><td class=ja>Ja</td><td class=ja>Ja</td></tr>
    <tr><td>E-handel (omsetning, produkter)</td><td class=ja>Ja</td><td class=ja>Ja (uten ordre-ID/kundedata)</td></tr>
    <tr><td>Åpen kildekode / self-host</td><td class=nei>Nei</td><td class=ja>Ja</td></tr>
    <tr><td>Etterprøvbare, forseglede tall</td><td class=nei>Nei</td><td class=ja>Ja (Pro)</td></tr>
    <tr><td>Pris</td><td class=ja>Gratis</td><td class=delvis>Fra 99 kr/mnd · self-host gratis</td></tr>
  </table>
  <p class=muted>Etterprøvbare tall: dagstallene forsegles kryptografisk i en uavhengig offentlig
  logg, så de kan ikke pyntes på i etterkant. Nyttig når tall skal dokumenteres overfor kunder
  eller annonsører.</p>
</section>

<section>
  <h2>Trygg overgang: kjør begge en periode</h2>
  <p>Vanligste vei: legg inn Sporløs ved siden av GA, sammenlign tallene noen uker, og fjern GA (og
  banneret) når du er trygg. Husk bare at banneret må stå så lenge GA er på siden.</p>
  <a class=cta href="/signup">Prøv gratis i 30 dager</a>
  <p class=muted style="margin-top:.8rem">Uten kort. <a href="/">Les mer om Sporløs →</a></p>
</section>
</div></div>
"""
        + _SITE_FOOTER
        + "</body></html>"
    )


# ---------- Blogg — innhold bor i app/blogg.py, rendering her (jf. _GUIDES) ----------

_BLOGG_LEDE = "Om sporing, personvern og ærlig måling — fra folkene bak Sporløs."
_BLOGG_RSS_LINK = (
    '<link rel="alternate" type="application/rss+xml" title="Sporløs-bloggen" '
    'href="/blogg/rss.xml">'
)
_BLOGG_CSS = """
.content{max-width:680px;margin:0 auto;padding-bottom:1rem}
h1{font-size:1.9rem;letter-spacing:-.02em;line-height:1.25}
h2{font-size:1.2rem;margin-top:2.2rem}
.dato{font-size:.85rem;color:var(--muted)}
.lede{font-size:1.12rem;color:var(--muted)}
blockquote{margin:1.4rem 0;padding:.2rem 0 .2rem 1.1rem;border-left:3px solid var(--accent);
font-size:1.05rem}
.content ul{padding-left:1.2rem}.content ul li{margin:.3rem 0}
.muted{font-size:.9rem;color:var(--muted)}
"""


def _blogg_norsk_dato(iso: str) -> str:
    return f"{int(iso[8:10])}. {_MND[int(iso[5:7])]} {iso[:4]}"


# RFC 822-datoer for RSS — egne navnelister så output aldri avhenger av locale.
_RSS_DAG = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_RSS_MND = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun",
            "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _blogg_rss_dato(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{_RSS_DAG[d.weekday()]}, {d.day:02d} {_RSS_MND[d.month]} {d.year} 08:00:00 +0200"


def _render_blogg_post(slug):
    p = blogg.POSTS[slug]
    url = f"https://sporlos.no/blogg/{slug}"
    ld = (
        '<script type="application/ld+json">'
        + json.dumps(
            {
                "@context": "https://schema.org",
                "@type": "BlogPosting",
                "headline": p["tittel"],
                "description": p["beskrivelse"],
                "datePublished": p["dato"],
                "url": url,
                "inLanguage": "nb",
                "author": {"@type": "Organization", "name": "Sporløs", "url": "https://sporlos.no"},
                "publisher": {
                    "@type": "Organization",
                    "name": "Datamynt AS",
                    "logo": {
                        "@type": "ImageObject",
                        "url": "https://sporlos.no/static/brand/app-ikon.png",
                    },
                },
            },
            ensure_ascii=False,
        )
        + "</script>"
    )
    return HTMLResponse(
        f"""<!doctype html><html lang="no"><head><meta charset="utf-8">
<title>{escape(p['tittel'])} — Sporløs-bloggen</title>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta name="description" content="{escape(p['beskrivelse'])}">
<link rel="canonical" href="{url}">
<meta property="og:title" content="{escape(p['tittel'])}">
<meta property="og:description" content="{escape(p['beskrivelse'])}">
<meta property="og:type" content="article">
<meta property="og:url" content="{url}">
<meta property="og:locale" content="nb_NO">
<meta property="article:published_time" content="{p['dato']}">
{_BRAND_HEAD}{_OG_META}{_BLOGG_RSS_LINK}{ld}
<style>{_BRAND_CSS}{_CHROME_CSS}{_BLOGG_CSS}</style>
{_SELF_SNIPPET}</head><body>
<div class=wrap>
{_SITE_NAV}
<div class=content>
<p class=muted style="margin:0"><a href="/blogg">← Bloggen</a></p>
<h1>{escape(p['tittel'])}</h1>
<p class=dato>{escape(_blogg_norsk_dato(p['dato']))}</p>
<p class=lede>{escape(p['ingress'])}</p>
{p['body']}
</div></div>
{_SITE_FOOTER}</body></html>"""
    )


async def blogg_post(request):
    slug = request.path_params.get("slug", "")
    if slug not in blogg.POSTS:
        return RedirectResponse("/blogg", status_code=302)
    return _render_blogg_post(slug)


async def blogg_index(request):
    kort = "".join(
        f'<a class=post href="/blogg/{slug}">'
        f'<span class=dato>{escape(_blogg_norsk_dato(p["dato"]))}</span>'
        f"<b>{escape(p['tittel'])}</b>"
        f'<span class=ing>{escape(p["ingress"])}</span></a>'
        for slug, p in blogg.POSTS.items()
    )
    return HTMLResponse(
        f"""<!doctype html><html lang="no"><head><meta charset="utf-8">
<title>Blogg — Sporløs</title>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta name="description" content="{escape(_BLOGG_LEDE)}">
<link rel="canonical" href="https://sporlos.no/blogg">
{_BRAND_HEAD}{_OG_META}{_BLOGG_RSS_LINK}
<style>{_BRAND_CSS}{_CHROME_CSS}
.content{{max-width:680px;margin:0 auto;padding-bottom:1rem}}
h1{{font-size:2.1rem;letter-spacing:-.025em}}
.lede{{font-size:1.15rem;color:var(--muted)}}
.post{{display:flex;flex-direction:column;gap:.25rem;border:1px solid var(--line);border-radius:12px;
padding:1.2rem 1.3rem;margin:.8rem 0;text-decoration:none;background:var(--card);transition:border-color .2s}}
.post:hover{{border-color:var(--accent)}}
.post b{{color:var(--ink);font-size:1.08rem;line-height:1.35}}
.post .dato{{color:var(--muted);font-size:.8rem}}
.post .ing{{color:var(--muted);font-size:.92rem}}
.muted{{font-size:.9rem;color:var(--muted)}}</style>
{_SELF_SNIPPET}</head><body>
<div class=wrap>
{_SITE_NAV}
<div class=content>
<h1>Bloggen</h1>
<p class=lede>{escape(_BLOGG_LEDE)}</p>
{kort}
<p class=muted>Abonner med <a href="/blogg/rss.xml">RSS</a>.</p>
</div></div>
{_SITE_FOOTER}</body></html>"""
    )


async def blogg_rss(request):
    items = "".join(
        f"<item><title>{escape(p['tittel'])}</title>"
        f"<link>https://sporlos.no/blogg/{slug}</link>"
        f"<guid>https://sporlos.no/blogg/{slug}</guid>"
        f"<pubDate>{_blogg_rss_dato(p['dato'])}</pubDate>"
        f"<description>{escape(p['beskrivelse'])}</description></item>"
        for slug, p in blogg.POSTS.items()
    )
    return Response(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0"><channel>'
        "<title>Sporløs-bloggen</title>"
        "<link>https://sporlos.no/blogg</link>"
        f"<description>{escape(_BLOGG_LEDE)}</description>"
        f"<language>nb</language>{items}</channel></rss>",
        media_type="application/rss+xml",
    )


def _alias(to):
    """Norsk URL-alias → kanonisk rute (redirect, ingen duplisert side for SEO)."""
    async def handler(request):
        return RedirectResponse(to, status_code=302)
    return handler


async def robots(request):
    return PlainTextResponse(
        "User-agent: *\nAllow: /\nDisallow: /app\n\nSitemap: https://sporlos.no/sitemap.xml\n"
    )


async def llms_txt(request):
    """llms.txt — kuratert oversikt for AI-assistenter (GEO). Bing-indeksen
    mater Copilot/ChatGPT-søk, så en ren, siterbar oppsummering hjelper."""
    return PlainTextResponse(
        "# Sporløs\n\n"
        "> Personvernvennlig, cookieløs webanalyse for EØS — et norsk "
        "Plausible-alternativ. Ingen cookies og ingen samtykkebanner (kun "
        "daglig-saltet enveis-hash, aldri rå-IP lagret). Self-hostbar (AGPL) "
        "eller hosted SaaS. Valgfri BSV-forankring av dags-aggregater som "
        "premium. Drevet av Datamynt AS.\n\n"
        "## Sider\n"
        "- [Hjem](https://sporlos.no/)\n"
        "- [Google Analytics-alternativ](https://sporlos.no/google-analytics-alternativ)\n"
        "- [Spørsmål og svar](https://sporlos.no/sporsmal)\n"
        "- [Blogg](https://sporlos.no/blogg)\n"
        "- [Shopify-integrasjon](https://sporlos.no/shopify)\n"
        "- [For utviklere](https://sporlos.no/utviklere)\n"
        "- [Demo](https://sporlos.no/demo)\n"
        "- [Personvern](https://sporlos.no/personvern)\n"
        "- [Vilkår](https://sporlos.no/vilkar)\n"
    )


async def sitemap(request):
    pages = ["/", "/demo", "/google-analytics-alternativ", "/sporsmal", "/integrasjoner",
             "/shopify", "/signup", "/vilkar", "/personvern", "/utviklere", "/blogg"]
    pages += [f"/integrasjoner/{slug}" for slug in _GUIDES]
    pages += [f"/blogg/{slug}" for slug in blogg.POSTS]
    urls = "".join(f"<url><loc>https://sporlos.no{p}</loc></url>" for p in pages)
    return Response(
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
        media_type="application/xml",
    )


_PERIODS = {"1": ("i dag", 1), "7": ("7 dager", 7), "30": ("30 dager", 30), "90": ("90 dager", 90)}

# Plan-grenser bor i store (delt med notify). Lokale alias beholdes.
_PLAN_LIMITS = store.PLAN_LIMITS


def _plan_limits(plan: str) -> tuple[int | None, int | None]:
    return store.plan_limits(plan)


def _trial_expired(tenant: dict) -> bool:
    if (tenant.get("plan") or "trial") != "trial" or not tenant.get("trial_ends_at"):
        return False
    try:
        ends = datetime.strptime(str(tenant["trial_ends_at"])[:19], "%Y-%m-%d %H:%M:%S")
        return ends.replace(tzinfo=timezone.utc) < datetime.now(timezone.utc)
    except Exception:
        return False


def _fmt_n(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _fmt_kr(cents: int, currency: str = "NOK") -> str:
    """Øre → hele kroner for visning. Andre valutaer får koden som suffiks."""
    return f"{_fmt_n(round(cents / 100))} {'kr' if currency == 'NOK' else currency}"


def _safe_filename(s: str) -> str:
    """Domene o.l. inn i content-disposition: kun ufarlige tegn. `\"` brekker
    header-quoting og CR/LF får h11 til å kaste — begge kan nå hit via
    kundens eget domenefelt."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", s)

# Delt dashboard-CSS (innlogget dashboard + offentlig live-demo).
_DASH_CSS = """
.wrap{max-width:980px;margin:0 auto;padding:0 1.2rem 4rem}
nav{display:flex;align-items:center;justify-content:space-between;padding:1.2rem 0 1.6rem}
nav .links{display:flex;gap:1.1rem;align-items:center;font-size:.9rem}
nav .links a{color:var(--muted);text-decoration:none}nav .links a:hover{color:var(--ink)}
nav .links a.btn{color:#fff;padding:.45rem .9rem}
.tema{background:none;border:1px solid var(--line);border-radius:99px;width:30px;height:30px;
cursor:pointer;color:var(--muted);font-size:1rem;line-height:1;padding:0}
.tema:hover{color:var(--ink);border-color:var(--muted)}
.head{display:flex;align-items:baseline;justify-content:space-between;gap:1rem;flex-wrap:wrap;margin-bottom:1rem}
h1{font-size:1.7rem;letter-spacing:-.02em;margin:0}
.tabs{display:flex;flex-wrap:wrap;gap:.3rem}
.tabs a{padding:.32rem .8rem;border:1px solid var(--line);border-radius:99px;white-space:nowrap;
text-decoration:none;color:var(--muted);font-size:.85rem;background:var(--card)}
.tabs a.on{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:1.1rem 1.25rem}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.8rem;margin:1rem 0}
.kpi b{font-size:1.9rem;display:block;line-height:1.15;letter-spacing:-.02em}
.kpi span{color:var(--muted);font-size:.8rem}
.kpi .d{display:block;font-size:.78rem;font-weight:600;margin-top:.2rem}
.dg{color:var(--ok)}.dr{color:var(--err)}.d0{color:var(--muted)}
/* KPI-hierarki (v2): unike eier blikket — stort kort m/ sparkline + forseglet-badge,
   fire sekundære KPI-er ved siden. Stables på smal skjerm. */
.kpiband{display:grid;grid-template-columns:1.15fr .85fr;gap:.9rem;margin:1rem 0}
@media(max-width:760px){.kpiband{grid-template-columns:1fr}}
.kpihero{display:flex;flex-direction:column}
.kpihero .top{display:flex;justify-content:space-between;align-items:flex-start;gap:.6rem}
.kpihero .lbl{color:var(--muted);font-size:.82rem}
.kpihero .big{font-size:2.7rem;font-weight:800;letter-spacing:-.025em;line-height:1.1;
font-variant-numeric:tabular-nums}
.kpihero .d{font-size:.82rem;font-weight:600}
.kpihero .chart{height:84px;margin-top:.5rem}
.kpisec{display:grid;grid-template-columns:1fr 1fr;gap:.8rem}
.kpisec .kpi{padding:.7rem .9rem}
.kpisec .kpi b{font-size:1.45rem}
.segl-badge{display:inline-flex;align-items:center;gap:.4rem;border:1px solid var(--line);
border-radius:999px;padding:.22rem .65rem .22rem .45rem;font-size:.7rem;color:var(--muted);
background:var(--bg);white-space:nowrap;flex:none}
.hint{color:var(--muted);font-size:.78rem;margin:.1rem 0 .45rem}
.tomt{border:1.5px dashed var(--line);border-radius:12px;padding:1.6rem 1.2rem;text-align:center;
position:relative;overflow:hidden;margin:1rem 0}
.tomt svg.vm{position:absolute;right:-24px;bottom:-30px;width:130px;color:var(--accent);opacity:.06}
.tomt b{font-size:1rem;display:block}
.tomt small{color:var(--muted);font-size:.82rem;display:block;margin-top:.3rem}
.tomt .puls{color:var(--ok);font-size:.78rem;margin-top:.6rem;display:inline-flex;align-items:center;gap:.4rem}
.tomt .dot{width:7px;height:7px;border-radius:50%;background:var(--ok);display:inline-block}
@media (prefers-reduced-motion:no-preference){
@keyframes p{0%,100%{opacity:1}50%{opacity:.35}}.tomt .dot{animation:p 2.4s ease-in-out infinite}}
.chartcard{margin:0 0 .9rem;padding-bottom:.6rem}
.chart{width:100%;height:170px;display:block}
.chartwrap{position:relative;touch-action:pan-y}
.ctip{position:absolute;top:0;left:0;pointer-events:none;background:var(--ink);color:var(--bg);
padding:.4rem .65rem;border-radius:8px;font-size:.78rem;line-height:1.45;white-space:nowrap;
transform:translate(-50%,-118%);box-shadow:0 8px 22px -8px rgba(23,38,62,.5);z-index:5}
.ctip b{font-variant-numeric:tabular-nums}
.ctip .tl{display:block;opacity:.75;font-size:.72rem}
.cdot{position:absolute;width:9px;height:9px;border-radius:50%;background:var(--accent);
border:2px solid var(--card);transform:translate(-50%,-50%);pointer-events:none;z-index:4}
.axis{display:flex;justify-content:space-between;color:var(--muted);font-size:.75rem;padding:0 .2rem}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:.9rem;margin:.9rem 0}
.block{margin:.9rem 0}
body.nobars td{background:none !important}
table{border-collapse:collapse;width:100%;margin:.3rem 0;table-layout:fixed}
td,th{border-bottom:1px solid var(--line);padding:.42rem 0;text-align:left;font-size:.92rem;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
th{color:var(--muted);font-weight:600;font-size:.8rem}
td:last-child,th:last-child{text-align:right;color:var(--muted);width:5rem}
tr:last-child td{border-bottom:0}
h3{margin:0 0 .5rem;font-size:1rem;letter-spacing:-.01em}
/* Verifiserbare tall: Status-kolonnen må romme «✓ forankret ↗» (tillitssignalet
   skal aldri ellipsis-klippes); Dag-kolonnen er fast smal, Segl tar resten. */
.vt th:first-child,.vt td:first-child{width:6.2rem}
.vt th:last-child,.vt td:last-child{width:7.6rem}
@media(max-width:560px){.vt th:first-child,.vt td:first-child{width:5.4rem}
.vt th:last-child,.vt td:last-child{width:6.4rem}}
details summary{cursor:pointer;color:var(--accent-deep);font-size:.9rem}
pre{background:var(--bg);padding:.8rem;border-radius:8px;overflow:auto;font-size:.78rem}
.footnote{color:var(--muted);font-size:.8rem;margin-top:2rem}
.footnote a{color:var(--muted)}
.ic{width:14px;height:14px;vertical-align:-2px;margin-right:.45rem;color:var(--muted);opacity:.8;flex:none}
.fl{margin-right:.4rem}
"""


# Av/på for andelssøylene i tabellene — huskes i nettleseren (localStorage).
# Mørk modus «Midnattsblekk» (design-runde 2, palett B): blekkets egen kulør
# mørknet — papir om dagen, blekk om natten. Inkluderes KUN i dashbord/demo-
# templatene; forsiden og juss-sidene er alltid papir. Auto via systeminnstilling.
# «Midnattsblekk». NB knapp-fyll: i mørk modus flipper --ink til nesten-hvitt, så
# .btn med hvit tekst MÅ ha egne --btn-bg-var (ellers hvit-på-lyst = usynlig).
# Sekundærknapp = dempet blå-grå flate; primær (.btn-accent) holder saturert blå.
_DARK_VARS = (
    "--bg:#121a2b;--card:#19233a;--line:#283450;--ink:#e9edf6;--muted:#9aa6bf;"
    "--accent:#7da2ff;--accent-deep:#8fb0ff;--ok:#4ade80;"
    "--bar:#22335a;--ok-bg:#10302a;--ok-ink:#6ee7a8;--err:#f58a8a;--err-bg:#371b21;"
    "--info:#aebcff;--info-bg:#1b2843;--warn:#e3b341;--warn-bg:#33280f;"
    "--btn-bg:#2f6fed;--btn-bg-h:#1d4ed8"
)
# Manuell overstyring (data-theme) + auto (systeminnstilling, med mindre manuelt lyst).
_DARK_CSS = f"""
:root[data-theme="dark"]{{{_DARK_VARS}}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{{_DARK_VARS}}}}}
"""

# Tema-toggle: tidlig inline-script setter lagret tema FØR render (unngår blink),
# og window.byttTema veksler lyst↔mørkt og husker valget. Knapp i dashbord-nav.
_THEME_HEAD = (
    "<script>(function(){var k='sporlosTema',r=document.documentElement,"
    "s=localStorage.getItem(k);if(s)r.setAttribute('data-theme',s);"
    "window.byttTema=function(){var d=window.matchMedia('(prefers-color-scheme:dark)').matches,"
    "c=r.getAttribute('data-theme')||(d?'dark':'light'),n=c==='dark'?'light':'dark';"
    "localStorage.setItem(k,n);r.setAttribute('data-theme',n);};})();</script>"
)
_THEME_BTN = (
    '<button class=tema onclick="byttTema()" title="Bytt lyst/mørkt" '
    'aria-label="Bytt lyst eller mørkt tema">◐</button>'
)


_BARS_JS = """<script>
(function () {
  var k = 'sporlosBars';
  if (localStorage.getItem(k) === 'av') document.body.classList.add('nobars');
  var t = document.getElementById('barstoggle');
  if (t) t.onclick = function (e) {
    e.preventDefault();
    var av = document.body.classList.toggle('nobars');
    localStorage.setItem(k, av ? 'av' : 'pa');
  };
})();
</script>"""

# Graf-hover: crosshair + boble som snapper til nærmeste bucket. Leser
# forhåndsberegnede [x, y, etikett, unike, visninger] fra data-pts (_area_chart),
# så JS-en slipper all geometri-logikk utover skalering viewBox→piksler.
_CHART_JS = """<script>
(function () {
  var nf = new Intl.NumberFormat('nb-NO');
  document.querySelectorAll('.chartwrap').forEach(function (w) {
    var pts;
    try { pts = JSON.parse(w.dataset.pts || '[]'); } catch (e) { return; }
    if (pts.length < 2) return;
    var svg = w.querySelector('svg'), tip = w.querySelector('.ctip'),
        cx = svg.querySelector('.cx'), dot = w.querySelector('.cdot'),
        W = +w.dataset.w || 880, H = +w.dataset.h || 170;
    function show(clientX) {
      var r = svg.getBoundingClientRect();
      if (!r.width) return;
      var x = (clientX - r.left) / r.width * W, best = 0, bd = 1e9;
      for (var i = 0; i < pts.length; i++) {
        var d = Math.abs(pts[i][0] - x);
        if (d < bd) { bd = d; best = i; }
      }
      var p = pts[best];
      cx.setAttribute('x1', p[0]); cx.setAttribute('x2', p[0]);
      tip.innerHTML = '<span class=tl></span><b></b>';
      tip.querySelector('.tl').textContent = p[2];
      tip.querySelector('b').textContent = nf.format(p[3]);
      tip.querySelector('b').insertAdjacentText('afterend',
        ' unike \\u00b7 ' + nf.format(p[4]) + ' visn.');
      tip.hidden = false;
      /* svg ligger øverst i wrapperen, så svg-lokale piksler == wrapper-lokale */
      var px = p[0] / W * r.width, py = p[1] / H * r.height;
      dot.hidden = false;
      dot.style.left = px + 'px'; dot.style.top = py + 'px';
      var half = tip.offsetWidth / 2 + 6;
      tip.style.left = Math.max(half, Math.min(r.width - half, px)) + 'px';
      tip.style.top = Math.max(py, 34) + 'px';
    }
    function hide() {
      tip.hidden = true;
      dot.hidden = true;
      cx.setAttribute('x1', -9); cx.setAttribute('x2', -9);
    }
    w.addEventListener('mousemove', function (e) { show(e.clientX); });
    w.addEventListener('mouseleave', hide);
    w.addEventListener('touchstart', function (e) { show(e.touches[0].clientX); }, {passive: true});
    w.addEventListener('touchmove', function (e) { show(e.touches[0].clientX); }, {passive: true});
    w.addEventListener('touchend', hide);
    w.addEventListener('touchcancel', hide);
  });
})();
</script>"""


def _stat_table(items, key, icon=None):
    """Nøkkel/antall-tabell: andelssøyle bak hver rad (relativt til toppraden),
    ellipsis-trunkering og full verdi som tooltip.

    Ikon per rad: enten `icon` (callable verdi→html, f.eks. icons.browser)
    eller forhåndsutfylt `i["ikon"]` (brukes for land, der flagget må slås
    opp FØR navnet oversettes til norsk)."""
    mx = max((i["n"] for i in items), default=0) or 1
    rows = ""
    for i in items:
        pct = i["n"] / mx * 100
        ic = i.get("ikon") or (icon(str(i[key])) if icon else "")
        rows += (
            f'<tr><td title="{escape(str(i[key]))}" style="background:linear-gradient(90deg,'
            f'var(--bar) {pct:.0f}%,transparent {pct:.0f}%);border-radius:4px;padding-left:.45rem">'
            f'{ic}{escape(str(i[key]))}</td><td>{i["n"]}</td></tr>'
        )
    return f"<table>{rows or '<tr><td>ingen data enda</td></tr>'}</table>"


_VS_LABEL = {"1": "i går", "7": "forrige 7 dager", "30": "forrige 30 dager", "90": "forrige 90 dager"}


def _delta(now, before, invert=False):
    """↑/↓-endring mot forrige periode. invert=True når lavere er bedre (flukt)."""
    if not before:
        return ""
    pct = round((now - before) / before * 100)
    if abs(pct) > 500:
        # «↑ 1862 %» sier bare at forrige periode var nesten tom (typisk ny site
        # i 90-dagers-visning) — det er støy, ikke innsikt.
        return ('<small class="d d0" title="forrige periode hadde for lite data '
                'til meningsfull sammenligning">—</small>')
    if pct == 0:
        return '<small class="d d0" title="mot forrige periode">±0 %</small>'
    up = pct > 0
    good = (not up) if invert else up
    return (
        f'<small class="d {"dg" if good else "dr"}" title="mot forrige periode">'
        f'{"↑" if up else "↓"} {abs(pct)} %</small>'
    )


def _siden(ts):
    """«for 4 min siden» o.l. fra et UTC-tidspunkt — driver tomtilstanden."""
    if not ts:
        return None
    sek = (datetime.now(timezone.utc) - ts).total_seconds()
    if sek < 90:
        return "for et øyeblikk siden"
    if sek < 3600:
        return f"for {int(sek // 60)} min siden"
    if sek < 86400:
        t = int(sek // 3600)
        return f"for {t} time{'r' if t != 1 else ''} siden"
    d = int(sek // 86400)
    return f"for {d} dag{'er' if d != 1 else ''} siden"


# «Forseglet»-badgen (Segl × Presisjon fra design-runde 2): dobbel ring m/ luft
# der streken krysser — et FUNKSJONELT symbol som kun settes ved forseglede tall,
# aldri dekor. card-param = flatens farge bak (utstansings-effekten).
def _segl_badge(size=26, card="var(--card)"):
    return (
        f'<svg width={size} height={size} viewBox="0 0 64 64" style="color:var(--accent);flex:none" aria-hidden=true>'
        '<circle cx="32" cy="32" r="22" fill="none" stroke="currentColor" stroke-width="2.5"/>'
        '<circle cx="32" cy="32" r="13" fill="none" stroke="currentColor" stroke-width="6.5"/>'
        f'<line x1="18" y1="50" x2="46" y2="14" stroke="{card}" stroke-width="11.5" stroke-linecap="round"/>'
        '<line x1="18" y1="50" x2="46" y2="14" stroke="currentColor" stroke-width="6.5" stroke-linecap="round"/></svg>'
    )


def _verify_table(rollups, public_id=None):
    """Forseglede dagstall m/ status. Med public_id lenkes hver dag til /proof
    (nedlastbart bevis) og hver forankring til en uavhengig kjede-utforsker."""
    rows = []
    for r in rollups:
        day = str(r["day"])[:10]
        if r.get("txid"):
            status = (
                f'<a href="https://whatsonchain.com/tx/{escape(str(r["txid"]))}" '
                'title="Se forankrings-transaksjonen på en uavhengig utforsker" '
                'style="color:var(--ok);text-decoration:none">✓ forankret ↗</a>'
            )
        else:
            status = '<span title="Seglet er laget — venter på neste forankring til kjeden">venter</span>'
        proof_link = ""
        if public_id and r.get("rollup_hash"):
            proof_link = (
                f' <a href="/proof?site={escape(public_id)}&day={day}" title="Last ned bevis (JSON)" '
                'style="font-size:.72rem">bevis</a>'
            )
        rows.append(
            f"<tr><td>{escape(day)}</td><td>{r['visitors']}</td><td>{r['pageviews']}</td>"
            f'<td style="font-family:monospace;font-size:.72rem;color:var(--muted)">'
            f"{escape((r['rollup_hash'] or '')[:12])}…{proof_link}</td>"
            f"<td>{status}</td></tr>"
        )
    rr = "".join(rows)
    anchored = sum(1 for r in rollups if r.get("txid"))
    badge = ""
    if rollups:
        badge = (
            '<span style="display:inline-flex;align-items:center;gap:.4rem;border:1px solid var(--line);'
            'border-radius:999px;padding:.22rem .7rem .22rem .45rem;font-size:.72rem;color:var(--muted);'
            f'background:var(--bg);float:right">{_segl_badge(16, "var(--bg)")}'
            f"{anchored}/{len(rollups)} forankret</span>"
        )
    howto = (
        '<details style="margin-top:.6rem"><summary>Hvordan etterprøver jeg dette selv?</summary>'
        '<ol style="color:var(--muted);font-size:.85rem;margin:.6rem 0 .2rem;padding-left:1.3rem">'
        "<li><b>Last ned beviset</b> for en dag (lenken ved seglet). Det inneholder dagens tall "
        "nøyaktig slik de ble forseglet.</li>"
        "<li><b>Regn ut seglet selv:</b> sha256 av tallene (kanonisk JSON, oppskrift i beviset) "
        "skal gi nøyaktig samme hash som står her.</li>"
        "<li><b>Følg Merkle-stien</b> i beviset opp til roten — hvert steg er én sha256.</li>"
        "<li><b>Slå opp transaksjonen</b> på en uavhengig utforsker (lenken i Status-kolonnen): "
        "roten ligger i OP_RETURN-feltet, tidsstemplet av et nettverk vi ikke kontrollerer.</li>"
        "</ol>"
        '<p style="color:var(--muted);font-size:.85rem;margin:.4rem 0 .2rem">Endres ett eneste tall '
        "i ettertid, stemmer ikke seglet i steg 2 — det er hele poenget. Du trenger ikke stole på "
        "oss, bare på sha256.</p></details>"
    )
    return (
        f"<h3>{badge}<span style='display:inline-flex;align-items:center;gap:.5rem'>"
        f"{_segl_badge(22)}Verifiserbare tall</span></h3>"
        '<p style="color:var(--muted);font-size:.85rem">Hver dags tall forsegles med en kryptografisk '
        "hash som forankres i en offentlig blokkjede — etter det kan ingen, heller ikke vi, endre dem "
        "uten at det synes.</p>"
        '<table class=vt><tr><th>Dag</th><th>Unike</th><th>Visn.</th><th>Segl</th><th>Status</th></tr>'
        f"{rr or '<tr><td>ingen forseglede dager enda — første segl lages i natt</td><td></td><td></td><td></td><td></td></tr>'}</table>"
        f"{howto}"
    )


_UKEDAGER = ["man.", "tir.", "ons.", "tor.", "fre.", "lør.", "søn."]


def _fmt_bucket(bucket, unit: str, win_start: date | None = None, today: date | None = None) -> str:
    """Menneskelig etikett for en tidsserie-bucket: «kl. 14–15», «tir. 8. juli»,
    «uke 28 · 6.–12. juli». Faller tilbake til råstrengen ved uventet format.

    win_start/today klipper uke-spennet til det dataene faktisk dekker — første
    og siste uke i et 90-dagers vindu er som regel delvise, og en etikett som
    påstår hel uke ville forklart et «stup» i grafen med feil premiss."""
    b = str(bucket)
    try:
        if unit == "hour":
            h = int(b[11:13])
            return f"kl. {h:02d}–{(h + 1) % 24:02d}"
        d = date(int(b[:4]), int(b[5:7]), int(b[8:10]))
    except (ValueError, IndexError):
        return b
    if unit == "week":
        start = max(d, win_start) if win_start else d
        full_end = d + timedelta(days=6)
        end = min(full_end, today) if today else full_end
        partial = " hittil" if (start > d or end < full_end) else ""
        if start == end:
            span = f"{start.day}. {_MND[start.month]}"
        elif start.month == end.month:
            span = f"{start.day}.–{end.day}. {_MND[end.month]}"
        else:
            span = f"{start.day}. {_MND[start.month][:3]}–{end.day}. {_MND[end.month][:3]}"
        return f"uke {d.isocalendar()[1]} · {span}{partial}"
    return f"{_UKEDAGER[d.weekday()]} {d.day}. {_MND[d.month]}"


def _area_chart(series, width=880, height=170, days=7):
    """SVG-areagraf (unike per bucket): aksent-linje + gradientflate + interaktiv
    hover (crosshair + boble m/ tall for nærmeste bucket — se _CHART_JS).
    Rene rette segmenter — ærlig dataviz, ingen utjevning som lyver mellom punktene."""
    if not series:
        return '<p class=muted style="font-size:.9rem">ingen data enda</p>'
    unit = "hour" if days == 1 else ("week" if days >= 60 else "day")
    pad_x, pad_top, pad_bot = 8, 14, 22
    peak = max((b["visitors"] for b in series), default=0) or 1
    n = len(series)
    step = (width - 2 * pad_x) / max(n - 1, 1)
    span = height - pad_top - pad_bot
    pts = [
        (pad_x + i * step, pad_top + span * (1 - b["visitors"] / peak))
        for i, b in enumerate(series)
    ]
    # Hover-data: [x, y, etikett, unike, visninger] per bucket — resten gjør JS-en.
    today = datetime.now(timezone.utc).date()
    win_start = today - timedelta(days=days - 1)
    hover = "[]"
    if n > 1:
        hover = json.dumps(
            [
                [round(x, 1), round(y, 1), _fmt_bucket(b["bucket"], unit, win_start, today),
                 b["visitors"], b["pageviews"]]
                for (x, y), b in zip(pts, series)
            ],
            ensure_ascii=False,
        )
    if n == 1:  # ett punkt: tegn en flat strek over hele bredden
        y = pts[0][1]
        pts = [(pad_x, y), (width - pad_x, y)]
    line = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"{line} L{pts[-1][0]:.1f},{height - pad_bot} L{pts[0][0]:.1f},{height - pad_bot} Z"
    # Aksen holdes kort (uke-spenn bryter over to linjer på mobil) — detaljene bor i hoveren.
    if unit == "week":
        first = escape(_fmt_bucket(series[0]["bucket"], unit).split(" · ")[0])
        last = escape(_fmt_bucket(series[-1]["bucket"], unit).split(" · ")[0])
    else:
        first = escape(_fmt_bucket(series[0]["bucket"], unit))
        last = "nå" if unit == "hour" else escape(_fmt_bucket(series[-1]["bucket"], unit))
    return (
        f"<div class=chartwrap data-w={width} data-h={height} data-pts='{escape(hover)}'>"
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" class=chart role=img>'
        '<defs><linearGradient id=cg x1=0 y1=0 x2=0 y2=1>'
        '<stop offset=0 style="stop-color:var(--accent)" stop-opacity=".16"/>'
        '<stop offset=1 style="stop-color:var(--accent)" stop-opacity="0"/></linearGradient></defs>'
        f'<path d="{area}" fill="url(#cg)"/>'
        f'<path d="{line}" fill=none style="stroke:var(--accent)" stroke-width="2.5" '
        'stroke-linejoin=round stroke-linecap=round/>'
        f'<line class=cx x1=-9 x2=-9 y1="{pad_top - 6}" y2="{height - pad_bot}" '
        'style="stroke:var(--line)" stroke-width="1.5"/>'
        "</svg><div class=cdot hidden></div><div class=ctip hidden></div></div>"
        f'<div class=axis><span>{first}</span><span>topp: {peak} unike</span><span>{last}</span></div>'
    )


def _public_stats_page(request, site, base_path, *, public_id, suffix, intro, title, description, canonical):
    """Delt renderer for offentlige statistikk-sider (/demo + /p/<site>). Read-only."""
    period = request.query_params.get("period", "7")
    if period not in _PERIODS:
        period = "7"
    label, days = _PERIODS[period]

    s = store.stats(site["id"], days)
    # Flagg slås opp på engelsk navn FØR oversettelse til norsk visningsnavn
    s["countries"] = [
        {**c, "ikon": icons.flag(c["k"]), "k": country_no(c["k"])} for c in s["countries"]
    ]
    prev = store.kpis(site["id"], days, offset=1)
    chart = _area_chart(store.timeseries(site["id"], days), days=days)
    flow = store.flow_stats(site["id"], days)
    transitions = store.path_transitions(site["id"], days)
    verify_html = _verify_table(store.recent_rollups(site["id"]), public_id)

    # Navigasjonsstier + kampanjer vises kun når det finnes data — demoen skal
    # vise bredden i produktet, men tomme kort selger ingenting.
    nav_html = ""
    if transitions:
        nav_rows = "".join(
            f'<tr><td title="{escape(tr["from"])} → {escape(tr["to"])}">'
            f'{escape(tr["from"])} → {escape(tr["to"])}</td><td>{tr["n"]}</td></tr>'
            for tr in transitions[:8]
        )
        nav_html = (
            '<div class="card block"><h3>Navigasjonsstier</h3>'
            '<p class=muted style="font-size:.85rem;margin:.1rem 0 .4rem">Vanligste '
            "side→side-overganger innen en økt — som aggregat, aldri enkeltpersoner.</p>"
            f"<table><tr><th>Fra → Til</th><th>Antall</th></tr>{nav_rows}</table></div>"
        )
    camp_html = ""
    if s.get("campaigns"):
        camp_rows = "".join(
            f'<tr><td title="{escape(c["source"])}">{escape(c["source"])}'
            f'{(" / " + escape(c["campaign"])) if c["campaign"] else ""}</td>'
            f'<td>{c["visitors"]}</td><td>{c["n"]}</td></tr>'
            for c in s["campaigns"][:8]
        )
        camp_html = (
            '<div class="card block"><h3>Kampanjer (UTM)</h3>'
            f"<table><tr><th>Kilde / kampanje</th><th>Unike</th><th>Visn.</th></tr>{camp_rows}</table></div>"
        )

    tabs = " ".join(
        f'<a href="{base_path}?period={k}" class="{"on" if k == period else ""}">{escape(v[0])}</a>'
        for k, v in _PERIODS.items()
    )

    return HTMLResponse(
        f"""<!doctype html><html lang="no"><head><meta charset="utf-8">
<title>{escape(title)}</title>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta name=description content="{escape(description)}">
<link rel="canonical" href="{escape(canonical)}">
{_BRAND_HEAD}{_OG_META}
<style>{_BRAND_CSS}{_DARK_CSS}{_CHROME_CSS}{_DASH_CSS}
.demobar{{background:var(--info-bg);border:1px solid var(--line);color:var(--info);border-radius:10px;
padding:.6rem .9rem;font-size:.9rem;margin-bottom:1rem}}</style>
</head><body>
<div class=wrap>
{_SITE_NAV}
{intro}
<div class=head><h1>{escape(site["domain"])} <span class=muted style="font-size:1rem;font-weight:400">· {escape(suffix)}</span></h1>
<div class=tabs>{tabs}</div></div>
<div class=kpis>
  <div class="card kpi" title="Unike per dag — vi følger ingen på tvers av dager"><b>{s['visitors']}</b><span>unike besøkende</span>{_delta(s['visitors'], prev['visitors'])}</div>
  <div class="card kpi" title="Én sammenhengende økt — 30 min pause regnes som nytt besøk"><b>{s['sessions']}</b><span>besøk</span>{_delta(s['sessions'], prev['sessions'])}</div>
  <div class="card kpi"><b>{s['pageviews']}</b><span>sidevisninger</span>{_delta(s['pageviews'], prev['pageviews'])}</div>
  <div class="card kpi" title="Andel besøk som forlot nettstedet etter bare én side — lavere er bedre"><b>{s['bounce_rate']}%</b><span>fluktfrekvens</span>{_delta(s['bounce_rate'], prev['bounce_rate'], invert=True)}</div>
  <div class="card kpi" title="Sidevisninger delt på besøk — hvor dypt folk går"><b>{s['views_per_session']}</b><span>visn. per besøk</span></div>
</div>
<div class="card chartcard">
<p class=muted style="font-size:.8rem;margin:.1rem 0 .6rem">Unike besøkende · {escape(label)} <span style="float:right">endring målt mot {_VS_LABEL[period]}</span></p>
{chart}
</div>
<div class=grid>
  <div class=card><h3>Topp sider</h3>{_stat_table(s['top_paths'], 'path')}</div>
  <div class=card><h3>Topp kilder</h3><p class=hint>hvor trafikken kommer fra — «direkte» = skrev inn adressen eller bokmerke</p>{_stat_table(s['top_sources'], 'src')}</div>
  <div class=card><h3>Inngangssider</h3><p class=hint>første side i besøket — der folk lander</p>{_stat_table(flow['entries'], 'path')}</div>
  <div class=card><h3>Utgangssider</h3><p class=hint>siste side før de dro — se etter lekkasjer</p>{_stat_table(flow['exits'], 'path')}</div>
  <div class=card><h3>Land</h3>{_stat_table(s['countries'], 'k')}</div>
  <div class=card><h3>Fylke / region</h3>{_stat_table(s['regions'], 'k')}</div>
  <div class=card><h3>Enheter</h3>{_stat_table(s['devices'], 'k', icons.device)}</div>
  <div class=card><h3>Nettlesere</h3>{_stat_table(s['browsers'], 'k', icons.browser)}</div>
  <div class=card><h3>Operativsystem</h3>{_stat_table(s['os'], 'k', icons.os)}</div>
</div>
{nav_html}
{camp_html}
<div class="card block">{verify_html}</div>
<div class="card block" style="text-align:center;padding:2rem">
  <p style="margin:0 0 1rem;font-size:1.05rem"><b>Vil du ha dette for ditt nettsted — uten cookie-banner?</b></p>
  <a class="btn btn-accent" href="/signup">Start gratis prøve</a>
  <p class=fine style="margin-top:.7rem;color:var(--muted);font-size:.85rem">30 dager · uten kort</p>
</div>
<p class=footnote>Cookieløs · ingen IP lagret · samtykkefri ·
<a href="#" id=barstoggle>andelssøyler av/på</a> ·
Geo: <a href="https://db-ip.com">IP Geolocation by DB-IP</a> (CC BY 4.0)</p>
</div>
{_SITE_FOOTER}
{_BARS_JS}
{_CHART_JS}
{_SELF_SNIPPET}</body></html>""",
        headers={"cache-control": "public, max-age=60"},
    )


async def proof(request):
    """GET /proof?site=<public_id>&day=YYYY-MM-DD — nedlastbart verifiserings-bevis.

    Selvstendig JSON: dagens tall (kanonisk payload), segl-hash, Merkle-sti, rot og
    txid — alt en tredjepart trenger for å etterprøve uten å stole på oss.
    Tilgang: eier av siten, eller site med offentlig dashboard (inkl. demo-siten)."""
    public_id = request.query_params.get("site") or ""
    day = request.query_params.get("day") or ""
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        return JSONResponse({"error": "day må være YYYY-MM-DD"}, status_code=400)
    site = store.resolve_site(public_id) if public_id else None
    if not site:
        return JSONResponse({"error": "ukjent site"}, status_code=404)
    user = _user(request)
    allowed = (
        (user and site["tenant_id"] == user["tid"])
        or bool((store.get_public_site(public_id) or {}).get("public_dash"))
        or public_id == os.environ.get("SPORLOS_DEMO_SITE", "6LIACtOSP-S7")
    )
    if not allowed:
        return JSONResponse({"error": "ukjent site"}, status_code=404)  # ikke-eier ser ikke at den finnes
    r = store.get_rollup(site["id"], day)
    if not r or not r.get("rollup_hash"):
        return JSONResponse({"error": "ingen forseglet rollup for denne dagen"}, status_code=404)

    # Payload NØYAKTIG som i store.compute_rollup — sha256 av denne er seglet.
    # int()-coercion er semantisk viktig: DB-kolonnen kan gi 100.0 (float) tilbake,
    # men seglet ble laget av int (round()) — "100.0" ≠ "100" i kanonisk JSON.
    payload = {
        "site_id": int(r["site_id"]),
        "day": day,
        "pageviews": int(r["pageviews"]),
        "visitors": int(r["visitors"]),
        "sessions": int(r["sessions"]),
        "bounce_rate": int(r["bounce_rate"]),
    }
    try:
        mproof = json.loads(r["merkle_proof"]) if r.get("merkle_proof") else None
    except (TypeError, ValueError):
        mproof = None
    txid = r.get("txid")
    out = {
        "hva": f"Verifiserings-bevis for {site['domain']} {day}, utstedt av Sporløs (sporlos.no).",
        "domain": site["domain"],
        "day": day,
        "payload": payload,
        "rollup_hash": r["rollup_hash"],
        "steg_1": "sha256(json.dumps(payload, sort_keys=True, separators=(', ', ': ')).encode()).hexdigest() == rollup_hash",
        "merkle": {
            "steg_2": "RFC 6962-stil: blad = sha256(0x00 || bytes.fromhex(rollup_hash)); "
                      "node = sha256(0x01 || venstre || høyre). Følg proof-stegene "
                      "(right=true → søsken på høyre side) opp til root.",
            "proof": mproof,
            "root": r.get("merkle_root"),
        },
        "kjede": {
            "steg_3": "root ligger i OP_RETURN-feltet i transaksjonen under (prefiks 'SPORLOS'), "
                      "tidsstemplet av BSV-nettverket.",
            "txid": txid,
            "explorer": f"https://whatsonchain.com/tx/{txid}" if txid else None,
            "anchored_at": str(r["anchored_at"])[:19] if r.get("anchored_at") else None,
        }
        if txid
        else {"status": "venter", "note": "Seglet er laget, men ikke forankret on-chain enda."},
    }
    return JSONResponse(
        out,
        headers={
            "content-disposition":
                f'attachment; filename="sporlos-bevis-{_safe_filename(site["domain"])}-{day}.json"'
        },
    )


async def demo(request):
    """Offentlig live-demo: ekte tall for sporlos.no selv — produktet i drift som bevis."""
    site = store.resolve_site(os.environ.get("SPORLOS_DEMO_SITE", "6LIACtOSP-S7"))
    if not site:
        return PlainTextResponse("not found", status_code=404)
    intro = (
        "<div class=demobar>Dette er ekte, levende tall for <b>sporlos.no</b> — målt av "
        "Sporløs selv, uten cookies og uten samtykke. Det du ser her, er det kundene får.</div>"
    )
    return _public_stats_page(
        request, site, "/demo",
        public_id=os.environ.get("SPORLOS_DEMO_SITE", "6LIACtOSP-S7"),
        suffix="live demo", intro=intro,
        title="Live demo — ekte tall for sporlos.no | Sporløs",
        description="Sporløs i drift: ekte, levende statistikk for sporlos.no — cookieløst og uten samtykke. Slik ser dashbordet ut.",
        canonical="https://sporlos.no/demo",
    )


async def public_dash(request):
    """Opt-in offentlig dashboard per site — deles med lenke, ingen innlogging."""
    pid = request.path_params["public_id"]
    site = store.get_public_site(pid)
    if not site or not site.get("public_dash"):
        return PlainTextResponse("not found", status_code=404)
    return _public_stats_page(
        request, site, f"/p/{escape(pid)}",
        public_id=pid,
        suffix="offentlig statistikk", intro="",
        title=f"{site['domain']} — offentlig statistikk | Sporløs",
        description=f"Åpen, cookieløs statistikk for {site['domain']} — målt av Sporløs, uten cookies og uten samtykke.",
        canonical=f"https://sporlos.no/p/{pid}",
    )


async def site_public_toggle(request):
    """Slå offentlig dashboard av/på for egen site (form i dashbordet)."""
    f = await request.form()
    site, pid = _own_site(request, f)
    if site:
        store.set_public_dash(pid, site["tenant_id"], f.get("on") == "1")
    return RedirectResponse(f"/app?site={pid}" if pid else "/app", status_code=302)


async def export_csv(request):
    """CSV-eksport for regneark. Semikolon + UTF-8 BOM = norsk Excel åpner den riktig."""
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    site = store.resolve_site(request.query_params.get("site") or "")
    if not site or site["tenant_id"] != user["tid"]:
        return PlainTextResponse("not found", status_code=404)
    period = request.query_params.get("period", "7")
    if period not in _PERIODS:
        period = "7"
    days = _PERIODS[period][1]
    what = request.query_params.get("what", "tidsserie")

    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    if what == "tidsserie":
        w.writerow(["dag", "unike besøkende", "sidevisninger"])
        # Alltid dagsoppløsning i CSV (unit="day") — regneark aggregerer selv;
        # uke-buckets under en «dag»-header ville stille endret semantikken.
        for b in store.timeseries(site["id"], days, unit=None if days == 1 else "day"):
            w.writerow([b["bucket"], b["visitors"], b["pageviews"]])
    elif what in ("sider", "kilder", "land"):
        w.writerow([what[:-1] if what != "land" else "land", "sidevisninger", "unike besøkende"])
        for r in store.export_breakdown(site["id"], days, what):
            k = country_no(r["k"]) if what == "land" else r["k"]
            w.writerow([k, r["n"], r["u"]])
    else:
        return PlainTextResponse("ukjent eksport", status_code=400)

    fname = f"sporlos-{_safe_filename(site['domain'])}-{what}-{days}d.csv"
    return Response(
        "\ufeff" + buf.getvalue(),  # BOM: Excel skal lese æøå riktig
        media_type="text/csv; charset=utf-8",
        headers={"content-disposition": f'attachment; filename="{fname}"'},
    )


async def dashboard(request):
    """Dashboard m/ periodevelger, trendgraf og breakdowns. Styling: midlertidig (design-runde senere)."""
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    public_id = request.query_params.get("site")
    site = store.resolve_site(public_id) if public_id else None
    if site and site["tenant_id"] != user["tid"]:
        site, public_id = None, None  # tenant-isolasjon: ikke din site

    me = store.get_user(user["uid"])
    verify_banner = ""
    if me and not me["email_verified"] and me.get("email"):
        sent = "Ny lenke sendt. " if request.query_params.get("vsent") else ""
        verify_banner = (
            '<p style="background:var(--warn-bg);color:var(--warn);padding:.5rem .8rem;border-radius:7px;'
            f'font-size:.9rem">{sent}Bekreft e-posten din ({escape(me["email"])}) — sjekk innboksen, '
            'eller <a href="/resend-verify" style="color:inherit;text-decoration:underline">send på nytt</a>.</p>'
        )

    if not site:
        sites = store.list_sites(user["tid"])
        tenant = store.get_tenant(user["tid"]) or {}
        def _dot(s):
            # Tilkoblet hvis vi noen gang har sett et event; ellers venter på første besøk.
            if s.get("last_ts"):
                return ('<span title="tilkoblet — data mottatt" style="color:var(--ok)">●</span> ')
            return ('<span title="venter på første besøk" style="color:var(--muted)">○</span> ')

        rows = "".join(
            f'<tr><td>{_dot(s)}<a href="/app?site={escape(s["public_id"])}">{escape(s["domain"])}</a></td>'
            f'<td>{s["visitors"]}</td><td>{s["pv"]}</td></tr>'
            for s in sites
        )
        plan = tenant.get("plan") or "trial"
        pv_lim, site_lim = _plan_limits(plan)
        expired = _trial_expired(tenant)
        usage = store.monthly_usage(user["tid"])

        trial = ""
        if expired:
            trial = (
                '<p style="background:var(--err-bg);color:var(--err);padding:.5rem .8rem;border-radius:7px;'
                'font-size:.9rem"><b>Prøveperioden er utløpt.</b> Tallene dine samles fortsatt '
                "(vi kaster aldri data) — velg en plan under for å fortsette.</p>"
            )
        elif plan == "trial" and tenant.get("trial_ends_at"):
            trial = (
                '<p style="background:var(--info-bg);color:var(--info);padding:.5rem .8rem;border-radius:7px;'
                f'font-size:.9rem">Prøveperiode — utløper {escape(str(tenant["trial_ends_at"])[:10])}.</p>'
            )

        # Forbruk mot plan (skjules for ubegrensede planer)
        usage_html = ""
        if pv_lim:
            pct = min(100, round(usage["pageviews"] / pv_lim * 100))
            over = usage["pageviews"] > pv_lim
            color = "var(--err)" if over else ("var(--warn)" if pct >= 80 else "var(--ok)")
            warn = ""
            if over:
                warn = (
                    '<p style="color:var(--err);margin:.4rem 0 0">Over planens visninger denne '
                    "måneden — alt måles fortsatt, men vurder å oppgradere.</p>"
                )
            usage_html = (
                '<div class=card style="font-size:.85rem;color:var(--muted)">'
                f'Visninger denne måneden: <b style="color:var(--ink)">{_fmt_n(usage["pageviews"])}</b> '
                f"av {_fmt_n(pv_lim)}"
                f'<div style="background:var(--line);border-radius:99px;height:6px;margin:.35rem 0">'
                f'<div style="width:{pct}%;background:{color};height:6px;border-radius:99px"></div></div>'
                f'Nettsteder: {usage["sites"]} av {site_lim}{warn}</div>'
            )

        limit_msg = ""
        if request.query_params.get("limit") == "sites":
            limit_msg = (
                '<p style="background:var(--err-bg);color:var(--err);padding:.5rem .8rem;border-radius:7px;'
                f'font-size:.9rem">Planen din har plass til {site_lim} nettsted'
                f'{"er" if (site_lim or 0) != 1 else ""} — oppgrader for å legge til flere.</p>'
            )
        _err = request.query_params.get("err")
        if _err in ("domain", "dup"):
            msg = ("Skriv inn et domene (f.eks. dittdomene.no)." if _err == "domain"
                   else "Du har allerede lagt til dette nettstedet.")
            limit_msg += (
                '<p style="background:var(--err-bg);color:var(--err);padding:.5rem .8rem;'
                f'border-radius:7px;font-size:.9rem">{msg}</p>'
            )
        upgrade = ""
        if tenant.get("plan") in ("trial", "cancelled", None):
            btns = ""
            if stripe:
                btns = "".join(
                    f'<a href="/billing/checkout?plan={k}" style="display:inline-block;'
                    "margin:.2rem .4rem .2rem 0;padding:.4rem .7rem;border:1px solid var(--info);"
                    'border-radius:7px;text-decoration:none;color:var(--info);font-size:.9rem">'
                    f"{escape(_PLAN_LABELS[k])}</a>"
                    for k in ("liten", "vekst", "pro")
                    if STRIPE_PRICES.get(k)
                )
            vbtns = ""
            if vipps.configured():
                vbtns = "".join(
                    f'<a href="/billing/vipps/start?plan={k}" style="display:inline-block;'
                    "margin:.2rem .4rem .2rem 0;padding:.4rem .7rem;border:1px solid #ff5b24;"
                    'border-radius:7px;text-decoration:none;color:#ff5b24;font-size:.9rem">'
                    f"{escape(_PLAN_LABELS[k])} med Vipps</a>"
                    for k in ("liten", "vekst", "pro")
                )
            if btns or vbtns:
                sep = "<br>" if (btns and vbtns) else ""
                upgrade = (
                    f'<div style="margin:1rem 0"><b>Oppgrader:</b><br>{btns}{sep}{vbtns}<br>'
                    '<span style="color:var(--muted);font-size:.8rem">Faktura/EHF for byrå/kommune? '
                    '<a href="/vilkar">Kontakt oss</a></span></div>'
                )
        # API-tilgang: read-only nøkler for AI-verktøy/integrasjoner
        keys = store.list_api_keys(user["tid"])
        new_key = request.session.pop("new_api_key", None)
        new_key_html = ""
        if new_key:
            new_key_html = (
                '<p style="background:var(--ok-bg);color:var(--ok-ink);padding:.6rem .8rem;border-radius:7px;'
                'font-size:.85rem;word-break:break-all"><b>Ny nøkkel — kopier den nå, den vises '
                f"ikke igjen:</b><br><code>{escape(new_key)}</code></p>"
            )
        key_rows = "".join(
            f'<tr><td>{escape(k["label"])} <small style="color:var(--muted)">{escape(k["prefix"])}…</small></td>'
            f'<td>{escape(str(k["created_at"])[:10])}</td>'
            f'<td>{escape(str(k["last_used_at"])[:10]) if k["last_used_at"] else "aldri"}</td>'
            f'<td><form method=post action="/app/api-keys/revoke" style="display:inline">'
            f'<input type=hidden name=key_id value="{k["id"]}">'
            '<button title="Trekk tilbake" style="background:none;border:0;color:var(--err);cursor:pointer">✕</button>'
            "</form></td></tr>"
            for k in keys
        )
        keys_table = (
            f"<table><tr><th>Nøkkel</th><th>Laget</th><th>Sist brukt</th><th></th></tr>{key_rows}</table>"
            if key_rows
            else ""
        )
        api_html = (
            "<div class=card>"
            '<p class=fine style="margin:.2rem 0 .6rem">Read-only nøkler for AI-verktøy og '
            'integrasjoner — kun aggregater, aldri rådata. <a href="/utviklere">Dokumentasjon</a>.</p>'
            f"{new_key_html}{keys_table}"
            '<form class=add method=post action="/app/api-keys" style="margin-top:.6rem">'
            '<input name=label placeholder="Navn (f.eks. Claude)" maxlength=60>'
            "<button class=btn>Lag API-nøkkel</button></form></div>"
        )

        # Bytt passord (+ flash-melding fra ?pw=)
        pw_flash = {
            "ok": '<p style="background:var(--ok-bg);color:var(--ok-ink);padding:.5rem .8rem;border-radius:7px;font-size:.9rem">Passordet er byttet.</p>',
            "feil": '<p style="background:var(--err-bg);color:var(--err);padding:.5rem .8rem;border-radius:7px;font-size:.9rem">Feil nåværende passord.</p>',
            "kort": '<p style="background:var(--err-bg);color:var(--err);padding:.5rem .8rem;border-radius:7px;font-size:.9rem">Nytt passord må ha minst 8 tegn.</p>',
        }.get(request.query_params.get("pw") or "", "")
        password_html = (
            '<div class=card><details><summary style="cursor:pointer;font-weight:600">Bytt passord</summary>'
            '<form method=post action="/app/password" style="display:flex;gap:.5rem;flex-wrap:wrap;margin-top:.7rem">'
            '<input name=old type=password placeholder="Nåværende passord" required '
            'style="flex:1;min-width:10rem;padding:.5rem;border:1px solid var(--line);border-radius:8px">'
            '<input name=new type=password placeholder="Nytt passord (min. 8)" required minlength=8 '
            'style="flex:1;min-width:10rem;padding:.5rem;border:1px solid var(--line);border-radius:8px">'
            "<button class=btn>Bytt</button></form>"
            '<p class=fine style="margin:.5rem 0 0">Logget inn med Google eller innlogg.no? Da styres innloggingen der.</p>'
            "</details></div>"
        )

        planinfo = ""
        if tenant.get("plan") in ("liten", "vekst", "pro"):
            label = {"liten": "Liten", "vekst": "Vekst", "pro": "Pro"}[tenant["plan"]]
            if stripe and tenant.get("stripe_customer_id"):
                portal = ' · <a href="/billing/portal" style="color:var(--info)">Administrer abonnement</a>'
            elif tenant.get("vipps_agreement_id") and not tenant.get("vipps_pending_plan"):
                portal = (
                    " · betales med Vipps · "
                    '<form method=post action="/billing/vipps/avslutt" style="display:inline" '
                    "onsubmit=\"return confirm('Stoppe Vipps-avtalen? Planen gjelder ut betalt periode.')\">"
                    '<button style="background:none;border:0;padding:0;color:#b91c1c;cursor:pointer;'
                    'font-size:inherit;text-decoration:underline">Avslutt abonnement</button></form>'
                )
            else:
                portal = ""
            # <div>, ikke <p>: nettlesere lukker <p> ved <form> (Vipps-avslutt-knappen)
            planinfo = (
                '<div style="background:var(--ok-bg);color:var(--ok-ink);padding:.5rem .8rem;border-radius:7px;'
                f'font-size:.9rem;margin:1rem 0"><b>Plan:</b> {label}{portal}</div>'
            )
        vipps_flash = {
            "ok": '<p style="background:var(--ok-bg);color:var(--ok-ink);padding:.5rem .8rem;border-radius:7px;font-size:.9rem">Vipps-avtalen er aktiv — velkommen! 🎉</p>',
            "venter": '<p style="background:var(--info-bg);color:var(--info);padding:.5rem .8rem;border-radius:7px;font-size:.9rem">Venter på bekreftelse fra Vipps — oppdater siden om et øyeblikk.</p>',
            "avbrutt": '<p style="background:var(--err-bg);color:var(--err);padding:.5rem .8rem;border-radius:7px;font-size:.9rem">Vipps-betalingen ble avbrutt — ingenting er trukket.</p>',
            "feil": '<p style="background:var(--err-bg);color:var(--err);padding:.5rem .8rem;border-radius:7px;font-size:.9rem">Noe gikk galt mot Vipps — prøv igjen, eller bruk kort.</p>',
            "stoppet": '<p style="background:var(--info-bg);color:var(--info);padding:.5rem .8rem;border-radius:7px;font-size:.9rem">Vipps-avtalen er stoppet. Planen gjelder ut betalt periode.</p>',
        }.get(request.query_params.get("vipps") or "", "")
        plan_sec = ""
        if planinfo or usage_html or upgrade:
            plan_sec = f"<h2 class=sec>Plan og forbruk</h2>{planinfo}{usage_html}{upgrade}"
        return HTMLResponse(
            f"""<!doctype html><html lang=no><meta charset=utf-8>
<title>Sporløs — mine nettsteder</title>
<meta name=viewport content="width=device-width, initial-scale=1">
{_BRAND_HEAD}
<style>{_BRAND_CSS}{_DARK_CSS}
.wrap{{max-width:640px;margin:0 auto;padding:0 1.2rem 4rem}}
nav{{display:flex;align-items:center;justify-content:space-between;padding:1.2rem 0 1.6rem}}
nav a.ut{{color:var(--muted);text-decoration:none;font-size:.9rem}}
h1{{font-size:1.6rem;letter-spacing:-.02em;margin:0 0 .3rem}}
h2.sec{{font-size:.74rem;letter-spacing:.1em;text-transform:uppercase;color:var(--muted);
font-weight:700;margin:2rem 0 .4rem}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:1.1rem 1.25rem;margin:.9rem 0}}
table{{border-collapse:collapse;width:100%;table-layout:fixed}}
th,td{{border-bottom:1px solid var(--line);padding:.55rem .2rem;text-align:left;font-size:.95rem;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
th{{color:var(--muted);font-weight:600;font-size:.8rem}}
th:not(:first-child),td:not(:first-child){{text-align:right;width:5.5rem;color:var(--muted)}}
tr:last-child td{{border-bottom:0}}
td a{{color:var(--ink);text-decoration:none;font-weight:600}}td a:hover{{color:var(--accent-deep)}}
form.add{{display:flex;gap:.5rem}}
input,textarea,select{{color:var(--ink);background:var(--card)}}
input::placeholder,textarea::placeholder{{color:var(--muted)}}
form.add input{{flex:1;padding:.6rem;border:1px solid var(--line);border-radius:8px;font-size:.95rem;background:var(--card);color:var(--ink)}}
.fine{{color:var(--muted);font-size:.8rem}}
.tema{{background:none;border:1px solid var(--line);border-radius:99px;width:30px;height:30px;cursor:pointer;color:var(--muted);font-size:1rem;line-height:1;padding:0;margin-right:.6rem}}
.tema:hover{{color:var(--ink);border-color:var(--muted)}}</style>
{_THEME_HEAD}
<div class=wrap>
<nav>{_WORDMARK}<span>{_THEME_BTN}<a class=ut href="/logout">Logg ut</a></span></nav>
<h1>Mine nettsteder</h1>
{verify_banner}
{trial}
{limit_msg}
{vipps_flash}
<h2 class=sec>Nettsteder <span style="float:right;text-transform:none;letter-spacing:0;font-weight:400">tall for i dag</span></h2>
<div class=card>
<table><tr><th>Nettsted</th><th>Unike</th><th>Visn.</th></tr>
{rows or '<tr><td>ingen nettsteder enda — legg til det første under</td><td></td><td></td></tr>'}</table>
</div>
<form class=add method=post action="/app/sites">
  <input name=domain placeholder="dittdomene.no" required>
  <button class=btn>Legg til nettsted</button>
</form>
{plan_sec}
<h2 class=sec>API-tilgang</h2>
{api_html}
<h2 class=sec>Konto</h2>
{pw_flash}
{password_html}
<p class=fine style="margin-top:1.5rem">Cookieløs · ingen IP lagret · samtykkefri</p>
</div>"""
        )

    period = request.query_params.get("period", "7")
    if period not in _PERIODS:
        period = "7"
    label, days = _PERIODS[period]

    s = store.stats(site["id"], days)
    # Flagg slås opp på engelsk navn FØR oversettelse til norsk visningsnavn
    s["countries"] = [
        {**c, "ikon": icons.flag(c["k"]), "k": country_no(c["k"])} for c in s["countries"]
    ]
    prev = store.kpis(site["id"], days, offset=1)
    series = store.timeseries(site["id"], days)
    events = store.top_events(site["id"], days)
    goals = store.goal_stats(site["id"], days)
    funnels = store.funnel_stats(site["id"], days)
    rollups = store.recent_rollups(site["id"])
    flow = store.flow_stats(site["id"], days)
    transitions = store.path_transitions(site["id"], days)

    # Periodevelger
    tabs = " ".join(
        f'<a href="/app?site={escape(public_id)}&period={k}" class="{"on" if k == period else ""}">{escape(v[0])}</a>'
        for k, v in _PERIODS.items()
    )

    chart = _area_chart(series, days=days)

    table = _stat_table

    # «Steg 2: lim inn koden» — vises ÅPENT øverst så lenge siten ikke har data.
    # Dette er aktiveringssteget; tidligere lå snippeten kun gjemt i en kollapset
    # <details> nederst, og ferske kunder fant den aldri (kunde-klarhets-revisjon).
    _snip = escape(
        f'<script defer data-site="{public_id}" '
        f'data-api="{PUBLIC_BASE}/api/event" src="{PUBLIC_BASE}/sporlos.js"></script>'
    )
    # Gate på ALL-TIME (aldri mottatt event), ikke periode-tomt — ellers får en
    # etablert site «Steg 2: lim inn koden» igjen i en stille uke (review-funn).
    onboard_card = ""
    if store.last_event_at(site["id"]) is None:
        onboard_card = (
            '<div class="card block" style="border:1px solid var(--accent)">'
            "<h3>Steg 2: lim inn sporingskoden</h3>"
            '<p class=muted style="margin:.2rem 0 .6rem">Lim denne rett før '
            "&lt;/head&gt; på sidene du vil måle — så er du i gang. Ingen cookies, "
            "ingen samtykke å sette opp.</p>"
            f'<pre id=snip style="white-space:pre-wrap;word-break:break-all">{_snip}</pre>'
            '<button class=btn onclick="navigator.clipboard.writeText('
            "document.getElementById('snip').textContent).then(()=>{this.textContent='Kopiert ✓'})\" "
            'style="font-size:.9rem;padding:.45rem .9rem">Kopier koden</button>'
            '<p class=muted style="font-size:.82rem;margin:.7rem 0 0">Bruker du WordPress eller '
            'Shopify? <a href="https://wordpress.org/plugins/sporlos-analytics/">WordPress-plugin</a> · '
            '<a href="/shopify">Shopify-guide</a></p></div>'
        )

    # KPI-band v2: unike eier blikket (stort kort m/ sparkline + forseglet-badge),
    # fire sekundære KPI-er ved siden. Tom periode → «scriptet lytter»-tilstand
    # i stedet for nakne nuller, så brukeren vet at innsamlingen fungerer.
    anchored = sum(1 for r in rollups if r.get("txid"))
    hero_badge = ""
    if rollups:
        hero_badge = (
            f'<span class=segl-badge>{_segl_badge(15, "var(--bg)")}'
            f"{anchored}/{len(rollups)} forankret</span>"
        )
    if s["pageviews"] == 0:
        siden = _siden(store.last_event_at(site["id"]))
        livstegn = (
            f"Scriptet er aktivt og lytter — siste livstegn {siden}."
            if siden
            else "Legg inn sporings-koden nederst, så dukker tallene opp her."
        )
        puls = (
            '<span class=puls><span class=dot></span>tilkoblet</span>'
            if siden
            else ""
        )
        kpiband = (
            '<div class=tomt>'
            '<svg class=vm viewBox="0 0 64 64" aria-hidden=true>'
            '<circle cx="32" cy="32" r="16" fill="none" stroke="currentColor" stroke-width="7"/>'
            '<line x1="17" y1="51" x2="47" y2="13" stroke="currentColor" stroke-width="7" stroke-linecap="round"/></svg>'
            f"<b>Ingen besøk målt i {label.lower()}</b><small>{livstegn}</small>{puls}</div>"
        )
    else:
        kpiband = f"""<div class=kpiband>
  <div class="card kpihero">
    <div class=top>
      <div><div class=lbl>Unike besøkende · {escape(label)}</div>
      <div class=big>{_fmt_n(s['visitors'])}</div>
      {_delta(s['visitors'], prev['visitors']) or '<small class="d d0">&nbsp;</small>'}</div>
      {hero_badge}
    </div>
    {chart}
  </div>
  <div class=kpisec>
    <div class="card kpi" title="Én sammenhengende økt — 30 min pause regnes som nytt besøk"><b>{_fmt_n(s['sessions'])}</b><span>besøk</span>{_delta(s['sessions'], prev['sessions'])}</div>
    <div class="card kpi"><b>{_fmt_n(s['pageviews'])}</b><span>sidevisninger</span>{_delta(s['pageviews'], prev['pageviews'])}</div>
    <div class="card kpi" title="Andel besøk som forlot nettstedet etter bare én side — lavere er bedre"><b>{s['bounce_rate']}%</b><span>fluktfrekvens</span>{_delta(s['bounce_rate'], prev['bounce_rate'], invert=True)}</div>
    <div class="card kpi" title="Sidevisninger delt på besøk — hvor dypt folk går"><b>{s['views_per_session']}</b><span>visn. per besøk</span></div>
  </div>
</div>"""

    # Mål / konverteringer
    goal_rows = "".join(
        f'<tr><td>{escape(g["name"])} <small style="color:var(--muted)">'
        f'({escape(g["match_type"])}: {escape(g["match_value"])})</small></td>'
        f'<td>{g["completions"]}</td><td>{g["rate"]}%</td>'
        f'<td><form method=post action="/app/goals/delete" style="display:inline">'
        f'<input type=hidden name=site value="{escape(public_id)}">'
        f'<input type=hidden name=goal_id value="{g["id"]}">'
        '<button title="Slett" style="background:none;border:0;color:var(--err);cursor:pointer">✕</button>'
        "</form></td></tr>"
        for g in goals
    )
    goals_html = (
        "<h3>Mål / konverteringer</h3>"
        '<p class=hint>Et mål teller besøk som når noe du bryr deg om: en side '
        "(f.eks. <code>/takk</code>) eller en hendelse (f.eks. <code>signup</code>). "
        "Rate = andel av alle besøk i perioden.</p>"
        f"<table><tr><th>Mål</th><th>Fullført</th><th>Rate</th><th></th></tr>"
        f"{goal_rows or '<tr><td>ingen mål enda</td><td></td><td></td><td></td></tr>'}</table>"
        '<form method=post action="/app/goals" style="display:flex;gap:.4rem;flex-wrap:wrap;margin:.5rem 0;font-size:.9rem">'
        f'<input type=hidden name=site value="{escape(public_id)}">'
        '<input name=name placeholder="Navn (f.eks. Påmelding)" required style="flex:1;min-width:8rem;padding:.4rem;border:1px solid #ccc;border-radius:6px">'
        '<select name=match_type style="padding:.4rem;border:1px solid #ccc;border-radius:6px"><option value=event>Hendelse</option><option value=path>Sti</option></select>'
        '<input name=match_value placeholder="signup eller /takk" required style="flex:1;min-width:8rem;padding:.4rem;border:1px solid #ccc;border-radius:6px">'
        '<button style="background:#1a1a1a;color:#fff;border:0;padding:0 .8rem;border-radius:6px;cursor:pointer">Legg til mål</button>'
        "</form>"
    )
    # Funnels (steg m/ drop-off)
    frows = ""
    for fu in funnels:
        steprows = "".join(
            f'<tr><td>{i + 1}. {escape(st["value"])} '
            f'<small style="color:var(--muted)">({escape(st["type"])})</small></td>'
            f'<td>{st["count"]}</td><td>{st["rate"]}%</td></tr>'
            for i, st in enumerate(fu["steps"])
        )
        frows += (
            f'<div style="margin:.8rem 0"><b>{escape(fu["name"])}</b> '
            '<form method=post action="/app/funnels/delete" style="display:inline">'
            f'<input type=hidden name=site value="{escape(public_id)}">'
            f'<input type=hidden name=funnel_id value="{fu["id"]}">'
            '<button title="Slett" style="background:none;border:0;color:var(--err);cursor:pointer">✕</button></form>'
            f"<table>{steprows}</table></div>"
        )
    if not frows:
        frows = (
            '<p style="color:var(--muted);font-size:.9rem">Ingen funnels enda. '
            "Eksempel: <code>/</code> → <code>/priser</code> → <code>signup</code> "
            "viser hvor mange som går hele veien — og hvor de faller fra.</p>"
        )
    funnels_html = (
        "<h3>Funnels</h3>"
        '<p class=hint>En funnel følger besøk gjennom en stegvis rekke sider/hendelser '
        "og viser frafallet mellom hvert steg.</p>"
        f"{frows}"
        '<form method=post action="/app/funnels" style="margin:.5rem 0;font-size:.9rem">'
        f'<input type=hidden name=site value="{escape(public_id)}">'
        '<input name=name placeholder="Navn (f.eks. Kjøpstrakt)" required '
        'style="padding:.4rem;border:1px solid #ccc;border-radius:6px;width:100%;box-sizing:border-box;margin-bottom:.4rem">'
        '<textarea name=steps required rows=4 placeholder="Ett steg per linje, i rekkefolge:&#10;/&#10;/priser&#10;signup" '
        'style="width:100%;box-sizing:border-box;padding:.4rem;border:1px solid #ccc;border-radius:6px;font:inherit"></textarea>'
        '<button style="background:#1a1a1a;color:#fff;border:0;padding:.4rem .8rem;border-radius:6px;cursor:pointer;margin-top:.4rem">Lag funnel</button>'
        '<div style="color:var(--muted);font-size:.8rem">Linjer som starter med / = sti, ellers = hendelse. Min. 2 steg.</div>'
        "</form>"
    )
    nav_rows = "".join(
        f'<tr><td title="{escape(tr["from"])} → {escape(tr["to"])}">'
        f'{escape(tr["from"])} → {escape(tr["to"])}</td><td>{tr["n"]}</td></tr>'
        for tr in transitions
    )
    nav_html = (
        "<h3>Navigasjonsstier</h3>"
        '<p style="color:var(--muted);font-size:.85rem">Vanligste side→side-overganger innen en økt.</p>'
        "<table><tr><th>Fra → Til</th><th>Antall</th></tr>"
        f"{nav_rows or '<tr><td>ingen overganger enda</td><td></td></tr>'}</table>"
    )
    event_rows = "".join(
        f'<tr><td>{escape(e["k"])}</td><td>{e["u"]}</td><td>{e["n"]}</td></tr>' for e in events
    )
    events_html = (
        "<h3>Hendelser</h3>"
        '<p class=hint>Egendefinerte hendelser du selv sender: '
        "<code>sporlos('navn')</code> i JS, eller <code>data-sporlos-event=\"navn\"</code> "
        'på en knapp/lenke. Kjøp med beløp/produkter: se <a href="/utviklere">E-handel</a>.</p>'
        "<table><tr><th>Hendelse</th><th>Unike</th><th>Totalt</th></tr>"
        f"{event_rows or '<tr><td>ingen hendelser enda</td><td></td><td></td></tr>'}</table>"
    )
    # Verifiserbare tall (B): forseglet hash per dag, status forankret/venter
    verify_html = _verify_table(rollups, public_id)

    # Kampanjer (UTM) — vises kun når det finnes kampanjetrafikk i perioden.
    camp_rows = "".join(
        f"<tr><td>{escape(' · '.join(x for x in (c['source'], c['medium'], c['campaign']) if x) or 'ukjent')}</td>"
        f"<td style='text-align:right;color:var(--muted);width:5rem'>{c['visitors']}</td>"
        f"<td style='text-align:right;color:var(--muted);width:5rem'>{c['n']}</td></tr>"
        for c in s["campaigns"]
    )
    campaigns_html = ""
    if camp_rows:
        campaigns_html = (
            "<h3>Kampanjer (UTM)</h3>"
            "<table><tr><th style='text-align:left;color:var(--muted);font-size:.85rem'>Kilde · medium · kampanje</th>"
            "<th style='text-align:right;color:var(--muted);font-size:.85rem'>Unike</th>"
            f"<th style='text-align:right;color:var(--muted);font-size:.85rem'>Visn.</th></tr>{camp_rows}</table>"
        )

    # E-handel — seksjonen finnes kun for sites som faktisk har målt kjøp (all-time),
    # så en rolig uke ikke får seksjonen til å forsvinne, og ikke-butikker slipper støy.
    ecom_html = ""
    if store.has_ecommerce(site["id"]):
        ec = store.ecommerce_stats(site["id"], days)
        ec_prev = store.ecommerce_stats(site["id"], days, offset=1)
        # Dominerende valuta styrer KPI-er og tabeller — valutaer blandes aldri.
        dom = ec["by_currency"][0]["currency"] if ec["by_currency"] else "NOK"
        row = next((r for r in ec["by_currency"] if r["currency"] == dom), None)
        prow = next((r for r in ec_prev["by_currency"] if r["currency"] == dom), None)
        rev = row["revenue_cents"] if row else 0
        orders = row["orders"] if row else 0
        prev_rev = prow["revenue_cents"] if prow else 0
        prev_orders = prow["orders"] if prow else 0
        aov = round(rev / orders) if orders else 0
        prev_aov = round(prev_rev / prev_orders) if prev_orders else 0

        if orders:
            stat = (
                '<div style="display:flex;gap:1.8rem;flex-wrap:wrap;margin:.4rem 0 1rem">'
                f'<div><div style="font-size:1.45rem;font-weight:700">{_fmt_kr(rev, dom)}</div>'
                f'<small style="color:var(--muted)">omsetning</small> {_delta(rev, prev_rev)}</div>'
                f'<div><div style="font-size:1.45rem;font-weight:700">{_fmt_n(orders)}</div>'
                f'<small style="color:var(--muted)">ordrer</small> {_delta(orders, prev_orders)}</div>'
                f'<div><div style="font-size:1.45rem;font-weight:700">{_fmt_kr(aov, dom)}</div>'
                f'<small style="color:var(--muted)">snittordre</small> {_delta(aov, prev_aov)}</div>'
                "</div>"
            )
            prod_rows = "".join(
                f"<tr><td>{escape(p['name'])}</td>"
                f"<td style='text-align:right;color:var(--muted)'>{_fmt_n(p['qty'])}</td>"
                f"<td style='text-align:right'>{_fmt_kr(p['revenue_cents'], dom)}</td></tr>"
                for p in store.top_products(site["id"], days, dom)
            )
            src_rows = "".join(
                f"<tr><td>{escape(sr['src'])}</td>"
                f"<td style='text-align:right;color:var(--muted)'>{_fmt_n(sr['orders'])}</td>"
                f"<td style='text-align:right'>{_fmt_kr(sr['revenue_cents'], dom)}</td></tr>"
                for sr in store.revenue_by_source(site["id"], days, dom)
            )
            tables = (
                '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:1rem">'
                "<div><table><tr><th>Produkt</th><th style='text-align:right'>Antall</th>"
                f"<th style='text-align:right'>Omsetning</th></tr>"
                f"{prod_rows or '<tr><td>kjøp uten produktlinjer</td><td></td><td></td></tr>'}</table></div>"
                "<div><table><tr><th>Kilde</th><th style='text-align:right'>Ordrer</th>"
                f"<th style='text-align:right'>Omsetning</th></tr>{src_rows}</table>"
                '<p style="color:var(--muted);font-size:.78rem;margin:.4rem 0 0">Kilde = besøkerens '
                "første kilde i samme døgn (UTC) — hashen roterer ved midnatt, så attribusjon "
                "krysser aldri døgn.</p>"
                "</div></div>"
            )
            other_orders = ec["orders"] - orders
            if other_orders:
                tables += (
                    f'<p style="color:var(--muted);font-size:.8rem">+ {other_orders} '
                    "ordrer i andre valutaer — full liste i API-et.</p>"
                )
            body = stat + tables
        else:
            body = f'<p style="color:var(--muted);font-size:.9rem">Ingen kjøp målt i {label.lower()}.</p>'
        ecom_html = (
            "<h3>E-handel</h3>"
            "<p class=hint>Kjøp sendes med <code>sporlos('purchase', {…})</code> — kun beløp og "
            "produktnavn, uten ordre-ID eller kundedata. Nettleser-rapporterte tall: "
            "veiledende, ikke avregningsgrunnlag. "
            '<a href="/utviklere">Slik sender du kjøp</a>.</p>'
            + body
        )

    blocks = "".join(
        f'<div class="card block">{b}</div>'
        for b in (ecom_html, campaigns_html, goals_html, funnels_html, nav_html, events_html, verify_html)
        if b
    )

    # Opt-in offentlig dashboard (delbar lenke, som /demo) — av som standard
    pub_on = bool((store.get_public_site(public_id) or {}).get("public_dash"))
    toggle_btn = (
        f'<form method=post action="/app/sites/public" style="display:inline;margin-left:.6rem">'
        f'<input type=hidden name=site value="{escape(public_id)}">'
        f'<input type=hidden name=on value="{0 if pub_on else 1}">'
        '<button class=btn style="font-size:.8rem;padding:.25rem .6rem">'
        f'{"Skru av delingen" if pub_on else "Del med åpen lenke"}</button></form>'
    )
    if pub_on:
        pub_text = (
            '<span style="color:var(--ok)">● Delt.</span> Alle med lenken '
            f'<a href="/p/{escape(public_id)}">sporlos.no/p/{escape(public_id)}</a> ser tallene — '
            "read-only, uten innlogging."
        )
    else:
        pub_text = (
            "Ikke delt — bare du ser tallene. Deling gir en åpen, read-only lenke "
            "(som vår egen <a href=/demo>live-demo</a>) du kan gi til styre, kunder eller annonsører."
        )
    public_html = (
        '<div class="card block"><h3>Offentlig dashboard</h3>'
        f'<p class=muted style="font-size:.85rem">{pub_text}{toggle_btn}</p></div>'
    )

    return HTMLResponse(
        f"""<!doctype html><html lang=no><meta charset=utf-8>
<title>Sporløs — {escape(site['domain'])}</title>
<meta name=viewport content="width=device-width, initial-scale=1">
{_BRAND_HEAD}
<style>{_BRAND_CSS}{_DARK_CSS}{_DASH_CSS}</style>
{_THEME_HEAD}
<div class=wrap>
<nav>{_WORDMARK}<div class=links>{_THEME_BTN}<a href="/app">Mine sites</a><a href="/logout">Logg ut</a></div></nav>
{verify_banner}
<div class=head><h1>{escape(site['domain'])}</h1><div class=tabs>{tabs}</div></div>
{kpiband}
{onboard_card}
<p class=muted style="font-size:.8rem;margin:.3rem 0 .9rem">Last ned CSV (regneark):
  <a href="/app/export?site={escape(public_id)}&period={period}&what=tidsserie">tidsserie</a> ·
  <a href="/app/export?site={escape(public_id)}&period={period}&what=sider">sider</a> ·
  <a href="/app/export?site={escape(public_id)}&period={period}&what=kilder">kilder</a> ·
  <a href="/app/export?site={escape(public_id)}&period={period}&what=land">land</a>
  · <a href="#" id=barstoggle>andelssøyler av/på</a></p>
<div class=grid>
  <div class=card><h3>Topp sider</h3>{table(s['top_paths'], 'path')}</div>
  <div class=card><h3>Topp kilder</h3><p class=hint>hvor trafikken kommer fra — «direkte» = skrev inn adressen eller bokmerke</p>{table(s['top_sources'], 'src')}</div>
  <div class=card><h3>Inngangssider</h3><p class=hint>første side i besøket — der folk lander</p>{table(flow['entries'], 'path')}</div>
  <div class=card><h3>Utgangssider</h3><p class=hint>siste side før de dro — se etter lekkasjer</p>{table(flow['exits'], 'path')}</div>
  <div class=card><h3>Land</h3>{table(s['countries'], 'k')}</div>
  <div class=card><h3>Fylke / region</h3>{table(s['regions'], 'k')}</div>
  <div class=card><h3>Enheter</h3>{table(s['devices'], 'k', icons.device)}</div>
  <div class=card><h3>Nettlesere</h3>{table(s['browsers'], 'k', icons.browser)}</div>
  <div class=card><h3>Operativsystem</h3>{table(s['os'], 'k', icons.os)}</div>
</div>
{blocks}
<div class="card block"><details><summary>Vis sporings-kode</summary>
<pre>{escape(f'<script defer data-site="{public_id}" data-api="{PUBLIC_BASE}/api/event" src="{PUBLIC_BASE}/sporlos.js"></script>')}</pre>
<p class=muted style="font-size:.82rem;margin:.5rem 0 0">Plattform-guider:
<a href="https://wordpress.org/plugins/sporlos-analytics/">WordPress</a> ·
<a href="/shopify">Shopify</a></p></details></div>
{public_html}
<p class=footnote>Cookieløs · ingen IP lagret · samtykkefri ·
Geo: <a href="https://db-ip.com">IP Geolocation by DB-IP</a> (CC BY 4.0)</p>
</div>
{_BARS_JS}
{_CHART_JS}"""
    )


routes = [
    Route("/healthz", healthz),
    Route("/healthz/db", healthz_db),
    Route("/sporlos.js", tracker),
    Route("/api/event", ingest, methods=["POST"]),
    Route("/", landing),
    Route("/vilkar", vilkar),
    Route("/personvern", personvern),
    Route("/google-analytics-alternativ", ga_alternativ),
    Route("/demo", demo),
    Route("/p/{public_id}", public_dash),
    Route("/app/sites/public", site_public_toggle, methods=["POST"]),
    Route("/robots.txt", robots),
    Route("/sitemap.xml", sitemap),
    Route("/llms.txt", llms_txt),
    Route("/favicon.svg", favicon),
    Route("/favicon.ico", favicon_ico),
    Route("/apple-touch-icon.png", apple_icon),
    Route("/site.webmanifest", webmanifest),
    Route("/static/schibsted-grotesk.woff2", brand_font),
    Route("/static/og.png", og_image),
    # Resten av static/ (favicon-PNG-er, brand-logoer) — eksplisitte ruter over vinner.
    Mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static"),
    Route("/signup", signup, methods=["GET", "POST"]),
    Route("/login", login, methods=["GET", "POST"]),
    Route("/registrer", _alias("/signup")),
    Route("/logg-inn", _alias("/login")),
    Route("/sammenligning", _alias("/google-analytics-alternativ")),
    Route("/priser", _alias("/#priser")),
    Route("/forgot", forgot, methods=["GET", "POST"]),
    Route("/reset", reset, methods=["GET", "POST"]),
    Route("/unsubscribe", unsubscribe),
    Route("/verify", verify_email),
    Route("/resend-verify", resend_verify),
    Route("/logout", logout),
    Route("/auth/google", google_login),
    Route("/auth/google/callback", google_callback, name="google_callback"),
    Route("/auth/innlogg", innlogg_login),
    Route("/auth/innlogg/callback", innlogg_callback, name="innlogg_callback"),
    Route("/betal", betal),
    Route("/billing/checkout", billing_checkout),
    Route("/billing/portal", billing_portal),
    Route("/api/hero", hero_stats),
    Route("/proof", proof),
    Route("/sporsmal", sporsmal),
    Route("/assist.js", assist_js),
    Route("/api/assist", assist_api, methods=["POST"]),
    Route("/billing/vipps/start", vipps_start),
    Route("/billing/vipps/retur", vipps_return),
    Route("/billing/vipps/avslutt", vipps_cancel, methods=["POST"]),
    Route("/webhooks/stripe", stripe_webhook, methods=["POST"]),
    Route("/webhooks/shopify/compliance", shopify_compliance, methods=["POST"]),
    Route("/app", dashboard),
    Route("/app/export", export_csv),
    Route("/app/api-keys", api_key_create, methods=["POST"]),
    Route("/app/api-keys/revoke", api_key_revoke, methods=["POST"]),
    Route("/app/password", change_password, methods=["POST"]),
    Route("/utviklere", utviklere),
    Route("/shopify", shopify_guide),
    Route("/integrasjoner", integrasjoner),
    Route("/integrasjoner/{slug}", platform_guide),
    Route("/blogg", blogg_index),
    Route("/blogg/rss.xml", blogg_rss),  # må stå FØR {slug}-ruta
    Route("/blogg/{slug}", blogg_post),
    Route("/api/v1/sites", api.sites),
    Route("/api/v1/stats", api.stats),
    Route("/api/v1/timeseries", api.timeseries),
    Route("/api/v1/breakdown", api.breakdown),
    Route("/api/v1/goals", api.goals),
    Route("/api/v1/events", api.events),
    Route("/api/v1/ecommerce", api.ecommerce),
    Route("/api/v1/anchors", api.anchors),
    Route("/app/sites", create_site_post, methods=["POST"]),
    Route("/app/goals", goal_create, methods=["POST"]),
    Route("/app/goals/delete", goal_delete, methods=["POST"]),
    Route("/app/funnels", funnel_create, methods=["POST"]),
    Route("/app/funnels/delete", funnel_delete, methods=["POST"]),
]

# Ingestion må ta imot cross-origin beacons fra ethvert kunde-domene.
# Trygt her fordi vi aldri bruker cookies/credentials (cookieløst by design).
middleware = [
    Middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST"],
        allow_headers=["content-type", "authorization"],
    ),
    Middleware(
        SessionMiddleware,
        secret_key=SESSION_SECRET,
        https_only=HTTPS_ONLY,
        same_site="lax",
    ),
    # Komprimer HTML/CSS/JSON (~70-80% mindre) på markedsførings- og /app-sider.
    # minimum_size hopper over de bittesmå beacon-svarene (POST /api/v1/events).
    Middleware(GZipMiddleware, minimum_size=500),
]

app = Starlette(routes=routes, middleware=middleware)
