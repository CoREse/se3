"""The implement step's per-fix-round file list (``fix_round_files_changed``).

The fix loop re-enters the SAME implement ``Step`` object
(``state_machine._transition_to_fix``), and ``_resolve_files_changed`` rewrites
``outputs.files_changed`` from a flow-baseline git diff after every round — so
that key is cumulative by construction and a round's own contribution used to be
unrecoverable from the persisted outputs. The round's LLM call DOES self-report
its own ``files_changed``, but that value was merged into the cumulative list and
immediately overwritten by the git-diff resolution.

``_run_single_llm_call`` now persists the round's raw self-reported list (before
the merge), plus the files this round's restricted edits touched, under a new
``fix_round_files_changed`` key. These tests hold the three things that matter:
the key is written on fix rounds only, it carries this round's files rather than
the cumulative set, and NO existing field's semantics moved.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tianluo.engine.models import FlowInstance, Step, StepStatus, StepType
from tianluo.engine.steps import implement as implement_mod


def _step(inputs: dict | None = None, outputs: dict | None = None) -> Step:
    step = Step(
        step_type=StepType.IMPLEMENT,
        step_id="test-implement",
        inputs=dict(inputs or {}),
    )
    step.outputs.update(outputs or {})
    return step


def _flow(tmp_path: Path) -> FlowInstance:
    return FlowInstance(
        task_description="Test task",
        change_path=tmp_path / "tianluo",
    )


def _run(step: Step, flow: FlowInstance, project_root: Path, result: dict | None,
         **kwargs) -> StepStatus:
    """Drive ``_run_single_llm_call`` with a canned parsed LLM result."""
    with patch.object(implement_mod, "LLMCaller") as caller_cls, \
            patch.object(implement_mod, "parse_json_response", return_value=result):
        caller_cls.return_value.call.return_value = "<response>"
        return implement_mod._run_single_llm_call(
            "prompt", step, flow, project_root, [], 0, **kwargs,
        )


# ---------------------------------------------------------------------------
# _is_fix_round — either marker alone is enough
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "inputs,expected",
    [
        ({}, False),
        ({"fix_iteration": 0}, False),
        ({"fix_iteration": 1}, True),
        ({"fix_iteration": "3"}, True),
        ({"is_fix_iteration": True}, True),
        ({"is_fix_iteration": True, "fix_iteration": 2}, True),
        ({"fix_iteration": "not-a-number"}, False),
    ],
)
def test_is_fix_round_reads_either_marker(inputs, expected):
    assert implement_mod._is_fix_round(_step(inputs)) is expected


# ---------------------------------------------------------------------------
# The new key
# ---------------------------------------------------------------------------

def test_first_round_never_writes_the_fix_round_key(tmp_path):
    step = _step()
    status = _run(step, _flow(tmp_path), tmp_path,
                  {"files_changed": ["a.py"], "summary": "did it"})
    assert status == StepStatus.COMPLETED
    assert "fix_round_files_changed" not in step.outputs, (
        "round one has no 'this round vs. cumulative' distinction to record"
    )
    assert step.outputs["files_changed"] == ["a.py"]


def test_fix_round_records_only_this_round_self_reported_files(tmp_path):
    # A fix round carries the previous rounds' cumulative list in outputs, and
    # (holistic path) merges into it — the new key must NOT pick that up.
    step = _step(
        {"is_fix_iteration": True, "fix_iteration": 2},
        {"files_changed": ["src/a.py", "src/b.py"]},
    )
    status = _run(step, _flow(tmp_path), tmp_path,
                  {"files_changed": ["src/c.py"], "summary": "round 2"},
                  preserve_existing_outputs=True)
    assert status == StepStatus.COMPLETED
    assert step.outputs["fix_round_files_changed"] == ["src/c.py"]
    # …while the cumulative key keeps its existing merge semantics.
    assert step.outputs["files_changed"] == ["src/a.py", "src/b.py", "src/c.py"]
    assert step.outputs["summary"] == "round 2"


def test_fix_round_key_is_deduped(tmp_path):
    step = _step({"fix_iteration": 1})
    _run(step, _flow(tmp_path), tmp_path,
         {"files_changed": ["src/a.py", "src/a.py", "", "src/b.py"]})
    assert step.outputs["fix_round_files_changed"] == ["src/a.py", "src/b.py"]


def test_fix_round_key_includes_this_round_restricted_edit_files(tmp_path):
    step = _step({"fix_iteration": 1})
    applied = [{"file_path": ".claude/settings.json"}]
    with patch.object(implement_mod, "_apply_restricted_edits",
                      return_value=(applied, [])):
        _run(step, _flow(tmp_path), tmp_path, {
            "files_changed": ["src/a.py"],
            "restricted_edits": [{
                "file_path": ".claude/settings.json",
                "old_string": "a", "new_string": "b",
            }],
        })
    assert step.outputs["fix_round_files_changed"] == [
        "src/a.py", ".claude/settings.json",
    ]


def test_fix_round_key_excludes_restricted_edits_carried_from_earlier_rounds(tmp_path):
    """The merged ``restricted_edits_applied`` is cumulative; the round key is not."""
    step = _step(
        {"fix_iteration": 2},
        {"restricted_edits_applied": [{"file_path": ".claude/old.json"}]},
    )
    applied = [{"file_path": ".claude/new.json"}]
    with patch.object(implement_mod, "_apply_restricted_edits",
                      return_value=(applied, [])):
        _run(step, _flow(tmp_path), tmp_path, {
            "files_changed": [],
            "restricted_edits": [{
                "file_path": ".claude/new.json",
                "old_string": "a", "new_string": "b",
            }],
        }, preserve_existing_outputs=True)
    assert step.outputs["fix_round_files_changed"] == [".claude/new.json"]
    # The existing cumulative field keeps carrying BOTH rounds.
    assert [e["file_path"] for e in step.outputs["restricted_edits_applied"]] == [
        ".claude/old.json", ".claude/new.json",
    ]


def test_unparseable_fix_round_clears_the_previous_rounds_value(tmp_path):
    """A round that reported nothing must not leave round N-1 standing as "this round"."""
    step = _step(
        {"fix_iteration": 2},
        {"fix_round_files_changed": ["src/round1.py"],
         "files_changed": ["src/round1.py"]},
    )
    _run(step, _flow(tmp_path), tmp_path, None, preserve_existing_outputs=True)
    assert step.outputs["fix_round_files_changed"] == []


# ---------------------------------------------------------------------------
# Nothing else moved
# ---------------------------------------------------------------------------

def test_existing_outputs_are_untouched_for_a_non_fix_round(tmp_path):
    step = _step()
    result = {
        "files_changed": ["src/a.py"],
        "tests_added": ["tests/test_a.py"],
        "test_mapping": {"src/a.py": "tests/test_a.py"},
        "summary": "done",
        "completion_status": "complete",
        "incomplete_tasks": [],
        "estimated_test_duration": 90,
    }
    status = _run(step, _flow(tmp_path), tmp_path, result)
    assert status == StepStatus.COMPLETED
    assert step.outputs["files_changed"] == ["src/a.py"]
    assert step.outputs["tests_added"] == ["tests/test_a.py"]
    assert step.outputs["test_mapping"] == {"src/a.py": "tests/test_a.py"}
    assert step.outputs["summary"] == "done"
    assert step.outputs["completion_status"] == "complete"
    assert step.outputs["estimated_test_duration"] == 90
    assert step.outputs["group_summaries"] == [{"group_id": "", "summary": "done"}]


def test_fix_round_with_empty_summary_still_keeps_the_previous_one(tmp_path):
    """Pre-existing semantics: an empty round summary preserves the old value."""
    step = _step({"fix_iteration": 1}, {"summary": "round 1 summary"})
    _run(step, _flow(tmp_path), tmp_path,
         {"files_changed": ["src/a.py"], "summary": ""},
         preserve_existing_outputs=True)
    assert step.outputs["summary"] == "round 1 summary"
    assert step.outputs["fix_round_files_changed"] == ["src/a.py"]


def test_resolve_files_changed_still_overwrites_only_the_cumulative_key(tmp_path):
    """The git-diff resolution is what makes ``files_changed`` cumulative.

    It must keep doing exactly that — and must NOT touch the round key, or the
    new block would show the cumulative set again.
    """
    step = _step(
        {"fix_iteration": 1},
        {"files_changed": ["src/c.py"], "fix_round_files_changed": ["src/c.py"]},
    )
    completed = MagicMock(returncode=0, stdout="src/a.py\nsrc/b.py\nsrc/c.py\n")
    with patch.object(implement_mod, "_run_git", return_value=completed):
        implement_mod._resolve_files_changed(step, tmp_path, "abc123")
    assert step.outputs["files_changed"] == ["src/a.py", "src/b.py", "src/c.py"]
    assert step.outputs["fix_round_files_changed"] == ["src/c.py"]
