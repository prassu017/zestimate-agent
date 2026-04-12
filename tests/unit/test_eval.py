"""Unit tests for the eval harness — dataset, runner, and report.

We verify that:
    * EvalCase.is_correct handles exact/tolerance/status-only semantics
    * run_eval() runs the default synthetic dataset end-to-end at 100%
    * run_eval() runs the default fixture dataset end-to-end at 100%
    * categories filter correctly
    * live cases are auto-skipped when no live_agent_factory is supplied
    * the report computes summary stats correctly
    * JSON / CSV serialization is well-formed
"""

from __future__ import annotations

import csv
import io
import json

import pytest

from zestimate_agent.eval import (
    DEFAULT_DATASET,
    EvalCategory,
    EvalMode,
    EvalReport,
    EvalRunConfig,
    by_category,
    by_mode,
    run_eval,
)
from zestimate_agent.eval.dataset import EvalCase
from zestimate_agent.eval.report import summarize
from zestimate_agent.eval.runner import EvalOutcome
from zestimate_agent.models import ZestimateResult, ZestimateStatus

# ─── Dataset filters ────────────────────────────────────────────


class TestDatasetFilters:
    def test_dataset_has_all_modes_represented(self) -> None:
        modes = {c.mode for c in DEFAULT_DATASET}
        assert EvalMode.SYNTHETIC in modes
        assert EvalMode.FIXTURE in modes
        assert EvalMode.LIVE in modes

    def test_every_synthetic_case_has_html(self) -> None:
        for c in by_mode(EvalMode.SYNTHETIC):
            assert c.synthetic_html is not None, f"{c.id} missing synthetic_html"

    def test_every_fixture_case_has_file_and_it_exists(self) -> None:
        for c in by_mode(EvalMode.FIXTURE):
            assert c.fixture_html_file is not None
            html = c.load_html()
            assert html is not None and len(html) > 500

    def test_by_category_returns_only_matching(self) -> None:
        condos = by_category(EvalCategory.CONDO)
        assert all(c.category == EvalCategory.CONDO for c in condos)
        assert len(condos) >= 1


# ─── EvalCase.is_correct semantics (via EvalOutcome) ────────────


class TestEvalOutcome:
    def _case(
        self,
        *,
        expected_value: int | None,
        expected_status: ZestimateStatus = ZestimateStatus.OK,
        tolerance_pct: float = 0.0,
        expected_zpid: str | None = None,
    ) -> EvalCase:
        return EvalCase(
            id="test",
            address="x",
            category=EvalCategory.SFH,
            mode=EvalMode.SYNTHETIC,
            expected_status=expected_status,
            expected_value=expected_value,
            expected_zpid=expected_zpid,
            tolerance_pct=tolerance_pct,
        )

    def test_exact_match_passes(self) -> None:
        case = self._case(expected_value=500_000)
        result = ZestimateResult(status=ZestimateStatus.OK, value=500_000)
        o = EvalOutcome(case=case, result=result, elapsed_ms=1)
        assert o.is_correct
        assert o.value_exact_match

    def test_one_dollar_off_fails_exact(self) -> None:
        case = self._case(expected_value=500_000)
        result = ZestimateResult(status=ZestimateStatus.OK, value=499_999)
        o = EvalOutcome(case=case, result=result, elapsed_ms=1)
        assert not o.is_correct
        assert not o.value_exact_match
        assert o.value_within_1pct  # but still within 1%

    def test_tolerance_1pct_drift(self) -> None:
        case = self._case(expected_value=500_000, tolerance_pct=1.0)
        result = ZestimateResult(status=ZestimateStatus.OK, value=504_000)
        o = EvalOutcome(case=case, result=result, elapsed_ms=1)
        assert o.is_correct  # within 1% tolerance

    def test_out_of_tolerance_fails(self) -> None:
        case = self._case(expected_value=500_000, tolerance_pct=1.0)
        result = ZestimateResult(status=ZestimateStatus.OK, value=520_000)  # +4%
        o = EvalOutcome(case=case, result=result, elapsed_ms=1)
        assert not o.is_correct

    def test_status_only_case_ignores_value(self) -> None:
        case = self._case(
            expected_value=None,
            expected_status=ZestimateStatus.NO_ZESTIMATE,
        )
        result = ZestimateResult(status=ZestimateStatus.NO_ZESTIMATE)
        o = EvalOutcome(case=case, result=result, elapsed_ms=1)
        assert o.is_correct

    def test_wrong_status_fails_even_if_value_right(self) -> None:
        case = self._case(
            expected_value=500_000,
            expected_status=ZestimateStatus.OK,
        )
        result = ZestimateResult(status=ZestimateStatus.ERROR, value=500_000)
        o = EvalOutcome(case=case, result=result, elapsed_ms=1)
        assert not o.is_correct

    def test_zpid_match_required_when_specified(self) -> None:
        case = self._case(expected_value=500_000, expected_zpid="123")
        result = ZestimateResult(status=ZestimateStatus.OK, value=500_000, zpid="999")
        o = EvalOutcome(case=case, result=result, elapsed_ms=1)
        assert not o.is_correct  # zpid mismatch
        assert o.value_exact_match  # but value was exact


# ─── run_eval() end-to-end ──────────────────────────────────────


class TestRunEval:
    @pytest.mark.asyncio
    async def test_synthetic_dataset_hits_100pct(self) -> None:
        outcomes = await run_eval(
            DEFAULT_DATASET,
            config=EvalRunConfig(mode=EvalMode.SYNTHETIC),
        )
        assert len(outcomes) >= 5
        report = EvalReport.from_outcomes(outcomes)
        assert report.summary.accuracy == 1.0
        assert report.summary.hit_target
        assert report.failures() == []

    @pytest.mark.asyncio
    async def test_fixture_dataset_hits_100pct(self) -> None:
        outcomes = await run_eval(
            DEFAULT_DATASET,
            config=EvalRunConfig(mode=EvalMode.FIXTURE),
        )
        assert len(outcomes) >= 1
        report = EvalReport.from_outcomes(outcomes)
        assert report.summary.accuracy == 1.0
        assert all(o.value_exact_match for o in outcomes if o.case.expected_value)

    @pytest.mark.asyncio
    async def test_category_filter(self) -> None:
        outcomes = await run_eval(
            DEFAULT_DATASET,
            config=EvalRunConfig(categories=("parser_fallback",)),
        )
        assert len(outcomes) >= 2
        assert all(o.case.category == EvalCategory.PARSER_FALLBACK for o in outcomes)

    @pytest.mark.asyncio
    async def test_live_cases_skipped_without_factory(self) -> None:
        """Running in mixed mode without a live_agent_factory must drop live cases."""
        outcomes = await run_eval(DEFAULT_DATASET, config=EvalRunConfig())
        assert all(o.case.mode != EvalMode.LIVE for o in outcomes)

    @pytest.mark.asyncio
    async def test_limit_honored(self) -> None:
        outcomes = await run_eval(
            DEFAULT_DATASET,
            config=EvalRunConfig(mode=EvalMode.SYNTHETIC, limit=2),
        )
        assert len(outcomes) == 2

    @pytest.mark.asyncio
    async def test_bogus_mode_yields_empty(self) -> None:
        outcomes = await run_eval(
            DEFAULT_DATASET,
            config=EvalRunConfig(categories=("nonexistent_category",)),
        )
        assert outcomes == []


# ─── Report summarization ──────────────────────────────────────


class TestReport:
    def _outcome(
        self,
        *,
        category: EvalCategory,
        expected: int,
        actual: int | None,
        elapsed_ms: int = 10,
        status: ZestimateStatus = ZestimateStatus.OK,
    ) -> EvalOutcome:
        case = EvalCase(
            id=f"case-{category.value}",
            address="x",
            category=category,
            mode=EvalMode.SYNTHETIC,
            expected_value=expected,
            expected_status=status,
        )
        result = ZestimateResult(status=status, value=actual)
        return EvalOutcome(case=case, result=result, elapsed_ms=elapsed_ms)

    def test_all_correct_summary(self) -> None:
        outs = [
            self._outcome(category=EvalCategory.SFH, expected=100, actual=100),
            self._outcome(category=EvalCategory.CONDO, expected=200, actual=200),
        ]
        s = summarize(outs)
        assert s.total == 2
        assert s.correct == 2
        assert s.accuracy == 1.0
        assert s.hit_target
        assert len(s.per_category) == 2

    def test_one_failure_drags_accuracy(self) -> None:
        outs = [
            self._outcome(category=EvalCategory.SFH, expected=100, actual=100),
            self._outcome(category=EvalCategory.SFH, expected=200, actual=999),  # wrong
        ]
        s = summarize(outs)
        assert s.correct == 1
        assert s.accuracy == 0.5
        assert not s.hit_target

    def test_latency_stats(self) -> None:
        outs = [
            self._outcome(category=EvalCategory.SFH, expected=100, actual=100, elapsed_ms=i)
            for i in [1, 5, 10, 50, 100]
        ]
        s = summarize(outs)
        assert s.p50_ms == 10
        assert s.p95_ms in (50, 100)  # index depends on rounding
        assert s.mean_ms == 33  # (1+5+10+50+100)/5 = 33.2

    def test_empty_outcomes_safe(self) -> None:
        s = summarize([])
        assert s.total == 0
        assert s.accuracy == 0.0
        assert not s.hit_target

    def test_json_is_valid(self) -> None:
        outs = [
            self._outcome(category=EvalCategory.SFH, expected=100, actual=100),
            self._outcome(category=EvalCategory.CONDO, expected=200, actual=200),
        ]
        report = EvalReport.from_outcomes(outs)
        parsed = json.loads(report.to_json())
        assert parsed["summary"]["total"] == 2
        assert parsed["summary"]["accuracy_pct"] == 100.0
        assert len(parsed["cases"]) == 2
        assert parsed["cases"][0]["is_correct"] is True

    def test_csv_is_well_formed(self) -> None:
        outs = [
            self._outcome(category=EvalCategory.SFH, expected=100, actual=100),
            self._outcome(category=EvalCategory.CONDO, expected=200, actual=180),  # fail
        ]
        report = EvalReport.from_outcomes(outs)
        buf = io.StringIO(report.to_csv())
        reader = list(csv.reader(buf))
        assert reader[0][0] == "id"  # header row
        assert len(reader) == 3  # header + 2 rows
        assert reader[1][0] == "case-sfh"
        assert reader[2][0] == "case-condo"
