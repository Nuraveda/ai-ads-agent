from __future__ import annotations

import json
from argparse import Namespace
from datetime import datetime, timezone
from typing import Any

import asyncpg

from ads_agent.actions.approval_targets import proposal_target
from ads_agent.actions.discord_notifier import post_proposal_to_discord
from ads_agent.agent.nodes.tiktok_common import load_tiktok_context
from ads_agent.config import STORE_AD_ACCOUNTS, get_store, settings
from ads_agent.discord.dispatcher import build_tiktok_port_card
from ads_agent.meta.graph_client import _get, ads_for_account, creative_details


def _days_live(created_time: str) -> int:
    if not created_time:
        return 0
    try:
        created = datetime.fromisoformat(created_time.replace("Z", "+00:00"))
    except ValueError:
        return 0
    return max(0, int((datetime.now(timezone.utc) - created).total_seconds() // 86400))


def _first_text(*values: Any) -> str:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _creative_landing_url(creative: dict[str, Any]) -> str:
    spec = creative.get("object_story_spec") or {}
    link_data = spec.get("link_data") or {}
    video_data = spec.get("video_data") or {}
    cta_value = (video_data.get("call_to_action") or {}).get("value") or {}
    return _first_text(
        link_data.get("link"),
        cta_value.get("link"),
        creative.get("object_url"),
    )


async def _select_only_active_ad(slug: str) -> str:
    account_ids = STORE_AD_ACCOUNTS.get(slug) or []
    if not account_ids:
        store = get_store(slug)
        if store and store.meta_ad_account:
            account_ids = [store.meta_ad_account]
    active: list[dict[str, Any]] = []
    for account_id in account_ids:
        for ad in await ads_for_account(account_id, days=7, limit=200):
            if (ad.get("effective_status") or "").upper() == "ACTIVE":
                active.append(ad)
    if len(active) != 1:
        raise RuntimeError(
            f"expected exactly one active Meta ad for `{slug}`, found {len(active)}; "
            "pass --meta-ad-id for a deterministic demo"
        )
    return str(active[0]["ad_id"])


async def _meta_demo_context(meta_ad_id: str) -> dict[str, Any]:
    detail = await creative_details(meta_ad_id, days=7)
    node = await _get(
        meta_ad_id,
        {
            "fields": (
                "id,name,status,effective_status,created_time,campaign{name},"
                "creative{id,thumbnail_url,body,title,video_id,object_url,"
                "object_story_spec}"
            )
        },
    )
    creative = node.get("creative") or detail.get("creative") or {}
    spec = creative.get("object_story_spec") or {}
    video_data = spec.get("video_data") or {}
    link_data = spec.get("link_data") or {}
    ad_text = _first_text(
        video_data.get("message"),
        link_data.get("message"),
        creative.get("body"),
        creative.get("title"),
        detail.get("ad_name"),
    )
    return {
        "meta_ad_id": meta_ad_id,
        "ad_name": node.get("name") or detail.get("ad_name") or meta_ad_id,
        "campaign_name": (node.get("campaign") or {}).get("name") or "",
        "days_live": _days_live(node.get("created_time") or ""),
        "impressions": int(detail.get("impressions") or 0),
        "landing_url": _creative_landing_url(creative),
        "ad_text": ad_text[:100],
        "created_time": node.get("created_time") or "",
    }


async def run_port_meta_to_tiktok(args: Namespace) -> None:
    slug = str(args.slug).strip()
    meta_ad_id = str(args.meta_ad_id or "").strip() or await _select_only_active_ad(slug)

    ctx, err = await load_tiktok_context(slug)
    if err or ctx is None:
        raise RuntimeError(err or f"no TikTok context for `{slug}`")
    meta = await _meta_demo_context(meta_ad_id)

    if args.print_context_only:
        print(
            json.dumps(
                {
                    "slug": slug,
                    "store": ctx.store.brand,
                    "meta_ad_id": meta_ad_id,
                    "meta_ad_name": meta["ad_name"],
                    "tiktok_advertiser_id": ctx.advertiser_id,
                    "identity_id_present": bool(ctx.identity_id),
                    "pixel_id_present": bool(ctx.pixel_id),
                    "default_location_ids": list(ctx.default_location_ids),
                    "landing_url_present": bool(meta["landing_url"]),
                    "ad_text_present": bool(meta["ad_text"]),
                },
                indent=2,
            )
        )
        return

    target = proposal_target(slug)
    if target is None or target.discord_channel_id is None:
        raise RuntimeError(f"no Discord proposal target configured for `{slug}`")
    if not meta["landing_url"]:
        raise RuntimeError(f"Meta ad {meta_ad_id} has no landing URL in creative")
    if not meta["ad_text"]:
        raise RuntimeError(f"Meta ad {meta_ad_id} has no usable ad text in creative")

    params = {
        "meta_ad_id": meta_ad_id,
        "tiktok_slug": slug,
        "landing_url": meta["landing_url"],
        "ad_text": meta["ad_text"],
        "display_name": ctx.store.brand[:40],
        "daily_budget": float(args.daily_budget),
        "bid_price": float(args.bid_price),
        "call_to_action": str(args.cta or "LEARN_MORE").upper(),
        "tiktok_advertiser_id": ctx.advertiser_id,
        "identity_id": ctx.identity_id,
        "identity_type": ctx.identity_type,
        "pixel_id": ctx.pixel_id,
        "currency": ctx.currency,
        "default_location_ids": list(ctx.default_location_ids),
    }
    evidence = {
        "days_live": meta["days_live"],
        "impressions": meta["impressions"],
        "campaign_name": meta["campaign_name"],
        "creative_type": "video ad",
    }

    pool = await asyncpg.create_pool(settings().postgres_rw_dsn, min_size=1, max_size=2)
    try:
        async with pool.acquire() as conn:
            action_id = await conn.fetchval(
                """INSERT INTO ads_agent.agent_actions (
                       store_slug, action_kind, target_object_id, target_object_name,
                       params, rationale, evidence, expected_impact,
                       discord_channel_id, approval_platform
                   ) VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7::jsonb,$8::jsonb,$9,$10)
                   RETURNING id""",
                slug,
                "tiktok_port",
                meta_ad_id,
                meta["ad_name"],
                json.dumps(params),
                "Mirror this live Meta video ad onto TikTok after Discord approval.",
                json.dumps(evidence),
                json.dumps({}),
                target.discord_channel_id,
                "discord",
            )

        card = build_tiktok_port_card(
            meta_ad_id=meta_ad_id,
            ad_name=meta["ad_name"],
            days_live=meta["days_live"],
            impressions=meta["impressions"],
            slug=slug,
            budget=float(args.daily_budget),
            advertiser_id=ctx.advertiser_id,
            action_id=action_id,
        )
        try:
            message_id = await post_proposal_to_discord(target.discord_channel_id, action_id, card)
        except Exception as e:
            async with pool.acquire() as conn:
                await conn.execute(
                    """UPDATE ads_agent.agent_actions
                       SET status='failed',
                           result=$1::jsonb
                       WHERE id=$2 AND status='pending_approval'""",
                    json.dumps({"notify_error": str(e)[:500]}),
                    action_id,
                )
            raise
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE ads_agent.agent_actions
                   SET discord_message_id=$1
                   WHERE id=$2""",
                message_id,
                action_id,
            )
    finally:
        await pool.close()

    safe_name = str(meta["ad_name"]).replace('"', '\\"')
    print(
        f'queued: action_id={action_id} meta_ad_id={meta_ad_id} '
        f'ad_name="{safe_name}" awaiting_approval=true'
    )
