"""Field acceptance of the windowed history transport, on the REAL flow.

Every other test in this area drives synthetic blocks. This one drives the flow
the defect was reported against — ``20260829-224712_878b4fc9`` under
``~/workspace/creqt``: 222,231,505 bytes of jsonl across 67 step blocks, whose
whole-flow delivery was 161 MiB and whose in-memory bundle was 554.8 MiB against
a 256 MiB cache budget. Synthetic blocks cannot reproduce what actually broke:
the eviction⇄re-pull storm only appears when ONE flow is larger than the entire
cache, and the step-payload sizes (a single 9.2 MB ``step_completed`` whose
``inputs.scope_diff`` is 8.7 MB of git diff) only appear in real engine output.

The daemon leg is stood in for by :class:`DaemonHistoryReader` reading the real
directory, framed exactly as ``DaemonClient._handle_history_window_request``
frames it (same 1 MiB chunking, same repeated window description). So the read
path, the wire framing, the server's relay, the payload shaping and the REST
contract are all the production code — only the socket between daemon and server
is the test's.

The whole module skips when that flow is not on this machine: it is a field
acceptance harness for the host it was reported on, not a portable fixture.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from _authsrv import authed_app, authed_hello, login
from tianluo.daemon import protocol
from tianluo.daemon.client import HISTORY_WINDOW_FRAME_BYTES
from tianluo.daemon.history import DaemonHistoryReader

FLOW = "20260829-224712_878b4fc9"
PROJECT_ROOT = os.path.expanduser("~/workspace/creqt")
HISTORY_DIR = Path(PROJECT_ROOT) / "tianluo" / "history" / FLOW
MACHINE = "hdev-real"

#: The flow's tail block — the one the reader must land on when the view opens.
TAIL_BLOCK_PREFIX = "55_summarize"
#: …and its first, the one paging up must be able to reach.
HEAD_BLOCK_PREFIX = "01_discovery"

#: What the pre-fix whole-flow delivery cost this flow, measured with
#: ``summarize_history_records`` over all 10021 records (REST compact-JSON
#: accounting). The tail window has to be a different order of magnitude, not
#: merely smaller — that gap is the fix.
WHOLE_FLOW_DELIVERY_BYTES = 161 * 1024 * 1024

pytestmark = [
    pytest.mark.skipif(
        not HISTORY_DIR.is_dir(),
        reason="real flow %s is not present on this machine" % FLOW,
    ),
    # Pinned to one xdist worker so the module-scoped fixtures below are built
    # ONCE. Under the repo's `--dist loadgroup` an ungrouped module scatters its
    # tests across workers, and each worker would then re-read the flow's 222 MB
    # for itself — a five-fold IO cost for identical coverage.
    pytest.mark.xdist_group(name="real_flow_history"),
]


# ==========================================================================
# the daemon stand-in
# ==========================================================================


def _serve_window_request(sock, payload, reader):
    """Answer one HISTORY_WINDOW_REQUEST off the real on-disk flow.

    Mirrors the daemon's own handler: read the window, then chunk by WIRE BYTES
    rather than by block, because one block of this flow is a single multi-MB
    record and a block-per-frame rule would emit frames far past what the socket
    should carry in one write.
    """
    request_id = str(payload.get("request_id") or "")
    steps = payload.get("steps")
    window = reader.read_flow_window(
        str(payload.get("flow_id") or ""),
        project_root=str(payload.get("project_root") or "") or PROJECT_ROOT,
        count=int(payload.get("count") or 10),
        before_step=str(payload.get("before_step") or "") or None,
        steps=steps if isinstance(steps, list) and steps else None,
        if_signature=str(payload.get("if_signature") or "") or None,
    )

    def _send(records, final):
        sock.send_text(
            protocol.make_history_window_data(
                window.flow_id,
                request_id=request_id,
                records=records,
                steps=window.steps,
                window=window.window,
                counts=window.counts,
                signature=window.signature,
                final=final,
            ).to_json()
        )

    if window.not_modified:
        sock.send_text(
            protocol.make_history_window_data(
                FLOW, request_id=request_id,
                signature=window.signature, not_modified=True, final=True,
            ).to_json()
        )
        return window

    chunk, chunk_bytes = [], 0
    for record in window.records:
        size = len(json.dumps(record, ensure_ascii=False))
        if chunk and chunk_bytes + size > HISTORY_WINDOW_FRAME_BYTES:
            _send(chunk, False)
            chunk, chunk_bytes = [], 0
        chunk.append(record)
        chunk_bytes += size
    _send(chunk, True)
    return window


class _RealDaemon:
    """A connected daemon whose history reads come off the real flow."""

    def __init__(self, client, app):
        self._client = client
        self._ctx = client.websocket_connect("/ws")
        self.sock = self._ctx.__enter__()
        self.sock.send_text(
            authed_hello(
                app, MACHINE, "hnas-dev", "12.14.1",
                protocol_version=protocol.PROTOCOL_VERSION,
            )
        )
        protocol.decode(self.sock.receive_text())  # WELCOME
        self.sock.send_text(
            protocol.make_history_index(
                [{"flow_id": FLOW, "project_root": PROJECT_ROOT}]
            ).to_json()
        )
        self.reader = DaemonHistoryReader(lambda: [PROJECT_ROOT])
        #: Every window this daemon was asked for, so a test can assert the
        #: browse never fell back to the whole-flow pull.
        self.served = []

    def close(self):
        self._ctx.__exit__(None, None, None)

    def get(self, path, params=None):
        """Issue a REST GET, servicing the daemon side until it answers."""
        out = {}

        def _run():
            out["resp"] = self._client.get(path, params=params or {})

        worker = threading.Thread(target=_run)
        worker.start()
        try:
            while worker.is_alive() or "resp" not in out:
                msg = protocol.decode(self.sock.receive_text())
                if msg.type == protocol.MSG_HISTORY_WINDOW_REQUEST:
                    self.served.append(
                        _serve_window_request(self.sock, msg.payload, self.reader)
                    )
                elif msg.type == protocol.MSG_HISTORY_REQUEST:
                    raise AssertionError(
                        "server asked for a WHOLE-FLOW pull of a %d-block flow; "
                        "the windowed browse must never re-enter that path"
                        % len(self.reader.read_flow_window(FLOW, count=1).steps)
                    )
                if "resp" in out and not worker.is_alive():
                    break
        finally:
            worker.join(timeout=120)
        return out["resp"]


@pytest.fixture(scope="module")
def daemon_and_client():
    """One connected daemon for the module.

    Module-scoped because every read here walks real jsonl: the tail window is
    25 MB off disk and the page-back walk is the whole 222 MB. A per-test
    connection would re-read all of it once per assertion for no added coverage
    — nothing in this module mutates server state (the point of most of it is
    that NOTHING is cached), so the tests do not interact.
    """
    from fastapi.testclient import TestClient

    app, _key = authed_app()
    with TestClient(app) as client:
        login(client)
        daemon = _RealDaemon(client, app)
        try:
            yield daemon, client, app
        finally:
            daemon.close()


@pytest.fixture(scope="module")
def tail_window(daemon_and_client):
    """The default open: the flow's last 10 step blocks."""
    daemon, _client, _app = daemon_and_client
    resp = daemon.get("/api/history/%s" % FLOW, {"window": 10})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _wire_bytes(body):
    return len(json.dumps(body, ensure_ascii=False, separators=(",", ":")))


def _event_type(record):
    """The engine event type of a delivered record.

    A delivered record is the daemon's holder — ``{step_id, step_type, ordinal,
    message}`` — and the engine's own ``type`` lives on the message inside it,
    not on the holder.
    """
    return ((record.get("message") or {}).get("type")) or record.get("type")


# ==========================================================================
# 1. the view opens on the tail
# ==========================================================================


def test_the_real_flow_opens_on_its_tail_window(tail_window):
    body = tail_window

    assert body["delivery"] == "window"
    assert body["window"]["mode"] == "tail"
    assert body["window"]["source"] == "daemon"
    loaded = body["window"]["loaded"]
    assert len(loaded) == 10
    # The last block of the flow, and it really is the summarize step whose
    # step_completed the reader must land on.
    assert loaded[-1].startswith(TAIL_BLOCK_PREFIX)
    assert body["window"]["has_earlier"] is True
    assert body["window"]["steps"][-1] == loaded[-1]
    tail_records = [r for r in body["records"] if r["step_id"] == loaded[-1]]
    assert any(
        _event_type(r) == "step_completed" for r in tail_records
    ), "the tail block carries no step_completed"

    # The transport is windowed, not merely rendered windowed: the response is
    # an order of magnitude off the 161 MiB the whole flow used to cost.
    assert _wire_bytes(body) < WHOLE_FLOW_DELIVERY_BYTES // 8

    # …and the browser is told the delivery is settled, so its interrupted-
    # delivery repair loop — the `history delivery incomplete` storm — is never
    # armed for the head it has deliberately not loaded.
    assert body["incomplete"] is False


def test_the_tail_window_creates_no_bundle_for_an_over_budget_flow(
    daemon_and_client, tail_window
):
    """554.8 MiB of records against a 256 MiB budget must never be cached."""
    _daemon, _client, app = daemon_and_client
    state = app.state.server_state
    assert FLOW not in state._history_data


# ==========================================================================
# 2. paging up reaches the first block
# ==========================================================================


def test_the_real_flow_pages_back_to_its_very_first_block(
    daemon_and_client, tail_window
):
    """The acceptance the storm made impossible: browse to record one."""
    daemon, _client, app = daemon_and_client
    body = tail_window
    all_steps = list(body["window"]["steps"])
    seen = list(body["window"]["loaded"])
    pages = 1

    while body["window"]["has_earlier"]:
        anchor = body["window"]["steps"][body["window"]["first_index"]]
        body = daemon.get(
            "/api/history/%s" % FLOW, {"window": 10, "before": anchor}
        ).json()
        assert body["window"]["mode"] == "before"
        assert body["records"], "a page-up returned no records"
        seen = list(body["window"]["loaded"]) + seen
        pages += 1
        assert pages <= 20, "paging up did not converge"

    # Every block of the flow was reachable, in order, ending at the first one.
    assert seen == all_steps
    assert seen[0].startswith(HEAD_BLOCK_PREFIX)
    # Still no bundle: the whole browse ran off direct window reads, so there
    # was nothing to evict and nothing to re-pull. That absence IS the fix —
    # a flow larger than the entire cache budget stayed browsable throughout.
    assert FLOW not in app.state.server_state._history_data
    assert app.state.server_state._history_evictions == 0, (
        "a windowed browse of the flow evicted a bundle — the eviction⇄re-pull "
        "steady state this fix exists to remove"
    )


# ==========================================================================
# 3. the step-event payloads are lazy on real records
# ==========================================================================


def _largest_step_event(records):
    events = [
        r for r in records
        if _event_type(r) in ("step_completed", "step_failed", "step_output")
    ]
    assert events, "the window carried no step events"
    return max(events, key=lambda r: len(json.dumps(r, ensure_ascii=False)))


def _original_on_disk(step_id, ordinal):
    """The record as the engine actually wrote it, straight off the jsonl."""
    with (HISTORY_DIR / ("%s.jsonl" % step_id)).open(encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if i == ordinal:
                return json.loads(line)
    raise AssertionError("no line %d in block %s" % (ordinal, step_id))


def test_real_step_events_ship_their_outputs_but_not_their_inputs(tail_window):
    from tianluo.server.history_summary import (
        STEP_INPUTS_LAZY_KEY,
        STEP_INPUT_INLINE_KEYS,
    )

    record = _largest_step_event(tail_window["records"])
    message = record["message"]
    original = _original_on_disk(record["step_id"], record["ordinal"])
    original_step = (original.get("data") or {}).get("step") or {}

    assert message.get(STEP_INPUTS_LAZY_KEY) is True
    step = (message.get("data") or {}).get("step") or {}
    # Held back down to the handful of scalars the report card actually reads:
    # no scope_diff, no test_results, no fix_history, no thrice-repeated task
    # description riding inline. Checked against the ORIGINAL, so a step whose
    # inputs happened to be empty cannot pass this vacuously.
    assert set(original_step.get("inputs") or {}) - set(STEP_INPUT_INLINE_KEYS)
    assert set(step.get("inputs") or {}) <= set(STEP_INPUT_INLINE_KEYS)

    # …while EVERY other field of the snapshot is inline and byte-identical to
    # what the engine wrote — that identity, rather than a hand-listed set of
    # keys, is what keeps the report card and the usage chip pixel-unchanged
    # across step types (a test step's card reads test_results where a
    # self_check's reads issues, and neither may be shaped).
    for key, value in original_step.items():
        if key == "inputs":
            continue
        assert step.get(key) == value, "step.%s was altered on the wire" % key
    for key, value in original.items():
        if key == "data":
            continue
        assert message.get(key) == value, "%s was altered on the wire" % key

    assert message.get("detail_flow") == FLOW
    assert message.get("detail_version")


def test_the_raw_chip_fetches_a_real_step_payload_back_whole(
    daemon_and_client, tail_window
):
    """View raw on an uncached, over-budget flow still prints the record."""
    daemon, _client, _app = daemon_and_client
    record = _largest_step_event(tail_window["records"])

    detail = daemon.get(
        "/api/history/%s/detail" % FLOW,
        {
            "source": "step",
            "step_id": record["step_id"],
            "ordinal": record["ordinal"],
        },
    )
    assert detail.status_code == 200, detail.text
    payload = detail.json()
    assert payload["source"] == "step"
    fetched = json.dumps(payload["record"], ensure_ascii=False)
    # The whole held-back machine input came back — for the biggest record of
    # this flow that is megabytes of scope_diff, which is exactly what the
    # inline delivery used to be paying for on every open.
    assert len(fetched) > len(json.dumps(record["message"], ensure_ascii=False))
    # …and it is the ORIGINAL message, carrying no wire marker, so "View raw"
    # prints the record exactly as the daemon stored it.
    assert "step_inputs_lazy" not in payload["record"]
    assert payload["record"] == _original_on_disk(
        record["step_id"], record["ordinal"]
    )
    assert isinstance(payload.get("inputs"), dict)


# ==========================================================================
# 4. the steady-state cost of WATCHING this flow
# ==========================================================================
#
# The window leg builds no bundle, so the browser holds no progress token for it
# and its 3 s self-heal poll can only re-ask for the tail. On THIS flow an
# unconditional re-ask is 25 MB of jsonl re-parsed by the daemon and several MB
# of shaped JSON re-sent to the browser, every tick, for as long as the flow is
# watched — an unwindowed view of the same flow answered its poll with a
# signature comparison. The window therefore carries a probe of its own.


def test_the_tail_window_hands_back_a_probe(tail_window):
    assert tail_window["window"]["signature"], (
        "a relayed window with no probe leaves the poll unconditional"
    )


def test_watching_this_flow_costs_a_stat_per_block_not_a_re_read(
    daemon_and_client, tail_window
):
    from tianluo.daemon import history as history_mod

    daemon, _client, app = daemon_and_client
    probe = tail_window["window"]["signature"]

    opened = []
    real = history_mod._consumable_lines

    def _spy(path):
        opened.append(str(path))
        return real(path)

    history_mod._consumable_lines = _spy
    try:
        resp = daemon.get(
            "/api/history/%s" % FLOW, {"window": 10, "wsig": probe}
        )
    finally:
        history_mod._consumable_lines = real

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["delivery"] == "not_modified"
    # Not one line of the flow's 222 MB was read, and not one record shipped.
    assert opened == [], "an unchanged tail was re-read off disk"
    assert body["records"] == []
    assert _wire_bytes(body) < 4096
    # No window block: the browser keeps the block index and the paged-open span
    # it already holds, so the poll cannot collapse the reader back to the tail.
    assert "window" not in body
    # …and it stays settled and uncached, so neither the repair loop nor the
    # eviction⇄re-pull steady state can be armed by the poll itself.
    assert body["incomplete"] is False
    assert FLOW not in app.state.server_state._history_data
    assert app.state.server_state._history_evictions == 0


def test_a_changed_tail_is_still_delivered_in_full(daemon_and_client, tail_window):
    """The probe must not be able to freeze a live reader on a stale tail."""
    daemon, _client, _app = daemon_and_client
    resp = daemon.get(
        "/api/history/%s" % FLOW, {"window": 10, "wsig": "a-probe-from-before"}
    )
    body = resp.json()
    assert body["delivery"] == "window"
    assert body["window"]["loaded"] == tail_window["window"]["loaded"]
    assert len(body["records"]) == len(tail_window["records"])
    assert body["window"]["signature"] == tail_window["window"]["signature"]
