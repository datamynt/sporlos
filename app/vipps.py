"""Vipps MobilePay Recurring API v3 — abonnementsbetaling («Faste betalinger»).

Mode-bevisst som Stripe: VIPPS_MODE=test|live velger _TEST/_LIVE-suffiksede
env-vars (fallback til usuffikset). Aktiveres kun når alle nøkler er satt —
uten nøkler er hele modulen no-op og knappene vises ikke.

Flyt (ingen webhooks — polling + nattlig cron er nok for månedsabonnement):
  1. create_agreement(): trekning av første måned bakes inn som initialCharge,
     bruker godkjenner i Vipps-appen via vippsConfirmationUrl.
  2. /billing/vipps/retur poller get_agreement() til ACTIVE → plan settes.
  3. `manage.py vipps-charges` (cron, daglig): lager neste månedstrekk minst
     2 dager før forfall (Vipps-krav: minst 1 dag), og fanger opp avtaler
     brukeren har stoppet i Vipps-appen (STOPPED → plan cancelled).

Idempotens: Idempotency-Key + externalId = "sporlos-<tenant>-<YYYY-MM>" gjør
re-kjøringer av cron trygge — samme måned kan aldri trekkes dobbelt.
"""

from __future__ import annotations

import datetime as dt
import os
import time
import uuid

import httpx

VIPPS_MODE = os.environ.get("VIPPS_MODE", "test").lower()


def _env(base: str) -> str:
    return os.environ.get(f"{base}_{VIPPS_MODE.upper()}") or os.environ.get(base, "")


CLIENT_ID = _env("VIPPS_CLIENT_ID")
CLIENT_SECRET = _env("VIPPS_CLIENT_SECRET")
SUBSCRIPTION_KEY = _env("VIPPS_SUBSCRIPTION_KEY")
MSN = _env("VIPPS_MSN")

BASE = "https://api.vipps.no" if VIPPS_MODE == "live" else "https://apitest.vipps.no"

# Priser i øre — speiler Stripe-produktene (NOK/mnd, eks. mva legges ikke på her:
# prisene på sporlos.no er oppgitt eks. mva, Vipps trekker beløpet vi sender).
PLAN_ORE = {"liten": 9900, "vekst": 24900, "pro": 59900}
PLAN_NAMES = {"liten": "Sporløs Liten", "vekst": "Sporløs Vekst", "pro": "Sporløs Pro"}

_token_cache: dict = {"token": None, "exp": 0.0}


def configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET and SUBSCRIPTION_KEY and MSN)


def _token() -> str:
    """Access token (cache til utløp, med 60s margin)."""
    if _token_cache["token"] and time.time() < _token_cache["exp"] - 60:
        return _token_cache["token"]
    r = httpx.post(
        f"{BASE}/accesstoken/get",
        headers={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "Ocp-Apim-Subscription-Key": SUBSCRIPTION_KEY,
        },
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["exp"] = time.time() + int(data.get("expires_in", 3600))
    return _token_cache["token"]


def _headers(idempotency_key: str | None = None) -> dict:
    h = {
        "Authorization": f"Bearer {_token()}",
        "Ocp-Apim-Subscription-Key": SUBSCRIPTION_KEY,
        "Merchant-Serial-Number": MSN,
        "Content-Type": "application/json",
        "Vipps-System-Name": "sporlos",
        "Vipps-System-Version": "1.0",
    }
    if idempotency_key:
        h["Idempotency-Key"] = idempotency_key
    return h


def create_agreement(plan: str, base_url: str) -> dict:
    """Opprett avtaleutkast m/ første måned som initialCharge.

    Returnerer {"agreementId": ..., "vippsConfirmationUrl": ...}.
    """
    amount = PLAN_ORE[plan]
    name = PLAN_NAMES[plan]
    body = {
        "interval": {"unit": "MONTH", "count": 1},
        "pricing": {"type": "LEGACY", "amount": amount, "currency": "NOK"},
        "productName": name,
        "productDescription": "Webanalyse uten cookies — sporlos.no",
        "merchantRedirectUrl": f"{base_url}/billing/vipps/retur",
        "merchantAgreementUrl": f"{base_url}/app",
        "initialCharge": {
            "amount": amount,
            "description": f"{name} — første måned",
            "transactionType": "DIRECT_CAPTURE",
        },
    }
    r = httpx.post(
        f"{BASE}/recurring/v3/agreements",
        json=body,
        headers=_headers(idempotency_key=str(uuid.uuid4())),
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def get_agreement(agreement_id: str) -> dict:
    r = httpx.get(
        f"{BASE}/recurring/v3/agreements/{agreement_id}", headers=_headers(), timeout=20
    )
    r.raise_for_status()
    return r.json()


def stop_agreement(agreement_id: str) -> None:
    r = httpx.patch(
        f"{BASE}/recurring/v3/agreements/{agreement_id}",
        json={"status": "STOPPED"},
        headers=_headers(idempotency_key=str(uuid.uuid4())),
        timeout=20,
    )
    r.raise_for_status()


def create_charge(tenant_id: int, agreement_id: str, plan: str, due: dt.date) -> str:
    """Månedstrekk på aktiv avtale. Idempotent per tenant+måned."""
    ext = f"sporlos-{tenant_id}-{due:%Y-%m}"
    body = {
        "amount": PLAN_ORE[plan],
        "transactionType": "DIRECT_CAPTURE",
        "type": "RECURRING",
        "description": f"{PLAN_NAMES[plan]} — {due:%B %Y}"[:45],
        "due": due.isoformat(),
        "retryDays": 7,
        "externalId": ext,
    }
    r = httpx.post(
        f"{BASE}/recurring/v3/agreements/{agreement_id}/charges",
        json=body,
        headers=_headers(idempotency_key=ext),
        timeout=20,
    )
    r.raise_for_status()
    return r.json().get("chargeId", "")


def next_month(d: dt.date) -> dt.date:
    """Neste forfallsdato: +1 måned, dag klemt til 28 (unngår 29/30/31-trøbbel)."""
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return dt.date(y, m, min(d.day, 28))


def sweep() -> str:
    """Nattlig avstemming + månedstrekk (cron). Trygg å kjøre flere ganger.

    - pending-avtaler: aktiver hvis bruker godkjente uten å komme tilbake,
      rydd hvis avtalen utløp uten godkjenning (plan røres ikke).
    - STOPPED (bruker stoppet i Vipps-appen, eller vi stoppet): planen løper
      ut betalt periode, deretter cancelled.
    - ACTIVE: opprett neste månedstrekk senest 2 dager før forfall
      (Vipps-krav: minst 1 dag før).
    """
    from app import store  # her for å holde API-klienten import-lett

    if not configured():
        return "vipps ikke konfigurert — hopper over"
    today = dt.date.today()
    charged = activated = stopped = 0
    for t in store.vipps_tenants():
        agid = t["vipps_agreement_id"]
        try:
            status = get_agreement(agid).get("status", "")
        except Exception as e:
            print(f"  tenant {t['id']}: klarte ikke hente avtale {agid}: {e}")
            continue
        if t["vipps_pending_plan"]:
            if status == "ACTIVE":
                store.activate_vipps(
                    t["id"], t["vipps_pending_plan"], next_month(today).isoformat()
                )
                activated += 1
            elif status in ("STOPPED", "EXPIRED"):
                store.clear_vipps(t["id"])  # aldri aktivert — plan røres ikke
            continue
        if status == "STOPPED":
            due = str(t["vipps_charged_through"] or "")[:10]
            if not due or due <= today.isoformat():
                store.clear_vipps(t["id"], new_plan="cancelled")
                stopped += 1
            continue
        if status != "ACTIVE" or t["plan"] not in PLAN_ORE or not t["vipps_charged_through"]:
            continue
        due = dt.date.fromisoformat(str(t["vipps_charged_through"])[:10])
        if (due - today).days <= 2:
            if due <= today:  # forfall rukket å passere (nedetid e.l.) → skyv frem
                due = today + dt.timedelta(days=2)
            create_charge(t["id"], agid, t["plan"], due)
            store.set_vipps_charged_through(t["id"], next_month(due).isoformat())
            charged += 1
    return f"vipps-sweep: {charged} trekk opprettet, {activated} aktivert, {stopped} avsluttet"
