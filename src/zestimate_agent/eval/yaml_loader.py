"""Load eval cases from a YAML file — so non-engineers can add cases.

The YAML schema mirrors the `EvalCase` dataclass exactly. A Pydantic model
validates each entry on load and produces clear errors on typos / missing
fields.

Example YAML::

    cases:
      - id: custom-sfh
        address: "123 Main St, Seattle, WA 98101"
        category: sfh
        mode: live
        expected_status: ok
        expected_value: 750000
        tolerance_pct: 1.0
        notes: "added by PM for regression"
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

from zestimate_agent.eval.dataset import EvalCase, EvalCategory, EvalMode
from zestimate_agent.models import ZestimateStatus


class YamlEvalCase(BaseModel):
    """Pydantic schema for a single eval case in YAML."""

    id: str
    address: str
    category: str
    mode: str = "synthetic"

    expected_status: str = "ok"
    expected_value: int | None = None
    expected_zpid: str | None = None
    tolerance_pct: float = 0.0

    synthetic_html: str | None = None
    fixture_html_file: str | None = None
    canned_zpid: str | None = None
    canned_url: str | None = None

    notes: str = ""
    tags: list[str] = Field(default_factory=list)

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str) -> str:
        try:
            EvalCategory(v)
        except ValueError as e:
            valid = ", ".join(c.value for c in EvalCategory)
            raise ValueError(f"invalid category '{v}' — must be one of: {valid}") from e
        return v

    @field_validator("mode")
    @classmethod
    def validate_mode(cls, v: str) -> str:
        try:
            EvalMode(v)
        except ValueError as e:
            valid = ", ".join(m.value for m in EvalMode)
            raise ValueError(f"invalid mode '{v}' — must be one of: {valid}") from e
        return v

    @field_validator("expected_status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        try:
            ZestimateStatus(v)
        except ValueError as e:
            valid = ", ".join(s.value for s in ZestimateStatus)
            raise ValueError(f"invalid expected_status '{v}' — must be one of: {valid}") from e
        return v

    def to_eval_case(self) -> EvalCase:
        return EvalCase(
            id=self.id,
            address=self.address,
            category=EvalCategory(self.category),
            mode=EvalMode(self.mode),
            expected_status=ZestimateStatus(self.expected_status),
            expected_value=self.expected_value,
            expected_zpid=self.expected_zpid,
            tolerance_pct=self.tolerance_pct,
            synthetic_html=self.synthetic_html,
            fixture_html_file=self.fixture_html_file,
            canned_zpid=self.canned_zpid,
            canned_url=self.canned_url,
            notes=self.notes,
            tags=tuple(self.tags),
        )


class YamlDataset(BaseModel):
    """Top-level YAML schema: just a list of cases."""

    cases: list[YamlEvalCase]


def load_yaml_dataset(path: Path) -> tuple[EvalCase, ...]:
    """Load and validate eval cases from a YAML file.

    Raises ``pydantic.ValidationError`` on schema violations with
    clear field-level error messages.
    """
    raw: dict[str, Any] = yaml.safe_load(path.read_text())
    dataset = YamlDataset.model_validate(raw)
    return tuple(c.to_eval_case() for c in dataset.cases)
