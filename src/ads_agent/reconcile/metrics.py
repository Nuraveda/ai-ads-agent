"""Reconciliation metrics (v1+).

Computes per store x day x campaign:
  match_rate, pixel_only_rate, capi_only_rate, dark_conversion_rate,
  utm_coverage, dedup_event_id_coverage, true_roas vs meta_reported_roas delta.
"""
from __future__ import annotations

# Implementations land in v1 alongside matcher.py.
