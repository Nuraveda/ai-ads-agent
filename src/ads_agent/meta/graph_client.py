"""Direct Meta Marketing API (Graph API) client.

Uses META_ACCESS_TOKEN directly. Cleaner + faster for our read-heavy workload
than going through MCP protocol wrapping.

Scope: just the 3-4 endpoints the agent actually needs:
  - /{act_id}/insights        — spend + impressions + clicks + conversions per date range
  - /{act_id}                 — account metadata (name, currency, status)
"""
from __future__ import annotations

import httpx

from ads_agent.config import settings

GRAPH_BASE = "https://graph.facebook.com/v21.0"


class MetaGraphError(RuntimeError):
    pass


async def _get(path: str, params: dict) -> dict:
    token = settings().meta_access_token
    if not token:
        raise MetaGraphError("META_ACCESS_TOKEN not set")
    params = {**params, "access_token": token}
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.get(f"{GRAPH_BASE}/{path}", params=params)
    body = r.json()
    if r.status_code != 200 or body.get("error"):
        raise MetaGraphError(f"meta {path}: {body.get('error') or r.text[:300]}")
    return body


async def account_spend(ad_account_id: str, days: int = 7) -> dict:
    """Return {spend, impressions, clicks, purchases, purchase_value, currency}
    for the past N days (inclusive of today)."""
    params = {
        "level": "account",
        "fields": "spend,impressions,clicks,actions,action_values,account_currency",
        "date_preset": f"last_{days}_d" if days in (7, 14, 28, 30, 90) else None,
    }
    # For arbitrary windows, use time_range instead of date_preset
    if params["date_preset"] is None:
        from datetime import datetime, timedelta, timezone
        end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        params.pop("date_preset")
        params["time_range"] = f'{{"since":"{start}","until":"{end}"}}'
    else:
        # Canonical preset names: last_7d, last_14d, last_28d, last_30d, last_90d
        params["date_preset"] = f"last_{days}d"

    body = await _get(f"{ad_account_id}/insights", params)
    rows = body.get("data", [])
    if not rows:
        return {"spend": 0.0, "impressions": 0, "clicks": 0, "purchases": 0, "purchase_value": 0.0, "currency": "?"}
    row = rows[0]

    # Use omni_purchase ONLY — Meta's canonical dedup action type (matches
    # Ads Manager's "Purchase ROAS" column). The other aliases (purchase,
    # fb_pixel_purchase, onsite_web_*_purchase) represent the same events
    # viewed through different attribution lenses; summing them 3-5× inflates
    # the ROAS number. See PURCHASE_ACTION_TYPES above for full rationale.
    purchases = 0
    purchase_value = 0.0
    for a in row.get("actions", []) or []:
        if a.get("action_type") == "omni_purchase":
            purchases += int(a.get("value", 0) or 0)
    for av in row.get("action_values", []) or []:
        if av.get("action_type") == "omni_purchase":
            purchase_value += float(av.get("value", 0) or 0)

    return {
        "spend": float(row.get("spend", 0) or 0),
        "impressions": int(row.get("impressions", 0) or 0),
        "clicks": int(row.get("clicks", 0) or 0),
        "purchases": purchases,
        "purchase_value": purchase_value,
        "currency": row.get("account_currency", "?"),
    }


async def account_info(ad_account_id: str) -> dict:
    body = await _get(ad_account_id, {"fields": "name,currency,account_status,amount_spent"})
    return body


# ---------------------------------------------------------------------------
# Ad-level insights + creative fetch (v1 /ads /creative /ideas substrate)
# ---------------------------------------------------------------------------

# Meta returns the same purchase event under multiple `action_type` aliases —
# `purchase`, `omni_purchase`, `offsite_conversion.fb_pixel_purchase`,
# `onsite_web_purchase`, `onsite_web_app_purchase` — all representing the
# same underlying conversions. Summing across them double-counts by 3-5×.
#
# `omni_purchase` is Meta's canonical cross-device deduplicated parent
# metric and is the same number shown in Ads Manager's "Purchase ROAS"
# column. Use ONLY that.
#
# Confirmed 2026-04-20 via raw API call against act_654879327196107:
#   purchase_roas.omni_purchase = 1.22×  (matches dashboard)
#   summing all 5 aliases = 3.67×         (wrong — earlier bug in this code)
PURCHASE_ACTION_TYPES = {
    "omni_purchase",
}


def _sum_purchases(row: dict) -> tuple[int, float]:
    purchases = 0
    purchase_value = 0.0
    for a in row.get("actions", []) or []:
        if a.get("action_type") in PURCHASE_ACTION_TYPES:
            purchases += int(a.get("value", 0) or 0)
    for av in row.get("action_values", []) or []:
        if av.get("action_type") in PURCHASE_ACTION_TYPES:
            purchase_value += float(av.get("value", 0) or 0)
    return purchases, purchase_value


async def _paginate_get(path: str, base_params: dict, max_rows: int = 1000) -> list[dict]:
    """GET `path` with cursor pagination until exhausted or max_rows."""
    out: list[dict] = []
    params = dict(base_params)
    cursor: str | None = None
    while True:
        if cursor:
            params["after"] = cursor
        body = await _get(path, params)
        out.extend(body.get("data", []))
        nxt = (body.get("paging") or {}).get("cursors", {}).get("after")
        if not nxt or len(out) >= max_rows:
            break
        cursor = nxt
    return out


async def ads_for_account(ad_account_id: str, days: int = 7, limit: int = 200) -> list[dict]:
    """Return joined ad metadata + ad-level insights for every ad in this account.

    One row per ad, metrics over last N days. Paginates both the /ads
    metadata endpoint and the /insights endpoint — Meta chokes on large
    accounts with ~>150 ads in a single page when creative{…} is included,
    so the per-page cap is 50 here.
    """
    # 1) ad metadata + creative (heavy payload → small page size)
    ads_rows = await _paginate_get(
        f"{ad_account_id}/ads",
        {
            "fields": "id,name,status,effective_status,created_time,creative{id,thumbnail_url,body,title,object_type,video_id}",
            "limit": 50,
        },
        max_rows=limit,
    )
    ad_by_id: dict[str, dict] = {a["id"]: a for a in ads_rows}

    # 2) insights at ad level (paginated; lighter fields → 100 per page)
    if days in (7, 14, 28, 30, 90):
        time_params = {"date_preset": f"last_{days}d"}
    else:
        from datetime import datetime, timedelta, timezone
        end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        time_params = {"time_range": f'{{"since":"{start}","until":"{end}"}}'}

    insight_rows = await _paginate_get(
        f"{ad_account_id}/insights",
        {
            "level": "ad",
            "fields": "ad_id,ad_name,spend,impressions,clicks,ctr,cpc,cpm,frequency,reach,actions,action_values,account_currency",
            "limit": 100,
            **time_params,
        },
        max_rows=limit,
    )
    insights_body = {"data": insight_rows}

    results: list[dict] = []
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    for row in insights_body.get("data", []):
        ad_id = row.get("ad_id")
        ad = ad_by_id.get(ad_id, {})
        purchases, purchase_value = _sum_purchases(row)
        spend = float(row.get("spend", 0) or 0)

        # Parse created_time (ISO 8601 with TZ) → days live
        days_live: float = 0.0
        created = ad.get("created_time")
        if created:
            try:
                ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
                days_live = max(0.0, (now - ts).total_seconds() / 86400.0)
            except Exception:
                pass

        results.append({
            "ad_account_id": ad_account_id,
            "ad_id": ad_id,
            "ad_name": ad.get("name") or row.get("ad_name", ""),
            "status": ad.get("status", ""),
            "effective_status": ad.get("effective_status", ""),
            "creative": ad.get("creative", {}),
            "created_time": created or "",
            "days_live": days_live,
            "spend": spend,
            "currency": row.get("account_currency", "?"),
            "impressions": int(row.get("impressions", 0) or 0),
            "clicks": int(row.get("clicks", 0) or 0),
            "ctr": float(row.get("ctr", 0) or 0),
            "cpc": float(row.get("cpc", 0) or 0),
            "cpm": float(row.get("cpm", 0) or 0),
            "frequency": float(row.get("frequency", 0) or 0),
            "reach": int(row.get("reach", 0) or 0),
            "purchases": purchases,
            "purchase_value": purchase_value,
            "reported_roas": (purchase_value / spend) if spend > 0 else 0.0,
        })
    return results


async def creative_details(ad_id: str, days: int = 7) -> dict:
    """Return one ad's creative + last-N-days metrics (for /creative vision critique)."""
    ad_info = await _get(
        ad_id,
        {"fields": "id,name,status,effective_status,adset_id,campaign_id,"
                   "creative{id,thumbnail_url,body,title,video_id,image_url,object_type,object_story_spec,effective_object_story_id}"},
    )

    if days in (7, 14, 28, 30, 90):
        time_params = {"date_preset": f"last_{days}d"}
    else:
        from datetime import datetime, timedelta, timezone
        end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
        time_params = {"time_range": f'{{"since":"{start}","until":"{end}"}}'}

    insights_body = await _get(
        f"{ad_id}/insights",
        {
            "fields": "spend,impressions,clicks,ctr,cpc,cpm,frequency,reach,actions,action_values,account_currency",
            **time_params,
        },
    )
    rows = insights_body.get("data", [])
    m = rows[0] if rows else {}
    purchases, purchase_value = _sum_purchases(m)
    spend = float(m.get("spend", 0) or 0)

    return {
        "ad_id": ad_id,
        "ad_name": ad_info.get("name", ""),
        "status": ad_info.get("status", ""),
        "effective_status": ad_info.get("effective_status", ""),
        "creative": ad_info.get("creative", {}),
        "currency": m.get("account_currency", "?"),
        "spend": spend,
        "impressions": int(m.get("impressions", 0) or 0),
        "clicks": int(m.get("clicks", 0) or 0),
        "ctr": float(m.get("ctr", 0) or 0),
        "cpc": float(m.get("cpc", 0) or 0),
        "cpm": float(m.get("cpm", 0) or 0),
        "frequency": float(m.get("frequency", 0) or 0),
        "reach": int(m.get("reach", 0) or 0),
        "purchases": purchases,
        "purchase_value": purchase_value,
        "reported_roas": (purchase_value / spend) if spend > 0 else 0.0,
    }


# ---------------------------------------------------------------------------
# Meta audit hierarchy pulls — campaign → adset → ad (joined with insights)
# ---------------------------------------------------------------------------

def _time_params(days: int) -> dict:
    if days in (7, 14, 28, 30, 90):
        return {"date_preset": f"last_{days}d"}
    from datetime import datetime, timedelta, timezone
    end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")
    return {"time_range": f'{{"since":"{start}","until":"{end}"}}'}


def _is_asc_plus(campaign_meta: dict) -> bool:
    """Heuristic: Advantage+ Shopping Campaigns carry smart_promotion_type,
    AUTOMATED_SHOPPING_ADS objective or `is_smart_promotion=True` flag, or
    the special name prefix Meta uses on creation ("Advantage+ "). We
    merge signals so the check works across account vintages."""
    name = (campaign_meta.get("name") or "").lower()
    if "advantage+" in name or name.startswith("asc "):
        return True
    if campaign_meta.get("smart_promotion_type"):
        return True
    obj = (campaign_meta.get("objective") or "").upper()
    # Meta returns OUTCOME_SALES for manual sales campaigns too; the
    # objective alone isn't enough. Keep as false-positive guard only.
    return obj in {"AUTOMATED_SHOPPING_ADS"}


async def campaigns_for_account(
    ad_account_id: str, days: int = 14, limit: int = 200,
) -> list[dict]:
    """Return joined campaign metadata + campaign-level insights.

    One row per campaign, metrics over last N days. Includes objective,
    buying_type, budget, daily_budget, start/end time, and an `is_asc_plus`
    flag derived from Meta's smart_promotion_type.
    """
    meta_body = await _get(
        f"{ad_account_id}/campaigns",
        {
            "fields": "id,name,status,effective_status,objective,buying_type,"
                      "daily_budget,lifetime_budget,start_time,stop_time,"
                      "smart_promotion_type,special_ad_categories",
            "limit": limit,
        },
    )
    camp_by_id: dict[str, dict] = {c["id"]: c for c in meta_body.get("data", [])}

    insights_body = await _get(
        f"{ad_account_id}/insights",
        {
            "level": "campaign",
            "fields": "campaign_id,campaign_name,spend,impressions,clicks,ctr,"
                      "cpc,cpm,frequency,reach,actions,action_values,account_currency",
            "limit": limit,
            **_time_params(days),
        },
    )

    out: list[dict] = []
    for row in insights_body.get("data", []):
        cid = row.get("campaign_id")
        c = camp_by_id.get(cid, {})
        purchases, purchase_value = _sum_purchases(row)
        spend = float(row.get("spend", 0) or 0)
        daily = float(c.get("daily_budget") or 0) / 100.0  # Meta cents → currency
        out.append({
            "campaign_id": cid,
            "name": c.get("name") or row.get("campaign_name", ""),
            "status": c.get("status", ""),
            "effective_status": c.get("effective_status", ""),
            "objective": c.get("objective", ""),
            "buying_type": c.get("buying_type", ""),
            "is_asc_plus": _is_asc_plus(c),
            "daily_budget": daily,
            "lifetime_budget": float(c.get("lifetime_budget") or 0) / 100.0,
            "currency": row.get("account_currency", "?"),
            "spend": spend,
            "impressions": int(row.get("impressions", 0) or 0),
            "clicks": int(row.get("clicks", 0) or 0),
            "ctr": float(row.get("ctr", 0) or 0),
            "cpc": float(row.get("cpc", 0) or 0),
            "cpm": float(row.get("cpm", 0) or 0),
            "frequency": float(row.get("frequency", 0) or 0),
            "reach": int(row.get("reach", 0) or 0),
            "purchases": purchases,
            "purchase_value": purchase_value,
            "roas": (purchase_value / spend) if spend > 0 else 0.0,
        })
    # Surface DISABLED / PAUSED campaigns with zero 14d spend that insights
    # won't return — the audit still wants to see them listed.
    returned_ids = {r["campaign_id"] for r in out}
    for cid, c in camp_by_id.items():
        if cid in returned_ids:
            continue
        out.append({
            "campaign_id": cid,
            "name": c.get("name", ""),
            "status": c.get("status", ""),
            "effective_status": c.get("effective_status", ""),
            "objective": c.get("objective", ""),
            "buying_type": c.get("buying_type", ""),
            "is_asc_plus": _is_asc_plus(c),
            "daily_budget": float(c.get("daily_budget") or 0) / 100.0,
            "lifetime_budget": float(c.get("lifetime_budget") or 0) / 100.0,
            "currency": "?", "spend": 0.0, "impressions": 0, "clicks": 0,
            "ctr": 0.0, "cpc": 0.0, "cpm": 0.0, "frequency": 0.0, "reach": 0,
            "purchases": 0, "purchase_value": 0.0, "roas": 0.0,
        })
    return out


async def adsets_for_account(
    ad_account_id: str, days: int = 14, limit: int = 500,
) -> list[dict]:
    """One row per ad set with campaign_id parent + 14d insights."""
    meta_body = await _get(
        f"{ad_account_id}/adsets",
        {
            "fields": "id,name,status,effective_status,campaign_id,"
                      "daily_budget,lifetime_budget,optimization_goal,"
                      "billing_event,bid_strategy,targeting",
            "limit": limit,
        },
    )
    as_by_id: dict[str, dict] = {a["id"]: a for a in meta_body.get("data", [])}

    insights_body = await _get(
        f"{ad_account_id}/insights",
        {
            "level": "adset",
            "fields": "adset_id,adset_name,campaign_id,spend,impressions,clicks,"
                      "ctr,cpc,cpm,frequency,reach,actions,action_values",
            "limit": limit,
            **_time_params(days),
        },
    )
    out: list[dict] = []
    for row in insights_body.get("data", []):
        aid = row.get("adset_id")
        a = as_by_id.get(aid, {})
        purchases, purchase_value = _sum_purchases(row)
        spend = float(row.get("spend", 0) or 0)
        out.append({
            "adset_id": aid,
            "campaign_id": row.get("campaign_id") or a.get("campaign_id", ""),
            "name": a.get("name") or row.get("adset_name", ""),
            "status": a.get("status", ""),
            "effective_status": a.get("effective_status", ""),
            "optimization_goal": a.get("optimization_goal", ""),
            "billing_event": a.get("billing_event", ""),
            "bid_strategy": a.get("bid_strategy", ""),
            "daily_budget": float(a.get("daily_budget") or 0) / 100.0,
            "spend": spend,
            "impressions": int(row.get("impressions", 0) or 0),
            "clicks": int(row.get("clicks", 0) or 0),
            "ctr": float(row.get("ctr", 0) or 0),
            "cpc": float(row.get("cpc", 0) or 0),
            "cpm": float(row.get("cpm", 0) or 0),
            "frequency": float(row.get("frequency", 0) or 0),
            "reach": int(row.get("reach", 0) or 0),
            "purchases": purchases,
            "purchase_value": purchase_value,
            "roas": (purchase_value / spend) if spend > 0 else 0.0,
        })
    # Append PAUSED/ARCHIVED ad sets that had no 14d spend
    seen = {r["adset_id"] for r in out}
    for aid, a in as_by_id.items():
        if aid in seen:
            continue
        out.append({
            "adset_id": aid,
            "campaign_id": a.get("campaign_id", ""),
            "name": a.get("name", ""),
            "status": a.get("status", ""),
            "effective_status": a.get("effective_status", ""),
            "optimization_goal": a.get("optimization_goal", ""),
            "billing_event": a.get("billing_event", ""),
            "bid_strategy": a.get("bid_strategy", ""),
            "daily_budget": float(a.get("daily_budget") or 0) / 100.0,
            "spend": 0.0, "impressions": 0, "clicks": 0, "ctr": 0.0, "cpc": 0.0,
            "cpm": 0.0, "frequency": 0.0, "reach": 0, "purchases": 0,
            "purchase_value": 0.0, "roas": 0.0,
        })
    return out


async def ad_destinations_for_account(ad_account_id: str, limit: int = 500) -> dict[str, str]:
    """One slim call to /{ad_account}/ads to grab every ad's destination URL.

    Returned dict: {ad_id: destination_url_or_empty}. Used by the audit
    decomposer to tag each AdRow with destination + ASIN. Single call,
    creative subfields only — much cheaper than the full /ads pull that
    triggered "reduce data" 400s before.
    """
    out: dict[str, str] = {}
    cursor_after: str | None = None
    while True:
        params = {
            "fields": "id,creative{object_url,object_story_spec{"
                      "video_data{call_to_action{value{link}}},"
                      "link_data{link}},asset_feed_spec{link_urls}}",
            "limit": min(limit, 100),
        }
        if cursor_after:
            params["after"] = cursor_after
        body = await _get(f"{ad_account_id}/ads", params)
        from ads_agent.meta.destinations import extract_destination_link
        for row in body.get("data", []):
            link = extract_destination_link(row.get("creative") or {})
            out[str(row.get("id", ""))] = link or ""
        nxt = (body.get("paging") or {}).get("cursors", {}).get("after")
        if not nxt or len(out) >= limit:
            break
        cursor_after = nxt
    return out


async def ads_for_account_lean(
    ad_account_id: str, days: int = 14, limit: int = 500,
) -> list[dict]:
    """Lean ad-level insights for the audit decomposer.

    Unlike `ads_for_account`, does NOT pull creative thumbnails / video_ids
    (those are fat and the audit doesn't need them). Returns enough to
    join into adset→campaign hierarchy. Uses /insights directly so
    adset_id/campaign_id come back in one round-trip. Paginates until
    exhausted or `limit` ads collected.
    """
    out: list[dict] = []
    url = f"{ad_account_id}/insights"
    params = {
        "level": "ad",
        "fields": "ad_id,ad_name,adset_id,adset_name,campaign_id,campaign_name,"
                  "spend,impressions,clicks,ctr,cpc,cpm,frequency,reach,"
                  "actions,action_values,account_currency",
        "limit": 100,
        **_time_params(days),
    }
    cursor_after: str | None = None
    while True:
        if cursor_after:
            params["after"] = cursor_after
        body = await _get(url, params)
        for row in body.get("data", []):
            spend = float(row.get("spend", 0) or 0)
            purchases, purchase_value = _sum_purchases(row)
            out.append({
                "ad_id": row.get("ad_id"),
                "ad_name": row.get("ad_name", ""),
                "adset_id": row.get("adset_id", ""),
                "adset_name": row.get("adset_name", ""),
                "campaign_id": row.get("campaign_id", ""),
                "spend": spend,
                "impressions": int(row.get("impressions", 0) or 0),
                "clicks": int(row.get("clicks", 0) or 0),
                "ctr": float(row.get("ctr", 0) or 0),
                "cpc": float(row.get("cpc", 0) or 0),
                "cpm": float(row.get("cpm", 0) or 0),
                "frequency": float(row.get("frequency", 0) or 0),
                "reach": int(row.get("reach", 0) or 0),
                "purchases": purchases,
                "purchase_value": purchase_value,
                "reported_roas": (purchase_value / spend) if spend > 0 else 0.0,
                "account_currency": row.get("account_currency", "?"),
            })
        nxt = (body.get("paging") or {}).get("cursors", {}).get("after")
        if not nxt or len(out) >= limit:
            break
        cursor_after = nxt
    return out


async def ad_ctr_trend_7d(ad_id: str) -> dict:
    """Return {ctr_7d, ctr_prev7d, delta_pct} for fatigue-diagnosis.

    Two consecutive 7-day windows compared. A drop >30% (delta_pct < -0.3)
    combined with frequency > 2.5 is the fatigue signal.
    """
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc).date()

    def window(start_ago: int, end_ago: int) -> str:
        s = (now - timedelta(days=start_ago)).strftime("%Y-%m-%d")
        e = (now - timedelta(days=end_ago)).strftime("%Y-%m-%d")
        return f'{{"since":"{s}","until":"{e}"}}'

    async def fetch(tr: str) -> float:
        b = await _get(f"{ad_id}/insights",
                       {"fields": "ctr", "time_range": tr})
        d = b.get("data", [])
        return float(d[0].get("ctr", 0) or 0) if d else 0.0

    ctr_7d = await fetch(window(6, 0))
    ctr_prev = await fetch(window(13, 7))
    delta = (ctr_7d - ctr_prev) / ctr_prev if ctr_prev > 0 else 0.0
    return {"ctr_7d": ctr_7d, "ctr_prev7d": ctr_prev, "delta_pct": delta}
