"""DEM source registry and resolution."""

from .base import DEMSource, BBox
from .usgs_3dep import USGS3DEP10m
from .nps_sfm_rainier import NPSSfMRainier2021
from .wi_dnr_lidar import WiDNRLiDAR
from .igic_indiana_lidar import IGICIndianaLiDAR
from .isgs_ilhmp import ISGSILHMPSource

# Built-in sources, ordered by priority (highest first).
# resolve_source(auto) picks the first source whose covers(bbox) is True.
# Higher priority = finer resolution = preferred when coverage overlaps.
_SOURCES = [
    NPSSfMRainier2021(),   # priority=95 — 0.67m, MORA park boundary only
    IGICIndianaLiDAR(),    # priority=92 — 0.76m, Indiana only (all 92 counties)
    ISGSILHMPSource(),     # priority=88 — 0.3m,  Illinois only (all 102 counties)
    WiDNRLiDAR(),          # priority=90 — 1m,    Wisconsin only
    USGS3DEP10m(),         # priority=80 — 10m,   CONUS/AK/HI fallback
]

# Lookup by name
_SOURCE_MAP = {s.name: s for s in _SOURCES}


def get_source(name: str) -> DEMSource:
    """Get a DEM source by name."""
    if name not in _SOURCE_MAP:
        available = ", ".join(_SOURCE_MAP.keys())
        raise ValueError(f"Unknown DEM source: {name}. Available: {available}")
    return _SOURCE_MAP[name]


def resolve_source(bbox: BBox, name: str = "auto") -> DEMSource:
    """Resolve the best DEM source for a bounding box.

    If name is 'auto', picks the highest-priority source that covers the bbox.
    Otherwise, returns the named source (and validates coverage).
    """
    if name != "auto":
        source = get_source(name)
        if not source.covers(bbox):
            raise ValueError(
                f"DEM source '{name}' does not cover bbox "
                f"({bbox.west:.2f},{bbox.south:.2f},{bbox.east:.2f},{bbox.north:.2f})"
            )
        return source

    for source in _SOURCES:
        if source.covers(bbox):
            return source

    raise ValueError(
        f"No DEM source covers bbox "
        f"({bbox.west:.2f},{bbox.south:.2f},{bbox.east:.2f},{bbox.north:.2f})"
    )


def list_sources():
    """List all available DEM sources."""
    return list(_SOURCES)
