"""GMTED2010 30 arc-second (~1km) global DEM source.

Downloads 20°×30° tiles from the USGS/NGA Global Multi-resolution Terrain
Elevation Data 2010 product. Tiles are publicly accessible — no auth needed.

URL pattern:
    https://edcintl.cr.usgs.gov/downloads/sciweb1/shared/topo/downloads/GMTED/
        Global_tiles_GMTED/300darcsec/mea/W{lon:03d}/{lat}{NS}{lon:03d}{EW}_20101117_gmted_mea300.tif

Why this source exists
----------------------
Every other source in the registry is 10m or finer. That is correct for the
county- and park-scale layers the platform ships, and catastrophic for a
continental build: a CONUS bbox at zoom 0-6 produces ~12 MB of tiles, but
resolve_source would hand you usgs-3dep-1m and a 72.8 GB download that takes
three and a quarter hours. GMTED2010 at 30 arc-second is ~100 MB for the same
area.

Resolution honesty
------------------
30 arc-second is ~0.7-0.9 km/px at CONUS latitudes. A zoom-6 tile is
5.625° across 256 px, i.e. ~2.4 km/px, so this source is still slightly
oversampled at z6 and comfortable through z0-6. Do not push it past z7 —
use the 7.5 arc-second GMTED subset (75darcsec) or 3DEP if you need that.

Priority
--------
priority=5/6/7 (30s/15s/7.5s), deliberately below every other source
(including the 30m Copernicus global fallback at priority=10). A global
source whose
covers() returns True everywhere would otherwise hijack auto-resolution for
every county build on the platform. The consequence is that 'auto' will NOT
select this source for a CONUS bbox either — usgs-3dep-1m (priority 85) still
wins because its covers() is also True there. Continental builds must pass
--dem gmted2010-30s explicitly. Making resolve_source scale-aware (deriving a
target ground resolution from the requested max zoom) is the real fix and is
deliberately not attempted here.
"""

import math
import subprocess
from pathlib import Path
from typing import List, Tuple

import requests

from .base import BBox

# Base URL for the GMTED2010 global tile tree. The resolution directory and
# the "mea" (mean statistic) segment are appended per-source, so this stops
# at Global_tiles_GMTED.
_BASE = (
    "https://edcintl.cr.usgs.gov/downloads/sciweb1/shared/topo/downloads/"
    "GMTED/Global_tiles_GMTED"
)

# GMTED2010 global tiling scheme: 20° of latitude by 30° of longitude.
# Tiles are named for their SOUTH-WEST corner.
#
# The latitude grid is ANCHORED AT -90, so the valid south-west corners are
# -90,-70,-50,-30,-10,10,30,50,70 — NOT multiples of 20. Snapping a latitude
# with floor(lat/20)*20 yields 20N/40N, which do not exist and 404.
_LAT_STEP = 20
_LON_STEP = 30
_LAT_ORIGIN = -90
_LON_ORIGIN = -180
_LAT_MAX = 70   # southern edge of the northernmost band (70N..90N)
_LON_MAX = 150  # western edge of the easternmost band (150E..180E)


class _GMTED2010Base:
    """Shared machinery for the GMTED2010 resolution family.

    GMTED2010 publishes the same global 20°x30° tile grid at three
    resolutions. They differ only in a URL path segment, a filename suffix
    and the pixel size, so the download/merge/clip/void-fill pipeline is
    identical. Subclasses set the four class attributes below.
    """

    # URL path segment, e.g. "300darcsec"
    _ARCSEC_DIR: str = ""
    # Filename statistic suffix, e.g. "mea300"
    _MEA_SUFFIX: str = ""
    # Cache filename prefix; MUST differ per resolution or a 30s clip and a
    # 7.5s clip of the same bbox collide in the cache and the coarser one
    # silently wins.
    _FILE_PREFIX: str = ""

    name = ""
    description = ""
    resolution_m = 0.0
    priority = 5

    # GMTED2010 spans 84°S to 84°N. Outside that there is simply no data.
    _coverage_south = -84.0
    _coverage_north = 84.0

    def covers(self, bbox: BBox) -> bool:
        """Global, within the GMTED2010 latitude envelope."""
        return (
            bbox.south >= self._coverage_south
            and bbox.north <= self._coverage_north
        )

    def download(self, bbox: BBox, output_dir: Path, progress_cb=None) -> Path:
        """Download GMTED tiles covering bbox, merge if needed, clip to bbox."""
        output_dir.mkdir(parents=True, exist_ok=True)

        tiles = self._tiles_for_bbox(bbox)
        if progress_cb:
            progress_cb(f"Need {len(tiles)} tile(s): {', '.join(t[0] for t in tiles)}")

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

        if not downloaded:
            raise ValueError(
                f"No GMTED2010 tiles available for bbox "
                f"({bbox.west:.2f},{bbox.south:.2f},{bbox.east:.2f},{bbox.north:.2f})"
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

        if progress_cb:
            progress_cb("Filling interior voids...")

        filled = self._fill_interior_voids(clipped)
        if progress_cb and filled:
            progress_cb(f"  patched {filled} void px (GMTED2010 dropped cells)")

        for pattern in ("_merged_*.tif", "_merged_*.vrt"):
            for f in output_dir.glob(pattern):
                if f != clipped:
                    f.unlink()

        if progress_cb:
            size_mb = clipped.stat().st_size / (1024 * 1024)
            progress_cb(f"Done: {clipped.name} ({size_mb:.1f} MB)")

        return clipped

    def _tiles_for_bbox(self, bbox: BBox) -> List[Tuple[str, str]]:
        """Return [(tile_name, url)] for all 20°x30° tiles covering bbox.

        Tiles are named for their south-west corner, so we floor each bbox
        edge onto the tile grid and walk it.
        """
        def _snap(value: float, step: int, origin: int, hi: int) -> int:
            """Snap a coordinate down onto the tile grid anchored at origin."""
            snapped = int(math.floor((value - origin) / step) * step + origin)
            return max(origin, min(hi, snapped))

        lat_start = _snap(bbox.south, _LAT_STEP, _LAT_ORIGIN, _LAT_MAX)
        lat_end = _snap(bbox.north, _LAT_STEP, _LAT_ORIGIN, _LAT_MAX)
        lon_start = _snap(bbox.west, _LON_STEP, _LON_ORIGIN, _LON_MAX)
        lon_end = _snap(bbox.east, _LON_STEP, _LON_ORIGIN, _LON_MAX)

        tiles = []
        for lat in range(lat_start, lat_end + 1, _LAT_STEP):
            for lon in range(lon_start, lon_end + 1, _LON_STEP):
                ns = "N" if lat >= 0 else "S"
                ew = "E" if lon >= 0 else "W"
                tile_name = f"{abs(lat):02d}{ns}{abs(lon):03d}{ew}"
                directory = f"{ew}{abs(lon):03d}"
                filename = f"{tile_name}_20101117_gmted_{self._MEA_SUFFIX}.tif"
                url = f"{_BASE}/{self._ARCSEC_DIR}/mea/{directory}/{filename}"
                tiles.append((tile_name, url))

        return tiles

    def _download_tile(self, url: str, output: Path):
        """Download a single tile with streaming."""
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
        """Merge tiles into a VRT.

        Same rationale as usgs_3dep.py: gdal_merge.py routes pixels through
        osgeo.gdal_array and dies on a numpy ABI mismatch. A VRT is a few KB
        and gdalwarp reads it directly in the clip step that always follows.
        """
        import hashlib
        key = hashlib.md5("|".join(str(t) for t in sorted(tiles)).encode()).hexdigest()[:8]
        merged = output_dir / f"_merged_{key}.vrt"
        cmd = [
            "gdalbuildvrt",
            "-resolution", "highest",
            str(merged),
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

    def _fill_interior_voids(self, path: Path) -> int:
        """Patch GMTED2010's dropped cells, in place. Returns pixels filled.

        GMTED2010's mean-statistic product drops whole 1°x1° cells to 0 over
        some large inland water bodies. A confirmed example sits in Lake
        Superior at lon -87..-86, lat 47..48: a clean 120x120 block of 0 ringed
        entirely by the lake's true 179 m surface. Left alone it renders as a
        hard-edged dark rectangle in any elevation-coloured theme.

        The fix has to distinguish those voids from the ocean, which is also
        legitimately 0. It does so topologically rather than by value: ocean is
        connected to the raster border, an interior void is not. Only
        zero-valued connected components that touch no edge are filled, each
        from its nearest valid neighbour. Coastal land sitting at exactly 0 m
        stays connected to the sea and is therefore never touched.
        """
        import numpy as np
        import rasterio
        from scipy import ndimage

        with rasterio.open(path) as ds:
            band = ds.read(1)
            profile = ds.profile

        zero = band == 0
        if not zero.any():
            return 0

        labels, count = ndimage.label(zero)
        if count == 0:
            return 0

        # Any component appearing on the border is sea (or a genuine edge
        # flat), not a void. Resolve membership through a per-label lookup
        # rather than np.isin: isin against thousands of edge labels is a
        # sort over every pixel, and at 7.5 arc-second "every pixel" is 344
        # million of them.
        border = np.concatenate([
            labels[0, :], labels[-1, :], labels[:, 0], labels[:, -1],
        ])
        is_void = np.ones(count + 1, dtype=bool)
        is_void[0] = False
        is_void[np.unique(border)] = False

        void_ids = np.flatnonzero(is_void)
        if void_ids.size == 0:
            return 0

        interior = is_void[labels]
        n = int(interior.sum())
        if n == 0:
            return 0

        # Nearest-valid-neighbour fill, run per component inside its own
        # bounding box. A whole-raster distance_transform_edt with
        # return_indices allocates two index planes the size of the raster
        # (2.75 GB at 7.5 arc-second, on top of the 1.4 GB label array) and
        # spends that cost measuring distances across thousands of km of
        # ocean that will never be written. The voids are a handful of small
        # blocks, so window down to them.
        boxes = ndimage.find_objects(labels)
        height, width = band.shape

        for lid in void_ids:
            box = boxes[lid - 1]
            if box is None:
                continue
            ys, xs = box
            # Pad by one pixel so the window always contains the valid ring
            # the void is to be filled from.
            y0, y1 = max(0, ys.start - 1), min(height, ys.stop + 1)
            x0, x1 = max(0, xs.start - 1), min(width, xs.stop + 1)

            sub = band[y0:y1, x0:x1]
            target = interior[y0:y1, x0:x1] & (labels[y0:y1, x0:x1] == lid)
            if not target.any():
                continue

            # Measure distance against EVERY zero in the window, not just
            # this component. If a neighbouring void (or a corner of the
            # ocean) were treated as valid data the fill would propagate a 0
            # back into the hole it is meant to repair. Only this
            # component's pixels are written, so ocean inside the window is
            # read past and left untouched.
            _, idx = ndimage.distance_transform_edt(
                sub == 0, return_distances=True, return_indices=True
            )
            sub[target] = sub[tuple(i[target] for i in idx)]

        # Forcing tiled=True without a block size inherits whatever the
        # source profile had; if that was untiled, GDAL rejects the write
        # ("blocks must be multiples of 16"). Pin the block size explicitly.
        profile.update(
            compress="DEFLATE", tiled=True, bigtiff="IF_SAFER",
            blockxsize=256, blockysize=256,
        )
        with rasterio.open(path, "w", **profile) as dst:
            dst.write(band, 1)

        return n

    def _output_filename(self, bbox: BBox) -> str:
        """Generate a deterministic output filename from the bbox."""
        lat = (bbox.south + bbox.north) / 2
        lon = (bbox.west + bbox.east) / 2
        ns = "n" if lat >= 0 else "s"
        ew = "w" if lon < 0 else "e"
        return f"{self._FILE_PREFIX}_{ns}{abs(lat):.2f}_{ew}{abs(lon):.2f}.tif"


class GMTED2010_30s(_GMTED2010Base):
    """GMTED2010 30 arc-second (~1km), global. Honest through zoom 7."""

    _ARCSEC_DIR = "300darcsec"
    _MEA_SUFFIX = "mea300"
    _FILE_PREFIX = "gmted30s"

    name = "gmted2010-30s"
    description = (
        "GMTED2010 30 arc-second (~1km) global DEM — continental builds, z0-7"
    )
    # 30 arc-second at the equator; the real value shrinks with latitude.
    resolution_m = 926.0
    priority = 5


class GMTED2010_15s(_GMTED2010Base):
    """GMTED2010 15 arc-second (~463m), global. Honest through zoom 8."""

    _ARCSEC_DIR = "150darcsec"
    _MEA_SUFFIX = "mea150"
    _FILE_PREFIX = "gmted15s"

    name = "gmted2010-15s"
    description = (
        "GMTED2010 15 arc-second (~463m) global DEM — continental builds, z0-8"
    )
    resolution_m = 463.0
    priority = 6


class GMTED2010_7_5s(_GMTED2010Base):
    """GMTED2010 7.5 arc-second (~231m), global. Honest through zoom 9."""

    _ARCSEC_DIR = "075darcsec"
    _MEA_SUFFIX = "mea075"
    _FILE_PREFIX = "gmted75s"

    name = "gmted2010-7.5s"
    description = (
        "GMTED2010 7.5 arc-second (~231m) global DEM — continental builds, z0-9"
    )
    resolution_m = 231.0
    priority = 7
