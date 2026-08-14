"""Protocol revisions 4-6 — presence, and the upload / fetch file channels.

Revision 4 covers the MSG_VIEWERS edge message and its constructor, plus the
optional ``viewers`` level field piggybacked on MSG_PING (byte-compatible with
revision 3 when absent) and the ``supports_presence`` negotiation gate that
keeps a daemon at full speed against any pre-presence server.

Revision 5 covers the upload channel (MSG_UPLOAD_COMMAND / MSG_UPLOAD_RESULT),
its shared MAX_UPLOAD_BYTES limit and UPLOAD_ERROR_CODES contract, the version
bump to "5", and the ``supports_uploads`` gate that lets the server refuse an
upload to a pre-upload daemon up front instead of stalling the browser on a
timeout.

Revision 6 covers the fetch channel (MSG_FETCH_COMMAND / MSG_FETCH_RESULT) that
reads an uploaded file back out, its FETCH_ERROR_CODES contract, the version
bump to "6", and the ``supports_fetch`` gate — the same up-front refusal as
uploads, needed more acutely because a fetch backs an inline thumbnail and so
repeats on every re-render.

Also covers the additive project-registry management pair (MSG_PROJECT_COMMAND
/ MSG_PROJECT_RESULT), which deliberately rode on revision 4 without a bump.
"""

from __future__ import annotations

import pytest

from tianluo.daemon import protocol
from tianluo.daemon.protocol import (
    MAX_UPLOAD_BYTES,
    MSG_FETCH_COMMAND,
    MSG_FETCH_RESULT,
    MSG_PING,
    MSG_PROJECT_COMMAND,
    MSG_PROJECT_RESULT,
    MSG_UPLOAD_COMMAND,
    MSG_UPLOAD_RESULT,
    MSG_VIEWERS,
    ProtocolError,
    make_fetch_command,
    make_fetch_result,
    make_ping,
    make_project_command,
    make_project_result,
    make_upload_command,
    make_upload_result,
    make_viewers,
    supports_fetch,
    supports_presence,
    supports_spawn_strategy,
    supports_traffic_reduction,
    supports_uploads,
)


# -- version bump ----------------------------------------------------------


def test_protocol_version_bumped_to_7():
    # Revision 5 added the upload channel, revision 6 the fetch channel that
    # reads those files back, and revision 7 the optional spawn
    # ``implementation_strategy`` field. Each bump is what lets the server tell
    # "this daemon can serve the frame" from "this daemon will ignore it", so a
    # paste (or a thumbnail, or an explicit strategy) against an old daemon
    # fails immediately with an explainable error instead of waiting out a
    # timeout — or, for the strategy field, silently running a different
    # strategy than the operator requested.
    assert protocol.PROTOCOL_VERSION == "7"


def test_revision_7_does_not_regress_earlier_gates():
    # A revision-7 peer must still satisfy every older gate — a bump adds a
    # capability, it never withdraws one.
    assert supports_traffic_reduction("6") is True
    assert supports_presence("6") is True
    assert supports_uploads("6") is True
    assert supports_traffic_reduction(protocol.PROTOCOL_VERSION) is True
    assert supports_presence(protocol.PROTOCOL_VERSION) is True
    assert supports_uploads(protocol.PROTOCOL_VERSION) is True
    assert supports_fetch(protocol.PROTOCOL_VERSION) is True
    assert supports_spawn_strategy(protocol.PROTOCOL_VERSION) is True


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
    # advanced to 5/6/7 for the (unrelated) upload, fetch and spawn-strategy
    # channels, so pin only the fact that these types exist below the current
    # revision.
    assert protocol.PROTOCOL_VERSION == "7"
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


# -- fetch channel (protocol revision 6) -------------------------------------


def test_supports_fetch_gate():
    assert supports_fetch("6") is True
    assert supports_fetch(6) is True
    assert supports_fetch("7") is True
    assert supports_fetch(" 6 ") is True

    # Fail closed: anything we cannot read as ">= 6" means "do not dispatch".
    # "5" matters most — a revision-5 daemon speaks uploads but not fetch, so
    # the two gates must not be confused for one another.
    assert supports_fetch("5") is False
    assert supports_fetch(5) is False
    assert supports_fetch("4") is False
    assert supports_fetch("") is False
    assert supports_fetch(None) is False
    assert supports_fetch("abc") is False
    assert supports_fetch({}) is False


def test_supports_fetch_is_bound_to_min_fetch_protocol_version():
    # The constant and the predicate are one contract: the boundary the
    # predicate enforces must be exactly MIN_FETCH_PROTOCOL_VERSION, so raising
    # the constant alone (or the predicate alone) can never leave the server
    # dispatching to a daemon the constant says is too old.
    floor = protocol.MIN_FETCH_PROTOCOL_VERSION
    assert floor == 6
    assert supports_fetch(floor) is True
    assert supports_fetch(floor - 1) is False
    assert supports_fetch(str(floor)) is True
    assert supports_fetch(str(floor - 1)) is False
    # The current revision must satisfy its own floor, or a daemon and server
    # built from this very module would refuse to speak the channel.
    assert int(protocol.PROTOCOL_VERSION) >= floor


def test_fetch_messages_registered_in_correct_directions():
    assert MSG_FETCH_COMMAND == "fetch_command"
    assert MSG_FETCH_RESULT == "fetch_result"

    assert MSG_FETCH_COMMAND in protocol.SERVER_TO_DAEMON
    assert MSG_FETCH_COMMAND not in protocol.DAEMON_TO_SERVER
    assert MSG_FETCH_RESULT in protocol.DAEMON_TO_SERVER
    assert MSG_FETCH_RESULT not in protocol.SERVER_TO_DAEMON

    # ALL_MESSAGE_TYPES is derived as the union, so both must appear without
    # anyone having hand-maintained a third list.
    assert MSG_FETCH_COMMAND in protocol.ALL_MESSAGE_TYPES
    assert MSG_FETCH_RESULT in protocol.ALL_MESSAGE_TYPES


def test_fetch_error_codes_set():
    assert protocol.FETCH_ERROR_CODES == frozenset(
        {
            "invalid_path",
            "not_registered",
            "not_found",
            "too_large",
            "unsupported",
            "read_failed",
        }
    )


def test_fetch_error_codes_are_distinct_from_upload_codes():
    # A read fails in ways a write cannot and vice versa; keeping the two sets
    # separate is what stops either channel drifting into accepting a code the
    # other side never emits.
    assert "not_found" in protocol.FETCH_ERROR_CODES
    assert "not_found" not in protocol.UPLOAD_ERROR_CODES
    assert "invalid_filename" in protocol.UPLOAD_ERROR_CODES
    assert "invalid_filename" not in protocol.FETCH_ERROR_CODES


def test_make_fetch_command_round_trip():
    msg = make_fetch_command(
        "/srv/proj", "tianluo/uploads/0123456789ab_shot.png", request_id="fx-1"
    )
    assert msg.type == MSG_FETCH_COMMAND

    decoded = protocol.decode(msg.to_json())
    assert decoded.type == MSG_FETCH_COMMAND
    assert decoded.payload == {
        "project_root": "/srv/proj",
        "path": "tianluo/uploads/0123456789ab_shot.png",
        "request_id": "fx-1",
    }


def test_make_fetch_command_omits_empty_request_id():
    msg = make_fetch_command("/srv/proj", "tianluo/uploads/ab_a.png")
    assert "request_id" not in msg.payload


def test_make_fetch_command_accepts_legacy_se3_layout_path():
    # A project created before the se3→tianluo rename stores its uploads under
    # se3/uploads/; the protocol must not hard-code either layout.
    msg = make_fetch_command("/srv/proj", "se3/uploads/ab_a.png")
    assert msg.payload["path"] == "se3/uploads/ab_a.png"


@pytest.mark.parametrize("project_root", ["", "relative/proj", "./proj", None])
def test_make_fetch_command_rejects_non_absolute_project_root(project_root):
    with pytest.raises(ProtocolError):
        make_fetch_command(project_root, "tianluo/uploads/ab_a.png")


@pytest.mark.parametrize("path", ["", "   ", None])
def test_make_fetch_command_rejects_empty_path(path):
    with pytest.raises(ProtocolError):
        make_fetch_command("/srv/proj", path)


@pytest.mark.parametrize("path", ["/etc/passwd", "/srv/proj/tianluo/uploads/a.png"])
def test_make_fetch_command_rejects_absolute_path(path):
    # An absolute path would both leak the daemon machine's layout to the
    # browser and let a caller aim the read anywhere on that filesystem.
    with pytest.raises(ProtocolError):
        make_fetch_command("/srv/proj", path)


@pytest.mark.parametrize(
    "path",
    [
        "..",
        "../etc/passwd",
        "tianluo/uploads/../../../etc/passwd",
        "tianluo/../uploads/a.png",
    ],
)
def test_make_fetch_command_rejects_traversal_path(path):
    with pytest.raises(ProtocolError):
        make_fetch_command("/srv/proj", path)


def test_make_fetch_command_allows_dotdot_inside_a_filename():
    # Only a whole ".." *segment* is traversal; a name that merely contains the
    # two characters is an ordinary file the daemon may legitimately serve.
    msg = make_fetch_command("/srv/proj", "tianluo/uploads/ab_v..1.png")
    assert msg.payload["path"] == "tianluo/uploads/ab_v..1.png"


def test_make_fetch_result_success_round_trip():
    msg = make_fetch_result(
        "fx-2",
        ok=True,
        content_b64="aGVsbG8=",
        size=5,
        name="0123456789ab_shot.png",
    )
    assert msg.type == MSG_FETCH_RESULT

    decoded = protocol.decode(msg.to_json())
    assert decoded.type == MSG_FETCH_RESULT
    assert decoded.payload == {
        "request_id": "fx-2",
        "ok": True,
        "content_b64": "aGVsbG8=",
        "size": 5,
        "name": "0123456789ab_shot.png",
    }


def test_make_fetch_result_success_carries_zero_size():
    # A 0-byte file is a real answer, not an absence; dropping the key as falsy
    # would leave the server unable to tell it from "not reported".
    msg = make_fetch_result("fx-3", ok=True, content_b64="", name="ab_e.log")
    decoded = protocol.decode(msg.to_json())
    assert decoded.payload["size"] == 0
    assert decoded.payload["content_b64"] == ""


def test_make_fetch_result_accepts_exactly_the_limit():
    msg = make_fetch_result("fx-4", ok=True, content_b64="x", size=MAX_UPLOAD_BYTES)
    assert msg.payload["size"] == MAX_UPLOAD_BYTES


def test_make_fetch_result_rejects_oversized_declared_size():
    # The read-back leg shares the upload ceiling: the same base64 blow-up has
    # to fit the same per-frame websocket cap.
    with pytest.raises(ProtocolError):
        make_fetch_result("fx-5", ok=True, content_b64="x", size=MAX_UPLOAD_BYTES + 1)


def test_make_fetch_result_rejects_negative_size():
    with pytest.raises(ProtocolError):
        make_fetch_result("fx-6", ok=True, content_b64="", size=-1)


def test_make_fetch_result_failure_carries_code_round_trip():
    msg = make_fetch_result(
        "fx-7",
        ok=False,
        error="tianluo/uploads/gone.png does not exist",
        error_code="not_found",
    )
    decoded = protocol.decode(msg.to_json())
    assert decoded.payload == {
        "request_id": "fx-7",
        "ok": False,
        "error": "tianluo/uploads/gone.png does not exist",
        "error_code": "not_found",
    }


def test_make_fetch_result_failure_omits_success_fields():
    # A failed fetch has no content/size/name to report; emitting them at their
    # defaults would let a caller mistake a failure for a 0-byte success.
    msg = make_fetch_result("fx-8", ok=False, error_code="read_failed")
    for key in ("content_b64", "size", "name"):
        assert key not in msg.payload


@pytest.mark.parametrize(
    "error_code", ["bogus", "invalid_filename", "NOT_FOUND", "write_failed"]
)
def test_make_fetch_result_rejects_unknown_error_code(error_code):
    with pytest.raises(ProtocolError):
        make_fetch_result("fx-9", ok=False, error_code=error_code)


@pytest.mark.parametrize("error_code", sorted(protocol.FETCH_ERROR_CODES))
def test_make_fetch_result_accepts_every_declared_error_code(error_code):
    msg = make_fetch_result("fx-10", ok=False, error_code=error_code)
    assert msg.payload["error_code"] == error_code
