"""Tests for the invariant_check step (src/se3/engine/steps/invariant_check.py).

INVARIANT_CHECK is the anchored replacement for the retired spec_gate/spec_check.
Coverage:

- The verbatim_quote anchoring: an ungrounded "nit" whose quote is not a literal
  substring of {task_description, charter, why-comments} is dropped → COMPLETED.
- A grounded violation (quote ∈ charter, evidence ∈ changed files) survives →
  REVISION_NEEDED with fix outputs.
- Coverage is limited to *recorded* invariants (the anchor pool is the only
  source of truth).
- No diff (e.g. the review flow's invariant_check) → cheap pass, no LLM call.
- No anchors recorded → cheap pass; an anchor-less self-check is refused.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from se3.engine.steps import invariant_check
from se3.engine.models import FlowInstance, FlowStatus, Step, StepStatus, StepType


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

CHARTER_TEXT = (
    "# Demo — Charter\n\n## Purpose\nDemo project.\n\n"
    "### Requirement: Key Constraints\n"
    "- The engine MUST never write project source files from a read-only step.\n"
)


def _make_flow(project_root: Path, task: str = "Implement the widget") -> FlowInstance:
    flow = FlowInstance(
        task_description=task,
        task_type="feature",
        status=FlowStatus.INIT,
    )
    flow.change_path = project_root / "change"
    return flow


def _make_step(inputs: dict) -> Step:
    return Step(step_type=StepType.INVARIANT_CHECK, inputs=inputs)


def _install_fake_caller(monkeypatch, responses):
    """Replace ``invariant_check.LLMCaller`` with a scripted fake."""
    state = {"prompts": [], "responses": list(responses), "calls": 0}

    class FakeCaller:
        def __init__(self, *args, **kwargs):
            pass

        def call(self, prompt, **kwargs):
            state["calls"] += 1
            state["prompts"].append(prompt)
            return state["responses"].pop(0)

    monkeypatch.setattr(invariant_check, "LLMCaller", FakeCaller)
    return state


def _grounded_issue(quote: str, path: str) -> dict:
    return {
        "severity": "high",
        "actual_behavior": "the read-only step writes a source file",
        "expected_behavior": "the step must not write source files",
        "divergence": "when the handler runs it edits src/foo.py directly",
        "expectation_source": {"type": "charter", "verbatim_quote": quote},
        "evidence_lines": [f"{path}:10"],
        "missing_in": [],
        "out_of_scope": False,
    }


# ---------------------------------------------------------------------------
# cheap-pass cases
# ---------------------------------------------------------------------------

def test_no_diff_passes_cheap_without_llm(tmp_path, monkeypatch):
    """The review flow's invariant_check (no diff) passes for free, no LLM call."""
    state = _install_fake_caller(monkeypatch, [])  # any call would IndexError
    flow = _make_flow(tmp_path)
    step = _make_step({"charter": CHARTER_TEXT, "changes_made": {}})

    result = invariant_check.invariant_check_handler(step, flow)

    assert result is StepStatus.COMPLETED
    assert state["calls"] == 0
    assert step.outputs["skipped_reason"] == "no_diff"
    assert step.outputs["actionable_count"] == 0


def test_no_anchors_refuses_anchorless_check(tmp_path, monkeypatch):
    """With a diff but zero recorded anchors, the step refuses to invent an
    invariant: cheap pass, no LLM call."""
    state = _install_fake_caller(monkeypatch, [])
    flow = _make_flow(tmp_path, task="")  # no task anchor
    step = _make_step({
        "task_description": "",
        "charter": "",          # no charter anchor
        "why_comments": [],     # no why-comment anchor
        "changes_made": {"files_changed": ["src/foo.py"]},
    })

    result = invariant_check.invariant_check_handler(step, flow)

    assert result is StepStatus.COMPLETED
    assert state["calls"] == 0
    assert step.outputs["skipped_reason"] == "no_anchors"


# ---------------------------------------------------------------------------
# anchoring
# ---------------------------------------------------------------------------

def test_ungrounded_nit_is_filtered(tmp_path, monkeypatch):
    """An issue whose verbatim_quote is NOT in the anchor pool is dropped, so
    the step passes — coverage is limited to recorded invariants."""
    ungrounded = {
        "severity": "high",
        "actual_behavior": "uses a global variable",
        "expected_behavior": "should use dependency injection",
        "divergence": "always",
        # This phrase appears nowhere in the charter / task / why-comments.
        "expectation_source": {
            "type": "charter",
            "verbatim_quote": "dependency injection is mandatory everywhere",
        },
        "evidence_lines": ["src/foo.py:3"],
        "missing_in": [],
        "out_of_scope": False,
    }
    state = _install_fake_caller(
        monkeypatch, [json.dumps({"issues": [ungrounded], "summary": "x"})]
    )
    flow = _make_flow(tmp_path)
    step = _make_step({
        "charter": CHARTER_TEXT,
        "changes_made": {"files_changed": ["src/foo.py"]},
    })

    result = invariant_check.invariant_check_handler(step, flow)

    assert state["calls"] == 1
    assert result is StepStatus.COMPLETED
    assert step.outputs["issues"] == []
    # The raw issue was seen but dropped by the source-pool substring check.
    assert step.outputs["validation_stats"]["quote_not_in_source_count"] == 1


def test_grounded_violation_routes_to_fix(tmp_path, monkeypatch):
    """A violation quoting the charter verbatim, with evidence in a changed
    file, survives → REVISION_NEEDED with fix outputs."""
    quote = "The engine MUST never write project source files from a read-only step."
    issue = _grounded_issue(quote, "src/foo.py")
    state = _install_fake_caller(
        monkeypatch, [json.dumps({"issues": [issue], "summary": "violation"})]
    )
    flow = _make_flow(tmp_path)
    step = _make_step({
        "charter": CHARTER_TEXT,
        "changes_made": {"files_changed": ["src/foo.py"]},
    })

    result = invariant_check.invariant_check_handler(step, flow)

    assert result is StepStatus.REVISION_NEEDED
    assert step.outputs["actionable_count"] == 1
    assert step.outputs["fix_needed"] is True
    assert step.outputs["fix_context"]["reason"] == "invariant_check"
    assert "invariant" in step.outputs["fix_instructions"].lower()


def test_evidence_must_point_at_changed_file(tmp_path, monkeypatch):
    """A quote that IS in the charter but whose evidence path is not among the
    changed files is dropped (the anchoring is two-sided)."""
    quote = "The engine MUST never write project source files from a read-only step."
    issue = _grounded_issue(quote, "src/unrelated.py")  # not in files_changed
    state = _install_fake_caller(
        monkeypatch, [json.dumps({"issues": [issue], "summary": "x"})]
    )
    flow = _make_flow(tmp_path)
    step = _make_step({
        "charter": CHARTER_TEXT,
        "changes_made": {"files_changed": ["src/foo.py"]},
    })

    result = invariant_check.invariant_check_handler(step, flow)

    assert result is StepStatus.COMPLETED
    assert step.outputs["issues"] == []
    assert step.outputs["validation_stats"]["bad_evidence_count"] == 1


def test_why_comment_anchor_is_harvested_from_changed_file(tmp_path, monkeypatch):
    """A why-comment colocated in a changed file enters the anchor pool, so a
    violation quoting it survives."""
    src = tmp_path / "src"
    src.mkdir()
    f = src / "widget.py"
    f.write_text(
        "# Invariant: the cache key must include the tenant id\n"
        "def build_key():\n    return 'global'\n",
        encoding="utf-8",
    )
    quote = "the cache key must include the tenant id"
    issue = _grounded_issue(quote, "src/widget.py")
    issue["expectation_source"]["type"] = "why_comment"
    state = _install_fake_caller(
        monkeypatch, [json.dumps({"issues": [issue], "summary": "x"})]
    )
    # No charter, no task — the ONLY anchor is the harvested why-comment.
    flow = _make_flow(tmp_path, task="")
    step = _make_step({
        "task_description": "",
        "charter": "",
        "changes_made": {"files_changed": ["src/widget.py"]},
    })

    result = invariant_check.invariant_check_handler(step, flow)

    assert state["calls"] == 1
    assert result is StepStatus.REVISION_NEEDED
    assert step.outputs["actionable_count"] == 1


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_deleted_why_comment_invariant_recovered_from_baseline(tmp_path, monkeypatch):
    """A why-comment documenting a binding invariant must remain anchorable even
    when the violating diff DELETED that comment.

    Regression guard: the harvest reads each touched file's baseline (flow-start)
    content via ``git show <baseline>:<path>``, not just the working tree, so an
    implementation that erases the original comment while breaking the invariant
    cannot launder the violation past the anchored check.
    """
    src = tmp_path / "src"
    src.mkdir()
    f = src / "ledger.py"
    f.write_text(
        "# Invariant: account balance must never go negative\n"
        "def debit(x):\n    return x\n",
        encoding="utf-8",
    )
    _git(["init"], tmp_path)
    _git(["config", "user.email", "t@t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    _git(["add", "src/ledger.py"], tmp_path)
    _git(["commit", "-m", "baseline"], tmp_path)
    baseline = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True
    ).stdout.strip()

    # The violating change rewrites the file and DELETES the invariant comment.
    f.write_text("def debit(x):\n    return -x\n", encoding="utf-8")

    quote = "account balance must never go negative"
    issue = _grounded_issue(quote, "src/ledger.py")
    issue["expectation_source"]["type"] = "why_comment"
    state = _install_fake_caller(
        monkeypatch, [json.dumps({"issues": [issue], "summary": "x"})]
    )
    # The ONLY anchor is the deleted why-comment, recoverable from baseline.
    flow = _make_flow(tmp_path, task="")
    flow.baseline_commit = baseline
    step = _make_step({
        "task_description": "",
        "charter": "",
        "changes_made": {"files_changed": ["src/ledger.py"]},
    })

    result = invariant_check.invariant_check_handler(step, flow)

    assert state["calls"] == 1
    assert result is StepStatus.REVISION_NEEDED
    assert step.outputs["actionable_count"] == 1


def test_unparsable_llm_response_fails(tmp_path, monkeypatch):
    state = _install_fake_caller(monkeypatch, ["not json at all"])
    flow = _make_flow(tmp_path)
    step = _make_step({
        "charter": CHARTER_TEXT,
        "changes_made": {"files_changed": ["src/foo.py"]},
    })

    result = invariant_check.invariant_check_handler(step, flow)

    assert result is StepStatus.FAILED
    assert step.error_message
