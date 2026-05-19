"""Apply color theme to a grayscale hillshade.

Two color modes:
- 'ramp': gdaldem color-relief on the hillshade (input values 0-255)
- 'elevation': gdaldem color-relief on the DEM, modulated by hillshade
"""

import subprocess
from pathlib import Path
from typing import Optional

import numpy as np
import rasterio

from ..themes.registry import Theme


def apply_style(
    hillshade_path: Path,
    output_path: Path,
    theme: Theme,
    dem_path: Optional[Path] = None,
    progress_cb=None,
) -> Path:
    """Apply a theme's color ramp to a grayscale hillshade.

    Args:
        hillshade_path: Input grayscale hillshade GeoTIFF (Byte, 0-255)
        output_path: Output styled RGBA GeoTIFF
        theme: Theme with ramp and color_mode
        dem_path: Required for color_mode='elevation'
        progress_cb: Progress callback

    Returns:
        Path to output file
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    ramp_path = theme.ramp_path()

    if theme.color_mode == "elevation":
        if dem_path is None:
            raise ValueError("DEM path required for elevation color mode")
        return _apply_elevation_style(
            hillshade_path, dem_path, output_path, ramp_path, progress_cb
        )
    else:
        return _apply_ramp_style(
            hillshade_path, output_path, ramp_path, progress_cb
        )


def _apply_ramp_style(
    hillshade_path: Path,
    output_path: Path,
    ramp_path: Path,
    progress_cb=None,
) -> Path:
    """Apply color-relief ramp directly to hillshade values (0-255)."""
    if progress_cb:
        progress_cb(f"Styling with ramp: {ramp_path.stem}")

    # gdaldem color-relief maps input values → RGBA via the ramp file
    cmd = [
        "gdaldem", "color-relief",
        str(hillshade_path),
        str(ramp_path),
        str(output_path),
        "-alpha",
        "-co", "COMPRESS=LZW",
        "-co", "TILED=YES",
        "-co", "BIGTIFF=YES",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gdaldem color-relief failed: {result.stderr}")

    if progress_cb:
        size_mb = output_path.stat().st_size / (1024 * 1024)
        progress_cb(f"Styled: {output_path.name} ({size_mb:.1f} MB)")

    return output_path


def _apply_elevation_style(
    hillshade_path: Path,
    dem_path: Path,
    output_path: Path,
    ramp_path: Path,
    progress_cb=None,
) -> Path:
    """Apply color-relief ramp to DEM elevation values, modulated by hillshade.

    1. gdaldem color-relief on DEM → elevation-colored RGBA
    2. Multiply RGB channels by hillshade (normalized to 0-1) for 3D effect
    """
    if progress_cb:
        progress_cb(f"Elevation styling with ramp: {ramp_path.stem}")

    # Step 1: color-relief on DEM
    elev_colored = output_path.with_name(output_path.stem + "_elev_tmp.tif")

    cmd = [
        "gdaldem", "color-relief",
        str(dem_path),
        str(ramp_path),
        str(elev_colored),
        "-alpha",
        "-co", "COMPRESS=LZW",
        "-co", "TILED=YES",
        "-co", "BIGTIFF=YES",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gdaldem color-relief (elevation) failed: {result.stderr}")

    if progress_cb:
        progress_cb("  Modulating by hillshade...")

    # Step 2: multiply RGB by hillshade for 3D effect
    with rasterio.open(elev_colored) as elev_src:
        elev_data = elev_src.read()  # (4, H, W) — RGBA
        profile = elev_src.profile.copy()

    with rasterio.open(hillshade_path) as hs_src:
        hs_data = hs_src.read(1).astype(np.float64) / 255.0  # normalize to 0-1

    # Modulate RGB channels, keep alpha
    for band in range(3):  # R, G, B
        elev_data[band] = np.clip(
            elev_data[band].astype(np.float64) * hs_data, 0, 255
        ).astype(np.uint8)

    with rasterio.open(output_path, "w", **profile) as dst:
        dst.write(elev_data)

    # Clean up temp
    if elev_colored.exists():
        elev_colored.unlink()

    if progress_cb:
        size_mb = output_path.stat().st_size / (1024 * 1024)
        progress_cb(f"Styled: {output_path.name} ({size_mb:.1f} MB)")

    return output_path
