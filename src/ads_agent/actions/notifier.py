"""Telegram notifier for the action approval loop.

One entry point: `post_proposal(...)` inserts a row into ads_agent.agent_actions
and posts it to the configured Telegram group with Approve/Reject inline buttons.
The message_id + chat_id are persisted so the button handler can find the row.

Button handler lives in src/ads_agent/telegram/callbacks.py.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Optional

import asyncpg
import httpx

from ads_agent.actions.guardrails import (
    GuardrailViolation,
    assert_pause_applicable,
    fetch_meta_ad_status,
    fetch_meta_adset_status,
    fetch_meta_campaign_status,
)
from ads_agent.actions.models import ActionProposal, format_telegram_message

log = logging.getLogger(__name__)


# Action-kind → (platform, status_fetcher) for pre-write guardrails.
# Only listed if the action is destructive (pause/cut). Budget changes and
# keyword adds don't need a pause-applicability check.
_PAUSE_STATUS_FETCHERS = {
    "pause_adset":     ("meta",   fetch_meta_adset_status),
    "pause_ad":        ("meta",   fetch_meta_ad_status),
    "amazon_pause_ad": ("amazon", None),   # Amazon path has its own state lookup path; enabled in executor
    # "resume_adset" intentionally NOT guarded — resume IS applicable on a paused target
    # "update_adset_budget" guarded separately via assert_budget_change_applicable
}

TELEGRAM_API = "https://api.telegram.org"


def _bot_token() -> str:
    tok = os.environ.get("TELEGRAM_BOT_TOKEN_ADS", "").strip()
    if not tok:
        raise RuntimeError("TELEGRAM_BOT_TOKEN_ADS not set")
    return tok


def _inline_keyboard(action_id: int) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "✅ Approve",   "callback_data": f"act:approve:{action_id}"},
            {"text": "❌ Reject",    "callback_data": f"act:reject:{action_id}"},
        ]]
    }


class TelegramNotifyError(RuntimeError):
    """Raised when an action row was inserted but Telegram delivery failed.

    The row is marked `notify_failed` so the planner's dedup window does
    not suppress future reproposals for the same target (issue #6).
    """


async def post_proposal(
    pool: asyncpg.Pool,
    prop: ActionProposal,
    chat_id: int | None = None,    # back-compat — Telegram-only callers
    *,
    target=None,                   # ProposalTarget (preferred) — Discord+TG dispatch
) -> int:
    """Insert action row, post to configured platforms, return action_id.

    Cutover (2026-04-25): now supports Discord in addition to Telegram via
    the `target: ProposalTarget` kwarg (see actions.approval_targets). When
    both telegram_chat_id and discord_channel_id are set, the row is dual-
    posted; either platform's Approve/Reject click resolves the row and
    the other platform's message gets its buttons stripped on edit.

    Back-compat: legacy callers passing `chat_id: int` keep getting
    Telegram-only behaviour with no Discord post. New code should pass
    `target=` instead.

    Issue #6 invariant preserved: if EVERY configured platform's send
    fails, mark the row notify_failed and raise TelegramNotifyError. If
    at least one platform delivered, the row is live and the action_id
    is returned. (TelegramNotifyError name kept for API stability;
    semantically it's now "notify failed on every platform".)

    Pre-write guardrail still applies: refuses no-op pauses before any
    DB or network work via assert_pause_applicable().
    """
    # Resolve target — accept either legacy chat_id int or new target obj
    from ads_agent.actions.approval_targets import ProposalTarget
    if target is None:
        if chat_id is None:
            raise ValueError("post_proposal needs either chat_id= or target=")
        target = ProposalTarget(telegram_chat_id=chat_id, discord_channel_id=None)
    if not target.has_any:
        raise ValueError("post_proposal: target has no platform configured")
    # 0) Guardrail: reject no-op pauses before any DB/Telegram work.
    fetcher_entry = _PAUSE_STATUS_FETCHERS.get(prop.action_kind)
    if fetcher_entry and fetcher_entry[1] is not None:
        platform, fetcher = fetcher_entry
        try:
            await assert_pause_applicable(
                platform, target_id=prop.target_object_id,
                fetch_effective_status=fetcher,
                target_name=prop.target_object_name,
            )
        except GuardrailViolation as e:
            # Re-raise so the planner can log + skip. Intentionally NOT wrapped
            # in TelegramNotifyError so planner's retry-on-notify-failure path
            # doesn't retry these — they're structural rejections, not transient.
            log.info("guardrail rejected proposal for %s: %s",
                     prop.target_object_id, e)
            raise

    # 1) Insert (status defaults to pending_approval). Both platform's
    # channel ids are nullable; we'll set the message ids after each post.
    async with pool.acquire() as conn:
        action_id = await conn.fetchval(
            """INSERT INTO ads_agent.agent_actions (
                   store_slug, action_kind, target_object_id, target_object_name,
                   params, rationale, evidence, expected_impact,
                   telegram_chat_id, discord_channel_id, approval_platform
               ) VALUES ($1,$2,$3,$4,$5::jsonb,$6,$7::jsonb,$8::jsonb,$9,$10,$11)
               RETURNING id""",
            prop.store_slug, prop.action_kind, prop.target_object_id,
            prop.target_object_name, json.dumps(prop.params),
            prop.rationale, json.dumps(prop.evidence), json.dumps(prop.expected_impact),
            target.telegram_chat_id,
            target.discord_channel_id,
            "dual" if target.is_dual else (
                "discord" if target.discord_channel_id else "telegram"
            ),
        )

    text = format_telegram_message(
        action_id=action_id,
        store_slug=prop.store_slug,
        action_kind=prop.action_kind,
        target_name=prop.target_object_name,
        rationale=prop.rationale,
        params=prop.params,
        evidence=prop.evidence,
        expected_impact=prop.expected_impact,
    )

    # 2) Try each configured platform. Track per-platform success and
    # update the row with whichever message_ids we got.
    tg_msg_id: int | None = None
    tg_err: str = ""
    dc_msg_id: int | None = None
    dc_err: str = ""

    if target.telegram_chat_id is not None:
        try:
            async with httpx.AsyncClient(timeout=15.0) as c:
                resp = await c.post(
                    f"{TELEGRAM_API}/bot{_bot_token()}/sendMessage",
                    json={
                        "chat_id": target.telegram_chat_id,
                        "text": text,
                        "parse_mode": "Markdown",
                        "disable_web_page_preview": True,
                        "reply_markup": _inline_keyboard(action_id),
                    },
                )
            body = resp.json()
            if body.get("ok"):
                tg_msg_id = body["result"]["message_id"]
            else:
                tg_err = body.get("description") or "unknown TG error"
        except Exception as e:
            tg_err = f"transport: {e!r}"

    if target.discord_channel_id is not None:
        try:
            from ads_agent.actions.discord_notifier import (
                DiscordNotifyError, post_proposal_to_discord,
            )
            dc_msg_id = await post_proposal_to_discord(
                target.discord_channel_id, action_id, text,
            )
        except Exception as e:
            dc_err = f"{type(e).__name__}: {e!r}"

    # 3) Persist whatever message ids we got
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE ads_agent.agent_actions
               SET telegram_message_id=$1, discord_message_id=$2
               WHERE id=$3""",
            tg_msg_id, dc_msg_id, action_id,
        )

    # 4) Did anything land? If every configured platform failed, mark
    # notify_failed so the planner reproposes next tick (issue #6).
    landed = []
    if target.telegram_chat_id is not None and tg_msg_id is not None:
        landed.append("telegram")
    if target.discord_channel_id is not None and dc_msg_id is not None:
        landed.append("discord")

    if not landed:
        reason = f"telegram={tg_err or 'n/a'} | discord={dc_err or 'n/a'}"
        log.error(
            "ALL platforms failed for action %s: %s — marking notify_failed",
            action_id, reason,
        )
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE ads_agent.agent_actions
                   SET status='notify_failed',
                       result=$1::jsonb
                   WHERE id=$2 AND status='pending_approval'""",
                json.dumps({"notify_error": reason[:500]}),
                action_id,
            )
        raise TelegramNotifyError(
            f"Notify failed on every platform for action {action_id}: {reason}"
        )

    if (target.telegram_chat_id and not tg_msg_id) or (target.discord_channel_id and not dc_msg_id):
        # Partial — at least one platform delivered, but log the loser.
        log.warning(
            "action %s posted on %s but failed on others: tg=%s dc=%s",
            action_id, ",".join(landed), tg_err or "ok", dc_err or "ok",
        )

    log.info(
        "Posted action %s on %s (tg_msg=%s, dc_msg=%s)",
        action_id, ",".join(landed), tg_msg_id, dc_msg_id,
    )
    return action_id


async def post_text(chat_id: int, text: str, parse_mode: str = "Markdown") -> Optional[int]:
    """Free-form outbound message — used for action-executed confirmations,
    daily digests, alerts. No admin gating; the bot can speak whenever.
    """
    async with httpx.AsyncClient(timeout=15.0) as c:
        resp = await c.post(
            f"{TELEGRAM_API}/bot{_bot_token()}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": text[:4000],
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            },
        )
    body = resp.json()
    if body.get("ok"):
        return body["result"]["message_id"]
    log.warning("post_text failed: %s", body)
    return None


async def edit_message_remove_buttons(
    chat_id: int, message_id: int, new_text: str
) -> None:
    """After Approve/Reject click, edit the original prompt to show resolution."""
    async with httpx.AsyncClient(timeout=15.0) as c:
        await c.post(
            f"{TELEGRAM_API}/bot{_bot_token()}/editMessageText",
            json={
                "chat_id": chat_id,
                "message_id": message_id,
                "text": new_text[:4000],
                "parse_mode": "Markdown",
                "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": []},
            },
        )
