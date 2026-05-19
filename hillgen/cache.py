"""Local cache management for hillgen intermediates."""

import os
from pathlib import Path

# Default cache directory
_DEFAULT_CACHE_DIR = Path.home() / ".hillgen" / "cache"


def get_cache_dir() -> Path:
    """Return the cache directory, respecting HILLGEN_CACHE env var."""
    env = os.environ.get("HILLGEN_CACHE")
    if env:
        return Path(env)
    return _DEFAULT_CACHE_DIR


def ensure_cache_dir(stage: str = "") -> Path:
    """Create and return the cache directory (or a stage subdirectory)."""
    cache_dir = get_cache_dir()
    if stage:
        cache_dir = cache_dir / stage
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir
