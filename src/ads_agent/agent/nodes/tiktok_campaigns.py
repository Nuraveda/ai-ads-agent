"""tiktok_campaigns: list TikTok campaigns for one advertiser."""
from __future__ import annotations

from ads_agent.agent.nodes.tiktok_common import load_tiktok_context
from ads_agent.tiktok.client import TikTokError, list_campaigns


async def tiktok_campaigns_node(state: dict) -> dict:
    slug = state['store_slug']
    limit = int(state.get('limit', 10))
    ctx, error = await load_tiktok_context(slug)
    if error:
        return {**state, 'reply_text': error}
    assert ctx is not None

    try:
        result = await list_campaigns(ctx.advertiser_id, limit=limit, access_token=ctx.access_token)
    except TikTokError as exc:
        return {
            **state,
            'reply_text': f"*{ctx.store.brand}* · TikTok campaigns\n\nCould not list campaigns: {exc}",
        }

    campaigns = result.get('campaigns', [])
    page_info = result.get('page_info', {})
    total_number = int(page_info.get('total_number') or len(campaigns) or 0)
    if not campaigns:
        return {
            **state,
            'reply_text': (
                f"*{ctx.store.brand}* · TikTok campaigns\n\n"
                f"Advertiser ID `{ctx.advertiser_id}` · Auth `{ctx.auth_source}`\n"
                'No campaigns found for this advertiser yet.'
            ),
        }

    lines = [
        f"*{ctx.store.brand}* · TikTok campaigns",
        '',
        f"Advertiser ID `{ctx.advertiser_id}` · Auth `{ctx.auth_source}` · Showing {len(campaigns)} of {total_number}",
        '',
    ]
    for row in campaigns:
        lines.append(f"• `{row['campaign_id']}` · {row['campaign_name'] or 'Unnamed campaign'}")
        lines.append(
            f"  status `{row['operation_status'] or 'unknown'}` · objective `{row['objective_type'] or 'n/a'}`"
        )
        if row.get('budget'):
            lines.append(f"  budget {row['budget']:,.2f} · mode `{row['budget_mode'] or 'n/a'}`")
        if row.get('secondary_status'):
            lines.append(f"  secondary `{row['secondary_status']}`")
    lines.append('')
    lines.append(
        '_Use `/tiktok_campaign_status <store> <campaign_id> <enable|disable>` or '
        '`/tiktok_campaign_budget <store> <campaign_id> <amount>` to manage one campaign._'
    )
    return {**state, 'reply_text': '\n'.join(lines)}
