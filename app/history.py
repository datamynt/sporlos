"""Long history: per-day breakdowns kept for the life of the account (daily_breakdowns).

Raw events (one row per hit, with the day's visitor hash) are deleted after
store.RAW_DAYS days by store.retention_sweep. Before that happens, the nightly rollup
(store.compute_rollup) writes this table: per site and day, the same breakdowns the
dashboard shows, as plain counts. No visitor hash and no per-visitor row ever lands
here, so the rows are aggregates, not pseudonymous records. They are deleted only
with the site or the account (store.SITE_TABLES).

One row = (site_id, day, dim, key, currency) -> n, visitors, revenue_cents.

    dim           key                             n          visitors   revenue_cents
    kpi           pageviews|visitors|sessions|    the value  -          -
                  bounces
    page          path                            pageviews  distinct   -
    source        referrer host or 'direkte'      pageviews  distinct   -
    country, region, device, browser, os          pageviews  distinct   -
    campaign      JSON [utm_source, medium, camp] pageviews  distinct   -
    entry, exit   path                            sessions   -          -
    event         event name (not pageview)       hits       distinct   -
    ai            canonical AI assistant host     pageviews  distinct   -
    ai_total      ''                              pageviews  distinct   -
    ecom          '' (currency column set)        orders     -          sum
    ecom_source   first source the same day       orders     -          sum
    ecom_payment  payment slug or 'ukjent'        orders     -          sum
    product       product name                    quantity   -          sum
    bot           JSON [bot_class, agent]         hits       -          -
    bot_ai_path   path an AI crawler fetched      hits       -          -

`visitors` is the number of distinct hashes that day. The hash rotates at UTC
midnight, so a sum over days is exactly what a COUNT(DISTINCT) over the same days
gives on raw events: the dashboard already counts that way («summert per dag»).
Goals need no table of their own: a goal is an event name or a path, and its
completions are the `visitors` of that event or page row, so goals defined later
still work on old days (as long as the page was within the day's top CAP).

Each dim keeps at most CAP keys per site and day (by n). Totals never lose
anything (kpi is uncapped); only the long tail of rarely seen keys on a busy day
is dropped from the per-key lists.

Reading: a window that starts before the raw-data horizon (the first day of the
RAW_DAYS window) is split. Days before the horizon come from this table (and the
per-day totals from daily_rollups for days older than this table); the rest comes
from events exactly as the 7/30/90-day views compute it. Windows that start inside
the horizon never touch this module, so those views are unchanged.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from app import store

P = store.P

CAP = 500  # keys per dim per site per day

_PV_DIMS = {
    "page": "path",
    "source": "COALESCE(referrer_src, 'direkte')",
    "country": "COALESCE(country, 'ukjent')",
    "region": "COALESCE(region, 'ukjent')",
    "device": "COALESCE(device, 'ukjent')",
    "browser": "COALESCE(browser, 'ukjent')",
    "os": "COALESCE(os, 'ukjent')",
}
EVENT_DIMS = (
    "kpi", *_PV_DIMS, "campaign", "entry", "exit", "event", "ai", "ai_total",
    "ecom", "ecom_source", "ecom_payment", "product",
)
BOT_DIMS = ("bot", "bot_ai_path")
ALL_DIMS = EVENT_DIMS + BOT_DIMS

# Stats-API prop -> dim
API_DIMS = {
    "pages": "page", "sources": "source", "countries": "country", "regions": "region",
    "devices": "device", "browsers": "browser", "os": "os",
}


def _i(x) -> int:
    """PG returns SUM(bigint) as Decimal and SUM over NULLs as None."""
    return int(x or 0)


def _day_window(day: str) -> tuple[str, str]:
    d = datetime.strptime(day, "%Y-%m-%d")
    return f"{day} 00:00:00", (d + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")


def _put(out: dict, dim: str, key: str, n, visitors=None, revenue=None, currency: str = "") -> None:
    out.setdefault(dim, {})[(key, currency)] = [
        _i(n), None if visitors is None else _i(visitors), None if revenue is None else _i(revenue)
    ]


def _cap(rows: dict, cap: int | None, by: int = 0) -> dict:
    if not cap or len(rows) <= cap:
        return rows
    keep = sorted(rows.items(), key=lambda kv: (-(kv[1][by] or 0), kv[0]))[:cap]
    return dict(keep)


# --- computing from raw data -----------------------------------------------------------

def event_dims(cur, site_id: int, start: str, end: str, dims, cap: int | None = None) -> dict:
    """Breakdowns over raw events (and bot_daily) for the window [start, end).

    Used per day by the rollup (cap=CAP) and, uncapped, for the recent part of a
    long window. Every query mirrors the one the dashboard runs on the same data."""
    dims = set(dims)
    out: dict = {}
    a = (site_id, start, end)
    pv = f"site_id = {P} AND ts >= {P} AND ts < {P} AND name = 'pageview'"
    lim = f" LIMIT {int(cap)}" if cap else ""

    if "kpi" in dims:
        cur.execute(
            f"SELECT COUNT(*) AS n, COUNT(DISTINCT visitor_hash) AS u FROM events WHERE {pv}", a
        )
        r = cur.fetchone()
        cur.execute(f"SELECT visitor_hash, ts FROM events WHERE {pv} ORDER BY visitor_hash, ts", a)
        sessions, bounces = store._sessionize(cur.fetchall())
        for k, v in (("pageviews", r["n"]), ("visitors", r["u"]),
                     ("sessions", sessions), ("bounces", bounces)):
            _put(out, "kpi", k, v)

    for dim, expr in _PV_DIMS.items():
        if dim not in dims:
            continue
        self_ref = store._NOT_SELF_REFERRAL if dim == "source" else ""
        cur.execute(
            f"SELECT {expr} AS k, COUNT(*) AS n, COUNT(DISTINCT visitor_hash) AS u "
            f"FROM events WHERE {pv}{self_ref} GROUP BY k ORDER BY n DESC, k{lim}",
            a,
        )
        for r in cur.fetchall():
            _put(out, dim, r["k"], r["n"], r["u"])

    if "campaign" in dims:
        cur.execute(
            f"SELECT COALESCE(utm_source,'') AS s, COALESCE(utm_medium,'') AS m, "
            f"COALESCE(utm_campaign,'') AS c, COUNT(*) AS n, COUNT(DISTINCT visitor_hash) AS u "
            f"FROM events WHERE {pv} AND (utm_source IS NOT NULL OR utm_campaign IS NOT NULL) "
            f"GROUP BY s, m, c ORDER BY n DESC, s, m, c{lim}",
            a,
        )
        for r in cur.fetchall():
            _put(out, "campaign", json.dumps([r["s"], r["m"], r["c"]], ensure_ascii=False),
                 r["n"], r["u"])

    if "entry" in dims or "exit" in dims:
        # Same session rule as store.flow_stats.
        cur.execute(
            f"SELECT visitor_hash, ts, path FROM events WHERE {pv} ORDER BY visitor_hash, ts", a
        )
        entries: dict[str, int] = {}
        exits: dict[str, int] = {}
        prev_v, prev_e, last_path = None, 0.0, None
        for r in cur.fetchall():
            v, e, p = r["visitor_hash"], store._epoch(r["ts"]), r["path"]
            if v != prev_v or (e - prev_e) > store._SESSION_GAP:
                if last_path is not None:
                    exits[last_path] = exits.get(last_path, 0) + 1
                entries[p] = entries.get(p, 0) + 1
            last_path = p
            prev_v, prev_e = v, e
        if last_path is not None:
            exits[last_path] = exits.get(last_path, 0) + 1
        for dim, d in (("entry", entries), ("exit", exits)):
            if dim in dims:
                rows = _cap({(k, ""): [n, None, None] for k, n in d.items()}, cap)
                if rows:
                    out[dim] = rows

    if "event" in dims:
        cur.execute(
            f"SELECT name AS k, COUNT(*) AS n, COUNT(DISTINCT visitor_hash) AS u FROM events "
            f"WHERE site_id = {P} AND ts >= {P} AND ts < {P} AND name <> 'pageview' "
            f"GROUP BY name ORDER BY n DESC, name{lim}",
            a,
        )
        for r in cur.fetchall():
            _put(out, "event", r["k"], r["n"], r["u"])

    if "ai" in dims or "ai_total" in dims:
        # Same grouping as store.ai_referral_sources: a visitor seen under both the
        # referrer and the utm spelling of one assistant counts once.
        pred, params = store._ai_where()
        cur.execute(
            f"""SELECT referrer_src AS r, LOWER(utm_source) AS s, visitor_hash AS v,
                       COUNT(*) AS n
                FROM events WHERE {pv} AND {pred}
                GROUP BY referrer_src, LOWER(utm_source), visitor_hash""",
            (*a, *params),
        )
        seen: dict[str, set] = {}
        views: dict[str, int] = {}
        all_v: set = set()
        for r in cur.fetchall():
            k = store._ai_key(r["r"], r["s"])
            seen.setdefault(k, set()).add(r["v"])
            all_v.add(r["v"])
            views[k] = views.get(k, 0) + _i(r["n"])
        if "ai" in dims:
            rows = _cap({(k, ""): [views[k], len(v), None] for k, v in seen.items()}, cap)
            if rows:
                out["ai"] = rows
        if "ai_total" in dims and all_v:
            _put(out, "ai_total", "", sum(views.values()), len(all_v))

    ecom = f"site_id = {P} AND ts >= {P} AND ts < {P} AND revenue_cents IS NOT NULL"
    if "ecom" in dims:
        cur.execute(
            f"SELECT COALESCE(currency, 'NOK') AS c, COUNT(*) AS n, SUM(revenue_cents) AS rev "
            f"FROM events WHERE {ecom} GROUP BY COALESCE(currency, 'NOK')",
            a,
        )
        for r in cur.fetchall():
            _put(out, "ecom", "", r["n"], revenue=r["rev"], currency=r["c"])
    if "ecom_payment" in dims:
        cur.execute(
            f"SELECT COALESCE(currency, 'NOK') AS c, COALESCE(payment_method, 'ukjent') AS k, "
            f"COUNT(*) AS n, SUM(revenue_cents) AS rev FROM events WHERE {ecom} "
            f"GROUP BY COALESCE(currency, 'NOK'), COALESCE(payment_method, 'ukjent')",
            a,
        )
        rows = {(r["k"], r["c"]): [_i(r["n"]), None, _i(r["rev"])] for r in cur.fetchall()}
        rows = _cap(rows, cap, by=2)
        if rows:
            out["ecom_payment"] = rows
    if "ecom_source" in dims:
        # Same attribution as store.revenue_by_source: the buyer's first pageview in
        # the window, which is the same UTC day because the hash rotates at midnight.
        cur.execute(
            f"""SELECT e.revenue_cents AS rev, COALESCE(e.currency, 'NOK') AS c,
                   COALESCE(
                       (SELECT COALESCE(p.utm_source, p.referrer_src, 'direkte')
                          FROM events p
                         WHERE p.site_id = e.site_id AND p.visitor_hash = e.visitor_hash
                           AND p.name = 'pageview' AND p.ts >= {P} AND p.ts <= e.ts
                         ORDER BY p.ts LIMIT 1),
                       'direkte') AS src
              FROM events e
             WHERE e.site_id = {P} AND e.ts >= {P} AND e.ts < {P}
               AND e.revenue_cents IS NOT NULL""",
            (start, site_id, start, end),
        )
        rows: dict = {}
        for r in cur.fetchall():
            src = "direkte" if r["src"] in store._PSP_SOURCES else r["src"]
            row = rows.setdefault((src, r["c"]), [0, None, 0])
            row[0] += 1
            row[2] += _i(r["rev"])
        rows = _cap(rows, cap, by=2)
        if rows:
            out["ecom_source"] = rows
    if "product" in dims:
        cur.execute(
            f"SELECT name, COALESCE(currency, 'NOK') AS c, SUM(qty) AS q, "
            f"SUM(qty * unit_price_cents) AS rev FROM event_items "
            f"WHERE site_id = {P} AND ts >= {P} AND ts < {P} "
            f"GROUP BY name, COALESCE(currency, 'NOK')",
            a,
        )
        rows = {(r["name"], r["c"]): [_i(r["q"]), None, _i(r["rev"])] for r in cur.fetchall()}
        rows = _cap(rows, cap, by=2)
        if rows:
            out["product"] = rows

    first, last_excl = start[:10], end[:10]
    if "bot" in dims:
        cur.execute(
            f"SELECT bot_class AS c, agent AS a, SUM(n) AS n FROM bot_daily "
            f"WHERE site_id = {P} AND day >= {P} AND day < {P} GROUP BY bot_class, agent",
            (site_id, first, last_excl),
        )
        rows = {
            (json.dumps([r["c"], r["a"]], ensure_ascii=False), ""): [_i(r["n"]), None, None]
            for r in cur.fetchall()
        }
        rows = _cap(rows, cap)
        if rows:
            out["bot"] = rows
    if "bot_ai_path" in dims:
        cur.execute(
            f"SELECT path AS p, SUM(n) AS n FROM bot_daily WHERE site_id = {P} AND day >= {P} "
            f"AND day < {P} AND bot_class = 'ai' AND path <> '' GROUP BY path",
            (site_id, first, last_excl),
        )
        rows = _cap({(r["p"], ""): [_i(r["n"]), None, None] for r in cur.fetchall()}, cap)
        if rows:
            out["bot_ai_path"] = rows
    return out


def write_day(cur, site_id: int, day: str) -> int | None:
    """Recompute and replace one site's breakdowns for one UTC day, on the caller's
    cursor (the rollup's transaction). Returns rows written, or None when the day's
    source rows are gone: a day whose events were already deleted is never replaced
    with zeros, so re-running the rollup or the backfill on an old day loses nothing."""
    start, end = _day_window(day)
    cur.execute(
        f"SELECT 1 AS x FROM events WHERE site_id = {P} AND ts >= {P} AND ts < {P} LIMIT 1",
        (site_id, start, end),
    )
    has_events = cur.fetchone() is not None
    cur.execute(
        f"SELECT 1 AS x FROM bot_daily WHERE site_id = {P} AND day = {P} LIMIT 1", (site_id, day)
    )
    has_bots = cur.fetchone() is not None
    dims = (EVENT_DIMS if has_events else ()) + (BOT_DIMS if has_bots else ())
    if not dims:
        return None
    data = event_dims(cur, site_id, start, end, dims, cap=CAP)
    ph = ",".join([P] * len(dims))
    cur.execute(
        f"DELETE FROM daily_breakdowns WHERE site_id = {P} AND day = {P} AND dim IN ({ph})",
        (site_id, day, *dims),
    )
    rows = [
        (site_id, day, dim, key, currency, n, u, rev)
        for dim, keyed in data.items()
        for (key, currency), (n, u, rev) in keyed.items()
    ]
    if not rows:
        return 0
    sql = ("INSERT INTO daily_breakdowns "
           "(site_id, day, dim, key, currency, n, visitors, revenue_cents) VALUES ")
    if store._USE_PG:
        import psycopg2.extras
        psycopg2.extras.execute_values(cur, sql + "%s", rows, page_size=500)
    else:
        cur.executemany(sql + "(?, ?, ?, ?, ?, ?, ?, ?)", rows)
    return len(rows)


def compute_breakdowns(site_id: int, day: str) -> int | None:
    """write_day in a transaction of its own (retention sweep, backfill)."""
    with store._cursor() as cur:
        return write_day(cur, site_id, day)


def _source_days(cur, before: str | None = None, since: str | None = None) -> list[tuple[int, str]]:
    """(site_id, day) pairs that still have raw rows (events or bot_daily), oldest first.
    `before`/`since` bound the day ('YYYY-MM-DD', before exclusive, since inclusive)."""
    day_expr = "to_char(ts, 'YYYY-MM-DD')" if store._USE_PG else "substr(ts, 1, 10)"
    ev_where, bd_where, ev_args, bd_args = [], [], [], []
    if before:
        ev_where.append(f"ts < {P}")
        ev_args.append(f"{before} 00:00:00")
        bd_where.append(f"day < {P}")
        bd_args.append(before)
    if since:
        ev_where.append(f"ts >= {P}")
        ev_args.append(f"{since} 00:00:00")
        bd_where.append(f"day >= {P}")
        bd_args.append(since)
    ew = (" WHERE " + " AND ".join(ev_where)) if ev_where else ""
    bw = (" WHERE " + " AND ".join(bd_where)) if bd_where else ""
    pairs = set()
    cur.execute(f"SELECT DISTINCT site_id, {day_expr} AS day FROM events{ew}", tuple(ev_args))
    pairs |= {(int(r["site_id"]), str(r["day"])[:10]) for r in cur.fetchall()}
    cur.execute(f"SELECT DISTINCT site_id, day FROM bot_daily{bw}", tuple(bd_args))
    pairs |= {(int(r["site_id"]), str(r["day"])[:10]) for r in cur.fetchall()}
    return sorted(pairs, key=lambda p: (p[1], p[0]))


def days_with_breakdowns(cur, since: str, before: str) -> set[tuple[int, str]]:
    cur.execute(
        f"SELECT DISTINCT site_id, day FROM daily_breakdowns WHERE day >= {P} AND day < {P}",
        (since, before),
    )
    return {(int(r["site_id"]), str(r["day"])[:10]) for r in cur.fetchall()}


def backfill(apply: bool = False) -> dict:
    """`manage backfill-breakdowns`: compute every past day that still has raw data.

    Dry run by default (counts only). With apply=True each (site, day) is recomputed
    and replaced in its own transaction, so a re-run gives the same rows and an
    interrupted run can simply be started again. Today is left out: it is still
    being written, and tonight's rollup computes it."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with store._cursor() as cur:
        pairs = _source_days(cur, before=today)
        have = days_with_breakdowns(cur, pairs[0][1], today) if pairs else set()
    res = {
        "applied": apply,
        "site_days": len(pairs),
        "sites": len({p[0] for p in pairs}),
        "first_day": pairs[0][1] if pairs else None,
        "last_day": pairs[-1][1] if pairs else None,
        "already_present": sum(1 for p in pairs if p in have),
        "rows_written": 0,
    }
    if apply:
        for site_id, day in pairs:
            res["rows_written"] += compute_breakdowns(site_id, day) or 0
    return res


# --- reading long windows --------------------------------------------------------------

def horizon() -> str:
    """First instant still read from raw events: the start of the RAW_DAYS window."""
    return store._period_window(store.RAW_DAYS)[0]


def _split(start: str, end: str) -> tuple[tuple[str, str], tuple[str, str] | None]:
    """[start, end) -> (stored days [first, last_excl), recent raw window or None)."""
    b = horizon()
    old = (start[:10], min(end, b)[:10])
    recent = (b, end) if end > b else None
    return old, recent


def _old_kpis(cur, site_id: int, first: str, last_excl: str) -> dict[str, dict]:
    """Per-day totals for stored days: the kpi rows, else the sealed daily_rollups row
    (days rolled up before this table existed; bounces rebuilt from the rounded rate)."""
    per: dict[str, dict] = {}
    cur.execute(
        f"SELECT day, key, n FROM daily_breakdowns WHERE site_id = {P} AND dim = 'kpi' "
        f"AND day >= {P} AND day < {P}",
        (site_id, first, last_excl),
    )
    for r in cur.fetchall():
        per.setdefault(str(r["day"])[:10], {})[r["key"]] = _i(r["n"])
    cur.execute(
        f"SELECT day, pageviews, visitors, sessions, bounce_rate FROM daily_rollups "
        f"WHERE site_id = {P} AND day >= {P} AND day < {P}",
        (site_id, first, last_excl),
    )
    for r in cur.fetchall():
        d = str(r["day"])[:10]
        if d in per:
            continue
        sessions = _i(r["sessions"])
        per[d] = {
            "pageviews": _i(r["pageviews"]), "visitors": _i(r["visitors"]), "sessions": sessions,
            "bounces": round(float(r["bounce_rate"] or 0) * sessions / 100),
        }
    return per


def _stored(cur, site_id: int, first: str, last_excl: str, dims) -> dict:
    dims = [d for d in dims if d != "kpi"]
    out: dict = {}
    if not dims or first >= last_excl:
        return out
    ph = ",".join([P] * len(dims))
    cur.execute(
        f"SELECT dim, key, currency, SUM(n) AS n, SUM(visitors) AS u, SUM(revenue_cents) AS rev "
        f"FROM daily_breakdowns WHERE site_id = {P} AND day >= {P} AND day < {P} "
        f"AND dim IN ({ph}) GROUP BY dim, key, currency",
        (site_id, first, last_excl, *dims),
    )
    for r in cur.fetchall():
        _put(out, r["dim"], r["key"], r["n"], r["u"], r["rev"], r["currency"])
    return out


def _add(into: dict, more: dict) -> None:
    for dim, keyed in more.items():
        tgt = into.setdefault(dim, {})
        for k, (n, u, rev) in keyed.items():
            cur = tgt.get(k)
            if cur is None:
                tgt[k] = [n, u, rev]
            else:
                cur[0] += n
                cur[1] = None if cur[1] is None and u is None else (cur[1] or 0) + (u or 0)
                cur[2] = None if cur[2] is None and rev is None else (cur[2] or 0) + (rev or 0)


def merged(site_id: int, start: str, end: str, dims) -> dict:
    """Breakdowns for [start, end): stored days + raw events from the horizon on."""
    (first, last_excl), recent = _split(start, end)
    with store._cursor() as cur:
        out = _stored(cur, site_id, first, last_excl, dims)
        if "kpi" in dims:
            tot = {"pageviews": 0, "visitors": 0, "sessions": 0, "bounces": 0}
            if first < last_excl:
                for day in _old_kpis(cur, site_id, first, last_excl).values():
                    for k in tot:
                        tot[k] += day.get(k, 0)
            out["kpi"] = {(k, ""): [v, None, None] for k, v in tot.items()}
        if recent:
            _add(out, event_dims(cur, site_id, recent[0], recent[1], dims))
    return out


def _top(rows: dict | None, limit: int, by: int = 0, currency: str | None = None) -> list:
    """[(key, n, visitors, revenue)] sorted by the chosen column, then key."""
    items = [
        (k, n, u, rev) for (k, c), (n, u, rev) in (rows or {}).items()
        if currency is None or c == currency
    ]
    items.sort(key=lambda t: (-(t[1 + by] or 0), t[0]))
    return items[: int(limit)]


def _kpi(d: dict) -> dict:
    k = {key: v[0] for (key, _), v in d.get("kpi", {}).items()}
    return {x: k.get(x, 0) for x in ("pageviews", "visitors", "sessions", "bounces")}


def kpis(site_id: int, start: str, end: str) -> dict:
    k = _kpi(merged(site_id, start, end, ["kpi"]))
    s = k["sessions"]
    return {
        "pageviews": k["pageviews"], "visitors": k["visitors"], "sessions": s,
        "bounce_rate": round(k["bounces"] / s * 100) if s else 0,
    }


def stats(site_id: int, start: str, end: str) -> dict:
    d = merged(site_id, start, end, ["kpi", *_PV_DIMS, "campaign"])
    k = _kpi(d)
    s = k["sessions"]

    def kn(dim, name):
        return [{name: key, "n": n} for key, n, _, _ in _top(d.get(dim), 10)]

    campaigns = []
    for key, n, u, _ in _top(d.get("campaign"), 10):
        src, med, camp = json.loads(key)
        campaigns.append({"source": src, "medium": med, "campaign": camp, "n": n, "visitors": u or 0})
    return {
        "pageviews": k["pageviews"],
        "visitors": k["visitors"],
        "sessions": s,
        "bounce_rate": round(k["bounces"] / s * 100) if s else 0,
        "views_per_session": round(k["pageviews"] / s, 1) if s else 0.0,
        "top_paths": kn("page", "path"),
        "top_sources": kn("source", "src"),
        "campaigns": campaigns,
        "devices": kn("device", "k"),
        "browsers": kn("browser", "k"),
        "os": kn("os", "k"),
        "countries": kn("country", "k"),
        "regions": kn("region", "k"),
    }


def daily_series(site_id: int, days: int) -> list[dict]:
    """One bucket per day for the last `days` days: stored per-day totals before the
    horizon, raw pageviews after. Pageviews only, like the KPI numbers."""
    start, end = store._period_window(days)
    (first, last_excl), recent = _split(start, end)
    found: dict[str, tuple[int, int]] = {}
    with store._cursor() as cur:
        for day, k in _old_kpis(cur, site_id, first, last_excl).items():
            found[day] = (k.get("pageviews", 0), k.get("visitors", 0))
        if recent:
            b = store._bucket_expr("day")
            cur.execute(
                f"SELECT {b} AS bucket, COUNT(*) AS pv, COUNT(DISTINCT visitor_hash) AS vis "
                f"FROM events WHERE site_id = {P} AND ts >= {P} AND ts < {P} "
                f"AND name = 'pageview' GROUP BY bucket",
                (site_id, *recent),
            )
            for r in cur.fetchall():
                found[str(r["bucket"])] = (_i(r["pv"]), _i(r["vis"]))
    return [
        {"bucket": lbl, "pageviews": found.get(lbl, (0, 0))[0], "visitors": found.get(lbl, (0, 0))[1]}
        for lbl in store._bucket_labels(days, "day")
    ]


def flow_stats(site_id: int, start: str, end: str) -> dict:
    d = merged(site_id, start, end, ["entry", "exit"])
    return {
        "entries": [{"path": k, "n": n} for k, n, _, _ in _top(d.get("entry"), 10)],
        "exits": [{"path": k, "n": n} for k, n, _, _ in _top(d.get("exit"), 10)],
    }


def goal_stats(site_id: int, start: str, end: str, goals: list[dict]) -> list[dict]:
    """Completions = distinct visitors per day of the goal's event or page, summed."""
    (first, last_excl), recent = _split(start, end)
    want_ev = sorted({g["match_value"] for g in goals if g["match_type"] == "event"})
    want_pg = sorted({g["match_value"] for g in goals if g["match_type"] != "event"})
    got: dict[tuple[str, str], int] = {}
    with store._cursor() as cur:
        total = sum(k.get("visitors", 0) for k in _old_kpis(cur, site_id, first, last_excl).values())
        for dim, keys in (("event", want_ev), ("page", want_pg)):
            if not keys or first >= last_excl:
                continue
            ph = ",".join([P] * len(keys))
            cur.execute(
                f"SELECT key, SUM(visitors) AS u FROM daily_breakdowns WHERE site_id = {P} "
                f"AND day >= {P} AND day < {P} AND dim = {P} AND key IN ({ph}) GROUP BY key",
                (site_id, first, last_excl, dim, *keys),
            )
            for r in cur.fetchall():
                got[(dim, r["key"])] = got.get((dim, r["key"]), 0) + _i(r["u"])
        if recent:
            base = f"site_id = {P} AND ts >= {P} AND ts < {P}"
            a = (site_id, *recent)
            cur.execute(
                f"SELECT COUNT(DISTINCT visitor_hash) AS n FROM events "
                f"WHERE {base} AND name = 'pageview'", a,
            )
            total += _i(cur.fetchone()["n"])
            for dim, keys, cond in (("event", want_ev, "name = {P}"),
                                    ("page", want_pg, "name = 'pageview' AND path = {P}")):
                for v in keys:
                    cur.execute(
                        f"SELECT COUNT(DISTINCT visitor_hash) AS n FROM events "
                        f"WHERE {base} AND {cond.format(P=P)}", (*a, v),
                    )
                    got[(dim, v)] = got.get((dim, v), 0) + _i(cur.fetchone()["n"])
    out = []
    for g in goals:
        dim = "event" if g["match_type"] == "event" else "page"
        comp = got.get((dim, g["match_value"]), 0)
        out.append({**g, "completions": comp,
                    "rate": round(comp / total * 100, 1) if total else 0.0})
    return out


def top_events(site_id: int, start: str, end: str) -> list[dict]:
    d = merged(site_id, start, end, ["event"])
    return [{"k": k, "n": n, "u": u or 0} for k, n, u, _ in _top(d.get("event"), 10)]


def ecommerce_stats(site_id: int, start: str, end: str) -> dict:
    d = merged(site_id, start, end, ["ecom"])
    by_currency = sorted(
        ({"currency": c, "orders": n, "revenue_cents": rev or 0}
         for (_, c), (n, _, rev) in d.get("ecom", {}).items()),
        key=lambda r: (-r["orders"], r["currency"]),
    )
    return {"orders": sum(r["orders"] for r in by_currency), "by_currency": by_currency}


def top_products(site_id: int, start: str, end: str, currency: str, limit: int) -> list[dict]:
    d = merged(site_id, start, end, ["product"])
    return [{"name": k, "qty": n, "revenue_cents": rev or 0}
            for k, n, _, rev in _top(d.get("product"), limit, by=2, currency=currency)]


def revenue_by_source(site_id: int, start: str, end: str, currency: str, limit: int) -> list[dict]:
    d = merged(site_id, start, end, ["ecom_source"])
    return [{"src": k, "orders": n, "revenue_cents": rev or 0}
            for k, n, _, rev in _top(d.get("ecom_source"), limit, by=2, currency=currency)]


def revenue_by_payment(site_id: int, start: str, end: str, currency: str, limit: int) -> list[dict]:
    d = merged(site_id, start, end, ["ecom_payment"])
    return [{"method": k, "orders": n, "revenue_cents": rev or 0}
            for k, n, _, rev in _top(d.get("ecom_payment"), limit, by=2, currency=currency)]


def has_ecommerce(site_id: int) -> bool:
    with store._cursor() as cur:
        cur.execute(
            f"SELECT 1 AS x FROM daily_breakdowns WHERE site_id = {P} AND dim = 'ecom' LIMIT 1",
            (site_id,),
        )
        return cur.fetchone() is not None


def api_breakdown(site_id: int, start: str, end: str, prop: str, limit: int) -> list[dict]:
    dim = API_DIMS[prop]
    d = merged(site_id, start, end, [dim])
    return [{"value": k, "pageviews": n, "visitors": u or 0} for k, n, u, _ in _top(d.get(dim), limit)]


def ai_referrals(site_id: int, start: str, end: str) -> dict:
    d = merged(site_id, start, end, ["ai_total"])
    n, u, _ = d.get("ai_total", {}).get(("", ""), [0, 0, None])
    return {"visitors": u or 0, "views": n}


def ai_referral_sources(site_id: int, start: str, end: str, limit: int) -> list[dict]:
    d = merged(site_id, start, end, ["ai"])
    out = [{"k": k, "u": u or 0, "n": n} for (k, _), (n, u, _) in d.get("ai", {}).items()]
    out.sort(key=lambda a: (-a["u"], -a["n"], a["k"]))
    return out[: int(limit)]


def bot_summary(site_id: int, start: str, end: str, limit: int) -> dict:
    d = merged(site_id, start, end, ["bot", "bot_ai_path"])
    classes: dict[str, int] = {}
    agents = []
    for (key, _), (n, _, _) in d.get("bot", {}).items():
        cls, agent = json.loads(key)
        classes[cls] = classes.get(cls, 0) + n
        agents.append({"agent": agent, "class": cls, "n": n})
    agents.sort(key=lambda a: (-a["n"], a["agent"]))
    cls_list = sorted(({"class": c, "n": n} for c, n in classes.items()),
                      key=lambda c: (-c["n"], c["class"]))
    return {
        "total": sum(classes.values()),
        "classes": cls_list,
        "agents": agents[: int(limit)],
        "ai_paths": [{"path": k, "n": n} for k, n, _, _ in _top(d.get("bot_ai_path"), limit)],
    }
