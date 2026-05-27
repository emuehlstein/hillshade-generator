# Architecture

## System Overview

Hillgen is a local-first hillshade generation tool with an optional community library.

1. **Local CLI** — run on your Mac/Linux machine
2. **Community library** — scriptedrelief.com hosts published tiles for browsing

```
┌─────────────────────────────────────────────────────────────┐
│                      User's Machine                         │
│                                                             │
│  hillgen run --place "Crater Lake" --theme ...      │
│       │                                                     │
│       ▼                                                     │
│  ┌─────────┐   ┌──────────┐   ┌───────────────────────┐   │
│  │ Resolve │──▶│ Pipeline │──▶│ Output                │   │
│  │  Area   │   │  Engine  │   │ .pmtiles  .mbtiles    │   │
│  └─────────┘   └──────────┘   └───────────────────────┘   │
│       │                                                     │
│       ▼                                                     │
│  ~/.hillgen/cache/        (reusable intermediates)  │
│       │                                                     │
│       ▼  optional                                           │
│  hillgen publish ──▶ scriptedrelief.com             │
└─────────────────────────────────────────────────────────────┘
```

---

## Processing Pipeline

Every hillshade generation follows the same six-stage pipeline. Each stage produces a reusable intermediate that can be cached locally.

```
Stage 0: Acquire DEM
    │  Download tiles from USGS/Copernicus/SRTM/local file
    │  Merge, clip to bounds, fill nodata voids
    │
    ▼
Stage 1: Reproject
    │  Warp to EPSG:4326 (WGS84)
    │  Bilinear resampling
    │  ★ Cache key: {area}_{source}_{resolution}_4326.tif
    │
    ▼
Stage 2: Compute Hillshade
    │  gdaldem hillshade — one pass per shading mode:
    │    - standard: single azimuth (315°)
    │    - multidirectional: GDAL -multidirectional
    │    - igor: oblique-angle variant
    │    - combined: multi-light approach
    │  Apply vertical exaggeration (fixed or auto-computed)
    │  Each pass cached independently:
    │    ★ {area}_{source}_gray_multi_{exag}x.tif
    │    ★ {area}_{source}_gray_igor_{exag}x.tif
    │    ★ {area}_{source}_gray_combined_{exag}x.tif
    │  Composite themes blend cached sub-layers (cheap numpy math):
    │    ★ {area}_{source}_gray_composite_{weights}_{exag}x.tif
    │
    ▼
Stage 3: Apply Theme
    │  Color ramp via gdaldem color-relief
    │  OR elevation-mapped coloring (ramp on DEM, modulated by hillshade)
    │  Optional aspect blending for depth cues
    │  ★ Cache key: {area}_{theme}_{exag}x.tif
    │
    ▼
Stage 4: Generate Tiles
    │  gdal2tiles.py → XYZ PNG tiles
    │  Configurable zoom range (default 10-16)
    │  Parallel generation (uses available cores)
    │  ★ Output: tile directory
    │
    ▼
Stage 5: Package
    │  Tile dir → MBTiles (sqlite, TMS scheme)
    │  MBTiles → PMTiles (single-file, HTTP range requests)
    │  Inject metadata (bounds, center, attribution)
    │  ★ Output: .mbtiles + .pmtiles
```

### Caching Hierarchy

The pipeline is designed around aggressive intermediate caching. The DEM is the most expensive artifact — everything downstream is increasingly cheap to regenerate.

```
Level 0: Raw DEM download                   ← minutes to hours, never recompute
  Level 1: Reprojected to EPSG:4326          ← minutes, reuse across all themes
    Level 2a: Shading sub-layers @ Nx        ← minutes each, cached per mode+exag
              (multi, igor, combined)            reuse across themes at same exag
    Level 2b: Blended composite              ← seconds (numpy weighted sum of 2a)
      Level 3: Styled raster                 ← seconds, cheap
        Level 4: MBTiles                     ← minutes, canonical format
          Level 5: PMTiles                   ← seconds, derived for web
```

**Reuse patterns:**
| Change | Reuses from |
|--------|-------------|
| Different theme, same exag + same shading mode | Level 2a (sub-layers) |
| Different composite weights, same exag | Level 2a (sub-layers, re-blend only) |
| Different theme, same exag, non-composite | Level 2a (single sub-layer) |
| Different exaggeration | Level 1 (reprojected DEM) |
| Different DEM source | Nothing — full reprocess |
| Different zoom range only | Level 3 (styled raster) |

The key insight: `gdaldem hillshade` is the expensive part of Stage 2. The blend is just a weighted sum of three arrays. By caching multi/igor/combined independently, changing composite weights or switching between a composite theme and a pure-multidirectional theme never re-runs `gdaldem`.

### Cache Storage

Two cache layers, same layout, checked in order:

1. **Local** `~/.hillgen/cache/` — always on, fastest
2. **Public S3** `s3://scriptedrelief-data/` — on by default, shared across all users

**No AWS credentials required.** The `scriptedrelief-data` bucket is public-read. The CLI fetches cached intermediates over anonymous HTTPS — same as downloading a DEM from USGS, just faster because it's already reprojected and ready.

The lookup flow for each pipeline stage:
1. Check local cache → hit? Use it.
2. Check S3 cache → hit? Download to local cache, use it.
3. Miss everywhere → compute, save to local cache.

We populate the S3 cache from our own generation runs. As coverage grows, more users get cache hits on their first run. Users can also point at their own private bucket with `--s3-cache s3://my-bucket/` or skip S3 entirely with `--no-cache`.

```
cache/                                   # same layout locally and in S3
├── dem/
│   ├── usgs-3dep/{area}_merged.tif
│   ├── copernicus/{area}_merged.tif
│   └── local/{hash}.tif
├── reprojected/
│   └── {area}_{source}_4326.tif
├── hillshade/
│   ├── {area}_{source}_gray_multi_{exag}x.tif
│   ├── {area}_{source}_gray_igor_{exag}x.tif
│   ├── {area}_{source}_gray_combined_{exag}x.tif
│   └── {area}_{source}_gray_composite_{weights}_{exag}x.tif
├── styled/
│   └── {area}_{theme}_{exag}x.tif
└── manifest.json              # index of what's cached, checksums, timestamps
```

Cache keys are deterministic — same area + source + params = same key. Two people generating Cook County dark 9x will produce and reuse the same cached artifacts.

### S3 Layout

```
s3://scriptedrelief-data/                      # public-read — shared cache + artifacts
├── cache/                               # mirrors local cache layout
│   ├── dem/
│   ├── reprojected/
│   ├── hillshade/                       # sub-layers: multi, igor, combined, composite
│   └── styled/
├── mbtiles/                             # final mbtiles outputs
│   └── {area}-{theme}-{exag}x.mbtiles
└── catalog.json                         # master index of all outputs

s3://scriptedrelief/                     # public — web serving
├── tiles/
│   └── {area}-{theme}-{exag}x.pmtiles
├── catalog.json                         # layer index for the viewer
├── index.html
└── assets/
```

---

## DEM Source System

### Catalog Architecture

DEM sources are defined as catalog entries, each implementing a standard interface:

```python
class DEMSource:
    name: str              # e.g. "usgs-3dep-10m"
    resolution_m: float    # native resolution in meters
    coverage: Geometry     # geographic coverage polygon
    priority: int          # higher = preferred when multiple sources cover area

    def covers(self, bbox: BBox) -> bool:
        """Does this source cover the requested area?"""

    def download(self, bbox: BBox, output_dir: Path) -> Path:
        """Download DEM tiles, merge, return path to merged GeoTIFF."""
```

### Source Selection

When the user doesn't specify `--dem`, the resolver:

1. Finds all sources whose coverage intersects the requested bbox
2. Sorts by `priority` (highest first), then `resolution_m` (smallest first)
3. Picks the best match
4. Falls back to the next source if download fails

### Built-in Sources

| Source ID | Provider | Resolution | Coverage | Priority |
|-----------|----------|-----------|----------|----------|
| `nps-sfm-rainier-2021` | NPS SfM | 0.67m | Mt. Rainier NP | 95 |
| `igic-indiana-lidar` | Indiana IGIC | 0.76m | Indiana (92 counties) | 92 |
| `wi-dnr-lidar` | Wisconsin DNR | 1m | Wisconsin | 90 |
| `isgs-ilhmp` | Illinois ISGS | 0.3m | Illinois (102 counties) | 88 |
| `usgs-3dep-10m` | USGS TNM | ~10m (1/3") | CONUS + AK/HI | 80 |

### Adding More Sources

Additional state LiDAR programs and global DEM providers (Copernicus, SRTM, finer USGS 3DEP tiers)
can be added by implementing the `DEMSource` interface in `hillgen/sources/base.py` and registering
them in `hillgen/sources/__init__.py`. See the existing implementations for reference.

---

## Theme System

### Theme Definition

A theme is a named bundle of all visual parameters:

```python
@dataclass
class Theme:
    name: str
    description: str

    # Color
    ramp: str               # Name of ramp file or path
    color_mode: str          # "ramp" | "elevation" | "tint"

    # Shading
    shading: str             # "standard" | "multidirectional" | "composite"
    composite_weights: tuple # (multi, igor, combined) weights
    azimuth: float
    altitude: float

    # Exaggeration
    exaggeration: str        # "auto" or fixed number
    terrain_type: str        # Hint for auto-exag: flat, rolling, mountain, auto

    # Depth
    aspect_blend: float      # 0.0 = none, 0.1 = subtle aspect coloring

    # Metadata
    tags: list[str]
    default_zoom: str
```

### Auto-Exaggeration

When `exaggeration: "auto"`, the system computes an appropriate value from DEM statistics:

```
terrain_type    exag_range    logic
─────────────────────────────────────
flat            6–12x         elevation_range < 100m → 9x
rolling         3–6x          100m < range < 500m → 4x
mountain        1.5–3x        range > 500m → 2x
urban-lidar     8–15x         1m resolution + urban bbox
auto            detect        compute from DEM min/max/stdev
```

This is critical for usability — flat terrain (Chicago, Florida) needs 9x+ to show any features, while the Rockies look absurd at 3x.

### Color Ramp Format

Ramps use GDAL color-relief syntax. Two modes:

**Percentage-based** (auto-scales to DEM range):
```
0%    20  20  40  255
25%   40  60  100 255
50%   80  90  110 255
75%   140 145 155 255
100%  220 220 230 255
```

**Absolute elevation** (fixed breakpoints):
```
0     20  20  40  255
500   40  80  60  255
1000  120 110 80  255
2000  180 170 160 255
4000  240 240 245 255
```

### Theme Discovery

```
~/.hillgen/themes/         # user custom themes
./themes/                          # project-local themes
hillgen/themes/builtin/    # package built-ins
```

Priority: project-local > user dir > built-in (allows overriding built-ins).

---

## CLI Design

### Command Structure

```
hillgen
├── run          Full pipeline (acquire → package) — convenience wrapper
├── fetch        Download + cache DEM only (no processing)
├── reproject    Reproject a cached DEM to EPSG:4326
├── shade        Generate grayscale hillshade from reprojected DEM
├── style        Apply theme (color ramp + blending) to a hillshade
├── tile         Cut styled raster into tiles (XYZ directory)
├── package      Pack tile directory into MBTiles and/or PMTiles
├── themes       List/show/validate themes
├── sources      List available DEM sources
├── view         Local Leaflet tile viewer
├── publish      Upload to community library
├── cache        Manage local + S3 cache
│   ├── status   Show cache size and contents
│   ├── clean    Remove stale intermediates
│   └── pull     Pre-fetch DEM for an area
└── version      Show version and GDAL info
```

`run` is sugar — it calls `fetch → reproject → shade → style → tile → package` in sequence, skipping any stage with a valid cache hit. Each subcommand is independently useful:

```bash
# Just grab the DEM and cache it (planning, bandwidth window)
hillgen fetch --place "Mt. Hood" --dem usgs-3dep-10m

# Generate the grayscale hillshade (bottleneck step, want to inspect)
hillgen shade --place "Mt. Hood" --exaggeration 9

# Try a different theme on an existing hillshade (seconds, no re-shade)
hillgen style --place "Mt. Hood" --theme midnight --exaggeration 9

# Re-tile at different zoom range without re-styling
hillgen tile --place "Mt. Hood" --theme midnight --exaggeration 9 --zoom 8-18

# Full pipeline in one shot
hillgen run --place "Mt. Hood" --theme midnight
```

Each subcommand picks up from the cache — if the previous stage's output exists, it uses it. If not, it runs the prerequisite stages first. So `hillgen style` with no cached hillshade will fetch + reproject + shade automatically, then apply the theme.

**Pipeline stage flags for `run`:**

`run` also accepts stage-limiting flags for when you want partial execution without remembering subcommand names:

```bash
hillgen run --place "Mt. Hood" --theme midnight --stop-after shade
hillgen run --place "Mt. Hood" --theme midnight --start-from style
```

### Area Specification

Two ways to define the target area (exactly one required):

| Flag | Input | Resolution |
|------|-------|-----------|
| `--bbox W,S,E,N` | Bounding box coordinates | Exact |
| `--place "NAME"` | Place name → geocoded via Nominatim | Geocode + buffer |

`--place` uses OpenStreetMap Nominatim (free, no API key). Area features (cities, parks) use Nominatim's bounding box directly; point features (mountains, peaks) get an automatic buffer of ~0.1° (~11km). Override the buffer with `--buffer 0.15` for extra surrounding context.

### Output Control

```bash
# Default: PMTiles in ./output/
hillgen run --place "..." --theme midnight

# Custom output
hillgen run --place "..." --theme midnight \
  --output ~/maps/rainier.pmtiles

# Multiple formats
hillgen run --place "..." --theme midnight \
  --format pmtiles,mbtiles

# Keep intermediates for debugging/reuse
hillgen run --place "..." --theme midnight \
  --keep-intermediates
```

---

## Publishing

The `publish` command takes a locally-generated hillshade and makes it available on scriptedrelief.com:

```bash
hillgen publish ./output/crater-lake-alpine-glacier.pmtiles
```

**What it does:**
1. Validates the PMTiles file (bounds, zoom levels, metadata)
2. Uploads to `s3://scriptedrelief/tiles/`
3. Updates `catalog.json` with the new layer entry
4. Triggers a CloudFront invalidation so the new tile appears immediately

**Auth:** No AWS credentials needed for contributors. The CLI uses presigned URLs for uploads and opens a GitHub PR for review. See [submission.md](submission.md) for the full submission pipeline, validation checks, and infrastructure details.

**Intermediate sharing vs publishing:**
The S3 cache (`scriptedrelief-data`) is public-read — anyone running `hillgen run` automatically pulls cached intermediates with zero auth. Publishing puts the *final rendered PMTiles* into the separate `scriptedrelief` bucket for web viewing on scriptedrelief.com.

```
Local generation          S3 cache (public-read)       Public library
──────────────           ─────────────────────           ──────────────
run             ◀──────  scriptedrelief-data/cache/
                          (anonymous HTTPS reads)         
publish         ──────────────────────────────────▶  scriptedrelief/tiles/
```

---

## Web Viewer (scriptedrelief.com)

### Stack

- **Leaflet** — lightweight raster tile viewer
- **pmtiles.js** — PMTiles protocol for direct S3 range requests
- **CloudFront** — CDN + HTTPS
- Entirely static — no backend server

### Features

- Auto-populates layer list from `catalog.json`
- Theme switcher
- Exaggeration comparison slider
- Region search
- "Download MBTiles" link for offline users
- Mobile-responsive (bottom sheet controls on narrow screens)

### catalog.json Schema

```json
{
  "generated": "2026-05-18T...",
  "layers": [
    {
      "area": "crater-lake",
      "name": "Crater Lake",
      "theme": "alpine-glacier",
      "exaggeration": 3,
      "dem_source": "usgs-3dep-10m",
      "zoom": [8, 16],
      "bounds": [-122.25, 42.85, -121.95, 43.0],
      "center": [-122.1, 42.93],
      "pmtiles": "tiles/crater-lake-alpine-glacier-3x.pmtiles",
      "mbtiles_size_mb": 245,
      "tile_count": 48000,
      "generated_at": "2026-05-18T...",
      "source_attribution": "USGS 3DEP"
    }
  ]
}
```

---

## Migration from ilhmp

Hillgen inherits and generalizes ilhmp's battle-tested components:

| ilhmp Component | Hillgen Equivalent | Changes |
|---|---|---|
| `ilhmp run <county>` | `hillgen run --county <name> --state IL` | Generalized to any area |
| `ilhmp/themes.py` | `hillgen/themes/` | Same dataclass, expanded registry |
| `ilhmp/ramps/*.txt` | `hillgen/themes/ramps/` | Direct copy, add new ramps |
| `ilhmp/counties.py` | `hillgen/sources/isgs.py` | Becomes one DEM source of many |
| `ilhmp/tile.py` | `hillgen/pipeline/tiler.py` | Same gdal2tiles approach |
| ISGS-specific download | `hillgen/sources/` plugin system | Source-agnostic |
| `s3://ilhmp-dem-cache` | `s3://scriptedrelief-data/cache/` | Cleaner layout, same read-through strategy |

### What We Keep

- **Theme system** — the `Theme` dataclass, all 25+ built-in themes, color ramp files
- **Composite shading** — Simmon-inspired multi+igor+combined blending
- **Auto-exaggeration** — terrain-type-aware exaggeration tuning
- **S3 intermediate caching** — five-level hierarchy, now public-read so everyone benefits
- **PMTiles for web** — serverless delivery format
- **MBTiles for ATAK** — canonical offline format

### What We Fix

- **TMS/XYZ coordinate confusion** — ilhmp had bugs with `scheme=tms` metadata vs XYZ coordinates. Hillgen standardizes on XYZ everywhere, with TMS only as an explicit packaging option.
- **State-locked DEM sources** — ISGS was the only source. Now pluggable with auto-selection.
- **`--place` geocoding** — no more needing to look up bounding boxes manually.
- **Intermediate cleanup** — ilhmp left multi-GB intermediates on disk by default. Hillgen cleans up unless `--keep-intermediates`.
- **numpy 2.x compatibility** — proper dependency pinning avoids numpy 1.x/2.x ABI conflicts with system GDAL.

---

## Package Structure

```
hillshade-generator/
├── README.md
├── AGENTS.md                    # quick-reference for AI assistants
├── CONTRIBUTING.md
├── ROADMAP.md
├── install.sh
├── pyproject.toml
├── docs/
│   ├── ARCHITECTURE.md          # this file
│   └── submission.md            # contribute + publish pipelines
├── web/                         # scriptedrelief.com static site
│   ├── index.html
│   ├── gallery.html
│   ├── catalog.json
│   └── previews/
├── infra/
│   └── broker/                  # AWS SAM Lambda + API Gateway for --contribute
├── scripts/                     # deploy / IAM / comparison utilities
├── hillgen/                     # Python package
│   ├── __init__.py
│   ├── cli.py                   # Click CLI: run, fetch, reproject, shade, style,
│   │                            #            tile, package, themes, sources, view,
│   │                            #            publish, auth, cache, version
│   ├── cache.py                 # local filesystem cache helpers
│   ├── cache_s3.py              # S3 read-through + push (broker or direct)
│   ├── contribute_broker.py     # GitHub-auth client for the upload broker
│   ├── viewer.py                # local Leaflet tile preview server
│   ├── pipeline/
│   │   ├── orchestrator.py      # ensure_styled: walks all stages with cache reuse
│   │   ├── reproject.py         # EPSG:4326 warp
│   │   ├── hillshade.py         # gdaldem shading modes + composite blend
│   │   ├── style.py             # color ramp + aspect blending
│   │   ├── tiler.py             # gdal2tiles → XYZ tile dir
│   │   ├── packager.py          # MBTiles + PMTiles packaging
│   │   ├── auto_exag.py         # terrain-aware exaggeration
│   │   └── integrity.py         # raster sanity checks
│   ├── themes/
│   │   ├── registry.py          # Theme dataclass + built-in registrations + custom loader
│   │   └── ramps/               # GDAL color-relief ramp .txt files
│   ├── sources/
│   │   ├── base.py              # DEMSource abstract class + BBox + resolve_source
│   │   ├── usgs_3dep.py         # USGS TNM 1/3 arc-second downloader
│   │   ├── nps_sfm_rainier.py   # NPS Mt. Rainier SfM
│   │   ├── wi_dnr_lidar.py      # Wisconsin DNR ImageServer
│   │   ├── igic_indiana_lidar.py# Indiana IGIC
│   │   └── isgs_ilhmp.py        # Illinois ISGS / ILHMP (from ilhmp)
│   └── geo/
│       └── geocoder.py          # Nominatim place resolution
└── tests/
    ├── test_contribute_broker.py
    ├── test_igic_indiana_lidar.py
    └── test_wi_dnr_lidar.py
```

---

## Design Principles

1. **Themes are the UX.** Users think in themes, not GDAL flags. Every visual decision lives in a theme.
2. **Cache everything expensive.** DEMs take hours to download. Grayscale hillshades take minutes. Never recompute what you can cache.
3. **PMTiles first.** Serverless delivery is the default. MBTiles exists for offline/ATAK compatibility.
4. **One command, zero config.** `hillgen run --place "Mt. Hood" --theme midnight` should just work — downloading the DEM, picking the right resolution, and outputting PMTiles.
5. **Local-first.** Everything runs on your machine. Cloud generation is a future add-on, not a requirement.
6. **Portable themes.** A theme JSON + ramp file is everything someone needs to reproduce a style. Share them, submit them, fork them.
