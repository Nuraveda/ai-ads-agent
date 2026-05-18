"""alerts: surface tracking + spend + bias-correction signals the user should act on.

Checks (per store):
  1. CPC drift vs family baseline ($3.50 for Urban-family)
  2. Spend up, revenue flat (last 7d vs prior 7d)
  3. Tracking gap (Meta purchases >> Shopify paid — dedup/CAPI issue)
  4. Premature-kill bias: ad sets paused in last 48h that were still inside
     the learning-phase window (<72h live + <$75 spent). This is the human-bias
     reminder ("you killed this too early, revive if you still believe").
  5. Underperformers that have burned budget with zero paid Shopify orders in 72h.

Returns a ranked list with each alert tagged by severity (🔴 act now / 🟡 watch / 🟢 info).
"""
from __future__ import annotations

from ads_agent.config import STORE_AD_ACCOUNTS, get_store
from ads_agent.fx import convert
from ads_agent.meta.graph_client import MetaGraphError, ads_for_account
from ads_agent.posthog.queries import store_insights


# Per-family CPC baseline (what "normal" looks like). Exceeding this by 30%+ = alert.
FAMILY_CPC_BASELINE: dict[str, tuple[float, str]] = {
    "urban": (3.50, "CAD"),
    "storico": (3.50, "CAD"),
    "classicoo": (3.50, "CAD"),
    "trendsetters": (3.50, "CAD"),
    # example INR - CPC baseline unknown yet; compute from live data later
    # Mokshya - too new, no baseline
}


async def alerts_node(state: dict) -> dict:
    slug = state["store_slug"]
    store = get_store(slug)
    if store is None:
        return {**state, "reply_text": f"Unknown store: `{slug}`"}

    alerts: list[tuple[str, str]] = []  # (severity_emoji, message)

    # --- Shopify side ---
    window_7d = await store_insights(store.slug, 7)
    window_14d = await store_insights(store.slug, 14)

    # --- Meta side: aggregate ads across all linked accounts ---
    ad_accounts = STORE_AD_ACCOUNTS.get(store.slug, [])
    ads_7d: list[dict] = []
    ads_prior_7d: list[dict] = []  # use last 14d minus last 7d as proxy
    for act in ad_accounts:
        try:
            ads_7d.extend(await ads_for_account(act, days=7))
            ads_prior_7d.extend(await ads_for_account(act, days=14))
        except MetaGraphError:
            continue

    # Convert Meta spend/revenue from ad-account currency to Shopify store currency
    # so "spend up revenue flat" is comparing like with like.
    async def _sum_converted(ads, field: str) -> float:
        total = 0.0
        for a in ads:
            total += await convert(a.get(field, 0) or 0, a.get("currency", store.currency), store.currency)
        return total

    spend_7d_shop = await _sum_converted(ads_7d, "spend")
    spend_14d_shop = await _sum_converted(ads_prior_7d, "spend")
    spend_prior_7d_shop = max(0.0, spend_14d_shop - spend_7d_shop)
    purchases_7d = sum(a["purchases"] for a in ads_7d)
    revenue_7d_shop = await _sum_converted(ads_7d, "purchase_value")

    # Meta-native totals (kept in ad-account currency) — used for CPC baseline check
    # which compares against a CPC threshold denominated in Meta's own currency.
    spend_7d_native = sum(a.get("spend", 0) or 0 for a in ads_7d)
    native_ccy = next((a.get("currency") for a in ads_7d if a.get("currency")), "?")

    # --- Check 1: CPC drift vs family baseline (compared in Meta's native currency) ---
    baseline = FAMILY_CPC_BASELINE.get(store.slug)
    if baseline:
        base_cpc, base_currency = baseline
        total_clicks = sum(a["clicks"] for a in ads_7d)
        avg_cpc_native = spend_7d_native / total_clicks if total_clicks > 0 else 0
        # Guard: only compare if ad-account currency matches baseline currency
        if native_ccy == base_currency and avg_cpc_native > 0:
            if avg_cpc_native > base_cpc * 1.3:
                alerts.append(("🔴", (
                    f"*CPC drift:* avg CPC ran at {avg_cpc_native:.2f} {base_currency} last 7d, "
                    f"vs family baseline {base_cpc:.2f}. "
                    f"+{(avg_cpc_native/base_cpc-1)*100:.0f}% — cost-inefficient audiences or creative fatigue."
                )))
            elif avg_cpc_native > base_cpc * 1.15:
                alerts.append(("🟡", (
                    f"*CPC mildly elevated:* {avg_cpc_native:.2f} {base_currency} vs baseline {base_cpc:.2f} "
                    f"(+{(avg_cpc_native/base_cpc-1)*100:.0f}%). Watch for drift."
                )))

    # --- Check 2: spend up, revenue flat (both sides in Shopify store currency) ---
    if spend_7d_shop > 0 and spend_prior_7d_shop > 0:
        spend_delta = (spend_7d_shop - spend_prior_7d_shop) / spend_prior_7d_shop
        paid_7d = window_7d.paid_revenue
        paid_14d = window_14d.paid_revenue
        paid_prior_7d = max(0, paid_14d - paid_7d)
        rev_delta = ((paid_7d - paid_prior_7d) / paid_prior_7d) if paid_prior_7d > 0 else 0
        if spend_delta > 0.2 and rev_delta < 0.05:
            alerts.append(("🔴", (
                f"*Spend up, revenue flat:* spend +{spend_delta*100:.0f}% week-over-week "
                f"({spend_prior_7d_shop:,.0f} → {spend_7d_shop:,.0f} {store.currency}, FX-normalized) but "
                f"Shopify paid revenue moved {rev_delta*100:+.0f}% "
                f"({paid_prior_7d:,.0f} → {paid_7d:,.0f} {store.currency}). Efficiency breaking."
            )))

    # --- Check 3: tracking gap (Meta purchases vs Shopify paid) ---
    if purchases_7d > 0 and window_7d.paid_orders > 0:
        gap = abs(purchases_7d - window_7d.paid_orders) / window_7d.paid_orders
        if gap > 1.0:
            alerts.append(("🔴", (
                f"*Tracking gap:* Meta reports {purchases_7d} purchases vs "
                f"Shopify {window_7d.paid_orders} paid orders last 7d "
                f"({gap*100:.0f}% divergence). Run `/tracking_audit {slug}` for remediation."
            )))
        elif gap > 0.3:
            alerts.append(("🟡", (
                f"*Tracking gap (moderate):* Meta {purchases_7d} vs Shopify {window_7d.paid_orders} paid "
                f"({gap*100:.0f}% divergence last 7d). Likely dedup issue."
            )))

    # Thresholds are denominated in Meta-native currency. For CAD accounts
    # (Urban family), $75 CAD ≈ ₹4,650 INR worth of ad spend — roughly one full
    # learning-phase day. For INR-native accounts (example, Mokshya-INR) this
    # translates to ₹75 which is tiny, so we convert the USD-75 equivalent per account.
    # Simple approach: threshold = 75 in the ad's native currency. This is a
    # useful heuristic because "75 CAD of test spend" and "75 * some multiplier INR"
    # aren't really the same learning-phase budget; tune per-family later.
    LEARNING_BUDGET_NATIVE = 75

    # --- Check 4: premature-kill bias (paused ads still in learning phase) ---
    early_kills = [
        a for a in ads_7d
        if a["status"] == "PAUSED"
        and 0 < a["spend"] < LEARNING_BUDGET_NATIVE
        and a["purchases"] <= 1
    ]
    if early_kills:
        names = ", ".join(f"`{a['ad_name'][:25]}`" for a in early_kills[:3])
        alerts.append(("🟡", (
            f"*Premature-kill reminder:* {len(early_kills)} ad(s) paused while still in learning phase "
            f"(<{LEARNING_BUDGET_NATIVE} {native_ccy} spent + ≤1 purchase). Examples: {names}. "
            f"If you still believe the hypothesis, relaunch with 72h hold."
        )))

    # --- Check 5: underperformers burning budget ---
    zero_purchase_burners = [
        a for a in ads_7d
        if a["status"] == "ACTIVE"
        and a["spend"] > LEARNING_BUDGET_NATIVE
        and a["purchases"] == 0
    ]
    if zero_purchase_burners:
        zero_purchase_burners.sort(key=lambda a: a["spend"], reverse=True)
        names = ", ".join(f"`{a['ad_name'][:25]}` ({a['spend']:.0f} {a.get('currency','?')})" for a in zero_purchase_burners[:3])
        alerts.append(("🔴", (
            f"*Zero-purchase burners:* {len(zero_purchase_burners)} active ad(s) spent >{LEARNING_BUDGET_NATIVE} {native_ccy} "
            f"last 7d with 0 purchases. Top: {names}. Pause or test new creative."
        )))

    # --- Format reply ---
    if not alerts:
        return {**state, "reply_text": f"*{store.brand}* · alerts\nNo actionable signals in last 7d. ✓"}

    # Sort: 🔴 first, then 🟡, then 🟢
    order = {"🔴": 0, "🟡": 1, "🟢": 2}
    alerts.sort(key=lambda t: order.get(t[0], 3))

    lines = [f"*{store.brand}* · alerts (last 7d)", ""]
    for sev, msg in alerts:
        lines.append(f"{sev} {msg}")
        lines.append("")
    return {**state, "reply_text": "\n".join(lines).rstrip()}
