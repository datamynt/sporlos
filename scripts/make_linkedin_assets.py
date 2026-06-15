"""LinkedIn-side-assets for Sporløs (Showcase page under Datamynt).

LinkedIn-krav:
  - Logo (profilbilde): kvadrat, min 300x300 → vi lager 400x400 (Blekk-ikonet)
  - Cover/omslag: 1128x191 px (vises beskåret på mobil — hold viktig innhold midtstilt vertikalt)

Kjør:  .venv/bin/python3 scripts/make_linkedin_assets.py  < /dev/null
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
FONT = str(ROOT / "scripts" / "schibsted-grotesk.ttf")
OUT = ROOT / "static" / "brand" / "linkedin"
OUT.mkdir(parents=True, exist_ok=True)

S = 3
INK = (23, 38, 62)
PAPER = (250, 249, 246)
ACCENT = (47, 111, 237)
MUTED = (95, 107, 125)


def disk(d, cx, cy, r, fill, slash):
    """Blekk-disken: solid sirkel m/ skråstrek i flatens farge."""
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill)
    sw = round(r * 8 / 26)
    dx, dy = r * 16 / 26, r * 20 / 26
    x1, y1, x2, y2 = cx - dx, cy + dy, cx + dx, cy - dy
    d.line([x1, y1, x2, y2], fill=slash, width=sw)
    for x, y in ((x1, y1), (x2, y2)):
        d.ellipse([x - sw // 2, y - sw // 2, x + sw // 2, y + sw // 2], fill=slash)


def cutout_disk(img, cx, cy, r, fill):
    """Disk m/ TRANSPARENT utstanset strek (på eget lag, alpha-komponert)."""
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill)
    sw = round(r * 8 / 26)
    dx, dy = r * 16 / 26, r * 20 / 26
    x1, y1, x2, y2 = cx - dx, cy + dy, cx + dx, cy - dy
    d.line([x1, y1, x2, y2], fill=(0, 0, 0, 0), width=sw)
    for x, y in ((x1, y1), (x2, y2)):
        d.ellipse([x - sw // 2, y - sw // 2, x + sw // 2, y + sw // 2], fill=(0, 0, 0, 0))
    img.alpha_composite(layer)


def font(size, weight):
    f = ImageFont.truetype(FONT, size * S)
    f.set_variation_by_axes([weight])
    return f


# ── Logo 400x400 (Blekk-ikonet) ──────────────────────────────────────────
px = 400
logo = Image.new("RGB", (px * S, px * S), INK)
disk(ImageDraw.Draw(logo), px * S // 2, px * S // 2, round(px * S * 26 / 64), ACCENT, INK)
logo.resize((px, px), Image.LANCZOS).save(OUT / "logo-400.png", optimize=True)
print("skrev logo-400.png (profilbilde)")

# ── Cover 1128x191 (papir, ordmerke + tagline, ø-vannmerke til høyre) ──────
W, H = 1128 * S, 191 * S
cov = Image.new("RGBA", (W, H), PAPER + (255,))
# stor, svak ø-disk til høyre (utstanset strek)
cutout_disk(cov, int(W * 0.87), H // 2, int(H * 0.95), ACCENT + (16,))
d = ImageDraw.Draw(cov)
# tynn aksentstripe i topp (signaturen fra nettsiden)
d.rectangle([0, 0, W, 5 * S], fill=ACCENT)
# lockup: liten solid disk + ordmerke
r = 40 * S
cx, cy = 80 * S + r, H // 2 - 12 * S
disk(d, cx, cy, r, ACCENT, PAPER)
d.text((cx + r + 28 * S, cy), "sporløs", font=font(72, 800), fill=INK, anchor="lm")
d.text(
    (80 * S, H // 2 + 46 * S),
    "Webanalyse uten cookie-banner — bygget i Norge",
    font=font(30, 500),
    fill=MUTED,
    anchor="lm",
)
cov.convert("RGB").resize((1128, 191), Image.LANCZOS).save(OUT / "cover-1128x191.png", optimize=True)
print("skrev cover-1128x191.png (omslag)")
