"""Machine-aware ``tianluo/state/run.pid`` record codec, shared by CLI + daemon.

``luo run`` records its own pid into ``run.pid`` for the lifetime of a flow so
``luo end-session`` and the resume double-spawn guards can locate the live
process. On a shared filesystem that marker is visible to *other* machines,
whose process tables can never observe the recording process — so the record
now also stamps :func:`~tianluo.core.machine_id.stable_machine_id`, and every
consumer routes the holder machine through
:func:`~tianluo.core.machine_id.is_local_machine` before trusting the local
process table.

Format is a three-line record — ``pid``, machine id, flow id — written via
tmp+rename so a concurrent reader always sees a complete prior or complete new
record. Older records decode by truncation: a two-line record yields
``flow_id is None`` and a legacy single-line (bare pid) record additionally
yields ``machine_id is None``, which :func:`is_local_machine` treats as local so
pre-upgrade markers keep their same-machine behaviour.

WHY the flow id is recorded: the marker is scoped to a *state dir*, not to a
flow, so a project root's marker may name a live run of a DIFFERENT flow than
the one a resume was requested for. Without the flow id the refusal could only
guess, and would tell the operator to end a session that is doing unrelated
work — see :meth:`RunHolder.owns_flow`.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple, Optional, Tuple

from .machine_id import is_local_machine

RUN_PID_FILENAME = "run.pid"


class RunHolder(NamedTuple):
    """The owner recorded in a ``run.pid`` marker.

    ``machine_id`` / ``flow_id`` are ``None`` for records written before those
    lines existed (and ``flow_id`` is also ``None`` while a brand-new run has
    not yet minted its flow id).
    """

    pid: int
    machine_id: Optional[str]
    flow_id: Optional[str]

    def owns_flow(self, flow_id: str, *, flow_scoped: bool = False) -> bool:
        """Whether this marker can be attributed to *flow_id*.

        Pass ``flow_scoped=True`` for a state dir that can only ever host that
        one flow (a flow's isolation worktree): there an unstamped marker still
        belongs to it. In a shared project root an unstamped marker is
        ambiguous and must NOT be claimed as the flow's, or a refusal would
        assert that a flow is running when a different one holds the root.
        """
        if self.flow_id:
            return str(self.flow_id) == str(flow_id)
        return flow_scoped


def encode_run_pidfile(
    pid: int, machine_id: str, flow_id: Optional[str] = None
) -> str:
    """Encode a ``run.pid`` record (pid line, machine-id line, flow-id line).

    The flow-id line is omitted when unknown — a new run stamps the marker
    before the engine mints the flow id — which decodes back to
    ``flow_id=None`` exactly like a pre-upgrade two-line record.
    """
    if flow_id:
        return f"{pid}\n{machine_id}\n{flow_id}\n"
    return f"{pid}\n{machine_id}\n"


def read_run_holder(state_dir: Path) -> Optional[RunHolder]:
    """Return the :class:`RunHolder` recorded in ``<state_dir>/run.pid``.

    Returns ``None`` when the marker is absent / unreadable / malformed / holds
    a non-positive pid.
    """
    pid_file = Path(state_dir) / RUN_PID_FILENAME
    try:
        raw = pid_file.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    lines = raw.splitlines()
    if not lines:
        return None
    try:
        pid = int(lines[0].strip())
    except ValueError:
        return None
    if pid <= 0:
        return None

    def _line(index: int) -> Optional[str]:
        if len(lines) > index and lines[index].strip():
            return lines[index].strip()
        return None

    return RunHolder(pid=pid, machine_id=_line(1), flow_id=_line(2))


def read_run_pidfile(state_dir: Path) -> Tuple[Optional[int], Optional[str]]:
    """Return ``(pid, machine_id)`` from ``<state_dir>/run.pid``.

    ``machine_id`` is ``None`` for a legacy single-line (bare pid) record, so
    the caller can treat it as local via :func:`is_local_machine`. Returns
    ``(None, None)`` when there is no usable record. Kept as the narrow view for
    the call sites that only decide "is this pid mine to signal?".
    """
    holder = read_run_holder(state_dir)
    if holder is None:
        return (None, None)
    return (holder.pid, holder.machine_id)


def foreign_run_holder(state_dir: Path) -> Optional[RunHolder]:
    """Return the ``run.pid`` holder iff it is recorded on ANOTHER machine.

    Returns ``None`` when there is no live marker, it is unreadable, or it is
    owned by the local machine — including a legacy record with no machine id,
    which :func:`is_local_machine` treats as local. A non-``None`` result names
    the remote machine (and, when recorded, the flow it is running) and is the
    signal to refuse a second engine: the local process table can never confirm
    the remote process is dead, so refusing a double-writer is the safe default.
    """
    holder = read_run_holder(state_dir)
    if holder is None:
        return None
    if is_local_machine(holder.machine_id):
        return None
    return holder
