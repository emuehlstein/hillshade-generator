"""ISGS Illinois Height Modernization Program (ILHMP) DEM source.

Downloads 1ft (~0.3m) bare-earth DTM or surface DSM data for any Illinois
county from the ISGS ArcGIS ImageServer, using exportImage to pull only
the requested bounding box — no need to download full county ZIPs (40–140 GB).

Service base:
    https://data.isgs.illinois.edu/arcgis/rest/services/Elevation/

Each county has a service named IL_{County}_{DTM|DSM}_{year}/ImageServer.
County-to-service mapping is imported from illinois-hillshade-gen (ilhmp)
when available; falls back to a minimal built-in table for common counties.

Resolution: 1 US survey foot ≈ 0.3048 m
Coverage:   Illinois only (approx. -91.5,36.97,-87.02,42.51)
Priority:   88 (finer than WI DNR 1m, coarser than Indiana IGIC)
"""

import json
import math
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .base import BBox

# Illinois bounding box (with small buffer)
_IL_WEST  = -91.6
_IL_EAST  = -86.9
_IL_SOUTH =  36.9
_IL_NORTH =  42.6

# ISGS ImageServer practical limits (advertised: 15000×4100, actual: ~3500×2500)
# The service returns an HTML error page — not a curl error — for larger requests.
_MAX_WIDTH  = 3500
_MAX_HEIGHT = 2500

# ISGS base URL
_IMAGESERVER_BASE = "https://data.isgs.illinois.edu/arcgis/rest/services/Elevation"

# Fallback county→service table for the most commonly used counties.
# Keys are lowercase county names; values are (dtm_service, dsm_service, year).
# The full 102-county table lives in illinois-hillshade-gen; we import it when
# available and merge it on top of this table.
_FALLBACK_COUNTIES: Dict[str, Tuple[str, str, str]] = {
    "cook":     ("IL_Cook_DTM_2022",     "IL_Cook_DSM_2022",     "2022"),
    "lasalle":  ("IL_LaSalle_DTM_2017",  "IL_LaSalle_DSM_2017",  "2017"),
    "mchenry":  ("IL_McHenry_DTM_2018",  "IL_McHenry_DSM_2018",  "2018"),
    "lake":     ("IL_Lake_DTM_2019",     "IL_Lake_DSM_2019",     "2019"),
    "dupage":   ("IL_DuPage_DTM_2017",   "IL_DuPage_DSM_2017",   "2017"),
    "kane":     ("IL_Kane_DTM_2018",     "IL_Kane_DSM_2018",     "2018"),
    "kendall":  ("IL_Kendall_DTM_2018",  "IL_Kendall_DSM_2018",  "2018"),
    "will":     ("IL_Will_DTM_2018",     "IL_Will_DSM_2018",     "2018"),
    "grundy":   ("IL_Grundy_DTM_2018",   "IL_Grundy_DSM_2018",   "2018"),
    "putnam":   ("IL_Putnam_DTM_2022",   "IL_Putnam_DSM_2022",   "2022"),
    "bureau":   ("IL_Bureau_DTM_2023",   "IL_Bureau_DSM_2023",   "2023"),
    "dekalb":   ("IL_DeKalb_DTM_2018",   "IL_DeKalb_DSM_2018",   "2018"),
    "boone":    ("IL_Boone_DTM_2018",    "IL_Boone_DSM_2018",    "2018"),
    "winnebago":("IL_Winnebago_DTM_2018","IL_Winnebago_DSM_2018","2018"),
    "ogle":     ("IL_Ogle_DTM_2018",     "IL_Ogle_DSM_2018",     "2018"),
    "lee":      ("IL_Lee_DTM_2018",      "IL_Lee_DSM_2018",      "2018"),
    "whiteside":("IL_Whiteside_DTM_2018","IL_Whiteside_DSM_2018","2018"),
}


# Approximate county centroids (lat, lon) for fast point-in-county lookup.
# Derived from US Census TIGER/Line data for Illinois (FIPS 17).
_IL_COUNTY_CENTROIDS: Dict[str, Tuple[float, float]] = {
    "adams":      (39.998, -91.189),
    "alexander":  (37.179, -89.339),
    "bond":       (38.882, -89.434),
    "boone":      (42.326, -88.822),
    "brown":      (39.955, -90.745),
    "bureau":     (41.401, -89.524),
    "calhoun":    (39.164, -90.655),
    "carroll":    (42.068, -89.942),
    "cass":       (39.973, -90.246),
    "champaign":  (40.140, -88.198),
    "christian":  (39.545, -89.270),
    "clark":      (39.333, -87.779),
    "clay":       (38.752, -88.480),
    "clinton":    (38.592, -89.424),
    "coles":      (39.524, -88.220),
    "cook":       (41.840, -87.817),
    "crawford":   (39.001, -87.752),
    "cumberland": (39.274, -88.237),
    "dekalb":     (41.895, -88.763),
    "dewitt":     (40.180, -88.883),
    "douglas":    (39.763, -88.222),
    "dupage":     (41.851, -88.084),
    "edgar":      (39.673, -87.748),
    "edwards":    (38.413, -88.082),
    "effingham":  (39.071, -88.572),
    "fayette":    (38.993, -89.022),
    "ford":       (40.591, -88.228),
    "franklin":   (37.993, -88.928),
    "fulton":     (40.468, -90.200),
    "gallatin":   (37.758, -88.218),
    "greene":     (39.353, -90.367),
    "grundy":     (41.260, -88.408),
    "hamilton":   (38.082, -88.542),
    "hancock":    (40.402, -91.149),
    "hardin":     (37.517, -88.258),
    "henderson":  (40.811, -90.944),
    "henry":      (41.349, -90.122),
    "iroquois":   (40.748, -87.826),
    "jackson":    (37.779, -89.381),
    "jasper":     (38.987, -88.168),
    "jefferson":  (38.297, -88.927),
    "jersey":     (39.090, -90.342),
    "jodaviess":  (42.349, -90.157),
    "johnson":    (37.450, -88.878),
    "kane":       (41.939, -88.432),
    "kankakee":   (41.123, -87.862),
    "kendall":    (41.590, -88.427),
    "knox":       (40.930, -90.217),
    "lake":       (42.278, -87.876),
    "lasalle":    (41.338, -88.887),
    "lawrence":   (38.716, -87.728),
    "lee":        (41.748, -89.297),
    "livingston": (40.893, -88.559),
    "logan":      (40.130, -89.367),
    "macon":      (39.863, -88.964),
    "macoupin":   (39.262, -89.926),
    "madison":    (38.844, -89.924),
    "marion":     (38.651, -88.918),
    "marshall":   (41.029, -89.353),
    "mason":      (40.232, -89.919),
    "massac":     (37.217, -88.709),
    "mcdonough":  (40.458, -90.676),
    "mchenry":    (42.324, -88.448),
    "mclean":     (40.494, -88.834),
    "menard":     (40.026, -89.797),
    "mercer":     (41.199, -90.745),
    "monroe":     (38.271, -90.173),
    "montgomery": (39.230, -89.467),
    "morgan":     (39.716, -90.190),
    "moultrie":   (39.636, -88.621),
    "ogle":       (41.991, -89.322),
    "peoria":     (40.795, -89.757),
    "perry":      (38.083, -89.374),
    "piatt":      (40.008, -88.535),
    "pike":       (39.622, -90.879),
    "pope":       (37.400, -88.560),
    "pulaski":    (37.219, -89.093),
    "putnam":     (41.199, -89.283),
    "randolph":   (38.053, -89.820),
    "richland":   (38.706, -88.081),
    "rockisland": (41.455, -90.581),
    "saline":     (37.751, -88.549),
    "sangamon":   (39.756, -89.652),
    "schuyler":   (40.154, -90.609),
    "scott":      (39.642, -90.471),
    "shelby":     (39.399, -88.786),
    "stclair":    (38.469, -90.007),
    "stark":      (41.085, -89.798),
    "stephenson": (42.348, -89.662),
    "tazewell":   (40.509, -89.514),
    "union":      (37.463, -89.245),
    "vermilion":  (40.183, -87.734),
    "wabash":     (38.439, -87.844),
    "warren":     (40.850, -90.617),
    "washington": (38.346, -89.407),
    "wayne":      (38.436, -88.426),
    "white":      (38.087, -88.168),
    "whiteside":  (41.752, -89.916),
    "will":       (41.445, -87.981),
    "williamson": (37.723, -88.921),
    "winnebago":  (42.337, -89.161),
    "woodford":   (40.791, -89.216),
}


def _build_county_map() -> Dict[str, Tuple[str, str, str]]:
    """Build county→(dtm_svc, dsm_svc, year) map.

    Prefers the full 102-county table from ilhmp (illinois-hillshade-gen)
    when installed; falls back to _FALLBACK_COUNTIES.
    """
    table = dict(_FALLBACK_COUNTIES)
    try:
        from ilhmp import counties as ilhmp_counties  # type: ignore
        for key, county in ilhmp_counties.COUNTIES.items():
            colls = county.get("collections", [])
            if not colls:
                continue
            latest = colls[0]
            dtm_svc = latest.get("dtm_imageserver")
            dsm_svc = latest.get("dsm_imageserver")
            year    = latest.get("year", "")
            if dtm_svc:
                table[key] = (dtm_svc, dsm_svc or dtm_svc, year)
    except ImportError:
        pass
    return table


class ISGSILHMPSource:
    """ISGS ILHMP 1ft DTM/DSM for Illinois via ArcGIS ImageServer (exportImage)."""

    name = "isgs-ilhmp"
    description = (
        "ISGS Illinois Height Modernization Program (ILHMP) 1ft (~0.3m) DTM, "
        "all 102 Illinois counties via ArcGIS ImageServer bbox export"
    )
    resolution_m = 0.3
    priority = 88

    def __init__(self, dem_type: str = "dtm"):
        """
        Args:
            dem_type: 'dtm' (bare-earth, default) or 'dsm' (surface with buildings/trees)
        """
        self.dem_type = dem_type.lower()
        self._county_map: Optional[Dict[str, Tuple[str, str, str]]] = None

    @property
    def county_map(self) -> Dict[str, Tuple[str, str, str]]:
        if self._county_map is None:
            self._county_map = _build_county_map()
        return self._county_map

    def covers(self, bbox: BBox) -> bool:
        """Returns True if the bbox overlaps Illinois."""
        return (
            bbox.west  < _IL_EAST
            and bbox.east  > _IL_WEST
            and bbox.south < _IL_NORTH
            and bbox.north > _IL_SOUTH
        )

    def _county_for_bbox(self, bbox: BBox) -> Optional[str]:
        """Return the lowercase county key whose service covers the bbox center.

        Uses a precomputed centroid table (no network calls) to find which
        county contains the bbox centroid. Picks the closest county centroid
        as a fast approximation — good enough for any single-county bbox.
        """
        cx = (bbox.west + bbox.east) / 2
        cy = (bbox.south + bbox.north) / 2

        best_key = None
        best_dist = float("inf")
        for key, (lat, lon) in _IL_COUNTY_CENTROIDS.items():
            if key not in self.county_map:
                continue
            dist = (lat - cy) ** 2 + (lon - cx) ** 2
            if dist < best_dist:
                best_dist = dist
                best_key = key
        return best_key

    def service_for_bbox(self, bbox: BBox) -> str:
        """Return the ImageServer service name that best covers this bbox."""
        county = self._county_for_bbox(bbox)
        if county and county in self.county_map:
            dtm_svc, dsm_svc, _ = self.county_map[county]
            return dtm_svc if self.dem_type == "dtm" else dsm_svc
        raise ValueError(
            f"No ILHMP service found for bbox "
            f"({bbox.west:.3f},{bbox.south:.3f},{bbox.east:.3f},{bbox.north:.3f}). "
            f"Is it in Illinois? Try --source usgs-3dep-10m for non-IL areas."
        )

    def download(self, bbox: BBox, output_dir: Path, progress_cb=None) -> Path:
        """Download ILHMP DEM for bbox via ImageServer exportImage.

        Tiles the request to respect server limits (15000×4100 px/call),
        downloads each tile sequentially, merges with gdal_merge.py, and
        clips to the exact bbox.

        Returns path to the merged, clipped GeoTIFF (WGS84 / EPSG:4326, F32).
        """
        output_dir.mkdir(parents=True, exist_ok=True)

        # Resolve service
        svc_name = self.service_for_bbox(bbox)
        base_url = f"{_IMAGESERVER_BASE}/{svc_name}/ImageServer"

        if progress_cb:
            progress_cb(f"ISGS ImageServer: {svc_name}")

        # Pixel size: ~1ft at mid-latitude
        mid_lat = (bbox.south + bbox.north) / 2
        pixel_size = 0.3048 / (111_320 * math.cos(math.radians(mid_lat)))

        # Tile grid
        lon_range = bbox.east  - bbox.west
        lat_range = bbox.north - bbox.south
        total_w = int(math.ceil(lon_range / pixel_size))
        total_h = int(math.ceil(lat_range / pixel_size))
        cols = math.ceil(total_w / _MAX_WIDTH)
        rows = math.ceil(total_h / _MAX_HEIGHT)

        if progress_cb:
            progress_cb(
                f"  {total_w}×{total_h}px at ~{pixel_size*111320:.2f}m/px "
                f"→ {cols}×{rows} tile grid"
            )

        # Download tiles
        tile_paths = _download_tiles(
            base_url=base_url,
            bbox=bbox,
            cols=cols, rows=rows,
            pixel_size=pixel_size,
            work_dir=output_dir,
            progress_cb=progress_cb,
        )

        # Merge
        output_name = (
            f"ilhmp_{self.dem_type}_"
            f"{bbox.west:.4f},{bbox.south:.4f},{bbox.east:.4f},{bbox.north:.4f}.tif"
            .replace(",", "_")
        )
        output_path = output_dir / output_name

        if len(tile_paths) == 1:
            tile_paths[0].replace(output_path)
        else:
            if progress_cb:
                progress_cb(f"Merging {len(tile_paths)} tiles...")
            _merge_tiles(tile_paths, output_path)
            for p in tile_paths:
                p.unlink(missing_ok=True)

        size_mb = output_path.stat().st_size / (1024 * 1024)
        if progress_cb:
            progress_cb(f"Done: {output_path.name} ({size_mb:.1f} MB)")

        return output_path


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _export_url(
    base_url: str,
    west: float, south: float, east: float, north: float,
    width: int, height: int,
) -> str:
    """Build an ArcGIS ImageServer exportImage URL for a WGS84 bbox.

    Use plain WKID integers for bboxSR/imageSR — JSON-encoded SR objects
    cause some ArcGIS endpoints to return an HTML error page instead of the image.
    """
    params = urllib.parse.urlencode({
        "bbox":                  f"{west},{south},{east},{north}",
        "bboxSR":                4326,
        "size":                  f"{width},{height}",
        "imageSR":               4326,
        "format":                "tiff",
        "pixelType":             "F32",
        "noDataInterpretation":  "esriNoDataMatchAny",
        "interpolation":         "RSP_BilinearInterpolation",
        "f":                     "image",
    })
    return f"{base_url}/exportImage?{params}"


def _service_extent_wgs84(base_url: str) -> Tuple[float, float, float, float]:
    """Query /info?f=json and return (west, south, east, north) in WGS84."""
    url = f"{base_url}?f=json"
    req = urllib.request.Request(url, headers={"User-Agent": "curl/7.88.1"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        info = json.load(resp)

    ext    = info.get("extent", {})
    sr_wkt = (ext.get("spatialReference") or {}).get("wkt", "")
    xmin, ymin = ext["xmin"], ext["ymin"]
    xmax, ymax = ext["xmax"], ext["ymax"]

    corners = [(xmin, ymin), (xmin, ymax), (xmax, ymin), (xmax, ymax)]
    input_text = "\n".join(f"{x} {y}" for x, y in corners)
    cmd = ["gdaltransform", "-s_srs", sr_wkt, "-t_srs", "EPSG:4326"]
    r = subprocess.run(cmd, input=input_text, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gdaltransform failed: {r.stderr}")

    lons, lats = [], []
    for line in r.stdout.strip().splitlines():
        parts = line.split()
        lons.append(float(parts[0]))
        lats.append(float(parts[1]))

    return (min(lons), min(lats), max(lons), max(lats))


def _download_tiles(
    base_url: str,
    bbox: BBox,
    cols: int,
    rows: int,
    pixel_size: float,
    work_dir: Path,
    progress_cb=None,
) -> List[Path]:
    """Download all tiles for the bbox grid, return list of Paths."""
    tile_paths: List[Path] = []
    total = cols * rows
    n = 0

    lon_step = (bbox.east  - bbox.west)  / cols
    lat_step = (bbox.north - bbox.south) / rows

    for row in range(rows):
        for col in range(cols):
            t_west  = bbox.west  + col * lon_step
            t_east  = min(bbox.west  + (col + 1) * lon_step, bbox.east)
            t_north = bbox.north - row * lat_step
            t_south = max(bbox.north - (row + 1) * lat_step, bbox.south)

            t_w = min(int(math.ceil((t_east  - t_west)  / pixel_size)), _MAX_WIDTH)
            t_h = min(int(math.ceil((t_north - t_south) / pixel_size)), _MAX_HEIGHT)

            n += 1
            tile_path = work_dir / f"_ilhmp_tile_{row}_{col}.tif"
            url = _export_url(base_url, t_west, t_south, t_east, t_north, t_w, t_h)

            if progress_cb:
                progress_cb(f"  [{n}/{total}] tile ({col},{row}) {t_w}×{t_h}px ...")

            # Use curl: ISGS server is slow to start responding (60-90s for
            # large tiles) and rejects Python's default User-Agent with HTML.
            # curl handles both gracefully.
            curl_cmd = [
                "curl", "-fsSL",
                "--max-time", "600",
                "-o", str(tile_path),
                url,
            ]
            r = subprocess.run(curl_cmd, capture_output=True, text=True)
            if r.returncode != 0:
                raise RuntimeError(
                    f"ILHMP tile ({col},{row}) curl failed (exit {r.returncode}): {r.stderr[:200]}"
                )

            # Validate — server returns HTML error pages on bad requests
            with open(tile_path, "rb") as fh:
                magic = fh.read(4)
            if magic[:2] not in (b"II", b"MM"):
                snippet = tile_path.read_text(errors="replace")[:300]
                raise RuntimeError(
                    f"ILHMP tile ({col},{row}) returned non-TIFF response:\n{snippet}"
                )

            tile_paths.append(tile_path)

    return tile_paths


def _merge_tiles(tile_paths: List[Path], output_path: Path) -> None:
    """Merge downloaded tiles into a single GeoTIFF using gdal_merge.py."""
    cmd = [
        "gdal_merge.py",
        "-o", str(output_path),
        "-of", "GTiff",
        "-co", "COMPRESS=DEFLATE",
        "-co", "TILED=YES",
        "-co", "BIGTIFF=IF_SAFER",
        "-n", "3.4e+38",
        "-a_nodata", "nan",
    ] + [str(p) for p in tile_paths]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gdal_merge.py failed: {r.stderr}")
