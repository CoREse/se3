"""Tests for strategy alignment: fast converges to ACCEPT/REJECT, strict placeholder."""

from __future__ import annotations

import pytest

from se3.engine.merge.conflict_resolver import (
    Confidence,
    FileResolution,
    HunkResolution,
    LLMResolution,
    MergeStrategy,
)
from se3.engine.merge.strategy import DecisionAction, StrategyDecider


def _make_resolution(
    path: str = "shared.txt",
    resolved_content: str = "resolved\n",
    overall_confidence: Confidence = Confidence.HIGH,
    hunk_confidence: Confidence = Confidence.HIGH,
    requires_human_review: bool = False,
    spec_guardrail_concern: bool = False,
    is_spec: bool = False,
) -> LLMResolution:
    """Build a mock LLMResolution."""
    return LLMResolution(
        files=[
            FileResolution(
                path=path,
                resolved_content=resolved_content,
                hunks=[
                    HunkResolution(
                        start_line=1,
                        end_line=5,
                        confidence=hunk_confidence,
                        reasoning="test reasoning",
                    ),
                ],
                overall_confidence=overall_confidence,
                flags={
                    "requires_human_review": requires_human_review,
                    "spec_guardrail_concern": spec_guardrail_concern,
                },
                is_spec=is_spec,
            ),
        ],
        overall_confidence=overall_confidence,
        flags={
            "requires_human_review": requires_human_review,
            "spec_guardrail_concern": spec_guardrail_concern,
        },
    )


class TestFastStrategyAcceptOrRejectOnly:
    """Fast strategy must never return HUMAN_CALL — only ACCEPT or REJECT."""

    def test_fast_never_returns_human_call_across_various_inputs(self) -> None:
        """Property: _decide_fast never emits HUMAN_CALL for any LLM output."""
        decider = StrategyDecider()

        test_cases = [
            # (overall_conf, hunk_conf, is_spec, spec_guardrail, req_human)
            (Confidence.HIGH, Confidence.HIGH, False, False, False),
            (Confidence.HIGH, Confidence.HIGH, True, False, False),
            (Confidence.LOW, Confidence.LOW, False, False, False),
            (Confidence.LOW, Confidence.LOW, True, False, False),
            (Confidence.MEDIUM, Confidence.MEDIUM, False, True, False),
            (Confidence.MEDIUM, Confidence.MEDIUM, True, True, False),
            (Confidence.HIGH, Confidence.HIGH, False, True, False),
            (Confidence.HIGH, Confidence.HIGH, True, True, False),
            (Confidence.HIGH, Confidence.HIGH, False, False, True),
            (Confidence.HIGH, Confidence.HIGH, True, False, True),
        ]

        for overall_c, hunk_c, is_spec, sgc, rhr in test_cases:
            resolution = _make_resolution(
                overall_confidence=overall_c,
                hunk_confidence=hunk_c,
                is_spec=is_spec,
                spec_guardrail_concern=sgc,
                requires_human_review=rhr,
            )
            decision = decider.decide(
                resolution,
                has_spec_files=is_spec,
                strategy=MergeStrategy.FAST,
            )
            assert decision.action != DecisionAction.HUMAN_CALL, (
                f"fast returned HUMAN_CALL for: "
                f"overall={overall_c.value}, hunk={hunk_c.value}, "
                f"is_spec={is_spec}, sgc={sgc}, rhr={rhr}"
            )

    def test_fast_regular_low_confidence_accept(self) -> None:
        """Low confidence on regular file in fast mode → ACCEPT."""
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.LOW,
            hunk_confidence=Confidence.LOW,
            is_spec=False,
        )
        decision = decider.decide(
            resolution, has_spec_files=False, strategy=MergeStrategy.FAST,
        )
        assert decision.action == DecisionAction.ACCEPT

    def test_fast_regular_medium_confidence_accept(self) -> None:
        """Medium confidence on regular file in fast mode → ACCEPT."""
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.MEDIUM,
            hunk_confidence=Confidence.MEDIUM,
            is_spec=False,
        )
        decision = decider.decide(
            resolution, has_spec_files=False, strategy=MergeStrategy.FAST,
        )
        assert decision.action == DecisionAction.ACCEPT

    def test_fast_regular_requires_human_review_accept(self) -> None:
        """requires_human_review on regular file in fast mode → ACCEPT.

        The flag is only checked at global level; per-file requires_human_review
        on non-spec files is intentionally ignored in fast mode.
        """
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.LOW,
            is_spec=False,
            requires_human_review=False,
        )
        # Set per-file flag (global is False)
        resolution.files[0].flags["requires_human_review"] = True
        decision = decider.decide(
            resolution, has_spec_files=False, strategy=MergeStrategy.FAST,
        )
        assert decision.action == DecisionAction.ACCEPT

    def test_fast_global_requires_human_review_reject(self) -> None:
        """Global requires_human_review in fast mode → REJECT (abort, no human call)."""
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            is_spec=False,
            requires_human_review=True,
        )
        decision = decider.decide(
            resolution, has_spec_files=False, strategy=MergeStrategy.FAST,
        )
        assert decision.action == DecisionAction.REJECT
        assert "fast strategy aborts" in decision.reason.lower()
        assert "no human call" in decision.reason.lower()

    def test_fast_spec_guardrail_concern_deferred_accept(self) -> None:
        """spec_guardrail_concern on spec file → ACCEPT (deferred to post-merge)."""
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            hunk_confidence=Confidence.HIGH,
            is_spec=True,
            spec_guardrail_concern=True,
        )
        decision = decider.decide(
            resolution, has_spec_files=True, strategy=MergeStrategy.FAST,
        )
        assert decision.action == DecisionAction.ACCEPT
        assert "deferred" in decision.reason.lower()

    def test_fast_spec_low_confidence_reject(self) -> None:
        """Low confidence on spec file in fast mode → REJECT."""
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            hunk_confidence=Confidence.HIGH,
            is_spec=True,
        )
        resolution.files[0].overall_confidence = Confidence.LOW
        decision = decider.decide(
            resolution, has_spec_files=True, strategy=MergeStrategy.FAST,
        )
        assert decision.action == DecisionAction.REJECT
        assert "fast strategy aborts" in decision.reason.lower()

    def test_fast_spec_medium_confidence_reject(self) -> None:
        """Medium confidence on spec file in fast mode → REJECT."""
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            hunk_confidence=Confidence.HIGH,
            is_spec=True,
        )
        resolution.files[0].overall_confidence = Confidence.MEDIUM
        decision = decider.decide(
            resolution, has_spec_files=True, strategy=MergeStrategy.FAST,
        )
        assert decision.action == DecisionAction.REJECT

    def test_fast_spec_requires_human_review_reject(self) -> None:
        """requires_human_review on spec file in fast mode → REJECT."""
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            hunk_confidence=Confidence.HIGH,
            is_spec=True,
            requires_human_review=True,
        )
        decision = decider.decide(
            resolution, has_spec_files=True, strategy=MergeStrategy.FAST,
        )
        assert decision.action == DecisionAction.REJECT
        assert "fast strategy aborts" in decision.reason.lower()

    def test_fast_spec_per_file_requires_human_review_reject(self) -> None:
        """Per-file requires_human_review on spec file (global False) → REJECT.

        This is the safety-critical path at strategy.py:241-244: when the
        global requires_human_review flag is False but a spec file sets it
        per-file, fast mode must still REJECT. The global path (line 220-224)
        is NOT reached here, so the per-file check is what actually fires.
        """
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            hunk_confidence=Confidence.HIGH,
            is_spec=True,
            requires_human_review=False,  # global is False
        )
        # Set per-file flag only — this must trigger the spec-file REJECT path
        resolution.files[0].flags["requires_human_review"] = True
        decision = decider.decide(
            resolution, has_spec_files=True, strategy=MergeStrategy.FAST,
        )
        assert decision.action == DecisionAction.REJECT
        assert "requires_human_review on spec file" in decision.reason
        assert "fast strategy aborts" in decision.reason.lower()

    def test_fast_mixed_regular_and_spec_accept(self) -> None:
        """Mixed regular + spec files, spec has high confidence → ACCEPT."""
        decider = StrategyDecider()
        file_regular = FileResolution(
            path="regular.txt",
            resolved_content="ok",
            hunks=[HunkResolution(1, 3, Confidence.LOW, "low")],
            overall_confidence=Confidence.LOW,
            flags={},
            is_spec=False,
        )
        file_spec = FileResolution(
            path="se3/specs/test/spec.md",
            resolved_content="ok",
            hunks=[HunkResolution(1, 3, Confidence.HIGH, "high")],
            overall_confidence=Confidence.HIGH,
            flags={},
            is_spec=True,
        )
        resolution = LLMResolution(
            files=[file_regular, file_spec],
            overall_confidence=Confidence.HIGH,
            flags={},
        )
        decision = decider.decide(
            resolution, has_spec_files=True, strategy=MergeStrategy.FAST,
        )
        assert decision.action == DecisionAction.ACCEPT

    def test_fast_mixed_regular_and_spec_low_spec_reject(self) -> None:
        """Mixed regular + spec files, spec has LOW confidence → REJECT."""
        decider = StrategyDecider()
        file_regular = FileResolution(
            path="regular.txt",
            resolved_content="ok",
            hunks=[HunkResolution(1, 3, Confidence.HIGH, "high")],
            overall_confidence=Confidence.HIGH,
            flags={},
            is_spec=False,
        )
        file_spec = FileResolution(
            path="se3/specs/test/spec.md",
            resolved_content="ok",
            hunks=[HunkResolution(1, 3, Confidence.LOW, "low")],
            overall_confidence=Confidence.LOW,
            flags={},
            is_spec=True,
        )
        resolution = LLMResolution(
            files=[file_regular, file_spec],
            overall_confidence=Confidence.HIGH,
            flags={},
        )
        decision = decider.decide(
            resolution, has_spec_files=True, strategy=MergeStrategy.FAST,
        )
        assert decision.action == DecisionAction.REJECT


class TestStrictStrategyPlaceholder:
    """Strict strategy is short-circuited by orchestrator; kept as fallback."""

    def test_strict_docstring_mentions_orchestrator_short_circuit(self) -> None:
        """The docstring should document that orchestrator short-circuits strict."""
        decider = StrategyDecider()
        doc = decider._decide_strict.__doc__ or ""
        assert "orchestrator" in doc.lower()
        assert "short-circuit" in doc.lower() or "placeholder" in doc.lower()

    def test_strict_fallback_all_high_accept(self) -> None:
        """Fallback path: all high confidence + no flags → ACCEPT."""
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            hunk_confidence=Confidence.HIGH,
        )
        decision = decider.decide(
            resolution, has_spec_files=False, strategy=MergeStrategy.STRICT,
        )
        assert decision.action == DecisionAction.ACCEPT

    def test_strict_fallback_low_hunk_human_call(self) -> None:
        """Fallback path: low hunk confidence → HUMAN_CALL (not REJECT)."""
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            hunk_confidence=Confidence.LOW,
        )
        decision = decider.decide(
            resolution, has_spec_files=False, strategy=MergeStrategy.STRICT,
        )
        assert decision.action == DecisionAction.HUMAN_CALL

    def test_strict_fallback_spec_guardrail_human_call(self) -> None:
        """Fallback path: spec_guardrail_concern → HUMAN_CALL."""
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            spec_guardrail_concern=True,
        )
        decision = decider.decide(
            resolution, has_spec_files=True, strategy=MergeStrategy.STRICT,
        )
        assert decision.action == DecisionAction.HUMAN_CALL


class TestDefaultStrategyUnchanged:
    """Default strategy behavior is unchanged by this alignment work."""

    def test_default_high_confidence_accept(self) -> None:
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            hunk_confidence=Confidence.HIGH,
        )
        decision = decider.decide(
            resolution, has_spec_files=False, strategy=MergeStrategy.DEFAULT,
        )
        assert decision.action == DecisionAction.ACCEPT

    def test_default_low_overall_human_call(self) -> None:
        decider = StrategyDecider()
        resolution = _make_resolution(overall_confidence=Confidence.LOW)
        decision = decider.decide(
            resolution, has_spec_files=False, strategy=MergeStrategy.DEFAULT,
        )
        assert decision.action == DecisionAction.HUMAN_CALL

    def test_default_spec_guardrail_human_call(self) -> None:
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            spec_guardrail_concern=True,
        )
        decision = decider.decide(
            resolution, has_spec_files=True, strategy=MergeStrategy.DEFAULT,
        )
        assert decision.action == DecisionAction.HUMAN_CALL

    def test_default_requires_human_review_human_call(self) -> None:
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            requires_human_review=True,
        )
        decision = decider.decide(
            resolution, has_spec_files=False, strategy=MergeStrategy.DEFAULT,
        )
        assert decision.action == DecisionAction.HUMAN_CALL
