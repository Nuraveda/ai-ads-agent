"""LLM routing via LiteLLM.

Tiers:
  cheap   -> Gemini 2.5 Flash         (bulk classification, short summaries)
  smart   -> Gemini 2.5 Pro           (deep reasoning, tracking audits)
  openai  -> gpt-4o-mini              (fallback / comparison)

Falls back gracefully if a provider key is missing.
"""
from __future__ import annotations

import logging
import os

from ads_agent.config import settings

log = logging.getLogger(__name__)


def _ensure_env() -> None:
    """LiteLLM reads keys from env vars. Mirror our Settings values into os.environ."""
    s = settings()
    if s.google_api_key and not os.environ.get("GEMINI_API_KEY"):
        os.environ["GEMINI_API_KEY"] = s.google_api_key
        os.environ["GOOGLE_API_KEY"] = s.google_api_key
    if s.openai_api_key and not os.environ.get("OPENAI_API_KEY"):
        os.environ["OPENAI_API_KEY"] = s.openai_api_key
    if s.anthropic_api_key and not os.environ.get("ANTHROPIC_API_KEY"):
        os.environ["ANTHROPIC_API_KEY"] = s.anthropic_api_key


def pick(tier: str = "cheap") -> str:
    """Return a LiteLLM model string."""
    s = settings()
    if tier == "smart":
        if s.google_api_key:
            return "gemini/gemini-2.5-pro"
        if s.openai_api_key:
            return "openai/gpt-4o"
        return "openai/gpt-4o-mini"
    if tier == "openai":
        return "openai/gpt-4o-mini"
    # cheap default
    if s.google_api_key:
        return "gemini/gemini-2.5-flash"
    return "openai/gpt-4o-mini"


def _fallback_chain(tier: str) -> list[str]:
    """Ordered list of models to try for a tier, from preferred to last-resort."""
    s = settings()
    chain: list[str] = []
    if tier == "smart":
        if s.google_api_key:
            chain.extend(["gemini/gemini-2.5-pro", "gemini/gemini-2.5-flash"])
        if s.openai_api_key:
            chain.append("openai/gpt-4o")
        if s.anthropic_api_key:
            chain.append("claude-sonnet-4-5")
    else:
        if s.google_api_key:
            chain.append("gemini/gemini-2.5-flash")
        if s.openai_api_key:
            chain.append("openai/gpt-4o-mini")
    return chain or ["openai/gpt-4o-mini"]


async def complete(prompt: str, *, tier: str = "cheap", system: str | None = None, max_tokens: int = 800) -> str:
    """One-shot text completion with automatic fallback across providers.

    Detects truncated Gemini responses (finish_reason=length with short output —
    usually caused by Gemini 2.5's thinking-token consumption) and falls through
    to the next provider instead of returning a partial answer.
    """
    _ensure_env()
    import litellm

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    last_err: Exception | None = None
    last_partial: str = ""
    for model in _fallback_chain(tier):
        try:
            resp = await litellm.acompletion(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.3,
            )
            choice = resp.choices[0]
            text = choice.message.content or ""
            finish = getattr(choice, "finish_reason", "unknown")
            log.info("LLM %s finish_reason=%s len=%d", model, finish, len(text))

            if not text.strip():
                continue
            # If the model stopped naturally (stop/end_turn), accept.
            # If finish is length/MAX_TOKENS AND output is short, try next provider —
            # a Gemini "thinking" burn-off leaves only a stump.
            if finish in ("stop", "end_turn", "STOP"):
                return text
            if len(text) >= 300:
                return text
            log.warning("LLM %s returned short reply (%d chars) finish=%s; trying next", model, len(text), finish)
            last_partial = text
        except Exception as e:
            last_err = e
            log.warning("LLM %s failed: %s", model, str(e)[:200])
            continue

    if last_partial:
        return last_partial
    log.error("all LLM providers failed for tier=%s", tier)
    return f"(LLM error across all providers: {last_err})"


async def complete_vision(
    prompt: str,
    image_urls: list[str],
    *,
    tier: str = "smart",
    system: str | None = None,
    max_tokens: int = 1500,
) -> str:
    """Multimodal completion — pass 1+ image URLs alongside a text prompt.

    Image fetching is done by the provider, not us. If the URL is a Meta-CDN
    signed URL with an expiry, make sure we pass it while it's still fresh.
    """
    _ensure_env()
    import litellm

    content: list[dict] = [{"type": "text", "text": prompt}]
    for url in image_urls:
        content.append({"type": "image_url", "image_url": {"url": url}})

    messages: list[dict] = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": content})

    last_err: Exception | None = None
    for model in _fallback_chain(tier):
        try:
            resp = await litellm.acompletion(
                model=model,
                messages=messages,
                max_tokens=max_tokens,
                temperature=0.4,
            )
            choice = resp.choices[0]
            text = choice.message.content or ""
            finish = getattr(choice, "finish_reason", "unknown")
            log.info("Vision LLM %s finish_reason=%s len=%d", model, finish, len(text))
            # If model stopped for a non-natural reason (safety, length at low tokens),
            # fall through to the next provider rather than returning a truncated reply.
            if text.strip() and finish not in ("content_filter", "safety"):
                # Accept partial if it's at least substantive; else try next model
                if len(text) >= 400 or finish in ("stop", "end_turn", "STOP", "MAX_TOKENS"):
                    return text
                log.warning("Vision LLM %s returned short reply (%d chars) finish=%s; trying next", model, len(text), finish)
        except Exception as e:
            last_err = e
            log.warning("Vision LLM %s failed: %s", model, str(e)[:200])
            continue

    log.error("all vision LLM providers failed for tier=%s", tier)
    return f"(Vision LLM error: {last_err})"
