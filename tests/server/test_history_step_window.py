"""Tail-first, step-block windowed history delivery.

The defect these lock down (observed on the real flow ``20260829-224712_878b4fc9``,
222 MB on disk / 554 MiB resident as a bundle against a 256 MiB cache budget):
``GET /api/history/{flow}`` only ever answered with the WHOLE flow, and the whole
flow did not fit. The bundle was evicted, the browser's next page missed, the
whole flow was pulled from the daemon again, and it was evicted again — a steady
state in which the reader never saw more than a prefix and the console repeated
``history delivery incomplete`` forever.

The fix makes the transport windowed. ``?window=N`` serves the flow's LAST N step
blocks; ``?window=N&before=<step>`` serves the N blocks before that one. Three
sources answer it, and all three are exercised here:

* the cached bundle, sliced by block (free, and the common case);
* a direct daemon window read, relayed WITHOUT ever creating a bundle — the path
  that makes a flow bigger than the whole cache budget browsable to its first
  block;
* the pre-revision-9 fallback: the existing whole-flow pull, then the same slice.

Plus the two properties that make the storm impossible rather than merely
unlikely: a flow whose daemon pull is in flight is unevictable, and a windowed
browse of an over-budget flow performs no eviction⇄re-pull cycle at all.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from _authsrv import authed_app, authed_hello, login
from tianluo.daemon import protocol
from tianluo.server.state import ServerState

FLOW = "flow-window"
MACHINE = "m1"


def _record(step, ordinal, payload_chars=200):
    return {
        "step_id": step,
        "step_type": "implement",
        "ordinal": ordinal,
        "message": {"role": "assistant", "content": "x" * payload_chars},
    }


def _blocks(count, per_block=3, payload_chars=200):
    """``count`` step blocks of ``per_block`` records, in flow order."""
    out = []
    for i in range(count):
        step = "%02d_implement_h%02d" % (i, i)
        for j in range(per_block):
            out.append(_record(step, j, payload_chars))
    return out


def _step_ids(count):
    return ["%02d_implement_h%02d" % (i, i) for i in range(count)]


def _cursor(count, per_block=3):
    return {"%s.jsonl" % s: per_block for s in _step_ids(count)}


# ==========================================================================
# ServerState: slicing a cached bundle by step block
# ==========================================================================


async def _seed_state(state, records, flow=FLOW, cursor=None):
    await state.apply_history_frame(
        flow,
        protocol.HISTORY_MODE_FULL,
        records,
        cursor=cursor or {},
        machine_id=MACHINE,
    )


def test_the_default_window_is_the_tail_of_the_flow():
    async def scenario():
        state = ServerState(history_cache_budget_bytes=1024 * 1024 * 1024)
        await _seed_state(state, _blocks(8))
        snap = await state.get_history_window_snapshot(FLOW, count=3)
        assert snap["delivery"] == "window"
        assert snap["window"]["mode"] == "tail"
        assert snap["window"]["loaded"] == _step_ids(8)[-3:]
        assert snap["window"]["first_index"] == 5
        assert snap["window"]["has_earlier"] is True
        # …and only the windowed blocks' records travel.
        assert {r["step_id"] for r in snap["records"]} == set(_step_ids(8)[-3:])
        assert len(snap["records"]) == 9

    asyncio.run(scenario())


def test_the_block_index_travels_whole_so_the_client_can_page_itself():
    async def scenario():
        state = ServerState(history_cache_budget_bytes=1024 * 1024 * 1024)
        await _seed_state(state, _blocks(8))
        snap = await state.get_history_window_snapshot(FLOW, count=2)
        # Every block id, not just the window's: a handful of short strings, and
        # it is what lets the browser bound its completeness self-check to the
        # blocks it has actually loaded.
        assert snap["window"]["steps"] == _step_ids(8)

    asyncio.run(scenario())


def test_paging_back_reaches_the_first_block_covering_every_record():
    async def scenario():
        state = ServerState(history_cache_budget_bytes=1024 * 1024 * 1024)
        await _seed_state(state, _blocks(9))
        snap = await state.get_history_window_snapshot(FLOW, count=2)
        seen = list(snap["records"])
        pages = 1
        while snap["window"]["has_earlier"]:
            anchor = snap["window"]["steps"][snap["window"]["first_index"]]
            snap = await state.get_history_window_snapshot(
                FLOW, count=2, before_step=anchor
            )
            assert snap["window"]["mode"] == "before"
            assert snap["records"], "a page-up above the first block was empty"
            seen = list(snap["records"]) + seen
            pages += 1
        assert pages == 5
        # The union of the pages IS the flow, in flow order, with nothing
        # duplicated and nothing skipped.
        assert seen == _blocks(9)

    asyncio.run(scenario())


def test_a_window_wider_than_the_flow_is_the_whole_flow():
    async def scenario():
        state = ServerState(history_cache_budget_bytes=1024 * 1024 * 1024)
        await _seed_state(state, _blocks(3))
        snap = await state.get_history_window_snapshot(FLOW, count=50)
        assert snap["window"]["first_index"] == 0
        assert snap["window"]["has_earlier"] is False
        assert snap["records"] == _blocks(3)

    asyncio.run(scenario())


def test_an_unknown_anchor_yields_an_empty_window_not_the_tail():
    async def scenario():
        state = ServerState(history_cache_budget_bytes=1024 * 1024 * 1024)
        await _seed_state(state, _blocks(4))
        snap = await state.get_history_window_snapshot(
            FLOW, count=2, before_step="99_never"
        )
        # Silently serving the tail would answer a page-up with the page the
        # reader already holds — indistinguishable, to them, from the history
        # simply stopping.
        assert snap["records"] == [] and snap["window"]["loaded"] == []
        assert snap["window"]["steps"] == _step_ids(4)
        # …and "empty" must not also say "you are standing on the first block":
        # that retires the reader's page-up and un-scopes their completeness
        # check onto a head they never loaded.
        assert snap["window"]["has_earlier"] is True
        assert snap["window"]["first_index"] == 4

    asyncio.run(scenario())


def test_an_unresolvable_page_up_never_claims_the_head_is_loaded():
    """A bundle holding only PART of the flow must not answer "you're at 0".

    The shape that produces it: the reader paged up through a daemon-relayed
    window covering all of a big flow, and meanwhile a bundle appeared
    server-side holding only the leading blocks (a large recovery lands as a
    ``full`` head followed by ``append`` tails, so mid-drain the bundle is a
    prefix). The next page-up is then answered from that cache with an anchor
    the bundle does not contain.
    """
    async def scenario():
        state = ServerState(history_cache_budget_bytes=1024 * 1024 * 1024)
        # The bundle holds blocks 0..3 of a flow the reader knows has more.
        await _seed_state(state, _blocks(4))
        snap = await state.get_history_window_snapshot(
            FLOW, count=2, before_step="07_implement_h07"
        )
        window = snap["window"]
        assert window["loaded"] == [] and snap["records"] == []
        # `has_earlier` false + first_index 0 is the exact reply that removed the
        # 'Load earlier steps' control, un-scoped findMissingOrdinals, and drove
        # the backfill / full re-pull escalation the windowing exists to prevent.
        assert window["has_earlier"] is True
        assert window["first_index"] == len(window["steps"])
        assert window["last_index"] == len(window["steps"]) - 1

    asyncio.run(scenario())


def test_both_window_legs_answer_an_unresolvable_anchor_identically():
    """Cache leg and daemon-relay leg are one contract, not two dialects."""
    from tianluo.server.app import _window_payload_from_daemon

    async def scenario():
        state = ServerState(history_cache_budget_bytes=1024 * 1024 * 1024)
        await _seed_state(state, _blocks(4))
        cached = await state.get_history_window_snapshot(
            FLOW, count=2, before_step="99_never"
        )
        relayed = _window_payload_from_daemon(
            FLOW, MACHINE,
            {"records": [], "steps": _step_ids(4), "window": [], "counts": {}},
            count=2, before_step="99_never",
        )
        for key in ("loaded", "first_index", "last_index", "has_earlier"):
            assert cached["window"][key] == relayed["window"][key], key

    asyncio.run(scenario())


def test_the_progress_token_still_pins_the_WHOLE_bundle():
    async def scenario():
        state = ServerState(history_cache_budget_bytes=1024 * 1024 * 1024)
        await _seed_state(state, _blocks(6))
        snap = await state.get_history_window_snapshot(FLOW, count=2)
        # The token means "records of this bundle the server has sent"; minting
        # it at the window's edge would make the next poll re-ship the head the
        # windowed client deliberately did not ask for. Minted at the FULL count,
        # the ordinary delta poll is an append-only tail read — live follow keeps
        # working — and the unloaded head stays unrequested.
        delta = await state.get_history_snapshot(
            FLOW, after=snap["progress"], known_signature=snap["signature"]
        )
        assert delta["delivery"] == "not_modified"
        assert delta["records"] == []

    asyncio.run(scenario())


def test_a_window_read_is_a_ui_view_that_protects_the_bundle():
    async def scenario():
        state = ServerState(history_cache_budget_bytes=1)
        await _seed_state(state, _blocks(4))
        # Reading a window is a UI read of this flow, so it refreshes the
        # eviction recency exactly as a whole-bundle read does — a reader paging
        # backwards must not have the bundle swept out from under them.
        assert await state.get_history_window_snapshot(FLOW, count=2) is not None
        await _seed_state(state, _blocks(2), flow="other")
        assert FLOW in state._history_data

    asyncio.run(scenario())


def test_a_bundle_from_another_machine_reads_as_a_miss():
    async def scenario():
        state = ServerState(history_cache_budget_bytes=1024 * 1024 * 1024)
        await _seed_state(state, _blocks(3))
        assert await state.get_history_window_snapshot(
            FLOW, count=2, expected_machine_id="m-other"
        ) is None

    asyncio.run(scenario())


# ==========================================================================
# the anti-storm guarantees
# ==========================================================================


def test_a_flow_whose_pull_is_in_flight_is_never_evicted():
    async def scenario():
        state = ServerState(history_cache_budget_bytes=1)
        await _seed_state(state, _blocks(4))
        # Age the view marker past the hot window: a large flow's drain takes far
        # longer than it, and nothing re-reads while the browser is parked on the
        # reply — which is exactly when the old code evicted the bundle the drain
        # was still filling.
        state._history_read_at[FLOW] = -1e9
        await state.pin_history_pull(FLOW)
        await _seed_state(state, _blocks(2), flow="other")
        assert FLOW in state._history_data, "the drain's own bundle was evicted"
        await state.release_history_pull(FLOW)
        await _seed_state(state, _blocks(2), flow="other2")
        # Released, and cold again: the pin is bounded by the pull, not a leak.
        assert FLOW not in state._history_data

    asyncio.run(scenario())


def test_the_pull_pin_ages_out_so_a_lost_caller_cannot_wedge_the_budget():
    async def scenario():
        state = ServerState(history_cache_budget_bytes=1)
        await _seed_state(state, _blocks(4))
        state._history_read_at[FLOW] = -1e9
        await state.pin_history_pull(FLOW)
        # A caller that died without releasing (cancelled request, crashed task)
        # must not make its flow permanently unevictable.
        state._history_pull_pinned[FLOW] -= state._HISTORY_PULL_PIN_TTL + 1
        await _seed_state(state, _blocks(2), flow="other")
        assert FLOW not in state._history_data

    asyncio.run(scenario())


def test_browsing_an_over_budget_flow_to_its_first_block_evicts_nothing(caplog):
    """The acceptance property: one flow, bigger than the whole cache budget."""
    async def scenario():
        probe = ServerState(history_cache_budget_bytes=1024 * 1024 * 1024)
        await _seed_state(probe, _blocks(10, payload_chars=4000))
        size = probe._bundle_bytes(probe._history_data[FLOW])
        # A budget the flow alone blows through, several times over.
        state = ServerState(history_cache_budget_bytes=size // 4)
        await _seed_state(state, _blocks(10, payload_chars=4000))
        with caplog.at_level(logging.INFO, logger="tianluo.server.state"):
            snap = await state.get_history_window_snapshot(FLOW, count=2)
            pages = 1
            while snap["window"]["has_earlier"]:
                anchor = snap["window"]["steps"][snap["window"]["first_index"]]
                snap = await state.get_history_window_snapshot(
                    FLOW, count=2, before_step=anchor
                )
                pages += 1
                assert pages < 20, "paging did not converge on the first block"
        assert snap["window"]["first_index"] == 0
        # Not one eviction across the whole browse — the loop that made this
        # flow un-openable cannot even start.
        assert "history-cache EVICT flow=%s" % FLOW not in caplog.text
        assert not any(
            "EVICT flow=%s" % FLOW in r.message for r in caplog.records
        )
        assert FLOW in state._history_data

    asyncio.run(scenario())


def test_the_over_budget_report_still_names_the_flow_holding_the_memory():
    """Over-budget attribution is preserved, not traded away for the fix."""
    async def scenario():
        probe = ServerState(history_cache_budget_bytes=1024 * 1024 * 1024)
        await _seed_state(probe, _blocks(6, payload_chars=4000))
        size = probe._bundle_bytes(probe._history_data[FLOW])
        state = ServerState(history_cache_budget_bytes=size // 3)
        await _seed_state(state, _blocks(6, payload_chars=4000))
        await state.get_history_window_snapshot(FLOW, count=2)
        stats = await state.history_cache_stats()
        assert stats["bytes"] > stats["budget_bytes"]
        assert any(t["flow_id"] == FLOW for t in stats["top"])

    asyncio.run(scenario())


def test_an_unwatched_flow_is_still_evicted():
    """The windowing must not turn the budget off for flows nobody is reading."""
    async def scenario():
        state = ServerState(history_cache_budget_bytes=1)
        await _seed_state(state, _blocks(4), flow="unwatched")
        await _seed_state(state, _blocks(4), flow="other")
        assert "unwatched" not in state._history_data

    asyncio.run(scenario())


# ==========================================================================
# the REST route
# ==========================================================================


@pytest.fixture()
def client_and_app():
    from fastapi.testclient import TestClient

    app, _key = authed_app()
    with TestClient(app) as client:
        login(client)
        yield client, app


def _connect(client, app, protocol_version="", usage_summary=None):
    daemon = client.websocket_connect("/ws")
    sock = daemon.__enter__()
    sock.send_text(
        authed_hello(
            app, MACHINE, "host", "6.4.0", protocol_version=protocol_version
        )
    )
    protocol.decode(sock.receive_text())  # WELCOME
    session = {"flow_id": FLOW}
    if usage_summary is not None:
        session["usage_summary"] = usage_summary
    sock.send_text(protocol.make_history_index([session]).to_json())
    return daemon, sock


def _seed_bundle(client, sock, records, cursor=None):
    sock.send_text(
        protocol.make_history_data(
            FLOW, protocol.HISTORY_MODE_FULL, records, cursor=cursor or {}
        ).to_json()
    )
    for _ in range(50):
        resp = client.get("/api/history/%s" % FLOW)
        if resp.status_code == 200 and resp.json().get("cached"):
            return resp
    raise AssertionError("bundle never became cache-visible")


def test_the_route_serves_a_tail_window_from_the_cache(client_and_app):
    client, app = client_and_app
    daemon, sock = _connect(client, app)
    try:
        _seed_bundle(client, sock, _blocks(8), cursor=_cursor(8))
        body = client.get(
            "/api/history/%s" % FLOW, params={"window": 3}
        ).json()
        assert body["delivery"] == "window"
        assert body["window"]["loaded"] == _step_ids(8)[-3:]
        assert body["window"]["has_earlier"] is True
        assert {r["step_id"] for r in body["records"]} == set(_step_ids(8)[-3:])
        # The cursor still describes the WHOLE flow — the client scopes its own
        # completeness check to the blocks it holds, rather than the server
        # pretending the unloaded head does not exist.
        assert body["cursor"] == _cursor(8)
    finally:
        daemon.__exit__(None, None, None)


def test_the_route_pages_backwards_by_block(client_and_app):
    client, app = client_and_app
    daemon, sock = _connect(client, app)
    try:
        _seed_bundle(client, sock, _blocks(8), cursor=_cursor(8))
        first = client.get("/api/history/%s" % FLOW, params={"window": 3}).json()
        anchor = first["window"]["steps"][first["window"]["first_index"]]
        page = client.get(
            "/api/history/%s" % FLOW, params={"window": 3, "before": anchor}
        ).json()
        assert page["window"]["mode"] == "before"
        assert page["window"]["loaded"] == _step_ids(8)[2:5]
        assert page["window"]["has_earlier"] is True
    finally:
        daemon.__exit__(None, None, None)


def test_an_unwindowed_request_is_byte_for_byte_what_it_always_was(client_and_app):
    client, app = client_and_app
    daemon, sock = _connect(client, app)
    try:
        resp = _seed_bundle(client, sock, _blocks(8), cursor=_cursor(8))
        body = resp.json()
        # No `window` parameter → the full snapshot dialect, untouched: an older
        # browser (and every non-conversation consumer) keeps its behaviour.
        assert body["delivery"] == "full"
        assert "window" not in body
        assert len(body["records"]) == 24
    finally:
        daemon.__exit__(None, None, None)


def test_the_window_size_is_capped(client_and_app):
    from tianluo.server.app import HISTORY_WINDOW_MAX_BLOCKS

    client, app = client_and_app
    daemon, sock = _connect(client, app)
    try:
        _seed_bundle(client, sock, _blocks(4), cursor=_cursor(4))
        body = client.get(
            "/api/history/%s" % FLOW, params={"window": 10 ** 9}
        ).json()
        # A hand-crafted enormous window must not turn the windowed route back
        # into the whole-flow read it replaces.
        assert body["window"]["block_size"] == HISTORY_WINDOW_MAX_BLOCKS
    finally:
        daemon.__exit__(None, None, None)


def test_a_cache_miss_is_served_by_a_daemon_window_read_without_caching(
    client_and_app,
):
    """The path that makes an over-budget flow browsable at all."""
    client, app = client_and_app
    daemon, sock = _connect(client, app)
    try:
        _seed_bundle(client, sock, _blocks(8), cursor=_cursor(8))
        app.state.server_state._history_data.pop(FLOW, None)

        import threading

        result = {}

        def _ask():
            result["resp"] = client.get(
                "/api/history/%s" % FLOW, params={"window": 2}
            )

        worker = threading.Thread(target=_ask)
        worker.start()
        try:
            for _ in range(20):
                msg = protocol.decode(sock.receive_text())
                if msg.type == protocol.MSG_HISTORY_WINDOW_REQUEST:
                    break
            else:  # pragma: no cover - defensive
                raise AssertionError("no HISTORY_WINDOW_REQUEST relayed")
            assert msg.payload["count"] == 2
            assert "before_step" not in msg.payload
            steps = _step_ids(8)
            sock.send_text(
                protocol.make_history_window_data(
                    FLOW,
                    request_id=msg.payload["request_id"],
                    records=[r for r in _blocks(8) if r["step_id"] in steps[-2:]],
                    steps=steps,
                    window=steps[-2:],
                    counts=_cursor(8),
                ).to_json()
            )
        finally:
            worker.join(timeout=30)
        body = result["resp"].json()
        assert result["resp"].status_code == 200, result["resp"].text
        assert body["delivery"] == "window"
        assert body["window"]["source"] == "daemon"
        assert body["window"]["loaded"] == _step_ids(8)[-2:]
        assert len(body["records"]) == 6
        # No token, because there is no bundle for one to pin — and NO bundle was
        # created, which is the whole point: a flow larger than the cache budget
        # is served straight through instead of being cached and evicted.
        assert body["progress"] is None and body["signature"] is None
        assert FLOW not in app.state.server_state._history_data
        # …and it declares itself settled, so the browser's interrupted-delivery
        # repair loop is never armed for a bundle that does not exist.
        assert body["incomplete"] is False
    finally:
        daemon.__exit__(None, None, None)


def test_a_relayed_window_still_carries_the_session_usage(client_and_app):
    """The usage/cost surface must survive the leg that builds no bundle.

    The whole-flow pull carried the daemon's usage payload; the relayed window
    has no bundle for ``_bundle_usage`` to aggregate and must not answer with
    the window's own records (that would report ten blocks' cost as the
    session's total). It answers from the index summary the daemon already
    pushes — otherwise exactly the big flows this leg exists for would be the
    only ones opening with the usage region hidden.
    """
    summary = {
        "totals": {"input_tokens": 10, "output_tokens": 4},
        "actual_cost_usd": 1.25,
        "estimated_cost_usd": None,
        "completeness": "complete",
    }
    client, app = client_and_app
    daemon, sock = _connect(client, app, usage_summary=summary)
    try:
        _seed_bundle(client, sock, _blocks(6), cursor=_cursor(6))
        app.state.server_state._history_data.pop(FLOW, None)

        import threading

        result = {}

        def _ask():
            result["resp"] = client.get(
                "/api/history/%s" % FLOW, params={"window": 2}
            )

        worker = threading.Thread(target=_ask)
        worker.start()
        try:
            for _ in range(20):
                msg = protocol.decode(sock.receive_text())
                if msg.type == protocol.MSG_HISTORY_WINDOW_REQUEST:
                    break
            else:  # pragma: no cover - defensive
                raise AssertionError("no HISTORY_WINDOW_REQUEST relayed")
            steps = _step_ids(6)
            sock.send_text(
                protocol.make_history_window_data(
                    FLOW,
                    request_id=msg.payload["request_id"],
                    records=[r for r in _blocks(6) if r["step_id"] in steps[-2:]],
                    steps=steps, window=steps[-2:], counts=_cursor(6),
                ).to_json()
            )
        finally:
            worker.join(timeout=30)
        body = result["resp"].json()
        assert body["window"]["source"] == "daemon"
        assert body["usage"] == summary
    finally:
        daemon.__exit__(None, None, None)


def test_a_relayed_window_omits_usage_when_nothing_reports_any(client_and_app):
    """No fabricated zero summary for a flow whose usage is genuinely unknown."""
    client, app = client_and_app
    daemon, sock = _connect(client, app)
    try:
        _seed_bundle(client, sock, _blocks(4), cursor=_cursor(4))
        app.state.server_state._history_data.pop(FLOW, None)

        import threading

        result = {}

        def _ask():
            result["resp"] = client.get(
                "/api/history/%s" % FLOW, params={"window": 2}
            )

        worker = threading.Thread(target=_ask)
        worker.start()
        try:
            for _ in range(20):
                msg = protocol.decode(sock.receive_text())
                if msg.type == protocol.MSG_HISTORY_WINDOW_REQUEST:
                    break
            else:  # pragma: no cover - defensive
                raise AssertionError("no HISTORY_WINDOW_REQUEST relayed")
            steps = _step_ids(4)
            sock.send_text(
                protocol.make_history_window_data(
                    FLOW,
                    request_id=msg.payload["request_id"],
                    records=[r for r in _blocks(4) if r["step_id"] in steps[-2:]],
                    steps=steps, window=steps[-2:], counts=_cursor(4),
                ).to_json()
            )
        finally:
            worker.join(timeout=30)
        assert result["resp"].json()["usage"] is None
    finally:
        daemon.__exit__(None, None, None)


def test_a_daemon_window_read_is_chunked_and_settles_on_the_final_frame(
    client_and_app,
):
    client, app = client_and_app
    daemon, sock = _connect(client, app)
    try:
        _seed_bundle(client, sock, _blocks(6), cursor=_cursor(6))
        app.state.server_state._history_data.pop(FLOW, None)

        import threading

        result = {}

        def _ask():
            result["resp"] = client.get(
                "/api/history/%s" % FLOW, params={"window": 2}
            )

        worker = threading.Thread(target=_ask)
        worker.start()
        try:
            for _ in range(20):
                msg = protocol.decode(sock.receive_text())
                if msg.type == protocol.MSG_HISTORY_WINDOW_REQUEST:
                    break
            else:  # pragma: no cover - defensive
                raise AssertionError("no HISTORY_WINDOW_REQUEST relayed")
            rid = msg.payload["request_id"]
            steps = _step_ids(6)
            wanted = [r for r in _blocks(6) if r["step_id"] in steps[-2:]]
            sock.send_text(
                protocol.make_history_window_data(
                    FLOW, request_id=rid, records=wanted[:2], steps=steps,
                    window=steps[-2:], counts=_cursor(6), final=False,
                ).to_json()
            )
            sock.send_text(
                protocol.make_history_window_data(
                    FLOW, request_id=rid, records=wanted[2:], steps=steps,
                    window=steps[-2:], counts=_cursor(6), final=True,
                ).to_json()
            )
        finally:
            worker.join(timeout=30)
        body = result["resp"].json()
        # Both chunks arrived under one request and only the `final` one settled
        # it, so a window too big for one frame is delivered whole.
        assert len(body["records"]) == len(wanted)
    finally:
        daemon.__exit__(None, None, None)


def test_a_legacy_daemon_falls_back_to_the_full_pull_and_still_windows(
    client_and_app,
):
    """Compatibility: revision 8 knows no window request — degrade, don't hang."""
    client, app = client_and_app
    daemon, sock = _connect(client, app, protocol_version="8")
    try:
        _seed_bundle(client, sock, _blocks(8), cursor=_cursor(8))
        app.state.server_state._history_data.pop(FLOW, None)
        # The full-pull throttle would otherwise skip the fallback pull entirely.
        app.state.server_state._history_full_pull_at.pop(FLOW, None)

        import threading

        result = {}

        def _ask():
            result["resp"] = client.get(
                "/api/history/%s" % FLOW, params={"window": 2}
            )

        worker = threading.Thread(target=_ask)
        worker.start()
        try:
            for _ in range(20):
                msg = protocol.decode(sock.receive_text())
                if msg.type == protocol.MSG_HISTORY_WINDOW_REQUEST:
                    raise AssertionError(
                        "a revision-8 daemon was sent a window request it "
                        "would silently drop"
                    )
                if msg.type == protocol.MSG_HISTORY_REQUEST:
                    break
            else:  # pragma: no cover - defensive
                raise AssertionError("no HISTORY_REQUEST relayed")
            sock.send_text(
                protocol.make_history_data(
                    FLOW, protocol.HISTORY_MODE_FULL, _blocks(8),
                    cursor=_cursor(8),
                ).to_json()
            )
        finally:
            worker.join(timeout=30)
        body = result["resp"].json()
        assert result["resp"].status_code == 200, result["resp"].text
        # Available, just not memory-frugal: the fallback pull rebuilt the bundle
        # and the route sliced the requested window out of it.
        assert body["delivery"] == "window"
        assert body["window"]["loaded"] == _step_ids(8)[-2:]
        assert body["window"]["has_earlier"] is True
    finally:
        daemon.__exit__(None, None, None)


def test_the_fallback_pull_pins_the_flow_against_eviction(client_and_app):
    """The fallback must not re-introduce the evict-mid-drain half of the loop."""
    client, app = client_and_app
    state = app.state.server_state
    seen = {"pinned": False}
    original = state.pin_history_pull

    async def _spy(flow_id):
        if flow_id == FLOW:
            seen["pinned"] = True
        await original(flow_id)

    state.pin_history_pull = _spy
    daemon, sock = _connect(client, app, protocol_version="8")
    try:
        _seed_bundle(client, sock, _blocks(4), cursor=_cursor(4))
        state._history_data.pop(FLOW, None)
        state._history_full_pull_at.pop(FLOW, None)

        import threading

        result = {}

        def _ask():
            result["resp"] = client.get(
                "/api/history/%s" % FLOW, params={"window": 2}
            )

        worker = threading.Thread(target=_ask)
        worker.start()
        try:
            for _ in range(20):
                msg = protocol.decode(sock.receive_text())
                if msg.type == protocol.MSG_HISTORY_REQUEST:
                    break
            sock.send_text(
                protocol.make_history_data(
                    FLOW, protocol.HISTORY_MODE_FULL, _blocks(4),
                    cursor=_cursor(4),
                ).to_json()
            )
        finally:
            worker.join(timeout=30)
        assert seen["pinned"], "the fallback pull left its bundle evictable"
        # …and released again once the pull settled.
        assert FLOW not in state._history_pull_pinned
    finally:
        state.pin_history_pull = original
        daemon.__exit__(None, None, None)


def test_a_windowed_request_for_an_unowned_flow_is_404(client_and_app):
    client, app = client_and_app
    resp = client.get("/api/history/nobody-owns-this", params={"window": 2})
    assert resp.status_code == 404


def test_the_windowed_records_are_summarized_like_every_other_delivery(
    client_and_app,
):
    """Window replies leave through the same shaping funnel as a full snapshot."""
    from tianluo.server.history_summary import STEP_INPUTS_LAZY_KEY

    client, app = client_and_app
    daemon, sock = _connect(client, app)
    try:
        step = "00_self_check_aa"
        big = {
            "step_id": step,
            "step_type": "self_check",
            "ordinal": 0,
            "message": {
                "type": "step_completed",
                "step_id": step,
                "data": {
                    "step": {
                        "step_id": step,
                        "step_type": "self_check",
                        "status": "completed",
                        "inputs": {"scope_diff": "d" * 40000},
                        "outputs": {"issues": [], "actionable_count": 0},
                    }
                },
            },
        }
        _seed_bundle(client, sock, [big], cursor={"%s.jsonl" % step: 1})
        body = client.get("/api/history/%s" % FLOW, params={"window": 5}).json()
        shipped = body["records"][0]["message"]
        assert shipped[STEP_INPUTS_LAZY_KEY] is True
        assert "scope_diff" not in shipped["data"]["step"]["inputs"]
        assert shipped["data"]["step"]["outputs"] == {
            "issues": [], "actionable_count": 0
        }
    finally:
        daemon.__exit__(None, None, None)


def test_a_raw_chip_on_an_uncached_flow_reads_one_block_not_the_whole_flow(
    client_and_app,
):
    """Requirement: 查看原始 must still fetch on demand — on ANY size of flow.

    With windowing the huge flow that motivated this typically has NO bundle, so
    a detail lookup that could only re-pull the whole flow would time out and
    paint the chip permanently unavailable. It reads the addressed record's own
    step block instead.
    """
    client, app = client_and_app
    daemon, sock = _connect(client, app)
    try:
        step = "03_implement_h03"
        record = {
            "step_id": step,
            "step_type": "implement",
            "ordinal": 0,
            "message": {
                "type": "step_completed",
                "step_id": step,
                "data": {
                    "step": {
                        "step_id": step,
                        "step_type": "implement",
                        "status": "completed",
                        "inputs": {"scope_diff": "d" * 40000},
                        "outputs": {},
                    }
                },
            },
        }
        _seed_bundle(client, sock, [record], cursor={"%s.jsonl" % step: 1})
        app.state.server_state._history_data.pop(FLOW, None)

        import threading

        result = {}

        def _ask():
            result["resp"] = client.get(
                "/api/history/%s/detail" % FLOW,
                params={"step_id": step, "ordinal": 0, "source": "step"},
            )

        worker = threading.Thread(target=_ask)
        worker.start()
        try:
            for _ in range(20):
                msg = protocol.decode(sock.receive_text())
                if msg.type == protocol.MSG_HISTORY_WINDOW_REQUEST:
                    break
                assert msg.type != protocol.MSG_HISTORY_REQUEST, (
                    "the detail lookup pulled the WHOLE flow for one record"
                )
            else:  # pragma: no cover - defensive
                raise AssertionError("no HISTORY_WINDOW_REQUEST relayed")
            assert msg.payload["steps"] == [step]
            sock.send_text(
                protocol.make_history_window_data(
                    FLOW, request_id=msg.payload["request_id"],
                    records=[record], steps=[step], window=[step],
                    counts={"%s.jsonl" % step: 1},
                ).to_json()
            )
        finally:
            worker.join(timeout=30)
        assert result["resp"].status_code == 200, result["resp"].text
        body = result["resp"].json()
        # The held-back payload comes back whole.
        assert body["source"] == "step"
        assert body["inputs"]["scope_diff"] == "d" * 40000
        assert (
            body["record"]["data"]["step"]["inputs"]["scope_diff"] == "d" * 40000
        )
    finally:
        daemon.__exit__(None, None, None)


# ==========================================================================
# the window registry
# ==========================================================================


def test_the_window_registry_accumulates_until_the_final_frame():
    from tianluo.server.ws import HistoryWindowRegistry

    async def scenario():
        reg = HistoryWindowRegistry()
        fut = reg.begin("r1")
        reg.accumulate("r1", {
            "ok": True, "final": False, "records": [1, 2],
            "steps": ["a", "b"], "window": ["b"], "counts": {"b.jsonl": 2},
        })
        assert not fut.done(), "a non-final chunk settled the request"
        reg.accumulate("r1", {
            "ok": True, "final": True, "records": [3],
            "steps": ["a", "b"], "window": ["b"], "counts": {"b.jsonl": 2},
        })
        out = await fut
        assert out["records"] == [1, 2, 3]
        assert out["steps"] == ["a", "b"] and out["counts"] == {"b.jsonl": 2}

    asyncio.run(scenario())


def test_the_window_registry_reports_a_failed_read():
    from tianluo.server.ws import HistoryWindowRegistry

    async def scenario():
        reg = HistoryWindowRegistry()
        fut = reg.begin("r1")
        reg.accumulate("r1", {
            "ok": False, "final": True, "error": "disk on fire", "records": [],
        })
        out = await fut
        assert out["ok"] is False and out["error"] == "disk on fire"

    asyncio.run(scenario())


def test_a_frame_for_a_discarded_request_is_dropped():
    from tianluo.server.ws import HistoryWindowRegistry

    async def scenario():
        reg = HistoryWindowRegistry()
        reg.begin("r1")
        reg.discard("r1")
        # Residue of a waiter that timed out; re-creating its accumulator here
        # would be a slow leak keyed by a request nobody is waiting on.
        reg.accumulate("r1", {"ok": True, "final": True, "records": [1]})
        assert reg._chunks == {} and reg._waiters == {}

    asyncio.run(scenario())


# ==========================================================================
# the conditional (unchanged-window) poll
# ==========================================================================
#
# A relayed window builds no bundle, so the browser holds no progress token for
# it and its 3 s self-heal poll can only re-ask for the tail. Unconditionally
# that re-read + re-shape + re-gzip is the flow's whole tail window on EVERY
# tick, for as long as it is watched — the follow cost this leg must not have.
# The window therefore carries a daemon-minted `signature` the client echoes as
# `wsig`, and the server binds it STATELESSLY: it stores nothing, it relays the
# probe to the daemon that minted it.


def _relay_window(client, sock, params, *, reply):
    """Issue a windowed GET, answer the relayed request, return (msg, response).

    *reply* receives the observed request payload and returns the kwargs for the
    ``MSG_HISTORY_WINDOW_DATA`` frame the daemon sends back.
    """
    import threading

    result = {}

    def _ask():
        result["resp"] = client.get("/api/history/%s" % FLOW, params=params)

    worker = threading.Thread(target=_ask)
    worker.start()
    try:
        for _ in range(20):
            msg = protocol.decode(sock.receive_text())
            if msg.type == protocol.MSG_HISTORY_WINDOW_REQUEST:
                break
        else:  # pragma: no cover - defensive
            raise AssertionError("no HISTORY_WINDOW_REQUEST relayed")
        sock.send_text(
            protocol.make_history_window_data(
                FLOW, request_id=msg.payload["request_id"],
                **reply(msg.payload),
            ).to_json()
        )
    finally:
        worker.join(timeout=30)
    return msg, result["resp"]


def test_a_relayed_window_carries_the_probe_for_the_next_poll(client_and_app):
    client, app = client_and_app
    daemon, sock = _connect(client, app)
    try:
        _seed_bundle(client, sock, _blocks(8), cursor=_cursor(8))
        app.state.server_state._history_data.pop(FLOW, None)
        steps = _step_ids(8)
        msg, resp = _relay_window(
            client, sock, {"window": 2},
            reply=lambda p: dict(
                records=[r for r in _blocks(8) if r["step_id"] in steps[-2:]],
                steps=steps, window=steps[-2:], counts=_cursor(8),
                signature="sig-1",
            ),
        )
        # A first open sends no probe — there is nothing yet to be conditional on.
        assert "if_signature" not in msg.payload
        body = resp.json()
        # No bundle token (there is no bundle), but the window itself carries the
        # probe the client's next tail poll presents.
        assert body["progress"] is None and body["signature"] is None
        assert body["window"]["signature"] == "sig-1"
    finally:
        daemon.__exit__(None, None, None)


def test_the_probe_is_relayed_and_an_unchanged_tail_ships_nothing(client_and_app):
    client, app = client_and_app
    daemon, sock = _connect(client, app)
    try:
        _seed_bundle(client, sock, _blocks(8), cursor=_cursor(8))
        app.state.server_state._history_data.pop(FLOW, None)
        msg, resp = _relay_window(
            client, sock, {"window": 2, "wsig": "sig-1"},
            reply=lambda p: dict(signature="sig-1", not_modified=True),
        )
        assert msg.payload["if_signature"] == "sig-1"
        body = resp.json()
        assert resp.status_code == 200, resp.text
        assert body["delivery"] == "not_modified"
        # Not one record, and no window block — the browser keeps the window and
        # the block index it already holds (an absent block is inert to the
        # client's `adoptWindowMeta`).
        assert body["records"] == []
        assert "window" not in body
        assert "cursor" not in body
        # …and it still declares itself settled, so a windowed view is never
        # handed the interrupted-delivery repair loop of a bundle that does not
        # exist.
        assert body["incomplete"] is False
        assert body["progress"] is None and body["signature"] is None
        # The steady state must remain stateless: no bundle was created by the
        # poll, and none is needed to answer the next one.
        assert FLOW not in app.state.server_state._history_data
    finally:
        daemon.__exit__(None, None, None)


def test_a_stale_probe_is_answered_with_the_window(client_and_app):
    client, app = client_and_app
    daemon, sock = _connect(client, app)
    try:
        _seed_bundle(client, sock, _blocks(8), cursor=_cursor(8))
        app.state.server_state._history_data.pop(FLOW, None)
        steps = _step_ids(8)
        _msg, resp = _relay_window(
            client, sock, {"window": 2, "wsig": "sig-old"},
            reply=lambda p: dict(
                records=[r for r in _blocks(8) if r["step_id"] in steps[-2:]],
                steps=steps, window=steps[-2:], counts=_cursor(8),
                signature="sig-2",
            ),
        )
        body = resp.json()
        assert body["delivery"] == "window"
        assert len(body["records"]) == 6
        assert body["window"]["signature"] == "sig-2"
    finally:
        daemon.__exit__(None, None, None)


def test_a_daemon_that_ignores_the_probe_keeps_the_old_behaviour(client_and_app):
    """Requirement 6: an older peer must degrade, never wedge."""
    client, app = client_and_app
    daemon, sock = _connect(client, app)
    try:
        _seed_bundle(client, sock, _blocks(8), cursor=_cursor(8))
        app.state.server_state._history_data.pop(FLOW, None)
        steps = _step_ids(8)
        _msg, resp = _relay_window(
            client, sock, {"window": 2, "wsig": "sig-1"},
            reply=lambda p: dict(
                records=[r for r in _blocks(8) if r["step_id"] in steps[-2:]],
                steps=steps, window=steps[-2:], counts=_cursor(8),
            ),
        )
        body = resp.json()
        # No `signature` in the reply → the window carries none → the client
        # simply polls unconditionally, exactly as it did before the probe
        # existed. Nothing loops and nothing is refused.
        assert body["delivery"] == "window"
        assert body["window"]["signature"] == ""
        assert len(body["records"]) == 6
    finally:
        daemon.__exit__(None, None, None)


def test_the_cached_leg_ignores_the_probe(client_and_app):
    client, app = client_and_app
    daemon, sock = _connect(client, app)
    try:
        _seed_bundle(client, sock, _blocks(8), cursor=_cursor(8))
        body = client.get(
            "/api/history/%s" % FLOW, params={"window": 3, "wsig": "sig-1"}
        ).json()
        # The probe describes the DAEMON's files; the cached leg answers from a
        # bundle and hands back a real progress token, which is what the client
        # polls with from then on.
        assert body["delivery"] == "window"
        assert body["progress"] and body["signature"]
        assert len(body["records"]) == 9
    finally:
        daemon.__exit__(None, None, None)


def test_the_window_registry_carries_the_probe_and_the_verdict():
    from tianluo.server.ws import HistoryWindowRegistry

    async def scenario():
        reg = HistoryWindowRegistry()
        fut = reg.begin("r1")
        reg.accumulate("r1", {
            "ok": True, "final": True, "records": [], "steps": [],
            "window": [], "counts": {}, "signature": "sig-1",
            "not_modified": True,
        })
        out = await fut
        assert out["not_modified"] is True and out["signature"] == "sig-1"

    asyncio.run(scenario())


def test_the_registry_defaults_say_a_reply_is_a_real_window():
    from tianluo.server.ws import HistoryWindowRegistry

    async def scenario():
        reg = HistoryWindowRegistry()
        fut = reg.begin("r1")
        # A daemon that predates the probe sends neither field; reading its reply
        # as "unchanged" would hand the browser an empty conversation.
        reg.accumulate("r1", {
            "ok": True, "final": True, "records": [1], "steps": ["a"],
            "window": ["a"], "counts": {"a.jsonl": 1},
        })
        out = await fut
        assert out["not_modified"] is False and out["signature"] == ""

    asyncio.run(scenario())
