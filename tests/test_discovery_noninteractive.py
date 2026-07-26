"""Tests for non-interactive (daemon-spawned) discovery pause handling.

When ``se3 run --discover`` runs under a daemon (``--output-format json``,
no terminal), the discovery step cannot block on a terminal read. Instead it
writes the clarifying question to a ``se3/calls/`` call file and pauses; the
web answers it through the existing call/response mechanism. These tests
exercise that path in :mod:`tianluo.commands.run`.
"""

import json
from pathlib import Path
from types import SimpleNamespace

from tianluo.commands.run import (
    _DISCOVERY_AWAITING,
    _PROGRAMMATIC_CONFIRM,
    _handle_discovery_pause_noninteractive,
    _read_discovery_response,
    _write_discovery_call,
)


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


def test_write_discovery_call_creates_question_call_file(tmp_path):
    flow = _make_flow()
    step = _make_step(outputs={"message": "What database?", "questions": ["SQL or NoSQL?"]})

    call_file = _write_discovery_call(flow, step, tmp_path)

    assert call_file.exists()
    assert call_file.parent == tmp_path / "se3" / "calls"
    data = json.loads(call_file.read_text())
    # Question pauses ride the unified call queue as a plain ``call`` kind.
    assert data["kind"] == "call"
    # Owning flow/step ids live in context (scopes the per-flow filter) and are
    # mirrored top-level for backward-compatible readers.
    assert data["context"]["flow_id"] == "flow-xyz"
    assert data["context"]["step_id"] == "01_discovery"
    assert data["flow_id"] == "flow-xyz"
    assert data["step_id"] == "01_discovery"
    assert "What database?" in data["prompt"]
    assert "SQL or NoSQL?" in data["prompt"]
    # Question pauses carry no confirm option.
    assert data["options"] == []


def test_write_discovery_call_marks_confirmation(tmp_path):
    flow = _make_flow()
    step = _make_step(
        outputs={
            "awaiting_programmatic_confirm": True,
            "refined_description": "Build a CLI tool",
        }
    )

    call_file = _write_discovery_call(flow, step, tmp_path)
    data = json.loads(call_file.read_text())
    # Confirmation pauses carry the dedicated discovery_confirm kind so the web
    # console renders a GUI confirm button + the textual confirm hint (i18n:
    # "Type 1 to confirm" under the en-US test locale).
    assert data["kind"] == "discovery_confirm"
    assert "Build a CLI tool" in data["prompt"]
    assert "Type 1 to confirm" in data["prompt"]
    # The confirm option encodes the literal "1" the gate's == "1" check wants.
    assert len(data["options"]) == 1
    assert data["options"][0]["value"] == "1"
    # Per-flow scoping + refined description preserved in context.
    assert data["context"]["flow_id"] == "flow-xyz"
    assert data["context"]["refined_description"] == "Build a CLI tool"


def test_read_discovery_response_from_daemon_envelope(tmp_path):
    call_file = tmp_path / "discovery_01_x.json"
    call_file.write_text("{}")
    resp = tmp_path / "discovery_01_x.response.json"
    resp.write_text(json.dumps({"call_id": "discovery_01_x", "response": "Use SQL"}))

    assert _read_discovery_response(call_file) == "Use SQL"


def test_read_discovery_response_from_plain_sibling(tmp_path):
    call_file = tmp_path / "discovery_01_x.json"
    call_file.write_text("{}")
    resp = tmp_path / "discovery_01_x.response"
    resp.write_text(json.dumps({"feedback": "More detail"}))

    assert _read_discovery_response(call_file) == "More detail"


def test_read_discovery_response_missing_returns_none(tmp_path):
    call_file = tmp_path / "discovery_01_x.json"
    call_file.write_text("{}")
    assert _read_discovery_response(call_file) is None


def test_noninteractive_first_pause_writes_call_and_awaits(tmp_path):
    flow = _make_flow()
    step = _make_step(outputs={"message": "Clarify scope?"})
    persistence = _RecordingPersistence()

    result = _handle_discovery_pause_noninteractive(flow, step, persistence, tmp_path)

    assert result is _DISCOVERY_AWAITING
    assert persistence.saved == 1
    call_path = step.outputs.get("discovery_call_file")
    assert call_path and Path(call_path).exists()


def test_noninteractive_unanswered_call_keeps_awaiting(tmp_path):
    flow = _make_flow()
    step = _make_step(outputs={"message": "Clarify scope?"})
    persistence = _RecordingPersistence()

    _handle_discovery_pause_noninteractive(flow, step, persistence, tmp_path)
    # Re-enter while no response file exists yet — still awaiting, no new call.
    first_call = step.outputs["discovery_call_file"]
    result = _handle_discovery_pause_noninteractive(flow, step, persistence, tmp_path)
    assert result is _DISCOVERY_AWAITING
    assert step.outputs["discovery_call_file"] == first_call


def test_noninteractive_consumes_question_response(tmp_path):
    flow = _make_flow()
    step = _make_step(outputs={"message": "Clarify scope?"})
    persistence = _RecordingPersistence()

    _handle_discovery_pause_noninteractive(flow, step, persistence, tmp_path)
    call_file = Path(step.outputs["discovery_call_file"])
    resp = call_file.parent / f"{call_file.stem}.response.json"
    resp.write_text(json.dumps({"response": "Scope is the auth module"}))

    result = _handle_discovery_pause_noninteractive(flow, step, persistence, tmp_path)
    assert result == "Scope is the auth module"
    # The answered call + response files are cleaned up for the next round.
    assert not call_file.exists()
    assert not resp.exists()
    assert "discovery_call_file" not in step.outputs


def test_noninteractive_confirm_with_1_returns_sentinel(tmp_path):
    flow = _make_flow()
    step = _make_step(
        outputs={"awaiting_programmatic_confirm": True, "refined_description": "Do X"}
    )
    persistence = _RecordingPersistence()

    _handle_discovery_pause_noninteractive(flow, step, persistence, tmp_path)
    call_file = Path(step.outputs["discovery_call_file"])
    (call_file.parent / f"{call_file.stem}.response.json").write_text(
        json.dumps({"response": "1"})
    )

    result = _handle_discovery_pause_noninteractive(flow, step, persistence, tmp_path)
    assert result is _PROGRAMMATIC_CONFIRM
    assert step.inputs.get("programmatic_confirmed") is True


def test_noninteractive_confirm_other_text_continues_discovery(tmp_path):
    flow = _make_flow()
    step = _make_step(
        outputs={"awaiting_programmatic_confirm": True, "refined_description": "Do X"}
    )
    persistence = _RecordingPersistence()

    _handle_discovery_pause_noninteractive(flow, step, persistence, tmp_path)
    call_file = Path(step.outputs["discovery_call_file"])
    (call_file.parent / f"{call_file.stem}.response.json").write_text(
        json.dumps({"response": "Actually, also handle OAuth"})
    )

    result = _handle_discovery_pause_noninteractive(flow, step, persistence, tmp_path)
    assert result == "Actually, also handle OAuth"
    assert "awaiting_programmatic_confirm" not in step.outputs
    assert step.inputs.get("programmatic_confirmed") is None
