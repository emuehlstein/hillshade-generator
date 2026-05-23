"""Tests for Indiana IGIC LiDAR DEM source."""

import pytest
from unittest.mock import patch, MagicMock
from hillgen.sources.igic_indiana_lidar import (
    IGICIndianaLiDAR,
    _county_to_slug,
    _tile_url,
    _tile_year,
    _bbox_intersects,
    _output_filename,
    _BASE_URL,
)
from hillgen.sources.base import BBox
from hillgen.sources import resolve_source


# ── Coverage ─────────────────────────────────────────────────────────────────

def test_covers_laporte():
    src = IGICIndianaLiDAR()
    assert src.covers(BBox(-86.93, 41.47, -86.48, 41.78))


def test_covers_south_bend():
    src = IGICIndianaLiDAR()
    assert src.covers(BBox(-86.4, 41.6, -86.1, 41.8))


def test_covers_indianapolis():
    src = IGICIndianaLiDAR()
    assert src.covers(BBox(-86.3, 39.6, -85.9, 40.0))


def test_covers_entire_indiana():
    src = IGICIndianaLiDAR()
    assert src.covers(BBox(-88.0, 37.8, -84.8, 41.7))


def test_does_not_cover_chicago():
    src = IGICIndianaLiDAR()
    assert not src.covers(BBox(-87.9, 41.7, -87.6, 42.1))


def test_does_not_cover_ohio():
    src = IGICIndianaLiDAR()
    assert not src.covers(BBox(-83.0, 39.0, -80.5, 41.0))


def test_does_not_cover_wisconsin():
    src = IGICIndianaLiDAR()
    assert not src.covers(BBox(-88.5, 43.0, -87.5, 44.0))


def test_does_not_cover_kentucky():
    src = IGICIndianaLiDAR()
    assert not src.covers(BBox(-86.5, 36.5, -85.0, 37.5))


# ── County slug normalization ─────────────────────────────────────────────────

def test_slug_laporte():
    assert _county_to_slug("La Porte") == "laporte"


def test_slug_dekalb():
    assert _county_to_slug("De Kalb") == "dekalb"


def test_slug_stjoseph():
    assert _county_to_slug("St Joseph") == "stjoseph"


def test_slug_simple():
    assert _county_to_slug("Allen") == "allen"


def test_slug_tippecanoe():
    assert _county_to_slug("Tippecanoe") == "tippecanoe"


def test_slug_vanderburgh():
    assert _county_to_slug("Vanderburgh") == "vanderburgh"


def test_slug_lowercase():
    # slug func normalizes to lower before checking overrides,
    # so LAPORTE (no space) hits the default path and still produces "laporte"
    assert _county_to_slug("LAPORTE") == "laporte"
    assert _county_to_slug("allen") == "allen"
    # But "LA PORTE" (with space) hits the override
    assert _county_to_slug("LA PORTE") == "laporte"


# ── Tile year and URL ─────────────────────────────────────────────────────────

def test_tile_year_2018():
    assert _tile_year("in2018_29902180_12") == 2018


def test_tile_year_2013():
    assert _tile_year("in2013_30752350_12") == 2013


def test_tile_url_new_dataset():
    url = _tile_url("laporte", "in2018_29902180_12")
    assert "QL2_3DEP_LiDAR_IN_2017_2019_l2" in url
    assert "laporte" in url
    assert url.endswith("in2018_29902180_12.img")


def test_tile_url_old_dataset():
    url = _tile_url("laporte", "in2013_30752350_12")
    assert "/state/2013/dem/" in url
    assert url.endswith("in2013_30752350_12.img")


def test_tile_url_2017():
    url = _tile_url("allen", "in2017_12345678_12")
    assert "QL2_3DEP_LiDAR_IN_2017_2019_l2" in url


def test_tile_url_2020():
    url = _tile_url("posey", "in2020_12345678_12")
    assert "QL2_3DEP_LiDAR_IN_2017_2019_l2" in url


# ── Geometry intersection ─────────────────────────────────────────────────────

_LAPORTE_POLYGON = {
    "type": "Polygon",
    "coordinates": [[
        [-86.95, 41.23], [-86.47, 41.23],
        [-86.47, 41.77], [-86.95, 41.77],
        [-86.95, 41.23],
    ]]
}

_LAPORTE_MULTIPOLYGON = {
    "type": "MultiPolygon",
    "coordinates": [[[
        [-86.95, 41.23], [-86.47, 41.23],
        [-86.47, 41.77], [-86.95, 41.77],
        [-86.95, 41.23],
    ]]]
}


def test_intersects_polygon_inside():
    bbox = BBox(-86.80, 41.50, -86.60, 41.70)
    assert _bbox_intersects(_LAPORTE_POLYGON, bbox)


def test_intersects_multipolygon():
    bbox = BBox(-86.80, 41.50, -86.60, 41.70)
    assert _bbox_intersects(_LAPORTE_MULTIPOLYGON, bbox)


def test_no_intersect_outside():
    bbox = BBox(-85.0, 40.0, -84.5, 40.5)
    assert not _bbox_intersects(_LAPORTE_POLYGON, bbox)


def test_intersects_partial_overlap():
    bbox = BBox(-87.1, 41.60, -86.80, 41.90)  # partially overlaps west edge
    assert _bbox_intersects(_LAPORTE_POLYGON, bbox)


def test_empty_geometry():
    assert not _bbox_intersects({"type": "Polygon", "coordinates": []}, BBox(-87, 41, -86, 42))


# ── Output filename ───────────────────────────────────────────────────────────

def test_output_filename_laporte():
    bbox = BBox(-86.65, 41.70, -86.58, 41.75)
    name = _output_filename(bbox)
    assert name.startswith("igic_in_lidar_")
    assert name.endswith(".tif")
    assert "n41" in name
    assert "w86" in name


def test_output_filename_unique_per_bbox():
    b1 = BBox(-86.65, 41.70, -86.58, 41.75)
    b2 = BBox(-87.00, 41.50, -86.90, 41.60)
    assert _output_filename(b1) != _output_filename(b2)


# ── Source metadata ───────────────────────────────────────────────────────────

def test_source_attributes():
    src = IGICIndianaLiDAR()
    assert src.name == "igic-indiana-lidar"
    assert src.resolution_m < 1.0   # 0.76m
    assert src.priority == 92
    assert "indiana" in src.description.lower()
    assert "92" in src.description


# ── Auto-selection ────────────────────────────────────────────────────────────

def test_auto_selects_igic_for_indiana():
    """resolve_source should pick igic-indiana-lidar for an IN bbox."""
    bbox = BBox(-86.65, 41.70, -86.58, 41.75)
    source = resolve_source(bbox)
    assert source.name == "igic-indiana-lidar"


def test_auto_selects_usgs_outside_indiana():
    """resolve_source should fall back to USGS 3DEP outside IN."""
    bbox = BBox(-87.9, 41.7, -87.6, 42.1)  # Chicago area
    source = resolve_source(bbox)
    assert source.name == "usgs-3dep-10m"


def test_igic_priority_over_wi_dnr():
    """For a bbox in IN, IGIC should have higher priority than WI DNR."""
    src_in = IGICIndianaLiDAR()
    from hillgen.sources.wi_dnr_lidar import WiDNRLiDAR
    src_wi = WiDNRLiDAR()
    assert src_in.priority > src_wi.priority


# ── Download (mocked) ─────────────────────────────────────────────────────────

def _make_tile_feature(tile_name: str, west: float, south: float, east: float, north: float) -> dict:
    return {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [west, south], [east, south],
                [east, north], [west, north],
                [west, south],
            ]]
        },
        "properties": {"TILE_NAME": tile_name},
    }


def _make_county_feature(name: str, west: float, south: float, east: float, north: float) -> dict:
    return {
        "type": "Feature",
        "geometry": {
            "type": "Polygon",
            "coordinates": [[
                [west, south], [east, south],
                [east, north], [west, north],
                [west, south],
            ]]
        },
        "properties": {"NAME_L": name, "lidar_year": "2013,2018"},
    }


@patch("hillgen.sources.igic_indiana_lidar._reproject_and_clip")
@patch("hillgen.sources.igic_indiana_lidar._mosaic_tiles")
@patch("hillgen.sources.igic_indiana_lidar._download_tile")
@patch("hillgen.sources.igic_indiana_lidar.requests")
def test_download_calls_correct_endpoints(mock_requests, mock_dl, mock_mosaic, mock_reproject, tmp_path):
    """download() should fetch tile index and download only intersecting tiles."""
    bbox = BBox(-86.65, 41.70, -86.58, 41.75)

    # Two tiles: one intersects, one does not
    tile_in = _make_tile_feature("in2018_30752350_12", -86.67, 41.69, -86.59, 41.76)
    tile_out = _make_tile_feature("in2018_31002355_12", -85.0, 40.0, -84.5, 40.5)

    county_feat = _make_county_feature("La Porte", -86.95, 41.23, -86.47, 41.77)

    counties_resp = MagicMock()
    counties_resp.json.return_value = {"features": [county_feat]}
    tile_resp = MagicMock()
    tile_resp.json.return_value = {"features": [tile_in, tile_out]}

    mock_get = MagicMock(side_effect=[counties_resp, tile_resp])
    mock_requests.get = mock_get
    mock_requests.Session.return_value.__enter__ = MagicMock(return_value=MagicMock(get=MagicMock()))
    mock_requests.Session.return_value.__exit__ = MagicMock(return_value=False)

    # Mock download to return a fake file
    fake_file = tmp_path / "in2018_30752350_12.img"
    fake_file.write_bytes(b"fake")
    mock_dl.return_value = fake_file

    # Mock reproject to create expected output file
    def fake_reproject(src, dst, bbox):
        dst.write_bytes(b"fake-reprojected")
    mock_reproject.side_effect = fake_reproject

    src = IGICIndianaLiDAR()
    result = src.download(bbox, tmp_path)

    # Should have fetched county index and tile index
    assert mock_requests.get.call_count == 2

    # Only the intersecting tile should be downloaded
    assert mock_dl.call_count == 1
    called_url = mock_dl.call_args[0][0]
    assert "in2018_30752350_12" in called_url
    assert "QL2_3DEP_LiDAR_IN_2017_2019_l2" in called_url

    # No mosaic needed for a single tile
    mock_mosaic.assert_not_called()

    # Reproject should be called once
    mock_reproject.assert_called_once()


@patch("hillgen.sources.igic_indiana_lidar._reproject_and_clip")
@patch("hillgen.sources.igic_indiana_lidar._mosaic_tiles")
@patch("hillgen.sources.igic_indiana_lidar._download_tile")
@patch("hillgen.sources.igic_indiana_lidar.requests")
def test_download_mosaics_multiple_tiles(mock_requests, mock_dl, mock_mosaic, mock_reproject, tmp_path):
    """download() should mosaic when more than one tile is needed."""
    bbox = BBox(-86.80, 41.60, -86.50, 41.80)

    tiles = [
        _make_tile_feature("in2018_30002180_12", -86.82, 41.59, -86.71, 41.71),
        _make_tile_feature("in2018_30002185_12", -86.71, 41.59, -86.60, 41.71),
        _make_tile_feature("in2018_30002190_12", -86.60, 41.59, -86.49, 41.71),
    ]
    county_feat = _make_county_feature("La Porte", -86.95, 41.23, -86.47, 41.77)

    counties_resp = MagicMock()
    counties_resp.json.return_value = {"features": [county_feat]}
    tile_resp = MagicMock()
    tile_resp.json.return_value = {"features": tiles}

    mock_requests.get = MagicMock(side_effect=[counties_resp, tile_resp])
    mock_requests.Session.return_value.__enter__ = MagicMock(return_value=MagicMock())
    mock_requests.Session.return_value.__exit__ = MagicMock(return_value=False)

    fake_files = []
    for t in tiles:
        f = tmp_path / f"{t['properties']['TILE_NAME']}.img"
        f.write_bytes(b"fake")
        fake_files.append(f)

    mock_dl.side_effect = fake_files

    # Don't pre-create the mosaic file — let mock_mosaic create it
    mosaic_path = tmp_path / "_igic_mosaic.tif"
    def fake_mosaic(paths, out):
        out.write_bytes(b"mosaic")
        return out
    mock_mosaic.side_effect = fake_mosaic

    def fake_reproject(src, dst, bbox):
        dst.write_bytes(b"done")
    mock_reproject.side_effect = fake_reproject

    src = IGICIndianaLiDAR()
    result = src.download(bbox, tmp_path)

    assert mock_dl.call_count == 3
    mock_mosaic.assert_called_once()
    mock_reproject.assert_called_once()
