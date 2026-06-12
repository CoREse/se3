"""Tests for the server-side history relay: ServerState caching, WebSocket
routing of history messages, and the ``/api/history`` REST endpoints.

The server is a pure in-memory relay — these tests exercise index
aggregation, append merging, on-demand pull (cache hit / miss), and the
pull timeout path.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from _authsrv import authed_hello
from se3.daemon import protocol
from se3.server.state import (
    ServerState,
    decode_progress,
    encode_progress,
)
from se3.server.ws import HistoryRequestRegistry, request_history


# --------------------------------------------------------------------------
# ServerState — history index & data caching
# --------------------------------------------------------------------------


def test_history_index_write_and_aggregate():
    state = ServerState()

    async def scenario():
        await state.update_history_index(
            "m1", [{"flow_id": "f1", "task_description": "A", "updated_at": "2026-01-02"}]
        )
        await state.update_history_index(
            "m2", [{"flow_id": "f2", "task_description": "B", "updated_at": "2026-01-03"}]
        )
        index = await state.get_history_index()
        assert len(index) == 2
        # Sorted by updated_at descending.
        assert index[0]["flow_id"] == "f2"
        # Each entry is annotated with its reporting machine.
        by_flow = {e["flow_id"]: e["machine_id"] for e in index}
        assert by_flow == {"f1": "m1", "f2": "m2"}

    asyncio.run(scenario())


def test_history_index_replaced_per_machine():
    state = ServerState()

    async def scenario():
        await state.update_history_index("m1", [{"flow_id": "f1"}])
        await state.update_history_index("m1", [{"flow_id": "f9"}])
        index = await state.get_history_index()
        assert [e["flow_id"] for e in index] == ["f9"]

    asyncio.run(scenario())


def test_history_data_full_then_append_merges():
    state = ServerState()

    async def scenario():
        await state.append_history(
            "f1",
            protocol.HISTORY_MODE_FULL,
            [{"step": "s1", "line": 1}],
            cursor={"s1": 1},
            machine_id="m1",
        )
        await state.append_history(
            "f1",
            protocol.HISTORY_MODE_APPEND,
            [{"step": "s1", "line": 2}, {"step": "s1", "line": 3}],
            cursor={"s1": 3},
        )
        cached = await state.get_history("f1")
        assert cached is not None
        assert [r["line"] for r in cached["records"]] == [1, 2, 3]
        assert cached["cursor"] == {"s1": 3}
        assert cached["machine_id"] == "m1"

    asyncio.run(scenario())


def test_history_data_initial_append_requires_authoritative_full():
    state = ServerState()

    async def scenario():
        # After a server restart the daemon may keep its cursor and push only
        # the new tail. That tail must not become an authoritative snapshot.
        await state.append_history(
            "f1",
            protocol.HISTORY_MODE_APPEND,
            [{"step": "s1", "line": 9}],
            machine_id="m1",
        )
        assert await state.get_history("f1") is None
        assert await state.get_history_snapshot("f1") is None

        await state.append_history(
            "f1",
            protocol.HISTORY_MODE_APPEND,
            [{"step": "s1", "line": 10}],
            machine_id="m1",
        )
        assert await state.get_history_snapshot("f1") is None

        await state.append_history(
            "f1",
            protocol.HISTORY_MODE_FULL,
            [{"step": "s1", "line": 1}, {"step": "s1", "line": 2}],
            machine_id="m1",
        )
        snapshot = await state.get_history_snapshot("f1")
        assert [record["line"] for record in snapshot["records"]] == [1, 2]

    asyncio.run(scenario())


def test_unrecognized_mode_replace_clears_requires_full_flag():
    """A non-append / unrecognized replacing frame must clear requires-full.

    A first-sighting append flags the flow requires-full. If a version-skewed
    or malformed daemon then pushes a frame whose ``mode`` is missing /
    unrecognized, it takes the wholesale-replace branch and creates a fresh
    authoritative bundle. That bundle MUST be able to keep accumulating
    appends — otherwise REST cache-hits a frozen bundle and silently discards
    every subsequent delta forever.
    """
    state = ServerState()

    async def scenario():
        # First-sighting append flags requires-full and is discarded.
        assert (
            await state.append_history(
                "f1", protocol.HISTORY_MODE_APPEND, [{"line": 9}], machine_id="m1"
            )
            is False
        )
        # An unrecognized / missing mode replaces the bundle wholesale.
        assert (
            await state.append_history(
                "f1", "", [{"line": 1}, {"line": 2}], machine_id="m1"
            )
            is True
        )
        snapshot = await state.get_history_snapshot("f1")
        assert [record["line"] for record in snapshot["records"]] == [1, 2]

        # A following append must now extend the bundle, not be discarded.
        assert (
            await state.append_history(
                "f1", protocol.HISTORY_MODE_APPEND, [{"line": 3}], machine_id="m1"
            )
            is True
        )
        snapshot = await state.get_history_snapshot("f1")
        assert [record["line"] for record in snapshot["records"]] == [1, 2, 3]

    asyncio.run(scenario())


def test_append_history_returns_whether_cache_was_populated():
    """``append_history`` reports ``True`` only when it actually cached records.

    The on-demand pull waiter relies on this signal so a racing, discarded
    append cannot prematurely resolve it (see the cache-miss pull path).
    """
    state = ServerState()

    async def scenario():
        # First-sighting append → discarded, flags requires-full.
        assert (
            await state.append_history(
                "f1", protocol.HISTORY_MODE_APPEND, [{"line": 9}], machine_id="m1"
            )
            is False
        )
        # Subsequent append while still requires-full → still discarded.
        assert (
            await state.append_history(
                "f1", protocol.HISTORY_MODE_APPEND, [{"line": 10}], machine_id="m1"
            )
            is False
        )
        # Authoritative full → populates the cache.
        assert (
            await state.append_history(
                "f1", protocol.HISTORY_MODE_FULL, [{"line": 1}], machine_id="m1"
            )
            is True
        )
        # An ordinary append extending the bundle → applied.
        assert (
            await state.append_history(
                "f1", protocol.HISTORY_MODE_APPEND, [{"line": 2}], machine_id="m1"
            )
            is True
        )
        # A cross-machine delta is unanchored → discarded (and evicts the cache).
        assert (
            await state.append_history(
                "f1", protocol.HISTORY_MODE_APPEND, [{"line": 3}], machine_id="m2"
            )
            is False
        )

    asyncio.run(scenario())


def test_history_data_full_replaces():
    state = ServerState()

    async def scenario():
        await state.append_history("f1", protocol.HISTORY_MODE_FULL, [{"line": 1}])
        await state.append_history("f1", protocol.HISTORY_MODE_FULL, [{"line": 9}])
        cached = await state.get_history("f1")
        assert [r["line"] for r in cached["records"]] == [9]

    asyncio.run(scenario())


def test_get_history_miss_returns_none():
    state = ServerState()

    async def scenario():
        assert await state.get_history("nope") is None

    asyncio.run(scenario())


def test_find_machine_for_history_flow():
    state = ServerState()

    async def scenario():
        await state.update_history_index("m1", [{"flow_id": "f1"}])
        assert await state.find_machine_for_history_flow("f1") == "m1"
        # Falls back to cached data owner.
        await state.append_history(
            "f2", protocol.HISTORY_MODE_FULL, [], machine_id="m2"
        )
        assert await state.find_machine_for_history_flow("f2") == "m2"
        # Falls back to the live flow set.
        await state.update_status(
            "m3", {"machine_id": "m3", "flows": [{"flow_id": "f3"}]}
        )
        assert await state.find_machine_for_history_flow("f3") == "m3"
        assert await state.find_machine_for_history_flow("ghost") is None

    asyncio.run(scenario())


def test_request_history_uses_prevalidated_machine_without_reresolving():
    class Manager:
        def __init__(self):
            self.sent = []

        def is_connected(self, machine_id):
            return machine_id == "m-owner"

        async def send_to(self, machine_id, message):
            self.sent.append((machine_id, message))
            return True

    class State:
        async def find_machine_for_history_flow(self, flow_id):
            raise AssertionError("ownership must not be resolved a second time")

    async def scenario():
        manager = Manager()
        sent = await request_history(
            manager,
            State(),
            "f1",
            machine_id="m-owner",
        )
        assert sent is True
        assert len(manager.sent) == 1
        machine_id, message = manager.sent[0]
        assert machine_id == "m-owner"
        assert message.type == protocol.MSG_HISTORY_REQUEST
        assert message.payload["flow_id"] == "f1"

    asyncio.run(scenario())


def test_request_history_pins_exact_connection():
    class Manager:
        def __init__(self):
            self.sent = []

        def is_connected(self, machine_id):
            return True

        async def send_to_connection(self, machine_id, connection, message):
            self.sent.append((machine_id, connection, message))
            return connection == "validated-socket"

    async def scenario():
        manager = Manager()
        sent = await request_history(
            manager,
            object(),
            "f1",
            machine_id="m-owner",
            connection="validated-socket",
        )
        assert sent is True
        machine_id, connection, message = manager.sent[0]
        assert machine_id == "m-owner"
        assert connection == "validated-socket"
        assert message.type == protocol.MSG_HISTORY_REQUEST

    asyncio.run(scenario())


def test_history_request_registry_ignores_same_flow_from_other_machine():
    async def scenario():
        registry = HistoryRequestRegistry()
        fut = registry.register("f1", "m-owner")

        registry.resolve("f1", {"records": ["foreign"]}, machine_id="m-other")
        await asyncio.sleep(0)
        assert not fut.done()

        expected = {"records": ["owned"]}
        registry.resolve("f1", expected, machine_id="m-owner")
        assert await fut == expected

    asyncio.run(scenario())


def test_history_request_registry_begin_pull_dedups_inflight():
    """Concurrent cache-miss callers for the same (flow, machine) share one
    daemon pull: only the first is the leader that sends the request.

    Without this, each concurrent request sent its own ``MSG_HISTORY_REQUEST``;
    the first reply resolved all waiters and was suppressed, but the second
    reply found no waiter, replaced the cache generation, and was broadcast as
    ``mode: full`` — clearing the progress tokens REST had just returned.
    """
    async def scenario():
        registry = HistoryRequestRegistry()

        fut_a, leader_a = registry.begin_pull("f1", "m1")
        fut_b, leader_b = registry.begin_pull("f1", "m1")
        # Exactly one leader for the shared in-flight pull.
        assert leader_a is True
        assert leader_b is False

        # A different machine for the same flow is a distinct pull.
        fut_c, leader_c = registry.begin_pull("f1", "m2")
        assert leader_c is True

        # One reply resolves every waiter parked for (f1, m1) and clears the
        # in-flight marker, so a later cache miss starts a fresh pull.
        payload = {"records": [{"line": 1}]}
        assert registry.resolve("f1", payload, machine_id="m1") is True
        assert await fut_a == payload
        assert await fut_b == payload
        assert not fut_c.done()

        fut_d, leader_d = registry.begin_pull("f1", "m1")
        assert leader_d is True

    asyncio.run(scenario())


def test_history_request_registry_inflight_cleared_when_all_discarded():
    """When every waiter for a key is discarded (all timed out / send failed),
    the in-flight marker clears so the next request becomes a leader again."""
    async def scenario():
        registry = HistoryRequestRegistry()

        fut_a, leader_a = registry.begin_pull("f1", "m1")
        fut_b, leader_b = registry.begin_pull("f1", "m1")
        assert leader_a is True and leader_b is False

        registry.discard("f1", fut_a, "m1")
        # A follower waiter still parked keeps the pull in flight.
        fut_c, leader_c = registry.begin_pull("f1", "m1")
        assert leader_c is False

        registry.discard("f1", fut_b, "m1")
        registry.discard("f1", fut_c, "m1")
        # All waiters gone — the marker is cleared, so a fresh request leads.
        fut_d, leader_d = registry.begin_pull("f1", "m1")
        assert leader_d is True

    asyncio.run(scenario())


def test_history_request_registry_fail_pull_releases_followers():
    """When the leader fails before dispatch, ``fail_pull`` must release every
    parked follower at once AND clear the in-flight marker — not leave the
    marker set (because followers remain) the way ``discard`` would.

    Regression: a leader whose daemon send failed / was cancelled before a
    successful dispatch used to only ``discard`` its own waiter. With followers
    still parked the in-flight marker stayed set, so the followers waited out
    ``HISTORY_PULL_TIMEOUT`` on a request that was never sent and every later
    request joined the abandoned pull instead of leading a replacement.
    """
    from se3.server.ws import _PullAbandoned

    async def scenario():
        registry = HistoryRequestRegistry()

        fut_a, leader_a = registry.begin_pull("f1", "m1")  # leader
        fut_b, leader_b = registry.begin_pull("f1", "m1")  # follower
        fut_c, leader_c = registry.begin_pull("f1", "m1")  # follower
        assert leader_a is True
        assert leader_b is False and leader_c is False

        # Leader fails before dispatch: release the followers and clear marker.
        registry.fail_pull("f1", "m1", exclude=fut_a)

        # Followers are woken immediately with ``_PullAbandoned`` so they can
        # retry rather than waiting out the timeout.
        for fut in (fut_b, fut_c):
            assert fut.done()
            with pytest.raises(_PullAbandoned):
                fut.result()
        # The leader's own future is left untouched — it is already unwinding
        # and never awaits it on this path (so no unretrieved-exception noise).
        assert not fut_a.done()

        # The marker is cleared: the very next request leads a fresh pull.
        fut_d, leader_d = registry.begin_pull("f1", "m1")
        assert leader_d is True

    asyncio.run(scenario())


def test_history_caches_are_not_persisted(tmp_path):
    """The relay holds history purely in memory — no files are written."""
    state = ServerState()

    async def scenario():
        await state.update_history_index("m1", [{"flow_id": "f1"}])
        await state.append_history("f1", protocol.HISTORY_MODE_FULL, [{"line": 1}])

    asyncio.run(scenario())
    # No on-disk artifact of any kind.
    assert list(tmp_path.iterdir()) == []


# --------------------------------------------------------------------------
# ServerState — incremental history progress token (generation + offset)
# --------------------------------------------------------------------------


def test_progress_token_roundtrip():
    token = encode_progress(7, 42, "m1")
    decoded = decode_progress(token)
    assert decoded == {"generation": 7, "offset": 42, "machine_id": "m1"}


def test_progress_token_is_opaque_and_credential_free():
    # The signed envelope carries only an encoded scalar payload + signature —
    # no record content, owner identity, or signing key.
    token = encode_progress(1, 3, "m1")
    import base64
    raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
    assert "record" not in raw and "owner" not in raw
    assert set(json.loads(raw).keys()) == {"p", "s"}
    assert decode_progress(token) == {
        "generation": 1,
        "offset": 3,
        "machine_id": "m1",
    }


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "",
        "not-base64!!!",
        "@@@@",
        # valid base64 of non-JSON
        "Zm9vYmFy",  # b64("foobar")
    ],
)
def test_decode_progress_malformed_returns_none(bad):
    assert decode_progress(bad) is None


def test_decode_progress_rejects_wrong_version_and_types():
    import base64

    def _b64(obj):
        return base64.urlsafe_b64encode(
            json.dumps(obj).encode("utf-8")
        ).decode("ascii")

    # Wrong version marker.
    assert decode_progress(_b64({"v": 99, "g": 1, "o": 0, "m": "m1"})) is None
    # Non-int offset.
    assert decode_progress(_b64({"v": 1, "g": 1, "o": "x", "m": "m1"})) is None
    # Negative offset.
    assert decode_progress(_b64({"v": 1, "g": 1, "o": -1, "m": "m1"})) is None
    # Boolean smuggled where an int is expected.
    assert decode_progress(_b64({"v": 1, "g": True, "o": 0, "m": "m1"})) is None
    # Non-str machine id.
    assert decode_progress(_b64({"v": 1, "g": 1, "o": 0, "m": 5})) is None
    # Not a dict.
    assert decode_progress(_b64([1, 2, 3])) is None


def test_snapshot_miss_returns_none():
    state = ServerState()

    async def scenario():
        assert await state.get_history_snapshot("nope") is None

    asyncio.run(scenario())


def test_snapshot_full_without_token():
    state = ServerState()

    async def scenario():
        await state.append_history(
            "f1", protocol.HISTORY_MODE_FULL,
            [{"line": 1}, {"line": 2}], machine_id="m1",
        )
        snap = await state.get_history_snapshot("f1")
        assert snap["delivery"] == "full"
        assert [r["line"] for r in snap["records"]] == [1, 2]
        # Progress pins to the full record count.
        prog = decode_progress(snap["progress"])
        assert prog["offset"] == 2
        assert prog["machine_id"] == "m1"

    asyncio.run(scenario())


def test_snapshot_delta_after_append_keeps_generation():
    state = ServerState()

    async def scenario():
        await state.append_history(
            "f1", protocol.HISTORY_MODE_FULL, [{"line": 1}], machine_id="m1"
        )
        snap1 = await state.get_history_snapshot("f1")
        # New records arrive on the same bundle generation.
        await state.append_history(
            "f1", protocol.HISTORY_MODE_APPEND,
            [{"line": 2}, {"line": 3}], machine_id="m1",
        )
        snap2 = await state.get_history_snapshot(
            "f1", after=snap1["progress"]
        )
        assert snap2["delivery"] == "delta"
        # Only the records after the prior offset come back, in order.
        assert [r["line"] for r in snap2["records"]] == [2, 3]
        prog2 = decode_progress(snap2["progress"])
        assert prog2["offset"] == 3

    asyncio.run(scenario())


def test_snapshot_empty_delta_when_no_new_records():
    state = ServerState()

    async def scenario():
        await state.append_history(
            "f1", protocol.HISTORY_MODE_FULL, [{"line": 1}], machine_id="m1"
        )
        snap = await state.get_history_snapshot("f1")
        again = await state.get_history_snapshot("f1", after=snap["progress"])
        assert again["delivery"] == "delta"
        assert again["records"] == []

    asyncio.run(scenario())


def test_snapshot_full_replace_invalidates_old_token():
    state = ServerState()

    async def scenario():
        await state.append_history(
            "f1", protocol.HISTORY_MODE_FULL, [{"line": 1}], machine_id="m1"
        )
        snap1 = await state.get_history_snapshot("f1")
        # A full replacement rolls a new generation.
        await state.append_history(
            "f1", protocol.HISTORY_MODE_FULL,
            [{"line": 9}, {"line": 10}], machine_id="m1",
        )
        snap2 = await state.get_history_snapshot("f1", after=snap1["progress"])
        # Old token's generation no longer matches -> full fallback.
        assert snap2["delivery"] == "full"
        assert [r["line"] for r in snap2["records"]] == [9, 10]

    asyncio.run(scenario())


def test_snapshot_offset_out_of_range_falls_back_full():
    state = ServerState()

    async def scenario():
        await state.append_history(
            "f1", protocol.HISTORY_MODE_FULL,
            [{"line": 1}, {"line": 2}], machine_id="m1",
        )
        snap = await state.get_history_snapshot("f1")
        gen = decode_progress(snap["progress"])["generation"]
        # Hand-forge a token whose offset exceeds the record count.
        bad = encode_progress(gen, 99, "m1")
        again = await state.get_history_snapshot("f1", after=bad)
        assert again["delivery"] == "full"
        assert len(again["records"]) == 2

    asyncio.run(scenario())


def test_snapshot_rejects_client_forged_in_range_offset():
    state = ServerState()

    async def scenario():
        await state.append_history(
            "f1",
            protocol.HISTORY_MODE_FULL,
            [{"line": 1}, {"line": 2}, {"line": 3}, {"line": 4}],
            machine_id="m1",
        )
        snap = await state.get_history_snapshot("f1")
        decoded = decode_progress(snap["progress"])
        # This token is syntactically valid and points inside the current
        # bundle, but it was not signed by this ServerState instance.
        forged = encode_progress(decoded["generation"], 3, "m1")
        again = await state.get_history_snapshot("f1", after=forged)
        assert again["delivery"] == "full"
        assert [r["line"] for r in again["records"]] == [1, 2, 3, 4]

    asyncio.run(scenario())


def test_snapshot_machine_mismatch_in_token_falls_back_full():
    state = ServerState()

    async def scenario():
        await state.append_history(
            "f1", protocol.HISTORY_MODE_FULL, [{"line": 1}], machine_id="m1"
        )
        snap = await state.get_history_snapshot("f1")
        gen = decode_progress(snap["progress"])["generation"]
        # Same generation/offset but a different machine id in the token.
        bad = encode_progress(gen, 1, "OTHER")
        again = await state.get_history_snapshot("f1", after=bad)
        assert again["delivery"] == "full"

    asyncio.run(scenario())


def test_snapshot_expected_machine_mismatch_returns_none():
    state = ServerState()

    async def scenario():
        await state.append_history(
            "f1", protocol.HISTORY_MODE_FULL, [{"line": 1}], machine_id="m1"
        )
        # The flow has since moved to a different daemon -> treat as a miss.
        assert (
            await state.get_history_snapshot(
                "f1", expected_machine_id="m2"
            )
            is None
        )
        # Matching machine still returns a snapshot.
        snap = await state.get_history_snapshot(
            "f1", expected_machine_id="m1"
        )
        assert snap is not None and snap["delivery"] == "full"

    asyncio.run(scenario())


def test_snapshot_owner_scoped_resolution_move_returns_none():
    state = ServerState()

    async def scenario():
        await state.register_machine("m1", owner_id="owner-a")
        await state.register_machine("m2", owner_id="owner-a")
        await state.update_history_index("m1", [{"flow_id": "f1"}])
        await state.append_history(
            "f1",
            protocol.HISTORY_MODE_FULL,
            [{"line": 1}],
            machine_id="m1",
        )
        assert await state.get_history_snapshot(
            "f1",
            expected_machine_id="m1",
            expected_owner="owner-a",
        )

        # The authoritative index moves the flow while the old machine's
        # bundle remains cached. The atomic owner-scoped read must reject it.
        await state.update_history_index("m1", [])
        await state.update_history_index("m2", [{"flow_id": "f1"}])
        assert (
            await state.get_history_snapshot(
                "f1",
                expected_machine_id="m1",
                expected_owner="owner-a",
            )
            is None
        )

    asyncio.run(scenario())


def test_snapshot_machine_change_on_append_invalidates_cache():
    state = ServerState()

    async def scenario():
        await state.append_history(
            "f1", protocol.HISTORY_MODE_FULL, [{"line": 1}], machine_id="m1"
        )
        # An append carrying a different machine id is not sufficient to build
        # a new authoritative bundle. The mixed cache is discarded so the REST
        # route must pull a full snapshot from m2.
        await state.append_history(
            "f1", protocol.HISTORY_MODE_APPEND, [{"line": 2}], machine_id="m2"
        )
        assert await state.get_history_snapshot("f1") is None
        # Further deltas from m2 remain ignored until its authoritative full
        # bundle arrives; they must not recreate a partial cache.
        await state.append_history(
            "f1", protocol.HISTORY_MODE_APPEND, [{"line": 3}], machine_id="m2"
        )
        assert await state.get_history_snapshot("f1") is None
        await state.append_history(
            "f1",
            protocol.HISTORY_MODE_FULL,
            [{"line": 10}, {"line": 11}],
            machine_id="m2",
        )
        restored = await state.get_history_snapshot("f1")
        assert [r["line"] for r in restored["records"]] == [10, 11]
        assert restored["machine_id"] == "m2"

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# WebSocket routing + REST endpoints
# --------------------------------------------------------------------------


@pytest.fixture()
def client_and_app(monkeypatch):
    from fastapi.testclient import TestClient

    import se3.server.app as app_module

    from _authsrv import authed_app, login

    # ``GET /api/history`` now broadcasts a forced index re-push to every
    # connected daemon and waits for the replies. Tests using a stand-in
    # daemon that does not answer would otherwise block the full 2 s timeout
    # on every call, so shorten the wait here.
    monkeypatch.setattr(app_module, "HISTORY_INDEX_REFRESH_TIMEOUT", 0.3)

    app, _key = authed_app()
    with TestClient(app) as client:
        login(client)
        yield client, app


def _receive_until(daemon, msg_type):
    """Read frames from *daemon*, skipping index-refresh broadcasts.

    ``GET /api/history`` queues a ``MSG_HISTORY_INDEX_REQUEST`` on every
    connected daemon; a test that next expects a different server→daemon frame
    must skip past those broadcasts. Returns the first frame of *msg_type*.
    """
    while True:
        frame = protocol.decode(daemon.receive_text())
        if frame.type == msg_type:
            return frame
        assert frame.type == protocol.MSG_HISTORY_INDEX_REQUEST


def test_history_index_message_routed_to_state(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws") as daemon:
        daemon.send_text(authed_hello(app, "m1", "host", "6.4.0"))
        protocol.decode(daemon.receive_text())  # WELCOME
        daemon.send_text(
            protocol.make_history_index(
                [{"flow_id": "f1", "task_description": "T", "status": "completed"}]
            ).to_json()
        )
        for _ in range(50):
            sessions = client.get("/api/history").json()["sessions"]
            if sessions:
                break
        assert sessions and sessions[0]["flow_id"] == "f1"
        assert sessions[0]["machine_id"] == "m1"


def test_history_index_broadcast_to_ui(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws/ui") as ui:
        assert json.loads(ui.receive_text())["type"] == "snapshot"
        with client.websocket_connect("/ws") as daemon:
            daemon.send_text(authed_hello(app, "m1", "host", "6.4.0"))
            protocol.decode(daemon.receive_text())  # WELCOME
            assert json.loads(ui.receive_text())["type"] == "status_update"
            daemon.send_text(
                protocol.make_history_index([{"flow_id": "f1"}]).to_json()
            )
            pushed = json.loads(ui.receive_text())
            assert pushed["type"] == "history_index"
            assert pushed["sessions"][0]["flow_id"] == "f1"


def test_history_data_message_cached_and_broadcast(client_and_app):
    client, app = client_and_app
    with client.websocket_connect("/ws/ui") as ui:
        assert json.loads(ui.receive_text())["type"] == "snapshot"
        with client.websocket_connect("/ws") as daemon:
            daemon.send_text(authed_hello(app, "m1", "host", "6.4.0"))
            protocol.decode(daemon.receive_text())  # WELCOME
            assert json.loads(ui.receive_text())["type"] == "status_update"
            # Active flow incremental append arrives unsolicited.
            daemon.send_text(
                protocol.make_history_data(
                    "f1",
                    protocol.HISTORY_MODE_FULL,
                    [{"step": "s1", "line": "hi"}],
                ).to_json()
            )
            pushed = json.loads(ui.receive_text())
            assert pushed["type"] == "history_data"
            assert pushed["flow_id"] == "f1"
            # And it landed in the cache (served straight from REST).
            resp = client.get("/api/history/f1")
            assert resp.status_code == 200
            body = resp.json()
            assert body["cached"] is True
            assert body["records"][0]["line"] == "hi"


def test_history_detail_on_demand_pull(client_and_app):
    """A cache miss triggers a MSG_HISTORY_REQUEST and resolves on the reply."""
    client, app = client_and_app
    with client.websocket_connect("/ws") as daemon:
        daemon.send_text(authed_hello(app, "m1", "host", "6.4.0"))
        protocol.decode(daemon.receive_text())  # WELCOME
        # Report the index so the server knows m1 owns f1.
        daemon.send_text(protocol.make_history_index([{"flow_id": "f1"}]).to_json())
        for _ in range(50):
            if client.get("/api/history").json()["sessions"]:
                break

        result: dict = {}

        def do_get():
            result["resp"] = client.get("/api/history/f1")

        worker = threading.Thread(target=do_get)
        worker.start()
        try:
            # The server should route a pull request to the daemon (skipping
            # any index-refresh broadcast queued by the GET /api/history above).
            req = _receive_until(daemon, protocol.MSG_HISTORY_REQUEST)
            assert req.payload["flow_id"] == "f1"
            daemon.send_text(
                protocol.make_history_data(
                    "f1",
                    protocol.HISTORY_MODE_FULL,
                    [{"step": "s1", "line": "pulled"}],
                ).to_json()
            )
        finally:
            worker.join(timeout=5)
        resp = result["resp"]
        assert resp.status_code == 200
        body = resp.json()
        assert body["cached"] is False
        assert body["records"][0]["line"] == "pulled"


def test_on_demand_pull_reply_not_rebroadcast_to_ui(client_and_app):
    """An on-demand cache-miss pull reply must NOT be re-broadcast to the UI.

    The parked REST handler returns the full records plus a fresh ``progress``
    token to the requesting client; re-broadcasting the same ``mode: full``
    frame over ``/ws/ui`` would make every history consumer reset its progress
    to null and force another full fetch on the next reconnect. The fix
    suppresses the broadcast for a frame that resolved a pull waiter, while an
    unsolicited live append (no waiter) still streams to the UI.

    Asserted by ordering: after the pull completes, an unsolicited append is
    sent and the UI's next ``history_data`` frame must be that append — never
    the pulled full frame, which would arrive first had it been broadcast.
    """
    client, app = client_and_app
    with client.websocket_connect("/ws/ui") as ui:
        assert json.loads(ui.receive_text())["type"] == "snapshot"
        with client.websocket_connect("/ws") as daemon:
            daemon.send_text(authed_hello(app, "m1", "host", "6.4.0"))
            protocol.decode(daemon.receive_text())  # WELCOME
            assert json.loads(ui.receive_text())["type"] == "status_update"
            daemon.send_text(
                protocol.make_history_index([{"flow_id": "f1"}]).to_json()
            )
            for _ in range(50):
                if client.get("/api/history").json()["sessions"]:
                    break

            result: dict = {}

            def do_get():
                result["resp"] = client.get("/api/history/f1")

            worker = threading.Thread(target=do_get)
            worker.start()
            try:
                req = _receive_until(daemon, protocol.MSG_HISTORY_REQUEST)
                assert req.payload["flow_id"] == "f1"
                # Pull reply (resolves the parked REST waiter).
                daemon.send_text(
                    protocol.make_history_data(
                        "f1",
                        protocol.HISTORY_MODE_FULL,
                        [{"step": "s1", "line": "pulled"}],
                    ).to_json()
                )
            finally:
                worker.join(timeout=5)
            assert result["resp"].status_code == 200
            assert result["resp"].json()["records"][0]["line"] == "pulled"

            # Now an unsolicited live append (no waiter parked).
            daemon.send_text(
                protocol.make_history_data(
                    "f1",
                    protocol.HISTORY_MODE_APPEND,
                    [{"step": "s1", "line": "live"}],
                ).to_json()
            )
            # The UI's next history_data frame must be the live append, proving
            # the pull reply above was not re-broadcast (else it would arrive
            # first).
            pushed = json.loads(ui.receive_text())
            while pushed["type"] != "history_data":
                pushed = json.loads(ui.receive_text())
            assert pushed["flow_id"] == "f1"
            assert pushed["mode"] == protocol.HISTORY_MODE_APPEND
            assert pushed["records"][0]["line"] == "live"


def test_history_detail_pull_ignores_racing_append_waits_for_full(client_and_app):
    """A cache-miss pull must not be resolved by a discarded racing append.

    Reproduces the server-restart race: the flow is flagged requires-full by a
    first-sighting append, so the periodic push-loop appends that arrive while
    the on-demand pull is in flight are silently discarded by
    ``append_history``. Those discarded frames must NOT wake the REST handler
    (which would re-read the still-empty cache and raise a spurious 409); the
    handler keeps waiting for the daemon's authoritative full reply and returns
    it.
    """
    client, app = client_and_app
    with client.websocket_connect("/ws") as daemon:
        daemon.send_text(authed_hello(app, "m1", "host", "6.4.0"))
        protocol.decode(daemon.receive_text())  # WELCOME
        daemon.send_text(protocol.make_history_index([{"flow_id": "f1"}]).to_json())
        for _ in range(50):
            if client.get("/api/history").json()["sessions"]:
                break

        # First-sighting append → flow flagged requires-full, nothing cached.
        daemon.send_text(
            protocol.make_history_data(
                "f1", protocol.HISTORY_MODE_APPEND, [{"step": "s1", "line": "tail"}]
            ).to_json()
        )

        result: dict = {}

        def do_get():
            result["resp"] = client.get("/api/history/f1")

        worker = threading.Thread(target=do_get)
        worker.start()
        try:
            req = _receive_until(daemon, protocol.MSG_HISTORY_REQUEST)
            assert req.payload["flow_id"] == "f1"
            # A racing periodic append arrives first — still discarded because
            # the flow is requires-full. The waiter must NOT resolve on it.
            daemon.send_text(
                protocol.make_history_data(
                    "f1",
                    protocol.HISTORY_MODE_APPEND,
                    [{"step": "s1", "line": "racing"}],
                ).to_json()
            )
            # The authoritative full reply finally lands and resolves the pull.
            daemon.send_text(
                protocol.make_history_data(
                    "f1",
                    protocol.HISTORY_MODE_FULL,
                    [{"step": "s1", "line": "full-a"}, {"step": "s1", "line": "full-b"}],
                ).to_json()
            )
        finally:
            worker.join(timeout=5)
        resp = result["resp"]
        assert resp.status_code == 200
        body = resp.json()
        assert body["cached"] is False
        assert body["delivery"] == "full"
        assert [r["line"] for r in body["records"]] == ["full-a", "full-b"]


def test_history_detail_no_daemon_404(client_and_app):
    client, _ = client_and_app
    resp = client.get("/api/history/ghost")
    assert resp.status_code == 404


def test_history_detail_pull_timeout(client_and_app, monkeypatch):
    """When the owning daemon never replies, the pull times out with 504."""
    import se3.server.app as app_module

    monkeypatch.setattr(app_module, "HISTORY_PULL_TIMEOUT", 0.5)
    client, app = client_and_app
    with client.websocket_connect("/ws") as daemon:
        daemon.send_text(authed_hello(app, "m1", "host", "6.4.0"))
        protocol.decode(daemon.receive_text())  # WELCOME
        daemon.send_text(protocol.make_history_index([{"flow_id": "f1"}]).to_json())
        for _ in range(50):
            if client.get("/api/history").json()["sessions"]:
                break

        result: dict = {}

        def do_get():
            result["resp"] = client.get("/api/history/f1")

        worker = threading.Thread(target=do_get)
        worker.start()
        try:
            # Drain the request but deliberately never answer it (skipping any
            # index-refresh broadcast queued by the GET /api/history above).
            _receive_until(daemon, protocol.MSG_HISTORY_REQUEST)
        finally:
            worker.join(timeout=5)
        assert result["resp"].status_code == 504


def test_history_detail_cancelled_pull_clears_inflight_marker():
    """A cache-miss pull cancelled mid-await (the client disconnecting before
    the daemon replies) must still unregister its waiter and clear the
    ``(flow_id, machine_id)`` in-flight marker.

    Regression: ``asyncio.wait_for`` re-raises ``CancelledError`` (not
    ``TimeoutError``) when the awaiting request is cancelled, so the old
    ``except asyncio.TimeoutError`` branch never ran ``discard`` on that path.
    The stale cancelled future then kept the key marked in-flight, so every
    later request became a follower that sent no fresh ``MSG_HISTORY_REQUEST``
    and merely timed out. The handler now discards in a ``finally`` so the
    marker clears on cancellation too.
    """
    import httpx

    from _authsrv import authed_app

    async def scenario():
        app, _key = authed_app()
        owner_id = app.state.test_owner_id
        state = app.state.server_state
        manager = app.state.connection_manager
        registry = app.state.history_registry

        # Make m1 the owning daemon of f1 with a cache miss (no records cached),
        # so a GET takes the on-demand pull path.
        await state.register_machine("m1", owner_id=owner_id)
        await state.update_history_index("m1", [{"flow_id": "f1"}])

        sent = asyncio.Event()

        class FakeWS:
            async def send_text(self, _data):
                # The leader's MSG_HISTORY_REQUEST has been dispatched; the
                # handler is about to park on the daemon reply.
                sent.set()

        await manager.connect("m1", FakeWS())

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            r = await client.post(
                "/api/auth/login", json={"username": "admin", "password": "pw"}
            )
            assert r.status_code == 200, r.text

            task = asyncio.create_task(client.get("/api/history/f1"))
            # Wait until the leader has sent the pull and is parked on the reply.
            await asyncio.wait_for(sent.wait(), timeout=5)
            # Let the handler reach its ``await asyncio.wait_for(fut, ...)``.
            await asyncio.sleep(0)
            # Simulate the client disconnecting: cancel mid-await.
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        # The waiter must have been dropped and the marker cleared, so the next
        # cache-miss request leads a fresh pull instead of parking as a follower.
        fut, is_leader = registry.begin_pull("f1", "m1")
        assert is_leader is True

    asyncio.run(scenario())


def test_history_detail_cancelled_during_send_clears_inflight_marker():
    """A cache-miss pull cancelled while the leader's ``MSG_HISTORY_REQUEST``
    is still being sent (the client disconnecting before ``send_text``
    returns) must still unregister its waiter and clear the in-flight marker.

    Regression: the leader's daemon send sat OUTSIDE the ``try``/``finally``
    that discards the waiter, so a cancellation fired while ``send_text`` was
    blocked left the waiter parked and the ``(flow_id, machine_id)`` key marked
    in-flight forever. Every later request then became a follower that sent no
    fresh ``MSG_HISTORY_REQUEST`` and merely timed out. The handler now sends
    inside the ``try`` so the ``finally`` discards on this path too.
    """
    import httpx

    from _authsrv import authed_app

    async def scenario():
        app, _key = authed_app()
        owner_id = app.state.test_owner_id
        state = app.state.server_state
        manager = app.state.connection_manager
        registry = app.state.history_registry

        await state.register_machine("m1", owner_id=owner_id)
        await state.update_history_index("m1", [{"flow_id": "f1"}])

        sending = asyncio.Event()
        release = asyncio.Event()

        class BlockingWS:
            async def send_text(self, _data):
                # The leader is now blocked mid-send: signal the test and never
                # return until released, so the request is cancelled while the
                # MSG_HISTORY_REQUEST is still in flight.
                sending.set()
                await release.wait()

        await manager.connect("m1", BlockingWS())

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            r = await client.post(
                "/api/auth/login", json={"username": "admin", "password": "pw"}
            )
            assert r.status_code == 200, r.text

            task = asyncio.create_task(client.get("/api/history/f1"))
            # Wait until the leader is blocked inside ``send_text``.
            await asyncio.wait_for(sending.wait(), timeout=5)
            # Cancel the request while the daemon send is still blocked.
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            release.set()

        # The leader's waiter must have been dropped and the marker cleared even
        # though the cancellation happened during the send, so the next request
        # leads a fresh pull instead of parking as a follower forever.
        fut, is_leader = registry.begin_pull("f1", "m1")
        assert is_leader is True

    asyncio.run(scenario())


def test_history_detail_leader_cancel_releases_parked_followers():
    """A shared-pull leader cancelled BEFORE a successful daemon dispatch, while
    a follower is parked, must release the follower immediately rather than
    leaving it stranded until ``HISTORY_PULL_TIMEOUT``.

    Regression: the leader's ``finally`` only ``discard``-ed its own waiter, so
    with a follower still parked the in-flight marker stayed set. The follower
    waited out the full timeout on a ``MSG_HISTORY_REQUEST`` that was never
    sent, and every later request joined the abandoned pull. The follower now
    receives ``_PullAbandoned``, retries as the new leader, dispatches a fresh
    request, and completes.
    """
    import httpx

    from _authsrv import authed_app

    async def scenario():
        app, _key = authed_app()
        owner_id = app.state.test_owner_id
        state = app.state.server_state
        manager = app.state.connection_manager
        registry = app.state.history_registry

        await state.register_machine("m1", owner_id=owner_id)
        await state.update_history_index("m1", [{"flow_id": "f1"}])

        first_sending = asyncio.Event()
        second_sent = asyncio.Event()
        sends: list = []

        class FirstBlocksWS:
            async def send_text(self, _data):
                sends.append(1)
                if len(sends) == 1:
                    # Leader's send blocks so a follower can park, then the
                    # leader is cancelled mid-send (before any dispatch).
                    first_sending.set()
                    await asyncio.Event().wait()  # blocks until cancelled
                else:
                    # The follower retried as the new leader and dispatched a
                    # genuine fresh request — exactly what must happen instead
                    # of stranding it behind the abandoned pull.
                    second_sent.set()

        await manager.connect("m1", FirstBlocksWS())

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            r = await client.post(
                "/api/auth/login", json={"username": "admin", "password": "pw"}
            )
            assert r.status_code == 200, r.text

            leader = asyncio.create_task(client.get("/api/history/f1"))
            await asyncio.wait_for(first_sending.wait(), timeout=5)

            # A second concurrent request parks as a follower behind the leader.
            follower = asyncio.create_task(client.get("/api/history/f1"))
            # Give the follower enough turns to reach its parked ``await``.
            for _ in range(10):
                await asyncio.sleep(0)

            # Cancel the leader while its send is still blocked (no dispatch).
            leader.cancel()
            with pytest.raises(asyncio.CancelledError):
                await leader

            # The follower must be released and retry as the new leader,
            # dispatching a fresh request well within the pull timeout.
            await asyncio.wait_for(second_sent.wait(), timeout=5)

            # Resolve the follower-leader's pull with an authoritative reply.
            await state.append_history(
                "f1",
                protocol.HISTORY_MODE_FULL,
                [{"step": "s1", "line": "full-a"}],
                machine_id="m1",
            )
            registry.resolve("f1", {"records": []}, machine_id="m1")

            resp = await asyncio.wait_for(follower, timeout=5)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["delivery"] == "full"
            assert [rec["line"] for rec in body["records"]] == ["full-a"]

        # The marker is clear afterwards: a fresh request leads a new pull.
        fut, is_leader = registry.begin_pull("f1", "m1")
        assert is_leader is True

    asyncio.run(scenario())


def test_history_endpoints_empty(client_and_app):
    client, _ = client_and_app
    resp = client.get("/api/history")
    assert resp.status_code == 200
    assert resp.json() == {"sessions": [], "count": 0}


# --------------------------------------------------------------------------
# IndexRefreshRegistry + broadcast_index_refresh (unit)
# --------------------------------------------------------------------------


class _FakeServerWS:
    """A server-side WebSocket stand-in capturing what the server sends down."""

    def __init__(self):
        self.sent = []

    async def send_text(self, data):
        self.sent.append(protocol.decode(data))


def test_index_refresh_registry_resolve_and_discard():
    from se3.server.ws import IndexRefreshRegistry

    async def scenario():
        reg = IndexRefreshRegistry()
        fut = reg.register("m1")
        reg.resolve("m1")
        assert fut.result() is True
        # resolve with no parked waiter is a no-op (no error).
        reg.resolve("ghost")
        # discard removes a waiter without resolving it.
        fut2 = reg.register("m2")
        reg.discard("m2", fut2)
        assert not fut2.done()
        # discard on an already-cleared machine is harmless.
        reg.discard("m2", fut2)

    asyncio.run(scenario())


def test_broadcast_index_refresh_sends_to_connected_and_returns_waiters():
    from se3.server.ws import (
        ConnectionManager,
        IndexRefreshRegistry,
        broadcast_index_refresh,
    )

    async def scenario():
        mgr = ConnectionManager()
        ws1, ws2 = _FakeServerWS(), _FakeServerWS()
        await mgr.connect("m1", ws1)
        await mgr.connect("m2", ws2)
        reg = IndexRefreshRegistry()

        waiters = await broadcast_index_refresh(mgr, reg)

        assert set(waiters) == {"m1", "m2"}
        assert ws1.sent[0].type == protocol.MSG_HISTORY_INDEX_REQUEST
        assert ws2.sent[0].type == protocol.MSG_HISTORY_INDEX_REQUEST
        # A daemon's re-push resolves only its own waiter.
        reg.resolve("m1")
        assert waiters["m1"].result() is True
        assert not waiters["m2"].done()

    asyncio.run(scenario())


def test_broadcast_index_refresh_no_daemon_returns_empty():
    from se3.server.ws import (
        ConnectionManager,
        IndexRefreshRegistry,
        broadcast_index_refresh,
    )

    async def scenario():
        waiters = await broadcast_index_refresh(
            ConnectionManager(), IndexRefreshRegistry()
        )
        assert waiters == {}

    asyncio.run(scenario())


def test_broadcast_index_refresh_discards_waiter_on_send_failure():
    from se3.server.ws import (
        ConnectionManager,
        IndexRefreshRegistry,
        broadcast_index_refresh,
    )

    class _BadWS:
        async def send_text(self, data):
            raise RuntimeError("boom")

    async def scenario():
        mgr = ConnectionManager()
        await mgr.connect("m1", _BadWS())
        reg = IndexRefreshRegistry()
        waiters = await broadcast_index_refresh(mgr, reg)
        # Send failed -> the machine is not returned as a waiter ...
        assert waiters == {}
        # ... and no dangling waiter was left behind in the registry.
        reg.resolve("m1")  # no-op, must not raise

    asyncio.run(scenario())


# --------------------------------------------------------------------------
# GET /api/history actively refreshes the index on entry
# --------------------------------------------------------------------------


def test_history_list_broadcasts_index_refresh_request(client_and_app):
    """Entering the history view asks every connected daemon to re-push."""
    client, app = client_and_app
    with client.websocket_connect("/ws") as daemon:
        daemon.send_text(authed_hello(app, "m1", "host", "6.4.0"))
        protocol.decode(daemon.receive_text())  # WELCOME

        result: dict = {}

        def do_get():
            result["resp"] = client.get("/api/history")

        worker = threading.Thread(target=do_get)
        worker.start()
        try:
            req = _receive_until(daemon, protocol.MSG_HISTORY_INDEX_REQUEST)
            assert req.type == protocol.MSG_HISTORY_INDEX_REQUEST
        finally:
            worker.join(timeout=5)
        assert result["resp"].status_code == 200


def test_history_list_returns_latest_after_forced_repush(client_and_app):
    """A stale cached index (5/14) is replaced by the daemon's forced
    re-push (5/21) before GET /api/history aggregates and returns."""
    client, app = client_and_app
    with client.websocket_connect("/ws") as daemon:
        daemon.send_text(authed_hello(app, "m1", "host", "6.4.0"))
        protocol.decode(daemon.receive_text())  # WELCOME
        # Stale index the server caches up front (only an old 5/14 entry).
        daemon.send_text(
            protocol.make_history_index(
                [{"flow_id": "f1", "updated_at": "2026-05-14"}]
            ).to_json()
        )

        result: dict = {}

        def do_get():
            result["resp"] = client.get("/api/history")

        worker = threading.Thread(target=do_get)
        worker.start()
        try:
            # The GET broadcasts a forced index-refresh; the daemon answers
            # with a fresh index carrying the latest 5/21 session.
            req = _receive_until(daemon, protocol.MSG_HISTORY_INDEX_REQUEST)
            assert req.type == protocol.MSG_HISTORY_INDEX_REQUEST
            daemon.send_text(
                protocol.make_history_index(
                    [{"flow_id": "f1", "updated_at": "2026-05-21"}]
                ).to_json()
            )
        finally:
            worker.join(timeout=5)

        resp = result["resp"]
        assert resp.status_code == 200
        sessions = resp.json()["sessions"]
        assert sessions and sessions[0]["flow_id"] == "f1"
        # The forced re-push won: the response carries the latest date.
        assert sessions[0]["updated_at"] == "2026-05-21"


def test_history_list_degrades_to_cache_on_timeout(client_and_app):
    """A connected daemon that never answers the refresh request still yields
    a prompt 200 with the currently cached index (no 5xx, no hang)."""
    client, app = client_and_app
    with client.websocket_connect("/ws") as daemon:
        daemon.send_text(authed_hello(app, "m1", "host", "6.4.0"))
        protocol.decode(daemon.receive_text())  # WELCOME
        daemon.send_text(
            protocol.make_history_index(
                [{"flow_id": "f1", "updated_at": "2026-05-14"}]
            ).to_json()
        )

        result: dict = {}

        def do_get():
            result["resp"] = client.get("/api/history")

        worker = threading.Thread(target=do_get)
        worker.start()
        try:
            # Drain the refresh request but never answer it -> forced timeout.
            _receive_until(daemon, protocol.MSG_HISTORY_INDEX_REQUEST)
        finally:
            worker.join(timeout=5)

        resp = result["resp"]
        assert resp.status_code == 200
        sessions = resp.json()["sessions"]
        assert sessions and sessions[0]["flow_id"] == "f1"


def test_history_list_no_daemon_returns_200_without_blocking(client_and_app):
    """With no connected daemon the endpoint returns the cached index
    immediately (no waiter to await) and never errors."""
    client, _ = client_and_app
    resp = client.get("/api/history")
    assert resp.status_code == 200
    assert resp.json() == {"sessions": [], "count": 0}


# --------------------------------------------------------------------------
# REST incremental history: GET /api/history/{flow_id}?after=<progress>
# --------------------------------------------------------------------------


def _seed_and_confirm(ui, daemon, app, *, flow_id, mode, records):
    """Push a history_data frame and wait for the matching UI broadcast.

    ``MSG_HISTORY_DATA`` is processed asynchronously by the server (cache write
    then UI broadcast). Draining the broadcast guarantees the cache landed
    before the test issues its REST read, so the read never slips into the
    daemon-pull (cache-miss) path and blocks.
    """
    daemon.send_text(protocol.make_history_data(flow_id, mode, records).to_json())
    pushed = json.loads(ui.receive_text())
    assert pushed["type"] == "history_data"
    assert pushed["flow_id"] == flow_id


def test_history_detail_full_response_carries_progress(client_and_app):
    """A request without ``after`` returns the complete bundle, tagged
    ``delivery: "full"`` and carrying a fresh progress token."""
    client, app = client_and_app
    with client.websocket_connect("/ws/ui") as ui:
        assert json.loads(ui.receive_text())["type"] == "snapshot"
        with client.websocket_connect("/ws") as daemon:
            daemon.send_text(authed_hello(app, "m1", "host", "6.4.0"))
            protocol.decode(daemon.receive_text())  # WELCOME
            assert json.loads(ui.receive_text())["type"] == "status_update"
            _seed_and_confirm(
                ui, daemon, app,
                flow_id="f1", mode=protocol.HISTORY_MODE_FULL,
                records=[{"step": "s1", "line": 1}, {"step": "s1", "line": 2}],
            )
            body = client.get("/api/history/f1").json()
            assert body["cached"] is True
            assert body["delivery"] == "full"
            assert [r["line"] for r in body["records"]] == [1, 2]
            # The progress token is usable for a follow-up delta read.
            prog = decode_progress(body["progress"])
            assert prog is not None and prog["offset"] == 2


def test_history_detail_delta_returns_only_new_records(client_and_app):
    """After holding a progress token, a reconnect refetch returns only the
    records appended since — no duplicates, no loss, original order."""
    client, app = client_and_app
    with client.websocket_connect("/ws/ui") as ui:
        assert json.loads(ui.receive_text())["type"] == "snapshot"
        with client.websocket_connect("/ws") as daemon:
            daemon.send_text(authed_hello(app, "m1", "host", "6.4.0"))
            protocol.decode(daemon.receive_text())  # WELCOME
            assert json.loads(ui.receive_text())["type"] == "status_update"
            _seed_and_confirm(
                ui, daemon, app,
                flow_id="f1", mode=protocol.HISTORY_MODE_FULL,
                records=[{"step": "s1", "line": 1}, {"step": "s1", "line": 2}],
            )
            first = client.get("/api/history/f1").json()
            progress = first["progress"]
            # Daemon appends two more records while the client is "disconnected".
            _seed_and_confirm(
                ui, daemon, app,
                flow_id="f1", mode=protocol.HISTORY_MODE_APPEND,
                records=[{"step": "s1", "line": 3}, {"step": "s1", "line": 4}],
            )
            second = client.get(
                "/api/history/f1", params={"after": progress}
            ).json()
            assert second["delivery"] == "delta"
            # Only the appended tail, in order, with no duplication of 1/2.
            assert [r["line"] for r in second["records"]] == [3, 4]
            # Re-using the refreshed token yields an empty delta (nothing new).
            third = client.get(
                "/api/history/f1", params={"after": second["progress"]}
            ).json()
            assert third["delivery"] == "delta"
            assert third["records"] == []


def test_history_detail_stale_token_after_full_replace_falls_back(client_and_app):
    """A token issued before a full-bundle replacement no longer pins the
    current generation, so the server falls back to a full response."""
    client, app = client_and_app
    with client.websocket_connect("/ws/ui") as ui:
        assert json.loads(ui.receive_text())["type"] == "snapshot"
        with client.websocket_connect("/ws") as daemon:
            daemon.send_text(authed_hello(app, "m1", "host", "6.4.0"))
            protocol.decode(daemon.receive_text())  # WELCOME
            assert json.loads(ui.receive_text())["type"] == "status_update"
            _seed_and_confirm(
                ui, daemon, app,
                flow_id="f1", mode=protocol.HISTORY_MODE_FULL,
                records=[{"step": "s1", "line": 1}],
            )
            stale = client.get("/api/history/f1").json()["progress"]
            # A new full bundle replaces the cache and rolls the generation.
            _seed_and_confirm(
                ui, daemon, app,
                flow_id="f1", mode=protocol.HISTORY_MODE_FULL,
                records=[{"step": "s2", "line": 9}],
            )
            body = client.get(
                "/api/history/f1", params={"after": stale}
            ).json()
            assert body["delivery"] == "full"
            assert [r["line"] for r in body["records"]] == [9]


def test_history_detail_cache_miss_with_token_still_pulls_full(client_and_app):
    """A cache miss pulls from the owning daemon even when the client supplies
    an ``after`` token, and the pulled result is returned as a full response."""
    client, app = client_and_app
    with client.websocket_connect("/ws") as daemon:
        daemon.send_text(authed_hello(app, "m1", "host", "6.4.0"))
        protocol.decode(daemon.receive_text())  # WELCOME
        # Report the index so the server knows m1 owns f1 (but nothing cached).
        daemon.send_text(protocol.make_history_index([{"flow_id": "f1"}]).to_json())
        for _ in range(50):
            if client.get("/api/history").json()["sessions"]:
                break

        result: dict = {}
        # A syntactically valid token that cannot match any cached bundle.
        bogus = encode_progress(1, 5, "m1")

        def do_get():
            result["resp"] = client.get(
                "/api/history/f1", params={"after": bogus}
            )

        worker = threading.Thread(target=do_get)
        worker.start()
        try:
            # Despite the token, a cache miss must still request from the daemon.
            req = _receive_until(daemon, protocol.MSG_HISTORY_REQUEST)
            assert req.payload["flow_id"] == "f1"
            daemon.send_text(
                protocol.make_history_data(
                    "f1",
                    protocol.HISTORY_MODE_FULL,
                    [{"step": "s1", "line": "pulled"}],
                ).to_json()
            )
        finally:
            worker.join(timeout=5)
        body = result["resp"].json()
        assert result["resp"].status_code == 200
        assert body["cached"] is False
        assert body["delivery"] == "full"
        assert body["records"][0]["line"] == "pulled"
        assert decode_progress(body["progress"]) is not None


def test_history_detail_rejects_cache_replaced_after_pinned_pull(
    client_and_app, monkeypatch
):
    """The post-pull snapshot must still belong to the owner-validated machine.

    A same-flow cache replacement between the daemon reply and the final read
    fails closed instead of returning the registry's unvalidated raw payload.
    """
    import se3.server.app as app_module

    client, app = client_and_app
    with client.websocket_connect("/ws") as daemon:
        daemon.send_text(authed_hello(app, "m1", "host", "6.4.0"))
        protocol.decode(daemon.receive_text())  # WELCOME
        daemon.send_text(protocol.make_history_index([{"flow_id": "f1"}]).to_json())
        for _ in range(50):
            if client.get("/api/history").json()["sessions"]:
                break

        snapshot_calls = []

        async def missing_snapshot(
            flow_id,
            *,
            after=None,
            expected_machine_id=None,
            expected_owner=None,
        ):
            snapshot_calls.append(
                (flow_id, after, expected_machine_id, expected_owner)
            )
            return None

        sent_to = []

        async def pinned_request(manager, state, flow_id, *, machine_id=None, **kwargs):
            sent_to.append(machine_id)
            app.state.history_registry.resolve(
                flow_id,
                {"records": [{"line": "unvalidated"}]},
                machine_id=machine_id,
            )
            return True

        monkeypatch.setattr(
            app.state.server_state, "get_history_snapshot", missing_snapshot
        )
        monkeypatch.setattr(app_module, "request_history", pinned_request)

        resp = client.get("/api/history/f1")
        assert resp.status_code == 409
        assert sent_to == ["m1"]
        assert snapshot_calls == [
            ("f1", None, "m1", app.state.test_owner_id),
            ("f1", None, "m1", app.state.test_owner_id),
        ]
        assert "unvalidated" not in resp.text


def test_history_detail_serves_full_after_same_owner_daemon_reconnect(
    client_and_app, monkeypatch
):
    """A same-machine, same-owner daemon reconnect during the pull window must
    NOT discard the freshly cached snapshot.

    The cache miss triggers an on-demand pull; while it is in flight the daemon
    briefly drops and re-dials under the same ``machine_id`` and same owner, so
    its socket object differs but the data it pushes is authoritative. The
    waiter resolves on a frame from the new connection and the cache now holds
    valid full records that ``get_history_snapshot`` (bound to the owning
    machine + owner) validates. The route MUST return that full snapshot rather
    than reject it just because the socket object changed."""
    import se3.server.app as app_module

    client, app = client_and_app
    with client.websocket_connect("/ws") as daemon:
        daemon.send_text(authed_hello(app, "m1", "host", "6.4.0"))
        protocol.decode(daemon.receive_text())  # WELCOME
        daemon.send_text(protocol.make_history_index([{"flow_id": "f1"}]).to_json())
        for _ in range(50):
            if client.get("/api/history").json()["sessions"]:
                break

        async def missing_then_full(
            flow_id,
            *,
            after=None,
            expected_machine_id=None,
            expected_owner=None,
        ):
            # The second read (after the pull) validates against the owning
            # machine + owner; returning records models a successful
            # validation, i.e. the bundle still belongs to m1 / this owner.
            if after is None and getattr(missing_then_full, "called", False):
                assert expected_machine_id == "m1"
                assert expected_owner == app.state.test_owner_id
                return {
                    "delivery": "full",
                    "records": [{"line": "authoritative"}],
                    "progress": "p",
                }
            missing_then_full.called = True
            return None

        async def complete_pull(
            manager, state, flow_id, *, machine_id=None, **kwargs
        ):
            app.state.history_registry.resolve(
                flow_id, {"records": []}, machine_id=machine_id
            )
            return True

        # The daemon reconnected under the same machine id during the pull, so
        # the socket object differs. This must no longer gate the response.
        async def replaced_connection(machine_id, websocket):
            return False

        monkeypatch.setattr(
            app.state.server_state, "get_history_snapshot", missing_then_full
        )
        monkeypatch.setattr(app_module, "request_history", complete_pull)
        monkeypatch.setattr(
            app.state.connection_manager,
            "is_current_connection",
            replaced_connection,
        )

        resp = client.get("/api/history/f1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["cached"] is False
        assert body["delivery"] == "full"
        assert body["records"] == [{"line": "authoritative"}]


def test_history_detail_fails_closed_when_snapshot_validation_fails(
    client_and_app, monkeypatch
):
    """When the post-pull snapshot validation actually fails (the bundle's
    machine/owner changed), the route fails closed with 409 rather than leaking
    records from a different machine that reused the same flow id."""
    import se3.server.app as app_module

    client, app = client_and_app
    with client.websocket_connect("/ws") as daemon:
        daemon.send_text(authed_hello(app, "m1", "host", "6.4.0"))
        protocol.decode(daemon.receive_text())  # WELCOME
        daemon.send_text(protocol.make_history_index([{"flow_id": "f1"}]).to_json())
        for _ in range(50):
            if client.get("/api/history").json()["sessions"]:
                break

        async def always_missing(
            flow_id,
            *,
            after=None,
            expected_machine_id=None,
            expected_owner=None,
        ):
            # Validation never passes — models the bundle having moved to a
            # different machine/owner while the pull was in flight.
            return None

        async def complete_pull(
            manager, state, flow_id, *, machine_id=None, **kwargs
        ):
            app.state.history_registry.resolve(
                flow_id, {"records": [{"line": "must-not-leak"}]}, machine_id=machine_id
            )
            return True

        monkeypatch.setattr(
            app.state.server_state, "get_history_snapshot", always_missing
        )
        monkeypatch.setattr(app_module, "request_history", complete_pull)

        resp = client.get("/api/history/f1")
        assert resp.status_code == 409
        assert "must-not-leak" not in resp.text


def test_history_detail_ownership_gate_precedes_token(client_and_app):
    """The ownership / machine resolution gate runs before any data read, so
    an unknown flow 404s even when an ``after`` token is supplied."""
    client, _ = client_and_app
    resp = client.get("/api/history/ghost", params={"after": encode_progress(1, 0, "m1")})
    assert resp.status_code == 404
