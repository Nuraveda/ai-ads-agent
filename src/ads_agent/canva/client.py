"""Canva Connect API client.

Creative-asset producer for the agent. Common flow for an ad/social post:

  1. Designer creates a "brand template" once in Canva (e.g. an
     Instagram-square promo with placeholders for headline + product image).
  2. Agent calls autofill_brand_template() with per-post copy + image asset.
  3. Agent calls export_design() to get a PNG/MP4/PDF URL.
  4. That URL is fed into the existing publishing pipeline.

Auth handling lives in ads_agent.canva.oauth — this module only knows
about the Bearer access_token returned by resolve_access_token().

Docs: https://www.canva.dev/docs/connect/api-reference/
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
from typing import Any

import httpx

from ads_agent.canva.oauth import resolve_access_token
from ads_agent.config import settings

log = logging.getLogger(__name__)


class CanvaApiError(RuntimeError):
    def __init__(self, status: int, body: Any):
        self.status = status
        self.body = body
        super().__init__(f"Canva API {status}: {body!r}")


class NotConnected(RuntimeError):
    """Raised when no live Canva access token exists for this account_ref."""


async def _bearer(account_ref: str) -> str:
    token = await resolve_access_token(account_ref)
    if not token:
        raise NotConnected(
            f"No live Canva token for account_ref={account_ref!r}. "
            f"Run /api/canva/consent-url and complete authorization."
        )
    return token


async def _request(
    account_ref: str,
    method: str,
    path: str,
    *,
    json_body: dict | None = None,
    params: dict | None = None,
    timeout: float = 30.0,
) -> dict:
    s = settings()
    token = await _bearer(account_ref)
    url = f"{s.canva_api_base}{path}"
    headers = {"Authorization": f"Bearer {token}"}
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.request(
            method, url, headers=headers, json=json_body, params=params,
        )
    body: Any
    try:
        body = resp.json()
    except Exception:
        body = {"non_json_body": resp.text[:500]}
    if resp.status_code >= 400:
        log.error("canva api %s %s -> %s %s", method, path, resp.status_code, body)
        raise CanvaApiError(resp.status_code, body)
    return body if isinstance(body, dict) else {"value": body}


# ---------------------------------------------------------------------------
# Profile + brand templates
# ---------------------------------------------------------------------------

async def get_user(account_ref: str) -> dict:
    return await _request(account_ref, "GET", "/users/me")


async def list_brand_templates(account_ref: str, *, limit: int = 50) -> list[dict]:
    out: list[dict] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": min(100, max(1, limit - len(out)))}
        if cursor:
            params["continuation"] = cursor
        page = await _request(account_ref, "GET", "/brand-templates", params=params)
        out.extend(page.get("items") or [])
        cursor = page.get("continuation")
        if not cursor or len(out) >= limit:
            break
    return out[:limit]


async def get_brand_template_dataset(account_ref: str, template_id: str) -> dict:
    return await _request(
        account_ref, "GET", f"/brand-templates/{template_id}/dataset",
    )


# ---------------------------------------------------------------------------
# Asset upload
# ---------------------------------------------------------------------------

async def upload_asset(
    account_ref: str, *, file_bytes: bytes, name: str,
) -> dict:
    """Upload binary asset, poll the job to completion, return the asset object.
    The asset's `id` is what to pass into autofill image fields."""
    s = settings()
    token = await _bearer(account_ref)

    # Canva wants the header itself as a JSON string (not base64); the
    # `name_base64` value inside it is base64 of the asset name.
    metadata = {"name_base64": base64.b64encode(name.encode()).decode()}
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/octet-stream",
        "Asset-Upload-Metadata": json.dumps(metadata),
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{s.canva_api_base}/asset-uploads",
            headers=headers, content=file_bytes,
        )
    try:
        body = resp.json()
    except Exception:
        body = {"non_json_body": resp.text[:500]}
    if resp.status_code >= 400:
        log.error("canva asset upload create -> %s %s", resp.status_code, body)
        raise CanvaApiError(resp.status_code, body)

    job = (body or {}).get("job") or {}
    job_id = job.get("id")
    if not job_id:
        raise CanvaApiError(500, {"error": "asset_upload missing job id", "body": body})

    return await _poll_job(
        account_ref,
        path=f"/asset-uploads/{job_id}",
        result_key="asset",
        kind="asset_upload",
    )


# ---------------------------------------------------------------------------
# Autofill
# ---------------------------------------------------------------------------

async def autofill_brand_template(
    account_ref: str,
    *,
    template_id: str,
    fields: dict[str, dict],
    title: str | None = None,
) -> dict:
    """Render a finished design from a brand template + field values.

    `fields` shape — one entry per template field, keyed by field name.
    Use get_brand_template_dataset() to discover what's valid.

      {
        "headline":   {"type": "text",  "text": "Ship faster"},
        "subhead":    {"type": "text",  "text": "with our SDK"},
        "hero_image": {"type": "image", "asset_id": "<from upload_asset>"},
      }

    Returns the completed design (poll-driven; blocks until done).
    """
    body: dict[str, Any] = {
        "brand_template_id": template_id,
        "data": fields,
    }
    if title:
        body["title"] = title

    job_resp = await _request(account_ref, "POST", "/autofills", json_body=body)
    job = job_resp.get("job") or {}
    job_id = job.get("id")
    if not job_id:
        raise CanvaApiError(500, {"error": "autofill missing job id", "body": job_resp})

    return await _poll_job(
        account_ref,
        path=f"/autofills/{job_id}",
        result_key="result",
        kind="autofill",
    )


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

async def export_design(
    account_ref: str,
    *,
    design_id: str,
    fmt: str = "png",
    pages: list[int] | None = None,
) -> list[str]:
    """Export a design to one or more downloadable URLs.

    fmt: "png" | "jpg" | "pdf" | "mp4" | "gif"
    Returns a list of HTTPS URLs (one per page for image formats).
    URLs are short-lived — download promptly.
    """
    fmt_block: dict[str, Any] = {"type": fmt}
    if pages:
        fmt_block["pages"] = pages

    job_resp = await _request(
        account_ref, "POST", "/exports",
        json_body={"design_id": design_id, "format": fmt_block},
    )
    job = job_resp.get("job") or {}
    job_id = job.get("id")
    if not job_id:
        raise CanvaApiError(500, {"error": "export missing job id", "body": job_resp})

    final = await _poll_job(
        account_ref,
        path=f"/exports/{job_id}",
        result_key="urls",
        kind="export",
    )
    if isinstance(final, list):
        return final
    if isinstance(final, dict) and isinstance(final.get("urls"), list):
        return list(final["urls"])
    raise CanvaApiError(500, {"error": "unexpected export result shape", "body": final})


# ---------------------------------------------------------------------------
# Designs (read-only)
# ---------------------------------------------------------------------------

async def list_designs(account_ref: str, *, limit: int = 50) -> list[dict]:
    out: list[dict] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"limit": min(100, max(1, limit - len(out)))}
        if cursor:
            params["continuation"] = cursor
        page = await _request(account_ref, "GET", "/designs", params=params)
        out.extend(page.get("items") or [])
        cursor = page.get("continuation")
        if not cursor or len(out) >= limit:
            break
    return out[:limit]


async def get_design(account_ref: str, design_id: str) -> dict:
    return await _request(account_ref, "GET", f"/designs/{design_id}")


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

async def _poll_job(
    account_ref: str,
    *,
    path: str,
    result_key: str,
    kind: str,
    interval_s: float = 1.5,
    timeout_s: float = 180.0,
) -> Any:
    waited = 0.0
    while waited < timeout_s:
        payload = await _request(account_ref, "GET", path)
        job = payload.get("job") or payload
        status = (job.get("status") or "").lower()
        if status in {"success", "succeeded", "completed", "ready"}:
            return job.get(result_key) or job
        if status in {"failed", "error"}:
            log.error("canva %s job failed: %s", kind, job)
            raise CanvaApiError(500, {"error": f"{kind} job failed", "body": job})
        await asyncio.sleep(interval_s)
        waited += interval_s
    raise CanvaApiError(504, {"error": f"{kind} job timed out", "path": path})
