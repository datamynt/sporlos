"""Full data export: everything we store about a site (or a whole account) as a ZIP of CSV files.

A customer owns their data. This is the answer to "give me everything": one ZIP per site,
or one for the whole account, with semicolon-separated UTF-8 CSV files that Norwegian
Excel opens directly (BOM, decimal comma), and a README.txt that explains each file.

What is NOT in it, on purpose:
  - visitor_hash. It is our join key (a daily-salted one-way hash); the customer gets the
    pseudonymous event rows without it. session_id is left out for the same reason.
  - password hashes, API keys, the Search Console token, other customers' anything.

Size-safe: the ZIP is produced on the fly (data descriptors, no seek), the big tables are
read in keyset pages of PAGE rows and no database connection is held while the client
downloads. Memory use does not grow with the size of the site.

Signed links (for the warning mail): a login is not needed to download, because a
customer whose plan ended may not remember how they logged in. The link carries the account id
and an expiry, signed with the app secret (auth.sign_token), and is a GET that only shows a
button; the download itself is a POST, so a mail scanner that opens links does not
start a big export.
"""

from __future__ import annotations

import csv
import io
import json
import threading
import zipfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Callable, Iterable, Iterator

from app import auth, store
from app.geo import country_no

P = store.P
PAGE = 5000  # rows per database round trip for the big tables
FLUSH_EVERY = 500  # rows per chunk handed to the client

LINK_PURPOSE = "export"

# One export at a time per slot: an export reads a lot of rows, and it is served by the
# same process that takes the measurements.
_SLOTS = threading.BoundedSemaphore(2)


# --- Cell formatting ----------------------------------------------------------------

def csv_cell(v):
    """Neutralise spreadsheet formulas: a text cell starting with = + - @ (or tab/CR) is
    executed by Excel. Paths and sources come from a public endpoint."""
    if isinstance(v, str) and v[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + v
    return v


def _num(v: float | Decimal) -> str:
    if isinstance(v, Decimal):
        v = float(v)
    if v == int(v):
        return str(int(v))
    return f"{v:.4f}".rstrip("0").rstrip(".").replace(".", ",")  # decimal comma for Excel


def fmt(v) -> str:
    if v is None:
        return ""
    if isinstance(v, bool):
        return "ja" if v else "nei"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, (float, Decimal)):
        return _num(v)
    if isinstance(v, datetime):
        if v.tzinfo:
            v = v.astimezone(timezone.utc)
        return v.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(v, date):
        return v.isoformat()
    return csv_cell(str(v))


def money(cents) -> str:
    """Whole minor units (øre/cent) as an amount with a decimal comma."""
    c = int(cents or 0)
    sign = "-" if c < 0 else ""
    c = abs(c)
    return f"{sign}{c // 100},{c % 100:02d}"


def _day(v) -> str:
    return v.isoformat() if isinstance(v, date) and not isinstance(v, datetime) else str(v)[:10]


# --- Reading the database -----------------------------------------------------------

def _rows(sql: str, args: tuple = ()) -> list[dict]:
    with store._cursor() as cur:
        cur.execute(sql, args)
        return [dict(r) for r in cur.fetchall()]


def _paged(table: str, cols: list[str], keys: list[str], where: str, args: tuple) -> Iterator[dict]:
    """All rows of a table matching `where`, in the order of `keys`, PAGE rows per query
    (keyset pagination: no OFFSET, and no connection held between pages). `keys` must be
    a unique key of the matching rows."""
    last = None
    select = ", ".join(cols)
    order = ", ".join(keys)
    while True:
        cond, extra = "", ()
        if last is not None:
            parts, vals = [], []
            for i, k in enumerate(keys):
                eq = [f"{keys[j]} = {P}" for j in range(i)]
                parts.append("(" + " AND ".join(eq + [f"{k} > {P}"]) + ")")
                vals += [last[keys[j]] for j in range(i)] + [last[k]]
            # The first key >= its last value keeps the query on the index range.
            cond = f" AND {keys[0]} >= {P} AND ({' OR '.join(parts)})"
            extra = (last[keys[0]], *vals)
        page = _rows(f"SELECT {select} FROM {table} WHERE {where}{cond} ORDER BY {order} LIMIT {PAGE}",
                     (*args, *extra))
        yield from page
        if len(page) < PAGE:
            return
        last = page[-1]


def _table_exists(name: str) -> bool:
    with store._cursor() as cur:
        return store._table_exists(cur, name)


# --- The files of one site -----------------------------------------------------------

# (file name, what it holds). The README is generated from this list, and a test checks
# that every file in the ZIP is described here and the other way round.
SITE_FILES: list[tuple[str, str]] = [
    ("nettsted.csv", "Nettstedet: domene, nettsted-ID og når det ble opprettet."),
    ("tidsserie.csv", "Sidevisninger og unike besøkende per dag, fra de rå hendelsene som fortsatt er lagret (høyst 90 dager)."),
    ("daglige_tall.csv", "Dagstall for HELE historikken: sidevisninger, unike, økter og fluktfrekvens per dag, med fingeravtrykk (rollup_hash) og BSV-forankring (txid, merkle_root, merkle_proof). Se under «Bevis»."),
    ("sider.csv", "Sider: sidevisninger og unike besøkende per side (rå hendelser, høyst 90 dager)."),
    ("inngangssider.csv", "Inngangssider: første side i en økt, med antall økter."),
    ("utgangssider.csv", "Utgangssider: siste side i en økt, med antall økter."),
    ("kilder.csv", "Trafikkilder (henvisende nettsted, «direkte» når ingen)."),
    ("land.csv", "Land."),
    ("regioner.csv", "Fylke/region."),
    ("enheter.csv", "Enhetstype (desktop, mobil, nettbrett)."),
    ("nettlesere.csv", "Nettlesere."),
    ("operativsystemer.csv", "Operativsystemer."),
    ("kampanjer.csv", "UTM-kampanjer (kilde, medium, kampanje)."),
    ("egendefinerte_hendelser.csv", "Egendefinerte hendelser (alt som ikke er sidevisning): antall og unike besøkende."),
    ("maal.csv", "Målene dine, med antall unike som fullførte dem og konverteringsrate."),
    ("trakter.csv", "Traktene dine: hvert steg, og hvor mange besøkende som nådde det."),
    ("ehandel_oppsummering.csv", "E-handel: ordrer og omsetning per valuta."),
    ("ehandel_kilder.csv", "E-handel: ordrer og omsetning per trafikkilde."),
    ("ehandel_betalingsmaater.csv", "E-handel: ordrer og omsetning per betalingsmåte."),
    ("ehandel_produkter.csv", "E-handel: solgt antall og omsetning per produkt."),
    ("roboter.csv", "Roboter og KI-agenter, talt for seg (aldri blandet med besøkende): dag, klasse, agent, side, antall."),
    ("sokestatistikk.csv", "Søkedata fra Google Search Console og Bing, hvis du har koblet dem til: klikk, visninger og posisjon."),
    ("forankringer.csv", "Forankringer av dagstallene i BSV-kjeden (kun fingeravtrykk, aldri data), der vi har dem."),
    ("raadata_hendelser.csv", "De rå hendelsene som fortsatt er lagret (høyst 90 dager): tidspunkt (UTC), navn, side, kilde, land, region, enhet, nettleser, OS, UTM og ev. omsetning. UTEN besøkerens fingeravtrykk (visitor_hash)."),
    ("raadata_varelinjer.csv", "Varelinjene til kjøpene i raadata_hendelser.csv (koble med hendelse_id)."),
]


def _site_row(site: dict) -> list[dict]:
    return _rows(f"SELECT domain, public_id, created_at, public_dash FROM sites WHERE id = {P}", (site["id"],))


class SiteExport:
    """Produces the rows of every file for one site. Each method returns (header, rows)."""

    def __init__(self, site: dict):
        self.site = site
        self.sid = int(site["id"])
        self._flow = None

    # -- small aggregates: SQL does the grouping, the result is one row per distinct value
    def _group(self, expr: str, header: str, extra_where: str = "") -> tuple[list, Iterable]:
        rows = _rows(
            f"SELECT {expr} AS k, COUNT(*) AS n, COUNT(DISTINCT visitor_hash) AS u FROM events "
            f"WHERE site_id = {P} AND name = 'pageview'{extra_where} GROUP BY k ORDER BY n DESC, k",
            (self.sid,),
        )
        return [header, "sidevisninger", "unike_besoekende"], ([r["k"], r["n"], r["u"]] for r in rows)

    def nettsted(self):
        r = _site_row(self.site)[0]
        return (["domene", "nettsted_id", "opprettet", "offentlig_dashboard"],
                [[r["domain"], r["public_id"], r["created_at"], bool(r["public_dash"])]])

    def tidsserie(self):
        day = "to_char(ts, 'YYYY-MM-DD')" if store._USE_PG else "substr(ts, 1, 10)"
        rows = _rows(
            f"SELECT {day} AS d, COUNT(*) AS n, COUNT(DISTINCT visitor_hash) AS u FROM events "
            f"WHERE site_id = {P} AND name = 'pageview' GROUP BY d ORDER BY d", (self.sid,))
        return ["dag", "sidevisninger", "unike_besoekende"], ([r["d"], r["n"], r["u"]] for r in rows)

    def daglige_tall(self):
        cols = ["day", "pageviews", "visitors", "sessions", "bounce_rate", "rollup_hash",
                "merkle_root", "merkle_proof", "txid", "anchored_at"]
        header = ["dag", "sidevisninger", "unike_besoekende", "okter", "fluktfrekvens_prosent",
                  "rollup_hash", "merkle_root", "merkle_proof", "txid", "forankret"]
        rows = _paged("daily_rollups", cols, ["day"], f"site_id = {P}", (self.sid,))
        return header, ([_day(r["day"])] + [r[c] for c in cols[1:]] for r in rows)

    def sider(self):
        return self._group("path", "side")

    def kilder(self):
        return self._group("COALESCE(referrer_src, 'direkte')", "kilde", store._NOT_SELF_REFERRAL)

    def land(self):
        header, rows = self._group("COALESCE(country, 'ukjent')", "land")
        return ["land", "lagret_verdi", "sidevisninger", "unike_besoekende"], (
            [country_no(r[0]), r[0], r[1], r[2]] for r in rows)

    def regioner(self):
        return self._group("COALESCE(region, 'ukjent')", "region")

    def enheter(self):
        return self._group("COALESCE(device, 'ukjent')", "enhet")

    def nettlesere(self):
        return self._group("COALESCE(browser, 'ukjent')", "nettleser")

    def operativsystemer(self):
        return self._group("COALESCE(os, 'ukjent')", "operativsystem")

    def kampanjer(self):
        rows = _rows(
            "SELECT COALESCE(utm_source,'') AS s, COALESCE(utm_medium,'') AS m, "
            "COALESCE(utm_campaign,'') AS c, COUNT(*) AS n, COUNT(DISTINCT visitor_hash) AS u "
            f"FROM events WHERE site_id = {P} AND name = 'pageview' "
            "AND (utm_source IS NOT NULL OR utm_campaign IS NOT NULL) "
            "GROUP BY s, m, c ORDER BY n DESC, s, m, c", (self.sid,))
        return (["utm_source", "utm_medium", "utm_campaign", "sidevisninger", "unike_besoekende"],
                ([r["s"], r["m"], r["c"], r["n"], r["u"]] for r in rows))

    def egendefinerte_hendelser(self):
        rows = _rows(
            f"SELECT name AS k, COUNT(*) AS n, COUNT(DISTINCT visitor_hash) AS u FROM events "
            f"WHERE site_id = {P} AND name <> 'pageview' GROUP BY name ORDER BY n DESC, name", (self.sid,))
        return ["hendelse", "antall", "unike_besoekende"], ([r["k"], r["n"], r["u"]] for r in rows)

    # -- sessions and funnels: one pass per day. The visitor hash rotates every UTC day, so
    # a session never spans midnight and a day is the largest unit that has to fit in memory.
    def _days(self) -> list[str]:
        day = "to_char(ts, 'YYYY-MM-DD')" if store._USE_PG else "substr(ts, 1, 10)"
        return [r["d"] for r in _rows(
            f"SELECT DISTINCT {day} AS d FROM events WHERE site_id = {P} ORDER BY d", (self.sid,))]

    def _flow_and_funnels(self) -> dict:
        if self._flow is not None:
            return self._flow
        funnels = []
        for f in _rows(f"SELECT id, name, steps FROM funnels WHERE site_id = {P} ORDER BY id", (self.sid,)):
            try:
                steps = json.loads(f["steps"])
            except ValueError:
                steps = []
            funnels.append({"name": f["name"], "steps": steps, "counts": [0] * len(steps)})
        entries: dict[str, int] = {}
        exits: dict[str, int] = {}
        for d in self._days():
            start = f"{d} 00:00:00"
            end = (datetime.strptime(d, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
            evs = _rows(
                f"SELECT visitor_hash, ts, name, path FROM events WHERE site_id = {P} "
                f"AND ts >= {P} AND ts < {P} ORDER BY visitor_hash, ts", (self.sid, start, end))
            by_visitor: dict[str, list[dict]] = {}
            for e in evs:
                by_visitor.setdefault(e["visitor_hash"], []).append(e)
            for seq in by_visitor.values():
                last_path, prev_e = None, 0.0
                for e in seq:
                    if e["name"] != "pageview":
                        continue
                    t = store._epoch(e["ts"])
                    if last_path is None or (t - prev_e) > store._SESSION_GAP:
                        if last_path is not None:
                            exits[last_path] = exits.get(last_path, 0) + 1
                        entries[e["path"]] = entries.get(e["path"], 0) + 1
                    last_path, prev_e = e["path"], t
                if last_path is not None:
                    exits[last_path] = exits.get(last_path, 0) + 1
                for f in funnels:
                    idx = 0
                    for e in seq:
                        if idx >= len(f["steps"]):
                            break
                        st = f["steps"][idx]
                        hit = (e["name"] == st.get("value")) if st.get("type") == "event" else (
                            e["name"] == "pageview" and e["path"] == st.get("value"))
                        if hit:
                            idx += 1
                    for i in range(idx):
                        f["counts"][i] += 1
        self._flow = {"entries": entries, "exits": exits, "funnels": funnels}
        return self._flow

    def inngangssider(self):
        d = self._flow_and_funnels()["entries"]
        return ["side", "okter"], ([k, n] for k, n in sorted(d.items(), key=lambda x: (-x[1], x[0])))

    def utgangssider(self):
        d = self._flow_and_funnels()["exits"]
        return ["side", "okter"], ([k, n] for k, n in sorted(d.items(), key=lambda x: (-x[1], x[0])))

    def trakter(self):
        rows = []
        for f in self._flow_and_funnels()["funnels"]:
            base = f["counts"][0] if f["counts"] else 0
            for i, st in enumerate(f["steps"]):
                n = f["counts"][i]
                rows.append([f["name"], i + 1, st.get("type"), st.get("value"), n,
                             round(n / base * 100, 1) if base else 0])
        return ["trakt", "steg", "type", "verdi", "besoekende", "andel_av_foerste_steg_prosent"], rows

    def maal(self):
        total = _rows(f"SELECT COUNT(DISTINCT visitor_hash) AS n FROM events WHERE site_id = {P} "
                      "AND name = 'pageview'", (self.sid,))[0]["n"] or 0
        out = []
        for g in _rows(f"SELECT id, name, match_type, match_value, created_at FROM goals "
                       f"WHERE site_id = {P} ORDER BY id", (self.sid,)):
            if g["match_type"] == "event":
                cond = "name = " + P
            else:
                cond = "name = 'pageview' AND path = " + P
            n = _rows(f"SELECT COUNT(DISTINCT visitor_hash) AS n FROM events WHERE site_id = {P} "
                      f"AND {cond}", (self.sid, g["match_value"]))[0]["n"] or 0
            out.append([g["name"], g["match_type"], g["match_value"], g["created_at"], n,
                        round(n / total * 100, 1) if total else 0])
        return (["maal", "type", "verdi", "opprettet", "unike_fullforinger", "konverteringsrate_prosent"], out)

    # -- e-commerce
    def _orders(self) -> list[dict]:
        """One row per order with the source of the visitor's first pageview before it.
        The hash rotates daily, so this is same-day attribution (as in the dashboard)."""
        return _rows(
            f"""SELECT e.revenue_cents AS rev, COALESCE(e.currency, 'NOK') AS cur,
                   COALESCE(e.payment_method, 'ukjent') AS pay,
                   COALESCE(
                       (SELECT COALESCE(p.utm_source, p.referrer_src, 'direkte')
                          FROM events p
                         WHERE p.site_id = e.site_id AND p.visitor_hash = e.visitor_hash
                           AND p.name = 'pageview' AND p.ts <= e.ts
                         ORDER BY p.ts LIMIT 1), 'direkte') AS src
                  FROM events e WHERE e.site_id = {P} AND e.revenue_cents IS NOT NULL""", (self.sid,))

    def _ecom(self) -> dict:
        orders = self._orders()
        psp = store._PSP_SOURCES
        agg: dict[str, dict] = {"cur": {}, "src": {}, "pay": {}}
        for o in orders:
            rev = int(o["rev"])
            for kind, key in (("cur", (o["cur"],)),
                              ("src", (o["cur"], "direkte" if o["src"] in psp else o["src"])),
                              ("pay", (o["cur"], o["pay"]))):
                a = agg[kind].setdefault(key, [0, 0])
                a[0] += 1
                a[1] += rev
        return agg

    def ehandel_oppsummering(self):
        a = self._ecom()["cur"]
        return (["valuta", "ordrer", "omsetning"],
                [[k[0], v[0], money(v[1])] for k, v in sorted(a.items(), key=lambda x: (-x[1][0], x[0]))])

    def ehandel_kilder(self):
        a = self._ecom()["src"]
        return (["valuta", "kilde", "ordrer", "omsetning"],
                [[k[0], k[1], v[0], money(v[1])] for k, v in sorted(a.items(), key=lambda x: (x[0][0], -x[1][1], x[0][1]))])

    def ehandel_betalingsmaater(self):
        a = self._ecom()["pay"]
        return (["valuta", "betalingsmaate", "ordrer", "omsetning"],
                [[k[0], k[1], v[0], money(v[1])] for k, v in sorted(a.items(), key=lambda x: (x[0][0], -x[1][1], x[0][1]))])

    def ehandel_produkter(self):
        rows = _rows(
            f"SELECT COALESCE(currency, 'NOK') AS cur, name, SUM(qty) AS qty, "
            f"SUM(qty * unit_price_cents) AS rev FROM event_items WHERE site_id = {P} "
            "GROUP BY COALESCE(currency, 'NOK'), name ORDER BY cur, rev DESC, name", (self.sid,))
        return (["valuta", "produkt", "antall", "omsetning"],
                ([r["cur"], r["name"], int(r["qty"] or 0), money(r["rev"])] for r in rows))

    # -- the rest of the tables
    def roboter(self):
        cols = ["day", "bot_class", "agent", "path", "n"]
        rows = _paged("bot_daily", cols, ["day", "bot_class", "agent", "path"], f"site_id = {P}", (self.sid,))
        return (["dag", "klasse", "agent", "side", "antall"],
                ([_day(r["day"]), r["bot_class"], r["agent"], r["path"], r["n"]] for r in rows))

    def sokestatistikk(self):
        if not _table_exists("search_stats"):
            return ["dag", "kilde", "dimensjon", "noekkel", "klikk", "visninger", "posisjon"], []
        cols = ["day", "source", "dim", "key", "clicks", "impressions", "position"]
        rows = _paged("search_stats", cols, ["day", "source", "dim", "key"], f"site_id = {P}", (self.sid,))
        return (["dag", "kilde", "dimensjon", "noekkel", "klikk", "visninger", "posisjon"],
                ([_day(r["day"])] + [r[c] for c in cols[1:]] for r in rows))

    def forankringer(self):
        header = ["periode", "rollup_hash", "txid", "forankret"]
        if not _table_exists("anchors"):
            return header, []
        period = "period::text" if store._USE_PG else "period"
        rows = _rows(f"SELECT {period} AS period, rollup_hash, txid, anchored_at FROM anchors "
                     f"WHERE site_id = {P} ORDER BY id", (self.sid,))
        return header, ([r["period"], r["rollup_hash"], r["txid"], r["anchored_at"]] for r in rows)

    def raadata_hendelser(self):
        cols = ["id", "ts", "name", "path", "referrer_src", "country", "region", "device", "browser",
                "os", "utm_source", "utm_medium", "utm_campaign", "revenue_cents", "currency",
                "payment_method"]
        header = ["hendelse_id", "tidspunkt_utc", "hendelse", "side", "kilde", "land", "region", "enhet",
                  "nettleser", "operativsystem", "utm_source", "utm_medium", "utm_campaign",
                  "omsetning_minste_enhet", "valuta", "betalingsmaate"]
        rows = _paged("events", cols, ["ts", "id"], f"site_id = {P}", (self.sid,))
        return header, ([r[c] for c in cols] for r in rows)

    def raadata_varelinjer(self):
        cols = ["id", "event_id", "ts", "name", "qty", "unit_price_cents", "currency"]
        header = ["varelinje_id", "hendelse_id", "tidspunkt_utc", "produkt", "antall",
                  "enhetspris_minste_enhet", "valuta"]
        rows = _paged("event_items", cols, ["ts", "id"], f"site_id = {P}", (self.sid,))
        return header, ([r[c] for c in cols] for r in rows)


def site_entries(site: dict, prefix: str = "") -> list[tuple[str, Callable]]:
    """(path in the ZIP, producer of (header, rows)) for every file of a site."""
    ex = SiteExport(site)
    return [(prefix + name, getattr(ex, name[:-4])) for name, _ in SITE_FILES]


# --- Account level ---------------------------------------------------------------------

def _account_files(tenant_id: int) -> list[tuple[str, Callable]]:
    def konto():
        t = _rows(f"SELECT name, plan, trial_ends_at, plan_ends_at, cancelled_at, created_at "
                  f"FROM tenants WHERE id = {P}", (tenant_id,))[0]
        return (["firmanavn", "plan", "proveperiode_slutt", "abonnement_gjelder_til", "avsluttet",
                 "opprettet"],
                [[t["name"], t["plan"], t["trial_ends_at"], t["plan_ends_at"], t["cancelled_at"],
                  t["created_at"]]])

    def brukere():
        rows = _rows(f"SELECT email, email_verified, created_at FROM users WHERE tenant_id = {P} ORDER BY id",
                     (tenant_id,))
        return (["epost", "bekreftet", "opprettet"],
                ([r["email"], bool(r["email_verified"]), r["created_at"]] for r in rows))

    return [("konto.csv", konto), ("brukere.csv", brukere)]


ACCOUNT_FILES: list[tuple[str, str]] = [
    ("konto.csv", "Kontoen: firmanavn, plan, prøveperiode og når den ble opprettet."),
    ("brukere.csv", "Brukerne på kontoen (e-postadresse, bekreftet, opprettet). Passord er aldri med."),
]


def _folder_names(sites: list[dict]) -> dict[int, str]:
    """A folder name per site: the domain with unsafe characters replaced, unique."""
    out, used = {}, set()
    for s in sites:
        base = "".join(c if c.isalnum() or c in "._-" else "_" for c in s["domain"]).strip("._") or "nettsted"
        name, i = base, 2
        while name.lower() in used:
            name, i = f"{base}-{i}", i + 1
        used.add(name.lower())
        out[int(s["id"])] = name
    return out


def readme_text(scope: str, label: str, now: datetime, site_folders: dict[str, str] | None = None) -> str:
    files = SITE_FILES
    lines = [
        "SPORLØS - ALLE DATA",
        "",
        f"{'Konto' if scope == 'account' else 'Nettsted'}: {label}",
        f"Laget: {now.strftime('%Y-%m-%d %H:%M')} UTC",
        "",
        "Dette er alt Sporløs har lagret om "
        + ("kontoen din og nettstedene på den." if scope == "account" else "nettstedet ditt."),
        "",
        "Slik åpner du filene",
        "- Filene er CSV med semikolon mellom kolonnene og UTF-8 (æøå). Norsk Excel åpner dem direkte.",
        "- Desimaltall har komma. Omsetning er oppgitt i hele valutaenheter (123,45), og i de rå",
        "  filene i minste enhet (øre/cent: 12345).",
        "- Alle tidspunkter og dager er UTC.",
        "- En celle som ville startet med = + - eller @ har fått en apostrof foran, så regnearket ikke",
        "  kjører den som formel.",
        "",
        "Hva som er med, og hva som ikke er det",
        "- Rå hendelser lagres i 90 dager. Eldre rader er slått sammen til dagstall, og de ligger i",
        "  daglige_tall.csv for hele historikken. Fordelingene (sider, kilder, land osv.) er regnet ut",
        "  fra de rå hendelsene, så de dekker de siste 90 dagene.",
        "- Besøkerens fingeravtrykk (visitor_hash) er IKKE med. Det er en enveis-hash som skifter hver",
        "  dag og som vi bruker for å telle unike besøkende. Du får hendelsene uten den.",
        "- Passord, API-nøkler og tilkoblinger til Google-kontoen din er ikke med.",
        "",
    ]
    if scope == "account":
        lines += ["Filer på kontonivå"]
        lines += [f"- {n}: {d}" for n, d in ACCOUNT_FILES]
        lines += ["", "Nettsteder: én mappe per nettsted, med filene under."]
        for pid, folder in (site_folders or {}).items():
            lines.append(f"- {folder}/  ({pid})")
        lines.append("")
    lines += ["Filer for hvert nettsted"]
    lines += [f"- {n}: {d}" for n, d in files]
    lines += [
        "",
        "Bevis (daglige_tall.csv)",
        "Hver dag får et fingeravtrykk (rollup_hash, sha256 av dagstallene). Fingeravtrykkene samles i et",
        "Merkle-tre, og roten (merkle_root) forankres i en transaksjon på BSV-kjeden (txid). Kjeden",
        "inneholder aldri data, bare fingeravtrykket. Med merkle_proof og txid kan du selv vise at",
        "tallene ikke er endret etter forankringen. Se https://sporlos.no/proof.",
        "",
        "Spørsmål om dataene? Skriv til post@sporlos.no.",
        "",
    ]
    return "\n".join(lines)


# --- Streaming the ZIP -------------------------------------------------------------------

class _Sink:
    """Write-only target for zipfile that collects what was written, so the generator can
    hand it to the client in chunks. No seek: zipfile then writes data descriptors."""

    def __init__(self):
        self._chunks: list[bytes] = []
        self._pos = 0

    def write(self, b) -> int:
        b = bytes(b)
        self._chunks.append(b)
        self._pos += len(b)
        return len(b)

    def tell(self) -> int:
        return self._pos

    def flush(self) -> None:
        pass

    def drain(self) -> bytes:
        out = b"".join(self._chunks)
        self._chunks.clear()
        return out


def stream_zip(entries: list[tuple[str, object]], now: datetime) -> Iterator[bytes]:
    """The ZIP as chunks. An entry is (path, text) for a text file or (path, producer)
    where producer() returns (header, rows) for a CSV file."""
    sink = _Sink()
    zf = zipfile.ZipFile(sink, "w")
    stamp = (now.year, now.month, now.day, now.hour, now.minute, now.second)
    try:
        for path, content in entries:
            zi = zipfile.ZipInfo(path, stamp)
            zi.compress_type = zipfile.ZIP_DEFLATED
            zi.external_attr = 0o644 << 16
            with zf.open(zi, "w", force_zip64=True) as f:
                if isinstance(content, str):
                    f.write(content.encode("utf-8"))
                else:
                    header, rows = content()
                    f.write(b"\xef\xbb\xbf")  # BOM: Excel reads æøå correctly
                    buf = io.StringIO()
                    w = csv.writer(buf, delimiter=";")
                    w.writerow(header)
                    n = 0
                    for row in rows:
                        w.writerow([fmt(c) for c in row])
                        n += 1
                        if n % FLUSH_EVERY == 0:
                            f.write(buf.getvalue().encode("utf-8"))
                            buf.seek(0)
                            buf.truncate()
                            chunk = sink.drain()
                            if chunk:
                                yield chunk
                    f.write(buf.getvalue().encode("utf-8"))
            chunk = sink.drain()
            if chunk:
                yield chunk
        zf.close()
        chunk = sink.drain()
        if chunk:
            yield chunk
    finally:
        sink.drain()


def site_zip(site: dict, now: datetime | None = None) -> Iterator[bytes]:
    now = now or datetime.now(timezone.utc)
    entries: list[tuple[str, object]] = [("README.txt", readme_text("site", site["domain"], now))]
    entries += site_entries(site)
    return stream_zip(entries, now)


def account_zip(tenant_id: int, now: datetime | None = None) -> Iterator[bytes]:
    now = now or datetime.now(timezone.utc)
    tenant = store.get_tenant(tenant_id) or {}
    sites = _rows(f"SELECT id, public_id, domain FROM sites WHERE tenant_id = {P} ORDER BY domain, id",
                  (tenant_id,))
    folders = _folder_names(sites)
    entries: list[tuple[str, object]] = [
        ("README.txt", readme_text("account", tenant.get("name") or "", now,
                                   {s["public_id"]: folders[int(s["id"])] for s in sites})),
    ]
    entries += _account_files(tenant_id)
    for s in sites:
        entries += site_entries(s, prefix=folders[int(s["id"])] + "/")
    return stream_zip(entries, now)


class Guarded:
    """Iterator that holds one export slot until it is exhausted, closed or collected, so a
    client that disconnects half way does not leak the slot."""

    def __init__(self, it: Iterator[bytes]):
        self._it = it
        self._open = True

    def __iter__(self):
        return self

    def __next__(self) -> bytes:
        try:
            return next(self._it)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._open:
            self._open = False
            try:
                close = getattr(self._it, "close", None)
                if close:
                    close()
            finally:
                _SLOTS.release()

    def __del__(self):
        try:
            self.close()
        except Exception:  # noqa: BLE001
            pass


def start(it: Iterator[bytes]) -> Guarded | None:
    """Reserve an export slot for `it`; None when two exports already run."""
    if not _SLOTS.acquire(blocking=False):
        return None
    return Guarded(it)


# --- Signed download links -----------------------------------------------------------------

def sign_link(tenant_id: int, expires: datetime) -> dict[str, str]:
    """Query parameters for /eksport: account id, expiry (unix seconds) and signature."""
    exp = str(int(expires.timestamp()))
    return {"tid": str(int(tenant_id)), "exp": exp,
            "t": auth.sign_token(LINK_PURPOSE, f"{int(tenant_id)}:{exp}")}


def verify_link(tid: str, exp: str, token: str, now: datetime | None = None) -> int | None:
    """The account id if the link is genuine and not expired, else None."""
    try:
        tenant_id, exp_s = int(tid), int(exp)
    except (TypeError, ValueError):
        return None
    if not auth.check_token(LINK_PURPOSE, f"{tenant_id}:{exp_s}", token):
        return None
    if exp_s < int((now or datetime.now(timezone.utc)).timestamp()):
        return None
    return tenant_id


def link_expired(tid: str, exp: str, token: str, now: datetime | None = None) -> bool:
    """True for a genuine link that has run out (to say so, instead of "invalid")."""
    try:
        tenant_id, exp_s = int(tid), int(exp)
    except (TypeError, ValueError):
        return False
    return auth.check_token(LINK_PURPOSE, f"{tenant_id}:{exp_s}", token) and exp_s < int(
        (now or datetime.now(timezone.utc)).timestamp())


__all__ = ["site_zip", "account_zip", "start", "sign_link", "verify_link", "link_expired",
           "SITE_FILES", "ACCOUNT_FILES"]
