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
    layer_type: str = "overlay",
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
        layer_type: 'baselayer' for opaque hypsometric/elevation-coloured output,
            'overlay' for a hillshade meant to be drawn over another basemap.
            ATAK honours this; getting it wrong makes a basemap unusable.
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

    # Inject metadata.
    #
    # Deliberately NO 'scheme' key. MBTiles is TMS row order by definition, and
    # mb-util has already flipped our XYZ input directory to TMS on the way in.
    # This previously wrote scheme=xyz, which was false: verified on a canary
    # build where z10 col=159 stored rows 653/654 (TMS); XYZ would be 369/370.
    # A reader that trusts scheme=xyz re-flips and renders the map upside down.
    # ATAK guidance is to omit the key entirely.
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
    _set_metadata(output_path, "type", layer_type)
    _set_metadata(output_path, "version", "1.1")

    # Blank-output guard. A tileset built over ground the DEM source has no
    # data for still produces a structurally valid MBTiles full of uniform
    # tiles, and sqlite quick_check passes it happily. Record the shape of
    # the tile payloads so a blank build is visible both at run time and
    # afterwards when auditing a batch.
    stats = audit_tile_content(output_path)
    _set_metadata(output_path, "hillgen:tile_count", str(stats["tile_count"]))
    _set_metadata(
        output_path, "hillgen:mean_tile_bytes", f"{stats['mean_tile_bytes']:.1f}"
    )
    _set_metadata(
        output_path, "hillgen:distinct_ratio", f"{stats['distinct_ratio']:.4f}"
    )
    if stats["suspect"] and progress_cb:
        progress_cb(
            f"WARNING: {output_path.name} looks blank — "
            f"{stats['tile_count']} tiles, mean {stats['mean_tile_bytes']:.0f} B/tile, "
            f"{stats['distinct_tiles']} distinct payloads "
            f"({stats['distinct_ratio']:.1%}). The DEM source probably has no "
            f"coverage over this bbox."
        )

    if progress_cb:
        size_mb = output_path.stat().st_size / (1024 * 1024)
        progress_cb(f"MBTiles: {output_path.name} ({size_mb:.1f} MB)")

    return output_path


# A 256px PNG hillshade tile with real terrain in it runs to several KB.
# Uniform fill compresses to a few hundred bytes.
_BLANK_MEAN_BYTES = 1500.0
# Blank builds repeat the same handful of payloads across the whole pyramid.
_BLANK_DISTINCT_RATIO = 0.02


def audit_tile_content(mbtiles_path: Path) -> dict:
    """Summarise tile payloads so blank/no-coverage builds are detectable.

    Returns tile_count, mean_tile_bytes, distinct_tiles, distinct_ratio and a
    "suspect" flag. Suspect means the tiles are both small on average and
    highly repetitive, which is the signature of a tileset rendered over
    ground the DEM source does not actually cover.
    """
    conn = sqlite3.connect(mbtiles_path)
    try:
        row = conn.execute(
            "SELECT COUNT(*), COALESCE(AVG(LENGTH(tile_data)), 0), "
            "COUNT(DISTINCT tile_data) FROM tiles"
        ).fetchone()
    finally:
        conn.close()
    tile_count, mean_bytes, distinct = int(row[0]), float(row[1]), int(row[2])
    ratio = (distinct / tile_count) if tile_count else 0.0
    return {
        "tile_count": tile_count,
        "mean_tile_bytes": mean_bytes,
        "distinct_tiles": distinct,
        "distinct_ratio": ratio,
        "suspect": bool(
            tile_count
            and mean_bytes < _BLANK_MEAN_BYTES
            and ratio < _BLANK_DISTINCT_RATIO
        ),
    }


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
