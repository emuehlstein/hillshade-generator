"""Tests for Wisconsin DNR LiDAR DEM source."""

import pytest
from unittest.mock import patch, MagicMock, call
from hillgen.sources.wi_dnr_lidar import (
    WiDNRLiDAR, _build_export_url, _chunk_bbox, _wgs84_to_epsg3071
)
from hillgen.sources.base import BBox
from hillgen.sources import resolve_source


# ── Coverage ─────────────────────────────────────────────────────────────────

def test_covers_milwaukee():
    src = WiDNRLiDAR()
    assert src.covers(BBox(-88.5, 43.0, -87.5, 44.0))


def test_covers_kettle_moraine():
    src = WiDNRLiDAR()
    assert src.covers(BBox(-88.6, 42.8, -88.0, 43.4))


def test_covers_entire_wisconsin():
    src = WiDNRLiDAR()
    # Just inside bounds
    assert src.covers(BBox(-92.8, 42.5, -86.9, 47.0))


def test_does_not_cover_chicago():
    src = WiDNRLiDAR()
    assert not src.covers(BBox(-88.5, 41.5, -87.5, 42.0))


def test_does_not_cover_minnesota():
    src = WiDNRLiDAR()
    assert not src.covers(BBox(-93.5, 44.0, -92.0, 45.0))


def test_does_not_cover_michigan():
    src = WiDNRLiDAR()
    assert not src.covers(BBox(-86.5, 43.0, -85.0, 44.0))


# ── Coordinate reprojection ───────────────────────────────────────────────────

def test_epsg3071_approx_milwaukee():
    """Milwaukee (~-87.9, 43.0) should be near WTM coords ~590000, 380000."""
    xmin, ymin, xmax, ymax = _wgs84_to_epsg3071(-88.0, 43.0, -87.8, 43.1)
    # WTM x should be in range ~570000–620000 for Milwaukee longitude
    assert 500000 < xmin < 700000
    assert 500000 < xmax < 700000
    # WTM y should be in range for ~43° latitude
    assert 200000 < ymin < 500000


# ── URL construction ──────────────────────────────────────────────────────────

def test_export_url_contains_required_params():
    bbox = BBox(-88.5, 43.0, -87.5, 44.0)
    url = _build_export_url(bbox)
    assert "exportImage" in url
    assert "pixelType=F32" in url
    assert "format=tiff" in url
    assert "bboxSR=3071" in url
    assert "imageSR=3071" in url  # service returns native CRS; gdalwarp reprojects downstream
    assert "noData=-9999" in url
    assert "f=image" in url


def test_export_url_bbox_is_projected():
    """The bbox param in the URL should be in EPSG:3071, not WGS84."""
    bbox = BBox(-88.5, 43.0, -87.5, 44.0)
    url = _build_export_url(bbox)
    # Extract bbox param value
    from urllib.parse import urlparse, parse_qs
    params = parse_qs(urlparse(url).query)
    bbox_vals = [float(v) for v in params["bbox"][0].split(",")]
    # EPSG:3071 x coords for WI should be large numbers (500k–700k range)
    assert bbox_vals[0] > 10000, "bbox should be projected, not WGS84"


# ── Chunking ─────────────────────────────────────────────────────────────────

def test_small_bbox_no_chunking():
    bbox = BBox(-88.5, 43.0, -88.1, 43.4)  # 0.4° x 0.4°, under threshold
    chunks = _chunk_bbox(bbox)
    assert len(chunks) == 1
    assert chunks[0] == bbox


def test_large_bbox_chunked():
    bbox = BBox(-92.9, 42.4, -86.8, 47.1)  # all of WI
    chunks = _chunk_bbox(bbox)
    assert len(chunks) > 1
    # All chunks should be non-degenerate
    for c in chunks:
        assert c.east > c.west
        assert c.north > c.south


def test_chunks_cover_full_bbox():
    """Union of all chunks should cover the full bbox."""
    bbox = BBox(-90.0, 43.0, -88.0, 45.0)
    chunks = _chunk_bbox(bbox)
    assert min(c.west for c in chunks) <= bbox.west + 0.001
    assert max(c.east for c in chunks) >= bbox.east - 0.001
    assert min(c.south for c in chunks) <= bbox.south + 0.001
    assert max(c.north for c in chunks) >= bbox.north - 0.001


# ── Auto-selection ────────────────────────────────────────────────────────────

def test_auto_selects_wi_dnr_for_wisconsin():
    """resolve_source should pick wi-dnr-lidar for a WI bbox."""
    bbox = BBox(-88.5, 43.0, -87.5, 44.0)
    source = resolve_source(bbox)
    assert source.name == "wi-dnr-lidar"


def test_auto_selects_usgs_outside_wisconsin():
    """resolve_source should fall back to USGS 3DEP outside WI."""
    bbox = BBox(-88.0, 41.7, -87.5, 42.0)  # Chicago area
    source = resolve_source(bbox)
    assert source.name == "usgs-3dep-10m"


# ── Source metadata ───────────────────────────────────────────────────────────

def test_source_attributes():
    src = WiDNRLiDAR()
    assert src.name == "wi-dnr-lidar"
    assert src.resolution_m == 1.0
    assert src.priority == 90
    assert "wisconsin" in src.description.lower()
