"""Grayscale hillshade generation via gdaldem.

Supports four shading modes:
- standard: single azimuth/altitude
- multidirectional: GDAL -multidirectional
- igor: GDAL -igor (oblique angle variant)
- combined: GDAL -combined (multi-light)

Composite shading blends multiple modes with configurable weights.
Each sub-layer is cached independently so changing weights or switching
between composite and single-mode themes never re-runs gdaldem.
"""

import subprocess
from enum import Enum
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np


class ShadingMode(str, Enum):
    STANDARD = "standard"
    MULTIDIRECTIONAL = "multi"
    COMBINED = "combined"
    IGOR = "igor"


def generate_grayscale(
    input_dem: Path,
    output_path: Path,
    exaggeration: float,
    mode: ShadingMode = ShadingMode.MULTIDIRECTIONAL,
    azimuth: float = 315.0,
    altitude: float = 45.0,
    progress_cb=None,
) -> Path:
    """Generate a single grayscale hillshade using gdaldem.

    Args:
        input_dem: Input DEM GeoTIFF (EPSG:4326)
        output_path: Output grayscale hillshade GeoTIFF
        exaggeration: Vertical exaggeration factor (z-factor)
        mode: Shading algorithm
        azimuth: Sun azimuth in degrees (for standard mode)
        altitude: Sun altitude in degrees (for standard mode)

    Returns:
        Path to output file
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if progress_cb:
        progress_cb(f"Generating {mode.value} hillshade (z={exaggeration}x)...")

    cmd = [
        "gdaldem", "hillshade",
        str(input_dem),
        str(output_path),
        "-z", str(exaggeration),
        "-compute_edges",
        "-co", "COMPRESS=LZW",
        "-co", "TILED=YES",
        "-co", "BIGTIFF=YES",
    ]

    if mode == ShadingMode.MULTIDIRECTIONAL:
        cmd.append("-multidirectional")
    elif mode == ShadingMode.COMBINED:
        cmd.append("-combined")
    elif mode == ShadingMode.IGOR:
        cmd.append("-igor")
    else:
        # STANDARD: explicit azimuth/altitude
        cmd += ["-az", str(azimuth), "-alt", str(altitude)]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gdaldem hillshade ({mode.value}) failed: {result.stderr}")

    if progress_cb:
        size_mb = output_path.stat().st_size / (1024 * 1024)
        progress_cb(f"  {mode.value}: {output_path.name} ({size_mb:.1f} MB)")

    return output_path


def generate_composite(
    input_dem: Path,
    output_path: Path,
    exaggeration: float,
    weights: Tuple[float, float, float] = (0.6, 0.3, 0.1),
    azimuth: float = 315.0,
    altitude: float = 45.0,
    cache_dir: Optional[Path] = None,
    progress_cb=None,
) -> Path:
    """Generate composite hillshade by blending multi + igor + combined.

    Each sub-layer is generated (or loaded from cache) independently,
    then blended with the given weights using numpy.

    Args:
        input_dem: Input DEM GeoTIFF (EPSG:4326)
        output_path: Output blended grayscale hillshade GeoTIFF
        exaggeration: Vertical exaggeration factor
        weights: (multidirectional, igor, combined) blend weights
        cache_dir: Directory for caching individual sub-layers
        progress_cb: Progress callback

    Returns:
        Path to output file
    """
    import rasterio

    output_path.parent.mkdir(parents=True, exist_ok=True)
    w_multi, w_igor, w_combined = weights

    layers: List[Tuple[ShadingMode, float]] = [
        (ShadingMode.MULTIDIRECTIONAL, w_multi),
        (ShadingMode.IGOR, w_igor),
        (ShadingMode.COMBINED, w_combined),
    ]

    if progress_cb:
        progress_cb(f"Composite hillshade: multi={w_multi}, igor={w_igor}, combined={w_combined}")

    layer_paths: List[Tuple[Path, float]] = []
    for mode, weight in layers:
        if weight <= 0:
            continue

        # Determine cache path for this sub-layer
        if cache_dir:
            sub_path = cache_dir / f"{input_dem.stem}_gray_{mode.value}_{exaggeration}x.tif"
        else:
            sub_path = output_path.parent / f"_tmp_gray_{mode.value}.tif"

        if sub_path.exists():
            if progress_cb:
                progress_cb(f"  {mode.value}: cached")
        else:
            generate_grayscale(
                input_dem, sub_path, exaggeration,
                mode=mode, azimuth=azimuth, altitude=altitude,
                progress_cb=progress_cb,
            )

        layer_paths.append((sub_path, weight))

    # Blend using chunked windowed reads to avoid loading full arrays into RAM
    if progress_cb:
        progress_cb("Blending sub-layers (chunked)...")

    CHUNK_ROWS = 512  # process 512 rows at a time (~200 MB peak for 80k-wide raster)

    with rasterio.open(layer_paths[0][0]) as src:
        profile = src.profile.copy()
        profile.update(dtype="uint8", compress="lzw", tiled=True,
                       blockxsize=512, blockysize=512)
        height, width = src.shape

        with rasterio.open(output_path, "w", **profile) as dst:
            for row_off in range(0, height, CHUNK_ROWS):
                chunk_h = min(CHUNK_ROWS, height - row_off)
                window = rasterio.windows.Window(0, row_off, width, chunk_h)
                blended = np.zeros((chunk_h, width), dtype=np.float64)

                for path, weight in layer_paths:
                    with rasterio.open(path) as layer_src:
                        data = layer_src.read(1, window=window).astype(np.float64)
                        blended += data * weight

                chunk_out = np.clip(blended, 0, 255).astype(np.uint8)
                dst.write(chunk_out, 1, window=window)

        del blended  # explicit cleanup

    if progress_cb:
        size_mb = output_path.stat().st_size / (1024 * 1024)
        progress_cb(f"Composite: {output_path.name} ({size_mb:.1f} MB)")

    # Clean up non-cached temp files
    if not cache_dir:
        for path, _ in layer_paths:
            if path.name.startswith("_tmp_") and path.exists():
                path.unlink()

    return output_path
