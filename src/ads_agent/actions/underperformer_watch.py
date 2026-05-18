"""Urban-family underperformer watch — Discord alert (no auto-pause).

Checks every active ad in the 4 Urban-family Meta ad accounts. Any ad
whose lifetime spend ≥ $20 (in account currency, per `STORE_AD_ACCOUNTS_JSON`)
AND lifetime omni_purchases < 4 is flagged as an underperformer and posted
to the `#urban-family-alert` Discord channel as a "keep watch or kill"
notice.

Design notes:
  * Alert-only — no auto-pause. The Urban family is ad-sensitive; HITL
    norms in the playbook gate pause actions, so we surface the signal
    and leave the kill decision to the operator.
  * Dedup window = today (UTC). Once an ad is alerted in a calendar day
    it won't be re-alerted in the same day, even if it stays
    underperforming across the next 30-min cycles.
  * "Lifetime" approximated as last 90 days — the Urban-family ad accounts
    are <2 years old and Meta's API has no native `lifetime` preset for
    custom date ranges; 90d is generous enough to capture every ad we'd
    ever watch under this rule.
  * Targets only 4 hardcoded ad-account IDs (the Urban family) to avoid
    accidentally firing on example / Mokshya if STORE_AD_ACCOUNTS_JSON
    is edited.

Run:
    python -m ads_agent.actions.underperformer_watch              # one cycle
    python -m ads_agent.actions.underperformer_watch --dry-run    # log only
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ads_agent.discord.poster import post_message
from ads_agent.meta.graph_client import MetaGraphError, ads_for_account

log = logging.getLogger("urban_watch")

# --- Config ------------------------------------------------------------------

# Hardcoded — see SHOPIFY_STORES_INFRA.md "Urban family" section.
URBAN_ACCOUNTS: tuple[tuple[str, str], ...] = (
    ("act_1909845012991177", "Urban Classics"),
    ("act_755235000581939",  "Storico"),
    ("act_1231977889107681", "Classicoo"),
    ("act_1445770643706149", "Trendsetters"),
)

ALERT_CHANNEL_ID = "1499205675950014617"  # #urban-family-alert

THRESHOLD_SPEND_USD = 20.0  # account-currency units, per the user spec
THRESHOLD_PURCHASES = 4     # < this = alert

# Persistent dedup state across cron firings.
STATE_DIR = Path(os.environ.get("URBAN_WATCH_STATE_DIR", "/home/support/.local/state/glitch-ads-bot"))
STATE_FILE = STATE_DIR / "urban_underperformer_alerted.json"


# --- State -------------------------------------------------------------------

def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError:
        log.warning("state file corrupt at %s — resetting", STATE_FILE)
        return {}


def _save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def _alerted_today(state: dict, ad_id: str) -> bool:
    return state.get(ad_id, {}).get("date") == _today_utc()


def _mark_alerted(state: dict, ad_id: str, snap: dict) -> None:
    state[ad_id] = {"date": _today_utc(), **snap}


# --- Detection ---------------------------------------------------------------

@dataclass(frozen=True)
class Underperformer:
    account_id: str
    store_name: str
    ad_id: str
    ad_name: str
    spend: float
    purchases: int
    currency: str
    days_live: float

    @property
    def cpa(self) -> float | None:
        return (self.spend / self.purchases) if self.purchases else None

    def ads_manager_url(self) -> str:
        # Direct deep-link to the ad in Ads Manager
        acct = self.account_id.removeprefix("act_")
        return (
            f"https://business.facebook.com/adsmanager/manage/ads"
            f"?act={acct}&selected_ad_ids={self.ad_id}"
        )


async def find_underperformers() -> list[Underperformer]:
    """Pull ad-level insights for all 4 Urban accounts and filter."""
    out: list[Underperformer] = []
    for account_id, store_name in URBAN_ACCOUNTS:
        try:
            ads = await ads_for_account(account_id, days=90, limit=500)
        except MetaGraphError as e:
            log.error("meta error for %s (%s): %s", account_id, store_name, e)
            continue
        for a in ads:
            # Skip non-active ads (PAUSED / DELETED / pending review etc.)
            eff = (a.get("effective_status") or "").upper()
            if eff != "ACTIVE":
                continue
            spend = float(a.get("spend") or 0)
            purchases = int(a.get("purchases") or 0)
            if spend >= THRESHOLD_SPEND_USD and purchases < THRESHOLD_PURCHASES:
                out.append(
                    Underperformer(
                        account_id=account_id,
                        store_name=store_name,
                        ad_id=str(a.get("ad_id") or ""),
                        ad_name=str(a.get("ad_name") or "(unnamed)"),
                        spend=spend,
                        purchases=purchases,
                        currency=str(a.get("currency") or "?"),
                        days_live=float(a.get("days_live") or 0),
                    )
                )
    return out


# --- Formatting --------------------------------------------------------------

def format_alert(u: Underperformer) -> str:
    cpa = f"{u.currency} {u.cpa:,.2f}" if u.cpa is not None else "— (no purchases)"
    spend = f"{u.currency} {u.spend:,.2f}"
    return (
        f"⚠️ **Underperformer — {u.store_name}**\n"
        f"**Ad:** {u.ad_name}\n"
        f"`{u.ad_id}` · live {u.days_live:.1f}d\n"
        f"Spend **{spend}** · Purchases **{u.purchases}** "
        f"(threshold ≥ {THRESHOLD_SPEND_USD:.0f} & < {THRESHOLD_PURCHASES})\n"
        f"CPA: **{cpa}**\n"
        f"Keep a watch or kill it → <{u.ads_manager_url()}>"
    )


# --- Main --------------------------------------------------------------------

async def run(dry_run: bool = False) -> dict:
    state = _load_state()
    flagged = await find_underperformers()
    new_alerts: list[Underperformer] = [u for u in flagged if not _alerted_today(state, u.ad_id)]

    log.info(
        "urban-watch: scanned 4 accounts, %d flagged, %d new (dedup=%d)",
        len(flagged), len(new_alerts), len(flagged) - len(new_alerts),
    )

    posted = 0
    for u in new_alerts:
        body = format_alert(u)
        if dry_run:
            log.info("[dry-run] would post:\n%s\n", body)
        else:
            try:
                await post_message(ALERT_CHANNEL_ID, body)
                posted += 1
            except Exception as e:
                log.error("discord post failed for ad=%s: %s", u.ad_id, e)
                continue
        _mark_alerted(state, u.ad_id, {
            "store": u.store_name,
            "ad_name": u.ad_name,
            "spend": u.spend,
            "purchases": u.purchases,
            "currency": u.currency,
        })

    if not dry_run:
        _save_state(state)

    return {"flagged": len(flagged), "new_alerts": len(new_alerts), "posted": posted}


def _cli() -> int:
    # Load env BEFORE importing settings — dotenv is permissive about JSON
    # values that bash `source` would mis-parse (STORES_JSON, SHOPIFY_WEBHOOK_SECRETS).
    from dotenv import load_dotenv
    load_dotenv("/home/support/ai_marketing_stack-ads-agent/.env")
    load_dotenv("/home/support/.config/glitch-discord/env")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    p = argparse.ArgumentParser(description="Urban-family underperformer watch.")
    p.add_argument("--dry-run", action="store_true", help="Log alerts but don't post or save state.")
    args = p.parse_args()
    summary = asyncio.run(run(dry_run=args.dry_run))
    log.info("done: %s", summary)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
