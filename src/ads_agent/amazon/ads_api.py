"""Native Amazon Ads API client (LWA OAuth + Sponsored Products v3).

Replaces the MAP (Marketplace Ad Pros) proxy as our primary path for reading
Amazon Ads data. Token is stored in `ads_agent.amazon_oauth_tokens` (loaded
on first call, refreshed when expired). Profile ids are cached on the token
row at OAuth time.

Surface (mirrors MAP for drop-in replacement):

  - list_sp_campaigns(slug, state_filter='ENABLED') → list[dict]
  - list_resources(slug, resource_type, parent_id=None, ...) → (data, gated)
  - get_campaign_metrics(slug, campaign_ids, days) → list[dict]
  - ads_totals(slug, days) → dict

Region: today only EU (covers IN + AE + IE/PL/ES/UK). NA region (US/CA/MX)
needs a separate OAuth grant under different LWA scope.

Token refresh: lazy. The first call refreshes the access token if the
cached one is expired (1h TTL); subsequent calls in the same process
reuse the cached token.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import asyncpg
import httpx

from ads_agent.config import settings

log = logging.getLogger(__name__)


# ----- Region + endpoint config -------------------------------------------

_REGION_HOSTS = {
    "NA": "https://advertising-api.amazon.com",
    "EU": "https://advertising-api-eu.amazon.com",
    "FE": "https://advertising-api-fe.amazon.com",
}


def _client_id() -> str:
    v = os.environ.get("AMAZON_ADS_CLIENT_ID", "").strip()
    if not v:
        raise AmazonAdsError("AMAZON_ADS_CLIENT_ID missing")
    return v


def _client_secret() -> str:
    v = os.environ.get("AMAZON_ADS_CLIENT_SECRET", "").strip()
    if not v:
        raise AmazonAdsError("AMAZON_ADS_CLIENT_SECRET missing")
    return v


def _region() -> str:
    return os.environ.get("AMAZON_ADS_REGION", "EU").strip().upper()


def _host() -> str:
    return _REGION_HOSTS.get(_region(), _REGION_HOSTS["EU"])


# ----- Errors --------------------------------------------------------------

class AmazonAdsError(RuntimeError):
    """Raised when Amazon Ads API call fails non-recoverably."""


# Compatibility re-export so callers migrating from MAP keep working
class MapMcpError(AmazonAdsError):
    """DEPRECATED — alias for AmazonAdsError. The MAP proxy is gone."""


# ----- Profile mapping (slug → Amazon Ads profile id) ---------------------

# Built from the cached profile_ids in `amazon_oauth_tokens` + the
# AMAZON_ACCOUNTS_JSON config that maps slug → marketplace account_id.
_PROFILE_MAP_CACHE: dict[str, str] | None = None


async def _load_profile_map(pool: asyncpg.Pool) -> dict[str, str]:
    """Resolve store_slug → Amazon Ads profileId.

    Strategy:
      1. From the active `amazon_oauth_tokens` row, fetch the cached
         `profile_ids` (a JSONB array of profileId strings).
      2. For each profileId, query GET /v2/profiles/{id} to find the
         marketplaceStringId (e.g. A21TJRUUN4KGV for amazon.in).
      3. Cross-reference with AMAZON_ACCOUNTS_JSON env entries that
         have ds_id="ASELL" (Seller account_id == marketplace id).
      4. Build {slug: profileId} for every store with a Seller match.

    Cached at module level after first build; bust by setting
    _PROFILE_MAP_CACHE = None.
    """
    global _PROFILE_MAP_CACHE
    if _PROFILE_MAP_CACHE is not None:
        return _PROFILE_MAP_CACHE

    async with pool.acquire() as c:
        row = await c.fetchrow(
            """SELECT profile_ids FROM ads_agent.amazon_oauth_tokens
               WHERE revoked_at IS NULL ORDER BY created_at DESC LIMIT 1"""
        )
    if not row or not row["profile_ids"]:
        log.warning("amazon ads: no cached profile_ids on token row")
        _PROFILE_MAP_CACHE = {}
        return _PROFILE_MAP_CACHE

    profile_ids = json.loads(row["profile_ids"]) if isinstance(row["profile_ids"], str) else row["profile_ids"]

    # Pull each profile's full record (including marketplaceStringId)
    access = await _access_token(pool)
    headers = {
        "Amazon-Advertising-API-ClientId": _client_id(),
        "Authorization": f"Bearer {access}",
    }
    profile_to_marketplace: dict[str, str] = {}
    async with httpx.AsyncClient(timeout=30.0) as cli:
        r = await cli.get(f"{_host()}/v2/profiles", headers=headers)
        if r.status_code != 200:
            log.warning("amazon ads: /v2/profiles failed %s: %s", r.status_code, r.text[:200])
            _PROFILE_MAP_CACHE = {}
            return _PROFILE_MAP_CACHE
        for p in r.json():
            mp = (p.get("accountInfo") or {}).get("marketplaceStringId", "")
            if mp:
                profile_to_marketplace[str(p["profileId"])] = mp

    # Now invert AMAZON_ACCOUNTS_JSON: find each Seller account_id (which
    # IS the marketplace id) and link to the matching profile_id
    raw = os.environ.get("AMAZON_ACCOUNTS_JSON", "{}")
    try:
        accts_by_slug = json.loads(raw)
    except json.JSONDecodeError:
        accts_by_slug = {}
    out: dict[str, str] = {}
    for slug, accts in accts_by_slug.items():
        for a in accts or []:
            if a.get("ds_id") != "ASELL":
                continue
            marketplace = a.get("account_id", "")
            for pid, mp in profile_to_marketplace.items():
                if mp == marketplace:
                    out[slug] = pid
                    break
    log.info("amazon ads: profile map built — %d slugs mapped: %s", len(out), list(out))
    _PROFILE_MAP_CACHE = out
    return out


async def profile_id_for(slug: str, pool: asyncpg.Pool | None = None) -> str:
    """Return the Amazon Ads profile_id for a store slug.

    When AMAZON_USE_MAP=1, returns the profile_id from STORE_MAP_ACCOUNTS_JSON
    (or 'map:<account_id>' as a marker) so callers can confirm the slug is
    mapped without hitting the native LWA path.

    Raises AmazonAdsError if the store isn't mapped.
    """
    import json as _json
    import os as _os
    if _os.environ.get("AMAZON_USE_MAP", "").strip() == "1":
        raw = _os.environ.get("STORE_MAP_ACCOUNTS_JSON", "").strip()
        m = _json.loads(raw) if raw else {}
        cfg = m.get(slug) or {}
        if cfg.get("integration_id") and cfg.get("account_id"):
            # MAP doesn't expose Amazon profile_id directly via STORE_MAP_ACCOUNTS_JSON,
            # but the account_id is the durable identifier. Return it so existence
            # checks pass; downstream funcs that route through MAP don't need profile_id.
            return f"map:{cfg['account_id']}"
        # MAP enabled but slug not mapped — fall through to native error path

    if pool is None:
        pool = await _default_pool()
    pmap = await _load_profile_map(pool)
    pid = pmap.get(slug)
    if not pid:
        raise AmazonAdsError(
            f"no Amazon Ads profile mapped for slug {slug!r}; "
            f"available: {list(pmap)}. Re-run OAuth + profile cache "
            f"if a new market was added."
        )
    return pid


# ----- Access token (refresh on demand) -----------------------------------

_TOKEN_CACHE: dict[str, Any] = {"access_token": None, "expires_at": None}


async def _default_pool() -> asyncpg.Pool:
    """Lazy default pool — uses the RW DSN."""
    return await asyncpg.create_pool(
        settings().postgres_rw_dsn, min_size=1, max_size=2,
    )


async def _access_token(pool: asyncpg.Pool) -> str:
    """Get a valid access token. Refresh via LWA if the cached one is expired."""
    now = datetime.now(timezone.utc)
    cached = _TOKEN_CACHE.get("access_token")
    exp = _TOKEN_CACHE.get("expires_at")
    if cached and exp and exp - now > timedelta(minutes=2):
        return cached

    async with pool.acquire() as c:
        row = await c.fetchrow(
            """SELECT id, refresh_token FROM ads_agent.amazon_oauth_tokens
               WHERE revoked_at IS NULL ORDER BY created_at DESC LIMIT 1"""
        )
    if not row:
        raise AmazonAdsError(
            "no active row in ads_agent.amazon_oauth_tokens — "
            "complete the LWA OAuth flow first via /api/amazon/consent-url"
        )

    async with httpx.AsyncClient(timeout=30.0) as cli:
        r = await cli.post(
            "https://api.amazon.com/auth/o2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": row["refresh_token"],
                "client_id": _client_id(),
                "client_secret": _client_secret(),
            },
        )
    if r.status_code != 200:
        raise AmazonAdsError(f"LWA token refresh failed {r.status_code}: {r.text[:300]}")
    body = r.json()
    access = body.get("access_token")
    if not access:
        raise AmazonAdsError(f"LWA refresh: no access_token in response: {body}")
    expires_in = int(body.get("expires_in") or 3600)

    # Persist on the token row + cache in-process
    new_exp = now + timedelta(seconds=expires_in)
    async with pool.acquire() as c:
        await c.execute(
            """UPDATE ads_agent.amazon_oauth_tokens
               SET last_access_token=$1, last_access_token_expires_at=$2, updated_at=NOW()
               WHERE id=$3""",
            access, new_exp, row["id"],
        )
    _TOKEN_CACHE["access_token"] = access
    _TOKEN_CACHE["expires_at"] = new_exp
    log.info("amazon ads: access token refreshed (expires %s)", new_exp.isoformat())
    return access


# ----- Core HTTP helpers ---------------------------------------------------

async def _post(
    path: str, *, profile_id: str, json_body: dict,
    accept: str, content_type: str | None = None,
    pool: asyncpg.Pool | None = None,
) -> dict:
    """POST to Amazon Ads API. Returns parsed JSON. Raises AmazonAdsError on non-2xx."""
    if pool is None:
        pool = await _default_pool()
    access = await _access_token(pool)
    headers = {
        "Amazon-Advertising-API-ClientId": _client_id(),
        "Amazon-Advertising-API-Scope": str(profile_id),
        "Authorization": f"Bearer {access}",
        "Accept": accept,
        "Content-Type": content_type or accept,
    }
    async with httpx.AsyncClient(timeout=60.0) as cli:
        r = await cli.post(f"{_host()}{path}", headers=headers, json=json_body)
    if r.status_code >= 400:
        raise AmazonAdsError(
            f"POST {path} HTTP {r.status_code}: {r.text[:400]}"
        )
    return r.json()


async def _get(
    path: str, *, profile_id: str, accept: str = "application/json",
    pool: asyncpg.Pool | None = None,
) -> dict:
    if pool is None:
        pool = await _default_pool()
    access = await _access_token(pool)
    headers = {
        "Amazon-Advertising-API-ClientId": _client_id(),
        "Amazon-Advertising-API-Scope": str(profile_id),
        "Authorization": f"Bearer {access}",
        "Accept": accept,
    }
    async with httpx.AsyncClient(timeout=60.0) as cli:
        r = await cli.get(f"{_host()}{path}", headers=headers)
    if r.status_code >= 400:
        raise AmazonAdsError(f"GET {path} HTTP {r.status_code}: {r.text[:400]}")
    return r.json()


# ----- SP read endpoints ---------------------------------------------------

_SP_CAMPAIGN = "application/vnd.spCampaign.v3+json"
_SP_KW       = "application/vnd.spKeyword.v3+json"
_SP_TARGET   = "application/vnd.spTargetingClause.v3+json"
_SP_PA       = "application/vnd.spProductAd.v3+json"
_SP_NEGKW    = "application/vnd.spNegativeKeyword.v3+json"
_SP_ADGROUP  = "application/vnd.spAdGroup.v3+json"


async def list_sp_campaigns(
    slug: str, state_filter: str = "ENABLED",
) -> list[dict]:
    """Return list of SP campaigns for a store, filtered by state.

    Output shape mirrors MAP's:
      [{campaignId, name, state, dynamicBidding, targetingType, budget, ...}, ...]
    """
    # MAP routing — see ads_totals docstring
    import json as _json
    import os as _os
    if _os.environ.get("AMAZON_USE_MAP", "").strip() == "1":
        raw = _os.environ.get("STORE_MAP_ACCOUNTS_JSON", "").strip()
        m = _json.loads(raw) if raw else {}
        cfg = m.get(slug) or {}
        if cfg.get("integration_id") and cfg.get("account_id"):
            from ads_agent.map.mcp_client import list_sp_campaigns as _map_list
            return await _map_list(cfg["integration_id"], cfg["account_id"], state_filter=state_filter)

    pool = await _default_pool()
    pid = await profile_id_for(slug, pool)
    body: dict[str, Any] = {"maxResults": 500}
    if state_filter:
        body["stateFilter"] = {"include": [state_filter]}
    out: list[dict] = []
    next_token: str | None = None
    while True:
        if next_token:
            body["nextToken"] = next_token
        data = await _post("/sp/campaigns/list", profile_id=pid, json_body=body,
                           accept=_SP_CAMPAIGN, pool=pool)
        out.extend(data.get("campaigns") or [])
        next_token = data.get("nextToken")
        if not next_token:
            break
    return out


async def list_sp_ad_groups(slug: str, campaign_ids: list[str] | None = None) -> list[dict]:
    pool = await _default_pool()
    pid = await profile_id_for(slug, pool)
    body: dict[str, Any] = {"maxResults": 500}
    if campaign_ids:
        body["campaignIdFilter"] = {"include": [str(x) for x in campaign_ids]}
    data = await _post("/sp/adGroups/list", profile_id=pid, json_body=body,
                       accept=_SP_ADGROUP, pool=pool)
    return data.get("adGroups") or []


async def list_sp_keywords(slug: str, campaign_ids: list[str] | None = None,
                           ad_group_ids: list[str] | None = None) -> list[dict]:
    pool = await _default_pool()
    pid = await profile_id_for(slug, pool)
    body: dict[str, Any] = {"maxResults": 500}
    if campaign_ids:
        body["campaignIdFilter"] = {"include": [str(x) for x in campaign_ids]}
    if ad_group_ids:
        body["adGroupIdFilter"] = {"include": [str(x) for x in ad_group_ids]}
    data = await _post("/sp/keywords/list", profile_id=pid, json_body=body,
                       accept=_SP_KW, pool=pool)
    return data.get("keywords") or []


async def list_sp_targets(slug: str, campaign_ids: list[str] | None = None,
                          ad_group_ids: list[str] | None = None) -> list[dict]:
    pool = await _default_pool()
    pid = await profile_id_for(slug, pool)
    body: dict[str, Any] = {"maxResults": 500}
    if campaign_ids:
        body["campaignIdFilter"] = {"include": [str(x) for x in campaign_ids]}
    if ad_group_ids:
        body["adGroupIdFilter"] = {"include": [str(x) for x in ad_group_ids]}
    data = await _post("/sp/targets/list", profile_id=pid, json_body=body,
                       accept=_SP_TARGET, pool=pool)
    return data.get("targetingClauses") or []


async def list_sp_product_ads(slug: str, campaign_ids: list[str] | None = None,
                              ad_group_ids: list[str] | None = None) -> list[dict]:
    pool = await _default_pool()
    pid = await profile_id_for(slug, pool)
    body: dict[str, Any] = {"maxResults": 500}
    if campaign_ids:
        body["campaignIdFilter"] = {"include": [str(x) for x in campaign_ids]}
    if ad_group_ids:
        body["adGroupIdFilter"] = {"include": [str(x) for x in ad_group_ids]}
    data = await _post("/sp/productAds/list", profile_id=pid, json_body=body,
                       accept=_SP_PA, pool=pool)
    return data.get("productAds") or []


async def list_sp_negative_keywords(slug: str, campaign_ids: list[str] | None = None) -> list[dict]:
    pool = await _default_pool()
    pid = await profile_id_for(slug, pool)
    body: dict[str, Any] = {"maxResults": 500}
    if campaign_ids:
        body["campaignIdFilter"] = {"include": [str(x) for x in campaign_ids]}
    data = await _post("/sp/negativeKeywords/list", profile_id=pid, json_body=body,
                       accept=_SP_NEGKW, pool=pool)
    return data.get("negativeKeywords") or []


# ----- MAP-compatible `list_resources` shim (drop-in for callers) ---------

async def list_resources(
    slug: str, resource_type: str,
    *, campaign_id: str | None = None, ad_group_id: str | None = None,
    state_filter: str = "ENABLED",
) -> tuple[Any, bool]:
    """Drop-in replacement for `map.mcp_client.call_tool("list_resources", ...)`.

    Returns (data, gated) where:
      - data is {"items": [...]} on success
      - gated is False (native API has no plan-gating; always available)

    resource_type ∈ {sp_campaigns, sp_ad_groups, sp_keywords, sp_product_targets,
                     sp_product_ads, sp_negative_keywords}
    """
    cids = [campaign_id] if campaign_id else None
    agids = [ad_group_id] if ad_group_id else None

    if resource_type == "sp_campaigns":
        items = await list_sp_campaigns(slug, state_filter=state_filter)
    elif resource_type == "sp_ad_groups":
        items = await list_sp_ad_groups(slug, campaign_ids=cids)
    elif resource_type == "sp_keywords":
        items = await list_sp_keywords(slug, campaign_ids=cids, ad_group_ids=agids)
    elif resource_type in ("sp_product_targets", "sp_targets"):
        items = await list_sp_targets(slug, campaign_ids=cids, ad_group_ids=agids)
    elif resource_type == "sp_product_ads":
        items = await list_sp_product_ads(slug, campaign_ids=cids, ad_group_ids=agids)
    elif resource_type == "sp_negative_keywords":
        items = await list_sp_negative_keywords(slug, campaign_ids=cids)
    else:
        raise AmazonAdsError(f"unknown resource_type: {resource_type}")
    return {"items": items}, False


# ----- Reports v3 (per-campaign metrics) -----------------------------------

async def get_campaign_metrics(
    slug: str, days: int = 14,
    *, group_by: str = "campaign",
) -> list[dict]:
    """Trigger an SP campaigns Reports v3 report and poll until ready.

    Returns one row per campaign with
      campaignId, impressions, clicks, cost, sales1d, purchases1d, etc.

    Reports are async on Amazon's side: create → poll status → download
    JSON. Typical wall time 30-90s for a 14-day SP campaign report on
    a small account.
    """
    pool = await _default_pool()
    pid = await profile_id_for(slug, pool)
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days - 1)
    # Note: spCampaigns SUMMARY supports `sales1d/7d/14d` and `purchases1d/7d/14d`
    # but NOT `acosClicks1d` / `roasClicks1d` — those are only on
    # spTargeting / spAdvertisedProduct. We compute ROAS in Python instead.
    body = {
        "name": f"sp-campaigns-{slug}-{start}-{end}",
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "configuration": {
            "adProduct": "SPONSORED_PRODUCTS",
            "groupBy": [group_by],
            "columns": [
                "campaignId", "campaignName", "campaignBiddingStrategy",
                "impressions", "clicks", "cost",
                "sales1d", "purchases1d", "unitsSoldClicks1d",
            ],
            "reportTypeId": "spCampaigns",
            "timeUnit": "SUMMARY",
            "format": "GZIP_JSON",
        },
    }
    accept = "application/vnd.createasyncreportrequest.v3+json"
    create = await _post("/reporting/reports", profile_id=pid, json_body=body,
                         accept=accept, content_type=accept, pool=pool)
    report_id = create.get("reportId")
    if not report_id:
        raise AmazonAdsError(f"reports/create returned no reportId: {create}")

    # Poll
    for i in range(200):  # 200 × 3s = 10 min cap                # up to 6 min total
        await asyncio.sleep(3)
        status = await _get(f"/reporting/reports/{report_id}", profile_id=pid,
                            accept="application/vnd.createasyncreportrequest.v3+json",
                            pool=pool)
        st = status.get("status")
        if st == "COMPLETED":
            url = status.get("url")
            break
        if st in ("FAILED", "CANCELLED"):
            raise AmazonAdsError(f"report {report_id} status={st}: {status}")
    else:
        raise AmazonAdsError(f"report {report_id} timed out after polling")

    # Download + decompress + parse
    import gzip
    async with httpx.AsyncClient(timeout=120.0) as cli:
        r = await cli.get(url)
    if r.status_code >= 400:
        raise AmazonAdsError(f"report download HTTP {r.status_code}")
    raw = gzip.decompress(r.content)
    return json.loads(raw)


async def get_keyword_metrics(slug: str, days: int = 14) -> list[dict]:
    """Per-keyword metrics via Reports v3 (spTargeting / SUMMARY).

    Note: spTargeting uses `keyword` (not `keywordText`) for the text
    field; ROAS column is `roasClicks14d` (no `roasClicks1d` exists for
    this report type). Returns one row per (keywordId | targetId).
    Caller distinguishes keywords from product targets by `keyword` /
    `targeting` field presence.
    """
    return await _run_report(slug, days, group_by="targeting", report_type="spTargeting", columns=[
        "keywordId", "keyword", "matchType", "adGroupId", "campaignId",
        "campaignStatus", "impressions", "clicks", "cost",
        "sales1d", "sales7d", "sales14d",
        "purchases1d", "purchases7d", "purchases14d",
        "unitsSoldClicks1d", "roasClicks14d",
    ])


async def get_target_metrics(slug: str, days: int = 14) -> list[dict]:
    """Per-product-target metrics. spTargeting report covers both keywords
    AND product targets; caller filters by `targeting` field presence."""
    return await _run_report(slug, days, group_by="targeting", report_type="spTargeting", columns=[
        "targetId", "targeting", "matchType", "adGroupId", "campaignId",
        "campaignStatus", "impressions", "clicks", "cost",
        "sales1d", "sales7d", "sales14d",
        "purchases1d", "purchases7d", "purchases14d",
        "unitsSoldClicks1d", "roasClicks14d",
    ])


async def get_ad_metrics(slug: str, days: int = 14) -> list[dict]:
    """Per-product-ad (advertised ASIN) metrics via Reports v3
    (spAdvertisedProduct / SUMMARY). Same caveat as keywords: 14d ROAS
    column is `roasClicks14d`, not `roasClicks1d`."""
    return await _run_report(slug, days, group_by="advertiser", report_type="spAdvertisedProduct", columns=[
        "advertisedAsin", "advertisedSku", "adId", "adGroupId", "campaignId",
        "campaignStatus", "impressions", "clicks", "cost",
        "sales1d", "sales7d", "sales14d",
        "purchases1d", "purchases7d", "purchases14d",
        "unitsSoldClicks1d", "roasClicks14d",
    ])


async def _run_report(
    slug: str, days: int, *, group_by: str, report_type: str, columns: list[str],
) -> list[dict]:
    """Generic Reports v3 runner. Async report — creates, polls, downloads."""
    pool = await _default_pool()
    pid = await profile_id_for(slug, pool)
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days - 1)
    body = {
        "name": f"{report_type}-{slug}-{start}-{end}",
        "startDate": start.isoformat(),
        "endDate": end.isoformat(),
        "configuration": {
            "adProduct": "SPONSORED_PRODUCTS",
            "groupBy": [group_by],
            "columns": columns,
            "reportTypeId": report_type,
            "timeUnit": "SUMMARY",
            "format": "GZIP_JSON",
        },
    }
    accept = "application/vnd.createasyncreportrequest.v3+json"
    create = await _post("/reporting/reports", profile_id=pid, json_body=body,
                         accept=accept, content_type=accept, pool=pool)
    report_id = create.get("reportId")
    if not report_id:
        raise AmazonAdsError(f"reports/create returned no reportId: {create}")
    for i in range(200):  # 200 × 3s = 10 min cap  # 6 min cap
        await asyncio.sleep(3)
        status = await _get(f"/reporting/reports/{report_id}", profile_id=pid,
                            accept="application/vnd.createasyncreportrequest.v3+json",
                            pool=pool)
        st = status.get("status")
        if st == "COMPLETED":
            url = status.get("url")
            break
        if st in ("FAILED", "CANCELLED"):
            raise AmazonAdsError(f"report {report_id} status={st}: {status}")
    else:
        raise AmazonAdsError(f"report {report_id} timed out (>3min)")
    import gzip
    async with httpx.AsyncClient(timeout=120.0) as cli:
        r = await cli.get(url)
    if r.status_code >= 400:
        raise AmazonAdsError(f"report download HTTP {r.status_code}")
    raw = gzip.decompress(r.content)
    return json.loads(raw)


async def ads_totals(slug: str, days: int = 14) -> dict[str, Any]:
    """Account-level totals across all SP campaigns. Wraps get_campaign_metrics.

    When AMAZON_USE_MAP=1, routes via MAP (Marketplace Ad Pros) MCP instead
    of native LWA. Used as a temporary path while LWA refresh is broken,
    or as a fallback when partner-tier app approval is pending.
    """
    import json as _json
    import os as _os
    if _os.environ.get("AMAZON_USE_MAP", "").strip() == "1":
        raw = _os.environ.get("STORE_MAP_ACCOUNTS_JSON", "").strip()
        m = _json.loads(raw) if raw else {}
        cfg = m.get(slug) or {}
        if cfg.get("integration_id") and cfg.get("account_id"):
            from ads_agent.map.mcp_client import ads_totals as _map_totals
            t = await _map_totals(cfg["integration_id"], cfg["account_id"], days)
            cost = float(t.get("cost") or 0)
            sales = float(t.get("sales14d") or 0)
            return {
                "spend": cost, "sales14d": sales,
                "purchases14d": int(t.get("purchases14d") or 0),
                "clicks": int(t.get("clicks") or 0),
                "impressions": int(t.get("impressions") or 0),
                "roas": (sales / cost) if cost > 0 else 0.0,
                "n_campaigns": 0,
                "currency": t.get("currency", ""),
                "_source": "map",
            }
        # missing MAP config — fall through to native (will raise if also broken)
        log.warning("AMAZON_USE_MAP=1 but no STORE_MAP_ACCOUNTS_JSON entry for %s — falling back to native", slug)

    rows = await get_campaign_metrics(slug, days=days)
    spend = sum(float(r.get("cost", 0) or 0) for r in rows)
    sales = sum(float(r.get("sales1d", 0) or 0) for r in rows)
    purch = sum(int(r.get("purchases1d", 0) or 0) for r in rows)
    clicks = sum(int(r.get("clicks", 0) or 0) for r in rows)
    imp = sum(int(r.get("impressions", 0) or 0) for r in rows)
    return {
        "spend": spend, "sales14d": sales, "purchases14d": purch,
        "clicks": clicks, "impressions": imp,
        "roas": (sales / spend) if spend > 0 else 0.0,
        "n_campaigns": len(rows),
        "_source": "amazon_ads_native",
    }


def reset_caches() -> None:
    """For tests + after re-OAuth: clear in-process token + profile caches."""
    global _PROFILE_MAP_CACHE
    _PROFILE_MAP_CACHE = None
    _TOKEN_CACHE["access_token"] = None
    _TOKEN_CACHE["expires_at"] = None
