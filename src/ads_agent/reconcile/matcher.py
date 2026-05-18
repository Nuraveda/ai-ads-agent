"""Shopify order <-> Meta conversion matcher (v1+).

Strategy:
  1. Exact join on order_id where CAPI was configured to send it.
  2. Fuzzy fallback: value within 1%, time within 10min, same ad account.

v0 stub. Full impl in v1 once your CAPI server is updated to include
order_id + shared event_id in Purchase payloads (v1 prerequisite).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ShopifyOrderRow:
    order_id: str
    created_at: int  # unix
    value: float
    currency: str
    utm_source: str | None
    utm_campaign: str | None


@dataclass(frozen=True)
class MetaConversionRow:
    event_time: int
    value: float
    currency: str
    order_id: str | None  # present when CAPI sent custom_data.order_id
    campaign_id: str | None


def match(
    shopify: list[ShopifyOrderRow],
    meta: list[MetaConversionRow],
) -> tuple[list[tuple[ShopifyOrderRow, MetaConversionRow]], list[ShopifyOrderRow], list[MetaConversionRow]]:
    # TODO(v1): implement exact + fuzzy join, return (matched, shopify_only, meta_only)
    raise NotImplementedError("matcher lands in v1")
