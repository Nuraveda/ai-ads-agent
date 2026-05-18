"""Meta Ads MCP client (glitch-ads-mcp on :3103, streamable-http).

All Meta write-actions (pause, budget change, creative update) route through
here rather than the direct Graph API, so there's one source of truth for
Meta credentials + rate-limiting in glitch-ads-mcp. Read-only operations
continue using src/ads_agent/meta/graph_client.py for speed (fewer hops).

Mirrors the Amazon MCP client pattern — ClientSession + streamable-http.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from grow_platform.services import META_ADS_MCP

log = logging.getLogger(__name__)

# Single source of truth lives in grow_platform.services; the env override
# `META_ADS_MCP_URL` is still respected there, but the `/mcp` path suffix is
# enforced at module load so legacy .env values get repaired automatically.
DEFAULT_URL = META_ADS_MCP.base_url


class MetaMcpError(RuntimeError):
    pass


def _unwrap(result: Any) -> Any:
    """FastMCP wraps returns as `{"content": [{"type":"text","text":"<json>"}]}`."""
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
    timeout_s: float = 90.0,
) -> Any:
    """Invoke one tool on glitch-ads-mcp. Returns the tool's result dict."""
    target = url or DEFAULT_URL
    async with streamablehttp_client(target) as (read_stream, write_stream, _meta):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            try:
                result = await session.call_tool(tool_name, arguments or {})
            except Exception as e:
                raise MetaMcpError(f"{tool_name}: {e}") from e
    return _unwrap(result)


class MetaAdsMCPClient:
    """Legacy sync wrapper kept for backward compatibility."""

    def __init__(self, base_url: str | None = None, *, timeout: float = 90.0) -> None:
        self.base_url = base_url or DEFAULT_URL
        self._timeout = timeout

    async def call(self, tool: str, arguments: dict) -> dict:
        return await call_tool(tool, arguments, url=self.base_url, timeout_s=self._timeout)
