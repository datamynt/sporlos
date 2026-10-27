"""Scraper bursts: a wave of brand-new "visitors", each landing on a different deep page.

Why this exists. On 27.10.2026 a price scraper walked through a proxy pool and read
~20 product pages on one shop in 9 seconds. Every hit had its own IP, its own
random-but-plausible browser UA and a spoofed search referrer, and a few came from
residential addresses, so neither the UA list, the datacenter ASN list nor the
webdriver flag could see it. What gives it away is not any single hit but the shape of
the traffic: a crowd of NEW visitors, one page view each, every one on a DIFFERENT
page that nobody lands on directly, from one country, within seconds.

The rule (all four numbers are constants below, tuned conservatively)
---------------------------------------------------------------------
A "landing" is a visitor hash's first page view. A landing is a *candidate* when it
has a country, is not on "/" and carries no utm parameters. Within one site and
one country, over WINDOW seconds, the candidates whose path is UNIQUE in the window (and the
LOOKAHEAD seconds after it)
are counted. The unique landings are a burst when

    count >= MIN_UNIQUE                                (a crowd)
    count >= RATE_FACTOR x the usual number of new visitors from that country per
                           window over the BASELINE before it   (not business as usual)

A landing is judged only LOOKAHEAD (5) seconds after it happened, so that a page a
second person opens a moment later is seen as shared, not unique. Once a burst is on, further candidate landings from the same site and country count as
part of it until EXTEND seconds pass without one (a scraper does not stop at the 8th).

What it deliberately does NOT fire on
* a newsletter or social blast: people land on the SAME page (not unique), and the
  links carry utm parameters (never candidates);
* a busy site with a long tail of product pages: the second condition scales with the
  site's own traffic from that country, so a shop that gets 25 new visitors per window
  from Norway needs 125 in a window before it counts;
* a person landing on a deep page next to a burst from another country.
A real person from the burst's country who lands on a page nobody else lands on in
the same 15 seconds can be caught; at MIN_UNIQUE 8 that costs one stray page view,
and the dashboard counts it as a robot hit (script/"burst"), not as a loss of data.

What it does to the data
The members' events are MOVED: deleted from `events` and `event_items`, added to
bot_daily as class "script", agent "burst", in ONE transaction, so humans and robots
are never mixed and running it twice moves nothing the second time. Only events of the
burst's own visitor hashes in [first landing, last landing + GRACE] are touched. No IP
and no UA is read or stored here, only what is already in `events`. A flagged hash is
remembered in memory for FLAG_TTL so its late events (checkout_start ...) are
counted as robot hits at ingest instead of being stored.

The live job (run_forever) sweeps every SWEEP_SECONDS, so a burst sits in `events`
for at most about that long. `python -m app.manage burst-backfill` applies the same
rule to any site and time range (dry run unless --apply).
"""

from __future__ import annotations

import asyncio
import bisect
import logging
import os
import threading
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from app import store

log = logging.getLogger("sporlos")

WINDOW = 15            # seconds a burst must fit in
LOOKAHEAD = 5          # seconds after a landing that still count when judging if its page is unique
MIN_UNIQUE = 8         # unique-path landings needed inside one window
RATE_FACTOR = 5        # ... and this many times the usual new visitors per window
BASELINE = 30 * 60     # seconds before the window that define "usual"
EXTEND = 30            # seconds a burst stays on after its last landing
GRACE = 10 * 60        # seconds after the last landing whose events still go along
SWEEP_SECONDS = 15     # live job interval
LOOKBACK = 90          # live job: how far back it looks for landings (seconds)
FLAG_TTL = 60 * 60     # in-memory memory of flagged hashes (seconds)
FLAG_MAX = 10_000

_lock = threading.Lock()
_active: dict[tuple[int, str], datetime] = {}      # (site, country) -> burst on until
_flagged: dict[tuple[int, str], float] = {}        # (site, hash) -> monotonic expiry


@dataclass(frozen=True)
class Landing:
    """A visitor hash's first page view. Nothing here identifies a person: the hash
    is the same daily-salted value already in `events`."""
    site_id: int
    vhash: str
    ts: datetime          # naive UTC
    path: str
    country: str | None
    tagged: bool = False  # carries utm parameters

    @property
    def candidate(self) -> bool:
        return bool(self.country) and self.path != "/" and not self.tagged


Baseline = Callable[[int, str, datetime, datetime, set], int]


def enabled() -> bool:
    return os.environ.get("SPORLOS_BURST_FILTER", "1").strip().lower() not in ("0", "false", "no")


def is_flagged(site_id: int, vhash: str) -> bool:
    """True for a hash the burst job moved out of `events` recently (see module doc)."""
    with _lock:
        exp = _flagged.get((site_id, vhash))
        if exp is None:
            return False
        if exp < time.monotonic():
            _flagged.pop((site_id, vhash), None)
            return False
        return True


def _remember(site_id: int, hashes) -> None:
    now = time.monotonic()
    with _lock:
        if len(_flagged) >= FLAG_MAX:
            for k in [k for k, e in _flagged.items() if e < now]:
                _flagged.pop(k, None)
        for h in hashes:
            if len(_flagged) < FLAG_MAX:
                _flagged[(site_id, h)] = now + FLAG_TTL


def reset() -> None:
    """Forget in-memory state (tests)."""
    with _lock:
        _active.clear()
        _flagged.clear()


# --- The rule, on plain data ------------------------------------------------------------

def find_bursts(
    landings: list[Landing],
    baseline: Baseline,
    active: dict[tuple[int, str], datetime] | None = None,
    horizon: datetime | None = None,
) -> list[Landing]:
    """The landings that belong to a burst, in time order.

    `baseline(site_id, country, start, end, exclude)` = number of landings (any path,
    utm or not) the DB has for that site/country in [start, end), minus those whose
    hash is in `exclude` (landings this very run already flagged, still in the DB).
    `active` carries "burst on until" across calls (the live job keeps one).
    `horizon`: landings after it are only used to judge earlier ones (the look-ahead),
    never flagged themselves; the live job passes now - LOOKAHEAD."""
    active = active if active is not None else {}
    window = timedelta(seconds=WINDOW)
    ahead = timedelta(seconds=LOOKAHEAD)
    cands: dict[tuple[int, str], list[Landing]] = defaultdict(list)
    for lnd in sorted(landings, key=lambda x: x.ts):
        if lnd.candidate:
            cands[(lnd.site_id, lnd.country)].append(lnd)
    times = {k: [c.ts for c in v] for k, v in cands.items()}
    done: set[int] = set()                      # id() of flagged landings
    flagged: list[Landing] = []
    flagged_by_key: dict[tuple[int, str], list[Landing]] = defaultdict(list)

    def flag(key, lands):
        for lnd in lands:
            done.add(id(lnd))
            flagged.append(lnd)
            flagged_by_key[key].append(lnd)

    for key, series in cands.items():
        for lnd in series:
            if id(lnd) in done or (horizon is not None and lnd.ts > horizon):
                continue
            if active.get(key, datetime.min) >= lnd.ts:
                flag(key, [lnd])
                active[key] = lnd.ts + timedelta(seconds=EXTEND)
                continue
            ts = times[key]
            lo = bisect.bisect_left(ts, lnd.ts - window)
            mid = bisect.bisect_right(ts, lnd.ts)
            hi = bisect.bisect_right(ts, lnd.ts + ahead)
            pool = [c for c in series[lo:mid] if id(c) not in done]
            seen = Counter(c.path for c in series[lo:hi] if id(c) not in done)
            unique = [c for c in pool if seen[c.path] == 1]
            if len(unique) < MIN_UNIQUE:
                continue
            start, end = lnd.ts - window - timedelta(seconds=BASELINE), lnd.ts - window
            exclude = {f.vhash for f in flagged_by_key[key]}
            usual = baseline(lnd.site_id, lnd.country, start, end, exclude) * WINDOW / BASELINE
            if len(unique) < RATE_FACTOR * usual:
                continue
            flag(key, unique)
            active[key] = lnd.ts + timedelta(seconds=EXTEND)
    return sorted(flagged, key=lambda x: x.ts)


# --- Database side ----------------------------------------------------------------------

_FIRST_PAGEVIEW = (
    "NOT EXISTS (SELECT 1 FROM events p WHERE p.site_id = e.site_id "
    "AND p.visitor_hash = e.visitor_hash AND p.name = 'pageview' "
    "AND (p.ts < e.ts OR (p.ts = e.ts AND p.id < e.id)))"
)


def _fmt(t: datetime) -> str:
    return t.strftime("%Y-%m-%d %H:%M:%S")


def _naive_utc(t) -> datetime:
    if isinstance(t, str):
        return datetime.fromisoformat(t.replace("T", " ")[:19])
    return t.astimezone(timezone.utc).replace(tzinfo=None) if t.tzinfo else t


def fetch_landings(start: datetime, end: datetime, site_id: int | None = None) -> list[Landing]:
    """Visitor hashes whose first page view falls in [start, end)."""
    sql = (
        "SELECT e.site_id, e.visitor_hash, e.ts, e.path, e.country, e.utm_source, "
        "e.utm_medium, e.utm_campaign FROM events e "
        f"WHERE e.name = 'pageview' AND e.ts >= {store.P} AND e.ts < {store.P} "
    )
    args: list = [_fmt(start), _fmt(end)]
    if site_id is not None:
        sql += f"AND e.site_id = {store.P} "
        args.append(site_id)
    sql += f"AND {_FIRST_PAGEVIEW} ORDER BY e.ts, e.id"
    with store._cursor() as cur:
        cur.execute(sql, args)
        rows = cur.fetchall()
    return [
        Landing(r["site_id"], r["visitor_hash"], _naive_utc(r["ts"]), r["path"], r["country"],
                bool(r["utm_source"] or r["utm_medium"] or r["utm_campaign"]))
        for r in rows
    ]


def db_baseline(site_id: int, country: str, start: datetime, end: datetime, exclude: set) -> int:
    """Landings from `country` on the site in [start, end), minus `exclude`."""
    with store._cursor() as cur:
        cur.execute(
            f"SELECT e.visitor_hash FROM events e WHERE e.name = 'pageview' AND e.site_id = {store.P} "
            f"AND e.country = {store.P} AND e.ts >= {store.P} AND e.ts < {store.P} AND {_FIRST_PAGEVIEW}",
            (site_id, country, _fmt(start), _fmt(end)),
        )
        return sum(1 for r in cur.fetchall() if r["visitor_hash"] not in exclude)


def move_to_bots(site_id: int, members: list[Landing], *, apply: bool = True) -> dict:
    """Move the events of these landings' hashes out of `events` into bot_daily
    (class script, agent burst), in one transaction. Only events of those hashes in
    [first landing, last landing + GRACE]. Returns {"events", "days"}; with
    apply=False nothing is written and the same numbers are reported."""
    start = min(m.ts for m in members)
    end = max(m.ts for m in members) + timedelta(seconds=GRACE)
    hashes = sorted({m.vhash for m in members})
    by_day: Counter = Counter()
    ids: list[int] = []
    with store._cursor() as cur:
        for i in range(0, len(hashes), 200):
            chunk = hashes[i:i + 200]
            marks = ",".join([store.P] * len(chunk))
            cur.execute(
                f"SELECT id, ts FROM events WHERE site_id = {store.P} AND ts >= {store.P} "
                f"AND ts <= {store.P} AND visitor_hash IN ({marks})",
                [site_id, _fmt(start), _fmt(end), *chunk],
            )
            for r in cur.fetchall():
                ids.append(r["id"])
                by_day[_naive_utc(r["ts"]).strftime("%Y-%m-%d")] += 1
        if apply and ids:
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                marks = ",".join([store.P] * len(chunk))
                cur.execute(f"DELETE FROM event_items WHERE event_id IN ({marks})", chunk)
                cur.execute(f"DELETE FROM events WHERE id IN ({marks})", chunk)
            for day, n in sorted(by_day.items()):
                cur.execute(
                    f"""INSERT INTO bot_daily (site_id, day, bot_class, agent, path, n)
                        VALUES ({store.P}, {store.P}, 'script', 'burst', '', {store.P})
                        ON CONFLICT (site_id, day, bot_class, agent, path)
                        DO UPDATE SET n = bot_daily.n + excluded.n""",
                    (site_id, day, n),
                )
    return {"events": len(ids), "days": sorted(by_day)}


def apply_bursts(flagged: list[Landing], *, apply: bool = True) -> list[dict]:
    """Group flagged landings per site and move them. One report row per site."""
    out = []
    by_site: dict[int, list[Landing]] = defaultdict(list)
    for f in flagged:
        by_site[f.site_id].append(f)
    for site_id, members in sorted(by_site.items()):
        res = move_to_bots(site_id, members, apply=apply)
        res.update(
            site_id=site_id,
            visitors=len({m.vhash for m in members}),
            first=min(m.ts for m in members),
            last=max(m.ts for m in members),
            countries=sorted({m.country for m in members}),
            paths=len({m.path for m in members}),
        )
        if apply:
            _remember(site_id, {m.vhash for m in members})
        out.append(res)
    return out


def sweep(now: datetime | None = None) -> list[dict]:
    """One pass of the live job: look at the last LOOKBACK seconds, move any burst.
    Safe to call at any time and from several processes: a second pass finds nothing
    left to move."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    lands = fetch_landings(now - timedelta(seconds=LOOKBACK), now + timedelta(seconds=1))
    if not lands:
        return []
    with _lock:
        active = dict(_active)
    # A landing is judged LOOKAHEAD seconds late, so a page that two people open a
    # moment apart is seen as the shared page it is, not as a unique one.
    flagged = find_bursts(lands, db_baseline, active, horizon=now - timedelta(seconds=LOOKAHEAD))
    with _lock:
        _active.clear()
        _active.update({k: v for k, v in active.items() if v >= now - timedelta(seconds=EXTEND)})
    if not flagged:
        return []
    res = apply_bursts(flagged)
    for r in res:
        log.warning(
            "burst: moved %s events (%s visitors, %s paths, %s) for site %s to the robot count",
            r["events"], r["visitors"], r["paths"], "/".join(r["countries"]), r["site_id"],
        )
    return res


def backfill(site_id: int, start: datetime, end: datetime, *, apply: bool = False) -> list[dict]:
    """The same rule over any range of one site. Dry run unless apply=True. Idempotent.
    With apply, the rollups of the touched days are recomputed (anchored days stay)."""
    lands = fetch_landings(start, end, site_id=site_id)
    flagged = find_bursts(lands, db_baseline, {})
    res = apply_bursts(flagged, apply=apply)
    if apply:
        for r in res:
            for day in r["days"]:
                store.compute_rollup(r["site_id"], day)
    return res


async def run_forever() -> None:
    """Background loop for the app's lifespan. Never raises."""
    while True:
        try:
            await asyncio.sleep(SWEEP_SECONDS)
            await asyncio.to_thread(sweep)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("burst: sweep failed")
