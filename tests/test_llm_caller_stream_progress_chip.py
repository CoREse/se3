"""Tests for the single-chip stream_progress protocol carried over
``StreamJSONTracker._emit_progress``.

Each tool call produces exactly two ``stream_progress`` records: an
in-flight one on the ``tool_use`` event (``tool_detail`` absent because the
in-flight chip has no detail panel yet) and a terminal one on the
``tool_result`` event (carrying ``is_error`` and a structured
``tool_detail`` payload). No third ``[<preview>]`` chip is emitted on
tool_result.
"""

from __future__ import annotations

import json

import pytest

from tianluo.engine import chat_history
from tianluo.engine.llm_caller import StreamJSONTracker


def _make_tracker(monkeypatch, tmp_path):
    """Build a tracker that captures every ``record_stream_progress`` call."""
    captured = []

    def fake_record_stream_progress(
        project_root,
        flow_id,
        step_id,
        step_type,
        content,
        raw_obj,
        attempt,
        timestamp=None,
        *,
        tool_use_id=None,
        is_error=None,
        tool_detail=None,
    ):
        captured.append(
            {
                "content": content,
                "raw_obj": raw_obj,
                "attempt": attempt,
                "tool_use_id": tool_use_id,
                "is_error": is_error,
                "tool_detail": tool_detail,
            }
        )

    monkeypatch.setattr(
        chat_history, "record_stream_progress", fake_record_stream_progress
    )
    tracker = StreamJSONTracker(
        project_root=tmp_path,
        flow_id="flow-chip",
        step_id="step-chip",
        step_type="implement",
        attempt=0,
    )
    return tracker, captured


def _tool_records(captured):
    """Return only the records that carry a tool_use_id (filter out any
    narrative text progress entries)."""
    return [c for c in captured if c["tool_use_id"]]


# ---------------------------------------------------------------------------
# Read — success
# ---------------------------------------------------------------------------


def test_read_success_emits_in_flight_and_terminal(monkeypatch, tmp_path):
    tracker, captured = _make_tracker(monkeypatch, tmp_path)

    tracker.process_line(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu-read-1",
                            "name": "Read",
                            "input": {"file_path": "src/foo.py", "offset": 0, "limit": 200},
                        }
                    ]
                },
            }
        )
    )
    tracker.process_line(
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu-read-1",
                            "content": "l1\nl2\nl3",
                            "is_error": False,
                        }
                    ]
                },
            }
        )
    )

    recs = _tool_records(captured)
    assert len(recs) == 2, f"expected 2 chip records, got {len(recs)}: {recs!r}"

    in_flight, terminal = recs

    # In-flight: tool_use_id set, no is_error, no tool_detail.
    assert in_flight["tool_use_id"] == "tu-read-1"
    assert in_flight["is_error"] is None
    assert in_flight["tool_detail"] is None
    assert in_flight["content"].startswith("[Read")
    assert "✓" not in in_flight["content"]
    assert "✗" not in in_flight["content"]

    # Terminal: success chip header with ✓, structured detail.
    assert terminal["tool_use_id"] == "tu-read-1"
    assert terminal["is_error"] is False
    assert terminal["content"].startswith("[Read ✓")
    assert terminal["tool_detail"] is not None
    assert terminal["tool_detail"]["kind"] == "read_text"
    assert terminal["tool_detail"]["file_path"] == "src/foo.py"
    # start_line is offset+1, but offset=0 means start at line 1.
    assert terminal["tool_detail"]["start_line"] == 1
    assert "l1" in terminal["tool_detail"]["text"]


# ---------------------------------------------------------------------------
# Edit — success (edit_diff with hunk start line numbers)
# ---------------------------------------------------------------------------


def test_edit_success_terminal_has_edit_diff_detail_with_hunk_start(
    monkeypatch, tmp_path
):
    tracker, captured = _make_tracker(monkeypatch, tmp_path)

    old_string = "line one\nline two\nline three\nline four\nline five\n"
    new_string = "line one\nline two\nLINE THREE CHANGED\nline four\nline five\n"

    tracker.process_line(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu-edit-1",
                            "name": "Edit",
                            "input": {
                                "file_path": "src/bar.py",
                                "old_string": old_string,
                                "new_string": new_string,
                            },
                        }
                    ]
                },
            }
        )
    )
    tracker.process_line(
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu-edit-1",
                            "content": "edit applied",
                            "is_error": False,
                        }
                    ]
                },
            }
        )
    )

    recs = _tool_records(captured)
    assert len(recs) == 2

    in_flight, terminal = recs

    # In-flight emits an [Edit:...] chip with the line-count summary but no detail.
    assert in_flight["tool_use_id"] == "tu-edit-1"
    assert in_flight["content"].startswith("[Edit")
    assert in_flight["tool_detail"] is None
    assert in_flight["is_error"] is None

    # Terminal carries the structured edit_diff payload.
    assert terminal["tool_use_id"] == "tu-edit-1"
    assert terminal["is_error"] is False
    assert terminal["content"].startswith("[Edit ✓")
    detail = terminal["tool_detail"]
    assert detail is not None
    assert detail["kind"] == "edit_diff"
    assert detail["file_path"] == "src/bar.py"
    # The diff carries at least one hunk header — verify hunk start line numbers
    # were parsed from it.
    assert detail["old_start_line"] is not None and detail["old_start_line"] >= 1
    assert detail["new_start_line"] is not None and detail["new_start_line"] >= 1
    assert "@@" in detail["diff"]


# ---------------------------------------------------------------------------
# Bash — failure
# ---------------------------------------------------------------------------


def test_bash_failure_emits_terminal_with_is_error_true(monkeypatch, tmp_path):
    tracker, captured = _make_tracker(monkeypatch, tmp_path)

    tracker.process_line(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu-bash-1",
                            "name": "Bash",
                            "input": {"command": "false"},
                        }
                    ]
                },
            }
        )
    )
    tracker.process_line(
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu-bash-1",
                            "content": "command failed: exit 1",
                            "is_error": True,
                        }
                    ]
                },
            }
        )
    )

    recs = _tool_records(captured)
    assert len(recs) == 2

    in_flight, terminal = recs

    assert in_flight["tool_use_id"] == "tu-bash-1"
    assert in_flight["content"].startswith("[Bash")
    assert in_flight["is_error"] is None
    assert in_flight["tool_detail"] is None

    assert terminal["tool_use_id"] == "tu-bash-1"
    assert terminal["is_error"] is True
    assert terminal["content"].startswith("[Bash ✗")
    detail = terminal["tool_detail"]
    assert detail is not None
    assert detail["kind"] == "bash_output"
    assert detail["command"] == "false"


# ---------------------------------------------------------------------------
# CLI stdout byte-equivalence — emoji-prefixed lines are unchanged
# ---------------------------------------------------------------------------


def test_cli_stdout_emoji_lines_preserved(monkeypatch, tmp_path, capsys):
    tracker, _captured = _make_tracker(monkeypatch, tmp_path)

    tracker.process_line(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "tu-cli-1",
                            "name": "Read",
                            "input": {"file_path": "x.py"},
                        }
                    ]
                },
            }
        )
    )
    tracker.process_line(
        json.dumps(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tu-cli-1",
                            "content": "abc\n",
                            "is_error": False,
                        }
                    ]
                },
            }
        )
    )
    out = capsys.readouterr().out
    assert "[llm-stream] 🔧 Read:" in out
    assert "[llm-stream] ✅ Read" in out


# ---------------------------------------------------------------------------
# Byte-identical jsonl when new fields absent (narrative text path)
# ---------------------------------------------------------------------------


def test_narrative_text_progress_jsonl_byte_identical_to_legacy(tmp_path):
    """A narrative-text progress write (no tool_use_id / is_error / tool_detail)
    must produce a record whose key set matches the pre-extension schema
    exactly — no extra keys leak through."""
    chat_history.record_stream_progress(
        tmp_path,
        "flow-byte",
        "01_discovery_abc12345",
        "discovery",
        "narrative chunk",
        None,
        attempt=0,
    )
    path = (
        tmp_path
        / "se3"
        / "history"
        / "flow-byte"
        / "01_discovery_abc12345.jsonl"
    )
    rec = json.loads(path.read_text(encoding="utf-8").strip())
    assert set(rec.keys()) == {
        "type",
        "role",
        "step_type",
        "content",
        "raw_json",
        "timestamp",
        "attempt",
        "partial",
    }


def test_record_stream_progress_writes_new_chip_fields_when_provided(tmp_path):
    """When the new chip fields are passed, they round-trip into the jsonl
    record so the daemon reader can forward them to the frontend."""
    detail = {"kind": "read_text", "file_path": "a.py", "text": "x", "start_line": 1, "truncated": False}
    chat_history.record_stream_progress(
        tmp_path,
        "flow-chip-fields",
        "01_implement_abc12345",
        "implement",
        "[Read ✓ a.py · 1 lines]",
        None,
        attempt=0,
        tool_use_id="tu-1",
        is_error=False,
        tool_detail=detail,
    )
    path = (
        tmp_path
        / "se3"
        / "history"
        / "flow-chip-fields"
        / "01_implement_abc12345.jsonl"
    )
    rec = json.loads(path.read_text(encoding="utf-8").strip())
    assert rec["tool_use_id"] == "tu-1"
    assert rec["is_error"] is False
    assert rec["tool_detail"] == detail
