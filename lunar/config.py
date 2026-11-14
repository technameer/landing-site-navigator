"""Where the data lives.

Nothing is hard-coded to one machine: each file is looked up in a short list of
places, and an environment variable wins over all of them.

    LUNAR_DEM       full path to the terrain GeoTIFF
    LUNAR_KERNELS   folder holding the four NAIF kernels
    LUNAR_ROIS      extra folder to search for SiteXX_ROIs.tgz
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

DEM_FILENAME = "LDEM_80S_20MPP_ADJ.TIF"
DEM_EXPECTED_BYTES = 2_696_082_051

# filename -> exact byte size published by PGDA (used as an integrity check)
KERNELS = {
    "naif0012.tls": 5_257,
    "de421.bsp": 16_790_528,
    "moon_pa_de421_1900-2050.bpc": 1_770_496,
    "moon_080317.tf": 21_437,
}

FRAME = "MOON_ME_DE421"  # the frame Product 90 registers its DEMs in

# Optional whole-Moon texture for the zoomed-out view (equirectangular,
# longitude -180..180, latitude +90 at the top row). Fetched once with
# scripts/fetch_globe.py; the app works fine without it.
GLOBE_TEXTURE = "moon_global.jpg"


def find_globe_texture() -> Path | None:
    """Path to the global texture, or None when it has not been downloaded."""
    for p in _candidates("LUNAR_GLOBE", GLOBE_TEXTURE, []):
        if p.is_file():
            return p
    return None


def _candidates(env: str, filename: str, extra: list[Path]) -> list[Path]:
    out: list[Path] = []
    if os.environ.get(env):
        out.append(Path(os.environ[env]).expanduser())
    out += [
        ROOT / filename,
        ROOT / "data" / filename,
        ROOT / "kernels" / filename,
        ROOT / "data" / "kernels" / filename,
        *[p / filename for p in extra],
        Path("/mnt/data") / filename,
    ]
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def find_dem() -> Path:
    for p in _candidates("LUNAR_DEM", DEM_FILENAME, []):
        if p.is_file():
            return p
    tried = "\n  ".join(str(p) for p in _candidates("LUNAR_DEM", DEM_FILENAME, []))
    raise FileNotFoundError(
        f"Terrain file {DEM_FILENAME} not found. Looked in:\n  {tried}\n"
        "Set LUNAR_DEM=/full/path/to/the/file.tif to point at it."
    )


def find_kernels() -> dict[str, Path]:
    env_dir = os.environ.get("LUNAR_KERNELS")
    extra = [Path(env_dir).expanduser()] if env_dir else []
    found, missing = {}, []
    for name in KERNELS:
        for p in _candidates("LUNAR_KERNELS", name, extra):
            if p.is_file():
                found[name] = p
                break
        else:
            missing.append(name)
    if missing:
        raise FileNotFoundError(
            "Missing NAIF kernel(s): " + ", ".join(missing) + "\n"
            "They belong in the project folder. Set LUNAR_KERNELS=<folder> if they live elsewhere."
        )
    return found


def find_roi_archives() -> list[Path]:
    """Every SiteXX_ROIs.tgz we can see, sorted Site01, Site04, ..."""
    folders = [ROOT, ROOT / "data" / "rois"]
    if os.environ.get("LUNAR_ROIS"):
        folders.append(Path(os.environ["LUNAR_ROIS"]).expanduser())
    hits: dict[str, Path] = {}
    for folder in folders:
        if not folder.is_dir():
            continue
        for p in sorted(folder.glob("*_ROIs.tgz")):
            hits.setdefault(p.name, p)  # first folder wins
    return sorted(hits.values(), key=lambda p: _natural_key(p.name))


def _natural_key(name: str):
    """Site2 < Site11, so string sort does not put 11 before 2."""
    import re

    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", name)]


@dataclass(frozen=True)
class Settings:
    dem_path: Path
    kernel_paths: dict[str, Path]
    roi_archives: list[Path]
    web_dir: Path = ROOT / "web"

    @property
    def kernel_dir(self) -> Path:
        return next(iter(self.kernel_paths.values())).parent


_settings: Settings | None = None


def settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings(
            dem_path=find_dem(),
            kernel_paths=find_kernels(),
            roi_archives=find_roi_archives(),
        )
    return _settings
