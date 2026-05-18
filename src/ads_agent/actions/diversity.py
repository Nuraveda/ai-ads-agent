"""Andromeda creative-diversity check (M15) — text-only.

Computes pairwise cosine similarity over a TF-IDF representation of each
ad's display text (ad_name + body + title). Andromeda (Meta's Oct 2025
delivery model) clusters near-identical creatives and suppresses delivery
on the duplicates; pairs with similarity > 0.6 are at risk.

Image-hash similarity is the next step (creative_thumbnail download +
perceptual hash) but is much heavier; this text-only pass already
catches the most common offender: "Variant N — same hook, same body".

Returns:
  {
    "ads": [{"ad_id":..., "label":..., "max_sim":..., "near_dupes":[ids...]}, ...],
    "n_high":  count of ads with max_sim > 0.6,
    "n_warn":  count between 0.55 and 0.6,
    "max_observed": float,
  }

No sklearn dependency — small matrix size makes pure-Python sufficient.
"""
from __future__ import annotations

import logging
import math
import re
from collections import Counter
from typing import Iterable

log = logging.getLogger(__name__)

_TOKEN_RE = re.compile(r"\w+")
_STOP = {
    "the", "a", "an", "and", "or", "of", "for", "to", "in", "on", "at",
    "is", "are", "with", "your", "you", "i", "we", "this", "that",
    "by", "from", "as", "be", "it", "its", "if", "but",
    # Common ad-creative noise
    "ad", "ads", "copy", "test", "v1", "v2", "v3", "new", "old", "final",
}


def _tokenize(text: str) -> list[str]:
    return [t.lower() for t in _TOKEN_RE.findall(text or "") if t.lower() not in _STOP and len(t) > 2]


def _tf_idf(docs: list[list[str]]) -> list[Counter]:
    """Compute simple TF-IDF vectors per doc (Counters keyed by term)."""
    n_docs = len(docs)
    if n_docs == 0:
        return []
    df: Counter = Counter()
    for d in docs:
        for term in set(d):
            df[term] += 1
    out: list[Counter] = []
    for d in docs:
        if not d:
            out.append(Counter())
            continue
        tf = Counter(d)
        max_tf = max(tf.values())
        v: Counter = Counter()
        for term, count in tf.items():
            tf_norm = count / max_tf
            idf = math.log((n_docs + 1) / (df[term] + 1)) + 1
            v[term] = tf_norm * idf
        out.append(v)
    return out


def _cosine(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    dot = sum(a[t] * b[t] for t in common)
    na = math.sqrt(sum(v * v for v in a.values()))
    nb = math.sqrt(sum(v * v for v in b.values()))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def diversity_report(ads: Iterable[dict]) -> dict:
    """Score creative diversity over a list of ad dicts.

    Each ad dict needs at least:
      ad_id, ad_name (optional), creative.body / creative.title (optional)
    The output's `ads` array is sorted by max_sim DESC so the worst
    offenders surface first.
    """
    items: list[dict] = []
    for ad in ads:
        ad_id = str(ad.get("ad_id") or "")
        if not ad_id:
            continue
        creative = ad.get("creative") or {}
        text = " ".join(filter(None, [
            ad.get("ad_name", ""),
            creative.get("title", ""),
            creative.get("body", ""),
        ]))
        items.append({
            "ad_id": ad_id,
            "label": (ad.get("ad_name") or "")[:80],
            "tokens": _tokenize(text),
        })
    n = len(items)
    if n < 2:
        return {"ads": [], "n_high": 0, "n_warn": 0, "max_observed": 0.0}

    vectors = _tf_idf([it["tokens"] for it in items])
    rows: list[dict] = []
    max_observed = 0.0
    for i in range(n):
        sims: list[tuple[float, str]] = []
        for j in range(n):
            if i == j:
                continue
            s = _cosine(vectors[i], vectors[j])
            if s > 0:
                sims.append((s, items[j]["ad_id"]))
        sims.sort(reverse=True)
        max_sim = sims[0][0] if sims else 0.0
        max_observed = max(max_observed, max_sim)
        near_dupes = [aid for s, aid in sims if s >= 0.55]
        rows.append({
            "ad_id":      items[i]["ad_id"],
            "label":      items[i]["label"],
            "max_sim":    round(max_sim, 3),
            "near_dupes": near_dupes[:5],
        })

    rows.sort(key=lambda r: r["max_sim"], reverse=True)
    n_high = sum(1 for r in rows if r["max_sim"] > 0.60)
    n_warn = sum(1 for r in rows if 0.55 < r["max_sim"] <= 0.60)
    return {
        "ads": rows,
        "n_high": n_high,
        "n_warn": n_warn,
        "max_observed": round(max_observed, 3),
    }
