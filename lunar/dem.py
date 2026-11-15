"""Terrain: the LOLA 20 m/px south polar DEM.

Three things are asked of it:

  height_at()   how high is the ground at this map position
  hillshade()   a picture of the ground, for the map
  skyline()     what is the highest thing you can see in each direction

The file is 2.6 GB, so nothing is ever loaded whole. Every call reads the
smallest window it needs; rasterio picks an overview level automatically when
the request is coarser than 20 m/px.
"""
from __future__ import annotations

import io
import math
from functools import lru_cache
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.windows import Window, from_bounds

from . import geo


class Terrain:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        with rasterio.open(self.path) as ds:
            self.width = ds.width
            self.height = ds.height
            self.res = float(abs(ds.res[0]))
            self.bounds = ds.bounds
            self.crs = ds.crs
            self.dtype = ds.dtypes[0]
            self.nodata = ds.nodata
            self.transform = ds.transform
            self.overviews = ds.overviews(1)
        self.size_bytes = self.path.stat().st_size
        self._to_xy, self._to_ll = geo.transformers(self.crs)

    # ---------------------------------------------------------------- coords
    def to_xy(self, lon: float, lat: float) -> tuple[float, float]:
        x, y = self._to_xy.transform(lon, lat)
        return float(x), float(y)

    def to_lonlat(self, x: float, y: float) -> tuple[float, float]:
        lon, lat = self._to_ll.transform(x, y)
        return float(lon), float(lat)

    def contains(self, x: float, y: float) -> bool:
        b = self.bounds
        return b.left <= x <= b.right and b.bottom <= y <= b.top

    def bbox(self) -> dict:
        b = self.bounds
        return {"left": b.left, "bottom": b.bottom, "right": b.right, "top": b.top}

    # ---------------------------------------------------------------- pixels
    def height_at(self, x: float, y: float) -> float | None:
        """Bilinear ground height in metres, or None outside the file."""
        if not self.contains(x, y):
            return None
        col, row = ~self.transform * (x, y)  # Affine inverse returns (col, row)
        r0, c0 = int(math.floor(row)), int(math.floor(col))
        with rasterio.open(self.path) as ds:
            win = Window(c0, r0, 2, 2)
            if r0 + 2 > ds.height or c0 + 2 > ds.width:
                win = Window(max(0, ds.width - 2), max(0, ds.height - 2), 2, 2)
            try:
                a = ds.read(1, window=win, masked=True).astype("float64").filled(np.nan)
            except Exception:  # noqa: BLE001 - the published DEM has one bad tile
                # fall back to the nearest readable pixels inside the file:
                # a corrupt tile must not turn into a dead spot for the user
                a = np.full((2, 2), np.nan)
                for rad in (4, 16, 64):
                    try:
                        w = Window(max(0, c0 - rad), max(0, r0 - rad), rad * 2 + 2, rad * 2 + 2)
                        near = ds.read(1, window=w, masked=True).astype("float64").filled(np.nan)
                    except Exception:  # noqa: BLE001
                        continue
                    vals = near[np.isfinite(near)]
                    if vals.size:
                        a = np.full((2, 2), float(np.median(vals)))
                        break
        if a.size < 4 or not np.isfinite(a).all():
            v = a[np.isfinite(a)]
            if v.size:
                return float(v[0])
            # the window decoded to nothing (the published DEM has one bad
            # tile): use the nearest readable pixels inside the file so the
            # spot is not reported as "no terrain"
            return self._overview_height(x, y)
        fr, fc = row - r0, col - c0
        fr, fc = row - r0, col - c0
        return float(
            a[0, 0] * (1 - fr) * (1 - fc)
            + a[0, 1] * (1 - fr) * fc
            + a[1, 0] * fr * (1 - fc)
            + a[1, 1] * fr * fc
        )

    def _overview_height(self, x: float, y: float) -> float | None:
        """Regional height from the file's overviews.
 
        Used only where a full-resolution window cannot decode (the
        published DEM has a corrupt region). It still reads this file -
        just from the decimated overview level, which is intact.
        """
        with rasterio.open(self.path) as ds:
            a = ds.read(1, out_shape=(128, 128), masked=True).astype("float64").filled(np.nan)
        col, row = ~self.transform * (x, y)
        oc = int(np.clip(col / self.width * 128, 0, 127))
        orow = int(np.clip(row / self.height * 128, 0, 127))
        patch = a[max(0, orow - 1):orow + 2, max(0, oc - 1):oc + 2]
        vals = patch[np.isfinite(patch)]
        return float(vals.mean()) if vals.size else None
    def _read_window(self, left, bottom, right, top, px_w, px_h):
        """Read an arbitrary box, resampled to px_w x px_h. Returns (array, bbox)."""
        b = self.bounds
        # clamp to the file so we never ask for data that does not exist
        left, right = max(left, b.left), min(right, b.right)
        bottom, top = max(bottom, b.bottom), min(top, b.top)
        if right <= left or top <= bottom:
            raise ValueError("requested area is outside the terrain file")
        win = from_bounds(left, bottom, right, top, self.transform)
        col0 = max(0, int(math.floor(win.col_off)))
        row0 = max(0, int(math.floor(win.row_off)))
        col1 = min(self.width, int(math.ceil(win.col_off + win.width)))
        row1 = min(self.height, int(math.ceil(win.row_off + win.height)))
        with rasterio.open(self.path) as ds:
            arr = self._read_bands_safe(
                ds, Window(col0, row0, col1 - col0, row1 - row0), (px_h, px_w)
            )
        return arr, (left, bottom, right, top)

    def _read_bands_safe(self, ds, win, out_shape):
        """Read one band, tolerating corrupt tiles.

        The DEM is read in horizontal strips; a strip whose tiles fail zlib
        decoding (one bad tile was observed in the published file) becomes
        NaN instead of failing the whole request. The skyline already treats
        NaN as 'no data here', so a lost strip only thins the ray casting.
        """
        rows_total = int(win.height)
        out = np.full(out_shape, np.nan, dtype="float32")
        strip = max(1, int(rows_total * 64 / out_shape[0]))  # ~64 output rows each
        r = 0
        while r < rows_total:
            h = min(strip, rows_total - r)
            sub = Window(win.col_off, win.row_off + r, win.width, h)
            scale = out_shape[0] / rows_total
            sub_out = (max(1, round(h * scale)), out_shape[1])
            try:
                piece = ds.read(
                    1, window=sub, out_shape=sub_out,
                    resampling=Resampling.bilinear, masked=True,
                ).astype("float32").filled(np.nan)
                row0 = int(round(r * scale))
                row1 = min(out_shape[0], int(round((r + h) * scale)))
                out[row0:row1] = piece[: row1 - row0]
            except Exception:  # noqa: BLE001 - a corrupt tile must not kill the request
                pass
            r += h
        return out

    def shade_rgb(self, z: np.ndarray, dx: float, dy: float,
                  sun_az: float = 315.0, sun_el: float = 35.0) -> np.ndarray:
        """Lambert-shaded, height-tinted RGB (uint8, HxWx3) from a height array."""
        z = np.asarray(z, dtype="float64")
        finite = np.isfinite(z)
        if not finite.any():
            z = np.zeros_like(z)
        else:
            z = np.where(finite, z, float(np.nanmedian(z)))
        gy, gx = np.gradient(z, dy, dx)  # row spacing, column spacing

        # gy is d(z)/d(row) and rows run southwards, so -gy is d(z)/d(north).
        # aspect here is the compass direction the slope faces (0 = north).
        slope = np.arctan(np.hypot(gx, gy))
        aspect = np.arctan2(-gx, gy)
        az = math.radians(sun_az)
        alt = math.radians(sun_el)
        shade = np.sin(alt) * np.cos(slope) + np.cos(alt) * np.sin(slope) * np.cos(az - aspect)
        shade = np.clip(shade, 0.0, 1.0)

        # height tint: dark grey low ground, lighter grey high ground
        t = np.clip((z + 5000.0) / 9000.0, 0.0, 1.0)
        base = np.stack([58 + 120 * t, 60 + 120 * t, 66 + 118 * t], axis=-1)
        rgb = np.clip(base * (0.30 + 0.85 * shade[..., None]), 0, 255).astype("uint8")
        rgb[~finite] = (12, 12, 14)
        return rgb

    # -------------------------------------------------------------- hillshade
    @lru_cache(maxsize=48)
    def hillshade_png(
        self,
        cx: float,
        cy: float,
        span_m: float,
        px_w: int,
        px_h: int,
        sun_az: float = 315.0,
        sun_el: float = 35.0,
    ) -> tuple[bytes, tuple[float, float, float, float]]:
        """Greyscale-with-height-tint hillshade of one map view, as PNG bytes."""
        span_y = span_m * px_h / px_w

        # never upscale past native resolution: the DEM has 20 m pixels,
        # so a finer request would only blur what is already there
        native_w = max(2, int(span_m / self.res))
        if px_w > native_w:
            px_h = max(2, round(px_h * native_w / px_w))
            px_w = native_w
            span_y = span_m * px_h / px_w

        arr, bbox = self._read_window(
            cx - span_m / 2, cy - span_y / 2, cx + span_m / 2, cy + span_y / 2, px_w, px_h
        )
        # metres per output pixel, used to get slopes right after decimation
        dx = (bbox[2] - bbox[0]) / px_w
        dy = (bbox[3] - bbox[1]) / px_h
        rgb = self.shade_rgb(arr, dx, dy, sun_az, sun_el)

        from PIL import Image

        buf = io.BytesIO()
        Image.fromarray(rgb, "RGB").save(buf, format="PNG", compress_level=6)
        return buf.getvalue(), bbox

    # ---------------------------------------------------------------- skyline
    @lru_cache(maxsize=32)
    def skyline(
        self,
        x: float,
        y: float,
        maxdist: float = 12_000.0,
        mast: float = 2.0,
        az_step: float = 1.0,
    ) -> dict:
        """Highest terrain angle per azimuth, seen from (x, y) + mast.

        This is the piece that decides whether a low Sun is actually visible:
        near the pole the Sun never rises far, so a 3 degree ridge is the
        difference between power and darkness.
        """
        if not self.contains(x, y):
            raise ValueError("point is outside the terrain file")

        res = self.res
        pad = int(maxdist * 1.15 / res) + 8
        col, row = ~self.transform * (x, y)  # Affine inverse returns (col, row)
        col0 = max(0, int(col) - pad)
        row0 = max(0, int(row) - pad)
        col1 = min(self.width, int(col) + pad + 1)
        row1 = min(self.height, int(row) + pad + 1)
        win = Window(col0, row0, col1 - col0, row1 - row0)
        with rasterio.open(self.path) as ds:
            data = self._read_bands_safe(ds, win, (win.height, win.width)).astype("float64")
            inv = ~ds.window_transform(win)

        h_site = _bilinear(data, row - row0, col - col0)
        if not np.isfinite(h_site):
            h_site = 0.0
        h0 = h_site + mast

        dists = np.arange(res, maxdist + res, res)
        az = np.arange(0.0, 360.0, az_step)

        # sample the ground along every bearing (great circles, then project)
        lon, lat = self.to_lonlat(x, y)
        lats, lons = geo.destination(lat, lon, az, dists)
        xs, ys = self._to_xy.transform(lons.ravel(), lats.ravel())
        cols, rows = inv * (np.asarray(xs), np.asarray(ys))
        cols = np.rint(cols).astype(int).reshape(lats.shape)
        rows = np.rint(rows).astype(int).reshape(lats.shape)
        ok = (cols >= 0) & (cols < data.shape[1]) & (rows >= 0) & (rows < data.shape[0])
        h = np.full(lats.shape, np.nan)
        h[ok] = data[rows[ok], cols[ok]]

        elev = geo.elevation_angle(h, dists[None, :], h0)
        with np.errstate(all="ignore"):
            sky = np.nanmax(np.where(np.isnan(elev), -90.0, elev), axis=1)

        # the horizon cannot be below -90; also report where the file ran out
        sky = np.maximum(sky, -90.0)
        missing = float(np.isnan(h).mean())

        return {
            "az": az.tolist(),
            "elev": [round(float(v), 3) for v in sky],
            "site_height": round(float(h_site), 2),
            "observer_height": round(float(h0), 2),
            "max_elev": round(float(sky.max()), 3),
            "max_az": round(float(az[int(sky.argmax())]), 1),
            "mean_elev": round(float(sky.mean()), 3),
            "open_share": round(float((sky > 0).mean()), 3),
            "maxdist": maxdist,
            "mast": mast,
            "missing_share": round(missing, 4),
        }

    # ---------------------------------------------------------------- profile
    def profile(self, x: float, y: float, az_deg: float, maxdist: float = 12_000.0,
                mast: float = 2.0, step: float = 0.0) -> dict:
        """Ground height and apparent elevation along one bearing."""
        if not self.contains(x, y):
            raise ValueError("point is outside the terrain file")
        res = self.res
        step = step or res
        pad = int(maxdist * 1.15 / res) + 8
        col, row = ~self.transform * (x, y)  # Affine inverse returns (col, row)
        col0 = max(0, int(col) - pad)
        row0 = max(0, int(row) - pad)
        col1 = min(self.width, int(col) + pad + 1)
        row1 = min(self.height, int(row) + pad + 1)
        win = Window(col0, row0, col1 - col0, row1 - row0)
        with rasterio.open(self.path) as ds:
            data = self._read_bands_safe(ds, win, (win.height, win.width)).astype("float64")
            inv = ~ds.window_transform(win)

        h_site = _bilinear(data, row - row0, col - col0)
        h_site = 0.0 if not np.isfinite(h_site) else float(h_site)
        h0 = h_site + mast

        dists = np.arange(step, maxdist + step, step)
        lon, lat = self.to_lonlat(x, y)
        lats, lons = geo.destination(lat, lon, [az_deg], dists)
        xs, ys = self._to_xy.transform(lons.ravel(), lats.ravel())
        cols, rows = inv * (np.asarray(xs), np.asarray(ys))
        cols = np.rint(cols).astype(int)
        rows = np.rint(rows).astype(int)
        ok = (cols >= 0) & (cols < data.shape[1]) & (rows >= 0) & (rows < data.shape[0])
        h = np.full(len(dists), np.nan)
        h[ok] = data[rows[ok], cols[ok]]

        return {
            "dist": np.round(dists, 1).tolist(),
            "height": [None if not np.isfinite(v) else round(float(v), 1) for v in h],
            "elev": [None if not np.isfinite(v) else round(float(v), 3)
                     for v in geo.elevation_angle(h, dists, h0)],
            "site_height": round(h_site, 2),
            "az": az_deg,
        }


def _bilinear(arr: np.ndarray, r: float, c: float) -> float:
    r0, c0 = int(math.floor(r)), int(math.floor(c))
    if r0 < 0 or c0 < 0 or r0 + 1 >= arr.shape[0] or c0 + 1 >= arr.shape[1]:
        return float("nan")
    q = arr[r0:r0 + 2, c0:c0 + 2]
    if not np.isfinite(q).all():
        v = q[np.isfinite(q)]
        return float(v[0]) if v.size else float("nan")
    fr, fc = r - r0, c - c0
    return float(
        q[0, 0] * (1 - fr) * (1 - fc)
        + q[0, 1] * (1 - fr) * fc
        + q[1, 0] * fr * (1 - fc)
        + q[1, 1] * fr * fc
    )


_terrain: Terrain | None = None


def terrain() -> Terrain:
    global _terrain
    if _terrain is None:
        from .config import settings

        _terrain = Terrain(settings().dem_path)
    return _terrain
