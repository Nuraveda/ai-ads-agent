"""Campaign-analyst prompt: enforces hierarchy-drill discipline + surgical
action taxonomy, given a pre-decomposed CampaignHierarchy.

Design premise: the LLM should NEVER propose "pause this campaign" when
the parent's aggregate ROAS is mediocre but one child holds a large
share of spend at good ROAS. The methodology below is written to force
the model to find the real lever.

Output contract: the analyst MUST return ONE JSON object with two keys:
  - "diagnosis"  : short prose, 4–8 sentences, describing what's actually
                   happening inside the campaign (winners, losers, tail)
  - "actions"    : a list of proposals at the RIGHT entity level
                   (keyword/ad/target/placement/campaign), each with:
                     {
                       "action_kind": "...",     # see taxonomy below
                       "target_level": "keyword|product_ad|product_target|campaign",
                       "target_id":   "...",     # MAP resource id
                       "target_label": "...",    # human-readable
                       "rationale":   "...",     # why, with numbers from the decomp
                       "expected_impact": "...", # e.g. "save ~₹234/week" or "unlock ~₹800/week in un-served demand"
                       "safety_check": "..."     # what could go wrong, or "none"
                     }

Action-kind taxonomy (EXTEND this list if you need a new lever):
  - pause_keyword            : stop bidding on a specific (keyword, match_type)
  - pause_product_ad         : pause one ASIN inside an ad group
  - pause_product_target     : drop one competitor-ASIN / category-target clause
  - adjust_keyword_bid       : +/- bid on one keyword (include % and direction)
  - adjust_product_ad_bid    : +/- bid on one product-ad row
  - adjust_placement_modifier: +/- % on TOP / PRODUCT_PAGE / SITE_AMAZON_BUSINESS
  - add_negative_keyword     : block a specific search term across an ad group
  - raise_campaign_budget    : ONLY if actual_avg_daily_spend >= 80% of daily_budget AND campaign ROAS > target
  - pause_campaign           : LAST RESORT — see rejection rules

Rejection rules the analyst MUST obey:
  1. "Don't kill the parent while its dominant child is winning": if the
     top child holds ≥ 40% of spend AND its ROAS ≥ 1.5×, NEVER propose
     pause_campaign. Instead, propose surgery on the losers.
  2. "Don't raise budget when bids are the throttle": if utilization
     < 70%, refuse raise_campaign_budget. Propose adjust_keyword_bid on
     high-ROAS winners instead.
  3. "Don't act on <₹50 tail positions": ignore zero-purchase rows that
     spent less than ₹50 — noise, not signal.
  4. "Zero-purchase AND cost > ₹100 = candidate for pause". Over ₹200
     with 0 purchases = strong pause candidate.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict

from ads_agent.agent.llm import complete
from ads_agent.agent.analysis.campaign_decomposer import CampaignHierarchy
from ads_agent.playbook import node_brief

log = logging.getLogger(__name__)


METHODOLOGY_PROMPT = """\
You are a senior Amazon Ads PPC strategist reviewing ONE Sponsored
Products campaign. Follow this methodology — do not skip steps, do not
aggregate insights that should stay per-entity.

STEP 1 — READ THE CONCENTRATION DATA FIRST
Before any recommendation, internalize:
  - What % of spend is in the top-1 child? Top-3?
  - What's the ROAS of the top-1 child vs the tail?
  - How many children have ZERO purchases and non-trivial spend?
These numbers are pre-computed for you in `concentration.*`. Use them.

STEP 2 — CLASSIFY EVERY CHILD
For each child in the `children` array, classify it into one of:
  HERO          : ≥15% of campaign spend AND ROAS ≥ 1.5× AND purchases14d ≥ 1
  BACKUP        : 1–15% of campaign spend AND ROAS ≥ 1.5×
  HIDDEN_WINNER : <5% of campaign spend AND ROAS ≥ 3.0× AND purchases14d ≥ 1  (under-funded)
  DEAD          : cost > ₹100 AND purchases14d = 0
  MARGINAL      : 0.5× ≤ ROAS < 1.5× with meaningful spend
  TAIL          : spend < ₹50 — ignore, not enough signal

STEP 3 — CHOOSE ACTIONS AT THE RIGHT LEVEL
NEVER propose pause_campaign if there is at least one HERO child.
Instead:
  - DEAD children → pause_keyword / pause_product_ad / pause_product_target
  - MARGINAL with cost > ₹200 → adjust_keyword_bid −20–40%
  - HIDDEN_WINNER → adjust_keyword_bid or adjust_product_ad_bid +20–40%
  - HERO → leave alone
  - Campaign-level action is ONLY allowed when ≥70% of children share the same verdict

STEP 4 — CHECK THE BUDGET CONSTRAINT
If `budget_utilization_pct ≥ 80` AND campaign ROAS ≥ 1.5×:
  → Include a `raise_campaign_budget` action with a specific new cap.
If utilization < 70%, do NOT propose budget changes. Bids are the throttle.

STEP 5 — OUTPUT FORMAT (STRICT MARKDOWN — NOT JSON)
Output exactly this structure. NO preamble, NO closing notes.

## DIAGNOSIS
A 4–6 sentence prose summary. Name the winners (by label), name the losers,
and say where the real spend-leak is. Cite specific numbers.

## ACTIONS
For every proposed action, one block in this format exactly:

### {N}. {action_kind} · {target_label}
- target_level: keyword | product_ad | product_target | campaign
- target_id: <the MAP / Amazon resource id>
- rationale: <why this action, with specific numbers from the data>
- expected_impact: <e.g. "reclaim ~₹234/week" or "unlock ~₹800/week un-served demand">
- safety_check: <what could go wrong, or "none">

Use sequential numbers (1, 2, 3…). List actions ordered by impact DESC.

BRAND CONTEXT
  - <client>: Indian pet-supplement D2C brand (India + UAE)
  - Healthy SP ROAS target: ≥ 1.8×
  - TACoS target: ≤ 15%
  - Tail-spend floor: ₹50

Action-kind taxonomy:
  pause_keyword · pause_product_ad · pause_product_target ·
  adjust_keyword_bid · adjust_product_ad_bid · adjust_placement_modifier ·
  add_negative_keyword · raise_campaign_budget · pause_campaign (last resort)

The campaign to analyze:
"""


import re


def _parse_markdown_report(text: str) -> dict:
    """Parse the analyst's markdown output into {diagnosis, actions}.

    Markdown is easier for LLMs to produce correctly than JSON — fewer
    escape-character traps. We parse with regex, not JSON.
    """
    # Diagnosis = everything between "## DIAGNOSIS" and "## ACTIONS"
    m = re.search(r"##\s*DIAGNOSIS\s*\n(.*?)(?=\n##\s*ACTIONS)",
                  text, re.DOTALL | re.IGNORECASE)
    diagnosis = m.group(1).strip() if m else ""

    actions: list[dict] = []
    # Each action block: "### N. action_kind · target_label" then bullet lines
    for block in re.finditer(
        r"###\s*\d+\.\s*(\S+)\s*·\s*([^\n]+)\n(.*?)(?=\n###\s*\d+\.|\Z)",
        text, re.DOTALL,
    ):
        action_kind = block.group(1).strip()
        target_label = block.group(2).strip()
        body = block.group(3)

        def _field(name: str) -> str:
            m = re.search(rf"-\s*{re.escape(name)}\s*:\s*(.*?)(?=\n\s*-\s|\Z)",
                          body, re.DOTALL | re.IGNORECASE)
            return m.group(1).strip() if m else ""

        actions.append({
            "action_kind":  action_kind,
            "target_label": target_label,
            "target_level": _field("target_level"),
            "target_id":    _field("target_id"),
            "rationale":    _field("rationale"),
            "expected_impact": _field("expected_impact"),
            "safety_check": _field("safety_check"),
        })

    return {"diagnosis": diagnosis, "actions": actions}


async def analyze_campaign(hierarchy: CampaignHierarchy,
                           *,
                           brand: str = "default",
                           model_tier: str = "smart") -> dict:
    """Run the methodology prompt against the decomposed campaign data.

    Loads the methodology from the brand's playbook (Section X →
    `amazon_recs` node brief). Falls back to the hardcoded
    METHODOLOGY_PROMPT if the playbook lacks that brief — so this still
    works on a fresh install before the private playbook package is set up.

    Returns {diagnosis, actions}. Markdown-based; tolerates LLM formatting
    quirks better than JSON.
    """
    playbook_methodology = node_brief("amazon_recs", brand)
    if playbook_methodology:
        methodology = playbook_methodology + "\n\nThe campaign to analyze:"
    else:
        log.warning(
            "playbook has no amazon_recs brief for brand %r — falling back "
            "to hardcoded METHODOLOGY_PROMPT. Add the brief to "
            "playbooks/<brand>.md Section X for brand-tuned analysis.",
            brand,
        )
        methodology = METHODOLOGY_PROMPT

    payload = hierarchy.to_dict()
    data_block = json.dumps(payload, indent=2, default=str)
    prompt = methodology + "\n\n" + data_block

    # max_tokens=12000 covers Gemini 2.5 Pro's hidden "thinking" budget
    # (often 4-8k tokens on multi-step reasoning) plus ~2-4k of actual
    # markdown output. Undersizing this is what cut diagnosis off at
    # sentence 3 in v1 of this function.
    raw = await complete(
        prompt, tier=model_tier, max_tokens=12000,
        system=("You are a senior PPC strategist. Output ONLY the markdown "
                "report as specified — no preamble, no JSON, no code fences. "
                "Keep prose tight; save tokens for the ACTIONS section."),
    )
    return _parse_markdown_report(raw)
