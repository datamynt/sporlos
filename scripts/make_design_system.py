"""Bygg designsystem-bundle for claude.ai/design (DesignSync).

Genererer standalone preview-kort (HTML m/ innbakt Schibsted Grotesk) til
design-system/ — pushes til Claude Design-prosjektet «Sporløs» der Thomas
kan iterere visuelt. Kjør på nytt etter endringer og re-push.

    .venv/bin/python3 scripts/make_design_system.py  < /dev/null
"""

from __future__ import annotations

import base64
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "design-system"

_font_b64 = base64.b64encode((ROOT / "static" / "schibsted-grotesk.woff2").read_bytes()).decode()

BASE_CSS = f"""
@font-face{{font-family:'Schibsted Grotesk';font-style:normal;font-weight:400 900;
font-display:swap;src:url(data:font/woff2;base64,{_font_b64}) format('woff2')}}
:root{{--bg:#faf9f6;--ink:#17263e;--muted:#5f6b7d;--accent:#2f6fed;--accent-deep:#1d4ed8;
--line:#e8e6e0;--card:#ffffff;--ok:#15803d;
font:17px/1.65 'Schibsted Grotesk',system-ui,sans-serif;color:var(--ink)}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);-webkit-font-smoothing:antialiased;padding:2rem}}
h2{{font-size:1.15rem;letter-spacing:-.02em;margin:0 0 1rem}}
h3{{font-size:.95rem;margin:0 0 .5rem}}
p.note{{color:var(--muted);font-size:.82rem;max-width:46rem}}
.row{{display:flex;gap:1.4rem;flex-wrap:wrap;align-items:flex-start}}
.cell{{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:1.3rem;text-align:center;min-width:150px}}
.cell small{{display:block;color:var(--muted);font-size:.75rem;margin-top:.6rem;line-height:1.4}}
.dark{{--bg:#10141f;--card:#171c2a;--line:#242b3d;--ink:#e7eaf3;--muted:#97a0b5;
--accent:#6e96ff;--accent-deep:#8fb0ff;background:var(--bg);color:var(--ink)}}
"""


def page(title: str, group: str, body: str, dark: bool = False) -> str:
    cls = ' class="dark"' if dark else ""
    return f"""<!-- @dsCard group="{group}" -->
<!doctype html><html lang=no><meta charset=utf-8><title>{title}</title>
<style>{BASE_CSS}</style><body{cls}>
{body}
</body></html>"""


# ---------------------------------------------------------------- ø-merket
def _mark(inner: str, label: str, sub: str = "") -> str:
    return (
        f'<div class=cell><svg width=84 height=84 viewBox="0 0 64 64" '
        f'style="color:var(--accent)">{inner}</svg><b style="font-size:.85rem">{label}</b>'
        f"<small>{sub}</small></div>"
    )


_CUR = (
    '<circle cx="32" cy="32" r="16" fill="none" stroke="currentColor" stroke-width="7"/>'
    '<line x1="17" y1="51" x2="47" y2="13" stroke="currentColor" stroke-width="7" stroke-linecap="round"/>'
)
_MAALING = (
    '<circle cx="32" cy="32" r="16" fill="none" stroke="currentColor" stroke-width="7"/>'
    '<polyline points="15,52 27,38 33,43 49,12" fill="none" stroke="currentColor" '
    'stroke-width="7" stroke-linecap="round" stroke-linejoin="round"/>'
)
_SEGL = (
    '<circle cx="32" cy="32" r="22" fill="none" stroke="currentColor" stroke-width="2.5"/>'
    '<circle cx="32" cy="32" r="13" fill="none" stroke="currentColor" stroke-width="6.5"/>'
    '<line x1="18" y1="50" x2="46" y2="14" stroke="currentColor" stroke-width="6.5" stroke-linecap="round"/>'
)
_NEGATIV = (
    '<circle cx="32" cy="32" r="26" fill="currentColor"/>'
    '<line x1="16" y1="52" x2="48" y2="12" stroke="var(--bg)" stroke-width="8" stroke-linecap="round"/>'
)
_PRESISJON = (
    '<circle cx="32" cy="32" r="16" fill="none" stroke="currentColor" stroke-width="6.5"/>'
    '<line x1="17" y1="51" x2="47" y2="13" stroke="var(--bg)" stroke-width="13" stroke-linecap="round"/>'
    '<line x1="17" y1="51" x2="47" y2="13" stroke="currentColor" stroke-width="6.5" stroke-linecap="round"/>'
)
_ANIM = (
    '<style>@keyframes dr{to{stroke-dashoffset:0}}'
    ".an1{stroke-dasharray:101;stroke-dashoffset:101;animation:dr .9s .2s ease-out forwards}"
    ".an2{stroke-dasharray:49;stroke-dashoffset:49;animation:dr .5s 1s ease-out forwards}</style>"
    '<circle class=an1 cx="32" cy="32" r="16" fill="none" stroke="currentColor" stroke-width="7"/>'
    '<line class=an2 x1="17" y1="51" x2="47" y2="13" stroke="currentColor" stroke-width="7" stroke-linecap="round"/>'
)

OMERKE = page(
    "ø-merket — retninger",
    "Brand",
    "<h2>ø-merket: dagens + 4 retninger</h2>"
    '<p class=note>ø = sirkel med strek = «ingen sporing». Spørsmålet er ikke å bytte idé, '
    "men hvor mye presisjon/karakter vi gir den. Alle holder seg i én farge og fungerer i 16px favicon.</p>"
    "<div class=row>"
    + _mark(_CUR, "Dagens", "ren geometri, trygg")
    + _mark(_PRESISJON, "Presisjon", "luft der streken krysser ringen — monogram-følelse, koster ingenting")
    + _mark(_MAALING, "Måling", "streken er en kurve som stiger — produktet inn i merket")
    + _mark(_SEGL, "Segl", "dobbel ring — stempel/segl, rimer med «verifiserbare tall»")
    + _mark(_NEGATIV, "Negativ", "solid disk — tyngde, beste app-ikon-kandidaten")
    + _mark(_ANIM, "Tegnes (last på nytt)", "wow-øyeblikk på landing: merket tegner seg selv")
    + "</div>",
)

# ---------------------------------------------------------------- farger
def _sw(var: str, name: str, hexv: str) -> str:
    return (
        f'<div class=cell style="min-width:118px;padding:.9rem">'
        f'<div style="background:{var};border:1px solid var(--line);border-radius:9px;height:54px"></div>'
        f'<b style="font-size:.78rem">{name}</b><small>{hexv}</small></div>'
    )


FARGER = page(
    "Farger — lys + mørk",
    "Colors",
    "<h2>Lys (papir — dagens)</h2><div class=row>"
    + _sw("var(--bg)", "papir", "#faf9f6") + _sw("var(--ink)", "blekk", "#17263e")
    + _sw("var(--muted)", "dempet", "#5f6b7d") + _sw("var(--accent)", "aksent", "#2f6fed")
    + _sw("var(--accent-deep)", "aksent dyp", "#1d4ed8") + _sw("var(--line)", "linje", "#e8e6e0")
    + "</div>"
    '<div class=dark style="border-radius:16px;padding:1.4rem;margin-top:1.6rem">'
    "<h2>Mørk (forslag — dashbord/demo)</h2><div class=row>"
    + _sw("#10141f", "natt", "#10141f") + _sw("#171c2a", "kort", "#171c2a")
    + _sw("#e7eaf3", "blekk", "#e7eaf3") + _sw("#97a0b5", "dempet", "#97a0b5")
    + _sw("#6e96ff", "aksent", "#6e96ff") + _sw("#242b3d", "linje", "#242b3d")
    + "</div>"
    '<p class=note>Marine-natt (ikke svart) — samme familie som blekket #17263e. '
    "Aksenten lysnes for kontrast på mørk flate.</p></div>",
)

# ---------------------------------------------------------------- typografi
TYPO = page(
    "Typografi",
    "Type",
    "<h2>Schibsted Grotesk — self-hostet, norsk</h2>"
    '<div style="display:grid;gap:.9rem">'
    '<div style="font-size:3.2rem;font-weight:800;letter-spacing:-.035em;line-height:1.05">'
    "Webanalyse uten sporing</div>"
    '<div style="font-size:1.6rem;font-weight:700;letter-spacing:-.02em">Overskrift h2 — 1.6rem/700</div>'
    '<div style="font-size:1.15rem;font-weight:600">Kortoverskrift — 1.15rem/600</div>'
    "<div>Brødtekst 17px/1.65 — Måler besøk uten cookies, uten IP-lagring og uten samtykkebanner. æøå ÆØÅ 0123456789</div>"
    '<div style="color:var(--muted);font-size:.85rem">Dempet .85rem — forklaringer, fotnoter</div>'
    '<div style="font-size:2.6rem;font-weight:800;letter-spacing:-.03em">'
    'Tall: <span style="font-variant-numeric:tabular-nums">12 847</span> '
    '<span style="color:var(--ok);font-size:1rem">↑ 23 %</span></div></div>'
    '<p class=note style="margin-top:1rem">Mulig wow-grep: størrelses-kontrasten økes — display enda større '
    "(4rem+) og alt sekundært mindre/dempet. Minimalisme med selvtillit = få elementer, stor skala.</p>",
)

# ---------------------------------------------------------------- KPI-kort
def _kpi(label: str, val: str, delta: str, good: bool = True) -> str:
    col = "var(--ok)" if good else "#b91c1c"
    return (
        f'<div class=cell style="text-align:left;min-width:160px"><small style="margin:0 0 .2rem">{label}</small>'
        f'<div style="font-size:1.9rem;font-weight:800;letter-spacing:-.02em;font-variant-numeric:tabular-nums">{val}'
        f'</div><span style="color:{col};font-size:.8rem">{delta} <span style="color:var(--muted)">mot forrige 7 dager</span></span></div>'
    )


KPI = page(
    "KPI-kort",
    "Components",
    "<h2>KPI-rad</h2><div class=row>"
    + _kpi("Unike besøkende", "1 482", "↑ 23 %")
    + _kpi("Sidevisninger", "5 211", "↑ 8 %")
    + _kpi("Fluktfrekvens", "42 %", "↓ 5 %")
    + _kpi("Visn. per besøk", "3,5", "± 0 %")
    + "</div>",
)

# ---------------------------------------------------------------- stat-tabell m/ ikoner
_IC = (
    '<svg style="width:14px;height:14px;vertical-align:-2px;margin-right:.45rem;color:var(--muted);opacity:.8" '
    'viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
    'stroke-linejoin="round">{}</svg>'
)
_MON = _IC.format('<rect x="2" y="4" width="20" height="13" rx="2"/><path d="M8 21h8M12 17v4"/>')
_PHN = _IC.format('<rect x="7" y="2" width="10" height="20" rx="2"/><path d="M11 18h2"/>')


def _trow(label: str, n: str, pct: int) -> str:
    return (
        f'<tr><td style="background:linear-gradient(90deg,#e9effd {pct}%,transparent {pct}%);'
        f'border-radius:4px;padding:.42rem .45rem;border-bottom:1px solid var(--line)">{label}</td>'
        f'<td style="text-align:right;color:var(--muted);border-bottom:1px solid var(--line);width:4rem">{n}</td></tr>'
    )


TABELL = page(
    "Breakdown-tabell",
    "Components",
    "<h2>Breakdown med ikoner + andelssøyler</h2>"
    '<div class=row><div class=cell style="text-align:left;min-width:280px"><h3>Land</h3>'
    '<table style="border-collapse:collapse;width:100%;font-size:.92rem">'
    + _trow("🇳🇴 Norge", "1 116", 100) + _trow("🇺🇸 USA", "271", 24)
    + _trow("🇳🇱 Nederland", "114", 10) + _trow("🇩🇪 Tyskland", "84", 8)
    + "</table></div>"
    '<div class=cell style="text-align:left;min-width:280px"><h3>Enheter</h3>'
    '<table style="border-collapse:collapse;width:100%;font-size:.92rem">'
    + _trow(_MON + "desktop", "812", 100) + _trow(_PHN + "mobil", "402", 50)
    + "</table></div></div>",
)

# ---------------------------------------------------------------- knapper + plan
KNAPPER = page(
    "Knapper og plan-kort",
    "Components",
    "<h2>Handlinger</h2><div class=row>"
    '<a href="#" style="background:var(--accent);color:#fff;padding:.62rem 1.25rem;border-radius:9px;'
    'text-decoration:none;font-weight:600">Prøv gratis i 30 dager</a>'
    '<a href="#" style="border:1px solid var(--line);color:var(--ink);padding:.62rem 1.25rem;'
    'border-radius:9px;text-decoration:none;font-weight:600;background:var(--card)">Se live-demo</a>'
    '<a href="#" style="color:var(--accent-deep)">Tekstlenke →</a></div>'
    '<h2 style="margin-top:1.6rem">Plan-kort (Vekst uthevet)</h2><div class=row>'
    '<div class=cell style="text-align:left;min-width:200px"><h3>Liten</h3>'
    '<div style="font-size:1.7rem;font-weight:800">99 kr<span style="font-size:.85rem;'
    'color:var(--muted);font-weight:400">/mnd</span></div><small>10 000 visninger · 1 nettsted</small></div>'
    '<div class=cell style="text-align:left;min-width:200px;border:2px solid var(--accent);'
    'box-shadow:0 8px 24px rgba(47,111,237,.10)"><h3 style="color:var(--accent-deep)">Vekst</h3>'
    '<div style="font-size:1.7rem;font-weight:800">249 kr<span style="font-size:.85rem;'
    'color:var(--muted);font-weight:400">/mnd</span></div><small>100 000 visninger · 10 nettsteder</small></div>'
    "</div>",
)

# ---------------------------------------------------------------- mørkt dashbord
DARK = page(
    "Dashbord i mørk modus (utkast)",
    "Dark mode",
    "<h2>Mørk modus — dashbord-utkast</h2><div class=row>"
    + _kpi("Unike besøkende", "1 482", "↑ 23 %")
    + _kpi("Sidevisninger", "5 211", "↑ 8 %")
    + "</div>"
    '<div class=cell style="text-align:left;min-width:340px;margin-top:1rem"><h3>Unike besøkende · siste 7 dager</h3>'
    '<svg viewBox="0 0 340 80" style="width:100%">'
    '<defs><linearGradient id=g x1=0 y1=0 x2=0 y2=1>'
    '<stop offset=0 stop-color="#6e96ff" stop-opacity=".30"/>'
    '<stop offset=1 stop-color="#6e96ff" stop-opacity="0"/></linearGradient></defs>'
    '<polygon fill=url(#g) points="0,70 0,52 56,44 113,58 170,30 226,38 283,18 340,26 340,70"/>'
    '<polyline fill=none stroke="#6e96ff" stroke-width="2.5" stroke-linecap=round '
    'points="0,52 56,44 113,58 170,30 226,38 283,18 340,26"/></svg></div>'
    '<p class=note>Auto via systeminnstilling (prefers-color-scheme) på dashbord + /demo. '
    "Forsiden beholder papir-identiteten.</p>",
    dark=True,
)

# ---------------------------------------------------------------- hero-retning
HERO = page(
    "Landing-hero: wow-retning",
    "Landing",
    '<div style="position:relative;overflow:hidden;border-radius:16px;border:1px solid var(--line);'
    'background:var(--card);padding:3.4rem 2.6rem">'
    '<svg viewBox="0 0 64 64" style="position:absolute;right:-90px;top:-70px;width:380px;'
    'color:var(--accent);opacity:.05">' + _CUR + "</svg>"
    '<div style="font-size:.85rem;color:var(--accent-deep);font-weight:600;letter-spacing:.06em;'
    'text-transform:uppercase">Norsk webanalyse</div>'
    '<h1 style="font-size:3.4rem;font-weight:800;letter-spacing:-.035em;line-height:1.04;'
    'margin:.4rem 0 .8rem;max-width:21ch">Mål alt.<br>Spor <span style="color:var(--accent)">ingen</span>.</h1>'
    '<p style="max-width:42ch;color:var(--muted);margin:0 0 1.4rem">Uten cookies, uten samtykkebanner — '
    "og hvert dagstall forsegles så det ikke kan pyntes i ettertid.</p>"
    '<a href="#" style="background:var(--accent);color:#fff;padding:.7rem 1.4rem;border-radius:9px;'
    'text-decoration:none;font-weight:600">Prøv gratis</a></div>'
    '<p class=note style="margin-top:1rem">Grepene: gigantisk ø-vannmerke (5 % opacity), kortere og '
    "frekkere overskrift med farge på nøkkelordet, mer luft, større skala. Fortsatt papir — bare mer selvsikker.</p>",
)

FILES = {
    "brand/o-merke.html": OMERKE,
    "brand/farger.html": FARGER,
    "brand/typografi.html": TYPO,
    "components/kpi-kort.html": KPI,
    "components/stat-tabell.html": TABELL,
    "components/knapper-planer.html": KNAPPER,
    "dark/dashbord-mork.html": DARK,
    "landing/hero-retninger.html": HERO,
}

for path, content in FILES.items():
    f = OUT / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content)
    print(f"skrev {f.relative_to(ROOT)} ({len(content) // 1024} KB)")
