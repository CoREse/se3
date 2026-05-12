"""Tests for extended exception handling, auto-repair, and template summary in commit step."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from se3.engine.models import FlowInstance, State, Step, StepStatus, StepType
from se3.engine.steps.commit import (
    commit_handler,
    _generate_commit_message,
    _generate_template_summary,
    _resolve_target_version,
    _collect_changes_from_flow,
    _collect_test_results_from_flow,
)
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
    # Ensure state.selected_steps exists for template summary check
    state = MagicMock(spec=State)
    state.selected_steps = kwargs.get("selected_steps", [
        StepType.ANALYZE, StepType.IMPLEMENT, StepType.COMMIT, StepType.SUMMARIZE,
    ])
    state.step_history = []
    state.steps = {}
    flow.state = state
    return flow


def _make_step(inputs: dict | None = None) -> Step:
    step = MagicMock(spec=Step)
    # Default to a sane suggested_version so the commit step's
    # _resolve_target_version() check passes. Individual tests that
    # exercise the missing/failed paths set inputs explicitly.
    base_inputs = {"suggested_version": "0.1.1", "bump_type": "patch"}
    if inputs:
        base_inputs.update(inputs)
    step.inputs = base_inputs
    step.outputs = {}
    return step


def _default_version_config(**overrides) -> VersionConfig:
    """Create a VersionConfig with sensible defaults."""
    cfg = MagicMock(spec=VersionConfig)
    cfg.enabled = True
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
        mock_bumper.set_version.return_value = "0.1.1"

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
        mock_bumper.set_version.return_value = "0.1.1"
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
        mock_bumper.set_version.return_value = "0.1.1"

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
        mock_bumper.set_version.return_value = "0.1.1"

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
        mock_bumper.set_version.return_value = "0.1.1"
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
        mock_bumper.set_version.return_value = "0.1.1"

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


# ---------------------------------------------------------------------------
# Helpers for template summary tests
# ---------------------------------------------------------------------------

def _make_flow_with_state(**kwargs) -> FlowInstance:
    """Create a FlowInstance with a real-ish State for template summary tests."""
    defaults = {
        "flow_id": "test-flow-summary",
        "task_description": "Add new feature",
        "task_type": "feature",
        "change_path": Path("/tmp/project/se3.yaml"),
    }
    defaults.update(kwargs)

    flow = MagicMock(spec=FlowInstance)
    for k, v in defaults.items():
        setattr(flow, k, v)

    # Build a state with selected_steps and step_history
    state = MagicMock(spec=State)
    state.selected_steps = kwargs.get("selected_steps", [
        StepType.ANALYZE, StepType.IMPLEMENT, StepType.TEST,
        StepType.VERSION_ANALYZE, StepType.COMMIT,
    ])
    state.step_history = kwargs.get("step_history", [])
    state.steps = kwargs.get("steps", {})
    flow.state = state
    return flow


class TestTemplateSummaryGeneration:
    """Template summary is generated when SUMMARIZE step is absent."""

    @patch("se3.engine.steps.commit._get_commit_hash", return_value="abc12345")
    @patch("se3.engine.steps.commit.subprocess")
    @patch("se3.engine.steps.commit._has_changes", return_value=True)
    @patch("se3.engine.steps.commit._load_version_config")
    @patch("se3.engine.steps.commit._generate_commit_message", return_value="feature: add X")
    @patch("se3.engine.steps.commit._generate_template_summary")
    def test_template_summary_called_when_no_summarize_step(
        self, mock_template, mock_commit_msg, mock_load_cfg, mock_has_changes,
        mock_subprocess, mock_hash
    ):
        """When SUMMARIZE is not in selected_steps, _generate_template_summary is called."""
        mock_load_cfg.return_value = _default_version_config(enabled=False)
        mock_subprocess.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        flow = _make_flow_with_state(selected_steps=[
            StepType.ANALYZE, StepType.IMPLEMENT, StepType.COMMIT,
        ])
        step = _make_step({"task_description": "Add X"})

        result = commit_handler(step, flow)

        assert result == StepStatus.COMPLETED
        mock_template.assert_called_once_with(flow, step)

    @patch("se3.engine.steps.commit._get_commit_hash", return_value="abc12345")
    @patch("se3.engine.steps.commit.subprocess")
    @patch("se3.engine.steps.commit._has_changes", return_value=True)
    @patch("se3.engine.steps.commit._load_version_config")
    @patch("se3.engine.steps.commit._generate_commit_message", return_value="feature: add X")
    @patch("se3.engine.steps.commit._generate_template_summary")
    def test_template_summary_not_called_when_summarize_step_present(
        self, mock_template, mock_commit_msg, mock_load_cfg, mock_has_changes,
        mock_subprocess, mock_hash
    ):
        """When SUMMARIZE is in selected_steps, _generate_template_summary is NOT called."""
        mock_load_cfg.return_value = _default_version_config(enabled=False)
        mock_subprocess.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        flow = _make_flow_with_state(selected_steps=[
            StepType.ANALYZE, StepType.IMPLEMENT, StepType.COMMIT, StepType.SUMMARIZE,
        ])
        step = _make_step({"task_description": "Add X"})

        result = commit_handler(step, flow)

        assert result == StepStatus.COMPLETED
        mock_template.assert_not_called()

    def test_generate_template_summary_creates_file(self, tmp_path):
        """_generate_template_summary writes a summary file to se3/state/."""
        flow = _make_flow_with_state(
            flow_id="ts-001",
            change_path=tmp_path / "se3.yaml",
            task_description="Implement auth",
            task_type="feature",
            step_history=[],
            steps={},
        )
        step = _make_step()
        step.outputs = {
            "commit_message": "feature: implement auth",
            "commit_hash": "deadbeef12345678",
            "version": "1.2.0",
            "version_bumped": True,
        }

        _generate_template_summary(flow, step)

        summary_file = tmp_path / "se3" / "state" / "summary-ts-001.md"
        assert summary_file.exists()
        content = summary_file.read_text()
        assert "Implement auth" in content
        assert "feature" in content
        assert "deadbeef" in content
        assert "1.2.0" in content
        assert "feature: implement auth" in content
        assert "template mode" in content


class TestTemplateSummaryVersionAnalysis:
    """Tests for Version Analysis reasoning section in template summary."""

    def test_generate_template_summary_with_reasoning(self, tmp_path):
        """When step.inputs['reasoning'] has a value, summary includes ### Version Analysis."""
        flow = _make_flow_with_state(
            flow_id="va-001",
            change_path=tmp_path / "se3.yaml",
            task_description="Add feature X",
            task_type="feature",
        )
        step = _make_step(inputs={"reasoning": "This is a minor bump because a new backward-compatible feature was added."})
        step.outputs = {
            "commit_message": "feature: add X",
            "commit_hash": "abc12345",
            "version": "1.3.0",
            "version_bumped": True,
        }

        _generate_template_summary(flow, step)

        summary_file = tmp_path / "se3" / "state" / "summary-va-001.md"
        content = summary_file.read_text()
        assert "### Version Analysis" in content
        assert "This is a minor bump because a new backward-compatible feature was added." in content

    def test_generate_template_summary_without_reasoning(self, tmp_path):
        """When step.inputs has no 'reasoning' key, summary does not include ### Version Analysis."""
        flow = _make_flow_with_state(
            flow_id="va-002",
            change_path=tmp_path / "se3.yaml",
            task_description="Fix bug Y",
            task_type="bugfix",
        )
        step = _make_step(inputs={"bump_type": "patch"})
        step.outputs = {
            "commit_message": "bugfix: fix Y",
            "commit_hash": "def67890",
            "version_bumped": False,
        }

        _generate_template_summary(flow, step)

        summary_file = tmp_path / "se3" / "state" / "summary-va-002.md"
        content = summary_file.read_text()
        assert "### Version Analysis" not in content

    def test_generate_template_summary_empty_reasoning(self, tmp_path):
        """When step.inputs['reasoning'] is an empty string, summary does not include ### Version Analysis."""
        flow = _make_flow_with_state(
            flow_id="va-003",
            change_path=tmp_path / "se3.yaml",
            task_description="Refactor Z",
            task_type="small",
        )
        step = _make_step(inputs={"reasoning": ""})
        step.outputs = {
            "commit_message": "small: refactor Z",
            "commit_hash": "aaa11111",
            "version_bumped": False,
        }

        _generate_template_summary(flow, step)

        summary_file = tmp_path / "se3" / "state" / "summary-va-003.md"
        content = summary_file.read_text()
        assert "### Version Analysis" not in content

    def test_version_analysis_section_position(self, tmp_path):
        """### Version Analysis appears after Version line and before ### Commit Message."""
        flow = _make_flow_with_state(
            flow_id="va-004",
            change_path=tmp_path / "se3.yaml",
            task_description="Add feature W",
            task_type="feature",
        )
        step = _make_step(inputs={"reasoning": "Minor bump: new feature added."})
        step.outputs = {
            "commit_message": "feature: add W",
            "commit_hash": "bbb22222",
            "version": "2.0.0",
            "version_bumped": True,
        }

        _generate_template_summary(flow, step)

        summary_file = tmp_path / "se3" / "state" / "summary-va-004.md"
        content = summary_file.read_text()
        version_pos = content.index("**Version:** 2.0.0")
        analysis_pos = content.index("### Version Analysis")
        commit_msg_pos = content.index("### Commit Message")
        assert version_pos < analysis_pos < commit_msg_pos


class TestCollectChangesFromFlow:
    """Tests for _collect_changes_from_flow helper."""

    def test_collects_string_file_paths(self):
        impl_step = MagicMock(spec=Step)
        impl_step.step_type = StepType.IMPLEMENT
        impl_step.outputs = {"files_changed": ["src/a.py", "src/b.py"]}

        flow = _make_flow_with_state(
            step_history=["impl-1"],
            steps={"impl-1": impl_step},
        )

        result = _collect_changes_from_flow(flow)
        assert result == ["src/a.py", "src/b.py"]

    def test_collects_dict_file_paths(self):
        impl_step = MagicMock(spec=Step)
        impl_step.step_type = StepType.IMPLEMENT
        impl_step.outputs = {
            "files_changed": [
                {"path": "src/a.py", "action": "modified"},
                {"path": "src/b.py", "action": "created"},
            ]
        }

        flow = _make_flow_with_state(
            step_history=["impl-1"],
            steps={"impl-1": impl_step},
        )

        result = _collect_changes_from_flow(flow)
        assert result == ["src/a.py", "src/b.py"]

    def test_returns_empty_when_no_implement_step(self):
        flow = _make_flow_with_state(step_history=[], steps={})
        assert _collect_changes_from_flow(flow) == []


class TestCollectTestResultsFromFlow:
    """Tests for _collect_test_results_from_flow helper."""

    def test_returns_most_recent_test_results(self):
        test_step_1 = MagicMock(spec=Step)
        test_step_1.step_type = StepType.TEST
        test_step_1.outputs = {"test_results": {"passed": False}}

        test_step_2 = MagicMock(spec=Step)
        test_step_2.step_type = StepType.TEST
        test_step_2.outputs = {"test_results": {"passed": True}}

        flow = _make_flow_with_state(
            step_history=["test-1", "test-2"],
            steps={"test-1": test_step_1, "test-2": test_step_2},
        )

        result = _collect_test_results_from_flow(flow)
        assert result == {"passed": True}

    def test_returns_empty_dict_when_no_test_step(self):
        flow = _make_flow_with_state(step_history=[], steps={})
        assert _collect_test_results_from_flow(flow) == {}


class TestResolveTargetVersion:
    """Tests for _resolve_target_version — authoritative version pickup."""

    def _va_step(self, status: StepStatus, outputs: dict | None = None) -> Step:
        s = MagicMock(spec=Step)
        s.step_type = StepType.VERSION_ANALYZE
        s.status = status
        s.outputs = outputs or {}
        return s

    def test_returns_suggested_version_from_inputs(self):
        flow = _make_flow_with_state(
            step_history=["va-1"],
            steps={
                "va-1": self._va_step(
                    StepStatus.COMPLETED,
                    {"suggested_version": "1.3.0", "current_version": "1.2.3"},
                )
            },
        )
        step = MagicMock(spec=Step)
        step.inputs = {"suggested_version": "1.3.0", "current_version": "1.2.3"}

        assert _resolve_target_version(step, flow) == "1.3.0"

    def test_falls_back_to_va_step_outputs(self):
        """When step.inputs lacks suggested_version, look at the VA step's outputs."""
        flow = _make_flow_with_state(
            step_history=["va-1"],
            steps={
                "va-1": self._va_step(
                    StepStatus.COMPLETED,
                    {"suggested_version": "2.0.0", "current_version": "1.9.0"},
                )
            },
        )
        step = MagicMock(spec=Step)
        step.inputs = {}

        assert _resolve_target_version(step, flow) == "2.0.0"

    def test_strips_whitespace(self):
        flow = _make_flow_with_state(step_history=[], steps={})
        step = MagicMock(spec=Step)
        step.inputs = {"suggested_version": "  1.4.0  "}

        assert _resolve_target_version(step, flow) == "1.4.0"

    def test_missing_suggested_version_raises_with_current_version(self):
        flow = _make_flow_with_state(
            step_history=["va-1"],
            steps={
                "va-1": self._va_step(
                    StepStatus.COMPLETED,
                    {"current_version": "1.2.3"},
                )
            },
        )
        step = MagicMock(spec=Step)
        step.inputs = {}

        with pytest.raises(RuntimeError) as exc_info:
            _resolve_target_version(step, flow)

        msg = str(exc_info.value)
        assert "suggested_version" in msg
        assert "1.2.3" in msg
        # Human-intervention guidance
        assert "human" in msg.lower() or "call" in msg.lower()

    def test_va_step_failed_raises_with_intervention_hint(self):
        flow = _make_flow_with_state(
            step_history=["va-1"],
            steps={
                "va-1": self._va_step(
                    StepStatus.FAILED,
                    {"current_version": "1.2.3"},
                )
            },
        )
        # Even if step.inputs somehow carries a stale suggested_version,
        # a FAILED version_analyze step takes precedence and we halt.
        step = MagicMock(spec=Step)
        step.inputs = {"suggested_version": "1.3.0"}

        with pytest.raises(RuntimeError) as exc_info:
            _resolve_target_version(step, flow)

        msg = str(exc_info.value)
        assert "version_analyze" in msg
        assert "failed" in msg.lower()
        assert "1.2.3" in msg
        # Human intervention guidance
        assert "human" in msg.lower() or "rerun" in msg.lower()

    def test_empty_string_suggested_version_raises(self):
        flow = _make_flow_with_state(step_history=[], steps={})
        step = MagicMock(spec=Step)
        step.inputs = {"suggested_version": "   "}

        with pytest.raises(RuntimeError):
            _resolve_target_version(step, flow)

    def test_no_va_step_at_all_uses_unknown_current_version(self):
        """When no version_analyze step ever ran, current_version is reported as unknown."""
        flow = _make_flow_with_state(step_history=[], steps={})
        step = MagicMock(spec=Step)
        step.inputs = {}

        with pytest.raises(RuntimeError) as exc_info:
            _resolve_target_version(step, flow)

        assert "<unknown>" in str(exc_info.value)


class TestCommitMessageBumpTypeDecoration:
    """Tests for bump_type display decoration in commit message generation."""

    def test_bump_type_appended_to_message(self):
        flow = _make_flow(task_type="feature")
        step = _make_step(inputs={
            "commit_message": "Add new auth endpoint",
            "bump_type": "minor",
        })

        msg = _generate_commit_message(flow, step)
        # Subject line is the first line
        subject = msg.split("\n", 1)[0]
        assert subject == "feature: Add new auth endpoint (minor bump)"

    def test_bump_type_missing_omits_suffix(self):
        flow = _make_flow(task_type="bugfix")
        step = _make_step(inputs={
            "commit_message": "Fix login crash",
        })
        # Clear the default bump_type from _make_step's defaults
        step.inputs.pop("bump_type", None)

        msg = _generate_commit_message(flow, step)
        subject = msg.split("\n", 1)[0]
        assert subject == "bugfix: Fix login crash"
        assert "bump)" not in subject

    def test_bump_type_none_omits_suffix(self):
        flow = _make_flow(task_type="small")
        step = _make_step(inputs={
            "commit_message": "Tidy comments",
            "bump_type": "none",
        })

        msg = _generate_commit_message(flow, step)
        subject = msg.split("\n", 1)[0]
        assert "bump)" not in subject

    def test_bump_type_empty_string_omits_suffix(self):
        flow = _make_flow(task_type="feature")
        step = _make_step(inputs={
            "commit_message": "Add X",
            "bump_type": "",
        })

        msg = _generate_commit_message(flow, step)
        subject = msg.split("\n", 1)[0]
        assert "bump)" not in subject
