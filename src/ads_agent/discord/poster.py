"""Post messages back to a Discord channel via the REST API.

Uses DISCORD_BOT_TOKEN from /home/support/.config/glitch-discord/env
(the same token the sibling bot uses). Direct HTTPS — no discord.py
runtime needed on this side, so the consumer is light and restart-safe.

Messages > 2000 chars are split into multiple posts. Code blocks are
preserved across splits where possible.
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
_MAX = 2000  # Discord single-message char limit


class DiscordPostError(RuntimeError):
    pass


def _token() -> str:
    tok = (os.environ.get("DISCORD_BOT_TOKEN") or "").strip()
    if not tok:
        raise DiscordPostError(
            "DISCORD_BOT_TOKEN missing — is /home/support/.config/glitch-discord/env present?"
        )
    return tok


def _chunks(text: str, limit: int = _MAX) -> list[str]:
    """Split a long message into Discord-safe chunks, preferring line breaks."""
    if len(text) <= limit:
        return [text]
    out: list[str] = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < int(limit * 0.5):  # no good linebreak, hard-cut
            cut = limit
        out.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        out.append(remaining)
    return out


async def post_message(
    channel_id: str,
    content: str,
    *,
    reply_to_message_id: str | None = None,
) -> list[str]:
    """Post `content` to `channel_id`, splitting if needed. Returns list of message ids posted."""
    if not content.strip():
        return []
    tok = _token()
    headers = {"Authorization": f"Bot {tok}", "Content-Type": "application/json"}
    url = f"{_BASE}/channels/{channel_id}/messages"
    posted: list[str] = []
    async with httpx.AsyncClient(timeout=30.0) as cli:
        for i, chunk in enumerate(_chunks(content)):
            body: dict = {"content": chunk}
            if i == 0 and reply_to_message_id:
                body["message_reference"] = {
                    "message_id": str(reply_to_message_id),
                    "fail_if_not_exists": False,
                }
            r = await cli.post(url, headers=headers, json=body)
            if r.status_code >= 400:
                raise DiscordPostError(
                    f"post {channel_id} chunk {i} HTTP {r.status_code}: {r.text[:200]}"
                )
            posted.append(str((r.json() or {}).get("id", "")))
    log.info("discord post channel=%s chunks=%d", channel_id, len(posted))
    return posted
