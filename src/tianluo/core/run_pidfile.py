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

INVARIANT: the marker is also the projects' single *ownership token*, and both
sides that can write it — a starting/resuming ``luo run`` and the destructive
``luo end-session`` — publish it exclusively through :func:`acquire_run_marker`.
Reading ownership and then writing unconditionally would make the token
advisory only: whichever side wrote last would win, so an end-session that had
just verified nothing owns the flow could still be overtaken by a resume and go
on to archive/delete a live flow's worktree and review baselines.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Callable, NamedTuple, Optional, Tuple

from .machine_id import is_local_machine, stable_machine_id

logger = logging.getLogger(__name__)

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


class MarkerProbe(NamedTuple):
    """What ``<state_dir>/run.pid`` looks like on disk right now.

    ``present`` says a marker file is there (or that its absence could not be
    established, because the read failed with something other than "no such
    file"); ``holder`` is its decoded owner. ``present and holder is None`` is
    the *undecidable* case — a marker exists but says nothing this host can
    act on.
    """

    present: bool
    holder: Optional["RunHolder"]

    @property
    def undecidable(self) -> bool:
        """Whether a marker exists whose owner could not be decoded.

        WHY a destructive caller must branch on this rather than on
        ``holder is None``: an unreadable marker (permission error, I/O error,
        truncated or garbage record) is evidence that *some* run claimed this
        state dir, and on a shared filesystem that run may be alive on another
        host. Collapsing it into "no marker" is what lets a cleanup path
        conclude "nothing is running" and delete a live flow's worktree.
        """
        return self.present and self.holder is None


def _decode_run_holder(raw: str) -> Optional[RunHolder]:
    """Decode a ``run.pid`` record body, or ``None`` when it is malformed."""
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


def probe_run_marker(state_dir: Path) -> MarkerProbe:
    """Report presence AND decoded owner of ``<state_dir>/run.pid`` separately.

    Only "no such file" (and a path whose parent is not a directory) counts as
    absent: a permission error, an I/O error or a stale NFS handle leaves the
    marker's existence unknown, and the safe reading of unknown is "present,
    owner undecidable" — see :attr:`MarkerProbe.undecidable`.
    """
    pid_file = Path(state_dir) / RUN_PID_FILENAME
    try:
        raw = pid_file.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return MarkerProbe(present=False, holder=None)
    except (OSError, ValueError):
        return MarkerProbe(present=True, holder=None)
    return MarkerProbe(present=True, holder=_decode_run_holder(raw))


def read_run_holder(state_dir: Path) -> Optional[RunHolder]:
    """Return the :class:`RunHolder` recorded in ``<state_dir>/run.pid``.

    Returns ``None`` when the marker is absent / unreadable / malformed / holds
    a non-positive pid. Callers whose next move is destructive must instead use
    :func:`probe_run_marker`, which keeps "absent" and "unreadable" apart.
    """
    return probe_run_marker(state_dir).holder


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

    An unreadable marker answers ``None`` here because this view can only speak
    about *identified* remote holders; a caller that is about to delete state
    must additionally refuse on :attr:`MarkerProbe.undecidable`.
    """
    holder = read_run_holder(state_dir)
    if holder is None:
        return None
    if is_local_machine(holder.machine_id):
        return None
    return holder


class MarkerClaim(NamedTuple):
    """Outcome of an attempt to take ``run.pid`` for the current process.

    ``acquired`` says the marker now names us. When it does not, ``blocked``
    separates the two cases that matter to a caller: ``True`` means exclusive
    ownership could not be established and the caller must stand down —
    either a *competing owner* holds the marker (``holder`` names it, or is
    ``None`` when the record could not be decoded, see
    :attr:`MarkerProbe.undecidable`), or the claim itself failed with an I/O
    error, which cannot prove the absence of one. ``blocked`` is ``False``
    only in the benign case where the marker ALREADY names this process and
    just could not be refreshed, so the caller keeps the ownership it holds.
    """

    acquired: bool
    holder: Optional["RunHolder"]
    blocked: bool


def _rewrite_owned_marker(marker: Path, flow_id: Optional[str]) -> bool:
    """Rewrite a marker THIS process already owns, via tmp+rename.

    Only ever called once ownership has been established, so the unconditional
    replace cannot clobber a competitor — it is the same record with a fuller
    flow id. Returns whether the rewrite landed.
    """
    tmp = marker.with_suffix(".pid.tmp")
    try:
        tmp.write_text(
            encode_run_pidfile(os.getpid(), stable_machine_id(), flow_id),
            encoding="utf-8",
        )
        tmp.replace(marker)
        return True
    except OSError:
        logger.debug("Rewriting owned run.pid at %s failed", marker, exc_info=True)
        try:
            tmp.unlink()
        except OSError:  # pragma: no cover - defensive
            pass
        return False


def _unestablished_claim(state_dir: Path) -> MarkerClaim:
    """The fail-closed answer for a claim that ended in an I/O failure.

    WHY blocked rather than a bare failure: an EACCES/EIO/ENOSPC from the
    exclusive create (or from the record write that follows it) says nothing
    about who owns the state dir — on a shared filesystem the very reason the
    write failed may sit next to a live remote run's marker or an
    ``luo end-session`` claim. Reporting "not blocked" made callers proceed
    into the flow WITHOUT owning the token, writing state concurrently with a
    destructive cleanup that still believes it owns the flow. Ownership was
    not established, so the only safe reading is "held".

    The probe is best-effort colour for the refusal message: it names the
    holder when one can still be decoded, and stays ``None`` when it cannot.
    """
    return MarkerClaim(False, probe_run_marker(state_dir).holder, True)


def acquire_run_marker(
    state_dir: Path,
    flow_id: Optional[str] = None,
    *,
    is_stale: Optional[Callable[["RunHolder"], bool]] = None,
) -> MarkerClaim:
    """Atomically take ``<state_dir>/run.pid`` for the CURRENT process.

    INVARIANT: this is the ONE way ``run.pid`` may be published. Every writer —
    a starting/resuming ``luo run`` and the ``luo end-session`` claim that
    guards its destructive window — goes through it, and the marker is created
    with ``O_CREAT | O_EXCL``, so two writers can never both succeed. An
    unconditional write from either side would reduce the other's ownership
    check to a read of a value the counterparty may overwrite a moment later:
    that is exactly how a resume on machine A lands *after* machine B claimed
    the flow, leaving B to archive and delete a running flow's worktree and
    review baselines.

    A marker this process already owns is re-taken idempotently (and rewritten,
    so a run can fill in its flow id once the engine mints one).

    *is_stale* is consulted ONLY for a marker owned by the LOCAL machine: it
    decides whether a record left behind by a run that died without its
    ``finally`` (SIGKILL, OOM, reboot) may be reclaimed. Liveness of a recorded
    pid is only decidable on the host that wrote it, so a foreign marker is
    never reclaimed here — that stays the operator's explicit act on the owning
    machine. Passing ``None`` disables reclamation entirely.

    Never raises for a RUNTIME failure: an unwritable/absent state dir, EACCES,
    EIO, ENOSPC and every other environmental error is folded into a fail-closed
    :class:`MarkerClaim` rather than an exception, because those callers must
    keep their "blocked" semantics. That contract does NOT cover a PROGRAMMING
    error in the argument itself: *state_dir* is validated first and a
    non-absolute path raises :class:`ValueError` before anything touches the
    filesystem.

    WHY the absolute-path guard: the marker's whole job is to name ONE state dir
    that every writer agrees on, so a path interpreted against the caller's
    current working directory can never be the right one. In practice the way a
    relative path arrives here is a test that mocked the persistence layer away
    — ``os.fspath`` on a bare ``MagicMock`` yields ``MagicMock/<name>/<id>``,
    which used to be really ``mkdir``-ed under the repo root, silently, once per
    test. Refusing loudly turns that leak into an immediate failure instead of
    disk litter, and no legitimate caller is affected: production state dirs are
    derived from a cwd- or git-resolved project root, and the one entry point
    that takes a root from the operator (``luo end-session -p``) absolutizes it
    before the claim — see :func:`~tianluo.commands.end_session_cmd._resolve_main_root`
    and :class:`~tianluo.engine.persistence.PersistenceManager`.

    Raises:
        ValueError: if *state_dir* does not resolve to an absolute path.
    """
    resolved = Path(os.fspath(state_dir))
    if not resolved.is_absolute():
        raise ValueError(
            "acquire_run_marker requires an absolute state_dir; got "
            f"{resolved!r} from a {type(state_dir).__name__}"
        )
    state_dir = resolved
    marker = state_dir / RUN_PID_FILENAME
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.debug("Cannot create state dir %s for run.pid", state_dir, exc_info=True)
        return _unestablished_claim(state_dir)

    # Two attempts at most: the second exists so a reclaimed stale marker (or
    # one that vanished under us) can be re-created exclusively. A second
    # FileExistsError means a competitor won that re-creation, which is a real
    # owner and must block.
    for attempt in (0, 1):
        try:
            fd = os.open(str(marker), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            probe = probe_run_marker(state_dir)
            if not probe.present:
                continue
            holder = probe.holder
            if (
                holder is not None
                and holder.pid == os.getpid()
                and is_local_machine(holder.machine_id)
            ):
                if _rewrite_owned_marker(marker, flow_id):
                    return MarkerClaim(True, holder, False)
                return MarkerClaim(False, holder, False)
            if (
                attempt == 0
                and holder is not None
                and is_local_machine(holder.machine_id)
                and is_stale is not None
                and is_stale(holder)
            ):
                try:
                    marker.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    logger.debug(
                        "Reclaiming stale run.pid at %s failed", marker, exc_info=True
                    )
                    return MarkerClaim(False, holder, True)
                continue
            return MarkerClaim(False, holder, True)
        except OSError:
            logger.debug("Claiming run.pid at %s failed", marker, exc_info=True)
            return _unestablished_claim(state_dir)

        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(encode_run_pidfile(os.getpid(), stable_machine_id(), flow_id))
        except OSError:
            # An empty/partial record reads as an *undecidable* marker, which
            # every destructive caller must treat as held — leaving one behind
            # would wedge the flow for good, so drop what we created.
            logger.debug("Writing run.pid at %s failed", marker, exc_info=True)
            try:
                marker.unlink()
            except OSError:  # pragma: no cover - defensive
                pass
            return _unestablished_claim(state_dir)

        holder = read_run_holder(state_dir)
        if (
            holder is None
            or holder.pid != os.getpid()
            or not is_local_machine(holder.machine_id)
        ):
            # Someone wrote over the record we just created — only possible from
            # a writer that does not use this protocol (a pre-upgrade tianluo on
            # another host). Reporting it as blocked keeps the destructive
            # callers on the safe side.
            return MarkerClaim(False, holder, True)
        return MarkerClaim(True, holder, False)

    return MarkerClaim(False, None, True)


def holds_run_marker(state_dir: Path) -> bool:
    """Whether ``<state_dir>/run.pid`` still names THIS process on this machine.

    Re-checked at the destructive boundaries of ``luo end-session``: the
    exclusive claim above is only half the guarantee while a pre-upgrade
    ``luo run`` may still exist on another host of a shared filesystem, and one
    of those publishes its marker unconditionally. Never raises.
    """
    try:
        holder = read_run_holder(state_dir)
    except Exception:  # noqa: BLE001 - a failed read proves nothing
        return False
    return (
        holder is not None
        and holder.pid == os.getpid()
        and is_local_machine(holder.machine_id)
    )


def release_run_marker(state_dir: Path, *, drop_undecodable: bool = False) -> bool:
    """Remove ``<state_dir>/run.pid`` iff it still names THIS process.

    Ownership is re-checked before unlinking so a marker a concurrent run has
    since written over ours is never removed — clearing a live run's marker is
    the double-writer hole the whole cross-machine guard exists to close.
    Returns whether the marker was removed. Never raises.

    *drop_undecodable* additionally removes a record that cannot be decoded at
    all. Only the exiting ``luo run`` that owned this state dir for the whole
    flow passes it: an undecidable marker blocks every later start AND every
    end-session (both must treat it as held), so leaving a corrupted record of
    our own behind would wedge the flow permanently. A caller whose ownership
    is momentary — end-session's claim — must NOT pass it, since there the
    unreadable record may be the live remote run it is guarding against.
    """
    state_dir = Path(state_dir)
    try:
        if not holds_run_marker(state_dir):
            if not (drop_undecodable and probe_run_marker(state_dir).undecidable):
                return False
        (state_dir / RUN_PID_FILENAME).unlink()
        return True
    except FileNotFoundError:
        return False
    except Exception:  # noqa: BLE001 - releasing a claim must never raise
        logger.debug("Releasing run.pid at %s failed", state_dir, exc_info=True)
        return False
