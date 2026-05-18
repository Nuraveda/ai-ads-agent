"""Embedding helper — Gemini text-embedding-004 (768-dim) with OpenAI fallback.

Both are free-tier or cheap enough for our volume (~hundreds of embeddings/day).
All calls async, all errors swallowed and logged (embeddings are fire-and-forget
decorations; absence never blocks anything).
"""
from __future__ import annotations

import logging
import os

import httpx

from ads_agent.config import settings

log = logging.getLogger(__name__)

GEMINI_EMBED_MODEL = "gemini-embedding-001"  # 768-dim (v1beta accepts outputDimensionality)
OPENAI_EMBED_MODEL = "text-embedding-3-small"  # 1536-dim native — truncated to 768 via dimensions param

EMBED_DIM = 768
MAX_INPUT_CHARS = 8000  # keep well under token limits on both providers


def _truncate(text: str) -> str:
    if not text:
        return ""
    return text[:MAX_INPUT_CHARS]


async def _gemini_embed(text: str) -> list[float] | None:
    key = settings().google_api_key
    if not key:
        return None
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_EMBED_MODEL}:embedContent?key={key}"
    body = {"content": {"parts": [{"text": _truncate(text)}]}, "outputDimensionality": EMBED_DIM}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(url, json=body)
        if r.status_code != 200:
            log.warning("gemini embed %d: %s", r.status_code, r.text[:200])
            return None
        values = r.json().get("embedding", {}).get("values")
        if values and len(values) == EMBED_DIM:
            return values
    except Exception:
        log.exception("gemini embed failed")
    return None


async def _openai_embed(text: str) -> list[float] | None:
    key = settings().openai_api_key
    if not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                json={"model": OPENAI_EMBED_MODEL, "input": _truncate(text), "dimensions": EMBED_DIM},
            )
        if r.status_code != 200:
            log.warning("openai embed %d: %s", r.status_code, r.text[:200])
            return None
        data = r.json().get("data", [])
        if data:
            values = data[0].get("embedding")
            if values and len(values) == EMBED_DIM:
                return values
    except Exception:
        log.exception("openai embed failed")
    return None


async def embed_text(text: str) -> list[float] | None:
    """Return a 768-dim embedding or None if all providers failed."""
    if not text or not text.strip():
        return None
    vec = await _gemini_embed(text)
    if vec is not None:
        return vec
    return await _openai_embed(text)


def composed_for_log(
    *, command: str, store_slug: str | None, args: dict | None, reply_text: str
) -> str:
    """Text we embed for a logged turn. Captures both the intent (command/store/args)
    and the answer (reply_text) so recall can match on either direction."""
    parts = [f"command: {command}"]
    if store_slug:
        parts.append(f"store: {store_slug}")
    if args:
        arg_str = " ".join(f"{k}={v}" for k, v in args.items() if v not in (None, ""))
        if arg_str:
            parts.append(f"args: {arg_str}")
    parts.append("")
    parts.append(reply_text or "")
    return "\n".join(parts)


def composed_for_query(
    *, command: str, store_slug: str | None, args: dict | None
) -> str:
    """Text we embed as a recall query — just intent, no reply yet."""
    parts = [f"command: {command}"]
    if store_slug:
        parts.append(f"store: {store_slug}")
    if args:
        arg_str = " ".join(f"{k}={v}" for k, v in args.items() if v not in (None, ""))
        if arg_str:
            parts.append(f"args: {arg_str}")
    return "\n".join(parts)
