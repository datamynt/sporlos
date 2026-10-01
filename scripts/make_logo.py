"""Generer logo-lockupen (ø-merke + «sporløs») som transparente PNG-er.

Kjør:  .venv/bin/python3 scripts/make_logo.py  < /dev/null
Lager static/brand/logo-light.png (blekk-tekst, for lys bakgrunn)
   og static/brand/logo-dark.png  (hvit tekst, for mørk bakgrunn).
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
FONT = str(ROOT / "scripts" / "schibsted-grotesk.ttf")
OUTDIR = ROOT / "static" / "brand"

S = 3
W, H = 800 * S, 240 * S  # working canvas; the output is trimmed to the ink
PAD = 12  # px of air on every side of the finished logo
INK = (23, 38, 62)
WHITE = (250, 250, 250)
ACCENT = (47, 111, 237)


def disk_mark(img, cx, cy, r, color):
    """«Blekk»-disken: solid sirkel m/ utstanset (transparent) skråstrek.
    Geometri fra app-ikonet: strek 16,52→48,12 / sw8 i 64-grid, skalert til r."""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=color)
    stroke = round(r * 8 / 26)
    dx, dy = r * 16 / 26, r * 20 / 26
    x1, y1, x2, y2 = cx - dx, cy + dy, cx + dx, cy - dy
    # ImageDraw ERSTATTER piksler (ingen blending) → (0,0,0,0) stanser ut streken
    d.line([x1, y1, x2, y2], fill=(0, 0, 0, 0), width=stroke)
    cap = stroke // 2
    for x, y in ((x1, y1), (x2, y2)):
        d.ellipse([x - cap, y - cap, x + cap, y + cap], fill=(0, 0, 0, 0))
    img.alpha_composite(layer)


def lockup(text_color, name):
    """The disk is centred on the x-height, the middle of the lowercase letters. Centring
    it on the font's whole ascender-to-descender box put it ~7 px too high. The result is
    trimmed to the ink with PAD on every side, so it sits centred wherever it is placed
    (Stripe, Vipps); a fixed 560 px canvas left 126 px of empty space on the right."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    f = ImageFont.truetype(FONT, 86 * S)
    f.set_variation_by_axes([800])
    r = 38 * S
    baseline = H * 0.6
    _, x_top, _, x_bottom = f.getbbox("x", anchor="ls")
    cx, cy = 2 * r, baseline + (x_top + x_bottom) / 2
    disk_mark(img, cx, cy, r, ACCENT)
    ImageDraw.Draw(img).text((cx + r + 24 * S, baseline), "sporløs", font=f, fill=text_color, anchor="ls")
    left, top, right, bottom = img.getbbox()
    pad = PAD * S
    img = img.crop((left - pad, top - pad, right + pad, bottom + pad))
    img = img.resize((round(img.width / S), round(img.height / S)), Image.LANCZOS)
    OUTDIR.mkdir(parents=True, exist_ok=True)
    out = OUTDIR / name
    img.save(out, "PNG", optimize=True)
    print(f"skrev {out} {img.size}")


lockup(INK, "logo-light.png")
lockup(WHITE, "logo-dark.png")
