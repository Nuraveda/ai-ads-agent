"""Canva Connect API OAuth 2.0 + PKCE flow.

Five-step flow coordinated across this FastAPI server and the Cloudflare
Pages Function at grow.example.com/canva/oauth/callback:

  1. Operator hits: GET /api/canva/consent-url?account_ref=<store-or-handle>
  2. Agent generates a random `state` AND a PKCE code_verifier, persists
     both in ads_agent.canva_oauth_state with a 10-minute TTL, then
     returns the Canva authorize URL (which carries the S256 code_challenge).
  3. Operator logs in to Canva and approves access.
  4. Canva redirects to grow.example.com/canva/oauth/callback with
     `?code=X&state=Y`. The CF Pages Function forwards that payload to
     /api/canva/oauth/receive with a shared-secret Bearer header.
  5. Agent looks up the state row (recovers the verifier), exchanges
     code+verifier for access_token and refresh_token, optionally fetches
     /users/me to populate canva_user_id, and stores tokens in
     ads_agent.canva_oauth_tokens.

Token endpoint authenticates via HTTP Basic (client_id:client_secret).
Access tokens last ~4 hours; refresh tokens last ~90 days. Refresh
tokens rotate on every refresh — exchange must be atomic.

Docs: https://www.canva.dev/docs/connect/authentication/
"""
from __future__ import annotations

import base64
import hashlib
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

DEFAULT_RETURN_URL = "https://grow.example.com/canva/oauth/callback"


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


def _settings_or_env(setting_value: str, env_key: str) -> str:
    """Prefer typed Settings value; fall back to raw env (matches the
    pattern other modules in this repo use)."""
    if setting_value:
        return setting_value
    return _env(env_key)


def _new_pkce_pair() -> tuple[str, str]:
    """RFC 7636 S256: verifier is a 43-128 char URL-safe string; challenge
    is base64url(sha256(verifier)) without padding."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _basic_auth_header() -> dict[str, str]:
    s = settings()
    client_id = _settings_or_env(s.canva_client_id, "CANVA_CLIENT_ID")
    client_secret = _settings_or_env(s.canva_client_secret, "CANVA_CLIENT_SECRET")
    creds = f"{client_id}:{client_secret}".encode()
    return {"Authorization": "Basic " + base64.b64encode(creds).decode()}


# ---------------------------------------------------------------------------
# Consent URL + state
# ---------------------------------------------------------------------------

async def generate_consent_url(
    pool: asyncpg.Pool,
    *,
    account_ref: str,
    return_url: str = DEFAULT_RETURN_URL,
    notes: str | None = None,
    scopes: str | None = None,
) -> str:
    s = settings()
    client_id = _settings_or_env(s.canva_client_id, "CANVA_CLIENT_ID")

    state = secrets.token_urlsafe(32)
    verifier, challenge = _new_pkce_pair()

    async with pool.acquire() as conn:
        await conn.execute(
            """INSERT INTO ads_agent.canva_oauth_state
               (state, code_verifier, account_ref, notes)
               VALUES ($1, $2, $3, $4)""",
            state,
            verifier,
            account_ref,
            notes,
        )

    params = {
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": return_url,
        "scope": scopes or s.canva_default_scopes,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{s.canva_auth_base}/api/oauth/authorize?{urlencode(params)}"


async def consume_state(pool: asyncpg.Pool, state: str) -> dict[str, Any] | None:
    """One-shot: marks the state used and returns the row (with verifier)."""
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """UPDATE ads_agent.canva_oauth_state
               SET used_at = NOW()
               WHERE state = $1
                 AND used_at IS NULL
                 AND expires_at > NOW()
               RETURNING account_ref, notes, code_verifier""",
            state,
        )
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Token exchange + refresh
# ---------------------------------------------------------------------------

async def exchange_code_for_tokens(
    *, code: str, code_verifier: str, redirect_uri: str = DEFAULT_RETURN_URL,
) -> dict[str, Any]:
    s = settings()
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        **_basic_auth_header(),
    }
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        resp = await client.post(
            f"{s.canva_api_base}/oauth/token", data=data, headers=headers,
        )
    body = _parse_response(resp, "token exchange")
    if not str(body.get("access_token") or "").strip():
        raise OAuthError(f"token exchange: access_token missing in {body}")
    return body


async def refresh_tokens(refresh_token: str) -> dict[str, Any]:
    s = settings()
    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        **_basic_auth_header(),
    }
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        resp = await client.post(
            f"{s.canva_api_base}/oauth/token", data=data, headers=headers,
        )
    body = _parse_response(resp, "token refresh")
    if not str(body.get("access_token") or "").strip():
        raise OAuthError(f"token refresh: access_token missing in {body}")
    return body


async def fetch_user_profile(access_token: str) -> dict[str, Any] | None:
    """Backfill canva_user_id from /users/me. Returns None on failure
    rather than raising — profile lookup is decorative, not load-bearing."""
    if not access_token:
        return None
    s = settings()
    try:
        async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
            resp = await client.get(
                f"{s.canva_api_base}/users/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
        if resp.status_code != 200:
            log.info("canva /users/me returned %s — skipping", resp.status_code)
            return None
        return resp.json()
    except Exception as exc:
        log.info("canva /users/me errored: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Token persistence
# ---------------------------------------------------------------------------

async def store_tokens(
    pool: asyncpg.Pool,
    *,
    account_ref: str,
    access_token: str,
    refresh_token: str | None,
    expires_in: int | None,
    scopes: list[str],
    canva_user_id: str | None,
    canva_team_id: str | None,
) -> int:
    expires_at = (
        datetime.now(timezone.utc) + timedelta(seconds=expires_in)
        if expires_in and expires_in > 0
        else None
    )

    async with pool.acquire() as conn:
        await conn.execute(
            """UPDATE ads_agent.canva_oauth_tokens
               SET revoked_at = NOW(),
                   revoke_reason = 'superseded by new authorization'
               WHERE account_ref = $1 AND revoked_at IS NULL""",
            account_ref,
        )
        token_id = await conn.fetchval(
            """INSERT INTO ads_agent.canva_oauth_tokens
               (account_ref, access_token, refresh_token,
                access_token_expires_at, scopes, canva_user_id, canva_team_id)
               VALUES ($1, $2, $3, $4, $5::jsonb, $6, $7)
               RETURNING id""",
            account_ref,
            access_token,
            refresh_token,
            expires_at,
            json.dumps(scopes),
            canva_user_id,
            canva_team_id,
        )
    log.info(
        "stored new Canva OAuth token for %s (id=%s, scopes=%s)",
        account_ref, token_id, scopes,
    )
    return int(token_id)


async def get_live_token(pool: asyncpg.Pool, account_ref: str) -> dict[str, Any] | None:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """SELECT * FROM ads_agent.canva_oauth_tokens
               WHERE account_ref = $1 AND revoked_at IS NULL
               ORDER BY created_at DESC
               LIMIT 1""",
            account_ref,
        )
    return dict(row) if row else None


async def resolve_access_token(account_ref: str) -> str | None:
    """Return a currently-valid access token, refreshing if near expiry.

    Returns None if there is no live token, the DB is unreachable, or
    refresh fails — callers should treat None as "Canva not connected
    for this account".
    """
    dsn = settings().postgres_rw_dsn.strip()
    if not dsn or "changeme" in dsn or "your_db_name" in dsn:
        return None
    try:
        pool = await asyncpg.create_pool(dsn, min_size=1, max_size=1)
    except Exception as exc:
        log.warning("Canva OAuth pool init failed for %s: %s", account_ref, exc)
        return None
    try:
        row = await get_live_token(pool, account_ref)
        if not row:
            return None

        # Refresh ~10 min before expiry.
        expires_at = row.get("access_token_expires_at")
        now = datetime.now(timezone.utc)
        needs_refresh = expires_at and (expires_at - timedelta(minutes=10)) <= now

        if not needs_refresh:
            return str(row.get("access_token") or "").strip() or None

        rt = str(row.get("refresh_token") or "").strip()
        if not rt:
            log.warning("Canva token for %s expired and no refresh_token", account_ref)
            return None

        try:
            refreshed = await refresh_tokens(rt)
        except OAuthError as exc:
            log.warning("Canva token refresh failed for %s: %s", account_ref, exc)
            return None

        new_at = str(refreshed.get("access_token") or "").strip()
        new_rt = str(refreshed.get("refresh_token") or "").strip() or rt
        new_expires_in = _to_int(refreshed.get("expires_in"))
        new_scopes = _scopes_from(refreshed)

        await store_tokens(
            pool,
            account_ref=account_ref,
            access_token=new_at,
            refresh_token=new_rt,
            expires_in=new_expires_in,
            scopes=new_scopes,
            canva_user_id=row.get("canva_user_id"),
            canva_team_id=row.get("canva_team_id"),
        )
        return new_at or None
    except Exception as exc:
        log.warning("Canva token resolve failed for %s: %s", account_ref, exc)
        return None
    finally:
        await pool.close()


# ---------------------------------------------------------------------------
# Top-level callback handler
# ---------------------------------------------------------------------------

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
    code_verifier = state_row.get("code_verifier") or ""
    if not code_verifier:
        raise OAuthError("state row missing PKCE code_verifier")

    token_data = await exchange_code_for_tokens(
        code=code, code_verifier=code_verifier,
    )
    access_token = str(token_data.get("access_token") or "").strip()
    refresh_token = str(token_data.get("refresh_token") or "").strip() or None
    expires_in = _to_int(token_data.get("expires_in"))
    scopes = _scopes_from(token_data)

    profile = await fetch_user_profile(access_token) or {}
    user_id = _extract_str(profile, [
        ("team_user", "user_id"), "user_id", "id", ("user", "id"),
    ])
    team_id = _extract_str(profile, [
        ("team_user", "team_id"), "team_id", ("team", "id"),
    ])

    token_id = await store_tokens(
        pool,
        account_ref=account_ref,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        scopes=scopes,
        canva_user_id=user_id,
        canva_team_id=team_id,
    )

    return {
        "ok": True,
        "account_ref": account_ref,
        "token_id": token_id,
        "scopes": scopes,
        "canva_user_id": user_id,
        "canva_team_id": team_id,
        "expires_in": expires_in,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_response(resp: httpx.Response, context: str) -> dict[str, Any]:
    try:
        body = resp.json()
    except json.JSONDecodeError as exc:
        raise OAuthError(f"{context}: non-JSON response {resp.status_code}") from exc
    if resp.status_code >= 400:
        raise OAuthError(f"{context}: {resp.status_code} {body}")
    if not isinstance(body, dict):
        raise OAuthError(f"{context}: unexpected response type {type(body).__name__}")
    return body


def _scopes_from(token_response: dict[str, Any]) -> list[str]:
    raw = token_response.get("scope") or token_response.get("scopes") or ""
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return [item for item in str(raw).split() if item]


def _extract_str(obj: dict[str, Any], paths: list) -> str | None:
    """Walk a list of candidate paths; return first non-empty string found."""
    for p in paths:
        node: Any = obj
        keys = p if isinstance(p, tuple) else (p,)
        for k in keys:
            if not isinstance(node, dict):
                node = None
                break
            node = node.get(k)
        if node is None:
            continue
        s = str(node).strip()
        if s:
            return s
    return None
