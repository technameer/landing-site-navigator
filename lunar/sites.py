"""The site catalogue: which ROI archives exist, and what is inside them.

Names come from the PGDA Product 78 page. Only the archives actually present on
disk are offered by the app; the rest are listed so the UI can say what is
missing instead of silently showing one site.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from . import config, roi as roi_mod

# PGDA Product 78 site list (verified against the page on 2026-11-12)
SITE_NAMES = {
    "Site01": "Connecting ridge",
    "Site04": "Shackleton rim",
    "Site06": "Nobile rim 1",
    "Site07": "Peak near Shackleton",
    "Site11": "de Gerlache rim",
    "Site20": "Leibnitz beta plateau",
    "Site20v2": "Leibnitz beta plateau (extended)",
    "Site23": "Malapert massif",
    "Site42": "de Gerlache-Kocher massif",
    "Haworth": "Haworth",
    "Shoemaker": "Shoemaker",
    "DM1": "Amundsen rim",
    "DM2": "Nobile rim 2",
    "SL2": "de Gerlache rim",
    "SL3": "Connecting ridge extension",
    "NPA": "Cabeus exterior wall 1",
    "NPB": "Amundsen 1",
    "NPC": "Idel'son L crater 1",
    "NPD": "Malapert crater 1",
    "LM1": "Shackleton Rim B",
    "LM2": "Shoemaker Rim A",
    "LM3": "Shoemaker Rim B",
    "LM4": "Shoemaker Rim C",
    "LM5": "Shoemaker Rim D",
    "LM6": "Shoemaker Rim E",
    "LM7": "Faustini Rim A",
    "LM8": "Shoemaker Rim F",
}


@dataclass
class RoiView:
    id: str
    index: int
    x: float
    y: float
    lon: float
    lat: float
    height: float | None
    area_km2: float
    extent_m: tuple[float, float]
    ring: list[tuple[float, float]]

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "index": self.index,
            "x": round(self.x, 1),
            "y": round(self.y, 1),
            "lon": round(self.lon, 4),
            "lat": round(self.lat, 4),
            "height": None if self.height is None else round(self.height, 1),
            "area_km2": round(self.area_km2, 4),
            "extent_m": [round(self.extent_m[0], 1), round(self.extent_m[1], 1)],
            "ring": [[round(px, 1), round(py, 1)] for px, py in self.ring],
        }


@dataclass
class SiteView:
    id: str
    name: str
    archive: str | None
    available: bool
    rois: list[RoiView]
    prj_warning: str | None = None

    def as_dict(self, with_rings: bool = True) -> dict:
        d = {
            "id": self.id,
            "name": self.name,
            "archive": self.archive,
            "available": self.available,
            "roi_count": len(self.rois),
            "prj_warning": self.prj_warning,
        }
        if with_rings:
            d["rois"] = [r.as_dict() for r in self.rois]
        return d


def _site_id(filename: str) -> str:
    return filename.split("_ROIs")[0]


@lru_cache(maxsize=1)
def catalogue() -> list[SiteView]:
    """Every known site, with polygons for the ones we have files for."""
    from .dem import terrain

    t = terrain()
    archives = {_site_id(p.name): p for p in config.settings().roi_archives}

    order = list(SITE_NAMES) + [s for s in archives if s not in SITE_NAMES]
    out: list[SiteView] = []
    for site_id in order:
        archive = archives.get(site_id)
        if archive is None:
            out.append(SiteView(site_id, SITE_NAMES.get(site_id, site_id), None, False, []))
            continue
        out.append(_load(site_id, archive, t))
    # available sites first, then by site number
    out.sort(key=lambda s: (not s.available, config._natural_key(s.id)))
    return out


def _load(site_id: str, archive: Path, t) -> SiteView:
    rois, prj = roi_mod.read_archive(archive)
    warning = None
    r = roi_mod.sphere_radius_from_prj(prj)
    if r is not None and abs(r - 1_737_400.0) > 1.0:
        warning = f"{archive.name} uses a {r:.0f} m sphere, terrain uses 1737400 m"

    views: list[RoiView] = []
    for r_obj in rois:
        cx, cy = r_obj.centre
        lon, lat = t.to_lonlat(cx, cy)
        views.append(
            RoiView(
                id=f"{site_id}-R{r_obj.index}",
                index=r_obj.index,
                x=cx,
                y=cy,
                lon=lon,
                lat=lat,
                height=t.height_at(cx, cy),
                area_km2=roi_mod.ring_area_km2(r_obj.ring),
                extent_m=r_obj.extent,
                ring=r_obj.ring,
            )
        )
    return SiteView(site_id, SITE_NAMES.get(site_id, site_id), archive.name, True, views, warning)


def get(site_id: str) -> SiteView:
    for s in catalogue():
        if s.id == site_id:
            return s
    raise KeyError(site_id)


def default_point() -> tuple[float, float]:
    """A sensible starting point: centre of the first ROI we have."""
    for s in catalogue():
        if s.available and s.rois:
            return s.rois[0].x, s.rois[0].y
    return 0.0, 0.0
