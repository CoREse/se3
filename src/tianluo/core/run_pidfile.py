"""Machine-aware ``tianluo/state/run.pid`` record codec, shared by CLI + daemon.

``luo run`` records its own pid into ``run.pid`` for the lifetime of a flow so
``luo end-session`` and the resume double-spawn guards can locate the live
process. On a shared filesystem that marker is visible to *other* machines,
whose process tables can never observe the recording process — so the record
now also stamps :func:`~tianluo.core.machine_id.stable_machine_id`, and every
consumer routes the holder machine through
:func:`~tianluo.core.machine_id.is_local_machine` before trusting the local
process table.

Format is a two-line record — ``pid`` on line 1, machine id on line 2 — written
via tmp+rename so a concurrent reader always sees a complete prior or complete
new record. A legacy single-line (bare pid) record decodes to ``machine_id is
None``, which :func:`is_local_machine` treats as local so pre-upgrade markers
keep their same-machine behaviour.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

from .machine_id import is_local_machine

RUN_PID_FILENAME = "run.pid"


def encode_run_pidfile(pid: int, machine_id: str) -> str:
    """Encode a machine-aware ``run.pid`` record (pid line + machine-id line)."""
    return f"{pid}\n{machine_id}\n"


def read_run_pidfile(state_dir: Path) -> Tuple[Optional[int], Optional[str]]:
    """Return ``(pid, machine_id)`` from ``<state_dir>/run.pid``.

    ``machine_id`` is ``None`` for a legacy single-line (bare pid) record, so
    the caller can treat it as local via :func:`is_local_machine`. Returns
    ``(None, None)`` when the marker is absent / unreadable / malformed / holds
    a non-positive pid.
    """
    pid_file = Path(state_dir) / RUN_PID_FILENAME
    try:
        raw = pid_file.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return (None, None)
    lines = raw.splitlines()
    if not lines:
        return (None, None)
    try:
        pid = int(lines[0].strip())
    except ValueError:
        return (None, None)
    if pid <= 0:
        return (None, None)
    machine_id: Optional[str] = None
    if len(lines) > 1 and lines[1].strip():
        machine_id = lines[1].strip()
    return (pid, machine_id)


def foreign_run_holder(state_dir: Path) -> Optional[str]:
    """Return the machine id holding ``run.pid`` iff it is ANOTHER machine.

    Returns ``None`` when there is no live marker, it is unreadable, or it is
    owned by the local machine — including a legacy record with no machine id,
    which :func:`is_local_machine` treats as local. A non-``None`` result names
    the remote machine and is the signal to refuse a second engine (the
    local process table can never confirm the remote process is dead, so
    refusing a double-writer is the safe default).
    """
    pid, machine_id = read_run_pidfile(state_dir)
    if pid is None:
        return None
    if is_local_machine(machine_id):
        return None
    return machine_id
