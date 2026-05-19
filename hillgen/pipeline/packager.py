"""Package tiles into MBTiles and PMTiles."""

import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Optional

import rasterio


def package_mbtiles(
    tiles_dir: Path,
    output_path: Path,
    name: str = "",
    description: str = "",
    bounds: Optional[str] = None,
    center: Optional[str] = None,
    min_zoom: Optional[int] = None,
    max_zoom: Optional[int] = None,
    progress_cb=None,
) -> Path:
    """Pack XYZ tile directory into MBTiles.

    Uses mb-util with --scheme=xyz to match our gdal2tiles --xyz output.
    Then injects metadata (bounds, center, name, etc.).

    Args:
        tiles_dir: Input directory with {z}/{x}/{y}.png structure
        output_path: Output .mbtiles file
        name: Layer name for metadata
        description: Layer description
        bounds: 'west,south,east,north' string
        center: 'lon,lat,zoom' string
        min_zoom: Minimum zoom level
        max_zoom: Maximum zoom level
        progress_cb: Progress callback

    Returns:
        Path to output file
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.exists():
        output_path.unlink()

    if progress_cb:
        progress_cb("Packaging MBTiles...")

    mb_util = _find_tool("mb-util")
    cmd = [
        mb_util,
        "--scheme=xyz",
        str(tiles_dir),
        str(output_path),
        "--image_format=png",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"mb-util failed: {result.stderr}")

    # Inject metadata
    _set_metadata(output_path, "scheme", "xyz")
    if name:
        _set_metadata(output_path, "name", name)
    if description:
        _set_metadata(output_path, "description", description)
    if bounds:
        _set_metadata(output_path, "bounds", bounds)
    if center:
        _set_metadata(output_path, "center", center)
    if min_zoom is not None:
        _set_metadata(output_path, "minzoom", str(min_zoom))
    if max_zoom is not None:
        _set_metadata(output_path, "maxzoom", str(max_zoom))
    _set_metadata(output_path, "format", "png")
    _set_metadata(output_path, "type", "overlay")

    if progress_cb:
        size_mb = output_path.stat().st_size / (1024 * 1024)
        progress_cb(f"MBTiles: {output_path.name} ({size_mb:.1f} MB)")

    return output_path


def package_pmtiles(
    input_mbtiles: Path,
    output_path: Path,
    progress_cb=None,
) -> Path:
    """Convert MBTiles to PMTiles format.

    Requires the `pmtiles` CLI tool.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if output_path.exists():
        output_path.unlink()

    if progress_cb:
        progress_cb("Converting to PMTiles...")

    pmtiles = _find_tool("pmtiles")
    cmd = [pmtiles, "convert", str(input_mbtiles), str(output_path)]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"pmtiles convert failed: {result.stderr}")

    if progress_cb:
        size_mb = output_path.stat().st_size / (1024 * 1024)
        progress_cb(f"PMTiles: {output_path.name} ({size_mb:.1f} MB)")

    return output_path


def metadata_from_raster(raster_path: Path) -> dict:
    """Extract bounds/center metadata from a styled raster."""
    with rasterio.open(raster_path) as src:
        b = src.bounds
        west, south, east, north = b.left, b.bottom, b.right, b.top
        center_lon = (west + east) / 2
        center_lat = (south + north) / 2
        return {
            "bounds": f"{west},{south},{east},{north}",
            "center": f"{center_lon},{center_lat}",
        }


def _find_tool(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise FileNotFoundError(f"{name} not found on PATH")
    return path


def _set_metadata(mbtiles_path: Path, key: str, value: str):
    conn = sqlite3.connect(str(mbtiles_path))
    conn.execute(
        "INSERT OR REPLACE INTO metadata (name, value) VALUES (?, ?)",
        (key, value),
    )
    conn.commit()
    conn.close()
