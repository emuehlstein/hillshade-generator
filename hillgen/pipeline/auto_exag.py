"""
Auto-exaggeration computation for hillshade generation.

Uses elevation standard deviation from DEM stats to compute a base exaggeration
that achieves ~40 gray levels of visual contrast.

Ported from ilhmp/auto_exag.py.
"""

import json
import subprocess
from pathlib import Path


def compute_auto_exaggeration(dem_path: Path, target_contrast: float = 40.0) -> float:
    """
    Compute automatic vertical exaggeration from DEM statistics.

    Higher stddev → terrain has lots of natural contrast → less exaggeration needed.
    Lower stddev → flat terrain → needs heavy exaggeration to show features.

    Args:
        dem_path: Path to input DEM GeoTIFF
        target_contrast: Target gray level contrast (default 40)

    Returns:
        Computed base exaggeration factor, clamped to [0.5, 15.0]
    """
    dem_path = Path(dem_path)

    cmd = ["gdalinfo", "-stats", "-json", str(dem_path)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

    if result.returncode != 0:
        return 3.0  # safe default

    try:
        info = json.loads(result.stdout)
        bands = info.get("bands", [])
        if not bands:
            return 3.0

        stddev = bands[0].get("stdDev")
        minimum = bands[0].get("minimum", 0)
        maximum = bands[0].get("maximum", 0)

        if stddev is None or stddev <= 0:
            return 3.0

        elev_range = maximum - minimum

        # Primary: stddev-based
        exag = target_contrast / stddev

        # Terrain-type adjustments based on elevation range
        if elev_range < 50:
            # Very flat (coastal, plains) — needs heavy exaggeration
            exag = max(exag, 9.0)
        elif elev_range < 200:
            # Rolling terrain — moderate exaggeration
            exag = max(exag, 4.0)
        elif elev_range < 1000:
            # Hilly — light exaggeration
            exag = max(exag, 2.0)
        else:
            # Mountains — terrain speaks for itself, but at least 1.5x
            exag = max(exag, 1.5)

        return round(max(1.0, min(15.0, exag)), 1)

    except (json.JSONDecodeError, KeyError, TypeError):
        return 3.0
