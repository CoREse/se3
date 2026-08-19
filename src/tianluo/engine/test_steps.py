"""Integration tests for flow engine step handlers.

Tests step implementations with mocked LLM calls.
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from .models import FlowInstance, Step, StepStatus, StepType
from .steps import (
    STEP_HANDLERS,
    analyze_handler,
    test_handler as run_test_step,
    commit_handler,
)


class TestAnalyzeStep:
    """Tests for the analyze step."""

    @patch("tianluo.engine.steps.analyze.ProjectContextCollector")
    @patch("tianluo.engine.steps.analyze.LLMCaller")
    def test_analyze_success(self, MockLLMCaller, MockCollector):
        """Test successful analysis produces classification fields.

        The retired spec-selection mechanism no longer produces
        ``relevant_specs`` / ``selected_items`` — they are emitted empty so
        defensive downstream consumers degrade cleanly — and no longer emits
        ``spec_content`` at all. The legacy ``selected_specs`` key never
        appears either.
        """
        mock_caller = MagicMock()
        mock_caller.call.return_value = json.dumps({
            "task_type": "feature",
            "scope": "backend",
            "complexity": "medium",
            "reasoning": "New user login feature",
            "root_cause_clear": True,
        })
        MockLLMCaller.return_value = mock_caller

        # Mock project context collector
        mock_collector_inst = MagicMock()
        mock_collector_inst.collect.return_value = {
            "git": {"branch": "main", "uncommitted_count": 0},
            "specs": ["base", "flow-engine"],
        }
        MockCollector.return_value = mock_collector_inst

        flow = FlowInstance(task_description="Add user login")
        step = Step(step_type=StepType.ANALYZE)
        step.inputs["task_description"] = "Add user login"

        result = analyze_handler(step, flow)

        assert result == StepStatus.COMPLETED
        # Classification outputs
        assert step.outputs["task_type"] == "feature"
        assert step.outputs["scope"] == "backend"
        assert step.outputs["complexity"] == "medium"
        assert step.outputs["reasoning"] == "New user login feature"
        assert step.outputs["root_cause_clear"] is True
        # Project summary is collected programmatically
        assert isinstance(step.outputs["project_summary"], str)
        assert len(step.outputs["project_summary"]) > 0
        # Retired spec-selection outputs are present but empty
        assert step.outputs["relevant_specs"] == []
        assert step.outputs["selected_items"] == []
        # The spec content channel is gone entirely.
        assert "spec_content" not in step.outputs
        # The legacy selected_specs key must never appear
        assert "selected_specs" not in step.outputs

    @patch("tianluo.engine.steps.analyze.ProjectContextCollector")
    @patch("tianluo.engine.steps.analyze.LLMCaller")
    def test_analyze_missing_root_cause_clear_defaults_to_false(
        self, MockLLMCaller, MockCollector, caplog
    ):
        """An analysis that never made the judgement degrades toward investigating.

        Older agents (and any prompt drift) can omit the field entirely; the
        conservative default must be false — a needless investigation round is
        cheap, planning a fix for an unidentified mechanism is not.
        """
        mock_caller = MagicMock()
        mock_caller.call.return_value = json.dumps({
            "task_type": "bugfix",
            "scope": "backend",
            "complexity": "medium",
            "reasoning": "Login sometimes 500s",
        })
        MockLLMCaller.return_value = mock_caller

        mock_collector_inst = MagicMock()
        mock_collector_inst.collect.return_value = {
            "git": {"branch": "main", "uncommitted_count": 0},
        }
        MockCollector.return_value = mock_collector_inst

        flow = FlowInstance(task_description="Login sometimes 500s")
        step = Step(step_type=StepType.ANALYZE)
        step.inputs["task_description"] = "Login sometimes 500s"

        with caplog.at_level("WARNING", logger="tianluo.engine.steps.analyze"):
            result = analyze_handler(step, flow)

        assert result == StepStatus.COMPLETED
        assert step.outputs["root_cause_clear"] is False
        assert "root_cause_clear" in caplog.text
        # An unclear bugfix earns an investigation round before planning.
        assert StepType.INVESTIGATE in flow.state.selected_steps

    @patch("tianluo.engine.steps.analyze.LLMCaller")
    def test_analyze_invalid_json(self, MockLLMCaller):
        """Test handling of invalid JSON from LLM."""
        mock_caller = MagicMock()
        mock_caller.call.return_value = "not valid json"
        MockLLMCaller.return_value = mock_caller

        flow = FlowInstance(task_description="Test task")
        step = Step(step_type=StepType.ANALYZE)
        step.inputs["task_description"] = "Test task"

        result = analyze_handler(step, flow)

        assert result == StepStatus.FAILED
        assert step.error_message is not None

    @patch("tianluo.engine.steps.analyze.LLMCaller")
    def test_analyze_llm_error(self, MockLLMCaller):
        """Test handling of LLM call failure."""
        from .llm_caller import LLMCallError

        mock_caller = MagicMock()
        mock_caller.call.side_effect = LLMCallError("API error")
        MockLLMCaller.return_value = mock_caller

        flow = FlowInstance(task_description="Test task")
        step = Step(step_type=StepType.ANALYZE)
        step.inputs["task_description"] = "Test task"

        result = analyze_handler(step, flow)

        assert result == StepStatus.FAILED


class TestTestStep:
    """Tests for the test step."""

    @pytest.fixture(autouse=True)
    def _warm_main_repo_root_cache(self, monkeypatch):
        """Pre-warm the git-probe lru_cache before ``@patch('subprocess.Popen')``.

        ``TestConfig.load`` (called at the top of ``test_handler``) resolves the
        main-repo root via ``_resolve_main_repo_root_cached``, which shells out
        to git through ``subprocess.run`` → ``subprocess.Popen``. The tests below
        patch ``subprocess.Popen`` to stub the test-command execution, which also
        corrupts that git probe when its cache is cold — so the tests only pass
        in the full suite (where an earlier test happens to warm the cache) and
        fail in isolation. This autouse fixture runs *before* the patch is
        applied, warming the cache for the current working directory so the
        probe never runs under the patched Popen.

        It also clears ``SE3_TEST_RUNNING`` from the environment for the test's
        duration. ``_run_command`` short-circuits to a ``passed=True`` skip
        result when that sentinel is set (its recursive-invocation guard), which
        fires whenever this suite is itself launched *by* the se3 test step
        (which exports ``SE3_TEST_RUNNING=1`` before spawning pytest). With the
        sentinel inherited, the failure-asserting test below would never reach
        its mocked ``Popen`` and would observe COMPLETED instead of
        REVISION_NEEDED. Clearing it makes these tests exercise the real path
        regardless of how the outer suite was invoked.
        """
        import tianluo.config as _cfg

        monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)
        _cfg._resolve_main_repo_root(Path.cwd())
        yield

    @patch("subprocess.Popen")
    def test_test_success(self, mock_popen):
        """Test successful test execution."""
        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_process.communicate.return_value = ("5 passed, 0 failed", "")
        mock_popen.return_value = mock_process

        flow = FlowInstance(task_description="Test feature")
        step = Step(step_type=StepType.TEST)

        result = run_test_step(step, flow)

        assert result == StepStatus.COMPLETED
        assert "test_results" in step.outputs

    @patch("subprocess.Popen")
    def test_test_failure(self, mock_popen):
        """Test handling of test failures.

        pytest exited non-zero but no per-test ``file::test FAILED`` lines are
        parseable from the output, so this is an *unparseable* failure. Under
        the baseline-driven gate (steps/test.py) an unparseable failure is
        treated as introduced (not inherited from any baseline) and triggers
        the fix loop — the handler returns REVISION_NEEDED, not COMPLETED.
        (The old known_test_failures.json exemption that let some failures pass
        through as COMPLETED has been removed; the frozen pre-implement baseline
        is now the sole exemption source, and an empty baseline exempts nothing.)
        """
        mock_process = MagicMock()
        mock_process.returncode = 1
        mock_process.communicate.return_value = ("3 passed, 2 failed", "FAILED test_foo")
        mock_popen.return_value = mock_process

        flow = FlowInstance(task_description="Test feature")
        step = Step(step_type=StepType.TEST)

        result = run_test_step(step, flow)

        assert result == StepStatus.REVISION_NEEDED
        assert step.outputs["tests_passed"] is False
        # An unparseable failure with an empty baseline is an introduced
        # (blocking) failure, so the test step demands a fix.
        assert step.outputs["test_results"]["tests_blocking"] is True


class TestCommitStep:
    """Tests for the commit step."""

    @patch("tianluo.engine.steps.commit._load_version_config")
    @patch("subprocess.run")
    def test_commit_success(self, mock_run, mock_version_config):
        """Test successful commit (pure-git path, version bumping disabled).

        With version bumping disabled the commit handler still issues several
        git calls beyond the core add/commit/rev-parse: ``_has_changes`` runs
        ``git status --porcelain``, the runtime-leak guard runs
        ``git diff --cached --name-only -z``, and the root-whitelist diagnostic
        runs ``git ls-files``. Rather than a brittle positional list, dispatch
        each mock by git subcommand so the test is robust to the exact call
        order/count. Version-bump behaviour is covered separately in
        ``tests/engine/steps/test_commit.py``.
        """
        from .version_bumper import VersionConfig
        mock_version_config.return_value = VersionConfig(enabled=False)

        def fake_git(cmd, *args, **kwargs):
            sub = cmd[1] if isinstance(cmd, (list, tuple)) and len(cmd) > 1 else ""
            if sub == "status":
                # _has_changes fallback: non-empty porcelain => has changes.
                return MagicMock(returncode=0, stdout="M file.py", stderr="")
            if sub == "commit":
                return MagicMock(returncode=0, stdout="[main abc123] message", stderr="")
            if sub == "rev-parse":
                return MagicMock(returncode=0, stdout="abc123def456", stderr="")
            # add / diff (leak guard, empty => no leaks) / ls-files / anything
            # else: succeed with empty output.
            return MagicMock(returncode=0, stdout="", stderr="")

        mock_run.side_effect = fake_git

        with tempfile.TemporaryDirectory() as tmpdir:
            flow = FlowInstance(task_description="Test commit")
            flow.change_path = Path(tmpdir) / "dummy"
            step = Step(step_type=StepType.COMMIT)

            result = commit_handler(step, flow)

            assert result == StepStatus.COMPLETED

    @patch("subprocess.run")
    def test_commit_no_changes(self, mock_run):
        """Test commit when there are no changes."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="",  # No changes
            stderr="",
        )

        flow = FlowInstance(task_description="Test commit")
        step = Step(step_type=StepType.COMMIT)

        result = commit_handler(step, flow)

        # Should succeed even with no changes
        assert result == StepStatus.COMPLETED


class TestVersionAnalyzeDeVersioning:
    """version_analyze: worktree flows emit a VersionIntent, not an authoritative
    suggested_version; synchronous flows keep their current behaviour."""

    _LLM_JSON = json.dumps({
        "suggested_version": "1.4.0",
        "bump_type": "minor",
        "reasoning": "Added a new capability",
        "confidence": "high",
        "commit_message": "Add new capability",
        "versions_changes": [
            "Add new capability X",
            "Wire capability X into the CLI",
        ],
    })

    def _run(self, tmpdir, *, worktree: bool):
        from .steps.version_analyze import version_analyze_handler

        with patch("tianluo.engine.steps.version_analyze.LLMCaller") as MockCaller:
            mock_caller = MagicMock()
            mock_caller.call.return_value = self._LLM_JSON
            MockCaller.return_value = mock_caller

            flow = FlowInstance(task_description="Add capability X")
            flow.task_type = "feature"
            flow.is_worktree_mode = worktree
            flow.change_path = Path(tmpdir) / "dummy"
            step = Step(step_type=StepType.VERSION_ANALYZE)
            step.inputs["task_description"] = "Add capability X"
            step.inputs["pre_session_version"] = "1.3.2"
            step.inputs["files_changed"] = ["src/cap.py"]

            result = version_analyze_handler(step, flow)
        return flow, step, result

    def test_worktree_flow_emits_intent_and_no_authoritative_version(self):
        """A worktree flow writes a VersionIntent file and does NOT expose an
        authoritative suggested_version the commit step could consume."""
        from .version_intent import read_intent

        with tempfile.TemporaryDirectory() as tmpdir:
            flow, step, result = self._run(tmpdir, worktree=True)

            assert result == StepStatus.COMPLETED
            # No authoritative version — the merge side decides it.
            assert "suggested_version" not in step.outputs
            # Provisional reference is retained, but only as provisional.
            assert step.outputs["provisional_suggested_version"] == "1.4.0"
            # Intent metadata surfaced in outputs.
            assert step.outputs["version_intent"]["flow_id"] == flow.flow_id
            # Bump hint + changelog bullets still forwarded for display / docs.
            assert step.outputs["bump_type"] == "minor"
            assert step.outputs["versions_changes"] == [
                "Add new capability X",
                "Wire capability X into the CLI",
            ]

            # Intent persisted on the (to-be-committed) flow branch path.
            intent = read_intent(Path(tmpdir), flow.flow_id)
            assert intent is not None
            assert intent.provisional_suggested_version == "1.4.0"
            assert intent.bump_type == "minor"
            assert intent.pre_session_baseline == "1.3.2"
            assert intent.versions_changes == [
                "Add new capability X",
                "Wire capability X into the CLI",
            ]
            # change_summary is the intent's substance and must be non-empty
            # even though bump_type happens to be usable here.
            assert intent.change_summary.strip()
            assert "src/cap.py" in intent.change_summary

    def test_worktree_intent_body_complete_without_usable_bump_type(self):
        """Under custom rules bump_type may be absent/lossy; the intent body
        (change_summary + versions_changes) must still be complete."""
        from .steps.version_analyze import version_analyze_handler
        from .version_intent import read_intent

        with tempfile.TemporaryDirectory() as tmpdir:
            with patch("tianluo.engine.steps.version_analyze.LLMCaller") as MockCaller:
                mock_caller = MagicMock()
                # No bump_type in the response -> _validate_result defaults it,
                # but the intent's substance must not depend on it.
                mock_caller.call.return_value = json.dumps({
                    "suggested_version": "2024.07.06",
                    "reasoning": "date-based scheme",
                    "confidence": "medium",
                    "commit_message": "Ship July build",
                    "versions_changes": ["Ship the July build"],
                })
                MockCaller.return_value = mock_caller

                flow = FlowInstance(task_description="Ship build")
                flow.task_type = "feature"
                flow.is_worktree_mode = True
                flow.change_path = Path(tmpdir) / "dummy"
                step = Step(step_type=StepType.VERSION_ANALYZE)
                step.inputs["pre_session_version"] = "2024.07.01"
                step.inputs["files_changed"] = ["build.py"]

                result = version_analyze_handler(step, flow)

            assert result == StepStatus.COMPLETED
            intent = read_intent(Path(tmpdir), flow.flow_id)
            assert intent is not None
            # Substance is complete regardless of bump_type.
            assert intent.versions_changes == ["Ship the July build"]
            assert intent.change_summary.strip()
            assert intent.provisional_suggested_version == "2024.07.06"

    def test_synchronous_flow_keeps_authoritative_version(self):
        """A non-worktree flow still exposes the authoritative suggested_version
        and writes NO intent file (behaviour unchanged)."""
        from .version_intent import read_intent

        with tempfile.TemporaryDirectory() as tmpdir:
            flow, step, result = self._run(tmpdir, worktree=False)

            assert result == StepStatus.COMPLETED
            assert step.outputs["suggested_version"] == "1.4.0"
            assert "version_intent" not in step.outputs
            assert "provisional_suggested_version" not in step.outputs
            assert step.outputs["bump_type"] == "minor"
            # No intent persisted for a synchronous flow.
            assert read_intent(Path(tmpdir), flow.flow_id) is None


class TestCommitDeVersioning:
    """commit step: worktree flows write no version file / VERSIONS.md /
    ``Version:`` line, only the bump-intent message decoration."""

    def _fake_git(self, captured):
        def fake_git(cmd, *args, **kwargs):
            sub = cmd[1] if isinstance(cmd, (list, tuple)) and len(cmd) > 1 else ""
            if sub == "status":
                return MagicMock(returncode=0, stdout="M file.py", stderr="")
            if sub == "commit":
                # cmd = ["git", "commit", "-m", <message>]
                captured["message"] = cmd[3] if len(cmd) > 3 else ""
                return MagicMock(returncode=0, stdout="[main abc123] msg", stderr="")
            if sub == "rev-parse":
                return MagicMock(returncode=0, stdout="abc123def456", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        return fake_git

    @patch("tianluo.engine.steps.commit.VersionBumper")
    @patch("tianluo.engine.steps.commit._load_version_config")
    @patch("subprocess.run")
    def test_worktree_commit_skips_version_and_docs(
        self, mock_run, mock_version_config, MockBumper
    ):
        """With version bumping ENABLED but the flow in worktree mode, the commit
        must not read/write the version file and must not stamp a Version line."""
        from .version_bumper import VersionConfig

        mock_version_config.return_value = VersionConfig(enabled=True)
        mock_bumper = MagicMock()
        MockBumper.return_value = mock_bumper

        captured: dict[str, str] = {}
        mock_run.side_effect = self._fake_git(captured)

        with tempfile.TemporaryDirectory() as tmpdir:
            flow = FlowInstance(task_description="Add worktree feature")
            flow.task_type = "feature"
            flow.is_worktree_mode = True
            flow.change_path = Path(tmpdir) / "dummy"
            step = Step(step_type=StepType.COMMIT)
            # These would be forwarded from version_analyze; a worktree flow
            # never carries an authoritative suggested_version, but even if a
            # stray one were present the commit must ignore it.
            step.inputs["bump_type"] = "minor"
            step.inputs["commit_message"] = "Add worktree feature"

            result = commit_handler(step, flow)

            assert result == StepStatus.COMPLETED
            # The version file was never touched.
            mock_bumper.set_version.assert_not_called()
            mock_bumper.detect_version_file.assert_not_called()
            # No authoritative version recorded on the step.
            assert "version" not in step.outputs
            assert step.outputs.get("version_bumped") is not True
            # Message carries the bump-intent decoration but NO Version: line.
            assert "(minor bump)" in captured["message"]
            assert "Version:" not in captured["message"]

    def test_worktree_commit_message_has_no_version_line(self):
        """_generate_commit_message: a worktree commit (new_version=None) yields
        the bump decoration but never a ``Version:`` line."""
        from .steps.commit import _generate_commit_message
        from .version_bumper import VersionConfig

        flow = FlowInstance(task_description="Add capability")
        flow.task_type = "feature"
        flow.is_worktree_mode = True
        step = Step(step_type=StepType.COMMIT)
        step.inputs["commit_message"] = "Add capability"
        step.inputs["bump_type"] = "minor"

        # new_version=None mirrors the worktree path where no version was written.
        message = _generate_commit_message(
            flow, step, new_version=None, version_config=VersionConfig(enabled=True)
        )
        assert "(minor bump)" in message
        assert "Version:" not in message

    def test_synchronous_commit_message_keeps_version_line(self):
        """Contrast: a synchronous commit with a written version keeps the
        ``Version:`` line — behaviour unchanged."""
        from .steps.commit import _generate_commit_message
        from .version_bumper import VersionConfig

        flow = FlowInstance(task_description="Add capability")
        flow.task_type = "feature"
        step = Step(step_type=StepType.COMMIT)
        step.inputs["commit_message"] = "Add capability"
        step.inputs["bump_type"] = "minor"

        message = _generate_commit_message(
            flow, step, new_version="1.4.0", version_config=VersionConfig(enabled=True)
        )
        assert "Version: 1.4.0" in message

    def test_discovery_flow_commit_prefix_uses_real_analyzed_type(self):
        """A --discover run carries flow.task_type='discovery' for its step
        sequence, but the commit subject must be prefixed with the real analyzed
        type (via effective_task_type), never 'discovery:'."""
        from .steps.commit import _generate_commit_message
        from .version_bumper import VersionConfig

        flow = FlowInstance(task_description="Build a thing")
        flow.task_type = "discovery"
        # analyze persisted the real inferred type here.
        flow.state.context["analyzed_type"] = "feature"
        step = Step(step_type=StepType.COMMIT)
        step.inputs["commit_message"] = "Build a thing"

        message = _generate_commit_message(
            flow, step, new_version="1.4.0", version_config=VersionConfig(enabled=True)
        )
        assert message.startswith("feature:")
        assert "discovery:" not in message

    def test_discovery_flow_without_analyzed_type_falls_back_to_feature(self):
        """Old state: flow.task_type='discovery' with no analyzed_type still
        never yields a 'discovery:' prefix — it degrades to 'feature:'."""
        from .steps.commit import _generate_commit_message
        from .version_bumper import VersionConfig

        flow = FlowInstance(task_description="Build a thing")
        flow.task_type = "discovery"
        step = Step(step_type=StepType.COMMIT)
        step.inputs["commit_message"] = "Build a thing"

        message = _generate_commit_message(
            flow, step, new_version="1.4.0", version_config=VersionConfig(enabled=True)
        )
        assert message.startswith("feature:")
        assert "discovery:" not in message


class TestCommitVersionRaceGuard:
    """commit step (change D): a synchronous commit re-checks the disk version
    against the version version_analyze OBSERVED on disk (its ``current_version``,
    NOT the pre-session baseline) in-lock; on drift (a concurrent direct-run flow
    bumped first) it re-runs version_analyze against the drifted baseline so the
    two flows do not collide on the same number (the 10.7.1 double-bump). The
    operand distinction matters: comparing against ``pre_session_version`` would
    misread this flow's OWN session commits (already folded into
    ``current_version``) as concurrent drift."""

    def _fake_git(self, captured):
        def fake_git(cmd, *args, **kwargs):
            sub = cmd[1] if isinstance(cmd, (list, tuple)) and len(cmd) > 1 else ""
            if sub == "status":
                return MagicMock(returncode=0, stdout="M file.py", stderr="")
            if sub == "commit":
                captured["message"] = cmd[3] if len(cmd) > 3 else ""
                return MagicMock(returncode=0, stdout="[main abc123] msg", stderr="")
            if sub == "rev-parse":
                return MagicMock(returncode=0, stdout="abc123def456", stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")
        return fake_git

    def _make_flow_with_version_analyze(self, tmpdir, *, va_current, baseline):
        """Build a non-worktree flow carrying a completed version_analyze step."""
        flow = FlowInstance(task_description="Add feature")
        flow.task_type = "feature"
        flow.is_worktree_mode = False
        flow.change_path = Path(tmpdir) / "dummy"

        va = Step(step_type=StepType.VERSION_ANALYZE)
        va.status = StepStatus.COMPLETED
        va.outputs["current_version"] = va_current
        va.outputs["suggested_version"] = "10.7.1"
        va.inputs["pre_session_version"] = baseline
        flow.state.add_step(va)
        return flow

    def _make_commit_step(self, *, suggested, baseline):
        step = Step(step_type=StepType.COMMIT)
        step.inputs["suggested_version"] = suggested
        step.inputs["pre_session_version"] = baseline
        step.inputs["bump_type"] = "patch"
        return step

    @patch("tianluo.engine.steps.commit._update_docs")
    @patch("tianluo.engine.steps.commit._read_head_commit", return_value=("abc123", ""))
    @patch("tianluo.engine.steps.version_analyze.version_analyze_handler")
    @patch("tianluo.engine.steps.commit.VersionBumper")
    @patch("tianluo.engine.steps.commit._load_version_config")
    @patch("subprocess.run")
    def test_drift_triggers_reanalyze_and_avoids_collision(
        self, mock_run, mock_cfg, MockBumper, mock_reanalyze, mock_hash, mock_docs
    ):
        """Disk drifted 10.7.0 -> 10.7.1 (a concurrent flow bumped first). The
        stale suggested_version 10.7.1 must NOT be written; version_analyze is
        re-run against the drifted baseline and its 10.7.2 is written instead."""
        from .version_bumper import VersionConfig

        mock_cfg.return_value = VersionConfig(enabled=True)

        mock_bumper = MagicMock()
        MockBumper.return_value = mock_bumper
        mock_bumper.detect_version_file.return_value = Path("/tmp/project/pyproject.toml")
        # In-lock disk read reveals the concurrent bump: 10.7.1, not the
        # pre-session baseline 10.7.0.
        mock_bumper.read_version.return_value = "10.7.1"
        mock_bumper.set_version.side_effect = lambda version, path: version

        def fake_reanalyze(va_step, flow):
            # Re-analysis against the drifted baseline yields the next patch.
            va_step.outputs["suggested_version"] = "10.7.2"
            va_step.outputs["bump_type"] = "patch"
            va_step.outputs["commit_message"] = "recomputed"
            va_step.outputs["versions_changes"] = ["recomputed entry"]
            va_step.outputs["reasoning"] = "drift recompute"
            return StepStatus.COMPLETED

        mock_reanalyze.side_effect = fake_reanalyze

        captured: dict[str, str] = {}
        mock_run.side_effect = self._fake_git(captured)

        with tempfile.TemporaryDirectory() as tmpdir:
            flow = self._make_flow_with_version_analyze(
                tmpdir, va_current="10.7.0", baseline="10.7.0"
            )
            step = self._make_commit_step(suggested="10.7.1", baseline="10.7.0")

            result = commit_handler(step, flow)

            assert result == StepStatus.COMPLETED
            # Drift detected -> version_analyze re-run against the drifted baseline.
            assert mock_reanalyze.called
            new_baseline = mock_reanalyze.call_args.args[0].inputs["pre_session_version"]
            assert new_baseline == "10.7.1"
            # The recomputed, non-colliding version was written — never the stale
            # 10.7.1 that would double-bump.
            written = [c.kwargs.get("version") for c in mock_bumper.set_version.call_args_list]
            assert written == ["10.7.2"]
            assert step.outputs["version"] == "10.7.2"

    @patch("tianluo.engine.steps.commit._update_docs")
    @patch("tianluo.engine.steps.commit._read_head_commit", return_value=("abc123", ""))
    @patch("tianluo.engine.steps.version_analyze.version_analyze_handler")
    @patch("tianluo.engine.steps.commit.VersionBumper")
    @patch("tianluo.engine.steps.commit._load_version_config")
    @patch("subprocess.run")
    def test_no_drift_writes_target_unchanged(
        self, mock_run, mock_cfg, MockBumper, mock_reanalyze, mock_hash, mock_docs
    ):
        """When the disk version still equals the pre-session baseline there was
        no concurrent bump: version_analyze is NOT re-run and the resolved target
        is written verbatim — behaviour unchanged."""
        from .version_bumper import VersionConfig

        mock_cfg.return_value = VersionConfig(enabled=True)

        mock_bumper = MagicMock()
        MockBumper.return_value = mock_bumper
        mock_bumper.detect_version_file.return_value = Path("/tmp/project/pyproject.toml")
        # Disk still at the baseline — no drift.
        mock_bumper.read_version.return_value = "10.7.0"
        mock_bumper.set_version.side_effect = lambda version, path: version

        captured: dict[str, str] = {}
        mock_run.side_effect = self._fake_git(captured)

        with tempfile.TemporaryDirectory() as tmpdir:
            flow = self._make_flow_with_version_analyze(
                tmpdir, va_current="10.7.0", baseline="10.7.0"
            )
            step = self._make_commit_step(suggested="10.7.1", baseline="10.7.0")

            result = commit_handler(step, flow)

            assert result == StepStatus.COMPLETED
            mock_reanalyze.assert_not_called()
            written = [c.kwargs.get("version") for c in mock_bumper.set_version.call_args_list]
            assert written == ["10.7.1"]
            assert step.outputs["version"] == "10.7.1"

    @patch("tianluo.engine.steps.commit._update_docs")
    @patch("tianluo.engine.steps.commit._read_head_commit", return_value=("abc123", ""))
    @patch("tianluo.engine.steps.version_analyze.version_analyze_handler")
    @patch("tianluo.engine.steps.commit.VersionBumper")
    @patch("tianluo.engine.steps.commit._load_version_config")
    @patch("subprocess.run")
    def test_own_session_advance_is_not_drift(
        self, mock_run, mock_cfg, MockBumper, mock_reanalyze, mock_hash, mock_docs
    ):
        """The guard's operand is version_analyze's OBSERVED disk version
        (``current_version``), NOT the pre-session baseline. When this flow's own
        session/implement commits advanced the version file, version_analyze
        observes the advanced value (current_version 10.7.5) while the pre-session
        baseline lags (10.7.0). Disk == current_version, so this is NOT drift: no
        re-analysis, target written verbatim.

        Discriminates the operand: the own-replay escape hatches are all left
        UNSTUBBED (no flow_committed_version, no Flow-trailer commits), so a wrong
        implementation comparing disk against pre_session_version (10.7.0) would
        see a spurious 10.7.5!=10.7.0 drift, fall through every own-replay probe,
        and re-run version_analyze — failing this test."""
        from .version_bumper import VersionConfig

        mock_cfg.return_value = VersionConfig(enabled=True)

        mock_bumper = MagicMock()
        MockBumper.return_value = mock_bumper
        mock_bumper.detect_version_file.return_value = Path("/tmp/project/pyproject.toml")
        # Disk holds the version version_analyze already observed (own session
        # commits advanced it past the pre-session baseline). Also serves as the
        # HEAD-blob read inside _version_at_commit, so head == disk (a committed,
        # not uncommitted, state).
        mock_bumper.read_version.return_value = "10.7.5"
        mock_bumper.set_version.side_effect = lambda version, path: version

        captured: dict[str, str] = {}
        mock_run.side_effect = self._fake_git(captured)

        with tempfile.TemporaryDirectory() as tmpdir:
            # current_version (observed) 10.7.5 != pre_session baseline 10.7.0.
            flow = self._make_flow_with_version_analyze(
                tmpdir, va_current="10.7.5", baseline="10.7.0"
            )
            step = self._make_commit_step(suggested="10.8.0", baseline="10.7.0")

            result = commit_handler(step, flow)

            assert result == StepStatus.COMPLETED
            # Disk == observed current_version -> NOT drift -> no recompute.
            mock_reanalyze.assert_not_called()
            written = [c.kwargs.get("version") for c in mock_bumper.set_version.call_args_list]
            assert written == ["10.8.0"]
            assert step.outputs["version"] == "10.8.0"

    @patch("tianluo.engine.steps.commit._update_docs")
    @patch("tianluo.engine.steps.commit._read_head_commit", return_value=("abc123", ""))
    @patch("tianluo.engine.steps.version_analyze.version_analyze_handler")
    @patch("tianluo.engine.steps.commit.VersionBumper")
    @patch("tianluo.engine.steps.commit._load_version_config")
    @patch("subprocess.run")
    def test_drift_reanalyze_failure_halts_commit(
        self, mock_run, mock_cfg, MockBumper, mock_reanalyze, mock_hash, mock_docs
    ):
        """If the drift re-analysis fails to produce a version, the commit must
        halt (FAIL) rather than silently write the stale, colliding target."""
        from .version_bumper import VersionConfig

        mock_cfg.return_value = VersionConfig(enabled=True)

        mock_bumper = MagicMock()
        MockBumper.return_value = mock_bumper
        mock_bumper.detect_version_file.return_value = Path("/tmp/project/pyproject.toml")
        mock_bumper.read_version.return_value = "10.7.1"
        mock_bumper.set_version.side_effect = lambda version, path: version

        def fake_reanalyze(va_step, flow):
            # Re-analysis failed to yield a version.
            return StepStatus.FAILED

        mock_reanalyze.side_effect = fake_reanalyze

        captured: dict[str, str] = {}
        mock_run.side_effect = self._fake_git(captured)

        with tempfile.TemporaryDirectory() as tmpdir:
            flow = self._make_flow_with_version_analyze(
                tmpdir, va_current="10.7.0", baseline="10.7.0"
            )
            step = self._make_commit_step(suggested="10.7.1", baseline="10.7.0")

            result = commit_handler(step, flow)

            assert result == StepStatus.FAILED
            # The colliding target was never written.
            mock_bumper.set_version.assert_not_called()

    @patch("tianluo.engine.steps.commit._update_docs")
    @patch("tianluo.engine.steps.commit._read_head_commit", return_value=("abc123", ""))
    @patch("tianluo.engine.steps.version_analyze.version_analyze_handler")
    @patch("tianluo.engine.steps.commit.VersionBumper")
    @patch("tianluo.engine.steps.commit._load_version_config")
    @patch("subprocess.run")
    def test_crash_resume_uncommitted_own_write_kept(
        self, mock_run, mock_cfg, MockBumper, mock_reanalyze, mock_hash, mock_docs
    ):
        """Crash-resume window: set_version wrote the version file (disk 10.7.5)
        but the process died before ``git commit``, so HEAD's blob still holds the
        pre-crash version (10.7.0). The drift is this flow's OWN uncommitted
        residue — the distinguishing fact is HEAD != disk (an uncommitted write) —
        so the target must be KEPT verbatim and re-written, never re-analysed
        (re-analysing off our own half-written bump would over-advance)."""
        from .version_bumper import VersionConfig

        mock_cfg.return_value = VersionConfig(enabled=True)

        detect = Path("/tmp/project/pyproject.toml")
        mock_bumper = MagicMock()
        MockBumper.return_value = mock_bumper
        mock_bumper.detect_version_file.return_value = detect
        # Must be a real bool: the crash-resume branch is gated on
        # ``not _use_script_mode`` (a MagicMock attr would read truthy and skip it).
        mock_bumper._use_script_mode = False

        def fake_read(path):
            # In-lock disk read sees our uncommitted set_version residue (10.7.5);
            # the HEAD-blob read inside _version_at_commit (a temp file) sees the
            # pre-crash committed version (10.7.0) — head != disk.
            return "10.7.5" if Path(path) == detect else "10.7.0"

        mock_bumper.read_version.side_effect = fake_read
        mock_bumper.set_version.side_effect = lambda version, path: version

        captured: dict[str, str] = {}
        mock_run.side_effect = self._fake_git(captured)

        with tempfile.TemporaryDirectory() as tmpdir:
            # Observed baseline 10.7.0; disk drifted to 10.7.5 (our own residue).
            flow = self._make_flow_with_version_analyze(
                tmpdir, va_current="10.7.0", baseline="10.7.0"
            )
            step = self._make_commit_step(suggested="10.8.0", baseline="10.7.0")

            result = commit_handler(step, flow)

            assert result == StepStatus.COMPLETED
            # Own uncommitted residue recognised -> NO re-analysis, target kept.
            mock_reanalyze.assert_not_called()
            written = [c.kwargs.get("version") for c in mock_bumper.set_version.call_args_list]
            assert written == ["10.8.0"]
            assert step.outputs["version"] == "10.8.0"

    @patch("tianluo.engine.steps.commit._update_docs")
    @patch("tianluo.engine.steps.commit._read_head_commit", return_value=("abc123", ""))
    @patch("tianluo.engine.steps.version_analyze.version_analyze_handler")
    @patch("tianluo.engine.steps.commit.VersionBumper")
    @patch("tianluo.engine.steps.commit._load_version_config")
    @patch("subprocess.run")
    def test_concurrent_drift_committed_head_triggers_reanalyze(
        self, mock_run, mock_cfg, MockBumper, mock_reanalyze, mock_hash, mock_docs
    ):
        """The inverse of the crash-resume case: the drift IS committed (HEAD blob
        == disk 10.7.1, a concurrent flow's landed bump), so it is NOT our own
        residue — the crash-resume self-heal must NOT fire and version_analyze IS
        re-run against the drifted baseline. Guards against inverting the
        head-vs-disk classification (which would keep a colliding version)."""
        from .version_bumper import VersionConfig

        mock_cfg.return_value = VersionConfig(enabled=True)

        mock_bumper = MagicMock()
        MockBumper.return_value = mock_bumper
        mock_bumper.detect_version_file.return_value = Path("/tmp/project/pyproject.toml")
        mock_bumper._use_script_mode = False
        # Both the in-lock read and the HEAD-blob read return 10.7.1 -> head == disk
        # (a committed concurrent bump, not our uncommitted residue).
        mock_bumper.read_version.return_value = "10.7.1"
        mock_bumper.set_version.side_effect = lambda version, path: version

        def fake_reanalyze(va_step, flow):
            va_step.outputs["suggested_version"] = "10.7.2"
            va_step.outputs["bump_type"] = "patch"
            va_step.outputs["commit_message"] = "recomputed"
            va_step.outputs["versions_changes"] = ["recomputed entry"]
            return StepStatus.COMPLETED

        mock_reanalyze.side_effect = fake_reanalyze

        captured: dict[str, str] = {}
        mock_run.side_effect = self._fake_git(captured)

        with tempfile.TemporaryDirectory() as tmpdir:
            flow = self._make_flow_with_version_analyze(
                tmpdir, va_current="10.7.0", baseline="10.7.0"
            )
            step = self._make_commit_step(suggested="10.7.1", baseline="10.7.0")

            result = commit_handler(step, flow)

            assert result == StepStatus.COMPLETED
            # Committed HEAD == disk -> concurrent drift, NOT self-heal -> recompute.
            assert mock_reanalyze.called
            written = [c.kwargs.get("version") for c in mock_bumper.set_version.call_args_list]
            assert written == ["10.7.2"]

    @patch("tianluo.engine.steps.commit._update_docs")
    @patch("tianluo.engine.steps.commit._read_head_commit", return_value=("abc123", ""))
    @patch("tianluo.engine.steps.version_analyze.version_analyze_handler")
    @patch("tianluo.engine.steps.commit.VersionBumper")
    @patch("tianluo.engine.steps.commit._load_version_config")
    @patch("subprocess.run")
    def test_drift_reanalyze_regression_halts_commit(
        self, mock_run, mock_cfg, MockBumper, mock_reanalyze, mock_hash, mock_docs
    ):
        """A drift re-analysis that hallucinates a version LOWER than the drifted
        disk version (10.7.1 -> 10.6.1) must HALT the commit, not silently write a
        regression. Equality with disk is not the only bad outcome — the refusal
        must also block a monotonicity violation."""
        from .version_bumper import VersionConfig

        mock_cfg.return_value = VersionConfig(enabled=True)

        mock_bumper = MagicMock()
        MockBumper.return_value = mock_bumper
        mock_bumper.detect_version_file.return_value = Path("/tmp/project/pyproject.toml")
        # Truthy _use_script_mode skips the crash-resume branch, so the drift
        # routes straight to re-analysis (head-vs-disk is irrelevant here).
        mock_bumper.read_version.return_value = "10.7.1"
        mock_bumper.set_version.side_effect = lambda version, path: version

        def fake_reanalyze(va_step, flow):
            # Hallucinated regression below the drifted disk version.
            va_step.outputs["suggested_version"] = "10.6.1"
            va_step.outputs["bump_type"] = "patch"
            va_step.outputs["commit_message"] = "regress"
            va_step.outputs["versions_changes"] = ["regress entry"]
            return StepStatus.COMPLETED

        mock_reanalyze.side_effect = fake_reanalyze

        captured: dict[str, str] = {}
        mock_run.side_effect = self._fake_git(captured)

        with tempfile.TemporaryDirectory() as tmpdir:
            flow = self._make_flow_with_version_analyze(
                tmpdir, va_current="10.7.0", baseline="10.7.0"
            )
            step = self._make_commit_step(suggested="10.7.1", baseline="10.7.0")

            result = commit_handler(step, flow)

            assert result == StepStatus.FAILED
            # The regressing version was never written.
            mock_bumper.set_version.assert_not_called()


class TestStepHandlers:
    """Tests for the STEP_HANDLERS registry."""

    def test_all_step_types_have_handlers(self):
        """Verify all step types have registered handlers.

        The retired spec steps (VERIFY_SPEC / UPDATE_SPEC / SPEC_GATE) keep their
        deprecated enum members for persisted-flow rendering but have no handlers
        — they are excluded here.
        """
        retired = {StepType.VERIFY_SPEC, StepType.UPDATE_SPEC, StepType.SPEC_GATE}
        for step_type in StepType:
            if step_type in retired:
                assert step_type not in STEP_HANDLERS
                continue
            assert step_type in STEP_HANDLERS, f"Missing handler for {step_type}"

    def test_handler_consistency(self):
        """Test that handlers have required interface."""
        for step_type, handler_func in STEP_HANDLERS.items():
            assert callable(handler_func), f"Handler for {step_type} must be callable"


class TestLLMCallerIntegration:
    """Tests for LLM caller integration with retry logic."""

    # Pin agent rotation off: LLMCaller() with no explicit agents resolves the
    # project's tianluo.yaml multi-agent chain and rotates agents on every failure,
    # which would reach the real CodexRunner. Forcing _rotate_agent to report
    # "no rotation available" isolates the retry-on-same-agent behavior these
    # tests target (the single mocked ClaudeCodeRunner).
    @patch("tianluo.engine.llm_caller.LLMCaller._rotate_agent", return_value=False)
    @patch("tianluo.engine.llm_caller.ClaudeCodeRunner")
    def test_llm_retry_success(self, MockRunner, _mock_no_rotate):
        """Test that retries eventually succeed."""
        from .llm_caller import LLMCaller

        # Set up mock runner to fail twice, then succeed
        mock_runner = MagicMock()
        mock_result_fail = MagicMock(success=False, cmd_used="claude", returncode=1)
        mock_result_ok = MagicMock(success=True, output="success")
        mock_runner.run_with_monitor.side_effect = [
            mock_result_fail,
            mock_result_fail,
            mock_result_ok,
        ]
        MockRunner.return_value = mock_runner

        caller = LLMCaller(
            agents=[{"name": "test-claude", "type": "claude-code", "cmd": "claude"}],
            max_retries=3,
            retry_delay=0.01,
        )
        result = caller.call(prompt="test prompt")

        assert result == "success"
        assert mock_runner.run_with_monitor.call_count == 3

    @patch("tianluo.engine.llm_caller.LLMCaller._rotate_agent", return_value=False)
    @patch("tianluo.engine.llm_caller.ClaudeCodeRunner")
    def test_llm_retry_exhausted(self, MockRunner, _mock_no_rotate):
        """Test that retry exhaustion raises LLMCallError."""
        from .llm_caller import LLMCaller, LLMCallError

        mock_runner = MagicMock()
        mock_result_fail = MagicMock(success=False, cmd_used="claude", returncode=1)
        mock_runner.run_with_monitor.return_value = mock_result_fail
        MockRunner.return_value = mock_runner

        caller = LLMCaller(
            agents=[{"name": "test-claude", "type": "claude-code", "cmd": "claude"}],
            max_retries=2,
            retry_delay=0.01,
        )

        with pytest.raises(LLMCallError):
            caller.call(prompt="test prompt")

        assert mock_runner.run_with_monitor.call_count == 2


class TestContextBuilderSpecSurfaceRemoved:
    """The spec-loading surface of context_builder is gone with the spec mirror.

    ContextBuilder existed only to resolve the specs directory and read spec
    files; both jobs died with ``tianluo/specs/``, so the class itself — and the
    spec-name injection built on it — no longer exist.
    """

    def test_context_builder_class_removed(self):
        from . import context_builder

        assert not hasattr(context_builder, "ContextBuilder")

    def test_spec_names_injection_removed(self):
        from . import context_builder

        assert not hasattr(context_builder, "get_spec_names_injection")
        assert not hasattr(context_builder, "SPEC_NAMES_INJECTION_DEFAULT_STEPS")
        assert not hasattr(context_builder, "SPEC_NAMES_INJECTION_FORBIDDEN_STEPS")

    def test_charter_injection_retained(self):
        """The charter is the surviving project-convention injection surface."""
        from .context_builder import get_charter_injection

        assert callable(get_charter_injection)


class TestIssueDiscoveryInjection:
    """Tests for get_issue_discovery_injection() function."""

    def test_summarize_not_injected_by_default(self, tmp_path):
        """summarize is no longer in the default whitelist (B-class removed),
        so it receives no injection by default."""
        from .context_builder import get_issue_discovery_injection

        result = get_issue_discovery_injection("summarize", tmp_path)
        assert result == ""

    def test_verify_spec_not_injected_by_default(self, tmp_path):
        """verify_spec was removed from the default whitelist; it files
        out-of-scope issues deterministically instead."""
        from .context_builder import get_issue_discovery_injection

        result = get_issue_discovery_injection("verify_spec", tmp_path)
        assert result == ""

    def test_non_whitelisted_step_returns_empty(self, tmp_path):
        """Non-whitelisted step (propose) returns empty string."""
        from .context_builder import get_issue_discovery_injection

        result = get_issue_discovery_injection("propose", tmp_path)
        assert result == ""

    def test_forbidden_step_implement_returns_empty(self, tmp_path):
        """Forbidden step (implement) returns empty string."""
        from .context_builder import get_issue_discovery_injection

        result = get_issue_discovery_injection("implement", tmp_path)
        assert result == ""

    def test_forbidden_step_test_returns_empty(self, tmp_path):
        """Forbidden step (test) returns empty string."""
        from .context_builder import get_issue_discovery_injection

        result = get_issue_discovery_injection("test", tmp_path)
        assert result == ""

    def test_custom_whitelist_from_config(self, tmp_path):
        """Custom tianluo.yaml whitelist is respected."""
        from .context_builder import get_issue_discovery_injection

        # Create tianluo.yaml with custom whitelist including 'design'
        config_path = tmp_path / "tianluo.yaml"
        config_path.write_text(
            "issue_discovery:\n  steps:\n    - design\n    - summarize\n"
        )

        # 'design' should now return injection
        result = get_issue_discovery_injection("design", tmp_path)
        assert result != ""
        assert "discovered_issues" in result

        # 'verify_spec' is no longer in custom whitelist
        result = get_issue_discovery_injection("verify_spec", tmp_path)
        assert result == ""

    def test_forbidden_step_overrides_config(self, tmp_path):
        """Forbidden step returns empty even if config includes it."""
        from .context_builder import get_issue_discovery_injection

        # Create tianluo.yaml that tries to whitelist forbidden 'implement'
        config_path = tmp_path / "tianluo.yaml"
        config_path.write_text(
            "issue_discovery:\n  steps:\n    - implement\n    - summarize\n"
        )

        result = get_issue_discovery_injection("implement", tmp_path)
        assert result == ""

    def test_missing_config_uses_empty_default(self, tmp_path):
        """Missing tianluo.yaml uses the default whitelist, which is now empty —
        summarize receives no injection."""
        from .context_builder import get_issue_discovery_injection

        # No tianluo.yaml exists in tmp_path
        result = get_issue_discovery_injection("summarize", tmp_path)
        assert result == ""

    def test_default_whitelist_is_empty(self):
        """The default whitelist no longer contains any step."""
        from .context_builder import ISSUE_DISCOVERY_DEFAULT_STEPS

        assert ISSUE_DISCOVERY_DEFAULT_STEPS == []


class TestSummarizeHandlerIssueDiscoveryIntegration:
    """Integration test: summarize handler no longer injects issue discovery
    nor produces discovered_issues. Its job is a pure session report."""

    @patch("tianluo.engine.steps.summarize.LLMCaller")
    def test_summarize_prompt_omits_issue_discovery(self, MockLLMCaller):
        """The prompt sent to LLM by summarize must NOT contain issue discovery
        text, and the step must NOT produce discovered_issues."""
        mock_caller = MagicMock()
        # Return valid NDJSON with summary text
        mock_caller.call.return_value = '{"type": "assistant", "message": {"content": [{"type": "text", "text": "Summary here"}]}}'
        MockLLMCaller.return_value = mock_caller

        flow = FlowInstance(task_description="Test task")
        # Set change_path to a temp dir so project_root resolves
        with tempfile.TemporaryDirectory() as tmpdir:
            flow.change_path = Path(tmpdir) / "dummy"
            step = Step(step_type=StepType.SUMMARIZE)
            step.inputs["task_description"] = "Test task"

            from .steps.summarize import summarize_handler
            summarize_handler(step, flow)

            # Verify the prompt sent to LLM does NOT contain issue discovery text
            assert mock_caller.call.called
            call_kwargs = mock_caller.call.call_args
            prompt = call_kwargs.kwargs.get("prompt", "")
            if not prompt and call_kwargs.args:
                prompt = call_kwargs.args[0]
            assert "discovered_issues" not in prompt, (
                "summarize must not inject issue discovery into its prompt."
            )
            # And no discovered_issues output is produced.
            assert "discovered_issues" not in step.outputs


class TestBuildStepInputs:
    """Tests for _build_step_inputs ANALYZE mapping and downstream consumption."""

    def _make_state_machine(self, tmp_path):
        """Create a StateMachine with a temp project root."""
        from .state_machine import StateMachine
        return StateMachine(project_root=tmp_path)

    def _make_flow_with_analyze(self, task_desc="Test task", analyze_outputs=None):
        """Create a FlowInstance with a completed ANALYZE step in history."""
        flow = FlowInstance(task_description=task_desc)
        analyze_step = Step(step_type=StepType.ANALYZE)
        analyze_step.status = StepStatus.COMPLETED
        defaults = {
            "task_type": "feature",
            "scope": "backend",
            "project_summary": "Branch: main\nRecent commits:\n  - abc123",
            "relevant_specs": ["base", "flow-engine"],
            "spec_content": {
                "base": "# base spec content",
                "flow-engine": "# flow-engine spec content",
            },
        }
        if analyze_outputs:
            defaults.update(analyze_outputs)
        analyze_step.outputs = defaults
        flow.state.steps[analyze_step.step_id] = analyze_step
        flow.state.step_history.append(analyze_step.step_id)
        return flow

    def test_analyze_mapping_forwards_classification_and_summary(self, tmp_path):
        """ANALYZE forwards task_type / scope / project_summary downstream.

        The retired spec-selection mechanism no longer forwards spec_content /
        relevant_specs / selected_items; downstream steps receive the charter +
        code-index injection instead.
        """
        sm = self._make_state_machine(tmp_path)
        flow = self._make_flow_with_analyze()

        inputs = sm._build_step_inputs(flow, StepType.PLAN)

        assert inputs["project_summary"] == "Branch: main\nRecent commits:\n  - abc123"
        assert inputs["task_type"] == "feature"
        assert inputs["scope"] == "backend"
        # spec_content / relevant_specs are no longer forwarded from ANALYZE.
        assert "spec_content" not in inputs
        assert "relevant_specs" not in inputs

    def test_deprecated_project_summary_step_backward_compat(self, tmp_path):
        """Persisted flows with old PROJECT_SUMMARY step still provide project_summary."""
        sm = self._make_state_machine(tmp_path)
        flow = FlowInstance(task_description="Old flow")

        # Simulate old flow with separate PROJECT_SUMMARY step
        ps_step = Step(step_type=StepType.PROJECT_SUMMARY)
        ps_step.status = StepStatus.COMPLETED
        ps_step.outputs = {"project_summary": "Old-style project summary"}
        flow.state.steps[ps_step.step_id] = ps_step
        flow.state.step_history.append(ps_step.step_id)

        inputs = sm._build_step_inputs(flow, StepType.PLAN)

        assert inputs["project_summary"] == "Old-style project summary"

    def test_old_persisted_read_spec_step_type_raises(self):
        """Persisted flows with old READ_SPEC step type cannot be deserialized."""
        with pytest.raises(ValueError):
            StepType("read_spec")


class TestStepSequences:
    """Tests verifying step sequences do not contain deprecated PROJECT_SUMMARY/READ_SPEC."""

    def test_all_task_types_exclude_project_summary(self):
        """No default task-type sequence may contain PROJECT_SUMMARY."""
        from .models import get_default_step_sequence
        for task_type in ["feature", "bugfix", "review", "small", "discovery"]:
            seq = get_default_step_sequence(task_type)
            assert StepType.PROJECT_SUMMARY not in seq, (
                f"{task_type} sequence still contains PROJECT_SUMMARY"
            )

    def test_small_sequence(self):
        """Small task sequence (charter refactor): ANALYZE → IMPLEMENT → TEST →
        CHARTER_FRESHNESS → VERSION_ANALYZE → COMMIT → SUMMARIZE.

        Lightweight commit-only flows gain the non-blocking CHARTER_FRESHNESS
        advisory but not INVARIANT_CHECK (no self_check / spec phase to extend).
        """
        from .models import get_default_step_sequence
        seq = get_default_step_sequence("small")
        expected = [
            StepType.ANALYZE,
            StepType.IMPLEMENT,
            StepType.TEST,
            StepType.CHARTER_FRESHNESS,
            StepType.VERSION_ANALYZE,
            StepType.COMMIT,
            StepType.SUMMARIZE,
        ]
        assert seq == expected

    def test_feature_sequence_starts_with_analyze(self):
        """Feature sequence starts with ANALYZE (not PROJECT_SUMMARY)."""
        from .models import get_default_step_sequence
        seq = get_default_step_sequence("feature")
        assert seq[0] == StepType.ANALYZE

    def test_review_sequence_has_invariant_check(self):
        """Review sequence (charter refactor): ANALYZE → INVARIANT_CHECK → SUMMARIZE.

        The retired VERIFY_SPEC is replaced by the anchored INVARIANT_CHECK.
        """
        from .models import get_default_step_sequence
        seq = get_default_step_sequence("review")
        assert seq == [
            StepType.ANALYZE,
            StepType.INVARIANT_CHECK,
            StepType.SUMMARIZE,
        ]

    def test_discovery_sequence_starts_with_discovery_then_analyze(self):
        """Discovery sequence starts with DISCOVERY → ANALYZE (no PROJECT_SUMMARY)."""
        from .models import get_default_step_sequence
        seq = get_default_step_sequence("discovery")
        assert seq[0] == StepType.DISCOVERY
        assert seq[1] == StepType.ANALYZE
        assert StepType.PROJECT_SUMMARY not in seq


class TestFormatRoundUsageFooter:
    """The shared compact per-round usage footer formatter (G1 task 1).

    The label chrome is i18n-rendered; the engine conftest pins the UI language
    to en-US, so these assert the en-US catalog wording (zh-CN carries the
    ``本轮 … · 累计 …`` translation of the same keys).
    """

    def test_normal_values_render_round_and_cumulative(self):
        from .token_usage import UsageTotals, format_round_usage_footer

        footer = format_round_usage_footer(
            UsageTotals(input_tokens=1234, output_tokens=567),
            UsageTotals(input_tokens=12345, output_tokens=6789),
        )
        assert footer == "This round 1,234 in / 567 out · Total 12,345 in / 6,789 out"

    def test_uses_thousands_separators_not_abbreviation(self):
        from .token_usage import UsageTotals, format_round_usage_footer

        footer = format_round_usage_footer(
            UsageTotals(input_tokens=1200, output_tokens=3400),
            UsageTotals(input_tokens=1200, output_tokens=3400),
        )
        # Comma thousands separators (consistent with render_usage_block),
        # never a 1.2k-style abbreviation.
        assert "1,200" in footer
        assert "3,400" in footer
        assert "1.2k" not in footer

    def test_zero_values_render_zeros(self):
        from .token_usage import UsageTotals, format_round_usage_footer

        footer = format_round_usage_footer(UsageTotals(), UsageTotals())
        assert footer == "This round 0 in / 0 out · Total 0 in / 0 out"

    def test_none_inputs_degrade_to_zero(self):
        from .token_usage import format_round_usage_footer

        # The function does not gate on emptiness itself — that is the caller's
        # job — but None must degrade to zeros rather than raise.
        assert format_round_usage_footer(None, None) == (
            "This round 0 in / 0 out · Total 0 in / 0 out"
        )

    def test_only_input_output_shown(self):
        from .token_usage import UsageTotals, format_round_usage_footer

        footer = format_round_usage_footer(
            UsageTotals(
                input_tokens=10,
                output_tokens=20,
                cache_read_input_tokens=999,
                cache_creation_input_tokens=888,
                total_cost_usd=1.23,
            ),
            UsageTotals(input_tokens=30, output_tokens=40),
        )
        # Per the task copy format only input/output are surfaced — no cache
        # breakdown and no cost in the compact footer.
        assert footer == "This round 10 in / 20 out · Total 30 in / 40 out"
        assert "cache" not in footer
        assert "$" not in footer


class TestDiscoveryCarriedTokenUsage:
    """Discovery carries token usage across rounds despite step.outputs.clear().

    Regression for the bug where discovery_handler's mid-handler
    ``step.outputs.clear()`` wiped the ``carried_token_usage`` written by
    run_step's finally block on the previous PAUSED round, so the terminal
    ``token_usage`` only reflected the last round instead of the whole
    discovery's real cumulative total (G1 task 2).
    """

    def test_carried_usage_accumulates_across_rounds_and_clear(self):
        from .state_machine import StateMachine
        from .steps import discovery as discovery_mod
        from .token_usage import UsageTotals, add_call_usage

        rounds = [
            (
                UsageTotals(input_tokens=100, output_tokens=10, total_cost_usd=0.01),
                {"mode": "question", "content": "c1", "questions": ["q1?"]},
            ),
            (
                UsageTotals(input_tokens=50, output_tokens=5, total_cost_usd=0.02),
                {"mode": "question", "content": "c2", "questions": ["q2?"]},
            ),
            (
                UsageTotals(input_tokens=30, output_tokens=3, total_cost_usd=0.03),
                {"mode": "confirmation", "content": "c3", "refined_description": "final task"},
            ),
        ]
        calls = {"n": 0}

        def fake_round(*args, **kwargs):
            idx = calls["n"]
            calls["n"] += 1
            usage, result = rounds[idx]
            add_call_usage(usage)
            return result, f"raw {idx}"

        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))
            sm.register_handler(StepType.DISCOVERY, discovery_mod.discovery_handler)

            flow = sm.create_flow("multi-round discovery", task_type="discovery")
            step = flow.state.get_current_step()
            assert step.step_type == StepType.DISCOVERY
            step.inputs["task_description"] = "build something"

            with patch.object(discovery_mod, "_run_discovery_round", side_effect=fake_round), \
                 patch.object(discovery_mod, "_display_discovery_message"):
                # Round 1 (question) — PAUSED: carry holds round 1 only. The
                # finally block publishes token_usage for non-terminal runs too
                # (so step renderers can display it), so on a PAUSED round it
                # equals the carried total at that point, not absent.
                sm.run_step(flow, step)
                assert step.status == StepStatus.PAUSED
                assert step.outputs["token_usage"]["input_tokens"] == 100
                assert step.outputs["carried_token_usage"]["input_tokens"] == 100

                # Round 2 (question) — PAUSED: carry survives the clear and
                # accumulates round 1 + round 2; published token_usage tracks it.
                step.status = StepStatus.PENDING
                step.inputs["resumed"] = True
                step.inputs["user_response"] = "answer 1"
                sm.run_step(flow, step)
                assert step.status == StepStatus.PAUSED
                assert step.outputs["token_usage"]["input_tokens"] == 150
                assert step.outputs["carried_token_usage"]["input_tokens"] == 150

                # Round 3 (confirmation gate) — still PAUSED, carry now sums all
                # three rounds.
                step.status = StepStatus.PENDING
                step.inputs["user_response"] = "answer 2"
                sm.run_step(flow, step)
                assert step.status == StepStatus.PAUSED
                assert step.outputs.get("awaiting_programmatic_confirm") is True
                assert step.outputs["carried_token_usage"]["input_tokens"] == 180

            # Final round: user confirms via the programmatic gate (no LLM call).
            # The terminal token_usage must reflect the FULL cumulative total of
            # every round, and the carry is cleared.
            step.status = StepStatus.PENDING
            step.inputs["programmatic_confirmed"] = True
            sm.run_step(flow, step)
            assert step.status == StepStatus.COMPLETED
            assert "carried_token_usage" not in step.outputs
            tu = step.outputs["token_usage"]
            assert tu["input_tokens"] == 180  # 100 + 50 + 30
            assert tu["output_tokens"] == 18  # 10 + 5 + 3
            assert tu["total_cost_usd"] == pytest.approx(0.06)  # 0.01+0.02+0.03

            # The CLI authoritative session total folds every run independently
            # and must agree with the rolled-up terminal record.
            su = flow.state.session_token_usage
            assert su.input_tokens == 180
            assert su.total_cost_usd == pytest.approx(0.06)


class TestDiscoveryRoundUsageFooter:
    """Per-round CLI usage footer rendering and gating (G2 tasks 1-3).

    The discovery message block appends a compact dim ``This round … · Total …``
    footer (i18n-rendered; en-US under the engine conftest's pinned language)
    only when this round actually invoked the LLM (a non-empty round usage); an
    empty / ``None`` round usage (empty-input redraw, ``--resume`` re-display)
    must render no footer.
    """

    def _render_to_text(self, **kwargs) -> str:
        import io

        from rich.console import Console

        from . import display
        from .steps import discovery as discovery_mod

        buf = Console(file=io.StringIO(), width=200, record=True)
        prev = display.get_console()
        display.set_console(buf)
        try:
            discovery_mod._display_discovery_message("hello world", None, **kwargs)
        finally:
            display.set_console(prev)
        return buf.export_text()

    def test_footer_rendered_when_round_usage_non_empty(self):
        from .token_usage import UsageTotals

        out = self._render_to_text(
            round_usage=UsageTotals(input_tokens=1234, output_tokens=567),
            cumulative_usage=UsageTotals(input_tokens=12345, output_tokens=6789),
        )
        assert "This round 1,234 in / 567 out · Total 12,345 in / 6,789 out" in out

    def test_no_footer_when_round_usage_none(self):
        out = self._render_to_text()
        assert "This round" not in out
        assert "Total" not in out

    def test_no_footer_when_round_usage_empty(self):
        from .token_usage import UsageTotals

        out = self._render_to_text(
            round_usage=UsageTotals(),
            cumulative_usage=UsageTotals(input_tokens=100, output_tokens=10),
        )
        # An empty round increment (no LLM call this round) suppresses the footer
        # even though a cumulative total exists.
        assert "This round" not in out
        assert "Total" not in out

    def test_handler_passes_round_and_cumulative_usage(self):
        """discovery_handler computes round=current_step_usage(), cumulative=carried+round."""
        from .state_machine import StateMachine
        from .steps import discovery as discovery_mod
        from .token_usage import UsageTotals, add_call_usage

        captured = {}

        def fake_display(*args, **kwargs):
            captured["round_usage"] = kwargs.get("round_usage")
            captured["cumulative_usage"] = kwargs.get("cumulative_usage")

        def fake_round(*args, **kwargs):
            add_call_usage(UsageTotals(input_tokens=30, output_tokens=3))
            return (
                {"mode": "question", "content": "c2", "questions": ["q2?"]},
                "raw 2",
            )

        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))
            sm.register_handler(StepType.DISCOVERY, discovery_mod.discovery_handler)

            flow = sm.create_flow("usage footer discovery", task_type="discovery")
            step = flow.state.get_current_step()
            step.inputs["task_description"] = "build something"
            # Simulate a prior round's carried cumulative total surviving the
            # outputs.clear() inside the handler.
            step.outputs["carried_token_usage"] = UsageTotals(
                input_tokens=100, output_tokens=10
            ).to_dict()
            step.inputs["resumed"] = True
            step.inputs["user_response"] = "answer 1"

            with patch.object(discovery_mod, "_run_discovery_round", side_effect=fake_round), \
                 patch.object(discovery_mod, "_display_discovery_message", side_effect=fake_display):
                sm.run_step(flow, step)

        assert step.status == StepStatus.PAUSED
        # This round's increment only.
        assert captured["round_usage"].input_tokens == 30
        assert captured["round_usage"].output_tokens == 3
        # Carried prior total (100/10) + this round (30/3).
        assert captured["cumulative_usage"].input_tokens == 130
        assert captured["cumulative_usage"].output_tokens == 13

    def test_programmatic_confirm_path_renders_no_footer(self):
        """The programmatic-confirmed early return makes no LLM call and no display."""
        from .state_machine import StateMachine
        from .steps import discovery as discovery_mod

        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))
            sm.register_handler(StepType.DISCOVERY, discovery_mod.discovery_handler)

            flow = sm.create_flow("confirm discovery", task_type="discovery")
            step = flow.state.get_current_step()
            step.inputs["task_description"] = "build something"
            step.inputs["programmatic_confirmed"] = True

            with patch.object(discovery_mod, "_display_discovery_message") as mock_display:
                sm.run_step(flow, step)

            assert step.status == StepStatus.COMPLETED
            # No footer / message rendered on the confirm fast-path.
            mock_display.assert_not_called()


class TestCliSinkUsageRendering:
    """CliSink per-step usage routing for the interactive/special step types (G3).

    discovery → a compact dim cumulative line (its per-round footer is rendered
    inline by the discovery handler; the terminal event surfaces the
    whole-discovery total), never the big usage block or full report; confirm →
    compact dim single-line footer from ``step.outputs['token_usage']`` (only
    when the LLM was actually called); plan → the unchanged big ``Step Token
    Usage`` block; other types → ``render_step_output`` unchanged.
    """

    def _completed_event(self, step):
        from .event_stream import EventType, new_event

        return new_event(
            EventType.STEP_COMPLETED,
            step_type=step.step_type.value,
            step=step,
        )

    def test_confirm_renders_compact_dim_footer(self):
        from rich.console import Console

        from .sink import CliSink

        step = Step(step_type=StepType.CONFIRM)
        step.outputs["token_usage"] = {"input_tokens": 1234, "output_tokens": 567}

        console = Console(record=True, width=200)
        sink = CliSink(console=console)
        with patch("tianluo.engine.step_renderers.render_step_usage") as big_block:
            sink.consume(self._completed_event(step))

        out = console.export_text()
        # Compact footer, round == cumulative (single LLM review per confirm step).
        assert "This round 1,234 in / 567 out · Total 1,234 in / 567 out" in out
        # NOT the big reverse-color per-step block.
        assert "Step Token Usage" not in out
        big_block.assert_not_called()

    def test_confirm_human_mode_no_usage_renders_nothing(self):
        from rich.console import Console

        from .sink import CliSink

        # Human-reviewer confirm makes no LLM call -> no token_usage in outputs.
        step = Step(step_type=StepType.CONFIRM)

        console = Console(record=True, width=200)
        sink = CliSink(console=console)
        sink.consume(self._completed_event(step))

        assert console.export_text().strip() == ""

    def test_confirm_empty_usage_renders_nothing(self):
        from rich.console import Console

        from .sink import CliSink

        step = Step(step_type=StepType.CONFIRM)
        step.outputs["token_usage"] = {"input_tokens": 0, "output_tokens": 0}

        console = Console(record=True, width=200)
        sink = CliSink(console=console)
        sink.consume(self._completed_event(step))

        assert console.export_text().strip() == ""

    def test_discovery_renders_cumulative_usage(self):
        from rich.console import Console

        from .sink import CliSink

        step = Step(step_type=StepType.DISCOVERY)
        step.outputs["token_usage"] = {"input_tokens": 999, "output_tokens": 111}

        console = Console(record=True, width=200)
        sink = CliSink(console=console)
        with patch("tianluo.engine.step_renderers.render_step_usage") as big_block, \
             patch("tianluo.engine.step_renderers.render_step_output") as full_report:
            sink.consume(self._completed_event(step))

        # Discovery's terminal event renders ONLY a compact dim cumulative line
        # (the whole-discovery total, including the confirmation round that
        # issues no LLM call) — never the big per-step usage block nor the full
        # report (both owned by the orchestrator's interactive path).
        out = console.export_text()
        assert "Discovery cumulative:" in out
        assert "999" in out and "111" in out
        big_block.assert_not_called()
        full_report.assert_not_called()

    def test_plan_keeps_big_usage_block(self):
        from .sink import CliSink

        step = Step(step_type=StepType.PLAN)
        step.outputs["token_usage"] = {"input_tokens": 1000, "output_tokens": 200}

        sink = CliSink()
        with patch("tianluo.engine.step_renderers.render_step_usage") as big_block, \
             patch("tianluo.engine.step_renderers.render_step_output") as full_report:
            sink.consume(self._completed_event(step))

        # plan retains the established big per-step usage block and is still
        # skipped from the full report (orchestrator owns the plan presentation).
        big_block.assert_called_once_with(step)
        full_report.assert_not_called()

    def test_non_interactive_step_uses_full_report(self):
        from .sink import CliSink

        step = Step(step_type=StepType.ANALYZE)
        step.outputs["token_usage"] = {"input_tokens": 10, "output_tokens": 20}

        sink = CliSink()
        with patch("tianluo.engine.step_renderers.render_step_output") as full_report:
            sink.consume(self._completed_event(step))

        # Non-skipped types go through the full report renderer (which itself
        # appends the big usage block) — unchanged behaviour.
        full_report.assert_called_once_with(step)


class TestDiscoveryIssueOperations:
    """discovery_handler executes scoped, user-directed issue_operations (G3).

    A round whose LLM result carries ``issue_operations`` is run through
    ``apply_discovery_issue_operations``: create issues are filed with
    ``source="human"`` and their IDs recorded in
    ``step.inputs['discovery_created_issue_ids']`` (which persists across rounds
    and ``--resume``); cross-round update/delete are honored only for IDs in
    that tracking set and rejected for out-of-scope IDs; a round without
    ``issue_operations`` behaves exactly as before; and the
    ``programmatic_confirmed`` early-return triggers no issue operations.
    """

    def _make_flow(self, tmpdir):
        from .state_machine import StateMachine
        from .steps import discovery as discovery_mod

        sm = StateMachine(Path(tmpdir))
        sm.register_handler(StepType.DISCOVERY, discovery_mod.discovery_handler)
        flow = sm.create_flow("discover issues", task_type="discovery")
        # discovery_handler derives project_root from change_path.parent; point
        # it at tmpdir so the IssueManager writes under tmpdir/tianluo/issues/.
        flow.change_path = Path(tmpdir) / "change"
        step = flow.state.get_current_step()
        assert step.step_type == StepType.DISCOVERY
        step.inputs["task_description"] = "do a thing"
        return sm, flow, step

    def _round_with(self, result):
        def fake_round(*args, **kwargs):
            return result, "raw"
        return fake_round

    def test_create_op_files_issue_and_tracks_id(self):
        from .issue_manager import IssueManager
        from .steps import discovery as discovery_mod

        result = {
            "mode": "question",
            "content": "filed it",
            "questions": ["anything else?"],
            "issue_operations": [
                {"action": "create", "title": "Split out X", "description": "details"}
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            sm, flow, step = self._make_flow(tmpdir)
            with patch.object(discovery_mod, "_run_discovery_round",
                              side_effect=self._round_with(result)), \
                 patch.object(discovery_mod, "_display_discovery_message"):
                sm.run_step(flow, step)

            assert step.status == StepStatus.PAUSED
            tracked = step.inputs.get("discovery_created_issue_ids")
            assert tracked and len(tracked) == 1
            new_id = tracked[0]

            mgr = IssueManager(Path(tmpdir))
            issue = mgr.load(new_id)
            assert issue is not None
            assert issue.source == "human"
            assert issue.display_title == "Split out X"

    def test_cross_round_update_and_delete_scope(self):
        from .issue_manager import IssueManager
        from .steps import discovery as discovery_mod

        with tempfile.TemporaryDirectory() as tmpdir:
            sm, flow, step = self._make_flow(tmpdir)
            mgr = IssueManager(Path(tmpdir))

            # Round 1: create.
            create_result = {
                "mode": "question",
                "content": "created",
                "questions": ["q?"],
                "issue_operations": [
                    {"action": "create", "title": "Orig", "description": "d"}
                ],
            }
            with patch.object(discovery_mod, "_run_discovery_round",
                              side_effect=self._round_with(create_result)), \
                 patch.object(discovery_mod, "_display_discovery_message"):
                sm.run_step(flow, step)
            tracked_id = step.inputs["discovery_created_issue_ids"][0]

            # A pre-existing issue NOT created by this discovery step (out of scope).
            foreign = mgr.create(description="foreign issue", title="Foreign")

            # Round 2: in-scope update succeeds; out-of-scope update rejected.
            update_result = {
                "mode": "question",
                "content": "updated",
                "questions": ["q?"],
                "issue_operations": [
                    {"action": "update", "id": tracked_id, "title": "Renamed"},
                    {"action": "update", "id": foreign.id, "title": "Hijacked"},
                ],
            }
            step.status = StepStatus.PENDING
            step.inputs["resumed"] = True
            step.inputs["user_response"] = "update them"
            with patch.object(discovery_mod, "_run_discovery_round",
                              side_effect=self._round_with(update_result)), \
                 patch.object(discovery_mod, "_display_discovery_message"):
                sm.run_step(flow, step)

            assert mgr.load(tracked_id).display_title == "Renamed"
            # The out-of-scope issue was not touched.
            assert mgr.load(foreign.id).display_title == "Foreign"

            # Round 3: in-scope delete removes it and drops it from tracking;
            # out-of-scope delete rejected (foreign issue survives).
            delete_result = {
                "mode": "question",
                "content": "deleted",
                "questions": ["q?"],
                "issue_operations": [
                    {"action": "delete", "id": tracked_id},
                    {"action": "delete", "id": foreign.id},
                ],
            }
            step.status = StepStatus.PENDING
            step.inputs["user_response"] = "delete them"
            with patch.object(discovery_mod, "_run_discovery_round",
                              side_effect=self._round_with(delete_result)), \
                 patch.object(discovery_mod, "_display_discovery_message"):
                sm.run_step(flow, step)

            assert mgr.load(tracked_id) is None
            assert tracked_id not in step.inputs["discovery_created_issue_ids"]
            # Out-of-scope delete was rejected — the foreign issue still exists.
            assert mgr.load(foreign.id) is not None

    def test_no_issue_operations_is_unchanged(self):
        from .issue_manager import IssueManager
        from .steps import discovery as discovery_mod

        result = {
            "mode": "question",
            "content": "just a question",
            "questions": ["what scope?"],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            sm, flow, step = self._make_flow(tmpdir)
            with patch.object(discovery_mod, "_run_discovery_round",
                              side_effect=self._round_with(result)), \
                 patch.object(discovery_mod, "_display_discovery_message"):
                sm.run_step(flow, step)

            assert step.status == StepStatus.PAUSED
            assert step.outputs.get("questions") == ["what scope?"]
            # No tracking key is created when no issue_operations were emitted.
            assert "discovery_created_issue_ids" not in step.inputs
            mgr = IssueManager(Path(tmpdir))
            assert mgr.list_issues() == []

    def test_created_id_surfaced_into_conversation_history(self):
        """The engine-assigned ID is fed back into the LLM context so a later
        'delete the one I just created' turn can resolve to a concrete id."""
        from .steps import discovery as discovery_mod

        result = {
            "mode": "question",
            "content": "filed it",
            "questions": ["anything else?"],
            "issue_operations": [
                {"action": "create", "title": "Split out X", "description": "d"}
            ],
        }
        with tempfile.TemporaryDirectory() as tmpdir:
            sm, flow, step = self._make_flow(tmpdir)
            with patch.object(discovery_mod, "_run_discovery_round",
                              side_effect=self._round_with(result)), \
                 patch.object(discovery_mod, "_display_discovery_message"):
                sm.run_step(flow, step)

            new_id = step.inputs["discovery_created_issue_ids"][0]
            history = step.inputs["discovery_state"]["history"]
            engine_notes = [e for e in history if e.get("role") == "system"]
            assert len(engine_notes) == 1
            note = engine_notes[0]["content"]
            # The concrete assigned id and the tracked-scope list are surfaced.
            assert new_id in note
            assert "create issue" in note
            # The formatted history that feeds the next prompt includes it.
            rendered = discovery_mod._format_conversation_history(history)
            assert new_id in rendered

    def test_update_drops_non_list_tags(self):
        """A scalar tags value on an update op must not corrupt the issue."""
        from .issue_manager import IssueManager
        from .steps import discovery as discovery_mod

        with tempfile.TemporaryDirectory() as tmpdir:
            sm, flow, step = self._make_flow(tmpdir)
            mgr = IssueManager(Path(tmpdir))

            create_result = {
                "mode": "question",
                "content": "created",
                "questions": ["q?"],
                "issue_operations": [
                    {"action": "create", "title": "Orig", "description": "d",
                     "tags": ["keep"]}
                ],
            }
            with patch.object(discovery_mod, "_run_discovery_round",
                              side_effect=self._round_with(create_result)), \
                 patch.object(discovery_mod, "_display_discovery_message"):
                sm.run_step(flow, step)
            tracked_id = step.inputs["discovery_created_issue_ids"][0]

            update_result = {
                "mode": "question",
                "content": "updated",
                "questions": ["q?"],
                "issue_operations": [
                    {"action": "update", "id": tracked_id, "tags": "urgent"},
                ],
            }
            step.status = StepStatus.PENDING
            step.inputs["resumed"] = True
            step.inputs["user_response"] = "tag it"
            with patch.object(discovery_mod, "_run_discovery_round",
                              side_effect=self._round_with(update_result)), \
                 patch.object(discovery_mod, "_display_discovery_message"):
                sm.run_step(flow, step)

            issue = mgr.load(tracked_id)
            # The scalar tags was dropped (not stored as the string "urgent"),
            # leaving the prior list intact rather than a char-iterable string.
            assert isinstance(issue.tags, list)
            assert issue.tags == ["keep"]

    def test_programmatic_confirmed_triggers_no_ops(self):
        from .issue_manager import IssueManager
        from .steps import discovery as discovery_mod

        with tempfile.TemporaryDirectory() as tmpdir:
            sm, flow, step = self._make_flow(tmpdir)
            step.inputs["programmatic_confirmed"] = True
            # Even if a stale issue_operations payload existed, the early return
            # never runs _run_discovery_round, so nothing is executed.
            with patch.object(discovery_mod, "_run_discovery_round") as mock_round, \
                 patch.object(discovery_mod, "_display_discovery_message"):
                sm.run_step(flow, step)

            assert step.status == StepStatus.COMPLETED
            mock_round.assert_not_called()
            assert "discovery_created_issue_ids" not in step.inputs
            mgr = IssueManager(Path(tmpdir))
            assert mgr.list_issues() == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
