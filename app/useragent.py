"""Lettvekts User-Agent-parsing + bot-deteksjon.

UA-strengen lagres ALDRI — vi utleder bare grove kategorier (enhet/nettleser/OS)
og forkaster resten. Ingen fingerprinting, ingen PII.
"""

from __future__ import annotations

import re

_BOT = re.compile(
    r"bot|crawl|spider|slurp|bingpreview|facebookexternalhit|embedly|preview|"
    r"pinterest|whatsapp|telegram|headless|monitor|uptime|curl|wget|"
    r"python-requests|axios|go-http|libwww|scrapy|semrush|ahrefs|lighthouse|"
    r"gtmetrix|pingdom|dataprovider|phantom|selenium",
    re.I,
)


def bot_match(ua: str) -> str | None:
    """Why a UA counts as a bot: "empty" for a blank UA, otherwise the lower-cased
    regex token that matched (`bot`, `preview`, `curl` ...), or None for a normal
    browser. The token comes from the fixed pattern above, never from free text
    in the UA, so it is safe to log (the UA string itself is not)."""
    if not ua or not ua.strip():
        return "empty"
    m = _BOT.search(ua)
    return m.group(0).lower() if m else None


def is_bot(ua: str) -> bool:
    """Tom UA eller kjent bot/script → tell ikke som besøkende."""
    return bot_match(ua) is not None


def parse_ua(ua: str) -> tuple[str, str, str]:
    """(device, browser, os) — grove kategorier for breakdowns."""
    u = ua.lower()

    # OS
    if "windows" in u:
        os_ = "Windows"
    elif "iphone" in u or "ipad" in u or "ios" in u:
        os_ = "iOS"
    elif "mac os x" in u or "macintosh" in u:
        os_ = "macOS"
    elif "android" in u:
        os_ = "Android"
    elif "linux" in u:
        os_ = "Linux"
    else:
        os_ = "Annet"

    # Enhet (Android-mobil har "mobile"; Android-tablet har det ikke)
    if "ipad" in u or "tablet" in u or ("android" in u and "mobile" not in u):
        device = "tablet"
    elif "mobile" in u or "iphone" in u or "android" in u:
        device = "mobil"
    else:
        device = "desktop"

    # Nettleser (rekkefølge er viktig: Edge/Opera/Samsung før Chrome; Chrome før Safari)
    if "edg" in u:
        browser = "Edge"
    elif "opr" in u or "opera" in u:
        browser = "Opera"
    elif "samsungbrowser" in u:
        browser = "Samsung Internet"
    elif "firefox" in u or "fxios" in u:
        browser = "Firefox"
    elif "chrome" in u or "crios" in u:
        browser = "Chrome"
    elif "safari" in u:
        browser = "Safari"
    else:
        browser = "Annet"

    return device, browser, os_


# --- Non-human traffic: class + agent name -------------------------------------------
# Robots get their own count (store.bot_daily), never a place in `events`. Every
# non-human hit is put in ONE of these classes. This dict is the single place the set
# is defined; the dashboard labels come from here too (the product UI is Norwegian).
BOT_CLASSES: dict[str, str] = {
    "ai": "KI-crawlere og -assistenter",       # AI training/search crawlers + assistants fetching for a user
    "agent": "KI-agenter i nettleser",          # an AI agent driving a real browser (Web Bot Auth / webdriver)
    "search": "Søkemotorer",                    # classic search-engine crawlers
    "social": "Lenkeforhåndsvisninger",         # link previews (Slack, Facebook, WhatsApp ...)
    "monitor": "Overvåking og SEO-verktøy",     # uptime checks, Lighthouse, SEO auditors
    "script": "Skript og verktøy",              # curl/python/headless/empty UA
    "datacenter": "Datasentre",                 # ordinary browser UA from a cloud/hosting network
    "other": "Andre roboter",                   # says it is a bot, but not one we have a name for
}
# Short form of the same labels, for a column next to a robot's name.
BOT_CLASS_SHORT: dict[str, str] = {
    "ai": "KI", "agent": "KI-agent", "search": "søk", "social": "forhåndsvisning",
    "monitor": "overvåking", "script": "skript", "datacenter": "datasenter", "other": "annet",
}

# Known user agents → (canonical agent name, class). Case-insensitive substring match,
# FIRST hit wins, so a more specific token must come before a shorter one it contains.
# Only the canonical name below is ever stored, never the UA string itself.
_KNOWN_AGENTS: tuple[tuple[str, str, str], ...] = (
    # AI crawlers and assistants (training, AI search, and fetches on a user's behalf)
    ("chatgpt-user", "ChatGPT-User", "ai"),
    ("oai-searchbot", "OAI-SearchBot", "ai"),
    ("gptbot", "GPTBot", "ai"),
    ("claude-user", "Claude-User", "ai"),
    ("claude-searchbot", "Claude-SearchBot", "ai"),
    ("claudebot", "ClaudeBot", "ai"),
    ("claude-web", "Claude-Web", "ai"),
    ("anthropic-ai", "anthropic-ai", "ai"),
    ("perplexity-user", "Perplexity-User", "ai"),
    ("perplexitybot", "PerplexityBot", "ai"),
    ("mistralai-user", "MistralAI-User", "ai"),
    ("duckassistbot", "DuckAssistBot", "ai"),
    ("meta-externalagent", "meta-externalagent", "ai"),
    ("meta-externalfetcher", "meta-externalfetcher", "ai"),
    ("facebookbot", "FacebookBot", "ai"),
    ("bytespider", "Bytespider", "ai"),
    ("ccbot", "CCBot", "ai"),
    ("amazonbot", "Amazonbot", "ai"),
    ("google-cloudvertexbot", "Google-CloudVertexBot", "ai"),
    ("cohere-ai", "cohere-ai", "ai"),
    ("cohere-training-data-crawler", "cohere-ai", "ai"),
    ("youbot", "YouBot", "ai"),
    ("diffbot", "Diffbot", "ai"),
    ("ai2bot", "AI2Bot", "ai"),
    ("timpibot", "Timpibot", "ai"),
    ("imagesiftbot", "ImagesiftBot", "ai"),
    ("omgilibot", "Omgilibot", "ai"),
    # Search engines
    ("google-inspectiontool", "Google-InspectionTool", "search"),
    ("storebot-google", "Storebot-Google", "search"),
    ("adsbot-google", "AdsBot-Google", "search"),
    ("mediapartners-google", "Mediapartners-Google", "search"),
    ("googleother", "GoogleOther", "search"),
    ("googlebot", "Googlebot", "search"),
    ("bingpreview", "BingPreview", "search"),
    ("bingbot", "Bingbot", "search"),
    ("applebot", "Applebot", "search"),
    ("yandex.com/bots", "YandexBot", "search"),
    ("baiduspider", "Baiduspider", "search"),
    ("duckduckbot", "DuckDuckBot", "search"),
    ("petalbot", "PetalBot", "search"),
    ("seznambot", "SeznamBot", "search"),
    ("qwantbot", "Qwantbot", "search"),
    ("mojeekbot", "MojeekBot", "search"),
    ("naver.me/spd", "Yeti (Naver)", "search"),
    ("sogou web spider", "Sogou", "search"),  # not bare "sogou": Sogou's own browser says that too
    ("slurp", "Yahoo Slurp", "search"),
    # Link previews
    ("facebookexternalhit", "facebookexternalhit", "social"),
    ("facebot", "Facebot", "social"),
    ("twitterbot", "Twitterbot", "social"),
    ("linkedinbot", "LinkedInBot", "social"),
    ("slackbot", "Slackbot", "social"),
    ("slack-imgproxy", "Slackbot", "social"),
    ("discordbot", "Discordbot", "social"),
    ("telegrambot", "TelegramBot", "social"),
    ("whatsapp", "WhatsApp", "social"),
    ("pinterest", "Pinterest", "social"),
    ("redditbot", "redditbot", "social"),
    ("skypeuripreview", "Skype", "social"),
    ("mastodon/", "Mastodon", "social"),
    ("embedly", "Embedly", "social"),
    ("iframely", "Iframely", "social"),
    # Monitoring, performance and SEO tools
    ("uptimerobot", "UptimeRobot", "monitor"),
    ("pingdom", "Pingdom", "monitor"),
    ("statuscake", "StatusCake", "monitor"),
    ("site24x7", "Site24x7", "monitor"),
    ("better uptime", "Better Stack", "monitor"),
    ("betterstack", "Better Stack", "monitor"),
    ("uptime-kuma", "Uptime Kuma", "monitor"),
    ("hetrixtools", "HetrixTools", "monitor"),
    ("updown.io", "updown.io", "monitor"),
    ("checkly", "Checkly", "monitor"),
    ("datadogsynthetics", "Datadog", "monitor"),
    ("newrelicpinger", "New Relic", "monitor"),
    ("chrome-lighthouse", "Lighthouse", "monitor"),
    ("lighthouse", "Lighthouse", "monitor"),
    ("gtmetrix", "GTmetrix", "monitor"),
    ("semrush", "SemrushBot", "monitor"),
    ("ahrefs", "AhrefsBot", "monitor"),
    ("mj12bot", "MJ12bot", "monitor"),
    ("dotbot", "DotBot", "monitor"),
    ("rogerbot", "rogerbot", "monitor"),
    ("screaming frog", "Screaming Frog", "monitor"),
    ("siteauditbot", "SiteAuditBot", "monitor"),
    ("dataforseobot", "DataForSeoBot", "monitor"),
    ("blexbot", "BLEXBot", "monitor"),
    ("serpstatbot", "serpstatbot", "monitor"),
    ("dataprovider", "Dataprovider", "monitor"),
    # Scripts, HTTP libraries and automated browsers that say so in the UA
    ("headlesschrome", "HeadlessChrome", "script"),
    ("phantomjs", "PhantomJS", "script"),
    ("selenium", "Selenium", "script"),
    ("puppeteer", "Puppeteer", "script"),
    ("playwright", "Playwright", "script"),
    ("curl/", "curl", "script"),
    ("wget", "Wget", "script"),
    ("python-requests", "python-requests", "script"),
    ("python-urllib", "python-urllib", "script"),
    ("python-httpx", "python-httpx", "script"),
    ("aiohttp", "aiohttp", "script"),
    ("axios", "axios", "script"),
    ("node-fetch", "node-fetch", "script"),
    ("undici", "undici", "script"),
    ("go-http-client", "Go-http-client", "script"),
    ("okhttp", "okhttp", "script"),
    ("apache-httpclient", "Apache-HttpClient", "script"),
    ("java/", "Java", "script"),
    ("libwww-perl", "libwww-perl", "script"),
    ("scrapy", "Scrapy", "script"),
    ("postmanruntime", "PostmanRuntime", "script"),
)

# bot_match() tokens that have no table entry above: which class the bare token means.
# "bot", "crawl", "spider" and "preview" say "a robot" but not which one → "other".
_TOKEN_CLASS = {
    "empty": "script", "curl": "script", "headless": "script", "go-http": "script",
    "libwww": "script", "monitor": "monitor", "uptime": "monitor",
    "telegram": "social", "preview": "other",
}


def classify_ua(ua: str) -> tuple[str, str] | None:
    """(class, agent) for a user agent that is not a person, else None.

    Superset of bot_match(): a UA that names a known AI fetcher or HTTP library is
    non-human even when the old regex misses it (Claude-User, Perplexity-User,
    python-urllib ...). `agent` is the canonical name from _KNOWN_AGENTS, or the
    bare bot_match() token ("bot", "crawl", "empty" ...) for an unnamed robot."""
    if not ua or not ua.strip():
        return ("script", "empty")
    u = ua.lower()
    for token, name, cls in _KNOWN_AGENTS:
        if token in u:
            return (cls, name)
    tok = bot_match(ua)
    if tok is None:
        return None
    return (_TOKEN_CLASS.get(tok, "other"), tok)


_NAMED = frozenset(name for _t, name, _c in _KNOWN_AGENTS)


def is_named_agent(agent: str) -> bool:
    """True for a canonical name from _KNOWN_AGENTS (not a bare bot_match token)."""
    return agent in _NAMED


# Web Bot Auth (HTTP Message Signatures, draft-meunier-web-bot-auth-architecture):
# an agent operator signs each request and names itself in `Signature-Agent`. We
# do NOT verify the signature (that needs the operator's key directory); a forged
# header can only take the sender out of the human count, so there is no gain in
# forging it. Known operators get a name, everyone else a generic one, so a sender
# cannot grow the table with free-text names.
_SIGNATURE_AGENTS = {
    "chatgpt.com": "ChatGPT Agent",
    "openai.com": "ChatGPT Agent",
}


def signed_agent(headers) -> str | None:
    """Agent name when the request carries Web Bot Auth headers, else None."""
    sig_agent = (headers.get("signature-agent") or "").strip().strip('"').lower()
    sig_input = (headers.get("signature-input") or "").lower()
    if not sig_agent and "web-bot-auth" not in sig_input:
        return None
    host = sig_agent.split("://", 1)[-1].split("/", 1)[0]
    for known, name in _SIGNATURE_AGENTS.items():
        if host == known or host.endswith("." + known):
            return name
    return "Web Bot Auth"
