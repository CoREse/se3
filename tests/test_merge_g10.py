"""Regression tests for G10 cross-cutting concerns (K2–K12).

Covers:
- K2: LLMTrace integration in ConflictResolver / GuardrailRepairer
- K3: Log level calibration (error paths use ERROR, not INFO)
- K4: SecretRedact in LLM trace prompts
- K5/K6: Empty repo, detached HEAD, shallow clone detection
- K9: mkstemp temp file cleanup on failure
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from se3.commands.merge.llm_trace import LLMTrace
from se3.engine.merge.conflict_resolver import ConflictResolver
from se3.engine.merge.guardrail_repair import GuardrailRepairer
from se3.engine.merge.human_call import _atomic_write_json
from se3.engine.merge.orchestrator import (
    DetachedHeadError,
    EmptyRepoError,
    MergeOrchestrator,
    ShallowRepoError,
    _check_repo_state,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )


def _commit(path: Path, message: str = "commit") -> None:
    subprocess.run(
        ["git", "-C", str(path), "add", "-A"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", message],
        check=True, capture_output=True,
    )


# ---------------------------------------------------------------------------
# K2: LLMTrace integration
# ---------------------------------------------------------------------------


def test_conflict_resolver_records_llm_trace(tmp_path: Path) -> None:
    """ConflictResolver._call_llm writes to LLMTrace when injected."""
    trace = MagicMock(spec=LLMTrace)
    trace.record = MagicMock(return_value=1)

    resolver = ConflictResolver(tmp_path, llm_trace=trace)

    # Mock the LLM caller to avoid real subprocess
    resolver._llm_caller = MagicMock()
    resolver._llm_caller.call = MagicMock(return_value='{"files": []}')

    result = resolver._call_llm("test prompt")

    assert result == '{"files": []}'
    trace.record.assert_called_once()
    call_kwargs = trace.record.call_args.kwargs
    assert call_kwargs["agent"] == "conflict_resolver"
    assert call_kwargs["prompt"] == "test prompt"
    assert call_kwargs["outcome"] == "success"
    assert call_kwargs["duration_sec"] >= 0.0


def test_conflict_resolver_records_error_in_trace(tmp_path: Path) -> None:
    """When LLM call fails, the trace records the error."""
    trace = MagicMock(spec=LLMTrace)
    trace.record = MagicMock(return_value=1)

    resolver = ConflictResolver(tmp_path, llm_trace=trace)
    resolver._llm_caller = MagicMock()
    resolver._llm_caller.call = MagicMock(side_effect=RuntimeError("llm boom"))

    with pytest.raises(RuntimeError, match="llm boom"):
        resolver._call_llm("test prompt")

    trace.record.assert_called_once()
    call_kwargs = trace.record.call_args.kwargs
    assert call_kwargs["outcome"] == "error"
    assert "llm boom" in call_kwargs["error"]


def test_guardrail_repairer_records_llm_trace(tmp_path: Path) -> None:
    """GuardrailRepairer._call_llm writes to LLMTrace when injected."""
    trace = MagicMock(spec=LLMTrace)
    trace.record = MagicMock(return_value=1)

    repairer = GuardrailRepairer(tmp_path, llm_trace=trace)
    repairer._llm_caller = MagicMock()
    repairer._llm_caller.call = MagicMock(return_value='{"files": []}')

    result = repairer._call_llm("repair prompt")

    assert result == {"files": []}  # parsed JSON dict
    trace.record.assert_called_once()
    call_kwargs = trace.record.call_args.kwargs
    assert call_kwargs["agent"] == "guardrail_repair"
    assert call_kwargs["outcome"] == "success"


# ---------------------------------------------------------------------------
# K4: SecretRedact in traces
# ---------------------------------------------------------------------------


def test_conflict_resolver_redacts_secrets_in_trace(tmp_path: Path) -> None:
    """Prompts containing API keys are redacted before trace recording."""
    trace = MagicMock(spec=LLMTrace)
    trace.record = MagicMock(return_value=1)

    resolver = ConflictResolver(tmp_path, llm_trace=trace)
    resolver._llm_caller = MagicMock()
    resolver._llm_caller.call = MagicMock(return_value="ok")

    prompt_with_secret = "api_key = sk-abc123def456"
    resolver._call_llm(prompt_with_secret)

    recorded_prompt = trace.record.call_args.kwargs["prompt"]
    assert "sk-abc123def456" not in recorded_prompt
    assert "sk-***" in recorded_prompt


# ---------------------------------------------------------------------------
# K3: Log level calibration
# ---------------------------------------------------------------------------


def test_orchestrator_log_level_info_default(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """_log defaults to INFO level."""
    caplog.set_level(logging.DEBUG, logger="se3.engine.merge.orchestrator")
    orch = MergeOrchestrator(tmp_path)
    orch._log("normal info message")
    assert any(r.levelno == logging.INFO for r in caplog.records)


def test_orchestrator_log_level_error(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """_log respects the level= parameter for errors."""
    caplog.set_level(logging.DEBUG, logger="se3.engine.merge.orchestrator")
    orch = MergeOrchestrator(tmp_path)
    orch._log("something broke", level=logging.ERROR)
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    assert not any(r.levelno == logging.INFO for r in caplog.records)


# ---------------------------------------------------------------------------
# K5/K6: Special repo state detection
# ---------------------------------------------------------------------------


def test_check_repo_state_empty_repo(tmp_path: Path) -> None:
    """Empty repo (no commits) raises EmptyRepoError."""
    _init_repo(tmp_path)
    with pytest.raises(EmptyRepoError):
        _check_repo_state(tmp_path)


def test_check_repo_state_detached_head(tmp_path: Path) -> None:
    """Detached HEAD raises DetachedHeadError."""
    _init_repo(tmp_path)
    (tmp_path / "file.txt").write_text("x")
    _commit(tmp_path, "initial")
    # Detach HEAD
    result = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    sha = result.stdout.strip()
    subprocess.run(
        ["git", "-C", str(tmp_path), "checkout", sha],
        check=True, capture_output=True,
    )
    with pytest.raises(DetachedHeadError):
        _check_repo_state(tmp_path)


def test_check_repo_state_shallow_clone(tmp_path: Path) -> None:
    """Shallow clone raises ShallowRepoError."""
    _init_repo(tmp_path)
    (tmp_path / "file.txt").write_text("x")
    _commit(tmp_path, "initial")
    # Mark as shallow by creating .git/shallow with a valid-looking SHA
    (tmp_path / ".git" / "shallow").write_text(
        "abcd1234abcd1234abcd1234abcd1234abcd1234\n"
    )
    with pytest.raises(ShallowRepoError):
        _check_repo_state(tmp_path)


def test_check_repo_state_normal_repo_passes(tmp_path: Path) -> None:
    """Normal repo with a branch passes without error."""
    _init_repo(tmp_path)
    (tmp_path / "file.txt").write_text("x")
    _commit(tmp_path, "initial")
    # Should not raise
    _check_repo_state(tmp_path)


# ---------------------------------------------------------------------------
# K9: Temp file cleanup
# ---------------------------------------------------------------------------


def test_atomic_write_json_cleans_up_temp_on_failure(tmp_path: Path) -> None:
    """When _atomic_write_json fails, no temp file is left behind."""
    call_file = tmp_path / "calls" / "test.json"
    call_data = {"key": "value"}

    # Make the parent directory read-only to force a failure
    call_file.parent.mkdir(parents=True, exist_ok=True)
    call_file.parent.chmod(0o555)
    try:
        with pytest.raises(OSError):
            _atomic_write_json(call_file, call_data)
    finally:
        call_file.parent.chmod(0o755)

    # No temp files should remain
    temp_files = list(tmp_path.glob(".tmp_*"))
    assert len(temp_files) == 0


def test_atomic_write_json_success_no_temp_leak(tmp_path: Path) -> None:
    """On success, no temp file is left behind."""
    call_file = tmp_path / "calls" / "test.json"
    call_data = {"key": "value"}

    _atomic_write_json(call_file, call_data)

    assert call_file.exists()
    # No temp files should remain
    temp_files = list(tmp_path.glob(".tmp_*"))
    assert len(temp_files) == 0
