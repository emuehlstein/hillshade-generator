# Submission & Publishing

How community-generated intermediates and finished hillshades get into the public library.

## Overview

There are two submission paths with different trust levels:

| What | Risk | Review | Mechanism |
|------|------|--------|-----------|
| **Intermediates** (DEMs, reprojected rasters, grayscale hillshades) | Low — deterministic, verifiable | Automated | `--contribute` flag → upload → validation |
| **PMTiles** (finished styled tiles for scriptedrelief.com) | Higher — public-facing, curated | Manual (PR) | `hillgen publish` → GitHub PR → maintainer review |

## Phases

### Alpha (current)

Contributors receive AWS credentials with direct write access to both buckets. Validation runs client-side before upload. No server-side infrastructure needed.

```bash
# Contribute intermediates (needs AWS creds)
hillgen run --place "Denali" --theme midnight --contribute

# Publish finished tiles (needs AWS creds)
hillgen publish ./output/denali-midnight-3x.pmtiles
```

### Production

Anonymous contributors use presigned URLs — no AWS account needed. Server-side validation promotes or rejects uploads automatically. PMTiles go through a PR-based review before appearing on scriptedrelief.com.

The rest of this document describes the production architecture.

---

## Intermediate Contributions

### Why auto-accept?

Intermediates are safe to accept automatically because they're:
- **Deterministic** — same area + source + parameters = same file. We can verify a submission against its inputs.
- **Format-constrained** — must be valid GeoTIFF with expected CRS, band count, data type, and bounds.
- **Not public-facing** — nobody browses the cache. It just accelerates pipeline runs for future users.

### User flow

```bash
# Generate locally AND contribute intermediates to the public cache
hillgen run --place "Denali" --theme midnight --contribute
```

The `--contribute` flag tells the CLI to upload intermediates to the shared cache after each pipeline stage. Generation works identically with or without the flag — it only controls whether results are shared back.

### Upload mechanism: presigned URLs

Users never need AWS credentials. The CLI requests a short-lived presigned upload URL from a small API, then PUTs the file directly to S3.

```
User's machine                    API (Lambda)                    S3
─────────────                    ────────────                    ──

POST /api/upload-request
  { cache_key, file_size,
    file_hash, stage_type }
          ────────────────▶
                                 Validate request:
                                 - cache key format valid?
                                 - size within limits for stage type?
                                 - key not already populated?

                                 Generate presigned PUT URL
                                 scoped to staging/{cache_key}
                                 (expires in 60 minutes)
          ◀────────────────
  { upload_url, expires_at }

PUT upload_url
  (raw file bytes)               ────────────────────────────▶   staging/{key}

                                 S3 event trigger → validation Lambda
                                 - runs format checks
                                 - on pass: copy to cache/{key}, delete staging/{key}
                                 - on fail: delete staging/{key}, log reason
```

### Validation checks (automated)

Every uploaded intermediate passes through a validation Lambda before promotion from `staging/` to `cache/`:

**All GeoTIFFs:**
- [ ] GDAL opens without error
- [ ] File is not truncated (size matches Content-Length)
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

### Integrity spot-check (optional, probabilistic)

For extra confidence, the validation Lambda can probabilistically verify submissions:
- Download the source DEM for the same area
- Reproject (or compute hillshade) for a small random tile within the bounds
- Compare the result against the corresponding region of the submitted file
- Reject if pixel values diverge beyond threshold

This runs on ~5% of submissions to catch systematic issues without reprocessing everything.

### Rate limiting & abuse prevention

- Presigned URLs are scoped to a single S3 key — can't write elsewhere
- Upload requests are rate-limited per IP (10/hour default)
- Maximum file size per stage type (DEM: 20GB, hillshade: 5GB, styled: 10GB)
- Total daily upload volume cap (100GB/day initially)
- Duplicate keys are rejected — can't overwrite existing cache entries
- All uploads land in `staging/` first — never directly in `cache/`

### Infrastructure

- **API Gateway** — single endpoint: `POST /api/upload-request`
- **Lambda (upload-request)** — validates request, generates presigned URL (~50 lines)
- **Lambda (validate-intermediate)** — triggered by S3 put to `staging/`, runs GDAL checks, promotes or rejects (~200 lines)
- **S3 event notification** — `staging/*` PUT → validate-intermediate Lambda

Total: 2 Lambdas, 1 API Gateway, 1 S3 event rule. Runs within free tier for moderate volume.

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

1. **Local validation** (runs before any upload):
   - Valid PMTiles v3 header
   - Tile type = raster PNG
   - Bounds are valid WGS84, non-zero area
   - Zoom range within 0-22, min < max
   - At least N tiles present (not empty)
   - Sample tiles decode as valid PNG (256×256 or 512×512)
   - Required metadata present: name, theme, exaggeration, dem_source
   - File size < 10GB

2. **Generate preview** — renders a preview image from center tiles at mid-zoom

3. **Upload to staging** — PMTiles uploaded via presigned URL to `scriptedrelief/staging/`

4. **Open GitHub PR** — CLI creates a PR on the hillshade-generator repo:
   - Adds entry to `catalog/pending/{submission-id}.json`
   - PR body includes: area name, theme, bounds, tile count, file size, DEM source
   - Preview image attached
   - Link to staged PMTiles for test viewing

### PR template

```markdown
## New Hillshade: Crater Lake — alpine-glacier 3x

**Area:** Crater Lake, OR
**Theme:** alpine-glacier
**Exaggeration:** 3x
**DEM Source:** USGS 3DEP 1/3 arc-second
**Zoom:** 8-16
**Tiles:** 48,000
**Size:** 245 MB

### Preview
![preview](./preview-crater-lake-alpine-glacier-3x.png)

### Validation
- [x] PMTiles v3 header valid
- [x] Bounds: [-122.25, 42.85, -121.95, 43.0]
- [x] Sample tiles decode OK
- [x] Metadata complete

### Test
[View staged tiles](https://scriptedrelief.com/preview?staging=crater-lake-alpine-glacier-3x)
```

### Maintainer review

The maintainer:
1. Checks the preview renders — do they look good?
2. Verifies the area doesn't duplicate existing coverage (or is a clear improvement)
3. Eyeballs metadata for consistency
4. Merges the PR

### Post-merge automation

A GitHub Actions workflow triggers on merge of `catalog/pending/*.json`:
1. Moves PMTiles from `scriptedrelief/staging/` to `scriptedrelief/tiles/`
2. Adds the entry to the main `catalog.json`
3. Invalidates CloudFront
4. Deletes `catalog/pending/{submission-id}.json`
5. Tile is live on scriptedrelief.com

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

## Future: Upload + Review Dashboard

When PR volume grows beyond what's comfortable to review on GitHub, we can add a web-based review dashboard:

```
hillgen publish  →  staging upload  →  submission registered via API
                                                    │
                                              review dashboard
                                              (static page reading from
                                               catalog/pending/*.json)
                                                    │
                                             maintainer approves
                                                    │
                                             API promotes to public
```

The building blocks (presigned uploads, validation, catalog management) are the same — we'd just swap the GitHub PR step for a dashboard UI. The PR-based flow is the right starting point because it's transparent, auditable, and uses existing tools.

---

## S3 Key Layout

```
s3://scriptedrelief-data/
├── cache/                     # production intermediates (public-read)
│   ├── dem/
│   ├── reprojected/
│   ├── hillshade/
│   └── styled/
└── staging/                   # intermediate uploads pending validation
    ├── dem/
    ├── reprojected/
    ├── hillshade/
    └── styled/

s3://scriptedrelief/
├── tiles/                     # production PMTiles (public-read, served via CloudFront)
│   └── {area}-{theme}-{exag}x.pmtiles
├── staging/                   # PMTiles pending review
│   └── {area}-{theme}-{exag}x.pmtiles
├── catalog.json               # production catalog
└── index.html                 # web viewer
```

Staging prefixes in both buckets are **not** public-read — only the production prefixes are. Staged PMTiles are accessible via a preview URL that routes through the API (presigned read URL for reviewers).

---

## Cost Impact

| Component | Free Tier | Overflow |
|-----------|-----------|----------|
| API Gateway | 1M requests/mo | $1/M requests |
| Lambda (upload-request) | 1M invocations/mo | $0.20/M |
| Lambda (validate) | 1M invocations/mo | ~$0.50/M (GDAL layer runs ~2s) |
| S3 staging churn | Tiny (files promoted or deleted within minutes) | Negligible |
| **Total for moderate community** | **$0/mo** | **< $5/mo at scale** |
