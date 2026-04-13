"""Unblocker-based fetchers — ScraperAPI, ZenRows, and Bright Data adapters.

All conform to `fetch.base.Fetcher` so the orchestrator can pick one at
runtime via config. Adding a new provider is ~20 lines and doesn't touch
the rest of the pipeline.

Credit-cost knobs per provider (as of 2026-04):

* ScraperAPI: `premium=true` only ≈ 10 credits per Zillow call (fast path,
  ~2-3s). Upgrades to `render=true` (~25 credits, ~15-25s) only when the
  fetched HTML lacks the Next.js `__NEXT_DATA__` hydration blob — which
  should be almost never, because Zillow property pages server-side render
  it into the initial response.
* ZenRows: `js_render=true` + `premium_proxy=true` ≈ 25 credits
* Bright Data Web Unlocker: flat rate

All requests are retried on transient errors via tenacity.
"""

from __future__ import annotations

import time
from typing import Any, ClassVar

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from zestimate_agent.config import get_settings
from zestimate_agent.errors import FetchBlockedError, FetchError, FetchTimeoutError
from zestimate_agent.fetch.circuit_breaker import (
    BREAKER_TRIP_ERRORS,
    CircuitBreaker,
    CircuitOpenError,
)
from zestimate_agent.logging import get_logger
from zestimate_agent.models import FetchResult

log = get_logger(__name__)


# ─── Block-detection helpers ────────────────────────────────────

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


# ─── Base class ─────────────────────────────────────────────────


class UnblockerFetcherBase:
    """Shared behavior for all unblocker-style fetchers.

    Connection pooling: when no external client is injected, the fetcher
    lazily creates a shared ``httpx.AsyncClient`` on first use and reuses
    it across calls, avoiding a fresh TLS handshake per fetch.
    """

    name = "unblocker"
    api_url: str = ""

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float | None = None,
        client: httpx.AsyncClient | None = None,
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        settings = get_settings()
        self._api_key = api_key
        self._timeout = timeout or settings.http_timeout_seconds
        self._ext_client = client
        self._own_client: httpx.AsyncClient | None = None
        self._breaker = circuit_breaker or CircuitBreaker(
            self.name,
            failure_threshold=settings.circuit_breaker_failure_threshold,
            recovery_timeout=settings.circuit_breaker_recovery_timeout,
        )

    def _params(self, url: str) -> dict[str, Any]:  # pragma: no cover - abstract
        raise NotImplementedError

    async def fetch(self, url: str) -> FetchResult:
        return await self._fetch_with_retry(url)

    def _get_client(self) -> httpx.AsyncClient:
        if self._ext_client is not None:
            return self._ext_client
        if self._own_client is None:
            self._own_client = httpx.AsyncClient(timeout=self._timeout)
        return self._own_client

    async def aclose(self) -> None:
        if self._own_client is not None:
            await self._own_client.aclose()
            self._own_client = None

    # ─── Retry wrapper ──────────────────────────────────────────

    async def _fetch_with_retry(self, url: str) -> FetchResult:
        if not self._breaker.allow_request():
            raise CircuitOpenError(
                f"{self.name} circuit breaker is OPEN — failing fast"
            )

        settings = get_settings()

        @retry(
            stop=stop_after_attempt(settings.http_max_retries),
            wait=wait_exponential(
                multiplier=settings.http_backoff_base_seconds,
                max=30,
            ),
            retry=retry_if_exception_type(
                (httpx.TransportError, httpx.ReadTimeout, FetchTimeoutError, FetchBlockedError)
            ),
            reraise=True,
        )
        async def _attempt() -> FetchResult:
            return await self._fetch_once(url)

        try:
            result = await _attempt()
            self._breaker.record_success()
            return result
        except BREAKER_TRIP_ERRORS:
            self._breaker.record_failure()
            raise

    async def _fetch_once(self, url: str) -> FetchResult:
        client = self._get_client()
        start = time.monotonic()
        try:
            log.debug("unblocker fetch", provider=self.name, url=url)
            r = await client.get(self.api_url, params=self._params(url))
            elapsed_ms = int((time.monotonic() - start) * 1000)

            if r.status_code == 408 or r.status_code == 504:
                raise FetchTimeoutError(f"{self.name} returned {r.status_code}")
            if r.status_code == 403 or r.status_code == 429:
                raise FetchBlockedError(
                    f"{self.name} returned {r.status_code} (likely rate limited)"
                )
            if r.status_code >= 400:
                raise FetchError(f"{self.name} returned {r.status_code}: {r.text[:200]}")

            html = r.text
            if _looks_blocked(html):
                raise FetchBlockedError(f"{self.name} returned blocked page")

            return FetchResult(
                html=html,
                status=r.status_code,
                final_url=url,
                fetcher=self.name,
                elapsed_ms=elapsed_ms,
            )
        except httpx.ReadTimeout as e:
            raise FetchTimeoutError(str(e)) from e


# ─── Concrete providers ─────────────────────────────────────────


class ScraperAPIFetcher(UnblockerFetcherBase):
    """ScraperAPI unblocker.

    Default mode: `premium=true` (~10 credits per Zillow call, ~2-3s wall).
    Zillow property pages are server-side-rendered Next.js, so the
    `__NEXT_DATA__` hydration blob ships in the initial HTML with no JS
    needed. Skipping `render=true` cuts latency ~10x and credit cost ~60%.

    Two sticky upgrades on retry:

    * `render=true` — if the fetched HTML lacks `__NEXT_DATA__`, we re-fetch
      with JS rendering enabled (~25 credits, ~15-25s). This is the safety
      net for pages that don't SSR the hydration blob.
    * `ultra_premium=true` — some protected properties (Apple HQ, etc.)
      need ultra_premium (~30 credits). We upgrade on a "protected domain"
      500 response.

    Both upgrades are class-level sets so the decision is sticky across
    retries and subsequent calls to the same URL.
    """

    name = "scraperapi"
    api_url = "http://api.scraperapi.com/"

    # URLs that previously needed ultra_premium — sticky across instances.
    _ULTRA_URLS: ClassVar[set[str]] = set()
    # URLs that previously needed JS render (no `__NEXT_DATA__` without it).
    _RENDER_URLS: ClassVar[set[str]] = set()

    def _params(self, url: str) -> dict[str, Any]:
        params = {
            "api_key": self._api_key,
            "url": url,
            "premium": "true",
            "country_code": "us",
        }
        if url in self._RENDER_URLS:
            params["render"] = "true"
        if url in self._ULTRA_URLS:
            params["ultra_premium"] = "true"
        return params

    async def _fetch_once(self, url: str) -> FetchResult:
        try:
            result = await super()._fetch_once(url)
        except FetchError as e:
            msg = str(e).lower()
            if (
                url not in self._ULTRA_URLS
                and (
                    "protected domain" in msg
                    or "ultra_premium" in msg
                    or " 500" in msg
                )
            ):
                log.info("scraperapi upgrading to ultra_premium", url=url)
                self._ULTRA_URLS.add(url)
                return await super()._fetch_once(url)
            raise

        # Happy-path feedback loop: if the raw HTML didn't include the
        # Next.js hydration blob, the no-render fast path probably didn't
        # work for this URL. Upgrade to render=true and try once more.
        # Only applies to Zillow property pages, so scope the check.
        if (
            url not in self._RENDER_URLS
            and "/homedetails/" in url
            and "__NEXT_DATA__" not in result.html
        ):
            log.info(
                "scraperapi upgrading to render=true (no __NEXT_DATA__)",
                url=url,
            )
            self._RENDER_URLS.add(url)
            return await super()._fetch_once(url)

        return result


class ZenRowsFetcher(UnblockerFetcherBase):
    """ZenRows unblocker. Configured with js_render + premium_proxy for Zillow."""

    name = "zenrows"
    api_url = "https://api.zenrows.com/v1/"

    def _params(self, url: str) -> dict[str, Any]:
        return {
            "apikey": self._api_key,
            "url": url,
            "js_render": "true",
            "premium_proxy": "true",
            "proxy_country": "us",
        }


class BrightDataFetcher(UnblockerFetcherBase):
    """Bright Data Web Unlocker (adapter stub — not our primary)."""

    name = "brightdata"
    api_url = "https://brightdata.com/"  # placeholder; real URL uses customer zone

    def _params(self, url: str) -> dict[str, Any]:
        return {"url": url, "token": self._api_key}


# ─── Factory ────────────────────────────────────────────────────


_PROVIDERS = {
    "scraperapi": ScraperAPIFetcher,
    "zenrows": ZenRowsFetcher,
    "brightdata": BrightDataFetcher,
}


def build_unblocker_fetcher() -> UnblockerFetcherBase:
    """Instantiate the configured unblocker fetcher from settings."""
    settings = get_settings()
    key = settings.unblocker_key
    if not key:
        raise FetchError(
            "UNBLOCKER_API_KEY is not set — cannot build unblocker fetcher"
        )
    cls = _PROVIDERS.get(settings.unblocker_provider)
    if cls is None:
        raise FetchError(f"unknown unblocker provider: {settings.unblocker_provider}")
    return cls(key)
