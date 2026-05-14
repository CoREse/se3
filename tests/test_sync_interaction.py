"""Tests for SyncInteractionHandler — high-impact deletion approval flow.

Under the one-directional sync model, the only kind of human gating is
``high-impact deletion`` (a whole ``### Requirement:`` block removal).
This module verifies:

* ``HighImpactDeletion`` data class
* MCP call file generation (type=``sync_high_impact_deletion``)
* Response file parsing (approve/skip, item_id and numeric id matching)
* Terminal interactive input (minimal coverage via mocked stdin)
* File-polling path (response file picked up while the loop runs)
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from se3.engine.sync_interaction import (
    HighImpactDeletion,
    SyncInteractionHandler,
    prompt_resume_or_exit,
)


def _make_items(n: int = 2):
    """Helper: build ``n`` ``HighImpactDeletion`` items with unique ids."""
    return [
        HighImpactDeletion(
            item_id=f"del_spec_{i}_abc{i:02d}",
            spec_name=f"spec_{i}",
            requirement_name=f"Requirement {i}",
            requirement_excerpt=f"Excerpt for item {i}",
        )
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# Data class
# ---------------------------------------------------------------------------

class TestHighImpactDeletion:
    def test_to_dict_roundtrip(self):
        item = HighImpactDeletion(
            item_id="x",
            spec_name="auth",
            requirement_name="Login",
            requirement_excerpt="Excerpt",
        )
        d = item.to_dict()
        assert d["item_id"] == "x"
        assert d["spec_name"] == "auth"
        assert d["requirement_name"] == "Login"
        assert d["requirement_excerpt"] == "Excerpt"


# ---------------------------------------------------------------------------
# Call file generation
# ---------------------------------------------------------------------------

class TestCallFileGeneration:
    def test_generates_high_impact_deletion_call_file(self, tmp_path):
        items = _make_items(2)
        handler = SyncInteractionHandler(tmp_path, items)
        call_file = handler.generate_pending_call_file()

        assert call_file.exists()
        data = json.loads(call_file.read_text())
        assert data["type"] == "sync_high_impact_deletion"
        assert len(data["items"]) == 2
        assert data["items"][0]["item_id"] == items[0].item_id
        assert data["items"][0]["options"] == ["approve", "skip"]

    def test_call_file_in_calls_dir(self, tmp_path):
        handler = SyncInteractionHandler(tmp_path, _make_items(1))
        call_file = handler.generate_pending_call_file()
        assert "se3/calls" in str(call_file)
        assert call_file.name.startswith("sync_deletion_")


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

class TestResponseParsing:
    def test_parse_by_item_id(self, tmp_path):
        items = _make_items(2)
        handler = SyncInteractionHandler(tmp_path, items)
        call_file = handler.generate_pending_call_file()

        response_data = {
            "items": [
                {"item_id": items[0].item_id, "decision": "approve"},
                {"item_id": items[1].item_id, "decision": "skip"},
            ]
        }
        response_path = Path(str(call_file) + ".response")
        response_path.write_text(json.dumps(response_data))

        decisions = handler._parse_response_file(response_path)
        assert decisions[items[0].item_id] == "approve"
        assert decisions[items[1].item_id] == "skip"

    def test_parse_by_numeric_id(self, tmp_path):
        items = _make_items(2)
        handler = SyncInteractionHandler(tmp_path, items)
        handler.generate_pending_call_file()

        response_data = {
            "items": [
                {"id": 1, "decision": "approve"},
                {"id": 2, "decision": "skip"},
            ]
        }
        response_path = tmp_path / "resp.json"
        response_path.write_text(json.dumps(response_data))

        decisions = handler._parse_response_file(response_path)
        assert decisions[items[0].item_id] == "approve"
        assert decisions[items[1].item_id] == "skip"

    def test_parse_invalid_decision_dropped(self, tmp_path):
        items = _make_items(2)
        handler = SyncInteractionHandler(tmp_path, items)
        handler.generate_pending_call_file()

        response_data = {
            "items": [
                {"item_id": items[0].item_id, "decision": "approve"},
                {"item_id": items[1].item_id, "decision": "garbage"},
            ]
        }
        response_path = tmp_path / "resp.json"
        response_path.write_text(json.dumps(response_data))

        decisions = handler._parse_response_file(response_path)
        # garbage decision is dropped from explicit entries; safe default
        # ("skip") fills missing item_ids so the handler can resolve.
        assert decisions[items[0].item_id] == "approve"
        assert decisions[items[1].item_id] == "skip"

    def test_parse_empty_returns_none(self, tmp_path):
        handler = SyncInteractionHandler(tmp_path, _make_items(1))
        response_path = tmp_path / "resp.json"
        response_path.write_text(json.dumps({"items": []}))
        assert handler._parse_response_file(response_path) is None

    def test_parse_bad_json_returns_none(self, tmp_path):
        handler = SyncInteractionHandler(tmp_path, _make_items(1))
        response_path = tmp_path / "resp.json"
        response_path.write_text("not json")
        assert handler._parse_response_file(response_path) is None


# ---------------------------------------------------------------------------
# Collect decisions — file-polling path
# ---------------------------------------------------------------------------

class TestFilePollingPath:
    def test_response_file_picked_up_while_polling(self, tmp_path):
        """Write a .response file from a background thread and ensure
        ``collect_decisions`` returns the parsed decisions."""
        items = _make_items(1)
        handler = SyncInteractionHandler(
            tmp_path, items, use_terminal=False
        )

        def write_response_after_delay():
            # Wait until the call file exists before writing the response.
            for _ in range(50):
                cp = handler._call_file_path
                if cp is not None:
                    response_path = Path(str(cp) + ".response")
                    response_path.write_text(json.dumps({
                        "items": [
                            {"item_id": items[0].item_id, "decision": "approve"},
                        ]
                    }))
                    return
                time.sleep(0.05)

        t = threading.Thread(target=write_response_after_delay, daemon=True)
        t.start()
        decisions = handler.collect_decisions()
        t.join(timeout=5)

        assert decisions == {items[0].item_id: "approve"}

    def test_no_pending_items_returns_empty(self, tmp_path):
        handler = SyncInteractionHandler(tmp_path, [])
        assert handler.collect_decisions() == {}


# ---------------------------------------------------------------------------
# prompt_resume_or_exit — TTY with EOF returns 'exit'
# ---------------------------------------------------------------------------

class TestPromptResumeOrExit:
    def test_tty_eof_returns_exit_and_writes_stderr(self):
        """When stdin is a TTY but readline() returns '' (EOF), the gate
        must return 'exit' and write guidance to stderr.

        This guards against the regression where a non-interactive pipeline
        (stdin closed) would see a falsy line and loop forever, burning
        LLM calls."""
        mock_stdin = MagicMock()
        mock_stdin.isatty.return_value = True
        mock_stdin.readline.return_value = ""

        mock_stdout = MagicMock()
        mock_stderr = MagicMock()

        with patch("se3.engine.sync_interaction.sys.stdin", mock_stdin):
            with patch("se3.engine.sync_interaction.sys.stdout", mock_stdout):
                with patch("se3.engine.sync_interaction.sys.stderr", mock_stderr):
                    result = prompt_resume_or_exit({
                        "completed_specs": 3,
                        "total_specs": 5,
                        "round_index": 2,
                        "max_rounds": 10,
                        "in_sync_specs": ["base"],
                        "failure_count": 3,
                        "reason": "quota_exhausted",
                        "checkpoint_path": "/tmp/cp.json",
                    })

        assert result == "exit"
        mock_stdin.readline.assert_called_once()
        # stderr should contain the EOF-specific guidance
        stderr_writes = "".join(call[0][0] for call in mock_stderr.write.call_args_list)
        assert "stdin closed (EOF)" in stderr_writes
        assert "--resume" in stderr_writes
