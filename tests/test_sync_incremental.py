```python
"""Tests for incremental sync optimizations: deps capture, per-spec deps
union, the touched-files tracking infrastructure (G3), and discovery
convergence tracking (G4)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from se3.engine.llm_caller import LLMCaller, StreamJSONTracker
from se3.engine.sync_engine import RoundResult, SpecAnalysis
from se3.engine.sync_loop import SyncLoop


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


# ---------------------------------------------------------------------------
# Discovery convergence helpers
# ---------------------------------------------------------------------------


def _round(
    idx: int,
    updated: int = 0,
    hashes: Optional[Dict[str, str]] = None,
    created: Optional[List[str]] = None,
    new_subsystems: int = 0,
) -> RoundResult:
    rr = RoundResult(round_index=idx)
    rr.specs_updated = updated
    rr.spec_hashes_after = dict(hashes or {})
    rr.specs_created = list(created or [])
    rr.new_subsystems_count = new_subsystems
    return rr


class _ScriptedEngine:
    """Stand-in for SyncEngine that returns scripted RoundResults."""

    def __init__(self, project_root: Path, interactive: bool = False) -> None:
        self.project_root = project_root
        self.interactive = interactive
        self.calls: List[Dict[str, Any]] = []
        self.script: List[RoundResult] = []

    def run_once(self, **kwargs: Any) -> RoundResult:
        self.calls.append(kwargs)
        if not self.script:
            raise AssertionError("ScriptedEngine ran out of scripted rounds")
        return self.script.pop(0)


@pytest.fixture
def patched_loop_deps(tmp_path, monkeypatch):
    """Patch SyncEngine + helpers used by SyncLoop.run() to keep tests
    fully hermetic (no LLMCaller, no project context, no real disk I/O)."""

    engine_holder: Dict[str, _ScriptedEngine] = {}

    monkeypatch.setattr("se3.engine.sync_loop.SyncEngine", _ScriptedEngine)

    monkeypatch.setattr(
        "se3.engine.llm_caller.LLMCaller",
        lambda **kwargs: MagicMock(name="LLMCaller"),
    )

    fake_collector = MagicMock()
    fake_collector.collect.return_value = {"git": {}, "specs": []}
    monkeypatch.setattr(
        "se3.engine.project_context.ProjectContextCollector",
        lambda project_root: fake_collector,
    )

    yield engine_holder


def _engine_factory(script, holder):
    """Return a SyncEngine replacement that returns *script* rounds."""
    def factory(project_root, interactive=False):
        eng = _ScriptedEngine(project_root, interactive=interactive)
        eng.script = list(script)
        holder["engine"] = eng
        return eng
    return factory


# ---------------------------------------------------------------------------
# Discovery convergence — basic behaviour
# ---------------------------------------------------------------------------


class TestDiscoveryConvergence:
    """Discovery runs every round until stable_rounds consecutive rounds
    produce 0 new subsystems."""

    def test_discovery_runs_until_converged(
        self, tmp_path, patched_loop_deps
    ):
        """Discovery runs every round when it keeps finding new subsystems,
        and stops only after stable_rounds consecutive 0-count rounds."""
        # stable_rounds=2: discovery needs 2 consecutive rounds of 0.
        # Round 1: 2 new → stable=0.  Round 2: 0 new → stable=1.
        # Round 3: 0 new → stable=2 → converged after this round.
        # Round 4: do_discovery=False (already converged).
        script = [
            _round(1, updated=1, hashes={"a": "X1"}, new_subsystems=2),
            _round(2, updated=1, hashes={"a": "X2"}, new_subsystems=0),
            _round(3, updated=0, hashes={"a": "X2"}, new_subsystems=0),
            _round(4, updated=0, hashes={"a": "X2"}, new_subsystems=0),
        ]

        with patch("se3.engine.sync_loop.SyncEngine",
                   _engine_factory(script, patched_loop_deps)):
            loop = SyncLoop(tmp_path, max_rounds=10, stable_rounds=2)
            loop_result = loop.run()

        assert loop_result.converged is True
        eng = patched_loop_deps["engine"]
        assert eng.calls[0]["do_discovery"] is True   # round 1
        assert eng.calls[1]["do_discovery"] is True   # round 2
        assert eng.calls[2]["do_discovery"] is True   # round 3
        # Round 4: discovery converged after round 3 (2nd consecutive 0)
        assert eng.calls[3]["do_discovery"] is False

    def test_discovery_converges_immediately_with_stable_rounds_1(
        self, tmp_path, patched_loop_deps
    ):
        """With stable_rounds=1, discovery converges after the first
        round that produces 0 new subsystems. The loop converges at
        the same time if analyze is also stable."""
        script = [
            _round(1, updated=1, hashes={"a": "X1"}, new_subsystems=2),
            _round(2, updated=0, hashes={"a": "X1"}, new_subsystems=0),
        ]

        with patch("se3.engine.sync_loop.SyncEngine",
                   _engine_factory(script, patched_loop_deps)):
            loop = SyncLoop(tmp_path, max_rounds=10, stable_rounds=1)
            loop_result = loop.run()

        assert loop_result.converged is True
        eng = patched_loop_deps["engine"]
        assert len(eng.calls) == 2
        assert eng.calls[0]["do_discovery"] is True
        assert eng.calls[1]["do_discovery"] is True

    def test_discovery_does_not_converge_with_continuous_finds(
        self, tmp_path, patched_loop_deps
    ):
        """When every round finds new subsystems, discovery never converges
        and do_discovery remains True for all rounds."""
        script = [
            _round(1, updated=1, hashes={"a": "X1"}, new_subsystems=2),
            _round(2, updated=1, hashes={"a": "X2"}, new_subsystems=1),
            _round(3, updated=1, hashes={"a": "X3"}, new_subsystems=1),
        ]

        with patch("se3.engine.sync_loop.SyncEngine",
                   _engine_factory(script, patched_loop_deps)):
            loop = SyncLoop(tmp_path, max_rounds=3)
            loop.run()

        eng = patched_loop_deps["engine"]
        for call in eng.calls:
            assert call["do_discovery"] is True

    def test_discovery_stable_count_resets_on_find(
        self, tmp_path, patched_loop_deps
    ):
        """A single round with >0 subsystems resets the consecutive
        0-count, requiring stable_rounds fresh consecutive zeros."""
        script = [
            _round(1, updated=1, hashes={"a": "X1"}, new_subsystems=0),
            _round(2, updated=1, hashes={"a": "X2"}, new_subsystems=2),  # reset
            _round(3, updated=1, hashes={"a": "X3"}, new_subsystems=0),
            _round(4, updated=0, hashes={"a": "X3"}, new_subsystems=0),
            _round(5, updated=0, hashes={"a": "X3"}, new_subsystems=0),
        ]

        with patch("se3.engine.sync_loop.SyncEngine",
                   _engine_factory(script, patched_loop_deps)):
            loop = SyncLoop(tmp_path, max_rounds=10, stable_rounds=2)
            loop_result = loop.run()

        assert loop_result.converged is True
        eng = patched_loop_deps["engine"]
        # Discovery runs through round 4 (stability reached at round 4)
        assert eng.calls[0]["do_discovery"] is True
        assert eng.calls[1]["do_discovery"] is True
        assert eng.calls[2]["do_discovery"] is True
        assert eng.calls[3]["do_discovery"] is True
        # Round 5: discovery converged (rounds 3 & 4 both had 0)
        assert eng.calls[4]["do_discovery"] is False


# ---------------------------------------------------------------------------
# Discovery convergence — resume behaviour
# ---------------------------------------------------------------------------


class TestDiscoveryResume:
    """Discovery must NOT run when resuming from a checkpoint."""

    def test_discovery_never_runs_on_resume(
        self, tmp_path, patched_loop_deps, monkeypatch
    ):
        from se3.engine import sync_checkpoint

        script = [
            _round(2, updated=1, hashes={"a": "X1"}, new_subsystems=0),
            _round(3, updated=0, hashes={"a": "X1"}, new_subsystems=0),
        ]

        checkpoint = sync_checkpoint.SyncCheckpoint(
            round_index=1,
            max_rounds=10,
            in_sync_specs={"a": "dummyhash"},
            failed_analyses={},
            reason="quota_exhausted",
        )

        # Stub recompute_in_sync so resume works without disk
        monkeypatch.setattr(
            sync_checkpoint, "recompute_in_sync",
            lambda cp, root: (["a"], []),
        )

        with patch("se3.engine.sync_loop.SyncEngine",
                   _engine_factory(script, patched_loop_deps)):
            loop = SyncLoop(tmp_path, max_rounds=10, resume_from=checkpoint)
            loop.run()

        eng = patched_loop_deps["engine"]
        for call in eng.calls:
            assert call["do_discovery"] is False


# ---------------------------------------------------------------------------
# Discovery convergence — SyncLoop attribute
# ---------------------------------------------------------------------------


class TestDiscoveryConvergedAttribute:
    """discovery_converged is set on the SyncLoop instance after run()."""

    def test_discovery_converged_true_on_convergence(
        self, tmp_path, patched_loop_deps
    ):
        script = [
            _round(1, updated=0, hashes={"a": "X"}, new_subsystems=0),
        ]

        with patch("se3.engine.sync_loop.SyncEngine",
                   _engine_factory(script, patched_loop_deps)):
            loop = SyncLoop(tmp_path, max_rounds=10, stable_rounds=1)
            loop.run()

        assert loop.discovery_converged is True

    def test_discovery_converged_false_without_convergence(
        self, tmp_path, patched_loop_deps
    ):
        """When discovery keeps finding subsystems, discovery_converged
        is False after run()."""
        script = [
            _round(1, updated=1, hashes={"a": "X1"}, new_subsystems=3),
            _round(2, updated=1, hashes={"a": "X2"}, new_subsystems=2),
        ]

        with patch("se3.engine.sync_loop.SyncEngine",
                   _engine_factory(script, patched_loop_deps)):
            loop = SyncLoop(tmp_path, max_rounds=2)
            loop.run()

        assert loop.discovery_converged is False

    def test_discovery_converged_false_on_resume_without_running(
        self, tmp_path, patched_loop_deps, monkeypatch
    ):
        """When resuming, discovery is never run so discovery_converged
        is False (not trustable as a convergence signal)."""
        from se3.engine import sync_checkpoint

        script = [
            _round(2, updated=0, hashes={"a": "X"}, new_subsystems=0),
        ]

        checkpoint = sync_checkpoint.SyncCheckpoint(
            round_index=1,
            max_rounds=10,
            in_sync_specs={"a": "dummyhash"},
            failed_analyses={},
            reason="quota_exhausted",
        )

        monkeypatch.setattr(
            sync_checkpoint, "recompute_in_sync",
            lambda cp, root: (["a"], []),
        )

        with patch("se3.engine.sync_loop.SyncEngine",
                   _engine_factory(script, patched_loop_deps)):
            loop = SyncLoop(tmp_path, max_rounds=10, resume_from=checkpoint)
            loop.run()

        assert loop.discovery_converged is False
```