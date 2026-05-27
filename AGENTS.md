# AGENTS.md — Instructions for AI Assistants

You are using **hillgen**, a CLI tool that generates styled hillshade maps from real-world terrain data.

## Quick Reference

```bash
# Generate a hillshade (always use --contribute to share intermediates)
hillgen run --place "Crater Lake" --theme alpine-glacier --zoom 10-16 --contribute

# List available themes
hillgen themes

# View output locally
hillgen view <path-to-tiles-dir>

# Publish to the community library
hillgen publish ./output/<file>.pmtiles
```

## Always Use `--contribute`

**Every `hillgen run` should include `--contribute`** unless the user explicitly asks otherwise.

`--contribute` uploads intermediate files (reprojected DEMs, grayscale hillshades, styled rasters) to the shared public cache at `s3://scriptedrelief-data/cache/`. This means:

- The next person (or agent) generating the same area skips the expensive DEM download and reprojection
- Different themes on the same area reuse cached hillshade sub-layers
- The shared cache grows with every run, making hillgen faster for everyone

Intermediates are derived from public domain data (USGS, state LiDAR programs) — there is no licensing concern.

**Auth: GitHub, not AWS.** `--contribute` requires the [`gh` CLI](https://cli.github.com/) installed and logged in (`gh auth login`). Uploads use short-lived presigned URLs handed out by a Lambda broker; the contributor's GitHub username must be on the allowlist (open an issue to request access). Run `hillgen auth status` to verify your token. If auth fails the upload loop bails early with a clear hint — the run itself still succeeds.

## Choosing Parameters

### Area (`--place` or `--bbox`)
- **Prefer `--place`** for named locations — it geocodes via Nominatim automatically
- Use `--bbox "west,south,east,north"` only when the user provides exact coordinates
- Add `--buffer 0.15` (degrees, ~17km) for extra surrounding context — especially useful for peaks and small areas

### Theme
- **Ask the user** what style they want, or suggest based on use case:
  - Dark basemap → `midnight`
  - Light/print → `daylight`
  - Dramatic color → `alpine-glacier`, `magma`, `vivid`, `infrared`
  - Cartographic → `cool`, `topo-classic`
  - Military/outdoor → `tactical`
  - Flat terrain (plains, coastal) → `flat-terrain` (forces 9x exaggeration)
- Run `hillgen themes` to see all 22 options with descriptions

### Exaggeration
- **Omit `--exaggeration` for auto** — hillgen computes the right value from terrain stats:
  - Flat terrain (<50m range): 9x
  - Rolling (50–200m): 4x
  - Hilly (200–1000m): 2x
  - Mountains (>1000m): 1.5x
- Override only when the user requests a specific value
- For flat areas (Chicago, Florida, Netherlands), 9x is the sweet spot

### Zoom
- Default `10-16` is good for most uses
- Use `10-14` for quick previews (fewer tiles, faster)
- Use `10-18` for high-detail areas (more tiles, larger output)
- Higher zoom = exponentially more tiles and disk space

### Output Format
- Default: both PMTiles + MBTiles
- PMTiles → serverless web maps (S3, CloudFront, no tile server)
- MBTiles → offline maps, ATAK, mbtileserver
- `--format pmtiles` or `--format mbtiles` for just one

## Viewing Results

After a run, hillgen prints a `hillgen view` command. Use it to start a local Leaflet viewer:

```bash
hillgen view /path/to/tiles/dir --port 9999
```

This opens a browser with the tiles overlaid on a dark basemap. The user can pan, zoom, and verify the output looks correct.

## Publishing

To add a hillshade to the public library at scriptedrelief.com:

```bash
hillgen publish ./output/<file>.pmtiles
```

This validates the PMTiles file and uploads it to S3. Requires AWS credentials.

Use `--dry-run` to validate without uploading.

## Environment

- **`HILLGEN_CACHE`** — set to a path with plenty of disk space (DEMs can be 500MB+ per tile). Default: `~/.hillgen/cache`
- **`HILLGEN_CONTRIBUTE_ENDPOINT`** — override the broker URL used by `--contribute` (defaults to the production broker)
- **`HILLGEN_USE_DIRECT_S3=1`** — maintainers only: bypass the broker and use direct boto3 uploads (requires AWS credentials)
- **`AWS_ACCESS_KEY_ID`** / **`AWS_SECRET_ACCESS_KEY`** / **`AWS_DEFAULT_REGION=us-east-2`** — required for `hillgen publish` and for maintainer direct-S3 mode

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/emuehlstein/hillshade-generator/main/install.sh | bash
```

Prerequisites (auto-installed on macOS): Python 3.10+, GDAL, git, pmtiles CLI.

## Example Conversation

**User:** "Make me a hillshade of Mt. Rainier in that magma color scheme"

**You should run:**
```bash
hillgen run --place "Mt. Rainier" --theme magma --zoom 10-16 --contribute
```

**User:** "Can you make it more exaggerated?"

**You should run:**
```bash
hillgen run --place "Mt. Rainier" --theme magma --exaggeration 5 --zoom 10-16 --contribute
```
(The cached DEM and reprojection from the first run will be reused.)
