"""Dual-channel failure-decision tests for ``se3 run``.

These cover the unified failure-decision pause introduced for the
"CLI + webui bystander" scenario:

* On a step failure (with retries remaining) the orchestrator
  *unconditionally* writes a ``retry_decision`` call file (so the web console
  shows a Retry/Skip/Abort chip) **and** emits a ``FLOW_PAUSED`` event — even
  on an interactive terminal.
* The CLI choice prompt and the webui response poller form a
  *whoever-first-wins* race (``_await_terminal_or_web_choice``): the loser is
  torn down (poller cancelled, call/response artifacts cleaned) and the answer
  is never consumed twice.
* The non-interactive pause/decision behavior and the max-retries auto-fail
  path are regression-covered.

The integration tests drive ``run_flow`` with a mocked StateMachine /
PersistenceManager (the same harness ``tests/commands/test_run.py`` uses) and
read the NDJSON event stream from ``--output-format json`` to assert the
emitted lifecycle events. The race-helper tests exercise
``_await_terminal_or_web_choice`` directly with ``sys.stdin.isatty`` forced
off, so they never depend on a real TTY or ``prompt_toolkit``.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from se3.commands import run
from se3.commands.run import run_flow
from se3.daemon import protocol
from se3.engine import interaction_calls
from se3.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)
from se3.engine.persistence import PersistenceManager


# ---------------------------------------------------------------------------
# Integration harness — drive run_flow with a single FAILED implement step
# ---------------------------------------------------------------------------


def _build_failed_flow(project_root: Path, retry_count: int = 0) -> FlowInstance:
    (project_root / "se3" / "state").mkdir(parents=True, exist_ok=True)
    flow = FlowInstance(
        flow_id="dc-flow-001",
        task_description="dual-channel task",
        task_type="feature",
        status=FlowStatus.RUNNING,
    )
    flow.state.selected_steps = [StepType.IMPLEMENT]
    flow.state.current_step_index = 0
    # PENDING (not RUNNING/FAILED) so the resume preparation does not reset the
    # step model's retry_count — the orchestrator runs the step from the loop
    # and the failure handler sees the retry_count we set here.
    step = Step(
        step_type=StepType.IMPLEMENT,
        status=StepStatus.PENDING,
        step_id="implement-dc",
        inputs={},
        outputs={},
    )
    step.error_message = "kaboom"
    step.retry_count = retry_count
    flow.state.add_step(step)
    flow.state.current_step_id = "implement-dc"
    return flow


def _run_failed_flow(
    project_root: Path,
    flow: FlowInstance,
    *,
    run_step_results=None,
):
    """Run the flow with mocked SM/PM and a non-interactive stdin.

    Returns ``(exit_code, ndjson_event_types)``.
    """
    captured = {}

    with patch("se3.commands.run.PersistenceManager") as mock_pm_class, patch(
        "se3.commands.run.StateMachine"
    ) as mock_sm_class, patch("se3.commands.run.STEP_HANDLERS", {}), patch(
        "se3.commands.run._stdin_is_interactive", return_value=False
    ), patch(
        "se3.commands.run.render_full"
    ):
        mock_pm = MagicMock()
        mock_pm_class.return_value = mock_pm
        mock_pm.load_flow.return_value = flow
        mock_pm.load_flow_by_id.return_value = flow
        mock_pm._peek_active_flow_id.return_value = flow.flow_id

        mock_sm = MagicMock()
        mock_sm_class.return_value = mock_sm
        if run_step_results is None:
            mock_sm.run_step.return_value = StepStatus.FAILED
        else:
            mock_sm.run_step.side_effect = list(run_step_results)
        mock_sm.transition_to_next.side_effect = (
            lambda f: setattr(f, "status", FlowStatus.COMPLETED)
        )

        import io
        import contextlib

        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exit_code = run_flow(
                project_root=project_root,
                flow_id=flow.flow_id,
                output_format="json",
            )
        captured["out"] = buf.getvalue()

    events = []
    for line in captured["out"].splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "type" in obj:
            events.append(obj["type"])
    return exit_code, events


def _calls_dir(project_root: Path) -> Path:
    return interaction_calls.calls_dir_for(project_root)


def test_failure_writes_retry_decision_call_and_emits_flow_paused(
    tmp_path: Path,
) -> None:
    """A failed step (retries remaining) writes a retry_decision call AND
    emits FLOW_PAUSED, so a webui bystander sees the failure + decision chip."""
    flow = _build_failed_flow(tmp_path)
    exit_code, events = _run_failed_flow(tmp_path, flow)

    # No answer on disk → the non-interactive channel pauses (exit 0).
    assert exit_code == 0
    assert flow.status == FlowStatus.PAUSED

    # The retry_decision call file exists (the webui chip).
    call_path = run._retry_decision_call_path(tmp_path, "implement-dc")
    assert call_path.exists()
    data = interaction_calls.read_call(call_path)
    assert data is not None
    assert data["kind"] == protocol.CALL_KIND_RETRY_DECISION

    # Both the per-step failure event and the failure pause event are emitted.
    assert "step_failed" in events
    assert "flow_paused" in events


def test_webui_answer_skip_completes_and_transitions(tmp_path: Path) -> None:
    """A pre-seeded webui 'skip' answer is consumed: the step is force-completed
    and the flow advances (regression for the decision channel)."""
    flow = _build_failed_flow(tmp_path)
    call_path = interaction_calls.write_retry_decision_call(
        tmp_path,
        flow_id=flow.flow_id,
        step_id="implement-dc",
        step_type="implement",
        error="kaboom",
    )
    interaction_calls.write_response(call_path, {"decision": "skip"})

    exit_code, events = _run_failed_flow(tmp_path, flow)

    assert exit_code == 0
    assert flow.status == FlowStatus.COMPLETED
    # The answered decision is consumed → artifacts cleaned up.
    assert not call_path.exists()
    assert not call_path.with_name(call_path.stem + ".response").exists()


def test_webui_answer_abort_fails_flow(tmp_path: Path) -> None:
    """A pre-seeded webui 'abort' answer fails the flow with exit code 1."""
    flow = _build_failed_flow(tmp_path)
    call_path = interaction_calls.write_retry_decision_call(
        tmp_path,
        flow_id=flow.flow_id,
        step_id="implement-dc",
        step_type="implement",
        error="kaboom",
    )
    interaction_calls.write_response(call_path, {"decision": "abort"})

    exit_code, _events = _run_failed_flow(tmp_path, flow)

    assert exit_code == 1
    assert flow.status == FlowStatus.FAILED
    assert not call_path.exists()


def test_webui_answer_retry_resets_step_and_increments_counter(
    tmp_path: Path,
) -> None:
    """A pre-seeded webui 'retry' answer resets the step to PENDING, bumps the
    retry counter, and re-runs (here the re-run succeeds)."""
    flow = _build_failed_flow(tmp_path)
    step = flow.state.get_current_step()
    call_path = interaction_calls.write_retry_decision_call(
        tmp_path,
        flow_id=flow.flow_id,
        step_id="implement-dc",
        step_type="implement",
        error="kaboom",
    )
    interaction_calls.write_response(call_path, {"decision": "retry"})

    # First run fails (consume retry), second run succeeds → flow completes.
    exit_code, _events = _run_failed_flow(
        tmp_path,
        flow,
        run_step_results=[StepStatus.FAILED, StepStatus.COMPLETED],
    )

    assert exit_code == 0
    assert flow.status == FlowStatus.COMPLETED
    assert step.retry_count == 1
    # The consumed answer is cleaned up so it can never be re-applied.
    assert not call_path.exists()


def test_max_retries_autofails_without_call_or_pause(tmp_path: Path) -> None:
    """retry_count >= 3 auto-fails: no retry_decision call, no FLOW_PAUSED,
    no race — unchanged from the historical behavior."""
    flow = _build_failed_flow(tmp_path, retry_count=3)
    exit_code, events = _run_failed_flow(tmp_path, flow)

    assert exit_code == 1
    assert flow.status == FlowStatus.FAILED
    # No decision artifact is written on the max-retries path.
    call_path = run._retry_decision_call_path(tmp_path, "implement-dc")
    assert not call_path.exists()
    assert "flow_paused" not in events


# ---------------------------------------------------------------------------
# whoever-first-wins race helper — _await_terminal_or_web_choice
# ---------------------------------------------------------------------------


def _seed_call(project_root: Path, step_id: str = "step-race") -> Path:
    return interaction_calls.write_retry_decision_call(
        project_root,
        flow_id="flow-race",
        step_id=step_id,
        step_type="implement",
        error="boom",
    )


_OPTIONS = ["Retry this step", "Skip to next step", "Abort flow"]


def test_race_web_answer_already_on_disk_wins_and_is_cleaned(
    tmp_path: Path,
) -> None:
    """A web response already on disk wins outright, is consumed, and the
    artifacts are removed so it can never be read twice."""
    call_path = _seed_call(tmp_path)
    interaction_calls.write_response(call_path, {"decision": "retry"})

    source, choice = run._await_terminal_or_web_choice(
        call_path, message="What would you like to do?", options=_OPTIONS
    )

    assert source == run._FAILURE_SRC_WEB
    assert choice == 0  # retry
    # Loser/winner artifacts are gone — no double consume possible.
    assert not call_path.exists()
    assert not call_path.with_name(call_path.stem + ".response").exists()
    assert not call_path.with_name(call_path.stem + ".response.json").exists()


def test_race_cli_first_wins_tears_down_webui_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the CLI answers first (no web response), the poller is torn down
    and any concurrent webui call/response is best-effort cleaned."""
    call_path = _seed_call(tmp_path)
    assert call_path.exists()

    # Force the non-TTY plain branch (deterministic, no prompt_toolkit) and
    # make the CLI choice return "Skip" (index 1).
    monkeypatch.setattr(run.sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(run, "prompt_user_choice", lambda *a, **k: 1)

    source, choice = run._await_terminal_or_web_choice(
        call_path, message="What would you like to do?", options=_OPTIONS
    )

    assert source == run._FAILURE_SRC_TERMINAL
    assert choice == 1  # skip
    # The CLI committed → the webui chip is torn down.
    assert not call_path.exists()


def test_race_web_answer_landing_during_cli_read_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a web answer lands while the CLI prompt is blocking, the web answer
    still wins (re-checked after the read)."""
    call_path = _seed_call(tmp_path)

    monkeypatch.setattr(run.sys.stdin, "isatty", lambda: False)

    def _answer_then_choose(*_a, **_k):
        # Simulate the webui answering during the (blocking) CLI read.
        interaction_calls.write_response(call_path, {"decision": "abort"})
        return 0  # CLI would have said "retry", but web wins

    monkeypatch.setattr(run, "prompt_user_choice", _answer_then_choose)

    source, choice = run._await_terminal_or_web_choice(
        call_path, message="What would you like to do?", options=_OPTIONS
    )

    assert source == run._FAILURE_SRC_WEB
    assert choice == 2  # abort (from the web answer, not the CLI's 0)
    assert not call_path.exists()


def test_race_no_call_file_degrades_to_plain_cli_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no call_file there is no web channel, so it degrades to a plain
    prompt_user_choice (equivalent to the pre-dual-channel path)."""
    monkeypatch.setattr(run, "prompt_user_choice", lambda *a, **k: 2)

    source, choice = run._await_terminal_or_web_choice(
        None, message="What would you like to do?", options=_OPTIONS
    )

    assert source == run._FAILURE_SRC_TERMINAL
    assert choice == 2


def test_failure_decision_to_choice_normalizes_unknown_to_abort() -> None:
    """retry/skip/abort map to 0/1/2; anything else (or None) → abort (2)."""
    assert run._failure_decision_to_choice("retry") == 0
    assert run._failure_decision_to_choice("skip") == 1
    assert run._failure_decision_to_choice("abort") == 2
    assert run._failure_decision_to_choice("garbage") == 2
    assert run._failure_decision_to_choice(None) == 2


# ---------------------------------------------------------------------------
# Interactive race coroutine — _await_terminal_or_web_choice_interactive
#
# The plain-branch tests above force ``sys.stdin.isatty()`` off and never reach
# the genuinely concurrent code (the background poller, the
# ``app.exit(web_sentinel)`` cross-thread cancellation, the cancel-cleanup).
# These tests drive the interactive coroutine directly with a fake
# ``PromptSession`` so the live PromptSession-vs-poller race, its teardown, and
# the "loser leaves no residual call/response / no double-consume" guarantees
# are actually exercised — without depending on a real TTY.
# ---------------------------------------------------------------------------


class _FakePromptApp:
    """Stand-in for ``PromptSession.app`` supporting the cancel handshake."""

    def __init__(self) -> None:
        self.is_running = False
        self._fut = None  # asyncio.Future, set while prompt_async blocks

    def exit(self, result=None) -> None:
        self.is_running = False
        if self._fut is not None and not self._fut.done():
            self._fut.set_result(result)


class _FakePromptSession:
    """Fake ``PromptSession`` whose ``prompt_async`` we control by mode.

    * ``terminal`` — returns ``value`` immediately (operator typed a choice).
    * ``cancel``   — raises ``KeyboardInterrupt`` (Ctrl+C / EOF at the prompt).
    * ``block``    — blocks on a future until ``app.exit(...)`` resolves it
      (so the background poller can cancel it when a web answer lands).
    """

    def __init__(self, *, mode: str, value=None, **_kw) -> None:
        self.app = _FakePromptApp()
        self._mode = mode
        self._value = value

    async def prompt_async(self):
        import asyncio

        if self._mode == "terminal":
            return self._value
        if self._mode == "cancel":
            raise KeyboardInterrupt
        # block-mode: wait until the poller cancels us via app.exit().
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self.app._fut = fut
        self.app.is_running = True
        try:
            return await fut
        finally:
            self.app.is_running = False


def _patch_prompt_session(monkeypatch: pytest.MonkeyPatch, *, mode: str, value=None) -> None:
    """Make the interactive coroutine build our fake PromptSession + no-op
    ``patch_stdout`` so no real terminal is touched."""
    import contextlib

    import prompt_toolkit
    import prompt_toolkit.patch_stdout

    monkeypatch.setattr(
        prompt_toolkit,
        "PromptSession",
        lambda **k: _FakePromptSession(mode=mode, value=value),
    )
    monkeypatch.setattr(
        prompt_toolkit.patch_stdout,
        "patch_stdout",
        lambda *a, **k: contextlib.nullcontext(),
    )


def test_interactive_race_web_first_cancels_prompt_and_cleans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A web answer the poller finds while the live prompt is blocking wins:
    the prompt is cancelled via ``app.exit(web_sentinel)`` and the loser leaves
    no residual call/response (no double-consume possible)."""
    call_path = _seed_call(tmp_path)
    interaction_calls.write_response(call_path, {"decision": "skip"})

    # The prompt blocks forever; only the poller (finding the web answer) can
    # end it — exercising the real cross-thread cancellation path.
    _patch_prompt_session(monkeypatch, mode="block")

    source, choice = run._await_terminal_or_web_choice_interactive(
        call_path,
        message="What would you like to do?",
        options=_OPTIONS,
        poll_interval=0.02,
    )

    assert source == run._FAILURE_SRC_WEB
    assert choice == 1  # skip
    # Winner consumed + loser torn down: nothing lingers on disk.
    assert not call_path.exists()
    assert not call_path.with_name(call_path.stem + ".response").exists()
    assert not call_path.with_name(call_path.stem + ".response.json").exists()


def test_interactive_race_terminal_first_tears_down_poller_and_cleans(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the terminal answers first (no web response), the prompt returns
    the operator's choice, the poller is torn down, and any concurrent webui
    call/response artifact is cleaned so the chip vanishes."""
    call_path = _seed_call(tmp_path)
    assert call_path.exists()

    # Operator types "2" → Skip (index 1). No web response is ever written.
    _patch_prompt_session(monkeypatch, mode="terminal", value="2")

    source, choice = run._await_terminal_or_web_choice_interactive(
        call_path,
        message="What would you like to do?",
        options=_OPTIONS,
        poll_interval=0.02,
    )

    assert source == run._FAILURE_SRC_TERMINAL
    assert choice == 1  # skip
    # The CLI committed → webui chip torn down.
    assert not call_path.exists()
    assert not call_path.with_name(call_path.stem + ".response").exists()


def test_interactive_race_ctrl_c_aborts_and_cleans_webui_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl+C / EOF at the failure prompt (no web answer) returns CANCEL — and
    the deterministic retry_decision call file is torn down too, so the
    FAILED-exempt chip does not keep surfacing on the now-aborted flow."""
    call_path = _seed_call(tmp_path)
    assert call_path.exists()

    _patch_prompt_session(monkeypatch, mode="cancel")

    source, choice = run._await_terminal_or_web_choice_interactive(
        call_path,
        message="What would you like to do?",
        options=_OPTIONS,
        poll_interval=0.02,
    )

    assert source == run._FAILURE_SRC_CANCEL
    assert choice is None  # call site maps this to abort
    # The losing webui channel is dismantled even on the abort-via-Ctrl+C path.
    assert not call_path.exists()
    assert not call_path.with_name(call_path.stem + ".response").exists()
    assert not call_path.with_name(call_path.stem + ".response.json").exists()
