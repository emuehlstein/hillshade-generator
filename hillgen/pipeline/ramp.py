"""Resolve percentage-based colour ramps to absolute elevation values.

Why this exists
---------------
`gdaldem color-relief` resolves a `NN%` stop against the min/max of *the raster
it is handed*, not against anything global. For a single-tile map that is
convenient. For a build that is cut into chunks it is a correctness bug: each
chunk has its own elevation range, so each chunk gets a different
elevation -> colour mapping and the same altitude is painted a different colour
on either side of a chunk seam.

A corridor build that contains both a 400 m coastal-hills chunk and a chunk
holding Marys Peak (1249 m) will paint the coastal chunk with the full
green -> tan -> brown -> grey -> white sweep that the mountain chunk reserves
for its top 800 m. The seams are blatant.

Materialising the ramp once against a build-wide elevation range makes the
mapping byte-identical for every chunk.
"""

import re
from pathlib import Path
from typing import Optional, Tuple

__all__ = [
    "ramp_is_relative",
    "materialize_ramp",
    "dem_elevation_range",
]

# A ramp entry is "<value> R G B [A]" with optional comma separators.
# <value> may be a number, "NN%", "nv" (nodata) or "default".
_PCT = re.compile(r"^([+-]?\d+(?:\.\d+)?)%$")


def _entries(text: str):
    """Yield (raw_line, value_token_or_None, remainder) for each ramp line."""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            yield line, None, None
            continue
        parts = stripped.replace(",", " ").split()
        yield line, parts[0], parts[1:]


def ramp_is_relative(ramp_path: Path) -> bool:
    """True if the ramp uses any percentage stop."""
    text = Path(ramp_path).read_text()
    for _line, token, _rest in _entries(text):
        if token is not None and _PCT.match(token):
            return True
    return False


def materialize_ramp(
    ramp_path: Path,
    vmin: float,
    vmax: float,
    out_dir: Path,
    tag: Optional[str] = None,
) -> Path:
    """Rewrite a percentage ramp into absolute units over [vmin, vmax].

    Non-percentage entries ("nv", "default", bare numbers) and comments are
    copied through untouched, so a ramp that mixes absolute and relative stops
    keeps its absolute ones exactly where the author put them.

    Returns the path to the derived ramp. If the ramp has no percentage stops
    the original path is returned unchanged -- it is already absolute.
    """
    ramp_path = Path(ramp_path)
    if not ramp_is_relative(ramp_path):
        return ramp_path

    if not (vmax > vmin):
        raise ValueError(
            f"elevation range must be increasing, got min={vmin} max={vmax}"
        )

    span = float(vmax) - float(vmin)
    out_lines = [
        f"# Derived from {ramp_path.name} by hillgen.",
        f"# Percentage stops resolved against an absolute range of "
        f"{vmin:.3f}..{vmax:.3f} so that every chunk of a multi-chunk build",
        "# shares one elevation -> colour mapping. Do not hand-edit.",
    ]

    for line, token, rest in _entries(ramp_path.read_text()):
        if token is None:
            out_lines.append(line)
            continue
        m = _PCT.match(token)
        if not m:
            out_lines.append(line)
            continue
        pct = float(m.group(1))
        value = float(vmin) + (pct / 100.0) * span
        out_lines.append(f"{value:.4f} " + " ".join(rest))

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = tag or f"{vmin:.0f}_{vmax:.0f}"
    dest = out_dir / f"{ramp_path.stem}_abs_{suffix}.txt"
    dest.write_text("\n".join(out_lines) + "\n")
    return dest


def dem_elevation_range(dem_path: Path) -> Tuple[float, float]:
    """Actual (min, max) of band 1, ignoring nodata.

    Uses gdalinfo -mm rather than rasterio so this stays usable in the pure-GDAL
    code paths and never pulls numpy into a subprocess-only pipeline.
    """
    import json
    import subprocess

    result = subprocess.run(
        ["gdalinfo", "-json", "-mm", str(dem_path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"gdalinfo failed on {dem_path}: {result.stderr}")

    info = json.loads(result.stdout)
    band = info["bands"][0]
    cmin = band.get("computedMin")
    cmax = band.get("computedMax")
    if cmin is None or cmax is None:
        raise RuntimeError(f"gdalinfo returned no computed min/max for {dem_path}")
    return float(cmin), float(cmax)
