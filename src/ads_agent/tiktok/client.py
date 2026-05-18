"""Thin async wrapper around the forked TikTok Business API Python SDK."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from ads_agent.config import settings

_IMPORT_ERROR: Exception | None = None

try:
    from business_api_client.api.account_management_api import AccountManagementApi
    from business_api_client.api.campaign_creation_api import CampaignCreationApi
    from business_api_client.api.measurement_api import MeasurementApi
    from business_api_client.api.reporting_api import ReportingApi
    from business_api_client.models.campaign_status_update_body import CampaignStatusUpdateBody
    from business_api_client.models.campaign_update_body import CampaignUpdateBody
except Exception as exc:  # pragma: no cover - exercised implicitly by runtime imports
    AccountManagementApi = None  # type: ignore[assignment]
    CampaignCreationApi = None  # type: ignore[assignment]
    MeasurementApi = None  # type: ignore[assignment]
    ReportingApi = None  # type: ignore[assignment]
    CampaignStatusUpdateBody = None  # type: ignore[assignment]
    CampaignUpdateBody = None  # type: ignore[assignment]
    _IMPORT_ERROR = exc


class TikTokError(RuntimeError):
    pass


def _require_sdk() -> None:
    if (
        _IMPORT_ERROR is not None
        or AccountManagementApi is None
        or CampaignCreationApi is None
        or MeasurementApi is None
        or ReportingApi is None
        or CampaignStatusUpdateBody is None
        or CampaignUpdateBody is None
    ):
        raise TikTokError(
            "TikTok SDK is unavailable. Install project dependencies to pull "
            "glitch-exec-labs/tiktok-business-api-sdk."
        )


def _access_token(override: str | None = None) -> str:
    token = (override or settings().tiktok_access_token).strip()
    if not token:
        raise TikTokError("TikTok access token not set")
    return token


def _unwrap(response: Any) -> dict[str, Any]:
    if hasattr(response, 'to_dict'):
        body = response.to_dict()
        if isinstance(body, dict):
            return body
    if isinstance(response, dict):
        return response
    raise TikTokError(f"Unexpected TikTok SDK response type: {type(response).__name__}")


def _expect_ok(response: Any) -> dict[str, Any]:
    body = _unwrap(response)
    code = body.get('code')
    if code not in (0, None):
        msg = body.get('message') or 'unknown error'
        req = body.get('request_id') or 'n/a'
        raise TikTokError(f"TikTok API error {code}: {msg} (request_id={req})")
    data = body.get('data')
    return data if isinstance(data, dict) else {}


def _date_window(days: int) -> tuple[str, str]:
    days = max(1, int(days))
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=days - 1)
    return start.isoformat(), end.isoformat()


def _to_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def _to_int(value: Any) -> int:
    try:
        return int(float(value or 0))
    except (TypeError, ValueError):
        return 0


def _page_size(limit: int, *, default: int = 10, maximum: int = 100) -> int:
    try:
        parsed = int(limit)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))


def normalize_campaign_operation(value: str) -> str:
    raw = str(value or '').strip().upper()
    if raw.startswith('OPERATION_STATUS_'):
        raw = raw.removeprefix('OPERATION_STATUS_')
    aliases = {
        'ENABLE': 'ENABLE',
        'ENABLED': 'ENABLE',
        'ON': 'ENABLE',
        'START': 'ENABLE',
        'RESUME': 'ENABLE',
        'DISABLE': 'DISABLE',
        'DISABLED': 'DISABLE',
        'OFF': 'DISABLE',
        'PAUSE': 'DISABLE',
        'PAUSED': 'DISABLE',
        'STOP': 'DISABLE',
    }
    if raw in aliases:
        return aliases[raw]
    raise TikTokError('Unsupported TikTok campaign operation. Use enable or disable.')


async def advertiser_info(advertiser_id: str, *, access_token: str | None = None) -> dict[str, Any]:
    _require_sdk()

    def _call() -> dict[str, Any]:
        try:
            response = AccountManagementApi().advertiser_info(
                [advertiser_id],
                _access_token(access_token),
                fields=['name', 'currency', 'status'],
            )
        except Exception as exc:
            raise TikTokError(f'advertiser_info: {exc}') from exc
        return _expect_ok(response)

    data = await asyncio.to_thread(_call)
    rows = data.get('list') if isinstance(data, dict) else None
    if isinstance(rows, list) and rows:
        first = rows[0]
        return first if isinstance(first, dict) else {}
    return {}


async def advertiser_spend(
    advertiser_id: str,
    days: int = 7,
    *,
    access_token: str | None = None,
) -> dict[str, Any]:
    _require_sdk()
    start_date, end_date = _date_window(days)

    def _call() -> dict[str, Any]:
        try:
            response = ReportingApi().report_integrated_get(
                'BASIC',
                _access_token(access_token),
                advertiser_id=advertiser_id,
                service_type='AUCTION',
                data_level='AUCTION_ADVERTISER',
                dimensions=['stat_time_day'],
                metrics=['spend', 'impressions', 'clicks'],
                start_date=start_date,
                end_date=end_date,
                page=1,
                page_size=max(30, days),
                enable_total_metrics=True,
            )
        except Exception as exc:
            raise TikTokError(f'report_integrated_get: {exc}') from exc
        return _expect_ok(response)

    data = await asyncio.to_thread(_call)
    totals = data.get('total_metrics') if isinstance(data.get('total_metrics'), dict) else {}
    rows = data.get('list') if isinstance(data.get('list'), list) else []
    if not totals and rows:
        totals = {
            'spend': sum(_to_float(r.get('spend')) for r in rows if isinstance(r, dict)),
            'impressions': sum(_to_int(r.get('impressions')) for r in rows if isinstance(r, dict)),
            'clicks': sum(_to_int(r.get('clicks')) for r in rows if isinstance(r, dict)),
        }

    spend = _to_float(totals.get('spend'))
    impressions = _to_int(totals.get('impressions'))
    clicks = _to_int(totals.get('clicks'))
    ctr = (clicks / impressions * 100.0) if impressions else 0.0
    cpc = (spend / clicks) if clicks else 0.0
    return {
        'spend': spend,
        'impressions': impressions,
        'clicks': clicks,
        'ctr': ctr,
        'cpc': cpc,
        'start_date': start_date,
        'end_date': end_date,
        'raw_total_metrics': totals,
    }


async def list_campaigns(
    advertiser_id: str,
    limit: int = 10,
    *,
    access_token: str | None = None,
) -> dict[str, Any]:
    _require_sdk()

    def _call() -> dict[str, Any]:
        try:
            response = CampaignCreationApi().campaign_get(
                advertiser_id,
                _access_token(access_token),
                page=1,
                page_size=_page_size(limit),
                fields=[
                    'campaign_id',
                    'campaign_name',
                    'objective_type',
                    'operation_status',
                    'secondary_status',
                    'budget',
                    'budget_mode',
                    'modify_time',
                ],
            )
        except Exception as exc:
            raise TikTokError(f'campaign_get: {exc}') from exc
        return _expect_ok(response)

    data = await asyncio.to_thread(_call)
    rows = data.get('list') if isinstance(data.get('list'), list) else []
    page_info = data.get('page_info') if isinstance(data.get('page_info'), dict) else {}
    campaigns: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        campaigns.append(
            {
                'campaign_id': str(row.get('campaign_id') or '').strip(),
                'campaign_name': str(row.get('campaign_name') or '').strip(),
                'objective_type': str(row.get('objective_type') or '').strip(),
                'operation_status': str(row.get('operation_status') or '').strip(),
                'secondary_status': str(row.get('secondary_status') or '').strip(),
                'budget': _to_float(row.get('budget')),
                'budget_mode': str(row.get('budget_mode') or '').strip(),
                'modify_time': str(row.get('modify_time') or '').strip(),
            }
        )
    return {'campaigns': campaigns, 'page_info': page_info}


async def update_campaign_status(
    advertiser_id: str,
    campaign_id: str,
    operation_status: str,
    *,
    access_token: str | None = None,
) -> dict[str, Any]:
    _require_sdk()

    def _call() -> dict[str, Any]:
        try:
            body = CampaignStatusUpdateBody(
                advertiser_id=advertiser_id,
                campaign_ids=[str(campaign_id)],
                operation_status=normalize_campaign_operation(operation_status),
            )
            response = CampaignCreationApi().campaign_status_update(
                _access_token(access_token),
                body=body,
            )
        except Exception as exc:
            raise TikTokError(f'campaign_status_update: {exc}') from exc
        return _expect_ok(response)

    return await asyncio.to_thread(_call)


async def update_campaign_budget(
    advertiser_id: str,
    campaign_id: str,
    budget: float,
    *,
    access_token: str | None = None,
) -> dict[str, Any]:
    _require_sdk()

    def _call() -> dict[str, Any]:
        try:
            body = CampaignUpdateBody(
                advertiser_id=advertiser_id,
                campaign_id=str(campaign_id),
                budget=float(budget),
            )
            response = CampaignCreationApi().campaign_update(
                _access_token(access_token),
                body=body,
            )
        except Exception as exc:
            raise TikTokError(f'campaign_update: {exc}') from exc
        return _expect_ok(response)

    return await asyncio.to_thread(_call)


async def list_pixels(
    advertiser_id: str,
    limit: int = 10,
    *,
    access_token: str | None = None,
) -> dict[str, Any]:
    _require_sdk()

    def _call() -> dict[str, Any]:
        try:
            # TikTok caps pixel_list page_size at 20 (vs 100 for other endpoints)
            response = MeasurementApi().pixel_list(
                advertiser_id,
                _access_token(access_token),
                page=1,
                page_size=_page_size(limit, maximum=20),
            )
        except Exception as exc:
            raise TikTokError(f'pixel_list: {exc}') from exc
        return _expect_ok(response)

    data = await asyncio.to_thread(_call)
    rows = data.get('pixels') if isinstance(data.get('pixels'), list) else []
    page_info = data.get('page_info') if isinstance(data.get('page_info'), dict) else {}
    pixels: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        pixels.append(
            {
                'pixel_id': str(row.get('pixel_id') or '').strip(),
                'pixel_name': str(row.get('pixel_name') or '').strip(),
                'pixel_code': str(row.get('pixel_code') or '').strip(),
                'activity_status': str(row.get('activity_status') or '').strip(),
                'partner_name': str(row.get('partner_name') or '').strip(),
                'pixel_setup_mode': str(row.get('pixel_setup_mode') or '').strip(),
                'events': row.get('events') if isinstance(row.get('events'), list) else [],
            }
        )
    return {'pixels': pixels, 'page_info': page_info}


# Ordered preference for lower-funnel events when picking an optimization_event.
# First event present in the pixel's `events` list that is also in this tuple
# wins. Adjust per-brand overrides in the workflow, not here.
PREFERRED_OPTIMIZATION_EVENTS: tuple[str, ...] = (
    # TikTok's API uses these enum names — NOT "PURCHASE" / "COMPLETE_PAYMENT"
    # (those are display names, not API values). Order: highest-funnel-intent first.
    "SHOPPING",          # Complete Payment
    "INITIATE_ORDER",    # Initiate Checkout
    "ADD_BILLING",       # Add Payment Info
    "ON_WEB_CART",       # Add to Cart
    "ON_WEB_DETAIL",     # View Content
    "ON_WEB_ORDER",      # Place an Order (some pixels expose this)
)


async def pick_optimization_event(
    advertiser_id: str,
    pixel_id: str,
    *,
    preference: tuple[str, ...] = PREFERRED_OPTIMIZATION_EVENTS,
    access_token: str | None = None,
) -> tuple[str, list[str]]:
    """Return (chosen_event, all_available_events).

    Looks up the pixel in the advertiser's library and picks the first
    event in `preference` that appears in the pixel's events. If nothing
    matches, raises TikTokError — the caller should not guess.
    """
    data = await list_pixels(advertiser_id, limit=100, access_token=access_token)
    match = next((p for p in data.get('pixels', []) if p['pixel_id'] == str(pixel_id)), None)
    if not match:
        raise TikTokError(f"pixel {pixel_id} not found under advertiser {advertiser_id}")
    available_raw = match.get('events') or []
    # `events` rows may be dicts like {"event": "INITIATE_ORDER", ...}
    available: list[str] = []
    for e in available_raw:
        if isinstance(e, dict):
            name = str(e.get('event') or e.get('event_name') or '').strip()
        else:
            name = str(e).strip()
        if name:
            available.append(name)
    for want in preference:
        if want in available:
            return want, available
    # TikTok's pixel/list endpoint does NOT always populate `events` reliably
    # (we've seen pixels with 30+ purchase events still return events=[]).
    # When the metadata is empty, trust the operator's preference order and
    # let adgroup/create do the actual validation.
    if not available:
        # pixel/list returned events=[] — fall back to first preference;
        # TikTok will validate at adgroup/create.
        return preference[0], available
    raise TikTokError(
        f"pixel {pixel_id} has no preferred event; available={available}, "
        f"preference={list(preference)}"
    )
