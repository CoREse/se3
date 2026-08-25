"""``run.pid`` machine-awareness in ``luo end-session``.

Two shared-filesystem hazards are covered:

* A ``run.pid`` recorded on ANOTHER machine must never be acted on locally —
  its PID number means nothing in this host's process table, so probing it can
  match (and then SIGTERM) an unrelated local ``luo run`` that happens to hold
  the same number.
* A ``run.pid`` recorded on ANOTHER machine must also block the destructive
  half of end-session: this host cannot observe that process table, so "no
  local process" is not proof the flow stopped, and archiving would delete a
  live flow's worktree and ``review-scopes/<flow_id>`` underneath it.
* A marker abandoned by a run that died without its ``finally`` (SIGKILL, OOM,
  reboot) must be reclaimable: the cross-machine resume refusal tells the
  operator to run ``luo end-session`` on the owning machine, so that command
  has to actually clear its own dead marker — otherwise the flow stays
  un-resumable from every other machine forever.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tianluo.commands import end_session_cmd
from tianluo.core.machine_id import stable_machine_id
from tianluo.core.run_pidfile import encode_run_pidfile, foreign_run_holder

FOREIGN_MACHINE = "node-elsewhere-deadbeef"
FOREIGN_PID = 4242


def _state_dir(root: Path) -> Path:
    return root / "tianluo" / "state"


def _live_local_pid() -> int:
    """A pid that is alive on THIS machine but is NOT this process.

    The in-process test harness shares a pid with both sides of the protocol,
    which the codec (correctly) reads as "this marker is already ours". A live
    *other* local pid is what a real concurrent ``luo run`` / ``luo end-session``
    looks like from the far side.
    """
    return os.getppid()


def _write_marker(root: Path, text: str) -> Path:
    state = _state_dir(root)
    state.mkdir(parents=True, exist_ok=True)
    marker = state / "run.pid"
    marker.write_text(text, encoding="utf-8")
    return marker


# --------------------------------------------------------------------------
# A foreign marker's PID is never probed / signalled locally
# --------------------------------------------------------------------------

class TestForeignMarkerIsIgnoredLocally:
    def test_read_run_pidfile_returns_none_for_foreign_record(
        self, tmp_path: Path
    ) -> None:
        _write_marker(tmp_path, encode_run_pidfile(FOREIGN_PID, FOREIGN_MACHINE))
        assert end_session_cmd._read_run_pidfile(tmp_path) is None

    def test_foreign_pid_never_reaches_the_liveness_probe(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An unrelated LOCAL ``luo run`` holding the same PID number would make
        # the probe answer True; the machine gate must keep us from asking.
        probed: list[int] = []

        def _probe(pid: int) -> bool:
            probed.append(pid)
            return True

        monkeypatch.setattr(end_session_cmd, "_pid_is_live_se3_run", _probe)
        monkeypatch.setattr(end_session_cmd, "psutil", None)

        _write_marker(tmp_path, encode_run_pidfile(FOREIGN_PID, FOREIGN_MACHINE))
        pids = end_session_cmd._discover_pids_for_flow(
            flow_id="flow-x", main_root=tmp_path, worktree_path=None
        )
        assert pids == []
        assert FOREIGN_PID not in probed

    def test_local_marker_is_still_discovered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            end_session_cmd, "_pid_is_live_se3_run", lambda pid: True
        )
        monkeypatch.setattr(end_session_cmd, "psutil", None)

        _write_marker(tmp_path, encode_run_pidfile(FOREIGN_PID, stable_machine_id()))
        pids = end_session_cmd._discover_pids_for_flow(
            flow_id="flow-x", main_root=tmp_path, worktree_path=None
        )
        assert pids == [FOREIGN_PID]

    def test_legacy_single_line_marker_is_still_discovered(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            end_session_cmd, "_pid_is_live_se3_run", lambda pid: True
        )
        monkeypatch.setattr(end_session_cmd, "psutil", None)

        _write_marker(tmp_path, f"{FOREIGN_PID}\n")
        pids = end_session_cmd._discover_pids_for_flow(
            flow_id="flow-x", main_root=tmp_path, worktree_path=None
        )
        assert pids == [FOREIGN_PID]


# --------------------------------------------------------------------------
# An abandoned LOCAL marker is reclaimable on its owning machine
# --------------------------------------------------------------------------

class TestAbandonedMarkerRecovery:
    def test_dead_local_marker_is_cleared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            end_session_cmd, "_pid_is_live_se3_run", lambda pid: False
        )
        marker = _write_marker(
            tmp_path, encode_run_pidfile(FOREIGN_PID, stable_machine_id())
        )
        assert end_session_cmd._clear_stale_local_run_pidfile(tmp_path) is True
        assert not marker.exists()

    def test_dead_legacy_marker_is_cleared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            end_session_cmd, "_pid_is_live_se3_run", lambda pid: False
        )
        marker = _write_marker(tmp_path, f"{FOREIGN_PID}\n")
        assert end_session_cmd._clear_stale_local_run_pidfile(tmp_path) is True
        assert not marker.exists()

    def test_live_local_marker_is_kept(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            end_session_cmd, "_pid_is_live_se3_run", lambda pid: True
        )
        marker = _write_marker(
            tmp_path, encode_run_pidfile(FOREIGN_PID, stable_machine_id())
        )
        assert end_session_cmd._clear_stale_local_run_pidfile(tmp_path) is False
        assert marker.exists()

    def test_foreign_marker_is_never_cleared_from_here(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Only the owning host can judge its PID dead; clearing it from another
        # machine would re-open the double-writer hole the guard closes.
        monkeypatch.setattr(
            end_session_cmd, "_pid_is_live_se3_run", lambda pid: False
        )
        marker = _write_marker(
            tmp_path, encode_run_pidfile(FOREIGN_PID, FOREIGN_MACHINE)
        )
        assert end_session_cmd._clear_stale_local_run_pidfile(tmp_path) is False
        assert marker.exists()

    def test_end_session_clears_marker_and_unblocks_resume(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The documented recovery works end-to-end on the owning machine.

        Before: another machine sees the marker as a foreign holder and refuses
        to resume. After ``luo end-session`` runs on the owning machine, the
        abandoned marker is gone and the refusal lifts.
        """
        (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
        marker = _write_marker(
            tmp_path, encode_run_pidfile(FOREIGN_PID, stable_machine_id())
        )
        owner = stable_machine_id()
        # From any other machine this marker reads as "held on <owner>".
        with monkeypatch.context() as elsewhere:
            elsewhere.setattr(
                "tianluo.core.machine_id.stable_machine_id",
                lambda: "some-other-host",
            )
            holder = foreign_run_holder(_state_dir(tmp_path))
            assert holder is not None and holder.machine_id == owner

        monkeypatch.setattr(
            end_session_cmd, "_pid_is_live_se3_run", lambda pid: False
        )
        monkeypatch.setattr(
            end_session_cmd,
            "_terminate_session_process",
            lambda **kwargs: (True, "no process"),
        )
        monkeypatch.setattr(
            end_session_cmd, "_archive_main_session", lambda *a, **k: None
        )

        exit_code = end_session_cmd.end_session(
            project_root=tmp_path, flow_id="flow-x"
        )
        assert exit_code == 0
        assert not marker.exists()
        # With the marker gone, no machine sees a foreign holder any more.
        assert foreign_run_holder(_state_dir(tmp_path)) is None


def test_own_pid_marker_is_treated_as_stale(tmp_path: Path) -> None:
    """A marker naming this (non-``luo run``) process is a recycled PID."""
    marker = _write_marker(
        tmp_path, encode_run_pidfile(os.getpid(), stable_machine_id())
    )
    assert end_session_cmd._clear_stale_local_run_pidfile(tmp_path) is True
    assert not marker.exists()


# --------------------------------------------------------------------------
# A foreign marker makes termination inconclusive (never "nothing running")
# --------------------------------------------------------------------------

class TestForeignMarkerBlocksCleanup:
    def test_terminate_is_inconclusive_on_foreign_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(end_session_cmd, "psutil", None)
        _write_marker(
            tmp_path,
            encode_run_pidfile(FOREIGN_PID, FOREIGN_MACHINE, "flow-x"),
        )
        ok, detail = end_session_cmd._terminate_session_process(
            flow_id="flow-x",
            pid=None,
            main_root=tmp_path,
            worktree_path=None,
            grace_seconds=0.0,
        )
        assert ok is False
        assert FOREIGN_MACHINE in detail

    def test_operator_supplied_pid_does_not_override_a_foreign_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A local --pid hint is no evidence about the remote process, and must
        # not let the destructive path proceed (nor signal an unrelated pid).
        signalled: list[int] = []
        monkeypatch.setattr(
            end_session_cmd,
            "_terminate_one",
            lambda p, g: (signalled.append(p), (True, "killed"))[1],
        )
        _write_marker(
            tmp_path,
            encode_run_pidfile(FOREIGN_PID, FOREIGN_MACHINE, "flow-x"),
        )
        ok, _ = end_session_cmd._terminate_session_process(
            flow_id="flow-x",
            pid=os.getpid(),
            main_root=tmp_path,
            worktree_path=None,
            grace_seconds=0.0,
        )
        assert ok is False
        assert signalled == []

    def test_unstamped_foreign_marker_also_blocks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A run that had not yet minted its flow id cannot be shown to be a
        # different flow; refusing is the recoverable direction.
        monkeypatch.setattr(end_session_cmd, "psutil", None)
        _write_marker(tmp_path, encode_run_pidfile(FOREIGN_PID, FOREIGN_MACHINE))
        ok, _ = end_session_cmd._terminate_session_process(
            flow_id="flow-x",
            pid=None,
            main_root=tmp_path,
            worktree_path=None,
            grace_seconds=0.0,
        )
        assert ok is False

    def test_foreign_marker_of_a_different_flow_does_not_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(end_session_cmd, "psutil", None)
        _write_marker(
            tmp_path,
            encode_run_pidfile(FOREIGN_PID, FOREIGN_MACHINE, "flow-other"),
        )
        ok, _ = end_session_cmd._terminate_session_process(
            flow_id="flow-x",
            pid=None,
            main_root=tmp_path,
            worktree_path=None,
            grace_seconds=0.0,
        )
        assert ok is True

    def test_local_marker_still_terminates_normally(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(end_session_cmd, "psutil", None)
        monkeypatch.setattr(
            end_session_cmd, "_pid_is_live_se3_run", lambda pid: False
        )
        _write_marker(
            tmp_path,
            encode_run_pidfile(FOREIGN_PID, stable_machine_id(), "flow-x"),
        )
        ok, _ = end_session_cmd._terminate_session_process(
            flow_id="flow-x",
            pid=None,
            main_root=tmp_path,
            worktree_path=None,
            grace_seconds=0.0,
        )
        assert ok is True

    def test_end_session_keeps_review_scopes_of_a_remotely_running_flow(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Machine B must not delete the baselines machine A is still using."""
        (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
        _write_marker(
            tmp_path,
            encode_run_pidfile(FOREIGN_PID, FOREIGN_MACHINE, "flow-x"),
        )
        scopes = _state_dir(tmp_path) / "review-scopes" / "flow-x"
        scopes.mkdir(parents=True, exist_ok=True)
        (scopes / "implementation.json").write_text("{}", encoding="utf-8")

        monkeypatch.setattr(end_session_cmd, "psutil", None)
        archived: list[str] = []
        monkeypatch.setattr(
            end_session_cmd,
            "_archive_main_session",
            lambda *a, **k: archived.append("main"),
        )

        exit_code = end_session_cmd.end_session(
            project_root=tmp_path, flow_id="flow-x"
        )

        assert exit_code == 1  # terminate step reported FAIL (inconclusive)
        assert archived == []
        assert (scopes / "implementation.json").exists()


# --------------------------------------------------------------------------
# A marker that exists but cannot be read is inconclusive, never "absent"
# --------------------------------------------------------------------------

class TestUnreadableMarkerBlocksCleanup:
    """An undecodable ``run.pid`` must block exactly like a foreign one.

    On a shared filesystem a remotely active flow's marker can be momentarily
    unreadable (permissions, an I/O error, a torn record). Collapsing that into
    "no marker" is what lets this host report termination successful and then
    delete the live flow's worktree / review baselines.
    """

    @pytest.mark.parametrize(
        "body", ["", "\n", "not-a-pid\n", "0\n", "-1\nnode-x\n"]
    )
    def test_malformed_marker_makes_termination_inconclusive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
    ) -> None:
        monkeypatch.setattr(end_session_cmd, "psutil", None)
        marker = _write_marker(tmp_path, body)
        ok, detail = end_session_cmd._terminate_session_process(
            flow_id="flow-x",
            pid=None,
            main_root=tmp_path,
            worktree_path=None,
            grace_seconds=0.0,
        )
        assert ok is False
        assert str(marker) in detail

    def test_unreadable_marker_makes_termination_inconclusive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A marker whose read raises (here: it is a directory) stands in for the
        # permission / IO failure a shared filesystem produces.
        monkeypatch.setattr(end_session_cmd, "psutil", None)
        state = _state_dir(tmp_path)
        state.mkdir(parents=True, exist_ok=True)
        (state / "run.pid").mkdir()
        ok, _ = end_session_cmd._terminate_session_process(
            flow_id="flow-x",
            pid=None,
            main_root=tmp_path,
            worktree_path=None,
            grace_seconds=0.0,
        )
        assert ok is False

    def test_operator_supplied_pid_does_not_override_an_unreadable_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        signalled: list[int] = []
        monkeypatch.setattr(
            end_session_cmd,
            "_terminate_one",
            lambda p, g: (signalled.append(p), (True, "killed"))[1],
        )
        _write_marker(tmp_path, "garbage\n")
        ok, _ = end_session_cmd._terminate_session_process(
            flow_id="flow-x",
            pid=os.getpid(),
            main_root=tmp_path,
            worktree_path=None,
            grace_seconds=0.0,
        )
        assert ok is False
        assert signalled == []

    def test_absent_marker_still_terminates_normally(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The guard must key on "a marker exists", not on "no holder decoded".
        monkeypatch.setattr(end_session_cmd, "psutil", None)
        ok, _ = end_session_cmd._terminate_session_process(
            flow_id="flow-x",
            pid=None,
            main_root=tmp_path,
            worktree_path=None,
            grace_seconds=0.0,
        )
        assert ok is True

    def test_end_session_keeps_review_scopes_on_an_unreadable_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
        _write_marker(tmp_path, "torn-record")
        scopes = _state_dir(tmp_path) / "review-scopes" / "flow-x"
        scopes.mkdir(parents=True, exist_ok=True)
        (scopes / "implementation.json").write_text("{}", encoding="utf-8")

        monkeypatch.setattr(end_session_cmd, "psutil", None)
        archived: list[str] = []
        monkeypatch.setattr(
            end_session_cmd,
            "_archive_main_session",
            lambda *a, **k: archived.append("main"),
        )

        exit_code = end_session_cmd.end_session(
            project_root=tmp_path, flow_id="flow-x"
        )

        assert exit_code == 1
        assert archived == []
        assert (scopes / "implementation.json").exists()


# --------------------------------------------------------------------------
# Cleanup runs under an exclusive ownership claim
# --------------------------------------------------------------------------

class TestOwnershipClaim:
    """Termination and cleanup must exclude a concurrent start/resume.

    Step 3 only establishes that nothing owns the flow at that instant; the
    destructive step 4 runs later. Between them another machine may resume the
    flow, so cleanup has to hold the same ``run.pid`` token a resume consults.
    """

    def test_claim_creates_a_marker_naming_this_process(
        self, tmp_path: Path
    ) -> None:
        from tianluo.core.run_pidfile import read_run_holder

        marker = end_session_cmd._claim_run_marker(tmp_path, "flow-x")
        assert marker is not None and marker.exists()
        holder = read_run_holder(marker.parent)
        assert holder.pid == os.getpid()
        assert holder.machine_id == stable_machine_id()
        assert holder.flow_id == "flow-x"

    def test_claim_fails_when_a_marker_already_exists(
        self, tmp_path: Path
    ) -> None:
        _write_marker(
            tmp_path, encode_run_pidfile(FOREIGN_PID, FOREIGN_MACHINE, "flow-x")
        )
        assert end_session_cmd._claim_run_marker(tmp_path, "flow-x") is None
        # ... and the existing marker is left exactly as it was.
        holder = foreign_run_holder(_state_dir(tmp_path))
        assert holder is not None and holder.pid == FOREIGN_PID

    def test_operator_supplied_relative_root_is_absolutized(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``-p .`` / ``-p ..`` must reach the claim as an absolute root.

        ``acquire_run_marker`` refuses a cwd-relative state dir, so a root left
        relative would blow up inside the claim instead of taking it.
        """
        nested = tmp_path / "repo" / "sub"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)

        assert end_session_cmd._resolve_main_root(Path(".")) == nested.resolve()
        assert (
            end_session_cmd._resolve_main_root(Path(".."))
            == (tmp_path / "repo").resolve()
        )

    def test_relative_root_claims_and_completes_the_destructive_window(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``luo end-session -p .`` must not abort after killing the run.

        Step 3c sits outside the try/finally that releases the claim, so a
        raising claim would leave the session un-archived with its process
        already dead — the failure mode a relative root produced.
        """
        from tianluo.core.run_pidfile import read_run_holder

        (tmp_path / "repo" / ".git").mkdir(parents=True)
        root = (tmp_path / "repo").resolve()
        monkeypatch.chdir(root)
        monkeypatch.setattr(
            end_session_cmd,
            "_terminate_session_process",
            lambda **kwargs: (True, "no process"),
        )
        during: list = []
        monkeypatch.setattr(
            end_session_cmd,
            "_archive_main_session",
            lambda *a, **k: during.append(read_run_holder(_state_dir(root))),
        )

        exit_code = end_session_cmd.end_session(
            project_root=Path("."), flow_id="flow-rel"
        )

        assert exit_code == 0
        # The claim landed in the real state dir, was held across the archive...
        assert during and during[0] is not None
        assert during[0].pid == os.getpid()
        assert during[0].machine_id == stable_machine_id()
        # ... and was released, so the flow is resumable again.
        assert not (_state_dir(root) / "run.pid").exists()

    def test_release_only_unlinks_our_own_claim(self, tmp_path: Path) -> None:
        marker = end_session_cmd._claim_run_marker(tmp_path, "flow-x")
        assert marker is not None
        # A run that overwrote our claim owns the flow now; releasing must not
        # clear its marker.
        marker.write_text(
            encode_run_pidfile(FOREIGN_PID, FOREIGN_MACHINE, "flow-x"),
            encoding="utf-8",
        )
        end_session_cmd._release_run_marker(marker)
        assert marker.exists()

        marker.write_text(
            encode_run_pidfile(os.getpid(), stable_machine_id(), "flow-x"),
            encoding="utf-8",
        )
        end_session_cmd._release_run_marker(marker)
        assert not marker.exists()

    def test_cleanup_runs_under_the_claim_and_releases_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tianluo.core.run_pidfile import read_run_holder

        (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(
            end_session_cmd,
            "_terminate_session_process",
            lambda **kwargs: (True, "no process"),
        )
        during: list = []

        def _archive(*a, **k):
            during.append(read_run_holder(_state_dir(tmp_path)))

        monkeypatch.setattr(end_session_cmd, "_archive_main_session", _archive)

        exit_code = end_session_cmd.end_session(
            project_root=tmp_path, flow_id="flow-x"
        )

        assert exit_code == 0
        # The claim was held for the whole destructive step...
        assert during and during[0] is not None
        assert during[0].pid == os.getpid()
        assert during[0].machine_id == stable_machine_id()
        # ... and dropped afterwards, so the flow is resumable again.
        assert not (_state_dir(tmp_path) / "run.pid").exists()

    def test_marker_appearing_after_the_liveness_check_blocks_cleanup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reported race: machine A resumes while machine B is ending."""
        (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
        scopes = _state_dir(tmp_path) / "review-scopes" / "flow-x"
        scopes.mkdir(parents=True, exist_ok=True)
        (scopes / "implementation.json").write_text("{}", encoding="utf-8")

        def _terminate(**kwargs):
            # Machine A resumes the paused flow the instant after this host
            # concluded nothing was running.
            _write_marker(
                tmp_path,
                encode_run_pidfile(FOREIGN_PID, FOREIGN_MACHINE, "flow-x"),
            )
            return True, "no live process found"

        monkeypatch.setattr(
            end_session_cmd, "_terminate_session_process", _terminate
        )
        archived: list[str] = []
        monkeypatch.setattr(
            end_session_cmd,
            "_archive_main_session",
            lambda *a, **k: archived.append("main"),
        )

        exit_code = end_session_cmd.end_session(
            project_root=tmp_path, flow_id="flow-x"
        )

        assert exit_code == 1
        assert archived == []
        assert (scopes / "implementation.json").exists()
        # The resuming machine's marker is untouched.
        holder = foreign_run_holder(_state_dir(tmp_path))
        assert holder is not None and holder.pid == FOREIGN_PID

    def test_worktree_archive_never_carries_the_claim(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The archive records the terminated flow, not end-session's claim."""
        import shutil

        from tianluo.engine.merge import cleanup as merge_cleanup

        root = tmp_path / "project"
        worktree = tmp_path / "wt"
        archive = tmp_path / "archive"
        root.mkdir(parents=True)
        worktree.mkdir(parents=True)

        claim = end_session_cmd._claim_run_marker(worktree, "flow-x")
        assert claim is not None

        def fake_archive(project_root, branch, wt_path, **kwargs):
            excludes = set(kwargs.get("exclude_relpaths") or ())
            shutil.copytree(
                wt_path,
                archive,
                ignore=lambda d, names: {
                    n
                    for n in names
                    if str(
                        (Path(d) / n).relative_to(wt_path)
                    ) in excludes
                },
            )
            return archive

        monkeypatch.setattr(merge_cleanup, "_archive_worktree", fake_archive)
        end_session_cmd._archive_worktree_session(
            root,
            "flow-x",
            {"worktree_path": str(worktree), "worktree_branch": "impl/x"},
            [],
            claimed_marker=claim,
        )

        assert archive.is_dir()
        assert not (archive / "tianluo" / "state" / "run.pid").exists()


# --------------------------------------------------------------------------
# The claim and a starting/resuming run exclude each other
# --------------------------------------------------------------------------

class TestClaimExcludesStartAndResume:
    """One protocol, both directions.

    The claim is only worth taking if the counterparty cannot publish over it:
    ``luo run`` used to write ``run.pid`` with an unconditional tmp+rename, so a
    resume landing mid-window replaced the claim and left end-session archiving
    a flow that had just become live again.
    """

    def _persistence(self, root: Path):
        from tianluo.engine.persistence import PersistenceManager

        return PersistenceManager(root)

    def test_a_run_cannot_publish_over_a_held_claim(self, tmp_path: Path) -> None:
        from tianluo.commands import run as run_cmd
        from tianluo.core.run_pidfile import read_run_holder

        claim = encode_run_pidfile(
            _live_local_pid(), stable_machine_id(), "flow-x"
        )
        _write_marker(tmp_path, claim)

        acquired = run_cmd._acquire_run_pidfile(self._persistence(tmp_path), "flow-x")
        assert not acquired.acquired
        assert acquired.blocked
        # ... and the claim is intact, so the destructive window is still its.
        holder = read_run_holder(_state_dir(tmp_path))
        assert holder.pid == _live_local_pid()

    def test_a_foreign_run_marker_cannot_be_published_over(
        self, tmp_path: Path
    ) -> None:
        from tianluo.commands import run as run_cmd

        _write_marker(
            tmp_path, encode_run_pidfile(FOREIGN_PID, FOREIGN_MACHINE, "flow-x")
        )
        acquired = run_cmd._acquire_run_pidfile(self._persistence(tmp_path), "flow-x")
        assert not acquired.acquired and acquired.blocked
        assert foreign_run_holder(_state_dir(tmp_path)).pid == FOREIGN_PID

    def test_run_flow_refuses_while_the_claim_is_held(self, tmp_path: Path) -> None:
        from tianluo.commands import run as run_cmd
        from tianluo.core.run_pidfile import read_run_holder

        _write_marker(
            tmp_path,
            encode_run_pidfile(_live_local_pid(), stable_machine_id(), "flow-x"),
        )

        assert run_cmd.run_flow(project_root=tmp_path, flow_id="flow-x") == 1
        # The refused run must not have dropped the owner's marker on its way
        # out (it never entered the lifecycle that clears it).
        holder = read_run_holder(_state_dir(tmp_path))
        assert holder is not None and holder.pid == _live_local_pid()

    def test_a_live_local_run_marker_blocks_the_claim(self, tmp_path: Path) -> None:
        # A run that owns the state dir keeps it: end-session may only claim
        # once step 3b has established the recorded pid is dead.
        _write_marker(
            tmp_path,
            encode_run_pidfile(_live_local_pid(), stable_machine_id(), "flow-x"),
        )
        assert end_session_cmd._claim_run_marker(tmp_path, "flow-x") is None

    def test_a_dead_local_marker_does_not_wedge_a_new_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tianluo.commands import run as run_cmd

        _write_marker(
            tmp_path, encode_run_pidfile(999999, stable_machine_id(), "flow-old")
        )
        monkeypatch.setattr(
            "tianluo.daemon.supervisor._is_alive", lambda pid: False
        )
        claim = run_cmd._acquire_run_pidfile(self._persistence(tmp_path), "flow-x")
        assert claim.acquired

    def test_a_live_end_session_claim_is_never_judged_stale_by_a_run(
        self, tmp_path: Path
    ) -> None:
        """The claim names a live ``luo end-session``, not an ``luo run``.

        Judging staleness by cmdline would declare it stale and steal it,
        re-opening the exact window the claim exists to close.
        """
        from tianluo.commands import run as run_cmd
        from tianluo.core.run_pidfile import RunHolder

        holder = RunHolder(os.getpid(), stable_machine_id(), "flow-x")
        assert run_cmd._run_marker_is_stale(holder) is False


class TestClaimIsRevalidatedThroughoutTheWindow:
    """A single verification at claim time is not enough.

    A pre-upgrade ``luo run`` elsewhere on a shared filesystem still publishes
    ``run.pid`` unconditionally, so ownership is re-read immediately before
    every irreversible step and the rest is abandoned when it is gone.
    """

    def test_main_archive_aborts_when_the_claim_was_taken_over(
        self, tmp_path: Path
    ) -> None:
        from tianluo.engine.persistence import PersistenceManager

        pm = PersistenceManager(tmp_path)
        pm.ensure_directories()
        pm.state_file.write_text('{"flow_id": "flow-x"}', encoding="utf-8")
        scopes = _state_dir(tmp_path) / "review-scopes" / "flow-x"
        scopes.mkdir(parents=True, exist_ok=True)
        (scopes / "implementation.json").write_text("{}", encoding="utf-8")

        claim = end_session_cmd._claim_run_marker(tmp_path, "flow-x")
        assert claim is not None
        # A writer that ignores the protocol overwrites the claim mid-window.
        _write_marker(
            tmp_path, encode_run_pidfile(FOREIGN_PID, FOREIGN_MACHINE, "flow-x")
        )

        results: list = []
        end_session_cmd._archive_main_session(
            tmp_path, "flow-x", results, claimed_marker=claim
        )

        assert [row[1] for row in results] == ["FAIL"]
        assert pm.state_file.exists()
        assert (scopes / "implementation.json").exists()

    def test_worktree_archive_aborts_when_the_claim_was_taken_over(
        self, tmp_path: Path
    ) -> None:
        root = tmp_path / "project"
        worktree = tmp_path / "wt"
        root.mkdir(parents=True)
        worktree.mkdir(parents=True)
        scopes = _state_dir(worktree) / "review-scopes" / "flow-x"
        scopes.mkdir(parents=True, exist_ok=True)
        (scopes / "implementation.json").write_text("{}", encoding="utf-8")

        claim = end_session_cmd._claim_run_marker(worktree, "flow-x")
        assert claim is not None
        _write_marker(
            worktree, encode_run_pidfile(FOREIGN_PID, FOREIGN_MACHINE, "flow-x")
        )

        results: list = []
        end_session_cmd._archive_worktree_session(
            root,
            "flow-x",
            {"worktree_path": str(worktree), "worktree_branch": "impl/x"},
            results,
            claimed_marker=claim,
        )

        assert [row[1] for row in results] == ["FAIL"]
        assert worktree.is_dir()
        assert (scopes / "implementation.json").exists()

    def test_worktree_deletion_is_skipped_when_ownership_changes_mid_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The takeover lands after the archive copy but before the delete."""
        from tianluo.engine.merge import cleanup as merge_cleanup

        root = tmp_path / "project"
        worktree = tmp_path / "wt"
        root.mkdir(parents=True)
        worktree.mkdir(parents=True)

        claim = end_session_cmd._claim_run_marker(worktree, "flow-x")
        assert claim is not None

        monkeypatch.setattr(
            merge_cleanup, "_archive_worktree", lambda *a, **k: tmp_path / "arch"
        )
        monkeypatch.setattr(
            merge_cleanup, "_promote_completed_engine_state", lambda *a, **k: None
        )

        def _sync(*_a, **_k):
            # A resume publishes over the claim while the archive is running.
            _write_marker(
                worktree,
                encode_run_pidfile(FOREIGN_PID, FOREIGN_MACHINE, "flow-x"),
            )
            return 0

        monkeypatch.setattr(end_session_cmd, "_sync_worktree_history", _sync)
        deleted: list = []
        monkeypatch.setattr(
            "tianluo.engine.worktree.delete_branch",
            lambda *a, **k: deleted.append("branch"),
        )
        monkeypatch.setattr(
            "tianluo.engine.worktree.force_cleanup_worktree",
            lambda *a, **k: deleted.append("worktree"),
        )

        results: list = []
        end_session_cmd._archive_worktree_session(
            root,
            "flow-x",
            {"worktree_path": str(worktree), "worktree_branch": "impl/x"},
            results,
            claimed_marker=claim,
        )

        assert deleted == []
        assert worktree.is_dir()
        assert any(
            row[1] == "FAIL"
            and row[0] == end_session_cmd.t("end_session.step.cleanup_branch")
            for row in results
        )

    def test_resumable_and_baselines_survive_a_takeover_mid_worktree_cleanup(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A takeover after the archive copy must stop both deletions.

        The re-owned flow resumes from that snapshot and rebuilds its
        SELF_CHECK scope from those baselines; deleting either "because we
        checked on entry" leaves a live flow without state nothing later can
        restore. Both therefore retire on the SAME ownership re-read, after the
        archive window — the copy is kept clean by excluding the store, not by
        reclaiming it before 4.1.
        """
        from tianluo.engine.merge import cleanup as merge_cleanup
        from tianluo.engine.persistence import PersistenceManager

        root = tmp_path / "project"
        worktree = tmp_path / "wt"
        root.mkdir(parents=True)
        worktree.mkdir(parents=True)

        pm = PersistenceManager(worktree)
        pm.ensure_directories()
        pm.resumable_dir.mkdir(parents=True, exist_ok=True)
        snapshot = pm.resumable_dir / "flow-x.json"
        snapshot.write_text('{"flow_id": "flow-x"}', encoding="utf-8")
        scopes = _state_dir(worktree) / "review-scopes" / "flow-x"
        scopes.mkdir(parents=True, exist_ok=True)
        (scopes / "implementation.json").write_text("{}", encoding="utf-8")

        claim = end_session_cmd._claim_run_marker(worktree, "flow-x")
        assert claim is not None

        monkeypatch.setattr(
            merge_cleanup, "_archive_worktree", lambda *a, **k: tmp_path / "arch"
        )
        monkeypatch.setattr(
            merge_cleanup, "_promote_completed_engine_state", lambda *a, **k: None
        )

        def _sync(*_a, **_k):
            # A pre-upgrade run elsewhere publishes over the claim mid-window.
            _write_marker(
                worktree,
                encode_run_pidfile(FOREIGN_PID, FOREIGN_MACHINE, "flow-x"),
            )
            return 0

        monkeypatch.setattr(end_session_cmd, "_sync_worktree_history", _sync)

        results: list = []
        end_session_cmd._archive_worktree_session(
            root,
            "flow-x",
            {"worktree_path": str(worktree), "worktree_branch": "impl/x"},
            results,
            claimed_marker=claim,
        )

        assert snapshot.exists()
        assert (scopes / "implementation.json").exists()
        assert any(
            row[1] == "FAIL"
            and row[0] == end_session_cmd.t("end_session.step.clear_resumable")
            for row in results
        )

    def test_main_baselines_survive_a_takeover_after_the_state_archive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tianluo.engine.persistence import PersistenceManager

        pm = PersistenceManager(tmp_path)
        pm.ensure_directories()
        pm.state_file.write_text('{"flow_id": "flow-x"}', encoding="utf-8")
        pm.resumable_dir.mkdir(parents=True, exist_ok=True)
        snapshot = pm.resumable_dir / "flow-x.json"
        snapshot.write_text('{"flow_id": "flow-x"}', encoding="utf-8")
        scopes = _state_dir(tmp_path) / "review-scopes" / "flow-x"
        scopes.mkdir(parents=True, exist_ok=True)
        (scopes / "implementation.json").write_text("{}", encoding="utf-8")

        claim = end_session_cmd._claim_run_marker(tmp_path, "flow-x")
        assert claim is not None

        real_clear = PersistenceManager.clear_state

        def _clear(self):
            # The takeover lands while the engine state is being archived.
            _write_marker(
                tmp_path,
                encode_run_pidfile(FOREIGN_PID, FOREIGN_MACHINE, "flow-x"),
            )
            return real_clear(self)

        monkeypatch.setattr(PersistenceManager, "clear_state", _clear)

        results: list = []
        end_session_cmd._archive_main_session(
            tmp_path, "flow-x", results, claimed_marker=claim
        )

        assert snapshot.exists()
        assert (scopes / "implementation.json").exists()
        assert any(
            row[1] == "FAIL"
            and row[0] == end_session_cmd.t("end_session.step.clear_resumable")
            for row in results
        )

    def test_branch_inference_window_is_revalidated_before_deletion(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A takeover DURING ``_infer_worktree_branch`` still stops the delete.

        The inference shells out to ``git worktree list``, an unbounded wait on
        a busy or network-mounted repository, so the check taken before it can
        be arbitrarily old by the time the branch/worktree deletion runs.
        """
        from tianluo.engine.merge import cleanup as merge_cleanup

        root = tmp_path / "project"
        worktree = tmp_path / "wt"
        root.mkdir(parents=True)
        worktree.mkdir(parents=True)

        claim = end_session_cmd._claim_run_marker(worktree, "flow-x")
        assert claim is not None

        monkeypatch.setattr(
            merge_cleanup, "_archive_worktree", lambda *a, **k: tmp_path / "arch"
        )
        monkeypatch.setattr(
            merge_cleanup, "_promote_completed_engine_state", lambda *a, **k: None
        )
        monkeypatch.setattr(
            end_session_cmd, "_sync_worktree_history", lambda *a, **k: 0
        )

        def _infer(*_a, **_k):
            # The pre-upgrade remote run publishes while git is being consulted.
            _write_marker(
                worktree,
                encode_run_pidfile(FOREIGN_PID, FOREIGN_MACHINE, "flow-x"),
            )
            return "impl/inferred"

        monkeypatch.setattr(end_session_cmd, "_infer_worktree_branch", _infer)
        deleted: list = []
        monkeypatch.setattr(
            "tianluo.engine.worktree.delete_branch",
            lambda *a, **k: deleted.append("branch"),
        )
        monkeypatch.setattr(
            "tianluo.engine.worktree.force_cleanup_worktree",
            lambda *a, **k: deleted.append("worktree"),
        )
        monkeypatch.setattr(
            "tianluo.engine.worktree.remove_worktree",
            lambda *a, **k: deleted.append("by-path"),
        )

        results: list = []
        end_session_cmd._archive_worktree_session(
            root,
            "flow-x",
            # No recorded branch: the inference runs, and the takeover lands
            # inside it.
            {"worktree_path": str(worktree), "worktree_branch": None},
            results,
            claimed_marker=claim,
        )

        assert deleted == []
        assert worktree.is_dir()
        assert any(
            row[1] == "FAIL"
            and row[0] == end_session_cmd.t("end_session.step.cleanup_branch")
            for row in results
        )

    def test_main_state_archive_is_revalidated_before_clear_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A takeover during the engine.json read must stop ``clear_state``."""
        from tianluo.engine.persistence import PersistenceManager

        pm = PersistenceManager(tmp_path)
        pm.ensure_directories()
        pm.state_file.write_text('{"flow_id": "flow-x"}', encoding="utf-8")
        pm.resumable_dir.mkdir(parents=True, exist_ok=True)
        snapshot = pm.resumable_dir / "flow-x.json"
        snapshot.write_text('{"flow_id": "flow-x"}', encoding="utf-8")
        scopes = _state_dir(tmp_path) / "review-scopes" / "flow-x"
        scopes.mkdir(parents=True, exist_ok=True)
        (scopes / "implementation.json").write_text("{}", encoding="utf-8")

        claim = end_session_cmd._claim_run_marker(tmp_path, "flow-x")
        assert claim is not None

        cleared: list = []
        monkeypatch.setattr(
            PersistenceManager, "clear_state", lambda self: cleared.append(1)
        )

        def _read(*_a, **_k):
            # The takeover lands while the main engine.json is being read.
            _write_marker(
                tmp_path,
                encode_run_pidfile(FOREIGN_PID, FOREIGN_MACHINE, "flow-x"),
            )
            return "flow-x"

        monkeypatch.setattr(end_session_cmd, "_read_main_flow_id", _read)

        results: list = []
        end_session_cmd._archive_main_session(
            tmp_path, "flow-x", results, claimed_marker=claim
        )

        assert cleared == []
        assert pm.state_file.exists()
        assert snapshot.exists()
        assert (scopes / "implementation.json").exists()
        assert any(
            row[1] == "FAIL"
            and row[0] == end_session_cmd.t("end_session.step.archive_session")
            for row in results
        )


class TestTerminalDispositionGatesTheBaselineReclaim:
    """Review baselines retire on a landed terminal disposition, never on an
    attempted one.

    Resumability has two channels: the resumable snapshot AND a still
    non-COMPLETED ``engine.json`` (``load_flow_by_id`` resolves the active
    state file first, and ``find_resumable_worktree_runs`` scans preserved
    worktrees). Clearing the snapshot is survivable — a resumed flow rebuilds
    it — but dropping the baselines is not: the resumed flow reaches SELF_CHECK
    with no baseline to diff against, and ``luo review-scope diff`` reports the
    snapshots as "cleaned up when a flow completes or is terminated" for a flow
    that did neither.
    """

    def test_main_baselines_survive_a_failed_state_archive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tianluo.engine.persistence import PersistenceManager

        pm = PersistenceManager(tmp_path)
        pm.ensure_directories()
        pm.state_file.write_text('{"flow_id": "flow-x"}', encoding="utf-8")
        pm.resumable_dir.mkdir(parents=True, exist_ok=True)
        snapshot = pm.resumable_dir / "flow-x.json"
        snapshot.write_text('{"flow_id": "flow-x"}', encoding="utf-8")
        scopes = _state_dir(tmp_path) / "review-scopes" / "flow-x"
        scopes.mkdir(parents=True, exist_ok=True)
        (scopes / "implementation.json").write_text("{}", encoding="utf-8")

        claim = end_session_cmd._claim_run_marker(tmp_path, "flow-x")
        assert claim is not None

        def _clear(self):
            # tianluo/state/archive/ unwritable: ENOSPC / EACCES at the rotate.
            raise OSError("archive directory is not writable")

        monkeypatch.setattr(PersistenceManager, "clear_state", _clear)

        results: list = []
        end_session_cmd._archive_main_session(
            tmp_path, "flow-x", results, claimed_marker=claim
        )

        # engine.json still holds the flow in a non-COMPLETED state, so it is
        # still offered by the resume picker — its baselines must stay.
        assert pm.state_file.exists()
        assert (scopes / "implementation.json").exists()
        # The survivable half still runs: a resume rebuilds the snapshot.
        assert not snapshot.exists()
        rows = {row[0]: row[1] for row in results}
        assert rows[end_session_cmd.t("end_session.step.archive_session")] == "FAIL"
        assert rows[end_session_cmd.t("end_session.step.clear_resumable")] == "OK"

    def test_worktree_baselines_survive_a_failed_archive(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tianluo.engine.merge import cleanup as merge_cleanup
        from tianluo.engine.persistence import PersistenceManager

        root = tmp_path / "project"
        worktree = tmp_path / "wt"
        root.mkdir(parents=True)
        worktree.mkdir(parents=True)
        wt_pm = PersistenceManager(worktree)
        wt_pm.ensure_directories()
        wt_pm.resumable_dir.mkdir(parents=True, exist_ok=True)
        snapshot = wt_pm.resumable_dir / "flow-x.json"
        snapshot.write_text('{"flow_id": "flow-x"}', encoding="utf-8")
        scopes = _state_dir(worktree) / "review-scopes" / "flow-x"
        scopes.mkdir(parents=True, exist_ok=True)
        (scopes / "implementation.json").write_text("{}", encoding="utf-8")

        claim = end_session_cmd._claim_run_marker(worktree, "flow-x")
        assert claim is not None

        def _archive(*_a, **_k):
            raise OSError("no space left on device")

        monkeypatch.setattr(merge_cleanup, "_archive_worktree", _archive)
        monkeypatch.setattr(
            merge_cleanup, "_promote_completed_engine_state", lambda *a, **k: None
        )
        monkeypatch.setattr(
            end_session_cmd, "_sync_worktree_history", lambda *a, **k: 0
        )

        results: list = []
        end_session_cmd._archive_worktree_session(
            root,
            "flow-x",
            {"worktree_path": str(worktree), "worktree_branch": "impl/x"},
            results,
            claimed_marker=claim,
        )

        # 4.5 preserves the worktree as the only copy of the unfinished work,
        # so the flow stays resumable and its baselines must survive with it.
        assert worktree.is_dir()
        assert (scopes / "implementation.json").exists()
        assert not snapshot.exists()
        rows = {row[0]: row[1] for row in results}
        assert rows[end_session_cmd.t("end_session.step.archive_worktree")] == "FAIL"
        assert rows[end_session_cmd.t("end_session.step.clear_resumable")] == "OK"
        assert rows[end_session_cmd.t("end_session.step.cleanup_branch")] == "SKIP"
