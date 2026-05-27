# Roadmap

Development plan for hillgen MVP. Each milestone produces something testable against real terrain before moving on.

**Test area:** Mt. St. Helens (~46.19, -122.18), bbox `-122.25,46.15,-122.10,46.25` — dramatic terrain, known from prior iso renders.

## Milestones

### M0: Skeleton
- [x] `pyproject.toml` with dependencies, entry point, metadata
- [x] `hillgen/cli.py` — Click group with `version` subcommand
- [x] `hillgen version` prints version + GDAL info
- **Test:** `pip install -e . && hillgen version`

### M1: Fetch
- [x] `hillgen fetch --bbox "-122.25,46.15,-122.10,46.25" --dem usgs-3dep-10m`
- [x] USGS 3DEP downloader (new, uses TNM S3 `current` URL pattern)
- [x] `DEMSource` base class + source registry + auto-resolution
- [x] Writes to `~/.hillgen/cache/dem/`
- [x] Tile caching (462MB raw tile cached, second run skips download)
- [x] Clips to exact bbox after download
- [x] `hillgen sources` lists available sources
- **Test:** valid GeoTIFF, `gdalinfo` shows correct bounds + CRS ✅
- **Result:** 1620×1080 px, 577-2535m elevation range, 5.4MB clipped

### M2: Reproject + Shade
- [x] `hillgen reproject --bbox ...` → EPSG:4326, bilinear
- [x] `hillgen shade --bbox ... --exaggeration 3 --shading composite` → grayscale hillshade
- [x] Auto-detects CRS (3DEP is NAD83/4269, correctly reprojects to 4326)
- [x] Composite shading: multi, igor, combined passes cached independently
- [x] Composite blend as weighted sum of cached sub-layers via numpy
- [x] Second run hits cache at every stage (DEM, reproject, sub-layers, composite)
- **Test:** grayscale GeoTIFF opened for visual verification ⬅️
- **Result:** 4 hillshade files (multi 1.7MB, igor 1.6MB, combined 1.7MB, composite 1.7MB)

### M3: Themes + Style
- [x] Copy 26 ramp `.txt` files from ilhmp
- [x] Port `Theme` dataclass + registry (22 built-in themes)
- [x] `hillgen themes` — list all, `hillgen themes --show midnight`
- [x] `hillgen style --bbox ... --theme midnight --exaggeration 3`
- [x] Ramp mode (color-relief on hillshade values) — midnight, daylight, tactical, etc.
- [x] Elevation mode (color-relief on DEM, modulated by hillshade) — alpine-glacier, magma, etc.
- [x] Custom theme JSON loading via file path
- [x] Sub-layer cache reuse across themes with different composite weights
- **Test:** midnight + alpine-glacier opened for visual verification ⬅️

### M4: Tile + Package
- [x] `hillgen tile` — gdal2tiles --xyz (not --tms, fixing the DuPage bug from day 1)
- [x] `hillgen package` — tile dir → MBTiles + PMTiles
- [x] Metadata injection (bounds, center, name, zoom, scheme=xyz)
- [x] `hillgen view` — local Leaflet tile viewer with dark basemap
- [x] `_ensure_styled` helper — shared pipeline logic for tile/package/run
- **Test:** viewer launched at localhost:9876, tiles rendering in browser ⬅️
- **Result:** 81 tiles z10-14, MBTiles 5.0MB, PMTiles 4.9MB

**— `hillgen run --bbox ... --theme midnight` works end to end —**

### M5: Local Cache
- [x] Cache lookups at each stage, skip on hit (done in M1-M4)
- [x] `hillgen cache status` — show what's cached and sizes
- [x] `hillgen cache clean` — remove stale intermediates (dry-run, per-stage)
- [x] `hillgen cache pull` — pre-fetch DEM for an area
- **Test:** cache clean --dry-run --stage styled shows 3 files, 10.9 MB

### M6: Place Geocoding
- [x] `--place "Mt. St. Helens"` via Nominatim
- [x] Auto-buffer for point features (~0.1° ≈ 11km)
- [x] Area features use Nominatim's boundingbox directly
- [x] `--buffer` flag for custom buffer size
- [ ] `--county cook --state IL` via Census TIGER (deferred)
- **Test:** `--place "Mt. St. Helens"` → bbox covering crater + surrounding terrain

### M7: Auto-Exaggeration
- [x] Port auto-exag from ilhmp, improved terrain-type thresholds
- [x] Wired into shade + _ensure_styled when exaggeration not specified
- **Test:** St Helens (range 2100m) → 1.5x, simulated flat (range 30m) → 9x
- **Thresholds:** flat(<50m)→9x, rolling(<200m)→4x, hilly(<1000m)→2x, mountain→1.5x

### M8: S3 Cache
- [x] `cache_s3.py` module: try_pull (anonymous HTTPS), push (boto3), exists
- [x] Created `scriptedrelief-data` bucket (us-east-2, public-read on `cache/` prefix)
- [x] Created `scriptedrelief` bucket (us-east-2, public-read, CORS for PMTiles)
- [x] Verified anonymous public read of cached intermediate
- [x] Verified `try_pull` downloads 5.4MB reprojected DEM from S3
- [x] Wire S3 read-through into pipeline stages automatically
- [x] `--contribute` flag triggers uploads after each stage

### M9: Publish
- [x] `hillgen publish` — validates PMTiles v3 header + uploads via boto3
- [x] `--dry-run` flag for validation without upload
- [x] First publish: midnight Mt. St. Helens PMTiles live on S3
- [ ] Update `catalog.json` after upload
- [ ] CloudFront distribution for scriptedrelief.com
- **Verified:** `https://scriptedrelief.s3.us-east-2.amazonaws.com/tiles/...` returns 200

---

## Website (scriptedrelief.com)

**Audience:**
1. **Developer** — found the repo, wants to see output quality
2. **Theme shopper** — picking a theme before generating via CLI
3. **Area browser** — looking for a specific region already in the library
4. **Designer/creative** — wants a beautiful hillshade for a UI, poster, data viz, or map project. Non-technical. May be a human or an AI agent fetching assets.

**Stack:** Pure static HTML/CSS/JS. Leaflet + pmtiles.js. Deployed to S3, served via CloudFront.

### Pages

#### W1: Landing / Hero
- [ ] Full-bleed hillshade render (dramatic area, e.g. St Helens)
- [ ] Tagline: "Beautiful terrain maps. Free to use."
- [ ] Two CTAs: "Browse Library" / "Generate Your Own"
- [ ] OG/meta tags for link previews

#### W2: Library Browser
- [ ] Grid of areas, each with hero render in default theme
- [ ] Click area → detail page with all themes as gallery
- [ ] Each theme card: preview image, name, one-line description, download button
- [ ] Downloads: PMTiles (devs), MBTiles (offline), PNG preview (designers)
- [ ] Resolution picker for PNG exports (1080p, 4K)
- [ ] Copyable embed/tile URLs
- [ ] Populated from `catalog.json`

#### W3: Theme Showcase
- [ ] Same area rendered in every theme, side by side
- [ ] Big visual comparison grid
- [ ] "Use this theme" → download link or CLI command

#### W4: Map Viewer
- [ ] Interactive Leaflet, PMTiles overlay, dark basemap
- [ ] Pan/zoom to explore
- [ ] Layer switcher from catalog.json
- [ ] Zoom level indicator

#### W5: Generate Your Own
- [ ] Quickstart, install instructions
- [ ] One-liner copy-paste
- [ ] "Build your command" form (pick area + theme → CLI string)
- [ ] Links to GitHub repo

#### Cross-cutting
- [ ] License clarity: "free to use, public domain source data, credit appreciated"
- [ ] Clean `catalog.json` with direct download URLs for AI agent access
- [ ] Predictable URL patterns: `scriptedrelief.com/tiles/{area}-{theme}.pmtiles`
- [ ] No jargon for designers — "Dark Blue" not "MBTiles z10-16 EPSG:4326"

---

## Status

| Milestone | Status | Started | Completed | Notes |
|-----------|--------|---------|-----------|-------|
| M0 | ✅ | 2026-05-18 | 2026-05-18 | Skeleton + all subcommand stubs |
| M1 | ✅ | 2026-05-18 | 2026-05-18 | USGS 3DEP 10m, tile caching, bbox clipping |
| M2 | ✅ | 2026-05-18 | 2026-05-18 | Reproject (NAD83→4326) + composite shading |
| M3 | ✅ | 2026-05-18 | 2026-05-18 | 22 themes, ramp + elevation color modes |
| M4 | ✅ | 2026-05-18 | 2026-05-18 | gdal2tiles XYZ, mbtiles+pmtiles, Leaflet viewer |
| M5 | ✅ | 2026-05-18 | 2026-05-18 | cache clean (dry-run, per-stage), cache pull |
| M6 | ✅ | 2026-05-18 | 2026-05-18 | Nominatim geocoding, --place works everywhere |
| M7 | ✅ | 2026-05-18 | 2026-05-18 | Auto-exag: flat→9x, rolling→4x, mountain→1.5x |
| M8 | ✅ | 2026-05-18 | 2026-05-18 | S3 buckets created, public read verified |
| M9 | ✅ | 2026-05-18 | 2026-05-18 | Publish validated + uploaded PMTiles to S3 |

## Decisions Log

Record design decisions as they come up during development.

| Date | Decision | Context |
|------|----------|---------|
| 2026-05-18 | CLI name: `hillgen` | Short, memorable |
| 2026-05-18 | Leaflet over MapLibre GL JS | 3D terrain rendering was unreliable |
| 2026-05-18 | Public S3 cache by default | Source DEMs are all public domain, lower barrier |
| 2026-05-18 | Alpha: direct S3 writes | Presigned URL infra deferred to production phase |
| 2026-05-18 | Cache composite sub-layers | Each gdaldem pass cached independently, blend is cheap |
| 2026-05-18 | No cloud generation for v1 | Local-only, cloud pipeline is a future add-on |
| 2026-05-18 | Pipeline subcommands | fetch/reproject/shade/style/tile/package each standalone |
| 2026-05-18 | S3 buckets | scriptedrelief-data (cache, public-read on cache/), scriptedrelief (web, public-read all) |
| 2026-05-18 | botocore[crt] needed | venv boto3 with aws login requires pip install "botocore[crt]" |
