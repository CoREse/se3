"""Failure-path tests for SyncAnalyzer + RoundResult.is_stable (G4).

Covers the G4 contract change: JSON parse errors and exhausted retries
SHALL NOT fabricate a CONFLICT diff. They SHALL instead populate
``SpecAnalysis.failed_analysis_reason`` and leave ``diffs`` empty so the
sync loop can mark the spec as failed-but-not-drifting and still
converge.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from se3.engine.llm_caller import LLMCallError
from se3.engine.sync_analyzer import (
    SyncAnalyzer,
    _REASON_INFRASTRUCTURE,
    _REASON_OUTPUT_FORMAT,
)
from se3.engine.sync_engine import (
    DiffType,
    RoundResult,
    SpecAnalysis,
    SpecDiff,
)


# ---------------------------------------------------------------------------
# Analyzer failure paths — no more fabricated CONFLICT diffs.
# ---------------------------------------------------------------------------


class TestAnalyzerJsonParseFailures:
    def test_malformed_non_empty_json_is_output_format_error(self, tmp_path):
        caller = MagicMock()
        caller.call.return_value = "not json {"
        analyzer = SyncAnalyzer(tmp_path, caller)

        result = analyzer.analyze_spec("auth", "spec", "ctx")

        # No fabricated CONFLICT — diffs are empty.
        assert result.diffs == []
        assert not any(d.diff_type == DiffType.CONFLICT for d in result.diffs)
        # The reason classifies this as an LLM-side format violation.
        assert result.failed_analysis_reason == _REASON_OUTPUT_FORMAT
        assert result.analysis_failed is True
        # is_in_sync is unchanged: empty diffs still means "no drift seen".
        assert result.is_in_sync is True
        assert result.spec_name == "auth"

    def test_empty_response_is_infrastructure_failure(self, tmp_path):
        caller = MagicMock()
        caller.call.return_value = ""
        analyzer = SyncAnalyzer(tmp_path, caller)

        result = analyzer.analyze_spec("auth", "spec", "ctx")

        assert result.diffs == []
        assert result.failed_analysis_reason == _REASON_INFRASTRUCTURE
        assert result.analysis_failed is True

    def test_whitespace_only_response_is_infrastructure_failure(self, tmp_path):
        caller = MagicMock()
        caller.call.return_value = "   \n\n  "
        analyzer = SyncAnalyzer(tmp_path, caller)

        result = analyzer.analyze_spec("auth", "spec", "ctx")

        assert result.diffs == []
        assert result.failed_analysis_reason == _REASON_INFRASTRUCTURE

    def test_exhausted_retries_is_infrastructure_failure(self, tmp_path):
        caller = MagicMock()
        caller.call.side_effect = LLMCallError("persistent network error")
        analyzer = SyncAnalyzer(tmp_path, caller)

        result = analyzer.analyze_spec("broken", "spec", "ctx")

        assert caller.call.call_count == 3
        assert result.diffs == []
        assert not any(d.diff_type == DiffType.CONFLICT for d in result.diffs)
        assert result.failed_analysis_reason == _REASON_INFRASTRUCTURE
        assert result.analysis_failed is True
        assert result.spec_name == "broken"

    def test_parse_response_directly_malformed(self, tmp_path):
        analyzer = SyncAnalyzer(tmp_path, MagicMock())
        result = analyzer._parse_analysis_response("spec", '{"diffs": [{"type"')

        assert result.diffs == []
        assert result.failed_analysis_reason == _REASON_OUTPUT_FORMAT


# ---------------------------------------------------------------------------
# RoundResult.is_stable — failed analyses MUST NOT block convergence.
# ---------------------------------------------------------------------------


class TestIsStableWithFailedAnalyses:
    def test_in_sync_plus_failed_is_stable(self):
        rr = RoundResult(round_index=1)
        rr.specs_updated = 0
        rr.analyses.append(SpecAnalysis(spec_name="auth", diffs=[]))
        rr.analyses.append(
            SpecAnalysis(
                spec_name="storage",
                diffs=[],
                failed_analysis_reason=_REASON_INFRASTRUCTURE,
            )
        )
        assert rr.is_stable is True

    def test_real_drift_still_blocks_stability(self):
        rr = RoundResult(round_index=1)
        rr.specs_updated = 0
        rr.analyses.append(
            SpecAnalysis(
                spec_name="auth",
                diffs=[SpecDiff(DiffType.GAP, "auth", "Unresolved drift")],
            )
        )
        assert rr.is_stable is False

    def test_all_failed_round_is_stable(self):
        rr = RoundResult(round_index=1)
        rr.specs_updated = 0
        rr.analyses.append(
            SpecAnalysis(
                spec_name="a",
                diffs=[],
                failed_analysis_reason=_REASON_OUTPUT_FORMAT,
            )
        )
        rr.analyses.append(
            SpecAnalysis(
                spec_name="b",
                diffs=[],
                failed_analysis_reason=_REASON_INFRASTRUCTURE,
            )
        )
        assert rr.is_stable is True

    def test_specs_updated_still_blocks_stability(self):
        rr = RoundResult(round_index=1)
        rr.specs_updated = 1
        rr.analyses.append(SpecAnalysis(spec_name="auth", diffs=[]))
        assert rr.is_stable is False


class TestSpecAnalysisSerialization:
    def test_failed_reason_roundtrips(self):
        a = SpecAnalysis(
            spec_name="auth",
            diffs=[],
            failed_analysis_reason=_REASON_INFRASTRUCTURE,
        )
        restored = SpecAnalysis.from_dict(a.to_dict())
        assert restored.failed_analysis_reason == _REASON_INFRASTRUCTURE
        assert restored.analysis_failed is True

    def test_default_none_does_not_appear_in_dict(self):
        a = SpecAnalysis(spec_name="auth", diffs=[])
        d = a.to_dict()
        assert "failed_analysis_reason" not in d
        restored = SpecAnalysis.from_dict(d)
        assert restored.failed_analysis_reason is None
        assert restored.analysis_failed is False
