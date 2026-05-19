"""Tests for the unified interaction-call primitives (G2).

Covers :mod:`se3.engine.interaction_calls` — writing ``kind``-tagged call
files, reading their ``.response`` answers, and draining mid-run
interjection requests — plus ``run.py``'s no-TTY step-failure path.
"""

import json
import threading
import time
from types import SimpleNamespace

import pytest

from se3.engine.interaction_calls import (
    KIND_CLI_CONFIRM,
    KIND_RETRY_DECISION,
    drain_interjection_requests,
    read_call_response,
    write_interaction_call,
)


# -- write_interaction_call -------------------------------------------------


def test_write_interaction_call_contains_kind_prompt_context_options(tmp_path):
    call_file = write_interaction_call(
        tmp_path,
        KIND_RETRY_DECISION,
        prompt="Step failed — choose how to proceed.",
        context={"step_type": "implement", "error": "boom"},
        options=[{"value": "retry", "label": "Retry"}],
    )

    assert call_file.exists()
    assert call_file.parent == tmp_path / "se3" / "calls"
    data = json.loads(call_file.read_text(encoding="utf-8"))
    assert data["kind"] == KIND_RETRY_DECISION
    assert data["prompt"] == "Step failed — choose how to proceed."
    assert data["context"] == {"step_type": "implement", "error": "boom"}
    assert data["options"] == [{"value": "retry", "label": "Retry"}]
    assert "created_at" in data and "call_id" in data


def test_write_interaction_call_defaults_empty_context_and_options(tmp_path):
    call_file = write_interaction_call(tmp_path, KIND_CLI_CONFIRM, prompt="Press 1")
    data = json.loads(call_file.read_text(encoding="utf-8"))
    assert data["context"] == {}
    assert data["options"] == []


def test_write_interaction_call_unique_filenames(tmp_path):
    a = write_interaction_call(tmp_path, KIND_RETRY_DECISION, prompt="a")
    b = write_interaction_call(tmp_path, KIND_RETRY_DECISION, prompt="b")
    assert a != b


def test_aggregator_parses_written_call_file(tmp_path):
    """The written file must be enumerated as a pending call by the daemon."""
    from se3.daemon.aggregator import DaemonAggregator

    write_interaction_call(
        tmp_path, KIND_RETRY_DECISION, prompt="decide", options=["retry"]
    )
    agg = DaemonAggregator()
    agg.add_project_root(tmp_path)
    snapshot = agg.get_snapshot()
    assert len(snapshot.pending_calls) == 1


# -- read_call_response -----------------------------------------------------


def test_read_call_response_returns_none_without_response(tmp_path):
    call_file = write_interaction_call(tmp_path, KIND_RETRY_DECISION, prompt="x")
    assert read_call_response(call_file) is None


def test_read_call_response_reads_plain_response(tmp_path):
    call_file = write_interaction_call(tmp_path, KIND_RETRY_DECISION, prompt="x")
    response_path = call_file.parent / f"{call_file.stem}.response"
    response_path.write_text(json.dumps({"choice": "retry"}), encoding="utf-8")

    result = read_call_response(call_file)
    assert result == {"choice": "retry"}


def test_read_call_response_reads_response_json_envelope(tmp_path):
    call_file = write_interaction_call(tmp_path, KIND_RETRY_DECISION, prompt="x")
    response_path = call_file.parent / f"{call_file.stem}.response.json"
    response_path.write_text(json.dumps({"choice": "skip"}), encoding="utf-8")

    assert read_call_response(call_file) == {"choice": "skip"}


def test_read_call_response_wraps_bare_scalar(tmp_path):
    call_file = write_interaction_call(tmp_path, KIND_RETRY_DECISION, prompt="x")
    response_path = call_file.parent / f"{call_file.stem}.response"
    response_path.write_text(json.dumps("retry"), encoding="utf-8")

    assert read_call_response(call_file) == {"response": "retry"}


# -- drain_interjection_requests --------------------------------------------


def _make_flow(current_step=None):
    state = SimpleNamespace(
        context={},
        get_current_step=lambda: current_step,
    )
    return SimpleNamespace(flow_id="flow-1", state=state)


def _write_request(project_root, name, payload):
    req_dir = project_root / "se3" / "interjections"
    req_dir.mkdir(parents=True, exist_ok=True)
    path = req_dir / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_drain_no_directory_returns_zero(tmp_path):
    flow = _make_flow()
    assert drain_interjection_requests(flow, tmp_path) == 0


def test_drain_folds_request_into_user_interjections(tmp_path):
    step = SimpleNamespace(step_id="04_implement", step_type="implement")
    flow = _make_flow(step)
    _write_request(tmp_path, "req1.json", {"text": "also handle edge cases"})

    count = drain_interjection_requests(flow, tmp_path)

    assert count == 1
    interjections = flow.state.context["user_interjections"]
    assert len(interjections) == 1
    entry = interjections[0]
    assert entry["text"] == "also handle edge cases"
    assert entry["step_id"] == "04_implement"
    assert entry["step_type"] == "implement"
    assert entry["timestamp"]


def test_drain_consumes_request_files(tmp_path):
    flow = _make_flow(SimpleNamespace(step_id="s", step_type="implement"))
    req = _write_request(tmp_path, "req1.json", {"text": "do more"})

    drain_interjection_requests(flow, tmp_path)

    assert not req.exists()


def test_drain_does_not_duplicate_on_second_call(tmp_path):
    flow = _make_flow(SimpleNamespace(step_id="s", step_type="implement"))
    _write_request(tmp_path, "req1.json", {"text": "once"})

    first = drain_interjection_requests(flow, tmp_path)
    second = drain_interjection_requests(flow, tmp_path)

    assert first == 1
    assert second == 0
    assert len(flow.state.context["user_interjections"]) == 1


def test_drain_skips_empty_text_but_still_consumes(tmp_path):
    flow = _make_flow(SimpleNamespace(step_id="s", step_type="implement"))
    req = _write_request(tmp_path, "empty.json", {"text": "   "})

    count = drain_interjection_requests(flow, tmp_path)

    assert count == 0
    assert not req.exists()
    assert flow.state.context.get("user_interjections", []) == []


def test_drain_discards_malformed_request(tmp_path):
    flow = _make_flow(SimpleNamespace(step_id="s", step_type="implement"))
    req_dir = tmp_path / "se3" / "interjections"
    req_dir.mkdir(parents=True)
    bad = req_dir / "bad.json"
    bad.write_text("{not json", encoding="utf-8")

    count = drain_interjection_requests(flow, tmp_path)

    assert count == 0
    assert not bad.exists()


def test_drain_accepts_bare_string_request(tmp_path):
    flow = _make_flow(SimpleNamespace(step_id="s", step_type="implement"))
    req_dir = tmp_path / "se3" / "interjections"
    req_dir.mkdir(parents=True)
    (req_dir / "r.json").write_text(json.dumps("hurry up"), encoding="utf-8")

    count = drain_interjection_requests(flow, tmp_path)

    assert count == 1
    assert flow.state.context["user_interjections"][0]["text"] == "hurry up"


# -- run.py no-TTY step-failure path ----------------------------------------


def test_handle_step_failure_noninteractive_resolves_via_response(tmp_path, monkeypatch):
    """The no-TTY path writes a retry_decision call file and polls for the answer."""
    from se3.commands import run as run_mod

    monkeypatch.setattr(run_mod, "_RETRY_DECISION_POLL_INTERVAL", 0.02)

    step = SimpleNamespace(
        step_id="04_implement",
        step_type=SimpleNamespace(value="implement"),
        error_message="kaboom",
        retry_count=1,
        status=None,
        outputs={},
    )
    flow = SimpleNamespace(flow_id="flow-1", status=None)
    persistence = SimpleNamespace(save_flow=lambda f: None)

    calls_dir = tmp_path / "se3" / "calls"

    def _responder():
        # Wait for the call file to appear, then answer it.
        for _ in range(200):
            if calls_dir.is_dir():
                files = [
                    p for p in calls_dir.iterdir()
                    if p.name.startswith("retry_decision_") and p.suffix == ".json"
                ]
                if files:
                    call_file = files[0]
                    (call_file.parent / f"{call_file.stem}.response").write_text(
                        json.dumps({"choice": "skip"}), encoding="utf-8"
                    )
                    return
            time.sleep(0.01)

    t = threading.Thread(target=_responder)
    t.start()
    try:
        decision = run_mod._handle_step_failure_noninteractive(
            flow, step, persistence, tmp_path
        )
    finally:
        t.join()

    assert decision == "skip"
    # Step was paused while awaiting the response.
    from se3.engine.models import StepStatus
    assert step.status == StepStatus.PAUSED


def test_handle_step_failure_noninteractive_unknown_response_aborts(tmp_path, monkeypatch):
    from se3.commands import run as run_mod

    monkeypatch.setattr(run_mod, "_RETRY_DECISION_POLL_INTERVAL", 0.02)

    step = SimpleNamespace(
        step_id="s",
        step_type=SimpleNamespace(value="test"),
        error_message="err",
        retry_count=0,
        status=None,
        outputs={},
    )
    flow = SimpleNamespace(flow_id="f", status=None)
    persistence = SimpleNamespace(save_flow=lambda f: None)
    calls_dir = tmp_path / "se3" / "calls"

    def _responder():
        for _ in range(200):
            if calls_dir.is_dir():
                files = [
                    p for p in calls_dir.iterdir()
                    if p.name.startswith("retry_decision_") and p.suffix == ".json"
                ]
                if files:
                    call_file = files[0]
                    (call_file.parent / f"{call_file.stem}.response").write_text(
                        json.dumps({"choice": "nonsense"}), encoding="utf-8"
                    )
                    return
            time.sleep(0.01)

    t = threading.Thread(target=_responder)
    t.start()
    try:
        decision = run_mod._handle_step_failure_noninteractive(
            flow, step, persistence, tmp_path
        )
    finally:
        t.join()

    assert decision == "abort"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
