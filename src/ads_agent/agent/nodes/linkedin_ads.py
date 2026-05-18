"""linkedin_ads node: ad-account roster + per-campaign metrics.

Trigger: `/linkedin_ads <store> [days]` from Telegram or Discord.

If the store hasn't been mapped to an account_id yet, the node emits an
actionable message listing every ad account the OAuth user can see —
that's the "Manage Access" linkage status, equivalent to the Google Ads
MCC roster pane.
"""
from __future__ import annotations

import logging

from ads_agent.config import get_store
from ads_agent.linkedin.client import (
    LinkedInError,
    ad_account_id_for,
    list_ad_accounts,
)
from ads_agent.linkedin.queries import (
    account_totals,
    list_campaigns,
    list_creatives,
)

log = logging.getLogger(__name__)


async def linkedin_ads_node(state: dict) -> dict:
    slug = state["store_slug"]
    days = int(state.get("days", 14))
    store = get_store(slug)
    if store is None:
        return {**state, "reply_text": f"Unknown store: `{slug}`"}

    # Resolve account_id; fall back to roster guidance
    try:
        aid = ad_account_id_for(slug)
    except LinkedInError as e:
        try:
            roster = list_ad_accounts()
        except LinkedInError as e2:
            roster = []
            roster_err = str(e2)
        else:
            roster_err = ""
        lines = [
            f"*{store.brand}* · LinkedIn Ads",
            "",
            f"No account_id mapped for `{slug}` yet. {e}",
            "",
            f"_Currently visible to our OAuth user ({len(roster)} accounts):_",
        ]
        for a in roster[:10]:
            lines.append(
                f"  • `{a['id']}` · {a['name']} · {a.get('type','')} · "
                f"{a.get('status','')} · {a.get('currency','')}"
            )
        if roster_err:
            lines.append(f"  _roster fetch failed: {roster_err[:120]}_")
        return {**state, "reply_text": "\n".join(lines)}

    # 1. Account totals
    try:
        totals = account_totals(slug, days=days)
    except LinkedInError as e:
        return {**state, "reply_text": f"*{store.brand}* · LinkedIn Ads error: `{e}`"}

    lines = [
        f"*{store.brand} · LinkedIn Ads* · account `{aid}` · last {days}d",
        f"  spend ${totals['spend']:,.2f} · {totals['clicks']:,} clicks / "
        f"{totals['impressions']:,} imp · CTR {totals['ctr']*100:.2f}% · "
        f"CPC ${totals['cpc']:.2f} · conv {totals['conversions']}",
        "",
    ]

    # 2. Campaigns
    try:
        camps = list_campaigns(slug, days=days)
    except LinkedInError as e:
        lines.append(f"  _campaign roster failed: {e}_")
        return {**state, "reply_text": "\n".join(lines)}

    if not camps:
        lines.append("_No campaigns on this account._")
        lines.append("_source: LinkedIn Marketing API (native)_")
        return {**state, "reply_text": "\n".join(lines)}

    lines.append(f"*Top {min(8, len(camps))} campaigns by spend:*")
    for c in camps[:8]:
        lines.append(
            f"  • `{c['name'][:50]}` · ${c['cost']:,.2f} · "
            f"{c['clicks']} clicks · CTR {c['ctr']*100:.2f}% · "
            f"{c.get('status','')} · {c.get('objective','')}"
        )
    if len(camps) > 8:
        lines.append(f"  …+{len(camps) - 8} more")
    lines.append("")

    # 3. Top creatives by spend
    try:
        creatives = list_creatives(slug, days=days)
    except LinkedInError as e:
        creatives = []
        log.warning("linkedin creatives failed: %s", e)
    if creatives:
        lines.append(f"*Top {min(5, len(creatives))} creatives by spend:*")
        for cr in creatives[:5]:
            lines.append(
                f"  • `{cr['creative_id']}` · ${cr['cost']:,.2f} · "
                f"{cr['clicks']} clicks · CTR {cr['ctr']*100:.2f}%"
            )
        lines.append("")

    lines.append("_source: LinkedIn Marketing API (native)_")
    return {**state, "reply_text": "\n".join(lines)}
