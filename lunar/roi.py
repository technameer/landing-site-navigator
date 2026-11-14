"""Region-of-interest shapefiles (PGDA Product 78).

A shapefile is three files: .shp (geometry), .shx (index), .dbf (attributes).
They ship inside SiteXX_ROIs.tgz. We only need the polygons, so this is a
small, dependency-free reader for the polygon shape types - no GDAL, no pyshp.

The .prj is read too, so the app can warn if a site file was made on a
different sphere than the terrain grid.
"""
from __future__ import annotations

import io
import math
import struct
import tarfile
from dataclasses import dataclass, field
from pathlib import Path

# ESRI shape types we understand
POLYGON, POLYGON_Z, POLYGON_M = 5, 15, 25


@dataclass
class Roi:
    index: int
    ring: list[tuple[float, float]]  # projected metres, closed ring
    attrs: dict[str, float] = field(default_factory=dict)
    area_m2: float = 0.0

    @property
    def centre(self) -> tuple[float, float]:
        xs = [p[0] for p in self.ring]
        ys = [p[1] for p in self.ring]
        return sum(xs) / len(xs), sum(ys) / len(ys)

    @property
    def extent(self) -> tuple[float, float]:
        xs = [p[0] for p in self.ring]
        ys = [p[1] for p in self.ring]
        return max(xs) - min(xs), max(ys) - min(ys)


def _read_shp(blob: bytes) -> list[list[tuple[float, float]]]:
    if struct.unpack(">i", blob[0:4])[0] != 9994:
        raise ValueError("not a shapefile (.shp signature is not 9994)")
    shape_type = struct.unpack("<i", blob[32:36])[0]
    if shape_type not in (POLYGON, POLYGON_Z, POLYGON_M):
        raise ValueError(f"unsupported shape type {shape_type} (expected a polygon)")

    rings: list[list[tuple[float, float]]] = []
    off = 100
    while off + 8 <= len(blob):
        _rec, words = struct.unpack(">2i", blob[off:off + 8])
        off += 8
        rec_end = off + words * 2
        stype = struct.unpack("<i", blob[off:off + 4])[0]
        if stype in (POLYGON, POLYGON_Z, POLYGON_M):
            n_parts, n_pts = struct.unpack("<2i", blob[off + 36:off + 44])
            parts = struct.unpack(f"<{n_parts}i", blob[off + 44:off + 44 + 4 * n_parts])
            pts_at = off + 44 + 4 * n_parts
            pts = [struct.unpack("<2d", blob[pts_at + 16 * i:pts_at + 16 * i + 16])
                   for i in range(n_pts)]
            bounds = list(parts) + [n_pts]
            for a, b in zip(bounds[:-1], bounds[1:]):
                if b > a:
                    rings.append(pts[a:b])
        off = rec_end
    return rings


def _read_dbf(blob: bytes) -> list[dict[str, float]]:
    """Attribute table -> one dict per record (numbers only, which is all we need)."""
    if len(blob) < 32:
        return []
    n_rec, = struct.unpack("<I", blob[4:8])
    header_len, rec_len = struct.unpack("<2H", blob[8:12])
    fields, pos = [], 32
    while pos + 32 <= header_len and blob[pos] != 0x0D:
        name = blob[pos:pos + 11].split(b"\0")[0].decode("latin-1").strip()
        ftype = chr(blob[pos + 11])
        length = blob[pos + 16]
        fields.append((name, ftype, length))
        pos += 32
    rows, pos = [], header_len
    for _ in range(n_rec):
        rec = blob[pos:pos + rec_len]
        pos += rec_len
        if len(rec) < rec_len:
            break
        row, cursor = {}, 1
        for name, ftype, length in fields:
            raw = rec[cursor:cursor + length].decode("latin-1").strip()
            cursor += length
            if ftype in "NF" and raw:
                try:
                    row[name] = float(raw)
                except ValueError:
                    pass
        rows.append(row)
    return rows


def _shoelace(ring) -> float:
    s = 0.0
    for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
        s += x0 * y1 - x1 * y0
    return abs(s) / 2.0


def read_archive(path: Path | str) -> tuple[list[Roi], str]:
    """Read SiteXX_ROIs.tgz (or a folder of the same files). Returns (rois, prj_text)."""
    path = Path(path)
    members: dict[str, bytes] = {}
    if path.is_dir():
        for p in path.iterdir():
            if p.is_file():
                members[p.name] = p.read_bytes()
    else:
        with tarfile.open(path, "r:gz") as tar:
            for m in tar.getmembers():
                if not m.isfile():
                    continue  # the archives contain one dangling symlink
                f = tar.extractfile(m)
                if f is not None:
                    members[Path(m.name).name] = f.read()

    shp = next((b for n, b in members.items() if n.lower().endswith(".shp")), None)
    if shp is None:
        raise ValueError(f"no .shp inside {path.name}")

    rings = _read_shp(shp)
    dbf = next((b for n, b in members.items() if n.lower().endswith(".dbf")), None)
    attrs = _read_dbf(dbf) if dbf else []
    prj = next((b.decode("latin-1") for n, b in members.items() if n.lower().endswith(".prj")), "")

    # keep only outer rings: an inner ring is fully contained in another one.
    # The Product 78 files are all simple single-ring polygons, so this is a
    # cheap safety net rather than a real topology builder.
    rois: list[Roi] = []
    for i, ring in enumerate(rings, start=1):
        if len(ring) < 4:
            continue
        if ring[0] != ring[-1]:
            ring = ring + [ring[0]]
        rois.append(
            Roi(
                index=i,
                ring=[(float(x), float(y)) for x, y in ring],
                attrs=attrs[i - 1] if i - 1 < len(attrs) else {},
                area_m2=_shoelace(ring),
            )
        )
    return rois, prj


def sphere_radius_from_prj(prj: str) -> float | None:
    """Pull the sphere radius out of a .prj WKT string, if it is there."""
    marker = "SPHEROID["
    i = prj.find(marker)
    if i < 0:
        return None
    try:
        inside = prj[i + len(marker):].split("]")[0]
        parts = [p.strip().strip('"') for p in inside.split(",")]
        return float(parts[1])
    except (IndexError, ValueError):
        return None


def polygon_contains(ring, x: float, y: float) -> bool:
    inside = False
    for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
        if (y0 > y) != (y1 > y):
            xin = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
            if x < xin:
                inside = not inside
    return inside


def ring_area_km2(ring) -> float:
    return _shoelace(ring) / 1e6
