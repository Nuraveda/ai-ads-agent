"""Generic placeholder planner rules — deliberately NOT calibrated.

This module is the public-repo fallback when the private playbook package
`ai_marketing_stack_ads_playbook` is not installed. A public clone of the engine
can run end-to-end with these stubs, but the thresholds are set far outside
the range of anything you'd want to trade on — propose-threshold of 3.0×
ROAS and scale-floor of 5.0× are deliberately loose so the engine proposes
rarely and only on obvious cases, without giving away the real calibration.

The tuned values live in the private package. See:
  https://github.com/glitch-exec-labs/ai_marketing_stack-ads-agent-private

Contract (same as the private module):
  - RULES: tuple[Callable[[dict], ActionProposal | None], ...]
  - DEDUP_HOURS: int
"""
from __future__ import annotations

from typing import Callable

from ads_agent.actions.models import ActionProposal

# Generic, obviously-not-tuned placeholders.
PAUSE_SPEND_MIN      = 10_000
PAUSE_ROAS_CEIL      = 0.5
NOSIGNAL_SPEND_MIN   = 10_000
NOSIGNAL_CLICKS_MIN  = 1_000
SCALE_SPEND_MIN      = 50_000
SCALE_ROAS_FLOOR     = 5.0
SCALE_MULTIPLIER     = 1.2

DEDUP_HOURS          = 72


def _rule_pause(r: dict) -> ActionProposal | None:
    """Generic demo pause rule — intentionally conservative."""
    if r["effective_status"] != "ACTIVE":
        return None
    spend = r["spend_14d"]
    roas = r["roas"]
    clicks = r["clicks_14d"]
    losing = spend > PAUSE_SPEND_MIN and roas is not None and roas < PAUSE_ROAS_CEIL
    nosignal = (
        spend > NOSIGNAL_SPEND_MIN
        and clicks > NOSIGNAL_CLICKS_MIN
        and roas is None
    )
    if not (losing or nosignal):
        return None
    rationale = (
        "[demo stub] Adset matched generic pause criteria. Install the "
        "ai_marketing_stack_ads_playbook package for calibrated rules."
    )
    return ActionProposal(
        store_slug=r.get("_store_slug", "unknown"),
        action_kind="pause_adset",
        target_object_id=r["adset_id"],
        target_object_name=(r["adset_name"] or r["adset_id"])[:60],
        rationale=rationale,
        params={},
        evidence={"spend_14d": spend, "roas_14d": roas, "clicks_14d": clicks},
        expected_impact={"note": "demo stub — no impact projection"},
    )


def _rule_scale(r: dict) -> ActionProposal | None:
    """Generic demo scale rule — intentionally conservative."""
    if r["effective_status"] != "ACTIVE":
        return None
    spend, roas, db = r["spend_14d"], r["roas"], r["daily_budget"]
    if (
        spend < SCALE_SPEND_MIN
        or not roas
        or roas < SCALE_ROAS_FLOOR
        or db == 0
    ):
        return None
    new_budget = int(db * SCALE_MULTIPLIER)
    return ActionProposal(
        store_slug=r.get("_store_slug", "unknown"),
        action_kind="update_adset_budget",
        target_object_id=r["adset_id"],
        target_object_name=(r["adset_name"] or r["adset_id"])[:60],
        rationale=(
            "[demo stub] Adset matched generic scale criteria. Install the "
            "ai_marketing_stack_ads_playbook package for calibrated rules."
        ),
        params={"new_daily_budget": new_budget, "old_daily_budget": db},
        evidence={"spend_14d": spend, "roas_14d": roas},
        expected_impact={"note": "demo stub — no impact projection"},
    )


RULES: tuple[Callable[[dict], ActionProposal | None], ...] = (
    _rule_pause,
    _rule_scale,
)
