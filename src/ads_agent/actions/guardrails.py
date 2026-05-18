"""Pre-write guardrails for action proposals.

Two classes of mistake the agent (or any ad-hoc operator script) can make
when generating proposals:

1. **Pause-a-dead-target.** Read 14-day insights, see spend, propose
   "pause this", miss the fact that the target was already paused mid-window.
   The pause becomes a no-op, the operator wastes approval time, and the
   audit log fills with meaningless entries.

2. **Raise-budget-on-under-spent-campaign.** Confuse actual daily
   spend with daily budget cap. Propose "raise cap X → Y" when the cap was
   already 10× higher than actual spend — meaning the throttle was never
   the budget, it was bid competitiveness. The "raise budget" action is a
   no-op that ships a wrong narrative to the founder.

Both bit us in the 22-Apr-2026 one-off report. These guardrails are the
defensive fix: EVERY proposal-creating code path (the periodic planner,
the /scan_amazon Telegram trigger, ad-hoc operator scripts, the private
playbook's custom rule closures) goes through these asserts before a row
hits Postgres or Telegram.

Usage:
    from ads_agent.actions.guardrails import (
        assert_pause_applicable,
        assert_budget_change_applicable,
        GuardrailViolation,
    )

    try:
        await assert_pause_applicable("meta", target_id=adset_id)
    except GuardrailViolation as e:
        log.info("skipping proposal: %s", e)
        continue
"""
from __future__ import annotations

import logging
from typing import Any, Literal

log = logging.getLogger(__name__)


class GuardrailViolation(RuntimeError):
    """Proposal was rejected by a pre-write guardrail. Non-fatal — the
    caller should skip this proposal and move on to the next one.

    Caller pattern:
        try:
            await assert_pause_applicable(...)
        except GuardrailViolation as e:
            log.info("dropped: %s", e)
            continue  # to the next candidate in the planner loop
    """


# Every "effectively off" state we consider non-pausable. Meta and Amazon
# use different string taxonomies; this union covers both.
#
# Meta effective_status values:
#   ACTIVE · WITH_ISSUES · PENDING_REVIEW · PENDING_BILLING_INFO
#   PAUSED · ADSET_PAUSED · CAMPAIGN_PAUSED · ARCHIVED · DELETED · DISAPPROVED
# Amazon SP/SB state values (lowercase in the API):
#   enabled · paused · archived
_NOT_PAUSABLE_META = frozenset({
    "PAUSED", "ADSET_PAUSED", "CAMPAIGN_PAUSED",
    "ARCHIVED", "DELETED", "DISAPPROVED",
})
_NOT_PAUSABLE_AMAZON = frozenset({"paused", "archived"})

# Budget-vs-actual threshold: if actual_daily_spend is less than this
# fraction of daily_cap, then the cap isn't the throttle — refuse to
# propose a cap raise. Configurable if needed later.
BUDGET_LEVER_THRESHOLD = 0.70


async def assert_pause_applicable(
    platform: Literal["meta", "amazon"],
    *,
    target_id: str,
    fetch_effective_status: Any,
    target_name: str = "",
) -> None:
    """Raise GuardrailViolation if the pause action would be a no-op.

    `fetch_effective_status` is an async callable that takes a target_id
    and returns the current effective_status string (or None if the
    target was deleted / not found). Passed in so this function doesn't
    depend on any particular MCP client — keeps it testable.

    On match:
      - Meta: raises if effective_status ∈ {PAUSED, ADSET_PAUSED, …}
      - Amazon: raises if state ∈ {paused, archived}
      - Target not found: raises (can't pause what doesn't exist)
    """
    if not target_id:
        raise GuardrailViolation("target_id is empty")

    try:
        status = await fetch_effective_status(target_id)
    except Exception as e:
        # Network or API flake — we can't prove the target is active, so
        # err conservative: reject rather than risk a no-op proposal.
        raise GuardrailViolation(
            f"could not fetch effective_status for {platform} target "
            f"{target_id!r}: {e}"
        ) from e

    if not status:
        raise GuardrailViolation(
            f"{platform} target {target_id!r} "
            f"({target_name or 'unnamed'}) not found — may have been "
            f"deleted. Refusing pause proposal."
        )

    not_pausable = _NOT_PAUSABLE_META if platform == "meta" else _NOT_PAUSABLE_AMAZON
    if status in not_pausable:
        raise GuardrailViolation(
            f"{platform} target {target_id!r} "
            f"({target_name or 'unnamed'}) already in non-active state "
            f"{status!r}. Pause would be a no-op."
        )


async def assert_budget_change_applicable(
    *,
    target_id: str,
    actual_daily_spend: float,
    current_daily_cap: float,
    direction: Literal["raise", "lower"],
    target_name: str = "",
    threshold: float = BUDGET_LEVER_THRESHOLD,
) -> None:
    """Raise GuardrailViolation if a budget change won't move the needle.

    The classic mistake is proposing "raise daily cap X → Y" when the
    campaign is only burning a small fraction of its current cap. In that
    case budget is not the throttle — bids are — and raising the cap is
    a no-op that looks like action to the operator.

    Rules:
      - `direction == "raise"` requires `actual_daily_spend >= threshold *
         current_daily_cap`. Below that, the planner should recommend a
         bid change instead.
      - `direction == "lower"` has no symmetric guardrail — reducing a
         cap that's under-spent is still useful to reclaim headroom.

    `current_daily_cap == 0` means unlimited/no-cap — always allows raise
    (nonsensical but we don't want to block edge cases).
    """
    if not target_id:
        raise GuardrailViolation("target_id is empty")

    if direction != "raise":
        return  # symmetric guardrail intentionally not implemented

    if current_daily_cap <= 0:
        return  # no cap set → cap raise is structurally valid

    utilization = actual_daily_spend / current_daily_cap
    if utilization < threshold:
        raise GuardrailViolation(
            f"target {target_id!r} ({target_name or 'unnamed'}) spends "
            f"only {actual_daily_spend:.2f}/day against a {current_daily_cap:.2f} "
            f"cap ({utilization:.0%} utilization, under the "
            f"{int(threshold*100)}% threshold). Budget is not the throttle; "
            f"bids are. Refusing 'raise cap' proposal — propose a bid change "
            f"instead."
        )


# ---------------------------------------------------------------------------
# Pre-wired status fetchers for convenience. Each one returns the current
# effective_status string for the given platform. Callers can skip these
# and pass their own fetch function if they want custom caching.
# ---------------------------------------------------------------------------

async def fetch_meta_campaign_status(target_id: str) -> str | None:
    """Return effective_status for a Meta campaign via Graph API."""
    import os
    import httpx

    tok = os.environ.get("META_ACCESS_TOKEN", "").strip()
    if not tok:
        raise RuntimeError("META_ACCESS_TOKEN not set")
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.get(
            f"https://graph.facebook.com/v21.0/{target_id}",
            params={"fields": "effective_status", "access_token": tok},
        )
    body = r.json()
    return body.get("effective_status")


async def fetch_meta_adset_status(target_id: str) -> str | None:
    """Return effective_status for a Meta adset via Graph API."""
    import os
    import httpx

    tok = os.environ.get("META_ACCESS_TOKEN", "").strip()
    if not tok:
        raise RuntimeError("META_ACCESS_TOKEN not set")
    async with httpx.AsyncClient(timeout=30.0) as c:
        r = await c.get(
            f"https://graph.facebook.com/v21.0/{target_id}",
            params={"fields": "effective_status", "access_token": tok},
        )
    body = r.json()
    return body.get("effective_status")


async def fetch_meta_ad_status(target_id: str) -> str | None:
    """Return effective_status for a Meta ad via Graph API."""
    # Meta uses the same node shape for ads
    return await fetch_meta_campaign_status(target_id)
