"""Shared TikTok node helpers."""
from __future__ import annotations

from dataclasses import dataclass

from ads_agent.config import STORE_TIKTOK_ACCOUNTS, Store, get_store
from ads_agent.tiktok.oauth import resolve_access_token


@dataclass(frozen=True)
class TikTokContext:
    store: Store
    advertiser_id: str
    country: str
    access_token: str | None
    auth_source: str
    identity_id: str | None = None
    identity_type: str | None = None
    pixel_id: str | None = None
    currency: str | None = None
    default_location_ids: tuple[str, ...] = ()


async def load_tiktok_context(slug: str) -> tuple[TikTokContext | None, str | None]:
    store = get_store(slug)
    if store is None:
        return None, f"Unknown store: `{slug}`. Try /stores."

    cfg = STORE_TIKTOK_ACCOUNTS.get(slug)
    if not cfg:
        return None, (
            f"*{store.brand}* · TikTok\n\n"
            f"No TikTok advertiser is mapped for `{slug}`.\n"
            "Set `STORE_TIKTOK_ACCOUNTS_JSON` with an `advertiser_id` for this store."
        )

    advertiser_id = cfg['advertiser_id']
    oauth_token = await resolve_access_token(slug)
    auth_source = 'oauth' if oauth_token else 'env'
    return (
        TikTokContext(
            store=store,
            advertiser_id=advertiser_id,
            country=cfg.get('country') or 'n/a',
            access_token=oauth_token,
            auth_source=auth_source,
            identity_id=cfg.get('identity_id'),
            identity_type=cfg.get('identity_type'),
            pixel_id=cfg.get('pixel_id'),
            currency=cfg.get('currency'),
            default_location_ids=tuple(cfg.get('default_location_ids') or ()),
        ),
        None,
    )
