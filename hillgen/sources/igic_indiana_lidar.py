"""Indiana IGIC QL2 LiDAR DEM source.

Downloads 2.5ft (0.76m) LiDAR-derived DTM tiles for all 92 Indiana counties
from the Purdue University Digital Forestry Lab's distribution server, hosted
in partnership with IGIC and the Indiana GIO Office.

Data source: https://lidar.digitalforestry.org/
Data credits: USDA NRCS, Indiana State Office + IGIC / USGS 3DEP Program
License: CC0 (public domain)

Coverage: All 92 Indiana counties, 2011-2020 (two vintage epochs per county)
Resolution: 2.5ft (~0.76m), NAD83(HARN) / Indiana West ftUS (EPSG:2968)

URL patterns:
  2017-2020 vintage: /QL2_3DEP_LiDAR_IN_2017_2019_l2/{county}/dtm/{tile}.img
  2011-2013 vintage: /state/{year}/dem/{tile}.img

Tile index GeoJSON per county:
  https://lidar.digitalforestry.org/gis_layers/tilemap_{county}.geojson
  (county name: lower-case, spaces removed, e.g. "laporte", "stjoseph")

Tiles are in state-plane feet (EPSG:2968). This source reprojects to
EPSG:4326 via gdalwarp before returning, matching the interface of
all other hillgen DEMSources.
"""

from __future__ import annotations

import json
import math
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import requests

from .base import BBox

_BASE_URL = "https://lidar.digitalforestry.org"
_TILE_INDEX_URL = f"{_BASE_URL}/gis_layers/tilemap_{{county}}.geojson"
_DTM_NEW_URL = f"{_BASE_URL}/QL2_3DEP_LiDAR_IN_2017_2019_l2/{{county}}/dtm/{{tile}}.img"
_DTM_OLD_URL = f"{_BASE_URL}/state/{{year}}/dem/{{tile}}.img"

# Indiana approximate coverage in WGS84
_IN_WEST = -88.10
_IN_EAST = -84.78
_IN_SOUTH = 37.77
_IN_NORTH = 41.80  # slightly generous to catch edge bboxes along the northern border

# Source CRS for all Indiana LiDAR tiles
_SRC_EPSG = "EPSG:2968"  # NAD83(HARN) / Indiana West (ftUS)

# County name normalization: display name -> URL slug
# Handles multi-word counties, accented chars, etc.
_COUNTY_SLUG_OVERRIDES = {
    "de kalb": "dekalb",
    "la porte": "laporte",
    "st joseph": "stjoseph",
    "st. joseph": "stjoseph",
}


def _county_to_slug(name: str) -> str:
    """Convert county display name to URL slug used by digitalforestry.org."""
    normalized = name.lower().strip()
    if normalized in _COUNTY_SLUG_OVERRIDES:
        return _COUNTY_SLUG_OVERRIDES[normalized]
    # Default: lower-case, spaces removed
    return normalized.replace(" ", "").replace(".", "").replace("-", "")


def _tile_year(tile_name: str) -> int:
    """Extract year from tile name like 'in2018_29902180_12'."""
    return int(tile_name[2:6])


def _tile_url(county_slug: str, tile_name: str) -> str:
    """Build the DTM download URL for a tile."""
    year = _tile_year(tile_name)
    if year >= 2017:
        return _DTM_NEW_URL.format(county=county_slug, tile=tile_name)
    else:
        return _DTM_OLD_URL.format(year=year, tile=tile_name)


def _bbox_intersects(tile_geom: dict, bbox: BBox) -> bool:
    """Check if a tile polygon/multipolygon (GeoJSON geometry) intersects the target bbox."""
    geom_type = tile_geom.get("type", "")
    coords = tile_geom.get("coordinates", [])
    if not coords:
        return False

    # Flatten all rings to a list of (lon, lat) points
    all_pts: list[tuple[float, float]] = []
    if geom_type == "Polygon":
        # coords = [outer_ring, *holes]
        for ring in coords:
            for pt in ring:
                all_pts.append((pt[0], pt[1]))
    elif geom_type == "MultiPolygon":
        # coords = [polygon, ...] where polygon = [outer_ring, *holes]
        for polygon in coords:
            for ring in polygon:
                for pt in ring:
                    all_pts.append((pt[0], pt[1]))
    else:
        # Fallback: try first ring as flat list
        try:
            ring = coords[0]
            for pt in ring:
                all_pts.append((pt[0], pt[1]))
        except (TypeError, IndexError):
            return False

    if not all_pts:
        return False

    lons = [p[0] for p in all_pts]
    lats = [p[1] for p in all_pts]
    t_west, t_east = min(lons), max(lons)
    t_south, t_north = min(lats), max(lats)
    # Overlap check
    return (
        t_east > bbox.west
        and t_west < bbox.east
        and t_north > bbox.south
        and t_south < bbox.north
    )


def _fetch_tile_index(county_slug: str) -> list[dict]:
    """Fetch tile index GeoJSON for a county. Returns list of feature dicts."""
    url = _TILE_INDEX_URL.format(county=county_slug)
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json()
    return data.get("features", [])


def _download_tile(url: str, output: Path, session: requests.Session) -> Path:
    """Download a single .img tile."""
    if output.exists():
        return output
    # Use a worker-unique tmp name to avoid rename races in parallel downloads
    import os
    tmp = output.with_suffix(f".tmp{os.getpid()}")
    try:
        with session.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                    f.write(chunk)
        # Atomic replace: if another worker already finished, just discard ours
        if not output.exists():
            tmp.rename(output)
        else:
            tmp.unlink()
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise
    return output


def _mosaic_tiles(paths: list[Path], output: Path) -> Path:
    """Merge multiple GeoTIFF/HFA tiles into one with gdal_merge.py."""
    cmd = [
        "gdal_merge.py", "-o", str(output),
        "-co", "COMPRESS=DEFLATE",
        "-co", "BIGTIFF=IF_SAFER",
        "-a_nodata", "-9999",
    ] + [str(p) for p in paths]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gdal_merge.py failed: {result.stderr[:500]}")
    return output


def _reproject_and_clip(src: Path, dst: Path, bbox: BBox) -> None:
    """Reproject from EPSG:2968 → EPSG:4326 and clip to WGS84 bbox."""
    cmd = [
        "gdalwarp",
        "-s_srs", _SRC_EPSG,
        "-t_srs", "EPSG:4326",
        "-r", "bilinear",
        "-te", str(bbox.west), str(bbox.south), str(bbox.east), str(bbox.north),
        "-te_srs", "EPSG:4326",
        "-co", "COMPRESS=DEFLATE",
        "-co", "TILED=YES",
        "-co", "BIGTIFF=IF_SAFER",
        "-dstnodata", "-9999",
        str(src), str(dst),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gdalwarp reproject+clip failed: {result.stderr[:500]}")


def _output_filename(bbox: BBox) -> str:
    lat = (bbox.south + bbox.north) / 2
    lon = (bbox.west + bbox.east) / 2
    ns = "n" if lat >= 0 else "s"
    ew = "w" if lon < 0 else "e"
    return f"igic_in_lidar_{ns}{abs(lat):.2f}_{ew}{abs(lon):.2f}.tif"


class IGICIndianaLiDAR:
    """Indiana IGIC QL2 LiDAR 2.5ft DTM, all 92 counties, ~0.76m resolution."""

    name = "igic-indiana-lidar"
    description = (
        "Indiana IGIC/Purdue QL2 LiDAR 2.5ft DTM (~0.76m), "
        "all 92 Indiana counties, 2011-2020 (CC0)"
    )
    resolution_m = 0.76
    priority = 92   # higher than wi-dnr-lidar (90), lower than nps-sfm-rainier (95)

    def covers(self, bbox: BBox) -> bool:
        """Check if bbox is within Indiana's approximate extent."""
        return (
            bbox.west >= _IN_WEST
            and bbox.east <= _IN_EAST
            and bbox.south >= _IN_SOUTH
            and bbox.north <= _IN_NORTH
        )

    def download(self, bbox: BBox, output_dir: Path, progress_cb=None) -> Path:
        """Download LiDAR DTM tiles for bbox, mosaic, reproject, return GeoTIFF."""
        output_dir.mkdir(parents=True, exist_ok=True)

        # Determine which county(ies) intersect the bbox.
        # For now, resolve the primary county from the tile index.
        # Multi-county bboxes: we fetch tile indexes for each county whose
        # approximate extent intersects the bbox, then union the tile sets.
        counties = self._counties_for_bbox(bbox, progress_cb=progress_cb)
        if not counties:
            raise ValueError(
                f"No Indiana counties found for bbox "
                f"({bbox.west:.3f},{bbox.south:.3f},{bbox.east:.3f},{bbox.north:.3f})"
            )

        # Collect all intersecting tiles across counties
        all_tiles: list[tuple[str, str]] = []  # (tile_name, county_slug)
        for county_name, county_slug in counties:
            if progress_cb:
                progress_cb(f"Fetching tile index for {county_name}...")
            try:
                features = _fetch_tile_index(county_slug)
            except Exception as e:
                raise RuntimeError(
                    f"Failed to fetch tile index for {county_name} (slug={county_slug}): {e}"
                ) from e

            for feat in features:
                geom = feat.get("geometry", {})
                props = feat.get("properties", {})
                tile_name = props.get("TILE_NAME", "")
                if tile_name and _bbox_intersects(geom, bbox):
                    all_tiles.append((tile_name, county_slug))

        if not all_tiles:
            raise ValueError(
                f"No LiDAR tiles found intersecting bbox "
                f"({bbox.west:.3f},{bbox.south:.3f},{bbox.east:.3f},{bbox.north:.3f})"
            )

        if progress_cb:
            progress_cb(f"Found {len(all_tiles)} tile(s) to download")

        # Download tiles in parallel (up to 4 workers)
        downloaded: list[Path] = []
        with requests.Session() as session, ThreadPoolExecutor(max_workers=4) as pool:
            futures = {}
            for tile_name, county_slug in all_tiles:
                url = _tile_url(county_slug, tile_name)
                out = output_dir / f"{tile_name}.img"
                futures[pool.submit(_download_tile, url, out, session)] = tile_name

            for fut in as_completed(futures):
                tile_name = futures[fut]
                try:
                    path = fut.result()
                    downloaded.append(path)
                    if progress_cb:
                        mb = path.stat().st_size / (1024 * 1024)
                        progress_cb(f"  {tile_name}: {mb:.1f} MB")
                except Exception as e:
                    raise RuntimeError(
                        f"Failed to download tile {tile_name}: {e}"
                    ) from e

        # Mosaic if multiple tiles
        if len(downloaded) == 1:
            merged = downloaded[0]
        else:
            if progress_cb:
                progress_cb(f"Mosaicking {len(downloaded)} tiles...")
            merged = output_dir / "_igic_mosaic.tif"
            if not merged.exists():
                _mosaic_tiles(downloaded, merged)

        # Reproject EPSG:2968 → EPSG:4326 + clip to bbox
        clipped = output_dir / _output_filename(bbox)
        if clipped.exists():
            clipped.unlink()
        if progress_cb:
            progress_cb(f"Reprojecting {_SRC_EPSG} → EPSG:4326 and clipping...")
        _reproject_and_clip(merged, clipped, bbox)

        # Clean up mosaic temp
        if merged != clipped and merged.name == "_igic_mosaic.tif" and merged.exists():
            merged.unlink()

        if progress_cb:
            mb = clipped.stat().st_size / (1024 * 1024)
            progress_cb(f"Done: {clipped.name} ({mb:.1f} MB)")

        return clipped

    def _counties_for_bbox(
        self, bbox: BBox, progress_cb=None
    ) -> list[tuple[str, str]]:
        """Return list of (county_name, county_slug) whose extent overlaps bbox.

        Uses the statewide county GeoJSON from digitalforestry.org.
        Falls back to bbox-center lookup if the statewide GeoJSON fails.
        """
        counties_url = f"{_BASE_URL}/gis_layers/in_counties_with_lidar_year.geojson"
        try:
            r = requests.get(counties_url, timeout=15)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            if progress_cb:
                progress_cb(f"Warning: could not fetch county index ({e}), trying center lookup")
            return self._counties_for_bbox_fallback(bbox)

        hits = []
        for feat in data.get("features", []):
            geom = feat.get("geometry", {})
            name = feat.get("properties", {}).get("NAME_L", "")
            if name and _bbox_intersects(geom, bbox):
                slug = _county_to_slug(name)
                hits.append((name, slug))

        return hits

    def _counties_for_bbox_fallback(self, bbox: BBox) -> list[tuple[str, str]]:
        """Fallback: return a best-guess single county from bbox center.

        Not reliable for bbox spanning multiple counties; prefer the GeoJSON path.
        """
        # This should rarely be needed; raise with a helpful message
        raise RuntimeError(
            "Could not fetch Indiana county index from lidar.digitalforestry.org. "
            "Please check your network connection and try again."
        )
