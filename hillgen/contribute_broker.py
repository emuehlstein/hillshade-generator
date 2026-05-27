"""Broker-backed upload path for `hillgen run --contribute`.

The broker (AWS Lambda) authenticates a GitHub token, checks the allowlist,
and returns a short-lived presigned S3 PUT URL. The CLI then PUTs the file
directly to S3 over HTTPS — no AWS credentials live on the contributor's
machine.

Auth: we ask the local `gh` CLI for a token via ``gh auth token``. If `gh`
isn't installed or the user isn't logged in, ``get_github_token`` raises a
clear error pointing them at the install/login docs.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional, Tuple

import requests

DEFAULT_ENDPOINT = "https://api.scriptedrelief.com/v1/contribute"


# ── Auth ───────────────────────────────────────────────────────────────────────

class AuthError(RuntimeError):
    """Raised when we can't obtain a usable GitHub token."""


def get_github_token() -> str:
    """Return a GitHub token from the local ``gh`` CLI.

    Resolution order:
        1. ``HILLGEN_GITHUB_TOKEN`` env var (for CI / advanced users).
        2. ``gh auth token``.
    """
    env_token = os.environ.get("HILLGEN_GITHUB_TOKEN", "").strip()
    if env_token:
        return env_token

    gh = shutil.which("gh")
    if not gh:
        raise AuthError(
            "GitHub CLI (`gh`) not found. Install from https://cli.github.com "
            "and run `gh auth login`, or set HILLGEN_GITHUB_TOKEN."
        )

    try:
        result = subprocess.run(
            [gh, "auth", "token"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as e:
        raise AuthError(f"Failed to invoke `gh auth token`: {e}") from e

    if result.returncode != 0:
        raise AuthError(
            "`gh auth token` failed — run `gh auth login` first.\n"
            f"stderr: {result.stderr.strip()}"
        )

    token = result.stdout.strip()
    if not token:
        raise AuthError("`gh auth token` returned no token — run `gh auth login`.")
    return token


# ── Broker ─────────────────────────────────────────────────────────────────────

class BrokerError(RuntimeError):
    """Raised on any non-2xx broker response. ``code`` is the broker error code."""

    def __init__(self, code: str, message: str, status: int):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


def _endpoint() -> str:
    return os.environ.get("HILLGEN_CONTRIBUTE_ENDPOINT", DEFAULT_ENDPOINT).rstrip("/")


def request_presigned_put(
    stage: str,
    filename: str,
    size_bytes: int,
    *,
    token: Optional[str] = None,
    sha256: Optional[str] = None,
    timeout: float = 10.0,
) -> dict:
    """Ask the broker for a presigned PUT URL.

    Returns the parsed JSON response on success: ``{url, key, expires_in,
    required_headers}``. Raises :class:`BrokerError` on any 4xx/5xx.
    """
    token = token or get_github_token()
    body = {"stage": stage, "filename": filename, "size_bytes": int(size_bytes)}
    if sha256:
        body["sha256"] = sha256

    resp = requests.post(
        _endpoint(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        data=json.dumps(body),
        timeout=timeout,
    )
    try:
        payload = resp.json()
    except ValueError:
        payload = {}

    if resp.status_code // 100 != 2:
        raise BrokerError(
            payload.get("error", "http_error"),
            payload.get("message", resp.text[:200]),
            resp.status_code,
        )
    return payload


def upload_via_broker(
    local_path: Path,
    stage: str,
    *,
    token: Optional[str] = None,
    progress_cb=None,
) -> Tuple[bool, Optional[str]]:
    """Upload ``local_path`` to S3 through the broker.

    Returns ``(ok, reason)`` mirroring :func:`hillgen.cache_s3.push` so callers
    can swap in this transport without touching their error-handling. ``reason``
    is the broker error code (e.g. ``"not_allowlisted"``) on failure.
    """
    size = local_path.stat().st_size
    try:
        grant = request_presigned_put(stage, local_path.name, size, token=token)
    except AuthError as e:
        return False, f"AuthError: {e}"
    except BrokerError as e:
        return False, f"{e.code}: {e.message}"
    except requests.RequestException as e:
        return False, f"NetworkError: {e}"

    if progress_cb:
        progress_cb(f"  → broker granted PUT (expires in {grant.get('expires_in', '?')}s)")

    headers = dict(grant.get("required_headers") or {})
    try:
        with open(local_path, "rb") as f:
            put = requests.put(grant["url"], data=f, headers=headers, timeout=300)
    except (OSError, requests.RequestException) as e:
        return False, f"UploadError: {e}"

    if put.status_code // 100 != 2:
        return False, f"S3PutError {put.status_code}: {put.text[:200]}"
    return True, None


# ── Gallery submission ────────────────────────────────────────────────────────
#
# Issue #11: `hillgen publish --gallery` used to require AWS credentials to
# update gallery/catalog.json directly. Now the broker handles the read-modify-
# write server-side and the CLI only needs a GitHub token (same as --contribute).


def submit_gallery_entry(
    *,
    pmtiles_url: str,
    title: str,
    caption: str = "",
    author: Optional[str] = None,
    preview_url: Optional[str] = None,
    size_mb: Optional[float] = None,
    token: Optional[str] = None,
    timeout: float = 15.0,
) -> dict:
    """Ask the broker to append a gallery entry to gallery/catalog.json.

    Returns the broker's JSON response: ``{status, entry?, count}``.
    Raises :class:`BrokerError` on any 4xx/5xx.
    """
    token = token or get_github_token()
    body = {
        "action": "gallery_submit",
        "pmtiles_url": pmtiles_url,
        "title": title,
        "caption": caption,
    }
    if author:
        body["author"] = author
    if preview_url:
        body["preview_url"] = preview_url
    if size_mb is not None:
        body["size_mb"] = float(size_mb)

    resp = requests.post(
        _endpoint(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        data=json.dumps(body),
        timeout=timeout,
    )
    try:
        payload = resp.json()
    except ValueError:
        payload = {}

    if resp.status_code // 100 != 2:
        raise BrokerError(
            payload.get("error", "http_error"),
            payload.get("message", resp.text[:200]),
            resp.status_code,
        )
    return payload
