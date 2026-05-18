"""GA4 Data API client — thin facade used by `roas_compute` + `tracking_audit`.

Design choices:
  - Read-only: we never write to GA4 from the agent, so we only request the
    analytics.readonly scope.
  - Lazy-init: the BetaAnalyticsDataClient is expensive (gRPC channel + cred
    validation), so we build it once on first use and cache it at module
    level. Acceptable because the SA creds are immutable at runtime.
  - Stream-scoped: every query is filtered by `streamId` so a property shared
    between India + Global storefronts produces clean per-store numbers. If
    the store's mapping lacks a stream_id we return property-wide totals and
    the caller can decide how to present that.
  - Never raises on missing config. `ga4_metrics()` returns None when the
    store has no GA4 mapping or the service-account file is absent, so
    downstream nodes can simply skip the GA4 block without a try/except.

Metric set is intentionally small:
  revenue             — sum of purchaseRevenue (GA4 ecommerce event)
  currency            — the property's reporting currency (advertisersCurrency)
  purchases           — count of ecommercePurchases events
  sessions            — session count in the same window
  converted_sessions  — sessions that contained at least one purchase

That's enough for roas_compute to show a third ROAS (ga4_revenue / spend)
and for tracking_audit to compute session-to-purchase conversion rates.
Richer breakdowns (source/medium/campaign) live in separate helpers below.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import date, timedelta
from functools import lru_cache

from ads_agent.config import STORE_GA4_STREAMS, settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GA4Metrics:
    """Headline numbers for one store + one date window."""
    property_id: str
    stream_id: str  # "" if query was unfiltered
    start_date: str  # YYYY-MM-DD
    end_date: str    # YYYY-MM-DD
    revenue: float
    currency: str
    purchases: int
    sessions: int
    converted_sessions: int  # sessions with at least one purchase


@lru_cache(maxsize=1)
def _client():
    """Cached BetaAnalyticsDataClient + Admin client.

    Imports deferred so the agent can start without GA4 creds (brands that
    haven't set it up yet).
    """
    sa_path = settings().ga4_service_account_json_path.strip()
    if not sa_path or not os.path.exists(sa_path):
        log.debug("GA4 service account path missing or empty; GA4 client unavailable")
        return None, None
    from google.analytics.admin_v1beta import AnalyticsAdminServiceClient
    from google.analytics.data_v1beta import BetaAnalyticsDataClient
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(
        sa_path, scopes=["https://www.googleapis.com/auth/analytics.readonly"],
    )
    return BetaAnalyticsDataClient(credentials=creds), AnalyticsAdminServiceClient(credentials=creds)


async def ga4_metrics(store_slug: str, days: int) -> GA4Metrics | None:
    """Return headline purchase metrics for a store over the last N days.

    Returns None (not an exception) when:
      - the store has no entry in STORE_GA4_STREAMS, or
      - GA4 service-account credentials are not configured.
    """
    cfg = STORE_GA4_STREAMS.get(store_slug)
    if not cfg:
        return None
    data_client, _ = _client()
    if data_client is None:
        return None

    end = date.today()
    start = end - timedelta(days=days)
    return await asyncio.to_thread(
        _run_report_sync,
        data_client,
        cfg["property_id"],
        cfg.get("stream_id", ""),
        start.isoformat(),
        end.isoformat(),
    )


def _run_report_sync(
    data_client,
    property_id: str,
    stream_id: str,
    start_date: str,
    end_date: str,
) -> GA4Metrics:
    """Blocking call into the GA4 Data API. Wrapped by ga4_metrics() in a thread
    so the surrounding LangGraph async node doesn't block the loop."""
    from google.analytics.data_v1beta.types import (
        DateRange, Dimension, Filter, FilterExpression, Metric, RunReportRequest,
    )

    dim_filter = None
    if stream_id:
        dim_filter = FilterExpression(
            filter=Filter(
                field_name="streamId",
                string_filter=Filter.StringFilter(value=stream_id),
            )
        )

    resp = data_client.run_report(RunReportRequest(
        property=f"properties/{property_id}",
        date_ranges=[DateRange(start_date=start_date, end_date=end_date)],
        dimensions=[],
        metrics=[
            Metric(name="purchaseRevenue"),
            Metric(name="ecommercePurchases"),
            Metric(name="sessions"),
            # Session-level conversion count (sessions that had at least one
            # conversion event). Useful later for roas_compute's CAC math
            # against *sessions* rather than *orders*.
            Metric(name="sessionConversionRate"),
        ],
        dimension_filter=dim_filter,
        limit=1,
    ))

    # Pull the numbers; default to zero when no rows.
    revenue = purchases = sessions = 0.0
    session_cvr = 0.0
    if resp.rows:
        mv = resp.rows[0].metric_values
        revenue = float(mv[0].value or 0)
        purchases = int(float(mv[1].value or 0))
        sessions = int(float(mv[2].value or 0))
        session_cvr = float(mv[3].value or 0)

    # Currency reported by GA4 for the property (set in Property Settings).
    # `resp.metadata.currency_code` is the canonical field; fall back to USD
    # if absent (old API versions).
    currency = (getattr(resp.metadata, "currency_code", None) or "USD")

    converted_sessions = int(round(sessions * session_cvr))

    return GA4Metrics(
        property_id=property_id,
        stream_id=stream_id,
        start_date=start_date,
        end_date=end_date,
        revenue=revenue,
        currency=currency,
        purchases=int(purchases),
        sessions=int(sessions),
        converted_sessions=converted_sessions,
    )
