"""
Theme registry — built-in themes and custom theme loading.

A theme captures everything needed to reproduce a specific visual style:
- Color ramp (or tint mode)
- Shading mode (multidirectional, composite, etc.)
- Exaggeration (fixed or auto with terrain-type hint)
- Composite weights
- Aspect blending

Ported from ilhmp/themes.py with identical Theme dataclass.
"""

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Ramp files ship with the package
RAMPS_DIR = Path(__file__).parent / "ramps"


@dataclass
class Theme:
    """A named preset capturing all visual parameters for hillshade generation."""
    name: str
    description: str

    # Color
    ramp: str = "dark"              # Name of ramp file (matches .txt in ramps/)
    color_mode: str = "ramp"        # 'ramp' (color-relief on hillshade) or 'elevation'

    # Shading
    shading: str = "multidirectional"  # standard, multidirectional, composite
    composite_weights: Tuple[float, ...] = (0.6, 0.3, 0.1)
    azimuth: float = 315.0
    altitude: float = 45.0

    # Exaggeration
    exaggeration: str = "auto"      # 'auto' or fixed number as string
    terrain_type: str = "auto"      # flat, rolling, mountain, urban-lidar, auto

    # Aspect blending (Simmon technique)
    aspect_blend: float = 0.0

    # Defaults
    default_zoom: str = "10-16"

    # Tags for filtering
    tags: List[str] = field(default_factory=list)

    def get_exaggeration_value(self) -> Optional[float]:
        if self.exaggeration == "auto":
            return None
        return float(self.exaggeration)

    def ramp_path(self) -> Path:
        """Resolve the ramp file path."""
        # Check if it's an absolute path or relative file
        p = Path(self.ramp)
        if p.exists():
            return p
        # Look in built-in ramps
        builtin = RAMPS_DIR / f"{self.ramp}.txt"
        if builtin.exists():
            return builtin
        raise FileNotFoundError(f"Ramp file not found: {self.ramp} (checked {builtin})")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["composite_weights"] = list(d["composite_weights"])
        return d


# ─── Built-in Themes ───────────────────────────────────────────

THEMES: Dict[str, Theme] = {}


def _register(theme: Theme) -> Theme:
    THEMES[theme.name] = theme
    return theme


# --- Core themes ---

_register(Theme(
    name="midnight",
    description="Deep blue-black with subtle terrain detail. Perfect as a dark basemap.",
    ramp="dark",
    shading="composite",
    composite_weights=(0.6, 0.3, 0.1),
    exaggeration="auto",
    tags=["dark", "basemap"],
))

_register(Theme(
    name="daylight",
    description="Warm grey with soft shadows. Clean light basemap for print or overlays.",
    ramp="light",
    shading="composite",
    composite_weights=(0.6, 0.3, 0.1),
    exaggeration="auto",
    tags=["light", "basemap"],
))

_register(Theme(
    name="tactical",
    description="Muted green-brown, high contrast. Military/outdoor overlay style.",
    ramp="tactical",
    shading="multidirectional",
    exaggeration="auto",
    tags=["tactical", "dark"],
))

_register(Theme(
    name="terrain",
    description="Hypsometric tints (green→brown→white). Educational reference style.",
    ramp="terrain",
    shading="composite",
    composite_weights=(0.6, 0.3, 0.1),
    exaggeration="auto",
    tags=["terrain", "cartographic"],
))

_register(Theme(
    name="simmon",
    description="Robert Simmon's composite technique. Publication-quality relief "
                "with multidirectional+igor+combined blending and aspect depth cues.",
    ramp="dark",
    shading="composite",
    composite_weights=(0.6, 0.3, 0.1),
    aspect_blend=0.1,
    exaggeration="auto",
    tags=["dark", "publication"],
))

_register(Theme(
    name="flat-terrain",
    description="Heavy 9x exaggeration for subtle landscapes — Great Plains, coastal, Illinois.",
    ramp="dark",
    shading="composite",
    composite_weights=(0.6, 0.3, 0.1),
    exaggeration="9",
    terrain_type="flat",
    tags=["dark", "flat"],
))

# --- Vivid themes ---

_register(Theme(
    name="vivid",
    description="Saturated blue→green→orange→red for maximum feature contrast.",
    ramp="vivid",
    shading="composite",
    composite_weights=(0.5, 0.3, 0.2),
    aspect_blend=0.14,
    exaggeration="auto",
    tags=["vivid", "dark"],
))

_register(Theme(
    name="cool",
    description="Desaturated blue-grey. Clean cartographic aesthetic.",
    ramp="cool",
    shading="composite",
    composite_weights=(0.5, 0.3, 0.2),
    aspect_blend=0.10,
    exaggeration="auto",
    tags=["cool", "cartographic"],
))

# --- Elevation-mapped themes ---

_register(Theme(
    name="vivid-elevation",
    description="Elevation-mapped vivid colors. Blue (low) → green → orange → red (high).",
    ramp="vivid-elev",
    color_mode="elevation",
    shading="composite",
    composite_weights=(0.5, 0.3, 0.2),
    aspect_blend=0.14,
    exaggeration="auto",
    tags=["vivid", "elevation"],
))

_register(Theme(
    name="cool-elevation",
    description="Elevation-mapped cool blue-grey. Deeper blue (low) → muted grey (high).",
    ramp="cool-elev",
    color_mode="elevation",
    shading="composite",
    composite_weights=(0.5, 0.3, 0.2),
    aspect_blend=0.10,
    exaggeration="auto",
    tags=["cool", "elevation"],
))

_register(Theme(
    name="alpine-glacier",
    description="Deep indigo valleys → teal forests → steel-blue ridges → icy white summit.",
    ramp="alpine-glacier",
    color_mode="elevation",
    shading="composite",
    composite_weights=(0.5, 0.3, 0.2),
    aspect_blend=0.12,
    exaggeration="auto",
    tags=["alpine", "elevation"],
))

# --- Experimental / landscape themes ---

for _n, _r, _desc in [
    ("magma", "magma", "Black → deep purple → red → orange → pale yellow summit"),
    ("infrared", "infrared", "False-color thermal: black → purple → cyan → green → yellow → red"),
    ("desert-sun", "desert-sun", "Deep rust canyons → sandy mesas → bleached white peaks"),
    ("aurora", "aurora", "Deep space black → midnight blue → electric teal → neon green → violet summit"),
    ("topo-classic", "topo-classic", "USGS hypsometric: deep green lowlands → tan → brown → grey → white"),
    ("ocean-depth", "ocean-depth", "Deep ocean blue at valleys → pale aqua at summit"),
    ("volcanic-ash", "volcanic-ash", "Charcoal base → warm ash grey → bone white summit"),
    ("toxic", "toxic", "Dark olive → poison green → acid yellow"),
    ("sunset-ridge", "sunset-ridge", "Deep violet night → rose → amber → pale gold summit"),
    ("forest-canopy", "forest-canopy", "Near-black forest floor → dark green → bright canopy → pale summit"),
]:
    _register(Theme(
        name=_n, description=_desc, ramp=_r,
        color_mode="elevation",
        shading="composite",
        composite_weights=(0.5, 0.3, 0.2),
        aspect_blend=0.12,
        exaggeration="auto",
        tags=["elevation", "experimental"],
    ))

_register(Theme(
    name="grayscale",
    description="Pure grayscale hillshade with no color. Useful as a base layer.",
    ramp="gray",
    shading="multidirectional",
    exaggeration="auto",
    tags=["gray", "base"],
))


# ─── API ────────────────────────────────────────────────────────

def get_theme(name: str) -> Optional[Theme]:
    """Get a theme by name, or load from JSON file path."""
    if name in THEMES:
        return THEMES[name]
    # Try loading as a file path
    p = Path(name)
    if p.exists() and p.suffix == ".json":
        return load_custom_theme(p)
    return None


def list_themes(tag: Optional[str] = None) -> List[Theme]:
    """List all themes, optionally filtered by tag."""
    themes = list(THEMES.values())
    if tag:
        themes = [t for t in themes if tag in t.tags]
    return themes


def load_custom_theme(path: Path) -> Theme:
    """Load a custom theme from a JSON file."""
    with open(path) as f:
        data = json.load(f)
    if "composite_weights" in data and isinstance(data["composite_weights"], list):
        data["composite_weights"] = tuple(data["composite_weights"])
    return Theme(**data)
