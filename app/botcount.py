"""Robots get their own count.

A non-human hit (bot UA, AI crawler, AI agent, datacenter network) is still dropped
from `events`, exactly as before; nothing changes for people. It is now also counted
in the aggregate table bot_daily: class, agent name and, for ai/search/agent, the
masked path. No IP, no UA string, no hash.

Hits are summed in memory and added to the database at most once a minute (or when
the buffer grows large), so a flood of curl requests costs a dict increment, not a
DB write per request. A crash loses at most the last minute of robot counts. The
dashboard and the API flush before they read, so the owner sees fresh numbers.

Rate-limit drops are NOT counted here: a script looping on a real visitor's page (a
broken single-page app) looks the same as a bot loop, so we cannot honestly call it
a robot. They stay in the drop log (app/main.py _note_drop).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from app import store
from app.useragent import BOT_CLASSES

log = logging.getLogger("sporlos")

FLUSH_SECONDS = 60
FLUSH_KEYS = 2_000   # flush early when this many distinct rows are waiting
MAX_KEYS = 20_000    # while the DB is unreachable, keep at most this many rows

_lock = threading.Lock()
_buf: dict[tuple, int] = {}
_flushed_at = time.monotonic()


def note(site_id: int, cls: str, agent: str, path: str = "") -> None:
    """Count one non-human hit. `path` must already be cleaned and masked (the
    caller passes "" for classes that keep no path). Best-effort: never raises."""
    try:
        if cls not in BOT_CLASSES:
            cls = "other"
        if cls not in store.BOT_PATH_CLASSES:
            path = ""
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = (site_id, day, cls, (agent or "")[:64], (path or "")[:512])
        with _lock:
            if key in _buf or len(_buf) < MAX_KEYS:
                _buf[key] = _buf.get(key, 0) + 1
            full = len(_buf) >= FLUSH_KEYS
        if full:
            flush()
        else:
            maybe_flush()
    except Exception:
        log.exception("ingest: robot count failed")


def maybe_flush() -> None:
    """Flush when the buffer has waited FLUSH_SECONDS. Cheap when there is nothing."""
    if _buf and time.monotonic() - _flushed_at >= FLUSH_SECONDS:
        flush()


def flush() -> None:
    """Move buffered hits to bot_daily. Never raises: on a DB error the hits go back
    into the buffer (bounded by MAX_KEYS) for the next try."""
    global _flushed_at
    with _lock:
        batch = dict(_buf)
        _buf.clear()
        _flushed_at = time.monotonic()
    if not batch:
        return
    try:
        store.add_bot_counts(batch)
    except Exception:
        log.exception("ingest: robot count could not be stored")
        with _lock:
            for k, n in batch.items():
                if k in _buf or len(_buf) < MAX_KEYS:
                    _buf[k] = _buf.get(k, 0) + n


def pending() -> dict[tuple, int]:
    """Snapshot of hits not yet written (for tests and diagnostics)."""
    with _lock:
        return dict(_buf)
