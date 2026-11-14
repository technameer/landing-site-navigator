#!/usr/bin/env python3
"""One-time download of the global Moon texture and the 3D engine files.

Fetches NASA SVS's rendition of the LROC WAC global colour mosaic
(equirectangular) into data/moon_global.jpg, plus Three.js and its orbit
controls into web/vendor/ so the 3D view never touches a CDN. This is the
app's only use of the internet, and it happens once, on purpose - after the
files exist everything runs offline. Delete them to fall back to the plain
grey globe and 2D-only map.

    ./bin/python scripts/fetch_globe.py
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# 2k first, 1k as the fallback. Public domain: NASA/GSFC/ASU.
SVS_URLS = [
    "https://svs.gsfc.nasa.gov/vis/a000000/a004700/a004720/lroc_color_poles_2k.jpg",
    "https://svs.gsfc.nasa.gov/vis/a000000/a004700/a004720/lroc_color_poles_1k.jpg",
]
THREE_URLS = [
    "https://unpkg.com/three@0.160.0/build/three.module.js",
    "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js",
]
ORBIT_URLS = [
    "https://unpkg.com/three@0.160.0/examples/jsm/controls/OrbitControls.js",
    "https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/controls/OrbitControls.js",
]

# each target: (where it goes, URLs to try in order)
TARGETS = [
    (ROOT / "data" / "moon_global.jpg", SVS_URLS),
    (ROOT / "web" / "vendor" / "three.module.js", THREE_URLS),
    (ROOT / "web" / "vendor" / "OrbitControls.js", ORBIT_URLS),
]


def fetch(dest: Path, urls: list[str]) -> bool:
    if dest.exists():
        print(f"already here: {dest} ({dest.stat().st_size:,} bytes)")
        return True
    dest.parent.mkdir(parents=True, exist_ok=True)
    last = None
    for url in urls:
        try:
            print(f"fetching {url} ...")
            with urllib.request.urlopen(url, timeout=60) as r, open(dest, "wb") as f:
                f.write(r.read())
            print(f"saved {dest} ({dest.stat().st_size:,} bytes)")
            return True
        except Exception as e:  # noqa: BLE001 - report it and try the next mirror
            last = e
            print(f"  failed: {e}")
    print(f"could not download {dest.name}: {last}", file=sys.stderr)
    if dest.exists():
        dest.unlink()
    return False


def main() -> int:
    ok = all(fetch(dest, urls) for dest, urls in TARGETS)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
