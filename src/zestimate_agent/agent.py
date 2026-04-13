"""Top-level orchestrator — the `ZestimateAgent` facade.

Wires Normalizer → Resolver → Fetcher → Parser into a single async call.

Key invariant: `get()` / `aget()` **never raise**. Every failure mode is
mapped to a `ZestimateStatus` and returned as a structured `ZestimateResult`.
Callers can inspect `result.status` or just check `result.ok`.
"""

from __future__ import annotations

import asyncio
import time
import uuid

import structlog

from zestimate_agent.cache import ResultCache, build_cache
from zestimate_agent.config import Settings, get_settings
from zestimate_agent.crosscheck import RentcastClient, build_rentcast_client
from zestimate_agent.errors import (
    AmbiguousAddressError,
    FetchBlockedError,
    FetchError,
    NormalizationError,
    NoZestimateError,
    ParseError,
    PropertyNotFoundError,
    ResolverError,
    ZestimateError,
)
from zestimate_agent.fetch.base import Fetcher
from zestimate_agent.fetch.chain import FetcherChain
from zestimate_agent.fetch.circuit_breaker import CircuitOpenError
from zestimate_agent.fetch.playwright import build_playwright_fetcher
from zestimate_agent.fetch.unblocker import build_unblocker_fetcher
from zestimate_agent.logging import get_logger
from zestimate_agent.models import NormalizedAddress, ZestimateResult, ZestimateStatus
from zestimate_agent.normalize import Normalizer, default_normalizer
from zestimate_agent.parse import parse as parse_page
from zestimate_agent.resolve import ZillowResolver
from zestimate_agent.validate import validate as validate_result

log = get_logger(__name__)


# ─── Per-stage latency (Prometheus + structured log) ───────────

try:
    from prometheus_client import Histogram

    STAGE_LATENCY = Histogram(
        "zestimate_stage_duration_seconds",
        "Per-pipeline-stage latency in seconds.",
        labelnames=("stage",),
        buckets=(0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0),
    )
except Exception:  # pragma: no cover — prometheus_client may not be installed
    STAGE_LATENCY = None  # type: ignore[assignment]


def _stage_ms(stage: str, t0: float) -> None:
    """Log and record a pipeline stage's wall-clock duration."""
    elapsed = time.monotonic() - t0
    log.debug("stage done", stage=stage, elapsed_ms=int(elapsed * 1000))
    if STAGE_LATENCY is not None:
        STAGE_LATENCY.labels(stage=stage).observe(elapsed)


def _playwright_available() -> bool:
    """Check if playwright is importable without side effects."""
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


class ZestimateAgent:
    """High-level agent. Construct via `ZestimateAgent.from_env()` or pass deps."""

    def __init__(
        self,
        settings: Settings,
        *,
        normalizer: Normalizer | None = None,
        resolver: ZillowResolver | None = None,
        fetcher: Fetcher | None = None,
        crosschecker: RentcastClient | None = None,
        cache: ResultCache | None = None,
    ) -> None:
        self._settings = settings
        self._normalizer = normalizer or default_normalizer()
        self._resolver = resolver or ZillowResolver()
        self._fetcher: Fetcher | None = fetcher
        # Lazy-built: don't hit env for rentcast key at construction time,
        # because tests commonly construct the agent without one.
        self._crosschecker: RentcastClient | None = crosschecker
        self._crosschecker_built = crosschecker is not None
        self._cache: ResultCache | None = cache
        self._cache_built = cache is not None
        self._closed = False

    # ─── Factories ──────────────────────────────────────────────

    @classmethod
    def from_env(cls) -> ZestimateAgent:
        return cls(get_settings())

    def _get_fetcher(self) -> Fetcher:
        if self._fetcher is None:
            if self._settings.fetcher_primary == "playwright":
                self._fetcher = build_playwright_fetcher()
            else:
                primary = build_unblocker_fetcher()
                # When Playwright is enabled and installed, wrap in a
                # chain so blocked requests automatically fall back to
                # headless Chrome.
                if self._settings.playwright_enabled and _playwright_available():
                    self._fetcher = FetcherChain(primary, build_playwright_fetcher())
                else:
                    self._fetcher = primary
        return self._fetcher

    def _get_crosschecker(self) -> RentcastClient | None:
        if not self._crosschecker_built:
            self._crosschecker = build_rentcast_client()
            self._crosschecker_built = True
        return self._crosschecker

    def _get_cache(self) -> ResultCache:
        if not self._cache_built:
            self._cache = build_cache()
            self._cache_built = True
        assert self._cache is not None
        return self._cache

    # ─── Public API ─────────────────────────────────────────────

    def get(self, address: str, **kwargs: object) -> ZestimateResult:
        """Synchronous convenience wrapper around `aget`."""
        return asyncio.run(self.aget(address, **kwargs))  # type: ignore[arg-type]

    async def aget(
        self,
        address: str,
        *,
        skip_crosscheck: bool = False,
        force_crosscheck: bool = False,
        use_cache: bool = True,
    ) -> ZestimateResult:
        """Primary async entry point. Never raises."""
        trace_id = str(uuid.uuid4())
        try:
            return await self._aget_inner(
                address,
                trace_id=trace_id,
                skip_crosscheck=skip_crosscheck,
                force_crosscheck=force_crosscheck,
                use_cache=use_cache,
            )
        except Exception as e:
            # Last-resort catch-all: the "never raises" contract must hold
            # even for completely unexpected errors (e.g. RuntimeError from
            # a misbehaving dependency).
            log.error("unexpected error in aget", error=str(e), exc_info=True)
            return ZestimateResult(
                status=ZestimateStatus.ERROR,
                error=f"unexpected: {e}",
                trace_id=trace_id,
            )

    async def _aget_inner(
        self,
        address: str,
        *,
        trace_id: str,
        skip_crosscheck: bool = False,
        force_crosscheck: bool = False,
        use_cache: bool = True,
    ) -> ZestimateResult:
        """Inner implementation — may raise, caught by aget()."""
        # Bind trace_id to structlog contextvars so every downstream log
        # line (normalizer, resolver, fetcher, parser) carries it without
        # explicit passing.
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(trace_id=trace_id)

        log.info("lookup start", address=address)
        normalized: NormalizedAddress | None = None

        # ─── 1. Normalize ───
        try:
            t0 = time.monotonic()
            normalized = await asyncio.to_thread(self._normalizer.normalize, address)
            _stage_ms("normalize", t0)
        except NormalizationError as e:
            return ZestimateResult(
                status=ZestimateStatus.NOT_FOUND,
                error=f"address normalization failed: {e}",
                trace_id=trace_id,
            )

        # ─── 1b. Cache lookup (after normalize so we have a canonical key) ───
        if use_cache:
            cached = await asyncio.to_thread(
                self._get_cache().get, normalized.canonical
            )
            if cached is not None:
                log.info(
                    "cache hit",
                    trace_id=trace_id,
                    canonical=normalized.canonical,
                    status=cached.status.value,
                    value=cached.value,
                )
                # Refresh trace_id so each call is traceable even on hit.
                return cached.model_copy(update={"trace_id": trace_id})

        # ─── 2. Resolve ───
        try:
            t0 = time.monotonic()
            resolved = await self._resolver.resolve(normalized)
            _stage_ms("resolve", t0)
        except PropertyNotFoundError as e:
            not_found = ZestimateResult(
                status=ZestimateStatus.NOT_FOUND,
                matched_address=normalized.canonical,
                error=str(e),
                trace_id=trace_id,
            )
            if use_cache:
                await asyncio.to_thread(
                    self._get_cache().set, normalized.canonical, not_found
                )
            return not_found
        except AmbiguousAddressError as e:
            return ZestimateResult(
                status=ZestimateStatus.AMBIGUOUS,
                matched_address=normalized.canonical,
                error=str(e),
                trace_id=trace_id,
            )
        except ResolverError as e:
            log.warning("resolver error", error=str(e))
            return ZestimateResult(
                status=ZestimateStatus.ERROR,
                matched_address=normalized.canonical,
                error=f"resolver: {e}",
                trace_id=trace_id,
            )

        log.info(
            "resolved",
            zpid=resolved.zpid,
            confidence=resolved.match_confidence,
            canonical=normalized.canonical,
        )

        # ─── 3. Fetch ───
        try:
            t0 = time.monotonic()
            fetcher = self._get_fetcher()
            fetch_result = await fetcher.fetch(resolved.url)
            _stage_ms("fetch", t0)
        except CircuitOpenError as e:
            log.warning("circuit breaker open — failing fast", error=str(e))
            return ZestimateResult(
                status=ZestimateStatus.BLOCKED,
                zpid=resolved.zpid,
                matched_address=resolved.matched_address,
                zillow_url=resolved.url,
                error=f"circuit breaker open: {e}",
                trace_id=trace_id,
            )
        except FetchBlockedError as e:
            return ZestimateResult(
                status=ZestimateStatus.BLOCKED,
                zpid=resolved.zpid,
                matched_address=resolved.matched_address,
                zillow_url=resolved.url,
                error=f"fetcher blocked: {e}",
                trace_id=trace_id,
            )
        except FetchError as e:
            log.error("fetch error", error=str(e))
            return ZestimateResult(
                status=ZestimateStatus.ERROR,
                zpid=resolved.zpid,
                matched_address=resolved.matched_address,
                zillow_url=resolved.url,
                error=f"fetch: {e}",
                trace_id=trace_id,
            )

        # ─── 4. Parse ───
        try:
            t0 = time.monotonic()
            result = await asyncio.to_thread(parse_page, fetch_result)
            _stage_ms("parse", t0)
        except NoZestimateError as e:
            no_zest = ZestimateResult(
                status=ZestimateStatus.NO_ZESTIMATE,
                zpid=resolved.zpid,
                matched_address=resolved.matched_address,
                zillow_url=resolved.url,
                fetcher=fetch_result.fetcher,
                fetched_at=fetch_result.fetched_at,
                error=str(e),
                trace_id=trace_id,
            )
            if use_cache:
                await asyncio.to_thread(
                    self._get_cache().set, normalized.canonical, no_zest
                )
            return no_zest
        except ParseError as e:
            log.error("parse error", error=str(e))
            return ZestimateResult(
                status=ZestimateStatus.ERROR,
                zpid=resolved.zpid,
                matched_address=resolved.matched_address,
                zillow_url=resolved.url,
                fetcher=fetch_result.fetcher,
                fetched_at=fetch_result.fetched_at,
                error=f"parse: {e}",
                trace_id=trace_id,
            )
        except ZestimateError as e:  # catch-all for other typed errors
            return ZestimateResult(
                status=ZestimateStatus.ERROR,
                error=str(e),
                trace_id=trace_id,
            )

        # ─── 5. Enrich with resolver metadata the parser didn't have ───
        enriched = result.model_copy(
            update={
                "zpid": result.zpid or resolved.zpid,
                "matched_address": result.matched_address or resolved.matched_address,
                "zillow_url": result.zillow_url or resolved.url,
                "alternates": resolved.alternates,
                "confidence": result.confidence * resolved.match_confidence * normalized.parse_confidence,
                "trace_id": trace_id,
            }
        )

        # ─── 6. Validate (sanity + cross-check) ───
        t0 = time.monotonic()
        validated = await validate_result(
            enriched,
            client=self._get_crosschecker(),
            address=normalized,
            force_crosscheck=force_crosscheck,
            skip_crosscheck=skip_crosscheck,
        )

        _stage_ms("validate", t0)

        # ─── 7. Cache write ───
        if use_cache:
            await asyncio.to_thread(
                self._get_cache().set, normalized.canonical, validated
            )

        log.info(
            "lookup done",
            value=validated.value,
            confidence=validated.confidence,
            fetcher=validated.fetcher,
            crosscheck=(
                validated.crosscheck.model_dump(mode="json")
                if validated.crosscheck is not None
                else None
            ),
        )
        return validated

    # ─── Cleanup ────────────────────────────────────────────────

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._fetcher is not None:
            await self._fetcher.aclose()
        if self._crosschecker is not None:
            await self._crosschecker.aclose()
        if self._cache is not None:
            await asyncio.to_thread(self._cache.close)
        await self._resolver.aclose()
