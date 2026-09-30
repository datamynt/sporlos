"""Mask ID-like segments in values stored before ingest started masking them.

    python -m app.manage mask-paths            # dry run: counts per table + masked examples
    python -m app.manage mask-paths --apply    # backup CSV first, then one transaction

Covers what ingest now masks (app/pathmask.py): raw event paths and event names,
plus goal match values and funnel steps, which are compared with those.

The daily rollups are left alone. They hold counts only, and their sealed hash is
a sha256 of {site_id, day, pageviews, visitors, sessions, bounce_rate}: no path
goes into it, and masking a path changes none of those numbers, so an anchored
hash stays valid. search_stats is left alone too: it holds what search engines
report for pages they have publicly indexed, not visitor paths.
"""

from __future__ import annotations

import csv
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from app import store
from app.pathmask import mask_label, mask_path

P = store.P


def _mask_goal(match_type: str, value: str) -> str:
    return mask_path(value) if match_type == "path" else mask_label(value)


def _mask_steps(raw: str) -> str | None:
    """Funnel steps are JSON; None if unreadable or unchanged."""
    try:
        steps = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(steps, list):
        return None
    out = []
    for st in steps:
        if isinstance(st, dict) and isinstance(st.get("value"), str):
            st = {**st, "value": _mask_goal("path" if st.get("type") == "path" else "event",
                                            st["value"])}
        out.append(st)
    return json.dumps(out) if out != steps else None


def collect(cur) -> list[tuple[str, int, str, str, str]]:
    """Every stored value masking would change, as (table, id, column, old, new)."""
    changes = []
    cur.execute("SELECT id, path, name FROM events")
    for r in cur:
        new_path = mask_path(r["path"])
        if new_path != r["path"]:
            changes.append(("events", r["id"], "path", r["path"], new_path))
        new_name = mask_label(r["name"])
        if new_name != r["name"]:
            changes.append(("events", r["id"], "name", r["name"], new_name))
    cur.execute("SELECT id, match_type, match_value FROM goals")
    for r in cur.fetchall():
        new = _mask_goal(r["match_type"], r["match_value"])
        if new != r["match_value"]:
            changes.append(("goals", r["id"], "match_value", r["match_value"], new))
    cur.execute("SELECT id, steps FROM funnels")
    for r in cur.fetchall():
        new = _mask_steps(r["steps"])
        if new is not None:
            changes.append(("funnels", r["id"], "steps", r["steps"], new))
    return changes


def summarize(changes) -> dict:
    per = Counter(f"{t}.{c}" for t, _, c, _, _ in changes)
    examples = Counter(new for t, _, c, _, new in changes if (t, c) == ("events", "path"))
    return {"rows": dict(per), "examples": [p for p, _ in examples.most_common(5)]}


def write_backup(changes, backup_dir: Path) -> Path:
    """id + old value of every row about to change, 0600: the old values are the
    very identifiers being removed."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = backup_dir / f"mask-paths-{ts}.csv"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["table", "id", "column", "old_value"])
        for t, i, c, old, _ in changes:
            w.writerow([t, i, c, old])
    return path


def run(apply: bool = False, backup_dir: str | Path = "backups") -> dict:
    """Dry run unless `apply`. With `apply`, the backup is written inside the same
    transaction that then updates, so it holds exactly the rows changed; if the
    backup cannot be written nothing is updated."""
    with store._cursor() as cur:
        changes = collect(cur)
        res = {**summarize(changes), "applied": False, "backup": None}
        if not apply or not changes:
            return res
        res["backup"] = str(write_backup(changes, Path(backup_dir)))
        for table, column in (("events", "path"), ("events", "name"),
                              ("goals", "match_value"), ("funnels", "steps")):
            rows = [(new, i) for t, i, c, _, new in changes if (t, c) == (table, column)]
            if rows:
                cur.executemany(f"UPDATE {table} SET {column} = {P} WHERE id = {P}", rows)
        res["applied"] = True
    return res
