"""Direct-HTTP multipart uploads to the TikTok Business API v1.3.

The `business_api_client` SDK's file upload helpers were flaky (silent
500s, inconsistent multipart boundary handling) during the first
Meta→TikTok port on 2026-04-23. This module replaces them with a
straight `httpx.post(..., files=...)` that proved reliable.

Covers only the two upload calls the port workflow needs:

  • video:  POST https://business-api.tiktok.com/open_api/v1.3/file/video/ad/upload/
  • image:  POST https://business-api.tiktok.com/open_api/v1.3/file/image/ad/upload/

Both endpoints expect UPLOAD_BY_FILE + a md5 signature of the file bytes.
Returns the asset id string that TikTok subsequently accepts in
ad-creation calls.

Access token resolution:
  - If `access_token` is passed, use it.
  - Else read `TIKTOK_ACCESS_TOKEN` from the environment.
  - Never log the token — it's only echoed through `_masked()` in errors.
"""
from __future__ import annotations

import hashlib
import logging
import os
from typing import Any

import httpx

log = logging.getLogger(__name__)

_BASE = "https://business-api.tiktok.com/open_api/v1.3"


class TikTokUploadError(RuntimeError):
    pass


def _token(override: str | None) -> str:
    tok = (override or os.environ.get("TIKTOK_ACCESS_TOKEN") or "").strip()
    if not tok:
        raise TikTokUploadError(
            "No TikTok access token — pass access_token= or set TIKTOK_ACCESS_TOKEN"
        )
    return tok


def _masked(tok: str) -> str:
    if len(tok) < 8:
        return "***"
    return f"{tok[:4]}…{tok[-4:]}"


def _md5(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _expect_ok(payload: dict[str, Any], *, op: str) -> dict[str, Any]:
    code = payload.get("code")
    if code not in (0, "0"):
        raise TikTokUploadError(
            f"{op} failed: code={code} message={payload.get('message')!r} "
            f"request_id={payload.get('request_id')}"
        )
    data = payload.get("data")
    # TikTok video/ad/upload returns a single-element LIST of dicts in 2025+
    # (was a bare dict pre-2024). Normalise both shapes.
    if isinstance(data, list):
        if not data:
            raise TikTokUploadError(f"{op} returned empty list")
        data = data[0]
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise TikTokUploadError(f"{op} returned non-dict data: {data!r}")
    return data


async def upload_video(
    advertiser_id: str,
    mp4_path: str,
    *,
    access_token: str | None = None,
    flaw_detect: bool = True,
    filename: str | None = None,
    timeout: float = 180.0,
) -> str:
    """Upload an MP4 to the advertiser's video library. Returns video_id."""
    tok = _token(access_token)
    size = os.path.getsize(mp4_path)
    if size < 10_000:
        raise TikTokUploadError(f"MP4 too small to upload: {size} B")

    sig = _md5(mp4_path)
    url = f"{_BASE}/file/video/ad/upload/"
    headers = {"Access-Token": tok}
    data = {
        "advertiser_id": str(advertiser_id),
        "upload_type": "UPLOAD_BY_FILE",
        "video_signature": sig,
        "flaw_detect": "true" if flaw_detect else "false",
    }
    name = filename or os.path.basename(mp4_path)
    with open(mp4_path, "rb") as f:
        files = {"video_file": (name, f, "video/mp4")}
        async with httpx.AsyncClient(timeout=timeout) as cli:
            r = await cli.post(url, headers=headers, data=data, files=files)
    if r.status_code >= 400:
        raise TikTokUploadError(
            f"upload_video HTTP {r.status_code} "
            f"(token={_masked(tok)}): {r.text[:300]}"
        )
    payload = r.json()
    # 40911 = "Duplicated material name". TikTok dedups by FILENAME, not content.
    # Fix: rename with timestamp + retry. (We don't trust signature-search to
    # find the right existing video — saw it return the wrong row in testing.)
    if payload.get("code") == 40911 and "uplicate" in (payload.get("message") or ""):
        import time as _t
        new_name = f"{int(_t.time()*1000)}_{name}"
        with open(mp4_path, "rb") as f2:
            files2 = {"video_file": (new_name, f2, "video/mp4")}
            async with httpx.AsyncClient(timeout=timeout) as cli:
                r = await cli.post(url, headers=headers, data=data, files=files2)
        payload = r.json()
    data_out = _expect_ok(payload, op="video/ad/upload")
    # Response shape: { data: [ { video_id, ... } ] } or a bare object
    first = data_out[0] if isinstance(data_out, list) else data_out
    if isinstance(first, dict) and first.get("video_id"):
        video_id = str(first["video_id"])
    else:
        # Some responses: data: { video_id: ... }
        video_id = str(data_out.get("video_id") or "") if isinstance(data_out, dict) else ""
    if not video_id:
        raise TikTokUploadError(f"video/ad/upload: no video_id in {payload!r}")
    log.info(
        "tiktok upload_video ok advertiser=%s video_id=%s size=%d",
        advertiser_id, video_id, size,
    )
    return video_id


async def upload_image(
    advertiser_id: str,
    image_path: str,
    *,
    access_token: str | None = None,
    filename: str | None = None,
    timeout: float = 60.0,
) -> str:
    """Upload an image (cover) to the advertiser's image library. Returns image_id."""
    tok = _token(access_token)
    size = os.path.getsize(image_path)
    if size < 1_000:
        raise TikTokUploadError(f"Image too small to upload: {size} B")

    sig = _md5(image_path)
    url = f"{_BASE}/file/image/ad/upload/"
    headers = {"Access-Token": tok}
    data = {
        "advertiser_id": str(advertiser_id),
        "upload_type": "UPLOAD_BY_FILE",
        "image_signature": sig,
    }
    name = filename or os.path.basename(image_path)
    mime = "image/jpeg"
    if name.lower().endswith(".png"):
        mime = "image/png"
    elif name.lower().endswith(".webp"):
        mime = "image/webp"
    with open(image_path, "rb") as f:
        files = {"image_file": (name, f, mime)}
        async with httpx.AsyncClient(timeout=timeout) as cli:
            r = await cli.post(url, headers=headers, data=data, files=files)
    if r.status_code >= 400:
        raise TikTokUploadError(
            f"upload_image HTTP {r.status_code} "
            f"(token={_masked(tok)}): {r.text[:300]}"
        )
    payload = r.json()
    # Same filename-dedup as videos (40911). Rename + retry, don't trust search.
    if payload.get("code") == 40911 and "uplicate" in (payload.get("message") or ""):
        import time as _t
        new_name = f"{int(_t.time()*1000)}_{name}"
        with open(image_path, "rb") as f2:
            files2 = {"image_file": (new_name, f2, mime)}
            async with httpx.AsyncClient(timeout=timeout) as cli:
                r = await cli.post(url, headers=headers, data=data, files=files2)
        payload = r.json()
    data_out = _expect_ok(payload, op="image/ad/upload")
    image_id = ""
    if isinstance(data_out, dict):
        image_id = str(data_out.get("image_id") or data_out.get("id") or "")
    elif isinstance(data_out, list) and data_out:
        image_id = str(data_out[0].get("image_id") or "")
    if not image_id:
        raise TikTokUploadError(f"image/ad/upload: no image_id in {payload!r}")
    log.info(
        "tiktok upload_image ok advertiser=%s image_id=%s size=%d",
        advertiser_id, image_id, size,
    )
    return image_id
