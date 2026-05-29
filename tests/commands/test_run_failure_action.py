"""Tests for the retry_decision mutual-exclusion between CLI TTY and webui.

Two surfaces are exercised:

* ``_resolve_step_failure_action`` — on an interactive terminal it now probes
  for an existing sibling response left by the webui at
  ``retry_decision_{step_id}.json``. When one is present the function adopts
  the decision and cleans up the artifacts so the webui chip disappears too;
  when none is present it falls through to the CLI prompt without writing a
  new call. The non-interactive (daemon-spawn / CI / pipe) path is
  unchanged.

* The post-prompt cleanup at the failure-handling call site — exposed via the
  ``_cleanup_retry_decision_artifacts`` + ``_retry_decision_call_path``
  helpers — wipes the retry_decision call + sibling responses after the CLI
  has answered, so the webui chip vanishes even when the webui wrote its
  answer *during* the CLI prompt.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from se3.daemon import protocol
from se3.engine import interaction_calls


def _stub_flow(flow_id: str = "flow-1") -> SimpleNamespace:
    return SimpleNamespace(flow_id=flow_id)


def _stub_step(
    step_id: str = "step-1",
    step_type: str = "implement",
    retry_count: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        step_id=step_id,
        step_type=SimpleNamespace(value=step_type),
        retry_count=retry_count,
    )


# --------------------------------------------------------------------------
# _resolve_step_failure_action: four quadrants
# --------------------------------------------------------------------------


def test_interactive_with_existing_response_returns_decision_and_cleans_up(
    tmp_path: Path,
) -> None:
    """Interactive + webui-answered ⇒ adopt the decision; artifacts vanish."""
    from se3.commands import run

    flow = _stub_flow()
    step = _stub_step(step_id="step-7")

    # Pre-seed: webui answered earlier via the deterministic call file.
    call_path = interaction_calls.write_retry_decision_call(
        tmp_path,
        flow_id=flow.flow_id,
        step_id=step.step_id,
        step_type=step.step_type.value,
        error="kaboom",
        retry_count=step.retry_count,
    )
    interaction_calls.write_response(call_path, {"decision": "skip"})
    response_sibling = call_path.with_name(call_path.stem + ".response")
    assert response_sibling.exists()

    action, info = run._resolve_step_failure_action(
        tmp_path, flow, step, "kaboom", interactive=True
    )

    assert action == "decision"
    assert info == "skip"
    # Call file + both sibling response variants are gone.
    assert not call_path.exists()
    assert not response_sibling.exists()
    assert not call_path.with_name(call_path.stem + ".response.json").exists()


def test_interactive_without_response_returns_race_and_writes_call(
    tmp_path: Path,
) -> None:
    """Interactive + no webui answer ⇒ race the CLI prompt vs. the webui, and
    a retry_decision call file IS written so the web console shows a chip."""
    from se3.commands import run
    from se3.daemon import protocol

    flow = _stub_flow()
    step = _stub_step(step_id="step-3")

    action, info = run._resolve_step_failure_action(
        tmp_path, flow, step, "boom", interactive=True
    )

    assert action == "race"
    call_path = Path(info)
    # The dual-channel pause writes a retry_decision call (the webui chip).
    assert call_path.exists()
    data = interaction_calls.read_call(call_path)
    assert data is not None
    assert data["kind"] == protocol.CALL_KIND_RETRY_DECISION
    # No answer was consumed, so no sibling response exists.
    assert not call_path.with_name(call_path.stem + ".response").exists()


def test_non_interactive_with_existing_response_unchanged(tmp_path: Path) -> None:
    """Non-interactive + answered ⇒ same behavior as before this change."""
    from se3.commands import run

    flow = _stub_flow()
    step = _stub_step(step_id="step-9")

    # Drive once with no answer to materialise the call file.
    action, info = run._resolve_step_failure_action(
        tmp_path, flow, step, "kaboom", interactive=False
    )
    assert action == "pause"
    call_path = Path(info)
    # Webui answers; second pass adopts the decision and cleans up.
    interaction_calls.write_response(call_path, {"decision": "retry"})
    action, decision = run._resolve_step_failure_action(
        tmp_path, flow, step, "kaboom", interactive=False
    )
    assert action == "decision"
    assert decision == "retry"
    assert not call_path.exists()


def test_non_interactive_without_response_pauses(tmp_path: Path) -> None:
    """Non-interactive + no answer ⇒ writes call file, returns pause."""
    from se3.commands import run

    flow = _stub_flow()
    step = _stub_step(step_id="step-4")

    action, info = run._resolve_step_failure_action(
        tmp_path, flow, step, "kaboom", interactive=False
    )
    assert action == "pause"
    call_path = Path(info)
    assert call_path.exists()
    data = interaction_calls.read_call(call_path)
    assert data is not None
    assert data["kind"] == protocol.CALL_KIND_RETRY_DECISION


def test_interactive_unrecognized_decision_defaults_to_abort(tmp_path: Path) -> None:
    """A garbled webui response is taken as abort but still consumed."""
    from se3.commands import run

    flow = _stub_flow()
    step = _stub_step(step_id="step-5")

    # Hand-write a deterministic call file with a bogus decision payload.
    call_path = interaction_calls.write_retry_decision_call(
        tmp_path,
        flow_id=flow.flow_id,
        step_id=step.step_id,
        step_type=step.step_type.value,
        error="boom",
        retry_count=step.retry_count,
    )
    interaction_calls.write_response(call_path, {"decision": "nope-not-a-thing"})

    action, info = run._resolve_step_failure_action(
        tmp_path, flow, step, "boom", interactive=True
    )
    assert action == "decision"
    assert info == "abort"
    assert not call_path.exists()


# --------------------------------------------------------------------------
# Post-CLI-prompt cleanup helpers
# --------------------------------------------------------------------------


def test_cleanup_helper_removes_all_three_artifacts(tmp_path: Path) -> None:
    """After CLI prompt answers, the call file + both sibling responses go."""
    from se3.commands import run

    step_id = "step-cleanup"
    call_path = interaction_calls.write_retry_decision_call(
        tmp_path,
        flow_id="flow-1",
        step_id=step_id,
        step_type="implement",
        error="boom",
        retry_count=0,
    )
    # Write *both* sibling response variants — daemon writes
    # ``.response.json``; the engine writes ``.response``.
    interaction_calls.write_response(call_path, {"decision": "retry"})
    (call_path.with_name(call_path.stem + ".response.json")).write_text(
        json.dumps({"decision": "retry"}), encoding="utf-8"
    )
    assert call_path.exists()
    assert call_path.with_name(call_path.stem + ".response").exists()
    assert call_path.with_name(call_path.stem + ".response.json").exists()

    run._cleanup_retry_decision_artifacts(
        run._retry_decision_call_path(tmp_path, step_id)
    )

    assert not call_path.exists()
    assert not call_path.with_name(call_path.stem + ".response").exists()
    assert not call_path.with_name(call_path.stem + ".response.json").exists()


def test_cleanup_helper_is_noop_when_nothing_exists(tmp_path: Path) -> None:
    """Cleanup never raises when there is nothing to remove."""
    from se3.commands import run

    # No calls dir, no files — must be a quiet no-op.
    run._cleanup_retry_decision_artifacts(
        run._retry_decision_call_path(tmp_path, "ghost-step")
    )
    # Idempotent re-run too.
    run._cleanup_retry_decision_artifacts(
        run._retry_decision_call_path(tmp_path, "ghost-step")
    )


def test_cleanup_targets_only_deterministic_retry_decision_file(
    tmp_path: Path,
) -> None:
    """Cleanup targets the retry_decision-kind deterministic filename only.

    Other kinds of call files in the same ``se3/calls/`` directory (a plain
    ``call`` from MCP, a ``cli_confirm``, ...) must survive the cleanup.
    """
    from se3.commands import run

    step_id = "step-iso"
    rd_path = interaction_calls.write_retry_decision_call(
        tmp_path,
        flow_id="flow-1",
        step_id=step_id,
        step_type="implement",
        error="boom",
        retry_count=0,
    )
    # A bystander confirm-style call for the same step.
    other = interaction_calls.write_call(
        interaction_calls.calls_dir_for(tmp_path),
        kind=protocol.CALL_KIND_CALL,
        call_id="confirm_other",
        prompt="bystander",
        context={"flow_id": "flow-1", "step_id": step_id},
    )

    run._cleanup_retry_decision_artifacts(
        run._retry_decision_call_path(tmp_path, step_id)
    )

    assert not rd_path.exists()
    assert other.exists()
