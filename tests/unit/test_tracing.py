"""Tests for the OpenTelemetry tracing integration."""

from __future__ import annotations

from zestimate_agent.tracing import _NoOpSpan, is_enabled, start_span


class TestNoOpSpan:
    def test_set_attribute_noop(self) -> None:
        span = _NoOpSpan()
        span.set_attribute("key", "value")  # should not raise

    def test_set_status_noop(self) -> None:
        span = _NoOpSpan()
        span.set_status("OK")

    def test_record_exception_noop(self) -> None:
        span = _NoOpSpan()
        span.record_exception(RuntimeError("test"))


class TestStartSpan:
    def test_yields_span_without_otel(self) -> None:
        """When OTel SDK is not configured, start_span yields a no-op."""
        with start_span("test_stage", trace_id="abc-123") as span:
            # Should work without raising
            span.set_attribute("test.key", 42)

    def test_exception_propagates(self) -> None:
        """Exceptions inside the span context still propagate."""
        import pytest

        with pytest.raises(ValueError, match="boom"), start_span("test_stage") as _span:
            raise ValueError("boom")

    def test_is_enabled_returns_bool(self) -> None:
        result = is_enabled()
        assert isinstance(result, bool)
