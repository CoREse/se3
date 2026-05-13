"""SyncLoop — Multi-round convergence controller for one-directional sync.

The loop wraps ``SyncEngine`` and adds cross-round orchestration:

* drive ``SyncEngine.run_once`` up to ``max_rounds`` times,
* declare convergence after ``stable_rounds`` consecutive rounds with no
  spec changes,
* abort with an oscillation report when a spec's content hash oscillates
  between two or more states across rounds,
* aggregate per-round results into a single ``LoopResult``.

``SyncEngine.run_once`` itself stays stateless across rounds; everything
that depends on cross-round history lives here.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .sync_engine import LoopResult, RoundResult, SyncEngine

logger = logging.getLogger(__name__)

_DEFAULT_OSCILLATION_WINDOW = 8


@dataclass
class OscillationReport:
    """Diagnostic info about a detected oscillation cycle."""

    spec_name: str
    cycle_length: int
    hash_sequence: List[str] = field(default_factory=list)

    def summary(self) -> str:
        short = [h[:8] for h in self.hash_sequence]
        return (
            f"Spec '{self.spec_name}' oscillates with period={self.cycle_length}; "
            f"recent hashes: {short}"
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "spec_name": self.spec_name,
            "cycle_length": self.cycle_length,
            "hash_sequence": list(self.hash_sequence),
        }


class OscillationDetector:
    """Track per-spec content-hash history and flag periodic oscillation.

    We keep up to ``window`` most recent hashes per spec. After each
    ``record`` call, ``detect()`` examines every spec's sequence and
    reports the first one that contains a repeated period of length
    ``>= 2``. Period 1 (identical hash repeated) is NOT a cycle — it is
    convergence — so it is intentionally excluded.

    Acceptance examples:
      * ``[A,B,A,B]``      -> period 2  (trigger)
      * ``[A,B,C,A,B,C]``  -> period 3  (trigger)
      * ``[A,A,B,C]``      -> no cycle  (no trigger)
      * monotone increasing sequence -> no cycle (no trigger)
    """

    def __init__(self, window: int = _DEFAULT_OSCILLATION_WINDOW) -> None:
        if window < 4:
            raise ValueError("OscillationDetector window must be >= 4")
        self.window = window
        self._history: Dict[str, deque[str]] = {}

    def record(self, spec_name: str, content_hash: str) -> None:
        seq = self._history.get(spec_name)
        if seq is None:
            seq = deque(maxlen=self.window)
            self._history[spec_name] = seq
        seq.append(content_hash)

    def detect(self) -> Optional[OscillationReport]:
        for spec_name, seq in self._history.items():
            if len(seq) < 4:  # need at least one full period >= 2 repeated twice
                continue
            cycle_len = _find_cycle_period(list(seq))
            if cycle_len is not None:
                return OscillationReport(
                    spec_name=spec_name,
                    cycle_length=cycle_len,
                    hash_sequence=list(seq),
                )
        return None

    def history_snapshot(self) -> Dict[str, List[str]]:
        return {k: list(v) for k, v in self._history.items()}


def _find_cycle_period(seq: List[str]) -> Optional[int]:
    """Return the shortest period ``p >= 2`` for which ``seq[-2p:-p] == seq[-p:]``.

    A degenerate cycle whose period elements are all identical (e.g.
    ``[A,A,A,A]``) is treated as convergence, not oscillation, and
    returns ``None``.
    """
    n = len(seq)
    for p in range(2, n // 2 + 1):
        tail = seq[-p:]
        if tail == seq[-2 * p:-p] and len(set(tail)) >= 2:
            return p
    return None


class SyncLoop:
    """Drive ``SyncEngine.run_once`` to convergence.

    Args:
        project_root: Project root used by ``SyncEngine``.
        max_rounds: Hard upper bound on rounds. Beyond this, the loop
            aborts and reports ``did not converge``.
        stable_rounds: Number of consecutive zero-change rounds required
            before declaring convergence.
        interactive: When True, ``SyncEngine`` routes high-impact
            requirement deletions through ``SyncInteractionHandler`` and
            waits for ``approve|skip`` decisions in every round.
        progress_callback: Optional ``(phase, *, **)`` callback. Phases:
            ``round_start``, ``round_end``, ``spec_analyzing``,
            ``spec_analyzed``, ``converged``, ``oscillation``,
            ``max_rounds_exhausted``.
    """

    def __init__(
        self,
        project_root: Path,
        max_rounds: int = 10,
        stable_rounds: int = 1,
        interactive: bool = False,
        progress_callback: Optional[Callable[..., None]] = None,
        oscillation_window: int = _DEFAULT_OSCILLATION_WINDOW,
    ) -> None:
        if max_rounds < 1:
            raise ValueError("max_rounds must be >= 1")
        if stable_rounds < 1:
            raise ValueError("stable_rounds must be >= 1")

        self.project_root = Path(project_root)
        self.max_rounds = max_rounds
        self.stable_rounds = stable_rounds
        self.interactive = interactive
        self.progress_callback = progress_callback
        self.oscillation_window = oscillation_window

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self) -> LoopResult:
        from .llm_caller import LLMCaller
        from .project_context import ProjectContextCollector
        from .sync_history import SyncFlowContext

        flow_ctx = SyncFlowContext(self.project_root)
        flow_ctx.write_meta()

        llm_caller = LLMCaller(
            project_root=self.project_root,
            flow_id=flow_ctx.flow_id,
        )
        engine = SyncEngine(self.project_root, interactive=self.interactive)
        detector = OscillationDetector(window=self.oscillation_window)

        collector = ProjectContextCollector(self.project_root)

        loop_result = LoopResult()
        stable_count = 0

        for round_index in range(1, self.max_rounds + 1):
            self._emit("round_start", round_index=round_index)

            project_context = self._build_context(collector)

            round_result = engine.run_once(
                round_index=round_index,
                flow_ctx=flow_ctx,
                llm_caller=llm_caller,
                project_context=project_context,
                specs=None,  # let SyncEngine reload each round
                do_discovery=(round_index == 1),
                progress_callback=self._wrap_progress(round_index),
            )

            loop_result.rounds.append(round_result)
            loop_result.total_specs_updated += round_result.specs_updated
            for name in round_result.specs_created:
                if name not in loop_result.total_specs_created:
                    loop_result.total_specs_created.append(name)
            if round_result.discovery_failed:
                loop_result.discovery_failed = True
            loop_result.final_round_index = round_index

            for spec_name, content_hash in round_result.spec_hashes_after.items():
                detector.record(spec_name, content_hash)

            self._emit(
                "round_end",
                round_index=round_index,
                specs_updated=round_result.specs_updated,
                changes_by_spec=dict(round_result.changes_by_spec),
            )

            report = detector.detect()
            if report is not None:
                loop_result.oscillation_detected = True
                loop_result.oscillation_report = report.summary()
                self._emit(
                    "oscillation",
                    round_index=round_index,
                    report=report,
                )
                logger.warning("Oscillation detected: %s", report.summary())
                break

            if round_result.specs_updated == 0:
                stable_count += 1
                if stable_count >= self.stable_rounds:
                    loop_result.converged = True
                    self._emit(
                        "converged",
                        round_index=round_index,
                        stable_rounds=self.stable_rounds,
                    )
                    break
            else:
                stable_count = 0
        else:
            # Loop fell through without break: max_rounds exhausted.
            self._emit(
                "max_rounds_exhausted",
                round_index=self.max_rounds,
                max_rounds=self.max_rounds,
            )

        try:
            flow_ctx.write_rounds_summary(loop_result)
        except Exception as e:
            logger.warning("Failed to persist sync rounds summary: %s", e)

        return loop_result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _build_context(self, collector: Any) -> str:
        try:
            context_dict = collector.collect()
            return json.dumps(
                context_dict, indent=2, ensure_ascii=False, default=str
            )
        except Exception as e:
            logger.warning("Failed to collect project context: %s", e)
            return "{}"

    def _wrap_progress(
        self, round_index: int
    ) -> Optional[Callable[..., None]]:
        """Translate SyncEngine's progress phases into loop-level phases."""
        cb = self.progress_callback
        if cb is None:
            return None

        def adapter(
            phase: str,
            spec_name: Optional[str],
            index: int,
            total: int,
            analysis: Any,
        ) -> None:
            if phase == "analyzing":
                self._emit(
                    "spec_analyzing",
                    round_index=round_index,
                    spec_name=spec_name,
                    index=index,
                    total=total,
                )
            elif phase == "analyzed":
                self._emit(
                    "spec_analyzed",
                    round_index=round_index,
                    spec_name=spec_name,
                    index=index,
                    total=total,
                    analysis=analysis,
                )
            elif phase == "discovering":
                self._emit("discovering", round_index=round_index)

        return adapter

    def _emit(self, phase: str, **kwargs: Any) -> None:
        cb = self.progress_callback
        if cb is None:
            return
        try:
            cb(phase, **kwargs)
        except Exception:
            logger.debug("progress_callback raised on phase=%s", phase, exc_info=True)
