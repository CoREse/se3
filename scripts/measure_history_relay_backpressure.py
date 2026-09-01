#!/usr/bin/env python3
"""Measure what relaying a big flow's history drain costs the DAEMON receive loop.

WHY this script exists (and why it is not
``scripts/measure_server_loop_stalls.py``): that script measures *CPU on the
event loop* — how long an otherwise-idle loop is kept from running a due timer
while a render/gzip/usage pass happens. It cannot see the failure this script
exists for, because that failure costs the loop no CPU at all.

The failure: a cache-miss open of a large completed flow makes the daemon answer
one ``MSG_HISTORY_REQUEST`` with a ``full`` head plus ~146 chunk-bounded
``append`` tails. ``ws._serve_loop.receive()`` reads those frames in ONE
coroutine and ``await``s ``_handle_message`` for each, and the history branch of
``_handle_message`` ends in ``UiHub.broadcast_owned`` →
``_fan_out`` → ``await client.send_text(...)`` — an await on the BROWSER's
socket, executed on the DAEMON's receive path. A browser that cannot drain
(slow link, or a JS main thread rebuilding a 4000-record DOM per frame) puts
backpressure on that send, the daemon receive coroutine parks inside it, the
daemon socket's inbound queue stops being consumed, its Pong is never processed,
and uvicorn's WS keepalive closes the daemon connection with
``1011 INTERNAL_ERROR: keepalive ping timeout`` — mid-drain. Everything past
that point of the reply is never delivered, and (because the surviving prefix is
internally self-consistent) nothing on either side can tell.

So this script reports TWO numbers side by side, and the difference between them
is the whole point:

* **loop lateness** — CPU-style blockage, measured by a 5 ms ticker running in
  the same loop (the quantity ``measure_server_loop_stalls.py`` reports);
* **receive-path park** — how long one ``_handle_message`` call takes, i.e. how
  long the daemon's receive coroutine is unable to read the next frame.

Measured on this host against the real ``20260831-095750_23865927`` (147 frames /
4324 records) with a console costing 50 ms per frame, before and after the fix:

===========================  ==================  =================
                             pre-fix             post-fix
===========================  ==================  =================
receive-path park, total     11.35 s             2.45 s
worst park on one frame      0.121 s             0.047 s
worst loop lateness          0.060 s             0.096 s
relayed to the browser       147 frames/103.9MB  2 frames/~0 MB
bundle after the drain       4324 / 52_summarize 4324 / 52_summarize
===========================  ==================  =================

Read the first two rows against the last-but-one: pre-fix, the park tracks the
CONSOLE's cost (it is that console's send, awaited on the daemon's path), so a
browser 8-14 s behind on a single frame — which is what the live server showed —
walks straight through uvicorn's 20 s keepalive. The loop lateness row is flat
across both columns, which is the same statement from the other side: this was
never CPU on the loop. Post-fix the park is the server's own per-frame ingest
work and nothing else.

Run it with the repo's ``src/`` importable, against real history::

    PYTHONPATH=src python scripts/measure_history_relay_backpressure.py \
        --project-root /path/to/repo --flow-id 20260831-095750_23865927

With no ``--project-root`` it synthesizes a structurally equivalent flow, so the
script still runs anywhere (the shape is what matters — many chunk-bounded
frames behind one pull marker).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from typing import Any, Dict, List, Optional

from tianluo.daemon import protocol
from tianluo.server.state import ServerState
from tianluo.server.ws import (
    ConnectionManager,
    HistoryRequestRegistry,
    UiHub,
    _serve_loop,
)

MACHINE_ID = "measure-machine"
OWNER_ID = "measure-owner"


class _SlowUiClient:
    """A ``/ws/ui`` client that takes *delay* seconds to accept each frame.

    Stands in for the real backpressure source: a browser whose link (or whose
    JS main thread) cannot absorb relayed ``history_data`` frames as fast as the
    daemon produces them. ``websockets``/starlette surface that as a slow
    ``send_text`` await, which is exactly what this models.
    """

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.frames = 0
        self.bytes = 0
        self.types: Dict[str, int] = {}

    async def send_text(self, data: str) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        self.frames += 1
        self.bytes += len(data.encode("utf-8"))
        try:
            ptype = json.loads(data).get("type", "?")
        except Exception:  # pragma: no cover - defensive
            ptype = "?"
        self.types[ptype] = self.types.get(ptype, 0) + 1


def _real_frames(project_root: str, flow_id: str) -> List[Dict[str, Any]]:
    """Replay the daemon's own reader over a real flow, yielding its wire frames."""
    from tianluo.daemon.history import DaemonHistoryReader

    reader = DaemonHistoryReader(lambda: [project_root])
    frames: List[Dict[str, Any]] = []
    cursor: Optional[Dict[str, int]] = None
    while True:
        read = reader.read_flow(flow_id, project_root=project_root, cursor=cursor)
        frames.append(
            {
                "mode": read.mode,
                "records": read.records,
                "cursor": dict(read.cursor),
                "cursor_base": dict(getattr(read, "cursor_base", {}) or {}),
            }
        )
        cursor = dict(read.cursor)
        if not getattr(read, "truncated", False):
            break
    return frames


def _synthetic_frames(
    steps: int, records_per_step: int, per_frame: int
) -> List[Dict[str, Any]]:
    """Frames shaped like a real drain: one ``full`` head, chunk-bounded tails.

    Each frame but the last carries one record padded to the daemon's per-frame
    byte budget. WHY that matters and is not cosmetic: reaching the budget is the
    ONLY thing on the wire that says "this reply has more to come", so a frame
    under it retires the pull marker — and every frame after it is then read as
    live tail traffic rather than as part of the reply, which is a different
    delivery path entirely.
    """
    from tianluo.daemon.history import MAX_BYTES_PER_REPORT

    all_records = []
    for s in range(1, steps + 1):
        step_id = "%02d_step_%08x" % (s, s * 2654435761 % (1 << 32))
        for ordinal in range(records_per_step):
            all_records.append(
                {
                    "step_id": step_id,
                    "step_type": "implement",
                    "ordinal": ordinal,
                    "message": {"role": "assistant", "content": "x" * 512},
                }
            )
    frames: List[Dict[str, Any]] = []
    cursor: Dict[str, int] = {}
    for start in range(0, len(all_records), per_frame):
        chunk = all_records[start : start + per_frame]
        if start + per_frame < len(all_records):
            chunk[0] = dict(
                chunk[0],
                message=dict(chunk[0]["message"], content="x" * MAX_BYTES_PER_REPORT),
            )
        # ``cursor_base`` is the read's START line for every file this frame
        # touches — 0 for a file the drain reaches for the first time, not
        # "absent", or the server reads the frame as starting past a water mark
        # it never had and discards it as a cursor gap.
        base = {
            rec["step_id"] + ".jsonl": cursor.get(rec["step_id"] + ".jsonl", 0)
            for rec in chunk
        }
        for rec in chunk:
            cursor[rec["step_id"] + ".jsonl"] = rec["ordinal"] + 1
        frames.append(
            {
                "mode": (
                    protocol.HISTORY_MODE_FULL
                    if not frames
                    else protocol.HISTORY_MODE_APPEND
                ),
                "records": chunk,
                "cursor": dict(cursor),
                "cursor_base": base,
            }
        )
    return frames


class _ReplayingDaemonSocket:
    """The daemon leg, standing in for the real socket ``_serve_loop`` reads.

    It also MEASURES the thing that matters: the gap between one frame being
    handed to the receive loop and the loop coming back for the next one. That
    gap IS the park — the window in which the daemon socket's inbound queue is
    not being drained, so a Pong sitting behind the backlog is not processed and
    uvicorn's keepalive fires.
    """

    def __init__(self, frames: List[str]) -> None:
        self._frames = list(frames)
        self._index = 0
        self._handed_at: Optional[float] = None
        self.parks: List[float] = []

    async def receive_text(self) -> str:
        now = time.perf_counter()
        if self._handed_at is not None:
            self.parks.append(now - self._handed_at)
        if self._index >= len(self._frames):
            raise RuntimeError("daemon reply exhausted")
        frame = self._frames[self._index]
        self._index += 1
        self._handed_at = time.perf_counter()
        return frame

    async def send_text(self, text: str) -> None:  # heartbeat PING
        return None

    async def close(self) -> None:
        return None


async def _drive(
    frames: List[Dict[str, Any]], flow_id: str, ui_delay: float, tick: float
) -> None:
    state = ServerState()
    await state.register_machine(MACHINE_ID, "host", "127.0.0.1", owner_id=OWNER_ID)
    hub = UiHub()
    client = _SlowUiClient(ui_delay)
    await hub.register(client, OWNER_ID)
    registry = HistoryRequestRegistry()

    # Park a REST waiter and arm the replay marker exactly as a cache-miss open
    # does (``request_history`` arms it before the send), so the drain is
    # classified as one pull reply and not as live tail appends.
    waiter = registry.register(flow_id, machine_id=MACHINE_ID)
    await state.mark_history_replay(flow_id, cursor=None)

    lateness: List[float] = []
    stop = asyncio.Event()

    async def ticker() -> None:
        while not stop.is_set():
            due = time.perf_counter() + tick
            await asyncio.sleep(tick)
            lateness.append(time.perf_counter() - due)

    tick_task = asyncio.create_task(ticker())
    wire = [
        protocol.make_history_data(
            flow_id,
            frame["mode"],
            frame["records"],
            cursor=frame["cursor"],
            cursor_base=frame["cursor_base"],
        ).to_json()
        for frame in frames
    ]
    socket = _ReplayingDaemonSocket(wire)
    manager = ConnectionManager()
    await manager.connect(MACHINE_ID, socket)
    started = time.perf_counter()
    # The real receive loop, so the measurement covers the decode, the ingest
    # and the fan-out exactly as the server runs them.
    await _serve_loop(socket, manager, state, MACHINE_ID, hub, registry)
    wall = time.perf_counter() - started
    parks = socket.parks or [0.0]
    stop.set()
    tick_task.cancel()
    try:
        await tick_task
    except asyncio.CancelledError:
        pass
    # Let any deferred (post-fix) delivery finish so the relayed-byte count is
    # comparable across both code shapes.
    drain = getattr(hub, "wait_drained", None)
    if drain is not None:
        try:
            await asyncio.wait_for(drain(), timeout=60.0)
        except asyncio.TimeoutError:
            pass

    bundle = await state.get_history(flow_id, touch=False)
    records = bundle.get("records") or []
    total_records = sum(len(f["records"]) for f in frames)

    print("frames driven           : %d" % len(frames))
    print("records offered         : %d" % total_records)
    print("records in bundle       : %d" % len(records))
    print(
        "last record             : %s"
        % (records[-1].get("step_id") if records else "-")
    )
    print("REST waiter resolved    : %s" % waiter.done())
    print("")
    print("--- daemon receive-path park (frame handed → next frame read) ---")
    print("  worst   : %.3fs" % max(parks))
    print("  p95     : %.3fs" % statistics.quantiles(parks, n=20)[-1])
    print("  total   : %.3fs over %.3fs wall" % (sum(parks), wall))
    print("  >1.0s   : %d frames" % sum(1 for p in parks if p > 1.0))
    print("")
    print("--- event-loop lateness (%.0f ms ticker) ---" % (tick * 1000))
    print("  ticks   : %d" % len(lateness))
    print("  worst   : %.3fs" % (max(lateness) if lateness else 0.0))
    print("  >200ms  : %d" % sum(1 for l in lateness if l > 0.2))
    print("  >1.0s   : %d" % sum(1 for l in lateness if l > 1.0))
    print("")
    print("--- relayed to the (slow) browser ---")
    print("  frames  : %d  (%.1f MB)" % (client.frames, client.bytes / 1e6))
    print("  by type : %s" % client.types)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", default="")
    parser.add_argument("--flow-id", default="20260831-095750_23865927")
    parser.add_argument("--ui-delay", type=float, default=0.1)
    parser.add_argument("--tick", type=float, default=0.005)
    parser.add_argument("--max-frames", type=int, default=0)
    parser.add_argument("--steps", type=int, default=52)
    parser.add_argument("--records-per-step", type=int, default=83)
    parser.add_argument("--per-frame", type=int, default=30)
    args = parser.parse_args()

    if args.project_root:
        frames = _real_frames(args.project_root, args.flow_id)
        flow_id = args.flow_id
    else:
        frames = _synthetic_frames(
            args.steps, args.records_per_step, args.per_frame
        )
        flow_id = "synthetic-flow"
    if args.max_frames:
        frames = frames[: args.max_frames]
    asyncio.run(_drive(frames, flow_id, args.ui_delay, args.tick))


if __name__ == "__main__":
    main()
