"""SE3 End-Session command — terminate and archive a running session.

Ends an luo run flow: terminates the live ``luo run`` process (if any) and
archives the session. For a ``--worktree`` session this reuses the existing
``luo merge --delete-merged`` archival machinery (archive the worktree, promote
the terminal engine state into the main project's archive, sync history, delete
the isolation branch + worktree, clear the resumable snapshot) so the session
ends up archived exactly like a normally completed run — but WITHOUT merging the
(possibly unfinished / conflicting) work into the main branch. For a main-branch
session it simply archives the engine state and clears the resumable snapshot.

The primary motivation is the hanging worktree problem: a main-branch session
can simply be abandoned, but a ``--worktree`` session leaves a never-cleaned
worktree on disk; ``luo end-session`` gives the operator (and the daemon, on
behalf of the web console) a reliable way to clean it up.

Mirroring ``luo salvage``, the work is a fixed sequence of independently
fault-tolerant steps: a failure in one step is recorded but does not abort the
others, results are rendered as a Rich summary table, and the exit code is
non-zero when any step failed.
"""

from __future__ import annotations
from tianluo.runtime_paths import dual_runtime_glob, runtime_dir

import json
import logging
import os
import shutil
import signal
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich.console import Console
from rich.table import Table

from ..i18n import t

logger = logging.getLogger(__name__)
console = Console()

try:
    import psutil  # type: ignore
except Exception:  # pragma: no cover - psutil absent
    psutil = None  # type: ignore

# Seconds to wait for a process to exit after SIGTERM before escalating to
# SIGKILL.
DEFAULT_GRACE_SECONDS = 10.0


def end_session(
    project_root: Optional[Path] = None,
    flow_id: Optional[str] = None,
    pid: Optional[int] = None,
    archive_worktree: bool = True,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
) -> int:
    """Execute the end-session pipeline.

    Args:
        project_root: Project root directory. Auto-detected if None. A worktree
            copy path is normalized back to its owning main root.
        flow_id: The flow to end. When None, resolved from the main project's
            active ``engine.json``.
        pid: Optional hint for the live ``luo run`` process to terminate. When
            absent, the process is discovered by ``flow_id`` via psutil.
        archive_worktree: When True (default), a worktree session is archived
            (worktree archived + branch/worktree cleaned). When False, the live
            process is terminated but the worktree is left in place.
        grace_seconds: Time to wait after SIGTERM before SIGKILL.

    Returns:
        Exit code (0 = success, 1 = some step failed).
    """
    results: List[Tuple[str, str, str]] = []  # (step, status, detail)

    # -- Step 1: resolve the main project root ----------------------------
    try:
        project_root = _resolve_main_root(project_root)
        if project_root is None:
            console.print(t("end_session.no_project_root"))
            return 1
        # WHY: the root can sit above cwd (run from a subdirectory) or resolve to
        # a worktree's main root, and the caller bound the language before that
        # was known — re-bind now that the operating project is settled.
        from ..i18n import bind_project_root

        bind_project_root(project_root)
        results.append((t("end_session.step.resolve_root"), "OK", str(project_root)))
    except Exception as e:  # noqa: BLE001
        results.append((t("end_session.step.resolve_root"), "FAIL", str(e)[:80]))
        logger.warning("Step 1 (resolve root) failed: %s", e)
        _display_results(results)
        return 1

    # Resolve flow_id from the main engine.json when not supplied.
    if not flow_id:
        flow_id = _read_main_flow_id(project_root)
        if flow_id:
            logger.info("Resolved flow_id from main engine.json: %s", flow_id)

    # -- Step 2: discover a worktree session for this flow_id --------------
    wt_record: Optional[Dict[str, Any]] = None
    try:
        if flow_id:
            wt_record = _find_worktree_session(project_root, flow_id)
        if wt_record is not None:
            results.append(
                (
                    t("end_session.step.find_worktree"),
                    "OK",
                    t("end_session.detail.branch", branch=wt_record.get("worktree_branch")),
                )
            )
        else:
            results.append(
                (
                    t("end_session.step.find_worktree"),
                    "SKIP",
                    t("end_session.detail.main_branch_session"),
                )
            )
    except Exception as e:  # noqa: BLE001
        results.append((t("end_session.step.find_worktree"), "FAIL", str(e)[:80]))
        logger.warning("Step 2 (find worktree) failed: %s", e)

    # -- Step 3: terminate the live luo run process -----------------------
    # ``terminate_ok`` gates the destructive archive path below: when the
    # session process could not be killed it may still be executing, so
    # archiving / deleting its worktree (or clearing the main snapshot) would
    # remove or archive a stale snapshot out from under a live flow.
    terminate_ok = True
    try:
        worktree_path = (
            Path(wt_record["worktree_path"])
            if wt_record and wt_record.get("worktree_path")
            else None
        )
        terminate_ok, detail = _terminate_session_process(
            flow_id=flow_id,
            pid=pid,
            main_root=project_root,
            worktree_path=worktree_path,
            grace_seconds=grace_seconds,
        )
        results.append(
            (t("end_session.step.terminate_process"), "OK" if terminate_ok else "FAIL", detail)
        )
        if not terminate_ok:
            logger.warning("Step 3 (terminate process) did not confirm exit: %s", detail)
    except Exception as e:  # noqa: BLE001
        terminate_ok = False
        results.append((t("end_session.step.terminate_process"), "FAIL", str(e)[:80]))
        logger.warning("Step 3 (terminate process) failed: %s", e)

    # -- Step 3b: clear an abandoned LOCAL run.pid marker -------------------
    # Only meaningful once the process is confirmed gone. This is the operator
    # recovery path the cross-machine resume refusal points at: the marker can
    # only be judged dead on the host that wrote it, so end-session here is
    # what unblocks resuming the flow from any other machine.
    if terminate_ok:
        cleared_roots = [
            root
            for root in _run_marker_roots(project_root, wt_record)
            if _clear_stale_local_run_pidfile(root)
        ]
        for root in cleared_roots:
            results.append(
                (
                    t("end_session.step.clear_run_marker"),
                    "OK",
                    t("end_session.detail.run_marker_cleared", path=str(root)),
                )
            )

    # -- Step 3c: claim the flow's ownership marker ------------------------
    # WHY between the liveness verdict and the destructive steps: step 3 only
    # established that nothing owns the flow *at that instant*. On a shared
    # filesystem another machine may resume it a moment later, and the archive
    # below would then delete a running flow's worktree and review baselines.
    # Taking ``run.pid`` through the shared exclusive protocol
    # (``acquire_run_marker``, O_CREAT|O_EXCL) makes the two mutually exclusive
    # in both directions: a start/resume that got there first makes this claim
    # fail (and cleanup is skipped), and once the claim is held a start/resume
    # cannot publish over it — locally its own acquire is refused, remotely the
    # cross-machine guard additionally sees a foreign marker. The claim is
    # re-read before each irreversible step below, so the exclusion covers the
    # WHOLE destructive window rather than the instant it was taken.
    claimed: Optional[Path] = None
    claim_ok = True
    if terminate_ok:
        claimed, claim_ok = _claim_session_ownership(
            project_root, wt_record, flow_id
        )
        if not claim_ok:
            logger.warning(
                "Step 3c (claim ownership) failed for flow %s: a run marker "
                "appeared after the liveness check",
                flow_id,
            )

    try:
        # -- Step 4: archive -----------------------------------------------
        # Refuse to archive/clean while the session process is still alive: the
        # on-disk worktree + engine snapshot are still being written to, so a
        # destructive cleanup here could remove or archive a stale snapshot.
        if not terminate_ok:
            results.append(
                (
                    t("end_session.step.archive_session"),
                    "SKIP",
                    t("end_session.detail.process_still_alive"),
                )
            )
        elif not claim_ok:
            results.append(
                (
                    t("end_session.step.archive_session"),
                    "FAIL",
                    t("end_session.detail.ownership_claim_lost"),
                )
            )
        elif wt_record is not None and archive_worktree:
            _archive_worktree_session(
                project_root, flow_id, wt_record, results, claimed_marker=claimed
            )
        elif wt_record is not None and not archive_worktree:
            results.append(
                (
                    t("end_session.step.archive_worktree"),
                    "SKIP",
                    t("end_session.detail.no_archive_worktree_given"),
                )
            )
        else:
            _archive_main_session(
                project_root, flow_id, results, claimed_marker=claimed
            )
    finally:
        # The claim only guards this command's own destructive window; leaving
        # it behind would make the flow look held on this machine and block
        # every later resume.
        if claimed is not None:
            _release_run_marker(claimed)

    # -- Step 5: summary + aggregate exit code ----------------------------
    _display_results(results)
    has_failure = any(status == "FAIL" for _, status, _ in results)
    return 1 if has_failure else 0


# --------------------------------------------------------------------------
# Step 1 helpers
# --------------------------------------------------------------------------
def _find_project_root() -> Optional[Path]:
    """Find project root by looking for .git or an SE3 config file."""
    from ..config import is_se3_project_root

    cwd = Path.cwd()
    for p in [cwd] + list(cwd.parents):
        if (p / ".git").exists() or is_se3_project_root(p):
            return p
    return None


def _resolve_main_root(project_root: Optional[Path]) -> Optional[Path]:
    """Resolve the main project root, normalizing a worktree-copy path.

    A worktree isolation directory (``<main>/tianluo/worktrees/<name>``) is
    normalized back to its owning ``<main>`` so the archival writes land in the
    main project, not inside the worktree we are about to delete.

    WHY the root is absolutized here: an operator-supplied ``-p .`` / ``-p ..``
    is relative, while everything downstream must name the ONE state dir every
    other writer of this flow agrees on — the ownership claim in particular
    (:func:`_claim_run_marker` → ``acquire_run_marker``) rejects a
    cwd-relative state dir outright, since a path read against a working
    directory cannot be that agreed-on file. Auto-detection already yields a
    cwd-derived absolute path, so this only normalizes the explicit-``-p`` case,
    to the same canonical form ``luo run`` derives from ``Path.cwd()``.
    """
    if project_root is None:
        project_root = _find_project_root()
        if project_root is None:
            return None
    project_root = Path(project_root)
    if not project_root.is_absolute():
        project_root = project_root.resolve()
    try:
        from ..daemon.supervisor import resolve_worktree_main_root

        main = resolve_worktree_main_root(str(project_root))
        if main:
            return Path(main)
    except Exception:  # noqa: BLE001 - best effort normalization
        pass
    return project_root


def _read_main_flow_id(project_root: Path) -> Optional[str]:
    """Best-effort read of ``flow_id`` from the main project's engine.json."""
    engine_json = runtime_dir(project_root) / "state" / "engine.json"
    try:
        data = json.loads(engine_json.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    fid = data.get("flow_id") if isinstance(data, dict) else None
    return str(fid) if fid else None


# --------------------------------------------------------------------------
# Step 2 helpers
# --------------------------------------------------------------------------
def _find_worktree_session(
    project_root: Path, flow_id: str
) -> Optional[Dict[str, Any]]:
    """Locate the worktree session for *flow_id* under ``tianluo/worktrees/``.

    Scans ``<main>/tianluo/worktrees/*/tianluo/state/engine.json`` for an entry whose
    ``flow_id`` matches. Unlike :func:`run.find_resumable_worktree_runs` this is
    NOT filtered by status — a terminated flow may be in any non-COMPLETED state
    (PAUSED / FAILED / RUNNING) or even COMPLETED-but-unmerged, and we still
    want to clean it up. Returns ``None`` when no matching worktree is found
    (a main-branch session, or an already-cleaned worktree).
    """
    worktrees_dir = runtime_dir(project_root) / "worktrees"
    if not worktrees_dir.is_dir():
        return None

    for engine_file in sorted(dual_runtime_glob(worktrees_dir, "*/", "state/engine.json")):
        try:
            data = json.loads(engine_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        if str(data.get("flow_id")) != str(flow_id):
            continue
        worktree_path = data.get("worktree_path") or str(
            engine_file.parent.parent.parent
        )
        return {
            "flow_id": str(data.get("flow_id")),
            "status": data.get("status", "unknown"),
            "worktree_path": worktree_path,
            "worktree_branch": data.get("worktree_branch"),
            "worktree_original_branch": data.get("worktree_original_branch"),
        }
    return None


# --------------------------------------------------------------------------
# Step 3 helpers
# --------------------------------------------------------------------------
def _proc_alive(pid: int) -> bool:
    """Return whether *pid* names a live (non-zombie) process.

    A zombie (already-exited but not yet reaped by its parent) is treated as
    NOT alive: ``os.kill(pid, 0)`` succeeds for a zombie, so when psutil is
    available we additionally exclude the zombie status so a SIGKILL'd process
    awaiting reap by its parent is correctly seen as dead.
    """
    if pid <= 0:
        return False
    if psutil is not None:
        try:
            proc = psutil.Process(pid)
            return proc.status() != psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False
        except psutil.AccessDenied:
            return True
        except Exception:  # noqa: BLE001 - fall back to os.kill below
            pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another user — treat as alive.
        return True
    except OSError:
        return False
    return True


def _collect_descendants(pids: List[int]) -> set:
    """Return *pids* plus all their live recursive descendants.

    Ending a worktree session must terminate the whole live process tree, not
    just the top ``luo run`` parent: a running flow typically has an active
    Claude/Codex agent **child** subprocess executing inside the worktree, and
    killing only the parent would orphan that child (reparented to init), which
    would keep writing into the worktree while the command proceeds to archive
    and delete it. Capturing the descendant set up front — before SIGTERM kills
    the parent and orphans the children — lets us signal and verify the entire
    tree. When psutil is unavailable (or a pid no longer exists) we degrade to
    the bare pid set; the caller's own-pid exclusion is applied separately.
    """
    result: set = set()
    if psutil is None:
        return set(pids)
    for pid in pids:
        result.add(pid)
        try:
            proc = psutil.Process(pid)
            for child in proc.children(recursive=True):
                result.add(child.pid)
        except psutil.NoSuchProcess:
            continue
        except Exception:  # noqa: BLE001 - defensive; never block on scan
            continue
    return result


def _any_alive(pids) -> bool:
    """Return whether any pid in *pids* is currently alive."""
    return any(_proc_alive(p) for p in pids)


def _terminate_one(pid: int, grace_seconds: float) -> Tuple[bool, str]:
    """Terminate a pid and its whole descendant tree (SIGTERM → grace → SIGKILL).

    Ending a session must stop the entire live process tree — the top
    ``luo run`` parent **and** every agent (Claude/Codex) subprocess it spawned
    — before the worktree is archived or deleted, so no writer can keep mutating
    the worktree underneath the cleanup. The descendant set is captured up front
    (before the parent is killed and the children are orphaned), SIGTERM'd as a
    group, then any survivors are SIGKILL'd (re-collecting descendants of
    still-alive members to catch late-spawned grandchildren).

    Returns ``(ok, detail)`` where ``ok`` is ``True`` only when **every** process
    in the tree is confirmed dead (or none was running). If any member survives
    SIGKILL, or a signal fails outright (e.g. ``PermissionError``), returns
    ``ok=False`` so the caller can refuse the destructive archive path while a
    session process is still alive.
    """
    if not _proc_alive(pid):
        return True, t("end_session.term.not_running", pid=pid)

    own = os.getpid()
    # Capture the full tree up front, before SIGTERM orphans the children.
    tree = {p for p in _collect_descendants([pid]) if p != own and p > 0}
    if not tree:
        tree = {pid}

    n = len(tree)
    sigterm_failed: List[str] = []
    for p in tree:
        try:
            os.kill(p, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except OSError as exc:
            sigterm_failed.append(t("end_session.term.sigterm_failed", pid=p, exc=exc))

    deadline = time.monotonic() + max(0.0, grace_seconds)
    while time.monotonic() < deadline:
        if not _any_alive(tree):
            return True, t("end_session.term.sigterm_ok", pid=pid, n=n)
        time.sleep(0.1)

    # Escalate: re-collect descendants of still-alive members (late-spawned
    # grandchildren) and SIGKILL every survivor.
    survivors = {
        p
        for p in (tree | _collect_descendants([p for p in tree if _proc_alive(p)]))
        if p != own and p > 0
    }
    kill_failed: List[str] = list(sigterm_failed)
    for p in survivors:
        if not _proc_alive(p):
            continue
        try:
            os.kill(p, signal.SIGKILL)
        except ProcessLookupError:
            continue
        except OSError as exc:
            kill_failed.append(t("end_session.term.sigkill_failed", pid=p, exc=exc))

    # Brief wait for the kernel to reap the whole tree.
    kill_deadline = time.monotonic() + 2.0
    while time.monotonic() < kill_deadline:
        if not _any_alive(survivors):
            return True, t("end_session.term.sigkill_ok", pid=pid, n=len(survivors))
        time.sleep(0.1)

    alive = sorted(p for p in survivors if _proc_alive(p))
    detail = t("end_session.term.still_alive", pid=pid, alive=alive)
    if kill_failed:
        detail += "; " + "; ".join(kill_failed)
    return False, detail


def _read_run_pidfile(state_root: Path) -> Optional[int]:
    """Read the live-flow pid recorded in ``<state_root>/tianluo/state/run.pid``.

    ``luo run`` writes its own pid into this marker for the lifetime of the flow
    (in the *worktree's* state dir for a ``--worktree`` run), so end-session can
    locate the live process deterministically — including the otherwise
    un-findable case of a ``--worktree`` parent that keeps ``cwd==main_root`` and
    is momentarily between agent/test subprocesses (no descendant inside the
    worktree). Returns ``None`` when the marker is absent / unreadable / empty.

    Parses both the machine-aware two-line record and the legacy single-line
    (bare pid) record via the shared codec; only the pid is returned so the
    existing call sites are unchanged.

    WHY the machine gate: on a shared filesystem the marker may name a pid on
    ANOTHER host, where that number means nothing in our process table — an
    unrelated local ``luo run`` that happens to hold the same pid would match
    the liveness probe and be signalled by end-session while the real remote
    run keeps going. A foreign record therefore yields ``None``; only the
    owning machine (and legacy unstamped records, treated as local) may act on
    it. Clearing an abandoned foreign marker is the owning host's job — see
    :func:`_clear_stale_local_run_pidfile`.
    """
    from ..core.machine_id import is_local_machine
    from ..core.run_pidfile import read_run_pidfile

    pid, machine_id = read_run_pidfile(runtime_dir(state_root) / "state")
    if pid is not None and not is_local_machine(machine_id):
        logger.debug(
            "Ignoring run.pid at %s: recorded on machine %s", state_root, machine_id
        )
        return None
    return pid


def _run_marker_roots(
    project_root: Path, wt_record: Optional[Dict[str, Any]]
) -> List[Path]:
    """Roots whose ``run.pid`` marker belongs to the session being ended.

    A ``--worktree`` run writes its marker into the worktree's own state dir,
    so both it and the main root are candidates (mirrors the discovery order in
    :func:`_discover_pids_for_flow`).
    """
    roots: List[Path] = []
    if wt_record and wt_record.get("worktree_path"):
        roots.append(Path(wt_record["worktree_path"]))
    roots.append(Path(project_root))
    return roots


def _clear_stale_local_run_pidfile(state_root: Path) -> bool:
    """Remove ``<state_root>/tianluo/state/run.pid`` if it is a dead LOCAL marker.

    WHY this exists: a run killed without running its ``finally`` (SIGKILL, OOM,
    host reboot) leaves the marker behind. Cross-machine resume guards refuse a
    resume while a marker names another host, so an abandoned marker would make
    the project permanently un-resumable from every other machine. Running
    ``luo end-session`` on the owning host — the recovery the refusal message
    points at — clears it here, which is the only place that can: liveness of
    the recorded pid is only decidable on the machine that wrote it.

    Only ever unlinks a marker whose machine id is this machine (or absent, i.e.
    a legacy record) AND whose pid is no longer a live ``luo run``; a foreign or
    still-live marker is left untouched. Never raises.
    """
    try:
        from ..core.machine_id import is_local_machine
        from ..core.run_pidfile import RUN_PID_FILENAME, read_run_pidfile

        state_dir = runtime_dir(state_root) / "state"
        pid_file = state_dir / RUN_PID_FILENAME
        if not pid_file.exists():
            return False
        pid, machine_id = read_run_pidfile(state_dir)
        if not is_local_machine(machine_id):
            return False
        if pid is not None and pid != os.getpid() and _pid_is_live_se3_run(pid):
            return False
        pid_file.unlink()
        return True
    except Exception:  # noqa: BLE001 - the marker is purely advisory
        logger.debug("Failed to clear stale run.pid at %s", state_root, exc_info=True)
        return False


def _claim_run_marker(state_root: Path, flow_id: Optional[str]) -> Optional[Path]:
    """Claim ``<state_root>/tianluo/state/run.pid`` for THIS end-session process.

    Returns the claimed marker path, or ``None`` when the claim could not be
    taken — because a marker already exists (someone started or resumed the flow
    since we validated liveness) or the state dir could not be written.

    INVARIANT: the claim goes through the SAME exclusive publication protocol a
    starting/resuming ``luo run`` uses
    (:func:`~tianluo.core.run_pidfile.acquire_run_marker`, ``O_CREAT | O_EXCL``),
    so taking it and a concurrent run publishing its own marker cannot both
    succeed. Sharing one protocol is the whole mechanism: were either side to
    write unconditionally, "no marker exists" observed in step 3 and the
    archive/cleanup in step 4 would be two separate reads of a value the other
    side may change in between, and the cleanup would then delete the worktree
    and review baselines of a flow that has since been resumed.

    WHY the marker file and not a private lock name: ``run.pid`` is the one
    ownership token the counterparty consults — the cross-machine resume guard
    refuses a resume while a *foreign* marker holds the flow's state dir. A
    claim written anywhere else would be invisible to it, so the exclusion
    would only be half of a handshake. The claim is stamped with this machine's
    id exactly like a run's own marker, which is what makes a remote resume see
    it as held.

    WHY no stale-reclaim predicate is passed: an abandoned LOCAL marker was
    already cleared in step 3b, which alone can judge a recorded pid dead. Any
    marker still standing here therefore has an owner, and end-session must
    yield to it. Never raises.
    """
    from ..core.run_pidfile import RUN_PID_FILENAME, acquire_run_marker

    state_dir = runtime_dir(state_root) / "state"
    claim = acquire_run_marker(state_dir, flow_id)
    if not claim.acquired:
        if claim.blocked:
            logger.info(
                "run.pid at %s appeared before the claim; not claimed", state_root
            )
        else:
            logger.debug("Claiming run.pid at %s failed", state_root)
        return None
    return state_dir / RUN_PID_FILENAME


def _release_run_marker(marker: Path) -> None:
    """Drop a claim taken by :func:`_claim_run_marker`, if it still names us.

    Ownership is re-checked before unlinking so a marker a concurrent run has
    since written over ours is never removed — clearing a live run's marker is
    the double-writer hole the whole cross-machine guard exists to close. An
    *undecodable* record is likewise left alone (unlike the exiting run's own
    release): from here it is indistinguishable from a live remote run's marker.
    Never raises.
    """
    from ..core.run_pidfile import release_run_marker

    release_run_marker(Path(marker).parent)


def _claim_still_held(claimed_marker: Optional[Path]) -> bool:
    """Whether this command's ownership claim still names this process.

    WHY re-checked at every destructive boundary rather than once at step 3c:
    the exclusive claim only binds writers that follow the same protocol, and a
    shared filesystem can carry a *pre-upgrade* ``luo run`` on another host that
    still publishes ``run.pid`` unconditionally. Such a writer can overwrite the
    claim mid-window, which is precisely the moment the flow becomes live again
    — so ownership is re-read immediately before each irreversible step and the
    remaining destruction is abandoned when it is gone.

    ``None`` means no claim was needed (nothing left on disk to protect), which
    is not a lost claim.
    """
    from ..core.run_pidfile import holds_run_marker

    if claimed_marker is None:
        return True
    return holds_run_marker(Path(claimed_marker).parent)


def _abandon_if_claim_lost(
    claimed_marker: Optional[Path],
    results: List[Tuple[str, str, str]],
    step_key: str,
) -> bool:
    """Record *step_key* as abandoned when the ownership claim is gone.

    INVARIANT: EVERY destructive step of the cleanup — not just the final
    worktree/branch deletion — is immediately preceded by one of these checks.
    The claim can be overwritten mid-window by a pre-upgrade ``luo run`` on
    another host (see :func:`_claim_still_held`), and the moment that happens
    the flow is live again and still NEEDS the artifacts the remaining steps
    would delete: its review baselines and its resumable snapshot. Deleting
    them "because we already checked on entry" leaves a running flow without
    the state it resumes and self-checks from, which no later step can undo.
    Returns ``True`` when the caller must skip the step it guards.
    """
    if _claim_still_held(claimed_marker):
        return False
    results.append(
        (t(step_key), "FAIL", t("end_session.detail.ownership_claim_lost"))
    )
    return True


def _claim_session_ownership(
    project_root: Path, wt_record: Optional[Dict[str, Any]], flow_id: Optional[str]
) -> Tuple[Optional[Path], bool]:
    """Take the flow's ownership marker for the duration of the destructive steps.

    Returns ``(claimed_marker, ok)``. ``ok`` is ``False`` only when a marker was
    found where the claim should have gone (or it could not be written): the
    flow has an owner again, so archive/cleanup must not run.

    INVARIANT: the claim is taken in exactly ONE state dir — the one the target
    flow actually writes (its worktree's for a ``--worktree`` session, the main
    root's otherwise), mirroring the cross-machine resume guard. Claiming the
    main root as well for a worktree session would wedge an *unrelated*
    main-root flow's resume on another machine behind this cleanup, since a
    worktree flow body shares no state file with the main root.

    A vanished worktree directory is not claimed: there is no live state left to
    protect there, and creating the marker would resurrect a stray
    ``<worktree>/tianluo/state/`` in a directory the cleanup is about to drop.
    """
    root = (
        Path(wt_record["worktree_path"])
        if wt_record and wt_record.get("worktree_path")
        else Path(project_root)
    )
    if not root.exists():
        return None, True
    marker = _claim_run_marker(root, flow_id)
    if marker is None:
        return None, False
    return marker, True


def _pid_is_live_se3_run(pid: int) -> bool:
    """Return whether *pid* is a live process whose cmdline is an ``luo run``.

    Guards the ``run.pid`` marker against staleness: a recorded pid that has
    since died, or been recycled by an unrelated process, must NOT be signalled.
    When psutil is unavailable we fall back to a bare liveness probe (the
    cmdline cannot be inspected), which is still safe because the pid was written
    by an ``luo run`` into this flow's own state dir.

    WHY an empty cmdline means "live", not "stale": ``/proc/<pid>/cmdline`` is
    momentarily EMPTY while a process is between fork and exec (and for kernel
    threads / zombies). A live ``luo run`` that has just spawned — or that is
    read at exactly the wrong instant — would then be judged a recycled pid and
    its worktree archived/deleted underneath it. An unreadable cmdline is
    *inconclusive*, so it falls back to the same bare-liveness rule as the
    psutil-less path; only a cmdline we could actually read and that is NOT an
    ``luo run`` proves recycling. The reverse bias is unsafe: refusing to end a
    session is recoverable, destroying a live flow's worktree is not.
    """
    if not _proc_alive(pid):
        return False
    if psutil is None:
        return True
    from ..daemon.supervisor import _cmdline_is_se3_run

    try:
        cmdline = psutil.Process(pid).cmdline()
    except psutil.NoSuchProcess:
        # The process vanished between the liveness probe and the inspection:
        # genuinely gone, so the marker is stale.
        return False
    except psutil.AccessDenied:
        # Liveness is already established; only the cmdline is unreadable (a
        # hardened /proc, a differently-owned process). Inconclusive, so keep
        # the bare-liveness verdict rather than declaring the live run absent.
        return True
    except Exception:  # noqa: BLE001 - defensive; inspection failure is inconclusive
        return True
    if not cmdline:
        return True
    return _cmdline_is_se3_run(cmdline)


def _proc_has_descendant_in_dir(proc: Any, target_real: str) -> bool:
    """Return whether *proc*'s process tree is anchored inside *target_real*.

    A ``luo run --worktree`` flow's parent process never chdirs: it keeps its
    ``cwd`` at the main root and writes its ``engine.json`` into the worktree, so
    the main project's ``engine.json`` carries a different / absent ``flow_id``
    and cannot confirm this flow. The agent (Claude/Codex) subprocess it spawns,
    however, runs with its ``cwd`` set to the (per-flow unique) worktree
    directory, and the parent / its children frequently hold files open under the
    worktree (history jsonl, prompt snapshots, state). Matching either a
    descendant whose ``cwd`` is inside the worktree path OR any tree member that
    holds an open file under it positively identifies the live parent of this
    specific worktree flow, so the whole tree can be terminated before the
    destructive archive/cleanup runs.
    """
    if psutil is None or not target_real:
        return False
    prefix = target_real + os.sep
    # Check the parent itself plus all descendants for an open file under the
    # worktree (covers a parent that is between agent/test subprocesses but still
    # holds worktree files open).
    try:
        members = [proc] + proc.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        members = [proc]
    except Exception:  # noqa: BLE001 - defensive
        members = [proc]
    for member in members:
        try:
            for of in member.open_files():
                ofpath = getattr(of, "path", None)
                if not ofpath:
                    continue
                try:
                    ofreal = os.path.realpath(ofpath)
                except OSError:  # pragma: no cover - defensive
                    continue
                if ofreal == target_real or ofreal.startswith(prefix):
                    return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:  # noqa: BLE001 - defensive; never block on scan
            continue
    try:
        children = proc.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    except Exception:  # noqa: BLE001 - defensive; never block on scan
        return False
    for child in children:
        try:
            ccwd = child.cwd()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:  # noqa: BLE001 - defensive
            continue
        if not ccwd:
            continue
        try:
            ccwd_real = os.path.realpath(ccwd)
        except OSError:  # pragma: no cover - defensive
            continue
        if ccwd_real == target_real or ccwd_real.startswith(target_real + os.sep):
            return True
    return False


def _discover_pids_for_flow(
    flow_id: Optional[str],
    main_root: Path,
    worktree_path: Optional[Path],
) -> List[int]:
    """Discover live ``luo run`` pids that belong to *flow_id*.

    The most reliable source is the ``run.pid`` marker ``luo run`` writes into the
    flow's own ``tianluo/state`` dir (the worktree's for a ``--worktree`` run): it
    pins the live process regardless of ``cwd`` and even when the parent is
    momentarily between agent/test subprocesses with no descendant in the
    worktree. Failing that (an older run that predates the marker, or a stale
    marker), a match is also made when an ``luo run`` process's ``cwd`` is the
    worktree path (worktree session) or the main root (main-branch session) and
    its ``engine.json`` carries the requested ``flow_id``, OR when its command
    line explicitly carries ``--flow-id <flow_id>`` (the resume case), OR — for a
    live ``--worktree`` flow whose parent stays at the main root while its agent
    child runs inside the worktree — when its process tree is anchored inside this
    flow's unique worktree path (a descendant cwd or any open file under it).
    """
    pids: List[int] = []

    # 1) The authoritative source: the on-disk run.pid marker. For a worktree
    #    session it lives in the worktree's own state dir; for a main-branch
    #    session it lives in the main root's state dir. Verify the recorded pid
    #    is a live ``luo run`` before trusting it, so a dead / recycled pid is
    #    never signalled.
    pid_roots: List[Path] = []
    if worktree_path is not None:
        pid_roots.append(Path(worktree_path))
    pid_roots.append(Path(main_root))
    own_pid = os.getpid()
    for root in pid_roots:
        recorded = _read_run_pidfile(root)
        if recorded and recorded != own_pid and _pid_is_live_se3_run(recorded):
            pids.append(recorded)

    if psutil is None:
        return list(dict.fromkeys(pids))

    from ..daemon.supervisor import _cmdline_is_se3_run

    targets: set[str] = set()
    try:
        targets.add(os.path.realpath(str(main_root)))
    except OSError:  # pragma: no cover - defensive
        pass
    if worktree_path is not None:
        try:
            targets.add(os.path.realpath(str(worktree_path)))
        except OSError:  # pragma: no cover - defensive
            pass

    worktree_real: Optional[str] = None
    if worktree_path is not None:
        try:
            worktree_real = os.path.realpath(str(worktree_path))
        except OSError:  # pragma: no cover - defensive
            worktree_real = None

    try:
        current = psutil.Process().pid
        for proc in psutil.process_iter(["pid", "cmdline", "cwd"]):
            try:
                info = proc.info
                pid = info.get("pid")
                cmdline = info.get("cmdline") or []
                if pid is None or pid == current:
                    continue
                if not _cmdline_is_se3_run(cmdline):
                    continue

                # Explicit --flow-id on the command line (resume case).
                if flow_id and "--flow-id" in cmdline:
                    try:
                        idx = cmdline.index("--flow-id")
                        if idx + 1 < len(cmdline) and cmdline[idx + 1] == flow_id:
                            pids.append(pid)
                            continue
                    except ValueError:  # pragma: no cover - defensive
                        pass

                cwd = info.get("cwd")
                if not cwd:
                    continue
                try:
                    cwd_real = os.path.realpath(cwd)
                except OSError:  # pragma: no cover - defensive
                    continue
                if cwd_real not in targets:
                    continue
                # A cwd inside the session-unique worktree path is itself a
                # session-specific match and is accepted directly. A cwd at the
                # (shared) main root is ambiguous — an unrelated concurrent
                # ``luo run`` may also run there — so when a specific flow_id is
                # requested we require the engine.json at that cwd to POSITIVELY
                # confirm the same flow_id. An unreadable/absent on-disk flow_id
                # (corrupt, mid-write, or an INIT-phase flow not yet carrying a
                # flow_id) is NOT a confirmation and must NOT be terminated, lest
                # an unrelated session be killed by cwd alone.
                is_worktree_cwd = (
                    worktree_real is not None and cwd_real == worktree_real
                )
                if flow_id and not is_worktree_cwd:
                    on_disk = _read_main_flow_id(Path(cwd_real))
                    if on_disk != str(flow_id):
                        # A ``luo run --worktree`` flow's parent keeps
                        # cwd==main_root while its engine.json lives in the
                        # worktree, so the main engine.json's flow_id cannot
                        # confirm it. Fall back to matching a descendant (the
                        # agent subprocess) running inside this flow's unique
                        # worktree path, which positively identifies the live
                        # parent so it is terminated before archive/cleanup.
                        if worktree_real is None or not _proc_has_descendant_in_dir(
                            proc, worktree_real
                        ):
                            continue
                pids.append(pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:  # noqa: BLE001 - defensive, never block on scan
        logger.debug("Process scan for flow %s failed", flow_id, exc_info=True)
    return list(dict.fromkeys(pids))


def _blocking_run_marker(
    flow_id: Optional[str],
    main_root: Path,
    worktree_path: Optional[Path],
) -> Optional[Tuple[Path, Any]]:
    """Return ``(marker_path, holder)`` for a ``run.pid`` this host cannot judge dead.

    Searches the same roots :func:`_discover_pids_for_flow` reads markers from
    (the worktree's state dir first for a ``--worktree`` session, then the main
    root). Returns ``None`` when every marker is absent, local, or provably
    stamped with a *different* flow than the one being ended. A non-``None``
    result carries the holder when one could be decoded, and ``None`` in its
    place when the marker exists but its owner could not be read at all.

    WHY end-session must refuse on either: the recorded pid belongs to a
    process table this host can never observe, so :func:`_read_run_pidfile`
    yields no pid for it. Without this check the absence of a *local* process
    reads as "nothing is running", termination is reported successful, and the
    archive path then deletes the worktree and ``review-scopes/<flow_id>`` out
    from under a flow that is still executing on its owning machine. The marker
    is the only evidence that flow exists, so one this host cannot resolve to a
    dead local process makes termination INCONCLUSIVE (never "done"), which is
    what keeps the destructive steps skipped — matching the project-wide rule
    that a foreign ownership marker is always treated as held and is never
    broken from another host.

    INVARIANT: an *undecodable* marker (unreadable through permissions or an
    I/O error, truncated, or garbage) blocks exactly like a foreign one. It is
    indistinguishable from the marker of a run that is alive on another host —
    the shared-filesystem case where a transient read failure is expected — so
    collapsing it into "no marker" would restore the very verdict this guard
    exists to prevent. Recovering from a genuinely corrupt marker is the
    operator's explicit act (inspect it, then remove the file); it must never be
    inferred from a failed read.

    An unstamped foreign marker (a run that had not yet minted its flow id)
    also blocks: it cannot be shown to be a different flow, and refusing to end
    a session is recoverable where deleting a live flow's worktree is not. The
    recovery is the same one the cross-machine resume refusal points at — run
    ``luo end-session`` on the owning machine.
    """
    from ..core.machine_id import is_local_machine
    from ..core.run_pidfile import RUN_PID_FILENAME, probe_run_marker

    roots: List[Path] = []
    if worktree_path is not None:
        roots.append(Path(worktree_path))
    roots.append(Path(main_root))

    for root in roots:
        state_dir = runtime_dir(root) / "state"
        marker_path = state_dir / RUN_PID_FILENAME
        try:
            probe = probe_run_marker(state_dir)
        except Exception:  # noqa: BLE001 - a probe that itself fails proves nothing
            logger.debug("Reading run.pid at %s failed", root, exc_info=True)
            return marker_path, None
        if not probe.present:
            continue
        if probe.undecidable:
            return marker_path, None
        holder = probe.holder
        if is_local_machine(holder.machine_id):
            # A local marker is decidable here: process discovery below probes
            # the recorded pid, and a dead one is reclaimed in step 3b.
            continue
        if flow_id and holder.flow_id and not holder.owns_flow(str(flow_id)):
            # Provably a different flow's run on that host: it is not the flow
            # we are ending, so it is no evidence about this one.
            continue
        return marker_path, holder
    return None


def _terminate_session_process(
    flow_id: Optional[str],
    pid: Optional[int],
    main_root: Path,
    worktree_path: Optional[Path],
    grace_seconds: float,
) -> Tuple[bool, str]:
    """Terminate the live ``luo run`` process(es) for the session.

    Uses *pid* when supplied; otherwise discovers candidates by *flow_id*.
    Returns ``(ok, summary)``. ``ok`` is ``True`` when every targeted process is
    confirmed dead (or there was no live process — a PAUSED/FAILED worktree is
    fine), and ``False`` when any process could not be terminated (e.g. a
    permission failure or a process that survives SIGKILL) or when the session's
    run marker is owned by another machine — or cannot be read at all — leaving
    liveness undecidable from here, so the caller can refuse the destructive
    archive path while the session may still be executing.
    """
    # A run marker this host cannot judge dead — owned by another machine, or
    # unreadable — is decided BEFORE any local discovery: its pid is meaningless
    # (or unknown) here, so no local evidence (an absent process, or even an
    # operator-supplied ``--pid``) can prove the recorded run has stopped.
    # Answering "inconclusive" is what keeps archive/cleanup from deleting a
    # live flow's worktree and review baselines.
    blocking = _blocking_run_marker(flow_id, main_root, worktree_path)
    if blocking is not None:
        marker_path, holder = blocking
        if holder is None:
            return False, t(
                "end_session.term.marker_unreadable", path=str(marker_path)
            )
        return False, t(
            "end_session.term.foreign_machine",
            machine=holder.machine_id,
            pid=holder.pid,
            path=str(marker_path),
        )

    pids: List[int] = []
    if pid is not None and pid > 0:
        pids = [pid]
    else:
        pids = _discover_pids_for_flow(flow_id, main_root, worktree_path)

    if not pids:
        return True, t("end_session.term.no_live_process")

    all_ok = True
    details: List[str] = []
    for p in dict.fromkeys(pids):
        ok, detail = _terminate_one(p, grace_seconds)
        all_ok = all_ok and ok
        details.append(detail)
    return all_ok, "; ".join(details)


# --------------------------------------------------------------------------
# Step 4 helpers
# --------------------------------------------------------------------------
def _archive_worktree_session(
    project_root: Path,
    flow_id: Optional[str],
    wt_record: Dict[str, Any],
    results: List[Tuple[str, str, str]],
    claimed_marker: Optional[Path] = None,
) -> None:
    """Archive a worktree session, reusing the merge-cleanup machinery.

    Each sub-step is independently fault-tolerant; failures are recorded as a
    FAIL row but never abort the remaining sub-steps.

    *claimed_marker* is this command's own ``run.pid`` claim inside the
    worktree (see :func:`_claim_session_ownership`); it is kept out of the
    archive copy, which must record the terminated flow rather than the
    end-session process that reaped it.
    """
    from ..engine.merge.cleanup import (
        _archive_worktree,
        _promote_completed_engine_state,
    )
    from ..engine import worktree as wt_mod

    worktree_path = Path(wt_record["worktree_path"])
    branch = wt_record.get("worktree_branch")

    # Whether it is safe to destroy the on-disk worktree + isolation branch
    # below. The destructive cleanup (4.5) must NOT run unless we have either
    # successfully created an archive copy or confirmed there is nothing left
    # to lose (the worktree directory is already gone). A failed archive (disk
    # full, permission denied, etc.) leaves the only copy of the unfinished /
    # unmerged work in the live worktree, so deleting it would lose that work.
    archive_ok = False

    # The claim is re-read before EVERY irreversible step below, not just once
    # at step 3c — see :func:`_abandon_if_claim_lost`. This one guards the whole
    # archival sequence: a claim lost by here means the flow is owned again, so
    # nothing at all may be copied out or destroyed on its behalf.
    if _abandon_if_claim_lost(
        claimed_marker, results, "end_session.step.archive_session"
    ):
        return

    # 4.0 — locate the flow's review baselines so the archive copy can exclude
    # them, WITHOUT deleting the live copies yet. The archive is a verbatim copy
    # of the worktree, so snapshots still on disk at 4.1 would be duplicated
    # into tianluo/worktrees/.archive/ for good — a content store with no reader
    # left (the flow is terminated, and the archive holds the worktree files
    # themselves, which is the recovery data).
    #
    # INVARIANT: excluding the directory from the copy is the ONLY baseline
    # guard that may run here; the reclaim itself is deferred to 4.4, behind the
    # next ownership re-read. WHY: 4.1-4.3 copy a whole worktree, promote state
    # and sync history — a window wide enough for a pre-upgrade ``luo run`` on
    # another host to overwrite the claim. Deleting the baselines up front would
    # be irreversible by the time that takeover is noticed, leaving a re-owned,
    # live flow unable to reconstruct its SELF_CHECK scope; the exclusion keeps
    # the permanent copy clean either way, so nothing is lost by waiting.
    archive_excludes: List[str] = []
    if flow_id and worktree_path.exists():
        from ..engine.review_scope import flow_snapshot_relpath

        snapshot_relpath = flow_snapshot_relpath(worktree_path, flow_id)
        if snapshot_relpath:
            archive_excludes.append(snapshot_relpath)

    # The ownership claim this command holds for the duration of the cleanup is
    # end-session's own bookkeeping, not part of the flow being preserved — an
    # archive carrying it would read as a session that died holding a run.pid.
    if claimed_marker is not None:
        try:
            archive_excludes.append(
                str(Path(claimed_marker).relative_to(worktree_path))
            )
        except ValueError:  # pragma: no cover - claim outside this worktree
            logger.debug(
                "Claim %s is not inside worktree %s", claimed_marker, worktree_path
            )

    # 4.1 — copy the worktree directory into tianluo/worktrees/.archive/.
    if worktree_path.exists():
        try:
            archive_path = _archive_worktree(
                project_root,
                branch or (flow_id or "worktree"),
                worktree_path,
                exclude_relpaths=archive_excludes,
            )
            results.append((t("end_session.step.archive_worktree"), "OK", str(archive_path)))
            archive_ok = True
        except Exception as e:  # noqa: BLE001
            results.append((t("end_session.step.archive_worktree"), "FAIL", str(e)[:80]))
            logger.warning("Archive worktree failed: %s", e)
    else:
        results.append(
            (
                t("end_session.step.archive_worktree"),
                "SKIP",
                t("end_session.detail.worktree_dir_gone"),
            )
        )
        # Nothing on disk to lose — the branch metadata cleanup is still safe.
        archive_ok = True

    # 4.2 — promote the worktree's terminal engine.json into the main archive
    # regardless of status (force=True).
    try:
        promoted = _promote_completed_engine_state(
            project_root, worktree_path, force=True
        )
        if promoted is not None:
            results.append((t("end_session.step.promote_state"), "OK", str(promoted)))
        else:
            results.append(
                (
                    t("end_session.step.promote_state"),
                    "SKIP",
                    t("end_session.detail.no_engine_json_to_promote"),
                )
            )
    except Exception as e:  # noqa: BLE001
        results.append((t("end_session.step.promote_state"), "FAIL", str(e)[:80]))
        logger.warning("Promote state failed: %s", e)

    # 4.3 — sync the worktree's history into the main project's history.
    try:
        synced = _sync_worktree_history(
            project_root, worktree_path, flow_id, branch
        )
        results.append((t("end_session.step.sync_history"), "OK", t("end_session.detail.files_synced", count=synced)))
    except Exception as e:  # noqa: BLE001
        results.append((t("end_session.step.sync_history"), "FAIL", str(e)[:80]))
        logger.warning("Sync history failed: %s", e)

    # 4.4 — clear the worktree's resumable snapshot AND reclaim its review
    # baselines (see :func:`_clear_resumable`): this is the single point where
    # either is destroyed, so both die on one ownership decision rather than the
    # baselines going early at 4.0 and the snapshot surviving a takeover that
    # landed in between. Done BEFORE the worktree
    # is removed below: PersistenceManager re-creates ``tianluo/state/`` on
    # construction, so clearing after deletion would leave a stray empty dir
    # behind in the just-removed worktree path. For the same reason, skip this
    # entirely when the worktree directory is already gone: there is no snapshot
    # to clear in a vanished worktree, and constructing PersistenceManager would
    # re-create ``<worktree_path>/tianluo/state`` on disk — exactly the stray remnant
    # we are trying to avoid.
    #
    # Ownership is re-read first: 4.1-4.3 copy a whole worktree and its history,
    # a window wide enough for a takeover to land before this deletes the
    # snapshot (and the review baselines) a re-owned flow still needs.
    if _abandon_if_claim_lost(
        claimed_marker, results, "end_session.step.clear_resumable"
    ):
        logger.info("Ownership claim lost; leaving %s resumable state intact", flow_id)
    elif worktree_path.exists():
        try:
            # INVARIANT: the baseline reclaim also requires the archive to have
            # landed. When 4.1 failed, 4.5 below deliberately preserves the
            # branch and worktree as the only copy of the unfinished work — and
            # ``find_resumable_worktree_runs`` scans those worktrees' still
            # non-COMPLETED engine.json, so the resume picker keeps offering
            # the flow. Reclaiming its baselines would leave that resume unable
            # to reconstruct any SELF_CHECK scope; the snapshot clear itself is
            # survivable (a resumed flow rebuilds it), the baselines are not.
            if _clear_resumable(
                worktree_path,
                flow_id,
                claimed_marker,
                reclaim_baselines=archive_ok,
            ):
                results.append(
                    (
                        t("end_session.step.clear_resumable"),
                        "OK",
                        (flow_id or t("end_session.detail.unknown"))
                        if archive_ok
                        else t(
                            "end_session.detail.baselines_kept_archive_failed"
                        ),
                    )
                )
            else:
                results.append(
                    (
                        t("end_session.step.clear_resumable"),
                        "FAIL",
                        t("end_session.detail.baselines_kept"),
                    )
                )
        except Exception as e:  # noqa: BLE001
            results.append((t("end_session.step.clear_resumable"), "FAIL", str(e)[:80]))
            logger.warning("Clear resumable failed: %s", e)
    else:
        results.append(
            (
                t("end_session.step.clear_resumable"),
                "SKIP",
                t("end_session.detail.worktree_dir_gone"),
            )
        )

    # 4.5 — delete the isolation branch and force-clean the worktree.
    # Skipped when the archive could not be created (4.1 FAIL): destroying the
    # branch + worktree then would lose the only copy of the unfinished work,
    # and equally when the ownership claim was taken from us in the meantime:
    # this is the step that would delete a re-owned flow's live worktree.
    if _abandon_if_claim_lost(
        claimed_marker, results, "end_session.step.cleanup_branch"
    ):
        logger.info("Ownership claim lost; leaving worktree %s intact", worktree_path)
    elif not archive_ok:
        results.append(
            (
                t("end_session.step.cleanup_branch"),
                "SKIP",
                t("end_session.detail.archive_not_created"),
            )
        )
    else:
        # The worktree_branch may be absent from a worktree's engine.json (an
        # older / corrupt / tolerant state). The worktree has already been
        # archived above, so we MUST still remove the on-disk worktree and its
        # git registration rather than leaving it hanging — the whole reason
        # end-session exists. Prefer the recorded branch; otherwise infer it
        # from git's worktree metadata; failing that, remove by path.
        cleanup_branch = branch or _infer_worktree_branch(
            project_root, worktree_path
        )
        # INVARIANT: ownership is re-read AFTER the branch inference, not only
        # before it. ``_infer_worktree_branch`` shells out to ``git worktree
        # list`` — an unbounded wait on a busy or network-mounted repository —
        # so the check above can be arbitrarily old by the time the deletions
        # below run. Nothing here is undoable: ``delete_branch`` +
        # ``force_cleanup_worktree`` (and the by-path removal) destroy the live
        # worktree of a flow a pre-upgrade remote ``luo run`` may have re-owned
        # inside exactly that window.
        if _abandon_if_claim_lost(
            claimed_marker, results, "end_session.step.cleanup_branch"
        ):
            logger.info(
                "Ownership claim lost during branch inference; leaving "
                "worktree %s intact",
                worktree_path,
            )
            return
        try:
            if cleanup_branch:
                wt_mod.delete_branch(project_root, cleanup_branch)
                wt_mod.force_cleanup_worktree(project_root, cleanup_branch)
                detail = (
                    cleanup_branch
                    if branch
                    else t("end_session.detail.inferred", branch=cleanup_branch)
                )
                results.append((t("end_session.step.cleanup_branch"), "OK", detail))
            else:
                # No branch recorded and none inferable — remove the worktree
                # directory + registration by path so it is not left behind.
                # ``remove_worktree`` only drives git (remove/prune): for a
                # directory that git no longer tracks as a registered worktree
                # (stale / lost metadata), git leaves the on-disk directory in
                # place, so we follow up with an explicit rmtree and a final
                # prune, then verify the directory is actually gone before
                # reporting success — otherwise a stale worktree would survive
                # an end-session that reported OK.
                wt_mod.remove_worktree(project_root, worktree_path)
                if worktree_path.exists():
                    shutil.rmtree(worktree_path, ignore_errors=True)
                    # Prune any git metadata now the directory is gone.
                    try:
                        wt_mod.remove_worktree(project_root, worktree_path)
                    except Exception:  # noqa: BLE001 - best effort prune
                        logger.debug(
                            "Post-rmtree prune failed for %s",
                            worktree_path,
                            exc_info=True,
                        )
                if worktree_path.exists():
                    results.append(
                        (
                            t("end_session.step.cleanup_worktree"),
                            "FAIL",
                            t("end_session.detail.worktree_still_present", worktree_path=worktree_path),
                        )
                    )
                else:
                    results.append(
                        (
                            t("end_session.step.cleanup_worktree"),
                            "OK",
                            t("end_session.detail.removed_by_path", worktree_path=worktree_path),
                        )
                    )
        except Exception as e:  # noqa: BLE001
            results.append((t("end_session.step.cleanup_branch"), "FAIL", str(e)[:80]))
            logger.warning("Cleanup branch/worktree failed: %s", e)


def _archive_main_session(
    project_root: Path,
    flow_id: Optional[str],
    results: List[Tuple[str, str, str]],
    claimed_marker: Optional[Path] = None,
) -> None:
    """Archive a main-branch session: clear_state + clear resumable snapshot.

    When a specific *flow_id* was requested we MUST only archive the main
    engine state if it actually belongs to that flow. Otherwise — e.g. ending
    flow A whose worktree is already gone while the main project is busy with a
    different active flow B — we would archive (delete) the unrelated flow B's
    session, ending the wrong session. In that case we skip the destructive
    clear and only proceed to the (flow-scoped, harmless) resumable cleanup.

    *claimed_marker* is this command's ``run.pid`` claim; it is re-read here so
    a flow that regained an owner between step 3c and now is left intact — see
    :func:`_claim_still_held`.
    """
    from ..engine.persistence import PersistenceManager

    if _abandon_if_claim_lost(
        claimed_marker, results, "end_session.step.archive_session"
    ):
        return

    # INVARIANT: the review baselines are reclaimed only once the terminal
    # disposition has actually landed. A ``clear_state`` that raises (an
    # unwritable / full ``tianluo/state/archive/``) leaves the live engine.json
    # holding the flow in a non-COMPLETED state, and ``load_flow_by_id``
    # resolves that file FIRST — so the resume picker keeps offering the flow
    # even though its resumable snapshot cleared. Dropping the baselines there
    # is the one unrepairable outcome: the resumed flow reaches SELF_CHECK with
    # no baseline to diff against. This mirrors the engine-side ordering, where
    # ``save_flow`` must succeed before the snapshots are discarded.
    disposition_ok = True

    try:
        pm = PersistenceManager(project_root)
        if pm.state_file.exists():
            main_flow_id = _read_main_flow_id(project_root)
            # When a specific flow_id was requested, the destructive clear is
            # only safe once we have POSITIVELY confirmed the main engine.json
            # belongs to that same flow. An absent / unreadable flow_id (a
            # different flow's engine.json that is mid-write, in INIT, or
            # corrupt) counts as NOT confirmed — clearing it would archive the
            # wrong, unrelated session. Only with no specific flow_id requested
            # do we fall back to the unconditional clear.
            if flow_id and str(main_flow_id) != str(flow_id):
                if main_flow_id:
                    detail = t(
                        "end_session.detail.main_flow_mismatch",
                        main_flow_id=main_flow_id,
                        flow_id=flow_id,
                    )
                else:
                    detail = t(
                        "end_session.detail.main_flow_unreadable",
                        flow_id=flow_id,
                    )
                results.append((t("end_session.step.archive_session"), "SKIP", detail))
            elif _abandon_if_claim_lost(
                claimed_marker, results, "end_session.step.archive_session"
            ):
                # INVARIANT: ownership is re-read AFTER the engine.json read,
                # immediately before the clear. Constructing PersistenceManager
                # and reading the main flow id are file operations on a
                # possibly shared mount, so the entry check above can be
                # arbitrarily old by now — and ``clear_state`` archives the
                # engine state of whatever flow the file currently holds. A
                # pre-upgrade remote ``luo run`` that re-owned the flow inside
                # that window would find its live state rotated out from under
                # it, which nothing downstream can undo.
                logger.info(
                    "Ownership claim lost before clear_state; leaving %s "
                    "engine state intact",
                    flow_id or "the active flow",
                )
                return
            else:
                pm.clear_state()
                results.append((t("end_session.step.archive_session"), "OK", t("end_session.detail.session_archived")))
        else:
            results.append((t("end_session.step.archive_session"), "SKIP", t("end_session.detail.no_session_to_archive")))
    except Exception as e:  # noqa: BLE001
        disposition_ok = False
        results.append((t("end_session.step.archive_session"), "FAIL", str(e)[:80]))
        logger.warning("Archive main session failed: %s", e)

    # The archive above rewrites/rotates engine state, so a takeover can land
    # between it and here — and what follows deletes the resumable snapshot and
    # the review baselines a re-owned flow depends on.
    if _abandon_if_claim_lost(
        claimed_marker, results, "end_session.step.clear_resumable"
    ):
        return

    try:
        if flow_id:
            # An ended session is terminal — it is exactly the "no longer
            # resumable" signal the review baselines are kept for, so they are
            # reclaimed on the same step rather than lingering for a flow no
            # SELF_CHECK round will ever re-enter. Both paths go through
            # :func:`_clear_resumable` so the baseline reclaim carries the same
            # mid-step ownership re-read here as it does in the worktree path.
            if _clear_resumable(
                project_root,
                flow_id,
                claimed_marker,
                reclaim_baselines=disposition_ok,
            ):
                results.append(
                    (
                        t("end_session.step.clear_resumable"),
                        "OK",
                        flow_id
                        if disposition_ok
                        else t(
                            "end_session.detail.baselines_kept_archive_failed"
                        ),
                    )
                )
            else:
                results.append(
                    (
                        t("end_session.step.clear_resumable"),
                        "FAIL",
                        t("end_session.detail.baselines_kept"),
                    )
                )
        else:
            results.append((t("end_session.step.clear_resumable"), "SKIP", t("end_session.detail.no_flow_id")))
    except Exception as e:  # noqa: BLE001
        results.append((t("end_session.step.clear_resumable"), "FAIL", str(e)[:80]))
        logger.warning("Clear resumable (main) failed: %s", e)


def _infer_worktree_branch(
    project_root: Path, worktree_path: Path
) -> Optional[str]:
    """Infer the branch checked out at *worktree_path* from git's metadata.

    Used when the worktree's ``engine.json`` did not record a
    ``worktree_branch`` (older / corrupt / tolerant state) but the worktree is
    still registered with git. Parses ``git worktree list --porcelain`` and
    matches the requested path (by realpath) to its ``branch refs/heads/<name>``
    line. Returns ``None`` when no registered worktree matches the path or it is
    in a detached-HEAD state.
    """
    from ..engine.worktree import _run_git

    try:
        target = os.path.realpath(str(worktree_path))
    except OSError:  # pragma: no cover - defensive
        target = str(worktree_path)

    try:
        result = _run_git(
            project_root, "worktree", "list", "--porcelain", check=False
        )
    except Exception:  # noqa: BLE001 - never block cleanup on inference
        return None
    if result.returncode != 0:
        return None

    current_path: Optional[str] = None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            current_path = line.split(" ", 1)[1]
        elif line.startswith("branch ") and current_path is not None:
            try:
                cp_real = os.path.realpath(current_path)
            except OSError:  # pragma: no cover - defensive
                cp_real = current_path
            if cp_real == target:
                ref = line.split(" ", 1)[1]
                if ref.startswith("refs/heads/"):
                    return ref[len("refs/heads/"):]
                return ref or None
    return None


def _sync_worktree_history(
    project_root: Path,
    worktree_path: Path,
    flow_id: Optional[str],
    branch: Optional[str],
) -> int:
    """Copy the worktree's per-flow history into the main project (append-only).

    Files that do not yet exist in the main history directory are copied
    verbatim; a collision with an existing file is written as a
    ``<name>.from-<branch>`` sidecar (mirroring the merge-back convention used
    by the history reader) so existing records are never overwritten. When that
    sidecar name is *also* already taken (a prior partial end-session run, a
    merge-back sidecar, or a retry), a unique ``<name>.from-<branch>-N`` name is
    chosen instead of silently dropping the current worktree's file — so the
    latest record file is never lost when the worktree is subsequently deleted.
    A byte-identical existing target is treated as already-synced and skipped
    (keeping the operation idempotent across retries). Returns the number of
    files copied.
    """
    from ..utils import copy_file

    if not flow_id:
        return 0
    src_dir = runtime_dir(worktree_path) / "history" / flow_id
    if not src_dir.is_dir():
        return 0
    dst_dir = runtime_dir(project_root) / "history" / flow_id

    safe_branch = "worktree"
    if branch:
        safe_branch = branch.replace("/", "__")

    copied = 0
    for src in sorted(src_dir.rglob("*")):
        if not src.is_file():
            continue
        rel = src.relative_to(src_dir)
        dst = _resolve_history_target(dst_dir / rel, src, safe_branch)
        if dst is None:
            # An identical copy already exists in the main history dir.
            continue
        copy_file(src, dst)
        copied += 1
    return copied


def _same_file(a: Path, b: Path) -> bool:
    """Return True when *a* and *b* have byte-identical content (fault-tolerant)."""
    import filecmp

    try:
        return filecmp.cmp(str(a), str(b), shallow=False)
    except Exception:  # noqa: BLE001
        return False


def _resolve_history_target(
    dst: Path, src: Path, safe_branch: str
) -> Optional[Path]:
    """Resolve where to copy *src*, never overwriting an existing file.

    Returns the primary ``dst`` when it is free, a collision sidecar
    ``<name>.from-<branch>`` (or a uniquified ``...-N`` variant when that too is
    taken) otherwise, or ``None`` when a byte-identical copy already exists at
    any of those locations (idempotent skip).
    """
    if not dst.exists():
        return dst
    if _same_file(src, dst):
        return None

    base_name = f"{dst.name}.from-{safe_branch}"
    candidate = dst.with_name(base_name)
    n = 2
    while candidate.exists():
        if _same_file(src, candidate):
            return None
        candidate = dst.with_name(f"{base_name}-{n}")
        n += 1
    return candidate


def _clear_resumable(
    root: Path,
    flow_id: Optional[str],
    claimed_marker: Optional[Path] = None,
    *,
    reclaim_baselines: bool = True,
) -> bool:
    """Retire *flow_id*'s resumable state under *root* (worktree or main).

    Also reclaims the flow's review baselines: the session is over, so the
    snapshot store under that root has no reader left. INVARIANT: the two
    retire together, on this one call, and nowhere earlier. The archival path
    keeps baselines out of the archive copy by *excluding* the directory, not
    by deleting it up front — an early delete could not be undone once the
    ownership re-read in front of this call discovers a takeover, and a re-owned
    flow still needs its baselines to rebuild its SELF_CHECK scope.

    INVARIANT: the baseline reclaim is guarded by its OWN ownership re-read,
    taken after the snapshot clear rather than shared with the caller's check.
    The two deletions are separate syscall sequences, and clearing a resumable
    snapshot is not instantaneous — a pre-upgrade ``luo run`` on another host
    can overwrite the claim *during* it. The snapshot clear is by then done and
    survivable (a resumed flow rebuilds it), but dropping the baselines is not:
    the re-owned flow would keep running with no way to reconstruct its
    SELF_CHECK scope. So the baselines are abandoned whenever the claim is gone
    by the time their turn comes.

    INVARIANT: the reclaim also requires the resumable snapshot to be CONFIRMED
    gone. ``clear_resumable_snapshot`` is best-effort — a permission or I/O
    error on ``resumable/<flow_id>.json`` is swallowed so the rest of the
    end-session bookkeeping still runs — and a snapshot that survives leaves
    the flow resumable. Reclaiming its baselines anyway produces the one state
    nothing can repair: a resume that reaches SELF_CHECK with no baseline to
    diff against. Keeping them costs disk space the next terminal landing
    reclaims.

    INVARIANT: *reclaim_baselines* lets a caller retire the snapshot while
    keeping the baselines, and it is the caller's statement that the flow's
    TERMINAL disposition did not land — an archive that failed, leaving a
    non-COMPLETED engine.json the resume picker still reads (the main
    ``load_flow_by_id`` path, or ``find_resumable_worktree_runs`` over a
    preserved worktree). Resumability has two channels and the snapshot is only
    one of them, so "snapshot gone" is not by itself proof that no SELF_CHECK
    round will ever ask for these baselines again.

    Returns ``True`` when the reclaim ran (or there was nothing to do, or the
    caller asked for the snapshot clear alone), and ``False`` when it was
    abandoned — to a takeover that landed mid-step, or to a resumable snapshot
    that could not be retired.
    """
    from ..engine.persistence import PersistenceManager
    from ..engine.review_scope import discard_flow_snapshots

    if not flow_id:
        return True
    if not PersistenceManager(root).clear_resumable_snapshot(flow_id):
        logger.warning(
            "Resumable snapshot for %s survived the clear; keeping its review "
            "baselines so a resume can still reconstruct its scope",
            flow_id,
        )
        return False
    if not _claim_still_held(claimed_marker):
        logger.info(
            "Ownership claim lost mid-clear; keeping review baselines for %s",
            flow_id,
        )
        return False
    if not reclaim_baselines:
        logger.info(
            "Terminal disposition for %s did not land; keeping its review "
            "baselines so a resume can still reconstruct its scope",
            flow_id,
        )
        return True
    discard_flow_snapshots(root, flow_id)
    return True


# --------------------------------------------------------------------------
# Step 5 helpers
# --------------------------------------------------------------------------
def _display_results(results: List[Tuple[str, str, str]]) -> None:
    """Display end-session results as a Rich table (mirrors salvage)."""
    table = Table(title=t("end_session.table.title"))
    table.add_column(t("end_session.table.col_step"), style="cyan")
    table.add_column(t("end_session.table.col_status"), style="bold")
    table.add_column(t("end_session.table.col_detail"))

    status_styles = {
        "OK": t("end_session.status.ok"),
        "SKIP": t("end_session.status.skip"),
        "FAIL": t("end_session.status.fail"),
    }

    for step_name, status, detail in results:
        styled_status = status_styles.get(status, status)
        table.add_row(step_name, styled_status, detail)

    console.print()
    console.print(table)
    console.print()
