"""Tests for plan-confirm always-on wiring in ``tianluo.config``.

plan-confirm is a mechanical requirement-coverage guarantee that must run
regardless of whether ``confirmation.steps`` contains a ``plan`` entry (or
is empty entirely). These tests pin two config-layer behaviors:

- ``insert_confirmation_steps`` inserts exactly one CONFIRM after every plan
  step even when ``confirmation.steps`` is empty / has no plan entry, while
  non-plan steps stay config-driven and never double-confirm plan.
- ``resolve_confirm_inputs('plan')`` synthesizes a default entry
  (reviewer=None → default llm_caller chain, default max_iterations) when no
  ``confirmation.steps.plan`` is configured, honors an explicit plan entry's
  reviewer / max_iterations, and still returns None for unconfigured
  non-plan steps so state_machine's resolved-is-None→human fallback only
  fires off the plan path.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tianluo.config import (  # noqa: E402
    _CONFIRM_DEFAULT_MAX_ITERATIONS,
    insert_confirmation_steps,
    resolve_confirm_inputs,
)
from tianluo.engine.models import StepType  # noqa: E402


@pytest.fixture
def isolated_global_home(monkeypatch, tmp_path):
    """Neutralize the real ``~/.se3/config.yaml`` by pointing home at a
    clean temp dir, so only the project's tianluo.yaml (if any) is in play."""
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    return fake_home


# ---------------------------------------------------------------------------
# insert_confirmation_steps: plan always-on
# ---------------------------------------------------------------------------


class TestInsertPlanAlwaysOn:
    def test_no_se3_yaml_still_confirms_plan(self, tmp_path, isolated_global_home):
        # No tianluo.yaml at all → confirmation.steps is empty, yet plan must
        # still be confirmed.
        result = insert_confirmation_steps(
            [StepType.ANALYZE, StepType.PLAN, StepType.IMPLEMENT], tmp_path,
        )
        plan_idx = result.index(StepType.PLAN)
        assert result[plan_idx + 1] == StepType.CONFIRM
        assert result.count(StepType.CONFIRM) == 1

    def test_empty_steps_dict_still_confirms_plan(self, tmp_path, isolated_global_home):
        (tmp_path / "tianluo.yaml").write_text("confirmation: {steps: {}}\n")
        result = insert_confirmation_steps(
            [StepType.PLAN, StepType.IMPLEMENT], tmp_path,
        )
        plan_idx = result.index(StepType.PLAN)
        assert result[plan_idx + 1] == StepType.CONFIRM
        assert result.count(StepType.CONFIRM) == 1

    def test_steps_without_plan_entry_still_confirms_plan(
        self, tmp_path, isolated_global_home
    ):
        # confirmation.steps lists a non-plan step; plan has no entry but is
        # still confirmed, and the configured non-plan step is confirmed too.
        (tmp_path / "tianluo.yaml").write_text(
            "confirmation:\n"
            "  steps:\n"
            "    implement: {reviewer: human}\n"
        )
        result = insert_confirmation_steps(
            [StepType.PLAN, StepType.IMPLEMENT], tmp_path,
        )
        plan_idx = result.index(StepType.PLAN)
        impl_idx = result.index(StepType.IMPLEMENT)
        assert result[plan_idx + 1] == StepType.CONFIRM
        assert result[impl_idx + 1] == StepType.CONFIRM
        assert result.count(StepType.CONFIRM) == 2

    def test_explicit_plan_entry_does_not_double_confirm(
        self, tmp_path, isolated_global_home
    ):
        # An explicit plan entry must not stack a second CONFIRM on top of
        # the always-on one.
        (tmp_path / "tianluo.yaml").write_text(
            "confirmation:\n"
            "  steps:\n"
            "    plan: {reviewer: human}\n"
        )
        result = insert_confirmation_steps(
            [StepType.PLAN, StepType.IMPLEMENT], tmp_path,
        )
        plan_idx = result.index(StepType.PLAN)
        assert result[plan_idx + 1] == StepType.CONFIRM
        assert result.count(StepType.CONFIRM) == 1

    def test_non_plan_step_not_confirmed_when_unconfigured(
        self, tmp_path, isolated_global_home
    ):
        # No plan in the sequence and an empty config → no CONFIRM at all.
        (tmp_path / "tianluo.yaml").write_text("confirmation: {steps: {}}\n")
        result = insert_confirmation_steps(
            [StepType.IMPLEMENT, StepType.TEST], tmp_path,
        )
        assert StepType.CONFIRM not in result


# ---------------------------------------------------------------------------
# resolve_confirm_inputs: plan default synthesis vs explicit override
# ---------------------------------------------------------------------------


class TestResolvePlanInputs:
    def test_plan_unconfigured_synthesizes_default_llm_entry(
        self, tmp_path, isolated_global_home
    ):
        # No tianluo.yaml → plan must resolve to the default LLM chain, NOT None
        # (which would trip state_machine's human fallback).
        #
        # The builtin chain probes PATH, so pin which commands resolve; without
        # this the assertion would silently track whichever agents happen to be
        # installed on the host.
        with patch(
            "tianluo.config.shutil.which",
            side_effect=lambda cmd: "/usr/bin/claude" if cmd == "claude" else None,
        ):
            resolved = resolve_confirm_inputs(tmp_path, "plan")
        assert resolved is not None
        assert resolved["reviewer"] is None
        # only claude is on PATH, so the builtin chain narrows to it
        assert resolved["agents"] == [
            {
                "name": "claude",
                "type": "claude-code",
                "cmd": "claude",
                "priority": 0,
                "provider": "anthropic",
            }
        ]
        # resolve_confirm_inputs is the single source of truth for CONFIRM
        # inputs, so it already bakes in the concrete default rather than
        # deferring to the state_machine None→default fallback.
        assert resolved["max_iterations"] == _CONFIRM_DEFAULT_MAX_ITERATIONS

    def test_plan_unconfigured_matches_empty_plan_entry(
        self, tmp_path, isolated_global_home
    ):
        # The synthesized default must be indistinguishable from an explicit
        # empty ``plan: {}`` entry, so behavior is identical whether the entry
        # is present-but-empty or absent.
        no_entry = resolve_confirm_inputs(tmp_path, "plan")

        (tmp_path / "tianluo.yaml").write_text(
            "confirmation:\n"
            "  steps:\n"
            "    plan: {}\n"
        )
        empty_entry = resolve_confirm_inputs(tmp_path, "plan")
        assert no_entry == empty_entry

    def test_plan_explicit_reviewer_and_iterations_override(
        self, tmp_path, isolated_global_home
    ):
        # A human reviewer + custom max_iterations must be honored — the
        # always-on default never clobbers an explicit operator choice.
        (tmp_path / "tianluo.yaml").write_text(
            "confirmation:\n"
            "  steps:\n"
            "    plan: {reviewer: human, max_iterations: 5}\n"
        )
        resolved = resolve_confirm_inputs(tmp_path, "plan")
        assert resolved is not None
        assert resolved["reviewer"] == "human"
        assert resolved["max_iterations"] == 5
        assert resolved["agents"] is None

    def test_non_plan_unconfigured_returns_none(
        self, tmp_path, isolated_global_home
    ):
        # Non-plan steps remain config-driven: unconfigured → None, so the
        # human fallback path is preserved for them.
        assert resolve_confirm_inputs(tmp_path, "implement") is None

    def test_non_plan_configured_resolves_entry(
        self, tmp_path, isolated_global_home
    ):
        # Sanity: explicit non-plan config still resolves normally and is not
        # affected by the plan special-casing.
        (tmp_path / "tianluo.yaml").write_text(
            "confirmation:\n"
            "  steps:\n"
            "    implement: {reviewer: human}\n"
        )
        resolved = resolve_confirm_inputs(tmp_path, "implement")
        assert resolved is not None
        assert resolved["reviewer"] == "human"
