"""Tests for se3 history command — get_flow_detail() fallback logic.

Tests cover:
- Active flow lookup (existing behavior)
- Archived flow fallback
- History-only flow fallback
- Flow not found returns None
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from typer.testing import CliRunner

from se3.commands import history_cmd
from se3.commands.history_cmd import (
    app,
    get_flow_detail,
    _load_archived_flow,
    _detail_from_history,
    _show_detailed_sessions,
)


def _make_flow_dict(flow_id: str, task: str = "Test task", status: str = "completed") -> dict:
    """Create a minimal serialized FlowInstance dict."""
    now = datetime.now().isoformat()
    return {
        "flow_id": flow_id,
        "status": status,
        "task_description": task,
        "task_type": "bugfix",
        "state": {
            "current_step_id": None,
            "step_history": [],
            "steps": {},
            "selected_steps": [],
        },
        "created_at": now,
        "updated_at": now,
        "completed_at": now,
        "change_name": None,
        "change_path": None,
        "source_issue_id": None,
        "baseline_commit": None,
        "is_loop_mode": False,
        "loop_branch": None,
        "loop_worktree_path": None,
        "loop_original_branch": None,
    }


@pytest.fixture
def project(tmp_path):
    """Create a project directory with se3 structure."""
    (tmp_path / "se3" / "state" / "archive").mkdir(parents=True)
    (tmp_path / "se3" / "history").mkdir(parents=True)
    return tmp_path


class TestGetFlowDetailActive:
    """get_flow_detail returns detail from active engine.json."""

    def test_active_flow_found(self, project):
        flow_data = _make_flow_dict("flow-active-001")
        state_file = project / "se3" / "state" / "engine.json"
        state_file.write_text(json.dumps(flow_data), encoding="utf-8")

        detail = get_flow_detail(project, "flow-active-001")
        assert detail is not None
        assert detail["flow_id"] == "flow-active-001"
        assert detail["task_description"] == "Test task"

    def test_active_flow_id_mismatch_falls_through(self, project):
        flow_data = _make_flow_dict("flow-active-001")
        state_file = project / "se3" / "state" / "engine.json"
        state_file.write_text(json.dumps(flow_data), encoding="utf-8")

        detail = get_flow_detail(project, "flow-other-999")
        assert detail is None


class TestGetFlowDetailArchive:
    """get_flow_detail falls back to archived flows."""

    def test_archived_flow_found(self, project):
        flow_data = _make_flow_dict("flow-archived-002", task="Archived task")
        archive_file = project / "se3" / "state" / "archive" / "engine_20260401_120000.json"
        archive_file.write_text(json.dumps(flow_data), encoding="utf-8")

        detail = get_flow_detail(project, "flow-archived-002")
        assert detail is not None
        assert detail["flow_id"] == "flow-archived-002"
        assert detail["task_description"] == "Archived task"

    def test_archived_flow_preferred_over_history(self, project):
        """When flow exists in both archive and history, archive is used."""
        flow_data = _make_flow_dict("flow-both-003", task="From archive")
        archive_file = project / "se3" / "state" / "archive" / "engine_20260401_130000.json"
        archive_file.write_text(json.dumps(flow_data), encoding="utf-8")

        # Also create a history dir for the same flow
        history_dir = project / "se3" / "history" / "flow-both-003"
        history_dir.mkdir()
        (history_dir / "_meta.json").write_text(
            json.dumps({"created_at": datetime.now().isoformat()}),
            encoding="utf-8",
        )

        detail = get_flow_detail(project, "flow-both-003")
        assert detail is not None
        assert detail["status"] == "completed"  # from archive, not "history"

    def test_multiple_archive_files_scanned(self, project):
        """Correct flow is found even with multiple archive files."""
        flow_a = _make_flow_dict("flow-a", task="Task A")
        flow_b = _make_flow_dict("flow-b", task="Task B")
        archive_dir = project / "se3" / "state" / "archive"
        (archive_dir / "engine_20260401_100000.json").write_text(
            json.dumps(flow_a), encoding="utf-8"
        )
        (archive_dir / "engine_20260401_110000.json").write_text(
            json.dumps(flow_b), encoding="utf-8"
        )

        detail = get_flow_detail(project, "flow-b")
        assert detail is not None
        assert detail["task_description"] == "Task B"


class TestGetFlowDetailHistory:
    """get_flow_detail falls back to history-only flows."""

    def test_history_only_flow(self, project):
        flow_id = "flow-history-004"
        history_dir = project / "se3" / "history" / flow_id
        history_dir.mkdir()

        # Write a _meta.json
        meta = {"created_at": "2026-04-01T12:00:00", "type": "bugfix"}
        (history_dir / "_meta.json").write_text(
            json.dumps(meta), encoding="utf-8"
        )

        # Write a minimal JSONL chat history file
        msg = {
            "role": "user",
            "content": "Task description:\n---\nFix the bug\n---",
            "raw_json": [],
            "timestamp": "2026-04-01T12:00:00",
            "step_type": "analyze",
            "attempt": 0,
        }
        (history_dir / "analyze_0.jsonl").write_text(
            json.dumps(msg) + "\n", encoding="utf-8"
        )

        detail = get_flow_detail(project, flow_id)
        assert detail is not None
        assert detail["flow_id"] == flow_id
        assert detail["status"] == "history"
        assert detail["task_type"] == "bugfix"
        assert detail["chat_sessions"] >= 1

    def test_history_no_such_flow(self, project):
        detail = get_flow_detail(project, "nonexistent-flow")
        assert detail is None


class TestLoadArchivedFlow:
    """Unit tests for _load_archived_flow helper."""

    def test_no_archive_dir(self, tmp_path):
        assert _load_archived_flow(tmp_path, "any-id") is None

    def test_malformed_archive_skipped(self, project):
        archive_dir = project / "se3" / "state" / "archive"
        (archive_dir / "engine_20260401_000000.json").write_text("NOT JSON")

        assert _load_archived_flow(project, "any-id") is None


class TestDetailFromHistory:
    """Unit tests for _detail_from_history helper."""

    def test_no_history_dir(self, tmp_path):
        assert _detail_from_history(tmp_path, "no-such-flow") is None

    def test_empty_history_dir(self, project):
        flow_id = "empty-flow"
        (project / "se3" / "history" / flow_id).mkdir()

        detail = _detail_from_history(project, flow_id)
        assert detail is not None
        assert detail["flow_id"] == flow_id
        assert detail["steps"] == []
        assert detail["chat_sessions"] == 0

    def test_self_check_pass_index_reconstructed(self, project):
        """History-only flows reconstruct self_check pass indices from session order."""
        flow_id = "flow-sc-passes"
        history_dir = project / "se3" / "history" / flow_id
        history_dir.mkdir(parents=True)

        _write_jsonl(
            history_dir / "01_analyze_a.jsonl",
            [_mk_msg("user", "analyze", "2026-04-17T09:00:00")],
        )
        _write_jsonl(
            history_dir / "05_self_check_b.jsonl",
            [_mk_msg("user", "self_check", "2026-04-17T10:00:00")],
        )
        _write_jsonl(
            history_dir / "06_self_check_c.jsonl",
            [_mk_msg("user", "self_check", "2026-04-17T10:05:00")],
        )
        _write_jsonl(
            history_dir / "07_verify_spec_d.jsonl",
            [_mk_msg("user", "verify_spec", "2026-04-17T10:10:00")],
        )

        detail = _detail_from_history(project, flow_id)
        assert detail is not None
        steps = detail["steps"]
        assert len(steps) == 4

        # analyze has no pass metadata
        assert steps[0]["outputs"] == {}

        # First self_check gets pass_index=1
        assert steps[1]["outputs"]["self_check_pass_index"] == 1
        assert steps[1]["outputs"]["self_check_passes_required"] is not None

        # Second self_check gets pass_index=2
        assert steps[2]["outputs"]["self_check_pass_index"] == 2
        assert steps[2]["outputs"]["self_check_passes_required"] is not None

        # verify_spec resets the counter
        assert steps[3]["outputs"] == {}

    def test_self_check_pass_index_resets_after_non_self_check(self, project):
        """Consecutive self_check counter resets at non-self_check steps."""
        flow_id = "flow-sc-reset"
        history_dir = project / "se3" / "history" / flow_id
        history_dir.mkdir(parents=True)

        _write_jsonl(
            history_dir / "05_self_check_a.jsonl",
            [_mk_msg("user", "self_check", "2026-04-17T10:00:00")],
        )
        _write_jsonl(
            history_dir / "06_test_b.jsonl",
            [_mk_msg("user", "test", "2026-04-17T10:05:00")],
        )
        _write_jsonl(
            history_dir / "07_self_check_c.jsonl",
            [_mk_msg("user", "self_check", "2026-04-17T10:10:00")],
        )

        detail = _detail_from_history(project, flow_id)
        steps = detail["steps"]

        # First self_check pass_index=1
        assert steps[0]["outputs"]["self_check_pass_index"] == 1
        # test resets counter
        assert steps[1]["outputs"] == {}
        # Second self_check (after test) pass_index=1 again
        assert steps[2]["outputs"]["self_check_pass_index"] == 1


def _write_jsonl(path: Path, messages: list[dict]) -> None:
    lines = [json.dumps(m, ensure_ascii=False) for m in messages]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _mk_msg(
    role: str,
    step_type: str,
    timestamp: str,
    attempt: int = 0,
    content: str = "",
) -> dict:
    return {
        "role": role,
        "content": content or f"{role} at {timestamp}",
        "raw_json": [],
        "timestamp": timestamp,
        "step_type": step_type,
        "attempt": attempt,
    }


class TestShowDetailedSessionsInterleaving:
    """`_show_detailed_sessions` renders implement iterations interleaved
    with test / self_check sessions via `interleave_sessions_for_display`.

    Uses a spy on ``render_session_detailed`` to capture the rendered
    session order — avoids coupling to Rich console output formatting.
    """

    @staticmethod
    def _spy_renderables(monkeypatch):
        """Patch render_session_detailed to record calls and return nothing."""
        calls: list = []

        def fake(session, verbose=False):
            calls.append((session.step_id, session.step_type, len(session.messages)))
            return []

        monkeypatch.setattr(history_cmd, "render_session_detailed", fake)
        return calls

    def test_fix_loop_renders_iter_sessions_in_order(self, project, monkeypatch):
        flow_id = "flow-fix-loop"
        history_dir = project / "se3" / "history" / flow_id
        history_dir.mkdir(parents=True)

        # Multi-round implement: 3 iterations, each with a user prompt
        # separated by test session timestamps.
        _write_jsonl(
            history_dir / "04_implement_c.jsonl",
            [
                _mk_msg("user", "implement", "2026-04-17T10:00:00", attempt=0),
                _mk_msg("assistant", "implement", "2026-04-17T10:00:30", attempt=0),
                _mk_msg("user", "implement", "2026-04-17T10:06:00", attempt=1),
                _mk_msg("assistant", "implement", "2026-04-17T10:06:30", attempt=1),
                _mk_msg("user", "implement", "2026-04-17T10:12:00", attempt=2),
            ],
        )
        _write_jsonl(
            history_dir / "05_test_d.jsonl",
            [_mk_msg("user", "test", "2026-04-17T10:03:00")],
        )
        _write_jsonl(
            history_dir / "06_self_check_e.jsonl",
            [_mk_msg("user", "self_check", "2026-04-17T10:04:00")],
        )
        _write_jsonl(
            history_dir / "07_test_f.jsonl",
            [_mk_msg("user", "test", "2026-04-17T10:09:00")],
        )
        _write_jsonl(
            history_dir / "08_self_check_g.jsonl",
            [_mk_msg("user", "self_check", "2026-04-17T10:10:00")],
        )
        _write_jsonl(
            history_dir / "09_test_h.jsonl",
            [_mk_msg("user", "test", "2026-04-17T10:15:00")],
        )

        calls = self._spy_renderables(monkeypatch)

        _show_detailed_sessions(project, flow_id)

        rendered_ids = [c[0] for c in calls]
        assert rendered_ids == [
            "04_implement_c-iter1",
            "05_test_d",
            "06_self_check_e",
            "04_implement_c-iter2",
            "07_test_f",
            "08_self_check_g",
            "04_implement_c-iter3",
            "09_test_h",
        ]
        # Each virtual implement slice gets its own message subset.
        iter_counts = {
            step_id: msg_count
            for step_id, _type, msg_count in calls
            if step_id.startswith("04_implement_c-iter")
        }
        assert iter_counts == {
            "04_implement_c-iter1": 2,
            "04_implement_c-iter2": 2,
            "04_implement_c-iter3": 1,
        }

    def test_single_round_flow_renders_unchanged(self, project, monkeypatch):
        """A flow without fix-loop iterations keeps the original step_ids
        and renders the same set of sessions as before the G2 change.
        """
        flow_id = "flow-single-round"
        history_dir = project / "se3" / "history" / flow_id
        history_dir.mkdir(parents=True)

        _write_jsonl(
            history_dir / "01_analyze_a.jsonl",
            [_mk_msg("user", "analyze", "2026-04-17T09:00:00")],
        )
        _write_jsonl(
            history_dir / "04_implement_c.jsonl",
            [
                _mk_msg("user", "implement", "2026-04-17T10:00:00"),
                _mk_msg("assistant", "implement", "2026-04-17T10:00:30"),
            ],
        )
        _write_jsonl(
            history_dir / "05_test_d.jsonl",
            [_mk_msg("user", "test", "2026-04-17T10:05:00")],
        )

        calls = self._spy_renderables(monkeypatch)

        _show_detailed_sessions(project, flow_id)

        rendered_ids = [c[0] for c in calls]
        # No -iter suffix: single-iteration implement sessions pass through
        # unchanged (backward-compatible with pre-G2 rendering).
        assert rendered_ids == [
            "01_analyze_a",
            "04_implement_c",
            "05_test_d",
        ]

    def test_no_history_prints_notice(self, project, monkeypatch, capsys):
        flow_id = "no-history-flow"
        # Never create the history directory.

        calls = self._spy_renderables(monkeypatch)

        _show_detailed_sessions(project, flow_id)

        # Nothing rendered.
        assert calls == []

    def test_empty_implement_session_dropped(self, project, monkeypatch):
        """An empty implement jsonl produces no virtual sessions, and only
        the surviving non-implement sessions render.
        """
        flow_id = "flow-empty-impl"
        history_dir = project / "se3" / "history" / flow_id
        history_dir.mkdir(parents=True)

        # Empty-but-present implement file (zero messages after strip).
        (history_dir / "04_implement_empty.jsonl").write_text(
            "", encoding="utf-8"
        )
        _write_jsonl(
            history_dir / "05_test_d.jsonl",
            [_mk_msg("user", "test", "2026-04-17T10:05:00")],
        )

        calls = self._spy_renderables(monkeypatch)

        _show_detailed_sessions(project, flow_id)

        rendered_ids = [c[0] for c in calls]
        assert rendered_ids == ["05_test_d"]


class TestHistoryShowDetailedCliFixLoop:
    """End-to-end smoke test for the `se3 history show --detailed` CLI on a
    fix-loop flow.

    Builds a fixture flow with ``fix_iterations >= 2`` (engine.json +
    history directory) and invokes the CLI via ``CliRunner``. Asserts the
    rendered stdout contains each implement iteration as its own section,
    interleaved with test / self_check sections in timestamp order.
    """

    def test_fix_loop_cli_renders_iter_sessions_in_order(
        self, project, monkeypatch
    ):
        flow_id = "flow-cli-fix-loop"

        # Active-flow engine.json (so `history show` finds the flow via
        # get_flow_detail's first lookup path).
        flow_data = _make_flow_dict(
            flow_id, task="Fix something", status="completed"
        )
        state_file = project / "se3" / "state" / "engine.json"
        state_file.write_text(json.dumps(flow_data), encoding="utf-8")

        # history dir — 2 implement iterations, interleaved test/self_check.
        history_dir = project / "se3" / "history" / flow_id
        history_dir.mkdir(parents=True)

        _write_jsonl(
            history_dir / "04_implement_abc.jsonl",
            [
                _mk_msg("user", "implement", "2026-04-17T10:00:00", attempt=0),
                _mk_msg(
                    "assistant", "implement", "2026-04-17T10:00:30", attempt=0
                ),
                _mk_msg("user", "implement", "2026-04-17T10:06:00", attempt=1),
                _mk_msg(
                    "assistant", "implement", "2026-04-17T10:06:30", attempt=1
                ),
            ],
        )
        _write_jsonl(
            history_dir / "05_test_def.jsonl",
            [_mk_msg("user", "test", "2026-04-17T10:03:00")],
        )
        _write_jsonl(
            history_dir / "06_self_check_ghi.jsonl",
            [_mk_msg("user", "self_check", "2026-04-17T10:04:00")],
        )
        _write_jsonl(
            history_dir / "07_test_jkl.jsonl",
            [_mk_msg("user", "test", "2026-04-17T10:09:00")],
        )

        monkeypatch.setattr(
            history_cmd, "get_project_root", lambda: project
        )

        runner = CliRunner()
        result = runner.invoke(
            app,
            ["show", flow_id, "--detailed"],
            env={"COLUMNS": "200"},
        )

        assert result.exit_code == 0, result.output

        out = result.output

        # Both iteration headers appear, plus interleaved test/self_check.
        idx_iter1 = out.find("04_implement_abc-iter1")
        idx_test1 = out.find("05_test_def")
        idx_selfcheck = out.find("06_self_check_ghi")
        idx_iter2 = out.find("04_implement_abc-iter2")
        idx_test2 = out.find("07_test_jkl")

        assert idx_iter1 != -1, f"iter1 header missing:\n{out}"
        assert idx_iter2 != -1, f"iter2 header missing:\n{out}"
        assert idx_test1 != -1
        assert idx_selfcheck != -1
        assert idx_test2 != -1

        # Chronological order: iter1 → test1 → self_check → iter2 → test2.
        assert (
            idx_iter1 < idx_test1 < idx_selfcheck < idx_iter2 < idx_test2
        ), (
            f"Unexpected ordering (iter1={idx_iter1}, test1={idx_test1}, "
            f"self_check={idx_selfcheck}, iter2={idx_iter2}, "
            f"test2={idx_test2}) in output:\n{out}"
        )

        # Unsplit implement step_id must not leak through as its own section.
        assert "id: 04_implement_abc)" not in out, (
            "Un-split implement section leaked into output; split did not run."
        )
