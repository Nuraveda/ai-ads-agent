"""amazon_recs: surgical recommendations for an Amazon SP account.

As of 2026-04-22 (v2): replaced the blunt single-call analyst wrapper
with the decomposer + methodology pipeline from
`ads_agent.agent.analysis`. Output is at the correct entity level
(keyword / product_ad / product_target / campaign) with explicit
rationale + expected impact per action — fixes the "pause this campaign"
amateur output class.

Shape:
  1. Campaign roster overview (roster, budgets, bid strategies)
  2. Deep-dive on the top-N spending campaigns: decomposer +
     methodology analyst from the brand's playbook
  3. Account-level budget-recs (MAP endpoint — US-only; falls back
     silently for IN / AE markets)

If no MAP mapping for the store, returns a clean "not configured"
reply rather than raising.

Per-campaign pipeline cost: ~1 MAP list_resources call + ~1 ask_analyst
call for metrics + ~1 complete() call for methodology = ~30-60 seconds
per campaign. Default `drill_top_n=1` keeps /amazon_recs under 90 seconds;
pass days arg for deeper retrospective windows.
"""
from __future__ import annotations

import logging

from ads_agent.agent.analysis.campaign_analyst import analyze_campaign
from ads_agent.agent.analysis.campaign_decomposer import decompose_sp_campaign
from ads_agent.amazon.ads_api import (
    AmazonAdsError,
    list_sp_campaigns,
    profile_id_for,
)
from ads_agent.config import get_store

log = logging.getLogger(__name__)


DRILL_TOP_N = 1   # how many of the highest-budget campaigns to analyze deeply


def _ccy_for(country: str) -> str:
    return {"IN": "INR", "AE": "AED", "US": "USD", "UK": "GBP", "CA": "CAD"}.get(country, "")


def _brand_for(slug: str) -> str:
    """Map store_slug → brand playbook key via STORE_BRAND_REGISTRY_JSON."""
    # brand_registry stripped from open-source release; provide your own
    return brand_for(slug)


async def _format_drilldown(
    slug: str, campaign_id: str, campaign_name: str,
    country: str, days: int,
) -> list[str]:
    """Run decomposer + analyst and format as markdown lines."""
    out: list[str] = []
    out.append(f"*📊 Deep-dive · `{campaign_name}` ({country})*")
    try:
        hierarchy = await decompose_sp_campaign(
            slug=slug, campaign_id=campaign_id, days=days,
        )
    except Exception as e:
        out.append(f"  _decompose failed: {str(e)[:200]}_")
        return out

    ccy = _ccy_for(country)
    m = hierarchy.campaign.metrics
    c = hierarchy.concentration
    out.append(
        f"  14d: spend {m.cost:,.0f} {ccy} · sales {m.sales14d:,.0f} · "
        f"{m.purchases14d} purch · ROAS {m.roas:.2f}× · "
        f"util {hierarchy.campaign.utilization_pct:.0f}% of {hierarchy.campaign.daily_budget:,.0f}/day cap"
    )
    if c.n_active_children > 0:
        out.append(
            f"  concentration: top-1 {c.top_child_label!r} = {c.top_child_pct_spend}% "
            f"@ {c.top_child_roas}× · top-3 = {c.top_3_children_pct_spend}% · "
            f"{c.zero_purchase_children_count} zero-purch children burning "
            f"{c.zero_purchase_children_spend:,.0f} {ccy}"
        )

    try:
        analysis = await analyze_campaign(hierarchy, brand=_brand_for(slug))
    except Exception as e:
        out.append(f"  _analyst failed: {str(e)[:200]}_")
        return out

    diag = (analysis.get("diagnosis") or "").strip()
    if diag:
        out.append("")
        out.append(f"  *Diagnosis:* {diag[:900]}")

    actions = analysis.get("actions") or []
    if actions:
        out.append("")
        out.append(f"  *Actions ({len(actions)}):*")
        for i, a in enumerate(actions[:8], 1):
            kind = a.get("action_kind", "?")
            lbl  = (a.get("target_label") or "").strip()[:60]
            imp  = (a.get("expected_impact") or "").strip()
            out.append(f"    {i}. `{kind}` · {lbl}")
            rat = (a.get("rationale") or "").strip()
            if rat:
                out.append(f"       _{rat[:180]}_")
            if imp:
                out.append(f"       → {imp[:120]}")
    return out


async def amazon_recs_node(state: dict) -> dict:
    slug = state["store_slug"]
    days = int(state.get("days", 14))
    store = get_store(slug)
    if store is None:
        return {**state, "reply_text": f"Unknown store: `{slug}`"}

    # Native Amazon Ads via LWA OAuth — confirm slug has a profile mapping
    try:
        await profile_id_for(slug)
    except AmazonAdsError as e:
        return {**state, "reply_text": (
            f"*{store.brand}* · Amazon recs\n\n"
            f"No Amazon Ads profile mapped for `{slug}`. {e}"
        )}

    # Country comes from the store config; native API doesn't expose it directly
    country = (store.currency or "").upper().replace("INR","IN").replace("AED","AE")
    ccy = _ccy_for(country)
    lines = [f"*{store.brand} · Amazon {country}* · surgical recommendations", ""]

    # --- 1. Campaign roster ---------------------------------------------------
    try:
        campaigns = await list_sp_campaigns(slug)
    except AmazonAdsError as e:
        return {**state, "reply_text": f"Amazon Ads error on campaign list: {e}"}

    if not campaigns:
        lines.append("No enabled Sponsored Products campaigns found.")
        lines.append("")
        lines.append("_source: Amazon Ads native API (LWA OAuth)_")
        return {**state, "reply_text": "\n".join(lines)}

    total_budget = sum(
        float((c.get("budget") or {}).get("budget", 0) or 0) for c in campaigns
    )
    lines.append(f"*Enabled SP campaigns:* {len(campaigns)} · "
                 f"total daily budget {total_budget:,.0f} {ccy}")
    top_by_budget = sorted(
        campaigns,
        key=lambda c: (c.get("budget") or {}).get("budget", 0) or 0,
        reverse=True,
    )
    for c in top_by_budget[:5]:
        b = (c.get("budget") or {}).get("budget", 0)
        strat = (c.get("dynamicBidding") or {}).get("strategy", "?")
        ttype = c.get("targetingType", "?")
        lines.append(f"  • {c['name'][:55]} · {b:.0f}/d · {strat} · {ttype}")
    if len(campaigns) > 5:
        lines.append(f"  …+{len(campaigns) - 5} more")
    lines.append("")

    # --- 2. Drill-down analysis on top-N spending campaigns -------------------
    # (capped so /amazon_recs stays under ~90s total)
    drill_candidates = top_by_budget[:DRILL_TOP_N]
    for c in drill_candidates:
        camp_id = str(c.get("campaignId", ""))
        camp_name = c.get("name", "?")
        if not camp_id:
            continue
        block = await _format_drilldown(
            slug, camp_id, camp_name, country, days,
        )
        lines.extend(block)
        lines.append("")

    # Account-level budget recs were a MAP-paid-tier-only feature; gone with
    # the MAP rip-out. Native Amazon Ads API has its own /budgetRules and
    # /budgetUsage endpoints — wire those in a follow-up if the operator
    # wants budget-exhaustion alerts.

    lines.append("_source: Amazon Ads native API (LWA OAuth) + methodology "
                 "analyst (playbook Section X)_")
    return {**state, "reply_text": "\n".join(lines)}
