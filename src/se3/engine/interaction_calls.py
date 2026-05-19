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
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

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
