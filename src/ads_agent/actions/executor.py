"""Executor — picks up approved actions and runs them via glitch-ads-mcp.

Run every 5 minutes by a systemd timer. Claims rows atomically (status
approved → executing via UPDATE ... WHERE status='approved' RETURNING) so
concurrent runs don't double-execute.

Errors are recorded on the action row itself (status='failed', result={}).
Success updates status='executed', result=<MCP response>, executed_at=NOW.
On success, posts a confirmation to the same Telegram thread where the
proposal was approved.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import asyncpg

from ads_agent.actions.models import ACTION_TO_MCP
from ads_agent.actions.notifier import post_text
from ads_agent.amazon.ads_api import list_resources as native_list_resources
from ads_agent.amazon.mutations import call_native as native_amazon_call
from ads_agent.meta.mcp_client import call_tool as meta_call

log = logging.getLogger(__name__)


async def _fetch_prior_state(action_kind: str, target_id: str, params: dict) -> dict[str, Any]:
    """Before we mutate, snapshot the current state so a rollback is possible."""
    try:
        if action_kind in ("pause_adset", "resume_adset", "update_adset_budget"):
            return await meta_call("get_adset_details", {"adset_id": target_id})
        if action_kind == "pause_ad":
            return await meta_call("get_ad_details", {"ad_id": target_id})
        if action_kind == "amazon_pause_ad":
            data, _ = await native_list_resources(
                slug=params.get("slug") or params.get("store_slug"),
                resource_type="sp_product_ads",
                campaign_id=params.get("campaign_id"),
            )
            return data
        if action_kind == "amazon_add_negative_keyword":
            data, _ = await native_list_resources(
                slug=params.get("slug") or params.get("store_slug"),
                resource_type="sp_negative_keywords",
                campaign_id=params.get("campaign_id"),
            )
            return data
    except Exception as e:
        log.warning("prior-state fetch failed for %s %s: %s", action_kind, target_id, e)
    return {}


def _is_no_op(kind: str, prior: dict, params: dict, target_id: str = "") -> tuple[bool, str]:
    """Is this action already satisfied by the target's current state?
    Guards against no-op API calls (e.g. pausing an adset whose campaign is
    already paused, or setting a budget to a value it already has).
    """
    # prior may wrap under 'adset' / 'ad' keys from get_*_details
    info = prior
    for key in ("adset", "ad", "result"):
        if isinstance(info, dict) and key in info and isinstance(info[key], dict):
            info = info[key]
    eff = (info.get("effective_status") or "").upper() if isinstance(info, dict) else ""

    if kind in ("pause_adset", "pause_ad") and eff in (
        "PAUSED", "ADSET_PAUSED", "CAMPAIGN_PAUSED", "DISAPPROVED", "ARCHIVED", "DELETED",
    ):
        return True, f"target already effectively paused (effective_status={eff}); skipping API call"
    if kind == "resume_adset" and eff == "ACTIVE":
        return True, "target already ACTIVE; resume is a no-op"
    if kind == "update_adset_budget":
        current = info.get("daily_budget")
        new = params.get("new_daily_budget")
        if current is not None and new is not None and int(current) == int(new):
            return True, f"daily_budget already ₹{int(current)/100:,.0f}; no change needed"

    # --- Amazon no-op guards ---
    if kind == "amazon_pause_ad":
        # If the ad isn't in the ENABLED list, it's already paused/archived.
        items = (prior or {}).get("items") or []
        if items:
            active_ids = {str(it.get("adId")) for it in items}
            if target_id and target_id not in active_ids:
                return True, (
                    f"ad {target_id} is no longer in the ENABLED set — "
                    "already paused or archived; skipping API call"
                )
    if kind == "amazon_add_negative_keyword":
        # If an identical negative (same adGroupId + keywordText + matchType)
        # already exists, this create would collide / waste quota.
        items = (prior or {}).get("items") or []
        kw = (params.get("keyword_text") or "").strip().lower()
        mt = params.get("match_type", "NEGATIVE_EXACT")
        ag = params.get("adGroupId") or target_id
        for it in items:
            if (str(it.get("adGroupId")) == str(ag)
                    and (it.get("keywordText") or "").strip().lower() == kw
                    and it.get("matchType") == mt
                    and (it.get("state") or "").lower() in ("enabled", "paused")):
                return True, (
                    f"negative keyword {kw!r} ({mt}) already exists on ad group {ag}"
                )
    return False, ""


async def _execute_one(pool: asyncpg.Pool, action: dict) -> None:
    action_id = action["id"]
    kind      = action["action_kind"]
    target_id = action["target_object_id"]

    client_tag, mcp_tool, params_fn = ACTION_TO_MCP[kind]

    # Snapshot prior state (best-effort, not fatal). Pass action.params so
    # Amazon snapshotters have integration_id + account_id available.
    prior = await _fetch_prior_state(kind, target_id, action.get("params") or {})

    # Build tool arguments + NO-OP GUARD before firing anything at Meta/MAP
    tool_args = params_fn(action)
    is_noop, noop_reason = _is_no_op(kind, prior, action.get("params") or {}, target_id)
    if is_noop:
        log.warning("action %s is a no-op: %s — marking 'executed' with skip flag",
                    action_id, noop_reason)
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE ads_agent.agent_actions
                   SET status='executed', executed_at=NOW(),
                       prior_state=$1::jsonb,
                       result=$2::jsonb
                   WHERE id=$3""",
                json.dumps(prior),
                json.dumps({"skipped_no_op": True, "reason": noop_reason}),
                action_id,
            )
        await _notify_resolution(action, "noop", error=noop_reason)
        return

    log.info("Executing action %s: %s/%s(%s)", action_id, client_tag, mcp_tool, tool_args)

    try:
        if client_tag == "meta":
            result = await meta_call(mcp_tool, tool_args)
        elif client_tag in ("amazon", "map"):  # "map" tag retained for old rows
            # Native LWA path. Caller's args dict must include `slug`.
            result = await native_amazon_call(mcp_tool, tool_args)
        else:
            raise RuntimeError(f"unknown mcp client_tag {client_tag!r} for action_kind {kind!r}")
    except Exception as e:
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE ads_agent.agent_actions
                   SET status='failed',
                       result=$1::jsonb,
                       prior_state=$2::jsonb,
                       executed_at=NOW()
                   WHERE id=$3""",
                json.dumps({"error": str(e)[:1000]}),
                json.dumps(prior),
                action_id,
            )
        await _notify_resolution(action, "failed", error=str(e))
        return

    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE ads_agent.agent_actions
               SET status='executed',
                   result=$1::jsonb,
                   prior_state=$2::jsonb,
                   executed_at=NOW()
               WHERE id=$3""",
            json.dumps(result if isinstance(result, dict) else {"raw": str(result)}),
            json.dumps(prior),
            action_id,
        )
    await _notify_resolution(action, "executed", result=result)


async def _notify_resolution(action: dict, outcome: str, *,
                             result: Any = None, error: str = "") -> None:
    chat_id = action.get("telegram_chat_id")
    if not chat_id:
        return
    name = action.get("target_object_name") or action["target_object_id"]
    if outcome == "executed":
        kind = action.get("action_kind", "")
        platform = "Amazon" if kind.startswith("amazon_") else "Meta"
        msg = (
            f"✅ *Action #{action['id']} executed* — `{name}`\n"
            f"_{platform} responded OK. Prior state snapshotted; `/rollback {action['id']}` "
            f"will revert if needed within 72h._"
        )
    elif outcome == "noop":
        msg = (
            f"ℹ️ *Action #{action['id']} skipped (no-op)* — `{name}`\n"
            f"Reason: {error}\n"
            f"_No API call was made. Row closed as executed with skip flag._"
        )
    else:
        msg = (
            f"⚠️ *Action #{action['id']} failed* — `{name}`\n"
            f"Error: `{error[:200]}`\n"
            f"_Row marked failed. No Meta change applied. Check logs or re-propose._"
        )
    await post_text(chat_id, msg)


async def run_once(pool: asyncpg.Pool, max_batch: int = 10) -> int:
    """Claim up to max_batch approved actions atomically and run each.
    Returns the number of actions processed (includes failures).
    """
    # Atomic claim — status='approved' → 'executing' in a single UPDATE.
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """UPDATE ads_agent.agent_actions
               SET status='executing'
               WHERE id IN (
                   SELECT id FROM ads_agent.agent_actions
                   WHERE status='approved'
                   ORDER BY approved_at ASC
                   LIMIT $1
                   FOR UPDATE SKIP LOCKED
               )
               RETURNING *""",
            max_batch,
        )

    if not rows:
        return 0

    log.info("Claimed %d approved actions to execute", len(rows))
    for r in rows:
        action = dict(r)
        # asyncpg returns jsonb as dict already; ensure params is dict
        for col in ("params", "evidence", "expected_impact", "prior_state", "result"):
            v = action.get(col)
            if isinstance(v, str):
                try:
                    action[col] = json.loads(v)
                except Exception:
                    pass
        try:
            await _execute_one(pool, action)
        except Exception:
            log.exception("unhandled error executing action %s", action["id"])
    return len(rows)


async def expire_old_pending(pool: asyncpg.Pool) -> int:
    """Move past-expiry proposals from pending_approval → expired."""
    async with pool.acquire() as conn:
        result = await conn.execute(
            """UPDATE ads_agent.agent_actions
               SET status='expired'
               WHERE status='pending_approval'
                 AND expires_at < NOW()"""
        )
    # asyncpg returns "UPDATE N"
    try:
        return int(result.split()[-1])
    except Exception:
        return 0
