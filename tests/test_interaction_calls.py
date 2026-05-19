"""Tests for the unified interaction-call channel.

Covers the three layers that turn every human-in-the-loop interaction in a
running flow into one artifact — a JSON call file under ``se3/calls/``:

* :mod:`se3.daemon.protocol` — the ``MSG_INTERJECT_FLOW`` message, the
  ``CALL_KIND_*`` constants and decode backward-compatibility;
* :class:`se3.daemon.aggregator.DaemonAggregator` — parsing call files of
  every ``kind`` *and* legacy call files written before the field existed;
* :mod:`se3.engine.interaction_calls` — writing call files, reading sibling
  response files, and draining queued mid-flow interjections.

A dedicated backward-compatibility case feeds the aggregator and the reader a
``kind``-less, metadata-less call file (the only shape SE3 wrote before this
feature) and asserts it still parses cleanly as a plain ``call``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from se3.daemon import protocol
from se3.daemon.aggregator import DaemonAggregator
from se3.engine import interaction_calls


# --------------------------------------------------------------------------
# protocol: MSG_INTERJECT_FLOW, kind constants, decode compatibility
# --------------------------------------------------------------------------


def test_make_interject_flow_payload_and_direction():
    msg = protocol.make_interject_flow("flow-1", "please add logging", project_root="/p")
    assert msg.type == protocol.MSG_INTERJECT_FLOW
    assert msg.payload == {
        "flow_id": "flow-1",
        "text": "please add logging",
        "project_root": "/p",
    }
    # It is a server → daemon instruction, never daemon → server.
    assert protocol.MSG_INTERJECT_FLOW in protocol.SERVER_TO_DAEMON
    assert protocol.MSG_INTERJECT_FLOW not in protocol.DAEMON_TO_SERVER
    assert protocol.MSG_INTERJECT_FLOW in protocol.ALL_MESSAGE_TYPES


def test_interject_flow_round_trips_through_decode():
    raw = protocol.make_interject_flow("flow-1", "hi").to_json()
    restored = protocol.decode(raw)
    assert restored.type == protocol.MSG_INTERJECT_FLOW
    assert restored.payload["text"] == "hi"
    assert restored.payload["project_root"] == ""


def test_call_kind_constants_are_complete_and_distinct():
    assert protocol.CALL_KINDS == {
        protocol.CALL_KIND_CALL,
        protocol.CALL_KIND_INTERJECTION,
        protocol.CALL_KIND_RETRY_DECISION,
        protocol.CALL_KIND_CLI_CONFIRM,
    }
    # The four kinds are distinct string literals.
    assert len({
        protocol.CALL_KIND_CALL,
        protocol.CALL_KIND_INTERJECTION,
        protocol.CALL_KIND_RETRY_DECISION,
        protocol.CALL_KIND_CLI_CONFIRM,
    }) == 4


def test_decode_still_rejects_unknown_types():
    # Adding new known types must not weaken rejection of genuinely unknown
    # ones — a peer speaking an older revision must still be able to reject.
    with pytest.raises(protocol.ProtocolError):
        protocol.decode('{"type": "totally_bogus", "payload": {}}')


def test_decode_tolerates_interject_without_optional_fields():
    # A minimal-but-valid interject frame (only the required type) decodes.
    raw = json.dumps({"type": protocol.MSG_INTERJECT_FLOW, "payload": {}})
    msg = protocol.decode(raw)
    assert msg.type == protocol.MSG_INTERJECT_FLOW
    assert msg.payload == {}


# --------------------------------------------------------------------------
# aggregator: multi-kind parsing + legacy (kind-less) call files
# --------------------------------------------------------------------------


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_aggregator_parses_every_call_kind(tmp_path: Path):
    calls = tmp_path / "se3" / "calls"
    calls.mkdir(parents=True)
    _write(calls / "k_call.json", {"kind": "call", "prompt": "answer me"})
    _write(
        calls / "k_interjection.json",
        {"kind": "interjection", "prompt": "ctrl-c text"},
    )
    _write(
        calls / "k_retry.json",
        {
            "kind": "retry_decision",
            "prompt": "step failed",
            "options": ["retry", "skip", "abort"],
            "context": {"step_id": "s1"},
        },
    )
    _write(calls / "k_cli.json", {"kind": "cli_confirm", "prompt": "press 1"})

    aggregator = DaemonAggregator()
    parsed = {c.call_id: c for c in aggregator._enumerate_calls(tmp_path)}

    assert parsed["k_call"].kind == "call"
    assert parsed["k_interjection"].kind == "interjection"
    assert parsed["k_cli"].kind == "cli_confirm"

    retry = parsed["k_retry"]
    assert retry.kind == "retry_decision"
    assert retry.prompt == "step failed"
    assert retry.options == ["retry", "skip", "abort"]
    assert retry.context == {"step_id": "s1"}
    # The enriched fields survive the to_dict() snapshot serialization.
    assert retry.to_dict()["options"] == ["retry", "skip", "abort"]


def test_aggregator_legacy_call_file_without_kind(tmp_path: Path):
    """Backward compatibility: a pre-feature call file has no metadata."""
    calls = tmp_path / "se3" / "calls"
    calls.mkdir(parents=True)
    # The exact shape SE3 wrote before the unified channel existed: just the
    # confirm-call fields, no `kind` / `prompt` / `context` / `options`.
    _write(
        calls / "confirm_legacy_001.json",
        {"step": "confirm-1", "step_to_review_id": "s1", "type": "confirm"},
    )

    aggregator = DaemonAggregator()
    parsed = aggregator._enumerate_calls(tmp_path)

    assert len(parsed) == 1
    legacy = parsed[0]
    # A kind-less file is reported as the plain `call` kind, not dropped.
    assert legacy.kind == "call"
    assert legacy.prompt == ""
    assert legacy.context == {}
    assert legacy.options == []


def test_aggregator_unknown_kind_falls_back_to_call(tmp_path: Path):
    calls = tmp_path / "se3" / "calls"
    calls.mkdir(parents=True)
    _write(calls / "weird.json", {"kind": "not_a_real_kind", "prompt": "x"})
    _write(calls / "garbage.json", "this is not even a json object")

    aggregator = DaemonAggregator()
    parsed = {c.call_id: c for c in aggregator._enumerate_calls(tmp_path)}

    # An unrecognised kind and a non-object body both degrade to `call`.
    assert parsed["weird"].kind == "call"
    assert parsed["garbage"].kind == "call"


# --------------------------------------------------------------------------
# interaction_calls: write call / read call / classify
# --------------------------------------------------------------------------


def test_write_and_read_call_round_trip(tmp_path: Path):
    calls = tmp_path / "se3" / "calls"
    path = interaction_calls.write_call(
        calls,
        kind=interaction_calls.CALL_KIND_CLI_CONFIRM,
        prompt="Press 1 to continue",
        context={"pid": 1234},
        options=["1", "2"],
        call_id="cli_001",
    )
    assert path.name == "cli_001.json"

    data = interaction_calls.read_call(path)
    assert data is not None
    assert data["kind"] == "cli_confirm"
    assert data["prompt"] == "Press 1 to continue"
    assert data["context"] == {"pid": 1234}
    assert data["options"] == ["1", "2"]


def test_write_call_rejects_unknown_kind(tmp_path: Path):
    with pytest.raises(ValueError):
        interaction_calls.write_call(
            tmp_path / "se3" / "calls", kind="bogus", prompt="x"
        )


def test_read_call_legacy_file_defaults_kind(tmp_path: Path):
    """A call file with no `kind` key reads back as a plain `call`."""
    calls = tmp_path / "se3" / "calls"
    calls.mkdir(parents=True)
    legacy = calls / "old_call.json"
    legacy.write_text(json.dumps({"step": "s1"}), encoding="utf-8")

    data = interaction_calls.read_call(legacy)
    assert data is not None
    assert data["kind"] == "call"
    assert interaction_calls.classify_kind(data) == "call"


def test_read_call_handles_missing_and_malformed(tmp_path: Path):
    assert interaction_calls.read_call(tmp_path / "nope.json") is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert interaction_calls.read_call(bad) is None


def test_classify_kind_defensive():
    assert interaction_calls.classify_kind(None) == "call"
    assert interaction_calls.classify_kind({}) == "call"
    assert interaction_calls.classify_kind({"kind": "interjection"}) == "interjection"
    assert interaction_calls.classify_kind({"kind": "nonsense"}) == "call"


# --------------------------------------------------------------------------
# interaction_calls: response read / write
# --------------------------------------------------------------------------


def test_write_response_round_trip(tmp_path: Path):
    calls = tmp_path / "se3" / "calls"
    path = interaction_calls.write_call(
        calls, kind="call", prompt="?", call_id="c1"
    )
    assert interaction_calls.read_response(path) is None

    interaction_calls.write_response(path, {"approved": True, "feedback": "ok"})
    response = interaction_calls.read_response(path)
    assert response == {"approved": True, "feedback": "ok"}


def test_read_response_accepts_daemon_response_json(tmp_path: Path):
    """The daemon client writes `<stem>.response.json`; reader accepts both."""
    calls = tmp_path / "se3" / "calls"
    path = interaction_calls.write_call(
        calls, kind="retry_decision", prompt="failed", call_id="rd1"
    )
    # Simulate the daemon-client response writer.
    (calls / "rd1.response.json").write_text(
        json.dumps({"call_id": "rd1", "response": {"decision": "retry"}}),
        encoding="utf-8",
    )
    response = interaction_calls.read_response(path)
    assert response is not None
    assert response["response"] == {"decision": "retry"}


# --------------------------------------------------------------------------
# interaction_calls: drain_interjection_requests
# --------------------------------------------------------------------------


def test_drain_interjection_requests_consumes_and_is_idempotent(tmp_path: Path):
    calls = tmp_path / "se3" / "calls"
    interaction_calls.write_interjection_request(
        calls, "first instruction", flow_id="f1", call_id="ij_1"
    )
    interaction_calls.write_interjection_request(
        calls, "second instruction", flow_id="f1", call_id="ij_2"
    )

    drained = interaction_calls.drain_interjection_requests(tmp_path)
    texts = [d["text"] for d in drained]
    assert texts == ["first instruction", "second instruction"]
    assert {d["call_id"] for d in drained} == {"ij_1", "ij_2"}

    # A second drain returns nothing — every request was marked consumed.
    assert interaction_calls.drain_interjection_requests(tmp_path) == []


def test_drain_ignores_non_interjection_kinds(tmp_path: Path):
    calls = tmp_path / "se3" / "calls"
    interaction_calls.write_call(calls, kind="call", prompt="q", call_id="plain")
    interaction_calls.write_call(
        calls, kind="retry_decision", prompt="failed", call_id="rd"
    )
    interaction_calls.write_interjection_request(
        calls, "real interjection", call_id="ij"
    )

    drained = interaction_calls.drain_interjection_requests(tmp_path)
    assert [d["call_id"] for d in drained] == ["ij"]


def test_drain_skips_empty_text_requests(tmp_path: Path):
    calls = tmp_path / "se3" / "calls"
    # An interjection call whose text is whitespace-only is consumed silently.
    interaction_calls.write_call(
        calls,
        kind="interjection",
        prompt="   ",
        call_id="ij_empty",
        text="   ",
    )
    drained = interaction_calls.drain_interjection_requests(tmp_path)
    assert drained == []
    # It was still marked consumed so it is not re-examined forever.
    path = calls / "ij_empty.json"
    assert interaction_calls.read_response(path) is not None


def test_drain_missing_calls_dir_returns_empty(tmp_path: Path):
    assert interaction_calls.drain_interjection_requests(tmp_path) == []


# --------------------------------------------------------------------------
# interaction_calls: write_retry_decision_call
# --------------------------------------------------------------------------


def test_write_retry_decision_call_shape(tmp_path: Path):
    path = interaction_calls.write_retry_decision_call(
        tmp_path,
        flow_id="flow-9",
        step_id="step-3",
        step_type="implement",
        error="boom",
        retry_count=1,
    )
    # call_id is derived from the step id so a resume reuses the same file.
    assert path.name == "retry_decision_step-3.json"

    data = interaction_calls.read_call(path)
    assert data is not None
    assert data["kind"] == "retry_decision"
    assert data["options"] == ["retry", "skip", "abort"]
    assert data["context"]["flow_id"] == "flow-9"
    assert data["context"]["step_type"] == "implement"
    assert data["context"]["error"] == "boom"
    assert "boom" in data["prompt"]
