"""ideas: generate numbered creative briefs for offline production.

Given a store's top 3 performing ads (by spend with positive ROAS), produce
5 numbered creative briefs that extend / remix / counter the winning patterns.
Agent doesn't produce the creative itself — it produces a brief the user can
hand to a vendor/editor.

Each brief has:
  - Angle          — core emotional/conceptual direction
  - Hook           — first 1.5s framing suggestion
  - Body direction — copy leads, trust signals, offer phrasing
  - Audience fit   — who this speaks to + why it matches the family
  - Rationale      — which numbers / patterns from the user's data make this worth testing
"""
from __future__ import annotations

from ads_agent.agent.llm import complete_vision
from ads_agent.agent.nodes.creative_critique import BRAND_CONTEXT
# brand_registry stripped from open-source release; provide your own
from ads_agent.config import STORE_AD_ACCOUNTS, get_store
from ads_agent.meta.graph_client import MetaGraphError, ads_for_account


IDEAS_SYSTEM = """You are a creative director producing ad briefs for an e-commerce brand.
You receive: brand-family context, the top 3 performing ads (metrics + thumbnails + body copy), and the user's stated goal.

Produce exactly 5 numbered creative briefs for the VENDOR/EDITOR to produce next. Format EXACTLY:

*Brief 1 — <short angle name>*
• *Angle:* <one-sentence core emotional/conceptual direction>
• *Hook (first 1.5s):* <concrete visual suggestion — what's on frame, what text overlay>
• *Body direction:* <2–3 sentences on copy leads, trust signals, offer phrasing, CTA>
• *Audience fit:* <1–2 sentences on who this speaks to and why it matches this brand family>
• *Rationale:* <1–2 sentences tying this brief to observed data — which winner it extends, which pattern it tests, what CTR/ROAS signal justifies it>

*Brief 2 …*   (same structure)
...
*Brief 5 …*

Rules:
- Briefs must be TESTABLY DIFFERENT from each other — don't produce 5 copies of the same idea with rephrased hooks.
- At least 2 briefs should extend the winning pattern (do what's working more/better).
- At least 2 briefs should test a new angle not yet in the top 3 (new audience emotion, new proof element, new offer).
- Reference specific numbers from the data when justifying a brief.
- Match tone to the brand family — dropship vs legit-brand vs authentic-spiritual-brand differ sharply.
- No preamble, no conclusion. Start with *Brief 1* and end with the last rationale.
"""


async def ideas_node(state: dict) -> dict:
    slug = state["store_slug"]
    days = int(state.get("days", 30))
    store = get_store(slug)
    if store is None:
        return {**state, "reply_text": f"Unknown store: `{slug}`"}

    ad_accounts = STORE_AD_ACCOUNTS.get(store.slug, [])
    if not ad_accounts:
        return {**state, "reply_text": f"No Meta ad accounts mapped for `{slug}`."}

    # Gather ads with spend across all linked accounts
    all_ads: list[dict] = []
    for act in ad_accounts:
        try:
            all_ads.extend(await ads_for_account(act, days=days))
        except MetaGraphError:
            continue

    # Pick top 3 by spend with at least SOME purchases (otherwise it's a loser, not a winner)
    with_purchases = [a for a in all_ads if a["purchases"] > 0 and a["spend"] > 0]
    if len(with_purchases) < 2:
        return {**state, "reply_text": (
            f"*{store.brand}* · need at least 2 winning ads in last {days}d to generate ideas. "
            f"Found {len(with_purchases)}. Run `/ads {slug} {days}` to inspect."
        )}

    with_purchases.sort(key=lambda a: (a["purchase_value"], a["spend"]), reverse=True)
    top = with_purchases[:3]

    # Build the context block the LLM sees
    family_ctx = BRAND_CONTEXT.get(brand_for(slug), BRAND_CONTEXT["default"])
    winners_block_lines = []
    thumbnails: list[str] = []
    for i, a in enumerate(top, 1):
        creative = a.get("creative", {})
        body = (creative.get("body") or "").replace("\n", " ").strip()
        if len(body) > 250:
            body = body[:247] + "…"
        thumb = creative.get("thumbnail_url") or ""
        if thumb:
            thumbnails.append(thumb)
        winners_block_lines.append(
            f"WINNER {i}: {a['ad_name']}\n"
            f"  spend={a['spend']:,.2f} {a['currency']}  ·  CTR={a['ctr']:.2f}%  ·  CPC={a['cpc']:.2f}  ·  "
            f"purchases={a['purchases']}  ·  purchase_value={a['purchase_value']:,.2f}  ·  ROAS={a['reported_roas']:.2f}x\n"
            f"  body: \"{body}\""
        )
    winners_block = "\n\n".join(winners_block_lines)

    goal_hint = state.get("goal_hint") or (
        "Generate briefs that extend the winning patterns while testing 2 new angles."
    )

    prior = state.get("prior_context", "") or ""
    prompt = (
        (f"{prior}\n\n" if prior else "")
        + f"BRAND FAMILY CONTEXT:\n{family_ctx}\n\n"
        + f"STORE: {store.brand} ({store.shop_domain}) · {store.currency}\n\n"
        + f"TOP 3 WINNERS (last {days}d):\n{winners_block}\n\n"
        + f"GOAL: {goal_hint}\n\n"
        + "Produce the 5 numbered briefs now. "
        + "If <prior_context> shows previous /ideas runs for this store, do NOT repeat angles that were already proposed — extend or diverge."
    )

    # Brand-playbook brief keeps the LLM grounded in codified expertise
    # (Section X of /playbooks/<brand>.md). Falls back to vanilla IDEAS_SYSTEM
    # when no playbook exists for the brand (Urban family today).
    from ads_agent.playbook import node_brief
    brand_brief = node_brief("ideas", store.slug.split("-")[0])
    system_prompt = IDEAS_SYSTEM + (
        f"\n\n---\nBRAND PLAYBOOK CONTEXT (authoritative, overrides generic advice):\n{brand_brief}\n"
        if brand_brief else ""
    )

    if thumbnails:
        out = await complete_vision(prompt, thumbnails, tier="smart", system=system_prompt, max_tokens=4000)
    else:
        from ads_agent.agent.llm import complete
        out = await complete(prompt, tier="smart", system=system_prompt, max_tokens=4000)

    header = f"*Creative briefs · {store.brand} · based on top 3 of last {days}d*\n\n"
    return {**state, "reply_text": header + out}
