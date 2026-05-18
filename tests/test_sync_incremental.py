"""Tests for incremental sync optimisations — discovery convergence tracking."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from se3.engine.sync_engine import RoundResult, SpecAnalysis
from se3.engine.sync_loop import SyncLoop


# ---------------------------------------------------------------------------
# Helpers
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
