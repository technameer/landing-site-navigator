"""Lunar Landing Site Navigator - core library.

Four modules, one job each:

  config      where the data files live
  geo         spherical geometry shared by everything else
  dem         the LOLA terrain GeoTIFF (heights, hillshade, skyline)
  roi         the Product 78 region-of-interest shapefiles
  ephemeris   Sun and Earth positions from the NAIF SPICE kernels
  sites       glues roi + dem into a small site catalogue
"""

__version__ = "0.1.0"
