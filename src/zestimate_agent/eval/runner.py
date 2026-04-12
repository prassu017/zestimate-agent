"""Eval runner — executes an `EvalCase` dataset against a `ZestimateAgent`.

The runner has three execution modes that map 1:1 to `EvalCase.mode`:

* **synthetic** — stubs normalize + resolve + fetch with local fakes that
  return the case's inline HTML. Zero network, zero credits.
* **fixture** — stubs normalize + resolve, but uses a real `parse()` on
  HTML loaded from `eval/fixtures/`. Zero network, zero credits.
* **live** — hits the real agent end-to-end. Costs ScraperAPI credits
  and (unless `skip_crosscheck=True`) Rentcast requests.

One `run_eval()` entry point handles all three — it iterates a list of
cases, dispatches each to the appropriate agent configuration, and
returns a list of `EvalOutcome` records for the reporter to render.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from zestimate_agent.agent import ZestimateAgent
from zestimate_agent.cache import NullCache
from zestimate_agent.config import get_settings
from zestimate_agent.errors import (
    AmbiguousAddressError,
    PropertyNotFoundError,
)
from zestimate_agent.eval.dataset import (
    DEFAULT_DATASET,
    EvalCase,
    EvalMode,
)
from zestimate_agent.logging import get_logger
from zestimate_agent.models import (
    FetchResult,
    NormalizedAddress,
    ResolvedProperty,
    ZestimateResult,
    ZestimateStatus,
)

log = get_logger(__name__)


# ─── Outcome record ─────────────────────────────────────────────


@dataclass(frozen=True)
class EvalOutcome:
    """One row of eval output — case + actual result + derived booleans."""

    case: EvalCase
    result: ZestimateResult
    elapsed_ms: int
    exception: str | None = None

    # ─── Derived comparisons ────────────────────────────────────

    @property
    def status_match(self) -> bool:
        return self.result.status == self.case.expected_status

    @property
    def value_exact_match(self) -> bool:
        if self.case.expected_value is None:
            return True  # "don't care" cases pass
        return self.result.value == self.case.expected_value

    @property
    def value_within_1pct(self) -> bool:
        return self._within(0.01)

    @property
    def value_within_5pct(self) -> bool:
        return self._within(0.05)

    @property
    def value_within_tolerance(self) -> bool:
        return self._within(self.case.tolerance_pct / 100.0)

    def _within(self, frac: float) -> bool:
        if self.case.expected_value is None:
            return True
        if self.result.value is None:
            return False
        if frac == 0.0:
            return self.result.value == self.case.expected_value
        delta = abs(self.result.value - self.case.expected_value)
        return delta <= self.case.expected_value * frac

    @property
    def zpid_match(self) -> bool:
        if self.case.expected_zpid is None:
            return True
        return self.result.zpid == self.case.expected_zpid

    @property
    def is_correct(self) -> bool:
        """Canonical pass/fail for this case.

        "Correct" means:
            * status matches AND
            * value is within the case's declared tolerance (0 = exact) AND
            * zpid matches if one was specified

        For non-OK cases (NO_ZESTIMATE / NOT_FOUND / etc), only `status_match`
        is required since those cases have no value to compare.
        """
        if not self.status_match:
            return False
        if self.case.expected_status != ZestimateStatus.OK:
            return True  # status-only check
        return self.value_within_tolerance and self.zpid_match


# ─── Fakes for synthetic/fixture mode ──────────────────────────


class _CannedNormalizer:
    """Skip normalization — synthesize a canonical address straight from the case."""

    def normalize(self, raw: str) -> NormalizedAddress:
        # Minimal canonicalization: collapse whitespace, uppercase state via
        # a very loose regex-free parse. We don't want to actually exercise
        # usaddress here — that's a separate concern under test_normalize.
        canonical = " ".join(raw.split())
        # Seed with a dummy 5-digit zip so the model validator passes.
        # The canonical form is what goes into the cache key; resolver is stubbed.
        return NormalizedAddress(
            raw=raw,
            street=canonical,
            city="Testville",
            state="CA",
            zip="94000",
            canonical=canonical,
            parse_confidence=1.0,
        )


class _CannedResolver:
    """Returns a `ResolvedProperty` from pre-canned fields on the EvalCase."""

    def __init__(self, case: EvalCase) -> None:
        self._case = case

    async def resolve(self, normalized: NormalizedAddress) -> ResolvedProperty:
        if self._case.expected_status == ZestimateStatus.NOT_FOUND:
            raise PropertyNotFoundError(f"eval case {self._case.id} expects NOT_FOUND")
        if self._case.expected_status == ZestimateStatus.AMBIGUOUS:
            raise AmbiguousAddressError(f"eval case {self._case.id} expects AMBIGUOUS")
        zpid = self._case.canned_zpid or "999"
        url = self._case.canned_url or f"https://www.zillow.com/homedetails/{zpid}_zpid/"
        return ResolvedProperty(
            zpid=zpid,
            url=url,
            matched_address=normalized.canonical,
            match_confidence=1.0,
        )

    async def aclose(self) -> None:
        return None


class _CannedFetcher:
    """Serves the case's loaded HTML. No network."""

    name = "eval-synthetic"

    def __init__(self, html: str) -> None:
        self._html = html

    async def fetch(self, url: str) -> FetchResult:
        return FetchResult(
            html=self._html,
            status=200,
            final_url=url,
            fetcher=self.name,
            fetched_at=datetime.now(UTC),
            elapsed_ms=0,
        )

    async def aclose(self) -> None:
        return None


# ─── Runner ─────────────────────────────────────────────────────


async def _run_case(
    case: EvalCase,
    *,
    live_agent_factory: Callable[[], ZestimateAgent] | None,
    skip_crosscheck: bool,
    force_crosscheck: bool,
) -> EvalOutcome:
    """Execute one case and return its outcome (never raises)."""
    started = time.monotonic()
    try:
        if case.mode == EvalMode.LIVE:
            if live_agent_factory is None:
                raise RuntimeError(
                    "live mode case encountered but no live_agent_factory provided"
                )
            agent = live_agent_factory()
            try:
                result = await agent.aget(
                    case.address,
                    skip_crosscheck=skip_crosscheck,
                    force_crosscheck=force_crosscheck,
                )
            finally:
                await agent.aclose()
        else:
            html = case.load_html()
            if html is None:
                raise RuntimeError(
                    f"case {case.id}: mode={case.mode} but no HTML available"
                )
            agent = _build_stub_agent(case, html)
            try:
                result = await agent.aget(
                    case.address,
                    skip_crosscheck=True,  # synthetic/fixture never needs Rentcast
                    use_cache=False,  # eval must never hit the real cache
                )
            finally:
                await agent.aclose()
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return EvalOutcome(case=case, result=result, elapsed_ms=elapsed_ms)
    except Exception as e:
        # Eval harness must never crash on a bad case. Capture the error,
        # surface it in the outcome, and continue.
        log.warning("eval case raised unexpectedly", case=case.id, error=str(e))
        elapsed_ms = int((time.monotonic() - started) * 1000)
        err_result = ZestimateResult(
            status=ZestimateStatus.ERROR,
            error=f"{type(e).__name__}: {e}",
        )
        return EvalOutcome(
            case=case,
            result=err_result,
            elapsed_ms=elapsed_ms,
            exception=f"{type(e).__name__}: {e}",
        )


def _build_stub_agent(case: EvalCase, html: str) -> ZestimateAgent:
    """Assemble an agent with canned normalize/resolve/fetch for the given case."""
    return ZestimateAgent(
        get_settings(),
        normalizer=_CannedNormalizer(),  # type: ignore[arg-type]
        resolver=_CannedResolver(case),  # type: ignore[arg-type]
        fetcher=_CannedFetcher(html),
        crosschecker=None,
        cache=NullCache(),
    )


# ─── Config for run_eval ────────────────────────────────────────


@dataclass
class EvalRunConfig:
    """Optional knobs for `run_eval()`."""

    mode: EvalMode | None = None  # filter: only run cases matching this mode
    categories: tuple[str, ...] = field(default_factory=tuple)  # filter by category name
    limit: int | None = None
    concurrency: int = 4
    skip_crosscheck: bool = True  # default: protect the Rentcast cap
    force_crosscheck: bool = False
    live_agent_factory: Callable[[], ZestimateAgent] | None = None


async def run_eval(
    dataset: tuple[EvalCase, ...] = DEFAULT_DATASET,
    *,
    config: EvalRunConfig | None = None,
) -> list[EvalOutcome]:
    """Run an eval dataset and return per-case outcomes.

    The caller is responsible for filtering (via `config.mode` / `.categories`).
    Live cases require a `live_agent_factory`; if absent, live cases are skipped.
    """
    cfg = config or EvalRunConfig()

    # ─── Filter ──────────────────────────────────────────────
    cases: list[EvalCase] = list(dataset)
    if cfg.mode is not None:
        cases = [c for c in cases if c.mode == cfg.mode]
    if cfg.categories:
        allowed = set(cfg.categories)
        cases = [c for c in cases if c.category.value in allowed]
    if cfg.live_agent_factory is None and cfg.mode != EvalMode.LIVE:
        # No live factory → silently drop live cases (unless user asked for only live).
        cases = [c for c in cases if c.mode != EvalMode.LIVE]
    if cfg.limit is not None:
        cases = cases[: cfg.limit]

    if not cases:
        return []

    log.info(
        "eval run start",
        n_cases=len(cases),
        mode=cfg.mode.value if cfg.mode else "mixed",
        concurrency=cfg.concurrency,
    )

    # ─── Execute with bounded concurrency ────────────────────
    sem = asyncio.Semaphore(cfg.concurrency)

    async def _bounded(case: EvalCase) -> EvalOutcome:
        async with sem:
            return await _run_case(
                case,
                live_agent_factory=cfg.live_agent_factory,
                skip_crosscheck=cfg.skip_crosscheck,
                force_crosscheck=cfg.force_crosscheck,
            )

    outcomes = await asyncio.gather(*(_bounded(c) for c in cases))

    correct = sum(1 for o in outcomes if o.is_correct)
    log.info(
        "eval run done",
        n_cases=len(outcomes),
        correct=correct,
        accuracy=f"{correct / len(outcomes) * 100:.1f}%" if outcomes else "n/a",
    )
    return list(outcomes)
