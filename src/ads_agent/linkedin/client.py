"""Thin async wrapper around LinkedIn's `/rest/*` Marketing API.

Exposes:
  - LinkedInError                 — raised on auth / quota / API errors
  - get_token()                   — process-cached access token (auto-refreshes)
  - list_ad_accounts()            — accounts the OAuth user has any role on
  - ad_account_id_for(slug)       — slug → numeric account id (from env JSON)
  - request(method, path, ...)    — low-level signed HTTP call

Auth shape:
  - Reuses LinkedIn OAuth from glitch-social-media-agent (same app, same
    user, same client_id/secret). The current access token already has
    Advertising API scopes attached.
  - Refresh token rotates per LinkedIn's policy; we cache the latest
    in-memory and the operator must persist it to .env occasionally.

API version is pinned via `LINKEDIN_API_VERSION` (YYYYMM). Bump when
migrating to a newer release per LinkedIn's quarterly version policy.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

API_HOST = "https://api.linkedin.com"
OAUTH_HOST = "https://www.linkedin.com"


class LinkedInError(RuntimeError):
    """LinkedIn API failure (auth, quota, 4xx/5xx, JSON parse)."""


# ----- env helpers --------------------------------------------------------

def _env(name: str, *, required: bool = True) -> str:
    v = os.environ.get(name, "").strip()
    if not v and required:
        raise LinkedInError(f"{name} not set")
    return v


def _api_version() -> str:
    return os.environ.get("LINKEDIN_API_VERSION", "").strip() or "202604"


# ----- token cache --------------------------------------------------------

_LOCK = threading.Lock()
_TOKEN: str = ""
_TOKEN_EXPIRES_AT: float = 0.0


def get_token() -> str:
    """Return a valid access token; refresh if missing/expired.

    First call: uses LINKEDIN_ACCESS_TOKEN if present, otherwise refreshes
    via LINKEDIN_REFRESH_TOKEN. Subsequent calls re-use the cached token
    until 60s before its known expiry.
    """
    global _TOKEN, _TOKEN_EXPIRES_AT
    with _LOCK:
        now = time.time()
        if _TOKEN and now < _TOKEN_EXPIRES_AT - 60:
            return _TOKEN
        # First-time path: trust the seeded token until refresh proves needed
        if not _TOKEN:
            seeded = os.environ.get("LINKEDIN_ACCESS_TOKEN", "").strip()
            if seeded:
                _TOKEN = seeded
                # We don't know the seeded token's TTL; assume short and let
                # the next 401 trigger a refresh. Default to 50min.
                _TOKEN_EXPIRES_AT = now + 50 * 60
                return _TOKEN
        _refresh_locked()
        return _TOKEN


def _refresh_locked() -> None:
    """Caller must hold _LOCK."""
    global _TOKEN, _TOKEN_EXPIRES_AT
    refresh = _env("LINKEDIN_REFRESH_TOKEN")
    cid = _env("LINKEDIN_CLIENT_ID")
    sec = _env("LINKEDIN_CLIENT_SECRET")
    r = httpx.post(
        f"{OAUTH_HOST}/oauth/v2/accessToken",
        data={
            "grant_type": "refresh_token",
            "refresh_token": refresh,
            "client_id": cid,
            "client_secret": sec,
        },
        timeout=15,
    )
    if r.status_code >= 400:
        raise LinkedInError(f"refresh failed [{r.status_code}]: {r.text[:200]}")
    data = r.json()
    _TOKEN = data["access_token"]
    _TOKEN_EXPIRES_AT = time.time() + int(data.get("expires_in", 50 * 60))
    new_refresh = data.get("refresh_token")
    log.info(
        "linkedin: refreshed access_token (expires_in=%ss, refresh_rotated=%s)",
        data.get("expires_in"), bool(new_refresh and new_refresh != refresh),
    )


def reset_token() -> None:
    """For tests + after manual re-OAuth."""
    global _TOKEN, _TOKEN_EXPIRES_AT
    with _LOCK:
        _TOKEN = ""
        _TOKEN_EXPIRES_AT = 0.0


# ----- low-level HTTP -----------------------------------------------------

def request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict | None = None,
    with_version: bool = True,
    timeout: float = 30.0,
) -> Any:
    """Signed call to LinkedIn API. Auto-retries once on 401 with a refresh.

    `path` should start with /. `with_version=False` is for endpoints like
    /v2/userinfo that reject the LinkedIn-Version header.
    """
    headers = {
        "Authorization": f"Bearer {get_token()}",
        "X-Restli-Protocol-Version": "2.0.0",
        "Accept": "application/json",
    }
    if with_version:
        headers["LinkedIn-Version"] = _api_version()
    if json_body is not None:
        headers["Content-Type"] = "application/json"
    # Restli partial-update bodies use the `{"patch":{"$set":{...}}}` shape.
    # LinkedIn requires X-RestLi-Method: PARTIAL_UPDATE on those calls so
    # the request gets routed through the merge handler instead of full-replace.
    if isinstance(json_body, dict) and "patch" in json_body:
        headers["X-RestLi-Method"] = "PARTIAL_UPDATE"

    # LinkedIn's /rest/* parsers are picky about URL encoding — commas in
    # `fields=…` and colons in URN values must NOT be percent-encoded, but
    # httpx's default `params=` encodes both. Build the query string by hand
    # with a permissive safe-char set so things like
    #   fields=impressions,clicks,costInUsd
    #   accounts[0]=urn:li:sponsoredAccount:504466548
    # round-trip cleanly.
    def _qs(p: dict[str, Any] | None) -> str:
        if not p:
            return ""
        parts = []
        for k, v in p.items():
            parts.append(
                f"{quote(str(k), safe='[]')}={quote(str(v), safe=',:()[]%')}"
            )
        return "?" + "&".join(parts)

    url = f"{API_HOST}{path}{_qs(params)}"

    def _do() -> httpx.Response:
        return httpx.request(
            method,
            url,
            headers={**headers, "Authorization": f"Bearer {get_token()}"},
            json=json_body,
            timeout=timeout,
        )

    r = _do()
    if r.status_code == 401:
        # Stale token — force refresh + retry once
        with _LOCK:
            _refresh_locked()
        r = _do()
    if r.status_code >= 400:
        raise LinkedInError(
            f"LinkedIn {method} {path} [{r.status_code}]: {r.text[:300]}"
        )
    if not r.content:
        # Many LinkedIn write endpoints return 201 + empty body, with the
        # created entity's id in the `x-restli-id` (or `x-linkedin-id`)
        # header. Surface that to the caller.
        rid = r.headers.get("x-restli-id") or r.headers.get("x-linkedin-id") or ""
        return {"_id": rid, "_status": r.status_code}
    try:
        body = r.json()
    except json.JSONDecodeError as e:
        raise LinkedInError(f"non-JSON body from {path}: {r.text[:200]}") from e
    rid = r.headers.get("x-restli-id") or r.headers.get("x-linkedin-id")
    if rid and isinstance(body, dict) and "_id" not in body:
        body = {**body, "_id": rid}
    return body


# ----- account roster -----------------------------------------------------

def list_ad_accounts() -> list[dict]:
    """Every ad account the OAuth user has any role on.

    Returns dicts: {id, urn, name, type, status, currency, serving}.
    """
    out: list[dict] = []
    res = request("GET", "/rest/adAccounts", params={"q": "search"})
    for el in res.get("elements", []):
        aid = el.get("id")
        out.append({
            "id":        str(aid) if aid is not None else "",
            "urn":       f"urn:li:sponsoredAccount:{aid}" if aid is not None else "",
            "name":      el.get("name", ""),
            "type":      el.get("type", ""),
            "status":    el.get("status", ""),
            "currency":  el.get("currency", ""),
            "serving":   el.get("servingStatuses", []) or [],
        })
    return out


def list_account_users(account_id: str) -> list[dict]:
    """Roles assigned on a specific ad account (CAMPAIGN_MANAGER, VIEWER…)."""
    res = request(
        "GET", "/rest/adAccountUsers",
        params={"q": "accounts", "accounts": f"urn:li:sponsoredAccount:{account_id}"},
    )
    out = []
    for el in res.get("elements", []):
        out.append({
            "user":    el.get("user", ""),
            "role":    el.get("role", ""),
            "account": el.get("account", ""),
        })
    return out


# ----- store slug → ad account id -----------------------------------------

def _store_accounts_map() -> dict[str, str]:
    """Parse STORE_LINKEDIN_ADS_ACCOUNTS_JSON.

    Shape:
      {"nuraveda": {"account_id": "504466548"}, ...}
    or shorter:
      {"nuraveda": "504466548", ...}
    """
    raw = os.environ.get("STORE_LINKEDIN_ADS_ACCOUNTS_JSON", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("STORE_LINKEDIN_ADS_ACCOUNTS_JSON invalid: %s", e)
        return {}
    out: dict[str, str] = {}
    for slug, val in (parsed or {}).items():
        if isinstance(val, str):
            out[slug] = val
        elif isinstance(val, dict) and val.get("account_id"):
            out[slug] = str(val["account_id"])
    return out


def ad_account_id_for(slug: str) -> str:
    """Return the LinkedIn ad account id for a store slug.

    Raises LinkedInError if the slug isn't mapped — message guides the
    operator to the Manage Access step the client needs to do.
    """
    m = _store_accounts_map()
    aid = m.get(slug)
    if not aid:
        raise LinkedInError(
            f"no LinkedIn ad account mapped for slug {slug!r}; "
            f"configured slugs: {list(m)}. To wire a new client: have "
            f"them add the founder's LinkedIn user as CAMPAIGN_MANAGER on "
            f"their ad account (Campaign Manager → Manage Access), then "
            f"add the account_id to STORE_LINKEDIN_ADS_ACCOUNTS_JSON in .env."
        )
    return aid


def ad_account_urn_for(slug: str) -> str:
    return f"urn:li:sponsoredAccount:{ad_account_id_for(slug)}"
