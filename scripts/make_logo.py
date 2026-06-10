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


def o_mark(draw, cx, cy, r, stroke, color):
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], outline=color, width=stroke)
    dx, dy = 0.94 * r, 1.19 * r
    x1, y1, x2, y2 = cx - dx, cy + dy, cx + dx, cy - dy
    draw.line([x1, y1, x2, y2], fill=color, width=stroke)
    cap = stroke // 2
    for x, y in ((x1, y1), (x2, y2)):
        draw.ellipse([x - cap, y - cap, x + cap, y + cap], fill=color)


def lockup(text_color, name):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = 38 * S
    cx, cy = 10 * S + r, H // 2
    o_mark(d, cx, cy, r, 17 * S, ACCENT)
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
