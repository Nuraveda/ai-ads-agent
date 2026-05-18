"""ads_leaderboard: rank live ads by spend across all a store's Meta ad accounts.

Output is a Telegram-readable table with the top N ads — their spend, CTR, CPC,
purchases (Meta-reported), reported ROAS, and a hint of the creative (emoji +
truncated body). The full per-ad critique is available via /creative <ad_id>.
"""
from __future__ import annotations

from ads_agent.config import STORE_AD_ACCOUNTS, get_store
from ads_agent.meta.graph_client import MetaGraphError, ads_for_account

LEADER_TOP_N = 10
TELEGRAM_MSG_LIMIT = 3800  # leave headroom for markdown entities


def _creative_hint(creative: dict) -> str:
    kind = creative.get("object_type", "")
    icon = {"VIDEO": "🎬", "PHOTO": "🖼️", "SHARE": "🔗"}.get(kind, "📄")
    body = (creative.get("body") or "").replace("\n", " ").strip()
    if len(body) > 50:
        body = body[:47] + "…"
    return f"{icon} {body}" if body else icon


async def ads_leaderboard_node(state: dict) -> dict:
    slug = state["store_slug"]
    days = int(state.get("days", 7))
    store = get_store(slug)
    if store is None:
        return {**state, "reply_text": f"Unknown store: `{slug}`"}

    ad_accounts = STORE_AD_ACCOUNTS.get(store.slug, [])
    if not ad_accounts:
        return {**state, "reply_text": f"No Meta ad accounts mapped for `{slug}`."}

    all_ads: list[dict] = []
    for act in ad_accounts:
        try:
            rows = await ads_for_account(act, days=days)
            all_ads.extend(rows)
        except MetaGraphError as e:
            import logging
            logging.getLogger(__name__).warning(
                "ads_for_account %s failed: %s", act, str(e)[:300]
            )
            continue

    # Filter: only show ads that had any spend in window (active or paused, doesn't matter)
    all_ads = [a for a in all_ads if a["spend"] > 0]
    if not all_ads:
        return {**state, "reply_text": f"*{store.brand}* · last {days}d\nNo ads with spend in this window."}

    # Sort by spend desc
    all_ads.sort(key=lambda a: a["spend"], reverse=True)
    top = all_ads[:LEADER_TOP_N]

    totals = {
        "spend": sum(a["spend"] for a in all_ads),
        "purchases": sum(a["purchases"] for a in all_ads),
        "revenue": sum(a["purchase_value"] for a in all_ads),
        "clicks": sum(a["clicks"] for a in all_ads),
    }
    acct_roas = (totals["revenue"] / totals["spend"]) if totals["spend"] > 0 else 0
    avg_cpc = (totals["spend"] / totals["clicks"]) if totals["clicks"] > 0 else 0

    lines: list[str] = []
    lines.append(f"*{store.brand}* · last {days}d · top {len(top)} ads by spend")
    lines.append(
        f"{len(all_ads)} ads with spend · account totals: "
        f"spend {totals['spend']:,.0f} · purchases {totals['purchases']} · "
        f"reported rev {totals['revenue']:,.0f} · ROAS {acct_roas:.2f}x · avg CPC {avg_cpc:.2f}"
    )
    lines.append("")

    for i, a in enumerate(top, 1):
        status_emoji = "▶️" if a["status"] == "ACTIVE" else "⏸"
        roas = a["reported_roas"]
        lines.append(
            f"`{i:>2}` {status_emoji} *{a['ad_name'][:40]}*"
        )
        lines.append(
            f"    spend {a['spend']:,.0f}  ·  CTR {a['ctr']:.2f}%  ·  CPC {a['cpc']:.2f}  ·  "
            f"purchases {a['purchases']}  ·  rev {a['purchase_value']:,.0f}  ·  ROAS {roas:.2f}x"
        )
        lines.append(f"    {_creative_hint(a['creative'])}")
        lines.append(f"    `/creative {a['ad_id']}`")
        lines.append("")

    out = "\n".join(lines)
    if len(out) > TELEGRAM_MSG_LIMIT:
        out = out[:TELEGRAM_MSG_LIMIT] + "\n…(truncated)"
    return {**state, "reply_text": out}
