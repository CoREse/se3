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


class TestWorktreeIntentPersistenceIsMandatory:
    """A worktree flow must FAIL if its version intent cannot be persisted.

    The merge-side ``version_reconcile`` derives the final version SOLELY from
    the committed intent. If ``write_intent`` fails and the step still reported
    COMPLETED, the branch would land with no intent, reconcile would treat the
    session as contributing no bump, and the feature would merge with no version
    bump or changelog — silently. So a persist failure must surface as a FAILED
    (resumable) step, not a best-effort warning.
    """

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.steps.version_analyze._get_current_version", return_value="5.1.0")
    @patch("se3.engine.steps.version_analyze.LLMCaller")
    def test_write_intent_oserror_fails_the_step(
        self, mock_caller_cls, mock_ver, mock_inject
    ):
        mock_caller = MagicMock()
        mock_caller.call.return_value = _llm_response_json(
            bump_type="minor", suggested_version="5.2.0"
        )
        mock_caller_cls.return_value = mock_caller

        flow = _make_flow(is_worktree_mode=True)
        step = _make_step(
            {"task_description": "Add a feature", "pre_session_version": "5.1.0"}
        )

        # write_intent is imported inside _emit_version_intent as
        # ``from ..version_intent import ... write_intent``; patch it at the
        # source module so the local import binds to the failing stub.
        with patch(
            "se3.engine.version_intent.write_intent",
            side_effect=OSError("read-only filesystem"),
        ):
            result = version_analyze_handler(step, flow)

        assert result == StepStatus.FAILED
        assert step.error_message
        assert "version-intents" in step.error_message
        # No authoritative version leaks into outputs on the worktree path.
        assert "suggested_version" not in step.outputs

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.steps.version_analyze._get_current_version", return_value="5.1.0")
    @patch("se3.engine.steps.version_analyze.LLMCaller")
    def test_successful_persist_still_completes(
        self, mock_caller_cls, mock_ver, mock_inject, tmp_path
    ):
        mock_caller = MagicMock()
        mock_caller.call.return_value = _llm_response_json(
            bump_type="minor", suggested_version="5.2.0"
        )
        mock_caller_cls.return_value = mock_caller

        # project_root is derived from flow.change_path.parent; point it at the
        # tmp dir so the intent lands somewhere writable and inspectable.
        flow = _make_flow(
            is_worktree_mode=True,
            flow_id="flow-persist-ok",
            change_path=tmp_path / "se3.yaml",
        )
        step = _make_step(
            {"task_description": "Add a feature", "pre_session_version": "5.1.0"}
        )

        result = version_analyze_handler(step, flow)

        assert result == StepStatus.COMPLETED
        # The intent file was actually written under the project root.
        assert (tmp_path / "se3" / "version-intents" / "flow-persist-ok.json").exists()
        assert step.outputs["version_intent"]["bump_type"] == "minor"

    @patch("se3.engine.context_builder.get_issue_discovery_injection", return_value="")
    @patch("se3.engine.steps.version_analyze._version_bumping_enabled", return_value=False)
    @patch("se3.engine.steps.version_analyze._get_current_version", return_value="5.1.0")
    @patch("se3.engine.steps.version_analyze.LLMCaller")
    def test_version_disabled_worktree_emits_no_intent(
        self, mock_caller_cls, mock_ver, mock_enabled, mock_inject, tmp_path
    ):
        """version.enabled=false in worktree mode must NOT emit an intent (nor
        write one to disk): emitting one would make the merge-side reconcile bump
        a version the project deliberately does not manage. The step still
        COMPLETEs — the merge lands with no automatic bump, exactly as a
        non-worktree disabled flow does."""
        mock_caller = MagicMock()
        mock_caller.call.return_value = _llm_response_json(
            bump_type="minor", suggested_version="5.2.0"
        )
        mock_caller_cls.return_value = mock_caller

        flow = _make_flow(
            is_worktree_mode=True,
            flow_id="flow-disabled",
            change_path=tmp_path / "se3.yaml",
        )
        step = _make_step(
            {"task_description": "Add a feature", "pre_session_version": "5.1.0"}
        )

        # write_intent must never be reached when bumping is disabled.
        with patch(
            "se3.engine.version_intent.write_intent",
            side_effect=AssertionError("write_intent must not run when disabled"),
        ):
            result = version_analyze_handler(step, flow)

        assert result == StepStatus.COMPLETED
        assert "version_intent" not in step.outputs
        assert "suggested_version" not in step.outputs
        # Nothing was written to the intents directory.
        assert not (tmp_path / "se3" / "version-intents").exists()


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

        # The session's own "bump version to 5.2.0" commit put 5.2.0 on disk, so
        # in a real run its Flow+Version trailer makes ``_flow_wrote_version``
        # report True — the disk version is THIS flow's own work, not a concurrent
        # bump. The guard then keeps the resolved target as-is (no re-analysis).
        # We stub it True here because subprocess is mocked away (no real git
        # history to grep); without this the guard would treat the flow's own
        # in-session bump as indistinguishable-from-concurrent drift and, on a
        # re-analysis that returns the same 5.2.0, halt to avoid a collision.
        # ``create_annotated_version_tag`` shells out to git through its own
        # module, so mocking ``commit.subprocess`` does not reach it and it
        # would hit the non-repo tmp_path. A minor bump makes version_analyze
        # emit is_tag=True, so commit does attempt the tag; stub it out because
        # tagging is not what this replay test is asserting.
        with patch("se3.engine.steps.commit._has_changes", return_value=True), \
             patch("se3.engine.steps.commit._load_version_config") as mock_load_cfg, \
             patch("se3.engine.steps.commit._read_head_commit", return_value=("ffffffff", "")), \
             patch("se3.engine.steps.commit._flow_wrote_version", return_value=True), \
             patch("se3.engine.steps.commit.create_annotated_version_tag", return_value="v5.2.0"), \
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


class TestGuardVersionRaceOwnReplay:
    """Fix (iteration 4): the non-worktree race guard must not treat the flow's
    OWN already-committed version as concurrent drift.

    The pre-fix guard declared drift on any disk version above the pre-session
    baseline and re-ran version_analyze against it. On a replay/resume over the
    flow's own prior commit (disk already at the version this flow wrote), a
    baseline-sensitive LLM would then bump AGAIN (5.2.0 → 5.2.1/5.3.0). The fix
    detects the flow's own prior write via the git-durable Flow+Version trailer
    and keeps the already-resolved target instead of re-analysing.
    """

    def _guard_flow(self, tmp_path):
        flow = _make_flow(change_path=tmp_path / "se3.yaml", flow_id="flow-own")
        flow.state = State()
        return flow

    def test_own_prior_write_is_not_treated_as_drift(self, tmp_path):
        from se3.engine.steps import commit as commit_mod

        flow = self._guard_flow(tmp_path)
        step = Step(step_type=StepType.COMMIT, status=StepStatus.PENDING)
        step.inputs = {"pre_session_version": "5.1.0"}

        with patch.object(commit_mod, "_flow_wrote_version", return_value=True) as m_own, \
             patch.object(commit_mod, "_reanalyze_version_with_baseline") as m_re:
            result = commit_mod._guard_version_race(
                step, flow, disk_version="5.2.0", target_version="5.2.0"
            )

        # Own replay → keep the resolved target, do NOT re-analyse (no 2nd bump).
        assert result == "5.2.0"
        m_own.assert_called_once()
        m_re.assert_not_called()

    def test_true_concurrent_drift_still_reanalyses(self, tmp_path):
        from se3.engine.steps import commit as commit_mod

        flow = self._guard_flow(tmp_path)
        step = Step(step_type=StepType.COMMIT, status=StepStatus.PENDING)
        step.inputs = {"pre_session_version": "5.1.0"}

        # Not our own write (another flow bumped first) → recompute past it.
        with patch.object(commit_mod, "_flow_wrote_version", return_value=False), \
             patch.object(
                 commit_mod, "_reanalyze_version_with_baseline", return_value="5.3.0"
             ) as m_re:
            result = commit_mod._guard_version_race(
                step, flow, disk_version="5.2.0", target_version="5.2.0"
            )

        assert result == "5.3.0"
        m_re.assert_called_once()

    def test_reanalysis_forwards_refreshed_is_tag(self, tmp_path):
        """The recomputed tag decision must ride along with the recomputed version.

        Fix (iteration 11): `_reanalyze_version_with_baseline` forwarded
        bump_type/commit_message/versions_changes/reasoning but not `is_tag`, so
        commit_handler tagged the RECOMPUTED version using the SUPERSEDED
        analysis's tag decision — annotating a patch release, or silently
        skipping the tag for a minor one.
        """
        from se3.engine.steps import commit as commit_mod
        from se3.engine.steps import version_analyze as va_mod

        flow = self._guard_flow(tmp_path)
        va_step = Step(step_type=StepType.VERSION_ANALYZE, step_id="va-1")
        flow.state.steps["va-1"] = va_step
        flow.state.step_history.append("va-1")

        step = Step(step_type=StepType.COMMIT, status=StepStatus.PENDING)
        # Original (superseded) analysis said minor → tag.
        step.inputs = {"pre_session_version": "5.1.0", "is_tag": True}

        def fake_handler(s, _flow):
            # Re-analysis against the drifted 5.2.0 baseline yields a patch.
            s.outputs["suggested_version"] = "5.2.1"
            s.outputs["bump_type"] = "patch"
            s.outputs["is_tag"] = False
            return StepStatus.COMPLETED

        with patch.object(va_mod, "version_analyze_handler", fake_handler):
            new_version = commit_mod._reanalyze_version_with_baseline(
                step, flow, "5.2.0"
            )

        assert new_version == "5.2.1"
        assert step.inputs["bump_type"] == "patch"
        assert step.inputs["is_tag"] is False

    def test_reanalysis_forwards_is_tag_true(self, tmp_path):
        """Mirror case: patch → minor must start tagging."""
        from se3.engine.steps import commit as commit_mod
        from se3.engine.steps import version_analyze as va_mod

        flow = self._guard_flow(tmp_path)
        va_step = Step(step_type=StepType.VERSION_ANALYZE, step_id="va-1")
        flow.state.steps["va-1"] = va_step
        flow.state.step_history.append("va-1")

        step = Step(step_type=StepType.COMMIT, status=StepStatus.PENDING)
        step.inputs = {"pre_session_version": "5.1.0", "is_tag": False}

        def fake_handler(s, _flow):
            s.outputs["suggested_version"] = "5.3.0"
            s.outputs["bump_type"] = "minor"
            s.outputs["is_tag"] = True
            return StepStatus.COMPLETED

        with patch.object(va_mod, "version_analyze_handler", fake_handler):
            new_version = commit_mod._reanalyze_version_with_baseline(
                step, flow, "5.2.0"
            )

        assert new_version == "5.3.0"
        assert step.inputs["is_tag"] is True

    def test_reanalysis_returning_disk_version_halts(self, tmp_path):
        """Re-analysis that still returns the drifted disk version must halt.

        Fix (iteration 7): a concurrent flow bumped disk to 5.2.0; this flow's
        re-analysis erroneously returns 5.2.0 again (equal to the new baseline).
        Writing it would file this flow's changelog under the number the
        concurrent flow just released — the 10.7.1-type shared-version accident
        the guard exists to block. The guard must raise, not log-and-write.
        """
        from se3.engine.steps import commit as commit_mod

        flow = self._guard_flow(tmp_path)
        step = Step(step_type=StepType.COMMIT, status=StepStatus.PENDING)
        step.inputs = {"pre_session_version": "5.1.0"}

        with patch.object(commit_mod, "_flow_wrote_version", return_value=False), \
             patch.object(
                 commit_mod, "_reanalyze_version_with_baseline", return_value="5.2.0"
             ):
            with pytest.raises(RuntimeError, match="colliding version"):
                commit_mod._guard_version_race(
                    step, flow, disk_version="5.2.0", target_version="5.2.0"
                )
