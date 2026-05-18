"""BSK-002 → brain bridge (GROW-BIND-1 + 1b, 2026-05-17).

Mirrors every command-turn the Ads agent logs locally (in
`ads_agent.agent_memory`) onto the shared `glitch-brain-mcp` so
sibling agents on the same brand can see what Ads just did.

This is **additive**: the existing local pgvector path stays as the
agent's private vector-searchable memory. The brain mirror is the
sibling-visible coordination layer (per-`(brand, agent)` bearer
token; the server scopes to the right brand from the token).

Wiring contract (updated for BIND-1b):
  - Env `GLITCH_BRAIN_MCP_URL` overrides the brain URL.
  - Per-brand tokens: `BRAIN_TOKEN_BSK_002_<BRAND_SLUG_UPPER>` (e.g.
    `BRAIN_TOKEN_BSK_002_example`, `BRAIN_TOKEN_BSK_002_URBAN_CLASSICS`).
    The bridge resolves the right one from the incoming `store_slug`
    parameter — necessary because the Ads agent handles multiple
    brands in a single process and each (brand, agent) has its own
    bearer token issued in glitch-brain-mcp/tokens.issued.txt.
  - Backward-compat fallback: bare `BRAIN_TOKEN_BSK_002` if it's set
    (single-brand dev environments only). Per-brand wins when both
    are present.
  - Unknown / None `store_slug` → silent no-op (no brand context to
    pick a token).
  - All brain calls are fire-and-forget; brain failures NEVER block
    or fail the local insert.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any

from grow_platform.brain import BrainAuthError, BrainClient, BrainError
from grow_platform.brain.limits import cap_payload

log = logging.getLogger(__name__)

_DEFAULT_BRAIN_URL = "http://127.0.0.1:3107/mcp"
# Per-brand env key prefix. Suffix is the brand slug UPPER-snaked.
_BRAIN_TOKEN_PREFIX = "BRAIN_TOKEN_BSK_002_"
# Backward-compat: single-token env var (pre-BIND-1b deployments).
_BRAIN_TOKEN_LEGACY = "BRAIN_TOKEN_BSK_002"
_BRAIN_URL_ENV = "GLITCH_BRAIN_MCP_URL"

# Slug → env-suffix normalizer. Brand slugs use kebab-case
# (`urban-classics`); env keys use UPPER_SNAKE_CASE.
_NON_ENV_CHARS = re.compile(r"[^A-Z0-9_]")


def _slug_to_env_suffix(store_slug: str | None) -> str | None:
    """Turn a brand slug into the env-key suffix used for per-brand
    token lookup. Returns None when the slug is empty or null —
    callers treat that as 'no brand context, no mirror'."""
    if not store_slug:
        return None
    s = store_slug.strip().upper().replace("-", "_")
    s = _NON_ENV_CHARS.sub("", s)
    return s or None


def _brain_token_for(store_slug: str | None) -> str | None:
    """Resolve the BSK-002 bearer token for one brand.

    Lookup order:
      1. Per-brand: `BRAIN_TOKEN_BSK_002_<UPPER_SNAKED_BRAND_SLUG>`.
      2. Legacy bare: `BRAIN_TOKEN_BSK_002` (pre-BIND-1b single-token
         setup; only used if the per-brand key isn't set).

    Returns None when neither resolves OR `store_slug` is None — both
    cases the bridge treats as 'silent no-op'.
    """
    suffix = _slug_to_env_suffix(store_slug)
    if suffix is not None:
        per_brand = os.environ.get(_BRAIN_TOKEN_PREFIX + suffix)
        if per_brand:
            return per_brand
    legacy = os.environ.get(_BRAIN_TOKEN_LEGACY)
    return legacy or None


def _brain_url() -> str:
    return os.environ.get(_BRAIN_URL_ENV, _DEFAULT_BRAIN_URL)


def brain_available_for(store_slug: str | None) -> bool:
    """True when a BSK-002 brain token resolves for this brand.

    Callers use this for diagnostics (e.g. log on startup); the bridge
    functions below also check internally so it's safe to call them
    blind — they just no-op when unconfigured.
    """
    return _brain_token_for(store_slug) is not None


def brain_available() -> bool:
    """Legacy availability check — kept for backward-compat with any
    diagnostic code that asks 'is the brain wired at all?' Returns True
    if ANY per-brand BRAIN_TOKEN_BSK_002_* env var OR the legacy bare
    BRAIN_TOKEN_BSK_002 is set."""
    if any(k.startswith(_BRAIN_TOKEN_PREFIX) for k in os.environ):
        return any(os.environ[k] for k in os.environ if k.startswith(_BRAIN_TOKEN_PREFIX))
    return bool(os.environ.get(_BRAIN_TOKEN_LEGACY))


def _summarize_for_brain(reply_text: str, max_chars: int = 240) -> str:
    """Take the agent's full reply text and clip to a readable summary."""
    t = (reply_text or "").strip().replace("\n", " ")
    if len(t) <= max_chars:
        return t
    return t[: max_chars - 1].rstrip() + "…"


async def mirror_turn_to_brain(
    *,
    command: str,
    store_slug: str | None,
    args: dict[str, Any] | None,
    reply_text: str,
    key_metrics: dict[str, Any] | None,
    agent_reasoning: str | None,
    kind: str,
) -> None:
    """Best-effort mirror of one Telegram-command turn to the brain.

    Called from `store.log_turn` after the local insert succeeds. Errors
    are caught + logged at WARNING; the local insert is never rolled
    back on brain failure.

    No-ops silently when the BSK-002 brain token isn't configured —
    keeps the agent runnable in dev / on a fresh box without brain
    plumbing.
    """
    token = _brain_token_for(store_slug)
    if token is None:
        # Unconfigured (no per-brand token AND no legacy fallback) OR
        # no brand context (store_slug is None) → silent no-op. The
        # agent's primary data path is the local agent_memory table;
        # brain visibility is purely additive coordination.
        return

    payload: dict[str, Any] = {"args": args or {}, "kind": kind}
    if key_metrics is not None:
        payload["key_metrics"] = key_metrics
    if agent_reasoning is not None:
        payload["reasoning"] = agent_reasoning

    try:
        async with BrainClient(url=_brain_url(), token=token) as brain:
            await brain.append_activity(
                action=command,
                summary=_summarize_for_brain(reply_text),
                subject=store_slug,
                payload=cap_payload(payload),
                # agent_sku is also implied by the token's Principal on
                # the server side; sending it explicitly makes the
                # team_state filter exact when the brand has multiple
                # tokens (e.g. dev + prod) for the same agent.
                agent_sku="BSK-002",
            )
    except BrainAuthError:
        suffix = _slug_to_env_suffix(store_slug) or "<no-store-slug>"
        log.warning(
            "ads_agent brain mirror auth failed for store_slug=%r (BSK-002); "
            "check env var %s%s (or legacy %s)",
            store_slug, _BRAIN_TOKEN_PREFIX, suffix, _BRAIN_TOKEN_LEGACY,
        )
    except BrainError as e:
        log.warning("ads_agent brain mirror failed: %s", e)
    except Exception:  # noqa: BLE001 — never let brain take down the agent
        log.exception("ads_agent brain mirror raised unexpectedly")


def schedule_brain_mirror(
    *,
    command: str,
    store_slug: str | None,
    args: dict[str, Any] | None,
    reply_text: str,
    key_metrics: dict[str, Any] | None = None,
    agent_reasoning: str | None = None,
    kind: str = "insight",
) -> None:
    """Schedule a brain mirror on the running event loop.

    Called by `store.log_turn` after a successful local insert. Mirrors
    the local store's fire-and-forget convention so brain mirroring is
    invisible to Telegram-handler latency.
    """
    # Per-brand resolution: only schedule the mirror if a token for
    # THIS brand resolves. Avoids creating never-awaited tasks when
    # the brand has no token in env (or the agent received a None
    # store_slug, e.g. a Telegram command with no brand context).
    if not brain_available_for(store_slug):
        return
    asyncio.ensure_future(
        mirror_turn_to_brain(
            command=command,
            store_slug=store_slug,
            args=args,
            reply_text=reply_text,
            key_metrics=key_metrics,
            agent_reasoning=agent_reasoning,
            kind=kind,
        )
    )
