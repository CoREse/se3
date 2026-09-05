"""Tests for the unified interaction-call channel.

Covers the three layers that turn every human-in-the-loop interaction in a
running flow into one artifact — a JSON call file under ``tianluo/calls/``:

* :mod:`tianluo.daemon.protocol` — the ``MSG_INTERJECT_FLOW`` message, the
  ``CALL_KIND_*`` constants and decode backward-compatibility;
* :class:`tianluo.daemon.aggregator.DaemonAggregator` — parsing call files of
  every ``kind`` *and* legacy call files written before the field existed;
* :mod:`tianluo.engine.interaction_calls` — writing call files, reading sibling
  response files, and draining queued mid-flow interjections.

A dedicated backward-compatibility case feeds the aggregator and the reader a
``kind``-less, metadata-less call file (the only shape SE3 wrote before this
feature) and asserts it still parses cleanly as a plain ``call``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tianluo.daemon import protocol
from tianluo.daemon.aggregator import DaemonAggregator
from tianluo.engine import interaction_calls


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
        protocol.CALL_KIND_DISCOVERY_CONFIRM,
        protocol.CALL_KIND_CONFIRM,
        protocol.CALL_KIND_DIALOG,
    }
    # The seven kinds are distinct string literals.
    assert len({
        protocol.CALL_KIND_CALL,
        protocol.CALL_KIND_INTERJECTION,
        protocol.CALL_KIND_RETRY_DECISION,
        protocol.CALL_KIND_CLI_CONFIRM,
        protocol.CALL_KIND_DISCOVERY_CONFIRM,
        protocol.CALL_KIND_CONFIRM,
        protocol.CALL_KIND_DIALOG,
    }) == 7


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
    calls = tmp_path / "tianluo" / "calls"
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
    calls = tmp_path / "tianluo" / "calls"
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
    calls = tmp_path / "tianluo" / "calls"
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
    calls = tmp_path / "tianluo" / "calls"
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
            tmp_path / "tianluo" / "calls", kind="bogus", prompt="x"
        )


def test_read_call_legacy_file_defaults_kind(tmp_path: Path):
    """A call file with no `kind` key reads back as a plain `call`."""
    calls = tmp_path / "tianluo" / "calls"
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
    calls = tmp_path / "tianluo" / "calls"
    path = interaction_calls.write_call(
        calls, kind="call", prompt="?", call_id="c1"
    )
    assert interaction_calls.read_response(path) is None

    interaction_calls.write_response(path, {"approved": True, "feedback": "ok"})
    response = interaction_calls.read_response(path)
    assert response == {"approved": True, "feedback": "ok"}


def test_read_response_accepts_daemon_response_json(tmp_path: Path):
    """The daemon client writes `<stem>.response.json`; reader accepts both."""
    calls = tmp_path / "tianluo" / "calls"
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
    calls = tmp_path / "tianluo" / "calls"
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
    calls = tmp_path / "tianluo" / "calls"
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
    calls = tmp_path / "tianluo" / "calls"
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


def test_drain_unlinks_call_file_and_writes_served_sibling(tmp_path: Path):
    """Drain seals each consumed call via write-response-then-delete:
    a sibling .response is written first, then the .json file is unlinked.
    """
    calls = tmp_path / "tianluo" / "calls"
    interaction_calls.write_interjection_request(
        calls, "do the thing", flow_id="f1", call_id="ij_remove"
    )
    call_path = calls / "ij_remove.json"
    response_path = calls / "ij_remove.response"
    assert call_path.exists()
    assert not response_path.exists()

    drained = interaction_calls.drain_interjection_requests(tmp_path)
    assert len(drained) == 1
    # Call file is gone; sibling .response remains as the consumed marker.
    assert not call_path.exists()
    assert response_path.exists()
    body = json.loads(response_path.read_text(encoding="utf-8"))
    assert body.get("consumed") is True
    assert body.get("served_by") == "run_loop"
    assert "served_at" in body


def test_drain_returns_step_id_and_step_type_when_present(tmp_path: Path):
    """Drained entries surface step_id / step_type from the call context."""
    calls = tmp_path / "tianluo" / "calls"
    interaction_calls.write_call(
        calls,
        kind=interaction_calls.CALL_KIND_INTERJECTION,
        prompt="add a log line",
        text="add a log line",
        context={"flow_id": "f1", "step_id": "04_impl_abc", "step_type": "implement"},
        call_id="ij_ctx",
    )
    drained = interaction_calls.drain_interjection_requests(tmp_path)
    assert len(drained) == 1
    item = drained[0]
    assert item["text"] == "add a log line"
    assert item["step_id"] == "04_impl_abc"
    assert item["step_type"] == "implement"
    assert "created_at" in item


def test_drain_legacy_call_without_context_step_fields_empty(tmp_path: Path):
    """Legacy interjection calls (no context.step_id) still drain cleanly."""
    calls = tmp_path / "tianluo" / "calls"
    interaction_calls.write_interjection_request(
        calls, "legacy text", flow_id="", call_id="ij_legacy"
    )
    drained = interaction_calls.drain_interjection_requests(tmp_path)
    assert len(drained) == 1
    assert drained[0]["text"] == "legacy text"
    assert drained[0]["step_id"] == ""
    assert drained[0]["step_type"] == ""


def test_drain_second_pass_is_idempotent_after_unlink(tmp_path: Path):
    """A second drain pass finds nothing — the unlink ensures non-repeat."""
    calls = tmp_path / "tianluo" / "calls"
    interaction_calls.write_interjection_request(
        calls, "once", flow_id="f1", call_id="ij_once"
    )
    first = interaction_calls.drain_interjection_requests(tmp_path)
    assert len(first) == 1
    second = interaction_calls.drain_interjection_requests(tmp_path)
    assert second == []


# --------------------------------------------------------------------------
# interaction_calls: per-flow ownership of the interjection channel
# --------------------------------------------------------------------------


def test_drain_leaves_another_flows_interjection_untouched(tmp_path: Path):
    """A project root is a single slot successive flows occupy in turn.

    Flow B must not drain a message queued for the paused flow A: it would open
    the dialog against the wrong work AND strand A with nothing to wake it.
    """
    calls = tmp_path / "tianluo" / "calls"
    interaction_calls.write_interjection_request(
        calls, "for A", flow_id="flow-a", call_id="ij_a"
    )
    interaction_calls.write_interjection_request(
        calls, "for B", flow_id="flow-b", call_id="ij_b"
    )

    drained = interaction_calls.drain_interjection_requests(tmp_path, "flow-b")
    assert [item["text"] for item in drained] == ["for B"]
    # A's call file is untouched — not read, not sealed.
    assert (calls / "ij_a.json").exists()
    assert not (calls / "ij_a.response").exists()

    assert [
        item["text"]
        for item in interaction_calls.drain_interjection_requests(tmp_path, "flow-a")
    ] == ["for A"]


def test_unaddressed_interjection_is_deliverable_to_any_flow(tmp_path: Path):
    """Legacy producers wrote no flow_id; those must not become undrainable."""
    calls = tmp_path / "tianluo" / "calls"
    interaction_calls.write_interjection_request(
        calls, "legacy", flow_id="", call_id="ij_legacy_any"
    )
    drained = interaction_calls.drain_interjection_requests(tmp_path, "flow-z")
    assert [item["text"] for item in drained] == ["legacy"]


def test_has_pending_interjections_is_scoped_to_the_flow(tmp_path: Path):
    calls = tmp_path / "tianluo" / "calls"
    interaction_calls.write_interjection_request(
        calls, "for A", flow_id="flow-a", call_id="ij_peek"
    )
    assert interaction_calls.has_pending_interjections(tmp_path, "flow-a") is True
    assert interaction_calls.has_pending_interjections(tmp_path, "flow-b") is False
    # No scope asked for at all still sees everything.
    assert interaction_calls.has_pending_interjections(tmp_path, "") is True


def test_bound_active_flow_scopes_drains_without_an_explicit_argument(
    tmp_path: Path, monkeypatch
):
    """The run process binds its flow once; every drain site inherits it."""
    monkeypatch.setattr(interaction_calls, "_active_flow_id", "")
    calls = tmp_path / "tianluo" / "calls"
    interaction_calls.write_interjection_request(
        calls, "for A", flow_id="flow-a", call_id="ij_bound_a"
    )
    interaction_calls.bind_active_flow("flow-b")
    assert interaction_calls.active_flow_id() == "flow-b"
    assert interaction_calls.drain_interjection_requests(tmp_path) == []

    interaction_calls.bind_active_flow("flow-a")
    drained = interaction_calls.drain_interjection_requests(tmp_path)
    assert [item["text"] for item in drained] == ["for A"]


def test_call_flow_id_reads_context_then_legacy_top_level():
    assert interaction_calls.call_flow_id({"context": {"flow_id": "f1"}}) == "f1"
    assert interaction_calls.call_flow_id({"flow_id": "f2"}) == "f2"
    assert interaction_calls.call_flow_id({"context": "not-a-dict"}) == ""


# --------------------------------------------------------------------------
# interaction_calls: find_call_file / flow_id_for_call
# --------------------------------------------------------------------------


def test_find_call_file_hits_the_canonical_name(tmp_path: Path):
    calls_dir = interaction_calls.calls_dir_for(tmp_path)
    path = interaction_calls.write_call(
        calls_dir,
        kind=protocol.CALL_KIND_CALL,
        prompt="q",
        call_id="c1",
    )
    assert interaction_calls.find_call_file(calls_dir, "c1") == path


def test_find_call_file_scans_when_the_name_differs_from_the_id(tmp_path: Path):
    """Merge call files are named independently of the id recorded inside."""
    calls_dir = interaction_calls.calls_dir_for(tmp_path)
    calls_dir.mkdir(parents=True, exist_ok=True)
    target = calls_dir / "merge_feature-x_20260101.json"
    target.write_text(
        json.dumps({"call_id": "merge-77", "context": {"flow_id": "f7"}}),
        encoding="utf-8",
    )
    assert interaction_calls.find_call_file(calls_dir, "merge-77") == target
    assert interaction_calls.flow_id_for_call(tmp_path, "merge-77") == "f7"


def test_find_call_file_misses_are_none(tmp_path: Path):
    calls_dir = interaction_calls.calls_dir_for(tmp_path)
    assert interaction_calls.find_call_file(calls_dir, "nope") is None
    assert interaction_calls.find_call_file(calls_dir, "") is None
    calls_dir.mkdir(parents=True, exist_ok=True)
    (calls_dir / "garbage.json").write_text("{not json", encoding="utf-8")
    assert interaction_calls.find_call_file(calls_dir, "nope") is None


def test_flow_id_for_call_degrades_to_empty_when_unaddressed(tmp_path: Path):
    calls_dir = interaction_calls.calls_dir_for(tmp_path)
    interaction_calls.write_call(
        calls_dir, kind=protocol.CALL_KIND_CALL, prompt="q", call_id="c2"
    )
    assert interaction_calls.flow_id_for_call(tmp_path, "c2") == ""
    # No call file at all is the same "caller has no flow in hand" signal.
    assert interaction_calls.flow_id_for_call(tmp_path, "absent") == ""


def test_flow_id_for_call_reads_a_dialog_calls_owner(tmp_path: Path):
    interaction_calls.write_dialog_call(
        tmp_path,
        flow_id="flow-a",
        step_id="step-3",
        step_type="implement",
        prompt="what next?",
    )
    assert interaction_calls.flow_id_for_call(tmp_path, "dialog_step-3") == "flow-a"


def test_find_call_file_ignores_response_siblings(tmp_path: Path):
    """A ``<id>.response.json`` must never be mistaken for the call itself."""
    calls_dir = interaction_calls.calls_dir_for(tmp_path)
    calls_dir.mkdir(parents=True, exist_ok=True)
    (calls_dir / "orphan.response.json").write_text(
        json.dumps({"call_id": "orphan", "response": "1"}), encoding="utf-8"
    )
    assert interaction_calls.find_call_file(calls_dir, "orphan") is None


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


# --------------------------------------------------------------------------
# interaction_calls: make_cli_confirm_handler
# --------------------------------------------------------------------------


def test_cli_confirm_handler_returns_answer_when_response_arrives(tmp_path: Path):
    handler = interaction_calls.make_cli_confirm_handler(
        tmp_path, flow_id="f1", step_id="s1", poll_interval=0.01
    )
    calls = tmp_path / "tianluo" / "calls"

    answered = {"done": False}

    def is_alive() -> bool:
        # On the second poll, drop the answer in and report the child alive.
        if not answered["done"]:
            answered["done"] = True
            return True
        # Write the response, then keep reporting alive so the handler reads it.
        for path in calls.glob("*.json"):
            if path.name.endswith(".response.json"):
                continue
            interaction_calls.write_response(path, {"response": "1"})
        return True

    assert handler("Press 1", ["1", "2"], is_alive) == "1"


def test_cli_confirm_handler_marks_orphan_consumed_when_child_exits(tmp_path: Path):
    """A child that exits before answering leaves a consumed call file."""
    handler = interaction_calls.make_cli_confirm_handler(
        tmp_path, flow_id="f1", step_id="s1", poll_interval=0.01
    )
    calls = tmp_path / "tianluo" / "calls"

    # is_alive() reports the subprocess as already gone — no answer will come.
    assert handler("Press 1", ["1", "2"], lambda: False) is None

    call_files = [
        p for p in calls.glob("*.json") if not p.name.endswith(".response.json")
    ]
    assert len(call_files) == 1
    call_file = call_files[0]
    # The orphaned call file is marked consumed so the aggregator stops
    # enumerating it as an actionable pending interaction.
    response = interaction_calls.read_response(call_file)
    assert response is not None
    assert response.get("consumed") is True
    assert response.get("skipped") == "subprocess_exited"


def test_cli_confirm_handler_stops_waiting_when_a_stop_is_published(tmp_path: Path):
    """The confirm wait is the one place the runner's monitor loop hands control
    to a blocking callback. A stop published while the child sits at a
    confirmation prompt would otherwise be observed by nobody: the graceful-stop
    protocol never starts and Ctrl-C does nothing at all."""
    from tianluo.stop_signal import get_stop_signal

    handler = interaction_calls.make_cli_confirm_handler(
        tmp_path, flow_id="f1", step_id="s1", poll_interval=0.01
    )
    calls = tmp_path / "tianluo" / "calls"
    signal = get_stop_signal()
    signal.clear()

    polls = {"n": 0}

    def is_alive() -> bool:
        polls["n"] += 1
        if polls["n"] == 2:
            signal.request(text="stop this")
        # The child is genuinely still alive and still waiting on stdin.
        return True

    try:
        assert handler("Press 1", ["1", "2"], is_alive) is None
    finally:
        signal.clear()

    call_files = [
        p for p in calls.glob("*.json") if not p.name.endswith(".response.json")
    ]
    assert len(call_files) == 1
    response = interaction_calls.read_response(call_files[0])
    assert response is not None
    assert response.get("consumed") is True
    assert response.get("skipped") == "stop_requested"


def test_cli_confirm_handler_still_returns_a_late_answer_after_a_stop(tmp_path: Path):
    """A reply that landed in the final poll window is honoured even though a
    stop was published — the operator's answer is not thrown away."""
    from tianluo.stop_signal import get_stop_signal

    handler = interaction_calls.make_cli_confirm_handler(
        tmp_path, flow_id="f1", step_id="s1", poll_interval=0.01
    )
    calls = tmp_path / "tianluo" / "calls"
    signal = get_stop_signal()
    signal.clear()

    polls = {"n": 0}

    def is_alive() -> bool:
        polls["n"] += 1
        if polls["n"] == 2:
            for path in calls.glob("*.json"):
                if path.name.endswith(".response.json"):
                    continue
                interaction_calls.write_response(path, {"response": "1"})
            signal.request(text="stop this")
        return True

    try:
        assert handler("Press 1", ["1", "2"], is_alive) == "1"
    finally:
        signal.clear()


def test_dialog_call_carries_the_apply_error_and_group_work(tmp_path: Path):
    """Both live in the call CONTEXT, not only in the prompt: the prompt renders
    collapsed in the web console, so an apply failure carried only there
    republishes a byte-identical panel and reads as "nothing happened"."""
    path = interaction_calls.write_dialog_call(
        tmp_path,
        flow_id="f1",
        step_id="s1",
        step_type="implement",
        prompt="body",
        decision={"action": "restart", "workspace": "keep"},
        apply_error="engine.rewind.no_entry_snapshot",
        group_work=[{
            "branch": "impl/f1/G1",
            "worktree_path": "/tmp/wt",
            "commits": ["abc feat"],
            "status_summary": " M src/x.py",
        }],
    )
    data = json.loads(path.read_text())
    assert data["context"]["apply_error"] == "engine.rewind.no_entry_snapshot"
    assert data["context"]["group_work"][0]["branch"] == "impl/f1/G1"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
