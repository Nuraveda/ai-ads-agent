"""amazon_insights: Amazon Seller + Amazon Ads rollup for a store.

Source-of-truth via Marketplace Ad Pros (MAP) MCP. MAP proxies the
Amazon Ads Partner Network API and returns authoritative per-account
totals fresh-within-the-hour.

Trigger: `/amazon <store> [days]` from Telegram or Discord.
Dispatcher route key: `amazon`.

Looks up MAP integration_id + account_id for the slug from
`STORE_MAP_ACCOUNTS_JSON` env, then calls
`ads_agent.map.mcp_client.ads_totals(...)`.

Airbyte pipeline torn down 2026-05-08; if a Seller Central block is
needed in the future, source it from MAP's `selling_partner` integration
(see brand listing for `account_type=seller` accounts).
"""
from __future__ import annotations

import json
import logging
import os

from ads_agent.config import get_store
from ads_agent.map.mcp_client import MapMcpError, ads_totals as _map_ads_totals

log = logging.getLogger(__name__)


def _map_account_for(slug: str) -> tuple[str, str] | None:
    raw = os.environ.get("STORE_MAP_ACCOUNTS_JSON", "").strip()
    if not raw:
        return None
    try:
        m = json.loads(raw)
    except json.JSONDecodeError:
        return None
    cfg = m.get(slug)
    if not (cfg and cfg.get("integration_id") and cfg.get("account_id")):
        return None
    return cfg["integration_id"], cfg["account_id"]


async def amazon_insights_node(state: dict) -> dict:
    slug = state["store_slug"]
    days = int(state.get("days", 7))
    store = get_store(slug)
    if store is None:
        return {**state, "reply_text": f"Unknown store: `{slug}`"}

    map_acct = _map_account_for(slug)
    if not map_acct:
        return {
            **state,
            "reply_text": (
                f"*{store.brand}* · Amazon Ads\n\n"
                f"No MAP account mapped for `{slug}`. Add it to "
                f"`STORE_MAP_ACCOUNTS_JSON` in .env."
            ),
        }
    integration_id, account_id = map_acct

    try:
        t = await _map_ads_totals(integration_id, account_id, days)
    except MapMcpError as e:
        return {
            **state,
            "reply_text": f"*{store.brand}* · Amazon Ads error: `{e}`",
        }

    cost = float(t.get("cost") or 0)
    sales = float(t.get("sales14d") or 0)
    purch = int(t.get("purchases14d") or 0)
    clicks = int(t.get("clicks") or 0)
    imp = int(t.get("impressions") or 0)
    ccy = t.get("currency", "")
    roas = (sales / cost) if cost else 0.0
    acos = (cost / sales * 100) if sales else 0.0
    ctr = (clicks / imp * 100) if imp else 0.0
    cpc = (cost / clicks) if clicks else 0.0

    lines = [
        f"*{store.brand} · Amazon Ads* · last {days}d ({ccy})",
        f"  spend {ccy} {cost:,.2f} · sales (14d) {ccy} {sales:,.2f} · "
        f"purchases (14d) {purch}",
        f"  ROAS {roas:.2f}x · ACOS {acos:.1f}% · "
        f"{clicks:,} clicks / {imp:,} imp · CTR {ctr:.2f}% · CPC {ccy} {cpc:.2f}",
        "",
        "_source: MAP (Marketplace Ad Pros) → Amazon Ads Partner Network_",
    ]
    return {**state, "reply_text": "\n".join(lines)}
