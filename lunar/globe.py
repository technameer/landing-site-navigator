"""Whole-Moon backdrop for zoomed-out map views.

The map projection (south-polar stereographic) maps the southern hemisphere
into a disc of radius 2R centred on the pole: latitude -90 is the centre,
latitude 0 (the equator) is the rim. Where the polar DEM covers the view we
draw real terrain, exactly like /api/raster; everywhere else we sample a
global equirectangular texture (LROC WAC colour mosaic, downloaded once by
scripts/fetch_globe.py). Day and night come from the real Sun direction at
the requested time, computed with SPICE in server.py and passed in here.
"""
from __future__ import annotations

import io
import math
from functools import lru_cache

import numpy as np
from PIL import Image

from . import config, geo

EQUATOR_R = 2.0 * geo.R_MOON  # stereographic radius of latitude 0


class Globe:
    def __init__(self):
        self.path: str | None = None
        self.tex: np.ndarray | None = None  # (H, W, 3) uint8, row 0 = +90 lat

    def available(self) -> bool:
        return self.load()

    def load(self) -> bool:
        if self.tex is not None:
            return True
        path = config.find_globe_texture()
        if path is None:
            return False
        img = Image.open(path).convert("RGB")
        if img.width > 4096:  # plenty for a 2048 px render; keeps memory sane
            img = img.resize((4096, round(img.height * 4096 / img.width)), Image.LANCZOS)
        self.path = str(path)
        self.tex = np.asarray(img, dtype=np.uint8)
        return True

    def _sample(self, lon_deg: np.ndarray, lat_deg: np.ndarray) -> np.ndarray:
        """Bilinear texture lookup, RGB float32, same shape as the input grids."""
        H, W = self.tex.shape[:2]
        u = np.clip((lon_deg + 180.0) / 360.0, 0.0, 1.0) * (W - 1)
        v = np.clip((90.0 - lat_deg) / 180.0, 0.0, 1.0) * (H - 1)
        x0 = np.floor(u).astype(int)
        y0 = np.floor(v).astype(int)
        x1 = np.minimum(x0 + 1, W - 1)
        y1 = np.minimum(y0 + 1, H - 1)
        fx = (u - x0)[..., None]
        fy = (v - y0)[..., None]
        a = self.tex[y0, x0].astype(np.float32)
        b = self.tex[y0, x1].astype(np.float32)
        c = self.tex[y1, x0].astype(np.float32)
        d = self.tex[y1, x1].astype(np.float32)
        return a * (1 - fx) * (1 - fy) + b * fx * (1 - fy) + c * (1 - fx) * fy + d * fx * fy

    def render(
        self,
        cx: float,
        cy: float,
        span_m: float,
        px_w: int,
        px_h: int,
        sun_ll: tuple[float, float],
        earth_ll: tuple[float, float],
        dem=None,
        sun_az: float = 0.0,
        sun_el: float = 0.0,
    ) -> tuple[bytes, tuple[float, float, float, float]]:
        """Render one map window as PNG bytes; its extent in metres comes back too.

        sun_ll / earth_ll are (lat, lon) sub-points in MOON_ME_DE421. When
        `dem` (a Terrain) is given, whatever part of the view sits inside its
        footprint is drawn from real terrain instead of the texture.
        """
        span_y = span_m * px_h / px_w
        bbox = (cx - span_m / 2, cy - span_y / 2, cx + span_m / 2, cy + span_y / 2)

        xs = np.linspace(bbox[0], bbox[2], px_w)
        ys = np.linspace(bbox[1], bbox[3], px_h)
        X, Y = np.meshgrid(xs, ys)
        rho = np.hypot(X, Y)
        lon = np.degrees(np.arctan2(X, Y))
        lat = np.degrees(2.0 * np.arctan(rho / (2.0 * geo.R_MOON)) - np.pi / 2.0)
        disc = rho <= EQUATOR_R * 1.0005

        # Lambert day/night from the real Sun direction, on the sphere
        slat, slon = map(math.radians, sun_ll)
        s = np.array([math.cos(slat) * math.cos(slon),
                      math.cos(slat) * math.sin(slon),
                      math.sin(slat)])
        lat_r, lon_r = np.radians(lat), np.radians(lon)
        n = np.stack([np.cos(lat_r) * np.cos(lon_r),
                      np.cos(lat_r) * np.sin(lon_r),
                      np.sin(lat_r)], axis=-1)
        lit = n @ s
        # the ambient floor keeps the night side readable
        day = (0.45 + 0.55 * np.clip(lit, 0.0, 1.0) ** 0.6)[..., None]

        if self.tex is not None:
            rgb = self._sample(lon, lat) * day
        else:
            rgb = np.full((*lat.shape, 3), 112.0, np.float32) * day

        # real terrain where the DEM covers the view
        if dem is not None:
            try:
                arr, (l, b, r, t) = dem._read_window(*bbox, px_w, px_h)
            except ValueError:
                arr = None
            if arr is not None:
                dxm = (r - l) / px_w
                dym = (t - b) / px_h
                shade = dem.shade_rgb(arr, dxm, dym, sun_az, sun_el).astype(np.float32)
                have = np.isfinite(arr)
                # keep the polar patch readable even on the night side
                rgb[have] = shade[have] * np.clip(day[have], 0.60, 1.0)

        rgb[~disc] = np.array([8.0, 9.0, 11.0], np.float32)  # space

        # a faint star field outside the disc, fixed per view (not per pixel noise)
        if (~disc).any():
            rng = np.random.default_rng(7)
            stars = rng.random((px_h, px_w))
            twinkle = (stars > 0.9992) & ~disc
            rgb[twinkle] = np.array([235.0, 238.0, 245.0], np.float32)

        buf = io.BytesIO()
        Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), "RGB").save(
            buf, format="PNG", compress_level=6
        )
        return buf.getvalue(), bbox


@lru_cache(maxsize=1)
def globe() -> Globe:
    return Globe()
