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

import subprocess
from pathlib import Path

import requests

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

    def download(self, bbox: BBox, output_dir: Path, progress_cb=None) -> Path:
        """Download + clip the NPS SfM DEM for the given bbox.

        Strategy:
        1. Pull the VRT file from OT S3 (tiny, ~2KB).
        2. Use gdal_translate with the OT S3 endpoint as a VSICURL source
           to clip the bbox directly — avoids downloading all 18GB.
        3. Return the clipped GeoTIFF.

        The VRT references both source TIFFs with relative paths, so we
        build a /vsicurl/ path to the VRT instead of materialising it locally.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / self._output_filename(bbox)

        if output_path.exists():
            if progress_cb:
                size_mb = output_path.stat().st_size / (1024 * 1024)
                progress_cb(f"Cached: {output_path.name} ({size_mb:.1f} MB)")
            return output_path

        # Build GDAL virtual path to the VRT on OT S3
        vrt_url = f"{_OT_S3_ENDPOINT}/{_OT_S3_BUCKET}/{_VRT_KEY}"
        vsicurl_vrt = f"/vsicurl/{vrt_url}"

        if progress_cb:
            progress_cb(f"Downloading NPS SfM Rainier DEM (clipping to bbox)...")
            progress_cb(f"  Source VRT: {vrt_url}")

        cmd = [
            "gdal_translate",
            "-projwin",
            # projwin is ulx uly lrx lry (xmin ymax xmax ymin) in source CRS
            # Source is UTM 10N so we need to convert bbox from 4326 first.
            # We use gdalwarp instead (supports -t_srs + -te in target CRS).
        ]

        # gdalwarp handles the CRS conversion + clip in one step
        tmp_path = output_path.with_suffix(".tmp.tif")
        try:
            cmd = [
                "gdalwarp",
                "-t_srs", "EPSG:4326",
                "-te",
                str(bbox.west), str(bbox.south),
                str(bbox.east), str(bbox.north),
                "-r", "bilinear",
                "-co", "COMPRESS=DEFLATE",
                "-co", "TILED=YES",
                "-co", "BIGTIFF=IF_SAFER",
                vsicurl_vrt,
                str(tmp_path),
            ]

            if progress_cb:
                progress_cb("  Running gdalwarp (this may take a few minutes for large areas)...")

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(
                    f"gdalwarp failed for NPS SfM source:\n{result.stderr}"
                )

            tmp_path.rename(output_path)

        except Exception:
            if tmp_path.exists():
                tmp_path.unlink()
            raise

        if progress_cb:
            size_mb = output_path.stat().st_size / (1024 * 1024)
            progress_cb(f"Done: {output_path.name} ({size_mb:.1f} MB)")

        return output_path

    def _output_filename(self, bbox: BBox) -> str:
        lat = (bbox.south + bbox.north) / 2
        lon = (bbox.west + bbox.east) / 2
        ns = "n" if lat >= 0 else "s"
        ew = "w" if lon < 0 else "e"
        return f"nps_sfm_rainier_{ns}{abs(lat):.2f}_{ew}{abs(lon):.2f}.tif"
