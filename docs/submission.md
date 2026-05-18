# Submission & Publishing

How community-generated intermediates and finished hillshades get into the public library.

## Overview

There are two submission paths with different trust levels:

| What | Review | Mechanism |
|------|--------|----------|
| **Intermediates** (DEMs, reprojected rasters, grayscale hillshades) | Client-side validation | `--contribute` flag → direct S3 upload |
| **PMTiles** (finished styled tiles for scriptedrelief.com) | Client-side validation | `hillgen publish` → direct S3 upload + catalog update |

---

## Intermediate Contributions

Intermediates are safe to share freely because they're:
- **Deterministic** — same area + source + parameters = same file
- **Format-constrained** — valid GeoTIFF with expected CRS, band count, data type, and bounds
- **Not public-facing** — nobody browses the cache, it just accelerates pipeline runs

### User flow

```bash
# Generate locally AND contribute intermediates to the public cache
hillgen run --place "Denali" --theme midnight --contribute
```

The `--contribute` flag tells the CLI to upload intermediates to the shared cache after each pipeline stage. Generation works identically with or without the flag — it only controls whether results are shared back.

Contributors need AWS credentials with write access to `scriptedrelief-data`. The CLI uses boto3 with standard credential resolution (`AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`, `~/.aws/credentials`, or instance profile).

### Client-side validation

Before uploading, the CLI validates each intermediate:

**All GeoTIFFs:**
- [ ] GDAL opens without error
- [ ] Bounds are valid WGS84 (within ±180/±90), non-zero area
- [ ] Bounds match the area encoded in the cache key (within tolerance)
- [ ] File size within expected range for stage type + area size

**Reprojected DEMs** (`reprojected/{area}_{source}_4326.tif`):
- [ ] CRS is EPSG:4326
- [ ] Band count = 1
- [ ] Data type = Float32 or Float64
- [ ] No-data value is set

**Grayscale hillshade sub-layers** (`hillshade/{area}_{source}_gray_{mode}_{exag}x.tif`):
- [ ] CRS is EPSG:4326
- [ ] Band count = 1
- [ ] Data type = Byte (0-255)
- [ ] Pixel values span a reasonable range (not all 0, not all 255)
- [ ] Mode suffix is one of: `multi`, `igor`, `combined`, or `composite_{weights}`

**Styled rasters** (`styled/{area}_{theme}_{exag}x.tif`):
- [ ] CRS is EPSG:4326
- [ ] Band count = 4 (RGBA)
- [ ] Data type = Byte
- [ ] Alpha channel is not all-zero (not fully transparent)

Duplicate keys are skipped — the CLI checks if the cache key already exists before uploading.

---

## PMTiles Publishing

### Why manual review?

Published tiles appear on scriptedrelief.com under the project's name. Quality matters:
- Broken tiles, wrong bounds, or bad metadata degrade the library
- Duplicate or near-duplicate coverage wastes storage
- Attribution and naming should be consistent

### User flow

```bash
# Generate a hillshade
hillgen run --place "Crater Lake" --theme alpine-glacier

# Publish to the community library
hillgen publish ./output/crater-lake-alpine-glacier-3x.pmtiles
```

### What `publish` does

1. **Validates locally:**
   - Valid PMTiles v3 header
   - Tile type = raster PNG
   - Bounds are valid WGS84, non-zero area
   - Zoom range within 0-22, min < max
   - At least N tiles present (not empty)
   - Sample tiles decode as valid PNG (256×256 or 512×512)
   - Required metadata present: name, theme, exaggeration, dem_source
   - File size < 10GB

2. **Uploads** PMTiles to `s3://scriptedrelief/tiles/`

3. **Updates** `catalog.json` with the new layer entry

4. **Invalidates** CloudFront so the tile appears immediately

Contributors need AWS credentials with write access to the `scriptedrelief` bucket.

### Validation checklist (full)

**Structure:**
- [ ] Valid PMTiles v3 header (magic bytes, version, root offset)
- [ ] Tile type = raster (type 1)
- [ ] Internal compression recognized (gzip or none)

**Geography:**
- [ ] Bounds present and valid WGS84 (west < east, south < north, within ±180/±90)
- [ ] Bounds area > 0.001° (not a single point)
- [ ] Center point within bounds
- [ ] Zoom range: min_zoom < max_zoom, both within 0-22
- [ ] min_zoom ≤ 10, max_zoom ≥ 12 (minimum useful range)

**Tiles:**
- [ ] Total tile count > 100 (not trivially empty)
- [ ] 10 random sample tiles decode as valid PNG
- [ ] Sample tile dimensions are 256×256 or 512×512 (consistent)
- [ ] Sample tiles are not blank (not all one color)

**Metadata:**
- [ ] `name` present and non-empty
- [ ] `theme` matches a known theme name (or "custom")
- [ ] `exaggeration` present and numeric
- [ ] `dem_source` present (e.g. "USGS 3DEP 1/3 arc-second")
- [ ] `attribution` present
- [ ] `generated_at` present and valid ISO-8601
- [ ] `generator` = "hillgen" with version

**Limits:**
- [ ] File size < 10 GB
- [ ] No existing entry in catalog.json with same area + theme + exaggeration (or submission explicitly marked as update)

---

## S3 Key Layout

```
s3://scriptedrelief-data/
└── cache/                     # intermediates (public-read)
    ├── dem/
    ├── reprojected/
    ├── hillshade/
    └── styled/

s3://scriptedrelief/
├── tiles/                     # PMTiles (public-read, served via CloudFront)
│   └── {area}-{theme}-{exag}x.pmtiles
├── catalog.json               # layer index
└── index.html                 # web viewer
```
