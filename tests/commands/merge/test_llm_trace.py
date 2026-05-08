"""Tests for LLMTrace per-call jsonl logging."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from se3.commands.merge.llm_trace import LLMCallRecord, LLMTrace


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    return tmp_path


class TestLLMTraceLifecycle:
    """Start / stop / context-manager."""

    def test_start_creates_file(self, tmp_project: Path) -> None:
        trace = LLMTrace(tmp_project)
        trace.start()
        assert trace._current_path is not None
        assert trace._current_path.exists()
        trace.stop()

    def test_context_manager(self, tmp_project: Path) -> None:
        trace = LLMTrace(tmp_project)
        with trace:
            assert trace._started is True
            assert trace._file is not None
        assert trace._started is False

    def test_stop_closes_file(self, tmp_project: Path) -> None:
        trace = LLMTrace(tmp_project)
        trace.start()
        trace.stop()
        assert trace._file is None

    def test_idempotent_start(self, tmp_project: Path) -> None:
        trace = LLMTrace(tmp_project)
        trace.start()
        first_path = trace._current_path
        trace.start()  # no-op
        assert trace._current_path == first_path
        trace.stop()


class TestLLMTraceRecord:
    """Writing records and verifying jsonl output."""

    def test_seq_monotonic(self, tmp_project: Path) -> None:
        trace = LLMTrace(tmp_project)
        with trace:
            s1 = trace.record(agent="claude", prompt="hello", response="world")
            s2 = trace.record(agent="claude", prompt="hello2", response="world2")
            s3 = trace.record(agent="claude", prompt="hello3", response="world3")
        assert s1 == 1
        assert s2 == 2
        assert s3 == 3

    def test_record_content(self, tmp_project: Path) -> None:
        trace = LLMTrace(tmp_project)
        with trace:
            trace.record(
                agent="claude-opus",
                prompt="Solve conflict",
                response="Resolved",
                duration_sec=1.5,
                outcome="success",
                meta={"model": "opus-4"},
            )

        lines = trace._current_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["agent"] == "claude-opus"
        assert record["prompt_preview"] == "Solve conflict"
        assert record["response_preview"] == "Resolved"
        assert record["duration_sec"] == 1.5
        assert record["outcome"] == "success"
        assert record["meta"] == {"model": "opus-4"}
        assert record["seq"] == 1
        assert "timestamp" in record

    def test_preview_truncation(self, tmp_project: Path) -> None:
        trace = LLMTrace(tmp_project)
        with trace:
            trace.record(agent="a", prompt="x" * 5000, preview_chars=100)

        lines = trace._current_path.read_text(encoding="utf-8").strip().splitlines()
        record = json.loads(lines[0])
        assert len(record["prompt_preview"]) == 100

    def test_empty_prompt_and_response(self, tmp_project: Path) -> None:
        trace = LLMTrace(tmp_project)
        with trace:
            trace.record(agent="a")

        lines = trace._current_path.read_text(encoding="utf-8").strip().splitlines()
        record = json.loads(lines[0])
        assert record["prompt_preview"] == ""
        assert record["response_preview"] == ""

    def test_auto_start_on_record(self, tmp_project: Path) -> None:
        trace = LLMTrace(tmp_project)
        # Do not call start() explicitly.
        trace.record(agent="a", prompt="hi")
        assert trace._started is True
        trace.stop()


class TestLLMTraceRotation:
    """File rotation when size limit is reached."""

    def test_rotation(self, tmp_project: Path) -> None:
        trace = LLMTrace(tmp_project, max_file_bytes=200)
        with trace:
            trace.record(agent="a", prompt="x" * 300)
            first_path = trace._current_path
            # This should trigger rotation.
            trace.record(agent="a", prompt="y" * 300)

        # After rotation, the current path may be different.
        assert trace._current_path is not None
        # The first file should exist and contain the first record.
        assert first_path.exists()
        lines = first_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["prompt_preview"].startswith("x")

    def test_rotation_filenames_unique_in_same_microsecond(
        self, tmp_project: Path, monkeypatch
    ) -> None:
        """Regression: two rotations in the same microsecond must not
        collide on filename. The original implementation reused the
        record-counter as the filename suffix, so the same ``self._seq``
        could produce identical names if the timestamp (microsecond
        granular) matched. We pin ``datetime.now`` to a fixed instant
        and force three rotations to verify uniqueness.
        """
        import se3.commands.merge.llm_trace as trace_mod

        # Freeze the clock so every _new_file_path() sees the same ts.
        class _FrozenDatetime:
            @classmethod
            def now(cls, tz=None):  # noqa: ANN001
                from datetime import datetime as real_dt
                return real_dt(2026, 1, 1, 0, 0, 0, 0, tzinfo=tz)

        monkeypatch.setattr(trace_mod, "datetime", _FrozenDatetime)

        trace = LLMTrace(tmp_project, max_file_bytes=50)
        seen_paths: list[Path] = []
        with trace:
            for _ in range(3):
                trace.record(agent="a", prompt="x" * 100)
                if trace._current_path not in seen_paths:
                    seen_paths.append(trace._current_path)

        assert len(seen_paths) == len(set(seen_paths)), (
            f"rotation produced duplicate filenames: {seen_paths}"
        )


class TestLLMCallRecord:
    """Dataclass serialization."""

    def test_to_dict(self) -> None:
        record = LLMCallRecord(
            seq=1,
            timestamp_iso="2024-01-01T00:00:00",
            agent="claude",
            outcome="success",
        )
        d = record.to_dict()
        assert d["seq"] == 1
        assert d["agent"] == "claude"
        assert d["outcome"] == "success"
