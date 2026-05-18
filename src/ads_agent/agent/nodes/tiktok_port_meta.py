"""Agent nodes for the Meta→TikTok port workflow.

Two nodes:

  - `tiktok_port_meta_node`: build a DISABLED TikTok launch from a Meta ad.
    Triggers from `/port_meta_to_tiktok <meta_ad_id> <tiktok_slug>`.

  - `tiktok_enable_launch_node`: flip campaign+adgroup+ad to ENABLE from a
    previously-built manifest. Triggers from `/enable_tiktok_launch <manifest_id>`.

The port node always leaves everything DISABLED. The enable node is a
separate human-gated step. This mirrors the agent's general HITL stance
and prevents a runaway loop from spending money.
"""
from __future__ import annotations

import logging

from ads_agent.agent.workflows.port_meta_to_tiktok import (
    PortError,
    enable_launch,
    list_recent_manifests,
    port_meta_ad,
)

log = logging.getLogger(__name__)


async def tiktok_port_meta_node(state: dict) -> dict:
    meta_ad_id     = str(state.get("meta_ad_id") or "").strip()
    tiktok_slug    = str(state.get("tiktok_slug") or state.get("store_slug") or "").strip()
    landing_url    = str(state.get("landing_url") or "").strip()
    ad_text        = str(state.get("ad_text") or "").strip()
    display_name   = str(state.get("display_name") or "Brand").strip()
    daily_budget   = float(state.get("daily_budget") or 50)
    bid_price      = float(state.get("bid_price") or 10)
    cta            = str(state.get("call_to_action") or "LEARN_MORE").strip()

    if not (meta_ad_id and tiktok_slug and landing_url and ad_text):
        return {
            **state,
            "reply_text": (
                "Usage:\n"
                "`/port_meta_to_tiktok <meta_ad_id> <tiktok_slug>`\n\n"
                "plus required args passed through state:\n"
                "• `landing_url` — product page\n"
                "• `ad_text` — ≤100 chars TikTok caption\n"
                "• `display_name` — ≤40 chars profile display\n"
                "• `daily_budget` (default 50), `bid_price` (default 10), "
                "`call_to_action` (default LEARN_MORE)"
            ),
        }

    try:
        m = await port_meta_ad(
            meta_ad_id=meta_ad_id, tiktok_slug=tiktok_slug,
            landing_url=landing_url, ad_text=ad_text, display_name=display_name,
            daily_budget=daily_budget, bid_price=bid_price,
            call_to_action=cta,
        )
    except PortError as e:
        return {**state, "reply_text": f"❌ port failed: {e}"}
    except Exception as e:  # noqa: BLE001
        log.exception("tiktok_port_meta: unexpected error")
        return {**state, "reply_text": f"❌ port failed (unexpected): {e}"}

    reply = [
        f"✅ *TikTok launch built (DISABLED)* for `{m.tiktok_slug}`",
        "",
        f"• campaign: `{m.campaign_id}`",
        f"• adgroup:  `{m.adgroup_id}` · event `{m.chose_event}`",
        f"• ad:       `{m.ad_id}`",
        f"• video:    `{m.video_id}`",
        f"• cover:    `{m.image_id}`",
        f"• budget:   {m.daily_budget:.0f}/day {m.currency or ''}",
        "",
        f"manifest: `{m.manifest_id}`",
        f"to go live: `/enable_tiktok_launch {m.manifest_id}`",
        "",
        f"source Meta ad: `{m.meta_ad_id}` ({m.source_meta_ad_name})",
        f"available pixel events: {', '.join(m.available_events)}",
    ]
    return {**state, "reply_text": "\n".join(reply)}


async def tiktok_enable_launch_node(state: dict) -> dict:
    mid = str(state.get("manifest_id") or "").strip()
    if not mid:
        recent = list_recent_manifests(10)
        if not recent:
            return {**state, "reply_text":
                "usage: `/enable_tiktok_launch <manifest_id>`\n\n"
                "No recent manifests on disk."}
        lines = ["usage: `/enable_tiktok_launch <manifest_id>`", "", "Recent:"]
        for r in recent:
            lines.append(
                f"• `{r['manifest_id']}` · {r.get('tiktok_slug','?')} · "
                f"{r.get('status','?')} · {r.get('created_at','?')}"
            )
        return {**state, "reply_text": "\n".join(lines)}

    try:
        m = await enable_launch(mid)
    except PortError as e:
        return {**state, "reply_text": f"❌ enable failed: {e}"}
    except Exception as e:  # noqa: BLE001
        log.exception("tiktok_enable_launch: unexpected error")
        return {**state, "reply_text": f"❌ enable failed (unexpected): {e}"}

    return {**state, "reply_text":
        f"🟢 launch `{m.manifest_id}` enabled\n"
        f"• campaign `{m.campaign_id}` · adgroup `{m.adgroup_id}` · ad `{m.ad_id}`\n"
        f"advertiser `{m.advertiser_id}` · slug `{m.tiktok_slug}`\n"
        f"TikTok will still run audit/review before serving."}
