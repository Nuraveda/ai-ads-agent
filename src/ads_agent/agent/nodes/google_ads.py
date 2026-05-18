"""google_ads node: roster + per-campaign metrics for a store's Google Ads account.

Trigger: `/google_ads <store> [days]` from Telegram or Discord.

If the store hasn't linked an account under your MCC yet, the node emits
an actionable message explaining what to do. Once linked + customer_id is
added to STORE_GOOGLE_ADS_ACCOUNTS_JSON, the same command shows real
campaign + keyword data.

Lifetime-test: also lists MCC client accounts to confirm linkage status.
"""
from __future__ import annotations

import logging

from ads_agent.config import get_store
from ads_agent.google_ads.client import (
    GoogleAdsError,
    customer_id_for,
    list_mcc_clients,
)
from ads_agent.google_ads.queries import (
    account_totals,
    list_campaigns,
    list_search_terms,
)

log = logging.getLogger(__name__)


async def google_ads_node(state: dict) -> dict:
    slug = state["store_slug"]
    days = int(state.get("days", 14))
    store = get_store(slug)
    if store is None:
        return {**state, "reply_text": f"Unknown store: `{slug}`"}

    # Resolve customer_id; if not configured, give actionable instructions
    try:
        cid = customer_id_for(slug)
    except GoogleAdsError as e:
        try:
            mcc_kids = list_mcc_clients()
        except GoogleAdsError as e2:
            mcc_kids = []
            mcc_err = str(e2)
        else:
            mcc_err = ""
        lines = [
            f"*{store.brand}* · Google Ads",
            "",
            f"No customer_id mapped for `{slug}` yet. {e}",
            "",
            f"_Currently linked under your MCC ({len(mcc_kids)} accounts):_",
        ]
        for c in mcc_kids[:10]:
            kind = "MCC" if c.get("manager") else "Client"
            lines.append(f"  • {kind} · `{c['customer_id']}` · {c.get('descriptive_name','')} ({c.get('currency_code','')})")
        if mcc_err:
            lines.append(f"  _MCC list failed: {mcc_err[:120]}_")
        return {**state, "reply_text": "\n".join(lines)}

    # 1. Account totals
    try:
        totals = account_totals(slug, days=days)
    except GoogleAdsError as e:
        return {**state, "reply_text": f"*{store.brand}* · Google Ads error: `{e}`"}

    lines = [
        f"*{store.brand} · Google Ads* · customer `{cid}` · last {days}d",
        f"  spend ${totals['spend']:,.2f} · sales ${totals['sales']:,.2f} · "
        f"conversions {totals['conversions']:.0f} · ROAS {totals['roas']:.2f}× · "
        f"{totals['clicks']:,} clicks / {totals['impressions']:,} imp",
        "",
    ]

    # 2. Campaign roster — top by spend
    try:
        camps = list_campaigns(slug, days=days)
    except GoogleAdsError as e:
        lines.append(f"  _campaign roster failed: {e}_")
        return {**state, "reply_text": "\n".join(lines)}

    if not camps:
        lines.append("_No campaigns spent in this window._")
        lines.append("_source: Google Ads native API (LWA SA + dev token)_")
        return {**state, "reply_text": "\n".join(lines)}

    lines.append(f"*Top {min(8, len(camps))} campaigns by spend:*")
    for c in camps[:8]:
        lines.append(
            f"  • `{c['name'][:50]}` · ${c['cost']:,.2f} · "
            f"ROAS {c['roas']:.2f}× · conv {c['conversions']:.0f} · "
            f"CTR {c['ctr']*100:.2f}% · {c['channel_type']}"
        )
    if len(camps) > 8:
        lines.append(f"  …+{len(camps) - 8} more")
    lines.append("")

    # 3. Top wasteful search terms (zero-conv with cost)
    try:
        terms = list_search_terms(slug, days=days, min_cost=1.0)
    except GoogleAdsError as e:
        terms = []
        log.warning("search_terms failed: %s", e)
    waste = [t for t in terms if t["conversions"] == 0 and t["cost"] >= 5.0]
    if waste:
        lines.append(f"*Top {min(5, len(waste))} zero-conv search terms (negative-kw candidates):*")
        for t in waste[:5]:
            lines.append(
                f"  • `{t['search_term'][:45]}` · ${t['cost']:.2f} · "
                f"{t['clicks']} clicks · in `{t['campaign_name'][:30]}`"
            )

    lines.append("")
    lines.append("_source: Google Ads native API_")
    return {**state, "reply_text": "\n".join(lines)}
