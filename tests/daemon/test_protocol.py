"""Protocol revision 4 — presence signalling (MSG_VIEWERS + PING viewers).

Covers the wire-level contract this revision adds: the MSG_VIEWERS edge
message and its constructor, the optional ``viewers`` level field piggybacked
on MSG_PING (byte-compatible with revision 3 when absent), the version bump
to "4", and the ``supports_presence`` negotiation gate that keeps a daemon at
full speed against any pre-presence server.
"""

from __future__ import annotations

from se3.daemon import protocol
from se3.daemon.protocol import (
    MSG_PING,
    MSG_VIEWERS,
    make_ping,
    make_viewers,
    supports_presence,
    supports_traffic_reduction,
)


# -- version bump ----------------------------------------------------------


def test_protocol_version_bumped_to_4():
    # Revision 4 added the presence signalling; the bump is what lets the
    # daemon distinguish "server reports viewers" from "server never will",
    # so it only downshifts when silence actually means zero viewers.
    assert protocol.PROTOCOL_VERSION == "4"


def test_revision_4_does_not_regress_traffic_reduction_gate():
    # A revision-4 peer must still satisfy the older revision-3 gate.
    assert supports_traffic_reduction("4") is True
    assert supports_traffic_reduction(protocol.PROTOCOL_VERSION) is True


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
