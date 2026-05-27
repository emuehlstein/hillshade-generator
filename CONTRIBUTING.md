# Contributing to Hillgen

There are several ways to contribute:

- **Share intermediates** — run with `--contribute` to upload reprojected DEMs, hillshades, and styled rasters to the public cache, accelerating future runs for everyone. Requires a one-time allowlist add (see below).
- **Publish a hillshade** — run `hillgen publish` to submit a finished PMTiles to the scriptedrelief.com library. Goes through a PR-based review.
- **Submit a theme** — add a new color ramp and theme preset (see below).
- **Add a DEM source** — register a new elevation data provider.

See [docs/submission.md](docs/submission.md) for full details on the intermediate and PMTiles submission pipelines.

---

## Sharing Intermediates (`--contribute`)

`hillgen run --contribute` uploads three derived artifact stages —
`reprojected`, `hillshade`, `styled` — into the shared cache so other
contributors don't have to recompute them. Raw DEMs are **not** uploaded
(they're large and rarely re-hit).

You don't need AWS credentials. Authentication is done entirely with
your GitHub account, via a small Lambda broker that hands out
short-lived presigned S3 upload URLs.

### One-time setup

1. **Install the GitHub CLI** and log in:
   ```bash
   brew install gh
   gh auth login
   ```
2. **Request allowlist access** — open an issue at
   <https://github.com/emuehlstein/hillshade-generator/issues> titled
   *"Allowlist request: <your-github-username>"*. Once approved your
   username is added to the broker's allowlist (changes take ≤5 minutes
   to propagate).
3. **Verify**:
   ```bash
   hillgen auth status
   ```
   You should see a green `GitHub token: ✓ valid (user: …)`.

### Day-to-day usage

```bash
hillgen run --bbox … --dem … --theme … --contribute
```

Each derived file is uploaded individually; failures don't abort the
pipeline. If you see repeated `AuthError` / `not_allowlisted` / similar
fatal codes, the loop bails early and prints a hint — re-run with
`--no-contribute` to skip uploads, or run `hillgen auth status` to
diagnose.

### Troubleshooting

| Symptom | Fix |
|---|---|
| `gh auth token` returns nothing | `gh auth login` (or `gh auth refresh`) |
| `not_allowlisted` from broker | Open an allowlist-request issue |
| `invalid_token` | Your token expired — `gh auth refresh` |
| Broker unreachable | Set `HILLGEN_CONTRIBUTE_ENDPOINT` to the current API URL |
| You're a maintainer w/ direct S3 access | `HILLGEN_USE_DIRECT_S3=1 hillgen run --contribute …` bypasses the broker |

The broker stack lives in [infra/broker/](infra/broker/) — see its
README for deploy and ops details.

---

## Submitting a Theme

The easiest way to contribute is a new theme. A theme submission needs:

1. **A color ramp file** (`.txt`, GDAL color-relief format) in `hillgen/themes/ramps/`
2. **A `Theme(...)` registration** in `hillgen/themes/registry.py` (or a standalone JSON file if you don't want to upstream it)
3. **A sample render** — at least one screenshot or PMTiles preview showing the theme on real terrain
4. **A description** — what terrain/use case is it designed for?

### Theme Submission Checklist

- [ ] Ramp file is valid GDAL color-relief format (percentage-based or absolute)
- [ ] Theme registration / JSON includes all required fields (name, description, ramp, shading, exaggeration, tags)
- [ ] `name` is lowercase-kebab-case, unique
- [ ] `tags` include at least one of: `dark`, `light`, `elevation`, `cartographic`, `vivid`, `tactical`
- [ ] Sample render attached (PNG or link to PMTiles viewer)
- [ ] Description explains what terrain types it works best for
- [ ] Existing tests still pass: `pytest`

### Example Theme JSON

```json
{
  "name": "pacific-fog",
  "description": "Cool grey-blues evoking Pacific coast fog. Subtle, desaturated. Works well for coastal terrain and as a base layer.",
  "ramp": "pacific-fog",
  "color_mode": "ramp",
  "shading": "composite",
  "composite_weights": [0.5, 0.3, 0.2],
  "azimuth": 315.0,
  "altitude": 45.0,
  "exaggeration": "auto",
  "terrain_type": "auto",
  "aspect_blend": 0.08,
  "default_zoom": "10-16",
  "tags": ["cool", "cartographic", "coastal"]
}
```

## Adding a DEM Source

To add a new data source (e.g., a national LiDAR program or commercial DEM):

1. Create a new module in `hillgen/sources/` implementing the `DEMSource` interface from `sources/base.py`
2. Register it in `hillgen/sources/__init__.py` (`_SOURCES` list, ordered by priority)
3. Add tests in `tests/` mirroring the pattern in `tests/test_wi_dnr_lidar.py` or `tests/test_igic_indiana_lidar.py`
4. Mention it in the README “DEM Sources” table

## Reporting Bugs

Open an issue with:
- What you ran (full command)
- What happened (error message or screenshot)
- Your environment (`hillgen version` output)
- The area/theme that triggered the issue

## Development Setup

```bash
git clone https://github.com/emuehlstein/hillshade-generator.git
cd hillshade-generator
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```
