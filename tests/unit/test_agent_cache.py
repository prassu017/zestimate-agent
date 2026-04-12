"""Agent-level cache integration — proves cache reads short-circuit the pipeline
and cache writes happen for all stable statuses.

Uses stub normalizer/resolver/fetcher so we never touch the network. The
point is to verify the *wiring*, not the individual components.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from zestimate_agent.agent import ZestimateAgent
from zestimate_agent.cache import MemoryCache
from zestimate_agent.config import get_settings
from zestimate_agent.errors import FetchBlockedError, PropertyNotFoundError
from zestimate_agent.models import (
    FetchResult,
    NormalizedAddress,
    ResolvedProperty,
    ZestimateStatus,
)

# ─── Fakes ──────────────────────────────────────────────────────


class _FakeNormalizer:
    def normalize(self, raw: str) -> NormalizedAddress:
        return NormalizedAddress(
            raw=raw,
            street="500 5th Ave W #705",
            city="Seattle",
            state="WA",
            zip="98119",
            canonical="500 5th Ave W #705, Seattle, WA 98119",
            parse_confidence=1.0,
        )


class _FakeResolver:
    def __init__(self, *, fail_not_found: bool = False) -> None:
        self.calls = 0
        self.fail_not_found = fail_not_found

    async def resolve(self, normalized: NormalizedAddress) -> ResolvedProperty:
        self.calls += 1
        if self.fail_not_found:
            raise PropertyNotFoundError("no match")
        return ResolvedProperty(
            zpid="82362438",
            url="https://www.zillow.com/homedetails/82362438_zpid/",
            matched_address="500 5th Ave W #705, Seattle, WA 98119",
            match_confidence=1.0,
        )

    async def aclose(self) -> None:
        return None


class _FakeFetcher:
    name = "fake"

    def __init__(self, html: str, *, blocked: bool = False) -> None:
        self._html = html
        self._blocked = blocked
        self.calls = 0

    async def fetch(self, url: str) -> FetchResult:
        self.calls += 1
        if self._blocked:
            raise FetchBlockedError("captcha")
        return FetchResult(
            html=self._html,
            status=200,
            final_url=url,
            fetcher=self.name,
            fetched_at=datetime(2026, 4, 10, tzinfo=UTC),
            elapsed_ms=50,
        )

    async def aclose(self) -> None:
        return None


def _zillow_html(value: int = 636_500) -> str:
    """Tiny synthetic Zillow page with a parseable gdpClientCache."""
    import json

    gdp = {
        'ForSalePriorityQuery{"zpid":82362438}': {
            "property": {
                "zpid": 82362438,
                "zestimate": value,
                "streetAddress": "500 5th Ave W #705",
                "address": {
                    "streetAddress": "500 5th Ave W #705",
                    "city": "Seattle",
                    "state": "WA",
                    "zipcode": "98119",
                },
            }
        }
    }
    nd = {
        "props": {
            "pageProps": {
                "componentProps": {
                    "gdpClientCache": json.dumps(gdp),
                }
            }
        }
    }
    pad = "<!-- " + ("x" * 800) + " -->"
    return (
        f"<html><body>{pad}"
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script>'
        "</body></html>"
    )


# ─── Helper ─────────────────────────────────────────────────────


def _build_agent(
    *,
    resolver: _FakeResolver | None = None,
    fetcher: _FakeFetcher | None = None,
    cache: MemoryCache | None = None,
) -> ZestimateAgent:
    return ZestimateAgent(
        get_settings(),
        normalizer=_FakeNormalizer(),  # type: ignore[arg-type]
        resolver=resolver or _FakeResolver(),  # type: ignore[arg-type]
        fetcher=fetcher or _FakeFetcher(_zillow_html()),  # type: ignore[arg-type]
        crosschecker=None,
        cache=cache or MemoryCache(),
    )


# ─── Tests ──────────────────────────────────────────────────────


class TestAgentCache:
    @pytest.mark.asyncio
    async def test_cache_miss_then_hit_short_circuits_fetch(self) -> None:
        cache = MemoryCache()
        resolver = _FakeResolver()
        fetcher = _FakeFetcher(_zillow_html())
        agent = _build_agent(resolver=resolver, fetcher=fetcher, cache=cache)

        try:
            r1 = await agent.aget("500 5th Ave W #705, Seattle, WA 98119")
            r2 = await agent.aget("500 5th Ave W #705, Seattle, WA 98119")
        finally:
            await agent.aclose()

        assert r1.status == ZestimateStatus.OK
        assert r1.cached is False
        assert r2.status == ZestimateStatus.OK
        assert r2.cached is True
        assert r2.value == r1.value
        # Crucially: resolver + fetcher only called once
        assert resolver.calls == 1
        assert fetcher.calls == 1
        assert cache.stats.hits == 1
        assert cache.stats.writes == 1

    @pytest.mark.asyncio
    async def test_trace_id_refreshes_on_cache_hit(self) -> None:
        cache = MemoryCache()
        agent = _build_agent(cache=cache)
        try:
            r1 = await agent.aget("500 5th Ave W #705, Seattle, WA 98119")
            r2 = await agent.aget("500 5th Ave W #705, Seattle, WA 98119")
        finally:
            await agent.aclose()

        assert r1.trace_id != r2.trace_id
        assert r2.cached is True

    @pytest.mark.asyncio
    async def test_use_cache_false_bypasses_read_and_write(self) -> None:
        cache = MemoryCache()
        resolver = _FakeResolver()
        fetcher = _FakeFetcher(_zillow_html())
        agent = _build_agent(resolver=resolver, fetcher=fetcher, cache=cache)
        try:
            await agent.aget("addr", use_cache=False)
            await agent.aget("addr", use_cache=False)
        finally:
            await agent.aclose()
        assert resolver.calls == 2
        assert fetcher.calls == 2
        assert cache.stats.writes == 0
        assert cache.stats.hits == 0

    @pytest.mark.asyncio
    async def test_not_found_is_cached(self) -> None:
        cache = MemoryCache()
        resolver = _FakeResolver(fail_not_found=True)
        agent = _build_agent(resolver=resolver, cache=cache)
        try:
            r1 = await agent.aget("addr")
            r2 = await agent.aget("addr")
        finally:
            await agent.aclose()
        assert r1.status == ZestimateStatus.NOT_FOUND
        assert r2.status == ZestimateStatus.NOT_FOUND
        assert r2.cached is True
        assert resolver.calls == 1  # second call short-circuited

    @pytest.mark.asyncio
    async def test_blocked_is_not_cached(self) -> None:
        cache = MemoryCache()
        resolver = _FakeResolver()
        fetcher = _FakeFetcher(_zillow_html(), blocked=True)
        agent = _build_agent(resolver=resolver, fetcher=fetcher, cache=cache)
        try:
            r1 = await agent.aget("addr")
            r2 = await agent.aget("addr")
        finally:
            await agent.aclose()
        assert r1.status == ZestimateStatus.BLOCKED
        assert r2.status == ZestimateStatus.BLOCKED
        # Both calls executed the full pipeline — no caching of blocked state
        assert resolver.calls == 2
        assert fetcher.calls == 2
        assert cache.stats.writes == 0
