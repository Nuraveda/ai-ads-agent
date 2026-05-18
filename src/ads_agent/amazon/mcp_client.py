"""Thin HTTP client for the amazon-ads-mcp MCP server (port 3105, streamable-http).

All Amazon data access goes through this — the agent does NOT talk to
Supermetrics or Amazon APIs directly. The MCP server owns both the native
Amazon Ads API (pending approval) and the Supermetrics fallback.

Uses the official `mcp` SDK (streamable-http transport). The SDK handles the
session lifecycle (initialize → notifications/initialized → tools/call) which
we'd otherwise have to implement manually.

Slow path — some tools (supermetrics_ads_performance) hit Amazon's async
Reports API and take 2-3 minutes. Caller should run these from background
jobs, not inline in a user-facing handler.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from grow_platform.services import AMAZON_ADS_MCP

log = logging.getLogger(__name__)

# Single source of truth lives in grow_platform.services; env override
# `AMAZON_ADS_MCP_URL` is respected there with /mcp suffix enforced.
DEFAULT_URL = AMAZON_ADS_MCP.base_url


class AmazonMcpError(RuntimeError):
    pass


def _unwrap(result: Any) -> Any:
    """FastMCP wraps returns as `{"content": [{"type":"text","text":"<json>"}]}`.
    Unwrap into the native Python object so callers don't need to know."""
    content = getattr(result, "content", None)
    if content is None and isinstance(result, dict):
        content = result.get("content")
    if isinstance(content, list) and content:
        first = content[0]
        text = getattr(first, "text", None)
        if text is None and isinstance(first, dict):
            text = first.get("text")
        if text is not None:
            try:
                return json.loads(text)
            except Exception:
                return {"text": text}
    # Already structured
    sc = getattr(result, "structuredContent", None)
    if sc:
        return sc
    if isinstance(result, dict):
        return result
    return {}


async def call_tool(
    tool_name: str,
    arguments: dict[str, Any] | None = None,
    *,
    url: str | None = None,
    timeout_s: float = 420.0,
) -> Any:
    """Invoke one tool on amazon-ads-mcp. Returns the tool's result dict."""
    target = url or DEFAULT_URL
    # streamablehttp_client takes the raw URL; session is opened + init automatically
    async with streamablehttp_client(target) as (read_stream, write_stream, _meta):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            try:
                result = await session.call_tool(tool_name, arguments or {})
            except Exception as e:
                raise AmazonMcpError(f"{tool_name}: {e}") from e
    return _unwrap(result)
