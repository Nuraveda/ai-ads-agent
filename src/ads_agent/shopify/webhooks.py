"""Shopify webhook receiver.

HMAC verification uses the Custom App client secret (shpss_...) per-app,
pulled from SHOPIFY_WEBHOOK_SECRETS env var (JSON map keyed by custom_app slug).

After verification, each topic routes to a handler that fires PostHog events.
Shopify requires a 200 response within 5 seconds — all heavy work is fire-and-forget.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging

from ads_agent.config import get_store, settings

log = logging.getLogger(__name__)


class HmacVerificationError(RuntimeError):
    pass


def verify_hmac(*, shop_domain: str, raw_body: bytes, header_hmac_b64: str) -> None:
    """Raise HmacVerificationError on mismatch. Silent on success."""
    store = get_store(shop_domain)
    if store is None:
        raise HmacVerificationError(f"unknown shop: {shop_domain}")

    secret = settings().shopify_webhook_secret_map.get(store.custom_app)
    if not secret:
        raise HmacVerificationError(f"no webhook secret for app {store.custom_app}")

    mac = hmac.new(secret.encode(), raw_body, hashlib.sha256)
    expected = base64.b64encode(mac.digest()).decode()
    if not hmac.compare_digest(expected, header_hmac_b64):
        raise HmacVerificationError("hmac mismatch")


# ---------------------------------------------------------------------------
# Topic handlers
# ---------------------------------------------------------------------------

def _extract_order(payload: dict) -> dict:
    """Normalise the fields we care about from a Shopify order webhook payload."""
    money = payload.get("current_total_price") or payload.get("total_price") or "0"
    currency = payload.get("currency", "")
    customer = payload.get("customer") or {}

    # Guest-order fallback (issue #5):
    # `customer` may be present but `customer.id` can be literally None on
    # guest checkouts. The old `str(customer.get("id", ""))` turned None into
    # the string "None", which (a) is truthy so the `or payload.get("email")`
    # fallback never fired and (b) collapsed every guest order into one
    # PostHog person with distinct_id="None". Explicitly treat null/empty ids
    # as missing and prefer email → order_id → "anonymous".
    raw_cid = customer.get("id")
    customer_id_str = str(raw_cid) if raw_cid not in (None, "", 0) else ""
    customer_id = (
        customer_id_str
        or (customer.get("email") or payload.get("email") or "").strip()
        or str(payload.get("id") or "")
        or "anonymous"
    )

    # UTM params live in landing_site (URL string) or customer_journey_summary
    landing = payload.get("landing_site") or ""
    utm: dict[str, str] = {}
    if "utm_source" in landing:
        import urllib.parse
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(landing).query)
        for k in ("utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term"):
            if k in qs:
                utm[k.replace("utm_", "")] = qs[k][0]

    line_items = [
        {
            "sku": li.get("sku") or li.get("variant_id"),
            "title": li.get("title"),
            "quantity": li.get("quantity"),
            "price": li.get("price"),
        }
        for li in payload.get("line_items", [])
    ]

    return {
        "order_id": str(payload.get("id", "")),
        "order_name": payload.get("name", ""),
        "value": float(money),
        "currency": currency,
        "customer_id": customer_id,
        "email": customer.get("email") or payload.get("email") or "",
        "financial_status": payload.get("financial_status", ""),
        "fulfillment_status": payload.get("fulfillment_status") or "unfulfilled",
        "utm": utm,
        "line_items": line_items,
        "tags": payload.get("tags", ""),
        "source_name": payload.get("source_name", ""),
    }


async def handle_webhook(*, topic: str, shop_domain: str, payload: dict) -> None:
    """Route a verified webhook payload to the right handler."""
    store = get_store(shop_domain)
    store_slug = store.slug if store else shop_domain

    handlers = {
        "orders/create": _on_order_create,
        "orders/paid": _on_order_paid,
        "orders/fulfilled": _on_order_fulfilled,
        "orders/cancelled": _on_order_cancelled,
        "refunds/create": _on_refund_create,
    }
    handler = handlers.get(topic)
    if handler is None:
        log.warning("unhandled topic %s for %s", topic, shop_domain)
        return
    try:
        await handler(shop_domain=shop_domain, store_slug=store_slug, payload=payload)
    except Exception:
        log.exception("webhook handler error topic=%s shop=%s", topic, shop_domain)


async def _on_order_create(*, shop_domain: str, store_slug: str, payload: dict) -> None:
    from ads_agent.posthog.client import capture_order_event
    order = _extract_order(payload)
    capture_order_event("order_created", shop_domain=shop_domain, store_slug=store_slug, order=order)
    log.info("order_created shop=%s order=%s value=%s %s",
             store_slug, order["order_name"], order["value"], order["currency"])


async def _on_order_paid(*, shop_domain: str, store_slug: str, payload: dict) -> None:
    from ads_agent.posthog.client import capture_order_event
    order = _extract_order(payload)
    capture_order_event("order_paid", shop_domain=shop_domain, store_slug=store_slug, order=order)
    log.info("order_paid shop=%s order=%s value=%s %s",
             store_slug, order["order_name"], order["value"], order["currency"])


async def _on_order_fulfilled(*, shop_domain: str, store_slug: str, payload: dict) -> None:
    from ads_agent.posthog.client import capture_order_event
    order = _extract_order(payload)
    capture_order_event("order_fulfilled", shop_domain=shop_domain, store_slug=store_slug, order=order)


async def _on_order_cancelled(*, shop_domain: str, store_slug: str, payload: dict) -> None:
    from ads_agent.posthog.client import capture_order_event
    order = _extract_order(payload)
    capture_order_event("order_cancelled", shop_domain=shop_domain, store_slug=store_slug, order=order)


async def _on_refund_create(*, shop_domain: str, store_slug: str, payload: dict) -> None:
    from ads_agent.posthog.client import capture_order_event
    order_id = str(payload.get("order_id", ""))
    amount = float(payload.get("transactions", [{}])[0].get("amount", 0) if payload.get("transactions") else 0)
    capture_order_event(
        "refund_created",
        shop_domain=shop_domain,
        store_slug=store_slug,
        order={"order_id": order_id, "value": -amount, "currency": "", "customer_id": order_id,
               "email": "", "financial_status": "refunded", "fulfillment_status": "",
               "utm": {}, "line_items": [], "order_name": "", "tags": "", "source_name": ""},
    )
    log.info("refund_created shop=%s order_id=%s amount=%s", store_slug, order_id, amount)
