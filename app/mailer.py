"""Enkel SMTP-sender — gated på env (som Stripe/Google).

Fundament for passord-reset, trial-utløp-varsler, e-postbekreftelse, ukerapport.
Sender via Workspace SMTP (smtp.gmail.com:587 + app-passord) eller hvilken som helst
SMTP. No-op (logger) hvis ikke konfigurert, så resten av appen aldri krasjer på e-post.

To moduser:
  - SMTP-relay (IP-autentisert): sett SMTP_HOST=smtp-relay.gmail.com + MAIL_FROM. Ingen login.
  - SMTP-auth (app-passord): sett SMTP_HOST=smtp.gmail.com + SMTP_USER + SMTP_PASS.

Env: SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASS, MAIL_FROM (default = SMTP_USER).
"""

from __future__ import annotations

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

log = logging.getLogger("sporlos.mailer")


def _from() -> str | None:
    return os.environ.get("MAIL_FROM") or os.environ.get("SMTP_USER")


def configured() -> bool:
    # Nok for relay (IP-autentisert): host + en avsenderadresse. Login er valgfri.
    return bool(os.environ.get("SMTP_HOST") and _from())


def send(to: str, subject: str, body: str) -> bool:
    """Send en ren-tekst e-post. Returnerer True ved suksess, False ellers (aldri exception ut)."""
    if not configured():
        log.warning("SMTP ikke konfigurert — e-post ikke sendt (to=%s subject=%r)", to, subject)
        return False
    frm = _from()
    host = os.environ["SMTP_HOST"]
    port = int(os.environ.get("SMTP_PORT", "587"))
    user, pw = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASS")
    msg = EmailMessage()
    msg["From"] = frm
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(body)
    try:
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls(context=ssl.create_default_context())
            if user and pw:  # app-passord-modus; relay (IP-auth) hopper over login
                s.login(user, pw)
            s.send_message(msg)
        log.info("E-post sendt til %s (%r)", to, subject)
        return True
    except Exception as e:  # noqa: BLE001 — e-post skal aldri velte en request
        log.error("SMTP-send feilet: %s", e)
        return False
