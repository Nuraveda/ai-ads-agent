"""recall_prior: hybrid FTS + cosine vector search → MMR → XML-wrapped prior context.

Pattern inspired by openclaw/openclaw `active-memory` extension.

Flow:
  1. Embed the current command context ("command store args") via Gemini.
  2. Postgres query scores rows with: FTS (ts_rank_cd) + vector cosine + recency decay.
  3. Pull top-20 candidates, MMR re-rank for diversity, keep top-5.
  4. Format as <prior_context>…</prior_context> XML block the LLM can read.

Designed with asyncio.wait_for() timeout guard so recall never blocks a reply
longer than the configured budget. On any failure, returns empty string.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import Any

import asyncpg


def _parse_jsonb(v: Any) -> dict:
    if v is None:
        return {}
    if isinstance(v, dict):
        return v
    try:
        return json.loads(v)
    except Exception:
        return {}

from ads_agent.config import settings
from ads_agent.memory.embed import EMBED_DIM, composed_for_query, embed_text

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 2.5          # total budget for recall; fail-open beyond this
CANDIDATE_LIMIT = 20             # how many rows to score in SQL
TOP_K = 5                        # how many MMR-diverse rows to inject
LOOKBACK_DAYS = 30               # only consider turns within this window
MMR_LAMBDA = 0.7                 # relevance vs diversity trade-off (higher = favor relevance)
HALF_LIFE_DAYS = 14              # exponential recency decay

# Weight blend — empirical, tune after a week of real usage
W_FTS = 0.30
W_VEC = 0.55
W_RECENCY = 0.15


_pool: asyncpg.Pool | None = None
_pool_lock = asyncio.Lock()


async def _get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        async with _pool_lock:
            if _pool is None:
                _pool = await asyncpg.create_pool(
                    settings().postgres_insights_ro_url,
                    min_size=1,
                    max_size=4,
                    command_timeout=3.0,
                )
    return _pool


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _mmr(
    query_vec: list[float],
    candidates: list[dict],
    *,
    k: int = TOP_K,
    lam: float = MMR_LAMBDA,
) -> list[dict]:
    """Maximal Marginal Relevance re-rank. Candidates must have 'vec' and 'score' keys."""
    if not candidates:
        return []
    # Pre-compute sims for candidates that have embeddings. Rows without embeddings
    # still score via FTS + recency; we set sim=0 for them (they'll be selected on base score).
    remaining = [c for c in candidates]
    selected: list[dict] = []
    while remaining and len(selected) < k:
        if not selected:
            # First pick: pure relevance
            best = max(remaining, key=lambda c: c["score"])
        else:
            def _mmr_score(c: dict) -> float:
                max_sim = 0.0
                if c.get("vec"):
                    for s in selected:
                        if s.get("vec"):
                            max_sim = max(max_sim, _cosine(c["vec"], s["vec"]))
                return lam * c["score"] - (1 - lam) * max_sim
            best = max(remaining, key=_mmr_score)
        selected.append(best)
        remaining.remove(best)
    return selected


def _format_block(rows: list[dict]) -> str:
    if not rows:
        return ""
    lines = ["<prior_context>"]
    lines.append(
        "Prior related turns from this agent's memory (most relevant first). "
        "Use these to avoid repeating yourself and to detect change over time."
    )
    for r in rows:
        ts = r["ts"]
        age_days = r["age_days"]
        age_str = f"{age_days:.1f}d ago" if age_days >= 1 else f"{int(age_days * 24)}h ago"
        cmd = r["command"]
        slug = r.get("store_slug") or "-"
        args = r.get("args_summary") or ""
        excerpt = (r.get("reply_excerpt") or "").replace("\n", " ").strip()
        lines.append(f"- [{age_str}, /{cmd} {slug} {args}] {excerpt}")
    lines.append("</prior_context>")
    return "\n".join(lines)


async def _search(
    conn: asyncpg.Connection,
    *,
    query_vec_str: str,
    query_fts: str,
    store_slug: str | None,
    command_hint: str | None,
    exclude_id: int | None,
) -> list[dict]:
    """Hybrid SQL: FTS + vector distance + recency. Pull top CANDIDATE_LIMIT."""
    # Build WHERE clauses
    where = ["ts > NOW() - INTERVAL '%d days'" % LOOKBACK_DAYS]
    params: list[Any] = [query_vec_str, query_fts]
    # Narrow by store if provided (but keep global turns too, slightly deprioritized)
    store_filter = ""
    if store_slug:
        store_filter = "(store_slug = $3 OR store_slug IS NULL)"
        where.append(store_filter)
        params.append(store_slug)
    where_sql = " AND ".join(where)
    exclude_sql = ""
    if exclude_id is not None:
        exclude_sql = f"AND id <> ${len(params) + 1}"
        params.append(exclude_id)

    sql = f"""
        SELECT
            id,
            ts,
            EXTRACT(EPOCH FROM (NOW() - ts)) / 86400.0 AS age_days,
            command,
            store_slug,
            args,
            LEFT(COALESCE(reply_text, ''), 400) AS reply_excerpt,
            embedding IS NOT NULL AS has_vec,
            -- Cosine SIMILARITY from pgvector distance (vector_cosine_ops): similarity = 1 - distance
            CASE WHEN embedding IS NOT NULL
                 THEN 1 - (embedding <=> $1::vector)
                 ELSE 0
            END AS vec_sim,
            ts_rank_cd(reply_tsv, plainto_tsquery('english', $2)) AS fts_rank,
            -- Exponential recency decay: exp(-ln2 * age / half_life)
            EXP(-0.693147 * EXTRACT(EPOCH FROM (NOW() - ts)) / 86400.0 / {HALF_LIFE_DAYS}) AS recency,
            embedding
        FROM ads_agent.agent_memory
        WHERE {where_sql}
          {exclude_sql}
        ORDER BY
            -- Pre-filter by combined score; MMR re-ranks in Python
            ({W_VEC} * CASE WHEN embedding IS NOT NULL THEN 1 - (embedding <=> $1::vector) ELSE 0 END)
          + ({W_FTS} * ts_rank_cd(reply_tsv, plainto_tsquery('english', $2)))
          + ({W_RECENCY} * EXP(-0.693147 * EXTRACT(EPOCH FROM (NOW() - ts)) / 86400.0 / {HALF_LIFE_DAYS}))
          DESC
        LIMIT {CANDIDATE_LIMIT};
    """

    rows = await conn.fetch(sql, *params)
    out: list[dict] = []
    for r in rows:
        score = (
            W_VEC * float(r["vec_sim"])
            + W_FTS * float(r["fts_rank"])
            + W_RECENCY * float(r["recency"])
        )
        vec = None
        if r["embedding"] is not None:
            # pgvector returns string like "[0.1,0.2,...]"
            try:
                vec_raw = r["embedding"]
                if isinstance(vec_raw, str):
                    vec = [float(x) for x in vec_raw.strip("[]").split(",")]
            except Exception:
                vec = None
        args = _parse_jsonb(r["args"])
        args_summary = " ".join(f"{k}={v}" for k, v in args.items() if v not in (None, ""))
        out.append({
            "id": r["id"],
            "ts": r["ts"],
            "age_days": float(r["age_days"]),
            "command": r["command"],
            "store_slug": r["store_slug"],
            "args_summary": args_summary,
            "reply_excerpt": r["reply_excerpt"],
            "score": score,
            "vec": vec,
        })
    return out


async def recall_prior(
    *,
    store_slug: str | None,
    command: str,
    args: dict | None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    exclude_id: int | None = None,
) -> str:
    """Return an XML <prior_context> block of up to TOP_K relevant past turns.

    Guaranteed to return quickly — on timeout or any error, returns ''.
    """
    try:
        return await asyncio.wait_for(
            _recall_impl(store_slug=store_slug, command=command, args=args, exclude_id=exclude_id),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        log.info("recall timed out after %.1fs — returning empty context", timeout_s)
        return ""
    except Exception:
        log.exception("recall failed")
        return ""


async def _fetch_lessons(
    conn: asyncpg.Connection, store_slug: str | None, limit: int = 3
) -> list[dict]:
    """Always-on recall for `kind='lesson_learned'` rows for this store.

    Lessons represent persistent truths we want carried into future command
    replies (e.g. pixel-level diagnostics, delivery-partner context). They
    bypass MMR ranking because a) they're rare and b) always relevant for
    their store.
    """
    if not store_slug:
        return []
    rows = await conn.fetch(
        """SELECT id, ts,
                  EXTRACT(EPOCH FROM (NOW() - ts)) / 86400.0 AS age_days,
                  command, store_slug, args,
                  LEFT(COALESCE(reply_text, ''), 600) AS reply_excerpt
           FROM ads_agent.agent_memory
           WHERE kind = 'lesson_learned'
             AND store_slug = $1
           ORDER BY ts DESC
           LIMIT $2""",
        store_slug, limit,
    )
    out = []
    for r in rows:
        args = _parse_jsonb(r["args"])
        out.append({
            "id": r["id"], "ts": r["ts"], "age_days": float(r["age_days"]),
            "command": r["command"], "store_slug": r["store_slug"],
            "args_summary": " ".join(f"{k}={v}" for k, v in args.items() if v not in (None, "")),
            "reply_excerpt": r["reply_excerpt"], "score": 1.0, "vec": None,
        })
    return out


async def _recall_impl(
    *,
    store_slug: str | None,
    command: str,
    args: dict | None,
    exclude_id: int | None,
) -> str:
    query_text = composed_for_query(command=command, store_slug=store_slug, args=args)
    query_vec = await embed_text(query_text)
    if query_vec is None or len(query_vec) != EMBED_DIM:
        log.info("recall: could not embed query, returning empty")
        return ""

    vec_str = "[" + ",".join(f"{v:.7f}" for v in query_vec) + "]"

    pool = await _get_pool()
    async with pool.acquire() as conn:
        # Two passes: (1) always-on lessons for this store, (2) MMR-ranked recent activity
        lessons = await _fetch_lessons(conn, store_slug, limit=3)
        candidates = await _search(
            conn,
            query_vec_str=vec_str,
            query_fts=query_text,
            store_slug=store_slug,
            command_hint=command,
            exclude_id=exclude_id,
        )
        # Don't let MMR re-surface the same lesson rows (deduplicate by id)
        lesson_ids = {l["id"] for l in lessons}
        candidates = [c for c in candidates if c["id"] not in lesson_ids]

    picked = _mmr(query_vec, candidates, k=TOP_K, lam=MMR_LAMBDA)
    # Lessons first (persistent truths), then MMR-ranked fresh activity
    return _format_block(lessons + picked)
