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
from se3.engine.dag_scheduler import GroupResult, RelayContext
from se3.engine.llm_caller import LLMCaller
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

class _FakeResult:
    def __init__(self, *, success, output="", returncode=0):
        self.success = success
        self.output = output
        self.interrupted = False
        self.returncode = returncode
        self.cmd_used = "cmd"
        self.stderr_tail = ""


class _AgentStreamRunner:
    """Fake runner that streams one init line (revealing the model) and one
    tool_use, then returns a pre-programmed success/failure — so the tracker
    parses the model and fires the caller's on_agent_change."""

    def __init__(self, succeed, model="claude-opus-4-8"):
        self._succeed = succeed
        self._model = model

    def build_call_args(self, prompt, read_only, context_files=None, spec_guard_plugin=None):
        return ["-p", prompt]

    def detect_infra_error(self, returncode, output, stderr_tail):
        from se3.agent_runner import InfraErrorType

        return InfraErrorType.NONE

    def run_with_monitor(self, args, on_output=None, **kwargs):
        if on_output:
            on_output(json.dumps({"type": "init", "model": self._model}))
            on_output(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "tu-x",
                                    "name": "Read",
                                    "input": {"file_path": "a.py"},
                                }
                            ]
                        },
                    }
                )
            )
        return _FakeResult(
            success=self._succeed,
            output=json.dumps({"type": "init", "model": self._model}),
            returncode=0 if self._succeed else 1,
        )


class TestLLMCallerOnAgentChange:
    """The on_agent_change notification fires (agent, None) when each attempt's
    agent is selected and (agent, model) once the model is parsed — and across
    a rotation each attempt reports its own real agent, never a stale one."""

    def test_fires_agent_then_model(self, tmp_path):
        events: list[tuple[str, str | None]] = []

        caller = LLMCaller(
            project_root=tmp_path,
            max_retries=1,
            retry_delay=0,
            flow_id="flow-cb",
            step_id="01_implement_abc12345",
            step_type="implement",
            agents=[{"name": "dclaude", "type": "claude-code", "cmd": "echo"}],
            on_agent_change=lambda a, m: events.append((a, m)),
        )

        with patch.object(
            caller, "_get_current_runner",
            side_effect=lambda: _AgentStreamRunner(succeed=True),
        ), patch.object(LLMCaller, "_record_prompt"), patch.object(
            LLMCaller, "_record_response"
        ):
            caller.call("do the thing", json_mode="off")

        # First notification is the agent-only one fired at attempt selection;
        # the model upgrade follows once the init line is parsed.
        assert events[0] == ("dclaude", None)
        assert ("dclaude", "claude-opus-4-8") in events

    def test_rotation_reports_each_attempts_agent(self, tmp_path):
        events: list[tuple[str, str | None]] = []

        caller = LLMCaller(
            project_root=tmp_path,
            max_retries=2,
            retry_delay=0,
            flow_id="flow-rot",
            step_id="01_implement_abc12345",
            step_type="implement",
            agents=[
                {"name": "agentA", "type": "claude-code", "cmd": "echo"},
                {"name": "agentB", "type": "claude-code", "cmd": "echo"},
            ],
            on_agent_change=lambda a, m: events.append((a, m)),
        )

        runners = {
            0: _AgentStreamRunner(succeed=False),
            1: _AgentStreamRunner(succeed=True),
        }

        with patch.object(
            caller, "_get_current_runner",
            side_effect=lambda: runners[caller._current_agent_index],
        ), patch.object(LLMCaller, "_record_prompt"), patch.object(
            LLMCaller, "_record_response"
        ):
            caller.call("do the thing", json_mode="off")

        agents_seen = [a for a, _ in events]
        # Each attempt announced its own agent; the failed first attempt's
        # agentA never carries over to the rotated agentB attempt.
        assert agents_seen[0] == "agentA"
        assert "agentB" in agents_seen
        # The agent-only notification fired for both attempts.
        assert ("agentA", None) in events
        assert ("agentB", None) in events


class _ExecutingScheduler:
    """Scheduler stand-in that actually invokes ``execute_fn`` (with heavy
    worktree/git/LLM deps mocked) so the closure's on_agent_change relay and
    the coarse status sink's agent/model enrichment are both exercised."""

    def __init__(self, groups, max_workers=4, relay_plan=None, on_group_status=None):
        self._groups = groups
        self._on_group_status = on_group_status

    def run(self, execute_fn):
        results = []
        for g in self._groups:
            gid = g["group_id"]
            if self._on_group_status is not None:
                self._on_group_status(gid, "running")
            res = execute_fn(g, {}, RelayContext())
            results.append(res)
            if self._on_group_status is not None:
                self._on_group_status(gid, "completed")
        return results

    def get_fallback_leaves(self):
        return []


class _RelayCaller:
    """Fake LLMCaller whose .call() fires the on_agent_change relay just like
    the real in-worktree caller would (agent-only then agent · model)."""

    def __init__(self, *args, on_agent_change=None, **kwargs):
        self._cb = on_agent_change

    def call(self, prompt, **kwargs):
        if self._cb is not None:
            self._cb("dclaude", None)
            self._cb("dclaude", "claude-opus-4-8")
        return "{}"


class _GitStub:
    def __init__(self):
        self.stdout = ""
        self.stderr = ""
        self.returncode = 0


class TestDagAgentModelRelay:
    @patch("se3.engine.steps.implement.DAGScheduler", _ExecutingScheduler)
    @patch("se3.engine.steps.implement.merge_in_progress", return_value=False)
    @patch(
        "se3.engine.steps.implement.recover_stale_unmerged_paths",
        return_value=([], []),
    )
    @patch("se3.engine.steps.implement.get_current_branch", return_value="main")
    @patch("se3.engine.steps.implement.force_cleanup_worktree")
    @patch("se3.engine.steps.implement._restore_history_to_worktree")
    @patch("se3.engine.steps.implement.parse_json_response", return_value={})
    @patch("se3.engine.steps.implement._run_git", side_effect=lambda *a, **k: _GitStub())
    @patch("se3.engine.steps.implement.LLMCaller", _RelayCaller)
    @patch(
        "se3.engine.context_builder.get_runtime_context_injection",
        return_value="",
    )
    def test_running_and_completed_records_carry_agent_and_model(
        self,
        mock_runtime,
        mock_git,
        mock_parse,
        mock_restore,
        mock_cleanup,
        mock_branch,
        mock_recover,
        mock_merge_prog,
        tmp_path,
    ):
        """The closure's on_agent_change writes live running records carrying
        agent (then agent · model), and the coarse status sink enriches the
        completed record with the same final agent/model from the shared map."""
        wt_dir = tmp_path / "wt"
        wt_dir.mkdir()

        groups = [
            {
                "group_id": "G1",
                "group_order": 1,
                "depends_on": [],
                "tasks": [{"id": 1, "estimated_loc": 200}],
            },
        ]
        step = Step(
            step_type=StepType.IMPLEMENT,
            step_id="07_implement_relay1",
            inputs={},
            outputs={},
        )
        flow = FlowInstance(task_description="t", flow_id="flow-relay")

        with patch(
            "se3.engine.steps.implement.create_worktree", return_value=wt_dir
        ):
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

        records = _read_step_records(tmp_path, "flow-relay", "07_implement_relay1")
        status_records = [r for r in records if r.get("type") == "group_status"]
        assert status_records

        # At least one running record carries the real agent (and the model
        # once parsed) — the live "running in worktree" label.
        running_with_agent = [
            r
            for r in status_records
            if r["status"] == "running" and r.get("agent_name") == "dclaude"
        ]
        assert running_with_agent, "expected a running record tagged with the agent"
        assert any(
            r.get("model_name") == "claude-opus-4-8" for r in running_with_agent
        ), "expected the running label to upgrade to agent · model"

        # The completed record is enriched from the shared map with the final
        # agent/model so the card stays consistently labelled to the end.
        completed = [r for r in status_records if r["status"] == "completed"]
        assert completed
        assert completed[-1]["agent_name"] == "dclaude"
        assert completed[-1]["model_name"] == "claude-opus-4-8"

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
