"""Oregon DOGAMI lidar bare-earth DTM source.

Pulls 3ft (~0.914m) bare-earth Digital Terrain Model data for anywhere in
Oregon from the DOGAMI statewide lidar mosaic ArcGIS ImageServer, using
exportImage to fetch only the requested bounding box.

Service:
    https://gis.dogami.oregon.gov/arcgis/rest/services/lidar/DIGITAL_TERRAIN_MODEL_MOSAIC/ImageServer

Two service properties drive the whole design of this adapter:

1. NATIVE SR IS EPSG:6557 -- NAD83(2011) / Oregon GIC Lambert, horizontal
   units US SURVEY FEET. The advertised pixelSizeX/Y of 3 is 3 *feet*
   (~0.914 m), and the OLC source projects report LowPS=3.

   We therefore request every tile in the native SR at exactly 3 ft/px and
   reproject to EPSG:4326 locally. Requesting a coarse cell size makes the
   mosaic serve its pyramid overviews (Ov_i02_* rasters) instead of the
   Category=1 source lidar, so the cell size we ask for is never allowed to
   drift coarser than native. Callers wanting a coarser product get it from
   the final local gdalwarp (output_resolution_m), not from the server.

2. *** VERTICAL UNITS ARE FEET (NAVD88 ft). ***
   This is the trap. The service reports maxValues = 11236.2, which is
   Mt Hood in feet (11,249 ft), not metres. An identify at Mary's Peak
   (-123.5518, 44.5044) returns 4084.83 against a published summit of
   4097 ft / 1249 m.

   Left unconverted, every hillshade off this source is vertically
   exaggerated by ~3.28x on top of whatever exaggeration the user asked
   for. We multiply by _FT_TO_M (0.3048) before returning, so the output
   is a drop-in substitute for usgs_3dep (EPSG:4326, elevation in METRES).

   The conversion is done with a VRT <ScaleRatio> on a ComplexSource and
   applied by gdalwarp. No gdal_calc.py / gdal_merge.py -- those route
   pixels through osgeo.gdal_array (numpy) and are deprecated upstream.
   Only the GDAL C binaries (gdalbuildvrt / gdalwarp / gdal_translate /
   gdaltransform) are used here.

Resolution: 3 US survey feet ~ 0.914 m
Coverage:   Oregon statewide (where OLC lidar exists)
Priority:   91
"""

import math
import os
import re
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import List, Optional, Tuple

from .base import BBox

# ---------------------------------------------------------------------------
# Service constants
# ---------------------------------------------------------------------------

_IMAGESERVER = (
    "https://gis.dogami.oregon.gov/arcgis/rest/services/lidar/"
    "DIGITAL_TERRAIN_MODEL_MOSAIC/ImageServer"
)

# Native spatial reference: NAD83(2011) / Oregon GIC Lambert (US survey feet).
# The service advertises wkid 102970 / latestWkid 6557.
_NATIVE_EPSG = 6557

# Native cell size, in native SR units (US survey feet).
_NATIVE_PIXEL_FT = 3.0

# Native cell size expressed in metres (3 US survey feet).
_NATIVE_PIXEL_M = 3.0 * 1200.0 / 3937.0

# Vertical unit conversion. Service elevations are NAVD88 FEET.
_FT_TO_M = 0.3048

# (historical note) The service advertises maxImageWidth/Height = 120000.
# Superseded by the measured ceiling below. Old reasoning: 8192 px/side
# (~24,576 ft ~ 7.5 km) is a comfortable, reliable chunk.
# MEASURED request ceiling -- do not raise without re-measuring.
#
# Both this service and the USGS 3DEP ImageServer advertise generous
# maxImageWidth/Height (120000 here, 8000 for 3DEP). Those numbers are
# fiction. The real limit is RESPONSE BYTES, and it is ~32 MB on both:
#
#     px/side   Float32 bytes   DOGAMI   3DEP
#     2048      16.8 MB         200      200
#     2400      23.7 MB         200      200
#     2800      31.7 MB         200      200
#     3000      36.0 MB         500      504
#
# Repeating a small request four times returned 200 every time, so this is a
# hard size ceiling, not flakiness. 2560 px/side = 26.2 MB leaves ~20% headroom.
_MAX_TILE_PX = 2560

# NoData sentinel ArcGIS returns for F32 exportImage responses.
_ARCGIS_NODATA = -3.4028234663852886e38
_OUT_NODATA = -9999.0

# Oregon footprint used for source selection.
#
# West/east/north are padded OUTWARD on purpose: coastal and river-mouth
# bboxes legitimately include ocean or out-of-state water that the mosaic
# serves as nodata, and clipping those boxes out of DOGAMI would needlessly
# downgrade good inland terrain to 10 m 3DEP.
#
# SOUTH IS NOT PADDED. It is the true 42nd-parallel Oregon border.
# This used to be 41.90, which combined with an overlap-style covers() to
# hand DOGAMI boxes lying almost entirely in California. A box only has to
# poke 0.01 deg north of this line to be accepted, and everything south of
# the border returns nodata, so the result was a structurally valid, fully
# blank mbtiles. See covers() below.
_OR_WEST = -124.70
_OR_EAST = -116.40
_OR_SOUTH = 42.00
_OR_NORTH = 46.32

_HTTP_RETRIES = 4
_HTTP_BACKOFF = 3.0


class ORDOGAMILidar:
    """Oregon DOGAMI statewide 3ft bare-earth lidar DTM (ArcGIS ImageServer)."""

    name = "or-dogami-lidar"
    description = (
        "Oregon DOGAMI statewide lidar bare-earth DTM, 3ft (~0.914m), "
        "via ArcGIS ImageServer bbox export (NAVD88 ft converted to metres)"
    )
    resolution_m = 0.914
    priority = 91

    def __init__(self, output_resolution_m: Optional[float] = None):
        """
        Args:
            output_resolution_m: Optional cell size (metres) for the final
                EPSG:4326 product. Tiles are ALWAYS fetched from the server at
                native 3 ft; this only controls the local resample in the last
                gdalwarp. Leave None for native resolution.
        """
        self.output_resolution_m = output_resolution_m

    # -- DEMSource protocol -------------------------------------------------

    def covers(self, bbox: BBox) -> bool:
        """True if the bbox lies entirely within the Oregon footprint.

        CONTAINMENT, not overlap. resolve_source("auto") takes the first
        source whose covers() is True in priority order, so a source that
        claims a bbox it only partly holds data for silently wins over the
        CONUS-wide 3DEP fallback and renders the uncovered remainder blank.

        Every other regional source here (Rainier, Indiana, Wisconsin,
        3DEP 10 m) already uses containment. DOGAMI did not, and that is
        what produced a 5 MB all-blank tileset for a box sitting south of
        the California line. A bbox that straddles the border must fall
        through to 3DEP, which actually has data on both sides.

        Use covers_partial() when you want the overlap test.
        """
        return (
            bbox.west >= _OR_WEST
            and bbox.east <= _OR_EAST
            and bbox.south >= _OR_SOUTH
            and bbox.north <= _OR_NORTH
        )

    def covers_partial(self, bbox: BBox) -> bool:
        """True if the bbox overlaps the Oregon footprint at all."""
        return (
            bbox.west < _OR_EAST
            and bbox.east > _OR_WEST
            and bbox.south < _OR_NORTH
            and bbox.north > _OR_SOUTH
        )

    def download(self, bbox: BBox, output_dir: Path, progress_cb=None) -> Path:
        """Fetch the DOGAMI DTM for bbox and return a clipped GeoTIFF.

        Pipeline: project bbox to native SR -> tile the request grid under the
        server limit -> exportImage each tile at native 3 ft/px -> stitch with
        gdalbuildvrt -> apply the feet->metres ScaleRatio in a VRT -> gdalwarp
        to EPSG:4326 clipped to the exact bbox.

        Returns:
            Path to a GeoTIFF in EPSG:4326 with elevation in METRES.
        """
        output_dir.mkdir(parents=True, exist_ok=True)

        output_path = output_dir / self._output_filename(bbox)
        if output_path.exists():
            if progress_cb:
                progress_cb(f"Cached: {output_path.name}")
            return output_path

        # 1. Project the WGS84 bbox into the native SR and snap outward to the
        #    native 3 ft grid, so requested cell size is exactly native.
        nx_min, ny_min, nx_max, ny_max = _reproject_bbox(
            bbox, 4326, _NATIVE_EPSG
        )
        nx_min = math.floor(nx_min / _NATIVE_PIXEL_FT) * _NATIVE_PIXEL_FT
        ny_min = math.floor(ny_min / _NATIVE_PIXEL_FT) * _NATIVE_PIXEL_FT
        nx_max = math.ceil(nx_max / _NATIVE_PIXEL_FT) * _NATIVE_PIXEL_FT
        ny_max = math.ceil(ny_max / _NATIVE_PIXEL_FT) * _NATIVE_PIXEL_FT

        total_w = int(round((nx_max - nx_min) / _NATIVE_PIXEL_FT))
        total_h = int(round((ny_max - ny_min) / _NATIVE_PIXEL_FT))
        if total_w < 1 or total_h < 1:
            raise ValueError(f"Bbox {bbox} is too small to sample at 3 ft.")

        cols = math.ceil(total_w / _MAX_TILE_PX)
        rows = math.ceil(total_h / _MAX_TILE_PX)

        if progress_cb:
            progress_cb(f"DOGAMI DTM mosaic (EPSG:{_NATIVE_EPSG}, 3 ft native)")
            progress_cb(
                f"  {total_w}x{total_h}px at 3 ft/px (~0.914 m) "
                f"-> {cols}x{rows} tile grid ({cols * rows} request(s))"
            )

        # 2. Fetch each tile at exactly native cell size.
        tile_dir = output_dir / "_tiles"
        tile_dir.mkdir(parents=True, exist_ok=True)
        tile_vrts = self._fetch_tiles(
            nx_min, ny_min, total_w, total_h, cols, rows, tile_dir, progress_cb
        )

        try:
            # 3. Stitch the tiles into one virtual mosaic.
            mosaic_vrt = tile_dir / "_mosaic.vrt"
            _run(
                [
                    "gdalbuildvrt",
                    "-overwrite",
                    "-resolution", "highest",
                    "-srcnodata", repr(_ARCGIS_NODATA),
                    "-vrtnodata", repr(_OUT_NODATA),
                    str(mosaic_vrt),
                ]
                + [str(p) for p in tile_vrts]
            )

            # 4. Feet -> metres. THE critical step for this source.
            if progress_cb:
                progress_cb("  Converting NAVD88 feet -> metres (x0.3048)...")
            metres_vrt = tile_dir / "_metres.vrt"
            _apply_scale_ratio(mosaic_vrt, metres_vrt, _FT_TO_M)

            # 5. Reproject to EPSG:4326 and clip to the exact requested bbox.
            if progress_cb:
                progress_cb("  Reprojecting to EPSG:4326 and clipping...")
            tmp_out = output_dir / (output_path.name + ".tmp.tif")
            cmd = [
                "gdalwarp",
                "-overwrite",
                "-s_srs", f"EPSG:{_NATIVE_EPSG}",
                "-t_srs", "EPSG:4326",
                "-te", str(bbox.west), str(bbox.south),
                       str(bbox.east), str(bbox.north),
                "-te_srs", "EPSG:4326",
                "-r", "bilinear",
                "-ot", "Float32",
                "-srcnodata", repr(_OUT_NODATA),
                "-dstnodata", repr(_OUT_NODATA),
                "-co", "COMPRESS=DEFLATE",
                "-co", "TILED=YES",
                "-co", "BIGTIFF=IF_SAFER",
            ]
            # Always pin the output grid. Left to its own devices gdalwarp
            # picks a degree-square cell from an approximate transformer, which
            # here came out at 1.10 m north-south -- coarser than the 0.914 m
            # native data we just paid to download.
            deg = _metres_to_degrees(
                self.output_resolution_m or _NATIVE_PIXEL_M
            )
            cmd += ["-tr", str(deg), str(deg)]
            cmd += [str(metres_vrt), str(tmp_out)]
            _run(cmd)
            tmp_out.replace(output_path)
        finally:
            _cleanup(tile_dir)

        if progress_cb:
            size_mb = output_path.stat().st_size / (1024 * 1024)
            progress_cb(f"Done: {output_path.name} ({size_mb:.1f} MB, metres)")

        return output_path

    # -- internals ----------------------------------------------------------

    def _fetch_tiles(
        self,
        x_min: float,
        y_min: float,
        total_w: int,
        total_h: int,
        cols: int,
        rows: int,
        tile_dir: Path,
        progress_cb=None,
    ) -> List[Path]:
        """exportImage each tile of the grid at native 3 ft/px.

        Tiles are cut in *pixel* space and converted to native-SR coordinates,
        so every request has size = extent / 3 ft exactly. That keeps the
        mosaic on its Category=1 source rasters instead of the Ov_i02_*
        pyramid overviews a coarser cell size would select.
        """
        out: List[Path] = []
        total = cols * rows
        n = 0

        for row in range(rows):
            # Pixel rows are counted from the top (north) edge.
            py0 = row * _MAX_TILE_PX
            py1 = min(py0 + _MAX_TILE_PX, total_h)
            t_north = y_min + (total_h - py0) * _NATIVE_PIXEL_FT
            t_south = y_min + (total_h - py1) * _NATIVE_PIXEL_FT
            t_h = py1 - py0

            for col in range(cols):
                px0 = col * _MAX_TILE_PX
                px1 = min(px0 + _MAX_TILE_PX, total_w)
                t_west = x_min + px0 * _NATIVE_PIXEL_FT
                t_east = x_min + px1 * _NATIVE_PIXEL_FT
                t_w = px1 - px0

                n += 1
                tif = tile_dir / f"_dogami_{row}_{col}.tif"
                if progress_cb:
                    progress_cb(f"  [{n}/{total}] tile ({col},{row}) {t_w}x{t_h}px ...")

                url = _export_url(t_west, t_south, t_east, t_north, t_w, t_h)
                _fetch_tiff(url, tif, label=f"({col},{row})")

                # Force exact georeferencing from the extent we requested.
                # ArcGIS tiff responses are not reliably tagged with the
                # native SR, and we know the corners precisely.
                vrt = tif.with_suffix(".vrt")
                _run([
                    "gdal_translate", "-q", "-of", "VRT",
                    "-a_srs", f"EPSG:{_NATIVE_EPSG}",
                    "-a_ullr", str(t_west), str(t_north), str(t_east), str(t_south),
                    "-a_nodata", repr(_ARCGIS_NODATA),
                    str(tif), str(vrt),
                ])
                out.append(vrt)

        return out

    def _output_filename(self, bbox: BBox) -> str:
        return (
            f"dogami_dtm_{bbox.west:.4f}_{bbox.south:.4f}"
            f"_{bbox.east:.4f}_{bbox.north:.4f}.tif"
        )


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _export_url(
    west: float, south: float, east: float, north: float,
    width: int, height: int,
) -> str:
    """Build the ImageServer exportImage URL for a native-SR extent.

    bboxSR/imageSR are plain WKID integers -- JSON-encoded spatialReference
    objects make some ArcGIS endpoints return an HTML error page.
    """
    params = urllib.parse.urlencode({
        "bbox": f"{west},{south},{east},{north}",
        "bboxSR": _NATIVE_EPSG,
        "imageSR": _NATIVE_EPSG,
        "size": f"{width},{height}",
        "format": "tiff",
        "pixelType": "F32",
        "noDataInterpretation": "esriNoDataMatchAny",
        "interpolation": "RSP_BilinearInterpolation",
        "f": "image",
    })
    return f"{_IMAGESERVER}/exportImage?{params}"


def _fetch_tiff(url: str, dest: Path, label: str = "") -> None:
    """Download one exportImage response, retrying on 5xx / transport errors.

    curl rather than urllib: the endpoint is slow to first byte on large
    tiles and is friendlier to a curl User-Agent.
    """
    # The tile cache is shared by concurrent workers building overlapping
    # boxes, so a tile may already have been completed by someone else.
    if dest.exists() and dest.stat().st_size > 0:
        try:
            with open(dest, "rb") as fh:
                if fh.read(2) in (b"II", b"MM"):
                    return
        except OSError:
            pass

    # Download to a pid-unique temp file and move it into place atomically.
    # Writing straight to dest let a second worker read a half-written tile
    # ("TIFFReadEncodedTile ... got 20107 bytes, expected 65536"), and the
    # old unconditional dest.unlink() on failure deleted tiles another
    # worker was about to open (FileNotFoundError). Both took down adjacent
    # overlapping boxes hours into a run.
    tmp = dest.with_name(f"{dest.name}.{os.getpid()}.part")
    last = ""
    try:
        for attempt in range(1, _HTTP_RETRIES + 1):
            proc = subprocess.run(
                [
                    "curl", "-sS",
                    "--max-time", "900",
                    "--connect-timeout", "30",
                    "-w", "%{http_code}",
                    "-o", str(tmp),
                    url,
                ],
                capture_output=True, text=True,
            )
            code = (proc.stdout or "").strip()[-3:]
            if proc.returncode == 0 and code == "200":
                # The server answers bad requests with a 200 HTML/JSON error page,
                # so sniff the magic bytes before trusting it.
                with open(tmp, "rb") as fh:
                    magic = fh.read(2)
                if magic in (b"II", b"MM"):
                    os.replace(tmp, dest)
                    return
                snippet = tmp.read_text(errors="replace")[:300]
                raise RuntimeError(
                    f"DOGAMI tile {label} returned non-TIFF response:\n{snippet}"
                )

            last = f"exit={proc.returncode} http={code} {proc.stderr[:200]}"
            retryable = proc.returncode != 0 or (code.isdigit() and int(code) >= 500)
            if not retryable or attempt == _HTTP_RETRIES:
                break
            time.sleep(_HTTP_BACKOFF * attempt)

        raise RuntimeError(
            f"DOGAMI tile {label} failed after {_HTTP_RETRIES} tries: {last}"
        )
    finally:
        tmp.unlink(missing_ok=True)


def _apply_scale_ratio(src_vrt: Path, dst_vrt: Path, ratio: float) -> None:
    """Write a copy of src_vrt whose sources multiply pixel values by ratio.

    VRT <ComplexSource><ScaleRatio> is applied by GDAL on read, in C, with no
    numpy involvement -- unlike gdal_calc.py. <NODATA> inside the source is
    honoured *before* scaling, so nodata pixels stay nodata rather than being
    silently rescaled into plausible-looking elevations.
    """
    xml = src_vrt.read_text()

    # gdalbuildvrt emits ComplexSource when -srcnodata is given, but promote
    # SimpleSource too so this is correct either way.
    xml = xml.replace("<SimpleSource", "<ComplexSource").replace(
        "</SimpleSource>", "</ComplexSource>"
    )

    if "<ScaleRatio>" in xml:
        raise RuntimeError(f"{src_vrt} already carries a ScaleRatio")

    xml, count = re.subn(
        r"</ComplexSource>",
        f"  <ScaleRatio>{ratio}</ScaleRatio>\n    </ComplexSource>",
        xml,
    )
    if count == 0:
        raise RuntimeError(f"No raster sources found in {src_vrt}")

    dst_vrt.write_text(xml)


def _reproject_bbox(
    bbox: BBox, src_epsg: int, dst_epsg: int
) -> Tuple[float, float, float, float]:
    """Project a bbox between CRSs via gdaltransform, densifying the edges.

    Corners alone are not enough for a Lambert Conformal Conic projection --
    the north and south edges bow -- so sample along every edge and take the
    envelope of the results.
    """
    pts = []
    steps = 16
    for i in range(steps + 1):
        fx = bbox.west + (bbox.east - bbox.west) * i / steps
        fy = bbox.south + (bbox.north - bbox.south) * i / steps
        pts.append((fx, bbox.south))
        pts.append((fx, bbox.north))
        pts.append((bbox.west, fy))
        pts.append((bbox.east, fy))

    proc = _run(
        ["gdaltransform", "-s_srs", f"EPSG:{src_epsg}", "-t_srs", f"EPSG:{dst_epsg}"],
        stdin="\n".join(f"{x} {y}" for x, y in pts),
    )

    xs, ys = [], []
    for line in proc.stdout.strip().splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        xs.append(float(parts[0]))
        ys.append(float(parts[1]))
    if not xs:
        raise RuntimeError("gdaltransform returned no coordinates")

    return (min(xs), min(ys), max(xs), max(ys))


def _metres_to_degrees(res_m: float) -> float:
    """Degrees per pixel for a metre cell size, as a square EPSG:4326 cell.

    Sized off latitude (the axis where a degree is shortest in this hemisphere
    of the problem), so the same value used on both axes slightly oversamples
    east-west rather than throwing away native detail north-south.
    """
    return res_m / 111_320.0


def _run(cmd: List[str], stdin: Optional[str] = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, input=stdin, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {proc.stderr.strip()[:500]}")
    return proc


def _cleanup(tile_dir: Path) -> None:
    """Remove intermediate tiles/VRTs; never fatal."""
    try:
        for p in tile_dir.glob("_*"):
            p.unlink(missing_ok=True)
        tile_dir.rmdir()
    except OSError:
        pass
