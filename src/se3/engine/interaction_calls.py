```python
"""Unified human-interaction call files for a running flow.

Every environment in which a running flow needs a human to step in — a
pending MCP call, a Ctrl-C interjection, a retry/skip/abort failure
decision, a CLI subprocess confirmation prompt — is collapsed onto a
single on-disk carrier: a ``kind``-tagged JSON file under ``se3/calls/``.
The daemon aggregator reads those files and enriches them into
``PendingCall`` entries; the web console renders each ``kind`` as a
distinct, default-expanded interaction item.

This module owns three primitives:

* :func:`write_interaction_call` — write a ``kind``-tagged call file.
* :func:`read_call_response` — read its sibling ``.response`` answer.
* :func:`drain_interjection_requests` — consume mid-run interjection
  request files and fold them into ``flow.state.context``.

Interjection *requests* travel the opposite direction (server → daemon →
run.py): the daemon drops a request file under ``se3/interjections/`` and
``run.py`` consumes it at a step boundary, folding the text into the same
``user_interjections`` list the Ctrl-C path uses.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime
from itertools import count
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Recognised interaction kinds. ``call`` is the pre-existing pending MCP
# call; the others are added by the unified-intake work.
KIND_CALL = "call"
KIND_INTERJECTION = "interjection"
KIND_RETRY_DECISION = "retry_decision"
KIND_CLI_CONFIRM = "cli_confirm"

# Process-local sequence counter for collision-free filenames.
_seq = count()


def write_interaction_call(
    project_root: Path,
    kind: str,
    prompt: str,
    context: Optional[Dict[str, Any]] = None,
    options: Optional[Any] = None,
) -> Path:
    """Write a ``kind``-tagged interaction call file to ``se3/calls/``.

    Args:
        project_root: Project root directory.
        kind: Interaction kind (``retry_decision``, ``cli_confirm``, …).
        prompt: Human-readable text describing what is being asked.
        context: Display metadata (error summary, step ids, …).
        options: The allowed responses (e.g. a list of ``{value, label}``).

    Returns:
        Path to the written call file. A sibling ``<stem>.response`` file
        written by a responder is later picked up by
        :func:`read_call_response`.
    """
    calls_dir = Path(project_root) / "se3" / "calls"
    calls_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
    call_id = f"{kind}_{timestamp}_{os.getpid()}_{next(_seq)}"
    call_file = calls_dir / f"{call_id}.json"

    payload: Dict[str, Any] = {
        "type": "interaction",
        "kind": kind,
        "call_id": call_id,
        "prompt": prompt,
        "context": context or {},
        "options": options if options is not None else [],
        "created_at": datetime.now().timestamp(),
    }

    # Atomic write so the aggregator never observes a partial file.
    fd, tmp_path = tempfile.mkstemp(dir=calls_dir, prefix=f".tmp_{call_id}_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, call_file)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    logger.info("Wrote %s interaction call file: %s", kind, call_file)
    return call_file


def read_call_response(call_file: Path) -> Optional[Dict[str, Any]]:
    """Return the structured response for ``call_file``, or ``None``.

    Looks for a sibling ``<stem>.response.json`` (daemon-written envelope)
    or ``<stem>.response`` answer file. A JSON object is returned as-is; a
    bare JSON scalar/list is wrapped under a ``response`` key. Returns
    ``None`` when no response file exists or it cannot be parsed.
    """
    call_file = Path(call_file)
    for sibling in (
        call_file.parent / f"{call_file.stem}.response.json",
        call_file.parent / f"{call_file.stem}.response",
    ):
        if not sibling.exists():
            continue
        try:
            data = json.loads(sibling.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Failed to parse interaction response file: %s", sibling)
            continue
        if isinstance(data, dict):
            return data
        return {"response": data}
    return None


def drain_interjection_requests(flow: Any, project_root: Path) -> int:
    """Consume pending interjection request files into the flow.

    Interjection requests are JSON files dropped under
    ``se3/interjections/`` (by the daemon, on a server ``MSG_INTERJECT_FLOW``
    downlink). Each request's ``text`` is appended to
    ``flow.state.context["user_interjections"]`` — the same list the Ctrl-C
    interjection path uses, so it folds into the effective task description
    via :func:`compose_task_description_with_interjections`.

    Consumed request files are deleted so the same interjection is never
    applied twice. Malformed files are also removed so they cannot wedge
    the queue.

    Returns:
        The number of interjections folded into the flow.
    """
    requests_dir = Path(project_root) / "se3" / "interjections"
    if not requests_dir.is_dir():
        return 0

    files = sorted(
        (
            p
            for p in requests_dir.iterdir()
            if p.is_file() and p.suffix == ".json" and not p.name.startswith(".")
        ),
        key=lambda p: (p.stat().st_mtime if p.exists() else 0.0, p.name),
    )
    if not files:
        return 0

    current_step = flow.state.get_current_step()
    step_id = current_step.step_id if current_step else None
    if current_step is not None:
        step_type = (
            current_step.step_type.value
            if hasattr(current_step.step_type, "value")
            else str(current_step.step_type)
        )
    else:
        step_type = None

    interjections: List[Dict[str, Any]] = flow.state.context.setdefault(
        "user_interjections", []
    )

    added = 0
    for req_file in files:
        try:
            data = json.loads(req_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("Discarding malformed interjection request: %s", req_file)
            _safe_unlink(req_file)
            continue

        if isinstance(data, str):
            text = data.strip()
            req_step_id, req_ts = step_id, None
        elif isinstance(data, dict):
            text = str(data.get("text") or "").strip()
            req_step_id = data.get("step_id") or step_id
            req_ts = data.get("timestamp")
        else:
            text, req_step_id, req_ts = "", step_id, None

        if text:
            interjections.append(
                {
                    "text": text,
                    "step_id": req_step_id,
                    "step_type": step_type,
                    "timestamp": req_ts or datetime.now().isoformat(),
                }
            )
            added += 1
        _safe_unlink(req_file)

    if added:
        logger.info("Drained %d interjection request(s) into flow", added)
    return added


def _safe_unlink(path: Path) -> None:
    """Best-effort delete; never raises."""
    try:
        path.unlink()
    except OSError:
        pass
```