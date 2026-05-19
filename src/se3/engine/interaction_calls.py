"""Unified interaction-call file writer.

A small, dependency-free helper that writes *interaction call* files into a
project's ``se3/calls/`` directory. An interaction call is the single carrier
for every human-in-the-loop moment a running flow can surface — a pending MCP
call, a mid-flow interjection request, a retry/failure decision, or a
CLI-subprocess confirmation prompt.

Each call file is JSON carrying a ``kind`` discriminator plus display
metadata (``prompt`` / ``context`` / ``options``) so downstream consumers —
the daemon aggregator and the web console — can render and route it without
guessing from free text. A reviewer answers a call by writing a sibling
``<stem>.response`` (plain) or ``<stem>.response.json`` (envelope) file.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

# Recognized interaction-call kinds. ``cli_confirm`` is produced by the engine
# when the agent runner captures a CLI-subprocess confirmation prompt; the
# other kinds are written elsewhere as the unified intervention channel grows.
VALID_KINDS = ("call", "interjection", "retry_decision", "cli_confirm")


def write_interaction_call(
    project_root: Path,
    kind: str,
    prompt: str,
    *,
    context: Optional[Dict[str, Any]] = None,
    options: Optional[List[str]] = None,
    flow_id: Optional[str] = None,
    step_id: Optional[str] = None,
    call_id: Optional[str] = None,
) -> Path:
    """Write a single interaction call file under ``se3/calls/``.

    Args:
        project_root: The SE3 project root; the file lands in
            ``<project_root>/se3/calls/``.
        kind: One of :data:`VALID_KINDS` — the intervention discriminator.
        prompt: The text shown to the user (for ``cli_confirm`` this is the
            verbatim confirmation prompt captured from the child process).
        context: Optional structured context (what is being responded to).
        options: Optional list of selectable option labels.
        flow_id / step_id: Optional flow/step identifiers for routing.
        call_id: Optional explicit call id; when omitted a unique
            ``<kind>_<timestamp>`` id is generated.

    Returns:
        The path to the written call file.
    """
    calls_dir = Path(project_root) / "se3" / "calls"
    calls_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    if call_id is None:
        call_id = f"{kind}_{timestamp}"
    call_file = calls_dir / f"{call_id}.json"

    payload: Dict[str, Any] = {
        "kind": kind,
        # ``type`` / ``call_type`` mirror ``kind`` for older consumers that
        # key on those field names.
        "type": kind,
        "call_type": kind,
        "prompt": prompt,
        "context": context or {},
        "options": list(options) if options else [],
        "flow_id": flow_id,
        "step_id": step_id,
        "created_at": datetime.now().timestamp(),
    }
    call_file.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return call_file


def read_interaction_response(call_file: Path) -> Optional[str]:
    """Return the answer text for an interaction call, or ``None`` if unanswered.

    Supports both the ``<stem>.response.json`` envelope (answer nested under a
    ``response`` / ``answer`` / ``feedback`` / ``text`` key) and a plain
    ``<stem>.response`` sibling. Malformed JSON in a ``.response`` file falls
    back to the raw stripped text. Returns ``None`` when neither sibling
    exists yet.
    """
    call_file = Path(call_file)
    for sibling in (
        call_file.parent / f"{call_file.stem}.response.json",
        call_file.parent / f"{call_file.stem}.response",
    ):
        if not sibling.exists():
            continue
        try:
            raw = sibling.read_text(encoding="utf-8")
        except OSError:
            continue
        raw_stripped = raw.strip()
        try:
            data = json.loads(raw_stripped)
        except ValueError:
            # A plain-text response file — return it verbatim.
            return raw_stripped
        if isinstance(data, str):
            return data
        if isinstance(data, dict):
            for key in ("response", "answer", "feedback", "text"):
                value = data.get(key)
                if isinstance(value, str):
                    return value
            return json.dumps(data, ensure_ascii=False)
        return str(data)
    return None
