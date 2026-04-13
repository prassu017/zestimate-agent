"""FastAPI dependency providers.

The agent is a singleton pinned to `app.state.agent` by the lifespan context
(see `app.py`). Route handlers receive it via `Depends(get_agent)`, which keeps
them testable — tests can override the dependency with a fake agent via
`app.dependency_overrides`.
"""

from __future__ import annotations

import hmac
import time
from collections import defaultdict
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader

from zestimate_agent.config import Settings, get_settings

if TYPE_CHECKING:
    from zestimate_agent.agent import ZestimateAgent


# ─── Agent ──────────────────────────────────────────────────────


def get_agent(request: Request) -> ZestimateAgent:
    """Return the process-singleton `ZestimateAgent` built by the lifespan."""
    agent = getattr(request.app.state, "agent", None)
    if agent is None:  # pragma: no cover — startup invariant
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="agent not initialized",
        )
    return agent  # type: ignore[no-any-return]


def get_app_settings(request: Request) -> Settings:
    """Return the Settings pinned to the app (fallback to the module singleton)."""
    settings = getattr(request.app.state, "settings", None)
    if settings is None:
        return get_settings()
    return settings  # type: ignore[no-any-return]


# ─── API key auth ───────────────────────────────────────────────

# `auto_error=False` so we can emit a friendlier 401 when the key is missing.
_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# Annotated aliases keep `Depends(...)` out of function-default position
# (satisfies ruff B008) and match FastAPI's modern idiom.
ApiKeyDep = Annotated[str | None, Depends(_api_key_header)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]


def require_api_key(
    provided: ApiKeyDep,
    settings: SettingsDep,
) -> None:
    """Enforce `X-API-Key` when `settings.api_key` is configured.

    When `settings.api_key` is unset, this dependency is a no-op -- convenient
    for local dev. Set `ZESTIMATE_API_KEY=...` in the environment to lock down
    the API.

    Uses `hmac.compare_digest` for constant-time comparison to prevent
    timing-based side-channel attacks.
    """
    expected = settings.api_key_value
    if expected is None:
        return  # open API
    if not provided or not hmac.compare_digest(provided.encode(), expected.encode()):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing X-API-Key",
            headers={"WWW-Authenticate": "X-API-Key"},
        )


# ─── Rate limiter ──────────────────────────────────────────────

# Simple in-memory sliding-window rate limiter. Production deployments
# behind a load balancer should additionally use a distributed limiter
# (Redis + lua), but this protects single-node deployments and the
# Vercel demo from credit-exhaustion attacks.

# {client_ip: [(timestamp, ...), ...]}
_RATE_BUCKETS: dict[str, list[float]] = defaultdict(list)

# Defaults: 10 requests per 60-second window per IP.
_RATE_LIMIT = 10
_RATE_WINDOW = 60.0


def rate_limit(request: Request) -> None:
    """Enforce per-IP request rate limiting on expensive endpoints.

    Raises HTTP 429 when a client exceeds the configured window. The
    window slides on each request, pruning expired timestamps.
    """
    client_ip = request.client.host if request.client else "unknown"
    now = time.monotonic()
    bucket = _RATE_BUCKETS[client_ip]

    # Prune expired entries.
    cutoff = now - _RATE_WINDOW
    _RATE_BUCKETS[client_ip] = bucket = [t for t in bucket if t > cutoff]

    if len(bucket) >= _RATE_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"rate limit exceeded: max {_RATE_LIMIT} lookups per {int(_RATE_WINDOW)}s",
            headers={"Retry-After": str(int(_RATE_WINDOW))},
        )

    bucket.append(now)


def reset_rate_limiter() -> None:
    """Clear all rate-limit buckets. Used in tests."""
    _RATE_BUCKETS.clear()


# ─── Signed URL verification ─────────────────────────────────


def verify_signed_url(
    request: Request,
    settings: SettingsDep,
) -> None:
    """Validate HMAC-signed URL params when ``SIGNED_URL_SECRET`` is set.

    When the secret is unset, this is a no-op. When set, every request
    must include ``?sig=<hex>&exp=<unix_ts>`` query params.
    """
    from zestimate_agent.api.signed_url import _compute_signature

    secret = settings.signed_url_secret
    if secret is None:
        return

    sig = request.query_params.get("sig")
    exp = request.query_params.get("exp")

    if not sig or not exp:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="missing sig or exp query parameters",
        )

    try:
        expiry_ts = int(exp)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid exp parameter",
        ) from e

    now = int(time.time())
    if expiry_ts + 60 < now:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="signed URL expired",
        )

    expected = _compute_signature(secret, request.method, request.url.path, exp)
    if not hmac.compare_digest(sig, expected):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="invalid signature",
        )
