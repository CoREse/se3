"""Step events stop inlining the payload the default render never reads.

A long flow's history is dominated by the engine's own step events, not by chat:
a ``step_completed`` for a check-class step carries a full StepState snapshot
whose ``inputs`` is the machine input handed TO the step. On the flow that
motivated this (``20260829-224712_878b4fc9``, its ``49_self_check`` terminal
record) that snapshot measured 10.8 MB of ``inputs`` — ``scope_diff`` alone
10.1 MB, plus ``test_results``, ``fix_history`` and the task description stored
under three names — against 38 KB of ``outputs``. The default render reads NONE
of it: the report card renders ``outputs``' structured result fields plus
status / error_message / token_usage, and ``inputs`` is reachable only behind
the "查看原始" chip.

So the payload joins the existing summarize/fetch-on-demand split. Asserted
here, in both directions:

* what the card and the usage chip read must still ride inline, byte for byte —
  ``outputs``, ``status``, ``error_message``, ``token_usage`` /
  ``usage_summary``, and the handful of ``inputs`` scalars the renderers do read
  (``fix_iteration`` / ``is_fix_iteration``, ``reviewer`` /
  ``step_to_review_*``);
* the held-back payload must come back whole through
  ``GET /api/history/{flow_id}/detail?source=step``, whose tool-body siblings
  (``raw`` / ``progress``) keep their existing parameters and reply shape.

The record shapes below mirror the real flow's layout key for key; only the
filler is synthetic, so the fixture stays a few KB instead of 9 MB.
"""

from __future__ import annotations

import json

import pytest

from _authsrv import authed_app, authed_hello, login
from tianluo.daemon import protocol
from tianluo.server.history_summary import (
    DETAIL_SOURCE_PROGRESS,
    DETAIL_SOURCE_RAW,
    DETAIL_SOURCE_STEP,
    DETAIL_VERSION_KEY,
    STEP_INPUTS_LAZY_KEY,
    STEP_INPUT_INLINE_KEYS,
    locate_record_detail,
    summarize_history_records,
)

FLOW = "flow-step-payload"
MACHINE = "m1"
STEP = "49_self_check_815ee905"

#: Stand-ins for the real record's three heavyweights, at ~1/1000 scale.
SCOPE_DIFF = "\n".join(
    "+    line %04d of a scope diff hunk" % i for i in range(300)
)
TEST_RESULTS_TAIL = "\n".join("FAILED tests/test_%03d.py" % i for i in range(80))
FIX_HISTORY_NOTE = "round note " * 200
TASK_DESCRIPTION = "把 WebUI flow 历史视图改为尾部起步的窗口化加载。" * 40


def _outputs(**extra):
    """The self_check card's real output set (the part that must stay inline)."""
    out = {
        "issues": [{"severity": "high", "description": "an actionable finding"}],
        "actionable_count": 1,
        "self_check_result": {"passed": False},
        "fix_iteration": 2,
        "token_usage": {"input_tokens": 12000, "output_tokens": 900},
        "usage_summary": {"total_cost": 1.5, "records": 3},
    }
    out.update(extra)
    return out


def _inputs(**extra):
    """The real snapshot's ``inputs``, at scale, with its allowlisted scalars."""
    out = {
        "task_description": TASK_DESCRIPTION,
        "task_description_base": TASK_DESCRIPTION,
        "adjudicated_description": TASK_DESCRIPTION,
        "scope_diff": SCOPE_DIFF,
        "test_results": {"passed": False, "output_tail": TEST_RESULTS_TAIL},
        "fix_history": [{"round": 1, "note": FIX_HISTORY_NOTE}],
        "fix_iteration": 2,
        "is_fix_iteration": True,
        "reviewer": "dclaude",
        "step_to_review_type": "plan",
        "step_to_review_id": "03_plan_81d6fbca",
    }
    out.update(extra)
    return out


def _step_event(
    ordinal=0,
    event_type="step_completed",
    step_type="self_check",
    inputs=None,
    outputs=None,
    status="completed",
    error_message="",
    step_id=STEP,
):
    """One engine step event, in the daemon's ``{…, message: …}`` envelope."""
    return {
        "step_id": step_id,
        "step_type": step_type,
        "ordinal": ordinal,
        "message": {
            "type": event_type,
            "step_id": step_id,
            "step_type": step_type,
            "timestamp": "2026-08-29T23:41:12",
            "data": {
                "step": {
                    "step_id": step_id,
                    "step_type": step_type,
                    "status": status,
                    "started_at": "2026-08-29T23:40:00",
                    "completed_at": "2026-08-29T23:41:12",
                    "retry_count": 0,
                    "inputs": _inputs() if inputs is None else inputs,
                    "outputs": _outputs() if outputs is None else outputs,
                    "error_message": error_message,
                },
            },
        },
    }


def _shaped_step(record):
    """The ``data.step`` of *record* after the browser-side shaping."""
    return summarize_history_records([record], FLOW)[0]["message"]["data"]["step"]


# --------------------------------------------------------------------------
# pure shaping
# --------------------------------------------------------------------------


def test_step_inputs_are_held_back_and_marked():
    record = _step_event()
    out = summarize_history_records([record], FLOW)[0]
    message = out["message"]
    assert message[STEP_INPUTS_LAZY_KEY] is True
    assert message["detail_flow"] == FLOW
    assert message[DETAIL_VERSION_KEY]
    inputs = message["data"]["step"]["inputs"]
    for key in ("scope_diff", "test_results", "fix_history",
                "task_description", "task_description_base",
                "adjudicated_description"):
        assert key not in inputs, key
    # And it is a real saving, not a re-shuffle.
    before = len(json.dumps(record, separators=(",", ":")))
    after = len(json.dumps(out, separators=(",", ":")))
    assert after * 10 < before, (before, after)


def test_the_report_card_fields_still_ride_inline():
    """Requirement: the default report card is byte-identical."""
    record = _step_event()
    step = _shaped_step(record)
    original = record["message"]["data"]["step"]
    assert step["outputs"] == original["outputs"]
    assert step["status"] == original["status"]
    assert step["error_message"] == original["error_message"]
    # ... including the two usage payloads the footnote reads.
    assert step["outputs"]["token_usage"] == {
        "input_tokens": 12000, "output_tokens": 900,
    }
    assert step["outputs"]["usage_summary"] == {"total_cost": 1.5, "records": 3}


def test_the_inputs_keys_the_card_reads_ride_inline():
    """``implementFixIteration`` / ``renderConfirmReport`` read these."""
    step = _shaped_step(_step_event())
    inputs = step["inputs"]
    assert set(inputs) == set(STEP_INPUT_INLINE_KEYS)
    assert inputs["fix_iteration"] == 2
    assert inputs["is_fix_iteration"] is True
    assert inputs["reviewer"] == "dclaude"
    assert inputs["step_to_review_type"] == "plan"
    assert inputs["step_to_review_id"] == "03_plan_81d6fbca"


def test_a_step_failed_record_is_shaped_the_same_way():
    record = _step_event(
        event_type="step_failed", status="failed",
        error_message="the step blew up",
    )
    out = summarize_history_records([record], FLOW)[0]
    step = out["message"]["data"]["step"]
    assert out["message"][STEP_INPUTS_LAZY_KEY] is True
    assert "scope_diff" not in step["inputs"]
    # The failure message is the card's headline — never held back.
    assert step["error_message"] == "the step blew up"


def test_a_step_output_record_keeps_its_usage_chip():
    """``step_output`` renders a usage-only chip; that is all it may need."""
    record = _step_event(
        event_type="step_output", status="paused", step_type="implement",
    )
    out = summarize_history_records([record], FLOW)[0]
    step = out["message"]["data"]["step"]
    assert out["message"][STEP_INPUTS_LAZY_KEY] is True
    assert "scope_diff" not in step["inputs"]
    assert step["outputs"]["token_usage"]["input_tokens"] == 12000
    assert step["outputs"]["usage_summary"]["total_cost"] == 1.5


def test_a_small_step_event_rides_inline():
    """Benefit rule (b): holding back less than the markers cost is a LOSS."""
    record = _step_event(inputs={"fix_iteration": 1, "flow_id": "f"})
    assert summarize_history_records([record], FLOW)[0] is record
    assert STEP_INPUTS_LAZY_KEY not in record["message"]


def test_a_step_event_without_inputs_is_left_alone():
    record = _step_event(inputs={})
    assert summarize_history_records([record], FLOW)[0] is record


def test_an_unaddressable_step_event_is_never_lazified():
    """No ``(step_id, ordinal)`` address means nothing could fetch it back."""
    record = _step_event()
    record.pop("ordinal")
    assert summarize_history_records([record], FLOW)[0] is record


def test_shaping_does_not_mutate_the_cached_record():
    """The cache still holds the full bundle — that is what serves the detail."""
    record = _step_event()
    summarize_history_records([record], FLOW)
    assert record["message"]["data"]["step"]["inputs"]["scope_diff"] == SCOPE_DIFF
    assert STEP_INPUTS_LAZY_KEY not in record["message"]


def test_the_detail_version_moves_when_the_held_back_inputs_change():
    """A rewrite under the SAME address must not be answered from the cache."""
    first = _step_event()
    second = _step_event(inputs=_inputs(scope_diff=SCOPE_DIFF + "\n+ one more"))
    v1 = summarize_history_records([first], FLOW)[0]["message"][DETAIL_VERSION_KEY]
    v2 = summarize_history_records([second], FLOW)[0]["message"][DETAIL_VERSION_KEY]
    assert v1 != v2


# --------------------------------------------------------------------------
# detail extraction (pure)
# --------------------------------------------------------------------------


def test_locate_returns_the_whole_original_record():
    record = _step_event(ordinal=4)
    found = locate_record_detail(
        [record], step_id=STEP, ordinal=4, tool_use_id="",
        source=DETAIL_SOURCE_STEP,
    )
    assert found["record_found"] is True
    detail = found["detail"]
    assert detail["source"] == DETAIL_SOURCE_STEP
    assert detail["inputs"] == record["message"]["data"]["step"]["inputs"]
    # The reply IS the cached message, so "View raw" can print it unchanged.
    assert detail["record"] == record["message"]


def test_locate_is_scoped_to_the_addressed_record():
    records = [
        _step_event(ordinal=0, inputs=_inputs(scope_diff="FIRST " + SCOPE_DIFF)),
        _step_event(ordinal=1, inputs=_inputs(scope_diff="SECOND " + SCOPE_DIFF)),
    ]
    for ordinal, expected in ((0, "FIRST"), (1, "SECOND")):
        found = locate_record_detail(
            records, step_id=STEP, ordinal=ordinal, tool_use_id="",
            source=DETAIL_SOURCE_STEP,
        )
        assert found["detail"]["inputs"]["scope_diff"].startswith(expected)


def test_a_non_step_record_has_no_step_detail():
    record = {
        "step_id": STEP,
        "step_type": "implement",
        "ordinal": 0,
        "message": {"role": "assistant", "content": "hi", "raw_json": []},
    }
    found = locate_record_detail(
        [record], step_id=STEP, ordinal=0, tool_use_id="",
        source=DETAIL_SOURCE_STEP,
    )
    assert found["record_found"] is True
    assert found["detail"] is None


def test_the_tool_sources_still_require_an_id():
    """The existing tool-body semantics are untouched by the new source."""
    record = _step_event()
    for source in (DETAIL_SOURCE_PROGRESS, DETAIL_SOURCE_RAW):
        found = locate_record_detail(
            [record], step_id=STEP, ordinal=0, tool_use_id="", source=source,
        )
        assert found == {"detail": None, "record_found": False, "passed": False}


# --------------------------------------------------------------------------
# the detail endpoint
# --------------------------------------------------------------------------


@pytest.fixture()
def client_and_app():
    from fastapi.testclient import TestClient

    app, _key = authed_app()
    with TestClient(app) as client:
        login(client)
        yield client, app


def _seed(client, app, records, flow=FLOW):
    daemon = client.websocket_connect("/ws")
    sock = daemon.__enter__()
    sock.send_text(authed_hello(app, MACHINE, "host", "6.4.0"))
    protocol.decode(sock.receive_text())  # WELCOME
    sock.send_text(protocol.make_history_index([{"flow_id": flow}]).to_json())
    sock.send_text(
        protocol.make_history_data(
            flow, protocol.HISTORY_MODE_FULL, records
        ).to_json()
    )
    for _ in range(50):
        resp = client.get("/api/history/%s" % flow)
        if resp.status_code == 200 and resp.json().get("cached"):
            return daemon, sock, resp
    daemon.__exit__(None, None, None)
    raise AssertionError("bundle never became cache-visible")


def test_bundle_response_no_longer_inlines_the_step_payload(client_and_app):
    client, app = client_and_app
    records = [_step_event(ordinal=0)]
    daemon, _sock, resp = _seed(client, app, records)
    try:
        shipped = resp.json()["records"][0]["message"]
        assert shipped[STEP_INPUTS_LAZY_KEY] is True
        assert "scope_diff" not in shipped["data"]["step"]["inputs"]
        # ... while the card's own fields arrive intact.
        assert shipped["data"]["step"]["outputs"] == _outputs()
        before = len(json.dumps(records, separators=(",", ":")))
        after = len(json.dumps(resp.json()["records"], separators=(",", ":")))
        assert after * 10 < before, (before, after)
    finally:
        daemon.__exit__(None, None, None)


def test_detail_endpoint_returns_the_full_step_payload(client_and_app):
    client, app = client_and_app
    daemon, _sock, _resp = _seed(client, app, [_step_event(ordinal=3)])
    try:
        resp = client.get(
            "/api/history/%s/detail" % FLOW,
            # No tool_use_id: a step event holds no tool call.
            params={"step_id": STEP, "ordinal": 3,
                    "source": DETAIL_SOURCE_STEP},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["source"] == DETAIL_SOURCE_STEP
        assert body["inputs"]["scope_diff"] == SCOPE_DIFF
        assert body["inputs"]["test_results"]["output_tail"] == TEST_RESULTS_TAIL
        assert body["inputs"]["fix_history"][0]["note"] == FIX_HISTORY_NOTE
        # The whole original message comes back, so "View raw" prints the
        # record as recorded rather than the summary it was handed.
        assert body["record"]["data"]["step"]["inputs"]["scope_diff"] == SCOPE_DIFF
        assert STEP_INPUTS_LAZY_KEY not in body["record"]
        assert "detail_flow" not in body["record"]
    finally:
        daemon.__exit__(None, None, None)


def test_detail_endpoint_still_rejects_a_tool_request_without_an_id(
    client_and_app,
):
    """The new source must not loosen the tool sources' own contract."""
    client, app = client_and_app
    daemon, _sock, _resp = _seed(client, app, [_step_event(ordinal=0)])
    try:
        for source in (DETAIL_SOURCE_PROGRESS, DETAIL_SOURCE_RAW):
            resp = client.get(
                "/api/history/%s/detail" % FLOW,
                params={"step_id": STEP, "ordinal": 0, "source": source},
            )
            assert resp.status_code == 422, (source, resp.text)
        # ... and the address is still required for the step source too.
        assert client.get(
            "/api/history/%s/detail" % FLOW,
            params={"source": DETAIL_SOURCE_STEP},
        ).status_code == 422
    finally:
        daemon.__exit__(None, None, None)


def test_detail_endpoint_404s_a_record_that_holds_no_step_payload(
    client_and_app,
):
    client, app = client_and_app
    records = [
        _step_event(ordinal=0),
        {
            "step_id": STEP,
            "step_type": "self_check",
            "ordinal": 1,
            "message": {"role": "assistant", "content": "hi", "raw_json": []},
        },
    ]
    daemon, _sock, _resp = _seed(client, app, records)
    try:
        resp = client.get(
            "/api/history/%s/detail" % FLOW,
            params={"step_id": STEP, "ordinal": 1,
                    "source": DETAIL_SOURCE_STEP},
        )
        assert resp.status_code == 404, resp.text
    finally:
        daemon.__exit__(None, None, None)


def test_the_cache_still_holds_the_full_step_payload(client_and_app):
    """The daemon→server leg is untouched; only the wire copy is shaped."""
    client, app = client_and_app
    daemon, _sock, _resp = _seed(client, app, [_step_event(ordinal=0)])
    try:
        cached = app.state.server_state._history_data[FLOW]["records"]
        step = cached[0]["message"]["data"]["step"]
        assert step["inputs"]["scope_diff"] == SCOPE_DIFF
    finally:
        daemon.__exit__(None, None, None)
