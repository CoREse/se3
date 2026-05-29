"""Tests for batched/throttled fsync in the merge LLM trace writer, plus
the leaf-merge target-ref validation guard.

The fsync tests assert that the normal append path no longer fsyncs every
single record (the source of the parallel-DAG writeback stall) while still
forcing fsync at every stop()/__exit__/rotation point so no tail record is
ever lost, and that the jsonl stream stays complete and readable regardless.

The leaf-merge test asserts that a missing target branch ref fails
diagnosably (returns False) rather than reaching ``git merge`` and emitting
the opaque ``"not something we can merge"`` error.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import pytest

import se3.commands.merge.llm_trace as trace_mod
from se3.commands.merge.llm_trace import LLMTrace


# ---------------------------------------------------------------------------
# fsync batching / throttling
# ---------------------------------------------------------------------------


@pytest.fixture
def fsync_counter(monkeypatch) -> list[int]:
    """Patch ``os.fsync`` in the llm_trace module to count invocations.

    The real fsync is replaced with a no-op recorder so the test does not
    depend on actual disk durability — only on *when* the writer chooses
    to fsync.
    """
    calls: list[int] = []
    monkeypatch.setattr(trace_mod.os, "fsync", lambda fd: calls.append(fd))
    return calls


class TestFsyncThrottling:
    def test_no_fsync_under_thresholds(self, tmp_path: Path, fsync_counter):
        """Below both the count and time thresholds, no fsync fires while
        records are still being written."""
        trace = LLMTrace(tmp_path, fsync_every_n=10, fsync_interval_sec=3600)
        trace.start()
        for i in range(9):
            trace.record(agent="a", prompt=f"p{i}")
        # Under the every-N threshold and far under the interval: no
        # per-record fsync (the old behaviour would have fsync'd 9 times).
        assert fsync_counter == []
        trace.stop()

    def test_fsync_fires_every_n_records(self, tmp_path: Path, fsync_counter):
        trace = LLMTrace(tmp_path, fsync_every_n=5, fsync_interval_sec=3600)
        trace.start()
        for i in range(4):
            trace.record(agent="a", prompt=f"p{i}")
        assert len(fsync_counter) == 0
        trace.record(agent="a", prompt="p4")  # 5th record -> fsync, counter reset
        assert len(fsync_counter) == 1
        for i in range(4):
            trace.record(agent="a", prompt=f"q{i}")
        assert len(fsync_counter) == 1  # only 4 since reset, not yet
        trace.record(agent="a", prompt="q4")  # 10th overall -> 2nd fsync
        assert len(fsync_counter) == 2
        trace.stop()

    def test_per_record_fsync_when_every_n_nonpositive(
        self, tmp_path: Path, fsync_counter
    ):
        """``fsync_every_n <= 0`` restores per-record fsync semantics."""
        trace = LLMTrace(tmp_path, fsync_every_n=0, fsync_interval_sec=3600)
        trace.start()
        for i in range(3):
            trace.record(agent="a", prompt=f"p{i}")
        assert len(fsync_counter) == 3
        trace.stop()

    def test_stop_forces_tail_fsync(self, tmp_path: Path, fsync_counter):
        """stop()/__exit__ must fsync the tail even when the batch threshold
        was never reached, so the trailing records are durable."""
        trace = LLMTrace(tmp_path, fsync_every_n=1000, fsync_interval_sec=3600)
        with trace:
            trace.record(agent="a", prompt="only-one")
            assert fsync_counter == []  # threshold not reached mid-run
        # __exit__ -> stop -> _close_file -> _fsync_now
        assert len(fsync_counter) >= 1

    def test_rotation_forces_fsync(self, tmp_path: Path, fsync_counter):
        """A file rotation closes the old file, which must fsync its tail."""
        trace = LLMTrace(
            tmp_path, max_file_bytes=200,
            fsync_every_n=1000, fsync_interval_sec=3600,
        )
        with trace:
            trace.record(agent="a", prompt="x" * 300)
            first_path = trace._current_path
            assert fsync_counter == []  # batch threshold not hit
            trace.record(agent="a", prompt="y" * 300)  # triggers rotation
            assert len(fsync_counter) >= 1  # old file fsync'd on close
        # The rotated-out file is complete and readable.
        lines = first_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["prompt_preview"].startswith("x")


class TestRecordsRemainComplete:
    def test_no_tail_loss_with_batched_fsync(self, tmp_path: Path):
        """Even when fsync is throttled away entirely until close, every
        record is flushed and readable (append-only semantics preserved)."""
        trace = LLMTrace(tmp_path, fsync_every_n=10000, fsync_interval_sec=3600)
        with trace:
            for i in range(50):
                trace.record(
                    agent="a", prompt=f"prompt-{i}", response=f"resp-{i}",
                    outcome="success",
                )
        lines = trace._current_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 50
        seqs = [json.loads(line)["seq"] for line in lines]
        assert seqs == list(range(1, 51))
        # Spot-check the tail record is intact (not truncated).
        last = json.loads(lines[-1])
        assert last["prompt_preview"] == "prompt-49"
        assert last["response_preview"] == "resp-49"

    def test_records_readable_mid_run_before_fsync(self, tmp_path: Path):
        """A reader can see records before any fsync because each record is
        flushed to the OS page cache."""
        trace = LLMTrace(tmp_path, fsync_every_n=10000, fsync_interval_sec=3600)
        trace.start()
        trace.record(agent="a", prompt="early", response="visible")
        # No fsync has happened yet, but the flushed line is readable.
        content = trace._current_path.read_text(encoding="utf-8")
        assert "early" in content
        trace.stop()


# ---------------------------------------------------------------------------
# leaf-merge target ref validation
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=check,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "--initial-branch=main")
    _git(r, "config", "user.email", "test@example.com")
    _git(r, "config", "user.name", "Test")
    (r / "README.md").write_text("hello\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "init")
    return r


class TestLeafMergeRefValidation:
    def test_missing_ref_fails_safely(self, repo: Path, caplog):
        """A non-existent leaf branch ref returns False before any merge is
        attempted, with a diagnosable log — not the opaque
        'not something we can merge' error, and no in-progress merge state."""
        from se3.engine.steps.implement import _attempt_merge_with_resolution

        with caplog.at_level(logging.ERROR):
            ok = _attempt_merge_with_resolution(
                repo,
                branch="impl/does-not-exist/G9",
                task_description="t",
                group_summaries=[],
                spec_content="",
                flow_id=None,
                merge_step_id=None,
            )

        assert ok is False
        # No merge was started, so no MERGE_HEAD leftover.
        assert not (repo / ".git" / "MERGE_HEAD").exists()
        # Diagnosable error naming the missing ref / the rev-parse failure.
        assert "does not resolve to a commit" in caplog.text

    def test_existing_ref_proceeds_to_merge(self, repo: Path):
        """When the ref exists, the guard passes and a clean merge succeeds."""
        from se3.engine.steps.implement import _attempt_merge_with_resolution

        # Create a leaf branch with a non-conflicting new file.
        _git(repo, "checkout", "-b", "impl/real/G1", "main")
        (repo / "feature.txt").write_text("feature\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "feature on leaf")
        _git(repo, "checkout", "main")

        ok = _attempt_merge_with_resolution(
            repo,
            branch="impl/real/G1",
            task_description="t",
            group_summaries=[],
            spec_content="",
            flow_id=None,
            merge_step_id=None,
        )

        assert ok is True
        assert (repo / "feature.txt").exists()
