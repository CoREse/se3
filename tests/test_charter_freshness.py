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
from se3.engine.steps import summarize
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
    state = {"prompts": [], "responses": list(responses), "calls": 0, "init_kwargs": None}

    class FakeCaller:
        def __init__(self, *args, **kwargs):
            state["init_kwargs"] = kwargs

        def call(self, prompt, **kwargs):
            state["calls"] += 1
            state["prompts"].append(prompt)
            if not state["responses"]:
                raise AssertionError("unexpected extra LLM call")
            return state["responses"].pop(0)

    monkeypatch.setattr(charter_freshness, "LLMCaller", FakeCaller)
    return state


def _with_completed_invariant_check(flow: FlowInstance) -> FlowInstance:
    """Attach a COMPLETED invariant_check step so the closed loop's precondition
    is satisfied."""
    inv = Step(step_type=StepType.INVARIANT_CHECK, status=StepStatus.COMPLETED)
    flow.state.add_step(inv)
    return flow


def _propose(update: bool, patch, *, touched=None, suggested="do it"):
    return json.dumps({
        "charter_update_needed": update,
        "touched_classes": touched or (["conventions"] if update else []),
        "reason": "r",
        "suggested_update": suggested if update else "",
        "patch": patch,
    })


def _gate(admitted: bool, *, violations=None, weakened=None):
    return json.dumps({
        "admitted": admitted,
        "violations": violations or [],
        "weakened_removals": weakened or [],
    })


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


# ---------------------------------------------------------------------------
# propose -> gate -> apply closed loop (G3)
# ---------------------------------------------------------------------------

_DISK_CHARTER = (
    "# Charter\n\n"
    "## Purpose\n"
    "The alpha subsystem drives the widget loop.\n\n"
    "## Conventions\n"
    "- Log via logging.\n"
)

_REPLACE_PATCH = [{
    "op": "replace",
    "old_text": "The alpha subsystem drives the widget loop.",
    "new_text": "The beta subsystem drives the widget loop.",
}]


def test_precondition_missing_stays_advisory_and_never_writes(tmp_path, monkeypatch):
    """Update suggested but no COMPLETED invariant_check -> advisory only; the
    charter on disk is byte-for-byte unchanged and only ONE (propose) LLM call
    is made (the gate never runs)."""
    _write_charter(tmp_path, _DISK_CHARTER)
    state = _install_fake_caller(monkeypatch, [_propose(True, _REPLACE_PATCH)])
    flow = _make_flow(tmp_path)  # no invariant_check step
    step = _make_step({"changes_made": {"files_changed": ["src/foo.py"]}})

    result = charter_freshness.charter_freshness_handler(step, flow)

    assert result is StepStatus.COMPLETED
    assert state["calls"] == 1  # propose only, no gate
    assert step.outputs["charter_auto_updated"] is False
    assert step.outputs["degraded_reason"] == "invariant_check_not_completed"
    assert step.outputs["suggested_update"] == "do it"
    # Disk unchanged.
    assert (tmp_path / "se3" / "charter.md").read_text(encoding="utf-8") == _DISK_CHARTER


def test_gate_pass_writes_charter_atomically_with_diff(tmp_path, monkeypatch):
    _write_charter(tmp_path, _DISK_CHARTER)
    state = _install_fake_caller(monkeypatch, [
        _propose(True, _REPLACE_PATCH),
        _gate(True),
    ])
    flow = _with_completed_invariant_check(_make_flow(tmp_path))
    step = _make_step({"changes_made": {"files_changed": ["src/foo.py"]}})

    result = charter_freshness.charter_freshness_handler(step, flow)

    assert result is StepStatus.COMPLETED
    assert state["calls"] == 2  # propose + gate
    assert step.outputs["charter_auto_updated"] is True
    assert "beta subsystem" in step.outputs["charter_diff"]
    assert step.outputs["gate_verdicts"]["mechanical_ok"] is True
    assert step.outputs["gate_verdicts"]["llm_admitted"] is True
    # LLM sub-calls stayed read-only.
    assert state["init_kwargs"].get("force_read_only") is True
    # Disk actually updated.
    on_disk = (tmp_path / "se3" / "charter.md").read_text(encoding="utf-8")
    assert "beta subsystem" in on_disk
    assert "alpha subsystem" not in on_disk


def test_gate_reject_twice_degrades_without_writing(tmp_path, monkeypatch):
    _write_charter(tmp_path, _DISK_CHARTER)
    state = _install_fake_caller(monkeypatch, [
        _propose(True, _REPLACE_PATCH),
        _gate(False, violations=["over-legislated"]),
        _propose(True, _REPLACE_PATCH),
        _gate(False, violations=["still bad"]),
    ])
    flow = _with_completed_invariant_check(_make_flow(tmp_path))
    step = _make_step({"changes_made": {"files_changed": ["src/foo.py"]}})

    result = charter_freshness.charter_freshness_handler(step, flow)

    assert result is StepStatus.COMPLETED
    assert state["calls"] == 4  # two full propose+gate rounds
    assert step.outputs["charter_auto_updated"] is False
    assert "degraded_reason" in step.outputs
    assert step.outputs["gate_verdicts"]["llm_admitted"] is False
    # Prefer-stale-over-degraded: disk is byte-for-byte unchanged.
    assert (tmp_path / "se3" / "charter.md").read_text(encoding="utf-8") == _DISK_CHARTER


def test_gate_malformed_response_fails_closed_without_writing(tmp_path, monkeypatch):
    """A gate response with string-typed fields is NOT coerced into a pass.

    ``{"admitted": "false", "weakened_removals": "drops a rule"}`` must fail the
    gate (fail-closed) rather than being smoothed to ``admitted=True`` with empty
    findings — the propose->gate->apply contract requires a proven pass before any
    write, so the charter stays byte-for-byte unchanged.
    """
    _write_charter(tmp_path, _DISK_CHARTER)
    malformed = json.dumps({
        "admitted": "false",              # string, not bool
        "weakened_removals": "drops a rule",  # string, not list
    })
    state = _install_fake_caller(monkeypatch, [
        _propose(True, _REPLACE_PATCH),
        malformed,
        _propose(True, _REPLACE_PATCH),
        malformed,  # retry also malformed -> degrade
    ])
    flow = _with_completed_invariant_check(_make_flow(tmp_path))
    step = _make_step({"changes_made": {"files_changed": ["src/foo.py"]}})

    result = charter_freshness.charter_freshness_handler(step, flow)

    assert result is StepStatus.COMPLETED
    assert step.outputs["charter_auto_updated"] is False
    assert "degraded_reason" in step.outputs
    assert step.outputs["gate_verdicts"].get("llm_malformed")
    # Prefer-stale-over-degraded: disk is byte-for-byte unchanged.
    assert (tmp_path / "se3" / "charter.md").read_text(encoding="utf-8") == _DISK_CHARTER


def test_gate_reject_then_retry_succeeds(tmp_path, monkeypatch):
    """A first-round gate rejection feeds back; the corrected second-round patch
    passes and is applied."""
    _write_charter(tmp_path, _DISK_CHARTER)
    state = _install_fake_caller(monkeypatch, [
        _propose(True, _REPLACE_PATCH),
        _gate(False, violations=["reword please"]),
        _propose(True, _REPLACE_PATCH),
        _gate(True),
    ])
    flow = _with_completed_invariant_check(_make_flow(tmp_path))
    step = _make_step({"changes_made": {"files_changed": ["src/foo.py"]}})

    result = charter_freshness.charter_freshness_handler(step, flow)

    assert result is StepStatus.COMPLETED
    assert state["calls"] == 4
    assert step.outputs["charter_auto_updated"] is True
    # Second-round propose prompt carried the rejection feedback.
    assert "REJECTED" in state["prompts"][2]
    assert "beta subsystem" in (tmp_path / "se3" / "charter.md").read_text(encoding="utf-8")


def test_mechanical_reject_then_retry_succeeds_without_extra_gate_call(tmp_path, monkeypatch):
    """A patch quoting text not on disk fails the mechanical check with NO LLM
    gate call; the retried valid patch is applied."""
    _write_charter(tmp_path, _DISK_CHARTER)
    bad_patch = [{"op": "replace", "old_text": "TEXT NOT ON DISK", "new_text": "x"}]
    state = _install_fake_caller(monkeypatch, [
        _propose(True, bad_patch),   # mechanical fail -> no gate LLM call
        _propose(True, _REPLACE_PATCH),
        _gate(True),
    ])
    flow = _with_completed_invariant_check(_make_flow(tmp_path))
    step = _make_step({"changes_made": {"files_changed": ["src/foo.py"]}})

    result = charter_freshness.charter_freshness_handler(step, flow)

    assert result is StepStatus.COMPLETED
    assert state["calls"] == 3  # propose(bad), propose(good), gate
    assert step.outputs["charter_auto_updated"] is True


def test_size_red_light_is_gating_and_blocks_write(tmp_path, monkeypatch):
    """On this path the admission size check is GATING: a candidate over the
    threshold is not written (no LLM gate call needed)."""
    # A single insert over MAX_PATCH_NEW_CHARS would fail mechanically first, so
    # push the CANDIDATE over the 32 KiB size threshold by padding the base and
    # adding a within-budget insertion — the size gate must still block it.
    _write_charter(tmp_path, _DISK_CHARTER + ("q" * 30000))
    small_patch = [{"op": "insert_after", "anchor": "- Log via logging.\n",
                    "new_text": "y" * 5000}]
    state = _install_fake_caller(monkeypatch, [
        _propose(True, small_patch),
        _propose(True, small_patch),  # retry also blocked by size
    ])
    flow = _with_completed_invariant_check(_make_flow(tmp_path))
    step = _make_step({"changes_made": {"files_changed": ["src/foo.py"]}})
    before = (tmp_path / "se3" / "charter.md").read_text(encoding="utf-8")

    result = charter_freshness.charter_freshness_handler(step, flow)

    assert result is StepStatus.COMPLETED
    assert step.outputs["charter_auto_updated"] is False
    assert step.outputs["gate_verdicts"]["size_over_threshold"] is True
    # No LLM gate call was made (size gate short-circuits before it).
    assert state["calls"] == 2  # two propose calls only
    assert (tmp_path / "se3" / "charter.md").read_text(encoding="utf-8") == before


def test_resume_reentry_is_idempotent(tmp_path, monkeypatch):
    """On resume the charter is already updated; propose judges fresh against the
    on-disk text and nothing is written."""
    updated = _DISK_CHARTER.replace("alpha subsystem", "beta subsystem")
    _write_charter(tmp_path, updated)
    state = _install_fake_caller(monkeypatch, [_propose(False, [])])
    flow = _with_completed_invariant_check(_make_flow(tmp_path))
    step = _make_step({"changes_made": {"files_changed": ["src/foo.py"]}})

    result = charter_freshness.charter_freshness_handler(step, flow)

    assert result is StepStatus.COMPLETED
    assert state["calls"] == 1  # propose only; fresh -> no gate
    assert step.outputs["charter_auto_updated"] is False
    assert (tmp_path / "se3" / "charter.md").read_text(encoding="utf-8") == updated


def test_propose_prompt_uses_on_disk_charter_not_frozen_anchor(tmp_path, monkeypatch):
    """The propose base is the on-disk charter, never the frozen invariant anchor
    passed via step.inputs['charter']."""
    _write_charter(tmp_path, _DISK_CHARTER)
    state = _install_fake_caller(monkeypatch, [_propose(False, [])])
    flow = _with_completed_invariant_check(_make_flow(tmp_path))
    step = _make_step({
        "changes_made": {"files_changed": ["src/foo.py"]},
        "charter": "FROZEN ANCHOR TEXT — must not be the patch base",
    })

    charter_freshness.charter_freshness_handler(step, flow)

    prompt = state["prompts"][0]
    assert "alpha subsystem drives the widget loop." in prompt
    assert "FROZEN ANCHOR TEXT" not in prompt


def test_closed_loop_llm_failure_is_non_blocking(tmp_path, monkeypatch):
    """Even with the precondition satisfied, a propose LLM failure degrades to a
    soft no-op (still COMPLETED, charter untouched)."""
    _write_charter(tmp_path, _DISK_CHARTER)

    class BoomCaller:
        def __init__(self, *a, **k):
            pass

        def call(self, *a, **k):
            raise RuntimeError("network down")

    monkeypatch.setattr(charter_freshness, "LLMCaller", BoomCaller)
    flow = _with_completed_invariant_check(_make_flow(tmp_path))
    step = _make_step({"changes_made": {"files_changed": ["src/foo.py"]}})

    result = charter_freshness.charter_freshness_handler(step, flow)

    assert result is StepStatus.COMPLETED
    assert step.outputs["charter_auto_updated"] is False
    assert step.outputs["skipped_reason"] == "llm_error"
    assert (tmp_path / "se3" / "charter.md").read_text(encoding="utf-8") == _DISK_CHARTER


# ---------------------------------------------------------------------------
# summarize aggregation of the knowledge-guard results (G5)
# ---------------------------------------------------------------------------
# summarize is the sole flow-level consumer of the charter_freshness auto-update
# / advisory and the invariant_check why-comment losses; without it those results
# are stranded in a step detail view nobody reopens. These tests exercise the
# three shapes through the deterministic aggregation helpers (no LLM needed).

def _flow_with_guard_steps(
    tmp_path: Path,
    *,
    charter_outputs=None,
    invariant_outputs=None,
) -> FlowInstance:
    flow = _make_flow(tmp_path)
    if charter_outputs is not None:
        flow.state.add_step(Step(
            step_type=StepType.CHARTER_FRESHNESS,
            status=StepStatus.COMPLETED,
            outputs=dict(charter_outputs),
        ))
    if invariant_outputs is not None:
        flow.state.add_step(Step(
            step_type=StepType.INVARIANT_CHECK,
            status=StepStatus.COMPLETED,
            outputs=dict(invariant_outputs),
        ))
    return flow


def test_summarize_aggregates_charter_auto_update(tmp_path):
    flow = _flow_with_guard_steps(tmp_path, charter_outputs={
        "charter_update_needed": True,
        "charter_auto_updated": True,
        "touched_classes": ["conventions"],
        "charter_diff": "--- se3/charter.md (old)\n+++ se3/charter.md (new)\n-old\n+new",
    })

    section = summarize._format_knowledge_guards(flow)
    assert "Charter Auto-Update" in section
    assert "+new" in section  # diff excerpt is embedded

    # The deterministic fallback report also carries it.
    text = summarize._create_basic_summary_text(flow, {}, {}, "task")
    assert "Charter Auto-Update" in text
    assert "+new" in text


def test_summarize_aggregates_charter_advisory_and_reason(tmp_path):
    flow = _flow_with_guard_steps(tmp_path, charter_outputs={
        "charter_update_needed": True,
        "charter_auto_updated": False,
        "suggested_update": "Record the new runner adapter in the architecture section.",
        "degraded_reason": "invariant_check_not_completed",
    })

    section = summarize._format_knowledge_guards(flow)
    assert "Charter Update Advisory" in section
    assert "Record the new runner adapter" in section
    assert "invariant_check_not_completed" in section


def test_summarize_aggregates_why_comment_losses(tmp_path):
    flow = _flow_with_guard_steps(
        tmp_path,
        invariant_outputs={
            "why_comment_losses": [
                {"file": "src/foo.py", "comment": "# guards the retry window",
                 "why_it_matters": "the constraint is now undocumented"},
            ],
        },
    )

    section = summarize._format_knowledge_guards(flow)
    assert "Why-Comment Losses" in section
    assert "guards the retry window" in section
    assert "src/foo.py" in section


def test_summarize_no_guard_activity_adds_nothing(tmp_path):
    # charter fresh, no why losses -> empty section, base report unchanged.
    flow = _flow_with_guard_steps(
        tmp_path,
        charter_outputs={
            "charter_update_needed": False,
            "charter_auto_updated": False,
        },
        invariant_outputs={"why_comment_losses": []},
    )

    assert summarize._format_knowledge_guards(flow) == ""
    text = summarize._create_basic_summary_text(flow, {}, {}, "task")
    assert "Knowledge Guards" not in text
