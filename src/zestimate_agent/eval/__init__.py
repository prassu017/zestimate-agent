"""Evaluation harness — correctness measurement against a curated dataset.

This package is the answer to "how do we know we're hitting the ≥99%
accuracy target?" It provides:

* `dataset.py` — hand-curated `EvalCase` records across property
  categories (SFH, condo, luxury, rural, new-construction, no-Zestimate,
  messy-input) with golden values we compare against.
* `runner.py` — async runner that executes a dataset against a
  `ZestimateAgent` in one of three modes (synthetic / fixture / live).
* `report.py` — stats aggregation + pretty / JSON / CSV formatters.

The harness is budget-aware by design: synthetic and fixture modes cost
zero credits and run on every test invocation; live mode is gated behind
`--mode live` and defaults to `--no-crosscheck` to protect the Rentcast cap.
"""

from __future__ import annotations

from zestimate_agent.eval.dataset import (
    DEFAULT_DATASET,
    EvalCase,
    EvalCategory,
    EvalMode,
    by_category,
    by_mode,
)
from zestimate_agent.eval.report import EvalReport, EvalSummary
from zestimate_agent.eval.runner import EvalOutcome, EvalRunConfig, run_eval

__all__ = [
    "DEFAULT_DATASET",
    "EvalCase",
    "EvalCategory",
    "EvalMode",
    "EvalOutcome",
    "EvalReport",
    "EvalRunConfig",
    "EvalSummary",
    "by_category",
    "by_mode",
    "run_eval",
]
