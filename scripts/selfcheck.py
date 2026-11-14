#!/usr/bin/env python3
"""Numeric self-check: do the numbers still make sense?

    ./bin/python scripts/selfcheck.py

Every check compares the app against something independent: a published file
size, a closed-form projection formula, pyproj, or a fact about the Moon that
cannot be faked (the 1.54 degree axial tilt).
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lunar import config, ephemeris, geo, roi as roi_mod, sites  # noqa: E402
from lunar.dem import terrain  # noqa: E402

FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global FAILED
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILED += 1


def close(a: float, b: float, tol: float) -> bool:
    return abs(a - b) <= tol


def main() -> int:
    t = terrain()
    s = config.settings()

    print("\nfiles")
    check("terrain size matches PGDA", t.size_bytes == config.DEM_EXPECTED_BYTES,
          f"{t.size_bytes:,}")
    check("terrain grid 30400 x 30400 at 20 m", (t.width, t.height, t.res) == (30400, 30400, 20.0))
    for name, path in s.kernel_paths.items():
        check(f"{name} size", path.stat().st_size == config.KERNELS[name])

    print("\nprojection")
    # the browser does this in closed form; it must agree with pyproj
    for x, y in [(-11589.9, -12466.4), (0.0, -300000.0), (120000.0, 40000.0)]:
        lon_p, lat_p = t.to_lonlat(x, y)
        lon_c, lat_c = geo.xy_to_lonlat_closed_form(x, y)
        check(f"closed form == pyproj at ({x:.0f},{y:.0f})",
              close(lon_p, lon_c, 1e-6) and close(lat_p, lat_c, 1e-6),
              f"dlon {abs(lon_p - lon_c):.2e} dlat {abs(lat_p - lat_c):.2e}")

    print("\nregions")
    site = sites.get("Site01")
    check("Site01 archive present", site.available, site.archive or "")
    check("Site01 has 7 regions", len(site.rois) == 7, str(len(site.rois)))
    if site.rois:
        r1 = site.rois[0]
        check("R1 centre matches the shapefile", close(r1.x, -11589.9, 1.0) and close(r1.y, -12466.4, 1.0),
              f"{r1.x:.1f}, {r1.y:.1f}")
        check("R1 is inside the terrain file", t.contains(r1.x, r1.y))
        check("R1 ground height is plausible", r1.height is not None and 1000 < r1.height < 3000,
              f"{r1.height:.1f} m")
        check("R1 area is under 1 km2", 0 < r1.area_km2 < 1.0, f"{r1.area_km2:.4f} km2")

    print("\nephemeris")
    import spiceypy as sp

    ephemeris.ensure_loaded()
    check("frame MOON_ME_DE421 resolves", sp.namfrm("MOON_ME_DE421") == 31007)

    # The Moon's axis is tilted 1.54 deg to its orbit, so the Sun's elevation
    # measured from the pole must swing between roughly +1.5 and -1.5 deg.
    for date, expect in (("2026-11-14T00:00:00", 1.56), ("2027-05-14T00:00:00", -1.52)):
        et = ephemeris.utc_to_et(date)
        ang = ephemeris.look_angles(-90.0, 0.0, 0.0, np.array([et]))
        el = float(ang["SUN"]["el"][0])
        check(f"Sun at the pole on {date[:10]}", close(el, expect, 0.1), f"{el:+.2f} deg")

    et = ephemeris.utc_to_et("2026-11-12T00:00:00")
    d = ephemeris.body_vectors("EARTH", np.array([et]))
    dist = float(np.linalg.norm(d)) / 1000.0
    check("Earth-Moon distance", close(dist, 404168, 2000), f"{dist:,.0f} km")

    # the Sun must be about one astronomical unit away, i.e. a sane ephemeris
    dsun = float(np.linalg.norm(ephemeris.body_vectors("SUN", np.array([et])))) / 1000.0
    check("Sun-Moon distance", close(dsun, 149_600_000, 2_000_000), f"{dsun:,.0f} km")

    print("\nterrain products")
    x, y = sites.default_point()
    sky = t.skyline(x, y, 12_000, 2.0, 1.0)
    check("skyline has 360 azimuths", len(sky["az"]) == 360)
    check("skyline is finite", all(math.isfinite(v) for v in sky["elev"]))
    check("skyline is within -90..90 deg", -90 <= min(sky["elev"]) and max(sky["elev"]) <= 90)
    check("horizon elevation is sane", -5.0 < sky["max_elev"] < 30.0, f"{sky['max_elev']:.2f} deg")
    check("horizon has structure", max(sky["elev"]) - min(sky["elev"]) > 0.05,
          f"spread {max(sky['elev']) - min(sky['elev']):.2f} deg")
    check("no missing samples", sky["missing_share"] < 0.01, f"{sky['missing_share']:.3f}")

    # independent cross-check: walk along the highest bearing with profile()
    prof = t.profile(x, y, sky["max_az"], 12_000, 2.0, step=20.0)
    prof_max = max(v for v in prof["elev"] if v is not None)
    check("profile agrees with skyline", close(prof_max, sky["max_elev"], 0.02),
          f"{prof_max:.3f} vs {sky['max_elev']:.3f} deg")

    png, bbox = t.hillshade_png(0, 0, 20_000, 256, 256)
    check("hillshade is a PNG", png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 2000, f"{len(png)} bytes")
    check("hillshade bbox is right", close(bbox[2] - bbox[0], 20_000, 1))

    print("\ntimeline")
    tl = ephemeris.timeline(x, y, sky, "2026-11-14T00:00:00", hours=24, step_min=10)
    check("24 h at 10 min gives 145 samples", tl["stats"]["samples"] == 145)
    check("sunlit fraction is a fraction", 0.0 <= tl["stats"]["sunlit_pct"] <= 100.0,
          f"{tl['stats']['sunlit_pct']}%")
    check("earth visible fraction is a fraction", 0.0 <= tl["stats"]["earth_vis_pct"] <= 100.0,
          f"{tl['stats']['earth_vis_pct']}%")
    check("longest sunlit run fits the window", 0.0 <= tl["stats"]["longest_sunlit_h"] <= 24.0,
          f"{tl['stats']['longest_sunlit_h']} h")

    print("\nbrute force cross-check")
    # Cast no rays at all: bin every pixel in a 12 km window by its true
    # great-circle bearing and take the highest angle in each 1-degree bin.
    import rasterio
    from rasterio.windows import Window

    maxdist = 12_000.0
    pad = int(maxdist * 1.15 / t.res) + 8
    col, row = ~t.transform * (x, y)
    c0, r0 = max(0, int(col) - pad), max(0, int(row) - pad)
    c1, r1 = min(t.width, int(col) + pad + 1), min(t.height, int(row) + pad + 1)
    win = Window(c0, r0, c1 - c0, r1 - r0)
    with rasterio.open(t.path) as ds:
        data = ds.read(1, window=win)[::3, ::3].astype("float64")
        tr = ds.window_transform(win)
    rr, cc = np.mgrid[0:data.shape[0], 0:data.shape[1]]
    px = tr.c + (cc * 3 + 0.5) * tr.a
    py = tr.f + (rr * 3 + 0.5) * tr.e
    lon0, lat0 = t.to_lonlat(x, y)
    lons, lats = t._to_ll.transform(px, py)
    p1, p2 = np.radians(lat0), np.radians(lats)
    dl = np.radians(lons - lon0)
    dist = geo.R_MOON * np.arccos(np.clip(
        np.sin(p1) * np.sin(p2) + np.cos(p1) * np.cos(p2) * np.cos(dl), -1, 1))
    bearing = np.degrees(np.arctan2(
        np.sin(dl) * np.cos(p2),
        np.cos(p1) * np.sin(p2) - np.sin(p1) * np.cos(p2) * np.cos(dl))) % 360.0
    elev = geo.elevation_angle(data, dist, t.height_at(x, y) + 2.0)
    ok = (dist > t.res) & (dist <= maxdist) & np.isfinite(elev)
    bins = np.floor(bearing[ok]).astype(int) % 360
    best = np.full(360, -90.0)
    np.maximum.at(best, bins, elev[ok])
    check("brute force agrees on the maximum", abs(best.max() - sky["max_elev"]) < 0.05,
          f"{best.max():.2f} vs {sky['max_elev']:.2f} deg")

    print(f"\n{'all checks passed' if FAILED == 0 else f'{FAILED} check(s) failed'}")
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
