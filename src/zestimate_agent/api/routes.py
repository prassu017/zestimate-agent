"""HTTP route handlers.

Each handler is a thin async adapter over `ZestimateAgent.aget()` — no business
logic lives here. The agent already guarantees "never raises" so the only
exceptions we expect from it are dependency-injection / misuse errors, which
propagate as 500s.
"""

from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response, status
from fastapi.responses import HTMLResponse

from zestimate_agent import __version__
from zestimate_agent.agent import ZestimateAgent
from zestimate_agent.api import metrics
from zestimate_agent.api.deps import SettingsDep, get_agent, rate_limit, require_api_key
from zestimate_agent.api.landing import LANDING_HTML
from zestimate_agent.api.schemas import (
    HealthResponse,
    LookupRequest,
    LookupResponse,
    VersionResponse,
)
from zestimate_agent.crosscheck import get_usage_counter
from zestimate_agent.logging import get_logger
from zestimate_agent.models import ZestimateStatus

AgentDep = Annotated[ZestimateAgent, Depends(get_agent)]

log = get_logger(__name__)

router = APIRouter()

# Map ZestimateStatus → HTTP status code. OK and stable "no data" statuses
# return 200 — the client inspects `.status` to differentiate. Transient
# failures (blocked/error) return 502 so ops dashboards can alert on 5xx.
_STATUS_TO_HTTP = {
    ZestimateStatus.OK: status.HTTP_200_OK,
    ZestimateStatus.NO_ZESTIMATE: status.HTTP_200_OK,
    ZestimateStatus.NOT_FOUND: status.HTTP_404_NOT_FOUND,
    ZestimateStatus.AMBIGUOUS: status.HTTP_409_CONFLICT,
    ZestimateStatus.BLOCKED: status.HTTP_502_BAD_GATEWAY,
    ZestimateStatus.ERROR: status.HTTP_502_BAD_GATEWAY,
}


# ─── Landing page ───────────────────────────────────────────────


@router.get(
    "/",
    response_class=HTMLResponse,
    include_in_schema=False,  # keep it out of the OpenAPI schema
    summary="Interactive landing page",
)
async def landing() -> HTMLResponse:
    """Serve the single-file demo UI at the root path.

    This is a public, interactive page that calls `POST /lookup` via
    same-origin fetch(). It exists purely for human visitors — machine
    clients should hit `/lookup` directly (which is fully documented in
    `/docs`).
    """
    return HTMLResponse(
        content=LANDING_HTML,
        headers={
            # Short cache so iteration is fast; CDN can still cache briefly.
            "Cache-Control": "public, max-age=60",
        },
    )


# ─── Lookup ─────────────────────────────────────────────────────


@router.post(
    "/lookup",
    response_model=LookupResponse,
    dependencies=[Depends(require_api_key), Depends(rate_limit)],
    responses={
        200: {"description": "Lookup succeeded (check `status` for ok/no_zestimate)."},
        401: {"description": "Missing or invalid API key."},
        404: {"description": "Address did not resolve to a Zillow property."},
        409: {"description": "Address resolved to multiple candidates."},
        502: {"description": "Fetcher blocked or unexpected error."},
    },
    summary="Look up a Zestimate for an address",
)
async def lookup(
    body: LookupRequest,
    response: Response,
    agent: AgentDep,
) -> LookupResponse:
    started = time.monotonic()
    result = await agent.aget(
        body.address,
        skip_crosscheck=body.skip_crosscheck,
        force_crosscheck=body.force_crosscheck,
        use_cache=body.use_cache,
    )
    elapsed = time.monotonic() - started

    metrics.observe_lookup(result, elapsed)

    # Push current Rentcast usage into the gauge — cheap (file read, ~µs).
    try:
        snap = get_usage_counter().snapshot()
        metrics.set_rentcast_usage(snap.used, snap.cap)
    except Exception:  # pragma: no cover — defensive
        pass

    response.status_code = _STATUS_TO_HTTP.get(result.status, status.HTTP_200_OK)
    if result.trace_id:
        response.headers["X-Request-ID"] = result.trace_id
    return LookupResponse.from_result(result, elapsed_ms=int(elapsed * 1000))


# Spec-compliant alias: SPEC.md section 7 defines the endpoint as
# ``POST /zestimate``. We keep ``/lookup`` as the primary (documented)
# name for backwards compatibility and add this alias so both work.
router.add_api_route(
    "/zestimate",
    lookup,
    methods=["POST"],
    response_model=LookupResponse,
    dependencies=[Depends(require_api_key), Depends(rate_limit)],
    summary="Look up a Zestimate (alias for /lookup)",
    include_in_schema=False,  # avoid duplicate in OpenAPI docs
)


# ─── Health / readiness ─────────────────────────────────────────


@router.get(
    "/healthz",
    response_model=HealthResponse,
    summary="Liveness probe",
    tags=["health"],
)
async def healthz() -> HealthResponse:
    """Always 200 if the process is alive. No dependencies checked."""
    return HealthResponse(status="ok")


@router.get(
    "/readyz",
    response_model=HealthResponse,
    summary="Readiness probe",
    tags=["health"],
)
async def readyz(
    request: Request,
    settings: SettingsDep,
) -> HealthResponse:
    """Return 200 iff agent + cache are initialized and Rentcast cap not blown.

    A blown Rentcast cap is reported as `degraded` (still 200) — the agent
    still works without cross-check, so we don't page anyone.
    """
    checks: dict[str, object] = {}
    agent_ok = getattr(request.app.state, "agent", None) is not None
    checks["agent"] = "ok" if agent_ok else "missing"
    checks["cache_backend"] = settings.cache_backend

    # Circuit breaker state (if the fetcher exposes one).
    agent_inst = getattr(request.app.state, "agent", None)
    fetcher = getattr(agent_inst, "_fetcher", None)
    breaker = getattr(fetcher, "_breaker", None)
    if breaker is not None:
        checks["circuit_breaker"] = breaker.state.name.lower()

    try:
        snap = get_usage_counter().snapshot()
        checks["rentcast_used"] = snap.used
        checks["rentcast_cap"] = snap.cap
        checks["rentcast_exhausted"] = snap.exhausted
    except Exception as e:  # pragma: no cover — defensive
        checks["rentcast_error"] = str(e)

    overall = "ok" if agent_ok else "degraded"
    return HealthResponse(status=overall, checks=checks)


# ─── Version ────────────────────────────────────────────────────


@router.get(
    "/version",
    response_model=VersionResponse,
    summary="Package version",
    tags=["meta"],
)
async def version() -> VersionResponse:
    return VersionResponse(name="zestimate-agent", version=__version__)


# ─── Prometheus metrics ─────────────────────────────────────────


@router.get(
    "/metrics",
    summary="Prometheus metrics",
    tags=["meta"],
    response_class=Response,
    responses={200: {"content": {"text/plain": {}}}},
)
async def metrics_endpoint() -> Response:
    payload, content_type = metrics.render()
    return Response(content=payload, media_type=content_type)
