"""Common GAQL queries against a customer's Google Ads account.

GAQL = Google Ads Query Language. SELECT … FROM <resource> WHERE …
Reference: https://developers.google.com/google-ads/api/docs/query/overview

Each helper takes a `slug`, runs the query, and returns a list of plain
dicts so callers don't have to deal with proto-plus messages.
"""
from __future__ import annotations

import logging
from typing import Any

from ads_agent.google_ads.client import GoogleAdsError, search

log = logging.getLogger(__name__)


def _enum_name(v: Any) -> str:
    """Convert proto-plus Enum to its name; pass through strings; '' for None."""
    if v is None:
        return ""
    if hasattr(v, "name"):
        return v.name
    return str(v)


def list_campaigns(slug: str, days: int = 14) -> list[dict]:
    """Active campaigns + their last-N-day performance metrics."""
    gaql = f"""
      SELECT
        campaign.id, campaign.name, campaign.status,
        campaign.advertising_channel_type, campaign.bidding_strategy_type,
        campaign_budget.amount_micros,
        metrics.impressions, metrics.clicks, metrics.cost_micros,
        metrics.conversions, metrics.conversions_value,
        metrics.ctr, metrics.average_cpc, metrics.average_cpm
      FROM campaign
      WHERE segments.date DURING LAST_{days}_DAYS
        AND campaign.status != 'REMOVED'
      ORDER BY metrics.cost_micros DESC
    """.replace("LAST_14_DAYS", "LAST_14_DAYS").replace(f"LAST_{days}_DAYS",
        "LAST_7_DAYS" if days == 7 else
        "LAST_14_DAYS" if days == 14 else
        "LAST_30_DAYS" if days == 30 else
        "LAST_14_DAYS")
    rows = search(slug, gaql)
    out: list[dict] = []
    for r in rows:
        spend = (r.metrics.cost_micros or 0) / 1_000_000
        sales = float(r.metrics.conversions_value or 0)
        out.append({
            "campaign_id":   str(r.campaign.id),
            "name":          r.campaign.name,
            "status":        _enum_name(r.campaign.status),
            "channel_type":  _enum_name(r.campaign.advertising_channel_type),
            "bid_strategy":  _enum_name(r.campaign.bidding_strategy_type),
            "daily_budget":  (r.campaign_budget.amount_micros or 0) / 1_000_000,
            "impressions":   int(r.metrics.impressions or 0),
            "clicks":        int(r.metrics.clicks or 0),
            "cost":          spend,
            "conversions":   float(r.metrics.conversions or 0),
            "sales":         sales,
            "ctr":           float(r.metrics.ctr or 0),
            "avg_cpc":       (r.metrics.average_cpc or 0) / 1_000_000,
            "roas":          (sales / spend) if spend > 0 else 0.0,
        })
    return out


def list_keywords(slug: str, days: int = 14, min_cost: float = 0.0) -> list[dict]:
    """Keyword-level metrics. Pulls keyword_view (everything that has metrics)."""
    days_clause = (
        "LAST_7_DAYS" if days == 7 else
        "LAST_14_DAYS" if days == 14 else
        "LAST_30_DAYS" if days == 30 else
        "LAST_14_DAYS"
    )
    gaql = f"""
      SELECT
        ad_group_criterion.criterion_id,
        ad_group_criterion.keyword.text,
        ad_group_criterion.keyword.match_type,
        ad_group_criterion.status,
        ad_group_criterion.cpc_bid_micros,
        ad_group.id, ad_group.name,
        campaign.id, campaign.name,
        metrics.impressions, metrics.clicks, metrics.cost_micros,
        metrics.conversions, metrics.conversions_value,
        metrics.ctr, metrics.average_cpc
      FROM keyword_view
      WHERE segments.date DURING {days_clause}
        AND ad_group_criterion.status != 'REMOVED'
      ORDER BY metrics.cost_micros DESC
    """
    rows = search(slug, gaql)
    out: list[dict] = []
    for r in rows:
        spend = (r.metrics.cost_micros or 0) / 1_000_000
        if spend < min_cost:
            continue
        sales = float(r.metrics.conversions_value or 0)
        out.append({
            "criterion_id": str(r.ad_group_criterion.criterion_id),
            "keyword_text": r.ad_group_criterion.keyword.text,
            "match_type":   _enum_name(r.ad_group_criterion.keyword.match_type),
            "status":       _enum_name(r.ad_group_criterion.status),
            "cpc_bid":      (r.ad_group_criterion.cpc_bid_micros or 0) / 1_000_000,
            "ad_group_id":  str(r.ad_group.id),
            "ad_group_name": r.ad_group.name,
            "campaign_id":  str(r.campaign.id),
            "campaign_name": r.campaign.name,
            "impressions":  int(r.metrics.impressions or 0),
            "clicks":       int(r.metrics.clicks or 0),
            "cost":         spend,
            "conversions":  float(r.metrics.conversions or 0),
            "sales":        sales,
            "roas":         (sales / spend) if spend > 0 else 0.0,
            "ctr":          float(r.metrics.ctr or 0),
            "avg_cpc":      (r.metrics.average_cpc or 0) / 1_000_000,
        })
    return out


def list_search_terms(slug: str, days: int = 14, min_cost: float = 0.0) -> list[dict]:
    """Actual search queries that triggered ads (search_term_view).
    Most useful for finding negative-keyword candidates + harvest opportunities."""
    days_clause = (
        "LAST_7_DAYS" if days == 7 else
        "LAST_14_DAYS" if days == 14 else
        "LAST_30_DAYS" if days == 30 else
        "LAST_14_DAYS"
    )
    gaql = f"""
      SELECT
        search_term_view.search_term,
        search_term_view.status,
        ad_group.id, ad_group.name,
        campaign.id, campaign.name,
        metrics.impressions, metrics.clicks, metrics.cost_micros,
        metrics.conversions, metrics.conversions_value
      FROM search_term_view
      WHERE segments.date DURING {days_clause}
      ORDER BY metrics.cost_micros DESC
    """
    rows = search(slug, gaql)
    out: list[dict] = []
    for r in rows:
        spend = (r.metrics.cost_micros or 0) / 1_000_000
        if spend < min_cost:
            continue
        sales = float(r.metrics.conversions_value or 0)
        out.append({
            "search_term":    r.search_term_view.search_term,
            "status":         _enum_name(r.search_term_view.status),
            "ad_group_id":    str(r.ad_group.id),
            "campaign_id":    str(r.campaign.id),
            "campaign_name":  r.campaign.name,
            "impressions":    int(r.metrics.impressions or 0),
            "clicks":         int(r.metrics.clicks or 0),
            "cost":           spend,
            "conversions":    float(r.metrics.conversions or 0),
            "sales":          sales,
            "roas":           (sales / spend) if spend > 0 else 0.0,
        })
    return out


def account_totals(slug: str, days: int = 14) -> dict:
    """Account-level totals aggregated from list_campaigns()."""
    rows = list_campaigns(slug, days=days)
    spend  = sum(r["cost"] for r in rows)
    sales  = sum(r["sales"] for r in rows)
    purch  = sum(r["conversions"] for r in rows)
    clicks = sum(r["clicks"] for r in rows)
    imp    = sum(r["impressions"] for r in rows)
    return {
        "spend": round(spend, 2),
        "sales": round(sales, 2),
        "conversions": round(purch, 2),
        "clicks": clicks,
        "impressions": imp,
        "roas": round(sales / spend, 2) if spend else 0.0,
        "n_campaigns": len(rows),
        "_source": "google_ads_native",
    }
