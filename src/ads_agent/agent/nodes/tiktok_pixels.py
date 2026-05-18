"""tiktok_pixels: list TikTok pixels for one advertiser."""
from __future__ import annotations

from ads_agent.agent.nodes.tiktok_common import load_tiktok_context
from ads_agent.tiktok.client import TikTokError, list_pixels


async def tiktok_pixels_node(state: dict) -> dict:
    slug = state['store_slug']
    limit = int(state.get('limit', 10))
    ctx, error = await load_tiktok_context(slug)
    if error:
        return {**state, 'reply_text': error}
    assert ctx is not None

    try:
        result = await list_pixels(ctx.advertiser_id, limit=limit, access_token=ctx.access_token)
    except TikTokError as exc:
        return {
            **state,
            'reply_text': f"*{ctx.store.brand}* · TikTok pixels\n\nCould not list pixels: {exc}",
        }

    pixels = result.get('pixels', [])
    page_info = result.get('page_info', {})
    total_number = int(page_info.get('total_number') or len(pixels) or 0)
    if not pixels:
        return {
            **state,
            'reply_text': (
                f"*{ctx.store.brand}* · TikTok pixels\n\n"
                f"Advertiser ID `{ctx.advertiser_id}` · Auth `{ctx.auth_source}`\n"
                'No TikTok pixels found for this advertiser.'
            ),
        }

    lines = [
        f"*{ctx.store.brand}* · TikTok pixels",
        '',
        f"Advertiser ID `{ctx.advertiser_id}` · Auth `{ctx.auth_source}` · Showing {len(pixels)} of {total_number}",
        '',
    ]
    for row in pixels:
        event_count = len(row.get('events') or [])
        lines.append(f"• `{row['pixel_id']}` · {row['pixel_name'] or 'Unnamed pixel'}")
        lines.append(
            f"  code `{row['pixel_code'] or 'n/a'}` · status `{row['activity_status'] or 'unknown'}`"
        )
        lines.append(
            f"  partner `{row['partner_name'] or 'n/a'}` · setup `{row['pixel_setup_mode'] or 'n/a'}` · events {event_count}"
        )
    return {**state, 'reply_text': '\n'.join(lines)}
