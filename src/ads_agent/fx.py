"""FX rate helper — converts between the 4 currencies we touch (CAD, INR, USD, EUR).

Uses open.er-api.com (free, no key, USD-base rates). Cache for 6 hours in memory
because rates don't move materially intraday and our ROAS is daily/weekly at most.

If the API is unreachable, falls back to a hardcoded approximate rates snapshot
so the agent never fails hard on FX; replies carry a "~estimated" marker when
falling back.
"""
from __future__ import annotations

import logging
import time
from typing import Final

import httpx

log = logging.getLogger(__name__)

_CACHE: dict[str, float] = {}
_CACHE_TS: float = 0.0
_CACHE_TTL_SECONDS: Final[int] = 6 * 3600  # 6 hours

# Fallback rates (per 1 USD) — used when open.er-api.com is unreachable.
# Approximate mid-2026 levels, refresh periodically.
_FALLBACK_RATES: Final[dict[str, float]] = {
    "USD": 1.0,
    "INR": 87.0,
    "CAD": 1.38,
    "EUR": 0.93,
}


async def _fetch_rates() -> dict[str, float]:
    """Fetch USD-base rates from open.er-api.com. Returns {currency: rate_per_usd}."""
    url = "https://open.er-api.com/v6/latest/USD"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(url)
        data = r.json()
        if data.get("result") == "success" and "rates" in data:
            return data["rates"]
    except Exception:
        log.warning("FX fetch failed — using fallback rates", exc_info=True)
    return _FALLBACK_RATES


async def _ensure_rates() -> dict[str, float]:
    global _CACHE, _CACHE_TS
    now = time.time()
    if _CACHE and (now - _CACHE_TS) < _CACHE_TTL_SECONDS:
        return _CACHE
    rates = await _fetch_rates()
    _CACHE = rates
    _CACHE_TS = now
    return rates


async def convert(amount: float, from_ccy: str, to_ccy: str) -> float:
    """Convert `amount` from `from_ccy` to `to_ccy` using latest USD-base rates."""
    if not from_ccy or not to_ccy or from_ccy == to_ccy:
        return amount
    rates = await _ensure_rates()
    from_rate = rates.get(from_ccy.upper())
    to_rate = rates.get(to_ccy.upper())
    if from_rate is None or to_rate is None:
        log.warning("FX unknown currency: %s -> %s (cached rates: %s)", from_ccy, to_ccy, list(rates.keys())[:10])
        return amount  # best-effort: leave unchanged so the number is still readable
    # amount / from_rate = USD; * to_rate = target
    return (amount / from_rate) * to_rate


async def is_fallback_in_use() -> bool:
    """True if the last _ensure_rates() used fallback rates (API was down)."""
    # Heuristic: fallback rates dict is tiny; live API returns ~150 currencies.
    rates = await _ensure_rates()
    return len(rates) <= len(_FALLBACK_RATES) + 2
