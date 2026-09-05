"""Unified interaction-call file channel (``tianluo/calls/``).

Every point in a running flow that needs a human in the loop — a pending MCP
call, a queued mid-flow interjection, one round of the interjection dialog that
interjection opens, a retry/failure decision, a CLI subprocess confirmation
prompt, or a non-interactive discovery confirmation gate — is represented by
the *same* artifact: a JSON file under
``<project_root>/tianluo/calls/``. The file carries a ``kind`` field
(one of the ``CALL_KIND_*`` constants defined in :mod:`tianluo.daemon.protocol`)
plus display metadata (``prompt``, ``context``, ``options``) so the daemon
aggregator and the web console can render and route the interaction without
guessing at free text.

This module is the single producer/consumer helper for those files:

* :func:`write_call` writes a call file of any kind.
* :func:`read_call` parses one back, defaulting legacy files without a
  ``kind`` field to :data:`~tianluo.daemon.protocol.CALL_KIND_CALL`.
* :func:`read_response` / :func:`write_response` handle the sibling answer
  file (``<stem>.response`` or ``<stem>.response.json``).
* :func:`write_interjection_request` is the daemon-side producer for a
  mid-flow interjection; :func:`drain_interjection_requests` is the
  ``luo run`` consumer. It is drained wherever the interjection can be acted
  on at once — mid-call (interrupting the running LLM to open the dialog) or
  at a pause point (opening the dialog there, or becoming the paused
  DISCOVERY conversation's next reply). There is no step-boundary drain.
* :func:`write_retry_decision_call` writes the no-TTY failure-decision call.
* :func:`write_dialog_call` / :func:`read_dialog_response` carry one round of
  the mid-flow interjection dialog: the transcript so far out, and either the
  operator's next message or their confirmed (possibly edited) decision back.

The format is deliberately backward compatible: a call file produced by an
older SE3 (no ``kind`` / ``prompt`` / ``context`` keys) still parses cleanly
and is classified as a plain ``call``.
"""

from __future__ import annotations
from tianluo.runtime_paths import runtime_dir

import hashlib
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

from ..daemon.protocol import (
    CALL_KIND_CALL,
    CALL_KIND_CLI_CONFIRM,
    CALL_KIND_CONFIRM,
    CALL_KIND_DIALOG,
    CALL_KIND_DISCOVERY_CONFIRM,
    CALL_KIND_INTERJECTION,
    CALL_KIND_RETRY_DECISION,
    CALL_KINDS,
)

__all__ = [
    "CALL_KIND_CALL",
    "CALL_KIND_CLI_CONFIRM",
    "CALL_KIND_CONFIRM",
    "CALL_KIND_DIALOG",
    "CALL_KIND_DISCOVERY_CONFIRM",
    "CALL_KIND_INTERJECTION",
    "CALL_KIND_RETRY_DECISION",
    "CALL_KINDS",
    "active_flow_id",
    "bind_active_flow",
    "call_flow_id",
    "calls_dir_for",
    "classify_kind",
    "find_call_file",
    "flow_id_for_call",
    "write_call",
    "read_call",
    "read_response",
    "write_response",
    "response_path",
    "write_interjection_request",
    "drain_interjection_requests",
    "has_pending_interjections",
    "retry_decision_call_path",
    "write_retry_decision_call",
    "write_dialog_call",
    "read_dialog_response",
    "dialog_decision_revision",
    "dialog_response_binding",
    "write_interaction_call",
    "read_interaction_response",
    "make_cli_confirm_handler",
]


def calls_dir_for(project_root: Any) -> Path:
    """Return the ``tianluo/calls/`` directory for *project_root*."""
    return runtime_dir(Path(project_root)) / "calls"


def classify_kind(data: Optional[Dict[str, Any]]) -> str:
    """Resolve the interaction kind of a (possibly legacy) parsed call dict.

    A call file written before the ``kind`` field existed — or one whose
    ``kind`` is unrecognised — is classified as :data:`CALL_KIND_CALL`, so old
    artifacts keep working without migration.
    """
    if not isinstance(data, dict):
        return CALL_KIND_CALL
    kind = data.get("kind")
    return kind if kind in CALL_KINDS else CALL_KIND_CALL


def write_call(
    calls_dir: Any,
    *,
    kind: str,
    prompt: str,
    context: Optional[Dict[str, Any]] = None,
    options: Optional[List[Any]] = None,
    call_id: Optional[str] = None,
    **extra: Any,
) -> Path:
    """Write a call file of *kind* and return its path.

    The file is written atomically (temp + rename). *prompt* is the
    human-facing question, *context* the structured metadata (flow / step
    ids, error text, …) and *options* the discrete choices, if any.
    """
    if kind not in CALL_KINDS:
        raise ValueError(f"unknown call kind: {kind!r}")
    directory = Path(calls_dir)
    directory.mkdir(parents=True, exist_ok=True)
    if not call_id:
        call_id = f"{kind}_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
    payload: Dict[str, Any] = {
        "call_id": call_id,
        "kind": kind,
        "prompt": prompt,
        "context": dict(context) if context else {},
        "options": list(options) if options else [],
        "created_at": time.time(),
    }
    payload.update(extra)
    path = directory / f"{call_id}.json"
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    tmp.replace(path)
    return path


def read_call(call_path: Any) -> Optional[Dict[str, Any]]:
    """Parse a call file; return ``None`` on any read/parse error.

    The returned dict always has a ``kind`` key — defaulted via
    :func:`classify_kind` for legacy files that lack one.
    """
    try:
        data = json.loads(Path(call_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    data["kind"] = classify_kind(data)
    return data


def find_call_file(calls_dir: Any, call_id: Any) -> Optional[Path]:
    """Locate the call file carrying *call_id*, or ``None``.

    :func:`write_call` names the file after the id, so the direct hit answers
    almost every lookup for free. The directory scan exists because the name is
    not part of the contract: the merge orchestrator picks its own filenames
    (``merge_<branch>_<ts>.json``) while still recording a ``call_id`` inside,
    and an answer arriving from the web console knows only the id.
    """
    wanted = str(call_id or "").strip()
    if not wanted:
        return None
    directory = Path(calls_dir)
    direct = directory / f"{wanted}.json"
    if direct.is_file():
        return direct
    if not directory.is_dir():
        return None
    for path in sorted(directory.glob("*.json")):
        if path.name.endswith(".response.json"):
            continue
        data = read_call(path)
        if isinstance(data, dict) and str(data.get("call_id") or "") == wanted:
            return path
    return None


def flow_id_for_call(project_root: Any, call_id: Any) -> str:
    """The flow a pending call belongs to, or ``""`` when it is unaddressed.

    WHY the daemon needs this: a project root is a single ``engine.json`` slot
    that successive flows occupy in turn, so answering a call cannot mean
    "wake whoever holds the slot" — that runs an unrelated flow and strands the
    one that asked the question. ``""`` (no call file, or a legacy producer that
    recorded no flow) deliberately degrades to the slot occupant, preserving
    pre-addressing behaviour.
    """
    path = find_call_file(calls_dir_for(project_root), call_id)
    if path is None:
        return ""
    data = read_call(path)
    return call_flow_id(data) if isinstance(data, dict) else ""


def response_path(call_path: Any) -> Path:
    """Return the canonical sibling ``.response`` path for *call_path*."""
    call_path = Path(call_path)
    return call_path.with_name(call_path.stem + ".response")


def read_response(call_path: Any) -> Optional[Dict[str, Any]]:
    """Return the parsed sibling answer file, or ``None`` if none exists.

    Both ``<stem>.response`` (written by this module) and
    ``<stem>.response.json`` (written by the daemon client) are recognised so
    a response re-enters the flow regardless of which side answered.
    """
    call_path = Path(call_path)
    for suffix in (".response", ".response.json"):
        candidate = call_path.with_name(call_path.stem + suffix)
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict):
            return data
        return {"response": data}
    return None


def write_response(call_path: Any, response: Any) -> Path:
    """Write a sibling ``.response`` answer file and return its path."""
    target = response_path(call_path)
    body = response if isinstance(response, dict) else {"response": response}
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(
        json.dumps(body, indent=2, ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    tmp.replace(target)
    return target


def write_interjection_request(
    calls_dir: Any,
    text: str,
    *,
    flow_id: str = "",
    call_id: Optional[str] = None,
) -> Path:
    """Daemon-side producer: queue a mid-flow interjection as a call file.

    Called when the daemon receives a :data:`~tianluo.daemon.protocol.MSG_INTERJECT_FLOW`
    instruction. The running ``luo run`` process consumes it via
    :func:`drain_interjection_requests` as soon as it can act on it: mid-call
    it interrupts the running LLM and opens the interjection dialog; at a
    pause point it opens that dialog there (or, in a paused DISCOVERY, becomes
    the conversation's next reply).
    """
    return write_call(
        calls_dir,
        kind=CALL_KIND_INTERJECTION,
        prompt=text,
        context={"flow_id": flow_id},
        call_id=call_id,
        text=text,
    )


# WHY a process-scoped binding rather than a threaded parameter: one ``luo
# run`` process serves exactly one flow, but the interjection channel is a
# per-project-root directory that successive flows occupy in turn. Every drain
# site inside the run (pause gates, discovery turns, the dialog's own follow-up
# drains) therefore has the same answer to "which flow am I?", and most of them
# sit in helpers that never receive the flow object. Binding it once, where the
# flow is loaded, keeps the ownership check on every path instead of only on
# the few that happen to have a flow in scope.
_active_flow_id = ""


def bind_active_flow(flow_id: Any) -> None:
    """Declare which flow this process's interjection drains belong to."""
    global _active_flow_id
    _active_flow_id = str(flow_id or "").strip()


def active_flow_id() -> str:
    """The flow bound by :func:`bind_active_flow` (``""`` when unbound)."""
    return _active_flow_id


def call_flow_id(data: Dict[str, Any]) -> str:
    """The flow a call file is addressed to, or ``""`` when unaddressed.

    Producers record it under ``context.flow_id``; very old files (and any
    hand-written one) may carry it top-level instead, or not at all.
    """
    context = data.get("context") if isinstance(data.get("context"), dict) else {}
    return str(context.get("flow_id") or data.get("flow_id") or "").strip()


def _addresses_flow(data: Dict[str, Any], flow_id: Optional[str]) -> bool:
    """Whether an addressed call file belongs to *flow_id*.

    INVARIANT: an interjection is consumed only by the flow it was sent to.
    A project root is a single slot that successive flows occupy in turn, so a
    call queued for a paused flow A is still sitting there when a later flow B
    runs in the same root; without this check B would drain A's message, open
    the dialog against the wrong work, and leave A paused forever.

    Both sides degrade to a wildcard: a call file with no ``flow_id`` (legacy
    producer, or an operator-dropped file) is deliverable to whoever asks, and
    a caller that does not know its own flow id yet asks for everything
    (``flow_id=None`` defers to the process binding, ``""`` opts out entirely).
    """
    target = (_active_flow_id if flow_id is None else str(flow_id)).strip()
    if not target:
        return True
    own = call_flow_id(data)
    return not own or own == target


def has_pending_interjections(
    project_root: Any, flow_id: Optional[str] = None
) -> bool:
    """Whether an unanswered interjection call file is waiting, WITHOUT consuming it.

    WHY a peek exists next to the drain: the resume path has to know that this
    process was woken by an interjection *before* it decides how to re-arm the
    step, but the message itself belongs to whoever opens the dialog. Draining
    it here would consume the operator's first sentence into a code path that
    has nowhere to put it.

    *flow_id* scopes the peek to interjections addressed to that flow; see
    :func:`_addresses_flow`.
    """
    directory = calls_dir_for(project_root)
    if not directory.is_dir():
        return False
    for path in sorted(directory.glob("*.json")):
        if path.name.endswith(".response.json"):
            continue
        data = read_call(path)
        if data is None or classify_kind(data) != CALL_KIND_INTERJECTION:
            continue
        if not _addresses_flow(data, flow_id):
            continue
        if read_response(path) is not None:
            continue
        if str(data.get("text") or data.get("prompt") or "").strip():
            return True
    return False


def drain_interjection_requests(
    project_root: Any, flow_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """``luo run`` consumer for queued interjections (any-time, not only at
    step boundaries).

    Scans ``tianluo/calls/`` for unanswered :data:`CALL_KIND_INTERJECTION` call
    files and returns one dict per drained item (oldest first) carrying
    ``text`` / ``call_id`` / ``step_id`` / ``step_type`` / ``created_at``.
    ``step_id`` / ``step_type`` come from the call file's ``context`` (or
    legacy top-level fields) when the producer populated them; otherwise
    they degrade to empty strings. ``created_at`` is the original call-file
    timestamp so downstream history writers can preserve the chronological
    order in which the user interjected.

    Each consumed call file is sealed using a *write-response-then-delete*
    protocol: a sibling ``.response`` carrying
    ``{consumed: True, served_at: <ts>, served_by: 'run_loop'}`` is written
    first so the daemon aggregator's sibling-response judgement immediately
    classifies it as consumed during the brief window both files coexist;
    then the original ``.json`` call file is unlinked so a subsequent
    enumeration cannot find it again. The protocol is idempotent — once
    unlinked, a re-drain is a no-op; if a previous drain crashed between
    the write and the unlink, the next drain finishes the unlink half and
    skips. Empty-text requests follow the same path with a
    ``skipped: 'empty'`` marker.

    *flow_id* scopes the drain to interjections addressed to that flow: a call
    belonging to a different flow is left untouched (not read, not sealed) so
    its own flow can still consume it. See :func:`_addresses_flow`.
    """
    directory = calls_dir_for(project_root)
    if not directory.is_dir():
        return []
    drained: List[Dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        if path.name.endswith(".response.json"):
            continue
        data = read_call(path)
        if data is None or classify_kind(data) != CALL_KIND_INTERJECTION:
            continue
        if not _addresses_flow(data, flow_id):
            continue
        if read_response(path) is not None:
            # Sibling response already present (a prior drain crashed mid-
            # protocol, or the daemon answered out-of-band): finish the
            # unlink half so the aggregator no longer enumerates the call.
            try:
                Path(path).unlink()
            except OSError:
                pass
            continue
        text = str(data.get("text") or data.get("prompt") or "").strip()
        context = data.get("context") if isinstance(data.get("context"), dict) else {}
        if not text:
            write_response(
                path,
                {
                    "consumed": True,
                    "skipped": "empty",
                    "served_at": time.time(),
                    "served_by": "run_loop",
                },
            )
            try:
                Path(path).unlink()
            except OSError:
                pass
            continue
        step_id = context.get("step_id") or data.get("step_id") or ""
        step_type = context.get("step_type") or data.get("step_type") or ""
        drained.append(
            {
                "text": text,
                "call_id": data.get("call_id") or path.stem,
                "step_id": str(step_id) if step_id else "",
                "step_type": str(step_type) if step_type else "",
                "created_at": data.get("created_at"),
            }
        )
        # Write the sibling .response BEFORE unlinking the call file so the
        # aggregator's sibling-response check classifies it as consumed during
        # the brief window both files coexist; then unlink the call file so a
        # second drain pass cannot re-enumerate it.
        write_response(
            path,
            {
                "consumed": True,
                "served_at": time.time(),
                "served_by": "run_loop",
            },
        )
        try:
            Path(path).unlink()
        except OSError:
            pass
    # WHY the explicit sort: filename order is only *approximately* chronological
    # — the call_id embeds a millisecond stamp followed by random hex, so two
    # interjections pushed inside the same millisecond order arbitrarily. The
    # consumer feeds these to an LLM as consecutive user messages, where a
    # swapped pair changes what was said, so the promised "oldest first" has to
    # come from the recorded timestamp rather than from the name.
    drained.sort(key=lambda item: (item.get("created_at") or 0))
    return drained


def retry_decision_call_path(project_root: Any, step_id: str) -> Path:
    """Return where *step_id*'s retry_decision call file lives.

    The ``call_id`` is derived from the step id, so the path is knowable
    without writing (or having written) the call. That is what lets a resume
    *peek* at how the gate was answered before deciding what the answer
    entitles it to do, instead of having to re-raise the gate to find out.
    """
    return calls_dir_for(project_root) / f"retry_decision_{step_id}.json"


def write_retry_decision_call(
    project_root: Any,
    *,
    flow_id: str,
    step_id: str,
    step_type: str,
    error: str,
    retry_count: int = 0,
    options: Optional[List[str]] = None,
) -> Path:
    """Write a :data:`CALL_KIND_RETRY_DECISION` call file for a FAILED step.

    Used on the no-TTY failure path of ``luo run``: with no interactive
    terminal to host the Retry/Skip/Abort prompt, the decision is externalised
    as a call file that the web console (or any responder) can answer. The
    ``call_id`` is derived from *step_id* so a resume reuses the same file
    rather than piling up duplicates.
    """
    return write_call(
        calls_dir_for(project_root),
        kind=CALL_KIND_RETRY_DECISION,
        call_id=f"retry_decision_{step_id}",
        prompt=f"Step '{step_type}' failed: {error}",
        context={
            "flow_id": flow_id,
            "step_id": step_id,
            "step_type": step_type,
            "retry_count": retry_count,
            "error": error,
        },
        options=list(options) if options else ["retry", "skip", "abort"],
    )


def dialog_decision_revision(decision: Optional[Dict[str, Any]]) -> str:
    """Stable content id of the decision published in one dialog round.

    WHY the rounds need an id at all: they all share one ``call_id`` (the
    conversation is one growing panel), so a bare confirmation — "apply what is
    shown", carrying no fields of its own — is otherwise indistinguishable from
    a confirmation of the round that replaced it. The id is derived from the
    decision's VALUES, so a round republished unchanged keeps its id and a
    field edited anywhere (terminal or console) produces a new one.

    ``""`` means "this round proposed nothing", which no client can confirm.
    """
    if not decision:
        return ""
    try:
        blob = json.dumps(decision, sort_keys=True, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        blob = repr(decision)
    return hashlib.sha1(blob.encode("utf-8", "replace")).hexdigest()[:12]


def dialog_response_binding(call_path: Any) -> Dict[str, Any]:
    """What a pending dialog reply can be bound to the published round WITH.

    Returns ``{"responded_at": <float|None>, "decision_revision": <str>}``.

    A client that echoes the round's ``decision_revision`` back binds its
    confirmation exactly; one that does not is bound by WHEN it answered —
    rounds published after that instant provably were not on its screen. Kept
    apart from :func:`read_dialog_response` (which consumes the reply and whose
    return shape is a stable contract) because this is transport metadata, not
    part of the operator's answer.

    INVARIANT: ``responded_at`` is the response FILE's mtime, never the
    ``responded_at`` its writer stamped into the payload. The instant it is
    ordered against is a call file's mtime, so both ends have to come off the
    same clock: a project directory can be shared between machines, and the
    daemon that lands the answer is then a different host whose clock is not
    synchronised with the flow's. A stamp from a lagging clock orders a fresh
    answer before every round it could possibly have seen, and the dialog can
    never be confirmed at all; one from a leading clock is no better founded.
    A client that wants an exact binding echoes ``decision_revision``, which is
    clock-free.
    """
    binding: Dict[str, Any] = {"responded_at": None, "decision_revision": ""}
    call_path = Path(call_path)
    for suffix in (".response", ".response.json"):
        candidate = call_path.with_name(call_path.stem + suffix)
        try:
            raw = candidate.read_text(encoding="utf-8")
            mtime: Optional[float] = candidate.stat().st_mtime
        except OSError:
            continue
        binding["responded_at"] = mtime
        try:
            data = json.loads(raw)
        except ValueError:
            return binding
        if isinstance(data, dict):
            revision = data.get("decision_revision")
            if not revision and isinstance(data.get("response"), dict):
                revision = data["response"].get("decision_revision")
            if isinstance(revision, str):
                binding["decision_revision"] = revision
        return binding
    return binding


def write_dialog_call(
    project_root: Any,
    *,
    flow_id: str,
    step_id: str,
    step_type: str,
    prompt: str,
    transcript: Optional[List[Dict[str, Any]]] = None,
    decision: Optional[Dict[str, Any]] = None,
    rewind_targets: Optional[List[Dict[str, Any]]] = None,
    same_session: bool = False,
    agent_name: str = "",
    subject_step_id: str = "",
    reset_preview: Optional[Dict[str, Any]] = None,
    group_work: Optional[List[Dict[str, Any]]] = None,
    apply_error: str = "",
) -> Path:
    """Write a :data:`CALL_KIND_DIALOG` call file for one interjection-dialog round.

    The ``call_id`` is derived from *step_id* so a resumed flow re-uses the same
    file for the whole conversation rather than piling up one call per turn —
    the web console then shows a single growing dialog, which is what a
    conversation is.

    Because the rounds SHARE that id, the context carries a
    ``prompt_revision``: a consumer that caches anything per ``call_id`` (the
    web console caches the untruncated prompt body, which it fetches on demand)
    has no other way to tell one round's prompt from the next, and would keep
    rendering an earlier round — hiding, above all, the apply-failure banner
    that explains why a confirmed decision did not execute.

    Two reply shapes are supported by :func:`read_dialog_response`: free text
    (the operator's next message) and ``{"decision": {...}}`` (they confirmed,
    possibly after editing a field).
    """
    options: List[Any] = []
    if decision:
        # Presented as a one-click confirm alongside the editable fields, the
        # same affordance pattern the discovery-confirm gate uses.
        options = [{"label": "confirm", "value": "confirm"}]
    return write_call(
        calls_dir_for(project_root),
        kind=CALL_KIND_DIALOG,
        call_id=f"dialog_{step_id}",
        prompt=prompt,
        context={
            "flow_id": flow_id,
            "step_id": step_id,
            "step_type": step_type,
            "awaiting": "decision" if decision else "message",
            "transcript": list(transcript or []),
            "decision": dict(decision) if decision else None,
            "rewind_targets": list(rewind_targets or []),
            "same_session": bool(same_session),
            "agent_name": agent_name,
            # The step whose SESSION the dialog is talking to. At a CONFIRM /
            # failure gate that is the producer under review, while ``step_id``
            # above stays the flow's current step so the daemon's stale-call
            # filter keeps the conversation visible.
            "subject_step_id": subject_step_id or step_id,
            # Populated only for a proposed ``restart`` + ``workspace: reset``:
            # what the reset would discard, so the web console can show it
            # before the operator confirms.
            "reset_preview": dict(reset_preview) if reset_preview else None,
            # Populated for ANY proposed ``restart`` that would delete DAG group
            # worktrees/branches — including ``workspace: keep``, whose "keep"
            # only ever applied to the main tree. That work is invisible to the
            # reset preview (it lives on leaf branches, not in ``baseline..HEAD``
            # or the main tree's status), so without this the operator confirms a
            # discard they were never shown.
            "group_work": list(group_work or []),
            # Why the previously confirmed decision did not take effect. Carried
            # in the context — not only prepended to the prompt — because the
            # prompt renders collapsed: an apply failure that is only in there
            # re-publishes a byte-identical panel and reads as "nothing
            # happened".
            "apply_error": apply_error or "",
            # Content-derived, so it changes exactly when the rendered body
            # does; see the docstring.
            "prompt_revision": hashlib.sha1(
                (prompt or "").encode("utf-8", "replace")
            ).hexdigest()[:12],
            # Identifies the DECISION this round put on the table (see
            # :func:`dialog_decision_revision`). A client that echoes it back
            # with a bare confirmation binds that confirmation to the round it
            # actually rendered, instead of to whatever the flow has published
            # by the time the answer is read.
            "decision_revision": dialog_decision_revision(decision),
        },
        options=options,
        step_id=step_id,
        flow_id=flow_id,
    )


def read_dialog_response(call_path: Any) -> Optional[Dict[str, Any]]:
    """Return an interjection-dialog reply as ``{"text": ...}`` / ``{"decision": ...}``.

    Returns ``None`` while no answer has landed. A bare string answer is the
    operator's next dialog message; a mapping carrying ``decision`` is their
    confirmation (with any field they edited). The literal ``"confirm"``
    produced by the one-click option is recognised as "accept the proposed
    decision unchanged" — and is reported as ``{"confirm": True, "text":
    "confirm"}`` so a caller with NO proposal on the table can fall back to
    treating the word as what it also plainly is: the operator's next message.
    Only the caller knows whether a proposal exists, so the ambiguity is
    resolved there, never here.

    A ``preview_request`` alongside the decision means "adopt these fields and
    show me what they would do" — NOT "execute them". The web console sends it
    when the operator has edited a proposal into a workspace-discarding one, so
    the reset preview is taken and rendered for the decision actually about to
    run rather than for the one the agent originally proposed.
    """
    return _interpret_dialog_reply(read_response(call_path))


#: Fields that make a mapping a DECISION rather than transport metadata. A
#: mapping carrying none of them decides nothing, so reading it as one would
#: normalise its missing ``action`` into a ``continue`` the operator never
#: asked for.
_DIALOG_DECISION_FIELDS = frozenset(
    {"action", "instruction", "revised_description", "restart_step_id", "workspace"}
)


def _interpret_dialog_reply(
    data: Any, preview_only: bool = False, depth: int = 0
) -> Optional[Dict[str, Any]]:
    """Resolve one dialog answer, unwrapping the envelopes it can arrive in.

    WHY the unwrapping recurses instead of stopping one level down: the daemon
    re-wraps whatever a remote client sent into its own ``{"response": ...}``
    envelope, so a client's ``{"response": "confirm", "decision_revision": ...}``
    lands as a mapping nested inside a mapping. Treating that inner envelope as
    a decision — it has no decision field, only the round id it is bound by —
    executed a defaulted ``continue`` in place of the ``restart`` / ``exit`` the
    operator was confirming.
    """
    if isinstance(data, str):
        if data.strip().lower() == "confirm":
            return {"confirm": True, "text": data}
        return {"text": data}
    # The envelopes are shallow by construction; the bound only stops a
    # pathological (or hand-written) payload from recursing without end.
    if not isinstance(data, dict) or depth > 3:
        return None
    preview_only = preview_only or bool(
        data.get("preview_request") or data.get("preview")
    )
    if isinstance(data.get("decision"), dict):
        return _dialog_decision_reply(data["decision"], preview_only)
    for key in ("response", "answer", "text", "feedback"):
        if key not in data:
            continue
        nested = _interpret_dialog_reply(data[key], preview_only, depth + 1)
        if nested is not None:
            return nested
    if any(field in data for field in _DIALOG_DECISION_FIELDS):
        return _dialog_decision_reply(data, preview_only)
    if data.get("confirm") or data.get("decision_revision"):
        # Fieldless by design: a bare confirmation of the round it names. The
        # round id is binding metadata (:func:`dialog_response_binding` reads
        # it), never a decision field.
        return {"confirm": True, "text": "confirm"}
    return None


def _dialog_decision_reply(
    decision: Dict[str, Any], preview_only: bool
) -> Dict[str, Any]:
    reply: Dict[str, Any] = {"decision": decision}
    if preview_only:
        reply["preview"] = True
    return reply


# ---------------------------------------------------------------------------
# Backward-compatible convenience wrappers
#
# Older call sites (the CLI-confirmation handler in ``run.py`` and its test
# suite) address the call queue by *project root* and expect the bare answer
# value rather than the structured response envelope. These thin shims keep
# that surface working on top of the canonical project-root-agnostic API.
# ---------------------------------------------------------------------------


def write_interaction_call(
    project_root: Any,
    kind: str,
    prompt: str,
    *,
    options: Optional[List[Any]] = None,
    context: Optional[Dict[str, Any]] = None,
    **extra: Any,
) -> Path:
    """Write an interaction call file under ``<project_root>/tianluo/calls/``.

    A project-root-addressed convenience wrapper around :func:`write_call`;
    any extra keyword arguments (e.g. ``flow_id`` / ``step_id``) are folded
    into the call-file payload.
    """
    return write_call(
        calls_dir_for(project_root),
        kind=kind,
        prompt=prompt,
        options=options,
        context=context,
        **extra,
    )


def read_interaction_response(call_path: Any) -> Any:
    """Return the bare answer value for *call_path*, or ``None``.

    Reads either a ``<stem>.response.json`` envelope or a plain
    ``<stem>.response`` sibling. A ``{"response": ...}`` envelope is unwrapped
    to its inner value; a plain non-JSON ``.response`` file yields its stripped
    text. Returns ``None`` when no response file exists.
    """
    call_path = Path(call_path)
    envelope = call_path.with_name(call_path.stem + ".response.json")
    try:
        data = json.loads(envelope.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    else:
        if isinstance(data, dict) and "response" in data:
            return data["response"]
        return data

    plain = call_path.with_name(call_path.stem + ".response")
    try:
        text = plain.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        # The file exists but is empty — most likely the writer truncated via
        # ``open('w')`` and has not yet written its payload. Treat as
        # "no response yet" so the poller retries instead of returning the
        # empty string as a valid answer.
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return text
    if isinstance(parsed, dict) and "response" in parsed:
        return parsed["response"]
    return text


def make_cli_confirm_handler(
    project_root: Any,
    flow_id: Optional[str] = None,
    step_id: Optional[str] = None,
    poll_interval: float = 0.5,
) -> Callable[[str, List[str], Callable[[], bool]], Optional[str]]:
    """Build an ``on_confirm`` callback for ``ClaudeCodeRunner.run_with_monitor``.

    When the agent runner detects a CLI-subprocess confirmation prompt it
    invokes the returned callback with ``(prompt_text, options, is_alive)``.
    The callback writes a ``cli_confirm`` interaction call file (so the daemon
    aggregator surfaces it and the web console can answer it), then polls for
    the sibling ``.response`` file and returns its answer string for the
    runner to write back to the subprocess stdin.

    It returns ``None`` — a no-op for the runner — when the subprocess exits
    before any response arrives, so a child that finishes early never hangs
    the flow waiting on an answer that will not come.
    """

    def _on_confirm(
        prompt: str,
        options: List[str],
        is_alive: Callable[[], bool],
    ) -> Optional[str]:
        # ``flow_id`` / ``step_id`` MUST live inside ``context`` so the daemon
        # aggregator's per-flow filter (which inspects ``call.context.flow_id``)
        # can scope a ``cli_confirm`` call to the flow that produced it. Folding
        # them in as top-level extras would leave the call unattributed and
        # surface it in every concurrent flow's reply chip-bar.
        confirm_context: Dict[str, Any] = {"awaiting": "cli_confirm"}
        if flow_id:
            confirm_context["flow_id"] = flow_id
        if step_id:
            confirm_context["step_id"] = step_id
        call_file = write_interaction_call(
            project_root,
            kind=CALL_KIND_CLI_CONFIRM,
            prompt=prompt,
            options=options,
            context=confirm_context,
        )
        logger.info(
            "CLI confirmation prompt captured; wrote call file %s", call_file
        )
        # INVARIANT: this wait polls the stop signal as well as the child.
        # It is the one place the runner's monitor loop hands control to a
        # blocking callback, so a stop published while the child sits at a
        # confirmation prompt would otherwise be observed by nobody — the
        # graceful-stop protocol would never start and Ctrl-C would do
        # nothing at all until a web answer happened to arrive.
        from ..stop_signal import get_stop_signal

        stop = get_stop_signal()
        stopped = False
        while is_alive():
            response = read_interaction_response(call_file)
            if response is not None:
                return response
            if stop.is_set():
                stopped = True
                break
            time.sleep(poll_interval)
        # The subprocess exited (or a stop was requested) before a response
        # arrived — make one last check in case the answer landed during the
        # final poll window, then give up so the runner does not block.
        response = read_interaction_response(call_file)
        if response is not None:
            return response
        # No answer ever arrived. Mark the orphaned call file consumed
        # (mirroring drain_interjection_requests' empty handling) so the
        # aggregator stops enumerating it as a pending interaction — the
        # environment that asked for it is gone or is being wound down.
        stopped = stopped or stop.is_set()
        if read_response(call_file) is None:
            write_response(
                call_file,
                {
                    "consumed": True,
                    "skipped": "stop_requested" if stopped else "subprocess_exited",
                },
            )
            logger.info(
                "CLI confirmation left unanswered (%s); marked orphaned call "
                "file %s consumed",
                "stop requested" if stopped else "subprocess exited",
                call_file,
            )
        return None

    return _on_confirm
