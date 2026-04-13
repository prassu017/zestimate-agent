"""Circuit breaker for external fetcher calls.

Implements the classic three-state pattern (CLOSED -> OPEN -> HALF_OPEN) to
fail fast when an upstream provider (ScraperAPI, ZenRows, etc.) is down,
rather than burning retries and blocking callers for 30s each.

State transitions::

    CLOSED  --[failure_threshold reached]--> OPEN
    OPEN    --[recovery_timeout elapsed]---> HALF_OPEN
    HALF_OPEN --[success]-------------------> CLOSED
    HALF_OPEN --[failure]-------------------> OPEN

Thread-safety: uses `asyncio.Lock` for async contexts. The breaker is
per-fetcher-instance, so each provider gets its own state.

Prometheus integration: optional gauge tracks the current state (0=closed,
1=open, 2=half_open) per provider name.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from enum import IntEnum

from zestimate_agent.errors import FetchBlockedError, FetchError, FetchTimeoutError
from zestimate_agent.logging import get_logger

log = get_logger(__name__)

# ─── Prometheus (optional) ─────────────────────────────────────

try:
    from prometheus_client import Gauge

    CIRCUIT_STATE_GAUGE = Gauge(
        "zestimate_circuit_breaker_state",
        "Circuit breaker state: 0=closed, 1=open, 2=half_open.",
        labelnames=("provider",),
    )
except Exception:  # pragma: no cover
    CIRCUIT_STATE_GAUGE = None  # type: ignore[assignment]


# ─── Circuit states ────────────────────────────────────────────


class CircuitState(IntEnum):
    CLOSED = 0
    OPEN = 1
    HALF_OPEN = 2


# ─── Exceptions ────────────────────────────────────────────────

# Errors that count as "upstream failure" for the breaker.
BREAKER_TRIP_ERRORS = (
    FetchBlockedError,
    FetchTimeoutError,
)


class CircuitOpenError(FetchError):
    """Raised when the circuit is open and the call is rejected."""


# ─── Breaker ───────────────────────────────────────────────────


class CircuitBreaker:
    """Async-safe circuit breaker for a single upstream provider.

    Parameters
    ----------
    provider_name:
        Label for logging and Prometheus metrics.
    failure_threshold:
        Number of consecutive failures before opening the circuit.
    recovery_timeout:
        Seconds to wait in OPEN state before transitioning to HALF_OPEN.
    """

    def __init__(
        self,
        provider_name: str,
        *,
        failure_threshold: int = 5,
        recovery_timeout: float = 30.0,
    ) -> None:
        self.provider_name = provider_name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout

        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time: float = 0.0
        self._lock = asyncio.Lock()
        self._set_gauge()

    # ─── Public interface ──────────────────────────────────────

    @property
    def state(self) -> CircuitState:
        return self._state

    def record_success(self) -> None:
        """Call after a successful request."""
        if self._state == CircuitState.HALF_OPEN:
            log.info(
                "circuit breaker closing (recovered)",
                provider=self.provider_name,
            )
            self._state = CircuitState.CLOSED
            self._failure_count = 0
            self._set_gauge()
        elif self._state == CircuitState.CLOSED:
            self._failure_count = 0

    def record_failure(self) -> None:
        """Call after a failed request (one that should trip the breaker)."""
        self._failure_count += 1
        self._last_failure_time = time.monotonic()

        if self._state == CircuitState.HALF_OPEN:
            log.warning(
                "circuit breaker re-opening (half_open probe failed)",
                provider=self.provider_name,
            )
            self._state = CircuitState.OPEN
            self._set_gauge()
        elif (
            self._state == CircuitState.CLOSED
            and self._failure_count >= self.failure_threshold
        ):
            log.warning(
                "circuit breaker opening",
                provider=self.provider_name,
                failures=self._failure_count,
            )
            self._state = CircuitState.OPEN
            self._set_gauge()

    def allow_request(self) -> bool:
        """Return True if a request should be allowed through."""
        if self._state == CircuitState.CLOSED:
            return True

        if self._state == CircuitState.OPEN:
            elapsed = time.monotonic() - self._last_failure_time
            if elapsed >= self.recovery_timeout:
                log.info(
                    "circuit breaker transitioning to half_open",
                    provider=self.provider_name,
                    elapsed_s=round(elapsed, 1),
                )
                self._state = CircuitState.HALF_OPEN
                self._set_gauge()
                return True
            return False

        # HALF_OPEN: allow exactly one probe request.
        return True

    def reset(self) -> None:
        """Force-reset to CLOSED (for tests)."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._last_failure_time = 0.0
        self._set_gauge()

    # ─── Internal ──────────────────────────────────────────────

    def _set_gauge(self) -> None:
        if CIRCUIT_STATE_GAUGE is not None:
            with contextlib.suppress(Exception):
                CIRCUIT_STATE_GAUGE.labels(provider=self.provider_name).set(
                    self._state.value
                )
