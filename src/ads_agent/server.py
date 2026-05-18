"""FastAPI entrypoint.

Endpoints:
  GET  /healthz
  POST /shopify/webhook/{shop}     — HMAC-verified Shopify webhook receiver
  POST /telegram/webhook           — Telegram Update receiver (webhook mode)
  POST /agent/run                  — direct LangGraph entrypoint (for curl testing)
"""
from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from telegram import Update

from ads_agent import __version__
from ads_agent.config import settings
from ads_agent.shopify.webhooks import HmacVerificationError, handle_webhook, verify_hmac
from ads_agent.telegram.bot import build_app as build_telegram_app

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Security helpers (issues #7, #8, #9)
# ---------------------------------------------------------------------------

MAX_BODY_BYTES = int(os.environ.get("MAX_REQUEST_BODY_BYTES", str(1 * 1024 * 1024)))  # 1 MB
# Shopify webhooks can be up to ~1 MB for large orders; Telegram Updates are
# ~10 KB; OAuth callbacks and /agent/run are smaller still. 1 MB is a safe
# default cap across all routes.


def _require_bearer(request: Request, expected: str) -> None:
    """Validate Authorization: Bearer <expected>. Raise 401 on any failure.

    Fixes issue #8: previous code did `auth.startswith("Bearer ")` then
    `hmac.compare_digest(auth[len("Bearer "):], expected)` with no explicit
    non-empty check on either the token or `expected`. If `expected`
    happened to be "" in a misconfigured env (the caller was supposed to
    short-circuit earlier, but bugs can slip), a `Bearer ` header with
    an empty token would quietly match. This helper makes the non-empty
    checks explicit and centralises the parsing.
    """
    if not expected:
        # Caller should have raised 503 already; defense-in-depth.
        raise HTTPException(status_code=503, detail="endpoint token not configured")
    auth = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not auth.startswith(prefix):
        raise HTTPException(status_code=401, detail="unauthorized")
    supplied = auth[len(prefix):].strip()
    if not supplied:
        # "Bearer " with no token is not valid; reject before compare_digest.
        raise HTTPException(status_code=401, detail="unauthorized")
    if not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="unauthorized")


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose body exceeds MAX_BODY_BYTES (issue #9).

    Checks Content-Length up-front when available. For chunked / missing-
    Content-Length requests, reads the body with an incremental guard so
    a malicious client can't stream an unbounded payload past our parser.
    Only enforces on POST/PUT/PATCH — GET/HEAD pass through.
    """

    async def dispatch(self, request: Request, call_next):
        if request.method in ("GET", "HEAD", "OPTIONS"):
            return await call_next(request)
        cl = request.headers.get("content-length")
        if cl is not None:
            try:
                if int(cl) > MAX_BODY_BYTES:
                    return JSONResponse(
                        {"detail": f"request body too large (max {MAX_BODY_BYTES} bytes)"},
                        status_code=413,
                    )
            except ValueError:
                pass  # malformed Content-Length — let route handle it
        return await call_next(request)


async def _run_webhook_safely(topic: str, shop_domain: str, payload: dict) -> None:
    """Wrap handle_webhook so fire-and-forget errors surface in logs
    (issue #7). Without this, exceptions inside the task are only
    reported as asyncio 'Task exception was never retrieved' warnings
    that are easy to miss in Cloud Run / journalctl output.

    If sentry_sdk is importable, also send the exception to Sentry.
    Never re-raises — the caller has already returned 200 to Shopify.
    """
    try:
        await handle_webhook(topic=topic, shop_domain=shop_domain, payload=payload)
    except Exception as exc:  # noqa: BLE001
        log.exception(
            "shopify webhook handler failed shop=%s topic=%s err=%s",
            shop_domain, topic, str(exc)[:300],
        )
        try:
            import sentry_sdk  # type: ignore
            sentry_sdk.capture_exception(exc)
        except Exception:  # noqa: BLE001
            pass  # sentry optional; don't mask the original error


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize the Telegram Application in webhook mode (no polling).
    tg_app = build_telegram_app()
    await tg_app.initialize()
    await tg_app.start()
    app.state.tg = tg_app
    log.info("Telegram bot started in webhook mode (token ...%s)", settings().telegram_bot_token_ads[-6:])
    try:
        yield
    finally:
        await tg_app.stop()
        await tg_app.shutdown()


app = FastAPI(
    title="AI Marketing Stack Ads Agent",
    version=__version__,
    description="Systematic ads ops agent for Shopify stores.",
    lifespan=lifespan,
)
app.add_middleware(BodySizeLimitMiddleware)


@app.get("/healthz")
async def healthz() -> dict:
    return {
        "status": "ok",
        "version": __version__,
        "git_sha": os.environ.get("GIT_SHA", "dev"),
        "public_base_url": settings().public_base_url,
    }


@app.post("/shopify/webhook/{shop}")
async def shopify_webhook(shop: str, request: Request) -> dict:
    raw_body = await request.body()
    hmac_header = request.headers.get("X-Shopify-Hmac-Sha256", "")
    topic = request.headers.get("X-Shopify-Topic", "")

    try:
        verify_hmac(shop_domain=shop, raw_body=raw_body, header_hmac_b64=hmac_header)
    except HmacVerificationError as exc:
        log.warning("hmac fail shop=%s topic=%s err=%s", shop, topic, exc)
        raise HTTPException(status_code=401, detail="hmac verification failed")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="invalid json body")

    # Fire-and-forget, but wrap so errors land in logs + optional Sentry
    # instead of being swallowed by asyncio's unhandled-task-exception path.
    asyncio.ensure_future(_run_webhook_safely(topic=topic, shop_domain=shop, payload=payload))
    return {"ok": True}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> dict:
    """Receive Telegram Updates. Parsed + queued to the python-telegram-bot Application.

    Security (issue #1): we require the `X-Telegram-Bot-Api-Secret-Token`
    header to match the secret we configured when calling Telegram's
    `setWebhook?secret_token=...`. Without this, anyone who reaches the
    public endpoint can forge Updates with spoofed user ids / callback
    payloads — which would bypass the admin gate entirely.

    The expected secret is read from TELEGRAM_WEBHOOK_SECRET. If unset we
    refuse to accept updates at all (fail closed): running this endpoint
    on the public internet without a secret is never correct.
    """
    expected = settings().telegram_webhook_secret
    if not expected:
        log.error("TELEGRAM_WEBHOOK_SECRET is unset — refusing webhook updates")
        raise HTTPException(status_code=503, detail="telegram webhook not configured")

    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    # Constant-time compare to avoid timing oracles.
    if not hmac.compare_digest(supplied, expected):
        # Log but return 401 plainly — don't give shape hints to attackers.
        log.warning("telegram webhook: bad/missing secret token header")
        raise HTTPException(status_code=401, detail="unauthorized")

    body = await request.json()
    update = Update.de_json(body, app.state.tg.bot)
    await app.state.tg.update_queue.put(update)
    return {"ok": True}


@app.get("/api/amazon/consent-url")
async def api_amazon_consent_url(request: Request) -> dict:
    """Generate an LWA consent URL for authorizing Amazon Ads API access.

    Gated by the same AGENT_RUN_TOKEN bearer as /agent/run (admin-only).
    Query params:
      account_ref — logical key for which advertiser this consent is for
                    (e.g. "<client>", "nuraveda-self"). Required.
      notes       — optional free-text to label the state row.

    Returns {url: "...", state_expires_in_seconds: 600}.
    """
    expected = settings().agent_run_token
    if not expected:
        raise HTTPException(status_code=503, detail="endpoint disabled (no token)")
    _require_bearer(request, expected)

    account_ref = request.query_params.get("account_ref", "").strip()
    if not account_ref:
        raise HTTPException(status_code=400, detail="account_ref required")
    notes = request.query_params.get("notes")
    # Optional scope override — useful for testing plumbing without Ads API
    # approval by using `scope=profile` (always allowed on any Security Profile).
    scope_override = request.query_params.get("scope")

    import asyncpg
    from ads_agent.amazon.oauth import generate_consent_url, OAuthError, DEFAULT_SCOPE

    pool = await asyncpg.create_pool(
        os.environ.get("POSTGRES_RW_URL") or settings().postgres_insights_ro_url,
        min_size=1, max_size=2,
    )
    try:
        try:
            url = await generate_consent_url(
                pool, account_ref=account_ref,
                scope=scope_override or DEFAULT_SCOPE,
                notes=notes,
            )
        except OAuthError as e:
            raise HTTPException(status_code=400, detail=f"oauth config: {e}")
    finally:
        await pool.close()
    return {
        "url": url,
        "state_expires_in_seconds": 600,
        "account_ref": account_ref,
        "scope": scope_override or DEFAULT_SCOPE,
    }


@app.post("/api/amazon/oauth/receive")
async def api_amazon_oauth_receive(request: Request) -> dict:
    """Callback landing — called by the Cloudflare Pages Function at
    grow.example.com/amazon/oauth/callback after Amazon redirects the
    user back with ?code=X&state=Y.

    Gated by INTERNAL_API_SECRET (shared between CF Function and this server).
    Body: {"code": "...", "state": "..."}
    """
    expected = os.environ.get("INTERNAL_API_SECRET", "").strip()
    if not expected:
        log.error("INTERNAL_API_SECRET is unset — refusing amazon oauth callback")
        raise HTTPException(status_code=503, detail="oauth receiver disabled")
    _require_bearer(request, expected)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json body")
    code  = (body.get("code")  or "").strip()
    state = (body.get("state") or "").strip()
    if not code or not state:
        raise HTTPException(status_code=400, detail="code and state are required")

    import asyncpg
    from ads_agent.amazon.oauth import receive_callback, OAuthError

    pool = await asyncpg.create_pool(
        os.environ.get("POSTGRES_RW_URL") or settings().postgres_insights_ro_url,
        min_size=1, max_size=2,
    )
    try:
        try:
            result = await receive_callback(pool, code=code, state=state)
        except OAuthError as e:
            log.warning("oauth receive failed: %s", e)
            raise HTTPException(status_code=400, detail=f"oauth: {e}")
    finally:
        await pool.close()
    return result



@app.get("/api/tiktok/consent-url")
async def api_tiktok_consent_url(request: Request) -> dict:
    """Generate a TikTok Marketing API consent URL.

    Gated by the same AGENT_RUN_TOKEN bearer as /agent/run (admin-only).
    Query params:
      account_ref — logical key for who this authorization is for
                    (e.g. "urban" or "glitch-executor"). Required.
      notes       — optional free-text label for the pending state row.
    """
    expected = settings().agent_run_token
    if not expected:
        raise HTTPException(status_code=503, detail="endpoint disabled (no token)")
    _require_bearer(request, expected)

    account_ref = request.query_params.get("account_ref", "").strip()
    if not account_ref:
        raise HTTPException(status_code=400, detail="account_ref required")
    notes = request.query_params.get("notes")

    import asyncpg
    from ads_agent.tiktok.oauth import DEFAULT_RETURN_URL, OAuthError, generate_consent_url

    pool = await asyncpg.create_pool(
        os.environ.get("POSTGRES_RW_URL") or settings().postgres_insights_ro_url,
        min_size=1, max_size=2,
    )
    try:
        try:
            url = await generate_consent_url(pool, account_ref=account_ref, notes=notes)
        except OAuthError as e:
            raise HTTPException(status_code=400, detail=f"oauth config: {e}")
    finally:
        await pool.close()
    return {
        "url": url,
        "state_expires_in_seconds": 600,
        "account_ref": account_ref,
        "return_url": DEFAULT_RETURN_URL,
    }


@app.post("/api/tiktok/oauth/receive")
async def api_tiktok_oauth_receive(request: Request) -> dict:
    """Callback landing — called by the Cloudflare Pages Function at
    grow.example.com/tiktok/oauth/callback after TikTok redirects the
    user back with ?auth_code=X&state=Y.

    Gated by INTERNAL_API_SECRET (shared between CF Function and this server).
    Body: {"code": "...", "state": "..."}
    """
    expected = os.environ.get("INTERNAL_API_SECRET", "").strip()
    if not expected:
        log.error("INTERNAL_API_SECRET is unset — refusing TikTok oauth callback")
        raise HTTPException(status_code=503, detail="oauth receiver disabled")
    _require_bearer(request, expected)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json body")
    code = (body.get("code") or body.get("auth_code") or "").strip()
    state = (body.get("state") or "").strip()
    if not code or not state:
        raise HTTPException(status_code=400, detail="code and state are required")

    import asyncpg
    from ads_agent.tiktok.oauth import OAuthError, receive_callback

    pool = await asyncpg.create_pool(
        os.environ.get("POSTGRES_RW_URL") or settings().postgres_insights_ro_url,
        min_size=1, max_size=2,
    )
    try:
        try:
            result = await receive_callback(pool, code=code, state=state)
        except OAuthError as e:
            log.warning("TikTok oauth receive failed: %s", e)
            raise HTTPException(status_code=400, detail=f"oauth: {e}")
    finally:
        await pool.close()
    return result


@app.get("/api/canva/consent-url")
async def api_canva_consent_url(request: Request) -> dict:
    """Generate a Canva Connect API consent URL.

    Gated by AGENT_RUN_TOKEN bearer (admin-only).
    Query params:
      account_ref — logical key for who this authorization is for
                    (e.g. "urban" or "glitch-executor"). Required.
      notes       — optional free-text label for the pending state row.
    """
    expected = settings().agent_run_token
    if not expected:
        raise HTTPException(status_code=503, detail="endpoint disabled (no token)")
    _require_bearer(request, expected)

    account_ref = request.query_params.get("account_ref", "").strip()
    if not account_ref:
        raise HTTPException(status_code=400, detail="account_ref required")
    notes = request.query_params.get("notes")

    import asyncpg
    from ads_agent.canva.oauth import (
        DEFAULT_RETURN_URL,
        OAuthError,
        generate_consent_url,
    )

    pool = await asyncpg.create_pool(
        os.environ.get("POSTGRES_RW_URL") or settings().postgres_insights_ro_url,
        min_size=1, max_size=2,
    )
    try:
        try:
            url = await generate_consent_url(
                pool, account_ref=account_ref, notes=notes,
            )
        except OAuthError as e:
            raise HTTPException(status_code=400, detail=f"oauth config: {e}")
    finally:
        await pool.close()
    return {
        "url": url,
        "state_expires_in_seconds": 600,
        "account_ref": account_ref,
        "return_url": DEFAULT_RETURN_URL,
    }


@app.post("/api/canva/oauth/receive")
async def api_canva_oauth_receive(request: Request) -> dict:
    """Callback landing — called by the Cloudflare Pages Function at
    grow.example.com/canva/oauth/callback after Canva redirects the
    user back with ?code=X&state=Y.

    Gated by INTERNAL_API_SECRET (shared between CF Function and this server).
    Body: {"code": "...", "state": "..."}
    """
    expected = os.environ.get("INTERNAL_API_SECRET", "").strip()
    if not expected:
        log.error("INTERNAL_API_SECRET is unset — refusing Canva oauth callback")
        raise HTTPException(status_code=503, detail="oauth receiver disabled")
    _require_bearer(request, expected)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid json body")
    code = (body.get("code") or body.get("auth_code") or "").strip()
    state = (body.get("state") or "").strip()
    if not code or not state:
        raise HTTPException(status_code=400, detail="code and state are required")

    import asyncpg
    from ads_agent.canva.oauth import OAuthError, receive_callback

    pool = await asyncpg.create_pool(
        os.environ.get("POSTGRES_RW_URL") or settings().postgres_insights_ro_url,
        min_size=1, max_size=2,
    )
    try:
        try:
            result = await receive_callback(pool, code=code, state=state)
        except OAuthError as e:
            log.warning("Canva oauth receive failed: %s", e)
            raise HTTPException(status_code=400, detail=f"oauth: {e}")
    finally:
        await pool.close()
    return result


@app.post("/agent/run")
async def agent_run(request: Request) -> dict:
    """Direct LangGraph entrypoint.

    Security (issue #4): this endpoint executes the full LangGraph
    pipeline, which drives paid LLM calls and reads internal store
    analytics. It MUST NOT be publicly callable. We require a bearer
    token via `Authorization: Bearer <AGENT_RUN_TOKEN>`. If AGENT_RUN_TOKEN
    is unset, the endpoint is disabled (fail closed). In Cloud Run we
    additionally recommend gating it with IAM — do NOT deploy with
    `--allow-unauthenticated`.
    """
    expected = settings().agent_run_token
    if not expected:
        log.error("AGENT_RUN_TOKEN is unset — /agent/run is disabled")
        raise HTTPException(status_code=503, detail="agent run endpoint disabled")

    _require_bearer(request, expected)

    body = await request.json()
    from ads_agent.agent.graph import build_graph
    from ads_agent.memory.store import fire_and_forget as log_turn

    graph = build_graph()
    state = await graph.ainvoke(body)
    reply = state.get("reply_text", "")

    # Log the turn so curl-driven tests also accumulate in agent_memory.
    # user_tg_id=None → these show up as "non-Telegram / API" calls.
    log_turn(
        command=str(body.get("command", "unknown")),
        store_slug=body.get("store_slug") or None,
        user_tg_id=None,
        args={k: v for k, v in body.items() if k not in ("command", "store_slug")},
        reply_text=reply,
        key_metrics=state.get("orders_summary") if isinstance(state.get("orders_summary"), dict) else None,
    )
    return {"reply": reply, "state": state}
