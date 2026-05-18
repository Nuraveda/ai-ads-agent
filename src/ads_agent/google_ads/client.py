"""Thin wrapper around the official `google-ads` Python client.

Exposes:

  - get_googleads_client()        — process-cached, builds from env on first call
  - list_accessible_customers()   — what customer ids the SA can directly see
  - list_mcc_clients(mcc_id)      — every account linked under the manager
  - customer_id_for(slug)         — slug → customer_id via STORE_GOOGLE_ADS_ACCOUNTS_JSON
  - search(slug, gaql)            — run a GAQL query against a slug's account
  - search_mcc(query)             — run a GAQL query against the MCC itself
  - GoogleAdsError                — raised on auth / quota / GAQL errors

Same data-source convention as the other native clients in this repo:
the engine layer is brand-neutral, methodology lives in the playbook.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.errors import GoogleAdsException
from google.auth.transport.requests import Request
from google.oauth2 import service_account

log = logging.getLogger(__name__)


class GoogleAdsError(RuntimeError):
    """Raised on Google Ads API failures (auth, quota, GAQL syntax, etc.)."""


# ----- env helpers --------------------------------------------------------

def _developer_token() -> str:
    v = os.environ.get("GOOGLE_ADS_DEVELOPER_TOKEN", "").strip()
    if not v:
        raise GoogleAdsError("GOOGLE_ADS_DEVELOPER_TOKEN not set")
    return v


def _login_customer_id() -> str:
    v = os.environ.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID", "").strip()
    if not v:
        raise GoogleAdsError("GOOGLE_ADS_LOGIN_CUSTOMER_ID not set (your MCC id)")
    return v


def _quota_project() -> str:
    v = os.environ.get("GOOGLE_ADS_QUOTA_PROJECT_ID", "").strip()
    if not v:
        raise GoogleAdsError(
            "GOOGLE_ADS_QUOTA_PROJECT_ID not set — the GCP project your dev "
            "token was approved against. Our SA must have "
            "Service Usage Consumer IAM on it."
        )
    return v


def _service_account_path() -> str:
    v = os.environ.get("GOOGLE_ADS_SERVICE_ACCOUNT_JSON_PATH", "").strip()
    if not v:
        # Fall back to the GA4 SA path which is the same SA in our setup
        v = os.environ.get("GA4_SERVICE_ACCOUNT_JSON_PATH", "").strip()
    if not v:
        raise GoogleAdsError(
            "GOOGLE_ADS_SERVICE_ACCOUNT_JSON_PATH not set (and no fallback)"
        )
    return v


# ----- client cache --------------------------------------------------------

_CLIENT: GoogleAdsClient | None = None


def get_googleads_client() -> GoogleAdsClient:
    """Build (once) and return the GoogleAdsClient.

    The credentials carry a quota_project override so they can be used
    against a project the SA isn't natively in. Token refresh is handled
    automatically by `google-auth` library.
    """
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT
    creds = service_account.Credentials.from_service_account_file(
        _service_account_path(),
        scopes=["https://www.googleapis.com/auth/adwords"],
    ).with_quota_project(_quota_project())
    creds.refresh(Request())
    _CLIENT = GoogleAdsClient(
        credentials=creds,
        developer_token=_developer_token(),
        login_customer_id=_login_customer_id(),
        use_proto_plus=True,
    )
    log.info(
        "google_ads: client built · MCC=%s · quota_project=%s",
        _login_customer_id(), _quota_project(),
    )
    return _CLIENT


def reset_client() -> None:
    """For tests + after re-OAuth: clear cached client."""
    global _CLIENT
    _CLIENT = None


# ----- store slug → customer_id resolver -----------------------------------

def _store_accounts_map() -> dict[str, str]:
    """Parse STORE_GOOGLE_ADS_ACCOUNTS_JSON.

    Shape:
      {"<client>-ind": {"customer_id": "1234567890"}, ...}
    or shorter:
      {"<client>-ind": "1234567890", ...}
    """
    raw = os.environ.get("STORE_GOOGLE_ADS_ACCOUNTS_JSON", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("STORE_GOOGLE_ADS_ACCOUNTS_JSON invalid: %s", e)
        return {}
    out: dict[str, str] = {}
    for slug, val in (parsed or {}).items():
        if isinstance(val, str):
            out[slug] = val.replace("-", "")
        elif isinstance(val, dict) and val.get("customer_id"):
            out[slug] = str(val["customer_id"]).replace("-", "")
    return out


def customer_id_for(slug: str) -> str:
    """Return the Google Ads customer_id for a store slug.

    Raises GoogleAdsError if the slug isn't mapped — gives a useful
    message about the MCC linking step the operator must complete.
    """
    m = _store_accounts_map()
    cid = m.get(slug)
    if not cid:
        raise GoogleAdsError(
            f"no Google Ads customer_id mapped for slug {slug!r}; "
            f"configured slugs: {list(m)}. To wire a new client: have "
            f"them request access under MCC {_login_customer_id()}, "
            f"approve the link, then add the customer_id to "
            f"STORE_GOOGLE_ADS_ACCOUNTS_JSON in .env."
        )
    return cid


# ----- read endpoints ------------------------------------------------------

def list_accessible_customers() -> list[str]:
    """Customer ids directly accessible to the SA (excludes MCC children
    not yet linked). Returns bare ids (no `customers/` prefix)."""
    client = get_googleads_client()
    cs = client.get_service("CustomerService")
    try:
        res = cs.list_accessible_customers()
    except GoogleAdsException as e:
        raise GoogleAdsError(_format_error(e)) from e
    return [r.split("/")[-1] for r in res.resource_names]


def list_mcc_clients(mcc_id: str | None = None) -> list[dict]:
    """Every account linked under the MCC (level ≤ 2). Default MCC = login_customer_id.

    Returns list of dicts:
      {customer_id, descriptive_name, manager, currency_code, time_zone,
       status, level}
    """
    mcc = (mcc_id or _login_customer_id()).replace("-", "")
    query = """
      SELECT customer_client.id, customer_client.descriptive_name,
             customer_client.manager, customer_client.currency_code,
             customer_client.time_zone, customer_client.status,
             customer_client.level
      FROM customer_client WHERE customer_client.level <= 2
    """
    rows = search_mcc(query, mcc_id=mcc)
    return [
        {
            "customer_id":     str(r.customer_client.id),
            "descriptive_name": r.customer_client.descriptive_name,
            "manager":          bool(r.customer_client.manager),
            "currency_code":    r.customer_client.currency_code,
            "time_zone":        r.customer_client.time_zone,
            "status":           r.customer_client.status.name if r.customer_client.status else "",
            "level":            int(r.customer_client.level),
        }
        for r in rows
    ]


def search(slug: str, gaql: str) -> list[Any]:
    """Run a GAQL query against the slug's customer_id. Returns list of
    raw `GoogleAdsRow` objects (caller responsible for field extraction)."""
    cid = customer_id_for(slug)
    return _search(customer_id=cid, query=gaql)


def search_mcc(query: str, *, mcc_id: str | None = None) -> list[Any]:
    """Run a GAQL query against the MCC itself (e.g. customer_client list)."""
    mcc = (mcc_id or _login_customer_id()).replace("-", "")
    return _search(customer_id=mcc, query=query)


def _search(*, customer_id: str, query: str) -> list[Any]:
    client = get_googleads_client()
    gads = client.get_service("GoogleAdsService")
    rows: list[Any] = []
    try:
        stream = gads.search_stream(customer_id=customer_id, query=query)
        for batch in stream:
            rows.extend(batch.results)
    except GoogleAdsException as e:
        raise GoogleAdsError(_format_error(e)) from e
    return rows


# ----- error formatter -----------------------------------------------------

def _format_error(e: GoogleAdsException) -> str:
    """Pull the structured fields out of GoogleAdsException for a clean message."""
    parts: list[str] = []
    for err in e.failure.errors:
        code = ""
        ec = err.error_code
        for f in ec.DESCRIPTOR.fields:
            v = getattr(ec, f.name, None)
            if v and getattr(v, "name", None) and v != 0:
                code = f"{f.name}.{v.name}"
                break
        parts.append(f"[{code}] {err.message}".strip())
    return "GoogleAdsException: " + " · ".join(parts)
