"""Keeping big-bundle work off the tianluo-server event loop.

This is the *latency* half of the OOM/stutter work (the *memory* half lives in
``test_history_cache_budget.py``). A multi-MB history bundle is touched by
several paths that used to run whole on the asyncio loop, and every one of them
froze daemon heartbeats, browser polls and inbound frames for as long as it ran.
``scripts/measure_server_loop_stalls.py`` reproduces the numbers; on a 16 MiB /
4000-record bundle they rank:

* the usage re-aggregation inside ``ServerState._lock`` — ~235-350 ms, and it
  re-ran on EVERY ~3 s poll of a flow whose daemon sent no usage payload;
* the REST response's gzip — ~134 ms per full reply;
* the REST response's ``json.dumps`` — ~68 ms per full reply;
* the ``/ws/ui`` fan-out's ``json.dumps`` — ~54 ms, paid ONCE PER CLIENT.

The mechanism differs per item, and NOT interchangeably — which is what these
tests pin. ``zlib`` releases the GIL, so gzip really does leave the loop when it
is moved to a worker thread (measured: ~144 ms → ~1 ms of loop lateness). The C
JSON encoder does NOT, so a thread hop leaves the loop blocked on the GIL for
almost the whole render (~77 ms → ~99 ms) — worth nothing and costing a
round-trip. JSON is therefore rendered in record batches that ``await`` between
them (~77 ms → ~15 ms), on the loop. The inbound ``protocol.decode`` has the same
GIL property and cannot be batched at all without replacing the C scanner, so it
stays inline; what bounds it is the daemon's own byte chunking
(``daemon.history.MAX_BYTES_PER_REPORT``, 256 KiB ⇒ a sub-millisecond parse),
with ``ws.LARGE_FRAME_WARN_BYTES`` as the tripwire if that ever regresses.

Everything else that runs under the lock (the pending window, the ordinal index,
``_unnumbered_steps``, the record-list copy) measures 0-2 ms on the same bundle
and is deliberately left where it is — the atomicity of the snapshot is worth
more than two milliseconds.

The tests below assert the STRUCTURE of the fix rather than its timing: which
thread each half ran on, whether the loop stayed free while it ran (a rendezvous
that deadlocks if it did not), how many times a payload was serialized, and that
the batched and inline paths produce byte-identical output. The one deliberate
exception is the concurrency gate, where "no fifth render started" can only be
observed by looking.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import threading

import pytest

from tianluo.daemon import history as daemon_history
from tianluo.daemon import protocol
from tianluo.server import app as app_module
from tianluo.server import ws as ws_module
from tianluo.server.state import ServerState
from tianluo.server.ws import UiHub, _handle_message

MACHINE = "m-offload"
OWNER = "owner-offload"

#: A model the built-in price table does NOT know, so a rebuild priced with the
#: built-in fallback reports an unknown estimate and one priced with the
#: project's catalog (below) reports a real number — which is how the memo's
#: catalog invalidation becomes observable at all.
USAGE_MODEL = "tianluo-test-model"
USAGE_CATALOG = {
    "version": "test",
    "entries": {
        USAGE_MODEL: {
            "model": USAGE_MODEL,
            "uncached_input": 100.0,
            "output": 200.0,
            "cache_read": 1.0,
            "cache_creation": 1.0,
        }
    },
}


def _record(step: str, ordinal: int, chars: int = 400, usage: bool = False) -> dict:
    message = {"role": "assistant", "content": "x" * chars}
    if usage:
        message["usage_records"] = [
            {
                "call_id": f"{step}-{ordinal}",
                "usage_status": "complete",
                "reported_model": USAGE_MODEL,
                "resolved_model": USAGE_MODEL,
                "resolved_model_source": "reported",
                "logical_input_tokens": 1000,
                "uncached_input_tokens": 1000,
                "output_tokens": 2000,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            }
        ]
    return {
        "step_id": step,
        "step_type": "discovery",
        "ordinal": ordinal,
        "message": message,
    }


def _records(step: str, count: int, start: int = 0, **kw) -> list:
    return [_record(step, i, **kw) for i in range(start, start + count)]


def _big_payload(count: int = 400) -> dict:
    """A payload past ``HISTORY_RESPONSE_OFFLOAD_RECORDS`` (the offload gate)."""
    return {"flow_id": "f", "delivery": "full", "records": _records("plan", count)}


def _small_payload() -> dict:
    return {"flow_id": "f", "delivery": "delta", "records": _records("plan", 3)}


class _Req:
    """The only thing ``_history_response`` reads off a Request."""

    def __init__(self, accept_encoding: str = "gzip") -> None:
        self.headers = {"accept-encoding": accept_encoding}


# ---------------------------------------------------------------------------
# REST render: which thread does it run on?
# ---------------------------------------------------------------------------


def _thread_spy(monkeypatch, module, name):
    """Replace *module.name* with a wrapper recording its calling thread."""
    original = getattr(module, name)
    seen: list = []

    def spy(*args, **kwargs):
        seen.append(threading.get_ident())
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, spy)
    return seen


def test_big_history_response_gzips_off_the_event_loop(monkeypatch):
    """The half that CAN leave the loop (gzip releases the GIL) must leave it."""
    seen = _thread_spy(monkeypatch, app_module, "_gzip_history_body")

    async def scenario():
        await app_module._history_response(_big_payload(), _Req("gzip"))
        return threading.get_ident()

    loop_thread = asyncio.run(scenario())
    assert seen and loop_thread not in seen


def test_big_history_response_renders_json_in_batches_on_the_loop(monkeypatch):
    """The JSON half stays on the loop — but yields, which is the whole point.

    A worker thread would not free the loop (the C encoder holds the GIL), so the
    fix is to interleave the render with the loop instead of relocating it. The
    proof is that a coroutine scheduled BEFORE the render gets turns WHILE it
    runs: it could not if the render were one uninterrupted ``json.dumps``.
    """
    payload = _big_payload(count=app_module.HISTORY_RESPONSE_OFFLOAD_RECORDS * 3)
    ticks = {"n": 0}

    async def scenario():
        stop = asyncio.Event()

        async def ticker():
            while not stop.is_set():
                ticks["n"] += 1
                await asyncio.sleep(0)

        spinner = asyncio.ensure_future(ticker())
        await asyncio.sleep(0)
        before = ticks["n"]
        await app_module._history_response(payload, _Req("identity"))
        during = ticks["n"] - before
        stop.set()
        await spinner
        return during

    turns = asyncio.run(asyncio.wait_for(scenario(), timeout=30))
    # The batch size is adaptive (byte-budgeted), so the exact count is not
    # pinned — what must hold is that the render yielded repeatedly rather than
    # running as one uninterrupted pass.
    assert turns >= 2, f"loop got {turns} turns, expected >= 2"


def test_small_history_response_renders_in_one_pass(monkeypatch):
    """The idle-poll steady state must NOT pay machinery it cannot recoup."""
    seen = _thread_spy(monkeypatch, app_module, "dump_json_chunked")

    async def scenario():
        await app_module._history_response(_small_payload(), _Req())

    asyncio.run(scenario())
    assert seen == []


def test_small_history_response_renders_inline(monkeypatch):
    """The steady-state reply still goes through the plain one-pass render."""
    seen = _thread_spy(monkeypatch, app_module, "_render_history_body")

    async def scenario():
        await app_module._history_response(_small_payload(), _Req())
        return threading.get_ident()

    loop_thread = asyncio.run(scenario())
    assert seen == [loop_thread]


def test_loop_keeps_running_while_a_big_response_gzips(monkeypatch):
    """Decidable non-blocking proof: the gzip can only finish via the loop.

    The stubbed compression parks on an event that is set by a coroutine
    scheduled AFTER the response was started. If the compression ran on the event
    loop, that coroutine could never get a turn and this deadlocks; the
    ``wait_for`` turns the deadlock into a failure instead of a hung suite.
    """
    released = threading.Event()
    entered = threading.Event()

    def blocking_gzip(body):
        entered.set()
        assert released.wait(30), "gzip was never released"
        return b"gz"

    monkeypatch.setattr(app_module, "_gzip_history_body", blocking_gzip)

    async def scenario():
        render = asyncio.ensure_future(
            app_module._history_response(_big_payload(), _Req("gzip"))
        )

        async def releaser():
            # Only reachable if the loop is still turning while gzip runs.
            while not entered.is_set():
                await asyncio.sleep(0)
            released.set()

        await asyncio.gather(render, releaser())
        return render.result()

    response = asyncio.run(asyncio.wait_for(scenario(), timeout=30))
    assert response.body == b"gz"


def test_batched_and_inline_renders_are_byte_identical():
    """The batching must be a scheduling change only — never a content change."""
    payload = _big_payload()

    async def render_plain():
        return await app_module._history_response(payload, _Req("identity"))

    offloaded = asyncio.run(render_plain())

    # Raise the gate above this payload so the SAME payload takes the inline
    # branch, and compare what the two branches produced.
    original_gate = app_module.HISTORY_RESPONSE_OFFLOAD_RECORDS
    original_bytes = app_module.HISTORY_RESPONSE_OFFLOAD_BYTES
    app_module.HISTORY_RESPONSE_OFFLOAD_RECORDS = len(payload["records"]) + 1
    # Both gates have to be lifted now that either one can select the big path.
    app_module.HISTORY_RESPONSE_OFFLOAD_BYTES = 1 << 40
    try:
        inline = asyncio.run(render_plain())
    finally:
        app_module.HISTORY_RESPONSE_OFFLOAD_RECORDS = original_gate
        app_module.HISTORY_RESPONSE_OFFLOAD_BYTES = original_bytes

    assert "content-encoding" not in offloaded.headers
    assert "content-encoding" not in inline.headers
    assert offloaded.body == inline.body
    assert json.loads(inline.body) == payload

    # …and the gzipped offload carries the very same document.
    async def render_gzipped():
        return await app_module._history_response(payload, _Req("gzip"))

    compressed = asyncio.run(render_gzipped())
    assert compressed.headers["content-encoding"] == "gzip"
    assert json.loads(gzip.decompress(compressed.body)) == payload


def test_big_render_declares_encoding_so_gzip_middleware_passes_through():
    """Compressing in the worker only helps if the middleware then stands down."""

    async def scenario():
        return await app_module._history_response(_big_payload(), _Req("gzip"))

    response = asyncio.run(scenario())
    assert response.headers["content-encoding"] == "gzip"
    assert response.headers["vary"] == "Accept-Encoding"
    assert response.media_type == "application/json"
    # Starlette's GZipMiddleware skips a response that already declares an
    # encoding, so this header is what prevents a second compression pass on
    # the event loop — the more expensive half of the stall.
    assert json.loads(gzip.decompress(response.body))["delivery"] == "full"


def test_big_bundle_shaping_runs_under_the_render_gate(monkeypatch):
    """Summary shaping is an O(bundle) pass, so it obeys the same gate.

    It used to run ahead of the ``big`` decision and outside the gate, so N
    concurrent opens of a big flow held N shaped copies before the gate had
    admitted even one — the transient-memory source the gate exists to bound.
    The size decision is deliberately taken on the UNSHAPED records: shaping only
    ever removes bytes, so the estimate can over-classify (batched render for a
    payload that ships small) but never under-classify.
    """
    # One permit, so ``Semaphore.locked()`` is a direct read of "the gate is
    # held" (a fresh event loop below gets a gate built from this value).
    monkeypatch.setattr(app_module, "HISTORY_RENDER_CONCURRENCY", 1)
    original = app_module.summarize_history_records
    seen = []

    def spy(records, flow_id):
        gate = app_module._history_render_gate()
        seen.append(gate.locked())
        return original(records, flow_id)

    monkeypatch.setattr(app_module, "summarize_history_records", spy)

    async def scenario():
        await app_module._history_response(_big_payload(), _Req("gzip"))
        await app_module._history_response(_small_payload(), _Req("gzip"))

    asyncio.run(scenario())
    # The big payload shaped with the gate held; the small one skipped it (the
    # gate would cost more than the render it protects).
    assert seen == [True, False]


def test_a_client_that_cannot_gzip_still_gets_plain_json():
    async def scenario():
        return await app_module._history_response(_big_payload(), _Req("identity"))

    response = asyncio.run(scenario())
    assert "content-encoding" not in response.headers
    assert json.loads(response.body)["delivery"] == "full"


def test_concurrent_big_renders_are_capped(monkeypatch):
    """The offload must not become an unbounded transient-memory source.

    Each in-flight render holds a second full copy of the bundle (its serialized
    bytes) with the state lock released, where the cache budget cannot see it.
    The gate is what keeps a burst of console reloads from doing by response
    what the budget stopped the cache from doing by growth.
    """
    limit = app_module.HISTORY_RENDER_CONCURRENCY
    guard = threading.Lock()
    census = {"in_flight": 0, "peak": 0}
    released = threading.Event()

    def blocking_gzip(body):
        with guard:
            census["in_flight"] += 1
            census["peak"] = max(census["peak"], census["in_flight"])
        assert released.wait(30), "render was never released"
        with guard:
            census["in_flight"] -= 1
        return b"gz"

    monkeypatch.setattr(app_module, "_gzip_history_body", blocking_gzip)

    async def scenario():
        gate = app_module._history_render_gate()
        payload = _big_payload()
        tasks = [
            asyncio.ensure_future(
                app_module._history_response(payload, _Req("gzip"))
            )
            for _ in range(limit + 2)
        ]
        while not gate.locked():
            await asyncio.sleep(0.005)
        # The gate is drained, so the extra requests are parked on it. Give any
        # render the gate wrongly admitted time to show up in the census.
        await asyncio.sleep(0.5)
        peak = census["peak"]
        released.set()
        await asyncio.gather(*tasks)
        return peak

    peak = asyncio.run(asyncio.wait_for(scenario(), timeout=60))
    assert peak == limit, f"{peak} renders ran at once, gate allows {limit}"


# ---------------------------------------------------------------------------
# Inbound frame decode
# ---------------------------------------------------------------------------


def test_daemon_frames_decode_inline(monkeypatch):
    """The parse stays on the loop: a thread hop cannot free it (GIL).

    What bounds the cost is the daemon's own byte chunking, asserted below.
    """
    frame = protocol.make_history_data(
        "f", protocol.HISTORY_MODE_FULL, _records("plan", 800), cursor={}
    ).to_json()
    seen = _thread_spy(monkeypatch, protocol, "decode")

    async def scenario():
        message = await ws_module._decode_frame(frame)
        assert message.type == protocol.MSG_HISTORY_DATA
        return threading.get_ident()

    loop_thread = asyncio.run(scenario())
    assert seen == [loop_thread]


def test_the_inbound_parse_is_bounded_at_the_daemon_side():
    """The tripwire must sit above what the daemon can actually emit.

    A warn threshold at or below the daemon's own chunk cap would fire on every
    ordinary history frame and mean nothing; one far above it is what makes an
    actual regression of that cap visible.
    """
    assert ws_module.LARGE_FRAME_WARN_BYTES > daemon_history.MAX_BYTES_PER_REPORT


def test_an_oversized_frame_is_still_applied_but_logged(caplog):
    """Dropping history the daemon already advanced its cursor past = a hole."""
    filler = "y" * ws_module.LARGE_FRAME_WARN_BYTES
    frame = protocol.make_history_data(
        "f",
        protocol.HISTORY_MODE_FULL,
        [{"step_id": "plan", "ordinal": 0, "message": {"content": filler}}],
        cursor={},
    ).to_json()
    assert len(frame) >= ws_module.LARGE_FRAME_WARN_BYTES

    async def scenario():
        return await ws_module._decode_frame(frame)

    with caplog.at_level("WARNING", logger="tianluo.server.ws"):
        message = asyncio.run(scenario())
    assert message.type == protocol.MSG_HISTORY_DATA
    assert any("exceeds the" in r.message for r in caplog.records)


def test_malformed_large_frame_still_raises_protocol_error():
    """The size tripwire must not swallow or reshape the decode failure."""
    raw = "{" + "x" * ws_module.LARGE_FRAME_WARN_BYTES

    async def scenario():
        with pytest.raises(protocol.ProtocolError):
            await ws_module._decode_frame(raw)

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# /ws/ui fan-out
# ---------------------------------------------------------------------------


class _UiWS:
    """Minimal UI socket stand-in recording the exact text it was sent."""

    def __init__(self) -> None:
        self.sent: list = []

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    def frames(self) -> list:
        return [json.loads(text) for text in self.sent]


def test_fan_out_serializes_one_payload_once_for_every_client(monkeypatch):
    """Every client of an owner gets the same bytes, rendered once.

    The fan-out used to ``json.dumps`` per client — byte-identical output at
    ~53 ms a piece for a 16 MiB ``history_data`` frame, so a second console
    doubled the stall.
    """
    calls: list = []
    original = ws_module._render_ui_frame

    def counting(payload):
        calls.append(payload.get("type"))
        return original(payload)

    monkeypatch.setattr(ws_module, "_render_ui_frame", counting)

    async def scenario():
        hub = UiHub()
        clients = [_UiWS() for _ in range(3)]
        for client in clients:
            await hub.register(client, OWNER)
        payload = {"type": "history_data", "flow_id": "f", "records": _records("p", 5)}
        await hub.broadcast_owned(payload, OWNER)
        return clients

    clients = asyncio.run(scenario())
    assert calls == ["history_data"]
    texts = {client.sent[0] for client in clients}
    assert len(texts) == 1
    assert all(len(client.sent) == 1 for client in clients)


def test_fan_out_renders_each_owners_payload_separately(monkeypatch):
    """De-duplication is by payload identity, so scoped frames stay distinct."""
    calls: list = []
    original = ws_module._render_ui_frame

    def counting(payload):
        calls.append(payload)
        return original(payload)

    monkeypatch.setattr(ws_module, "_render_ui_frame", counting)

    async def scenario():
        hub = UiHub()
        a, b = _UiWS(), _UiWS()
        await hub.register(a, "owner-a")
        await hub.register(b, "owner-b")
        await hub.broadcast_scoped(
            {
                "owner-a": {"type": "machines", "machines": ["a"]},
                "owner-b": {"type": "machines", "machines": ["b"]},
            }
        )
        return a, b

    a, b = asyncio.run(scenario())
    assert len(calls) == 2
    assert a.frames()[0]["machines"] == ["a"]
    assert b.frames()[0]["machines"] == ["b"]


def test_big_history_frame_is_rendered_in_batches_for_the_ui(monkeypatch):
    """A big fan-out frame must let the loop turn, and send the same bytes."""
    payload = {
        "type": "history_data",
        "flow_id": "f",
        "records": _records("p", ws_module.UI_FRAME_CHUNKED_RECORDS * 2),
    }
    one_shot = ws_module._render_ui_frame(payload)
    ticks = {"n": 0}

    async def scenario():
        hub = UiHub()
        client = _UiWS()
        await hub.register(client, OWNER)
        stop = asyncio.Event()

        async def ticker():
            while not stop.is_set():
                ticks["n"] += 1
                await asyncio.sleep(0)

        spinner = asyncio.ensure_future(ticker())
        await asyncio.sleep(0)
        before = ticks["n"]
        await hub.broadcast_owned(payload, OWNER)
        during = ticks["n"] - before
        stop.set()
        await spinner
        return during, client

    turns, client = asyncio.run(asyncio.wait_for(scenario(), timeout=30))
    assert turns >= 2, f"loop got {turns} turns, expected >= 2"
    assert client.sent == [one_shot]


def test_small_ui_frames_are_rendered_inline(monkeypatch):
    seen = _thread_spy(monkeypatch, ws_module, "_render_ui_frame")

    async def scenario():
        hub = UiHub()
        await hub.register(_UiWS(), OWNER)
        await hub.broadcast_owned({"type": "machines", "machines": []}, OWNER)
        return threading.get_ident()

    loop_thread = asyncio.run(scenario())
    assert seen == [loop_thread]


def test_fan_out_order_and_scoping_are_unchanged():
    """Non-regression: the same clients get the same frames in the same order."""

    async def scenario():
        state = ServerState()
        await state.register_machine(MACHINE, "host", "12.0.0", owner_id=OWNER)
        hub = UiHub()
        mine, other, admin = _UiWS(), _UiWS(), _UiWS()
        await hub.register(mine, OWNER)
        await hub.register(other, "owner-else")
        await hub.register(admin, None)
        for ordinal in range(3):
            await _handle_message(
                protocol.make_history_data(
                    "f",
                    protocol.HISTORY_MODE_APPEND
                    if ordinal
                    else protocol.HISTORY_MODE_FULL,
                    _records("plan", 1, start=ordinal),
                    cursor={"plan.jsonl": ordinal + 1},
                ),
                state,
                MACHINE,
                hub,
                None,
            )
        return mine, other, admin

    mine, other, admin = asyncio.run(scenario())
    for client in (mine, admin):
        frames = [f for f in client.frames() if f.get("type") == "history_data"]
        assert [f["records"][0]["ordinal"] for f in frames] == [0, 1, 2]
        assert frames[0]["mode"] == protocol.HISTORY_MODE_FULL
        # The cursor/signature/pending meta a client self-checks against still
        # rides every frame.
        assert frames[-1]["cursor"] == {"plan.jsonl": 3}
        assert "signature" in frames[-1] and "pending" in frames[-1]
    assert other.frames() == []


# ---------------------------------------------------------------------------
# Lock-held work: the usage rebuild
# ---------------------------------------------------------------------------


async def _full(state, flow, records, *, machine=MACHINE, **kw):
    return await state.apply_history_frame(
        flow, protocol.HISTORY_MODE_FULL, records, machine_id=machine, **kw
    )


async def _append(state, flow, records, *, machine=MACHINE, **kw):
    return await state.apply_history_frame(
        flow, protocol.HISTORY_MODE_APPEND, records, machine_id=machine, **kw
    )


def _count_usage_extractions(monkeypatch):
    calls: list = []
    original = ServerState._usage_sources_from_records

    def counting(records):
        calls.append(len(records))
        return original(records)

    monkeypatch.setattr(
        ServerState, "_usage_sources_from_records", staticmethod(counting)
    )
    return calls


def test_usage_rebuild_is_memoized_across_repeated_snapshot_reads(monkeypatch):
    """The largest measured under-lock cost must be paid once, not per poll."""
    calls = _count_usage_extractions(monkeypatch)

    async def scenario():
        state = ServerState()
        await state.register_machine(MACHINE, "host", "12.0.0", owner_id=OWNER)
        await _full(state, "f", _records("plan", 30, usage=True))
        payloads = []
        for _ in range(5):
            snapshot = await state.get_history_snapshot("f")
            payloads.append(snapshot["usage"])
        return payloads

    payloads = asyncio.run(scenario())
    assert len(calls) == 1, f"usage was re-aggregated {len(calls)} times"
    assert payloads[0] is not None
    assert all(p == payloads[0] for p in payloads)


def test_usage_memo_also_covers_a_bundle_that_carries_no_usage(monkeypatch):
    """"This bundle has no usage" costs the same full walk to establish."""
    calls = _count_usage_extractions(monkeypatch)

    async def scenario():
        state = ServerState()
        await state.register_machine(MACHINE, "host", "12.0.0", owner_id=OWNER)
        await _full(state, "f", _records("plan", 30))
        return [
            (await state.get_history_snapshot("f"))["usage"] for _ in range(4)
        ]

    payloads = asyncio.run(scenario())
    assert len(calls) == 1
    assert payloads == [None, None, None, None]


def test_usage_memo_tracks_appends(monkeypatch):
    """A memo that outlived its records would under-report the flow's cost."""

    async def scenario():
        state = ServerState()
        await state.register_machine(MACHINE, "host", "12.0.0", owner_id=OWNER)
        await _full(state, "f", _records("plan", 5, usage=True),
                    cursor={"plan.jsonl": 5})
        before = (await state.get_history_snapshot("f"))["usage"]
        await _append(
            state, "f", _records("plan", 5, start=5, usage=True),
            cursor={"plan.jsonl": 10},
        )
        after = (await state.get_history_snapshot("f"))["usage"]
        return before, after

    before, after = asyncio.run(scenario())
    assert before is not None and after is not None
    assert (
        after["summary"]["totals"]["logical_input_tokens"]
        > before["summary"]["totals"]["logical_input_tokens"]
    )


def test_a_late_pricing_catalog_invalidates_the_usage_memo():
    """The memo is keyed on the record count, so the catalog must clear it.

    A re-pull may carry the project's pricing table after the records; serving
    a memoized summary priced with the built-in fallback would silently ignore
    the project's own overrides.
    """
    records = _records("plan", 6, usage=True)

    async def scenario():
        state = ServerState()
        await state.register_machine(MACHINE, "host", "12.0.0", owner_id=OWNER)
        await _full(state, "f", records)
        cheap = (await state.get_history_snapshot("f"))["usage"]
        # Same records, now with the project catalog attached.
        await _full(state, "f", records, usage_catalog=USAGE_CATALOG)
        expensive = (await state.get_history_snapshot("f"))["usage"]
        return cheap, expensive

    cheap, expensive = asyncio.run(scenario())
    assert cheap["summary"]["estimated_cost_usd"] is None
    assert expensive["summary"]["estimated_cost_usd"] > 0


def test_usage_memo_never_leaks_onto_the_wire():
    """The memo lives on the bundle dict; no getter may hand it to a client."""

    async def scenario():
        state = ServerState()
        await state.register_machine(MACHINE, "host", "12.0.0", owner_id=OWNER)
        await _full(state, "f", _records("plan", 5, usage=True),
                    cursor={"plan.jsonl": 5})
        snapshot = await state.get_history_snapshot("f")
        bundle = await state.get_history("f")
        meta = await state.get_history_bundle_meta("f")
        return snapshot, bundle, meta

    snapshot, bundle, meta = asyncio.run(scenario())
    for payload in (snapshot, bundle, meta):
        assert not [k for k in payload if k.startswith("_")], payload.keys()


# ---------------------------------------------------------------------------
# Offload x eviction: the two fixes must not tread on each other
# ---------------------------------------------------------------------------


def _evictable(monkeypatch):
    """Make every flow eligible for eviction the instant it stops being read.

    ``_enforce_history_budget`` refuses to evict a bundle a UI client read
    within the hot window, and taking a snapshot IS such a read — so a test that
    wants to observe an eviction of the flow it just snapshotted has to collapse
    that window rather than sleep out the real 30 s.
    """
    monkeypatch.setattr(ServerState, "_HISTORY_VIEW_HOT_WINDOW", 0.0)


def test_a_snapshot_taken_before_an_eviction_still_renders_whole(monkeypatch):
    """A bundle evicted mid-render must not produce a half snapshot.

    The snapshot is detached from the cache under ``ServerState._lock`` (its
    ``records`` list holds its own strong references), so the eviction and the
    in-flight response simply stop sharing. The render happens afterwards, off
    the loop, and still sees every record.
    """
    _evictable(monkeypatch)

    async def scenario():
        state = ServerState(history_cache_budget_bytes=1)
        await state.register_machine(MACHINE, "host", "12.0.0", owner_id=OWNER)
        await _full(state, "f", _records("plan", 300))
        snapshot = await state.get_history_snapshot("f")
        assert snapshot["delivery"] == "full"
        # Now drop the very bundle that snapshot came from.
        await state.report_history_cache()
        assert await state.get_history_snapshot("f") is None
        response = await app_module._history_response(
            {"flow_id": "f", "cached": True, **snapshot}, _Req("identity")
        )
        return json.loads(response.body)

    body = asyncio.run(scenario())
    assert body["delivery"] == "full"
    assert len(body["records"]) == 300
    assert [r["ordinal"] for r in body["records"]] == list(range(300))


def test_eviction_running_during_an_offloaded_render_does_not_break_it(monkeypatch):
    """Concurrency, not sequence: evict WHILE the worker thread is gzipping."""
    _evictable(monkeypatch)
    started = threading.Event()
    proceed = threading.Event()
    original = app_module._gzip_history_body

    def gated_gzip(body):
        started.set()
        assert proceed.wait(30)
        return original(body)

    async def scenario():
        state = ServerState(history_cache_budget_bytes=1)
        await state.register_machine(MACHINE, "host", "12.0.0", owner_id=OWNER)
        await _full(state, "f", _records("plan", 300))
        snapshot = await state.get_history_snapshot("f")
        app_module._gzip_history_body = gated_gzip
        try:
            render = asyncio.ensure_future(
                app_module._history_response(
                    {"flow_id": "f", **snapshot}, _Req("gzip")
                )
            )
            while not started.is_set():
                await asyncio.sleep(0)
            # The loop is demonstrably free here — evict from under the render.
            await state.report_history_cache()
            stats = await state.history_cache_stats()
            proceed.set()
            response = await render
        finally:
            app_module._gzip_history_body = original
        return json.loads(gzip.decompress(response.body)), stats

    body, stats = asyncio.run(asyncio.wait_for(scenario(), timeout=60))
    assert stats["flows"] == 0 and stats["evictions"] == 1
    assert len(body["records"]) == 300


def test_evicted_flow_rebuilds_and_renders_through_the_offload_path(monkeypatch):
    """Eviction → cache miss → full re-source → offloaded render, end to end."""
    _evictable(monkeypatch)
    records = _records("plan", 300)

    async def scenario():
        state = ServerState(history_cache_budget_bytes=1)
        await state.register_machine(MACHINE, "host", "12.0.0", owner_id=OWNER)
        await _full(state, "f", records)
        await state.report_history_cache()
        assert await state.get_history_snapshot("f") is None
        # A UI read re-admitted the flow, so the回源 full pull can repopulate it.
        outcome = await _full(state, "f", records)
        assert outcome.resolves_pull
        snapshot = await state.get_history_snapshot("f")
        response = await app_module._history_response(
            {"flow_id": "f", "cached": False, **snapshot}, _Req("gzip")
        )
        return response

    response = asyncio.run(scenario())
    body = json.loads(gzip.decompress(response.body))
    assert body["delivery"] == "full"
    assert len(body["records"]) == 300


# ---------------------------------------------------------------------------
# End-to-end non-regression of the REST delivery state machine
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("accept_encoding", ["gzip", "identity"])
def test_delivery_semantics_survive_the_offload(accept_encoding):
    """full → delta → not_modified over a bundle big enough to be offloaded."""
    from fastapi.testclient import TestClient

    from _authsrv import authed_app, authed_hello, login

    headers = {"accept-encoding": accept_encoding}
    app, _key = authed_app()
    with TestClient(app) as client:
        login(client)
        with client.websocket_connect("/ws") as sock:
            sock.send_text(authed_hello(app, MACHINE, "host", "12.0.0"))
            protocol.decode(sock.receive_text())  # WELCOME
            sock.send_text(
                protocol.make_history_data(
                    "f",
                    protocol.HISTORY_MODE_FULL,
                    _records("plan", 300),
                    cursor={"plan.jsonl": 300},
                ).to_json()
            )
            body = None
            for _ in range(200):
                response = client.get("/api/history/f", headers=headers)
                if response.status_code == 200 and response.json().get("cached"):
                    body = response.json()
                    break
            assert body is not None, "bundle never became cache-visible"
            assert body["delivery"] == "full"
            assert len(body["records"]) == 300
            assert [r["ordinal"] for r in body["records"]] == list(range(300))

            # Echo the token + signature: provably in sync ⇒ not_modified.
            quiet = client.get(
                "/api/history/f",
                params={"after": body["progress"], "sig": body["signature"]},
                headers=headers,
            ).json()
            assert quiet["delivery"] == "not_modified"
            assert quiet["records"] == []
            assert quiet["resync"] is False

            # An append behind the same token ⇒ delta carrying only the tail.
            sock.send_text(
                protocol.make_history_data(
                    "f",
                    protocol.HISTORY_MODE_APPEND,
                    _records("plan", 2, start=300),
                    cursor={"plan.jsonl": 302},
                ).to_json()
            )
            delta = None
            for _ in range(200):
                delta = client.get(
                    "/api/history/f",
                    params={"after": body["progress"], "sig": body["signature"]},
                    headers=headers,
                ).json()
                if delta["delivery"] == "delta":
                    break
            assert delta["delivery"] == "delta"
            assert [r["ordinal"] for r in delta["records"]] == [300, 301]


# ---------------------------------------------------------------------------
# Size, not record count: the heavy-tail shape the gates must actually cover
# ---------------------------------------------------------------------------
#
# Sampling every record under this repo's own ``tianluo/history/`` (204,107 of
# them) gives mean 40.7 KB, p90 12.3 KB, p99 1.1 MB, max 161 MB. A real bundle
# here — ``02097bb3-73e/58f7f446.jsonl`` — is 10 records and 11.0 MiB. Any gate
# or batch bound expressed as a RECORD COUNT classifies that bundle as trivial
# and renders it in one uninterrupted pass, which is precisely the stall the
# batching exists to remove. The tests below pin the byte dimension.


#: One p99-shaped record: heavy enough that a handful of them exceed the batch
#: byte budget on their own.
_HEAVY_CHARS = 1_100_000


def _heavy_payload(count: int = 10) -> dict:
    """The real 10-record / ~11 MiB bundle shape, well under every count gate."""
    return {
        "flow_id": "f",
        "delivery": "full",
        "records": _records("plan", count, chars=_HEAVY_CHARS),
    }


def test_few_but_huge_records_still_yield_to_the_loop():
    """The batch bound is bytes: 10 records must not render in one frozen pass."""
    payload = _heavy_payload()
    assert len(payload["records"]) < ws_module.UI_FRAME_CHUNKED_RECORDS
    ticks = {"n": 0}

    async def scenario():
        stop = asyncio.Event()

        async def ticker():
            while not stop.is_set():
                ticks["n"] += 1
                await asyncio.sleep(0)

        spinner = asyncio.ensure_future(ticker())
        await asyncio.sleep(0)
        before = ticks["n"]
        body = await ws_module.dump_json_chunked(
            payload, **app_module._HISTORY_JSON_KWARGS
        )
        during = ticks["n"] - before
        stop.set()
        await spinner
        return during, body

    turns, body = asyncio.run(asyncio.wait_for(scenario(), timeout=60))
    # Every record is larger than the budget on its own, so the render can only
    # cut batches at one record — i.e. it yields once per record.
    assert turns >= len(payload["records"]), f"loop got only {turns} turns"
    assert body == json.dumps(payload, **app_module._HISTORY_JSON_KWARGS).encode(
        "utf-8"
    )


def test_batches_do_not_degenerate_to_one_record_for_small_records():
    """Adapting DOWN for heavy records must not cost a turn per light one."""
    payload = _big_payload(count=4000)

    async def scenario():
        turns = {"n": 0}
        stop = asyncio.Event()

        async def ticker():
            while not stop.is_set():
                turns["n"] += 1
                await asyncio.sleep(0)

        spinner = asyncio.ensure_future(ticker())
        await asyncio.sleep(0)
        before = turns["n"]
        body = await ws_module.dump_json_chunked(
            payload, **app_module._HISTORY_JSON_KWARGS
        )
        during = turns["n"] - before
        stop.set()
        await spinner
        return during, body

    turns, body = asyncio.run(asyncio.wait_for(scenario(), timeout=60))
    # ~700 B/record ⇒ ~750 records per 0.5 MiB batch, so a few batches plus the
    # ramp — nowhere near one per record.
    assert turns < 100, f"batching degenerated: {turns} yields for 4000 records"
    assert body == json.dumps(payload, **app_module._HISTORY_JSON_KWARGS).encode(
        "utf-8"
    )


def _batch_sizes(monkeypatch, payload) -> list:
    """Rendered byte size of every record batch ``dump_json_chunked`` cut."""
    sizes: list = []
    real_dumps = json.dumps

    class _Shim:
        @staticmethod
        def dumps(value, **kw):
            rendered = real_dumps(value, **kw)
            if isinstance(value, list):
                sizes.append(len(rendered))
            return rendered

    # Rebind the name in ``ws``'s own namespace only — never on the shared
    # ``json`` module, which every other test in this worker is also using.
    monkeypatch.setattr(ws_module, "json", _Shim)
    body = asyncio.run(
        ws_module.dump_json_chunked(payload, **app_module._HISTORY_JSON_KWARGS)
    )
    monkeypatch.undo()
    assert body == real_dumps(payload, **app_module._HISTORY_JSON_KWARGS).encode(
        "utf-8"
    )
    return sizes


@pytest.mark.parametrize("chars", [400, 200_000])
def test_batch_bytes_stay_within_the_budget(monkeypatch, chars):
    """Whatever a record weighs, a batch is cut to the BYTE budget.

    Both shapes go through the same adaptive sizing: the light one converges on
    a batch of many records, the heavy one on a batch of one or two. What the
    old record-count bound could not do is the second column — 128 records of
    this size is ~25 MiB in a single uninterrupted ``json.dumps``.
    """
    payload = {
        "flow_id": "f",
        "delivery": "full",
        "records": _records("plan", 40 if chars > 1000 else 4000, chars=chars),
    }
    sizes = _batch_sizes(monkeypatch, payload)
    assert sizes, "no record batch was rendered"
    # One record is the floor no predictor can go below, so the bound is the
    # budget plus one record's own rendered size.
    ceiling = ws_module.JSON_RENDER_BATCH_BYTES + 2 * chars + 512
    assert max(sizes) <= ceiling, f"a batch rendered {max(sizes)} bytes"


def test_multi_mb_response_under_the_record_gate_takes_the_big_path(monkeypatch):
    """A 10-record / 11 MiB reply must not be classified as a small reply."""
    payload = _heavy_payload()
    assert len(payload["records"]) < app_module.HISTORY_RESPONSE_OFFLOAD_RECORDS
    # ``app`` binds the helper into its own namespace at import, so the spy
    # must go there — patching ``ws`` would leave the call site untouched.
    chunked = _thread_spy(monkeypatch, app_module, "dump_json_chunked")
    inline = _thread_spy(monkeypatch, app_module, "_render_history_body")
    gzipped = _thread_spy(monkeypatch, app_module, "_gzip_history_body")

    async def scenario():
        response = await app_module._history_response(payload, _Req("gzip"))
        return response, threading.get_ident()

    response, loop_thread = asyncio.run(asyncio.wait_for(scenario(), timeout=60))
    assert chunked, "the multi-MB payload rendered inline"
    assert inline == [], "the small one-pass render ran on a multi-MB payload"
    # …and the gzip that the middleware would otherwise do ON THE LOOP is both
    # done here and done off the loop.
    assert gzipped and loop_thread not in gzipped
    assert response.headers["content-encoding"] == "gzip"
    assert json.loads(gzip.decompress(response.body)) == payload


def test_multi_mb_ui_frame_under_the_record_gate_is_batched(monkeypatch):
    """The same byte gate guards the ``/ws/ui`` relay of that bundle."""
    payload = dict(_heavy_payload(), type="history_data")
    assert len(payload["records"]) < ws_module.UI_FRAME_CHUNKED_RECORDS
    one_shot = ws_module._render_ui_frame(payload)
    seen = _thread_spy(monkeypatch, ws_module, "dump_json_chunked")

    async def scenario():
        hub = UiHub()
        client = _UiWS()
        await hub.register(client, OWNER)
        await hub.broadcast_owned(payload, OWNER)
        return client

    client = asyncio.run(asyncio.wait_for(scenario(), timeout=60))
    assert seen, "the multi-MB relay frame rendered in one pass"
    # INVARIANT: batching is a scheduling change, never a content change.
    assert client.sent == [one_shot]


def test_records_reach_bytes_stops_walking_at_the_threshold():
    """The gate's cost must not scale with the bundle it declines to measure."""
    from tianluo.server.state import records_reach_bytes

    walked: list = []

    class _Probe(list):
        def __iter__(self):
            for item in list.__iter__(self):
                walked.append(item)
                yield item

    records = _Probe(_records("plan", 500, chars=100_000))

    assert records_reach_bytes(records, 1024 * 1024) is True
    # ~100 KB/record ⇒ the 1 MiB line is crossed within a handful of records.
    assert len(walked) < 20, f"walked {len(walked)} records past the threshold"

    assert records_reach_bytes(_records("plan", 3), 1024 * 1024) is False
    assert records_reach_bytes("not-a-list", 10) is False


# ---------------------------------------------------------------------------
# The byte gate against non-ASCII payloads
# ---------------------------------------------------------------------------

#: Per-record CJK payload sized so 100 records serialize to ~1.8 MB — over the
#: 1 MiB byte gate, but under the 200-record count gate AND under the ~0.75 MB
#: a 1 B/char weight would have charged them, which is exactly the window where
#: the old estimate mis-classified a stalling reply as small. This project's
#: configured language is zh-CN, so this is the normal shape of its history
#: records, not an edge case.
_CJK_CHARS = 6_000


def _cjk_payload(count: int = 100) -> dict:
    records = [_record("plan", i, chars=1) for i in range(count)]
    for record in records:
        record["message"]["content"] = "田螺" * (_CJK_CHARS // 2)
    return {"flow_id": "f", "delivery": "full", "records": records}


def test_the_estimate_bounds_the_wire_and_resident_cost_of_cjk_records():
    """The gates' over-estimation guarantee must hold for non-ASCII text too."""
    from tianluo.server.state import _estimate_record_bytes

    records = _cjk_payload()["records"]
    estimated = sum(_estimate_record_bytes(record) for record in records)
    wire = len(json.dumps(records, ensure_ascii=False).encode("utf-8"))
    assert estimated >= wire, f"estimated {estimated} < {wire} wire bytes"
    # …and it still bounds what the bundle actually costs the container: PEP 393
    # stores BMP text at 2 B/char, which the old 1 B/char weight halved.
    assert estimated >= 2 * len(records) * _CJK_CHARS


def test_cjk_bundle_under_the_record_gate_takes_the_big_path(monkeypatch):
    """A ~2.7 MB CJK reply must not be rendered inline and gzipped on the loop."""
    payload = _cjk_payload()
    assert len(payload["records"]) < app_module.HISTORY_RESPONSE_OFFLOAD_RECORDS
    chunked = _thread_spy(monkeypatch, app_module, "dump_json_chunked")
    inline = _thread_spy(monkeypatch, app_module, "_render_history_body")
    gzipped = _thread_spy(monkeypatch, app_module, "_gzip_history_body")

    async def scenario():
        response = await app_module._history_response(payload, _Req("gzip"))
        return response, threading.get_ident()

    response, loop_thread = asyncio.run(asyncio.wait_for(scenario(), timeout=60))
    assert chunked, "the multi-MB CJK payload rendered inline"
    assert inline == [], "the small one-pass render ran on a multi-MB payload"
    assert gzipped and loop_thread not in gzipped
    assert response.headers["content-encoding"] == "gzip"
    assert json.loads(gzip.decompress(response.body)) == payload


def test_cjk_ui_frame_under_the_record_gate_is_batched(monkeypatch):
    """The same byte gate must classify the ``/ws/ui`` relay of that bundle."""
    payload = dict(_cjk_payload(), type="history_data")
    assert len(payload["records"]) < ws_module.UI_FRAME_CHUNKED_RECORDS
    one_shot = ws_module._render_ui_frame(payload)
    seen = _thread_spy(monkeypatch, ws_module, "dump_json_chunked")

    async def scenario():
        hub = UiHub()
        client = _UiWS()
        await hub.register(client, OWNER)
        await hub.broadcast_owned(payload, OWNER)
        return client

    client = asyncio.run(asyncio.wait_for(scenario(), timeout=60))
    assert seen, "the multi-MB CJK relay frame rendered in one pass"
    assert client.sent == [one_shot]
