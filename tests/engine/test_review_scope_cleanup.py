"""Lifecycle of the per-flow review-scope snapshot store.

Review baselines are heavy (a descriptor plus a content blob per dirty or
untracked file, once per implementation and once per fix iteration) and nothing
used to remove them, so every flow a project ever ran left its snapshots behind.
They are now reclaimed at a flow's terminal points.

The hard part is *not* reclaiming too early: ``luo run --resume`` offers back
every flow whose status is not COMPLETED — a FAILED one included, as a retry —
and a resumed SELF_CHECK round can only rebuild its diff while its baselines
survive. These tests pin both directions, plus the safety rule on the reclaim
path and the error the read-only command owes an operator afterwards.

Every test builds its own project directory and its own flow id, so the suite
stays parallel-safe.
"""

from __future__ import annotations

import subprocess
import tempfile
import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from tianluo.cli import app
from tianluo.engine.models import FlowInstance, FlowStatus, StepStatus, StepType
from tianluo.engine.persistence import PersistenceManager
from tianluo.engine.review_scope import (
    ReviewScopeManager,
    discard_flow_snapshots,
)
from tianluo.engine.state_machine import StateMachine

runner = CliRunner()


def _flow_id() -> str:
    return f"flow-{uuid.uuid4().hex[:12]}"


def _seed_snapshots(project_root: Path, flow_id: str) -> Path:
    """Materialize a plausible snapshot store (descriptor + blob) for a flow."""
    manager = ReviewScopeManager(project_root, flow_id)
    baseline_dir = manager.root / "implementation-0123456789ab"
    (baseline_dir / "blobs").mkdir(parents=True)
    (baseline_dir / "descriptor.json").write_text("{}", encoding="utf-8")
    (baseline_dir / "blobs" / ("a" * 64)).write_bytes(b"captured content")
    return manager.root


def _run_flow(sm: StateMachine, flow, max_steps: int = 80) -> None:
    flow.status = FlowStatus.RUNNING
    taken = 0
    while flow.status == FlowStatus.RUNNING and taken < max_steps:
        step = flow.state.get_current_step()
        if not step:
            break
        sm.run_step(flow, step)
        sm.transition_to_next(flow)
        taken += 1


def _register_passing_handlers(sm: StateMachine) -> None:
    def mock_handler(step, flow):
        return StepStatus.COMPLETED

    for step_type in StepType:
        sm.register_handler(step_type, mock_handler)


class TestEngineTerminalReclaim:
    def test_completed_flow_reclaims_its_snapshots(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sm = StateMachine(root)
            _register_passing_handlers(sm)

            flow = sm.create_flow("reclaim on completion", task_type="small")
            store = _seed_snapshots(root, flow.flow_id)
            assert store.is_dir()

            _run_flow(sm, flow)

            assert flow.status == FlowStatus.COMPLETED
            assert not store.exists()

    def test_failed_flow_keeps_its_snapshots(self):
        """A FAILED flow is still offered as a retry, so it keeps its baselines."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sm = StateMachine(root)
            sm._get_max_fix_iterations = lambda: 2  # type: ignore[assignment]
            _register_passing_handlers(sm)

            def always_revision(step, flow):
                step.outputs["fix_needed"] = True
                step.outputs["fix_instructions"] = "still broken"
                step.outputs["fix_context"] = {
                    "reason": "invariant_check", "issues": [],
                }
                return StepStatus.REVISION_NEEDED

            sm.register_handler(StepType.INVARIANT_CHECK, always_revision)

            flow = sm.create_flow("exhaust the fix loop", task_type="feature")
            sm.init_flow(flow)
            store = _seed_snapshots(root, flow.flow_id)

            _run_flow(sm, flow)

            assert flow.status == FlowStatus.FAILED
            assert store.is_dir(), (
                "a FAILED flow can be resumed as a retry, so the SELF_CHECK "
                "round it resumes into still needs its baselines"
            )

    def test_mid_flow_transitions_keep_the_snapshots(self):
        """Only the terminal transition reclaims; every earlier one leaves it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sm = StateMachine(root)
            _register_passing_handlers(sm)

            flow = sm.create_flow("mid-flow survival", task_type="small")
            store = _seed_snapshots(root, flow.flow_id)
            flow.status = FlowStatus.RUNNING

            step = flow.state.get_current_step()
            sm.run_step(flow, step)
            sm.transition_to_next(flow)

            assert flow.status == FlowStatus.RUNNING
            assert store.is_dir()

    def test_reclaim_touches_only_the_terminating_flow(self):
        """Another flow's snapshots — resumable or not — are never collateral."""
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sm = StateMachine(root)
            _register_passing_handlers(sm)

            other_id = _flow_id()
            other_store = _seed_snapshots(root, other_id)

            flow = sm.create_flow("terminating flow", task_type="small")
            own_store = _seed_snapshots(root, flow.flow_id)

            _run_flow(sm, flow)

            assert flow.status == FlowStatus.COMPLETED
            assert not own_store.exists()
            assert other_store.is_dir()
            assert (other_store / "implementation-0123456789ab"
                    / "descriptor.json").is_file()

    def test_reclaim_failure_does_not_fail_the_flow(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sm = StateMachine(root)
            _register_passing_handlers(sm)

            flow = sm.create_flow("reclaim explodes", task_type="small")
            _seed_snapshots(root, flow.flow_id)

            with patch.object(
                ReviewScopeManager,
                "discard_snapshots",
                side_effect=OSError("device on fire"),
            ):
                _run_flow(sm, flow)

            assert flow.status == FlowStatus.COMPLETED


class TestReclaimSafety:
    @pytest.mark.parametrize(
        "unsafe",
        ["..", "../elsewhere", "a/b", "", ".hidden", "flow id"],
    )
    def test_unsafe_flow_ids_are_refused(self, unsafe):
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = ReviewScopeManager(Path(tmpdir), unsafe)
            with pytest.raises(ValueError):
                manager.discard_snapshots()

    def test_unsafe_flow_id_cannot_reach_a_sibling_directory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            victim = _seed_snapshots(root, _flow_id()).parent
            assert victim.is_dir()

            with pytest.raises(ValueError):
                ReviewScopeManager(root, "../review-scopes").discard_snapshots()

            assert victim.is_dir()

    def test_module_helper_never_raises(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            assert discard_flow_snapshots(Path(tmpdir), "../escape") is False

    def test_module_helper_reports_whether_anything_was_removed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            flow_id = _flow_id()
            store = _seed_snapshots(root, flow_id)

            assert discard_flow_snapshots(root, flow_id) is True
            assert not store.exists()
            # Idempotent: reclaiming an already-reclaimed flow is a no-op.
            assert discard_flow_snapshots(root, flow_id) is False


class TestDispositionChannels:
    """The non-engine channels that also retire a flow for good."""

    def test_salvage_reclaims_the_salvaged_flow(self):
        from tianluo.commands.salvage_cmd import _archive_session

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            flow_id = _flow_id()
            flow = FlowInstance(flow_id=flow_id, task_description="salvage me")
            flow.state.context["project_root"] = str(root)
            PersistenceManager(root).save_flow(flow)
            store = _seed_snapshots(root, flow_id)

            assert _archive_session(root) is True
            assert not store.exists()

    def test_end_session_worktree_clear_reclaims(self):
        from tianluo.commands.end_session_cmd import _clear_resumable

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            flow_id = _flow_id()
            store = _seed_snapshots(root, flow_id)

            _clear_resumable(root, flow_id)

            assert not store.exists()

    def test_end_session_without_a_flow_id_reclaims_nothing(self):
        from tianluo.commands.end_session_cmd import _clear_resumable

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            store = _seed_snapshots(root, _flow_id())

            _clear_resumable(root, None)

            assert store.is_dir()


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=root, check=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )


class TestCommandContractAfterReclaim:
    """``luo review-scope diff`` owes the operator the agreed 'reclaimed' error."""

    def test_reclaimed_flow_reports_cleaned_with_exit_five(self, tmp_path):
        root = tmp_path / "project"
        root.mkdir()
        _git(root, "init")
        _git(root, "config", "user.email", "reclaim@example.com")
        _git(root, "config", "user.name", "Reclaim Test")
        (root / ".gitignore").write_text("/tianluo/\n", encoding="utf-8")
        (root / "alpha.py").write_text("value = 1\n", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "initial")

        flow_id = _flow_id()
        manager = ReviewScopeManager(root, flow_id)
        implementation = manager.capture("implementation")
        (root / "alpha.py").write_text("value = 2\n", encoding="utf-8")

        flow = FlowInstance(flow_id=flow_id, task_description="reclaimed scope")
        flow.state.context["project_root"] = str(root)
        flow.state.context["review_scope"] = {
            "implementation_baseline": implementation.to_dict()
        }
        PersistenceManager(root).save_flow(flow)

        # Sanity: the scope is readable before the flow is disposed of.
        with patch(
            "tianluo.commands.review_scope_cmd.get_project_root", return_value=root
        ):
            before = runner.invoke(
                app, ["review-scope", "diff", "--flow", flow_id]
            )
        assert before.exit_code == 0

        assert manager.discard_snapshots() is True

        with patch(
            "tianluo.commands.review_scope_cmd.get_project_root", return_value=root
        ):
            after = runner.invoke(
                app, ["review-scope", "diff", "--flow", flow_id]
            )

        assert after.exit_code == 5
        assert "reclaimed" in after.output
