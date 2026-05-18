"""attribution: Meta → Amazon attribution report — dual-method.

Two ROAS numbers are computed side-by-side:

1. Subtraction-model ROAS (ads_agent.amazon_attribution_daily_v):
     meta_attributed_orders = total_amz_orders − amz_sp_orders (per advertised ASIN)
     meta_attributed_roas   = attributed_gross / meta_spend
   Upper bound — credits organic baseline to Meta.

2. Sessions-delta ROAS (ads_agent.amazon_traffic_daily_v):
     baseline_per_day = median(orders, gross) on zero-Meta-spend days in window
     incremental_orders = sum(orders on spend-days) − count(spend-days) × baseline
     true_roas = incremental_gross / total_meta_spend
   Incremental truth — uses Amazon's own Business Reports sessions data as a
   natural-experiment signal.

When the two methods diverge > 2×, the node flags which to trust:
  - For small / thin-baseline stores (< 3 zero-spend days), subtraction is the
    only usable number.
  - For stores with > 3 zero-spend days and > 3 spend-days in the window, the
    sessions-delta is the ground-truth call.

Both numbers printed with methodology block so the recipient understands.
"""
from __future__ import annotations

import logging
import statistics

import asyncpg

from ads_agent.config import get_store, settings

log = logging.getLogger(__name__)

# Currency normalization — same rate used in amazon_attribution_daily_v.
AED_TO_INR = 22.7


def _sessions_delta_roas(rows: list[dict]) -> dict:
    """Compute incremental ROAS via zero-spend-baseline subtraction.

    Input: list of daily rows with fields
        date, meta_spend_inr, meta_clicks, sessions, units, gross_inr
    Output: dict with incremental metrics + flags.

    Uses median (not mean) for baseline to be robust against one-off spike days.
    """
    zero_days = [r for r in rows if float(r.get("meta_spend_inr") or 0) == 0]
    spend_days = [r for r in rows if float(r.get("meta_spend_inr") or 0) > 0]

    if len(zero_days) < 3 or len(spend_days) < 3:
        return {
            "usable": False,
            "reason": (
                f"need ≥3 zero-spend and ≥3 spend days; got "
                f"{len(zero_days)}z / {len(spend_days)}s"
            ),
        }

    # Coerce all numerics to float up-front — asyncpg returns Decimal for NUMERIC
    # columns, which mixes badly with float in arithmetic downstream.
    baseline_orders_per_day   = float(statistics.median(float(r["units"]    or 0) for r in zero_days))
    baseline_gross_per_day    = float(statistics.median(float(r["gross_inr"] or 0) for r in zero_days))
    baseline_sessions_per_day = float(statistics.median(float(r["sessions"] or 0) for r in zero_days))

    n_spend = len(spend_days)
    total_spend    = sum(float(r["meta_spend_inr"] or 0) for r in spend_days)
    total_clicks   = sum(int(r["meta_clicks"]      or 0) for r in spend_days)
    total_sessions = sum(int(r["sessions"]         or 0) for r in spend_days)
    total_units    = sum(int(r["units"]            or 0) for r in spend_days)
    total_gross    = sum(float(r["gross_inr"]      or 0) for r in spend_days)

    expected_baseline_orders = n_spend * baseline_orders_per_day
    expected_baseline_gross  = n_spend * baseline_gross_per_day
    expected_baseline_sessions = n_spend * baseline_sessions_per_day

    incremental_orders   = max(0, total_units    - expected_baseline_orders)
    incremental_gross    = max(0, total_gross    - expected_baseline_gross)
    incremental_sessions = max(0, total_sessions - expected_baseline_sessions)

    roas = (incremental_gross / total_spend) if total_spend > 0 else 0
    cpo = (total_spend / incremental_orders) if incremental_orders > 0 else None

    return {
        "usable": True,
        "baseline_orders_per_day": baseline_orders_per_day,
        "baseline_gross_per_day":  baseline_gross_per_day,
        "baseline_sessions_per_day": baseline_sessions_per_day,
        "n_zero_days":  len(zero_days),
        "n_spend_days": n_spend,
        "spend_days_total_spend":    total_spend,
        "spend_days_total_clicks":   total_clicks,
        "spend_days_total_sessions": total_sessions,
        "spend_days_total_units":    total_units,
        "spend_days_total_gross":    total_gross,
        "incremental_orders":   incremental_orders,
        "incremental_gross":    incremental_gross,
        "incremental_sessions": incremental_sessions,
        "roas": roas,
        "cpo":  cpo,
        "click_to_session_ratio": (total_clicks / total_sessions) if total_sessions > 0 else None,
    }


async def attribution_node(state: dict) -> dict:
    slug = state["store_slug"]
    days = int(state.get("days", 30))
    store = get_store(slug)
    if store is None:
        return {**state, "reply_text": f"Unknown store: `{slug}`"}

    # Airbyte pipeline torn down 2026-05-08. The dual-method attribution model
    # (subtraction + sessions-delta) depended on amazon_attribution_daily_v
    # and amazon_traffic_daily_v which are gone. Re-architect off MAP +
    # PostHog before re-enabling.
    return {
        **state,
        "reply_text": (
            f"*{store.brand}* · attribution\n\n"
            f"Meta→Amazon attribution is **temporarily offline**. The Airbyte "
            f"pipeline that fed `amazon_attribution_daily_v` was torn down on "
            f"2026-05-08. Cross-channel attribution will be rebuilt on top of "
            f"MAP (Amazon Ads side) + PostHog (Shopify side).\n\n"
            f"For Amazon Ads totals use `/amazon {slug} {days}`."
        ),
    }
    # Unreachable — kept below for the eventual MAP+PostHog reimplementation.
    conn = await asyncpg.connect(settings().postgres_insights_ro_url)
    try:
        # Store-level totals
        agg = await conn.fetchrow(
            """SELECT
                   SUM(amz_orders)                  AS amz_orders,
                   SUM(amz_gross)                   AS amz_gross,
                   SUM(sp_orders)                   AS sp_orders,
                   SUM(sp_sales1d)                  AS sp_sales,
                   SUM(sp_cost)                     AS sp_cost,
                   SUM(meta_spend)                  AS meta_spend,
                   SUM(meta_clicks)                 AS meta_clicks,
                   SUM(meta_attributed_orders)      AS meta_orders,
                   SUM(meta_attributed_gross)       AS meta_gross_native,
                   SUM(meta_attributed_gross_inr)   AS meta_gross_inr,
                   MAX(currency)                    AS currency
               FROM ads_agent.amazon_attribution_daily_v
               WHERE store_slug = $1
                 AND date > (CURRENT_DATE - ($2::int * INTERVAL '1 day'))""",
            slug, days,
        )

        # Shopify-side Meta from meta_ads_daily (destination matches this store's
        # custom domain). Resolved via STORE_BRAND_REGISTRY_JSON.
        # brand_registry stripped from open-source release; provide your own
        shopify_domain = shop_host_for(slug) or None

        shopify_row = None
        if shopify_domain:
            shopify_row = await conn.fetchrow(
                """SELECT
                       SUM(spend)          AS meta_spend,
                       SUM(clicks)         AS meta_clicks,
                       SUM(purchases)      AS meta_purchases,
                       SUM(purchase_value) AS meta_purchase_value
                   FROM ads_agent.meta_ads_daily
                   WHERE date > (CURRENT_DATE - ($1::int * INTERVAL '1 day'))
                     AND destination_url ILIKE '%' || $2 || '%'""",
                days, shopify_domain,
            )

        # Top-5 ASINs by Meta-to-Amazon spend
        top_asins = await conn.fetch(
            """SELECT
                   asin,
                   SUM(amz_orders)               AS amz_orders,
                   SUM(amz_gross)                AS amz_gross,
                   SUM(meta_spend)               AS meta_spend,
                   SUM(meta_clicks)              AS meta_clicks,
                   SUM(meta_attributed_orders)   AS meta_orders,
                   SUM(meta_attributed_gross)    AS meta_gross,
                   SUM(meta_attributed_gross_inr) AS meta_gross_inr
               FROM ads_agent.amazon_attribution_daily_v
               WHERE store_slug = $1
                 AND date > (CURRENT_DATE - ($2::int * INTERVAL '1 day'))
                 AND asin IS NOT NULL
                 AND meta_spend > 0
               GROUP BY asin
               ORDER BY meta_spend DESC
               LIMIT 5""",
            slug, days,
        )

        # Daily time series joining Amazon traffic (sessions, units, gross) with
        # Meta-to-Amazon spend per day. Drives the sessions-delta computation.
        # brand_registry stripped from open-source release; provide your own
        amz_host = amazon_marketplace_for(slug) or 'amazon.com'
        # Reporting currency normalization: native → INR (legacy reporting unit
        # for portfolio rollup). Add per-currency rates to FX_TO_INR as needed.
        FX_TO_INR = {"INR": 1.0, "AED": AED_TO_INR}
        fx = FX_TO_INR.get(currency_for(slug, "USD"), 1.0)
        daily_series = await conn.fetch(
            """WITH amz AS (
                   SELECT date, sessions, units_ordered, gross
                   FROM ads_agent.amazon_traffic_daily_v
                   WHERE store_slug = $1
                     AND date > (CURRENT_DATE - ($2::int * INTERVAL '1 day'))
               ),
               meta AS (
                   SELECT date,
                          COALESCE(SUM(spend), 0)  AS spend,
                          COALESCE(SUM(clicks), 0) AS clicks
                   FROM ads_agent.meta_ads_daily
                   WHERE date > (CURRENT_DATE - ($2::int * INTERVAL '1 day'))
                     AND destination_url ~* $3
                   GROUP BY 1
               )
               SELECT
                   a.date,
                   a.sessions                               AS sessions,
                   a.units_ordered                          AS units,
                   (a.gross * $4::numeric)                  AS gross_inr,
                   COALESCE(m.spend, 0)                     AS meta_spend_inr,
                   COALESCE(m.clicks, 0)                    AS meta_clicks
               FROM amz a
               LEFT JOIN meta m USING (date)
               ORDER BY a.date""",
            slug, days, amz_host, fx,
        )
    except asyncpg.UndefinedTableError:
        return {**state, "reply_text": (
            f"*{store.brand}* · attribution\n\n"
            f"`amazon_attribution_daily_v` view missing. Apply "
            f"`ops/scripts/migrate_amazon_attribution_view.sql` and retry."
        )}
    finally:
        await conn.close()

    if not agg or not agg["amz_orders"]:
        return {**state, "reply_text": (
            f"*{store.brand}* · attribution (last {days}d)\n\n"
            f"No Amazon orders in the window. Either Airbyte hasn't synced, or "
            f"this store's Seller Central source isn't configured."
        )}

    ccy = agg["currency"] or currency_for(slug, "USD")
    amz_gross = float(agg["amz_gross"] or 0)
    sp_sales = float(agg["sp_sales"] or 0)
    sp_cost = float(agg["sp_cost"] or 0)
    meta_spend = float(agg["meta_spend"] or 0)
    meta_clicks = int(agg["meta_clicks"] or 0)
    meta_orders = int(agg["meta_orders"] or 0)
    meta_gross_native = float(agg["meta_gross_native"] or 0)
    meta_gross_inr = float(agg["meta_gross_inr"] or 0)

    roas_inr = (meta_gross_inr / meta_spend) if meta_spend > 0 else 0
    cpo_inr = (meta_spend / meta_orders) if meta_orders > 0 else 0

    lines: list[str] = []
    lines.append(f"*{store.brand}* · Meta→Amazon attribution (last {days}d)")
    lines.append("")

    # ── Amazon side ─────────────────────────────────────────────────────────
    lines.append("*Amazon Seller*")
    lines.append(f"  orders: {int(agg['amz_orders'])} · gross: {amz_gross:,.0f} {ccy}")
    lines.append(f"  SP Ads: {int(agg['sp_orders'])} orders · {sp_sales:,.0f} {ccy} sales · {sp_cost:,.0f} {ccy} spend")
    lines.append("")

    # ── Method 1: subtraction model (upper bound) ───────────────────────────
    lines.append("*Meta → Amazon — Method 1: Subtraction model (upper bound)*")
    lines.append(f"  spend: ₹{meta_spend:,.0f} · clicks: {meta_clicks:,}")
    lines.append(f"  attributed orders: {meta_orders} · gross: {meta_gross_native:,.0f} {ccy}"
                 + (f" (~₹{meta_gross_inr:,.0f})" if ccy != "INR" else ""))
    lines.append(f"  ROAS: {roas_inr:.2f}× · CPO: ₹{cpo_inr:,.0f}")
    lines.append("")

    # ── Method 2: sessions-delta (incremental truth) ────────────────────────
    daily_rows = [dict(r) for r in daily_series]
    sd = _sessions_delta_roas(daily_rows)
    lines.append("*Meta → Amazon — Method 2: Sessions-delta (incremental truth)*")
    if not sd.get("usable"):
        lines.append(f"  _insufficient data_: {sd['reason']}")
        lines.append("  falling back to Method 1 above.")
        roas_sd = None
    else:
        roas_sd = sd["roas"]
        lines.append(
            f"  baseline (zero-Meta, n={sd['n_zero_days']}d): "
            f"{sd['baseline_orders_per_day']:.1f} orders/d · "
            f"₹{sd['baseline_gross_per_day']:,.0f}/d · "
            f"{sd['baseline_sessions_per_day']:.0f} sessions/d"
        )
        lines.append(
            f"  spend-window (n={sd['n_spend_days']}d): "
            f"₹{sd['spend_days_total_spend']:,.0f} spend · "
            f"{sd['spend_days_total_clicks']:,} clicks · "
            f"{sd['spend_days_total_sessions']:,} Amz sessions"
        )
        if sd.get("click_to_session_ratio"):
            lines.append(
                f"  Meta click → Amz session ratio: "
                f"{sd['click_to_session_ratio']:.1f}× "
                f"(ideal ~1.0; high = click loss / bot)"
            )
        lines.append(
            f"  incremental orders: {sd['incremental_orders']:.1f} · "
            f"gross: ₹{sd['incremental_gross']:,.0f}"
        )
        cpo_str = f"₹{sd['cpo']:,.0f}" if sd.get("cpo") else "n/a"
        lines.append(f"  *ROAS: {sd['roas']:.2f}× · CPO: {cpo_str}*")
    lines.append("")

    # ── Divergence flag ─────────────────────────────────────────────────────
    if roas_sd is not None and roas_inr > 0:
        ratio = roas_inr / max(roas_sd, 0.01)
        if ratio > 2.0:
            lines.append(
                f"⚠ *Subtraction is {ratio:.1f}× higher than sessions-delta.* "
                f"Trust Method 2 — organic baseline is being mis-credited to Meta."
            )
            lines.append("")
        elif ratio < 0.5:
            lines.append(
                f"⚠ *Sessions-delta is {1/ratio:.1f}× higher than subtraction.* "
                f"Meta may be driving halo orders on non-advertised ASINs — investigate."
            )
            lines.append("")
        else:
            lines.append(
                f"_Methods agree within 2× (ratio {ratio:.2f}) — confident in ~{roas_sd:.1f}× true ROAS._"
            )
            lines.append("")

    # ── Shopify side (Meta-reported; not attribution-model) ─────────────────
    if shopify_row and shopify_row["meta_spend"] and float(shopify_row["meta_spend"]) > 0:
        sh_spend = float(shopify_row["meta_spend"] or 0)
        sh_purch = int(shopify_row["meta_purchases"] or 0)
        sh_pv    = float(shopify_row["meta_purchase_value"] or 0)
        sh_roas  = (sh_pv / sh_spend) if sh_spend > 0 else 0
        sh_cpo   = (sh_spend / sh_purch) if sh_purch > 0 else 0
        lines.append(f"*Meta → Shopify ({shopify_domain}, Meta-reported)*")
        lines.append(f"  spend: ₹{sh_spend:,.0f} · purchases: {sh_purch} · value: ₹{sh_pv:,.0f}")
        lines.append(f"  reported ROAS: {sh_roas:.2f}× · CPO: ₹{sh_cpo:,.0f}")
        lines.append(f"  _note: Meta-side conversions known to over-report on this account; "
                     f"/roas {slug} for PostHog-ground-truth cross-check_")
        lines.append("")

        # Channel comparison
        total_spend = meta_spend + sh_spend
        if total_spend > 0:
            amz_share = meta_spend / total_spend * 100
            lines.append("*Channel split*")
            lines.append(f"  Amazon: ₹{meta_spend:,.0f} ({amz_share:.1f}% of Meta budget) → {roas_inr:.1f}× ROAS")
            lines.append(f"  Shopify: ₹{sh_spend:,.0f} ({100-amz_share:.1f}% of Meta budget) → {sh_roas:.1f}× ROAS (reported)")
            if roas_inr > sh_roas * 1.5 and meta_spend < sh_spend:
                lines.append(f"  ⚡ Amazon ROAS appears {roas_inr/max(sh_roas,0.01):.1f}× higher — consider reallocation")
            lines.append("")

    # ── Top ASINs by Meta-to-Amazon spend ───────────────────────────────────
    if top_asins:
        lines.append("*Top ASINs by Meta-to-Amazon spend*")
        for r in top_asins:
            ms = float(r["meta_spend"] or 0)
            mg_inr = float(r["meta_gross_inr"] or 0)
            mg_native = float(r["meta_gross"] or 0)
            asin_roas = (mg_inr / ms) if ms > 0 else 0
            lines.append(
                f"• `{r['asin']}`: spend ₹{ms:,.0f} · {int(r['meta_clicks'] or 0):,} clicks · "
                f"{int(r['meta_orders'] or 0)} orders · {mg_native:,.0f} {ccy} gross · {asin_roas:.1f}× ROAS"
            )
        lines.append("")

    # ── Caveats (always printed; methodology transparency) ──────────────────
    lines.append(
        "_Method 1 (subtraction): orders on Meta-advertised ASINs minus SP-Ads orders. "
        "Upper-bound; credits organic baseline._\n"
        "_Method 2 (sessions-delta): median-of-zero-spend-days baseline subtracted from "
        "spend-day totals using Amazon's Business Reports. Incremental truth._\n"
        "_ROAS normalized to INR (AED→INR @ 22.7). Repeat-buyer effect uncontrolled (PII off in Airbyte)._"
    )

    return {**state, "reply_text": "\n".join(lines)}
