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
- [ ] Copy 25+ ramp `.txt` files from ilhmp
- [ ] Port `Theme` dataclass + registry
- [ ] `hillgen themes` — list all, `hillgen themes --show midnight`
- [ ] `hillgen style --bbox ... --theme midnight --exaggeration 9`
- **Test:** styled RGBA raster — visual compare against ilhmp output

### M4: Tile + Package
- [ ] `hillgen tile` — gdal2tiles, XYZ coordinates (not TMS)
- [ ] `hillgen package` — tile dir → MBTiles + PMTiles
- [ ] Metadata injection (bounds, center, attribution, scheme)
- [ ] `hillgen view` — local Leaflet tile viewer
- **Test:** `hillgen view` serves tiles, visually verify in browser
- **Regression:** verify TMS/XYZ correctness (the DuPage bug)

**— `hillgen run --bbox ... --theme midnight` works end to end —**

### M5: Local Cache
- [ ] Cache lookups at each stage, skip on hit
- [ ] `hillgen cache status` — show what's cached and sizes
- [ ] `hillgen cache clean` — remove stale intermediates
- **Test:** second run with different theme skips fetch/reproject/shade

### M6: Place Geocoding
- [ ] `--place "Mt. St. Helens"` via Nominatim
- [ ] `--county cook --state IL` via Census TIGER (stretch)
- [ ] `--buffer 5km` for point features
- **Test:** `--place "Crater Lake"` resolves to correct bbox

### M7: Auto-Exaggeration
- [ ] Port `auto_exag.py` from ilhmp
- [ ] Wire into shade stage when theme has `exaggeration: "auto"`
- **Test:** flat terrain (Summerdale) → 9x, mountains (Rainier) → ~2x

### M8: S3 Cache
- [ ] Read-through from `s3://scriptedrelief-data/cache/`
- [ ] Public-read anonymous HTTPS fetches (no creds for reads)
- [ ] Write with `--contribute` (needs AWS creds)
- [ ] Duplicate key check before upload
- **Test:** upload intermediate, clear local cache, re-run pulls from S3

### M9: Publish
- [ ] `hillgen publish` — validate PMTiles + upload to `s3://scriptedrelief/tiles/`
- [ ] Update `catalog.json`
- [ ] CloudFront invalidation
- **Test:** published tile viewable on scriptedrelief.com

## Status

| Milestone | Status | Started | Completed | Notes |
|-----------|--------|---------|-----------|-------|
| M0 | ✅ | 2026-05-18 | 2026-05-18 | Skeleton + all subcommand stubs |
| M1 | ✅ | 2026-05-18 | 2026-05-18 | USGS 3DEP 10m, tile caching, bbox clipping |
| M2 | ✅ | 2026-05-18 | 2026-05-18 | Reproject (NAD83→4326) + composite shading |
| M3 | | | | |
| M4 | | | | |
| M5 | | | | |
| M6 | | | | |
| M7 | | | | |
| M8 | | | | |
| M9 | | | | |

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
