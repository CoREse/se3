#!/usr/bin/env python3
"""Measure what a big history bundle costs the tianluo-server event loop.

WHY this script exists: the server's stutter fixes (batching the REST and
``/ws/ui`` JSON renders so they yield to the loop, offloading the gzip pass,
memoizing the usage rebuild that ran under ``ServerState._lock``) are only
defensible if the numbers behind them are reproducible. Every "measured ~N ms"
figure quoted in a why-comment in ``src/tianluo/server/{app,ws,state}.py`` comes
from here, and the whole point of the output is to separate *confirmed* stall
sources from work that merely looks heavy: several O(bundle) helpers that run
inside the state lock turn out to cost one or two milliseconds on a 16 MiB
bundle and are deliberately left alone.

It measures two different things, and the difference is the load-bearing part:

* **cost** — how long a call takes (the ``=== N records ===`` table);
* **loop lateness** — how long an otherwise-idle event loop is prevented from
  running a due timer while that call happens (the ``loop lateness`` table).

They are NOT interchangeable. ``asyncio.to_thread`` only removes lateness for
work that releases the GIL: ``zlib`` does (gzip drops from ~131 ms of lateness
to ~1 ms), CPython's C JSON encoder/scanner does not (``json.dumps`` goes from
~53 ms to ~46 ms — a thread hop that buys almost nothing and adds a round-trip).
That is why the JSON renders are batched with an ``await`` between record
batches (~7 ms lateness) instead of being offloaded, and why the inbound frame
parse — which cannot be batched without abandoning the C scanner — is left inline
and bounded at the daemon side instead (``daemon.history.MAX_BYTES_PER_REPORT``).

Run it with the repo's ``src/`` importable::

    PYTHONPATH=src python scripts/measure_server_loop_stalls.py

Numbers are host- and CPython-version-specific; what should survive across hosts
is the ORDERING (usage rebuild ≫ gzip > JSON render ≈ fan-out render > frame
parse ≫ the index/pending/copy helpers) and the GIL behaviour above, which is
what the code's decisions rest on.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import statistics
import time
from typing import Any, Callable, Dict, List

from tianluo.daemon import protocol
from tianluo.server.state import ServerState, _estimate_record_bytes
from tianluo.server.ws import dump_json_chunked

#: The two bundle sizes that matter: a routine multi-MB conversation, and one at
#: the protocol's 16 MiB frame ceiling — which exists precisely to carry a whole
#: session in a single ``MSG_HISTORY_DATA``.
SHAPES = ((1500, 2000), (4000, 4000))


def _record(step: str, ordinal: int, chars: int) -> Dict[str, Any]:
    """A history record shaped like the daemon's: envelope + message + usage."""
    return {
        "step_id": step,
        "step_type": "discovery",
        "ordinal": ordinal,
        "message": {
            "role": "assistant",
            "content": "x" * chars,
            "usage_records": [
                {
                    "call_id": "call-%d" % ordinal,
                    "model": "claude-opus-4",
                    "input_tokens": 1200,
                    "output_tokens": 800,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                }
            ],
        },
    }


def _bench(rows: List[tuple], label: str, fn: Callable[[], Any], runs: int) -> None:
    samples = []
    for _ in range(runs):
        started = time.perf_counter()
        fn()
        samples.append(time.perf_counter() - started)
    rows.append((label, min(samples) * 1000.0, statistics.median(samples) * 1000.0))


def measure(count: int, chars: int, runs: int) -> None:
    records = [_record("plan", i, chars) for i in range(count)]
    cursor = {"plan.jsonl": count}
    payload = {"flow_id": "f", "delivery": "full", "records": records}
    body = json.dumps(
        payload, ensure_ascii=False, allow_nan=False,
        separators=(",", ":"), default=str,
    ).encode("utf-8")
    frame = protocol.make_history_data(
        "f", protocol.HISTORY_MODE_FULL, records, cursor=cursor
    ).to_json()
    ui_frame = {
        "type": "history_data", "flow_id": "f", "mode": "full", "records": records,
    }

    rows: List[tuple] = []
    _bench(rows, "REST render: json.dumps(payload)", lambda: json.dumps(
        payload, ensure_ascii=False, allow_nan=False,
        separators=(",", ":"), default=str,
    ).encode("utf-8"), runs)
    _bench(rows, "REST render: gzip.compress(body, 9)",
           lambda: gzip.compress(body, 9), runs)
    _bench(rows, "ws receive: protocol.decode(frame)",
           lambda: protocol.decode(frame), runs)
    _bench(rows, "/ws/ui fan-out: json.dumps(frame) [PER CLIENT]",
           lambda: json.dumps(ui_frame, ensure_ascii=False, default=str), runs)
    # Everything below runs inside ``ServerState._lock``. A fresh bundle dict per
    # call defeats the on-bundle memos, so these are the UNMEMOIZED costs — i.e.
    # what each one used to cost on every read.
    _bench(rows, "LOCK: usage rebuild (extract + build_usage_payload)",
           lambda: ServerState._bundle_usage(
               {"flow_id": "f", "records": records}), runs)
    _bench(rows, "LOCK: _bundle_pending_positions(records, cursor)",
           lambda: ServerState._bundle_pending_positions(records, cursor), runs)
    _bench(rows, "LOCK: _unnumbered_steps(records)",
           lambda: ServerState._unnumbered_steps(records), runs)
    _bench(rows, "LOCK: _index_records_by_ordinal(records)",
           lambda: ServerState._index_records_by_ordinal(records), runs)
    _bench(rows, "LOCK: byte accounting over the whole bundle",
           lambda: sum(_estimate_record_bytes(r) for r in records), runs)
    _bench(rows, "LOCK: list(records) shallow copy",
           lambda: list(records), runs)

    print(
        "\n=== %d records, %.1f MiB serialized (frame %.1f MiB) ==="
        % (count, len(body) / 1048576.0, len(frame) / 1048576.0)
    )
    for label, best, median in sorted(rows, key=lambda r: -r[1]):
        print("  %-52s %8.1f ms   (median %.1f)" % (label, best, median))


async def _worst_timer_lateness(work: Callable[[], Any], *, offload: bool,
                                runs: int) -> float:
    """Worst lateness (seconds) of a 5 ms timer on an otherwise-idle loop.

    This is the number that matters operationally: it is how long another
    daemon's heartbeat, a browser poll or a ``/ws/ui`` send waits because of
    *work*. A 5 ms period models the loop having something due at all times
    without the ticker itself burning CPU that would compete for the GIL and
    flatter the offloaded case.
    """
    worst = 0.0
    period = 0.005
    for _ in range(runs):
        stop = asyncio.Event()
        lateness: List[float] = []

        async def ticker() -> None:
            while not stop.is_set():
                started = time.perf_counter()
                await asyncio.sleep(period)
                lateness.append(time.perf_counter() - started - period)

        spinner = asyncio.ensure_future(ticker())
        await asyncio.sleep(0.05)
        lateness.clear()
        if offload:
            await asyncio.to_thread(work)
        else:
            result = work()
            if asyncio.iscoroutine(result):
                await result
        await asyncio.sleep(0.02)
        stop.set()
        await spinner
        worst = max(worst, max(lateness) if lateness else 0.0)
    return worst


def measure_lateness(count: int, chars: int, runs: int) -> None:
    """How much of each cost the EVENT LOOP actually pays, per mechanism."""
    records = [_record("plan", i, chars) for i in range(count)]
    payload = {"flow_id": "f", "delivery": "full", "records": records}
    dump_kwargs = dict(
        ensure_ascii=False, allow_nan=False, separators=(",", ":"), default=str
    )
    body = json.dumps(payload, **dump_kwargs).encode("utf-8")
    frame = protocol.make_history_data(
        "f", protocol.HISTORY_MODE_FULL, records, cursor={"plan.jsonl": count}
    ).to_json()

    cases = (
        ("json.dumps           inline",
         lambda: json.dumps(payload, **dump_kwargs), False),
        ("json.dumps           to_thread",
         lambda: json.dumps(payload, **dump_kwargs), True),
        ("json.dumps           batched (dump_json_chunked)",
         lambda: dump_json_chunked(payload, **dump_kwargs), False),
        ("gzip.compress        inline", lambda: gzip.compress(body, 9), False),
        ("gzip.compress        to_thread", lambda: gzip.compress(body, 9), True),
        ("protocol.decode      inline", lambda: protocol.decode(frame), False),
        ("protocol.decode      to_thread", lambda: protocol.decode(frame), True),
    )

    async def run_all() -> List[tuple]:
        return [
            (label, await _worst_timer_lateness(work, offload=off, runs=runs))
            for label, work, off in cases
        ]

    rows = asyncio.run(run_all())
    print("\n--- loop lateness (%d records, %.1f MiB) ---"
          % (count, len(body) / 1048576.0))
    for label, seconds in rows:
        print("  %-46s %8.1f ms" % (label, seconds * 1000.0))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--runs", type=int, default=5,
        help="timed repetitions per measurement (the minimum is reported)",
    )
    args = parser.parse_args()
    for count, chars in SHAPES:
        measure(count, chars, args.runs)
        measure_lateness(count, chars, args.runs)


if __name__ == "__main__":
    main()
