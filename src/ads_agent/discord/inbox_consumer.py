"""Watch /home/support/.glitch-discord/inbox/<channel>/ and dispatch each
message through the agent, posting replies back to Discord.

Design:
  - Polling-based (1s sleep). Simpler than inotify for the current low
    message volume; switches to inotify are a one-line swap.
  - Each *.json file is parsed; if `.content` starts with `/`, it's routed
    through `dispatcher.parse_and_run`. Reply is posted to `channel_id`,
    referencing the source message id. The file is then moved to
    `<channel>/processed/<id>.json` so re-runs don't double-process.
  - Failures move the file to `<channel>/errors/<id>.json` with an
    appended `{"_error": "..."}` field so an operator can retriage.

Run as:
  python -m ads_agent.discord.inbox_consumer
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
import logging
import os
import signal
import time
from pathlib import Path

from dotenv import load_dotenv

# Load agent .env first (for LLM keys etc.), then Discord env
load_dotenv("/home/support/ai_marketing_stack-ads-agent/.env")
load_dotenv("/home/support/.config/glitch-discord/env")

from ads_agent.discord.dispatcher import parse_and_run
from ads_agent.discord.poster import DiscordPostError, post_message

INBOX_ROOT = Path(os.environ.get("DISCORD_INBOX_ROOT", "/home/support/.glitch-discord/inbox"))
# Channels the consumer will serve. Extend via env for additional agents.
CHANNELS = tuple(
    c.strip() for c in os.environ.get("DISCORD_CONSUMER_CHANNELS", "grow-ads").split(",")
    if c.strip()
)
POLL_SECS = float(os.environ.get("DISCORD_CONSUMER_POLL_SECS", "1.0"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("discord_consumer")

_stop = asyncio.Event()


def _install_signal_handlers() -> None:
    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop.set)
        except NotImplementedError:
            pass  # Windows


async def _handle_one(channel: str, path: Path) -> None:
    try:
        msg = json.loads(path.read_text())
    except Exception as e:  # noqa: BLE001
        log.warning("unreadable %s: %s", path, e)
        _archive(path, channel, kind="errors", extra={"_error": f"unreadable: {e}"})
        return

    content   = (msg.get("content") or "").strip()
    author    = (msg.get("author") or {}).get("name", "?")
    channel_id = str(msg.get("channel_id") or "")
    msg_id    = str(msg.get("id") or "")

    if not channel_id:
        log.warning("missing channel_id in %s", path)
        _archive(path, channel, kind="errors", extra={"_error": "missing channel_id"})
        return

    # Ignore the bot's own messages if the upstream bot echoes them
    if author == "glitch-bot":
        _archive(path, channel, kind="processed")
        return

    if not content.startswith("/"):
        log.info("skip non-command from %s: %s", author, content[:60])
        _archive(path, channel, kind="processed")
        return

    log.info("dispatch channel=%s author=%s cmd=%s", channel, author, content[:80])
    try:
        reply = await parse_and_run(content)
    except Exception as e:  # noqa: BLE001
        log.exception("dispatch error")
        reply = f"❌ internal error: {e}"

    if reply:
        try:
            await post_message(channel_id, reply, reply_to_message_id=msg_id)
        except DiscordPostError as e:
            log.error("post failed: %s", e)
            _archive(path, channel, kind="errors", extra={"_error": f"post failed: {e}"})
            return

    _archive(path, channel, kind="processed")


def _archive(path: Path, channel: str, *, kind: str, extra: dict | None = None) -> None:
    dest_dir = INBOX_ROOT / channel / kind
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / path.name
    try:
        if extra:
            try:
                data = json.loads(path.read_text())
            except Exception:
                data = {"_raw": path.read_text(errors="replace")}
            data.update(extra)
            dest.write_text(json.dumps(data, indent=2))
            path.unlink(missing_ok=True)
        else:
            path.rename(dest)
    except Exception as e:  # noqa: BLE001
        log.error("archive failed %s → %s: %s", path, dest, e)


async def _watch_channel(channel: str) -> None:
    inbox = INBOX_ROOT / channel
    inbox.mkdir(parents=True, exist_ok=True)
    log.info("watching %s", inbox)
    while not _stop.is_set():
        try:
            entries = sorted(inbox.glob("*.json"))
        except Exception as e:  # noqa: BLE001
            log.error("scan %s failed: %s", inbox, e)
            entries = []
        for path in entries:
            if _stop.is_set():
                break
            await _handle_one(channel, path)
        try:
            await asyncio.wait_for(_stop.wait(), timeout=POLL_SECS)
        except asyncio.TimeoutError:
            pass


async def _handle_interaction(path: Path) -> None:
    """Process a button-click interaction JSON written by the sibling bot.

    Routes act:approve:N / act:reject:N to the platform-agnostic resolver
    (actions.resolver.resolve_action) using the same atomic UPDATE the
    Telegram callback path uses. After resolution, edits the original
    Discord message to strip buttons + show the verdict line. Honours
    the approver allowlist via DISCORD_APPROVER_USER_IDS_JSON env.
    """
    try:
        data = json.loads(path.read_text())
    except Exception as e:  # noqa: BLE001
        log.warning("unreadable interaction %s: %s", path, e)
        path.rename(path.parent / "errors" / path.name) if (path.parent / "errors").exists() else path.unlink(missing_ok=True)
        return

    custom_id  = data.get("custom_id", "")
    user_id    = data.get("user_id", "")
    user_name  = data.get("user_display") or data.get("user_name") or user_id
    channel_id = data.get("channel_id", "")
    message_id = data.get("message_id", "")

    parts = custom_id.split(":")
    if len(parts) == 3 and parts[0] == "tiktok" and parts[1] == "enable":
        await _handle_tiktok_enable_interaction(path, data, parts[2])
        return
    if len(parts) != 3 or parts[0] != "act":
        log.warning("malformed interaction custom_id: %r", custom_id)
        _archive_interaction(path, "processed")
        return
    _, verb, action_id_str = parts

    # Approver allowlist check
    allowed = _approver_allowlist()
    if allowed and str(user_id) not in allowed:
        log.warning(
            "interaction rejected: user=%s (%s) not in DISCORD_APPROVER_USER_IDS_JSON",
            user_id, user_name,
        )
        _archive_interaction(path, "rejected_unauthorised")
        return

    try:
        action_id = int(action_id_str)
    except ValueError:
        _archive_interaction(path, "errors")
        return

    import asyncpg
    from ads_agent.config import settings

    pool = await asyncpg.create_pool(settings().postgres_rw_dsn, min_size=1, max_size=2)
    try:
        action_kind = await _action_kind(pool, action_id)
        if action_kind == "tiktok_port":
            await _handle_tiktok_port_action(
                pool=pool,
                action_id=action_id,
                verb=verb,
                approver_id=f"discord:{user_id}",
                approver_name=str(user_name),
            )
            _archive_interaction(path, "processed")
            return
    finally:
        await pool.close()

    from ads_agent.actions.resolver import (
        edit_resolution_on_all_platforms, resolve_action,
    )

    pool = await asyncpg.create_pool(settings().postgres_rw_dsn, min_size=1, max_size=2)
    try:
        outcome = await resolve_action(
            pool, action_id=action_id, verb=verb,
            approver_id=f"discord:{user_id}",
            approver_name=str(user_name),
        )
        if outcome.success and outcome.row and outcome.verdict_line:
            # Caller may have a stale `original_text`; we just use empty so
            # the editor produces "<verdict line>" alone — this isn't ideal
            # for a posh receipt but keeps the channel readable. The full
            # proposal stays in the bot's prior message in scrollback.
            await edit_resolution_on_all_platforms(
                row=outcome.row,
                original_text="",  # we don't have the original text here
                verdict_line=outcome.verdict_line,
            )
        log.info(
            "interaction resolved action=%d verb=%s by %s → %s",
            action_id, verb, user_name, outcome.verdict,
        )
    finally:
        await pool.close()
    _archive_interaction(path, "processed")


async def _action_kind(pool, action_id: int) -> str:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT action_kind FROM ads_agent.agent_actions WHERE id=$1",
            action_id,
        )
    return str(row["action_kind"]) if row else ""


def _as_dict(value) -> dict:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _port_failure_step(error: str) -> str:
    text = error.lower()
    if "meta export" in text:
        return "meta_export"
    if "pixel event" in text:
        return "pixel_event_pick"
    if "upload" in text:
        return "asset_upload"
    if "creative create" in text:
        return "tiktok_create"
    return "workflow"


async def _handle_tiktok_port_action(
    *,
    pool,
    action_id: int,
    verb: str,
    approver_id: str,
    approver_name: str,
) -> None:
    from ads_agent.actions.discord_notifier import (
        edit_resolution,
        post_tiktok_done_to_discord,
    )
    from ads_agent.agent.workflows.port_meta_to_tiktok import port_meta_ad
    from ads_agent.discord.dispatcher import (
        build_tiktok_done_card,
        build_tiktok_failure_card,
    )
    from ads_agent.discord.poster import post_message

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM ads_agent.agent_actions WHERE id=$1",
            action_id,
        )
        if not row:
            log.warning("tiktok_port action %s not found", action_id)
            return
        row = dict(row)
        if row.get("status") != "pending_approval":
            log.info("tiktok_port action %s already %s", action_id, row.get("status"))
            return

        if verb == "reject":
            await conn.execute(
                """UPDATE ads_agent.agent_actions
                   SET status='rejected',
                       rejected_by_text=$1,
                       rejected_at=NOW()
                   WHERE id=$2 AND status='pending_approval'""",
                approver_id,
                action_id,
            )
            if row.get("discord_channel_id") and row.get("discord_message_id"):
                await edit_resolution(
                    int(row["discord_channel_id"]),
                    int(row["discord_message_id"]),
                    f"Rejected by `{approver_name}`. No TikTok ad was created.",
                )
            return

        if verb != "approve":
            log.warning("bad tiktok_port verb: %s", verb)
            return

        claimed = await conn.fetchrow(
            """UPDATE ads_agent.agent_actions
               SET status='executing',
                   approved_by_text=$1,
                   approved_at=NOW()
               WHERE id=$2 AND status='pending_approval'
               RETURNING *""",
            approver_id,
            action_id,
        )
        if not claimed:
            return
        row = dict(claimed)

    params = _as_dict(row.get("params"))
    manifest = None
    try:
        manifest = await port_meta_ad(
            meta_ad_id=str(params["meta_ad_id"]),
            tiktok_slug=str(params["tiktok_slug"]),
            landing_url=str(params["landing_url"]),
            ad_text=str(params["ad_text"]),
            display_name=str(params["display_name"]),
            daily_budget=float(params.get("daily_budget") or 50),
            bid_price=float(params.get("bid_price") or 10),
            call_to_action=str(params.get("call_to_action") or "LEARN_MORE"),
            tiktok_context_overrides=params,
        )
    except Exception as e:  # noqa: BLE001
        error = str(e)
        log.exception("tiktok_port action %s failed", action_id)
        async with pool.acquire() as conn:
            await conn.execute(
                """UPDATE ads_agent.agent_actions
                   SET status='failed',
                       executed_at=NOW(),
                       result=$1::jsonb
                   WHERE id=$2""",
                json.dumps({"error": error[:1000], "step": _port_failure_step(error)}),
                action_id,
            )
        if row.get("discord_channel_id") and row.get("discord_message_id"):
            await edit_resolution(
                int(row["discord_channel_id"]),
                int(row["discord_message_id"]),
                f"Approved by `{approver_name}`, but the TikTok port failed.",
            )
            await post_message(
                str(row["discord_channel_id"]),
                build_tiktok_failure_card(
                    step=_port_failure_step(error),
                    error=error,
                    manifest=None,
                ),
            )
        return

    result = asdict(manifest)
    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE ads_agent.agent_actions
               SET status='executed',
                   executed_at=NOW(),
                   result=$1::jsonb
               WHERE id=$2""",
            json.dumps(result),
            action_id,
        )

    if row.get("discord_channel_id") and row.get("discord_message_id"):
        await edit_resolution(
            int(row["discord_channel_id"]),
            int(row["discord_message_id"]),
            f"Approved by `{approver_name}`. TikTok port completed; created DISABLED.",
        )
        await post_tiktok_done_to_discord(
            int(row["discord_channel_id"]),
            build_tiktok_done_card(manifest),
            manifest_id=manifest.manifest_id,
            advertiser_id=manifest.advertiser_id,
        )


async def _handle_tiktok_enable_interaction(path: Path, data: dict, manifest_id: str) -> None:
    allowed = _approver_allowlist()
    user_id = str(data.get("user_id") or "")
    user_name = data.get("user_display") or data.get("user_name") or user_id
    if allowed and user_id not in allowed:
        log.warning("tiktok enable rejected: user=%s not allowlisted", user_name)
        _archive_interaction(path, "rejected_unauthorised")
        return

    from ads_agent.actions.discord_notifier import edit_resolution
    from ads_agent.agent.workflows.port_meta_to_tiktok import enable_launch
    from ads_agent.discord.poster import post_message

    channel_id = str(data.get("channel_id") or "")
    message_id = str(data.get("message_id") or "")
    try:
        manifest = await enable_launch(manifest_id)
    except Exception as e:  # noqa: BLE001
        log.exception("tiktok enable failed for %s", manifest_id)
        if channel_id:
            await post_message(channel_id, f"Enable failed for `{manifest_id}`: {str(e)[:500]}")
        _archive_interaction(path, "errors")
        return

    if channel_id and message_id:
        await edit_resolution(
            int(channel_id),
            int(message_id),
            f"Live on TikTok. Ad `{manifest.ad_id}` enabled by `{user_name}`.",
        )
    _archive_interaction(path, "processed")


def _archive_interaction(path: Path, kind: str) -> None:
    dest = INBOX_ROOT / "_interactions" / kind
    dest.mkdir(parents=True, exist_ok=True)
    try:
        path.rename(dest / path.name)
    except Exception:  # noqa: BLE001
        pass


def _approver_allowlist() -> set[str] | None:
    """Returns the set of allowed Discord user_ids, or None to allow all.

    None is dangerous in production — log loudly. Configure via
    DISCORD_APPROVER_USER_IDS_JSON='["123","456"]'.
    """
    raw = os.environ.get("DISCORD_APPROVER_USER_IDS_JSON", "").strip()
    if not raw:
        log.warning(
            "DISCORD_APPROVER_USER_IDS_JSON unset — ALL Discord users can "
            "approve actions. Set this in /home/support/.config/glitch-discord/env."
        )
        return None
    try:
        ids = json.loads(raw)
    except json.JSONDecodeError:
        log.error("DISCORD_APPROVER_USER_IDS_JSON malformed — refusing all clicks")
        return set()
    return {str(x) for x in ids}


async def _watch_interactions() -> None:
    """Watch the _interactions/ subdir of the inbox for button-click JSONs."""
    inbox = INBOX_ROOT / "_interactions"
    inbox.mkdir(parents=True, exist_ok=True)
    log.info("watching interactions: %s", inbox)
    while not _stop.is_set():
        try:
            entries = sorted(inbox.glob("*.json"))
        except Exception as e:  # noqa: BLE001
            log.error("scan %s failed: %s", inbox, e)
            entries = []
        for path in entries:
            if _stop.is_set():
                break
            try:
                await _handle_interaction(path)
            except Exception:  # noqa: BLE001
                log.exception("interaction handler crashed; archiving")
                _archive_interaction(path, "errors")
        try:
            await asyncio.wait_for(_stop.wait(), timeout=POLL_SECS)
        except asyncio.TimeoutError:
            pass


async def main() -> None:
    _install_signal_handlers()
    log.info("discord inbox consumer up · channels=%s root=%s", CHANNELS, INBOX_ROOT)
    await asyncio.gather(
        *[_watch_channel(c) for c in CHANNELS],
        _watch_interactions(),
    )
    log.info("shutdown clean")


if __name__ == "__main__":
    asyncio.run(main())
