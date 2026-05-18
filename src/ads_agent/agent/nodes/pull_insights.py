"""pull_insights: store-level rollup over a lookback window.

Reads from PostHog (authoritative order data) — NOT from Shopify Admin, so we
don't hit rate limits and can answer fast. PostHog is already populated by
webhook-receiver in real time plus the 90-day backfill.
"""
from __future__ import annotations

from ads_agent.config import get_store
from ads_agent.posthog.queries import store_insights


async def pull_insights_node(state: dict) -> dict:
    slug = state["store_slug"]
    days = int(state.get("days", 7))
    store = get_store(slug)
    if store is None:
        return {**state, "reply_text": f"Unknown store: `{slug}`. Try /stores."}

    r = await store_insights(store.slug, days)

    if r.unique_orders == 0:
        reply = f"*{store.brand}* · last {days}d\nNo orders in this window."
    else:
        aov = (r.paid_revenue / r.paid_orders) if r.paid_orders else 0.0
        lines = [
            f"*{store.brand}* ({store.shop_domain}) · last {days}d",
            f"Orders: {r.unique_orders}  (paid {r.paid_orders}, pending {r.pending_orders}, cancelled {r.cancelled_orders}, refunded {r.refunded_orders})",
            f"Paid revenue: {r.paid_revenue:,.2f} {store.currency}",
            f"AOV (paid): {aov:,.2f} {store.currency}",
            f"Email coverage: {r.email_coverage_pct}%",
            f"UTM coverage: {r.utm_coverage_pct}%" + (f" (top: {r.top_utm_source})" if r.top_utm_source else ""),
        ]
        reply = "\n".join(lines)

    return {**state, "orders_summary": r.__dict__, "reply_text": reply}
