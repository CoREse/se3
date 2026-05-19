"""Unified interaction-call file channel (``se3/calls/``).

Every point in a running flow that needs a human in the loop — a pending MCP
call, a Ctrl-C mid-flow interjection, a retry/failure decision, or a CLI
subprocess confirmation prompt — is represented by the *same* artifact: a JSON
file under ``<project_root>/se3/calls/``. The file carries a ``kind`` field
(one of the ``CALL_KIND_*`` constants defined in :mod:`se3.daemon.protocol`)
plus display metadata (``prompt``, ``context``, ``options``) so the daemon
aggregator and the web console can render and route the interaction without
guessing at free text.

This module is the single producer/consumer helper for those files:

* :func:`write_call` writes a call file of any kind.
* :func:`read_call` parses one back, defaulting legacy files without a
  ``kind`` field to :data:`~se3.daemon.protocol.CALL_KIND_CALL`.
* :func:`read_response` / :func:`write_response` handle the sibling answer
  file (``<stem>.response`` or ``<stem>.response.json``).
* :func:`write_interjection_request` is the daemon-side producer for a
  mid-flow interjection; :func:`drain_interjection_requests` is the
  ``se3 run`` step-boundary consumer.
* :func:`write_retry_decision_call` writes the no-TTY failure-decision call.

The format is deliberately backward compatible: a call file produced by an
older SE3 (no ``kind`` / ``prompt`` / ``context`` keys) still parses cleanly
and is classified as a plain ``call``.
"""

from __future__ import annotations

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
    CALL_KIND_INTERJECTION,
    CALL_KIND_RETRY_DECISION,
    CALL_KINDS,
)

__all__ = [
    "CALL_KIND_CALL",
    "CALL_KIND_CLI_CONFIRM",
    "CALL_KIND_INTERJECTION",
    "CALL_KIND_RETRY_DECISION",
    "CALL_KINDS",
    "calls_dir_for",
    "classify_kind",
    "write_call",
    "read_call",
    "read_response",
    "write_response",
    "response_path",
    "write_interjection_request",
    "drain_interjection_requests",
    "write_retry_decision_call",
    "write_interaction_call",
    "read_interaction_response",
    "make_cli_confirm_handler",
]


def calls_dir_for(project_root: Any) -> Path:
    """Return the ``se3/calls/`` directory for *project_root*."""
    return Path(project_root) / "se3" / "calls"


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

    Called when the daemon receives a :data:`~se3.daemon.protocol.MSG_INTERJECT_FLOW`
    instruction. The running ``se3 run`` process consumes it via
    :func:`drain_interjection_requests` at the next step boundary.
    """
    return write_call(
        calls_dir,
        kind=CALL_KIND_INTERJECTION,
        prompt=text,
        context={"flow_id": flow_id},
        call_id=call_id,
        text=text,
    )


def drain_interjection_requests(project_root: Any) -> List[Dict[str, Any]]:
    """``se3 run`` step-boundary consumer for queued interjections.

    Scans ``se3/calls/`` for unanswered :data:`CALL_KIND_INTERJECTION` call
    files, returns their entries (oldest first, each a dict with ``text`` and
    ``call_id``), and marks every consumed file by writing a sibling
    ``.response`` so it is never drained twice. Empty-text requests are
    consumed and skipped rather than returned.
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
        if read_response(path) is not None:
            continue  # already consumed
        text = str(data.get("text") or data.get("prompt") or "").strip()
        if not text:
            write_response(path, {"consumed": True, "skipped": "empty"})
            continue
        drained.append(
            {"text": text, "call_id": data.get("call_id") or path.stem}
        )
        write_response(path, {"consumed": True, "consumed_at": time.time()})
    return drained


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

    Used on the no-TTY failure path of ``se3 run``: with no interactive
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
    """Write an interaction call file under ``<project_root>/se3/calls/``.

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
        call_file = write_interaction_call(
            project_root,
            kind=CALL_KIND_CLI_CONFIRM,
            prompt=prompt,
            options=options,
            context={"awaiting": "cli_confirm"},
            flow_id=flow_id,
            step_id=step_id,
        )
        logger.info(
            "CLI confirmation prompt captured; wrote call file %s", call_file
        )
        while is_alive():
            response = read_interaction_response(call_file)
            if response is not None:
                return response
            time.sleep(poll_interval)
        # The subprocess exited before a response arrived — make one last
        # check in case the answer landed during the final poll window,
        # then give up so the runner does not block on a dead child.
        response = read_interaction_response(call_file)
        if response is not None:
            return response
        # No answer ever arrived and the child is gone. Mark the orphaned
        # call file consumed (mirroring drain_interjection_requests' empty
        # handling) so the aggregator stops enumerating it as a pending
        # interaction — the environment that asked for it no longer exists.
        if read_response(call_file) is None:
            write_response(
                call_file,
                {"consumed": True, "skipped": "subprocess_exited"},
            )
            logger.info(
                "CLI confirmation subprocess exited unanswered; "
                "marked orphaned call file %s consumed",
                call_file,
            )
        return None

    return _on_confirm
