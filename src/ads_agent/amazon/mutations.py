"""Native Amazon Ads write endpoints — pause / bid / negatives / budget.

Replaces the MAP `call_tool` write path used by the executor. Same
state-transition semantics; first-class typed methods so it's clear
what each one does.

All functions raise AmazonAdsError on non-2xx responses. Caller (the
executor) catches and marks the action row as failed.
"""
from __future__ import annotations

import logging
from typing import Any

from ads_agent.amazon.ads_api import (
    AmazonAdsError,
    _SP_CAMPAIGN, _SP_KW, _SP_NEGKW, _SP_PA, _SP_TARGET,
    _post, profile_id_for, _default_pool,
)

log = logging.getLogger(__name__)


# ----- pause / resume ------------------------------------------------------

async def update_keyword_state(slug: str, keyword_id: str, state: str) -> dict:
    """state ∈ {ENABLED, PAUSED, ARCHIVED}"""
    pool = await _default_pool()
    pid = await profile_id_for(slug, pool)
    body = {"keywords": [{"keywordId": str(keyword_id), "state": state}]}
    return await _post("/sp/keywords", profile_id=pid, json_body=body,
                       accept=_SP_KW, pool=pool)


async def update_product_ad_state(slug: str, ad_id: str, state: str) -> dict:
    pool = await _default_pool()
    pid = await profile_id_for(slug, pool)
    body = {"productAds": [{"adId": str(ad_id), "state": state}]}
    return await _post("/sp/productAds", profile_id=pid, json_body=body,
                       accept=_SP_PA, pool=pool)


async def update_target_state(slug: str, target_id: str, state: str) -> dict:
    pool = await _default_pool()
    pid = await profile_id_for(slug, pool)
    body = {"targetingClauses": [{"targetId": str(target_id), "state": state}]}
    return await _post("/sp/targets", profile_id=pid, json_body=body,
                       accept=_SP_TARGET, pool=pool)


async def update_campaign_state(slug: str, campaign_id: str, state: str) -> dict:
    pool = await _default_pool()
    pid = await profile_id_for(slug, pool)
    body = {"campaigns": [{"campaignId": str(campaign_id), "state": state}]}
    return await _post("/sp/campaigns", profile_id=pid, json_body=body,
                       accept=_SP_CAMPAIGN, pool=pool)


# ----- bid adjustments -----------------------------------------------------

async def update_keyword_bid(slug: str, keyword_id: str, new_bid: float) -> dict:
    pool = await _default_pool()
    pid = await profile_id_for(slug, pool)
    body = {"keywords": [{"keywordId": str(keyword_id), "bid": float(new_bid)}]}
    return await _post("/sp/keywords", profile_id=pid, json_body=body,
                       accept=_SP_KW, pool=pool)


async def update_target_bid(slug: str, target_id: str, new_bid: float) -> dict:
    pool = await _default_pool()
    pid = await profile_id_for(slug, pool)
    body = {"targetingClauses": [{"targetId": str(target_id), "bid": float(new_bid)}]}
    return await _post("/sp/targets", profile_id=pid, json_body=body,
                       accept=_SP_TARGET, pool=pool)


async def update_campaign_budget(slug: str, campaign_id: str, daily_budget: float) -> dict:
    pool = await _default_pool()
    pid = await profile_id_for(slug, pool)
    body = {"campaigns": [{"campaignId": str(campaign_id),
                            "budget": {"budget": float(daily_budget),
                                       "budgetType": "DAILY"}}]}
    return await _post("/sp/campaigns", profile_id=pid, json_body=body,
                       accept=_SP_CAMPAIGN, pool=pool)


# ----- negative keywords ---------------------------------------------------

async def add_negative_keyword(
    slug: str, ad_group_id: str, campaign_id: str,
    keyword_text: str, match_type: str,
) -> dict:
    """match_type ∈ {NEGATIVE_EXACT, NEGATIVE_PHRASE}"""
    pool = await _default_pool()
    pid = await profile_id_for(slug, pool)
    body = {
        "negativeKeywords": [{
            "campaignId": str(campaign_id),
            "adGroupId": str(ad_group_id),
            "keywordText": keyword_text,
            "matchType": match_type,
            "state": "ENABLED",
        }],
    }
    return await _post("/sp/negativeKeywords", profile_id=pid, json_body=body,
                       accept=_SP_NEGKW, pool=pool)


# ----- Generic dispatcher (drop-in for executor's `_map_call`) -------------

# Map MAP tool names → native handlers + arg adapters. Lets the executor
# code pass a tool name + args dict and get the right native call.
_NATIVE_DISPATCH: dict[str, Any] = {}


def _register(tool: str):
    def deco(fn):
        _NATIVE_DISPATCH[tool] = fn
        return fn
    return deco


@_register("amazon_pause_ad")
async def _t_pause_ad(args: dict) -> dict:
    return await update_product_ad_state(
        slug=args["slug"], ad_id=args["ad_id"], state="PAUSED",
    )


@_register("amazon_pause_keyword")
async def _t_pause_kw(args: dict) -> dict:
    return await update_keyword_state(
        slug=args["slug"], keyword_id=args["keyword_id"], state="PAUSED",
    )


@_register("amazon_pause_target")
async def _t_pause_target(args: dict) -> dict:
    return await update_target_state(
        slug=args["slug"], target_id=args["target_id"], state="PAUSED",
    )


@_register("amazon_pause_campaign")
async def _t_pause_camp(args: dict) -> dict:
    return await update_campaign_state(
        slug=args["slug"], campaign_id=args["campaign_id"], state="PAUSED",
    )


@_register("amazon_adjust_keyword_bid")
async def _t_kw_bid(args: dict) -> dict:
    return await update_keyword_bid(
        slug=args["slug"], keyword_id=args["keyword_id"],
        new_bid=float(args["new_bid"]),
    )


@_register("amazon_adjust_target_bid")
async def _t_target_bid(args: dict) -> dict:
    return await update_target_bid(
        slug=args["slug"], target_id=args["target_id"],
        new_bid=float(args["new_bid"]),
    )


@_register("amazon_raise_campaign_budget")
async def _t_camp_budget(args: dict) -> dict:
    return await update_campaign_budget(
        slug=args["slug"], campaign_id=args["campaign_id"],
        daily_budget=float(args["daily_budget"]),
    )


@_register("amazon_add_negative_keyword")
async def _t_neg(args: dict) -> dict:
    return await add_negative_keyword(
        slug=args["slug"],
        ad_group_id=args["ad_group_id"],
        campaign_id=args["campaign_id"],
        keyword_text=args["keyword_text"],
        match_type=args.get("match_type", "NEGATIVE_EXACT"),
    )


async def call_native(tool: str, args: dict) -> dict:
    """Drop-in replacement for `map.mcp_client.call_tool` on the WRITE path.

    args MUST include a `slug` (store identifier). The original MAP path
    used `integration_id` + `account_id`; native uses slug → profile_id
    via `profile_id_for()`.
    """
    handler = _NATIVE_DISPATCH.get(tool)
    if not handler:
        raise AmazonAdsError(f"unsupported native tool: {tool}")
    return await handler(args)
