"""FastAPI application factory + lifespan management.

The `create_app()` factory is the single entry point used by:

* the `zestimate serve` CLI command,
* the uvicorn CLI (`uvicorn zestimate_agent.api:create_app --factory`),
* the test suite (`TestClient(create_app(agent=fake_agent))`).

Lifespan
--------

A process-wide `ZestimateAgent` is constructed on startup and closed on
shutdown so httpx clients, cache, and Rentcast counter are all reused across
requests. Tests can skip this by passing `agent=...` to `create_app()`, in
which case the lifespan is a no-op and the test-supplied agent is used.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from zestimate_agent import __version__
from zestimate_agent.agent import ZestimateAgent
from zestimate_agent.api import metrics
from zestimate_agent.api.routes import router
from zestimate_agent.config import Settings, get_settings
from zestimate_agent.logging import configure_logging, get_logger

log = get_logger(__name__)


def create_app(
    *,
    agent: ZestimateAgent | None = None,
    settings: Settings | None = None,
) -> FastAPI:
    """Build a FastAPI app.

    Parameters
    ----------
    agent
        Optional pre-built agent to reuse (tests). When supplied, the lifespan
        will **not** close it on shutdown — ownership stays with the caller.
    settings
        Optional settings override. Defaults to `get_settings()`.
    """
    effective_settings = settings or get_settings()
    configure_logging()

    external_agent = agent is not None

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # ─── Startup ─────────────────────────────────────────
        if agent is not None:
            app.state.agent = agent
        else:
            log.info("api starting", host=effective_settings.api_host, port=effective_settings.api_port)
            app.state.agent = ZestimateAgent(effective_settings)
        app.state.settings = effective_settings

        try:
            yield
        finally:
            # ─── Shutdown ────────────────────────────────────
            if not external_agent:
                try:
                    await app.state.agent.aclose()
                except Exception as e:  # pragma: no cover — defensive
                    log.warning("agent close failed", error=str(e))
            log.info("api stopped")

    app = FastAPI(
        title="Zestimate Agent API",
        description=(
            "HTTP interface to the Zestimate agent. Fetches the current Zillow "
            "Zestimate for a US property address with optional Rentcast "
            "cross-check, rich property details, and confidence scoring.\n\n"
            "**Pipeline:** normalize -> resolve -> fetch -> parse -> validate\n\n"
            "**Key invariant:** every lookup returns a structured result with a "
            "`status` field — the API never returns unstructured errors."
        ),
        version=__version__,
        lifespan=lifespan,
        openapi_tags=[
            {
                "name": "lookup",
                "description": "Core Zestimate lookup endpoints. POST an address, get back a valuation.",
            },
            {
                "name": "health",
                "description": "Liveness and readiness probes for orchestrators (k8s, ECS, etc).",
            },
            {
                "name": "meta",
                "description": "Version info and Prometheus metrics exposition.",
            },
        ],
    )

    # ─── CORS (opt-in) ───────────────────────────────────────
    if effective_settings.cors_origin_list:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=effective_settings.cors_origin_list,
            allow_credentials=False,
            allow_methods=["GET", "POST", "OPTIONS"],
            allow_headers=["Content-Type", "X-API-Key"],
        )

    # ─── Metrics middleware ──────────────────────────────────
    @app.middleware("http")
    async def _metrics_mw(request: Request, call_next):  # type: ignore[no-untyped-def]
        started = time.monotonic()
        try:
            response = await call_next(request)
            code = response.status_code
        except Exception:
            code = 500
            raise
        finally:
            elapsed = time.monotonic() - started
            # Use route template (e.g. "/lookup") not raw path to keep cardinality low.
            path = _route_template(request)
            metrics.HTTP_LATENCY.labels(path=path, method=request.method).observe(elapsed)
            metrics.HTTP_REQUESTS.labels(
                path=path, method=request.method, code=str(code)
            ).inc()
        return response

    # ─── Uniform error envelope ──────────────────────────────
    @app.exception_handler(StarletteHTTPException)
    async def _http_exc(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": _reason(exc.status_code), "detail": str(exc.detail)},
            headers=exc.headers,
        )

    app.include_router(router)
    return app


# ─── Helpers ────────────────────────────────────────────────────


def _route_template(request: Request) -> str:
    """Return the FastAPI route template (e.g. '/lookup') for the request.

    Falls back to the raw path when no route matched (404s). This keeps
    Prometheus label cardinality bounded to the number of declared routes.
    """
    route = request.scope.get("route")
    if route is not None and hasattr(route, "path"):
        return str(route.path)
    return request.url.path


def _reason(code: int) -> str:
    return {
        400: "bad_request",
        401: "unauthorized",
        403: "forbidden",
        404: "not_found",
        409: "conflict",
        422: "unprocessable_entity",
        500: "internal_error",
        502: "bad_gateway",
        503: "service_unavailable",
    }.get(code, "error")
