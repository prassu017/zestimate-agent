"""Tests for the sitemap pre-warmer."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from zestimate_agent.agent import ZestimateAgent
from zestimate_agent.config import Settings
from zestimate_agent.models import (
    FetchResult,
    NormalizedAddress,
    ResolvedProperty,
)
from zestimate_agent.prewarm import PrewarmStats, prewarm_from_addresses

# ─── Fakes ────────────────────────────────────────────────────


class _FakeNormalizer:
    def normalize(self, raw: str) -> NormalizedAddress:
        return NormalizedAddress(
            raw=raw, street="1 Test", city="Test", state="CA",
            zip="99999", canonical=raw,
        )


class _FakeResolver:
    async def resolve(self, addr):
        return ResolvedProperty(
            zpid="1", url="https://zillow.com/homedetails/1_zpid/",
            matched_address=addr.canonical, match_confidence=1.0,
        )
    async def aclose(self):
        pass


class _OkFetcher:
    """Returns HTML that the parser can extract a Zestimate from."""
    name = "test"

    def _html(self, value: int = 500_000) -> str:
        import json
        prop = {"zpid": 1, "zestimate": value, "streetAddress": "1 Test",
                "address": {"streetAddress": "1 Test", "city": "Test", "state": "CA", "zipcode": "99999"}}
        gdp = {'ForSalePriorityQuery{"zpid":1}': {"property": prop}}
        nd = {"props": {"pageProps": {"componentProps": {"gdpClientCache": json.dumps(gdp)}}}}
        pad = "<!-- " + ("x" * 800) + " -->"
        return f'<html><body>{pad}<script id="__NEXT_DATA__" type="application/json">{json.dumps(nd)}</script></body></html>'

    async def fetch(self, url: str) -> FetchResult:
        return FetchResult(
            html=self._html(), status=200, final_url=url,
            fetcher="test", fetched_at=datetime.now(UTC),
        )
    async def aclose(self):
        pass


def _settings() -> Settings:
    return Settings(
        cache_backend="none",
        crosscheck_provider="none",
        unblocker_api_key="fake",
        playwright_enabled=False,
    )


class TestPrewarmFromAddresses:
    @pytest.mark.asyncio
    async def test_prewarms_addresses(self) -> None:
        agent = ZestimateAgent(
            _settings(),
            normalizer=_FakeNormalizer(),
            resolver=_FakeResolver(),
            fetcher=_OkFetcher(),
        )
        stats = await prewarm_from_addresses(
            ["123 Main St", "456 Oak Ave"],
            agent,
            concurrency=2,
            use_cache=False,
        )
        assert stats.total == 2
        assert stats.ok == 2
        assert stats.errors == 0

    @pytest.mark.asyncio
    async def test_empty_list(self) -> None:
        agent = ZestimateAgent(
            _settings(),
            normalizer=_FakeNormalizer(),
            resolver=_FakeResolver(),
            fetcher=_OkFetcher(),
        )
        stats = await prewarm_from_addresses([], agent, use_cache=False)
        assert stats.total == 0
        assert stats.ok == 0


class TestPrewarmStats:
    def test_defaults(self) -> None:
        stats = PrewarmStats()
        assert stats.total == 0
        assert stats.ok == 0
        assert stats.cached == 0
        assert stats.errors == 0
