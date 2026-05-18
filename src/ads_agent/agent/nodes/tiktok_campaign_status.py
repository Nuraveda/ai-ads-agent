"""tiktok_campaign_status: enable/disable one TikTok campaign."""
from __future__ import annotations

from ads_agent.agent.nodes.tiktok_common import load_tiktok_context
from ads_agent.tiktok.client import TikTokError, normalize_campaign_operation, update_campaign_status


async def tiktok_campaign_status_node(state: dict) -> dict:
    slug = state['store_slug']
    campaign_id = str(state.get('campaign_id') or '').strip()
    requested = str(state.get('campaign_status') or '').strip()
    if not campaign_id or not requested:
        return {
            **state,
            'reply_text': 'usage: /tiktok_campaign_status <store> <campaign_id> <enable|disable>',
        }

    ctx, error = await load_tiktok_context(slug)
    if error:
        return {**state, 'reply_text': error}
    assert ctx is not None

    try:
        operation = normalize_campaign_operation(requested)
        data = await update_campaign_status(
            ctx.advertiser_id,
            campaign_id,
            operation,
            access_token=ctx.access_token,
        )
    except TikTokError as exc:
        return {
            **state,
            'reply_text': f"*{ctx.store.brand}* · TikTok campaign status\n\nCould not update campaign `{campaign_id}`: {exc}",
        }

    lines = [
        f"*{ctx.store.brand}* · TikTok campaign status",
        '',
        f"Advertiser ID `{ctx.advertiser_id}` · Campaign `{campaign_id}`",
        f"Requested status: `{operation}` · Auth `{ctx.auth_source}`",
    ]
    if data:
        lines.append(f"TikTok response: `{data}`")
    else:
        lines.append('TikTok accepted the status update request.')
    return {**state, 'reply_text': '\n'.join(lines)}
