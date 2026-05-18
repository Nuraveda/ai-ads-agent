"""Common LinkedIn Marketing API queries against a slug's ad account.

Each helper takes a `slug`, hits one or more `/rest/*` endpoints, and
returns plain dicts — same convention as ads_agent.google_ads.queries.

References:
  https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads/account-structure/
  https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads-reporting/ads-reporting
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from ads_agent.linkedin.client import ad_account_id_for, ad_account_urn_for, request

log = logging.getLogger(__name__)


def _date_range(days: int) -> dict[str, Any]:
    end = date.today()
    start = end - timedelta(days=days)
    # Restli tuple syntax: (start:(year:Y,month:M,day:D),end:(...))
    return {
        "dateRange": (
            f"(start:(year:{start.year},month:{start.month},day:{start.day}),"
            f"end:(year:{end.year},month:{end.month},day:{end.day}))"
        ),
    }


def _u_decode(s: str) -> str:
    """LinkedIn URN paths in /rest/* are URL-encoded inline. Helper for
    pretty-printing only."""
    return s.replace("%3A", ":")


# ----- structure queries (no metrics) -------------------------------------

def list_campaign_groups(slug: str) -> list[dict]:
    """Campaign groups (analogous to ad-set groups) on the slug's account."""
    aid = ad_account_id_for(slug)
    res = request(
        "GET", f"/rest/adAccounts/{aid}/adCampaignGroups",
        params={"q": "search"},
    )
    out = []
    for el in res.get("elements", []):
        out.append({
            "id":     str(el.get("id", "")),
            "name":   el.get("name", ""),
            "status": el.get("status", ""),
            "total_budget": (el.get("totalBudget") or {}).get("amount", ""),
        })
    return out


def list_campaigns(slug: str, days: int = 14) -> list[dict]:
    """Campaigns + last-N-day metrics joined via adAnalytics pivot=CAMPAIGN."""
    aid = ad_account_id_for(slug)
    # 1. Roster
    res = request(
        "GET", f"/rest/adAccounts/{aid}/adCampaigns",
        params={"q": "search"},
    )
    camps: dict[str, dict] = {}
    for el in res.get("elements", []):
        cid = str(el.get("id", ""))
        if not cid:
            continue
        camps[f"urn:li:sponsoredCampaign:{cid}"] = {
            "campaign_id":  cid,
            "name":         el.get("name", ""),
            "status":       el.get("status", ""),
            "type":         el.get("type", ""),
            "objective":    el.get("objectiveType", ""),
            "daily_budget": (el.get("dailyBudget") or {}).get("amount", ""),
            "currency":     (el.get("dailyBudget") or {}).get("currencyCode", ""),
            "impressions":  0,
            "clicks":       0,
            "cost":         0.0,
            "conversions":  0,
            "ctr":          0.0,
            "cpc":          0.0,
        }

    # 2. Metrics — adAnalytics finder=analytics pivot=CAMPAIGN
    params = {
        "q":          "analytics",
        "pivot":      "CAMPAIGN",
        "timeGranularity": "ALL",
        # URN colons inside List(...) MUST be %3A-encoded per restli rules,
        # while structural colons (in dateRange tuples) stay literal.
        "accounts": f"List({ad_account_urn_for(slug).replace(':', '%3A')})",
        "fields":     "pivotValues,impressions,clicks,costInUsd,externalWebsiteConversions",
        **_date_range(days),
    }
    metrics_res = request("GET", "/rest/adAnalytics", params=params)
    for row in metrics_res.get("elements", []):
        urns = row.get("pivotValues") or []
        if not urns:
            continue
        urn = urns[0]
        c = camps.get(urn)
        if not c:
            continue
        c["impressions"]  = int(row.get("impressions", 0) or 0)
        c["clicks"]       = int(row.get("clicks", 0) or 0)
        c["cost"]         = float(row.get("costInUsd", 0) or 0)
        c["conversions"]  = int(row.get("externalWebsiteConversions", 0) or 0)
        c["ctr"]          = (c["clicks"] / c["impressions"]) if c["impressions"] else 0.0
        c["cpc"]          = (c["cost"]   / c["clicks"])      if c["clicks"]      else 0.0

    # Sort by spend desc
    return sorted(camps.values(), key=lambda r: r["cost"], reverse=True)


def list_creatives(slug: str, days: int = 14) -> list[dict]:
    """Creative-level metrics via adAnalytics pivot=CREATIVE."""
    params = {
        "q":           "analytics",
        "pivot":       "CREATIVE",
        "timeGranularity": "ALL",
        # URN colons inside List(...) MUST be %3A-encoded per restli rules,
        # while structural colons (in dateRange tuples) stay literal.
        "accounts": f"List({ad_account_urn_for(slug).replace(':', '%3A')})",
        "fields":      "pivotValues,impressions,clicks,costInUsd,externalWebsiteConversions",
        **_date_range(days),
    }
    res = request("GET", "/rest/adAnalytics", params=params)
    out = []
    for row in res.get("elements", []):
        urns = row.get("pivotValues") or []
        urn = urns[0] if urns else ""
        impressions = int(row.get("impressions", 0) or 0)
        clicks      = int(row.get("clicks", 0) or 0)
        cost        = float(row.get("costInUsd", 0) or 0)
        out.append({
            "creative_urn": urn,
            "creative_id":  urn.rsplit(":", 1)[-1] if urn else "",
            "impressions":  impressions,
            "clicks":       clicks,
            "cost":         cost,
            "conversions":  int(row.get("externalWebsiteConversions", 0) or 0),
            "ctr":          (clicks / impressions) if impressions else 0.0,
            "cpc":          (cost / clicks) if clicks else 0.0,
        })
    return sorted(out, key=lambda r: r["cost"], reverse=True)


def account_totals(slug: str, days: int = 14) -> dict:
    """Account-level totals via adAnalytics pivot=ACCOUNT."""
    params = {
        "q":           "analytics",
        "pivot":       "ACCOUNT",
        "timeGranularity": "ALL",
        # URN colons inside List(...) MUST be %3A-encoded per restli rules,
        # while structural colons (in dateRange tuples) stay literal.
        "accounts": f"List({ad_account_urn_for(slug).replace(':', '%3A')})",
        "fields":      "impressions,clicks,costInUsd,externalWebsiteConversions",
        **_date_range(days),
    }
    res = request("GET", "/rest/adAnalytics", params=params)
    el = (res.get("elements") or [{}])[0]
    impressions = int(el.get("impressions", 0) or 0)
    clicks      = int(el.get("clicks", 0) or 0)
    cost        = float(el.get("costInUsd", 0) or 0)
    conv        = int(el.get("externalWebsiteConversions", 0) or 0)
    return {
        "spend":       round(cost, 2),
        "clicks":      clicks,
        "impressions": impressions,
        "conversions": conv,
        "ctr":         round((clicks / impressions) if impressions else 0.0, 4),
        "cpc":         round((cost / clicks) if clicks else 0.0, 2),
        "_source":     "linkedin_marketing_api",
    }
