"""Periodiske varsler (trial-utløp). Kjøres fra manage.py via cron.

Bruker mailer (gated) + store. Markerer kun som varslet ved vellykket sending,
så en feilet sending prøves på nytt neste kjøring.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from app import auth, mailer, store
from app import export as data_export


def _base() -> str:
    d = os.environ.get("SPORLOS_DOMAIN")
    return f"https://{d}" if d and "FYLL" not in d else "https://sporlos.no"


def send_verification(uid: int, email: str) -> bool:
    """Send e-postbekreftelse (signert lenke, ingen DB-token)."""
    link = f"{_base()}/verify?uid={uid}&t={auth.sign_token('verify', str(uid))}"
    return mailer.send(
        email,
        "Bekreft e-posten din - Sporløs",
        f"Hei,\n\nBekreft e-postadressen din for Sporløs:\n{link}\n\n"
        "Opprettet du ikke en konto? Se bort fra denne e-posten.\n\nSporløs",
    )


def send_trial_reminders(within_days: int = 3) -> tuple[int, int]:
    """Send påminnelse til trial-tenants som utløper snart. Returns (sent, failed)."""
    sent = failed = 0
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
            f"Hei,\n\nPrøveperioden din på Sporløs går ut {naar}.\n\n"
            f"Velg en plan, så fortsetter målingen uten avbrudd:\n{_base()}/app\n\n"
            "Spørsmål, eller trenger du litt mer tid? Bare svar på denne e-posten.\n\nSporløs"
        )
        if mailer.send(email, "Prøveperioden din på Sporløs går snart ut", body):
            store.mark_trial_reminded(r["id"])
            sent += 1
        else:
            failed += 1
    return sent, failed


_MONTHS = ["januar", "februar", "mars", "april", "mai", "juni", "juli", "august", "september",
           "oktober", "november", "desember"]


def _no_long_date(dt: datetime) -> str:
    return f"{dt.day}. {_MONTHS[dt.month - 1]} {dt.year}"


def deletion_warning_mail(c: dict, now: datetime) -> tuple[str, str]:
    """(subject, body) of the mail that goes out store.DELETION_WARN_DAYS before an ended
    account becomes deletable. `c` is one row of store.deletion_warning_candidates()."""
    when = _no_long_date(c["delete_on"])
    ended = _no_long_date(c["ended_at"])
    why = (f"prøveperioden gikk ut {ended}" if c["category"] == "trial_expired"
           else f"abonnementet ble avsluttet {ended}")
    params = data_export.sign_link(c["id"], c["delete_on"] + timedelta(days=1))
    link = f"{_base()}/eksport?" + "&".join(f"{k}={v}" for k, v in params.items())
    body = (
        "Hei,\n\n"
        f"Kontoen «{c['name']}» på Sporløs er ikke lenger aktiv ({why}). Derfor blir kontoen og "
        f"alle dataene i den slettet {when}.\n\n"
        "Dette blir slettet:\n"
        "- alle nettstedene dine, med statistikk, dagstall, mål, trakter og søkedata\n"
        "- API-nøklene og alle brukerne på kontoen\n"
        "Sikkerhetskopiene rulleres ut innen 35 dager etter det.\n\n"
        "Vil du ta vare på dataene dine?\n"
        "Last ned alt som en ZIP med regnearkfiler (CSV), også de rå hendelsene som fortsatt er "
        f"lagret:\n{link}\n"
        "Lenken virker til kontoen slettes. Du kan også logge inn på "
        f"{_base()}/login og trykke «Last ned alle data» på kontosiden.\n\n"
        "Vil du beholde kontoen?\n"
        f"Velg en plan før {when}, så slettes ingenting og målingen fortsetter:\n{_base()}/app\n\n"
        "Dette er den eneste e-posten vi sender om slettingen. Spørsmål? Bare svar på denne "
        "e-posten.\n\nSporløs"
    )
    return f"Kontoen din på Sporløs slettes {when}. Last ned dataene dine nå", body


def send_deletion_warnings(enforce: bool = False, now: datetime | None = None,
                           limit: int = store.RETENTION_BATCH) -> dict:
    """Warn the owner of every ended account that is within store.DELETION_WARN_DAYS of
    deletion (or past it, if the job is new), once per ending. Runs in `manage
    purge-dormant`, before the purge. A dry run (enforce False) sends and marks nothing and
    only counts. The marker is set only when the mail was accepted, so a failed send is tried
    again the next day, and the purge holds the account until the warning is out.

    Not tied to email_optout: this is a notice about the customer's own data, not a
    newsletter. Returns counts only."""
    now = now or datetime.now(timezone.utc)
    due = store.deletion_warning_candidates(now)
    res = {"enforce": enforce, "due": len(due), "sent": 0, "failed": 0}
    if not enforce:
        return res
    for c in due[:limit]:
        subject, body = deletion_warning_mail(c, now)
        if mailer.send(c["email"], subject, body):
            store.mark_deletion_warned(c["id"], now)
            res["sent"] += 1
        else:
            res["failed"] += 1
    return res


def send_stalled_alerts() -> int:
    """Varsle drifts-admin når en site som HADDE trafikk plutselig er stille — det
    eneste push-signalet på at sporing har sluttet å virke (CMS-bytte, CSP, DB-feil).
    Sendes til SPORLOS_ADMIN_EMAIL (fallback MAIL_FROM). Returnerer 1 hvis sendt, ellers 0."""
    stalled = store.stalled_sites()
    if not stalled:
        return 0
    admin = os.environ.get("SPORLOS_ADMIN_EMAIL") or os.environ.get("MAIL_FROM")
    if not admin:
        return 0
    lines = "\n".join(
        f"  - {s['domain']} (siste hendelse: {str(s['last_ts'])[:16]})" for s in stalled
    )
    body = (
        "Hei,\n\nFolgende nettsteder hadde trafikk nylig, men har ikke registrert "
        "noe det siste dognet - sporingen kan ha sluttet a virke (CMS-bytte, CSP, e.l.):\n\n"
        f"{lines}\n\nSjekk at sporingskoden fortsatt ligger på sidene.\n\nSporlos"
    )
    ok = mailer.send(admin, f"Sporlos: {len(stalled)} nettsted uten trafikk siste dogn", body)
    return 1 if ok else 0


def send_overage_alerts() -> tuple[int, int]:
    """Vennlig varsel når en tenant passerer planens månedlige visningsgrense.
    Maks én e-post per kalendermåned (overage_notified_month). Data kastes aldri
    — dette er informasjon + oppgraderings-nudge, ingen avstenging.
    Returns (sent, failed)."""
    sent = failed = 0
    for t in store.overage_candidates():
        pv_lim, _ = store.plan_limits(t.get("plan"))
        if not pv_lim or not t.get("email") or t.get("plan") == "cancelled":
            continue
        usage = store.monthly_usage(t["id"])
        if usage["pageviews"] <= pv_lim:
            continue
        tid = str(t["id"])
        unsub = f"{_base()}/unsubscribe?tid={tid}&t={auth.sign_token('unsub', tid)}"
        fmt = lambda n: f"{n:,}".replace(",", " ")  # noqa: E731 — tusenskille med mellomrom
        body = (
            "Hei,\n\nGratulerer, nettstedene dine vokser! Du har passert planens "
            f"{fmt(pv_lim)} visninger denne måneden ({fmt(usage['pageviews'])} så langt).\n\n"
            "Alt måles fortsatt som før, og vi kaster aldri data. Men vurder gjerne "
            f"å oppgradere, så planen passer trafikken:\n{_base()}/app\n\n"
            f"Vil du ikke ha slike varsler? Meld deg av her:\n{unsub}\n\nSporløs"
        )
        if mailer.send(t["email"], "Nettstedene dine vokser – du har passert planens visninger", body):
            store.mark_overage_notified(t["id"])
            sent += 1
        else:
            failed += 1
    return sent, failed


def send_weekly_reports(days: int = 7) -> int:
    """Ukentlig sammendrag (pv + unike per site) til hver tenant med trafikk. Antall sendt."""
    sent = 0
    for t in store.weekly_report_data(days):
        lines = [
            f"- {s['domain']}: {s['pv']} sidevisninger, {s['uv']} unike besøkende (summert per dag)"
            for s in t["sites"]
            if s["pv"] > 0
        ]
        if not lines:
            continue
        tid = str(t["tenant_id"])
        unsub = f"{_base()}/unsubscribe?tid={tid}&t={auth.sign_token('unsub', tid)}"
        body = (
            "Hei,\n\nDin siste uke på Sporløs:\n\n"
            + "\n".join(lines)
            + f"\n\nSe full statistikk: {_base()}/app\n\n"
            f"Vil du ikke ha ukerapport? Meld av her:\n{unsub}\n\nSporløs"
        )
        if mailer.send(t["email"], "Din uke på Sporløs", body):
            sent += 1
    return sent
