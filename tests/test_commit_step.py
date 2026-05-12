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

from se3.engine.models import FlowInstance, Step, StepStatus, StepType
from se3.engine.steps.commit import commit_handler
from se3.engine.version_bumper import VersionBumper, VersionConfig


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

    @patch("se3.engine.steps.commit._has_changes", return_value=True)
    @patch("se3.engine.steps.commit._generate_commit_message", return_value="test: msg")
    @patch("se3.engine.steps.commit.subprocess")
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

        with patch("se3.engine.steps.commit._load_version_config", return_value=config):
            result = commit_handler(step, flow)

        # Should NOT fail — the handler should recover by initializing the version
        assert result != StepStatus.FAILED
        assert result == StepStatus.COMPLETED
        # Version should have been bumped
        assert step.outputs.get("version_bumped") is True

    @patch("se3.engine.steps.commit._has_changes", return_value=True)
    @patch("se3.engine.steps.commit._generate_commit_message", return_value="test: msg")
    @patch("se3.engine.steps.commit.subprocess")
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

        with patch("se3.engine.steps.commit._load_version_config", return_value=config):
            commit_handler(step, flow)

        # The pyproject.toml should now contain a version
        from se3.engine.version_bumper import TomlVersionHandler

        handler = TomlVersionHandler()
        version = handler.read_version(temp_dir / "pyproject.toml")
        # Should be bumped from 0.1.0 (initialized) -> next version
        assert version is not None

    @patch("se3.engine.steps.commit._has_changes", return_value=True)
    @patch("se3.engine.steps.commit._generate_commit_message", return_value="fix: msg")
    @patch("se3.engine.steps.commit.subprocess")
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

        with patch("se3.engine.steps.commit._load_version_config", return_value=config):
            result = commit_handler(step, flow)

        # Should complete successfully, not raise ValueError
        assert result == StepStatus.COMPLETED

    @patch("se3.engine.steps.commit._has_changes", return_value=True)
    @patch("se3.engine.steps.commit._generate_commit_message", return_value="feat: msg")
    @patch("se3.engine.steps.commit.subprocess")
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

        with patch("se3.engine.steps.commit._load_version_config", return_value=config):
            result = commit_handler(step, flow)

        assert result == StepStatus.COMPLETED
        # Version should have been bumped from 1.2.3
        assert step.outputs.get("version") is not None
        assert step.outputs.get("version_bumped") is True
