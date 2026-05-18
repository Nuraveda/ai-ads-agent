"""tiktok_campaign_budget: update one TikTok campaign budget."""
from __future__ import annotations

from ads_agent.agent.nodes.tiktok_common import load_tiktok_context
from ads_agent.tiktok.client import TikTokError, update_campaign_budget


async def tiktok_campaign_budget_node(state: dict) -> dict:
    slug = state['store_slug']
    campaign_id = str(state.get('campaign_id') or '').strip()
    try:
        budget = float(state.get('budget'))
    except (TypeError, ValueError):
        budget = 0.0
    if not campaign_id or budget <= 0:
        return {
            **state,
            'reply_text': 'usage: /tiktok_campaign_budget <store> <campaign_id> <budget>',
        }

    ctx, error = await load_tiktok_context(slug)
    if error:
        return {**state, 'reply_text': error}
    assert ctx is not None

    try:
        data = await update_campaign_budget(
            ctx.advertiser_id,
            campaign_id,
            budget,
            access_token=ctx.access_token,
        )
    except TikTokError as exc:
        return {
            **state,
            'reply_text': f"*{ctx.store.brand}* · TikTok campaign budget\n\nCould not update campaign `{campaign_id}`: {exc}",
        }

    lines = [
        f"*{ctx.store.brand}* · TikTok campaign budget",
        '',
        f"Advertiser ID `{ctx.advertiser_id}` · Campaign `{campaign_id}`",
        f"Requested budget: {budget:,.2f} · Auth `{ctx.auth_source}`",
    ]
    if data:
        lines.append(f"TikTok response: `{data}`")
    else:
        lines.append('TikTok accepted the budget update request.')
    return {**state, 'reply_text': '\n'.join(lines)}
