"""Protocol revisions 4 and 5 — presence signalling and the upload channel.

Revision 4 covers the MSG_VIEWERS edge message and its constructor, plus the
optional ``viewers`` level field piggybacked on MSG_PING (byte-compatible with
revision 3 when absent) and the ``supports_presence`` negotiation gate that
keeps a daemon at full speed against any pre-presence server.

Revision 5 covers the upload channel (MSG_UPLOAD_COMMAND / MSG_UPLOAD_RESULT),
its shared MAX_UPLOAD_BYTES limit and UPLOAD_ERROR_CODES contract, the version
bump to "5", and the ``supports_uploads`` gate that lets the server refuse an
upload to a pre-upload daemon up front instead of stalling the browser on a
timeout.

Also covers the additive project-registry management pair (MSG_PROJECT_COMMAND
/ MSG_PROJECT_RESULT), which deliberately rode on revision 4 without a bump.
"""

from __future__ import annotations

import pytest

from tianluo.daemon import protocol
from tianluo.daemon.protocol import (
    MAX_UPLOAD_BYTES,
    MSG_PING,
    MSG_PROJECT_COMMAND,
    MSG_PROJECT_RESULT,
    MSG_UPLOAD_COMMAND,
    MSG_UPLOAD_RESULT,
    MSG_VIEWERS,
    ProtocolError,
    make_ping,
    make_project_command,
    make_project_result,
    make_upload_command,
    make_upload_result,
    make_viewers,
    supports_presence,
    supports_traffic_reduction,
    supports_uploads,
)


# -- version bump ----------------------------------------------------------


def test_protocol_version_bumped_to_5():
    # Revision 5 added the upload channel; the bump is what lets the server
    # tell "this daemon can store an attached file" from "this daemon will
    # ignore the frame", so a paste against an old daemon fails immediately
    # with an explainable error instead of waiting out a timeout.
    assert protocol.PROTOCOL_VERSION == "5"


def test_revision_5_does_not_regress_earlier_gates():
    # A revision-5 peer must still satisfy every older gate — the bump adds a
    # capability, it never withdraws one.
    assert supports_traffic_reduction("5") is True
    assert supports_presence("5") is True
    assert supports_traffic_reduction(protocol.PROTOCOL_VERSION) is True
    assert supports_presence(protocol.PROTOCOL_VERSION) is True
    assert supports_uploads(protocol.PROTOCOL_VERSION) is True


# -- supports_presence gate --------------------------------------------------


def test_supports_presence_gate():
    assert supports_presence("4") is True
    assert supports_presence(4) is True
    assert supports_presence("5") is True
    assert supports_presence(" 4 ") is True

    assert supports_presence("3") is False
    assert supports_presence(3) is False
    assert supports_presence("2") is False
    assert supports_presence("") is False
    assert supports_presence(None) is False
    assert supports_presence("not-a-number") is False
    assert supports_presence({}) is False


# -- MSG_VIEWERS registration & round trip -----------------------------------


def test_viewers_registered_as_server_to_daemon():
    assert MSG_VIEWERS == "viewers"
    assert MSG_VIEWERS in protocol.SERVER_TO_DAEMON
    assert MSG_VIEWERS in protocol.ALL_MESSAGE_TYPES
    assert MSG_VIEWERS not in protocol.DAEMON_TO_SERVER


def test_make_viewers_round_trip():
    msg = make_viewers(2, seq=7)
    assert msg.type == MSG_VIEWERS
    assert msg.payload == {"count": 2}
    assert msg.seq == 7

    decoded = protocol.decode(msg.to_json())
    assert decoded.type == MSG_VIEWERS
    assert decoded.payload == {"count": 2}
    assert decoded.seq == 7


def test_make_viewers_zero_count_is_carried():
    # count == 0 is the "last browser closed" edge — the whole point of the
    # message — so it must be present on the wire, never omitted as falsy.
    msg = make_viewers(0)
    assert msg.payload == {"count": 0}
    decoded = protocol.decode(msg.to_json())
    assert decoded.payload["count"] == 0


def test_make_viewers_coerces_count_to_int():
    assert make_viewers(True).payload == {"count": 1}
    assert make_viewers(3.0).payload == {"count": 3}


# -- PING viewers level field -------------------------------------------------


def test_make_ping_without_viewers_matches_revision_3_payload():
    # None must omit the field entirely: the payload stays byte-identical to
    # the revision-3 PING so older daemons see exactly the frames they always
    # did.
    msg = make_ping(seq=5)
    assert msg.type == MSG_PING
    assert msg.payload == {}
    assert msg.seq == 5
    assert '"payload": {}' in msg.to_json()


def test_make_ping_with_viewers_round_trip():
    msg = make_ping(seq=9, viewers=3)
    assert msg.payload == {"viewers": 3}
    decoded = protocol.decode(msg.to_json())
    assert decoded.type == MSG_PING
    assert decoded.payload == {"viewers": 3}
    assert decoded.seq == 9


def test_make_ping_viewers_zero_is_carried():
    # 0 is a meaningful level ("nobody watching"), not an absence — it is the
    # self-heal path for a lost 1→0 edge, so it must survive the wire.
    msg = make_ping(viewers=0)
    assert msg.payload == {"viewers": 0}
    decoded = protocol.decode(msg.to_json())
    assert decoded.payload["viewers"] == 0


def test_make_ping_coerces_viewers_to_int():
    assert make_ping(viewers=2.0).payload == {"viewers": 2}


# -- project-registry management messages ------------------------------------


def test_project_messages_registered_in_correct_directions():
    assert MSG_PROJECT_COMMAND == "project_command"
    assert MSG_PROJECT_RESULT == "project_result"

    assert MSG_PROJECT_COMMAND in protocol.SERVER_TO_DAEMON
    assert MSG_PROJECT_COMMAND not in protocol.DAEMON_TO_SERVER
    assert MSG_PROJECT_RESULT in protocol.DAEMON_TO_SERVER
    assert MSG_PROJECT_RESULT not in protocol.SERVER_TO_DAEMON

    assert MSG_PROJECT_COMMAND in protocol.ALL_MESSAGE_TYPES
    assert MSG_PROJECT_RESULT in protocol.ALL_MESSAGE_TYPES


def test_project_management_did_not_bump_protocol_version():
    # The pair is additive the way MSG_END_SESSION was: an older peer that does
    # not know the type just ignores the frame, and nothing existing degrades —
    # so it rode on revision 4 without a bump of its own. The revision has since
    # advanced to 5 for the (unrelated) upload channel, so pin only the fact
    # that these types exist below the current revision.
    assert protocol.PROTOCOL_VERSION == "5"
    assert supports_presence(protocol.PROTOCOL_VERSION) is True


def test_project_operations_set():
    assert protocol.PROJECT_OP_ADD == "add"
    assert protocol.PROJECT_OP_REMOVE == "remove"
    assert protocol.PROJECT_OPERATIONS == frozenset({"add", "remove"})


@pytest.mark.parametrize("operation", ["add", "remove"])
def test_make_project_command_round_trip(operation):
    msg = make_project_command(operation, "/srv/proj", request_id="req-1")
    assert msg.type == MSG_PROJECT_COMMAND

    decoded = protocol.decode(msg.to_json())
    assert decoded.type == MSG_PROJECT_COMMAND
    assert decoded.payload == {
        "operation": operation,
        "project_root": "/srv/proj",
        "request_id": "req-1",
    }


def test_make_project_command_omits_empty_request_id():
    msg = make_project_command("add", "/srv/proj")
    assert msg.payload == {"operation": "add", "project_root": "/srv/proj"}
    assert "request_id" not in msg.payload


@pytest.mark.parametrize("operation", ["", "list", "delete", "ADD", None])
def test_make_project_command_rejects_unknown_operation(operation):
    with pytest.raises(ProtocolError):
        make_project_command(operation, "/srv/proj")


def test_make_project_result_success_round_trip():
    msg = make_project_result("req-2", ok=True, project_root="/srv/proj")
    assert msg.type == MSG_PROJECT_RESULT

    decoded = protocol.decode(msg.to_json())
    assert decoded.type == MSG_PROJECT_RESULT
    assert decoded.payload == {
        "request_id": "req-2",
        "ok": True,
        "project_root": "/srv/proj",
    }


def test_make_project_result_omits_empty_optional_fields():
    # Empty optionals must be absent, not present-as-"" — the server tells
    # "no code reported" from "code is the empty string" by key presence.
    msg = make_project_result("req-3")
    assert msg.payload == {"request_id": "req-3", "ok": True}
    for key in ("error", "error_code", "project_root"):
        assert key not in msg.payload


def test_make_project_result_failure_carries_code_round_trip():
    msg = make_project_result(
        "req-4",
        ok=False,
        error="Project /srv/proj has a running flow",
        error_code="live_flow",
    )
    decoded = protocol.decode(msg.to_json())
    assert decoded.payload == {
        "request_id": "req-4",
        "ok": False,
        "error": "Project /srv/proj has a running flow",
        "error_code": "live_flow",
    }


# -- upload channel (protocol revision 5) ------------------------------------


def test_supports_uploads_gate():
    assert supports_uploads("5") is True
    assert supports_uploads(5) is True
    assert supports_uploads("6") is True
    assert supports_uploads(" 5 ") is True

    # Fail closed: anything we cannot read as ">= 5" means "do not dispatch".
    assert supports_uploads("4") is False
    assert supports_uploads(4) is False
    assert supports_uploads("2") is False
    assert supports_uploads("") is False
    assert supports_uploads(None) is False
    assert supports_uploads("abc") is False
    assert supports_uploads({}) is False


def test_upload_messages_registered_in_correct_directions():
    assert MSG_UPLOAD_COMMAND == "upload_command"
    assert MSG_UPLOAD_RESULT == "upload_result"

    assert MSG_UPLOAD_COMMAND in protocol.SERVER_TO_DAEMON
    assert MSG_UPLOAD_COMMAND not in protocol.DAEMON_TO_SERVER
    assert MSG_UPLOAD_RESULT in protocol.DAEMON_TO_SERVER
    assert MSG_UPLOAD_RESULT not in protocol.SERVER_TO_DAEMON

    # ALL_MESSAGE_TYPES is derived as the union, so both must appear without
    # anyone having hand-maintained a third list.
    assert MSG_UPLOAD_COMMAND in protocol.ALL_MESSAGE_TYPES
    assert MSG_UPLOAD_RESULT in protocol.ALL_MESSAGE_TYPES


def test_max_upload_bytes_is_20mb_and_fits_a_ws_frame():
    assert MAX_UPLOAD_BYTES == 20 * 1024 * 1024
    # base64 inflates by ~4/3; the encoded payload plus JSON framing must still
    # sit well under the per-frame websocket cap.
    assert MAX_UPLOAD_BYTES * 4 // 3 < protocol.MAX_WS_MESSAGE_BYTES


def test_upload_error_codes_set():
    assert protocol.UPLOAD_ERROR_CODES == frozenset(
        {
            "invalid_path",
            "not_registered",
            "too_large",
            "invalid_filename",
            "invalid_payload",
            "write_failed",
            "unsupported",
        }
    )


def test_make_upload_command_round_trip():
    msg = make_upload_command(
        "/srv/proj", "shot.png", "aGVsbG8=", size=5, request_id="up-1"
    )
    assert msg.type == MSG_UPLOAD_COMMAND

    decoded = protocol.decode(msg.to_json())
    assert decoded.type == MSG_UPLOAD_COMMAND
    assert decoded.payload == {
        "project_root": "/srv/proj",
        "filename": "shot.png",
        "content_b64": "aGVsbG8=",
        "size": 5,
        "request_id": "up-1",
    }


def test_make_upload_command_omits_empty_request_id():
    msg = make_upload_command("/srv/proj", "a.txt", "", size=0)
    assert "request_id" not in msg.payload


def test_make_upload_command_accepts_empty_file():
    # A 0-byte attachment is legal; only *oversized* uploads are rejected.
    msg = make_upload_command("/srv/proj", "empty.log", "", size=0)
    assert msg.payload["size"] == 0


def test_make_upload_command_accepts_exactly_the_limit():
    msg = make_upload_command(
        "/srv/proj", "big.bin", "x", size=MAX_UPLOAD_BYTES
    )
    assert msg.payload["size"] == MAX_UPLOAD_BYTES


@pytest.mark.parametrize("filename", ["", "   ", None])
def test_make_upload_command_rejects_empty_filename(filename):
    with pytest.raises(ProtocolError):
        make_upload_command("/srv/proj", filename, "", size=0)


@pytest.mark.parametrize("project_root", ["", "relative/proj", "./proj", None])
def test_make_upload_command_rejects_non_absolute_project_root(project_root):
    with pytest.raises(ProtocolError):
        make_upload_command(project_root, "a.txt", "", size=0)


def test_make_upload_command_rejects_oversized_declared_size():
    with pytest.raises(ProtocolError):
        make_upload_command(
            "/srv/proj", "big.bin", "x", size=MAX_UPLOAD_BYTES + 1
        )


def test_make_upload_result_success_round_trip():
    msg = make_upload_result(
        "up-2",
        ok=True,
        path="tianluo/uploads/0123456789ab_shot.png",
        size=5,
        deduplicated=True,
    )
    assert msg.type == MSG_UPLOAD_RESULT

    decoded = protocol.decode(msg.to_json())
    assert decoded.type == MSG_UPLOAD_RESULT
    assert decoded.payload == {
        "request_id": "up-2",
        "ok": True,
        "path": "tianluo/uploads/0123456789ab_shot.png",
        "size": 5,
        "deduplicated": True,
    }


def test_make_upload_result_success_carries_zero_defaults():
    # size 0 and deduplicated False are real answers ("empty file", "freshly
    # written"), not absences — they must ride the wire rather than be dropped
    # as falsy, or the server cannot tell them from "not reported".
    msg = make_upload_result("up-3", ok=True, path="tianluo/uploads/ab_e.log")
    decoded = protocol.decode(msg.to_json())
    assert decoded.payload["size"] == 0
    assert decoded.payload["deduplicated"] is False


def test_make_upload_result_failure_carries_code_round_trip():
    msg = make_upload_result(
        "up-4",
        ok=False,
        error="/srv/proj is not a registered project",
        error_code="not_registered",
    )
    decoded = protocol.decode(msg.to_json())
    assert decoded.payload == {
        "request_id": "up-4",
        "ok": False,
        "error": "/srv/proj is not a registered project",
        "error_code": "not_registered",
    }


def test_make_upload_result_failure_omits_success_fields():
    # A failed upload has no path/size/deduplicated to report; emitting them at
    # their defaults would let a caller mistake a failure for a 0-byte success.
    msg = make_upload_result("up-5", ok=False, error_code="write_failed")
    for key in ("path", "size", "deduplicated"):
        assert key not in msg.payload


@pytest.mark.parametrize(
    "error_code", ["bogus", "live_flow", "TOO_LARGE", "not_found"]
)
def test_make_upload_result_rejects_unknown_error_code(error_code):
    with pytest.raises(ProtocolError):
        make_upload_result("up-6", ok=False, error_code=error_code)


@pytest.mark.parametrize("error_code", sorted(protocol.UPLOAD_ERROR_CODES))
def test_make_upload_result_accepts_every_declared_error_code(error_code):
    msg = make_upload_result("up-7", ok=False, error_code=error_code)
    assert msg.payload["error_code"] == error_code
