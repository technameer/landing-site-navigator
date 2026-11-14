#!/usr/bin/env python3
"""
Lunar Landing Site Navigator - dataset explorer.

Three small commands to help you understand the data before the hackathon.

  1) Look inside a LOLA terrain GeoTIFF (heights in metres):
       python explore_datasets.py inspect path/to/LDEM_80S_80MPP_ADJ.TIF --plot

  2) Compute the terrain skyline (horizon profile) seen from one site:
       python explore_datasets.py skyline path/to/LDEM.TIF --lat -89.5 --lon 30 --maxdist 15000
       (or give projected metres instead: --xy 1200 -5300)

  3) Sun and Earth elevation over a lunar day, using NASA SPICE kernels,
     optionally checked against the skyline from step 2:
       python explore_datasets.py spice kernels/ --lat -89.5 --lon 30 \
              --start 2026-11-14 --skyline skyline_-89.5_30.npy

Needs:  pip install numpy rasterio pyproj matplotlib spiceypy

Status: 'inspect' and 'skyline' were tested on a synthetic terrain file.
        'spice' has NOT been run against real kernels - verify it against
        JPL Horizons before trusting any number.
"""
import argparse
import math
import os
import sys

import numpy as np

R_MOON = 1_737_400.0  # metres, reference sphere used by the LOLA polar products
SUN_RADIUS_DEG = 0.27  # approximate angular radius of the Sun seen from the Moon


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def moon_geo_crs():
    from pyproj import CRS
    return CRS.from_proj4(f"+proj=longlat +R={R_MOON} +no_defs")


def destination(lat_deg, lon_deg, bearings_deg, dists_m):
    """Points reached from (lat, lon) travelling along great circles on a sphere.
    bearings_deg: shape (nb,), dists_m: shape (ns,). Returns lat, lon arrays (nb, ns)."""
    lat1 = math.radians(lat_deg)
    lon1 = math.radians(lon_deg)
    th = np.radians(np.asarray(bearings_deg))[:, None]
    dl = (np.asarray(dists_m) / R_MOON)[None, :]
    lat2 = np.arcsin(np.sin(lat1) * np.cos(dl) + np.cos(lat1) * np.sin(dl) * np.cos(th))
    lon2 = lon1 + np.arctan2(np.sin(th) * np.sin(dl) * np.cos(lat1),
                             np.cos(dl) - np.sin(lat1) * np.sin(lat2))
    return np.degrees(lat2), (np.degrees(lon2) + 180.0) % 360.0 - 180.0


def local_frame(lat_deg, lon_deg):
    """Unit vectors Up, East, North at a lat/lon in the Moon-fixed frame."""
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    up = np.array([math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat)])
    east = np.array([-math.sin(lon), math.cos(lon), 0.0])
    north = np.array([-math.sin(lat) * math.cos(lon), -math.sin(lat) * math.sin(lon), math.cos(lat)])
    return up, east, north


def site_lonlat(ds, args):
    """Return (lat, lon, x, y) for the site, from --lat/--lon or --xy."""
    from pyproj import Transformer
    if ds.crs is None:
        sys.exit("This file has no CRS, so lat/lon cannot be converted. "
                 "Use a GeoTIFF from the pgda.gsfc.nasa.gov LOLA pages.")
    to_xy = Transformer.from_crs(moon_geo_crs(), ds.crs, always_xy=True)
    to_ll = Transformer.from_crs(ds.crs, moon_geo_crs(), always_xy=True)
    if args.xy:
        x, y = args.xy
        lon, lat = to_ll.transform(x, y)
    else:
        if args.lat is None or args.lon is None:
            sys.exit("Give --lat and --lon, or --xy X Y.")
        lat, lon = args.lat, args.lon
        x, y = to_xy.transform(lon, lat)
    return lat, lon, x, y


# --------------------------------------------------------------------------
# command 1: inspect
# --------------------------------------------------------------------------
def cmd_inspect(a):
    import rasterio
    with rasterio.open(a.dem) as ds:
        print(f"File          : {a.dem}")
        print(f"Driver        : {ds.driver}")
        print(f"Size (px)     : {ds.width} x {ds.height}, bands: {ds.count}, dtype: {ds.dtypes[0]}")
        print(f"Pixel size    : {ds.res[0]} x {ds.res[1]} (map units, normally metres)")
        print(f"Bounds        : {ds.bounds}")
        print(f"CRS           : {ds.crs}")
        print(f"Nodata value  : {ds.nodata}")
        print(f"Tiled/overviews: blocks={ds.block_shapes[0]}, overviews={ds.overviews(1)}")

        half = min(abs(ds.bounds.left), abs(ds.bounds.right), abs(ds.bounds.bottom), abs(ds.bounds.top))
        edge_lat = -90.0 + math.degrees(2 * math.atan(0.5 * half / R_MOON))
        print(f"(Pole-centred maps only) edge is ~{half / 1000:.0f} km from the pole -> reaches ~{edge_lat:.1f} deg latitude")

        step = max(1, int(max(ds.width, ds.height) / 1500))
        out_h, out_w = max(1, ds.height // step), max(1, ds.width // step)
        arr = ds.read(1, out_shape=(out_h, out_w), masked=True)
        print(f"\nHeights (decimated x{step} so it loads quickly):")
        print(f"  min {arr.min():.1f} m   max {arr.max():.1f} m   mean {arr.mean():.1f} m")
        print(f"  masked/no-data pixels: {100 * arr.mask.mean():.2f}%" if np.ndim(arr.mask) else "  no masked pixels")
        if ds.bounds.left <= 0 <= ds.bounds.right and ds.bounds.bottom <= 0 <= ds.bounds.top:
            v = next(ds.sample([(0.0, 0.0)]))[0]
            print(f"  height at the pole (x=0, y=0): {v:.1f} m")
        print("\nTip: if min/max look like ~1,730,000 the heights are radii; "
              "if they are within a few km they are heights above the reference sphere.")

        if a.plot:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            fig, ax = plt.subplots(figsize=(7, 7))
            im = ax.imshow(arr, cmap="gray")
            fig.colorbar(im, ax=ax, label="height (m)")
            ax.set_title("Terrain preview (decimated)")
            fig.savefig("dem_preview.png", dpi=120, bbox_inches="tight")
            print("Saved dem_preview.png")


# --------------------------------------------------------------------------
# command 2: skyline
# --------------------------------------------------------------------------
def compute_skyline(ds, lat, lon, x, y, maxdist, mast, az_step=1.0):
    """Highest terrain angle (degrees above local horizontal) for each azimuth."""
    from pyproj import Transformer
    from rasterio.windows import Window

    res = ds.res[0]
    pad = int(maxdist * 1.15 / res) + 5
    row, col = ds.index(x, y)
    col0, row0 = max(0, col - pad), max(0, row - pad)
    col1, row1 = min(ds.width, col + pad), min(ds.height, row + pad)
    win = Window(col0, row0, col1 - col0, row1 - row0)
    data = ds.read(1, window=win, masked=True).astype("float64").filled(np.nan)
    inv = ~ds.window_transform(win)

    to_xy = Transformer.from_crs(moon_geo_crs(), ds.crs, always_xy=True)
    dists = np.arange(res, maxdist, res)
    az = np.arange(0.0, 360.0, az_step)
    lats, lons = destination(lat, lon, az, dists)
    xs, ys = to_xy.transform(lons.ravel(), lats.ravel())
    cols, rows = inv * (np.asarray(xs), np.asarray(ys))
    cols = np.rint(cols).astype(int).reshape(lats.shape)
    rows = np.rint(rows).astype(int).reshape(lats.shape)
    ok = (cols >= 0) & (cols < data.shape[1]) & (rows >= 0) & (rows < data.shape[0])
    h = np.full(lats.shape, np.nan)
    h[ok] = data[rows[ok], cols[ok]]

    # observer height = terrain at the site + mast
    r0, c0 = (int(round(v)) for v in (inv * (x, y))[::-1])
    h_site = data[r0, c0] if (0 <= r0 < data.shape[0] and 0 <= c0 < data.shape[1]) else np.nan
    h_site = 0.0 if np.isnan(h_site) else float(h_site)
    h0 = h_site + mast

    alpha = (dists / R_MOON)[None, :]
    elev = np.degrees(np.arctan2((R_MOON + h) * np.cos(alpha) - (R_MOON + h0),
                                 (R_MOON + h) * np.sin(alpha)))
    with np.errstate(all="ignore"):
        sky = np.nanmax(np.where(np.isnan(elev), -90.0, elev), axis=1)
    return az, sky, h_site, np.isnan(h).mean()


def cmd_skyline(a):
    import rasterio
    with rasterio.open(a.dem) as ds:
        lat, lon, x, y = site_lonlat(ds, a)
        print(f"Site: lat {lat:.4f}, lon {lon:.4f}  (x={x:.1f} m, y={y:.1f} m)")
        az, sky, h_site, miss = compute_skyline(ds, lat, lon, x, y, a.maxdist, a.mast)
    print(f"Site terrain height: {h_site:.1f} m, mast: {a.mast} m")
    print(f"Skyline: max {sky.max():.2f} deg at azimuth {az[sky.argmax()]:.0f} deg, "
          f"mean {sky.mean():.2f} deg, share of directions above 0 deg: {100 * (sky > 0).mean():.0f}%")
    print(f"Samples outside the file/no-data: {100 * miss:.1f}%")
    stem = f"skyline_{a.lat if a.lat is not None else 'xy'}_{a.lon if a.lon is not None else ''}"
    np.save(stem + ".npy", sky)
    print(f"Saved {stem}.npy (one value per degree of azimuth, 0 = north, clockwise)")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, ax = plt.subplots(figsize=(10, 3.2))
    ax.fill_between(az, sky, min(sky.min(), -1) - 1, color="0.5")
    ax.set_xlim(0, 360)
    ax.set_xlabel("Azimuth (deg, 0 = north)")
    ax.set_ylabel("Skyline elevation (deg)")
    ax.set_title("Terrain skyline seen from the site")
    fig.savefig(stem + ".png", dpi=120, bbox_inches="tight")
    print(f"Saved {stem}.png")


# --------------------------------------------------------------------------
# command 3: spice
# --------------------------------------------------------------------------
KERNELS = ["naif0012.tls", "de421.bsp", "moon_080317.tf", "moon_pa_de421_1900-2050.bpc"]


def cmd_spice(a):
    import spiceypy as sp
    for name in KERNELS:
        path = os.path.join(a.kernels, name)
        if not os.path.exists(path):
            sys.exit(f"Missing kernel: {path}\nDownload it from NASA NAIF generic_kernels "
                     "(lsk/, spk/planets/, fk/satellites/, pck/).")
        sp.furnsh(path)

    up, east, north = local_frame(a.lat, a.lon)
    site = (R_MOON + a.height) * up  # metres, Moon-fixed frame
    sky = np.load(a.skyline) if a.skyline else np.zeros(360)

    t0 = sp.str2et(f"{a.start} 00:00:00 UTC")
    n = int(a.days * 24 / a.step_hours)
    rows = []
    for k in range(n + 1):
        et = t0 + k * a.step_hours * 3600.0
        out = {}
        for body in ("SUN", "EARTH"):
            pos_km, _ = sp.spkpos(body, et, a.frame, "LT+S", "MOON")
            d = np.array(pos_km) * 1000.0 - site  # vector from the site to the body
            d /= np.linalg.norm(d)
            el = math.degrees(math.asin(float(np.clip(up @ d, -1, 1))))
            az = math.degrees(math.atan2(east @ d, north @ d)) % 360.0
            out[body] = (el, az)
        s_el, s_az = out["SUN"]
        e_el, e_az = out["EARTH"]
        sun_lit = (s_el + SUN_RADIUS_DEG) > sky[int(round(s_az)) % 360]
        earth_vis = e_el > sky[int(round(e_az)) % 360] + a.min_earth_elev
        rows.append((et, s_el, s_az, sun_lit, e_el, e_az, earth_vis))

    print(f"Site lat {a.lat}, lon {a.lon}, frame {a.frame}, skyline: "
          f"{'from file' if a.skyline else 'none (flat horizon)'}")
    print("UTC time               SunEl   SunAz  Lit | EarthEl EarthAz  Vis")
    per_row = max(1, int(24 / a.step_hours))
    for r in rows[::per_row]:
        print(f"{sp.et2utc(r[0], 'C', 0):<22} {r[1]:6.2f} {r[2]:7.1f}  {'Y' if r[3] else '-'}  | "
              f"{r[4]:6.2f} {r[5]:7.1f}   {'Y' if r[6] else '-'}")

    lit = np.mean([r[3] for r in rows])
    vis = np.mean([r[6] for r in rows])
    both = np.mean([r[3] and r[6] for r in rows])
    print(f"\nSunlit {100 * lit:.0f}% | Earth visible {100 * vis:.0f}% | both {100 * both:.0f}% of samples")
    for label, idx in (("Sun", 3), ("Earth", 6)):
        flips = [(rows[i][0], rows[i + 1][idx]) for i in range(len(rows) - 1) if rows[i][idx] != rows[i + 1][idx]]
        print(f"{label} changes (to within {a.step_hours} h):",
              ", ".join(f"{sp.et2utc(t, 'C', 0)} -> {'up' if up_ else 'down'}" for t, up_ in flips[:6]) or "none")


# --------------------------------------------------------------------------
def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("inspect", help="describe a terrain GeoTIFF")
    pi.add_argument("dem")
    pi.add_argument("--plot", action="store_true")
    pi.set_defaults(fn=cmd_inspect)

    ps = sub.add_parser("skyline", help="terrain horizon profile for one site")
    ps.add_argument("dem")
    ps.add_argument("--lat", type=float)
    ps.add_argument("--lon", type=float)
    ps.add_argument("--xy", type=float, nargs=2, metavar=("X", "Y"))
    ps.add_argument("--maxdist", type=float, default=15000.0, help="how far to look, metres")
    ps.add_argument("--mast", type=float, default=2.0, help="height of the observer above ground, metres")
    ps.set_defaults(fn=cmd_skyline)

    pk = sub.add_parser("spice", help="Sun/Earth elevation over time using SPICE kernels")
    pk.add_argument("kernels", help="folder containing the 4 kernel files")
    pk.add_argument("--lat", type=float, required=True)
    pk.add_argument("--lon", type=float, required=True)
    pk.add_argument("--height", type=float, default=0.0, help="site height above the 1737.4 km sphere, m")
    pk.add_argument("--start", default="2026-11-14", help="UTC date, YYYY-MM-DD")
    pk.add_argument("--days", type=float, default=29.6)
    pk.add_argument("--step-hours", type=float, default=1.0)
    pk.add_argument("--frame", default="MOON_ME", help="try MOON_ME_DE421 if MOON_ME is not found")
    pk.add_argument("--skyline", help=".npy file from the 'skyline' command")
    pk.add_argument("--min-earth-elev", type=float, default=0.0, help="antenna minimum elevation, degrees")
    pk.set_defaults(fn=cmd_spice)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
