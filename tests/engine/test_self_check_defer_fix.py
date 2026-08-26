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
    _describe_issue,
    _has_critical_or_high,
    _fold_still_present_into_current,
    _classify_still_present_prev_issues,
    _FOLD_MARKER,
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

    def test_duplicate_carrying_nothing_is_dropped_exactly_as_before(self):
        a = _valid_issue(actual="alpha bug", path="a.py")
        a_copy = _valid_issue(actual="alpha bug", path="a.py",
                              quote="handle edge cases")
        merged = _merge_dedup_issues([a], [a_copy])
        assert merged == [a]


class TestDedupCarryBlockTransfer:
    """A deduped-away duplicate must not take a carried statement with it.

    The dedup RULE is untouched — same signature, same survivor. What the
    fold adds is that a duplicate may be carrying a previous finding whose
    only route into the fix loop is that carry region, so the region moves
    onto the survivor before the duplicate is discarded.
    """

    @staticmethod
    def _carrying(*, own="shared wording", severity="medium",
                  prev_severity="medium", prev_actual="previous round wording"):
        prev = _valid_issue(actual=prev_actual, path="a.py",
                            severity=prev_severity, quote="handle edge cases")
        current = _valid_issue(actual=own, path="a.py", severity=severity)
        folded = _fold_still_present_into_current([current], [prev], [True])
        assert folded[2] == 1
        return folded[0][0], prev

    def test_carried_statement_moves_onto_the_survivor(self):
        stashed = _valid_issue(actual="shared wording", path="a.py")
        carrying, prev = self._carrying()
        merged = _merge_dedup_issues([stashed], [carrying])
        # Same dedup verdict as without the fold: one entry, the existing one.
        assert len(merged) == 1
        assert merged[0]["actual_behavior"] == "shared wording"
        # ... but the statement it was carrying is still readable.
        assert prev["actual_behavior"] in merged[0]["divergence"]
        assert (
            prev["expectation_source"]["verbatim_quote"]
            in merged[0]["divergence"]
        )
        assert prev["evidence_lines"][0] in merged[0]["divergence"]

    def test_transfer_does_not_shift_the_survivor_identity(self):
        from tianluo.engine.steps.self_check import _issue_identity_tokens

        stashed = _valid_issue(actual="shared wording", path="a.py")
        carrying, _prev = self._carrying()
        merged = _merge_dedup_issues([stashed], [carrying])
        assert _issue_signature(merged) == _issue_signature([stashed])
        assert (
            _issue_identity_tokens(merged[0])
            == _issue_identity_tokens(stashed)
        )

    def test_transfer_raises_the_survivor_severity(self):
        stashed = _valid_issue(actual="shared wording", path="a.py",
                               severity="medium")
        carrying, _prev = self._carrying(prev_severity="critical")
        merged = _merge_dedup_issues([stashed], [carrying])
        assert merged[0]["severity"] == "critical"
        assert _has_critical_or_high(merged) is True
        # The stashed dict itself is an audit record; it is copied, not mutated.
        assert stashed["severity"] == "medium"

    def test_transfer_never_lowers_the_survivor_severity(self):
        stashed = _valid_issue(actual="shared wording", path="a.py",
                               severity="critical")
        carrying, _prev = self._carrying(severity="low", prev_severity="low")
        merged = _merge_dedup_issues([stashed], [carrying])
        assert merged[0]["severity"] == "critical"

    def test_transfer_stacks_behind_what_the_survivor_already_carries(self):
        survivor, survivor_prev = self._carrying(prev_actual="survivor's older statement")
        carrying, dup_prev = self._carrying(prev_actual="duplicate's older statement")
        merged = _merge_dedup_issues([survivor], [carrying])
        assert len(merged) == 1
        rendered = merged[0]["divergence"]
        assert rendered.count(_FOLD_MARKER) == 2
        for statement in (survivor_prev, dup_prev):
            assert statement["actual_behavior"] in rendered


class TestFoldStillPresentIntoCurrent:
    """Position-keyed fold-in of ``still_present`` previous findings.

    The reviewer is REQUIRED by the prompt to re-list a ``still_present``
    finding while the same finding is also re-admitted verbatim, so both
    arrivals are structural. The fold joins them on evidence position alone —
    never on whether two descriptions read alike.
    """

    @staticmethod
    def _assert_carries(rendered, *statements):
        """Every field of every folded statement must be readable."""
        for statement in statements:
            for key in ("actual_behavior", "expected_behavior", "divergence"):
                value = statement.get(key)
                if value:
                    assert value in rendered
            for line in statement.get("evidence_lines") or []:
                assert line in rendered
            source = statement.get("expectation_source") or {}
            if source.get("verbatim_quote"):
                assert source["verbatim_quote"] in rendered

    def test_same_position_folds_without_mutating_the_original(self):
        current = _valid_issue(actual="new wording", path="a.py",
                               divergence="this round's failure mode")
        prev = _valid_issue(actual="old wording", path="a.py",
                            divergence="last round's failure mode",
                            quote="handle edge cases")
        original_current = dict(current)
        original_prev = dict(prev)
        issues, unfolded, folded = _fold_still_present_into_current(
            [current], [prev], [True],
        )
        assert folded == 1
        assert unfolded == []
        assert len(issues) == 1
        assert issues[0]["actual_behavior"] == "new wording"
        # The fold is lossless: both statements, both expectation-source
        # quotes and both evidence citations survive in the one finding.
        self._assert_carries(issues[0]["divergence"], prev)
        # This round's own wording still leads; the folded one follows it.
        assert issues[0]["divergence"].startswith(current["divergence"])
        # The raw_issues audit records must stay verbatim.
        assert current == original_current
        assert prev == original_prev

    def test_folded_statement_keeps_unknown_schema_fields(self):
        # Losslessness must not depend on a hard-coded field list.
        current = _valid_issue(actual="new wording", path="a.py")
        prev = dict(_valid_issue(actual="old wording", path="a.py"),
                    some_future_field="a value only the newer schema knows")
        issues, _unfolded, folded = _fold_still_present_into_current(
            [current], [prev], [True],
        )
        assert folded == 1
        assert "a value only the newer schema knows" in issues[0]["divergence"]

    def test_fold_does_not_shift_the_signature_dedup_key(self):
        # The fold writes into ``divergence``, which the signature dedup reads.
        # That key is matched by EQUALITY, so widening it would stop a
        # re-report from matching its own earlier self.
        current = _valid_issue(actual="new wording", path="a.py",
                               divergence="this round's failure mode")
        prev = _valid_issue(actual="old wording", path="a.py",
                            divergence="last round's failure mode")
        issues, _unfolded, folded = _fold_still_present_into_current(
            [current], [prev], [True],
        )
        assert folded == 1
        assert _issue_signature(issues) == _issue_signature([current])

    def test_fold_leaves_the_pairing_identity_unchanged(self):
        # A finding's identity is its OWN wording, before and after a fold.
        # Widening it with carried text could tie a resolution summary between
        # two previous findings and hand it to the wrong one, leaving the
        # finding the verdict actually described unclaimed.
        from tianluo.engine.steps.self_check import _issue_identity_tokens

        current = _valid_issue(actual="new wording", path="a.py",
                               divergence="this round's failure mode",
                               quote="Implement the defer feature")
        prev = _valid_issue(actual="rollback hook omitted sudo", path="a.py",
                            divergence="last round's failure mode",
                            quote="handle edge cases")
        issues, _unfolded, folded = _fold_still_present_into_current(
            [current], [prev], [True],
        )
        assert folded == 1
        assert _issue_identity_tokens(issues[0]) == _issue_identity_tokens(current)
        # The carried statement IS readable in the text — it just scores no
        # identity of its own.
        assert "rollback hook omitted sudo" in issues[0]["divergence"]
        assert "rollback" not in _issue_identity_tokens(issues[0])

    def test_fold_scaffolding_never_becomes_identity(self):
        # The markers, field labels and severity note the fold renders are
        # words a resolution summary can easily contain; letting them score
        # would make every folded finding a magnet for unrelated summaries.
        from tianluo.engine.steps.self_check import _issue_identity_tokens

        current = _valid_issue(actual="alpha", path="a.py", severity="low",
                               divergence="beta")
        prev = _valid_issue(actual="gamma", path="a.py", severity="critical",
                            divergence="delta")
        issues, _unfolded, folded = _fold_still_present_into_current(
            [current], [prev], [True],
        )
        assert folded == 1
        # Exactly this round's own identity — neither the carried statement
        # nor the rendering that carries it contributes anything.
        assert _issue_identity_tokens(issues[0]) == _issue_identity_tokens(current)
        assert "severity_raised" in issues[0]["divergence"]
        assert _FOLD_MARKER in issues[0]["divergence"]

    def test_two_previous_findings_at_one_position_both_fold(self):
        current = _valid_issue(actual="third wording", path="a.py")
        prev_a = _valid_issue(actual="first wording", path="a.py")
        prev_b = _valid_issue(actual="second wording", path="a.py")
        issues, unfolded, folded = _fold_still_present_into_current(
            [current], [prev_a, prev_b], [True, True],
        )
        assert (folded, unfolded, len(issues)) == (2, [], 1)
        self._assert_carries(issues[0]["divergence"], prev_a, prev_b)
        assert issues[0]["divergence"].count(_FOLD_MARKER) == 2

    def test_different_position_is_left_to_the_readmission_path(self):
        current = _valid_issue(actual="new wording", path="a.py", line=1)
        drifted = _valid_issue(actual="old wording", path="a.py", line=9)
        elsewhere = _valid_issue(actual="other bug", path="b.py")
        issues, unfolded, folded = _fold_still_present_into_current(
            [current], [drifted, elsewhere], [True, True],
        )
        assert folded == 0
        assert unfolded == [drifted, elsewhere]
        assert issues == [current]

    def test_case_differing_paths_are_distinct_positions(self):
        # On a case-sensitive filesystem ``Foo.py`` and ``foo.py`` are two
        # files, so their findings are two defects and must not be folded.
        current = _valid_issue(actual="new wording", path="foo.py")
        other_file = _valid_issue(actual="old wording", path="Foo.py")
        issues, unfolded, folded = _fold_still_present_into_current(
            [current], [other_file], [True],
        )
        assert (folded, unfolded, issues) == (0, [other_file], [current])

    def test_identical_case_sensitive_position_still_folds(self):
        current = _valid_issue(actual="new wording", path="Foo.py")
        prev = _valid_issue(actual="old wording", path="Foo.py")
        _issues, unfolded, folded = _fold_still_present_into_current(
            [current], [prev], [True],
        )
        assert (folded, unfolded) == (1, [])

    def test_positionless_previous_issue_is_not_folded(self):
        current = _valid_issue(actual="new wording", path="a.py")
        blind = {"severity": "low", "actual_behavior": "no position"}
        issues, unfolded, folded = _fold_still_present_into_current(
            [current], [blind, "not-a-dict"], [True, True],
        )
        assert folded == 0
        assert unfolded == [blind, "not-a-dict"]
        assert issues == [current]

    def test_fail_closed_sweep_entry_is_never_folded(self):
        # The sweep re-admits findings NO verdict named individually. Nothing
        # says such a finding is the same defect as a same-line re-report, so
        # it keeps the existing separate re-admission path.
        current = _valid_issue(actual="new wording", path="a.py")
        swept = _valid_issue(actual="unclaimed previous finding", path="a.py")
        issues, unfolded, folded = _fold_still_present_into_current(
            [current], [swept], [False],
        )
        assert (folded, unfolded, issues) == (0, [swept], [current])

    def test_only_the_identified_entries_fold(self):
        current = _valid_issue(actual="new wording", path="a.py")
        verdicted = _valid_issue(actual="named by a verdict", path="a.py")
        swept = _valid_issue(actual="swept in fail-closed", path="a.py")
        issues, unfolded, folded = _fold_still_present_into_current(
            [current], [verdicted, swept], [True, False],
        )
        assert folded == 1
        assert unfolded == [swept]
        assert "named by a verdict" in issues[0]["divergence"]
        assert "swept in fail-closed" not in issues[0]["divergence"]

    def test_classify_marks_sweep_entries_unidentified(self):
        # Two previous findings, one unpairable/indecisive still_present
        # verdict → the fail-closed sweep re-admits both, and neither is
        # marked as individually identified.
        prev_a = _valid_issue(actual="alpha bug", path="a.py")
        prev_b = _valid_issue(actual="beta bug", path="b.py")
        issues, identified = _classify_still_present_prev_issues(
            [{"prev_issue_summary": "", "status": "still_present"}],
            [prev_a, prev_b],
        )
        assert issues == [prev_a, prev_b]
        assert identified == [False, False]

    def test_classify_marks_paired_verdicts_identified(self):
        prev_a = _valid_issue(actual="alpha bug", path="a.py")
        prev_b = _valid_issue(actual="beta bug", path="b.py")
        issues, identified = _classify_still_present_prev_issues(
            [
                {"prev_issue_summary": "alpha bug", "status": "still_present"},
                {"prev_issue_summary": "beta bug", "status": "fixed"},
            ],
            [prev_a, prev_b],
        )
        assert issues == [prev_a]
        assert identified == [True]

    def test_same_missing_in_without_evidence_lines_does_not_fold(self):
        # Two distinct omissions can name the same integration point; with no
        # evidence line to compare there is no position match, so they stay
        # separate findings.
        current = dict(_valid_issue(actual="omission A"),
                       evidence_lines=[], missing_in=["deployment/setup.sh"])
        prev = dict(_valid_issue(actual="omission B"),
                    evidence_lines=[], missing_in=["deployment/setup.sh"])
        issues, unfolded, folded = _fold_still_present_into_current(
            [current], [prev], [True],
        )
        assert (folded, unfolded, issues) == (0, [prev], [current])

    def test_legacy_location_alone_does_not_fold(self):
        current = {"severity": "medium", "actual_behavior": "one",
                   "location": "deployment/setup.sh"}
        prev = {"severity": "medium", "actual_behavior": "two",
                "location": "deployment/setup.sh"}
        issues, unfolded, folded = _fold_still_present_into_current(
            [current], [prev], [True],
        )
        assert (folded, unfolded, issues) == (0, [prev], [current])

    def test_fold_raises_top_level_severity_to_the_higher_one(self):
        # A still-present HIGH re-reported as medium must not become a
        # deferrable medium: the severity gates read the top-level field only.
        current = _valid_issue(actual="new wording", path="a.py",
                               severity="medium")
        prev = _valid_issue(actual="old wording", path="a.py",
                            severity="high")
        issues, _unfolded, folded = _fold_still_present_into_current(
            [current], [prev], [True],
        )
        assert folded == 1
        assert issues[0]["severity"] == "high"
        assert _has_critical_or_high(issues) is True
        # The round's own severity is preserved in the record, not erased.
        assert current["severity"] == "medium"
        assert "severity_raised" in issues[0]["divergence"]
        assert "reported as medium this round" in issues[0]["divergence"]

    def test_fold_never_lowers_the_current_severity(self):
        current = _valid_issue(actual="new wording", path="a.py",
                               severity="critical")
        prev = _valid_issue(actual="old wording", path="a.py",
                            severity="low")
        issues, _unfolded, folded = _fold_still_present_into_current(
            [current], [prev], [True],
        )
        assert folded == 1
        assert issues[0]["severity"] == "critical"
        assert "severity_raised" not in issues[0]["divergence"]

    def test_describe_issue_carries_every_folded_statement(self):
        current = _valid_issue(actual="new wording", path="a.py",
                               divergence="this round's failure mode",
                               quote="Implement the defer feature")
        # A second evidence citation: the fold joins on the PRIMARY position
        # only, but every citation of both statements must survive.
        prev = dict(
            _valid_issue(actual="old wording", path="a.py",
                         divergence="last round's failure mode",
                         quote="handle edge cases"),
            evidence_lines=["a.py:1", "b.py:20"],
        )
        issues, _unfolded, _folded = _fold_still_present_into_current(
            [current], [prev], [True],
        )
        rendered = _describe_issue(issues[0])
        self._assert_carries(rendered, current, prev)

    def test_describe_issue_unchanged_without_folding(self):
        issue = _valid_issue(actual="lone wording", path="a.py")
        rendered = _describe_issue(issue)
        assert rendered == (
            "a.py:1 | actual: lone wording | expected: correct behavior "
            "| divergence: concrete failure mode"
        )

    def test_deferred_stash_rescue_prompt_carries_folded_statements(self):
        # The state machine's deferred-stash rescue is a route into IMPLEMENT
        # that bypasses ``_build_fix_outputs``; it must not hide folded content.
        from tianluo.engine.state_machine import StateMachine

        current = _valid_issue(actual="new wording", path="a.py",
                               divergence="this round's failure mode",
                               quote="Implement the defer feature")
        # A second evidence citation: the fold joins on the PRIMARY position
        # only, but every citation of both statements must survive.
        prev = dict(
            _valid_issue(actual="old wording", path="a.py",
                         divergence="last round's failure mode",
                         quote="handle edge cases"),
            evidence_lines=["a.py:1", "b.py:20"],
        )
        issues, _unfolded, _folded = _fold_still_present_into_current(
            [current], [prev], [True],
        )
        sm = Mock(spec=StateMachine)
        sm._get_max_fix_iterations.return_value = 5
        flow = Mock()
        flow.state.get_fix_iteration.return_value = 1
        flow.state.context = {}
        step = Step(step_type=StepType.SELF_CHECK, status=StepStatus.PENDING)
        StateMachine._route_deferred_into_fix_loop(sm, flow, step, issues)

        instructions = step.outputs["fix_instructions"]
        self._assert_carries(instructions, current, prev)
        assert instructions.count("- [") == 1

    def test_implement_fix_context_carries_folded_statements(self):
        from tianluo.engine.steps.implement import _format_fix_context_structured

        current = _valid_issue(actual="new wording", path="a.py",
                               divergence="this round's failure mode",
                               quote="Implement the defer feature")
        # A second evidence citation: the fold joins on the PRIMARY position
        # only, but every citation of both statements must survive.
        prev = dict(
            _valid_issue(actual="old wording", path="a.py",
                         divergence="last round's failure mode",
                         quote="handle edge cases"),
            evidence_lines=["a.py:1", "b.py:20"],
        )
        issues, _unfolded, _folded = _fold_still_present_into_current(
            [current], [prev], [True],
        )
        rendered = _format_fix_context_structured(
            {"reason": "self_check", "issues": issues, "iteration": 2},
        )
        self._assert_carries(rendered, current, prev)
        assert rendered.count("  - [") == 1

    def test_implement_fix_context_unchanged_without_folding(self):
        from tianluo.engine.steps.implement import _format_fix_context_structured

        issue = _valid_issue(actual="lone wording", path="a.py")
        rendered = _format_fix_context_structured(
            {"reason": "self_check", "issues": [issue], "iteration": 2},
        )
        assert rendered.splitlines() == [
            "Reason: self_check",
            "Self-check findings:",
            "  - [medium] lone wording — concrete failure mode @ a.py:1",
        ]


class TestFoldedFindingMeetsTheDeferredStash:
    """The stash merge is the fold's other dedup call site.

    A finding this round folded a previous one into can collide with an
    identically-worded stash entry from an earlier pass. The collision verdict
    is the pre-existing one; what must not happen is the folded statement
    leaving the fix loop with the dropped duplicate.
    """

    @staticmethod
    def _run(step, flow, payload):
        with patch("tianluo.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = json.dumps(payload)
            mock_cls.return_value = mock_caller
            return self_check_handler(step, flow)

    def test_stash_duplicate_keeps_the_folded_statement(self, tmp_path):
        flow = _make_flow(tmp_path)
        stashed = _valid_issue(actual="shared wording", path="a.py")
        prev = _valid_issue(actual="previous round wording", path="a.py",
                            quote="handle edge cases")
        current = _valid_issue(actual="shared wording", path="a.py")
        step = _make_step(
            pass_index=2, passes_required=2, threshold=3,
            deferred=[stashed],
        )
        step.inputs["prev_self_check_issues"] = [prev]
        result = self._run(step, flow, {
            "issues": [current],
            "previous_issue_resolutions": [
                {"prev_issue_summary": "previous round wording",
                 "status": "still_present"},
            ],
            "summary": "s",
        })

        assert result == StepStatus.REVISION_NEEDED
        assert step.outputs["validation_stats"]["folded_still_present_count"] == 1
        issues = step.outputs["issues"]
        # Dedup verdict unchanged: the stash entry survives, the duplicate goes.
        assert len(issues) == 1
        assert issues[0] is not stashed
        assert issues[0]["actual_behavior"] == "shared wording"
        instructions = step.outputs["fix_instructions"]
        assert instructions.count("- [") == 1
        # The folded previous finding reached the fix loop all the same.
        assert prev["actual_behavior"] in instructions
        assert prev["expectation_source"]["verbatim_quote"] in instructions


class TestFoldedSeverityReachesFixImmediately:
    """A still-present HIGH folded into a medium re-report keeps its weight.

    The defer decision and ``_has_critical_or_high`` read the top-level
    ``severity`` only, so a fold that left the top level at the re-report's
    severity would route a high finding into the deferral path it could not
    take before folding existed.
    """

    @staticmethod
    def _run(step, flow, payload):
        with patch("tianluo.engine.steps.self_check.LLMCaller") as mock_cls:
            mock_caller = Mock()
            mock_caller.call.return_value = json.dumps(payload)
            mock_cls.return_value = mock_caller
            return self_check_handler(step, flow)

    def test_high_prev_folded_into_medium_report_fixes_now(self, tmp_path):
        flow = _make_flow(tmp_path)
        prev = _valid_issue(severity="high", actual="old high wording",
                            path="a.py", quote="handle edge cases")
        current = _valid_issue(severity="medium", actual="new medium wording",
                               path="a.py")
        step = _make_step(pass_index=1, passes_required=3, threshold=3)
        step.inputs["prev_self_check_issues"] = [prev]
        result = self._run(step, flow, {
            "issues": [current],
            "previous_issue_resolutions": [
                {"prev_issue_summary": "old high wording",
                 "status": "still_present"},
            ],
            "summary": "s",
        })
        assert result == StepStatus.REVISION_NEEDED
        assert "self_check_deferred" not in step.outputs
        issues = step.outputs["issues"]
        assert len(issues) == 1
        assert issues[0]["severity"] == "high"
        assert "old high wording" in step.outputs["fix_instructions"]
        assert "new medium wording" in step.outputs["fix_instructions"]


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


class TestDeferHotDisabledMidChain:
    """The defer threshold is re-read from tianluo.yaml on every pass; a
    hot-edit to 0 must NOT orphan the stash accumulated by earlier passes.
    A non-empty stash always reaches the fix loop — the flush/merge decision
    must not depend solely on the CURRENT pass's defer_enabled value.
    """

    def test_clean_last_pass_flushes_stash_even_when_defer_now_disabled(
        self, tmp_path,
    ):
        flow = _make_flow(tmp_path)
        prior = _valid_issue(severity="medium", actual="stashed before hot-edit")
        step = _make_step(
            pass_index=2, passes_required=2, threshold=0, deferred=[prior],
        )
        result = _run_handler(step, flow, [])  # clean last pass
        assert result == StepStatus.REVISION_NEEDED
        assert "stashed before hot-edit" in step.outputs["fix_instructions"]
        assert len(step.outputs["fix_context"]["issues"]) == 1
        # Consumed into the fix loop: the stash output is cleared.
        assert step.outputs["self_check_deferred_issues"] == []

    def test_finding_pass_merges_stash_even_when_defer_now_disabled(
        self, tmp_path,
    ):
        """A finding-bearing pass with deferral hot-disabled still merges the
        prior stash so fix_instructions carries the FULL accumulated set."""
        flow = _make_flow(tmp_path)
        prior = _valid_issue(severity="medium", actual="stashed before hot-edit")
        step = _make_step(
            pass_index=2, passes_required=2, threshold=0, deferred=[prior],
        )
        issues = [_valid_issue(severity="low", actual="new pass finding")]
        result = _run_handler(step, flow, issues)
        assert result == StepStatus.REVISION_NEEDED
        fixed = step.outputs["fix_context"]["issues"]
        assert len(fixed) == 2
        assert "stashed before hot-edit" in step.outputs["fix_instructions"]
        assert "new pass finding" in step.outputs["fix_instructions"]

    def test_clean_middle_pass_echoes_stash_even_when_defer_now_disabled(
        self, tmp_path,
    ):
        """A non-terminal clean pass keeps carrying the stash so the tail pass
        can flush it — regardless of the current pass's threshold."""
        flow = _make_flow(tmp_path)
        prior = _valid_issue(severity="medium", actual="stashed before hot-edit")
        step = _make_step(
            pass_index=2, passes_required=3, threshold=0, deferred=[prior],
        )
        result = _run_handler(step, flow, [])
        assert result == StepStatus.COMPLETED
        assert step.outputs["self_check_deferred_issues"] == [prior]
        assert step.outputs["self_check_deferred"] is True


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
        """With deferral enabled, a repeated pass with NO
        pending stash is NOT exempt from the defer/fix arbitration: at the chain
        tail (last pass) it MUST enter the fix loop. Regression coverage ensures
        pass 1/1 cannot lose the lone recurring finding."""
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

    def test_repeated_findings_enter_fix_when_deferral_disabled(self, tmp_path):
        """threshold=0 enters fix even when legacy convergence is requested."""
        flow = _make_flow(tmp_path)
        issues = [_valid_issue(severity="medium", actual="recurring bug", path="a.py")]
        step = self._convergence_step(
            pass_index=1, passes_required=1, threshold=0, prev_issues=list(issues),
        )
        result = _run_handler(step, flow, issues)
        assert result == StepStatus.REVISION_NEEDED
        assert step.outputs.get("converged") is not True
        assert step.outputs["fix_needed"] is True


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

    def test_deferred_flush_rescue_is_scoped_per_round(self, tmp_path):
        """The Skip rescue guard bounds retries WITHIN one round only.

        A flow-wide latch would silently discard a later round's freshly
        stashed validated findings — the one outcome the check-step contract
        forbids — so a new round must get its own rescue.
        """
        cfg = WorkflowConfig(
            self_check_passes_required=1, self_check_defer_fix_threshold=3,
        )
        sm = _make_state_machine(tmp_path, cfg)
        flow = _flow_ready(tmp_path)
        i1 = _valid_issue(actual="deferred bug one")
        flow.state.context["self_check_deferred_issues"] = [i1]

        # Round A's terminal pass was force-completed by the Skip gate: no
        # outputs, so nobody consumed the stash.
        round_a = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.COMPLETED,
            inputs={"self_check_round_id": "scr-aaaaaaaaaaaa"},
            outputs={},
        )
        assert sm._unflushed_deferred_issues(flow, round_a) == [i1]

        # The rescue runs once and latches for THIS round.
        flow.state.context["self_check_deferred_flush_attempted"] = (
            sm._deferred_flush_round_key(round_a)
        )
        assert sm._unflushed_deferred_issues(flow, round_a) == []

        # A later round with its own stash is rescued again.
        round_b = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.COMPLETED,
            inputs={"self_check_round_id": "scr-bbbbbbbbbbbb"},
            outputs={},
        )
        assert sm._unflushed_deferred_issues(flow, round_b) == [i1]

    def test_later_round_skip_reenters_fix_loop_instead_of_discarding(
        self, tmp_path,
    ):
        """End-to-end: a second Skipped terminal pass still re-runs with the
        stash re-injected rather than closing the round clean."""
        cfg = WorkflowConfig(
            self_check_passes_required=1, self_check_defer_fix_threshold=3,
        )
        sm = _make_state_machine(tmp_path, cfg)
        flow = _flow_ready(tmp_path)
        i1 = _valid_issue(actual="deferred bug one")

        # An earlier round already consumed its one rescue.
        flow.state.context["self_check_deferred_flush_attempted"] = "scr-earlier"

        inputs = sm._build_step_inputs(flow, StepType.SELF_CHECK)
        assert inputs["self_check_round_id"]
        skipped = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.COMPLETED,
            inputs=inputs,
            outputs={},
        )
        flow.state.add_step(skipped)
        flow.state.current_step_id = skipped.step_id
        flow.state.context["self_check_deferred_issues"] = [i1]

        nxt = sm.transition_to_next(flow)
        assert nxt is not None
        assert nxt.step_type == StepType.SELF_CHECK
        assert nxt.inputs["self_check_deferred_issues"] == [i1]
        assert flow.state.context["self_check_deferred_flush_attempted"] == (
            inputs["self_check_round_id"]
        )

    def test_requirement_mutation_keeps_deferred_stash_in_new_full_round(
        self, tmp_path,
    ):
        """A mid-chain interjection ends the pass chain, but the deferred
        stash must survive the forced full round's pass-#1 reset."""
        cfg = WorkflowConfig(
            self_check_passes_required=3, self_check_defer_fix_threshold=3,
        )
        sm = _make_state_machine(tmp_path, cfg)
        flow = _flow_ready(tmp_path)

        # Pass 1: real inputs (round id present) + a deferred finding.
        inputs1 = sm._build_step_inputs(flow, StepType.SELF_CHECK)
        assert inputs1["self_check_round_id"]
        i1 = _valid_issue(actual="deferred bug one")
        sc1 = Step(
            step_type=StepType.SELF_CHECK,
            status=StepStatus.COMPLETED,
            inputs=inputs1,
            outputs={
                "self_check_deferred_issues": [i1],
                "self_check_deferred": True,
                "issues": [],
                "actionable_count": 0,
            },
        )
        flow.state.add_step(sc1)
        flow.state.current_step_id = sc1.step_id

        # Pass 2: clean, echoes the stash forward unchanged.
        sc2 = sm.transition_to_next(flow)
        assert sc2.inputs["self_check_pass_index"] == 2
        assert sc2.inputs["self_check_deferred_issues"] == [i1]
        sc2.status = StepStatus.COMPLETED
        sc2.outputs = {
            "self_check_deferred_issues": [i1],
            "self_check_deferred": True,
            "issues": [],
            "actionable_count": 0,
        }
        flow.state.current_step_id = sc2.step_id

        # User interjects mid-chain → effective requirements mutate → the
        # controller must force a new full round at pass #1 WITHOUT
        # discarding the stash accumulated by the invalidated chain.
        flow.state.context["user_interjections"] = [
            {"text": "also keep the legacy API working"}
        ]
        sc3 = sm.transition_to_next(flow)
        assert sc3.step_type == StepType.SELF_CHECK
        assert sc3.inputs["self_check_pass_index"] == 1
        assert sc3.inputs["self_check_round_id"] != sc2.inputs["self_check_round_id"]
        assert sc3.inputs["self_check_deferred_issues"] == [i1]
        assert flow.state.context["self_check_deferred_issues"] == [i1]


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
