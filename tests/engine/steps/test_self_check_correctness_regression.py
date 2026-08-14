"""SELF_CHECK requirement-authority, quality-dimension, and evidence tests."""

from __future__ import annotations

import json
from unittest.mock import Mock, patch

import pytest

from tianluo.engine.models import FlowInstance, FlowStatus, Step, StepStatus, StepType
from tianluo.engine.context_builder import get_self_check_constraint_sources
from tianluo.engine.steps.self_check import (
    SELF_CHECK_PROMPT,
    _build_source_pool,
    _validate_and_filter_issues,
    self_check_handler,
)


class TestPromptContract:
    @pytest.mark.parametrize(
        "dimension",
        [
            "Requirement Completeness",
            "Behavioral Correctness",
            "Cross-Module Integration",
            "Regression Safety",
            "Robustness",
            "Test Coverage",
        ],
    )
    def test_all_six_quality_dimensions_are_explicit(self, dimension):
        assert dimension in SELF_CHECK_PROMPT

    def test_effective_task_is_authoritative_and_plan_is_not(self):
        assert "functional-requirement authority" in SELF_CHECK_PROMPT
        assert "only navigation clues" in SELF_CHECK_PROMPT
        assert "Never treat them as requirement sources" in SELF_CHECK_PROMPT
        assert 'expectation_source.type = "plan_task"' in SELF_CHECK_PROMPT
        assert "Plan Task Groups" not in SELF_CHECK_PROMPT
        assert "Per-Task Correctness" not in SELF_CHECK_PROMPT

    def test_finding_contract_is_complete_and_concise(self):
        for field in (
            "location",
            "actual_behavior",
            "expected_behavior",
            "divergence",
            "expectation_source",
            "evidence_lines",
            "missing_in",
            "previous_issue_resolutions",
        ):
            assert field in SELF_CHECK_PROMPT
        assert "Do not repeat the task, charter, project" in SELF_CHECK_PROMPT

    def test_prompt_format_placeholders_intact(self):
        for placeholder in (
            "{task_description}",
            "{review_scope}",
            "{changes_made}",
            "{test_results}",
            "{project_constraints}",
            "{fix_context}",
        ):
            assert placeholder in SELF_CHECK_PROMPT
        SELF_CHECK_PROMPT.format(
            task_description="t",
            review_scope="s",
            changes_made="c",
            test_results="r",
            project_constraints="p",
            fix_context="f",
        )


class TestEffectiveSourcePool:
    def test_constraint_collector_keeps_charter_and_marked_comments(self, tmp_path):
        runtime = tmp_path / "tianluo"
        runtime.mkdir()
        (runtime / "charter.md").write_text(
            "Charter hard constraint", encoding="utf-8"
        )
        source = tmp_path / "src" / "feature.py"
        source.parent.mkdir()
        source.write_text(
            "# ordinary narration\n# WHY: preserve compatibility\n"
            "# INVARIANT: one writer only\nvalue = 1\n",
            encoding="utf-8",
        )
        sources = get_self_check_constraint_sources(
            tmp_path,
            {"files_changed": ["src/feature.py"]},
        )
        assert sources["charter"] == "Charter hard constraint"
        assert "WHY: preserve compatibility" in sources["why_comments"]
        assert "INVARIANT: one writer only" in sources["why_comments"]
        assert "ordinary narration" not in sources["why_comments"]

    def test_effective_base_interjections_and_constraints_are_included(self):
        pool = _build_source_pool(
            {
                "task_description": "decorated fallback",
                "task_description_base": "adjudicated effective base",
                "original_task_description": "superseded original",
                "user_interjections": [{"text": "late user requirement"}],
                "project_constraints": {
                    "charter": "project hard constraint",
                    "why_comments": "WHY: preserve lock ownership",
                },
                "task_groups": [{"tasks": [{"description": "derived plan expansion"}]}],
                "implement_summary": "derived implementation claim",
            }
        )
        assert "adjudicated effective base" in pool
        assert "late user requirement" in pool
        assert "project hard constraint" in pool
        assert "WHY: preserve lock ownership" in pool
        assert "superseded original" not in pool
        assert "derived plan expansion" not in pool
        assert "derived implementation claim" not in pool

    def test_legacy_spec_carrier_is_not_requirement_authority(self):
        assert _build_source_pool(
            {"task_description": "x", "spec_content": {"base": "skip", "charter": "keep"}}
        ) == ["x"]


class TestValidation:
    def _inputs(self):
        return {
            "task_description_base": "Implement feature X with bounded retries",
            "changes_made": {
                "files_changed": ["src/feature.py", "src/shared.py"],
            },
            "project_constraints": {"charter": "Shared helpers preserve caller contracts"},
        }

    def _issue(self, source_type="task_description", quote="bounded retries"):
        return {
            "severity": "medium",
            "location": "src/feature.py:10",
            "actual_behavior": "retry has no bound",
            "expected_behavior": "retry stops at the configured bound",
            "divergence": "an always-failing operation loops forever",
            "expectation_source": {"type": source_type, "verbatim_quote": quote},
            "evidence_lines": ["src/feature.py:10"],
            "missing_in": [],
        }

    def test_task_and_constraint_grounded_findings_survive(self):
        task = self._issue()
        constraint = self._issue("charter", "Shared helpers preserve caller contracts")
        kept, stats = _validate_and_filter_issues([task, constraint], self._inputs())
        assert kept == [task, constraint]
        assert stats["kept_count"] == 2

    def test_plan_task_source_is_rejected(self):
        issue = self._issue("plan_task", "bounded retries")
        kept, stats = _validate_and_filter_issues([issue], self._inputs())
        assert kept == []
        assert stats["unsupported_source_type_count"] == 1

    def test_requirement_omission_can_use_missing_in(self):
        issue = self._issue()
        issue["actual_behavior"] = "the required integration is absent"
        issue["evidence_lines"] = []
        issue["missing_in"] = ["src/integration.py"]
        kept, _ = _validate_and_filter_issues([issue], self._inputs())
        assert kept == [issue]

    def test_regression_requires_a_changed_causal_line(self):
        issue = self._issue("regression", "")
        issue["evidence_lines"] = ["src/shared.py:5"]
        kept, _ = _validate_and_filter_issues([issue], self._inputs())
        assert kept == [issue]

        issue["evidence_lines"] = []
        issue["missing_in"] = ["src/shared.py"]
        kept, stats = _validate_and_filter_issues([issue], self._inputs())
        assert kept == []
        assert stats["bad_evidence_count"] == 1

    def test_deletion_only_regression_can_ground_on_missing_in(self):
        # A fix whose only diff on src/shared.py is deletions leaves no
        # current-file line to cite. The prompt directs the reviewer to
        # missing_in there; rejecting it would discard an evidence-valid
        # regression and let the round complete clean.
        inputs = dict(self._inputs())
        inputs["scope_changed_paths"] = ["src/shared.py"]
        inputs["scope_causal_anchors"] = {}
        inputs["scope_deletion_anchors"] = {"src/shared.py": [[5, 5]]}

        issue = self._issue("regression", "")
        issue["evidence_lines"] = []
        issue["missing_in"] = ["src/shared.py"]
        kept, _ = _validate_and_filter_issues([issue], inputs)
        assert kept == [issue]

        # A path OUTSIDE the current scope still cannot ground a regression.
        issue["missing_in"] = ["src/untouched.py"]
        kept, stats = _validate_and_filter_issues([issue], inputs)
        assert kept == []
        assert stats["bad_evidence_count"] == 1

    def test_still_open_finding_anchors_survive_a_narrow_incremental_scope(self):
        # An incremental round's changed paths are the fix delta only; a
        # still-open finding in a file the fix never touched must stay
        # groundable on its own recorded anchors, or the re-report is dropped
        # and the round reads clean while the defect survives.
        prev = self._issue()
        prev["evidence_lines"] = ["src/untouched.py:42"]
        inputs = dict(self._inputs())
        inputs["scope_changed_paths"] = ["src/other.py"]
        inputs["scope_causal_anchors"] = {"src/other.py": [[1, 3]]}
        inputs["prev_self_check_issues"] = [prev]

        rereport = self._issue()
        rereport["evidence_lines"] = ["src/untouched.py:42"]
        kept, _ = _validate_and_filter_issues([rereport], inputs)
        assert kept == [rereport]

        # Without the prior finding the same citation is out of scope.
        inputs.pop("prev_self_check_issues")
        kept, stats = _validate_and_filter_issues([rereport], inputs)
        assert kept == []
        assert stats["bad_evidence_count"] == 1

    def test_prior_anchor_does_not_make_an_anchor_less_path_anchor_bearing(self):
        # The current fix DELETES src/gone.py, so the path is anchor-less by
        # construction. A still-open prior finding recorded a line inside it;
        # that stale number must not turn the path anchor-bearing, or the
        # bare-path re-report the prompt prescribes is dropped as bad evidence
        # and the round completes clean against its own resolutions record.
        prev = self._issue()
        prev["evidence_lines"] = ["src/gone.py:123"]
        inputs = dict(self._inputs())
        inputs["scope_changed_paths"] = ["src/gone.py", "src/other.py"]
        inputs["scope_causal_anchors"] = {"src/other.py": [[1, 3]]}
        inputs["scope_deletion_anchors"] = {"src/gone.py": [[100, 130]]}
        inputs["prev_self_check_issues"] = [prev]

        rereport = self._issue()
        rereport["evidence_lines"] = ["src/gone.py"]
        kept, stats = _validate_and_filter_issues([rereport], inputs)
        assert kept == [rereport]
        assert stats["bad_evidence_count"] == 0

        # A regression re-report in the same bare-path form grounds too.
        regression = self._issue("regression", "")
        regression["evidence_lines"] = ["src/gone.py"]
        kept, stats = _validate_and_filter_issues([regression], inputs)
        assert kept == [regression]
        assert stats["bad_evidence_count"] == 0

    def test_prior_anchor_does_not_extend_an_anchor_bearing_range_set(self):
        # Case A: a prior finding's line on an anchor-BEARING path is an
        # unchanged context line, not a current-side causal anchor. A NEW
        # finding citing it must not ground there.
        prev = self._issue()
        prev["evidence_lines"] = ["src/a.py:100"]
        inputs = dict(self._inputs())
        inputs["scope_changed_paths"] = ["src/a.py"]
        inputs["scope_causal_anchors"] = {"src/a.py": [[1, 5]]}
        inputs["prev_self_check_issues"] = [prev]

        fresh = self._issue("regression", "")
        fresh["evidence_lines"] = ["src/a.py:100"]
        kept, stats = _validate_and_filter_issues([fresh], inputs)
        assert kept == []
        assert stats["bad_evidence_count"] == 1

        # The real current-side anchor still grounds it.
        fresh["evidence_lines"] = ["src/a.py:3"]
        kept, _ = _validate_and_filter_issues([fresh], inputs)
        assert kept == [fresh]

        # Case B: an old-side line of a file this scope DELETED is never a
        # current-side anchor either — but the path itself is anchor-less, so
        # the citation grounds at path level with the line ignored.
        prev["evidence_lines"] = ["src/gone.py:123"]
        inputs["scope_changed_paths"] = ["src/a.py", "src/gone.py"]
        fresh["evidence_lines"] = ["src/gone.py:123"]
        kept, stats = _validate_and_filter_issues([fresh], inputs)
        assert kept == [fresh]
        assert stats["bad_evidence_count"] == 0

    def test_bare_path_prior_finding_widens_the_scope(self):
        # A prior finding on an anchor-less path is recorded in the very form
        # the prompt mandates there — a bare path, no ``:N``. Its location must
        # still widen the changed-path set, or the bare-path re-report of a
        # still-open defect is dropped and the round reads clean while its own
        # resolutions record says "still_present".
        prev = self._issue()
        prev["location"] = "assets/icon.png"
        prev["evidence_lines"] = ["assets/icon.png"]
        inputs = dict(self._inputs())
        inputs["scope_changed_paths"] = ["src/foo.py"]
        inputs["scope_causal_anchors"] = {"src/foo.py": [[10, 12]]}
        inputs["prev_self_check_issues"] = [prev]

        rereport = self._issue()
        rereport["location"] = "assets/icon.png"
        rereport["evidence_lines"] = ["assets/icon.png"]
        kept, stats = _validate_and_filter_issues([rereport], inputs)
        assert kept == [rereport]
        assert stats["bad_evidence_count"] == 0

        # missing_in carrying a line suffix widens by PATH too.
        prev["evidence_lines"] = []
        prev["missing_in"] = ["assets/icon.png:7"]
        kept, _ = _validate_and_filter_issues([rereport], inputs)
        assert kept == [rereport]

        # Without the prior finding the same citation is out of scope.
        inputs.pop("prev_self_check_issues")
        kept, stats = _validate_and_filter_issues([rereport], inputs)
        assert kept == []
        assert stats["bad_evidence_count"] == 1

    def test_prior_line_citation_does_not_widen_as_a_bare_path(self):
        # Widening strips the line suffix: keeping "src/foo.py:10" in the PATH
        # set would let that same text ground at path level on an
        # anchor-BEARING path and bypass its causal-anchor check.
        prev = self._issue()
        prev["location"] = "src/foo.py:10"
        prev["evidence_lines"] = ["src/foo.py:10"]
        inputs = dict(self._inputs())
        inputs["scope_changed_paths"] = ["src/foo.py"]
        inputs["scope_causal_anchors"] = {"src/foo.py": [[20, 22]]}
        inputs["prev_self_check_issues"] = [prev]

        fresh = self._issue("regression", "")
        fresh["evidence_lines"] = ["src/foo.py:10"]
        kept, stats = _validate_and_filter_issues([fresh], inputs)
        assert kept == []
        assert stats["bad_evidence_count"] == 1

    def test_undecidable_scope_accepts_regression_missing_in(self):
        # Both baselines unreconstructable: the evidence_lines channel accepts
        # the path, so the missing_in form the prompt authorizes must too.
        inputs = dict(self._inputs())
        inputs["scope_undecidable"] = True
        inputs["scope_changed_paths"] = []
        inputs["scope_causal_anchors"] = {}
        inputs["changes_made"] = {"files_changed": ["src/sorter.py"]}

        issue = self._issue("regression", "")
        issue["evidence_lines"] = []
        issue["missing_in"] = ["src/sorter.py"]
        kept, stats = _validate_and_filter_issues([issue], inputs)
        assert kept == [issue]
        assert stats["bad_evidence_count"] == 0

        twin = self._issue("regression", "")
        twin["evidence_lines"] = ["src/sorter.py"]
        twin["missing_in"] = []
        kept, _ = _validate_and_filter_issues([twin], inputs)
        assert kept == [twin]

    def test_undecidable_scope_keeps_evidence_outside_the_summary_list(self):
        # The implement step's self-reported files_changed is not ground truth.
        # Under an undecidable baseline a finding on a genuinely flow-changed
        # file the summary omitted must not be dropped — that is a silent loss
        # in exactly the degraded state the mechanism exists to make safe.
        inputs = dict(self._inputs())
        inputs["scope_undecidable"] = True
        inputs["scope_changed_paths"] = ["src/a.py"]
        inputs["scope_causal_anchors"] = {}
        inputs["changes_made"] = {"files_changed": ["src/a.py"]}

        issue = self._issue("regression", "")
        issue["evidence_lines"] = ["src/b.py:42"]
        kept, stats = _validate_and_filter_issues([issue], inputs)
        assert kept == [issue]
        assert stats["bad_evidence_count"] == 0
        # The relaxation is tallied, not silent.
        assert stats["undecidable_scope_kept_count"] == 1

        # A decidable scope keeps the strict rule.
        inputs["scope_undecidable"] = False
        kept, stats = _validate_and_filter_issues([issue], inputs)
        assert kept == []
        assert stats["bad_evidence_count"] == 1

    def test_undecidable_scope_unions_reconstructed_and_reported_paths(self):
        # Neither source is authoritative when the baseline is undecidable, so
        # the hint set is their union — a partially reconstructed path must not
        # be erased by the summary, nor the summary by it.
        inputs = dict(self._inputs())
        inputs["scope_undecidable"] = True
        inputs["scope_changed_paths"] = ["src/from_diff.py"]
        inputs["changes_made"] = {"files_changed": ["src/from_summary.py"]}
        for path in ("src/from_diff.py", "src/from_summary.py"):
            issue = self._issue("regression", "")
            issue["evidence_lines"] = [f"{path}:7"]
            kept, stats = _validate_and_filter_issues([issue], inputs)
            assert kept == [issue], path
            # Both ground on the changed-path hint itself, no relaxation.
            assert stats["undecidable_scope_kept_count"] == 0, path

    def test_out_of_scope_flag_is_not_a_release_valve(self):
        issue = self._issue("regression", "")
        issue["out_of_scope"] = True
        kept, stats = _validate_and_filter_issues([issue], self._inputs())
        assert kept == [issue]
        assert "out_of_scope_count" not in stats


def test_handler_prompt_uses_effective_requirements_not_task_groups(tmp_path):
    flow = FlowInstance(
        flow_id="test-flow-cr",
        task_description="Implement feature X",
        task_type="feature",
        status=FlowStatus.RUNNING,
        change_path=tmp_path / "changes" / "cr",
    )
    step = Step(
        step_type=StepType.SELF_CHECK,
        status=StepStatus.PENDING,
        inputs={
            "task_description": "Implement feature X\n\n## Additional Instructions\n- preserve Y",
            "task_description_base": "Implement feature X",
            "user_interjections": [{"text": "preserve Y"}],
            "changes_made": {"files_changed": ["src/feature.py"]},
            "test_results": {"passed": True},
            "task_groups": [{"tasks": [{"description": "plan-only Z"}]}],
        },
    )
    with patch("tianluo.engine.steps.self_check.LLMCaller") as caller_cls:
        caller = Mock()
        caller.call.return_value = json.dumps({"issues": [], "summary": "ok"})
        caller_cls.return_value = caller
        assert self_check_handler(step, flow) == StepStatus.COMPLETED
    prompt = caller.call.call_args.kwargs["prompt"]
    assert "Implement feature X" in prompt
    assert "preserve Y" in prompt
    assert "plan-only Z" not in prompt
    assert "Cross-Module Integration" in prompt
