"""Read-only access to the Shopify auth-hub Prisma Session table.

This module reads the `Session` table written by the Shopify auth hub
(a Remix/Shopify-CLI app using @shopify/shopify-app-remix and Prisma with
PrismaSessionStorage). The agent accesses it via a dedicated read-only DB
role so it cannot modify auth state.

Setup the read-only role once:

    CREATE USER insights_ro WITH PASSWORD 'choose_a_strong_password';
    GRANT CONNECT ON DATABASE your_db TO insights_ro;
    GRANT USAGE ON SCHEMA public TO insights_ro;
    GRANT SELECT ON "Session" TO insights_ro;

Then set POSTGRES_INSIGHTS_RO_URL in .env.
"""
from __future__ import annotations

from dataclasses import dataclass

import asyncpg

from ads_agent.config import settings


@dataclass(frozen=True)
class ShopifySession:
    shop: str
    access_token: str
    scope: str
    is_online: bool


async def get_session(shop_domain: str) -> ShopifySession | None:
    """Fetch the offline session for a shop. Returns None if not installed.

    Filters to `id LIKE 'offline_%'` so we skip `oauth_state_%` nonce rows
    that exist during an in-flight install but carry no access token.
    """
    conn = await asyncpg.connect(settings().postgres_insights_ro_url)
    try:
        row = await conn.fetchrow(
            '''SELECT shop, "accessToken", scope, "isOnline"
               FROM "Session"
               WHERE shop = $1
                 AND id LIKE 'offline_%'
                 AND "accessToken" != ''
               ORDER BY expires DESC NULLS FIRST
               LIMIT 1''',
            shop_domain,
        )
    finally:
        await conn.close()
    if row is None:
        return None
    return ShopifySession(
        shop=row["shop"],
        access_token=row["accessToken"],
        scope=row["scope"] or "",
        is_online=row["isOnline"],
    )


async def list_sessions() -> list[ShopifySession]:
    """All completed offline sessions (excludes oauth_state nonce rows)."""
    conn = await asyncpg.connect(settings().postgres_insights_ro_url)
    try:
        rows = await conn.fetch(
            '''SELECT DISTINCT ON (shop) shop, "accessToken", scope, "isOnline"
               FROM "Session"
               WHERE id LIKE 'offline_%'
                 AND "accessToken" != ''
               ORDER BY shop, expires DESC NULLS FIRST'''
        )
    finally:
        await conn.close()
    return [
        ShopifySession(
            shop=r["shop"], access_token=r["accessToken"], scope=r["scope"] or "", is_online=r["isOnline"]
        )
        for r in rows
    ]
