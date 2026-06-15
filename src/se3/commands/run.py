"""SE3 Run command — The unified entry point for SE3 3.0 flow engine.

Replaces start/work/done with a state machine-driven workflow that:
- Creates new flows or resumes interrupted ones
- Handles all step types programmatically

Usage:
    se3 run "Implement feature X"              # New flow
    se3 run --resume                           # Resume interrupted flow
    se3 run "Fix bug" --type=bugfix            # Specify task type
"""

from __future__ import annotations

import json
import logging
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import typer
from rich.rule import Rule

# Add engine to path if needed
try:
    from ..engine.models import FlowInstance, FlowStatus, StepStatus, StepType
    from ..engine.persistence import PersistenceManager
    from ..engine.state_machine import StateMachine
    from ..engine.context_builder import ContextBuilder
    from ..engine.steps import STEP_HANDLERS
    from ..engine.steps.discovery import PROGRAMMATIC_CONFIRM_SENTINEL
    from ..engine.llm_caller import set_extra_prompt
    from ..config import ConfigError, clear_main_repo_root_cache
    from ..engine.output import (
        display_error,
        display_success,
        format_output,
        get_console,
        render_full,
        render_usage_block,
    )
    from ..engine.event_stream import EventEmitter, EventType, new_event
    from ..engine.sink import CliSink, HistorySink, JsonSink
    from ..cli import _read_multiline_input
except ImportError:
    # Direct import for development
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from engine.models import FlowInstance, FlowStatus, StepStatus, StepType
    from engine.persistence import PersistenceManager
    from engine.state_machine import StateMachine
    from engine.context_builder import ContextBuilder
    from engine.steps import STEP_HANDLERS
    from engine.steps.discovery import PROGRAMMATIC_CONFIRM_SENTINEL
    from engine.llm_caller import set_extra_prompt
    from config import ConfigError, clear_main_repo_root_cache
    from engine.output import (
        display_error,
        display_success,
        format_output,
        get_console,
        render_full,
        render_usage_block,
    )
    from engine.event_stream import EventEmitter, EventType, new_event
    from engine.sink import CliSink, HistorySink, JsonSink
    from cli import _read_multiline_input


app = typer.Typer()
logger = logging.getLogger(__name__)

SE3_DIR = "se3"
STATE_FILE = "state/engine.json"

# Global flag set by SIGINT handler to detect interrupt requests
_interrupt_requested = False


def _sigint_handler(signum: int, frame: Any) -> None:
    """Handle SIGINT by setting a flag and re-raising KeyboardInterrupt.

    This ensures Ctrl-C is never lost, even during blocking subprocess calls.
    """
    global _interrupt_requested
    _interrupt_requested = True
    raise KeyboardInterrupt


def get_project_root() -> Path:
    """Find project root by looking for .git directory or an SE3 config file.

    When the current directory is inside a git worktree, this returns the
    worktree root (so SE3 state files remain isolated per-worktree).
    Config lookup via :func:`config.get_project_config_path` automatically
    ascends to the main repository when appropriate, so worktree-local
    ``se3.local.yaml`` still takes precedence and the main repo's
    ``se3.local.yaml`` can override the worktree's tracked ``se3.yaml``.
    """
    from ..config import is_se3_project_root

    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        # Check for .git directory
        if (parent / ".git").exists():
            return parent
        # Check for any SE3 project marker (se3.yaml, se3.local.yaml, se3.config.yaml)
        if is_se3_project_root(parent):
            return parent
    return cwd


def _interpret_confirm_answer(text: str) -> tuple[bool, Optional[str]]:
    """Interpret a free-text CONFIRM answer from the web console.

    The web console offers only a single free-text reply box, so an operator
    approving a CONFIRM gate types something like "approve" / "yes" / "ok"
    rather than a structured ``{approved: true}`` payload. Recognize common
    approval phrases as approval; treat anything else as a revision request,
    carrying the full text along as feedback.

    Returns:
        (approved, feedback) — feedback is None on approval, otherwise the
        original (stripped) text so the reviewed step gets the operator's note.
    """
    stripped = (text or "").strip()
    if not stripped:
        # An empty answer is ambiguous; default to not approved so the gate
        # is not silently passed.
        return False, None

    # Match the first word / whole-string against known approval tokens so a
    # longer note like "approve, looks good" still counts as approval.
    approve_tokens = {
        "approve", "approved", "yes", "y", "ok", "okay", "lgtm",
        "accept", "accepted", "continue", "proceed", "pass", "skip",
    }
    reject_tokens = {
        "no", "n", "reject", "rejected", "deny", "denied",
        "request changes", "changes", "revise", "revision",
    }

    lowered = stripped.lower()
    first_word = lowered.split()[0] if lowered.split() else lowered

    if lowered in approve_tokens or first_word in approve_tokens:
        return True, None
    if lowered in reject_tokens or first_word in reject_tokens:
        return False, stripped

    # Any other free text is treated as a revision request, with the operator's
    # note preserved as feedback for the reviewed step.
    return False, stripped


def _check_confirm_response(flow: FlowInstance, current_step: Any, project_root: Path) -> Optional[StepStatus]:
    """Check for existing human response when resuming a CONFIRM step.

    This prevents duplicate call files and enables immediate continuation
    if the human has already responded.

    Args:
        flow: Current flow instance
        current_step: The CONFIRM step being resumed
        project_root: Project root directory

    Returns:
        StepStatus if response found and processed, None otherwise
    """
    calls_dir = project_root / "se3" / "calls"
    if not calls_dir.exists():
        return None

    for call_file in calls_dir.glob("confirm_*.json"):
        try:
            with open(call_file) as f:
                data = json.load(f)

            # Match by step_id only — change_id is flow-level and would
            # cause different confirm steps to share the same response
            if data.get('step') == current_step.step_id:
                # Check for a sibling response file. The web console answers
                # via the daemon, which writes ``<stem>.response.json`` with
                # the answer nested under a ``response`` key; an interactive
                # operator path writes a plain ``<stem>.response``. Accept both.
                for response_path in (
                    call_file.parent / f"{call_file.stem}.response.json",
                    call_file.parent / f"{call_file.stem}.response",
                ):
                    if not response_path.exists():
                        continue
                    try:
                        with open(response_path) as f:
                            response_data = json.load(f)

                        # Unwrap the daemon's ``{"response": ...}`` envelope.
                        # The web console POSTs a free-text answer, so the
                        # daemon writes ``{"call_id": ..., "response": "<text>"}``
                        # — the inner value can be either a structured
                        # ``{approved, feedback}`` dict or a plain string.
                        free_text: Optional[str] = None
                        if isinstance(response_data, dict):
                            inner = response_data.get('response')
                            if isinstance(inner, dict):
                                response_data = inner
                            elif isinstance(inner, str):
                                free_text = inner

                        if free_text is not None:
                            # Web console path: interpret the operator's
                            # free-text answer rather than rejecting blindly.
                            approved, feedback = _interpret_confirm_answer(free_text)
                        else:
                            approved = response_data.get('approved')
                            feedback = response_data.get('feedback')
                            # A response file with no explicit ``approved`` key
                            # but a free-text ``text``/``feedback`` value is
                            # treated as a free-text answer too.
                            if approved is None:
                                text_answer = response_data.get('text')
                                if isinstance(text_answer, str) and text_answer.strip():
                                    approved, feedback = _interpret_confirm_answer(text_answer)
                                elif isinstance(feedback, str) and feedback.strip():
                                    approved, feedback = _interpret_confirm_answer(feedback)
                                else:
                                    approved = False
                            approved = bool(approved)

                        # Store result in step outputs for state machine
                        current_step.outputs['review_result'] = {
                            'approved': approved,
                            'feedback': feedback,
                            'step_to_review_id': current_step.inputs.get('step_to_review_id'),
                            'step_to_review_type': current_step.inputs.get('step_to_review_type'),
                        }

                        if approved:
                            current_step.outputs['revision_feedback'] = feedback
                            return StepStatus.COMPLETED
                        else:
                            current_step.outputs['revision_feedback'] = feedback
                            return StepStatus.REVISION_NEEDED

                    except (json.JSONDecodeError, IOError):
                        logger.warning(f"Failed to parse response file: {response_path}")
                        continue
        except (json.JSONDecodeError, IOError):
            continue

    return None


def _handle_confirm_pause(
    flow: FlowInstance,
    current_step: Any,
    persistence: Any,
    project_root: Path,
    prompt_history: Any = None,
) -> Optional[bool]:
    """Handle interactive confirmation when CONFIRM step is paused.

    Displays the reviewed step's content and prompts the user to approve or
    request changes. Writes the response file so the confirm handler can
    process it on re-run.

    Returns:
        True if response was written, None if user chose to exit.
    """
    # Drain any web-pushed interjections that arrived while this confirm gate
    # was waiting. The structured approve/feedback JSON payload is left
    # unchanged — the history-write + task_description recompose performed
    # inside the drain is what makes the interjection visible to later steps.
    _drain_pending_interjections(flow, project_root, persistence)

    step_to_review_id = current_step.inputs.get("step_to_review_id")
    step_to_review_type = current_step.inputs.get("step_to_review_type", "unknown")

    # The reviewed step's output was already displayed by render_step_output
    # in the previous iteration, so just prompt directly.
    options = ["Approve and continue", "Request changes", "Exit (pause flow)"]
    try:
        choice = prompt_user_choice(
            f"Review {step_to_review_type} output above:", options
        )
    except (KeyboardInterrupt, EOFError):
        persistence.save_flow(flow)
        return None

    if choice == 2:
        # Exit
        persistence.save_flow(flow)
        return None

    approved = choice == 0
    feedback = None

    if not approved:
        # Get feedback from user
        feedback = _read_multiline_input(
            prompt_title="Feedback",
            prompt_message="Describe the changes you'd like (Ctrl+D or Esc+Enter to finish, Ctrl+C to cancel):",
            history=prompt_history,
        )
        if feedback is None:
            persistence.save_flow(flow)
            return None

    # Write the response file
    call_file_path = current_step.outputs.get("call_file")
    if call_file_path:
        call_file = Path(call_file_path)
        response_path = call_file.parent / f"{call_file.stem}.response"
        response_data = {
            "approved": approved,
            "feedback": feedback,
            "step_to_review_id": step_to_review_id,
            "step_to_review_type": step_to_review_type,
        }
        with open(response_path, "w") as f:
            json.dump(response_data, f, indent=2, ensure_ascii=False)

    status_text = "Approved" if approved else f"Changes requested: {feedback}"
    render_full(status_text, title="Confirmation Result")

    return True


def find_existing_flows(project_root: Path) -> List[Dict[str, Any]]:
    """Find all existing flow state files."""
    flows = []
    se3_dir = project_root / SE3_DIR
    state_file = se3_dir / "state" / "engine.json"

    if not state_file.exists():
        return flows

    try:
        with open(state_file) as f:
            data = json.load(f)
            state_data = data.get("state", {})
            flows.append({
                "id": data.get("flow_id", "unknown"),
                "status": data.get("status", "unknown"),
                "description": data.get("task_description", "No description"),
                "current_step": state_data.get("current_step_id"),
                "file": state_file.name,
            })
    except (json.JSONDecodeError, IOError):
        pass

    return flows


def prompt_user_choice(message: str, options: List[str]) -> int:
    """Prompt user to select an option."""
    print(f"\n{message}")
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")

    while True:
        try:
            choice = input("\nSelect (number): ").strip()
            idx = int(choice) - 1
            if 0 <= idx < len(options):
                return idx
            print(f"Please enter a number between 1 and {len(options)}")
        except ValueError:
            print("Please enter a valid number")
        except EOFError:
            # Handle non-interactive mode - default to last option (typically Abort)
            print(f"Non-interactive mode detected, selecting option {len(options)} ({options[-1]})")
            return len(options) - 1


def _stdin_is_interactive() -> bool:
    """Return whether the process has an interactive (TTY) stdin.

    Off a terminal (a daemon-spawned ``se3 run --output-format json``, CI, a
    pipe), there is no operator to host an interactive prompt — the failure
    decision must instead be externalised as a call file.
    """
    try:
        return bool(sys.stdin and sys.stdin.isatty())
    except (ValueError, AttributeError):
        return False


def _retry_decision_call_path(project_root: Path, step_id: str) -> Path:
    """Path of the deterministic ``retry_decision_{step_id}.json`` call file.

    Shared helper used by both :func:`_resolve_step_failure_action` (to probe
    for an out-of-band webui answer at the interactive entry point) and the
    post-CLI-prompt cleanup at the failure-handling call site, so both ends of
    the mutual-exclusion pair agree on a single filename.
    """
    from ..engine import interaction_calls

    return interaction_calls.calls_dir_for(project_root) / f"retry_decision_{step_id}.json"


def _cleanup_retry_decision_artifacts(call_path: Path) -> None:
    """Best-effort unlink of a retry_decision call file and its sibling
    response files (``.response`` / ``.response.json``).

    Used on both the consume-an-out-of-band-answer path and the
    interactive-CLI-answered path to make sure no stale webui chip lingers
    after a decision has been taken.
    """
    for stale in (
        call_path,
        call_path.with_name(call_path.stem + ".response"),
        call_path.with_name(call_path.stem + ".response.json"),
    ):
        try:
            stale.unlink()
        except OSError:
            pass


# Source markers + decision vocabulary for the failure-decision dual channel —
# the selection-mode sibling of the discovery ``_DISCOVERY_SRC_*`` markers.
_FAILURE_SRC_TERMINAL = "terminal"
_FAILURE_SRC_WEB = "web"
_FAILURE_SRC_CANCEL = "cancel"

# Retry / Skip / Abort, in the 0 / 1 / 2 order the failure-handling call site
# (and the historical prompt_user_choice menu) expects.
_FAILURE_DECISIONS = ("retry", "skip", "abort")


def _normalize_failure_decision(raw: Any) -> str:
    """Coerce a raw decision payload to ``retry`` / ``skip`` / ``abort``.

    Anything unrecognized (or missing) is taken as ``abort`` — the safe
    default that never silently re-runs or skips work.
    """
    decision = str(raw if raw is not None else "abort").strip().lower()
    return decision if decision in _FAILURE_DECISIONS else "abort"


def _failure_decision_to_choice(decision: str) -> int:
    """Map a (raw or normalized) decision to its 0 / 1 / 2 choice index."""
    return _FAILURE_DECISIONS.index(_normalize_failure_decision(decision))


def _read_failure_response_decision(call_file: Path) -> Optional[str]:
    """Return the normalized decision from a retry_decision response, or ``None``.

    Recognises both ``.response`` and ``.response.json`` siblings via
    :func:`interaction_calls.read_response`; the answer may live under a
    ``decision`` or ``response`` key. Returns ``None`` when no answer is on
    disk yet.
    """
    from ..engine import interaction_calls

    response = interaction_calls.read_response(call_file)
    if response is None:
        return None
    return _normalize_failure_decision(
        response.get("decision") or response.get("response")
    )


def _resolve_step_failure_action(
    project_root: Path,
    flow: FlowInstance,
    current_step: Any,
    error_msg: str,
    *,
    interactive: bool,
) -> Tuple[str, Any]:
    """Decide how a FAILED step should be handled — a *dual-channel* pause.

    Regardless of whether the process owns a terminal, a
    :data:`~se3.engine.interaction_calls.CALL_KIND_RETRY_DECISION` call file is
    written under ``se3/calls/`` so a webui bystander sees the failure as a
    Retry/Skip/Abort chip and can answer it. This is what makes the CLI and
    the web console *equivalent* on the failure-decision path (it fixes both
    "webui can't see the failure" and "webui can't retry").

    Interactive (TTY) and non-interactive (daemon-spawn / CI / pipe) failures
    are two decision channels that must finish *mutually exclusively* — once
    either side answers, the other must be left with no stale chip or call
    file. This function and the race helper / post-prompt cleanup at the
    failure-handling call site together enforce that.

    Returns one of:

    * ``("decision", "retry"|"skip"|"abort")`` — a sibling response is already
      on disk (a resume after a prior pause, or the webui answered before we
      got here); the answer is consumed and the call file plus both sibling
      response variants are removed. Returned on **both** channels.
    * ``("race", call_path)`` — interactive, no answer yet: the caller runs the
      *dual-channel race* (CLI Retry/Skip/Abort prompt vs. the webui response
      poller) — whoever answers first wins.
    * ``("pause", call_path)`` — non-interactive, no answer yet: the caller
      pauses the flow so the decision can be made out-of-band and applied on
      the next ``se3 run --resume`` (unchanged from the prior behavior).
    """
    from ..engine import interaction_calls

    # Unconditionally (re)write the retry_decision call file — in BOTH the
    # interactive and non-interactive cases — so the web console surfaces the
    # failure as a chip. The ``call_id`` is derived from the step id, so a
    # later failure of the same step reuses the same file rather than piling
    # up duplicates.
    call_path = Path(
        interaction_calls.write_retry_decision_call(
            project_root,
            flow_id=flow.flow_id,
            step_id=current_step.step_id,
            step_type=current_step.step_type.value,
            error=error_msg,
            retry_count=current_step.retry_count,
        )
    )

    # If an answer is already waiting on disk, consume it directly — identical
    # on both channels. The deterministic call_id means a stale response would
    # otherwise be silently re-applied on a later failure, so we clean up.
    decision = _read_failure_response_decision(call_path)
    if decision is not None:
        _cleanup_retry_decision_artifacts(call_path)
        return ("decision", decision)

    if interactive:
        # TTY: race the CLI prompt against the webui response poller.
        return ("race", call_path)

    # Non-interactive: pause and let the decision be made out-of-band.
    return ("pause", call_path)


def _await_terminal_or_web_choice(
    call_file: Optional[Path],
    *,
    message: str,
    options: List[str],
    poll_interval: float = 0.4,
) -> Tuple[str, Optional[int]]:
    """Race a CLI Retry/Skip/Abort choice against a webui retry_decision answer.

    The selection-mode sibling of :func:`_await_terminal_or_web`. Returns a
    ``(source, choice)`` tuple where *choice* is the 0-based option index:

    * ``(_FAILURE_SRC_WEB, idx)``      — a web response was already on disk or
      arrived first; the call file and both sibling responses are removed so
      it can never be read twice.
    * ``(_FAILURE_SRC_TERMINAL, idx)`` — the operator answered at the terminal;
      any concurrent webui call/response is best-effort torn down so the chip
      vanishes.
    * ``(_FAILURE_SRC_CANCEL, None)``  — Ctrl+C / EOF with no web answer; the
      caller treats this as abort.

    With no ``call_file`` (or no TTY) there is no web channel to race, so it
    degrades to a plain :func:`prompt_user_choice` — behavior equivalent to the
    pre-dual-channel path.
    """
    # No web channel — terminal only (backward compatible / no call_file).
    if call_file is None:
        return (_FAILURE_SRC_TERMINAL, prompt_user_choice(message, options))

    # Deterministic priority: an answer already waiting on disk wins outright.
    early = _read_failure_response_decision(call_file)
    if early is not None:
        _cleanup_retry_decision_artifacts(call_file)
        return (_FAILURE_SRC_WEB, _failure_decision_to_choice(early))

    # No interactive terminal to race against (piped stdin / no TTY): block on
    # the plain choice, then re-check the web file once so a response that
    # landed during the read is still preferred. Either way clean up so the
    # chip does not linger.
    if not sys.stdin.isatty():
        choice = prompt_user_choice(message, options)
        late = _read_failure_response_decision(call_file)
        if late is not None:
            _cleanup_retry_decision_artifacts(call_file)
            return (_FAILURE_SRC_WEB, _failure_decision_to_choice(late))
        _cleanup_retry_decision_artifacts(call_file)
        return (_FAILURE_SRC_TERMINAL, choice)

    # Interactive TTY: race the prompt against a background web-response poller.
    return _await_terminal_or_web_choice_interactive(
        call_file,
        message=message,
        options=options,
        poll_interval=poll_interval,
    )


def _await_terminal_or_web_choice_interactive(
    call_file: Path,
    *,
    message: str,
    options: List[str],
    poll_interval: float,
) -> Tuple[str, Optional[int]]:
    """TTY dual-wait for the failure decision: a choice prompt raced against a
    web-response poller.

    Mirrors :func:`_await_terminal_or_web_interactive` but reads a numeric
    Retry/Skip/Abort choice instead of free text. A daemon thread polls
    ``call_file`` for a sibling response and cancels the prompt the instant one
    appears (re-scheduling ``app.exit`` until the app is actually running to
    close the build-race window). Whichever side answers first wins; the loser
    is torn down without consuming anything twice. Any unexpected failure
    degrades to a plain :func:`prompt_user_choice`.
    """
    import asyncio
    import threading

    from prompt_toolkit import PromptSession
    from prompt_toolkit.patch_stdout import patch_stdout

    # Render the choice menu (same shape as prompt_user_choice).
    print(f"\n{message}")
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")

    session = PromptSession(message="\nSelect (number): ")
    web_sentinel = object()

    async def _race() -> Tuple[str, Optional[int]]:
        loop = asyncio.get_running_loop()
        stop = threading.Event()
        web_holder: Dict[str, Optional[str]] = {"value": None}

        def _cancel_prompt() -> None:
            app = session.app
            if app is not None and app.is_running:
                app.exit(result=web_sentinel)

        def _poll() -> None:
            while not stop.is_set():
                decision = _read_failure_response_decision(call_file)
                if decision is not None:
                    web_holder["value"] = decision
                    # The app may not be running yet on the first try; keep
                    # scheduling the cancel until the prompt tears down.
                    while not stop.is_set():
                        loop.call_soon_threadsafe(_cancel_prompt)
                        if stop.wait(poll_interval):
                            break
                    return
                stop.wait(poll_interval)

        poller = threading.Thread(target=_poll, daemon=True)
        poller.start()
        try:
            with patch_stdout():
                while True:
                    result = await session.prompt_async()
                    if result is web_sentinel:
                        break
                    try:
                        idx = int(str(result).strip()) - 1
                    except (ValueError, TypeError):
                        idx = -1
                    if 0 <= idx < len(options):
                        return (_FAILURE_SRC_TERMINAL, idx)
                    print(
                        f"Please enter a number between 1 and {len(options)}"
                    )
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            stop.set()

        # Web won (sentinel) or the prompt was cancelled — a web answer may
        # also have landed during teardown.
        if web_holder["value"] is not None:
            _cleanup_retry_decision_artifacts(call_file)
            return (_FAILURE_SRC_WEB, _failure_decision_to_choice(web_holder["value"]))
        return (_FAILURE_SRC_CANCEL, None)

    try:
        source, choice = asyncio.run(_race())
    except KeyboardInterrupt:
        # Ctrl+C at the failure prompt is a CLI-side commitment to abort — tear
        # down the concurrent webui call/response so the (FAILED-exempt)
        # retry_decision chip does not keep surfacing on the now-aborted flow.
        _cleanup_retry_decision_artifacts(call_file)
        return (_FAILURE_SRC_CANCEL, None)
    except Exception:  # pragma: no cover - defensive: fall back to plain choice
        logger.exception(
            "Interactive failure dual-wait failed; using a plain choice prompt"
        )
        choice = prompt_user_choice(message, options)
        late = _read_failure_response_decision(call_file)
        if late is not None:
            _cleanup_retry_decision_artifacts(call_file)
            return (_FAILURE_SRC_WEB, _failure_decision_to_choice(late))
        _cleanup_retry_decision_artifacts(call_file)
        return (_FAILURE_SRC_TERMINAL, choice)

    # The CLI committed to a decision — including aborting via Ctrl+C / EOF
    # (_FAILURE_SRC_CANCEL) — so best-effort tear down any concurrent webui
    # call/response, identically for all outcomes, so the chip disappears
    # (mirrors the post-prompt cleanup the non-dual-channel path used to do).
    # _FAILURE_SRC_WEB already cleaned up inside _race().
    if source != _FAILURE_SRC_WEB:
        _cleanup_retry_decision_artifacts(call_file)
    return (source, choice)


def _drain_pending_interjections(
    flow: FlowInstance,
    project_root: Path,
    persistence: PersistenceManager,
) -> List[str]:
    """Consume daemon-queued interjection call files.

    The web console pushes mid-flow instructions through the server as
    ``MSG_INTERJECT_FLOW``; the daemon turns each into an ``interjection``-kind
    call file under ``se3/calls/``. This function drains those files and:

    * folds each into ``flow.state.context["user_interjections"]`` using the
      same entry shape as a Ctrl-C interjection;
    * recomposes the current step's ``task_description`` so the instruction
      takes effect on the next run / iteration;
    * writes a ``{role: 'user', kind: 'interjection', ...}`` line into the
      current step's history jsonl so ``se3 history show`` and the web
      console see the user's bubble at the point the interjection arrived;
    * when the current step is PAUSED (waiting on a prompt response), also
      buffers each drained text into
      ``flow.state.context['_pending_paused_interjections']`` so the reply
      path can prefix it onto the next user message to the LLM.

    Returns the list of drained text strings (oldest first); callers can use
    the return value to gate display / log messages.
    """
    from ..engine import interaction_calls

    try:
        drained = interaction_calls.drain_interjection_requests(project_root)
    except Exception:  # pragma: no cover - defensive; never break the flow
        logger.exception("Failed to drain pending interjection requests")
        return []
    if not drained:
        return []

    from datetime import datetime

    from ..engine.chat_history import record_user_interjection
    from ..engine.state_machine import _effective_task_description_base
    from ..engine.task_description import compose_task_description_with_interjections

    interjections = flow.state.context.setdefault("user_interjections", [])
    current_step = flow.state.get_current_step()
    step_id = ""
    step_type_value = ""
    attempt = 0
    is_paused = False
    if current_step is not None:
        step_id = current_step.step_id
        step_type_value = (
            current_step.step_type.value
            if hasattr(current_step.step_type, "value")
            else str(current_step.step_type)
        )
        try:
            attempt = int(current_step.inputs.get("retry_count", 0) or 0)
        except (TypeError, ValueError):
            attempt = 0
        is_paused = current_step.status == StepStatus.PAUSED

    # The PAUSED reply-prefix buffer is only populated for DISCOVERY-paused
    # steps — only discovery reply paths call _consume_paused_interjection_prefix
    # to read + clear it. CONFIRM-paused interjections already reach the LLM via
    # task_description recomposition (user_interjections list); buffering them
    # here would leak into a later DISCOVERY pause's LLM call as a stale prefix.
    is_discovery_paused = is_paused and current_step.step_type == StepType.DISCOVERY
    paused_buffer: Optional[List[str]] = (
        flow.state.context.setdefault("_pending_paused_interjections", [])
        if is_discovery_paused
        else None
    )

    drained_texts: List[str] = []
    for item in drained:
        text = item["text"]
        drained_texts.append(text)
        interjections.append(
            {
                "text": text,
                "step_id": step_id,
                "step_type": step_type_value,
                "timestamp": datetime.now().isoformat(),
                "source": "web-console",
            }
        )
        if paused_buffer is not None:
            paused_buffer.append(text)
        # Persist the user's bubble to the per-step jsonl so history viewers
        # and the web console see the interjection in chronological order
        # alongside the LLM turns.
        if step_id:
            try:
                record_user_interjection(
                    project_root=project_root,
                    flow_id=flow.flow_id,
                    step_id=step_id,
                    step_type=step_type_value,
                    text=text,
                    attempt=attempt,
                    source="web-console",
                )
            except Exception:  # pragma: no cover - never break the flow
                logger.exception(
                    "Failed to write user interjection to history jsonl"
                )
        get_console().print(
            f"[dim]Interjection received from web console: "
            f"{text[:80]}[/dim]"
        )

    if current_step is not None:
        current_step.inputs["task_description"] = (
            compose_task_description_with_interjections(
                base=_effective_task_description_base(flow),
                interjections=interjections,
            )
        )
    persistence.save_flow(flow)
    return drained_texts


def _consume_paused_interjection_prefix(flow: FlowInstance) -> str:
    """Return + clear the buffered ``[interjection: ...]\\n`` prefix.

    PAUSED reply paths (discovery continue, etc.) call this just before
    handing the user's reply to the LLM as the next user turn. The buffer is
    populated by :func:`_drain_pending_interjections` only when the current
    step is PAUSED; entries are joined in arrival order, each rendered as a
    ``[interjection: <text>]`` line. An empty buffer (or a flow that has no
    ``state.context`` shape, such as a unit-test stub) yields the empty
    string so the reply is unchanged.
    """
    try:
        context = flow.state.context
    except AttributeError:
        return ""
    buffer = context.get("_pending_paused_interjections")
    if not buffer:
        return ""
    lines = [f"[interjection: {text}]" for text in buffer if str(text).strip()]
    # Clear the buffer regardless — even an all-whitespace queue should not
    # carry over into the next reply.
    context["_pending_paused_interjections"] = []
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _complete_flow_via_fallback(flow: FlowInstance) -> None:
    """Mark ``flow`` COMPLETED via the no-current-step fallback path.

    Invoked from the run loop when :meth:`State.get_current_step` returns
    ``None`` (e.g. a resume whose ``current_step_id`` is stale/dangling, so the
    step it names is gone from ``state.steps``). Besides flipping the status, it
    mirrors the engine-side completion fix
    (:meth:`StateMachine.transition_to_next`) by advancing the step index to the
    total step count, so the unified "completed steps / total steps" semantics
    report total/total and progress 1.0 to every consumer of engine state
    (the daemon aggregator, history, the web console). Without this advance, a
    flow completed via this fallback would surface a mid-flow index
    (e.g. 5/13 / progress 0.38) despite being done.
    """
    flow.status = FlowStatus.COMPLETED
    flow.state.current_step_index = len(flow.state.selected_steps)


def handle_resume_interactive(project_root: Path) -> Optional[str]:
    """Handle interactive resume flow.

    Returns:
        Flow ID to resume, or None if user chooses new flow.
    """
    flows = find_existing_flows(project_root)
    # Isolated --worktree runs persist their state in their own worktree's
    # engine.json (not the main repo's), so they must be discovered separately
    # and folded into the same picker. Each carries enough metadata for
    # resume_run to re-dispatch it inside its worktree and merge it back.
    worktree_runs = find_resumable_worktree_runs(project_root)

    # Filter to resumable flows (exclude only COMPLETED). worktree_runs are
    # already filtered to non-COMPLETED by find_resumable_worktree_runs.
    terminal_statuses = {FlowStatus.COMPLETED.value}
    active_flows = [f for f in flows if f["status"] not in terminal_statuses]
    active_flows = active_flows + worktree_runs

    if not active_flows:
        if not flows and not worktree_runs:
            get_console().print("[dim]No existing flows found. Starting new flow.[/dim]")
        else:
            get_console().print("[dim]No in-progress flows found.[/dim]")
            if flows:
                get_console().print(f"[dim]Found {len(flows)} completed flow(s).[/dim]")
        return None

    if len(active_flows) == 1:
        flow = active_flows[0]
        is_failed = flow["status"] == FlowStatus.FAILED.value
        label = "failed" if is_failed else "interrupted"
        wt_suffix = " (worktree)" if flow.get("is_worktree_run") else ""
        content = [
            f"Found {label} flow:",
            "",
            f"  ID: {flow['id']}{wt_suffix}",
            f"  Description: {flow['description']}",
            f"  Current step: {flow['current_step']}",
        ]
        render_full("\n".join(content), title="Resume Flow")

        action = "Retry failed flow" if is_failed else "Resume this flow"
        options = [action, "Start new flow"]
        choice = prompt_user_choice("What would you like to do?", options)

        if choice == 0:
            return flow['id']
        return None

    # Multiple active flows
    content = [f"Found {len(active_flows)} resumable flows:", ""]
    options = []
    for flow in active_flows:
        status_tag = " [FAILED]" if flow["status"] == FlowStatus.FAILED.value else ""
        wt_tag = " [worktree]" if flow.get("is_worktree_run") else ""
        options.append(
            f"{flow['description']} (step: {flow['current_step']}){wt_tag}{status_tag}"
        )
    options.append("Start new flow")

    for i, opt in enumerate(options[:-1], 1):
        content.append(f"  {i}. {opt}")
    content.append(f"  {len(options)}. Start new flow")

    render_full("\n".join(content), title="Resume Flow")
    choice = prompt_user_choice("Which flow to resume?", options)

    if choice < len(active_flows):
        return active_flows[choice]["id"]
    return None


def _handle_step_interrupt(flow: FlowInstance, current_step: Any, persistence: PersistenceManager, prompt_history: Any = None) -> Optional[StepStatus]:
    """Handle KeyboardInterrupt during step execution.

    A non-empty user-typed instruction is persisted to
    ``flow.state.context["user_interjections"]`` and inlined into the
    current step's ``inputs["task_description"]`` so the immediate re-run
    sees it. Downstream steps pick up the same interjections via
    ``state_machine._build_step_inputs`` (which composes them onto every
    new step's task_description on construction).

    Returns:
        StepStatus to continue, or None to exit
    """
    user_input = _read_multiline_input(
        prompt_title="Additional Instruction",
        prompt_message="Enter additional instruction (Ctrl+D or Esc+Enter to finish, Ctrl+C to cancel, empty to retry as-is):",
        history=prompt_history,
    )
    if user_input is None:
        # Cancelled (Ctrl+C): save and exit
        persistence.save_flow(flow)
        render_full(
            "Interrupted by user. Flow state saved.\n"
            "Resume with: se3 run --resume",
            title="Exit"
        )
        return None
    if user_input:
        from datetime import datetime
        from ..engine.task_description import compose_task_description_with_interjections
        from ..engine.state_machine import _effective_task_description_base

        step_type_value = (
            current_step.step_type.value
            if hasattr(current_step.step_type, "value")
            else str(current_step.step_type)
        )
        entry = {
            "text": user_input,
            "step_id": current_step.step_id,
            "step_type": step_type_value,
            "timestamp": datetime.now().isoformat(),
        }
        flow.state.context.setdefault("user_interjections", []).append(entry)

        # Mutate the current step's inputs in-place so the immediate re-run
        # sees the new instruction. Compose from the *un-decorated* base
        # (refined_description if discovery ran, else flow.task_description)
        # against the FULL interjections list — never against the step's
        # already-composed task_description. Snapshotting the latter would
        # double the ``## Additional Instructions`` section when the user
        # interrupts a step that was built AFTER an earlier interjection
        # (its inputs.task_description already carries the section, and
        # composing on top of it would emit a second one).
        current_step.inputs["task_description"] = (
            compose_task_description_with_interjections(
                base=_effective_task_description_base(flow),
                interjections=flow.state.context["user_interjections"],
            )
        )
        get_console().print(
            "[dim]Additional instruction recorded — retrying step "
            "with persistent interjection.[/dim]"
        )
    else:
        get_console().print("[dim]Retrying step as-is...[/dim]")
    # Reset step to PENDING so it re-runs
    current_step.status = StepStatus.PENDING
    persistence.save_flow(flow)
    # Return a special marker to indicate retry
    return StepStatus.PENDING


def _restore_discovery_display(current_step: Any) -> None:
    """Re-display the last AI message from a PAUSED discovery step on resume.

    When a DISCOVERY step is PAUSED and we are resuming (rather than running
    for the first time), the LLM already asked its question — we must NOT
    call the LLM again.  This function re-prints the last assistant message
    stored in ``discovery_state`` so the user can see what was asked before
    they type their reply.

    Args:
        current_step: The discovery step with status PAUSED
    """
    from ..engine.steps.discovery import _display_discovery_message

    discovery_state = current_step.inputs.get("discovery_state", {})
    history = discovery_state.get("history", [])

    # Find the last assistant entry in the conversation history
    last_assistant = None
    for entry in reversed(history):
        if entry.get("role") == "assistant":
            last_assistant = entry
            break

    if last_assistant:
        # Use step.outputs["message"] for display (clean parsed content),
        # NOT history content (which is the full raw LLM result text for context).
        content = current_step.outputs.get("message", last_assistant.get("content", ""))
        # Re-display proposed_description if it was set
        # (confirmation mode stores refined_description instead)
        proposed = current_step.outputs.get("proposed_description") \
            or current_step.outputs.get("refined_description") \
            or None
        questions = current_step.outputs.get("questions") or None
        # Detect confirmation mode for proper rendering
        is_confirmation = current_step.outputs.get("awaiting_programmatic_confirm", False)
        # Pass the raw result text so narrative outside JSON blocks is rendered
        raw_result_text = last_assistant.get("content", "")
        # No round/cumulative usage passed: --resume re-display issues no LLM
        # call, so the per-round footer must not be (re-)rendered here.
        _display_discovery_message(
            content, proposed, questions,
            is_confirmation=is_confirmation,
            raw_result_text=raw_result_text,
        )
    else:
        # No history yet — show generic resume notice
        get_console().print("[dim]Resuming discovery — please respond to continue.[/dim]")


def _maybe_write_discovery_call(
    flow: FlowInstance, current_step: Any, project_root: Optional[Path]
) -> Optional[Path]:
    """Mirror an interactive discovery pause to a ``se3/calls/`` call file.

    Writing the call file makes the web console surface the *same* pending
    interaction the terminal is blocking on, so a CLI-started discovery session
    can be answered from the web. Returns the call-file path, or ``None`` when
    no project root is known (the backward-compatible terminal-only path that
    unit tests exercise) or the write fails — in which case the caller simply
    falls back to a terminal-only wait.
    """
    if project_root is None:
        return None
    try:
        return _write_discovery_call(flow, current_step, Path(project_root))
    except Exception:  # pragma: no cover - defensive: never block on web mirror
        logger.exception("Failed to mirror discovery pause to a call file")
        return None


def _cleanup_discovery_response(call_file: Optional[Path]) -> None:
    """Remove only the sibling ``.response`` answer files for *call_file*.

    Consuming a web answer deletes its response file so a follow-up wait on the
    *same* call file (e.g. the confirmation gate's empty-input re-display loop)
    cannot re-read the already-consumed answer. The call file itself is left in
    place. Idempotent.
    """
    if call_file is None:
        return
    for path in (
        call_file.parent / f"{call_file.stem}.response.json",
        call_file.parent / f"{call_file.stem}.response",
    ):
        try:
            path.unlink()
        except OSError:
            pass


def _cleanup_discovery_call(call_file: Optional[Path]) -> None:
    """Remove the call file plus any sibling ``.response`` answer files.

    Called once a pause round is resolved (terminal/web answer, or cancel) so a
    stale call file never lingers as a "待回复" chip on the web console.
    Idempotent — safe to call when the files are already gone.
    """
    if call_file is None:
        return
    _cleanup_discovery_response(call_file)
    try:
        call_file.unlink()
    except OSError:
        pass


# Source markers returned by :func:`_await_terminal_or_web`.
_DISCOVERY_SRC_TERMINAL = "terminal"
_DISCOVERY_SRC_WEB = "web"
_DISCOVERY_SRC_CANCEL = "cancel"


def _await_terminal_or_web(
    call_file: Optional[Path],
    *,
    prompt_title: str,
    prompt_message: str,
    history: Any = None,
    strip: bool = True,
    poll_interval: float = 0.4,
    tick_callback: Optional[Callable[[], None]] = None,
) -> Tuple[str, Optional[str]]:
    """Wait for whichever comes first: a terminal answer or a web response.

    Returns a ``(source, value)`` tuple:

    * ``(_DISCOVERY_SRC_WEB, text)``      — a web response file arrived first.
    * ``(_DISCOVERY_SRC_TERMINAL, text)`` — the operator answered at the terminal.
    * ``(_DISCOVERY_SRC_CANCEL, None)``   — Ctrl+C / EOF with no answer.

    Determinism / no double-consume:

    * A web response already on disk when the wait begins always wins (it is
      checked before any terminal read) and is consumed (its ``.response`` file
      removed) before returning, so it can never be read twice.
    * When ``call_file`` is ``None`` there is no web channel, so this degrades
      to a plain :func:`_read_multiline_input` terminal read (the path unit
      tests exercise when no project root is supplied).
    """
    # No web channel — terminal only (backward compatible).
    if call_file is None:
        text = _read_multiline_input(
            prompt_title=prompt_title,
            prompt_message=prompt_message,
            history=history,
            strip=strip,
        )
        if text is None:
            return (_DISCOVERY_SRC_CANCEL, None)
        return (_DISCOVERY_SRC_TERMINAL, text)

    # Deterministic priority: an answer already waiting on disk wins outright.
    early = _read_discovery_response(call_file)
    if early is not None:
        _cleanup_discovery_response(call_file)
        return (_DISCOVERY_SRC_WEB, early)

    # No interactive terminal to race against (piped stdin / no TTY): block on
    # the plain reader, but re-check the web file once afterwards so a response
    # that landed during the read is still preferred.
    if not sys.stdin.isatty():
        text = _read_multiline_input(
            prompt_title=prompt_title,
            prompt_message=prompt_message,
            history=history,
            strip=strip,
        )
        late = _read_discovery_response(call_file)
        if late is not None:
            _cleanup_discovery_response(call_file)
            return (_DISCOVERY_SRC_WEB, late)
        if text is None:
            return (_DISCOVERY_SRC_CANCEL, None)
        return (_DISCOVERY_SRC_TERMINAL, text)

    # Interactive TTY: race the prompt_toolkit read against a background poller
    # that cancels the prompt the instant a web response file appears.
    return _await_terminal_or_web_interactive(
        call_file,
        prompt_title=prompt_title,
        prompt_message=prompt_message,
        history=history,
        strip=strip,
        poll_interval=poll_interval,
        tick_callback=tick_callback,
    )


def _await_terminal_or_web_interactive(
    call_file: Path,
    *,
    prompt_title: str,
    prompt_message: str,
    history: Any,
    strip: bool,
    poll_interval: float,
    tick_callback: Optional[Callable[[], None]] = None,
) -> Tuple[str, Optional[str]]:
    """TTY dual-wait: a prompt_toolkit read raced against a web-response poller.

    The prompt runs via :meth:`PromptSession.prompt_async` inside a private
    event loop. A daemon thread polls ``call_file`` for a sibling response and,
    when one appears, cancels the prompt by scheduling ``app.exit`` on the loop
    with ``loop.call_soon_threadsafe`` (re-scheduling until the app is actually
    running, to close the build-race window). Whichever side completes first
    wins; the loser is torn down without consuming anything twice. Any
    unexpected failure degrades to a plain terminal read.
    """
    import asyncio
    import threading

    from prompt_toolkit import PromptSession
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.keys import Keys
    from prompt_toolkit.patch_stdout import patch_stdout

    from ..engine.display import render_text

    render_text(prompt_message, title=prompt_title)

    kb = KeyBindings()

    @kb.add(Keys.ControlD)
    def _(event):  # noqa: ANN001 - prompt_toolkit callback signature
        event.app.current_buffer.validate_and_handle()

    session = PromptSession(
        multiline=True, message="> ", key_bindings=kb, history=history
    )
    web_sentinel = object()

    async def _race() -> Tuple[str, Optional[str]]:
        loop = asyncio.get_running_loop()
        stop = threading.Event()
        web_holder: Dict[str, Optional[str]] = {"value": None}

        def _cancel_prompt() -> None:
            app = session.app
            if app is not None and app.is_running:
                app.exit(result=web_sentinel)

        def _poll() -> None:
            while not stop.is_set():
                # Every poll tick: drain any web-pushed interjections that
                # arrived while the operator was at the prompt, so they are
                # persisted to history + buffered for the LLM prefix without
                # waiting for the user to type a reply.
                if tick_callback is not None:
                    try:
                        tick_callback()
                    except Exception:  # pragma: no cover - never break the wait
                        logger.exception(
                            "Interjection tick callback raised; ignoring"
                        )
                resp = _read_discovery_response(call_file)
                if resp is not None:
                    web_holder["value"] = resp
                    # The app may not be running yet on the first try; keep
                    # scheduling the cancel until the prompt tears down.
                    while not stop.is_set():
                        loop.call_soon_threadsafe(_cancel_prompt)
                        if stop.wait(poll_interval):
                            break
                    return
                stop.wait(poll_interval)

        poller = threading.Thread(target=_poll, daemon=True)
        poller.start()
        try:
            with patch_stdout():
                result = await session.prompt_async()
        except (KeyboardInterrupt, EOFError):
            result = None
        finally:
            stop.set()

        if result is web_sentinel:
            _cleanup_discovery_response(call_file)
            return (_DISCOVERY_SRC_WEB, web_holder["value"])
        if result is None:
            # Cancelled — but a web answer may have landed during teardown.
            if web_holder["value"] is not None:
                _cleanup_discovery_response(call_file)
                return (_DISCOVERY_SRC_WEB, web_holder["value"])
            return (_DISCOVERY_SRC_CANCEL, None)
        if strip:
            result = result.strip()
        return (_DISCOVERY_SRC_TERMINAL, result)

    try:
        return asyncio.run(_race())
    except KeyboardInterrupt:
        return (_DISCOVERY_SRC_CANCEL, None)
    except Exception:  # pragma: no cover - defensive: fall back to plain read
        logger.exception(
            "Interactive discovery dual-wait failed; using a plain terminal read"
        )
        text = _read_multiline_input(
            prompt_title=prompt_title,
            prompt_message=prompt_message,
            history=history,
            strip=strip,
        )
        late = _read_discovery_response(call_file)
        if late is not None:
            _cleanup_discovery_response(call_file)
            return (_DISCOVERY_SRC_WEB, late)
        if text is None:
            return (_DISCOVERY_SRC_CANCEL, None)
        return (_DISCOVERY_SRC_TERMINAL, text)


def _handle_discovery_pause(
    flow: FlowInstance,
    current_step: Any,
    persistence: PersistenceManager,
    prompt_history: Any = None,
    project_root: Optional[Path] = None,
) -> Optional[str]:
    """Handle an interactive discovery pause — get the user's response.

    The clarifying question is mirrored to a ``se3/calls/`` call file (when a
    project root is known) so the web console surfaces the same pending
    interaction; the terminal and the web response file are then awaited in
    parallel and whichever answers first drives the flow in this same process
    (no ``--resume`` needed). The flow stays RUNNING throughout — it is never
    marked PAUSED — so a watching daemon does not race this live process with a
    duplicate ``--resume`` spawn.

    Args:
        flow: Current flow instance
        current_step: The discovery step
        persistence: Persistence manager
        prompt_history: Prompt history for readline
        project_root: Project root for mirroring the pause to a call file

    Returns:
        The user's response string, the :data:`_PROGRAMMATIC_CONFIRM` sentinel
        (confirmation gate), or ``None`` to exit.
    """
    # Programmatic confirmation gate: LLM confirmed, now require human approval
    if current_step.outputs.get("awaiting_programmatic_confirm"):
        return _handle_discovery_programmatic_confirm(
            flow, current_step, persistence, prompt_history, project_root
        )

    render_full(
        "Discovery mode is exploring your requirements.\n"
        "Please respond to the questions above to help clarify what you want to build.",
        title="Discovery Pause"
    )

    # Drain on entry so any interjection queued before this pause is folded
    # in immediately, ahead of the dual-wait that blocks the operator.
    if project_root is not None:
        _drain_pending_interjections(flow, project_root, persistence)

    def _tick() -> None:
        if project_root is not None:
            _drain_pending_interjections(flow, project_root, persistence)

    call_file = _maybe_write_discovery_call(flow, current_step, project_root)
    try:
        while True:
            source, value = _await_terminal_or_web(
                call_file,
                prompt_title="Discovery Response",
                prompt_message="Enter your response (Ctrl+D or Esc+Enter to finish, Ctrl+C to cancel):",
                history=prompt_history,
                strip=True,
                tick_callback=_tick,
            )

            if source == _DISCOVERY_SRC_CANCEL:
                # User cancelled
                persistence.save_flow(flow)
                render_full(
                    "Discovery paused. Flow state saved.\n"
                    "Resume with: se3 run --resume",
                    title="Paused"
                )
                return None

            if not value:
                # Empty terminal input — ask again (web never submits empty).
                get_console().print(
                    "[yellow]Please provide a response or press Ctrl+C to exit.[/yellow]"
                )
                continue

            # Prefix any buffered interjections (collected by drain ticks while
            # we waited) onto this user reply so the next LLM call sees them
            # ahead of the actual reply text.
            prefix = (
                _consume_paused_interjection_prefix(flow)
                if project_root is not None
                else ""
            )
            if prefix:
                persistence.save_flow(flow)
                return prefix + value
            return value
    finally:
        _cleanup_discovery_call(call_file)


# Sentinel returned by programmatic confirm handler when user chooses to proceed.
# INVARIANT: This value is only returned AFTER setting inputs["programmatic_confirmed"]=True.
# The discovery_handler's early-return guard (programmatic_confirmed check) MUST execute
# before any code path that could feed this value to the LLM as a user message. The
# persistence.save_flow() call in the orchestrator loop happens after the sentinel is
# returned but before the next handler invocation, so the flag is always persisted.
_PROGRAMMATIC_CONFIRM = PROGRAMMATIC_CONFIRM_SENTINEL


def _handle_discovery_programmatic_confirm(
    flow: FlowInstance,
    current_step: Any,
    persistence: PersistenceManager,
    prompt_history: Any = None,
    project_root: Optional[Path] = None,
) -> Optional[str]:
    """Handle the programmatic confirmation gate after the LLM confirms discovery.

    Mirrored to a :data:`~se3.engine.interaction_calls.CALL_KIND_DISCOVERY_CONFIRM`
    call file (one-click ``"1"`` option) when a project root is known, then the
    terminal and the web response file are awaited in parallel. The user types
    exactly ``"1"`` (strict equality, only trailing-newline artifacts stripped)
    — or clicks the web confirm button, which submits the same ``"1"`` through
    the call/response channel — to confirm and proceed. Empty input is a no-op
    (re-displays the confirmation panel). Any other non-empty input continues
    discovery as the next user turn. The flow stays RUNNING throughout — it is
    never marked PAUSED — so a watching daemon does not race this live process
    with a duplicate ``--resume`` spawn.

    Args:
        flow: Current flow instance
        current_step: The discovery step
        persistence: Persistence manager
        prompt_history: Prompt history for readline
        project_root: Project root for mirroring the gate to a call file

    Returns:
        _PROGRAMMATIC_CONFIRM sentinel if user confirms,
        user's input string if they want to continue discovery,
        or None if cancelled (Ctrl+C/EOF).
    """
    # The confirmation panel was already displayed by the discovery handler
    # or _restore_discovery_display.
    if project_root is not None:
        _drain_pending_interjections(flow, project_root, persistence)

    def _tick() -> None:
        if project_root is not None:
            _drain_pending_interjections(flow, project_root, persistence)

    call_file = _maybe_write_discovery_call(flow, current_step, project_root)
    try:
        while True:
            source, user_input = _await_terminal_or_web(
                call_file,
                prompt_title="Discovery Confirmation",
                prompt_message="Type 1 to confirm and proceed, or type your questions/feedback to continue discovery (Ctrl+D or Esc+Enter to finish, Ctrl+C to cancel):",
                history=prompt_history,
                strip=False,
                tick_callback=_tick,
            )

            if source == _DISCOVERY_SRC_CANCEL:
                # None = Ctrl+C (interactive) or EOF/empty pipe (non-interactive).
                # NOTE: Intentional divergence — interactive empty input (Ctrl+D
                # on empty buffer) returns "" and loops with re-display.
                # Non-interactive empty input returns None because
                # sys.stdin.read() consumes all data at once; there is nothing
                # left to re-read, so pausing is the only safe behavior.
                persistence.save_flow(flow)
                render_full(
                    "Discovery paused. Flow state saved.\n"
                    "Resume with: se3 run --resume",
                    title="Paused",
                )
                return None

            # Strip trailing newlines — these are artifacts of the multiline
            # input UI (pressing Enter before Ctrl+D), not part of the user's
            # intended input. The spec's strict == "1" rule still rejects
            # " 1 ", "1.", "yes", " 1", etc.; only the exact "1" confirms.
            if user_input.rstrip('\n\r') == "1":
                current_step.inputs["programmatic_confirmed"] = True
                return _PROGRAMMATIC_CONFIRM

            if not user_input.strip():
                # Empty or whitespace-only input — no-op: re-display the cached
                # confirmation panel and keep waiting on the same call file.
                # (The web console never submits an empty confirm; this is the
                # terminal empty-buffer affordance.)
                from ..engine.steps.discovery import _display_discovery_message

                content = current_step.outputs.get("message", "")
                refined = (
                    current_step.outputs.get("refined_description")
                    or current_step.outputs.get("proposed_description")
                    or ""
                )
                raw_result_text = current_step.outputs.get("raw_result_text", "")

                # No round/cumulative usage passed: an empty-input redraw issues
                # no LLM call, so the per-round footer must not be rendered.
                _display_discovery_message(
                    content, refined, is_confirmation=True, raw_result_text=raw_result_text
                )
                continue

            # Non-empty, non-"1" input — continue discovery:
            # clear the programmatic confirm flag, use this input as the next
            # discovery user input directly (no separate prompt for questions)
            current_step.outputs.pop("awaiting_programmatic_confirm", None)
            get_console().print(
                "[dim]Captured input — continuing discovery with your feedback.[/dim]"
            )
            prefix = (
                _consume_paused_interjection_prefix(flow)
                if project_root is not None
                else ""
            )
            if prefix:
                persistence.save_flow(flow)
                return prefix + user_input
            return user_input
    finally:
        _cleanup_discovery_call(call_file)


# Sentinel returned by the non-interactive discovery pause handler when a call
# file has been written (or is still awaiting a response): the run loop must
# persist the flow and exit so the web "Respond to Flow" interaction can answer.
_DISCOVERY_AWAITING = object()


def _discovery_call_question(current_step: Any) -> str:
    """Build the human-readable prompt for a discovery *question* call file."""
    outputs = current_step.outputs
    # Question mode: surface the LLM's clarifying message and its questions.
    parts: List[str] = []
    message = outputs.get("message")
    if message:
        parts.append(str(message))
    questions = outputs.get("questions")
    if isinstance(questions, list) and questions:
        if parts:
            parts.append("")
        for i, question in enumerate(questions, 1):
            parts.append(f"{i}. {question}")
    if not parts:
        parts.append(
            "Discovery is exploring your requirements. Reply with details to "
            "help clarify what you want to build."
        )
    return "\n".join(parts)


def _write_discovery_call(
    flow: FlowInstance, current_step: Any, project_root: Path
) -> Path:
    """Write a ``se3/calls/`` call file for a non-interactive discovery pause.

    The call joins the unified human-call queue via the shared
    :func:`~se3.engine.interaction_calls.write_call` helper, so the daemon
    aggregator and web console render and route it like any other interaction.

    * A *confirmation* pause carries the
      :data:`~se3.engine.interaction_calls.CALL_KIND_DISCOVERY_CONFIRM` kind, a
      prompt with the ``输入 1 确认`` textual fallback + refined description,
      and a one-click confirm ``option`` (response ``"1"``) so the web console
      shows both a GUI confirm button and the textual hint.
    * A *question* pause is a plain ``call`` carrying the LLM's clarifying
      prompt.

    The owning ``flow_id`` / ``step_id`` are written into ``context`` so the
    aggregator's per-flow filter scopes the call to its flow; they are also
    mirrored as top-level fields for backward-compatible readers. The user's
    reply is consumed on the next resume.
    """
    from ..engine import interaction_calls
    from ..engine.steps.discovery import discovery_confirm_metadata

    is_confirmation = bool(current_step.outputs.get("awaiting_programmatic_confirm"))
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    call_id = f"discovery_{current_step.step_id}_{timestamp}"
    context: Dict[str, Any] = {
        "flow_id": flow.flow_id,
        "step_id": current_step.step_id,
        "step_type": "discovery",
    }

    if is_confirmation:
        refined = (
            current_step.outputs.get("refined_description")
            or current_step.outputs.get("proposed_description")
            or ""
        )
        prompt, options = discovery_confirm_metadata(str(refined))
        if refined:
            context["refined_description"] = str(refined)
        kind = interaction_calls.CALL_KIND_DISCOVERY_CONFIRM
    else:
        prompt = _discovery_call_question(current_step)
        options = []
        kind = interaction_calls.CALL_KIND_CALL

    return interaction_calls.write_call(
        interaction_calls.calls_dir_for(project_root),
        kind=kind,
        prompt=prompt,
        context=context,
        options=options,
        call_id=call_id,
        # Backward-compatible top-level fields some readers/tests still expect.
        step_id=current_step.step_id,
        flow_id=flow.flow_id,
    )


def _read_discovery_response(call_file: Path) -> Optional[str]:
    """Return the response text for a discovery call file, or ``None``.

    Supports both the daemon-written ``<stem>.response.json`` envelope (the
    answer nested under a ``response`` key) and a plain ``<stem>.response``
    sibling, mirroring the confirm-call response protocol.
    """
    for sibling in (
        call_file.parent / f"{call_file.stem}.response.json",
        call_file.parent / f"{call_file.stem}.response",
    ):
        if not sibling.exists():
            continue
        try:
            data = json.loads(sibling.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Failed to parse discovery response file: %s", sibling)
            continue
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            for key in ("response", "answer", "feedback", "text"):
                value = data.get(key)
                if isinstance(value, str):
                    return value
            if "response" in data:
                return str(data["response"])
        return str(data)
    return None


def _handle_discovery_pause_noninteractive(
    flow: FlowInstance,
    current_step: Any,
    persistence: PersistenceManager,
    project_root: Path,
) -> Any:
    """Handle a discovery pause without a terminal (daemon ``--output-format json``).

    Rather than blocking on a terminal read, the clarifying question is written
    to a ``se3/calls/`` call file and the flow pauses; the web answers via the
    existing call/response mechanism. On resume the response is consumed and
    fed back into discovery as the next user turn.

    Returns the user-response string, the :data:`_PROGRAMMATIC_CONFIRM`
    sentinel, or :data:`_DISCOVERY_AWAITING` when the flow must pause to wait
    for a web response.
    """
    # Drain any queued interjections regardless of whether a response has
    # landed yet. This is the non-interactive path's only opportunity to
    # consume an interjection before the flow exits PAUSED — the main run
    # loop's drain only fires on the next --resume.
    _drain_pending_interjections(flow, project_root, persistence)

    call_path = current_step.outputs.get("discovery_call_file")
    if call_path:
        call_file = Path(call_path)
        response = (
            _read_discovery_response(call_file) if call_file.exists() else None
        )
        if response is None:
            # The outstanding call has not been answered yet — keep waiting.
            return _DISCOVERY_AWAITING
        # Consume the answered call: drop the pointer and remove the call +
        # response files so the next round starts a fresh call.
        is_confirmation = bool(
            current_step.outputs.get("awaiting_programmatic_confirm")
        )
        current_step.outputs.pop("discovery_call_file", None)
        for path in (
            call_file,
            call_file.parent / f"{call_file.stem}.response.json",
            call_file.parent / f"{call_file.stem}.response",
        ):
            try:
                path.unlink()
            except OSError:
                pass
        if is_confirmation:
            if response.rstrip("\n\r") == "1":
                current_step.inputs["programmatic_confirmed"] = True
                return _PROGRAMMATIC_CONFIRM
            # Any other answer keeps refining the requirements.
            current_step.outputs.pop("awaiting_programmatic_confirm", None)
        # Prefix any buffered interjections onto the user reply before it
        # becomes the next LLM user message.
        prefix = _consume_paused_interjection_prefix(flow)
        if prefix:
            persistence.save_flow(flow)
            return prefix + response
        return response
    # No outstanding call — write one and pause for a web response.
    call_file = _write_discovery_call(flow, current_step, project_root)
    current_step.outputs["discovery_call_file"] = str(call_file)
    persistence.save_flow(flow)
    logger.info("Discovery paused for web response: wrote call file %s", call_file)
    return _DISCOVERY_AWAITING


def make_cli_confirm_handler(
    project_root: Path,
    flow_id: Optional[str] = None,
    step_id: Optional[str] = None,
    poll_interval: float = 0.5,
) -> Callable[[str, List[str], Callable[[], bool]], Optional[str]]:
    """Build an ``on_confirm`` callback for :meth:`ClaudeCodeRunner.run_with_monitor`.

    Thin re-export of :func:`se3.engine.interaction_calls.make_cli_confirm_handler`
    so existing call sites and tests can keep importing it from ``run``. The
    canonical implementation lives in the engine layer because it is wired
    into the flow's LLM execution path by :mod:`se3.engine.llm_caller`, which
    cannot import this command module without a circular dependency.
    """
    from ..engine.interaction_calls import (
        make_cli_confirm_handler as _make_cli_confirm_handler,
    )

    return _make_cli_confirm_handler(
        project_root,
        flow_id=flow_id,
        step_id=step_id,
        poll_interval=poll_interval,
    )


def _should_show_type(current_step_type: str, flow: FlowInstance) -> bool:
    """Check if task type should be displayed for the current step.

    Type should not be shown before analyze completes (when still pending).

    Args:
        current_step_type: The current step type being executed
        flow: The flow instance

    Returns:
        True if type should be shown, False otherwise
    """
    # Don't show type during analyze step or when type is still pending
    if current_step_type == StepType.ANALYZE.value:
        return False

    # Check if type is still pending
    if flow.state.is_type_pending():
        return False

    return True


def _get_display_task_type(flow: FlowInstance) -> Optional[str]:
    """Get the task type to display, or None if pending.

    Args:
        flow: The flow instance

    Returns:
        Type string for display, or None if pending
    """
    # Check for resolved type first
    resolved = flow.state.context.get("resolved_type")
    if resolved:
        return resolved

    # Check for explicit type
    explicit = flow.state.context.get("explicit_type")
    if explicit:
        return explicit

    # Check flow's task_type
    if flow.task_type:
        return flow.task_type

    # Still pending
    return None


def _resolve_main_lock_root(project_root: Path) -> Path:
    """Resolve the main-repository root that owns the main-worktree lock.

    The main-worktree mutex always lives at the *main repository's*
    ``se3/state/merge.lock`` (never inside a linked worktree). When
    ``project_root`` is itself a linked git worktree, resolve back to the main
    repo via :func:`config._resolve_main_repo_root`; otherwise ``project_root``
    is already the main repo and is returned unchanged. This guarantees a
    synchronous ``se3 run`` launched from inside a worktree still contends on
    the single project-wide lock.
    """
    from ..config import _resolve_main_repo_root

    main_root = _resolve_main_repo_root(project_root)
    return main_root if main_root is not None else project_root


def _ensure_main_lock_for_step(
    main_lock: Any,
    flow: Any,
    current_step: Any,
    project_root: Path,
    persistence: Any,
) -> None:
    """Lazily acquire the main-worktree mutex before a code-touching step.

    Implements the lock-aware deferred acquisition for a synchronous run, the
    engine side of the (1a)+(1b) lock-regression fix:

    1. **No-op** when there is no lock to take (``main_lock is None`` — a
       ``--worktree`` flow body runs lock-free), when this run already holds it
       (acquired on an earlier step), or when ``current_step`` is the DISCOVERY
       step. Discovery only clarifies requirements and never holds the global
       lock (1a), so a long, human-paused exploration cannot stall other
       synchronous runs / merges queued behind it.
    2. **Non-blocking probe**: ``acquire(blocking=False)``. When the lock is
       free this returns immediately and the run behaves identically to the
       pre-regression up-front acquire — no visible wait state is produced. A
       stale lock (dead holder PID) is reclaimed in place, also with no wait
       state.
    3. **On contention** (:class:`MergeLockBusy`): mark the flow
       ``waiting_for_lock=True`` and persist engine.json (status stays RUNNING,
       so the daemon sees a live, queued flow rather than a silent stall),
       append a streaming ``waiting_for_lock`` event to the step's jsonl so the
       web console surfaces it incrementally, then BLOCK on
       ``acquire(blocking=True)`` until the current holder releases. This is the
       (1b) general fallback: any lock wait is shown as running-and-waiting,
       never as the "已发布" pseudo-success that silently never started.

    After a successful acquisition (fast path, stale-reclaim, or post-block) any
    set ``waiting_for_lock`` flag is cleared and persisted — including a stale
    True left by a previously interrupted wait.
    """
    if main_lock is None or main_lock.held:
        return
    if current_step.step_type == StepType.DISCOVERY:
        return

    from .merge.merge_lock import MergeLockBusy, MergeLockStale

    acquired = False
    try:
        main_lock.acquire(blocking=False)
        acquired = True
    except MergeLockStale:
        # Holder PID is dead — reclaim the lock in place. No human-visible
        # wait is warranted because this resolves immediately.
        try:
            main_lock.acquire(blocking=False, break_stale=True)
            acquired = True
        except (MergeLockBusy, MergeLockStale):
            acquired = False
    except MergeLockBusy:
        acquired = False

    if not acquired:
        # Lock is genuinely held by another run/merge: surface a visible
        # running "waiting for lock" state BEFORE blocking so the flow never
        # appears to be a silent stall, then queue on the blocking acquire.
        flow.waiting_for_lock = True
        try:
            persistence.save_flow(flow)
        except Exception:
            logger.debug(
                "failed to persist waiting_for_lock=True for %s",
                flow.flow_id, exc_info=True,
            )
        try:
            from ..engine.chat_history import record_waiting_for_lock
            record_waiting_for_lock(
                project_root=project_root,
                flow_id=flow.flow_id,
                step_id=current_step.step_id,
                step_type=current_step.step_type.value,
            )
        except Exception:
            logger.debug(
                "failed to record waiting_for_lock event for %s",
                current_step.step_id, exc_info=True,
            )
        # Block until the holder releases. The kernel releases an flock when
        # the holding process exits, so a crashed holder cannot wedge this.
        main_lock.acquire(blocking=True)

    # Acquired (one of the paths above). Clear any waiting flag — covers both
    # the just-set flag and a stale True left by a previously interrupted wait.
    if flow.waiting_for_lock:
        flow.waiting_for_lock = False
        try:
            persistence.save_flow(flow)
        except Exception:
            logger.debug(
                "failed to persist waiting_for_lock=False for %s",
                flow.flow_id, exc_info=True,
            )


def run_flow(
    project_root: Path,
    flow_id: Optional[str] = None,
    task_description: Optional[str] = None,
    task_type: str = "pending",
    change_name: Optional[str] = None,
    is_worktree_mode: bool = False,
    prompt_history: Any = None,
    source_issue_id: Optional[str] = None,
    output_format: str = "cli",
    acquire_main_lock: bool = True,
    worktree_branch: Optional[str] = None,
    worktree_original_branch: Optional[str] = None,
) -> int:
    """Run a flow to completion.

    Args:
        project_root: Project root directory
        flow_id: Flow ID to resume (None for new flow)
        task_description: Task description for new flow
        task_type: Type of task (feature, bugfix, etc., or 'pending' to auto-detect)
        change_name: Optional change name
        is_worktree_mode: Whether this flow runs in worktree isolation mode
        source_issue_id: Optional issue ID that triggered this flow
        output_format: Outermost event-stream sink selection — ``"cli"`` hangs
            the Rich rendering :class:`CliSink` (default, byte-identical to the
            historical CLI output), ``"json"`` hangs the structured
            :class:`JsonSink` (NDJSON to stdout) for daemon consumption.
        acquire_main_lock: When ``True`` (the default for a synchronous
            ``se3 run``), acquire the project's main-worktree mutex
            (``MergeLock(main_repo).acquire(blocking=True)``) before running and
            hold it for the *entire* run, releasing it on every exit path. The
            lock always targets the main repository's ``se3/state/merge.lock``
            (resolved from a worktree via :func:`_resolve_main_lock_root`), so
            synchronous runs serialise against each other and against any
            ``se3 merge``. When ``False`` — the case for a ``--worktree`` run's
            isolated flow body — no lock is taken, so multiple worktree flow
            bodies execute concurrently and only contend at their trailing
            ``se3 merge`` step. The DAG implement-step isolation worktrees never
            call ``run_flow`` and so never participate in this lock.
        worktree_branch: For a new ``--worktree`` flow, the isolation branch
            name to record on the flow (``worktree_branch``); ignored on resume.
        worktree_original_branch: For a new ``--worktree`` flow, the branch the
            run was launched from / will merge back into; recorded on the flow
            (``worktree_original_branch``) so resume can drive the trailing
            merge. Ignored on resume.

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    # Initialize components before signal handler (for clean state on early exit)
    persistence = PersistenceManager(project_root)
    state_machine = StateMachine(project_root, persistence)

    # Register SIGINT handler for reliable Ctrl-C handling
    global _interrupt_requested
    _interrupt_requested = False
    old_sigint_handler = signal.signal(signal.SIGINT, _sigint_handler)

    # Build the project's main-worktree mutex for a synchronous run, but do NOT
    # acquire it up front. Acquisition is DEFERRED to just before the first
    # code-touching (non-discovery) step inside _run_flow_impl (see
    # _ensure_main_lock_for_step): the discovery step only clarifies
    # requirements and must not hold the global lock for the entire (possibly
    # long, human-paused) exploration — doing so would silently stall every
    # other synchronous run / merge queued behind it. Once acquired the lock is
    # held for the remainder of the run; the finally below releases it on every
    # exit path (release() is a no-op when it was never taken). When
    # ``acquire_main_lock`` is False — the case for a ``--worktree`` run's
    # isolated flow body — no lock object is created at all, so the body runs
    # lock-free.
    main_lock = None
    try:
        if acquire_main_lock:
            from .merge.merge_lock import MergeLock

            main_lock = MergeLock(_resolve_main_lock_root(project_root))

        return _run_flow_impl(
            project_root, flow_id, task_description, task_type, change_name,
            is_worktree_mode, persistence, state_machine, prompt_history,
            source_issue_id=source_issue_id,
            output_format=output_format,
            worktree_branch=worktree_branch,
            worktree_original_branch=worktree_original_branch,
            main_lock=main_lock,
        )
    finally:
        # Restore original signal handler
        signal.signal(signal.SIGINT, old_sigint_handler)
        # Ensure the pre-implement baseline subprocess never outlives the flow.
        # If the flow ended before IMPLEMENT was dispatched (analyze/plan/confirm
        # failure, or an Abort/Exit at a confirm pause), _ensure_baseline_ready
        # never ran, so the background full-suite pytest would otherwise be left
        # orphaned (a hung test would never exit). This covers every exit path
        # of _run_flow_impl: normal return, exception, and sys.exit/SystemExit.
        state_machine.cleanup_baseline_capture()
        # Release the main-worktree mutex (held for the whole synchronous run).
        if main_lock is not None:
            main_lock.release()


def _run_flow_impl(
    project_root: Path,
    flow_id: Optional[str],
    task_description: Optional[str],
    task_type: str,
    change_name: Optional[str],
    is_worktree_mode: bool,
    persistence: PersistenceManager,
    state_machine: StateMachine,
    prompt_history: Any = None,
    source_issue_id: Optional[str] = None,
    output_format: str = "cli",
    worktree_branch: Optional[str] = None,
    worktree_original_branch: Optional[str] = None,
    main_lock: Any = None,
) -> int:
    """Internal implementation of flow execution.

    ``main_lock`` is an unacquired :class:`MergeLock` for a synchronous run
    (``None`` for a ``--worktree`` flow body, which runs lock-free). It is
    acquired lazily by :func:`_ensure_main_lock_for_step` immediately before
    the first non-discovery step and held for the rest of the run; ``run_flow``
    releases it on every exit path.
    """
    # Register all step handlers
    for step_type, handler in STEP_HANDLERS.items():
        state_machine.register_handler(step_type, handler)

    # Build the unified event stream and hang the outermost sink. ``se3 run``
    # is caller-agnostic: it always emits the same structured event stream and
    # only the tail sink differs. ``cli`` hangs the Rich rendering CliSink
    # (byte-identical to the historical CLI output — flow-level events are a
    # no-op there and step output is rendered exactly as before); ``json``
    # hangs JsonSink, which writes NDJSON to stdout for daemon consumption.
    emitter = EventEmitter()
    if output_format == "json":
        emitter.subscribe(JsonSink())
    else:
        emitter.subscribe(CliSink())
    # Persist step lifecycle events into the per-step chat history jsonl so
    # the daemon's history reader forwards them to the web console — without
    # this, step_completed / step_failed events only land in CliSink's
    # terminal output or JsonSink's stdout (which the daemon spawner
    # redirects to a per-flow log file), and the running-flow console never
    # sees the structured outputs the frontend needs to render report cards.
    emitter.subscribe(HistorySink(project_root))

    # Load or create flow
    try:
        if flow_id:
            flow = persistence.load_flow()
            if not flow or flow.flow_id != flow_id:
                display_error(f"Flow '{flow_id}' not found")
                return 1

            # Detect and handle resume of a RUNNING or FAILED step
            current_step = flow.state.get_current_step()
            if current_step and current_step.status == StepStatus.RUNNING:
                # Step was interrupted - prepare for resumption with context
                current_step.status = StepStatus.PENDING
                current_step.inputs["resumed"] = True
                # Increment retry_count so LLMCaller picks up conversation history
                # from the interrupted run via _get_retry_context()
                current_step.inputs["retry_count"] = current_step.inputs.get("retry_count", 0) + 1
                current_step.retry_count = 0
                logger.info(f"Resuming interrupted step: {current_step.step_id} ({current_step.step_type.value})")
                persistence.save_flow(flow)
            elif current_step and current_step.status == StepStatus.FAILED:
                # Step failed - prepare for retry from breakpoint
                current_step.status = StepStatus.PENDING
                current_step.inputs["resumed"] = True
                # Increment retry_count so LLMCaller picks up conversation history (external_attempt)
                current_step.inputs["retry_count"] = current_step.inputs.get("retry_count", 0) + 1
                # Reset retry_count on the step model for fresh retry budget
                current_step.retry_count = 0
                flow.status = FlowStatus.RUNNING
                logger.info(f"Retrying failed step from breakpoint: {current_step.step_id} ({current_step.step_type.value})")
                persistence.save_flow(flow)

            # A flow persisted as PAUSED (e.g. a non-interactive discovery
            # pause awaiting a web response) is being actively resumed now —
            # flip it back to RUNNING so its on-disk status reflects reality
            # and the daemon does not mistake a live resume for a still-paused
            # flow needing another --resume.
            if flow.status == FlowStatus.PAUSED:
                flow.status = FlowStatus.RUNNING
                persistence.save_flow(flow)

            # Display flow info with full content
            content = [
                f"Resuming flow: {flow.flow_id}",
                f"Current step: {flow.state.current_step_id}",
                f"Task: {flow.task_description}",
            ]
            render_full("\n".join(content), title="Flow Info")
        else:
            if not task_description:
                display_error("Task description required for new flow")
                return 1

            flow = state_machine.create_flow(
                task_description=task_description,
                task_type=task_type,
                change_name=change_name,
                is_worktree_mode=is_worktree_mode,
            )

            # Set source issue ID if provided
            if source_issue_id:
                flow.source_issue_id = source_issue_id

            # Record the worktree-isolation metadata on a new --worktree flow so
            # it persists in the worktree's engine.json. This lets a later
            # `se3 run --resume` from the main repo discover the run, re-dispatch
            # it inside its worktree, and merge the right branch back.
            if is_worktree_mode:
                flow.worktree_path = str(project_root)
                if worktree_branch:
                    flow.worktree_branch = worktree_branch
                if worktree_original_branch:
                    flow.worktree_original_branch = worktree_original_branch

            # Store explicit_type if user provided --type flag
            explicit_type = bool(task_type and task_type != "pending")
            if explicit_type:
                flow.state.context["explicit_type"] = task_type

            # Persist engine.json eagerly when either an explicit --type was
            # given OR this is a worktree-mode flow. For worktree mode this
            # writes ``is_worktree_mode=True`` (plus ``worktree_path``) into the
            # worktree's engine.json *before* discovery's first LLM call produces
            # any history — closing the daemon observability blind spot where the
            # strict ``is_worktree_mode`` gate in
            # ``aggregator._active_worktree_run_roots`` would otherwise not yet
            # admit the worktree's live history at the discovery startup window.
            # A single save covers both cases, so an explicit-type worktree flow
            # is not double-written; resume never reaches this new-flow branch,
            # so the path stays idempotent for ``--resume``.
            if explicit_type or is_worktree_mode:
                persistence.save_flow(flow)

            # Display new flow info with full content
            content = [
                f"Created new flow: {flow.flow_id}",
                f"Task: {task_description}",
            ]

            # Only show type if explicitly provided (pending is auto-detect)
            if task_type and task_type != "pending":
                content.append(f"Type: {task_type} (user-specified)")
            else:
                content.append("Type: pending (will be determined by analyze)")

            if change_name:
                content.append(f"Change: {change_name}")
            render_full("\n".join(content), title="New Flow")

        # Initialize flow metadata and baseline commit (idempotent — safe for both
        # new and resumed flows).
        state_machine.init_flow(flow)
    except ConfigError as exc:
        display_error(str(exc))
        return 2

    # Emit FLOW_STARTED once the flow is created/loaded and initialized. The
    # human-facing "New Flow"/"Flow Info" panel was already rendered above by
    # the existing render_full() calls — CliSink no-ops this event so CLI
    # output is unchanged; JsonSink forwards it for the daemon.
    emitter.emit(new_event(
        EventType.FLOW_STARTED,
        flow_id=flow.flow_id,
        task_description=flow.task_description,
        task_type=flow.task_type,
        is_worktree_mode=is_worktree_mode,
    ))

    # Execute flow
    while flow.status not in (FlowStatus.COMPLETED, FlowStatus.FAILED):
        # Step boundary: consume any interjection requests the daemon queued
        # (delivered via MSG_INTERJECT_FLOW from the web console) and fold
        # them into the flow's user_interjections so every subsequently-built
        # step's task_description picks them up.
        _drain_pending_interjections(flow, project_root, persistence)

        current_step = flow.state.get_current_step()
        if not current_step:
            get_console().print("[dim]No current step — marking flow complete[/dim]")
            _complete_flow_via_fallback(flow)
            persistence.save_flow(flow)
            break

        # If the current step already finished (process crashed after the step
        # handler returned but before transition_to_next was saved), advance
        # without re-running the step.
        if current_step.status in (StepStatus.COMPLETED, StepStatus.PARTIAL):
            logger.info(
                f"Step {current_step.step_type.value} already {current_step.status.value}, "
                "advancing to next step without re-running"
            )
            # Emit the persisted terminal event before transitioning. When the
            # process crashed after run_step saved the terminal status but
            # before lines 2165-2177 emitted the event, the CLI and web history
            # would never see that step's usage or report card. Emit it here so
            # sinks can surface it.
            # Mapping: COMPLETED/PARTIAL → STEP_COMPLETED, FAILED → STEP_FAILED
            # (same as the normal flow at lines 2185-2197).
            #
            # Guard: only emit when the event was NOT already persisted by the
            # original process.  If the process crashed *after* HistorySink
            # appended the terminal event but *before* transition_to_next was
            # saved, re-emitting here would create a duplicate jsonl record,
            # causing the web session badge to double-count token usage and
            # producing duplicate report cards.
            from ..engine.chat_history import has_step_terminal_event
            if not has_step_terminal_event(
                flow.project_root, flow.flow_id, current_step.step_id
            ):
                emitter.emit(new_event(
                    EventType.STEP_COMPLETED,
                    flow_id=flow.flow_id,
                    step_id=current_step.step_id,
                    step_type=current_step.step_type.value,
                    step=current_step,
                ))
            else:
                logger.info(
                    "Terminal event already persisted for %s, skipping "
                    "re-emission on resume",
                    current_step.step_id,
                )
            state_machine.transition_to_next(flow)
            persistence.save_flow(flow)
            continue

        # REVISION_NEEDED saved but transition not yet applied: call transition_to_next
        # which routes to _transition_to_fix (TEST/VERIFY_SPEC) or _transition_to_revision
        # (CONFIRM), without re-running the step and without double-incrementing counters.
        if current_step.status == StepStatus.REVISION_NEEDED:
            logger.info(
                f"Step {current_step.step_type.value} already REVISION_NEEDED, "
                "applying transition without re-running"
            )
            get_console().print(
                f"[yellow]Resuming: revision was already requested from "
                f"{current_step.step_type.value}[/yellow]"
            )
            # Emit the persisted token_usage before transitioning. When the
            # process crashed after run_step saved REVISION_NEEDED + token_usage
            # but before STEP_OUTPUT was emitted, the CLI and web history would
            # never see that round's usage. Emit it here so sinks can surface it.
            #
            # Guard: only emit when the step_output was NOT already persisted
            # by the original process, to avoid duplicate records that would
            # double-count token usage in the web session badge.
            step_usage = (current_step.outputs or {}).get("token_usage")
            if step_usage:
                from ..engine.chat_history import has_step_output_event
                if not has_step_output_event(
                    flow.project_root, flow.flow_id, current_step.step_id
                ):
                    emitter.emit(new_event(
                        EventType.STEP_OUTPUT,
                        flow_id=flow.flow_id,
                        step_id=current_step.step_id,
                        step_type=current_step.step_type.value,
                        step=current_step,
                    ))
                else:
                    logger.info(
                        "step_output event already persisted for %s, "
                        "skipping re-emission on resume",
                        current_step.step_id,
                    )
            state_machine.transition_to_next(flow)
            persistence.save_flow(flow)
            continue

        # Lazily acquire the main-worktree mutex before the first code-touching
        # (non-discovery) step. Discovery steps run lock-free (1a); the first
        # non-discovery step (analyze for a normal run) acquires here and holds
        # the lock for the rest of the run. If the lock is contended this blocks
        # AND surfaces a visible running "waiting for lock" state (1b). A Ctrl+C
        # while queued exits cleanly to await --resume.
        try:
            _ensure_main_lock_for_step(
                main_lock, flow, current_step, project_root, persistence)
        except KeyboardInterrupt:
            # The flow is no longer actively queued for the lock once the
            # process exits, so clear waiting_for_lock before persisting.
            # Otherwise engine.json records status=running + waiting_for_lock=True
            # for a dead process, and the daemon/web console would keep rendering
            # it as a live "running · waiting for lock" flow until a manual
            # `se3 run --resume` re-acquires and clears the flag.
            flow.waiting_for_lock = False
            persistence.save_flow(flow)
            emitter.emit(new_event(
                EventType.FLOW_PAUSED, flow_id=flow.flow_id,
                step_id=current_step.step_id,
                step_type=current_step.step_type.value,
            ))
            get_console().print(
                "[yellow]Interrupted while waiting for the main-worktree "
                "lock — exiting (resume with `se3 run --resume`).[/yellow]"
            )
            return 130

        # Display compact step header — skip for CONFIRM steps (the prompt speaks for itself)
        step_type_value = current_step.step_type.value
        if current_step.step_type != StepType.CONFIRM:
            show_type = _should_show_type(step_type_value, flow)
            display_type = _get_display_task_type(flow) if show_type else None

            type_suffix = ""
            if display_type:
                type_suffix = f" [dim]({display_type})[/dim]"
            elif flow.state.is_type_pending():
                type_suffix = " [dim](pending)[/dim]"

            console = get_console()
            console.print(Rule(f"[bold]{step_type_value}[/bold]{type_suffix}", style="cyan"))

        step_start_time = datetime.now()

        # Track whether state_machine.run_step was actually invoked this
        # iteration.  When a PAUSED discovery step is resumed, run_step is
        # skipped and the previous round's stale token_usage stays in
        # step.outputs — emitting STEP_OUTPUT for that stale data would
        # duplicate the CLI usage block and append a zombie usage chip to
        # the web history.  Only emit STEP_OUTPUT when run_step was called
        # (meaning a fresh token-consuming LLM round actually happened).
        step_ran_llm = True

        # Emit STEP_STARTED for EVERY step type the moment it ACTUALLY enters
        # RUNNING — including the non-LLM TEST / COMMIT / SPEC_GATE steps and the
        # interactive CONFIRM / DISCOVERY steps. It is a no-op in CliSink (the
        # per-step renderer presents output only on completion), forwarded by
        # JsonSink, and persisted by HistorySink as a lightweight
        # ``step_started`` anchor so the web console shows the step's region
        # (with a "进行中" status) immediately rather than waiting for the
        # first conversation record or the final step_completed.
        #
        # The emit is wired as ``run_step``'s ``on_running`` callback rather than
        # fired here before the call: ``run_step`` invokes it only AFTER the step
        # is marked RUNNING and persisted (and after its pre-handler
        # preprocessing succeeds). A step with no registered handler fails before
        # that point, and a step whose baseline / spec-snapshot preprocessing
        # raises never reaches it either — so neither leaves a dangling "进行中"
        # anchor that would never be terminated. HistorySink additionally dedups
        # by step_id (has_step_started_event / has_step_terminal_event), so a
        # PAUSED step re-entered on resume never produces a duplicate region.
        def _emit_step_started(started_step: Any) -> None:
            emitter.emit(new_event(
                EventType.STEP_STARTED,
                flow_id=flow.flow_id,
                step_id=started_step.step_id,
                step_type=started_step.step_type.value,
            ))

        # Special handling for CONFIRM steps on resume - check for existing response
        if current_step.step_type == StepType.CONFIRM and flow_id and current_step.status == StepStatus.PAUSED:
            existing_result = _check_confirm_response(flow, current_step, project_root)
            if existing_result:
                get_console().print(f"[dim]Found existing confirmation response: {existing_result.value}[/dim]")
                result = existing_result
                # No LLM call — the user response was already on disk.
                step_ran_llm = False
            else:
                try:
                    result = state_machine.run_step(
                        flow, current_step, on_running=_emit_step_started)
                except KeyboardInterrupt:
                    result = _handle_step_interrupt(flow, current_step, persistence, prompt_history)
                    if result is None:
                        emitter.emit(new_event(
                            EventType.FLOW_PAUSED, flow_id=flow.flow_id,
                            step_id=current_step.step_id, step_type=step_type_value,
                        ))
                        return 130
                    continue

        # Special handling for DISCOVERY steps on resume - restore last AI message without
        # re-calling the LLM (the question was already asked; just wait for user input again)
        elif current_step.step_type == StepType.DISCOVERY and current_step.status == StepStatus.PAUSED:
            # Skip the Rich re-display for non-interactive (daemon) runs — the
            # NDJSON event stream is the only output channel there.
            if output_format != "json":
                _restore_discovery_display(current_step)
            result = StepStatus.PAUSED
            # No LLM call on resume — just re-displays the prior question.
            step_ran_llm = False

        else:
            try:
                result = state_machine.run_step(
                    flow, current_step, on_running=_emit_step_started)
            except KeyboardInterrupt:
                result = _handle_step_interrupt(flow, current_step, persistence, prompt_history)
                if result is None:
                    emitter.emit(new_event(
                        EventType.FLOW_PAUSED, flow_id=flow.flow_id,
                        step_id=current_step.step_id, step_type=step_type_value,
                    ))
                    return 130
                continue

        # Emit the step's terminal event for EVERY step type — including the
        # interactive CONFIRM/DISCOVERY steps and PLAN, which used to be
        # excluded here. Emitting it is what lets HistorySink persist the
        # step's structured outputs to the per-step jsonl (→ web report cards)
        # and JsonSink forward the event to the daemon; without it, a finished
        # discovery/plan/confirm/summarize step left the web console with no
        # final card to render. We only emit on a *terminal* result: a step
        # that returned PAUSED (DISCOVERY awaiting user input, CONFIRM awaiting
        # approval) or REVISION_NEEDED has not finished yet, so its terminal
        # event is deferred until a later re-run reaches COMPLETED / PARTIAL /
        # FAILED.
        #
        # CliSink deliberately skips rendering CONFIRM/DISCOVERY/PLAN (their CLI
        # output is owned by the orchestrator's interactive/special paths), so
        # this emit does NOT double-render the interactive steps on the CLI;
        # HistorySink and JsonSink still receive it. run.py itself no longer
        # imports render_step_output — rendering lives entirely in the sink.
        if result in (StepStatus.COMPLETED, StepStatus.PARTIAL, StepStatus.FAILED):
            step_event_type = (
                EventType.STEP_FAILED
                if result == StepStatus.FAILED
                else EventType.STEP_COMPLETED
            )
            emitter.emit(new_event(
                step_event_type,
                flow_id=flow.flow_id,
                step_id=current_step.step_id,
                step_type=step_type_value,
                step=current_step,
            ))

        # Non-terminal steps (PAUSED / REVISION_NEEDED / RETRYING) that
        # consumed tokens need their usage surfaced, but no terminal event
        # will ever be emitted for them — in the fix loop a REVISION_NEEDED
        # self_check/verify_spec/test is abandoned and a new step is created,
        # so its carried usage never reaches a terminal event. Emit a
        # STEP_OUTPUT event that carries the step's current token_usage so
        # CliSink can render the usage block and HistorySink can persist it
        # for the web console's session badge / per-step footnote.
        # Steps that will later reach a terminal status (e.g. discovery
        # PAUSED → re-run → COMPLETED) also emit STEP_OUTPUT here; the web
        # console's accumulateSessionUsage de-duplicates by preferring the
        # terminal record when both exist for the same step_id.
        #
        # ONLY emit STEP_OUTPUT when run_step was actually called this
        # iteration (step_ran_llm). When a PAUSED discovery step is resumed
        # without calling run_step, the prior round's stale token_usage is
        # still sitting in step.outputs — emitting STEP_OUTPUT for that stale
        # data would duplicate the CLI usage block and append a zombie usage
        # chip to the web history.
        elif result not in (StepStatus.COMPLETED, StepStatus.PARTIAL, StepStatus.FAILED) and step_ran_llm:
            # Discovery PAUSED is excluded: the discovery handler already renders
            # the per-round inline usage footer, and emitting STEP_OUTPUT here
            # would duplicate the cumulative usage on the CLI and persist a
            # redundant web usage chip.  The terminal COMPLETED event (emitted
            # when discovery finishes) carries the whole-discovery cumulative.
            if not (step_type_value == "discovery" and result == StepStatus.PAUSED):
                step_usage = (current_step.outputs or {}).get("token_usage")
                if step_usage:
                    emitter.emit(new_event(
                        EventType.STEP_OUTPUT,
                        flow_id=flow.flow_id,
                        step_id=current_step.step_id,
                        step_type=step_type_value,
                        step=current_step,
                    ))

        # Persist a non-terminal SETTLED status (PAUSED / RETRYING) so the web
        # console's step region reflects the step's real state instead of being
        # frozen on the stale "进行中" running anchor — most visibly the DISCOVERY
        # step, which shows "进行中" the instant it enters RUNNING and then
        # pauses awaiting user input with no terminal event and (by design) no
        # STEP_OUTPUT. The lightweight ``step_status`` jsonl line rides the same
        # history_data channel as ``step_started`` and the frontend renders it as
        # the region's current "已暂停" / "重试中" status row (superseding the
        # running anchor). The write is idempotent against the step's CURRENT
        # lifecycle state — not "this status appeared anywhere earlier" — so a
        # multi-round step (DISCOVERY running -> paused -> running -> paused, all
        # reusing one step_id) records the SECOND ``paused`` after the intervening
        # ``running`` re-arm, instead of suppressing it and leaving ``running`` as
        # the latest persisted status while the step is actually paused. Only a
        # back-to-back re-entry whose last lifecycle anchor is ALREADY this status
        # is skipped (no duplicate stacked row). ``get_step_history`` skips the
        # line so the CLI history / retry context never ingest it. Best-effort: a
        # write fault must never break the running flow.
        if result in (StepStatus.PAUSED, StepStatus.RETRYING):
            try:
                from ..engine.chat_history import (
                    last_step_lifecycle_status,
                    record_step_status,
                )
                _status_val = result.value
                if last_step_lifecycle_status(
                    project_root, flow.flow_id, current_step.step_id
                ) != _status_val:
                    record_step_status(
                        project_root=project_root,
                        flow_id=flow.flow_id,
                        step_id=current_step.step_id,
                        step_type=step_type_value,
                        status=_status_val,
                    )
            except Exception:
                logger.debug(
                    "failed to persist step_status for %s",
                    current_step.step_id, exc_info=True,
                )

        # Handle CONFIRM step PAUSED state - prompt user for approval
        if current_step.step_type == StepType.CONFIRM and result == StepStatus.PAUSED:
            # Defensive guard: LLM reviewer should never return PAUSED
            if current_step.inputs.get('reviewer') == 'llm':
                logger.warning(
                    "BUG: LLM reviewer returned PAUSED — this should not happen. "
                    "Skipping interactive prompt."
                )
                current_step.status = StepStatus.PENDING
                persistence.save_flow(flow)
                continue
            confirm_result = _handle_confirm_pause(flow, current_step, persistence, project_root, prompt_history)
            if confirm_result is None:
                # User chose to exit
                emitter.emit(new_event(
                    EventType.FLOW_PAUSED, flow_id=flow.flow_id,
                    step_id=current_step.step_id, step_type=step_type_value,
                ))
                return 130
            # Re-run confirm step to process the response
            current_step.status = StepStatus.PENDING
            persistence.save_flow(flow)
            continue

        # Handle discovery step PAUSED state - need user input to continue
        if current_step.step_type == StepType.DISCOVERY and result == StepStatus.PAUSED:
            if output_format == "json":
                # Non-interactive (daemon spawn): write the clarifying question
                # as a se3/calls/ call file and let the web answer it through
                # the existing "Respond to Flow" mechanism.
                user_response = _handle_discovery_pause_noninteractive(
                    flow, current_step, persistence, project_root
                )
                if user_response is _DISCOVERY_AWAITING:
                    # Call file written / still unanswered — pause and exit so
                    # the flow can be resumed once a web response arrives.
                    # Persist FlowStatus.PAUSED: this process is exiting while
                    # the flow still has work to do, and the daemon keys its
                    # resume decision off this on-disk status (only a PAUSED
                    # flow is re-spawned with --resume once the web answer is
                    # written).
                    flow.status = FlowStatus.PAUSED
                    persistence.save_flow(flow)
                    emitter.emit(new_event(
                        EventType.FLOW_PAUSED, flow_id=flow.flow_id,
                        step_id=current_step.step_id, step_type=step_type_value,
                    ))
                    return 0
            else:
                # Discovery is waiting for an interactive user response. The
                # pause is mirrored to a se3/calls/ call file so the web console
                # can answer it too; terminal + web are awaited in parallel and
                # whichever answers first drives this same live process. The
                # flow stays RUNNING (never PAUSED) so the daemon does not spawn
                # a duplicate --resume against the live interactive process.
                user_response = _handle_discovery_pause(
                    flow, current_step, persistence, prompt_history, project_root
                )

                if user_response is None:
                    # User chose to exit
                    emitter.emit(new_event(
                        EventType.FLOW_PAUSED, flow_id=flow.flow_id,
                        step_id=current_step.step_id, step_type=step_type_value,
                    ))
                    return 130

            # Store user response and re-run discovery step.
            # Advancing to the next discovery round is a NEW LLM call with a
            # new CONTINUE_DISCOVERY_PROMPT (containing the fresh
            # user_response and updated conversation history) — not a retry
            # of the previous round. Clear any stale retry counter left
            # behind by a prior FAILED-Retry or interrupted-resume so the
            # next round's prompt isn't discarded by LLMCaller's
            # retry-context wrapping.
            current_step.inputs["user_response"] = user_response
            current_step.inputs["resumed"] = True
            current_step.inputs.pop("retry_count", None)
            current_step.status = StepStatus.PENDING
            persistence.save_flow(flow)
            continue

        if result == StepStatus.FAILED:
            error_msg = current_step.error_message or "Unknown error"
            display_error(f"Step failed: {error_msg}")

            max_retries = 3
            if current_step.retry_count >= max_retries:
                display_error(
                    f"Max retries ({max_retries}) reached for step {current_step.step_type.value}"
                )
                # Auto-fail: exit without asking user (no FLOW_PAUSED, no
                # decision chip, no prompt — unchanged from the prior behavior).
                flow.status = FlowStatus.FAILED
                persistence.save_flow(flow)
                return 1

            # Dual-channel failure pause. _resolve_step_failure_action
            # unconditionally writes the retry_decision call file (so the web
            # console shows a Retry/Skip/Abort chip), then routes:
            #   * "decision" — an answer is already on disk (resume / webui);
            #   * "race"     — interactive: race the CLI prompt vs. the webui
            #                  response (whoever answers first wins);
            #   * "pause"    — non-interactive: pause for an out-of-band answer.
            action, info = _resolve_step_failure_action(
                project_root, flow, current_step, error_msg,
                interactive=_stdin_is_interactive(),
            )

            # The step has failed with retries remaining — surface a paused
            # state to webui/daemon regardless of channel, so a bystander sees
            # the failure (and the decision chip) rather than nothing.
            emitter.emit(new_event(
                EventType.FLOW_PAUSED, flow_id=flow.flow_id,
                step_id=current_step.step_id, step_type=step_type_value,
            ))

            if action == "pause":
                get_console().print(
                    "[yellow]Step failed with no interactive terminal — wrote "
                    f"a retry_decision call ({Path(info).name}). Pausing the "
                    "flow; respond via the web console or `se3 run --resume`."
                    "[/yellow]"
                )
                flow.status = FlowStatus.PAUSED
                persistence.save_flow(flow)
                return 0
            if action == "decision":
                choice = _failure_decision_to_choice(info)
            else:
                # Interactive terminal ("race"): race the CLI Retry/Skip/Abort
                # prompt against the webui retry_decision response. Whoever
                # answers first wins; the losing side is torn down by the race
                # helper (poller cancelled + artifacts cleaned), so the chip
                # vanishes and the answer is never consumed twice.
                options = ["Retry this step", "Skip to next step", "Abort flow"]
                _source, choice = _await_terminal_or_web_choice(
                    info, message="What would you like to do?", options=options,
                )
                if choice is None:
                    # Ctrl+C / EOF with no answer — treat as abort, matching the
                    # historical prompt_user_choice non-interactive default.
                    choice = len(options) - 1

            if choice == 0:
                # Reset step status and retry from where it left off
                current_step.status = StepStatus.PENDING
                current_step.inputs["resumed"] = True
                current_step.inputs["retry_count"] = current_step.inputs.get("retry_count", 0) + 1
                current_step.retry_count += 1
                persistence.save_flow(flow)
                continue
            elif choice == 1:
                # Force step to completed so transition works
                current_step.status = StepStatus.COMPLETED
                state_machine.transition_to_next(flow)
                persistence.save_flow(flow)
                continue
            else:
                flow.status = FlowStatus.FAILED
                persistence.save_flow(flow)
                return 1

        # Handle REVISION_NEEDED status from CONFIRM step
        if result == StepStatus.REVISION_NEEDED:
            get_console().print("[yellow]Revision requested — returning to previous step[/yellow]")
            # Mark the CONFIRM step as completed with revision info
            current_step.status = StepStatus.REVISION_NEEDED
            # Transition will handle going back to the previous step
            state_machine.transition_to_next(flow)
            persistence.save_flow(flow)
            continue

        step_duration = (datetime.now() - step_start_time).total_seconds()
        console = get_console()
        console.print(f"  [green]✓[/green] [bold]{current_step.step_type.value}[/bold] completed [dim]({step_duration:.1f}s)[/dim]")

        # Transition to next step
        state_machine.transition_to_next(flow)
        persistence.save_flow(flow)

    # Flow complete. Emit the terminal flow event (no-op in CliSink — the
    # human-facing summary line below is rendered as before; forwarded by
    # JsonSink for the daemon).
    if flow.status == FlowStatus.COMPLETED:
        emitter.emit(new_event(
            EventType.FLOW_COMPLETED, flow_id=flow.flow_id,
        ))
        display_success("Flow completed successfully!")
        # Session-level token/cost summary (sum of every step's usage). Renders
        # nothing when the flow consumed no LLM tokens.
        render_usage_block(flow.state.session_token_usage, title="Session Token Usage")
        return 0
    elif flow.status == FlowStatus.FAILED:
        current_step = flow.state.get_current_step()
        error_msg = current_step.error_message if current_step else "Unknown error"
        emitter.emit(new_event(
            EventType.FLOW_FAILED, flow_id=flow.flow_id, message=error_msg,
        ))
        display_error(f"Flow failed: {error_msg}")
        # Still surface whatever tokens/cost were consumed before the failure.
        render_usage_block(flow.state.session_token_usage, title="Session Token Usage")
        return 1
    else:
        get_console().print(f"[dim]Flow ended with status: {flow.status.value}[/dim]")
        return 0


def _slugify_for_branch(text: str) -> str:
    """Slugify a task description into a filesystem/branch-safe fragment.

    Lowercases, collapses any run of non-``[a-z0-9]`` characters into a single
    hyphen, strips leading/trailing hyphens, and truncates to 30 characters
    (re-stripping any trailing hyphen produced by truncation). An empty result
    falls back to ``"task"`` so the branch name is always valid.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower())
    slug = slug.strip("-")[:30].rstrip("-")
    return slug or "task"


def _generate_worktree_branch_name(task: str) -> str:
    """Build the isolation branch name for a ``--worktree`` run.

    Shape: ``worktree/<slug>-<timestamp>-<rand>``. The timestamp keeps the name
    human-readable/sortable, but its 1-second resolution does NOT guarantee
    uniqueness: two ``--worktree`` runs of the same task launched within the
    same wall-clock second would otherwise compute identical names and the
    second ``git branch`` would fail with "already exists", losing that run.
    A short random suffix (``uuid4`` hex) is therefore appended so concurrent
    runs — including of the same task in the same second — always get distinct
    branches and proceed in parallel. The ``worktree/`` prefix keeps these
    branches greppable and distinct from the implement step's internal
    ``impl/*`` DAG branches, and the suffix contains no slashes so the result
    still lands the worktree under ``se3/worktrees/<branch-safe-name>/`` via
    :func:`worktree.create_worktree`'s path rule.
    """
    slug = _slugify_for_branch(task)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    rand = uuid.uuid4().hex[:8]
    return f"worktree/{slug}-{timestamp}-{rand}"


def _worktree_flow_status(worktree_path: Path) -> Optional[str]:
    """Read the on-disk flow status from a worktree's ``engine.json``.

    Returns the persisted ``status`` string (e.g. ``"COMPLETED"`` / ``"PAUSED"``)
    or ``None`` when the file is missing / unreadable.
    """
    engine_file = worktree_path / SE3_DIR / "state" / "engine.json"
    try:
        with open(engine_file) as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError, OSError):
        return None
    return data.get("status")


def _finalize_worktree_merge(
    project_root: Path,
    worktree_branch: str,
    worktree_original_branch: Optional[str],
) -> int:
    """Merge a completed ``--worktree`` run's branch back into the main repo.

    Reuses the heavy ``se3 merge`` orchestrator (``run_merge``): version bump,
    postconditions, typed FailureReason, context-aware LLM conflict resolution,
    and — by default — ``--delete-merged`` cleanup that archives the worktree
    and deletes the isolation branch. ``run_merge`` itself acquires the
    main-worktree mutex (blocking), so this trailing merge serialises against
    synchronous runs and other merges. No extra diff-confirmation interaction is
    issued — a ``--worktree`` run is meant to be invisible to the user.

    Returns the merge exit code (0 on success).
    """
    from .merge_cmd import run_merge

    target = worktree_original_branch or "the original branch"
    get_console().print(
        Rule(f"[bold]worktree merge[/bold] [dim]→ {target}[/dim]", style="cyan")
    )
    render_full(
        f"Flow succeeded in isolation. Merging branch '{worktree_branch}' "
        f"back into '{target}'…",
        title="Worktree Merge",
    )
    rc = run_merge(branches=[worktree_branch], project_root=project_root)
    if rc == 0:
        display_success(f"Merged '{worktree_branch}' back into '{target}'.")
    else:
        display_error(
            f"Merge of '{worktree_branch}' failed (exit {rc}). The worktree and "
            f"branch are preserved; resolve and re-run `se3 merge {worktree_branch}`."
        )
    return rc


def run_worktree_mode(
    project_root: Path,
    task: str,
    task_type: str = "feature",
    change_name: Optional[str] = None,
    prompt_history: Any = None,
    output_format: str = "cli",
    source_issue_id: Optional[str] = None,
) -> int:
    """Run a flow in an isolated git worktree, then merge the result back.

    Thin orchestration wrapper for ``se3 run --worktree``:

    1. Generate an isolation branch name and fork a worktree from the current
       branch (``se3/worktrees/<branch-safe-name>/``).
    2. Run the *exact same* flow as a synchronous run, but with
       ``project_root=worktree`` and ``acquire_main_lock=False`` — so the flow
       body executes in isolation and does NOT hold the main-worktree mutex,
       letting multiple ``--worktree`` runs proceed concurrently. Step flow,
       persistence, ``--type`` and ``--resume`` are identical to a sync run.
    3. On success, merge the isolation branch back into the original branch via
       the heavy ``run_merge`` (which acquires the lock for that step only).
    4. On failure / interruption, preserve the worktree and branch and print a
       ``--resume`` hint — no merge is attempted.

    Returns the run's exit code (the merge's exit code when the flow succeeded).
    """
    from ..engine.worktree import fork_worktree, get_current_branch

    try:
        original_branch = get_current_branch(project_root)
    except RuntimeError as exc:
        display_error(f"Cannot start --worktree run: {exc}")
        return 1

    worktree_branch = _generate_worktree_branch_name(task)

    try:
        worktree_path = fork_worktree(project_root, original_branch, worktree_branch)
    except Exception as exc:  # noqa: BLE001 - surface any git failure cleanly
        display_error(f"Failed to create isolation worktree: {exc}")
        return 1

    # Topology changed (a worktree was added); drop any cached main-repo
    # resolution so later lookups reflect the new layout.
    clear_main_repo_root_cache()

    render_full(
        "\n".join(
            [
                "Started an isolated --worktree run.",
                f"  Branch: {worktree_branch}",
                f"  Worktree: {worktree_path}",
                f"  Merges back into: {original_branch}",
            ]
        ),
        title="Worktree Run",
    )

    # Run the flow inside the worktree. acquire_main_lock=False: the flow body
    # runs lock-free so concurrent --worktree runs do not serialise here; only
    # the trailing merge contends on the main-worktree mutex.
    exit_code = run_flow(
        project_root=worktree_path,
        task_description=task,
        task_type=task_type,
        change_name=change_name,
        is_worktree_mode=True,
        prompt_history=prompt_history,
        source_issue_id=source_issue_id,
        output_format=output_format,
        acquire_main_lock=False,
        worktree_branch=worktree_branch,
        worktree_original_branch=original_branch,
    )

    if exit_code != 0:
        # Failed / interrupted: preserve the worktree + branch for --resume and
        # do NOT merge. Mirrors a synchronous run that left state behind.
        render_full(
            "\n".join(
                [
                    f"Worktree run did not complete (exit {exit_code}).",
                    f"State preserved in worktree '{worktree_path}' "
                    f"(branch '{worktree_branch}').",
                    "Resume with: se3 run --resume",
                ]
            ),
            title="Worktree Run Paused",
        )
        return exit_code

    # A 0 exit code is ambiguous in --output-format json: a flow that PAUSED
    # for non-interactive input (e.g. a daemon-spawned --worktree --discover run
    # awaiting a web answer) also returns 0. Only a genuinely COMPLETED flow may
    # trigger the trailing merge — merging a paused flow would delete its branch
    # and archive its worktree (engine.json + call files), irrecoverably losing
    # the run that the web operator is about to answer. Preserve and exit.
    status = _worktree_flow_status(worktree_path)
    if status != FlowStatus.COMPLETED.value:
        render_full(
            "\n".join(
                [
                    f"Worktree run paused (status: {status or 'unknown'}); "
                    "no merge attempted.",
                    f"State preserved in worktree '{worktree_path}' "
                    f"(branch '{worktree_branch}').",
                    "It will be resumed once the pending input is answered.",
                ]
            ),
            title="Worktree Run Paused",
        )
        return exit_code

    # Success: merge the isolation branch back into the original branch.
    return _finalize_worktree_merge(
        project_root, worktree_branch, original_branch
    )


def find_resumable_worktree_runs(project_root: Path) -> List[Dict[str, Any]]:
    """Discover resumable ``--worktree`` runs under ``se3/worktrees/``.

    Each isolated ``--worktree`` run persists its flow state in its own
    ``se3/worktrees/<name>/se3/state/engine.json``. This scans those files and
    returns one entry per non-COMPLETED worktree flow so the resume picker can
    surface them alongside the main-repo flow. A successfully-merged run has had
    its worktree archived/removed by ``--delete-merged``, so only failed or
    interrupted runs remain to be found here.

    Returns a list of dicts shaped like :func:`find_existing_flows` entries plus
    ``worktree_path`` / ``worktree_branch`` / ``worktree_original_branch`` so the
    resume dispatcher can re-run the flow inside the worktree and merge it back.
    """
    runs: List[Dict[str, Any]] = []
    worktrees_dir = project_root / SE3_DIR / "worktrees"
    if not worktrees_dir.is_dir():
        return runs

    terminal_statuses = {FlowStatus.COMPLETED.value}
    for engine_file in sorted(worktrees_dir.glob("*/se3/state/engine.json")):
        try:
            with open(engine_file) as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError, OSError):
            continue

        if not data.get("is_worktree_mode"):
            continue
        if data.get("status", "unknown") in terminal_statuses:
            continue

        state_data = data.get("state", {})
        # Fall back to the engine.json location when the persisted worktree_path
        # is missing (older / partially-written state).
        worktree_path = data.get("worktree_path") or str(
            engine_file.parent.parent.parent
        )
        runs.append(
            {
                "id": data.get("flow_id", "unknown"),
                "status": data.get("status", "unknown"),
                "description": data.get("task_description", "No description"),
                "current_step": state_data.get("current_step_id"),
                "file": str(engine_file),
                "is_worktree_run": True,
                "worktree_path": worktree_path,
                "worktree_branch": data.get("worktree_branch"),
                "worktree_original_branch": data.get("worktree_original_branch"),
            }
        )
    return runs


def _find_worktree_run_by_flow_id(
    project_root: Path, flow_id: str
) -> Optional[Dict[str, Any]]:
    """Return the resumable worktree-run record for ``flow_id``, or ``None``."""
    for run in find_resumable_worktree_runs(project_root):
        if run.get("id") == flow_id:
            return run
    return None


def _self_worktree_run(
    project_root: Path, flow_id: str
) -> Optional[Dict[str, Any]]:
    """Return a worktree-run record when ``project_root`` *is* the worktree.

    The daemon resumes a ``--worktree`` run by relaunching ``se3 run --resume``
    with its ``cwd`` set to the worktree directory itself — that is where the
    run's ``engine.json`` / history live, where its WebUI call-responses are
    written, and what the daemon's resume validation reads. In that case the
    flow is not discoverable via :func:`find_resumable_worktree_runs` (which
    scans ``<main_repo>/se3/worktrees/``, one level up), so this reads the
    worktree's own ``engine.json`` and recognises it as a resumable worktree
    run. Returns ``None`` when ``project_root`` is not an ``is_worktree_mode``
    flow, the flow id does not match, or the flow is already COMPLETED.
    """
    engine_file = project_root / SE3_DIR / "state" / "engine.json"
    try:
        with open(engine_file) as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError, OSError):
        return None
    if not data.get("is_worktree_mode"):
        return None
    if str(data.get("flow_id")) != str(flow_id):
        return None
    if data.get("status", "unknown") == FlowStatus.COMPLETED.value:
        return None
    return {
        "id": data.get("flow_id", flow_id),
        "status": data.get("status", "unknown"),
        "description": data.get("task_description", "No description"),
        "is_worktree_run": True,
        "worktree_path": data.get("worktree_path") or str(project_root),
        "worktree_branch": data.get("worktree_branch"),
        "worktree_original_branch": data.get("worktree_original_branch"),
    }


def _resume_worktree_run(
    project_root: Path,
    run: Dict[str, Any],
    prompt_history: Any = None,
    output_format: str = "cli",
) -> int:
    """Resume a worktree run inside its worktree, then merge on success.

    The flow body is re-dispatched with ``project_root=<worktree>`` and
    ``acquire_main_lock=False`` (the flow body never holds the main-worktree
    mutex). On success the isolation branch is merged back via the heavy
    ``run_merge``; on failure the worktree/branch are preserved with no merge.
    """
    worktree_path = Path(run["worktree_path"])
    worktree_branch = run.get("worktree_branch")
    worktree_original_branch = run.get("worktree_original_branch")

    if not worktree_path.exists():
        display_error(
            f"Worktree path no longer exists: {worktree_path}. "
            "Cannot resume this worktree run."
        )
        return 1

    exit_code = run_flow(
        project_root=worktree_path,
        flow_id=run["id"],
        prompt_history=prompt_history,
        output_format=output_format,
        acquire_main_lock=False,
    )

    if exit_code != 0:
        render_full(
            "\n".join(
                [
                    f"Worktree run did not complete (exit {exit_code}).",
                    f"State preserved in worktree '{worktree_path}'.",
                    "Resume again with: se3 run --resume",
                ]
            ),
            title="Worktree Run Paused",
        )
        return exit_code

    # A 0 exit is ambiguous in json mode: a flow that PAUSED again for further
    # non-interactive input also returns 0. Only merge a genuinely COMPLETED
    # flow — merging a still-paused flow would archive its worktree and delete
    # its branch, losing the run mid-resume.
    status = _worktree_flow_status(worktree_path)
    if status != FlowStatus.COMPLETED.value:
        render_full(
            "\n".join(
                [
                    f"Worktree run paused again (status: {status or 'unknown'}); "
                    "no merge attempted.",
                    f"State preserved in worktree '{worktree_path}'.",
                    "It will be resumed once the pending input is answered.",
                ]
            ),
            title="Worktree Run Paused",
        )
        return exit_code

    if not worktree_branch:
        display_error(
            "Worktree run completed but no isolation branch was recorded; "
            "cannot merge automatically. Merge manually if needed."
        )
        return 1

    return _finalize_worktree_merge(
        project_root, worktree_branch, worktree_original_branch
    )


def resume_run(
    project_root: Path,
    flow_id: str,
    prompt_history: Any = None,
    output_format: str = "cli",
) -> int:
    """Dispatch a resume by flow id to the right path (worktree vs. main).

    If ``flow_id`` names a resumable ``--worktree`` run (discovered under
    ``se3/worktrees/``), it is resumed inside its worktree and merged back on
    success. When ``project_root`` *is itself* such a worktree — the shape the
    daemon uses when it relaunches ``se3 run --resume`` with ``cwd`` set to the
    worktree directory — the same lock-free body + merge-back path is taken,
    with the merge driven from the resolved main repo. Otherwise the main-repo
    flow is resumed in place (a synchronous run that acquires the main-worktree
    mutex for its whole duration).
    """
    worktree_run = _find_worktree_run_by_flow_id(project_root, flow_id)
    if worktree_run is not None:
        return _resume_worktree_run(
            project_root, worktree_run, prompt_history, output_format
        )
    self_run = _self_worktree_run(project_root, flow_id)
    if self_run is not None:
        # ``project_root`` is the worktree itself; the trailing merge must run
        # from the main repo (where the original branch is checked out), so
        # resolve it rather than merging from inside the worktree.
        main_root = _resolve_main_lock_root(project_root)
        return _resume_worktree_run(
            main_root, self_run, prompt_history, output_format
        )
    return run_flow(
        project_root=project_root,
        flow_id=flow_id,
        prompt_history=prompt_history,
        output_format=output_format,
    )


## CLI entry point is in cli.py (@app.command("run"))
## This module provides the logic functions: run_flow, etc.


if __name__ == "__main__":
    app()
