#!/usr/bin/env python3
"""Pre-flight check: are the data files here, intact, and readable?

    ./bin/python scripts/preflight.py

Prints one line per file and a short summary. Exit code 1 if anything that the
app needs is missing or the wrong size.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lunar import config, ephemeris, sites  # noqa: E402
from lunar.dem import terrain  # noqa: E402


def ok(flag: bool) -> str:
    return "ok  " if flag else "FAIL"


def main() -> int:
    problems = 0
    print(f"project : {config.ROOT}")

    try:
        s = config.settings()
    except FileNotFoundError as e:
        print(f"\n{e}")
        return 1

    # ---- terrain
    t = terrain()
    size_ok = t.size_bytes == config.DEM_EXPECTED_BYTES
    print(f"\nterrain : {t.path}")
    print(f"  {ok(size_ok)}  {t.size_bytes:,} bytes (PGDA publishes {config.DEM_EXPECTED_BYTES:,})")
    print(f"  {'ok  '}  {t.width} x {t.height} px, {t.res} m/px, {len(t.overviews)} overview levels")
    print(f"  {'ok  '}  {t.crs.to_string() if t.crs else 'no CRS'}")
    problems += 0 if size_ok else 1

    # ---- kernels
    print("\nkernels :")
    for name, path in s.kernel_paths.items():
        got = path.stat().st_size
        want = config.KERNELS[name]
        good = got == want
        print(f"  {ok(good)}  {name:<30} {got:>12,} bytes")
        problems += 0 if good else 1

    # ---- sites
    print("\nregions :")
    available = [x for x in sites.catalogue() if x.available]
    if not available:
        print("  FAIL  no SiteXX_ROIs.tgz found")
        problems += 1
    for site in available:
        print(f"  ok    {site.id:<10} {site.name:<28} {len(site.rois)} regions")
        if site.prj_warning:
            print(f"        warning: {site.prj_warning}")

    # ---- live smoke test
    print("\nchecks  :")
    try:
        ephemeris.ensure_loaded()
        print("  ok    SPICE kernels loaded together")
    except Exception as e:  # pragma: no cover
        print(f"  FAIL  SPICE: {e}")
        problems += 1

    try:
        x, y = sites.default_point()
        t0 = time.time()
        sky = t.skyline(x, y, 12_000, 2.0, 1.0)
        dt = time.time() - t0
        print(f"  ok    skyline at {x:.0f}/{y:.0f}: max {sky['max_elev']:.2f} deg, "
              f"{dt:.2f} s")
    except Exception as e:  # pragma: no cover
        print(f"  FAIL  skyline: {e}")
        problems += 1

    print(f"\n{'all good' if problems == 0 else f'{problems} problem(s)'}")
    return 0 if problems == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
