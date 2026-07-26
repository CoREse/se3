"""Tests for record_group_status and get_step_history's skip of group_status.

Covers the DAG per-group status writer (G2): it must append a well-formed
single-line NDJSON record carrying the full schema, never raise on write
failure, and be skipped by get_step_history so the lightweight web-only
markers never leak into CLI history or retry-context construction.
"""

import json
from pathlib import Path

import pytest

from tianluo.engine.chat_history import (
    get_step_history,
    record_group_status,
    record_prompt,
    record_response,
)


@pytest.fixture
def tmp_project(tmp_path):
    """Create a temporary project directory with se3/history structure."""
    (tmp_path / "se3" / "history").mkdir(parents=True)
    return tmp_path


def _read_lines(project_root: Path, flow_id: str, step_id: str):
    path = (
        project_root / "se3" / "history" / flow_id / f"{step_id}.jsonl"
    )
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").strip().split("\n")
        if line.strip()
    ]


class TestRecordGroupStatus:
    def test_writes_valid_single_line_ndjson(self, tmp_project):
        record_group_status(
            tmp_project, "flow1", "01_implement_abcd", "implement", "G3", "running"
        )
        records = _read_lines(tmp_project, "flow1", "01_implement_abcd")
        assert len(records) == 1
        rec = records[0]
        assert rec["type"] == "group_status"
        assert rec["role"] == "system"
        assert rec["step_type"] == "implement"
        assert rec["group_id"] == "G3"
        assert rec["status"] == "running"
        assert "timestamp" in rec and rec["timestamp"]

    def test_explicit_timestamp_is_preserved(self, tmp_project):
        ts = "2026-06-01T12:00:00"
        record_group_status(
            tmp_project,
            "flow1",
            "01_implement_abcd",
            "implement",
            "G1",
            "completed",
            timestamp=ts,
        )
        rec = _read_lines(tmp_project, "flow1", "01_implement_abcd")[0]
        assert rec["timestamp"] == ts

    def test_appends_multiple_status_transitions(self, tmp_project):
        for gid, status in [
            ("G1", "queued"),
            ("G1", "running"),
            ("G1", "completed"),
        ]:
            record_group_status(
                tmp_project, "flow1", "01_implement_abcd", "implement", gid, status
            )
        records = _read_lines(tmp_project, "flow1", "01_implement_abcd")
        assert [r["status"] for r in records] == ["queued", "running", "completed"]

    def test_agent_only_field_written(self, tmp_project):
        record_group_status(
            tmp_project,
            "flow1",
            "01_implement_abcd",
            "implement",
            "G3",
            "running",
            agent_name="dclaude",
        )
        rec = _read_lines(tmp_project, "flow1", "01_implement_abcd")[0]
        assert rec["agent_name"] == "dclaude"
        # Model not known yet → no key, no empty placeholder.
        assert "model_name" not in rec

    def test_agent_and_model_fields_written(self, tmp_project):
        record_group_status(
            tmp_project,
            "flow1",
            "01_implement_abcd",
            "implement",
            "G3",
            "running",
            agent_name="dclaude",
            model_name="claude-opus-4-8",
        )
        rec = _read_lines(tmp_project, "flow1", "01_implement_abcd")[0]
        assert rec["agent_name"] == "dclaude"
        assert rec["model_name"] == "claude-opus-4-8"

    def test_all_none_record_is_byte_identical_to_legacy(self, tmp_project):
        """With both optional fields defaulting to None the written record's
        key set stays byte-identical to the pre-extension schema, so legacy
        readers are unaffected."""
        ts = "2026-06-01T12:00:00"
        record_group_status(
            tmp_project,
            "flow1",
            "01_implement_abcd",
            "implement",
            "G1",
            "queued",
            timestamp=ts,
        )
        rec = _read_lines(tmp_project, "flow1", "01_implement_abcd")[0]
        assert rec == {
            "type": "group_status",
            "role": "system",
            "step_type": "implement",
            "group_id": "G1",
            "status": "queued",
            "timestamp": ts,
        }

    def test_agent_model_lines_still_skipped_by_get_step_history(self, tmp_project):
        record_group_status(
            tmp_project,
            "flow1",
            "01_implement_abcd",
            "implement",
            "G1",
            "running",
            agent_name="dclaude",
            model_name="claude-opus-4-8",
        )
        session = get_step_history(tmp_project, "flow1", "01_implement_abcd")
        # group_status lines (even enriched ones) never become ChatMessages.
        assert session is None

    def test_write_failure_does_not_raise(self, tmp_project, monkeypatch):
        # Force the open() inside record_group_status to raise OSError; the
        # function must swallow it (logger.warning) rather than propagate.
        import tianluo.engine.chat_history as ch

        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(Path, "open", boom)
        # Should not raise.
        record_group_status(
            tmp_project, "flow1", "01_implement_abcd", "implement", "G2", "failed"
        )


class TestGetStepHistorySkipsGroupStatus:
    def test_group_status_lines_are_skipped(self, tmp_project):
        record_prompt(
            tmp_project, "flow1", "01_implement_abcd", "implement", "do work", 0
        )
        record_group_status(
            tmp_project, "flow1", "01_implement_abcd", "implement", "G1", "running"
        )
        record_response(
            tmp_project, "flow1", "01_implement_abcd", "implement", "", 0
        )
        record_group_status(
            tmp_project, "flow1", "01_implement_abcd", "implement", "G1", "completed"
        )

        session = get_step_history(tmp_project, "flow1", "01_implement_abcd")
        assert session is not None
        # Only the user prompt + assistant response remain; no group_status.
        roles = [m.role for m in session.messages]
        assert roles == ["user", "assistant"]
        assert all(
            getattr(m, "status", None) is None for m in session.messages
        )

    def test_only_group_status_yields_no_session(self, tmp_project):
        record_group_status(
            tmp_project, "flow1", "01_implement_abcd", "implement", "G1", "queued"
        )
        session = get_step_history(tmp_project, "flow1", "01_implement_abcd")
        # No ChatMessages → no session.
        assert session is None
