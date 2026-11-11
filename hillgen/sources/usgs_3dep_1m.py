"""USGS 3DEP 1-metre DEM source (national ArcGIS ImageServer).

Pulls 1 m bare-earth elevation from the USGS National Map 3DEP multi-resolution
ImageServer via exportImage, tiling the request grid under the service's hard
8000x8000 px cap, then merging and reprojecting to EPSG:4326.

Service:

    https://elevation.nationalmap.gov/arcgis/rest/services/3DEPElevation/ImageServer

Probed service metadata (verified live):
    maxImageWidth = maxImageHeight = 8000   <- HARD cap
    spatialReference                        = EPSG:3857 (wkid 102100)
    pixelSizeX = pixelSizeY                 = 1.0 (3857 units)
    bandCount = 1, pixelType = F32
    serviceDataType = esriImageServiceDataTypeElevation

Vertical units are METRES, no conversion applied. Verified against the live
service: Mt Tamalpais (-122.5964, 37.9235) -> 785.4 m max over the summit tile
(published summit 784 m); Mary's Peak (-123.5518, 44.5044) -> 1244.6 (published
1249 m).

COVERAGE IS PATCHY
------------------
This endpoint is a *best-available* mosaic. Where 1 m lidar does not exist it
does NOT return NoData -- it silently serves 1/3 arc-second (~10 m) data
resampled up to the requested cell size. Verified: a 1 m request over Newport,
OR returns a full tile of plausible-looking 10 m terrain (min -0.12, max 41.6,
100% valid). Shipping that as "1 m" produces a mushy hillshade that looks fine
until you compare it against real lidar.

So coverage is gated on the authoritative 3DEP coverage index instead of on the
pixels that come back:

    https://index.nationalmap.gov/arcgis/rest/services/3DEPElevationIndex/MapServer/1

Layer 1 is the "1 meter" polygon footprint layer. A returnCountOnly envelope
query against it is one cheap request and cleanly discriminates covered from
uncovered areas (Newport -> 0, Mt Tamalpais -> 1). covers() consults it, and
download() re-checks and raises NoOneMeterCoverageError naming the offending
bbox rather than quietly returning fallback data.

Output contract: single-band Float32 GeoTIFF, EPSG:4326, elevation in metres --
a drop-in substitute for hillgen.sources.usgs_3dep output, using the same cache
directory and buffer conventions.
"""

import hashlib
import json
import math
import os
import random
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .base import BBox

# --- Service endpoints -----------------------------------------------------

_IMAGESERVER = (
    "https://elevation.nationalmap.gov/arcgis/rest/services"
    "/3DEPElevation/ImageServer"
)

# Layer 1 of the 3DEP elevation index is the "1 meter" footprint layer.
_INDEX_1M_LAYER = (
    "https://index.nationalmap.gov/arcgis/rest/services"
    "/3DEPElevationIndex/MapServer/1/query"
)

# --- Request sizing --------------------------------------------------------

# Hard server cap, confirmed from ?f=json. Requests above this are rejected.
_SERVER_MAX_PX = 8000

# Per-request budget per side. Deliberately well under the cap: ArcGIS counts
# the cap against the image it actually renders, and leaving headroom absorbs
# the rounding done when converting a tile extent back to a pixel count.
# At the native 1 m cell size this is a 5 km x 5 km request.
# MEASURED request ceiling -- do not raise without re-measuring.
#
# The documented "maxImageWidth = maxImageHeight = 8000 hard cap" is not the
# binding constraint. The real limit is RESPONSE BYTES, ~32 MB, and it is the
# same on the Oregon DOGAMI ImageServer:
#
#     px/side   Float32 bytes   3DEP   DOGAMI
#     2048      16.8 MB         200    200
#     2400      23.7 MB         200    200
#     2800      31.7 MB         200    200
#     3000      36.0 MB         504    500
#
# Repeating a small request four times returned 200 every time, so this is a
# hard size ceiling, not flakiness. 2560 px/side = 26.2 MB leaves ~20% headroom.
# At 1 m that is a 2.56 km tile.
_TILE_PX = 2560

# Native cell size of the service, in EPSG:3857 units.
_NATIVE_CELL_3857 = 1.0

# Nominal metres per degree of latitude (WGS84 mean). Longitude is scaled by
# cos(lat) from the same constant.
_M_PER_DEG = 111_320.0

# Generous CONUS envelope. 3DEP 1 m exists in AK/HI/PR too, but the index query
# is what actually decides; this is only a cheap pre-filter.
_CONUS_WEST = -125.1
_CONUS_EAST = -66.8
_CONUS_SOUTH = 24.4
_CONUS_NORTH = 49.5

_R_MERC = 6378137.0
_MERC_MAX_LAT = 85.05112878

_HTTP_UA = "curl/8.4.0"


class NoOneMeterCoverageError(RuntimeError):
    """Raised when 3DEP has no 1 m lidar over the requested bbox.

    Catchable and distinct from transport errors, so a caller can fall back to
    usgs-3dep-10m deliberately instead of unknowingly rendering a flat grey
    hillshade built from upsampled 10 m data.
    """


# ---------------------------------------------------------------------------
# Web Mercator helpers (pure math -- no pyproj / GDAL python bindings needed)
# ---------------------------------------------------------------------------

def _lon_to_x(lon: float) -> float:
    return math.radians(lon) * _R_MERC


def _lat_to_y(lat: float) -> float:
    lat = max(min(lat, _MERC_MAX_LAT), -_MERC_MAX_LAT)
    return math.log(math.tan(math.pi / 4.0 + math.radians(lat) / 2.0)) * _R_MERC


def _x_to_lon(x: float) -> float:
    return math.degrees(x / _R_MERC)


def _y_to_lat(y: float) -> float:
    return math.degrees(2.0 * math.atan(math.exp(y / _R_MERC)) - math.pi / 2.0)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _http_get(url: str, timeout: int = 120, retries: int = 8) -> bytes:
    """GET with retry on 5xx and transient transport errors.

    The National Map front end intermittently answers 502/503/504 under load --
    observed even on a plain ?f=json metadata call -- so every request here is
    retried with exponential backoff.

    Be patient here. A single export can be one of several hundred tiles making
    up one chunk of a multi-hour build, and losing it aborts the whole chunk.
    The old budget was 4 attempts over ~14s, which a 504 under load outlasts
    easily; a real corridor run died on exactly that. Backoff is capped so the
    tail attempts stay spaced without growing without bound, and jitter keeps a
    retry storm from re-synchronising against an already-struggling server.
    """
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": _HTTP_UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code < 500:
                raise
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_err = e
        if attempt < retries - 1:
            delay = min(2.0 * (2 ** attempt), 60.0)
            time.sleep(delay + random.uniform(0.0, 0.25 * delay))
    raise RuntimeError(f"GET failed after {retries} attempts: {url}\n  {last_err}")


# ---------------------------------------------------------------------------
# Coverage index
# ---------------------------------------------------------------------------

def _index_count(bbox: BBox, timeout: int = 45) -> int:
    """Count of 1 m footprint polygons intersecting bbox; -1 if unreachable."""
    params = urllib.parse.urlencode({
        "geometry": f"{bbox.west},{bbox.south},{bbox.east},{bbox.north}",
        "geometryType": "esriGeometryEnvelope",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "returnCountOnly": "true",
        "f": "json",
    })
    try:
        raw = _http_get(f"{_INDEX_1M_LAYER}?{params}", timeout=timeout, retries=3)
        return int(json.loads(raw).get("count", 0))
    except Exception:
        return -1  # unreachable; caller decides what to do


def _center_covered(bbox: BBox, timeout: int = 45) -> int:
    """1 if the bbox centre point sits inside a 1 m footprint, 0 if not, -1 unknown.

    One extra cheap request. An envelope that merely clips the edge of a
    footprint still returns count>0, so this distinguishes "the area is mostly
    covered" from "a corner grazes a neighbouring project".
    """
    cx = (bbox.west + bbox.east) / 2.0
    cy = (bbox.south + bbox.north) / 2.0
    params = urllib.parse.urlencode({
        "geometry": f"{cx},{cy}",
        "geometryType": "esriGeometryPoint",
        "inSR": 4326,
        "spatialRel": "esriSpatialRelIntersects",
        "returnCountOnly": "true",
        "f": "json",
    })
    try:
        raw = _http_get(f"{_INDEX_1M_LAYER}?{params}", timeout=timeout, retries=2)
        return 1 if int(json.loads(raw).get("count", 0)) > 0 else 0
    except Exception:
        return -1


# ---------------------------------------------------------------------------
# exportImage
# ---------------------------------------------------------------------------

def _export_url(west: float, south: float, east: float, north: float,
                width: int, height: int) -> str:
    """Build an exportImage URL for a WGS84 bbox, rendered in EPSG:3857.

    bboxSR is 4326 and imageSR is 3857 (the service's native SR), so the server
    does no reprojection beyond the cell-size change. A lon/lat rectangle maps
    to an axis-aligned Web Mercator rectangle exactly, so requesting the bbox in
    4326 while rendering in 3857 neither skews nor crops the tile.

    Plain WKID integers are used rather than JSON spatial-reference objects --
    some ArcGIS deployments answer the JSON form with an HTML error page.
    """
    params = urllib.parse.urlencode({
        "bbox": f"{west},{south},{east},{north}",
        "bboxSR": 4326,
        "imageSR": 3857,
        "size": f"{width},{height}",
        "format": "tiff",
        "pixelType": "F32",
        "noDataInterpretation": "esriNoDataMatchAny",
        "interpolation": "RSP_BilinearInterpolation",
        "f": "image",
    })
    return f"{_IMAGESERVER}/exportImage?{params}"


def _fetch_tile(url: str, dest: Path, timeout: int = 600) -> None:
    """Fetch one exportImage tile to dest, validating it is really a TIFF."""
    tmp = dest.with_suffix(".tmp")
    try:
        # Tile fetches are the long pole: a single 502/504 here throws away
        # the whole chunk's DEM pass. Let _http_get's jittered backoff ride
        # out an ImageServer wobble instead of hardcoding a short count.
        data = _http_get(url, timeout=timeout, retries=8)
        if data[:2] not in (b"II", b"MM"):
            snippet = data[:300].decode("utf-8", errors="replace")
            raise RuntimeError(
                f"3DEP exportImage returned a non-TIFF response:\n{snippet}"
            )
        tmp.write_bytes(data)
        tmp.replace(dest)
    finally:
        if tmp.exists():
            tmp.unlink()


# ---------------------------------------------------------------------------
# GDAL helpers (C binaries only)
# ---------------------------------------------------------------------------

def _run(cmd: List[str], env: Optional[Dict[str, str]] = None
         ) -> subprocess.CompletedProcess:
    run_env = None
    if env:
        run_env = dict(os.environ)
        run_env.update(env)
    r = subprocess.run(cmd, capture_output=True, text=True, env=run_env)
    if r.returncode != 0:
        raise RuntimeError(
            f"{cmd[0]} failed (exit {r.returncode}): {r.stderr.strip()[:400]}"
        )
    return r


def _build_vrt(tiles: List[Path], dest: Path) -> Path:
    """Mosaic tiles into a VRT.

    gdalbuildvrt, never gdal_merge.py: gdal_merge.py pushes every pixel through
    osgeo.gdal_array (numpy), which is broken on this machine and is deprecated
    upstream anyway. A VRT is a few KB, needs no numpy, and gdalwarp consumes it
    directly in the reproject/clip step that always follows.
    """
    _run(["gdalbuildvrt", "-resolution", "highest", "-q", str(dest)]
         + [str(t) for t in tiles])
    return dest


def _warp_to_4326(src: Path, dest: Path, bbox: BBox,
                  tr_x: float, tr_y: float) -> None:
    """Reproject 3857 -> 4326, clip to the exact bbox, at a fixed cell size."""
    _run([
        "gdalwarp",
        "-overwrite",
        "-t_srs", "EPSG:4326",
        "-te", str(bbox.west), str(bbox.south), str(bbox.east), str(bbox.north),
        "-te_srs", "EPSG:4326",
        "-tr", f"{tr_x:.12f}", f"{tr_y:.12f}",
        "-r", "bilinear",
        "-ot", "Float32",
        "-dstnodata", "-999999",
        "-co", "COMPRESS=DEFLATE",
        "-co", "PREDICTOR=3",
        "-co", "TILED=YES",
        "-co", "BIGTIFF=IF_SAFER",
        "-q",
        str(src), str(dest),
    ])


def _band_stats(path: Path) -> Dict[str, float]:
    """min/max/mean/valid_percent for band 1, via gdalinfo -json -stats.

    GDAL_PAM_ENABLED=NO keeps gdalinfo from dropping a .aux.xml sidecar next to
    the cached GeoTIFF; the stats are only wanted in-process.
    """
    r = _run(["gdalinfo", "-json", "-stats", str(path)],
             env={"GDAL_PAM_ENABLED": "NO"})
    info = json.loads(r.stdout)
    band = info["bands"][0]
    md = band.get("metadata", {}).get("", {})

    def pick(key: str, md_key: str) -> float:
        if key in band:
            return float(band[key])
        return float(md.get(md_key, "nan"))

    return {
        "min": pick("minimum", "STATISTICS_MINIMUM"),
        "max": pick("maximum", "STATISTICS_MAXIMUM"),
        "mean": pick("mean", "STATISTICS_MEAN"),
        "valid_percent": float(md.get("STATISTICS_VALID_PERCENT", 100.0)),
    }


# ---------------------------------------------------------------------------
# Source
# ---------------------------------------------------------------------------

class USGS3DEP1m:
    """USGS 3DEP 1 m lidar DEM, CONUS wherever 1 m coverage exists."""

    name = "usgs-3dep-1m"
    description = (
        "USGS 3DEP 1m lidar DEM via National Map ArcGIS ImageServer; "
        "CONUS-wide where 1m lidar exists (coverage is patchy)"
    )
    resolution_m = 1.0
    # Above the 10m national fallback (80), below the state/park-specific lidar
    # sources (88-95) which are either finer or more locally curated.
    priority = 85

    def __init__(self, output_resolution_m: Optional[float] = None,
                 check_index: bool = True):
        """
        Args:
            output_resolution_m: Ground cell size of the final EPSG:4326
                raster, in metres. Defaults to the native 1.0 m.
            check_index: Consult the 3DEP coverage index. Disable only if the
                bbox is already known to have 1 m coverage and the extra
                request is unwanted -- the fallback data this guard catches is
                not otherwise detectable.
        """
        self.output_resolution_m = output_resolution_m or self.resolution_m
        self.check_index = check_index
        self._coverage_cache: Dict[Tuple[float, float, float, float], int] = {}

    # -- coverage ----------------------------------------------------------

    def covers(self, bbox: BBox) -> bool:
        """True if bbox is in CONUS and the 1 m index reports a footprint.

        Falls back to the plain CONUS envelope if the index is unreachable, so
        a network blip downgrades to optimistic rather than dropping the source
        out of auto-selection. download() re-checks before spending time on a
        large pull.
        """
        if not (bbox.west < _CONUS_EAST and bbox.east > _CONUS_WEST
                and bbox.south < _CONUS_NORTH and bbox.north > _CONUS_SOUTH):
            return False
        if not self.check_index:
            return True

        key = (bbox.west, bbox.south, bbox.east, bbox.north)
        if key not in self._coverage_cache:
            self._coverage_cache[key] = _index_count(bbox)
        count = self._coverage_cache[key]
        if count < 0:
            return True  # index unreachable -- do not hard-fail selection
        return count > 0

    # -- download ----------------------------------------------------------

    def download(self, bbox: BBox, output_dir: Path, progress_cb=None) -> Path:
        """Fetch 1 m 3DEP for bbox; return a clipped EPSG:4326 GeoTIFF in metres.

        Raises:
            NoOneMeterCoverageError: no 1 m lidar over this bbox (the service
                would have silently returned upsampled 10 m data).
        """
        output_dir.mkdir(parents=True, exist_ok=True)

        def say(msg: str) -> None:
            if progress_cb:
                progress_cb(msg)

        out_path = output_dir / self._output_filename(bbox)
        if out_path.exists():
            say(f"Cached: {out_path.name}")
            return out_path

        # --- coverage gate ------------------------------------------------
        if self.check_index:
            count = self._coverage_cache.get(
                (bbox.west, bbox.south, bbox.east, bbox.north))
            if count is None:
                count = _index_count(bbox)
            if count == 0:
                raise NoOneMeterCoverageError(self._no_coverage_message(bbox))
            if count < 0:
                say("  WARNING: 3DEP coverage index unreachable; proceeding "
                    "without the 1m coverage check")
            elif _center_covered(bbox) == 0:
                say("  WARNING: bbox centre is outside the 1m footprint; this "
                    "area is only partly 1m lidar and the remainder will be "
                    "upsampled 10m data")

        # --- request grid, computed in EPSG:3857 --------------------------
        x0, x1 = _lon_to_x(bbox.west), _lon_to_x(bbox.east)
        y0, y1 = _lat_to_y(bbox.south), _lat_to_y(bbox.north)

        total_w = int(math.ceil((x1 - x0) / _NATIVE_CELL_3857))
        total_h = int(math.ceil((y1 - y0) / _NATIVE_CELL_3857))
        cols = max(1, math.ceil(total_w / _TILE_PX))
        rows = max(1, math.ceil(total_h / _TILE_PX))

        mid_lat = (bbox.south + bbox.north) / 2.0
        ground_px = _NATIVE_CELL_3857 * math.cos(math.radians(mid_lat))
        say(f"3DEP 1m: {total_w}x{total_h}px in EPSG:3857 "
            f"(~{ground_px:.2f} m ground/px) -> {cols}x{rows} request grid, "
            f"<={_TILE_PX}px/side (server cap {_SERVER_MAX_PX})")

        # --- fetch tiles --------------------------------------------------
        key = hashlib.md5(
            f"{bbox.west},{bbox.south},{bbox.east},{bbox.north}"
            f"|{_TILE_PX}|{self.output_resolution_m}".encode()
        ).hexdigest()[:8]

        x_step = (x1 - x0) / cols
        y_step = (y1 - y0) / rows
        tiles: List[Path] = []
        n = 0

        for row in range(rows):
            for col in range(cols):
                n += 1
                tx0 = x0 + col * x_step
                tx1 = min(x0 + (col + 1) * x_step, x1)
                ty1 = y1 - row * y_step
                ty0 = max(y1 - (row + 1) * y_step, y0)

                tw = min(int(math.ceil((tx1 - tx0) / _NATIVE_CELL_3857)),
                         _SERVER_MAX_PX)
                th = min(int(math.ceil((ty1 - ty0) / _NATIVE_CELL_3857)),
                         _SERVER_MAX_PX)
                if tw < 1 or th < 1:
                    continue

                tile_path = output_dir / f"_3dep1m_{key}_{row}_{col}.tif"
                if tile_path.exists():
                    say(f"  [{n}/{cols * rows}] tile ({col},{row}): cached")
                    tiles.append(tile_path)
                    continue

                say(f"  [{n}/{cols * rows}] tile ({col},{row}) {tw}x{th}px ...")
                _fetch_tile(_export_url(
                    _x_to_lon(tx0), _y_to_lat(ty0),
                    _x_to_lon(tx1), _y_to_lat(ty1),
                    tw, th,
                ), tile_path)
                tiles.append(tile_path)

        if not tiles:
            raise RuntimeError(
                f"3DEP 1m: request grid for bbox "
                f"({bbox.west},{bbox.south},{bbox.east},{bbox.north}) "
                f"produced no tiles"
            )

        # --- mosaic + reproject + clip ------------------------------------
        deg_y = self.output_resolution_m / _M_PER_DEG
        deg_x = self.output_resolution_m / (
            _M_PER_DEG * max(math.cos(math.radians(mid_lat)), 1e-6))

        vrt: Optional[Path] = None
        if len(tiles) == 1:
            src = tiles[0]
        else:
            say(f"Mosaicking {len(tiles)} tiles (gdalbuildvrt)...")
            vrt = _build_vrt(tiles, output_dir / f"_3dep1m_{key}.vrt")
            src = vrt

        say("Reprojecting to EPSG:4326 and clipping to bbox (gdalwarp)...")
        _warp_to_4326(src, out_path, bbox, deg_x, deg_y)

        # --- cleanup ------------------------------------------------------
        if vrt is not None and vrt.exists():
            vrt.unlink()
        for t in tiles:
            t.unlink(missing_ok=True)

        # --- sanity net: an all-NoData result must not pass silently -------
        stats = _band_stats(out_path)
        if stats["valid_percent"] <= 0.0 or not math.isfinite(stats["max"]):
            out_path.unlink(missing_ok=True)
            raise NoOneMeterCoverageError(
                f"USGS 3DEP returned an entirely NoData raster for bbox "
                f"({bbox.west:.5f},{bbox.south:.5f},"
                f"{bbox.east:.5f},{bbox.north:.5f}); "
                f"there is no elevation data at all here."
            )

        size_mb = out_path.stat().st_size / (1024 * 1024)
        say(f"Done: {out_path.name} ({size_mb:.1f} MB), "
            f"elevation {stats['min']:.1f}..{stats['max']:.1f} m, "
            f"{stats['valid_percent']:.1f}% valid")

        return out_path

    # -- helpers -----------------------------------------------------------

    def _no_coverage_message(self, bbox: BBox) -> str:
        index_layer = _INDEX_1M_LAYER.rsplit("/", 1)[0]
        return (
            f"USGS 3DEP has no 1m lidar coverage over bbox "
            f"({bbox.west:.5f},{bbox.south:.5f},"
            f"{bbox.east:.5f},{bbox.north:.5f}). "
            f"The ImageServer would have answered with 1/3 arc-second (~10m) "
            f"data resampled onto a 1m grid, which renders as a smooth, "
            f"detail-free hillshade. Use source 'usgs-3dep-10m' to request the "
            f"10m product explicitly, or choose a bbox inside the 1m footprint "
            f"(see {index_layer})."
        )

    def _output_filename(self, bbox: BBox) -> str:
        """Deterministic cache filename, mirroring the usgs_3dep convention."""
        lat = (bbox.south + bbox.north) / 2
        lon = (bbox.west + bbox.east) / 2
        ns = "n" if lat >= 0 else "s"
        ew = "w" if lon < 0 else "e"
        return f"3dep1m_{ns}{abs(lat):.2f}_{ew}{abs(lon):.2f}.tif"
