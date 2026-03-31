"""Tests for extended exception handling and auto-repair in commit step."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from se3.engine.models import FlowInstance, Step, StepStatus
from se3.engine.steps.commit import commit_handler
from se3.engine.version_bumper import BumpType, VersionBumper, VersionConfig


def _make_flow(**kwargs) -> FlowInstance:
    defaults = {
        "flow_id": "test-flow-001",
        "task_description": "Fix authentication bug",
        "task_type": "bugfix",
        "change_path": Path("/tmp/project/se3.yaml"),
    }
    defaults.update(kwargs)
    flow = MagicMock(spec=FlowInstance)
    for k, v in defaults.items():
        setattr(flow, k, v)
    return flow


def _make_step(inputs: dict | None = None) -> Step:
    step = MagicMock(spec=Step)
    step.inputs = inputs or {}
    step.outputs = {}
    return step


def _default_version_config(**overrides) -> VersionConfig:
    """Create a VersionConfig with sensible defaults."""
    cfg = MagicMock(spec=VersionConfig)
    cfg.enabled = True
    cfg.bump_rules = {"bugfix": "patch", "feature": "minor"}
    cfg.include_in_commit_message = True
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


class TestRuntimeErrorScriptModeAutoRepair:
    """read_version() raises RuntimeError in script mode -> auto-repair via generate_version_script()."""

    @patch("se3.engine.steps.commit._get_commit_hash", return_value="abc123")
    @patch("se3.engine.steps.commit.subprocess")
    @patch("se3.engine.steps.commit._has_changes", return_value=True)
    @patch("se3.engine.steps.commit._load_version_config")
    @patch("se3.engine.steps.commit._generate_commit_message", return_value="bugfix: fix auth")
    def test_runtime_error_script_mode_triggers_generate_version_script(
        self, mock_commit_msg, mock_load_cfg, mock_has_changes, mock_subprocess, mock_hash
    ):
        """RuntimeError in script mode calls generate_version_script(), retry succeeds."""
        mock_load_cfg.return_value = _default_version_config()

        version_file = Path("/tmp/project/version.py")

        mock_bumper = MagicMock(spec=VersionBumper)
        mock_bumper.detect_version_file.return_value = version_file
        mock_bumper._use_script_mode = True
        mock_bumper._script_runner = MagicMock()

        # First call raises RuntimeError, second call (retry) succeeds
        mock_bumper.read_version.side_effect = [RuntimeError("script error"), "0.1.0"]
        mock_bumper.bump_version.return_value = "0.1.1"

        mock_subprocess.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        flow = _make_flow()
        step = _make_step({"task_description": "Fix auth"})

        with patch("se3.engine.steps.commit.VersionBumper", return_value=mock_bumper):
            with patch(
                "se3.engine.version_script_interface.generate_version_script"
            ) as mock_gen:
                result = commit_handler(step, flow)

        # Verify generate_version_script was called for script mode repair
        mock_gen.assert_called_once_with(Path("/tmp/project"))
        # read_version was called twice: first raises, second succeeds
        assert mock_bumper.read_version.call_count == 2
        assert result == StepStatus.COMPLETED

    @patch("se3.engine.steps.commit._get_commit_hash", return_value="abc123")
    @patch("se3.engine.steps.commit.subprocess")
    @patch("se3.engine.steps.commit._has_changes", return_value=True)
    @patch("se3.engine.steps.commit._load_version_config")
    @patch("se3.engine.steps.commit._generate_commit_message", return_value="bugfix: fix auth")
    def test_runtime_error_script_mode_retry_fails_propagates(
        self, mock_commit_msg, mock_load_cfg, mock_has_changes, mock_subprocess, mock_hash
    ):
        """RuntimeError in script mode, repair runs but retry also fails -> FAILED."""
        mock_load_cfg.return_value = _default_version_config()

        version_file = Path("/tmp/project/version.py")

        mock_bumper = MagicMock(spec=VersionBumper)
        mock_bumper.detect_version_file.return_value = version_file
        mock_bumper._use_script_mode = True
        mock_bumper._script_runner = MagicMock()

        # Both calls raise RuntimeError
        mock_bumper.read_version.side_effect = RuntimeError("persistent script error")

        flow = _make_flow()
        step = _make_step({"task_description": "Fix auth"})

        with patch("se3.engine.steps.commit.VersionBumper", return_value=mock_bumper):
            with patch(
                "se3.engine.version_script_interface.generate_version_script"
            ) as mock_gen:
                result = commit_handler(step, flow)

        # generate_version_script was called (repair attempted)
        mock_gen.assert_called_once()
        # But the retry failed so step should be FAILED
        assert result == StepStatus.FAILED
        assert "persistent script error" in step.error_message


class TestRuntimeErrorFileModeAutoRepair:
    """read_version() raises RuntimeError in file mode -> auto-repair via initialize_version_system()."""

    @patch("se3.engine.steps.commit._get_commit_hash", return_value="abc123")
    @patch("se3.engine.steps.commit.subprocess")
    @patch("se3.engine.steps.commit._has_changes", return_value=True)
    @patch("se3.engine.steps.commit._load_version_config")
    @patch("se3.engine.steps.commit._generate_commit_message", return_value="bugfix: fix auth")
    def test_runtime_error_file_mode_triggers_initialize_version_system(
        self, mock_commit_msg, mock_load_cfg, mock_has_changes, mock_subprocess, mock_hash
    ):
        """RuntimeError in file mode calls initialize_version_system(), retry succeeds."""
        mock_load_cfg.return_value = _default_version_config()

        version_file = Path("/tmp/project/version.py")
        repaired_file = Path("/tmp/project/version_repaired.py")

        mock_bumper = MagicMock(spec=VersionBumper)
        mock_bumper.detect_version_file.return_value = version_file
        mock_bumper._use_script_mode = False
        mock_bumper._script_runner = None

        # First call raises RuntimeError, second call (retry) succeeds
        mock_bumper.read_version.side_effect = [RuntimeError("file parse error"), "0.1.0"]
        mock_bumper.bump_version.return_value = "0.1.1"
        mock_bumper.initialize_version_system.return_value = repaired_file

        mock_subprocess.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        flow = _make_flow()
        step = _make_step({"task_description": "Fix auth"})

        with patch("se3.engine.steps.commit.VersionBumper", return_value=mock_bumper):
            result = commit_handler(step, flow)

        # initialize_version_system was called for file mode repair
        mock_bumper.initialize_version_system.assert_called_once_with(
            project_root=Path("/tmp/project"),
            initial_version="0.1.0",
        )
        assert mock_bumper.read_version.call_count == 2
        assert result == StepStatus.COMPLETED

    @patch("se3.engine.steps.commit._get_commit_hash", return_value="abc123")
    @patch("se3.engine.steps.commit.subprocess")
    @patch("se3.engine.steps.commit._has_changes", return_value=True)
    @patch("se3.engine.steps.commit._load_version_config")
    @patch("se3.engine.steps.commit._generate_commit_message", return_value="bugfix: fix auth")
    def test_runtime_error_file_mode_retry_fails_propagates(
        self, mock_commit_msg, mock_load_cfg, mock_has_changes, mock_subprocess, mock_hash
    ):
        """RuntimeError in file mode, repair runs but retry also fails -> FAILED."""
        mock_load_cfg.return_value = _default_version_config()

        version_file = Path("/tmp/project/version.py")

        mock_bumper = MagicMock(spec=VersionBumper)
        mock_bumper.detect_version_file.return_value = version_file
        mock_bumper._use_script_mode = False
        mock_bumper._script_runner = None

        # Both calls raise RuntimeError
        mock_bumper.read_version.side_effect = RuntimeError("persistent file error")
        mock_bumper.initialize_version_system.return_value = version_file

        flow = _make_flow()
        step = _make_step({"task_description": "Fix auth"})

        with patch("se3.engine.steps.commit.VersionBumper", return_value=mock_bumper):
            result = commit_handler(step, flow)

        mock_bumper.initialize_version_system.assert_called_once()
        assert result == StepStatus.FAILED
        assert "persistent file error" in step.error_message


class TestRuntimeErrorNoVersionFileAutoRepair:
    """read_version() raises RuntimeError on the no-version-file path (initialize then read)."""

    @patch("se3.engine.steps.commit._get_commit_hash", return_value="abc123")
    @patch("se3.engine.steps.commit.subprocess")
    @patch("se3.engine.steps.commit._has_changes", return_value=True)
    @patch("se3.engine.steps.commit._load_version_config")
    @patch("se3.engine.steps.commit._generate_commit_message", return_value="bugfix: fix auth")
    def test_no_version_file_script_mode_runtime_error_triggers_repair(
        self, mock_commit_msg, mock_load_cfg, mock_has_changes, mock_subprocess, mock_hash
    ):
        """No version file initially, initialize creates one, read_version raises RuntimeError
        in script mode -> generate_version_script() called, retry succeeds."""
        mock_load_cfg.return_value = _default_version_config()

        created_file = Path("/tmp/project/VERSION")

        mock_bumper = MagicMock(spec=VersionBumper)
        mock_bumper.detect_version_file.return_value = None  # No version file exists
        mock_bumper._use_script_mode = True
        mock_bumper._script_runner = MagicMock()
        mock_bumper.initialize_version_system.return_value = created_file

        # First read_version raises RuntimeError, retry succeeds
        mock_bumper.read_version.side_effect = [RuntimeError("bad script"), "0.1.0"]
        mock_bumper.bump_version.return_value = "0.1.1"

        mock_subprocess.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        flow = _make_flow()
        step = _make_step({"task_description": "Fix auth"})

        with patch("se3.engine.steps.commit.VersionBumper", return_value=mock_bumper):
            with patch(
                "se3.engine.version_script_interface.generate_version_script"
            ) as mock_gen:
                result = commit_handler(step, flow)

        mock_gen.assert_called_once_with(Path("/tmp/project"))
        assert mock_bumper.read_version.call_count == 2
        assert result == StepStatus.COMPLETED

    @patch("se3.engine.steps.commit._get_commit_hash", return_value="abc123")
    @patch("se3.engine.steps.commit.subprocess")
    @patch("se3.engine.steps.commit._has_changes", return_value=True)
    @patch("se3.engine.steps.commit._load_version_config")
    @patch("se3.engine.steps.commit._generate_commit_message", return_value="bugfix: fix auth")
    def test_no_version_file_file_mode_runtime_error_triggers_repair(
        self, mock_commit_msg, mock_load_cfg, mock_has_changes, mock_subprocess, mock_hash
    ):
        """No version file initially, initialize creates one, read_version raises RuntimeError
        in file mode -> initialize_version_system() called again, retry succeeds."""
        mock_load_cfg.return_value = _default_version_config()

        created_file = Path("/tmp/project/VERSION")

        mock_bumper = MagicMock(spec=VersionBumper)
        mock_bumper.detect_version_file.return_value = None
        mock_bumper._use_script_mode = False
        mock_bumper._script_runner = None
        # First call from no-file path, second from auto-repair
        mock_bumper.initialize_version_system.return_value = created_file

        mock_bumper.read_version.side_effect = [RuntimeError("bad file"), "0.1.0"]
        mock_bumper.bump_version.return_value = "0.1.1"

        mock_subprocess.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        flow = _make_flow()
        step = _make_step({"task_description": "Fix auth"})

        with patch("se3.engine.steps.commit.VersionBumper", return_value=mock_bumper):
            result = commit_handler(step, flow)

        # initialize_version_system called twice: once to create, once for repair
        assert mock_bumper.initialize_version_system.call_count == 2
        assert result == StepStatus.COMPLETED


class TestValueErrorKeyErrorRegression:
    """Existing ValueError/KeyError handling still works (regression tests)."""

    @patch("se3.engine.steps.commit._get_commit_hash", return_value="abc123")
    @patch("se3.engine.steps.commit.subprocess")
    @patch("se3.engine.steps.commit._has_changes", return_value=True)
    @patch("se3.engine.steps.commit._load_version_config")
    @patch("se3.engine.steps.commit._generate_commit_message", return_value="bugfix: fix auth")
    def test_value_error_triggers_auto_repair_file_mode(
        self, mock_commit_msg, mock_load_cfg, mock_has_changes, mock_subprocess, mock_hash
    ):
        """ValueError from read_version in file mode triggers initialize_version_system repair."""
        mock_load_cfg.return_value = _default_version_config()

        version_file = Path("/tmp/project/version.py")

        mock_bumper = MagicMock(spec=VersionBumper)
        mock_bumper.detect_version_file.return_value = version_file
        mock_bumper._use_script_mode = False
        mock_bumper._script_runner = None

        mock_bumper.read_version.side_effect = [ValueError("bad version format"), "0.1.0"]
        mock_bumper.bump_version.return_value = "0.1.1"
        mock_bumper.initialize_version_system.return_value = version_file

        mock_subprocess.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        flow = _make_flow()
        step = _make_step({"task_description": "Fix auth"})

        with patch("se3.engine.steps.commit.VersionBumper", return_value=mock_bumper):
            result = commit_handler(step, flow)

        mock_bumper.initialize_version_system.assert_called_once()
        assert result == StepStatus.COMPLETED

    @patch("se3.engine.steps.commit._get_commit_hash", return_value="abc123")
    @patch("se3.engine.steps.commit.subprocess")
    @patch("se3.engine.steps.commit._has_changes", return_value=True)
    @patch("se3.engine.steps.commit._load_version_config")
    @patch("se3.engine.steps.commit._generate_commit_message", return_value="bugfix: fix auth")
    def test_key_error_triggers_auto_repair_script_mode(
        self, mock_commit_msg, mock_load_cfg, mock_has_changes, mock_subprocess, mock_hash
    ):
        """KeyError from read_version in script mode triggers generate_version_script repair."""
        mock_load_cfg.return_value = _default_version_config()

        version_file = Path("/tmp/project/version.py")

        mock_bumper = MagicMock(spec=VersionBumper)
        mock_bumper.detect_version_file.return_value = version_file
        mock_bumper._use_script_mode = True
        mock_bumper._script_runner = MagicMock()

        mock_bumper.read_version.side_effect = [KeyError("missing key"), "0.1.0"]
        mock_bumper.bump_version.return_value = "0.1.1"

        mock_subprocess.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        flow = _make_flow()
        step = _make_step({"task_description": "Fix auth"})

        with patch("se3.engine.steps.commit.VersionBumper", return_value=mock_bumper):
            with patch(
                "se3.engine.version_script_interface.generate_version_script"
            ) as mock_gen:
                result = commit_handler(step, flow)

        mock_gen.assert_called_once()
        assert result == StepStatus.COMPLETED

    @patch("se3.engine.steps.commit._get_commit_hash", return_value="abc123")
    @patch("se3.engine.steps.commit.subprocess")
    @patch("se3.engine.steps.commit._has_changes", return_value=True)
    @patch("se3.engine.steps.commit._load_version_config")
    @patch("se3.engine.steps.commit._generate_commit_message", return_value="bugfix: fix auth")
    def test_value_error_retry_fails_propagates(
        self, mock_commit_msg, mock_load_cfg, mock_has_changes, mock_subprocess, mock_hash
    ):
        """ValueError repair succeeds but retry read_version still fails -> FAILED."""
        mock_load_cfg.return_value = _default_version_config()

        version_file = Path("/tmp/project/version.py")

        mock_bumper = MagicMock(spec=VersionBumper)
        mock_bumper.detect_version_file.return_value = version_file
        mock_bumper._use_script_mode = False
        mock_bumper._script_runner = None

        mock_bumper.read_version.side_effect = ValueError("still broken")
        mock_bumper.initialize_version_system.return_value = version_file

        flow = _make_flow()
        step = _make_step({"task_description": "Fix auth"})

        with patch("se3.engine.steps.commit.VersionBumper", return_value=mock_bumper):
            result = commit_handler(step, flow)

        assert result == StepStatus.FAILED
        assert "still broken" in step.error_message
