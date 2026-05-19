"""Tile generation — styled raster → XYZ PNG tile directory."""

import shutil
import subprocess
from pathlib import Path
from typing import Tuple


def find_tool(name: str) -> str:
    """Find a CLI tool on PATH."""
    path = shutil.which(name)
    if not path:
        raise FileNotFoundError(
            f"{name} not found. Install it:\n"
            f"  gdal2tiles.py: comes with GDAL (brew install gdal)\n"
            f"  mb-util: pip install mbutil\n"
            f"  pmtiles: go install github.com/protomaps/go-pmtiles/cmd/pmtiles@latest"
        )
    return path


def parse_zoom(zoom_str: str) -> Tuple[int, int]:
    """Parse '10-16' into (10, 16)."""
    parts = zoom_str.split("-")
    if len(parts) == 2:
        return int(parts[0]), int(parts[1])
    elif len(parts) == 1:
        z = int(parts[0])
        return z, z
    raise ValueError(f"Invalid zoom range: {zoom_str}")


def generate_tiles(
    input_raster: Path,
    output_dir: Path,
    zoom: str = "10-16",
    progress_cb=None,
) -> Path:
    """Generate XYZ tile directory from a styled raster using gdal2tiles.

    NOTE: We use --xyz (not --tms) to avoid the coordinate confusion
    that plagued ilhmp. XYZ is what web viewers expect natively.

    Args:
        input_raster: Input styled RGBA GeoTIFF
        output_dir: Output directory for {z}/{x}/{y}.png tiles
        zoom: Zoom range string (e.g. '10-16')
        progress_cb: Progress callback

    Returns:
        Path to output directory
    """
    min_zoom, max_zoom = parse_zoom(zoom)

    if progress_cb:
        progress_cb(f"Generating tiles z{min_zoom}-{max_zoom}...")

    gdal2tiles = find_tool("gdal2tiles.py")

    # Clean output dir if exists
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        gdal2tiles,
        "-z", f"{min_zoom}-{max_zoom}",
        "-w", "none",
        "--xyz",              # XYZ coordinates, NOT TMS
        "--processes=4",
        str(input_raster),
        str(output_dir),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gdal2tiles failed: {result.stderr}")

    # Count tiles
    tile_count = sum(1 for _ in output_dir.rglob("*.png"))

    if progress_cb:
        progress_cb(f"Generated {tile_count:,} tiles in {output_dir}")

    return output_dir
