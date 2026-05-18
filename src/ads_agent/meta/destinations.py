"""Classify Meta-ad destination URLs.

Engine-level helper that's brand-neutral: every ad gets tagged
`amazon | shopify-ind | shopify-global | shopify-other | other`
plus an `ASIN` when the destination is an Amazon PDP. This lives in the
engine because the *data* is brand-agnostic; the *methodology* (whether
to use the halo number or pause on Meta-ROAS) lives per-brand in
`playbooks/<brand>.md` Section X.

Why we do this:
  - Meta has no visibility into Amazon orders, so any Amazon-destined ad
    structurally reads `omni_purchase = 0` in the Graph API insights —
    Meta-reported ROAS on those ads is a floor, not the truth.
  - A brand-tuned audit (e.g. <client>'s) needs to identify Amazon ads
    and use the cross-channel halo number from
    `ads_agent.amazon_attribution_daily_v` instead of Meta-ROAS, then
    propose `RECLAIM` rather than `PAUSE`.
  - Other brands without an `amazon` destination just see all ads tagged
    `shopify-*` and the existing methodology runs unchanged.

We do NOT follow `amzn.eu/d/<short>` redirects here — the resolver lives
inside `agent/analysis/meta_decomposer.py` so the network round-trip
stays in one place.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

# Compiled once. Matches /dp/ASIN, /gp/product/ASIN, /product/ASIN paths.
_ASIN_RE = re.compile(r"/(?:dp|gp/product|product)/([A-Z0-9]{10})(?:[/?]|$)")

# Name-based intent classifiers. Names carry the human-stated intent of a
# campaign (e.g. "UAE - AMAZON - GoodGut") and are useful when:
#   - URL extraction fails (missing destination_url, short-link not yet
#     resolved, sync field-expansion bug)
#   - URL and name disagree (e.g. campaign duplicated from Amazon to
#     Shopify but name not updated — common operational drift)
# Used together with classify_destination() for cross-evidence.
# `amazon` and `amzn` are unique enough tokens that we don't enforce word
# boundaries — <client> names like `HOJ_Luna_NewAmazon` and `_amazon_uae`
# mix them into compound names. False positives on these tokens in normal
# English ad copy are rare. `amz` alone IS too generic; require a separator
# (`amz_`, `amz-`, `_amz`, `-amz`) so we don't fire on "amzanything".
_NAME_AMZ_RE = re.compile(r"(amazon|amzn|amz[_-]|[_-]amz)", re.I)
_NAME_AE_RE  = re.compile(r"(?<![a-z])(uae|dubai|abu[- ]?dhabi|emirates|fitoor)(?![a-z])", re.I)
# IN is a tricky token — it appears as a word in lots of unrelated names.
# Look for the more specific signals.
_NAME_IN_RE  = re.compile(
    r"(?<![a-z])(india|bharat|delhi|mumbai|bangalore|hyderabad|noida|gurgaon|" \
    r"chennai|pune|kolkata|amazon[._ -]?in|in[._ -]?amazon)(?![a-z])",
    re.I,
)


def classify_destination(url: str | None) -> str:
    """Bucket a Meta destination URL into a stable label.

    Returns one of:
      amazon
      shopify-<primary_market>   (per-store registry hit, lowercase market code)
      shopify-other              (any *.myshopify.com)
      other | unknown

    Custom Shopify domains are resolved via STORE_BRAND_REGISTRY_JSON's
    `shop_host` + `primary_market` fields. Add an entry there to surface
    a new store-specific bucket (e.g. `shopify-ae`, `shopify-in`).
    """
    if not url:
        return "unknown"
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return "unknown"
    if "amazon." in host or host.endswith("amzn.eu") or host.endswith("amzn.to"):
        return "amazon"
    # Custom-domain match against registry
    # brand_registry stripped from open-source release; provide your own
    for shop_host, slug in host_to_slug().items():
        if shop_host and shop_host in host:
            entry = entry_for(slug)
            mkt = (entry.primary_market.lower() if entry and entry.primary_market
                   else "custom")
            return f"shopify-{mkt}"
    if "myshopify.com" in host:
        return "shopify-other"
    return "other"


def classify_name(*texts: str | None) -> dict:
    """Classify intent from campaign / adset / ad names. Joins all
    supplied texts and runs three keyword passes.

    Returns:
      {"amazon_intent": bool, "market_hint": "AE"|"IN"|"unknown"}
    """
    blob = " ".join(t for t in texts if t)
    return {
        "amazon_intent": bool(_NAME_AMZ_RE.search(blob)),
        "market_hint":   "AE" if _NAME_AE_RE.search(blob)
                         else ("IN" if _NAME_IN_RE.search(blob) else "unknown"),
    }


def cross_check(destination: str, name_classification: dict) -> str:
    """Compare URL-based destination vs name-based intent.

    Returns:
      "match"       — both signals agree (most cases)
      "name_only"   — name says Amazon but URL points at Shopify or other
      "url_only"    — URL says Amazon but name has no Amazon hint
      "n/a"         — not Amazon-related on either side
    """
    url_amazon = (destination == "amazon")
    name_amazon = bool(name_classification.get("amazon_intent"))
    if not url_amazon and not name_amazon:
        return "n/a"
    if url_amazon and name_amazon:
        return "match"
    if name_amazon and not url_amazon:
        return "name_only"  # campaign named Amazon, URL points elsewhere
    return "url_only"       # URL is Amazon, name doesn't say so


def parse_asin(url: str | None) -> str | None:
    """Extract a 10-character ASIN from an Amazon URL, if present.

    Handles the canonical /dp/{ASIN}, /gp/product/{ASIN}, /product/{ASIN}
    paths. Returns None for amzn.eu/d/<short> short links — those need a
    one-shot redirect resolution upstream.
    """
    if not url:
        return None
    m = _ASIN_RE.search(url)
    return m.group(1) if m else None


def extract_destination_link(creative: dict | None) -> str | None:
    """Walk the four Meta object_story_spec shapes that carry the
    landing-page URL. Returns None if the creative omits all of them
    (rare — usually means the ad is using an inactive deep-link).
    """
    if not creative:
        return None
    os_spec = creative.get("object_story_spec") or {}
    # 1) video_data.call_to_action.value.link
    vd = os_spec.get("video_data") or {}
    cta = (vd.get("call_to_action") or {}).get("value") or {}
    if cta.get("link"):
        return cta["link"]
    # 2) link_data.link (image/link ads)
    ld = os_spec.get("link_data") or {}
    if ld.get("link"):
        return ld["link"]
    # 3) object_url (legacy)
    if creative.get("object_url"):
        return creative["object_url"]
    # 4) asset_feed_spec.link_urls (Dynamic Creative Optimization)
    afs = creative.get("asset_feed_spec") or {}
    link_urls = afs.get("link_urls") or []
    if link_urls and isinstance(link_urls, list):
        first = link_urls[0]
        if isinstance(first, dict) and first.get("website_url"):
            return first["website_url"]
    return None
