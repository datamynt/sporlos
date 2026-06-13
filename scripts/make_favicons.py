"""Favicon-pakke fra «Blekk»-merket — alt Google og nettlesere leter etter.

Reproduserer _FAVICON_SVG (rundet blekk-rute + aksentdisk r22 + strek sw7) i PIL.
Lager:
  static/brand/favicon.ico        (16/32/48 — Google leter spesifikt etter denne)
  static/brand/favicon-48.png     PNG-fallback for crawlere uten SVG
  static/brand/favicon-96.png
  static/brand/apple-touch-icon.png  (180, opak kvadrat — iOS maskerer selv)
  static/brand/icon-192.png       PWA / web-manifest
  static/brand/icon-512.png

Kjør:  .venv/bin/python3 scripts/make_favicons.py  < /dev/null
"""

from pathlib import Path

from PIL import Image, ImageDraw

INK = (23, 38, 62)      # #17263e
ACCENT = (47, 111, 237)  # #2f6fed
OUT = Path(__file__).resolve().parent.parent / "static" / "brand"


def mark(size: int, rounded: bool = True, opaque: bool = False) -> Image.Image:
    """Tegn merket på 64-rutenettet, supersamplet, nedskalert til `size`."""
    s = 4
    w = size * s
    img = Image.new("RGBA", (w, w), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    k = w / 64
    if rounded:
        d.rounded_rectangle([0, 0, w - 1, w - 1], radius=14 * k, fill=INK + (255,))
    else:
        d.rectangle([0, 0, w, w], fill=INK + (255,))
    d.ellipse([32 * k - 22 * k, 32 * k - 22 * k, 32 * k + 22 * k, 32 * k + 22 * k],
              fill=ACCENT + (255,))
    sw = round(7 * k)
    x1, y1, x2, y2 = 18.5 * k, 49 * k, 45.5 * k, 15 * k
    d.line([x1, y1, x2, y2], fill=INK + (255,), width=sw)
    cap = sw // 2
    for x, y in ((x1, y1), (x2, y2)):
        d.ellipse([x - cap, y - cap, x + cap, y + cap], fill=INK + (255,))
    img = img.resize((size, size), Image.LANCZOS)
    return img.convert("RGB") if opaque else img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    # ICO med flere størrelser — bygg fra 256 og la PIL skalere ned.
    base = mark(256)
    ico = OUT / "favicon.ico"
    base.save(ico, sizes=[(16, 16), (32, 32), (48, 48)])
    print(f"skrev {ico} ({ico.stat().st_size} bytes)")
    for n in (48, 96):
        p = OUT / f"favicon-{n}.png"
        mark(n).save(p, "PNG", optimize=True)
        print(f"skrev {p} ({p.stat().st_size} bytes)")
    apple = OUT / "apple-touch-icon.png"
    mark(180, rounded=False, opaque=True).save(apple, "PNG", optimize=True)
    print(f"skrev {apple} ({apple.stat().st_size} bytes)")
    for n in (192, 512):
        p = OUT / f"icon-{n}.png"
        mark(n).save(p, "PNG", optimize=True)
        print(f"skrev {p} ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
