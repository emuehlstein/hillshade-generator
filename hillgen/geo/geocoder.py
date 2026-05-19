"""Place name → bounding box via OpenStreetMap Nominatim.

Free, no API key. Rate limit: 1 request/sec (we only call once per run).
"""

import time
import requests

from ..sources.base import BBox

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_USER_AGENT = "hillgen/0.1.0 (https://github.com/emuehlstein/hillshade-generator)"

# Buffer in degrees to add around point features (peaks, addresses)
_DEFAULT_BUFFER_DEG = 0.1  # ~11km at equator


def geocode(place: str, buffer_deg: float = _DEFAULT_BUFFER_DEG) -> BBox:
    """Geocode a place name to a bounding box.

    For area features (cities, parks, counties), uses the returned boundingbox.
    For point features (peaks, addresses), adds a buffer around the point.

    Args:
        place: Place name (e.g. "Mt. St. Helens", "Crater Lake", "Cook County, IL")
        buffer_deg: Buffer in degrees for point features (default ~11km)

    Returns:
        BBox in WGS84

    Raises:
        ValueError: If the place can't be found
    """
    resp = requests.get(
        _NOMINATIM_URL,
        params={
            "q": place,
            "format": "jsonv2",
            "limit": 1,
        },
        headers={"User-Agent": _USER_AGENT},
        timeout=10,
    )
    resp.raise_for_status()

    results = resp.json()
    if not results:
        raise ValueError(f"Could not geocode: '{place}'")

    result = results[0]

    # Nominatim returns boundingbox as [south, north, west, east] (strings)
    bb = result.get("boundingbox")
    if bb:
        south, north, west, east = [float(x) for x in bb]

        # Check if it's effectively a point (tiny bbox)
        if abs(north - south) < 0.001 and abs(east - west) < 0.001:
            # Point feature — add buffer
            lat = float(result["lat"])
            lon = float(result["lon"])
            return BBox(
                west=lon - buffer_deg,
                south=lat - buffer_deg,
                east=lon + buffer_deg,
                north=lat + buffer_deg,
            )

        return BBox(west=west, south=south, east=east, north=north)

    # Fallback to lat/lon point + buffer
    lat = float(result["lat"])
    lon = float(result["lon"])
    return BBox(
        west=lon - buffer_deg,
        south=lat - buffer_deg,
        east=lon + buffer_deg,
        north=lat + buffer_deg,
    )
