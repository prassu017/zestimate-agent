"""Tests for RedisResultCache — uses fakeredis for zero-infra testing."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from zestimate_agent.cache import (
    RedisResultCache,
    _make_key,
)
from zestimate_agent.models import ZestimateResult, ZestimateStatus


def _ok_result(value: int = 500_000) -> ZestimateResult:
    return ZestimateResult(
        status=ZestimateStatus.OK,
        value=value,
        matched_address="1 Test St, Test, CA 99999",
        zpid="123",
        confidence=0.95,
    )


def _error_result() -> ZestimateResult:
    return ZestimateResult(
        status=ZestimateStatus.ERROR,
        error="transient",
    )


@pytest.fixture()
def redis_cache():
    """Build a RedisResultCache backed by fakeredis."""
    try:
        import fakeredis
    except ImportError:
        pytest.skip("fakeredis not installed")

    with patch("redis.from_url") as mock_from_url:
        fake = fakeredis.FakeRedis(decode_responses=True)
        mock_from_url.return_value = fake
        cache = RedisResultCache("redis://fake:6379/0", ttl_seconds=3600)
    yield cache
    cache.close()


class TestRedisResultCache:
    def test_miss_returns_none(self, redis_cache: RedisResultCache) -> None:
        assert redis_cache.get("unknown address") is None
        assert redis_cache.stats.misses == 1

    def test_set_and_get_roundtrip(self, redis_cache: RedisResultCache) -> None:
        result = _ok_result()
        redis_cache.set("1 Test St, Test, CA 99999", result)
        assert redis_cache.stats.writes == 1

        got = redis_cache.get("1 Test St, Test, CA 99999")
        assert got is not None
        assert got.value == 500_000
        assert got.status == ZestimateStatus.OK
        assert got.cached is True
        assert redis_cache.stats.hits == 1

    def test_non_cacheable_status_not_stored(self, redis_cache: RedisResultCache) -> None:
        redis_cache.set("err addr", _error_result())
        assert redis_cache.stats.writes == 0
        assert redis_cache.get("err addr") is None

    def test_clear_removes_entries(self, redis_cache: RedisResultCache) -> None:
        redis_cache.set("addr1", _ok_result(100_000))
        redis_cache.set("addr2", _ok_result(200_000))
        assert redis_cache.stats.writes == 2

        cleared = redis_cache.clear()
        assert cleared == 2
        assert redis_cache.get("addr1") is None
        assert redis_cache.get("addr2") is None

    def test_volume_returns_bytes(self, redis_cache: RedisResultCache) -> None:
        redis_cache.set("addr1", _ok_result())
        vol = redis_cache.volume()
        assert vol > 0

    def test_corrupt_value_evicted(self, redis_cache: RedisResultCache) -> None:
        """If the stored JSON is corrupt, get() evicts and returns None."""
        key = _make_key("corrupt addr")
        redis_cache._r.setex(key, 3600, "not-valid-json{{{")

        got = redis_cache.get("corrupt addr")
        assert got is None
        assert redis_cache.stats.evictions == 1
        assert redis_cache.stats.misses == 1
        # Key should be deleted
        assert redis_cache._r.get(key) is None

    def test_conforms_to_protocol(self, redis_cache: RedisResultCache) -> None:
        from zestimate_agent.cache import ResultCache

        assert isinstance(redis_cache, ResultCache)
