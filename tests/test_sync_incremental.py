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


# ============================================================================
# G5: Three-level skip mechanism & per-spec convergence
# ============================================================================


def _round_with_analyses(
    idx: int,
    spec_names: List[str],
    in_sync: Optional[Dict[str, bool]] = None,
    created: Optional[List[str]] = None,
    new_subsystems: int = 0,
    touched_files: Optional[Dict[str, List[str]]] = None,
    failed: Optional[Dict[str, str]] = None,
) -> RoundResult:
    """Create a RoundResult with SpecAnalysis entries per spec."""
    rr = RoundResult(round_index=idx)
    rr.new_subsystems_count = new_subsystems
    if created:
        rr.specs_created = list(created)
    in_sync_map = in_sync or {}
    touched_map = touched_files or {}
    failed_map = failed or {}
    updated = 0
    for name in spec_names:
        is_sync = in_sync_map.get(name, True)
        tfiles = touched_map.get(name, [])
        reason = failed_map.get(name)
        if reason:
            analysis = SpecAnalysis(
                spec_name=name,
                diffs=[],
                failed_analysis_reason=reason,
                touched_files=tfiles,
            )
        elif is_sync:
            analysis = SpecAnalysis(spec_name=name, diffs=[], touched_files=tfiles)
        else:
            from se3.engine.sync_engine import DiffType, SpecDiff
            diff = SpecDiff(
                diff_type=DiffType.EXTENSION,
                spec_name=name,
                description=f"drift in {name}",
            )
            analysis = SpecAnalysis(
                spec_name=name,
                diffs=[diff],
                touched_files=tfiles,
            )
            updated += 1
        rr.analyses.append(analysis)
        rr.spec_hashes_after[name] = f"hash_{name}_{idx}"
        if tfiles:
            rr.per_spec_deps[name] = sorted(set(tfiles))
    rr.specs_updated = updated
    for name in spec_names:
        rr.changes_by_spec.setdefault(name, []).append(f"round {idx} change")
    return rr


def _write_sync_state_file(root: Path, fp: str, discovery_converged: bool = True,
                           spec_deps: Optional[Dict] = None) -> None:
    """Write a minimal sync_state.json for testing."""
    from se3.engine.sync_state import state_path
    p = state_path(root)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "state_version": 1,
        "converged_at": "2026-01-01T00:00:00Z",
        "code_fingerprint": fp,
        "discovery_converged": discovery_converged,
        "spec_deps": spec_deps or {},
        "obsolete_specs": [],
    }
    p.write_text(json.dumps(data), encoding="utf-8")


class _CallTrackingEngine:
    """Engine stand-in that records calls and returns scripted results."""

    def __init__(self, project_root: Path, interactive: bool = False) -> None:
        self.project_root = project_root
        self.interactive = interactive
        self.calls: List[Dict[str, Any]] = []
        self.script: List[RoundResult] = []
        self._specs: Dict[str, Any] = {}

    def run_once(self, **kwargs: Any) -> RoundResult:
        self.calls.append(kwargs)
        if not self.script:
            raise AssertionError("CallTrackingEngine ran out of scripted rounds")
        return self.script.pop(0)

    def _load_specs(self) -> Dict[str, Any]:
        return dict(self._specs)


# ---------------------------------------------------------------------------
# Level 1: Global shutter
# ---------------------------------------------------------------------------


class TestLevel1GlobalShutter:
    """Level 1: sync_state with matching code_fingerprint → 0 LLM calls."""

    def test_global_shutter_hit_zero_llm_calls(self, tmp_path, monkeypatch):
        """When fingerprint matches and discovery_converged, return immediately."""
        import subprocess

        # Init git so compute_code_fingerprint works
        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.email", "test@test"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=str(tmp_path), check=True)
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("print('hello')")
        subprocess.run(["git", "add", "src/main.py"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(tmp_path), check=True)

        from se3.engine.sync_state import compute_code_fingerprint
        fp = compute_code_fingerprint(tmp_path)

        _write_sync_state_file(tmp_path, fp, discovery_converged=True)

        # Patch SyncEngine to count calls
        engine_holder = {}
        def factory(project_root, interactive=False):
            eng = _CallTrackingEngine(project_root, interactive=interactive)
            engine_holder["engine"] = eng
            return eng

        monkeypatch.setattr("se3.engine.sync_loop.SyncEngine", factory)
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

        loop = SyncLoop(tmp_path, max_rounds=10, stable_rounds=1)
        result = loop.run()

        assert result.converged is True
        eng = engine_holder.get("engine")
        if eng is not None:
            assert len(eng.calls) == 0

    def test_global_shutter_skipped_on_mismatched_fingerprint(
        self, tmp_path, monkeypatch
    ):
        """When fingerprint differs, proceed to normal sync."""
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.email", "test@test"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=str(tmp_path), check=True)

        # Write sync_state with a fingerprint that won't match
        _write_sync_state_file(tmp_path, "deadbeef", discovery_converged=True)

        engine_holder = {}
        def factory(project_root, interactive=False):
            eng = _CallTrackingEngine(project_root, interactive=interactive)
            eng._specs = {
                "spec_a": {
                    "name": "spec_a",
                    "path": str(tmp_path / "se3" / "specs" / "spec_a" / "spec.md"),
                    "content": "# test",
                },
            }
            eng.script = [
                _round_with_analyses(1, ["spec_a"], in_sync={"spec_a": True},
                                     new_subsystems=0),
            ]
            if "engine" not in engine_holder:
                engine_holder["engine"] = eng
            return eng

        monkeypatch.setattr("se3.engine.sync_loop.SyncEngine", factory)
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

        loop = SyncLoop(tmp_path, max_rounds=10, stable_rounds=1)
        result = loop.run()

        # Not via global shutter — engine was called
        eng = engine_holder.get("engine")
        assert eng is not None
        assert len(eng.calls) > 0

    def test_global_shutter_skipped_when_discovery_not_converged(
        self, tmp_path, monkeypatch
    ):
        """When discovery_converged is False, level 1 does NOT trigger."""
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.email", "test@test"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=str(tmp_path), check=True)

        from se3.engine.sync_state import compute_code_fingerprint
        fp = compute_code_fingerprint(tmp_path)

        _write_sync_state_file(tmp_path, fp, discovery_converged=False)

        engine_holder = {}
        def factory(project_root, interactive=False):
            eng = _CallTrackingEngine(project_root, interactive=interactive)
            eng._specs = {
                "spec_a": {
                    "name": "spec_a",
                    "path": str(tmp_path / "se3" / "specs" / "spec_a" / "spec.md"),
                    "content": "# test",
                },
            }
            eng.script = [
                _round_with_analyses(1, ["spec_a"], in_sync={"spec_a": True},
                                     new_subsystems=0),
            ]
            engine_holder["engine"] = eng
            return eng

        monkeypatch.setattr("se3.engine.sync_loop.SyncEngine", factory)
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

        loop = SyncLoop(tmp_path, max_rounds=10, stable_rounds=1)
        result = loop.run()

        eng = engine_holder.get("engine")
        assert eng is not None
        assert len(eng.calls) > 0  # should proceed to normal sync

    def test_force_skips_global_shutter(self, tmp_path, monkeypatch):
        """--force ignores sync_state."""
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.email", "test@test"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=str(tmp_path), check=True)
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("print('hello')")
        subprocess.run(["git", "add", "src/main.py"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(tmp_path), check=True)

        from se3.engine.sync_state import compute_code_fingerprint
        fp = compute_code_fingerprint(tmp_path)
        _write_sync_state_file(tmp_path, fp, discovery_converged=True)

        engine_holder = {}
        def factory(project_root, interactive=False):
            eng = _CallTrackingEngine(project_root, interactive=interactive)
            eng._specs = {
                "spec_a": {
                    "name": "spec_a",
                    "path": str(tmp_path / "se3" / "specs" / "spec_a" / "spec.md"),
                    "content": "# test",
                },
            }
            eng.script = [
                _round_with_analyses(1, ["spec_a"], in_sync={"spec_a": True},
                                     new_subsystems=0),
            ]
            engine_holder["engine"] = eng
            return eng

        monkeypatch.setattr("se3.engine.sync_loop.SyncEngine", factory)
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

        loop = SyncLoop(tmp_path, max_rounds=10, stable_rounds=1, force=True)
        result = loop.run()

        eng = engine_holder.get("engine")
        assert eng is not None
        assert len(eng.calls) > 0  # force should bypass cache


# ---------------------------------------------------------------------------
# Level 2: Per-spec gate
# ---------------------------------------------------------------------------


class TestLevel2PerSpecGate:
    """Level 2: per-spec deps hash match → skip; file-set change invalidates."""

    def _setup_git_repo(self, tmp_path: Path, files: Dict[str, str]) -> str:
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.email", "test@test"],
                       cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.name", "T"],
                       cwd=str(tmp_path), check=True)
        for rel_path, content in files.items():
            full = tmp_path / rel_path
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(content)
        subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"],
                       cwd=str(tmp_path), check=True)
        from se3.engine.sync_state import compute_code_fingerprint
        return compute_code_fingerprint(tmp_path)

    def _make_patched_loop(self, tmp_path, monkeypatch, spec_deps=None,
                           discovery_converged=True, force=False):
        """Set up patched SyncLoop with sync_state."""
        from se3.engine.sync_state import compute_file_content_hash

        fp = self._setup_git_repo(tmp_path, {"src/main.py": "hello",
                                              "src/util.py": "world"})
        _write_sync_state_file(tmp_path, fp, discovery_converged=discovery_converged,
                               spec_deps=spec_deps)

        engine_holder = {}
        spec_a_path = tmp_path / "se3" / "specs" / "spec_a" / "spec.md"
        spec_b_path = tmp_path / "se3" / "specs" / "spec_b" / "spec.md"
        spec_a_path.parent.mkdir(parents=True, exist_ok=True)
        spec_b_path.parent.mkdir(parents=True, exist_ok=True)
        spec_a_path.write_text("# Spec A\n## Purpose\nTest.\n### Requirement: R1\n")
        spec_b_path.write_text("# Spec B\n## Purpose\nTest.\n### Requirement: R1\n")

        def factory(project_root, interactive=False):
            eng = _CallTrackingEngine(project_root, interactive=interactive)
            eng._specs = {
                "spec_a": {"name": "spec_a", "path": str(spec_a_path),
                           "content": spec_a_path.read_text()},
                "spec_b": {"name": "spec_b", "path": str(spec_b_path),
                           "content": spec_b_path.read_text()},
            }
            eng.script = [
                _round_with_analyses(1, ["spec_a", "spec_b"],
                                     in_sync={"spec_a": True, "spec_b": True},
                                     new_subsystems=0),
            ]
            engine_holder["engine"] = eng
            return eng

        monkeypatch.setattr("se3.engine.sync_loop.SyncEngine", factory)
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
        return engine_holder

    def test_spec_skipped_when_all_deps_match(self, tmp_path, monkeypatch):
        """Spec whose spec_hash and all dep file hashes match is skipped."""
        from se3.engine.sync_state import compute_file_content_hash

        # Create the files first, then compute hashes
        h_main = compute_file_content_hash(tmp_path / "src" / "main.py") if (tmp_path / "src" / "main.py").exists() else None

        # We set up git first, then compute hashes
        import subprocess
        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.email", "test@test"],
                       cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.name", "T"],
                       cwd=str(tmp_path), check=True)
        (tmp_path / "src").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "main.py").write_text("hello")
        subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"],
                       cwd=str(tmp_path), check=True)

        from se3.engine.sync_state import compute_file_content_hash
        h_main = compute_file_content_hash(tmp_path / "src" / "main.py")

        spec_deps = {
            "spec_a": {
                "spec_hash": "any",
                "deps": {"src/main.py": h_main},
            },
        }
        _write_sync_state_file(tmp_path, "non-matching-fp", discovery_converged=True,
                               spec_deps=spec_deps)

        spec_a_path = tmp_path / "se3" / "specs" / "spec_a" / "spec.md"
        spec_b_path = tmp_path / "se3" / "specs" / "spec_b" / "spec.md"
        spec_a_path.parent.mkdir(parents=True, exist_ok=True)
        spec_b_path.parent.mkdir(parents=True, exist_ok=True)
        spec_a_path.write_text("# Spec A\n## Purpose\nTest.\n### Requirement: R1\n")
        spec_b_path.write_text("# Spec B\n## Purpose\nTest.\n### Requirement: R1\n")

        engine_holder = {}
        def factory(project_root, interactive=False):
            eng = _CallTrackingEngine(project_root, interactive=interactive)
            eng._specs = {
                "spec_a": {"name": "spec_a", "path": str(spec_a_path),
                           "content": spec_a_path.read_text()},
                "spec_b": {"name": "spec_b", "path": str(spec_b_path),
                           "content": spec_b_path.read_text()},
            }
            eng.script = [
                _round_with_analyses(1, ["spec_a", "spec_b"],
                                     in_sync={"spec_a": True, "spec_b": True},
                                     new_subsystems=0),
            ]
            engine_holder["engine"] = eng
            return eng

        monkeypatch.setattr("se3.engine.sync_loop.SyncEngine", factory)
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

        loop = SyncLoop(tmp_path, max_rounds=10, stable_rounds=1)
        loop.run()

        eng = engine_holder["engine"]
        assert len(eng.calls) == 1
        skip = eng.calls[0].get("skip_specs") or set()
        assert "spec_a" in skip

    def test_spec_not_skipped_when_dep_hash_differs(self, tmp_path, monkeypatch):
        """Spec with a dep file whose content changed is NOT skipped."""
        import subprocess
        from se3.engine.sync_state import compute_code_fingerprint

        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.email", "test@test"],
                       cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.name", "T"],
                       cwd=str(tmp_path), check=True)
        (tmp_path / "src").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "main.py").write_text("hello")
        subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"],
                       cwd=str(tmp_path), check=True)

        spec_deps = {
            "spec_a": {
                "spec_hash": "any",
                "deps": {"src/main.py": "wrong_hash"},
            },
        }
        _write_sync_state_file(tmp_path, "non-matching-fp", discovery_converged=True,
                               spec_deps=spec_deps)

        spec_a_path = tmp_path / "se3" / "specs" / "spec_a" / "spec.md"
        spec_b_path = tmp_path / "se3" / "specs" / "spec_b" / "spec.md"
        spec_a_path.parent.mkdir(parents=True, exist_ok=True)
        spec_b_path.parent.mkdir(parents=True, exist_ok=True)
        spec_a_path.write_text("# Spec A\n## Purpose\nTest.\n### Requirement: R1\n")
        spec_b_path.write_text("# Spec B\n## Purpose\nTest.\n### Requirement: R1\n")

        engine_holder = {}
        def factory(project_root, interactive=False):
            eng = _CallTrackingEngine(project_root, interactive=interactive)
            eng._specs = {
                "spec_a": {"name": "spec_a", "path": str(spec_a_path),
                           "content": spec_a_path.read_text()},
                "spec_b": {"name": "spec_b", "path": str(spec_b_path),
                           "content": spec_b_path.read_text()},
            }
            eng.script = [
                _round_with_analyses(1, ["spec_a", "spec_b"],
                                     in_sync={"spec_a": True, "spec_b": True},
                                     new_subsystems=0),
            ]
            engine_holder["engine"] = eng
            return eng

        monkeypatch.setattr("se3.engine.sync_loop.SyncEngine", factory)
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

        loop = SyncLoop(tmp_path, max_rounds=10, stable_rounds=1)
        loop.run()

        eng = engine_holder["engine"]
        skip = eng.calls[0].get("skip_specs") or set()
        assert "spec_a" not in skip

    def test_file_set_change_invalidates_all_level2_skips(self, tmp_path, monkeypatch):
        """Adding a new file invalidates all per-spec skip decisions."""
        import subprocess
        from se3.engine.sync_state import compute_code_fingerprint, compute_file_content_hash

        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.email", "test@test"],
                       cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.name", "T"],
                       cwd=str(tmp_path), check=True)
        (tmp_path / "src").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "main.py").write_text("hello")
        subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"],
                       cwd=str(tmp_path), check=True)

        fp = compute_code_fingerprint(tmp_path)
        h_main = compute_file_content_hash(tmp_path / "src" / "main.py")
        spec_deps = {
            "spec_a": {"spec_hash": "any", "deps": {"src/main.py": h_main}},
        }
        _write_sync_state_file(tmp_path, "non-matching-fp", discovery_converged=True,
                               spec_deps=spec_deps)

        # Add a new file that is NOT in the recorded deps
        (tmp_path / "src" / "new_file.py").write_text("new content")

        spec_a_path = tmp_path / "se3" / "specs" / "spec_a" / "spec.md"
        spec_b_path = tmp_path / "se3" / "specs" / "spec_b" / "spec.md"
        spec_a_path.parent.mkdir(parents=True, exist_ok=True)
        spec_b_path.parent.mkdir(parents=True, exist_ok=True)
        spec_a_path.write_text("# Spec A\n## Purpose\nTest.\n### Requirement: R1\n")
        spec_b_path.write_text("# Spec B\n## Purpose\nTest.\n### Requirement: R1\n")

        engine_holder = {}
        def factory(project_root, interactive=False):
            eng = _CallTrackingEngine(project_root, interactive=interactive)
            eng._specs = {
                "spec_a": {"name": "spec_a", "path": str(spec_a_path),
                           "content": spec_a_path.read_text()},
                "spec_b": {"name": "spec_b", "path": str(spec_b_path),
                           "content": spec_b_path.read_text()},
            }
            eng.script = [
                _round_with_analyses(1, ["spec_a", "spec_b"],
                                     in_sync={"spec_a": True, "spec_b": True},
                                     new_subsystems=0),
            ]
            engine_holder["engine"] = eng
            return eng

        monkeypatch.setattr("se3.engine.sync_loop.SyncEngine", factory)
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

        loop = SyncLoop(tmp_path, max_rounds=10, stable_rounds=1)
        loop.run()

        eng = engine_holder["engine"]
        skip = eng.calls[0].get("skip_specs") or set()
        # All level-2 skips invalidated
        assert "spec_a" not in skip
        # Discovery should be forced
        assert eng.calls[0].get("do_discovery") is True

    def test_no_skip_when_deps_empty(self, tmp_path, monkeypatch):
        """Spec with empty deps in cache is not skipped (conservative)."""
        import subprocess
        from se3.engine.sync_state import compute_code_fingerprint

        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.email", "test@test"],
                       cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.name", "T"],
                       cwd=str(tmp_path), check=True)

        spec_deps = {"spec_a": {"spec_hash": "any", "deps": {}}}
        _write_sync_state_file(tmp_path, "non-matching-fp", discovery_converged=True,
                               spec_deps=spec_deps)

        spec_a_path = tmp_path / "se3" / "specs" / "spec_a" / "spec.md"
        spec_a_path.parent.mkdir(parents=True, exist_ok=True)
        spec_a_path.write_text("# Spec A\n## Purpose\nTest.\n### Requirement: R1\n")

        engine_holder = {}
        def factory(project_root, interactive=False):
            eng = _CallTrackingEngine(project_root, interactive=interactive)
            eng._specs = {
                "spec_a": {"name": "spec_a", "path": str(spec_a_path),
                           "content": spec_a_path.read_text()},
            }
            eng.script = [
                _round_with_analyses(1, ["spec_a"], in_sync={"spec_a": True},
                                     new_subsystems=0),
            ]
            engine_holder["engine"] = eng
            return eng

        monkeypatch.setattr("se3.engine.sync_loop.SyncEngine", factory)
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

        loop = SyncLoop(tmp_path, max_rounds=10, stable_rounds=1)
        loop.run()

        eng = engine_holder["engine"]
        skip = eng.calls[0].get("skip_specs") or set()
        assert "spec_a" not in skip

    def test_new_spec_not_in_cache_not_skipped(self, tmp_path, monkeypatch):
        """Spec that exists on disk but not in sync_state cache is NOT skipped."""
        import subprocess
        from se3.engine.sync_state import compute_code_fingerprint, compute_file_content_hash

        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.email", "test@test"],
                       cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.name", "T"],
                       cwd=str(tmp_path), check=True)
        (tmp_path / "src").mkdir(parents=True, exist_ok=True)
        (tmp_path / "src" / "main.py").write_text("hello")
        subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"],
                       cwd=str(tmp_path), check=True)

        h_main = compute_file_content_hash(tmp_path / "src" / "main.py")
        # Only spec_a in cache, not spec_b
        spec_deps = {
            "spec_a": {"spec_hash": "any", "deps": {"src/main.py": h_main}},
        }
        _write_sync_state_file(tmp_path, "non-matching-fp", discovery_converged=True,
                               spec_deps=spec_deps)

        spec_a_path = tmp_path / "se3" / "specs" / "spec_a" / "spec.md"
        spec_b_path = tmp_path / "se3" / "specs" / "spec_b" / "spec.md"
        spec_a_path.parent.mkdir(parents=True, exist_ok=True)
        spec_b_path.parent.mkdir(parents=True, exist_ok=True)
        spec_a_path.write_text("# Spec A\n## Purpose\nTest.\n### Requirement: R1\n")
        spec_b_path.write_text("# Spec B\n## Purpose\nTest.\n### Requirement: R1\n")

        engine_holder = {}
        def factory(project_root, interactive=False):
            eng = _CallTrackingEngine(project_root, interactive=interactive)
            eng._specs = {
                "spec_a": {"name": "spec_a", "path": str(spec_a_path),
                           "content": spec_a_path.read_text()},
                "spec_b": {"name": "spec_b", "path": str(spec_b_path),
                           "content": spec_b_path.read_text()},
            }
            eng.script = [
                _round_with_analyses(1, ["spec_a", "spec_b"],
                                     in_sync={"spec_a": True, "spec_b": True},
                                     new_subsystems=0),
            ]
            engine_holder["engine"] = eng
            return eng

        monkeypatch.setattr("se3.engine.sync_loop.SyncEngine", factory)
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

        loop = SyncLoop(tmp_path, max_rounds=10, stable_rounds=1)
        loop.run()

        eng = engine_holder["engine"]
        skip = eng.calls[0].get("skip_specs") or set()
        assert "spec_a" in skip
        assert "spec_b" not in skip


# ---------------------------------------------------------------------------
# Level 3: Per-spec early exit
# ---------------------------------------------------------------------------


class TestLevel3PerSpecEarlyExit:
    """Level 3: specs individually converge and exit early."""

    def _make_patched_loop_g3(self, tmp_path, monkeypatch, script, stable_rounds=2):
        """Helper that patches for level-3 testing."""
        engine_holder = {}

        def factory(project_root, interactive=False):
            eng = _CallTrackingEngine(project_root, interactive=interactive)
            # Only set the script for the first engine (the main loop one).
            # Subsequent engine creations (e.g. _write_sync_state → _engine_specs)
            # get a fresh engine without script and don't overwrite the holder.
            if "engine" not in engine_holder:
                eng.script = list(script)
                engine_holder["engine"] = eng
            return eng

        monkeypatch.setattr("se3.engine.sync_loop.SyncEngine", factory)
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
        return engine_holder

    def test_spec_exits_after_stable_rounds_zero_drift(self, tmp_path, monkeypatch):
        """Spec A reaches stable_rounds consecutive 0-drift → exits from subsequent rounds.
        B has drift in round 1, so A converges first. Round 3: A is in skip set."""
        script = [
            # Round 1: A in-sync, B has drift
            _round_with_analyses(1, ["A", "B"], in_sync={"A": True, "B": False},
                                 touched_files={"A": ["a.py"], "B": ["b.py"]}),
            # Round 2: A in-sync (counter=2→converged), B in-sync (counter=1)
            _round_with_analyses(2, ["A", "B"], in_sync={"A": True, "B": True},
                                 touched_files={"A": ["a.py"], "B": ["b.py"]}),
            # Round 3: A in skip set, B in-sync (counter=2→converged)
            _round_with_analyses(3, ["A", "B"], in_sync={"A": True, "B": True},
                                 touched_files={"A": ["a.py"], "B": ["b.py"]}),
        ]
        engine_holder = self._make_patched_loop_g3(tmp_path, monkeypatch, script,
                                                    stable_rounds=2)
        loop = SyncLoop(tmp_path, max_rounds=10, stable_rounds=2)
        result = loop.run()

        assert result.converged is True
        eng = engine_holder["engine"]
        # Round 3: A should be in the skip set (per-spec converged after round 2)
        round3_skip = eng.calls[2].get("skip_specs") or set()
        assert "A" in round3_skip

    def test_drift_resets_per_spec_counter(self, tmp_path, monkeypatch):
        """A single drift resets the consecutive 0-drift counter for that spec."""
        # Round 1: in-sync → counter=1
        # Round 2: drift → counter=0
        # Round 3: in-sync → counter=1
        # Round 4: in-sync → counter=2 → converged!
        script = [
            _round_with_analyses(1, ["A"], in_sync={"A": True},
                                 touched_files={"A": ["a.py"]}),
            _round_with_analyses(2, ["A"], in_sync={"A": False},
                                 touched_files={"A": ["a.py"]}),
            _round_with_analyses(3, ["A"], in_sync={"A": True},
                                 touched_files={"A": ["a.py"]}),
            _round_with_analyses(4, ["A"], in_sync={"A": True},
                                 touched_files={"A": ["a.py"]}),
        ]
        engine_holder = self._make_patched_loop_g3(tmp_path, monkeypatch, script,
                                                    stable_rounds=2)
        loop = SyncLoop(tmp_path, max_rounds=10, stable_rounds=2)
        result = loop.run()

        assert result.converged is True
        eng = engine_holder["engine"]
        # After round 2 (drift), A's counter is reset → not in skip set for round 3
        round3_skip = eng.calls[2].get("skip_specs") or set()
        assert "A" not in round3_skip

    def test_one_spec_drift_does_not_reset_others(self, tmp_path, monkeypatch):
        """Spec B's drift does NOT reset already-converged Spec A.
        A converges after rounds 2-3 (2 consecutive 0-drift).
        B keeps drifting until round 4, when it finally is in-sync.
        Round 5 gives B its second consecutive 0-drift → convergence."""
        # A: round1=drift, r2=sync, r3=sync → counter=2 after r3 → converged
        # B: round1=drift, r2=drift, r3=drift, r4=sync → counter=1 after r4
        # Round 5: B=sync → counter=2 → converged
        script = [
            _round_with_analyses(1, ["A", "B"], in_sync={"A": False, "B": False},
                                 touched_files={"A": ["a.py"], "B": ["b.py"]}),
            _round_with_analyses(2, ["A", "B"], in_sync={"A": True, "B": False},
                                 touched_files={"A": ["a.py"], "B": ["b.py"]}),
            _round_with_analyses(3, ["A", "B"], in_sync={"A": True, "B": False},
                                 touched_files={"A": ["a.py"], "B": ["b.py"]}),
            _round_with_analyses(4, ["A", "B"], in_sync={"A": True, "B": True},
                                 touched_files={"A": ["a.py"], "B": ["b.py"]}),
            _round_with_analyses(5, ["A", "B"], in_sync={"A": True, "B": True},
                                 touched_files={"A": ["a.py"], "B": ["b.py"]}),
        ]
        engine_holder = self._make_patched_loop_g3(tmp_path, monkeypatch, script,
                                                    stable_rounds=2)
        loop = SyncLoop(tmp_path, max_rounds=10, stable_rounds=2)
        result = loop.run()

        assert result.converged is True
        eng = engine_holder["engine"]
        # Round 4: A should be in skip set (converged after round 3)
        round4_skip = eng.calls[3].get("skip_specs") or set()
        assert "A" in round4_skip
        # B should NOT be in skip set (still drifting)
        assert "B" not in round4_skip

    def test_level3_works_without_cache(self, tmp_path, monkeypatch):
        """Level 3 per-spec early exit works even without sync_state cache."""
        # A: round1=drift, round2=sync, round3=sync → converged after r3
        # B: round1=sync, round2=sync → converged after r2
        script = [
            _round_with_analyses(1, ["A", "B"], in_sync={"A": False, "B": True},
                                 touched_files={"A": ["a.py"], "B": ["b.py"]}),
            _round_with_analyses(2, ["A", "B"], in_sync={"A": True, "B": True},
                                 touched_files={"A": ["a.py"], "B": ["b.py"]}),
            _round_with_analyses(3, ["A", "B"], in_sync={"A": True, "B": True},
                                 touched_files={"A": ["a.py"], "B": ["b.py"]}),
        ]
        engine_holder = self._make_patched_loop_g3(tmp_path, monkeypatch, script,
                                                    stable_rounds=2)
        loop = SyncLoop(tmp_path, max_rounds=10, stable_rounds=2)
        result = loop.run()
        assert result.converged is True


# ---------------------------------------------------------------------------
# SyncState write on convergence
# ---------------------------------------------------------------------------


class TestSyncStateWriteOnConvergence:
    """sync_state is written after convergence."""

    def test_sync_state_written_after_convergence(self, tmp_path, monkeypatch):
        """After convergence, sync_state.json is created."""
        import subprocess
        from se3.engine.sync_state import state_path

        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.email", "test@test"],
                       cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.name", "T"],
                       cwd=str(tmp_path), check=True)
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("hello")
        subprocess.run(["git", "add", "src/main.py"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"],
                       cwd=str(tmp_path), check=True)

        spec_a_dir = tmp_path / "se3" / "specs" / "spec-a"
        spec_a_dir.mkdir(parents=True)
        (spec_a_dir / "spec.md").write_text(
            "<!-- spec-format: v1 -->\n# spec-a Specification\n"
            "## Purpose\nTest.\n### Requirement: R1\n"
        )

        engine_holder = {}
        def factory(project_root, interactive=False):
            eng = _CallTrackingEngine(project_root, interactive=interactive)
            eng._specs = {
                "spec-a": {
                    "name": "spec-a",
                    "path": str(spec_a_dir / "spec.md"),
                    "content": (spec_a_dir / "spec.md").read_text(),
                },
            }
            eng.script = [
                _round_with_analyses(1, ["spec-a"], in_sync={"spec-a": True},
                                     touched_files={"spec-a": ["src/main.py"]},
                                     new_subsystems=0),
            ]
            engine_holder["engine"] = eng
            return eng

        monkeypatch.setattr("se3.engine.sync_loop.SyncEngine", factory)
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

        loop = SyncLoop(tmp_path, max_rounds=10, stable_rounds=1)
        result = loop.run()

        assert result.converged is True
        assert state_path(tmp_path).exists()

        from se3.engine.sync_state import load
        saved = load(tmp_path)
        assert saved is not None
        assert saved.discovery_converged is True
        assert "spec-a" in saved.spec_deps

    def test_sync_state_not_written_with_failed_analysis(self, tmp_path, monkeypatch):
        """sync_state is NOT written when there are unresolved failed analyses."""
        import subprocess
        from se3.engine.sync_state import state_path

        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)

        engine_holder = {}
        def factory(project_root, interactive=False):
            eng = _CallTrackingEngine(project_root, interactive=interactive)
            eng.script = [
                _round_with_analyses(1, ["A"], in_sync={"A": True},
                                     failed={"A": "infrastructure_failure"},
                                     new_subsystems=0),
            ]
            engine_holder["engine"] = eng
            return eng

        monkeypatch.setattr("se3.engine.sync_loop.SyncEngine", factory)
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

        loop = SyncLoop(tmp_path, max_rounds=10, stable_rounds=1)
        result = loop.run()

        assert not state_path(tmp_path).exists()

    def test_sync_state_persists_for_next_sync(self, tmp_path, monkeypatch):
        """Second sync with unchanged tree hits level-1 shutter."""
        import subprocess
        from se3.engine.sync_state import state_path

        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.email", "test@test"],
                       cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.name", "T"],
                       cwd=str(tmp_path), check=True)
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("hello")
        subprocess.run(["git", "add", "src/main.py"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"],
                       cwd=str(tmp_path), check=True)

        spec_a_dir = tmp_path / "se3" / "specs" / "spec-a"
        spec_a_dir.mkdir(parents=True)
        (spec_a_dir / "spec.md").write_text(
            "<!-- spec-format: v1 -->\n# spec-a Specification\n"
            "## Purpose\nTest.\n### Requirement: R1\n"
        )

        # First sync
        engine_holder1 = {}
        def factory1(project_root, interactive=False):
            eng = _CallTrackingEngine(project_root, interactive=interactive)
            eng._specs = {
                "spec-a": {
                    "name": "spec-a",
                    "path": str(spec_a_dir / "spec.md"),
                    "content": (spec_a_dir / "spec.md").read_text(),
                },
            }
            eng.script = [
                _round_with_analyses(1, ["spec-a"], in_sync={"spec-a": True},
                                     touched_files={"spec-a": ["src/main.py"]},
                                     new_subsystems=0),
            ]
            engine_holder1["engine"] = eng
            return eng

        monkeypatch.setattr("se3.engine.sync_loop.SyncEngine", factory1)
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

        loop1 = SyncLoop(tmp_path, max_rounds=10, stable_rounds=1)
        result1 = loop1.run()
        assert result1.converged is True
        assert state_path(tmp_path).exists()

        # Second sync: should hit level-1 shutter
        engine_holder2 = {}
        def factory2(project_root, interactive=False):
            eng = _CallTrackingEngine(project_root, interactive=interactive)
            engine_holder2["engine"] = eng
            return eng

        monkeypatch.setattr("se3.engine.sync_loop.SyncEngine", factory2)

        loop2 = SyncLoop(tmp_path, max_rounds=10, stable_rounds=1)
        result2 = loop2.run()
        assert result2.converged is True

        eng2 = engine_holder2.get("engine")
        if eng2 is not None:
            assert len(eng2.calls) == 0


# ---------------------------------------------------------------------------
# Skip set correctness
# ---------------------------------------------------------------------------


class TestSkipSetCorrectness:
    """Verify skip_specs handling is correct with per-spec convergence."""

    def test_per_spec_converged_added_to_skip_set(self, tmp_path, monkeypatch):
        """Per-spec converged specs are added to the skip set.
        A is in-sync from round 1, reaches stable_rounds=2 after round 2."""
        script = [
            _round_with_analyses(1, ["A", "B"], in_sync={"A": True, "B": False},
                                 touched_files={"A": ["a.py"], "B": ["b.py"]},
                                 new_subsystems=0),
            _round_with_analyses(2, ["A", "B"], in_sync={"A": True, "B": True},
                                 touched_files={"A": ["a.py"], "B": ["b.py"]},
                                 new_subsystems=0),
            _round_with_analyses(3, ["A", "B"], in_sync={"A": True, "B": True},
                                 touched_files={"A": ["a.py"], "B": ["b.py"]},
                                 new_subsystems=0),
        ]
        engine_holder = {}
        def factory(project_root, interactive=False):
            eng = _CallTrackingEngine(project_root, interactive=interactive)
            eng.script = list(script)
            engine_holder["engine"] = eng
            return eng

        monkeypatch.setattr("se3.engine.sync_loop.SyncEngine", factory)
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

        loop = SyncLoop(tmp_path, max_rounds=10, stable_rounds=2)
        result = loop.run()
        assert result.converged is True

        eng = engine_holder["engine"]
        # Round 2: A has 1 drift-free round → not yet in skip set
        round2_skip = eng.calls[1].get("skip_specs") or set()
        assert "A" not in round2_skip

        # Round 3: A has 2 drift-free rounds → in skip set
        round3_skip = eng.calls[2].get("skip_specs") or set()
        assert "A" in round3_skip

# ---------------------------------------------------------------------------
# G7: CLI options (--force / --confirm-cleanup) and result reporting
# ---------------------------------------------------------------------------


def _patch_loop_engine(tmp_path, monkeypatch, script, specs=None):
    """Patch SyncEngine / LLMCaller / collector for an in-process loop run."""
    engine_holder: Dict[str, Any] = {}

    def factory(project_root, interactive=False):
        eng = _CallTrackingEngine(project_root, interactive=interactive)
        if "engine" not in engine_holder:
            eng.script = list(script)
            eng._specs = dict(specs or {})
            engine_holder["engine"] = eng
        return eng

    monkeypatch.setattr("se3.engine.sync_loop.SyncEngine", factory)
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
    return engine_holder


class TestG7ResultRendering:
    """G7 task 2: the final report surfaces in-sync / skip / obsolete stats."""

    def _render(self, capsys, **fields):
        from se3.commands.sync import _render_loop_result
        from se3.engine.sync_engine import LoopResult

        result = LoopResult(**fields)
        _render_loop_result(result, show_diff=False)
        return capsys.readouterr().out

    def test_level_1_in_sync_reports_zero_llm_calls(self, capsys):
        out = self._render(capsys, converged=True, level_1_cache_hit=True)
        assert "0 LLM calls" in out
        assert "in-sync" in out

    def test_level_1_suppresses_level_2_3_lines(self, capsys):
        out = self._render(
            capsys,
            converged=True,
            level_1_cache_hit=True,
            level_2_skipped_specs=["a", "b"],
            level_3_early_exit_specs=["c"],
        )
        assert "Level-2 cache" not in out
        assert "Level-3 early exit" not in out

    def test_level_2_skipped_count_visible(self, capsys):
        out = self._render(
            capsys,
            converged=True,
            level_2_skipped_specs=["spec_a", "spec_b", "spec_c"],
        )
        assert "Level-2 cache" in out
        assert "3 spec(s) skipped" in out

    def test_level_3_early_exit_visible(self, capsys):
        out = self._render(
            capsys,
            converged=True,
            level_3_early_exit_specs=["spec_x", "spec_y"],
        )
        assert "Level-3 early exit" in out
        assert "2 spec(s)" in out

    def test_obsolete_deleted_and_kept_visible(self, capsys):
        out = self._render(
            capsys,
            converged=True,
            obsolete_specs_deleted=["dead_one"],
            obsolete_specs_kept=["maybe_dead"],
        )
        assert "Obsolete specs deleted" in out
        assert "dead_one" in out
        assert "Obsolete specs kept" in out
        assert "maybe_dead" in out

    def test_normal_convergence_has_no_skip_lines(self, capsys):
        out = self._render(capsys, converged=True)
        assert "Level-2 cache" not in out
        assert "Level-3 early exit" not in out


class TestG7ForceFlag:
    """G7 task 1: --force ignores the cache and rewrites sync_state."""

    def test_force_rewrites_sync_state_after_convergence(
        self, tmp_path, monkeypatch
    ):
        import subprocess
        from se3.engine.sync_state import compute_code_fingerprint, state_path, load

        subprocess.run(["git", "init", "-q"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.email", "t@t"],
                       cwd=str(tmp_path), check=True)
        subprocess.run(["git", "config", "user.name", "T"],
                       cwd=str(tmp_path), check=True)
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("print('hi')")
        subprocess.run(["git", "add", "-A"], cwd=str(tmp_path), check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"],
                       cwd=str(tmp_path), check=True)

        fp = compute_code_fingerprint(tmp_path)
        _write_sync_state_file(tmp_path, fp, discovery_converged=True)
        stale = state_path(tmp_path).read_text(encoding="utf-8")
        assert "2026-01-01T00:00:00Z" in stale

        script = [
            _round_with_analyses(1, ["spec_a"], in_sync={"spec_a": True},
                                 touched_files={"spec_a": ["src/main.py"]},
                                 new_subsystems=0),
        ]
        engine_holder = _patch_loop_engine(tmp_path, monkeypatch, script)

        loop = SyncLoop(tmp_path, max_rounds=10, stable_rounds=1, force=True)
        result = loop.run()

        assert result.converged is True
        assert result.level_1_cache_hit is False
        assert len(engine_holder["engine"].calls) > 0

        rewritten = load(tmp_path)
        assert rewritten is not None
        assert rewritten.converged_at != "2026-01-01T00:00:00Z"


class TestG7ConfirmCleanup:
    """G7 task 1: --confirm-cleanup is threaded into obsolete-spec deletion."""

    def _run_with_obsolete(self, tmp_path, monkeypatch, confirm_cleanup):
        delete_calls: List[Dict[str, Any]] = []

        def fake_delete(project_root, obsolete_specs, confirm):
            delete_calls.append(
                {"obsolete_specs": list(obsolete_specs), "confirm": confirm}
            )
            return {"deleted": list(obsolete_specs) if not confirm else [],
                    "kept": [] if not confirm else list(obsolete_specs)}

        monkeypatch.setattr(
            "se3.engine.sync_discovery.SpecDiscovery.delete_obsolete_specs",
            staticmethod(fake_delete),
        )
        monkeypatch.setattr(
            SyncLoop,
            "_update_obsolete_candidates",
            staticmethod(lambda **kw: {"ghost_spec"}),
        )

        script = [
            _round_with_analyses(1, ["A"], in_sync={"A": True},
                                 touched_files={"A": ["a.py"]}),
            _round_with_analyses(2, ["A"], in_sync={"A": True},
                                 touched_files={"A": ["a.py"]}),
        ]
        _patch_loop_engine(tmp_path, monkeypatch, script)

        loop = SyncLoop(tmp_path, max_rounds=10, stable_rounds=1,
                        confirm_cleanup=confirm_cleanup)
        result = loop.run()
        return result, delete_calls

    def test_confirm_cleanup_true_passes_confirm(self, tmp_path, monkeypatch):
        result, delete_calls = self._run_with_obsolete(
            tmp_path, monkeypatch, confirm_cleanup=True
        )
        assert result.converged is True
        assert len(delete_calls) == 1
        assert delete_calls[0]["confirm"] is True
        assert delete_calls[0]["obsolete_specs"] == ["ghost_spec"]

    def test_confirm_cleanup_false_deletes_directly(self, tmp_path, monkeypatch):
        result, delete_calls = self._run_with_obsolete(
            tmp_path, monkeypatch, confirm_cleanup=False
        )
        assert result.converged is True
        assert len(delete_calls) == 1
        assert delete_calls[0]["confirm"] is False
        assert result.obsolete_specs_deleted == ["ghost_spec"]


class TestG7Level3Telemetry:
    """G7 task 2: LoopResult records level-3 early-exit specs."""

    def test_level_3_early_exit_specs_recorded(self, tmp_path, monkeypatch):
        script = [
            _round_with_analyses(1, ["A", "B"], in_sync={"A": True, "B": False},
                                 touched_files={"A": ["a.py"], "B": ["b.py"]}),
            _round_with_analyses(2, ["A", "B"], in_sync={"A": True, "B": True},
                                 touched_files={"A": ["a.py"], "B": ["b.py"]}),
        ]
        _patch_loop_engine(tmp_path, monkeypatch, script)

        loop = SyncLoop(tmp_path, max_rounds=10, stable_rounds=1)
        result = loop.run()

        assert result.converged is True
        assert set(result.level_3_early_exit_specs) == {"A", "B"}
