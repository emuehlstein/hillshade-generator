# hillgen

Generate stylized hillshade and relief map tiles from real-world terrain data.

🌐 [**scriptedrelief.com**](https://scriptedrelief.com) — Browse the tile library

```bash
hillgen run --place "Artist Point, WA" --theme alpine-glacier --zoom 10-14
```

---

## Install

### Homebrew (macOS / Linux)

```bash
brew tap emuehlstein/hillshade
brew install hillgen
```

### pip

```bash
pip install hillgen
```

### One-liner installer

```bash
curl -fsSL https://raw.githubusercontent.com/emuehlstein/hillshade-generator/main/install.sh | bash
```

Checks for prerequisites (Python 3.10+, GDAL), installs hillgen, and adds it to your PATH.

### From source

```bash
git clone https://github.com/emuehlstein/hillshade-generator
cd hillshade-generator
pip install .
```

---

## Quick Start

```bash
# Generate a hillshade of Artist Point, WA
hillgen run --place "Artist Point, WA" --theme alpine-glacier --zoom 10-14

# Generate a dark hillshade of Mt. Rainier at high zoom
hillgen run --bbox "-121.85,46.72,-121.65,46.92" \
  --theme midnight --zoom 10-16 --output rainier.pmtiles
```

## CLI Reference

```
hillgen run        Full pipeline: fetch → reproject → shade → style → tile → package
hillgen fetch      Download DEM for a location or bbox
hillgen reproject  Reproject a cached DEM to EPSG:4326
hillgen shade      Generate grayscale hillshade from a DEM
hillgen style      Apply a color theme to a hillshade
hillgen tile       Cut a styled raster into XYZ tiles
hillgen package    Package tiles into MBTiles / PMTiles
hillgen themes     List available themes
hillgen sources    List available DEM sources
hillgen view       Start a local Leaflet tile viewer
hillgen publish    Upload a PMTiles file to scriptedrelief.com
hillgen cache      Manage local cache (status / clean / pull)
hillgen auth       Inspect contributor authentication
hillgen version    Show version and environment info
```

## Dependencies

- **Python 3.10+**
- **GDAL 3.6+** — `brew install gdal` (macOS) / `apt install gdal-bin python3-gdal` (Linux)
- **rasterio**, **numpy**, **click**, **requests** — installed automatically via pip
- **pmtiles CLI** (optional, for PMTiles output) — `brew install pmtiles`

---

## What It Does

1. **Acquires** terrain data from public DEM sources (USGS 3DEP, Copernicus, SRTM, state LiDAR programs)
2. **Processes** with configurable vertical exaggeration, shading modes, and composite blending
3. **Styles** using a theme system: 20+ built-in themes, easy JSON-based custom themes
4. **Outputs** PMTiles (serverless web maps), MBTiles (offline/ATAK), and reusable intermediates

---

## Full Install Options

### Manual (macOS)

```bash
brew install gdal python3
git clone https://github.com/emuehlstein/hillshade-generator.git
cd hillshade-generator
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
hillgen version
```

### Legacy one-liner (macOS/Linux)
```bash
curl -fsSL https://raw.githubusercontent.com/emuehlstein/hillshade-generator/main/install.sh | bash
```



### Generate Your First Hillshade

```bash
# By bounding box
hillgen run --bbox "-105.35,39.6,-105.15,39.8" --theme midnight

# By named place (geocodes automatically)
hillgen run --place "Mt. St. Helens" --theme desert-sun --zoom 8-16

# Add a buffer around the area (degrees, ~111km per degree)
hillgen run --place "Mt. Hood" --buffer 0.15 --theme alpine-glacier
```

### Browse Themes

```bash
hillgen themes                     # list all themes
hillgen themes --show midnight     # details + preview link
hillgen themes --tag elevation     # filter by tag
```

### Step-by-Step Control

Each pipeline stage is a standalone subcommand. Run one at a time or let `run` do them all:

```bash
# Just download and cache the DEM (no processing)
hillgen fetch --place "Mt. Hood" --dem usgs-3dep-10m

# Generate grayscale hillshade only (the expensive step)
hillgen shade --place "Mt. Hood" --exaggeration 9

# Apply a theme to an existing hillshade (seconds)
hillgen style --place "Mt. Hood" --theme midnight --exaggeration 9

# Re-tile at a different zoom without re-processing
hillgen tile --place "Mt. Hood" --theme midnight --zoom 8-18

# Package tiles into MBTiles/PMTiles
hillgen package --place "Mt. Hood" --theme midnight
```

Each subcommand picks up from the cache — if a previous stage's output exists, it's reused automatically.

### Preview Locally

```bash
hillgen view ./output/ --port 9999   # opens a local Leaflet viewer
```

---

## Themes

Themes are the heart of Hillgen. Each theme bundles shading mode, color ramp, exaggeration strategy, and blending parameters into a single named preset.

### Built-in Themes

| Theme | Style | Best For |
|-------|-------|----------|
| `midnight` | Deep blue-black with subtle terrain | Dark basemaps, overlays |
| `daylight` | Warm grey with soft shadows | Light basemaps, print |
| `tactical` | Muted green-brown, high contrast | Military/outdoor overlays |
| `terrain` | Hypsometric tints (green→brown→white) | Educational, reference |
| `simmon` | Robert Simmon's composite technique | Publication-quality relief |
| `flat-terrain` | Heavy exaggeration for subtle landscapes | Great Plains, coastal, IL |
| `desert-sun` | Rust canyons → sandy mesas → white peaks | Arid landscapes |
| `alpine-glacier` | Deep indigo → teal → icy white | Volcanic peaks, heavy relief |
| `infrared` | False-color thermal palette | Dramatic visualization |
| `vivid` | Saturated blue→green→orange→red | Maximum feature contrast |
| `cool` | Desaturated blue-grey, cartographic | Professional base layers |
| `grayscale` | Pure hillshade, no color | Custom coloring base layer |

### Custom Themes

Create a JSON file:

```json
{
  "name": "my-custom-theme",
  "description": "A warm amber hillshade",
  "ramp": "path/to/ramp.txt",
  "shading": "composite",
  "composite_weights": [0.6, 0.3, 0.1],
  "exaggeration": "auto",
  "terrain_type": "auto",
  "aspect_blend": 0.1,
  "tags": ["warm", "custom"]
}
```

```bash
hillgen run --place "Grand Canyon" --theme ./my-custom-theme.json
```

### Color Ramps

Color ramps are GDAL color-relief format (one elevation→RGBA mapping per line). Drop a `.txt` file in `themes/ramps/` or reference an absolute path in your theme JSON.

```
0%   20  20  40  255
25%  40  60  100 255
50%  80  90  110 255
75%  140 145 155 255
100% 220 220 230 255
```

### Submit a Theme

We welcome community themes! See [CONTRIBUTING.md](CONTRIBUTING.md) for the submission process — essentially: add your ramp file + theme JSON, include a sample render, and open a PR.

---

## DEM Sources

Hillgen auto-selects the best available DEM source for your area, or you can specify one explicitly.

| Source | ID | Resolution | Coverage | Auto-select |
|--------|----|-----------|----------|-------------|
| **NPS SfM Rainier 2021** | `nps-sfm-rainier-2021` | 0.67m | Mt. Rainier NP | ✅ |
| **Illinois ISGS / ILHMP LiDAR** | `isgs-ilhmp` | 0.3m | Illinois (102 counties) | ✅ |
| **Indiana IGIC LiDAR** | `igic-indiana-lidar` | 0.76m | Indiana (92 counties) | ✅ |
| **Wisconsin DNR LiDAR** | `wi-dnr-lidar` | 1m | Wisconsin | ✅ |
| **USGS 3DEP 1/3 arc-sec** | `usgs-3dep-10m` | ~10m | CONUS / AK / HI | ✅ (fallback) |
| **Local file** | (path) | Any | Any | — |

### Wisconsin DNR LiDAR (`wi-dnr-lidar`)

High-resolution 1m LiDAR-derived DEM from the Wisconsin DNR ArcGIS ImageServer.
Auto-selected for any bbox fully within Wisconsin.

- **Coverage:** Wisconsin only
- **Resolution:** 1m native (EPSG:3071), resampled to requested zoom
- **Units:** Meters, NAVD88
- **Source:** <https://dnrmaps.wi.gov/arcgis_image/rest/services/DW_Elevation/EN_DEM_from_LiDAR/ImageServer>

```bash
# Auto-selected when your area is in Wisconsin
hillgen run --place "Kettle Moraine Southern Unit" --theme simmon --zoom 10-18

# Or request explicitly
hillgen run --place "Kettle Moraine Southern Unit" --dem wi-dnr-lidar --theme simmon --zoom 10-18
```

```bash
# Auto-select (picks best available resolution)
hillgen run --place "Denali" --theme alpine-glacier

# Force a specific source
hillgen run --bbox "..." --dem usgs-3dep-1m --theme midnight

# Use a local DEM file
hillgen run --dem ./my-dem.tif --theme simmon
```

### Adding DEM Sources

DEM sources are defined as catalog entries. New sources can be added by implementing the `DEMSource` interface in [hillgen/sources/base.py](hillgen/sources/base.py) and registering them in [hillgen/sources/__init__.py](hillgen/sources/__init__.py). See the existing sources for reference implementations.

---

## Output Formats

| Format | Use Case | Default |
|--------|----------|---------|
| **PMTiles** | Serverless web maps (S3/CloudFront, no tile server) | ✅ |
| **MBTiles** | Offline maps, ATAK, mbtileserver | ✅ |
| **Directory** | XYZ tile directory (`{z}/{x}/{y}.png`) | `--format dir` |
| **GeoTIFF** | Intermediate styled raster (for GIS workflows) | `--keep-intermediates` |

```bash
# PMTiles only (default)
hillgen run --place "Yosemite" --theme simmon

# MBTiles for ATAK
hillgen run --place "Yosemite" --theme tactical --format mbtiles

# Both
hillgen run --place "Yosemite" --theme simmon --format pmtiles,mbtiles

# Keep intermediates locally (reprojected DEM, grayscale hillshade, styled raster)
hillgen run --place "Yosemite" --theme simmon --keep-intermediates

# Skip the public cache (fully offline)
hillgen run --place "Yosemite" --theme simmon --no-cache
```

---

## Sharing & Publishing

### Public Intermediate Cache

Hillgen ships with a built-in public cache at `s3://scriptedrelief-data/`. Every run automatically checks the cache before downloading or processing — **no config, no auth, no AWS account needed.**

The source DEMs are all public domain (USGS, Copernicus, SRTM), so the derived intermediates are too. The more people use Hillgen, the fuller the cache gets, and the faster it is for everyone.

```bash
# This just works — pulls cached intermediates automatically
hillgen run --place "Crater Lake" --theme midnight

# Second run with a different theme reuses the cached DEM and reprojection
hillgen run --place "Crater Lake" --theme alpine-glacier

# Fully offline (skip cache reads)
hillgen run --place "Crater Lake" --theme midnight --no-cache

# Point at your own private S3 bucket instead
hillgen run --place "Crater Lake" --theme midnight --s3-cache s3://my-bucket/
```

The cache is read-through with deterministic keys — same area + source + parameters = same cache key. If anyone has ever generated Crater Lake at 3x exaggeration, you skip the DEM download and reprojection entirely and start from the cached grayscale hillshade.

### Contribute Your Intermediates

Help the community by sharing your cached intermediates back:

```bash
# Generate AND upload your intermediates to the public cache
hillgen run --place "Denali" --theme midnight --contribute
```

`--contribute` is on by default. No AWS credentials needed — authentication uses your GitHub account via the [`gh` CLI](https://cli.github.com/), and uploads use short-lived presigned URLs handed out by a small Lambda broker. Run `hillgen auth status` to verify your token, and see [CONTRIBUTING.md](CONTRIBUTING.md) for the one-time allowlist setup. Full architecture is in [docs/submission.md](docs/submission.md).

### Publish to scriptedrelief.com

Push your locally-generated hillshade to the public library:

```bash
hillgen publish ./output/crater-lake-alpine-glacier.pmtiles
```

The CLI validates your file locally, uploads it to staging, and opens a GitHub PR for review. Once merged, tiles go live on scriptedrelief.com automatically. See [docs/submission.md](docs/submission.md) for the full submission and validation process.

### Community Library

The web viewer at [scriptedrelief.com](https://scriptedrelief.com) lets you:
- Pan/zoom any published hillshade
- Switch themes on the same region
- Compare exaggeration levels
- Download MBTiles for offline use

---

## Architecture

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full system design, including:

- Processing pipeline stages
- Caching strategy (DEM → grayscale → styled → tiled)
- Theme system internals
- DEM source catalog design

See [docs/submission.md](docs/submission.md) for how community contributions (intermediates and PMTiles) are submitted, validated, and promoted.

---

## Project Lineage

Hillgen is the successor to [ilhmp](https://github.com/emuehlstein/illinois-hillshade-gen) (Illinois Height Modernization Project), which started as an Illinois-specific hillshade generator for ATAK offline map packages. The lessons learned from generating 23GB of tiles across 15+ regions informed this broader, source-agnostic platform.

Key improvements over ilhmp:
- **Global DEM support** — not locked to Illinois/ISGS
- **Theme-first design** — themes are portable, shareable, and the primary user-facing concept
- **PMTiles-native** — serverless delivery as the default, not an afterthought
- **Community library** — shared public gallery at scriptedrelief.com
- **One-liner CLI** — `--place` geocoding, auto DEM selection, zero config for simple cases
- **Local-first** — runs entirely on your machine with no cloud dependencies

---

## License

MIT

---

## Links

- **Website:** [scriptedrelief.com](https://scriptedrelief.com)
- **GitHub:** [emuehlstein/hillshade-generator](https://github.com/emuehlstein/hillshade-generator)
- **ilhmp (predecessor):** [emuehlstein/illinois-hillshade-gen](https://github.com/emuehlstein/illinois-hillshade-gen)
