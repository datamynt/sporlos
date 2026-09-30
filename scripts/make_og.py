"""Generer static/og.png (1200x630) — delebilde for sosiale medier.

Kjør:  .venv/bin/python3 scripts/make_og.py  < /dev/null
Tegner i 3x og nedskalerer (PIL antialiaser ikke former selv).
Fonten er Schibsted Grotesk (OFL), samme som nettsiden.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
FONT = str(ROOT / "scripts" / "schibsted-grotesk.ttf")
OUT = ROOT / "static" / "og.png"

S = 3  # supersampling
W, H = 1200 * S, 630 * S

BG = (250, 249, 246)
INK = (23, 38, 62)
MUTED = (95, 107, 125)
ACCENT = (47, 111, 237)
ACCENT_DEEP = (29, 78, 216)
ACCENT_LIGHT = (143, 179, 255)


def font(size, weight=400):
    f = ImageFont.truetype(FONT, size * S)
    f.set_variation_by_axes([weight])
    return f


def o_mark(draw, cx, cy, r, stroke, color):
    """Ø-merket: sirkel + skråstrek m/ runde ender (samme geometri som favicon)."""
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=stroke)
    # strek 17,51 -> 47,13 i 64-boksen ≈ vinkel, skalert: overshoot ~1.2r
    dx, dy = 0.94 * r, 1.19 * r
    x1, y1, x2, y2 = cx - dx, cy + dy, cx + dx, cy - dy
    draw.line([x1, y1, x2, y2], fill=color, width=stroke)
    cap = stroke // 2
    for x, y in ((x1, y1), (x2, y2)):
        draw.ellipse([x - cap, y - cap, x + cap, y + cap], fill=color)


img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)

# Topplinje: gradient deep -> accent -> light (signaturen fra nettsiden)
bar_h = 14 * S
for x in range(W):
    t = x / W
    if t < 0.45:
        u = t / 0.45
        c = tuple(round(a + (b - a) * u) for a, b in zip(ACCENT_DEEP, ACCENT))
    else:
        u = (t - 0.45) / 0.55
        c = tuple(round(a + (b - a) * u) for a, b in zip(ACCENT, ACCENT_LIGHT))
    d.line([x, 0, x, bar_h], fill=c)

# Vannmerke: stort, svakt ø-merke nede til høyre (tegnes på eget lag m/ alpha)
wm = Image.new("RGBA", (W, H), (0, 0, 0, 0))
o_mark(ImageDraw.Draw(wm), int(W * 0.86), int(H * 0.78), 230 * S, 52 * S, INK + (14,))
img = Image.alpha_composite(img.convert("RGBA"), wm).convert("RGB")
d = ImageDraw.Draw(img)

# Lockup: «Blekk»-disken + «sporløs» (vannmerket beholder strek-varianten — luftigere)
mark_r = 54 * S
mx, my = 96 * S + mark_r, 250 * S
disk = Image.new("RGBA", (W, H), (0, 0, 0, 0))
dd = ImageDraw.Draw(disk)
dd.ellipse([mx - mark_r, my - mark_r, mx + mark_r, my + mark_r], fill=ACCENT)
_sw = round(mark_r * 8 / 26)
_dx, _dy = mark_r * 16 / 26, mark_r * 20 / 26
_x1, _y1, _x2, _y2 = mx - _dx, my + _dy, mx + _dx, my - _dy
dd.line([_x1, _y1, _x2, _y2], fill=(0, 0, 0, 0), width=_sw)  # stanser ut streken
for _x, _y in ((_x1, _y1), (_x2, _y2)):
    dd.ellipse([_x - _sw // 2, _y - _sw // 2, _x + _sw // 2, _y + _sw // 2], fill=(0, 0, 0, 0))
img = Image.alpha_composite(img.convert("RGBA"), disk).convert("RGB")
d = ImageDraw.Draw(img)
wordmark = font(124, 800)
d.text((mx + mark_r + 36 * S, my), "sporløs", font=wordmark, fill=INK, anchor="lm")

# Tagline + domene
d.text((96 * S, 410 * S), "Webanalyse uten cookie-banner.", font=font(52, 650), fill=INK, anchor="lm")
d.text(
    (96 * S, 478 * S),
    "Cookieløst · ingen IP lagret · data i Norge",
    font=font(34, 450),
    fill=MUTED,
    anchor="lm",
)
d.text((96 * S, 552 * S), "sporlos.no", font=font(34, 650), fill=ACCENT_DEEP, anchor="lm")

img = img.resize((1200, 630), Image.LANCZOS)
OUT.parent.mkdir(exist_ok=True)
img.save(OUT, "PNG", optimize=True)
print(f"skrev {OUT} ({OUT.stat().st_size} bytes)")
