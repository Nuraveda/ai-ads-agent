"""PostHog Cloud client — event capture and querying.

Project: 384306 / Default project (us.i.posthog.com).
Capture key set in POSTHOG_API_KEY env var.

All capture calls are fire-and-forget (PostHog SDK batches + flushes
in a background thread). Never blocks the webhook response path.
"""
from __future__ import annotations

import logging
from functools import lru_cache

import posthog as _posthog

from ads_agent.config import settings

log = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def client() -> _posthog.Posthog:
    s = settings()
    ph = _posthog.Posthog(
        project_api_key=s.posthog_api_key,
        host=s.posthog_host,
    )
    ph.disabled = not s.posthog_api_key or s.posthog_api_key.startswith("phc_REPLACE")
    return ph


def capture_order_event(
    event: str,
    *,
    shop_domain: str,
    store_slug: str,
    order: dict,
    timestamp: str | None = None,
) -> None:
    """Fire a Shopify order lifecycle event to PostHog.

    distinct_id: customer_id preferred; falls back to order_id so every
    event is always person-linked even for guest checkouts.

    Properties included:
      - shop, store_slug, order_id, order_name        (for joins)
      - value, currency                                (for revenue maths)
      - financial_status, fulfillment_status           (for lifecycle)
      - utm_source/medium/campaign/content             (for attribution)
      - line_items JSON, tags, source_name             (for product analysis)
    """
    # Defense-in-depth for issue #5: even if an upstream caller passes
    # customer_id=None or the literal string "None" (common trap from
    # `str(None)`), fall through to order_id / email rather than collapse
    # all guest orders into one PostHog person.
    def _clean(v: object) -> str:
        s = str(v).strip() if v is not None else ""
        return "" if s.lower() in ("", "none", "null") else s

    distinct_id = (
        _clean(order.get("customer_id"))
        or _clean(order.get("email"))
        or _clean(order.get("order_id"))
        or "unknown"
    )
    props: dict = {
        "shop": shop_domain,
        "store_slug": store_slug,
        "order_id": order.get("order_id", ""),
        "order_name": order.get("order_name", ""),
        "value": order.get("value", 0),
        "currency": order.get("currency", ""),
        "email": order.get("email", ""),
        "financial_status": order.get("financial_status", ""),
        "fulfillment_status": order.get("fulfillment_status", ""),
        "source_name": order.get("source_name", ""),
        "tags": order.get("tags", ""),
    }

    # Flatten UTM params as top-level properties so PostHog can filter on them
    for k, v in (order.get("utm") or {}).items():
        props[f"utm_{k}"] = v

    # Line items as JSON string (PostHog doesn't index nested arrays well)
    import json
    li = order.get("line_items") or []
    if li:
        props["line_items"] = json.dumps(li)
        props["line_item_count"] = len(li)

    kwargs = {"distinct_id": distinct_id, "event": event, "properties": props}
    if timestamp:
        from datetime import datetime
        try:
            kwargs["timestamp"] = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass
    try:
        client().capture(**kwargs)
    except Exception:
        log.exception("PostHog capture failed event=%s order_id=%s", event, order.get("order_id"))
