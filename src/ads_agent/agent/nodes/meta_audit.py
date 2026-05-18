"""meta_audit: operator-grade D2C Meta Ads account audit.

Runs the per-brand methodology prompt (playbooks/<brand>.md Section X →
`meta_audit`) against a decomposed MetaAccountHierarchy. Output is the
four-verb action list: SCALE / REFRESH / PAUSE / WATCH.

Trigger:
  Telegram — /meta_audit <store> [days]
  Discord  — /meta_audit <store> [days]

Dispatcher route key: "meta_audit".

Pipeline cost: 4 Meta Graph API pulls (≈3-6 s) + 1 LLM complete
(≈12-25 s on Gemini 2.5 Pro with 16k max_tokens). Total ~20-35 s per run
for an account with <50 campaigns and <500 ads. Cached per (account, day).
"""
from __future__ import annotations

import logging

from ads_agent.agent.analysis.meta_audit_analyst import audit_meta_account
from ads_agent.agent.analysis.meta_decomposer import decompose_meta_account
# brand_registry stripped from open-source release; provide your own
from ads_agent.config import get_store
from ads_agent.meta.graph_client import MetaGraphError

log = logging.getLogger(__name__)


def _brand_for(slug: str) -> str:
    """Store slug → playbook brand key, via STORE_BRAND_REGISTRY_JSON."""
    return brand_for(slug)


async def meta_audit_node(state: dict) -> dict:
    slug = state["store_slug"]
    days = int(state.get("days", 14))
    store = get_store(slug)
    if store is None:
        return {**state, "reply_text": f"Unknown store: `{slug}`. /stores for list."}

    ad_account_id = store.meta_ad_account
    if not ad_account_id:
        return {
            **state,
            "reply_text": (
                f"*{store.brand}* · no Meta ad account mapped for `{slug}`. "
                "Add it to STORES_JSON in .env."
            ),
        }

    try:
        hierarchy = await decompose_meta_account(
            ad_account_id, days=days, store_slug=slug,
        )
    except MetaGraphError as e:
        return {**state, "reply_text": f"*{store.brand}* · Meta audit failed at decompose: `{e}`"}
    except Exception as e:  # noqa: BLE001
        log.exception("meta_audit decompose unexpected")
        return {**state, "reply_text": f"*{store.brand}* · Meta audit unexpected decompose error: `{e}`"}

    if hierarchy.summary.spend == 0 and hierarchy.summary.impressions == 0:
        return {
            **state,
            "reply_text": (
                f"*{store.brand}* · Meta audit\n\n"
                f"No delivery in the last {days} days (account `{ad_account_id}` "
                f"had 0 spend, 0 impressions). Nothing to audit."
            ),
        }

    try:
        report = await audit_meta_account(hierarchy, brand=_brand_for(slug))
    except Exception as e:  # noqa: BLE001
        log.exception("meta_audit analyst unexpected")
        return {
            **state,
            "reply_text": (
                f"*{store.brand}* · Meta audit — decompose ok, analyst failed: `{e}`\n"
                f"Account snapshot: {hierarchy.summary.n_campaigns} campaigns · "
                f"{hierarchy.summary.spend:,.0f} {hierarchy.summary.currency} spend · "
                f"{hierarchy.summary.blended_roas:.2f}× blended ROAS."
            ),
        }

    body = (report.get("diagnosis") or "").strip()
    halo_downgrades = report.get("halo_downgrades") or 0
    noise = hierarchy.skipped_noise or {}
    noise_line = ""
    if noise.get("count", 0) > 0:
        noise_line = (
            f" · {noise['count']} low-signal campaigns skipped "
            f"({noise['total_spend']:,.0f} {hierarchy.summary.currency} total, "
            f"{noise['reason']})"
        )
    header = (
        f"*{store.brand} · Meta audit* · account `{ad_account_id}` · "
        f"{days}d · {len(hierarchy.campaigns)}/{hierarchy.summary.n_campaigns} campaigns "
        f"analysed{noise_line}\n"
        f"spend {hierarchy.summary.spend:,.0f} {hierarchy.summary.currency} · "
        f"{hierarchy.summary.purchases} purchases · "
        f"blended ROAS {hierarchy.summary.blended_roas:.2f}×\n"
        f"_methodology: playbooks/{_brand_for(slug)}.md · Section X "
        f"(refs/meta-audit-checklist.md · refs/2025-platform-changes.md)_\n"
    )
    if halo_downgrades:
        header += (
            f"_⚠ {halo_downgrades} action(s) downgraded for unverified halo "
            f"citations — see [HALO_UNVERIFIED] tags in rationales below_\n"
        )
    # Surface name-vs-URL drift so operators catch misnamed campaigns without
    # reading the whole audit body.
    mismatches = (hierarchy.destination_mismatches or {}).get("buckets", {})
    name_only = mismatches.get("name_only", {}) or {}
    if name_only.get("spend", 0) > 0:
        header += (
            f"_⚠ {name_only['ads']} ad(s) / {name_only['spend']:,.0f} "
            f"{hierarchy.summary.currency} have *Amazon-sounding names but "
            f"point at Shopify URLs* — campaign drift; see destination_mismatches_\n"
        )
    header += "\n"

    # Health Score banner — terse, scannable
    health = report.get("health") or {}
    health_banner = ""
    if health:
        def bar(score: int) -> str:
            filled = round(score / 10)
            return "█" * filled + "░" * (10 - filled)
        health_banner = (
            f"*Health: {health['total']}/100 · Grade {health['grade']}*\n"
            f"```\n"
            f"Pixel/CAPI {health['pixel_capi']:>3}/100  {bar(health['pixel_capi'])}  (30%)\n"
            f"Creative   {health['creative']:>3}/100  {bar(health['creative'])}  (30%)\n"
            f"Structure  {health['structure']:>3}/100  {bar(health['structure'])}  (20%)\n"
            f"Audience   {health['audience']:>3}/100  {bar(health['audience'])}  (20%)\n"
            f"```\n"
        )

    # Quick Wins — high-severity + low-effort, surfaced before the full report
    qw = report.get("quick_wins") or []
    quick_wins_block = ""
    if qw:
        lines = ["*Quick Wins (high-severity × low-effort):*"]
        for i, a in enumerate(qw[:5], 1):
            label = (a.get("target_label") or "?")[:60]
            check = (a.get("check_id") or "").strip()
            verb  = a.get("action_kind", "?")
            impact = (a.get("expected_impact") or "").strip()
            lines.append(
                f"  {i}. `{verb}` · {label}"
                + (f" · `{check}`" if check else "")
                + (f" → {impact[:80]}" if impact else "")
            )
        quick_wins_block = "\n".join(lines) + "\n\n"

    return {
        **state,
        "reply_text": header + health_banner + quick_wins_block + body,
    }
