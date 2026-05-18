"""Export a Meta (Facebook) video creative for port into another ad platform.

The Meta→TikTok launch workflow needs:

  1. The Meta ad's video_id (from `creative.object_story_spec.video_data.video_id`).
  2. A fresh short-lived `source` URL for that video object — Graph API v23+
     returns this behind `/{video_id}?fields=source`.
  3. The thumbnail (either a generated first-frame or `creative.thumbnail_url`).

This module wraps those three calls + the MP4 / JPG download into a single
`fetch_meta_ad_video(ad_id)` coroutine. Files land in a tempdir and are
returned as absolute paths; the caller is responsible for cleanup.

Hard failures (ad not found, creative has no video, source URL not returned)
raise `MetaVideoExportError` rather than returning partial data — the port
workflow needs an all-or-nothing guarantee before it starts creating TikTok
objects.
"""
from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from ads_agent.meta.graph_client import _get, creative_details

log = logging.getLogger(__name__)


class MetaVideoExportError(RuntimeError):
    """Raised when we cannot produce a complete (mp4 + thumbnail) bundle."""


@dataclass(frozen=True)
class MetaVideoBundle:
    ad_id: str
    ad_name: str
    video_id: str
    mp4_path: str
    thumbnail_path: str
    mp4_md5: str
    thumbnail_md5: str
    source_ad_meta: dict


def _md5_file(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


async def _download(url: str, dst: str) -> None:
    async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as cli:
        async with cli.stream("GET", url) as r:
            if r.status_code >= 400:
                raise MetaVideoExportError(
                    f"download failed {r.status_code} for {url[:120]}"
                )
            with open(dst, "wb") as f:
                async for chunk in r.aiter_bytes(1 << 20):
                    f.write(chunk)


async def fetch_meta_ad_video(
    ad_id: str, *, workdir: str | None = None
) -> MetaVideoBundle:
    """Pull the MP4 + thumbnail for a Meta video ad into a local tempdir.

    Raises MetaVideoExportError on any step that can't produce the expected
    asset — the caller should treat this as a hard stop.
    """
    ad = await creative_details(ad_id, days=7)
    creative = ad.get("creative") or {}

    # video_id can live in two places depending on creative shape
    video_id = (
        (creative.get("object_story_spec") or {})
        .get("video_data", {})
        .get("video_id")
        or creative.get("video_id")
        or ""
    )
    if not video_id:
        raise MetaVideoExportError(
            f"ad {ad_id} has no video_id in creative (object_type="
            f"{creative.get('object_type')!r}); likely a SHARE or image ad"
        )

    # Fresh source URL + picture URL from the video object itself
    video_obj = await _get(
        str(video_id),
        {"fields": "source,picture,thumbnails{uri,is_preferred}"},
    )
    source_url = video_obj.get("source") or ""
    if not source_url:
        raise MetaVideoExportError(
            f"video {video_id} did not return a fresh source URL "
            "(fb_exchange_token may be needed)"
        )

    thumbnail_url = (
        video_obj.get("picture")
        or (
            next(
                (t.get("uri") for t in (video_obj.get("thumbnails") or {}).get("data", []) if t.get("is_preferred")),
                None,
            )
        )
        or creative.get("thumbnail_url")
        or ""
    )
    if not thumbnail_url:
        raise MetaVideoExportError(
            f"no thumbnail URL for video {video_id}; TikTok ad creation "
            "requires a cover image"
        )

    work = Path(workdir or tempfile.mkdtemp(prefix="meta_port_"))
    work.mkdir(parents=True, exist_ok=True)
    mp4_path = str(work / f"{video_id}.mp4")
    thumb_path = str(work / f"{video_id}.jpg")

    await _download(source_url, mp4_path)
    await _download(thumbnail_url, thumb_path)

    mp4_size = os.path.getsize(mp4_path)
    thumb_size = os.path.getsize(thumb_path)
    if mp4_size < 10_000:
        raise MetaVideoExportError(f"downloaded MP4 is suspiciously small ({mp4_size} B)")
    if thumb_size < 1_000:
        raise MetaVideoExportError(f"downloaded thumbnail is suspiciously small ({thumb_size} B)")

    log.info(
        "meta_video_export: ad=%s video=%s mp4=%d B thumb=%d B",
        ad_id, video_id, mp4_size, thumb_size,
    )
    return MetaVideoBundle(
        ad_id=ad_id,
        ad_name=ad.get("ad_name", ""),
        video_id=str(video_id),
        mp4_path=mp4_path,
        thumbnail_path=thumb_path,
        mp4_md5=_md5_file(mp4_path),
        thumbnail_md5=_md5_file(thumb_path),
        source_ad_meta={
            "status": ad.get("status"),
            "effective_status": ad.get("effective_status"),
            "currency": ad.get("currency"),
            "spend": ad.get("spend"),
            "purchases": ad.get("purchases"),
            "purchase_value": ad.get("purchase_value"),
            "reported_roas": ad.get("reported_roas"),
        },
    )
