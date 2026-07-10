"""Tests for the protocol revision 3 traffic-reduction messages.

Covers the four new constructors (`make_keepalive`, `make_history_index_delta`,
`make_detail_request`, `make_detail_data`), their encode/decode round-trips,
direction-set registration, the `supports_traffic_reduction` version gate, and
the unchanged rejection of genuinely unknown types.
"""

from __future__ import annotations

import pytest

from se3.daemon import protocol
from se3.daemon.protocol import (
    DETAIL_KIND_CALL,
    DETAIL_KIND_ISSUE,
    MSG_DETAIL_DATA,
    MSG_DETAIL_REQUEST,
    MSG_HISTORY_INDEX_DELTA,
    MSG_KEEPALIVE,
    ProtocolError,
    decode,
    supports_traffic_reduction,
)


# -- registration ---------------------------------------------------------


def test_new_types_registered_in_correct_directions():
    assert MSG_KEEPALIVE in protocol.DAEMON_TO_SERVER
    assert MSG_HISTORY_INDEX_DELTA in protocol.DAEMON_TO_SERVER
    assert MSG_DETAIL_DATA in protocol.DAEMON_TO_SERVER
    assert MSG_DETAIL_REQUEST in protocol.SERVER_TO_DAEMON
    for t in (MSG_KEEPALIVE, MSG_HISTORY_INDEX_DELTA, MSG_DETAIL_DATA, MSG_DETAIL_REQUEST):
        assert t in protocol.ALL_MESSAGE_TYPES
    # Direction isolation: a request only flows server->daemon, data only back.
    assert MSG_DETAIL_REQUEST not in protocol.DAEMON_TO_SERVER
    assert MSG_DETAIL_DATA not in protocol.SERVER_TO_DAEMON
    assert MSG_KEEPALIVE not in protocol.SERVER_TO_DAEMON


# -- version gate ---------------------------------------------------------


def test_supports_traffic_reduction_gate():
    assert supports_traffic_reduction("3") is True
    assert supports_traffic_reduction(3) is True
    assert supports_traffic_reduction("4") is True
    # Legacy peers (and garbage / missing versions) must degrade to full frames.
    assert supports_traffic_reduction("2") is False
    assert supports_traffic_reduction("1") is False
    assert supports_traffic_reduction("") is False
    assert supports_traffic_reduction(None) is False
    assert supports_traffic_reduction("not-a-number") is False


# -- make_keepalive -------------------------------------------------------


def test_make_keepalive_round_trip():
    msg = protocol.make_keepalive("abc123", seq=9)
    assert msg.type == MSG_KEEPALIVE
    assert msg.seq == 9
    decoded = decode(msg.to_json())
    assert decoded.type == MSG_KEEPALIVE
    assert decoded.payload["signature"] == "abc123"


def test_make_keepalive_defaults_empty_signature():
    msg = protocol.make_keepalive()
    assert msg.payload["signature"] == ""
    assert decode(msg.to_json()).payload["signature"] == ""


# -- make_history_index_delta ---------------------------------------------


def test_make_history_index_delta_round_trip():
    upserts = [
        {"flow_id": "f1", "status": "RUNNING", "active": True},
        {"flow_id": "f2", "status": "COMPLETED", "active": False},
    ]
    removed = ["f9", "f10"]
    msg = protocol.make_history_index_delta(upserts, removed, seq=4)
    assert msg.type == MSG_HISTORY_INDEX_DELTA
    assert msg.seq == 4
    decoded = decode(msg.to_json())
    assert decoded.payload["upserts"] == upserts
    assert decoded.payload["removed"] == removed


def test_make_history_index_delta_defaults_empty():
    msg = protocol.make_history_index_delta()
    assert msg.payload["upserts"] == []
    assert msg.payload["removed"] == []
    decoded = decode(msg.to_json())
    assert decoded.payload == {"upserts": [], "removed": []}


# -- make_detail_request --------------------------------------------------


def test_make_detail_request_issue_round_trip():
    msg = protocol.make_detail_request(
        DETAIL_KIND_ISSUE, "042", project_root="/proj", request_id="r1"
    )
    assert msg.type == MSG_DETAIL_REQUEST
    decoded = decode(msg.to_json())
    p = decoded.payload
    assert p["kind"] == DETAIL_KIND_ISSUE
    assert p["target_id"] == "042"
    assert p["project_root"] == "/proj"
    assert p["request_id"] == "r1"


def test_make_detail_request_omits_empty_optionals():
    msg = protocol.make_detail_request(DETAIL_KIND_CALL, "call-7")
    p = msg.payload
    assert p["kind"] == DETAIL_KIND_CALL
    assert p["target_id"] == "call-7"
    assert "project_root" not in p
    assert "request_id" not in p


def test_make_detail_request_rejects_bad_kind():
    with pytest.raises(ProtocolError):
        protocol.make_detail_request("bogus", "x")


# -- make_detail_data -----------------------------------------------------


def test_make_detail_data_success_round_trip():
    detail = {"id": "042", "description": "the full untruncated text " * 20}
    msg = protocol.make_detail_data("r1", DETAIL_KIND_ISSUE, detail=detail, seq=2)
    decoded = decode(msg.to_json())
    p = decoded.payload
    assert p["request_id"] == "r1"
    assert p["kind"] == DETAIL_KIND_ISSUE
    assert p["ok"] is True
    assert p["detail"] == detail
    assert "error" not in p


def test_make_detail_data_failure_omits_detail():
    msg = protocol.make_detail_data(
        "r2", DETAIL_KIND_CALL, ok=False, error="not found"
    )
    p = msg.payload
    assert p["ok"] is False
    assert p["error"] == "not found"
    assert "detail" not in p


def test_make_detail_data_rejects_bad_kind():
    with pytest.raises(ProtocolError):
        protocol.make_detail_data("r", "bogus")


# -- backward compatibility unchanged -------------------------------------


def test_unknown_type_still_rejected():
    raw = '{"type": "still_unknown", "seq": 0, "timestamp": 1.0, "payload": {}}'
    with pytest.raises(ProtocolError):
        decode(raw)
