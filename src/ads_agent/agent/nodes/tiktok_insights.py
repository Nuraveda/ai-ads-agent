"""tiktok_insights: TikTok advertiser snapshot + paid media totals."""
from __future__ import annotations

from ads_agent.agent.nodes.tiktok_common import load_tiktok_context
from ads_agent.config import settings
from ads_agent.tiktok.client import TikTokError, advertiser_info, advertiser_spend


async def tiktok_insights_node(state: dict) -> dict:
    slug = state['store_slug']
    days = int(state.get('days', 7))
    ctx, error = await load_tiktok_context(slug)
    if error:
        return {**state, 'reply_text': error}
    assert ctx is not None

    try:
        info = await advertiser_info(ctx.advertiser_id, access_token=ctx.access_token)
        metrics = await advertiser_spend(ctx.advertiser_id, days=days, access_token=ctx.access_token)
    except TikTokError as exc:
        return {
            **state,
            'reply_text': (
                f"*{ctx.store.brand}* · TikTok\n\n"
                f"Could not query TikTok yet: {exc}\n"
                "Required now: either complete TikTok OAuth via "
                f"`/api/tiktok/consent-url?account_ref={slug}` or set "
                "`TIKTOK_ACCESS_TOKEN` manually."
            ),
        }

    name = info.get('name') or f"Advertiser {ctx.advertiser_id}"
    currency = info.get('currency') or ctx.store.currency
    status = info.get('status') or 'unknown'
    configured_env = settings().tiktok_env.strip() or 'sandbox'
    env_label = 'production' if ctx.access_token and slug != 'tiktok-sandbox' else configured_env

    lines = [
        f"*{ctx.store.brand}* · TikTok (last {days}d)",
        '',
        f"Advertiser: `{name}` · ID `{ctx.advertiser_id}`",
        f"Status: `{status}` · Country: `{ctx.country}` · Env: `{env_label}` · Auth: `{ctx.auth_source}`",
        (
            f"Spend: {metrics['spend']:,.2f} {currency} · "
            f"Impressions: {metrics['impressions']:,} · Clicks: {metrics['clicks']:,}"
        ),
        f"CTR: {metrics['ctr']:.2f}% · CPC: {metrics['cpc']:.2f} {currency}",
        (
            f"_Source: TikTok Business API via "
            f"`glitch-exec-labs/tiktok-business-api-sdk` · "
            f"{metrics['start_date']} → {metrics['end_date']}_"
        ),
    ]
    return {
        **state,
        'reply_text': '\n'.join(lines),
        'orders_summary': {
            'channel': 'tiktok',
            'advertiser_id': ctx.advertiser_id,
            'spend': metrics['spend'],
            'impressions': metrics['impressions'],
            'clicks': metrics['clicks'],
            'ctr': metrics['ctr'],
            'cpc': metrics['cpc'],
            'currency': currency,
            'auth_source': ctx.auth_source,
        },
    }
