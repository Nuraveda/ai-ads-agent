"""TikTok Marketing API OAuth flow.

Five-step flow coordinated across our FastAPI server and the Cloudflare
Pages Function at grow.example.com/tiktok/oauth/callback:

  1. Operator hits: GET /api/tiktok/consent-url?account_ref=<store-or-handle>
  2. Agent generates a random `state`, persists it in
     ads_agent.tiktok_oauth_state with a 10-minute TTL, then returns the
     TikTok authorization URL.
  3. Operator logs in to TikTok Business and approves access for the app.
  4. TikTok redirects to grow.example.com/tiktok/oauth/callback
     with `?auth_code=X&state=Y` (or `?code=X&state=Y` depending on shape).
     The CF Pages Function forwards that payload to
     /api/tiktok/oauth/receive with a shared-secret Bearer header.
  5. Agent validates state, exchanges auth_code for access_token and
     refresh_token, fetches the advertisers attached to that grant, and
     stores the token row in ads_agent.tiktok_oauth_tokens.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

import asyncpg
import httpx

from ads_agent.config import settings

log = logging.getLogger(__name__)

TIKTOK_CONSENT_URL = "https://ads.tiktok.com/marketing_api/auth"
TIKTOK_TOKEN_URL = "https://business-api.tiktok.com/open_api/v1.3/oauth2/access_token/"
TIKTOK_ADVERTISER_URL = "https://business-api.tiktok.com/open_api/v1.3/oauth2/advertiser/get/"
DEFAULT_RETURN_URL = "https://grow.example.com/tiktok/oauth/callback"


class OAuthError(RuntimeError):
    pass


def _env(key: str) -> str:
    value = os.environ.get(key, "").strip()
    if not value:
        raise OAuthError(f"env {key} is not set")
    return value


def _to_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_response(resp: httpx.Response, context: str) -> dict[str, Any]:
    try:
        body = resp.json()
    except json.JSONDecodeError as exc:
        raise OAuthError(f"{context}: non-JSON response {resp.status_code}") from exc
    if resp.status_code != 200:
        raise OAuthError(f"{context}: {resp.status_code} {body}")
    if not isinstance(body, dict):
        raise OAuthError(f"{context}: unexpected response type {type(body).__name__}")
    code = body.get("code")
    if code not in (0, None):
        raise OAuthError(
            f"{context}: TikTok API error {code}: {body.get('message') or 'unknown error'}"
        )
    return body


async def generate_consent_url(
    pool: asyncpg.Pool,
    *,
    account_ref: str,
    return_url: str = DEFAULT_RETURN_URL,
    notes: str | None = None,
) -> str:
    app_id = _env("TIKTOK_APP_ID")
    state = secrets.token_urlsafe(32)

    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO ads_agent.tiktok_oauth_state (state, account_ref, notes)
               VALUES ($1, $2, $3)""",
            state,
            account_ref,
            notes,
        )

    params = {
        "app_id": app_id,
        "redirect_uri": return_url,
        "state": state,
        "response_type": "code",
    }
    return f"{TIKTOK_CONSENT_URL}?{urlencode(params)}"


async def consume_state(pool: asyncpg.Pool, state: str) -> dict[str, Any] | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE ads_agent.tiktok_oauth_state
               SET used_at = NOW()
               WHERE state = $1
                 AND used_at IS NULL
                 AND expires_at > NOW()
               RETURNING account_ref, notes""",
            state,
        )
    return dict(row) if row else None


async def exchange_code_for_tokens(code: str) -> dict[str, Any]:
    payload = {
        "app_id": _env("TIKTOK_APP_ID"),
        "secret": _env("TIKTOK_APP_SECRET"),
        "auth_code": code,
    }
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        resp = await client.post(TIKTOK_TOKEN_URL, json=payload)
    body = _parse_response(resp, "token exchange")
    data = body.get("data")
    if not isinstance(data, dict) or not str(data.get("access_token") or "").strip():
        raise OAuthError(f"token exchange: access_token missing in {body}")
    return data


async def fetch_authorized_advertisers(access_token: str) -> list[dict[str, Any]]:
    if not access_token:
        return []
    params = {
        "app_id": _env("TIKTOK_APP_ID"),
        "secret": _env("TIKTOK_APP_SECRET"),
    }
    headers = {"Access-Token": access_token}
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        resp = await client.get(TIKTOK_ADVERTISER_URL, params=params, headers=headers)
    body = _parse_response(resp, "advertiser lookup")
    data = body.get("data")
    rows = data.get("list") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    return [row for row in rows if isinstance(row, dict)]


async def store_tokens(
    pool: asyncpg.Pool,
    *,
    account_ref: str,
    access_token: str,
    refresh_token: str | None,
    expires_in: int | None,
    advertiser_ids: list[str],
    advertisers: list[dict[str, Any]],
) -> int:
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        if expires_in and expires_in > 0
        else None
    )

    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE ads_agent.tiktok_oauth_tokens
               SET revoked_at = NOW(), revoke_reason = 'superseded by new authorization'
               WHERE account_ref = $1 AND revoked_at IS NULL""",
            account_ref,
        )
        token_id = await conn.fetchval(
            """INSERT INTO ads_agent.tiktok_oauth_tokens
               (account_ref, access_token, refresh_token,
                access_token_expires_at, advertiser_ids, advertisers)
               VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb)
               RETURNING id""",
            account_ref,
            access_token,
            refresh_token,
            expires_at,
            json.dumps(advertiser_ids),
            json.dumps(advertisers),
        )
    log.info(
        "stored new TikTok OAuth token for %s (id=%s, advertisers=%s)",
        account_ref,
        token_id,
        advertiser_ids,
    )
    return int(token_id)


async def get_live_token(pool: asyncpg.Pool, account_ref: str) -> dict[str, Any] | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT * FROM ads_agent.tiktok_oauth_tokens
               WHERE account_ref = $1 AND revoked_at IS NULL
               ORDER BY created_at DESC
               LIMIT 1""",
            account_ref,
        )
    return dict(row) if row else None


async def resolve_access_token(account_ref: str) -> str | None:
    dsn = settings().postgres_rw_dsn.strip()
    if not dsn or "changeme" in dsn or "your_db_name" in dsn:
        return None
    try:
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1)
    except Exception as exc:
        log.warning("TikTok OAuth pool init failed for %s: %s", account_ref, exc)
        return None
    try:
        row = await get_live_token(pool, account_ref)
    except Exception as exc:
        log.warning("TikTok OAuth token lookup failed for %s: %s", account_ref, exc)
        return None
    finally:
        await pool.close()
    token = str((row or {}).get("access_token") or "").strip()
    return token or None


async def receive_callback(
    pool: asyncpg.Pool,
    *,
    code: str,
    state: str,
) -> dict[str, Any]:
    if not code or not state:
        raise OAuthError("missing code or state in callback")

    state_row = await consume_state(pool, state)
    if not state_row:
        raise OAuthError("state invalid, expired, or already used")

    account_ref = state_row.get("account_ref") or "unknown"
    token_data = await exchange_code_for_tokens(code)
    access_token = str(token_data.get("access_token") or "").strip()
    refresh_token = str(token_data.get("refresh_token") or "").strip() or None
    expires_in = _to_int(token_data.get("expires_in"))

    advertisers = await fetch_authorized_advertisers(access_token)
    advertiser_ids_raw = token_data.get("advertiser_ids")
    if isinstance(advertiser_ids_raw, list) and advertiser_ids_raw:
        advertiser_ids = [str(item).strip() for item in advertiser_ids_raw if str(item).strip()]
    else:
        advertiser_ids = [
            str(row.get("advertiser_id") or "").strip()
            for row in advertisers
            if str(row.get("advertiser_id") or "").strip()
        ]

    token_id = await store_tokens(
        pool,
        account_ref=account_ref,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        advertiser_ids=advertiser_ids,
        advertisers=advertisers,
    )

    return {
        "ok": True,
        "account_ref": account_ref,
        "token_id": token_id,
        "advertiser_ids": advertiser_ids,
        "advertiser_count": len(advertiser_ids),
        "expires_in": expires_in,
    }
