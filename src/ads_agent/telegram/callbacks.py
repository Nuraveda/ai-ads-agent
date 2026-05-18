"""Telegram inline-button callback handler for action approvals.

Callback data format: `act:<verb>:<action_id>`
  verb ∈ {approve, reject}
  action_id: int

Security model (issue #2):
  1. Callback is admin-gated: only user ids in TELEGRAM_ADMIN_IDS can
     approve/reject. Non-admin clicks get a Telegram toast and no state
     change.
  2. State transition is first-click-wins via an atomic UPDATE in the
     shared resolver (`actions.resolver.resolve_action`). Safe across
     two concurrent clicks even when one is from Telegram and the other
     from Discord (during the dual-post cutover, 2026-04-25).

Cutover note: this handler now delegates to the platform-agnostic
resolver so a click here also strips the Discord buttons (and vice
versa from the Discord side). DB stays the single source of truth.
"""
from __future__ import annotations

import logging

import asyncpg
from telegram import Update
from telegram.ext import ContextTypes

from ads_agent.actions.resolver import (
    edit_resolution_on_all_platforms,
    resolve_action,
)
from ads_agent.config import settings
from ads_agent.telegram.auth import is_admin

log = logging.getLogger(__name__)


async def _pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(
        settings().postgres_rw_dsn, min_size=1, max_size=2,
    )


async def action_button_handler(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.data or not q.data.startswith("act:"):
        return

    if not is_admin(update):
        user_id = update.effective_user.id if update.effective_user else "?"
        log.warning("non-admin callback rejected: user=%s data=%s", user_id, q.data)
        await q.answer(
            "Not authorized. Only configured admins can approve or reject actions.",
            show_alert=True,
        )
        return

    parts = q.data.split(":")
    if len(parts) != 3:
        await q.answer("malformed callback", show_alert=False)
        return
    _, verb, action_id_str = parts
    try:
        action_id = int(action_id_str)
    except ValueError:
        await q.answer("bad action id", show_alert=False)
        return

    user = update.effective_user
    approver_id = f"tg:{user.id if user else '?'}"
    approver_name = (user.username or user.full_name or str(user.id)) if user else "?"

    pool = await _pool()
    try:
        outcome = await resolve_action(
            pool,
            action_id=action_id, verb=verb,
            approver_id=approver_id, approver_name=approver_name,
        )
        await q.answer(outcome.toast, show_alert=not outcome.success)
        if outcome.success and outcome.row and outcome.verdict_line:
            original = q.message.text or ""
            await edit_resolution_on_all_platforms(
                row=outcome.row,
                original_text=original,
                verdict_line=outcome.verdict_line,
            )
    finally:
        await pool.close()
