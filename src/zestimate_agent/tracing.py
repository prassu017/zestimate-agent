"""Optional OpenTelemetry integration — per-pipeline-stage spans.

When `opentelemetry-api` is installed, every pipeline stage (normalize,
resolve, fetch, parse, validate) gets its own span under a parent
"zestimate.lookup" trace. When the SDK is *not* installed, the module
exports no-op helpers so the rest of the codebase can call them
unconditionally without import guards.

Usage in agent.py::

    from zestimate_agent.tracing import tracer, start_span

    with start_span("normalize", trace_id=trace_id) as span:
        normalized = ...
        span.set_attribute("address.canonical", normalized.canonical)
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

try:
    from opentelemetry import trace
    from opentelemetry.trace import StatusCode

    tracer = trace.get_tracer("zestimate_agent")
    _HAS_OTEL = True
except ImportError:
    tracer = None  # type: ignore[assignment]
    _HAS_OTEL = False


class _NoOpSpan:
    """Duck-typed span for when OTel is not installed."""

    def set_attribute(self, key: str, value: Any) -> None:
        pass

    def set_status(self, status: Any, description: str | None = None) -> None:
        pass

    def record_exception(self, exception: BaseException) -> None:
        pass


@contextmanager
def start_span(
    name: str,
    *,
    trace_id: str | None = None,
    attributes: dict[str, Any] | None = None,
) -> Generator[Any, None, None]:
    """Start a child span under the current context.

    If OTel is not installed, yields a no-op span so callers don't need
    to branch.
    """
    if not _HAS_OTEL or tracer is None:
        yield _NoOpSpan()
        return

    attrs: dict[str, Any] = {}
    if trace_id:
        attrs["zestimate.trace_id"] = trace_id
    if attributes:
        attrs.update(attributes)

    with tracer.start_as_current_span(f"zestimate.{name}", attributes=attrs) as span:
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(StatusCode.ERROR, str(exc))
            raise


def is_enabled() -> bool:
    """Return True when OTel tracing is available."""
    return _HAS_OTEL
