"""Reproject DEM to EPSG:4326 for web mapping."""

import json
import subprocess
from pathlib import Path


def native_resolution_deg(input_path: Path) -> tuple[float, float] | None:
    """Return (xres, yres) in degrees by reading the input raster's geotransform.

    Returns None if gdalinfo fails or the raster is already geographic.
    We convert projected linear units (metres) to approximate degrees so
    gdalwarp -tr can lock the output at the same ground sample distance.
    """
    result = subprocess.run(
        ["gdalinfo", "-json", "-nomd", "-norat", "-noct", str(input_path)],
        capture_output=True, text=True, timeout=120,
    )
    if result.returncode != 0:
        return None
    try:
        info = json.loads(result.stdout)
        gt = info.get("geoTransform")  # [x0, xres, 0, y0, 0, -yres]
        if not gt:
            return None
        xres_native = abs(gt[1])
        yres_native = abs(gt[5])
        # If already in degrees (geographic CRS), use as-is
        srs = info.get("coordinateSystem", {}).get("wkt", "")
        if "GEOGCS" in srs or "GEOGRAPHICCRS" in srs:
            return xres_native, yres_native
        # Projected CRS — pixel size is in metres.
        # Approximate conversion: 1° ≈ 111,320 m (good enough for -tr).
        return xres_native / 111_320, yres_native / 111_320
    except Exception:
        return None


def reproject_to_4326(input_path: Path, output_path: Path, progress_cb=None) -> Path:
    """Reproject a DEM to EPSG:4326 at native resolution using bilinear resampling.

    Uses gdalinfo to detect the source pixel size and passes -tr to gdalwarp so
    the output is never coarsened (or inflated) beyond the native GSD.

    Args:
        input_path: Input GeoTIFF (any CRS)
        output_path: Output GeoTIFF in EPSG:4326

    Returns:
        Path to output file
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)

    res = native_resolution_deg(input_path)
    if res:
        xres, yres = res
        if progress_cb:
            progress_cb(f"Reprojecting to EPSG:4326 at native res ({xres:.6f}° × {yres:.6f}°)...")
    else:
        if progress_cb:
            progress_cb("Reprojecting to EPSG:4326 (native res detection failed, letting gdalwarp decide)...")

    cmd = [
        "gdalwarp",
        "-t_srs", "EPSG:4326",
        "-r", "bilinear",
        "-co", "COMPRESS=DEFLATE",
        "-co", "TILED=YES",
        "-co", "BIGTIFF=IF_SAFER",
    ]
    if res:
        cmd += ["-tr", str(xres), str(yres)]
    cmd += [
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
