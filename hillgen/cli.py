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
from pathlib import Path

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


def _ensure_styled(resolved_bbox, dem, theme_name, exaggeration):
    """Run fetch → reproject → shade → style, returning the styled raster path.

    Reuses cache at every stage.
    """
    from .themes import get_theme
    from .sources import resolve_source
    from .cache import ensure_cache_dir
    from .pipeline.reproject import reproject_to_4326, needs_reproject
    from .pipeline.hillshade import generate_grayscale, generate_composite, ShadingMode
    from .pipeline.style import apply_style

    theme = get_theme(theme_name)
    if theme is None:
        raise click.BadParameter(f"Unknown theme: {theme_name}", param_hint="--theme")

    cb = lambda msg: click.echo(msg)
    source = resolve_source(resolved_bbox, dem)

    # S3 push helper — pushes a completed stage file to the private intermediates
    # bucket if HILLGEN_S3_INTERMEDIATES env var is set (e.g. s3://bucket/prefix/).
    # Silently skips if boto3 unavailable or var not set.
    import os as _os
    _s3_intermediates = _os.environ.get("HILLGEN_S3_INTERMEDIATES", "").rstrip("/")

    def _push_intermediate(path: Path, stage: str):
        if not _s3_intermediates or not path.exists():
            return
        try:
            import boto3
            bucket, _, prefix = _s3_intermediates.replace("s3://", "").partition("/")
            key = f"{prefix}/{stage}/{path.name}" if prefix else f"{stage}/{path.name}"
            cb(f"  → S3 {_s3_intermediates}/{stage}/{path.name}")
            boto3.client("s3").upload_file(str(path), bucket, key)
        except Exception as e:
            cb(f"  ⚠ S3 push skipped: {e}")

    # DEM
    dem_dir = ensure_cache_dir("dem") / source.name
    dem_path = source.download(resolved_bbox, dem_dir, progress_cb=cb)
    _push_intermediate(dem_path, f"dem/{source.name}")

    # Resolve exaggeration (auto if needed)
    if exaggeration and str(exaggeration) != "auto":
        exag = float(exaggeration)
    elif theme.get_exaggeration_value():
        exag = theme.get_exaggeration_value()
    else:
        from .pipeline.auto_exag import compute_auto_exaggeration
        exag = compute_auto_exaggeration(dem_path)
        click.echo(f"Auto-exaggeration: {exag}x")

    # Reproject
    if needs_reproject(dem_path):
        reproj_dir = ensure_cache_dir("reprojected")
        reproj_path = reproj_dir / f"{dem_path.stem}_4326.tif"
        if not reproj_path.exists():
            reproject_to_4326(dem_path, reproj_path, progress_cb=cb)
            _push_intermediate(reproj_path, "reprojected")
        else:
            click.echo(f"Reproject cached: {reproj_path.name}")
        input_dem = reproj_path
    else:
        input_dem = dem_path

    # Hillshade
    hs_dir = ensure_cache_dir("hillshade")
    if theme.shading == "composite":
        weights = theme.composite_weights
        w_str = "-".join(str(w) for w in weights)
        hs_path = hs_dir / f"{input_dem.stem}_gray_composite_{w_str}_{exag}x.tif"
        if not hs_path.exists():
            generate_composite(input_dem, hs_path, exag, weights=weights, cache_dir=hs_dir, progress_cb=cb)
            _push_intermediate(hs_path, "hillshade")
            # Also push sub-layer grayscales (multi/igor/combined)
            for sub in hs_dir.glob(f"{input_dem.stem}_gray_*_{exag}x.tif"):
                if sub != hs_path:
                    _push_intermediate(sub, "hillshade")
        else:
            click.echo(f"Hillshade cached: {hs_path.name}")
    else:
        mode_str = "multi" if theme.shading == "multidirectional" else theme.shading
        mode = ShadingMode(mode_str)
        hs_path = hs_dir / f"{input_dem.stem}_gray_{mode.value}_{exag}x.tif"
        if not hs_path.exists():
            generate_grayscale(input_dem, hs_path, exag, mode=mode, progress_cb=cb)
            _push_intermediate(hs_path, "hillshade")
        else:
            click.echo(f"Hillshade cached: {hs_path.name}")

    # Style
    styled_dir = ensure_cache_dir("styled")
    styled_path = styled_dir / f"{input_dem.stem}_{theme.name}_{exag}x.tif"
    if not styled_path.exists():
        elev_dem = input_dem if theme.color_mode == "elevation" else None
        apply_style(hs_path, styled_path, theme, dem_path=elev_dem, progress_cb=cb)
        _push_intermediate(styled_path, "styled")
    else:
        click.echo(f"Styled cached: {styled_path.name}")

    return styled_path


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
        return bbox
    except ValueError as e:
        raise click.BadParameter(str(e), param_hint="--place")

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
    reproj_dir = ensure_cache_dir("reprojected")
    output = reproj_dir / f"{dem_path.stem}_4326.tif"

    if output.exists():
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
    if needs_reproject(dem_path):
        reproj_dir = ensure_cache_dir("reprojected")
        reproj_path = reproj_dir / f"{dem_path.stem}_4326.tif"
        if not reproj_path.exists():
            reproject_to_4326(dem_path, reproj_path, progress_cb=cb)
        else:
            click.echo(f"Reproject cached: {reproj_path.name}")
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
        if output.exists():
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
        if output.exists():
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

    from .themes import get_theme
    from .sources import resolve_source
    from .cache import ensure_cache_dir
    from .pipeline.reproject import reproject_to_4326, needs_reproject
    from .pipeline.hillshade import generate_grayscale, generate_composite, ShadingMode
    from .pipeline.style import apply_style

    theme = get_theme(theme_name)
    if theme is None:
        raise click.BadParameter(f"Unknown theme: {theme_name}", param_hint="--theme")

    # Resolve exaggeration
    exag = exaggeration or theme.get_exaggeration_value() or 3.0
    cb = lambda msg: click.echo(msg)

    source = resolve_source(resolved_bbox, dem)

    # Ensure DEM
    dem_dir = ensure_cache_dir("dem") / source.name
    dem_path = source.download(resolved_bbox, dem_dir, progress_cb=cb)

    # Ensure reproject
    if needs_reproject(dem_path):
        reproj_dir = ensure_cache_dir("reprojected")
        reproj_path = reproj_dir / f"{dem_path.stem}_4326.tif"
        if not reproj_path.exists():
            reproject_to_4326(dem_path, reproj_path, progress_cb=cb)
        else:
            click.echo(f"Reproject cached: {reproj_path.name}")
        input_dem = reproj_path
    else:
        input_dem = dem_path

    # Ensure hillshade
    hs_dir = ensure_cache_dir("hillshade")
    if theme.shading == "composite":
        weights = theme.composite_weights
        w_str = "-".join(str(w) for w in weights)
        hs_path = hs_dir / f"{input_dem.stem}_gray_composite_{w_str}_{exag}x.tif"
        if not hs_path.exists():
            generate_composite(
                input_dem, hs_path, exag, weights=weights,
                cache_dir=hs_dir, progress_cb=cb,
            )
        else:
            click.echo(f"Hillshade cached: {hs_path.name}")
    else:
        mode_str = "multi" if theme.shading == "multidirectional" else theme.shading
        mode = ShadingMode(mode_str)
        hs_path = hs_dir / f"{input_dem.stem}_gray_{mode.value}_{exag}x.tif"
        if not hs_path.exists():
            generate_grayscale(input_dem, hs_path, exag, mode=mode, progress_cb=cb)
        else:
            click.echo(f"Hillshade cached: {hs_path.name}")

    # Apply style
    styled_dir = ensure_cache_dir("styled")
    styled_path = styled_dir / f"{input_dem.stem}_{theme.name}_{exag}x.tif"

    if styled_path.exists():
        click.echo(f"Cached: {styled_path}")
        return

    # For elevation mode, pass the reprojected DEM
    elev_dem = input_dem if theme.color_mode == "elevation" else None

    apply_style(hs_path, styled_path, theme, dem_path=elev_dem, progress_cb=cb)
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
def run(bbox, place, theme, exaggeration, dem, zoom, output_format, output,
        keep_intermediates, contribute, no_cache, s3_cache, buffer_deg, stop_after, start_from):
    """Full pipeline: fetch → reproject → shade → style → tile → package."""
    resolved_bbox = _resolve_bbox(bbox, place, buffer_deg=buffer_deg)
    cb = lambda msg: click.echo(msg)

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
        for stage in ["reprojected", "hillshade", "styled"]:
            stage_dir = cache_dir / stage
            if not stage_dir.exists():
                continue
            for f in stage_dir.rglob("*.tif"):
                if not s3_exists(stage, f.name):
                    click.echo(f"  Uploading {stage}/{f.name}...")
                    if s3_push(f, stage, f.name):
                        contributed += 1
                        click.echo(f"    ✓ uploaded")
                    else:
                        click.echo(f"    ✗ failed (check AWS credentials)")
                else:
                    click.echo(f"  {stage}/{f.name}: already in cache")
        if contributed:
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
    tiles_path = get_cache_dir() / "tiles" / tiles_dir_name
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
@click.argument("path", type=click.Path(exists=True))
@click.option("--dry-run", is_flag=True, help="Validate only, don't upload")
def publish(path, dry_run):
    """Publish a PMTiles file to the community library."""
    import struct

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

    # Upload to S3
    try:
        import boto3
    except ImportError:
        click.echo("Error: boto3 required for publish. pip install boto3")
        raise SystemExit(1)

    bucket = "scriptedrelief"
    key = f"tiles/{p.name}"

    click.echo(f"Uploading to s3://{bucket}/{key}...")
    s3 = boto3.client("s3")
    s3.upload_file(str(p), bucket, key, ExtraArgs={"ContentType": "application/x-protobuf"})
    click.echo(f"Published: https://scriptedrelief.com/{key}")

    # TODO: update catalog.json, invalidate CloudFront
    click.echo("Note: catalog.json update not yet implemented.")


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
