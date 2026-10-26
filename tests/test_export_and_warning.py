"""Full data export ("Last ned alle data") and the warning mail before an account is deleted.

Export: one ZIP per site and one per account, CSV with semicolon + BOM, a README, every
table the customer owns, and never the visitor hash. Only your own sites; a signed link
(from the warning mail) works until it expires and cannot be forged or pointed at another
account; a cancelled account can still download.

Warning: one mail per ending, 14 days before the account becomes deletable, to the owner,
never to active accounts, not by a dry run, retried after a failed send, reset when the
account buys a plan again; the purge holds an ended account until the warning is out.

Run ONE FILE PER PROCESS (the backend is chosen at import):
    .venv/bin/python3 -m pytest -q tests/test_export_and_warning.py
"""

from __future__ import annotations

import contextlib
import csv
import io
import json
import os
import re
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

_PG = os.environ.get("SPORLOS_TEST_PG")  # a throwaway Postgres, see tests/test_pg_pool.py
_TMP_DB = tempfile.NamedTemporaryFile(prefix="sporlos_test_", suffix=".db", delete=False)
_TMP_DB.close()
if _PG:
    os.environ["DATABASE_URL"] = _PG
else:
    os.environ["SPORLOS_DB"] = _TMP_DB.name
    os.environ.pop("DATABASE_URL", None)

from starlette.testclient import TestClient  # noqa: E402

from app import export as data_export  # noqa: E402
from app import mailer, main, manage, notify, store  # noqa: E402
from app.auth import hash_password  # noqa: E402

P = store.P
PW = "passord-123"
SECRET_HASH = "VISITORHASHSECRET0123456789abcdef"
SENT: list[tuple[str, str, str]] = []
FAIL = {"on": False}


def _fake_send(to, subject, body):
    if FAIL["on"]:
        return False
    SENT.append((to, subject, body))
    return True


def setUpModule():
    store.init_db()
    store.ensure_retention_schema()
    if not _PG:
        with store._cursor() as cur:  # `anchors` exists only in the Postgres schema
            cur.execute(
                "CREATE TABLE IF NOT EXISTS anchors (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "site_id INTEGER NOT NULL, period TEXT, rollup_hash TEXT NOT NULL, txid TEXT, anchored_at TEXT)"
            )
    mailer.send = _fake_send


def tearDownModule():
    os.unlink(_TMP_DB.name)


def ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


_n = [0]


def account(company: str | None = None, **cols) -> tuple[int, int, str]:
    _n[0] += 1
    email = f"eier{_n[0]}@example.no"
    tid, uid = store.create_account(company or f"Firma {_n[0]}", email, hash_password(PW))
    store.set_email_verified(uid)
    if cols:
        sets = ", ".join(f"{k} = {P}" for k in cols)
        with store._cursor() as cur:
            cur.execute(f"UPDATE tenants SET {sets} WHERE id = {P}", (*cols.values(), tid))
    return tid, uid, email


def login(email: str) -> TestClient:
    c = TestClient(main.app)
    r = c.post("/login", data={"email": email, "password": PW}, follow_redirects=False)
    assert r.headers["location"] == "/app", r.headers.get("location")
    return c


def seed_site(tid: int, domain: str) -> dict:
    """A site with a row in every table the export reads."""
    site = store.create_site(tid, domain)
    sid = site["id"]
    now = utcnow()
    for i in range(7):
        store.insert_event(sid, {"name": "pageview", "path": f"/side-{i % 3}", "visitor_hash": SECRET_HASH + str(i % 2),
                                 "referrer_src": "google.com" if i % 2 else None, "country": "Norway",
                                 "region": "Oslo", "device": "desktop", "browser": "Firefox", "os": "Linux",
                                 "utm_source": "nyhetsbrev" if i == 0 else None,
                                 "utm_campaign": "host" if i == 0 else None})
    store.insert_event(sid, {"name": "=HYPERLINK(\"http://x\")", "path": "/", "visitor_hash": SECRET_HASH + "0"})
    store.insert_event(sid, {"name": "purchase", "path": "/kasse", "visitor_hash": SECRET_HASH + "0",
                             "revenue_cents": 12345, "currency": "NOK", "payment_method": "vipps"},
                       items=[{"name": "Kaffe", "qty": 2, "unit_price_cents": 6000}])
    store.create_goal(sid, "Kjøp", "event", "purchase")
    store.create_funnel(sid, "Kasse", [{"type": "path", "value": "/side-0"}, {"type": "event", "value": "purchase"}])
    day = (now - timedelta(days=400)).strftime("%Y-%m-%d")
    store.compute_rollup(sid, now.strftime("%Y-%m-%d"))
    with store._cursor() as cur:  # an old day with a proof, long after its raw events are gone
        cur.execute(f"INSERT INTO daily_rollups (site_id, day, pageviews, visitors, sessions, bounce_rate, "
                    f"rollup_hash, merkle_root, merkle_proof, txid, anchored_at) VALUES "
                    f"({P},{P},{P},{P},{P},{P},{P},{P},{P},{P},{P})",
                    (sid, day, 10, 4, 5, 40.5, "ab" * 32, "cd" * 32, '["x","y"]', "ef" * 32, "2025-01-02 00:00:00"))
        cur.execute(f"INSERT INTO anchors (site_id, period, rollup_hash, txid) VALUES ({P}, {P}, {P}, {P})",
                    (sid, "[2025-01-01,2025-01-02)", "ab" * 32, "ef" * 32))
    store.upsert_search_rows([(sid, day, "google", "total", "", 3, 40, 7.5)])
    store.set_search_connection(sid, "ENCRYPTED-REFRESH-TOKEN", "sc-domain:" + domain, "owner@example.no")
    store.add_bot_counts({(sid, day, "ai", "GPTBot", "/"): 4})
    return site


def read_zip(blob: bytes) -> dict[str, bytes]:
    z = zipfile.ZipFile(io.BytesIO(blob))
    assert z.testzip() is None
    return {n: z.read(n) for n in z.namelist()}


def table(files: dict[str, bytes], name: str) -> list[list[str]]:
    raw = files[name]
    assert raw.startswith(b"\xef\xbb\xbf"), f"{name} has no BOM"
    return list(csv.reader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";"))


class SiteExportTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tid, cls.uid, cls.email = account("Kaffe AS")
        cls.site = seed_site(cls.tid, "kaffe.no")
        cls.client = login(cls.email)
        r = cls.client.get(f"/app/export/alt?site={cls.site['public_id']}")
        cls.resp = r
        cls.files = read_zip(r.content)

    def test_download_headers(self):
        r = self.resp
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "application/zip")
        self.assertRegex(r.headers["content-disposition"], r'filename="sporlos-kaffe\.no-alle-data-\d{4}-\d{2}-\d{2}\.zip"')
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertNotEqual(r.headers.get("content-encoding"), "gzip")  # already compressed

    def test_zip_has_every_file_and_a_readme_describing_each(self):
        expected = {n for n, _ in data_export.SITE_FILES} | {"README.txt"}
        self.assertEqual(set(self.files), expected)
        readme = self.files["README.txt"].decode()
        for name, _ in data_export.SITE_FILES:
            self.assertIn(f"- {name}:", readme)
        self.assertIn("visitor_hash", readme)  # says it is left out
        self.assertIn("UTC", readme)

    def test_csv_format_is_semicolon_utf8_bom(self):
        for name, _ in data_export.SITE_FILES:
            raw = self.files[name]
            self.assertTrue(raw.startswith(b"\xef\xbb\xbf"), name)
            rows = table(self.files, name)
            self.assertTrue(rows, name)  # at least the header
            self.assertTrue(all(len(r) == len(rows[0]) for r in rows), name)
            self.assertGreater(len(rows[0]), 1, name)  # split on ;, not on ,
        self.assertIn("Kjøp", self.files["maal.csv"].decode("utf-8-sig"))  # æøå survives

    def test_no_visitor_hash_anywhere(self):
        for name, blob in self.files.items():
            text = blob.decode("utf-8-sig")
            self.assertNotIn("visitor_hash", text.split("\n", 1)[0], name)  # no such column
            self.assertNotIn("visitor", text.split("\n", 1)[0].replace("unike_besoekende", ""), name)
            self.assertNotIn(SECRET_HASH, text, name)
            self.assertNotIn("session_id", text.split("\n", 1)[0], name)
        # and none of the secrets we hold for the customer
        self.assertNotIn("ENCRYPTED-REFRESH-TOKEN", b"".join(self.files.values()).decode("utf-8", "replace"))
        self.assertNotIn("pbkdf2", b"".join(self.files.values()).decode("utf-8", "replace"))

    def test_raw_events_are_all_there_without_the_hash(self):
        rows = table(self.files, "raadata_hendelser.csv")
        head, body = rows[0], rows[1:]
        self.assertEqual(len(body), 9)
        self.assertIn("tidspunkt_utc", head)
        self.assertNotIn("visitor_hash", head)
        paths = [r[head.index("side")] for r in body]
        self.assertIn("/kasse", paths)
        purchase = [r for r in body if r[head.index("hendelse")] == "purchase"][0]
        self.assertEqual(purchase[head.index("omsetning_minste_enhet")], "12345")
        self.assertEqual(purchase[head.index("betalingsmaate")], "vipps")
        items = table(self.files, "raadata_varelinjer.csv")
        self.assertEqual(items[1][items[0].index("produkt")], "Kaffe")
        self.assertEqual(items[1][items[0].index("hendelse_id")], purchase[head.index("hendelse_id")])

    def test_formula_cells_are_neutralised(self):
        rows = table(self.files, "egendefinerte_hendelser.csv")
        names = [r[0] for r in rows[1:]]
        self.assertIn("'=HYPERLINK(\"http://x\")", names)
        self.assertFalse([n for n in names if n.startswith("=")])

    def test_aggregates_match_the_stored_events(self):
        sider = {r[0]: r for r in table(self.files, "sider.csv")[1:]}
        self.assertEqual(sider["/side-0"][1], "3")  # events 0, 3, 6
        kilder = {r[0]: r[1] for r in table(self.files, "kilder.csv")[1:]}
        self.assertEqual(kilder["google.com"], "3")
        self.assertEqual(kilder["direkte"], "4")
        land = table(self.files, "land.csv")[1]
        self.assertEqual(land[0], "Norge")
        self.assertEqual(land[1], "Norway")
        kamp = table(self.files, "kampanjer.csv")[1]
        self.assertEqual(kamp[:3], ["nyhetsbrev", "", "host"])
        self.assertEqual(table(self.files, "tidsserie.csv")[1][1], "7")  # custom events and purchases are not pageviews
        entries = {r[0]: r[1] for r in table(self.files, "inngangssider.csv")[1:]}
        self.assertTrue(entries)
        self.assertEqual(sum(int(v) for v in entries.values()), sum(
            int(v) for v in {r[0]: r[1] for r in table(self.files, "utgangssider.csv")[1:]}.values()))

    def test_goals_funnels_ecommerce(self):
        goal = table(self.files, "maal.csv")[1]
        self.assertEqual(goal[0], "Kjøp")
        self.assertEqual(goal[4], "1")
        trakt = table(self.files, "trakter.csv")
        self.assertEqual([r[1] for r in trakt[1:]], ["1", "2"])
        self.assertEqual(trakt[2][4], "1")
        summ = table(self.files, "ehandel_oppsummering.csv")[1]
        self.assertEqual(summ, ["NOK", "1", "123,45"])
        self.assertEqual(table(self.files, "ehandel_betalingsmaater.csv")[1], ["NOK", "vipps", "1", "123,45"])
        self.assertEqual(table(self.files, "ehandel_kilder.csv")[1][:2], ["NOK", "nyhetsbrev"])  # first pageview of the buyer
        prod = table(self.files, "ehandel_produkter.csv")[1]
        self.assertEqual(prod, ["NOK", "Kaffe", "2", "120,00"])

    def test_history_and_anchoring_are_kept(self):
        rows = table(self.files, "daglige_tall.csv")
        head = rows[0]
        old = [r for r in rows[1:] if r[head.index("txid")]][0]
        self.assertEqual(old[head.index("txid")], "ef" * 32)
        self.assertEqual(old[head.index("merkle_root")], "cd" * 32)
        self.assertEqual(old[head.index("merkle_proof")], '["x","y"]')
        self.assertEqual(old[head.index("rollup_hash")], "ab" * 32)
        self.assertEqual(old[head.index("fluktfrekvens_prosent")], "40,5")
        self.assertEqual(table(self.files, "forankringer.csv")[1][2], "ef" * 32)
        self.assertEqual(table(self.files, "roboter.csv")[1][1:], ["ai", "GPTBot", "/", "4"])
        self.assertEqual(table(self.files, "sokestatistikk.csv")[1][4:], ["3", "40", "7,5"])

    def test_big_tables_are_paged_without_losing_or_repeating_rows(self):
        tid, _, _ = account("Stor AS")
        site = store.create_site(tid, "stor.no")
        for i in range(23):
            store.insert_event(site["id"], {"name": "pageview", "path": f"/p{i:02d}", "visitor_hash": "h"})
        with store._cursor() as cur:  # many events share one timestamp: the id must break the tie
            cur.execute(f"UPDATE events SET ts = {P} WHERE site_id = {P}", ("2026-01-01 10:00:00", site["id"]))
        old = data_export.PAGE
        data_export.PAGE = 5
        try:
            files = read_zip(b"".join(data_export.site_zip(site)))
        finally:
            data_export.PAGE = old
        rows = table(files, "raadata_hendelser.csv")[1:]
        self.assertEqual(sorted(r[3] for r in rows), [f"/p{i:02d}" for i in range(23)])
        self.assertEqual(len({r[0] for r in rows}), 23)

    def test_stream_is_chunked_and_slot_is_released(self):
        stream = data_export.start(data_export.site_zip(self.site))
        self.assertIsNotNone(stream)
        first = next(stream)
        self.assertTrue(first.startswith(b"PK"))
        stream2 = data_export.start(data_export.site_zip(self.site))
        self.assertIsNotNone(stream2)
        self.assertIsNone(data_export.start(data_export.site_zip(self.site)))  # two at a time
        r = self.client.get(f"/app/export/alt?site={self.site['public_id']}")
        self.assertEqual(r.status_code, 429)
        stream.close()  # a client that disconnected
        for _ in stream2:
            pass
        again = data_export.start(iter(()))
        self.assertIsNotNone(again)
        again.close()


class AccountExportTest(unittest.TestCase):
    def test_account_zip_covers_only_own_sites(self):
        tid, _, email = account("Eier AS")
        a, b = seed_site(tid, "eier.no"), seed_site(tid, "blogg.eier.no")
        other_tid, _, _ = account("Annen AS")
        seed_site(other_tid, "annen-konkurrent.no")
        store.insert_event(store.get_site_by_domain(other_tid, "annen-konkurrent.no")["id"],
                           {"name": "pageview", "path": "/hemmelig-annen", "visitor_hash": "z"})
        r = login(email).get("/app/export/konto")
        self.assertEqual(r.status_code, 200)
        files = read_zip(r.content)
        self.assertIn("README.txt", files)
        self.assertEqual(table(files, "konto.csv")[1][0], "Eier AS")
        self.assertEqual([x[0] for x in table(files, "brukere.csv")[1:]], [email])
        folders = {n.split("/")[0] for n in files if "/" in n}
        self.assertEqual(folders, {"eier.no", "blogg.eier.no"})
        for folder in folders:
            self.assertEqual({n.split("/", 1)[1] for n in files if n.startswith(folder + "/")},
                             {n for n, _ in data_export.SITE_FILES})
        everything = b"".join(files.values()).decode("utf-8", "replace")
        self.assertNotIn("annen-konkurrent", everything)
        self.assertNotIn("hemmelig-annen", everything)
        self.assertNotIn(SECRET_HASH, everything)
        readme = files["README.txt"].decode()
        self.assertIn("eier.no/", readme)
        self.assertIn(a["public_id"], readme)
        self.assertIn(b["public_id"], readme)

    def test_another_tenants_site_is_404_and_logged_out_goes_to_login(self):
        victim_tid, _, _ = account("Offer AS")
        site = seed_site(victim_tid, "offer.no")
        _, _, attacker = account("Angriper AS")
        c = login(attacker)
        r = c.get(f"/app/export/alt?site={site['public_id']}")
        self.assertEqual(r.status_code, 404)
        self.assertNotIn(b"PK", r.content[:2])
        self.assertEqual(c.get("/app/export/alt?site=finnes-ikke").status_code, 404)
        out = TestClient(main.app)
        for url in (f"/app/export/alt?site={site['public_id']}", "/app/export/konto"):
            r = out.get(url, follow_redirects=False)
            self.assertEqual((r.status_code, r.headers["location"]), (302, "/login"))

    def test_cancelled_account_can_still_log_in_and_download(self):
        tid, _, email = account("Avsluttet AS")
        seed_site(tid, "avsluttet.no")
        store.set_tenant_plan(tid, "cancelled")
        c = login(email)
        page = c.get("/app")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Last ned alle data", page.text)
        self.assertIn('href="/app/export/konto"', page.text)
        files = read_zip(c.get("/app/export/konto").content)
        self.assertIn("avsluttet.no/raadata_hendelser.csv", files)

    def test_site_page_and_delete_card_link_to_the_export(self):
        tid, _, email = account("Lenke AS")
        site = seed_site(tid, "lenke.no")
        page = login(email).get(f"/app?site={site['public_id']}")
        self.assertIn(f'/app/export/alt?site={site["public_id"]}', page.text)
        self.assertGreaterEqual(page.text.count(f'/app/export/alt?site={site["public_id"]}'), 2)


class SignedLinkTest(unittest.TestCase):
    def setUp(self):
        self.tid, _, _ = account("Lenke AS")
        seed_site(self.tid, "lenke-sig.no")
        self.c = TestClient(main.app)

    def params(self, expires=None) -> dict:
        return data_export.sign_link(self.tid, expires or utcnow() + timedelta(days=5))

    def test_valid_link_shows_a_page_and_downloads_without_login(self):
        p = self.params()
        page = self.c.get("/eksport", params=p)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Lenke AS", page.text)
        self.assertIn("Last ned alle data (ZIP)", page.text)
        self.assertNotIn(b"PK", page.content[:2])  # the GET starts no export
        r = self.c.post("/eksport", data=p)
        self.assertEqual(r.status_code, 200)
        files = read_zip(r.content)
        self.assertIn("lenke-sig.no/raadata_hendelser.csv", files)

    def test_expired_link_is_refused(self):
        p = self.params(utcnow() - timedelta(minutes=1))
        page = self.c.get("/eksport", params=p)
        self.assertEqual(page.status_code, 410)
        self.assertIn("gått ut", page.text)
        r = self.c.post("/eksport", data=p)
        self.assertEqual(r.status_code, 410)
        self.assertNotEqual(r.headers["content-type"], "application/zip")

    def test_forged_or_moved_links_are_refused(self):
        p = self.params()
        other_tid, _, _ = account("Annen AS")
        for bad in (
            {**p, "t": "0" * 32},
            {**p, "t": ""},
            {**p, "tid": str(other_tid)},                      # someone else's account, same signature
            {**p, "exp": str(int(p["exp"]) + 86400 * 365)},     # stretched expiry
            {**p, "tid": "abc"},
            {"tid": p["tid"]},
        ):
            self.assertEqual(self.c.get("/eksport", params=bad).status_code, 404, bad)
            r = self.c.post("/eksport", data=bad)
            self.assertEqual(r.status_code, 404, bad)
            self.assertNotEqual(r.headers["content-type"], "application/zip")

    def test_link_for_a_deleted_account_says_so(self):
        p = self.params()
        store.delete_tenant(self.tid)
        page = self.c.get("/eksport", params=p)
        self.assertEqual(page.status_code, 404)
        self.assertIn("slettet", page.text)


def _ended(**kw) -> datetime:
    # An hour of margin: the cutoffs are strict and second-resolution, so an account that
    # ended exactly N days ago would sit on the edge. The exact edge is tested with a fixed now.
    return utcnow() - timedelta(**kw) - timedelta(hours=1)


class WarningTest(unittest.TestCase):
    def setUp(self):
        SENT.clear()
        FAIL["on"] = False
        with store._cursor() as cur:
            cur.execute("SELECT id FROM tenants")
            ids = [int(r["id"]) for r in cur.fetchall()]
        for tid in ids:
            store.delete_tenant(tid)

    def cancelled(self, days_ago: int, **cols):
        tid, uid, email = account(plan="cancelled", cancelled_at=ts(_ended(days=days_ago)), **cols)
        return tid, email

    def trial(self, days_ago: int):
        tid, uid, email = account(trial_ends_at=ts(_ended(days=days_ago)))
        return tid, email

    def test_warned_exactly_fourteen_days_before_deletion(self):
        self.assertEqual(store.RETENTION_ENDED_DAYS - store.DELETION_WARN_DAYS, 70)
        early, _ = self.cancelled(69)
        due, due_mail = self.cancelled(70)
        late_trial, late_mail = self.trial(71)
        res = notify.send_deletion_warnings(enforce=True)
        self.assertEqual((res["due"], res["sent"], res["failed"]), (2, 2, 0))
        self.assertEqual(sorted(m[0] for m in SENT), sorted([due_mail, late_mail]))

    def test_sent_once_per_account_across_daily_runs(self):
        self.cancelled(72)
        for _ in range(3):
            notify.send_deletion_warnings(enforce=True)
        self.assertEqual(len(SENT), 1)
        self.assertEqual(notify.send_deletion_warnings(enforce=True)["due"], 0)

    def test_never_sent_to_active_or_paying_or_unverified_accounts(self):
        long_ago = ts(_ended(days=400))
        account(plan="vekst", trial_ends_at=long_ago)
        account(plan="owner", trial_ends_at=long_ago)
        account(plan="byra", trial_ends_at=long_ago)
        account(trial_ends_at=ts(utcnow() + timedelta(days=10)))                          # trial still running
        account(plan="cancelled", cancelled_at=ts(_ended(days=10)))                        # just ended
        account(plan="cancelled", cancelled_at=long_ago, vipps_agreement_id="agr-1")      # money moving
        account(plan="cancelled", cancelled_at=long_ago, invoice_details="{}")
        account(trial_ends_at=long_ago, stripe_subscription_id="sub_1")
        account(trial_ends_at=long_ago, vipps_pending_plan="vekst")
        _, uid, _ = account(trial_ends_at=ts(utcnow() + timedelta(days=10)),
                            created_at=ts(_ended(days=30)))
        with store._cursor() as cur:
            cur.execute(f"UPDATE users SET email_verified = 0 WHERE id = {P}", (uid,))  # unverified, no data
        res = notify.send_deletion_warnings(enforce=True)
        self.assertEqual((res["due"], res["sent"]), (0, 0))
        self.assertEqual(SENT, [])

    def test_dry_run_sends_and_marks_nothing(self):
        tid, _ = self.cancelled(75)
        res = notify.send_deletion_warnings(enforce=False)
        self.assertEqual((res["due"], res["sent"]), (1, 0))
        self.assertEqual(SENT, [])
        self.assertEqual(len(store.deletion_warning_candidates()), 1)
        out = io.StringIO()
        os.environ.pop("SPORLOS_RETENTION_ENFORCE", None)
        with contextlib.redirect_stdout(out):
            self.assertEqual(manage.main(["purge-dormant"]), 0)
        payload = json.loads(out.getvalue().split(" ", 1)[1])
        self.assertEqual(payload["deletion_warnings"]["due"], 1)
        self.assertNotIn("@", out.getvalue())
        self.assertEqual(SENT, [])

    def test_manage_enforce_sends_and_reports_failures_in_the_exit_code(self):
        self.cancelled(75)
        os.environ["SPORLOS_RETENTION_ENFORCE"] = "1"
        try:
            FAIL["on"] = True
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(manage.main(["purge-dormant"]), 1)
            self.assertEqual(SENT, [])
            FAIL["on"] = False
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(manage.main(["purge-dormant"]), 0)
            self.assertEqual(json.loads(out.getvalue().split(" ", 1)[1])["deletion_warnings"]["sent"], 1)
        finally:
            os.environ.pop("SPORLOS_RETENTION_ENFORCE", None)
        self.assertEqual(len(SENT), 1)

    def test_failed_send_is_retried_next_run(self):
        self.cancelled(72)
        FAIL["on"] = True
        res = notify.send_deletion_warnings(enforce=True)
        self.assertEqual((res["sent"], res["failed"]), (0, 1))
        FAIL["on"] = False
        res = notify.send_deletion_warnings(enforce=True)
        self.assertEqual((res["sent"], res["failed"]), (1, 0))

    def test_reactivation_resets_the_marker_and_the_next_ending_gets_its_own_warning(self):
        tid, _ = self.cancelled(72)
        notify.send_deletion_warnings(enforce=True)
        self.assertEqual(len(SENT), 1)
        self.assertIsNotNone(self._warned(tid))
        store.set_tenant_plan(tid, "vekst")                       # buys a plan again
        self.assertIsNone(self._warned(tid))
        self.assertEqual(notify.send_deletion_warnings(enforce=True)["due"], 0)
        store.set_tenant_plan(tid, "cancelled")                   # leaves again, later
        with store._cursor() as cur:
            cur.execute(f"UPDATE tenants SET cancelled_at = {P} WHERE id = {P}", (ts(_ended(days=71)), tid))
        notify.send_deletion_warnings(enforce=True)
        self.assertEqual(len(SENT), 2)

    def test_vipps_and_invoice_reactivation_also_reset(self):
        for how in ("vipps", "invoice"):
            tid, _ = self.cancelled(72)
            notify.send_deletion_warnings(enforce=True)
            self.assertIsNotNone(self._warned(tid), how)
            if how == "vipps":
                store.activate_vipps(tid, "liten", "2026-12-01")
            else:
                store.set_invoice_billing(tid, "liten", {"org": "1"}, "2027-10-01")
            self.assertIsNone(self._warned(tid), how)

    def test_an_old_marker_from_an_earlier_ending_does_not_count(self):
        tid, _ = self.cancelled(72, deletion_warned_at=ts(_ended(days=300)))
        self.assertEqual([c["id"] for c in store.deletion_warning_candidates()], [tid])

    def test_mail_text_and_link(self):
        tid, email = self.cancelled(70, name="Kaffe AS")
        notify.send_deletion_warnings(enforce=True)
        to, subject, body = SENT[0]
        self.assertEqual(to, email)
        delete_on = store.deletion_date(_ended(days=70), utcnow())
        self.assertAlmostEqual(delete_on.timestamp(), (_ended(days=70) + timedelta(days=84)).timestamp(), delta=5)
        when = notify._no_long_date(delete_on)
        self.assertIn(when, subject)
        for needle in ("Kaffe AS", when, "abonnementet ble avsluttet", "Last ned alle data", "/login",
                       "Velg en plan", "ZIP", "rå hendelsene", "35 dager", "alle brukerne"):
            self.assertIn(needle, body)
        self.assertNotIn("Co-authored", body)
        link = re.search(r"https?://\S+/eksport\?\S+", body).group(0)
        q = {k: v[0] for k, v in parse_qs(urlparse(link).query).items()}
        self.assertEqual(data_export.verify_link(q["tid"], q["exp"], q["t"]), tid)
        # valid until the day after the deletion date, not forever
        self.assertAlmostEqual(int(q["exp"]), int((delete_on + timedelta(days=1)).timestamp()), delta=5)
        self.assertIsNone(data_export.verify_link(q["tid"], q["exp"], q["t"], delete_on + timedelta(days=2)))
        # the link works end to end
        r = TestClient(main.app).post("/eksport", data=q)
        self.assertEqual(r.status_code, 200)
        self.assertIn("README.txt", read_zip(r.content))

    def test_trial_mail_names_the_trial(self):
        self.trial(71)
        notify.send_deletion_warnings(enforce=True)
        self.assertIn("prøveperioden gikk ut", SENT[0][2])

    def test_mail_goes_to_the_owner_only_even_when_unsubscribed_from_reports(self):
        tid, email = self.cancelled(72, email_optout=1)
        store.create_invite(tid, "kollega@example.no", store.account_owner_id(tid))
        notify.send_deletion_warnings(enforce=True)
        self.assertEqual([m[0] for m in SENT], [email])

    def test_account_page_shows_the_deletion_date_for_an_ended_account(self):
        tid, email = self.cancelled(72)
        page = login(email).get("/app").text
        self.assertIn("Kontoen er ikke aktiv", page)
        n = store.deletion_notice(tid)
        self.assertIn(main._no_date(n["delete_on"].strftime("%Y-%m-%d")), page)
        active, _, active_mail = account(plan="liten")
        self.assertNotIn("Kontoen er ikke aktiv", login(active_mail).get("/app").text)

    def _warned(self, tid):
        with store._cursor() as cur:
            cur.execute(f"SELECT deletion_warned_at FROM tenants WHERE id = {P}", (tid,))
            return cur.fetchone()["deletion_warned_at"]


class PurgeWaitsForTheWarningTest(unittest.TestCase):
    def setUp(self):
        SENT.clear()
        FAIL["on"] = False
        with store._cursor() as cur:
            cur.execute("SELECT id FROM tenants")
            ids = [int(r["id"]) for r in cur.fetchall()]
        for tid in ids:
            store.delete_tenant(tid)

    def exists(self, tid) -> bool:
        with store._cursor() as cur:
            cur.execute(f"SELECT 1 FROM tenants WHERE id = {P}", (tid,))
            return cur.fetchone() is not None

    def test_ended_account_is_deleted_only_after_the_warning(self):
        now = utcnow()
        tid, _, _ = account(plan="cancelled", cancelled_at=ts(now - timedelta(days=84, hours=1)))
        res = store.purge_dormant(enforce=True, now=now)          # eligible but never warned
        self.assertEqual((res["counts"]["held_for_notice"], res["counts"]["deleted_accounts"]), (1, 0))
        self.assertTrue(self.exists(tid))
        notify.send_deletion_warnings(enforce=True, now=now)       # the warning goes out today
        self.assertEqual(len(SENT), 1)
        for days in (1, 3, 4):
            store.purge_dormant(enforce=True, now=now + timedelta(days=days))
            self.assertTrue(self.exists(tid), f"deleted {days} days after the warning")
        # Day 89 after the end: the promise to delete within 90 days wins over the notice.
        res = store.purge_dormant(enforce=True, now=now + timedelta(days=5, hours=1))
        self.assertEqual(res["counts"]["deleted_accounts"], 1)
        self.assertFalse(self.exists(tid))

    def test_minimum_notice_after_a_late_warning(self):
        now = utcnow()
        tid, _, _ = account(plan="cancelled", cancelled_at=ts(now - timedelta(days=77)))
        notify.send_deletion_warnings(enforce=True, now=now)       # mail was down until day 77
        self.assertEqual(len(SENT), 1)
        # Eligible from day 84, but not before DELETION_MIN_NOTICE_DAYS after the mail (day 84 here).
        store.purge_dormant(enforce=True, now=now + timedelta(days=6, hours=23))
        self.assertTrue(self.exists(tid))
        res = store.purge_dormant(enforce=True, now=now + timedelta(days=7, minutes=1))
        self.assertEqual(res["counts"]["deleted_accounts"], 1)

    def test_normal_schedule_deletes_fourteen_days_after_the_warning(self):
        now = utcnow()
        tid, _, _ = account(plan="cancelled", cancelled_at=ts(now - timedelta(days=70, hours=1)))
        notify.send_deletion_warnings(enforce=True, now=now)
        self.assertEqual(len(SENT), 1)
        res = store.purge_dormant(enforce=True, now=now + timedelta(days=13))
        self.assertEqual(res["counts"]["deleted_accounts"], 0)
        res = store.purge_dormant(enforce=True, now=now + timedelta(days=14, minutes=1))
        self.assertEqual(res["counts"]["deleted_accounts"], 1)

    def test_the_warning_starts_exactly_fourteen_days_before_the_cutoff(self):
        NOW = datetime(2026, 10, 21, 12, 0, 0, tzinfo=timezone.utc)
        edge = NOW - timedelta(days=store.RETENTION_ENDED_DAYS - store.DELETION_WARN_DAYS)
        just_in, _, _ = account(plan="cancelled", cancelled_at=ts(edge - timedelta(seconds=1)))
        on_edge, _, _ = account(plan="cancelled", cancelled_at=ts(edge))
        ids = [c["id"] for c in store.deletion_warning_candidates(NOW)]
        self.assertIn(just_in, ids)
        self.assertNotIn(on_edge, ids)

    def test_nobody_is_kept_past_the_promise_when_mail_cannot_be_sent(self):
        now = utcnow()
        tid, _, _ = account(plan="cancelled", cancelled_at=ts(now - timedelta(days=88, hours=1)))
        FAIL["on"] = True
        notify.send_deletion_warnings(enforce=True, now=now)
        self.assertTrue(store.purge_dormant(enforce=True, now=now)["counts"]["held_for_notice"])
        self.assertTrue(self.exists(tid))
        res = store.purge_dormant(enforce=True, now=now + timedelta(hours=23, minutes=30))  # day 89
        self.assertEqual(res["counts"]["deleted_accounts"], 1)
        self.assertLess(store.RETENTION_HARD_DAYS, 90)

    def test_a_warning_from_an_earlier_ending_does_not_release_the_account(self):
        now = utcnow()
        tid, _, _ = account(plan="cancelled", cancelled_at=ts(now - timedelta(days=85)),
                            deletion_warned_at=ts(now - timedelta(days=300)))
        res = store.purge_dormant(enforce=True, now=now)
        self.assertEqual(res["counts"]["deleted_accounts"], 0)
        self.assertTrue(self.exists(tid))

    def test_dry_run_reports_held_accounts_and_changes_nothing(self):
        now = utcnow()
        tid, _, _ = account(plan="cancelled", cancelled_at=ts(now - timedelta(days=85)))
        res = store.purge_dormant(enforce=False, now=now)
        self.assertEqual(res["counts"]["held_for_notice"], 1)
        self.assertEqual(res["counts"]["accounts"]["cancelled"], 1)
        self.assertTrue(self.exists(tid))

    def test_unverified_signups_need_no_warning(self):
        now = utcnow()
        tid, uid, _ = account(created_at=ts(now - timedelta(days=30)))
        with store._cursor() as cur:
            cur.execute(f"UPDATE users SET email_verified = 0 WHERE id = {P}", (uid,))
        res = store.purge_dormant(enforce=True, now=now)
        self.assertEqual(res["counts"]["deleted_accounts"], 1)
        self.assertEqual(res["counts"]["held_for_notice"], 0)
        self.assertEqual(SENT, [])


class PublicTextMatchesTheCodeTest(unittest.TestCase):
    """The terms (§10), the privacy page and the FAQ promise what the code does."""

    def page(self, path: str) -> str:
        return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", TestClient(main.app).get(path).text))

    def test_pages_state_the_export_and_the_warning_with_the_real_numbers(self):
        warn, ended = str(store.DELETION_WARN_DAYS), str(store.RETENTION_ENDED_DAYS)
        vilkar, personvern, faq = self.page("/vilkar"), self.page("/personvern"), self.page("/sporsmal")
        self.assertIn("ZIP med CSV-filer", vilkar)
        self.assertIn(f"én e-post {warn} dager før", vilkar)
        self.assertIn("innen 90 dager", vilkar)
        self.assertIn("Last ned alle data", personvern)
        self.assertIn(f"{ended} dager etter at prøveperioden gikk ut eller abonnementet ble avsluttet", personvern)
        self.assertIn(f"én e-post {warn} dager før", personvern)
        self.assertIn("uten besøkskoden", personvern)
        self.assertIn("Kan jeg ta med meg dataene mine hvis jeg slutter?", faq)
        self.assertIn(f"én e-post {warn} dager før kontoen slettes", faq)
        self.assertIn(f"slettes {ended} dager etter", faq)
        self.assertLess(store.RETENTION_ENDED_DAYS, 90)

    def test_the_mail_says_what_the_pages_say(self):
        c = {"id": 1, "name": "X AS", "category": "cancelled", "ended_at": utcnow() - timedelta(days=70),
             "delete_on": utcnow() + timedelta(days=14)}
        body = notify.deletion_warning_mail(c, utcnow())[1]
        self.assertIn("35 dager", body)  # backups, same as the delete card and the privacy page


if __name__ == "__main__":
    unittest.main()
