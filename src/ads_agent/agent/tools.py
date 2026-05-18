"""Pydantic AI tool schemas surfaced into LangGraph nodes.

Typed in/out lets us swap the underlying model (Claude <-> Gemini) without
rewriting tool call sites. v0 exposes only `pull_orders_summary`; more arrive
in v1 (`reconcile`, `roas_compare`) and v2 (`ads_action`).
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class OrdersSummaryInput(BaseModel):
    store_slug: str = Field(..., description="store slug, e.g. 'urban'")
    days: int = Field(7, ge=1, le=90)


class OrdersSummaryOutput(BaseModel):
    store_slug: str
    days: int
    order_count: int
    gross_revenue: float
    currency: str
    avg_order_value: float
    top_skus: list[str] = Field(default_factory=list)


class ROASCompareInput(BaseModel):
    store_slug: str
    days: int = Field(7, ge=1, le=90)


class ROASCompareOutput(BaseModel):
    store_slug: str
    days: int
    meta_spend: float
    shopify_revenue: float
    true_roas: float
    meta_reported_roas: float | None = None
    delta_pct: float | None = None


class TrackingAuditInput(BaseModel):
    store_slug: str
    days: int = Field(30, ge=1, le=90)


class TrackingAuditOutput(BaseModel):
    store_slug: str
    days: int
    match_rate: float
    utm_coverage: float
    dedup_event_id_coverage: float
    recipes: list[str] = Field(default_factory=list)
