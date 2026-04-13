"""Fetcher chain — try primary, fall back to secondary on failure.

Provides automatic failover between fetcher implementations. The typical
setup is unblocker (fast, cheap) as primary and Playwright (slow, resilient)
as fallback::

    chain = FetcherChain(
        primary=ScraperAPIFetcher("key"),
        fallback=PlaywrightFetcher(),
    )
    result = await chain.fetch(url)  # tries primary, falls back on error

The chain only falls back on ``FetchBlockedError`` and ``CircuitOpenError``
— errors that indicate the upstream can't serve the request. Other errors
(timeout, parse, etc.) propagate immediately because retrying with a
different fetcher wouldn't help.
"""

from __future__ import annotations

from zestimate_agent.errors import FetchBlockedError
from zestimate_agent.fetch.base import Fetcher
from zestimate_agent.fetch.circuit_breaker import CircuitOpenError
from zestimate_agent.logging import get_logger
from zestimate_agent.models import FetchResult

log = get_logger(__name__)

# Errors that trigger fallback to the secondary fetcher.
_FAILOVER_ERRORS = (FetchBlockedError, CircuitOpenError)


class FetcherChain:
    """Two-fetcher chain with automatic failover.

    Conforms to the ``Fetcher`` Protocol so it's a drop-in replacement
    anywhere a single fetcher is expected.
    """

    name = "chain"

    def __init__(self, primary: Fetcher, fallback: Fetcher) -> None:
        self._primary = primary
        self._fallback = fallback

    async def fetch(self, url: str) -> FetchResult:
        try:
            return await self._primary.fetch(url)
        except _FAILOVER_ERRORS as e:
            log.warning(
                "primary fetcher failed, falling back",
                primary=self._primary.name,
                fallback=self._fallback.name,
                error=str(e),
            )
            return await self._fallback.fetch(url)

    async def aclose(self) -> None:
        await self._primary.aclose()
        await self._fallback.aclose()
