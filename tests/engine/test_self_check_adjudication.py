"""Tests for the self_check ↔ adjudication ledger wiring (group G3, task 1).

Covers the three behaviours the self_check handler gained:

  1. Every SELF_CHECK round's validated issues are appended to the cross-round
     adjudication ledger on ``flow.state.context``.
  2. A fix-loop round's ``previous_issue_resolutions`` are paired back with the
     previous issues (by position) and recorded so trigger (b) ("打脸") can read
     the ``fixed`` verdicts by fingerprint.
  3. The convergence shortcut is suppressed when a structural oscillation
     trigger would fire (``convergence_enabled=True`` scenario), so an
     oscillating spec contradiction is routed to the adjudicator instead of
     being silently swallowed as "converged". Non-oscillation convergence is
     unchanged.

The failure-asserting nature of these tests does not touch the se3 test step, so
no SE3_TEST_RUNNING guard is needed here.
"""

from __future__ import annotations

import json
import pytest
from unittest.mock import Mock, patch

from se3.engine import adjudication
from se3.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)
from se3.engine.steps.self_check import self_check_handler, _pair_resolutions_with_prev


_TASK = "Implement the parser feature and handle the empty-input edge case"
_QUOTE = "handle the empty-input edge case"


def _issue(*, expected="returns None", actual="broken behavior here",
           divergence="concrete failure mode", path="a.py", line=1,
           quote=_QUOTE, severity="medium"):
    """A self_check issue that survives ``_validate_and_filter_issues``.

    ``quote`` must be a substring of the source pool (task_description_base) and
    ``path`` must appear in ``changes_made.files_changed``.
    """
    return {
        "severity": severity,
        "actual_behavior": actual,
        "expected_behavior": expected,
        "divergence": divergence,
        "expectation_source": {"type": "task_description", "verbatim_quote": quote},
        "evidence_lines": [f"{path}:{line}"],
        "missing_in": [],
        "out_of_scope": False,
    }


def _make_flow(tmp_path):
    return FlowInstance(
        flow_id="adj-sc-flow",
        task_description=_TASK,
        task_type="feature",
        status=FlowStatus.RUNNING,
        change_path=tmp_path / "changes" / "c",
    )


def _make_step(
    *,
    pass_index=1,
    passes_required=1,
    fix_iteration=0,
    convergence_enabled=False,
    prev_issues=None,
    step_id=None,
):
    inputs = {
        "task_description": _TASK,
        "task_description_base": _TASK,
        "changes_made": {"files_changed": [{"path": "a.py", "action": "modify"}]},
        "test_results": {"passed": True, "returncode": 0},
        "spec_content": {},
        "self_check_pass_index": pass_index,
        "self_check_passes_required": passes_required,
        "self_check_convergence_enabled": convergence_enabled,
        "self_check_defer_fix_threshold": 0,  # defer disabled → convergence reachable
        "max_fix_iterations": 10,
    }
    if fix_iteration:
        inputs["fix_iteration"] = fix_iteration
    if prev_issues is not None:
        inputs["prev_self_check_issues"] = prev_issues
    step = Step(
        step_type=StepType.SELF_CHECK,
        status=StepStatus.PENDING,
        inputs=inputs,
    )
    if step_id:
        step.step_id = step_id
    return step


def _run(step, flow, issues, previous_issue_resolutions=None):
    payload = {"issues": issues, "summary": "s"}
    if previous_issue_resolutions is not None:
        payload["previous_issue_resolutions"] = previous_issue_resolutions
    with patch("se3.engine.steps.self_check.LLMCaller") as mock_cls:
        mock_caller = Mock()
        mock_caller.call.return_value = json.dumps(payload)
        mock_cls.return_value = mock_caller
        return self_check_handler(step, flow)


# ---------------------------------------------------------------------------
# 1. Round recording
# ---------------------------------------------------------------------------

class TestRoundRecording:
    def test_issues_recorded_into_ledger(self, tmp_path):
        flow = _make_flow(tmp_path)
        step = _make_step(step_id="01_self_check_aaa")
        issue = _issue(expected="returns None")
        _run(step, flow, [issue])

        ledger = flow.state.context[adjudication.LEDGER_KEY]
        obs = ledger["observations"]
        assert len(obs) == 1
        assert obs[0]["fingerprint"] == adjudication.fingerprint(issue)
        assert ledger["round_count"] == 1

    def test_clean_round_still_records_and_advances(self, tmp_path):
        """A round with no validated issues still counts as a round (round_count
        advances) so periodic/reproduction counting stays accurate."""
        flow = _make_flow(tmp_path)
        step = _make_step(step_id="01_self_check_clean")
        result = _run(step, flow, [])
        assert result == StepStatus.COMPLETED
        ledger = flow.state.context[adjudication.LEDGER_KEY]
        assert ledger["round_count"] == 1
        assert ledger["observations"] == []

    def test_replay_same_step_id_is_idempotent(self, tmp_path):
        """A --resume replay of the same PENDING step must not double-count."""
        flow = _make_flow(tmp_path)
        issue = _issue()
        step1 = _make_step(step_id="01_self_check_same")
        _run(step1, flow, [issue])
        # Re-run a fresh step object carrying the SAME step_id (resume replay).
        step2 = _make_step(step_id="01_self_check_same")
        _run(step2, flow, [issue])
        ledger = flow.state.context[adjudication.LEDGER_KEY]
        assert ledger["round_count"] == 1
        assert len(ledger["observations"]) == 1


# ---------------------------------------------------------------------------
# 2. Resolution recording + pairing
# ---------------------------------------------------------------------------

class TestResolutionRecording:
    def test_pair_resolutions_by_position(self):
        prev = [_issue(expected="A"), _issue(expected="B", quote=_QUOTE)]
        resolutions = [
            {"prev_issue_summary": "first", "status": "fixed"},
            {"prev_issue_summary": "second", "status": "still_present"},
        ]
        paired = _pair_resolutions_with_prev(resolutions, prev)
        assert paired[0]["issue"] is prev[0]
        assert paired[1]["issue"] is prev[1]

    def test_pair_cardinality_mismatch_skips_positional_pairing(self):
        """When the reviewer returns a different COUNT than prev_issues the
        positional alignment cannot be trusted (an omitted/extra entry shifts
        every index), so NO resolution is paired by index — they pass through
        unpaired (empty fingerprint, no trigger weight) rather than record a
        'fixed' verdict against the wrong issue's fingerprint (issue 6)."""
        prev = [_issue()]
        resolutions = [
            {"status": "fixed"},
            {"status": "fixed"},  # count (2) != prev count (1)
        ]
        paired = _pair_resolutions_with_prev(resolutions, prev)
        assert "issue" not in paired[0]
        assert "issue" not in paired[1]

    def test_pair_omitted_first_entry_does_not_mislabel(self):
        """The finding's concrete case: 3 prev issues but only 2 resolutions
        (first omitted). Index-pairing would stamp resolution[0] (about issue #2)
        onto issue #1's fingerprint. The cardinality guard refuses to pair, so no
        wrong-fingerprint 'fixed' verdict is produced (issue 6)."""
        prev = [
            _issue(expected="A"),
            _issue(expected="B", quote=_QUOTE),
            _issue(expected="C"),
        ]
        resolutions = [
            {"prev_issue_summary": "issue #2", "status": "fixed"},
            {"prev_issue_summary": "issue #3", "status": "still_present"},
        ]
        paired = _pair_resolutions_with_prev(resolutions, prev)
        assert all("issue" not in p for p in paired)

    def test_pair_preserves_already_identified_issue_on_mismatch(self):
        """A resolution that already carries its own machine-identified ``issue``
        keeps it even when the counts diverge — only the untrustworthy positional
        inference is suppressed."""
        prev = [_issue(expected="A")]
        own = _issue(expected="Z", quote=_QUOTE)
        resolutions = [
            {"status": "fixed", "issue": own},
            {"status": "fixed"},  # count mismatch → no positional pairing
        ]
        paired = _pair_resolutions_with_prev(resolutions, prev)
        assert paired[0]["issue"] is own
        assert "issue" not in paired[1]

    def test_pair_reordered_entries_refuses_wrong_fingerprint(self):
        """Matching COUNT but REVERSED order: the reviewer returned the verdict
        about issue B first and A second. Blind positional pairing would stamp
        B's ``fixed`` verdict onto A's fingerprint (spuriously firing trigger (b)
        for A, masking the real 打脸 for B). The content-match reorder guard sees
        each summary describes a DIFFERENT prev issue than its positional partner
        and leaves both unpaired (empty fingerprint, no trigger weight)."""
        issue_a = _issue(
            expected="parser returns None on empty input",
            quote="handle the empty-input edge case",
            path="parser.py",
        )
        issue_b = _issue(
            expected="tokenizer raises ValueError on malformed token",
            quote="reject the malformed token stream",
            path="tokenizer.py",
        )
        prev = [issue_a, issue_b]
        # Summaries are in the OPPOSITE order to prev.
        resolutions = [
            {"prev_issue_summary": "tokenizer malformed token ValueError",
             "status": "fixed"},
            {"prev_issue_summary": "parser empty input returns None",
             "status": "fixed"},
        ]
        paired = _pair_resolutions_with_prev(resolutions, prev)
        assert all("issue" not in p for p in paired)

    def test_pair_in_order_signal_pairs_correctly(self):
        """Matching count, IN order, discriminating summaries: each positional
        partner is the best content match, so both pair by position."""
        issue_a = _issue(
            expected="parser returns None on empty input",
            quote="handle the empty-input edge case",
            path="parser.py",
        )
        issue_b = _issue(
            expected="tokenizer raises ValueError on malformed token",
            quote="reject the malformed token stream",
            path="tokenizer.py",
        )
        prev = [issue_a, issue_b]
        resolutions = [
            {"prev_issue_summary": "parser empty input returns None",
             "status": "fixed"},
            {"prev_issue_summary": "tokenizer malformed token ValueError",
             "status": "fixed"},
        ]
        paired = _pair_resolutions_with_prev(resolutions, prev)
        assert paired[0]["issue"] is issue_a
        assert paired[1]["issue"] is issue_b

    def test_fixed_resolution_recorded_with_fingerprint(self, tmp_path):
        """A pass-#1 fix round's ``fixed`` verdict lands on the ledger paired to
        the previous issue, so trigger (b) can match it by fingerprint."""
        flow = _make_flow(tmp_path)
        prev_issue = _issue(expected="returns None")
        step = _make_step(
            step_id="03_self_check_fix",
            fix_iteration=1,
            prev_issues=[prev_issue],
        )
        # This round reports no new issues but declares the prev issue fixed.
        _run(
            step, flow, [],
            previous_issue_resolutions=[
                {"prev_issue_summary": "the None case", "status": "fixed"}
            ],
        )
        ledger = flow.state.context[adjudication.LEDGER_KEY]
        assert len(ledger["resolutions"]) == 1
        res = ledger["resolutions"][0]
        assert res["status"] == "fixed"
        assert res["fingerprint"] == adjudication.fingerprint(prev_issue)


# ---------------------------------------------------------------------------
# 3. Convergence suppression guard
# ---------------------------------------------------------------------------

class TestConvergenceSuppression:
    def _seed_prior_round(self, flow, expected):
        """Record a prior round flagging the shared position with ``expected``."""
        adjudication.record_self_check_round(
            flow.state.context, [_issue(expected=expected)], round_id="prior",
        )

    def test_oscillation_suppresses_convergence(self, tmp_path):
        """convergence_enabled=True + an oscillating position → the shortcut is
        blocked and the flow enters the fix loop (REVISION_NEEDED) so the
        adjudicator can intervene, rather than COMPLETED-converged."""
        flow = _make_flow(tmp_path)
        # Prior round demanded "returns None" at this position.
        self._seed_prior_round(flow, expected="returns None")

        # This round demands the OPPOSITE ("returns zero") at the same position
        # (same file + quote), but keeps actual/divergence identical to a prev
        # issue so ``_issues_converged`` would otherwise fire.
        current = _issue(expected="returns zero")
        prev_for_convergence = _issue(expected="returns zero")
        step = _make_step(
            step_id="05_self_check_osc",
            convergence_enabled=True,
            prev_issues=[prev_for_convergence],
        )
        result = _run(step, flow, [current])

        assert result == StepStatus.REVISION_NEEDED
        assert step.outputs.get("converged") is not True
        assert step.outputs["fix_needed"] is True

    def test_non_oscillation_convergence_unchanged(self, tmp_path):
        """No oscillation on the ledger → convergence shortcut still fires and
        the pass COMPLETEs as converged (behaviour preserved)."""
        flow = _make_flow(tmp_path)
        # Prior round demanded the SAME expected as this round → not oscillating.
        self._seed_prior_round(flow, expected="returns zero")

        current = _issue(expected="returns zero")
        prev_for_convergence = _issue(expected="returns zero")
        step = _make_step(
            step_id="05_self_check_conv",
            convergence_enabled=True,
            prev_issues=[prev_for_convergence],
        )
        result = _run(step, flow, [current])

        assert result == StepStatus.COMPLETED
        assert step.outputs.get("converged") is True

    def test_convergence_disabled_no_suppression_effect(self, tmp_path):
        """With convergence disabled (default), the oscillation guard has no
        bearing: an issue-bearing pass enters the fix loop as always."""
        flow = _make_flow(tmp_path)
        self._seed_prior_round(flow, expected="returns None")
        step = _make_step(step_id="05_self_check_off", convergence_enabled=False)
        result = _run(step, flow, [_issue(expected="returns zero")])
        assert result == StepStatus.REVISION_NEEDED
