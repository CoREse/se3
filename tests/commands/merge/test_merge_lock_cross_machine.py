"""Cross-machine single-writer regression tests for the merge lock.

These cover the shared-filesystem multi-machine failure mode where a lock
holder lives on a *different* host, whose PID is meaningless in the local
process table. The forensic root cause was: a foreign holder's PID was probed
with the local process table, judged "dead", and its still-live lock was
force-broken — letting two engines execute the same flow concurrently.

Scenarios (mapping to the design's requirement 5):

* (a) A forged foreign-machine holder record is NEVER judged stale or broken —
  ``acquire`` raises :class:`MergeLockBusy` (carrying the machine id), the
  stale-break path is never entered, and the lock file is left byte-for-byte
  intact.
* (c-lock) A legacy 17-byte pure-PID record (no machine field) is treated as
  local, so the pre-upgrade stale semantics still apply.
* (d) A same-machine dead-PID stale lock is still reclaimed by ``break_stale``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import tianluo
from tianluo.commands.merge import merge_lock as ml
from tianluo.commands.merge.merge_lock import (
    MergeLock,
    MergeLockBusy,
    MergeLockStale,
    inspect_lock,
)
from tianluo.core.machine_id import stable_machine_id

_SRC_ROOT = str(Path(tianluo.__file__).resolve().parent.parent)

_FOREIGN_MACHINE = "otherhost-deadbeef"
# A PID that is (essentially) guaranteed not to exist locally, so that if the
# machine guard were ever bypassed the holder would be misjudged "dead".
_DEAD_PID = 999999


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    (tmp_path / "tianluo" / "state").mkdir(parents=True)
    return tmp_path


def _lock_path(project_root: Path) -> Path:
    return project_root / "tianluo" / "state" / "merge.lock"


# --------------------------------------------------------------------------
# Record encode / decode invariants
# --------------------------------------------------------------------------

class TestHolderRecordCodec:
    def test_new_record_round_trips_pid_and_machine(self) -> None:
        rec = ml._encode_holder_record(12345, "host-abc123")
        assert len(rec) == ml._HOLDER_RECORD_LEN
        assert ml._decode_holder_record(rec) == (12345, "host-abc123", False)

    def test_record_is_fixed_width_regardless_of_machine_len(self) -> None:
        short = ml._encode_holder_record(1, "a")
        long = ml._encode_holder_record(1, "b" * 40)
        assert len(short) == len(long) == ml._HOLDER_RECORD_LEN

    def test_overlong_machine_id_is_truncated_not_corrupting(self) -> None:
        rec = ml._encode_holder_record(7, "z" * 5000)
        # The record stays fixed width and the PID (the primary field) is
        # still parseable — truncation must never corrupt the record.
        assert len(rec) == ml._HOLDER_RECORD_LEN
        pid, machine, corrupt = ml._decode_holder_record(rec)
        assert pid == 7
        assert corrupt is False
        assert machine is not None and machine.startswith("z")

    def test_legacy_pure_pid_record_decodes_machine_none(self) -> None:
        # 16-digit zero-padded PID + newline == the pre-upgrade 17-byte record.
        legacy = f"{4242:016d}\n".encode("utf-8")
        assert len(legacy) == ml._PID_LINE_LEN == 17
        assert ml._decode_holder_record(legacy) == (4242, None, False)

    def test_non_numeric_first_line_is_corrupt(self) -> None:
        pid, machine, corrupt = ml._decode_holder_record(b"not-a-number\nhost-x\n")
        assert pid is None
        assert corrupt is True

    def test_reader_caps_read_at_record_len(self, tmp_project: Path) -> None:
        # A reader must never read past _HOLDER_RECORD_LEN, so trailing junk
        # from a longer legacy record cannot poison the parse.
        path = _lock_path(tmp_project)
        rec = ml._encode_holder_record(321, "host-q")
        path.write_bytes(rec + b"TRAILING-GARBAGE-THAT-MUST-BE-IGNORED" * 4)
        probe = MergeLock(tmp_project)
        assert probe._read_holder_pid() == 321
        assert probe._last_read_machine_id == "host-q"


# --------------------------------------------------------------------------
# (a) Foreign-machine holder is never stale / never broken
# --------------------------------------------------------------------------

class TestForeignHolderNeverStale:
    def _spawn_foreign_holder(self, path: Path) -> subprocess.Popen:
        """Hold the flock in a subprocess with a forged foreign-machine record.

        The record carries a locally-dead PID so that, absent the machine
        guard, it would be misjudged stale and broken.
        """
        record = ml._encode_holder_record(_DEAD_PID, _FOREIGN_MACHINE)
        script = f"""
import fcntl, os, time
fd = os.open({str(path)!r}, os.O_RDWR | os.O_CREAT)
fcntl.flock(fd, fcntl.LOCK_EX)
os.ftruncate(fd, 0)
os.write(fd, {record!r})
os.fsync(fd)
print("HOLDING", flush=True)
time.sleep(5)
"""
        holder = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        assert holder.stdout is not None
        assert "HOLDING" in holder.stdout.readline()
        return holder

    def test_foreign_holder_raises_busy_not_stale_and_not_broken(
        self, tmp_project: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = _lock_path(tmp_project)
        holder = self._spawn_foreign_holder(path)
        try:
            before = path.read_bytes()

            # Spy: the stale-break path must never run for a foreign holder.
            broke = {"called": False}
            orig = MergeLock._try_break_stale_and_acquire

            def _spy(self, *a, **k):  # pragma: no cover - must not be hit
                broke["called"] = True
                return orig(self, *a, **k)

            monkeypatch.setattr(
                MergeLock, "_try_break_stale_and_acquire", _spy
            )

            lock = MergeLock(tmp_project)
            # break_stale=True is the strongest case: even when the caller
            # explicitly permits breaking, a foreign holder is untouchable.
            with pytest.raises(MergeLockBusy) as exc_info:
                lock.acquire(break_stale=True)

            assert broke["called"] is False, "foreign lock was force-broken"
            assert exc_info.value.holder_machine == _FOREIGN_MACHINE
            assert _FOREIGN_MACHINE in str(exc_info.value)
            # The lock file is left byte-for-byte intact (never unlinked).
            assert path.exists()
            assert path.read_bytes() == before
        finally:
            holder.terminate()
            holder.wait(timeout=5)

    def test_foreign_holder_raises_busy_even_without_break_stale(
        self, tmp_project: Path
    ) -> None:
        path = _lock_path(tmp_project)
        holder = self._spawn_foreign_holder(path)
        try:
            lock = MergeLock(tmp_project)
            # Non-break path must ALSO surface busy (never MergeLockStale),
            # so _ensure_main_lock_for_step queues instead of breaking.
            with pytest.raises(MergeLockBusy) as exc_info:
                lock.acquire(break_stale=False)
            assert exc_info.value.holder_machine == _FOREIGN_MACHINE
        finally:
            holder.terminate()
            holder.wait(timeout=5)

    def test_inspect_lock_reports_foreign_holder_alive_not_stale(
        self, tmp_project: Path
    ) -> None:
        path = _lock_path(tmp_project)
        # No flock held — inspect only reads the record. A foreign holder must
        # still be reported alive / not stale so auto-clean leaves it intact.
        path.write_bytes(ml._encode_holder_record(_DEAD_PID, _FOREIGN_MACHINE))
        status = inspect_lock(tmp_project)
        assert status.exists is True
        assert status.holder_machine == _FOREIGN_MACHINE
        assert status.alive is True
        assert status.stale is False


# --------------------------------------------------------------------------
# (c-lock) Legacy no-machine record is treated as local
# --------------------------------------------------------------------------

class TestLegacyRecordIsLocal:
    def test_legacy_dead_pid_still_raises_stale(self, tmp_project: Path) -> None:
        path = _lock_path(tmp_project)
        # Pre-upgrade record: bare PID, no machine field, nothing holds flock.
        path.write_text("999999\n", encoding="utf-8")

        lock = MergeLock(tmp_project)
        with pytest.raises(MergeLockStale) as exc_info:
            lock.acquire()
        assert exc_info.value.holder_pid == _DEAD_PID
        # No machine id recorded → the legacy holder is treated as local.
        assert exc_info.value.holder_machine is None

    def test_legacy_dead_pid_reclaimed_with_break_stale(
        self, tmp_project: Path
    ) -> None:
        path = _lock_path(tmp_project)
        path.write_text("999999\n", encoding="utf-8")

        lock = MergeLock(tmp_project)
        lock.acquire(break_stale=True)
        try:
            assert lock._fd is not None
            # Our own machine-aware record replaced the legacy one.
            assert lock._read_holder_pid() == os.getpid()
            assert lock._last_read_machine_id == stable_machine_id()
        finally:
            lock.release()

    def test_inspect_lock_legacy_dead_pid_is_stale(
        self, tmp_project: Path
    ) -> None:
        path = _lock_path(tmp_project)
        path.write_text("999999\n", encoding="utf-8")
        status = inspect_lock(tmp_project)
        assert status.holder_machine is None
        assert status.alive is False
        assert status.stale is True


# --------------------------------------------------------------------------
# (d) Same-machine stale lock recovery is unaffected
# --------------------------------------------------------------------------

class TestSameMachineStaleRecovery:
    def test_local_dead_pid_record_raises_stale(self, tmp_project: Path) -> None:
        path = _lock_path(tmp_project)
        # Machine-aware record for THIS machine but a dead PID.
        path.write_bytes(ml._encode_holder_record(_DEAD_PID, stable_machine_id()))

        lock = MergeLock(tmp_project)
        with pytest.raises(MergeLockStale) as exc_info:
            lock.acquire()
        assert exc_info.value.holder_pid == _DEAD_PID

    def test_local_dead_pid_reclaimed_with_break_stale(
        self, tmp_project: Path
    ) -> None:
        path = _lock_path(tmp_project)
        path.write_bytes(ml._encode_holder_record(_DEAD_PID, stable_machine_id()))

        lock = MergeLock(tmp_project)
        lock.acquire(break_stale=True)
        try:
            assert lock._fd is not None
            assert lock._read_holder_pid() == os.getpid()
        finally:
            lock.release()

    def test_local_live_holder_across_processes_is_busy(
        self, tmp_project: Path
    ) -> None:
        """A live same-machine holder is busy (existing behaviour intact)."""
        script = f"""
import sys, time
sys.path.insert(0, {_SRC_ROOT!r})
from tianluo.commands.merge.merge_lock import MergeLock
lock = MergeLock({str(tmp_project)!r})
lock.acquire()
print("HOLDING", flush=True)
time.sleep(2.0)
lock.release()
"""
        holder = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        try:
            assert holder.stdout is not None
            assert "HOLDING" in holder.stdout.readline()
            lock = MergeLock(tmp_project)
            with pytest.raises(MergeLockBusy) as exc_info:
                lock.acquire(break_stale=True)
            # A live local holder: busy, and its recorded machine is local.
            assert exc_info.value.holder_machine == stable_machine_id()
        finally:
            holder.wait(timeout=10)
