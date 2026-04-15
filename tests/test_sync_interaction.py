"""Unit tests for SyncInteractionHandler — terminal path, file polling, concurrency."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from se3.engine.sync_engine import (
    Conflict,
    DiffType,
    PendingDecision,
    SpecAnalysis,
    SpecDiff,
    SyncEngine,
    SyncResult,
)
from se3.engine.sync_interaction import SyncInteractionHandler


def _make_pending(n=3):
    """Create a list of PendingDecision items for testing."""
    items = []
    for i in range(n):
        pd_type = "gap" if i % 2 == 0 else "conflict"
        items.append(
            PendingDecision(
                type=pd_type,
                item_id=f"item_{i}",
                spec_name=f"spec_{i}",
                description=f"Description for item {i}",
                diff=f"src/module_{i}.py:10",
                confidence="low",
                decision="pending",
            )
        )
    return items


# ---------------------------------------------------------------------------
# Call file generation and response parsing
# ---------------------------------------------------------------------------

class TestCallFileGeneration:
    def test_generates_call_file(self, tmp_path):
        items = _make_pending(2)
        handler = SyncInteractionHandler(tmp_path, items)
        call_file = handler._generate_pending_call_file()

        assert call_file.exists()
        data = json.loads(call_file.read_text())
        assert data["type"] == "sync_pending_decisions"
        assert len(data["items"]) == 2
        assert data["items"][0]["item_id"] == "item_0"
        assert data["items"][0]["options"] == ["update_spec", "create_issue"]

    def test_call_file_in_calls_dir(self, tmp_path):
        handler = SyncInteractionHandler(tmp_path, _make_pending(1))
        call_file = handler._generate_pending_call_file()

        assert "se3/calls" in str(call_file)
        assert call_file.name.startswith("sync_pending_")

    def test_parse_response_by_item_id(self, tmp_path):
        items = _make_pending(2)
        handler = SyncInteractionHandler(tmp_path, items)
        call_file = handler._generate_pending_call_file()

        response_data = {
            "items": [
                {"item_id": "item_0", "decision": "update_spec"},
                {"item_id": "item_1", "decision": "create_issue"},
            ]
        }
        response_path = Path(str(call_file) + ".response")
        response_path.write_text(json.dumps(response_data))

        decisions = handler._parse_response_file(response_path)
        assert decisions == {"item_0": "update_spec", "item_1": "create_issue"}

    def test_parse_response_by_numeric_id(self, tmp_path):
        items = _make_pending(2)
        handler = SyncInteractionHandler(tmp_path, items)
        handler._generate_pending_call_file()

        response_data = {
            "items": [
                {"id": 1, "decision": "update_spec"},
                {"id": 2, "decision": "create_issue"},
            ]
        }
        response_path = tmp_path / "resp.json"
        response_path.write_text(json.dumps(response_data))

        decisions = handler._parse_response_file(response_path)
        assert decisions == {"item_0": "update_spec", "item_1": "create_issue"}

    def test_parse_response_skips_invalid_decision(self, tmp_path):
        items = _make_pending(2)
        handler = SyncInteractionHandler(tmp_path, items)
        handler._generate_pending_call_file()

        response_data = {
            "items": [
                {"item_id": "item_0", "decision": "invalid_value"},
                {"item_id": "item_1", "decision": "update_spec"},
            ]
        }
        response_path = tmp_path / "resp.json"
        response_path.write_text(json.dumps(response_data))

        decisions = handler._parse_response_file(response_path)
        assert decisions == {"item_1": "update_spec"}

    def test_parse_response_returns_none_for_empty(self, tmp_path):
        items = _make_pending(1)
        handler = SyncInteractionHandler(tmp_path, items)

        response_path = tmp_path / "resp.json"
        response_path.write_text(json.dumps({"items": []}))

        assert handler._parse_response_file(response_path) is None

    def test_parse_response_returns_none_for_bad_json(self, tmp_path):
        items = _make_pending(1)
        handler = SyncInteractionHandler(tmp_path, items)

        response_path = tmp_path / "resp.json"
        response_path.write_text("not json")

        assert handler._parse_response_file(response_path) is None


# ---------------------------------------------------------------------------
# Terminal path (Path A) — mocked stdin
# ---------------------------------------------------------------------------

class TestTerminalPath:
    def test_single_item_decision(self, tmp_path):
        items = _make_pending(1)
        handler = SyncInteractionHandler(tmp_path, items)

        with patch("builtins.input", side_effect=["1:1", "done"]):
            decisions = handler._collect_terminal_input()

        assert decisions == {"item_0": "update_spec"}

    def test_multiple_item_decisions(self, tmp_path):
        items = _make_pending(3)
        handler = SyncInteractionHandler(tmp_path, items)

        with patch("builtins.input", side_effect=["1:1", "2:2", "3:1", "done"]):
            decisions = handler._collect_terminal_input()

        assert decisions == {
            "item_0": "update_spec",
            "item_1": "create_issue",
            "item_2": "update_spec",
        }

    def test_batch_all_update_spec(self, tmp_path):
        items = _make_pending(3)
        handler = SyncInteractionHandler(tmp_path, items)

        with patch("builtins.input", side_effect=["all:1"]):
            decisions = handler._collect_terminal_input()

        assert len(decisions) == 3
        assert all(d == "update_spec" for d in decisions.values())

    def test_batch_all_create_issue(self, tmp_path):
        items = _make_pending(2)
        handler = SyncInteractionHandler(tmp_path, items)

        with patch("builtins.input", side_effect=["all:2"]):
            decisions = handler._collect_terminal_input()

        assert all(d == "create_issue" for d in decisions.values())

    def test_done_defaults_remaining_to_create_issue(self, tmp_path):
        items = _make_pending(3)
        handler = SyncInteractionHandler(tmp_path, items)

        with patch("builtins.input", side_effect=["1:1", "done"]):
            decisions = handler._collect_terminal_input()

        assert decisions["item_0"] == "update_spec"
        assert decisions["item_1"] == "create_issue"
        assert decisions["item_2"] == "create_issue"

    def test_auto_completes_when_all_resolved(self, tmp_path):
        items = _make_pending(2)
        handler = SyncInteractionHandler(tmp_path, items)

        with patch("builtins.input", side_effect=["1:1", "2:2"]):
            decisions = handler._collect_terminal_input()

        assert decisions == {"item_0": "update_spec", "item_1": "create_issue"}

    def test_invalid_decision_value_rejected(self, tmp_path, capsys):
        items = _make_pending(1)
        handler = SyncInteractionHandler(tmp_path, items)

        with patch("builtins.input", side_effect=["1:3", "1:1", "done"]):
            decisions = handler._collect_terminal_input()

        assert decisions["item_0"] == "update_spec"

    def test_invalid_item_number_rejected(self, tmp_path, capsys):
        items = _make_pending(2)
        handler = SyncInteractionHandler(tmp_path, items)

        with patch("builtins.input", side_effect=["5:1", "1:2", "done"]):
            decisions = handler._collect_terminal_input()

        assert decisions["item_0"] == "create_issue"
        assert decisions["item_1"] == "create_issue"

    def test_eof_returns_none(self, tmp_path):
        items = _make_pending(1)
        handler = SyncInteractionHandler(tmp_path, items)

        with patch("builtins.input", side_effect=EOFError):
            decisions = handler._collect_terminal_input()

        assert decisions is None

    def test_empty_input_ignored(self, tmp_path):
        items = _make_pending(1)
        handler = SyncInteractionHandler(tmp_path, items)

        with patch("builtins.input", side_effect=["", "all:1"]):
            decisions = handler._collect_terminal_input()

        assert decisions["item_0"] == "update_spec"

    def test_invalid_format_rejected(self, tmp_path, capsys):
        items = _make_pending(1)
        handler = SyncInteractionHandler(tmp_path, items)

        with patch("builtins.input", side_effect=["garbage", "1:1", "done"]):
            decisions = handler._collect_terminal_input()

        assert decisions["item_0"] == "update_spec"


# ---------------------------------------------------------------------------
# File polling path (Path B)
# ---------------------------------------------------------------------------

class TestFilePollingPath:
    def test_detects_new_response_file(self, tmp_path):
        items = _make_pending(2)
        handler = SyncInteractionHandler(tmp_path, items)
        call_file = handler._generate_pending_call_file()
        handler._call_file_path = call_file

        response_data = {
            "items": [
                {"item_id": "item_0", "decision": "update_spec"},
                {"item_id": "item_1", "decision": "create_issue"},
            ]
        }

        def write_response_after_delay():
            time.sleep(0.3)
            response_path = Path(str(call_file) + ".response")
            response_path.write_text(json.dumps(response_data))

        writer = threading.Thread(target=write_response_after_delay)
        writer.start()

        handler._file_watch_path(call_file)
        writer.join()

        assert handler._done_event.is_set()
        assert handler._decisions == {"item_0": "update_spec", "item_1": "create_issue"}

    def test_stops_on_stop_event(self, tmp_path):
        items = _make_pending(1)
        handler = SyncInteractionHandler(tmp_path, items)
        call_file = handler._generate_pending_call_file()

        def stop_after_delay():
            time.sleep(0.3)
            handler._stop_event.set()

        stopper = threading.Thread(target=stop_after_delay)
        stopper.start()

        handler._file_watch_path(call_file)
        stopper.join()

        assert not handler._done_event.is_set()

    def test_detects_preexisting_response(self, tmp_path):
        items = _make_pending(1)
        handler = SyncInteractionHandler(tmp_path, items)
        call_file = handler._generate_pending_call_file()

        response_path = Path(str(call_file) + ".response")
        response_path.write_text(json.dumps({"items": [{"item_id": "item_0", "decision": "update_spec"}]}))

        handler._file_watch_path(call_file)

        assert handler._done_event.is_set()
        assert handler._decisions.get("item_0") == "update_spec"


# ---------------------------------------------------------------------------
# Concurrent coordination (both paths)
# ---------------------------------------------------------------------------

class TestConcurrentCoordination:
    def test_terminal_path_wins(self, tmp_path):
        items = _make_pending(2)
        handler = SyncInteractionHandler(tmp_path, items)

        with patch("builtins.input", side_effect=["all:1"]):
            with patch.object(handler, "_render_pending_items"):
                decisions = handler.collect_decisions()

        assert len(decisions) == 2
        assert all(d == "update_spec" for d in decisions.values())

    def test_file_path_wins(self, tmp_path):
        items = _make_pending(2)
        handler = SyncInteractionHandler(tmp_path, items)

        def block_terminal():
            handler._stop_event.wait()

        with patch("builtins.input", side_effect=block_terminal):
            with patch.object(handler, "_render_pending_items"):

                def write_response():
                    time.sleep(0.3)
                    call_file = handler._call_file_path
                    response_path = Path(str(call_file) + ".response")
                    response_data = {
                        "items": [
                            {"item_id": "item_0", "decision": "create_issue"},
                            {"item_id": "item_1", "decision": "create_issue"},
                        ]
                    }
                    response_path.write_text(json.dumps(response_data))

                writer = threading.Thread(target=write_response)
                writer.start()
                decisions = handler.collect_decisions()
                writer.join()

        assert decisions == {"item_0": "create_issue", "item_1": "create_issue"}

    def test_empty_pending_returns_empty(self, tmp_path):
        handler = SyncInteractionHandler(tmp_path, [])
        decisions = handler.collect_decisions()
        assert decisions == {}

    def test_keyboard_interrupt_handled(self, tmp_path):
        items = _make_pending(1)
        handler = SyncInteractionHandler(tmp_path, items)

        with patch.object(handler, "_render_pending_items"):
            with patch.object(
                handler, "_done_event", wraps=handler._done_event
            ) as mock_event:
                original_wait = handler._done_event.wait

                call_count = 0

                def interrupt_on_wait(timeout=None):
                    nonlocal call_count
                    call_count += 1
                    if call_count >= 2:
                        raise KeyboardInterrupt
                    return original_wait(timeout)

                mock_event.wait = interrupt_on_wait

                with patch("builtins.input", side_effect=lambda *a: time.sleep(10)):
                    with pytest.raises(KeyboardInterrupt):
                        handler.collect_decisions()

        assert handler._stop_event.is_set()

    def test_both_paths_produce_equivalent_results(self, tmp_path):
        items = _make_pending(2)

        handler_a = SyncInteractionHandler(tmp_path, items)
        with patch("builtins.input", side_effect=["1:1", "2:2"]):
            decisions_a = handler_a._collect_terminal_input()

        handler_b = SyncInteractionHandler(tmp_path, items)
        call_file = handler_b._generate_pending_call_file()
        response_data = {
            "items": [
                {"item_id": "item_0", "decision": "update_spec"},
                {"item_id": "item_1", "decision": "create_issue"},
            ]
        }
        response_path = Path(str(call_file) + ".response")
        response_path.write_text(json.dumps(response_data))

        decisions_b = handler_b._parse_response_file(response_path)

        assert decisions_a == decisions_b

    def test_response_file_written_on_terminal_completion(self, tmp_path):
        items = _make_pending(2)
        handler = SyncInteractionHandler(tmp_path, items)

        with patch("builtins.input", side_effect=["all:2"]):
            with patch.object(handler, "_render_pending_items"):
                decisions = handler.collect_decisions()

        assert handler._call_file_path is not None
        response_path = Path(str(handler._call_file_path) + ".response")
        assert response_path.exists()
        resp_data = json.loads(response_path.read_text())
        assert len(resp_data["items"]) == 2
        assert all(i["decision"] == "create_issue" for i in resp_data["items"])


# ---------------------------------------------------------------------------
# SyncEngine integration
# ---------------------------------------------------------------------------

class TestSyncEngineIntegration:
    def test_interact_called_when_pending(self, tmp_path):
        """When pending decisions exist, _interact_for_decisions is called."""
        engine = SyncEngine(tmp_path, mode="strict")

        pending = [
            PendingDecision(
                type="gap",
                item_id="gap_1",
                spec_name="auth",
                description="Missing feature",
                decision="pending",
            )
        ]

        mock_handler_instance = MagicMock()
        mock_handler_instance.collect_decisions.return_value = {"gap_1": "create_issue"}

        with patch("se3.engine.sync_interaction.SyncInteractionHandler", return_value=mock_handler_instance) as mock_cls:
            result = engine._interact_for_decisions(pending, MagicMock())

        mock_cls.assert_called_once_with(tmp_path, pending)
        mock_handler_instance.collect_decisions.assert_called_once()

    def test_interact_not_called_when_empty(self, tmp_path):
        """_interact_for_decisions with empty list still works."""
        engine = SyncEngine(tmp_path)

        with patch("se3.engine.sync_interaction.SyncInteractionHandler") as mock_cls:
            mock_cls.return_value.collect_decisions.return_value = {}
            result = engine._interact_for_decisions([], MagicMock())

        mock_cls.assert_called_once()
        assert result["specs_updated"] == 0
        assert result["issues_created"] == 0

    def test_run_integrates_pending_decisions(self, tmp_path):
        """Full run() with pending decisions calls _interact_for_decisions when tty."""
        spec_dir = tmp_path / "se3" / "specs" / "test_spec"
        spec_dir.mkdir(parents=True, exist_ok=True)
        (spec_dir / "spec.md").write_text("# Spec\n## Purpose\nTest.")

        engine = SyncEngine(tmp_path, mode="strict")

        mock_interact = MagicMock(return_value={"specs_updated": 0, "issues_created": 1})

        with patch.object(engine, "_interact_for_decisions", mock_interact):
            with patch("sys.stdin") as mock_stdin:
                mock_stdin.isatty.return_value = True
                with patch("se3.engine.llm_caller.LLMCaller"):
                    with patch("se3.engine.project_context.ProjectContextCollector") as mock_ctx:
                        mock_ctx.return_value.collect.return_value = {}
                        with patch("se3.engine.sync_analyzer.SyncAnalyzer") as mock_analyzer:
                            mock_analysis = SpecAnalysis(
                                spec_name="test_spec",
                                diffs=[SpecDiff(DiffType.GAP, "test_spec", "Missing feature")],
                            )
                            mock_analyzer.return_value.analyze_spec.return_value = mock_analysis
                            result = engine.run()

        mock_interact.assert_called_once()

    def test_run_skips_interaction_when_no_pending(self, tmp_path):
        """run() with no pending decisions does not call _interact_for_decisions."""
        spec_dir = tmp_path / "se3" / "specs" / "test_spec"
        spec_dir.mkdir(parents=True, exist_ok=True)
        (spec_dir / "spec.md").write_text("# Spec\n## Purpose\nTest.")

        engine = SyncEngine(tmp_path, mode="fast")

        mock_interact = MagicMock(return_value={"specs_updated": 0, "issues_created": 0})

        with patch.object(engine, "_interact_for_decisions", mock_interact):
            with patch("se3.engine.llm_caller.LLMCaller"):
                with patch("se3.engine.project_context.ProjectContextCollector") as mock_ctx:
                    mock_ctx.return_value.collect.return_value = {}
                    with patch("se3.engine.sync_analyzer.SyncAnalyzer") as mock_analyzer:
                        mock_analysis = SpecAnalysis(
                            spec_name="test_spec",
                            diffs=[],
                        )
                        mock_analyzer.return_value.analyze_spec.return_value = mock_analysis
                        result = engine.run()

        mock_interact.assert_not_called()

    def test_run_generates_call_file_when_not_tty(self, tmp_path):
        """run() with pending decisions generates call file when stdin is not a tty."""
        spec_dir = tmp_path / "se3" / "specs" / "test_spec"
        spec_dir.mkdir(parents=True, exist_ok=True)
        (spec_dir / "spec.md").write_text("# Spec\n## Purpose\nTest.")

        engine = SyncEngine(tmp_path, mode="strict")

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.isatty.return_value = False
            with patch("se3.engine.llm_caller.LLMCaller"):
                with patch("se3.engine.project_context.ProjectContextCollector") as mock_ctx:
                    mock_ctx.return_value.collect.return_value = {}
                    with patch("se3.engine.sync_analyzer.SyncAnalyzer") as mock_analyzer:
                        mock_analysis = SpecAnalysis(
                            spec_name="test_spec",
                            diffs=[SpecDiff(DiffType.GAP, "test_spec", "Missing feature")],
                        )
                        mock_analyzer.return_value.analyze_spec.return_value = mock_analysis
                        result = engine.run()

        assert result.call_file is not None
        assert "sync_pending_" in result.call_file

    def test_execute_decisions_gap_update_spec(self, tmp_path):
        spec_dir = tmp_path / "se3" / "specs" / "auth"
        spec_dir.mkdir(parents=True, exist_ok=True)
        (spec_dir / "spec.md").write_text("# Auth\n## Purpose\nAuth spec.")

        engine = SyncEngine(tmp_path)
        engine._load_specs()

        pending = [
            PendingDecision(
                type="gap",
                item_id="gap_1",
                spec_name="auth",
                description="Old requirement",
                diff="src/auth.py:10",
            ),
        ]

        mock_llm = MagicMock()
        mock_llm.call.return_value = "# Auth\n## Purpose\nUpdated auth spec."

        result = engine._execute_decisions(
            pending, {"gap_1": "update_spec"}, mock_llm
        )

        assert result["specs_updated"] == 1

    def test_execute_decisions_conflict_create_issue(self, tmp_path):
        engine = SyncEngine(tmp_path)

        pending = [
            PendingDecision(
                type="conflict",
                item_id="conflict_1",
                spec_name="auth",
                description="Conflicting behavior",
                diff="src/auth.py:20",
            ),
        ]

        result = engine._execute_decisions(
            pending, {"conflict_1": "create_issue"}, MagicMock()
        )

        assert result["issues_created"] == 1

    def test_execute_decisions_skips_unknown_ids(self, tmp_path):
        engine = SyncEngine(tmp_path)

        pending = [
            PendingDecision(
                type="gap",
                item_id="gap_1",
                spec_name="auth",
                description="Test",
            ),
        ]

        result = engine._execute_decisions(
            pending, {"unknown_id": "update_spec"}, MagicMock()
        )

        assert result["specs_updated"] == 0
        assert result["issues_created"] == 0


# ---------------------------------------------------------------------------
# Dict-based items (backward compatibility)
# ---------------------------------------------------------------------------

class TestDictItems:
    def test_handler_works_with_dicts(self, tmp_path):
        items = [
            {
                "type": "gap",
                "item_id": "dict_item_0",
                "spec_name": "spec_a",
                "description": "A gap",
                "diff": "",
                "confidence": "low",
                "decision": "pending",
            }
        ]
        handler = SyncInteractionHandler(tmp_path, items)
        call_file = handler._generate_pending_call_file()

        data = json.loads(call_file.read_text())
        assert data["items"][0]["item_id"] == "dict_item_0"

    def test_get_field_from_dict(self, tmp_path):
        handler = SyncInteractionHandler(tmp_path)
        assert handler._get_field({"key": "val"}, "key") == "val"
        assert handler._get_field({"key": "val"}, "missing", "default") == "default"

    def test_get_field_from_dataclass(self, tmp_path):
        handler = SyncInteractionHandler(tmp_path)
        pd = PendingDecision(type="gap", item_id="test_id")
        assert handler._get_field(pd, "item_id") == "test_id"
        assert handler._get_field(pd, "nonexistent", "fallback") == "fallback"


# ---------------------------------------------------------------------------
# Write response file
# ---------------------------------------------------------------------------

class TestWriteResponseFile:
    def test_writes_response_file(self, tmp_path):
        items = _make_pending(2)
        handler = SyncInteractionHandler(tmp_path, items)
        call_file = handler._generate_pending_call_file()
        handler._call_file_path = call_file

        handler._write_response_file({"item_0": "update_spec", "item_1": "create_issue"})

        response_path = Path(str(call_file) + ".response")
        assert response_path.exists()
        data = json.loads(response_path.read_text())
        assert data["items"][0]["decision"] == "update_spec"
        assert data["items"][1]["decision"] == "create_issue"

    def test_no_call_file_no_crash(self, tmp_path):
        handler = SyncInteractionHandler(tmp_path, _make_pending(1))
        handler._call_file_path = None
        handler._write_response_file({"item_0": "update_spec"})
