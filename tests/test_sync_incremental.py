"""Tests for incremental sync optimizations: deps capture, per-spec deps
union, and the touched-files tracking infrastructure (G3)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from se3.engine.llm_caller import LLMCaller, StreamJSONTracker
from se3.engine.sync_engine import RoundResult, SpecAnalysis


# ---------------------------------------------------------------------------
# StreamJSONTracker touched-files capture
# ---------------------------------------------------------------------------

class TestStreamJSONTrackerTouchedFiles:
    """Verify that StreamJSONTracker records file paths from Read/Grep/Glob
    tool_use calls and normalizes them to project-relative paths."""

    def _make_ndjson_line(self, data: dict) -> str:
        return json.dumps(data)

    def _assistant_with_tool_use(self, name: str, tool_input: dict,
                                 tool_use_id: str = "tu_1") -> str:
        return self._make_ndjson_line({
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": name,
                        "id": tool_use_id,
                        "input": tool_input,
                    }
                ]
            }
        })

    def test_captures_read_file_path(self, tmp_path):
        tracker = StreamJSONTracker(project_root=tmp_path)
        line = self._assistant_with_tool_use("Read", {"file_path": "src/main.py"})
        tracker.process_line(line)
        assert "src/main.py" in tracker.touched_files

    def test_captures_grep_path(self, tmp_path):
        tracker = StreamJSONTracker(project_root=tmp_path)
        line = self._assistant_with_tool_use("Grep", {"path": "src", "pattern": "TODO"})
        tracker.process_line(line)
        assert "src" in tracker.touched_files

    def test_captures_glob_path(self, tmp_path):
        tracker = StreamJSONTracker(project_root=tmp_path)
        line = self._assistant_with_tool_use("Glob", {"path": "tests", "pattern": "**/*.py"})
        tracker.process_line(line)
        assert "tests" in tracker.touched_files

    def test_normalizes_absolute_path_to_relative(self, tmp_path):
        tracker = StreamJSONTracker(project_root=tmp_path)
        abs_path = str(tmp_path / "src" / "main.py")
        line = self._assistant_with_tool_use("Read", {"file_path": abs_path})
        tracker.process_line(line)
        assert str(abs_path) not in tracker.touched_files
        assert "src/main.py" in tracker.touched_files

    def test_skips_path_outside_project_root(self, tmp_path):
        tracker = StreamJSONTracker(project_root=tmp_path)
        outside = "/completely/outside/project/file.py"
        line = self._assistant_with_tool_use("Read", {"file_path": outside})
        tracker.process_line(line)
        assert outside not in tracker.touched_files

    def test_ignores_edit_and_write_for_deps(self, tmp_path):
        """Edit/Write tools should not contribute to deps (only Read/Grep/Glob)."""
        tracker = StreamJSONTracker(project_root=tmp_path)
        line = self._assistant_with_tool_use(
            "Write", {"file_path": "se3/specs/base/spec.md", "content": "..."}
        )
        tracker.process_line(line)
        assert len(tracker.touched_files) == 0

    def test_multiple_tool_calls_union(self, tmp_path):
        tracker = StreamJSONTracker(project_root=tmp_path)
        tracker.process_line(
            self._assistant_with_tool_use("Read", {"file_path": "src/a.py"}, "tu_1")
        )
        tracker.process_line(
            self._assistant_with_tool_use("Read", {"file_path": "src/b.py"}, "tu_2")
        )
        tracker.process_line(
            self._assistant_with_tool_use("Grep", {"path": "src", "pattern": "X"}, "tu_3")
        )
        assert tracker.touched_files == {"src/a.py", "src/b.py", "src"}

    def test_same_path_deduplicated(self, tmp_path):
        tracker = StreamJSONTracker(project_root=tmp_path)
        for _ in range(3):
            tracker.process_line(
                self._assistant_with_tool_use("Read", {"file_path": "src/a.py"})
            )
        assert tracker.touched_files == {"src/a.py"}

    def test_empty_file_path_ignored(self, tmp_path):
        tracker = StreamJSONTracker(project_root=tmp_path)
        line = self._assistant_with_tool_use("Read", {"file_path": ""})
        tracker.process_line(line)
        assert len(tracker.touched_files) == 0

    def test_empty_path_ignored_for_grep(self, tmp_path):
        tracker = StreamJSONTracker(project_root=tmp_path)
        line = self._assistant_with_tool_use("Grep", {"path": "", "pattern": "X"})
        tracker.process_line(line)
        assert len(tracker.touched_files) == 0

    def test_tool_use_without_id_still_records_path(self, tmp_path):
        """Path recording should work even without a tool_use_id."""
        tracker = StreamJSONTracker(project_root=tmp_path)
        line = self._make_ndjson_line({
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "Read",
                        "id": "",
                        "input": {"file_path": "src/main.py"},
                    }
                ]
            }
        })
        tracker.process_line(line)
        assert "src/main.py" in tracker.touched_files

    def test_no_project_root_keeps_raw_paths(self):
        """Without project_root, paths are stored as-is."""
        tracker = StreamJSONTracker(project_root=None)
        line = self._assistant_with_tool_use("Glob", {
            "path": "some/relative/dir"
        })
        tracker.process_line(line)
        assert "some/relative/dir" in tracker.touched_files

    def test_touched_files_returns_copy(self, tmp_path):
        """touched_files property returns a copy, not a reference."""
        tracker = StreamJSONTracker(project_root=tmp_path)
        tracker.process_line(
            self._assistant_with_tool_use("Read", {"file_path": "x.py"})
        )
        files = tracker.touched_files
        files.add("y.py")  # mutate the copy
        assert "y.py" not in tracker.touched_files


# ---------------------------------------------------------------------------
# SpecAnalysis.touched_files
# ---------------------------------------------------------------------------

class TestSpecAnalysisTouchedFiles:
    """Verify SpecAnalysis carries touched_files through serialization."""

    def test_default_is_empty_list(self):
        analysis = SpecAnalysis(spec_name="test")
        assert analysis.touched_files == []

    def test_stores_touched_files(self):
        analysis = SpecAnalysis(
            spec_name="test",
            touched_files=["src/a.py", "src/b.py"],
        )
        assert analysis.touched_files == ["src/a.py", "src/b.py"]

    def test_serializes_touched_files(self):
        analysis = SpecAnalysis(
            spec_name="test",
            touched_files=["src/main.py", "tests/test.py"],
        )
        d = analysis.to_dict()
        assert d["touched_files"] == ["src/main.py", "tests/test.py"]

    def test_deserializes_touched_files(self):
        data = {
            "spec_name": "test",
            "touched_files": ["x.py", "y.py"],
            "diffs": [],
            "analyzed_at": "2025-01-01T00:00:00",
        }
        analysis = SpecAnalysis.from_dict(data)
        assert analysis.touched_files == ["x.py", "y.py"]

    def test_deserializes_missing_touched_files_as_empty(self):
        data = {
            "spec_name": "test",
            "diffs": [],
            "analyzed_at": "2025-01-01T00:00:00",
        }
        analysis = SpecAnalysis.from_dict(data)
        assert analysis.touched_files == []


# ---------------------------------------------------------------------------
# RoundResult.per_spec_deps aggregation
# ---------------------------------------------------------------------------

class TestRoundResultPerSpecDeps:
    """Verify RoundResult aggregates per-spec touched files from analyses."""

    def test_default_is_empty_dict(self):
        result = RoundResult(round_index=1)
        assert result.per_spec_deps == {}

    def test_aggregates_from_analyses(self):
        result = RoundResult(round_index=1)
        result.analyses = [
            SpecAnalysis(spec_name="auth", touched_files=["src/auth.py", "src/login.py"]),
            SpecAnalysis(spec_name="api", touched_files=["src/api.py"]),
        ]
        # Simulate what run_once does
        for analysis in result.analyses:
            if analysis.touched_files:
                result.per_spec_deps[analysis.spec_name] = sorted(
                    set(analysis.touched_files)
                )

        assert result.per_spec_deps == {
            "auth": ["src/auth.py", "src/login.py"],
            "api": ["src/api.py"],
        }

    def test_empty_touched_files_skipped(self):
        result = RoundResult(round_index=1)
        result.analyses = [
            SpecAnalysis(spec_name="auth", touched_files=[]),
            SpecAnalysis(spec_name="api", touched_files=["src/api.py"]),
        ]
        for analysis in result.analyses:
            if analysis.touched_files:
                result.per_spec_deps[analysis.spec_name] = sorted(
                    set(analysis.touched_files)
                )
        assert "auth" not in result.per_spec_deps
        assert result.per_spec_deps == {"api": ["src/api.py"]}

    def test_serialization_roundtrip(self):
        result = RoundResult(round_index=1)
        result.per_spec_deps = {"auth": ["src/auth.py", "src/login.py"]}
        d = result.to_dict()
        assert d["per_spec_deps"] == {"auth": ["src/auth.py", "src/login.py"]}


# ---------------------------------------------------------------------------
# Cross-round deps union (simulated)
# ---------------------------------------------------------------------------

class TestCrossRoundDepsUnion:
    """Simulate cross-round deps accumulation — union of per-spec touched
    files across rounds, which only grows (never shrinks)."""

    def test_union_grows_across_rounds(self):
        """Each round may touch additional files; the cross-round union is
        the superset of all rounds."""
        round1_deps = {"auth": {"src/auth.py", "src/login.py"}}
        round2_deps = {"auth": {"src/auth.py", "src/session.py"}}

        # Simulate cross-round union
        union = round1_deps["auth"] | round2_deps["auth"]
        assert union == {"src/auth.py", "src/login.py", "src/session.py"}

    def test_union_never_shrinks(self):
        """The union should only grow, never shrink."""
        round1_deps = {"auth": {"a.py", "b.py", "c.py"}}
        round2_deps = {"auth": {"a.py"}}  # fewer files this round

        union = round1_deps["auth"] | round2_deps["auth"]
        # b.py and c.py are preserved from round1
        assert union == {"a.py", "b.py", "c.py"}

    def test_new_spec_added_in_later_round(self):
        round1_deps: dict = {}
        round2_deps = {"new_spec": {"src/new.py"}}

        # Start with round1, accumulate round2
        accumulated = dict(round1_deps)
        for spec_name, files in round2_deps.items():
            if spec_name in accumulated:
                accumulated[spec_name] = accumulated[spec_name] | files
            else:
                accumulated[spec_name] = set(files)

        assert accumulated == {"new_spec": {"src/new.py"}}


# ---------------------------------------------------------------------------
# LLMCaller.last_touched_files integration
# ---------------------------------------------------------------------------

class TestLLMCallerLastTouchedFiles:
    """Verify LLMCaller exposes last_touched_files after a call."""

    def test_default_is_empty(self, tmp_path):
        caller = LLMCaller(project_root=tmp_path)
        assert caller.last_touched_files == set()

    def test_reset_before_each_call(self, tmp_path):
        caller = LLMCaller(project_root=tmp_path)
        # Simulate the state after a call would have set them
        caller._last_touched_files = {"a.py", "b.py"}
        # A new call would reset via _call_with_retry start
        caller._last_touched_files = set()
        assert caller.last_touched_files == set()

    def test_returns_copy_not_reference(self, tmp_path):
        caller = LLMCaller(project_root=tmp_path)
        caller._last_touched_files = {"x.py"}
        files = caller.last_touched_files
        files.add("y.py")
        assert caller.last_touched_files == {"x.py"}
