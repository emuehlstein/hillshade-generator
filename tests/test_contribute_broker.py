"""Tests for hillgen.contribute_broker — CLI-side broker client."""

from __future__ import annotations

import json
from pathlib import Path
from unittest import mock

import pytest
import requests

from hillgen import contribute_broker as cb


# ── get_github_token ──────────────────────────────────────────────────────────

def test_get_github_token_prefers_env(monkeypatch):
    monkeypatch.setenv("HILLGEN_GITHUB_TOKEN", "ghp_envvalue")
    assert cb.get_github_token() == "ghp_envvalue"


def test_get_github_token_missing_gh(monkeypatch):
    monkeypatch.delenv("HILLGEN_GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(cb.shutil, "which", lambda _: None)
    with pytest.raises(cb.AuthError, match="GitHub CLI"):
        cb.get_github_token()


def test_get_github_token_gh_fails(monkeypatch):
    monkeypatch.delenv("HILLGEN_GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(cb.shutil, "which", lambda _: "/usr/bin/gh")
    fake = mock.Mock(returncode=1, stdout="", stderr="not logged in")
    monkeypatch.setattr(cb.subprocess, "run", lambda *a, **k: fake)
    with pytest.raises(cb.AuthError, match="gh auth token"):
        cb.get_github_token()


def test_get_github_token_gh_ok(monkeypatch):
    monkeypatch.delenv("HILLGEN_GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(cb.shutil, "which", lambda _: "/usr/bin/gh")
    fake = mock.Mock(returncode=0, stdout="ghp_fromgh\n", stderr="")
    monkeypatch.setattr(cb.subprocess, "run", lambda *a, **k: fake)
    assert cb.get_github_token() == "ghp_fromgh"


# ── request_presigned_put ─────────────────────────────────────────────────────

def _fake_response(status: int, body: dict) -> mock.Mock:
    r = mock.Mock(spec=requests.Response)
    r.status_code = status
    r.json.return_value = body
    r.text = json.dumps(body)
    return r


def test_request_presigned_put_success(monkeypatch):
    payload = {
        "url": "https://s3.amazonaws.com/bucket/cache/hillshade/x.tif?sig=abc",
        "key": "cache/hillshade/x.tif",
        "expires_in": 900,
        "required_headers": {"Content-Length": "12345"},
    }
    captured = {}

    def fake_post(url, headers=None, data=None, timeout=None):
        captured["url"] = url
        captured["headers"] = headers
        captured["body"] = json.loads(data)
        return _fake_response(200, payload)

    monkeypatch.setattr(cb.requests, "post", fake_post)

    result = cb.request_presigned_put(
        "hillshade", "x.tif", 12345, token="ghp_x", sha256="deadbeef",
    )
    assert result == payload
    assert captured["headers"]["Authorization"] == "Bearer ghp_x"
    assert captured["body"]["stage"] == "hillshade"
    assert captured["body"]["filename"] == "x.tif"
    assert captured["body"]["size_bytes"] == 12345
    assert captured["body"]["sha256"] == "deadbeef"


def test_request_presigned_put_raises_on_403(monkeypatch):
    monkeypatch.setattr(
        cb.requests, "post",
        lambda *a, **k: _fake_response(403, {"error": "not_allowlisted", "message": "nope"}),
    )
    with pytest.raises(cb.BrokerError) as ei:
        cb.request_presigned_put("hillshade", "x.tif", 100, token="t")
    assert ei.value.code == "not_allowlisted"
    assert ei.value.status == 403


def test_request_presigned_put_uses_env_endpoint(monkeypatch):
    monkeypatch.setenv("HILLGEN_CONTRIBUTE_ENDPOINT", "https://custom.example.com/v1/contribute/")
    seen = {}

    def fake_post(url, **_kw):
        seen["url"] = url
        return _fake_response(200, {"url": "x", "key": "y"})

    monkeypatch.setattr(cb.requests, "post", fake_post)
    cb.request_presigned_put("styled", "x.tif", 1, token="t")
    # Trailing slash stripped
    assert seen["url"] == "https://custom.example.com/v1/contribute"


# ── upload_via_broker ─────────────────────────────────────────────────────────

def test_upload_via_broker_full_path(monkeypatch, tmp_path: Path):
    local = tmp_path / "x.tif"
    local.write_bytes(b"\x00" * 1024)

    monkeypatch.setattr(
        cb, "request_presigned_put",
        lambda *a, **k: {
            "url": "https://s3.example.com/put",
            "key": "cache/hillshade/x.tif",
            "expires_in": 900,
            "required_headers": {"Content-Length": "1024",
                                 "x-amz-meta-contributor": "gh:alice"},
        },
    )

    put_seen = {}

    def fake_put(url, data=None, headers=None, timeout=None):
        put_seen["url"] = url
        put_seen["headers"] = headers
        r = mock.Mock(spec=requests.Response)
        r.status_code = 200
        r.text = ""
        return r

    monkeypatch.setattr(cb.requests, "put", fake_put)

    ok, reason = cb.upload_via_broker(local, "hillshade", token="t")
    assert ok and reason is None
    assert put_seen["url"] == "https://s3.example.com/put"
    assert put_seen["headers"]["x-amz-meta-contributor"] == "gh:alice"


def test_upload_via_broker_surfaces_broker_error(monkeypatch, tmp_path: Path):
    local = tmp_path / "x.tif"
    local.write_bytes(b"\x00")
    monkeypatch.setattr(
        cb, "request_presigned_put",
        mock.Mock(side_effect=cb.BrokerError("not_allowlisted", "nope", 403)),
    )
    ok, reason = cb.upload_via_broker(local, "hillshade", token="t")
    assert not ok
    assert "not_allowlisted" in reason


def test_upload_via_broker_surfaces_auth_error(monkeypatch, tmp_path: Path):
    local = tmp_path / "x.tif"
    local.write_bytes(b"\x00")
    monkeypatch.setattr(
        cb, "request_presigned_put",
        mock.Mock(side_effect=cb.AuthError("gh missing")),
    )
    ok, reason = cb.upload_via_broker(local, "hillshade", token="t")
    assert not ok
    assert "AuthError" in reason


def test_upload_via_broker_surfaces_s3_put_failure(monkeypatch, tmp_path: Path):
    local = tmp_path / "x.tif"
    local.write_bytes(b"\x00")
    monkeypatch.setattr(
        cb, "request_presigned_put",
        lambda *a, **k: {
            "url": "https://s3.example.com/put",
            "key": "cache/hillshade/x.tif",
            "expires_in": 900,
            "required_headers": {},
        },
    )
    r = mock.Mock(spec=requests.Response)
    r.status_code = 403
    r.text = "AccessDenied"
    monkeypatch.setattr(cb.requests, "put", lambda *a, **k: r)
    ok, reason = cb.upload_via_broker(local, "hillshade", token="t")
    assert not ok
    assert "S3PutError 403" in reason


# ── submit_gallery_entry (issue #11) ──────────────────────────────────────────

def test_submit_gallery_entry_sends_expected_body(monkeypatch):
    captured = {}

    def fake_post(url, headers=None, data=None, timeout=None):
        captured["url"] = url
        captured["body"] = json.loads(data)
        captured["headers"] = headers
        return _fake_response(200, {"status": "ok", "entry": {}, "count": 1})

    monkeypatch.setattr(cb.requests, "post", fake_post)

    out = cb.submit_gallery_entry(
        pmtiles_url="https://scriptedrelief.com/gallery/foo.pmtiles",
        title="Foo",
        caption="A test caption",
        author="alice",
        preview_url="https://scriptedrelief.com/gallery/preview-foo.png",
        size_mb=12.3,
        token="ghp_test",
    )
    assert out["status"] == "ok"
    assert captured["body"]["action"] == "gallery_submit"
    assert captured["body"]["pmtiles_url"].endswith("/gallery/foo.pmtiles")
    assert captured["body"]["title"] == "Foo"
    assert captured["body"]["author"] == "alice"
    assert captured["body"]["size_mb"] == 12.3
    assert captured["headers"]["Authorization"] == "Bearer ghp_test"


def test_submit_gallery_entry_surfaces_broker_error(monkeypatch):
    monkeypatch.setattr(
        cb.requests, "post",
        lambda *a, **k: _fake_response(403, {"error": "not_allowlisted", "message": "nope"}),
    )
    with pytest.raises(cb.BrokerError) as ei:
        cb.submit_gallery_entry(
            pmtiles_url="https://scriptedrelief.com/gallery/foo.pmtiles",
            title="Foo",
            token="t",
        )
    assert ei.value.code == "not_allowlisted"


def test_submit_gallery_entry_duplicate_returns_payload(monkeypatch):
    monkeypatch.setattr(
        cb.requests, "post",
        lambda *a, **k: _fake_response(
            200, {"status": "duplicate", "pmtiles": "https://scriptedrelief.com/gallery/foo.pmtiles", "count": 7}
        ),
    )
    out = cb.submit_gallery_entry(
        pmtiles_url="https://scriptedrelief.com/gallery/foo.pmtiles",
        title="Foo",
        token="t",
    )
    assert out["status"] == "duplicate"
    assert out["count"] == 7
