#!/usr/bin/env python3
"""Lunar Landing Site Navigator - web server.

    ./bin/python server.py                 # http://127.0.0.1:8000
    ./bin/python server.py --port 9000 --host 0.0.0.0

JSON API, one endpoint per question the interface asks:

    /api/status      what data is loaded, and is it intact
    /api/sites       the ROI polygons, with coordinates and heights
    /api/raster      a hillshade picture of any map window (PNG)
    /api/point       lon/lat/height for one map position
    /api/skyline     terrain horizon, one value per azimuth
    /api/profile     ground height along one bearing
    /api/ephemeris   Sun + Earth elevation over time, with shadowing applied
"""
from __future__ import annotations

import argparse
import io
import traceback

from flask import Flask, jsonify, request, send_file, send_from_directory
from werkzeug.exceptions import HTTPException

from lunar import config, ephemeris, globe as globe_mod, sites
from lunar.dem import terrain

app = Flask(__name__, static_folder=None)
app.config["JSON_SORT_KEYS"] = False


# --------------------------------------------------------------------- helpers
def _num(name: str, default: float, lo: float, hi: float) -> float:
    raw = request.args.get(name)
    if raw is None or raw == "":
        return default
    try:
        v = float(raw)
    except ValueError:
        raise BadRequest(f"{name} must be a number")
    if not (lo <= v <= hi):
        raise BadRequest(f"{name} must be between {lo} and {hi}")
    return v


class BadRequest(Exception):
    pass


@app.errorhandler(BadRequest)
def _bad_request(e):
    return jsonify({"ok": False, "error": str(e)}), 400


@app.errorhandler(Exception)
def _any_error(e):
    if isinstance(e, HTTPException):
        return e  # 404 and friends keep their own status code
    if isinstance(e, (BadRequest, ValueError)):
        return jsonify({"ok": False, "error": str(e)}), 400
    traceback.print_exc()
    return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), 500


# ----------------------------------------------------------------------- pages
@app.get("/")
def index():
    return send_from_directory(app.static_folder_path, "index.html")


@app.get("/<path:name>")
def static_file(name: str):
    return send_from_directory(app.static_folder_path, name)


# ------------------------------------------------------------------------ API
@app.get("/api/status")
def api_status():
    t = terrain()
    s = config.settings()
    kernels = []
    for name, path in s.kernel_paths.items():
        size = path.stat().st_size
        expected = config.KERNELS[name]
        kernels.append({
            "name": name,
            "bytes": size,
            "expected_bytes": expected,
            "ok": size == expected,
        })
    warnings = []
    if t.size_bytes != config.DEM_EXPECTED_BYTES:
        warnings.append(
            f"terrain file is {t.size_bytes:,} bytes, PGDA publishes "
            f"{config.DEM_EXPECTED_BYTES:,} - it may be truncated or a different product"
        )
    if any(not k["ok"] for k in kernels):
        warnings.append("one or more NAIF kernels do not match their published size")
    available = [x for x in sites.catalogue() if x.available]
    if not available:
        warnings.append("no SiteXX_ROIs.tgz found - the map will have no regions")

    gp = config.find_globe_texture()
    globe_info: dict = {"available": gp is not None, "path": str(gp) if gp else None}
    if gp:
        globe_info["bytes"] = gp.stat().st_size
    else:
        warnings.append(
            "no global Moon texture - the zoomed-out view is plain grey; "
            "run scripts/fetch_globe.py once to add it"
        )

    try:
        lo, hi = ephemeris.coverage()
        coverage = {"start": ephemeris.et_to_utc(lo)[:10], "end": ephemeris.et_to_utc(hi)[:10]}
    except Exception:
        coverage = None

    return jsonify({
        "ok": True,
        "dem": {
            "path": str(t.path),
            "filename": t.path.name,
            "width": t.width,
            "height": t.height,
            "res_m": t.res,
            "crs": t.crs.to_string() if t.crs else None,
            "crs_name": (t.crs.to_dict().get("proj") if t.crs else None),
            "bounds": t.bbox(),
            "bytes": t.size_bytes,
            "expected_bytes": config.DEM_EXPECTED_BYTES,
            "overviews": t.overviews,
        },
        "kernels": kernels,
        "frame": config.FRAME,
        "globe": globe_info,
        "coverage": coverage,
        "sites": [x.as_dict(with_rings=False) for x in sites.catalogue()],
        "warnings": warnings,
    })


@app.get("/api/sites")
def api_sites():
    return jsonify({
        "ok": True,
        "sites": [s.as_dict(with_rings=True) for s in sites.catalogue() if s.available],
    })


@app.get("/api/point")
def api_point():
    t = terrain()
    x = _num("x", 0.0, -1e9, 1e9)
    y = _num("y", 0.0, -1e9, 1e9)
    if not t.contains(x, y):
        raise BadRequest("point is outside the terrain file")
    lon, lat = t.to_lonlat(x, y)
    return jsonify({
        "ok": True,
        "x": round(x, 1),
        "y": round(y, 1),
        "lon": round(lon, 4),
        "lat": round(lat, 4),
        "height": t.height_at(x, y),
    })


@app.get("/api/raster")
def api_raster():
    """Hillshade PNG for one map window.

    The returned image is clipped to the terrain file; the real extent comes
    back in the X-BBox header as "left,bottom,right,top".
    """
    t = terrain()
    cx = _num("cx", 0.0, -1e9, 1e9)
    cy = _num("cy", 0.0, -1e9, 1e9)
    span = _num("span", 600_000.0, 200.0, 1e6)
    w = int(_num("w", 900, 64, 2048))
    h = int(_num("h", 700, 64, 2048))
    sun_az = _num("sun_az", 315.0, 0.0, 360.0)
    sun_el = _num("sun_el", 35.0, -90.0, 90.0)

    png, bbox = t.hillshade_png(
        round(cx, 1), round(cy, 1), round(span, 1), w, h, round(sun_az, 1), round(sun_el, 1)
    )
    resp = send_file(io.BytesIO(png), mimetype="image/png", max_age=3600)
    resp.headers["X-BBox"] = ",".join(f"{v:.1f}" for v in bbox)
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@app.get("/api/globe")
def api_globe():
    """Whole-Moon view for zoomed-out map spans.

    Same X-BBox contract as /api/raster; X-Sun-Subpoint and X-Earth-Subpoint
    additionally give "lat,lon" pairs for the labelled markers.
    """
    t = terrain()
    g = globe_mod.globe()
    if not g.available():
        raise BadRequest("global Moon texture not found - run scripts/fetch_globe.py once")
    cx = _num("cx", 0.0, -1e9, 1e9)
    cy = _num("cy", 0.0, -1e9, 1e9)
    span = _num("span", 8_000_000.0, 200.0, 12e6)
    w = int(_num("w", 1200, 64, 2048))
    h = int(_num("h", 900, 64, 2048))
    t_iso = request.args.get("t") or "2026-11-14T00:00:00"
    sun_az = _num("sun_az", 315.0, 0.0, 360.0)
    sun_el = _num("sun_el", 35.0, -90.0, 90.0)
    try:
        sun = ephemeris.body_subpoint("SUN", t_iso)
        earth = ephemeris.body_subpoint("EARTH", t_iso)
    except Exception:
        sun, earth = (0.0, 0.0), (0.0, 0.0)
    png, bbox = g.render(
        round(cx, 1), round(cy, 1), round(span, 1), w, h, sun, earth,
        dem=t, sun_az=round(sun_az, 1), sun_el=round(sun_el, 1),
    )
    resp = send_file(io.BytesIO(png), mimetype="image/png", max_age=3600)
    resp.headers["X-BBox"] = ",".join(f"{v:.1f}" for v in bbox)
    resp.headers["X-Sun-Subpoint"] = f"{sun[0]:.4f},{sun[1]:.4f}"
    resp.headers["X-Earth-Subpoint"] = f"{earth[0]:.4f},{earth[1]:.4f}"
    resp.headers["Cache-Control"] = "public, max-age=3600"
    return resp


@app.get("/api/globe-texture")
def api_globe_texture():
    """The global Moon texture for the 3D view, or 404 when not downloaded."""
    gp = config.find_globe_texture()
    if gp is None:
        raise BadRequest("global Moon texture not found - run scripts/fetch_globe.py once")
    resp = send_file(gp, mimetype="image/jpeg", max_age=86400)
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.get("/api/skyline")
def api_skyline():
    t = terrain()
    x = _num("x", sites.default_point()[0], -1e9, 1e9)
    y = _num("y", sites.default_point()[1], -1e9, 1e9)
    maxdist = _num("maxdist", 12_000.0, 500.0, 60_000.0)
    mast = _num("mast", 2.0, 0.0, 100.0)
    az_step = _num("az_step", 1.0, 0.25, 10.0)
    sky = t.skyline(round(x, 1), round(y, 1), maxdist, mast, az_step)
    lon, lat = t.to_lonlat(x, y)
    return jsonify({
        "ok": True,
        "site": {
            "x": round(x, 1), "y": round(y, 1),
            "lon": round(lon, 4), "lat": round(lat, 4),
            "height": sky["site_height"],
        },
        **sky,
    })


@app.get("/api/profile")
def api_profile():
    t = terrain()
    x = _num("x", 0.0, -1e9, 1e9)
    y = _num("y", 0.0, -1e9, 1e9)
    az = _num("az", 0.0, 0.0, 360.0)
    maxdist = _num("maxdist", 12_000.0, 200.0, 60_000.0)
    mast = _num("mast", 2.0, 0.0, 100.0)
    step = _num("step", 0.0, 0.0, 1000.0)
    return jsonify({"ok": True, **t.profile(x, y, az, maxdist, mast, step)})


@app.get("/api/ephemeris")
def api_ephemeris():
    t = terrain()
    x = _num("x", sites.default_point()[0], -1e9, 1e9)
    y = _num("y", sites.default_point()[1], -1e9, 1e9)
    maxdist = _num("maxdist", 12_000.0, 500.0, 60_000.0)
    mast = _num("mast", 2.0, 0.0, 100.0)
    az_step = _num("az_step", 1.0, 0.25, 10.0)
    hours = _num("hours", 24.0, 0.5, 24 * 40)
    step_min = _num("step_min", 10.0, 0.5, 720.0)
    min_earth = _num("min_earth_elev", 0.0, -90.0, 90.0)
    start = request.args.get("start", "2026-11-14T00:00:00")
    if "T" not in start:
        start += "T00:00:00"

    # keep the response sane: above 12k samples, quietly coarsen the step and
    # report the step actually used in stats.step_min
    if hours * 60.0 / max(step_min, 0.5) > 12_000:
        step_min = hours * 60.0 / 12_000

    sky = t.skyline(round(x, 1), round(y, 1), maxdist, mast, az_step)
    result = ephemeris.timeline(
        x, y, sky, start, hours=hours, step_min=step_min, min_earth_elev=min_earth
    )
    return jsonify({"ok": True, **result})


# ------------------------------------------------------------------------ main
app.static_folder_path = str(config.settings().web_dir)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--debug", action="store_true")
    a = p.parse_args()

    s = config.settings()
    print(f"terrain : {s.dem_path}  ({s.dem_path.stat().st_size:,} bytes)")
    print(f"kernels : {', '.join(s.kernel_paths)}")
    print(f"sites   : {', '.join(x.id for x in sites.catalogue() if x.available) or 'none'}")
    print(f"open    : http://{a.host}:{a.port}")
    app.run(host=a.host, port=a.port, debug=a.debug, threaded=True)


if __name__ == "__main__":
    main()
