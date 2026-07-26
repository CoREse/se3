"""Tests for commit step handler edge cases.

Covers:
- commit_handler gracefully handles ValueError when read_version fails
  on a detected version file (the original crash scenario)
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tianluo.engine.models import FlowInstance, Step, StepStatus, StepType
from tianluo.engine.steps.commit import commit_handler
from tianluo.engine.version_bumper import VersionBumper, VersionConfig


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


def _make_flow(temp_dir: Path, task_type: str = "feature") -> FlowInstance:
    """Create a minimal FlowInstance pointing at temp_dir."""
    flow = FlowInstance()
    flow.task_description = "test task"
    flow.task_type = task_type
    # change_path.parent must equal project_root (temp_dir)
    flow.change_path = temp_dir / "change.yaml"
    return flow


def _make_step(suggested_version: str = "0.2.0") -> Step:
    """Create a minimal commit Step.

    Pre-populates ``suggested_version`` (and a placeholder ``bump_type``) on
    the step's inputs so that the commit handler's ``_resolve_target_version``
    check passes. Tests that want to exercise the missing/failed branches
    should override.
    """
    step = Step(step_type=StepType.COMMIT)
    step.inputs = {
        "task_description": "test task",
        "suggested_version": suggested_version,
        "bump_type": "patch",
    }
    return step


class TestCommitHandlerVersionlessFile:
    """commit_handler must not crash when a version file exists but has no version."""

    @patch("tianluo.engine.steps.commit._has_changes", return_value=True)
    @patch("tianluo.engine.steps.commit._generate_commit_message", return_value="test: msg")
    @patch("tianluo.engine.steps.commit.subprocess")
    def test_initializes_version_when_read_raises_value_error(
        self, mock_subprocess, mock_gen_msg, mock_has_changes, temp_dir: Path
    ):
        """When read_version raises ValueError, commit_handler should initialize
        the version system instead of crashing."""
        # Set up a pyproject.toml WITHOUT a version field (the crash scenario)
        (temp_dir / "pyproject.toml").write_text('[project]\nname = "test"\n')

        # Mock _load_version_config to return an enabled config
        config = VersionConfig(enabled=True)
        config.auto_generate_script = False

        # Mock subprocess calls to succeed
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "abc1234\n"
        mock_subprocess.run.return_value = mock_result

        step = _make_step()
        flow = _make_flow(temp_dir)

        with patch("tianluo.engine.steps.commit._load_version_config", return_value=config):
            result = commit_handler(step, flow)

        # Should NOT fail — the handler should recover by initializing the version
        assert result != StepStatus.FAILED
        assert result == StepStatus.COMPLETED
        # Version should have been bumped
        assert step.outputs.get("version_bumped") is True

    @patch("tianluo.engine.steps.commit._has_changes", return_value=True)
    @patch("tianluo.engine.steps.commit._generate_commit_message", return_value="test: msg")
    @patch("tianluo.engine.steps.commit.subprocess")
    def test_version_is_readable_after_recovery(
        self, mock_subprocess, mock_gen_msg, mock_has_changes, temp_dir: Path
    ):
        """After recovery from ValueError, the version file should be readable."""
        (temp_dir / "pyproject.toml").write_text('[project]\nname = "test"\n')

        config = VersionConfig(enabled=True)
        config.auto_generate_script = False

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "abc1234\n"
        mock_subprocess.run.return_value = mock_result

        step = _make_step()
        flow = _make_flow(temp_dir)

        with patch("tianluo.engine.steps.commit._load_version_config", return_value=config):
            commit_handler(step, flow)

        # The pyproject.toml should now contain a version
        from tianluo.engine.version_bumper import TomlVersionHandler

        handler = TomlVersionHandler()
        version = handler.read_version(temp_dir / "pyproject.toml")
        # Should be bumped from 0.1.0 (initialized) -> next version
        assert version is not None

    @patch("tianluo.engine.steps.commit._has_changes", return_value=True)
    @patch("tianluo.engine.steps.commit._generate_commit_message", return_value="fix: msg")
    @patch("tianluo.engine.steps.commit.subprocess")
    def test_init_py_without_version_does_not_crash(
        self, mock_subprocess, mock_gen_msg, mock_has_changes, temp_dir: Path
    ):
        """__init__.py without __version__ should not crash commit_handler."""
        # Create a Python project structure with versionless __init__.py
        src_dir = temp_dir / "src" / "mypkg"
        src_dir.mkdir(parents=True)
        (src_dir / "__init__.py").write_text("# empty\n")
        (temp_dir / "requirements.txt").write_text("pytest\n")

        config = VersionConfig(enabled=True)
        config.auto_generate_script = False

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "def5678\n"
        mock_subprocess.run.return_value = mock_result

        step = _make_step()
        flow = _make_flow(temp_dir, task_type="bugfix")

        with patch("tianluo.engine.steps.commit._load_version_config", return_value=config):
            result = commit_handler(step, flow)

        # Should complete successfully, not raise ValueError
        assert result == StepStatus.COMPLETED

    @patch("tianluo.engine.steps.commit._has_changes", return_value=True)
    @patch("tianluo.engine.steps.commit._generate_commit_message", return_value="feat: msg")
    @patch("tianluo.engine.steps.commit.subprocess")
    def test_normal_version_file_still_works(
        self, mock_subprocess, mock_gen_msg, mock_has_changes, temp_dir: Path
    ):
        """A pyproject.toml WITH a valid version should still work normally."""
        (temp_dir / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "1.2.3"\n'
        )

        config = VersionConfig(enabled=True)
        config.auto_generate_script = False

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "abc1234\n"
        mock_subprocess.run.return_value = mock_result

        step = _make_step()
        flow = _make_flow(temp_dir)

        with patch("tianluo.engine.steps.commit._load_version_config", return_value=config):
            result = commit_handler(step, flow)

        assert result == StepStatus.COMPLETED
        # Version should have been bumped from 1.2.3
        assert step.outputs.get("version") is not None
        assert step.outputs.get("version_bumped") is True


class TestCommitHandlerSuggestedVersionDriven:
    """commit_handler writes the suggested_version from version_analyze verbatim."""

    @patch("tianluo.engine.steps.commit._has_changes", return_value=True)
    @patch("tianluo.engine.steps.commit._generate_commit_message", return_value="feat: msg")
    @patch("tianluo.engine.steps.commit.subprocess")
    def test_suggested_version_written_verbatim_to_pyproject(
        self, mock_subprocess, mock_gen_msg, mock_has_changes, temp_dir: Path
    ):
        """suggested_version='9.4.2' on inputs → pyproject.toml ends up at exactly 9.4.2.

        This confirms the commit step bypasses any bump_type-based recomputation
        and writes the authoritative LLM-supplied version directly.
        """
        (temp_dir / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "1.2.3"\n'
        )

        config = VersionConfig(enabled=True)
        config.auto_generate_script = False

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "abc1234\n"
        mock_subprocess.run.return_value = mock_result

        step = _make_step(suggested_version="9.4.2")
        # bump_type intentionally mismatches: if the commit step used bump_type
        # to recompute, the result would be 1.2.4 (patch) — not 9.4.2.
        step.inputs["bump_type"] = "patch"
        flow = _make_flow(temp_dir)

        with patch("tianluo.engine.steps.commit._load_version_config", return_value=config):
            result = commit_handler(step, flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["version"] == "9.4.2"
        assert step.outputs["version_bumped"] is True

        # File on disk reflects the LLM-supplied version verbatim
        from tianluo.engine.version_bumper import TomlVersionHandler

        handler = TomlVersionHandler()
        assert handler.read_version(temp_dir / "pyproject.toml") == "9.4.2"

    @patch("tianluo.engine.steps.commit._has_changes", return_value=True)
    @patch("tianluo.engine.steps.commit._generate_commit_message", return_value="feat: msg")
    @patch("tianluo.engine.steps.commit.subprocess")
    def test_suggested_version_supports_major_jump(
        self, mock_subprocess, mock_gen_msg, mock_has_changes, temp_dir: Path
    ):
        """suggested_version can skip versions (e.g., 1.2.3 → 3.0.0) when LLM says so."""
        (temp_dir / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "1.2.3"\n'
        )

        config = VersionConfig(enabled=True)
        config.auto_generate_script = False

        mock_subprocess.run.return_value = MagicMock(returncode=0, stdout="hash\n")

        step = _make_step(suggested_version="3.0.0")
        flow = _make_flow(temp_dir)

        with patch("tianluo.engine.steps.commit._load_version_config", return_value=config):
            result = commit_handler(step, flow)

        assert result == StepStatus.COMPLETED
        from tianluo.engine.version_bumper import TomlVersionHandler
        assert TomlVersionHandler().read_version(temp_dir / "pyproject.toml") == "3.0.0"


class TestCommitHandlerMissingSuggestedVersion:
    """commit_handler halts with a guided error when suggested_version is absent."""

    @patch("tianluo.engine.steps.commit._has_changes", return_value=True)
    @patch("tianluo.engine.steps.commit.subprocess")
    def test_missing_suggested_version_raises_with_current_version(
        self, mock_subprocess, mock_has_changes, temp_dir: Path
    ):
        """When step.inputs has no suggested_version and no VA step in flow history,
        commit_handler returns FAILED with an error message that references the
        current version state and points the user toward human intervention.
        """
        (temp_dir / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "1.2.3"\n'
        )

        config = VersionConfig(enabled=True)
        config.auto_generate_script = False

        # No subprocess calls should happen — we fail before git operations
        mock_subprocess.run.return_value = MagicMock(returncode=0, stdout="hash\n")

        step = Step(step_type=StepType.COMMIT)
        # Intentionally empty inputs — no suggested_version anywhere
        step.inputs = {"task_description": "test"}
        flow = _make_flow(temp_dir)

        with patch("tianluo.engine.steps.commit._load_version_config", return_value=config):
            result = commit_handler(step, flow)

        assert result == StepStatus.FAILED
        # The error message should mention suggested_version and route the user
        # toward human intervention. current_version comes from the resolver's
        # fallback ("<unknown>") since no VA step is in the flow history.
        assert step.error_message is not None
        msg = step.error_message
        assert "suggested_version" in msg
        # Either the literal current_version marker or generic guidance
        assert "current_version" in msg or "<unknown>" in msg
        # Human-intervention hint
        assert "human" in msg.lower() or "rerun" in msg.lower() or "call" in msg.lower()

    @patch("tianluo.engine.steps.commit._has_changes", return_value=True)
    @patch("tianluo.engine.steps.commit.subprocess")
    def test_empty_string_suggested_version_raises(
        self, mock_subprocess, mock_has_changes, temp_dir: Path
    ):
        """Whitespace-only suggested_version is treated as missing → FAILED."""
        (temp_dir / "pyproject.toml").write_text(
            '[project]\nname = "test"\nversion = "1.0.0"\n'
        )

        config = VersionConfig(enabled=True)
        config.auto_generate_script = False
        mock_subprocess.run.return_value = MagicMock(returncode=0, stdout="hash\n")

        step = Step(step_type=StepType.COMMIT)
        step.inputs = {"task_description": "test", "suggested_version": "   "}
        flow = _make_flow(temp_dir)

        with patch("tianluo.engine.steps.commit._load_version_config", return_value=config):
            result = commit_handler(step, flow)

        assert result == StepStatus.FAILED
        assert "suggested_version" in step.error_message
