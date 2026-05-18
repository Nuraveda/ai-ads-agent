"""Write helpers for LinkedIn Marketing API.

All helpers default to PAUSED / DRAFT state where applicable so nothing
goes live by accident. Promote to ACTIVE via a separate explicit update.

Reference:
  https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads/account-structure/create-and-manage-campaign-groups
  https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads/account-structure/create-and-manage-campaigns
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any

from ads_agent.linkedin.client import LinkedInError, ad_account_id_for, request

log = logging.getLogger(__name__)


def _ms(d: date) -> int:
    """Convert a date to UTC-midnight epoch ms."""
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def _start_ms() -> int:
    """LinkedIn rejects runSchedule.start in the past. Use now + 60s buffer."""
    return int((time.time() + 60) * 1000)


def create_campaign_group(
    slug: str,
    *,
    name: str,
    total_budget: float | None = None,
    currency: str = "USD",
    days: int = 30,
    status: str = "DRAFT",
) -> dict:
    """Create a campaign group on a slug's ad account.

    Defaults to DRAFT — campaign groups must be DRAFT before any campaigns
    inside them can be promoted to ACTIVE.
    """
    aid = ad_account_id_for(slug)
    start_ms = _start_ms()
    end_ms = start_ms + days * 24 * 3600 * 1000
    body: dict[str, Any] = {
        "account":  f"urn:li:sponsoredAccount:{aid}",
        "name":     name,
        "status":   status,
        "runSchedule": {"start": start_ms, "end": end_ms},
    }
    if total_budget is not None:
        body["totalBudget"] = {"currencyCode": currency, "amount": str(total_budget)}
    res = request("POST", f"/rest/adAccounts/{aid}/adCampaignGroups", json_body=body)
    cg_id = res.get("_id")
    if not cg_id:
        raise LinkedInError(f"campaign-group create returned no id: {res}")
    return {
        "id":   str(cg_id),
        "urn":  f"urn:li:sponsoredCampaignGroup:{cg_id}",
        "name": name,
        "status": status,
        "account_id": aid,
    }


def create_campaign(
    slug: str,
    *,
    name: str,
    campaign_group_urn: str,
    daily_budget: float = 5.0,
    unit_cost: float = 5.0,
    currency: str = "USD",
    objective: str = "WEBSITE_TRAFFIC",
    cost_type: str = "CPM",
    type_: str = "SPONSORED_UPDATES",
    locale_country: str = "US",
    locale_language: str = "en",
    days: int = 30,
    status: str = "DRAFT",
) -> dict:
    """Create a campaign under an existing group.

    Defaults are demo-safe: PAUSED, $5/day, single-country US English,
    Sponsored Content / WEBSITE_VISIT objective. Caller must supply a
    `campaign_group_urn` (use `create_campaign_group()` first).
    """
    aid = ad_account_id_for(slug)
    start_ms = _start_ms()
    end_ms = start_ms + days * 24 * 3600 * 1000
    body: dict[str, Any] = {
        "account":             f"urn:li:sponsoredAccount:{aid}",
        "campaignGroup":       campaign_group_urn,
        "name":                name,
        "status":              status,
        "type":                type_,
        "objectiveType":       objective,
        "costType":            cost_type,
        "format":              "SINGLE_VIDEO" if type_ == "SPONSORED_UPDATES" else "TEXT_AD",
        "dailyBudget":         {"currencyCode": currency, "amount": str(daily_budget)},
        "unitCost":            {"currencyCode": currency, "amount": str(unit_cost)},
        "runSchedule":         {"start": start_ms, "end": end_ms},
        "locale":              {"country": locale_country, "language": locale_language},
        # Required boolean flags (LinkedIn rejects without them):
        "audienceExpansionEnabled": False,
        "offsiteDeliveryEnabled":   False,
        # Required disclosure as of 202404: declare whether the campaign
        # contains political content (LinkedIn's equivalent of Google's
        # EU political-ad flag). Always NONE_OF_THE_ABOVE for our flows.
        "politicalIntent": "NOT_DECLARED",
        # Minimal valid targeting: country = US. Without targeting the
        # request fails schema validation.
        "targetingCriteria": {
            "include": {
                "and": [
                    {
                        "or": {
                            "urn:li:adTargetingFacet:locations": [
                                "urn:li:geo:103644278"  # United States
                            ]
                        }
                    }
                ]
            }
        },
    }
    # Sponsored Content campaigns on the SINGLE_VIDEO format require this:
    if type_ == "SPONSORED_UPDATES":
        body["format"] = "STANDARD_UPDATE"
    res = request("POST", f"/rest/adAccounts/{aid}/adCampaigns", json_body=body)
    cid = res.get("_id")
    if not cid:
        raise LinkedInError(f"campaign create returned no id: {res}")
    return {
        "id":  str(cid),
        "urn": f"urn:li:sponsoredCampaign:{cid}",
        "name": name,
        "status": status,
        "campaign_group_urn": campaign_group_urn,
        "account_id": aid,
    }


def update_campaign_status(slug: str, campaign_id: str, status: str) -> dict:
    """Flip a campaign's status (DRAFT → ACTIVE → PAUSED → ARCHIVED)."""
    aid = ad_account_id_for(slug)
    # LinkedIn POST update uses ?ids=...&patch syntax (restli partial update)
    body = {"patch": {"$set": {"status": status}}}
    request(
        "POST",
        f"/rest/adAccounts/{aid}/adCampaigns/{campaign_id}",
        json_body=body,
    )
    return {"id": campaign_id, "new_status": status}


def update_campaign_group_status(slug: str, group_id: str, status: str) -> dict:
    """Flip a campaign group's status."""
    aid = ad_account_id_for(slug)
    body = {"patch": {"$set": {"status": status}}}
    request(
        "POST",
        f"/rest/adAccounts/{aid}/adCampaignGroups/{group_id}",
        json_body=body,
    )
    return {"id": group_id, "new_status": status}
