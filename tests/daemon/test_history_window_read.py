"""The daemon's step-block window read and its protocol leg.

Background. A flow's history is delivered to the server as ONE whole-flow read
whose reply the server caches as a bundle. For a flow whose history is larger
than the server's entire history-cache budget that shape cannot terminate: the
bundle is evicted, the next browser page misses, the whole flow is pulled again,
and the reader never gets past the prefix they already had. The window read is
the shape that does terminate — ``count`` STEP BLOCKS, addressed by block id
rather than by byte cursor, read straight off disk and never cached.

Covered here:

* the tail window, the ``before_step`` page-up, walking back to the first block,
  and the explicit ``steps`` selection the on-demand detail lookup uses;
* the invariants a window read shares with ``read_flow`` — ``step_id#ordinal``
  identity, blank/unparseable lines occupying their physical line number, an
  unterminated tail left alone — because the two reads deliver the SAME records
  and a client reconciles them against each other;
* the block index and per-file counts that ride along, which are what let the
  browser page backwards and scope its completeness self-check without a round
  trip per hop;
* the protocol revision bump and its capability predicate, which is what lets
  the server tell "this daemon can serve a window" from "this daemon will
  silently ignore the frame".
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tianluo.daemon import protocol
from tianluo.daemon.history import DaemonHistoryReader


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _reader(*roots):
    return DaemonHistoryReader(project_roots_provider=lambda: [str(r) for r in roots])


def _flow_dir(root, flow_id):
    d = root / "tianluo" / "history" / flow_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_step(flow_dir, name, lines):
    path = flow_dir / name
    path.write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )
    return path


def _seed_flow(tmp_path, flow_id="f-window", blocks=6, per_block=3):
    """A flow with *blocks* step files, each holding *per_block* records."""
    root = tmp_path / "proj"
    flow_dir = _flow_dir(root, flow_id)
    names = []
    for i in range(blocks):
        name = "%02d_implement_h%02d.jsonl" % (i, i)
        _write_step(
            flow_dir,
            name,
            [
                {"role": "assistant", "content": "block %d line %d" % (i, j)}
                for j in range(per_block)
            ],
        )
        names.append(name)
    return root, flow_id, names


def _keys(records):
    return [(r["step_id"], r["ordinal"]) for r in records]


# --------------------------------------------------------------------------
# the reader
# --------------------------------------------------------------------------


def test_the_default_window_is_the_flow_tail(tmp_path):
    root, flow, _names = _seed_flow(tmp_path, blocks=6)
    win = _reader(root).read_flow_window(flow, project_root=str(root), count=2)

    # The LAST two blocks — the shape a WebUI open wants, so the reader lands on
    # the end of the conversation rather than on its beginning.
    assert win.window == ["04_implement_h04", "05_implement_h05"]
    assert win.first_index == 4
    assert _keys(win.records) == [
        ("04_implement_h04", 0), ("04_implement_h04", 1), ("04_implement_h04", 2),
        ("05_implement_h05", 0), ("05_implement_h05", 1), ("05_implement_h05", 2),
    ]


def test_the_whole_block_index_rides_along(tmp_path):
    root, flow, names = _seed_flow(tmp_path, blocks=6)
    win = _reader(root).read_flow_window(flow, project_root=str(root), count=2)

    # Every block of the flow, in flow order, not just the window's — one short
    # string each, and it is what lets the browser page backwards and bound its
    # completeness self-check without asking again.
    assert win.steps == [n[: -len(".jsonl")] for n in names]
    # …plus the cursor-shaped per-file line counts, the same statement a bundle's
    # `cursor` makes, so a windowed client can check the blocks it HAS loaded.
    assert win.counts == {name: 3 for name in names}


def test_paging_back_walks_to_the_very_first_block(tmp_path):
    root, flow, _names = _seed_flow(tmp_path, blocks=7)
    reader = _reader(root)

    seen = []
    win = reader.read_flow_window(flow, project_root=str(root), count=3)
    seen.append(list(win.window))
    while win.first_index > 0:
        anchor = win.steps[win.first_index]
        win = reader.read_flow_window(
            flow, project_root=str(root), count=3, before_step=anchor
        )
        assert win.window, "a page-up below the first block returned nothing"
        seen.append(list(win.window))

    flat = [step for page in seen for step in page]
    # Every block reached exactly once, and the last page ends at block 0 — the
    # acceptance condition for "browsable to its first step block".
    assert sorted(flat) == sorted(win.steps)
    assert len(flat) == len(win.steps)
    assert seen[-1][0] == "00_implement_h00"


def test_an_unknown_anchor_yields_an_empty_window_not_the_tail(tmp_path):
    root, flow, _names = _seed_flow(tmp_path, blocks=4)
    win = _reader(root).read_flow_window(
        flow, project_root=str(root), count=2, before_step="99_not_a_step"
    )
    # Degrading to the tail would answer a page-up with the page the reader
    # already holds, which reads to them as "the history just stops". An empty
    # window is visible and recoverable — and the block index still rides along.
    assert win.window == [] and win.records == []
    assert len(win.steps) == 4
    # An empty window anchors at the flow's END: 0 would say "and you have
    # reached the first block", which retires the consumer's page-up and lets it
    # treat blocks it never loaded as holes.
    assert win.first_index == 4


def test_explicit_step_selection_reads_exactly_those_blocks(tmp_path):
    root, flow, _names = _seed_flow(tmp_path, blocks=5)
    win = _reader(root).read_flow_window(
        flow,
        project_root=str(root),
        steps=["03_implement_h03", "01_implement_h01", "nope"],
    )
    # In FLOW order regardless of how they were named, unknown ids ignored: the
    # detail lookup asks for one block and must not be handed its neighbours.
    assert win.window == ["01_implement_h01", "03_implement_h03"]
    assert {r["step_id"] for r in win.records} == {
        "01_implement_h01", "03_implement_h03"
    }
    assert win.first_index == 1


def test_a_selection_matching_no_block_is_empty_but_not_at_the_head(tmp_path):
    root, flow, _names = _seed_flow(tmp_path, blocks=5)
    win = _reader(root).read_flow_window(
        flow, project_root=str(root), steps=["nope", "also-nope"],
    )
    assert win.window == [] and win.records == []
    assert win.first_index == 5


def test_a_window_larger_than_the_flow_is_the_whole_flow(tmp_path):
    root, flow, names = _seed_flow(tmp_path, blocks=3)
    win = _reader(root).read_flow_window(flow, project_root=str(root), count=99)
    assert win.first_index == 0
    assert len(win.window) == len(names)


def test_ordinals_are_physical_lines_shared_with_the_cursor_read(tmp_path):
    """A window record and a cursor record for the same line are one identity."""
    root = tmp_path / "proj"
    flow = "f-ident"
    flow_dir = _flow_dir(root, flow)
    path = flow_dir / "00_implement_aa.jsonl"
    path.write_text(
        json.dumps({"role": "assistant", "content": "one"}) + "\n"
        + "\n"                                    # blank: occupies line 1
        + "{not json\n"                           # unparseable: occupies line 2
        + json.dumps({"role": "assistant", "content": "two"}) + "\n"
        # No trailing newline, but a COMPLETE object: the cursor read consumes
        # such a tail (the writer finished the record, not the newline), so the
        # window read must agree or the same line would arrive twice under two
        # different ordinals.
        + '{"role": "assistant", "content": "tail"}',
        encoding="utf-8",
    )
    reader = _reader(root)
    win = reader.read_flow_window(flow, project_root=str(root), count=5)
    full = reader.read_flow(flow, project_root=str(root))

    assert _keys(win.records) == _keys(full.records)
    assert _keys(win.records) == [
        ("00_implement_aa", 0), ("00_implement_aa", 3), ("00_implement_aa", 4),
    ]
    # …and the per-file count is the same statement the bundle cursor makes, so
    # a windowed client can check the blocks it holds against it.
    key = "00_implement_aa.jsonl"
    assert win.counts[key] == full.cursor[key] == 5


def test_an_unresolvable_flow_is_an_empty_window_not_a_raise(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    win = _reader(root).read_flow_window("nope", project_root=str(root), count=3)
    assert win.steps == [] and win.records == [] and win.window == []


def test_the_window_read_does_not_disturb_the_cursor_read(tmp_path):
    """A window jumps BACKWARDS; it must not rewind the incremental drain."""
    root, flow, _names = _seed_flow(tmp_path, blocks=4, per_block=2)
    reader = _reader(root)
    first = reader.read_flow(flow, project_root=str(root))
    reader.read_flow_window(flow, project_root=str(root), count=1)
    reader.read_flow_window(
        flow, project_root=str(root), count=1,
        before_step=first.records[0]["step_id"],
    )
    again = reader.read_flow(flow, project_root=str(root), cursor=first.cursor)
    # Still caught up: the window reads shared no offset state with the drain.
    assert again.records == []
    assert again.cursor == first.cursor


# --------------------------------------------------------------------------
# the protocol leg
# --------------------------------------------------------------------------


def test_window_messages_are_registered_in_the_right_directions():
    assert protocol.MSG_HISTORY_WINDOW_REQUEST in protocol.SERVER_TO_DAEMON
    assert protocol.MSG_HISTORY_WINDOW_REQUEST not in protocol.DAEMON_TO_SERVER
    assert protocol.MSG_HISTORY_WINDOW_DATA in protocol.DAEMON_TO_SERVER
    assert protocol.MSG_HISTORY_WINDOW_DATA not in protocol.SERVER_TO_DAEMON


def test_the_capability_gate_refuses_every_older_revision():
    assert protocol.MIN_HISTORY_WINDOW_PROTOCOL_VERSION == 9
    assert protocol.supports_history_window("9") is True
    assert protocol.supports_history_window(9) is True
    assert protocol.supports_history_window(" 10 ") is True
    # An older daemon silently DROPS the unknown frame, so an ungated request
    # would park the browser on the pull timeout for every page-up.
    for bad in ("8", 8, "", None, "not-a-number", {}):
        assert protocol.supports_history_window(bad) is False


def test_window_request_round_trips():
    msg = protocol.make_history_window_request(
        "f1", request_id="r1", project_root="/p", count=4, before_step="03_x"
    )
    decoded = protocol.decode(msg.to_json())
    assert decoded.type == protocol.MSG_HISTORY_WINDOW_REQUEST
    assert decoded.payload == {
        "flow_id": "f1", "request_id": "r1", "count": 4,
        "project_root": "/p", "before_step": "03_x",
    }


def test_window_request_rejects_a_request_that_names_nothing():
    # A window request with no flow / no correlation id / a non-positive count
    # has no sensible degenerate reading, and answering one would hand the
    # browser an empty window it would read as "this flow has no history".
    with pytest.raises(protocol.ProtocolError):
        protocol.make_history_window_request("", request_id="r")
    with pytest.raises(protocol.ProtocolError):
        protocol.make_history_window_request("f", request_id="")
    with pytest.raises(protocol.ProtocolError):
        protocol.make_history_window_request("f", request_id="r", count=0)


def test_window_data_round_trips_with_its_description():
    msg = protocol.make_history_window_data(
        "f1", request_id="r1", records=[{"step_id": "a", "ordinal": 0}],
        steps=["a", "b"], window=["b"], counts={"b.jsonl": 2}, final=False,
    )
    decoded = protocol.decode(msg.to_json())
    assert decoded.payload["ok"] is True
    assert decoded.payload["final"] is False
    assert decoded.payload["steps"] == ["a", "b"]
    assert decoded.payload["counts"] == {"b.jsonl": 2}


# --------------------------------------------------------------------------
# the daemon handler
# --------------------------------------------------------------------------


class _Provider:
    """Minimal history provider standing in for a DaemonHistoryReader."""

    def __init__(self, window=None, raises=False):
        self._window = window
        self._raises = raises
        self.calls = []

    def read_flow_window(self, flow_id, **kwargs):
        self.calls.append((flow_id, kwargs))
        if self._raises:
            raise RuntimeError("disk on fire")
        return self._window


class _Sock:
    def __init__(self):
        self.sent = []

    async def send_text(self, raw):
        self.sent.append(protocol.decode(raw))


def _client(provider):
    from tianluo.daemon.client import DaemonClient

    client = DaemonClient.__new__(DaemonClient)
    client._history_provider = provider
    client._seq = 0
    client._next_seq = lambda: 0

    async def _send(ws, message):
        await ws.send_text(message.to_json())

    client._send = _send
    return client


def _run(coro):
    return asyncio.run(coro)


def test_the_handler_answers_with_the_window_and_its_description(tmp_path):
    root, flow, names = _seed_flow(tmp_path, blocks=4)
    provider = DaemonHistoryReader(project_roots_provider=lambda: [str(root)])
    client, sock = _client(provider), _Sock()
    _run(client._handle_history_window_request(sock, {
        "request_id": "r1", "flow_id": flow,
        "project_root": str(root), "count": 2,
    }))
    assert len(sock.sent) == 1
    payload = sock.sent[0].payload
    assert payload["ok"] is True and payload["final"] is True
    assert payload["window"] == ["02_implement_h02", "03_implement_h03"]
    assert payload["steps"] == [n[: -len(".jsonl")] for n in names]
    assert len(payload["records"]) == 6


def test_a_window_over_one_frame_is_chunked_and_only_the_last_is_final(tmp_path):
    from tianluo.daemon import client as client_mod

    root = tmp_path / "proj"
    flow = "f-big"
    flow_dir = _flow_dir(root, flow)
    # Two blocks whose records comfortably exceed one frame budget.
    for i in range(2):
        _write_step(
            flow_dir, "%02d_implement_h%d.jsonl" % (i, i),
            [{"role": "assistant", "content": "x" * 4000} for _ in range(20)],
        )
    provider = DaemonHistoryReader(project_roots_provider=lambda: [str(root)])
    client, sock = _client(provider), _Sock()
    original = client_mod.HISTORY_WINDOW_FRAME_BYTES
    client_mod.HISTORY_WINDOW_FRAME_BYTES = 20_000
    try:
        _run(client._handle_history_window_request(sock, {
            "request_id": "r1", "flow_id": flow,
            "project_root": str(root), "count": 2,
        }))
    finally:
        client_mod.HISTORY_WINDOW_FRAME_BYTES = original

    assert len(sock.sent) > 1, "a multi-MB window must not ride in one frame"
    assert [m.payload["final"] for m in sock.sent] == (
        [False] * (len(sock.sent) - 1) + [True]
    )
    # Every record travels exactly once, and the description rides every frame so
    # a first frame lost to a reconnect costs no descriptive state.
    total = sum(len(m.payload["records"]) for m in sock.sent)
    assert total == 40
    for m in sock.sent:
        assert m.payload["steps"] == ["00_implement_h0", "01_implement_h1"]


def test_a_read_failure_is_reported_not_dropped():
    client, sock = _client(_Provider(raises=True)), _Sock()
    _run(client._handle_history_window_request(
        sock, {"request_id": "r1", "flow_id": "f", "count": 2}
    ))
    # Silence would cost the browser the whole pull timeout; an explicit failure
    # lets the server degrade to the full pull immediately.
    assert len(sock.sent) == 1
    assert sock.sent[0].payload["ok"] is False
    assert sock.sent[0].payload["error"]


def test_a_request_without_a_correlation_id_is_ignored():
    client, sock = _client(_Provider()), _Sock()
    _run(client._handle_history_window_request(
        sock, {"flow_id": "f", "count": 2}
    ))
    # Nothing could route the reply anyway; answering would be pure noise.
    assert sock.sent == []


def test_the_explicit_step_selection_reaches_the_provider():
    provider = _Provider(window=type("W", (), {
        "steps": [], "counts": {}, "window": [], "records": [], "first_index": 0,
        "signature": "", "not_modified": False,
    })())
    client, sock = _client(provider), _Sock()
    _run(client._handle_history_window_request(sock, {
        "request_id": "r1", "flow_id": "f", "steps": ["07_self_check_x"],
    }))
    assert provider.calls[0][1]["steps"] == ["07_self_check_x"]


# --------------------------------------------------------------------------
# the conditional (unchanged-window) read
# --------------------------------------------------------------------------
#
# WHY this leg exists at all: a window served straight from the daemon is never
# cached, so the browser holds NO progress token for it and its 3 s self-heal
# poll can only re-ask for the tail. Unconditionally that means the daemon
# re-parses tens of MB of jsonl and the server re-shapes and re-gzips the whole
# window, every tick, for as long as the flow is watched — the exact steady-state
# cost the windowing was introduced to remove.


def test_a_window_read_mints_a_signature(tmp_path):
    root, flow, _names = _seed_flow(tmp_path, blocks=4)
    win = _reader(root).read_flow_window(flow, project_root=str(root), count=2)
    assert win.signature, "a window read must hand back a probe for the next one"
    assert win.not_modified is False


def test_echoing_the_signature_of_an_unchanged_flow_reads_nothing(tmp_path):
    root, flow, _names = _seed_flow(tmp_path, blocks=4)
    reader = _reader(root)
    first = reader.read_flow_window(flow, project_root=str(root), count=2)

    again = reader.read_flow_window(
        flow, project_root=str(root), count=2, if_signature=first.signature
    )
    assert again.not_modified is True
    assert again.signature == first.signature
    # NOTHING else is populated: the counts pass alone walks every byte of every
    # block, so it must not run before the probe is answered.
    assert again.records == [] and again.window == [] and again.counts == {}


def test_the_conditional_read_never_opens_a_block(tmp_path, monkeypatch):
    """The whole point is the syscall count, so pin it rather than the reply."""
    from tianluo.daemon import history as history_mod

    root, flow, _names = _seed_flow(tmp_path, blocks=5)
    reader = _reader(root)
    first = reader.read_flow_window(flow, project_root=str(root), count=2)

    opened = []
    real = history_mod._consumable_lines

    def _spy(path):
        opened.append(str(path))
        return real(path)

    monkeypatch.setattr(history_mod, "_consumable_lines", _spy)
    reader.read_flow_window(
        flow, project_root=str(root), count=2, if_signature=first.signature
    )
    assert opened == [], "an unchanged window must not read a single line"


def test_a_grown_block_invalidates_the_signature(tmp_path):
    root, flow, names = _seed_flow(tmp_path, blocks=4)
    reader = _reader(root)
    first = reader.read_flow_window(flow, project_root=str(root), count=2)

    flow_dir = _flow_dir(root, flow)
    with open(flow_dir / names[-1], "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"role": "assistant", "content": "new"}) + "\n")

    again = reader.read_flow_window(
        flow, project_root=str(root), count=2, if_signature=first.signature
    )
    assert again.not_modified is False
    assert again.signature != first.signature
    assert len(again.records) == 7


def test_a_new_block_invalidates_the_signature(tmp_path):
    root, flow, _names = _seed_flow(tmp_path, blocks=4)
    reader = _reader(root)
    first = reader.read_flow_window(flow, project_root=str(root), count=2)

    _write_step(
        _flow_dir(root, flow), "04_test_h04.jsonl",
        [{"role": "assistant", "content": "fresh block"}],
    )
    again = reader.read_flow_window(
        flow, project_root=str(root), count=2, if_signature=first.signature
    )
    # The flow grew a step block: the tail window is a different pair of blocks
    # now, and answering `not_modified` would freeze the reader on the old tail.
    assert again.not_modified is False
    assert again.window == ["03_implement_h03", "04_test_h04"]


def test_a_signature_is_bound_to_the_request_shape(tmp_path):
    root, flow, _names = _seed_flow(tmp_path, blocks=6)
    reader = _reader(root)
    tail = reader.read_flow_window(flow, project_root=str(root), count=2)
    page = reader.read_flow_window(
        flow, project_root=str(root), count=2, before_step=tail.steps[4]
    )
    assert page.signature != tail.signature

    # A page-up's probe presented against the tail read must NOT be honoured:
    # the two answer different blocks, and "unchanged" would hand the poller a
    # window it never asked for (or, worse, nothing at all).
    crossed = reader.read_flow_window(
        flow, project_root=str(root), count=2, if_signature=page.signature
    )
    assert crossed.not_modified is False
    assert crossed.window == tail.window


def test_the_handler_relays_the_probe_and_answers_not_modified(tmp_path):
    root, flow, _names = _seed_flow(tmp_path, blocks=4)
    provider = DaemonHistoryReader(project_roots_provider=lambda: [str(root)])
    client, sock = _client(provider), _Sock()
    _run(client._handle_history_window_request(sock, {
        "request_id": "r1", "flow_id": flow,
        "project_root": str(root), "count": 2,
    }))
    first = sock.sent[0].payload
    assert first["signature"]

    sock.sent.clear()
    _run(client._handle_history_window_request(sock, {
        "request_id": "r2", "flow_id": flow, "project_root": str(root),
        "count": 2, "if_signature": first["signature"],
    }))
    assert len(sock.sent) == 1
    payload = sock.sent[0].payload
    assert payload["ok"] is True and payload["final"] is True
    assert payload["not_modified"] is True
    # Nothing to carry: the browser keeps the window it already holds.
    assert payload["records"] == [] and payload["steps"] == []


def test_a_stale_probe_still_gets_the_whole_window(tmp_path):
    root, flow, _names = _seed_flow(tmp_path, blocks=4)
    provider = DaemonHistoryReader(project_roots_provider=lambda: [str(root)])
    client, sock = _client(provider), _Sock()
    _run(client._handle_history_window_request(sock, {
        "request_id": "r1", "flow_id": flow, "project_root": str(root),
        "count": 2, "if_signature": "not-the-current-one",
    }))
    payload = sock.sent[0].payload
    assert "not_modified" not in payload
    assert len(payload["records"]) == 6


def test_the_probe_is_optional_on_the_wire(tmp_path):
    # A server that never learned the field, and a daemon answering one that did
    # not ask, must both keep the pre-existing behaviour.
    msg = protocol.make_history_window_request("f", request_id="r1", count=3)
    assert "if_signature" not in msg.payload
    reply = protocol.make_history_window_data("f", request_id="r1")
    assert "signature" not in reply.payload and "not_modified" not in reply.payload
