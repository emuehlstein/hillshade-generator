"""S3 read-through cache for hillgen intermediates.

Public reads: anonymous HTTPS GETs from s3://scriptedrelief-data/cache/
Writes: requires AWS credentials (alpha: direct upload with issued creds)

The S3 cache mirrors the local cache layout:
  s3://scriptedrelief-data/cache/dem/...
  s3://scriptedrelief-data/cache/reprojected/...
  s3://scriptedrelief-data/cache/hillshade/...
  s3://scriptedrelief-data/cache/styled/...
"""

from pathlib import Path
from typing import Optional

import requests

_DEFAULT_BUCKET = "scriptedrelief-data"
_DEFAULT_REGION = "us-east-2"


def s3_public_url(key: str, bucket: str = _DEFAULT_BUCKET, region: str = _DEFAULT_REGION) -> str:
    """Construct a public HTTPS URL for an S3 object."""
    return f"https://{bucket}.s3.{region}.amazonaws.com/{key}"


def cache_key_for(stage: str, filename: str) -> str:
    """Build an S3 cache key: cache/{stage}/{filename}"""
    return f"cache/{stage}/{filename}"


def try_pull(stage: str, filename: str, local_path: Path, bucket: str = _DEFAULT_BUCKET) -> bool:
    """Try to download a cached intermediate from S3.

    Returns True if successful, False if not found or error.
    Does not require AWS credentials — uses public HTTPS GET.
    """
    key = cache_key_for(stage, filename)
    url = s3_public_url(key, bucket)

    try:
        resp = requests.head(url, timeout=5)
        if resp.status_code != 200:
            return False

        # File exists in S3 — download it
        local_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = local_path.with_suffix(".s3tmp")

        with requests.get(url, stream=True, timeout=30) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=8 * 1024 * 1024):
                    f.write(chunk)

        tmp.rename(local_path)
        return True

    except (requests.RequestException, OSError):
        # Clean up partial download
        tmp = local_path.with_suffix(".s3tmp")
        if tmp.exists():
            tmp.unlink()
        return False


def push(local_path: Path, stage: str, filename: str, bucket: str = _DEFAULT_BUCKET) -> bool:
    """Upload a local intermediate to S3 cache.

    Requires AWS credentials (via boto3's default credential chain).
    Returns True if successful.
    """
    try:
        import boto3
    except ImportError:
        return False

    key = cache_key_for(stage, filename)

    try:
        s3 = boto3.client("s3")
        s3.upload_file(str(local_path), bucket, key)
        return True
    except Exception:
        return False


def exists(stage: str, filename: str, bucket: str = _DEFAULT_BUCKET) -> bool:
    """Check if a file exists in the S3 cache (anonymous HEAD request)."""
    key = cache_key_for(stage, filename)
    url = s3_public_url(key, bucket)
    try:
        resp = requests.head(url, timeout=5)
        return resp.status_code == 200
    except requests.RequestException:
        return False
