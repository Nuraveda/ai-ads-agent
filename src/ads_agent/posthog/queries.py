"""HogQL query helpers against PostHog Cloud.

Project: 384306. Uses the personal API token (phx_*) — for read queries, not capture.
The phx key is NOT in .env for safety; it's the user's personal account-scoped key,
supplied via POSTHOG_PERSONAL_API_KEY env var at runtime.

If not set, queries fall back to the project API key (phc_*) which works for
events-ingest but NOT for the /api/projects/{id}/query endpoint.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import httpx

from ads_agent.config import settings

POSTHOG_PROJECT_ID = 384306


def _auth_header() -> dict[str, str]:
    # For HogQL /query endpoint PostHog needs a PERSONAL API key (phx_).
    # We stash it in POSTHOG_PERSONAL_API_KEY env var. Falls back to POSTHOG_API_KEY
    # (phc_) which will 401 on read queries — tell the user if so.
    key = os.environ.get("POSTHOG_PERSONAL_API_KEY") or settings().posthog_api_key
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


async def hogql(query: str) -> list[list]:
    """Run a HogQL query, return list of rows."""
    url = f"{settings().posthog_host}/api/projects/{POSTHOG_PROJECT_ID}/query/"
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(url, headers=_auth_header(), json={"query": {"kind": "HogQLQuery", "query": query}})
    r.raise_for_status()
    return r.json().get("results", [])


@dataclass
class StoreInsights:
    store_slug: str
    days: int
    unique_orders: int
    paid_orders: int
    paid_revenue: float
    pending_orders: int
    pending_revenue: float
    cancelled_orders: int
    refunded_orders: int
    email_coverage_pct: float
    utm_coverage_pct: float          # coverage over orders that CAN carry UTMs
    utm_coverage_raw_pct: float      # coverage over all orders (includes in-app)
    in_app_checkout_orders: int      # orders where UTMs are physically impossible
    top_utm_source: str | None

    @property
    def pipeline_orders(self) -> int:
        """paid + pending = real-world sold-units count.

        Used when Shopify `financial_status=paid` is artificially low because
        the courier/delivery-partner integration that promotes COD orders to
        'paid' is broken or delayed. A small double-count is possible for
        orders that progressed from pending to paid inside the window, but
        it's negligible at reporting resolution.
        """
        return int(self.paid_orders) + int(self.pending_orders)

    @property
    def pipeline_revenue(self) -> float:
        return float(self.paid_revenue) + float(self.pending_revenue)


async def store_insights(store_slug: str, days: int = 7) -> StoreInsights:
    """Roll-up per store over the last N days.

    Dedup pattern: we may have multiple events per order_id (backfill ran twice,
    webhook fires live). We collapse to ONE row per (order_id, event) using
    argMin on timestamp — i.e. take the ORIGINAL event, which carries the real
    Shopify createdAt (subsequent backfills capture at now-time). Then filter
    that collapsed row set by real createdAt window.
    """
    # In-app-checkout detection:
    # Shopify sets `source_name` to the channel that created the order. UTM tags
    # are only possible when the customer landed on the storefront from a URL
    # carrying query params — which excludes these order sources:
    #   - Numeric Meta app IDs (Facebook/Instagram Shop checkout, e.g. '311856791553')
    #   - 'subscription_contract_*' (recurring sub orders, no new click path)
    #   - 'shop' / 'shop_app' (Shopify Shop app native checkout)
    #   - 'pos' (in-store point-of-sale)
    # Counting these against the UTM-coverage denominator produces misleadingly
    # low coverage % for in-app-heavy brands. We compute two numbers: a
    # "raw" coverage over everything, and an "effective" coverage restricted
    # to orders that CAN carry UTMs.
    q = f"""
    WITH original AS (
      SELECT
        properties.order_id                        AS order_id,
        event                                      AS event,
        min(timestamp)                             AS real_ts,
        argMin(properties.value, timestamp)        AS value,
        argMin(properties.email, timestamp)        AS email,
        argMin(properties.utm_source, timestamp)   AS utm_source,
        argMin(properties.source_name, timestamp)  AS source_name
      FROM events
      WHERE event LIKE 'order_%'
        AND properties.store_slug = '{store_slug}'
        AND notEmpty(properties.order_id)
      GROUP BY order_id, event
    ),
    tagged AS (
      SELECT
        order_id, event, real_ts, value, email, utm_source, source_name,
        (match(toString(source_name), '^[0-9]+$')
          OR startsWith(toString(source_name), 'subscription_contract')
          OR toString(source_name) IN ('shop', 'shop_app', 'pos'))      AS is_in_app
      FROM original
      WHERE real_ts > now() - INTERVAL {days} DAY
    )
    SELECT
      uniq(order_id)                                                          AS unique_orders,
      uniqIf(order_id, event = 'order_paid')                                  AS paid_orders,
      coalesce(sumIf(toFloat(value), event = 'order_paid'), 0)                AS paid_revenue,
      uniqIf(order_id, event = 'order_pending')                               AS pending_orders,
      coalesce(sumIf(toFloat(value), event = 'order_pending'), 0)             AS pending_revenue,
      uniqIf(order_id, event = 'order_cancelled')                             AS cancelled_orders,
      uniqIf(order_id, event IN ('order_refunded','order_partially_refunded','refund_created')) AS refunded_orders,
      countIf(notEmpty(email)) / greatest(count(), 1)                         AS email_cov,
      -- Raw: across ALL orders (legacy number, can look misleadingly low)
      countIf(notEmpty(utm_source)) / greatest(count(), 1)                    AS utm_cov_raw,
      -- Effective: only across orders that COULD carry UTMs (excludes in-app)
      countIf(notEmpty(utm_source) AND NOT is_in_app)
        / greatest(countIf(NOT is_in_app), 1)                                 AS utm_cov_effective,
      uniqIf(order_id, is_in_app)                                             AS in_app_orders
    FROM tagged
    """
    rows = await hogql(q)
    if not rows:
        return StoreInsights(store_slug, days, 0, 0, 0, 0, 0, 0, 0, 0.0, 0.0, 0.0, 0, None)
    u, p, rev, pend, pend_rev, canc, ref, em_cov, ut_cov_raw, ut_cov_eff, in_app = rows[0]

    # Top UTM source (same dedup pattern)
    q_utm = f"""
    WITH original AS (
      SELECT
        properties.order_id                      AS order_id,
        min(timestamp)                           AS real_ts,
        argMin(properties.utm_source, timestamp) AS utm_source
      FROM events
      WHERE event LIKE 'order_%'
        AND properties.store_slug = '{store_slug}'
        AND notEmpty(properties.order_id)
      GROUP BY order_id
    )
    SELECT utm_source, count()
    FROM original
    WHERE real_ts > now() - INTERVAL {days} DAY
      AND notEmpty(utm_source)
    GROUP BY utm_source ORDER BY count() DESC LIMIT 1
    """
    utm_rows = await hogql(q_utm)
    top_utm = utm_rows[0][0] if utm_rows else None

    return StoreInsights(
        store_slug=store_slug,
        days=days,
        unique_orders=int(u or 0),
        paid_orders=int(p or 0),
        paid_revenue=float(rev or 0),
        pending_orders=int(pend or 0),
        pending_revenue=float(pend_rev or 0),
        cancelled_orders=int(canc or 0),
        refunded_orders=int(ref or 0),
        email_coverage_pct=round((em_cov or 0) * 100, 1),
        utm_coverage_pct=round((ut_cov_eff or 0) * 100, 1),
        utm_coverage_raw_pct=round((ut_cov_raw or 0) * 100, 1),
        in_app_checkout_orders=int(in_app or 0),
        top_utm_source=top_utm,
    )
