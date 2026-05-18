"""Discord side of the action approval loop.

Mirrors actions.notifier (Telegram) for Discord:

  - post_proposal_to_discord(channel_id, action_id, text)
      → posts a message with two buttons (Approve / Reject) whose
        custom_ids are act:approve:N / act:reject:N. Returns the
        Discord message_id so the consumer can edit it after click.

  - edit_resolution(channel_id, message_id, new_text)
      → strips buttons + replaces text with the resolution summary.

Auth: uses DISCORD_BOT_TOKEN from /home/support/.config/glitch-discord/env
(loaded by the inbox_consumer). The agent process inherits it via
EnvironmentFile= in the systemd unit, but for cron-driven planner runs
we re-load the file here so it works standalone too.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

log = logging.getLogger(__name__)

_DISCORD_ENV = Path("/home/support/.config/glitch-discord/env")
if _DISCORD_ENV.exists():
    load_dotenv(_DISCORD_ENV)

_BASE = "https://discord.com/api/v10"

# Discord interaction component types
_TYPE_ACTION_ROW = 1
_TYPE_BUTTON = 2

# Button styles
_STYLE_PRIMARY = 1   # blurple
_STYLE_LINK = 5      # URL button
_STYLE_SUCCESS = 3   # green
_STYLE_DANGER  = 4   # red


class DiscordNotifyError(RuntimeError):
    """Raised when the Discord post fails irrecoverably."""


def _token() -> str:
    tok = (os.environ.get("DISCORD_BOT_TOKEN") or "").strip()
    if not tok:
        raise DiscordNotifyError(
            "DISCORD_BOT_TOKEN not set — /home/support/.config/glitch-discord/env"
        )
    return tok


def _components_for(action_id: int) -> list[dict]:
    """One action row with Approve (green) + Reject (red) buttons."""
    return [{
        "type": _TYPE_ACTION_ROW,
        "components": [
            {
                "type": _TYPE_BUTTON,
                "style": _STYLE_SUCCESS,
                "label": "Approve",
                "custom_id": f"act:approve:{action_id}",
                "emoji": {"name": "✅"},
            },
            {
                "type": _TYPE_BUTTON,
                "style": _STYLE_DANGER,
                "label": "Reject",
                "custom_id": f"act:reject:{action_id}",
                "emoji": {"name": "❌"},
            },
        ],
    }]


def _tiktok_done_components(manifest_id: str, advertiser_id: str) -> list[dict]:
    url = "https://ads.tiktok.com/i18n/campaign"
    if advertiser_id:
        url = f"{url}?aadvid={advertiser_id}"
    return [{
        "type": _TYPE_ACTION_ROW,
        "components": [
            {
                "type": _TYPE_BUTTON,
                "style": _STYLE_LINK,
                "label": "Open TikTok Ads Manager",
                "url": url,
            },
            {
                "type": _TYPE_BUTTON,
                "style": _STYLE_PRIMARY,
                "label": "Enable now",
                "custom_id": f"tiktok:enable:{manifest_id}",
            },
        ],
    }]


async def _post_with_components(
    channel_id: int,
    text: str,
    components: list[dict],
) -> int:
    if len(text) > 1900:
        text = text[:1900] + "…"
    body = {
        "content": text,
        "components": components,
        "allowed_mentions": {"parse": []},
    }
    headers = {"Authorization": f"Bot {_token()}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=15.0) as cli:
        r = await cli.post(f"{_BASE}/channels/{channel_id}/messages", headers=headers, json=body)
    if r.status_code >= 400:
        raise DiscordNotifyError(
            f"discord post HTTP {r.status_code} ch={channel_id}: {r.text[:300]}"
        )
    return int((r.json() or {}).get("id", 0))


async def post_proposal_to_discord(
    channel_id: int, action_id: int, text: str,
) -> int:
    """Post the proposal + buttons. Returns the Discord message_id.

    Long messages are *not* split here — Discord's 2000-char per-message
    cap means callers should keep proposal text well under that. The
    notifier's existing Telegram formatter already keeps proposals
    compact (~1-1.5 KB).
    """
    msg_id = await _post_with_components(channel_id, text, _components_for(action_id))
    log.info("discord proposal action=%d posted as msg=%d ch=%d", action_id, msg_id, channel_id)
    return msg_id


async def post_tiktok_done_to_discord(
    channel_id: int,
    text: str,
    *,
    manifest_id: str,
    advertiser_id: str,
) -> int:
    """Post the Meta -> TikTok completion card with Ads Manager + Enable buttons."""
    msg_id = await _post_with_components(
        channel_id,
        text,
        _tiktok_done_components(manifest_id, advertiser_id),
    )
    log.info("discord tiktok completion manifest=%s posted msg=%d", manifest_id, msg_id)
    return msg_id


async def edit_resolution(
    channel_id: int, message_id: int, new_text: str,
) -> None:
    """Replace text + remove buttons after the action resolves."""
    if len(new_text) > 1900:
        new_text = new_text[:1900] + "…"
    body = {
        "content": new_text,
        "components": [],  # strip buttons
        "allowed_mentions": {"parse": []},
    }
    headers = {"Authorization": f"Bot {_token()}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=15.0) as cli:
        r = await cli.patch(
            f"{_BASE}/channels/{channel_id}/messages/{message_id}",
            headers=headers, json=body,
        )
    if r.status_code >= 400:
        log.warning(
            "discord edit failed ch=%d msg=%d HTTP %d: %s",
            channel_id, message_id, r.status_code, r.text[:200],
        )
        return
    log.info("discord edited resolution ch=%d msg=%d", channel_id, message_id)
