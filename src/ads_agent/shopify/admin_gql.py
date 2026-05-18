"""Thin Shopify Admin GraphQL client.

We own this instead of using a community Shopify MCP (all toy-tier). Only the
6-8 queries we actually need for ads ops:
  - orders (with UTM via customer_journey + line_items + discounts)
  - refunds (source-of-truth for net revenue)
  - inventory levels for top-SKU OOS alerts
  - shop info (plan tier, to detect read_analytics availability)

Uses the 2025-01 Admin API version. Token comes from
`ads_agent.shopify.sessions.get_session`.
"""
from __future__ import annotations

import httpx

ADMIN_API_VERSION = "2025-01"


class ShopifyAdminError(RuntimeError):
    pass


class ShopifyAdminClient:
    def __init__(self, shop_domain: str, access_token: str, *, timeout: float = 30.0) -> None:
        self.shop_domain = shop_domain
        self._url = f"https://{shop_domain}/admin/api/{ADMIN_API_VERSION}/graphql.json"
        self._headers = {
            "X-Shopify-Access-Token": access_token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._timeout = timeout

    async def query(self, document: str, variables: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.post(
                self._url,
                headers=self._headers,
                json={"query": document, "variables": variables or {}},
            )
        if resp.status_code != 200:
            raise ShopifyAdminError(f"{self.shop_domain} {resp.status_code}: {resp.text[:500]}")
        body = resp.json()
        if body.get("errors"):
            raise ShopifyAdminError(f"{self.shop_domain} GraphQL errors: {body['errors']}")
        return body["data"]


# --- Canned queries (v0) ------------------------------------------------------

ORDERS_LAST_N_DAYS = """
query ordersLastNDays($query: String!, $first: Int!, $after: String) {
  orders(first: $first, after: $after, query: $query, sortKey: CREATED_AT, reverse: true) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        name
        createdAt
        displayFinancialStatus
        currentTotalPriceSet { shopMoney { amount currencyCode } }
        customer { id email }
        customerJourneySummary {
          firstVisit { landingPageHtml referrerUrl utmParameters { source medium campaign } }
        }
        lineItems(first: 50) {
          edges { node { title quantity sku } }
        }
      }
    }
  }
}
"""


SHOP_INFO = """
query shopInfo {
  shop {
    name
    myshopifyDomain
    plan { displayName partnerDevelopment shopifyPlus }
    currencyCode
  }
}
"""
