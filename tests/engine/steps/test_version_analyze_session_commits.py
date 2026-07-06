"""Tests for version_analyze prompt rendering of session-introduced commits
and pre-session version baseline (G4 of bugfix for double-bump defect).

Also contains the end-to-end replay regression for the 20260512-225655
double-bump scenario (G5 task 15): pre_session_version=5.1.0 is forwarded
from implement.outputs → version_analyze.inputs by state_machine, the
mocked LLM returns suggested_version=5.2.0, and commit step writes 5.2.0
(not 5.2.1) to disk.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from se3.engine.models import (
    FlowInstance,
    State,
    Step,
    StepStatus,
    StepType,
)
from se3.engine.state_machine import StateMachine
from se3.engine.steps.commit import commit_handler
from se3.engine.steps.version_analyze import (
    _format_session_commits,
    version_analyze_handler,
)
from se3.engine.version_bumper import VersionBumper


def _make_flow(**kwargs) -> FlowInstance:
    defaults = {
        "flow_id": "test-flow-va-sc",
        "task_description": "Fix something small",
        "task_type": "bugfix",
        "change_path": Path("/tmp/project/se3.yaml"),
        # Mirror the real FlowInstance default so a MagicMock(spec=…) flow does
        # not read is_worktree_mode as a truthy MagicMock and divert the handler
        # into the worktree intent-only branch. See test_version_analyze.py.
        "is_worktree_mode": False,
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
    step.step_type = StepType.VERSION_ANALYZE
    step.step_id = "va-sc-001"
    return step


def _llm_response_json(**overrides) -> str:
    payload = {
        "bump_type": "patch",
        "reasoning": "Fix-only changes",
        "confidence": "high",
        "suggested_version": "5.1.1",
        "commit_message": "Fix double version bump in worktree flow",
    }
    payload.update(overrides)
    return json.dumps(payload)


class TestFormatSessionCommitsHelper:
    """Direct unit tests for _format_session_commits."""

    def test_empty_list_renders_explanatory_text(self):
        text = _format_session_commits([])
        assert "implement 阶段未在主分支留下任何 commit" in text

    def test_none_renders_explanatory_text(self):
        text = _format_session_commits(None)
        assert "implement 阶段未在主分支留下任何 commit" in text

    def test_non_empty_renders_sha_and_subject(self):
        commits = [
            {
                "sha": "abcdef1234567890",
                "subject": "bump version to 5.2.0",
                "files": ["pyproject.toml", "VERSIONS.md"],
            },
            {
                "sha": "1122334455667788",
                "subject": "fix typo in helper",
                "files": ["src/se3/foo.py"],
            },
        ]
        text = _format_session_commits(commits)
        assert "abcdef12" in text
        assert "11223344" in text
        assert "bump version to 5.2.0" in text
        assert "fix typo in helper" in text
        assert "pyproject.toml" in text

    def test_renders_at_most_50_commits(self):
        commits = [
            {"sha": f"{i:040x}", "subject": f"commit {i}", "files": []}
            for i in range(60)
        ]
        text = _format_session_commits(commits)
        assert "还有 10 个未展示" in text
        # First commit appears, 50th commit appears, 51st does not.
        assert "commit 0" in text
        assert "commit 49" in text
        assert "commit 50" not in text

    def test_folds_long_file_lists(self):
        files = [f"file_{i}.py" for i in range(15)]
        commits = [{"sha": "deadbeefcafebabe", "subject": "many files", "files": files}]
        text = _format_session_commits(commits)
        assert "file_0.py" in text
        assert "file_9.py" in text
        assert "还有 5 个文件未展示" in text


class TestPromptIncludesSessionCommitsAndPreSession:
    """version_analyze_handler renders new fields into the prompt."""

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.steps.version_analyze._get_current_version", return_value="5.2.0")
    @patch("se3.engine.steps.version_analyze.LLMCaller")
    def test_prompt_includes_pre_session_version_and_commit_list(
        self, mock_caller_cls, mock_ver, mock_inject
    ):
        mock_caller = MagicMock()
        mock_caller.call.return_value = _llm_response_json()
        mock_caller_cls.return_value = mock_caller

        flow = _make_flow()
        step = _make_step(
            {
                "task_description": "Fix small bug",
                "pre_session_version": "5.1.0",
                "session_commits": [
                    {
                        "sha": "aaaaaaaa11112222",
                        "subject": "bump version to 5.2.0",
                        "files": ["pyproject.toml", "VERSIONS.md"],
                    },
                    {
                        "sha": "bbbbbbbb33334444",
                        "subject": "implement group G2 feature",
                        "files": ["src/se3/foo.py"],
                    },
                ],
            }
        )

        result = version_analyze_handler(step, flow)
        assert result == StepStatus.COMPLETED

        # Inspect prompt that was passed to LLMCaller.call
        call_kwargs = mock_caller.call.call_args.kwargs
        prompt = call_kwargs.get("prompt") or mock_caller.call.call_args.args[0]
        assert "Pre-Session Version" in prompt
        assert "5.1.0" in prompt
        assert "Session-Introduced Commits" in prompt
        assert "aaaaaaaa" in prompt
        assert "bbbbbbbb" in prompt
        assert "bump version to 5.2.0" in prompt
        assert "implement group G2 feature" in prompt
        # Fixed instruction about treating commits as not having happened.
        assert "视为未发生" in prompt
        # Disk-version is still surfaced for cross-reference.
        assert "5.2.0" in prompt

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.steps.version_analyze._get_current_version", return_value="5.1.0")
    @patch("se3.engine.steps.version_analyze.LLMCaller")
    def test_prompt_handles_empty_session_commits(
        self, mock_caller_cls, mock_ver, mock_inject
    ):
        mock_caller = MagicMock()
        mock_caller.call.return_value = _llm_response_json(
            suggested_version="5.1.1"
        )
        mock_caller_cls.return_value = mock_caller

        flow = _make_flow()
        step = _make_step(
            {
                "task_description": "Tiny doc fix",
                "pre_session_version": "5.1.0",
                "session_commits": [],
            }
        )

        result = version_analyze_handler(step, flow)
        assert result == StepStatus.COMPLETED

        call_kwargs = mock_caller.call.call_args.kwargs
        prompt = call_kwargs.get("prompt") or mock_caller.call.call_args.args[0]
        assert "Session-Introduced Commits" in prompt
        assert "implement 阶段未在主分支留下任何 commit" in prompt
        assert "Pre-Session Version" in prompt
        assert "5.1.0" in prompt

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.steps.version_analyze._get_current_version", return_value="5.1.0")
    @patch("se3.engine.steps.version_analyze.LLMCaller")
    def test_pre_session_version_fallback_to_current_version(
        self, mock_caller_cls, mock_ver, mock_inject, caplog
    ):
        """When pre_session_version is absent, fall back to disk-read current_version."""
        mock_caller = MagicMock()
        mock_caller.call.return_value = _llm_response_json(
            suggested_version="5.1.1"
        )
        mock_caller_cls.return_value = mock_caller

        flow = _make_flow()
        # No pre_session_version, no session_commits in inputs.
        step = _make_step({"task_description": "Tiny doc fix"})

        import logging

        with caplog.at_level(logging.WARNING):
            result = version_analyze_handler(step, flow)
        assert result == StepStatus.COMPLETED

        call_kwargs = mock_caller.call.call_args.kwargs
        prompt = call_kwargs.get("prompt") or mock_caller.call.call_args.args[0]
        # pre_session_version slot is filled with the disk version as fallback.
        assert "Pre-Session Version" in prompt
        assert "5.1.0" in prompt
        # Empty list renders the explanatory line, not a crash.
        assert "implement 阶段未在主分支留下任何 commit" in prompt
        # Warning was logged about fallback.
        assert any(
            "pre_session_version missing" in rec.getMessage()
            for rec in caplog.records
        )


class TestEndToEndDoubleBumpReplay:
    """End-to-end regression for the 20260512-225655 double-bump scenario.

    The cause was: implement (using a worktree) merged a 5.1.0 → 5.2.0
    bump commit back to main, leaving disk at 5.2.0; then version_analyze
    naively read disk and produced suggested_version=5.2.1 (patch bump on
    top of the already-bumped value); then commit step wrote 5.2.1 — a
    second, unintended bump.

    The fix routes pre_session_version=5.1.0 + the session_commits list
    from implement.outputs through state_machine._build_step_inputs() into
    version_analyze.inputs. With those, the LLM is instructed to treat
    those commits as not-having-happened and use 5.1.0 as the baseline, so
    suggested_version=5.2.0. Commit step then writes 5.2.0 to disk.

    This test wires the data flow with a real StateMachine, a real
    FlowInstance/State, and mocks only (a) the LLM call and (b) the
    git/VersionBumper side-effects. It asserts that the value the commit
    step writes is exactly the suggested_version, which itself was driven
    by the pre_session_version baseline.
    """

    def _make_implement_step(self) -> Step:
        step = Step(
            step_type=StepType.IMPLEMENT,
            status=StepStatus.COMPLETED,
            step_id="01_implement_aaaaaaaa",
        )
        step.outputs = {
            "files_changed": ["src/se3/foo.py"],
            "implemented_groups": ["G1", "G2"],
            "summary": "Implement group G2 plus an inadvertent version bump.",
            "completion_status": "complete",
            "incomplete_tasks": [],
            "restricted_edits_applied": [],
            "restricted_edits_failed": [],
            "tests_added": [],
            "test_mapping": {},
            "estimated_test_duration": 30,
            "pre_session_version": "5.1.0",
            "session_commits": [
                {
                    "sha": "aaaaaaaa11112222",
                    "subject": "bump version to 5.2.0",
                    "files": ["pyproject.toml", "VERSIONS.md"],
                },
                {
                    "sha": "bbbbbbbb33334444",
                    "subject": "implement group G2 feature",
                    "files": ["src/se3/foo.py"],
                },
            ],
        }
        return step

    def _make_flow_with_implement(self, tmp_path: Path) -> FlowInstance:
        flow = FlowInstance(
            flow_id="e2e-double-bump-replay",
            task_description="Fix small thing in foo",
            task_type="bugfix",
            state=State(),
            change_path=tmp_path / "se3.yaml",
        )
        flow.state.selected_steps = [
            StepType.IMPLEMENT,
            StepType.VERSION_ANALYZE,
            StepType.COMMIT,
        ]
        impl = self._make_implement_step()
        flow.state.add_step(impl)
        return flow

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.steps.version_analyze._get_current_version", return_value="5.2.0")
    @patch("se3.engine.steps.version_analyze.LLMCaller")
    def test_e2e_replay_writes_pre_session_baseline_not_disk_baseline(
        self,
        mock_llm_caller_cls,
        mock_disk_version,
        mock_inject,
        tmp_path,
    ):
        # --- Arrange ----------------------------------------------------------------
        flow = self._make_flow_with_implement(tmp_path)
        sm = StateMachine(project_root=tmp_path)

        # Step 1: state_machine builds version_analyze inputs from implement.outputs.
        va_inputs = sm._build_step_inputs(flow, StepType.VERSION_ANALYZE)

        # Sanity: the fix's data-forwarding hook is in place.
        assert va_inputs["pre_session_version"] == "5.1.0"
        assert len(va_inputs["session_commits"]) == 2
        assert va_inputs["session_commits"][0]["subject"] == "bump version to 5.2.0"

        # Set up the LLM mock — emulates an LLM that correctly used
        # pre_session_version=5.1.0 (NOT disk=5.2.0) as the baseline, so it
        # returns 5.2.0 (a minor bump from 5.1.0) rather than 5.2.1 (a patch
        # bump from disk 5.2.0).
        llm_response = json.dumps(
            {
                "bump_type": "minor",
                "reasoning": (
                    "Pre-Session Version is 5.1.0; ignoring the inadvertent "
                    "5.1.0→5.2.0 bump commit, the net change is a minor bump."
                ),
                "confidence": "high",
                "suggested_version": "5.2.0",
                "commit_message": "Bump to 5.2.0 (replay)",
            }
        )
        mock_llm = MagicMock()
        mock_llm.call.return_value = llm_response
        mock_llm_caller_cls.return_value = mock_llm

        va_step = Step(
            step_type=StepType.VERSION_ANALYZE,
            status=StepStatus.PENDING,
            step_id="02_version_analyze_bbbbbbbb",
        )
        va_step.inputs = va_inputs
        flow.state.add_step(va_step)

        # --- Act 1: run version_analyze ---------------------------------------------
        va_result = version_analyze_handler(va_step, flow)
        assert va_result == StepStatus.COMPLETED, va_step.error_message
        # Mark as completed for the state machine walk that follows.
        va_step.status = StepStatus.COMPLETED

        # Crucial: the LLM prompt should have surfaced Pre-Session Version 5.1.0
        # AND the bump commit, so the test asserts the data-flow guarantee.
        prompt = mock_llm.call.call_args.kwargs.get("prompt") or mock_llm.call.call_args.args[0]
        assert "Pre-Session Version" in prompt
        assert "5.1.0" in prompt
        assert "bump version to 5.2.0" in prompt
        assert "视为未发生" in prompt

        # The authoritative version field flows forward.
        assert va_step.outputs["suggested_version"] == "5.2.0"

        # --- Act 2: build commit inputs and run commit -----------------------------
        commit_inputs = sm._build_step_inputs(flow, StepType.COMMIT)
        # version_analyze → commit pipe.
        assert commit_inputs["suggested_version"] == "5.2.0"

        commit_step = Step(
            step_type=StepType.COMMIT,
            status=StepStatus.PENDING,
            step_id="03_commit_cccccccc",
        )
        commit_step.inputs = commit_inputs
        flow.state.add_step(commit_step)

        # Mock VersionBumper + git so we can observe what would be written
        # without touching real filesystem state. set_version's argument is
        # the assertion: it is the value the commit step picked up from
        # version_analyze, which itself was derived from pre_session_version.
        version_file = tmp_path / "pyproject.toml"

        mock_bumper = MagicMock(spec=VersionBumper)
        mock_bumper.detect_version_file.return_value = version_file
        mock_bumper._use_script_mode = False
        mock_bumper._script_runner = None
        mock_bumper.read_version.return_value = "5.2.0"  # disk already at 5.2.0
        mock_bumper.set_version.return_value = "5.2.0"

        with patch("se3.engine.steps.commit._has_changes", return_value=True), \
             patch("se3.engine.steps.commit._load_version_config") as mock_load_cfg, \
             patch("se3.engine.steps.commit._get_commit_hash", return_value="ffffffff"), \
             patch("se3.engine.steps.commit.subprocess") as mock_subproc, \
             patch("se3.engine.steps.commit.VersionBumper", return_value=mock_bumper):
            cfg = MagicMock()
            cfg.enabled = True
            cfg.include_in_commit_message = True
            mock_load_cfg.return_value = cfg
            mock_subproc.run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            commit_result = commit_handler(commit_step, flow)

        # --- Assert -----------------------------------------------------------------
        assert commit_result == StepStatus.COMPLETED, commit_step.error_message
        # The disk-written version is 5.2.0, NOT the naive 5.2.1 patch bump
        # that the pre-fix bug produced.
        mock_bumper.set_version.assert_called_once_with(
            version="5.2.0",
            path=version_file,
        )
        # Defensive: prove the bug-state did not slip through.
        write_calls = [c.kwargs.get("version") for c in mock_bumper.set_version.call_args_list]
        assert "5.2.1" not in write_calls
        assert commit_step.outputs.get("version") == "5.2.0"
