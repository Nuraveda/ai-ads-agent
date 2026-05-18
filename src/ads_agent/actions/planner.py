"""Planner — scans live Meta + Amazon data and emits ActionProposals.

Runs every 4 hours. For each enabled store:
  1. Fetch active adsets + spend + purchase_roas (last 14d) from Meta Graph API.
  2. Apply the configured RULES to find pause / budget-change candidates.
  3. Dedup against existing pending/approved/executed actions in the last
     DEDUP_HOURS — never re-propose the same target_id twice within that
     window. Rows with status IN ('notify_failed','expired') are excluded
     from dedup so a Telegram outage or TTL can't black-hole a target.
  4. Persist survivors as pending_approval rows via notifier.post_proposal —
     notifier then sends the Telegram approval prompt.

v2 (2026-04-22): added `plan_amazon_for_store` which asks MAP's
ask_report_analyst for 5 wasteful-spend targets per store, parses the
structured response, and emits `amazon_pause_ad` / `amazon_add_negative_keyword`
proposals on the same HITL approval path. Gated by AMAZON_PLANNER_ENABLED
env var so the systemd cron doesn't auto-run Amazon until operator flips
it on — keeps v2 safe behind a manual trigger while the pattern settles.

## Public engine / private playbook split

The **orchestration** lives here in the public repo (fetching Meta data,
dedup, posting proposals, handling notify_failed). The **calibrated
business logic** — which rules fire, what thresholds they use, what
rationale copy appears in the Telegram approval prompt — lives in the
private package `ai_marketing_stack_ads_playbook`.

At import time we try to load RULES + DEDUP_HOURS from the playbook and
fall back to the generic stub (`ads_agent.actions.rules_stub`) if the
playbook isn't installed. That keeps this repo runnable end-to-end for
a public cloner while keeping the tuned numbers proprietary.

See: https://github.com/glitch-exec-labs/ai_marketing_stack-ads-agent-private
"""
from __future__ import annotations

import json
import logging
import os
from typing import Callable

import asyncpg
import httpx

from ads_agent.actions.guardrails import GuardrailViolation
from ads_agent.actions.models import ActionProposal, LIGHTHOUSE_CHAT_ID
from ads_agent.actions.notifier import TelegramNotifyError, post_proposal
from ads_agent.amazon.ads_api import (
    AmazonAdsError as MapMcpError,    # alias keeps existing except-clauses working
    list_resources,
    profile_id_for,
)


# ask_analyst was MAP's MCP-side AI report query. Native Amazon Ads has
# no equivalent (Reports v3 returns raw rows; the methodology analyst we
# ship in /amazon_recs is the real replacement). For backward compat the
# planner's Amazon-waste-finding path now no-ops; operators should run
# /amazon_recs which goes through the methodology analyst.
async def ask_analyst(*_args, **_kwargs):
    """No-op stub — MAP's analyst is gone; use /amazon_recs instead."""
    return {"_replaced_by": "amazon_recs methodology analyst", "data": []}


async def map_call(tool: str, args: dict):
    """Backward-compat shim — routes list_resources through the native client."""
    if tool == "list_resources":
        # Caller passes integration_id + account_id (legacy MAP shape) — these
        # are now derivable from the slug. We can't recover slug from the
        # MAP-shaped args alone, so the planner's Amazon path is now disabled
        # until /scan_amazon is re-wired to pass slug.
        raise MapMcpError(
            "map_call('list_resources', ...) deprecated — pass slug to "
            "ads_agent.amazon.ads_api.list_resources directly"
        )
    raise MapMcpError(f"unsupported legacy tool: {tool}")

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com/v21.0"


# ---------------------------------------------------------------------------
# Rule set resolution — private playbook first, generic stub as fallback
# ---------------------------------------------------------------------------
try:
    from ai_marketing_stack_ads_playbook.rules import (  # type: ignore[import-not-found]
        DEDUP_HOURS,
        RULES,
    )
    _RULE_SOURCE = "ai_marketing_stack_ads_playbook (private, tuned)"
except ImportError:
    from ads_agent.actions.rules_stub import DEDUP_HOURS, RULES  # noqa: F401
    _RULE_SOURCE = "rules_stub (public demo — calibration not installed)"

log.info("planner: loaded %d rule(s) from %s", len(RULES), _RULE_SOURCE)


def _meta_token() -> str:
    tok = os.environ.get("META_ACCESS_TOKEN", "").strip()
    if not tok:
        raise RuntimeError("META_ACCESS_TOKEN not set")
    return tok


def _store_to_account(store_slug: str) -> list[str]:
    """Map store slug → Meta ad account IDs. Reads STORE_AD_ACCOUNTS_JSON."""
    raw = os.environ.get("STORE_AD_ACCOUNTS_JSON", "").strip()
    if not raw:
        return []
    try:
        m = json.loads(raw)
    except Exception:
        return []
    return list(m.get(store_slug, []))


async def _fetch_adset_snapshot(ad_account_id: str) -> list[dict]:
    """Pull last-14-day adset-level spend + purchase_roas from Meta Graph API."""
    async with httpx.AsyncClient(timeout=60.0) as c:
        # insights with purchase_roas (omni_purchase)
        ins = (await c.get(
            f"{GRAPH_BASE}/{ad_account_id}/insights",
            params={
                "level": "adset",
                "date_preset": "last_14d",
                "fields": "adset_id,adset_name,campaign_name,spend,clicks,impressions,purchase_roas",
                "limit": 500,
                "access_token": _meta_token(),
            },
        )).json().get("data", [])

        if not ins:
            return []

        # Metadata for each adset (effective_status, daily_budget)
        ids = [r["adset_id"] for r in ins]
        meta_by_id: dict[str, dict] = {}
        CHUNK = 30
        for i in range(0, len(ids), CHUNK):
            chunk = ids[i:i + CHUNK]
            body = (await c.get(
                f"{GRAPH_BASE}/",
                params={
                    "ids": ",".join(chunk),
                    "fields": "id,name,effective_status,daily_budget,lifetime_budget,campaign{name}",
                    "access_token": _meta_token(),
                },
            )).json()
            for k, v in body.items():
                if isinstance(v, dict) and v.get("id"):
                    meta_by_id[v["id"]] = v

    rows: list[dict] = []
    for r in ins:
        m = meta_by_id.get(r["adset_id"], {})
        eff = m.get("effective_status", "UNKNOWN")
        # Skip: archived/deleted (obvious) AND campaign-paused (pausing an
        # already-effectively-paused adset would be a no-op). Keep adset-level
        # PAUSED because the planner may want to RESUME it.
        if eff not in ("ACTIVE", "ADSET_PAUSED", "PAUSED"):
            continue
        if eff == "CAMPAIGN_PAUSED":
            continue  # won't consider until campaign is un-paused
        roas = None
        for pr in r.get("purchase_roas", []) or []:
            if pr.get("action_type") == "omni_purchase":
                roas = float(pr.get("value", 0) or 0)
        rows.append({
            "adset_id":      r["adset_id"],
            "adset_name":    r.get("adset_name"),
            "campaign_name": r.get("campaign_name"),
            "spend_14d":     float(r.get("spend", 0) or 0),
            "clicks_14d":    int(r.get("clicks", 0) or 0),
            "impressions_14d": int(r.get("impressions", 0) or 0),
            "roas":          roas,
            "effective_status": eff,
            "daily_budget":  int(m.get("daily_budget") or 0),  # Meta returns minor units (paise)
        })
    return rows


async def _recent_targets(pool: asyncpg.Pool, hours: int = DEDUP_HOURS) -> set[str]:
    """Return set of target_object_id strings with a still-relevant action row
    created within N hours.

    Issue #6: rows with status='notify_failed' represent proposals where the
    database row exists but no human ever saw the approval prompt. Those
    MUST NOT block re-proposal — otherwise a transient Telegram outage can
    black-hole a target for 72 hours. We exclude them here so the next
    planner tick gets another shot at posting. Same rationale for 'expired'
    rows (human never acted, TTL elapsed).
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """SELECT DISTINCT target_object_id
               FROM ads_agent.agent_actions
               WHERE created_at > NOW() - ($1 || ' hours')::interval
                 AND status NOT IN ('notify_failed', 'expired')""",
            str(hours),
        )
    return {r["target_object_id"] for r in rows}


# ---------------------------------------------------------------------------
# Amazon planner — MAP analyst → structured proposals
# ---------------------------------------------------------------------------

# Structured prompt asks for rows we can parse into ActionProposal inputs.
# Kept narrow so we don't burn analyst quota. 5 items max keeps the Telegram
# posting volume reasonable for a single run.
_AMAZON_PLAN_PROMPT = (
    "For the last 14 days on this Sponsored Products account, return up to 5 "
    "of the HIGHEST-IMPACT waste-reduction actions. For each action return a "
    "structured row with exactly these columns: "
    "action_type (one of: pause_ad, add_negative_keyword), "
    "campaign_name, campaign_id, ad_group_name, ad_group_id, "
    "ad_id (only for pause_ad; null otherwise), "
    "asin (only for pause_ad; null otherwise), "
    "keyword_text (only for add_negative_keyword; null otherwise), "
    "match_type (one of: NEGATIVE_EXACT, NEGATIVE_PHRASE; null for pause_ad), "
    "cost_14d (number, native currency), purchases_14d (integer), "
    "clicks_14d (integer), est_weekly_savings (number). "
    "Only include actions where cost_14d > 50 AND purchases_14d = 0 over the "
    "window. Sort by est_weekly_savings descending. No prose, just rows."
)


def _amazon_rows_from_analyst(analyst_data: dict) -> list[dict]:
    """Extract the structured rows the analyst returned + infer action_type
    if missing.

    MAP's `ask_report_analyst` doesn't always honor the "action_type" column
    in the prompt — often it just returns the raw waste-performance rows.
    We infer the right action from the row's shape:

      - has `ad_id` or `asin` (but no keyword_text) → pause_ad
      - has `keyword_text` with a regular match_type (EXACT/PHRASE/BROAD)
          → add_negative_keyword  (we'll add it as NEGATIVE_EXACT by default)
      - has `match_type = TARGETING_EXPRESSION` (product/category target)
          → skip for v1; needs a separate pause_target action_kind we
          haven't built yet
      - has explicit `action_type = pause_ad | add_negative_keyword` → trust it
    """
    if not isinstance(analyst_data, dict):
        return []
    rows = analyst_data.get("data") or []
    if not isinstance(rows, list):
        return []

    cleaned: list[dict] = []
    for r in rows:
        if not isinstance(r, dict):
            continue

        # Skip product-targeting rows — out of v1 scope
        mt = (r.get("match_type") or "").upper()
        if mt == "TARGETING_EXPRESSION":
            continue
        kw = (r.get("keyword_text") or "").strip()
        if kw.startswith("asin-expanded="):
            continue

        explicit = (r.get("action_type") or "").strip().lower()
        if explicit in ("pause_ad", "add_negative_keyword"):
            at = explicit
        elif kw and mt in ("EXACT", "PHRASE", "BROAD", "NEGATIVE_EXACT", "NEGATIVE_PHRASE"):
            # Keyword row → add negative. Force match_type to NEGATIVE_EXACT
            # unless the analyst explicitly said NEGATIVE_PHRASE.
            at = "add_negative_keyword"
            if mt != "NEGATIVE_PHRASE":
                r["match_type"] = "NEGATIVE_EXACT"
        elif r.get("ad_id") or r.get("adId") or r.get("asin"):
            at = "pause_ad"
        else:
            continue

        # Populate the inferred action_type so downstream can rely on it
        r = {**r, "action_type": at}

        # Per-type field sanity
        # pause_ad: require either ad_id (direct target) OR (ad_group_id + asin)
        # so we can hydrate ad_id from MAP before converting to proposal.
        if at == "pause_ad":
            has_direct = bool(r.get("ad_id") or r.get("adId"))
            has_lookup = bool(
                (r.get("ad_group_id") or r.get("adGroupId"))
                and r.get("asin")
            )
            if not (has_direct or has_lookup):
                continue
        if at == "add_negative_keyword" and not r.get("keyword_text"):
            continue
        cleaned.append(r)

    return cleaned


def _proposal_from_amazon_row(
    *, store_slug: str, row: dict, integration_id: str, account_id: str
) -> ActionProposal | None:
    """Convert one analyst row into an ActionProposal. Returns None if the
    row is missing required fields for its action_type."""
    at = (row.get("action_type") or "").strip().lower()
    campaign_name = row.get("campaign_name") or row.get("campaignName") or "?"
    ad_group_name = row.get("ad_group_name") or row.get("adGroupName") or "?"
    cost = float(row.get("cost_14d") or 0)
    purch = int(row.get("purchases_14d") or 0)
    clicks = int(row.get("clicks_14d") or 0)
    savings = float(row.get("est_weekly_savings") or 0)

    evidence = {
        "cost_14d": round(cost, 2),
        "purchases_14d": purch,
        "clicks_14d": clicks,
        "campaign": campaign_name,
        "ad_group": ad_group_name,
    }
    expected_impact = {"estimated_weekly_savings": round(savings, 2)}

    if at == "pause_ad":
        ad_id = str(row.get("ad_id") or row.get("adId") or "")
        asin = row.get("asin") or row.get("advertisedAsin") or "?"
        if not ad_id:
            return None
        return ActionProposal(
            store_slug=store_slug,
            action_kind="amazon_pause_ad",
            target_object_id=ad_id,
            target_object_name=f"{asin} in {campaign_name} / {ad_group_name}",
            rationale=(
                f"ASIN {asin} in {campaign_name} / {ad_group_name} has spent "
                f"{cost:,.2f} over the last 14 days with {purch} purchases "
                f"({clicks} clicks). Pausing this ad reclaims "
                f"~{savings:,.2f}/week with no revenue loss."
            ),
            params={
                "integration_id": integration_id,
                "account_id": account_id,
                "asin": asin,
                "campaign_id": row.get("campaign_id") or row.get("campaignId"),
                "ad_group_id": row.get("ad_group_id") or row.get("adGroupId"),
            },
            evidence=evidence,
            expected_impact=expected_impact,
        )

    if at == "add_negative_keyword":
        ag_id = str(row.get("ad_group_id") or row.get("adGroupId") or "")
        kw = row.get("keyword_text") or ""
        mt = (row.get("match_type") or "NEGATIVE_EXACT").upper()
        if mt not in ("NEGATIVE_EXACT", "NEGATIVE_PHRASE"):
            mt = "NEGATIVE_EXACT"
        if not ag_id or not kw:
            return None
        return ActionProposal(
            store_slug=store_slug,
            action_kind="amazon_add_negative_keyword",
            target_object_id=ag_id,
            target_object_name=f"{kw!r} on {campaign_name} / {ad_group_name}",
            rationale=(
                f"Search term {kw!r} in {campaign_name} / {ad_group_name} "
                f"has spent {cost:,.2f} over 14 days with {purch} purchases "
                f"({clicks} clicks). Adding as {mt} prevents future spend on "
                f"this term. Estimated reclaim ~{savings:,.2f}/week."
            ),
            params={
                "integration_id": integration_id,
                "account_id": account_id,
                "campaign_id": row.get("campaign_id") or row.get("campaignId"),
                "adGroupId": ag_id,
                "keyword_text": kw,
                "match_type": mt,
            },
            evidence=evidence,
            expected_impact=expected_impact,
        )
    return None


def _target_for(store_slug: str, chat_id_fallback: int):
    """Resolve ProposalTarget for store, falling back to chat_id-only if
    STORE_PROPOSAL_TARGETS_JSON isn't configured for this slug."""
    from ads_agent.actions.approval_targets import ProposalTarget, proposal_target
    t = proposal_target(store_slug)
    if t and t.has_any:
        return t
    return ProposalTarget(telegram_chat_id=chat_id_fallback, discord_channel_id=None)


async def plan_amazon_for_store(
    pool: asyncpg.Pool, store_slug: str, chat_id: int, *, force: bool = False,
) -> int:
    """Ask MAP's analyst for waste-reduction opportunities, convert each into
    an ActionProposal, dedup, and post for Approve/Reject. Returns count posted.

    Gated by AMAZON_PLANNER_ENABLED env var — set to '1' to enable for
    automated cron runs. `force=True` bypasses the env gate (used by the
    manual `/scan_amazon <store>` Telegram command) so the operator can
    always manually trigger a scan while keeping the cron off.
    """
    if not force and os.environ.get("AMAZON_PLANNER_ENABLED", "").strip() != "1":
        log.info(
            "Amazon planner disabled (AMAZON_PLANNER_ENABLED != '1') — "
            "skipping store %s", store_slug,
        )
        return 0

    # Native LWA path — confirm slug has an Amazon Ads profile mapped
    try:
        await profile_id_for(store_slug)
    except MapMcpError as e:
        log.info("no Amazon Ads profile mapped for %s — skipping: %s", store_slug, e)
        return 0

    # MAP's `ask_analyst` waste-finding prompt has no native equivalent —
    # native Reports v3 returns raw rows; the AI-style "give me 5 waste
    # targets" lives in the methodology analyst (run via /amazon_recs).
    # The planner's standalone Amazon path is now no-op; operators should
    # use /scan_amazon → which we kept working as a /amazon_recs trigger.
    log.info(
        "Amazon planner now no-ops — `ask_analyst` retired; "
        "operators should use /amazon_recs (methodology analyst) instead. "
        "Returning 0 proposals for %s.", store_slug,
    )
    return 0

    # ── below code unreachable but kept for reference until /scan_amazon
    # is rewired to call the methodology analyst directly ──
    # Native lookup of product_ads by campaign, used to resolve ad_id from
    # (adGroupId, asin) when a proposal row lacks ad_id.
    ad_lookup: dict[tuple[str, str], str] = {}
    campaign_cache: set[str] = set()
    rows: list[dict] = []  # placeholder so the rest type-checks

    async def _hydrate_campaign(campaign_id: str) -> None:
        if campaign_id in campaign_cache or not campaign_id:
            return
        campaign_cache.add(campaign_id)
        try:
            data_ads, _ = await list_resources(
                slug=store_slug, resource_type="sp_product_ads",
                campaign_id=campaign_id, state_filter="ENABLED",
            )
            for item in (data_ads or {}).get("items", []):
                key = (str(item.get("adGroupId")), str(item.get("asin") or ""))
                ad_lookup[key] = str(item.get("adId"))
        except MapMcpError as e:
            log.warning("native product-ads lookup failed for campaign %s: %s", campaign_id, e)

    # Resolve ad_id for each pause_ad row that doesn't already have one
    for r in rows:
        if r.get("action_type") == "pause_ad" and not (r.get("ad_id") or r.get("adId")):
            cid = str(r.get("campaign_id") or r.get("campaignId") or "")
            await _hydrate_campaign(cid)
            key = (str(r.get("ad_group_id") or r.get("adGroupId")), str(r.get("asin") or ""))
            adid = ad_lookup.get(key)
            if adid:
                r["ad_id"] = adid

    recent = await _recent_targets(pool)
    posted = 0
    for row in rows:
        prop = _proposal_from_amazon_row(
            store_slug=store_slug, row=row,
            integration_id=cfg["integration_id"], account_id=cfg["account_id"],
        )
        if prop is None:
            continue
        if prop.target_object_id in recent:
            continue  # dedup against recent actions on the same target
        try:
            await post_proposal(pool, prop, target=_target_for(store_slug, chat_id))
            posted += 1
            recent.add(prop.target_object_id)
        except GuardrailViolation as e:
            log.info(
                "guardrail skip Amazon %s: %s", prop.target_object_id, e,
            )
            recent.add(prop.target_object_id)
        except TelegramNotifyError as e:
            log.warning(
                "notify failed for Amazon target %s (will retry next tick): %s",
                prop.target_object_id, e,
            )
        except Exception:
            log.exception("post_proposal failed for Amazon target %s", prop.target_object_id)
    return posted


async def plan_for_store(pool: asyncpg.Pool, store_slug: str, chat_id: int) -> int:
    """Generate + post proposals for one store. Returns count posted."""
    recent = await _recent_targets(pool)
    posted = 0
    for acct in _store_to_account(store_slug):
        try:
            snapshot = await _fetch_adset_snapshot(acct)
        except Exception as e:
            log.warning("snapshot fetch failed for %s: %s", acct, e)
            continue

        for r in snapshot:
            r["_store_slug"] = store_slug
            if r["adset_id"] in recent:
                continue  # dedup

            for rule in RULES:
                prop = rule(r)
                if prop is None:
                    continue
                try:
                    await post_proposal(pool, prop, target=_target_for(store_slug, chat_id))
                    posted += 1
                    recent.add(r["adset_id"])  # don't double-propose same target
                    break  # one proposal per adset per planner run
                except GuardrailViolation as e:
                    # Pre-write check rejected this proposal (e.g. already paused).
                    # Add to `recent` so we don't re-evaluate this target again
                    # this tick, but don't retry on the next tick either — the
                    # target state isn't coming back to "actionable" on its own.
                    log.info("guardrail skip %s: %s", r["adset_id"], e)
                    recent.add(r["adset_id"])
                    break
                except TelegramNotifyError as e:
                    # Row exists as 'notify_failed' — do NOT add to `recent`
                    # so the next planner tick can retry. Issue #6.
                    log.warning(
                        "notify failed for %s (will retry next tick): %s",
                        r["adset_id"], e,
                    )
                    break
                except Exception as e:
                    log.exception("post_proposal failed for %s: %s", r["adset_id"], e)
    return posted
