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

import hashlib
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set

from . import sync_checkpoint as _checkpoint_module
from . import sync_state as _sync_state_module
from .sync_checkpoint import SyncCheckpoint
from .sync_engine import LoopResult, RoundResult, SyncEngine

logger = logging.getLogger(__name__)

_DEFAULT_OSCILLATION_WINDOW = 8
_DEFAULT_INFRA_FAILURE_THRESHOLD = 3


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
        infrastructure_failure_threshold: int = _DEFAULT_INFRA_FAILURE_THRESHOLD,
        resume_from: Optional[SyncCheckpoint] = None,
        prompt_resume_or_exit: Optional[Callable[[Dict[str, Any]], str]] = None,
        force: bool = False,
    ) -> None:
        if max_rounds < 1:
            raise ValueError("max_rounds must be >= 1")
        if stable_rounds < 1:
            raise ValueError("stable_rounds must be >= 1")
        if infrastructure_failure_threshold < 1:
            raise ValueError("infrastructure_failure_threshold must be >= 1")

        self.project_root = Path(project_root)
        self.max_rounds = max_rounds
        self.stable_rounds = stable_rounds
        self.interactive = interactive
        self.progress_callback = progress_callback
        self.oscillation_window = oscillation_window
        self.infrastructure_failure_threshold = infrastructure_failure_threshold
        self.resume_from = resume_from
        self.force = force
        self._consecutive_infra_failures = 0
        if prompt_resume_or_exit is None:
            from .sync_interaction import prompt_resume_or_exit as _default_prompt
            prompt_resume_or_exit = _default_prompt
        self._prompt_resume_or_exit = prompt_resume_or_exit

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
        self._consecutive_infra_failures = 0

        # Discovery convergence tracking (per-sync-call state).
        # Discovery runs every round until *stable_rounds* consecutive
        # rounds produce 0 new subsystems; after that it is skipped for
        # the rest of this sync invocation.
        discovery_converged = False
        discovery_stable_count = 0

        start_round = 1
        skip_specs: set[str] = set()
        if self.resume_from is not None:
            still_in_sync, changed = _checkpoint_module.recompute_in_sync(
                self.resume_from, self.project_root
            )
            skip_specs = set(still_in_sync)
            # The checkpoint's round_index records the round that *failed*;
            # resume continues from the next slot under the original budget,
            # so the remaining round budget is `max_rounds - round_index`.
            start_round = max(1, self.resume_from.round_index + 1)
            self._emit(
                "resuming",
                round_index=start_round,
                skipped_specs=sorted(skip_specs),
                changed_specs=sorted(changed),
            )
            logger.info(
                "Resuming sync from round %d; skipping %d still-in-sync specs, "
                "%d changed specs need re-analysis",
                start_round, len(skip_specs), len(changed),
            )

        # ── Level 1 & 2: load sync_state, evaluate skip gates ──────────────
        pre_loop_sync_state = None
        level_2_skip_specs: set[str] = set()
        force_discovery = False

        if not self.force and self.resume_from is None:
            pre_loop_sync_state = _sync_state_module.load(self.project_root)

        # Level 1 — global shutter
        if pre_loop_sync_state and pre_loop_sync_state.discovery_converged:
            current_fp = _sync_state_module.compute_code_fingerprint(
                self.project_root
            )
            if current_fp == pre_loop_sync_state.code_fingerprint:
                loop_result.converged = True
                loop_result.final_round_index = 0
                self._emit(
                    "converged",
                    round_index=0,
                    stable_rounds=self.stable_rounds,
                )
                self.discovery_converged = True
                logger.info(
                    "Level-1 global shutter: code fingerprint matches, "
                    "0 LLM calls."
                )
                return loop_result

        # Level 2 — per-spec gate (only if discovery was converged in cache)
        if pre_loop_sync_state and pre_loop_sync_state.discovery_converged:
            file_set_changed = _sync_state_module.detect_file_set_change(
                pre_loop_sync_state, self.project_root
            )
            if file_set_changed:
                discovery_converged = False
                discovery_stable_count = 0
                force_discovery = True
                logger.info(
                    "Level-2 guard: file-set change detected, "
                    "invalidating all per-spec skips and forcing discovery."
                )
            else:
                # Pre-load specs to evaluate per-spec gate.
                all_specs = engine._load_specs()
                for spec_name in all_specs:
                    entry = pre_loop_sync_state.spec_deps.get(spec_name)
                    if not entry:
                        continue
                    deps = entry.get("deps", {})
                    if not deps:
                        continue
                    current_hashes: Dict[str, str] = {}
                    all_present = True
                    for dep_path in deps:
                        h = _sync_state_module.compute_file_content_hash(
                            self.project_root / dep_path
                        )
                        if h is None:
                            all_present = False
                            break
                        current_hashes[dep_path] = h
                    if all_present and pre_loop_sync_state.spec_in_sync(
                        spec_name, current_hashes
                    ):
                        level_2_skip_specs.add(spec_name)
                if level_2_skip_specs:
                    logger.info(
                        "Level-2 per-spec gate: %d spec(s) in-sync from cache: %s",
                        len(level_2_skip_specs),
                        sorted(level_2_skip_specs),
                    )

        # Per-spec convergence tracking (Level 3).
        per_spec_zero_drift: Dict[str, int] = {}
        per_spec_converged: Set[str] = set()

        # Initial skip set includes level-2 cache hits.
        skip_specs = level_2_skip_specs.copy()

        normal_exit = False

        try:
            round_index = start_round
            while round_index <= self.max_rounds:
                self._emit("round_start", round_index=round_index)

                project_context = self._build_context(collector)

                # Discovery: run every round until converged (or never on resume).
                if self.resume_from is not None:
                    do_discovery = False
                elif force_discovery:
                    do_discovery = True
                    force_discovery = False  # only force the first round
                else:
                    do_discovery = not discovery_converged

                # Build skip set for this round: level-2 cache hits + per-spec converged.
                round_skip = skip_specs | per_spec_converged

                round_result = engine.run_once(
                    round_index=round_index,
                    flow_ctx=flow_ctx,
                    llm_caller=llm_caller,
                    project_context=project_context,
                    specs=None,  # let SyncEngine reload each round
                    do_discovery=do_discovery,
                    progress_callback=self._wrap_progress(round_index),
                    skip_specs=round_skip if round_skip else None,
                )

                # Track discovery convergence: count consecutive rounds
                # that produced 0 new subsystems.
                if do_discovery:
                    if round_result.new_subsystems_count == 0:
                        discovery_stable_count += 1
                        if discovery_stable_count >= self.stable_rounds:
                            discovery_converged = True
                            logger.info(
                                "Discovery converged after %d consecutive "
                                "rounds with 0 new subsystems.",
                                discovery_stable_count,
                            )
                    else:
                        discovery_stable_count = 0

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

                # ── Per-spec convergence tracking (Level 3) ──────────────
                any_drift_this_round = False
                for analysis in round_result.analyses:
                    name = analysis.spec_name
                    if analysis.analysis_failed:
                        continue
                    if analysis.is_in_sync:
                        per_spec_zero_drift[name] = (
                            per_spec_zero_drift.get(name, 0) + 1
                        )
                        if (
                            per_spec_zero_drift[name]
                            >= self.stable_rounds
                        ):
                            per_spec_converged.add(name)
                    else:
                        per_spec_zero_drift[name] = 0
                        per_spec_converged.discard(name)
                        any_drift_this_round = True

                # ── Infrastructure failure tracking ───────────────────────
                infra_failed_count = sum(
                    1
                    for a in round_result.analyses
                    if getattr(a, "failed_analysis_reason", None)
                    == "infrastructure_failure"
                )
                if infra_failed_count > 0:
                    self._consecutive_infra_failures += 1
                else:
                    self._consecutive_infra_failures = 0

                if (
                    self._consecutive_infra_failures
                    >= self.infrastructure_failure_threshold
                ):
                    skip_specs = set()
                    should_continue = self._handle_infra_failure_threshold(
                        round_result=round_result,
                        loop_result=loop_result,
                        round_index=round_index,
                    )
                    if should_continue:
                        self._consecutive_infra_failures = 0
                        round_index += 1
                        continue
                    else:
                        cp_path = str(
                            _checkpoint_module.checkpoint_path(self.project_root)
                        )
                        loop_result.paused = True
                        loop_result.checkpoint_path = cp_path
                        self._emit(
                            "paused",
                            round_index=round_index,
                            checkpoint_path=cp_path,
                        )
                        return loop_result

                # ── Oscillation ───────────────────────────────────────────
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
                    normal_exit = True
                    break

                # ── Convergence ───────────────────────────────────────────
                # Compute the set of all specs seen across every round.
                all_specs_seen: Set[str] = set()
                for r in loop_result.rounds:
                    for a in r.analyses:
                        all_specs_seen.add(a.spec_name)
                all_specs_seen.update(level_2_skip_specs)

                # Collect specs that had a failed analysis in this round.
                specs_failed_this_round: Set[str] = set()
                for analysis in round_result.analyses:
                    if analysis.analysis_failed:
                        specs_failed_this_round.add(analysis.spec_name)

                # Per-spec convergence: every seen spec has individually
                # reached stable_rounds consecutive 0-drift rounds, OR has
                # a persistent failed analysis (doesn't block convergence
                # but prevents sync_state from being written — checked in
                # _write_sync_state). Discovery must also be converged.
                if all_specs_seen:
                    per_spec_done = all(
                        per_spec_zero_drift.get(name, 0)
                        >= self.stable_rounds
                        or name in specs_failed_this_round
                        for name in all_specs_seen
                    )
                    if per_spec_done and discovery_converged:
                        loop_result.converged = True
                        self._emit(
                            "converged",
                            round_index=round_index,
                            stable_rounds=self.stable_rounds,
                        )
                        normal_exit = True
                        break
                else:
                    # Fallback: traditional global stability for test
                    # fixtures that provide RoundResults with empty analyses.
                    if round_result.is_stable:
                        stable_count += 1
                        if stable_count >= self.stable_rounds:
                            loop_result.converged = True
                            self._emit(
                                "converged",
                                round_index=round_index,
                                stable_rounds=self.stable_rounds,
                            )
                            normal_exit = True
                            break
                    else:
                        stable_count = 0

                # After the first post-resume round, the skip set has served
                # its purpose; re-analyze normally from then on.
                skip_specs = set()

                round_index += 1
            else:
                # while loop completed without break: max_rounds exhausted.
                self._emit(
                    "max_rounds_exhausted",
                    round_index=self.max_rounds,
                    max_rounds=self.max_rounds,
                )
                normal_exit = True
        except KeyboardInterrupt:
            # The interrupt was raised from inside prompt_resume_or_exit or
            # any spec-update path. The checkpoint (if any) has already been
            # written by _handle_infra_failure_threshold; do NOT clear it.
            self._emit(
                "interrupted",
                checkpoint_path=str(
                    _checkpoint_module.checkpoint_path(self.project_root)
                ),
            )
            raise

        # Persist discovery convergence status on the instance so callers
        # (and future sync_state writing) can inspect it.
        self.discovery_converged = discovery_converged

        # ── Write sync_state on genuine convergence ──────────────────────
        if loop_result.converged and normal_exit:
            try:
                engine_specs = engine._load_specs()
            except Exception:
                engine_specs = {}
            self._write_sync_state(
                loop_result=loop_result,
                discovery_converged=discovery_converged,
                level_2_skip_specs=level_2_skip_specs,
                engine_specs=engine_specs,
            )

        if normal_exit:
            # Any clean exit path (converged, oscillation, max-rounds) clears
            # the checkpoint so the next `se3 sync` starts fresh.
            try:
                _checkpoint_module.clear(self.project_root)
            except Exception as exc:  # pragma: no cover — defensive
                logger.debug("Failed to clear sync checkpoint: %s", exc)

        try:
            flow_ctx.write_rounds_summary(loop_result)
        except Exception as e:
            logger.warning("Failed to persist sync rounds summary: %s", e)

        return loop_result

    # ------------------------------------------------------------------
    # SyncState persistence
    # ------------------------------------------------------------------

    def _write_sync_state(
        self,
        loop_result: LoopResult,
        discovery_converged: bool,
        level_2_skip_specs: set[str],
        engine_specs: Dict[str, Any] | None = None,
    ) -> None:
        """Write sync_state.json only when sync truly converged and there are
        no unresolved failed analyses."""
        # Do not write when there are unresolved failed analyses.
        for r in loop_result.rounds:
            for analysis in r.analyses:
                if analysis.analysis_failed:
                    logger.info(
                        "Not writing sync_state: spec '%s' has unresolved "
                        "failed analysis.",
                        analysis.spec_name,
                    )
                    return

        try:
            code_fp = _sync_state_module.compute_code_fingerprint(self.project_root)
        except Exception:
            logger.debug("Failed to compute code fingerprint; skipping sync_state")
            return

        # Union of per-spec deps across ALL rounds (union, not snapshot).
        deps_union: Dict[str, Set[str]] = {}
        for r in loop_result.rounds:
            for spec_name, files in r.per_spec_deps.items():
                if spec_name in level_2_skip_specs:
                    continue  # was never analyzed this sync, keep cached deps
                if spec_name not in deps_union:
                    deps_union[spec_name] = set()
                deps_union[spec_name].update(files)

        # Build spec_deps: hash each spec's current content + dep file hashes.
        spec_deps: Dict[str, Dict[str, Any]] = {}
        if deps_union:
            if engine_specs is None:
                try:
                    engine_specs = self._engine_specs()
                except Exception:
                    logger.debug("Failed to load specs for sync_state; skipping")
                    return
            for spec_name, files in deps_union.items():
                spec_info = engine_specs.get(spec_name)
                if not spec_info:
                    continue
                spec_path = spec_info.get("path")
                if not spec_path:
                    continue
                try:
                    content = Path(spec_path).read_text(encoding="utf-8")
                except OSError:
                    continue
                spec_hash = hashlib.sha256(
                    content.encode("utf-8")
                ).hexdigest()

                dep_hashes: Dict[str, str] = {}
                for rel_path in sorted(files):
                    h = _sync_state_module.compute_file_content_hash(
                        self.project_root / rel_path
                    )
                    if h:
                        dep_hashes[rel_path] = h

                spec_deps[spec_name] = {
                    "spec_hash": spec_hash,
                    "deps": dep_hashes,
                }

        # Carry forward cached spec deps that were never analyzed this sync.
        for spec_name in level_2_skip_specs:
            if spec_name in spec_deps:
                continue
            cached = _sync_state_module.load(self.project_root)
            if cached and spec_name in cached.spec_deps:
                spec_deps[spec_name] = dict(cached.spec_deps[spec_name])

        state = _sync_state_module.SyncState(
            converged_at=datetime.now(timezone.utc).isoformat(),
            code_fingerprint=code_fp,
            discovery_converged=discovery_converged,
            spec_deps=spec_deps,
            obsolete_specs=[],  # handled in G8
        )
        _sync_state_module.save(state, self.project_root)
        logger.info(
            "Wrote sync_state: fingerprint=%s, %d spec(s), discovery_converged=%s",
            code_fp[:12], len(spec_deps), discovery_converged,
        )

    def _engine_specs(self) -> Dict[str, Any]:
        """Return the spec dict from a fresh engine._load_specs() call."""
        engine = SyncEngine(self.project_root, interactive=self.interactive)
        return engine._load_specs()

    # ------------------------------------------------------------------
    # Infrastructure-failure handling
    # ------------------------------------------------------------------

    def _handle_infra_failure_threshold(
        self,
        round_result: RoundResult,
        loop_result: LoopResult,
        round_index: int,
    ) -> bool:
        """Write checkpoint, prompt user, and return ``True`` if loop should continue.

        Raises ``KeyboardInterrupt`` (propagated) if the user signals exit
        via Ctrl-C — the caller catches and returns the partial result.
        """
        # Build the set of in-sync specs from the LATEST analysis state per
        # spec across every round so far. Earlier rounds are not authoritative:
        # if a spec was reported in_sync in round 1 but later analyzed as
        # drifting in round 2, the round-2 verdict wins. Only specs whose
        # most recent successful analysis was ``is_in_sync=True`` are recorded
        # so resume re-analyzes anything where drift was last seen.
        latest_analysis_by_spec: Dict[str, Any] = {}
        latest_hash_by_spec: Dict[str, str] = {}
        for r in loop_result.rounds:
            for analysis in r.analyses:
                name = analysis.spec_name
                latest_analysis_by_spec[name] = analysis
                h = r.spec_hashes_after.get(name)
                if h:
                    latest_hash_by_spec[name] = h

        in_sync_hashes: Dict[str, str] = {}
        failed_reasons: Dict[str, str] = {}
        for name, analysis in latest_analysis_by_spec.items():
            if analysis.analysis_failed:
                failed_reasons[name] = (
                    analysis.failed_analysis_reason or "unknown"
                )
                continue
            if analysis.is_in_sync:
                h = latest_hash_by_spec.get(name)
                if h:
                    in_sync_hashes[name] = h

        checkpoint = SyncCheckpoint(
            round_index=round_index,
            max_rounds=self.max_rounds,
            in_sync_specs=in_sync_hashes,
            failed_analyses=failed_reasons,
            reason="quota_exhausted",
        )
        try:
            checkpoint_file = _checkpoint_module.save(checkpoint, self.project_root)
        except OSError as exc:
            logger.error("Failed to persist sync checkpoint: %s", exc)
            checkpoint_file = _checkpoint_module.checkpoint_path(self.project_root)

        stats: Dict[str, Any] = {
            "completed_specs": len(in_sync_hashes),
            "total_specs": len(round_result.analyses) or len(in_sync_hashes),
            "round_index": round_index,
            "max_rounds": self.max_rounds,
            "in_sync_specs": sorted(in_sync_hashes.keys()),
            "failure_count": self._consecutive_infra_failures,
            "checkpoint_path": str(checkpoint_file),
            "reason": "quota_exhausted",
        }
        self._emit(
            "infra_failure_pause",
            round_index=round_index,
            checkpoint_path=str(checkpoint_file),
            stats=stats,
        )

        try:
            decision = self._prompt_resume_or_exit(stats)
        except KeyboardInterrupt:
            # Propagate; outer try/except handles the emit and re-raise.
            raise

        if decision == "continue":
            return True
        return False

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
