"""Integration tests for the DAG implement per-group status wiring.

Covers G3: ``_run_dag_parallel`` builds a closure that persists each scheduler
status transition (running / completed / …) to the **main repo's** step jsonl
via ``chat_history.record_group_status``, so the web console can surface live
per-group progress while groups run in isolated worktrees. Also verifies the
group-history salvage path never appends to that main step file (which holds
the ``group_status`` records), so there is no duplication.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from se3.engine.chat_history import record_group_status
from se3.engine.dag_scheduler import GroupResult
from se3.engine.models import FlowInstance, Step, StepType
from se3.engine.steps.implement import _run_dag_parallel, _salvage_results_history


def _read_step_records(project_root: Path, flow_id: str, step_id: str) -> list[dict]:
    path = project_root / "se3" / "history" / flow_id / f"{step_id}.jsonl"
    if not path.exists():
        return []
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class _FakeScheduler:
    """Stand-in DAGScheduler that drives the captured ``on_group_status``.

    Mimics the real scheduler's lifecycle emissions (running → completed for
    each group) so the test exercises implement.py's closure wiring without
    spinning up worktrees, threads, or git. The constructor signature mirrors
    the real :class:`DAGScheduler` so it slots in via ``patch``.
    """

    def __init__(self, groups, max_workers=4, relay_plan=None, on_group_status=None):
        self._groups = groups
        self._on_group_status = on_group_status

    def run(self, execute_fn):
        results = []
        for g in self._groups:
            gid = g["group_id"]
            if self._on_group_status is not None:
                self._on_group_status(gid, "running")
                self._on_group_status(gid, "completed")
            # No branch_name / worktree_path → merge + cleanup paths are no-ops.
            results.append(GroupResult(group_id=gid, status="completed"))
        return results

    def get_fallback_leaves(self):
        return []


class TestDagGroupStatusWiring:
    @patch("se3.engine.steps.implement.DAGScheduler", _FakeScheduler)
    @patch("se3.engine.steps.implement.merge_in_progress", return_value=False)
    @patch(
        "se3.engine.steps.implement.recover_stale_unmerged_paths",
        return_value=([], []),
    )
    @patch("se3.engine.steps.implement.get_current_branch", return_value="main")
    def test_running_and_completed_records_written(
        self, mock_branch, mock_recover, mock_merge_prog, tmp_path
    ):
        """During DAG parallel execution the main step jsonl gains running +
        completed group_status records for each group, tagged step_type."""
        groups = [
            {
                "group_id": "G1",
                "group_order": 1,
                "depends_on": [],
                "tasks": [{"id": 1, "estimated_loc": 200}],
            },
            {
                "group_id": "G2",
                "group_order": 2,
                "depends_on": ["G1"],
                "tasks": [{"id": 2, "estimated_loc": 200}],
            },
        ]
        step = Step(
            step_type=StepType.IMPLEMENT,
            step_id="07_implement_abc123",
            inputs={},
            outputs={},
        )
        flow = FlowInstance(task_description="t", flow_id="flow-xyz")

        _run_dag_parallel(
            groups=groups,
            step=step,
            flow=flow,
            project_root=tmp_path,
            task_description="t",
            task_type="feature",
            design_section="",
            spec_summary="",
            injection=None,
            retry_count=0,
        )

        records = _read_step_records(tmp_path, "flow-xyz", "07_implement_abc123")
        status_records = [r for r in records if r.get("type") == "group_status"]
        assert status_records, "expected group_status records in the main step jsonl"

        seen = {(r["group_id"], r["status"]) for r in status_records}
        for gid in ("G1", "G2"):
            assert (gid, "running") in seen
            assert (gid, "completed") in seen

        # flow_id / step_id were routed correctly: the records landed under the
        # main repo's se3/history/<flow_id>/<step_id>.jsonl, and every record
        # carries the implement step_type.
        assert all(r["step_type"] == "implement" for r in status_records)

    def test_salvage_does_not_append_to_main_step_file(self, tmp_path):
        """The main step file (group_status sink) is never touched by the
        _G\\d+ history salvage, so status records are not duplicated."""
        project_root = tmp_path / "main"
        flow_id = "flow-salvage"
        step_id = "07_implement_def456"

        # The DAG status sink wrote group_status lines into the main step file.
        record_group_status(project_root, flow_id, step_id, "implement", "G1", "running")
        record_group_status(
            project_root, flow_id, step_id, "implement", "G1", "completed"
        )
        main_step_file = (
            project_root / "se3" / "history" / flow_id / f"{step_id}.jsonl"
        )
        before = main_step_file.read_text(encoding="utf-8")

        # A worktree carries its own group-specific conversation history file.
        worktree = tmp_path / "wt"
        wt_flow_dir = worktree / "se3" / "history" / flow_id
        wt_flow_dir.mkdir(parents=True)
        group_history = wt_flow_dir / f"{step_id}_G1.jsonl"
        group_history.write_text(
            '{"role": "assistant", "content": "group work"}\n', encoding="utf-8"
        )

        results = [
            GroupResult(
                group_id="G1",
                status="completed",
                branch_name="impl/f/G1",
                worktree_path=worktree,
            )
        ]
        _salvage_results_history(results, project_root)

        # Salvage matches only _G\d+.jsonl, so the main step file is unchanged.
        assert main_step_file.read_text(encoding="utf-8") == before

        # The group conversation file, however, was salvaged into the main repo.
        salvaged = (
            project_root / "se3" / "history" / flow_id / f"{step_id}_G1.jsonl"
        )
        assert salvaged.exists()
        assert "group work" in salvaged.read_text(encoding="utf-8")
