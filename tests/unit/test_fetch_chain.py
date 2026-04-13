"""Tests for the fetcher chain — automatic failover."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from zestimate_agent.errors import FetchBlockedError, FetchError
from zestimate_agent.fetch.base import Fetcher
from zestimate_agent.fetch.chain import FetcherChain
from zestimate_agent.fetch.circuit_breaker import CircuitOpenError
from zestimate_agent.models import FetchResult


class _FakeFetcher:
    """Minimal Fetcher that returns a canned result or raises."""

    name: str = "fake"

    def __init__(self, *, html: str = "<html>ok</html>", error: Exception | None = None) -> None:
        self._html = html
        self._error = error
        self.call_count = 0

    async def fetch(self, url: str) -> FetchResult:
        self.call_count += 1
        if self._error is not None:
            raise self._error
        return FetchResult(
            html=self._html,
            status=200,
            final_url=url,
            fetcher=self.name,
            fetched_at=datetime.now(UTC),
        )

    async def aclose(self) -> None:
        pass


class TestFetcherChain:
    @pytest.mark.asyncio
    async def test_primary_succeeds_no_fallback(self) -> None:
        primary = _FakeFetcher(html="<html>primary</html>")
        fallback = _FakeFetcher(html="<html>fallback</html>")
        chain = FetcherChain(primary, fallback)

        result = await chain.fetch("https://example.com")
        assert "primary" in result.html
        assert primary.call_count == 1
        assert fallback.call_count == 0

    @pytest.mark.asyncio
    async def test_falls_back_on_blocked(self) -> None:
        primary = _FakeFetcher(error=FetchBlockedError("blocked"))
        fallback = _FakeFetcher(html="<html>fallback</html>")
        chain = FetcherChain(primary, fallback)

        result = await chain.fetch("https://example.com")
        assert "fallback" in result.html
        assert primary.call_count == 1
        assert fallback.call_count == 1

    @pytest.mark.asyncio
    async def test_falls_back_on_circuit_open(self) -> None:
        primary = _FakeFetcher(error=CircuitOpenError("open"))
        fallback = _FakeFetcher(html="<html>fallback</html>")
        chain = FetcherChain(primary, fallback)

        result = await chain.fetch("https://example.com")
        assert "fallback" in result.html

    @pytest.mark.asyncio
    async def test_non_failover_error_propagates(self) -> None:
        """Errors that aren't FetchBlockedError or CircuitOpenError propagate."""
        primary = _FakeFetcher(error=FetchError("generic 500"))
        fallback = _FakeFetcher(html="<html>fallback</html>")
        chain = FetcherChain(primary, fallback)

        with pytest.raises(FetchError, match="generic 500"):
            await chain.fetch("https://example.com")
        # Fallback should NOT have been called.
        assert fallback.call_count == 0

    @pytest.mark.asyncio
    async def test_both_fail_raises_fallback_error(self) -> None:
        """If both primary and fallback fail, the fallback's error propagates."""
        primary = _FakeFetcher(error=FetchBlockedError("primary blocked"))
        fallback = _FakeFetcher(error=FetchBlockedError("fallback blocked"))
        chain = FetcherChain(primary, fallback)

        with pytest.raises(FetchBlockedError, match="fallback blocked"):
            await chain.fetch("https://example.com")

    @pytest.mark.asyncio
    async def test_aclose_closes_both(self) -> None:
        primary = _FakeFetcher()
        fallback = _FakeFetcher()
        chain = FetcherChain(primary, fallback)
        await chain.aclose()
        # If aclose didn't raise, both were closed.

    def test_conforms_to_fetcher_protocol(self) -> None:
        primary = _FakeFetcher()
        fallback = _FakeFetcher()
        chain = FetcherChain(primary, fallback)
        assert isinstance(chain, Fetcher)
        assert chain.name == "chain"
