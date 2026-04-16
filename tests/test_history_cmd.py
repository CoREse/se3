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

from se3.commands.history_cmd import (
    get_flow_detail,
    _load_archived_flow,
    _detail_from_history,
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
