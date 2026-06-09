"""Lagringslag — Postgres (prod/lokal Docker) + SQLite (null-avhengighets-fallback).

Backend velges av DATABASE_URL:
  - satt  → Postgres/Cloud SQL via psycopg2 (db/schema.sql er kilden til sannhet)
  - tom   → SQLite på SPORLOS_DB (default ./sporlos.db) for rask lokal kjøring

Lokal Postgres som matcher prod:
    docker compose up -d
    export DATABASE_URL=postgresql://sporlos:sporlos@localhost:5432/sporlos
    .venv/bin/python3 -m app.manage init  < /dev/null
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
from datetime import datetime, timedelta, timezone
from pathlib import Path

_DSN = os.environ.get("DATABASE_URL")
_DB_PATH = os.environ.get("SPORLOS_DB", "sporlos.db")
_USE_PG = bool(_DSN)

# Parameter-plassholder: psycopg2 bruker %s, sqlite3 bruker ?.
P = "%s" if _USE_PG else "?"

if _USE_PG:
    import psycopg2
    import psycopg2.extras
else:
    import sqlite3

# SQLite-speil av db/schema.sql (samme form, SQLite-dialekt).
_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    plan TEXT NOT NULL DEFAULT 'trial',
    trial_ends_at TEXT,
    trial_reminded_at TEXT,
    email_optout INTEGER NOT NULL DEFAULT 0,
    stripe_customer_id TEXT,
    stripe_subscription_id TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS sites (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id INTEGER NOT NULL REFERENCES tenants(id),
    domain TEXT NOT NULL,
    public_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (tenant_id, domain)
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id INTEGER NOT NULL REFERENCES sites(id),
    ts TEXT NOT NULL DEFAULT (datetime('now')),
    name TEXT NOT NULL DEFAULT 'pageview',
    path TEXT NOT NULL,
    referrer_src TEXT,
    country TEXT,
    region TEXT,
    device TEXT,
    browser TEXT,
    os TEXT,
    visitor_hash TEXT NOT NULL,
    session_id TEXT
);
CREATE INDEX IF NOT EXISTS events_site_ts ON events (site_id, ts);
CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id INTEGER NOT NULL REFERENCES sites(id),
    name TEXT NOT NULL,
    match_type TEXT NOT NULL,
    match_value TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS funnels (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id INTEGER NOT NULL REFERENCES sites(id),
    name TEXT NOT NULL,
    steps TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS reset_tokens (
    token TEXT PRIMARY KEY,
    email TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_rollups (
    site_id INTEGER NOT NULL,
    day TEXT NOT NULL,
    pageviews INTEGER NOT NULL DEFAULT 0,
    visitors INTEGER NOT NULL DEFAULT 0,
    sessions INTEGER NOT NULL DEFAULT 0,
    bounce_rate REAL,
    rollup_hash TEXT,
    merkle_root TEXT,
    merkle_proof TEXT,
    txid TEXT,
    anchored_at TEXT,
    PRIMARY KEY (site_id, day)
);
"""


@contextlib.contextmanager
def _cursor():
    """Uniform markør for begge backends. Committer ved exit, ruller tilbake ved feil.

    Gir dict-aktige rader (dict(row) virker for både sqlite3.Row og RealDictRow).
    """
    if _USE_PG:
        conn = psycopg2.connect(_DSN)
        try:
            with conn:
                # Naive UTC-strenger tolkes som UTC => dag-grenser blir riktige.
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute("SET TIME ZONE 'UTC'")
                    yield cur
        finally:
            conn.close()
    else:
        conn = sqlite3.connect(_DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn.cursor()
        finally:
            conn.close()


def init_db() -> None:
    if _USE_PG:
        schema = (Path(__file__).resolve().parent.parent / "db" / "schema.sql").read_text()
        with _cursor() as cur:
            cur.execute(schema)
            # Idempotent migrering for eksisterende DB-er
            cur.execute("ALTER TABLE events ADD COLUMN IF NOT EXISTS region TEXT")
            cur.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS plan TEXT NOT NULL DEFAULT 'trial'")
            cur.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS trial_ends_at TIMESTAMPTZ")
            cur.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS trial_reminded_at TIMESTAMPTZ")
            cur.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS email_optout INTEGER NOT NULL DEFAULT 0")
            cur.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT")
            cur.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT")
            cur.execute("ALTER TABLE daily_rollups ADD COLUMN IF NOT EXISTS merkle_root TEXT")
            cur.execute("ALTER TABLE daily_rollups ADD COLUMN IF NOT EXISTS merkle_proof TEXT")
            cur.execute("ALTER TABLE daily_rollups ADD COLUMN IF NOT EXISTS txid TEXT")
            cur.execute("ALTER TABLE daily_rollups ADD COLUMN IF NOT EXISTS anchored_at TIMESTAMPTZ")
    else:
        with _cursor() as cur:
            cur.executescript(_SQLITE_SCHEMA)
            for ddl in (  # SQLite mangler ADD COLUMN IF NOT EXISTS
                "ALTER TABLE events ADD COLUMN region TEXT",
                "ALTER TABLE tenants ADD COLUMN plan TEXT NOT NULL DEFAULT 'trial'",
                "ALTER TABLE tenants ADD COLUMN trial_ends_at TEXT",
                "ALTER TABLE tenants ADD COLUMN trial_reminded_at TEXT",
                "ALTER TABLE tenants ADD COLUMN email_optout INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE tenants ADD COLUMN stripe_customer_id TEXT",
                "ALTER TABLE tenants ADD COLUMN stripe_subscription_id TEXT",
            ):
                try:
                    cur.execute(ddl)
                except Exception:
                    pass


def create_tenant(name: str) -> int:
    with _cursor() as cur:
        if _USE_PG:
            cur.execute(f"INSERT INTO tenants (name) VALUES ({P}) RETURNING id", (name,))
            return cur.fetchone()["id"]
        cur.execute(f"INSERT INTO tenants (name) VALUES ({P})", (name,))
        return cur.lastrowid


def create_site(tenant_id: int, domain: str) -> dict:
    public_id = secrets.token_urlsafe(9)
    with _cursor() as cur:
        sql = f"INSERT INTO sites (tenant_id, domain, public_id) VALUES ({P}, {P}, {P})"
        if _USE_PG:
            cur.execute(sql + " RETURNING id", (tenant_id, domain, public_id))
            site_id = cur.fetchone()["id"]
        else:
            cur.execute(sql, (tenant_id, domain, public_id))
            site_id = cur.lastrowid
    return {"id": site_id, "domain": domain, "public_id": public_id}


def list_sites(tenant_id: int) -> list[dict]:
    """Sites for ÉN tenant m/ dagens pv + unike — driver konto-oversikten (isolert)."""
    now = datetime.now(timezone.utc)
    day_start = now.strftime("%Y-%m-%d 00:00:00")
    day_end = (now + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
    with _cursor() as cur:
        cur.execute(
            f"""SELECT s.public_id, s.domain, t.name AS tenant,
                       COUNT(e.id) AS pv,
                       COUNT(DISTINCT e.visitor_hash) AS visitors
                FROM sites s
                JOIN tenants t ON t.id = s.tenant_id
                LEFT JOIN events e
                       ON e.site_id = s.id AND e.ts >= {P} AND e.ts < {P}
                WHERE s.tenant_id = {P}
                GROUP BY s.id, s.public_id, s.domain, t.name
                ORDER BY pv DESC, s.domain""",
            (day_start, day_end, tenant_id),
        )
        return [dict(r) for r in cur.fetchall()]


def create_account(company: str, email: str, password_hash: str, trial_days: int = 30) -> tuple[int, int]:
    """Opprett tenant (trial) + første bruker. (tenant_id, user_id). Reiser ved dup epost."""
    trial_ends = (datetime.now(timezone.utc) + timedelta(days=trial_days)).strftime(
        "%Y-%m-%d %H:%M:%S"
    )
    with _cursor() as cur:
        if _USE_PG:
            cur.execute(
                f"INSERT INTO tenants (name, plan, trial_ends_at) VALUES ({P}, {P}, {P}) RETURNING id",
                (company, "trial", trial_ends),
            )
            tid = cur.fetchone()["id"]
            cur.execute(
                f"INSERT INTO users (tenant_id, email, password_hash) VALUES ({P}, {P}, {P}) RETURNING id",
                (tid, email.strip().lower(), password_hash),
            )
            uid = cur.fetchone()["id"]
        else:
            cur.execute(
                f"INSERT INTO tenants (name, plan, trial_ends_at) VALUES ({P}, {P}, {P})",
                (company, "trial", trial_ends),
            )
            tid = cur.lastrowid
            cur.execute(
                f"INSERT INTO users (tenant_id, email, password_hash) VALUES ({P}, {P}, {P})",
                (tid, email.strip().lower(), password_hash),
            )
            uid = cur.lastrowid
    return tid, uid


def get_user_by_email(email: str) -> dict | None:
    with _cursor() as cur:
        cur.execute(
            f"SELECT id, tenant_id, email, password_hash FROM users WHERE email = {P}",
            (email.strip().lower(),),
        )
        r = cur.fetchone()
        return dict(r) if r else None


def set_password(email: str, password_hash: str) -> None:
    with _cursor() as cur:
        cur.execute(
            f"UPDATE users SET password_hash = {P} WHERE email = {P}",
            (password_hash, email.strip().lower()),
        )


def create_reset_token(email: str) -> str:
    token = secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    with _cursor() as cur:
        cur.execute(
            f"INSERT INTO reset_tokens (token, email, expires_at) VALUES ({P}, {P}, {P})",
            (token, email.strip().lower(), expires),
        )
    return token


def pop_reset_token(token: str) -> str | None:
    """Returner e-post hvis token er gyldig + ikke utløpt, og forbruk den (engangs)."""
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _cursor() as cur:
        cur.execute(
            f"SELECT email FROM reset_tokens WHERE token = {P} AND expires_at > {P}", (token, now)
        )
        r = cur.fetchone()
        cur.execute(f"DELETE FROM reset_tokens WHERE token = {P}", (token,))
        return r["email"] if r else None


def trial_ending_tenants(within_days: int = 3) -> list[dict]:
    """Trial-tenants som utløper innen `within_days` og ikke er varslet. Med én bruker-epost."""
    now = datetime.now(timezone.utc)
    now_str = now.strftime("%Y-%m-%d %H:%M:%S")
    cutoff = (now + timedelta(days=within_days)).strftime("%Y-%m-%d %H:%M:%S")
    with _cursor() as cur:
        cur.execute(
            f"SELECT t.id, t.trial_ends_at, "
            f"(SELECT email FROM users WHERE tenant_id = t.id ORDER BY id LIMIT 1) AS email "
            f"FROM tenants t WHERE t.plan = 'trial' AND t.trial_ends_at IS NOT NULL "
            f"AND t.trial_ends_at > {P} AND t.trial_ends_at <= {P} "
            f"AND t.trial_reminded_at IS NULL",
            (now_str, cutoff),
        )
        return [dict(r) for r in cur.fetchall()]


def mark_trial_reminded(tenant_id: int) -> None:
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    with _cursor() as cur:
        cur.execute(
            f"UPDATE tenants SET trial_reminded_at = {P} WHERE id = {P}", (now_str, tenant_id)
        )


def set_email_optout(tenant_id: int, optout: bool = True) -> None:
    with _cursor() as cur:
        cur.execute(
            f"UPDATE tenants SET email_optout = {P} WHERE id = {P}",
            (1 if optout else 0, tenant_id),
        )


def weekly_report_data(days: int = 7) -> list[dict]:
    """Per tenant m/ epost: pv + unike per site siste `days`. Kun tenants med trafikk."""
    start, end = _period_window(days)
    with _cursor() as cur:
        cur.execute(
            "SELECT t.id, "
            "(SELECT email FROM users WHERE tenant_id = t.id ORDER BY id LIMIT 1) AS email "
            "FROM tenants t WHERE COALESCE(t.email_optout, 0) = 0"
        )
        tenants = [dict(r) for r in cur.fetchall()]
        out = []
        for t in tenants:
            if not t["email"]:
                continue
            cur.execute(f"SELECT id, domain FROM sites WHERE tenant_id = {P}", (t["id"],))
            sites = [dict(r) for r in cur.fetchall()]
            site_stats, total = [], 0
            for s in sites:
                cur.execute(
                    f"SELECT COUNT(*) AS pv, COUNT(DISTINCT visitor_hash) AS uv FROM events "
                    f"WHERE site_id = {P} AND ts >= {P} AND ts < {P} AND name = 'pageview'",
                    (s["id"], start, end),
                )
                r = cur.fetchone()
                pv, uv = r["pv"], r["uv"]
                total += pv
                site_stats.append({"domain": s["domain"], "pv": pv, "uv": uv})
            if total > 0:
                out.append({"tenant_id": t["id"], "email": t["email"], "sites": site_stats})
    return out


def get_tenant(tenant_id: int) -> dict | None:
    with _cursor() as cur:
        cur.execute(
            f"SELECT id, name, plan, trial_ends_at, stripe_customer_id, stripe_subscription_id "
            f"FROM tenants WHERE id = {P}",
            (tenant_id,),
        )
        r = cur.fetchone()
        return dict(r) if r else None


def set_tenant_plan(
    tenant_id: int, plan: str, customer_id: str | None = None, sub_id: str | None = None
) -> None:
    """Sett plan (og ev. Stripe-id-er) på en tenant — kalles fra webhook/checkout."""
    sets, args = [f"plan = {P}"], [plan]
    if customer_id is not None:
        sets.append(f"stripe_customer_id = {P}")
        args.append(customer_id)
    if sub_id is not None:
        sets.append(f"stripe_subscription_id = {P}")
        args.append(sub_id)
    args.append(tenant_id)
    with _cursor() as cur:
        cur.execute(f"UPDATE tenants SET {', '.join(sets)} WHERE id = {P}", tuple(args))


def get_tenant_by_customer(customer_id: str) -> dict | None:
    with _cursor() as cur:
        cur.execute(f"SELECT id FROM tenants WHERE stripe_customer_id = {P}", (customer_id,))
        r = cur.fetchone()
        return dict(r) if r else None


def reassign_site(public_id: str, tenant_id: int) -> None:
    """Flytt en site til en annen tenant (brukt for migrering av Thomas' sites)."""
    with _cursor() as cur:
        cur.execute(
            f"UPDATE sites SET tenant_id = {P} WHERE public_id = {P}", (tenant_id, public_id)
        )


def top_events(site_id: int, days: int = 7) -> list[dict]:
    """Egendefinerte hendelser (ikke pageview) — antall + unike, for å se hva som fyrer."""
    start, end = _period_window(days)
    with _cursor() as cur:
        cur.execute(
            f"SELECT name AS k, COUNT(*) AS n, COUNT(DISTINCT visitor_hash) AS u "
            f"FROM events WHERE site_id = {P} AND ts >= {P} AND ts < {P} AND name <> 'pageview' "
            "GROUP BY name ORDER BY n DESC LIMIT 10",
            (site_id, start, end),
        )
        return [dict(r) for r in cur.fetchall()]


def create_goal(site_id: int, name: str, match_type: str, match_value: str) -> None:
    with _cursor() as cur:
        cur.execute(
            f"INSERT INTO goals (site_id, name, match_type, match_value) "
            f"VALUES ({P}, {P}, {P}, {P})",
            (site_id, name, match_type, match_value),
        )


def delete_goal(goal_id: int, site_id: int) -> None:
    with _cursor() as cur:
        cur.execute(
            f"DELETE FROM goals WHERE id = {P} AND site_id = {P}", (goal_id, site_id)
        )


def goal_stats(site_id: int, days: int = 7) -> list[dict]:
    """Hvert mål m/ unike fullføringer + konverteringsrate (av unike besøkende)."""
    start, end = _period_window(days)
    base = f"site_id = {P} AND ts >= {P} AND ts < {P}"
    with _cursor() as cur:
        cur.execute(
            f"SELECT COUNT(DISTINCT visitor_hash) AS n FROM events "
            f"WHERE {base} AND name = 'pageview'",
            (site_id, start, end),
        )
        total = cur.fetchone()["n"] or 0
        cur.execute(
            f"SELECT id, name, match_type, match_value FROM goals WHERE site_id = {P} ORDER BY id",
            (site_id,),
        )
        goals = [dict(r) for r in cur.fetchall()]
        out = []
        for g in goals:
            if g["match_type"] == "event":
                cond, mv = f"name = {P}", g["match_value"]
            else:
                cond, mv = f"name = 'pageview' AND path = {P}", g["match_value"]
            cur.execute(
                f"SELECT COUNT(DISTINCT visitor_hash) AS n FROM events WHERE {base} AND {cond}",
                (site_id, start, end, mv),
            )
            comp = cur.fetchone()["n"] or 0
            out.append({**g, "completions": comp, "rate": round(comp / total * 100, 1) if total else 0.0})
    return out


def flow_stats(site_id: int, days: int = 7) -> dict:
    """Inngangs- og utgangssider (aggregat fra økter). Første side i en økt = inngang,
    siste = utgang. 30-min gap definerer økt (samme som sesjonisering). Ingen ny data."""
    start, end = _period_window(days)
    with _cursor() as cur:
        cur.execute(
            f"SELECT visitor_hash, ts, path FROM events WHERE site_id = {P} AND ts >= {P} "
            f"AND ts < {P} AND name = 'pageview' ORDER BY visitor_hash, ts",
            (site_id, start, end),
        )
        rows = cur.fetchall()
    entries, exits = {}, {}
    prev_v, prev_e, last_path = None, 0.0, None
    for r in rows:
        v, e, p = r["visitor_hash"], _epoch(r["ts"]), r["path"]
        if v != prev_v or (e - prev_e) > _SESSION_GAP:
            if last_path is not None:
                exits[last_path] = exits.get(last_path, 0) + 1
            entries[p] = entries.get(p, 0) + 1
        last_path = p
        prev_v, prev_e = v, e
    if last_path is not None:
        exits[last_path] = exits.get(last_path, 0) + 1

    def top(d):
        return [
            {"path": k, "n": n}
            for k, n in sorted(d.items(), key=lambda x: -x[1])[:10]
        ]

    return {"entries": top(entries), "exits": top(exits)}


def path_transitions(site_id: int, days: int = 7, limit: int = 12) -> list[dict]:
    """Vanligste side→side-overganger innen økter (navigasjonsstier). Hopper over
    selv-overganger (samme side på rad, f.eks. refresh). Aggregat, ingen ny data."""
    start, end = _period_window(days)
    with _cursor() as cur:
        cur.execute(
            f"SELECT visitor_hash, ts, path FROM events WHERE site_id = {P} AND ts >= {P} "
            f"AND ts < {P} AND name = 'pageview' ORDER BY visitor_hash, ts",
            (site_id, start, end),
        )
        rows = cur.fetchall()
    trans = {}
    prev_v, prev_e, prev_p = None, 0.0, None
    for r in rows:
        v, e, p = r["visitor_hash"], _epoch(r["ts"]), r["path"]
        same_session = v == prev_v and (e - prev_e) <= _SESSION_GAP
        if same_session and prev_p is not None and prev_p != p:
            key = (prev_p, p)
            trans[key] = trans.get(key, 0) + 1
        prev_v, prev_e, prev_p = v, e, p
    return [
        {"from": k[0], "to": k[1], "n": n}
        for k, n in sorted(trans.items(), key=lambda x: -x[1])[:limit]
    ]


def create_funnel(site_id: int, name: str, steps: list[dict]) -> None:
    with _cursor() as cur:
        cur.execute(
            f"INSERT INTO funnels (site_id, name, steps) VALUES ({P}, {P}, {P})",
            (site_id, name, json.dumps(steps)),
        )


def delete_funnel(funnel_id: int, site_id: int) -> None:
    with _cursor() as cur:
        cur.execute(
            f"DELETE FROM funnels WHERE id = {P} AND site_id = {P}", (funnel_id, site_id)
        )


def funnel_stats(site_id: int, days: int = 7) -> list[dict]:
    """For hver funnel: antall unike besøkende som nådde hvert steg (i rekkefølge) + rate.
    Per besøkende (daglig-hash), steg matches sekvensielt i tidsorden."""
    start, end = _period_window(days)
    with _cursor() as cur:
        cur.execute(
            f"SELECT id, name, steps FROM funnels WHERE site_id = {P} ORDER BY id", (site_id,)
        )
        funnels = [dict(r) for r in cur.fetchall()]
        if not funnels:
            return []
        cur.execute(
            f"SELECT visitor_hash, name, path FROM events WHERE site_id = {P} AND ts >= {P} "
            f"AND ts < {P} ORDER BY visitor_hash, ts",
            (site_id, start, end),
        )
        rows = cur.fetchall()
    visitors = {}
    for r in rows:
        visitors.setdefault(r["visitor_hash"], []).append(r)

    def matches(ev, st):
        if st["type"] == "event":
            return ev["name"] == st["value"]
        return ev["name"] == "pageview" and ev["path"] == st["value"]

    out = []
    for f in funnels:
        steps = json.loads(f["steps"])
        counts = [0] * len(steps)
        for evs in visitors.values():
            idx = 0
            for ev in evs:
                if idx >= len(steps):
                    break
                if matches(ev, steps[idx]):
                    idx += 1
            for i in range(idx):
                counts[i] += 1
        base = counts[0] if counts else 0
        step_out = [
            {
                "value": st["value"],
                "type": st["type"],
                "count": counts[i],
                "rate": round(counts[i] / base * 100) if base else 0,
            }
            for i, st in enumerate(steps)
        ]
        out.append({"id": f["id"], "name": f["name"], "steps": step_out})
    return out


def compute_rollup(site_id: int, day: str) -> dict:
    """Beregn + lagre dags-aggregat for én site/dag, med sha256 av kanonisk JSON.
    Hashen er det som forankres on-chain (B2) for å gjøre tallene uforanderlige."""
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start, end = f"{day} 00:00:00", (d + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
    base = f"site_id = {P} AND ts >= {P} AND ts < {P} AND name = 'pageview'"
    a = (site_id, start, end)
    with _cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS n FROM events WHERE {base}", a)
        pv = cur.fetchone()["n"]
        cur.execute(f"SELECT COUNT(DISTINCT visitor_hash) AS n FROM events WHERE {base}", a)
        vis = cur.fetchone()["n"]
        cur.execute(f"SELECT visitor_hash, ts FROM events WHERE {base} ORDER BY visitor_hash, ts", a)
        sessions, bounces = _sessionize(cur.fetchall())
        br = round(bounces / sessions * 100) if sessions else 0
        payload = {
            "site_id": site_id, "day": day, "pageviews": pv,
            "visitors": vis, "sessions": sessions, "bounce_rate": br,
        }
        h = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        cur.execute(
            f"""INSERT INTO daily_rollups
                    (site_id, day, pageviews, visitors, sessions, bounce_rate, rollup_hash)
                VALUES ({P}, {P}, {P}, {P}, {P}, {P}, {P})
                ON CONFLICT (site_id, day) DO UPDATE SET
                    pageviews = excluded.pageviews, visitors = excluded.visitors,
                    sessions = excluded.sessions, bounce_rate = excluded.bounce_rate,
                    rollup_hash = excluded.rollup_hash""",
            (site_id, day, pv, vis, sessions, br, h),
        )
    return {**payload, "rollup_hash": h}


def rollup_all(day: str | None = None) -> tuple[int, str]:
    """Rollup for alle sites for en dag (default i går, UTC). Kjøres nattlig via cron."""
    if day is None:
        day = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    with _cursor() as cur:
        cur.execute("SELECT id FROM sites")
        ids = [r["id"] for r in cur.fetchall()]
    for sid in ids:
        compute_rollup(sid, day)
    return len(ids), day


def recent_rollups(site_id: int, limit: int = 14) -> list[dict]:
    with _cursor() as cur:
        cur.execute(
            f"SELECT day, pageviews, visitors, rollup_hash, txid FROM daily_rollups "
            f"WHERE site_id = {P} ORDER BY day DESC LIMIT {int(limit)}",
            (site_id,),
        )
        return [dict(r) for r in cur.fetchall()]


def pending_rollups() -> list[dict]:
    """Alle forseglede, men ikke-forankrede rollups — bladene i neste Merkle-batch.
    Deterministisk rekkefølge (site_id, day) så roten er reproduserbar."""
    with _cursor() as cur:
        cur.execute(
            "SELECT site_id, day, rollup_hash FROM daily_rollups "
            "WHERE txid IS NULL AND rollup_hash IS NOT NULL ORDER BY site_id, day"
        )
        return [dict(r) for r in cur.fetchall()]


def record_anchor(items: list[dict]) -> None:
    """Lagre rot + inklusjons-bevis + txid på hver forankret rollup."""
    with _cursor() as cur:
        for it in items:
            cur.execute(
                f"UPDATE daily_rollups SET merkle_root = {P}, merkle_proof = {P}, "
                f"txid = {P}, anchored_at = {P} WHERE site_id = {P} AND day = {P}",
                (it["root"], it["proof"], it["txid"], it["anchored_at"], it["site_id"], it["day"]),
            )


def resolve_site(public_id: str) -> dict | None:
    with _cursor() as cur:
        cur.execute(
            f"SELECT id, tenant_id, domain FROM sites WHERE public_id = {P}",
            (public_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def insert_event(site_id: int, ev: dict) -> None:
    """Lagre ett event. INGEN IP, INGEN cookie i `ev` — kun grove kategorier."""
    with _cursor() as cur:
        cur.execute(
            f"""INSERT INTO events
                (site_id, name, path, referrer_src, country, region, device, browser, os, visitor_hash, session_id)
                VALUES ({P}, {P}, {P}, {P}, {P}, {P}, {P}, {P}, {P}, {P}, {P})""",
            (
                site_id,
                ev.get("name", "pageview"),
                ev.get("path", "/"),
                ev.get("referrer_src"),
                ev.get("country"),
                ev.get("region"),
                ev.get("device"),
                ev.get("browser"),
                ev.get("os"),
                ev["visitor_hash"],
                ev.get("session_id"),
            ),
        )


def _period_window(days: int) -> tuple[str, str]:
    """[start, end)-vindu (UTC) som dekker `days` kalenderdager t.o.m. i dag.

    Naive UTC-strenger → samme SQL kjører identisk på Postgres og SQLite.
    """
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=days - 1)).strftime("%Y-%m-%d 00:00:00")
    end = (now + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
    return start, end


def _bucket_expr(unit: str) -> str:
    """SQL-uttrykk som trunkerer ts til dag/time, per backend."""
    if _USE_PG:
        fmt = "YYYY-MM-DD" if unit == "day" else "YYYY-MM-DD HH24"
        return f"to_char(ts, '{fmt}')"
    return f"substr(ts, 1, {10 if unit == 'day' else 13})"


def _bucket_labels(days: int, unit: str) -> list[str]:
    """Full liste over buckets (også tomme), så grafen blir sammenhengende."""
    now = datetime.now(timezone.utc)
    if unit == "hour":
        d = now.strftime("%Y-%m-%d")
        return [f"{d} {h:02d}" for h in range(now.hour + 1)]
    return [(now - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d") for i in range(days)]


_SESSION_GAP = 30 * 60  # 30 min inaktivitet = ny sesjon (analytics-standard)


def _epoch(ts) -> float:
    """ts er datetime (Postgres) eller str (SQLite) — normaliser til epoch-sekunder.
    Kun differanser brukes, så absolutt tidssone er likegyldig."""
    if isinstance(ts, str):
        return datetime.strptime(ts[:19], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        ).timestamp()
    return ts.timestamp()


def _sessionize(rows) -> tuple[int, int]:
    """Tell sesjoner + bounces fra (visitor_hash, ts)-rader sortert på (visitor_hash, ts).

    En bounce = sesjon med nøyaktig én sidevisning. MERK: visitor_hash roterer
    daglig, så sesjoner krysser ikke midnatt (akseptabelt — intradag uansett)."""
    sessions = bounces = n_in = 0
    prev_v, prev_e = None, 0.0
    for r in rows:
        v, e = r["visitor_hash"], _epoch(r["ts"])
        if v != prev_v or (e - prev_e) > _SESSION_GAP:
            if n_in == 1:
                bounces += 1
            sessions += 1
            n_in = 1
        else:
            n_in += 1
        prev_v, prev_e = v, e
    if n_in == 1:
        bounces += 1
    return sessions, bounces


def stats(site_id: int, days: int = 7) -> dict:
    """KPI-er + topp sider/kilder for et periodevindu.

    MERK: unike besøkende telles via daglig-roterende hash, så over flere dager
    summeres daglige unike (en gjenganger teller én gang per dag) — samme
    personvern-bevarende oppførsel som Plausible.
    """
    start, end = _period_window(days)
    # Traffic-metrikk teller KUN sidevisninger — egendefinerte hendelser holdes utenfor.
    where = f"site_id = {P} AND ts >= {P} AND ts < {P} AND name = 'pageview'"
    args = (site_id, start, end)
    with _cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS n FROM events WHERE {where}", args)
        pv = cur.fetchone()["n"]
        cur.execute(f"SELECT COUNT(DISTINCT visitor_hash) AS n FROM events WHERE {where}", args)
        vis = cur.fetchone()["n"]
        cur.execute(
            f"SELECT path, COUNT(*) AS n FROM events WHERE {where} "
            "GROUP BY path ORDER BY n DESC LIMIT 10",
            args,
        )
        top_paths = [dict(r) for r in cur.fetchall()]
        cur.execute(
            f"SELECT COALESCE(referrer_src, 'direkte') AS src, COUNT(*) AS n "
            f"FROM events WHERE {where} GROUP BY src ORDER BY n DESC LIMIT 10",
            args,
        )
        top_src = [dict(r) for r in cur.fetchall()]

        # Breakdowns på enhet/nettleser/OS (kategorier fra UA, UA-en selv lagres ikke)
        breakdowns = {}
        for col in ("device", "browser", "os", "country", "region"):
            cur.execute(
                f"SELECT COALESCE({col}, 'ukjent') AS k, COUNT(*) AS n "
                f"FROM events WHERE {where} GROUP BY k ORDER BY n DESC LIMIT 10",
                args,
            )
            breakdowns[col] = [dict(r) for r in cur.fetchall()]

        # Rader for sessionisering (visitor_hash + tid)
        cur.execute(
            f"SELECT visitor_hash, ts FROM events WHERE {where} ORDER BY visitor_hash, ts",
            args,
        )
        sess_rows = cur.fetchall()

    sessions, bounces = _sessionize(sess_rows)
    bounce_rate = round(bounces / sessions * 100) if sessions else 0
    views_per = round(pv / sessions, 1) if sessions else 0.0

    return {
        "pageviews": pv,
        "visitors": vis,
        "sessions": sessions,
        "bounce_rate": bounce_rate,
        "views_per_session": views_per,
        "top_paths": top_paths,
        "top_sources": top_src,
        "devices": breakdowns["device"],
        "browsers": breakdowns["browser"],
        "os": breakdowns["os"],
        "countries": breakdowns["country"],
        "regions": breakdowns["region"],
    }


def timeseries(site_id: int, days: int = 7) -> list[dict]:
    """Per-dag (eller per-time for days=1) buckets med pv + unike. Tomme fylles med 0."""
    unit = "hour" if days == 1 else "day"
    start, end = _period_window(days)
    b = _bucket_expr(unit)
    where = f"site_id = {P} AND ts >= {P} AND ts < {P}"
    with _cursor() as cur:
        cur.execute(
            f"SELECT {b} AS bucket, COUNT(*) AS pv, COUNT(DISTINCT visitor_hash) AS vis "
            f"FROM events WHERE {where} GROUP BY bucket ORDER BY bucket",
            (site_id, start, end),
        )
        found = {r["bucket"]: r for r in cur.fetchall()}
    out = []
    for label in _bucket_labels(days, unit):
        r = found.get(label)
        out.append(
            {
                "bucket": label,
                "pageviews": r["pv"] if r else 0,
                "visitors": r["vis"] if r else 0,
            }
        )
    return out
