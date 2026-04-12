"""Result cache — daily partitioned, TTL-bounded key-value store.

Why cache at the ZestimateResult level (not at fetch/HTML level):
    * The whole point is to avoid burning ScraperAPI credits *and* Rentcast
      requests on duplicate lookups. Caching the final result short-circuits
      both in one shot.
    * HTML is large (~1MB) and Zillow churns the DOM daily. Caching parsed
      values is tiny and schema-stable.

Key layout:
    v1:{canonical_address}:{YYYY-MM-DD}

* `v1` — schema version prefix. Bump to invalidate all entries on a
  breaking model change without touching the DB file.
* `{canonical}` — normalized, deterministic form from `Normalizer`.
* `{YYYY-MM-DD}` — forces a daily refresh even if TTL were extended,
  because Zestimates update overnight. With TTL=6h (default) the date
  partition is belt-and-suspenders.

Cacheable statuses (only):
    OK            — the happy path; biggest cost saver
    NO_ZESTIMATE  — Zillow genuinely has no Zestimate for this property (stable)
    NOT_FOUND     — address doesn't resolve to a Zillow property (stable-ish;
                    short TTL catches the edge case where Zillow indexes it later)

We never cache BLOCKED / ERROR / AMBIGUOUS because they're retry-worthy —
the next call might succeed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from zestimate_agent.config import get_settings
from zestimate_agent.logging import get_logger
from zestimate_agent.models import ZestimateResult, ZestimateStatus

log = get_logger(__name__)

CACHE_SCHEMA_VERSION = "v1"
CACHEABLE_STATUSES: frozenset[ZestimateStatus] = frozenset(
    {
        ZestimateStatus.OK,
        ZestimateStatus.NO_ZESTIMATE,
        ZestimateStatus.NOT_FOUND,
    }
)


# ─── Cache stats ────────────────────────────────────────────────


class CacheStats:
    """Lightweight mutable counter bag. Human-readable via __str__."""

    __slots__ = ("evictions", "hits", "misses", "writes")

    def __init__(self) -> None:
        self.hits = 0
        self.misses = 0
        self.writes = 0
        self.evictions = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "writes": self.writes,
            "evictions": self.evictions,
        }


# ─── Protocol ───────────────────────────────────────────────────


@runtime_checkable
class ResultCache(Protocol):
    """Minimal interface every cache backend conforms to."""

    stats: CacheStats

    def get(self, canonical: str) -> ZestimateResult | None: ...
    def set(self, canonical: str, result: ZestimateResult) -> None: ...
    def clear(self) -> int: ...
    def volume(self) -> int: ...
    def close(self) -> None: ...


# ─── Key builder ────────────────────────────────────────────────


def _make_key(canonical: str) -> str:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    # Lowercase the address so "500 5TH Ave" and "500 5th ave" collide.
    return f"{CACHE_SCHEMA_VERSION}:{canonical.lower().strip()}:{today}"


# ─── Null cache (for CACHE_BACKEND=none / tests) ────────────────


class NullCache:
    """No-op cache. Every `get` misses, every `set` is a no-op."""

    def __init__(self) -> None:
        self.stats = CacheStats()

    def get(self, canonical: str) -> ZestimateResult | None:
        self.stats.misses += 1
        return None

    def set(self, canonical: str, result: ZestimateResult) -> None:
        return None

    def clear(self) -> int:
        return 0

    def volume(self) -> int:
        return 0

    def close(self) -> None:
        return None


# ─── Memory cache (for tests) ───────────────────────────────────


class MemoryCache:
    """Dict-backed cache. Tests use this to avoid filesystem I/O."""

    def __init__(self, *, ttl_seconds: int = 21600) -> None:
        self.stats = CacheStats()
        self._ttl = ttl_seconds
        self._store: dict[str, tuple[float, str]] = {}

    def get(self, canonical: str) -> ZestimateResult | None:
        key = _make_key(canonical)
        item = self._store.get(key)
        if item is None:
            self.stats.misses += 1
            return None
        expiry, payload = item
        if expiry < datetime.now(UTC).timestamp():
            del self._store[key]
            self.stats.evictions += 1
            self.stats.misses += 1
            return None
        self.stats.hits += 1
        return _deserialize(payload)

    def set(self, canonical: str, result: ZestimateResult) -> None:
        if result.status not in CACHEABLE_STATUSES:
            return
        key = _make_key(canonical)
        payload = _serialize(result)
        expiry = datetime.now(UTC).timestamp() + self._ttl
        self._store[key] = (expiry, payload)
        self.stats.writes += 1

    def clear(self) -> int:
        n = len(self._store)
        self._store.clear()
        return n

    def volume(self) -> int:
        return sum(len(v[1]) for v in self._store.values())

    def close(self) -> None:
        self._store.clear()


# ─── Disk cache (SQLite via diskcache) ──────────────────────────


class DiskResultCache:
    """Production cache — SQLite-backed via the `diskcache` library.

    diskcache handles: atomic commits, TTL expiry on read, size-bounded
    LRU eviction, and cross-thread safety. We wrap it so the rest of the
    codebase never imports diskcache directly.
    """

    def __init__(self, path: Path, *, ttl_seconds: int = 21600) -> None:
        # Lazy import — keeps the dep optional when CACHE_BACKEND=none/memory.
        import diskcache

        self.stats = CacheStats()
        self._ttl = ttl_seconds
        path.parent.mkdir(parents=True, exist_ok=True)
        self._cache: diskcache.Cache = diskcache.Cache(str(path))

    def get(self, canonical: str) -> ZestimateResult | None:
        key = _make_key(canonical)
        raw = self._cache.get(key)
        if raw is None:
            self.stats.misses += 1
            return None
        if not isinstance(raw, str):
            # Type drift — treat as miss and evict.
            self._cache.delete(key)
            self.stats.evictions += 1
            self.stats.misses += 1
            return None
        try:
            result = _deserialize(raw)
        except Exception as e:
            log.warning("cache decode failed — evicting", key=key, error=str(e))
            self._cache.delete(key)
            self.stats.evictions += 1
            self.stats.misses += 1
            return None
        self.stats.hits += 1
        return result

    def set(self, canonical: str, result: ZestimateResult) -> None:
        if result.status not in CACHEABLE_STATUSES:
            return
        key = _make_key(canonical)
        payload = _serialize(result)
        self._cache.set(key, payload, expire=self._ttl)
        self.stats.writes += 1

    def clear(self) -> int:
        n = len(self._cache)
        self._cache.clear()
        return n

    def volume(self) -> int:
        vol = self._cache.volume()
        return int(vol)

    def close(self) -> None:
        self._cache.close()


# ─── Serialization ──────────────────────────────────────────────


def _serialize(result: ZestimateResult) -> str:
    """Serialize a ZestimateResult. Always strips `cached=True` before writing.

    We flip `cached=True` when *reading* back, not when writing, so a cached
    result re-written (e.g. via different canonicalization) doesn't claim to
    have been served from cache.
    """
    d = result.model_dump(mode="json")
    d["cached"] = False
    return json.dumps(d)


def _deserialize(raw: str) -> ZestimateResult:
    d: dict[str, Any] = json.loads(raw)
    d["cached"] = True
    return ZestimateResult.model_validate(d)


# ─── Factory ────────────────────────────────────────────────────


def build_cache() -> ResultCache:
    """Return a cache configured from settings."""
    settings = get_settings()
    backend = settings.cache_backend

    if backend == "none":
        return NullCache()
    if backend == "memory":
        return MemoryCache(ttl_seconds=settings.cache_ttl_seconds)
    if backend == "sqlite":
        return DiskResultCache(
            settings.cache_path,
            ttl_seconds=settings.cache_ttl_seconds,
        )

    log.warning("unknown cache backend — falling back to NullCache", backend=backend)
    return NullCache()
