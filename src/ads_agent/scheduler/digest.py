"""Daily digest + nightly reconciliation entrypoints (v2+).

Invoked by Cloud Scheduler -> Cloud Run /jobs/* endpoints. Stub in v0.
"""
from __future__ import annotations


async def run_daily_digest() -> dict:
    raise NotImplementedError("daily digest lands in v2")


async def run_nightly_reconciliation() -> dict:
    raise NotImplementedError("nightly reconciliation lands in v2")
