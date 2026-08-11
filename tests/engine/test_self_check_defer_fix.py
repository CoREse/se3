"""Tests for the self_check defer-fix mechanism (item 1) and the
nested-chain ``self_check_passes_required`` recording fix (item 3).

Covers:

Item 1 — defer-fix five paths:
  - few non-critical/high issues with a subsequent pass → COMPLETED + stash
  - critical/high issue → immediate REVISION_NEEDED with accumulated issues
  - chain-tail (last pass) merges the stash into one consolidated fix
  - signature dedup across passes (no duplicate in the fix list)
  - threshold=0 disables deferral (historical immediate-fix behavior)
  Plus: config parsing, threshold-reached immediate fix, clean-tail flush,
  and the state-machine stash lifecycle (reset at pass #1, carry-forward).

Item 3 — under a nested ``llm_caller.steps.self_check`` chain with no explicit
  ``self_check_passes_required``, the effective pass count (== chain length) is
  what gets injected and recorded in ``step.outputs['self_check_passes_required']``,
  and ``se3 history show``'s resolver returns the same effective value.
"""

from __future__ import annotations

import json
import pytest
import yaml
from pathlib import Path
from unittest.mock import Mock, patch

from tianluo.config import (
    ConfigError,
    WorkflowConfig,
    DEFAULT_SELF_CHECK_DEFER_FIX_THRESHOLD,
    effective_self_check_passes_required,
    resolve_self_check_passes_required,
    load_self_check_resolution,
)
from tianluo.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)
from tianluo.engine.state_machine import StateMachine
from tianluo.engine.steps.self_check import (
    self_check_handler,
    _merge_dedup_issues,
    _issue_signature,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _valid_issue(
    *,
    severity="medium",
    path="a.py",
    line=1,
    actual="broken behavior here",
    divergence="concrete failure mode",
    quote="Implement the defer feature",
):
    """A self_check issue that survives ``_validate_and_filter_issues``.

    ``quote`` must be a substring of the step's ``task_description`` (the
    source pool) and ``path`` must be one of ``changes_made.files_changed``.
    """
    return {
        "severity": severity,
        "actual_behavior": actual,
        "expected_behavior": "correct behavior",
        "divergence": divergence,
        "expectation_source": {"type": "task_description", "verbatim_quote": quote},
        "evidence_lines": [f"{path}:{line}"],
        "missing_in": [],
        "out_of_scope": False,
    }


_TASK = "Implement the defer feature and handle edge cases"


def _make_flow(tmp_path):
    return FlowInstance(
        flow_id="defer-flow",
        task_description=_TASK,
        task_type="feature",
        status=FlowStatus.RUNNING,
        change_path=tmp_path / "changes" / "c",
    )


def _make_step(
    *,
    pass_index,
    passes_required,
    threshold,
    deferred=None,
    fix_iteration=0,
):
    inputs = {
        "task_description": _TASK,
        "task_description_base": _TASK,
        "changes_made": {"files_changed": [{"path": "a.py", "action": "modify"}]},
        "test_results": {"passed": True, "returncode": 0},
        "spec_content": {},
        "self_check_pass_index": pass_index,
        "self_check_passes_required": passes_required,
        "self_check_defer_fix_threshold": threshold,
        "self_check_deferred_issues": deferred if deferred is not None else [],
        "max_fix_iterations": 10,
    }
    if fix_iteration:
        inputs["fix_iteration"] = fix_iteration
    return Step(
        step_type=StepType.SELF_CHECK,
        status=StepStatus.PENDING,
        inputs=inputs,
    )


def _run_handler(step, flow, issues):
    with patch("tianluo.engine.steps.self_check.LLMCaller") as mock_cls:
        mock_caller = Mock()
        mock_caller.call.return_value = json.dumps(
            {"issues": issues, "summary": "s"}
        )
        mock_cls.return_value = mock_caller
        return self_check_handler(step, flow)


# ---------------------------------------------------------------------------
# Task 1: config parsing of self_check_defer_fix_threshold
# ---------------------------------------------------------------------------


class TestDeferThresholdConfig:
    def test_default_is_zero(self):
        cfg = WorkflowConfig.from_dict({"workflow": {}})
        assert cfg.self_check_defer_fix_threshold == 0
        assert DEFAULT_SELF_CHECK_DEFER_FIX_THRESHOLD == 0

    def test_explicit_value(self):
        cfg = WorkflowConfig.from_dict(
            {"workflow": {"self_check_defer_fix_threshold": 5}}
        )
        assert cfg.self_check_defer_fix_threshold == 5

    def test_zero_disables(self):
        cfg = WorkflowConfig.from_dict(
            {"workflow": {"self_check_defer_fix_threshold": 0}}
        )
        assert cfg.self_check_defer_fix_threshold == 0

    def test_null_disables(self):
        cfg = WorkflowConfig.from_dict(
            {"workflow": {"self_check_defer_fix_threshold": None}}
        )
        assert cfg.self_check_defer_fix_threshold == 0

    def test_bool_warns_and_falls_back(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            cfg = WorkflowConfig.from_dict(
                {"workflow": {"self_check_defer_fix_threshold": True}}
            )
        assert cfg.self_check_defer_fix_threshold == 0
        assert "self_check_defer_fix_threshold" in caplog.text

    def test_float_warns_and_falls_back(self, caplog):
        import logging

        with caplog.at_level(logging.WARNING):
            cfg = WorkflowConfig.from_dict(
                {"workflow": {"self_check_defer_fix_threshold": 2.5}}
            )
        assert cfg.self_check_defer_fix_threshold == 0
        assert "self_check_defer_fix_threshold" in caplog.text

    def test_negative_fails_fast(self):
        with pytest.raises(ConfigError) as exc:
            WorkflowConfig.from_dict(
                {"workflow": {"self_check_defer_fix_threshold": -1}}
            )
        assert "self_check_defer_fix_threshold" in str(exc.value)


# ---------------------------------------------------------------------------
# Merge/dedup helper
# ---------------------------------------------------------------------------


class TestMergeDedup:
    def test_dedup_drops_matching_signature(self):
        a = _valid_issue(actual="alpha bug", path="a.py")
        b = _valid_issue(actual="beta bug", path="a.py")
        # b2 is a paraphrase-identical copy of a (same signature)
        a_copy = _valid_issue(actual="alpha bug", path="a.py")
        merged = _merge_dedup_issues([a], [a_copy, b])
        assert len(merged) == 2
        sigs = _issue_signature(merged)
        assert sigs == _issue_signature([a, b])

    def test_unsignable_issue_kept(self):
        # An issue with no location/description produces no signature → kept.
        empty = {"severity": "low"}
        merged = _merge_dedup_issues([], [empty, empty])
        assert len(merged) == 2


# ---------------------------------------------------------------------------
# Task 2: handler defer-fix decision (five paths)
# ---------------------------------------------------------------------------


class TestDeferTriggered:
    def test_few_noncritical_issues_defer(self, tmp_path):
        """< threshold non-critical issues, subsequent pass left → COMPLETED."""
        flow = _make_flow(tmp_path)
        step = _make_step(pass_index=1, passes_required=3, threshold=3)
        issues = [
            _valid_issue(severity="medium", actual="bug one"),
            _valid_issue(severity="low", actual="bug two"),
        ]
        result = _run_handler(step, flow, issues)
        assert result == StepStatus.COMPLETED
        assert step.outputs["self_check_deferred"] is True
        assert len(step.outputs["self_check_deferred_issues"]) == 2
        # No fix loop entered this pass.
        assert "fix_instructions" not in step.outputs


class TestCriticalHighImmediateFix:
    def test_high_severity_immediate_fix(self, tmp_path):
        flow = _make_flow(tmp_path)
        step = _make_step(pass_index=1, passes_required=3, threshold=3)
        issues = [_valid_issue(severity="high", actual="serious bug")]
        result = _run_handler(step, flow, issues)
        assert result == StepStatus.REVISION_NEEDED
        assert "serious bug" in step.outputs["fix_instructions"]
        assert step.outputs["fix_context"]["issues"]

    def test_critical_with_accumulated_stash(self, tmp_path):
        """A critical issue flushes the prior stash too (full accumulated set)."""
        flow = _make_flow(tmp_path)
        prior = _valid_issue(severity="medium", actual="earlier bug")
        step = _make_step(
            pass_index=2, passes_required=3, threshold=3, deferred=[prior],
        )
        issues = [_valid_issue(severity="critical", actual="critical bug")]
        result = _run_handler(step, flow, issues)
        assert result == StepStatus.REVISION_NEEDED
        fixed = step.outputs["fix_context"]["issues"]
        assert len(fixed) == 2
        assert "earlier bug" in step.outputs["fix_instructions"]
        assert "critical bug" in step.outputs["fix_instructions"]


class TestThresholdReachedImmediateFix:
    def test_count_at_threshold_fixes_now(self, tmp_path):
        flow = _make_flow(tmp_path)
        # threshold=2, two issues → len NOT < threshold → fix now.
        step = _make_step(pass_index=1, passes_required=3, threshold=2)
        issues = [
            _valid_issue(severity="medium", actual="bug one"),
            _valid_issue(severity="low", actual="bug two"),
        ]
        result = _run_handler(step, flow, issues)
        assert result == StepStatus.REVISION_NEEDED


class TestChainTailMergeIntoFix:
    def test_last_pass_merges_stash_and_current(self, tmp_path):
        flow = _make_flow(tmp_path)
        prior = _valid_issue(severity="medium", actual="stashed bug")
        step = _make_step(
            pass_index=3, passes_required=3, threshold=3, deferred=[prior],
        )
        # Even a single, sub-threshold, non-critical issue on the LAST pass
        # cannot defer (no subsequent pass) → flush merged.
        issues = [_valid_issue(severity="low", actual="final bug")]
        result = _run_handler(step, flow, issues)
        assert result == StepStatus.REVISION_NEEDED
        fixed = step.outputs["fix_context"]["issues"]
        assert len(fixed) == 2
        assert "stashed bug" in step.outputs["fix_instructions"]
        assert "final bug" in step.outputs["fix_instructions"]

    def test_clean_last_pass_flushes_stash(self, tmp_path):
        """Last pass finds nothing but the stash is non-empty → flush it."""
        flow = _make_flow(tmp_path)
        prior = _valid_issue(severity="medium", actual="only stashed bug")
        step = _make_step(
            pass_index=3, passes_required=3, threshold=3, deferred=[prior],
        )
        result = _run_handler(step, flow, [])  # no issues this pass
        assert result == StepStatus.REVISION_NEEDED
        assert "only stashed bug" in step.outputs["fix_instructions"]
        assert len(step.outputs["fix_context"]["issues"]) == 1


class TestSignatureDedupAcrossPasses:
    def test_duplicate_dropped_when_merging_into_fix(self, tmp_path):
        flow = _make_flow(tmp_path)
        dup = _valid_issue(severity="medium", actual="repeated bug", path="a.py")
        # Stash already contains the same logical issue.
        step = _make_step(
            pass_index=3, passes_required=3, threshold=3, deferred=[dup],
        )
        # LLM re-reports the identical issue plus a fresh one.
        fresh = _valid_issue(severity="low", actual="brand new bug", path="a.py")
        same = _valid_issue(severity="medium", actual="repeated bug", path="a.py")
        result = _run_handler(step, flow, [same, fresh])
        assert result == StepStatus.REVISION_NEEDED
        fixed = step.outputs["fix_context"]["issues"]
        # 1 deduped + 1 fresh == 2 (not 3)
        assert len(fixed) == 2


class TestThresholdZeroDisabled:
    def test_disabled_immediate_fix(self, tmp_path):
        flow = _make_flow(tmp_path)
        step = _make_step(pass_index=1, passes_required=3, threshold=0)
        issues = [_valid_issue(severity="medium", actual="lone bug")]
        result = _run_handler(step, flow, issues)
        # Historical behavior: any issue → fix immediately, no defer.
        assert result == StepStatus.REVISION_NEEDED
        assert step.outputs.get("self_check_deferred") is not True

    def test_disabled_clean_completes(self, tmp_path):
        flow = _make_flow(tmp_path)
        step = _make_step(pass_index=1, passes_required=3, threshold=0)
        result = _run_handler(step, flow, [])
        assert result == StepStatus.COMPLETED


class TestConvergenceSubordinateToDefer:
    """The convergence shortcut MUST NOT bypass the defer/fix arbitration when
    deferral is enabled (threshold > 0). With deferral on, every non-empty
    finding is accumulated (defer) or merged + fixed — never discarded by a
    COMPLETED convergence shortcut. Regression for the bug where three repeated
    medium issues at threshold 3 returned COMPLETED and lost the findings.
    """

    def _convergence_step(self, *, pass_index, passes_required, threshold,
                          prev_issues, deferred=None):
        step = _make_step(
            pass_index=pass_index,
            passes_required=passes_required,
            threshold=threshold,
            deferred=deferred,
        )
        step.inputs["self_check_convergence_enabled"] = True
        step.inputs["prev_self_check_issues"] = prev_issues
        return step

    def test_threshold_reached_converged_enters_fix_not_completed(self, tmp_path):
        """threshold=3, three converged medium issues → REVISION_NEEDED, not
        COMPLETED; the findings reach the fix list rather than being swallowed
        by the convergence shortcut."""
        flow = _make_flow(tmp_path)
        issues = [
            _valid_issue(severity="medium", actual="bug one", path="a.py", line=1),
            _valid_issue(severity="medium", actual="bug two", path="a.py", line=2),
            _valid_issue(severity="medium", actual="bug three", path="a.py", line=3),
        ]
        step = self._convergence_step(
            pass_index=1, passes_required=3, threshold=3, prev_issues=list(issues),
        )
        result = _run_handler(step, flow, issues)
        assert result == StepStatus.REVISION_NEEDED
        assert step.outputs.get("converged") is not True
        fixed = step.outputs["fix_context"]["issues"]
        assert len(fixed) == 3
        assert "bug one" in step.outputs["fix_instructions"]
        assert "bug three" in step.outputs["fix_instructions"]

    def test_accumulated_stash_not_discarded_by_convergence(self, tmp_path):
        """An accumulated deferred stash blocks the convergence shortcut: a
        below-threshold, non-last pass whose findings converge is still
        DEFERRED (stash preserved + grown) rather than COMPLETED-and-dropped,
        so the earlier deferred issues survive to a later flush/fix."""
        flow = _make_flow(tmp_path)
        prior = _valid_issue(severity="medium", actual="earlier stashed bug",
                             path="a.py", line=1)
        issues = [_valid_issue(severity="low", actual="recurring bug",
                               path="a.py", line=2)]
        step = self._convergence_step(
            pass_index=2, passes_required=3, threshold=3,
            prev_issues=list(issues), deferred=[prior],
        )
        result = _run_handler(step, flow, issues)
        assert result == StepStatus.COMPLETED
        # Deferred (stash preserved+grown), NOT converged-and-discarded.
        assert step.outputs.get("converged") is not True
        assert step.outputs.get("self_check_deferred") is True
        assert len(step.outputs["self_check_deferred_issues"]) == 2

    def test_below_threshold_no_stash_tail_pass_enters_fix_not_converged(self, tmp_path):
        """With deferral enabled, a below-threshold converged pass with NO
        pending stash is NOT exempt from the defer/fix arbitration: at the chain
        tail (last pass) it MUST enter the fix loop rather than be dropped by the
        convergence shortcut. Regression for the bug where pass 1/1 returned a
        converged COMPLETED and lost the lone recurring finding."""
        flow = _make_flow(tmp_path)
        issues = [_valid_issue(severity="low", actual="lone recurring bug", path="a.py")]
        step = self._convergence_step(
            pass_index=1, passes_required=1, threshold=3, prev_issues=list(issues),
        )
        result = _run_handler(step, flow, issues)
        assert result == StepStatus.REVISION_NEEDED
        assert step.outputs.get("converged") is not True
        assert "lone recurring bug" in step.outputs["fix_instructions"]

    def test_below_threshold_no_stash_nonlast_pass_defers_not_converged(self, tmp_path):
        """With deferral enabled, a below-threshold converged pass with NO
        pending stash on a NON-last pass MUST be deferred (stashed for a later
        consolidated fix) rather than dropped by the convergence shortcut."""
        flow = _make_flow(tmp_path)
        issues = [_valid_issue(severity="low", actual="lone recurring bug", path="a.py")]
        step = self._convergence_step(
            pass_index=1, passes_required=3, threshold=3, prev_issues=list(issues),
        )
        result = _run_handler(step, flow, issues)
        assert result == StepStatus.COMPLETED
        assert step.outputs.get("converged") is not True
        assert step.outputs.get("self_check_deferred") is True
        assert len(step.outputs["self_check_deferred_issues"]) == 1

    def test_convergence_still_applies_when_deferral_disabled(self, tmp_path):
        """threshold=0 (deferral off): the legacy convergence shortcut is
        preserved — converged non-critical issues return COMPLETED."""
        flow = _make_flow(tmp_path)
        issues = [_valid_issue(severity="medium", actual="recurring bug", path="a.py")]
        step = self._convergence_step(
            pass_index=1, passes_required=1, threshold=0, prev_issues=list(issues),
        )
        result = _run_handler(step, flow, issues)
        assert result == StepStatus.COMPLETED
        assert step.outputs.get("converged") is True


# ---------------------------------------------------------------------------
# Task 3: state-machine stash lifecycle
# ---------------------------------------------------------------------------


def _make_state_machine(tmp_path, cfg):
    with patch("tianluo.engine.state_machine.PersistenceManager"):
        sm = StateMachine(project_root=tmp_path)
    sm._get_workflow_config = lambda **kwargs: cfg
    return sm


def _flow_ready(tmp_path):
    flow = _make_flow(tmp_path)
    flow.state.selected_steps = [
        StepType.IMPLEMENT,
        StepType.TEST,
        StepType.SELF_CHECK,
        StepType.VERIFY_SPEC,
        StepType.COMMIT,
    ]
    impl = Step(step_type=StepType.IMPLEMENT, status=StepStatus.COMPLETED,
                outputs={"files_changed": ["src/a.py"]})
    flow.state.add_step(impl)
    test = Step(step_type=StepType.TEST, status=StepStatus.COMPLETED,
                outputs={"test_results": {"passed": True}})
    flow.state.add_step(test)
    return flow


class TestStashLifecycle:
    def test_threshold_injected_into_inputs(self, tmp_path):
        cfg = WorkflowConfig(
            self_check_passes_required=3, self_check_defer_fix_threshold=4,
        )
        sm = _make_state_machine(tmp_path, cfg)
        flow = _flow_ready(tmp_path)
        inputs = sm._build_step_inputs(flow, StepType.SELF_CHECK)
        assert inputs["self_check_defer_fix_threshold"] == 4

    def test_pass_one_resets_stale_stash(self, tmp_path):
        cfg = WorkflowConfig(
            self_check_passes_required=3, self_check_defer_fix_threshold=3,
        )
        sm = _make_state_machine(tmp_path, cfg)
        flow = _flow_ready(tmp_path)
        # Stale stash from a prior round.
        flow.state.context["self_check_deferred_issues"] = [{"severity": "low"}]
        inputs = sm._build_step_inputs(flow, StepType.SELF_CHECK)  # pass #1
        assert inputs["self_check_pass_index"] == 1
        assert inputs["self_check_deferred_issues"] == []
        assert flow.state.context["self_check_deferred_issues"] == []

    def test_stash_carries_forward_across_passes(self, tmp_path):
        cfg = WorkflowConfig(
            self_check_passes_required=3, self_check_defer_fix_threshold=3,
        )
        sm = _make_state_machine(tmp_path, cfg)
        flow = _flow_ready(tmp_path)

        i1 = _valid_issue(actual="bug one")
        sc1 = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.COMPLETED,
            outputs={
                "self_check_deferred_issues": [i1],
                "self_check_deferred": True,
                "issues": [],
                "actionable_count": 0,
            },
        )
        flow.state.add_step(sc1)
        flow.state.current_step_id = sc1.step_id

        sc2 = sm.transition_to_next(flow)
        assert sc2.step_type == StepType.SELF_CHECK
        assert sc2.inputs["self_check_pass_index"] == 2
        assert sc2.inputs["self_check_deferred_issues"] == [i1]
        assert flow.state.context["self_check_deferred_issues"] == [i1]

        # Pass 2 defers another issue → stash grows.
        i2 = _valid_issue(actual="bug two")
        sc2.status = StepStatus.COMPLETED
        sc2.outputs = {
            "self_check_deferred_issues": [i1, i2],
            "self_check_deferred": True,
            "issues": [],
            "actionable_count": 0,
        }
        flow.state.current_step_id = sc2.step_id

        sc3 = sm.transition_to_next(flow)
        assert sc3.step_type == StepType.SELF_CHECK
        assert sc3.inputs["self_check_pass_index"] == 3
        assert sc3.inputs["self_check_deferred_issues"] == [i1, i2]


# ---------------------------------------------------------------------------
# Task 3/4: nested-chain effective passes_required recording (item 3)
# ---------------------------------------------------------------------------


def _write_nested_project(tmp_path, *, explicit_passes=None):
    cfg = {
        "agents": {"a": {"cmd": "echo"}, "b": {"cmd": "echo"}},
        "llm_caller": {"defaults": ["a"], "steps": {"self_check": [["a"], ["b"]]}},
        "workflow": {},
    }
    if explicit_passes is not None:
        cfg["workflow"]["self_check_passes_required"] = explicit_passes
    (tmp_path / "tianluo.yaml").write_text(yaml.safe_dump(cfg))
    return tmp_path


class TestNestedChainPassesRequiredRecording:
    def test_effective_helper_uses_chain_count(self, tmp_path):
        _write_nested_project(tmp_path)
        cfg = WorkflowConfig.load(tmp_path)
        resolution = load_self_check_resolution(tmp_path)
        assert resolution.form == "nested"
        assert effective_self_check_passes_required(cfg, resolution) == 2

    def test_resolver_returns_chain_count(self, tmp_path):
        _write_nested_project(tmp_path)
        assert resolve_self_check_passes_required(tmp_path) == 2

    def test_explicit_count_wins(self, tmp_path):
        _write_nested_project(tmp_path, explicit_passes=4)
        assert resolve_self_check_passes_required(tmp_path) == 4

    def test_flat_or_default_unchanged(self, tmp_path):
        # No self_check override → falls back to the configured count (1).
        (tmp_path / "tianluo.yaml").write_text(yaml.safe_dump({"workflow": {}}))
        assert resolve_self_check_passes_required(tmp_path) == 1

    def test_state_machine_injects_chain_count(self, tmp_path):
        _write_nested_project(tmp_path)
        with patch("tianluo.engine.state_machine.PersistenceManager"):
            sm = StateMachine(project_root=tmp_path)
        sm._workflow_config_cache = None
        sm._self_check_resolution_cache = None
        assert sm._get_self_check_passes_required() == 2
        flow = _flow_ready(tmp_path)
        inputs = sm._build_step_inputs(flow, StepType.SELF_CHECK)
        assert inputs["self_check_passes_required"] == 2

    def test_handler_records_effective_passes_required(self, tmp_path):
        """End-to-end: the value injected (2) is what the handler records."""
        _write_nested_project(tmp_path)
        with patch("tianluo.engine.state_machine.PersistenceManager"):
            sm = StateMachine(project_root=tmp_path)
        sm._workflow_config_cache = None
        sm._self_check_resolution_cache = None
        flow = _flow_ready(tmp_path)
        inputs = sm._build_step_inputs(flow, StepType.SELF_CHECK)
        step = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.PENDING,
            inputs=inputs,
        )
        result = _run_handler(step, flow, [])
        assert result == StepStatus.COMPLETED
        assert step.outputs["self_check_passes_required"] == 2
