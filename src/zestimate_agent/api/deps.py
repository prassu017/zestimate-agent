"""FastAPI dependency providers.

The agent is a singleton pinned to `app.state.agent` by the lifespan context
(see `app.py`). Route handlers receive it via `Depends(get_agent)`, which keeps
them testable — tests can override the dependency with a fake agent via
`app.dependency_overrides`.
"""

from __future__ import annotations

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

    When `settings.api_key` is unset, this dependency is a no-op — convenient
    for local dev. Set `ZESTIMATE_API_KEY=...` in the environment to lock down
    the API.
    """
    expected = settings.api_key_value
    if expected is None:
        return  # open API
    if not provided or provided != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid or missing X-API-Key",
            headers={"WWW-Authenticate": "X-API-Key"},
        )
