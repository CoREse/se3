"""Tests for the shared per-message-type wire byte accountant."""

from __future__ import annotations

import threading

from se3.daemon.wire_metrics import WireMetrics


def test_record_and_snapshot_accumulate_by_type():
    m = WireMetrics()
    m.record("status_update", 100)
    m.record("status_update", 50)
    m.record("keepalive", 10)
    snap = m.snapshot()
    assert snap["status_update"] == {"bytes": 150, "count": 2}
    assert snap["keepalive"] == {"bytes": 10, "count": 1}
    # Synthetic roll-up across every type.
    assert snap["__total__"] == {"bytes": 160, "count": 3}


def test_snapshot_is_a_copy():
    m = WireMetrics()
    m.record("x", 5)
    snap = m.snapshot()
    snap["x"]["bytes"] = 9999
    # Mutating the returned dict must not corrupt internal state.
    assert m.snapshot()["x"]["bytes"] == 5


def test_bad_nbytes_coerced_to_zero():
    m = WireMetrics()
    m.record("x", -10)
    m.record("x", None)  # type: ignore[arg-type]
    m.record("x", "not-int")  # type: ignore[arg-type]
    snap = m.snapshot()
    # All coerced to 0 bytes, but each is still a counted frame.
    assert snap["x"] == {"bytes": 0, "count": 3}


def test_reset_clears_counters():
    m = WireMetrics()
    m.record("x", 5)
    m.reset()
    assert m.snapshot() == {"__total__": {"bytes": 0, "count": 0}}


def test_concurrent_record_is_thread_safe():
    m = WireMetrics()
    n_threads = 8
    per_thread = 1000

    def worker():
        for _ in range(per_thread):
            m.record("frame", 1)

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snap = m.snapshot()
    assert snap["frame"] == {"bytes": n_threads * per_thread, "count": n_threads * per_thread}


def test_no_server_import():
    """core/server isolation: the metrics module must not pull in se3.server."""
    import ast
    import inspect

    import se3.daemon.wire_metrics as wm

    tree = ast.parse(inspect.getsource(wm))
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert not any(name.startswith("se3.server") for name in imported)
