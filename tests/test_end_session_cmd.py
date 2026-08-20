"""Tests for the ``se3 end-session`` command (end_session_cmd)."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from tianluo.commands import end_session_cmd
from tianluo.commands.end_session_cmd import end_session
from tianluo.engine.worktree import _branch_safe_name, exists_for_branch


# --------------------------------------------------------------------------
# git helpers
# --------------------------------------------------------------------------
def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )
    (path / "README.md").write_text("# Test\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"],
        check=True, capture_output=True,
    )


def _branch_exists(path: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--verify", branch],
        capture_output=True, text=True, check=False,
    )
    return result.returncode == 0


def _write_fake_se3(directory: Path, body: str) -> Path:
    """Write an executable ``se3`` shebang script that runs *body*.

    Spawning ``[fake_se3, "run", ...]`` makes psutil observe
    ``[interpreter, /path/se3, run, ...]`` — the real shebang-rewritten
    console-script shape the tightened ``_cmdline_is_se3_run`` matches at
    argv[1]. Inline ``python -c <code> se3 run`` is no longer recognised.
    """
    path = directory / "tianluo"
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)
    return path


def _make_worktree_session(
    main: Path,
    flow_id: str,
    branch: str,
    status: str = "PAUSED",
) -> Path:
    """Create a real worktree session with engine.json + history + snapshot."""
    safe = _branch_safe_name(branch)
    wt_path = main / "tianluo" / "worktrees" / safe
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", str(wt_path), "-b", branch],
        check=True, capture_output=True,
    )

    # engine.json inside the worktree
    state_dir = wt_path / "tianluo" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    engine = {
        "flow_id": flow_id,
        "status": status,
        "task_description": "do a thing",
        "is_worktree_mode": True,
        "worktree_path": str(wt_path),
        "worktree_branch": branch,
        "worktree_original_branch": "master",
        "state": {"current_step_id": "01_discovery"},
    }
    (state_dir / "engine.json").write_text(json.dumps(engine, indent=2))

    # resumable snapshot
    resumable_dir = state_dir / "resumable"
    resumable_dir.mkdir(parents=True, exist_ok=True)
    (resumable_dir / f"{flow_id}.json").write_text(json.dumps(engine, indent=2))

    # history
    hist_dir = wt_path / "tianluo" / "history" / flow_id
    hist_dir.mkdir(parents=True, exist_ok=True)
    (hist_dir / "01_discovery_ab12.jsonl").write_text(
        json.dumps({"role": "user", "content": "hello"}) + "\n"
    )
    return wt_path


def _make_main_session(main: Path, flow_id: str, status: str = "PAUSED") -> None:
    """Create a main-branch session (engine.json at main/tianluo/state + snapshot)."""
    state_dir = main / "tianluo" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    engine = {
        "flow_id": flow_id,
        "status": status,
        "task_description": "main task",
        "state": {"current_step_id": "01_analyze"},
    }
    (state_dir / "engine.json").write_text(json.dumps(engine, indent=2))
    resumable_dir = state_dir / "resumable"
    resumable_dir.mkdir(parents=True, exist_ok=True)
    (resumable_dir / f"{flow_id}.json").write_text(json.dumps(engine, indent=2))


# --------------------------------------------------------------------------
# worktree-session archival
# --------------------------------------------------------------------------
def test_worktree_session_is_archived(tmp_path: Path) -> None:
    main = tmp_path / "repo"
    main.mkdir()
    _init_repo(main)

    flow_id = "wt-flow-123"
    branch = "worktree/do-a-thing-1"
    wt_path = _make_worktree_session(main, flow_id, branch, status="PAUSED")

    assert exists_for_branch(main, branch)

    rc = end_session(project_root=main, flow_id=flow_id, pid=None)
    assert rc == 0

    # Worktree archived under .archive/
    archive_root = main / "tianluo" / "worktrees" / ".archive"
    assert archive_root.is_dir()
    assert any(archive_root.iterdir())

    # Terminal state promoted into the main archive (force=True, status PAUSED)
    promoted = main / "tianluo" / "state" / "archive" / f"engine_{flow_id}.json"
    assert promoted.exists()
    promoted_data = json.loads(promoted.read_text())
    assert promoted_data["flow_id"] == flow_id
    # project_root stamped to the main repo
    assert Path(promoted_data["project_root"]).resolve() == main.resolve()

    # History synced into the main project
    main_hist = main / "tianluo" / "history" / flow_id / "01_discovery_ab12.jsonl"
    assert main_hist.exists()

    # Branch + worktree metadata cleaned up
    assert not _branch_exists(main, branch)
    assert not exists_for_branch(main, branch)

    # Worktree directory removed (and not re-created by the resumable clear)
    assert not wt_path.exists()


def test_worktree_session_history_collision_uses_sidecar(tmp_path: Path) -> None:
    main = tmp_path / "repo"
    main.mkdir()
    _init_repo(main)

    flow_id = "wt-flow-collide"
    branch = "worktree/collide-1"
    _make_worktree_session(main, flow_id, branch)

    # Pre-existing main history file with the SAME name → collision.
    main_hist_dir = main / "tianluo" / "history" / flow_id
    main_hist_dir.mkdir(parents=True, exist_ok=True)
    existing = main_hist_dir / "01_discovery_ab12.jsonl"
    existing.write_text(json.dumps({"role": "assistant", "content": "old"}) + "\n")

    rc = end_session(project_root=main, flow_id=flow_id)
    assert rc == 0

    # Original is untouched; the worktree copy lands as a sidecar.
    assert json.loads(existing.read_text().strip())["content"] == "old"
    sidecars = list(main_hist_dir.glob("*.from-*"))
    assert sidecars, "expected a .from-<branch> sidecar for the colliding file"


# --------------------------------------------------------------------------
# main-branch session archival
# --------------------------------------------------------------------------
def test_main_branch_session_is_archived(tmp_path: Path) -> None:
    main = tmp_path / "repo"
    main.mkdir()
    _init_repo(main)

    flow_id = "main-flow-1"
    _make_main_session(main, flow_id, status="FAILED")

    state_file = main / "tianluo" / "state" / "engine.json"
    snapshot = main / "tianluo" / "state" / "resumable" / f"{flow_id}.json"
    assert state_file.exists()
    assert snapshot.exists()

    rc = end_session(project_root=main, flow_id=flow_id)
    assert rc == 0

    # engine.json moved into archive/
    assert not state_file.exists()
    archive_dir = main / "tianluo" / "state" / "archive"
    assert any(archive_dir.glob("engine_*.json"))

    # resumable snapshot cleared
    assert not snapshot.exists()


def test_flow_id_resolved_from_main_engine(tmp_path: Path) -> None:
    """When flow_id is omitted, it is read from the main engine.json."""
    main = tmp_path / "repo"
    main.mkdir()
    _init_repo(main)

    flow_id = "auto-flow"
    _make_main_session(main, flow_id)

    rc = end_session(project_root=main, flow_id=None)
    assert rc == 0
    snapshot = main / "tianluo" / "state" / "resumable" / f"{flow_id}.json"
    assert not snapshot.exists()


# --------------------------------------------------------------------------
# process termination
# --------------------------------------------------------------------------
def test_terminate_by_pid_sigterm(tmp_path: Path) -> None:
    main = tmp_path / "repo"
    main.mkdir()
    _init_repo(main)
    _make_main_session(main, "p-flow")

    # A long-lived child to terminate by pid.
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        assert end_session_cmd._proc_alive(proc.pid)
        rc = end_session(project_root=main, flow_id="p-flow", pid=proc.pid)
        assert rc == 0
        # Give the kernel a moment to reap.
        for _ in range(50):
            if not end_session_cmd._proc_alive(proc.pid):
                break
            time.sleep(0.1)
        assert not end_session_cmd._proc_alive(proc.pid)
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        proc.wait(timeout=5)


def test_terminate_sigkill_escalation(tmp_path: Path) -> None:
    """A process ignoring SIGTERM is escalated to SIGKILL."""
    main = tmp_path / "repo"
    main.mkdir()
    _init_repo(main)
    _make_main_session(main, "k-flow")

    # Child that ignores SIGTERM.
    code = (
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "time.sleep(60)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code])
    try:
        # Let the child install its handler.
        time.sleep(0.5)
        rc = end_session(
            project_root=main, flow_id="k-flow", pid=proc.pid, grace_seconds=1.0
        )
        assert rc == 0
        for _ in range(50):
            if not end_session_cmd._proc_alive(proc.pid):
                break
            time.sleep(0.1)
        assert not end_session_cmd._proc_alive(proc.pid)
    finally:
        try:
            proc.kill()
        except Exception:
            pass
        proc.wait(timeout=5)


def test_terminate_kills_child_subprocess(tmp_path: Path) -> None:
    """Ending a session must terminate the whole process tree, not just parent.

    A ``se3 run`` parent typically has a live agent (Claude/Codex) child writing
    into the worktree; killing only the parent would orphan that child. Here the
    parent spawns a long-lived child and we verify ``end-session --pid <parent>``
    kills both.
    """
    main = tmp_path / "repo"
    main.mkdir()
    _init_repo(main)
    _make_main_session(main, "tree-flow")

    pidfile = tmp_path / "child.pid"
    code = (
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', "
        "'import time; time.sleep(120)'])\n"
        f"open(r'{pidfile}', 'w').write(str(child.pid))\n"
        "time.sleep(120)\n"
    )
    proc = subprocess.Popen([sys.executable, "-c", code])
    child_pid = None
    try:
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text().strip():
                child_pid = int(pidfile.read_text().strip())
                break
            time.sleep(0.1)
        assert child_pid is not None
        assert end_session_cmd._proc_alive(child_pid)

        rc = end_session(
            project_root=main, flow_id="tree-flow", pid=proc.pid,
            grace_seconds=2.0,
        )
        assert rc == 0

        for _ in range(50):
            if not end_session_cmd._proc_alive(proc.pid) and not (
                end_session_cmd._proc_alive(child_pid)
            ):
                break
            time.sleep(0.1)
        assert not end_session_cmd._proc_alive(proc.pid)
        assert not end_session_cmd._proc_alive(child_pid)
    finally:
        for p in (proc.pid, child_pid):
            if p:
                try:
                    end_session_cmd.os.kill(p, 9)
                except Exception:
                    pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass


def test_worktree_without_branch_is_still_cleaned_up(tmp_path: Path) -> None:
    """A worktree session whose engine.json lacks worktree_branch is still cleaned.

    The branch is inferred from git's worktree metadata so the worktree
    directory + registration are removed rather than left hanging.
    """
    main = tmp_path / "repo"
    main.mkdir()
    _init_repo(main)

    flow_id = "wt-no-branch"
    branch = "worktree/no-branch-1"
    wt_path = _make_worktree_session(main, flow_id, branch)

    # Strip worktree_branch from the worktree engine.json (older/corrupt state).
    engine_file = wt_path / "tianluo" / "state" / "engine.json"
    data = json.loads(engine_file.read_text())
    data.pop("worktree_branch", None)
    engine_file.write_text(json.dumps(data, indent=2))

    assert exists_for_branch(main, branch)

    rc = end_session(project_root=main, flow_id=flow_id)
    assert rc == 0

    # Worktree archived, and the worktree directory + registration cleaned up
    # despite the missing branch in engine.json.
    assert not wt_path.exists()
    assert not exists_for_branch(main, branch)


def test_stale_unregistered_worktree_dir_is_removed(tmp_path: Path) -> None:
    """A stale worktree dir (no git registration, no branch) is fully removed.

    When a worktree session's engine.json carries no ``worktree_branch`` AND git
    no longer tracks the directory as a registered worktree (so no branch can be
    inferred), end-session takes the remove-by-path branch. ``remove_worktree``
    only drives git and would leave the on-disk directory behind; the command
    must still delete the directory rather than reporting success while it
    survives.
    """
    main = tmp_path / "repo"
    main.mkdir()
    _init_repo(main)

    flow_id = "stale-wt"
    # Build a worktree session dir manually — it is NOT a registered git
    # worktree, and its engine.json has no worktree_branch, so the branch can
    # neither be recorded nor inferred.
    wt_path = main / "tianluo" / "worktrees" / "stale"
    state_dir = wt_path / "tianluo" / "state"
    state_dir.mkdir(parents=True)
    engine = {
        "flow_id": flow_id,
        "status": "FAILED",
        "is_worktree_mode": True,
        "worktree_path": str(wt_path),
        # no worktree_branch
    }
    (state_dir / "engine.json").write_text(json.dumps(engine, indent=2))
    assert wt_path.exists()

    rc = end_session(project_root=main, flow_id=flow_id)
    assert rc == 0

    # The stale worktree directory is gone (archived first, then removed).
    assert not wt_path.exists()
    # And it was archived before removal.
    archive_root = main / "tianluo" / "worktrees" / ".archive"
    assert archive_root.exists() and any(archive_root.iterdir())


def test_discovers_live_worktree_parent_by_descendant_cwd(tmp_path: Path) -> None:
    """A live ``--worktree`` flow's parent is found via its agent child's cwd.

    A ``se3 run --worktree`` parent keeps cwd==main_root and writes engine.json
    into the worktree, so the main engine.json's flow_id never confirms it.
    Discovery must instead match the descendant (agent subprocess) running inside
    the worktree path. We spawn a parent (cwd=main, fake ``se3 run`` cmdline)
    that forks a child chdir'd into the worktree, and verify the parent pid is
    discovered.
    """
    main = tmp_path / "repo"
    main.mkdir()
    _init_repo(main)
    flow_id = "live-wt"
    branch = "worktree/live-1"
    wt_path = _make_worktree_session(main, flow_id, branch, status="RUNNING")

    # NOTE: no main engine.json is created, mirroring a real worktree flow whose
    # state lives only inside the worktree.

    pidfile = tmp_path / "child.pid"
    child_code = (
        "import os, time; "
        f"os.chdir(r'{wt_path}'); "
        "time.sleep(120)"
    )
    # The parent stays at cwd=main and spawns the child chdir'd into the
    # worktree. The parent is a faithful console-script stub (a shebang script
    # named ``se3``) so psutil reads ``[interpreter, /path/se3, run]`` and the
    # tightened predicate matches it at argv[1].
    parent_code = (
        "import subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, '-c', {child_code!r}])\n"
        f"open(r'{pidfile}', 'w').write(str(child.pid))\n"
        "time.sleep(120)\n"
    )
    fake_se3 = _write_fake_se3(tmp_path, parent_code)
    proc = subprocess.Popen([str(fake_se3), "run"], cwd=str(main))
    child_pid = None
    try:
        for _ in range(100):
            if pidfile.exists() and pidfile.read_text().strip():
                child_pid = int(pidfile.read_text().strip())
                break
            time.sleep(0.1)
        assert child_pid is not None
        # Give the child a moment to finish chdir into the worktree.
        time.sleep(0.3)

        pids = end_session_cmd._discover_pids_for_flow(flow_id, main, wt_path)
        assert proc.pid in pids
    finally:
        for p in (proc.pid, child_pid):
            if p:
                try:
                    end_session_cmd.os.kill(p, 9)
                except Exception:
                    pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass


def test_discovers_live_worktree_parent_via_pidfile(tmp_path: Path) -> None:
    """A live ``--worktree`` parent with NO descendant in the worktree is found.

    This is the self-check scenario: the parent keeps cwd==main_root, the main
    engine.json does not carry the flow_id (the flow's state is in the worktree),
    and the parent is momentarily between agent/test subprocesses so it has no
    descendant whose cwd is inside the worktree. The cwd/descendant heuristics
    cannot find it; the ``se3 run`` process records its pid in the worktree's
    ``run.pid`` marker, and discovery MUST locate it from there so the worktree
    is never archived/deleted under a still-live process.
    """
    main = tmp_path / "repo"
    main.mkdir()
    _init_repo(main)
    flow_id = "live-wt-pidfile"
    branch = "worktree/live-pidfile-1"
    wt_path = _make_worktree_session(main, flow_id, branch, status="RUNNING")

    # A parent that has NO children and stays at cwd=main. It is a faithful
    # console-script stub (shebang script named ``se3``) so psutil reads
    # ``[interpreter, /path/se3, run, --worktree]`` and the tightened predicate
    # matches it at argv[1] (passing the run.pid liveness/cmdline guard).
    fake_se3 = _write_fake_se3(tmp_path, "import time\ntime.sleep(120)\n")
    proc = subprocess.Popen(
        [str(fake_se3), "run", "--worktree"],
        cwd=str(main),
    )
    try:
        # ``se3 run`` writes its pid into the worktree's state dir; simulate it.
        (wt_path / "tianluo" / "state" / "run.pid").write_text(str(proc.pid))

        pids = end_session_cmd._discover_pids_for_flow(flow_id, main, wt_path)
        assert proc.pid in pids
    finally:
        try:
            end_session_cmd.os.kill(proc.pid, 9)
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass


def test_stale_pidfile_is_ignored(tmp_path: Path) -> None:
    """A run.pid pointing at a dead pid must not be reported as a live process."""
    main = tmp_path / "repo"
    main.mkdir()
    _init_repo(main)
    flow_id = "stale-pid"
    branch = "worktree/stale-1"
    wt_path = _make_worktree_session(main, flow_id, branch)

    # Spawn and reap a short-lived process to get a definitely-dead pid.
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (wt_path / "tianluo" / "state" / "run.pid").write_text(str(dead.pid))

    pids = end_session_cmd._discover_pids_for_flow(flow_id, main, wt_path)
    assert dead.pid not in pids


def test_no_live_process_still_archives(tmp_path: Path) -> None:
    """A PAUSED worktree with no live process is still archived cleanly."""
    main = tmp_path / "repo"
    main.mkdir()
    _init_repo(main)
    flow_id = "dead-flow"
    branch = "worktree/dead-1"
    _make_worktree_session(main, flow_id, branch)

    rc = end_session(project_root=main, flow_id=flow_id, pid=None)
    assert rc == 0
    assert not exists_for_branch(main, branch)
    assert (main / "tianluo" / "state" / "archive" / f"engine_{flow_id}.json").exists()


def test_unkillable_process_skips_destructive_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A target process that cannot be terminated must NOT trigger archival.

    If ``--pid`` points to a process that stays alive after SIGKILL (e.g. a
    permission failure), the worktree/branch must be preserved and the command
    must exit non-zero rather than archiving a stale snapshot of a live flow.
    """
    main = tmp_path / "repo"
    main.mkdir()
    _init_repo(main)
    flow_id = "wt-alive"
    branch = "worktree/alive-1"
    wt_path = _make_worktree_session(main, flow_id, branch)

    # Simulate a process that refuses to die: signals "succeed" but the process
    # is reported alive throughout.
    monkeypatch.setattr(end_session_cmd, "_proc_alive", lambda pid: True)
    monkeypatch.setattr(end_session_cmd.os, "kill", lambda *a, **k: None)

    rc = end_session(
        project_root=main, flow_id=flow_id, pid=4242, grace_seconds=0.2
    )
    assert rc == 1  # termination not confirmed → nonzero exit

    # Destructive archive must have been skipped: worktree + branch survive.
    assert wt_path.exists()
    assert _branch_exists(main, branch)
    assert exists_for_branch(main, branch)
    assert not (
        main / "tianluo" / "state" / "archive" / f"engine_{flow_id}.json"
    ).exists()


# --------------------------------------------------------------------------
# degradation + fault tolerance
# --------------------------------------------------------------------------
def test_missing_worktree_degrades_to_main_archive(tmp_path: Path) -> None:
    """No worktree found → main-branch archival path, no error."""
    main = tmp_path / "repo"
    main.mkdir()
    _init_repo(main)
    _make_main_session(main, "lonely-flow")

    rc = end_session(project_root=main, flow_id="lonely-flow")
    assert rc == 0


def test_mismatched_main_flow_is_not_archived(tmp_path: Path) -> None:
    """Ending flow A (worktree gone) must NOT archive an unrelated active flow B.

    The main project's engine.json belongs to a *different* flow; clearing it
    would end the wrong session.
    """
    main = tmp_path / "repo"
    main.mkdir()
    _init_repo(main)
    # The main project is busy with flow B.
    _make_main_session(main, "flow-B", status="RUNNING")
    state_file = main / "tianluo" / "state" / "engine.json"
    b_snapshot = main / "tianluo" / "state" / "resumable" / "flow-B.json"
    assert state_file.exists()

    # Ending flow A, whose worktree is already gone (no worktree on disk).
    rc = end_session(project_root=main, flow_id="flow-A")
    assert rc == 0

    # Flow B's engine.json + snapshot are left intact — not archived.
    assert state_file.exists()
    assert json.loads(state_file.read_text())["flow_id"] == "flow-B"
    assert b_snapshot.exists()
    # And no archived engine_flow-B.json was produced.
    archive_dir = main / "tianluo" / "state" / "archive"
    assert not list(archive_dir.glob("engine_*.json")) if archive_dir.exists() else True


def test_unreadable_main_flow_is_not_archived(tmp_path: Path) -> None:
    """Ending flow A when the main engine.json has no readable flow_id.

    A different flow B may be mid-write / INIT / corrupt so its engine.json
    carries no usable flow_id. Without a positive match to the requested
    flow_id, the destructive clear must be skipped — otherwise we archive the
    wrong, unrelated session.
    """
    main = tmp_path / "repo"
    main.mkdir()
    _init_repo(main)

    state_dir = main / "tianluo" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_file = state_dir / "engine.json"
    # engine.json belongs to an active flow B but its flow_id is absent/corrupt.
    state_file.write_text(json.dumps({"status": "INIT", "state": {}}))

    rc = end_session(project_root=main, flow_id="flow-A")
    assert rc == 0

    # The unconfirmed main session is left intact — not archived/cleared.
    assert state_file.exists()
    archive_dir = state_dir / "archive"
    assert not list(archive_dir.glob("engine_*.json")) if archive_dir.exists() else True


def test_archive_failure_preserves_worktree_and_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed worktree archive must NOT destroy the only copy of the work."""
    main = tmp_path / "repo"
    main.mkdir()
    _init_repo(main)
    flow_id = "wt-archive-fail"
    branch = "worktree/archive-fail-1"
    wt_path = _make_worktree_session(main, flow_id, branch)

    # Force the archive-copy step to fail (e.g. disk full / permission denied).
    def _boom(*args, **kwargs):
        raise OSError("No space left on device")

    monkeypatch.setattr(
        "tianluo.engine.merge.cleanup._archive_worktree", _boom
    )

    rc = end_session(project_root=main, flow_id=flow_id)
    assert rc == 1  # archive failed → nonzero exit

    # The destructive cleanup must have been skipped: branch + worktree survive.
    assert _branch_exists(main, branch)
    assert exists_for_branch(main, branch)
    assert wt_path.exists()


def test_step_failure_does_not_abort_and_exit_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A sub-step raising is recorded but does not abort; exit code is nonzero."""
    main = tmp_path / "repo"
    main.mkdir()
    _init_repo(main)
    flow_id = "fail-flow"
    branch = "worktree/fail-1"
    _make_worktree_session(main, flow_id, branch)

    # Force the promote step to blow up.
    def _boom(*args, **kwargs):
        raise RuntimeError("promotion exploded")

    monkeypatch.setattr(
        "tianluo.engine.merge.cleanup._promote_completed_engine_state", _boom
    )

    rc = end_session(project_root=main, flow_id=flow_id)
    # Non-zero because a step failed...
    assert rc == 1
    # ...but later steps still ran: branch was still cleaned up.
    assert not exists_for_branch(main, branch)


def test_unresolvable_root_returns_one(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        end_session_cmd, "_find_project_root", lambda: None
    )
    rc = end_session(project_root=None, flow_id="x")
    assert rc == 1


# --------------------------------------------------------------------------
# ``_pid_is_live_se3_run``: an unreadable cmdline is INCONCLUSIVE
# --------------------------------------------------------------------------
class _FakePsutil:
    """Stand-in for the ``psutil`` module whose ``Process`` raises on cmdline().

    Only the surface ``_pid_is_live_se3_run`` touches is provided; the real
    exception classes are reused so the production ``except`` clauses match.
    """

    NoSuchProcess = psutil.NoSuchProcess
    AccessDenied = psutil.AccessDenied
    STATUS_ZOMBIE = psutil.STATUS_ZOMBIE

    def __init__(self, raiser):
        self._raiser = raiser

    def Process(self, pid=None):  # noqa: N802 - mirrors the psutil API
        outer = self

        class _P:
            def cmdline(self):
                raise outer._raiser()

        return _P()


@pytest.mark.parametrize(
    "raiser",
    [
        lambda: psutil.AccessDenied(1234),
        lambda: OSError("cmdline unreadable"),
    ],
    ids=["access-denied", "other-error"],
)
def test_unreadable_cmdline_keeps_live_verdict(
    monkeypatch: pytest.MonkeyPatch, raiser
) -> None:
    """A confirmed-live pid whose cmdline cannot be read stays "live".

    Regression guard: returning False here made end-session report *no live
    process* for a run it had already proven alive, so the destructive
    snapshot/worktree cleanup could run underneath a still-executing flow.
    """
    monkeypatch.setattr(end_session_cmd, "_proc_alive", lambda pid: True)
    monkeypatch.setattr(end_session_cmd, "psutil", _FakePsutil(raiser))

    assert end_session_cmd._pid_is_live_se3_run(1234) is True


def test_vanished_pid_is_not_live(monkeypatch: pytest.MonkeyPatch) -> None:
    """A process that disappears during inspection is genuinely stale."""
    monkeypatch.setattr(end_session_cmd, "_proc_alive", lambda pid: True)
    monkeypatch.setattr(
        end_session_cmd,
        "psutil",
        _FakePsutil(lambda: psutil.NoSuchProcess(1234)),
    )

    assert end_session_cmd._pid_is_live_se3_run(1234) is False


def test_unreadable_cmdline_marker_is_not_cleared(tmp_path: Path, monkeypatch) -> None:
    """The abandoned-marker reclaim must not unlink a live-but-opaque run."""
    from tianluo.core.machine_id import stable_machine_id
    from tianluo.core.run_pidfile import encode_run_pidfile

    state = tmp_path / "tianluo" / "state"
    state.mkdir(parents=True, exist_ok=True)
    marker = state / "run.pid"
    marker.write_text(encode_run_pidfile(4242, stable_machine_id()), encoding="utf-8")

    monkeypatch.setattr(end_session_cmd, "_proc_alive", lambda pid: True)
    monkeypatch.setattr(
        end_session_cmd,
        "psutil",
        _FakePsutil(lambda: psutil.AccessDenied(4242)),
    )

    assert end_session_cmd._clear_stale_local_run_pidfile(tmp_path) is False
    assert marker.exists()
