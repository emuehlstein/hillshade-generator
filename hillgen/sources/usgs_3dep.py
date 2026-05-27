"""USGS 3DEP 1/3 arc-second (~10m) DEM source.

Downloads 1°×1° tiles from the USGS 3D Elevation Program via the
National Map S3 bucket. Tiles are publicly accessible — no auth needed.

URL pattern (current, latest version):
    https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/current/{tile}/{tile_filename}.tif

Tile naming: n{lat}w{lon} where lat/lon are the NW corner of the 1° cell
(ceiling of north, ceiling of abs(west)).
"""

import math
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

import click
import requests

from .base import BBox

# Base URL for 3DEP 1/3 arc-second tiles (current/latest)
_S3_BASE = "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/current"


class USGS3DEP10m:
    """USGS 3DEP 1/3 arc-second (~10m resolution), CONUS + HI/AK."""

    name = "usgs-3dep-10m"
    description = "USGS 3DEP 1/3 arc-second (~10m), covers CONUS, Alaska, Hawaii"
    resolution_m = 10.0
    priority = 80

    # 3DEP is US-only. Use a small set of rectangles that hug the southern
    # border so the resolver doesn't claim Mexico / Caribbean / Canada
    # coverage just because the bbox sits in a loose -180..-60 / 15..72 box
    # (the 1° tiles for those regions don't exist on prd-tnm and would 404
    # mid-download).
    _coverage_rects = (
        # CONUS, split by southern border:
        #   Pacific (CA south edge ~32.53°N)
        (-125.5, 32.55, -114.5, 49.5),
        #   Mountain (AZ/NM south edge ~31.33°N)
        (-114.7, 31.33, -103.0, 49.5),
        #   Plains/East/TX-tip (TX south edge ~25.84°N)
        (-106.7, 25.84, -66.5, 49.5),
        # Alaska
        (-180.0, 51.0, -129.0, 72.0),
        # Hawaii
        (-161.0, 18.5, -154.5, 22.5),
    )

    def covers(self, bbox: BBox) -> bool:
        """True if bbox is fully inside one of the 3DEP coverage rectangles."""
        for w, s, e, n in self._coverage_rects:
            if bbox.west >= w and bbox.east <= e and bbox.south >= s and bbox.north <= n:
                return True
        return False

    def download(self, bbox: BBox, output_dir: Path, progress_cb=None) -> Path:
        """Download 3DEP tiles covering bbox, merge if needed, clip to bbox.

        Returns path to the final clipped GeoTIFF.
        """
        output_dir.mkdir(parents=True, exist_ok=True)

        # Determine which 1° tiles we need
        tiles = self._tiles_for_bbox(bbox)
        if progress_cb:
            progress_cb(f"Need {len(tiles)} tile(s): {', '.join(t[0] for t in tiles)}")

        # Download each tile
        downloaded = []
        for tile_name, url in tiles:
            tile_path = output_dir / f"{tile_name}.tif"

            if tile_path.exists():
                if progress_cb:
                    progress_cb(f"  {tile_name}: cached")
                downloaded.append(tile_path)
                continue

            if progress_cb:
                progress_cb(f"  {tile_name}: downloading...")

            self._download_tile(url, tile_path)
            downloaded.append(tile_path)

            if progress_cb:
                size_mb = tile_path.stat().st_size / (1024 * 1024)
                progress_cb(f"  {tile_name}: {size_mb:.0f} MB")

        # Merge if multiple tiles, then clip to bbox
        if len(downloaded) == 1:
            merged = downloaded[0]
        else:
            if progress_cb:
                progress_cb(f"Merging {len(downloaded)} tiles...")
            merged = self._merge_tiles(downloaded, output_dir)

        # Clip to exact bbox
        clipped = output_dir / self._output_filename(bbox)
        if clipped.exists():
            clipped.unlink()

        if progress_cb:
            progress_cb("Clipping to bbox...")

        self._clip_to_bbox(merged, clipped, bbox)

        # Clean up merge temp if we created one
        for f in output_dir.glob("_merged_*.tif"):
            if f != clipped:
                f.unlink()

        if progress_cb:
            size_mb = clipped.stat().st_size / (1024 * 1024)
            progress_cb(f"Done: {clipped.name} ({size_mb:.1f} MB)")

        return clipped

    def _tiles_for_bbox(self, bbox: BBox) -> List[Tuple[str, str]]:
        """Return list of (tile_name, download_url) for all 1° tiles covering bbox."""
        tiles = []

        # 3DEP tiles are named by their NW corner: n{ceil(lat)}w{ceil(abs(lon))}
        lat_min = math.floor(bbox.south)
        lat_max = math.floor(bbox.north)
        lon_min = math.floor(bbox.west)
        lon_max = math.floor(bbox.east)

        for lat in range(lat_min, lat_max + 1):
            for lon in range(lon_min, lon_max + 1):
                # Tile name uses NW corner convention
                tile_lat = lat + 1  # north edge of the 1° cell
                tile_lon = abs(lon)  # west edge, expressed as positive

                # Direction prefixes
                ns = "n" if tile_lat >= 0 else "s"
                ew = "w" if lon < 0 else "e"

                tile_name = f"{ns}{abs(tile_lat):02d}{ew}{tile_lon:03d}"
                filename = f"USGS_13_{tile_name}"
                url = f"{_S3_BASE}/{tile_name}/{filename}.tif"
                tiles.append((tile_name, url))

        return tiles

    def _download_tile(self, url: str, output: Path):
        """Download a single tile with streaming."""
        output.parent.mkdir(parents=True, exist_ok=True)
        tmp = output.with_suffix(".tmp")

        try:
            with requests.get(url, stream=True, timeout=30) as r:
                r.raise_for_status()
                with open(tmp, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                        f.write(chunk)
            tmp.rename(output)
        except Exception:
            if tmp.exists():
                tmp.unlink()
            raise

    def _merge_tiles(self, tiles: List[Path], output_dir: Path) -> Path:
        """Merge multiple GeoTIFF tiles into one."""
        import hashlib
        key = hashlib.md5("|".join(str(t) for t in sorted(tiles)).encode()).hexdigest()[:8]
        merged = output_dir / f"_merged_{key}.tif"
        cmd = [
            "gdal_merge.py", "-o", str(merged),
            "-co", "COMPRESS=DEFLATE",
            "-co", "BIGTIFF=IF_SAFER",
        ] + [str(t) for t in tiles]

        subprocess.run(cmd, check=True, capture_output=True, text=True)
        return merged

    def _clip_to_bbox(self, input_path: Path, output_path: Path, bbox: BBox):
        """Clip a raster to exact bounding box."""
        cmd = [
            "gdalwarp",
            "-te", str(bbox.west), str(bbox.south), str(bbox.east), str(bbox.north),
            "-co", "COMPRESS=DEFLATE",
            "-co", "TILED=YES",
            "-co", "BIGTIFF=IF_SAFER",
            str(input_path),
            str(output_path),
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)

    def _output_filename(self, bbox: BBox) -> str:
        """Generate a deterministic output filename from the bbox."""
        # Use bbox center for naming
        lat = (bbox.south + bbox.north) / 2
        lon = (bbox.west + bbox.east) / 2
        ns = "n" if lat >= 0 else "s"
        ew = "w" if lon < 0 else "e"
        return f"3dep_{ns}{abs(lat):.2f}_{ew}{abs(lon):.2f}.tif"
