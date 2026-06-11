"""Sporløs — ingestion + dashboard.

Lokal dogfood (SQLite):
    .venv/bin/python3 -m app.manage init                               < /dev/null
    .venv/bin/python3 -m app.manage create-site "Datamynt" merdata.no  < /dev/null
    .venv/bin/uvicorn app.main:app                                     < /dev/null

Prod-lik (Postgres i Docker): se docker-compose.yml / DEPLOY.md.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from starlette.routing import Route

from app import api, mailer, notify, store
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


def _user(request):
    """Innlogget bruker fra session, eller None."""
    uid, tid = request.session.get("uid"), request.session.get("tid")
    return {"uid": uid, "tid": tid} if uid and tid else None


# Google OAuth (OpenID Connect) — aktiveres kun når credentials er satt.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
oauth = None
if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
    from authlib.integrations.starlette_client import OAuth

    oauth = OAuth()
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


def _google_button():
    if not oauth:
        return ""
    return (
        '<a href="/auth/google" style="display:block;text-align:center;border:1px solid #ccc;'
        'border-radius:8px;padding:.6rem;margin-top:1rem;text-decoration:none;color:#222">'
        "Fortsett med Google</a>"
    )


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


async def ingest(request):
    """POST /api/event — fra tracker-snippet. Beregner cookieløs hash, lagrer."""
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "bad json"}, status_code=400)

    public_id = payload.get("s")
    site = store.resolve_site(public_id) if public_id else None
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

    store.insert_event(
        site["id"],
        {
            "name": payload.get("n", "pageview"),
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
        },
    )
    return PlainTextResponse("", status_code=204)


def _clean_utm(v) -> str | None:
    """Kampanjeparameter fra tracker: trim + lengde-cap. Kun hvitlistede nøkler når hit."""
    if not v or not isinstance(v, str):
        return None
    return v.strip()[:120] or None


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

_FAVICON_SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">
<circle cx="32" cy="36" r="16" fill="none" stroke="#2f6fed" stroke-width="7"/>
<line x1="17" y1="55" x2="47" y2="13" stroke="#2f6fed" stroke-width="7" stroke-linecap="round"/>
</svg>"""

_WORDMARK = (
    '<a class=brand href="/"><svg viewBox="0 0 64 64" aria-hidden=true>'
    '<circle cx="32" cy="32" r="16" fill="none" stroke="currentColor" stroke-width="7"/>'
    '<line x1="17" y1="51" x2="47" y2="13" stroke="currentColor" stroke-width="7" stroke-linecap="round"/>'
    "</svg>sporløs</a>"
)

_BRAND_HEAD = (
    '<link rel=icon href="/favicon.svg" type="image/svg+xml">'
    '<meta name=theme-color content="#faf9f6">'
)

_BRAND_CSS = """
@font-face{font-family:'Schibsted Grotesk';font-style:normal;font-weight:400 900;
font-display:swap;src:url(/static/schibsted-grotesk.woff2) format('woff2')}
:root{--bg:#faf9f6;--ink:#17263e;--muted:#5f6b7d;--accent:#2f6fed;--accent-deep:#1d4ed8;
--line:#e8e6e0;--card:#ffffff;--ok:#15803d;
font:17px/1.65 'Schibsted Grotesk',system-ui,-apple-system,"Segoe UI",sans-serif;color:var(--ink)}
html{overflow-y:scroll}
body{margin:0;background:var(--bg);-webkit-font-smoothing:antialiased}
body::before{content:'';display:block;height:3px;
background:linear-gradient(90deg,var(--accent-deep),var(--accent) 45%,#8fb3ff)}
a{color:var(--accent-deep)}
.brand{display:inline-flex;align-items:center;gap:.45rem;font-weight:700;font-size:1.15rem;
letter-spacing:-.02em;color:var(--ink);text-decoration:none}
.brand svg{width:1.12em;height:1.12em;color:var(--accent);transform:translateY(-.02em)}
.btn{display:inline-block;background:var(--ink);color:#fff;text-decoration:none;
padding:.7rem 1.4rem;border-radius:9px;font-weight:600;border:0;font-size:1rem;cursor:pointer;
transition:background .15s,transform .15s,box-shadow .15s}
.btn:hover{background:#0e1a2e;transform:translateY(-1px)}
.btn-accent{background:var(--accent);box-shadow:0 8px 20px -10px rgba(47,111,237,.55)}
.btn-accent:hover{background:var(--accent-deep)}
.muted{color:var(--muted)}
"""

# Sporløs måler sporlos.no med Sporløs — definert ÉN gang, brukt i alle templates.
_SELF_SNIPPET = (
    '<script defer data-site="6LIACtOSP-S7" data-api="https://sporlos.no/api/event" '
    'src="https://sporlos.no/sporlos.js"></script>'
)

# Felles header/footer for alle offentlige sider — samme ramme overalt,
# så ingen side føles som å «dette ut» av nettstedet.
_CHROME_CSS = """
.wrap{max-width:980px;margin:0 auto;padding:0 1.3rem}
nav.site{display:flex;align-items:center;justify-content:space-between;padding:1.4rem 0;gap:.8rem}
nav.site .links{display:flex;gap:1.2rem;align-items:center;font-size:.95rem;flex-wrap:wrap}
nav.site .links a{color:var(--muted);text-decoration:none}
nav.site .links a:hover{color:var(--ink)}
nav.site .links a.btn{color:#fff;padding:.5rem 1rem}
footer.site{background:var(--ink);color:#aeb9cb;font-size:.85rem;line-height:1.9;margin-top:4rem}
footer.site .wrap{padding-top:2.6rem;padding-bottom:3rem}
footer.site a{color:#cdd6e4}
footer.site .brand{color:#fff;margin-bottom:.6rem}
footer.site .brand svg{color:var(--accent)}
"""

_SITE_NAV = (
    "<nav class=site>" + _WORDMARK + '<div class=links>'
    '<a href="/demo">Live demo</a>'
    '<a href="/google-analytics-alternativ">Mot Google Analytics</a>'
    '<a href="/login">Logg inn</a>'
    '<a class="btn btn-accent" href="/signup">Prøv gratis</a></div></nav>'
)

_SITE_FOOTER = (
    "<footer class=site><div class=wrap>" + _WORDMARK + "<br>"
    "Personvennlig webanalyse, bygget i Norge.<br><br>"
    '<a href="/demo">Live demo</a> · '
    '<a href="/google-analytics-alternativ">Sporløs mot Google Analytics</a> · '
    '<a href="https://status.sporlos.no">Status</a> · '
    '<a href="/vilkar">Salgsbetingelser</a> · <a href="/personvern">Personvern</a><br>'
    'Et produkt fra <a href="https://datamynt.no">Datamynt AS</a> · org.nr 936 017 207 · '
    "Maridalsveien 163, 0461 Oslo · post@sporlos.no</div></footer>"
)


async def favicon(request):
    return Response(_FAVICON_SVG, media_type="image/svg+xml",
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


_OG_META = (
    '<meta property=og:image content="https://sporlos.no/static/og.png">'
    '<meta property=og:image:width content="1200">'
    '<meta property=og:image:height content="630">'
    '<meta name=twitter:card content="summary_large_image">'
)


async def landing(request):
    """Offentlig landingsside (§3-15-budskapet)."""
    return HTMLResponse(
        """<!doctype html><html lang=no><meta charset=utf-8>
<title>Sporløs — webanalyse uten cookie-banner</title>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta name=description content="Cookieløs, samtykke-fri webanalyse bygget i Norge. Ingen cookie-banner. Data på norsk-eid infrastruktur.">
<link rel=canonical href="https://sporlos.no/">
<meta property=og:title content="Sporløs — webanalyse uten cookie-banner">
<meta property=og:description content="Cookieløs, samtykke-fri webanalyse bygget i Norge. Ingen cookie-banner. Data på norsk-eid infrastruktur.">
<meta property=og:type content="website">
<meta property=og:url content="https://sporlos.no/">
<meta property=og:locale content="nb_NO">
"""
        + _BRAND_HEAD
        + _OG_META
        + "<style>"
        + _BRAND_CSS
        + _CHROME_CSS
        + """
body{background:radial-gradient(1100px 480px at 78% -120px,rgba(47,111,237,.08),transparent 70%),var(--bg)}
header{padding:3.5rem 0 1rem;max-width:680px;position:relative}
.wm{position:absolute;right:-270px;top:-30px;width:330px;height:330px;opacity:.055;
color:var(--ink);pointer-events:none}
@media(max-width:1080px){.wm{display:none}}
.tag{display:inline-block;color:var(--accent-deep);font-size:.78rem;font-weight:600;
letter-spacing:.09em;text-transform:uppercase;margin-bottom:1.2rem}
h1{font-size:clamp(2.2rem,5.5vw,3.2rem);line-height:1.06;margin:0 0 1.1rem;
letter-spacing:-.03em;font-weight:800}
.lede{font-size:1.2rem;color:var(--muted);max-width:36em}
.hero-ctas{margin:1.8rem 0 .6rem;display:flex;gap:1rem;align-items:center;flex-wrap:wrap}
.fine{font-size:.85rem;color:var(--muted)}
.demo{background:var(--card);border:1px solid var(--line);border-radius:14px;margin:2.6rem 0 0;
padding:1.1rem 1.3rem 1rem;box-shadow:0 18px 50px -28px rgba(23,38,62,.28)}
.demo-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:.8rem}
.demo-top b{font-size:.95rem;letter-spacing:-.01em}
.demo-id{display:flex;align-items:center;gap:.75rem}
.dots{display:inline-flex;gap:5px}
.dots i{width:9px;height:9px;border-radius:50%;background:#e6e2da;display:block}
.demo-top .pills span{padding:.18rem .6rem;border:1px solid var(--line);border-radius:99px;
font-size:.72rem;color:var(--muted);margin-left:.25rem}
.demo-top .pills span.on{background:var(--ink);color:#fff;border-color:var(--ink)}
.demo-kpis{display:grid;grid-template-columns:repeat(3,1fr);gap:.6rem;margin-bottom:.9rem}
.demo-kpi{border:1px solid var(--line);border-radius:9px;padding:.5rem .7rem}
.demo-kpi b{font-size:1.25rem;display:block;letter-spacing:-.02em}
.demo-kpi span{font-size:.68rem;color:var(--muted)}
.demo svg{width:100%;height:110px;display:block}
.demo-axis{display:flex;justify-content:space-between;color:var(--muted);font-size:.68rem;margin:-.1rem 0 .8rem}
.demo-cols{display:grid;grid-template-columns:1fr 1fr;gap:1.2rem}
.demo-cols h4{margin:0 0 .25rem;font-size:.72rem;color:var(--muted);font-weight:600;
letter-spacing:.05em;text-transform:uppercase}
.demo-row{display:flex;justify-content:space-between;font-size:.8rem;padding:.22rem 0;
border-bottom:1px solid var(--line)}
.demo-row:last-child{border-bottom:0}
.demo-row span:last-child{color:var(--muted)}
@media(max-width:560px){.demo-cols{grid-template-columns:1fr}.demo-kpis{grid-template-columns:repeat(3,1fr)}}
.strip{padding:1.2rem 0 2.2rem;border-bottom:1px solid var(--line);font-size:.88rem;color:var(--muted)}
.strip b{color:var(--ink);font-weight:600}
.strip span{white-space:nowrap}
section{padding:3rem 0;border-bottom:1px solid var(--line)}
h2{font-size:1.5rem;letter-spacing:-.015em;margin:0 0 1.2rem}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(230px,1fr));gap:1rem}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:1.2rem 1.3rem}
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
</style>
"""
        + _SELF_SNIPPET
        + """<div class=wrap>
"""
        + _SITE_NAV
        + """
<header>
  <svg class=wm viewBox="0 0 64 64" aria-hidden=true>
    <circle cx="32" cy="32" r="16" fill="none" stroke="currentColor" stroke-width="6"/>
    <line x1="17" y1="51" x2="47" y2="13" stroke="currentColor" stroke-width="6" stroke-linecap="round"/>
  </svg>
  <span class=tag>Norsk · cookieløs · samtykkefri</span>
  <h1>Webanalyse uten cookie&#8209;banner.</h1>
  <p class=lede>Sporløs måler nettstedet ditt uten cookies, uten å lagre IP, og uten å samle
  personopplysninger — så du slipper banneret, og besøkerne dine slipper å bli sporet.</p>
  <div class=hero-ctas>
    <a class=btn href="/signup">Start gratis prøve</a>
    <a href="/google-analytics-alternativ" style="font-size:.95rem">Ærlig sammenligning med GA →</a>
  </div>
  <p class=fine>30 dager gratis · uten kort · åpen kildekode</p>
</header>

<div class=demo aria-hidden=true>
  <div class=demo-top>
    <span class=demo-id><span class=dots><i></i><i></i><i></i></span><b>dittdomene.no</b></span>
    <span class=pills><span>i dag</span><span class=on>7 dager</span><span>30 dager</span></span></div>
  <div class=demo-kpis>
    <div class=demo-kpi><b>4 312</b><span>unike besøkende</span></div>
    <div class=demo-kpi><b>6 980</b><span>sidevisninger</span></div>
    <div class=demo-kpi><b>38 %</b><span>fluktfrekvens</span></div>
  </div>
  <svg viewBox="0 0 880 110" preserveAspectRatio="none">
    <defs><linearGradient id=g x1=0 y1=0 x2=0 y2=1>
      <stop offset=0 stop-color=#2f6fed stop-opacity=.16 />
      <stop offset=1 stop-color=#2f6fed stop-opacity=0 />
    </linearGradient></defs>
    <path d="M8,88 L132,76 L256,80 L380,54 L504,60 L628,34 L752,40 L872,20 L872,104 L8,104 Z" fill=url(#g) />
    <path d="M8,88 L132,76 L256,80 L380,54 L504,60 L628,34 L752,40 L872,20" fill=none stroke=#2f6fed stroke-width=2.5 stroke-linejoin=round stroke-linecap=round />
  </svg>
  <div class=demo-axis><span>3. juni</span><span>10. juni</span></div>
  <div class=demo-cols>
    <div><h4>Topp sider</h4>
      <div class=demo-row><span>/</span><span>2 841</span></div>
      <div class=demo-row><span>/priser</span><span>1 322</span></div>
      <div class=demo-row><span>/blogg/uten-banner</span><span>904</span></div>
    </div>
    <div><h4>Topp kilder</h4>
      <div class=demo-row><span>direkte</span><span>1 988</span></div>
      <div class=demo-row><span>google</span><span>1 471</span></div>
      <div class=demo-row><span>dn.no</span><span>512</span></div>
    </div>
  </div>
</div>
<p class=fine style="text-align:right;margin:.5rem 0 0"><a href="/demo">Se ekte live-tall for denne siden →</a></p>

<div class=strip>
  <b>Måler allerede våre egne nettsteder:</b>
  <span>peck.to</span> · <span>merdata.no</span> · <span>datamynt.no</span> ·
  <span>peck.world</span> · <span>docs.peck.to</span> · <span>peck.cat</span> ·
  <span>overlay.social</span> — og denne siden.
</div>

<section>
  <h2>Hvorfor slipper du banner?</h2>
  <div class=law>
  <p>Ekomloven § 3-15 (i kraft 2025) krever samtykke for å <em>lagre eller lese</em> noe på
  besøkerens enhet. Sporløs rører aldri enheten — ingen cookies, ingen identifikatorer — så kravet
  utløses ikke. Og uten personopplysninger utløses heller ikke GDPR-samtykke.</p>
  <ul>
    <li>Setter aldri cookies eller lagrer noe i nettleseren</li>
    <li>Lagrer aldri IP-adresser (brukes flyktig til en daglig-roterende hash, så forkastes)</li>
    <li>Fingerprinter aldri, følger aldri besøkende på tvers av dager og nettsteder</li>
  </ul>
  </div>
</section>

<section>
  <h2>Alt du faktisk trenger</h2>
  <div class=cards>
    <div class=card><h3>Hele bildet, ikke et utvalg</h3><p>Uten samtykkekrav måles alle besøk —
    ikke bare de som trykker «godta». Tallene blir mer riktige enn med GA, ikke mindre.</p></div>
    <div class=card><h3>Mål, funnels og kampanjer</h3><p>Egendefinerte hendelser, konverteringsrate,
    funnels med drop-off og UTM-kampanjer. Uten at noen blir identifisert.</p></div>
    <div class=card><h3>Data i Norge</h3><p>Norsk-eid drift på servere i Stavanger, utenfor
    rekkevidden til US CLOUD Act. Sporingsscriptet er
    <a href="https://github.com/datamynt/sporlos-tracker">åpen kildekode</a> — etterprøv selv.</p></div>
    <div class=card><h3>Lett som en fjær</h3><p>Sporingsscriptet er ~1,5 kB — rundt en
    sekstidel av Google Analytics. Siden din merker det ikke.</p></div>
    <div class=card><h3>Inngang, utgang og stier</h3><p>Hvor folk lander, hvor de forsvinner og
    hvordan de beveger seg — som aggregat, aldri som enkeltpersoner.</p></div>
    <div class=card><h3>Verifiserbare tall</h3><p>Dagstallene forsegles i en uavhengig offentlig
    logg, så de kan ikke pyntes i etterkant. Dokumentasjon som holder. (Pro)</p></div>
  </div>
</section>

<section>
  <h2>Priser</h2>
  <p class=muted style="margin:0">Etter sidevisninger per måned (totale visninger, ikke unike besøkende) ·
  eks. mva · årlig = 2 måneder gratis. Over grensen? Vi slutter aldri å måle og sender aldri
  overraskelsesregninger — du får et varsel og velger selv om du vil oppgradere.</p>
  <div class=plans>
    <div class=plan><b>Liten</b><span class=pris>99 kr<small>/mnd</small></span>
      <small class=hva>10 000 visninger<br>1 nettsted</small></div>
    <div class="plan hl"><b>Vekst</b><span class=pris>249 kr<small>/mnd</small></span>
      <small class=hva>100 000 visninger<br>10 nettsteder</small></div>
    <div class=plan><b>Pro</b><span class=pris>599 kr<small>/mnd</small></span>
      <small class=hva>1 mill. visninger<br>15 nettsteder<br>verifiserbare tall</small></div>
    <div class=plan><b>Byrå</b><span class=pris>fra 1 490 kr</span>
      <small class=hva>fra 25 kundenettsteder<br>white-label · forsegling inkl.</small></div>
  </div>
  <p class=fine>Prøv hostet gratis i 30 dager — uten kort. Vil du ha det helt gratis?
  Sporløs er åpen kildekode — kjør det på egen server. Enterprise/kommune: ta kontakt.</p>
  <a class=btn href="/signup" style="margin-top:.8rem">Start gratis prøve</a>
  <p class=fine style="margin-top:.7rem"><a href="/login">Har du konto? Logg inn</a></p>
</section>

</div>
"""
        + _SITE_FOOTER
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
    if _user(request):
        return RedirectResponse("/app", status_code=302)
    err = ""
    if request.method == "POST":
        f = await request.form()
        company = (f.get("company") or "").strip()
        email = (f.get("email") or "").strip().lower()
        pw = f.get("password") or ""
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
                return RedirectResponse("/app", status_code=302)
            except Exception:
                err = "Kunne ikke opprette konto. Prøv igjen."
    eb = f'<div class=err>{escape(err)}</div>' if err else ""
    return _shell(
        "Opprett konto",
        f"""<h1>Opprett konto</h1><p class=muted>30 dager gratis · uten kort.</p>{eb}
<form method=post>
  <label>Firma</label><input name=company required>
  <label>E-post</label><input name=email type=email required>
  <label>Passord</label><input name=password type=password required minlength=8>
  <button>Start gratis prøve</button>
</form>
{_google_button()}
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
{_google_button()}
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
        if u and u["password_hash"] != "!google-oauth":
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
    if not oauth:
        return RedirectResponse("/login", status_code=302)
    return await oauth.google.authorize_redirect(request, f"{PUBLIC_BASE}/auth/google/callback")


async def google_callback(request):
    if not oauth:
        return RedirectResponse("/login", status_code=302)
    try:
        token = await oauth.google.authorize_access_token(request)
    except Exception:
        return RedirectResponse("/login", status_code=302)
    info = token.get("userinfo") or {}
    email = (info.get("email") or "").strip().lower()
    if not email or info.get("email_verified") is False:
        return RedirectResponse("/login", status_code=302)
    u = store.get_user_by_email(email)
    if u:
        request.session["uid"], request.session["tid"] = u["id"], u["tenant_id"]
    else:
        # Ny Google-bruker → opprett konto. Sentinel-hash => kan ikke passord-logge.
        name = info.get("name") or email.split("@")[0]
        tid, uid = store.create_account(name, email, "!google-oauth")
        request.session["uid"], request.session["tid"] = uid, tid
    return RedirectResponse("/app", status_code=302)


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
    if domain:
        try:
            store.create_site(user["tid"], domain)
        except Exception:
            pass  # f.eks. duplikat domene under samme konto
    return RedirectResponse("/app", status_code=302)


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
<tr><td><code>GET /api/v1/anchors</code></td><td>forseglede dags-aggregater: sha256-hash + blokkjede-txid — bevis på at historiske tall ikke er endret i etterkant</td></tr>
</table>
<p class=muted>Alle svar er JSON. Land returneres som ISO-koder. Feil gir
<code>{{"error": "..."}}</code> med 400/401/404. Nøkler kan trekkes tilbake når som helst i dashbordet.</p>

<h2>Eksempel: spør en AI om tallene dine</h2>
<p>Lim denne siden + nøkkelen din inn i Claude eller ChatGPT og be den f.eks.
«hent siste 30 dager for nettstedet mitt og forklar hva som driver trafikken».
Verktøy som kan gjøre HTTP-kall trenger ikke mer enn dette.</p>
</div></div>
{_SITE_FOOTER}"""
    )


def _legal(title, inner):
    return HTMLResponse(
        f"""<!doctype html><html lang=no><meta charset=utf-8>
<title>{escape(title)} — Sporløs</title>
<meta name=viewport content="width=device-width, initial-scale=1">
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


async def vilkar(request):
    return _legal(
        "Salgsbetingelser",
        """<h1>Salgsbetingelser</h1>
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
        """<h1>Personvernerklæring</h1>
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
        """<!doctype html><html lang=no><meta charset=utf-8>
<title>Norsk alternativ til Google Analytics — ærlig sammenligning | Sporløs</title>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta name=description content="Hva mister du og hva får du ved å bytte fra Google Analytics til Sporløs? Ærlig sammenligning: cookie-banner, datakvalitet, Google Ads, SEO og pris.">
<link rel=canonical href="https://sporlos.no/google-analytics-alternativ">
<meta property=og:title content="Norsk alternativ til Google Analytics — ærlig sammenligning">
<meta property=og:description content="Hva mister du og hva får du ved å bytte fra Google Analytics? Ærlig sammenligning uten skjønnmaling.">
<meta property=og:type content="article">
<meta property=og:url content="https://sporlos.no/google-analytics-alternativ">
<meta property=og:locale content="nb_NO">
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
    <li><b>E-handelsrapporter på produktnivå.</b> Ikke støttet ennå.</li>
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
    <tr><td>Scriptvekt</td><td class=nei>~90 kB+</td><td class=ja>~1,5 kB</td></tr>
    <tr><td>Datalagring</td><td class=nei>Google (USA-tilknyttet)</td><td class=ja>Norge, norsk-eid drift</td></tr>
    <tr><td>Google Ads-integrasjon</td><td class=ja>Ja</td><td class=nei>Nei</td></tr>
    <tr><td>Bruker-/segmentanalyse, BigQuery</td><td class=ja>Ja</td><td class=nei>Nei (kun aggregater)</td></tr>
    <tr><td>Mål, funnels, kilder, enheter</td><td class=ja>Ja</td><td class=ja>Ja</td></tr>
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


async def sitemap(request):
    pages = ["/", "/demo", "/google-analytics-alternativ", "/signup", "/vilkar", "/personvern", "/utviklere"]
    urls = "".join(f"<url><loc>https://sporlos.no{p}</loc></url>" for p in pages)
    return Response(
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{urls}</urlset>',
        media_type="application/xml",
    )


_PERIODS = {"1": ("i dag", 1), "7": ("7 dager", 7), "30": ("30 dager", 30)}

# Plan-grenser: (visninger/mnd, antall nettsteder). None = ubegrenset.
# Filosofi: vi KASTER ALDRI kundens data — over grensen vises varsel og
# oppgraderings-nudge, men målingen fortsetter. Hard grense kun på å legge
# til flere nettsteder. Trial får Vekst-nivå (raus prøve).
_PLAN_LIMITS = {
    "trial": (100_000, 5),
    "liten": (10_000, 1),
    "vekst": (100_000, 10),
    "pro": (1_000_000, 15),
    "byra": (None, None),
    "owner": (None, None),
    "cancelled": (0, 0),
}


def _plan_limits(plan: str) -> tuple[int | None, int | None]:
    return _PLAN_LIMITS.get(plan or "trial", _PLAN_LIMITS["trial"])


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

# Delt dashboard-CSS (innlogget dashboard + offentlig live-demo).
_DASH_CSS = """
.wrap{max-width:980px;margin:0 auto;padding:0 1.2rem 4rem}
nav{display:flex;align-items:center;justify-content:space-between;padding:1.2rem 0 1.6rem}
nav .links{display:flex;gap:1.1rem;align-items:center;font-size:.9rem}
nav .links a{color:var(--muted);text-decoration:none}nav .links a:hover{color:var(--ink)}
nav .links a.btn{color:#fff;padding:.45rem .9rem}
.head{display:flex;align-items:baseline;justify-content:space-between;gap:1rem;flex-wrap:wrap;margin-bottom:1rem}
h1{font-size:1.7rem;letter-spacing:-.02em;margin:0}
.tabs a{padding:.32rem .8rem;margin-left:.3rem;border:1px solid var(--line);border-radius:99px;
text-decoration:none;color:var(--muted);font-size:.85rem;background:var(--card)}
.tabs a.on{background:var(--ink);color:#fff;border-color:var(--ink)}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:1.1rem 1.25rem}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:.8rem;margin:1rem 0}
.kpi b{font-size:1.9rem;display:block;line-height:1.15;letter-spacing:-.02em}
.kpi span{color:var(--muted);font-size:.8rem}
.kpi .d{display:block;font-size:.78rem;font-weight:600;margin-top:.2rem}
.dg{color:var(--ok)}.dr{color:#b91c1c}.d0{color:var(--muted)}
.chartcard{margin:0 0 .9rem;padding-bottom:.6rem}
.chart{width:100%;height:170px;display:block}
.chart circle{fill:transparent}.chart circle:hover{fill:var(--accent)}
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
details summary{cursor:pointer;color:var(--accent-deep);font-size:.9rem}
pre{background:#f3f1ec;padding:.8rem;border-radius:8px;overflow:auto;font-size:.78rem}
.footnote{color:var(--muted);font-size:.8rem;margin-top:2rem}
.footnote a{color:var(--muted)}
"""


# Av/på for andelssøylene i tabellene — huskes i nettleseren (localStorage).
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


def _stat_table(items, key):
    """Nøkkel/antall-tabell: andelssøyle bak hver rad (relativt til toppraden),
    ellipsis-trunkering og full verdi som tooltip."""
    mx = max((i["n"] for i in items), default=0) or 1
    rows = ""
    for i in items:
        pct = i["n"] / mx * 100
        rows += (
            f'<tr><td title="{escape(str(i[key]))}" style="background:linear-gradient(90deg,'
            f'#e9effd {pct:.0f}%,transparent {pct:.0f}%);border-radius:4px;padding-left:.45rem">'
            f'{escape(str(i[key]))}</td><td>{i["n"]}</td></tr>'
        )
    return f"<table>{rows or '<tr><td>ingen data enda</td></tr>'}</table>"


_VS_LABEL = {"1": "i går", "7": "forrige 7 dager", "30": "forrige 30 dager"}


def _delta(now, before, invert=False):
    """↑/↓-endring mot forrige periode. invert=True når lavere er bedre (flukt)."""
    if not before:
        return ""
    pct = round((now - before) / before * 100)
    if pct == 0:
        return '<small class="d d0" title="mot forrige periode">±0 %</small>'
    up = pct > 0
    good = (not up) if invert else up
    return (
        f'<small class="d {"dg" if good else "dr"}" title="mot forrige periode">'
        f'{"↑" if up else "↓"} {abs(pct)} %</small>'
    )


def _verify_table(rollups):
    rr = "".join(
        f'<tr><td>{escape(str(r["day"])[:10])}</td><td>{r["visitors"]}</td><td>{r["pageviews"]}</td>'
        f'<td style="font-family:monospace;font-size:.72rem;color:#999">{escape((r["rollup_hash"] or "")[:12])}…</td>'
        f'<td>{"✓ forankret" if r.get("txid") else "venter"}</td></tr>'
        for r in rollups
    )
    return (
        "<h3>Verifiserbare tall</h3>"
        '<p style="color:#666;font-size:.85rem">Daglige tall forsegles med en kryptografisk hash og '
        "forankres i en uavhengig, offentlig logg — så de ikke kan endres i ettertid.</p>"
        "<table><tr><th>Dag</th><th>Unike</th><th>Visn.</th><th>Segl</th><th>Status</th></tr>"
        f"{rr or '<tr><td>ingen forseglede dager enda</td><td></td><td></td><td></td><td></td></tr>'}</table>"
    )


def _area_chart(series, width=880, height=170):
    """SVG-areagraf (unike per bucket): aksent-linje + gradientflate + hover-punkter.
    Rene rette segmenter — ærlig dataviz, ingen utjevning som lyver mellom punktene."""
    if not series:
        return '<p class=muted style="font-size:.9rem">ingen data enda</p>'
    pad_x, pad_top, pad_bot = 8, 14, 22
    peak = max((b["visitors"] for b in series), default=0) or 1
    n = len(series)
    step = (width - 2 * pad_x) / max(n - 1, 1)
    span = height - pad_top - pad_bot
    pts = [
        (pad_x + i * step, pad_top + span * (1 - b["visitors"] / peak))
        for i, b in enumerate(series)
    ]
    if n == 1:  # ett punkt: tegn en flat strek over hele bredden
        y = pts[0][1]
        pts = [(pad_x, y), (width - pad_x, y)]
    line = "M" + " L".join(f"{x:.1f},{y:.1f}" for x, y in pts)
    area = f"{line} L{pts[-1][0]:.1f},{height - pad_bot} L{pts[0][0]:.1f},{height - pad_bot} Z"
    dots = "".join(
        f'<circle cx="{pad_x + i * step:.1f}" cy="{pad_top + span * (1 - b["visitors"] / peak):.1f}" r="9">'
        f"<title>{escape(str(b['bucket']))}: {b['visitors']} unike · {b['pageviews']} visn.</title></circle>"
        for i, b in enumerate(series)
    ) if n > 1 else ""
    first, last = escape(str(series[0]["bucket"])), escape(str(series[-1]["bucket"]))
    return (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="none" class=chart role=img>'
        '<defs><linearGradient id=cg x1=0 y1=0 x2=0 y2=1>'
        '<stop offset=0 stop-color="#2f6fed" stop-opacity=".16"/>'
        '<stop offset=1 stop-color="#2f6fed" stop-opacity="0"/></linearGradient></defs>'
        f'<path d="{area}" fill="url(#cg)"/>'
        f'<path d="{line}" fill=none stroke="#2f6fed" stroke-width="2.5" '
        'stroke-linejoin=round stroke-linecap=round/>'
        f"{dots}</svg>"
        f'<div class=axis><span>{first}</span><span>topp: {peak} unike</span><span>{last}</span></div>'
    )


async def demo(request):
    """Offentlig live-demo: ekte tall for sporlos.no selv, read-only.
    Produktet i drift som bevis — og grunnmur for fremtidig public-dashboard-toggle per site."""
    site = store.resolve_site(os.environ.get("SPORLOS_DEMO_SITE", "6LIACtOSP-S7"))
    if not site:
        return PlainTextResponse("not found", status_code=404)

    period = request.query_params.get("period", "7")
    if period not in _PERIODS:
        period = "7"
    label, days = _PERIODS[period]

    s = store.stats(site["id"], days)
    s["countries"] = [{**c, "k": country_no(c["k"])} for c in s["countries"]]
    prev = store.kpis(site["id"], days, offset=1)
    chart = _area_chart(store.timeseries(site["id"], days))
    flow = store.flow_stats(site["id"], days)
    verify_html = _verify_table(store.recent_rollups(site["id"]))

    tabs = " ".join(
        f'<a href="/demo?period={k}" class="{"on" if k == period else ""}">{escape(v[0])}</a>'
        for k, v in _PERIODS.items()
    )

    return HTMLResponse(
        f"""<!doctype html><html lang=no><meta charset=utf-8>
<title>Live demo — ekte tall for sporlos.no | Sporløs</title>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta name=description content="Sporløs i drift: ekte, levende statistikk for sporlos.no — cookieløst og uten samtykke. Slik ser dashbordet ut.">
<link rel=canonical href="https://sporlos.no/demo">
{_BRAND_HEAD}{_OG_META}
<style>{_BRAND_CSS}{_CHROME_CSS}{_DASH_CSS}
.demobar{{background:#eef3ff;border:1px solid #d6e2ff;color:var(--accent-deep);border-radius:10px;
padding:.6rem .9rem;font-size:.9rem;margin-bottom:1rem}}</style>
<div class=wrap>
{_SITE_NAV}
<div class=demobar>Dette er ekte, levende tall for <b>sporlos.no</b> — målt av Sporløs selv,
uten cookies og uten samtykke. Det du ser her, er det kundene får.</div>
<div class=head><h1>sporlos.no <span class=muted style="font-size:1rem;font-weight:400">· live demo</span></h1>
<div class=tabs>{tabs}</div></div>
<div class=kpis>
  <div class="card kpi"><b>{s['visitors']}</b><span>unike besøkende</span>{_delta(s['visitors'], prev['visitors'])}</div>
  <div class="card kpi"><b>{s['sessions']}</b><span>besøk</span>{_delta(s['sessions'], prev['sessions'])}</div>
  <div class="card kpi"><b>{s['pageviews']}</b><span>sidevisninger</span>{_delta(s['pageviews'], prev['pageviews'])}</div>
  <div class="card kpi"><b>{s['bounce_rate']}%</b><span>fluktfrekvens</span>{_delta(s['bounce_rate'], prev['bounce_rate'], invert=True)}</div>
  <div class="card kpi"><b>{s['views_per_session']}</b><span>visn. per besøk</span></div>
</div>
<div class="card chartcard">
<p class=muted style="font-size:.8rem;margin:.1rem 0 .6rem">Unike besøkende · {escape(label)} <span style="float:right">endring målt mot {_VS_LABEL[period]}</span></p>
{chart}
</div>
<div class=grid>
  <div class=card><h3>Topp sider</h3>{_stat_table(s['top_paths'], 'path')}</div>
  <div class=card><h3>Topp kilder</h3>{_stat_table(s['top_sources'], 'src')}</div>
  <div class=card><h3>Inngangssider</h3>{_stat_table(flow['entries'], 'path')}</div>
  <div class=card><h3>Utgangssider</h3>{_stat_table(flow['exits'], 'path')}</div>
  <div class=card><h3>Land</h3>{_stat_table(s['countries'], 'k')}</div>
  <div class=card><h3>Fylke / region</h3>{_stat_table(s['regions'], 'k')}</div>
  <div class=card><h3>Enheter</h3>{_stat_table(s['devices'], 'k')}</div>
</div>
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
{_SELF_SNIPPET}""",
        headers={"cache-control": "public, max-age=60"},
    )


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
        for b in store.timeseries(site["id"], days):
            w.writerow([b["bucket"], b["visitors"], b["pageviews"]])
    elif what in ("sider", "kilder", "land"):
        w.writerow([what[:-1] if what != "land" else "land", "sidevisninger", "unike besøkende"])
        for r in store.export_breakdown(site["id"], days, what):
            k = country_no(r["k"]) if what == "land" else r["k"]
            w.writerow([k, r["n"], r["u"]])
    else:
        return PlainTextResponse("ukjent eksport", status_code=400)

    fname = f"sporlos-{site['domain']}-{what}-{days}d.csv"
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
            '<p style="background:#fff7ed;color:#9a3412;padding:.5rem .8rem;border-radius:7px;'
            f'font-size:.9rem">{sent}Bekreft e-posten din ({escape(me["email"])}) — sjekk innboksen, '
            'eller <a href="/resend-verify" style="color:#9a3412;text-decoration:underline">send på nytt</a>.</p>'
        )

    if not site:
        sites = store.list_sites(user["tid"])
        tenant = store.get_tenant(user["tid"]) or {}
        rows = "".join(
            f'<tr><td><a href="/app?site={escape(s["public_id"])}">{escape(s["domain"])}</a></td>'
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
                '<p style="background:#fef2f2;color:#b91c1c;padding:.5rem .8rem;border-radius:7px;'
                'font-size:.9rem"><b>Prøveperioden er utløpt.</b> Tallene dine samles fortsatt '
                "(vi kaster aldri data) — velg en plan under for å fortsette.</p>"
            )
        elif plan == "trial" and tenant.get("trial_ends_at"):
            trial = (
                '<p style="background:#eef2ff;color:#3730a3;padding:.5rem .8rem;border-radius:7px;'
                f'font-size:.9rem">Prøveperiode — utløper {escape(str(tenant["trial_ends_at"])[:10])}.</p>'
            )

        # Forbruk mot plan (skjules for ubegrensede planer)
        usage_html = ""
        if pv_lim:
            pct = min(100, round(usage["pageviews"] / pv_lim * 100))
            over = usage["pageviews"] > pv_lim
            color = "#b91c1c" if over else ("#a16207" if pct >= 80 else "#15803d")
            warn = ""
            if over:
                warn = (
                    '<p style="color:#b91c1c;margin:.4rem 0 0">Over planens visninger denne '
                    "måneden — alt måles fortsatt, men vurder å oppgradere.</p>"
                )
            usage_html = (
                '<div style="background:#fff;border:1px solid #e8e6e0;border-radius:12px;'
                'padding:.9rem 1.1rem;font-size:.85rem;color:#5f6b7d;margin:.9rem 0">'
                f'Visninger denne måneden: <b style="color:#17263e">{_fmt_n(usage["pageviews"])}</b> '
                f"av {_fmt_n(pv_lim)}"
                f'<div style="background:#eee;border-radius:99px;height:6px;margin:.35rem 0">'
                f'<div style="width:{pct}%;background:{color};height:6px;border-radius:99px"></div></div>'
                f'Nettsteder: {usage["sites"]} av {site_lim}{warn}</div>'
            )

        limit_msg = ""
        if request.query_params.get("limit") == "sites":
            limit_msg = (
                '<p style="background:#fef2f2;color:#b91c1c;padding:.5rem .8rem;border-radius:7px;'
                f'font-size:.9rem">Planen din har plass til {site_lim} nettsted'
                f'{"er" if (site_lim or 0) != 1 else ""} — oppgrader for å legge til flere.</p>'
            )
        upgrade = ""
        if stripe and tenant.get("plan") in ("trial", "cancelled", None):
            btns = "".join(
                f'<a href="/billing/checkout?plan={k}" style="display:inline-block;'
                "margin:.2rem .4rem .2rem 0;padding:.4rem .7rem;border:1px solid #3730a3;"
                'border-radius:7px;text-decoration:none;color:#3730a3;font-size:.9rem">'
                f"{escape(_PLAN_LABELS[k])}</a>"
                for k in ("liten", "vekst", "pro")
                if STRIPE_PRICES.get(k)
            )
            if btns:
                upgrade = (
                    f'<div style="margin:1rem 0"><b>Oppgrader:</b><br>{btns}<br>'
                    '<span style="color:#888;font-size:.8rem">Faktura/EHF for byrå/kommune? '
                    '<a href="/vilkar">Kontakt oss</a></span></div>'
                )
        # API-tilgang: read-only nøkler for AI-verktøy/integrasjoner
        keys = store.list_api_keys(user["tid"])
        new_key = request.session.pop("new_api_key", None)
        new_key_html = ""
        if new_key:
            new_key_html = (
                '<p style="background:#ecfdf5;color:#065f46;padding:.6rem .8rem;border-radius:7px;'
                'font-size:.85rem;word-break:break-all"><b>Ny nøkkel — kopier den nå, den vises '
                f"ikke igjen:</b><br><code>{escape(new_key)}</code></p>"
            )
        key_rows = "".join(
            f'<tr><td>{escape(k["label"])} <small style="color:#999">{escape(k["prefix"])}…</small></td>'
            f'<td>{escape(str(k["created_at"])[:10])}</td>'
            f'<td>{escape(str(k["last_used_at"])[:10]) if k["last_used_at"] else "aldri"}</td>'
            f'<td><form method=post action="/app/api-keys/revoke" style="display:inline">'
            f'<input type=hidden name=key_id value="{k["id"]}">'
            '<button title="Trekk tilbake" style="background:none;border:0;color:#c00;cursor:pointer">✕</button>'
            "</form></td></tr>"
            for k in keys
        )
        keys_table = (
            f"<table><tr><th>Nøkkel</th><th>Laget</th><th>Sist brukt</th><th></th></tr>{key_rows}</table>"
            if key_rows
            else ""
        )
        api_html = (
            '<div class=card><h3 style="margin-top:0">API-tilgang</h3>'
            '<p class=fine style="margin:.2rem 0 .6rem">Read-only nøkler for AI-verktøy og '
            'integrasjoner — kun aggregater, aldri rådata. <a href="/utviklere">Dokumentasjon</a>.</p>'
            f"{new_key_html}{keys_table}"
            '<form class=add method=post action="/app/api-keys" style="margin-top:.6rem">'
            '<input name=label placeholder="Navn (f.eks. Claude)" maxlength=60>'
            "<button class=btn>Lag API-nøkkel</button></form></div>"
        )

        planinfo = ""
        if tenant.get("plan") in ("liten", "vekst", "pro"):
            label = {"liten": "Liten", "vekst": "Vekst", "pro": "Pro"}[tenant["plan"]]
            portal = (
                ' · <a href="/billing/portal" style="color:#3730a3">Administrer abonnement</a>'
                if stripe and tenant.get("stripe_customer_id")
                else ""
            )
            planinfo = (
                '<p style="background:#ecfdf5;color:#065f46;padding:.5rem .8rem;border-radius:7px;'
                f'font-size:.9rem"><b>Plan:</b> {label}{portal}</p>'
            )
        return HTMLResponse(
            f"""<!doctype html><html lang=no><meta charset=utf-8>
<title>Sporløs — mine sites</title>
<meta name=viewport content="width=device-width, initial-scale=1">
{_BRAND_HEAD}
<style>{_BRAND_CSS}
.wrap{{max-width:640px;margin:0 auto;padding:0 1.2rem 4rem}}
nav{{display:flex;align-items:center;justify-content:space-between;padding:1.2rem 0 1.6rem}}
nav a.ut{{color:var(--muted);text-decoration:none;font-size:.9rem}}
h1{{font-size:1.6rem;letter-spacing:-.02em;margin:0 0 .3rem}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:1.1rem 1.25rem;margin:.9rem 0}}
table{{border-collapse:collapse;width:100%;table-layout:fixed}}
th,td{{border-bottom:1px solid var(--line);padding:.55rem .2rem;text-align:left;font-size:.95rem;
overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
th{{color:var(--muted);font-weight:600;font-size:.8rem}}
th:not(:first-child),td:not(:first-child){{text-align:right;width:5.5rem;color:var(--muted)}}
tr:last-child td{{border-bottom:0}}
td a{{color:var(--ink);text-decoration:none;font-weight:600}}td a:hover{{color:var(--accent-deep)}}
form.add{{display:flex;gap:.5rem}}
form.add input{{flex:1;padding:.6rem;border:1px solid var(--line);border-radius:8px;font-size:.95rem;background:var(--card)}}
.fine{{color:var(--muted);font-size:.8rem}}</style>
<div class=wrap>
<nav>{_WORDMARK}<a class=ut href="/logout">Logg ut</a></nav>
<h1>Mine sites</h1><p class="fine" style="margin:0 0 1rem">tall for i dag</p>
{verify_banner}
{trial}
{limit_msg}
{planinfo}
{usage_html}
{upgrade}
<div class=card>
<table><tr><th>Nettsted</th><th>Unike</th><th>Visn.</th></tr>
{rows or '<tr><td>ingen sites enda — legg til ett under</td><td></td><td></td></tr>'}</table>
</div>
<form class=add method=post action="/app/sites">
  <input name=domain placeholder="dittdomene.no" required>
  <button class=btn>Legg til nettsted</button>
</form>
{api_html}
<p class=fine style="margin-top:1.5rem">Cookieløs · ingen IP lagret · samtykkefri</p>
</div>"""
        )

    period = request.query_params.get("period", "7")
    if period not in _PERIODS:
        period = "7"
    label, days = _PERIODS[period]

    s = store.stats(site["id"], days)
    s["countries"] = [{**c, "k": country_no(c["k"])} for c in s["countries"]]
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

    chart = _area_chart(series)

    table = _stat_table

    # Mål / konverteringer
    goal_rows = "".join(
        f'<tr><td>{escape(g["name"])} <small style="color:#999">'
        f'({escape(g["match_type"])}: {escape(g["match_value"])})</small></td>'
        f'<td>{g["completions"]}</td><td>{g["rate"]}%</td>'
        f'<td><form method=post action="/app/goals/delete" style="display:inline">'
        f'<input type=hidden name=site value="{escape(public_id)}">'
        f'<input type=hidden name=goal_id value="{g["id"]}">'
        '<button title="Slett" style="background:none;border:0;color:#c00;cursor:pointer">✕</button>'
        "</form></td></tr>"
        for g in goals
    )
    goals_html = (
        "<h3>Mål / konverteringer</h3>"
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
            f'<small style="color:#999">({escape(st["type"])})</small></td>'
            f'<td>{st["count"]}</td><td>{st["rate"]}%</td></tr>'
            for i, st in enumerate(fu["steps"])
        )
        frows += (
            f'<div style="margin:.8rem 0"><b>{escape(fu["name"])}</b> '
            '<form method=post action="/app/funnels/delete" style="display:inline">'
            f'<input type=hidden name=site value="{escape(public_id)}">'
            f'<input type=hidden name=funnel_id value="{fu["id"]}">'
            '<button title="Slett" style="background:none;border:0;color:#c00;cursor:pointer">✕</button></form>'
            f"<table>{steprows}</table></div>"
        )
    if not frows:
        frows = '<p style="color:#888;font-size:.9rem">Ingen funnels enda.</p>'
    funnels_html = (
        f"<h3>Funnels</h3>{frows}"
        '<form method=post action="/app/funnels" style="margin:.5rem 0;font-size:.9rem">'
        f'<input type=hidden name=site value="{escape(public_id)}">'
        '<input name=name placeholder="Navn (f.eks. Kjøpstrakt)" required '
        'style="padding:.4rem;border:1px solid #ccc;border-radius:6px;width:100%;box-sizing:border-box;margin-bottom:.4rem">'
        '<textarea name=steps required rows=4 placeholder="Ett steg per linje, i rekkefolge:&#10;/&#10;/priser&#10;signup" '
        'style="width:100%;box-sizing:border-box;padding:.4rem;border:1px solid #ccc;border-radius:6px;font:inherit"></textarea>'
        '<button style="background:#1a1a1a;color:#fff;border:0;padding:.4rem .8rem;border-radius:6px;cursor:pointer;margin-top:.4rem">Lag funnel</button>'
        '<div style="color:#888;font-size:.8rem">Linjer som starter med / = sti, ellers = hendelse. Min. 2 steg.</div>'
        "</form>"
    )
    nav_rows = "".join(
        f'<tr><td title="{escape(tr["from"])} → {escape(tr["to"])}">'
        f'{escape(tr["from"])} → {escape(tr["to"])}</td><td>{tr["n"]}</td></tr>'
        for tr in transitions
    )
    nav_html = (
        "<h3>Navigasjonsstier</h3>"
        '<p style="color:#888;font-size:.85rem">Vanligste side→side-overganger innen en økt.</p>'
        "<table><tr><th>Fra → Til</th><th>Antall</th></tr>"
        f"{nav_rows or '<tr><td>ingen overganger enda</td><td></td></tr>'}</table>"
    )
    event_rows = "".join(
        f'<tr><td>{escape(e["k"])}</td><td>{e["u"]}</td><td>{e["n"]}</td></tr>' for e in events
    )
    events_html = (
        "<h3>Hendelser</h3><table><tr><th>Hendelse</th><th>Unike</th><th>Totalt</th></tr>"
        f"{event_rows or '<tr><td>ingen hendelser enda</td><td></td><td></td></tr>'}</table>"
    )
    # Verifiserbare tall (B): forseglet hash per dag, status forankret/venter
    verify_html = _verify_table(rollups)

    # Kampanjer (UTM) — vises kun når det finnes kampanjetrafikk i perioden.
    camp_rows = "".join(
        f"<tr><td>{escape(' · '.join(x for x in (c['source'], c['medium'], c['campaign']) if x) or 'ukjent')}</td>"
        f"<td style='text-align:right;color:#666;width:5rem'>{c['visitors']}</td>"
        f"<td style='text-align:right;color:#666;width:5rem'>{c['n']}</td></tr>"
        for c in s["campaigns"]
    )
    campaigns_html = ""
    if camp_rows:
        campaigns_html = (
            "<h3>Kampanjer (UTM)</h3>"
            "<table><tr><th style='text-align:left;color:#888;font-size:.85rem'>Kilde · medium · kampanje</th>"
            "<th style='text-align:right;color:#888;font-size:.85rem'>Unike</th>"
            f"<th style='text-align:right;color:#888;font-size:.85rem'>Visn.</th></tr>{camp_rows}</table>"
        )

    blocks = "".join(
        f'<div class="card block">{b}</div>'
        for b in (campaigns_html, goals_html, funnels_html, nav_html, events_html, verify_html)
        if b
    )

    return HTMLResponse(
        f"""<!doctype html><html lang=no><meta charset=utf-8>
<title>Sporløs — {escape(site['domain'])}</title>
<meta name=viewport content="width=device-width, initial-scale=1">
{_BRAND_HEAD}
<style>{_BRAND_CSS}{_DASH_CSS}</style>
<div class=wrap>
<nav>{_WORDMARK}<div class=links><a href="/app">Mine sites</a><a href="/logout">Logg ut</a></div></nav>
{verify_banner}
<div class=head><h1>{escape(site['domain'])}</h1><div class=tabs>{tabs}</div></div>
<div class=kpis>
  <div class="card kpi"><b>{s['visitors']}</b><span>unike besøkende</span>{_delta(s['visitors'], prev['visitors'])}</div>
  <div class="card kpi"><b>{s['sessions']}</b><span>besøk</span>{_delta(s['sessions'], prev['sessions'])}</div>
  <div class="card kpi"><b>{s['pageviews']}</b><span>sidevisninger</span>{_delta(s['pageviews'], prev['pageviews'])}</div>
  <div class="card kpi"><b>{s['bounce_rate']}%</b><span>fluktfrekvens</span>{_delta(s['bounce_rate'], prev['bounce_rate'], invert=True)}</div>
  <div class="card kpi"><b>{s['views_per_session']}</b><span>visn. per besøk</span></div>
</div>
<div class="card chartcard">
<p class=muted style="font-size:.8rem;margin:.1rem 0 .6rem">Unike besøkende · {escape(label)} <span style="float:right">endring målt mot {_VS_LABEL[period]}</span></p>
{chart}
</div>
<p class=muted style="font-size:.8rem;margin:-.2rem 0 .9rem">Last ned CSV (regneark):
  <a href="/app/export?site={escape(public_id)}&period={period}&what=tidsserie">tidsserie</a> ·
  <a href="/app/export?site={escape(public_id)}&period={period}&what=sider">sider</a> ·
  <a href="/app/export?site={escape(public_id)}&period={period}&what=kilder">kilder</a> ·
  <a href="/app/export?site={escape(public_id)}&period={period}&what=land">land</a>
  · <a href="#" id=barstoggle>andelssøyler av/på</a></p>
<div class=grid>
  <div class=card><h3>Topp sider</h3>{table(s['top_paths'], 'path')}</div>
  <div class=card><h3>Topp kilder</h3>{table(s['top_sources'], 'src')}</div>
  <div class=card><h3>Inngangssider</h3>{table(flow['entries'], 'path')}</div>
  <div class=card><h3>Utgangssider</h3>{table(flow['exits'], 'path')}</div>
  <div class=card><h3>Land</h3>{table(s['countries'], 'k')}</div>
  <div class=card><h3>Fylke / region</h3>{table(s['regions'], 'k')}</div>
  <div class=card><h3>Enheter</h3>{table(s['devices'], 'k')}</div>
  <div class=card><h3>Nettlesere</h3>{table(s['browsers'], 'k')}</div>
  <div class=card><h3>Operativsystem</h3>{table(s['os'], 'k')}</div>
</div>
{blocks}
<div class="card block"><details><summary>Vis sporings-kode</summary>
<pre>{escape(f'<script defer data-site="{public_id}" data-api="{PUBLIC_BASE}/api/event" src="{PUBLIC_BASE}/sporlos.js"></script>')}</pre></details></div>
<p class=footnote>Cookieløs · ingen IP lagret · samtykkefri ·
Geo: <a href="https://db-ip.com">IP Geolocation by DB-IP</a> (CC BY 4.0)</p>
</div>
{_BARS_JS}"""
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
    Route("/robots.txt", robots),
    Route("/sitemap.xml", sitemap),
    Route("/favicon.svg", favicon),
    Route("/static/schibsted-grotesk.woff2", brand_font),
    Route("/static/og.png", og_image),
    Route("/signup", signup, methods=["GET", "POST"]),
    Route("/login", login, methods=["GET", "POST"]),
    Route("/registrer", _alias("/signup")),
    Route("/logg-inn", _alias("/login")),
    Route("/sammenligning", _alias("/google-analytics-alternativ")),
    Route("/forgot", forgot, methods=["GET", "POST"]),
    Route("/reset", reset, methods=["GET", "POST"]),
    Route("/unsubscribe", unsubscribe),
    Route("/verify", verify_email),
    Route("/resend-verify", resend_verify),
    Route("/logout", logout),
    Route("/auth/google", google_login),
    Route("/auth/google/callback", google_callback, name="google_callback"),
    Route("/billing/checkout", billing_checkout),
    Route("/billing/portal", billing_portal),
    Route("/webhooks/stripe", stripe_webhook, methods=["POST"]),
    Route("/app", dashboard),
    Route("/app/export", export_csv),
    Route("/app/api-keys", api_key_create, methods=["POST"]),
    Route("/app/api-keys/revoke", api_key_revoke, methods=["POST"]),
    Route("/utviklere", utviklere),
    Route("/api/v1/sites", api.sites),
    Route("/api/v1/stats", api.stats),
    Route("/api/v1/timeseries", api.timeseries),
    Route("/api/v1/breakdown", api.breakdown),
    Route("/api/v1/goals", api.goals),
    Route("/api/v1/events", api.events),
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
]

app = Starlette(routes=routes, middleware=middleware)
