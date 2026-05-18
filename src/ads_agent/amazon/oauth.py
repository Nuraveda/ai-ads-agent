"""Amazon Ads LWA (Login with Amazon) OAuth flow.

Five-step flow coordinated across our FastAPI server and the Cloudflare
Pages Function at grow.example.com/amazon/oauth/callback:

  1. Operator hits our agent: GET /api/amazon/consent-url?account_ref=<client>
  2. Agent generates a random `state`, persists in ads_agent.amazon_oauth_state
     with 10-min TTL, returns a clickable amazon.com/ap/oa URL.
  3. Operator opens URL → Amazon shows "Allow this app to access your Ads
     account" → clicks Allow.
  4. Amazon redirects to grow.example.com/amazon/oauth/callback?code=X&state=Y.
     The CF Pages Function forwards {code, state} to agent's POST
     /api/amazon/oauth/receive with a shared-secret Bearer header.
  5. Agent validates state, exchanges `code` for refresh_token via
     POST api.amazon.com/auth/o2/token, stores refresh_token in
     ads_agent.amazon_oauth_tokens.

All subsequent Amazon Ads API calls mint a short-lived access_token from the
refresh_token on demand and include `Authorization: Bearer <access>` +
`Amazon-Advertising-API-ClientId: <lwa_client_id>`.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

import asyncpg
import httpx

log = logging.getLogger(__name__)

LWA_TOKEN_URL  = "https://api.amazon.com/auth/o2/token"
LWA_CONSENT_URL = "https://www.amazon.com/ap/oa"

DEFAULT_SCOPE = "advertising::campaign_management"

# Where Amazon redirects the user after they click Allow.
# This must match ONE of the "Allowed Return URLs" on the LWA Security Profile.
DEFAULT_RETURN_URL = "https://grow.example.com/amazon/oauth/callback"


class OAuthError(RuntimeError):
    pass


def _env(key: str) -> str:
    v = os.environ.get(key, "").strip()
    if not v:
        raise OAuthError(f"env {key} is not set")
    return v


# ── state management ────────────────────────────────────────────────────────

async def generate_consent_url(
    pool: asyncpg.Pool,
    *,
    account_ref: str,
    scope: str = DEFAULT_SCOPE,
    return_url: str = DEFAULT_RETURN_URL,
    notes: str | None = None,
) -> str:
    """Generate the LWA consent URL the operator clicks to authorize.

    Persists a one-time state token with 10-min TTL for CSRF protection.
    """
    client_id = _env("AMAZON_ADS_CLIENT_ID")
    state = secrets.token_urlsafe(32)

    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO ads_agent.amazon_oauth_state (state, account_ref, scope, notes)
               VALUES ($1, $2, $3, $4)""",
            state, account_ref, scope, notes,
        )

    params = {
        "client_id":     client_id,
        "scope":         scope,
        "response_type": "code",
        "redirect_uri":  return_url,
        "state":         state,
    }
    return f"{LWA_CONSENT_URL}?{urlencode(params)}"


async def consume_state(pool: asyncpg.Pool, state: str) -> dict | None:
    """Atomically claim a pending state token. Returns the row if valid + unused
    + unexpired; else None. Marks used_at so it can't be replayed.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE ads_agent.amazon_oauth_state
               SET used_at = NOW()
               WHERE state = $1
                 AND used_at IS NULL
                 AND expires_at > NOW()
               RETURNING account_ref, scope, notes""",
            state,
        )
    return dict(row) if row else None


# ── token exchange ──────────────────────────────────────────────────────────

async def exchange_code_for_tokens(
    code: str, return_url: str = DEFAULT_RETURN_URL,
) -> dict:
    """POST to LWA token endpoint, return {access_token, refresh_token,
    expires_in, scope, token_type}."""
    client_id     = _env("AMAZON_ADS_CLIENT_ID")
    client_secret = _env("AMAZON_ADS_CLIENT_SECRET")

    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.post(
            LWA_TOKEN_URL,
            data={
                "grant_type":    "authorization_code",
                "code":          code,
                "redirect_uri":  return_url,
                "client_id":     client_id,
                "client_secret": client_secret,
            },
        )
    body = r.json()
    if r.status_code != 200 or "access_token" not in body:
        raise OAuthError(f"token exchange failed: {r.status_code} {body}")
    return body


async def refresh_access_token(refresh_token: str) -> dict:
    """Mint a new access_token from a refresh_token. Access tokens last ~1h."""
    client_id     = _env("AMAZON_ADS_CLIENT_ID")
    client_secret = _env("AMAZON_ADS_CLIENT_SECRET")

    async with httpx.AsyncClient(timeout=20.0) as c:
        r = await c.post(
            LWA_TOKEN_URL,
            data={
                "grant_type":    "refresh_token",
                "refresh_token": refresh_token,
                "client_id":     client_id,
                "client_secret": client_secret,
            },
        )
    body = r.json()
    if r.status_code != 200 or "access_token" not in body:
        raise OAuthError(f"refresh failed: {r.status_code} {body}")
    return body


# ── token storage ───────────────────────────────────────────────────────────

async def store_tokens(
    pool: asyncpg.Pool,
    *,
    account_ref: str,
    refresh_token: str,
    scope: str,
    access_token: str | None = None,
    expires_in: int | None = None,
    region: str | None = None,
) -> int:
    """Insert (or revive) an amazon_oauth_tokens row. Only one live row per
    account_ref is allowed by the partial unique index.
    """
    region = region or os.environ.get("AMAZON_ADS_REGION", "FE")
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        if expires_in else None
    )

    async with pool.acquire() as conn:
        # Revoke any live row for this account_ref first
        await conn.execute(
            """UPDATE ads_agent.amazon_oauth_tokens
               SET revoked_at = NOW(), revoke_reason = 'superseded by new authorization'
               WHERE account_ref = $1 AND revoked_at IS NULL""",
            account_ref,
        )
        token_id = await conn.fetchval(
            """INSERT INTO ads_agent.amazon_oauth_tokens
               (account_ref, refresh_token, scope, region,
                last_access_token, last_access_token_expires_at)
               VALUES ($1, $2, $3, $4, $5, $6)
               RETURNING id""",
            account_ref, refresh_token, scope, region,
            access_token, expires_at,
        )
    log.info("stored new Amazon tokens for %s (id=%s, region=%s, scope=%s)",
             account_ref, token_id, region, scope)
    return token_id


async def get_live_token(pool: asyncpg.Pool, account_ref: str) -> Optional[dict]:
    """Fetch the current live token row (non-revoked) for an account."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT * FROM ads_agent.amazon_oauth_tokens
               WHERE account_ref = $1 AND revoked_at IS NULL
               ORDER BY created_at DESC
               LIMIT 1""",
            account_ref,
        )
    return dict(row) if row else None


# ── full receive-handler used by the FastAPI endpoint ──────────────────────

async def receive_callback(
    pool: asyncpg.Pool,
    *,
    code: str,
    state: str,
) -> dict:
    """Handle the callback payload forwarded by the Cloudflare Pages Function.
    Returns a summary dict for the success page.
    """
    if not code or not state:
        raise OAuthError("missing code or state in callback")

    state_row = await consume_state(pool, state)
    if not state_row:
        raise OAuthError("state invalid, expired, or already used")

    account_ref = state_row["account_ref"] or "unknown"
    scope       = state_row["scope"] or DEFAULT_SCOPE

    tokens = await exchange_code_for_tokens(code)
    refresh = tokens["refresh_token"]
    access  = tokens.get("access_token")
    exp     = tokens.get("expires_in")

    token_id = await store_tokens(
        pool,
        account_ref=account_ref,
        refresh_token=refresh,
        scope=tokens.get("scope") or scope,
        access_token=access,
        expires_in=exp,
    )

    return {
        "ok":           True,
        "account_ref":  account_ref,
        "token_id":     token_id,
        "scope":        tokens.get("scope"),
        "expires_in":   exp,
    }
