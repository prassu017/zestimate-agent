"""Eval report — aggregate EvalOutcome lists into stats + pretty/JSON/CSV.

The summary is designed to answer "did we hit ≥99% accuracy?" at a glance:

    OVERALL  14/15 (93.3%)  ·  p50 12ms  p95 840ms
    SFH            3/3  (100%)
    CONDO          4/4  (100%)
    LUXURY         2/3  (66.7%)   ← investigate
    PARSER_FALL    2/2  (100%)
    ...

and to emit a machine-readable JSON/CSV for downstream plotting.
"""

from __future__ import annotations

import csv
import io
import json
import statistics
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from zestimate_agent.eval.runner import EvalOutcome

# ─── Summary types ──────────────────────────────────────────────


@dataclass(frozen=True)
class CategoryStats:
    category: str
    total: int
    correct: int
    exact_value: int
    within_1pct: int
    within_5pct: int
    status_match: int
    zpid_match: int

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


@dataclass(frozen=True)
class EvalSummary:
    total: int
    correct: int
    exact_value: int
    within_1pct: int
    within_5pct: int
    status_match: int
    zpid_match: int
    p50_ms: int
    p95_ms: int
    mean_ms: int
    per_category: tuple[CategoryStats, ...]

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def hit_target(self) -> bool:
        """Return True iff overall accuracy meets the ≥99% contract."""
        return self.accuracy >= 0.99

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "correct": self.correct,
            "accuracy_pct": round(self.accuracy * 100, 2),
            "hit_99pct_target": self.hit_target,
            "exact_value": self.exact_value,
            "within_1pct": self.within_1pct,
            "within_5pct": self.within_5pct,
            "status_match": self.status_match,
            "zpid_match": self.zpid_match,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "mean_ms": self.mean_ms,
            "per_category": [
                {
                    "category": s.category,
                    "total": s.total,
                    "correct": s.correct,
                    "accuracy_pct": round(s.accuracy * 100, 2),
                    "exact_value": s.exact_value,
                    "within_1pct": s.within_1pct,
                    "within_5pct": s.within_5pct,
                    "status_match": s.status_match,
                    "zpid_match": s.zpid_match,
                }
                for s in self.per_category
            ],
        }


# ─── Aggregation ────────────────────────────────────────────────


def summarize(outcomes: list[EvalOutcome]) -> EvalSummary:
    if not outcomes:
        return EvalSummary(
            total=0,
            correct=0,
            exact_value=0,
            within_1pct=0,
            within_5pct=0,
            status_match=0,
            zpid_match=0,
            p50_ms=0,
            p95_ms=0,
            mean_ms=0,
            per_category=(),
        )

    total = len(outcomes)
    correct = sum(1 for o in outcomes if o.is_correct)
    exact_value = sum(1 for o in outcomes if o.value_exact_match)
    w1 = sum(1 for o in outcomes if o.value_within_1pct)
    w5 = sum(1 for o in outcomes if o.value_within_5pct)
    status_match = sum(1 for o in outcomes if o.status_match)
    zpid_match = sum(1 for o in outcomes if o.zpid_match)

    latencies = sorted(o.elapsed_ms for o in outcomes)
    p50 = latencies[len(latencies) // 2]
    p95_idx = max(0, int(len(latencies) * 0.95) - 1)
    p95 = latencies[p95_idx]
    mean_ms = int(statistics.mean(latencies))

    by_cat: dict[str, list[EvalOutcome]] = defaultdict(list)
    for o in outcomes:
        by_cat[o.case.category.value].append(o)

    per_category = tuple(
        sorted(
            (
                CategoryStats(
                    category=cat,
                    total=len(items),
                    correct=sum(1 for o in items if o.is_correct),
                    exact_value=sum(1 for o in items if o.value_exact_match),
                    within_1pct=sum(1 for o in items if o.value_within_1pct),
                    within_5pct=sum(1 for o in items if o.value_within_5pct),
                    status_match=sum(1 for o in items if o.status_match),
                    zpid_match=sum(1 for o in items if o.zpid_match),
                )
                for cat, items in by_cat.items()
            ),
            key=lambda s: s.category,
        )
    )

    return EvalSummary(
        total=total,
        correct=correct,
        exact_value=exact_value,
        within_1pct=w1,
        within_5pct=w5,
        status_match=status_match,
        zpid_match=zpid_match,
        p50_ms=p50,
        p95_ms=p95,
        mean_ms=mean_ms,
        per_category=per_category,
    )


# ─── Formatters ─────────────────────────────────────────────────


@dataclass(frozen=True)
class EvalReport:
    outcomes: list[EvalOutcome]
    summary: EvalSummary

    @classmethod
    def from_outcomes(cls, outcomes: list[EvalOutcome]) -> EvalReport:
        return cls(outcomes=outcomes, summary=summarize(outcomes))

    def to_json(self) -> str:
        return json.dumps(
            {
                "summary": self.summary.as_dict(),
                "cases": [
                    {
                        "id": o.case.id,
                        "address": o.case.address,
                        "category": o.case.category.value,
                        "mode": o.case.mode.value,
                        "expected_status": o.case.expected_status.value,
                        "expected_value": o.case.expected_value,
                        "expected_zpid": o.case.expected_zpid,
                        "tolerance_pct": o.case.tolerance_pct,
                        "actual_status": o.result.status.value,
                        "actual_value": o.result.value,
                        "actual_zpid": o.result.zpid,
                        "confidence": o.result.confidence,
                        "elapsed_ms": o.elapsed_ms,
                        "is_correct": o.is_correct,
                        "status_match": o.status_match,
                        "value_exact_match": o.value_exact_match,
                        "value_within_1pct": o.value_within_1pct,
                        "value_within_5pct": o.value_within_5pct,
                        "zpid_match": o.zpid_match,
                        "error": o.result.error,
                        "exception": o.exception,
                    }
                    for o in self.outcomes
                ],
            },
            indent=2,
        )

    def to_csv(self) -> str:
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(
            [
                "id",
                "category",
                "mode",
                "address",
                "expected_status",
                "expected_value",
                "actual_status",
                "actual_value",
                "is_correct",
                "status_match",
                "value_exact_match",
                "value_within_1pct",
                "elapsed_ms",
                "error",
            ]
        )
        for o in self.outcomes:
            writer.writerow(
                [
                    o.case.id,
                    o.case.category.value,
                    o.case.mode.value,
                    o.case.address,
                    o.case.expected_status.value,
                    o.case.expected_value or "",
                    o.result.status.value,
                    o.result.value or "",
                    o.is_correct,
                    o.status_match,
                    o.value_exact_match,
                    o.value_within_1pct,
                    o.elapsed_ms,
                    (o.result.error or "").replace("\n", " ")[:200],
                ]
            )
        return buf.getvalue()

    def failures(self) -> list[EvalOutcome]:
        return [o for o in self.outcomes if not o.is_correct]
