"""Playwright-based fetcher — headless Chrome fallback for Zillow.

Uses a real Chromium browser to render Zillow property pages. This is the
**fallback** fetcher — slower and more resource-intensive than the unblocker
providers, but capable of handling pages that resist proxy-based scraping.

Requires the ``playwright`` optional dependency group::

    pip install -e ".[playwright]"
    playwright install chromium

Design:
- Launches a persistent browser context on first fetch (lazy init).
- Reuses the context across calls (connection pooling equivalent).
- Applies ``playwright-stealth`` patches to avoid bot detection.
- Obeys the same ``Fetcher`` Protocol as unblocker fetchers.
- Integrates the circuit breaker for fast-fail on repeated failures.

When to use:
- Set ``FETCHER_PRIMARY=playwright`` in env, or
- Wire as a fallback in a future "chain of fetchers" pattern.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from zestimate_agent.config import get_settings
from zestimate_agent.errors import FetchBlockedError, FetchError, FetchTimeoutError
from zestimate_agent.fetch.circuit_breaker import (
    BREAKER_TRIP_ERRORS,
    CircuitBreaker,
    CircuitOpenError,
)
from zestimate_agent.logging import get_logger
from zestimate_agent.models import FetchResult

if TYPE_CHECKING:
    pass

log = get_logger(__name__)

# Block-page markers (shared with unblocker, but kept local for independence).
_BLOCK_MARKERS = (
    "Press & Hold to confirm you are",
    "Access to this page has been denied",
    "Please verify you are a human",
    '<div id="px-captcha"',
    "Pardon Our Interruption",
)


def _looks_blocked(html: str) -> bool:
    if not html or len(html) < 500:
        return True
    lowered = html.lower()
    return any(m.lower() in lowered for m in _BLOCK_MARKERS)


class PlaywrightFetcher:
    """Headless Chromium fetcher via Playwright.

    Lazily launches a browser on first ``fetch()`` call. The browser
    persists for the lifetime of the fetcher (call ``aclose()`` to shut
    it down).
    """

    name = "playwright"

    def __init__(
        self,
        *,
        headless: bool | None = None,
        proxy_url: str | None = None,
        timeout: float | None = None,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        settings = get_settings()
        self._headless = headless if headless is not None else settings.playwright_headless
        self._proxy_url = proxy_url or settings.playwright_proxy_url
        self._timeout_ms = int((timeout or settings.http_timeout_seconds) * 1000)
        self._breaker = circuit_breaker or CircuitBreaker(
            self.name,
            failure_threshold=settings.circuit_breaker_failure_threshold,
            recovery_timeout=settings.circuit_breaker_recovery_timeout,
        )

        # Lazy-initialized browser state.
        self._pw: object | None = None  # playwright context manager
        self._browser: object | None = None
        self._context: object | None = None

    async def _ensure_browser(self) -> object:
        """Launch the browser if not already running. Returns the browser context."""
        if self._context is not None:
            return self._context

        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise FetchError(
                "Playwright is not installed. Run: pip install -e '.[playwright]' && playwright install chromium"
            ) from e

        self._pw = await async_playwright().__aenter__()  # type: ignore[union-attr]

        launch_kwargs: dict[str, object] = {"headless": self._headless}
        if self._proxy_url:
            launch_kwargs["proxy"] = {"server": self._proxy_url}

        self._browser = await self._pw.chromium.launch(**launch_kwargs)  # type: ignore[union-attr]

        self._context = await self._browser.new_context(  # type: ignore[union-attr]
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            locale="en-US",
        )

        # Apply stealth patches if available.
        try:
            from playwright_stealth import stealth_async  # type: ignore[import-untyped]

            await stealth_async(self._context)  # type: ignore[arg-type]
        except ImportError:
            log.debug("playwright-stealth not installed, skipping stealth patches")

        return self._context

    async def fetch(self, url: str) -> FetchResult:
        """Fetch a URL using headless Chromium."""
        if not self._breaker.allow_request():
            raise CircuitOpenError(
                f"{self.name} circuit breaker is OPEN — failing fast"
            )

        try:
            result = await self._fetch_once(url)
            self._breaker.record_success()
            return result
        except BREAKER_TRIP_ERRORS:
            self._breaker.record_failure()
            raise

    async def _fetch_once(self, url: str) -> FetchResult:
        context = await self._ensure_browser()
        page = await context.new_page()  # type: ignore[union-attr]

        start = time.monotonic()
        try:
            log.debug("playwright fetch", url=url)
            response = await page.goto(url, wait_until="networkidle", timeout=self._timeout_ms)

            if response is None:
                raise FetchError("playwright: no response from page.goto()")

            status = response.status
            if status == 403 or status == 429:
                raise FetchBlockedError(f"playwright returned {status}")

            # Wait briefly for any late-arriving JS hydration.
            await page.wait_for_timeout(1000)
            html = await page.content()
            elapsed_ms = int((time.monotonic() - start) * 1000)

            if _looks_blocked(html):
                raise FetchBlockedError("playwright returned blocked page")

            return FetchResult(
                html=html,
                status=status,
                final_url=page.url,
                fetcher=self.name,
                elapsed_ms=elapsed_ms,
            )
        except TimeoutError as e:
            raise FetchTimeoutError(f"playwright timeout: {e}") from e
        finally:
            await page.close()

    async def aclose(self) -> None:
        """Shut down the browser and Playwright process."""
        if self._context is not None:
            await self._context.close()  # type: ignore[union-attr]
            self._context = None
        if self._browser is not None:
            await self._browser.close()  # type: ignore[union-attr]
            self._browser = None
        if self._pw is not None:
            await self._pw.__aexit__(None, None, None)  # type: ignore[union-attr]
            self._pw = None


def build_playwright_fetcher() -> PlaywrightFetcher:
    """Instantiate a Playwright fetcher from settings."""
    return PlaywrightFetcher()
