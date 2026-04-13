"""Tests for the circuit breaker."""

from __future__ import annotations

import time

import pytest

from zestimate_agent.fetch.circuit_breaker import (
    CircuitBreaker,
    CircuitOpenError,
    CircuitState,
)


class TestCircuitBreakerStates:
    """State machine transitions."""

    def test_starts_closed(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=3, recovery_timeout=10.0)
        assert cb.state == CircuitState.CLOSED
        assert cb.allow_request() is True

    def test_stays_closed_below_threshold(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=3, recovery_timeout=10.0)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED
        assert cb.allow_request() is True

    def test_opens_at_threshold(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=3, recovery_timeout=10.0)
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.allow_request() is False

    def test_success_resets_failure_count(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=3, recovery_timeout=10.0)
        cb.record_failure()
        cb.record_failure()
        cb.record_success()
        # After success, counter resets — need 3 more consecutive failures.
        cb.record_failure()
        assert cb.state == CircuitState.CLOSED

    def test_transitions_to_half_open_after_timeout(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=2, recovery_timeout=0.1)
        cb.record_failure()
        cb.record_failure()
        assert cb.state == CircuitState.OPEN

        # Wait for recovery timeout.
        time.sleep(0.15)
        assert cb.allow_request() is True
        assert cb.state == CircuitState.HALF_OPEN

    def test_half_open_success_closes(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=2, recovery_timeout=0.1)
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.15)
        cb.allow_request()  # transitions to HALF_OPEN
        assert cb.state == CircuitState.HALF_OPEN

        cb.record_success()
        assert cb.state == CircuitState.CLOSED
        assert cb.allow_request() is True

    def test_half_open_failure_reopens(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=2, recovery_timeout=0.1)
        cb.record_failure()
        cb.record_failure()
        time.sleep(0.15)
        cb.allow_request()  # transitions to HALF_OPEN

        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        assert cb.allow_request() is False

    def test_reset_returns_to_closed(self) -> None:
        cb = CircuitBreaker("test", failure_threshold=1, recovery_timeout=999.0)
        cb.record_failure()
        assert cb.state == CircuitState.OPEN
        cb.reset()
        assert cb.state == CircuitState.CLOSED
        assert cb.allow_request() is True


class TestCircuitOpenError:
    def test_is_fetch_error(self) -> None:
        from zestimate_agent.errors import FetchError

        assert issubclass(CircuitOpenError, FetchError)


class TestPrometheusGauge:
    def test_gauge_updates_on_state_change(self) -> None:
        """Gauge should reflect the integer state value."""
        from prometheus_client import REGISTRY

        cb = CircuitBreaker("test_prom", failure_threshold=1, recovery_timeout=999.0)
        val = REGISTRY.get_sample_value(
            "zestimate_circuit_breaker_state", {"provider": "test_prom"}
        )
        assert val == 0.0  # CLOSED

        cb.record_failure()
        val = REGISTRY.get_sample_value(
            "zestimate_circuit_breaker_state", {"provider": "test_prom"}
        )
        assert val == 1.0  # OPEN

        cb.reset()
        val = REGISTRY.get_sample_value(
            "zestimate_circuit_breaker_state", {"provider": "test_prom"}
        )
        assert val == 0.0  # CLOSED


class TestFetcherIntegration:
    """Circuit breaker wired into the ScraperAPI fetcher."""

    @pytest.mark.asyncio
    async def test_circuit_open_raises_without_hitting_network(self) -> None:
        import httpx

        from zestimate_agent.fetch.unblocker import ScraperAPIFetcher

        calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            return httpx.Response(200, text="<html></html>")

        breaker = CircuitBreaker("test_sapi", failure_threshold=1, recovery_timeout=999.0)
        breaker.record_failure()  # trip it open
        assert breaker.state == CircuitState.OPEN

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            f = ScraperAPIFetcher("key", client=client, circuit_breaker=breaker)
            with pytest.raises(CircuitOpenError):
                await f.fetch("https://www.zillow.com/homedetails/1_zpid/")

        # No network call should have been made.
        assert len(calls) == 0
