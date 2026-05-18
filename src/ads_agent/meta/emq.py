"""Event Match Quality (EMQ) check — Meta's data-quality score for the
Purchase event (M04 in the audit checklist).

The Graph API surfaces EMQ via the dataset_event_match_quality_metrics
edge on a pixel/dataset, but it requires `ads_management` + Business
asset assignment that we don't currently hold. So today this module
returns "not_measured" and recommends the operator pull the score from
Events Manager → Data sources → Pixel → Diagnostics.

When/if we get the right scope on META_ACCESS_TOKEN, swap _NOT_MEASURED
out for an actual API call. Decomposer + analyst already factor M04 in
through the checklist; this just centralises the future API path.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

from ads_agent.config import settings

log = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.facebook.com/v21.0"


@dataclass(frozen=True)
class EmqReading:
    pixel_id: str | None
    score: float | None        # 0..10, None if not measured
    measured: bool             # False → operator must check Events Manager
    detail: str                # human-readable note
    grade: str                 # excellent/good/fair/poor/unknown


def _grade(score: float) -> str:
    if score >= 8.0: return "excellent"
    if score >= 6.0: return "good"
    if score >= 4.0: return "fair"
    return "poor"


async def fetch_emq(pixel_id: str | None) -> EmqReading:
    """Try to fetch EMQ for a pixel; fall back to 'not measured' guidance."""
    if not pixel_id:
        return EmqReading(
            pixel_id=None, score=None, measured=False,
            grade="unknown",
            detail=(
                "No pixel_id available. Cannot pull EMQ programmatically. "
                "Ask operator to check Events Manager → Diagnostics → "
                "Event Match Quality for the Purchase event."
            ),
        )
    token = settings().meta_access_token
    if not token:
        return EmqReading(
            pixel_id=pixel_id, score=None, measured=False, grade="unknown",
            detail="META_ACCESS_TOKEN unset; cannot query EMQ.",
        )
    # Endpoint is plan-gated. Wrap the whole thing so a 403 / 400 doesn't
    # break the audit; we just degrade gracefully.
    try:
        url = f"{GRAPH_BASE}/{pixel_id}"
        params = {
            "fields": "stats_history{event_name,event_match_quality_score}",
            "access_token": token,
        }
        async with httpx.AsyncClient(timeout=15.0) as cli:
            r = await cli.get(url, params=params)
        if r.status_code != 200:
            return EmqReading(
                pixel_id=pixel_id, score=None, measured=False, grade="unknown",
                detail=(
                    f"EMQ endpoint returned HTTP {r.status_code}. Likely "
                    "missing scope (`ads_management` + dataset asset). "
                    "Check Events Manager UI for now."
                ),
            )
        body = r.json()
        # Walk the stats_history for an `event_name == 'Purchase'` row
        rows = (body.get("stats_history") or {}).get("data", [])
        score: float | None = None
        for row in rows:
            if (row.get("event_name") or "").lower() == "purchase":
                s = row.get("event_match_quality_score")
                if s is not None:
                    score = float(s)
                    break
        if score is None:
            return EmqReading(
                pixel_id=pixel_id, score=None, measured=False, grade="unknown",
                detail="Endpoint reachable but no Purchase EMQ row returned.",
            )
        return EmqReading(
            pixel_id=pixel_id, score=score, measured=True,
            grade=_grade(score),
            detail=f"EMQ for Purchase = {score:.1f} ({_grade(score)})",
        )
    except Exception as e:  # noqa: BLE001
        log.warning("EMQ fetch failed for %s: %s", pixel_id, e)
        return EmqReading(
            pixel_id=pixel_id, score=None, measured=False, grade="unknown",
            detail=f"EMQ fetch error: {type(e).__name__}",
        )
