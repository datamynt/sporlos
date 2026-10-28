"""Robots get their own count: classification, ingest into bot_daily (never into
`events`), the API and the dashboard section.

Run ONE FILE PER PROCESS (each suite owns its SQLite file):
    .venv/bin/python3 -m unittest tests.test_bots -v < /dev/null
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from tests.test_ingest_limits import UA, IngestTestCase

from app import botcount, main, store
from app.auth import hash_password
from app.useragent import (
    _KNOWN_AGENTS,
    _TOKEN_CLASS,
    BOT_CLASS_SHORT,
    BOT_CLASSES,
    bot_match,
    classify_ua,
    signed_agent,
)

IP = "84.208.10.20"
TODAY = lambda: datetime.now(timezone.utc).strftime("%Y-%m-%d")  # noqa: E731

GPTBOT = "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; GPTBot/1.2; +https://openai.com/gptbot"
CLAUDE_USER = "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Claude-User/1.0; +Claude-User@anthropic.com)"
PERPLEXITY_USER = "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Perplexity-User/1.0; +https://perplexity.ai/perplexity-user)"
GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"
SLACK = "Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)"
HEADLESS = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/140.0 Safari/537.36"
IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1"
SOGOU_BROWSER = "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/86.0 Safari/537.36 SE 2.X MetaSr 1.0 Sogou"


class ClassifyTest(unittest.TestCase):
    def test_known_agents(self):
        cases = {
            GPTBOT: ("ai", "GPTBot"),
            CLAUDE_USER: ("ai", "Claude-User"),
            PERPLEXITY_USER: ("ai", "Perplexity-User"),
            "Mozilla/5.0 (compatible; ClaudeBot/1.0; +claudebot@anthropic.com)": ("ai", "ClaudeBot"),
            "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; ChatGPT-User/1.0; +https://openai.com/bot": ("ai", "ChatGPT-User"),
            "Mozilla/5.0 (Linux; Android 5.0) AppleWebKit/537.36 (KHTML, like Gecko) Mobile Safari/537.36 (compatible; Bytespider; spider-feedback@bytedance.com)": ("ai", "Bytespider"),
            "meta-externalagent/1.1 (+https://developers.facebook.com/docs/sharing/webmasters/crawler)": ("ai", "meta-externalagent"),
            "CCBot/2.0 (https://commoncrawl.org/faq/)": ("ai", "CCBot"),
            GOOGLEBOT: ("search", "Googlebot"),
            "Mozilla/5.0 (compatible; Baiduspider/2.0; +http://www.baidu.com/search/spider.html)": ("search", "Baiduspider"),
            "Mozilla/5.0 (Macintosh) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15 (Applebot/0.1)": ("search", "Applebot"),
            "Mozilla/5.0 (compatible; YandexBot/3.0; +http://yandex.com/bots)": ("search", "YandexBot"),
            "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)": ("social", "facebookexternalhit"),
            SLACK: ("social", "Slackbot"),
            "WhatsApp/2.23.20.0": ("social", "WhatsApp"),
            "Mozilla/5.0 (compatible; UptimeRobot/2.0; http://www.uptimerobot.com/)": ("monitor", "UptimeRobot"),
            "Mozilla/5.0 (compatible; AhrefsBot/7.0; +http://ahrefs.com/robot/)": ("monitor", "AhrefsBot"),
            HEADLESS: ("script", "HeadlessChrome"),
            "curl/8.5.0": ("script", "curl"),
            "python-requests/2.32.3": ("script", "python-requests"),
            "Python-urllib/3.12": ("script", "python-urllib"),
            "": ("script", "empty"),
            "   ": ("script", "empty"),
            "SomeNewCrawler/1.0 (+https://example.com/crawler)": ("other", "crawl"),
            "Mozilla/5.0 LinkPreview": ("other", "preview"),
        }
        for ua, want in cases.items():
            with self.subTest(ua=ua):
                self.assertEqual(classify_ua(ua), want)

    def test_people_are_not_classified(self):
        for ua in (UA, IPHONE, SOGOU_BROWSER,
                   "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36 Edg/140.0"):
            with self.subTest(ua=ua):
                self.assertIsNone(classify_ua(ua))

    def test_classify_is_a_superset_of_bot_match(self):
        """Every UA the old filter drops is still non-human, with a class."""
        for ua in ("curl/8", "Slackbot", "x bot", "spider", "Pingdom.com_bot", "lighthouse",
                   "Mozilla/5.0 (compatible; MJ12bot/v1.4.8)", "telegram", "embedly"):
            with self.subTest(ua=ua):
                self.assertIsNotNone(bot_match(ua))
                self.assertIsNotNone(classify_ua(ua))

    def test_bot_match_behaviour_is_unchanged(self):
        self.assertEqual(bot_match(""), "empty")
        self.assertEqual(bot_match("Slackbot-LinkExpanding"), "bot")
        self.assertIsNone(bot_match(UA))
        self.assertIsNone(bot_match(CLAUDE_USER))  # new named agent, old regex untouched

    def test_every_class_is_in_the_one_list(self):
        classes = {c for _t, _n, c in _KNOWN_AGENTS} | set(_TOKEN_CLASS.values())
        self.assertLessEqual(classes, set(BOT_CLASSES))
        self.assertEqual(set(BOT_CLASS_SHORT), set(BOT_CLASSES))

    def test_signed_agent(self):
        self.assertIsNone(signed_agent({}))
        self.assertIsNone(signed_agent({"signature-input": 'sig1=("@authority");tag="other"'}))
        self.assertEqual(signed_agent({"signature-agent": '"https://chatgpt.com"'}), "ChatGPT Agent")
        self.assertEqual(signed_agent({"signature-agent": "https://agent.example"}), "Web Bot Auth")
        self.assertEqual(
            signed_agent({"signature-input": 'sig1=("@authority");created=1;tag="web-bot-auth"'}),
            "Web Bot Auth",
        )


class BotIngestTest(IngestTestCase):
    def setUp(self):
        super().setUp()
        botcount.flush()
        with botcount._lock:
            botcount._buf.clear()

    def send(self, ua=UA, ip=IP, path="/x", headers=None, **body):
        h = {"user-agent": ua, "x-forwarded-for": ip, **(headers or {})}
        payload = {"s": self.site["public_id"], "n": "pageview", "p": path, **body}
        return self.client.post("/api/event", json=payload, headers=h)

    def bot_rows(self):
        botcount.flush()
        with store._cursor() as cur:
            cur.execute(
                "SELECT day, bot_class, agent, path, n FROM bot_daily WHERE site_id = ? "
                "ORDER BY bot_class, agent, path",
                (self.site["id"],),
            )
            return [dict(r) for r in cur.fetchall()]

    def test_ai_crawler_is_counted_with_masked_path_and_never_stored_as_event(self):
        for _ in range(3):
            self.assertEqual(self.send(GPTBOT, path="/ordre/3f2a9b1c7d4e5f60?x=1").status_code, 204)
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.bot_rows(), [
            {"day": TODAY(), "bot_class": "ai", "agent": "GPTBot", "path": "/ordre/:id", "n": 3},
        ])

    def test_named_ai_fetcher_the_old_regex_missed_is_not_a_person(self):
        self.send(CLAUDE_USER, path="/guide")
        self.assertEqual(self.rows(), [])
        self.assertEqual([(r["bot_class"], r["agent"], r["path"]) for r in self.bot_rows()],
                         [("ai", "Claude-User", "/guide")])

    def test_social_and_script_keep_no_path(self):
        self.send(SLACK, path="/a")
        self.send("curl/8.5.0", path="/b")
        self.send("", path="/c")
        self.assertEqual(self.rows(), [])
        self.assertEqual([(r["bot_class"], r["agent"], r["path"], r["n"]) for r in self.bot_rows()], [
            ("script", "curl", "", 1), ("script", "empty", "", 1), ("social", "Slackbot", "", 1),
        ])

    def test_datacenter_is_counted_by_provider(self):
        with mock.patch.object(main, "datacenter_asn", return_value=24940):
            self.assertEqual(self.send(path="/priser").status_code, 204)
        self.assertEqual(self.rows(), [])
        self.assertEqual([(r["bot_class"], r["agent"], r["path"]) for r in self.bot_rows()],
                         [("datacenter", "Hetzner", "")])

    def test_webdriver_flag_counts_as_agent_never_as_a_person(self):
        self.send(path="/kasse", wd=1)
        self.send(path="/kasse", n="purchase", wd=1, rv=10000)
        self.assertEqual(self.rows(), [])
        self.assertEqual([(r["bot_class"], r["agent"], r["path"], r["n"]) for r in self.bot_rows()],
                         [("agent", "webdriver", "/kasse", 2)])

    def test_web_bot_auth_signature_is_an_agent(self):
        sig = {"signature-agent": '"https://chatgpt.com"',
               "signature-input": 'sig1=("@authority" "signature-agent");tag="web-bot-auth"'}
        self.send(path="/", headers=sig)
        self.assertEqual(self.rows(), [])
        self.assertEqual([(r["bot_class"], r["agent"]) for r in self.bot_rows()],
                         [("agent", "ChatGPT Agent")])

    def test_agent_beats_datacenter(self):
        """An AI agent's browser usually runs in a cloud: it is an agent, not 'datacenter'."""
        with mock.patch.object(main, "datacenter_asn", return_value=8075):
            self.send(wd=1)
        self.assertEqual([r["bot_class"] for r in self.bot_rows()], ["agent"])

    def test_named_ua_beats_webdriver_flag(self):
        self.send(HEADLESS, wd=1)
        self.assertEqual([(r["bot_class"], r["agent"]) for r in self.bot_rows()],
                         [("script", "HeadlessChrome")])

    def test_person_is_stored_and_adds_no_robot_row(self):
        for flag in (None, 0, False, "0"):
            body = {} if flag is None else {"wd": flag}
            self.send(**body)
        self.assertEqual(len(self.rows()), 4)
        self.assertEqual(self.bot_rows(), [])

    def test_rate_limit_drops_are_not_counted_as_robots(self):
        with mock.patch.object(main, "_MAX_EVENTS_PER_MINUTE", 2):
            for _ in range(5):
                self.send()
        self.assertEqual(len(self.rows()), 2)
        self.assertEqual(self.bot_rows(), [])

    def test_hits_wait_in_memory_until_flushed(self):
        self.send(GPTBOT)
        self.send(GPTBOT)
        with store._cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM bot_daily WHERE site_id = ?", (self.site["id"],))
            self.assertEqual(cur.fetchone()["n"], 0)
        self.assertEqual(sum(botcount.pending().values()), 2)
        self.assertEqual(self.bot_rows()[0]["n"], 2)
        self.assertEqual(botcount.pending(), {})

    def test_flush_after_the_interval(self):
        with mock.patch.object(botcount, "FLUSH_SECONDS", 0):
            self.send(GPTBOT)
        self.assertEqual(botcount.pending(), {})

    def test_db_error_keeps_the_hits_and_never_fails_the_beacon(self):
        with mock.patch.object(store, "add_bot_counts", side_effect=RuntimeError("db down")), \
                mock.patch.object(botcount, "FLUSH_SECONDS", 0), \
                self.assertLogs("sporlos", "ERROR"):
            self.assertEqual(self.send(GPTBOT).status_code, 204)
        self.assertEqual(sum(botcount.pending().values()), 1)
        self.assertEqual(self.bot_rows()[0]["n"], 1)

    def test_path_cap_folds_new_paths_into_the_total(self):
        with mock.patch.object(store, "BOT_PATH_CAP", 2):
            for p in ("/a", "/b", "/c", "/a", "/d"):
                self.send(GOOGLEBOT, path=p)
                botcount.flush()
        rows = {r["path"]: r["n"] for r in self.bot_rows()}
        self.assertEqual(rows, {"/a": 2, "/b": 1, "": 2})

    def test_unknown_class_becomes_other(self):
        botcount.note(self.site["id"], "nonsense", "x", "/p")
        self.assertEqual([(r["bot_class"], r["path"]) for r in self.bot_rows()], [("other", "")])

    def test_add_is_additive_across_flushes(self):
        key = (self.site["id"], TODAY(), "ai", "GPTBot", "/")
        store.add_bot_counts({key: 2})
        store.add_bot_counts({key: 3})
        self.assertEqual(self.bot_rows()[0]["n"], 5)

    def test_summary_window_and_retention(self):
        sid = self.site["id"]
        old = (datetime.now(timezone.utc) - timedelta(days=100)).strftime("%Y-%m-%d")
        ten = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d")
        store.add_bot_counts({
            (sid, TODAY(), "ai", "GPTBot", "/a"): 5,
            (sid, TODAY(), "ai", "ClaudeBot", "/b"): 2,
            (sid, TODAY(), "search", "Googlebot", "/a"): 4,
            (sid, ten, "script", "curl", ""): 7,
            (sid, old, "script", "curl", ""): 9,
        })
        week = store.bot_summary(sid, 7)
        self.assertEqual(week["total"], 11)
        self.assertEqual(week["classes"], [{"class": "ai", "n": 7}, {"class": "search", "n": 4}])
        self.assertEqual(week["agents"][0], {"agent": "GPTBot", "class": "ai", "n": 5})
        self.assertEqual(week["ai_paths"], [{"path": "/a", "n": 5}, {"path": "/b", "n": 2}])
        self.assertEqual(store.bot_summary(sid, 30)["total"], 18)
        store.retention_sweep(90)
        with store._cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM bot_daily WHERE site_id = ? AND day = ?", (sid, old))
            self.assertEqual(cur.fetchone()["n"], 0)
        self.assertEqual(store.bot_summary(sid, 30)["total"], 18)


class BotApiAndDashboardTest(IngestTestCase):
    def setUp(self):
        super().setUp()
        email = f"bots-{self.site['id']}@example.no"
        tid, _uid = store.create_account("Robot AS", email, hash_password("passord-123"))
        self.site = store.create_site(tid, f"robot-{self.site['id']}.example")
        self.key = store.create_api_key(tid, "test")["key"]
        self.email = email

    def api(self, **params):
        return self.client.get(
            "/api/v1/bots", params={"site": self.site["public_id"], **params},
            headers={"Authorization": f"Bearer {self.key}"},
        )

    def seed(self):
        sid = self.site["id"]
        store.add_bot_counts({
            (sid, TODAY(), "ai", "GPTBot", "/guide/esim"): 6,
            (sid, TODAY(), "agent", "webdriver", "/kasse"): 2,
            (sid, TODAY(), "datacenter", "AWS", ""): 3,
            (sid, TODAY(), "other", "bot", ""): 1,
        })

    def test_api_shape(self):
        self.seed()
        botcount.note(self.site["id"], "ai", "GPTBot", "/guide/esim")  # buffered: API flushes first
        r = self.api(period="7")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["period_days"], 7)
        self.assertEqual(d["unit"], "hits")
        self.assertEqual(d["total"], 13)
        self.assertEqual(d["classes"][0], {"class": "ai", "label": BOT_CLASSES["ai"], "hits": 7})
        self.assertEqual(d["agents"][0], {"agent": "GPTBot", "class": "ai", "hits": 7})
        self.assertEqual(d["ai_paths"], [{"path": "/guide/esim", "hits": 7}])
        self.assertIn("JavaScript", d["note"])

    def test_api_auth_and_validation(self):
        self.assertEqual(self.client.get("/api/v1/bots", params={"site": self.site["public_id"]}).status_code, 401)
        self.assertEqual(self.api(period="9").status_code, 400)
        self.assertEqual(self.api(limit="x").status_code, 400)
        other = store.create_site(store.create_tenant("Annen"), "annen-robot.example")
        r = self.client.get("/api/v1/bots", params={"site": other["public_id"]},
                            headers={"Authorization": f"Bearer {self.key}"})
        self.assertEqual(r.status_code, 404)

    def dashboard(self):
        from starlette.testclient import TestClient

        c = TestClient(main.app)
        c.post("/login", data={"email": self.email, "password": "passord-123"})
        r = c.get(f"/app?site={self.site['public_id']}&period=7")
        self.assertEqual(r.status_code, 200)
        return r.text

    def test_dashboard_section(self):
        self.seed()
        html = self.dashboard()
        self.assertIn("Roboter og KI-agenter", html)
        self.assertIn('class="card block bots"', html)
        self.assertIn("GPTBot", html)
        self.assertIn("/guide/esim", html)
        self.assertIn("automatisert nettleser (webdriver)", html)
        self.assertIn("ukjent («bot»)", html)
        self.assertIn("De fleste KI-crawlere gjør ikke det", html)  # the honesty note

    def test_dashboard_without_robots_has_no_section(self):
        self.assertNotIn("Roboter og KI-agenter", self.dashboard())

    def test_dashboard_quiet_period(self):
        old = (datetime.now(timezone.utc) - timedelta(days=20)).strftime("%Y-%m-%d")
        store.add_bot_counts({(self.site["id"], old, "ai", "GPTBot", "/"): 1})
        html = self.dashboard()
        self.assertIn("Roboter og KI-agenter", html)
        self.assertIn("Ingen roboter registrert", html)
        self.assertNotIn("Roboter telles fra", html)  # counting predates the 7-day period

    def test_first_bot_day_is_a_string_on_postgres(self):
        # Postgres hands back bot_daily.day as a date; the dashboard compares it to a string.
        from contextlib import contextmanager
        from datetime import date

        cur = mock.Mock()
        cur.fetchone.return_value = {"d": date(2026, 10, 26)}

        @contextmanager
        def fake_cursor():
            yield cur

        with mock.patch.object(store, "_cursor", fake_cursor):
            self.assertEqual(store.first_bot_day(self.site["id"]), "2026-10-26")

    def test_dashboard_notes_when_counting_started(self):
        self.seed()  # today only: a 7-day period reaches back before the first count
        today = datetime.now(timezone.utc).strftime("%d.%m.%Y")
        self.assertIn(f"Roboter telles fra {today}", self.dashboard())


if __name__ == "__main__":
    unittest.main()
