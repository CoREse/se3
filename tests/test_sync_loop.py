"""Tests for SyncLoop — multi-round convergence, oscillation, interactive mode."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from se3.engine.sync_engine import (
    DiffType,
    LoopResult,
    RoundResult,
    SpecAnalysis,
    SpecDiff,
)
from se3.engine.sync_loop import (
    OscillationDetector,
    OscillationReport,
    SyncLoop,
    _find_cycle_period,
)


# ---------------------------------------------------------------------------
# OscillationDetector
# ---------------------------------------------------------------------------


class TestFindCyclePeriod:
    def test_alternating_ab(self):
        assert _find_cycle_period(["A", "B", "A", "B"]) == 2

    def test_three_cycle_abc(self):
        assert _find_cycle_period(["A", "B", "C", "A", "B", "C"]) == 3

    def test_a_a_b_c_no_cycle(self):
        assert _find_cycle_period(["A", "A", "B", "C"]) is None

    def test_monotone_no_cycle(self):
        assert _find_cycle_period(["A", "B", "C", "D", "E", "F"]) is None

    def test_period_one_excluded(self):
        # AAAA is convergence, not oscillation.
        assert _find_cycle_period(["A", "A", "A", "A"]) is None

    def test_too_short(self):
        assert _find_cycle_period(["A", "B"]) is None

    def test_partial_match_no_cycle(self):
        assert _find_cycle_period(["A", "B", "C", "B"]) is None


class TestOscillationDetector:
    def test_detects_two_cycle(self):
        det = OscillationDetector()
        for h in ["A", "B", "A", "B"]:
            det.record("auth", h)
        report = det.detect()
        assert report is not None
        assert report.spec_name == "auth"
        assert report.cycle_length == 2

    def test_detects_three_cycle(self):
        det = OscillationDetector()
        for h in ["A", "B", "C", "A", "B", "C"]:
            det.record("auth", h)
        report = det.detect()
        assert report is not None
        assert report.cycle_length == 3

    def test_no_detection_on_aabc(self):
        det = OscillationDetector()
        for h in ["A", "A", "B", "C"]:
            det.record("auth", h)
        assert det.detect() is None

    def test_no_detection_on_monotone(self):
        det = OscillationDetector()
        for h in ["A", "B", "C", "D"]:
            det.record("auth", h)
        assert det.detect() is None

    def test_report_has_hash_sequence_and_summary(self):
        det = OscillationDetector()
        for h in ["aaaaaaaaaa", "bbbbbbbbbb", "aaaaaaaaaa", "bbbbbbbbbb"]:
            det.record("auth", h)
        report = det.detect()
        assert report is not None
        assert len(report.hash_sequence) == 4
        assert "auth" in report.summary()
        assert "period=2" in report.summary()

    def test_multiple_specs_independent(self):
        det = OscillationDetector()
        det.record("a", "X")
        det.record("a", "Y")
        det.record("b", "M")
        det.record("b", "M")  # period-1, no trigger
        det.record("a", "X")
        det.record("a", "Y")
        report = det.detect()
        assert report is not None
        assert report.spec_name == "a"

    def test_rejects_too_small_window(self):
        with pytest.raises(ValueError):
            OscillationDetector(window=2)

    def test_whitespace_robust_via_hash(self):
        # The detector itself just compares hashes — normalization happens
        # in SyncEngine._hash_spec_content. Here we sanity-check that two
        # distinct hashes are treated as distinct states.
        det = OscillationDetector()
        for h in ["x" * 64, "y" * 64, "x" * 64, "y" * 64]:
            det.record("s", h)
        assert det.detect() is not None


# ---------------------------------------------------------------------------
# SyncLoop run() — scripted RoundResults
# ---------------------------------------------------------------------------


def _round(
    idx: int,
    updated: int = 0,
    hashes: Optional[Dict[str, str]] = None,
    created: Optional[List[str]] = None,
) -> RoundResult:
    rr = RoundResult(round_index=idx)
    rr.specs_updated = updated
    rr.spec_hashes_after = dict(hashes or {})
    rr.specs_created = list(created or [])
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
    fully hermetic (no LLMCaller, no project context, no real disk I/O
    beyond ``tmp_path``)."""

    engine_holder: Dict[str, _ScriptedEngine] = {}

    def make_engine(project_root, interactive: bool = False):
        eng = _ScriptedEngine(project_root, interactive=interactive)
        engine_holder["engine"] = eng
        return eng

    monkeypatch.setattr("se3.engine.sync_loop.SyncEngine", make_engine)

    # Stub LLMCaller — it's only instantiated; SyncLoop hands it off to
    # the engine, but our scripted engine doesn't touch it.
    monkeypatch.setattr(
        "se3.engine.llm_caller.LLMCaller",
        lambda **kwargs: MagicMock(name="LLMCaller"),
    )

    # Stub project context collector.
    fake_collector = MagicMock()
    fake_collector.collect.return_value = {"git": {}, "specs": []}
    monkeypatch.setattr(
        "se3.engine.project_context.ProjectContextCollector",
        lambda project_root: fake_collector,
    )

    yield engine_holder


class TestSyncLoopConvergence:
    def test_converges_first_round(self, tmp_path, patched_loop_deps):
        loop = SyncLoop(tmp_path, max_rounds=10)
        eng = None

        def make_engine(*_, **__):
            return None  # placeholder, replaced by patched_loop_deps

        loop_result = None
        # Trigger build of engine through SyncLoop.run; script after-the-fact
        # not possible because engine is constructed inside run(). So script
        # via patched_loop_deps holder: but the holder gets populated when
        # SyncLoop calls SyncEngine(). We script the engine by overriding
        # _ScriptedEngine.script via a side-effect on its construction.
        scripted_returns = [_round(1, updated=0, hashes={"a": "X"})]

        def _engine_factory(project_root, interactive=False):
            eng = _ScriptedEngine(project_root, interactive=interactive)
            eng.script = scripted_returns
            patched_loop_deps["engine"] = eng
            return eng

        with patch("se3.engine.sync_loop.SyncEngine", _engine_factory):
            loop_result = loop.run()

        assert loop_result.converged is True
        assert loop_result.oscillation_detected is False
        assert len(loop_result.rounds) == 1
        assert loop_result.total_specs_updated == 0
        assert loop_result.final_round_index == 1

    def test_converges_after_multiple_rounds(self, tmp_path, patched_loop_deps):
        scripted = [
            _round(1, updated=2, hashes={"a": "X1", "b": "Y1"}),
            _round(2, updated=1, hashes={"a": "X2", "b": "Y1"}),
            _round(3, updated=0, hashes={"a": "X2", "b": "Y1"}),
        ]

        def _engine_factory(project_root, interactive=False):
            eng = _ScriptedEngine(project_root, interactive=interactive)
            eng.script = scripted
            patched_loop_deps["engine"] = eng
            return eng

        with patch("se3.engine.sync_loop.SyncEngine", _engine_factory):
            loop_result = SyncLoop(tmp_path, max_rounds=10).run()

        assert loop_result.converged is True
        assert len(loop_result.rounds) == 3
        assert loop_result.total_specs_updated == 3
        assert loop_result.final_round_index == 3

    def test_stable_rounds_two_requires_two_zero_rounds(
        self, tmp_path, patched_loop_deps
    ):
        scripted = [
            _round(1, updated=1, hashes={"a": "X1"}),
            _round(2, updated=0, hashes={"a": "X1"}),
            _round(3, updated=0, hashes={"a": "X1"}),
        ]

        def _engine_factory(project_root, interactive=False):
            eng = _ScriptedEngine(project_root, interactive=interactive)
            eng.script = scripted
            patched_loop_deps["engine"] = eng
            return eng

        with patch("se3.engine.sync_loop.SyncEngine", _engine_factory):
            loop_result = SyncLoop(tmp_path, max_rounds=10, stable_rounds=2).run()

        assert loop_result.converged is True
        assert len(loop_result.rounds) == 3
        # Round 2 alone would not be enough; needs round 3 too.

    def test_stable_rounds_two_does_not_finish_after_one(
        self, tmp_path, patched_loop_deps
    ):
        scripted = [
            _round(1, updated=0, hashes={"a": "X1"}),
            _round(2, updated=1, hashes={"a": "X2"}),  # resets stable_count
            _round(3, updated=0, hashes={"a": "X2"}),
            _round(4, updated=0, hashes={"a": "X2"}),
        ]

        def _engine_factory(project_root, interactive=False):
            eng = _ScriptedEngine(project_root, interactive=interactive)
            eng.script = scripted
            patched_loop_deps["engine"] = eng
            return eng

        with patch("se3.engine.sync_loop.SyncEngine", _engine_factory):
            loop_result = SyncLoop(tmp_path, max_rounds=10, stable_rounds=2).run()

        assert loop_result.converged is True
        assert len(loop_result.rounds) == 4

    def test_max_rounds_exhausted(self, tmp_path, patched_loop_deps):
        scripted = [
            _round(1, updated=1, hashes={"a": "X1"}),
            _round(2, updated=1, hashes={"a": "X2"}),
        ]

        def _engine_factory(project_root, interactive=False):
            eng = _ScriptedEngine(project_root, interactive=interactive)
            eng.script = scripted
            patched_loop_deps["engine"] = eng
            return eng

        events: List[str] = []

        def cb(phase: str, **kwargs: Any) -> None:
            events.append(phase)

        with patch("se3.engine.sync_loop.SyncEngine", _engine_factory):
            loop_result = SyncLoop(
                tmp_path, max_rounds=2, progress_callback=cb
            ).run()

        assert loop_result.converged is False
        assert loop_result.oscillation_detected is False
        assert len(loop_result.rounds) == 2
        assert "max_rounds_exhausted" in events
        assert loop_result.final_round_index == 2

    def test_oscillation_detected_breaks_loop(
        self, tmp_path, patched_loop_deps
    ):
        scripted = [
            _round(1, updated=1, hashes={"a": "A"}),
            _round(2, updated=1, hashes={"a": "B"}),
            _round(3, updated=1, hashes={"a": "A"}),
            _round(4, updated=1, hashes={"a": "B"}),
            # extras (should never be consumed)
            _round(5, updated=1, hashes={"a": "A"}),
        ]

        def _engine_factory(project_root, interactive=False):
            eng = _ScriptedEngine(project_root, interactive=interactive)
            eng.script = scripted
            patched_loop_deps["engine"] = eng
            return eng

        events: List[str] = []

        def cb(phase: str, **kwargs: Any) -> None:
            events.append(phase)

        with patch("se3.engine.sync_loop.SyncEngine", _engine_factory):
            loop_result = SyncLoop(
                tmp_path, max_rounds=10, progress_callback=cb
            ).run()

        assert loop_result.oscillation_detected is True
        assert loop_result.converged is False
        assert loop_result.oscillation_report is not None
        assert "oscillation" in events
        # Loop must abort no later than the 4th round (when ABAB appears).
        assert len(loop_result.rounds) == 4

    def test_discovery_only_on_first_round(
        self, tmp_path, patched_loop_deps
    ):
        scripted = [
            _round(1, updated=1, hashes={"a": "X1"}),
            _round(2, updated=0, hashes={"a": "X1"}),
        ]

        def _engine_factory(project_root, interactive=False):
            eng = _ScriptedEngine(project_root, interactive=interactive)
            eng.script = scripted
            patched_loop_deps["engine"] = eng
            return eng

        with patch("se3.engine.sync_loop.SyncEngine", _engine_factory):
            loop = SyncLoop(tmp_path, max_rounds=10)
            loop.run()

        eng = patched_loop_deps["engine"]
        assert eng.calls[0]["do_discovery"] is True
        assert eng.calls[1]["do_discovery"] is False

    def test_round_aware_step_ids(self, tmp_path, patched_loop_deps):
        """Verify SyncLoop forwards a SyncFlowContext that supports
        round-aware step_id generation (covers task-11 acceptance:
        every per-round LLM step_id has a round dimension)."""
        scripted = [_round(1, updated=0, hashes={})]

        def _engine_factory(project_root, interactive=False):
            eng = _ScriptedEngine(project_root, interactive=interactive)
            eng.script = scripted
            patched_loop_deps["engine"] = eng
            return eng

        with patch("se3.engine.sync_loop.SyncEngine", _engine_factory):
            SyncLoop(tmp_path, max_rounds=10).run()

        eng = patched_loop_deps["engine"]
        flow_ctx = eng.calls[0]["flow_ctx"]
        sid = flow_ctx.make_round_step_id(1, "analyze", "spec_x")
        assert "r1" in sid
        assert "spec_x" in sid


class TestSyncLoopProgressCallback:
    def test_emits_round_start_end_and_converged(
        self, tmp_path, patched_loop_deps
    ):
        scripted = [_round(1, updated=0, hashes={"a": "X"})]

        def _engine_factory(project_root, interactive=False):
            eng = _ScriptedEngine(project_root, interactive=interactive)
            eng.script = scripted
            patched_loop_deps["engine"] = eng
            return eng

        events: List[str] = []

        def cb(phase: str, **kwargs: Any) -> None:
            events.append(phase)

        with patch("se3.engine.sync_loop.SyncEngine", _engine_factory):
            SyncLoop(tmp_path, max_rounds=10, progress_callback=cb).run()

        assert "round_start" in events
        assert "round_end" in events
        assert "converged" in events
        assert "max_rounds_exhausted" not in events
        assert "oscillation" not in events

    def test_spec_progress_phases_forwarded(
        self, tmp_path, patched_loop_deps
    ):
        """The inner SyncEngine progress callback (phase='analyzing',
        'analyzed', 'discovering') must be translated into loop-level
        phases by SyncLoop._wrap_progress."""
        captured: List[str] = []

        def cb(phase: str, **kwargs: Any) -> None:
            captured.append(phase)

        # We need a scripted engine that ALSO drives its progress_callback.
        def _engine_factory(project_root, interactive=False):
            eng = _ScriptedEngine(project_root, interactive=interactive)

            def fake_run(**kwargs):
                pcb = kwargs.get("progress_callback")
                if pcb:
                    pcb("discovering", None, 0, 0, None)
                    pcb("analyzing", "auth", 0, 1, None)
                    pcb("analyzed", "auth", 0, 1, SpecAnalysis("auth"))
                return _round(1, updated=0, hashes={"auth": "X"})

            eng.run_once = fake_run  # type: ignore[assignment]
            patched_loop_deps["engine"] = eng
            return eng

        with patch("se3.engine.sync_loop.SyncEngine", _engine_factory):
            SyncLoop(tmp_path, max_rounds=10, progress_callback=cb).run()

        assert "spec_analyzing" in captured
        assert "spec_analyzed" in captured
        assert "discovering" in captured


class TestSyncLoopInteractiveFlag:
    def test_interactive_false_passed_to_engine(
        self, tmp_path, patched_loop_deps
    ):
        seen: Dict[str, bool] = {}

        def _engine_factory(project_root, interactive=False):
            seen["interactive"] = interactive
            eng = _ScriptedEngine(project_root, interactive=interactive)
            eng.script = [_round(1, updated=0, hashes={})]
            patched_loop_deps["engine"] = eng
            return eng

        with patch("se3.engine.sync_loop.SyncEngine", _engine_factory):
            SyncLoop(tmp_path, max_rounds=10, interactive=False).run()

        assert seen["interactive"] is False

    def test_interactive_true_passed_to_engine(
        self, tmp_path, patched_loop_deps
    ):
        seen: Dict[str, bool] = {}

        def _engine_factory(project_root, interactive=False):
            seen["interactive"] = interactive
            eng = _ScriptedEngine(project_root, interactive=interactive)
            eng.script = [_round(1, updated=0, hashes={})]
            patched_loop_deps["engine"] = eng
            return eng

        with patch("se3.engine.sync_loop.SyncEngine", _engine_factory):
            SyncLoop(tmp_path, max_rounds=10, interactive=True).run()

        assert seen["interactive"] is True

    def test_persistent_skip_does_not_cause_infinite_loop(
        self, tmp_path, patched_loop_deps
    ):
        """If interactive=True and the user always skips a high-impact
        deletion, the spec content never changes — so specs_updated==0
        and the loop converges naturally on the next stable round."""
        scripted = [
            _round(1, updated=0, hashes={"auth": "STABLE"}),
        ]

        def _engine_factory(project_root, interactive=False):
            eng = _ScriptedEngine(project_root, interactive=interactive)
            eng.script = scripted
            patched_loop_deps["engine"] = eng
            return eng

        with patch("se3.engine.sync_loop.SyncEngine", _engine_factory):
            loop_result = SyncLoop(
                tmp_path, max_rounds=10, interactive=True
            ).run()

        assert loop_result.converged is True


class TestSyncLoopRoundsSummaryPersistence:
    def test_rounds_summary_written(self, tmp_path, patched_loop_deps):
        scripted = [_round(1, updated=0, hashes={"a": "X"})]

        def _engine_factory(project_root, interactive=False):
            eng = _ScriptedEngine(project_root, interactive=interactive)
            eng.script = scripted
            patched_loop_deps["engine"] = eng
            return eng

        with patch("se3.engine.sync_loop.SyncEngine", _engine_factory):
            SyncLoop(tmp_path, max_rounds=10).run()

        history_dir = tmp_path / "se3" / "history"
        assert history_dir.exists()
        # Find exactly one flow dir with _rounds.json
        flow_dirs = list(history_dir.iterdir())
        assert len(flow_dirs) == 1
        rounds_json = flow_dirs[0] / "_rounds.json"
        assert rounds_json.exists()


class TestSyncLoopValidation:
    def test_rejects_max_rounds_zero(self, tmp_path):
        with pytest.raises(ValueError):
            SyncLoop(tmp_path, max_rounds=0)

    def test_rejects_stable_rounds_zero(self, tmp_path):
        with pytest.raises(ValueError):
            SyncLoop(tmp_path, stable_rounds=0)
