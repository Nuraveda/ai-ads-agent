"""log_turn: fire-and-forget writer of command turns to ads_agent.agent_memory.

Never blocks a Telegram reply — called after the reply is sent, errors swallowed
with a warning. Writes are async via asyncpg; a single shared connection pool
is created on first use.

Week 1 scope: just write. Embeddings + recall come in Week 2.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import asyncpg

from ads_agent.config import settings

log = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None
_pool_lock = asyncio.Lock()


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                # agent_memory is a write path — must use the RW DSN.
                # See issue #3: the legacy POSTGRES_INSIGHTS_RO_URL is a
                # documented read-only role and silently breaks inserts in
                # a correctly-permissioned deployment.
                _pool = await asyncpg.create_pool(
                    settings().postgres_rw_dsn,
                    min_size=1,
                    max_size=4,
                    command_timeout=5.0,
                )
    return _pool


async def log_turn(
    *,
    command: str,
    store_slug: str | None,
    user_tg_id: int | None,
    args: dict[str, Any] | None,
    reply_text: str,
    key_metrics: dict[str, Any] | None = None,
    agent_reasoning: str | None = None,
    kind: str = "insight",
) -> int | None:
    """Insert one row into ads_agent.agent_memory. Returns the new id or None on failure.

    Fire-and-forget embedding is scheduled after the insert so it doesn't
    delay any callers. All errors are logged and swallowed.
    """
    row_id: int | None = None
    try:
        pool = await _get_pool()
        async with pool.acquire() as conn:
            row_id = await conn.fetchval(
                """
                INSERT INTO ads_agent.agent_memory
                    (user_tg_id, store_slug, command, args, reply_text, key_metrics, agent_reasoning, kind)
                VALUES
                    ($1, $2, $3, $4::jsonb, $5, $6::jsonb, $7, $8)
                RETURNING id
                """,
                user_tg_id,
                store_slug,
                command,
                json.dumps(args or {}),
                reply_text,
                json.dumps(key_metrics) if key_metrics is not None else None,
                agent_reasoning,
                kind,
            )
    except Exception:
        log.exception("log_turn failed (command=%s store=%s)", command, store_slug)
        return None

    # Fire-and-forget embedding: compute + UPDATE without awaiting
    if row_id is not None:
        asyncio.ensure_future(
            _embed_and_update(row_id, command=command, store_slug=store_slug,
                              args=args, reply_text=reply_text)
        )
        # Brain mirror (GROW-BIND-1): also report the turn to
        # glitch-brain-mcp so sibling agents on the same brand can see
        # what Ads just did via team_state / recent_activity / briefing.
        # Silently no-ops when BRAIN_TOKEN_BSK_002 isn't configured.
        from ads_agent.memory.brain import schedule_brain_mirror
        schedule_brain_mirror(
            command=command,
            store_slug=store_slug,
            args=args,
            reply_text=reply_text,
            key_metrics=key_metrics,
            agent_reasoning=agent_reasoning,
            kind=kind,
        )
    return row_id


async def _embed_and_update(
    row_id: int,
    *,
    command: str,
    store_slug: str | None,
    args: dict[str, Any] | None,
    reply_text: str,
) -> None:
    """Compute an embedding for the row and UPDATE the embedding column."""
    from ads_agent.memory.embed import composed_for_log, embed_text

    text = composed_for_log(command=command, store_slug=store_slug, args=args, reply_text=reply_text)
    vec = await embed_text(text)
    if vec is None:
        log.warning("embed skipped for row %d (no embedding available)", row_id)
        return
    try:
        # pgvector accepts a string literal "[x,y,z]" for the vector cast
        vec_str = "[" + ",".join(f"{v:.7f}" for v in vec) + "]"
        pool = await _get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                "UPDATE ads_agent.agent_memory SET embedding = $1::vector WHERE id = $2",
                vec_str,
                row_id,
            )
    except Exception:
        log.exception("embed update failed for row %d", row_id)


def fire_and_forget(
    *,
    command: str,
    store_slug: str | None,
    user_tg_id: int | None,
    args: dict[str, Any] | None,
    reply_text: str,
    key_metrics: dict[str, Any] | None = None,
    agent_reasoning: str | None = None,
    kind: str = "insight",
) -> None:
    """Schedule a log_turn call on the running loop without awaiting.

    Use this from Telegram handlers so writes never delay user-visible reply.
    """
    coro = log_turn(
        command=command,
        store_slug=store_slug,
        user_tg_id=user_tg_id,
        args=args,
        reply_text=reply_text,
        key_metrics=key_metrics,
        agent_reasoning=agent_reasoning,
        kind=kind,
    )
    asyncio.ensure_future(coro)
