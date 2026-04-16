"""Tests for LLMCaller._call_two_phase required_keys validation.

Tests cover:
- Fast path: valid JSON with matching required_keys → skip Phase 2
- Fast path: valid JSON missing required_keys → fallback to Phase 2
- Fast path: required_keys=None → same as before (no key check)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from se3.engine.llm_caller import LLMCaller


def _make_caller(**kwargs) -> LLMCaller:
    """Create an LLMCaller with defaults suitable for testing."""
    defaults = dict(
        project_root=Path("/tmp/test"),
        max_retries=1,
        flow_id="test-flow",
        step_id="test-step",
        step_type="self_check",
        agents=[{"name": "test", "type": "claude-code", "cmd": "echo"}],
    )
    defaults.update(kwargs)
    return LLMCaller(**defaults)


class TestCallTwoPhaseRequiredKeys:
    """Test required_keys validation in the two-phase fast path."""

    @patch.object(LLMCaller, "_call_with_retry")
    @patch.object(LLMCaller, "_get_phase1_cache_path", return_value=None)
    def test_fast_path_with_matching_required_keys(self, _mock_cache, mock_retry):
        """When Phase 1 returns valid JSON containing required_keys, skip Phase 2."""
        phase1_json = json.dumps({"issues": [], "summary": "All good"})
        mock_retry.return_value = phase1_json

        caller = _make_caller()
        result = caller._call_two_phase(
            prompt="test",
            timeout=None,
            context_files=None,
            on_output=None,
            json_schema_hint=None,
            required_keys=["issues"],
        )

        parsed = json.loads(result)
        assert "issues" in parsed
        # _call_with_retry called once (Phase 1 only, no Phase 2)
        assert mock_retry.call_count == 1

    @patch.object(LLMCaller, "_call_with_retry")
    @patch.object(LLMCaller, "_get_phase1_cache_path", return_value=None)
    def test_fast_path_missing_required_keys_falls_back_to_phase2(
        self, _mock_cache, mock_retry
    ):
        """When Phase 1 JSON is valid but missing required_keys, fall back to Phase 2."""
        # Phase 1 returns valid JSON but without the "issues" key
        phase1_json = json.dumps({"summary": "All good"})
        mock_retry.return_value = phase1_json

        # Phase 2 extractor returns the complete JSON
        mock_extractor = MagicMock()
        mock_extractor.extract.return_value = {"issues": [], "summary": "All good"}

        with patch("se3.engine.json_extractor.JSONExtractor", return_value=mock_extractor) as mock_cls:
            caller = _make_caller()
            result = caller._call_two_phase(
                prompt="test",
                timeout=None,
                context_files=None,
                on_output=None,
                json_schema_hint=None,
                required_keys=["issues"],
            )

        parsed = json.loads(result)
        assert "issues" in parsed
        # Phase 2 extractor was called
        mock_extractor.extract.assert_called_once()
        # required_keys was passed to extractor
        call_kwargs = mock_extractor.extract.call_args
        assert call_kwargs.kwargs.get("required_keys") == ["issues"]

    @patch.object(LLMCaller, "_call_with_retry")
    @patch.object(LLMCaller, "_get_phase1_cache_path", return_value=None)
    def test_fast_path_with_none_required_keys(self, _mock_cache, mock_retry):
        """When required_keys=None, fast path accepts any valid JSON (backward compat)."""
        phase1_json = json.dumps({"summary": "All good"})
        mock_retry.return_value = phase1_json

        caller = _make_caller()
        result = caller._call_two_phase(
            prompt="test",
            timeout=None,
            context_files=None,
            on_output=None,
            json_schema_hint=None,
            required_keys=None,
        )

        parsed = json.loads(result)
        assert "summary" in parsed
        # Phase 1 only — no Phase 2 needed
        assert mock_retry.call_count == 1


class TestCallPassesRequiredKeys:
    """Test that call() passes required_keys to _call_two_phase()."""

    @patch.object(LLMCaller, "_call_two_phase")
    def test_call_passes_required_keys_to_two_phase(self, mock_two_phase):
        """call() with json_mode='two_phase' passes required_keys through."""
        mock_two_phase.return_value = '{"issues": []}'

        caller = _make_caller()
        caller.call(
            prompt="test",
            json_mode="two_phase",
            required_keys=["issues"],
        )

        call_kwargs = mock_two_phase.call_args.kwargs
        assert call_kwargs["required_keys"] == ["issues"]
