"""Purge the leftover accounts from the 2026-07-20/21 /signup abuse incident.

Incident (see IN_FLIGHT.md 2026-07-22): a list-bot abused /signup as an
e-mail oracle, creating 68 fake accounts (user id 2-69; id 1 is the only
real account) using strangers' e-mail addresses. Each signup triggered a
verification e-mail to that stranger — 12 bounced, 4 were clicked. The
abuse vector is fixed (honeypot + per-IP/global throttle, commit 033e33f),
but the rows still exist: a GDPR liability, since we're holding e-mail
addresses of people who never signed up for anything.

This script deletes those rows for one tenant/user at a time, walking the
full FK graph (event_items -> events -> search_stats -> daily_rollups ->
funnels -> goals -> sites -> api_keys -> reset_tokens -> users -> tenants).
(db/schema.sql also has an `anchors` table, but nothing in app/store.py
ever writes to it — BSV-anchoring actually lives on daily_rollups columns —
so it's dead schema and there's nothing there to purge.)

Schema note on "never logged in": this app has no last-login column —
sessions are signed cookies (itsdangerous), never persisted to the DB. The
only real signal available is `users.email_verified`. We treat
email_verified = 0 as "never used the account" and back that up with hard
safety nets (see `assert_safe_to_delete`): the run refuses outright if the
selection would touch id 1, any verified user, any tenant that has events
(real product usage), any api_key that was ever used, or any tenant that
has OTHER users not in the selection (would mean deleting a shared tenant
out from under a real user — shouldn't currently be possible since
create_account() always creates a fresh 1:1 tenant, but we check anyway).

Usage (run inside the app container, where DATABASE_URL is already set):

    # dry run (default) - prints per-table counts + a sha256 of the emails,
    # never the emails themselves. Nothing is written.
    docker compose -f docker-compose.b550.yml exec app \\
        python scripts/purge_bot_signups.py

    # apply - writes a 0600 JSON backup of every affected row, then deletes
    # everything inside one transaction.
    docker compose -f docker-compose.b550.yml exec app \\
        python scripts/purge_bot_signups.py --apply

Selection defaults match the July incident; override only for testing:
    --min-id 2 --max-id 69 --start 2026-07-20 --end 2026-07-22 (inclusive, UTC)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Allow `python scripts/purge_bot_signups.py` to be run from any CWD (matters
# both for `docker compose exec` — WORKDIR /app — and for local/test runs).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import store  # noqa: E402

P = store.P

DEFAULT_MIN_ID = 2
DEFAULT_MAX_ID = 69
DEFAULT_START = "2026-07-20"
DEFAULT_END = "2026-07-22"

# Child -> parent order, all deleted by site_id.
# NOTE: db/schema.sql also defines an `anchors` table, but no code path in
# app/store.py ever writes to it (BSV-anchoring actually lives on
# daily_rollups.{merkle_root,merkle_proof,txid,anchored_at}) and it isn't
# even present in the SQLite mirror — it's dead schema, not wired up. Left
# out here on purpose; nothing to purge there since it's always empty.
_SITE_SCOPED_TABLES = [
    "event_items",
    "events",
    "search_stats",
    "daily_rollups",
    "funnels",
    "goals",
]


class UnsafeSelection(RuntimeError):
    """Raised when the candidate selection fails a safety check. Never caught
    silently — the script always exits non-zero and deletes nothing."""


def _end_exclusive(end_day: str) -> str:
    d = date.fromisoformat(end_day) + timedelta(days=1)
    return d.strftime("%Y-%m-%d 00:00:00")


def _in_clause(n: int) -> str:
    return ", ".join([P] * n)


def select_candidates(cur, min_id: int, max_id: int, start: str, end: str) -> list[dict]:
    """Users matching the incident criteria: id range, created in [start, end]
    (UTC, end inclusive), never e-mail-verified."""
    end_excl = _end_exclusive(end)
    start_ts = f"{start} 00:00:00"
    cur.execute(
        f"SELECT id, tenant_id, email, email_verified, created_at FROM users "
        f"WHERE id BETWEEN {P} AND {P} AND created_at >= {P} AND created_at < {P} "
        f"AND email_verified = 0 ORDER BY id",
        (min_id, max_id, start_ts, end_excl),
    )
    return [dict(r) for r in cur.fetchall()]


def assert_safe_to_delete(cur, users: list[dict]) -> None:
    """Hard refusal checks. Raises UnsafeSelection (never deletes) if any
    candidate looks like it could be the real account or a used account."""
    if not users:
        return

    ids = [u["id"] for u in users]
    tenant_ids = sorted({u["tenant_id"] for u in users})

    if 1 in ids:
        raise UnsafeSelection("selection includes user id 1 (the real account) — refusing.")

    verified = [u["id"] for u in users if u["email_verified"]]
    if verified:
        raise UnsafeSelection(f"selection includes verified user(s): {verified} — refusing.")

    # Any events under any of the candidate tenants' sites = real usage.
    cur.execute(
        f"SELECT COUNT(*) AS n FROM events e "
        f"JOIN sites s ON s.id = e.site_id "
        f"WHERE s.tenant_id IN ({_in_clause(len(tenant_ids))})",
        tuple(tenant_ids),
    )
    if int(dict(cur.fetchone())["n"]) > 0:
        raise UnsafeSelection(
            "selection includes tenant(s) with recorded events (real usage) — refusing."
        )

    # Any api_key ever actually used under a candidate tenant.
    cur.execute(
        f"SELECT COUNT(*) AS n FROM api_keys "
        f"WHERE tenant_id IN ({_in_clause(len(tenant_ids))}) AND last_used_at IS NOT NULL",
        tuple(tenant_ids),
    )
    if int(dict(cur.fetchone())["n"]) > 0:
        raise UnsafeSelection(
            "selection includes tenant(s) with a used api_key — refusing."
        )

    # Any tenant shared with a user NOT in the selection (shouldn't happen —
    # create_account() always makes a fresh 1:1 tenant — but check anyway).
    cur.execute(
        f"SELECT DISTINCT tenant_id FROM users "
        f"WHERE tenant_id IN ({_in_clause(len(tenant_ids))}) AND id NOT IN ({_in_clause(len(ids))})",
        tuple(tenant_ids) + tuple(ids),
    )
    shared = [dict(r)["tenant_id"] for r in cur.fetchall()]
    if shared:
        raise UnsafeSelection(
            f"tenant(s) {shared} have another user outside the selection — refusing."
        )


def _site_ids_for_tenants(cur, tenant_ids: list[int]) -> list[int]:
    if not tenant_ids:
        return []
    cur.execute(
        f"SELECT id FROM sites WHERE tenant_id IN ({_in_clause(len(tenant_ids))})",
        tuple(tenant_ids),
    )
    return [dict(r)["id"] for r in cur.fetchall()]


def table_counts(cur, users: list[dict]) -> dict[str, int]:
    """Per-table row counts that WOULD be deleted for this selection. Read-only."""
    ids = [u["id"] for u in users]
    tenant_ids = sorted({u["tenant_id"] for u in users})
    emails = [u["email"] for u in users]
    site_ids = _site_ids_for_tenants(cur, tenant_ids)

    counts: dict[str, int] = {"tenants": len(tenant_ids), "users": len(ids), "sites": len(site_ids)}

    for table in _SITE_SCOPED_TABLES:
        if not site_ids:
            counts[table] = 0
            continue
        cur.execute(
            f"SELECT COUNT(*) AS n FROM {table} WHERE site_id IN ({_in_clause(len(site_ids))})",
            tuple(site_ids),
        )
        counts[table] = int(dict(cur.fetchone())["n"])

    if tenant_ids:
        cur.execute(
            f"SELECT COUNT(*) AS n FROM api_keys WHERE tenant_id IN ({_in_clause(len(tenant_ids))})",
            tuple(tenant_ids),
        )
        counts["api_keys"] = int(dict(cur.fetchone())["n"])
    else:
        counts["api_keys"] = 0

    if emails:
        cur.execute(
            f"SELECT COUNT(*) AS n FROM reset_tokens WHERE email IN ({_in_clause(len(emails))})",
            tuple(emails),
        )
        counts["reset_tokens"] = int(dict(cur.fetchone())["n"])
    else:
        counts["reset_tokens"] = 0

    return counts


def email_digest(users: list[dict]) -> str:
    """sha256 of the sorted, lower-cased e-mail list. Printed instead of the
    e-mails themselves so a dry-run report can be shared/pasted safely."""
    emails = sorted(u["email"].strip().lower() for u in users)
    return hashlib.sha256("\n".join(emails).encode()).hexdigest()


def collect_backup(cur, users: list[dict]) -> dict:
    """Full row dump (not just counts) of everything about to be deleted."""
    ids = [u["id"] for u in users]
    tenant_ids = sorted({u["tenant_id"] for u in users})
    emails = [u["email"] for u in users]
    site_ids = _site_ids_for_tenants(cur, tenant_ids)

    def fetch_all(sql: str, params: tuple) -> list[dict]:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    backup: dict = {
        "tenants": fetch_all(
            f"SELECT * FROM tenants WHERE id IN ({_in_clause(len(tenant_ids))})", tuple(tenant_ids)
        )
        if tenant_ids
        else [],
        "users": fetch_all(f"SELECT * FROM users WHERE id IN ({_in_clause(len(ids))})", tuple(ids)),
        "sites": fetch_all(
            f"SELECT * FROM sites WHERE id IN ({_in_clause(len(site_ids))})", tuple(site_ids)
        )
        if site_ids
        else [],
        "api_keys": fetch_all(
            f"SELECT * FROM api_keys WHERE tenant_id IN ({_in_clause(len(tenant_ids))})",
            tuple(tenant_ids),
        )
        if tenant_ids
        else [],
        "reset_tokens": fetch_all(
            f"SELECT * FROM reset_tokens WHERE email IN ({_in_clause(len(emails))})", tuple(emails)
        )
        if emails
        else [],
    }
    for table in _SITE_SCOPED_TABLES:
        backup[table] = (
            fetch_all(
                f"SELECT * FROM {table} WHERE site_id IN ({_in_clause(len(site_ids))})",
                tuple(site_ids),
            )
            if site_ids
            else []
        )
    return backup


def write_backup(backup: dict, backup_dir: Path) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = backup_dir / f"purge_bot_signups_{ts}.json"
    path.write_text(json.dumps(backup, indent=2, default=str))
    os.chmod(path, 0o600)
    return path


def delete_selection(cur, users: list[dict]) -> dict[str, int]:
    """Runs all deletes on the given cursor (caller controls the transaction —
    call this inside a single `with store._cursor() as cur:` block so every
    delete commits or rolls back together)."""
    ids = [u["id"] for u in users]
    tenant_ids = sorted({u["tenant_id"] for u in users})
    emails = [u["email"] for u in users]
    site_ids = _site_ids_for_tenants(cur, tenant_ids)

    deleted: dict[str, int] = {}
    for table in _SITE_SCOPED_TABLES:
        if not site_ids:
            deleted[table] = 0
            continue
        cur.execute(
            f"DELETE FROM {table} WHERE site_id IN ({_in_clause(len(site_ids))})",
            tuple(site_ids),
        )
        deleted[table] = cur.rowcount

    if site_ids:
        cur.execute(
            f"DELETE FROM sites WHERE id IN ({_in_clause(len(site_ids))})", tuple(site_ids)
        )
        deleted["sites"] = cur.rowcount
    else:
        deleted["sites"] = 0

    if tenant_ids:
        cur.execute(
            f"DELETE FROM api_keys WHERE tenant_id IN ({_in_clause(len(tenant_ids))})",
            tuple(tenant_ids),
        )
        deleted["api_keys"] = cur.rowcount
    else:
        deleted["api_keys"] = 0

    if emails:
        cur.execute(
            f"DELETE FROM reset_tokens WHERE email IN ({_in_clause(len(emails))})", tuple(emails)
        )
        deleted["reset_tokens"] = cur.rowcount
    else:
        deleted["reset_tokens"] = 0

    cur.execute(f"DELETE FROM users WHERE id IN ({_in_clause(len(ids))})", tuple(ids))
    deleted["users"] = cur.rowcount

    if tenant_ids:
        cur.execute(
            f"DELETE FROM tenants WHERE id IN ({_in_clause(len(tenant_ids))})", tuple(tenant_ids)
        )
        deleted["tenants"] = cur.rowcount
    else:
        deleted["tenants"] = 0

    return deleted


def _print_report(users: list[dict], counts: dict[str, int]) -> None:
    print(f"candidates: {len(users)} user(s), ids {[u['id'] for u in users]}")
    for table in ("tenants", "users", "sites", *_SITE_SCOPED_TABLES, "api_keys", "reset_tokens"):
        print(f"  {table:14s} {counts.get(table, 0)}")
    print(f"sha256(sorted emails) = {email_digest(users)}")


def run(args: argparse.Namespace) -> int:
    with store._cursor() as cur:
        users = select_candidates(cur, args.min_id, args.max_id, args.start, args.end)
        if not users:
            print("no candidates match the selection — nothing to do.")
            return 0
        try:
            assert_safe_to_delete(cur, users)
        except UnsafeSelection as exc:
            print(f"REFUSING: {exc}", file=sys.stderr)
            return 1
        counts = table_counts(cur, users)

    _print_report(users, counts)

    if not args.apply:
        print("\ndry run — nothing deleted. Re-run with --apply to purge.")
        return 0

    with store._cursor() as cur:
        # Re-select + re-check inside the same transaction as the backup/delete,
        # in case anything changed between the dry-run report above and here.
        users = select_candidates(cur, args.min_id, args.max_id, args.start, args.end)
        if not users:
            print("no candidates match the selection anymore — nothing to do.")
            return 0
        try:
            assert_safe_to_delete(cur, users)
        except UnsafeSelection as exc:
            print(f"REFUSING: {exc}", file=sys.stderr)
            return 1
        backup = collect_backup(cur, users)
        backup_path = write_backup(backup, Path(args.backup_dir))
        print(f"backup written: {backup_path} (0600, {len(users)} user(s))")
        deleted = delete_selection(cur, users)

    print("deleted:")
    for table, n in deleted.items():
        print(f"  {table:14s} {n}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--min-id", type=int, default=DEFAULT_MIN_ID)
    parser.add_argument("--max-id", type=int, default=DEFAULT_MAX_ID)
    parser.add_argument("--start", default=DEFAULT_START, help="UTC date, inclusive (YYYY-MM-DD)")
    parser.add_argument("--end", default=DEFAULT_END, help="UTC date, inclusive (YYYY-MM-DD)")
    parser.add_argument(
        "--apply", action="store_true", help="write backup + delete (default: dry run only)"
    )
    parser.add_argument(
        "--backup-dir", default="backups", help="where the pre-delete JSON backup is written"
    )
    return parser


def main(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
