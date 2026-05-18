"""Create-path TikTok Business API calls the SDK wrapper doesn't cover.

The `business_api_client` Python SDK we vendor exposes read + status/budget
update but not:

    • campaign/create           (CONVERSIONS objective w/ required budget)
    • adgroup/create            (pixel + optimization_event + OCPM pricing)
    • ad/create                 (SINGLE_VIDEO + identity + cover image)

This module is a thin, explicit wrapper over the v1.3 REST endpoints that
the 2026-04-23 example launch proved reliable. Constraints baked in:

  - Minimum daily budget: 50 (in advertiser currency). Enforced client-side
    so the error is actionable, not a "code 40002: invalid budget" from TT.
  - Campaign create requires budget fields even when operation_status=DISABLE.
  - Ad text must be ≤ 100 characters (truncated with log warning).
  - `ad_format=SINGLE_VIDEO` requires a cover image_id.

Every call returns the primary id (`campaign_id` / `adgroup_id` / `ad_id`)
as a string. On non-zero TikTok `code`, raises TikTokCreativeError with the
full response body so the operator sees exactly what TikTok rejected.
"""
from __future__ import annotations

import logging
import os
from typing import Any, Sequence

import httpx

log = logging.getLogger(__name__)

_BASE = "https://business-api.tiktok.com/open_api/v1.3"
MIN_DAILY_BUDGET = 50.0
AD_TEXT_MAX = 100


class TikTokCreativeError(RuntimeError):
    pass


def _token(override: str | None) -> str:
    tok = (override or os.environ.get("TIKTOK_ACCESS_TOKEN") or "").strip()
    if not tok:
        raise TikTokCreativeError("No TikTok access token available")
    return tok


async def _post(
    path: str, body: dict[str, Any], *, access_token: str | None, timeout: float = 60.0
) -> dict[str, Any]:
    tok = _token(access_token)
    url = f"{_BASE}{path}"
    async with httpx.AsyncClient(timeout=timeout) as cli:
        r = await cli.post(url, headers={"Access-Token": tok, "Content-Type": "application/json"}, json=body)
    if r.status_code >= 400:
        raise TikTokCreativeError(f"{path} HTTP {r.status_code}: {r.text[:400]}")
    payload = r.json()
    if payload.get("code") not in (0, "0"):
        raise TikTokCreativeError(
            f"{path} code={payload.get('code')} "
            f"message={payload.get('message')!r} "
            f"request_id={payload.get('request_id')}"
        )
    return payload.get("data") or {}


# ---------- campaign ---------------------------------------------------------

async def create_campaign(
    advertiser_id: str,
    *,
    campaign_name: str,
    objective_type: str = "CONVERSIONS",
    budget: float,
    budget_mode: str = "BUDGET_MODE_DAY",
    operation_status: str = "DISABLE",
    access_token: str | None = None,
) -> str:
    if budget < MIN_DAILY_BUDGET:
        raise TikTokCreativeError(
            f"campaign budget {budget} below TikTok minimum {MIN_DAILY_BUDGET}"
        )
    body = {
        "advertiser_id": str(advertiser_id),
        "campaign_name": campaign_name[:512],
        "objective_type": objective_type,
        "budget_mode": budget_mode,
        "budget": float(budget),
        "operation_status": operation_status,
    }
    data = await _post("/campaign/create/", body, access_token=access_token)
    cid = str(data.get("campaign_id") or "")
    if not cid:
        raise TikTokCreativeError(f"campaign/create returned no campaign_id: {data!r}")
    log.info("tiktok create_campaign ok %s → %s", campaign_name, cid)
    return cid


# ---------- adgroup ----------------------------------------------------------

async def create_adgroup(
    advertiser_id: str,
    campaign_id: str,
    *,
    adgroup_name: str,
    location_ids: Sequence[str],
    pixel_id: str,
    optimization_event: str,
    budget: float,
    bid_price: float,
    schedule_start_time: str,
    placement_type: str = "PLACEMENT_TYPE_NORMAL",
    placements: Sequence[str] = ("PLACEMENT_TIKTOK",),
    billing_event: str = "OCPM",
    optimization_goal: str = "CONVERT",
    promotion_type: str = "WEBSITE",
    promotion_target_type: str = "EXTERNAL_WEBSITE",
    budget_mode: str = "BUDGET_MODE_DAY",
    operation_status: str = "DISABLE",
    access_token: str | None = None,
) -> str:
    if budget < MIN_DAILY_BUDGET:
        raise TikTokCreativeError(
            f"adgroup budget {budget} below TikTok minimum {MIN_DAILY_BUDGET}"
        )
    if not location_ids:
        raise TikTokCreativeError("adgroup create requires at least one location_id")
    # For OCPM + CONVERT goal, TikTok requires `conversion_bid_price` (cost-per-conversion target).
    # `bid_price` is for non-OCPM legacy CPC bidding. Map appropriately:
    body = {
        "advertiser_id": str(advertiser_id),
        "campaign_id": str(campaign_id),
        "adgroup_name": adgroup_name[:512],
        "placement_type": placement_type,
        "placements": list(placements),
        "promotion_type": promotion_type,
        "promotion_target_type": promotion_target_type,
        "location_ids": [str(x) for x in location_ids],
        "pixel_id": str(pixel_id),
        "optimization_event": optimization_event,
        "optimization_goal": optimization_goal,
        "billing_event": billing_event,
        "bid_type": "BID_TYPE_CUSTOM",
        "conversion_bid_price": float(bid_price),
        "budget_mode": budget_mode,
        "budget": float(budget),
        "schedule_type": "SCHEDULE_FROM_NOW",
        "schedule_start_time": schedule_start_time,
        "operation_status": operation_status,
    }
    data = await _post("/adgroup/create/", body, access_token=access_token)
    aid = str(data.get("adgroup_id") or "")
    if not aid:
        raise TikTokCreativeError(f"adgroup/create returned no adgroup_id: {data!r}")
    log.info("tiktok create_adgroup ok %s → %s", adgroup_name, aid)
    return aid


# ---------- ad ---------------------------------------------------------------

async def create_ad(
    advertiser_id: str,
    adgroup_id: str,
    *,
    ad_name: str,
    identity_id: str,
    identity_type: str,
    video_id: str,
    image_id: str,
    landing_url: str,
    ad_text: str,
    display_name: str,
    call_to_action: str = "LEARN_MORE",
    ad_format: str = "SINGLE_VIDEO",
    operation_status: str = "DISABLE",
    access_token: str | None = None,
) -> str:
    text = (ad_text or "").strip()
    if len(text) > AD_TEXT_MAX:
        log.warning("ad_text length %d exceeds TikTok cap %d; truncating",
                    len(text), AD_TEXT_MAX)
        text = text[: AD_TEXT_MAX - 1] + "…"
    if not image_id:
        raise TikTokCreativeError("SINGLE_VIDEO ad requires a cover image_id")
    body = {
        "advertiser_id": str(advertiser_id),
        "adgroup_id": str(adgroup_id),
        "creatives": [
            {
                "ad_name": ad_name[:512],
                "ad_format": ad_format,
                "identity_id": identity_id,
                "identity_type": identity_type,
                "video_id": video_id,
                "image_ids": [image_id],
                "ad_text": text,
                "call_to_action": call_to_action,
                "display_name": display_name[:40],
                "landing_page_url": landing_url,
                "operation_status": operation_status,
            }
        ],
    }
    data = await _post("/ad/create/", body, access_token=access_token)
    ids = data.get("ad_ids") or []
    if not ids:
        # Some TT responses: data.creatives[0].ad_id
        creatives = data.get("creatives") or []
        if creatives and creatives[0].get("ad_id"):
            return str(creatives[0]["ad_id"])
        raise TikTokCreativeError(f"ad/create returned no ad_ids: {data!r}")
    ad_id = str(ids[0])
    log.info("tiktok create_ad ok %s → %s", ad_name, ad_id)
    return ad_id


# ---------- status flips (for post-review enable) ----------------------------

async def update_adgroup_status(
    advertiser_id: str,
    adgroup_id: str,
    *,
    operation_status: str,
    access_token: str | None = None,
) -> dict[str, Any]:
    return await _post(
        "/adgroup/status/update/",
        {
            "advertiser_id": str(advertiser_id),
            "adgroup_ids": [str(adgroup_id)],
            "operation_status": operation_status,
        },
        access_token=access_token,
    )


async def update_ad_status(
    advertiser_id: str,
    ad_id: str,
    *,
    operation_status: str,
    access_token: str | None = None,
) -> dict[str, Any]:
    return await _post(
        "/ad/status/update/",
        {
            "advertiser_id": str(advertiser_id),
            "ad_ids": [str(ad_id)],
            "operation_status": operation_status,
        },
        access_token=access_token,
    )
