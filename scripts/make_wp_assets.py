"""wordpress.org-assets for sporlos-analytics: ikon + bannere (Blekk-identiteten).

Kjør:  .venv/bin/python3 scripts/make_wp_assets.py  < /dev/null
Skriver til integrations/wordpress/assets/ (SVN-mappen `assets/` på wp.org).
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
FONT = str(ROOT / "scripts" / "schibsted-grotesk.ttf")
OUT = ROOT / "integrations" / "wordpress" / "assets"
OUT.mkdir(parents=True, exist_ok=True)

S = 3
INK = (23, 38, 62)
PAPER = (250, 249, 246)
ACCENT = (47, 111, 237)
MUTED = (95, 107, 125)


def disk(d, cx, cy, r, fill, slash):
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill)
    sw = round(r * 8 / 26)
    dx, dy = r * 16 / 26, r * 20 / 26
    x1, y1, x2, y2 = cx - dx, cy + dy, cx + dx, cy - dy
    d.line([x1, y1, x2, y2], fill=slash, width=sw)
    for x, y in ((x1, y1), (x2, y2)):
        d.ellipse([x - sw // 2, y - sw // 2, x + sw // 2, y + sw // 2], fill=slash)


def font(size, weight):
    f = ImageFont.truetype(FONT, size * S)
    f.set_variation_by_axes([weight])
    return f


# Ikon (samme som app-ikonet): blekk-bunn + aksent-disk
for px in (256, 128):
    img = Image.new("RGB", (px * S, px * S), INK)
    d = ImageDraw.Draw(img)
    disk(d, px * S // 2, px * S // 2, round(px * S * 26 / 64), ACCENT, INK)
    img.resize((px, px), Image.LANCZOS).save(OUT / f"icon-{px}x{px}.png", optimize=True)
    print(f"skrev icon-{px}x{px}.png")

# Banner: papir, disk + ordmerke + tagline (1544x500 retina + 772x250)
W, H = 1544 * S // 2, 500 * S // 2  # tegnes i 2x-flate, eksporteres i begge
img = Image.new("RGB", (1544 * S, 500 * S), PAPER)
d = ImageDraw.Draw(img)
r = 110 * S
cx, cy = 170 * S + r, 250 * S
disk(d, cx, cy, r, ACCENT, PAPER)
d.text((cx + r + 70 * S, cy - 40 * S), "sporløs", font=font(190, 800), fill=INK, anchor="lm")
d.text(
    (cx + r + 74 * S, cy + 118 * S),
    "Webanalyse uten cookie-banner",
    font=font(64, 500),
    fill=MUTED,
    anchor="lm",
)
img.resize((1544, 500), Image.LANCZOS).save(OUT / "banner-1544x500.png", optimize=True)
img.resize((772, 250), Image.LANCZOS).save(OUT / "banner-772x250.png", optimize=True)
print("skrev banner-1544x500.png + banner-772x250.png")
