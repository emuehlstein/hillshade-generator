"""Base class for DEM sources."""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class BBox:
    """Bounding box in WGS84 (EPSG:4326)."""
    west: float
    south: float
    east: float
    north: float

    def __post_init__(self):
        if self.west >= self.east:
            raise ValueError(f"west ({self.west}) must be less than east ({self.east})")
        if self.south >= self.north:
            raise ValueError(f"south ({self.south}) must be less than north ({self.north})")
        if not (-180 <= self.west <= 180 and -180 <= self.east <= 180):
            raise ValueError(f"Longitude out of range: west={self.west}, east={self.east}")
        if not (-90 <= self.south <= 90 and -90 <= self.north <= 90):
            raise ValueError(f"Latitude out of range: south={self.south}, north={self.north}")

    @classmethod
    def from_string(cls, s: str) -> "BBox":
        """Parse 'west,south,east,north' string."""
        parts = [float(x.strip()) for x in s.split(",")]
        if len(parts) != 4:
            raise ValueError(f"Expected 4 values (west,south,east,north), got {len(parts)}")
        return cls(west=parts[0], south=parts[1], east=parts[2], north=parts[3])

    def __str__(self):
        return f"{self.west},{self.south},{self.east},{self.north}"


class DEMSource(Protocol):
    """Interface for DEM data sources."""

    name: str
    description: str
    resolution_m: float
    priority: int

    def covers(self, bbox: BBox) -> bool:
        """Does this source have data covering the requested area?"""
        ...

    def download(self, bbox: BBox, output_dir: Path, progress_cb=None) -> Path:
        """Download DEM tiles for bbox, merge, and return path to merged GeoTIFF.

        Args:
            bbox: Area to download
            output_dir: Directory to save the output file
            progress_cb: Optional callback(message: str) for progress updates

        Returns:
            Path to the merged GeoTIFF
        """
        ...
