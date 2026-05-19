"""Reproject DEM to EPSG:4326 for web mapping."""

import subprocess
from pathlib import Path


def reproject_to_4326(input_path: Path, output_path: Path, progress_cb=None) -> Path:
    """Reproject a DEM to EPSG:4326 using bilinear resampling.

    Args:
        input_path: Input GeoTIFF (any CRS)
        output_path: Output GeoTIFF in EPSG:4326

    Returns:
        Path to output file
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if progress_cb:
        progress_cb("Reprojecting to EPSG:4326...")

    cmd = [
        "gdalwarp",
        "-t_srs", "EPSG:4326",
        "-r", "bilinear",
        "-co", "COMPRESS=DEFLATE",
        "-co", "TILED=YES",
        "-co", "BIGTIFF=IF_SAFER",
        str(input_path),
        str(output_path),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gdalwarp failed: {result.stderr}")

    if progress_cb:
        size_mb = output_path.stat().st_size / (1024 * 1024)
        progress_cb(f"Reprojected: {output_path.name} ({size_mb:.1f} MB)")

    return output_path


def needs_reproject(input_path: Path) -> bool:
    """Check if a raster needs reprojection to EPSG:4326."""
    result = subprocess.run(
        ["gdalsrsinfo", "-o", "epsg", str(input_path)],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        return True  # assume needs reproject if we can't check
    return "4326" not in result.stdout
