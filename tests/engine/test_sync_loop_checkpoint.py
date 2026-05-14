"""Tests for SyncLoop checkpoint + resume + infrastructure-failure pause.

These tests exercise three closely related G5 features:

* writing a ``sync_checkpoint.json`` and pausing when the loop hits the
  consecutive infrastructure-failure threshold,
* continuing past the pause when the user signals "continue",
* resuming a run from a checkpoint with sha256-based skip detection so
  only changed specs are re-analyzed.

All tests are hermetic — ``SyncEngine`` is replaced by a scripted stand-in
and ``LLMCaller`` / ``ProjectContextCollector`` are stubbed out.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from unittest.mock import MagicMock, patch

from se3.engine import sync_checkpoint as checkpoint_mod
from se3.engine.sync_checkpoint import (
    SyncCheckpoint,
    checkpoint_path,
    clear,
    load,
    recompute_in_sync,
    save,
)
from se3.engine.sync_engine import RoundResult, SpecAnalysis
from se3.engine.sync_loop import SyncLoop


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _analysis(name: str, *, failed: Optional[str] = None) -> SpecAnalysis:
    a = SpecAnalysis(spec_name=name, diffs=[])
    if failed is not None:
        a.failed_analysis_reason = failed
    return a


def _round(
    idx: int,
    *,
    updated: int = 0,
    hashes: Optional[Dict[str, str]] = None,
    analyses: Optional[List[SpecAnalysis]] = None,
) -> RoundResult:
    rr = RoundResult(round_index=idx)
    rr.specs_updated = updated
    rr.spec_hashes_after = dict(hashes or {})
    rr.analyses = list(analyses or [])
    return rr


class _ScriptedEngine:
    """SyncEngine stand-in that pops scripted RoundResults in order.

    Also records every ``run_once`` kwargs dict so tests can inspect
    which ``skip_specs`` (or other arguments) were forwarded.
    """

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
    """Patch SyncEngine + helpers so SyncLoop.run() is fully hermetic."""

    engine_holder: Dict[str, _ScriptedEngine] = {}

    def make_engine(project_root, interactive: bool = False):
        eng = _ScriptedEngine(project_root, interactive=interactive)
        engine_holder["engine"] = eng
        return eng

    monkeypatch.setattr("se3.engine.sync_loop.SyncEngine", make_engine)

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


def _script_engine(scripted: List[RoundResult], patched_loop_deps):
    """Install scripted RoundResults into the SyncEngine that SyncLoop builds."""

    def factory(project_root, interactive: bool = False):
        eng = _ScriptedEngine(project_root, interactive=interactive)
        eng.script = list(scripted)
        patched_loop_deps["engine"] = eng
        return eng

    return patch("se3.engine.sync_loop.SyncEngine", factory)


# ---------------------------------------------------------------------------
# SyncCheckpoint round-trip
# ---------------------------------------------------------------------------


class TestCheckpointPersistence:
    def test_save_load_round_trip(self, tmp_path):
        cp = SyncCheckpoint(
            round_index=3,
            max_rounds=10,
            in_sync_specs={"base": "a" * 64, "flow": "b" * 64},
            failed_analyses={"docs": "infrastructure_failure"},
            reason="quota_exhausted",
        )
        save(cp, tmp_path)
        loaded = load(tmp_path)
        assert loaded is not None
        assert loaded.round_index == 3
        assert loaded.max_rounds == 10
        assert loaded.in_sync_specs == {"base": "a" * 64, "flow": "b" * 64}
        assert loaded.failed_analyses == {"docs": "infrastructure_failure"}
        assert loaded.reason == "quota_exhausted"

    def test_load_missing_returns_none(self, tmp_path):
        assert load(tmp_path) is None

    def test_clear_missing_is_no_op(self, tmp_path):
        clear(tmp_path)  # must not raise

    def test_clear_removes_file(self, tmp_path):
        save(SyncCheckpoint(round_index=1, max_rounds=10), tmp_path)
        assert checkpoint_path(tmp_path).exists()
        clear(tmp_path)
        assert not checkpoint_path(tmp_path).exists()

    def test_atomic_write_no_tmp_left_behind(self, tmp_path):
        save(SyncCheckpoint(round_index=1, max_rounds=10), tmp_path)
        state_dir = tmp_path / "se3" / "state"
        tmp_leftovers = [p for p in state_dir.iterdir() if p.name.endswith(".tmp")]
        assert tmp_leftovers == []

    def test_recompute_in_sync_detects_unchanged_and_changed(self, tmp_path):
        # Two specs on disk; one matches its recorded hash, one does not.
        specs_dir = tmp_path / "se3" / "specs"
        (specs_dir / "stable").mkdir(parents=True)
        (specs_dir / "stable" / "spec.md").write_text("ALPHA\n", encoding="utf-8")
        (specs_dir / "drifted").mkdir(parents=True)
        (specs_dir / "drifted" / "spec.md").write_text("OLD\n", encoding="utf-8")

        # Compute current hash for stable, fake a wrong hash for drifted.
        from se3.engine.sync_checkpoint import _hash_disk_spec

        stable_hash = _hash_disk_spec(specs_dir / "stable" / "spec.md")
        assert stable_hash is not None
        drifted_hash_recorded = "0" * 64  # intentionally wrong

        cp = SyncCheckpoint(
            round_index=2,
            max_rounds=10,
            in_sync_specs={"stable": stable_hash, "drifted": drifted_hash_recorded},
        )

        still, changed = recompute_in_sync(cp, tmp_path)
        assert still == {"stable"}
        assert changed == {"drifted"}


# ---------------------------------------------------------------------------
# SyncLoop — infrastructure-failure pause writes checkpoint
# ---------------------------------------------------------------------------


class TestInfraFailurePause:
    def test_three_consecutive_failures_writes_checkpoint_and_exits(
        self, tmp_path, patched_loop_deps
    ):
        """3 consecutive rounds with ``infrastructure_failure`` analyses
        trigger the pause. ``prompt_resume_or_exit`` returns ``'exit'``, so
        the loop returns without converging and leaves the checkpoint on
        disk for a later ``--resume``.

        Each round has ``specs_updated=1`` so the round is NOT considered
        stable (the loop must reach the failure-threshold check before any
        convergence break).
        """

        scripted = [
            _round(
                1,
                updated=1,
                hashes={"a": "h1"},
                analyses=[
                    _analysis("a", failed="infrastructure_failure"),
                    _analysis("base"),  # in sync
                ],
            ),
            _round(
                2,
                updated=1,
                hashes={"a": "h1"},
                analyses=[
                    _analysis("a", failed="infrastructure_failure"),
                    _analysis("base"),
                ],
            ),
            _round(
                3,
                updated=1,
                hashes={"a": "h1", "base": "bh"},
                analyses=[
                    _analysis("a", failed="infrastructure_failure"),
                    _analysis("base"),
                ],
            ),
        ]

        prompt_calls: List[Dict[str, Any]] = []

        def fake_prompt(stats: Dict[str, Any]) -> str:
            prompt_calls.append(stats)
            return "exit"

        with _script_engine(scripted, patched_loop_deps):
            loop = SyncLoop(
                tmp_path,
                max_rounds=10,
                infrastructure_failure_threshold=3,
                prompt_resume_or_exit=fake_prompt,
            )
            loop_result = loop.run()

        # Checkpoint must exist with the right shape.
        path = checkpoint_path(tmp_path)
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["reason"] == "quota_exhausted"
        assert data["round_index"] == 3
        assert data["max_rounds"] == 10
        # `base` was reported in-sync on every round and has a hash by round
        # 3, so it should land in the in_sync set.
        assert "base" in data["in_sync_specs"]
        assert data["failed_analyses"].get("a") == "infrastructure_failure"

        # Prompt was actually invoked with structured stats.
        assert len(prompt_calls) == 1
        stats = prompt_calls[0]
        assert stats["failure_count"] >= 3
        assert stats["round_index"] == 3
        assert stats["reason"] == "quota_exhausted"

        # Loop did not falsely converge.
        assert loop_result.converged is False
        assert loop_result.oscillation_detected is False

    def test_continue_resets_counter_and_run_converges(
        self, tmp_path, patched_loop_deps
    ):
        """Two failing rounds (with drift to keep them unstable), prompt
        fires once at threshold=2, user answers continue, then round 3
        converges cleanly."""

        scripted = [
            _round(
                1,
                updated=1,
                hashes={"a": "h1"},
                analyses=[_analysis("a", failed="infrastructure_failure")],
            ),
            _round(
                2,
                updated=1,
                hashes={"a": "h1"},
                analyses=[_analysis("a", failed="infrastructure_failure")],
            ),
            _round(
                3,
                updated=0,
                hashes={"a": "h1"},
                analyses=[_analysis("a")],  # in sync this time
            ),
        ]

        calls = {"n": 0}

        def fake_prompt(stats):
            calls["n"] += 1
            return "continue"

        with _script_engine(scripted, patched_loop_deps):
            loop = SyncLoop(
                tmp_path,
                max_rounds=10,
                infrastructure_failure_threshold=2,
                prompt_resume_or_exit=fake_prompt,
            )
            loop_result = loop.run()

        assert calls["n"] == 1
        assert loop_result.converged is True
        # Normal exit clears the checkpoint.
        assert not checkpoint_path(tmp_path).exists()

    def test_checkpoint_cleared_on_natural_convergence(
        self, tmp_path, patched_loop_deps
    ):
        """If a prior run left a checkpoint, a fresh run that converges
        without ever hitting the failure threshold still ends up clearing
        whatever stale checkpoint was on disk."""

        # Pre-existing checkpoint that should be wiped.
        save(SyncCheckpoint(round_index=99, max_rounds=10), tmp_path)
        assert checkpoint_path(tmp_path).exists()

        scripted = [
            _round(
                1,
                updated=0,
                hashes={"a": "h1"},
                analyses=[_analysis("a")],
            )
        ]

        with _script_engine(scripted, patched_loop_deps):
            loop = SyncLoop(
                tmp_path,
                max_rounds=10,
                prompt_resume_or_exit=lambda s: "exit",
            )
            loop_result = loop.run()

        assert loop_result.converged is True
        assert not checkpoint_path(tmp_path).exists()


# ---------------------------------------------------------------------------
# SyncLoop — resume from checkpoint
# ---------------------------------------------------------------------------


class TestResumeFromCheckpoint:
    def test_resume_skips_unchanged_specs_and_continues_round_index(
        self, tmp_path, patched_loop_deps
    ):
        """Build a checkpoint with two recorded in-sync specs. Modify one on
        disk so its hash no longer matches. Resume should pass ``skip_specs``
        containing only the unmodified spec, and start at the recorded
        round_index, not round 1."""

        specs_dir = tmp_path / "se3" / "specs"
        (specs_dir / "stable").mkdir(parents=True)
        (specs_dir / "stable" / "spec.md").write_text("ALPHA\n", encoding="utf-8")
        (specs_dir / "drifted").mkdir(parents=True)
        (specs_dir / "drifted" / "spec.md").write_text("ORIGINAL\n", encoding="utf-8")

        from se3.engine.sync_checkpoint import _hash_disk_spec

        stable_hash = _hash_disk_spec(specs_dir / "stable" / "spec.md")
        drifted_hash = _hash_disk_spec(specs_dir / "drifted" / "spec.md")

        # Now perturb the second spec — its disk hash will no longer match.
        (specs_dir / "drifted" / "spec.md").write_text("CHANGED\n", encoding="utf-8")

        # The checkpoint's ``round_index`` records the round that failed; the
        # resumed run continues from ``round_index + 1`` under the original
        # budget (remaining = max_rounds - round_index).
        cp = SyncCheckpoint(
            round_index=3,
            max_rounds=10,
            in_sync_specs={"stable": stable_hash, "drifted": drifted_hash},
        )

        scripted = [
            _round(
                4,
                updated=0,
                hashes={"stable": stable_hash, "drifted": "newhash"},
                analyses=[_analysis("stable"), _analysis("drifted")],
            )
        ]

        with _script_engine(scripted, patched_loop_deps):
            loop = SyncLoop(
                tmp_path,
                max_rounds=10,
                resume_from=cp,
                prompt_resume_or_exit=lambda s: "exit",
            )
            loop_result = loop.run()

        engine = patched_loop_deps["engine"]
        # Exactly one call (round 4); discovery suppressed; skip_specs has
        # only "stable" because "drifted"'s disk hash changed.
        assert len(engine.calls) == 1
        call = engine.calls[0]
        assert call["round_index"] == 4
        assert call["do_discovery"] is False
        assert call["skip_specs"] == {"stable"}

        assert loop_result.converged is True
        assert loop_result.final_round_index == 4
        # Checkpoint cleared on successful convergence.
        assert not checkpoint_path(tmp_path).exists()
