"""Tests for the YAML eval dataset loader."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest
from pydantic import ValidationError

from zestimate_agent.eval.dataset import EvalCategory, EvalMode
from zestimate_agent.eval.yaml_loader import YamlEvalCase, load_yaml_dataset
from zestimate_agent.models import ZestimateStatus


class TestYamlEvalCase:
    def test_valid_case(self) -> None:
        case = YamlEvalCase(
            id="test-1",
            address="123 Main St, Test, CA 99999",
            category="sfh",
            mode="synthetic",
            expected_status="ok",
            expected_value=500_000,
        )
        ec = case.to_eval_case()
        assert ec.id == "test-1"
        assert ec.category == EvalCategory.SFH
        assert ec.mode == EvalMode.SYNTHETIC
        assert ec.expected_status == ZestimateStatus.OK
        assert ec.expected_value == 500_000

    def test_invalid_category_raises(self) -> None:
        with pytest.raises(ValidationError, match="invalid category"):
            YamlEvalCase(
                id="bad",
                address="123 Test",
                category="nonexistent_type",
            )

    def test_invalid_mode_raises(self) -> None:
        with pytest.raises(ValidationError, match="invalid mode"):
            YamlEvalCase(
                id="bad",
                address="123 Test",
                category="sfh",
                mode="turbo",
            )

    def test_invalid_status_raises(self) -> None:
        with pytest.raises(ValidationError, match="invalid expected_status"):
            YamlEvalCase(
                id="bad",
                address="123 Test",
                category="sfh",
                expected_status="super_ok",
            )

    def test_defaults(self) -> None:
        case = YamlEvalCase(id="x", address="addr", category="sfh")
        assert case.mode == "synthetic"
        assert case.expected_status == "ok"
        assert case.tolerance_pct == 0.0
        assert case.tags == []

    def test_tags_converted_to_tuple(self) -> None:
        case = YamlEvalCase(
            id="t", address="a", category="sfh", tags=["regression", "p0"]
        )
        ec = case.to_eval_case()
        assert ec.tags == ("regression", "p0")


class TestLoadYamlDataset:
    def test_load_valid_file(self, tmp_path: Path) -> None:
        yaml_content = dedent("""\
            cases:
              - id: yaml-test-1
                address: "1 YAML St, Test, CA 99999"
                category: sfh
                mode: synthetic
                expected_status: ok
                expected_value: 300000
              - id: yaml-test-2
                address: "2 YAML St, Test, CA 99999"
                category: not_found
                mode: live
                expected_status: not_found
                notes: "should fail gracefully"
        """)
        path = tmp_path / "test_cases.yaml"
        path.write_text(yaml_content)

        cases = load_yaml_dataset(path)
        assert len(cases) == 2
        assert cases[0].id == "yaml-test-1"
        assert cases[0].expected_value == 300_000
        assert cases[1].expected_status == ZestimateStatus.NOT_FOUND

    def test_load_invalid_file_raises(self, tmp_path: Path) -> None:
        yaml_content = dedent("""\
            cases:
              - id: bad
                address: "test"
                category: fake_category
        """)
        path = tmp_path / "bad.yaml"
        path.write_text(yaml_content)

        with pytest.raises(ValidationError):
            load_yaml_dataset(path)

    def test_load_empty_cases(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.yaml"
        path.write_text("cases: []\n")
        cases = load_yaml_dataset(path)
        assert cases == ()
