"""Tests for the ``se3 end-session`` command (end_session_cmd)."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from se3.commands import end_session_cmd
from se3.commands.end_session_cmd import end_session
from se3.engine.worktree import _branch_safe_name, exists_for_branch


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


def _make_worktree_session(
    main: Path,
    flow_id: str,
    branch: str,
    status: str = "PAUSED",
) -> Path:
    """Create a real worktree session with engine.json + history + snapshot."""
    safe = _branch_safe_name(branch)
    wt_path = main / "se3" / "worktrees" / safe
    wt_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", str(wt_path), "-b", branch],
        check=True, capture_output=True,
    )

    # engine.json inside the worktree
    state_dir = wt_path / "se3" / "state"
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
    hist_dir = wt_path / "se3" / "history" / flow_id
    hist_dir.mkdir(parents=True, exist_ok=True)
    (hist_dir / "01_discovery_ab12.jsonl").write_text(
        json.dumps({"role": "user", "content": "hello"}) + "\n"
    )
    return wt_path


def _make_main_session(main: Path, flow_id: str, status: str = "PAUSED") -> None:
    """Create a main-branch session (engine.json at main/se3/state + snapshot)."""
    state_dir = main / "se3" / "state"
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
    archive_root = main / "se3" / "worktrees" / ".archive"
    assert archive_root.is_dir()
    assert any(archive_root.iterdir())

    # Terminal state promoted into the main archive (force=True, status PAUSED)
    promoted = main / "se3" / "state" / "archive" / f"engine_{flow_id}.json"
    assert promoted.exists()
    promoted_data = json.loads(promoted.read_text())
    assert promoted_data["flow_id"] == flow_id
    # project_root stamped to the main repo
    assert Path(promoted_data["project_root"]).resolve() == main.resolve()

    # History synced into the main project
    main_hist = main / "se3" / "history" / flow_id / "01_discovery_ab12.jsonl"
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
    main_hist_dir = main / "se3" / "history" / flow_id
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

    state_file = main / "se3" / "state" / "engine.json"
    snapshot = main / "se3" / "state" / "resumable" / f"{flow_id}.json"
    assert state_file.exists()
    assert snapshot.exists()

    rc = end_session(project_root=main, flow_id=flow_id)
    assert rc == 0

    # engine.json moved into archive/
    assert not state_file.exists()
    archive_dir = main / "se3" / "state" / "archive"
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
    snapshot = main / "se3" / "state" / "resumable" / f"{flow_id}.json"
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
    assert (main / "se3" / "state" / "archive" / f"engine_{flow_id}.json").exists()


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
        "se3.engine.merge.cleanup._promote_completed_engine_state", _boom
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
