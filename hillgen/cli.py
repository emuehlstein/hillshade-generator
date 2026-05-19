"""
Hillgen CLI

Usage:
    hillgen version
    hillgen fetch --bbox "-87.70,41.96,-87.66,41.99" --dem usgs-3dep-10m
    hillgen shade --bbox "-87.70,41.96,-87.66,41.99" --exaggeration 9
    hillgen style --bbox "..." --theme midnight
    hillgen run --place "Crater Lake" --theme midnight
"""

import shutil
import subprocess
import sys

import click

from . import __version__


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


# Placeholder subcommands — will be implemented in subsequent milestones

@cli.command()
@click.option("--bbox", type=str, help="Bounding box: west,south,east,north")
@click.option("--place", type=str, help="Place name (geocoded via Nominatim)")
@click.option("--dem", type=str, default="auto", help="DEM source (auto, usgs-3dep-10m, usgs-3dep-1m, copernicus-30m, srtm-30m)")
def fetch(bbox, place, dem):
    """Download and cache DEM data for an area."""
    click.echo("Not yet implemented — see ROADMAP.md M1")
    raise SystemExit(1)


@cli.command()
@click.option("--bbox", type=str, help="Bounding box: west,south,east,north")
@click.option("--place", type=str, help="Place name (geocoded via Nominatim)")
def reproject(bbox, place):
    """Reproject a cached DEM to EPSG:4326."""
    click.echo("Not yet implemented — see ROADMAP.md M2")
    raise SystemExit(1)


@cli.command()
@click.option("--bbox", type=str, help="Bounding box: west,south,east,north")
@click.option("--place", type=str, help="Place name (geocoded via Nominatim)")
@click.option("--exaggeration", type=str, default="auto", help="Vertical exaggeration (number or 'auto')")
@click.option("--shading", type=click.Choice(["standard", "multidirectional", "composite"]), default="composite")
def shade(bbox, place, exaggeration, shading):
    """Generate grayscale hillshade from a reprojected DEM."""
    click.echo("Not yet implemented — see ROADMAP.md M2")
    raise SystemExit(1)


@cli.command()
@click.option("--bbox", type=str, help="Bounding box: west,south,east,north")
@click.option("--place", type=str, help="Place name (geocoded via Nominatim)")
@click.option("--theme", type=str, required=True, help="Theme name or path to custom theme JSON")
@click.option("--exaggeration", type=str, default="auto", help="Vertical exaggeration (number or 'auto')")
def style(bbox, place, theme, exaggeration):
    """Apply a theme to a cached hillshade."""
    click.echo("Not yet implemented — see ROADMAP.md M3")
    raise SystemExit(1)


@cli.command()
@click.option("--bbox", type=str, help="Bounding box: west,south,east,north")
@click.option("--place", type=str, help="Place name (geocoded via Nominatim)")
@click.option("--theme", type=str, required=True, help="Theme name")
@click.option("--exaggeration", type=str, default="auto")
@click.option("--zoom", type=str, default="10-16", help="Zoom range (e.g. 10-16)")
def tile(bbox, place, theme, exaggeration, zoom):
    """Cut a styled raster into XYZ tiles."""
    click.echo("Not yet implemented — see ROADMAP.md M4")
    raise SystemExit(1)


@cli.command()
@click.option("--bbox", type=str, help="Bounding box: west,south,east,north")
@click.option("--place", type=str, help="Place name (geocoded via Nominatim)")
@click.option("--theme", type=str, required=True, help="Theme name")
@click.option("--exaggeration", type=str, default="auto")
@click.option("--format", "output_format", type=str, default="pmtiles,mbtiles", help="Output formats (pmtiles, mbtiles, dir)")
def package(bbox, place, theme, exaggeration, output_format):
    """Package tiles into MBTiles and/or PMTiles."""
    click.echo("Not yet implemented — see ROADMAP.md M4")
    raise SystemExit(1)


@cli.command()
@click.option("--bbox", type=str, help="Bounding box: west,south,east,north")
@click.option("--place", type=str, help="Place name (geocoded via Nominatim)")
@click.option("--theme", type=str, required=True, help="Theme name")
@click.option("--exaggeration", type=str, default="auto")
@click.option("--dem", type=str, default="auto", help="DEM source")
@click.option("--zoom", type=str, default="10-16", help="Zoom range")
@click.option("--format", "output_format", type=str, default="pmtiles,mbtiles")
@click.option("--output", type=click.Path(), help="Output path")
@click.option("--keep-intermediates", is_flag=True, help="Keep intermediate files in output dir")
@click.option("--contribute", is_flag=True, help="Upload intermediates to public S3 cache")
@click.option("--no-cache", is_flag=True, help="Skip S3 cache reads (fully offline)")
@click.option("--s3-cache", type=str, help="Custom S3 cache bucket (default: s3://scriptedrelief-data/)")
@click.option("--stop-after", type=click.Choice(["fetch", "reproject", "shade", "style", "tile", "package"]))
@click.option("--start-from", type=click.Choice(["fetch", "reproject", "shade", "style", "tile", "package"]))
def run(bbox, place, theme, exaggeration, dem, zoom, output_format, output,
        keep_intermediates, contribute, no_cache, s3_cache, stop_after, start_from):
    """Full pipeline: fetch → reproject → shade → style → tile → package."""
    click.echo("Not yet implemented — see ROADMAP.md M1-M4")
    raise SystemExit(1)


@cli.command()
@click.option("--tag", type=str, help="Filter by tag")
@click.option("--show", type=str, help="Show details for a specific theme")
def themes(tag, show):
    """List available themes."""
    click.echo("Not yet implemented — see ROADMAP.md M3")
    raise SystemExit(1)


@cli.command()
def sources():
    """List available DEM sources."""
    click.echo("Not yet implemented — see ROADMAP.md M1")
    raise SystemExit(1)


@cli.command()
@click.argument("path", type=click.Path(exists=True))
@click.option("--port", type=int, default=9999, help="Server port")
def view(path, port):
    """Start a local tile viewer."""
    click.echo("Not yet implemented — see ROADMAP.md M4")
    raise SystemExit(1)


@cli.command()
@click.argument("path", type=click.Path(exists=True))
def publish(path):
    """Publish a PMTiles file to the community library."""
    click.echo("Not yet implemented — see ROADMAP.md M9")
    raise SystemExit(1)


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
def clean(dry_run):
    """Remove cached intermediates."""
    click.echo("Not yet implemented — see ROADMAP.md M5")
    raise SystemExit(1)


def _human_size(nbytes):
    """Format bytes as human-readable string."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(nbytes) < 1024:
            return f"{nbytes:.1f} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} PB"
