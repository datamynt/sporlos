"""Minify tracker/sporlos.js into tracker/sporlos.min.js (what /sporlos.js serves).

    python3 scripts/build_tracker.py < /dev/null      # needs node/npx for terser

The first line of the output carries a short sha256 of the source it was built
from. app/main.py serves the minified file only while that hash matches the
source on disk, and falls back to the readable source otherwise, so a forgotten
rebuild can never ship stale tracking logic. tests/test_tracker.py fails on a
mismatch. The readable source stays public at /sporlos.src.js.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "tracker" / "sporlos.js"
OUT = ROOT / "tracker" / "sporlos.min.js"


def main() -> int:
    source = SRC.read_bytes()
    digest = hashlib.sha256(source).hexdigest()[:16]  # staleness check, not security
    res = subprocess.run(
        ["npx", "--yes", "terser@5", str(SRC), "--compress", "--mangle", "--ecma", "5"],
        capture_output=True, stdin=subprocess.DEVNULL, check=True,
    )
    # Kept short on purpose: every byte here is shipped to every visitor.
    banner = f"/*! Sporlos (MIT) source: sporlos.no/sporlos.src.js src:{digest} */\n"
    OUT.write_bytes(banner.encode() + res.stdout.strip() + b"\n")
    print(f"{OUT.relative_to(ROOT)}: {OUT.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
