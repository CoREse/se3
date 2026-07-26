"""Tests for the self_check Per-Task Correctness + Regression sections.

Covers G3:
- the new prompt sections (Per-Task Correctness hard audit, Regression /
  Unintended Side Effects) and the removal of the old soft-reference wording;
- ``_build_source_pool`` now folds in task_groups text (task description +
  acceptance_criteria) so ``plan_task`` quotes can ground there;
- ``_validate_and_filter_issues`` keeps ``plan_task`` issues whose quote hits a
  task's text and ``regression`` issues that carry valid diff evidence (the
  latter bypass the verbatim-quote check), while still dropping ungrounded ones.
"""

from __future__ import annotations

import json
from unittest.mock import Mock, patch

import pytest

from tianluo.engine.models import FlowInstance, FlowStatus, Step, StepStatus, StepType
from tianluo.engine.steps.self_check import (
    SELF_CHECK_PROMPT,
    _build_source_pool,
    _validate_and_filter_issues,
    self_check_handler,
)


# ---------------------------------------------------------------------------
# Prompt content: the two new sections + removed soft wording
# ---------------------------------------------------------------------------


class TestPromptSections:
    def test_prompt_has_per_task_correctness_section(self):
        assert "Per-Task Correctness" in SELF_CHECK_PROMPT
        assert "HARD AUDIT" in SELF_CHECK_PROMPT
        # The correctness dimension instructs the reviewer to infer the
        # task→change correspondence from the actual diff.
        assert "diff" in SELF_CHECK_PROMPT
        assert 'expectation_source.type = "plan_task"' in SELF_CHECK_PROMPT

    def test_prompt_has_regression_section(self):
        assert "Regression / Unintended Side Effects" in SELF_CHECK_PROMPT
        assert "outside the scope" in SELF_CHECK_PROMPT.lower() or (
            "OUTSIDE the scope" in SELF_CHECK_PROMPT
        )
        assert 'expectation_source.type = "regression"' in SELF_CHECK_PROMPT

    def test_prompt_dropped_soft_reference_wording(self):
        # The flipped task_groups intro must no longer carry the soft
        # "scope reference / reasonable deviation" disclaimer.
        assert "NOT a strict specification" not in SELF_CHECK_PROMPT
        assert "Reasonable deviations from the plan" not in SELF_CHECK_PROMPT
        assert "missing-plan-compliance" not in SELF_CHECK_PROMPT
        assert "plan-conformance audit" not in SELF_CHECK_PROMPT

    def test_prompt_retains_integral_dimensions(self):
        # Robustness / test-coverage dimensions are preserved, not split out.
        assert "Code Robustness" in SELF_CHECK_PROMPT
        assert "Test Coverage Gaps" in SELF_CHECK_PROMPT

    def test_prompt_retains_what_not_to_check(self):
        # version / downstream-step ownership guard must survive the rewrite.
        assert "What NOT to check" in SELF_CHECK_PROMPT
        assert "version_analyze" in SELF_CHECK_PROMPT
        assert "downstream" in SELF_CHECK_PROMPT.lower()

    def test_expectation_source_schema_lists_new_types(self):
        # The schema doc enumerates the two new grounding types.
        assert "plan_task" in SELF_CHECK_PROMPT
        assert "regression" in SELF_CHECK_PROMPT

    def test_prompt_format_placeholders_intact(self):
        # The .format placeholders must still be present and balanced so the
        # handler's .format() call does not raise.
        for placeholder in (
            "{task_description}",
            "{changes_made}",
            "{test_results}",
            "{spec_content}",
            "{task_groups_section}",
            "{fix_context}",
        ):
            assert placeholder in SELF_CHECK_PROMPT
        # A smoke .format with all placeholders filled must not raise
        # (guards against an unescaped stray brace introduced by the edits).
        SELF_CHECK_PROMPT.format(
            task_description="t",
            changes_made="c",
            test_results="r",
            spec_content="s",
            task_groups_section="",
            fix_context="f",
        )


# ---------------------------------------------------------------------------
# _build_source_pool: task_groups text is folded in
# ---------------------------------------------------------------------------


class TestSourcePoolTaskGroups:
    def _inputs_with_groups(self):
        return {
            "task_description": "Implement feature X",
            "task_groups": [
                {
                    "group_id": "G1",
                    "name": "Auth",
                    "tasks": [
                        {
                            "id": 1,
                            "description": "Add login endpoint returning JWT",
                            "acceptance_criteria": [
                                "Returns 200 on valid creds",
                                "Returns 401 otherwise",
                            ],
                        }
                    ],
                }
            ],
        }

    def test_task_description_in_pool(self):
        pool = _build_source_pool(self._inputs_with_groups())
        assert "Add login endpoint returning JWT" in pool

    def test_acceptance_criteria_in_pool(self):
        pool = _build_source_pool(self._inputs_with_groups())
        assert "Returns 200 on valid creds" in pool
        assert "Returns 401 otherwise" in pool

    def test_missing_task_groups_no_crash(self):
        # Pre-existing callers without task_groups are unaffected.
        assert _build_source_pool({"task_description": "x"}) == ["x"]

    def test_malformed_task_groups_skipped(self):
        pool = _build_source_pool(
            {
                "task_description": "x",
                "task_groups": [
                    "not a dict",
                    {"tasks": "not a list"},
                    {"tasks": ["not a dict", {"description": "good one"}]},
                ],
            }
        )
        assert "good one" in pool
        # Garbage entries didn't inject empty strings.
        assert "" not in pool


# ---------------------------------------------------------------------------
# _validate_and_filter_issues: plan_task + regression grounding
# ---------------------------------------------------------------------------


class TestValidatePlanTaskRegression:
    def _inputs(self):
        return {
            "task_description": "Implement feature X",
            "changes_made": {
                "files_changed": [
                    {"path": "src/feature.py", "action": "modify"},
                    {"path": "src/shared.py", "action": "modify"},
                ]
            },
            "spec_content": {"base": "PEP 8"},
            "task_groups": [
                {
                    "group_id": "G1",
                    "name": "Core",
                    "tasks": [
                        {
                            "id": 1,
                            "description": "Add retry with exponential backoff",
                            "acceptance_criteria": ["Caps retries at 5 attempts"],
                        }
                    ],
                }
            ],
        }

    def _plan_task_issue(self, quote="Add retry with exponential backoff"):
        return {
            "severity": "high",
            "actual_behavior": "retries forever with no cap",
            "expected_behavior": "stops after 5 attempts",
            "divergence": "an always-failing call loops indefinitely",
            "expectation_source": {"type": "plan_task", "verbatim_quote": quote},
            "evidence_lines": ["src/feature.py:10"],
            "missing_in": [],
            "out_of_scope": False,
        }

    def _regression_issue(self, evidence="src/shared.py:5"):
        return {
            "severity": "critical",
            "actual_behavior": "shared.normalize() now lowercases its input",
            "expected_behavior": "shared.normalize() preserves case as before",
            "divergence": "other callers relying on original case now break",
            # A regression's quote describes pre-existing behavior; it is NOT
            # required to be a substring of any source-pool entry.
            "expectation_source": {
                "type": "regression",
                "verbatim_quote": "normalize preserved case",
            },
            "evidence_lines": [evidence],
            "missing_in": [],
            "out_of_scope": False,
        }

    def test_plan_task_issue_kept_when_quote_hits_task(self):
        kept, stats = _validate_and_filter_issues(
            [self._plan_task_issue()], self._inputs()
        )
        assert len(kept) == 1
        assert stats["kept_count"] == 1

    def test_plan_task_issue_kept_when_quote_hits_acceptance_criterion(self):
        kept, _ = _validate_and_filter_issues(
            [self._plan_task_issue(quote="Caps retries at 5 attempts")],
            self._inputs(),
        )
        assert len(kept) == 1

    def test_plan_task_issue_dropped_when_quote_not_in_any_task(self):
        kept, stats = _validate_and_filter_issues(
            [self._plan_task_issue(quote="this phrase is nowhere in the plan")],
            self._inputs(),
        )
        assert kept == []
        assert stats["quote_not_in_source_count"] == 1

    def test_regression_issue_kept_with_valid_evidence(self):
        kept, stats = _validate_and_filter_issues(
            [self._regression_issue()], self._inputs()
        )
        assert len(kept) == 1
        assert stats["kept_count"] == 1

    def test_regression_issue_bypasses_quote_check(self):
        # Even an empty quote is fine for regression — grounding is evidence.
        issue = self._regression_issue()
        issue["expectation_source"]["verbatim_quote"] = ""
        kept, _ = _validate_and_filter_issues([issue], self._inputs())
        assert len(kept) == 1

    def test_regression_issue_dropped_without_evidence(self):
        issue = self._regression_issue()
        issue["evidence_lines"] = []
        issue["missing_in"] = []
        kept, stats = _validate_and_filter_issues([issue], self._inputs())
        assert kept == []
        assert stats["bad_evidence_count"] == 1

    def test_regression_issue_dropped_when_evidence_path_not_changed(self):
        kept, stats = _validate_and_filter_issues(
            [self._regression_issue(evidence="src/unrelated.py:99")],
            self._inputs(),
        )
        assert kept == []
        assert stats["bad_evidence_count"] == 1

    def test_legacy_task_description_grounding_still_works(self):
        # The pre-existing task_description path must not regress.
        issue = self._plan_task_issue()
        issue["expectation_source"] = {
            "type": "task_description",
            "verbatim_quote": "Implement feature X",
        }
        kept, _ = _validate_and_filter_issues([issue], self._inputs())
        assert len(kept) == 1

    def test_out_of_scope_release_valve_still_applies(self):
        issue = self._regression_issue()
        issue["out_of_scope"] = True
        kept, stats = _validate_and_filter_issues([issue], self._inputs())
        assert kept == []
        assert stats["out_of_scope_count"] == 1

    def test_wholly_missing_task_survives_via_missing_in(self):
        # A planned task implemented nowhere has NO changed lines to cite, so the
        # reviewer grounds it on missing_in. This — the most severe correctness
        # failure the hard audit exists to catch — must survive validation
        # rather than being dropped for empty evidence_lines.
        issue = self._plan_task_issue()
        issue["actual_behavior"] = "the retry helper was never implemented"
        issue["evidence_lines"] = []
        issue["missing_in"] = ["src/feature.py"]
        kept, stats = _validate_and_filter_issues([issue], self._inputs())
        assert len(kept) == 1
        assert stats["kept_count"] == 1
        assert stats["bad_evidence_count"] == 0

    def test_plan_task_without_any_grounding_still_dropped(self):
        # missing_in is the grounding for a wholly-missing task; without either
        # evidence_lines or missing_in the issue is genuinely ungrounded and
        # must still be dropped (the relaxation must not become a free pass).
        issue = self._plan_task_issue()
        issue["evidence_lines"] = []
        issue["missing_in"] = []
        kept, stats = _validate_and_filter_issues([issue], self._inputs())
        assert kept == []
        assert stats["bad_evidence_count"] == 1

    def test_regression_dropped_when_grounded_only_by_missing_in(self):
        # A regression claims a change BROKE existing behavior, so it must point
        # at the changed line(s) responsible. missing_in (a file that was never
        # edited) is self-contradictory grounding for a regression and would
        # leave the implement step with no concrete location to fix — drop it
        # even though missing_in would satisfy the wholly-missing plan_task case.
        issue = self._regression_issue()
        issue["evidence_lines"] = []
        issue["missing_in"] = ["src/config.py"]
        kept, stats = _validate_and_filter_issues([issue], self._inputs())
        assert kept == []
        assert stats["bad_evidence_count"] == 1

    def test_plan_task_quote_matches_ellipsis_capped_line(self):
        # When _format_task_groups caps a long task description with a trailing
        # ellipsis under budget pressure, a reviewer who quotes the prompt-
        # visible capped line (prefix + "…") must still ground against the full
        # untruncated description in the source pool.
        inputs = self._inputs()
        full_desc = "Add retry with exponential backoff"
        # Simulate the reviewer copying a prompt-visible capped line.
        capped_quote = full_desc[:20].rstrip() + "…"
        issue = self._plan_task_issue(quote=capped_quote)
        kept, stats = _validate_and_filter_issues([issue], inputs)
        assert len(kept) == 1
        assert stats["quote_not_in_source_count"] == 0

    def test_plan_task_quote_matches_visible_bullet_id_line(self):
        # _render_task_groups prefixes every task line with a Markdown bullet
        # and the task id ("- [1] <desc>…"). A reviewer who copies that exact
        # prompt-visible line — bullet, id, AND trailing ellipsis — must still
        # ground against the raw description stored in the source pool.
        inputs = self._inputs()
        full_desc = "Add retry with exponential backoff"
        visible_line = "- [1] " + full_desc[:20].rstrip() + "…"
        issue = self._plan_task_issue(quote=visible_line)
        kept, stats = _validate_and_filter_issues([issue], inputs)
        assert len(kept) == 1
        assert stats["quote_not_in_source_count"] == 0

    def test_plan_task_quote_matches_visible_bullet_line_no_ellipsis(self):
        # Same as above but for a short (uncapped) task whose visible line keeps
        # the bullet/id prefix without a trailing ellipsis.
        inputs = self._inputs()
        visible_line = "- [1] Add retry with exponential backoff"
        issue = self._plan_task_issue(quote=visible_line)
        kept, stats = _validate_and_filter_issues([issue], inputs)
        assert len(kept) == 1
        assert stats["quote_not_in_source_count"] == 0


class TestPromptGroundingDirectives:
    def test_correctness_dimension_directs_missing_in_for_unimplemented_task(self):
        # The per-task dimension must tell the reviewer to ground a wholly-
        # unimplemented task on missing_in (else such findings get dropped).
        assert "missing_in" in SELF_CHECK_PROMPT
        # The directive ties an entirely-unimplemented task to missing_in.
        lowered = SELF_CHECK_PROMPT.lower()
        assert "entirely unimplemented" in lowered
        assert "missing_in" in SELF_CHECK_PROMPT


# ---------------------------------------------------------------------------
# Handler-level: the new sections reach the prompt with task_groups present
# ---------------------------------------------------------------------------


@pytest.fixture
def flow(tmp_path):
    f = FlowInstance(
        flow_id="test-flow-cr",
        task_description="Implement feature X",
        task_type="feature",
        status=FlowStatus.RUNNING,
        change_path=tmp_path / "changes" / "cr",
    )
    f.state.selected_steps = [
        StepType.IMPLEMENT,
        StepType.TEST,
        StepType.SELF_CHECK,
        StepType.VERIFY_SPEC,
    ]
    return f


def test_handler_prompt_carries_new_sections(flow):
    step = Step(
        step_type=StepType.SELF_CHECK,
        status=StepStatus.PENDING,
        inputs={
            "task_description": "Implement feature X",
            "changes_made": {"files_changed": ["src/feature.py"]},
            "test_results": {"passed": True, "returncode": 0, "stdout": "ok"},
            "spec_content": {"base": "Base spec content"},
            "task_groups": [
                {
                    "group_id": "G1",
                    "name": "Core",
                    "tasks": [{"id": 1, "description": "do the thing"}],
                }
            ],
        },
    )
    response = json.dumps({"issues": [], "summary": "ok"})
    with patch("tianluo.engine.steps.self_check.LLMCaller") as mock_cls:
        mock_caller = Mock()
        mock_caller.call.return_value = response
        mock_cls.return_value = mock_caller
        result = self_check_handler(step, flow)
    assert result == StepStatus.COMPLETED
    prompt = mock_caller.call.call_args.kwargs["prompt"]
    assert "Per-Task Correctness" in prompt
    assert "Regression / Unintended Side Effects" in prompt
    assert "HARD AUDIT" in prompt
    assert "do the thing" in prompt
