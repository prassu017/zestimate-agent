"""Integration test: ZestimateAgent.aget() NEVER raises.

This is the agent's core invariant. We inject failures at every pipeline
stage and verify the agent always returns a ZestimateResult with an
appropriate status — never throws an exception to the caller.

Each test constructs the agent with a fake that blows up at one specific
layer, then asserts we get a structured result back.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from zestimate_agent.agent import ZestimateAgent
from zestimate_agent.config import Settings
from zestimate_agent.errors import (
    AmbiguousAddressError,
    FetchBlockedError,
    FetchError,
    FetchTimeoutError,
    NormalizationError,
    PropertyNotFoundError,
    ResolverError,
)
from zestimate_agent.models import (
    FetchResult,
    NormalizedAddress,
    ResolvedProperty,
    ZestimateResult,
    ZestimateStatus,
)

# ─── Helpers ───────────────────────────────────────────────────


def _settings() -> Settings:
    return Settings(
        cache_backend="none",
        crosscheck_provider="none",
        unblocker_api_key="fake",
        playwright_enabled=False,
    )


def _fake_address() -> NormalizedAddress:
    return NormalizedAddress(
        raw="123 Test St, Seattle, WA 98101",
        street="123 Test St",
        city="Seattle",
        state="WA",
        zip="98101",
        canonical="123 Test St, Seattle, WA 98101",
    )


def _fake_resolved() -> ResolvedProperty:
    return ResolvedProperty(
        zpid="12345",
        url="https://www.zillow.com/homedetails/12345_zpid/",
        matched_address="123 Test St, Seattle, WA 98101",
        match_confidence=1.0,
    )


def _fake_fetch_result() -> FetchResult:
    return FetchResult(
        html="<html><body>test</body></html>",
        status=200,
        final_url="https://www.zillow.com/homedetails/12345_zpid/",
        fetcher="test",
        fetched_at=datetime.now(UTC),
    )


# ─── Fake components that raise at specific stages ─────────────


class _BrokenNormalizer:
    def normalize(self, raw: str) -> NormalizedAddress:
        raise NormalizationError("bad address")


class _GoodNormalizer:
    def normalize(self, raw: str) -> NormalizedAddress:
        return _fake_address()


class _NotFoundResolver:
    async def resolve(self, addr: NormalizedAddress) -> ResolvedProperty:
        raise PropertyNotFoundError("no match")

    async def aclose(self) -> None:
        pass


class _AmbiguousResolver:
    async def resolve(self, addr: NormalizedAddress) -> ResolvedProperty:
        raise AmbiguousAddressError("multiple matches")

    async def aclose(self) -> None:
        pass


class _ResolverError:
    async def resolve(self, addr: NormalizedAddress) -> ResolvedProperty:
        raise ResolverError("resolver exploded")

    async def aclose(self) -> None:
        pass


class _GoodResolver:
    async def resolve(self, addr: NormalizedAddress) -> ResolvedProperty:
        return _fake_resolved()

    async def aclose(self) -> None:
        pass


class _BlockedFetcher:
    name = "test"

    async def fetch(self, url: str) -> FetchResult:
        raise FetchBlockedError("captcha")

    async def aclose(self) -> None:
        pass


class _TimeoutFetcher:
    name = "test"

    async def fetch(self, url: str) -> FetchResult:
        raise FetchTimeoutError("timed out")

    async def aclose(self) -> None:
        pass


class _FetchErrorFetcher:
    name = "test"

    async def fetch(self, url: str) -> FetchResult:
        raise FetchError("500 from upstream")

    async def aclose(self) -> None:
        pass


class _UnexpectedErrorFetcher:
    name = "test"

    async def fetch(self, url: str) -> FetchResult:
        raise RuntimeError("completely unexpected")

    async def aclose(self) -> None:
        pass


class _GoodFetcherBadParse:
    """Returns HTML that the parser can't extract a Zestimate from."""

    name = "test"

    async def fetch(self, url: str) -> FetchResult:
        return FetchResult(
            html="<html><body>" + ("x" * 1000) + "</body></html>",
            status=200,
            final_url=url,
            fetcher="test",
            fetched_at=datetime.now(UTC),
        )

    async def aclose(self) -> None:
        pass


# ─── Tests ─────────────────────────────────────────────────────


class TestNeverRaises:
    """Every test asserts that aget() returns a result, never raises."""

    @pytest.mark.asyncio
    async def test_normalization_error(self) -> None:
        agent = ZestimateAgent(
            _settings(),
            normalizer=_BrokenNormalizer(),  # type: ignore[arg-type]
        )
        result = await agent.aget("garbage input!!!", use_cache=False)
        assert isinstance(result, ZestimateResult)
        # NormalizationError maps to NOT_FOUND or ERROR depending on the agent.
        assert result.status in {ZestimateStatus.ERROR, ZestimateStatus.NOT_FOUND}
        assert result.ok is False

    @pytest.mark.asyncio
    async def test_property_not_found(self) -> None:
        agent = ZestimateAgent(
            _settings(),
            normalizer=_GoodNormalizer(),  # type: ignore[arg-type]
            resolver=_NotFoundResolver(),  # type: ignore[arg-type]
        )
        result = await agent.aget("123 Nonexistent St, Nowhere, XY 00000", use_cache=False)
        assert result.status == ZestimateStatus.NOT_FOUND

    @pytest.mark.asyncio
    async def test_ambiguous_address(self) -> None:
        agent = ZestimateAgent(
            _settings(),
            normalizer=_GoodNormalizer(),  # type: ignore[arg-type]
            resolver=_AmbiguousResolver(),  # type: ignore[arg-type]
        )
        result = await agent.aget("Main St", use_cache=False)
        assert result.status == ZestimateStatus.AMBIGUOUS

    @pytest.mark.asyncio
    async def test_resolver_error(self) -> None:
        agent = ZestimateAgent(
            _settings(),
            normalizer=_GoodNormalizer(),  # type: ignore[arg-type]
            resolver=_ResolverError(),  # type: ignore[arg-type]
        )
        result = await agent.aget("123 Test St, Seattle, WA 98101", use_cache=False)
        assert result.status == ZestimateStatus.ERROR

    @pytest.mark.asyncio
    async def test_fetch_blocked(self) -> None:
        agent = ZestimateAgent(
            _settings(),
            normalizer=_GoodNormalizer(),  # type: ignore[arg-type]
            resolver=_GoodResolver(),  # type: ignore[arg-type]
            fetcher=_BlockedFetcher(),  # type: ignore[arg-type]
        )
        result = await agent.aget("123 Test St, Seattle, WA 98101", use_cache=False)
        assert result.status == ZestimateStatus.BLOCKED

    @pytest.mark.asyncio
    async def test_fetch_timeout(self) -> None:
        agent = ZestimateAgent(
            _settings(),
            normalizer=_GoodNormalizer(),  # type: ignore[arg-type]
            resolver=_GoodResolver(),  # type: ignore[arg-type]
            fetcher=_TimeoutFetcher(),  # type: ignore[arg-type]
        )
        result = await agent.aget("123 Test St, Seattle, WA 98101", use_cache=False)
        # FetchTimeoutError is a FetchError, mapped to ERROR.
        assert result.status in {ZestimateStatus.ERROR, ZestimateStatus.BLOCKED}

    @pytest.mark.asyncio
    async def test_fetch_generic_error(self) -> None:
        agent = ZestimateAgent(
            _settings(),
            normalizer=_GoodNormalizer(),  # type: ignore[arg-type]
            resolver=_GoodResolver(),  # type: ignore[arg-type]
            fetcher=_FetchErrorFetcher(),  # type: ignore[arg-type]
        )
        result = await agent.aget("123 Test St, Seattle, WA 98101", use_cache=False)
        assert result.status == ZestimateStatus.ERROR

    @pytest.mark.asyncio
    async def test_unexpected_runtime_error(self) -> None:
        agent = ZestimateAgent(
            _settings(),
            normalizer=_GoodNormalizer(),  # type: ignore[arg-type]
            resolver=_GoodResolver(),  # type: ignore[arg-type]
            fetcher=_UnexpectedErrorFetcher(),  # type: ignore[arg-type]
        )
        result = await agent.aget("123 Test St, Seattle, WA 98101", use_cache=False)
        assert result.status == ZestimateStatus.ERROR
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_parse_error(self) -> None:
        agent = ZestimateAgent(
            _settings(),
            normalizer=_GoodNormalizer(),  # type: ignore[arg-type]
            resolver=_GoodResolver(),  # type: ignore[arg-type]
            fetcher=_GoodFetcherBadParse(),  # type: ignore[arg-type]
        )
        result = await agent.aget("123 Test St, Seattle, WA 98101", use_cache=False)
        # Parser can't find Zestimate → ERROR or related status.
        assert result.status in {
            ZestimateStatus.ERROR,
            ZestimateStatus.BLOCKED,
            ZestimateStatus.NO_ZESTIMATE,
        }
        assert result.ok is False

    @pytest.mark.asyncio
    async def test_all_failures_return_result_type(self) -> None:
        """Meta-test: every failure case above returns a ZestimateResult."""
        failure_fetchers = [
            _BlockedFetcher,
            _TimeoutFetcher,
            _FetchErrorFetcher,
            _UnexpectedErrorFetcher,
            _GoodFetcherBadParse,
        ]
        for fetcher_cls in failure_fetchers:
            agent = ZestimateAgent(
                _settings(),
                normalizer=_GoodNormalizer(),  # type: ignore[arg-type]
                resolver=_GoodResolver(),  # type: ignore[arg-type]
                fetcher=fetcher_cls(),  # type: ignore[arg-type]
            )
            result = await agent.aget("123 Test St, Seattle, WA 98101", use_cache=False)
            assert isinstance(result, ZestimateResult), (
                f"{fetcher_cls.__name__} did not return ZestimateResult"
            )
            assert result.ok is False
