"""SE3 Run command — The unified entry point for SE3 3.0 flow engine.

Replaces start/work/done with a state machine-driven workflow that:
- Creates new flows or resumes interrupted ones
- Handles all step types programmatically

Usage:
    luo run "Implement feature X"              # New flow
    luo run --resume                           # Resume interrupted flow
    luo run "Fix bug" --type=bugfix            # Specify task type
"""

from __future__ import annotations
from tianluo.runtime_paths import dual_runtime_glob, runtime_dir

import json
import logging
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import typer
from rich.rule import Rule

# Add engine to path if needed
try:
    from ..engine.models import FlowInstance, FlowStatus, StepStatus, StepType
    from ..engine.persistence import PersistenceManager
    from ..engine.state_machine import StateMachine
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
        render_usage_summary_block,
    )
    from ..engine.event_stream import EventEmitter, EventType, new_event
    from ..engine.sink import CliSink, HistorySink, JsonSink
    from ..stop_signal import (
        STOP_REASON_INTERRUPT,
        InterjectionWatcher,
        get_stop_signal,
    )
    from ..i18n import t, t_status
    from ..cli import _read_multiline_input
except ImportError:
    # Direct import for development
    import sys
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from engine.models import FlowInstance, FlowStatus, StepStatus, StepType
    from engine.persistence import PersistenceManager
    from engine.state_machine import StateMachine
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
        render_usage_summary_block,
    )
    from engine.event_stream import EventEmitter, EventType, new_event
    from engine.sink import CliSink, HistorySink, JsonSink
    from stop_signal import (
        STOP_REASON_INTERRUPT,
        InterjectionWatcher,
        get_stop_signal,
    )
    from i18n import t, t_status
    from cli import _read_multiline_input


app = typer.Typer()
logger = logging.getLogger(__name__)

# Deprecated constant kept for importers; path resolution goes through
# runtime_dir() so legacy tianluo/ layouts keep working (removed in 13.0.0).
SE3_DIR = "se3"
STATE_FILE = "state/engine.json"

# Global flag set by SIGINT handler to detect interrupt requests
_interrupt_requested = False


def _sigint_handler(signum: int, frame: Any) -> None:
    """Route Ctrl-C to the cooperative stop signal, raising only when apt.

    Two different things have to happen depending on what the process is doing:

    * **An LLM subprocess is in flight.** Publishing on the stop signal is the
      ONLY correct move. Raising would unwind the runner's supervisor loop
      before it can wait for a stream message boundary and SIGINT the child's
      process group, which is what leaves the provider session resumable — and
      it would not reach a DAG group runner on a worker thread at all.
    * **The main thread is inside a non-interruptible section** (a multi-
      command git sequence like the DAG leaf merge-back). Publishing only:
      the section's exit raises the deferred interrupt itself
      (:func:`~tianluo.stop_signal.uninterruptible_scope`), so the sequence
      completes and the dialog opens at the next breakpoint instead of
      leaving a MERGE_HEAD the step then refuses to re-run.
    * **Nothing is in flight** (waiting on the merge lock, blocked on a
      terminal read). There is no child to wind down and the blocking call can
      only be broken by the exception, so raise as before.

    ``_interrupt_requested`` is kept for existing importers.
    """
    global _interrupt_requested
    _interrupt_requested = True
    signal_obj = get_stop_signal()
    signal_obj.request(reason=STOP_REASON_INTERRUPT)
    if signal_obj.llm_active or signal_obj.uninterruptible:
        return
    # Claim before raising: a watcher retry or a scope exit may be delivering
    # this same request concurrently, and the claim guarantees exactly one
    # KeyboardInterrupt per request no matter which channel wins the race.
    #
    # Losing the claim is not always a stand-down: the watcher's escalation
    # reaches the main thread THROUGH this handler (``interrupt_main`` only
    # trips SIGINT, it does not raise), so it claims the delivery and hands the
    # raise here. Consuming that handoff is what makes a web interjection cut a
    # non-LLM step short exactly as Ctrl-C does — the one path of decision 5.
    if (
        signal_obj.claim_interrupt_delivery()
        or signal_obj.consume_escalation_handoff()
    ):
        raise KeyboardInterrupt


def get_project_root() -> Path:
    """Find project root by looking for .git directory or an SE3 config file.

    When the current directory is inside a git worktree, this returns the
    worktree root (so SE3 state files remain isolated per-worktree).
    Config lookup via :func:`config.get_project_config_path` automatically
    ascends to the main repository when appropriate, so worktree-local
    ``tianluo.local.yaml`` still takes precedence and the main repo's
    ``tianluo.local.yaml`` can override the worktree's tracked ``tianluo.yaml``.

    Also binds the i18n language to the discovered root: this is the point at
    which the command settles on *which project* it operates on, and the root can
    sit above the cwd, so the import-time (cwd-resolved) language singleton must
    be re-resolved here for the project's ``language.language`` to take effect.
    """
    from ..config import is_se3_project_root
    from ..i18n import bind_project_root

    cwd = Path.cwd()
    root = cwd
    for parent in [cwd] + list(cwd.parents):
        # Check for .git directory, or any SE3 project marker
        # (tianluo.yaml, tianluo.local.yaml, se3.config.yaml).
        if (parent / ".git").exists() or is_se3_project_root(parent):
            root = parent
            break
    bind_project_root(root)
    return root


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
    # NOTE: the web frontend keeps a mirror of these token sets to decide
    # locally whether a free-text reply is an approval, a rejection, or an
    # unrecognized note needing a "this will be treated as a revision request"
    # confirmation (see G3). Keep the two lists in sync when editing either.
    approve_tokens = {
        "approve", "approved", "yes", "y", "ok", "okay", "lgtm",
        "accept", "accepted", "continue", "proceed", "pass", "skip",
        # Chinese approval words — the web console is operated in Chinese, so
        # an operator's first instinct is "同意"/"通过"/"批准" rather than an
        # English token. Without these they fell through to revision-request.
        "同意", "通过", "批准", "确认", "允许", "接受",
    }
    reject_tokens = {
        "no", "n", "reject", "rejected", "deny", "denied",
        "request changes", "changes", "revise", "revision",
        # Chinese rejection words — mirror of the approval additions above.
        "驳回", "拒绝", "打回", "否决", "不通过", "重做", "重拟",
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
    calls_dir = runtime_dir(project_root) / "calls"
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

                        # The gate resolved (web-answered). A dialog note parked
                        # at this pause is scoped to a failure gate's Retry and
                        # dies with any other resolution.
                        from ..engine.interjection_dialog import discard_gate_note

                        discard_gate_note(flow)

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


def _render_covered_surfaces(flow: FlowInstance, step_to_review_id: Optional[str]) -> None:
    """Print the ruling's boundary-clause coverage at an adjudicate confirm gate.

    ADJUDICATE has no dedicated CLI renderer, so its outputs would otherwise reach
    the approver through the generic key-value dump — where a nested list of
    ``{surface, justification}`` is effectively unreadable. Printing it explicitly
    here is what makes the human gate able to do its one job: catch a surface the
    sweep swept in *wrongly* before the boundary clause is written into the
    contract. Read from ``step.outputs`` — the same audit record the call file's
    display payload projects — never recomputed.
    """
    from rich.markup import escape

    try:
        from ..engine.context_builder import _display_covered_surfaces
    except ImportError:  # direct-import (development) path, as at module top
        from engine.context_builder import _display_covered_surfaces

    reviewed = flow.state.steps.get(step_to_review_id) if (flow.state and step_to_review_id) else None
    surfaces = _display_covered_surfaces(reviewed.outputs.get("covered_surfaces")) if reviewed else []

    if not surfaces:
        lines = [t("cli.run.confirm.adjudicate.covered_surfaces_none")]
    else:
        lines = []
        for entry in surfaces:
            lines.append(
                t("cli.run.confirm.adjudicate.surface_item", surface=escape(entry["surface"]))
            )
            lines.append(
                t(
                    "cli.run.confirm.adjudicate.justification_item",
                    justification=escape(entry["justification"]),
                )
            )

    render_full("\n".join(lines), title=t("cli.run.confirm.adjudicate.covered_surfaces_title"))


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
        True if the response was written, one of the ``_DIALOG_*`` outcome
        strings when an interjection dialog decided to restart or exit (the
        caller re-enters the run loop or leaves), or None if the user chose to
        exit through the menu.
    """
    # An interjection that arrived while this gate was waiting opens the same
    # mid-flow dialog Ctrl-C does — there is no subprocess to interrupt here,
    # but the operator's question ("why is it proposing this?") is exactly the
    # one the dialog exists to answer. Its interlocutor is the session of the
    # step whose output is being reviewed.
    pending = _collect_pending_dialog_messages(project_root)
    if pending:
        outcome = _dialog_at_pause_point(
            flow, current_step, persistence, project_root, prompt_history,
            initial_messages=pending, pause_context="confirm",
        )
        if outcome != _DIALOG_RESUME_PAUSE:
            return outcome

    step_to_review_id = current_step.inputs.get("step_to_review_id")
    step_to_review_type = current_step.inputs.get("step_to_review_type", "unknown")

    # The reviewed step's output was already displayed by render_step_output
    # in the previous iteration, so just prompt directly — except for an
    # adjudicate ruling, whose boundary-clause coverage the generic renderer
    # cannot show legibly (see _render_covered_surfaces).
    if step_to_review_type == "adjudicate":
        _render_covered_surfaces(flow, step_to_review_id)

    options = [
        t("cli.run.confirm.opt_approve"),
        t("cli.run.confirm.opt_request_changes"),
        t("cli.run.confirm.opt_exit"),
    ]
    try:
        # Raced against the interjection queue, not a plain blocking prompt: an
        # operator who interjects from the web AFTER this menu is on screen must
        # reach the dialog, and the pre-prompt drain above only catches what had
        # already arrived. There is no retry_decision-style call file at this
        # gate (the CONFIRM call has its own response shape), so the web-choice
        # channel is left unarmed and only the interjection one is polled.
        gate_interjections: List[str] = []
        source, choice = _await_terminal_or_web_choice(
            None,
            message=t("cli.run.confirm.review_prompt", step_type=step_to_review_type),
            options=options,
            interjection_sink=gate_interjections,
            project_root=project_root,
        )
        if source == _FAILURE_SRC_INTERJECT:
            outcome = _dialog_at_pause_point(
                flow, current_step, persistence, project_root, prompt_history,
                initial_messages=gate_interjections, pause_context="confirm",
            )
            if outcome != _DIALOG_RESUME_PAUSE:
                return outcome
            return _handle_confirm_pause(
                flow, current_step, persistence, project_root, prompt_history
            )
        if choice is None:
            raise KeyboardInterrupt
    except KeyboardInterrupt:
        # Ctrl-C AT the gate opens the dialog rather than exiting outright: the
        # operator interrupting a review is asking about the thing they are
        # reviewing, and the previous shape gave them nowhere to ask.
        outcome = _dialog_at_pause_point(
            flow, current_step, persistence, project_root, prompt_history,
            pause_context="confirm",
        )
        if outcome == _DIALOG_RESUME_PAUSE:
            return _handle_confirm_pause(
                flow, current_step, persistence, project_root, prompt_history
            )
        return outcome
    except EOFError:
        persistence.save_flow(flow)
        return None

    # The gate is resolving. A one-shot note parked by a dialog at this pause
    # has exactly one consumer — a failure gate's Retry — and every CONFIRM
    # resolution (approve, request changes, exit) is a different one, so the
    # note is dropped here rather than leaked into the step's next life.
    from ..engine.interjection_dialog import discard_gate_note

    discard_gate_note(flow)

    if choice == 2:
        # Exit
        persistence.save_flow(flow)
        return None

    approved = choice == 0
    feedback = None

    if not approved:
        # Get feedback from user
        feedback = _read_multiline_input(
            prompt_title=t("cli.run.confirm.feedback_title"),
            prompt_message=t("cli.run.confirm.feedback_message"),
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

    status_text = (
        t("cli.run.confirm.approved")
        if approved
        else t("cli.run.confirm.changes_requested", feedback=feedback)
    )
    render_full(status_text, title=t("cli.run.confirm.result_title"))

    return True


def find_existing_flows(project_root: Path) -> List[Dict[str, Any]]:
    """Find all existing flow state files."""
    flows = []
    se3_dir = runtime_dir(project_root)
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
                "description": data.get("task_description") or t("cli.common.no_description"),
                "current_step": state_data.get("current_step_id"),
                "file": state_file.name,
            })
    except (json.JSONDecodeError, IOError):
        pass

    return flows


def find_resumable_snapshot_flows(project_root: Path) -> List[Dict[str, Any]]:
    """Find resumable flows that exist only as per-flow snapshots.

    The single-slot engine.json is overwritten by the next ``luo run``, so a
    paused/interrupted/failed flow's recoverable state would otherwise be lost.
    :class:`PersistenceManager` mirrors every non-COMPLETED save into
    ``tianluo/state/resumable/<flow_id>.json``; this surfaces those snapshots in the
    resume picker shaped like :func:`find_existing_flows` entries. COMPLETED
    flows never have a snapshot (it is cleared on completion), so they cannot
    appear here.
    """
    flows: List[Dict[str, Any]] = []
    persistence = PersistenceManager(project_root)
    for flow in persistence.list_resumable_snapshots():
        if flow.status == FlowStatus.COMPLETED:
            continue
        flows.append({
            "id": flow.flow_id,
            "status": flow.status.value,
            "description": flow.task_description or t("cli.common.no_description"),
            "current_step": flow.state.current_step_id,
            "file": str(persistence.resumable_dir / f"{flow.flow_id}.json"),
        })
    return flows


def _read_choice_line(
    prompt: str, *, timeout: Optional[float] = None, echo_prompt: bool = True
) -> Any:
    """One line of the operator's answer to a menu.

    Off a TTY the read goes through the process-wide stdin funnel instead of
    ``input()``. WHY: the dual-channel waits also read a non-TTY stdin, and two
    independent readers on one pipe race for the same bytes — whichever the
    kernel hands the line to wins, so a menu answer could vanish into a reader
    nobody is listening to. The funnel is the single owner; ``input()`` stays
    only for a TTY, where prompt_toolkit-free line editing still matters.

    With *timeout* the funnel read is bounded and may return
    :data:`~tianluo.stdin_channel.PENDING`, which consumes nothing — that is
    what lets a caller poll another channel between slices and come back to the
    same unanswered menu. *echo_prompt* is then set False on the resumed
    slices so the prompt is written once, not once per slice.
    """
    if sys.stdin.isatty():
        return input(prompt)

    from ..stdin_channel import read_line

    if echo_prompt:
        print(prompt, end="", flush=True)
    line = read_line(timeout)
    if line is None:
        raise EOFError
    return line


def prompt_user_choice(
    message: str,
    options: List[str],
    *,
    poll: Optional[Callable[[], bool]] = None,
    poll_interval: float = 0.4,
) -> Optional[int]:
    """Prompt user to select an option.

    Returns the 0-based option index, or ``None`` when *poll* claimed the wait.

    WHY *poll* exists: off a TTY the funnel read returns only on a line or on
    EOF, and a launcher-held pipe may deliver neither for the life of the gate.
    A caller racing this menu against another channel (a web decision file, the
    interjection queue) therefore cannot check that channel only before and
    after the read — at the CONFIRM gate, which has no decision file to
    re-check afterwards, a web interjection arriving once the menu was on
    screen was never seen at all. With *poll* supplied the read is taken in
    bounded slices and *poll* runs between them; the first truthy result
    abandons the menu having consumed nothing from stdin.
    """
    from ..stdin_channel import PENDING

    print(f"\n{message}")
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")

    echo_prompt = True
    while True:
        try:
            if poll is not None and poll():
                return None
            line = _read_choice_line(
                t("cli.run.choice.select"),
                timeout=poll_interval if poll is not None else None,
                echo_prompt=echo_prompt,
            )
            if line is PENDING:
                echo_prompt = False
                continue
            echo_prompt = True
            choice = str(line).strip()
            idx = int(choice) - 1
            if 0 <= idx < len(options):
                return idx
            print(t("cli.run.choice.enter_between", n=len(options)))
        except ValueError:
            print(t("cli.run.choice.enter_valid"))
        except EOFError:
            # Handle non-interactive mode - default to last option (typically Abort)
            print(t("cli.run.choice.non_interactive", n=len(options), option=options[-1]))
            return len(options) - 1


def _stdin_is_interactive() -> bool:
    """Return whether the process has an interactive (TTY) stdin.

    Off a terminal (a daemon-spawned ``luo run --output-format json``, CI, a
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
#: A web interjection arrived while the gate was on screen. Decision 5 makes
#: that the same event as a Ctrl-C here: it opens the mid-flow dialog rather
#: than sitting on disk until the gate happens to be resolved some other way.
_FAILURE_SRC_INTERJECT = "interject"

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


def _peek_failure_gate_decision(project_root: Path, step_id: str) -> Optional[str]:
    """Peek at how *step_id*'s retry_decision gate was answered, if at all.

    Deliberately non-consuming: the answer still has to reach
    :func:`_resolve_step_failure_action`, which owns consuming it and tearing
    down the chip. This is only for the resume path, which needs to know what
    the answer *entitles* it to do before the step runs again.
    """
    from ..engine import interaction_calls

    return _read_failure_response_decision(
        interaction_calls.retry_decision_call_path(project_root, step_id)
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
    :data:`~tianluo.engine.interaction_calls.CALL_KIND_RETRY_DECISION` call file is
    written under ``tianluo/calls/`` so a webui bystander sees the failure as a
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
      the next ``luo run --resume`` (unchanged from the prior behavior).
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
    interjection_sink: Optional[List[str]] = None,
    project_root: Optional[Path] = None,
) -> Tuple[str, Optional[int]]:
    """Race a CLI choice against a webui answer AND an incoming interjection.

    The selection-mode sibling of :func:`_await_terminal_or_web`. Returns a
    ``(source, choice)`` tuple where *choice* is the 0-based option index:

    * ``(_FAILURE_SRC_WEB, idx)``      — a web response was already on disk or
      arrived first; the call file and both sibling responses are removed so
      it can never be read twice.
    * ``(_FAILURE_SRC_TERMINAL, idx)`` — the operator answered at the terminal;
      any concurrent webui call/response is best-effort torn down so the chip
      vanishes.
    * ``(_FAILURE_SRC_INTERJECT, None)`` — a web interjection arrived while the
      gate was displayed; its text is appended to *interjection_sink* and the
      caller opens the mid-flow dialog.
    * ``(_FAILURE_SRC_CANCEL, None)``  — Ctrl+C / EOF with no web answer; the
      caller treats this as abort.

    Passing *interjection_sink* + *project_root* arms the interjection channel;
    a gate with no web decision file (the CONFIRM gate) uses that alone. With
    neither channel — and with no TTY — it degrades to a plain
    :func:`prompt_user_choice`.
    """
    watch_interjections = interjection_sink is not None and project_root is not None
    sink_baseline = len(interjection_sink) if interjection_sink is not None else 0

    def _take_interjections() -> bool:
        if not watch_interjections:
            return False
        texts = _collect_pending_dialog_messages(project_root)
        if not texts:
            return False
        interjection_sink.extend(texts)
        return True

    def _park_stray_interjections() -> None:
        """Re-park interjections the poller consumed but the caller won't read.

        The caller only consults *interjection_sink* on
        :data:`_FAILURE_SRC_INTERJECT`; on every other outcome an interjection
        the poller sealed off disk in the same tick would simply vanish. Parking
        it hands it to the next dialog / wait instead.
        """
        if interjection_sink is None or len(interjection_sink) <= sink_baseline:
            return
        stray = interjection_sink[sink_baseline:]
        del interjection_sink[sink_baseline:]
        _defer_web_messages(stray)

    # No channel to race at all — terminal only (backward compatible).
    if call_file is None and not watch_interjections:
        return (_FAILURE_SRC_TERMINAL, prompt_user_choice(message, options))

    # Deterministic priority: an answer already waiting on disk wins outright.
    if call_file is not None:
        early = _read_failure_response_decision(call_file)
        if early is not None:
            _cleanup_retry_decision_artifacts(call_file)
            return (_FAILURE_SRC_WEB, _failure_decision_to_choice(early))
    if _take_interjections():
        return (_FAILURE_SRC_INTERJECT, None)

    # No interactive terminal to race against (piped stdin / no TTY): race the
    # bounded terminal read against the web channels, exactly as the TTY branch
    # and :func:`_await_terminal_or_web_non_tty` do. WHY it is a race and not
    # "block on the choice, then re-check once": with a pipe the launcher holds
    # open the funnel read returns only at EOF, so the post-read re-check can be
    # arbitrarily late — and at the CONFIRM gate, where ``call_file`` is None,
    # there is nothing to re-check at all, which left a web interjection queued
    # for the whole life of the menu.
    if not sys.stdin.isatty():
        raced_web: Dict[str, Optional[str]] = {"decision": None}

        def _poll_channels() -> bool:
            if call_file is not None:
                decision = _read_failure_response_decision(call_file)
                if decision is not None:
                    raced_web["decision"] = decision
                    return True
            try:
                return _take_interjections()
            except Exception:  # pragma: no cover - never break the gate
                logger.exception("Interjection poll at the gate failed")
                return False

        choice = prompt_user_choice(
            message, options, poll=_poll_channels, poll_interval=poll_interval
        )
        if choice is None:
            # A web channel won; nothing was consumed from stdin, so the
            # operator's next line still belongs to whoever asks for it next.
            raced = raced_web["decision"]
            if raced is not None:
                _cleanup_retry_decision_artifacts(call_file)
                return (_FAILURE_SRC_WEB, _failure_decision_to_choice(raced))
            return (_FAILURE_SRC_INTERJECT, None)
        if call_file is not None:
            late = _read_failure_response_decision(call_file)
            if late is not None:
                _cleanup_retry_decision_artifacts(call_file)
                return (_FAILURE_SRC_WEB, _failure_decision_to_choice(late))
            _cleanup_retry_decision_artifacts(call_file)
        _park_stray_interjections()
        return (_FAILURE_SRC_TERMINAL, choice)

    # Interactive TTY: race the prompt against a background poller.
    source, choice = _await_terminal_or_web_choice_interactive(
        call_file,
        message=message,
        options=options,
        poll_interval=poll_interval,
        take_interjections=_take_interjections if watch_interjections else None,
    )
    if source != _FAILURE_SRC_INTERJECT:
        _park_stray_interjections()
    return (source, choice)


def _await_terminal_or_web_choice_interactive(
    call_file: Optional[Path],
    *,
    message: str,
    options: List[str],
    poll_interval: float,
    take_interjections: Optional[Callable[[], bool]] = None,
) -> Tuple[str, Optional[int]]:
    """TTY dual-wait for a gate choice, raced against the web channels.

    Mirrors :func:`_await_terminal_or_web_interactive` but reads a numeric
    choice instead of free text. A daemon thread polls ``call_file`` for a
    sibling response and — when *take_interjections* is supplied — the
    interjection queue as well, cancelling the prompt the instant either
    appears (re-scheduling ``app.exit`` until the app is actually running to
    close the build-race window). Whichever side answers first wins; the loser
    is torn down without consuming anything twice. Any unexpected failure
    degrades to a plain :func:`prompt_user_choice`.

    WHY the interjection is polled here and not only before the prompt: a
    pre-prompt drain only catches what had already arrived. An operator who
    interjects from the web AFTER the Retry/Skip/Abort menu is on screen was
    otherwise ignored until the gate was resolved by some other route.
    """
    import asyncio
    import threading

    from prompt_toolkit import PromptSession
    from prompt_toolkit.patch_stdout import patch_stdout

    # Render the choice menu (same shape as prompt_user_choice).
    print(f"\n{message}")
    for i, opt in enumerate(options, 1):
        print(f"  {i}. {opt}")

    session = PromptSession(message=t("cli.run.choice.select"))
    web_sentinel = object()

    async def _race() -> Tuple[str, Optional[int]]:
        loop = asyncio.get_running_loop()
        stop = threading.Event()
        web_holder: Dict[str, Optional[str]] = {"value": None}
        interjected = threading.Event()

        def _cancel_prompt() -> None:
            app = session.app
            if app is not None and app.is_running:
                app.exit(result=web_sentinel)

        def _poll() -> None:
            while not stop.is_set():
                decision = (
                    _read_failure_response_decision(call_file)
                    if call_file is not None
                    else None
                )
                if decision is None and take_interjections is not None:
                    try:
                        if take_interjections():
                            interjected.set()
                    except Exception:  # pragma: no cover - never break the gate
                        logger.exception("Interjection poll at the gate failed")
                if decision is not None or interjected.is_set():
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
                    print(t("cli.run.choice.enter_between", n=len(options)))
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            stop.set()

        # Web won (sentinel) or the prompt was cancelled — a web answer may
        # also have landed during teardown.
        if web_holder["value"] is not None:
            if call_file is not None:
                _cleanup_retry_decision_artifacts(call_file)
            return (_FAILURE_SRC_WEB, _failure_decision_to_choice(web_holder["value"]))
        if interjected.is_set():
            # Left for the caller to resolve: the gate is not answered, it is
            # suspended while the dialog runs, and the artifacts must survive so
            # the chip is still answerable when the dialog says "continue".
            return (_FAILURE_SRC_INTERJECT, None)
        return (_FAILURE_SRC_CANCEL, None)

    try:
        source, choice = asyncio.run(_race())
    except KeyboardInterrupt:
        # Ctrl+C at the failure prompt is a CLI-side commitment to abort — tear
        # down the concurrent webui call/response so the (FAILED-exempt)
        # retry_decision chip does not keep surfacing on the now-aborted flow.
        if call_file is not None:
            _cleanup_retry_decision_artifacts(call_file)
        return (_FAILURE_SRC_CANCEL, None)
    except Exception:  # pragma: no cover - defensive: fall back to plain choice
        logger.exception(
            "Interactive failure dual-wait failed; using a plain choice prompt"
        )
        choice = prompt_user_choice(message, options)
        if call_file is not None:
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
    # _FAILURE_SRC_WEB already cleaned up inside _race(); an interjection
    # SUSPENDS the gate rather than answering it, so its artifacts stay.
    if source not in (_FAILURE_SRC_WEB, _FAILURE_SRC_INTERJECT) and call_file is not None:
        _cleanup_retry_decision_artifacts(call_file)
    return (source, choice)


def _reclaim_review_snapshots(flow: FlowInstance) -> None:
    """Reclaim a terminal flow's review baselines.

    INVARIANT: called only AFTER the terminal status is durably saved. This is
    the second landing of COMPLETED (the engine's own is in
    StateMachine.transition_to_next), and the same ordering rule holds: a save
    that raises leaves the flow persisted as resumable, and baselines destroyed
    first would leave a resumed SELF_CHECK round unable to reconstruct its own
    scope.

    INVARIANT: it additionally requires the flow's resumable snapshot to be
    CONFIRMED gone. ``save_flow`` retires it on the COMPLETED landing, but
    best-effort — a permission or I/O error there is swallowed so the primary
    engine.json write still lands — and a surviving snapshot keeps the flow
    resumable. Reclaiming its baselines anyway is the one half-clean state
    nothing can repair; keeping them only costs disk space.

    Total by contract: reclaiming disk space never fails a flow.
    """
    from ..engine.persistence import PersistenceManager
    from ..engine.review_scope import discard_flow_snapshots
    from ..engine.steps._project_root import resolve_flow_project_root

    try:
        flow_root = resolve_flow_project_root(flow)
        if PersistenceManager(flow_root).resumable_snapshot_exists(flow.flow_id):
            logger.warning(
                "Resumable snapshot for flow %s survived the completion clear; "
                "keeping its review baselines",
                flow.flow_id,
            )
            return
        discard_flow_snapshots(flow_root, flow.flow_id)
    except Exception:  # noqa: BLE001 - see docstring
        logger.debug(
            "Failed to reclaim review baselines for flow %s",
            getattr(flow, "flow_id", "?"),
            exc_info=True,
        )


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
    # Flows whose recoverable state survives only as a per-flow snapshot (their
    # engine.json was overwritten by a later run). These keep paused/interrupted
    # flows resumable from the picker even after a subsequent flow ran.
    snapshot_runs = find_resumable_snapshot_flows(project_root)

    # Filter to resumable flows (exclude only COMPLETED). worktree_runs are
    # already filtered to non-COMPLETED by find_resumable_worktree_runs, and
    # snapshot_runs by find_resumable_snapshot_flows.
    terminal_statuses = {FlowStatus.COMPLETED.value}
    active_flows = [f for f in flows if f["status"] not in terminal_statuses]
    active_flows = active_flows + worktree_runs
    # De-duplicate snapshot-only flows against the live engine.json / worktree
    # flows already listed (those are the authoritative live copy).
    seen_ids = {f.get("id") for f in active_flows}
    active_flows = active_flows + [
        f for f in snapshot_runs if f.get("id") not in seen_ids
    ]

    if not active_flows:
        if not flows and not worktree_runs:
            get_console().print(t("cli.run.resume.no_flows"))
        else:
            get_console().print(t("cli.run.resume.no_in_progress"))
            if flows:
                get_console().print(t("cli.run.resume.found_completed", count=len(flows)))
        return None

    if len(active_flows) == 1:
        flow = active_flows[0]
        is_failed = flow["status"] == FlowStatus.FAILED.value
        label = t("cli.run.resume.label_failed") if is_failed else t("cli.run.resume.label_interrupted")
        wt_suffix = t("cli.run.resume.worktree_suffix") if flow.get("is_worktree_run") else ""
        render_full(
            t(
                "cli.run.resume.single_body",
                label=label,
                flow_id=flow["id"],
                wt_suffix=wt_suffix,
                description=flow["description"],
                current_step=flow["current_step"],
            ),
            title=t("cli.run.resume.title"),
        )

        action = t("cli.run.resume.action_retry") if is_failed else t("cli.run.resume.action_resume")
        options = [action, t("cli.run.start_new_flow")]
        choice = prompt_user_choice(t("cli.run.what_to_do"), options)

        if choice == 0:
            return flow['id']
        return None

    # Multiple active flows
    content = [t("cli.run.resume.multi_header", count=len(active_flows)), ""]
    options = []
    for flow in active_flows:
        status_tag = t("cli.run.resume.tag_failed") if flow["status"] == FlowStatus.FAILED.value else ""
        wt_tag = t("cli.run.resume.tag_worktree") if flow.get("is_worktree_run") else ""
        options.append(
            t(
                "cli.run.resume.flow_option",
                description=flow["description"],
                current_step=flow["current_step"],
                wt_tag=wt_tag,
                status_tag=status_tag,
            )
        )
    start_new = t("cli.run.start_new_flow")
    options.append(start_new)

    for i, opt in enumerate(options[:-1], 1):
        content.append(f"  {i}. {opt}")
    content.append(f"  {len(options)}. {start_new}")

    render_full("\n".join(content), title=t("cli.run.resume.title"))
    choice = prompt_user_choice(t("cli.run.resume.which_flow"), options)

    if choice < len(active_flows):
        return active_flows[choice]["id"]
    return None


# --- Mid-flow interjection dialog -------------------------------------------
#
# An interjection is no longer a text box whose content is appended to the
# step's task description. It is a short read-only conversation held at the
# breakpoint — with the agent that was doing the work, inside its own provider
# session whenever that session is reachable — which settles into a structured
# decision the user confirms. Ctrl-C and a web-pushed interjection enter this
# same path (decision 5); the only difference is who published the stop signal.

#: Outcomes of a dialog, returned to the run loop.
#: How many times one non-interactive dialog round re-drains the calls
#: directory before publishing. Bounded so a script posting interjections in a
#: tight loop cannot keep the round from ever reaching the web console.
_MAX_DIALOG_DRAINS = 20

_DIALOG_CONTINUE_STEP = "continue_step"      # re-run the interrupted step
_DIALOG_RESUME_PAUSE = "resume_pause"        # go back to the pause point
_DIALOG_RESTARTED = "restarted"              # flow was rewound; re-enter loop
_DIALOG_EXIT = "exit"                        # save + leave
_DIALOG_AWAITING_WEB = "awaiting_web"        # json mode: paused for a web reply


def _dialog_rewind_targets(flow: FlowInstance) -> List[Dict[str, str]]:
    """Steps the operator may restart from, newest last (for the web console).

    Truncated at the CURRENT step: a rewind runs forward from its target, so a
    later history entry is not a restart target at all — offering one hands the
    operator a choice that ``resolve_rewind_target`` then rejects, or (before it
    did) silently left the steps in between un-run.
    """
    history = list(getattr(flow.state, "step_history", []) or [])
    current = getattr(flow.state, "current_step_id", None)
    if current and current in history:
        history = history[: history.index(str(current)) + 1]
    targets: List[Dict[str, str]] = []
    for sid in history:
        step = flow.state.steps.get(sid)
        if step is None:
            continue
        step_type = (
            step.step_type.value
            if hasattr(step.step_type, "value")
            else str(step.step_type)
        )
        targets.append({"step_id": sid, "step_type": step_type})
    return targets


def _reset_preview(flow: FlowInstance, decision: Any, project_root: Optional[Path]) -> Any:
    """Describe what a ``restart`` + ``workspace: reset`` would throw away.

    INVARIANT: the operator never confirms a reset blind. Returns ``None`` only
    when the decision is not a reset at all; every reset decision gets a
    preview object, and one that could not be taken comes back with
    ``ok=False`` so :func:`_confirm_and_apply_decision` withholds the
    confirmation. A swallowed failure used to render as "no preview" while the
    Apply affordance stayed live — i.e. exactly the blind discard this exists
    to prevent.
    """
    from ..engine.flow_workspace import ResetPreview
    from ..engine.interjection_dialog import ACTION_RESTART, WORKSPACE_RESET

    if decision is None:
        return None
    if decision.action != ACTION_RESTART or decision.workspace != WORKSPACE_RESET:
        return None
    if project_root is None:
        return ResetPreview(
            ok=False, error=t("cli.run.dialog.reset_preview_no_root")
        )
    try:
        from ..engine.flow_workspace import preview_reset

        return preview_reset(flow, Path(project_root))
    except Exception as exc:  # noqa: BLE001 - reported, never silently dropped
        logger.exception("Failed to build the workspace reset preview")
        return ResetPreview(ok=False, error=str(exc))


def _group_work_preview(
    flow: Optional[FlowInstance], decision: Any, project_root: Optional[Path]
) -> List[Dict[str, Any]]:
    """What a proposed ``restart`` would delete from the DAG group worktrees.

    INVARIANT: computed for BOTH ``keep`` and ``reset``. ``workspace: keep``
    only ever meant "leave the flow's own tree alone" — a rewind removes the
    group worktrees and leaf branches either way, and that work appears in
    neither the main tree's ``git status`` nor ``baseline..HEAD``. Without this
    the operator confirms a discard they were never shown.
    """
    from ..engine.interjection_dialog import ACTION_RESTART

    if flow is None or decision is None or project_root is None:
        return []
    if getattr(decision, "action", "") != ACTION_RESTART:
        return []
    try:
        from ..engine.flow_workspace import preview_group_work
        from ..engine.rewind import rewind_group_branches

        branches = rewind_group_branches(
            flow, decision.restart_step_id or None
        )
        if not branches:
            return []
        previews = preview_group_work(
            Path(project_root),
            branches,
            str(getattr(flow, "baseline_commit", "") or ""),
        )
        return [pv.to_dict() for pv in previews]
    except Exception:  # noqa: BLE001 - a blind spot, never a broken dialog
        logger.exception("Failed to preview the DAG group work a restart discards")
        return []


def _render_group_work(lines: List[str], group_work: List[Dict[str, Any]]) -> None:
    """Append the group-work discard summary to a decision panel."""
    if not group_work:
        return
    lines.append("")
    lines.append(t("cli.run.dialog.group_work_title"))
    for entry in group_work:
        lines.append(
            t(
                "cli.run.dialog.group_work_branch",
                branch=entry.get("branch", ""),
                worktree=entry.get("worktree_path", "") or "-",
            )
        )
        for commit in entry.get("commits") or []:
            lines.append(f"    {commit}")
        summary = (entry.get("status_summary") or "").strip()
        if summary:
            lines.append("    " + t("cli.run.dialog.group_work_uncommitted"))
            for line in summary.splitlines():
                lines.append(f"      {line}")


def _render_dialog_decision(
    decision: Any,
    flow: Optional[FlowInstance] = None,
    project_root: Optional[Path] = None,
) -> None:
    """Show a proposed decision so the operator can confirm or edit it."""
    lines = [
        t("cli.run.dialog.field_action", value=decision.action),
    ]
    if decision.action == "restart":
        lines.append(
            t(
                "cli.run.dialog.field_restart_step",
                value=decision.restart_step_id or t("cli.run.dialog.current_step"),
            )
        )
        lines.append(
            t("cli.run.dialog.field_workspace", value=decision.workspace)
        )
        if flow is not None:
            targets = _dialog_rewind_targets(flow)
            if targets:
                # The editor takes a bare step id, so the panel must say which
                # ids exist — a mistyped target otherwise fails only AFTER
                # confirmation.
                lines.append(
                    t(
                        "cli.run.dialog.rewind_targets",
                        targets=", ".join(
                            f"{tg['step_id']} ({tg['step_type']})"
                            for tg in targets
                        ),
                    )
                )
        preview = _reset_preview(flow, decision, project_root) if flow else None
        if preview is not None and not preview.ok:
            # Never render an unavailable preview as a clean tree: an empty
            # status panel is read as "nothing to lose", which is the opposite
            # of what a failed `git status` means.
            lines.append("")
            lines.append(
                t("cli.run.dialog.reset_preview_failed", error=preview.error)
            )
        elif preview is not None:
            lines.append("")
            lines.append(t("cli.run.dialog.reset_preview_title"))
            lines.append(
                preview.status_summary or t("cli.run.dialog.reset_preview_clean")
            )
            if preview.flow_commits:
                lines.append("")
                lines.append(t("cli.run.dialog.reset_preview_commits"))
                lines.extend(preview.flow_commits)
            if preview.snapshot_warning:
                lines.append("")
                lines.append(t("cli.run.dialog.reset_no_snapshot"))
        _render_group_work(
            lines, _group_work_preview(flow, decision, project_root)
        )
    if decision.instruction:
        lines.append(
            t("cli.run.dialog.field_instruction", value=decision.instruction)
        )
    if decision.revised_description:
        lines.append(
            t(
                "cli.run.dialog.field_revised",
                value=decision.revised_description,
            )
        )
    render_full("\n".join(lines), title=t("cli.run.dialog.decision_title"))


# Accepted edit keys → decision attribute. The keys include the LABELS the
# proposal panel renders ("restart from", "revised description"), because that
# is what an operator retypes when they edit a field at confirmation time; a
# label that is shown but rejected sends their edit to the LLM as a new message
# instead.
_DIALOG_EDIT_FIELDS = {
    "action": "action",
    "workspace": "workspace",
    "restart_step_id": "restart_step_id",
    "restart_from": "restart_step_id",
    "restart_step": "restart_step_id",
    "instruction": "instruction",
    "revised_description": "revised_description",
    "revised": "revised_description",
}


def _apply_decision_edits(decision: Any, text: str) -> bool:
    """Apply ``field: value`` edit lines to *decision*; report whether any stuck.

    Gives the terminal the same "confirm, but change a field first" affordance
    the web console gets from its structured reply. A message that is not a
    pure block of edit lines is left alone and continues the conversation
    instead — the user's prose must never be silently swallowed as an edit.
    """
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return False
    parsed: List[Tuple[str, str]] = []
    for line in lines:
        if ":" not in line and "=" not in line:
            return False
        sep = ":" if (":" in line and (":" < "=" or "=" not in line)) else "="
        if ":" in line and "=" in line:
            sep = ":" if line.index(":") < line.index("=") else "="
        key, value = line.split(sep, 1)
        key = key.strip().lower().replace(" ", "_")
        if key not in _DIALOG_EDIT_FIELDS:
            return False
        parsed.append((key, value.strip()))
    from ..engine.interjection_dialog import ACTIONS, WORKSPACES

    for key, value in parsed:
        if key == "action":
            if value.lower() not in ACTIONS:
                return False
            decision.action = value.lower()
        elif key == "workspace":
            if value.lower() not in WORKSPACES:
                return False
            decision.workspace = value.lower()
        else:
            setattr(decision, _DIALOG_EDIT_FIELDS[key], value)
    return True


def _decision_snapshot(decision: Any) -> Optional[Dict[str, Any]]:
    """A by-value snapshot of the decision currently on the table.

    Everything that identifies a published round is derived from VALUES, never
    from object identity: ``_apply_decision_edits`` rewrites a pending proposal
    IN PLACE, so the same object holds different fields one round later, and a
    bare web confirmation of the earlier round would be waved through.
    """
    if decision is None:
        return None
    try:
        return dict(decision.to_dict())
    except Exception:  # pragma: no cover - defensive: never break the dialog
        logger.exception("Failed to snapshot the pending dialog decision")
        # A snapshot that cannot be taken must not read as "unchanged": a value
        # that never compares equal — and so hashes to a fresh round id every
        # time — forces a fresh confirmation rather than applying a proposal
        # whose published form is unknown.
        return {"__unsnapshottable__": object()}


def _dialog_round_revision(decision: Any) -> str:
    """Publication id of the decision currently on the table (``""`` if none).

    Delegates to the same helper the call file publishes, so what the terminal
    compares against is byte-for-byte what a console client was shown.
    """
    from ..engine.interaction_calls import dialog_decision_revision

    return dialog_decision_revision(_decision_snapshot(decision))


def _record_published_round(
    rounds: List[Dict[str, Any]],
    revision: str,
    call_file: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Ledger the round just mirrored to the call file.

    WHY a ledger and not just "the last thing published": a bare confirmation
    ("apply what is shown") carries no fields, so binding it needs the set of
    rounds that could still have been on the answering client's screen. A
    republication of the SAME values collapses into the previous entry — it is
    the same round, and its publication instant must stay the one at which
    those values became current.

    WHY the publication instant is read back off the call file: the answer it
    is compared against is timed by the response file's mtime, so both ends
    have to come off the filesystem's own clock. Linux stamps file times from
    the COARSE clock — up to a jiffy behind ``time.time()`` — and on a project
    directory shared between machines the answer is landed by a host whose
    clock is not synchronised with this one at all; either gap can make a reply
    written after a round read as one written before it. Where the mirror
    failed there is no file, and hence no round on any console to be confirmed,
    so the wall clock stands in.
    """
    if not rounds or rounds[-1].get("revision") != revision:
        at = None
        if call_file is not None:
            try:
                at = call_file.stat().st_mtime
            except OSError:  # pragma: no cover - defensive
                at = None
        rounds.append({"revision": revision, "at": time.time() if at is None else at})
    return rounds


def _seed_published_rounds(
    prior_state: Optional[Dict[str, Any]], proposal: Any
) -> List[Dict[str, Any]]:
    """Recover the ledger of a dialog a previous process published rounds for.

    The round a resumed dialog inherits was put on the console by the process
    before it, so its publication is invisible here. Recorded at ``0.0`` when
    the ledger itself did not survive: an unknown publication instant must read
    as "could have been seen", never as "cannot have been".
    """
    if not prior_state:
        return []
    rounds = prior_state.get("published_rounds")
    if isinstance(rounds, list):
        return [
            {"revision": str(r.get("revision") or ""), "at": float(r.get("at") or 0.0)}
            for r in rounds
            if isinstance(r, dict)
        ]
    revision = _dialog_round_revision(proposal)
    return [{"revision": revision, "at": 0.0}] if revision else []


def _bare_confirmation_is_stale(
    rounds: List[Dict[str, Any]],
    current_revision: str,
    *,
    echoed_revision: str = "",
    responded_at: Optional[float] = None,
) -> bool:
    """Whether a fieldless confirmation can NO LONGER be applied as it stands.

    INVARIANT: a bare confirmation is bound to the round its author actually
    saw, never to whatever the flow happens to have published by the time the
    answer is read. The rounds share one call file, so the live proposal can
    already carry an edit made — at the terminal or from another client — after
    the round being confirmed was rendered; applying it then executes a
    decision nobody approved, and for ``restart``+``workspace: reset`` that
    discards the workspace on an approval never given.

    Two bindings, in order of strength:

    * the client echoed the round's ``decision_revision`` — exact, and the only
      one that also catches a console left open across a later round;
    * otherwise the answer's timestamp — rounds published after it provably
      were not on screen. What remains plausible must be a single round whose
      values are still the ones on the table; anything else is ambiguous, and
      ambiguity resolves as "confirm again", never as "apply".

    ``rounds`` is therefore never pruned by a refusal: once two different
    rounds have been published, a client that cannot echo an id is ambiguous
    for good — which is the point, since the answer it re-sends after a refusal
    is the very same one it had already composed against the round it was
    refused for. The echo is what re-opens the path.

    Rounds that proposed nothing are excluded: they carry no confirm
    affordance, so no confirmation can be an answer to one. With none plausible
    at all the confirmation cannot be a confirmation of anything — coherent
    only where nothing is on the table, where the caller reads the word as the
    operator's next message instead.
    """
    if echoed_revision:
        return echoed_revision != current_revision
    seen = {
        str(entry.get("revision") or "")
        for entry in rounds
        if entry.get("revision")
        and (responded_at is None or float(entry.get("at") or 0.0) <= responded_at)
    }
    if not seen:
        return bool(current_revision)
    return seen != {current_revision}


def _confirm_and_apply_decision(
    flow: FlowInstance,
    current_step: Any,
    decision: Any,
    dialog: Any,
    persistence: PersistenceManager,
    project_root: Path,
    *,
    pause_context: Optional[str],
) -> Tuple[Optional[str], str]:
    """Execute a confirmed decision and translate it into a loop outcome.

    Returns ``(outcome, error)``. ``outcome`` is ``None`` when the decision
    could NOT be applied (a bad rewind target, a refused rewind, a failed
    reset) — the confirmation then did not happen, and both front ends keep
    the operator in the dialog with the fields on the table, rather than
    silently executing a ``continue`` they never confirmed.
    """
    from ..engine.interjection_dialog import (
        ACTION_EXIT,
        ACTION_RESTART,
        apply_decision,
        summarize_transcript,
    )

    # INVARIANT: a reset is executed only after its preview has been
    # successfully taken AND shown. The decision confirmed here is the EDITED
    # one — a web operator can turn a proposed ``continue`` into
    # ``restart``+``reset`` — so the check cannot rely on whatever preview the
    # proposal happened to carry; it is re-taken and re-rendered for the exact
    # decision about to run.
    preview = _reset_preview(flow, decision, project_root)
    if preview is not None:
        if not preview.ok:
            error = t("cli.run.dialog.reset_preview_failed", error=preview.error)
            display_error(error)
            return None, error
        _render_dialog_decision(decision, flow, project_root)

    summary = summarize_transcript(dialog.transcript())
    outcome = apply_decision(
        flow, current_step, decision, project_root, dialog_summary=summary,
        # At a CONFIRM / failure gate ``continue`` means "resume waiting HERE",
        # so no step status or retry counter may move.
        continue_reenters_step=not pause_context,
    )

    # Rendered BEFORE the ok check: once a safety ref exists the tree has
    # already been changed, and a later failure (the snapshot replay, the
    # rewind) must not swallow the ref and the recovery command with it — that
    # is the only handle the operator has on the work just discarded.
    if outcome.reset is not None and outcome.reset.safe_ref:
        from ..engine.flow_workspace import describe_reset

        discarded, recovery = describe_reset(outcome.reset)
        render_full(
            t(
                "cli.run.dialog.reset_done",
                discarded=discarded or t("cli.run.dialog.reset_nothing_discarded"),
                ref=outcome.reset.safe_ref,
                recovery=recovery,
            ),
            title=t("cli.run.dialog.reset_title"),
        )
        if outcome.reset.warning:
            display_error(outcome.reset.warning)

    # Rendered BEFORE the ok check for the same reason as the reset panel: the
    # group worktrees are captured and removed while the rewind is planned, so
    # a restart that then fails on the workspace reset has still discarded them
    # — and these refs are the operator's only handle on what they held.
    if outcome.preserved_refs:
        get_console().print(
            t(
                "cli.run.dialog.group_work_saved",
                refs="\n".join(outcome.preserved_refs),
            )
        )

    # Same reason again: the groups this names have already lost their only
    # working copy, so the flow will re-run them on whatever happens next. That
    # is a change to what the flow is about to do, and a refusal that reported
    # only "nothing was applied" would hide it.
    if outcome.invalidated_group_steps:
        display_error(
            t(
                "cli.run.dialog.group_state_invalidated",
                steps=", ".join(outcome.invalidated_group_steps),
            )
        )

    if not outcome.ok:
        display_error(t("cli.run.dialog.apply_failed", error=outcome.error))
        # The flow object may already carry a recorded revision / a half-applied
        # reset, so persist before handing control back rather than losing it.
        persistence.save_flow(flow)
        return None, outcome.error

    persistence.save_flow(flow)

    if decision.action == ACTION_EXIT:
        render_full(
            t("cli.run.interrupt.saved_body"),
            title=t("cli.run.interrupt.exit_title"),
        )
        return _DIALOG_EXIT, ""
    if decision.action == ACTION_RESTART:
        rewound = outcome.rewind
        get_console().print(
            t(
                "cli.run.dialog.restarted",
                step_id=rewound.target_step_id if rewound else "",
                count=len(rewound.removed_step_ids) if rewound else 0,
            )
        )
        return _DIALOG_RESTARTED, ""
    # continue
    get_console().print(t("cli.run.dialog.continuing"))
    return (_DIALOG_RESUME_PAUSE if pause_context else _DIALOG_CONTINUE_STEP), ""


def _rearm_resumed_step(step: Any) -> None:
    """Arm an interrupted/failed step to run again as a retry.

    The single definition behind ``--resume``'s re-arm and the loop's
    fallback: ``resumed`` plus one ``retry_count`` increment is what makes
    LLMCaller treat the next attempt as a continuation (and consider a native
    resume of the interrupted session). Counting it twice would stamp the
    continuation records with an attempt number no attempt ever produced.
    """
    step.status = StepStatus.PENDING
    # The in-memory body is authoritative once fresh resume inputs are
    # assigned; see the resume block (issue #244 B3-i).
    step.cold_loaded = True
    inputs = step.inputs if step.inputs is not None else {}
    inputs["resumed"] = True
    try:
        inputs["retry_count"] = int(inputs.get("retry_count", 0) or 0) + 1
    except (TypeError, ValueError):
        inputs["retry_count"] = 1
    step.inputs = inputs
    step.retry_count = 0


def _collect_pending_dialog_messages(project_root: Optional[Path]) -> List[str]:
    """Drain interjection call files that arrived DURING the dialog.

    Decision 5: while a dialog is open a new interjection is simply the next
    thing the user said — it must not re-trigger an interruption on top of the
    interruption already in progress.

    Messages parked by a dual-wait that consumed them and then lost the race to
    a terminal answer come first: they are already off disk, so this drain is
    the only place left that can deliver them.
    """
    messages = _drain_deferred_web_messages()
    if project_root is None:
        return messages
    try:
        from ..engine import interaction_calls

        messages.extend(
            str(item.get("text") or "")
            for item in interaction_calls.drain_interjection_requests(project_root)
            if str(item.get("text") or "").strip()
        )
    except Exception:  # pragma: no cover - never break the dialog
        logger.exception("Failed to drain interjections during dialog")
    return messages


def _run_interjection_dialog(
    flow: FlowInstance,
    current_step: Any,
    persistence: PersistenceManager,
    project_root: Optional[Path],
    prompt_history: Any = None,
    *,
    initial_messages: Optional[List[str]] = None,
    pause_context: Optional[str] = None,
    call_step: Any = None,
    apply_step: Any = None,
    prior_state: Optional[Dict[str, Any]] = None,
) -> str:
    """Drive the interactive interjection dialog to a confirmed decision.

    ``pause_context`` names the pause point the dialog was opened from
    (``"confirm"`` / ``"failure"``, or ``"completed_step"`` when the request
    landed as the step was already finishing); it makes ``continue`` mean "go
    back to waiting there" instead of "re-run the step".

    ``call_step`` is the step the mirrored call file is ATTRIBUTED to, which at
    a pause point is the gate rather than *current_step* (the producer whose
    session the dialog talks to). The daemon drops any call whose step is no
    longer the flow's current one, so a call filed against the completed
    producer would never reach the web console.

    ``apply_step`` is the step a confirmed decision is APPLIED to, defaulting to
    *current_step*. It differs only when the interlocutor and the interrupted
    step are not the same one — a CONFIRM cut off mid-call talks to the
    producer's session but must itself be the step a ``continue`` re-runs.

    ``prior_state`` resumes a dialog a previous (daemon/json) process paused
    mid-round: the transcript and a pending proposal are restored so the
    terminal picks the conversation up where the web left it.

    An empty first message means "change nothing, resume now" and short-circuits
    without any LLM call — the cheapest and most common answer to an accidental
    Ctrl-C must not cost a round trip.
    """
    from ..engine.interjection_dialog import (
        DialogDecision,
        InterjectionDialog,
        find_dialog_session,
        parse_direct_decision,
    )

    root = Path(project_root) if project_root is not None else Path.cwd()
    call_step = call_step if call_step is not None else current_step
    prior_state = prior_state if isinstance(prior_state, dict) else None
    if apply_step is None and prior_state:
        apply_step = flow.state.steps.get(prior_state.get("apply_step_id") or "")
    apply_step = apply_step if apply_step is not None else current_step
    # WHY the key's PRESENCE and not its truthiness (same rule the
    # non-interactive driver applies): a persisted ``None`` is a SETTLED answer
    # — that dialog either found no session or watched the provider reject the
    # one it had, and fell back to the standalone conversation. Re-running the
    # lookup when the operator takes the paused dialog over at the terminal
    # would rediscover the rejected id from the earlier session-bearing record,
    # announce a same-session conversation that cannot happen, and probe the
    # dead session a second time. A prior binding that IS set is adopted only
    # if the dialog still accepts it — the agent may have been removed from the
    # chain while the flow was paused.
    if prior_state is not None and "binding" in prior_state:
        binding = prior_state.get("binding")
    else:
        binding = find_dialog_session(flow, current_step, root)
    dialog = InterjectionDialog(flow, current_step, root, binding=binding)
    if prior_state:
        # The prior process's turns are already in the step jsonl (it wrote
        # them as they happened); assigning the list directly — never via
        # record_user_turn — is what keeps them from being doubled.
        dialog.turns = list(prior_state.get("transcript") or [])

    # ``dialog.binding`` — not the raw lookup — is what the panel announces:
    # the dialog drops a binding whose agent is no longer configured or whose
    # runner cannot resume, and claiming a same-session conversation that will
    # not happen is worse than saying nothing.
    render_full(
        t(
            "cli.run.dialog.opened_same_session"
            if dialog.binding
            else "cli.run.dialog.opened_standalone",
            agent=(dialog.binding or {}).get("agent_name") or "-",
            step_type=dialog.context.current_step_type or "-",
        ),
        title=t("cli.run.dialog.title"),
    )

    pending: List[str] = [m for m in (initial_messages or []) if m is not None]
    proposal = (
        DialogDecision.from_dict(prior_state.get("decision"))
        if prior_state
        else None
    )
    # Holds a structured decision that arrived from the web mid-wait; the
    # dual-wait channel can only carry a string, so the payload is parked here
    # and the string is a sentinel. A bare confirmation additionally parks what
    # it can be BOUND to (see ``published_rounds``), so it is never applied to
    # a round its author never saw.
    web_decision: Dict[str, Any] = {}
    call_file: Optional[Path] = None
    # INVARIANT: every round mirrored to the console is ledgered here, and a
    # bare web confirmation is bound to the round it answered — never to the
    # live ``proposal``. The rounds share one call id, so the live object can
    # already carry a terminal edit made after the console rendered (and the
    # operator confirmed) an earlier round; reading that edit back as the thing
    # they approved would, for ``keep`` → ``reset``, discard the workspace on an
    # approval never given. Seeded from ``prior_state`` because the round a
    # resumed dialog inherits was published by the process before it.
    published_rounds: List[Dict[str, Any]] = _seed_published_rounds(
        prior_state, proposal
    )

    def _dialog_tick() -> Optional[str]:
        """Poll the web side: a dialog reply first, then queued interjections.

        A reply on the dialog call file wins over a queued interjection because
        it is an answer to the question actually on screen. A structured
        decision is parked in ``web_decision`` and reported as a sentinel, since
        the dual-wait can only hand back text.

        INVARIANT: the sentinel is a dialog-local control string and is never
        parked in the process-wide deferred queue — outside this dialog its
        payload is unreachable, so it would be handed to a later wait (or to the
        dialog LLM) as if the operator had typed a NUL string. A payload whose
        sentinel lost the dual-wait race to a terminal answer is therefore
        re-offered here instead: this dialog is the only place that can still
        deliver it, and :func:`_discard_unclaimed_web_decision` resolves what is
        left when the dialog ends.
        """
        if web_decision.get("value") is not None:
            return _DIALOG_WEB_DECISION
        if call_file is not None:
            reply = _read_dialog_call_reply(call_file)
            if reply is not None:
                text_reply = reply.get("text")
                # WHY ``"1"`` is routed through the decision channel too: the
                # call body offers it as "enter 1 to apply", so from the web it
                # is a bare confirmation like any other and must be bound to
                # the round it answered. Handed back as plain text it would
                # confirm whatever the terminal has since edited into place.
                bare_one = str(text_reply or "").strip() == "1"
                if "decision" in reply or "confirm" in reply or bare_one:
                    if bare_one and "decision" not in reply:
                        reply["confirm"] = True
                    web_decision["value"] = reply
                    # What this confirmation can be bound to: the round id it
                    # echoed, else when it was written. Captured here, at the
                    # instant the answer is taken off disk, because the round
                    # published afterwards is by definition not the one its
                    # author was looking at.
                    web_decision["revision"] = str(
                        reply.get("echoed_revision") or ""
                    )
                    web_decision["responded_at"] = reply.get("responded_at")
                    return _DIALOG_WEB_DECISION
                if text_reply is not None:
                    return str(text_reply)
        return _drain_interjection_as_reply(project_root)

    try:
        while True:
            if proposal is not None:
                _render_dialog_decision(proposal, flow, root)
            # WHY a reply already on disk is consumed BEFORE the round is
            # republished: the rounds share one call file, so republishing
            # first replaces the round the pending reply was written against.
            # The ledger below still binds a bare confirmation correctly, but
            # draining first keeps the common case out of the "ambiguous, ask
            # again" branch entirely.
            queued = _dialog_tick()

            # Mirror the round to a call file so the web console shows the same
            # conversation the terminal is holding, and can answer it. The call
            # id is stable per step, so refreshing never orphans an unread
            # reply — the drain above is what keeps it from being misattributed.
            call_file = _refresh_dialog_call(
                flow, call_step, dialog, proposal, project_root,
            )
            _record_published_round(
                published_rounds, _dialog_round_revision(proposal), call_file
            )

            if queued is None:
                # The first round has no call file until the refresh above, so
                # a reply left on disk by a previous process is picked up by
                # the tick (which understands a structured decision) rather
                # than by the dual-wait's generic early check, which would
                # flatten it to a string.
                queued = _dialog_tick()
            if queued is not None:
                pending.append(queued)

            if pending:
                text: Optional[str] = pending.pop(0)
            else:
                title = (
                    t("cli.run.dialog.confirm_title")
                    if proposal is not None
                    else t("cli.run.dialog.input_title")
                )
                message = (
                    t("cli.run.dialog.confirm_message")
                    if proposal is not None
                    else t("cli.run.dialog.input_message")
                )
                try:
                    _source, text = _await_terminal_or_web(
                        call_file,
                        prompt_title=title,
                        prompt_message=message,
                        history=prompt_history,
                        strip=False,
                        tick_callback=_dialog_tick,
                    )
                except KeyboardInterrupt:
                    text = None
                if text is None:
                    # Ctrl-C at the input box IS the exit decision (decision 3)
                    # — so it is routed through the very same apply path an
                    # explicit ``exit`` takes, not short-circuited. Saving and
                    # returning directly used to skip ``apply_decision``, and
                    # with it the discard of any one-shot gate note parked by an
                    # earlier ``continue`` at this same pause: the note then
                    # outlived the pause it was scoped to and was delivered to a
                    # later ``--resume``'s run.
                    from ..engine.interjection_dialog import ACTION_EXIT

                    outcome, _error = _confirm_and_apply_decision(
                        flow, apply_step, DialogDecision(action=ACTION_EXIT),
                        dialog, persistence, root, pause_context=pause_context,
                    )
                    if outcome is not None:
                        return outcome
                    persistence.save_flow(flow)
                    render_full(
                        t("cli.run.interrupt.saved_body"),
                        title=t("cli.run.interrupt.exit_title"),
                    )
                    return _DIALOG_EXIT

            stripped = (text or "").strip()

            if stripped == _DIALOG_WEB_DECISION:
                reply = web_decision.pop("value", {})
                echoed_revision = str(web_decision.pop("revision", "") or "")
                responded_at = web_decision.pop("responded_at", None)
                edited = (
                    DialogDecision.from_dict(reply["decision"], strict=True)
                    if isinstance(reply.get("decision"), dict)
                    else None
                )
                if isinstance(reply.get("decision"), dict) and edited is None:
                    # A user-edited decision with an invalid action/workspace is
                    # rejected, never coerced: executing it as ``continue``
                    # would run a decision the operator never made.
                    display_error(t("cli.run.dialog.invalid_decision"))
                    continue
                if edited is None and _bare_confirmation_is_stale(
                    published_rounds,
                    _dialog_round_revision(proposal),
                    echoed_revision=echoed_revision,
                    responded_at=responded_at,
                ):
                    # A bare confirmation carries no fields of its own — it can
                    # only mean "apply what is on the table". This one cannot be
                    # bound to the round now on the table (see
                    # :func:`_bare_confirmation_is_stale`), so applying it would
                    # execute a decision its author never saw. The current round
                    # is re-published for a fresh confirmation instead. Nothing
                    # is lost by dropping it: a bare confirmation's ``text`` is
                    # the one-click marker ("confirm" / "1"), never prose the
                    # operator typed.
                    #
                    # INVARIANT: the ledger is NOT pruned to the round being
                    # republished. Doing that made the very next fieldless
                    # confirmation valid by construction — including the blind
                    # retry of the one just refused, sent by a client that never
                    # fetched the new round — and that is how an approval of
                    # ``keep`` executed a workspace ``reset``. What re-opens the
                    # path is evidence the client HAS seen the republished
                    # round: the round id echoed back (or an answer written
                    # while no other round was ever on the table), never the
                    # bare word again.
                    display_error(t("cli.run.dialog.web_decision_superseded"))
                    continue
                if edited is None and proposal is None:
                    # A bare "confirm" with nothing on the table is not a
                    # confirmation of anything — it is the operator's next
                    # message, and applying an empty decision here would resume
                    # the flow behind their back.
                    text = str(reply.get("text") or "")
                    stripped = text.strip()
                    if not stripped:
                        continue
                elif reply.get("preview") and edited is not None:
                    # The operator edited the proposal into one that would
                    # discard the workspace. Adopt the fields and loop: the next
                    # turn renders — and re-mirrors to the web console — a
                    # preview built for THIS decision, so their Apply is
                    # informed rather than blind.
                    proposal = edited
                    continue
                else:
                    decided = edited or proposal or DialogDecision()
                    outcome, _error = _confirm_and_apply_decision(
                        flow, apply_step, decided, dialog, persistence, root,
                        pause_context=pause_context,
                    )
                    if outcome is None:
                        # The confirmation failed to apply (bad rewind target,
                        # refused reset) — the decision stays on the table for
                        # correction instead of degrading into a continue.
                        proposal = decided
                        continue
                    return outcome

            if not stripped:
                # INVARIANT: an empty line means "change nothing, continue
                # immediately" at EVERY point of the dialog, in both front
                # ends. It is never a confirmation of a pending proposal — the
                # operator who just wants to resume must not be able to trigger
                # a restart + workspace reset by pressing Enter — and it never
                # spends an LLM round.
                get_console().print(t("cli.run.interrupt.retry_as_is"))
                outcome, _error = _confirm_and_apply_decision(
                    flow, apply_step, DialogDecision(), dialog, persistence,
                    root, pause_context=pause_context,
                )
                if outcome is None:
                    continue
                return outcome

            if proposal is not None:
                if stripped == "1":
                    outcome, _error = _confirm_and_apply_decision(
                        flow, apply_step, proposal, dialog, persistence, root,
                        pause_context=pause_context,
                    )
                    if outcome is None:
                        continue  # the proposal stays on the table
                    return outcome
                if _apply_decision_edits(proposal, stripped):
                    get_console().print(t("cli.run.dialog.edits_applied"))
                    continue
                proposal = None  # falls through: the next dialog message

            direct = parse_direct_decision(stripped)
            if direct is not None:
                dialog.record_user_turn(stripped)
                proposal = direct
                continue

            try:
                turn = dialog.ask(stripped)
            except KeyboardInterrupt:
                # Ctrl-C WHILE the agent is replying cancels this round only
                # and returns to the input box; the conversation is not lost.
                get_stop_signal().clear()
                get_console().print(t("cli.run.dialog.round_cancelled"))
                continue
            except Exception as exc:  # noqa: BLE001
                logger.exception("Interjection dialog round failed")
                display_error(t("cli.run.dialog.round_failed", error=str(exc)))
                continue

            if turn.content:
                render_full(turn.content, title=t("cli.run.dialog.reply_title"))
            proposal = turn.decision if turn.is_decision else None
    finally:
        _discard_unclaimed_web_decision(web_decision)
        _cleanup_discovery_call(call_file)


def _discard_unclaimed_web_decision(web_decision: Dict[str, Any]) -> None:
    """Resolve a web payload the dialog consumed off disk but never claimed.

    Reached when the terminal side answered in the same poll tick that consumed
    a structured reply AND that answer resolved the dialog: the sentinel is
    dialog-local, so once the dialog returns there is nothing left that could
    apply the decision — it lost the race outright and is dropped rather than
    leaking into a later wait. Dropping loses no operator prose: the payload is
    either a structured decision (no free text at all) or a one-click
    confirmation whose ``text`` is the literal marker word, and both are answers
    to a question this dialog no longer asks. The operator is told, so the
    silence is not mistaken for the decision having been applied.
    """
    reply = web_decision.pop("value", None)
    web_decision.pop("revision", None)
    web_decision.pop("responded_at", None)
    if not isinstance(reply, dict):
        return
    logger.info("Web dialog decision arrived after the dialog was resolved; dropped")
    try:
        get_console().print(t("cli.run.dialog.web_decision_dropped"))
    except Exception:  # pragma: no cover - never break the dialog teardown
        logger.exception("Failed to report the dropped web dialog decision")


#: Sentinel the dual-wait hands back when a STRUCTURED decision arrived from the
#: web. The channel can only carry a string, so the payload is parked alongside.
_DIALOG_WEB_DECISION = "\x00tianluo-dialog-decision"


def _read_dialog_call_reply(call_file: Optional[Path]) -> Optional[Dict[str, Any]]:
    """Consume a reply to the mirrored dialog call file, if one has landed.

    Consuming (rather than peeking) matters: the same file is re-used for every
    round of the conversation, so a reply left on disk would be re-read as the
    next round's answer.
    """
    if call_file is None:
        return None
    try:
        from ..engine import interaction_calls

        # Read BEFORE the answer is consumed: the binding lives on the response
        # file itself (when it was written, which round id it echoed), and that
        # file is gone the moment the reply has been taken.
        binding = interaction_calls.dialog_response_binding(call_file)
        reply = interaction_calls.read_dialog_response(call_file)
    except Exception:  # pragma: no cover - never break the dialog
        logger.exception("Failed to read the dialog call reply")
        return None
    if reply is not None:
        reply["echoed_revision"] = binding.get("decision_revision") or ""
        reply["responded_at"] = binding.get("responded_at")
        _cleanup_discovery_response(call_file)
    return reply


def _reset_preview_payload(
    flow: FlowInstance, proposal: Any, project_root: Optional[Path]
) -> Optional[Dict[str, Any]]:
    """The reset preview as a JSON payload for the web console's dialog card."""
    preview = _reset_preview(flow, proposal, project_root)
    if preview is None:
        return None
    return {
        "status_summary": preview.status_summary,
        "flow_commits": list(preview.flow_commits),
        "baseline_commit": preview.baseline_commit,
        "has_dirty_snapshot": preview.has_dirty_snapshot,
        "snapshot_warning": preview.snapshot_warning,
        # The web console gates its Apply button on this: a preview that could
        # not be taken must not read as an empty (clean) one.
        "ok": bool(preview.ok),
        "error": preview.error,
    }


def _refresh_dialog_call(
    flow: FlowInstance,
    call_step: Any,
    dialog: Any,
    proposal: Any,
    project_root: Optional[Path],
) -> Optional[Path]:
    """Mirror the current dialog round to a ``tianluo/calls/`` call file.

    The same reason ``_maybe_write_discovery_call`` exists: an interaction the
    terminal is blocking on should be visible — and answerable — from the web
    console too. Returns ``None`` when there is no project root or the write
    fails, in which case the round degrades to a terminal-only wait.

    ``call_step`` is the flow's CURRENT step, which at a pause point is the
    gate and not the dialog's subject: the daemon filters out any pending call
    whose step the flow has already walked past, so filing this against the
    reviewed producer would hide the whole conversation from the web console.
    """
    if project_root is None:
        return None
    try:
        from ..engine import interaction_calls

        return interaction_calls.write_dialog_call(
            project_root,
            flow_id=flow.flow_id,
            step_id=call_step.step_id,
            step_type=dialog.context.current_step_type,
            prompt=_dialog_call_prompt(dialog, proposal),
            transcript=dialog.transcript(),
            decision=proposal.to_dict() if proposal is not None else None,
            rewind_targets=_dialog_rewind_targets(flow),
            same_session=dialog.binding is not None,
            agent_name=(dialog.binding or {}).get("agent_name") or "",
            subject_step_id=dialog.context.current_step_id,
            reset_preview=_reset_preview_payload(flow, proposal, project_root),
            group_work=_group_work_preview(flow, proposal, project_root),
        )
    except Exception:  # pragma: no cover - defensive: never block on the mirror
        logger.exception("Failed to mirror the interjection dialog to a call file")
        return None


def _dialog_subject_step(flow: FlowInstance, current_step: Any) -> Any:
    """The step whose session the dialog should talk to.

    At a CONFIRM gate or a failure-decision pause the operator is asking about
    the artefact under review, not about the gate — so the interlocutor is the
    step that PRODUCED it. That step's session still holds the reasoning behind
    the output being questioned; the CONFIRM step has no session at all.
    """
    try:
        if current_step.step_type == StepType.CONFIRM:
            reviewed_id = (current_step.inputs or {}).get("step_to_review_id")
            reviewed = flow.state.steps.get(reviewed_id) if reviewed_id else None
            if reviewed is not None:
                return reviewed
    except Exception:  # pragma: no cover - defensive
        logger.debug("Failed to resolve dialog subject step", exc_info=True)
    return current_step


def _dialog_at_pause_point(
    flow: FlowInstance,
    current_step: Any,
    persistence: PersistenceManager,
    project_root: Optional[Path],
    prompt_history: Any = None,
    *,
    initial_messages: Optional[List[str]] = None,
    pause_context: Optional[str] = "confirm",
    output_format: str = "cli",
) -> str:
    """Open the interjection dialog from a pause point (CONFIRM / failure).

    There is no LLM subprocess to interrupt here, so the dialog starts
    immediately. ``continue`` returns :data:`_DIALOG_RESUME_PAUSE`, meaning
    "go back to waiting at this gate" — the pause is not resolved by the
    dialog, only informed by it.

    ``pause_context=None`` says the opposite: this is not a gate but a step
    that was cut off mid-call in a process that has since exited, so
    ``continue`` re-runs it as a retry (and performs the single retry-count
    increment the resume path deliberately skipped).
    """
    subject = _dialog_subject_step(flow, current_step)
    # A Ctrl-C at the gate publishes the stop request before raising, and any
    # text that came with it is the conversation's opening message. Taken (not
    # merely read) here because the request is level-triggered: leaving it set
    # would make LLMCaller's pre-spawn check reject the dialog's own first
    # read-only turn, so the user's opening question would be recorded and
    # immediately reported as cancelled.
    request = get_stop_signal().take()
    messages = list(initial_messages or [])
    if request is not None:
        messages.extend(text for text in request.texts if text)
    if output_format == "json":
        # No terminal to prompt on: each round travels through a `dialog`
        # call file and the flow exits PAUSED between them.
        return _run_interjection_dialog_noninteractive(
            flow, subject, persistence, Path(project_root or Path.cwd()),
            initial_messages=messages,
            pause_context=pause_context,
            call_step=current_step,
        )
    return _run_interjection_dialog(
        flow, subject, persistence, project_root, prompt_history,
        initial_messages=messages,
        pause_context=pause_context,
        call_step=current_step,
    )


def _dialog_state(flow: FlowInstance) -> Optional[Dict[str, Any]]:
    """Return the persisted non-interactive dialog state, if one is open."""
    state = flow.state.context.get("active_dialog")
    return state if isinstance(state, dict) else None


def _gate_call_is_pending(project_root: Optional[Path], step: Any) -> bool:
    """Whether *step*'s gate still has an unanswered call file on disk.

    INVARIANT: ``continue`` at a gate resolves NOTHING — the gate keeps waiting
    for its own answer. Across a process boundary the wait IS that call file, so
    "go back to waiting" can only mean "exit with the gate untouched" while the
    file is still pending. Re-entering the run loop instead would re-run the
    CONFIRM handler: the step would flip PAUSED → RUNNING and publish a second
    confirm call for a gate nobody resolved.

    Returns False when no such call exists (the stop landed before the handler
    published it) or when it has already been answered — in both cases the gate
    genuinely has to be (re-)entered.
    """
    if project_root is None or step is None:
        return False
    try:
        from ..engine import interaction_calls

        step_id = str(getattr(step, "step_id", "") or "")
        if not step_id:
            return False
        calls_dir = interaction_calls.calls_dir_for(project_root)
        candidates = [
            interaction_calls.retry_decision_call_path(project_root, step_id)
        ]
        if calls_dir.exists():
            candidates.extend(sorted(calls_dir.glob(f"confirm_{step_id}_*.json")))
        for path in candidates:
            if not path.exists():
                continue
            if interaction_calls.read_response(path) is None:
                return True
    except Exception:  # pragma: no cover - a probe must never break the resume
        logger.exception("Failed to probe the pending gate call")
    return False


def _run_interjection_dialog_noninteractive(
    flow: FlowInstance,
    current_step: Any,
    persistence: PersistenceManager,
    project_root: Path,
    *,
    initial_messages: Optional[List[str]] = None,
    pause_context: Optional[str] = None,
    call_step: Any = None,
    apply_step: Any = None,
) -> str:
    """Drive one round of the dialog through the ``tianluo/calls/`` channel.

    Mirrors the non-interactive DISCOVERY pause exactly: each round writes a
    ``dialog`` call file, the flow exits PAUSED, the daemon re-spawns
    ``--resume`` once the web answers, and the answer is consumed on the way
    back in. The conversation state lives in ``flow.state.context`` because it
    has to survive that process boundary.
    """
    from ..engine import interaction_calls
    from ..engine.interjection_dialog import (
        DialogDecision,
        InterjectionDialog,
        find_dialog_session,
        parse_direct_decision,
    )

    call_step = call_step if call_step is not None else current_step
    apply_step = apply_step if apply_step is not None else current_step
    state = _dialog_state(flow) or {
        "step_id": current_step.step_id,
        # The gate the call file is filed against; see _refresh_dialog_call.
        "call_step_id": call_step.step_id,
        # The step a confirmed decision is APPLIED to; see the interactive
        # driver's docstring. Persisted because it must survive the daemon's
        # process boundary, exactly like the call step.
        "apply_step_id": apply_step.step_id,
        "pause_context": pause_context,
        "transcript": [],
        "decision": None,
        "call_file": None,
        # The rounds this conversation has put on the console, oldest first.
        # Crosses the daemon's process boundary because a bare confirmation is
        # bound to the round it answered, and that round was published by an
        # earlier wake-up; see :func:`_bare_confirmation_is_stale`.
        "published_rounds": [],
    }
    pause_context = state.get("pause_context") or pause_context
    call_step_id = state.get("call_step_id") or call_step.step_id
    state["call_step_id"] = call_step_id
    apply_step_id = state.get("apply_step_id") or apply_step.step_id
    state["apply_step_id"] = apply_step_id
    apply_step = flow.state.steps.get(apply_step_id) or apply_step

    # WHY the key's PRESENCE and not its truthiness: ``None`` is a settled
    # answer here — either no session was ever found, or a round discovered the
    # provider had dropped it. Re-looking-up on every wake-up would resurrect a
    # dead binding across the daemon's process boundary, so each later round
    # would probe the same dead session again and stamp its user record with
    # the obsolete id.
    if "binding" in state:
        binding = state.get("binding")
    else:
        binding = find_dialog_session(flow, current_step, project_root)
        state["binding"] = binding
    dialog = InterjectionDialog(
        flow, current_step, project_root, binding=binding
    )
    dialog.turns = list(state.get("transcript") or [])

    # WHY ``is not None`` rather than truthiness: an EMPTY message is itself
    # an answer here — "change nothing, resume now" — and filtering it out
    # would silently turn the cheapest decision into another paused round.
    incoming: List[str] = [m for m in (initial_messages or []) if m is not None]
    proposal = DialogDecision.from_dict(state.get("decision"))
    # Set when the web reply asked for a preview of an EDITED decision rather
    # than its execution; the round is then re-published instead of applied.
    preview_only = False
    # Set when the web reply was an explicit confirmation. It outranks an
    # interjection that raced it: the operator already said "do this".
    confirmed = False
    # Set when a web free-text reply edited the proposal's fields: the round is
    # re-published (with a rebuilt preview) rather than applied.
    edits_only = False
    # An apply/validation failure from THIS wake, republished with the round so
    # the web operator SEES why their decision did not execute instead of
    # watching the flow silently stay paused. Deliberately not loaded back from
    # ``state``: once the operator answers, the new round stands on its own.
    apply_error = ""

    # Decision 5: an interjection that arrives while a round is open IS that
    # round's next user message. Drained BEFORE the still-unanswered bail-out
    # below, or the daemon's wake-up would exit straight back to PAUSED and the
    # file would sit pending until some unrelated event woke the flow again.
    drained = _collect_pending_dialog_messages(project_root)

    published_rounds: List[Dict[str, Any]] = _seed_published_rounds(state, proposal)

    def _reject_stale_confirmation() -> str:
        """Re-publish the current round instead of applying a bare confirmation.

        INVARIANT: the ledger keeps every round it has published. Pruning it to
        the one being republished would make the next fieldless confirmation
        bindable by construction — a client that retries the refused answer
        without ever fetching the new round would have its stale approval
        executed, which for ``keep`` → ``reset`` discards the workspace on an
        approval never given. Only evidence the client saw this round (the
        echoed round id) re-opens the path.
        """
        return t("cli.run.dialog.web_decision_superseded")

    call_path = state.get("call_file")
    if call_path:
        call_file = Path(call_path)
        # Taken before the answer is consumed — the binding lives on the
        # response file, which reading the reply removes.
        binding = (
            interaction_calls.dialog_response_binding(call_file)
            if call_file.exists()
            else {}
        )
        response = (
            interaction_calls.read_dialog_response(call_file)
            if call_file.exists()
            else None
        )
        if response is None and not drained:
            # Still unanswered and nothing new arrived — stay paused.
            flow.state.context["active_dialog"] = state
            flow.status = FlowStatus.PAUSED
            persistence.save_flow(flow)
            return _DIALOG_AWAITING_WEB
        _cleanup_discovery_call(call_file)
        state["call_file"] = None
        # Whether a FIELDLESS answer ("confirm" / "1") can still be read as an
        # answer to the round now on the table; see
        # :func:`_bare_confirmation_is_stale`.
        stale_confirmation = _bare_confirmation_is_stale(
            published_rounds,
            _dialog_round_revision(proposal),
            echoed_revision=str(binding.get("decision_revision") or ""),
            responded_at=binding.get("responded_at"),
        )
        if response is None:
            # Superseded by a new interjection: the open call file is retired
            # and republished below carrying the answer to this message.
            proposal = None
        elif response.get("confirm") and proposal is not None:
            if stale_confirmation:
                # A fieldless confirmation of a round this one has replaced —
                # applying it would execute a decision its author never saw.
                apply_error = _reject_stale_confirmation()
                preview_only = True  # republish for a fresh confirmation
            else:
                confirmed = True  # confirm the proposal unchanged
        elif isinstance(response.get("decision"), dict):
            edited = DialogDecision.from_dict(response["decision"], strict=True)
            if edited is None:
                # A user-edited decision with an invalid action/workspace is
                # rejected and re-published for correction — never coerced into
                # a different action and executed.
                apply_error = t("cli.run.dialog.invalid_decision")
                preview_only = True  # republish, do not apply
            else:
                proposal = edited
                # "Show me what this would do", not "do it": the round is
                # re-published with a preview built for the edited decision.
                preview_only = bool(response.get("preview"))
                # An edited decision that is NOT a preview request is the
                # operator's Apply. Recording that explicitly is what keeps a
                # racing interjection from overwriting it below — they already
                # said "do this".
                confirmed = not preview_only
        else:
            text_reply = str(response.get("text") or "")
            stripped_reply = text_reply.strip()
            if proposal is not None and stripped_reply == "1":
                # The call body tells the web operator "Enter 1 to apply", so
                # the channel honours it exactly as the terminal does — and,
                # being just as fieldless, it is bound to its round the same
                # way a one-click confirm is.
                if stale_confirmation:
                    apply_error = _reject_stale_confirmation()
                    preview_only = True
                else:
                    confirmed = True
            elif proposal is not None and _apply_decision_edits(
                proposal, stripped_reply
            ):
                # ``field: value`` lines edit the pending proposal, exactly as
                # at the terminal; the re-published round carries the edited
                # fields (and a preview rebuilt for them).
                edits_only = True
            else:
                # With no proposal on the table a bare "confirm" is just the
                # operator's next message (the reply carries its own text), not
                # a confirmation of something that was never offered.
                incoming.append(text_reply)
                proposal = None

    incoming.extend(drained)

    if (
        proposal is not None
        and not preview_only
        and not edits_only
        and (confirmed or not incoming)
    ):
        # Anything that raced the confirmation is recorded before the decision
        # executes: the dialog is about to end, so an unanswered message would
        # otherwise vanish without ever appearing in the step's history.
        for raced in incoming:
            if raced.strip():
                dialog.record_user_turn(raced, source="web-console")
                logger.info(
                    "Dialog: message arrived alongside a confirmed decision; "
                    "recorded but not answered"
                )
        outcome, apply_error = _confirm_and_apply_decision(
            flow, apply_step, proposal, dialog, persistence, project_root,
            pause_context=pause_context,
        )
        if outcome is not None:
            flow.state.context.pop("active_dialog", None)
            return outcome
        # The confirmed decision failed to apply (a bad rewind target, a
        # refused reset). The dialog stays open and the round is republished
        # below with the error on it — the flow must NOT fall through to a
        # ``continue`` the operator never confirmed, and the web side must see
        # why nothing happened.
        state["transcript"] = dialog.transcript()
        state["decision"] = proposal.to_dict() if proposal is not None else None
        state["apply_error"] = apply_error
        state["binding"] = dialog.binding
        state["pause_context"] = pause_context
        call_file = interaction_calls.write_dialog_call(
            project_root,
            flow_id=flow.flow_id,
            step_id=call_step_id,
            step_type=dialog.context.current_step_type,
            prompt=_dialog_call_prompt(dialog, proposal, error=apply_error),
            transcript=state["transcript"],
            decision=state["decision"],
            rewind_targets=_dialog_rewind_targets(flow),
            same_session=dialog.binding is not None,
            agent_name=(dialog.binding or {}).get("agent_name") or "",
            subject_step_id=dialog.context.current_step_id,
            reset_preview=_reset_preview_payload(flow, proposal, project_root),
            group_work=_group_work_preview(flow, proposal, project_root),
            apply_error=apply_error or "",
        )
        state["call_file"] = str(call_file)
        state["published_rounds"] = _record_published_round(
            published_rounds, _dialog_round_revision(proposal), call_file
        )
        flow.state.context["active_dialog"] = state
        flow.status = FlowStatus.PAUSED
        persistence.save_flow(flow)
        return _DIALOG_AWAITING_WEB

    # The messages still to consume this round. Refilled from the calls
    # directory as it drains: while THIS process is alive the daemon refuses to
    # spawn another ``--resume``, so an interjection posted while the agent was
    # answering has no other way in — draining only once left it pending until
    # some unrelated wake-up.
    pending: List[str] = list(incoming)
    drains = 0

    def _next_message() -> Optional[str]:
        nonlocal drains
        while not pending:
            if drains >= _MAX_DIALOG_DRAINS:
                return None
            drains += 1
            fresh = _collect_pending_dialog_messages(project_root)
            if not fresh:
                return None
            pending.extend(fresh)
        return pending.pop(0)

    while True:
        message = _next_message()
        if message is None:
            break
        text = (message or "").strip()
        if not text:
            # Empty message = resume unchanged, no LLM call — but only when it
            # is the last thing the user said. A blank followed by more text is
            # not an answer, it is noise in front of one.
            follow_up = _next_message()
            if follow_up is not None:
                pending.insert(0, follow_up)
                continue
            outcome, apply_error = _confirm_and_apply_decision(
                flow, apply_step, DialogDecision(), dialog, persistence,
                project_root, pause_context=pause_context,
            )
            if outcome is None:
                # Even "resume unchanged" must not silently degrade when the
                # apply itself fails — republish with the error attached.
                break
            flow.state.context.pop("active_dialog", None)
            return outcome
        direct = parse_direct_decision(text)
        if direct is not None:
            dialog.record_user_turn(text, source="web-console")
            proposal = direct
            # INVARIANT: no message is dropped on the way to a proposal. A
            # proposal is not a confirmation — only the operator confirms — so
            # anything they said after it is still theirs to have answered, and
            # breaking here deleted its call file without ever recording,
            # showing or processing the text.
            continue
        try:
            turn = dialog.ask(text)
        except Exception:  # noqa: BLE001 - a failed round must not lose the flow
            logger.exception("Interjection dialog round failed")
            turn = None
        if turn is not None and turn.is_decision:
            proposal = turn.decision
        else:
            # A further message supersedes the previous proposal: the agent has
            # been asked something new and has not proposed anything yet.
            proposal = None

    state["transcript"] = dialog.transcript()
    state["decision"] = proposal.to_dict() if proposal is not None else None
    state["pause_context"] = pause_context
    # A round may have discovered the provider dropped the session; the dialog
    # then dropped the binding in memory. That has to cross the process
    # boundary, or the next wake-up rebuilds the dialog on the dead binding.
    state["binding"] = dialog.binding
    # A round publishes this wake's apply error (if any); a clean conversation
    # round clears a stale one from the state.
    if apply_error:
        state["apply_error"] = apply_error
    else:
        state.pop("apply_error", None)
    prompt_text = _dialog_call_prompt(dialog, proposal, error=apply_error)
    call_file = interaction_calls.write_dialog_call(
        project_root,
        flow_id=flow.flow_id,
        step_id=call_step_id,
        step_type=dialog.context.current_step_type,
        prompt=prompt_text,
        transcript=state["transcript"],
        decision=state["decision"],
        rewind_targets=_dialog_rewind_targets(flow),
        same_session=dialog.binding is not None,
        agent_name=(dialog.binding or {}).get("agent_name") or "",
        subject_step_id=dialog.context.current_step_id,
        reset_preview=_reset_preview_payload(flow, proposal, project_root),
        group_work=_group_work_preview(flow, proposal, project_root),
        apply_error=apply_error or "",
    )
    state["call_file"] = str(call_file)
    state["published_rounds"] = _record_published_round(
        published_rounds, _dialog_round_revision(proposal), call_file
    )
    flow.state.context["active_dialog"] = state
    flow.status = FlowStatus.PAUSED
    persistence.save_flow(flow)
    logger.info("Interjection dialog paused for web reply: %s", call_file)
    return _DIALOG_AWAITING_WEB


def _dialog_call_prompt(dialog: Any, proposal: Any, error: str = "") -> str:
    """Human-facing body of a ``dialog`` call file."""
    parts: List[str] = []
    if error:
        # A failed Apply is republished WITH its reason: a call body that just
        # re-shows the proposal would read as "nothing happened" and invite the
        # same click again.
        parts.append(t("cli.run.dialog.apply_failed", error=error))
        parts.append("")
    for turn in dialog.transcript()[-6:]:
        speaker = (
            t("cli.run.dialog.speaker_user")
            if turn.get("role") == "user"
            else t("cli.run.dialog.speaker_agent")
        )
        content = str(turn.get("content") or "").strip()
        if content:
            parts.append(f"{speaker}: {content}")
    if proposal is not None:
        parts.append("")
        parts.append(t("cli.run.dialog.confirm_message"))
    else:
        parts.append("")
        parts.append(t("cli.run.dialog.input_message"))
    return "\n".join(parts)


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
        get_console().print(t("cli.run.discovery.resume_notice"))


def _maybe_write_discovery_call(
    flow: FlowInstance, current_step: Any, project_root: Optional[Path]
) -> Optional[Path]:
    """Mirror an interactive discovery pause to a ``tianluo/calls/`` call file.

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


def _drain_interjection_as_reply(project_root: Optional[Path]) -> Optional[str]:
    """Consume queued interjections and return them as ONE discovery reply.

    Decision 5's DISCOVERY rule: while discovery is paused the user is already
    at a conversation prompt, so a web-pushed interjection is just their next
    turn — not a reason to nest a second conversation. Several queued messages
    are joined into one reply so nothing is dropped and their order is kept.
    Returns ``None`` when nothing was queued.
    """
    if project_root is None:
        return None
    texts = _collect_pending_dialog_messages(project_root)
    if not texts:
        return None
    return "\n".join(texts)


def _discard_outstanding_discovery_call(current_step: Any) -> None:
    """Drop a discovery call that an interjection answered out of band.

    Without this the stale call file would keep showing as a pending
    interaction on the web console even though its question has been overtaken
    by the interjection that is now the user's reply.
    """
    call_path = current_step.outputs.pop("discovery_call_file", None)
    if call_path:
        _cleanup_discovery_call(Path(call_path))


#: Web / interjection messages that were consumed off disk but lost the race to
#: a terminal answer.
#:
#: INVARIANT: a message consumed off disk is never dropped. The dual-wait
#: pollers CONSUME as they read (an interjection call file is sealed, a dialog
#: ``.response`` deleted), so when the terminal side completes inside the same
#: poll tick the message exists nowhere else — discarding it loses a decision
#: the web operator already applied. Parking it here delivers it as the next
#: message instead: every subsequent web-channel sweep and every dialog-message
#: drain takes from this queue first.
#:
#: Process-global for the same reason the stop signal is: which wait picks the
#: message up depends on where the flow goes next, not on who parked it.
_DEFERRED_WEB_MESSAGES: List[str] = []


def _defer_web_messages(values: Any) -> None:
    """Park consumed-but-undelivered web messages for the next wait.

    INVARIANT: only real operator text is ever parked. The interjection
    dialog's control sentinel is a dialog-local marker whose payload lives in
    that dialog's own state, so parking it would outlive the dialog and be
    handed to an unrelated wait — as a discovery reply, or as the first message
    of a new dialog — as if the operator had typed a NUL string. Its owning
    dialog re-offers or resolves it instead (see ``_dialog_tick`` and
    :func:`_discard_unclaimed_web_decision`).
    """
    if isinstance(values, str):
        values = [values]
    for value in values or []:
        text = str(value or "")
        if not text:
            continue
        if text == _DIALOG_WEB_DECISION:
            logger.debug("Dialog control sentinel not parked in the web queue")
            continue
        _DEFERRED_WEB_MESSAGES.append(text)


def _take_deferred_web_message() -> Optional[str]:
    """Pop the oldest parked message, or ``None``."""
    if _DEFERRED_WEB_MESSAGES:
        return _DEFERRED_WEB_MESSAGES.pop(0)
    return None


def _drain_deferred_web_messages() -> List[str]:
    """Pop every parked message, oldest first."""
    drained = list(_DEFERRED_WEB_MESSAGES)
    _DEFERRED_WEB_MESSAGES.clear()
    return drained


def _ask_tick(
    tick_callback: Optional[Callable[[], Optional[str]]]
) -> Optional[str]:
    """Poll the structured reply channel; a raising tick never breaks the wait."""
    if tick_callback is None:
        return None
    try:
        return tick_callback()
    except Exception:  # pragma: no cover - never break the wait
        logger.exception("Interjection tick callback raised; ignoring")
        return None


def _poll_web_answer(
    call_file: Optional[Path],
    tick_callback: Optional[Callable[[], Optional[str]]],
) -> Optional[str]:
    """One non-blocking sweep of the web channel; the answer text, or ``None``.

    WHY the tick callback is asked FIRST and the plain response file only after:
    the tick understands the structured dialog protocol (it parks a
    ``{"decision": {...}}`` payload and hands back a sentinel), whereas
    :func:`_read_discovery_response` flattens any non-string payload to its
    ``str()`` repr — which, for a web operator's Apply, would turn an executable
    decision into prose fed back to the dialog LLM, and consume the file so it
    could never be re-read. Every place that consults the web channel therefore
    goes through this order.

    A message parked by an earlier wait that consumed it and then lost the race
    to a terminal answer wins outright: it is already off disk, so no later
    sweep could rediscover it.
    """
    deferred = _take_deferred_web_message()
    if deferred is not None:
        return deferred
    pushed = _ask_tick(tick_callback)
    if pushed is not None:
        return pushed
    if call_file is None:
        return None
    resp = _read_discovery_response(call_file)
    if resp is None:
        return None
    # A reply that landed in the window BETWEEN the tick above and this read is
    # still the structured channel's to interpret: the peek proves the file is
    # there now, so asking the tick once more consumes it as a decision instead
    # of flattening it here. Without this the two reads race, and roughly every
    # other web Apply was destroyed by the loser.
    pushed = _ask_tick(tick_callback)
    if pushed is not None:
        return pushed
    _cleanup_discovery_response(call_file)
    return resp


def _await_terminal_or_web_non_tty(
    call_file: Optional[Path],
    *,
    prompt_title: str,
    prompt_message: str,
    history: Any,
    strip: bool,
    poll_interval: float,
    tick_callback: Optional[Callable[[], Optional[str]]] = None,
) -> Tuple[str, Optional[str]]:
    """Non-TTY dual-wait: the blocking stdin read raced against the web channel.

    WHY this is a race and not "read stdin, then check the web file once": with
    a non-TTY stdin that stays open (a pipe held by the launcher), the read
    returns only at EOF, so the post-read check can be arbitrarily late — a web
    operator answering the dialog would sit unanswered, and their answer would
    then be read through the flattening generic path instead of the structured
    tick. The TTY branch already races the two channels; this gives the non-TTY
    branch the same semantics, structured decisions included.

    INVARIANT: losing the race consumes no stdin. The terminal side is polled
    in bounded slices through the process-wide funnel rather than by parking a
    thread in the read — a parked reader outlives the wait it was started for
    and steals the operator's next answer (a gate choice, CONFIRM feedback)
    from the consumer that actually asked for it.
    """
    from ..stdin_channel import PENDING

    while True:
        pushed = _poll_web_answer(call_file, tick_callback)
        if pushed is not None:
            return (_DISCOVERY_SRC_WEB, pushed)
        text = _read_multiline_input(
            prompt_title=prompt_title,
            prompt_message=prompt_message,
            history=history,
            strip=strip,
            timeout=poll_interval,
        )
        if text is not PENDING:
            break

    # The terminal answered — but a web answer may have landed in the same
    # instant, and the web keeps its long-standing priority here.
    late = _poll_web_answer(call_file, tick_callback)
    if late is not None:
        return (_DISCOVERY_SRC_WEB, late)
    if text is None:
        return (_DISCOVERY_SRC_CANCEL, None)
    return (_DISCOVERY_SRC_TERMINAL, text)


def _await_terminal_or_web(
    call_file: Optional[Path],
    *,
    prompt_title: str,
    prompt_message: str,
    history: Any = None,
    strip: bool = True,
    poll_interval: float = 0.4,
    tick_callback: Optional[Callable[[], Optional[str]]] = None,
) -> Tuple[str, Optional[str]]:
    """Wait for whichever comes first: a terminal answer or a web response.

    ``tick_callback`` is polled alongside the response file while the operator
    is at the prompt. Returning a string from it delivers that text as the
    answer (source ``web``) and cancels the terminal read — which is how a
    web-pushed interjection becomes the paused DISCOVERY step's next reply
    without the operator having to type anything.

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
        if tick_callback is not None:
            pushed = tick_callback()
            if pushed:
                return (_DISCOVERY_SRC_WEB, pushed)
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
    early = _poll_web_answer(call_file, tick_callback)
    if early is not None:
        return (_DISCOVERY_SRC_WEB, early)

    # No interactive terminal to race against (piped stdin / no TTY): race the
    # blocking stdin read against the web channel, so an answer that lands while
    # stdin stays open is acted on rather than waiting for EOF.
    if not sys.stdin.isatty():
        return _await_terminal_or_web_non_tty(
            call_file,
            prompt_title=prompt_title,
            prompt_message=prompt_message,
            history=history,
            strip=strip,
            poll_interval=poll_interval,
            tick_callback=tick_callback,
        )

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
    tick_callback: Optional[Callable[[], Optional[str]]] = None,
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
                # Every poll tick: give the tick callback a chance to supply an
                # answer of its own (a web-pushed interjection is the paused
                # step's next reply), then check the response file — in that
                # order, and race-free, via the shared sweep.
                resp = _poll_web_answer(call_file, tick_callback)
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
        # The terminal answered first, but the poller may have consumed a web
        # message in the very same tick — and consuming removed it from disk.
        # Park it so it is delivered as the next message rather than lost.
        _defer_web_messages(web_holder["value"])
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
        late = _poll_web_answer(call_file, tick_callback)
        if late is not None:
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

    The clarifying question is mirrored to a ``tianluo/calls/`` call file (when a
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
        t("cli.run.discovery.pause_body"),
        title=t("cli.run.discovery.pause_title"),
    )

    # WHY an interjection during a DISCOVERY pause does NOT open the mid-flow
    # dialog (decision 5): discovery IS a conversation, and the user is already
    # at its prompt. Whatever they push from the web is simply their next
    # discovery reply, so it is delivered straight into this round instead of
    # nesting a second conversation inside the first.
    early = _drain_interjection_as_reply(project_root)
    if early is not None:
        return early

    def _tick() -> Optional[str]:
        return _drain_interjection_as_reply(project_root)

    call_file = _maybe_write_discovery_call(flow, current_step, project_root)
    try:
        while True:
            source, value = _await_terminal_or_web(
                call_file,
                prompt_title=t("cli.run.discovery.response_title"),
                prompt_message=t("cli.run.discovery.response_message"),
                history=prompt_history,
                strip=True,
                tick_callback=_tick,
            )

            if source == _DISCOVERY_SRC_CANCEL:
                # User cancelled
                persistence.save_flow(flow)
                render_full(
                    t("cli.run.discovery.paused_body"),
                    title=t("cli.run.discovery.paused_title"),
                )
                return None

            if not value:
                # Empty terminal input — ask again (web never submits empty).
                get_console().print(t("cli.run.discovery.provide_response"))
                continue

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

    Mirrored to a :data:`~tianluo.engine.interaction_calls.CALL_KIND_DISCOVERY_CONFIRM`
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
    # or _restore_discovery_display. An interjection arriving here is the
    # user's next discovery turn (decision 5) — it continues the refinement
    # rather than confirming it.
    early = _drain_interjection_as_reply(project_root)
    if early is not None:
        current_step.outputs.pop("awaiting_programmatic_confirm", None)
        return early

    def _tick() -> Optional[str]:
        return _drain_interjection_as_reply(project_root)

    call_file = _maybe_write_discovery_call(flow, current_step, project_root)
    try:
        while True:
            source, user_input = _await_terminal_or_web(
                call_file,
                prompt_title=t("cli.run.discovery.confirm_title"),
                prompt_message=t("cli.run.discovery.confirm_message"),
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
                    t("cli.run.discovery.paused_body"),
                    title=t("cli.run.discovery.paused_title"),
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
            get_console().print(t("cli.run.discovery.captured_input"))
            return user_input
    finally:
        _cleanup_discovery_call(call_file)


# Sentinel returned by the non-interactive discovery pause handler when a call
# file has been written (or is still awaiting a response): the run loop must
# persist the flow and exit so the web "Respond to Flow" interaction can answer.
_DISCOVERY_AWAITING = object()

# Sentinel returned by the non-interactive CONFIRM pause handler once the flow
# has been persisted PAUSED with its call file on disk: the run loop must emit
# FLOW_PAUSED and exit 0 so the daemon can re-spawn --resume after the web
# answer lands. Mirrors _DISCOVERY_AWAITING for the CONFIRM gate.
_CONFIRM_AWAITING = object()


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
        # Framework-authored copy (the LLM produced neither a message nor any
        # questions), so it renders through i18n — unlike the LLM's own
        # message/questions above, which pass through verbatim in whatever
        # language the flow's language config made it produce.
        parts.append(t("cli.run.discovery.empty_round_prompt"))
    return "\n".join(parts)


def _write_discovery_call(
    flow: FlowInstance, current_step: Any, project_root: Path
) -> Path:
    """Write a ``tianluo/calls/`` call file for a non-interactive discovery pause.

    The call joins the unified human-call queue via the shared
    :func:`~tianluo.engine.interaction_calls.write_call` helper, so the daemon
    aggregator and web console render and route it like any other interaction.

    * A *confirmation* pause carries the
      :data:`~tianluo.engine.interaction_calls.CALL_KIND_DISCOVERY_CONFIRM` kind, a
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
    to a ``tianluo/calls/`` call file and the flow pauses; the web answers via the
    existing call/response mechanism. On resume the response is consumed and
    fed back into discovery as the next user turn.

    Returns the user-response string, the :data:`_PROGRAMMATIC_CONFIRM`
    sentinel, or :data:`_DISCOVERY_AWAITING` when the flow must pause to wait
    for a web response.
    """
    # An interjection queued for a paused DISCOVERY step is that step's next
    # user turn (decision 5), so it is consumed here and fed straight back
    # rather than staying queued until the flow resumes for another reason.
    early = _drain_interjection_as_reply(project_root)
    if early is not None:
        _discard_outstanding_discovery_call(current_step)
        return early

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
        return response
    # No outstanding call — write one and pause for a web response.
    call_file = _write_discovery_call(flow, current_step, project_root)
    current_step.outputs["discovery_call_file"] = str(call_file)
    persistence.save_flow(flow)
    logger.info("Discovery paused for web response: wrote call file %s", call_file)
    return _DISCOVERY_AWAITING


def _handle_confirm_pause_noninteractive(
    flow: FlowInstance,
    current_step: Any,
    persistence: PersistenceManager,
    project_root: Path,
) -> Any:
    """Handle a CONFIRM pause without a terminal (daemon ``--output-format json``).

    The CONFIRM step handler has already written the ``confirm_*.json`` call
    file and stashed its path in ``current_step.outputs['call_file']`` before
    returning PAUSED, so there is nothing to prompt for here: the web console
    answers the call through the daemon's MSG_RESPOND_CALL channel, which writes
    a ``<stem>.response.json`` sibling consumed by :func:`_check_confirm_response`
    on the next resume.

    The only job left is to persist ``FlowStatus.PAUSED`` to the engine.json
    top-level status. The daemon keys its resume decision off exactly that
    on-disk status (``daemon/daemon.py`` ``_resume_paused_flow`` only re-spawns a
    flow whose status == "PAUSED"), so without this the process would exit while
    the flow's top-level status was still "running" and the answered call would
    never be consumed — the "approved but nothing happens" deadlock this fixes.
    Mirrors the DISCOVERY json branch's on-disk-status contract.

    Returns :data:`_CONFIRM_AWAITING`; the caller emits FLOW_PAUSED and exits 0.
    """
    call_file = current_step.outputs.get("call_file")
    if not call_file or not Path(call_file).exists():
        # Fail-safe: the confirm handler is expected to have written the call
        # file before returning PAUSED. If it is somehow missing we still pause
        # (rather than silently exit non-PAUSED and wedge the flow), but warn so
        # the anomaly is visible — a paused flow with no call file simply cannot
        # be answered and will surface for manual intervention.
        logger.warning(
            "CONFIRM pause has no call file on disk (step %s); persisting PAUSED "
            "anyway to avoid a stuck non-PAUSED flow", current_step.step_id
        )
    flow.status = FlowStatus.PAUSED
    persistence.save_flow(flow)
    logger.info(
        "Confirm paused for web response (non-interactive): flow %s status=PAUSED",
        flow.flow_id,
    )
    return _CONFIRM_AWAITING


def make_cli_confirm_handler(
    project_root: Path,
    flow_id: Optional[str] = None,
    step_id: Optional[str] = None,
    poll_interval: float = 0.5,
) -> Callable[[str, List[str], Callable[[], bool]], Optional[str]]:
    """Build an ``on_confirm`` callback for :meth:`ClaudeCodeRunner.run_with_monitor`.

    Thin re-export of :func:`tianluo.engine.interaction_calls.make_cli_confirm_handler`
    so existing call sites and tests can keep importing it from ``run``. The
    canonical implementation lives in the engine layer because it is wired
    into the flow's LLM execution path by :mod:`tianluo.engine.llm_caller`, which
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

    WHY the raw string is returned rather than a looked-up label: a flow
    persisted under a since-retired task type must still display as itself.
    Mapping through a table would render such a flow blank the moment the type
    is dropped from the current classification space.

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
    ``tianluo/state/merge.lock`` (never inside a linked worktree). When
    ``project_root`` is itself a linked git worktree, resolve back to the main
    repo via :func:`config._resolve_main_repo_root`; otherwise ``project_root``
    is already the main repo and is returned unchanged. This guarantees a
    synchronous ``luo run`` launched from inside a worktree still contends on
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
    True left by a previously interrupted wait. When this call had written a
    streaming ``waiting_for_lock`` jsonl anchor (the contended path), it also
    emits a matching ``chat_history.record_lock_acquired`` clearing anchor so the
    web console's live transcript does not stay frozen on "等待锁" — persisting
    ``waiting_for_lock=False`` to engine.json alone never supersedes the streamed
    row; only a later same-step lifecycle anchor does.
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

    wrote_waiting_event = False
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
            wrote_waiting_event = True
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

    # If a "等待锁" jsonl anchor was emitted this call, emit the matching clear
    # event the moment the lock is acquired. Persisting waiting_for_lock=False to
    # engine.json alone does NOT supersede the streaming "等待锁" row the web
    # console already rendered — only a later same-step lifecycle anchor does.
    # The step's own ``step_started`` running anchor would eventually supersede
    # it, but a window (and, under contention, an unstable ordering) exists
    # between the acquire and that anchor, leaving the live transcript frozen on
    # "等待锁". record_lock_acquired closes that window with an explicit,
    # idempotent clearing anchor. Gated on wrote_waiting_event so a free/stale
    # acquire (no "等待锁" anchor was written) produces no spurious event.
    if wrote_waiting_event:
        try:
            from ..engine.chat_history import record_lock_acquired
            record_lock_acquired(
                project_root=project_root,
                flow_id=flow.flow_id,
                step_id=current_step.step_id,
                step_type=current_step.step_type.value,
            )
        except Exception:
            logger.debug(
                "failed to record lock-acquired clear event for %s",
                current_step.step_id, exc_info=True,
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
    manage_pidfile: bool = True,
    plan_decomposition: Optional[str] = None,
    plan_granularity: Optional[str] = None,
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
            ``luo run``), acquire the project's main-worktree mutex
            (``MergeLock(main_repo).acquire(blocking=True)``) before running and
            hold it for the *entire* run, releasing it on every exit path. The
            lock always targets the main repository's ``tianluo/state/merge.lock``
            (resolved from a worktree via :func:`_resolve_main_lock_root`), so
            synchronous runs serialise against each other and against any
            ``luo merge``. When ``False`` — the case for a ``--worktree`` run's
            isolated flow body — no lock is taken, so multiple worktree flow
            bodies execute concurrently and only contend at their trailing
            ``luo merge`` step. The DAG implement-step isolation worktrees never
            call ``run_flow`` and so never participate in this lock.
        worktree_branch: For a new ``--worktree`` flow, the isolation branch
            name to record on the flow (``worktree_branch``); ignored on resume.
        worktree_original_branch: For a new ``--worktree`` flow, the branch the
            run was launched from / will merge back into; recorded on the flow
            (``worktree_original_branch``) so resume can drive the trailing
            merge. Ignored on resume.
        manage_pidfile: When ``True`` (default for a synchronous ``luo run``),
            ``run_flow`` takes ``tianluo/state/run.pid`` on entry (exclusively —
            it REFUSES to start when another owner holds it, see
            :func:`_acquire_run_pidfile`) and clears it on exit so
            ``luo end-session`` can locate the live process. When
            ``False`` — the case for a ``--worktree`` run's isolated flow body —
            the CALLER (``run_worktree_mode`` / ``_resume_worktree_run``) owns
            the pidfile for the WHOLE worktree-run lifecycle, INCLUDING the
            post-COMPLETED trailing merge/cleanup phase that runs after
            ``run_flow`` returns. If ``run_flow`` cleared the marker in its own
            ``finally``, end-session dispatched during that trailing merge would
            find no pidfile and (because the still-live wrapper keeps
            ``cwd==main_root`` and the main ``engine.json`` does not carry the
            worktree flow_id) could not discover the process — and would then
            archive/delete the worktree out from under a still-running merge.
        plan_decomposition: Explicit PLAN decomposition doctrine
            (``capability`` / ``granular``) for a NEW flow.
        plan_granularity: Explicit group-count pressure
            (``auto`` / ``single`` / ``conservative``) for a NEW flow.

            WHY both are new-flow-only: the plan mode is decided once, at
            ``create_flow``, and persisted in the flow context. A resume must
            keep executing the shape it already entered — a hot config change or
            a stray flag on the resume command line must never re-decide the
            grouping of a flow whose PLAN has already produced groups.

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
    # Record this process's pid into ``tianluo/state/run.pid`` so ``luo end-session``
    # can RELIABLY identify the live flow process even when it is a ``--worktree``
    # run whose process ``cwd`` stays at the main repo (engine.json lives in the
    # worktree) and which is momentarily between agent/test subprocesses, i.e.
    # has no descendant whose cwd is inside the worktree. Cwd/descendant scanning
    # cannot find such a parent; the on-disk pid does. The file lives in the same
    # state dir as engine.json (the worktree's for a worktree run), so there is
    # at most one writer per state dir — enforced, not assumed: the marker is
    # taken exclusively and a run that cannot take it does not start, which is
    # what makes it a real ownership token for ``luo end-session`` to claim
    # against. It is cleared on every exit path below.
    #
    # For a ``--worktree`` flow body the CALLER owns the pidfile for the whole
    # worktree-run lifecycle (flow body + trailing merge), so it passes
    # ``manage_pidfile=False`` and we neither write nor clear it here — see the
    # ``manage_pidfile`` docstring above.
    if manage_pidfile:
        claim = _acquire_run_pidfile(persistence, flow_id)
        # Refuse BEFORE the try below: a run that never took the marker must not
        # reach the ``finally`` that drops it, or it would clear the owner's.
        # That finally is also what restores SIGINT, so this early exit has to
        # hand the caller's handler back itself.
        if _refuse_on_held_run_marker(claim, persistence.state_dir):
            signal.signal(signal.SIGINT, old_sigint_handler)
            return 1
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
            plan_decomposition=plan_decomposition,
            plan_granularity=plan_granularity,
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
        # Drop our run.pid marker so a finished flow is no longer reported as a
        # live process (best-effort; only removes the file when it still names us).
        # Skipped when the caller manages the pidfile (a --worktree flow body):
        # the marker must survive into the trailing merge/cleanup phase.
        if manage_pidfile:
            _clear_run_pidfile(persistence)


def _run_marker_is_stale(holder: "RunHolder") -> bool:
    """Whether a LOCAL ``run.pid`` record may be reclaimed by a starting run.

    Only bare liveness is consulted, never "does the pid look like an luo run":
    the marker is also the token ``luo end-session`` claims for its destructive
    window, and that claim names a live ``luo end-session`` process. Judging
    staleness by cmdline would declare that claim stale and steal it — putting
    a fresh engine back into exactly the window the claim exists to close. A
    live pid therefore always means "held"; only a dead one is reclaimable, and
    a genuinely recycled pid stays recoverable through ``luo end-session``,
    which clears its own host's abandoned markers.
    """
    from ..daemon.supervisor import _is_alive

    return not _is_alive(int(getattr(holder, "pid", 0) or 0))


def _acquire_run_pidfile(
    persistence: PersistenceManager, flow_id: Optional[str] = None
) -> "MarkerClaim":
    """Take ``run.pid`` for this process, refusing when another owner holds it.

    Read by ``luo end-session`` to reliably locate the live flow process, and by
    the resume double-spawn guards to reject a second engine when the marker is
    held by a live run on ANOTHER machine (a shared-filesystem hazard the local
    process table can never observe). The machine id is stamped so those guards
    can tell "held on this host" from "held on host X"; the flow id so they can
    tell "*your* flow runs there" from "*another* flow holds that root" and
    never point the operator at ending an unrelated session. ``flow_id`` is
    unknown for a brand-new run (the engine mints it later) — see
    :func:`_stamp_run_pidfile_flow`, which fills it in.

    INVARIANT: publication is exclusive (``acquire_run_marker``), and a blocked
    claim ABORTS the run. The marker is not merely advisory bookkeeping: it is
    the token ``luo end-session`` claims to make its archive/cleanup mutually
    exclusive with a start/resume. Overwriting an existing marker here — as a
    plain tmp+rename does — would let this engine start *inside* another host's
    destructive window, which then deletes this flow's worktree and review
    baselines while it runs. Refusing to start is recoverable; that is not.

    INVARIANT: the run fails CLOSED. A claim that could not be established for
    a local I/O reason (unwritable state dir, EIO/EACCES on a shared mount) is
    refused exactly like a competing owner: the failure proves nothing about
    ownership, and starting anyway would put this engine into the flow with no
    token at all — writing state next to an ``luo end-session`` that still
    believes it owns the flow and is deleting its baselines. Never raises.
    """
    from ..core.run_pidfile import MarkerClaim, acquire_run_marker

    try:
        persistence.ensure_directories()
        return acquire_run_marker(
            persistence.state_dir, flow_id, is_stale=_run_marker_is_stale
        )
    except Exception:  # noqa: BLE001 - unestablished ownership blocks the run
        logger.debug("Failed to write run.pid marker", exc_info=True)
        return MarkerClaim(False, None, True)


def _refuse_on_held_run_marker(claim: "MarkerClaim", state_dir: Path) -> bool:
    """Display the refusal for a ``run.pid`` this run does not own; ``True`` when refused.

    A claim that is not ``blocked`` means the marker already names THIS
    process and only its refresh failed — ownership is intact, so there is
    nothing to refuse — see :func:`_acquire_run_pidfile`.

    Two holder-less refusals are told apart by re-probing the marker, because
    the operator action differs: a record that is THERE but nobody can decode
    is recoverable only by inspecting and removing that file (and must never be
    broken automatically — on a shared filesystem it may be the live remote run
    this refusal is protecting), whereas a claim that failed with an I/O error
    leaves ownership simply unestablished, and the fix is to make the state
    directory writable and retry.
    """
    from ..core.machine_id import is_local_machine
    from ..core.run_pidfile import probe_run_marker

    if not claim.blocked:
        return False
    holder = claim.holder
    if holder is None:
        key = (
            "cli.run.marker.held_unreadable"
            if probe_run_marker(Path(state_dir)).present
            else "cli.run.marker.unverifiable"
        )
        display_error(t(key, path=str(Path(state_dir) / "run.pid")))
    elif is_local_machine(holder.machine_id):
        display_error(t("cli.run.marker.held_locally", pid=holder.pid))
    else:
        display_error(
            t("cli.run.marker.held_by_machine", machine=holder.machine_id)
        )
    return True


def _stamp_run_pidfile_flow(
    persistence: PersistenceManager, flow_id: Optional[str]
) -> None:
    """Best-effort: fill this run's flow id into an already-written ``run.pid``.

    A new run stamps the marker before the engine mints the flow id, so the
    record starts flow-less; without this the cross-machine resume guard on
    another host could only report "this root has a live run" and never name
    the flow. Only rewrites a marker this process already owns (same pid, this
    machine), so a concurrently-relaunched run — or a live run owning it from
    another host — is never clobbered. Never raises.
    """
    if not flow_id:
        return
    try:
        from ..core.machine_id import is_local_machine
        from ..core.run_pidfile import read_run_holder

        holder = read_run_holder(persistence.state_dir)
        if (
            holder is None
            or holder.pid != os.getpid()
            or not is_local_machine(holder.machine_id)
            or holder.flow_id == str(flow_id)
        ):
            return
        # Re-taking a marker we already own is idempotent and rewrites the
        # record in place, so this cannot clobber a competitor.
        _acquire_run_pidfile(persistence, str(flow_id))
    except Exception:  # noqa: BLE001 - the marker is purely advisory
        logger.debug("Failed to stamp flow id into run.pid marker", exc_info=True)


def _clear_run_pidfile(persistence: PersistenceManager) -> None:
    """Best-effort: remove ``tianluo/state/run.pid`` when it still names this process.

    Only unlinks a record that is our own pid on THIS machine, so a
    concurrently-relaunched flow — or, critically, a live run or an
    ``luo end-session`` claim that owns it from another host — is never
    clobbered. Shares :func:`~tianluo.core.run_pidfile.release_run_marker` with
    end-session's claim release so both sides drop the token by the same rule;
    ``drop_undecodable`` is this side's alone, because a corrupted record in the
    state dir this run owned would otherwise be read as "held" forever and wedge
    both a later start and end-session. Never raises.
    """
    from ..core.run_pidfile import release_run_marker

    release_run_marker(persistence.state_dir, drop_undecodable=True)



def _execute_step_with_interjections(
    state_machine: StateMachine,
    flow: FlowInstance,
    current_step: Any,
    project_root: Path,
    on_running: Callable[[Any], None],
) -> Tuple[Any, bool]:
    """Run a step with the web-interjection watcher live.

    Returns ``(result, stopped, interrupted)``. ``stopped`` is True when a stop
    request (Ctrl-C or a web interjection) arrived during the step — the caller
    then opens the interjection dialog. The watcher and the SIGINT handler
    publish to the SAME signal, which is what makes those two entry points one
    path; the watcher additionally escalates to a main-thread
    ``KeyboardInterrupt`` when no runner is supervising a child, so a step
    doing its work in Python (TEST above all) is cut short by a web
    interjection exactly as Ctrl-C cuts it short.

    ``interrupted`` distinguishes "the step was actually cut short" from "the
    request landed as the step was already finishing". Only the former may be
    re-run by a confirmed ``continue``: rerunning a suite that completed anyway
    would discard good work the user never asked to throw away.
    """
    signal_obj = get_stop_signal()
    signal_obj.clear()
    watcher = InterjectionWatcher(
        project_root, signal=signal_obj, escalate_to_main=True
    )
    watcher.start()
    result = None
    interrupted = False
    try:
        result = state_machine.run_step(flow, current_step, on_running=on_running)
    except KeyboardInterrupt:
        interrupted = True
    finally:
        try:
            watcher.stop()
        except KeyboardInterrupt:  # pragma: no cover - escalation raced the stop
            interrupted = True
    if interrupted:
        return None, True, True
    # A cooperative stop does not raise: the runner returns partial output and
    # the step reports failure, so the flag is the only reliable evidence that
    # the work was cut short rather than merely interjected upon at the end.
    stopped = signal_obj.is_set()
    return result, stopped, stopped and _is_incomplete_result(result)


def _stop_request_as_discovery_reply(
    current_step: Any, result: Any, interrupted: bool
) -> Optional[str]:
    """Take a stop request that landed as DISCOVERY paused, as its next reply.

    WHY (decision 5): discovery IS a conversation and the user is already at its
    prompt, so an interjection arriving in the final tick of the discovery call
    is their next discovery reply — not a request to nest a second conversation
    inside the first. It cannot be left for ``_drain_interjection_as_reply`` to
    find, because the watcher has already consumed the call file into the stop
    request; without taking it here the text is delivered to the small dialog
    and the operator has to retype it.

    Returns ``None`` — leaving the request published for the small dialog — for
    anything else: a genuinely interrupted call, a non-DISCOVERY step, a
    discovery round that did not pause, the programmatic-confirm gate (where an
    arbitrary message is not an answer), and a bare Ctrl-C carrying no text.
    """
    from ..engine.models import StepStatus, StepType

    if interrupted:
        return None
    if getattr(current_step, "step_type", None) != StepType.DISCOVERY:
        return None
    if result != StepStatus.PAUSED:
        return None
    if (getattr(current_step, "outputs", None) or {}).get(
        "awaiting_programmatic_confirm"
    ):
        return None
    signal_obj = get_stop_signal()
    pending = signal_obj.pending
    texts = [
        str(text).strip()
        for text in (getattr(pending, "texts", None) or [])
        if str(text).strip()
    ]
    if not texts:
        return None
    signal_obj.take()
    return "\n\n".join(texts)


def _is_incomplete_result(result: Any) -> bool:
    """Whether *result* shows the step did NOT reach a terminal outcome.

    A cooperatively stopped LLM step surfaces as FAILED (partial output, no
    parsed result); a step that ran to COMPLETED/PARTIAL despite the request
    finished its work, and re-running it is destruction, not continuation.
    """
    from ..engine.models import StepStatus

    status = getattr(result, "status", result)
    return status not in (
        StepStatus.COMPLETED,
        StepStatus.PARTIAL,
        StepStatus.PAUSED,
        StepStatus.REVISION_NEEDED,
    )


def _dialog_outcome_exit_code(
    outcome: str,
    flow: FlowInstance,
    persistence: PersistenceManager,
    emitter: Any,
    current_step: Any,
    step_type_value: str,
) -> Optional[int]:
    """Translate a dialog outcome into a process exit code, or ``None``.

    ``None`` means "the run loop keeps going": ``continue`` re-armed the step
    and ``restart`` rewound the flow, both of which are resolved by simply
    re-entering the loop.
    """
    if outcome == _DIALOG_AWAITING_WEB:
        emitter.emit(new_event(
            EventType.FLOW_PAUSED, flow_id=flow.flow_id,
            step_id=current_step.step_id, step_type=step_type_value,
        ))
        return 0
    if outcome == _DIALOG_EXIT:
        persistence.save_flow(flow)
        emitter.emit(new_event(
            EventType.FLOW_PAUSED, flow_id=flow.flow_id,
            step_id=current_step.step_id, step_type=step_type_value,
        ))
        # Same return code the pre-dialog Ctrl-C-then-cancel path used, so
        # scripts keying off it are unaffected.
        return 130
    return None


def _open_dialog_after_stop(
    flow: FlowInstance,
    current_step: Any,
    persistence: PersistenceManager,
    project_root: Path,
    prompt_history: Any,
    output_format: str,
    *,
    interrupted: bool = True,
) -> str:
    """Consume the stop request and open the interjection dialog it asked for.

    ``interrupted=False`` means the step had already reached a terminal result
    when the request landed. The dialog still opens (the user asked for it), but
    ``continue`` then means "carry on from here" — re-arming a step that
    finished would throw away work nobody asked to discard.
    """
    signal_obj = get_stop_signal()
    request = signal_obj.take()
    messages = list(request.texts) if request is not None else []
    pause_context = None if interrupted else "completed_step"
    # INVARIANT: the interrupted step's state reaches disk BEFORE the dialog
    # blocks on the operator. The stop handlers record what the interruption
    # left behind — above all a DAG implement's ``dag_preserved_worktrees`` and
    # ``implemented_groups``, which name on-disk worktrees deliberately left in
    # place — and only in memory. The dialog can then sit at a prompt for
    # hours; a process that dies there (SSH drop, closed terminal) would leave
    # engine.json holding the pre-handler snapshot, and the next ``--resume``
    # would find no record of the preserved worktrees: it would probe the
    # interrupted groups as unaccounted, misread fork-heir commits as
    # "completed", and force-clean the worktrees holding their uncommitted work
    # and the cwd their provider sessions are bound to.
    try:
        persistence.save_flow(flow)
    except Exception:  # pragma: no cover - defensive
        logger.debug("Failed to persist flow before the dialog", exc_info=True)
    # The watcher polls the moment it starts, so an interjection that landed in
    # the gap between two steps is drained as soon as the NEXT step is entered —
    # and that step can be a CONFIRM gate, which has no session of its own and
    # is not what the operator is asking about. Resolving the subject here (as
    # the pause-point entry point already does) points the conversation at the
    # step that produced the artefact under review, and parks any one-shot note
    # on the step id the gate's Retry actually consumes.
    subject = _dialog_subject_step(flow, current_step)
    apply_step = None
    if subject is not current_step:
        if interrupted:
            # INVARIANT: the gate reading applies only when the gate actually
            # REACHED its wait. A CONFIRM cut off INSIDE ``run_step`` — the
            # watcher escalating an interjection that was already on disk when
            # the step started — published nothing to go back to, so its
            # ``continue`` is an ordinary retry of the CONFIRM step. The
            # conversation still belongs to the producer's session (the gate has
            # none), so only the step the decision is APPLIED to moves back.
            apply_step = current_step
        else:
            # The stop landed on a CONFIRM gate that had reached PAUSED.
            # ``continue`` there means "go back to waiting at this gate" — the
            # same semantics the pause-point entry point uses — not "re-run the
            # reviewed producer as a retry", which would discard an approval
            # decision nobody made.
            pause_context = "confirm"
    if output_format == "json":
        return _run_interjection_dialog_noninteractive(
            flow, subject, persistence, project_root,
            initial_messages=messages,
            pause_context=pause_context,
            call_step=current_step,
            apply_step=apply_step,
        )
    return _run_interjection_dialog(
        flow, subject, persistence, project_root, prompt_history,
        initial_messages=messages,
        pause_context=pause_context,
        call_step=current_step,
        apply_step=apply_step,
    )


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
    plan_decomposition: Optional[str] = None,
    plan_granularity: Optional[str] = None,
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

    # Build the unified event stream and hang the outermost sink. ``luo run``
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
            # Header-first lazy resume (issue #244 B4). ``load_flow_by_id``
            # resolves the active engine.json first, else the per-flow resumable
            # snapshot (resumable/<flow_id>.json) when engine.json has since been
            # overwritten by a later ``luo run`` — and in BOTH cases reads only
            # the KB-scale header, faulting in each step's cold body on first
            # keyed access. Resuming a paused/interrupted flow with many large
            # completed step cold files therefore no longer re-materializes every
            # step payload before reaching the current step (the whole point of
            # the hot/cold split); the eager ``load_flow`` reconstruct used here
            # before defeated it. A normally COMPLETED flow has no snapshot
            # (cleared on completion), so it is never resurrected via the snapshot.
            #
            # Peek the active engine.json's flow_id (size-guarded header read)
            # before loading so we know whether the flow was recovered from its
            # snapshot and must be written back as the live engine.json.
            recovered_from_snapshot = (
                str(persistence._peek_active_flow_id() or "") != str(flow_id)
            )
            flow = persistence.load_flow_by_id(flow_id)
            if not flow:
                display_error(t("cli.run.error.flow_not_found", flow_id=flow_id))
                return 1

            # Scope this process's interjection drains to the flow it resumes:
            # a call queued for a different flow that once occupied this root
            # must stay untouched rather than be opened as this flow's dialog.
            from ..engine import interaction_calls as _ic_bind

            _ic_bind.bind_active_flow(flow.flow_id)

            # A COMPLETED flow is terminal and must not be resumed, regardless of
            # whether it came from the active engine.json or a stale per-flow
            # snapshot under tianluo/state/resumable/. This mirrors the
            # daemon/server/frontend completed-flow guard so the CLI resume path
            # agrees with the rest of the stack. Guard BEFORE persisting a
            # recovered snapshot so a stale COMPLETED snapshot is never
            # re-materialized as the live engine.json.
            if flow.status == FlowStatus.COMPLETED:
                display_error(t("cli.run.error.flow_completed", flow_id=flow_id))
                return 1

            # Recovered from its per-flow resumable snapshot (engine.json holds a
            # different/absent flow): write it back as the live engine.json so the
            # resume bookkeeping below, and the daemon's single-slot
            # observability, both see a live flow again. The write is header-only
            # for the lazily-loaded steps (their unchanged cold bodies already sit
            # in this flow's steps/<flow_id>/ partition, shared with the snapshot).
            if recovered_from_snapshot:
                persistence.save_flow(flow)

            # A rewind whose target could not be rebuilt fails the flow with
            # the rebuild request still armed (the main loop pops
            # ``pending_rewind_step_type`` only after a successful rebuild).
            # Resume must re-enter the loop so the rebuild is retried: left
            # FAILED, the loop body never runs and the flow is bricked — the
            # target and its successors are already deleted and nothing would
            # ever re-create them. The current step is the target's COMPLETED
            # predecessor, so none of the step re-arm branches below fire.
            if (
                flow.status == FlowStatus.FAILED
                and flow.state.context.get("pending_rewind_step_type")
            ):
                logger.info(
                    "Resuming into a pending rewind rebuild (%s)",
                    flow.state.context["pending_rewind_step_type"],
                )
                flow.status = FlowStatus.RUNNING
                persistence.save_flow(flow)

            # Detect and handle resume of a RUNNING or FAILED step.
            #
            # INVARIANT: a resume that was triggered by an interjection (or that
            # lands on a dialog still awaiting a web reply) does NOT re-arm the
            # step. The dialog owns what happens next — it re-arms the step
            # itself on ``continue`` (so re-arming here would double the retry
            # counter), and at a failure gate it must be able to hand the
            # operator back the Retry/Skip/Abort choice, which is only reachable
            # while the step is still FAILED. Re-arming here instead spawned an
            # unrequested retry of the very step the operator was asking about.
            current_step = flow.state.get_current_step()
            dialog_owns_step = _dialog_state(flow) is not None
            if not dialog_owns_step:
                try:
                    from ..engine import interaction_calls as _ic

                    dialog_owns_step = _ic.has_pending_interjections(
                        project_root, flow.flow_id
                    )
                except Exception:  # noqa: BLE001 - never block a resume
                    logger.debug("Failed to peek pending interjections", exc_info=True)
            if dialog_owns_step and current_step and current_step.status in (
                StepStatus.RUNNING, StepStatus.FAILED
            ):
                logger.info(
                    "Resume triggered while an interjection is pending; leaving "
                    "step %s %s for the dialog",
                    current_step.step_id, current_step.status.value,
                )
                if flow.status == FlowStatus.FAILED:
                    # The step keeps its FAILED status (that is what routes it
                    # to the Retry/Skip/Abort gate), but the FLOW has to be
                    # running for the loop to get there at all.
                    flow.status = FlowStatus.RUNNING
                    persistence.save_flow(flow)
            elif current_step and current_step.status == StepStatus.RUNNING:
                # Step was interrupted - prepare for resumption with context
                # (``resumed`` + ONE retry_count increment, so LLMCaller picks
                # up the interrupted run's conversation).
                _rearm_resumed_step(current_step)
                logger.info(f"Resuming interrupted step: {current_step.step_id} ({current_step.step_type.value})")
                persistence.save_flow(flow)
            elif current_step and current_step.status == StepStatus.FAILED:
                # Step failed - prepare for retry from breakpoint.
                #
                # This IS the failure gate's Retry in json/daemon mode: the web
                # answer to the retry_decision call is what made the daemon
                # re-spawn `--resume`, and the re-arm below is the execution
                # launched straight out of that pause. So it is also the one
                # consumer of a note parked there (decision 4) — without this,
                # an instruction confirmed at the gate would be delivered at an
                # interactive terminal and silently dropped under the daemon.
                #
                # INVARIANT: only the gate's *Retry* re-arms the step, and only
                # Retry consumes a note parked there. A `skip` or `abort` answer
                # resolves the pause WITHOUT the execution it was scoped to, so
                # the step must stay FAILED and fall straight through to the
                # failure-decision path (which owns consuming the answer and
                # applying it). Re-arming on those answers ran the step one more
                # time first — and a rerun that happened to succeed swallowed the
                # operator's Skip/Abort entirely, so the two channels disagreed
                # on the same resolution. The peek must not consume the response:
                # `_resolve_step_failure_action` still owns applying it.
                from ..engine.interjection_dialog import (
                    consume_gate_note,
                    discard_gate_note,
                )

                gate_answer = _peek_failure_gate_decision(
                    project_root, current_step.step_id
                )
                if gate_answer in ("skip", "abort"):
                    discard_gate_note(flow)
                    logger.info(
                        "Resuming failed step %s (%s) into its %s decision "
                        "without re-running it",
                        current_step.step_id, current_step.step_type.value,
                        gate_answer,
                    )
                else:
                    _rearm_resumed_step(current_step)
                    consume_gate_note(flow, current_step)
                    logger.info(f"Retrying failed step from breakpoint: {current_step.step_id} ({current_step.step_type.value})")
                flow.status = FlowStatus.RUNNING
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
            render_full(
                t(
                    "cli.run.flow_info.body",
                    flow_id=flow.flow_id,
                    current_step=flow.state.current_step_id,
                    task=flow.task_description,
                ),
                title=t("cli.run.flow_info.title"),
            )
        else:
            if not task_description:
                display_error(t("cli.run.error.task_required"))
                return 1

            flow = state_machine.create_flow(
                task_description=task_description,
                task_type=task_type,
                change_name=change_name,
                is_worktree_mode=is_worktree_mode,
                plan_decomposition=plan_decomposition,
                plan_granularity=plan_granularity,
            )

            from ..engine import interaction_calls as _ic_bind

            _ic_bind.bind_active_flow(flow.flow_id)

            # Set source issue ID if provided
            if source_issue_id:
                flow.source_issue_id = source_issue_id

            # Record the worktree-isolation metadata on a new --worktree flow so
            # it persists in the worktree's engine.json. This lets a later
            # `luo run --resume` from the main repo discover the run, re-dispatch
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
                t("cli.run.new_flow.created", flow_id=flow.flow_id),
                t("cli.run.new_flow.task", task=task_description),
            ]

            # Only show type if explicitly provided (pending is auto-detect)
            if task_type and task_type != "pending":
                content.append(t("cli.run.new_flow.type_user", task_type=task_type))
            else:
                content.append(t("cli.run.new_flow.type_pending"))

            if change_name:
                content.append(t("cli.run.new_flow.change", change_name=change_name))
            render_full("\n".join(content), title=t("cli.run.new_flow.title"))

        # Initialize flow metadata and baseline commit (idempotent — safe for both
        # new and resumed flows).
        state_machine.init_flow(flow)
    except ConfigError as exc:
        display_error(str(exc))
        return 2

    # The flow id exists only now for a new run, so fill it into the run.pid
    # marker written at startup: another machine's resume guard reads that
    # record to decide whether IT is the flow being resumed (refuse + point at
    # end-session here) or merely a co-tenant of this root (refuse without
    # naming an unrelated session as the culprit).
    _stamp_run_pidfile_flow(persistence, flow.flow_id)

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
        # WHY there is no step-boundary interjection drain any more: an
        # interjection now has exactly two doors — interrupt the running call
        # and open the dialog, or (at a pause point) open the dialog / become
        # the paused conversation's next reply. Silently folding one into the
        # next step's task description was the third door, and it is the one
        # that gave the user no answer and no confirmation.

        # A rewind deleted its target step object, so the target has to be
        # rebuilt before the loop looks for a current step — otherwise a rewind
        # to the FIRST step (no current step left at all) would read as "flow
        # finished", and a rewind to a later one would advance past the target
        # because the step before it is COMPLETED.
        #
        # INVARIANT: the marker is popped only AFTER a successful rebuild. A
        # rebuild that raises fails the flow with the marker still armed, and
        # the resume path re-enters this loop (FAILED -> RUNNING when the
        # marker is present) so ``luo run --resume`` retries the rebuild —
        # without it the flow would be bricked: the target and its successors
        # are already deleted, and nothing would ever re-create them.
        _rewind_type = flow.state.context.get("pending_rewind_step_type")
        if _rewind_type:
            try:
                state_machine.rebuild_rewound_step(flow, StepType(_rewind_type))
            except Exception as exc:
                # INVARIANT: a rewind that cannot rebuild its target FAILS the
                # flow. After the rewind, ``current_step_id`` names the target's
                # COMPLETED predecessor and ``current_step_index`` names the
                # target's slot, so simply continuing hands the loop to
                # ``transition_to_next`` and it advances PAST the target — the
                # step the user explicitly asked to re-run would never run, and
                # the flow would report success having skipped it. Failing
                # loudly keeps the state on disk exactly where the operator can
                # fix the cause (e.g. unreadable upstream outputs) and resume.
                logger.exception(
                    "Failed to rebuild the rewound step %s", _rewind_type,
                )
                display_error(
                    t(
                        "cli.run.rewind.rebuild_failed",
                        step_type=str(_rewind_type),
                        error=str(exc),
                    )
                )
                flow.status = FlowStatus.FAILED
                persistence.save_flow(flow)
                _finalize_sync_source_issue(
                    project_root, flow, is_worktree_mode, resolved=False
                )
                return 1
            flow.state.context.pop("pending_rewind_step_type", None)
            persistence.save_flow(flow)

        # A dialog left open by a previous process (the flow exited PAUSED
        # waiting for a reply) is resumed before anything else: it owns the
        # flow until it settles. A daemon/json run continues it through the
        # call-file channel; an interactive ``--resume`` takes the same paused
        # conversation over at the terminal — transcript and pending decision
        # included — so the operator is never told "answer it on the web" by a
        # silent exit.
        dialog_state = _dialog_state(flow)
        if dialog_state is not None:
            dialog_step = flow.state.steps.get(dialog_state.get("step_id")) or (
                flow.state.get_current_step()
            )
            if dialog_step is None:
                flow.state.context.pop("active_dialog", None)
            else:
                call_step = flow.state.steps.get(
                    dialog_state.get("call_step_id") or ""
                ) or dialog_step
                if output_format != "json" and _stdin_is_interactive():
                    flow.state.context.pop("active_dialog", None)
                    persistence.save_flow(flow)
                    outcome = _run_interjection_dialog(
                        flow, dialog_step, persistence, project_root,
                        prompt_history,
                        pause_context=dialog_state.get("pause_context"),
                        call_step=call_step,
                        prior_state=dialog_state,
                    )
                    if outcome in (_DIALOG_EXIT, _DIALOG_AWAITING_WEB):
                        emitter.emit(new_event(
                            EventType.FLOW_PAUSED, flow_id=flow.flow_id,
                            step_id=dialog_step.step_id,
                            step_type=(
                                dialog_step.step_type.value
                                if hasattr(dialog_step.step_type, "value")
                                else str(dialog_step.step_type)
                            ),
                        ))
                        return 0 if outcome == _DIALOG_AWAITING_WEB else 130
                    flow.status = FlowStatus.RUNNING
                    persistence.save_flow(flow)
                    continue
                outcome = _run_interjection_dialog_noninteractive(
                    flow, dialog_step, persistence, project_root
                )
                if outcome == _DIALOG_AWAITING_WEB:
                    emitter.emit(new_event(
                        EventType.FLOW_PAUSED, flow_id=flow.flow_id,
                        step_id=dialog_step.step_id,
                        step_type=(
                            dialog_step.step_type.value
                            if hasattr(dialog_step.step_type, "value")
                            else str(dialog_step.step_type)
                        ),
                    ))
                    return 0
                if outcome == _DIALOG_EXIT:
                    flow.status = FlowStatus.PAUSED
                    persistence.save_flow(flow)
                    emitter.emit(new_event(
                        EventType.FLOW_PAUSED, flow_id=flow.flow_id,
                    ))
                    return 0
                if outcome == _DIALOG_RESUME_PAUSE and _gate_call_is_pending(
                    project_root, call_step
                ):
                    # A confirmed ``continue`` at a gate whose call is still
                    # unanswered goes back to THAT wait. The wait lives in the
                    # call file, not in this process, so returning to it means
                    # exiting with the gate's step exactly as the dialog found
                    # it — re-entering the loop would re-run the CONFIRM
                    # handler, flip the step to RUNNING and publish a duplicate
                    # call for a gate the operator never resolved (and a crash
                    # in that window would record the gate as an interrupted
                    # running step).
                    flow.status = FlowStatus.PAUSED
                    persistence.save_flow(flow)
                    emitter.emit(new_event(
                        EventType.FLOW_PAUSED, flow_id=flow.flow_id,
                        step_id=call_step.step_id,
                        step_type=(
                            call_step.step_type.value
                            if hasattr(call_step.step_type, "value")
                            else str(call_step.step_type)
                        ),
                    ))
                    return 0
                flow.status = FlowStatus.RUNNING
                persistence.save_flow(flow)
                continue

        current_step = flow.state.get_current_step()
        if not current_step:
            get_console().print(t("cli.run.no_current_step"))
            _complete_flow_via_fallback(flow)
            persistence.save_flow(flow)
            _reclaim_review_snapshots(flow)
            break

        # An interjection queued while a step sits PAUSED — or while it was
        # RUNNING in a process that has since exited — opens the dialog here.
        # DISCOVERY is excluded on purpose: its own pause handler consumes the
        # text as the conversation's next reply (decision 5), and stealing it
        # here would nest a second conversation inside the first.
        if (
            current_step.status in (StepStatus.PAUSED, StepStatus.RUNNING)
            and current_step.step_type != StepType.DISCOVERY
        ):
            _mid_step = current_step.status == StepStatus.RUNNING
            _paused_msgs = _collect_pending_dialog_messages(project_root)
            if _paused_msgs:
                outcome = _dialog_at_pause_point(
                    flow, current_step, persistence, project_root, prompt_history,
                    initial_messages=_paused_msgs,
                    # A step left RUNNING was cut off mid-call, not stopped at a
                    # gate: ``continue`` there means "re-run this step as a
                    # retry", which is also what performs the single retry-count
                    # increment (the resume path deliberately did not).
                    pause_context=None if _mid_step else "confirm",
                    output_format=output_format,
                )
                if outcome == _DIALOG_RESTARTED:
                    continue
                if outcome in (_DIALOG_EXIT, _DIALOG_AWAITING_WEB):
                    emitter.emit(new_event(
                        EventType.FLOW_PAUSED, flow_id=flow.flow_id,
                        step_id=current_step.step_id,
                        step_type=current_step.step_type.value,
                    ))
                    return 0 if outcome == _DIALOG_AWAITING_WEB else 130
            elif _mid_step:
                # The interjection was answered elsewhere between the resume
                # peek and this drain: fall back to the ordinary resume re-arm
                # so the interrupted step continues as a retry rather than as a
                # brand-new call.
                _rearm_resumed_step(current_step)
                persistence.save_flow(flow)

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
                project_root, flow.flow_id, current_step.step_id
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
            # Advancing RESOLVES the pause the note was parked at. In the
            # interactive CLI the same sequence goes through normal result
            # processing, which discards it; this fast path is the json/daemon
            # counterpart and must behave identically, or a note parked by a
            # ``continue`` at a ``completed_step`` pause survives to be consumed
            # by a later, unrelated gate Retry on the same step.
            from ..engine.interjection_dialog import discard_gate_note

            discard_gate_note(flow)
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
                t("cli.run.revision_resuming", step_type=current_step.step_type.value)
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
                    project_root, flow.flow_id, current_step.step_id
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
            # AUDIT (return-130 non-interactive reachability, issue: CONFIRM json
            # pause): triggered only by an operator Ctrl+C while blocked on the
            # main-worktree lock. A daemon-spawned json run has no TTY and never
            # receives KeyboardInterrupt, so this exit is unreachable in json
            # mode — no "non-interactive exit left status=running" bug here.
            # The flow is no longer actively queued for the lock once the
            # process exits, so clear waiting_for_lock before persisting.
            # Otherwise engine.json records status=running + waiting_for_lock=True
            # for a dead process, and the daemon/web console would keep rendering
            # it as a live "running · waiting for lock" flow until a manual
            # `luo run --resume` re-acquires and clears the flag.
            flow.waiting_for_lock = False
            persistence.save_flow(flow)
            emitter.emit(new_event(
                EventType.FLOW_PAUSED, flow_id=flow.flow_id,
                step_id=current_step.step_id,
                step_type=current_step.step_type.value,
            ))
            get_console().print(t("cli.run.interrupted_waiting_lock"))
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
                type_suffix = t("cli.run.pending_suffix")

            # The header is user-facing chrome, so it shows the localized step
            # title; step_type_value stays raw for the event stream below.
            from ..engine.step_renderers import step_display_title
            step_title = step_display_title(current_step.step_type)

            console = get_console()
            console.print(Rule(f"[bold]{step_title}[/bold]{type_suffix}", style="cyan"))

        step_start_time = datetime.now()

        # Track whether state_machine.run_step was actually invoked this
        # iteration.  When a PAUSED discovery step is resumed, run_step is
        # skipped and the previous round's stale token_usage stays in
        # step.outputs — emitting STEP_OUTPUT for that stale data would
        # duplicate the CLI usage block and append a zombie usage chip to
        # the web history.  Only emit STEP_OUTPUT when run_step was called
        # (meaning a fresh token-consuming LLM round actually happened).
        step_ran_llm = True

        # A discovery reply taken off the stop signal (an interjection that
        # landed as the round paused). Set below, consumed by the discovery
        # pause handling instead of prompting for an answer already given.
        pending_discovery_reply: Optional[str] = None

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
                get_console().print(t("cli.run.confirm.found_existing", value=existing_result.value))
                result = existing_result
                # No LLM call — the user response was already on disk.
                step_ran_llm = False
            elif _gate_call_is_pending(project_root, current_step):
                # INVARIANT: the gate's published, still-unanswered call IS the
                # wait. Whatever re-entered the loop — a plain ``--resume``, or
                # an interactive takeover of a dialog whose confirmed
                # ``continue`` means "go back to this same pause point" — the
                # gate must be re-presented without re-running it: ``run_step``
                # would flip the step PAUSED -> RUNNING (and a crash in that
                # window would record the gate as an interrupted running step)
                # for a handler that can only hand back the PAUSED it already
                # persisted.
                result = StepStatus.PAUSED
                step_ran_llm = False
            else:
                result, stopped, interrupted = _execute_step_with_interjections(
                    state_machine, flow, current_step, project_root,
                    _emit_step_started,
                )
                if stopped:
                    outcome = _open_dialog_after_stop(
                        flow, current_step, persistence, project_root,
                        prompt_history, output_format, interrupted=interrupted,
                    )
                    exit_code = _dialog_outcome_exit_code(
                        outcome, flow, persistence, emitter, current_step,
                        step_type_value,
                    )
                    if exit_code is not None:
                        return exit_code
                    # _DIALOG_RESUME_PAUSE only comes back when the step had
                    # already produced its result: "continue" there means carry
                    # on from here, so the result is processed normally instead
                    # of the step being re-entered.
                    if outcome != _DIALOG_RESUME_PAUSE:
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

        elif current_step.status == StepStatus.FAILED:
            # A bounded holistic IMPLEMENT continuation persists FAILED (with
            # an error message) via transition_to_next instead of re-running
            # the agent. Route that persisted failure into the Retry/Skip/Abort
            # decision path below without re-invoking the handler — run_step
            # has no FAILED-status guard and would treat the step as RUNNING.
            result = StepStatus.FAILED
            step_ran_llm = False

        elif current_step.status == StepStatus.COMPLETED:
            # The step is finished but its result was never processed: a dialog
            # opened as it was completing paused the flow (json/daemon mode
            # exits between dialog rounds) before the terminal event and the
            # transition. ``run_step`` has no COMPLETED guard and would re-run
            # the whole step — destroying work nobody asked to discard — so the
            # persisted result is processed instead.
            result = StepStatus.COMPLETED
            step_ran_llm = False

        else:
            result, stopped, interrupted = _execute_step_with_interjections(
                state_machine, flow, current_step, project_root,
                _emit_step_started,
            )
            if stopped:
                # An interjection that landed as DISCOVERY was pausing is that
                # conversation's next reply, not a request for a second one —
                # it is routed straight into the discovery round below.
                pending_discovery_reply = _stop_request_as_discovery_reply(
                    current_step, result, interrupted
                )
                if pending_discovery_reply is not None:
                    stopped = False
            if stopped:
                # Ctrl-C or a web interjection cut the call short: open the
                # mid-flow dialog at the breakpoint (both entry points share
                # this one path — decision 5).
                outcome = _open_dialog_after_stop(
                    flow, current_step, persistence, project_root,
                    prompt_history, output_format, interrupted=interrupted,
                )
                exit_code = _dialog_outcome_exit_code(
                    outcome, flow, persistence, emitter, current_step,
                    step_type_value,
                )
                if exit_code is not None:
                    return exit_code
                # See the CONFIRM branch: a pause-style outcome means the step
                # already finished, so fall through to its normal result
                # handling rather than re-running it.
                if outcome != _DIALOG_RESUME_PAUSE:
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
        #
        # INVARIANT: a failure is announced ONCE per execution. A pause-point
        # dialog's ``continue`` means "go back to the Retry/Skip/Abort wait", so
        # the loop turns again with the step still FAILED and no handler ever
        # re-run — replaying its terminal event there appends a second
        # step_failed record (duplicate failure card, double-counted step usage)
        # for one failure. The marker is cleared by ``run_step``, so a genuine
        # re-failure after a Retry announces itself normally, and it lives in
        # the step's inputs so the json/daemon resume path — which comes back
        # into this same still-FAILED step — obeys the same rule.
        failure_already_announced = bool(
            (current_step.inputs or {}).get("failure_announced")
        )
        if result in (StepStatus.COMPLETED, StepStatus.PARTIAL, StepStatus.FAILED):
            step_event_type = (
                EventType.STEP_FAILED
                if result == StepStatus.FAILED
                else EventType.STEP_COMPLETED
            )
            if not (result == StepStatus.FAILED and failure_already_announced):
                emitter.emit(new_event(
                    step_event_type,
                    flow_id=flow.flow_id,
                    step_id=current_step.step_id,
                    step_type=step_type_value,
                    step=current_step,
                ))
            if result == StepStatus.FAILED:
                current_step.inputs["failure_announced"] = True

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
            if output_format == "json":
                # Non-interactive (daemon spawn): the confirm handler already
                # wrote the confirm_*.json call file, so there is nothing to
                # prompt for — persist FlowStatus.PAUSED and exit 0 so the
                # daemon re-spawns --resume once the web answer lands. Without
                # this the process would return 130 with the top-level status
                # still "running", so _resume_paused_flow's status == "PAUSED"
                # check never fires and the answered confirm is never consumed
                # ("approved but nothing happens"). Mirrors the DISCOVERY json
                # branch below.
                _handle_confirm_pause_noninteractive(
                    flow, current_step, persistence, project_root
                )
                emitter.emit(new_event(
                    EventType.FLOW_PAUSED, flow_id=flow.flow_id,
                    step_id=current_step.step_id, step_type=step_type_value,
                ))
                return 0
            confirm_result = _handle_confirm_pause(flow, current_step, persistence, project_root, prompt_history)
            if confirm_result == _DIALOG_RESTARTED:
                # An interjection dialog at this gate rewound the flow; the
                # CONFIRM step no longer exists, so just re-enter the loop.
                continue
            if confirm_result is None or confirm_result == _DIALOG_EXIT:
                # User chose to exit. This is an interactive Ctrl+C / explicit
                # "Exit" choice (return 130 distinguishes a user-initiated exit
                # from a normal pause) — unreachable in json/daemon mode, which
                # takes the branch above and never prompts.
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
            if pending_discovery_reply is not None:
                # The operator already answered — from the web, in the instant
                # this round was pausing. Prompting again (terminal or call
                # file) would ask them to retype what they just sent.
                user_response = pending_discovery_reply
            elif output_format == "json":
                # Non-interactive (daemon spawn): write the clarifying question
                # as a tianluo/calls/ call file and let the web answer it through
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
                # pause is mirrored to a tianluo/calls/ call file so the web console
                # can answer it too; terminal + web are awaited in parallel and
                # whichever answers first drives this same live process. The
                # flow stays RUNNING (never PAUSED) so the daemon does not spawn
                # a duplicate --resume against the live interactive process.
                user_response = _handle_discovery_pause(
                    flow, current_step, persistence, prompt_history, project_root
                )

                if user_response is None:
                    # User chose to exit. AUDIT (return-130 non-interactive
                    # reachability): this is the *interactive* discovery branch
                    # (output_format != "json"); the json branch above handles
                    # the daemon path and persists PAUSED + returns 0. A daemon
                    # run never enters this else, so this 130 exit is unreachable
                    # in json mode — no missing-PAUSED defect.
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
            error_msg = current_step.error_message or t("cli.run.error.unknown")
            # Same single-announcement rule as the terminal event above: coming
            # back to this gate from a dialog must not re-print the failure the
            # operator is already looking at.
            if not failure_already_announced:
                display_error(t("cli.run.error.step_failed", error=error_msg))

            max_retries = 3
            if current_step.retry_count >= max_retries:
                display_error(
                    t(
                        "cli.run.error.max_retries",
                        max_retries=max_retries,
                        step_type=current_step.step_type.value,
                    )
                )
                # Auto-fail: exit without asking user (no FLOW_PAUSED, no
                # decision chip, no prompt — unchanged from the prior behavior).
                flow.status = FlowStatus.FAILED
                persistence.save_flow(flow)
                # This is one of the two most common FAILED exits (the other is
                # the Abort decision below); both return early from inside the
                # step loop and never reach the bottom terminal FAILED branch,
                # so a synchronous from-issue run would otherwise leave its
                # source issue stranded IN_PROGRESS. Finalize here off the
                # persisted terminal status (worktree flows are a no-op — the
                # wrapper handles their FAILED→open off the same persisted state).
                _finalize_sync_source_issue(
                    project_root, flow, is_worktree_mode, resolved=False
                )
                return 1

            # Dual-channel failure pause. _resolve_step_failure_action
            # unconditionally writes the retry_decision call file (so the web
            # console shows a Retry/Skip/Abort chip), then routes:
            #   * "decision" — an answer is already on disk (resume / webui);
            #   * "race"     — interactive: race the CLI prompt vs. the webui
            #                  response (whoever answers first wins);
            #   * "pause"    — non-interactive: pause for an out-of-band answer.
            # An interjection queued while the step was failing is a question
            # about the failure, so it opens the mid-flow dialog before the
            # Retry/Skip/Abort gate is even presented (decision 5). ``continue``
            # from that dialog falls through to the gate as normal.
            pending_failure_msgs = _collect_pending_dialog_messages(project_root)
            if pending_failure_msgs:
                dialog_outcome = _dialog_at_pause_point(
                    flow, current_step, persistence, project_root, prompt_history,
                    initial_messages=pending_failure_msgs,
                    pause_context="failure",
                    output_format=output_format,
                )
                if dialog_outcome == _DIALOG_RESTARTED:
                    continue
                if dialog_outcome in (_DIALOG_EXIT, _DIALOG_AWAITING_WEB):
                    emitter.emit(new_event(
                        EventType.FLOW_PAUSED, flow_id=flow.flow_id,
                        step_id=current_step.step_id, step_type=step_type_value,
                    ))
                    if dialog_outcome == _DIALOG_AWAITING_WEB:
                        return 0
                    return 130

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
                    t("cli.run.failure.paused_non_interactive", call_name=Path(info).name)
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
                options = [
                    t("cli.run.failure.opt_retry"),
                    t("cli.run.failure.opt_skip"),
                    t("cli.run.failure.opt_abort"),
                ]
                gate_interjections: List[str] = []
                _source, choice = _await_terminal_or_web_choice(
                    info, message=t("cli.run.what_to_do"), options=options,
                    interjection_sink=gate_interjections,
                    project_root=project_root,
                )
                if _source == _FAILURE_SRC_INTERJECT:
                    # An interjection that landed while the menu was on screen
                    # is the same event as a Ctrl-C here: it opens the dialog,
                    # and the gate is re-presented afterwards.
                    dialog_outcome = _dialog_at_pause_point(
                        flow, current_step, persistence, project_root,
                        prompt_history,
                        initial_messages=gate_interjections,
                        pause_context="failure",
                        output_format=output_format,
                    )
                    if dialog_outcome == _DIALOG_RESTARTED:
                        continue
                    if dialog_outcome in (_DIALOG_EXIT, _DIALOG_AWAITING_WEB):
                        emitter.emit(new_event(
                            EventType.FLOW_PAUSED, flow_id=flow.flow_id,
                            step_id=current_step.step_id,
                            step_type=step_type_value,
                        ))
                        if dialog_outcome == _DIALOG_AWAITING_WEB:
                            return 0
                        return 130
                    continue
                if choice is None:
                    # Ctrl-C at the failure gate opens the dialog instead of
                    # silently aborting: an operator who interrupts here wants
                    # to understand the failure, and abort-by-default threw
                    # away the flow without ever answering that.
                    dialog_outcome = _dialog_at_pause_point(
                        flow, current_step, persistence, project_root,
                        prompt_history, pause_context="failure",
                    )
                    if dialog_outcome == _DIALOG_RESTARTED:
                        continue
                    if dialog_outcome == _DIALOG_EXIT:
                        emitter.emit(new_event(
                            EventType.FLOW_PAUSED, flow_id=flow.flow_id,
                            step_id=current_step.step_id,
                            step_type=step_type_value,
                        ))
                        return 130
                    # ``continue`` from the dialog means "back to this gate":
                    # re-run the loop iteration so the choice is presented
                    # again, now informed by the conversation.
                    continue

            if choice == 0:
                # Reset step status and retry from where it left off. This is
                # the ONE consumer of a gate-parked dialog note (decision 4):
                # the instruction the operator confirmed at this pause rides
                # this re-run and is then gone.
                from ..engine.interjection_dialog import (
                    consume_gate_note,
                    discard_gate_note,
                )

                consume_gate_note(flow, current_step)
                current_step.status = StepStatus.PENDING
                current_step.inputs["resumed"] = True
                current_step.inputs["retry_count"] = current_step.inputs.get("retry_count", 0) + 1
                current_step.retry_count += 1
                persistence.save_flow(flow)
                continue
            elif choice == 1:
                from ..engine.interjection_dialog import discard_gate_note

                discard_gate_note(flow)
                # Force step to completed so transition works
                current_step.status = StepStatus.COMPLETED
                # A holistic IMPLEMENT still carries its partial record in
                # outputs (completion_status='partial' / incomplete_tasks);
                # the transition's continuation gate would re-capture it and
                # either re-present the same failure prompt or re-run the
                # agent. Skip is an explicit human decision — mark the step
                # so the gate (one-shot, consumed on transition) advances
                # past it without re-arming.
                current_step.inputs["holistic_skip_forced"] = True
                state_machine.transition_to_next(flow)
                persistence.save_flow(flow)
                continue
            else:
                from ..engine.interjection_dialog import discard_gate_note

                discard_gate_note(flow)
                flow.status = FlowStatus.FAILED
                persistence.save_flow(flow)
                # 'Abort flow' also returns early from inside the step loop and
                # bypasses the bottom terminal branch; finalize the source issue
                # here so a synchronous from-issue abort returns it to OPEN
                # (worktree flows are a no-op — see the max-retries branch above).
                _finalize_sync_source_issue(
                    project_root, flow, is_worktree_mode, resolved=False
                )
                return 1

        # Handle REVISION_NEEDED status from CONFIRM step
        if result == StepStatus.REVISION_NEEDED:
            get_console().print(t("cli.run.revision_requested"))
            # Mark the CONFIRM step as completed with revision info
            current_step.status = StepStatus.REVISION_NEEDED
            # Transition will handle going back to the previous step
            state_machine.transition_to_next(flow)
            # Same reason as the transition at the bottom of the loop: routing
            # back for a revision is a fresh run of the target step, not the
            # failure-gate Retry a parked note is scoped to, so the note dies
            # here rather than surfacing in a later, unrelated pause.
            from ..engine.interjection_dialog import discard_gate_note

            discard_gate_note(flow)
            persistence.save_flow(flow)
            continue

        step_duration = (datetime.now() - step_start_time).total_seconds()
        console = get_console()
        console.print(
            t(
                "cli.run.step_completed",
                step_type=current_step.step_type.value,
                duration=step_duration,
            )
        )

        # Transition to next step. Advancing past a step also ends any pause
        # that was holding a one-shot dialog note for it (the completed-step
        # pseudo-pause above all) — the note's only consumer is a failure
        # gate's Retry, and a step the flow walked past has none coming.
        state_machine.transition_to_next(flow)
        from ..engine.interjection_dialog import discard_gate_note

        discard_gate_note(flow)
        persistence.save_flow(flow)

    # Flow complete. Emit the terminal flow event (no-op in CliSink — the
    # human-facing summary line below is rendered as before; forwarded by
    # JsonSink for the daemon).
    if flow.status == FlowStatus.COMPLETED:
        emitter.emit(new_event(
            EventType.FLOW_COMPLETED, flow_id=flow.flow_id,
        ))
        display_success(t("cli.run.flow_completed"))
        # Synchronous (non-worktree) from-issue finalization: the source issue
        # is resolved here, at the flow's true terminal state, rather than in
        # the cli.py wrapper. This is the ONLY point common to both the first
        # run and a daemon/`--resume` continuation (which re-enters via
        # resume_run→run_flow without the wrapper), and `flow.source_issue_id`
        # is restored from engine.json on resume, so it is process-independent.
        # Worktree flows defer resolve to the trailing merge (run_merge), so
        # they are skipped here.
        _finalize_sync_source_issue(project_root, flow, is_worktree_mode, resolved=True)
        # Session-level usage/cost summary from the shared UsageSummary
        # backend. Renders nothing when the flow consumed no LLM calls.
        _render_session_usage_summary(flow, project_root)
        return 0
    elif flow.status == FlowStatus.FAILED:
        current_step = flow.state.get_current_step()
        error_msg = (current_step.error_message if current_step else None) or t(
            "cli.run.error.unknown"
        )
        emitter.emit(new_event(
            EventType.FLOW_FAILED, flow_id=flow.flow_id, message=error_msg,
        ))
        display_error(t("cli.run.error.flow_failed", error=error_msg))
        # Failed sync from-issue run: return the source issue to OPEN (existing
        # semantics), now also covering the resume path.
        _finalize_sync_source_issue(project_root, flow, is_worktree_mode, resolved=False)
        # Still surface whatever tokens/cost were consumed before the failure.
        _render_session_usage_summary(flow, project_root)
        return 1
    else:
        get_console().print(t("cli.run.flow_ended_status", status=flow.status.value))
        return 0


def _render_session_usage_summary(flow: Any, project_root: Path) -> None:
    """Render the session usage/cost summary from the shared UsageSummary backend.

    The CLI shows the same actual / estimated / unknown classification that
    step outputs and history payloads derive from the same records + pricing
    catalog — nothing here re-sums or re-prices independently. A flow with no
    recoverable per-call records (older schema) falls back to the legacy
    five-field block so usage never disappears on upgrade.
    """
    from ..usage import UsageSummary

    records = list(getattr(flow.state, "session_usage_records", None) or [])
    if not records:
        records = list(flow.state.session_token_usage.usage_records)
    if records:
        try:
            from ..config import load_pricing_catalog

            catalog = load_pricing_catalog(project_root)
        except Exception:
            logger.debug(
                "Failed to load pricing catalog for session summary",
                exc_info=True,
            )
            # Degrade to the builtin catalog, matching history_cmd and the
            # daemon usage backend: a different fallback here would make the
            # end-of-flow summary and `luo history show` of the very same flow
            # print contradictory estimates from the shared backend.
            from ..pricing import PricingCatalog

            catalog = PricingCatalog.builtin()
        render_usage_summary_block(
            UsageSummary.summarize(records, catalog=catalog),
            title=t("cli.run.session_usage_title"),
        )
        return
    # Legacy fallback: no per-call records — keep the old five-field block.
    render_usage_block(
        flow.state.session_token_usage, title=t("cli.run.session_usage_title")
    )


def _finalize_sync_source_issue(
    project_root: Path,
    flow: Any,
    is_worktree_mode: bool,
    resolved: bool,
) -> None:
    """Best-effort finalize the source issue of a synchronous from-issue flow.

    Called from ``run_flow``'s terminal branches. Transitions the issue named
    by ``flow.source_issue_id`` to RESOLVED (``resolved=True``, flow COMPLETED)
    or back to OPEN (``resolved=False``, flow FAILED), but ONLY when it is
    currently IN_PROGRESS — so a re-terminated resume or a manually-touched
    issue is never clobbered.

    Worktree flows are skipped: their resolve is owned by the trailing
    ``luo merge`` (only a successful merge-back should resolve), so this only
    handles the non-worktree case. Any failure is swallowed — issue
    bookkeeping must never change the flow's exit code.

    The worktree-skip guard consults the *persisted* ``flow.is_worktree_mode``
    (restored from engine.json) in addition to the caller's ``is_worktree_mode``
    argument. On the daemon/``--resume`` path ``_resume_worktree_run`` re-enters
    ``run_flow`` with a flow that was serialized as worktree-mode, so keying off
    the persisted flag guarantees a resumed worktree flow can never reach the
    synchronous finalize (which would mutate the worktree checkout's own issue
    store) regardless of what the caller passed.
    """
    source_issue_id = getattr(flow, "source_issue_id", None)
    if (
        not source_issue_id
        or is_worktree_mode
        or getattr(flow, "is_worktree_mode", False)
    ):
        return
    try:
        from ..engine.issue_manager import IssueManager, IssueStatus

        issue_mgr = IssueManager(project_root)
        issue = issue_mgr.load(source_issue_id)
        # Only the run that actually holds the issue in-progress finalizes it;
        # a pause (persisted status PAUSED, never reaches these branches) thus
        # cannot resolve, and neither can a second terminal pass.
        if issue is None or issue.status != IssueStatus.IN_PROGRESS:
            return
        target = IssueStatus.RESOLVED if resolved else IssueStatus.OPEN
        issue_mgr.update_status(source_issue_id, target)
    except Exception:
        # Best-effort only — never let issue bookkeeping affect the return code.
        pass


def _collect_from_issue_flow_ids(
    project_root: Path, worktree: bool, source_issue_id: Optional[str]
) -> Set[str]:
    """Collect the ``flow_id`` of every persisted engine.json carrying ``source_issue_id``.

    This exists to tell a *stale prior run's* leftover state apart from state
    the *current* dispatch actually persisted. The main-repo
    ``tianluo/state/engine.json`` is a single reused slot: after a ``--from-issue A``
    run completes it still carries A's ``source_issue_id`` until the NEXT run's
    first ``save_flow`` overwrites it. Keying "does a persisted flow own this
    issue's finalize?" on ``source_issue_id`` alone would therefore mistake that
    stale slot for the current dispatch's state — so a flow is identified by its
    (unique) ``flow_id`` and the caller diffs a before/after snapshot: only a
    ``flow_id`` present AFTER the dispatch but not BEFORE was written by it.

    An engine.json missing a ``flow_id`` is keyed by its file path so an
    unchanged stale file compares equal across the snapshot (never counted as
    "new"). Corrupt/unreadable engine.json files are treated as absent.
    """
    if not source_issue_id:
        return set()
    sid = str(source_issue_id)
    ids: Set[str] = set()

    def _collect(engine_file: Path) -> None:
        try:
            with open(engine_file) as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError, OSError):
            return
        if not (
            isinstance(data, dict)
            and str(data.get("source_issue_id") or "") == sid
        ):
            return
        fid = data.get("flow_id")
        ids.add(str(fid) if fid else f"__nofid__:{engine_file}")

    if worktree:
        # A --worktree flow persists its state in its own worktree engine.json
        # (live under tianluo/worktrees/*, or copied into .archive/ once merged/GC'd).
        # A failure before fork_worktree never creates any of these.
        worktrees_dir = runtime_dir(project_root) / "worktrees"
        if not worktrees_dir.is_dir():
            return ids
        for ef in dual_runtime_glob(worktrees_dir, "*/", "state/engine.json"):
            _collect(ef)
        for ef in dual_runtime_glob(worktrees_dir / ".archive", "*/", "state/engine.json"):
            _collect(ef)
        return ids

    _collect(runtime_dir(project_root) / "state" / "engine.json")
    return ids


def snapshot_from_issue_flow_ids(
    project_root: Path, worktree: bool, source_issue_id: Optional[str]
) -> Set[str]:
    """Snapshot (BEFORE dispatch) the flow_ids already carrying ``source_issue_id``.

    The ``--from-issue`` wrapper takes this snapshot before dispatching so that
    :func:`from_issue_flow_state_exists` can later report only flows *this*
    dispatch persisted, ignoring a prior run's leftover single-slot engine.json
    (see :func:`_collect_from_issue_flow_ids` for why the id alone is unsafe).
    """
    return _collect_from_issue_flow_ids(project_root, worktree, source_issue_id)


def from_issue_flow_state_exists(
    project_root: Path,
    worktree: bool,
    source_issue_id: Optional[str],
    prior_flow_ids: Optional[Set[str]] = None,
) -> bool:
    """True when THIS dispatch persisted a flow carrying ``source_issue_id``.

    The ``--from-issue`` wrapper transitions the issue OPEN→IN_PROGRESS *before*
    dispatching the flow. Finalization then rides entirely on the flow reaching a
    persisted terminal state, so a dispatch that fails BEFORE any flow state is
    written — ``get_current_branch`` / ``fork_worktree`` raising in
    ``run_worktree_mode``, or a pre-flow ``ConfigError`` / flow-load failure in
    ``run_flow`` — would strand the issue IN_PROGRESS with no resume or merge
    path able to finalize it. This lets the wrapper tell that apart from a flow
    that genuinely reached a persisted state (paused / failed / completed): only
    the former self-recovers by reverting the issue to OPEN.

    A flow that merely paused, or a COMPLETED worktree flow whose merge failed
    (deliberately left IN_PROGRESS for a retry-merge), owns its own finalize and
    MUST NOT be reverted — both persist an engine.json carrying the
    ``source_issue_id``, so this returns ``True`` for them and the wrapper leaves
    them alone.

    ``prior_flow_ids`` is the caller's pre-dispatch snapshot (see
    :func:`snapshot_from_issue_flow_ids`). A persisted flow counts as "owned by
    this dispatch" only when its ``flow_id`` is absent from that snapshot — this
    is what stops a *stale* prior run's leftover main-repo engine.json (single
    reused slot, still carrying the same ``source_issue_id`` from an earlier
    completed run of the same issue) from being mistaken for the current
    dispatch's state and wrongly suppressing the revert.
    """
    if not source_issue_id:
        return False
    current = _collect_from_issue_flow_ids(project_root, worktree, source_issue_id)
    return bool(current - set(prior_flow_ids or ()))


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
    still lands the worktree under ``tianluo/worktrees/<branch-safe-name>/`` via
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
    engine_file = runtime_dir(worktree_path) / "state" / "engine.json"
    try:
        with open(engine_file) as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError, OSError):
        return None
    return data.get("status")


def _read_worktree_source_issue_id(worktree_path: Path) -> Optional[str]:
    """Read the persisted ``source_issue_id`` from a worktree's ``engine.json``.

    The source-issue finalize decision must NOT depend on the original wrapper
    process being alive (a paused run may be resumed by a fresh
    ``luo run --resume`` process); the only cross-process-stable signal is the
    id persisted in the worktree's own flow state. Returns ``None`` when the
    file is missing/unreadable or the flow carried no source issue.
    """
    engine_file = runtime_dir(worktree_path) / "state" / "engine.json"
    try:
        with open(engine_file) as f:
            data = json.load(f)
    except (json.JSONDecodeError, IOError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    source_issue_id = data.get("source_issue_id")
    return str(source_issue_id) if source_issue_id else None


def _finalize_worktree_source_issue(
    main_root: Path,
    worktree_path: Path,
    status: Optional[str],
    merge_rc: Optional[int],
    worktree_branch: Optional[str] = None,
    target_branch: Optional[str] = None,
) -> None:
    """Best-effort finalize the source issue of a ``--worktree`` from-issue run.

    Decision is driven by the flow's *persisted terminal status* (read from the
    worktree engine.json), never the wrapper's exit code — a json-mode pause
    also returns 0, so the exit code cannot distinguish pause from completion.

    - ``status == FAILED`` → move the issue back to OPEN (mirrors sync failure)
      UNLESS the isolation branch already landed on master. With the merge split
      into ``merge_integrate`` + ``version_reconcile``, a flow can merge the
      branch (integrate succeeds, work is on master) and then FAIL in the cheap
      ``version_reconcile`` step. Reopening the issue then would spawn duplicate
      work for code that already landed; the version miscompute is a merge-side
      retry (``luo merge`` / resume re-runs reconcile), not unmerged work. So
      when the branch is an ancestor of master, leave the issue IN_PROGRESS.
    - ``status == COMPLETED`` → the resolve is owned by ``run_merge`` (it only
      resolves on a successful merge-back, ``merge_rc == 0``); when the merge
      failed (``merge_rc != 0``) the issue is deliberately left IN_PROGRESS so
      the retry-merge path (``luo merge <branch>``) can resolve it later. Either
      way there is nothing to do here for COMPLETED.

    Only an issue that is currently IN_PROGRESS is touched, so a pause (which
    never reaches these terminal branches) and a second terminal pass cannot
    clobber a manually-changed issue. All failures are swallowed — issue
    bookkeeping must never alter the worktree run's exit code.
    """
    if status != FlowStatus.FAILED.value:
        return
    # A FAILED flow whose branch already landed on master is not unmerged work —
    # the failure is downstream of merge_integrate (a version_reconcile miscompute
    # that a merge-side retry fixes). Reopening would duplicate landed code.
    if worktree_branch and _worktree_branch_landed(
        main_root, worktree_branch, target_branch
    ):
        return
    source_issue_id = _read_worktree_source_issue_id(worktree_path)
    if not source_issue_id:
        return
    try:
        from ..engine.issue_manager import IssueManager, IssueStatus

        issue_mgr = IssueManager(main_root)
        issue = issue_mgr.load(source_issue_id)
        if issue is None or issue.status != IssueStatus.IN_PROGRESS:
            return
        issue_mgr.update_status(source_issue_id, IssueStatus.OPEN)
    except Exception:
        # Best-effort only — never let issue bookkeeping affect the return code.
        pass


def _worktree_branch_landed(
    project_root: Path,
    worktree_branch: str,
    target_branch: Optional[str],
) -> bool:
    """True iff *worktree_branch* has landed on *target_branch* (is its ancestor).

    Equivalent to ``git merge-base --is-ancestor worktree_branch target_branch``.
    Fail-closed: any git error (missing ref, indeterminate ancestry) returns
    False, so an unverifiable state is treated as "not merged" rather than
    reported as a successful merge. Refs are shared across all linked worktrees,
    so this resolves identically whether *project_root* is the main checkout or a
    worktree. Falls back to HEAD when no target branch was recorded.
    """
    from ..engine.worktree import _run_git

    target = target_branch or "HEAD"
    try:
        result = _run_git(
            project_root,
            "merge-base",
            "--is-ancestor",
            worktree_branch,
            target,
            check=False,
            timeout=15,
        )
    except Exception:  # noqa: BLE001 - any git failure => "not landed" (fail closed)
        logger.debug(
            "ancestry check failed for %s -> %s", worktree_branch, target,
            exc_info=True,
        )
        return False
    return result.returncode == 0


def _finalize_worktree_cleanup(
    project_root: Path,
    worktree_branch: str,
    worktree_original_branch: Optional[str],
    worktree_path: Path,
) -> int:
    """Clean up after a worktree flow that merged itself back in-flow.

    Successor to the retired ``_finalize_worktree_merge`` trailing-merge path.
    The merge is no longer a wrapper concern: a worktree flow's own step
    sequence now ends with ``merge_integrate`` + ``version_reconcile`` (executed
    in the main checkout under the merge lock), so by the time the flow reaches
    COMPLETED the branch has *already* landed on master and the final version has
    been reconciled. "Flow completed" therefore means "actually merged".

    All that remains is housekeeping — archive the worktree + delete the
    (now-ancestor) isolation branch + promote the terminal flow state — plus
    resolving the source issue of a ``--from-issue`` run. Both are best-effort:
    the merge already succeeded, so a cleanup hiccup must never be reported as a
    merge failure (the standalone worktree GC reclaims anything left behind).
    Returns 0.
    """
    from ..engine.merge.cleanup import CleanupManager
    from .merge.merge_lock import MergeLock
    from .merge_cmd import (
        _backfill_resolved_source_issues,
        _map_branches_to_source_issues,
    )

    target = worktree_original_branch or t("cli.run.merge.unknown_target")

    # Guard: "COMPLETED" only means "landed" because a worktree flow's step
    # sequence now ends with merge_integrate + version_reconcile. But a flow
    # PERSISTED BEFORE those steps existed carries a selected_steps without them,
    # and resume never re-derives the sequence — so such a flow can reach
    # COMPLETED having never merged. Verify the branch actually became an
    # ancestor of the target before resolving the source issue / reporting a
    # merge; otherwise we would silently close a --from-issue source issue and
    # print "Merged" for work still stranded in the worktree.
    if not _worktree_branch_landed(
        project_root, worktree_branch, worktree_original_branch
    ):
        display_error(
            t(
                "cli.run.merge.not_landed",
                branch=worktree_branch,
                target=target,
                worktree_path=worktree_path,
            )
        )
        return 1

    get_console().print(
        Rule(t("cli.run.merge.header", target=target), style="cyan")
    )

    # Capture branch→issue BEFORE cleanup archives/removes the worktree (whose
    # engine.json holds source_issue_id).
    branch_issue_map = _map_branches_to_source_issues(project_root, [worktree_branch])

    # Housekeeping mutates the main checkout (worktree remove + branch delete +
    # state promotion), so it runs under the same main-worktree mutex the merge
    # steps used — serialised against concurrent runs/merges.
    lock = MergeLock(_resolve_main_lock_root(project_root), blocking=True)
    lock.acquire(blocking=True)
    try:
        # Final runtime sync BEFORE cleanup archives/removes the worktree.
        # merge_integrate's integrate() synced the worktree's runtime state as it
        # stood mid-step — version_reconcile had not yet run and the confirm-gate
        # records were not yet written. Those later records (version_reconcile
        # history, confirm answers, merge_integrate's own completion) live only in
        # the worktree, so without a second sync here the main-checkout history
        # would stop at the mid-flight merge_integrate anchor and lose the tail of
        # the flow once the worktree is gone. Best-effort: a sync hiccup must never
        # turn a successful merge into a cleanup failure.
        try:
            from ..engine.merge.runtime_sync import sync_branch_runtime

            sync_branch_runtime(project_root, worktree_branch)
        except Exception:  # noqa: BLE001 - history sync is best-effort
            logger.debug(
                "final worktree runtime sync failed post in-flow merge",
                exc_info=True,
            )
        try:
            CleanupManager(project_root).delete_merged_branches([worktree_branch])
        except Exception:  # noqa: BLE001 - cleanup is best-effort; GC is the net
            logger.debug("worktree cleanup failed post in-flow merge", exc_info=True)
    finally:
        try:
            lock.release()
        except Exception:  # noqa: BLE001
            logger.debug("main lock release failed after worktree cleanup", exc_info=True)

    # The branch's commits are ancestors of master (merged in-flow), so its
    # source issue is resolved through the same idempotent choke point the CLI
    # merge uses.
    resolved = _backfill_resolved_source_issues(
        project_root, [worktree_branch], branch_issue_map
    )
    display_success(t("cli.run.merge.merged", branch=worktree_branch, target=target))
    for issue_id in resolved:
        get_console().print(t("cli.run.merge.resolved_issue", issue_id=issue_id))
    return 0


def run_worktree_mode(
    project_root: Path,
    task: str,
    task_type: str = "feature",
    change_name: Optional[str] = None,
    prompt_history: Any = None,
    output_format: str = "cli",
    source_issue_id: Optional[str] = None,
    plan_decomposition: Optional[str] = None,
    plan_granularity: Optional[str] = None,
) -> int:
    """Run a flow in an isolated git worktree, then merge the result back.

    Thin orchestration wrapper for ``luo run --worktree``:

    1. Generate an isolation branch name and fork a worktree from the current
       branch (``tianluo/worktrees/<branch-safe-name>/``).
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
        display_error(t("cli.run.worktree.cannot_start", error=exc))
        return 1

    worktree_branch = _generate_worktree_branch_name(task)

    try:
        worktree_path = fork_worktree(project_root, original_branch, worktree_branch)
    except Exception as exc:  # noqa: BLE001 - surface any git failure cleanly
        display_error(t("cli.run.worktree.create_failed", error=exc))
        return 1

    # Topology changed (a worktree was added); drop any cached main-repo
    # resolution so later lookups reflect the new layout.
    clear_main_repo_root_cache()

    render_full(
        t(
            "cli.run.worktree.started",
            branch=worktree_branch,
            worktree_path=worktree_path,
            original=original_branch,
        ),
        title=t("cli.run.worktree.title"),
    )

    # Own the worktree's ``run.pid`` marker for the ENTIRE worktree-run lifecycle
    # — the isolated flow body AND the trailing merge/cleanup phase below — so
    # ``luo end-session`` can reliably terminate this still-live wrapper process
    # at any point. ``run_flow`` is told NOT to manage the marker
    # (``manage_pidfile=False``); if it cleared the marker in its own ``finally``
    # the still-running merge would be undiscoverable (the wrapper keeps
    # ``cwd==main_root`` and the main ``engine.json`` lacks the worktree flow_id),
    # letting end-session archive/delete the worktree mid-merge. The marker lives
    # in the worktree's own state dir (where end-session looks first for a
    # worktree session). The finally clears it on every exit; a successful merge
    # may have already deleted the worktree (and the marker with it), in which
    # case the clear is a harmless no-op.
    wt_persistence = PersistenceManager(worktree_path)
    if _refuse_on_held_run_marker(
        _acquire_run_pidfile(wt_persistence), wt_persistence.state_dir
    ):
        return 1
    try:
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
            manage_pidfile=False,
            plan_decomposition=plan_decomposition,
            plan_granularity=plan_granularity,
        )

        if exit_code != 0:
            # Failed / interrupted: preserve the worktree + branch for --resume
            # and do NOT merge. Mirrors a synchronous run that left state behind.
            # Finalize the source issue off the PERSISTED status (only a genuine
            # FAILED moves it back to open; an interrupted/paused run stays
            # in-progress) — never off exit_code, which cannot tell them apart.
            _finalize_worktree_source_issue(
                project_root,
                worktree_path,
                _worktree_flow_status(worktree_path),
                merge_rc=None,
                worktree_branch=worktree_branch,
                target_branch=original_branch,
            )
            render_full(
                t(
                    "cli.run.worktree.did_not_complete",
                    exit_code=exit_code,
                    worktree_path=worktree_path,
                    branch=worktree_branch,
                ),
                title=t("cli.run.worktree.paused_title"),
            )
            return exit_code

        # A 0 exit code is ambiguous in --output-format json: a flow that PAUSED
        # for non-interactive input (e.g. a daemon-spawned --worktree --discover
        # run awaiting a web answer) also returns 0. Only a genuinely COMPLETED
        # flow may trigger the trailing merge — merging a paused flow would delete
        # its branch and archive its worktree (engine.json + call files),
        # irrecoverably losing the run the web operator is about to answer.
        status = _worktree_flow_status(worktree_path)
        if status != FlowStatus.COMPLETED.value:
            render_full(
                t(
                    "cli.run.worktree.paused_no_merge",
                    status=t_status(status or "unknown"),
                    worktree_path=worktree_path,
                    branch=worktree_branch,
                ),
                title=t("cli.run.worktree.paused_title"),
            )
            return exit_code

        # Success means the flow already merged itself back in-flow (its final
        # steps, merge_integrate + version_reconcile, run in the main checkout
        # under the merge lock). All that is left is housekeeping + resolving the
        # source issue of a --from-issue run.
        return _finalize_worktree_cleanup(
            project_root, worktree_branch, original_branch, worktree_path
        )
    finally:
        _clear_run_pidfile(wt_persistence)


def find_resumable_worktree_runs(project_root: Path) -> List[Dict[str, Any]]:
    """Discover resumable ``--worktree`` runs under ``tianluo/worktrees/``.

    Each isolated ``--worktree`` run persists its flow state in its own
    ``tianluo/worktrees/<name>/tianluo/state/engine.json``. This scans those files and
    returns one entry per non-COMPLETED worktree flow so the resume picker can
    surface them alongside the main-repo flow. A successfully-merged run has had
    its worktree archived/removed by ``--delete-merged``, so only failed or
    interrupted runs remain to be found here.

    Returns a list of dicts shaped like :func:`find_existing_flows` entries plus
    ``worktree_path`` / ``worktree_branch`` / ``worktree_original_branch`` so the
    resume dispatcher can re-run the flow inside the worktree and merge it back.
    """
    runs: List[Dict[str, Any]] = []
    worktrees_dir = runtime_dir(project_root) / "worktrees"
    if not worktrees_dir.is_dir():
        return runs

    terminal_statuses = {FlowStatus.COMPLETED.value}
    for engine_file in sorted(dual_runtime_glob(worktrees_dir, "*/", "state/engine.json")):
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
                "description": data.get("task_description") or t("cli.common.no_description"),
                "current_step": state_data.get("current_step_id"),
                "file": str(engine_file),
                "is_worktree_run": True,
                "worktree_path": worktree_path,
                "worktree_branch": data.get("worktree_branch"),
                "worktree_original_branch": data.get("worktree_original_branch"),
            }
        )
    return runs


def find_worktree_source_issue_by_branch(
    project_root: Path, branch: str
) -> Optional[str]:
    """Return the ``source_issue_id`` of the worktree flow on ``branch``.

    Scans both live worktree state
    (``tianluo/worktrees/*/tianluo/state/engine.json``) and archived worktree state
    (``tianluo/worktrees/.archive/*/tianluo/state/engine.json``) for an
    ``is_worktree_mode`` flow whose ``worktree_branch`` matches ``branch`` and
    returns its ``source_issue_id``.

    Unlike :func:`find_resumable_worktree_runs`, COMPLETED flows are NOT
    excluded — they are in fact the *only* flows this returns: by the time
    ``luo merge`` resolves a source issue the flow must (by construction) have
    already reached COMPLETED, and a leftover branch whose first merge failed
    may only be re-merged long after its worktree was GC'd into ``.archive`` —
    both cases still map back to the source issue. A non-COMPLETED flow (e.g.
    a paused ``--from-issue`` run whose partial-work branch an operator merges
    by hand) is deliberately *not* mapped: resolving its source issue would
    mark unfinished work done. So the status gate is a correctness guard, not
    an optimisation.

    Returns ``None`` when no matching COMPLETED flow is found or the branch
    carried no source issue. Corrupt / unreadable engine.json files are skipped
    rather than raised, so a single bad file never blocks the merge's backfill.
    """
    if not branch:
        return None
    worktrees_dir = runtime_dir(project_root) / "worktrees"
    if not worktrees_dir.is_dir():
        return None

    # Live worktrees first (authoritative for a not-yet-archived run), then the
    # archive: ``--delete-merged`` / worktree GC copies the whole worktree —
    # including its engine.json — under ``.archive/`` before removing the live
    # directory, so a retry-merge of a GC'd branch still finds its source issue.
    engine_files: List[Path] = []
    engine_files.extend(sorted(dual_runtime_glob(worktrees_dir, "*/", "state/engine.json")))
    engine_files.extend(
        sorted(dual_runtime_glob(worktrees_dir / ".archive", "*/", "state/engine.json"))
    )
    for engine_file in engine_files:
        try:
            with open(engine_file) as f:
                data = json.load(f)
        except (json.JSONDecodeError, IOError, OSError):
            continue
        if not isinstance(data, dict):
            continue
        if not data.get("is_worktree_mode"):
            continue
        if data.get("worktree_branch") != branch:
            continue
        # Only a COMPLETED flow's source issue may be resolved by a merge:
        # merging a paused/partial branch by hand must not mark unfinished
        # work done. The finalizing merge and any retry both run over a flow
        # that has already reached COMPLETED, so this gate never blocks a
        # legitimate backfill.
        if data.get("status", "unknown") != FlowStatus.COMPLETED.value:
            continue
        source_issue_id = data.get("source_issue_id")
        if source_issue_id:
            return str(source_issue_id)
    return None


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

    The daemon resumes a ``--worktree`` run by relaunching ``luo run --resume``
    with its ``cwd`` set to the worktree directory itself — that is where the
    run's ``engine.json`` / history live, where its WebUI call-responses are
    written, and what the daemon's resume validation reads. In that case the
    flow is not discoverable via :func:`find_resumable_worktree_runs` (which
    scans ``<main_repo>/tianluo/worktrees/``, one level up), so this reads the
    worktree's own ``engine.json`` and recognises it as a resumable worktree
    run. Returns ``None`` when ``project_root`` is not an ``is_worktree_mode``
    flow, the flow id does not match, or the flow is already COMPLETED.
    """
    engine_file = runtime_dir(project_root) / "state" / "engine.json"
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
        "description": data.get("task_description") or t("cli.common.no_description"),
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
        display_error(t("cli.run.worktree.path_gone", worktree_path=worktree_path))
        return 1

    # Own the worktree's ``run.pid`` marker for the whole resume lifecycle (flow
    # body + trailing merge), mirroring ``run_worktree_mode`` — ``run_flow`` is
    # told not to manage it so the marker survives into the post-COMPLETED merge,
    # keeping the still-live process discoverable by ``luo end-session``. See the
    # ``run_flow`` ``manage_pidfile`` docstring.
    wt_persistence = PersistenceManager(worktree_path)
    if _refuse_on_held_run_marker(
        _acquire_run_pidfile(wt_persistence, run["id"]), wt_persistence.state_dir
    ):
        return 1
    try:
        exit_code = run_flow(
            project_root=worktree_path,
            flow_id=run["id"],
            # This is a worktree flow: keep the flag truthful so run_flow's
            # terminal finalize is correctly recognized as worktree-mode (its
            # resolve is owned by the trailing run_merge, not the sync finalize).
            is_worktree_mode=True,
            prompt_history=prompt_history,
            output_format=output_format,
            acquire_main_lock=False,
            manage_pidfile=False,
        )

        if exit_code != 0:
            # Same persisted-status finalize as the first-run path: a resume that
            # ends FAILED moves the source issue back to open; an interrupted /
            # re-paused resume (also exit!=0) leaves it in-progress. Decoupled
            # from the original wrapper — this fresh --resume process owns the
            # finalize now.
            _finalize_worktree_source_issue(
                project_root,
                worktree_path,
                _worktree_flow_status(worktree_path),
                merge_rc=None,
                worktree_branch=worktree_branch,
                target_branch=worktree_original_branch,
            )
            render_full(
                t(
                    "cli.run.worktree.did_not_complete_resume",
                    exit_code=exit_code,
                    worktree_path=worktree_path,
                ),
                title=t("cli.run.worktree.paused_title"),
            )
            return exit_code

        # A 0 exit is ambiguous in json mode: a flow that PAUSED again for further
        # non-interactive input also returns 0. Only merge a genuinely COMPLETED
        # flow — merging a still-paused flow would archive its worktree and delete
        # its branch, losing the run mid-resume.
        status = _worktree_flow_status(worktree_path)
        if status != FlowStatus.COMPLETED.value:
            render_full(
                t(
                    "cli.run.worktree.paused_again",
                    status=t_status(status or "unknown"),
                    worktree_path=worktree_path,
                ),
                title=t("cli.run.worktree.paused_title"),
            )
            return exit_code

        if not worktree_branch:
            display_error(t("cli.run.worktree.no_branch"))
            return 1

        # In-flow merge already landed the branch on master (see
        # run_worktree_mode); only housekeeping + source-issue resolve remain.
        return _finalize_worktree_cleanup(
            project_root, worktree_branch, worktree_original_branch, worktree_path
        )
    finally:
        _clear_run_pidfile(wt_persistence)


def _read_engine_status(project_root: Path, flow_id: str) -> Optional[str]:
    """Return the upper-cased status of *flow_id* from the active engine.json.

    Used by the resume cross-machine preflight to skip the guard for a COMPLETED
    flow (a foreign ``run.pid`` marker left by a since-finished run must not
    block; COMPLETED is unresumable anyway). Reads only the size-guarded header
    so a tens-of-MB legacy engine.json is not fully parsed. Returns ``None`` when
    the active engine.json does not describe *flow_id*.
    """
    from ..daemon.disk_json_cache import read_engine_header

    engine_json = runtime_dir(project_root) / "state" / "engine.json"
    data = read_engine_header(engine_json, active=True)
    if data is None or str(data.get("flow_id") or "") != flow_id:
        return None
    return str(data.get("status") or "").upper()


def _cross_machine_resume_block(project_root: Path, flow_id: str) -> Optional[str]:
    """Return the message refusing a cross-machine resume, or ``None`` to allow it.

    A non-``None`` result is the rendered, user-facing refusal naming the
    machine whose live run holds the ``run.pid`` marker; the caller displays it
    and exits non-zero. The guard is skipped when the flow is COMPLETED
    (unresumable, and its foreign marker is at most stale) and for local /
    legacy (unstamped) markers, which
    :func:`~tianluo.core.run_pidfile.foreign_run_holder` already treats as local
    so pre-upgrade markers keep the same-machine path.

    INVARIANT: ownership is judged from exactly ONE state dir — the one the
    target flow actually writes. A ``--worktree`` flow never stamps the main
    root's marker (``run_worktree_mode`` / ``_resume_worktree_run`` write
    ``run.pid`` into the *worktree's* own state dir and pass
    ``manage_pidfile=False`` down to ``run_flow``), so for such a flow only
    ``<worktree>/tianluo/state/run.pid`` can hold it. Consulting the main root
    as well would wedge it behind an unrelated main-root run on another machine:
    a worktree flow body shares no state file with the main root, and the two
    running concurrently is the architecture, not a conflict. Conversely, a main
    root flow is judged solely by the main root's marker.

    WHY two wordings: the main root's marker is scoped to a state dir, not to a
    flow, so it may belong to a live run of a DIFFERENT flow there. That still
    refuses (a second main-root engine would race the root's single-slot
    engine.json), but only a marker actually attributable to *flow_id* may say
    "this flow is running on X" and point at ``luo end-session`` there —
    otherwise the operator would be told to end an unrelated live session.
    """
    from ..core.run_pidfile import foreign_run_holder

    # ``flow_scoped``: a flow's own isolation worktree can host no other flow,
    # so an unstamped marker there is still attributable to it.
    root, flow_scoped = project_root, False
    worktree_run = _find_worktree_run_by_flow_id(project_root, flow_id)
    if worktree_run is not None:
        worktree_path = worktree_run.get("worktree_path")
        if worktree_path:
            root, flow_scoped = Path(worktree_path), True
    elif _self_worktree_run(project_root, flow_id) is not None:
        # The daemon resumes a worktree run with cwd set to the worktree itself;
        # that state dir is then the flow's own, so its marker is attributable.
        flow_scoped = True

    # A finished run's leftover marker must not block (COMPLETED is unresumable
    # anyway). Judged against the authoritative root's own engine.json.
    if _read_engine_status(root, flow_id) == "COMPLETED":
        return None
    holder = foreign_run_holder(runtime_dir(root) / "state")
    if holder is None:
        return None
    if holder.owns_flow(flow_id, flow_scoped=flow_scoped):
        return t("cli.run.resume.held_by_machine", machine=holder.machine_id)
    return t(
        "cli.run.resume.root_busy_machine",
        machine=holder.machine_id,
        holder_flow=holder.flow_id or t("cli.run.resume.unknown_flow"),
        flow_id=flow_id,
    )


def resume_run(
    project_root: Path,
    flow_id: str,
    prompt_history: Any = None,
    output_format: str = "cli",
) -> int:
    """Dispatch a resume by flow id to the right path (worktree vs. main).

    If ``flow_id`` names a resumable ``--worktree`` run (discovered under
    ``tianluo/worktrees/``), it is resumed inside its worktree and merged back on
    success. When ``project_root`` *is itself* such a worktree — the shape the
    daemon uses when it relaunches ``luo run --resume`` with ``cwd`` set to the
    worktree directory — the same lock-free body + merge-back path is taken,
    with the merge driven from the resolved main repo. Otherwise the main-repo
    flow is resumed in place (a synchronous run that acquires the main-worktree
    mutex for its whole duration).
    """
    # Cross-machine single-writer preflight — mirror of the daemon's
    # ``request_resume`` guard for the direct ``luo run --resume`` path: if the
    # target flow's ``run.pid`` marker is held by a live run on another machine,
    # refuse rather than spawn a second engine that would race the shared
    # engine.json. The local process table can never observe the remote process,
    # so the on-disk machine-stamped marker is the only cross-host signal.
    blocked = _cross_machine_resume_block(project_root, flow_id)
    if blocked is not None:
        display_error(blocked)
        return 1

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
