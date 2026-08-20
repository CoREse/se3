"""Codec + attribution rules of the ``tianluo/state/run.pid`` holder record.

The marker is scoped to a *state dir*, not to a flow, so a project root's
record may name a live run of a different flow than the one being resumed. The
flow-id line (and :meth:`RunHolder.owns_flow`) is what keeps a refusal from
claiming the requested flow is the one running there.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from tianluo.core.machine_id import stable_machine_id
from tianluo.core.run_pidfile import (
    RunHolder,
    acquire_run_marker,
    encode_run_pidfile,
    foreign_run_holder,
    holds_run_marker,
    probe_run_marker,
    read_run_holder,
    read_run_pidfile,
    release_run_marker,
)

FOREIGN = "node-elsewhere-deadbeef"


def _write(state_dir: Path, text: str) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    marker = state_dir / "run.pid"
    marker.write_text(text, encoding="utf-8")
    return marker


class TestCodecRoundTrip:
    def test_full_record_round_trips(self, tmp_path: Path) -> None:
        _write(tmp_path, encode_run_pidfile(4242, FOREIGN, "flow-a"))
        assert read_run_holder(tmp_path) == RunHolder(4242, FOREIGN, "flow-a")

    def test_flow_id_omitted_when_not_yet_minted(self, tmp_path: Path) -> None:
        # A brand-new run stamps the marker before the engine mints the flow id.
        _write(tmp_path, encode_run_pidfile(7, FOREIGN))
        assert read_run_holder(tmp_path) == RunHolder(7, FOREIGN, None)

    def test_legacy_records_decode_by_truncation(self, tmp_path: Path) -> None:
        _write(tmp_path, "99\n")
        assert read_run_holder(tmp_path) == RunHolder(99, None, None)
        _write(tmp_path, f"99\n{FOREIGN}\n")
        assert read_run_holder(tmp_path) == RunHolder(99, FOREIGN, None)

    def test_narrow_view_still_returns_pid_and_machine(self, tmp_path: Path) -> None:
        _write(tmp_path, encode_run_pidfile(4242, FOREIGN, "flow-a"))
        assert read_run_pidfile(tmp_path) == (4242, FOREIGN)

    def test_unusable_records_yield_nothing(self, tmp_path: Path) -> None:
        assert read_run_holder(tmp_path) is None  # absent
        _write(tmp_path, "")
        assert read_run_holder(tmp_path) is None
        _write(tmp_path, "not-a-pid\n")
        assert read_run_holder(tmp_path) is None
        _write(tmp_path, "0\n")
        assert read_run_holder(tmp_path) is None


class TestOwnsFlow:
    def test_recorded_flow_id_decides(self) -> None:
        holder = RunHolder(1, FOREIGN, "flow-a")
        assert holder.owns_flow("flow-a")
        assert not holder.owns_flow("flow-b")
        # A stamped record is authoritative even in the flow's own worktree.
        assert not holder.owns_flow("flow-b", flow_scoped=True)

    def test_unstamped_record_is_claimed_only_in_a_flow_scoped_dir(self) -> None:
        holder = RunHolder(1, FOREIGN, None)
        # Shared project root: ambiguous, must not be blamed on the flow.
        assert not holder.owns_flow("flow-a")
        # The flow's own isolation worktree can host no other flow.
        assert holder.owns_flow("flow-a", flow_scoped=True)


class TestForeignHolder:
    def test_foreign_record_is_returned_whole(self, tmp_path: Path) -> None:
        _write(tmp_path, encode_run_pidfile(4242, FOREIGN, "flow-a"))
        holder = foreign_run_holder(tmp_path)
        assert holder == RunHolder(4242, FOREIGN, "flow-a")

    def test_local_and_legacy_records_are_not_foreign(self, tmp_path: Path) -> None:
        _write(tmp_path, encode_run_pidfile(4242, stable_machine_id(), "flow-a"))
        assert foreign_run_holder(tmp_path) is None
        _write(tmp_path, "4242\n")
        assert foreign_run_holder(tmp_path) is None


class TestMarkerProbe:
    """"Absent" and "unreadable" must stay distinguishable.

    A caller about to delete a flow's worktree cannot treat a marker it merely
    failed to read as proof that no run claimed the state dir — on a shared
    filesystem that marker may belong to a run alive on another host.
    """

    def test_absent_marker_is_not_present(self, tmp_path: Path) -> None:
        probe = probe_run_marker(tmp_path)
        assert probe.present is False
        assert probe.holder is None
        assert probe.undecidable is False

    def test_decodable_marker_is_present_with_a_holder(self, tmp_path: Path) -> None:
        _write(tmp_path, encode_run_pidfile(4242, FOREIGN, "flow-a"))
        probe = probe_run_marker(tmp_path)
        assert probe.present is True
        assert probe.holder == RunHolder(4242, FOREIGN, "flow-a")
        assert probe.undecidable is False

    def test_malformed_marker_is_present_but_undecidable(self, tmp_path: Path) -> None:
        for body in ("", "\n", "not-a-pid\n", "0\n"):
            _write(tmp_path, body)
            probe = probe_run_marker(tmp_path)
            assert probe.present is True, body
            assert probe.undecidable is True, body

    def test_unreadable_marker_is_present_but_undecidable(self, tmp_path: Path) -> None:
        # Stands in for the permission / IO failure a shared filesystem gives.
        (tmp_path / "run.pid").mkdir(parents=True)
        probe = probe_run_marker(tmp_path)
        assert probe.present is True
        assert probe.undecidable is True


class TestExclusivePublication:
    """``run.pid`` is an ownership token, not advisory bookkeeping.

    Both writers — a starting/resuming ``luo run`` and the ``luo end-session``
    claim that guards its destructive window — publish through
    :func:`acquire_run_marker`, so neither can overwrite the other's record.
    Without that, an end-session that just verified nothing owns the flow could
    be overtaken by a resume and go on to delete a live flow's worktree.
    """

    def test_first_writer_wins_and_the_second_is_blocked(
        self, tmp_path: Path
    ) -> None:
        first = acquire_run_marker(tmp_path, "flow-a")
        assert first.acquired and first.holder.pid == os.getpid()

        # A different owner's record must survive an attempted publication.
        _write(tmp_path, encode_run_pidfile(4242, FOREIGN, "flow-a"))
        second = acquire_run_marker(tmp_path, "flow-b")
        assert not second.acquired
        assert second.blocked
        assert second.holder.pid == 4242
        assert read_run_holder(tmp_path) == RunHolder(4242, FOREIGN, "flow-a")

    def test_reclaiming_our_own_marker_is_idempotent(self, tmp_path: Path) -> None:
        # This is how a new run fills in the flow id the engine mints later.
        assert acquire_run_marker(tmp_path).acquired
        assert read_run_holder(tmp_path).flow_id is None
        assert acquire_run_marker(tmp_path, "flow-a").acquired
        holder = read_run_holder(tmp_path)
        assert holder.pid == os.getpid() and holder.flow_id == "flow-a"

    def test_a_dead_local_marker_is_reclaimable_when_allowed(
        self, tmp_path: Path
    ) -> None:
        _write(tmp_path, encode_run_pidfile(4242, stable_machine_id(), "flow-a"))
        # Without a staleness predicate the marker always holds...
        assert not acquire_run_marker(tmp_path, "flow-b").acquired
        # ...with one, an abandoned local record is reclaimed so a crashed run
        # cannot wedge the project forever.
        claim = acquire_run_marker(tmp_path, "flow-b", is_stale=lambda h: True)
        assert claim.acquired
        assert read_run_holder(tmp_path).pid == os.getpid()

    def test_a_foreign_marker_is_never_reclaimed_as_stale(
        self, tmp_path: Path
    ) -> None:
        # Liveness of a foreign pid is undecidable here, so the predicate is
        # not even consulted for another machine's record.
        _write(tmp_path, encode_run_pidfile(4242, FOREIGN, "flow-a"))
        claim = acquire_run_marker(tmp_path, "flow-b", is_stale=lambda h: True)
        assert not claim.acquired and claim.blocked
        assert read_run_holder(tmp_path) == RunHolder(4242, FOREIGN, "flow-a")

    def test_an_undecodable_marker_blocks(self, tmp_path: Path) -> None:
        _write(tmp_path, "not-a-pid\n")
        claim = acquire_run_marker(tmp_path, "flow-a")
        assert not claim.acquired and claim.blocked and claim.holder is None

    def test_an_io_failure_blocks_instead_of_starting_unowned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fail CLOSED: an EIO/EACCES cannot prove nobody owns the state dir.

        Reported as "not blocked", the caller would enter the flow owning no
        token at all — writing state beside an ``luo end-session`` that still
        believes it owns the flow and is deleting its baselines.
        """
        real_open = os.open

        def _boom(path, *args, **kwargs):
            if str(path).endswith("run.pid"):
                raise OSError(errno.EIO, "I/O error")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", _boom)
        claim = acquire_run_marker(tmp_path, "flow-a")
        assert not claim.acquired and claim.blocked

    def test_an_io_failure_still_names_the_owner_it_can_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The refusal is more actionable when the surviving record decodes.
        _write(tmp_path, encode_run_pidfile(4242, FOREIGN, "flow-a"))
        real_open = os.open

        def _boom(path, *args, **kwargs):
            if str(path).endswith("run.pid"):
                raise OSError(errno.EACCES, "denied")
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(os, "open", _boom)
        claim = acquire_run_marker(tmp_path, "flow-a")
        assert not claim.acquired and claim.blocked
        assert claim.holder == RunHolder(4242, FOREIGN, "flow-a")

    def test_a_failed_record_write_blocks_and_leaves_no_marker(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The exclusive create landed but the record could not be written.

        The half-written marker is dropped (an undecidable record would wedge
        the flow for good), and because ownership was never established the
        claim must still read as held.
        """
        def _boom(*_a, **_k):
            raise OSError(errno.ENOSPC, "no space")

        monkeypatch.setattr(os, "fdopen", _boom)
        claim = acquire_run_marker(tmp_path, "flow-a")
        assert not claim.acquired and claim.blocked
        assert not (tmp_path / "run.pid").exists()


class TestReleaseRules:
    def test_release_only_drops_our_own_record(self, tmp_path: Path) -> None:
        assert acquire_run_marker(tmp_path, "flow-a").acquired
        _write(tmp_path, encode_run_pidfile(4242, FOREIGN, "flow-a"))
        assert release_run_marker(tmp_path) is False
        assert (tmp_path / "run.pid").exists()

        _write(tmp_path, encode_run_pidfile(os.getpid(), stable_machine_id()))
        assert release_run_marker(tmp_path) is True
        assert not (tmp_path / "run.pid").exists()

    def test_undecodable_record_is_dropped_only_for_the_owning_run(
        self, tmp_path: Path
    ) -> None:
        # end-session's momentary claim must leave it (it may be a live remote
        # run's marker); the exiting run that owned the state dir must drop it,
        # or the corrupted record reads as "held" forever.
        _write(tmp_path, "garbage\n")
        assert release_run_marker(tmp_path) is False
        assert release_run_marker(tmp_path, drop_undecodable=True) is True
        assert not (tmp_path / "run.pid").exists()

    def test_holds_run_marker_tracks_ownership(self, tmp_path: Path) -> None:
        assert holds_run_marker(tmp_path) is False
        assert acquire_run_marker(tmp_path, "flow-a").acquired
        assert holds_run_marker(tmp_path) is True
        _write(tmp_path, encode_run_pidfile(4242, FOREIGN, "flow-a"))
        assert holds_run_marker(tmp_path) is False
