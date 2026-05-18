"""roas_compute: true ROAS = Shopify paid revenue / Meta spend, vs Meta-reported ROAS.

Cross-currency aware: Shopify store currency and Meta ad account currency often
differ (e.g. Urban family sells in INR but runs ads in CAD). We convert Meta
spend into the Shopify store's currency using live FX before computing ROAS,
so the ratio is apples-to-apples.

Pulls:
  - Shopify paid revenue (from PostHog, last N days, in store.currency)
  - Meta spend + Meta-reported purchase_value (from Graph API, in each account's currency)
  - For stores with multiple ad accounts, sums converted spend across ALL linked accounts.

Store → ad-account multimap is loaded from STORE_AD_ACCOUNTS_JSON env var
via `ads_agent.config.STORE_AD_ACCOUNTS`. Never hard-code account IDs here.
"""
from __future__ import annotations

import json
import logging
import os

from ads_agent.config import STORE_AD_ACCOUNTS, get_store
from ads_agent.fx import convert
from ads_agent.ga4.client import ga4_metrics
from ads_agent.meta.destinations import classify_destination
from ads_agent.meta.graph_client import (
    MetaGraphError,
    account_spend,
    ad_destinations_for_account,
    ads_for_account_lean,
)
from ads_agent.posthog.queries import store_insights

log = logging.getLogger(__name__)


async def _meta_spend_by_destination(act: str, days: int) -> dict[str, float]:
    """Bucket an ad account's spend by destination class for the window.

    Returns: {'amazon': spend_native, 'shopify-<market>': spend_native, ...}
    Currency stays in the Meta account's native currency.
    """
    try:
        ads = await ads_for_account_lean(act, days=days, limit=500)
        dests = await ad_destinations_for_account(act, limit=500)
    except MetaGraphError as e:
        log.warning("destination split failed for %s: %s", act, e)
        return {}
    out: dict[str, float] = {}
    for ad in ads:
        ad_id = ad.get("ad_id") or ad.get("id")
        spend = float(ad.get("spend") or 0)
        if not ad_id or spend <= 0:
            continue
        url = dests.get(str(ad_id), "") or ""
        klass = classify_destination(url) if url else "unknown"
        out[klass] = out.get(klass, 0.0) + spend
    return out


def _map_account_for(slug: str) -> tuple[str, str] | None:
    raw = os.environ.get("STORE_MAP_ACCOUNTS_JSON", "").strip()
    if not raw:
        return None
    try:
        m = json.loads(raw)
    except json.JSONDecodeError:
        return None
    cfg = m.get(slug)
    if not (cfg and cfg.get("integration_id") and cfg.get("account_id")):
        return None
    return cfg["integration_id"], cfg["account_id"]


# Per-store CAC threshold (ad-account native currency, per pipeline order).
# "Pipeline order" = paid + pending orders (real-world conversions; `paid`
# alone is artificially low because the delivery partner's status-integration
# to Shopify is currently broken — see user note 2026-04-16).
#
# Urban family: ~$3 CAD CAC is within threshold (10 orders / $30 CAD spend).
CAC_THRESHOLDS: dict[str, tuple[float, str]] = {
    "urban": (3.0, "CAD"),
    "storico": (3.0, "CAD"),
    "classicoo": (3.0, "CAD"),
    "trendsetters": (3.0, "CAD"),
    # example and Mokshya thresholds TBD — fall back to no verdict
}


async def roas_compute_node(state: dict) -> dict:
    slug = state["store_slug"]
    days = int(state.get("days", 7))
    store = get_store(slug)
    if store is None:
        return {**state, "reply_text": f"Unknown store: `{slug}`"}

    shop_ccy = store.currency  # Shopify/native reporting currency
    shopify = await store_insights(store.slug, days)

    ad_accounts = STORE_AD_ACCOUNTS.get(store.slug, [])
    if not ad_accounts:
        return {**state, "reply_text": f"No Meta ad accounts mapped for `{slug}` yet."}

    # Aggregate in SHOP CURRENCY via FX, also keep Meta-native totals
    total_spend_shop = 0.0
    total_meta_value_shop = 0.0
    total_spend_native = 0.0
    total_meta_purchases = 0
    native_ccy: str | None = None
    account_lines: list[str] = []
    fx_notes: set[str] = set()

    for act in ad_accounts:
        try:
            d = await account_spend(act, days=days)
        except MetaGraphError as e:
            account_lines.append(f"  {act}: error ({str(e)[:60]})")
            continue

        meta_ccy = d["currency"]
        spend_native = d["spend"]
        value_native = d["purchase_value"]

        spend_shop = await convert(spend_native, meta_ccy, shop_ccy)
        value_shop = await convert(value_native, meta_ccy, shop_ccy)

        total_spend_shop += spend_shop
        total_meta_value_shop += value_shop
        total_spend_native += spend_native
        total_meta_purchases += d["purchases"]
        if native_ccy is None:
            native_ccy = meta_ccy

        if meta_ccy != shop_ccy:
            fx_notes.add(f"{meta_ccy}→{shop_ccy}")

        if d["spend"] > 0 or d["purchases"] > 0:
            if meta_ccy == shop_ccy:
                account_lines.append(
                    f"  {act}: spend {spend_native:,.2f} {meta_ccy} · "
                    f"{d['purchases']} purchases · reported rev {value_native:,.2f} {meta_ccy}"
                )
            else:
                account_lines.append(
                    f"  {act}: spend {spend_native:,.2f} {meta_ccy} (≈ {spend_shop:,.2f} {shop_ccy}) · "
                    f"{d['purchases']} purchases · reported rev {value_native:,.2f} {meta_ccy} (≈ {value_shop:,.2f} {shop_ccy})"
                )

    # GA4 first-party attribution — optional, only for stores mapped in
    # STORE_GA4_STREAMS. Returns None silently for unmapped stores so brands
    # still on the old path don't see any behavior change.
    ga4 = await ga4_metrics(store.slug, days)
    ga4_revenue_shop = 0.0
    ga4_roas = 0.0
    if ga4 and ga4.revenue > 0:
        ga4_revenue_shop = await convert(ga4.revenue, ga4.currency, shop_ccy)
        ga4_roas = (ga4_revenue_shop / total_spend_shop) if total_spend_shop > 0 else 0.0

    # Paid-only view (conservative floor — current "financial_status=paid" count)
    paid_roas = (shopify.paid_revenue / total_spend_shop) if total_spend_shop > 0 else 0.0

    # Pipeline view (paid + pending = all real conversions, since the delivery-partner
    # integration that promotes COD orders to paid is currently broken upstream)
    pipeline_orders = shopify.pipeline_orders
    pipeline_revenue = shopify.pipeline_revenue
    pipeline_roas = (pipeline_revenue / total_spend_shop) if total_spend_shop > 0 else 0.0

    # Customer Acquisition Cost per pipeline order, in Meta's native currency
    cac_native = (total_spend_native / pipeline_orders) if pipeline_orders > 0 else 0.0
    threshold = CAC_THRESHOLDS.get(store.slug)
    cac_verdict = ""
    if threshold and native_ccy == threshold[1] and cac_native > 0:
        target, t_ccy = threshold
        if cac_native <= target:
            cac_verdict = f" ✅ within threshold (≤ {target:.2f} {t_ccy})"
        elif cac_native <= target * 1.5:
            cac_verdict = f" 🟡 above threshold (target ≤ {target:.2f} {t_ccy}, +{(cac_native/target-1)*100:.0f}%)"
        else:
            cac_verdict = f" 🔴 well above threshold (target ≤ {target:.2f} {t_ccy}, +{(cac_native/target-1)*100:.0f}%)"

    meta_roas = (total_meta_value_shop / total_spend_shop) if total_spend_shop > 0 else 0.0

    fx_tag = ""
    if fx_notes:
        fx_tag = f"  (FX: {', '.join(sorted(fx_notes))})"

    lines = [
        f"*{store.brand}* · last {days}d · ROAS",
        "",
        f"Pipeline orders (paid+pending): *{pipeline_orders}*  ·  paid: {shopify.paid_orders}  ·  pending: {shopify.pending_orders}  ·  cancelled: {shopify.cancelled_orders}",
        f"Pipeline revenue: {pipeline_revenue:,.2f} {shop_ccy}  (paid: {shopify.paid_revenue:,.2f}, pending: {shopify.pending_revenue:,.2f})",
        f"Meta spend: {total_spend_native:,.2f} {native_ccy or '?'}  (≈ {total_spend_shop:,.2f} {shop_ccy}){fx_tag}",
    ]
    # GA4 block only appears for mapped stores; keeps the message compact for
    # brands that don't have GA4 wired in yet.
    if ga4:
        lines.append(
            f"GA4: *{ga4.purchases}* purchases · {ga4_revenue_shop:,.2f} {shop_ccy} revenue · "
            f"{ga4.sessions:,} sessions · {ga4.converted_sessions} converted sessions"
        )
    lines.extend([
        "",
        f"*Pipeline ROAS: {pipeline_roas:.2f}x*  (pipeline revenue / Meta spend, same-currency — use this as the truth until delivery-partner payment status is fixed)",
        f"Paid-only ROAS: {paid_roas:.2f}x  (conservative floor, under-reports because courier→Shopify status sync is broken)",
        f"Meta-reported ROAS: {meta_roas:.2f}x  (from `omni_purchase` — matches Meta Ads Manager; can't see Amazon conversions)",
    ])
    if ga4:
        lines.append(
            f"*GA4 ROAS: {ga4_roas:.2f}x*  (first-party ground truth · session-attributed, excludes Amazon / in-app Meta Shop)"
        )
    lines.extend([
        "",
        f"*CAC per pipeline order: {cac_native:,.2f} {native_ccy or '?'}*{cac_verdict}",
    ])
    if account_lines:
        lines.append("")
        lines.append("Per-account breakdown (native currency first):")
        lines.extend(account_lines)

    # ── Destination-split: how much Meta spend goes to Shopify vs Amazon ──
    # Without this, the headline ROAS unfairly penalises Meta spend whose
    # ads point at Amazon listings (their conversions land outside Shopify).
    dest_split: dict[str, float] = {}
    for act in ad_accounts:
        per_act = await _meta_spend_by_destination(act, days)
        for k, v in per_act.items():
            dest_split[k] = dest_split.get(k, 0.0) + v

    if dest_split:
        amazon_spend_native  = dest_split.get("amazon", 0.0)
        shopify_spend_native = sum(v for k, v in dest_split.items() if k.startswith("shopify-"))
        other_spend_native   = sum(v for k, v in dest_split.items() if k in ("other", "unknown", "shopify-other"))
        amazon_spend_shop  = await convert(amazon_spend_native, native_ccy or shop_ccy, shop_ccy)
        shopify_spend_shop = await convert(shopify_spend_native, native_ccy or shop_ccy, shop_ccy)

        # Shopify-only pipeline ROAS: pipeline revenue / spend on Shopify-destined ads only
        shopify_only_roas = (pipeline_revenue / shopify_spend_shop) if shopify_spend_shop > 0 else 0.0

        lines.append("")
        lines.append("*Destination split (where the Meta budget actually points):*")
        lines.append(
            f"  → Shopify-destined: {shopify_spend_native:,.2f} {native_ccy or '?'} "
            f"(≈ {shopify_spend_shop:,.2f} {shop_ccy}) — {(shopify_spend_native/total_spend_native*100 if total_spend_native else 0):.0f}% of Meta spend"
        )
        if amazon_spend_native > 0:
            lines.append(
                f"  → Amazon-destined: {amazon_spend_native:,.2f} {native_ccy or '?'} "
                f"(≈ {amazon_spend_shop:,.2f} {shop_ccy}) — {(amazon_spend_native/total_spend_native*100 if total_spend_native else 0):.0f}% of Meta spend "
                f"_(conversions land on Amazon, NOT in Shopify pipeline)_"
            )
        if other_spend_native > 0:
            lines.append(
                f"  → Other / unattributed: {other_spend_native:,.2f} {native_ccy or '?'}"
            )
        if shopify_spend_shop > 0:
            lines.append(
                f"  *Shopify-only Pipeline ROAS: {shopify_only_roas:.2f}x* "
                f"(pipeline revenue / Shopify-destined Meta spend — fairer baseline)"
            )

        # ── Amazon-side context from MAP ──────────────────────────────────────
        map_acct = _map_account_for(slug)
        if map_acct:
            try:
                from ads_agent.map.mcp_client import ads_totals as _map_totals
                a = await _map_totals(map_acct[0], map_acct[1], days)
                a_cost = float(a.get("cost") or 0)
                a_sales = float(a.get("sales14d") or 0)
                a_purch = int(a.get("purchases14d") or 0)
                a_ccy = a.get("currency", "")
                a_roas = (a_sales / a_cost) if a_cost else 0.0
                lines.append("")
                lines.append("*Amazon side (last %dd, separate from Meta):*" % days)
                lines.append(
                    f"  Amazon Ads spend: {a_cost:,.2f} {a_ccy} · sales (14d): {a_sales:,.2f} {a_ccy} · "
                    f"purchases: {a_purch} · ROAS {a_roas:.2f}x"
                )
                lines.append(
                    f"  _Amazon Ads spend is independent of Meta. Some Meta spend "
                    f"({amazon_spend_native:,.0f} {native_ccy or '?'} above) drove halo "
                    f"traffic to Amazon listings; that uplift sits inside the Amazon "
                    f"sales number above but isn't separately attributed._"
                )
            except Exception as e:
                log.warning("MAP ads_totals failed for %s: %s", slug, e)

    return {**state, "reply_text": "\n".join(lines)}
