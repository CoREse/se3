"""Tests for the interactive (CLI-terminal) discovery pause dual-path wait.

An interactive ``se3 run --discover`` pause now ALSO mirrors itself to a
``se3/calls/`` call file so the web console can answer it, and the run loop
waits on the terminal and the web response file in parallel — whichever answers
first drives the *same* live process forward (no ``--resume``). The flow stays
RUNNING throughout, so a watching daemon never races it with a duplicate spawn.

These tests drive that path in :mod:`tianluo.commands.run`:

* both pause forms (question / programmatic-confirm) write the right call file,
* a web response file is consumed and the call+response are cleaned up,
* the terminal-first path consumes terminal input and cleans up the call file,
* and the aggregator deduplicates multiple call files for one (flow, step).

Tests run with a non-TTY stdin (pytest captures stdin), so the dual-wait takes
its non-interactive branch: it calls ``_read_multiline_input`` for the terminal
read and re-checks the web response file afterwards. We patch
``_read_multiline_input`` to simulate the operator (terminal-first) or to
materialize a web response mid-wait (web-first).
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tianluo.commands.run import (
    _PROGRAMMATIC_CONFIRM,
    _handle_discovery_pause,
    _handle_discovery_programmatic_confirm,
)
from tianluo.daemon.aggregator import DaemonAggregator, PendingCall


# --------------------------------------------------------------------------
# Fixtures / helpers
# --------------------------------------------------------------------------


def _make_step(step_id="01_discovery", outputs=None, inputs=None):
    return SimpleNamespace(
        step_id=step_id,
        outputs=dict(outputs or {}),
        inputs=dict(inputs or {}),
    )


def _make_flow(flow_id="flow-xyz"):
    return SimpleNamespace(flow_id=flow_id)


class _RecordingPersistence:
    def __init__(self):
        self.saved = 0

    def save_flow(self, flow):
        self.saved += 1


def _calls_dir(project_root: Path) -> Path:
    return project_root / "se3" / "calls"


def _only_call_file(project_root: Path) -> Path:
    files = [
        p
        for p in _calls_dir(project_root).glob("*.json")
        if not p.name.endswith(".response.json")
    ]
    assert len(files) == 1, f"expected exactly one call file, got {files}"
    return files[0]


# --------------------------------------------------------------------------
# Task 1 — both pause forms write the right call file
# --------------------------------------------------------------------------


def test_interactive_question_pause_writes_call_file(tmp_path):
    """A terminal answer is consumed, and the question call file was written
    with kind=call + context.flow_id/step_id while the wait was in progress."""
    flow = _make_flow()
    step = _make_step(outputs={"message": "Which database?", "questions": ["SQL?"]})
    persistence = _RecordingPersistence()
    captured = {}

    def fake_read(*args, **kwargs):
        # The call file exists at the moment the terminal read blocks.
        call_file = _only_call_file(tmp_path)
        captured["data"] = json.loads(call_file.read_text())
        return "Use Postgres"

    with patch("tianluo.commands.run._read_multiline_input", side_effect=fake_read):
        result = _handle_discovery_pause(
            flow, step, persistence, None, tmp_path
        )

    assert result == "Use Postgres"
    data = captured["data"]
    assert data["kind"] == "call"
    assert data["context"]["flow_id"] == "flow-xyz"
    assert data["context"]["step_id"] == "01_discovery"
    assert "Which database?" in data["prompt"]
    assert data["options"] == []
    # Terminal answer consumed → the call file is cleaned up (no stale chip).
    assert not list(_calls_dir(tmp_path).glob("*.json"))


def test_interactive_confirm_pause_writes_discovery_confirm_call(tmp_path):
    """The confirmation gate writes a discovery_confirm call with a one-click
    value='1' option, then a terminal '1' confirms and cleans up."""
    flow = _make_flow()
    step = _make_step(
        outputs={
            "awaiting_programmatic_confirm": True,
            "refined_description": "Build a CLI tool",
        }
    )
    persistence = _RecordingPersistence()
    captured = {}

    def fake_read(*args, **kwargs):
        call_file = _only_call_file(tmp_path)
        captured["data"] = json.loads(call_file.read_text())
        return "1"

    with patch("tianluo.commands.run._read_multiline_input", side_effect=fake_read):
        result = _handle_discovery_programmatic_confirm(
            flow, step, persistence, None, tmp_path
        )

    assert result is _PROGRAMMATIC_CONFIRM
    assert step.inputs.get("programmatic_confirmed") is True
    data = captured["data"]
    assert data["kind"] == "discovery_confirm"
    assert "Type 1 to confirm" in data["prompt"]
    assert "Build a CLI tool" in data["prompt"]
    assert len(data["options"]) == 1
    assert data["options"][0]["value"] == "1"
    assert data["context"]["flow_id"] == "flow-xyz"
    assert data["context"]["step_id"] == "01_discovery"
    # Confirmed → call file cleaned up.
    assert not list(_calls_dir(tmp_path).glob("*.json"))


def test_interactive_pause_keeps_flow_running(tmp_path):
    """A successful pause round never calls save_flow (no PAUSED persist) — the
    flow stays RUNNING so the daemon does not duplicate-spawn a --resume."""
    flow = _make_flow()
    step = _make_step(outputs={"message": "?"})
    persistence = _RecordingPersistence()

    with patch("tianluo.commands.run._read_multiline_input", return_value="answer"):
        _handle_discovery_pause(flow, step, persistence, None, tmp_path)

    assert persistence.saved == 0


# --------------------------------------------------------------------------
# Task 2 — dual-path wait: web-first, terminal-first, cancel
# --------------------------------------------------------------------------


def test_web_response_consumed_and_files_cleaned(tmp_path):
    """A web response that arrives while the terminal is blocked is consumed,
    and both the call file and the response file are cleaned up."""
    flow = _make_flow()
    step = _make_step(outputs={"message": "Clarify scope?"})
    persistence = _RecordingPersistence()

    def fake_read(*args, **kwargs):
        # Simulate the web answering while we "block" on the terminal: write the
        # response sibling and return None (terminal produced nothing).
        call_file = _only_call_file(tmp_path)
        resp = call_file.parent / f"{call_file.stem}.response.json"
        resp.write_text(json.dumps({"response": "Scope is the auth module"}))
        return None

    with patch("tianluo.commands.run._read_multiline_input", side_effect=fake_read):
        result = _handle_discovery_pause(
            flow, step, persistence, None, tmp_path
        )

    assert result == "Scope is the auth module"
    # Web answer consumed → call + response both removed (no stale chip, no
    # double-consume on a later round).
    assert not list(_calls_dir(tmp_path).glob("*.json"))
    assert not list(_calls_dir(tmp_path).glob("*.response.json"))


def test_web_confirm_response_returns_sentinel(tmp_path):
    """A web confirm submitting '1' drives the gate exactly like a terminal '1'."""
    flow = _make_flow()
    step = _make_step(
        outputs={"awaiting_programmatic_confirm": True, "refined_description": "Do X"}
    )
    persistence = _RecordingPersistence()

    def fake_read(*args, **kwargs):
        call_file = _only_call_file(tmp_path)
        resp = call_file.parent / f"{call_file.stem}.response.json"
        resp.write_text(json.dumps({"response": "1"}))
        return None

    with patch("tianluo.commands.run._read_multiline_input", side_effect=fake_read):
        result = _handle_discovery_programmatic_confirm(
            flow, step, persistence, None, tmp_path
        )

    assert result is _PROGRAMMATIC_CONFIRM
    assert step.inputs.get("programmatic_confirmed") is True
    assert not list(_calls_dir(tmp_path).glob("*.json"))


def test_web_confirm_other_text_continues_discovery(tmp_path):
    """A web reply other than '1' clears the gate and continues discovery."""
    flow = _make_flow()
    step = _make_step(
        outputs={"awaiting_programmatic_confirm": True, "refined_description": "Do X"}
    )
    persistence = _RecordingPersistence()

    def fake_read(*args, **kwargs):
        call_file = _only_call_file(tmp_path)
        resp = call_file.parent / f"{call_file.stem}.response.json"
        resp.write_text(json.dumps({"response": "Also handle OAuth"}))
        return None

    with patch("tianluo.commands.run._read_multiline_input", side_effect=fake_read):
        result = _handle_discovery_programmatic_confirm(
            flow, step, persistence, None, tmp_path
        )

    assert result == "Also handle OAuth"
    assert "awaiting_programmatic_confirm" not in step.outputs
    assert step.inputs.get("programmatic_confirmed") is None
    assert not list(_calls_dir(tmp_path).glob("*.json"))


def test_terminal_first_consumes_terminal_and_cleans_up(tmp_path):
    """When the terminal answers (and no web response exists), the terminal
    input wins and the call file is cleaned up."""
    flow = _make_flow()
    step = _make_step(outputs={"message": "Clarify scope?"})
    persistence = _RecordingPersistence()

    with patch(
        "tianluo.commands.run._read_multiline_input", return_value="terminal answer"
    ):
        result = _handle_discovery_pause(
            flow, step, persistence, None, tmp_path
        )

    assert result == "terminal answer"
    assert not list(_calls_dir(tmp_path).glob("*.json"))


def test_cancel_saves_flow_and_cleans_up_call(tmp_path):
    """Ctrl+C / EOF (terminal returns None, no web response) pauses the flow
    (state saved) and the mirrored call file is cleaned up — no stale chip."""
    flow = _make_flow()
    step = _make_step(outputs={"message": "?"})
    persistence = _RecordingPersistence()

    with patch("tianluo.commands.run._read_multiline_input", return_value=None):
        result = _handle_discovery_pause(
            flow, step, persistence, None, tmp_path
        )

    assert result is None
    assert persistence.saved == 1
    assert not list(_calls_dir(tmp_path).glob("*.json"))


def test_no_project_root_is_terminal_only(tmp_path):
    """With no project root, no call file is written and the handler degrades to
    a plain terminal read (backward compatibility)."""
    flow = _make_flow()
    step = _make_step(outputs={"message": "?"})
    persistence = _RecordingPersistence()

    with patch("tianluo.commands.run._read_multiline_input", return_value="hi"):
        result = _handle_discovery_pause(flow, step, persistence, None, None)

    assert result == "hi"
    # No se3/calls/ directory created when there is no web channel.
    assert not (tmp_path / "se3" / "calls").exists()


# --------------------------------------------------------------------------
# Task 3 — aggregator dedup: newest call per (flow_id, step_id)
# --------------------------------------------------------------------------


def _call(call_id, *, flow_id, step_id, created_at):
    return PendingCall(
        call_id=call_id,
        path=f"/tmp/{call_id}.json",
        project_root="/tmp",
        kind="call",
        created_at=created_at,
        context={"flow_id": flow_id, "step_id": step_id},
    )


def test_dedup_keeps_newest_call_per_step():
    """Multiple unanswered calls for one (flow, step) collapse to the newest."""
    calls = [
        _call("discovery_s1_001", flow_id="F1", step_id="s1", created_at=100.0),
        _call("discovery_s1_002", flow_id="F1", step_id="s1", created_at=200.0),
        _call("discovery_s1_003", flow_id="F1", step_id="s1", created_at=150.0),
    ]

    result = DaemonAggregator._dedup_calls_by_step(calls)

    assert len(result) == 1
    assert result[0].call_id == "discovery_s1_002"  # highest created_at


def test_dedup_independent_steps_are_all_kept():
    """Different (flow, step) keys are never collapsed together."""
    calls = [
        _call("a", flow_id="F1", step_id="s1", created_at=100.0),
        _call("b", flow_id="F1", step_id="s2", created_at=110.0),
        _call("c", flow_id="F2", step_id="s1", created_at=120.0),
    ]

    result = DaemonAggregator._dedup_calls_by_step(calls)

    assert {c.call_id for c in result} == {"a", "b", "c"}


def test_dedup_passes_through_unkeyable_calls():
    """Calls with no flow_id / step_id are passed through, never deduplicated."""
    unattributed_a = PendingCall(
        call_id="merge_x", path="/tmp/merge_x.json", project_root="/tmp"
    )
    unattributed_b = PendingCall(
        call_id="merge_y", path="/tmp/merge_y.json", project_root="/tmp"
    )
    calls = [unattributed_a, unattributed_b]

    result = DaemonAggregator._dedup_calls_by_step(calls)

    assert {c.call_id for c in result} == {"merge_x", "merge_y"}


def test_dedup_preserves_first_seen_order():
    """The surviving winner is emitted at its key's first-seen position."""
    calls = [
        _call("s1_old", flow_id="F1", step_id="s1", created_at=100.0),
        _call("s2_only", flow_id="F1", step_id="s2", created_at=110.0),
        _call("s1_new", flow_id="F1", step_id="s1", created_at=200.0),
    ]

    result = DaemonAggregator._dedup_calls_by_step(calls)

    # s1's slot comes first (first-seen), now occupied by the newest s1 call.
    assert [c.call_id for c in result] == ["s1_new", "s2_only"]


def test_dedup_integrates_with_snapshot(tmp_path):
    """End-to-end: two discovery call files sharing a step_id surface as one
    pending call in the per-flow snapshot."""
    state_dir = tmp_path / "se3" / "state"
    state_dir.mkdir(parents=True)
    calls_dir = tmp_path / "se3" / "calls"
    calls_dir.mkdir(parents=True)

    engine = {
        "flow_id": "F1",
        "task_description": "t",
        "status": "running",
        "state": {
            "current_step_id": "01_discovery",
            "selected_steps": ["discovery"],
            "current_step_index": 0,
            "steps": {
                "01_discovery": {"step_type": "discovery", "status": "running"}
            },
        },
    }
    (state_dir / "engine.json").write_text(json.dumps(engine))

    def _write_call(name, created_at):
        path = calls_dir / name
        path.write_text(
            json.dumps(
                {
                    "call_id": path.stem,
                    "kind": "call",
                    "prompt": "?",
                    "context": {"flow_id": "F1", "step_id": "01_discovery"},
                    "options": [],
                }
            )
        )
        import os

        os.utime(path, (created_at, created_at))

    _write_call("discovery_01_discovery_001.json", 100.0)
    _write_call("discovery_01_discovery_002.json", 200.0)

    aggregator = DaemonAggregator()
    snapshot = aggregator._snapshot_for_root(tmp_path)

    assert snapshot is not None
    assert len(snapshot.pending_calls) == 1
    assert snapshot.pending_calls[0].call_id == "discovery_01_discovery_002"
