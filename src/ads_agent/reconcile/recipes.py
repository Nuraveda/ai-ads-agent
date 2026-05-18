"""Remediation recipes. Strings only — HITL via Telegram, no auto-apply in v1.

The agent picks which of these to surface based on metrics from `metrics.py`.
Recipes intentionally name the specific file/admin-panel location to edit so
the operator can act without further investigation.

**Checkout-topology matters**: recipes differ between brands depending on
whether checkout is native-Shopify (<client>, Mokshya) or 3rd-party-hosted
(Urban family: Classicoo/Storico on Shiprocket; Urban Classics/Trendsetters
on Flexipe). The `recipe_for()` helper selects the right variant based on
store slug; nodes should call it instead of indexing RECIPES directly.
"""
from __future__ import annotations


# ---- Per-topology recipe pools ---------------------------------------------

# Brands with 3rd-party checkout (Shopify → Shiprocket / Flexipe → thank-you).
# Meta Pixel + CAPI fire from the 3rd-party admin, not Shopify theme.
_RECIPES_3PARTY_CHECKOUT: dict[str, str] = {
    "low_utm_coverage": (
        "UTM coverage is low. Enable Meta auto-tagging (\"URL parameters\" in Ads Manager → Ad level). "
        "Also verify the 3rd-party checkout (Shiprocket / Flexipe) preserves query params when "
        "redirecting customers from Shopify → 3rd-party → thank-you. If the 3rd-party strips UTMs "
        "on handoff, the Purchase event they fire can't attribute back to the ad."
    ),
    "capi_gap_no_order_id": (
        "Meta is not receiving Shopify `order_id` on Purchase events. In the 3rd-party checkout "
        "admin (Shiprocket or Flexipe) → Meta/Facebook integration → enable \"Send order ID in "
        "custom_data\". If the 3rd-party doesn't expose this, ask them to add `custom_data.order_id` "
        "to their CAPI payload so reconciliation against Shopify orders becomes exact, not fuzzy."
    ),
    "pixel_not_firing": (
        "Meta reports 0 Purchase events but Shopify has real orders. Root cause is usually one of: "
        "(1) 3rd-party checkout's Meta integration not enabled — turn on Pixel + CAPI in "
        "Shiprocket / Flexipe admin, paste your Pixel ID and Meta Access Token; "
        "(2) Pixel base code missing from Shopify theme (needed for ViewContent / AddToCart). "
        "Diff the theme.liquid of the broken store against a working sibling store to find the gap."
    ),
    "no_dedup_event_id": (
        "Browser Pixel and server CAPI are both firing Purchase but Meta isn't deduplicating — "
        "that's why Meta-reported purchases run 10–20× higher than Shopify-confirmed orders. "
        "In the 3rd-party checkout admin (Shiprocket / Flexipe), enable `event_id` generation "
        "and verify BOTH the browser Pixel call AND the server CAPI call emit the SAME "
        "event_id per order. Meta dedups within a 48h window on matching event_id + event_name."
    ),
    "third_party_pixel_mismatch": (
        "Checkout is on a 3rd-party domain (Shiprocket / Flexipe), so the Meta Pixel configured "
        "in your Shopify theme NEVER fires Purchase — only ViewContent / AddToCart. All Purchase "
        "events come from the 3rd-party's server-side CAPI. Audit the 3rd-party's Meta integration "
        "to ensure it's attached to the RIGHT Pixel (matching the one on your Shopify theme) — "
        "a mismatch here produces Pixel-only ViewContent data and CAPI-only Purchase data, both "
        "orphaned from each other in Meta's attribution model."
    ),
    "spend_up_revenue_flat": (
        "Spend has increased >20% week-over-week while Shopify revenue is flat (in the same currency). "
        "Likely paths: (a) tracking regression (Pixel/CAPI event drop from 3rd-party), "
        "(b) COD accept-rate drop (check `glitch-cod-confirm` health), (c) audience saturation / "
        "creative fatigue on the top-spend ad set (run /ads <store> and check frequency)."
    ),
    "delivery_status_not_updating": (
        "Shopify orders stay `financial_status=pending` indefinitely — the delivery-partner → "
        "Shopify status sync is broken. COD orders never promote to `paid` even after cash is "
        "collected on delivery. Agent treats pipeline (paid+pending) as the truth until this is "
        "fixed. Follow-up with Shiprocket / Flexipe / courier to restore the delivered-and-paid "
        "status webhook back to Shopify."
    ),
}


# Brands with native Shopify checkout + Shopify Facebook & Instagram sales
# channel handling pixel + CAPI (<client>, Mokshya, etc.). No 3rd-party
# redirect; Meta events flow via the Shopify FB channel app.
_RECIPES_SHOPIFY_NATIVE: dict[str, str] = {
    "low_utm_coverage": (
        "UTM coverage on web orders is low. Fix in Meta Ads Manager: Ad account → "
        "Settings → URL parameters, set the account-level default template to "
        "`utm_source=meta&utm_medium=paid&utm_campaign={{campaign.name}}|{{campaign.id}}"
        "&utm_content={{ad.name}}|{{ad.id}}&utm_term={{adset.name}}|{{adset.id}}`. "
        "This tags every outbound click; Shopify captures it in order.landing_site. "
        "Propagation to delivered ads takes 4-8 hours. Note: orders whose `source_name` is a "
        "numeric Meta app ID come from in-app Shop checkout — UTMs are physically impossible "
        "for those, so the effective coverage metric already excludes them."
    ),
    "capi_gap_no_order_id": (
        "Verify Shopify → Sales channels → Facebook & Instagram → Data sharing is ON for "
        "Pixel + Conversions API + Customer information. The Shopify FB channel handles "
        "`event_id = order_id` deduplication automatically — no theme-level code required. "
        "If EMQ (Event Match Quality) in Meta Events Manager is &lt; 7.0, the likely cause is "
        "customer info fields (email/phone/address) not being passed. Enable all three toggles."
    ),
    "pixel_not_firing": (
        "Meta reports 0 Purchase events but Shopify has real orders. Check Shopify → Sales "
        "channels → Facebook & Instagram: (1) is the channel installed and connected to the "
        "Commerce Manager? (2) is the Meta Pixel ID populated and matching the pixel on the "
        "Meta ad account running ads for this store? A mismatched pixel ID sends events to the "
        "wrong ad account's pixel and they appear as zero on the one you're looking at."
    ),
    "no_dedup_event_id": (
        "The Shopify Facebook & Instagram sales channel handles pixel+CAPI dedup via `event_id` "
        "automatically — if you're seeing a 3-5× purchase-count gap vs Shopify, it's usually NOT "
        "a dedup bug. Two other common causes to check first: "
        "(a) Meta's 7-day-click + 1-day-view attribution window counts purchases that Shopify "
        "records days apart; (b) In-app-checkout purchases on Meta Shop are credited to Meta "
        "but appear in Shopify under `source_name = <numeric Meta app id>`. Only after ruling "
        "these out should you investigate Meta Events Manager → Diagnostics → duplicate events."
    ),
    "third_party_pixel_mismatch": (
        "Not applicable — this store uses Shopify's native checkout. If diagnosing cross-device "
        "or cross-subdomain tracking issues, check Shopify → Customer events → custom Web Pixels "
        "to see if any 3rd-party analytics tools are injecting a competing pixel."
    ),
    "spend_up_revenue_flat": (
        "Spend has risen &gt;20% WoW while Shopify revenue is flat. Likely causes in order of "
        "probability: (a) creative fatigue on top-spend ad set (check frequency via /ads), "
        "(b) audience saturation (expand or test LAL/retargeting), (c) Meta optimizer learning "
        "from low-EMQ signal — fix the pixel quality first if EMQ &lt; 7.0."
    ),
    "delivery_status_not_updating": (
        "Shopify orders stay `financial_status=pending` indefinitely — the courier → Shopify "
        "payment-confirmation webhook is broken. COD orders never promote to `paid` even after "
        "cash is collected. Agent uses pipeline (paid+pending) for ROAS math until the webhook "
        "is restored. Contact the delivery-partner integration (Delhivery, Shiprocket, etc.) "
        "or re-install their Shopify app."
    ),
}


# Map store slugs to checkout topology. Defaults to 3rd-party (the safer
# fallback for stores using Razorpay/Cashfree-style hosted checkout).
# Brands on Shopify native checkout (clean URL = `/checkouts/<token>/thank_you`)
# are read from STORE_NATIVE_CHECKOUT_SLUGS_JSON, e.g.
#   STORE_NATIVE_CHECKOUT_SLUGS_JSON=["store-a","store-b"]


def _native_checkout_slugs() -> set[str]:
    import json
    import os
    raw = os.environ.get("STORE_NATIVE_CHECKOUT_SLUGS_JSON", "").strip()
    if not raw:
        return set()
    try:
        v = json.loads(raw)
        return set(v) if isinstance(v, list) else set()
    except json.JSONDecodeError:
        return set()


def recipe_for(store_slug: str, key: str) -> str:
    """Return the recipe body for `key`, selected for this store's checkout topology."""
    if store_slug in _native_checkout_slugs():
        return _RECIPES_SHOPIFY_NATIVE.get(key, "")
    return _RECIPES_3PARTY_CHECKOUT.get(key, "")


# Backward-compat: keep RECIPES as the 3rd-party variant (default for
# existing call sites); new code should use recipe_for(slug, key) instead.
RECIPES: dict[str, str] = _RECIPES_3PARTY_CHECKOUT
