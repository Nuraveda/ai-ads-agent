"""Per-store proposal target config.

Each store can be wired to receive proposals on Telegram, Discord, or both
(during the 48h cutover window). Reads from env STORE_PROPOSAL_TARGETS_JSON;
falls back to the original LIGHTHOUSE_CHAT_ID Telegram-only behaviour if unset.

env shape:
  STORE_PROPOSAL_TARGETS_JSON = {
    "<client>-ind":    {"telegram_chat_id": -100..., "discord_channel_id": 1497086918012309565},
    "<client>-global": {"telegram_chat_id": -100..., "discord_channel_id": 1497086918012309565},
    "urban":          {"discord_channel_id": 1497089775486631946}
  }

Either or both fields may be present per slug. A slug with neither is treated
as "no proposal channel configured" and the planner skips it.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProposalTarget:
    telegram_chat_id: int | None
    discord_channel_id: int | None

    @property
    def has_any(self) -> bool:
        return self.telegram_chat_id is not None or self.discord_channel_id is not None

    @property
    def is_dual(self) -> bool:
        return self.telegram_chat_id is not None and self.discord_channel_id is not None


def _parse_int(v) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _load_from_env() -> dict[str, ProposalTarget]:
    raw = os.environ.get("STORE_PROPOSAL_TARGETS_JSON", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("STORE_PROPOSAL_TARGETS_JSON invalid: %s", e)
        return {}
    out: dict[str, ProposalTarget] = {}
    for slug, cfg in parsed.items():
        if not isinstance(cfg, dict):
            continue
        out[slug] = ProposalTarget(
            telegram_chat_id=_parse_int(cfg.get("telegram_chat_id")),
            discord_channel_id=_parse_int(cfg.get("discord_channel_id")),
        )
    return out


_TARGETS: dict[str, ProposalTarget] | None = None


def proposal_target(slug: str) -> ProposalTarget | None:
    """Return the ProposalTarget for a slug, or None if not configured.

    Falls back to legacy LIGHTHOUSE_CHAT_ID for <client>-* if the env var is
    unset, so a fresh deploy of just this code without env updates still
    posts to Telegram exactly as before.
    """
    global _TARGETS
    if _TARGETS is None:
        _TARGETS = _load_from_env()

    cfg = _TARGETS.get(slug)
    if cfg and cfg.has_any:
        return cfg

    # Legacy fallback — if no per-slug target is configured but the lighthouse
    # client supergroup chat id is set, route there. Set LIGHTHOUSE_FALLBACK_SLUGS
    # to an env-driven JSON list to opt slugs into this fallback, e.g.
    #   LIGHTHOUSE_FALLBACK_SLUGS=["store-a","store-b"]
    import json as _json
    import os as _os
    fb = _os.environ.get("LIGHTHOUSE_FALLBACK_SLUGS", "").strip()
    if fb:
        try:
            slugs = set(_json.loads(fb)) if fb else set()
        except _json.JSONDecodeError:
            slugs = set()
        if slug in slugs:
            try:
                from ads_agent.actions.models import LIGHTHOUSE_CHAT_ID
                return ProposalTarget(
                    telegram_chat_id=LIGHTHOUSE_CHAT_ID,
                    discord_channel_id=None,
                )
            except Exception:  # noqa: BLE001
                pass
    return None


def reload_targets() -> None:
    """Force a fresh read of env (for tests + after env changes)."""
    global _TARGETS
    _TARGETS = None


def configured_slugs() -> list[str]:
    """Slugs with any proposal target configured."""
    global _TARGETS
    if _TARGETS is None:
        _TARGETS = _load_from_env()
    out = list(_TARGETS.keys())
    # Include legacy-fallback slugs (LIGHTHOUSE_FALLBACK_SLUGS env) if their
    # proposal_target() resolves via the back-compat path.
    import json as _json
    import os as _os
    fb = _os.environ.get("LIGHTHOUSE_FALLBACK_SLUGS", "").strip()
    if fb:
        try:
            for slug in _json.loads(fb) or []:
                if slug not in out and proposal_target(slug):
                    out.append(slug)
        except _json.JSONDecodeError:
            pass
    return out
