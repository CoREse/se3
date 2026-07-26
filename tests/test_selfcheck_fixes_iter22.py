"""Regression tests for the iteration-22 self-check fixes (issue #243 / #244 一期).

Locks in the fixes for:

* **A failed lazy read of the externalized context must not be persisted as truth
  (persistence.py / models.py).** ``_build_lazy_flow`` (load_flow_by_id /
  load_resumable_snapshot) and the eager ``_reconstruct_full_dict`` (load_flow)
  both leave ``State.cold_context_loaded`` False when ``_context.json`` cannot be
  read, so the very next ``save_flow`` re-emits the recorded ``context_ref``
  verbatim instead of atomically overwriting an intact cold context file with
  ``{}`` — a transient EACCES/EIO/NFS blip no longer permanently loses flow
  context. The context analogue of the per-step ``cold_loaded`` guard.
* **Mixed-key payloads are hashable (persistence._canonical_json).** A step
  input/output/context dict mixing ``str`` and ``int`` keys no longer raises
  ``TypeError`` out of ``sort_keys=True``, so such a flow can still be persisted.
* **END_SESSION handler runs off the websocket event loop (client.py).** The
  injected end-session handler is dispatched via ``asyncio.to_thread`` so a
  handler that spawns a subprocess / inspects engine state never blocks the
  websocket receive loop, heartbeats, or push processing.
* **pending_calls_signature snapshots the shared root set (aggregator.py).** The
  signature path iterates a ``list()`` copy of ``_project_roots`` so a concurrent
  root registration in another thread cannot raise "Set changed size during
  iteration".
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from tianluo.engine.models import FlowInstance, FlowStatus, Step, StepStatus, StepType
from tianluo.engine.persistence import (
    PersistenceManager,
    _canonical_json,
    _content_hash,
)


def _make_flow(n_steps: int = 3, payload_size: int = 20_000) -> FlowInstance:
    flow = FlowInstance(task_description="iter22 flow", status=FlowStatus.PAUSED)
    flow.task_type = "feature"
    blob = "Z" * payload_size
    for i in range(n_steps):
        step = Step(step_type=StepType.IMPLEMENT, status=StepStatus.COMPLETED)
        step.inputs = {"test_results": blob, "idx": i}
        step.outputs = {"ok": True}
        flow.state.add_step(step)
    flow.state.selected_steps = [StepType.IMPLEMENT]
    flow.state.current_step_id = flow.state.step_history[-1]
    flow.state.context = {"spec_content": blob, "resolved_type": "feature"}
    flow.state.increment_fix_iteration({"reason": "ctx", "blob": blob})
    return flow


# -- context read failure must never clobber the intact cold context file ----


def _cold_context_path(tmp_path: Path, flow_id: str) -> Path:
    return tmp_path / "tianluo" / "state" / "steps" / flow_id / "_context.json"


@pytest.mark.parametrize("loader", ["lazy", "eager"])
def test_failed_context_read_is_not_persisted_over_intact_file(tmp_path, loader):
    """A transient context read failure must not overwrite the real cold file.

    Simulate an unreadable ``_context.json`` at load time (its content is
    replaced by unparseable bytes so the read degrades to empty), then re-save.
    The fix keeps ``cold_context_loaded`` False, so the recorded ``context_ref``
    is re-emitted and the *original* (restored) file survives unchanged — no
    empty ``{}`` is written over it.
    """
    pm = PersistenceManager(tmp_path)
    flow = _make_flow()
    pm.save_flow(flow)
    ctx_file = _cold_context_path(tmp_path, flow.flow_id)
    good_bytes = ctx_file.read_bytes()
    good_payload = json.loads(good_bytes)
    assert good_payload["context"]["resolved_type"] == "feature"

    # Corrupt the cold context so the load-time read fails (parse error).
    ctx_file.write_text("{ this is not json", encoding="utf-8")

    fresh = PersistenceManager(tmp_path)
    if loader == "lazy":
        loaded = fresh.load_flow_by_id(flow.flow_id)
    else:
        loaded = fresh.load_flow()
    assert loaded is not None
    # This access degrades to empty for this session (tolerant read, B3)...
    assert loaded.state.context == {}
    assert loaded.state.cold_context_loaded is False

    # Pretend the transient failure has cleared: restore the real bytes on disk.
    ctx_file.write_bytes(good_bytes)

    # Re-persisting must NOT rewrite the (now intact) cold context with {}.
    fresh.save_flow(loaded)
    after = json.loads(ctx_file.read_bytes())
    assert after == good_payload
    assert after["context"]["resolved_type"] == "feature"


def test_loaded_context_still_round_trips(tmp_path):
    """A normally-loaded context IS marked loaded and persists genuine edits."""
    pm = PersistenceManager(tmp_path)
    flow = _make_flow()
    pm.save_flow(flow)

    fresh = PersistenceManager(tmp_path)
    loaded = fresh.load_flow_by_id(flow.flow_id)
    assert loaded is not None
    assert loaded.state.cold_context_loaded is True
    loaded.state.context["resolved_type"] = "bugfix"
    fresh.save_flow(loaded)

    ctx_file = _cold_context_path(tmp_path, flow.flow_id)
    after = json.loads(ctx_file.read_bytes())
    assert after["context"]["resolved_type"] == "bugfix"


# -- mixed str/int keys must hash without raising ----------------------------


def test_canonical_json_handles_mixed_key_types():
    obj = {"a": 1, 2: "b", "nested": {3: "x", "y": 4}, "list": [{5: "q", "r": 6}]}
    # Fast path would raise TypeError on sort_keys; the fallback stringifies keys.
    encoded = _canonical_json(obj)
    assert isinstance(encoded, str)
    # Deterministic: same input hashes the same way across calls.
    assert _content_hash(obj) == _content_hash(dict(obj))


def test_save_flow_with_mixed_key_step_payload(tmp_path):
    pm = PersistenceManager(tmp_path)
    flow = _make_flow(n_steps=1, payload_size=10)
    victim = flow.state.step_history[0]
    flow.state.steps[victim].outputs = {"by_index": {0: "a", "1": "b", 2: "c"}}
    # Must not raise TypeError out of _content_hash / _split_flow.
    pm.save_flow(flow)

    loaded = PersistenceManager(tmp_path).load_flow_by_id(flow.flow_id)
    assert loaded is not None
    # JSON coerces int keys to str on write; the payload round-trips intact.
    assert loaded.state.steps[victim].outputs["by_index"]["1"] == "b"


# -- pending_calls_signature snapshots the shared set ------------------------


def test_pending_calls_signature_snapshots_roots(tmp_path):
    """Iterating a snapshot survives a concurrent root registration."""
    from tianluo.daemon.aggregator import DaemonAggregator

    agg = DaemonAggregator()
    for i in range(20):
        r = tmp_path / f"root{i}"
        (r / "tianluo" / "calls").mkdir(parents=True)
        agg.add_project_root(r)

    # The snapshot semantics the fix relies on: a list() copy is immune to the
    # live set growing while it is consumed (a plain set comprehension over the
    # live set would risk "Set changed size during iteration").
    live = agg._project_roots
    snapshot = [str(r) for r in list(live)]
    live.add(tmp_path / "late_root")
    assert len(snapshot) == 20  # snapshot unaffected by the later add

    # And the real method returns a signature without raising.
    sig = agg.pending_calls_signature()
    assert isinstance(sig, dict)


# -- END_SESSION handler is dispatched off the event loop --------------------


def test_end_session_handler_dispatched_off_loop():
    """The end-session handler runs via asyncio.to_thread, not inline.

    A synchronous handler that blocks (records the thread it runs on) must not
    execute on the event-loop thread — the fix wraps it in ``asyncio.to_thread``.
    """
    import threading

    from tianluo.daemon.client import DaemonClient

    seen = {}
    loop_thread = {}

    def _handler(flow_id, project_root, reason):
        seen["thread"] = threading.current_thread().name
        seen["args"] = (flow_id, project_root, reason)

    async def _drive():
        loop_thread["name"] = threading.current_thread().name
        client = DaemonClient.__new__(DaemonClient)
        client._end_session_handler = _handler
        # No reverse-resolution needed: project_root is supplied.
        await client._handle_end_session(
            {"flow_id": "f1", "project_root": "/tmp/p", "reason": "user"}
        )

    asyncio.run(_drive())
    assert seen["args"] == ("f1", "/tmp/p", "user")
    # Handler executed on a worker thread, not the event-loop thread.
    assert seen["thread"] != loop_thread["name"]
