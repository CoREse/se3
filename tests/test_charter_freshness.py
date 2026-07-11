"""Tests for the charter_freshness step (src/se3/engine/steps/charter_freshness.py).

CHARTER_FRESHNESS is a flow-end advisory (never blocking) that reuses the
version_analyze "LLM reads the change -> recommends" shape. Coverage:

- No diff -> cheap pass, no LLM call, charter_update_needed=False, COMPLETED.
- Diff that does NOT touch a charter class -> COMPLETED, update not needed.
- Diff that DOES touch a charter class -> COMPLETED (non-blocking) with an
  update prompt + suggested_update.
- An LLM failure degrades to a soft no-op (still COMPLETED, never blocks).
- The admission-check trigger fires only when the diff edited se3/charter.md.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from se3.engine.steps import charter_freshness
from se3.engine.models import FlowInstance, FlowStatus, Step, StepStatus, StepType
from se3.engine import charter


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_flow(project_root: Path, task: str = "Tweak the widget loop") -> FlowInstance:
    flow = FlowInstance(
        task_description=task,
        task_type="feature",
        status=FlowStatus.INIT,
    )
    flow.change_path = project_root / "change"
    return flow


def _make_step(inputs: dict) -> Step:
    return Step(step_type=StepType.CHARTER_FRESHNESS, inputs=inputs)


def _install_fake_caller(monkeypatch, responses):
    state = {"prompts": [], "responses": list(responses), "calls": 0}

    class FakeCaller:
        def __init__(self, *args, **kwargs):
            pass

        def call(self, prompt, **kwargs):
            state["calls"] += 1
            state["prompts"].append(prompt)
            if not state["responses"]:
                raise AssertionError("unexpected extra LLM call")
            return state["responses"].pop(0)

    monkeypatch.setattr(charter_freshness, "LLMCaller", FakeCaller)
    return state


def _write_charter(project_root: Path, text: str) -> None:
    se3_dir = project_root / "se3"
    se3_dir.mkdir(parents=True, exist_ok=True)
    (se3_dir / "charter.md").write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# cheap pass
# ---------------------------------------------------------------------------

def test_no_diff_passes_cheap_without_llm(tmp_path, monkeypatch):
    state = _install_fake_caller(monkeypatch, [])  # any call raises
    flow = _make_flow(tmp_path)
    step = _make_step({"changes_made": {}})

    result = charter_freshness.charter_freshness_handler(step, flow)

    assert result is StepStatus.COMPLETED
    assert state["calls"] == 0
    assert step.outputs["charter_update_needed"] is False
    assert step.outputs["skipped_reason"] == "no_diff"


# ---------------------------------------------------------------------------
# LLM verdicts (always non-blocking COMPLETED)
# ---------------------------------------------------------------------------

def test_untouched_charter_completes_without_prompt(tmp_path, monkeypatch):
    resp = json.dumps({
        "charter_update_needed": False,
        "touched_classes": [],
        "reason": "Implementation detail only.",
        "suggested_update": "",
    })
    state = _install_fake_caller(monkeypatch, [resp])
    flow = _make_flow(tmp_path)
    step = _make_step({"changes_made": {"files_changed": ["src/foo.py"]}})

    result = charter_freshness.charter_freshness_handler(step, flow)

    assert state["calls"] == 1
    assert result is StepStatus.COMPLETED
    assert step.outputs["charter_update_needed"] is False
    assert step.outputs["touched_classes"] == []


def test_touched_charter_surfaces_update_prompt(tmp_path, monkeypatch):
    resp = json.dumps({
        "charter_update_needed": True,
        "touched_classes": ["architecture"],
        "reason": "Introduced a new top-level subsystem boundary.",
        "suggested_update": "Note the new code-index subsystem in the architecture section.",
    })
    state = _install_fake_caller(monkeypatch, [resp])
    flow = _make_flow(tmp_path)
    step = _make_step({"changes_made": {"files_changed": ["src/se3/engine/code_index.py"]}})

    result = charter_freshness.charter_freshness_handler(step, flow)

    # Hit, but still non-blocking.
    assert result is StepStatus.COMPLETED
    assert step.outputs["charter_update_needed"] is True
    assert step.outputs["touched_classes"] == ["architecture"]
    assert "code-index" in step.outputs["suggested_update"]


def test_llm_failure_is_non_blocking(tmp_path, monkeypatch):
    class BoomCaller:
        def __init__(self, *a, **k):
            pass

        def call(self, *a, **k):
            raise RuntimeError("network down")

    monkeypatch.setattr(charter_freshness, "LLMCaller", BoomCaller)
    flow = _make_flow(tmp_path)
    step = _make_step({"changes_made": {"files_changed": ["src/foo.py"]}})

    result = charter_freshness.charter_freshness_handler(step, flow)

    assert result is StepStatus.COMPLETED  # never blocks
    assert step.outputs["charter_update_needed"] is False
    assert step.outputs["skipped_reason"] == "llm_error"


def test_unparsable_response_is_non_blocking(tmp_path, monkeypatch):
    state = _install_fake_caller(monkeypatch, ["definitely not json"])
    flow = _make_flow(tmp_path)
    step = _make_step({"changes_made": {"files_changed": ["src/foo.py"]}})

    result = charter_freshness.charter_freshness_handler(step, flow)

    assert result is StepStatus.COMPLETED
    assert step.outputs["charter_update_needed"] is False
    assert step.outputs["skipped_reason"] == "parse_error"


# ---------------------------------------------------------------------------
# admission-check trigger (task 3)
# ---------------------------------------------------------------------------

def test_admission_check_not_run_when_charter_untouched(tmp_path, monkeypatch):
    resp = json.dumps({"charter_update_needed": False, "touched_classes": [],
                       "reason": "x", "suggested_update": ""})
    _install_fake_caller(monkeypatch, [resp])
    flow = _make_flow(tmp_path)
    step = _make_step({"changes_made": {"files_changed": ["src/foo.py"]}})

    charter_freshness.charter_freshness_handler(step, flow)

    assert "admission_checked" not in step.outputs


def test_admission_check_runs_and_warns_when_charter_oversized(tmp_path, monkeypatch):
    """When the diff edits se3/charter.md and the charter is over its monitoring
    threshold, the altitude-gate warning is surfaced (still non-blocking)."""
    # Write an oversized charter (> the 32 KiB default) so check_admission flags
    # over_threshold.
    big = "# Charter\n\n" + ("x" * 40000)
    _write_charter(tmp_path, big)

    resp = json.dumps({"charter_update_needed": True, "touched_classes": ["conventions"],
                       "reason": "charter edited", "suggested_update": "..."})
    _install_fake_caller(monkeypatch, [resp])
    flow = _make_flow(tmp_path)
    step = _make_step({"changes_made": {"files_changed": ["se3/charter.md", "src/foo.py"]}})

    result = charter_freshness.charter_freshness_handler(step, flow)

    assert result is StepStatus.COMPLETED  # admission is a monitoring light, not a wall
    assert step.outputs["admission_checked"] is True
    assert step.outputs["admission_over_threshold"] is True
    assert "monitoring threshold" in step.outputs["admission_warning"]


def test_admission_check_runs_no_warn_when_charter_small(tmp_path, monkeypatch):
    _write_charter(tmp_path, "# Charter\n\n## Purpose\nsmall and tidy.\n")
    resp = json.dumps({"charter_update_needed": True, "touched_classes": ["identity"],
                       "reason": "charter edited", "suggested_update": "..."})
    _install_fake_caller(monkeypatch, [resp])
    flow = _make_flow(tmp_path)
    step = _make_step({"changes_made": {"files_changed": ["se3/charter.md"]}})

    charter_freshness.charter_freshness_handler(step, flow)

    assert step.outputs["admission_checked"] is True
    assert step.outputs["admission_over_threshold"] is False
    assert "admission_warning" not in step.outputs


# ---------------------------------------------------------------------------
# admission gate prompt builder + check_admission in-memory candidate path (G2)
# ---------------------------------------------------------------------------

def test_admission_gate_prompt_includes_standard_and_removal_question():
    """The gate prompt embeds the full admission standard and the extra
    removal-weakening question that the plain standard does not carry."""
    prompt = charter.build_admission_gate_prompt(
        candidate_text="# Charter\n\n## Purpose\nnew descriptive line.\n",
        replaced_texts=[],
        diff_summary="Added a code-index subsystem.",
    )

    assert charter.CHARTER_ADMISSION_STANDARD in prompt
    assert charter.CHARTER_ADMISSION_GATE_REMOVAL_QUESTION in prompt
    # The candidate full text and the JSON verdict schema keys are present.
    assert "new descriptive line." in prompt
    assert "admitted" in prompt
    assert "violations" in prompt
    assert "weakened_removals" in prompt
    assert "Added a code-index subsystem." in prompt


def test_admission_gate_prompt_pure_insertion_states_no_removal():
    """Empty replaced_texts (a pure-insertion patch) yields a prompt that says
    no text is removed, so the removal question is trivially satisfied."""
    prompt = charter.build_admission_gate_prompt(
        candidate_text="# Charter\nbody\n",
        replaced_texts=None,
    )

    assert "PURE INSERTION" in prompt
    # No verbatim "replaced passage" block for an insert-only patch.
    assert "replaced passage #" not in prompt


def test_admission_gate_prompt_lists_each_replaced_text_verbatim():
    """Every replaced (removed) passage is rendered verbatim so the gate can
    judge each removal individually."""
    replaced = [
        "Old convention A that is being reworded.",
        "Stale statement B corrected by this change.",
    ]
    prompt = charter.build_admission_gate_prompt(
        candidate_text="# Charter\nrewritten body\n",
        replaced_texts=replaced,
        diff_summary="Reword two stale statements.",
    )

    for text in replaced:
        assert text in prompt
    assert "replaced passage #1" in prompt
    assert "replaced passage #2" in prompt


def test_admission_gate_prompt_filters_blank_replaced_entries():
    """Blank / non-string replaced entries are dropped; if none survive the
    prompt degrades to the pure-insertion wording."""
    prompt = charter.build_admission_gate_prompt(
        candidate_text="# Charter\nbody\n",
        replaced_texts=["   ", "", None, 123],
    )

    assert "PURE INSERTION" in prompt
    assert "replaced passage #" not in prompt


def test_check_admission_in_memory_candidate_under_threshold():
    """check_admission accepts an in-memory candidate string directly and reports
    over_threshold False for a small candidate (the byte check is a monitoring
    light, never blocking)."""
    candidate = "# Charter\n\n## Purpose\nsmall and tidy.\n"

    result = charter.check_admission(candidate)

    assert result.over_threshold is False
    assert result.warning is None
    assert result.size_bytes == len(candidate.encode("utf-8"))
    assert result.admission_standard is charter.CHARTER_ADMISSION_STANDARD


def test_check_admission_in_memory_candidate_over_threshold():
    """An over-threshold candidate sets over_threshold and a monitoring warning
    without raising or blocking."""
    candidate = "# Charter\n\n" + ("x" * 40000)

    result = charter.check_admission(candidate)

    assert result.over_threshold is True
    assert result.warning is not None
    assert "monitoring threshold" in result.warning
    assert result.size_bytes == len(candidate.encode("utf-8"))


def test_check_admission_respects_custom_threshold_for_candidate():
    """A tiny custom threshold flips a normally-small candidate to over_threshold,
    confirming the in-memory path honours the threshold argument."""
    candidate = "# Charter\nbody\n"

    result = charter.check_admission(candidate, threshold_bytes=4)

    assert result.over_threshold is True
    assert result.threshold_bytes == 4
