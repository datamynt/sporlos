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


def is_bot(ua: str) -> bool:
    """Tom UA eller kjent bot/script → tell ikke som besøkende."""
    if not ua or not ua.strip():
        return True
    return bool(_BOT.search(ua))


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
