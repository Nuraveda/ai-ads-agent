"""Port a winning Meta video ad into a new TikTok Conversions ad.

End-to-end orchestrator for the workflow we ran manually on 2026-04-23 for
example Global. Packaged so the agent can re-run it for any (meta_ad_id,
tiktok_slug) pair without the operator reassembling the HTTP calls.

## Flow

  1. Load TikTok context (advertiser / identity / pixel / locations)
     from STORE_TIKTOK_ACCOUNTS_JSON for `tiktok_slug`.
  2. Fetch the Meta video bundle — MP4 + thumbnail — into a tempdir.
  3. Pick the best available lower-funnel optimization event from the
     TikTok pixel's live events (preference: PURCHASE → ... → ON_WEB_DETAIL).
  4. Upload video + cover image to the TikTok advertiser library.
  5. Create campaign (CONVERSIONS, daily budget, DISABLED).
  6. Create adgroup (pixel + optimization_event + OCPM, DISABLED).
  7. Create ad (SINGLE_VIDEO + identity + cover, DISABLED).
  8. Persist a manifest JSON so a separate `/enable_tiktok_launch` call
     can flip campaign/adgroup/ad to ENABLE after human review.

## Safety

  - Everything created is DISABLED. There is no `dry_run=False` path that
    creates + enables in one shot — the enable flip is a separate public
    call so an autonomous slip cannot spend money.
  - Budget is checked against TikTok's floor (50/day in advertiser currency)
    before any HTTP call.
  - Advertiser currency vs. passed budget is logged, not enforced — some
    brands deliberately run tight low-budget TT tests.

## Manifest

  Written to LAUNCH_MANIFEST_DIR/<slug>__<ts>.json, readable by
  `enable_launch(manifest_id)` to flip statuses later.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ads_agent.agent.nodes.tiktok_common import TikTokContext, load_tiktok_context
from ads_agent.meta.video_export import MetaVideoExportError, fetch_meta_ad_video
from ads_agent.tiktok.client import TikTokError, pick_optimization_event
from ads_agent.tiktok.creatives import (
    TikTokCreativeError,
    create_ad,
    create_adgroup,
    create_campaign,
    update_ad_status,
    update_adgroup_status,
)
from ads_agent.tiktok.uploads import TikTokUploadError, upload_image, upload_video

log = logging.getLogger(__name__)

LAUNCH_MANIFEST_DIR = Path(
    os.environ.get("TIKTOK_LAUNCH_MANIFEST_DIR", "/var/log/ads-agent/tiktok_launches")
)


class PortError(RuntimeError):
    """Any failure during the port workflow, annotated with which step failed."""


@dataclass
class LaunchManifest:
    manifest_id: str
    created_at: str
    tiktok_slug: str
    advertiser_id: str
    meta_ad_id: str
    landing_url: str
    campaign_id: str
    adgroup_id: str
    ad_id: str
    chose_event: str
    available_events: list[str]
    video_id: str
    image_id: str
    identity_id: str
    identity_type: str
    daily_budget: float
    currency: str
    status: str  # "built_disabled" | "enabled" | "paused_by_user"
    source_meta_ad_name: str
    source_meta_video_id: str


def _manifest_path(mid: str) -> Path:
    LAUNCH_MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    return LAUNCH_MANIFEST_DIR / f"{mid}.json"


def _write_manifest(m: LaunchManifest) -> Path:
    path = _manifest_path(m.manifest_id)
    path.write_text(json.dumps(asdict(m), indent=2))
    return path


def _read_manifest(mid: str) -> LaunchManifest:
    path = _manifest_path(mid)
    if not path.exists():
        raise PortError(f"manifest not found: {mid}")
    raw = json.loads(path.read_text())
    return LaunchManifest(**raw)


def _require_context_fields(ctx: TikTokContext) -> None:
    missing = []
    if not ctx.identity_id:           missing.append("identity_id")
    if not ctx.identity_type:         missing.append("identity_type")
    if not ctx.pixel_id:              missing.append("pixel_id")
    if not ctx.default_location_ids:  missing.append("default_location_ids")
    if missing:
        raise PortError(
            f"TikTok slug `{ctx.store.slug}` is missing {missing} in "
            "STORE_TIKTOK_ACCOUNTS_JSON. Fill these in .env before running "
            "the Meta→TikTok port."
        )


async def port_meta_ad(
    *,
    meta_ad_id: str,
    tiktok_slug: str,
    landing_url: str,
    ad_text: str,
    display_name: str,
    daily_budget: float,
    bid_price: float,
    call_to_action: str = "LEARN_MORE",
    campaign_name: str | None = None,
    adgroup_name: str | None = None,
    ad_name: str | None = None,
    preferred_events: tuple[str, ...] | None = None,
    tiktok_context_overrides: dict[str, Any] | None = None,
) -> LaunchManifest:
    """Build a DISABLED TikTok launch from a Meta winner. Returns manifest."""
    # 1. Context + required fields
    ctx, err = await load_tiktok_context(tiktok_slug)
    if err or ctx is None:
        raise PortError(err or f"no TikTok context for {tiktok_slug}")
    overrides = tiktok_context_overrides or {}
    if overrides:
        ctx = TikTokContext(
            store=ctx.store,
            advertiser_id=str(overrides.get("tiktok_advertiser_id") or ctx.advertiser_id),
            country=ctx.country,
            access_token=ctx.access_token,
            auth_source=ctx.auth_source,
            identity_id=str(overrides.get("identity_id") or ctx.identity_id or ""),
            identity_type=str(overrides.get("identity_type") or ctx.identity_type or ""),
            pixel_id=str(overrides.get("pixel_id") or ctx.pixel_id or ""),
            currency=str(overrides.get("currency") or ctx.currency or ""),
            default_location_ids=tuple(
                str(x)
                for x in (
                    overrides.get("default_location_ids")
                    or ctx.default_location_ids
                    or ()
                )
                if x
            ),
        )
    _require_context_fields(ctx)

    ts = time.strftime("%Y%m%d-%H%M%S")
    campaign_name = campaign_name or f"Meta-port · {meta_ad_id} · {ts}"
    adgroup_name  = adgroup_name  or f"Meta-port AG · {meta_ad_id}"
    ad_name       = ad_name       or f"Meta-port ad · {meta_ad_id}"

    # 2. Meta video bundle
    try:
        bundle = await fetch_meta_ad_video(meta_ad_id)
    except MetaVideoExportError as e:
        raise PortError(f"meta export failed: {e}") from e

    # 3. Pick optimization event from the live pixel
    try:
        chose_event, available_events = await pick_optimization_event(
            ctx.advertiser_id,
            ctx.pixel_id or "",
            preference=preferred_events or (
                # TikTok API enums (not display names) — see PREFERRED_OPTIMIZATION_EVENTS
                "SHOPPING", "INITIATE_ORDER", "ADD_BILLING",
                "ON_WEB_CART", "ON_WEB_DETAIL", "ON_WEB_ORDER",
            ),
            access_token=ctx.access_token,
        )
    except TikTokError as e:
        raise PortError(f"pixel event pick failed: {e}") from e

    # 4. Upload video + cover
    try:
        tt_video_id = await upload_video(
            ctx.advertiser_id, bundle.mp4_path, access_token=ctx.access_token,
        )
        tt_image_id = await upload_image(
            ctx.advertiser_id, bundle.thumbnail_path, access_token=ctx.access_token,
        )
    except TikTokUploadError as e:
        raise PortError(f"upload failed: {e}") from e

    # 5-7. Create campaign/adgroup/ad — everything DISABLED
    try:
        campaign_id = await create_campaign(
            ctx.advertiser_id,
            campaign_name=campaign_name,
            objective_type="CONVERSIONS",
            budget=daily_budget,
            operation_status="DISABLE",
            access_token=ctx.access_token,
        )
        adgroup_id = await create_adgroup(
            ctx.advertiser_id, campaign_id,
            adgroup_name=adgroup_name,
            location_ids=ctx.default_location_ids,
            pixel_id=ctx.pixel_id or "",
            optimization_event=chose_event,
            budget=daily_budget,
            bid_price=bid_price,
            schedule_start_time=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(time.time() + 600)),
            operation_status="DISABLE",
            access_token=ctx.access_token,
        )
        ad_id = await create_ad(
            ctx.advertiser_id, adgroup_id,
            ad_name=ad_name,
            identity_id=ctx.identity_id or "",
            identity_type=ctx.identity_type or "TT_USER",
            video_id=tt_video_id,
            image_id=tt_image_id,
            landing_url=landing_url,
            ad_text=ad_text,
            display_name=display_name,
            call_to_action=call_to_action,
            operation_status="DISABLE",
            access_token=ctx.access_token,
        )
    except TikTokCreativeError as e:
        raise PortError(f"creative create failed: {e}") from e

    # 8. Manifest
    manifest = LaunchManifest(
        manifest_id=f"{tiktok_slug}__{ts}",
        created_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        tiktok_slug=tiktok_slug,
        advertiser_id=ctx.advertiser_id,
        meta_ad_id=meta_ad_id,
        landing_url=landing_url,
        campaign_id=campaign_id,
        adgroup_id=adgroup_id,
        ad_id=ad_id,
        chose_event=chose_event,
        available_events=available_events,
        video_id=tt_video_id,
        image_id=tt_image_id,
        identity_id=ctx.identity_id or "",
        identity_type=ctx.identity_type or "TT_USER",
        daily_budget=float(daily_budget),
        currency=ctx.currency or "",
        status="built_disabled",
        source_meta_ad_name=bundle.ad_name,
        source_meta_video_id=bundle.video_id,
    )
    path = _write_manifest(manifest)
    log.info("port_meta_to_tiktok: manifest written → %s", path)
    return manifest


async def enable_launch(manifest_id: str) -> LaunchManifest:
    """Flip campaign + adgroup + ad from DISABLE → ENABLE.

    Campaign status flip uses the existing client wrapper; adgroup and ad
    use the helpers in creatives.py.
    """
    from ads_agent.tiktok.client import update_campaign_status  # local import: avoid SDK load at module import
    m = _read_manifest(manifest_id)
    if m.status == "enabled":
        return m
    ctx, err = await load_tiktok_context(m.tiktok_slug)
    if err or ctx is None:
        raise PortError(err or f"no TikTok context for {m.tiktok_slug}")

    await update_campaign_status(
        ctx.advertiser_id, m.campaign_id,
        operation_status="ENABLE",
        access_token=ctx.access_token,
    )
    await update_adgroup_status(
        ctx.advertiser_id, m.adgroup_id,
        operation_status="ENABLE",
        access_token=ctx.access_token,
    )
    await update_ad_status(
        ctx.advertiser_id, m.ad_id,
        operation_status="ENABLE",
        access_token=ctx.access_token,
    )
    m.status = "enabled"
    _write_manifest(m)
    return m


def list_recent_manifests(limit: int = 20) -> list[dict[str, Any]]:
    """Summaries of recent launch manifests (newest first)."""
    if not LAUNCH_MANIFEST_DIR.exists():
        return []
    files = sorted(LAUNCH_MANIFEST_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: list[dict[str, Any]] = []
    for p in files[:limit]:
        try:
            out.append(json.loads(p.read_text()))
        except Exception:  # noqa: BLE001
            continue
    return out
