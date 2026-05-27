"""
Hillgen CLI

Usage:
    hillgen version
    hillgen fetch --bbox "-87.70,41.96,-87.66,41.99" --dem usgs-3dep-10m
    hillgen shade --bbox "-87.70,41.96,-87.66,41.99" --exaggeration 9
    hillgen style --bbox "..." --theme midnight
    hillgen run --place "Crater Lake" --theme midnight
"""

import math
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import click

from . import __version__


# ── Run estimate / sanity check ────────────────────────────────────────────────

# Empirical tile-size medians by zoom level (PNG, hillshade)
# Values calibrated from Cook County 3DEP + La Porte LiDAR runs
_TILE_KB = {
    10: 4, 11: 8, 12: 16, 13: 30, 14: 55, 15: 75, 16: 88, 17: 95, 18: 100, 19: 105, 20: 110,
}

# Rough DEM download size per square degree by source
_DEM_MB_PER_SQ_DEG = {
    "usgs-3dep-10m": 40,
    "igic-indiana-lidar": 500,   # 16MB tiles, ~5000ft x 5000ft, ~30 tiles/deg²
    "wi-dnr-lidar":      400,
    "nps-sfm-rainier-2021": 800,
}
_DEM_MB_PER_SQ_DEG_DEFAULT = 50

# Rough processing time constants (seconds)
_SHADE_SEC_PER_SQ_DEG = {"usgs-3dep-10m": 5, "igic-indiana-lidar": 30}
_SHADE_SEC_DEFAULT = 8
_TILE_SEC_PER_TILE = 0.45


def _count_tiles(west: float, south: float, east: float, north: float,
                 min_zoom: int, max_zoom: int) -> int:
    """Estimate XYZ tile count for a bounding box and zoom range."""
    total = 0
    for z in range(min_zoom, max_zoom + 1):
        n = 2 ** z
        x0 = int((west + 180) / 360 * n)
        x1 = int((east + 180) / 360 * n)
        y0 = int((1 - math.log(math.tan(math.radians(north)) + 1 / math.cos(math.radians(north))) / math.pi) / 2 * n)
        y1 = int((1 - math.log(math.tan(math.radians(south)) + 1 / math.cos(math.radians(south))) / math.pi) / 2 * n)
        total += max(1, (x1 - x0 + 1)) * max(1, (y1 - y0 + 1))
    return total


def _format_time(seconds: float) -> str:
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds / 60)}m"
    h = int(seconds / 3600)
    m = int((seconds % 3600) / 60)
    return f"{h}h {m}m" if m else f"{h}h"


def _format_size(mb: float) -> str:
    if mb >= 1024:
        return f"{mb / 1024:.1f} GB"
    return f"{int(mb)} MB"


def estimate_run(
    bbox,           # BBox namedtuple with .west .south .east .north
    dem: str,
    zoom: str,      # e.g. "10-16"
    dem_cached: bool = False,
    styled_cached: bool = False,
) -> dict:
    """Return a dict of estimates for a hillgen run."""
    from .pipeline.tiler import parse_zoom
    min_z, max_z = parse_zoom(zoom)

    w, s, e, n = bbox.west, bbox.south, bbox.east, bbox.north
    area_sq_deg = abs(e - w) * abs(n - s)

    # Tile count
    tile_count = _count_tiles(w, s, e, n, min_z, max_z)

    # MBTiles size estimate
    size_kb = sum(
        _count_tiles(w, s, e, n, z, z) * _TILE_KB.get(z, 100)
        for z in range(min_z, max_z + 1)
    )
    size_mb = size_kb / 1024

    # DEM download size
    mb_per_sq = _DEM_MB_PER_SQ_DEG.get(dem, _DEM_MB_PER_SQ_DEG_DEFAULT)
    dem_mb = area_sq_deg * mb_per_sq

    # Time estimate
    shade_sec = 0 if styled_cached else area_sq_deg * _SHADE_SEC_PER_SQ_DEG.get(dem, _SHADE_SEC_DEFAULT)
    tile_sec = 0 if styled_cached else tile_count * _TILE_SEC_PER_TILE
    total_sec = shade_sec + tile_sec

    return {
        "tile_count": tile_count,
        "size_mb": size_mb,
        "dem_mb": dem_mb,
        "total_sec": total_sec,
        "min_zoom": min_z,
        "max_zoom": max_z,
        "area_sq_deg": area_sq_deg,
        "width_deg": abs(e - w),
        "height_deg": abs(n - s),
    }


def print_estimate(est: dict, dem: str, theme: str, exaggeration: Optional[float]) -> None:
    """Pretty-print the run estimate."""
    click.echo("")
    click.echo("┌─ Run estimate ─────────────────────────────────────────────────")
    click.echo(f"│  Source:      {dem}")
    click.echo(f"│  Theme:       {theme}" + (f"  {exaggeration}x exaggeration" if exaggeration else ""))
    click.echo(f"│  Zoom:        z{est['min_zoom']}–{est['max_zoom']}")
    click.echo(f"│  Area:        {est['width_deg']:.2f}° × {est['height_deg']:.2f}°  ({est['area_sq_deg']:.4f} sq°)")
    click.echo(f"│  Tiles:       ~{est['tile_count']:,}")
    click.echo(f"│  Output:      ~{_format_size(est['size_mb'])} MBTiles/PMTiles")
    click.echo(f"│  DEM:         ~{_format_size(est['dem_mb'])} download")
    click.echo(f"│  Time:        ~{_format_time(est['total_sec'])}")
    click.echo("└─────────────────────────────────────────────────────")


# Thresholds for warnings / confirmation prompts
_WARN_TILES = 50_000
_WARN_SIZE_MB = 500
_WARN_DEM_MB = 2_000
_WARN_TIME_SEC = 30 * 60
_BLOCK_TILES = 2_000_000   # refuse without --force


def sanity_check(
    est: dict,
    force: bool = False,
    yes: bool = False,
) -> bool:
    """
    Print warnings for large runs and prompt for confirmation when thresholds
    are exceeded.  Returns True if the run should proceed, False to abort.
    """
    warnings = []

    if est["tile_count"] > _BLOCK_TILES:
        click.echo(
            click.style(
                f"\n⛔  {est['tile_count']:,} tiles exceeds the hard limit of {_BLOCK_TILES:,}."
                " Use --max-zoom to reduce the zoom range, or a smaller --bbox.",
                fg="red", bold=True,
            )
        )
        return False

    if est["tile_count"] > _WARN_TILES:
        warnings.append(f"⚠️  {est['tile_count']:,} tiles — tiling alone will take ~{_format_time(est['tile_count'] * _TILE_SEC_PER_TILE)}")
    if est["size_mb"] > _WARN_SIZE_MB:
        warnings.append(f"⚠️  Output ~{_format_size(est['size_mb'])} — make sure you have disk space")
    if est["dem_mb"] > _WARN_DEM_MB:
        warnings.append(f"⚠️  DEM download ~{_format_size(est['dem_mb'])} — may take several minutes")
    if est["total_sec"] > _WARN_TIME_SEC:
        warnings.append(f"⚠️  Estimated time ~{_format_time(est['total_sec'])} — consider running in the background")
    if est["max_zoom"] >= 18:
        warnings.append(f"⚠️  z{est['max_zoom']} tiles are near or beyond LiDAR native resolution — interpolated pixels above z17")

    if not warnings:
        return True

    click.echo("")
    for w in warnings:
        click.echo(click.style(w, fg="yellow"))

    if force or yes:
        return True

    click.echo("")
    return click.confirm("Proceed anyway?", default=False)



@click.group()
def cli():
    """Generate beautiful, styled hillshade maps from real-world terrain data."""
    pass


@cli.command()
def version():
    """Show hillgen version and environment info."""
    click.echo(f"hillgen {__version__}")
    click.echo(f"Python {sys.version.split()[0]}")

    # GDAL version
    gdal_version = _get_gdal_version()
    if gdal_version:
        click.echo(f"GDAL   {gdal_version}")
    else:
        click.secho("GDAL   not found", fg="red")
        click.echo("  Install: brew install gdal (macOS) / apt install gdal-bin python3-gdal (Linux)")

    # rasterio
    try:
        import rasterio
        click.echo(f"Rasterio {rasterio.__version__}")
    except ImportError:
        click.secho("Rasterio not found", fg="red")

    # numpy
    try:
        import numpy
        click.echo(f"NumPy  {numpy.__version__}")
    except ImportError:
        click.secho("NumPy  not found", fg="red")

    # pmtiles CLI
    pmtiles_path = shutil.which("pmtiles")
    if pmtiles_path:
        click.echo(f"pmtiles CLI found at {pmtiles_path}")
    else:
        click.echo("pmtiles CLI not found (optional, for PMTiles conversion)")

    # Cache dir
    from .cache import get_cache_dir
    cache_dir = get_cache_dir()
    click.echo(f"Cache  {cache_dir}")


def _get_gdal_version():
    """Try to get GDAL version from osgeo bindings or CLI."""
    # Try Python bindings first
    try:
        from osgeo import gdal
        return gdal.VersionInfo()
    except ImportError:
        pass

    # Fall back to CLI
    try:
        result = subprocess.run(
            ["gdalinfo", "--version"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            # "GDAL 3.9.1, released 2024/06/28"
            return result.stdout.strip().split(",")[0].replace("GDAL ", "")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    return None


def _ensure_styled(resolved_bbox, dem, theme_name, exaggeration):
    """Run fetch → reproject → shade → style, returning the styled raster path.

    Thin Click-layer wrapper around :func:`hillgen.pipeline.orchestrator.ensure_styled`
    that translates errors into ``click.BadParameter`` and routes progress
    through ``click.echo``.
    """
    from .pipeline.orchestrator import ensure_styled

    try:
        return ensure_styled(
            resolved_bbox,
            dem,
            theme_name,
            exaggeration,
            allow_s3_pull=True,
            progress_cb=lambda msg: click.echo(msg),
        )
    except ValueError as e:
        # Most commonly: unknown theme name.
        if "theme" in str(e).lower():
            raise click.BadParameter(str(e), param_hint="--theme")
        raise


def _resolve_bbox(bbox_str, place, buffer_deg=None):
    """Resolve a BBox from --bbox or --place."""
    from .sources.base import BBox

    if bbox_str and place:
        raise click.UsageError("Specify --bbox or --place, not both")
    if not bbox_str and not place:
        raise click.UsageError("Specify --bbox or --place")

    if bbox_str:
        try:
            bbox = BBox.from_string(bbox_str)
            # Apply buffer to explicit bbox if requested
            if buffer_deg:
                bbox = BBox(
                    west=max(-180, bbox.west - buffer_deg),
                    south=max(-90, bbox.south - buffer_deg),
                    east=min(180, bbox.east + buffer_deg),
                    north=min(90, bbox.north + buffer_deg),
                )
            return bbox
        except ValueError as e:
            raise click.BadParameter(str(e), param_hint="--bbox")

    from .geo.geocoder import geocode
    buf = buffer_deg if buffer_deg is not None else 0.1  # default ~11km
    try:
        bbox = geocode(place, buffer_deg=buf)
        click.echo(f"Geocoded '{place}' → {bbox}")
        _warn_large_place_bbox(place, bbox)
        return bbox
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="--place")


# Threshold above which we hint that a geocoded place is suspiciously large.
# 0.5 sq° ≈ 6,000 km² — bigger than most counties; "Death Valley NP", "Yellowstone",
# "Olympic Peninsula" all blow past this. Issue #12.
_LARGE_PLACE_SQ_DEG = 0.5


def _warn_large_place_bbox(place: str, bbox) -> None:
    """Hint that a place name resolved to a very large bounding box.

    Helps first-time users (and agents) catch cases like ``--place "Death Valley"``
    which expands to the entire NP (~3 sq°) before the tile-count estimate makes
    that obvious. We only inform — the run estimate + sanity check still gate
    the actual work.
    """
    w = abs(bbox.east - bbox.west)
    h = abs(bbox.north - bbox.south)
    area = w * h
    if area < _LARGE_PLACE_SQ_DEG:
        return
    click.secho(
        f"ℹ️  '{place}' resolved to a large bbox "
        f"({w:.2f}° × {h:.2f}° = {area:.2f} sq°).",
        fg="cyan",
    )
    click.secho(
        "   For a sub-region, try --bbox west,south,east,north "
        "or --place \"<more specific feature>\".",
        fg="cyan",
    )

@cli.command()
@click.option("--bbox", type=str, help="Bounding box: west,south,east,north")
@click.option("--place", type=str, help="Place name (geocoded via Nominatim)")
@click.option("--dem", type=str, default="auto", help="DEM source (auto, usgs-3dep-10m, usgs-3dep-1m, copernicus-30m, srtm-30m)")
@click.option("--buffer", "buffer_deg", type=float, default=None, help="Buffer in degrees around area (default 0.1° ≈ 11km)")
def fetch(bbox, place, dem, buffer_deg):
    """Download and cache DEM data for an area."""
    resolved_bbox = _resolve_bbox(bbox, place, buffer_deg=buffer_deg)

    from .sources import resolve_source
    source = resolve_source(resolved_bbox, dem)
    click.echo(f"Source: {source.name} ({source.description})")

    from .cache import ensure_cache_dir
    cache_dir = ensure_cache_dir("dem") / source.name

    result = source.download(
        resolved_bbox, cache_dir,
        progress_cb=lambda msg: click.echo(msg),
    )

    click.echo(f"\nDEM saved to: {result}")

    # Show basic info via gdalinfo
    try:
        import subprocess as sp
        info = sp.run(["gdalinfo", "-stats", str(result)], capture_output=True, text=True, timeout=30)
        if info.returncode == 0:
            for line in info.stdout.splitlines():
                line = line.strip()
                if any(k in line for k in ["Size is", "Origin", "Pixel Size", "STATISTICS_MINIMUM", "STATISTICS_MAXIMUM"]):
                    click.echo(f"  {line}")
    except (FileNotFoundError, Exception):
        pass


@cli.command()
@click.option("--bbox", type=str, help="Bounding box: west,south,east,north")
@click.option("--place", type=str, help="Place name (geocoded via Nominatim)")
@click.option("--dem", type=str, default="auto", help="DEM source")
def reproject(bbox, place, dem):
    """Reproject a cached DEM to EPSG:4326."""
    resolved_bbox = _resolve_bbox(bbox, place)

    from .sources import resolve_source
    from .cache import ensure_cache_dir
    from .pipeline.reproject import reproject_to_4326, needs_reproject

    source = resolve_source(resolved_bbox, dem)

    # Find or fetch the DEM
    dem_dir = ensure_cache_dir("dem") / source.name
    dem_path = source.download(resolved_bbox, dem_dir, progress_cb=lambda msg: click.echo(msg))

    # Check if already EPSG:4326
    if not needs_reproject(dem_path):
        click.echo(f"Already EPSG:4326: {dem_path}")
        return

    # Reproject
    from .pipeline.orchestrator import cache_lookup
    reproj_dir = ensure_cache_dir("reprojected")
    output = reproj_dir / f"{dem_path.stem}_4326.tif"

    if cache_lookup("reprojected", output, progress_cb=lambda msg: click.echo(msg)):
        click.echo(f"Cached: {output}")
        return

    reproject_to_4326(dem_path, output, progress_cb=lambda msg: click.echo(msg))
    click.echo(f"\nReprojected: {output}")


@cli.command()
@click.option("--bbox", type=str, help="Bounding box: west,south,east,north")
@click.option("--place", type=str, help="Place name (geocoded via Nominatim)")
@click.option("--dem", type=str, default="auto", help="DEM source")
@click.option("--exaggeration", type=float, default=None, help="Vertical exaggeration factor (default: auto)")
@click.option("--shading", type=click.Choice(["standard", "multidirectional", "composite"]), default="composite")
def shade(bbox, place, dem, exaggeration, shading):
    """Generate grayscale hillshade from a reprojected DEM."""
    resolved_bbox = _resolve_bbox(bbox, place)

    from .sources import resolve_source
    from .cache import ensure_cache_dir
    from .pipeline.reproject import reproject_to_4326, needs_reproject
    from .pipeline.hillshade import generate_grayscale, generate_composite, ShadingMode

    source = resolve_source(resolved_bbox, dem)
    cb = lambda msg: click.echo(msg)

    # Step 1: ensure DEM exists
    dem_dir = ensure_cache_dir("dem") / source.name
    dem_path = source.download(resolved_bbox, dem_dir, progress_cb=cb)

    # Step 2: ensure reprojected to 4326
    from .pipeline.orchestrator import cache_lookup
    if needs_reproject(dem_path):
        reproj_dir = ensure_cache_dir("reprojected")
        reproj_path = reproj_dir / f"{dem_path.stem}_4326.tif"
        if cache_lookup("reprojected", reproj_path, progress_cb=cb):
            click.echo(f"Reproject cached: {reproj_path.name}")
        else:
            reproject_to_4326(dem_path, reproj_path, progress_cb=cb)
        input_dem = reproj_path
    else:
        input_dem = dem_path

    # Auto-exaggeration if not specified
    if exaggeration is None:
        from .pipeline.auto_exag import compute_auto_exaggeration
        exaggeration = compute_auto_exaggeration(input_dem)
        click.echo(f"Auto-exaggeration: {exaggeration}x")

    # Step 3: generate hillshade
    hs_dir = ensure_cache_dir("hillshade")

    if shading == "composite":
        output = hs_dir / f"{input_dem.stem}_gray_composite_0.6-0.3-0.1_{exaggeration}x.tif"
        if cache_lookup("hillshade", output, progress_cb=cb):
            click.echo(f"Cached: {output}")
            return
        generate_composite(
            input_dem, output, exaggeration,
            weights=(0.6, 0.3, 0.1),
            cache_dir=hs_dir,
            progress_cb=cb,
        )
    else:
        mode = ShadingMode(shading if shading != "multidirectional" else "multi")
        output = hs_dir / f"{input_dem.stem}_gray_{mode.value}_{exaggeration}x.tif"
        if cache_lookup("hillshade", output, progress_cb=cb):
            click.echo(f"Cached: {output}")
            return
        generate_grayscale(
            input_dem, output, exaggeration,
            mode=mode, progress_cb=cb,
        )

    click.echo(f"\nHillshade saved to: {output}")


@cli.command()
@click.option("--bbox", type=str, help="Bounding box: west,south,east,north")
@click.option("--place", type=str, help="Place name (geocoded via Nominatim)")
@click.option("--dem", type=str, default="auto", help="DEM source")
@click.option("--theme", "theme_name", type=str, required=True, help="Theme name or path to custom theme JSON")
@click.option("--exaggeration", type=float, default=None, help="Override theme exaggeration")
def style(bbox, place, dem, theme_name, exaggeration):
    """Apply a theme to a cached hillshade."""
    resolved_bbox = _resolve_bbox(bbox, place)
    styled_path = _ensure_styled(resolved_bbox, dem, theme_name, exaggeration)
    click.echo(f"\nStyled output: {styled_path}")


@cli.command()
@click.option("--bbox", type=str, help="Bounding box: west,south,east,north")
@click.option("--place", type=str, help="Place name (geocoded via Nominatim)")
@click.option("--dem", type=str, default="auto", help="DEM source")
@click.option("--theme", "theme_name", type=str, required=True, help="Theme name")
@click.option("--exaggeration", type=float, default=None, help="Override theme exaggeration")
@click.option("--zoom", type=str, default="10-16", help="Zoom range (e.g. 10-16)")
def tile(bbox, place, dem, theme_name, exaggeration, zoom):
    """Cut a styled raster into XYZ tiles."""
    resolved_bbox = _resolve_bbox(bbox, place)
    styled_path = _ensure_styled(resolved_bbox, dem, theme_name, exaggeration)

    from .pipeline.tiler import generate_tiles
    from .cache import ensure_cache_dir

    tiles_dir = ensure_cache_dir("tiles") / f"{styled_path.stem}_z{zoom}"
    if tiles_dir.exists() and any(tiles_dir.rglob("*.png")):
        count = sum(1 for _ in tiles_dir.rglob("*.png"))
        click.echo(f"Cached: {tiles_dir} ({count:,} tiles)")
        return

    generate_tiles(styled_path, tiles_dir, zoom=zoom, progress_cb=lambda msg: click.echo(msg))
    click.echo(f"\nTiles: {tiles_dir}")


@cli.command()
@click.option("--bbox", type=str, help="Bounding box: west,south,east,north")
@click.option("--place", type=str, help="Place name (geocoded via Nominatim)")
@click.option("--dem", type=str, default="auto", help="DEM source")
@click.option("--theme", "theme_name", type=str, required=True, help="Theme name")
@click.option("--exaggeration", type=float, default=None, help="Override theme exaggeration")
@click.option("--zoom", type=str, default="10-16", help="Zoom range")
@click.option("--format", "output_format", type=str, default="pmtiles,mbtiles", help="Output formats")
@click.option("--output", type=click.Path(), help="Output directory")
def package(bbox, place, dem, theme_name, exaggeration, zoom, output_format, output):
    """Package tiles into MBTiles and/or PMTiles."""
    resolved_bbox = _resolve_bbox(bbox, place)
    styled_path = _ensure_styled(resolved_bbox, dem, theme_name, exaggeration)

    from .pipeline.tiler import generate_tiles
    from .pipeline.packager import package_mbtiles, package_pmtiles, metadata_from_raster
    from .cache import ensure_cache_dir
    from .themes import get_theme

    theme = get_theme(theme_name)
    exag = exaggeration or (theme.get_exaggeration_value() if theme else None) or 3.0
    cb = lambda msg: click.echo(msg)

    # Ensure tiles exist
    tiles_dir = ensure_cache_dir("tiles") / f"{styled_path.stem}_z{zoom}"
    if not tiles_dir.exists() or not any(tiles_dir.rglob("*.png")):
        generate_tiles(styled_path, tiles_dir, zoom=zoom, progress_cb=cb)

    # Output location
    out_dir = Path(output) if output else Path("./output")
    out_dir.mkdir(parents=True, exist_ok=True)

    base_name = f"{styled_path.stem}"
    meta = metadata_from_raster(styled_path)
    from .pipeline.tiler import parse_zoom
    min_z, max_z = parse_zoom(zoom)

    formats = [f.strip() for f in output_format.split(",")]

    mbtiles_path = None
    if "mbtiles" in formats:
        mbtiles_path = out_dir / f"{base_name}.mbtiles"
        package_mbtiles(
            tiles_dir, mbtiles_path,
            name=base_name,
            description=f"{theme_name} hillshade, {exag}x exaggeration",
            bounds=meta["bounds"],
            center=f"{meta['center']},{min_z}",
            min_zoom=min_z, max_zoom=max_z,
            progress_cb=cb,
        )

    if "pmtiles" in formats:
        if mbtiles_path is None:
            # Need mbtiles as intermediate
            mbtiles_path = out_dir / f"{base_name}.mbtiles"
            package_mbtiles(
                tiles_dir, mbtiles_path,
                name=base_name,
                bounds=meta["bounds"],
                center=f"{meta['center']},{min_z}",
                min_zoom=min_z, max_zoom=max_z,
                progress_cb=cb,
            )
        pmtiles_path = out_dir / f"{base_name}.pmtiles"
        package_pmtiles(mbtiles_path, pmtiles_path, progress_cb=cb)

        # Clean up intermediate mbtiles if not requested
        if "mbtiles" not in formats and mbtiles_path.exists():
            mbtiles_path.unlink()

    click.echo(f"\nOutput: {out_dir}")


@cli.command()
@click.option("--bbox", type=str, help="Bounding box: west,south,east,north")
@click.option("--place", type=str, help="Place name (geocoded via Nominatim)")
@click.option("--theme", type=str, required=True, help="Theme name")
@click.option("--exaggeration", type=str, default=None, help="Vertical exaggeration (number or omit for auto)")
@click.option("--dem", type=str, default="auto", help="DEM source")
@click.option("--zoom", type=str, default="10-16", help="Zoom range")
@click.option("--format", "output_format", type=str, default="pmtiles,mbtiles")
@click.option("--output", type=click.Path(), help="Output path")
@click.option("--keep-intermediates/--no-keep-intermediates", default=True, show_default=True, help="Keep intermediate files in output dir")
@click.option("--contribute/--no-contribute", default=True, show_default=True, help="Upload intermediates to public S3 cache")
@click.option("--no-cache", is_flag=True, help="Skip S3 cache reads (fully offline)")
@click.option("--s3-cache", type=str, help="Custom S3 cache bucket (default: s3://scriptedrelief-data/)")
@click.option("--buffer", "buffer_deg", type=float, default=None, help="Buffer in degrees around area (default 0.1° ≈ 11km)")
@click.option("--stop-after", type=click.Choice(["fetch", "reproject", "shade", "style", "tile", "package"]))
@click.option("--start-from", type=click.Choice(["fetch", "reproject", "shade", "style", "tile", "package"]))
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompts for large runs")
@click.option("--force", is_flag=True, help="Skip all sanity checks including hard limits")
@click.option("--estimate", is_flag=True, help="Show size/time estimate and exit without running")
def run(bbox, place, theme, exaggeration, dem, zoom, output_format, output,
        keep_intermediates, contribute, no_cache, s3_cache, buffer_deg, stop_after, start_from,
        yes, force, estimate):
    """Full pipeline: fetch → reproject → shade → style → tile → package."""
    resolved_bbox = _resolve_bbox(bbox, place, buffer_deg=buffer_deg)
    cb = lambda msg: click.echo(msg)

    # ── Estimate + sanity check ────────────────────────────────────────────
    from .cache import ensure_cache_dir as _ecd
    from .pipeline.tiler import parse_zoom as _pz
    from .sources import resolve_source as _rs
    _min_z, _max_z = _pz(zoom)
    # Resolve DEM source name for accurate estimates
    _dem_name = dem
    try:
        from .sources.base import BBox as _BBox
        _est_bbox = _BBox(resolved_bbox.west, resolved_bbox.south, resolved_bbox.east, resolved_bbox.north)
        _src = _rs(_est_bbox, dem)
        _dem_name = _src.name
    except Exception:
        _dem_name = dem if dem != "auto" else "usgs-3dep-10m"

    # Check if costly stages are already cached
    _styled_cached = False
    try:
        _theme_obj = None
        from .themes import get_theme as _gt
        _theme_obj = _gt(theme)
        _exag_val = float(exaggeration) if exaggeration else (
            _theme_obj.get_exaggeration_value() if _theme_obj else 3.0
        )
        from .cache import get_cache_dir as _gcd
        _styled_dir = _gcd() / "styled"
        _styled_cached = any(_styled_dir.glob("*.tif")) if _styled_dir.exists() else False
    except Exception:
        _exag_val = float(exaggeration) if exaggeration else 3.0

    _est = estimate_run(resolved_bbox, _dem_name, zoom, styled_cached=_styled_cached)
    print_estimate(_est, _dem_name, theme, _exag_val)

    if estimate:
        return

    if not force and not sanity_check(_est, force=force, yes=yes):
        raise SystemExit(0)

    # Stages in order
    stages = ["fetch", "reproject", "shade", "style", "tile", "package"]
    start_idx = stages.index(start_from) if start_from else 0
    stop_idx = stages.index(stop_after) if stop_after else len(stages) - 1

    # Stages 0-3 (fetch through style) are handled by _ensure_styled
    if stop_idx >= 3:
        styled_path = _ensure_styled(resolved_bbox, dem, theme, exaggeration)
    elif stop_idx >= 0:
        # Partial run — just run up to the requested stage
        styled_path = _ensure_styled(resolved_bbox, dem, theme, exaggeration)
        if stop_after in ("fetch", "reproject", "shade", "style"):
            click.echo(f"\nStopped after: {stop_after}")
            return

    if stop_idx < 4:
        click.echo(f"\nStopped after: {stop_after}")
        return

    # Stage 4: tile
    from .pipeline.tiler import generate_tiles
    from .cache import ensure_cache_dir

    tiles_dir = ensure_cache_dir("tiles") / f"{styled_path.stem}_z{zoom}"
    if not tiles_dir.exists() or not any(tiles_dir.rglob("*.png")):
        generate_tiles(styled_path, tiles_dir, zoom=zoom, progress_cb=cb)
    else:
        count = sum(1 for _ in tiles_dir.rglob("*.png"))
        click.echo(f"Tiles cached: {count:,} tiles")

    if stop_idx < 5:
        click.echo(f"\nStopped after: tile")
        return

    # Stage 5: package
    from .pipeline.packager import package_mbtiles, package_pmtiles, metadata_from_raster
    from .pipeline.tiler import parse_zoom
    from .themes import get_theme as _get_theme

    theme_obj = _get_theme(theme)
    exag = exaggeration or (theme_obj.get_exaggeration_value() if theme_obj else None) or 3.0

    out_dir = Path(output) if output else Path("./output")
    out_dir.mkdir(parents=True, exist_ok=True)

    base_name = styled_path.stem
    meta = metadata_from_raster(styled_path)
    min_z, max_z = parse_zoom(zoom)
    formats = [f.strip() for f in output_format.split(",")]

    mbtiles_path = None
    if "mbtiles" in formats:
        mbtiles_path = out_dir / f"{base_name}.mbtiles"
        package_mbtiles(
            tiles_dir, mbtiles_path,
            name=base_name,
            description=f"{theme} hillshade, {exag}x exaggeration",
            bounds=meta["bounds"], center=f"{meta['center']},{min_z}",
            min_zoom=min_z, max_zoom=max_z, progress_cb=cb,
        )

    if "pmtiles" in formats:
        if mbtiles_path is None:
            mbtiles_path = out_dir / f"{base_name}.mbtiles"
            package_mbtiles(
                tiles_dir, mbtiles_path, name=base_name,
                bounds=meta["bounds"], center=f"{meta['center']},{min_z}",
                min_zoom=min_z, max_zoom=max_z, progress_cb=cb,
            )
        pmtiles_path = out_dir / f"{base_name}.pmtiles"
        package_pmtiles(mbtiles_path, pmtiles_path, progress_cb=cb)
        if "mbtiles" not in formats and mbtiles_path.exists():
            mbtiles_path.unlink()

    # Contribute intermediates to S3 if requested
    if contribute:
        from .cache_s3 import push as s3_push, exists as s3_exists
        from .cache import get_cache_dir
        cache_dir = get_cache_dir()
        click.echo("\nContributing intermediates to shared cache...")
        contributed = 0
        skipped_reason: Optional[str] = None
        # On the first fatal auth/allowlist failure we stop trying — every
        # subsequent upload would fail identically and the noise drowns the
        # real run output (issue #1). Includes both direct-S3 codes and
        # broker error codes.
        _fatal_codes = (
            # Direct-S3 path
            "AccessDenied", "NoCredentialsError",
            "ExpiredToken", "InvalidAccessKeyId",
            # Broker path
            "AuthError", "missing_token", "invalid_token",
            "not_allowlisted",
        )
        for stage in ["reprojected", "hillshade", "styled"]:
            if skipped_reason:
                break
            stage_dir = cache_dir / stage
            if not stage_dir.exists():
                continue
            for f in stage_dir.rglob("*.tif"):
                if skipped_reason:
                    break
                if s3_exists(stage, f.name):
                    click.echo(f"  {stage}/{f.name}: already in cache")
                    continue
                click.echo(f"  Uploading {stage}/{f.name}...")
                ok, reason = s3_push(f, stage, f.name)
                if ok:
                    contributed += 1
                    click.echo("    ✓ uploaded")
                else:
                    click.echo(f"    ✗ failed: {reason}")
                    if reason and any(c in reason for c in _fatal_codes):
                        skipped_reason = reason
        if skipped_reason:
            click.echo(
                f"\n⚠️  --contribute disabled for the rest of this run: "
                f"{skipped_reason}\n"
                f"   Run `hillgen auth status` for diagnostics, or rerun "
                f"with --no-contribute."
            )
        elif contributed:
            click.echo(f"Contributed {contributed} file(s) to s3://scriptedrelief-data/cache/")
        else:
            click.echo("All intermediates already in shared cache.")

    # Output summary
    click.echo(f"\nDone! Output: {out_dir}")
    out_files = sorted(out_dir.glob(f"{base_name}.*"))
    for f in out_files:
        size_mb = f.stat().st_size / (1024 * 1024)
        click.echo(f"  {f.name} ({size_mb:.1f} MB)")
    click.echo(f"\nTo view locally:")
    tiles_dir_name = f"{styled_path.stem}_z{zoom}"
    from .cache import get_cache_dir as _get_cache_dir
    tiles_path = _get_cache_dir() / "tiles" / tiles_dir_name
    click.echo(f"  hillgen view {tiles_path}")


@cli.command()
@click.option("--tag", type=str, help="Filter by tag")
@click.option("--show", type=str, help="Show details for a specific theme")
def themes(tag, show):
    """List available themes."""
    from .themes import get_theme, list_themes as _list_themes

    if show:
        theme = get_theme(show)
        if not theme:
            click.echo(f"Unknown theme: {show}")
            raise SystemExit(1)
        click.echo(f"Theme: {theme.name}")
        click.echo(f"  {theme.description}")
        click.echo(f"  Ramp:         {theme.ramp}")
        click.echo(f"  Color mode:   {theme.color_mode}")
        click.echo(f"  Shading:      {theme.shading}")
        if theme.shading == "composite":
            click.echo(f"  Weights:      {', '.join(str(w) for w in theme.composite_weights)}")
        click.echo(f"  Exaggeration: {theme.exaggeration}")
        if theme.aspect_blend > 0:
            click.echo(f"  Aspect blend: {theme.aspect_blend}")
        click.echo(f"  Tags:         {', '.join(theme.tags)}")
        return

    all_themes = _list_themes(tag=tag)
    if not all_themes:
        click.echo(f"No themes found{f' with tag: {tag}' if tag else ''}")
        return

    click.echo(f"{len(all_themes)} themes{f' (tag: {tag})' if tag else ''}:\n")
    for t in all_themes:
        tags = ", ".join(t.tags)
        click.echo(f"  {t.name:20s} {t.description[:60]}")
    click.echo(f"\nUse --show <name> for details.")


@cli.command()
def sources():
    """List available DEM sources."""
    from .sources import list_sources
    for s in list_sources():
        click.echo(f"  {s.name:20s} {s.resolution_m:6.1f}m  pri={s.priority:3d}  {s.description}")


@cli.command()
@click.argument("path", type=click.Path(exists=True))
@click.option("--port", type=int, default=9999, help="Server port")
def view(path, port):
    """Start a local tile viewer."""
    from .viewer import serve_tiles
    serve_tiles(Path(path), port=port)


@cli.command()
@click.argument("pmtiles", type=click.Path(), required=False)
@click.option("-o", "--output", "output", type=click.Path(), default=None,
              help="Output PNG path. Default: <pmtiles-stem>.png next to the pmtiles file "
                   "(or next to --from-styled when no pmtiles is given).")
@click.option("--width", type=int, default=1200,
              help="Output PNG width in pixels (height preserves aspect ratio).")
@click.option("--from-styled", "from_styled", type=click.Path(exists=True), default=None,
              help="Render directly from this styled GeoTIFF (skips cache lookup). "
                   "PMTILES becomes optional in this mode.")
def preview(pmtiles, output, width, from_styled):
    """Render a flat PNG preview of a generated PMTiles file.

    Issue #8: reads the matching styled GeoTIFF from the hillgen cache
    (``$HILLGEN_CACHE/styled/<stem>.tif``) and downscales it with
    ``gdal_translate``. Use ``--from-styled`` to point at any GeoTIFF directly
    (e.g. to backfill a thumbnail for an existing gallery entry).
    """
    import shutil
    import subprocess

    if not pmtiles and not from_styled:
        raise click.UsageError("Provide PMTILES, --from-styled, or both.")

    p = Path(pmtiles) if pmtiles else None
    if p is not None and p.suffix != ".pmtiles":
        raise click.BadParameter("expected a .pmtiles file", param_hint="PMTILES")

    if from_styled:
        styled = Path(from_styled)
    else:
        # p is guaranteed not None here; cache lookup mode requires it to exist
        # so we can resolve the stem and find a matching styled tif.
        if not p.exists():
            raise click.BadParameter(f"path does not exist: {p}", param_hint="PMTILES")
        from .cache import get_cache_dir
        styled = get_cache_dir() / "styled" / f"{p.stem}.tif"

    if not styled.exists():
        click.echo(f"Error: styled raster not found: {styled}")
        click.echo("Hint: re-run `hillgen run ...` (intermediates land in the cache),")
        click.echo("      or pass --from-styled <path-to-styled.tif>.")
        raise SystemExit(1)

    if not shutil.which("gdal_translate"):
        click.echo("Error: gdal_translate not found on PATH. Install GDAL.")
        raise SystemExit(1)

    if output:
        out = Path(output)
    elif p is not None:
        out = p.with_suffix(".png")
    else:
        out = styled.with_suffix(".png")
    out.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "gdal_translate",
        "-of", "PNG",
        "-outsize", str(width), "0",
        "-co", "ZLEVEL=9",
        "-q",
        str(styled),
        str(out),
    ]
    click.echo(f"Rendering {styled.name} → {out.name} ({width}px wide)...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        click.echo(f"gdal_translate failed:\n{result.stderr}")
        raise SystemExit(1)

    size_kb = out.stat().st_size / 1024
    click.echo(f"✓ {out} ({size_kb:.0f} KB)")


def _publish_gallery_via_broker(
    p: Path,
    *,
    preview,
    size_mb: float,
    title,
    caption,
    author,
):
    """Issue #11: upload a PMTiles + optional preview through the broker.

    Mirrors the boto3 path in ``publish()`` but uses GitHub-token auth instead
    of AWS credentials. Steps:

      1. Upload the .pmtiles via ``upload_via_broker`` (stage ``gallery-pmtiles``)
      2. Optionally upload preview image (stage ``gallery-preview``)
      3. Call ``submit_gallery_entry`` so the broker appends to catalog.json
    """
    from . import contribute_broker as cb

    try:
        token = cb.get_github_token()
    except cb.AuthError as e:
        click.echo(f"Auth error: {e}")
        click.echo("Set HILLGEN_USE_DIRECT_S3=1 to fall back to direct AWS uploads.")
        raise SystemExit(1)

    click.echo(f"Uploading {p.name} via broker...")
    ok, reason = cb.upload_via_broker(p, "gallery-pmtiles", token=token,
                                      progress_cb=lambda m: click.echo(m))
    if not ok:
        click.echo(f"Upload failed: {reason}")
        raise SystemExit(1)
    pmtiles_url = f"https://scriptedrelief.com/gallery/{p.name}"
    click.echo(f"✓ PMTiles: {pmtiles_url}")

    preview_url = None
    if preview:
        prev_path = Path(preview)
        # Match the existing naming convention so previews don't collide.
        broker_name = f"preview-{p.stem}{prev_path.suffix}"
        # The broker writes the file at gallery/<filename>; copy locally to a
        # uniquely-named temp so the broker sees the right basename.
        import shutil as _sh
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            staged = Path(td) / broker_name
            _sh.copyfile(prev_path, staged)
            click.echo(f"Uploading preview {staged.name} via broker...")
            ok, reason = cb.upload_via_broker(staged, "gallery-preview", token=token,
                                              progress_cb=lambda m: click.echo(m))
        if not ok:
            click.echo(f"Preview upload failed: {reason}")
            raise SystemExit(1)
        preview_url = f"https://scriptedrelief.com/gallery/{broker_name}"
        click.echo(f"✓ Preview: {preview_url}")

    # Append catalog entry server-side.
    try:
        resp = cb.submit_gallery_entry(
            pmtiles_url=pmtiles_url,
            title=title or p.stem,
            caption=caption or "",
            author=author,
            preview_url=preview_url,
            size_mb=size_mb,
            token=token,
        )
    except cb.BrokerError as e:
        click.echo(f"Catalog update failed: {e.code}: {e.message}")
        raise SystemExit(1)

    if resp.get("status") == "duplicate":
        click.echo(f"Already in gallery: {pmtiles_url}")
    else:
        click.echo(f"✓ Catalog: https://scriptedrelief.com/gallery/catalog.json")
        click.echo(f"Submission registered. {resp.get('count', '?')} total in gallery.")


def _publish_url_only(
    *,
    pmtiles_url: str,
    preview_url,
    size_mb,
    title,
    caption,
    author,
    dry_run: bool,
):
    """Register a gallery submission pointing at an already-hosted PMTiles URL.

    Issue #10: when the PMTiles file already lives in our S3 bucket (e.g.
    promoted from a curated library entry under ``tiles/``), append a gallery
    catalog entry instead of re-uploading hundreds of MB. By default this
    goes through the broker (GitHub-token auth, issue #11); maintainers can
    set ``HILLGEN_USE_DIRECT_S3=1`` to use AWS credentials directly.
    """
    import datetime
    import json
    import re
    from urllib.parse import urlparse

    import requests as _requests

    parsed = urlparse(pmtiles_url)
    if parsed.scheme not in ("http", "https"):
        raise click.BadParameter("must be an http(s) URL", param_hint="--pmtiles-url")
    if not parsed.path.endswith(".pmtiles"):
        raise click.BadParameter("URL path must end in .pmtiles", param_hint="--pmtiles-url")

    try:
        head = _requests.head(pmtiles_url, allow_redirects=True, timeout=10)
    except _requests.RequestException as e:
        click.echo(f"Error: HEAD {pmtiles_url} failed: {e}")
        raise SystemExit(1)
    if head.status_code // 100 != 2:
        click.echo(f"Error: HEAD {pmtiles_url} returned {head.status_code}")
        raise SystemExit(1)

    if size_mb is None:
        cl = head.headers.get("Content-Length")
        if cl and cl.isdigit():
            size_mb = int(cl) / (1024 * 1024)
        else:
            click.echo("Warning: server did not return Content-Length; pass --size-mb to set explicitly.")
            size_mb = 0.0

    # Ranged GET for magic-byte validation. Servers that ignore Range still
    # send the full body but we cap the read at 127 bytes.
    try:
        r = _requests.get(pmtiles_url, headers={"Range": "bytes=0-126"}, timeout=15, stream=True)
        header_bytes = r.raw.read(127)
        r.close()
    except _requests.RequestException as e:
        click.echo(f"Error: range GET failed: {e}")
        raise SystemExit(1)
    if len(header_bytes) < 8 or header_bytes[0:7] != b"PMTiles":
        click.echo(f"Error: URL does not appear to serve a PMTiles v3 file (magic={header_bytes[0:7]!r})")
        raise SystemExit(1)
    if header_bytes[7] != 3:
        click.echo(f"Error: expected PMTiles v3, got v{header_bytes[7]}")
        raise SystemExit(1)

    click.echo(f"Validated remote PMTiles v3: {pmtiles_url} ({size_mb:.1f} MB)")

    if dry_run:
        click.echo("Dry run — validation passed, not updating catalog.")
        return

    # Issue #11: prefer the broker so users don't need AWS creds for catalog
    # updates. Maintainers can opt out via HILLGEN_USE_DIRECT_S3=1.
    if not os.environ.get("HILLGEN_USE_DIRECT_S3"):
        from . import contribute_broker as cb
        try:
            resp = cb.submit_gallery_entry(
                pmtiles_url=pmtiles_url,
                title=title or Path(pmtiles_url).stem,
                caption=caption or "",
                author=author,
                preview_url=preview_url,
                size_mb=size_mb,
            )
        except cb.AuthError as e:
            click.echo(f"Auth error: {e}")
            click.echo("Set HILLGEN_USE_DIRECT_S3=1 to fall back to direct AWS catalog writes.")
            raise SystemExit(1)
        except cb.BrokerError as e:
            click.echo(f"Catalog update failed: {e.code}: {e.message}")
            raise SystemExit(1)

        if resp.get("status") == "duplicate":
            click.echo(f"Already in gallery: {pmtiles_url}")
        else:
            click.echo(f"✓ Catalog: https://scriptedrelief.com/gallery/catalog.json")
            click.echo(f"Submission registered (no upload). {resp.get('count', '?')} total in gallery.")
        return

    # ── Maintainer fallback: direct boto3 ─────────────────────────────────

    try:
        import boto3
        from botocore.exceptions import NoCredentialsError, ClientError
    except ImportError:
        click.echo("Error: boto3 required for HILLGEN_USE_DIRECT_S3 path. pip install boto3")
        raise SystemExit(1)

    bucket = "scriptedrelief"
    catalog_key = "gallery/catalog.json"
    try:
        s3 = boto3.client("s3", region_name="us-east-2")
        try:
            obj = s3.get_object(Bucket=bucket, Key=catalog_key)
            catalog = json.loads(obj["Body"].read())
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                catalog = {"submissions": []}
            else:
                raise

        if any(s.get("pmtiles") == pmtiles_url for s in catalog.get("submissions", [])):
            click.echo(f"Already in gallery: {pmtiles_url}")
            return

        default_title = re.sub(r"[_-]", " ", Path(parsed.path).stem).strip() or pmtiles_url
        entry = {
            "pmtiles": pmtiles_url,
            "preview": preview_url,
            "title": title or default_title,
            "caption": caption or "",
            "author": author or "anonymous",
            "size_mb": round(size_mb, 1),
            "submitted": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        catalog.setdefault("submissions", []).append(entry)

        s3.put_object(
            Bucket=bucket,
            Key=catalog_key,
            Body=json.dumps(catalog, indent=2).encode(),
            ContentType="application/json",
            CacheControl="public, max-age=60",
        )
        click.echo(f"✓ Catalog: https://scriptedrelief.com/{catalog_key}")
        click.echo(f"Submission registered (no upload). {len(catalog['submissions'])} total in gallery.")
    except NoCredentialsError:
        click.echo("Error: no AWS credentials found for catalog update.")
        click.echo("Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY env vars, then retry.")
        raise SystemExit(1)
    except ClientError as e:
        click.echo(f"Error updating catalog: {e}")
        raise SystemExit(1)


@cli.command()
@click.argument("path", type=click.Path(exists=True), required=False)
@click.option("--dry-run", is_flag=True, help="Validate only, don't upload")
@click.option("--gallery", is_flag=True, help="Upload to community gallery (gallery/ prefix)")
@click.option("--title", type=str, default=None, help="Display title for gallery entry")
@click.option("--caption", type=str, default=None, help="Short description for gallery entry")
@click.option("--author", type=str, default=None, help="Your name or handle")
@click.option("--preview", type=click.Path(exists=True), default=None, help="Preview PNG to upload alongside")
@click.option("--area", type=str, default=None, help="Area slug for catalog entry (e.g. kennecott)")
@click.option("--name", "display_name", type=str, default=None, help="Display name for catalog entry")
@click.option("--theme", type=str, default=None, help="Theme name for catalog entry")
@click.option("--description", type=str, default=None, help="Description for catalog entry")
@click.option("--tags", type=str, default=None, help="Comma-separated tags for catalog entry")
@click.option("--no-catalog", is_flag=True, help="Skip catalog.json update")
@click.option("--pmtiles-url", type=str, default=None,
              help="Register a gallery submission pointing at an already-hosted PMTiles URL "
                   "(skips upload). Requires --gallery.")
@click.option("--preview-url", type=str, default=None,
              help="URL of an already-hosted preview image; used with --pmtiles-url.")
@click.option("--size-mb", type=float, default=None,
              help="Manual size in MB for --pmtiles-url submissions (otherwise resolved via HEAD).")
def publish(path, dry_run, gallery, title, caption, author, preview, area, display_name, theme, description, tags, no_catalog, pmtiles_url, preview_url, size_mb):
    """Publish a PMTiles file to scriptedrelief.com.

    Default: uploads to tiles/ (curator use, requires AWS credentials).
    With --gallery: routes the upload + catalog update through the broker
    using your GitHub token (same auth as ``hillgen run --contribute``).
    Set HILLGEN_USE_DIRECT_S3=1 to fall back to direct AWS uploads.

    Use --pmtiles-url to register a gallery submission for a PMTiles file
    that's already hosted (e.g. promoted from tiles/) without re-uploading.
    """
    import json
    import datetime

    # ── Issue #10: --pmtiles-url short-circuit ─────────────────────────────
    # Register a gallery submission pointing at an already-hosted PMTiles file,
    # skipping upload entirely. Useful when the file is already in our S3
    # bucket (e.g. under tiles/) and re-uploading 100s of MB would be wasteful.
    if pmtiles_url:
        if not gallery:
            raise click.UsageError("--pmtiles-url only makes sense with --gallery")
        if path:
            raise click.UsageError("Provide either a PATH or --pmtiles-url, not both")
        return _publish_url_only(
            pmtiles_url=pmtiles_url,
            preview_url=preview_url,
            size_mb=size_mb,
            title=title,
            caption=caption,
            author=author,
            dry_run=dry_run,
        )

    if not path:
        raise click.UsageError("Provide a PATH (or --pmtiles-url for --gallery submissions)")

    p = Path(path)
    if not p.suffix == ".pmtiles":
        click.echo(f"Expected .pmtiles file, got: {p.suffix}")
        raise SystemExit(1)

    # Validate PMTiles v3 header
    with open(p, "rb") as f:
        header = f.read(127)

    if len(header) < 127:
        click.echo("Error: file too small to be valid PMTiles")
        raise SystemExit(1)

    magic = header[0:7]
    if magic != b"PMTiles":
        click.echo(f"Error: invalid PMTiles magic bytes: {magic}")
        raise SystemExit(1)

    version = header[7]
    if version != 3:
        click.echo(f"Error: expected PMTiles v3, got v{version}")
        raise SystemExit(1)

    size_mb = p.stat().st_size / (1024 * 1024)
    click.echo(f"Valid PMTiles v3: {p.name} ({size_mb:.1f} MB)")

    if dry_run:
        click.echo("Dry run — validation passed, not uploading.")
        return

    # Issue #11: route gallery uploads through the broker by default so
    # contributors only need a GitHub token (same as --contribute). Maintainers
    # who own AWS credentials can opt into direct boto3 with
    # HILLGEN_USE_DIRECT_S3=1.
    if gallery and not os.environ.get("HILLGEN_USE_DIRECT_S3"):
        return _publish_gallery_via_broker(
            p, preview=preview, size_mb=size_mb,
            title=title, caption=caption, author=author,
        )

    # Upload to S3
    try:
        import boto3
        from botocore.exceptions import NoCredentialsError, ClientError
    except ImportError:
        click.echo("Error: boto3 required for publish. pip install boto3")
        raise SystemExit(1)

    bucket = "scriptedrelief"
    prefix = "gallery/" if gallery else "tiles/"
    region = "us-east-2"

    # Issue #9: when uploading to the curated library (non-gallery) and a
    # `--area` slug is provided, organize under tiles/<area>/<file> to match
    # the existing catalog convention (tiles/indiana-dunes/..., etc.).
    area_subdir = ""
    if not gallery and area:
        # Defensive: keep the slug to a single path segment with no traversal.
        slug = area.strip().strip("/").replace(" ", "-").lower()
        if "/" in slug or ".." in slug or not slug:
            raise click.BadParameter(
                "must be a single path segment (e.g. 'death-valley'), no slashes",
                param_hint="--area",
            )
        area_subdir = f"{slug}/"

    try:
        s3 = boto3.client("s3", region_name=region)

        # Upload PMTiles
        key = f"{prefix}{area_subdir}{p.name}"
        click.echo(f"Uploading to s3://{bucket}/{key}...")
        s3.upload_file(
            str(p), bucket, key,
            ExtraArgs={"ContentType": "application/x-protobuf", "CacheControl": "public, max-age=31536000"}
        )
        click.echo(f"✓ PMTiles: https://scriptedrelief.com/{key}")

        # Upload preview PNG if provided
        preview_url = None
        if preview:
            prev_path = Path(preview)
            # Use PMTiles stem as prefix to ensure unique preview filenames
            prev_key = f"{prefix}{area_subdir}preview-{p.stem}{prev_path.suffix}"
            click.echo(f"Uploading preview to s3://{bucket}/{prev_key}...")
            s3.upload_file(
                str(prev_path), bucket, prev_key,
                ExtraArgs={"ContentType": "image/png", "CacheControl": "public, max-age=31536000"}
            )
            preview_url = f"https://scriptedrelief.com/{prev_key}"
            click.echo(f"✓ Preview: {preview_url}")

        if gallery:
            # Fetch current gallery catalog, append entry, write back
            catalog_key = "gallery/catalog.json"
            try:
                obj = s3.get_object(Bucket=bucket, Key=catalog_key)
                catalog = json.loads(obj["Body"].read())
            except s3.exceptions.NoSuchKey:
                catalog = {"submissions": []}
            except ClientError as e:
                if e.response["Error"]["Code"] == "NoSuchKey":
                    catalog = {"submissions": []}
                else:
                    raise

            pmtiles_url = f"https://scriptedrelief.com/{key}"
            # Dedupe — don't append if this PMTiles URL is already in the catalog
            if any(s.get("pmtiles") == pmtiles_url for s in catalog["submissions"]):
                click.echo(f"Already in gallery: {pmtiles_url}")
                click.echo("Use a different output filename or remove the existing entry first.")
                raise SystemExit(0)

            new_entry = {
                "pmtiles": pmtiles_url,
                "preview": preview_url,
                "title": title or p.stem,
                "caption": caption or "",
                "author": author or "anonymous",
                "size_mb": round(size_mb, 1),
                "submitted": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            catalog["submissions"].append(new_entry)

            s3.put_object(
                Bucket=bucket,
                Key=catalog_key,
                Body=json.dumps(catalog, indent=2).encode(),
                ContentType="application/json",
                CacheControl="public, max-age=60",
            )
            click.echo(f"✓ Catalog: https://scriptedrelief.com/{catalog_key}")
            click.echo("")
            click.echo(f"Submission uploaded! {len(catalog['submissions'])} total in gallery.")
        else:
            click.echo(f"Published: https://scriptedrelief.com/{key}")
            if no_catalog:
                click.echo("Skipping catalog.json update (--no-catalog).")
            else:
                # Parse metadata from filename if not provided
                # Expected pattern: {source}_{lat}_{lon}_{crs}_{theme}_{exag}x.pmtiles
                import re
                stem = p.stem  # e.g. 3dep_n40.53_w112.14_4326_vivid-elevation_9.0x
                parsed_theme = None
                parsed_exag = None
                m = re.search(r'_([a-z][a-z0-9-]+)_([0-9]+(?:\.[0-9]+)?)x$', stem)
                if m:
                    parsed_theme = m.group(1)
                    parsed_exag = float(m.group(2))

                catalog_key = "catalog.json"
                try:
                    obj = s3.get_object(Bucket=bucket, Key=catalog_key)
                    catalog = json.loads(obj["Body"].read())
                except ClientError as e:
                    if e.response["Error"]["Code"] == "NoSuchKey":
                        catalog = {"generated": "", "layers": []}
                    else:
                        raise

                pmtiles_rel = f"tiles/{area_subdir}{p.name}"
                pmtiles_url = f"https://scriptedrelief.com/{pmtiles_rel}"

                # Dedupe by pmtiles path
                existing = [l for l in catalog.get("layers", []) if l.get("pmtiles") == pmtiles_rel]
                if existing:
                    click.echo(f"Already in catalog: {pmtiles_rel}")
                else:
                    entry = {
                        "area": area or stem,
                        "name": display_name or area or stem,
                        "theme": theme or parsed_theme or "",
                        "description": description or "",
                        "exaggeration": parsed_exag or 1.0,
                        "dem_source": "USGS 3DEP 1/3 arc-second",
                        "zoom": [10, 16],
                        "pmtiles": pmtiles_rel,
                        "size_mb": round(size_mb, 1),
                    }
                    if tags:
                        entry["tags"] = [t.strip() for t in tags.split(",")]
                    if preview_url:
                        entry["preview"] = preview_url

                    catalog.setdefault("layers", []).append(entry)
                    catalog["generated"] = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

                    s3.put_object(
                        Bucket=bucket,
                        Key=catalog_key,
                        Body=json.dumps(catalog, indent=2).encode(),
                        ContentType="application/json",
                        CacheControl="public, max-age=60",
                    )
                    click.echo(f"✓ Catalog: https://scriptedrelief.com/{catalog_key} ({len(catalog['layers'])} layers)")

    except NoCredentialsError:
        click.echo("Error: no AWS credentials found.")
        click.echo("Set AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY env vars, then retry.")
        raise SystemExit(1)
    except ClientError as e:
        click.echo(f"Error uploading: {e}")
        raise SystemExit(1)


@cli.group()
def auth():
    """Inspect contributor authentication (GitHub via `gh` CLI)."""
    pass


@auth.command("status")
def auth_status():
    """Show whether a usable GitHub token is available for --contribute."""
    from .contribute_broker import get_github_token, AuthError, DEFAULT_ENDPOINT
    import os as _os

    endpoint = _os.environ.get("HILLGEN_CONTRIBUTE_ENDPOINT", DEFAULT_ENDPOINT)
    click.echo(f"Broker endpoint:  {endpoint}")

    try:
        token = get_github_token()
    except AuthError as e:
        click.secho(f"GitHub token:     ✗ {e}", fg="red")
        click.echo("\nTo fix:")
        click.echo("  1. Install gh:  brew install gh   (or https://cli.github.com)")
        click.echo("  2. Log in:      gh auth login")
        click.echo("  3. Verify:      hillgen auth status")
        raise SystemExit(1)

    # Verify the token works against GitHub.
    try:
        import requests
        r = requests.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json"},
            timeout=5,
        )
        if r.status_code == 200:
            username = r.json().get("login", "?")
            click.secho(f"GitHub token:     ✓ valid (user: {username})", fg="green")
            click.echo(
                "\nNote: being authenticated does not mean you're on the contributor "
                "allowlist. Open an issue at "
                "https://github.com/emuehlstein/hillshade-generator to request access."
            )
        else:
            click.secho(f"GitHub token:     ✗ rejected by GitHub ({r.status_code})", fg="red")
            raise SystemExit(1)
    except Exception as e:
        click.secho(f"GitHub token:     ⚠ could not verify ({e})", fg="yellow")


@cli.group()
def cache():
    """Manage the local and S3 cache."""
    pass


@cache.command()
def status():
    """Show cache contents and sizes."""
    from .cache import get_cache_dir
    cache_dir = get_cache_dir()
    click.echo(f"Cache directory: {cache_dir}")
    if not cache_dir.exists():
        click.echo("  (empty — no cached files)")
        return

    total_size = 0
    total_files = 0
    for stage in ["dem", "reprojected", "hillshade", "styled"]:
        stage_dir = cache_dir / stage
        if not stage_dir.exists():
            continue
        files = list(stage_dir.rglob("*"))
        files = [f for f in files if f.is_file()]
        size = sum(f.stat().st_size for f in files)
        total_size += size
        total_files += len(files)
        click.echo(f"  {stage + ':':14s} {len(files):4d} files  {_human_size(size)}")

    click.echo(f"  {'total:':14s} {total_files:4d} files  {_human_size(total_size)}")


@cache.command()
@click.option("--dry-run", is_flag=True, help="Show what would be deleted")
@click.option("--stage", type=click.Choice(["dem", "reprojected", "hillshade", "styled", "tiles", "all"]), default="all")
def clean(dry_run, stage):
    """Remove cached intermediates."""
    from .cache import get_cache_dir
    cache_dir = get_cache_dir()
    if not cache_dir.exists():
        click.echo("Cache is already empty.")
        return

    stages = ["dem", "reprojected", "hillshade", "styled", "tiles"] if stage == "all" else [stage]
    total_size = 0
    total_files = 0

    for s in stages:
        stage_dir = cache_dir / s
        if not stage_dir.exists():
            continue
        files = [f for f in stage_dir.rglob("*") if f.is_file()]
        size = sum(f.stat().st_size for f in files)
        if files:
            click.echo(f"  {s}: {len(files)} files, {_human_size(size)}")
            total_size += size
            total_files += len(files)
            if not dry_run:
                shutil.rmtree(stage_dir)

    if total_files == 0:
        click.echo("Nothing to clean.")
    elif dry_run:
        click.echo(f"\nWould delete {total_files} files ({_human_size(total_size)})")
    else:
        click.echo(f"\nDeleted {total_files} files ({_human_size(total_size)})")


@cache.command(name="pull")
@click.option("--bbox", type=str, help="Bounding box: west,south,east,north")
@click.option("--place", type=str, help="Place name")
@click.option("--dem", type=str, default="auto", help="DEM source")
def cache_pull(bbox, place, dem):
    """Pre-fetch DEM for an area (download only, no processing)."""
    # This is just an alias for fetch
    from .sources import resolve_source
    from .cache import ensure_cache_dir
    resolved_bbox = _resolve_bbox(bbox, place)
    source = resolve_source(resolved_bbox, dem)
    dem_dir = ensure_cache_dir("dem") / source.name
    source.download(resolved_bbox, dem_dir, progress_cb=lambda msg: click.echo(msg))


def _human_size(nbytes):
    """Format bytes as human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"
