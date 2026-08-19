"""Tests for the per-group structured summary list on IMPLEMENT outputs.

``step.outputs["group_summaries"]`` pairs each real ``group_id`` with the
summary that group's own implement call reported. It exists because the
aggregate ``summary`` string is a lossy ``"; ".join(...)``: a group summary may
itself contain semicolons, so the string can neither be split back into groups
nor tell which group a fragment came from. The renderers consume the structured
list; the string is kept verbatim for its existing downstream consumers.

Covered here: the sequential aggregation point (fresh and resumed) and the
holistic single-call path, plus the resume payload the handler hands to the DAG
path. The DAG aggregation point itself is covered in test_implement_dag.py.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent / "src"))

from tianluo.engine.models import FlowInstance, Step, StepStatus, StepType
from tianluo.engine.steps.implement import (
    _lone_group_id,
    _normalize_group_summaries,
    _prior_group_summaries,
    implement_handler,
)

_IMP = "tianluo.engine.steps.implement"


def _groups(*group_ids):
    """Groups carrying enough LOC to clear the merge-into-one-call threshold."""
    return [
        {
            "group_id": gid,
            "group_order": i + 1,
            "depends_on": [],
            "tasks": [{"id": f"t-{gid}", "estimated_loc": 500}],
        }
        for i, gid in enumerate(group_ids)
    ]


class TestNormalizeHelpers:
    """The persisted list is narrowed on read — restored state can hold anything."""

    def test_non_list_yields_empty(self):
        assert _normalize_group_summaries(None) == []
        assert _normalize_group_summaries("G1: did stuff") == []

    def test_entries_are_coerced_and_junk_dropped(self):
        assert _normalize_group_summaries([
            {"group_id": "G1", "summary": "a"},
            "not a dict",
            {"summary": "no group id"},
            {"group_id": "G3"},
        ]) == [
            {"group_id": "G1", "summary": "a"},
            {"group_id": "", "summary": "no group id"},
            {"group_id": "G3", "summary": ""},
        ]

    def test_prior_selection_filters_to_requested_groups(self):
        outputs = {"group_summaries": [
            {"group_id": "G1", "summary": "a"},
            {"group_id": "G2", "summary": "b"},
        ]}
        assert _prior_group_summaries(outputs, {"G2"}) == [
            {"group_id": "G2", "summary": "b"},
        ]

    def test_lone_group_id_only_for_a_single_real_group(self):
        assert _lone_group_id(["G3"]) == "G3"
        assert _lone_group_id([{"group_id": "G4"}]) == "G4"
        # No group, or more than one — never invent a positional label.
        assert _lone_group_id([]) == ""
        assert _lone_group_id(["G1", "G2"]) == ""
        assert _lone_group_id(None) == ""


class _HandlerCase:
    """Shared fixture plumbing for handler-level implement runs."""

    def setup_method(self):
        self.tmpdir = tempfile.mkdtemp()
        self.project_root = Path(self.tmpdir)
        self.flow = FlowInstance(
            flow_id="test-flow-group-summaries",
            task_description="Test task",
            task_type="feature",
            change_path=self.project_root / "change",
        )

    def teardown_method(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _step(self, groups, outputs=None, resumed=False):
        inputs = {
            "task_description": "test",
            "task_type": "feature",
            "task_groups": groups,
            "spec_content": {},
        }
        if resumed:
            inputs["resumed"] = True
        return Step(
            step_type=StepType.IMPLEMENT,
            step_id="impl-gs",
            inputs=inputs,
            outputs=dict(outputs or {}),
        )


@patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value=None)
@patch(f"{_IMP}._should_use_dag", return_value=False)
@patch(f"{_IMP}.LLMCaller")
class TestSequentialAggregation(_HandlerCase):
    """The sequential group loop records one entry per real group."""

    def test_each_group_gets_its_real_id_and_own_summary(
        self, mock_caller_cls, mock_dag, mock_inj,
    ):
        mock_caller_cls.return_value.call.return_value = "response"
        per_group = {
            "G1": "added a; wired a",
            "G2": "fixed b; covered b",
        }
        seen = []

        def fake_parse(response, required_keys=None):
            gid = ["G1", "G2"][len(seen)]
            seen.append(gid)
            return {
                "files_changed": [],
                "tests_added": [],
                "test_mapping": {},
                "summary": per_group[gid],
                "completion_status": "complete",
            }

        step = self._step(_groups("G1", "G2"))
        with patch(f"{_IMP}.parse_json_response", side_effect=fake_parse):
            result = implement_handler(step, self.flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["group_summaries"] == [
            {"group_id": "G1", "summary": "added a; wired a"},
            {"group_id": "G2", "summary": "fixed b; covered b"},
        ]
        # The joined string keeps its historical semantics untouched — which is
        # exactly why it cannot be split back into two groups.
        assert step.outputs["summary"] == "added a; wired a; fixed b; covered b"
        assert step.outputs["implemented_groups"] == ["G1", "G2"]

    def test_resume_keeps_the_list_aligned_with_implemented_groups(
        self, mock_caller_cls, mock_dag, mock_inj,
    ):
        mock_caller_cls.return_value.call.return_value = "response"

        step = self._step(
            _groups("G1", "G2"),
            outputs={
                "implemented_groups": ["G1"],
                "group_summaries": [{"group_id": "G1", "summary": "added a; wired a"}],
                "files_changed": [],
                "tests_added": [],
                "test_mapping": {},
            },
            resumed=True,
        )
        with patch(f"{_IMP}.parse_json_response", return_value={
            "files_changed": [],
            "tests_added": [],
            "test_mapping": {},
            "summary": "fixed b",
            "completion_status": "complete",
        }):
            result = implement_handler(step, self.flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["implemented_groups"] == ["G1", "G2"]
        # The skipped group keeps its real pre-resume summary rather than the
        # "(previously completed)" placeholder the prompt context uses.
        assert step.outputs["group_summaries"] == [
            {"group_id": "G1", "summary": "added a; wired a"},
            {"group_id": "G2", "summary": "fixed b"},
        ]
        assert [e["group_id"] for e in step.outputs["group_summaries"]] == \
            step.outputs["implemented_groups"]

    def test_resume_of_a_pre_field_flow_still_aligns(
        self, mock_caller_cls, mock_dag, mock_inj,
    ):
        """An old flow has no persisted list; the completed group still gets a
        (summary-less) slot so the list stays index-aligned."""
        mock_caller_cls.return_value.call.return_value = "response"

        step = self._step(
            _groups("G1", "G2"),
            outputs={"implemented_groups": ["G1"]},
            resumed=True,
        )
        with patch(f"{_IMP}.parse_json_response", return_value={
            "files_changed": [],
            "tests_added": [],
            "test_mapping": {},
            "summary": "fixed b",
            "completion_status": "complete",
        }):
            implement_handler(step, self.flow)

        assert step.outputs["group_summaries"] == [
            {"group_id": "G1", "summary": ""},
            {"group_id": "G2", "summary": "fixed b"},
        ]


@patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value=None)
@patch(f"{_IMP}.LLMCaller")
class TestSingleCallAggregation(_HandlerCase):
    """The single-call path writes exactly one entry, never a fabricated G1."""

    def test_single_group_entry_carries_that_group_id(
        self, mock_caller_cls, mock_inj,
    ):
        mock_caller_cls.return_value.call.return_value = "response"
        step = self._step([{"group_id": "G4", "tasks": [{"id": "t1"}]}])
        with patch(f"{_IMP}.parse_json_response", return_value={
            "files_changed": [],
            "tests_added": [],
            "test_mapping": {},
            "summary": "did it; twice",
            "completion_status": "complete",
        }):
            result = implement_handler(step, self.flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["group_summaries"] == [
            {"group_id": "G4", "summary": "did it; twice"},
        ]

    def test_no_groups_yields_an_unlabelled_entry(self, mock_caller_cls, mock_inj):
        mock_caller_cls.return_value.call.return_value = "response"
        step = self._step([])
        with patch(f"{_IMP}.parse_json_response", return_value={
            "files_changed": [],
            "tests_added": [],
            "test_mapping": {},
            "summary": "did it",
            "completion_status": "complete",
        }):
            implement_handler(step, self.flow)

        assert step.outputs["group_summaries"] == [
            {"group_id": "", "summary": "did it"},
        ]

    def test_empty_summary_yields_no_entry(self, mock_caller_cls, mock_inj):
        mock_caller_cls.return_value.call.return_value = "response"
        step = self._step([])
        with patch(f"{_IMP}.parse_json_response", return_value={
            "files_changed": [],
            "tests_added": [],
            "test_mapping": {},
            "summary": "",
            "completion_status": "complete",
        }):
            implement_handler(step, self.flow)

        assert step.outputs["group_summaries"] == []


class TestDagResumePayload(_HandlerCase):
    """The handler must hand the prior list to _run_dag_parallel on resume."""

    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch(f"{_IMP}.has_new_commits", return_value=False)
    @patch(f"{_IMP}.get_current_branch", return_value="main")
    @patch(f"{_IMP}.has_commits", return_value=True)
    @patch(f"{_IMP}._run_dag_parallel", return_value=StepStatus.COMPLETED)
    def test_prior_outputs_carry_completed_group_summaries(
        self, mock_dag, mock_has_commits, mock_branch, mock_newc, mock_inj,
    ):
        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": [],
             "tasks": [{"id": 1, "estimated_loc": 500}]},
            {"group_id": "G2", "group_order": 2, "depends_on": [],
             "tasks": [{"id": 2, "estimated_loc": 500}]},
        ]
        step = self._step(
            groups,
            outputs={
                "implemented_groups": ["G1"],
                "group_summaries": [
                    {"group_id": "G1", "summary": "added a; wired a"},
                    # Stale entry for a group not in implemented_groups.
                    {"group_id": "G9", "summary": "should not travel"},
                ],
                "files_changed": [],
                "tests_added": [],
                "test_mapping": {},
            },
            resumed=True,
        )
        implement_handler(step, self.flow)

        prior = mock_dag.call_args.kwargs["prior_outputs"]
        assert prior["group_summaries"] == [
            {"group_id": "G1", "summary": "added a; wired a"},
        ]

    @patch("tianluo.engine.context_builder.get_issue_discovery_injection", return_value=None)
    @patch(f"{_IMP}.has_commits", return_value=True)
    @patch(f"{_IMP}._run_dag_parallel", return_value=StepStatus.COMPLETED)
    def test_all_completed_resume_also_carries_the_list(
        self, mock_dag, mock_has_commits, mock_inj,
    ):
        groups = [
            {"group_id": "G1", "group_order": 1, "depends_on": [],
             "tasks": [{"id": 1, "estimated_loc": 500}]},
            {"group_id": "G2", "group_order": 2, "depends_on": [],
             "tasks": [{"id": 2, "estimated_loc": 500}]},
        ]
        step = self._step(
            groups,
            outputs={
                "implemented_groups": ["G1", "G2"],
                "group_summaries": [
                    {"group_id": "G1", "summary": "added a"},
                    {"group_id": "G2", "summary": "added b"},
                ],
                "files_changed": [],
                "tests_added": [],
                "test_mapping": {},
            },
            resumed=True,
        )
        implement_handler(step, self.flow)

        prior = mock_dag.call_args.kwargs["prior_outputs"]
        assert sorted(e["group_id"] for e in prior["group_summaries"]) == ["G1", "G2"]
