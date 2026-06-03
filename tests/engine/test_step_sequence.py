"""Tests for step sequence configuration: SUMMARIZE removal and steps.append."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from se3.engine.models import StepType, get_default_step_sequence
from se3.config import StepConfig, load_step_config, apply_step_config


class TestSummarizeNotInDefaults:
    """SUMMARIZE must not appear in any default step sequence."""

    @pytest.mark.parametrize("task_type", [
        "feature", "bugfix", "review", "small", "directive", "discovery",
    ])
    def test_summarize_not_in_default_sequence(self, task_type):
        steps = get_default_step_sequence(task_type)
        assert StepType.SUMMARIZE not in steps, (
            f"SUMMARIZE should not be in default sequence for {task_type}"
        )

    @pytest.mark.parametrize("task_type", [
        "feature", "bugfix", "review", "small", "directive", "discovery",
    ])
    def test_commit_is_last_step(self, task_type):
        """COMMIT should be the last step in every default sequence (after SUMMARIZE removal)."""
        steps = get_default_step_sequence(task_type)
        # review has no COMMIT step
        if task_type == "review":
            assert StepType.COMMIT not in steps
        else:
            assert steps[-1] == StepType.COMMIT


class TestSpecGateInDefaults:
    """Mechanism A: SPEC_GATE is inserted after UPDATE_SPEC (and before
    VERSION_ANALYZE) in the feature and discovery sequences only — the two
    task types whose default sequence runs UPDATE_SPEC."""

    @pytest.mark.parametrize("task_type", ["feature", "discovery"])
    def test_spec_gate_follows_update_spec(self, task_type):
        steps = get_default_step_sequence(task_type)
        assert StepType.SPEC_GATE in steps, (
            f"SPEC_GATE should be in the {task_type} sequence"
        )
        assert StepType.UPDATE_SPEC in steps
        # SPEC_GATE must come immediately after UPDATE_SPEC.
        update_idx = steps.index(StepType.UPDATE_SPEC)
        gate_idx = steps.index(StepType.SPEC_GATE)
        assert gate_idx == update_idx + 1, (
            "SPEC_GATE must immediately follow UPDATE_SPEC"
        )

    @pytest.mark.parametrize("task_type", ["feature", "discovery"])
    def test_spec_gate_precedes_version_analyze(self, task_type):
        steps = get_default_step_sequence(task_type)
        gate_idx = steps.index(StepType.SPEC_GATE)
        version_idx = steps.index(StepType.VERSION_ANALYZE)
        assert gate_idx < version_idx, (
            "SPEC_GATE must run before VERSION_ANALYZE"
        )
        assert version_idx == gate_idx + 1, (
            "VERSION_ANALYZE must immediately follow SPEC_GATE"
        )

    @pytest.mark.parametrize("task_type", ["bugfix", "review", "small", "directive"])
    def test_spec_gate_absent_when_no_update_spec(self, task_type):
        """Sequences without UPDATE_SPEC must not carry the gate."""
        steps = get_default_step_sequence(task_type)
        assert StepType.UPDATE_SPEC not in steps
        assert StepType.SPEC_GATE not in steps, (
            f"SPEC_GATE should not appear in the {task_type} sequence"
        )


class TestStepConfig:
    """StepConfig loading from se3.yaml."""

    def test_default_config_has_no_append_steps(self):
        config = StepConfig()
        assert config.append_steps == []

    def test_load_from_yaml(self, tmp_path):
        config_path = tmp_path / "se3.yaml"
        config_path.write_text(yaml.dump({"steps": {"append": ["summarize"]}}))
        config = StepConfig.load(tmp_path)
        assert config.append_steps == ["summarize"]

    def test_load_missing_file_returns_default(self, tmp_path):
        config = StepConfig.load(tmp_path)
        assert config.append_steps == []

    def test_load_empty_steps_section(self, tmp_path):
        config_path = tmp_path / "se3.yaml"
        config_path.write_text(yaml.dump({"steps": {}}))
        config = StepConfig.load(tmp_path)
        assert config.append_steps == []

    def test_load_invalid_append_type(self, tmp_path):
        config_path = tmp_path / "se3.yaml"
        config_path.write_text(yaml.dump({"steps": {"append": "not-a-list"}}))
        config = StepConfig.load(tmp_path)
        assert config.append_steps == []


class TestApplyStepConfig:
    """apply_step_config appends valid steps from se3.yaml."""

    def test_appends_summarize(self, tmp_path):
        config_path = tmp_path / "se3.yaml"
        config_path.write_text(yaml.dump({"steps": {"append": ["summarize"]}}))

        steps = get_default_step_sequence("feature")
        assert StepType.SUMMARIZE not in steps

        result = apply_step_config(steps, tmp_path)
        assert StepType.SUMMARIZE in result
        assert result[-1] == StepType.SUMMARIZE

    def test_no_duplicate_if_already_present(self, tmp_path):
        config_path = tmp_path / "se3.yaml"
        config_path.write_text(yaml.dump({"steps": {"append": ["commit"]}}))

        steps = get_default_step_sequence("feature")
        original_len = len(steps)

        result = apply_step_config(steps, tmp_path)
        # COMMIT is already in the sequence, should not be duplicated
        assert len(result) == original_len

    def test_ignores_invalid_step_names(self, tmp_path):
        config_path = tmp_path / "se3.yaml"
        config_path.write_text(yaml.dump({"steps": {"append": ["nonexistent_step"]}}))

        steps = get_default_step_sequence("feature")
        result = apply_step_config(steps, tmp_path)
        assert len(result) == len(steps)

    def test_no_config_returns_unchanged(self, tmp_path):
        steps = get_default_step_sequence("feature")
        result = apply_step_config(steps, tmp_path)
        assert result == steps

    def test_multiple_steps_appended(self, tmp_path):
        """Multiple valid steps can be appended."""
        config_path = tmp_path / "se3.yaml"
        config_path.write_text(yaml.dump({"steps": {"append": ["summarize"]}}))

        steps = get_default_step_sequence("review")
        result = apply_step_config(steps, tmp_path)
        assert StepType.SUMMARIZE in result
        # For review: ANALYZE, VERIFY_SPEC, + SUMMARIZE
        assert result[-1] == StepType.SUMMARIZE
