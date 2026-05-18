"""Meta Conversions API sender (v1+).

Purpose: send Purchase / AddToCart / InitiateCheckout events to Meta with:
  - order_id as custom_data for reliable Shopify <-> Meta join at reconciliation time
  - event_id shared with client-side Pixel for dedup

Uses facebook-python-business-sdk (official). Only lives on the VM side because
the webhook receiver feeds events from /shopify/webhook/{shop}; Cloud Run doesn't
need CAPI wiring.

Stub in v0. Full impl lands in v1 once webhooks are live.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PurchaseEvent:
    pixel_id: str
    event_id: str  # shared with client-side pixel for dedup
    event_time: int  # unix seconds
    order_id: str
    value: float
    currency: str
    email_hash_sha256: str | None = None
    phone_hash_sha256: str | None = None


async def send_purchase(event: PurchaseEvent) -> None:
    # TODO(v1): call facebook_business.adobjects.serverside.event.Event
    raise NotImplementedError("capi_sender lands in v1")
