"""End-to-end tests for the flow engine.

Tests complete flow execution for real tasks.
"""

import json
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from .models import FlowInstance, StepType, StepStatus, FlowStatus
from .state_machine import StateMachine
from .persistence import PersistenceManager


class MockSubprocessResult:
    """Mock for subprocess result."""
    def __init__(self, stdout="", stderr="", returncode=0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def create_mock_llm_responses():
    """Create mock LLM responses for a complete flow."""
    return {
        StepType.ANALYZE: json.dumps({
            "task_type": "small",
            "scope": "documentation",
            "complexity": "simple",
            "suggested_steps": ["analyze", "implement", "commit"],
        }),
        StepType.PLAN: json.dumps({
            "title": "Add README",
            "description": "Create project README file",
        }),
        StepType.IMPLEMENT: "Created README.md with project description",
        StepType.TEST: json.dumps({"passed": True}),
        StepType.COMMIT: json.dumps({
            "message": "docs: add README",
            "files": ["README.md"],
        }),
    }


class TestEndToEndSmallTask:
    """End-to-end test for a small task."""

    @patch("subprocess.run")
    def test_complete_small_flow(self, mock_run):
        """Test complete flow for a small documentation task."""
        responses = create_mock_llm_responses()
        call_count = [0]

        def mock_run_impl(args, **kwargs):
            call_count[0] += 1
            # Determine which step based on args
            prompt = " ".join(args) if isinstance(args, list) else str(args)

            if "analyze" in prompt.lower():
                return MockSubprocessResult(stdout=responses.get(StepType.ANALYZE, "{}"))
            elif "implement" in prompt.lower():
                return MockSubprocessResult(stdout=responses.get(StepType.IMPLEMENT, ""))
            elif "test" in prompt.lower():
                return MockSubprocessResult(stdout=responses.get(StepType.TEST, "{}"))
            elif "commit" in prompt.lower():
                return MockSubprocessResult(returncode=0)
            else:
                return MockSubprocessResult(stdout="{}")

        mock_run.side_effect = mock_run_impl

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            sm = StateMachine(project_root)

            # Create flow
            flow = sm.create_flow(
                task_description="Add README file",
                task_type="small"
            )

            # Register mock handlers for all steps
            def mock_handler(step, flow):
                step.status = StepStatus.COMPLETED
                step.outputs["mock"] = True
                return StepStatus.COMPLETED

            for step_type in StepType:
                sm.register_handler(step_type, mock_handler)

            # Execute flow
            max_steps = 15
            steps_taken = 0
            while flow.status not in (FlowStatus.COMPLETED, FlowStatus.FAILED) and steps_taken < max_steps:
                step = flow.state.get_current_step()
                if not step:
                    flow.status = FlowStatus.COMPLETED
                    break

                sm.run_step(flow, step)
                sm.transition_to_next(flow)
                steps_taken += 1

            assert flow.status == FlowStatus.COMPLETED
            assert steps_taken > 0

    @patch("subprocess.run")
    def test_flow_with_interruption(self, mock_run):
        """Test flow that gets interrupted and resumed."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            pm = PersistenceManager(project_root)
            sm = StateMachine(project_root)

            # Create and save flow
            flow = sm.create_flow("Test task")
            step = flow.state.get_current_step()
            step.status = StepStatus.COMPLETED
            pm.save_flow(flow)

            # Simulate interruption by creating new instances
            pm2 = PersistenceManager(project_root)
            sm2 = StateMachine(project_root)

            loaded, is_resumed = sm2.load_or_create_flow()

            assert is_resumed
            assert loaded.flow_id == flow.flow_id
            assert loaded.state.get_current_step() is not None


class TestRealWorldScenarios:
    """Tests based on real-world scenarios."""

    def test_feature_request_flow(self):
        """Test flow for a typical feature request."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))

            def mock_handler(step, flow):
                step.status = StepStatus.COMPLETED
                return StepStatus.COMPLETED

            for step_type in StepType:
                sm.register_handler(step_type, mock_handler)

            flow = sm.create_flow(
                task_description="Add user authentication",
                task_type="feature"
            )

            # Execute all steps
            max_steps = 20
            steps_taken = 0
            while flow.status not in (FlowStatus.COMPLETED, FlowStatus.FAILED) and steps_taken < max_steps:
                step = flow.state.get_current_step()
                if not step:
                    flow.status = FlowStatus.COMPLETED
                    break

                result = sm.run_step(flow, step)
                if result == StepStatus.FAILED:
                    break

                sm.transition_to_next(flow)
                steps_taken += 1

            assert flow.status == FlowStatus.COMPLETED
            # Feature should go through analyze, propose, design, implement, test, commit
            assert steps_taken >= 5

    def test_bugfix_flow(self):
        """Test flow for a bug fix."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))

            def mock_handler(step, flow):
                step.status = StepStatus.COMPLETED
                return StepStatus.COMPLETED

            for step_type in StepType:
                sm.register_handler(step_type, mock_handler)

            flow = sm.create_flow(
                task_description="Fix login error",
                task_type="bugfix"
            )

            # Execute
            max_steps = 15
            steps_taken = 0
            while flow.status not in (FlowStatus.COMPLETED, FlowStatus.FAILED) and steps_taken < max_steps:
                step = flow.state.get_current_step()
                if not step:
                    break

                sm.run_step(flow, step)
                sm.transition_to_next(flow)
                steps_taken += 1

            assert flow.status == FlowStatus.COMPLETED


class TestErrorRecovery:
    """Tests for error recovery in real scenarios."""

    def test_recovery_after_step_failure(self):
        """Test recovery when a step fails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            sm = StateMachine(Path(tmpdir))

            call_count = 0

            def flaky_handler(step, flow):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    step.error_message = "Simulated error"
                    return StepStatus.FAILED
                step.status = StepStatus.COMPLETED
                return StepStatus.COMPLETED

            sm.register_handler(StepType.ANALYZE, flaky_handler)

            # Register success handlers for other steps
            for step_type in [StepType.PLAN, StepType.IMPLEMENT, StepType.COMMIT]:
                sm.register_handler(step_type, lambda s, f: StepStatus.COMPLETED)

            flow = sm.create_flow("Test recovery")

            # First attempt fails
            step = flow.state.get_current_step()
            result = sm.run_step(flow, step)
            assert result == StepStatus.FAILED

            # Retry succeeds
            step.retry_count += 1
            result = sm.run_step(flow, step)
            assert result == StepStatus.COMPLETED

    def test_state_consistency_across_persistence(self):
        """Test that state remains consistent after save/load cycles."""
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            pm = PersistenceManager(project_root)
            sm = StateMachine(project_root)

            # Create flow and progress through some steps
            flow = sm.create_flow("Test consistency")

            # Complete analyze step
            step1 = flow.state.get_current_step()
            step1.status = StepStatus.COMPLETED
            step1.outputs["result"] = "analysis done"

            # Transition
            sm.transition_to_next(flow)

            # Save
            pm.save_flow(flow)

            # Load in fresh instances
            pm2 = PersistenceManager(project_root)
            sm2 = StateMachine(project_root)

            loaded, _ = sm2.load_or_create_flow()

            # Verify state
            assert loaded.flow_id == flow.flow_id
            assert len(loaded.state.steps) == 2  # analyze + next step
            first_step_id = loaded.state.step_history[0]
            assert loaded.state.steps[first_step_id].outputs["result"] == "analysis done"
            assert loaded.state.get_current_step().step_type != StepType.ANALYZE


class TestWorktreeMergeSteps:
    """End-to-end coverage for the merge-side steps of a worktree flow.

    A ``--worktree`` flow's release point is the merge, so its sequence ends
    with ``merge_integrate`` + ``version_reconcile`` (executed in the main
    checkout under the merge lock via the step-level cwd override). Most tests
    stub the ``integrate()`` / ``reconcile()`` libraries and drive the state
    machine to prove the wiring: sequence composition, end-to-end execution to a
    reconciled version, resume between the two steps, and the per-step
    confirmation gate landing on the version decision only. One test
    (``test_worktree_merge_steps_land_real_version_on_master``) drives the same
    step path against the REAL libraries in a temp git repo, so a genuine
    handler↔library integration break is caught here, not only in the CLI tests.
    """

    @staticmethod
    def _register_mock_handlers(sm, *, keep_merge_real=True):
        """Register pass-through handlers for every non-merge step type."""
        from tianluo.engine.steps import (
            merge_integrate_handler,
            version_reconcile_handler,
        )

        def mock_handler(step, flow):
            step.status = StepStatus.COMPLETED
            step.outputs.setdefault("mock", True)
            return StepStatus.COMPLETED

        for step_type in StepType:
            sm.register_handler(step_type, mock_handler)
        if keep_merge_real:
            sm.register_handler(StepType.MERGE_INTEGRATE, merge_integrate_handler)
            sm.register_handler(StepType.VERSION_RECONCILE, version_reconcile_handler)

    @staticmethod
    def _fake_integrate_result(branch):
        from types import SimpleNamespace

        return SimpleNamespace(
            success=True,
            pending_human=False,
            merged_branches=[branch],
            newly_merged_branches=[branch],
            already_ancestor_branches=[],
            failure_reason=None,
            failure_detail=None,
        )

    @staticmethod
    def _fake_reconcile_result():
        from tianluo.engine.merge.reconcile import ReconcileResult

        return ReconcileResult(
            success=True,
            base_version="11.12.0",
            final_version="11.13.0",
            bump_type="minor",
            channel="deterministic",
            consumed_flow_ids=["flow-x"],
            reconcile_commit="abc123def456",
        )

    def _make_worktree_flow(self, project_root):
        (project_root / "tianluo" / "state").mkdir(parents=True, exist_ok=True)
        # The merge-side steps resolve their cwd via ``probe_main_repo_root``,
        # which is deliberately strict: a non-git project_root is a genuine fault
        # (a silent fallback to a linked worktree would land the merge outside the
        # main checkout). Even for the mocked-library wiring tests the fixture must
        # therefore be a real git repo so the probe resolves — here project_root is
        # the main checkout itself, so the probe returns it as the merge cwd.
        self._git(project_root, "init", "-q")
        self._git(project_root, "config", "user.email", "t@example.com")
        self._git(project_root, "config", "user.name", "Test")
        sm = StateMachine(project_root)
        self._register_mock_handlers(sm)
        flow = sm.create_flow(
            task_description="Add a worktree feature",
            task_type="small",  # avoids the always-on plan-confirm
            is_worktree_mode=True,
        )
        flow.worktree_branch = "worktree/add-feature"
        flow.worktree_original_branch = "main"
        return sm, flow

    def _run_to_completion(self, sm, flow, *, max_steps=30):
        steps = 0
        while (
            flow.status not in (FlowStatus.COMPLETED, FlowStatus.FAILED)
            and steps < max_steps
        ):
            step = flow.state.get_current_step()
            if not step:
                break
            sm.run_step(flow, step)
            if step.status == StepStatus.FAILED:
                break
            sm.transition_to_next(flow)
            steps += 1
        return steps

    def test_worktree_sequence_appends_merge_steps(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sm, flow = self._make_worktree_flow(Path(tmpdir))
            seq = flow.state.selected_steps
            assert StepType.MERGE_INTEGRATE in seq
            assert StepType.VERSION_RECONCILE in seq
            # The merge is the immediate post-commit boundary: integrate then
            # reconcile land directly after commit, BEFORE any ordinary post-commit
            # step (e.g. a configured summarize) — no flow step may run in the
            # worktree between the de-versioned commit and the branch landing on
            # master.
            commit_i = seq.index(StepType.COMMIT)
            assert seq[commit_i + 1] == StepType.MERGE_INTEGRATE
            assert seq[commit_i + 2] == StepType.VERSION_RECONCILE
            if StepType.SUMMARIZE in seq:
                assert seq.index(StepType.SUMMARIZE) > seq.index(
                    StepType.VERSION_RECONCILE
                )
            # a non-worktree flow never gets them
            sm2 = StateMachine(Path(tmpdir))
            plain = sm2.create_flow("x", task_type="small", is_worktree_mode=False)
            assert StepType.MERGE_INTEGRATE not in plain.state.selected_steps

    def test_analyze_rederive_preserves_worktree_merge_steps(self):
        """The analyze step's sequence rebuild must NOT drop the merge steps.

        ANALYZE is the first step of every task-type sequence and always rebuilds
        ``selected_steps`` from scratch (:func:`analyze._update_flow_steps`). The
        two merge-side steps create_flow appended for a worktree flow must survive
        that rebuild in the post-commit position — a rebuild that discarded them
        (the regression) would leave every ``se3 run --worktree`` completing at
        summarize without ever merging or reconciling. The worktree e2e tests mock
        the ANALYZE handler, so this drives the real re-derivation directly.
        """
        from tianluo.engine.steps.analyze import _update_flow_steps

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sm, flow = self._make_worktree_flow(root)
            # change_path.parent is the project root _update_flow_steps loads
            # config from; point it at the isolated tmpdir (no tianluo.yaml → defaults).
            flow.change_path = root / "change"
            assert StepType.MERGE_INTEGRATE in flow.state.selected_steps

            _update_flow_steps(flow, "small")

            seq = flow.state.selected_steps
            assert StepType.MERGE_INTEGRATE in seq
            assert StepType.VERSION_RECONCILE in seq
            commit_i = seq.index(StepType.COMMIT)
            assert seq[commit_i + 1] == StepType.MERGE_INTEGRATE
            assert seq[commit_i + 2] == StepType.VERSION_RECONCILE

            # a non-worktree flow's analyze rebuild still omits them
            plain = sm.create_flow("x", task_type="small", is_worktree_mode=False)
            plain.change_path = root / "change"
            _update_flow_steps(plain, "small")
            assert StepType.MERGE_INTEGRATE not in plain.state.selected_steps
            assert StepType.VERSION_RECONCILE not in plain.state.selected_steps

    def test_worktree_flow_runs_to_reconcile_on_master(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sm, flow = self._make_worktree_flow(Path(tmpdir))
            with patch(
                "tianluo.engine.merge.integrate",
                return_value=self._fake_integrate_result("worktree/add-feature"),
            ) as mock_integrate, patch(
                "tianluo.engine.merge.reconcile",
                return_value=self._fake_reconcile_result(),
            ) as mock_reconcile:
                self._run_to_completion(sm, flow)

            assert flow.status == FlowStatus.COMPLETED
            # integrate ran against the main checkout for our branch
            mock_integrate.assert_called_once()
            _args, kwargs = mock_integrate.call_args
            assert kwargs.get("delete_merged") is False
            assert kwargs.get("acquire_lock") is False
            # reconcile ran and produced the final version, recorded on the step
            mock_reconcile.assert_called_once()
            reconcile_step = next(
                s for s in flow.state.steps.values()
                if s.step_type == StepType.VERSION_RECONCILE
            )
            assert reconcile_step.status == StepStatus.COMPLETED
            assert reconcile_step.outputs["final_version"] == "11.13.0"
            assert reconcile_step.outputs["base_version"] == "11.12.0"

    @staticmethod
    def _git(root, *args):
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=True,
        )

    def _make_real_git_project(self, root):
        """A committed git project + a feature branch carrying a version intent.

        Mirrors a de-versioned worktree session: a feature branch with a code
        change plus a committed ``tianluo/version-intents/<flow>.json`` (changelog
        bullet + minor bump hint, NO version number). Returns the default branch
        name. HEAD is left on the default branch, so the merge steps merge the
        feature branch into it.
        """
        from tianluo.engine.version_intent import VersionIntent, write_intent

        (root / "tianluo" / "state").mkdir(parents=True, exist_ok=True)
        (root / "pyproject.toml").write_text(
            '[project]\nname = "demo"\nversion = "11.12.0"\n', encoding="utf-8"
        )
        (root / "VERSIONS.md").write_text(
            "# Demo Version History\n\n## 11.12.0 - 2026-07-06\n- baseline entry\n",
            encoding="utf-8",
        )
        (root / "README.md").write_text("# Demo\n", encoding="utf-8")
        self._git(root, "init", "-q")
        self._git(root, "config", "user.email", "t@example.com")
        self._git(root, "config", "user.name", "Test")
        self._git(root, "add", "-A")
        self._git(root, "commit", "-q", "-m", "baseline")
        default = self._git(
            root, "rev-parse", "--abbrev-ref", "HEAD"
        ).stdout.strip()

        self._git(root, "checkout", "-q", "-b", "worktree/add-feature")
        (root / "feature.txt").write_text("work\n", encoding="utf-8")
        write_intent(
            root,
            VersionIntent(
                flow_id="flow-real",
                change_summary="add feature",
                versions_changes=["feat: a real landed feature"],
                bump_type="minor",
                pre_session_baseline="11.12.0",
                provisional_suggested_version="11.13.0",
            ),
        )
        self._git(root, "add", "-A")
        self._git(root, "commit", "-q", "-m", "work + intent")
        self._git(root, "checkout", "-q", default)
        return default

    def test_worktree_merge_steps_land_real_version_on_master(self):
        """Drive the state machine's merge steps against the REAL libraries.

        The other tests in this class stub ``integrate()`` / ``reconcile()`` to
        prove state-machine wiring; this one drives the same step path through a
        real temp git repo with the real libraries, so a genuine handler↔library
        integration break (signature drift, lock interplay, result-shape
        mismatch) is caught by the engine e2e rather than only by the CLI-path
        tests. It exercises cwd override + merge lock + real integrate + real
        reconcile together, landing a real version on master via the steps.
        """
        from tianluo.engine.merge.reconcile import read_current_version
        from tianluo.engine.version_intent import is_consumed

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            default = self._make_real_git_project(root)

            # Build a worktree flow whose merge steps carry cwd == the main
            # checkout (== root here, since root is not itself a linked worktree).
            sm = StateMachine(root)
            self._register_mock_handlers(sm)  # real merge handlers; mock the rest
            flow = sm.create_flow(
                task_description="Add a worktree feature",
                task_type="small",
                is_worktree_mode=True,
            )
            # version_reconcile filters to THIS flow's own intent (the change-#3
            # concurrency guard). In production the intent is written by this same
            # flow during version_analyze/commit, so its id always matches. The
            # fixture had to write the intent before the flow existed, so align the
            # flow's id with the intent it owns — otherwise reconcile finds no
            # matching intent and no-ops, leaving the version un-bumped.
            flow.flow_id = "flow-real"
            flow.worktree_branch = "worktree/add-feature"
            flow.worktree_original_branch = default

            self._run_to_completion(sm, flow)

            assert flow.status == FlowStatus.COMPLETED
            # Real integrate(): the branch actually landed on master.
            ancestor = subprocess.run(
                [
                    "git", "-C", str(root), "merge-base", "--is-ancestor",
                    "worktree/add-feature", default,
                ],
                capture_output=True,
            )
            assert ancestor.returncode == 0
            # Real reconcile(): a real version was derived + written onto master.
            assert read_current_version(root) == "11.13.0"
            versions = (root / "VERSIONS.md").read_text(encoding="utf-8")
            assert "feat: a real landed feature" in versions
            assert "11.13.0" in versions
            assert is_consumed(root, "flow-real")
            # A reconcile commit carrying the session trailer landed on master.
            log = self._git(root, "log", "--format=%B", "-n", "20").stdout
            assert "Version-Reconcile-Session: flow-real" in log
            # The step recorded the REAL ReconcileResult shape (not a canned one).
            reconcile_step = next(
                s for s in flow.state.steps.values()
                if s.step_type == StepType.VERSION_RECONCILE
            )
            assert reconcile_step.status == StepStatus.COMPLETED
            assert reconcile_step.outputs["final_version"] == "11.13.0"
            assert reconcile_step.outputs["base_version"] == "11.12.0"

    def test_integrate_failure_does_not_reach_reconcile(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            sm, flow = self._make_worktree_flow(Path(tmpdir))
            from types import SimpleNamespace

            failed = SimpleNamespace(
                success=False,
                pending_human=False,
                merged_branches=[],
                failure_reason="CONFLICT",
                failure_detail="unresolved",
            )
            with patch(
                "tianluo.engine.merge.integrate", return_value=failed
            ), patch("tianluo.engine.merge.reconcile") as mock_reconcile:
                self._run_to_completion(sm, flow)

            # merge_integrate FAILED, so version_reconcile never ran.
            integrate_step = next(
                s for s in flow.state.steps.values()
                if s.step_type == StepType.MERGE_INTEGRATE
            )
            assert integrate_step.status == StepStatus.FAILED
            mock_reconcile.assert_not_called()
            assert flow.status != FlowStatus.COMPLETED

    def test_resume_between_merge_steps_only_reruns_reconcile(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            sm, flow = self._make_worktree_flow(project_root)

            # Phase 1: run until merge_integrate has completed and the flow has
            # transitioned to version_reconcile (PENDING) — then "crash".
            with patch(
                "tianluo.engine.merge.integrate",
                return_value=self._fake_integrate_result("worktree/add-feature"),
            ) as mock_integrate:
                steps = 0
                while steps < 30:
                    step = flow.state.get_current_step()
                    if not step:
                        break
                    sm.run_step(flow, step)
                    sm.transition_to_next(flow)
                    steps += 1
                    cur = flow.state.get_current_step()
                    if cur and cur.step_type == StepType.VERSION_RECONCILE:
                        break
            assert mock_integrate.call_count == 1

            # Phase 2: fresh process — new StateMachine + persistence — resumes.
            sm2 = StateMachine(project_root)
            self._register_mock_handlers(sm2)
            loaded, is_resumed = sm2.load_or_create_flow()
            assert is_resumed
            loaded.worktree_branch = "worktree/add-feature"

            with patch(
                "tianluo.engine.merge.integrate"
            ) as mock_integrate2, patch(
                "tianluo.engine.merge.reconcile",
                return_value=self._fake_reconcile_result(),
            ) as mock_reconcile:
                self._run_to_completion(sm2, loaded)

            assert loaded.status == FlowStatus.COMPLETED
            # The merge was NOT redone on resume — only the version decision reran.
            mock_integrate2.assert_not_called()
            mock_reconcile.assert_called_once()

    def test_per_step_confirm_gates_only_version_decision(self):
        import yaml

        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            (project_root / "tianluo" / "state").mkdir(parents=True, exist_ok=True)
            # Gate ONLY the version decision behind a human confirmation.
            (project_root / "tianluo.yaml").write_text(
                yaml.safe_dump(
                    {
                        "confirmation": {
                            "steps": {"version_reconcile": {"reviewer": "human"}}
                        }
                    }
                )
            )
            sm = StateMachine(project_root)
            self._register_mock_handlers(sm)
            flow = sm.create_flow(
                task_description="x", task_type="small", is_worktree_mode=True
            )
            seq = flow.state.selected_steps
            ri = seq.index(StepType.VERSION_RECONCILE)
            mi = seq.index(StepType.MERGE_INTEGRATE)
            # A CONFIRM is inserted immediately AFTER version_reconcile...
            assert seq[ri + 1] == StepType.CONFIRM
            # ...but NOT after merge_integrate (the expensive merge is not re-gated).
            assert seq[mi + 1] == StepType.VERSION_RECONCILE


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
