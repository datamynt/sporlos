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
W, H = 560 * S, 140 * S
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
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    r = 38 * S
    cx, cy = 10 * S + r, H // 2
    disk_mark(img, cx, cy, r, ACCENT)
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(FONT, 86 * S)
    f.set_variation_by_axes([800])
    d.text((cx + r + 24 * S, cy), "sporløs", font=f, fill=text_color, anchor="lm")
    img = img.resize((W // S, H // S), Image.LANCZOS)
    OUTDIR.mkdir(parents=True, exist_ok=True)
    out = OUTDIR / name
    img.save(out, "PNG", optimize=True)
    print(f"skrev {out}")


lockup(INK, "logo-light.png")
lockup(WHITE, "logo-dark.png")
