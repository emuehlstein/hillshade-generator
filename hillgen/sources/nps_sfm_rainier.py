"""NPS SfM 2021 Mount Rainier DEM source.

Structure-from-Motion (SfM) survey of Mount Rainier National Park,
collected Sept 2021 by the National Park Service.

Resolution: 0.67 m raster (DSM — surface model, not bare earth)
Coverage:   Full MORA park boundary, ~1,215 km²
Bounds:     W: -121.97°  E: -121.43°  S: 46.69°  N: 47.02°
CRS:        NAD83 / UTM Zone 10N (EPSG:26910), NAVD88 vertical
License:    CC BY 4.0
DOI:        https://doi.org/10.5069/G92Z13Q7

Data lives on the OpenTopography S3 mirror (no auth required):
  s3://raster/WA21_Rainier/WA21_Rainier_hh/
Two GeoTIFFs — west half (Sept 21) and east+north half (Sept 23–24).
A VRT at WA21_Rainier_hh.vrt stitches them together.

Note: This is a DSM (surface model), not bare earth. Snow/ice, trees,
and structures are included. For lower-flank terrain analysis a bare-earth
LiDAR source would be more accurate; for high-mountain hillshades the
difference is minimal.
"""

import math
import subprocess
from pathlib import Path
from typing import List

from .base import BBox

# OpenTopography S3 endpoint (anonymous access)
_OT_S3_ENDPOINT = "https://opentopography.s3.sdsc.edu"
_OT_S3_BUCKET = "raster"
_DATASET = "WA21_Rainier"
_VRT_KEY = f"{_DATASET}/{_DATASET}_hh.vrt"

# Park boundary (from dataset metadata)
_BOUNDS_WEST = -121.97121022749072
_BOUNDS_EAST = -121.4321388709856
_BOUNDS_SOUTH = 46.68572778666915
_BOUNDS_NORTH = 47.0235172275356


class NPSSfMRainier2021:
    """NPS SfM 2021 Mount Rainier — 0.67m DSM, full park boundary."""

    name = "nps-sfm-rainier-2021"
    description = "NPS SfM 2021 Mt. Rainier (0.67m DSM), covers MORA park boundary"
    resolution_m = 0.67
    priority = 95  # Higher than 3DEP (80) — finer resolution wins within coverage

    def covers(self, bbox: BBox) -> bool:
        """True if bbox is entirely within the MORA park boundary."""
        return (
            bbox.west >= _BOUNDS_WEST
            and bbox.east <= _BOUNDS_EAST
            and bbox.south >= _BOUNDS_SOUTH
            and bbox.north <= _BOUNDS_NORTH
        )

    def covers_partial(self, bbox: BBox) -> bool:
        """True if bbox overlaps the MORA park boundary at all."""
        return not (
            bbox.east < _BOUNDS_WEST
            or bbox.west > _BOUNDS_EAST
            or bbox.north < _BOUNDS_SOUTH
            or bbox.south > _BOUNDS_NORTH
        )

    # Max degrees per quadrant side before splitting (~1° ≈ 80–110 km at this lat).
    # 0.2° ≈ ~15-18 km — keeps each gdalwarp pass under ~1 GB working memory.
    _TILE_SIZE_DEG = 0.2

    def download(self, bbox: BBox, output_dir: Path, progress_cb=None) -> Path:
        """Download + clip the NPS SfM DEM for the given bbox.

        Large bboxes are automatically split into tiles (≤0.2° per side) to
        avoid macOS killing gdalwarp under memory pressure at 0.67m resolution.
        Each tile is fetched separately via /vsicurl/ and the results are
        merged with gdal_merge.py into a single output GeoTIFF.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / self._output_filename(bbox)

        if output_path.exists():
            if progress_cb:
                size_mb = output_path.stat().st_size / (1024 * 1024)
                progress_cb(f"Cached: {output_path.name} ({size_mb:.1f} MB)")
            return output_path

        vrt_url = f"{_OT_S3_ENDPOINT}/{_OT_S3_BUCKET}/{_VRT_KEY}"
        vsicurl_vrt = f"/vsicurl/{vrt_url}"

        tiles = self._split_bbox(bbox)
        if progress_cb:
            progress_cb(
                f"Downloading NPS SfM Rainier DEM — "
                f"{len(tiles)} tile(s), bbox {bbox}"
            )
            progress_cb(f"  Source VRT: {vrt_url}")

        tile_paths: List[Path] = []
        for i, tile_bbox in enumerate(tiles):
            tile_path = output_dir / f"_tile_{i}_{self._output_filename(tile_bbox)}"
            if tile_path.exists():
                if progress_cb:
                    progress_cb(f"  Tile {i+1}/{len(tiles)}: cached")
                tile_paths.append(tile_path)
                continue

            tmp = tile_path.with_suffix(".tmp.tif")
            try:
                if progress_cb:
                    progress_cb(
                        f"  Tile {i+1}/{len(tiles)}: "
                        f"({tile_bbox.west:.3f},{tile_bbox.south:.3f}) → "
                        f"({tile_bbox.east:.3f},{tile_bbox.north:.3f})"
                    )
                cmd = [
                    "gdalwarp",
                    "-t_srs", "EPSG:4326",
                    "-te",
                    str(tile_bbox.west), str(tile_bbox.south),
                    str(tile_bbox.east), str(tile_bbox.north),
                    "-r", "bilinear",
                    "-co", "COMPRESS=DEFLATE",
                    "-co", "TILED=YES",
                    "-co", "BIGTIFF=IF_SAFER",
                    vsicurl_vrt,
                    str(tmp),
                ]
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode != 0:
                    raise RuntimeError(
                        f"gdalwarp failed for tile {i+1}:\n{result.stderr}"
                    )
                tmp.rename(tile_path)
                if progress_cb:
                    size_mb = tile_path.stat().st_size / (1024 * 1024)
                    progress_cb(f"    → {size_mb:.0f} MB")
                tile_paths.append(tile_path)
            except Exception:
                if tmp.exists():
                    tmp.unlink()
                raise

        # Merge all tiles into final output
        if len(tile_paths) == 1:
            tile_paths[0].rename(output_path)
        else:
            if progress_cb:
                progress_cb(f"Merging {len(tile_paths)} tiles...")
            cmd = [
                "gdal_merge.py",
                "-o", str(output_path),
                "-co", "COMPRESS=DEFLATE",
                "-co", "TILED=YES",
                "-co", "BIGTIFF=IF_SAFER",
            ] + [str(p) for p in tile_paths]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(f"gdal_merge failed:\n{result.stderr}")
            # Clean up individual tiles
            for p in tile_paths:
                p.unlink(missing_ok=True)

        if progress_cb:
            size_mb = output_path.stat().st_size / (1024 * 1024)
            progress_cb(f"Done: {output_path.name} ({size_mb:.1f} MB)")

        return output_path

    def _split_bbox(self, bbox: BBox) -> List[BBox]:
        """Split bbox into tiles no larger than _TILE_SIZE_DEG per side."""
        width = bbox.east - bbox.west
        height = bbox.north - bbox.south
        cols = max(1, math.ceil(width / self._TILE_SIZE_DEG))
        rows = max(1, math.ceil(height / self._TILE_SIZE_DEG))

        if cols == 1 and rows == 1:
            return [bbox]

        col_w = width / cols
        row_h = height / rows
        tiles = []
        for r in range(rows):
            for c in range(cols):
                tiles.append(BBox(
                    west=bbox.west + c * col_w,
                    east=bbox.west + (c + 1) * col_w,
                    south=bbox.south + r * row_h,
                    north=bbox.south + (r + 1) * row_h,
                ))
        return tiles

    def _output_filename(self, bbox: BBox) -> str:
        lat = (bbox.south + bbox.north) / 2
        lon = (bbox.west + bbox.east) / 2
        ns = "n" if lat >= 0 else "s"
        ew = "w" if lon < 0 else "e"
        return f"nps_sfm_rainier_{ns}{abs(lat):.2f}_{ew}{abs(lon):.2f}.tif"
