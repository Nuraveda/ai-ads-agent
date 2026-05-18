"""Platform-agnostic approve/reject resolver for action proposals.

Both Telegram callbacks and Discord interactions land here. Caller passes
the action_id, the verb, and the approver's identity (platform-prefixed
user id + display name). Resolver does:

  1. Atomic UPDATE guarded by `WHERE status='pending_approval'`
     (first-click-wins; safe across two simultaneous Approve clicks even
     across two platforms during the dual-post cutover).
  2. Returns ResolutionOutcome describing what happened so the caller can
     produce the right inline reply / toast.
  3. Calls back to platform-specific message editors so both the
     Telegram message AND the Discord message get their buttons stripped
     after a single click on either side.

This is the only place that writes the final approved/rejected status
to ads_agent.agent_actions. Telegram + Discord both call into it.
"""
from __future__ import annotations

import datetime as _dt
import logging
from dataclasses import dataclass
from typing import Optional

import asyncpg

log = logging.getLogger(__name__)


@dataclass
class ResolutionOutcome:
    success: bool                 # action transitioned this call
    verdict: str                  # "approved" / "rejected" / "expired" / "already_resolved" / "not_found"
    toast: str                    # short text for the click-platform's toast/ack
    verdict_line: str | None      # markdown summary line for the message edit
    row: dict | None              # full DB row (or None if not found)


async def resolve_action(
    pool: asyncpg.Pool,
    *,
    action_id: int,
    verb: str,
    approver_id: str,             # "tg:12345" or "discord:987654321"
    approver_name: str,           # display name for the resolution line
) -> ResolutionOutcome:
    """Atomically resolve a pending_approval action."""
    if verb not in ("approve", "reject"):
        return ResolutionOutcome(False, "bad_verb", "Unknown verb.", None, None)

    now = _dt.datetime.utcnow()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM ads_agent.agent_actions WHERE id=$1",
            action_id,
        )
        if not row:
            return ResolutionOutcome(False, "not_found", "Action not found.", None, None)

        # Expiry → atomic mark expired (only if still pending)
        if row["expires_at"] and row["expires_at"].replace(tzinfo=None) < now:
            await conn.execute(
                """UPDATE ads_agent.agent_actions
                   SET status='expired'
                   WHERE id=$1 AND status='pending_approval'""",
                action_id,
            )
            return ResolutionOutcome(
                False, "expired",
                "Proposal expired (72h). Re-plan to retry.",
                None, dict(row),
            )

        if verb == "approve":
            result = await conn.execute(
                """UPDATE ads_agent.agent_actions
                   SET status='approved',
                       approved_by_text=$1, approved_at=NOW()
                   WHERE id=$2 AND status='pending_approval'""",
                approver_id, action_id,
            )
            verdict_line = (
                f"✅ **Approved by `{approver_name}`** at "
                f"{now.strftime('%Y-%m-%d %H:%M UTC')}. "
                f"Executor will run within 5 minutes."
            )
            toast = "Approved — executor will run shortly."
            verdict = "approved"
        else:  # reject
            result = await conn.execute(
                """UPDATE ads_agent.agent_actions
                   SET status='rejected',
                       rejected_by_text=$1, rejected_at=NOW()
                   WHERE id=$2 AND status='pending_approval'""",
                approver_id, action_id,
            )
            verdict_line = (
                f"❌ **Rejected by `{approver_name}`**. "
                f"No change will be applied."
            )
            toast = "Rejected."
            verdict = "rejected"

        try:
            rows_affected = int(result.split()[-1])
        except (ValueError, IndexError):
            rows_affected = 0

        if rows_affected == 0:
            # Re-read to know current status
            fresh = await conn.fetchrow(
                "SELECT status FROM ads_agent.agent_actions WHERE id=$1",
                action_id,
            )
            status_now = fresh["status"] if fresh else "unknown"
            return ResolutionOutcome(
                False, "already_resolved",
                f"Already {status_now}. No change.",
                None, dict(row),
            )

        return ResolutionOutcome(
            True, verdict, toast, verdict_line, dict(row),
        )


async def edit_resolution_on_all_platforms(
    *, row: dict, original_text: str, verdict_line: str,
) -> None:
    """After a successful resolve, strip buttons + append verdict on every
    platform the proposal was posted to. Failures are logged but never
    raised — DB is authoritative.
    """
    new_text = (original_text or "").rstrip() + "\n\n" + verdict_line

    # Telegram side
    if row.get("telegram_chat_id") and row.get("telegram_message_id"):
        try:
            from ads_agent.actions.notifier import edit_message_remove_buttons
            await edit_message_remove_buttons(
                chat_id=row["telegram_chat_id"],
                message_id=row["telegram_message_id"],
                new_text=new_text,
            )
        except Exception:
            log.exception("Telegram edit failed for action %s", row.get("id"))

    # Discord side
    if row.get("discord_channel_id") and row.get("discord_message_id"):
        try:
            from ads_agent.actions.discord_notifier import edit_resolution
            await edit_resolution(
                channel_id=int(row["discord_channel_id"]),
                message_id=int(row["discord_message_id"]),
                new_text=new_text,
            )
        except Exception:
            log.exception("Discord edit failed for action %s", row.get("id"))
