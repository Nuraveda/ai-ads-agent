"""Central configuration for the AI Marketing Stack Ads Agent.

Single source of truth for:
  - Store registry (shop domain -> custom app slug + Meta ad account)
  - Meta ad-account multimap (one store may spend through several Meta accounts)
  - Required Shopify scope matrix per Custom App
  - Env-derived runtime settings (DB, Meta, PostHog, Telegram, LLM)

**Real store data loads from env vars at runtime** — the committed file carries
only an illustrative placeholder so the repo can serve as a public showcase
without leaking client myshopify domains or Meta act_... IDs.

Point STORES_JSON and STORE_AD_ACCOUNTS_JSON in .env at the real data.
See .env.example for the exact shape.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from functools import lru_cache

from dotenv import load_dotenv
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)

load_dotenv()


@dataclass(frozen=True)
class Store:
    """One Shopify storefront in the AI Marketing Stack fleet."""

    slug: str  # short handle used in Telegram commands (e.g. "urban")
    brand: str  # human-facing brand name
    shop_domain: str  # *.myshopify.com
    custom_app: str  # matches *_CLIENT_ID prefix in multi-store-theme-manager/.env
    meta_ad_account: str | None  # act_... primary; all linked accounts live in STORE_AD_ACCOUNTS
    currency: str
    notes: str = ""


# Placeholder registry (public showcase). The real registry loads from
# STORES_JSON in .env at runtime — see _load_stores() below.
_PLACEHOLDER_STORES: tuple[Store, ...] = (
    Store(
        slug="store-a",
        brand="Store A (Example)",
        shop_domain="your-store-a.myshopify.com",
        custom_app="store-a",
        meta_ad_account="act_REPLACE_WITH_YOUR_ACCOUNT_ID",
        currency="USD",
        notes="Placeholder — replace via STORES_JSON env var.",
    ),
    Store(
        slug="store-b-india",
        brand="Store B (India)",
        shop_domain="your-store-b.myshopify.com",
        custom_app="store-b-ind",
        meta_ad_account="act_REPLACE_WITH_YOUR_ACCOUNT_ID",
        currency="INR",
        notes="Example of a shared-ad-account sibling.",
    ),
    Store(
        slug="store-b-global",
        brand="Store B (Global)",
        shop_domain="your-store-b-global.myshopify.com",
        custom_app="store-b",
        meta_ad_account="act_REPLACE_WITH_YOUR_ACCOUNT_ID",
        currency="INR",
        notes="Shares ad account with Store B India; reconcile ROAS across both.",
    ),
)


def _load_stores() -> tuple[Store, ...]:
    """Load STORES from STORES_JSON env var; fall back to placeholders."""
    raw = os.environ.get("STORES_JSON", "").strip()
    if not raw:
        return _PLACEHOLDER_STORES
    try:
        entries = json.loads(raw)
        return tuple(
            Store(
                slug=e["slug"],
                brand=e.get("brand", e["slug"]),
                shop_domain=e["shop_domain"],
                custom_app=e["custom_app"],
                meta_ad_account=e.get("meta_ad_account"),
                currency=e.get("currency", "USD"),
                notes=e.get("notes", ""),
            )
            for e in entries
        )
    except (json.JSONDecodeError, KeyError) as ex:
        log.warning("STORES_JSON invalid, using placeholders: %s", ex)
        return _PLACEHOLDER_STORES


STORES: tuple[Store, ...] = _load_stores()


def _load_store_ad_accounts() -> dict[str, list[str]]:
    """Map store_slug -> list of Meta ad account IDs to sum spend across.

    Loads from STORE_AD_ACCOUNTS_JSON env var (JSON object). Empty map if unset;
    roas_compute will then reply "no Meta accounts mapped for <slug>" gracefully.
    """
    raw = os.environ.get("STORE_AD_ACCOUNTS_JSON", "").strip()
    if not raw:
        return {}
    try:
        return {k: list(v) for k, v in json.loads(raw).items()}
    except (json.JSONDecodeError, TypeError, ValueError) as ex:
        log.warning("STORE_AD_ACCOUNTS_JSON invalid, returning empty map: %s", ex)
        return {}


STORE_AD_ACCOUNTS: dict[str, list[str]] = _load_store_ad_accounts()


# ---------------------------------------------------------------------------
# TikTok mapping — store slug -> TikTok advertiser account metadata.
# ---------------------------------------------------------------------------


def _load_store_tiktok_accounts() -> dict[str, dict[str, str]]:
    raw = os.environ.get("STORE_TIKTOK_ACCOUNTS_JSON", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as ex:
        log.warning("STORE_TIKTOK_ACCOUNTS_JSON invalid, returning empty map: %s", ex)
        return {}
    out: dict[str, dict[str, str]] = {}
    for slug, cfg in parsed.items():
        if not isinstance(cfg, dict):
            continue
        advertiser_id = str(cfg.get("advertiser_id") or "").strip()
        country = str(cfg.get("country") or "").strip().upper()
        if not advertiser_id:
            log.warning("STORE_TIKTOK_ACCOUNTS_JSON[%s] missing advertiser_id", slug)
            continue
        entry: dict = {"advertiser_id": advertiser_id, "country": country}
        # Optional fields used by the Meta→TikTok port workflow. All are strings
        # except default_location_ids (list[str]). Missing → workflow prompts.
        for k in ("identity_id", "identity_type", "pixel_id", "currency"):
            v = cfg.get(k)
            if v:
                entry[k] = str(v).strip()
        locs = cfg.get("default_location_ids")
        if isinstance(locs, list):
            entry["default_location_ids"] = [str(x).strip() for x in locs if x]
        out[slug] = entry
    return out


STORE_TIKTOK_ACCOUNTS: dict[str, dict] = _load_store_tiktok_accounts()


# ---------------------------------------------------------------------------
# GA4 stream mapping — store slug -> GA4 property + data stream.
#
# Shape of STORE_GA4_STREAMS_JSON env var:
#   {
#     "<client>-ind":    {"property_id": "484508586", "stream_id": "10481705777"},
#     "<client>-global": {"property_id": "484508586", "stream_id": "14412103683"}
#   }
#
# property_id  — GA4 property number (shared between IN + Global under <client>)
# stream_id    — data-stream ID used to filter reports to one domain
#
# Stores without an entry here are skipped by GA4-dependent nodes (no error,
# no output line) so the agent keeps working for brands that don't have GA4
# plumbed in yet (Urban family, Mokshya as of 2026-04-22).
# ---------------------------------------------------------------------------


def _load_store_ga4_streams() -> dict[str, dict[str, str]]:
    raw = os.environ.get("STORE_GA4_STREAMS_JSON", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as ex:
        log.warning("STORE_GA4_STREAMS_JSON invalid, returning empty map: %s", ex)
        return {}
    out: dict[str, dict[str, str]] = {}
    for slug, cfg in parsed.items():
        if not isinstance(cfg, dict):
            log.warning("STORE_GA4_STREAMS_JSON[%s] is not an object — skipping", slug)
            continue
        pid = str(cfg.get("property_id") or "").strip()
        sid = str(cfg.get("stream_id") or "").strip()
        if not pid:
            log.warning("STORE_GA4_STREAMS_JSON[%s] missing property_id — skipping", slug)
            continue
        out[slug] = {"property_id": pid, "stream_id": sid}
    return out


STORE_GA4_STREAMS: dict[str, dict[str, str]] = _load_store_ga4_streams()


# ---------------------------------------------------------------------------
# Marketplace Ad Pros mapping — store slug → MAP integration + account + country.
#
# Shape of STORE_MAP_ACCOUNTS_JSON env var:
#   {
#     "<client>-ind":    {"integration_id": "<uuid>", "account_id": "<uuid>", "country": "IN"},
#     "<client>-global": {"integration_id": "<uuid>", "account_id": "<uuid>", "country": "AE"}
#   }
#
# Only primary-market mapping today. Multi-market stores (<client>-global
# covers AE + UK + IE + ES + PL) pick the dominant ad-spend market as
# primary; other markets can be queried via /amazon_recs <slug> <country>
# with an explicit country override.
# ---------------------------------------------------------------------------


def _load_store_map_accounts() -> dict[str, dict[str, str]]:
    raw = os.environ.get("STORE_MAP_ACCOUNTS_JSON", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError) as ex:
        log.warning("STORE_MAP_ACCOUNTS_JSON invalid, returning empty map: %s", ex)
        return {}
    out: dict[str, dict[str, str]] = {}
    for slug, cfg in parsed.items():
        if not isinstance(cfg, dict):
            continue
        iid = str(cfg.get("integration_id") or "").strip()
        aid = str(cfg.get("account_id") or "").strip()
        country = str(cfg.get("country") or "").strip().upper()
        if not iid or not aid:
            log.warning("STORE_MAP_ACCOUNTS_JSON[%s] missing integration_id or account_id", slug)
            continue
        out[slug] = {"integration_id": iid, "account_id": aid, "country": country}
    return out


STORE_MAP_ACCOUNTS: dict[str, dict[str, str]] = _load_store_map_accounts()


# ---------------------------------------------------------------------------
# AMAZON_ACCOUNTS_JSON — store slug → list of Amazon accounts (Seller +
# Sponsored Ads, by data-source ds_id). Parsed once at import time.
#
# Used by: native Amazon Ads client (slug → marketplace → profile_id),
# the meta_audit halo gate (slug must be in this map for halo to fire),
# and Airbyte → Postgres sync mappings.
# ---------------------------------------------------------------------------


def _load_store_amazon_accounts() -> dict[str, list[dict]]:
    raw = os.environ.get("AMAZON_ACCOUNTS_JSON", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning("AMAZON_ACCOUNTS_JSON invalid: %s", e)
        return {}
    out: dict[str, list[dict]] = {}
    for slug, accts in (parsed or {}).items():
        if not isinstance(accts, list):
            continue
        out[slug] = [a for a in accts if isinstance(a, dict)]
    return out


STORE_AMAZON_ACCOUNTS: dict[str, list[dict]] = _load_store_amazon_accounts()


# Scopes we need above the existing write_orders baseline to do analytics reads.
# Apply these by bumping *_SCOPES csv in /home/support/multi-store-theme-manager/.env
# then forcing merchant re-auth.
REQUIRED_ANALYTICS_SCOPES: tuple[str, ...] = (
    "read_orders",
    "read_customers",
    "read_products",
    "read_analytics",  # Plus-only; agent should gracefully degrade if denied
    "read_reports",
)


def get_store(slug_or_domain: str) -> Store | None:
    """Resolve by slug or shop_domain. Case-insensitive on slug."""
    s = slug_or_domain.strip().lower()
    for store in STORES:
        if store.slug.lower() == s or store.shop_domain.lower() == s:
            return store
    return None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Postgres — two DSNs:
    #   RO:  role that can ONLY read the Shopify auth-hub `Session` table.
    #        Used by `shopify.sessions` to pull per-store Shopify access tokens.
    #   RW:  role that owns `ads_agent.*` (agent_memory, agent_actions, ...).
    #        Used by memory writes, action inserts, approval callbacks, executor.
    # If `postgres_rw_url` is blank, code falls back to `postgres_insights_ro_url`
    # for backwards compatibility — but any write path will fail against a
    # properly-locked RO role, which is what issue #3 is about. Set both in prod.
    postgres_insights_ro_url: str = "postgresql://insights_ro:changeme@127.0.0.1:5432/your_db_name"
    postgres_rw_url: str = ""

    # Shopify per-app webhook HMAC secrets — JSON map { custom_app_slug: secret }
    shopify_webhook_secrets: str = "{}"

    # Meta Ads MCP (local, streamable-http) + direct SDK
    meta_ads_mcp_url: str = "http://127.0.0.1:3103"
    meta_access_token: str = ""
    meta_app_id: str = ""
    meta_app_secret: str = ""

    # TikTok Business API (forked SDK wrapper under ads_agent.tiktok)
    tiktok_access_token: str = ""
    tiktok_app_id: str = ""
    tiktok_app_secret: str = ""
    tiktok_env: str = "sandbox"

    # Canva Connect API — drives a Canva account from the server: autofill
    # brand templates with copy/images, upload assets, export designs.
    # Used as a creative-asset producer for ads + social posts.
    #
    # Register an integration at https://www.canva.com/developers/integrations
    # and set the redirect URL there to canva_redirect_uri exactly.
    #
    # OAuth 2.0 with PKCE. Access tokens last ~4 hours; refresh tokens 90 days.
    # Tokens are stored in ads_agent.canva_oauth_tokens (RW role required).
    canva_client_id: str = ""
    canva_client_secret: str = ""
    canva_redirect_uri: str = "https://grow.example.com/canva/oauth/callback"
    canva_api_base: str = "https://api.canva.com/rest/v1"
    canva_auth_base: str = "https://www.canva.com"
    canva_default_scopes: str = (
        "asset:read asset:write "
        "design:meta:read design:content:read design:content:write "
        "brandtemplate:meta:read brandtemplate:content:read "
        "profile:read"
    )

    # PostHog Cloud
    posthog_api_key: str = ""
    posthog_host: str = "https://us.i.posthog.com"

    # Telegram
    telegram_bot_token_ads: str = ""
    telegram_admin_ids: str = ""  # csv of int ids
    # Secret token configured via Telegram setWebhook?secret_token=... and
    # echoed by Telegram in the `X-Telegram-Bot-Api-Secret-Token` header on
    # every update. We reject updates that don't carry it (issue #1).
    telegram_webhook_secret: str = ""

    # Shared-secret bearer token required to invoke POST /agent/run.
    # Blank = endpoint is disabled entirely (issue #4). In prod, put this behind
    # Cloud Run IAM or an API gateway as well — do not --allow-unauthenticated.
    agent_run_token: str = ""

    # LLMs (LiteLLM routes to these)
    anthropic_api_key: str = ""
    google_api_key: str = ""
    openai_api_key: str = ""
    vertex_project: str = ""
    vertex_location: str = "us-central1"

    # GA4 — first-party attribution. Path to a service-account JSON with
    # Viewer role on the relevant GA4 properties. Blank → GA4 nodes skip
    # silently, which is the right behavior for brands that aren't wired in.
    ga4_service_account_json_path: str = ""

    # Marketplace Ad Pros — remote Amazon Ads + SP-API MCP at
    # https://app.marketplaceadpros.com/mcp. Bearer-auth via a static API key
    # minted in their dashboard. Free tier gives list_brands + list_resources;
    # $10/wk AI Connect unlocks ask_report_analyst + Amazon's rec endpoints.
    map_api_key: str = ""

    # Runtime
    public_base_url: str = "https://ads.yourdomain.com"
    log_level: str = "INFO"

    @property
    def shopify_webhook_secret_map(self) -> dict[str, str]:
        try:
            return json.loads(self.shopify_webhook_secrets or "{}")
        except json.JSONDecodeError:
            return {}

    @property
    def postgres_rw_dsn(self) -> str:
        """Writable DSN for `ads_agent.*` tables.

        Falls back to `postgres_insights_ro_url` ONLY if `postgres_rw_url` is
        unset — this keeps dev `.env` files working. In prod, set both.
        """
        return self.postgres_rw_url or self.postgres_insights_ro_url

    @property
    def admin_telegram_ids(self) -> set[int]:
        out: set[int] = set()
        for raw in (self.telegram_admin_ids or "").split(","):
            raw = raw.strip()
            if raw.isdigit():
                out.add(int(raw))
        return out


@lru_cache
def settings() -> Settings:
    return Settings()
