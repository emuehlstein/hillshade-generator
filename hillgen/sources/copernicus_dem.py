"""Copernicus DEM GLO-30 (~30m global) source.

Downloads 1°×1° COG tiles from the AWS Open Data `copernicus-dem-30m`
public bucket. Tiles are anonymously readable over HTTPS — no AWS auth
needed.

URL pattern:
    https://copernicus-dem-30m.s3.amazonaws.com/
        Copernicus_DSM_COG_10_{LAT}_00_{LON}_00_DEM/
        Copernicus_DSM_COG_10_{LAT}_00_{LON}_00_DEM.tif

Tile naming convention: each tile is 1°×1° and named by the corner of
the tile that is **closest to the equator/prime meridian** (i.e., the
south edge for N lats, north edge for S lats; west edge for E lons,
east edge for W lons). So the tile covering [-111°, -110°] × [23°, 24°]
is ``N23_W110`` (south edge=N23, east edge=W110).

This is a true global source, but its priority is lower than every
domestic LiDAR/3DEP source so it only wins when nothing else covers the
bbox.
"""

import math
import subprocess
from pathlib import Path
from typing import List, Tuple

import requests

from .base import BBox


_S3_BASE = "https://copernicus-dem-30m.s3.amazonaws.com"


class CopernicusDEM30m:
    """Copernicus DEM GLO-30 (~30m resolution), global coverage."""

    name = "copernicus-dem-30m"
    description = "Copernicus DEM GLO-30 (~30m), global"
    resolution_m = 30.0
    # Below every domestic source; above nothing (last resort).
    priority = 10

    def covers(self, bbox: BBox) -> bool:
        """Global land coverage. The dataset is technically 60°S–85°N but
        for our purposes any sane bbox is in range."""
        return -85.0 <= bbox.south and bbox.north <= 85.0

    def download(self, bbox: BBox, output_dir: Path, progress_cb=None) -> Path:
        output_dir.mkdir(parents=True, exist_ok=True)

        tiles = self._tiles_for_bbox(bbox)
        if progress_cb:
            progress_cb(
                f"Copernicus DEM: {len(tiles)} tile(s): "
                f"{', '.join(t[0] for t in tiles)}"
            )

        downloaded: List[Path] = []
        for tile_name, url in tiles:
            tile_path = output_dir / f"{tile_name}.tif"

            if tile_path.exists():
                if progress_cb:
                    progress_cb(f"  {tile_name}: cached")
                downloaded.append(tile_path)
                continue

            if progress_cb:
                progress_cb(f"  {tile_name}: downloading...")

            try:
                self._download_tile(url, tile_path)
            except requests.HTTPError as e:
                # Ocean tiles legitimately don't exist; skip 404s rather
                # than fail the whole run. We still need at least one tile.
                if e.response is not None and e.response.status_code == 404:
                    if progress_cb:
                        progress_cb(f"  {tile_name}: missing (likely ocean), skipped")
                    continue
                raise

            downloaded.append(tile_path)
            if progress_cb:
                size_mb = tile_path.stat().st_size / (1024 * 1024)
                progress_cb(f"  {tile_name}: {size_mb:.0f} MB")

        if not downloaded:
            raise RuntimeError(
                f"Copernicus DEM: no tiles found for bbox "
                f"({bbox.west:.2f},{bbox.south:.2f},{bbox.east:.2f},{bbox.north:.2f}) "
                f"— area may be entirely over ocean."
            )

        if len(downloaded) == 1:
            merged = downloaded[0]
        else:
            if progress_cb:
                progress_cb(f"Merging {len(downloaded)} tiles...")
            merged = self._merge_tiles(downloaded, output_dir)

        clipped = output_dir / self._output_filename(bbox)
        if clipped.exists():
            clipped.unlink()

        if progress_cb:
            progress_cb("Clipping to bbox...")

        self._clip_to_bbox(merged, clipped, bbox)

        for f in output_dir.glob("_merged_*.tif"):
            if f != clipped:
                f.unlink()

        if progress_cb:
            size_mb = clipped.stat().st_size / (1024 * 1024)
            progress_cb(f"Done: {clipped.name} ({size_mb:.1f} MB)")

        return clipped

    def _tiles_for_bbox(self, bbox: BBox) -> List[Tuple[str, str]]:
        """Return list of (tile_name, url) covering the bbox.

        Each 1°×1° tile is labelled by the corner closest to (0,0):
            - latitude: floor(lat_south) for positive lats; -ceil(lat_south)
              for negative lats — but since each integer band [k, k+1] gets
              a single label, the rule simplifies to:
                * k >= 0  → "N{k:02d}"
                * k <  0  → "S{abs(k+1):02d}"   (because the band [-3,-2]
                  is the "S2" tile — north edge is -2)
            - longitude (symmetric):
                * k >= 0  → "E{k:03d}"
                * k <  0  → "W{abs(k+1):03d}"
        """
        tiles = []

        lat_min = math.floor(bbox.south)
        lat_max = math.floor(bbox.north)
        # Don't request an extra band if north sits exactly on an integer boundary.
        if bbox.north == lat_max and lat_max > bbox.south:
            lat_max -= 1

        lon_min = math.floor(bbox.west)
        lon_max = math.floor(bbox.east)
        if bbox.east == lon_max and lon_max > bbox.west:
            lon_max -= 1

        for lat in range(lat_min, lat_max + 1):
            if lat >= 0:
                lat_label = f"N{lat:02d}"
            else:
                lat_label = f"S{abs(lat + 1):02d}"

            for lon in range(lon_min, lon_max + 1):
                if lon >= 0:
                    lon_label = f"E{lon:03d}"
                else:
                    lon_label = f"W{abs(lon + 1):03d}"

                tile_id = f"Copernicus_DSM_COG_10_{lat_label}_00_{lon_label}_00_DEM"
                url = f"{_S3_BASE}/{tile_id}/{tile_id}.tif"
                # Short name for local caching / progress output
                short = f"cop30_{lat_label}_{lon_label}"
                tiles.append((short, url))

        return tiles

    def _download_tile(self, url: str, output: Path):
        output.parent.mkdir(parents=True, exist_ok=True)
        tmp = output.with_suffix(".tmp")
        try:
            with requests.get(url, stream=True, timeout=60) as r:
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
        lat = (bbox.south + bbox.north) / 2
        lon = (bbox.west + bbox.east) / 2
        ns = "n" if lat >= 0 else "s"
        ew = "w" if lon < 0 else "e"
        return f"cop30_{ns}{abs(lat):.2f}_{ew}{abs(lon):.2f}.tif"
