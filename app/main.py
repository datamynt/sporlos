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
import secrets
import threading
import time
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
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

from app import api, assist, blogg, icons, innlogg, mailer, notify, store, vipps
from app.auth import check_token, hash_password, verify_password
from app.datacenter import is_datacenter
from app.geo import country_no
from app.geo import lookup as geo_lookup
from app.pathmask import mask_label, mask_path
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


# Absolute session lifetime. The cookie is re-signed on every response, so without
# this a session used at least every 14 days would never expire.
SESSION_MAX_AGE = 30 * 86400


def _login(request, uid: int, tid: int) -> None:
    """Start a fresh session for this user, stamped with its session version."""
    request.session.clear()
    request.session.update(
        uid=uid, tid=tid, sv=store.session_version(uid) or 0, iat=int(time.time())
    )


def _user(request):
    """Innlogget bruker fra session, eller None.

    A session is valid only while the user exists, its version matches (a password
    change or reset bumps it, which logs out every other session) and it is younger
    than SESSION_MAX_AGE."""
    uid, tid = request.session.get("uid"), request.session.get("tid")
    if not (uid and tid):
        return None
    iat = request.session.get("iat")
    if iat is None:  # issued before sessions carried a timestamp: start the clock now
        request.session["iat"] = iat = int(time.time())
    if time.time() - iat > SESSION_MAX_AGE or store.session_version(uid) != request.session.get("sv", 0):
        request.session.clear()
        return None
    return {"uid": uid, "tid": tid}


# Google OAuth app for «Koble til Search Console» (per-site GSC access with the
# customer's own Google account). Login with Google/Microsoft is separate and goes
# through Datamynt ID, see app/innlogg.py and the /auth/sso routes.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
_HAS_GOOGLE = bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)
oauth = None
if _HAS_GOOGLE:
    from authlib.integrations.starlette_client import OAuth

    oauth = OAuth()
    # «Koble til Search Console»-knappen: ekstra scope +
    # offline access (refresh-token). prompt=consent tvinger frem refresh-
    # token også ved re-kobling (Google utelater det ellers ved re-samtykke).
    oauth.register(
        name="gscconn",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={
            "scope": "openid email https://www.googleapis.com/auth/webmasters.readonly"
        },
        authorize_params={"access_type": "offline", "prompt": "consent"},
    )


# Provider marks, inline so the login page loads nothing from Google or Microsoft.
_SSO_ICONS = {
    "google": (
        '<svg viewBox="0 0 48 48" aria-hidden=true>'
        '<path fill="#FFC107" d="M43.6 20.5H42V20H24v8h11.3C33.7 32.7 29.2 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.9 1.2 8 3.1l5.7-5.7C34 6.1 29.3 4 24 4 12.9 4 4 12.9 4 24s8.9 20 20 20 20-8.9 20-20c0-1.3-.1-2.4-.4-3.5z"/>'
        '<path fill="#FF3D00" d="M6.3 14.7l6.6 4.8C14.7 15.1 19 12 24 12c3.1 0 5.9 1.2 8 3.1l5.7-5.7C34 6.1 29.3 4 24 4 16.3 4 9.7 8.3 6.3 14.7z"/>'
        '<path fill="#4CAF50" d="M24 44c5.2 0 9.9-2 13.4-5.2l-6.2-5.2C29.2 35.1 26.7 36 24 36c-5.2 0-9.6-3.3-11.3-8l-6.5 5C9.5 39.6 16.2 44 24 44z"/>'
        '<path fill="#1976D2" d="M43.6 20.5H42V20H24v8h11.3c-.8 2.2-2.2 4.2-4.1 5.6l6.2 5.2C36.9 39.2 44 34 44 24c0-1.3-.1-2.4-.4-3.5z"/>'
        "</svg>"
    ),
    "microsoft": (
        '<svg viewBox="0 0 21 21" aria-hidden=true>'
        '<path fill="#f25022" d="M1 1h9v9H1z"/><path fill="#7fba00" d="M11 1h9v9h-9z"/>'
        '<path fill="#00a4ef" d="M1 11h9v9H1z"/><path fill="#ffb900" d="M11 11h9v9h-9z"/>'
        "</svg>"
    ),
}

_SSO_CSS = """
.sso{margin-top:1.4rem}
.sso-or{display:flex;align-items:center;gap:.7rem;color:var(--muted);font-size:.8rem;margin-bottom:.8rem}
.sso-or::before,.sso-or::after{content:"";flex:1;border-top:1px solid var(--line)}
a.sso-btn{display:flex;align-items:center;justify-content:center;gap:.6rem;border:1px solid var(--line);
border-radius:8px;padding:.62rem;margin-top:.55rem;text-decoration:none;color:var(--ink);
background:var(--card);font-weight:600;font-size:.95rem;transition:border-color .15s}
a.sso-btn:hover{border-color:var(--muted)}
a.sso-btn svg{width:18px;height:18px;flex:none}
a.sso-link{display:inline-flex;align-items:center;gap:.45rem;color:var(--ink);font-size:.9rem;text-decoration:none;
border:1px solid var(--line);border-radius:8px;padding:.4rem .7rem;background:var(--card)}
a.sso-link:hover{border-color:var(--muted)}
a.sso-link svg,#konto .fine svg{width:16px;height:16px;vertical-align:-3px}
"""


def _sso_buttons(plan: str = "") -> str:
    """«Fortsett med Google/Microsoft» — empty when Datamynt ID isn't configured."""
    providers = [p for p in ("google", "microsoft") if innlogg.enabled(p)]
    if not providers:
        return ""
    q = f"?plan={plan}" if plan in _PLAN_LABELS else ""
    links = "".join(
        f'<a class=sso-btn href="/auth/sso/start/{p}{q}">{_SSO_ICONS[p]}'
        f"<span>Fortsett med {innlogg.LABELS[p]}</span></a>"
        for p in providers
    )
    return f'<div class=sso><div class=sso-or>eller</div>{links}</div>'


# Messages for /login?sso=… (the flow redirects there on anything but success).
_SSO_MESSAGES = {
    "feil": "Innloggingen feilet. Prøv igjen, eller bruk e-post og passord.",
    "avbrutt": "Innloggingen ble avbrutt.",
    "epost": "Kontoen hos leverandøren har ingen e-postadresse vi kan bruke.",
    "for-mange": "For mange forsøk akkurat nå. Prøv igjen om en time.",
}


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

# Tak på hvor mange bekreftelses-e-poster /signup kan utløse. Romslig for ekte
# bruk (mobilnett deler IP via CGNAT), stramt nok til å drepe en liste-bot som
# tygger 2 adresser hver halvtime — se store.signup_attempts.
SIGNUP_PER_IP_HOURLY = 5
SIGNUP_GLOBAL_HOURLY = 40

# Tak på /forgot — samme misbruksmønster som /signup (se over), pluss et
# per-mål-tak: uten det kan en angriper spamme ÉN ekte innboks med
# tilbakestillings-e-post uendelig, selv fra mange IP-er. 3/time/e-post er
# romslig for en ekte bruker som roter med passordet, stramt nok til at
# trakassering av ett offer ikke skalerer — se store.forgot_attempts.
FORGOT_PER_IP_HOURLY = 5
# Password guessing: a person who mistypes a few times never gets near these.
LOGIN_FAILS_PER_IP_HOURLY = 30
LOGIN_FAILS_PER_EMAIL_HOURLY = 10
FORGOT_PER_EMAIL_HOURLY = 3
FORGOT_GLOBAL_HOURLY = 40
stripe = None
if STRIPE_SECRET:
    import stripe as _stripe

    _stripe.api_key = STRIPE_SECRET
    stripe = _stripe

# Tracker-scriptet leses én gang ved oppstart og serveres på /sporlos.js.
_TRACKER_SRC = (Path(__file__).resolve().parent.parent / "tracker" / "sporlos.js").read_text()


def _load_tracker() -> str:
    """The minified build, but only while it was built from the source on disk.
    scripts/build_tracker.py stamps a short sha256 of the source into the first
    line; on a mismatch (someone edited sporlos.js and forgot to rebuild) the
    readable source is served instead, so stale tracking logic never ships."""
    path = Path(__file__).resolve().parent.parent / "tracker" / "sporlos.min.js"
    try:
        built = path.read_text()
    except OSError:
        return _TRACKER_SRC
    want = "src:" + hashlib.sha256(_TRACKER_SRC.encode()).hexdigest()[:16]
    if want in built.split("\n", 1)[0]:
        return built
    log.warning("tracker/sporlos.min.js is stale — serving the unminified source")
    return _TRACKER_SRC


_TRACKER = _load_tracker()
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


def healthz_db(request):
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


async def tracker_source(request):
    """The readable source of what /sporlos.js runs — «etterprøv selv»."""
    return Response(
        _TRACKER_SRC,
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


def hero_stats(request):
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
<meta property="og:title" content="Spørsmål og svar om cookieløs webanalyse | Sporløs">
<meta property="og:description" content="Trenger du cookie-banner? Er Google Analytics lovlig i Norge? Ærlige svar om cookieløs, samtykkefri webanalyse.">
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
{_site_nav(request)}
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
    visitor = visitor_hash(ip, ua, "assist", secret=SECRET, day_salt=store.daily_salt())
    # LLM-kallet tar sekunder — av tråden så event-loopen ikke blokkerer ingest.
    ans, status = await asyncio.to_thread(assist.answer, str(data.get("q", "")), history, visitor)
    return JSONResponse({"a": ans}, status_code=status)


async def ingest(request):
    """POST /api/event — fra tracker-snippet. Beregner cookieløs hash, lagrer."""
    # A real beacon is a few hundred bytes; the largest legitimate one (25 product
    # lines) is about 5 kB. Nothing reads an unbounded body into memory.
    try:
        if int(request.headers.get("content-length") or 0) > _MAX_EVENT_BYTES:
            return JSONResponse({"error": "too large"}, status_code=413)
        raw = await request.body()
        if len(raw) > _MAX_EVENT_BYTES:
            return JSONResponse({"error": "too large"}, status_code=413)
        payload = json.loads(raw)
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "bad json"}, status_code=400)

    # The rest is blocking work (database, geo lookup). Off the event loop, so a
    # slow query never makes other visitors' beacons wait in line.
    return await asyncio.to_thread(
        _ingest_store, payload, dict(request.headers),
        request.client.host if request.client else "",
    )


_MAX_EVENT_BYTES = 16_384
_MAX_EVENTS_PER_MINUTE = 120  # per visitor hash; far beyond a person clicking around
_rate_lock = threading.Lock()
_rate_minute = 0
_rate_counts: dict[str, int] = {}


def _over_rate(vhash: str) -> bool:
    """Fixed one-minute window per visitor hash, in memory. A script stuck in a
    loop (or someone replaying a beacon) must not be able to burn through a
    customer's monthly pageview quota. The table is dropped every minute, so it
    holds nothing that outlives the window."""
    global _rate_minute
    minute = int(time.time() // 60)
    with _rate_lock:
        if minute != _rate_minute:
            _rate_minute = minute
            _rate_counts.clear()
        n = _rate_counts.get(vhash, 0) + 1
        _rate_counts[vhash] = n
    return n > _MAX_EVENTS_PER_MINUTE


def _clean_name(v) -> str:
    """Event name: a short string, or it is a pageview. A name is free text, so an
    ID-like word in it (an order number, say) is masked like a path segment."""
    if not isinstance(v, str) or not v.strip():
        return "pageview"
    return mask_label(v.strip()[:64])


def _clean_path(v) -> str:
    """Path only. The tracker never sends a query string, but the endpoint is
    public and other senders (plugins, custom code) may pass a full URL. Query
    strings and fragments are where e-mail addresses and tokens hide, so they
    are cut here, on the server, where the promise can actually be enforced.
    ID-like segments (order numbers, tokens, UUIDs) become `:id` for the same
    reason: on many sites the path itself is the key (app/pathmask.py)."""
    if not isinstance(v, str):
        return "/"
    v = v.strip()
    if "://" in v[:10]:
        from urllib.parse import urlparse

        v = urlparse(v).path
    v = v.split("?", 1)[0].split("#", 1)[0]
    if not v.startswith("/"):
        return "/"
    return mask_path(v[:512])


def _ingest_store(payload: dict, headers: dict, client_host: str):
    public_id = payload.get("s")
    if not isinstance(public_id, str) or len(public_id) > 64:
        return JSONResponse({"error": "unknown site"}, status_code=404)
    try:
        site = store.resolve_site_cached(public_id) if public_id else None
    except Exception:
        # DB nede e.l. — ikke la beaconen få 500; vi kan uansett ikke lagre nå.
        log.exception("ingest: resolve_site feilet")
        return PlainTextResponse("", status_code=204)
    if not site:
        return JSONResponse({"error": "unknown site"}, status_code=404)

    ua = headers.get("user-agent", "")
    # Bots/scripts telles ikke — aksepter stille (204) men lagre ingenting.
    if is_bot(ua):
        return PlainTextResponse("", status_code=204)

    ip = client_ip(headers, fallback=client_host)
    # Datasenter-trafikk (crawlere m/ vanlig UA) telles heller ikke.
    if is_datacenter(ip):
        return PlainTextResponse("", status_code=204)
    try:
        day_salt = store.daily_salt()
    except Exception:
        # Same reasoning as resolve_site above: no salt means no storable event.
        log.exception("ingest: daily_salt feilet")
        return PlainTextResponse("", status_code=204)
    vhash = visitor_hash(ip, ua, str(site["id"]), secret=SECRET, day_salt=day_salt)
    device, browser, os_ = parse_ua(ua)
    country, region = geo_lookup(ip)  # land + fylke, by-nivå brukes aldri
    # ip og ua brukes KUN her (hash + kategorisering + geo) — aldri lagret.

    if _over_rate(vhash):
        return PlainTextResponse("", status_code=204)

    name = _clean_name(payload.get("n"))
    # E-handel: valgfri ordresum/produktlinjer på egendefinerte hendelser (aldri pageview).
    revenue, currency, items, payment = None, None, [], None
    if name != "pageview":
        revenue = _clean_money(payload.get("rv"))
        items = _clean_items(payload.get("it"))
        if revenue is not None or items:
            payment = _clean_payment(payload.get("pm"))
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
                "path": _clean_path(payload.get("p", "/")),
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
                "payment_method": payment,
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


def _clean_payment(v) -> str | None:
    """Betalingsmåte fra tracker («vipps», «stripe_card», «klarna» …): lav-slug,
    maks 32 tegn av [a-z0-9_-]. Alt annet avvises — feltet skal aldri kunne
    bære ordre-ID eller kundedata (samme grunn som at sku-felt ikke finnes)."""
    if not isinstance(v, str):
        return None
    s = v.strip().lower()
    if s and len(s) <= 32 and all(c.isascii() and (c.isalnum() or c in "_-") for c in s):
        return s
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

        if not isinstance(ref, str):
            return None
        # hostname, not netloc: netloc keeps "user:password@" and the port.
        return (urlparse(ref[:2048]).hostname or "")[:253] or None
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

# Bump when the icon files change: they are cached for a week, so browsers that
# fetched the short-lived kit mark (28.09.2026) would keep showing it otherwise.
_ICON_V = "?v=blekk2"

_BRAND_HEAD = (
    # Modern: skarp SVG. Fallback: ICO (Google) + PNG (crawlere uten SVG). Apple + PWA.
    f'<link rel="icon" type="image/svg+xml" href="/favicon.svg{_ICON_V}">'
    f'<link rel="icon" href="/favicon.ico{_ICON_V}" sizes="48x48">'
    f'<link rel="icon" type="image/png" sizes="48x48" href="/static/brand/favicon-48.png{_ICON_V}">'
    f'<link rel="icon" type="image/png" sizes="96x96" href="/static/brand/favicon-96.png{_ICON_V}">'
    f'<link rel="apple-touch-icon" href="/apple-touch-icon.png{_ICON_V}">'
    f'<link rel="manifest" href="/site.webmanifest{_ICON_V}">'
    '<meta name=theme-color content="#faf9f6" media="(prefers-color-scheme: light)">'
    '<meta name=theme-color content="#121a2b" media="(prefers-color-scheme: dark)">'
    # Theme: apply the stored choice before first paint (no flash) and define the toggle
    # used by the theme button in the shared header (logged-in variant). It is defined on
    # every page, so the button works wherever the header shows it.
    "<script>(function(){var k='sporlosTema',r=document.documentElement;"
    "try{var t=localStorage.getItem(k);if(t)r.setAttribute('data-theme',t)}catch(e){}"
    "window.byttTema=function(){var d=window.matchMedia('(prefers-color-scheme:dark)').matches,"
    "c=r.getAttribute('data-theme')||(d?'dark':'light'),n=c==='dark'?'light':'dark';"
    "try{localStorage.setItem(k,n)}catch(e){}r.setAttribute('data-theme',n)}})();</script>"
)

# Dark tokens live next to the light ones, so EVERY page that loads _BRAND_CSS follows
# the theme — the public site used to stay light while the dashboard went dark.
_DARK_VARS = (
    "--bg:#121a2b;--card:#19233a;--line:#283450;--ink:#e9edf6;--muted:#9aa6bf;"
    "--accent:#7da2ff;--accent-deep:#8fb0ff;--ok:#4ade80;"
    "--bar:#22335a;--ok-bg:#10302a;--ok-ink:#6ee7a8;--err:#f58a8a;--err-bg:#371b21;"
    "--info:#aebcff;--info-bg:#1b2843;--warn:#e3b341;--warn-bg:#33280f;"
    "--btn-bg:#2f6fed;--btn-bg-h:#1d4ed8;--footer:#0c1220;color-scheme:dark"
)
# Manuell overstyring (data-theme) + auto (systeminnstilling, med mindre manuelt lyst).
_DARK_CSS = f"""
:root[data-theme="dark"]{{{_DARK_VARS}}}
@media (prefers-color-scheme:dark){{:root:not([data-theme="light"]){{{_DARK_VARS}}}}}
"""

_BRAND_CSS = """
@font-face{font-family:'Schibsted Grotesk';font-style:normal;font-weight:400 900;
font-display:swap;src:url(/static/schibsted-grotesk.woff2) format('woff2')}
:root{--bg:#faf9f6;--ink:#17263e;--footer:#17263e;--muted:#5f6b7d;--accent:#2f6fed;--accent-deep:#1d4ed8;
--line:#e8e6e0;--card:#ffffff;--ok:#15803d;
--bar:#e9effd;--ok-bg:#ecfdf5;--ok-ink:#065f46;--err:#b91c1c;--err-bg:#fef2f2;
--info:#3730a3;--info-bg:#eef2ff;--warn:#a16207;--warn-bg:#fff7ed;
--btn-bg:#17263e;--btn-bg-h:#0e1a2e;--accent-fill:#2f6fed;--accent-fill-h:#1d4ed8;color-scheme:light;
font:17px/1.65 'Schibsted Grotesk',system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--ink)}
html{overflow-y:scroll;scrollbar-gutter:stable}
body{margin:0;background:var(--bg);-webkit-font-smoothing:antialiased}
:where(a,button,summary,input,select,textarea):focus-visible{outline:2px solid var(--accent);outline-offset:2px}
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
""" + _DARK_CSS

# Sporløs måler sporlos.no med Sporløs — definert ÉN gang, brukt i alle templates.
_SELF_SNIPPET = (
    '<script defer data-site="6LIACtOSP-S7" data-api="https://sporlos.no/api/event" '
    'src="https://sporlos.no/sporlos.js"></script>'
)
# Assistenten rir på samme injeksjonspunkt — vises kun når LLM-nøkkel er satt.
# ?v= buster 1t-cachen ved widget-endringer — bump ved endring i assist/widget.js.
if assist.configured():
    _SELF_SNIPPET += '<script defer src="/assist.js?v=3"></script>'

# ONE frame for every page, public and logged-in: the header, the content and the footer
# all sit inside .wrap, so the logo and the nav links never move when you navigate.
# Narrower content (.content, .auth) is centred INSIDE that same frame.
_CHROME_CSS = """
.wrap{max-width:980px;margin:0 auto;padding:0 1.3rem}
.content{max-width:680px;margin:0 auto;padding-bottom:1rem}
/* A wide table scrolls inside itself on a phone; it must never widen the page sideways. */
@media(max-width:760px){.content table{display:block;max-width:100%;overflow-x:auto}}
/* Header. Fixed height: the logged-out and logged-in variants, and every page, put the logo
   and the links on exactly the same pixels (2.65rem = the height of the "Prøv gratis" button). */
nav.site{position:relative;display:flex;align-items:center;justify-content:space-between;gap:.8rem;
box-sizing:content-box;height:2.65rem;padding:1.4rem 0}
nav.site .navr,nav.site .navmenu{display:flex;align-items:center;gap:1.2rem}
nav.site .nl{color:var(--muted);font-size:.95rem;line-height:1.3;text-decoration:none;padding:.35rem 0;
white-space:nowrap;background:none;border:0;font-family:inherit;cursor:pointer;
margin:0;width:auto;font-weight:inherit;border-radius:0;text-align:left}
nav.site .nl:hover,nav.site .nl[aria-current=page]{color:var(--ink)}
nav.site .btn{padding:.5rem 1rem;white-space:nowrap}
nav.site .tema{display:inline-flex;align-items:center;justify-content:center;gap:.6rem;width:2rem;height:2rem;
padding:0;border:1px solid var(--line);border-radius:99px;background:none;color:var(--muted);
font:inherit;font-size:.95rem;cursor:pointer}
nav.site .tema:hover{color:var(--ink);border-color:var(--muted)}
nav.site .tema svg{width:1rem;height:1rem;flex:none}
nav.site .tl{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
nav.site form.logout{display:contents}
/* Phone menu without JavaScript: a hidden checkbox toggles the panel. */
nav.site .navtoggle{position:absolute;right:0;opacity:0;width:2.65rem;height:2.65rem;margin:0;pointer-events:none}
nav.site .navbtn{display:none;align-items:center;justify-content:center;flex:none;width:2.65rem;height:2.65rem;
box-sizing:border-box;border:1px solid var(--line);border-radius:10px;color:var(--ink);cursor:pointer}
nav.site .navbtn svg{width:1.25rem;height:1.25rem}
nav.site .navtoggle:checked ~ .navbtn{background:var(--card);border-color:var(--muted)}
nav.site .navtoggle:focus-visible ~ .navbtn{outline:2px solid var(--accent);outline-offset:2px}
@media(max-width:760px){
nav.site{padding:1.1rem 0}
nav.site .navr{margin-left:auto}
nav.site .navbtn{display:inline-flex}
nav.site .navmenu{display:none}
nav.site .navtoggle:checked ~ .navr .navmenu{display:flex;flex-direction:column;align-items:stretch;gap:0;
position:absolute;top:calc(100% - .4rem);left:0;right:0;z-index:20;padding:.4rem;background:var(--card);
border:1px solid var(--line);border-radius:12px;box-shadow:0 18px 40px -18px rgba(23,38,62,.4)}
nav.site .navmenu .nl,nav.site .navmenu .tema{display:flex;align-items:center;justify-content:flex-start;gap:.6rem;
width:100%;height:auto;box-sizing:border-box;padding:.7rem .8rem;border:0;border-radius:8px;color:var(--ink);font-size:1rem}
nav.site .navmenu .nl:hover,nav.site .navmenu .tema:hover{background:var(--bg)}
nav.site .navmenu .tl{position:static;width:auto;height:auto;overflow:visible;clip:auto}
}
/* Footer = blekk-panel i BEGGE moduser, så --footer holdes mørk og flipper IKKE
   slik --ink gjør i mørk modus (ellers lys-på-lys = usynlig, jf. knapp-fellen). */
/* Short pages: body fills the window and the sticky footer is pushed to its bottom edge,
   instead of floating halfway up the screen. */
body{min-height:100vh;min-height:100dvh}
footer.site{background:var(--footer);color:#aeb9cb;font-size:.9rem;line-height:1.6;margin-top:4rem;
border-top:1px solid rgba(255,255,255,.07);position:sticky;top:100vh}
footer.site .wrap{padding-top:3.2rem;padding-bottom:1.6rem}
footer.site a{color:#cdd6e4;text-decoration:none}
footer.site a:hover{color:#fff;text-decoration:underline}
footer.site .brand{color:#fff;margin-bottom:.7rem}
footer.site .brand svg{color:var(--accent-fill);--mark-gap:var(--footer)}
.foot-top{display:grid;grid-template-columns:1.6fr 1fr 1fr 1fr;gap:2.4rem}
.foot-brand p{margin:0;max-width:24em}
.foot-brand .foot-company{margin-top:1.3rem;font-size:.82rem;line-height:1.8}
.foot-company span{display:block;font-size:.7rem;font-weight:600;letter-spacing:.1em;text-transform:uppercase;color:#909caf}
footer.site .foot-company a{color:#fff;font-weight:700;font-size:.92rem}
footer.site h3{margin:0 0 .8rem;font-size:.7rem;font-weight:600;letter-spacing:.12em;text-transform:uppercase;color:#909caf}
.foot-duo{display:grid;gap:1.6rem;align-content:start}
.foot-link{display:block;padding:.22rem 0}
.foot-bottom{display:flex;justify-content:space-between;align-items:center;gap:1rem 2rem;flex-wrap:wrap;
margin-top:2.6rem;padding-top:1.3rem;border-top:1px solid rgba(255,255,255,.13);font-size:.8rem;color:#909caf}
.foot-bottom .foot-dm{display:inline-flex;align-items:center;gap:.6rem;color:#909caf}
.foot-bottom .foot-dm:hover{text-decoration:none;color:#cdd6e4}
@media(max-width:820px){.foot-top{grid-template-columns:1fr 1fr;gap:2rem 1.6rem}.foot-brand{grid-column:1/-1}
.foot-duo{display:contents}}
"""

# Theme toggle (logged-in header). Inline SVG instead of a "◐" glyph: the glyph's size and
# baseline depend on which font the system falls back to. The label is visually hidden on
# desktop and shown in the phone menu.
_THEME_BTN = (
    '<button type=button class=tema onclick="byttTema()" title="Bytt lyst/mørkt">'
    '<svg viewBox="0 0 16 16" aria-hidden=true><circle cx=8 cy=8 r=6.25 fill=none '
    'stroke=currentColor stroke-width=1.5 /><path d="M8 1.75a6.25 6.25 0 0 0 0 12.5z" '
    'fill=currentColor /></svg><span class=tl>Bytt tema</span></button>'
)

_MENU_ICON = (
    '<svg viewBox="0 0 20 20" aria-hidden=true fill=none stroke=currentColor stroke-width=1.8 '
    'stroke-linecap=round><path d="M3 6h14M3 10h14M3 14h14" /></svg>'
)

# Public links in the logged-out header. The last, primary action is rendered as a button.
_NAV_PUBLIC_LINKS = (
    ("/demo", "Live demo"),
    ("/#priser", "Priser"),
    ("/google-analytics-alternativ", "Mot Google Analytics"),
    ("/blogg", "Blogg"),
    ("/login", "Logg inn"),
)


def _logout_link() -> str:
    """The logout control of the shared header. /logout only logs out on POST (a GET shows a
    button), so this is a one-button form; `form.logout` is display:contents and the button
    is styled like the other `.nl` links."""
    return '<form class=logout method=post action="/logout"><button class=nl>Logg ut</button></form>'


def _nav_link(href: str, label: str, path: str) -> str:
    current = " aria-current=page" if href == path else ""
    return f'<a class=nl href="{href}"{current}>{label}</a>'


def _site_nav(request=None) -> str:
    """The ONE header for every page. Logged-out and logged-in variants share the same
    geometry: logo on the left, actions right-aligned, fixed height, same frame (.wrap).
    On a phone everything but the primary action moves into a menu panel (no JavaScript)."""
    path = request.url.path if request is not None else ""
    if request is not None and request.query_params.get("site"):
        path = ""  # a single site's dashboard is below the sites list, not the list itself
    if request is not None and _user(request):
        right = (
            _nav_link("/app", "Mine nettsteder", path)
            + f"<div class=navmenu>{_logout_link()}{_THEME_BTN}</div>"
        )
    else:
        right = (
            "<div class=navmenu>"
            + "".join(_nav_link(h, label, path) for h, label in _NAV_PUBLIC_LINKS)
            + '</div><a class="btn btn-accent" href="/signup">Prøv gratis</a>'
        )
    return (
        "<nav class=site aria-label=Hovedmeny>" + _WORDMARK
        + '<input type=checkbox id=navtoggle class=navtoggle aria-label=Meny>'
        + f"<div class=navr>{right}</div>"
        + f'<label class=navbtn for=navtoggle aria-hidden=true>{_MENU_ICON}</label></nav>'
    )


# Same structure as heltenig.no's footer, the fleet's reference: brand + who is
# behind it, link columns, legal line. Every link here is a page that exists.
_SITE_FOOTER = (
    "<footer class=site><div class=wrap><div class=foot-top>"
    "<div class=foot-brand>" + _WORDMARK +
    "<p>Webanalyse uten cookies, uten samtykkebanner og uten persondata. Bygget og driftet i Norge.</p>"
    '<p class=foot-company><span>En tjeneste fra</span>'
    '<a href="https://datamynt.no" rel=noopener>Datamynt AS</a><br>'
    "Org.nr: 936 017 207<br>Maridalsveien 163, 0461 Oslo</p></div>"
    "<div><h3>Produkt</h3>"
    '<a class=foot-link href="/demo">Live demo</a>'
    '<a class=foot-link href="/#priser">Priser</a>'
    '<a class=foot-link href="/google-analytics-alternativ">Sporløs mot Google Analytics</a>'
    '<a class=foot-link href="/integrasjoner">Integrasjoner</a>'
    '<a class=foot-link href="/utviklere">API for utviklere</a></div>'
    "<div><h3>Ressurser</h3>"
    '<a class=foot-link href="/sporsmal">Spørsmål og svar</a>'
    '<a class=foot-link href="/blogg">Blogg</a>'
    '<a class=foot-link href="https://status.sporlos.no" rel=noopener>Driftsstatus ↗</a>'
    '<a class=foot-link href="https://github.com/datamynt/sporlos-tracker" rel=noopener>Åpen kildekode ↗</a></div>'
    "<div class=foot-duo><div><h3>Kontakt</h3>"
    '<a class=foot-link href="mailto:post@sporlos.no">post@sporlos.no</a>'
    '<a class=foot-link href="https://datamynt.no" rel=noopener>datamynt.no</a></div>'
    "<div><h3>Juridisk</h3>"
    '<a class=foot-link href="/personvern">Personvern</a>'
    '<a class=foot-link href="/vilkar">Salgsbetingelser</a></div></div>'
    "</div><div class=foot-bottom>"
    f"<span>© {date.today().year} Sporløs – en tjeneste fra Datamynt AS (org.nr 936 017 207)</span>"
    '<a class=foot-dm href="https://datamynt.no" rel=noopener aria-label="En del av Datamynt">'
    'En del av <img src="/static/datamynt-logo.svg" alt="Datamynt" height="20"></a>'
    "</div></div></footer>"
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
    '<meta property="og:image" content="https://sporlos.no/static/og.png?v=blekk2">'
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
.plan .velg-hl{background:var(--accent-fill);border-color:var(--accent-fill);color:#fff}
.plan .velg-hl:hover{background:var(--accent-fill-h);border-color:var(--accent-fill-h);color:#fff}
</style>
"""
        + _SELF_SNIPPET
        + "</head><body>"  # eksplisitt head/body — LinkedIn-parseren er pirkete
        + """<div class=wrap>
"""
        + _site_nav(request)
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
    <div class="card reveal"><h3>Data i Norge</h3><p>Norsk-eid drift på vår egen server i Oslo.
    Sporingsscriptet er
    <a href="https://github.com/datamynt/sporlos-tracker">åpen kildekode</a> — etterprøv selv.</p></div>
    <div class="card reveal"><h3>Lett som en fjær</h3><p>Sporingsscriptet er ~1 kB komprimert — rundt en
    nittidel av Google Analytics. Siden din merker det ikke.</p></div>
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


def _shell(request, title, inner, status_code=200):
    """Small centred card (login, signup, messages) inside the shared frame."""
    return HTMLResponse(
        f"""<!doctype html><html lang=no><meta charset=utf-8>
<title>{escape(title)} — Sporløs</title>
<meta name=viewport content="width=device-width, initial-scale=1">
{_BRAND_HEAD}
<style>{_BRAND_CSS}{_CHROME_CSS}
.auth{{font-size:16px;max-width:380px;margin:1.5rem auto 0;padding:0 0 3rem}}
.auth h1{{font-size:1.5rem;letter-spacing:-.02em}}
.auth label{{display:block;margin:.8rem 0 .2rem;font-size:.9rem;color:var(--muted)}}
.auth input{{width:100%;padding:.6rem;border:1px solid var(--line);border-radius:8px;font-size:1rem;
box-sizing:border-box;background:var(--card);color:var(--ink);font:inherit}}
.auth form .btn{{margin-top:1.2rem;width:100%}}
.auth button{{margin-top:1.2rem;width:100%;background:var(--btn-bg);color:#fff;border:0;padding:.7rem;
border-radius:8px;font-size:1rem;cursor:pointer;font:inherit;font-weight:600}}
.auth .err{{background:var(--err-bg);color:var(--err);padding:.6rem;border-radius:8px;font-size:.9rem;margin:.5rem 0}}
.auth .ok{{color:var(--ok);font-size:.9rem}}
.auth .muted{{margin-top:1.2rem;font-size:.85rem}}{_SSO_CSS}</style>
{_SELF_SNIPPET}
<div class=wrap>{_site_nav(request)}
<div class=auth>
{inner}
</div></div>
{_SITE_FOOTER}""",
        status_code=status_code,
    )


def _not_found_page(request):
    """Branded 404 in the shared frame, so a wrong link does not drop the visitor into a bare
    text page with no header."""
    return _shell(
        request,
        "Fant ikke siden",
        "<h1>Fant ikke siden</h1>"
        "<p class=muted>Lenken er feil, eller siden er flyttet.</p>"
        '<p class=muted><a href="/">Til forsiden</a> · <a href="/blogg">Blogg</a> · '
        '<a href="/demo">Live demo</a></p>',
        status_code=404,
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
        # Honeypot: feltet er skjult for mennesker, men bots fyller alt de finner.
        # Vi later som det gikk bra — da merker ikke boten at den er stoppet.
        if (f.get("website") or "").strip():
            log.warning("signup: honeypot utløst (email=%s)", email)
            return _shell(
                request,
                "Sjekk e-posten",
                "<h1>Sjekk e-posten din</h1><p class=muted>Vi har sendt deg en "
                "bekreftelseslenke.</p>",
            )
        if not company or "@" not in email or len(pw) < 8:
            err = "Fyll inn firma, gyldig e-post og passord (min. 8 tegn)."
        elif store.get_user_by_email(email):
            err = "Det finnes allerede en konto med denne e-posten."
        else:
            # Struping per IP + globalt. Teller kun forsøk som faktisk ville sendt
            # e-post, så en ekte bruker som skriver feil passordlengde ikke straffes.
            ip = client_ip(dict(request.headers), fallback=request.client.host if request.client else "")
            used, total = store.signup_attempts(ip)
            if used >= SIGNUP_PER_IP_HOURLY or total >= SIGNUP_GLOBAL_HOURLY:
                log.warning(
                    "signup: strupet (ip_hash=%s used=%d total=%d email_hash=%s)",
                    store._ip_key(ip)[:16], used, total, store.forgot_email_hash(email)[:16],
                )
                err = "For mange registreringer herfra akkurat nå. Prøv igjen om en time."
            else:
                try:
                    tid, uid = store.create_account(company, email, hash_password(pw))
                    _login(request, uid, tid)
                    store.signup_bump(ip)
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
        request,
        "Opprett konto",
        f"""<h1>Opprett konto</h1>{chosen}{eb}
<form method=post>
  <input type=hidden name=plan value="{escape(plan)}">
  <label>Firma</label><input name=company required>
  <label>E-post</label><input name=email type=email required autocomplete=email>
  <label>Passord</label><input name=password type=password required minlength=8 autocomplete=new-password>
  <div style="position:absolute;left:-9999px" aria-hidden=true>
    <label>Nettsted</label><input name=website tabindex=-1 autocomplete=off></div>
  <button>{"Fortsett til betaling" if plan else "Start gratis prøve"}</button>
</form>
{_sso_buttons(plan)}
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
        ip = client_ip(dict(request.headers), fallback=request.client.host if request.client else "")
        by_ip, by_email = store.login_failures(ip, email)
        if by_ip >= LOGIN_FAILS_PER_IP_HOURLY or by_email >= LOGIN_FAILS_PER_EMAIL_HOURLY:
            err = "For mange mislykkede forsøk. Vent en time, eller bruk «Glemt passord»."
        else:
            u = store.get_user_by_email(email)
            if u and verify_password(pw, u["password_hash"]):
                _login(request, u["id"], u["tenant_id"])
                return RedirectResponse("/app", status_code=302)
            store.login_fail_bump(ip, email)
            err = "Feil e-post eller passord."
    eb = f'<div class=err>{escape(err)}</div>' if err else ""
    sso_msg = _SSO_MESSAGES.get(request.query_params.get("sso") or "")
    if sso_msg:
        eb += f"<div class=err>{escape(sso_msg)}</div>"
    if request.query_params.get("reset"):
        eb += '<p class=ok>Passordet er oppdatert — logg inn.</p>'
    return _shell(
        request,
        "Logg inn",
        f"""<h1>Logg inn</h1>{eb}
<form method=post>
  <label>E-post</label><input name=email type=email required autocomplete=email>
  <label>Passord</label><input name=password type=password required autocomplete=current-password>
  <button>Logg inn</button>
</form>
{_sso_buttons()}
<p class=muted>Ny her? <a href="/signup">Opprett konto</a> · <a href="/forgot">Glemt passord?</a></p>""",
    )


def unsubscribe(request):
    tid = request.query_params.get("tid") or ""
    token = request.query_params.get("t") or ""
    if tid and check_token("unsub", tid, token):
        try:
            store.set_email_optout(int(tid), True)
        except Exception:
            pass
        return _shell(
            request,
            "Avmeldt",
            "<h1>Du er avmeldt</h1><p class=muted>Du får ikke flere ukerapporter på e-post. "
            'Vil du ha dem tilbake, kontakt oss på post@sporlos.no.</p>'
            '<p class=muted><a href="/app">Til Sporløs</a></p>',
        )
    return _shell(
        request,
        "Ugyldig lenke",
        '<h1>Ugyldig avmeldings-lenke</h1><p class=muted><a href="/">Til forsiden</a></p>',
    )


def verify_email(request):
    uid = request.query_params.get("uid") or ""
    token = request.query_params.get("t") or ""
    if uid and check_token("verify", uid, token):
        try:
            store.set_email_verified(int(uid))
        except Exception:
            pass
        return _shell(
            request,
            "Bekreftet",
            '<h1>E-posten er bekreftet ✓</h1><p class=muted><a href="/app">Til Sporløs</a></p>',
        )
    return _shell(
        request,
        "Ugyldig lenke",
        '<h1>Ugyldig bekreftelseslenke</h1><p class=muted><a href="/app">Til Sporløs</a></p>',
    )


def resend_verify(request):
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
        # Honeypot: samme skjulte felt som /signup. Et menneske ser det aldri;
        # en bot som fyller alt den finner får det vanlige "sjekk e-posten"-svaret
        # og merker aldri at den ble stoppet.
        if (f.get("website") or "").strip():
            log.warning("forgot: honeypot utløst")
        elif email:
            # Struping per IP + per mål-e-post + globalt. Teller kun forsøk som
            # faktisk ville sendt e-post (ekte, ikke-SSO konto), så en bruker
            # som roter med adressen sin ikke straffes for andres forsøk.
            ip = client_ip(dict(request.headers), fallback=request.client.host if request.client else "")
            used_ip, used_email, total = store.forgot_attempts(ip, email)
            throttled = (
                used_ip >= FORGOT_PER_IP_HOURLY
                or used_email >= FORGOT_PER_EMAIL_HOURLY
                or total >= FORGOT_GLOBAL_HOURLY
            )
            if throttled:
                log.warning(
                    "forgot: strupet (ip_hash=%s used_ip=%d used_email=%d total=%d email_hash=%s)",
                    store._ip_key(ip)[:16], used_ip, used_email, total, store.forgot_email_hash(email)[:16],
                )
            else:
                u = store.get_user_by_email(email)
                # SSO-only accounts («!»-sentinel) may add a password this way too: the
                # link proves who owns the address, same as the provider login did.
                if u:
                    store.forgot_bump(ip, email)
                    token = store.create_reset_token(email)
                    link = f"{PUBLIC_BASE}/reset?token={token}"
                    mailer.send(
                        email,
                        "Tilbakestill passordet ditt – Sporløs",
                        f"Hei,\n\nKlikk for å velge nytt passord (gyldig i 1 time):\n{link}\n\n"
                        "Ba du ikke om dette, kan du se bort fra e-posten.\n\nSporløs",
                    )
        # alltid samme svar (ingen e-post-enumerering, heller ikke ved struping/honeypot)
        return _shell(
            request,
            "Sjekk e-posten",
            "<h1>Sjekk e-posten din</h1><p class=muted>Hvis det finnes en konto på adressen, "
            "har vi sendt en lenke for å tilbakestille passordet. Lenken er gyldig i én time.</p>"
            '<p class=muted><a href="/login">Tilbake til innlogging</a></p>',
        )
    return _shell(
        request,
        "Glemt passord",
        """<h1>Glemt passord</h1>
<p class=muted>Skriv inn e-posten din, så sender vi en lenke for å velge nytt passord.</p>
<form method=post>
  <label>E-post</label><input name=email type=email required>
  <div style="position:absolute;left:-9999px" aria-hidden=true>
    <label>Nettsted</label><input name=website tabindex=-1 autocomplete=off></div>
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
                request,
                "Lenke utløpt",
                "<h1>Lenken er ugyldig eller utløpt</h1>"
                '<p class=muted><a href="/forgot">Be om en ny</a></p>',
            )
        if len(pw) < 8:
            new = store.create_reset_token(email)  # ny token, prøv igjen
            return _shell(
                request,
                "For kort passord",
                f"""<h1>Velg nytt passord</h1><div class=err>Passordet må være minst 8 tegn.</div>
<form method=post>
  <input type=hidden name=token value="{escape(new)}">
  <label>Nytt passord</label><input name=password type=password required minlength=8>
  <button>Lagre passord</button>
</form>""",
            )
        store.set_password(email, hash_password(pw))
        store.invalidate_reset_tokens(email)
        u = store.get_user_by_email(email)
        if u:
            store.bump_session_version(u["id"])  # logs out every existing session
            me = store.get_user(u["id"]) or {}
            if not me.get("email_verified"):
                # The reset link proves who owns the address. If the account was never
                # verified, someone else may have registered it (pre-hijack): their
                # sessions just ended above, and their API keys go too.
                store.revoke_all_api_keys(u["tenant_id"])
                store.set_email_verified(u["id"])
        return RedirectResponse("/login?reset=1", status_code=302)
    return _shell(
        request,
        "Velg nytt passord",
        f"""<h1>Velg nytt passord</h1>
<form method=post>
  <input type=hidden name=token value="{escape(token)}">
  <label>Nytt passord</label><input name=password type=password required minlength=8>
  <button>Lagre passord</button>
</form>""",
    )


async def logout(request):
    # Only a POST logs out. SameSite=Lax keeps the session cookie off cross-site POSTs,
    # so another site can't forge one; a GET (old bookmark, <img>, prefetch) gets a button.
    if request.method == "POST":
        request.session.clear()
        return RedirectResponse("/", status_code=303)
    if not _user(request):
        return RedirectResponse("/", status_code=302)
    return _shell(
        request,
        "Logg ut",
        "<h1>Logg ut?</h1>"
        '<form method=post action="/logout"><button>Logg ut</button></form>'
        '<p class=muted><a href="/app">Tilbake til nettstedene</a></p>',
    )


# --- Google/Microsoft login through Datamynt ID (app/innlogg.py) --------------
# A login is bound to (IdP id, the provider's user id) in user_identities. The
# e-mail only decides which account a FIRST login attaches to, and only when the
# address is proven: Google asserts a verified address; for Microsoft (whose
# address any tenant admin can type) we send a confirmation link first.
_SSO_TTL = 600            # button click -> provider callback
_SSO_CONFIRM_TTL = 1800   # confirmation mail -> click


def _sso_fail(code: str = "feil"):
    return RedirectResponse(f"/login?sso={code}", status_code=302)


async def sso_start(request):
    provider = request.path_params.get("provider", "")
    if not innlogg.enabled(provider):
        return _sso_fail()
    plan = request.query_params.get("plan", "")
    return await _sso_begin(
        request, provider, plan=plan if plan in _PLAN_LABELS else "",
        # ?reauth=1: a logged-in, SSO-only user proves it's still them before deleting the account.
        reauth=request.query_params.get("reauth") == "1",
    )


async def _sso_begin(request, provider: str, **flow):
    """Send the browser to the provider. `flow` rides along in the session, bound to this
    one state value, and comes back to sso_callback as `pending`."""
    state = secrets.token_urlsafe(24)
    request.session["sso"] = {"s": state, "p": provider, "exp": int(time.time()) + _SSO_TTL, **flow}
    try:
        url = await innlogg.start(
            provider, f"{PUBLIC_BASE}/auth/sso/callback?state={state}",
            f"{PUBLIC_BASE}/login?sso=avbrutt",
        )
    except innlogg.IdpError as e:
        log.warning("sso start %s: %s", provider, e)
        return _sso_fail()
    return RedirectResponse(url, status_code=302)


def _sso_claim(existing: dict, who) -> None:
    """Attach a proven login to an existing account. If that account's address was
    never verified, whoever registered it may not own it (pre-hijack): their
    password, sessions and API keys end here. They can set a password again
    through «Glemt passord», which mails the real owner."""
    row = store.get_user(existing["id"]) or {}
    if not row.get("email_verified"):
        store.set_password(existing["email"], "!sso")
        store.bump_session_version(existing["id"])
        store.revoke_all_api_keys(existing["tenant_id"])
        store.set_email_verified(existing["id"])
    store.link_identity(existing["id"], who.idp_id, who.subject, who.email)


def _sso_new_account(who) -> tuple[int, int]:
    company = who.name or who.email.split("@")[0]
    tid, uid = store.create_account(company, who.email, "!sso")
    store.set_email_verified(uid)
    store.link_identity(uid, who.idp_id, who.subject, who.email)
    return tid, uid


def _sso_done(request, uid: int, tid: int, plan: str = "", new: bool = False):
    _login(request, uid, tid)
    if new and plan:
        return RedirectResponse(f"/betal?plan={plan}", status_code=302)
    return RedirectResponse("/app", status_code=302)


async def sso_callback(request):
    pending = request.session.pop("sso", None) or {}
    q = request.query_params
    if (not pending or time.time() > pending.get("exp", 0)
            or not hmac.compare_digest(str(pending.get("s", "")), q.get("state", ""))):
        return _sso_fail()
    provider, plan = pending["p"], pending.get("plan", "")
    try:
        who = await innlogg.finish(provider, q.get("id", ""), q.get("token", ""))
    except innlogg.IdpError as e:
        log.warning("sso callback %s: %s", provider, e)
        return _sso_fail()

    if pending.get("invite"):  # started from an invite link: join that account
        return _sso_join(request, pending["invite"], who)
    bound = store.user_by_identity(who.idp_id, who.subject)
    me = _user(request)
    if me and pending.get("reauth"):
        # Fresh login before deleting an SSO-only account: only the login bound to
        # this very user counts. A new session restarts the clock (iat).
        if not bound or bound["id"] != me["uid"]:
            return RedirectResponse("/app?konto=annen#slett-konto", status_code=302)
        _login(request, me["uid"], me["tid"])
        return RedirectResponse("/app?konto=klar#slett-konto", status_code=302)
    if me:  # «Koble til» from the account page
        if bound and bound["id"] != me["uid"]:
            return RedirectResponse("/app?sso=opptatt#konto", status_code=302)
        store.link_identity(me["uid"], who.idp_id, who.subject, who.email)
        return RedirectResponse("/app?sso=koblet#konto", status_code=302)
    if bound:
        return _sso_done(request, bound["id"], bound["tenant_id"])
    if not who.email:
        return _sso_fail("epost")

    existing = store.get_user_by_email(who.email)
    if who.email_verified:
        if existing:
            _sso_claim(existing, who)
            return _sso_done(request, existing["id"], existing["tenant_id"])
        tid, uid = _sso_new_account(who)
        return _sso_done(request, uid, tid, plan, new=True)

    # Unproven address (Microsoft): mail a link that only works in THIS browser.
    # The session holds a hash of the nonce, never the nonce: the session cookie is
    # readable by whoever holds it, and that may be the person we're checking.
    ip = client_ip(dict(request.headers), fallback=request.client.host if request.client else "")
    used_ip, used_email, total = store.forgot_attempts(ip, who.email)
    if used_ip >= FORGOT_PER_IP_HOURLY or used_email >= FORGOT_PER_EMAIL_HOURLY or total >= FORGOT_GLOBAL_HOURLY:
        return _sso_fail("for-mange")
    store.forgot_bump(ip, who.email)
    nonce = secrets.token_urlsafe(32)
    request.session["sso_confirm"] = {
        "h": hashlib.sha256(nonce.encode()).hexdigest(), "exp": int(time.time()) + _SSO_CONFIRM_TTL,
        "p": provider, "idp": who.idp_id, "sub": who.subject, "email": who.email,
        "name": who.name or "", "plan": plan,
    }
    label = innlogg.LABELS[provider]
    mailer.send(
        who.email,
        f"Bekreft innlogging med {label} – Sporløs",
        f"Hei,\n\nNoen, forhåpentligvis du, vil logge inn på Sporløs med {label}-kontoen"
        f"{' «' + who.name + '»' if who.name else ''}. Klikk for å bekrefte at {who.email} er din "
        f"adresse. Lenken virker i 30 minutter, og bare i nettleseren der du startet:\n"
        f"{PUBLIC_BASE}/auth/sso/bekreft?n={nonce}\n\n"
        "Var det ikke deg, kan du se bort fra e-posten. Ingenting blir koblet til kontoen din.\n\nSporløs",
    )
    return _shell(
        request,
        "Sjekk e-posten",
        f"<h1>Sjekk e-posten din</h1><p class=muted>Vi har sendt en lenke til "
        f"<b>{escape(who.email)}</b>. Klikk på den i denne nettleseren for å fullføre "
        f"innloggingen med {escape(label)}.</p>"
        "<p class=muted>Vi spør én gang fordi Microsoft ikke bekrefter e-postadresser for oss.</p>",
    )


async def sso_confirm(request):
    pend = request.session.get("sso_confirm") or {}
    n = request.query_params.get("n", "")
    if (not pend or not n or time.time() > pend.get("exp", 0)
            or not hmac.compare_digest(pend.get("h", ""), hashlib.sha256(n.encode()).hexdigest())):
        return _shell(
            request,
            "Lenken virker ikke",
            "<h1>Lenken virker ikke her</h1><p class=muted>Åpne lenken i samme nettleser som du "
            "startet innloggingen i, innen 30 minutter.</p>"
            '<p class=muted><a href="/login">Prøv igjen</a></p>',
        )
    request.session.pop("sso_confirm", None)
    who = innlogg.IdpLogin(pend["p"], pend["idp"], pend["sub"], pend["email"], True, pend["name"] or None)
    bound = store.user_by_identity(who.idp_id, who.subject)
    if bound:
        return _sso_done(request, bound["id"], bound["tenant_id"])
    existing = store.get_user_by_email(who.email)
    if existing:
        _sso_claim(existing, who)
        return _sso_done(request, existing["id"], existing["tenant_id"])
    tid, uid = _sso_new_account(who)
    return _sso_done(request, uid, tid, pend.get("plan", ""), new=True)


async def gsc_connect(request):
    """«Koble til Search Console»: kunden godkjenner med egen Google-konto,
    vi henter refresh-token og auto-matcher property mot sitens domene."""
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    public_id = request.query_params.get("site") or ""
    site = store.resolve_site(public_id)
    if not (_HAS_GOOGLE and oauth) or not site or site["tenant_id"] != user["tid"]:
        return RedirectResponse("/app", status_code=302)
    request.session["gscconn_site"] = public_id
    return await oauth.gscconn.authorize_redirect(request, f"{PUBLIC_BASE}/app/seo/callback")


async def gsc_callback(request):
    from app import seo as seo_mod

    user = _user(request)
    public_id = request.session.pop("gscconn_site", "")
    site = store.resolve_site(public_id) if public_id else None
    if not user or not site or site["tenant_id"] != user["tid"]:
        return RedirectResponse("/app", status_code=302)
    back = f"/app?site={public_id}"
    try:
        token = await oauth.gscconn.authorize_access_token(request)
    except Exception:
        return RedirectResponse(f"{back}&gsc=avbrutt", status_code=302)
    refresh = token.get("refresh_token")
    if not refresh:
        # prompt=consent skal alltid gi refresh-token; mangler det, si det ærlig
        # fremfor å lagre en kobling som dør når access-tokenet utløper.
        return RedirectResponse(f"{back}&gsc=feil", status_code=302)
    email = ((token.get("userinfo") or {}).get("email") or "").strip().lower()
    prop = None
    try:
        props = seo_mod.gsc_properties_for_token(token["access_token"])
        prop = seo_mod.match_property(site["domain"], props)
    except Exception:
        pass  # property kan matches ved neste synk — koblingen er det viktige
    store.set_search_connection(site["id"], seo_mod.encrypt_token(refresh), prop, email)
    return RedirectResponse(f"{back}&gsc={'ok' if prop else 'delvis'}#sok", status_code=302)


async def gsc_disconnect(request):
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    form = await request.form()
    public_id = str(form.get("site") or "")
    site = store.resolve_site(public_id)
    if site and site["tenant_id"] == user["tid"]:
        store.delete_search_connection(site["id"])
    return RedirectResponse(f"/app?site={public_id}#sok", status_code=302)


def billing_checkout(request):
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


def billing_portal(request):
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


def vipps_start(request):
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


def vipps_cancel(request):
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
        request,
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
    # Normaliser: dropp scheme/www/sti — domenet er kun visningsetikett, men stygt
    # input forvirrer (og duplikat skal ikke svelges stille). Selve scheme/lowercase/
    # www-regelen bor i store (delt med POST /api/v1/sites); stien kastes her, siden
    # dashbordet registrerer ett domene — API-et tar imot «host/sti» for byggere som
    # skiller kunder på sti.
    domain = store.normalize_domain(f.get("domain") or "").split("/")[0].strip()
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
        # Stored events are masked, so the goal is too: "/order/<id>" matches "/order/:id".
        mval = mask_path(mval) if mtype == "path" else mask_label(mval)
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
            if line.startswith("/"):
                steps.append({"type": "path", "value": mask_path(line)})
            else:
                steps.append({"type": "event", "value": mask_label(line)})
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
    store.invalidate_reset_tokens(me["email"])
    # Every other session (a stolen cookie, a forgotten laptop) ends here; this one stays.
    request.session["sv"] = store.bump_session_version(me["id"])
    return RedirectResponse("/app?pw=ok", status_code=302)


# --- Self-serve deletion: one site, or the whole account --------------------------
# Deletion logs one line with ids only (no e-mail, no domain). WARNING because the
# app configures no logging: INFO from the "sporlos" logger never reaches the
# container log.

# An account without a password (Google/Microsoft only) proves it is still its owner
# by a login at most this old.
_REAUTH_SECONDS = 600
_PAID_PLANS = ("liten", "vekst", "pro")


def _running_subscription(tenant: dict) -> str:
    """Returns "stripe" or "vipps" while a paid subscription runs (or a Vipps agreement
    awaits approval), else "". Account deletion waits for it to end: we never cancel it on
    the customer's behalf, since nothing in this flow may touch money."""
    if tenant.get("vipps_pending_plan"):
        return "vipps"
    if tenant.get("plan") in _PAID_PLANS:
        if tenant.get("stripe_subscription_id"):
            return "stripe"
        if tenant.get("vipps_agreement_id"):
            return "vipps"
    return ""


async def site_delete(request):
    """Delete one site and all its data. The user types the domain to confirm."""
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    f = await request.form()
    site, pid = _own_site(request, f)
    if not site:  # not this tenant's site: don't reveal whether it exists
        return RedirectResponse("/app", status_code=302)
    typed = str(f.get("confirm") or "").strip().lower()
    if not typed or (typed != site["domain"] and store.normalize_domain(typed) != site["domain"]):
        return RedirectResponse(f"/app?site={pid}&slett=feil#slett", status_code=302)
    counts = store.delete_site(site["id"], user["tid"])
    if counts is None:
        return RedirectResponse("/app", status_code=302)
    log.warning("site deleted: tenant=%s site=%s rows=%s", user["tid"], site["id"], sum(counts.values()))
    request.session["deleted_site"] = site["domain"]  # shown once on /app, kept out of the URL
    return RedirectResponse("/app", status_code=302)


async def account_delete(request):
    """Delete the whole account (tenant). Confirm by typing the company name or the
    e-mail, plus the password; an SSO-only account needs a login from the last
    _REAUTH_SECONDS instead. Refused while a paid subscription runs."""
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    f = await request.form()
    back = "/app?konto={}#slett-konto"
    tenant = store.get_tenant(user["tid"]) or {}
    me = store.get_user(user["uid"]) or {}
    login_row = store.get_user_by_email(me.get("email") or "") or {}
    if user["uid"] != store.account_owner_id(user["tid"]):
        return RedirectResponse(back.format("ikke-eier"), status_code=302)
    if _running_subscription(tenant):
        return RedirectResponse(back.format("abonnement"), status_code=302)
    typed = str(f.get("confirm") or "").strip().lower()
    if not typed or typed not in (str(tenant.get("name") or "").strip().lower(), me.get("email")):
        return RedirectResponse(back.format("feil"), status_code=302)
    pw_hash = str(login_row.get("password_hash") or "")
    if pw_hash.startswith("!"):  # SSO only: no password to ask for
        if time.time() - int(request.session.get("iat") or 0) > _REAUTH_SECONDS:
            return RedirectResponse(back.format("logginn"), status_code=302)
    else:
        # The password field is a guessing oracle for whoever holds the session:
        # same failure budget as /login.
        ip = client_ip(dict(request.headers), fallback=request.client.host if request.client else "")
        by_ip, by_email = store.login_failures(ip, me["email"])
        if by_ip >= LOGIN_FAILS_PER_IP_HOURLY or by_email >= LOGIN_FAILS_PER_EMAIL_HOURLY:
            return RedirectResponse(back.format("for-mange"), status_code=302)
        if not verify_password(f.get("password") or "", pw_hash):
            store.login_fail_bump(ip, me["email"])
            return RedirectResponse(back.format("passord"), status_code=302)
    emails = store.tenant_emails(user["tid"])
    counts = store.delete_tenant(user["tid"])
    request.session.clear()
    log.warning(
        "account deleted: tenant=%s by_user=%s users=%s sites=%s",
        user["tid"], user["uid"], counts.get("users", 0), counts.get("sites", 0),
    )
    for addr in emails:
        mailer.send(
            addr,
            "Kontoen er slettet – Sporløs",
            "Hei,\n\nKontoen og alle data er slettet fra Sporløs: nettstedene med all statistikk, "
            "API-nøklene og alle brukerne på kontoen. Sporingskoden på nettstedene teller ikke "
            "lenger, og du kan fjerne den når det passer.\n\n"
            "Sikkerhetskopiene våre slettes ikke én og én. Dataene forsvinner fra dem når "
            "kopiene roterer ut.\n\n"
            "Var det ikke du som slettet kontoen, skriv til post@sporlos.no.\n\nSporløs",
        )
    return _shell(
        request,
        "Kontoen er slettet",
        "<h1>Kontoen er slettet</h1>"
        "<p class=muted>Nettstedene, all statistikk, API-nøklene og brukerne er slettet. "
        "Vi har sendt en bekreftelse på e-post.</p>"
        "<p class=muted>Sporingskoden på nettstedene teller ikke lenger. Fjern den når det passer.</p>"
        '<p class=muted><a href="/">Til forsiden</a></p>',
    )


# --- Users of an account: invite a colleague, remove one -------------------------
# Every user of an account has the same rights (no roles yet): sees and runs the same
# sites, invites and removes others. Nobody removes themselves here; that is what
# deleting the account is for.
# Invite mails go to addresses the inviter types, so: only from a verified address,
# and capped per account and per target address (hashed hourly buckets, like /forgot).
INVITES_PER_ACCOUNT_HOURLY = 10
INVITES_PER_ADDRESS_HOURLY = 3
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _plain(s: str, limit: int = 80) -> str:
    """User-typed text (a company name) for a plain-text mail: one line, capped."""
    s = " ".join(str(s or "").split())
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _short_date(v) -> str:
    try:
        d = datetime.strptime(str(v)[:10], "%Y-%m-%d")
    except ValueError:
        return ""
    return f"{d.day}.{d.month}."


async def user_invite(request):
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    f = await request.form()
    back = "/app?brukere={}#brukere"
    email = str(f.get("email") or "").strip().lower()
    me = store.get_user(user["uid"]) or {}
    if not me.get("email_verified"):
        return RedirectResponse(back.format("ubekreftet"), status_code=302)
    if len(email) > 254 or not _EMAIL_RE.match(email):
        return RedirectResponse(back.format("ugyldig"), status_code=302)
    if store.get_user_by_email(email):  # users.email is unique across all accounts
        return RedirectResponse(back.format("finnes"), status_code=302)
    by_account, by_address = store.invite_attempts(user["tid"], email)
    if by_account >= INVITES_PER_ACCOUNT_HOURLY or by_address >= INVITES_PER_ADDRESS_HOURLY:
        log.warning("invite: throttled (tenant=%s)", user["tid"])
        return RedirectResponse(back.format("for-mange"), status_code=302)
    inv = store.create_invite(user["tid"], email, user["uid"])
    store.invite_bump(user["tid"], email)
    company = _plain((store.get_tenant(user["tid"]) or {}).get("name"))
    sent = mailer.send(
        email,
        "Invitasjon til Sporløs",
        f"Hei,\n\n{me['email']} har invitert deg til {company} på Sporløs, webanalyse uten "
        f"cookies.\n\nKlikk for å bli med (lenken gjelder i {store.INVITE_TTL_DAYS} dager og "
        f"virker én gang):\n{PUBLIC_BASE}/invitasjon?t={inv['token']}\n\n"
        "Kjenner du ikke til dette, kan du se bort fra e-posten.\n\nSporløs",
    )
    if not sent:  # nobody holds the link, so the invite is useless: drop it
        store.delete_invite(inv["id"], user["tid"])
        return RedirectResponse(back.format("sendefeil"), status_code=302)
    log.warning("invite sent: tenant=%s by_user=%s invite=%s", user["tid"], user["uid"], inv["id"])
    return RedirectResponse(back.format("invitert"), status_code=302)


async def invite_revoke(request):
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    f = await request.form()
    try:
        done = store.delete_invite(int(f.get("invite_id") or 0), user["tid"])
    except ValueError:
        done = False
    return RedirectResponse("/app?brukere=trukket#brukere" if done else "/app#brukere", status_code=302)


async def user_remove(request):
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)
    f = await request.form()
    try:
        target = int(f.get("user_id") or 0)
    except ValueError:
        target = 0
    if target == user["uid"]:
        return RedirectResponse("/app?brukere=deg#brukere", status_code=302)
    if target == store.account_owner_id(user["tid"]):
        return RedirectResponse("/app?brukere=eier#brukere", status_code=302)
    if not store.remove_user(target, user["tid"]):  # not in this account: say nothing
        return RedirectResponse("/app#brukere", status_code=302)
    log.warning("user removed: tenant=%s user=%s by_user=%s", user["tid"], target, user["uid"])
    return RedirectResponse("/app?brukere=fjernet#brukere", status_code=302)


def _invite_gone(request):
    return _shell(
        request,
        "Invitasjonen virker ikke",
        "<h1>Invitasjonen virker ikke</h1><p class=muted>Lenken er allerede brukt, trukket "
        f"tilbake eller utløpt (den gjelder i {store.INVITE_TTL_DAYS} dager). Be den som "
        "inviterte deg om å sende en ny.</p>"
        '<p class=muted><a href="/login">Logg inn</a></p>',
    )


def _invite_exists(request, email: str):
    return _shell(
        request,
        "Du har allerede en konto",
        f"<h1>Du har allerede en konto</h1><p class=muted><b>{escape(email)}</b> har allerede "
        "en Sporløs-konto, og en adresse kan bare høre til én konto. Be den som inviterte deg "
        "om å invitere en annen adresse.</p>"
        '<p class=muted><a href="/login">Logg inn</a></p>',
    )


async def invitation(request):
    """/invitasjon?t=…: join the account an invite points to. Password, or Google/Microsoft."""
    if request.method == "POST":
        f = await request.form()
        token, pw = str(f.get("t") or ""), f.get("password") or ""
    else:
        token, pw = request.query_params.get("t") or "", ""
    inv = store.get_invite(token)
    if not inv:
        return _invite_gone(request)
    err = ""
    if request.method == "POST":
        if len(pw) < 8:
            err = "Passordet må ha minst 8 tegn."
        else:
            res = store.accept_invite(inv["id"], hash_password(pw))
            if res == "exists":
                return _invite_exists(request, inv["email"])
            if not res:
                return _invite_gone(request)
            _login(request, res["uid"], res["tid"])
            log.warning("invite accepted: tenant=%s user=%s", res["tid"], res["uid"])
            return RedirectResponse("/app", status_code=302)
    company, t = escape(inv["company"] or ""), escape(token)
    sso = ""
    providers = [p for p in ("google", "microsoft") if innlogg.enabled(p)]
    if providers:
        sso = '<div class=sso><div class=sso-or>eller</div>' + "".join(
            f'<a class=sso-btn href="/invitasjon/sso/{p}?t={t}">{_SSO_ICONS[p]}'
            f"<span>Fortsett med {innlogg.LABELS[p]}</span></a>"
            for p in providers
        ) + "</div>"
    other = (
        "<p class=muted>Du er logget inn på en annen konto nå. Blir du med, logges du inn "
        f"som {escape(inv['email'])} i stedet.</p>" if _user(request) else ""
    )
    eb = f"<div class=err>{escape(err)}</div>" if err else ""
    return _shell(
        request,
        "Bli med",
        f"""<h1>Bli med i {company}</h1>
<p class=muted><b>{escape(inv["inviter"])}</b> har invitert deg til <b>{company}</b> på Sporløs.
Du ser og styrer de samme nettstedene som resten av {company}.</p>{other}{eb}
<form method=post action="/invitasjon">
  <input type=hidden name=t value="{t}">
  <label>E-post</label><input name=email value="{escape(inv["email"])}" readonly autocomplete=username
    style="color:var(--muted);background:var(--bg)">
  <label>Velg passord</label><input name=password type=password required minlength=8 autocomplete=new-password>
  <button>Bli med</button>
</form>
{sso}
<p class=muted>Invitasjonen gjelder til {_short_date(inv["expires_at"])} og virker én gang.</p>""",
    )


async def invitation_sso(request):
    """«Fortsett med Google/Microsoft» on the invite page: the normal SSO flow, with the
    invite id riding along in the flow's own session state."""
    provider = request.path_params.get("provider", "")
    if not innlogg.enabled(provider):
        return _sso_fail()
    inv = store.get_invite(request.query_params.get("t") or "")
    if not inv:
        return _invite_gone(request)
    return await _sso_begin(request, provider, plan="", invite=inv["id"])


def _sso_join(request, invite_id: int, who):
    """Finish an invite with a Google/Microsoft login. The invite decides the account and
    the address (the inviter typed it, and the link was mailed there); the provider's
    e-mail decides nothing. The login must not be bound to anyone yet."""
    inv = store.get_invite_by_id(invite_id)
    if not inv:
        return _invite_gone(request)
    res = store.accept_invite(inv["id"], "!sso", identity=(who.idp_id, who.subject, who.email))
    if res == "taken":
        label = escape(innlogg.LABELS.get(who.provider, ""))
        return _shell(
            request,
            "Allerede i bruk",
            f"<h1>{label}-kontoen er allerede i bruk</h1><p class=muted>Den er koblet til en "
            "annen Sporløs-bruker. Åpne lenken i invitasjonen igjen og velg et passord i stedet.</p>",
        )
    if res == "exists":
        return _invite_exists(request, inv["email"])
    if not res:
        return _invite_gone(request)
    _login(request, res["uid"], res["tid"])
    log.warning("invite accepted: tenant=%s user=%s sso=%s", res["tid"], res["uid"], who.provider)
    return RedirectResponse("/app", status_code=302)


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
h1{{font-size:2rem;letter-spacing:-.02em}}h2{{font-size:1.15rem;margin-top:2rem}}
pre{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:.8rem;overflow-x:auto;font-size:.82rem}}
code{{font-size:.88em}}
table{{border-collapse:collapse;width:100%}}td{{padding:.3rem .5rem;border-bottom:1px solid var(--line);vertical-align:top;font-size:.9rem}}
.muted{{font-size:.85rem;color:var(--muted)}}</style>
{_SELF_SNIPPET}
<div class=wrap>
{_site_nav(request)}
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
<tr><td><code>POST /api/v1/sites</code></td><td>opprett ett nettsted — <code>{{"domain": "dittdomene.no"}}</code>. Idempotent: samme domene gir samme site-ID (200) i stedet for en duplikat (201 = ny)</td></tr>
<tr><td><code>GET /api/v1/stats</code></td><td>KPI-er (unike, visninger, økter, fluktrate) + topplister, med forrige periode til sammenligning</td></tr>
<tr><td><code>GET /api/v1/timeseries</code></td><td>per dag (per time når period=1)</td></tr>
<tr><td><code>GET /api/v1/breakdown</code></td><td>full liste per dimensjon: <code>prop=pages|sources|countries|regions|devices|browsers|os</code> (+ <code>limit</code>, maks 1000)</td></tr>
<tr><td><code>GET /api/v1/goals</code></td><td>mål/konverteringer med rate</td></tr>
<tr><td><code>GET /api/v1/events</code></td><td>egendefinerte hendelser</td></tr>
<tr><td><code>GET /api/v1/ecommerce</code></td><td>e-handel: ordrer + omsetning per valuta, toppprodukter, omsetning per kilde og per betalingsmåte (beløp i øre)</td></tr>
<tr><td><code>GET /api/v1/anchors</code></td><td>forseglede dags-aggregater: sha256-hash + blokkjede-txid — bevis på at historiske tall ikke er endret i etterkant</td></tr>
</table>
<p class=muted>Alle svar er JSON. Land returneres som ISO-koder. Feil gir
<code>{{"error": "..."}}</code> med 400/401/403/404. <code>POST /api/v1/sites</code> er det eneste
skrivende kallet — det oppretter kun et tomt nettsted under din egen konto, og endrer
aldri måledata. Nøkler kan trekkes tilbake når som helst i dashbordet.</p>

<h2>E-handel: send kjøp</h2>
<p>Kall <code>sporlos('purchase', …)</code> fra ordrebekreftelsen, så får du omsetning,
ordrer, snittordre, toppprodukter og omsetning per kilde i dashbordet:</p>
<pre>sporlos('purchase', {{
  revenue: 1198,           // ordresum i kroner
  currency: 'NOK',         // valgfri, NOK er standard
  payment: 'vipps',        // valgfri betalingsmåte-slug, f.eks. vipps/stripe_card/klarna
  items: [
    {{ name: 'eSIM Europa 10 GB', qty: 2, price: 599 }}
  ]
}});</pre>
<p class=muted>Kun beløp, produktnavn og ev. betalingsmåte (kort slug som «vipps» —
fritekst avvises) sendes — vi <b>ber aldri om ordre-ID eller
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
h1{{font-size:2rem;letter-spacing:-.02em}}h2{{font-size:1.15rem;margin-top:2rem}}
ol{{padding-left:1.2rem}}ol li{{margin:.4rem 0}}
pre{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:.8rem;overflow-x:auto;font-size:.78rem;max-height:340px}}
code{{font-size:.88em}}
table{{border-collapse:collapse;width:100%}}td{{padding:.3rem .5rem;border-bottom:1px solid var(--line);vertical-align:top;font-size:.9rem}}
.muted{{font-size:.85rem;color:var(--muted)}}
.note{{background:var(--info-bg);color:var(--info);border-radius:8px;padding:.7rem .9rem;font-size:.88rem}}</style>
{_SELF_SNIPPET}
<div class=wrap>
{_site_nav(request)}
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


def _legal(request, title, inner, path="", desc=""):
    canon = f'<link rel="canonical" href="https://sporlos.no{path}">' if path else ""
    meta_desc = f'<meta name="description" content="{escape(desc)}">' if desc else ""
    return HTMLResponse(
        f"""<!doctype html><html lang=no><meta charset=utf-8>
<title>{escape(title)} — Sporløs</title>
<meta name=viewport content="width=device-width, initial-scale=1">
{meta_desc}{canon}
{_BRAND_HEAD}
<style>{_BRAND_CSS}{_CHROME_CSS}
h1{{font-size:2rem;letter-spacing:-.02em}}h2{{font-size:1.15rem;margin-top:2rem}}
table{{border-collapse:collapse;width:100%}}td{{padding:.3rem .5rem;border-bottom:1px solid var(--line);vertical-align:top}}
.muted{{font-size:.85rem}}</style>
{_SELF_SNIPPET}
<div class=wrap>
{_site_nav(request)}
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


def _render_guide(request, slug):
    g = _GUIDES[slug]
    steg = "".join(f"<li>{s}</li>" for s in g["steg"])
    feller = "".join(f"<li>{f}</li>" for f in g["feller"])
    return HTMLResponse(
        f"""<!doctype html><html lang="no"><head><meta charset="utf-8">
<title>Sporløs på {escape(g['navn'])} — installasjonsguide</title>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta name="description" content="Slik installerer du Sporløs cookieløs webanalyse på {escape(g['navn'])} — uten cookie-banner. {escape(g['krav'])}">
<link rel="canonical" href="https://sporlos.no/integrasjoner/{slug}">
<meta property="og:title" content="Sporløs på {escape(g['navn'])} — installasjonsguide">
<meta property="og:description" content="Slik installerer du Sporløs cookieløs webanalyse på {escape(g['navn'])} — uten cookie-banner. {escape(g['krav'])}">
{_BRAND_HEAD}{_OG_META}
<style>{_BRAND_CSS}{_CHROME_CSS}
h1{{font-size:2rem;letter-spacing:-.02em}}h2{{font-size:1.15rem;margin-top:2rem}}
ol{{padding-left:1.2rem}}ol li{{margin:.45rem 0}}ul{{padding-left:1.2rem}}ul li{{margin:.3rem 0;color:var(--muted);font-size:.92rem}}
pre{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:.8rem;overflow-x:auto;font-size:.78rem}}
.muted{{font-size:.85rem;color:var(--muted)}}
.note{{background:var(--info-bg);color:var(--info);border-radius:8px;padding:.7rem .9rem;font-size:.88rem;margin:1rem 0}}</style>
{_SELF_SNIPPET}</head><body>
<div class=wrap>
{_site_nav(request)}
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
    return _render_guide(request, slug)


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
<meta property="og:title" content="Integrasjoner — Sporløs fungerer med plattformen din">
<meta property="og:description" content="Sporløs cookieløs webanalyse fungerer med WordPress, Shopify, Wix, Squarespace, Webflow, Framer, Ghost og Google Tag Manager — eller hvilken som helst side der du kan lime inn en kodesnutt.">
{_BRAND_HEAD}{_OG_META}
<style>{_BRAND_CSS}{_CHROME_CSS}
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
{_site_nav(request)}
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
        request,
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
        request,
        "Personvernerklæring",
        path="/personvern",
        desc="Slik behandler Sporløs personopplysninger: ingen IP-lagring, ingen cookies, kun daglig-roterende hash.",
        inner="""<h1>Personvernerklæring</h1>
<p>Denne erklæringen beskriver hvordan Datamynt AS behandler personopplysninger som
behandlingsansvarlig for kunder og besøkende på sporlos.no.</p>

<h2>1. Hva vi samler om kunder</h2>
<p>Når du oppretter konto lagrer vi e-post, firmanavn og et kryptert passord. Faktureringsopplysninger
håndteres av vår betalingspartner (Stripe/Vipps); vi lagrer ikke kortnummer.</p>
<p>Velger du å logge inn med Google eller Microsoft, går innloggingen via Datamynt ID, vår egen
innloggingstjeneste på samme server i Oslo. Vi lagrer da leverandørens bruker-ID og e-postadressen
din, slik at vi kjenner deg igjen neste gang. Vi får ikke passordet ditt, og siden laster ingenting
fra Google eller Microsoft før du selv trykker på knappen.</p>
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
<tr><td><b>Datamynt AS</b></td><td>Drift — egen server i Oslo, Norge. Ingen ekstern hostingleverandør. Nattlige sikkerhetskopier lagres på egne maskiner i Oslo, på to adresser.</td></tr>
<tr><td><b>Stripe / Vipps</b></td><td>Betaling</td></tr>
<tr><td><b>Google Workspace</b></td><td>E-post (support og transaksjonsmeldinger til kunder)</td></tr>
<tr><td><b>Google / Microsoft</b></td><td>Kun hvis du selv velger å logge inn med dem</td></tr>
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
.cta{display:inline-block;background:var(--btn-bg);color:#fff;text-decoration:none;padding:.7rem 1.3rem;border-radius:8px;margin-top:1rem}
.fine{font-size:.85rem;color:var(--muted)}
</style>
"""
        + _SELF_SNIPPET
        + "</head><body>"  # eksplisitt head/body — LinkedIn-parseren er pirkete
        + """<div class=wrap>
"""
        + _site_nav(request)
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
    <tr><td>Scriptvekt</td><td class=nei>~90 kB+</td><td class=ja>~1 kB komprimert</td></tr>
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


def _render_blogg_post(request, slug):
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
{_site_nav(request)}
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
    return _render_blogg_post(request, slug)


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
<meta property="og:title" content="Blogg — Sporløs">
<meta property="og:description" content="{escape(_BLOGG_LEDE)}">
<meta property="og:type" content="website">
<meta property="og:url" content="https://sporlos.no/blogg">
<meta property="og:locale" content="nb_NO">
{_BRAND_HEAD}{_OG_META}{_BLOGG_RSS_LINK}
<style>{_BRAND_CSS}{_CHROME_CSS}
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
{_site_nav(request)}
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

# Visningsnavn for AI-assistent-referrers (store.AI_SOURCES) — host uten www.
_AI_NAMES = {
    "chatgpt.com": "ChatGPT", "chat.openai.com": "ChatGPT",
    "perplexity.ai": "Perplexity", "claude.ai": "Claude",
    "gemini.google.com": "Gemini", "bard.google.com": "Gemini",
    "copilot.microsoft.com": "Copilot", "you.com": "You.com", "poe.com": "Poe",
    "phind.com": "Phind", "chat.mistral.ai": "Le Chat", "grok.com": "Grok",
    "chat.deepseek.com": "DeepSeek",
}

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
# Theme toggle: the script lives in _BRAND_HEAD, the button (_THEME_BTN) in the shared header.


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


def _sparkline(series, width=96, height=26):
    """Kompakt trend-sparkline (unike per dag) for oversikts-radene — ren strek,
    ingen akse/hover/JS (til forskjell fra _area_chart). Flat baseline når det
    ikke finnes data, så radhøyden ikke hopper."""
    vals = [max(0, int(v)) for v in (series or [])]
    peak = max(vals, default=0)
    if not vals or peak == 0:
        return (
            f'<svg viewBox="0 0 {width} {height}" class=spark preserveAspectRatio=none aria-hidden=true>'
            f'<line x1=0 y1="{height - 3}" x2="{width}" y2="{height - 3}" '
            'style="stroke:var(--line)" stroke-width=1.5/></svg>'
        )
    n = len(vals)
    pad = 3
    step = (width - 2 * pad) / max(n - 1, 1)
    span = height - 2 * pad
    pts = [(pad + i * step, pad + span * (1 - v / peak)) for i, v in enumerate(vals)]
    if n == 1:  # ett punkt → flat strek over hele bredden
        y = pts[0][1]
        pts = [(pad, y), (width - pad, y)]
    line = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"{line} L{pts[-1][0]:.1f},{height - pad} L{pts[0][0]:.1f},{height - pad} Z"
    return (
        f'<svg viewBox="0 0 {width} {height}" class=spark preserveAspectRatio=none role=img '
        f'aria-label="topp {peak} unike/dag">'
        f'<path d="{area}" fill="var(--accent)" fill-opacity=".13"/>'
        f'<path d="{line}" fill=none style="stroke:var(--accent)" stroke-width=1.7 '
        'stroke-linejoin=round stroke-linecap=round/></svg>'
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
<meta property="og:title" content="{escape(title)}">
<meta property="og:description" content="{escape(description)}">
{_BRAND_HEAD}{_OG_META}
<style>{_BRAND_CSS}{_CHROME_CSS}{_DASH_CSS}
.demobar{{background:var(--info-bg);border:1px solid var(--line);color:var(--info);border-radius:10px;
padding:.6rem .9rem;font-size:.9rem;margin-bottom:1rem}}</style>
</head><body>
<div class=wrap>
{_site_nav(request)}
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


def proof(request):
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


def demo(request):
    """Offentlig live-demo: ekte tall for sporlos.no selv — produktet i drift som bevis."""
    site = store.resolve_site(os.environ.get("SPORLOS_DEMO_SITE", "6LIACtOSP-S7"))
    if not site:
        return _not_found_page(request)
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


def public_dash(request):
    """Opt-in offentlig dashboard per site — deles med lenke, ingen innlogging."""
    pid = request.path_params["public_id"]
    site = store.get_public_site(pid)
    if not site or not site.get("public_dash"):
        return _not_found_page(request)
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


def _csv_cell(v):
    """Neutralise spreadsheet formulas. Paths and sources originate from a public
    endpoint; a cell starting with = + - @ (or tab/CR) is executed by Excel when
    the customer opens the export. New paths always start with "/", but rows
    stored before that rule can hold anything."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def export_csv(request):
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
            w.writerow([_csv_cell(k), r["n"], r["u"]])
    else:
        return PlainTextResponse("ukjent eksport", status_code=400)

    fname = f"sporlos-{_safe_filename(site['domain'])}-{what}-{days}d.csv"
    return Response(
        "\ufeff" + buf.getvalue(),  # BOM: Excel skal lese æøå riktig
        media_type="text/csv; charset=utf-8",
        headers={"content-disposition": f'attachment; filename="{fname}"'},
    )


# Danger zone (delete a site / the account): the .card frame with a red-toned border
# and a red button. The button red is fixed, not --err: --err turns light pink in
# dark mode, and white text on it would be unreadable.
_DANGER_CSS = """
.card.danger{border-color:color-mix(in srgb,var(--err) 45%,var(--line))}
.danger summary{cursor:pointer;color:var(--err);font-weight:600;font-size:1rem}
.danger p{margin:.6rem 0}
.danger label{display:block;font-size:.85rem;color:var(--muted);margin:.8rem 0 .25rem}
.danger input:not([type=hidden]){width:100%;max-width:24rem;box-sizing:border-box;padding:.55rem .6rem;
border:1px solid var(--line);border-radius:8px;font:inherit;font-size:.95rem;background:var(--card);color:var(--ink)}
.danger .btn-danger{display:block;margin-top:1rem}
.btn-danger{background:#b91c1c}.btn-danger:hover{background:#991b1b}
"""


def _note(kind: str, html: str) -> str:
    """Flash message in the same style as pw_flash. `html` must already be escaped."""
    bg, fg = {"ok": ("--ok-bg", "--ok-ink"), "err": ("--err-bg", "--err"), "info": ("--info-bg", "--info")}[kind]
    return (
        f'<p style="background:var({bg});color:var({fg});padding:.5rem .8rem;border-radius:7px;'
        f'font-size:.9rem">{html}</p>'
    )


_KONTO_FLASH = {
    "ikke-eier": ("err", "Bare den som opprettet kontoen kan slette den."),
    "feil": ("err", "Skriv firmanavnet eller e-posten din nøyaktig slik den står, for å bekrefte."),
    "passord": ("err", "Feil passord. Kontoen er ikke slettet."),
    "for-mange": ("err", "For mange feil passord. Vent en time, eller bruk «Glemt passord»."),
    "logginn": ("err", "Logg inn på nytt først (under), så kan du slette kontoen."),
    "annen": ("err", "Det var ikke den Google- eller Microsoft-kontoen du er logget inn med."),
    "klar": ("ok", "Takk. Du kan slette kontoen de neste 10 minuttene."),
}


def _account_delete_card(request, user: dict, tenant: dict, me: dict) -> str:
    """«Slett kontoen» at the bottom of the account section."""
    code = request.query_params.get("konto") or ""
    flash = _note(*_KONTO_FLASH[code]) if code in _KONTO_FLASH else ""
    company = escape(str(tenant.get("name") or ""))
    intro = (
        f"<p class=fine>Sletter kontoen <b>{company}</b> for godt: alle nettsteder med all statistikk, "
        "API-nøklene og alle brukerne. Det kan ikke angres. Vil du ta vare på tallene, last ned CSV "
        "fra hvert nettsted først.</p>"
        "<p class=fine>Sikkerhetskopiene slettes ikke én og én. Dataene forsvinner fra dem når "
        "kopiene roterer ut, senest etter 35 dager.</p>"
    )
    owner = store.account_owner_id(user["tid"])
    running = _running_subscription(tenant)
    if user["uid"] != owner:
        owner_row = store.get_user(owner) if owner else None
        who = escape(owner_row["email"]) if owner_row else "den som opprettet kontoen"
        body = f"<p class=fine>Bare {who}, som opprettet kontoen, kan slette den.</p>"
    elif running == "stripe":
        body = _note(
            "info",
            "Du har et aktivt abonnement. Si det opp under "
            '<a href="/billing/portal" style="color:inherit">Administrer abonnement</a> først. '
            "Kontoen kan slettes når den betalte perioden er ute. Haster det, skriv til post@sporlos.no.",
        )
    elif running == "vipps":
        body = (
            '<div style="background:var(--info-bg);color:var(--info);padding:.5rem .8rem;'
            'border-radius:7px;font-size:.9rem">Du betaler med Vipps. Avslutt avtalen først, i '
            "Vipps-appen eller her: "
            '<form method=post action="/billing/vipps/avslutt" style="display:inline">'
            '<button style="background:none;border:0;padding:0;color:inherit;cursor:pointer;'
            'font:inherit;text-decoration:underline">Avslutt abonnement</button></form>. '
            "Kontoen kan slettes når den betalte perioden er ute. Haster det, skriv til "
            "post@sporlos.no.</div>"
        )
    else:
        login_row = store.get_user_by_email(me.get("email") or "") or {}
        sso_only = str(login_row.get("password_hash") or "").startswith("!")
        fresh = time.time() - int(request.session.get("iat") or 0) <= _REAUTH_SECONDS
        if sso_only and not fresh:
            linked = set(store.identities_for_user(user["uid"]))
            links = "".join(
                f'<a class=sso-link href="/auth/sso/start/{p}?reauth=1">{_SSO_ICONS[p]} '
                f"Logg inn på nytt med {innlogg.LABELS[p]}</a>"
                for p in ("google", "microsoft")
                if innlogg.enabled(p) and innlogg.PROVIDERS[p] in linked
            )
            body = (
                "<p class=fine>Du logger inn med Google eller Microsoft, så vi har ikke noe passord "
                "å spørre om. Logg inn på nytt for å bekrefte at det er deg. Da kan du slette kontoen "
                "de neste 10 minuttene.</p>"
                + (f'<div style="display:flex;gap:.8rem;flex-wrap:wrap">{links}</div>' if links else
                   '<p class=fine>Logg ut og inn igjen, eller sett et passord under '
                   '<a href="/forgot">Glemt passord</a>.</p>')
            )
        else:
            pw_field = (
                "" if sso_only else
                "<label for=del-pw>Passord</label>"
                "<input id=del-pw name=password type=password required autocomplete=current-password>"
            )
            body = (
                '<form method=post action="/app/account/delete">'
                f"<label for=del-confirm>Skriv <b>{company}</b> eller e-posten din for å bekrefte</label>"
                "<input id=del-confirm name=confirm required autocomplete=off autocapitalize=off spellcheck=false>"
                f"{pw_field}"
                '<button class="btn btn-danger">Slett kontoen for godt</button></form>'
            )
    opened = " open" if code in _KONTO_FLASH or code == "abonnement" else ""
    return (
        f"{flash}<div class='card danger' id=slett-konto><details{opened}>"
        f"<summary>Slett kontoen</summary>{intro}{body}</details></div>"
    )


_BRUKERE_FLASH = {
    "invitert": ("ok", "Invitasjonen er sendt. Den gjelder i 7 dager."),
    "finnes": ("err", "Den adressen har allerede en Sporløs-konto. En adresse kan bare høre til én konto."),
    "ugyldig": ("err", "Skriv en gyldig e-postadresse."),
    "for-mange": ("err", "For mange invitasjoner akkurat nå. Prøv igjen om en time."),
    "sendefeil": ("err", "Vi fikk ikke sendt e-posten. Prøv igjen om litt."),
    "ubekreftet": ("err", "Bekreft e-posten din først, så kan du invitere andre."),
    "fjernet": ("ok", "Brukeren er fjernet og logget ut."),
    "trukket": ("ok", "Invitasjonen er trukket tilbake."),
    "deg": ("err", "Du kan ikke fjerne deg selv. Be en annen bruker om det, eller slett hele kontoen under."),
    "eier": ("err", "Den som opprettet kontoen kan ikke fjernes."),
}

_USERS_CSS = """
table.users td{padding:.6rem .2rem}
table.users td:last-child{width:7.5rem}
table.users small{display:block;font-size:.78rem;color:var(--muted);font-weight:400}
table.users td.me{color:var(--muted);font-size:.9rem}
.linkbtn{background:none;border:0;padding:0;font:inherit;font-size:.9rem;cursor:pointer;
color:var(--err);text-decoration:underline}
"""


def _users_card(request, user: dict, me: dict) -> str:
    """«Brukere»: who can log in to this account, open invites, and the invite form."""
    code = request.query_params.get("brukere") or ""
    flash = _note(*_BRUKERE_FLASH[code]) if code in _BRUKERE_FLASH else ""
    rows = ""
    owner = store.account_owner_id(user["tid"])
    for u in store.list_users(user["tid"]):
        email = escape(u["email"])
        if u["id"] == user["uid"] or u["id"] == owner:
            label = " · ".join(x for x in (
                "deg" if u["id"] == user["uid"] else "", "eier" if u["id"] == owner else "") if x)
            rows += f'<tr><td title="{email}">{email}</td><td class=me>{label}</td></tr>'
            continue
        rows += (
            f'<tr><td title="{email}">{email}</td><td>'
            '<form method=post action="/app/users/remove" '
            "onsubmit=\"return confirm('Fjerne brukeren fra kontoen? Den logges ut med en gang.')\">"
            f'<input type=hidden name=user_id value="{int(u["id"])}">'
            "<button class=linkbtn>Fjern</button></form></td></tr>"
        )
    for inv in store.list_invites(user["tid"]):
        email = escape(inv["email"])
        rows += (
            f'<tr><td title="{email}">{email}'
            f"<small>Invitert · gjelder til {_short_date(inv['expires_at'])}</small></td><td>"
            '<form method=post action="/app/users/invite/revoke">'
            f'<input type=hidden name=invite_id value="{int(inv["id"])}">'
            "<button class=linkbtn>Trekk tilbake</button></form></td></tr>"
        )
    if me.get("email_verified"):
        form = (
            '<form class=add method=post action="/app/users/invite" style="margin-top:.8rem">'
            '<input name=email type=email placeholder="kollega@firma.no" required autocomplete=off>'
            "<button class=btn>Inviter</button></form>"
        )
    else:
        form = '<p class=fine style="margin:.8rem 0 0">Bekreft e-posten din først, så kan du invitere kolleger.</p>'
    return (
        f"{flash}<div class=card id=brukere><b>Brukere</b>"
        f"<table class=users style='margin-top:.4rem'>{rows}</table>{form}"
        '<p class=fine style="margin:.6rem 0 0">Alle brukere ser og styrer de samme nettstedene, og kan '
        "invitere og fjerne andre. Bare den som opprettet kontoen kan slette den. Invitasjonen sendes "
        "på e-post og gjelder i 7 dager.</p></div>"
    )


def _site_delete_card(request, site: dict, public_id: str) -> str:
    """Danger zone at the bottom of a site's dashboard."""
    pid = escape(public_id)
    domain = escape(site["domain"])
    failed = request.query_params.get("slett") == "feil"
    err = _note("err", f"Skriv <b>{domain}</b> nøyaktig for å bekrefte.") if failed else ""
    csv = " · ".join(
        f'<a href="/app/export?site={pid}&period=90&what={w}">{w}</a>'
        for w in ("tidsserie", "sider", "kilder", "land")
    )
    return (
        f"<div class='card block danger' id=slett><details{' open' if failed else ''}>"
        f"<summary>Slett nettstedet</summary>{err}"
        f"<p class=hint>Sletter <b>{domain}</b> og alt vi har lagret om det: hendelser, dagstall, "
        "mål, funnels og søkedata. Det kan ikke angres, og sporingskoden slutter å telle.</p>"
        f"<p class=hint>Vil du ta vare på tallene, last ned CSV først (siste 90 dager): {csv}.</p>"
        "<p class=hint>Forseglingen i den offentlige loggen inneholder bare fingeravtrykk av "
        "dagstall og sier ingenting om nettstedet. Den trenger ikke slettes.</p>"
        '<form method=post action="/app/sites/delete">'
        f'<input type=hidden name=site value="{pid}">'
        f"<label for=del-domain>Skriv <b>{domain}</b> for å bekrefte</label>"
        "<input id=del-domain name=confirm required autocomplete=off autocapitalize=off spellcheck=false>"
        '<button class="btn btn-danger">Slett nettstedet</button></form></details></div>'
    )


def dashboard(request):
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
        # Porteføljeoversikt over et valgt vindu (default 7 dager, IKKE «i dag»):
        # ved midnatt nullstilles ellers alle radene til 0, og du kan ikke
        # sammenligne sitene mot hverandre over tid.
        ov_period = request.query_params.get("period", "7")
        if ov_period not in _PERIODS:
            ov_period = "7"
        ov_label, ov_days = _PERIODS[ov_period]
        sites = store.overview_stats(user["tid"], ov_days)
        tenant = store.get_tenant(user["tid"]) or {}
        def _dot(s):
            # Tilkoblet hvis vi noen gang har sett et event; ellers venter på første besøk.
            if s.get("last_ts"):
                return ('<span title="tilkoblet — data mottatt" style="color:var(--ok)">●</span> ')
            return ('<span title="venter på første besøk" style="color:var(--muted)">○</span> ')

        ov_tabs = " ".join(
            f'<a href="/app?period={k}" class="{"on" if k == ov_period else ""}">{escape(v[0])}</a>'
            for k, v in _PERIODS.items()
        )
        single_day = ov_days == 1  # sparkline meningsløs for ett døgn
        rows = "".join(
            f'<tr><td>{_dot(s)}<a href="/app?site={escape(s["public_id"])}&period={ov_period}">'
            f'{escape(s["domain"])}</a></td>'
            f'<td class=trend>{"" if single_day else _sparkline(s["spark"])}</td>'
            f'<td class=num><b>{_fmt_n(s["visitors"])}</b>{_delta(s["visitors"], s["prev_visitors"])}</td>'
            f'<td class=num><b>{_fmt_n(s["pageviews"])}</b>{_delta(s["pageviews"], s["prev_pageviews"])}</td></tr>'
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
            '<p class=fine style="margin:.5rem 0 0">Har du bare logget inn med Google eller Microsoft? '
            'Bruk <a href="/forgot">Glemt passord</a> for å sette et passord.</p>'
            "</details></div>"
        )

        # Innlogging med Google/Microsoft: what's linked, and «Koble til» for the rest.
        sso_html = ""
        if innlogg.enabled():
            linked = set(store.identities_for_user(user["uid"]))
            items = []
            for prov in ("google", "microsoft"):
                if not innlogg.enabled(prov):
                    continue
                name = innlogg.LABELS[prov]
                if innlogg.PROVIDERS[prov] in linked:
                    items.append(f'<span class=fine>{_SSO_ICONS[prov]} {name}: koblet</span>')
                else:
                    items.append(f'<a class=sso-link href="/auth/sso/start/{prov}">{_SSO_ICONS[prov]} Koble til {name}</a>')
            sso_flash = {
                "koblet": '<p style="background:var(--ok-bg);color:var(--ok-ink);padding:.5rem .8rem;border-radius:7px;font-size:.9rem">Koblet. Neste gang kan du logge inn med ett klikk.</p>',
                "opptatt": '<p style="background:var(--err-bg);color:var(--err);padding:.5rem .8rem;border-radius:7px;font-size:.9rem">Den kontoen er allerede koblet til en annen Sporløs-bruker.</p>',
            }.get(request.query_params.get("sso") or "", "")
            sso_html = (
                f'{sso_flash}<div class=card id=konto><b>Innlogging</b>'
                '<div style="display:flex;gap:1.2rem;flex-wrap:wrap;margin-top:.6rem;align-items:center">'
                + "".join(items) + "</div></div>"
            )

        users_html = _users_card(request, user, me or {})
        delete_html = _account_delete_card(request, user, tenant, me or {})
        deleted_site = request.session.pop("deleted_site", None)
        deleted_flash = (
            _note("ok", f"<b>{escape(deleted_site)}</b> er slettet, med all statistikk.")
            if deleted_site else ""
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
<style>{_BRAND_CSS}{_CHROME_CSS}{_SSO_CSS}{_DANGER_CSS}{_USERS_CSS}
h1{{font-size:1.7rem;letter-spacing:-.02em;margin:0 0 .3rem}}
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
form.add input{{flex:1;min-width:0;padding:.6rem;border:1px solid var(--line);border-radius:8px;font-size:.95rem;background:var(--card);color:var(--ink)}}
.fine{{color:var(--muted);font-size:.8rem}}
.ovtabs{{display:flex;flex-wrap:wrap;gap:.3rem;margin:.1rem 0 .8rem}}
.ovtabs a{{padding:.3rem .75rem;border:1px solid var(--line);border-radius:99px;text-decoration:none;color:var(--muted);font-size:.82rem;background:var(--card)}}
.ovtabs a.on{{background:var(--ink);color:var(--bg);border-color:var(--ink)}}
table.ov th:nth-child(2),table.ov td.trend{{width:5.4rem}}
table.ov th:nth-child(3),table.ov td:nth-child(3),table.ov th:nth-child(4),table.ov td:nth-child(4){{width:4.4rem}}
table.ov td.num{{white-space:normal;line-height:1.15;vertical-align:middle}}
table.ov td.num b{{display:block;color:var(--ink);font-weight:700;font-variant-numeric:tabular-nums}}
table.ov td.trend{{overflow:visible;vertical-align:middle}}
table.ov td.trend .spark{{width:5rem;height:1.5rem;display:block;margin-left:auto}}
/* On a phone the trend column would leave no room for the domain name. */
@media(max-width:560px){{table.ov th:nth-child(2),table.ov td.trend{{display:none}}}}
.ov .d{{display:block;font-size:.68rem;font-weight:600;margin-top:.05rem}}
.dg{{color:var(--ok)}}.dr{{color:var(--err)}}.d0{{color:var(--muted)}}</style>
<div class=wrap>
{_site_nav(request)}
<h1>Mine nettsteder</h1>
{deleted_flash}
{verify_banner}
{trial}
{limit_msg}
{vipps_flash}
<h2 class=sec>Nettsteder <span style="float:right;text-transform:none;letter-spacing:0;font-weight:400">{escape(ov_label)}</span></h2>
<div class=ovtabs>{ov_tabs}<a href="/app/seo" style="margin-left:auto">Søk og AI →</a></div>
<div class=card>
<table class=ov><tr><th>Nettsted</th><th>Trend</th><th>Unike</th><th>Visn.</th></tr>
{rows or '<tr><td>ingen nettsteder enda — legg til det første under</td><td class=trend></td><td></td><td></td></tr>'}</table>
</div>
<form class=add method=post action="/app/sites">
  <input name=domain placeholder="dittdomene.no" required>
  <button class=btn>Legg til nettsted</button>
</form>
{plan_sec}
<h2 class=sec>API-tilgang</h2>
{api_html}
<h2 class=sec>Konto</h2>
{users_html}
{pw_flash}
{password_html}
{sso_html}
{delete_html}
<p class=fine style="margin-top:1.5rem">Cookieløs · ingen IP lagret · samtykkefri</p>
</div>
{_SITE_FOOTER}"""
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
            # Betalingsmåte-tabellen vises kun når minst ett kjøp faktisk sendte
            # feltet — sites uten payment i purchase-kallet får ikke en ren «ukjent»-tabell.
            pay = store.revenue_by_payment(site["id"], days, dom)
            pay_html = ""
            if any(pr["method"] != "ukjent" for pr in pay):
                pay_rows = "".join(
                    f"<tr><td>{escape(pr['method'])}</td>"
                    f"<td style='text-align:right;color:var(--muted)'>{_fmt_n(pr['orders'])}</td>"
                    f"<td style='text-align:right'>{_fmt_kr(pr['revenue_cents'], dom)}</td></tr>"
                    for pr in pay
                )
                pay_html = (
                    "<div><table><tr><th>Betalingsmåte</th><th style='text-align:right'>Ordrer</th>"
                    f"<th style='text-align:right'>Omsetning</th></tr>{pay_rows}</table></div>"
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
                "</div>"
                f"{pay_html}"
                "</div>"
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
            "<p class=hint>Kjøp sendes med <code>sporlos('purchase', {…})</code> — kun beløp, "
            "produktnavn og betalingsmåte, uten ordre-ID eller kundedata. Nettleser-rapporterte tall: "
            "veiledende, ikke avregningsgrunnlag. "
            '<a href="/utviklere">Slik sender du kjøp</a>.</p>'
            + body
        )

    # Søk (SEO/GEO) — vises når siten noen gang har fått søkedata (all-time, samme
    # gate-filosofi som e-handel) eller har AI-henvisninger i perioden. Søketall
    # leses fra lag-justerte vinduer (GSC leverer 1–2 døgn på etterskudd) så
    # deltaene sammenligner like fulle vinduer; AI-besøk måles live av oss selv.
    sok_html = ""
    ai = store.ai_referrals(site["id"], days)
    sok_conn = store.get_search_connection(site["id"])
    sok_flash = {
        "ok": '<p style="background:var(--ok-bg);color:var(--ok-ink);padding:.5rem .8rem;border-radius:7px;font-size:.9rem">Search Console er koblet til — tallene hentes ved neste synk (i natt, eller kjør synk manuelt).</p>',
        "delvis": '<p style="background:var(--warn-bg);color:var(--warn);padding:.5rem .8rem;border-radius:7px;font-size:.9rem">Koblet til Google, men fant ingen Search Console-property som matcher domenet — sjekk at kontoen din har tilgang til property-en.</p>',
        "avbrutt": '<p style="background:var(--info-bg);color:var(--info);padding:.5rem .8rem;border-radius:7px;font-size:.9rem">Tilkoblingen ble avbrutt — ingenting er lagret.</p>',
        "feil": '<p style="background:var(--err-bg);color:var(--err);padding:.5rem .8rem;border-radius:7px;font-size:.9rem">Google ga oss ikke varig tilgang — prøv igjen (fjern ev. Sporløs under myaccount.google.com → Sikkerhet → tredjepartstilgang først).</p>',
    }.get(request.query_params.get("gsc") or "", "")
    if sok_conn:
        sok_conn_html = (
            '<p class=fine style="margin:.6rem 0 0">Koblet til Search Console'
            + (f' som {escape(sok_conn["connected_email"])}' if sok_conn.get("connected_email") else "")
            + (f' <code>{escape(sok_conn["gsc_property"])}</code>' if sok_conn.get("gsc_property") else "")
            + ' · <form method=post action="/app/seo/disconnect" style="display:inline">'
            f'<input type=hidden name=site value="{escape(public_id)}">'
            '<button style="background:none;border:0;padding:0;color:var(--err);cursor:pointer;'
            'font-size:inherit;text-decoration:underline">Koble fra</button></form></p>'
        )
    elif _HAS_GOOGLE:
        sok_conn_html = (
            f'<p style="margin:.6rem 0 0"><a class=btn href="/app/seo/connect?site={escape(public_id)}" '
            'style="font-size:.9rem;padding:.45rem .9rem">Koble til Google Search Console</a> '
            '<span class=fine>— godkjenn med Google-kontoen som eier nettstedet, så henter vi '
            'søkeord, klikk og posisjoner automatisk.</span></p>'
        )
    else:
        sok_conn_html = ""
    if store.has_search(site["id"]) or ai["visitors"]:
        sk = store.search_kpis(site["id"], days)
        skp = store.search_kpis(site["id"], days, offset=1)
        aip = store.ai_referrals(site["id"], days, offset=1)
        g, gp = sk["google"], skp["google"]
        pos_delta = (
            _delta(g["position"], gp["position"], invert=True)
            if g["position"] and gp["position"] else ""
        )
        sok_stat = (
            '<div style="display:flex;gap:1.8rem;flex-wrap:wrap;margin:.4rem 0 1rem">'
            f'<div><div style="font-size:1.45rem;font-weight:700">{_fmt_n(g["clicks"])}</div>'
            f'<small style="color:var(--muted)">klikk fra Google</small> {_delta(g["clicks"], gp["clicks"])}</div>'
            f'<div><div style="font-size:1.45rem;font-weight:700">{_fmt_n(g["impressions"])}</div>'
            f'<small style="color:var(--muted)">visninger i søk</small> {_delta(g["impressions"], gp["impressions"])}</div>'
            f'<div><div style="font-size:1.45rem;font-weight:700">{g["position"] if g["position"] is not None else "–"}</div>'
            f'<small style="color:var(--muted)">snittposisjon</small> {pos_delta}</div>'
            f'<div><div style="font-size:1.45rem;font-weight:700">{_fmt_n(ai["visitors"])}</div>'
            f'<small style="color:var(--muted)">besøk fra AI-assistenter</small> {_delta(ai["visitors"], aip["visitors"])}</div>'
            "</div>"
        )
        q_rows = "".join(
            f"<tr><td title=\"{escape(q['k'])}\">{escape(q['k'])}</td>"
            f"<td style='text-align:right'>{_fmt_n(q['clicks'])}</td>"
            f"<td style='text-align:right;color:var(--muted)'>{_fmt_n(q['impressions'])}</td>"
            f"<td style='text-align:right;color:var(--muted)'>{q['position'] if q['position'] is not None else ''}</td></tr>"
            for q in store.search_top(site["id"], days, "query")
        )
        p_rows = "".join(
            f"<tr><td title=\"{escape(p['k'])}\">{escape(p['k'])}</td>"
            f"<td style='text-align:right'>{_fmt_n(p['clicks'])}</td>"
            f"<td style='text-align:right;color:var(--muted)'>{_fmt_n(p['impressions'])}</td></tr>"
            for p in store.search_top(site["id"], days, "page")
        )
        sok_tables = (
            '<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:1rem">'
            "<div><table><tr><th>Søkeord</th><th style='text-align:right'>Klikk</th>"
            "<th style='text-align:right'>Visn.</th><th style='text-align:right'>Pos.</th></tr>"
            f"{q_rows or '<tr><td>ingen søkeord i perioden</td><td></td><td></td><td></td></tr>'}</table></div>"
            "<div><table><tr><th>Sider fra søk</th><th style='text-align:right'>Klikk</th>"
            "<th style='text-align:right'>Visn.</th></tr>"
            f"{p_rows or '<tr><td>ingen sider i perioden</td><td></td><td></td></tr>'}</table></div></div>"
        )
        extras = []
        if sk["bing"]["clicks"] or sk["bing"]["impressions"]:
            extras.append(
                f"Bing: <b>{_fmt_n(sk['bing']['clicks'])}</b> klikk · "
                f"{_fmt_n(sk['bing']['impressions'])} visninger."
            )
        if sk["gmc"]["clicks"] or sk["gmc"]["impressions"]:
            extras.append(
                f"Google Shopping: <b>{_fmt_n(sk['gmc']['clicks'])}</b> klikk · "
                f"{_fmt_n(sk['gmc']['impressions'])} visninger."
            )
        ai_src = store.ai_referral_sources(site["id"], days)
        if ai_src:
            extras.append("AI-assistenter: " + " · ".join(
                f"{escape(_AI_NAMES.get(a['k'].removeprefix('www.'), a['k']))} <b>{a['u']}</b>"
                for a in ai_src
            ) + ".")
        sok_extra = (
            f'<p style="font-size:.9rem;margin:.7rem 0 0">{" &nbsp; ".join(extras)}</p>'
            if extras else ""
        )
        sok_html = (
            '<h3 id=sok>Søk og AI</h3>'
            "<p class=hint>Hvordan folk finner deg: Google/Bing-søk (Search Console-tall) "
            "og henvisninger fra AI-assistenter målt av Sporløs selv.</p>"
            + sok_flash + sok_stat + sok_tables + sok_extra + sok_conn_html
            + '<p style="color:var(--muted);font-size:.78rem;margin:.6rem 0 0">Søketall synkes '
            "daglig og har 1–2 døgns forsinkelse — perioden slutter derfor i forgårs. "
            "AI-besøk telles live.</p>"
        )
    elif sok_conn_html or sok_flash:
        # Ingen søkedata enda, men tilkobling er mulig/gjort — vis seksjonen som
        # onboarding i stedet for å gjemme featuren til første synk.
        sok_html = (
            '<h3 id=sok>Søk og AI</h3>'
            "<p class=hint>Se hvilke søkeord folk finner deg på i Google, og hvor mange "
            "besøk AI-assistenter sender deg — rett i Sporløs.</p>"
            + sok_flash + sok_conn_html
        )

    blocks = "".join(
        f'<div class="card block">{b}</div>'
        for b in (ecom_html, sok_html, campaigns_html, goals_html, funnels_html, nav_html, events_html, verify_html)
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
<style>{_BRAND_CSS}{_CHROME_CSS}{_DASH_CSS}{_DANGER_CSS}</style>
<div class=wrap>
{_site_nav(request)}
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
{_site_delete_card(request, site, public_id)}
<p class=footnote>Cookieløs · ingen IP lagret · samtykkefri ·
Geo: <a href="https://db-ip.com">IP Geolocation by DB-IP</a> (CC BY 4.0)</p>
</div>
{_SITE_FOOTER}
{_BARS_JS}
{_CHART_JS}"""
    )


def seo_page(request):
    """Flåteside: søk (Google/Bing) + AI-henvisninger på tvers av alle nettsteder.
    Samler det GSC/Bing-UI-ene ikke kan: alle properties i ÉN tabell, koblet mot
    trafikken vi selv måler (AI-henvisninger = GEO-signalet)."""
    user = _user(request)
    if not user:
        return RedirectResponse("/login", status_code=302)

    period = request.query_params.get("period", "7")
    if period not in _PERIODS or period == "1":
        period = "7"  # «i dag» er meningsløst med GSC-forsinkelsen
    label, days = _PERIODS[period]
    rows_data = store.seo_overview(user["tid"], days)
    any_search = any(s["clicks"] or s["impressions"] or s["bing_clicks"] for s in rows_data)

    tabs = " ".join(
        f'<a href="/app/seo?period={k}" class="{"on" if k == period else ""}">{escape(v[0])}</a>'
        for k, v in _PERIODS.items()
        if k != "1"
    )
    trows = "".join(
        f'<tr><td><a href="/app?site={escape(s["public_id"])}&period={period}#sok">{escape(s["domain"])}</a></td>'
        f'<td class=num><b>{_fmt_n(s["clicks"])}</b>{_delta(s["clicks"], s["prev_clicks"])}</td>'
        f'<td class=num><b>{_fmt_n(s["impressions"])}</b></td>'
        f'<td class=num><b>{s["position"] if s["position"] is not None else "–"}</b></td>'
        f'<td class=num><b>{_fmt_n(s["bing_clicks"])}</b></td>'
        f'<td class=num><b>{_fmt_n(s["ai"])}</b>{_delta(s["ai"], s["prev_ai"])}</td></tr>'
        for s in rows_data
    )
    setup = ""
    if not any_search:
        setup = (
            '<div class=card><b>Kom i gang med søkedata</b>'
            '<p class=fine style="margin:.4rem 0 0">Sporløs henter tallene fra Google Search '
            "Console og Bing Webmaster Tools og matcher automatisk mot nettstedene dine — "
            "ingen oppsett per nettsted.</p>"
            '<ol class=fine style="margin:.5rem 0 0;padding-left:1.2rem">'
            "<li>Sett <code>GSC_SERVICE_ACCOUNT</code> (service account-JSON) og/eller "
            "<code>BING_WEBMASTER_API_KEY</code> i miljøet.</li>"
            "<li>Gi service-kontoens e-postadresse lesetilgang på hver property i Search Console.</li>"
            "<li>Kjør <code>python -m app.manage seo-sync</code> (og legg den i daglig cron).</li></ol></div>"
        )

    return HTMLResponse(
        f"""<!doctype html><html lang=no><meta charset=utf-8>
<title>Sporløs — søk og AI på tvers</title>
<meta name=viewport content="width=device-width, initial-scale=1">
{_BRAND_HEAD}
<style>{_BRAND_CSS}{_CHROME_CSS}
h1{{font-size:1.7rem;letter-spacing:-.02em;margin:0 0 .3rem}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:1.1rem 1.25rem;margin:.9rem 0}}
table{{border-collapse:collapse;width:100%;table-layout:fixed}}
th,td{{border-bottom:1px solid var(--line);padding:.55rem .2rem;text-align:left;font-size:.95rem;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
th{{color:var(--muted);font-weight:600;font-size:.8rem}}
th:not(:first-child),td:not(:first-child){{text-align:right;width:4.6rem;color:var(--muted)}}
tr:last-child td{{border-bottom:0}}
td a{{color:var(--ink);text-decoration:none;font-weight:600}}td a:hover{{color:var(--accent-deep)}}
td.num{{white-space:normal;line-height:1.15;vertical-align:middle}}
td.num b{{display:block;color:var(--ink);font-weight:700;font-variant-numeric:tabular-nums}}
.d{{display:block;font-size:.68rem;font-weight:600;margin-top:.05rem}}
.dg{{color:var(--ok)}}.dr{{color:var(--err)}}.d0{{color:var(--muted)}}
.fine{{color:var(--muted);font-size:.8rem}}
.ovtabs{{display:flex;flex-wrap:wrap;gap:.3rem;margin:.1rem 0 .8rem}}
.ovtabs a{{padding:.3rem .75rem;border:1px solid var(--line);border-radius:99px;text-decoration:none;color:var(--muted);font-size:.82rem;background:var(--card)}}
.ovtabs a.on{{background:var(--ink);color:var(--bg);border-color:var(--ink)}}
@media(max-width:640px){{.card{{overflow-x:auto}}th:first-child,td:first-child{{width:8rem}}}}</style>
<div class=wrap>
{_site_nav(request)}
<h1>Søk og AI på tvers</h1>
<p class=fine style="margin:0 0 .8rem">Google/Bing-søk og AI-henvisninger for alle nettstedene dine i én tabell — {escape(label)}.</p>
<div class=ovtabs>{tabs}</div>
{setup}
<div class=card>
<table><tr><th>Nettsted</th><th>G-klikk</th><th>Visn.</th><th>Pos.</th><th>Bing</th><th>AI-besøk</th></tr>
{trows or '<tr><td>ingen nettsteder enda</td><td></td><td></td><td></td><td></td><td></td></tr>'}</table>
</div>
<p class=fine>Klikk/visninger/posisjon: Google Search Console (1–2 døgns forsinkelse — perioden slutter i forgårs).
Bing: Bing Webmaster Tools. AI-besøk: unike besøkende henvist fra AI-assistenter, målt live av Sporløs.</p>
</div>
{_SITE_FOOTER}"""
    )


routes = [
    Route("/healthz", healthz),
    Route("/healthz/db", healthz_db),
    Route("/sporlos.js", tracker),
    Route("/sporlos.src.js", tracker_source),
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
    Route("/logout", logout, methods=["GET", "POST"]),
    Route("/auth/sso/start/{provider}", sso_start),
    Route("/auth/sso/callback", sso_callback),
    Route("/auth/sso/bekreft", sso_confirm),
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
    Route("/app/seo", seo_page),
    Route("/app/seo/connect", gsc_connect),
    Route("/app/seo/callback", gsc_callback),
    Route("/app/seo/disconnect", gsc_disconnect, methods=["POST"]),
    Route("/app/export", export_csv),
    Route("/app/api-keys", api_key_create, methods=["POST"]),
    Route("/app/api-keys/revoke", api_key_revoke, methods=["POST"]),
    Route("/app/password", change_password, methods=["POST"]),
    Route("/app/account/delete", account_delete, methods=["POST"]),
    Route("/app/users/invite", user_invite, methods=["POST"]),
    Route("/app/users/invite/revoke", invite_revoke, methods=["POST"]),
    Route("/app/users/remove", user_remove, methods=["POST"]),
    Route("/invitasjon", invitation, methods=["GET", "POST"]),
    Route("/invitasjon/sso/{provider}", invitation_sso),
    Route("/utviklere", utviklere),
    Route("/shopify", shopify_guide),
    Route("/integrasjoner", integrasjoner),
    Route("/integrasjoner/{slug}", platform_guide),
    Route("/blogg", blogg_index),
    Route("/blogg/rss.xml", blogg_rss),  # må stå FØR {slug}-ruta
    Route("/blogg/{slug}", blogg_post),
    Route("/api/v1/sites", api.sites),
    Route("/api/v1/sites", api.create_site, methods=["POST"]),
    Route("/api/v1/stats", api.stats),
    Route("/api/v1/timeseries", api.timeseries),
    Route("/api/v1/breakdown", api.breakdown),
    Route("/api/v1/goals", api.goals),
    Route("/api/v1/events", api.events),
    Route("/api/v1/ecommerce", api.ecommerce),
    Route("/api/v1/anchors", api.anchors),
    Route("/app/sites", create_site_post, methods=["POST"]),
    Route("/app/sites/delete", site_delete, methods=["POST"]),
    Route("/app/goals", goal_create, methods=["POST"]),
    Route("/app/goals/delete", goal_delete, methods=["POST"]),
    Route("/app/funnels", funnel_create, methods=["POST"]),
    Route("/app/funnels/delete", funnel_delete, methods=["POST"]),
]

# IndexNow-nøkkelfila (/<nøkkel>.txt) — kun når INDEXNOW_KEY er satt.
from app import indexnow as _indexnow  # noqa: E402  (etter routes-lista med vilje)

_INDEXNOW_KEY = _indexnow.key()
if _INDEXNOW_KEY:
    async def _indexnow_keyfile(request):
        return PlainTextResponse(_INDEXNOW_KEY)

    routes += [
        Route(f"/{_INDEXNOW_KEY}.txt", _indexnow_keyfile),
]

_CSP = (
    "default-src 'self'; script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; "
    "connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Baseline security headers (Observatory baseline round, 2026-09-16).

    Content-Security-Policy (2026-09-30): every page is self-contained, with no
    third-party script, style, font, image or fetch (checked by grepping all
    rendered HTML), so everything is limited to 'self'. Inline <script>/<style>
    blocks are everywhere, hence 'unsafe-inline'; the policy still stops an
    injected <script src=evil> and exfiltration through fetch/img/connect.
    Stripe and Vipps checkout, GSC OAuth and Google/Microsoft login are all
    server-side redirects (top-level navigation), which CSP doesn't restrict.
    No form-action: Chrome applies it to redirects after a POST, and a payment
    redirect is exactly what a wrong form-action would silently break.

    X-Frame-Options: DENY. Checked whether anything served by this app is
    meant to be framed: the Shopify integration's "Fase 1" pixel (/shopify)
    is a copy-paste install guide, a normal top-level page, not an iframe.
    The "Fase 2" embedded Shopify app (integrations/shopify/app/
    shopify.app.toml has embedded=true, application_url=sporlos.no) is
    scaffolded only — [auth].redirect_urls is empty and app/main.py has no
    OAuth/embedded-UI route, so nothing on sporlos.no is actually loaded in
    a Shopify admin iframe today. If Fase 2 ships, give its route(s) a
    scoped `Content-Security-Policy: frame-ancestors https://admin.shopify.com
    https://*.myshopify.com` (or drop X-Frame-Options there) before wiring up
    the OAuth callback — don't just remove this header globally.
    """

    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers.setdefault("Content-Security-Policy", _CSP)
        # The header shows a logged-in or logged-out variant depending on the session cookie,
        # so a shared cache must never serve one visitor's variant to another.
        if response.headers.get("content-type", "").startswith("text/html"):
            response.headers.append("Vary", "Cookie")
        return response


# Ingestion må ta imot cross-origin beacons fra ethvert kunde-domene.
# Trygt her fordi vi aldri bruker cookies/credentials (cookieløst by design).
middleware = [
    Middleware(SecurityHeadersMiddleware),
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
        max_age=SESSION_MAX_AGE,
    ),
    # Komprimer HTML/CSS/JSON (~70-80% mindre) på markedsførings- og /app-sider.
    # minimum_size hopper over de bittesmå beacon-svarene (POST /api/v1/events).
    Middleware(GZipMiddleware, minimum_size=500),
]

async def _handle_404(request, exc):
    # API and static paths keep Starlette's plain answer; pages get the branded one.
    if request.url.path.startswith(("/api/", "/static/", "/webhooks/")):
        return PlainTextResponse("Not Found", status_code=404)
    return _not_found_page(request)


app = Starlette(routes=routes, middleware=middleware, exception_handlers={404: _handle_404})
