"""Tests for the Playwright fetcher — unit tests with mocked browser."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from zestimate_agent.errors import FetchBlockedError, FetchError
from zestimate_agent.fetch.circuit_breaker import CircuitBreaker, CircuitOpenError, CircuitState
from zestimate_agent.fetch.playwright import PlaywrightFetcher, _looks_blocked

# ─── Block detection ───────────────────────────────────────────


class TestBlockDetection:
    def test_empty_html_is_blocked(self) -> None:
        assert _looks_blocked("") is True

    def test_short_html_is_blocked(self) -> None:
        assert _looks_blocked("<html>short</html>") is True

    def test_captcha_is_blocked(self) -> None:
        html = "<html><body>" + ("x" * 1000) + "Press & Hold to confirm you are human</body></html>"
        assert _looks_blocked(html) is True

    def test_normal_html_is_not_blocked(self) -> None:
        html = "<html><body>" + ("x" * 1000) + "normal content</body></html>"
        assert _looks_blocked(html) is False


# ─── Fetcher unit tests ───────────────────────────────────────


class TestPlaywrightFetcher:
    @pytest.mark.asyncio
    async def test_circuit_breaker_open_skips_browser(self) -> None:
        breaker = CircuitBreaker("test_pw", failure_threshold=1, recovery_timeout=999.0)
        breaker.record_failure()
        assert breaker.state == CircuitState.OPEN

        fetcher = PlaywrightFetcher(circuit_breaker=breaker)
        with pytest.raises(CircuitOpenError):
            await fetcher.fetch("https://www.zillow.com/homedetails/1_zpid/")

    @pytest.mark.asyncio
    async def test_missing_playwright_raises_fetch_error(self) -> None:
        """When playwright is not installed, _ensure_browser raises FetchError."""
        fetcher = PlaywrightFetcher()

        with (
            patch(
                "zestimate_agent.fetch.playwright.PlaywrightFetcher._ensure_browser",
                side_effect=FetchError("Playwright is not installed"),
            ),
            pytest.raises(FetchError, match="not installed"),
        ):
            await fetcher.fetch("https://www.zillow.com/homedetails/1_zpid/")

    @pytest.mark.asyncio
    async def test_blocked_page_raises_blocked_error(self) -> None:
        """If the rendered page looks blocked, raise FetchBlockedError."""
        blocked_html = (
            "<html><body>" + ("x" * 1000)
            + "Press & Hold to confirm you are human</body></html>"
        )

        mock_response = MagicMock()
        mock_response.status = 200

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock(return_value=mock_response)
        mock_page.wait_for_timeout = AsyncMock()
        mock_page.content = AsyncMock(return_value=blocked_html)
        mock_page.url = "https://www.zillow.com/homedetails/1_zpid/"
        mock_page.close = AsyncMock()

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)

        fetcher = PlaywrightFetcher()
        fetcher._context = mock_context

        with pytest.raises(FetchBlockedError):
            await fetcher.fetch("https://www.zillow.com/homedetails/1_zpid/")

    @pytest.mark.asyncio
    async def test_happy_path_returns_fetch_result(self) -> None:
        """Successful fetch returns a FetchResult."""
        good_html = (
            "<html><body>" + ("x" * 1000)
            + '<script id="__NEXT_DATA__">{}</script></body></html>'
        )

        mock_response = MagicMock()
        mock_response.status = 200

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock(return_value=mock_response)
        mock_page.wait_for_timeout = AsyncMock()
        mock_page.content = AsyncMock(return_value=good_html)
        mock_page.url = "https://www.zillow.com/homedetails/1_zpid/"
        mock_page.close = AsyncMock()

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)

        fetcher = PlaywrightFetcher()
        fetcher._context = mock_context

        result = await fetcher.fetch("https://www.zillow.com/homedetails/1_zpid/")
        assert result.fetcher == "playwright"
        assert result.status == 200
        assert "__NEXT_DATA__" in result.html

    @pytest.mark.asyncio
    async def test_403_raises_blocked_error(self) -> None:
        mock_response = MagicMock()
        mock_response.status = 403

        mock_page = AsyncMock()
        mock_page.goto = AsyncMock(return_value=mock_response)
        mock_page.close = AsyncMock()

        mock_context = AsyncMock()
        mock_context.new_page = AsyncMock(return_value=mock_page)

        fetcher = PlaywrightFetcher()
        fetcher._context = mock_context

        with pytest.raises(FetchBlockedError, match="403"):
            await fetcher.fetch("https://www.zillow.com/homedetails/1_zpid/")

    @pytest.mark.asyncio
    async def test_aclose_resets_state(self) -> None:
        fetcher = PlaywrightFetcher()
        # Nothing to close when never opened — should not raise.
        await fetcher.aclose()
        assert fetcher._context is None
        assert fetcher._browser is None
        assert fetcher._pw is None
