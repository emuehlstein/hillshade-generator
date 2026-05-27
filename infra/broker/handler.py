"""AWS Lambda handler — contribute broker for hillgen.

Validates a GitHub OAuth token, checks the contributor allowlist, and issues
a presigned S3 PUT URL scoped to a single cache key. The contributor's CLI
then uploads directly to S3 over HTTPS; no AWS credentials ever leave this
Lambda.

Request (POST /v1/contribute):
    Authorization: Bearer <github_token>
    {
        "stage": "reprojected" | "hillshade" | "styled",
        "filename": "<basename>.tif",
        "size_bytes": <int>,
        "sha256": "<hex>"          # optional, logged for audit
    }

Response 200:
    {
        "url": "https://...s3.amazonaws.com/...?X-Amz-Signature=...",
        "key": "cache/<stage>/<filename>",
        "expires_in": 900,
        "required_headers": {
            "Content-Length": "<size_bytes>",
            "x-amz-meta-contributor": "gh:<username>"
        }
    }

Response 4xx: ``{"error": "<short_code>", "message": "<detail>"}``

Environment:
    BUCKET                — target S3 bucket (default: scriptedrelief-data)
    ALLOWLIST_KEY         — S3 key for allowlist.json (default: config/allowlist.json)
    MAX_BYTES             — global upload cap (default: 2_147_483_648 == 2 GiB)
    PRESIGN_TTL_SECONDS   — URL lifetime (default: 900)
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

import boto3
import urllib.request
import urllib.error

logger = logging.getLogger()
logger.setLevel(logging.INFO)

BUCKET = os.environ.get("BUCKET", "scriptedrelief-data")
BUCKET_GALLERY = os.environ.get("BUCKET_GALLERY", "scriptedrelief")
GALLERY_CATALOG_KEY = os.environ.get("GALLERY_CATALOG_KEY", "gallery/catalog.json")
ALLOWLIST_KEY = os.environ.get("ALLOWLIST_KEY", "config/allowlist.json")
MAX_BYTES = int(os.environ.get("MAX_BYTES", str(2 * 1024 * 1024 * 1024)))
PRESIGN_TTL = int(os.environ.get("PRESIGN_TTL_SECONDS", "900"))

# Per-stage upload configuration. Cache stages target the data bucket under
# cache/<stage>/; gallery stages target the public web bucket under gallery/
# and accept different file extensions. Adding a stage = adding an entry here.
_TIF_RE = re.compile(r"^[A-Za-z0-9._-]+\.tif$")
_PMTILES_RE = re.compile(r"^[A-Za-z0-9._-]+\.pmtiles$")
_PREVIEW_RE = re.compile(r"^[A-Za-z0-9._-]+\.(png|jpg|jpeg)$")

STAGES = {
    "reprojected":     {"bucket": BUCKET,         "prefix": "cache/reprojected/", "filename_re": _TIF_RE},
    "hillshade":       {"bucket": BUCKET,         "prefix": "cache/hillshade/",   "filename_re": _TIF_RE},
    "styled":          {"bucket": BUCKET,         "prefix": "cache/styled/",      "filename_re": _TIF_RE},
    "gallery-pmtiles": {"bucket": BUCKET_GALLERY, "prefix": "gallery/",           "filename_re": _PMTILES_RE},
    "gallery-preview": {"bucket": BUCKET_GALLERY, "prefix": "gallery/",           "filename_re": _PREVIEW_RE},
}
ALLOWED_STAGES = set(STAGES.keys())
# Back-compat alias — older tests / docs may reference FILENAME_RE.
FILENAME_RE = _TIF_RE

s3 = boto3.client("s3")

# ── Allowlist cache (warm-Lambda lifetime, max 5 min) ──────────────────────
_ALLOWLIST_CACHE: dict[str, Any] = {"ts": 0.0, "data": None}
_ALLOWLIST_TTL_SECONDS = 300


def _load_allowlist() -> dict[str, Any]:
    now = time.time()
    if _ALLOWLIST_CACHE["data"] is not None and now - _ALLOWLIST_CACHE["ts"] < _ALLOWLIST_TTL_SECONDS:
        return _ALLOWLIST_CACHE["data"]
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=ALLOWLIST_KEY)
        data = json.loads(obj["Body"].read())
    except Exception as e:
        logger.exception("Failed to load allowlist from s3://%s/%s: %s", BUCKET, ALLOWLIST_KEY, e)
        # Fail closed — no allowlist means nobody can contribute.
        data = {"version": 1, "contributors": []}
    _ALLOWLIST_CACHE.update(ts=now, data=data)
    return data


def _allowlist_entry(username: str) -> dict[str, Any] | None:
    allow = _load_allowlist()
    for entry in allow.get("contributors", []):
        if entry.get("github", "").lower() == username.lower():
            return entry
    return None


def _verify_github_token(token: str) -> str | None:
    """Return the GitHub username for ``token`` or ``None`` if invalid."""
    req = urllib.request.Request(
        "https://api.github.com/user",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "hillgen-contribute-broker",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            if resp.status != 200:
                return None
            body = json.loads(resp.read())
            return body.get("login")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        return None


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _error(status: int, code: str, message: str) -> dict[str, Any]:
    return _response(status, {"error": code, "message": message})


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    # ── Extract bearer token ──────────────────────────────────────────────
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    auth = headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return _error(401, "missing_token", "Authorization: Bearer <github_token> required.")
    token = auth.split(None, 1)[1].strip()

    # ── Verify token with GitHub ──────────────────────────────────────────
    username = _verify_github_token(token)
    if not username:
        return _error(401, "invalid_token", "GitHub token did not validate against api.github.com/user.")

    # ── Allowlist check ───────────────────────────────────────────────────
    entry = _allowlist_entry(username)
    if entry is None:
        logger.info("contribute_denied user=%s reason=not_allowlisted", username)
        return _error(
            403,
            "not_allowlisted",
            f"GitHub user '{username}' is not on the hillgen contributor allowlist. "
            "Open an issue at https://github.com/emuehlstein/hillshade-generator to request access.",
        )

    # ── Parse body ────────────────────────────────────────────────────────
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return _error(400, "bad_request", "Body must be valid JSON.")

    # Issue #11: route by `action`. Default "presign" preserves the original
    # contract; "gallery_submit" appends an entry to gallery/catalog.json
    # server-side, so the CLI never needs AWS credentials to publish.
    action = payload.get("action", "presign")
    if action == "presign":
        return _handle_presign(payload, username, entry)
    if action == "gallery_submit":
        return _handle_gallery_submit(payload, username, entry)
    return _error(400, "bad_action", "action must be 'presign' or 'gallery_submit'.")


def _handle_presign(payload: dict[str, Any], username: str, entry: dict[str, Any]) -> dict[str, Any]:
    stage = payload.get("stage")
    filename = payload.get("filename")
    size_bytes = payload.get("size_bytes")

    if stage not in ALLOWED_STAGES:
        return _error(400, "bad_stage", f"stage must be one of {sorted(ALLOWED_STAGES)}.")
    stage_cfg = STAGES[stage]
    if not isinstance(filename, str) or not stage_cfg["filename_re"].match(filename):
        return _error(
            400,
            "bad_filename",
            f"filename must match {stage_cfg['filename_re'].pattern} (no path separators).",
        )
    if not isinstance(size_bytes, int) or size_bytes <= 0:
        return _error(400, "bad_size", "size_bytes must be a positive integer.")

    max_bytes = int(entry.get("max_bytes", MAX_BYTES))
    if size_bytes > max_bytes:
        return _error(
            413,
            "too_large",
            f"size_bytes={size_bytes} exceeds your per-file cap of {max_bytes}.",
        )

    target_bucket = stage_cfg["bucket"]
    key = f"{stage_cfg['prefix']}{filename}"
    contributor_tag = f"gh:{username}"
    try:
        url = s3.generate_presigned_url(
            ClientMethod="put_object",
            Params={
                "Bucket": target_bucket,
                "Key": key,
                "ContentLength": size_bytes,
                "Metadata": {"contributor": contributor_tag},
            },
            ExpiresIn=PRESIGN_TTL,
            HttpMethod="PUT",
        )
    except Exception as e:
        logger.exception("presign failed user=%s key=%s", username, key)
        return _error(500, "presign_failed", str(e))

    logger.info(
        "contribute_granted user=%s stage=%s bucket=%s key=%s bytes=%d sha256=%s",
        username, stage, target_bucket, key, size_bytes, payload.get("sha256", "-"),
    )

    return _response(200, {
        "url": url,
        "key": key,
        "bucket": target_bucket,
        "expires_in": PRESIGN_TTL,
        "required_headers": {
            "Content-Length": str(size_bytes),
            "x-amz-meta-contributor": contributor_tag,
        },
    })


_GALLERY_URL_RE = re.compile(r"^https://(scriptedrelief\.com|[A-Za-z0-9.-]+\.cloudfront\.net)/gallery/[A-Za-z0-9._/-]+\.pmtiles$")
_GALLERY_PREVIEW_URL_RE = re.compile(r"^https://(scriptedrelief\.com|[A-Za-z0-9.-]+\.cloudfront\.net)/gallery/[A-Za-z0-9._/-]+\.(png|jpg|jpeg)$")


def _handle_gallery_submit(payload: dict[str, Any], username: str, entry: dict[str, Any]) -> dict[str, Any]:
    """Append an entry to gallery/catalog.json under the public web bucket."""
    pmtiles_url = payload.get("pmtiles_url")
    preview_url = payload.get("preview_url")
    title = payload.get("title")
    caption = payload.get("caption", "")
    size_mb = payload.get("size_mb")
    author = payload.get("author") or username

    if not isinstance(pmtiles_url, str) or not _GALLERY_URL_RE.match(pmtiles_url):
        return _error(
            400,
            "bad_pmtiles_url",
            "pmtiles_url must be https://scriptedrelief.com/gallery/<file>.pmtiles (or matching CloudFront).",
        )
    if preview_url is not None and (
        not isinstance(preview_url, str) or not _GALLERY_PREVIEW_URL_RE.match(preview_url)
    ):
        return _error(400, "bad_preview_url", "preview_url must be a https://.../gallery/<file>.(png|jpg|jpeg) URL.")
    if not isinstance(title, str) or not title.strip():
        return _error(400, "bad_title", "title is required.")
    if size_mb is not None and not isinstance(size_mb, (int, float)):
        return _error(400, "bad_size_mb", "size_mb must be a number.")

    # Read-modify-write the catalog under the gallery bucket.
    try:
        try:
            obj = s3.get_object(Bucket=BUCKET_GALLERY, Key=GALLERY_CATALOG_KEY)
            catalog = json.loads(obj["Body"].read())
        except s3.exceptions.NoSuchKey:
            catalog = {"submissions": []}
        except Exception as e:  # noqa: BLE001 - want to surface boto's NoSuchKey variants too
            err_code = getattr(e, "response", {}).get("Error", {}).get("Code") if hasattr(e, "response") else None
            if err_code == "NoSuchKey":
                catalog = {"submissions": []}
            else:
                raise

        submissions = catalog.setdefault("submissions", [])
        if any(s.get("pmtiles") == pmtiles_url for s in submissions):
            logger.info("gallery_submit_duplicate user=%s url=%s", username, pmtiles_url)
            return _response(200, {"status": "duplicate", "pmtiles": pmtiles_url, "count": len(submissions)})

        new_entry = {
            "pmtiles": pmtiles_url,
            "preview": preview_url,
            "title": title.strip(),
            "caption": (caption or "").strip(),
            "author": author,
            "size_mb": round(float(size_mb), 1) if size_mb is not None else None,
            "submitted": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "submitted_by": f"gh:{username}",
        }
        submissions.append(new_entry)

        s3.put_object(
            Bucket=BUCKET_GALLERY,
            Key=GALLERY_CATALOG_KEY,
            Body=json.dumps(catalog, indent=2).encode(),
            ContentType="application/json",
            CacheControl="public, max-age=60",
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("gallery_submit failed user=%s", username)
        return _error(500, "catalog_write_failed", str(e))

    logger.info("gallery_submit_ok user=%s url=%s count=%d", username, pmtiles_url, len(submissions))
    return _response(200, {"status": "ok", "entry": new_entry, "count": len(submissions)})
