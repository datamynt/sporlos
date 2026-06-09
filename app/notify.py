"""Periodiske varsler (trial-utløp). Kjøres fra manage.py via cron.

Bruker mailer (gated) + store. Markerer kun som varslet ved vellykket sending,
så en feilet sending prøves på nytt neste kjøring.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from app import auth, mailer, store


def _base() -> str:
    d = os.environ.get("SPORLOS_DOMAIN")
    return f"https://{d}" if d and "FYLL" not in d else "https://sporlos.no"


def send_trial_reminders(within_days: int = 3) -> int:
    """Send påminnelse til trial-tenants som utløper snart. Returnerer antall sendt."""
    sent = 0
    for r in store.trial_ending_tenants(within_days):
        email = r.get("email")
        if not email:
            continue
        try:
            ends = datetime.strptime(str(r["trial_ends_at"])[:19], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone.utc
            )
            days = max(0, (ends - datetime.now(timezone.utc)).days)
        except Exception:
            days = within_days
        naar = "i dag" if days == 0 else ("i morgen" if days == 1 else f"om {days} dager")
        body = (
            f"Hei,\n\nProveperioden din pa Sporlos utloper {naar}.\n\n"
            f"Velg en plan sa analysen fortsetter uten avbrudd:\n{_base()}/app\n\n"
            "Sporsmal eller trenger litt mer tid? Bare svar pa denne e-posten.\n\nSporlos"
        )
        if mailer.send(email, "Proveperioden din pa Sporlos utloper snart", body):
            store.mark_trial_reminded(r["id"])
            sent += 1
    return sent


def send_weekly_reports(days: int = 7) -> int:
    """Ukentlig sammendrag (pv + unike per site) til hver tenant med trafikk. Antall sendt."""
    sent = 0
    for t in store.weekly_report_data(days):
        lines = [
            f"- {s['domain']}: {s['pv']} sidevisninger, {s['uv']} unike besokende"
            for s in t["sites"]
            if s["pv"] > 0
        ]
        if not lines:
            continue
        tid = str(t["tenant_id"])
        unsub = f"{_base()}/unsubscribe?tid={tid}&t={auth.sign_token('unsub', tid)}"
        body = (
            "Hei,\n\nDin siste uke pa Sporlos:\n\n"
            + "\n".join(lines)
            + f"\n\nSe full statistikk: {_base()}/app\n\n"
            f"Vil du ikke ha ukerapport? Meld av her:\n{unsub}\n\nSporlos"
        )
        if mailer.send(t["email"], "Din uke pa Sporlos", body):
            sent += 1
    return sent
