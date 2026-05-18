# Contributing to Hillgen

There are several ways to contribute:

- **Share intermediates** — run with `--contribute` to upload cached DEMs and hillshades to the public cache, accelerating future runs for everyone. No review needed; automated validation handles it.
- **Publish a hillshade** — run `hillgen publish` to submit a finished PMTiles to the scriptedrelief.com library. Goes through a PR-based review.
- **Submit a theme** — add a new color ramp and theme preset (see below).
- **Add a DEM source** — register a new elevation data provider.

See [docs/submission.md](docs/submission.md) for full details on the intermediate and PMTiles submission pipelines.

---

## Submitting a Theme

The easiest way to contribute is a new theme. A theme submission needs:

1. **A color ramp file** (`.txt`, GDAL color-relief format) in `hillgen/themes/ramps/`
2. **A theme JSON** in `hillgen/themes/builtin/`
3. **A sample render** — at least one screenshot or PMTiles preview showing the theme on real terrain
4. **A description** — what terrain/use case is it designed for?

### Theme Submission Checklist

- [ ] Ramp file is valid GDAL color-relief format (percentage-based or absolute)
- [ ] Theme JSON includes all required fields (name, description, ramp, shading, exaggeration, tags)
- [ ] `name` is lowercase-kebab-case, unique
- [ ] `tags` include at least one of: `dark`, `light`, `elevation`, `cartographic`, `vivid`, `tactical`
- [ ] Sample render attached (PNG or link to PMTiles viewer)
- [ ] Description explains what terrain types it works best for
- [ ] Tests pass: `pytest tests/test_themes.py`

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

1. Create a new module in `hillgen/sources/`
2. Implement the `DEMSource` interface (see `sources/base.py`)
3. Register it in `sources/__init__.py`
4. Add tests in `tests/test_sources.py`
5. Document it in `docs/dem-sources.md`

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
