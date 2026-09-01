"""Tests for the protocol revision 2 history messages and spawn_flow discover.

Covers the three history constructors (`make_history_index`,
`make_history_request`, `make_history_data`), encode/decode round-trips,
backward-compatible handling of unknown message types, and the new
`discover` field on `make_spawn_flow`.
"""

from __future__ import annotations

import pytest

from tianluo.daemon import protocol
from tianluo.daemon.protocol import (
    HISTORY_MODE_APPEND,
    HISTORY_MODE_FULL,
    MSG_HISTORY_DATA,
    MSG_HISTORY_INDEX,
    MSG_HISTORY_INDEX_REQUEST,
    MSG_HISTORY_REQUEST,
    Message,
    ProtocolError,
    decode,
)


def test_protocol_version_current():
    # Revision 3 added the traffic-reduction messages (keepalive / index-delta /
    # detail); the bump is what lets each side detect a legacy peer and fall
    # back to full-frame semantics. Revision 4 later added the (unrelated)
    # presence signalling, revision 5 the (likewise unrelated) upload channel,
    # revision 6 its fetch counterpart, revision 7 the optional spawn
    # implementation_strategy field, revision 8 the plan_decomposition /
    # plan_granularity pair that replaced it, and revision 9 the history
    # step-window channel — the by-step-block range read that lets the server
    # answer a windowed WebUI open without pulling (and caching) a whole
    # multi-hundred-MB flow; pin the current revision here.
    assert protocol.PROTOCOL_VERSION == "9"


def test_history_message_types_registered():
    assert MSG_HISTORY_INDEX in protocol.DAEMON_TO_SERVER
    assert MSG_HISTORY_DATA in protocol.DAEMON_TO_SERVER
    assert MSG_HISTORY_REQUEST in protocol.SERVER_TO_DAEMON
    assert MSG_HISTORY_INDEX in protocol.ALL_MESSAGE_TYPES
    assert MSG_HISTORY_DATA in protocol.ALL_MESSAGE_TYPES
    assert MSG_HISTORY_REQUEST in protocol.ALL_MESSAGE_TYPES


def test_history_index_request_type_registered():
    # server -> daemon only; never sent the other way.
    assert MSG_HISTORY_INDEX_REQUEST in protocol.SERVER_TO_DAEMON
    assert MSG_HISTORY_INDEX_REQUEST in protocol.ALL_MESSAGE_TYPES
    assert MSG_HISTORY_INDEX_REQUEST not in protocol.DAEMON_TO_SERVER


def test_make_history_index_request_round_trip():
    msg = protocol.make_history_index_request(seq=5)
    assert msg.type == MSG_HISTORY_INDEX_REQUEST
    assert msg.seq == 5
    # No flow dimension — the payload is empty; it only triggers a re-push.
    assert msg.payload == {}

    decoded = decode(msg.to_json())
    assert decoded.type == MSG_HISTORY_INDEX_REQUEST
    assert decoded.payload == {}


# -- make_history_index ---------------------------------------------------


def test_make_history_index_round_trip():
    sessions = [
        {"flow_id": "f1", "task": "do X", "status": "COMPLETED", "active": False},
        {"flow_id": "f2", "task": "do Y", "status": "RUNNING", "active": True},
    ]
    msg = protocol.make_history_index(sessions, seq=3)
    assert msg.type == MSG_HISTORY_INDEX
    assert msg.seq == 3

    decoded = decode(msg.to_json())
    assert decoded.type == MSG_HISTORY_INDEX
    assert decoded.payload["sessions"] == sessions


# -- make_history_request -------------------------------------------------


def test_make_history_request_full_snapshot():
    msg = protocol.make_history_request("f1", project_root="/p")
    assert msg.type == MSG_HISTORY_REQUEST
    decoded = decode(msg.to_json())
    assert decoded.payload["flow_id"] == "f1"
    assert decoded.payload["project_root"] == "/p"
    assert decoded.payload["cursor"] == {}


def test_make_history_request_with_cursor():
    cursor = {"05_test": 1024, "07_self_check": 2048}
    msg = protocol.make_history_request("f1", project_root="/p", cursor=cursor)
    decoded = decode(msg.to_json())
    assert decoded.payload["cursor"] == cursor


# -- make_history_data ----------------------------------------------------


def test_make_history_data_full_round_trip():
    records = [{"role": "user", "content": "hi"}]
    msg = protocol.make_history_data("f1", HISTORY_MODE_FULL, records)
    assert msg.type == MSG_HISTORY_DATA
    decoded = decode(msg.to_json())
    assert decoded.payload["flow_id"] == "f1"
    assert decoded.payload["mode"] == HISTORY_MODE_FULL
    assert decoded.payload["records"] == records
    assert decoded.payload["cursor"] == {}


def test_make_history_data_append_with_cursor():
    records = [{"role": "assistant", "content": "ok"}]
    cursor = {"04_implement": 4096}
    msg = protocol.make_history_data(
        "f1", HISTORY_MODE_APPEND, records, cursor=cursor, seq=7
    )
    decoded = decode(msg.to_json())
    assert decoded.seq == 7
    assert decoded.payload["mode"] == HISTORY_MODE_APPEND
    assert decoded.payload["records"] == records
    assert decoded.payload["cursor"] == cursor


def test_make_history_data_rejects_bad_mode():
    with pytest.raises(ProtocolError):
        protocol.make_history_data("f1", "snapshot", [])


# -- backward compatibility ----------------------------------------------


def test_decode_unknown_type_raises_protocol_error():
    """An older/newer peer that emits an unrecognized type is rejected
    cleanly via ProtocolError rather than crashing the decoder."""
    raw = '{"type": "future_message", "seq": 0, "timestamp": 1.0, "payload": {}}'
    with pytest.raises(ProtocolError):
        decode(raw)


def test_decode_unknown_type_is_catchable():
    """Callers can tolerate unknown frames by catching ProtocolError, so a
    new peer talking to an old one (or vice versa) does not crash."""
    raw = Message.to_json(
        Message(type=protocol.MSG_HISTORY_DATA, payload={})  # known
    )
    # Known type still decodes fine.
    assert decode(raw).type == protocol.MSG_HISTORY_DATA
    try:
        decode('{"type": "totally_unknown", "payload": {}}')
    except ProtocolError:
        pass
    else:  # pragma: no cover
        pytest.fail("expected ProtocolError for unknown type")


# -- make_spawn_flow discover field --------------------------------------


def test_make_spawn_flow_discover_defaults_false():
    msg = protocol.make_spawn_flow("Build X")
    assert msg.payload["discover"] is False
    decoded = decode(msg.to_json())
    assert decoded.payload["discover"] is False


def test_make_spawn_flow_discover_true():
    msg = protocol.make_spawn_flow("Explore Y", discover=True)
    decoded = decode(msg.to_json())
    assert decoded.payload["discover"] is True
    assert decoded.payload["task_description"] == "Explore Y"


# -- MSG_ISSUE_COMMAND -----------------------------------------------------


def test_issue_command_type_registered():
    from tianluo.daemon.protocol import MSG_ISSUE_COMMAND

    assert MSG_ISSUE_COMMAND in protocol.SERVER_TO_DAEMON
    assert MSG_ISSUE_COMMAND in protocol.ALL_MESSAGE_TYPES
    assert MSG_ISSUE_COMMAND not in protocol.DAEMON_TO_SERVER


def test_make_issue_command_create_round_trip():
    msg = protocol.make_issue_command(
        "create",
        project_root="/proj",
        description="Something is broken",
        title="Fix it",
        priority="high",
        type="bug",
        tags=["auto"],
    )
    from tianluo.daemon.protocol import MSG_ISSUE_COMMAND

    assert msg.type == MSG_ISSUE_COMMAND
    decoded = decode(msg.to_json())
    p = decoded.payload
    assert p["operation"] == "create"
    assert p["project_root"] == "/proj"
    assert p["description"] == "Something is broken"
    assert p["title"] == "Fix it"
    assert p["priority"] == "high"
    assert p["type"] == "bug"
    assert p["tags"] == ["auto"]


def test_make_issue_command_edit_round_trip():
    msg = protocol.make_issue_command(
        "edit",
        project_root="/proj",
        issue_id="042",
        description="Updated description",
    )
    decoded = decode(msg.to_json())
    p = decoded.payload
    assert p["operation"] == "edit"
    assert p["issue_id"] == "042"
    assert p["description"] == "Updated description"
    assert "title" not in p  # omitted when empty


def test_make_issue_command_close_with_reason():
    msg = protocol.make_issue_command(
        "close",
        project_root="/proj",
        issue_id="042",
        reason="Fixed in #123",
    )
    decoded = decode(msg.to_json())
    p = decoded.payload
    assert p["operation"] == "close"
    assert p["reason"] == "Fixed in #123"


def test_make_issue_command_reopen():
    msg = protocol.make_issue_command(
        "reopen",
        project_root="/proj",
        issue_id="042",
    )
    decoded = decode(msg.to_json())
    p = decoded.payload
    assert p["operation"] == "reopen"
    assert p["issue_id"] == "042"


def test_make_issue_command_omits_empty_optional_fields():
    msg = protocol.make_issue_command(
        "create",
        project_root="/proj",
        description="minimal",
    )
    p = msg.payload
    assert p["operation"] == "create"
    assert p["description"] == "minimal"
    # No extra keys for empty optionals
    assert "issue_id" not in p
    assert "title" not in p
    assert "priority" not in p
    assert "type" not in p
    assert "scope" not in p
    assert "tags" not in p
    assert "reason" not in p
# -- make_spawn_flow resume_flow_id field ---------------------------------


def test_make_spawn_flow_resume_flow_id_omitted_by_default():
    """Normal spawn does not include resume_flow_id in the wire payload."""
    msg = protocol.make_spawn_flow("Build X", project_root="/p")
    assert "resume_flow_id" not in msg.payload
    decoded = decode(msg.to_json())
    assert "resume_flow_id" not in decoded.payload


def test_make_spawn_flow_resume_flow_id_round_trip():
    """A resume payload carries resume_flow_id and preserves all fields."""
    msg = protocol.make_spawn_flow(
        "",  # task_description is unused for resume
        project_root="/proj",
        resume_flow_id="abc-123",
    )
    assert msg.payload["resume_flow_id"] == "abc-123"
    assert msg.payload["project_root"] == "/proj"
    # task_description is still present (empty string) for schema stability.
    assert msg.payload["task_description"] == ""

    decoded = decode(msg.to_json())
    assert decoded.payload["resume_flow_id"] == "abc-123"
    assert decoded.payload["project_root"] == "/proj"


def test_make_spawn_flow_resume_flow_id_falsey_values_not_included():
    """Empty string resume_flow_id is not added to the payload."""
    msg = protocol.make_spawn_flow("task", resume_flow_id="")
    assert "resume_flow_id" not in msg.payload


def test_make_history_data_usage_round_trip():
    """Revision 7: a full HISTORY_DATA frame may carry the usage payload."""
    usage = {
        "calls": [],
        "steps": {},
        "summary": {"actual_cost_usd": 0.5, "completeness": "complete"},
        "legacy": False,
        "completeness": "complete",
    }
    msg = protocol.make_history_data(
        "f1", protocol.HISTORY_MODE_FULL, [{"step_id": "s"}], usage=usage
    )
    decoded = decode(msg.to_json())
    assert decoded.payload["usage"] == usage


def test_make_history_data_omits_usage_by_default():
    """Pre-revision-7 peers never see the usage key."""
    msg = protocol.make_history_data(
        "f1", protocol.HISTORY_MODE_FULL, [{"step_id": "s"}]
    )
    assert "usage" not in msg.payload
    decoded = decode(msg.to_json())
    assert "usage" not in decoded.payload


def test_make_history_data_usage_catalog_round_trip():
    """The serialized pricing catalog rides usage-bearing frames (full OR
    append) so the server re-aggregates with the same table the daemon used."""
    catalog = {"version": "2026-08-13", "entries": {"claude-opus-5": {}}}
    msg = protocol.make_history_data(
        "f1", protocol.HISTORY_MODE_APPEND, [{"step_id": "s"}],
        usage_catalog=catalog,
    )
    decoded = decode(msg.to_json())
    assert decoded.payload["usage_catalog"] == catalog
    full = protocol.make_history_data(
        "f1", protocol.HISTORY_MODE_FULL, [{"step_id": "s"}],
        usage_catalog=catalog,
    )
    assert decode(full.to_json()).payload["usage_catalog"] == catalog


def test_make_history_data_omits_usage_catalog_by_default():
    """The catalog key is additive: frames without one stay byte-compatible."""
    msg = protocol.make_history_data(
        "f1", protocol.HISTORY_MODE_FULL, [{"step_id": "s"}]
    )
    assert "usage_catalog" not in msg.payload
    decoded = decode(msg.to_json())
    assert "usage_catalog" not in decoded.payload


def test_make_spawn_flow_strategy_round_trip():
    msg = protocol.make_spawn_flow(
        "Build X", project_root="/p", implementation_strategy="direct"
    )
    decoded = decode(msg.to_json())
    assert decoded.payload["implementation_strategy"] == "direct"
    # Omitted when empty, so a plain spawn payload stays byte-compatible.
    plain = protocol.make_spawn_flow("Build X", project_root="/p")
    assert "implementation_strategy" not in plain.payload
