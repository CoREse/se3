"""``run.pid`` machine-awareness in ``luo end-session``.

Two shared-filesystem hazards are covered:

* A ``run.pid`` recorded on ANOTHER machine must never be acted on locally —
  its PID number means nothing in this host's process table, so probing it can
  match (and then SIGTERM) an unrelated local ``luo run`` that happens to hold
  the same number.
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
