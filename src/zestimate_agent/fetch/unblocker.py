"""Unblocker-based fetchers — ScraperAPI, ZenRows, and Bright Data adapters.

All conform to `fetch.base.Fetcher` so the orchestrator can pick one at
runtime via config. Adding a new provider is ~20 lines and doesn't touch
the rest of the pipeline.

Credit-cost knobs per provider (as of 2026-04):

* ScraperAPI: `render=true` + `premium=true` ≈ 25 credits per Zillow call
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
    """Shared behavior for all unblocker-style fetchers."""

    name = "unblocker"
    api_url: str = ""

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        settings = get_settings()
        self._api_key = api_key
        self._timeout = timeout or settings.http_timeout_seconds
        self._client = client
        self._owns_client = client is None

    def _params(self, url: str) -> dict[str, Any]:  # pragma: no cover - abstract
        raise NotImplementedError

    async def fetch(self, url: str) -> FetchResult:
        return await self._fetch_with_retry(url)

    async def aclose(self) -> None:
        if self._client is not None and self._owns_client:
            await self._client.aclose()

    # ─── Retry wrapper ──────────────────────────────────────────

    async def _fetch_with_retry(self, url: str) -> FetchResult:
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

        return await _attempt()

    async def _fetch_once(self, url: str) -> FetchResult:
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
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
        finally:
            if self._owns_client:
                await client.aclose()


# ─── Concrete providers ─────────────────────────────────────────


class ScraperAPIFetcher(UnblockerFetcherBase):
    """ScraperAPI unblocker.

    Default mode: `render=true` + `premium=true` (~25 credits per Zillow call).

    Some protected properties (high-profile commercial, Apple HQ, etc.) need
    `ultra_premium=true` (~30 credits). Rather than paying that upfront for
    every request, we upgrade *on retry* if the first attempt returns a 500
    with ScraperAPI's "protected domain" message.
    """

    name = "scraperapi"
    api_url = "http://api.scraperapi.com/"

    # When set, subsequent attempts on this URL will use ultra_premium.
    # Class-level so the upgrade is sticky across retries and instances.
    _ULTRA_URLS: ClassVar[set[str]] = set()

    def _params(self, url: str) -> dict[str, Any]:
        params = {
            "api_key": self._api_key,
            "url": url,
            "render": "true",
            "premium": "true",
            "country_code": "us",
        }
        if url in self._ULTRA_URLS:
            params["ultra_premium"] = "true"
        return params

    async def _fetch_once(self, url: str) -> FetchResult:
        try:
            return await super()._fetch_once(url)
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
