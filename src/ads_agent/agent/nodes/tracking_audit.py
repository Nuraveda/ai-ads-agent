"""tracking_audit: compare Shopify orders ↔ Meta reported conversions and flag gaps.

Surfaces:
  - match_rate = min(meta_purchases, shopify_paid) / max(...) — a rough join on counts
  - utm_coverage — % of orders carrying a known utm_source
  - pixel/CAPI gap signal — if Meta reported purchase_value is wildly higher/lower
    than Shopify paid revenue after currency normalization

Uses Gemini 2.5 Pro to pick the right recipe(s) from reconcile/recipes.py based
on the numbers observed.
"""
from __future__ import annotations

from ads_agent.agent.llm import complete
from ads_agent.config import STORE_AD_ACCOUNTS, get_store
from ads_agent.meta.graph_client import MetaGraphError, account_spend
from ads_agent.posthog.queries import store_insights
from ads_agent.reconcile.recipes import RECIPES, recipe_for

AUDIT_SYSTEM = """You are a Meta Ads + Shopify tracking reconciliation analyst.

CONTEXT you must know:
- Urban family stores use 3rd-party checkouts: Classicoo + Storico → Shiprocket; Urban Classics + Trendsetters → Flexipe.
- Meta Pixel + CAPI Purchase events fire FROM the 3rd-party (Shiprocket/Flexipe), NOT from Shopify theme.
- Shopify `financial_status=paid` is artificially low because courier → Shopify status sync is currently broken; pipeline (paid+pending) is the truth.
- example / Mokshya use Shopify native checkout (different tracking path — standard Pixel flow).

Given a store's numbers, identify which tracking issues are likely, and pick 1–3 remediation recipes by key.

Return EXACTLY this format and nothing else:
DIAGNOSIS: <one short sentence on what the numbers suggest>
RECIPES: <comma-separated recipe keys from: low_utm_coverage, capi_gap_no_order_id, pixel_not_firing, no_dedup_event_id, third_party_pixel_mismatch, spend_up_revenue_flat, delivery_status_not_updating>

Decision rules:
- Meta-reported purchases = 0 AND Shopify has orders (any status): include pixel_not_firing
- Meta-reported purchases >2× Shopify pipeline orders: include no_dedup_event_id
- Meta-reported purchases <0.5× Shopify pipeline orders: include capi_gap_no_order_id
- UTM coverage <25%: include low_utm_coverage
- Shopify paid_orders=0 but pending_orders>0: include delivery_status_not_updating
- Spend up, revenue flat (same currency): include spend_up_revenue_flat
- Never invent recipe keys that aren't in the list.
"""


async def tracking_audit_node(state: dict) -> dict:
    slug = state["store_slug"]
    days = int(state.get("days", 30))
    store = get_store(slug)
    if store is None:
        return {**state, "reply_text": f"Unknown store: `{slug}`"}

    shopify = await store_insights(store.slug, days)

    total_spend = 0.0
    total_meta_purchases = 0
    total_meta_purchase_value = 0.0
    for act in STORE_AD_ACCOUNTS.get(store.slug, []):
        try:
            d = await account_spend(act, days=days)
        except MetaGraphError:
            continue
        total_spend += d["spend"]
        total_meta_purchases += d["purchases"]
        total_meta_purchase_value += d["purchase_value"]

    # Count gap signal
    if shopify.paid_orders > 0:
        purchase_gap_pct = abs(total_meta_purchases - shopify.paid_orders) / shopify.paid_orders * 100
    else:
        purchase_gap_pct = 100.0 if total_meta_purchases > 0 else 0.0

    prior = state.get("prior_context", "") or ""
    numbers = (
        f"{prior}\n\n" if prior else ""
    ) + (
        f"Store: {store.slug} ({store.brand})\n"
        f"Window: last {days} days\n"
        f"Shopify paid orders: {shopify.paid_orders}\n"
        f"Shopify paid revenue: {shopify.paid_revenue:,.2f} {store.currency}\n"
        f"Meta reported purchases: {total_meta_purchases}\n"
        f"Meta reported purchase value: {total_meta_purchase_value:,.2f}\n"
        f"Meta spend: {total_spend:,.2f}\n"
        f"UTM coverage (effective, excludes in-app checkout): {shopify.utm_coverage_pct}%\n"
        f"UTM coverage (raw, all orders): {shopify.utm_coverage_raw_pct}%\n"
        f"In-app-checkout orders (Meta Shop / subscription / POS — UTMs impossible): {shopify.in_app_checkout_orders}\n"
        f"Purchase-count gap |meta − shopify| / shopify: {purchase_gap_pct:.1f}%\n"
    )

    # max_tokens=2500 covers Gemini 2.5's "thinking" token budget plus room for diagnosis + recipes.
    # Inject brand-specific playbook brief to keep diagnoses grounded in
    # example's actual tracking topology (no Shiprocket/Flexipe hallucination,
    # correct Meta Shop in-app checkout handling, etc.).
    from ads_agent.playbook import node_brief
    brand_brief = node_brief("tracking_audit", store.slug.split("-")[0])
    system_prompt = AUDIT_SYSTEM + (
        f"\n\n---\nBRAND PLAYBOOK CONTEXT (authoritative, overrides generic advice):\n{brand_brief}\n"
        if brand_brief else ""
    )
    llm_out = await complete(numbers, tier="smart", system=system_prompt, max_tokens=2500)

    # Parse the strict format
    diagnosis = ""
    keys: list[str] = []
    for ln in llm_out.splitlines():
        if ln.startswith("DIAGNOSIS:"):
            diagnosis = ln.split(":", 1)[1].strip()
        elif ln.startswith("RECIPES:"):
            keys = [k.strip() for k in ln.split(":", 1)[1].split(",") if k.strip() in RECIPES]

    lines = [f"*Tracking audit · {store.brand} · last {days}d*", ""]
    lines.append(f"Shopify paid orders: {shopify.paid_orders} · paid revenue: {shopify.paid_revenue:,.2f} {store.currency}")
    lines.append(f"Meta purchases: {total_meta_purchases} · Meta reported revenue: {total_meta_purchase_value:,.2f}")
    lines.append(f"Meta spend: {total_spend:,.2f}")
    # Report both UTM numbers when they diverge materially (in-app-heavy brand).
    # Otherwise just the effective one is enough.
    if (shopify.in_app_checkout_orders
            and shopify.utm_coverage_pct - shopify.utm_coverage_raw_pct > 5):
        lines.append(
            f"UTM coverage: {shopify.utm_coverage_pct}% of web orders "
            f"(raw {shopify.utm_coverage_raw_pct}% — {shopify.in_app_checkout_orders} "
            f"in-app / sub orders excluded)  |  purchase-count gap: {purchase_gap_pct:.1f}%"
        )
    else:
        lines.append(
            f"UTM coverage: {shopify.utm_coverage_pct}%  |  purchase-count gap: {purchase_gap_pct:.1f}%"
        )
    lines.append("")
    if diagnosis:
        lines.append(f"*Diagnosis:* {diagnosis}")
    if keys:
        lines.append("")
        lines.append("*Recommended fixes:*")
        for k in keys:
            lines.append(f"• {recipe_for(store.slug, k)}")
    else:
        lines.append("(Agent returned no actionable recipes for this window.)")

    return {**state, "reply_text": "\n".join(lines)}
