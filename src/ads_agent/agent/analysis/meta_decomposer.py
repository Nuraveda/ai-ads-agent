"""Decompose a Meta ad account into campaign → adset → ad with 14d metrics.

Mirror of `campaign_decomposer.py` (Amazon SP) for Meta. The analyst LLM
consumes a single structured dict that contains:

  account_summary           — totals + blended ROAS
  campaigns[]               — each with:
    metrics, config (ASC+/buying_type/budget), concentration (by ad_set
    spend share), ad_sets[] (each with metrics + ads[])
  pre_flight[]              — hygiene signals: Purchase event counts,
                              attribution window used, window length

Every number here is traceable to a Meta Graph API pull, timestamped.
The analyst is forbidden from inventing figures; if a claim doesn't tie
back to this payload, it's hallucination.
"""
from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

from ads_agent.meta.graph_client import (
    account_info,
    account_spend,
    ads_for_account_lean,
    adsets_for_account,
    campaigns_for_account,
)

log = logging.getLogger(__name__)


@dataclass
class AdRow:
    ad_id: str
    ad_name: str
    status: str
    effective_status: str
    spend: float
    impressions: int
    clicks: int
    ctr: float
    cpc: float
    cpm: float
    frequency: float
    reach: int
    purchases: int
    purchase_value: float
    roas: float
    days_live: float
    creative_object_type: str = ""
    creative_video_id: str = ""
    creative_thumbnail: str = ""
    # Destination tagging — engine-level, brand-neutral. The methodology
    # for what to do with these tags lives per-brand in the playbook.
    destination: str = "unknown"   # amazon | shopify-ind | shopify-global | shopify-other | other | unknown
    destination_url: str = ""
    target_asin: str = ""
    # Halo stamp — populated at decompose time from amazon_halo.per_asin.
    # Lets the analyst quote a deterministic per-ad halo number instead
    # of having to find the right per_asin row itself (which it sometimes
    # hallucinates). 0.0 / 0 when destination != amazon or no halo data.
    target_asin_halo_roas: float = 0.0
    target_asin_meta_orders: int = 0
    target_asin_meta_gross_inr: float = 0.0
    target_asin_meta_clicks: int = 0
    # Name-based intent (campaign+adset+ad names). Cross-evidence for
    # cases where URL extraction fails or name/URL disagree (e.g.
    # campaign duplicated from Amazon to Shopify but kept the old name).
    name_amazon_intent: bool = False
    name_market_hint: str = "unknown"     # AE | IN | unknown
    destination_consistency: str = "n/a"  # match | name_only | url_only | n/a


@dataclass
class AdSetRow:
    adset_id: str
    name: str
    status: str
    effective_status: str
    optimization_goal: str
    billing_event: str
    bid_strategy: str
    daily_budget: float
    spend: float
    impressions: int
    clicks: int
    ctr: float
    cpc: float
    cpm: float
    frequency: float
    reach: int
    purchases: int
    purchase_value: float
    roas: float
    ads: list[AdRow] = field(default_factory=list)


@dataclass
class Concentration:
    n_adsets: int
    n_ads: int
    top_ad_label: str
    top_ad_pct_spend: float
    top_ad_pct_revenue: float
    top_ad_roas: float
    top_3_ads_pct_spend: float
    zero_purchase_ads_count: int
    zero_purchase_ads_spend: float
    carried_by_one_ad: bool  # True if top-1 ad holds ≥70% of campaign revenue


@dataclass
class CampaignRow:
    campaign_id: str
    name: str
    status: str
    effective_status: str
    objective: str
    buying_type: str
    is_asc_plus: bool
    daily_budget: float
    lifetime_budget: float
    currency: str
    spend: float
    impressions: int
    clicks: int
    ctr: float
    cpc: float
    cpm: float
    frequency: float
    reach: int
    purchases: int
    purchase_value: float
    roas: float
    concentration: Concentration | None = None
    ad_sets: list[AdSetRow] = field(default_factory=list)
    # Campaign-level Amazon halo summary — pre-computed at decompose time
    # so the analyst can quote it verbatim instead of doing weighted-mean
    # math itself (which it has been hallucinating). Empty string when
    # the campaign has no Amazon-destined ads.
    amazon_halo_blended: float = 0.0
    amazon_destined_spend: float = 0.0
    amazon_destined_spend_pct: float = 0.0
    amazon_halo_summary: str = ""


@dataclass
class AccountSummary:
    ad_account_id: str
    account_name: str
    currency: str
    days: int
    generated_at: str
    n_campaigns: int
    n_adsets: int
    n_ads: int
    spend: float
    impressions: int
    clicks: int
    purchases: int
    purchase_value: float
    blended_roas: float
    blended_ctr: float
    blended_cpc: float


@dataclass
class PreFlight:
    attribution_window: str
    days_window: int
    day_of_week_skew_risk: bool
    purchase_event_count_7d: int
    purchase_event_value_sum_7d: float
    purchase_event_currency_sane: bool
    pixel_hygiene_ok: bool
    asc_plus_campaign_count: int
    manual_campaign_count: int
    # M04 — Event Match Quality for Purchase (None if not measured)
    emq_purchase_score: float | None = None
    emq_detail: str = ""


@dataclass
class MetaAccountHierarchy:
    summary: AccountSummary
    pre_flight: PreFlight
    campaigns: list[CampaignRow] = field(default_factory=list)
    skipped_noise: dict = field(default_factory=dict)   # {count, total_spend, reason}
    # M15 — Andromeda creative-similarity diagnostic over active ads
    creative_diversity: dict = field(default_factory=dict)
    # Destination-mix breakdown across the analysed ads (engine-level data)
    destination_mix: dict = field(default_factory=dict)
    # URL-vs-name cross-evidence summary. Surfaces operational drift like
    # "campaign named final_retargeting|_amazon_uae but its ads point at
    # the brand's Shopify storefront" — which a URL-only or name-only view
    # both miss.
    destination_mismatches: dict = field(default_factory=dict)
    # Amazon halo at account + per-ASIN level. Populated only when the
    # store has STORE_MAP_ACCOUNTS configured (today: <client> only).
    # Empty dict for brands without — analyst then ignores the halo
    # rules and applies the standard Meta-ROAS methodology.
    amazon_halo: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _mk_concentration(ads: list[AdRow], campaign_revenue: float) -> Concentration:
    live = [a for a in ads if a.spend > 0 or a.impressions > 0]
    n_ads = len(live)
    if not live:
        return Concentration(
            n_adsets=0, n_ads=0, top_ad_label="",
            top_ad_pct_spend=0, top_ad_pct_revenue=0, top_ad_roas=0,
            top_3_ads_pct_spend=0, zero_purchase_ads_count=0,
            zero_purchase_ads_spend=0, carried_by_one_ad=False,
        )
    live_sorted = sorted(live, key=lambda a: a.spend, reverse=True)
    total_spend = sum(a.spend for a in live_sorted) or 1.0
    total_rev = sum(a.purchase_value for a in live_sorted) or 0.0
    top = live_sorted[0]
    top3 = live_sorted[:3]
    zeros = [a for a in live_sorted if a.purchases == 0 and a.spend > 100]
    top_rev_pct = (top.purchase_value / total_rev) if total_rev > 0 else 0.0
    return Concentration(
        n_adsets=0,
        n_ads=n_ads,
        top_ad_label=top.ad_name[:80],
        top_ad_pct_spend=round(100 * top.spend / total_spend, 1),
        top_ad_pct_revenue=round(100 * top_rev_pct, 1),
        top_ad_roas=round(top.roas, 2),
        top_3_ads_pct_spend=round(100 * sum(a.spend for a in top3) / total_spend, 1),
        zero_purchase_ads_count=len(zeros),
        zero_purchase_ads_spend=round(sum(a.spend for a in zeros), 2),
        carried_by_one_ad=(top_rev_pct >= 0.70 and top.purchases >= 1),
    )


async def decompose_meta_account(
    ad_account_id: str, *, days: int = 14, noise_floor: float = 500.0,
    store_slug: str | None = None,
) -> MetaAccountHierarchy:
    """Parallel Graph API pulls + per-ad destination tagging + Amazon
    halo lookup (when store_slug is configured for MAP). Total runtime
    typically 4–8s for an account with <50 campaigns and <500 ads.

    `store_slug` is the agent's store key — used to look up
    STORE_MAP_ACCOUNTS for the Amazon halo. Pass None to skip the halo
    pull entirely (engine stays brand-neutral; <client>'s audit gets the
    halo, others see an empty dict).
    """
    t0 = time.time()
    from ads_agent.meta.graph_client import ad_destinations_for_account
    info, camp_rows, adset_rows, ad_rows, hygiene_7d, dest_map = await _parallel(
        account_info(ad_account_id),
        campaigns_for_account(ad_account_id, days=days),
        adsets_for_account(ad_account_id, days=days),
        ads_for_account_lean(ad_account_id, days=days, limit=1000),
        account_spend(ad_account_id, days=7),
        ad_destinations_for_account(ad_account_id, limit=1000),
    )

    currency = info.get("currency", "?")

    # Resolve any short links (amzn.eu/d/...) once per unique URL — small
    # set typically (<5), bounded; failures fall back to the original URL.
    short_links = {
        u for u in dest_map.values()
        if u and ("amzn.eu" in u or "amzn.to" in u)
    }
    resolved: dict[str, str] = {}
    for u in short_links:
        resolved[u] = await _resolve_short_url(u)

    from ads_agent.meta.destinations import (
        classify_destination, classify_name, cross_check, parse_asin,
    )

    ads_by_adset: dict[str, list[AdRow]] = {}
    for r in ad_rows:
        raw_dest = dest_map.get(r["ad_id"], "") or ""
        # Use resolved URL when we followed a short link; else raw
        eff_dest = resolved.get(raw_dest, raw_dest)
        # Name-based intent (cross-evidence layer)
        nc = classify_name(
            r.get("campaign_name"), r.get("adset_name"), r.get("ad_name"),
        )
        url_dest = classify_destination(eff_dest)
        ad = AdRow(
            ad_id=r["ad_id"], ad_name=r["ad_name"],
            status="", effective_status="",  # lean pull skips these; fine — audit works off numbers
            spend=r["spend"], impressions=r["impressions"],
            clicks=r["clicks"], ctr=r["ctr"], cpc=r["cpc"], cpm=r["cpm"],
            frequency=r["frequency"], reach=r["reach"],
            purchases=r["purchases"], purchase_value=r["purchase_value"],
            roas=r.get("reported_roas", 0.0),
            days_live=0.0,
            destination=url_dest,
            destination_url=eff_dest,
            target_asin=parse_asin(eff_dest) or "",
            name_amazon_intent=nc["amazon_intent"],
            name_market_hint=nc["market_hint"],
            destination_consistency=cross_check(url_dest, nc),
        )
        ads_by_adset.setdefault(r.get("adset_id", ""), []).append(ad)

    # Build adset rows
    adsets_by_campaign: dict[str, list[AdSetRow]] = {}
    for r in adset_rows:
        as_row = AdSetRow(
            adset_id=r["adset_id"], name=r["name"],
            status=r["status"], effective_status=r["effective_status"],
            optimization_goal=r["optimization_goal"],
            billing_event=r["billing_event"],
            bid_strategy=r["bid_strategy"],
            daily_budget=r["daily_budget"],
            spend=r["spend"], impressions=r["impressions"],
            clicks=r["clicks"], ctr=r["ctr"], cpc=r["cpc"], cpm=r["cpm"],
            frequency=r["frequency"], reach=r["reach"],
            purchases=r["purchases"], purchase_value=r["purchase_value"],
            roas=r["roas"],
            ads=sorted(
                ads_by_adset.get(r["adset_id"], []),
                key=lambda a: a.spend, reverse=True,
            ),
        )
        adsets_by_campaign.setdefault(r["campaign_id"], []).append(as_row)

    # Build campaign rows with concentration
    campaigns: list[CampaignRow] = []
    for r in camp_rows:
        ad_sets = adsets_by_campaign.get(r["campaign_id"], [])
        # Roll all ads across all ad sets for concentration
        all_ads: list[AdRow] = []
        for s in ad_sets:
            all_ads.extend(s.ads)
        conc = _mk_concentration(all_ads, r["purchase_value"])
        conc.n_adsets = len([s for s in ad_sets if s.spend > 0 or s.impressions > 0])
        campaigns.append(CampaignRow(
            campaign_id=r["campaign_id"], name=r["name"],
            status=r["status"], effective_status=r["effective_status"],
            objective=r["objective"], buying_type=r["buying_type"],
            is_asc_plus=r["is_asc_plus"],
            daily_budget=r["daily_budget"],
            lifetime_budget=r["lifetime_budget"],
            currency=r["currency"] if r["currency"] != "?" else currency,
            spend=r["spend"], impressions=r["impressions"],
            clicks=r["clicks"], ctr=r["ctr"], cpc=r["cpc"], cpm=r["cpm"],
            frequency=r["frequency"], reach=r["reach"],
            purchases=r["purchases"], purchase_value=r["purchase_value"],
            roas=r["roas"],
            concentration=conc,
            ad_sets=sorted(ad_sets, key=lambda s: s.spend, reverse=True),
        ))
    campaigns.sort(key=lambda c: c.spend, reverse=True)

    # Filter out tiny-spend campaigns — below noise_floor they're either
    # paused stragglers or half-built shells. Keep them counted in the
    # account summary totals but drop from the analyst payload so the
    # LLM doesn't burn tokens wrongly pausing ₹418 campaigns.
    significant = [c for c in campaigns if c.spend >= noise_floor or c.purchases > 0]
    dropped = [c for c in campaigns if c not in significant]
    dropped_spend = sum(c.spend for c in dropped)
    dropped_count = len(dropped)

    # Account summary
    total_spend = sum(c.spend for c in campaigns)
    total_rev   = sum(c.purchase_value for c in campaigns)
    total_imp   = sum(c.impressions for c in campaigns)
    total_clicks= sum(c.clicks for c in campaigns)
    total_purch = sum(c.purchases for c in campaigns)
    summary = AccountSummary(
        ad_account_id=ad_account_id,
        account_name=info.get("name", ""),
        currency=currency,
        days=days,
        generated_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        n_campaigns=len(campaigns),
        n_adsets=sum(len(c.ad_sets) for c in campaigns),
        n_ads=sum(len(s.ads) for c in campaigns for s in c.ad_sets),
        spend=round(total_spend, 2),
        impressions=total_imp,
        clicks=total_clicks,
        purchases=total_purch,
        purchase_value=round(total_rev, 2),
        blended_roas=round(total_rev / total_spend, 2) if total_spend > 0 else 0.0,
        blended_ctr=round(100 * total_clicks / total_imp, 2) if total_imp > 0 else 0.0,
        blended_cpc=round(total_spend / total_clicks, 2) if total_clicks > 0 else 0.0,
    )

    # Pre-flight hygiene — always use an independent 7-day pull so the
    # hygiene verdict is canonical regardless of the audit's data window.
    # Pixel is broken only if 7-day spend is meaningful but 0 purchases
    # OR purchases recorded with 0 value (a common mis-configuration where
    # the event fires but doesn't pass `value` + `currency`).
    h7_spend = float(hygiene_7d.get("spend", 0) or 0)
    h7_purch = int(hygiene_7d.get("purchases", 0) or 0)
    h7_value = float(hygiene_7d.get("purchase_value", 0) or 0)
    pixel_ok = (
        h7_spend < 1000  # too little spend to reliably detect — assume ok
        or (h7_purch > 0 and h7_value > 0)
    )
    asc = sum(1 for c in campaigns if c.is_asc_plus)

    # M04 EMQ — best-effort fetch, graceful degradation if scope missing
    emq_score: float | None = None
    emq_detail = ""
    try:
        from ads_agent.meta.emq import fetch_emq
        # Pixel id is per-store, not per-account — but we don't have it
        # threaded here yet. Pass None so emq.py returns the operator-
        # check guidance.
        # (Future: take pixel_id as decompose_meta_account argument.)
        reading = await fetch_emq(None)
        emq_score = reading.score
        emq_detail = reading.detail
    except Exception as e:  # noqa: BLE001
        emq_detail = f"EMQ check failed: {e}"

    pre = PreFlight(
        attribution_window="7d_click (default)",
        days_window=days,
        day_of_week_skew_risk=(days < 7),
        purchase_event_count_7d=h7_purch,
        purchase_event_value_sum_7d=round(h7_value, 2),
        purchase_event_currency_sane=(currency not in ("", "?")),
        pixel_hygiene_ok=pixel_ok,
        asc_plus_campaign_count=asc,
        manual_campaign_count=len(campaigns) - asc,
        emq_purchase_score=emq_score,
        emq_detail=emq_detail,
    )

    log.info(
        "meta_decompose: account=%s days=%d → %d significant campaigns "
        "(+%d noise skipped, ₹%.0f), %d adsets, %d ads in %.1fs",
        ad_account_id, days, len(significant), dropped_count, dropped_spend,
        summary.n_adsets, summary.n_ads, time.time() - t0,
    )
    # M15 — creative diversity score across the ads we actually analysed
    from ads_agent.actions.diversity import diversity_report
    flat_ads: list[dict] = []
    for c in significant:
        for s in c.ad_sets:
            for a in s.ads:
                if a.spend > 0 or a.impressions > 0:
                    flat_ads.append({
                        "ad_id": a.ad_id, "ad_name": a.ad_name,
                        "creative": {
                            "title": "",  # lean pull doesn't carry these
                            "body":  "",
                        },
                    })
    diversity = diversity_report(flat_ads) if flat_ads else {}

    # Destination mix across the campaigns we kept (engine-level, useful
    # for any brand to see at a glance — e.g. "11% of spend points at
    # Amazon"). Computed from AdRow.destination + AdRow.spend.
    destination_mix: dict = {}
    for c in significant:
        for s in c.ad_sets:
            for a in s.ads:
                if a.spend <= 0:
                    continue
                d = destination_mix.setdefault(
                    a.destination,
                    {"ads": 0, "spend": 0.0, "purchases": 0, "revenue": 0.0},
                )
                d["ads"] += 1
                d["spend"] += a.spend
                d["purchases"] += a.purchases
                d["revenue"] += a.purchase_value
    for d in destination_mix.values():
        d["spend"] = round(d["spend"], 2)
        d["revenue"] = round(d["revenue"], 2)
        d["meta_reported_roas"] = round(
            (d["revenue"] / d["spend"]) if d["spend"] > 0 else 0.0, 2,
        )

    # URL-vs-name mismatch summary (engine-neutral data — methodology for
    # what to do with it lives per-brand in the playbook).
    mismatches = {
        "match":      {"ads": 0, "spend": 0.0},
        "name_only":  {"ads": 0, "spend": 0.0},  # name says Amazon, URL doesn't
        "url_only":   {"ads": 0, "spend": 0.0},  # URL says Amazon, name doesn't
        "n/a":        {"ads": 0, "spend": 0.0},
    }
    sample_name_only: list[dict] = []   # surface top offenders for the audit
    for c in significant:
        for s in c.ad_sets:
            for a in s.ads:
                if a.spend <= 0:
                    continue
                bucket = mismatches.setdefault(
                    a.destination_consistency or "n/a",
                    {"ads": 0, "spend": 0.0},
                )
                bucket["ads"] += 1
                bucket["spend"] += a.spend
                if a.destination_consistency == "name_only" and a.spend > 200:
                    sample_name_only.append({
                        "ad_id": a.ad_id,
                        "ad_name": a.ad_name,
                        "campaign": c.name,
                        "spend": round(a.spend, 2),
                        "url": a.destination_url,
                    })
    for v in mismatches.values():
        v["spend"] = round(v["spend"], 2)
    sample_name_only.sort(key=lambda x: x["spend"], reverse=True)
    destination_mismatches = {
        "buckets": mismatches,
        "name_says_amazon_url_says_shopify_or_other": sample_name_only[:8],
        "note": (
            "name_only = campaign/adset/ad name contains Amazon hints but "
            "the destination URL points at Shopify (or other). Common "
            "operational drift: campaign duplicated from Amazon to Shopify "
            "but the name was kept. Treat the URL as truth for halo math; "
            "treat the name as a flag for ops review."
        ),
    }

    # Amazon halo — only fires for slugs configured in STORE_MAP_ACCOUNTS
    # (today: <client>-ind, <client>-global). Other brands get an empty dict
    # and their analyst prompt won't reference the halo at all.
    halo = await _amazon_halo_for_slug(store_slug, days)

    # Stamp per-ASIN halo onto every Amazon-destined AdRow so the analyst
    # has a deterministic, ad-attached number to quote in M40 / RECLAIM /
    # SCALE rationales — eliminates the per_asin lookup hallucination
    # we hit on the first live audit (LLM sometimes picked the wrong
    # ASIN's halo from the per_asin list).
    if halo and halo.get("per_asin"):
        asin_halo: dict[str, dict] = {row["asin"]: row for row in halo["per_asin"]}
        for c in significant:
            for s in c.ad_sets:
                for a in s.ads:
                    if a.destination != "amazon" or not a.target_asin:
                        continue
                    row = asin_halo.get(a.target_asin)
                    if not row:
                        continue
                    a.target_asin_halo_roas      = float(row.get("halo_roas") or 0.0)
                    a.target_asin_meta_orders    = int(row.get("meta_orders") or 0)
                    a.target_asin_meta_gross_inr = float(row.get("meta_gross_inr") or 0.0)
                    a.target_asin_meta_clicks    = int(row.get("meta_clicks") or 0)

        # Campaign-level halo summary (Phase A): spend-weighted mean across
        # the campaign's Amazon-destined ads, plus a one-line summary the
        # analyst is told to quote verbatim. Avoids LLM-side weighted-mean
        # math, which was producing hallucinated digits.
        for c in significant:
            amz_ads: list[AdRow] = []
            for s in c.ad_sets:
                for a in s.ads:
                    if a.destination == "amazon" and a.target_asin and a.spend > 0:
                        amz_ads.append(a)
            if not amz_ads:
                continue
            amz_spend = sum(a.spend for a in amz_ads)
            c.amazon_destined_spend = round(amz_spend, 2)
            c.amazon_destined_spend_pct = round(
                100 * amz_spend / c.spend, 1,
            ) if c.spend > 0 else 0.0
            blended = (
                sum(a.spend * a.target_asin_halo_roas for a in amz_ads)
                / amz_spend
            ) if amz_spend > 0 else 0.0
            c.amazon_halo_blended = round(blended, 2)
            # Per-ASIN breakdown sorted by spend share within the campaign
            by_asin: dict[str, dict] = {}
            for a in amz_ads:
                d = by_asin.setdefault(
                    a.target_asin,
                    {"spend": 0.0, "halo": a.target_asin_halo_roas},
                )
                d["spend"] += a.spend
            chunks = []
            for asin, d in sorted(by_asin.items(), key=lambda x: x[1]["spend"], reverse=True):
                pct = round(100 * d["spend"] / amz_spend, 0)
                chunks.append(f"{int(pct)}% {asin} ({d['halo']}×)")
            c.amazon_halo_summary = (
                f"{c.amazon_destined_spend_pct}% of campaign spend → Amazon: "
                + " · ".join(chunks)
                + f" · weighted blended halo {c.amazon_halo_blended}×"
            )

    return MetaAccountHierarchy(
        summary=summary, pre_flight=pre, campaigns=significant,
        skipped_noise={
            "count": dropped_count, "total_spend": round(dropped_spend, 2),
            "reason": f"spend < {noise_floor:.0f} {currency} and 0 purchases",
        },
        creative_diversity=diversity,
        destination_mix=destination_mix,
        destination_mismatches=destination_mismatches,
        amazon_halo=halo,
    )


async def _parallel(*coros):
    import asyncio
    return await asyncio.gather(*coros)


async def _resolve_short_url(url: str) -> str:
    """Follow `amzn.eu/d/<short>` (and similar) one redirect to a real
    `amazon.<tld>/dp/<ASIN>` URL. Best-effort, 5s timeout, swallows
    exceptions — falls back to the original URL on any failure."""
    try:
        import httpx
        async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as c:
            r = await c.head(url, headers={"User-Agent": "Mozilla/5.0"})
            loc = r.headers.get("location") or ""
            return loc or url
    except Exception:  # noqa: BLE001
        return url


async def _amazon_halo_for_slug(store_slug: str | None, days: int) -> dict:
    """Pull Meta→Amazon halo for a store at account + per-ASIN level.

    Empty dict if the store isn't configured for MAP (i.e. no Amazon
    integration). Today this fires for <client> IND + Global; other
    brands return {} and the analyst doesn't see this block at all.

    Reads from `ads_agent.amazon_attribution_daily_v` which is the same
    view the /attribution node consumes, so numbers are consistent.
    """
    if not store_slug:
        return {}
    try:
        from ads_agent.config import STORE_AMAZON_ACCOUNTS, settings
    except Exception:
        return {}
    if store_slug not in STORE_AMAZON_ACCOUNTS:
        return {}

    try:
        import asyncpg
        conn = await asyncpg.connect(settings().postgres_insights_ro_url)
    except Exception as e:  # noqa: BLE001
        log.warning("amazon_halo: pg connect failed: %s", e)
        return {}

    try:
        # meta_attributed_gross_inr is the ALREADY-INR-NORMALISED column the
        # attribution view exposes; meta_spend is also INR. Using the
        # native-currency `meta_attributed_gross` (e.g. AED for <client>-global)
        # against an INR spend gives a fake near-zero halo. INR_INR matters.
        agg = await conn.fetchrow(
            """SELECT
                   SUM(amz_orders)                  AS amz_orders,
                   SUM(amz_gross)                   AS amz_gross,
                   SUM(sp_orders)                   AS sp_orders,
                   SUM(sp_sales1d)                  AS sp_sales,
                   SUM(meta_spend)                  AS meta_spend,
                   SUM(meta_attributed_orders)      AS meta_orders,
                   SUM(meta_attributed_gross)       AS meta_gross_native,
                   SUM(meta_attributed_gross_inr)   AS meta_gross_inr,
                   MAX(currency)                    AS currency
               FROM ads_agent.amazon_attribution_daily_v
               WHERE store_slug = $1
                 AND date > (CURRENT_DATE - ($2::int * INTERVAL '1 day'))""",
            store_slug, days,
        )
        per_asin = await conn.fetch(
            """SELECT asin,
                      SUM(meta_spend)                  AS meta_spend,
                      SUM(meta_clicks)                 AS meta_clicks,
                      SUM(meta_attributed_orders)      AS meta_orders,
                      SUM(meta_attributed_gross)       AS meta_gross_native,
                      SUM(meta_attributed_gross_inr)   AS meta_gross_inr
               FROM ads_agent.amazon_attribution_daily_v
               WHERE store_slug = $1
                 AND date > (CURRENT_DATE - ($2::int * INTERVAL '1 day'))
                 AND asin IS NOT NULL
                 AND meta_spend > 0
               GROUP BY asin
               ORDER BY meta_spend DESC""",
            store_slug, days,
        )
    finally:
        await conn.close()

    if not agg or not agg["meta_spend"]:
        return {}

    meta_spend       = float(agg["meta_spend"] or 0)            # INR
    meta_orders      = int(agg["meta_orders"] or 0)
    meta_gross_inr   = float(agg["meta_gross_inr"] or 0)         # INR-normalised
    meta_gross_native= float(agg["meta_gross_native"] or 0)
    sp_orders        = int(agg["sp_orders"] or 0)
    sp_sales         = float(agg["sp_sales"] or 0)               # native (AED for AE)
    native_currency  = agg["currency"] or "?"
    # Subtraction-model halo (upper bound). Method 1 from /attribution.
    incremental_orders = max(0, meta_orders - sp_orders)
    # Both numerator and denominator must be in the same currency to be
    # comparable. meta_spend is INR; meta_gross_inr is INR; halo_roas is
    # therefore a pure ratio.
    incremental_gross_inr = max(0.0, meta_gross_inr)
    halo_roas = (incremental_gross_inr / meta_spend) if meta_spend > 0 else 0.0

    return {
        "currency_native": native_currency,
        "currency_normalised": "INR",
        "days": days,
        "method": "subtraction (Meta-attributed Amazon orders, INR-normalised)",
        "meta_spend_inr":               round(meta_spend, 2),
        "meta_attributed_orders":       meta_orders,
        "meta_attributed_gross_native": round(meta_gross_native, 2),
        "meta_attributed_gross_inr":    round(meta_gross_inr, 2),
        "sp_orders":                    sp_orders,
        "sp_sales_native":              round(sp_sales, 2),
        "incremental_orders":           incremental_orders,
        "halo_roas":                    round(halo_roas, 2),
        "per_asin": [
            {
                "asin": r["asin"],
                "meta_spend_inr":   round(float(r["meta_spend"] or 0), 2),
                "meta_clicks":      int(r["meta_clicks"] or 0),
                "meta_orders":      int(r["meta_orders"] or 0),
                "meta_gross_native": round(float(r["meta_gross_native"] or 0), 2),
                "meta_gross_inr":   round(float(r["meta_gross_inr"] or 0), 2),
                "halo_roas":        round(
                    (float(r["meta_gross_inr"] or 0) / float(r["meta_spend"] or 1))
                    if float(r["meta_spend"] or 0) > 0 else 0.0,
                    2,
                ),
            }
            for r in per_asin
        ],
        "note": (
            "Meta has no visibility into Amazon orders, so any ad whose "
            "destination is amazon.* will show meta-reported ROAS of ~0. "
            "For Amazon-destined ads, USE halo_roas (or per_asin[i].halo_roas) "
            "as the truth, NOT the ad's meta-reported ROAS. All gross figures "
            "are INR-normalised so halo_roas is comparable across IN + AE markets."
        ),
    }
