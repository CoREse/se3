"""Tests for strategy alignment: fast converges to ACCEPT/REJECT, strict placeholder.

The spec-guardrails chain has been removed, so ``StrategyDecider.decide``
no longer takes ``has_spec_files`` and no strategy tier gates on spec
paths or a ``spec_guardrail_concern`` flag. The only escalation signal
left is the explicit ``requires_human_review`` flag (global or per-file),
plus per-hunk confidence in the strict fallback.
"""

from __future__ import annotations

from tianluo.engine.merge.conflict_resolver import (
    Confidence,
    FileResolution,
    HunkResolution,
    LLMResolution,
    MergeStrategy,
)
from tianluo.engine.merge.strategy import DecisionAction, StrategyDecider


def _make_resolution(
    path: str = "shared.txt",
    resolved_content: str = "resolved\n",
    overall_confidence: Confidence = Confidence.HIGH,
    hunk_confidence: Confidence = Confidence.HIGH,
    requires_human_review: bool = False,
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
                flags={"requires_human_review": requires_human_review},
            ),
        ],
        overall_confidence=overall_confidence,
        flags={"requires_human_review": requires_human_review},
    )


class TestFastStrategyAcceptOrRejectOnly:
    """Fast strategy must never return HUMAN_CALL — only ACCEPT or REJECT."""

    def test_fast_never_returns_human_call_across_various_inputs(self) -> None:
        """Property: _decide_fast never emits HUMAN_CALL for any LLM output."""
        decider = StrategyDecider()

        test_cases = [
            # (overall_conf, hunk_conf, global_requires_human_review)
            (Confidence.HIGH, Confidence.HIGH, False),
            (Confidence.MEDIUM, Confidence.MEDIUM, False),
            (Confidence.LOW, Confidence.LOW, False),
            (Confidence.HIGH, Confidence.LOW, False),
            (Confidence.HIGH, Confidence.HIGH, True),
            (Confidence.LOW, Confidence.LOW, True),
        ]

        for overall_c, hunk_c, rhr in test_cases:
            for per_file_rhr in (False, True):
                resolution = _make_resolution(
                    overall_confidence=overall_c,
                    hunk_confidence=hunk_c,
                    requires_human_review=rhr,
                )
                resolution.files[0].flags["requires_human_review"] = per_file_rhr
                decision = decider.decide(
                    resolution, strategy=MergeStrategy.FAST,
                )
                assert decision.action != DecisionAction.HUMAN_CALL, (
                    f"fast returned HUMAN_CALL for: "
                    f"overall={overall_c.value}, hunk={hunk_c.value}, "
                    f"global_rhr={rhr}, per_file_rhr={per_file_rhr}"
                )
                # The global flag is the ONLY reject gate in fast mode.
                expected = (
                    DecisionAction.REJECT if rhr else DecisionAction.ACCEPT
                )
                assert decision.action == expected

    def test_fast_low_confidence_accept(self) -> None:
        """Low confidence in fast mode → ACCEPT (confidence is informational)."""
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.LOW,
            hunk_confidence=Confidence.LOW,
        )
        decision = decider.decide(resolution, strategy=MergeStrategy.FAST)
        assert decision.action == DecisionAction.ACCEPT
        assert decision.reason == "Fast strategy: accepted"

    def test_fast_medium_confidence_accept(self) -> None:
        """Medium confidence in fast mode → ACCEPT."""
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.MEDIUM,
            hunk_confidence=Confidence.MEDIUM,
        )
        decision = decider.decide(resolution, strategy=MergeStrategy.FAST)
        assert decision.action == DecisionAction.ACCEPT
        assert decision.reason == "Fast strategy: accepted"

    def test_fast_per_file_requires_human_review_accepted_with_warning(
        self,
    ) -> None:
        """Per-file requires_human_review in fast mode → ACCEPT + warning.

        Fast mode's contract is to never park a merge waiting on a human,
        so a per-file flag no longer rejects; it is accepted but the flag
        is surfaced in the decision reason so the operator can audit it.
        """
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.LOW,
            path="regular.txt",
            requires_human_review=False,  # global is False
        )
        resolution.files[0].flags["requires_human_review"] = True
        decision = decider.decide(resolution, strategy=MergeStrategy.FAST)
        assert decision.action == DecisionAction.ACCEPT
        assert decision.reason == (
            "Fast strategy: accepted; "
            "WARNING: requires_human_review on regular.txt"
        )

    def test_fast_global_requires_human_review_reject(self) -> None:
        """Global requires_human_review in fast mode → REJECT (abort, no human call)."""
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            requires_human_review=True,
        )
        decision = decider.decide(resolution, strategy=MergeStrategy.FAST)
        assert decision.action == DecisionAction.REJECT
        assert "fast strategy aborts" in decision.reason.lower()
        assert "no human call" in decision.reason.lower()

    def test_fast_mixed_confidence_files_accept(self) -> None:
        """Several files with mixed confidence and no flags → ACCEPT."""
        decider = StrategyDecider()
        file_low = FileResolution(
            path="regular.txt",
            resolved_content="ok",
            hunks=[HunkResolution(1, 3, Confidence.LOW, "low")],
            overall_confidence=Confidence.LOW,
            flags={},
        )
        file_high = FileResolution(
            path="docs/notes.md",
            resolved_content="ok",
            hunks=[HunkResolution(1, 3, Confidence.HIGH, "high")],
            overall_confidence=Confidence.HIGH,
            flags={},
        )
        resolution = LLMResolution(
            files=[file_low, file_high],
            overall_confidence=Confidence.HIGH,
            flags={},
        )
        decision = decider.decide(resolution, strategy=MergeStrategy.FAST)
        assert decision.action == DecisionAction.ACCEPT
        assert decision.reason == "Fast strategy: accepted"


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
        decision = decider.decide(resolution, strategy=MergeStrategy.STRICT)
        assert decision.action == DecisionAction.ACCEPT
        assert decision.reason == "All hunks high confidence (strict strategy)"

    def test_strict_fallback_low_hunk_human_call(self) -> None:
        """Fallback path: low hunk confidence → HUMAN_CALL (not REJECT)."""
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            hunk_confidence=Confidence.LOW,
        )
        decision = decider.decide(resolution, strategy=MergeStrategy.STRICT)
        assert decision.action == DecisionAction.HUMAN_CALL

    def test_strict_fallback_global_requires_human_review_human_call(self) -> None:
        """Fallback path: global requires_human_review → HUMAN_CALL."""
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            requires_human_review=True,
        )
        decision = decider.decide(resolution, strategy=MergeStrategy.STRICT)
        assert decision.action == DecisionAction.HUMAN_CALL
        assert decision.reason == (
            "requires_human_review flag set (strict strategy)"
        )

    def test_strict_fallback_per_file_requires_human_review_human_call(self) -> None:
        """Fallback path: per-file requires_human_review → HUMAN_CALL."""
        decider = StrategyDecider()
        resolution = _make_resolution(
            path="regular.txt",
            overall_confidence=Confidence.HIGH,
            requires_human_review=False,  # global is False
        )
        resolution.files[0].flags["requires_human_review"] = True
        decision = decider.decide(resolution, strategy=MergeStrategy.STRICT)
        assert decision.action == DecisionAction.HUMAN_CALL
        assert "regular.txt" in decision.reason


class TestSafeStrategy:
    """Safe strategy gates on the explicit human-review flag only."""

    def test_safe_high_confidence_accept(self) -> None:
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            hunk_confidence=Confidence.HIGH,
        )
        decision = decider.decide(resolution, strategy=MergeStrategy.SAFE)
        assert decision.action == DecisionAction.ACCEPT
        assert decision.reason == "No human-review flags set (safe strategy)"

    def test_safe_low_overall_accepted_without_flags(self) -> None:
        """Confidence rating is informational under the LLM-as-editor model.

        Safe strategy only gates on the explicit ``requires_human_review``
        flag; a LOW-confidence resolution without flags is accepted because
        the cleared-marker scan is the only success signal.
        """
        decider = StrategyDecider()
        resolution = _make_resolution(overall_confidence=Confidence.LOW)
        decision = decider.decide(resolution, strategy=MergeStrategy.SAFE)
        assert decision.action == DecisionAction.ACCEPT
        assert decision.reason == "No human-review flags set (safe strategy)"

    def test_safe_global_requires_human_review_human_call(self) -> None:
        decider = StrategyDecider()
        resolution = _make_resolution(
            overall_confidence=Confidence.HIGH,
            requires_human_review=True,
        )
        decision = decider.decide(resolution, strategy=MergeStrategy.SAFE)
        assert decision.action == DecisionAction.HUMAN_CALL
        assert decision.reason == (
            "requires_human_review flag set (safe strategy)"
        )

    def test_safe_per_file_requires_human_review_human_call(self) -> None:
        """A per-file flag alone still escalates under safe."""
        decider = StrategyDecider()
        resolution = _make_resolution(
            path="regular.txt",
            overall_confidence=Confidence.HIGH,
            requires_human_review=False,  # global is False
        )
        resolution.files[0].flags["requires_human_review"] = True
        decision = decider.decide(resolution, strategy=MergeStrategy.SAFE)
        assert decision.action == DecisionAction.HUMAN_CALL
        assert "regular.txt" in decision.reason
