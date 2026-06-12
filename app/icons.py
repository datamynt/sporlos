"""Minimalistiske inline-ikoner for dashbordets breakdown-lister.

Alt er inline SVG/emoji — ALDRI eksterne assets/CDN (personvernprodukt;
samme grunn som self-hostet font). To stilarter som deles om størrelse og
farge (14px, currentColor, dempet via CSS-klassen .ic):

  - kjente merke-glyfer (Chrome/Firefox/Opera/Apple/Android) fra Simple
    Icons (CC0) — kun de små; Safari/Linux sine var 5-12KB og er erstattet
    med håndtegnede ekvivalenter (kompass/terminal) i samme strek-stil
  - håndtegnede strek-ikoner (skjerm/telefon/nettbrett/kompass/terminal/
    ruter/globus) i Lucide-aktig stil: stroke 2, runde ender

Land vises som flagg-emoji (null bytes, null assets). NB: Windows mangler
farge-flagg i systemfonten — der degraderer det til bokstavpar (NO/SE),
som fortsatt er lesbart.
"""

from __future__ import annotations

_FILL = '<svg class=ic viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">{}</svg>'
_STROKE = (
    '<svg class=ic viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{}</svg>'
)

# Merke-glyfer (Simple Icons, CC0) — én path hver.
_CHROME = _FILL.format(
    '<path d="M12 0C8.21 0 4.831 1.757 2.632 4.501l3.953 6.848A5.454 5.454 0 0 1 12 '
    "6.545h10.691A12 12 0 0 0 12 0zM1.931 5.47A11.943 11.943 0 0 0 0 12c0 6.012 4.42 "
    "10.991 10.189 11.864l3.953-6.847a5.45 5.45 0 0 1-6.865-2.29zm13.342 2.166a5.446 "
    "5.446 0 0 1 1.45 7.09l.002.001h-.002l-5.344 9.257c.206.01.413.016.621.016 6.627 0 "
    "12-5.373 12-12 0-1.54-.29-3.011-.818-4.364zM12 16.364a4.364 4.364 0 1 1 0-8.728 "
    '4.364 4.364 0 0 1 0 8.728Z"/>'
)
_FIREFOX = _FILL.format(
    '<path d="M20.452 3.445a11.002 11.002 0 00-2.482-1.908C16.944.997 15.098.093 12.477.032c-.734'
    "-.017-1.457.03-2.174.144-.72.114-1.398.292-2.118.56-1.017.377-1.996.975-2.574 1.554.583-.349 "
    "1.476-.733 2.55-.992a10.083 10.083 0 013.729-.167c2.341.34 4.178 1.381 5.48 2.625a8.066 8.066 "
    "0 011.298 1.587c1.468 2.382 1.33 5.376.184 7.142-.85 1.312-2.67 2.544-4.37 2.53-.583-.023-1.438"
    "-.152-2.25-.566-2.629-1.343-3.021-4.688-1.118-6.306-.632-.136-1.82.13-2.646 1.363-.742 1.107-.7 "
    "2.816-.242 4.028a6.473 6.473 0 01-.59-1.895 7.695 7.695 0 01.416-3.845A8.212 8.212 0 019.45 "
    "5.399c.896-1.069 1.908-1.72 2.75-2.005-.54-.471-1.411-.738-2.421-.767C8.31 2.583 6.327 3.061 "
    "4.7 4.41a8.148 8.148 0 00-1.976 2.414c-.455.836-.691 1.659-.697 1.678.122-1.445.704-2.994 "
    "1.248-4.055-.79.413-1.827 1.668-2.41 3.042C.095 9.37-.2 11.608.14 13.989c.966 5.668 5.9 9.982 "
    '11.843 9.982C18.62 23.971 24 18.591 24 11.956a11.93 11.93 0 00-3.548-8.511z"/>'
)
_OPERA = _FILL.format(
    '<path d="M8.051 5.238c-1.328 1.566-2.186 3.883-2.246 6.48v.564c.061 2.598.918 4.912 2.246 '
    "6.479 1.721 2.236 4.279 3.654 7.139 3.654 1.756 0 3.4-.537 4.807-1.471C17.879 22.846 15.074 24 "
    "12 24c-.192 0-.383-.004-.57-.014C5.064 23.689 0 18.436 0 12 0 5.371 5.373 0 12 0h.045c3.055.012 "
    "5.84 1.166 7.953 3.055-1.408-.93-3.051-1.471-4.81-1.471-2.858 0-5.417 1.42-7.14 3.654h.003zM24 "
    "12c0 3.556-1.545 6.748-4.002 8.945-3.078 1.5-5.946.451-6.896-.205 3.023-.664 5.307-4.32 5.307-8.74 "
    '0-4.422-2.283-8.075-5.307-8.74.949-.654 3.818-1.703 6.896-.205C22.455 5.25 24 8.445 24 12z"/>'
)
_APPLE = _FILL.format(
    '<path d="M12.152 6.896c-.948 0-2.415-1.078-3.96-1.04-2.04.027-3.91 1.183-4.961 3.014-2.117 '
    "3.675-.546 9.103 1.519 12.09 1.013 1.454 2.208 3.09 3.792 3.039 1.52-.065 2.09-.987 3.935-.987 "
    "1.831 0 2.35.987 3.96.948 1.637-.026 2.676-1.48 3.676-2.948 1.156-1.688 1.636-3.325 1.662-3.415"
    "-.039-.013-3.182-1.221-3.22-4.857-.026-3.04 2.48-4.494 2.597-4.559-1.429-2.09-3.623-2.324-4.39"
    "-2.376-2-.156-3.675 1.09-4.61 1.09zM15.53 3.83c.843-1.012 1.4-2.427 1.245-3.83-1.207.052-2.662"
    '.805-3.532 1.818-.78.896-1.454 2.338-1.273 3.714 1.338.104 2.715-.688 3.559-1.701"/>'
)
_ANDROID = _FILL.format(
    '<path d="M18.4395 5.5586c-.675 1.1664-1.352 2.3318-2.0274 3.498-.0366-.0155-.0742-.0286-.1113'
    "-.043-1.8249-.6957-3.484-.8-4.42-.787-1.8551.0185-3.3544.4643-4.2597.8203-.084-.1494-1.7526"
    "-3.021-2.0215-3.4864a1.1451 1.1451 0 0 0-.1406-.1914c-.3312-.364-.9054-.4859-1.379-.203-.475"
    ".282-.7136.9361-.3886 1.5019 1.9466 3.3696-.0966-.2158 1.9473 3.3593.0172.031-.4946.2642-1.3926 "
    "1.0177C2.8987 12.176.452 14.772 0 18.9902h24c-.119-1.1108-.3686-2.099-.7461-3.0683-.7438-1.9118"
    "-1.8435-3.2928-2.7402-4.1836a12.1048 12.1048 0 0 0-2.1309-1.6875c.6594-1.122 1.312-2.2559 1.9649"
    "-3.3848.2077-.3615.1886-.7956-.0079-1.1191a1.1001 1.1001 0 0 0-.8515-.5332c-.5225-.0536-.9392"
    ".3128-1.0488.5449zm-.0391 8.461c.3944.5926.324 1.3306-.1563 1.6503-.4799.3197-1.188.0985-1.582"
    "-.4941-.3944-.5927-.324-1.3307.1563-1.6504.4727-.315 1.1812-.1086 1.582.4941zM7.207 13.5273c"
    ".4803.3197.5506 1.0577.1563 1.6504-.394.5926-1.1038.8138-1.584.4941-.48-.3197-.5503-1.0577"
    '-.1563-1.6504.4008-.6021 1.1087-.8106 1.584-.4941z"/>'
)

# Håndtegnede (samme strek-stil som resten av merkevaren)
_SAFARI = _STROKE.format(  # kompass
    '<circle cx="12" cy="12" r="10"/>'
    '<polygon points="16.2 7.8 14.1 14.1 7.8 16.2 9.9 9.9" fill="currentColor" stroke="none"/>'
)
_LINUX = _STROKE.format(  # terminal — ærlig og umiddelbart gjenkjennelig for målgruppen
    '<polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/>'
)
_WINDOWS = _FILL.format('<path d="M4 4h7.2v7.2H4zM12.8 4H20v7.2h-7.2zM4 12.8h7.2V20H4zM12.8 12.8H20V20h-7.2z"/>')
_GLOBE = _STROKE.format(
    '<circle cx="12" cy="12" r="10"/><path d="M2 12h20"/>'
    '<path d="M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/>'
)
_MONITOR = _STROKE.format(
    '<rect x="2" y="4" width="20" height="13" rx="2"/><path d="M8 21h8M12 17v4"/>'
)
_PHONE = _STROKE.format('<rect x="7" y="2" width="10" height="20" rx="2"/><path d="M11 18h2"/>')
_TABLET = _STROKE.format('<rect x="4" y="2" width="16" height="20" rx="2"/><path d="M11 18h2"/>')

_BROWSERS = {
    "Chrome": _CHROME,
    "Firefox": _FIREFOX,
    "Safari": _SAFARI,
    "Opera": _OPERA,
    "Edge": _GLOBE,
    "Samsung Internet": _GLOBE,
}
_OS = {
    "Windows": _WINDOWS,
    "macOS": _APPLE,
    "iOS": _APPLE,
    "Android": _ANDROID,
    "Linux": _LINUX,
}
_DEVICES = {"desktop": _MONITOR, "mobil": _PHONE, "tablet": _TABLET}


def browser(name: str) -> str:
    return _BROWSERS.get(name, _GLOBE)


def os(name: str) -> str:
    return _OS.get(name, "")


def device(name: str) -> str:
    return _DEVICES.get(name, "")


# Land → ISO 3166-1 alpha-2 (engelske DB-IP-navn, samme nøkler som geo.COUNTRY_NO).
_COUNTRY_ISO = {
    "Norway": "NO", "Sweden": "SE", "Denmark": "DK", "Finland": "FI", "Iceland": "IS",
    "Germany": "DE", "United States": "US", "United Kingdom": "GB",
    "Netherlands": "NL", "The Netherlands": "NL", "Belgium": "BE", "France": "FR",
    "Spain": "ES", "Portugal": "PT", "Italy": "IT", "Ireland": "IE", "Austria": "AT",
    "Switzerland": "CH", "Poland": "PL", "Czechia": "CZ", "Czech Republic": "CZ",
    "Estonia": "EE", "Latvia": "LV", "Lithuania": "LT", "Russia": "RU", "Ukraine": "UA",
    "Greece": "GR", "Turkey": "TR", "Türkiye": "TR", "China": "CN", "Japan": "JP",
    "South Korea": "KR", "Republic of Korea": "KR", "India": "IN", "Brazil": "BR",
    "Mexico": "MX", "Canada": "CA", "Australia": "AU", "New Zealand": "NZ",
    "South Africa": "ZA", "Croatia": "HR", "Hungary": "HU", "Romania": "RO",
    "Bulgaria": "BG", "Slovakia": "SK", "Slovenia": "SI", "Serbia": "RS",
    "Thailand": "TH", "Vietnam": "VN", "Philippines": "PH", "Indonesia": "ID",
    "United Arab Emirates": "AE", "Saudi Arabia": "SA", "Israel": "IL", "Egypt": "EG",
    "Argentina": "AR", "Chile": "CL", "Colombia": "CO", "Singapore": "SG",
    "Hong Kong": "HK", "Taiwan": "TW", "Luxembourg": "LU", "Cyprus": "CY", "Malta": "MT",
    "North Macedonia": "MK", "Bosnia and Herzegovina": "BA", "Albania": "AL",
    "Moldova": "MD", "Belarus": "BY", "Georgia": "GE", "Armenia": "AM",
    "Azerbaijan": "AZ", "Kazakhstan": "KZ", "Pakistan": "PK", "Bangladesh": "BD",
    "Sri Lanka": "LK", "Nepal": "NP", "Morocco": "MA", "Algeria": "DZ", "Tunisia": "TN",
    "Nigeria": "NG", "Kenya": "KE", "Ethiopia": "ET", "Ghana": "GH", "Peru": "PE",
    "Venezuela": "VE", "Ecuador": "EC", "Uruguay": "UY", "Bolivia": "BO",
    "Costa Rica": "CR", "Panama": "PA", "Cuba": "CU", "Dominican Republic": "DO",
    "Greenland": "GL", "Faroe Islands": "FO",
}


def flag(country_en: str | None) -> str:
    """Flagg-emoji fra engelsk landnavn (kalles FØR norsk oversettelse)."""
    iso = _COUNTRY_ISO.get(country_en or "")
    if not iso:
        return ""
    emoji = "".join(chr(0x1F1E6 + ord(c) - 65) for c in iso)
    return f'<span class=fl aria-hidden="true">{emoji}</span>'
