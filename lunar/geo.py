"""Spherical geometry shared by the DEM and ephemeris code.

Everything here assumes a sphere of radius 1,737,400 m, which is the reference
surface of the LOLA polar products. Using a sphere (not an ellipsoid) keeps the
terrain grid and the SPICE vectors in the same frame - which is the whole point.
"""
from __future__ import annotations

import math

import numpy as np
from pyproj import CRS, Transformer

R_MOON = 1_737_400.0  # metres
SUN_RADIUS_DEG = 0.266  # mean angular radius of the Sun seen from the Moon


def moon_crs() -> CRS:
    """Planetocentric lat/lon on the 1737.4 km sphere (IAU_MOON style)."""
    return CRS.from_proj4(f"+proj=longlat +R={R_MOON} +no_defs")


_to_ll_cache: dict[str, Transformer] = {}


def transformers(dem_crs: CRS) -> tuple[Transformer, Transformer]:
    """(lon/lat -> map x/y, map x/y -> lon/lat) for a given DEM CRS."""
    key = dem_crs.to_wkt()
    if key not in _to_ll_cache:
        _to_ll_cache[key] = Transformer.from_crs(moon_crs(), dem_crs, always_xy=True)
        _to_ll_cache[key + "#inv"] = Transformer.from_crs(dem_crs, moon_crs(), always_xy=True)
    return _to_ll_cache[key], _to_ll_cache[key + "#inv"]


def destination(lat_deg: float, lon_deg: float, bearings_deg, dists_m):
    """Walk great circles from one point.

    bearings_deg: shape (nb,), dists_m: shape (ns,)
    Returns lat, lon arrays of shape (nb, ns).
    """
    lat1 = math.radians(lat_deg)
    lon1 = math.radians(lon_deg)
    th = np.radians(np.asarray(bearings_deg, dtype=float))[:, None]
    dl = (np.asarray(dists_m, dtype=float) / R_MOON)[None, :]
    lat2 = np.arcsin(np.sin(lat1) * np.cos(dl) + np.cos(lat1) * np.sin(dl) * np.cos(th))
    lon2 = lon1 + np.arctan2(
        np.sin(th) * np.sin(dl) * np.cos(lat1),
        np.cos(dl) - np.sin(lat1) * np.sin(lat2),
    )
    return np.degrees(lat2), (np.degrees(lon2) + 180.0) % 360.0 - 180.0


def local_frame(lat_deg: float, lon_deg: float):
    """Unit vectors Up, East, North at a lat/lon in the Moon-fixed frame."""
    lat, lon = math.radians(lat_deg), math.radians(lon_deg)
    up = np.array([math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat)])
    east = np.array([-math.sin(lon), math.cos(lon), 0.0])
    north = np.array([-math.sin(lat) * math.cos(lon), -math.sin(lat) * math.sin(lon), math.cos(lat)])
    return up, east, north


def xy_to_lonlat_closed_form(x, y):
    """Inverse south-polar stereographic, for the browser (same maths as pyproj).

    Verified against pyproj in scripts/selfcheck.py. Kept here so the frontend
    and the backend cannot drift apart.
    """
    rho = math.hypot(x, y)
    if rho == 0.0:
        return 0.0, -90.0
    lat = math.degrees(2.0 * math.atan(rho / (2.0 * R_MOON)) - math.pi / 2.0)
    lon = math.degrees(math.atan2(x, y))
    return lon, lat


def elevation_angle(h_target, dist, h_observer):
    """Angle above the local horizontal, accounting for curvature.

    h_target, dist, h_observer may be arrays; heights in metres above the sphere.
    """
    alpha = dist / R_MOON
    r1 = R_MOON + h_target
    r0 = R_MOON + h_observer
    return np.degrees(np.arctan2(r1 * np.cos(alpha) - r0, r1 * np.sin(alpha)))
