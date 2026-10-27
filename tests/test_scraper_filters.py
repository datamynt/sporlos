"""Proxy-pool scraper filters: budget-hosting ASNs, fake search referrers, impossible
UAs, and the behavioural burst detector (app/burst.py) with its backfill command.

Run ONE FILE PER PROCESS (the suite owns its SQLite file):
    .venv/bin/python3 -m unittest tests.test_scraper_filters -v < /dev/null
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta
from unittest import mock

from tests.test_ingest_limits import UA, IngestTestCase

from app import botcount, burst, datacenter, main, manage, store
from app.burst import Landing, find_bursts
from app.useragent import classify_ua, impossible_ua, spoofed_referrer

T0 = datetime(2026, 10, 27, 11, 31, 0)
IP = "84.208.10.20"
FAKER_OPERA = "Opera/8.57.(X11; Linux x86_64; de-LI) Presto/2.9.181 Version/12.00"
REAL_OPERA_PRESTO = "Opera/9.80 (Windows NT 6.1; U; en) Presto/2.9.168 Version/11.51"
QUARK = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) QuarkPC/4.4.5.501 Chrome/122.0 Safari/537.36"
WIN_CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
MODERN_OPERA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36 OPR/120.0.0.0"


def land(i, path=None, country="US", at=0.0, tagged=False, site=1):
    return Landing(site, f"h{i}", T0 + timedelta(seconds=at), path or f"/bestill/produkt-{i}", country, tagged)


def quiet(site_id, country, start, end, exclude):
    return 0


class AsnListTest(unittest.TestCase):
    def test_proxy_pool_hosting_is_listed(self):
        for asn, name in {36352: "ColoCrossing/HostPapa", 18779: "EGIHosting", 46261: "QuickPacket",
                          62874: "Web2Objects", 8100: "QuadraNet", 53667: "FranTech/BuyVM"}.items():
            self.assertIn(asn, datacenter._DATACENTER_ASNS)
            self.assertEqual(datacenter.datacenter_name(asn), name)

    def test_networks_with_real_people_are_not_listed(self):
        for asn in (174, 21928, 701, 7029,    # Cogent, T-Mobile US, Verizon, Windstream
                    9009, 60068, 212238,       # M247 / Datacamp: consumer-VPN exits
                    13335, 15169, 2116):       # Cloudflare, Google, Telenor-ish carrier
            self.assertNotIn(asn, datacenter._DATACENTER_ASNS)

    def test_every_listed_asn_has_a_name(self):
        self.assertEqual(datacenter._DATACENTER_ASNS, frozenset(datacenter._DATACENTER_NAMES))


class SpoofedReferrerTest(unittest.TestCase):
    def test_fake_search_hosts(self):
        for host in ("www.yahoo.no", "www.bing.no", "www.yandex.no", "baidu.no", "WWW.Yandex.NO",
                     "yahoo.se", "bing.co.uk", "yandex.de", "www.baidu.dk", "baidu.no."):
            with self.subTest(host=host):
                self.assertTrue(spoofed_referrer(host))

    def test_real_referrers_are_not_flagged(self):
        for host in (None, "", "www.google.no", "google.no", "www.google.com", "www.bing.com", "bing.com",
                     "cn.bing.com", "search.yahoo.com", "no.search.yahoo.com", "yandex.ru", "yandex.com",
                     "yandex.com.tr", "www.baidu.com", "duckduckgo.com", "merdata.no", "t.co",
                     "l.facebook.com", "news.ycombinator.com", "notyahoo.no", "yahoo.no.example.com"):
            with self.subTest(host=host):
                self.assertFalse(spoofed_referrer(host))


class ImpossibleUaTest(unittest.TestCase):
    def test_faker_opera(self):
        self.assertEqual(impossible_ua(FAKER_OPERA), "faker-opera")
        self.assertEqual(classify_ua(FAKER_OPERA), ("script", "fake-ua"))

    def test_presto_next_to_a_modern_platform(self):
        for ua in ("Opera/9.80 (Windows NT 10.0; Win64; x64) Presto/2.12.388 Version/12.18",
                   "Opera/9.80 (X11; Linux x86_64) Presto/2.12.388 Chrome/120.0 Version/12.16"):
            with self.subTest(ua=ua):
                self.assertEqual(impossible_ua(ua), "presto-modern")

    def test_real_browsers_are_not_flagged(self):
        for ua in (REAL_OPERA_PRESTO, "Opera/9.80 (Windows NT 6.1; WOW64) Presto/2.12.388 Version/12.18",
                   "Opera/9.64 (X11; Linux i686; U; en) Presto/2.1.1", QUARK, WIN_CHROME, MODERN_OPERA, UA,
                   "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1"):
            with self.subTest(ua=ua):
                self.assertIsNone(impossible_ua(ua))
                self.assertIsNone(classify_ua(ua))

    def test_named_bot_beats_impossible_ua(self):
        self.assertEqual(classify_ua("Opera/8.57.(X11) Googlebot/2.1"), ("search", "Googlebot"))


class IngestRulesTest(IngestTestCase):
    def setUp(self):
        super().setUp()
        burst.reset()
        botcount.flush()
        with botcount._lock:
            botcount._buf.clear()

    def send(self, ua=UA, ip=IP, path="/x", **body):
        payload = {"s": self.site["public_id"], "n": "pageview", "p": path, **body}
        return self.client.post("/api/event", json=payload,
                                headers={"user-agent": ua, "x-forwarded-for": ip})

    def bots(self):
        botcount.flush()
        with store._cursor() as cur:
            cur.execute("SELECT bot_class, agent, n FROM bot_daily WHERE site_id = ? ORDER BY agent",
                        (self.site["id"],))
            return [(r["bot_class"], r["agent"], r["n"]) for r in cur.fetchall()]

    def test_fake_search_referrer_is_a_script_not_a_person(self):
        for ref in ("https://www.yahoo.no/", "https://www.bing.no/", "https://www.yandex.no/",
                    "http://www.baidu.no/"):
            self.send(r=ref)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.bots(), [("script", "fake-referrer", 4)])

    def test_real_search_referrers_still_count_as_people(self):
        for ref in ("https://www.google.no/", "https://www.google.com/", "https://www.bing.com/",
                    "https://search.yahoo.com/search?p=x", "https://merdata.no/"):
            self.send(r=ref)
        self.send()
        self.assertEqual(len(self.rows()), 6)
        self.assertEqual(self.bots(), [])

    def test_faker_ua_is_a_script_not_a_person(self):
        self.send(ua=FAKER_OPERA)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.bots(), [("script", "fake-ua", 1)])

    def test_real_old_opera_and_quark_are_people(self):
        self.send(ua=REAL_OPERA_PRESTO)
        self.send(ua=QUARK)
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.bots(), [])

    def test_budget_hosting_asn_is_a_datacenter(self):
        with mock.patch.object(main, "datacenter_asn", return_value=36352):
            self.send(ua=WIN_CHROME)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.bots(), [("datacenter", "ColoCrossing/HostPapa", 1)])

    def test_late_event_of_a_flagged_hash_is_a_robot_hit(self):
        """checkout_start arrives after the sweep has moved the landing."""
        day_salt = store.daily_salt()
        vh = main.visitor_hash(IP, UA, str(self.site["id"]), secret=main.SECRET, day_salt=day_salt)
        burst._remember(self.site["id"], {vh})
        self.send(path="/bestill/x", n="checkout_start")
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.bots(), [("script", "burst", 1)])


class DetectorTest(unittest.TestCase):
    """The rule on plain data."""

    def hashes(self, flagged):
        return sorted(f.vhash for f in flagged)

    def test_scraper_wave_is_flagged_in_full(self):
        wave = [land(i, at=i * 0.45) for i in range(20)]   # 20 pages in 9 s
        self.assertEqual(self.hashes(find_bursts(wave, quiet)), sorted(w.vhash for w in wave))

    def test_exactly_the_threshold_fires_and_one_less_does_not(self):
        self.assertEqual(len(find_bursts([land(i, at=i) for i in range(burst.MIN_UNIQUE)], quiet)), burst.MIN_UNIQUE)
        self.assertEqual(find_bursts([land(i, at=i) for i in range(burst.MIN_UNIQUE - 1)], quiet), [])

    def test_newsletter_blast_on_one_page_is_not_a_burst(self):
        blast = [land(i, path="/kampanje/vinter", at=i * 0.3, tagged=True) for i in range(60)]
        self.assertEqual(find_bursts(blast, quiet), [])

    def test_newsletter_blast_without_utm_on_one_page_is_not_a_burst(self):
        blast = [land(i, path="/kampanje/vinter", at=i * 0.3) for i in range(60)]
        self.assertEqual(find_bursts(blast, quiet), [])

    def test_newsletter_with_many_tagged_links_is_not_a_burst(self):
        blast = [land(i, path=f"/artikkel/{i % 12}", at=i * 0.4, tagged=True) for i in range(60)]
        self.assertEqual(find_bursts(blast, quiet), [])

    def test_the_look_ahead_is_what_tells_a_shared_page_from_a_unique_one(self):
        # eight different pages in a row ARE a burst while nobody repeats a page ...
        first8 = [land(i, path=f"/artikkel/{i}", at=i * 0.3) for i in range(8)]
        self.assertEqual(len(find_bursts(first8, quiet)), 8)
        # ... and are not once the same pages are opened again a moment later
        again = first8 + [land(10 + i, path=f"/artikkel/{i}", at=2.4 + i * 0.3) for i in range(8)]
        self.assertEqual(find_bursts(again, quiet), [])

    def test_landings_after_the_horizon_are_judged_next_time(self):
        wave = [land(i, at=i * 0.5) for i in range(20)]
        early = find_bursts(wave, quiet, {}, horizon=T0 + timedelta(seconds=5))
        self.assertTrue(early and all(f.ts <= T0 + timedelta(seconds=5) for f in early))

    def test_newsletter_with_untagged_links_to_few_pages_is_not_a_burst(self):
        # 40 people over 12 links: every page is landed on several times, none is unique
        blast = [land(i, path=f"/artikkel/{i % 12}", at=i * 0.3) for i in range(40)]
        self.assertEqual(find_bursts(blast, quiet), [])

    def test_a_person_on_a_deep_page_is_not_a_burst(self):
        self.assertEqual(find_bursts([land(1, path="/blogg/hvordan-velge-esim")], quiet), [])

    def test_slow_trickle_is_not_a_burst(self):
        slow = [land(i, at=i * 6) for i in range(30)]       # one new deep page every 6 s
        self.assertEqual(find_bursts(slow, quiet), [])

    def test_front_page_landings_are_not_candidates(self):
        self.assertEqual(find_bursts([land(i, path="/", at=i * 0.3) for i in range(30)], quiet), [])

    def test_no_country_means_no_candidate(self):
        self.assertEqual(find_bursts([land(i, country=None, at=i * 0.3) for i in range(30)], quiet), [])

    def test_mixed_countries_do_not_add_up(self):
        mixed = [land(i, country="US" if i % 2 else "DE", at=i * 0.5) for i in range(14)]   # 7 + 7
        self.assertEqual(find_bursts(mixed, quiet), [])

    def test_other_sites_do_not_add_up(self):
        mixed = [land(i, site=1 + i % 2, at=i * 0.5) for i in range(14)]
        self.assertEqual(find_bursts(mixed, quiet), [])

    def test_busy_site_with_long_tail_traffic_is_not_a_burst(self):
        """25 new Norwegian visitors per 15 s is business as usual: 10 in a window is noise."""
        busy = lambda s, c, a, b, ex: int(25 * burst.BASELINE / burst.WINDOW)  # noqa: E731
        self.assertEqual(find_bursts([land(i, country="NO", at=i * 0.5) for i in range(12)], busy), [])

    def test_the_same_wave_on_a_quiet_site_is_a_burst(self):
        wave = [land(i, country="NO", at=i * 0.5) for i in range(12)]
        self.assertEqual(len(find_bursts(wave, quiet)), 12)

    def test_baseline_excludes_what_the_run_already_flagged(self):
        seen = []

        def spy(site, country, start, end, exclude):
            seen.append(set(exclude))
            return 0
        wave = [land(i, at=i * 2) for i in range(40)]
        find_bursts(wave, spy)
        self.assertEqual(seen[0], set())

    def test_a_scraper_that_slows_down_stays_flagged_while_the_burst_is_on(self):
        wave = [land(i, at=i * 0.5) for i in range(10)] + [land(100 + i, at=8 + i * 10) for i in range(3)]
        flagged = find_bursts(wave, quiet)
        self.assertEqual(len(flagged), 13)
        late = [land(200, at=8 + 3 * 10 + 60)]          # a minute after the burst ended
        self.assertEqual(find_bursts(late, quiet), [])

    def test_active_state_carries_across_sweeps(self):
        active = {}
        find_bursts([land(i, at=i * 0.5) for i in range(10)], quiet, active)
        later = find_bursts([land(50, at=10)], quiet, active)
        self.assertEqual(self.hashes(later), ["h50"])


class BurstDbTest(IngestTestCase):
    def setUp(self):
        super().setUp()
        burst.reset()
        self.sid = self.site["id"]

    def add(self, vh, path, at, name="pageview", country="US", utm=None, rev=None, items=False):
        ts = (T0 + timedelta(seconds=at)).strftime("%Y-%m-%d %H:%M:%S")
        with store._cursor() as cur:
            cur.execute(
                "INSERT INTO events (site_id, ts, name, path, country, device, visitor_hash, utm_source, revenue_cents) "
                "VALUES (?, ?, ?, ?, ?, 'desktop', ?, ?, ?)",
                (self.sid, ts, name, path, country, vh, utm, rev),
            )
            if items:
                cur.execute("INSERT INTO event_items (site_id, event_id, name, qty, unit_price_cents) "
                            "VALUES (?, ?, 'Pakke', 1, 100)", (self.sid, cur.lastrowid))

    def wave(self, n=20):
        for i in range(n):
            self.add(f"bot{i}", f"/bestill/p{i}", i * 0.45)
            self.add(f"bot{i}", f"/bestill/p{i}", i * 0.45 + 1, name="checkout_start", items=(i == 0))

    def events(self):
        with store._cursor() as cur:
            cur.execute("SELECT visitor_hash, name, path FROM events WHERE site_id = ? ORDER BY id", (self.sid,))
            return [dict(r) for r in cur.fetchall()]

    def bot_rows(self):
        with store._cursor() as cur:
            cur.execute("SELECT day, bot_class, agent, path, n FROM bot_daily WHERE site_id = ?", (self.sid,))
            return [dict(r) for r in cur.fetchall()]

    def now(self):
        return T0 + timedelta(seconds=14)

    def test_sweep_moves_the_wave_and_leaves_people_alone(self):
        for i in range(5):                       # people: other country, same page, earlier visit
            self.add(f"human{i}", "/guide", i, country="NO", utm="nyhetsbrev")
        self.add("human0", "/guide", 30, name="scroll", country="NO")
        self.wave()
        res = burst.sweep(self.now())
        self.assertEqual(sum(r["events"] for r in res), 40)
        left = self.events()
        self.assertEqual(sorted({e["visitor_hash"] for e in left}), [f"human{i}" for i in range(5)])
        self.assertEqual(len(left), 6)
        self.assertEqual(self.bot_rows(), [{"day": "2026-10-27", "bot_class": "script", "agent": "burst",
                                            "path": "", "n": 40}])
        with store._cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM event_items WHERE site_id = ?", (self.sid,))
            self.assertEqual(cur.fetchone()["n"], 0)

    def test_a_burst_still_arriving_is_finished_by_the_next_sweep(self):
        self.wave()
        first = burst.sweep(T0 + timedelta(seconds=6))      # only the first part is judged yet
        second = burst.sweep(T0 + timedelta(seconds=21))
        self.assertEqual(self.events(), [])
        self.assertEqual(sum(r["events"] for r in first + second), 40)
        self.assertEqual(self.bot_rows()[0]["n"], 40)

    def test_sweep_is_idempotent(self):
        self.wave()
        burst.sweep(self.now())
        again = burst.sweep(self.now() + timedelta(seconds=15))
        self.assertEqual(again, [])
        self.assertEqual(self.bot_rows()[0]["n"], 40)

    def test_two_overlapping_sweeps_count_each_event_once(self):
        self.wave()
        lands = burst.fetch_landings(T0 - timedelta(seconds=60), T0 + timedelta(seconds=60))
        flagged = find_bursts(lands, burst.db_baseline, {})
        burst.apply_bursts(flagged)
        burst.apply_bursts(flagged)     # a second process with the same view
        self.assertEqual(self.bot_rows()[0]["n"], 40)
        self.assertEqual(self.events(), [])

    def test_newsletter_blast_is_untouched(self):
        for i in range(60):
            self.add(f"news{i}", "/kampanje/vinter", i * 0.2, utm="nyhetsbrev")
        for i in range(60):
            self.add(f"social{i}", "/kampanje/vinter", i * 0.2, country="NO")
        self.assertEqual(burst.sweep(self.now() + timedelta(seconds=10)), [])
        self.assertEqual(len(self.events()), 120)
        self.assertEqual(self.bot_rows(), [])

    def test_events_beyond_the_grace_period_stay(self):
        self.wave()
        self.add("bot0", "/bestill/p0", 20 + burst.GRACE + 60, name="checkout_start")
        burst.sweep(self.now())
        self.assertEqual([e["visitor_hash"] for e in self.events()], ["bot0"])

    def test_a_busy_baseline_blocks_the_sweep(self):
        for i in range(2400):           # 80 new US visitors a minute for the half hour before
            self.add(f"old{i}", f"/side/{i}", -1800 + i * 0.75)
        self.wave(12)
        self.assertEqual(burst.sweep(self.now()), [])

    def test_backfill_dry_run_changes_nothing_and_apply_moves(self):
        self.wave()
        self.add("human", "/blogg/x", 5, country="NO")
        res = burst.backfill(self.sid, T0 - timedelta(minutes=5), T0 + timedelta(minutes=5))
        self.assertEqual((res[0]["events"], res[0]["visitors"], res[0]["paths"]), (40, 20, 20))
        self.assertEqual(len(self.events()), 41)
        self.assertEqual(self.bot_rows(), [])
        burst.backfill(self.sid, T0 - timedelta(minutes=5), T0 + timedelta(minutes=5), apply=True)
        self.assertEqual([e["visitor_hash"] for e in self.events()], ["human"])
        self.assertEqual(self.bot_rows()[0]["n"], 40)
        burst.backfill(self.sid, T0 - timedelta(minutes=5), T0 + timedelta(minutes=5), apply=True)
        self.assertEqual(self.bot_rows()[0]["n"], 40)

    def test_backfill_recomputes_the_rollup_of_an_unanchored_day(self):
        self.wave()
        store.compute_rollup(self.sid, "2026-10-27")
        self.assertEqual(store.get_rollup(self.sid, "2026-10-27")["visitors"], 20)
        burst.backfill(self.sid, T0 - timedelta(minutes=5), T0 + timedelta(minutes=5), apply=True)
        self.assertEqual(store.get_rollup(self.sid, "2026-10-27")["visitors"], 0)

    def test_manage_command_is_a_dry_run_by_default(self):
        self.wave()
        args = ["burst-backfill", "--site", self.site["public_id"],
                "--from", "2026-10-27 11:20", "--to", "2026-10-27 11:40"]
        self.assertEqual(manage.main(args), 0)
        self.assertEqual(len(self.events()), 40)
        self.assertEqual(manage.main(args + ["--apply"]), 0)
        self.assertEqual(self.events(), [])
        self.assertEqual(manage.main(["burst-backfill", "--site", "x"]), 2)
        self.assertEqual(manage.main(["burst-backfill", "--site", "nope", "--from", "2026-10-27 11:20",
                                      "--to", "2026-10-27 11:40"]), 1)


if __name__ == "__main__":
    unittest.main()
