"""Codec + attribution rules of the ``tianluo/state/run.pid`` holder record.

The marker is scoped to a *state dir*, not to a flow, so a project root's
record may name a live run of a different flow than the one being resumed. The
flow-id line (and :meth:`RunHolder.owns_flow`) is what keeps a refusal from
claiming the requested flow is the one running there.
"""

from __future__ import annotations

from pathlib import Path

from tianluo.core.machine_id import stable_machine_id
from tianluo.core.run_pidfile import (
    RunHolder,
    encode_run_pidfile,
    foreign_run_holder,
    read_run_holder,
    read_run_pidfile,
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
