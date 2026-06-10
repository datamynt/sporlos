"""Kvadratisk app-ikon (ø-merket) for Vipps/avatarer — 512x512 på papir-bakgrunn.

Kjør:  .venv/bin/python3 scripts/make_appikon.py  < /dev/null
"""

from pathlib import Path

from PIL import Image, ImageDraw

S = 3
W = 512 * S
ACCENT = (47, 111, 237)
BG = (250, 249, 246)

img = Image.new("RGB", (W, W), BG)
d = ImageDraw.Draw(img)
cx = cy = W // 2
r, stroke = 130 * S, 56 * S
d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=ACCENT, width=stroke)
dx, dy = 0.94 * r, 1.19 * r
x1, y1, x2, y2 = cx - dx, cy + dy, cx + dx, cy - dy
d.line([x1, y1, x2, y2], fill=ACCENT, width=stroke)
cap = stroke // 2
for x, y in ((x1, y1), (x2, y2)):
    d.ellipse([x - cap, y - cap, x + cap, y + cap], fill=ACCENT)

img = img.resize((512, 512), Image.LANCZOS)
out = Path(__file__).resolve().parent.parent / "static" / "brand" / "app-ikon.png"
img.save(out, "PNG", optimize=True)
print(f"skrev {out} ({out.stat().st_size} bytes)")
