"""Periodiske varsler (trial-utløp). Kjøres fra manage.py via cron.

Bruker mailer (gated) + store. Markerer kun som varslet ved vellykket sending,
så en feilet sending prøves på nytt neste kjøring.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

from app import mailer, store


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
