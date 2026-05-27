"""Pipeline orchestrator: fetch → reproject → shade → style.

This module hosts the single ``ensure_styled`` entry point used by the
``hillgen run`` command (and reusable from notebooks / tests). The function
walks every pipeline stage, reusing cached output when present and falling
back to an S3 read-through cache before recomputing.

Splitting this out of ``cli.py`` keeps the Click layer thin and lets
downstream code call the pipeline without re-implementing stage wiring.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from .. import cache_s3
from ..cache import ensure_cache_dir

ProgressCb = Callable[[str], None]


def cache_lookup(
    stage: str,
    local_path: Path,
    *,
    allow_s3: bool = True,
    progress_cb: Optional[ProgressCb] = None,
) -> bool:
    """Return True if ``local_path`` is already populated.

    Resolution order:
        1. Local cache hit — return True immediately.
        2. S3 read-through (``cache_s3.try_pull``) when ``allow_s3`` is true
           and the local file is missing. On success the file is materialized
           at ``local_path`` and True is returned.
        3. Otherwise False — caller must compute the file.

    Failures in the S3 step are non-fatal: ``try_pull`` returns False on any
    network/HTTP error so the pipeline simply falls through to recomputation.
    """
    if local_path.exists():
        return True

    if not allow_s3:
        return False

    if cache_s3.try_pull(stage, local_path.name, local_path):
        if progress_cb:
            progress_cb(f"S3 cache hit: {stage}/{local_path.name}")
        return True

    return False


def ensure_styled(
    resolved_bbox,
    dem: str,
    theme_name: str,
    exaggeration,
    *,
    allow_s3_pull: bool = True,
    progress_cb: Optional[ProgressCb] = None,
) -> Path:
    """Run fetch → reproject → shade → style, returning the styled raster path.

    Each stage consults the local cache first, then the public S3 read-through
    cache (unless ``allow_s3_pull`` is False), and only recomputes when both
    miss. Sub-layer composite hillshades are looked up individually so a
    partial cache hit still avoids the most expensive GDAL passes.
    """
    from ..themes import get_theme
    from ..sources import resolve_source
    from .reproject import reproject_to_4326, needs_reproject
    from .hillshade import generate_grayscale, generate_composite, ShadingMode
    from .style import apply_style

    theme = get_theme(theme_name)
    if theme is None:
        raise ValueError(f"Unknown theme: {theme_name}")

    cb: ProgressCb = progress_cb or (lambda _msg: None)
    source = resolve_source(resolved_bbox, dem)

    # ── DEM ────────────────────────────────────────────────────────────────
    # Sources own their own filename derivation, so we can't S3-prefetch
    # without knowing the name in advance. Source implementations are
    # responsible for honoring the local cache; the S3 read-through for DEMs
    # is opt-in per source (future work).
    dem_dir = ensure_cache_dir("dem") / source.name
    dem_path = source.download(resolved_bbox, dem_dir, progress_cb=cb)

    # ── Exaggeration ───────────────────────────────────────────────────────
    if exaggeration and str(exaggeration) != "auto":
        exag = float(exaggeration)
    elif theme.get_exaggeration_value():
        exag = theme.get_exaggeration_value()
    else:
        from .auto_exag import compute_auto_exaggeration
        exag = compute_auto_exaggeration(dem_path)
        cb(f"Auto-exaggeration: {exag}x")

    # ── Reproject ──────────────────────────────────────────────────────────
    if needs_reproject(dem_path):
        reproj_dir = ensure_cache_dir("reprojected")
        reproj_path = reproj_dir / f"{dem_path.stem}_4326.tif"
        if cache_lookup("reprojected", reproj_path,
                        allow_s3=allow_s3_pull, progress_cb=cb):
            cb(f"Reproject cached: {reproj_path.name}")
        else:
            reproject_to_4326(dem_path, reproj_path, progress_cb=cb)
        input_dem = reproj_path
    else:
        input_dem = dem_path

    # ── Hillshade ──────────────────────────────────────────────────────────
    hs_dir = ensure_cache_dir("hillshade")
    if theme.shading == "composite":
        weights = theme.composite_weights
        w_str = "-".join(str(w) for w in weights)
        hs_path = hs_dir / f"{input_dem.stem}_gray_composite_{w_str}_{exag}x.tif"

        if cache_lookup("hillshade", hs_path,
                        allow_s3=allow_s3_pull, progress_cb=cb):
            cb(f"Hillshade cached: {hs_path.name}")
        else:
            # Opportunistically pre-pull sub-layers (multi/igor/combined) so
            # generate_composite reuses them rather than recomputing each.
            if allow_s3_pull:
                for sub in ("multi", "igor", "combined"):
                    sub_path = hs_dir / f"{input_dem.stem}_gray_{sub}_{exag}x.tif"
                    cache_lookup("hillshade", sub_path,
                                 allow_s3=True, progress_cb=cb)
            generate_composite(
                input_dem, hs_path, exag,
                weights=weights, cache_dir=hs_dir, progress_cb=cb,
            )
    else:
        mode_str = "multi" if theme.shading == "multidirectional" else theme.shading
        mode = ShadingMode(mode_str)
        hs_path = hs_dir / f"{input_dem.stem}_gray_{mode.value}_{exag}x.tif"

        if cache_lookup("hillshade", hs_path,
                        allow_s3=allow_s3_pull, progress_cb=cb):
            cb(f"Hillshade cached: {hs_path.name}")
        else:
            generate_grayscale(input_dem, hs_path, exag, mode=mode, progress_cb=cb)

    # ── Style ──────────────────────────────────────────────────────────────
    styled_dir = ensure_cache_dir("styled")
    styled_path = styled_dir / f"{input_dem.stem}_{theme.name}_{exag}x.tif"
    if cache_lookup("styled", styled_path,
                    allow_s3=allow_s3_pull, progress_cb=cb):
        cb(f"Styled cached: {styled_path.name}")
    else:
        elev_dem = input_dem if theme.color_mode == "elevation" else None
        apply_style(hs_path, styled_path, theme, dem_path=elev_dem, progress_cb=cb)

    return styled_path
