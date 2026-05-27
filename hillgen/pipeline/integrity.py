"""Pipeline integrity checks.

Centralized assertions that catch silent-corruption failure modes seen in
production runs (see issues #4 and #6). Each pipeline stage that produces a
large raster should call these against its output before declaring success
so a bad file never gets cached, contributed to S3, or fed to downstream
stages.

The checks are intentionally cheap — a single 64x64 read for nodata, two
512x512 reads for block variance — so they're safe to run unconditionally.

If ``rasterio`` is unavailable (e.g. inside a lightweight test environment)
the checks become no-ops rather than failing the run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional


def assert_has_data(path: Path, sample_size: int = 64) -> None:
    """Raise ``RuntimeError`` if ``path`` is entirely nodata in a center sample.

    GDAL utilities (notably ``gdalwarp``) can exit 0 while producing an
    all-nodata raster — for example when the temp volume runs out of space
    mid-write (issue #4). A center-window read is a cheap canary for this
    class of corruption.
    """
    try:
        import rasterio
        from rasterio.windows import Window
    except ImportError:
        return

    with rasterio.open(path) as src:
        size = min(sample_size, src.width, src.height)
        if size <= 0:
            return
        window = Window(
            max(0, src.width // 2 - size // 2),
            max(0, src.height // 2 - size // 2),
            size,
            size,
        )
        sample = src.read(1, window=window)
        nodata = src.nodata if src.nodata is not None else -9999
        if not (sample != nodata).any():
            raise RuntimeError(
                f"{path.name} is entirely nodata in a {size}x{size} center "
                f"sample — likely a disk-space or write-failure issue in "
                f"{path.parent} (see issue #4)."
            )


def assert_block_variance(
    path: Path,
    block: int = 512,
    band: int = 1,
    std_threshold: float = 1.0,
) -> None:
    """Raise if the first and last ``block``-sized windows are byte-identical.

    Detects the "repeated tile pattern" failure mode (issue #6) where a
    chunked writer's window offsets collapse and every block in the file
    ends up holding the same data. The check only runs when the raster is
    large enough for two non-overlapping windows — small outputs are a no-op.
    """
    try:
        import rasterio
        from rasterio.windows import Window
        import numpy as np
    except ImportError:
        return

    with rasterio.open(path) as src:
        if src.height < block * 4 or src.width < block * 4:
            return
        w0 = Window(0, 0, block, block)
        w1 = Window(src.width - block, src.height - block, block, block)
        b0 = src.read(band, window=w0)
        b1 = src.read(band, window=w1)
        if (
            b0.shape == b1.shape
            and np.array_equal(b0, b1)
            and float(b0.std()) < std_threshold
        ):
            raise RuntimeError(
                f"{path.name} appears corrupt: first and last {block}x{block} "
                f"blocks are identical and near-uniform "
                f"(see issue #6 — repeated tile pattern)."
            )


def validate_raster(
    path: Path,
    *,
    check_data: bool = True,
    check_block_variance: bool = False,
    band: int = 1,
) -> None:
    """Convenience wrapper running the enabled checks against ``path``."""
    if check_data:
        assert_has_data(path)
    if check_block_variance:
        assert_block_variance(path, band=band)
