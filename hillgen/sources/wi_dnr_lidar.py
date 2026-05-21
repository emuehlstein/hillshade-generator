"""Wisconsin DNR LiDAR DEM source.

Downloads 1m LiDAR-derived elevation data from the Wisconsin DNR ArcGIS ImageServer.

Service: https://dnrmaps.wi.gov/arcgis_image/rest/services/DW_Elevation/EN_DEM_from_LiDAR/ImageServer
CRS:     EPSG:3071 (Wisconsin Transverse Mercator)
Units:   Meters, NAVD88

For large bboxes, the request is split into 0.5° chunks downloaded in parallel
and mosaicked with rasterio.merge.
"""

import math
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List, Optional, Tuple
from urllib.parse import urlencode

import requests

from .base import BBox

BASE_URL = (
    "https://dnrmaps.wi.gov/arcgis_image/rest/services"
    "/DW_Elevation/EN_DEM_from_LiDAR/ImageServer/exportImage"
)

# Wisconsin coverage in WGS84 (slightly generous to catch edge bboxes)
_WI_WEST = -92.9
_WI_EAST = -86.8
_WI_SOUTH = 42.4
_WI_NORTH = 47.1

# Max chunk size per request (degrees). ~0.5° ≈ ~55km at WI latitude.
_MAX_TILE_DEG = 0.5
# Max pixel dimension per request
_MAX_PIXELS = 4096


def _wgs84_to_epsg3071(west: float, south: float, east: float, north: float):
    """Convert WGS84 bbox to EPSG:3071 (Wisconsin Transverse Mercator).

    Uses pyproj when available; falls back to a close approximation.
    """
    try:
        from pyproj import Transformer
        t = Transformer.from_crs("EPSG:4326", "EPSG:3071", always_xy=True)
        xmin, ymin = t.transform(west, south)
        xmax, ymax = t.transform(east, north)
        return xmin, ymin, xmax, ymax
    except ImportError:
        # Fallback: approximate WTM projection
        # Central meridian -90°, scale 0.9996, false easting 520000, false northing -4480000
        import math
        lon0 = math.radians(-90.0)
        a = 6378137.0
        f = 1 / 298.257223563
        b = a * (1 - f)
        e2 = 1 - (b / a) ** 2
        k0 = 0.9996
        fe, fn = 520000.0, -4480000.0

        def project(lon_deg, lat_deg):
            lon = math.radians(lon_deg)
            lat = math.radians(lat_deg)
            e = math.sqrt(e2)
            N = a / math.sqrt(1 - e2 * math.sin(lat) ** 2)
            T = math.tan(lat) ** 2
            C = e2 / (1 - e2) * math.cos(lat) ** 2
            A = (lon - lon0) * math.cos(lat)
            e4 = e2 ** 2
            e6 = e2 ** 3
            M = a * (
                (1 - e2 / 4 - 3 * e4 / 64 - 5 * e6 / 256) * lat
                - (3 * e2 / 8 + 3 * e4 / 32 + 45 * e6 / 1024) * math.sin(2 * lat)
                + (15 * e4 / 256 + 45 * e6 / 1024) * math.sin(4 * lat)
                - 35 * e6 / 3072 * math.sin(6 * lat)
            )
            x = fe + k0 * N * (A + (1 - T + C) * A ** 3 / 6)
            y = fn + k0 * (M + N * math.tan(lat) * (A ** 2 / 2 + (5 - T + 9 * C) * A ** 4 / 24))
            return x, y

        xmin, ymin = project(west, south)
        xmax, ymax = project(east, north)
        return xmin, ymin, xmax, ymax


def _build_export_url(bbox_wgs84: BBox) -> str:
    """Build the exportImage URL for a single bbox chunk."""
    xmin, ymin, xmax, ymax = _wgs84_to_epsg3071(
        bbox_wgs84.west, bbox_wgs84.south, bbox_wgs84.east, bbox_wgs84.north
    )

    # Compute pixel dimensions preserving aspect ratio, capped at MAX_PIXELS
    deg_w = bbox_wgs84.east - bbox_wgs84.west
    deg_h = bbox_wgs84.north - bbox_wgs84.south
    ratio = deg_w / deg_h
    if ratio >= 1:
        px_w = _MAX_PIXELS
        px_h = max(1, round(_MAX_PIXELS / ratio))
    else:
        px_h = _MAX_PIXELS
        px_w = max(1, round(_MAX_PIXELS * ratio))

    params = {
        "bbox": f"{xmin},{ymin},{xmax},{ymax}",
        "bboxSR": "3071",
        "imageSR": "3071",  # native CRS — service does not support on-the-fly reprojection
        "size": f"{px_w},{px_h}",
        "format": "tiff",
        "pixelType": "F32",
        "noData": "-9999",
        "noDataInterpretation": "esriNoDataMatchAny",
        "interpolation": "+RSP_BilinearInterpolation",
        "f": "image",
    }
    return f"{BASE_URL}?{urlencode(params)}"


def _chunk_bbox(bbox: BBox) -> List[BBox]:
    """Split a large bbox into 0.5° tiles for parallel download."""
    chunks = []
    lat = bbox.south
    while lat < bbox.north:
        lat_top = min(lat + _MAX_TILE_DEG, bbox.north)
        lon = bbox.west
        while lon < bbox.east:
            lon_right = min(lon + _MAX_TILE_DEG, bbox.east)
            try:
                chunks.append(BBox(west=lon, south=lat, east=lon_right, north=lat_top))
            except ValueError:
                pass  # skip degenerate chunks
            lon = lon_right
        lat = lat_top
    return chunks if chunks else [bbox]


def _download_chunk(bbox: BBox, output_path: Path, session: requests.Session) -> Path:
    """Download one chunk tile and save as GeoTIFF."""
    if output_path.exists():
        return output_path

    url = _build_export_url(bbox)
    tmp = output_path.with_suffix(".tmp")
    try:
        with session.get(url, stream=True, timeout=120) as r:
            r.raise_for_status()
            ct = r.headers.get("Content-Type", "")
            if "json" in ct or "html" in ct:
                err = r.text[:300]
                raise RuntimeError(f"Service returned non-image response: {err}")
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=4 * 1024 * 1024):
                    f.write(chunk)
        tmp.rename(output_path)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise
    return output_path


class WiDNRLiDAR:
    """Wisconsin DNR LiDAR 1m DEM via ArcGIS ImageServer."""

    name = "wi-dnr-lidar"
    description = "Wisconsin DNR LiDAR 1m DEM (EPSG:3071, meters NAVD88, WI only)"
    resolution_m = 1.0
    priority = 90  # higher than USGS 3DEP (80) when in WI

    def covers(self, bbox: BBox) -> bool:
        return (
            bbox.west >= _WI_WEST
            and bbox.east <= _WI_EAST
            and bbox.south >= _WI_SOUTH
            and bbox.north <= _WI_NORTH
        )

    def download(self, bbox: BBox, output_dir: Path, progress_cb=None) -> Path:
        """Download LiDAR DEM chunks, mosaic, clip, return GeoTIFF path."""
        output_dir.mkdir(parents=True, exist_ok=True)

        chunks = _chunk_bbox(bbox)
        if progress_cb:
            progress_cb(f"wi-dnr-lidar: {len(chunks)} tile(s) to download")

        # Download chunks in parallel
        chunk_paths = []
        with requests.Session() as session, ThreadPoolExecutor(max_workers=4) as pool:
            futures = {}
            for i, chunk in enumerate(chunks):
                slug = (
                    f"wi_dnr_{chunk.south:.3f}_{chunk.west:.3f}"
                    f"_{chunk.north:.3f}_{chunk.east:.3f}.tif"
                )
                out = output_dir / slug
                futures[pool.submit(_download_chunk, chunk, out, session)] = (i, chunk)

            for fut in as_completed(futures):
                i, chunk = futures[fut]
                try:
                    path = fut.result()
                    chunk_paths.append(path)
                    if progress_cb:
                        size_mb = path.stat().st_size / (1024 * 1024)
                        progress_cb(f"  chunk {i+1}/{len(chunks)}: {size_mb:.1f} MB")
                except Exception as e:
                    raise RuntimeError(
                        f"Failed to download WI DNR LiDAR chunk {chunk}: {e}"
                    ) from e

        # Mosaic if multiple chunks
        if len(chunk_paths) == 1:
            merged = chunk_paths[0]
        else:
            if progress_cb:
                progress_cb(f"Mosaicking {len(chunk_paths)} chunks...")
            merged = self._mosaic(chunk_paths, output_dir)

        # Clip to exact bbox
        clipped = output_dir / self._output_filename(bbox)
        if clipped.exists():
            clipped.unlink()
        if progress_cb:
            progress_cb("Clipping to bbox...")
        self._clip_to_bbox(merged, clipped, bbox)

        if progress_cb:
            size_mb = clipped.stat().st_size / (1024 * 1024)
            progress_cb(f"Done: {clipped.name} ({size_mb:.1f} MB)")

        return clipped

    def _mosaic(self, paths: List[Path], output_dir: Path) -> Path:
        """Merge multiple GeoTIFF tiles into one using rasterio.merge."""
        try:
            import rasterio
            from rasterio.merge import merge

            datasets = [rasterio.open(p) for p in paths]
            mosaic, transform = merge(datasets)
            meta = datasets[0].meta.copy()
            meta.update({
                "driver": "GTiff",
                "height": mosaic.shape[1],
                "width": mosaic.shape[2],
                "transform": transform,
                "compress": "deflate",
                "tiled": True,
            })
            out = output_dir / "_wi_dnr_mosaic.tif"
            with rasterio.open(out, "w", **meta) as dst:
                dst.write(mosaic)
            for ds in datasets:
                ds.close()
            return out
        except ImportError:
            # Fall back to gdal_merge.py
            out = output_dir / "_wi_dnr_mosaic.tif"
            cmd = [
                "gdal_merge.py", "-o", str(out),
                "-co", "COMPRESS=DEFLATE",
                "-co", "BIGTIFF=IF_SAFER",
            ] + [str(p) for p in paths]
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            return out

    def _clip_to_bbox(self, src: Path, dst: Path, bbox: BBox):
        cmd = [
            "gdalwarp",
            "-te", str(bbox.west), str(bbox.south), str(bbox.east), str(bbox.north),
            "-co", "COMPRESS=DEFLATE",
            "-co", "TILED=YES",
            "-co", "BIGTIFF=IF_SAFER",
            str(src), str(dst),
        ]
        subprocess.run(cmd, check=True, capture_output=True, text=True)

    def _output_filename(self, bbox: BBox) -> str:
        lat = (bbox.south + bbox.north) / 2
        lon = (bbox.west + bbox.east) / 2
        ns = "n" if lat >= 0 else "s"
        ew = "w" if lon < 0 else "e"
        return f"wi_dnr_lidar_{ns}{abs(lat):.2f}_{ew}{abs(lon):.2f}.tif"
