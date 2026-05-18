"""Run the per-brand Meta audit prompt (playbook Section X → meta_audit)
against a decomposed MetaAccountHierarchy.

Output contract: returns {diagnosis, actions[]} where every action is one
of the four verbs SCALE / REFRESH / PAUSE / WATCH at the correct entity
level (ad / adset / campaign), with rationale and expected_impact tied
to numbers from the decomposition.

Falls back to a minimal hardcoded prompt if the brand's playbook doesn't
carry a meta_audit brief yet — so a fresh install still produces
something useful, just less brand-tuned.
"""
from __future__ import annotations

import json
import logging
import re

from ads_agent.agent.analysis.meta_decomposer import MetaAccountHierarchy
from ads_agent.agent.llm import complete
from ads_agent.playbook import load_ref, node_brief

log = logging.getLogger(__name__)


_FALLBACK_METHODOLOGY = """\
You are a world-class D2C Meta Ads operator. Audit this account at
campaign / ad-set / ad level. Output actions only: SCALE, REFRESH,
PAUSE, or WATCH. Never say "monitor closely". Four strict verbs.

Pause rule: spend ≥ 3 × target_cpa AND ROAS < breakeven_roas.
Fatigue rule: frequency > 2.5 OR 7d-CTR drop > 30% → REFRESH, not PAUSE.
ASC+ campaigns: judge campaign-level only, no ad drill.
One-ad-carried campaigns: isolate the winner, pause the rest.

Defaults if brand config missing: target_roas=3.0, breakeven_roas=2.0,
assume AOV=₹1000 → target_cpa=₹333 → spend_enough=₹1000.

Output this exact structure:

## PRE-FLIGHT
One paragraph — attribution window, date range, pixel hygiene, ASC+ split.

## ACCOUNT SUMMARY
<one sentence verdict>

## CAMPAIGN TABLE
Markdown table: campaign | type | spend | rev | ROAS | purch | CTR | verdict | reason

## ADS TO PAUSE TODAY
## ADS TO REFRESH
## ADS TO SCALE
## CAMPAIGNS TO PAUSE

## 7-DAY OPERATING PLAN
- pause today (ids)
- refresh this week (concepts)
- scale (ids + ramp mode)
- creative briefs (3-5)

Then the ACTIONS block (one per rule-qualified recommendation):

### {N}. {verb} · {target_label}
- target_level: ad | adset | campaign
- target_id:   <meta id>
- rationale:   <why, with specific numbers>
- expected_impact: <e.g. "save ~₹480/week" or "scale ~2× over 7d">
- safety_check: <what could go wrong, or "none">
"""


_HEALTH_RE = re.compile(
    r"##\s*HEALTH\s*SCORE\s*\n\s*Total[:\s]+(\d{1,3})/100[^\n]*\n"
    r"\s*Pixel[/CAPI:\s]+(\d{1,3})/100[^\n]*\n"
    r"\s*Creative[:\s]+(\d{1,3})/100[^\n]*\n"
    r"\s*Structure[:\s]+(\d{1,3})/100[^\n]*\n"
    r"\s*Audience[:\s]+(\d{1,3})/100",
    re.IGNORECASE,
)


def _parse_health(text: str) -> dict:
    m = _HEALTH_RE.search(text)
    if not m:
        return {}
    total, pixel, creative, structure, audience = (int(g) for g in m.groups())
    grade = (
        "A" if total >= 90 else
        "B" if total >= 80 else
        "C" if total >= 70 else
        "D" if total >= 60 else
        "F"
    )
    return {
        "total": total, "grade": grade,
        "pixel_capi": pixel, "creative": creative,
        "structure": structure, "audience": audience,
    }


# Phase B: halo-citation verifier ------------------------------------------
# Any "X.Yx" or "X.YY" digits cited in an action's rationale near the word
# "halo" must trace back to a real number in the supplied data; otherwise
# the action is downgraded to severity=low (drops it out of Quick Wins) and
# tagged [HALO_UNVERIFIED]. The LLM has been hallucinating campaign-level
# halo digits even with per-ad halo stamps; this is a hard-fence check.

_HALO_NEAR_RE = re.compile(
    r"(?:halo[a-z\s]{0,30}?|halo[^\n]{0,30}?)(\d+\.\d{1,2})\s*[x×]?",
    re.IGNORECASE,
)


def _collect_valid_halo_numbers(hierarchy_data: dict) -> set[float]:
    """Build the set of halo numbers a rationale is allowed to cite.

    Includes:
      - account-level halo_roas
      - per_asin halo_roas
      - per-campaign amazon_halo_blended
    Plus their integer floors (the LLM sometimes rounds down).
    """
    valid: set[float] = set()
    halo = (hierarchy_data or {}).get("amazon_halo") or {}
    if not halo:
        return valid
    if (v := halo.get("halo_roas")) is not None:
        valid.add(round(float(v), 2))
    for row in halo.get("per_asin", []) or []:
        if (v := row.get("halo_roas")) is not None:
            valid.add(round(float(v), 2))
    for c in (hierarchy_data or {}).get("campaigns", []) or []:
        if c.get("amazon_halo_blended"):
            valid.add(round(float(c["amazon_halo_blended"]), 2))
    # Allow off-by-1 rounding ±0.05
    expanded: set[float] = set()
    for v in valid:
        expanded.add(v)
        expanded.add(round(v + 0.01, 2))
        expanded.add(round(v - 0.01, 2))
    return expanded


def _verify_halo_citations(
    actions: list[dict], hierarchy_data: dict,
) -> int:
    """Walk parsed actions, scan their rationales for halo citations,
    flag and downgrade any that don't match supplied numbers. Returns
    the count of actions that were downgraded.
    """
    valid = _collect_valid_halo_numbers(hierarchy_data)
    downgraded = 0
    for a in actions:
        rationale = a.get("rationale") or ""
        if "halo" not in rationale.lower():
            continue
        cited = [round(float(m), 2) for m in _HALO_NEAR_RE.findall(rationale)]
        if not cited:
            continue
        if not valid:
            # No halo data supplied; any halo citation is automatically wrong
            bad = cited
        else:
            tolerance = 0.10  # accept within 0.1 of any supplied value
            bad = [
                v for v in cited
                if not any(abs(v - good) <= tolerance for good in valid)
            ]
        if bad:
            a["rationale"] = (
                rationale
                + f"\n[HALO_UNVERIFIED: cited {bad}; supplied set {sorted(valid)}]"
            )
            # Downgrade severity so it falls out of Quick Wins (which keys
            # off severity ∈ {critical, high}).
            sev = a.get("severity", "medium")
            if sev in ("critical", "high"):
                a["_original_severity"] = sev
                a["severity"] = "low"
                downgraded += 1
    return downgraded


def _parse_report(text: str, hierarchy_data: dict | None = None) -> dict:
    """Split the markdown report into (narrative_body, parsed actions list).

    `hierarchy_data` is the JSON payload sent to the LLM — used by
    Phase-B halo-citation verification. Pass None to skip verification
    (e.g. in unit tests with synthetic markdown).
    """
    # Actions block begins at first `### N. verb · label`
    actions_start = re.search(r"(?m)^###\s*\d+\.", text)
    narrative = text[: actions_start.start()].strip() if actions_start else text.strip()
    health = _parse_health(text)

    actions: list[dict] = []
    for block in re.finditer(
        r"###\s*\d+\.\s*(\S+)\s*·\s*([^\n]+)\n(.*?)(?=\n###\s*\d+\.|\Z)",
        text, re.DOTALL,
    ):
        verb = block.group(1).strip().upper()
        target_label = block.group(2).strip()
        body = block.group(3)

        def _field(name: str) -> str:
            m = re.search(rf"-\s*{re.escape(name)}\s*:\s*(.*?)(?=\n\s*-\s|\Z)",
                          body, re.DOTALL | re.IGNORECASE)
            return m.group(1).strip() if m else ""

        actions.append({
            "action_kind":     verb,
            "target_label":    target_label,
            "target_level":    _field("target_level"),
            "target_id":       _field("target_id"),
            "check_id":        _field("check_id") or "",          # NEW: stable M-id
            "severity":        (_field("severity") or "medium").lower(),
            "effort":          (_field("effort") or "medium").lower(),
            "rationale":       _field("rationale"),
            "expected_impact": _field("expected_impact"),
            "safety_check":    _field("safety_check"),
        })
    # Phase B: verify halo citations against supplied data, downgrade
    # actions that fabricated digits. Done BEFORE sorting so the
    # severity downgrade is reflected in the final order + Quick Wins.
    halo_downgrades = 0
    if hierarchy_data is not None:
        halo_downgrades = _verify_halo_citations(actions, hierarchy_data)

    # Sort actions: severity DESC (critical → low), then effort ASC
    # (low → high). Stable so the LLM's relative ranking within a tier
    # is preserved.
    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    eff_order = {"low": 0, "medium": 1, "high": 2}
    actions.sort(key=lambda a: (
        sev_order.get(a.get("severity", "medium"), 2),
        eff_order.get(a.get("effort", "medium"), 1),
    ))
    quick_wins = [
        a for a in actions
        if a.get("severity") in ("critical", "high")
        and a.get("effort") == "low"
    ]
    return {
        "diagnosis": narrative,
        "actions": actions,
        "quick_wins": quick_wins,
        "health": health,
        "halo_downgrades": halo_downgrades,
    }


async def audit_meta_account(
    hierarchy: MetaAccountHierarchy,
    *,
    brand: str = "default",
    model_tier: str = "smart",
) -> dict:
    """Run the brand-tuned prompt against the decomposed account.

    Returns {diagnosis, actions} with diagnosis = the full markdown report
    (minus the parsed actions block) so the Telegram/Discord layer can
    just print it, while `actions` stays available for downstream write
    flows.
    """
    methodology = node_brief("meta_audit", brand)
    if not methodology:
        log.warning("no meta_audit brief for brand %r — using fallback", brand)
        methodology = _FALLBACK_METHODOLOGY

    # Inject the canonical 30-check checklist + 2025 platform-change context
    # as ground-truth references the analyst MUST cite by check-ID.
    checklist = load_ref("meta-audit-checklist")
    changes   = load_ref("2025-platform-changes")
    refs_block = ""
    if checklist:
        refs_block += (
            "\n\n# REFERENCE — meta audit checklist (cite check-IDs)\n"
            "Every action you propose MUST cite at least one check-ID from "
            "this checklist (e.g. M01, M15, M24). The check-ID goes on the "
            "`- check_id:` line of each action block.\n\n"
            + checklist
        )
    if changes:
        refs_block += (
            "\n\n# REFERENCE — 2025 platform changes (cite when relevant)\n"
            "When a finding intersects a 2025 platform change (Andromeda, "
            "iOS 14.5 dedup, link-click redefinition, OCAPI EOL, AEM v2, "
            "Threads), cite the change in the rationale BEFORE recommending "
            "structural fixes — many CTR/ROAS drops are metric redefinitions, "
            "not real performance regressions.\n\n"
            + changes
        )

    payload = hierarchy.to_dict()
    # Trim creative thumbnail URLs (they can be huge) — not needed for the
    # analyst, only the downstream display layer
    for c in payload.get("campaigns", []):
        for s in c.get("ad_sets", []):
            for a in s.get("ads", []):
                a.pop("creative_thumbnail", None)

    data_block = json.dumps(payload, indent=2, default=str)
    prompt = methodology + "\n\nThe account to analyse (JSON):\n" + data_block

    raw = await complete(
        prompt,
        tier=model_tier,
        max_tokens=16000,
        system=(
            "You are a senior D2C Meta Ads operator. Output ONLY the markdown "
            "report as specified — no preamble, no JSON fences. Every "
            "number you cite must trace to the supplied JSON. If the "
            "pre-flight Pixel hygiene is broken (pixel_hygiene_ok=false), "
            "STOP after the PRE-FLIGHT section and produce no further "
            "recommendations."
        ),
    )
    return _parse_report(raw, hierarchy_data=payload)
