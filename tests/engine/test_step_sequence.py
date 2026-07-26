"""Tests for step sequence configuration: SUMMARIZE removal and steps.append."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from tianluo.engine.models import StepType, get_default_step_sequence
from tianluo.config import StepConfig, load_step_config, apply_step_config


ALL_TASK_TYPES = ["feature", "bugfix", "review", "small", "directive", "discovery"]


class TestSummarizeInDefaults:
    """SUMMARIZE is the final step of every default step sequence."""

    @pytest.mark.parametrize("task_type", ALL_TASK_TYPES)
    def test_summarize_in_default_sequence(self, task_type):
        steps = get_default_step_sequence(task_type)
        assert StepType.SUMMARIZE in steps, (
            f"SUMMARIZE should be in default sequence for {task_type}"
        )

    @pytest.mark.parametrize("task_type", ALL_TASK_TYPES)
    def test_summarize_is_last_step(self, task_type):
        """SUMMARIZE is the last step in every default sequence."""
        steps = get_default_step_sequence(task_type)
        assert steps[-1] == StepType.SUMMARIZE, (
            f"SUMMARIZE should be the last step for {task_type}"
        )

    @pytest.mark.parametrize("task_type", ALL_TASK_TYPES)
    def test_summarize_appears_once(self, task_type):
        steps = get_default_step_sequence(task_type)
        assert steps.count(StepType.SUMMARIZE) == 1

    @pytest.mark.parametrize(
        "task_type", ["feature", "bugfix", "small", "directive", "discovery"]
    )
    def test_summarize_follows_commit(self, task_type):
        """For non-review sequences SUMMARIZE immediately follows COMMIT."""
        steps = get_default_step_sequence(task_type)
        commit_idx = steps.index(StepType.COMMIT)
        summarize_idx = steps.index(StepType.SUMMARIZE)
        assert summarize_idx == commit_idx + 1, (
            "SUMMARIZE must immediately follow COMMIT"
        )

    def test_review_summarize_follows_invariant_check(self):
        """The review sequence has no COMMIT; SUMMARIZE follows INVARIANT_CHECK.

        Charter refactor: the review flow is ANALYZE → INVARIANT_CHECK → SUMMARIZE
        (the retired VERIFY_SPEC is replaced by the anchored INVARIANT_CHECK).
        """
        steps = get_default_step_sequence("review")
        assert StepType.COMMIT not in steps
        assert steps[-1] == StepType.SUMMARIZE
        invariant_idx = steps.index(StepType.INVARIANT_CHECK)
        assert steps.index(StepType.SUMMARIZE) == invariant_idx + 1

    def test_unknown_task_type_falls_back_to_feature_with_summarize(self):
        """An unknown task type falls back to the feature sequence, ending in SUMMARIZE."""
        steps = get_default_step_sequence("not-a-real-type")
        assert steps == get_default_step_sequence("feature")
        assert steps[-1] == StepType.SUMMARIZE


class TestCharterStepsInDefaults:
    """Charter refactor: the retired spec governance steps (VERIFY_SPEC /
    UPDATE_SPEC / SPEC_GATE) are gone; INVARIANT_CHECK follows SELF_CHECK and
    CHARTER_FRESHNESS precedes VERSION_ANALYZE."""

    @pytest.mark.parametrize("task_type", ALL_TASK_TYPES)
    def test_retired_spec_steps_absent(self, task_type):
        steps = get_default_step_sequence(task_type)
        for retired in (StepType.VERIFY_SPEC, StepType.UPDATE_SPEC, StepType.SPEC_GATE):
            assert retired not in steps, (
                f"{retired.value} should not appear in the {task_type} sequence"
            )

    @pytest.mark.parametrize("task_type", ["feature", "bugfix", "discovery"])
    def test_invariant_check_follows_self_check(self, task_type):
        steps = get_default_step_sequence(task_type)
        sc_idx = steps.index(StepType.SELF_CHECK)
        ic_idx = steps.index(StepType.INVARIANT_CHECK)
        assert ic_idx == sc_idx + 1, "INVARIANT_CHECK must immediately follow SELF_CHECK"

    @pytest.mark.parametrize("task_type", ["feature", "bugfix", "discovery", "small", "directive"])
    def test_charter_freshness_precedes_version_analyze(self, task_type):
        steps = get_default_step_sequence(task_type)
        cf_idx = steps.index(StepType.CHARTER_FRESHNESS)
        version_idx = steps.index(StepType.VERSION_ANALYZE)
        assert version_idx == cf_idx + 1, (
            "VERSION_ANALYZE must immediately follow CHARTER_FRESHNESS"
        )

    def test_review_has_invariant_check_no_charter_freshness(self):
        """The commit-less review flow gets INVARIANT_CHECK but no CHARTER_FRESHNESS."""
        steps = get_default_step_sequence("review")
        assert StepType.INVARIANT_CHECK in steps
        assert StepType.CHARTER_FRESHNESS not in steps

    @pytest.mark.parametrize("task_type", ["small", "directive"])
    def test_lightweight_flows_have_charter_freshness_only(self, task_type):
        """Lightweight commit-only flows get CHARTER_FRESHNESS but not INVARIANT_CHECK."""
        steps = get_default_step_sequence(task_type)
        assert StepType.CHARTER_FRESHNESS in steps
        assert StepType.INVARIANT_CHECK not in steps


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

    @pytest.mark.parametrize("task_type", ALL_TASK_TYPES)
    def test_append_summarize_is_noop(self, task_type, tmp_path, caplog):
        """`steps.append: [summarize]` is a no-op now that SUMMARIZE is a
        default step: no duplication, no warning, position preserved."""
        config_path = tmp_path / "se3.yaml"
        config_path.write_text(yaml.dump({"steps": {"append": ["summarize"]}}))

        steps = get_default_step_sequence(task_type)
        assert StepType.SUMMARIZE in steps

        import logging

        with caplog.at_level(logging.WARNING):
            result = apply_step_config(steps, tmp_path)

        # No duplication: SUMMARIZE still appears exactly once and remains last.
        assert result.count(StepType.SUMMARIZE) == 1
        assert result[-1] == StepType.SUMMARIZE
        assert result == steps
        # No warning emitted for the now-redundant append.
        assert not any(
            "summarize" in rec.getMessage().lower() for rec in caplog.records
        )

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
