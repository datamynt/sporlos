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

from app import mailer, notify, store
from app.auth import check_token, hash_password, verify_password
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
            "country": country,
            "region": region,
            "device": device,
            "browser": browser,
            "os": os_,
            "visitor_hash": vhash,
        },
    )
    return PlainTextResponse("", status_code=204)


def _normalize_referrer(ref: str | None) -> str | None:
    """Reduser referrer til ren kilde-host (ingen query/PII)."""
    if not ref:
        return None
    try:
        from urllib.parse import urlparse

        return urlparse(ref).netloc or None
    except Exception:
        return None


async def landing(request):
    """Offentlig landingsside (§3-15-budskapet). Design-runde kommer senere."""
    return HTMLResponse(
        """<!doctype html><meta charset=utf-8>
<title>Sporløs — webanalyse uten cookie-banner</title>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta name=description content="Cookieløs, samtykke-fri webanalyse bygget i Norge. Ingen cookie-banner. Data på norsk-eid infrastruktur.">
<style>
:root{font:18px/1.6 system-ui;color:#1a1a1a}
body{margin:0}
.wrap{max-width:680px;margin:0 auto;padding:0 1.2rem}
header{padding:5rem 0 3rem}
h1{font-size:2.6rem;line-height:1.1;margin:0 0 1rem}
.lede{font-size:1.25rem;color:#444}
.tag{display:inline-block;background:#eef2ff;color:#3730a3;font-size:.8rem;padding:.25rem .7rem;border-radius:99px;margin-bottom:1.5rem}
section{padding:2rem 0;border-top:1px solid #eee}
h2{font-size:1.2rem;margin:0 0 .6rem}
ul{padding-left:1.2rem;margin:.5rem 0}li{margin:.3rem 0}
.cta{display:inline-block;background:#1a1a1a;color:#fff;text-decoration:none;padding:.7rem 1.3rem;border-radius:8px;margin-top:1rem}
footer{padding:3rem 0;color:#888;font-size:.85rem}
a{color:#3730a3}
</style>
<script defer data-site="6LIACtOSP-S7" data-api="https://sporlos.no/api/event" src="https://sporlos.no/sporlos.js"></script>
<div class=wrap>
<header>
  <span class=tag>Norsk · cookieløs · samtykke-fri</span>
  <h1>Webanalyse uten cookie-banner.</h1>
  <p class=lede>Sporløs måler nettstedet ditt uten cookies, uten å lagre IP, og uten å samle personopplysninger — så du slipper samtykke-banner, og besøkerne dine slipper å bli sporet.</p>
</header>
<section>
  <h2>Hvorfor slipper du banner?</h2>
  <p>Ekomloven § 3-15 (i kraft 2025) krever samtykke for å <em>lagre eller lese</em> noe på besøkerens enhet. Sporløs rører aldri enheten — ingen cookies, ingen identifikatorer — så kravet utløses ikke.</p>
  <p>Kommer du fra Google Analytics? <a href="/google-analytics-alternativ">Les den ærlige sammenligningen →</a></p>
</section>
<section>
  <h2>Hva Sporløs aldri gjør</h2>
  <ul>
    <li>Setter cookies eller lagrer noe i nettleseren</li>
    <li>Lagrer IP-adresser (brukes flyktig til en daglig-roterende hash, så forkastes)</li>
    <li>Fingerprinter eller følger besøkende på tvers av dager og nettsteder</li>
  </ul>
</section>
<section>
  <h2>Bygget i Norge, data i Norge</h2>
  <p>Kjører på norsk-eid infrastruktur (EU-eid sky, servere i Norge) — utenfor rekkevidden til US CLOUD Act. Åpen kildekode, så du kan etterprøve det selv.</p>
</section>
<section>
  <h2>Priser</h2>
  <p style="color:#444">Pris etter sidevisninger per måned. Eks. mva · årlig betaling = 2 måneder gratis.</p>
  <table style="width:100%;border-collapse:collapse;margin:1rem 0">
    <tr style="border-bottom:1px solid #eee"><td style="padding:.6rem 0"><b>Liten</b><br><small style="color:#888">10 000 visninger · 1 nettsted</small></td><td style="text-align:right">99 kr/mnd</td></tr>
    <tr style="border-bottom:1px solid #eee"><td style="padding:.6rem 0"><b>Vekst</b><br><small style="color:#888">100 000 visninger · 5 nettsteder</small></td><td style="text-align:right">249 kr/mnd</td></tr>
    <tr style="border-bottom:1px solid #eee"><td style="padding:.6rem 0"><b>Pro</b><br><small style="color:#888">1 mill. visninger · 15 nettsteder · verifiserbare tall</small></td><td style="text-align:right">599 kr/mnd</td></tr>
    <tr style="border-bottom:1px solid #eee"><td style="padding:.6rem 0"><b>Byrå / white-label</b><br><small style="color:#888">fra 25 kundenettsteder · anchring inkl.</small></td><td style="text-align:right">fra 1 490 kr/mnd</td></tr>
    <tr><td style="padding:.6rem 0"><b>Self-host</b><br><small style="color:#888">din egen server · åpen kildekode</small></td><td style="text-align:right">gratis</td></tr>
  </table>
  <p style="color:#666;font-size:.9rem">Prøv hostet gratis i 30 dager — uten kort. Vil du ha det helt gratis? Kjør det selv.</p>
</section>
<section>
  <a class=cta href="/signup">Start gratis prøve</a>
  <p class=muted style="margin-top:.8rem"><a href="/login">Har du konto? Logg inn</a></p>
</section>
<footer>
  <a href="/vilkar">Salgsbetingelser</a> · <a href="/personvern">Personvern</a><br>
  Sporløs · personvernvennlig webanalyse<br>
  Datamynt AS · org.nr 936 017 207 · Maridalsveien 163, 0461 Oslo · post@datamynt.no
</footer>
</div>"""
    )


def _shell(title, inner):
    return HTMLResponse(
        f"""<!doctype html><meta charset=utf-8>
<title>{escape(title)} — Sporløs</title>
<meta name=viewport content="width=device-width, initial-scale=1">
<style>body{{font:16px system-ui;max-width:380px;margin:4rem auto;padding:0 1rem;color:#1a1a1a}}
h1{{font-size:1.5rem}}label{{display:block;margin:.8rem 0 .2rem;font-size:.9rem;color:#444}}
input{{width:100%;padding:.6rem;border:1px solid #ccc;border-radius:7px;font-size:1rem;box-sizing:border-box}}
button{{margin-top:1.2rem;width:100%;background:#1a1a1a;color:#fff;border:0;padding:.7rem;border-radius:8px;font-size:1rem;cursor:pointer}}
.err{{background:#fee;color:#900;padding:.6rem;border-radius:7px;font-size:.9rem;margin:.5rem 0}}
.muted{{color:#888;font-size:.85rem;margin-top:1.2rem}}a{{color:#3730a3}}</style>
<script defer data-site="6LIACtOSP-S7" data-api="https://sporlos.no/api/event" src="https://sporlos.no/sporlos.js"></script>
{inner}"""
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
            'Vil du ha dem tilbake, kontakt oss på post@datamynt.no.</p>'
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


def _legal(title, inner):
    return HTMLResponse(
        f"""<!doctype html><meta charset=utf-8>
<title>{escape(title)} — Sporløs</title>
<meta name=viewport content="width=device-width, initial-scale=1">
<style>body{{font:17px/1.65 system-ui;max-width:680px;margin:0 auto;padding:3rem 1.2rem;color:#1a1a1a}}
h1{{font-size:2rem}}h2{{font-size:1.15rem;margin-top:2rem}}a{{color:#3730a3}}
.muted{{color:#888;font-size:.85rem}}table{{border-collapse:collapse;width:100%}}td{{padding:.3rem .5rem;border-bottom:1px solid #eee;vertical-align:top}}</style>
<p class=muted><a href="/">← Sporløs</a></p>
{inner}
<p class=muted style="margin-top:3rem">Datamynt AS · org.nr 936 017 207 · Maridalsveien 163, 0461 Oslo · post@datamynt.no<br>
Sist oppdatert 2026-06-09 · utkast, kvalitetssikres av jurist.</p>"""
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
E-post: <b>post@datamynt.no</b> · Telefon: +47 48 27 99 19</p>

<h2>2. Tjenesten og priser</h2>
<p>Sporløs er personvernvennlig webanalyse. Planer og priser fremgår av <a href="/">sporlos.no</a>,
oppgitt i NOK. (Datamynt er foreløpig ikke mva-registrert; mva tilkommer fra registreringstidspunktet.)</p>

<h2>3. Avtaleinngåelse</h2>
<p>Avtalen er bindende når bestillingen er sendt og bekreftet. Du må være myndig for å inngå avtale.</p>

<h2>4. Betaling</h2>
<p>Betaling skjer med Vipps eller betalingskort, forskuddsvis per betalingsperiode. Næringsdrivende
kan etter avtale betale mot faktura/EHF (post@datamynt.no).</p>

<h2>5. Levering</h2>
<p>Tjenesten gjøres tilgjengelig umiddelbart etter at avtalen er inngått.</p>

<h2>6. Løpetid, fornyelse og oppsigelse</h2>
<p><b>Ingen bindingstid.</b> Abonnementet løper fortløpende og fornyes automatisk for en ny periode
(måned eller år) til gjeldende pris inntil det sies opp. <b>Du kan si opp når som helst</b>, med
virkning fra utløpet av inneværende betalte periode.</p>
<p><b>Slik sier du opp:</b> betaler du med Vipps, kan du se og avslutte den faste avtalen direkte i
Vipps-appen. Ellers avslutter du i tjenesten eller ved å kontakte oss på <b>post@datamynt.no</b>.
Allerede betalt periode refunderes ikke, men du belastes ikke videre. Prisendringer varsles i rimelig tid.</p>

<h2>7. Angrerett (forbrukere)</h2>
<p>Som forbruker har du 14 dagers angrerett etter angrerettloven. For digitale tjenester som leveres
umiddelbart ber vi om ditt uttrykkelige samtykke til at leveringen starter før angrefristen utløper;
du erkjenner da at angreretten bortfaller når tjenesten er levert. Den gratis prøveperioden lar deg
uansett teste kostnadsfritt før kjøp. (Angrerett gjelder ikke ved salg til næringsdrivende.)</p>

<h2>8. Prøveperiode</h2>
<p>Nye kunder får 30 dager gratis, uten betalingskort og uten bindingstid. Prøveperioden går ikke
over til betalt abonnement uten at du aktivt velger en plan.</p>

<h2>9. Reklamasjon</h2>
<p>Ved feil eller mangel, kontakt oss på post@datamynt.no. Forbrukere har rettigheter etter
forbrukerkjøpsloven.</p>

<h2>10. Behandling av data</h2>
<p>Sporløs samler ikke personopplysninger om dine besøkende. Se <a href="/personvern">personvernerklæringen</a>;
for næringsdrivende gjelder i tillegg databehandleravtale (på forespørsel).</p>

<h2>11. Ansvar</h2>
<p>Tjenesten leveres "som den er" med tilstrebet høy oppetid. For næringsdrivende er vårt samlede
ansvar begrenset til vederlag betalt siste 12 måneder; forbrukeres ufravikelige rettigheter berøres ikke.</p>

<h2>12. Klage og konfliktløsning</h2>
<p>Ta først kontakt med oss på post@datamynt.no. Forbrukere kan klage til Forbrukertilsynet/Forbrukerrådet.
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

<h2>2. Besøkende på sporlos.no</h2>
<p>Vi måler vårt eget nettsted med Sporløs — cookieløst, uten å lagre IP og uten
personopplysninger. Derfor settes ingen sporings-cookies og det kreves ikke samtykke.</p>

<h2>3. Formål og grunnlag</h2>
<p>Vi behandler kontoopplysninger for å levere og fakturere tjenesten (avtale, personvern­forordningen
art. 6 nr. 1 b) og for support. Vi sender ikke markedsføring uten samtykke.</p>

<h2>4. Databehandlere og lagring</h2>
<table>
<tr><td><b>UpCloud</b></td><td>Hosting — servere i Stavanger, Norge (EU-eid)</td></tr>
<tr><td><b>Stripe / Vipps</b></td><td>Betaling</td></tr>
</table>
<p>Kontoopplysninger lagres så lenge du er kunde, og slettes innen rimelig tid etter at
kundeforholdet opphører.</p>

<h2>5. Dine rettigheter</h2>
<p>Du har rett til innsyn, retting, sletting og dataportabilitet. Kontakt oss på
post@datamynt.no. Du kan klage til Datatilsynet (datatilsynet.no).</p>

<h2>6. Analyse på vegne av kunder</h2>
<p>Når du bruker Sporløs på ditt eget nettsted, er du behandlingsansvarlig og vi er
databehandler. Da gjelder databehandleravtalen, ikke denne erklæringen.</p>""",
    )


async def ga_alternativ(request):
    """Ærlig sammenligning mot Google Analytics. Content/SEO-side, offentlig."""
    return HTMLResponse(
        """<!doctype html><meta charset=utf-8>
<title>Norsk alternativ til Google Analytics — ærlig sammenligning | Sporløs</title>
<meta name=viewport content="width=device-width, initial-scale=1">
<meta name=description content="Hva mister du og hva får du ved å bytte fra Google Analytics til Sporløs? Ærlig sammenligning: cookie-banner, datakvalitet, Google Ads, SEO og pris.">
<style>
:root{font:18px/1.6 system-ui;color:#1a1a1a}
body{margin:0}
.wrap{max-width:680px;margin:0 auto;padding:0 1.2rem}
header{padding:4rem 0 2rem}
h1{font-size:2.1rem;line-height:1.15;margin:0 0 1rem}
.lede{font-size:1.15rem;color:#444}
section{padding:1.6rem 0;border-top:1px solid #eee}
h2{font-size:1.25rem;margin:0 0 .6rem}
ul{padding-left:1.2rem;margin:.5rem 0}li{margin:.35rem 0}
table{border-collapse:collapse;width:100%;font-size:.95rem;margin:1rem 0}
td,th{padding:.5rem .6rem;border-bottom:1px solid #eee;text-align:left;vertical-align:top}
th{font-size:.85rem;color:#888;font-weight:600}
.ja{color:#15803d}.nei{color:#b91c1c}.delvis{color:#a16207}
.cta{display:inline-block;background:#1a1a1a;color:#fff;text-decoration:none;padding:.7rem 1.3rem;border-radius:8px;margin-top:1rem}
footer{padding:3rem 0;color:#888;font-size:.85rem}
a{color:#3730a3}
.muted{color:#888;font-size:.85rem}
</style>
<script defer data-site="6LIACtOSP-S7" data-api="https://sporlos.no/api/event" src="https://sporlos.no/sporlos.js"></script>
<div class=wrap>
<p class=muted style="padding-top:1.5rem"><a href="/">← Sporløs</a></p>
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
    <li><b>UTM-kampanjeparametre.</b> Kilder måles via referrer i dag; utvidet kampanjesporing står på
    planen. Trenger du detaljert kampanjeattribusjon nå, er GA sterkere.</li>
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

<footer>
  <a href="/vilkar">Salgsbetingelser</a> · <a href="/personvern">Personvern</a><br>
  Sporløs · personvernvennlig webanalyse<br>
  Datamynt AS · org.nr 936 017 207 · Maridalsveien 163, 0461 Oslo · post@datamynt.no
</footer>
</div>"""
    )


_PERIODS = {"1": ("i dag", 1), "7": ("7 dager", 7), "30": ("30 dager", 30)}


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
        trial = ""
        if tenant.get("plan") == "trial" and tenant.get("trial_ends_at"):
            trial = (
                '<p style="background:#eef2ff;color:#3730a3;padding:.5rem .8rem;border-radius:7px;'
                f'font-size:.9rem">Prøveperiode — utløper {escape(str(tenant["trial_ends_at"])[:10])}.</p>'
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
            f"""<!doctype html><meta charset=utf-8>
<title>Sporløs — mine sites</title>
<meta name=viewport content="width=device-width, initial-scale=1">
<style>body{{font:16px system-ui;max-width:640px;margin:3rem auto;padding:0 1rem;color:#222}}
table{{border-collapse:collapse;width:100%;margin:1rem 0}}
th,td{{border-bottom:1px solid #eee;padding:.6rem .4rem;text-align:left}}
th:not(:first-child),td:not(:first-child){{text-align:right;width:6rem;color:#666}}
a{{color:#3730a3;text-decoration:none}}
form.add{{display:flex;gap:.5rem;margin:1rem 0}}form.add input{{flex:1;padding:.5rem;border:1px solid #ccc;border-radius:7px}}
form.add button{{background:#1a1a1a;color:#fff;border:0;padding:0 1rem;border-radius:7px;cursor:pointer}}
.top{{display:flex;justify-content:space-between;align-items:center}}.top a{{font-size:.85rem;color:#888}}</style>
<div class=top><h1>Mine sites <small style="font-weight:400;color:#888;font-size:1rem">· i dag</small></h1>
<a href="/logout">Logg ut</a></div>
{verify_banner}
{trial}
{planinfo}
{upgrade}
<table><tr><th>Nettsted</th><th>Unike</th><th>Visn.</th></tr>
{rows or '<tr><td>ingen sites enda — legg til ett under</td><td></td><td></td></tr>'}</table>
<form class=add method=post action="/app/sites">
  <input name=domain placeholder="dittdomene.no" required>
  <button>Legg til nettsted</button>
</form>
<p style="color:#999;font-size:.8rem">Cookieløs · ingen IP lagret · samtykke-fri</p>"""
        )

    period = request.query_params.get("period", "7")
    if period not in _PERIODS:
        period = "7"
    label, days = _PERIODS[period]

    s = store.stats(site["id"], days)
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

    # CSS-trendgraf (unike besøkende per bucket)
    peak = max((b["visitors"] for b in series), default=0) or 1
    bars = "".join(
        f'<div class=bar style="height:{max(2, round(b["visitors"] / peak * 100))}%" '
        f'title="{escape(b["bucket"])}: {b["visitors"]} unike / {b["pageviews"]} visn."></div>'
        for b in series
    )

    def table(items, key):
        rows = "".join(
            f"<tr><td>{escape(str(i[key]))}</td><td>{i['n']}</td></tr>" for i in items
        )
        return f"<table>{rows or '<tr><td>ingen data enda</td></tr>'}</table>"

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
        f'<tr><td>{escape(tr["from"])} → {escape(tr["to"])}</td><td>{tr["n"]}</td></tr>'
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
    rr = "".join(
        f'<tr><td>{escape(str(r["day"])[:10])}</td><td>{r["visitors"]}</td><td>{r["pageviews"]}</td>'
        f'<td style="font-family:monospace;font-size:.72rem;color:#999">{escape((r["rollup_hash"] or "")[:12])}…</td>'
        f'<td>{"✓ forankret" if r.get("txid") else "venter"}</td></tr>'
        for r in rollups
    )
    verify_html = (
        "<h3>Verifiserbare tall</h3>"
        '<p style="color:#666;font-size:.85rem">Daglige tall forsegles med en kryptografisk hash og '
        "forankres i en uavhengig, offentlig logg — så de ikke kan endres i ettertid.</p>"
        "<table><tr><th>Dag</th><th>Unike</th><th>Visn.</th><th>Segl</th><th>Status</th></tr>"
        f"{rr or '<tr><td>ingen forseglede dager enda</td><td></td><td></td><td></td><td></td></tr>'}</table>"
    )

    return HTMLResponse(
        f"""<!doctype html><meta charset=utf-8>
<title>Sporløs — {escape(site['domain'])}</title>
<style>body{{font:16px system-ui;max-width:760px;margin:3rem auto;padding:0 1rem;color:#222}}
.tabs a{{padding:.3rem .7rem;margin-right:.3rem;border:1px solid #ddd;border-radius:6px;text-decoration:none;color:#555;font-size:.9rem}}
.tabs a.on{{background:#222;color:#fff;border-color:#222}}
.kpis{{display:flex;gap:2.5rem;margin:1.5rem 0}}.kpi b{{font-size:2.2rem;display:block;line-height:1}}.kpi span{{color:#888;font-size:.85rem}}
.chart{{display:flex;align-items:flex-end;gap:3px;height:120px;margin:1rem 0;border-bottom:1px solid #eee}}
.bar{{flex:1;background:#3b82f6;border-radius:2px 2px 0 0;min-height:2px}}.bar:hover{{background:#1d4ed8}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:0 2rem}}
table{{border-collapse:collapse;width:100%;margin:.5rem 0}}td{{border-bottom:1px solid #eee;padding:.4rem 0}}td:last-child{{text-align:right;color:#666;width:5rem}}
h3{{margin:1.5rem 0 .3rem;font-size:1rem}}</style>
<p style="margin:0 0 .5rem"><a href="/app" style="color:#3730a3;text-decoration:none;font-size:.85rem">← Mine sites</a></p>
<h1>{escape(site['domain'])}</h1>
{verify_banner}
<div class=tabs>{tabs}</div>
<details style="margin:1rem 0"><summary style="cursor:pointer;color:#3730a3;font-size:.9rem">Vis sporings-kode</summary>
<pre style="background:#f6f6f6;padding:.8rem;border-radius:7px;overflow:auto;font-size:.78rem">{escape(f'<script defer data-site="{public_id}" data-api="{PUBLIC_BASE}/api/event" src="{PUBLIC_BASE}/sporlos.js"></script>')}</pre></details>
<div class=kpis>
  <div class=kpi><b>{s['visitors']}</b><span>unike besøkende</span></div>
  <div class=kpi><b>{s['sessions']}</b><span>besøk</span></div>
  <div class=kpi><b>{s['pageviews']}</b><span>sidevisninger</span></div>
  <div class=kpi><b>{s['bounce_rate']}%</b><span>fluktfrekvens</span></div>
  <div class=kpi><b>{s['views_per_session']}</b><span>visn./besøk</span></div>
</div>
<p style="color:#888;font-size:.8rem;margin:-.5rem 0 1rem">{escape(label)}</p>
<div class=chart>{bars}</div>
<div class=grid>
  <div><h3>Topp sider</h3>{table(s['top_paths'], 'path')}</div>
  <div><h3>Topp kilder</h3>{table(s['top_sources'], 'src')}</div>
  <div><h3>Inngangssider</h3>{table(flow['entries'], 'path')}</div>
  <div><h3>Utgangssider</h3>{table(flow['exits'], 'path')}</div>
  <div><h3>Land</h3>{table(s['countries'], 'k')}</div>
  <div><h3>Fylke / region</h3>{table(s['regions'], 'k')}</div>
  <div><h3>Enheter</h3>{table(s['devices'], 'k')}</div>
  <div><h3>Nettlesere</h3>{table(s['browsers'], 'k')}</div>
  <div><h3>Operativsystem</h3>{table(s['os'], 'k')}</div>
</div>
{goals_html}
{funnels_html}
{nav_html}
{events_html}
{verify_html}
<p style="color:#999;font-size:.8rem;margin-top:2rem">Cookieløs · ingen IP lagret · samtykke-fri<br>
<span style="font-size:.75rem">Geo: <a href="https://db-ip.com" style="color:#aaa">IP Geolocation by DB-IP</a> (CC BY 4.0)</span></p>"""
    )


routes = [
    Route("/healthz", healthz),
    Route("/sporlos.js", tracker),
    Route("/api/event", ingest, methods=["POST"]),
    Route("/", landing),
    Route("/vilkar", vilkar),
    Route("/personvern", personvern),
    Route("/google-analytics-alternativ", ga_alternativ),
    Route("/signup", signup, methods=["GET", "POST"]),
    Route("/login", login, methods=["GET", "POST"]),
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
        allow_headers=["content-type"],
    ),
    Middleware(
        SessionMiddleware,
        secret_key=SESSION_SECRET,
        https_only=HTTPS_ONLY,
        same_site="lax",
    ),
]

app = Starlette(routes=routes, middleware=middleware)
