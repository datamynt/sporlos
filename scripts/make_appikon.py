"""Kvadratisk app-ikon — «Blekk»-varianten fra design-runde 2 (Negativ i squircle).

Blekkmørk bunn + aksentblå disk + blekk-strek med Presisjon-proporsjoner.
Plattformene maskerer selv (squircle/sirkel) — vi leverer fullt kvadrat.

Kjør:  .venv/bin/python3 scripts/make_appikon.py  < /dev/null
"""

from pathlib import Path

from PIL import Image, ImageDraw

S = 3  # supersampling
W = 512 * S
INK = (23, 38, 62)  # #17263e
ACCENT = (47, 111, 237)  # #2f6fed

img = Image.new("RGB", (W, W), INK)
d = ImageDraw.Draw(img)

# 64-rutenettet fra designkortet skalert opp: disk r26, strek 16,52→48,12 sw8
k = W / 64
d.ellipse([32 * k - 26 * k, 32 * k - 26 * k, 32 * k + 26 * k, 32 * k + 26 * k], fill=ACCENT)
stroke = round(8 * k)
x1, y1, x2, y2 = 16 * k, 52 * k, 48 * k, 12 * k
d.line([x1, y1, x2, y2], fill=INK, width=stroke)
cap = stroke // 2
for x, y in ((x1, y1), (x2, y2)):
    d.ellipse([x - cap, y - cap, x + cap, y + cap], fill=INK)

img = img.resize((512, 512), Image.LANCZOS)
out = Path(__file__).resolve().parent.parent / "static" / "brand" / "app-ikon.png"
img.save(out, "PNG", optimize=True)
print(f"skrev {out} ({out.stat().st_size} bytes)")
