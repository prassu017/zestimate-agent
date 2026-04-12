"""Tests for the result cache (NullCache, MemoryCache, DiskResultCache).

Covers:
    * hit/miss accounting
    * TTL expiry (via MemoryCache with short TTL)
    * which statuses are cacheable
    * `cached=True` flag is set on read, not on write
    * cache key includes today's date
    * disk cache round-trip and eviction on decode failure
    * factory dispatch
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from zestimate_agent.cache import (
    CACHEABLE_STATUSES,
    DiskResultCache,
    MemoryCache,
    NullCache,
    _make_key,
    _serialize,
    build_cache,
)
from zestimate_agent.models import CrossCheck, ZestimateResult, ZestimateStatus


def _ok_result(value: int = 636_500) -> ZestimateResult:
    return ZestimateResult(
        status=ZestimateStatus.OK,
        value=value,
        zpid="82362438",
        matched_address="500 5th Ave W #705, Seattle, WA 98119",
        zillow_url="https://www.zillow.com/homedetails/82362438_zpid/",
        fetched_at=datetime(2026, 4, 10, tzinfo=UTC),
        fetcher="scraperapi",
        confidence=0.95,
        crosscheck=CrossCheck(
            provider="rentcast",
            estimate=640_000,
            delta_pct=0.55,
            within_tolerance=True,
        ),
    )


# ─── NullCache ──────────────────────────────────────────────────


class TestNullCache:
    def test_get_always_misses(self) -> None:
        c = NullCache()
        c.set("foo", _ok_result())
        assert c.get("foo") is None
        assert c.stats.misses == 1
        assert c.stats.hits == 0
        assert c.volume() == 0

    def test_clear_returns_zero(self) -> None:
        assert NullCache().clear() == 0


# ─── MemoryCache ────────────────────────────────────────────────


class TestMemoryCache:
    def test_round_trip_sets_cached_flag(self) -> None:
        c = MemoryCache()
        c.set("500 5th Ave W #705, Seattle, WA 98119", _ok_result())
        got = c.get("500 5th Ave W #705, Seattle, WA 98119")
        assert got is not None
        assert got.cached is True
        assert got.value == 636_500
        assert got.crosscheck is not None
        assert got.crosscheck.estimate == 640_000
        assert c.stats.hits == 1
        assert c.stats.writes == 1

    def test_lowercase_canonical_collides(self) -> None:
        """Cache keys must be case-insensitive on the address."""
        c = MemoryCache()
        c.set("500 5th Ave, Seattle, WA 98119", _ok_result())
        got = c.get("500 5TH AVE, SEATTLE, WA 98119")
        assert got is not None
        assert got.value == 636_500

    def test_ttl_expiry(self) -> None:
        c = MemoryCache(ttl_seconds=0)  # expires immediately
        c.set("addr", _ok_result())
        # Wait a sliver to cross the expiry boundary
        time.sleep(0.01)
        assert c.get("addr") is None
        assert c.stats.evictions == 1
        assert c.stats.misses == 1

    def test_miss_increments_counter(self) -> None:
        c = MemoryCache()
        assert c.get("never seen") is None
        assert c.stats.misses == 1
        assert c.stats.hits == 0

    def test_non_ok_non_stable_statuses_not_cached(self) -> None:
        c = MemoryCache()
        for status in (
            ZestimateStatus.BLOCKED,
            ZestimateStatus.ERROR,
            ZestimateStatus.AMBIGUOUS,
        ):
            r = ZestimateResult(status=status, error="x")
            c.set(f"addr-{status.value}", r)
        assert c.stats.writes == 0
        assert c.volume() == 0

    @pytest.mark.parametrize(
        "status",
        [
            ZestimateStatus.OK,
            ZestimateStatus.NO_ZESTIMATE,
            ZestimateStatus.NOT_FOUND,
        ],
    )
    def test_stable_statuses_are_cached(self, status: ZestimateStatus) -> None:
        c = MemoryCache()
        r = (
            _ok_result()
            if status == ZestimateStatus.OK
            else ZestimateResult(status=status, matched_address="x")
        )
        c.set("addr", r)
        assert c.stats.writes == 1
        assert c.get("addr") is not None

    def test_clear_returns_count_and_empties(self) -> None:
        c = MemoryCache()
        c.set("a", _ok_result())
        c.set("b", _ok_result())
        assert c.clear() == 2
        assert c.get("a") is None

    def test_cached_flag_never_persisted(self) -> None:
        """Writing a result that already has cached=True should still
        store it as cached=False, so a re-read is the authoritative flipper."""
        c = MemoryCache()
        r = _ok_result().model_copy(update={"cached": True})
        c.set("addr", r)
        # Inspect the raw serialized payload — should have cached=False
        key = _make_key("addr")
        _, raw = c._store[key]  # type: ignore[attr-defined]
        assert '"cached": false' in raw
        # But re-read via get() flips it to True
        got = c.get("addr")
        assert got is not None and got.cached is True


# ─── Key format ─────────────────────────────────────────────────


class TestKey:
    def test_contains_version_and_date(self) -> None:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        key = _make_key("500 Fifth Avenue, Seattle, WA 98119")
        assert key.startswith("v1:")
        assert key.endswith(today)
        assert "500 fifth avenue" in key

    def test_is_whitespace_tolerant(self) -> None:
        k1 = _make_key("  500 Fifth Avenue, Seattle, WA 98119  ")
        k2 = _make_key("500 Fifth Avenue, Seattle, WA 98119")
        assert k1 == k2


# ─── Disk cache ─────────────────────────────────────────────────


class TestDiskCache:
    def test_round_trip_persists_across_instances(self, tmp_path: Path) -> None:
        path = tmp_path / "db"
        c1 = DiskResultCache(path, ttl_seconds=3600)
        try:
            c1.set("addr", _ok_result())
        finally:
            c1.close()

        c2 = DiskResultCache(path, ttl_seconds=3600)
        try:
            got = c2.get("addr")
        finally:
            c2.close()

        assert got is not None
        assert got.cached is True
        assert got.value == 636_500

    def test_volume_reflects_stored_data(self, tmp_path: Path) -> None:
        c = DiskResultCache(tmp_path / "db", ttl_seconds=3600)
        try:
            assert c.volume() >= 0
            c.set("addr1", _ok_result())
            c.set("addr2", _ok_result(value=900_000))
            assert c.volume() > 0
        finally:
            c.close()

    def test_clear_empties_cache(self, tmp_path: Path) -> None:
        c = DiskResultCache(tmp_path / "db", ttl_seconds=3600)
        try:
            c.set("addr1", _ok_result())
            c.set("addr2", _ok_result(value=900_000))
            n = c.clear()
            assert n >= 2
            assert c.get("addr1") is None
        finally:
            c.close()

    def test_corrupt_payload_is_evicted(self, tmp_path: Path) -> None:
        """Poison the cache with a non-JSON entry — get() should evict and miss."""
        c = DiskResultCache(tmp_path / "db", ttl_seconds=3600)
        try:
            key = _make_key("addr")
            # Bypass our wrapper and inject junk via diskcache directly
            c._cache.set(key, "{not json", expire=3600)  # type: ignore[attr-defined]
            got = c.get("addr")
            assert got is None
            assert c.stats.evictions == 1
        finally:
            c.close()

    def test_non_string_payload_is_evicted(self, tmp_path: Path) -> None:
        c = DiskResultCache(tmp_path / "db", ttl_seconds=3600)
        try:
            key = _make_key("addr")
            c._cache.set(key, 42, expire=3600)  # type: ignore[attr-defined]
            got = c.get("addr")
            assert got is None
            assert c.stats.evictions == 1
        finally:
            c.close()

    def test_ttl_expiry_on_disk(self, tmp_path: Path) -> None:
        c = DiskResultCache(tmp_path / "db", ttl_seconds=1)
        try:
            c.set("addr", _ok_result())
            assert c.get("addr") is not None
            time.sleep(1.2)
            assert c.get("addr") is None
        finally:
            c.close()


# ─── Serializer ─────────────────────────────────────────────────


def test_serialize_always_stores_cached_false() -> None:
    r = _ok_result().model_copy(update={"cached": True})
    raw = _serialize(r)
    assert '"cached": false' in raw


# ─── Factory ────────────────────────────────────────────────────


class TestBuildCache:
    def test_backend_none_returns_null(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CACHE_BACKEND", "none")
        from zestimate_agent.config import reset_settings_cache

        reset_settings_cache()
        cache = build_cache()
        try:
            assert isinstance(cache, NullCache)
        finally:
            cache.close()
            reset_settings_cache()

    def test_backend_memory_returns_memory(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CACHE_BACKEND", "memory")
        from zestimate_agent.config import reset_settings_cache

        reset_settings_cache()
        cache = build_cache()
        try:
            assert isinstance(cache, MemoryCache)
        finally:
            cache.close()
            reset_settings_cache()

    def test_backend_sqlite_returns_disk(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("CACHE_BACKEND", "sqlite")
        monkeypatch.setenv("CACHE_PATH", str(tmp_path / "db"))
        from zestimate_agent.config import reset_settings_cache

        reset_settings_cache()
        cache = build_cache()
        try:
            assert isinstance(cache, DiskResultCache)
        finally:
            cache.close()
            reset_settings_cache()


# ─── CACHEABLE_STATUSES constant ────────────────────────────────


def test_cacheable_statuses_sanity() -> None:
    assert ZestimateStatus.OK in CACHEABLE_STATUSES
    assert ZestimateStatus.NO_ZESTIMATE in CACHEABLE_STATUSES
    assert ZestimateStatus.NOT_FOUND in CACHEABLE_STATUSES
    assert ZestimateStatus.BLOCKED not in CACHEABLE_STATUSES
    assert ZestimateStatus.ERROR not in CACHEABLE_STATUSES
    assert ZestimateStatus.AMBIGUOUS not in CACHEABLE_STATUSES
