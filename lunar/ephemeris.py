"""Sun and Earth over a landing site, from the NAIF SPICE kernels.

The chain is short and worth stating plainly:

    de421.bsp                     where the Moon and Earth are
    moon_pa_de421_1900-2050.bpc   how the Moon is rotated (libration)
    moon_080317.tf                names that rotation MOON_ME_DE421
    naif0012.tls                  turns UTC into the clock SPICE uses

A site's local up/east/north vectors come from its lat/lon on the 1737.4 km
sphere - the same sphere the DEM uses - so terrain and sky are in one frame.

Sanity check built into scripts/selfcheck.py: at the lunar south pole the Sun
must sit at about +/-1.5 degrees, because that is the Moon's axial tilt. It does.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import numpy as np

from . import geo
from .config import FRAME, settings

_lock = threading.Lock()  # SPICE keeps global state; serialise all calls
_loaded: list[str] = []


def ensure_loaded() -> list[str]:
    """Load the four kernels once. Returns the names, in load order."""
    with _lock:
        for name, path in settings().kernel_paths.items():
            if str(path) not in _loaded:
                import spiceypy as sp

                sp.furnsh(str(path))
                _loaded.append(str(path))
    return [p.rsplit("/", 1)[-1] for p in _loaded]


def utc_to_et(iso: str) -> float:
    import spiceypy as sp

    ensure_loaded()
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    with _lock:
        return float(sp.str2et(dt.strftime("%Y-%m-%dT%H:%M:%S")))


def et_to_utc(et: float) -> str:
    import spiceypy as sp

    with _lock:
        return sp.et2utc(float(et), "ISOC", 1)


def body_vectors(body: str, ets: np.ndarray) -> np.ndarray:
    """Moon -> body position in the Moon-fixed frame, metres. Shape (3, n)."""
    import spiceypy as sp

    ensure_loaded()
    with _lock:
        pos, _ = sp.spkpos(body, np.asarray(ets, dtype=float), FRAME, "LT+S", "MOON")
    pos = np.asarray(pos, dtype=float)
    # spiceypy returns (n, 3) for an array of epochs and (3,) for a scalar one
    if pos.ndim == 1:
        pos = pos[:, None]
    elif pos.shape[0] != 3:
        pos = pos.T
    return pos * 1000.0


def coverage() -> tuple[float, float]:
    """Time span where both the ephemeris and the lunar orientation exist."""
    import spiceypy as sp

    ensure_loaded()
    s = settings()
    with _lock:
        c = sp.cell_double(2)
        sp.spkcov(str(s.kernel_paths["de421.bsp"]), 301, c)
        a, b = sp.wnfetd(c, 0)
        c2 = sp.cell_double(2)
        sp.pckcov(str(s.kernel_paths["moon_pa_de421_1900-2050.bpc"]), 31006, c2)
        a2, b2 = sp.wnfetd(c2, 0)
    return max(a, a2), min(b, b2)


def look_angles(lat: float, lon: float, height_m: float, ets: np.ndarray) -> dict:
    """Elevation and azimuth of the Sun and the Earth as seen from the site.

    Elevation is degrees above the local horizontal (curvature already included
    because the site sits on the sphere). Azimuth is degrees clockwise from north.
    """
    ets = np.atleast_1d(np.asarray(ets, dtype=float))
    up, east, north = geo.local_frame(lat, lon)
    site = (geo.R_MOON + height_m) * up  # metres, Moon-fixed

    out: dict[str, np.ndarray] = {}
    for body in ("SUN", "EARTH"):
        d = body_vectors(body, ets) - site[:, None]
        d /= np.linalg.norm(d, axis=0)
        el = np.degrees(np.arcsin(np.clip(up @ d, -1.0, 1.0)))
        az = np.degrees(np.arctan2(east @ d, north @ d)) % 360.0
        out[body] = {"el": el, "az": az}
    return out


def _skyline_lookup(sky: dict, az_deg: np.ndarray) -> np.ndarray:
    az = np.asarray(sky["az"], dtype=float)
    elev = np.asarray(sky["elev"], dtype=float)
    az = np.concatenate([az, [360.0]])
    elev = np.concatenate([elev, [elev[0]]])
    return np.interp(np.asarray(az_deg) % 360.0, az, elev)


def _runs(flags: np.ndarray) -> list[tuple[int, int]]:
    """Index ranges where a boolean array is True."""
    out, start = [], None
    for i, v in enumerate(flags):
        if v and start is None:
            start = i
        elif not v and start is not None:
            out.append((start, i - 1))
            start = None
    if start is not None:
        out.append((start, len(flags) - 1))
    return out


def timeline(
    x: float,
    y: float,
    sky: dict,
    start_iso: str,
    hours: float = 24.0,
    step_min: float = 10.0,
    min_earth_elev: float = 0.0,
) -> dict:
    """Sun and Earth elevation over time, and whether the terrain hides them."""
    from .dem import terrain

    t = terrain()
    lon, lat = t.to_lonlat(x, y)
    height = t.height_at(x, y)
    if height is None:
        raise ValueError("no terrain height at this point")

    et0 = utc_to_et(start_iso)
    step_s = max(10.0, float(step_min) * 60.0)
    n = int(round(hours * 3600.0 / step_s)) + 1
    ets = et0 + np.arange(n) * step_s

    lo, hi = coverage()
    if ets[0] < lo or ets[-1] > hi:
        raise ValueError(
            "requested window is outside the SPICE kernel coverage "
            f"({et_to_utc(lo)[:10]} to {et_to_utc(hi)[:10]} UTC)"
        )

    ang = look_angles(lat, lon, height, ets)
    sun, earth = ang["SUN"], ang["EARTH"]

    sun_sky = _skyline_lookup(sky, sun["az"])
    earth_sky = _skyline_lookup(sky, earth["az"])

    # a point is lit when the upper limb of the Sun clears the ridge
    lit = (sun["el"] + geo.SUN_RADIUS_DEG) > sun_sky
    vis = earth["el"] > np.maximum(earth_sky, min_earth_elev)

    times = [et_to_utc(et) for et in ets]
    runs = _runs(lit)
    # a run from sample a to b covers (b - a) intervals, not (b - a + 1)
    longest = max(((b - a) * step_s / 3600.0 for a, b in runs), default=0.0)

    def transitions(flags, label):
        out = []
        for i in range(1, len(flags)):
            if flags[i] != flags[i - 1]:
                out.append({"t": times[i], "to": "up" if flags[i] else "down", "what": label})
        return out

    trans = sorted(transitions(lit, "sun") + transitions(vis, "earth"), key=lambda r: r["t"])

    return {
        "t": times,
        "sun_el": np.round(sun["el"], 3).tolist(),
        "sun_az": np.round(sun["az"], 2).tolist(),
        "earth_el": np.round(earth["el"], 3).tolist(),
        "earth_az": np.round(earth["az"], 2).tolist(),
        "sun_sky": np.round(sun_sky, 3).tolist(),
        "earth_sky": np.round(earth_sky, 3).tolist(),
        "lit": lit.astype(int).tolist(),
        "earth_vis": vis.astype(int).tolist(),
        "transitions": trans[:24],
        "stats": {
            "sunlit_pct": round(100.0 * float(lit.mean()), 1),
            "earth_vis_pct": round(100.0 * float(vis.mean()), 1),
            "both_pct": round(100.0 * float((lit & vis).mean()), 1),
            "longest_sunlit_h": round(longest, 2),
            "sun_min_el": round(float(sun["el"].min()), 2),
            "sun_max_el": round(float(sun["el"].max()), 2),
            "earth_min_el": round(float(earth["el"].min()), 2),
            "earth_max_el": round(float(earth["el"].max()), 2),
            "horizon_max_deg": sky["max_elev"],
            "samples": n,
            "step_min": step_s / 60.0,
            "start": times[0],
            "end": times[-1],
        },
        "site": {
            "x": round(x, 1),
            "y": round(y, 1),
            "lon": round(lon, 4),
            "lat": round(lat, 4),
            "height": round(float(height), 2),
        },
    }
