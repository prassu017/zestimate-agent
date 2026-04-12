"""Prometheus metrics for the HTTP API.

All metric objects live at module level so they're process-global (Prometheus
requires this). Use `observe_lookup()` from route handlers — do **not**
increment counters directly from business logic.

Exposed metrics
---------------

- `zestimate_lookups_total{status}`         — counter of lookup outcomes
- `zestimate_lookup_duration_seconds`       — histogram of end-to-end latency
- `zestimate_cache_events_total{event}`     — hit/miss/write
- `zestimate_crosscheck_total{outcome}`     — ok/skipped/error
- `zestimate_rentcast_usage`                — gauge, updated per-request
- `zestimate_rentcast_cap`                  — gauge, constant
- `zestimate_http_requests_total{path,method,code}` — raw request counter
- `zestimate_http_request_duration_seconds{path,method}` — histogram

Histogram buckets are chosen for a lookup that's fast when cached (< 100ms)
and slow when live (1s - 30s).
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from zestimate_agent.models import ZestimateResult

# ─── Registry ───────────────────────────────────────────────────

# Default to the global registry so multiple imports don't re-register.
# Tests can pass a custom registry via `build_metrics(registry=...)`.
_REGISTRY: CollectorRegistry = REGISTRY

# ─── Metric definitions ─────────────────────────────────────────

LOOKUP_COUNTER = Counter(
    "zestimate_lookups_total",
    "Total Zestimate lookups by terminal status.",
    labelnames=("status",),
    registry=_REGISTRY,
)

LOOKUP_LATENCY = Histogram(
    "zestimate_lookup_duration_seconds",
    "End-to-end latency of /lookup, seconds.",
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 20.0, 30.0, 60.0),
    registry=_REGISTRY,
)

CACHE_EVENTS = Counter(
    "zestimate_cache_events_total",
    "Cache events during lookups.",
    labelnames=("event",),  # hit | miss | write
    registry=_REGISTRY,
)

CROSSCHECK_COUNTER = Counter(
    "zestimate_crosscheck_total",
    "Cross-check outcomes during lookups.",
    labelnames=("outcome",),  # ok | skipped | error
    registry=_REGISTRY,
)

RENTCAST_USAGE = Gauge(
    "zestimate_rentcast_usage",
    "Current month's Rentcast requests used.",
    registry=_REGISTRY,
)

RENTCAST_CAP = Gauge(
    "zestimate_rentcast_cap",
    "Configured monthly Rentcast request cap.",
    registry=_REGISTRY,
)

HTTP_REQUESTS = Counter(
    "zestimate_http_requests_total",
    "Raw HTTP request counter.",
    labelnames=("path", "method", "code"),
    registry=_REGISTRY,
)

HTTP_LATENCY = Histogram(
    "zestimate_http_request_duration_seconds",
    "HTTP request latency, seconds.",
    labelnames=("path", "method"),
    buckets=(0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
    registry=_REGISTRY,
)


# ─── Helpers ────────────────────────────────────────────────────


def observe_lookup(result: ZestimateResult, elapsed_seconds: float) -> None:
    """Record one completed lookup into the metrics backend.

    Called from the route handler *after* `agent.aget()` returns.
    Never raises — metric errors must not break the request.
    """
    try:
        LOOKUP_COUNTER.labels(status=result.status.value).inc()
        LOOKUP_LATENCY.observe(elapsed_seconds)

        if result.cached:
            CACHE_EVENTS.labels(event="hit").inc()
        else:
            CACHE_EVENTS.labels(event="miss").inc()

        if result.crosscheck is not None:
            if result.crosscheck.skipped:
                CROSSCHECK_COUNTER.labels(outcome="skipped").inc()
            elif result.crosscheck.estimate is not None:
                CROSSCHECK_COUNTER.labels(outcome="ok").inc()
            else:
                CROSSCHECK_COUNTER.labels(outcome="error").inc()
    except Exception:  # pragma: no cover — defensive only
        pass


def set_rentcast_usage(used: int, cap: int) -> None:
    """Push the current Rentcast usage/cap into the gauges."""
    try:
        RENTCAST_USAGE.set(used)
        RENTCAST_CAP.set(cap)
    except Exception:  # pragma: no cover
        pass


def render() -> tuple[bytes, str]:
    """Render the metrics in Prometheus exposition format.

    Returns `(payload_bytes, content_type)` suitable for a raw Response.
    """
    return generate_latest(_REGISTRY), CONTENT_TYPE_LATEST
