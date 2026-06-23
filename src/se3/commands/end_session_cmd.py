"""SE3 End-Session command — terminate and archive a running session.

Ends an se3 run flow: terminates the live ``se3 run`` process (if any) and
archives the session. For a ``--worktree`` session this reuses the existing
``se3 merge --delete-merged`` archival machinery (archive the worktree, promote
the terminal engine state into the main project's archive, sync history, delete
the isolation branch + worktree, clear the resumable snapshot) so the session
ends up archived exactly like a normally completed run — but WITHOUT merging the
(possibly unfinished / conflicting) work into the main branch. For a main-branch
session it simply archives the engine state and clears the resumable snapshot.

The primary motivation is the hanging worktree problem: a main-branch session
can simply be abandoned, but a ``--worktree`` session leaves a never-cleaned
worktree on disk; ``se3 end-session`` gives the operator (and the daemon, on
behalf of the web console) a reliable way to clean it up.

Mirroring ``se3 salvage``, the work is a fixed sequence of independently
fault-tolerant steps: a failure in one step is recorded but does not abort the
others, results are rendered as a Rich summary table, and the exit code is
non-zero when any step failed.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich.console import Console
from rich.table import Table

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
        pid: Optional hint for the live ``se3 run`` process to terminate. When
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
            console.print(
                "[red]Could not find project root "
                "(no .git, se3.yaml, se3.local.yaml, or se3.config.yaml found)[/red]"
            )
            return 1
        results.append(("Resolve root", "OK", str(project_root)))
    except Exception as e:  # noqa: BLE001
        results.append(("Resolve root", "FAIL", str(e)[:80]))
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
                    "Find worktree",
                    "OK",
                    f"branch={wt_record.get('worktree_branch')}",
                )
            )
        else:
            results.append(("Find worktree", "SKIP", "Main-branch session"))
    except Exception as e:  # noqa: BLE001
        results.append(("Find worktree", "FAIL", str(e)[:80]))
        logger.warning("Step 2 (find worktree) failed: %s", e)

    # -- Step 3: terminate the live se3 run process -----------------------
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
            ("Terminate process", "OK" if terminate_ok else "FAIL", detail)
        )
        if not terminate_ok:
            logger.warning("Step 3 (terminate process) did not confirm exit: %s", detail)
    except Exception as e:  # noqa: BLE001
        terminate_ok = False
        results.append(("Terminate process", "FAIL", str(e)[:80]))
        logger.warning("Step 3 (terminate process) failed: %s", e)

    # -- Step 4: archive ---------------------------------------------------
    # Refuse to archive/clean while the session process is still alive: the
    # on-disk worktree + engine snapshot are still being written to, so a
    # destructive cleanup here could remove or archive a stale snapshot.
    if not terminate_ok:
        results.append(
            (
                "Archive session",
                "SKIP",
                "Process still alive — archive skipped to avoid losing live work",
            )
        )
    elif wt_record is not None and archive_worktree:
        _archive_worktree_session(project_root, flow_id, wt_record, results)
    elif wt_record is not None and not archive_worktree:
        results.append(
            ("Archive worktree", "SKIP", "--no-archive-worktree given")
        )
    else:
        _archive_main_session(project_root, flow_id, results)

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

    A worktree isolation directory (``<main>/se3/worktrees/<name>``) is
    normalized back to its owning ``<main>`` so the archival writes land in the
    main project, not inside the worktree we are about to delete.
    """
    if project_root is None:
        project_root = _find_project_root()
        if project_root is None:
            return None
    project_root = Path(project_root)
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
    engine_json = project_root / "se3" / "state" / "engine.json"
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
    """Locate the worktree session for *flow_id* under ``se3/worktrees/``.

    Scans ``<main>/se3/worktrees/*/se3/state/engine.json`` for an entry whose
    ``flow_id`` matches. Unlike :func:`run.find_resumable_worktree_runs` this is
    NOT filtered by status — a terminated flow may be in any non-COMPLETED state
    (PAUSED / FAILED / RUNNING) or even COMPLETED-but-unmerged, and we still
    want to clean it up. Returns ``None`` when no matching worktree is found
    (a main-branch session, or an already-cleaned worktree).
    """
    worktrees_dir = project_root / "se3" / "worktrees"
    if not worktrees_dir.is_dir():
        return None

    for engine_file in sorted(worktrees_dir.glob("*/se3/state/engine.json")):
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


def _terminate_one(pid: int, grace_seconds: float) -> Tuple[bool, str]:
    """Terminate a single pid (SIGTERM → grace poll → SIGKILL).

    Returns ``(ok, detail)`` where ``ok`` is ``True`` only when the process is
    confirmed dead (or was never running). A process that survives SIGKILL, or a
    signal that fails outright (e.g. ``PermissionError``), returns ``ok=False``
    so the caller can refuse the destructive archive path while the session
    process is still alive.
    """
    if not _proc_alive(pid):
        return True, f"pid {pid} not running"
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True, f"pid {pid} already gone"
    except OSError as exc:
        return False, f"pid {pid} SIGTERM failed: {exc}"

    deadline = time.monotonic() + max(0.0, grace_seconds)
    while time.monotonic() < deadline:
        if not _proc_alive(pid):
            return True, f"pid {pid} terminated (SIGTERM)"
        time.sleep(0.1)

    # Escalate to SIGKILL.
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True, f"pid {pid} terminated (SIGTERM, late)"
    except OSError as exc:
        return False, f"pid {pid} SIGKILL failed: {exc}"

    # Brief wait for the kernel to reap.
    kill_deadline = time.monotonic() + 2.0
    while time.monotonic() < kill_deadline:
        if not _proc_alive(pid):
            return True, f"pid {pid} killed (SIGKILL)"
        time.sleep(0.1)
    return False, f"pid {pid} still alive after SIGKILL"


def _discover_pids_for_flow(
    flow_id: Optional[str],
    main_root: Path,
    worktree_path: Optional[Path],
) -> List[int]:
    """Discover live ``se3 run`` pids that belong to *flow_id*.

    A match is made when an ``se3 run`` process's ``cwd`` is the worktree path
    (worktree session) or the main root (main-branch session) and its
    ``engine.json`` carries the requested ``flow_id``, OR when its command line
    explicitly carries ``--flow-id <flow_id>`` (the resume case).
    """
    if psutil is None:
        return []

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

    pids: List[int] = []
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
                # ``se3 run`` may also run there — so when a specific flow_id is
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
                        continue
                pids.append(pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:  # noqa: BLE001 - defensive, never block on scan
        logger.debug("Process scan for flow %s failed", flow_id, exc_info=True)
    return pids


def _terminate_session_process(
    flow_id: Optional[str],
    pid: Optional[int],
    main_root: Path,
    worktree_path: Optional[Path],
    grace_seconds: float,
) -> Tuple[bool, str]:
    """Terminate the live ``se3 run`` process(es) for the session.

    Uses *pid* when supplied; otherwise discovers candidates by *flow_id*.
    Returns ``(ok, summary)``. ``ok`` is ``True`` when every targeted process is
    confirmed dead (or there was no live process — a PAUSED/FAILED worktree is
    fine), and ``False`` when any process could not be terminated (e.g. a
    permission failure or a process that survives SIGKILL), so the caller can
    refuse the destructive archive path while the session is still executing.
    """
    pids: List[int] = []
    if pid is not None and pid > 0:
        pids = [pid]
    else:
        pids = _discover_pids_for_flow(flow_id, main_root, worktree_path)

    if not pids:
        return True, "no live process found"

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
) -> None:
    """Archive a worktree session, reusing the merge-cleanup machinery.

    Each sub-step is independently fault-tolerant; failures are recorded as a
    FAIL row but never abort the remaining sub-steps.
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

    # 4.1 — copy the worktree directory into se3/worktrees/.archive/.
    if worktree_path.exists():
        try:
            archive_path = _archive_worktree(
                project_root, branch or (flow_id or "worktree"), worktree_path
            )
            results.append(("Archive worktree", "OK", str(archive_path)))
            archive_ok = True
        except Exception as e:  # noqa: BLE001
            results.append(("Archive worktree", "FAIL", str(e)[:80]))
            logger.warning("Archive worktree failed: %s", e)
    else:
        results.append(
            ("Archive worktree", "SKIP", "Worktree dir already gone")
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
            results.append(("Promote state", "OK", str(promoted)))
        else:
            results.append(("Promote state", "SKIP", "No engine.json to promote"))
    except Exception as e:  # noqa: BLE001
        results.append(("Promote state", "FAIL", str(e)[:80]))
        logger.warning("Promote state failed: %s", e)

    # 4.3 — sync the worktree's history into the main project's history.
    try:
        synced = _sync_worktree_history(
            project_root, worktree_path, flow_id, branch
        )
        results.append(("Sync history", "OK", f"{synced} file(s)"))
    except Exception as e:  # noqa: BLE001
        results.append(("Sync history", "FAIL", str(e)[:80]))
        logger.warning("Sync history failed: %s", e)

    # 4.4 — clear the worktree's resumable snapshot. Done BEFORE the worktree
    # is removed below: PersistenceManager re-creates ``se3/state/`` on
    # construction, so clearing after deletion would leave a stray empty dir
    # behind in the just-removed worktree path.
    try:
        _clear_resumable(worktree_path, flow_id)
        results.append(("Clear resumable", "OK", flow_id or "(unknown)"))
    except Exception as e:  # noqa: BLE001
        results.append(("Clear resumable", "FAIL", str(e)[:80]))
        logger.warning("Clear resumable failed: %s", e)

    # 4.5 — delete the isolation branch and force-clean the worktree.
    # Skipped when the archive could not be created (4.1 FAIL): destroying the
    # branch + worktree then would lose the only copy of the unfinished work.
    if not archive_ok:
        results.append(
            (
                "Cleanup branch",
                "SKIP",
                "Archive not created — worktree/branch preserved",
            )
        )
    elif branch:
        try:
            wt_mod.delete_branch(project_root, branch)
            wt_mod.force_cleanup_worktree(project_root, branch)
            results.append(("Cleanup branch", "OK", branch))
        except Exception as e:  # noqa: BLE001
            results.append(("Cleanup branch", "FAIL", str(e)[:80]))
            logger.warning("Cleanup branch failed: %s", e)
    else:
        results.append(("Cleanup branch", "SKIP", "No branch recorded"))


def _archive_main_session(
    project_root: Path,
    flow_id: Optional[str],
    results: List[Tuple[str, str, str]],
) -> None:
    """Archive a main-branch session: clear_state + clear resumable snapshot.

    When a specific *flow_id* was requested we MUST only archive the main
    engine state if it actually belongs to that flow. Otherwise — e.g. ending
    flow A whose worktree is already gone while the main project is busy with a
    different active flow B — we would archive (delete) the unrelated flow B's
    session, ending the wrong session. In that case we skip the destructive
    clear and only proceed to the (flow-scoped, harmless) resumable cleanup.
    """
    from ..engine.persistence import PersistenceManager

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
                    detail = (
                        f"Main engine.json is flow {main_flow_id}, "
                        f"not {flow_id}"
                    )
                else:
                    detail = (
                        "Main engine.json flow_id absent/unreadable; "
                        f"cannot confirm it is {flow_id}"
                    )
                results.append(("Archive session", "SKIP", detail))
            else:
                pm.clear_state()
                results.append(("Archive session", "OK", "Session archived"))
        else:
            results.append(("Archive session", "SKIP", "No session to archive"))
    except Exception as e:  # noqa: BLE001
        results.append(("Archive session", "FAIL", str(e)[:80]))
        logger.warning("Archive main session failed: %s", e)

    try:
        if flow_id:
            PersistenceManager(project_root).clear_resumable_snapshot(flow_id)
            results.append(("Clear resumable", "OK", flow_id))
        else:
            results.append(("Clear resumable", "SKIP", "No flow_id"))
    except Exception as e:  # noqa: BLE001
        results.append(("Clear resumable", "FAIL", str(e)[:80]))
        logger.warning("Clear resumable (main) failed: %s", e)


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
    src_dir = worktree_path / "se3" / "history" / flow_id
    if not src_dir.is_dir():
        return 0
    dst_dir = project_root / "se3" / "history" / flow_id

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


def _clear_resumable(worktree_path: Path, flow_id: Optional[str]) -> None:
    """Clear the resumable snapshot for *flow_id* in the worktree."""
    from ..engine.persistence import PersistenceManager

    if not flow_id:
        return
    PersistenceManager(worktree_path).clear_resumable_snapshot(flow_id)


# --------------------------------------------------------------------------
# Step 5 helpers
# --------------------------------------------------------------------------
def _display_results(results: List[Tuple[str, str, str]]) -> None:
    """Display end-session results as a Rich table (mirrors salvage)."""
    table = Table(title="End Session Results")
    table.add_column("Step", style="cyan")
    table.add_column("Status", style="bold")
    table.add_column("Detail")

    status_styles = {
        "OK": "[green]OK[/green]",
        "SKIP": "[yellow]SKIP[/yellow]",
        "FAIL": "[red]FAIL[/red]",
    }

    for step_name, status, detail in results:
        styled_status = status_styles.get(status, status)
        table.add_row(step_name, styled_status, detail)

    console.print()
    console.print(table)
    console.print()
