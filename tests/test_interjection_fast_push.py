"""Tests for the G3 fast-push + interjection_event lifecycle plumbing.

Covers:

* ``DaemonAggregator.pending_calls_signature`` — kind-agnostic stat-based
  fingerprint of every ``se3/calls/`` file under each tracked project root.
* ``DaemonClient._handle_interject`` — sets the ``_fast_push_event`` after
  writing the interjection file so the push loop wakes immediately.
* ``DaemonClient._calls_changed`` — debounces calls-directory deltas off
  the calls-signature provider.
* ``InterjectionEventTracker.diff_machine`` — server-side STATUS_UPDATE diff
  that emits ``interjection_event`` payloads with ``phase ∈ {pending,
  consumed}`` exactly once per ``(machine_id, flow_id, call_id, phase)``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from se3.daemon import protocol
from se3.daemon.aggregator import DaemonAggregator
from se3.daemon.client import DaemonClient
from se3.server.ws import (
    INTERJECTION_PHASE_CONSUMED,
    INTERJECTION_PHASE_PENDING,
    UI_EVENT_INTERJECTION,
    InterjectionEventTracker,
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _write(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(protocol.decode(data))


def _make_client(**kw) -> DaemonClient:
    return DaemonClient(
        "ws://server",
        machine_id="m1",
        hostname="host",
        se3_version="6.4.0",
        snapshot_provider=kw.pop("snapshot_provider", lambda: {"machine_id": "m1"}),
        **kw,
    )


# --------------------------------------------------------------------------
# aggregator: pending_calls_signature
# --------------------------------------------------------------------------


def test_pending_calls_signature_empty_when_no_calls_dir(tmp_path):
    agg = DaemonAggregator()
    agg.add_project_root(tmp_path)
    assert agg.pending_calls_signature() == {}


def test_pending_calls_signature_lists_each_file_with_stat(tmp_path):
    calls = tmp_path / "se3" / "calls"
    _write(calls / "interjection_1.json", {"kind": "interjection", "text": "a"})
    _write(calls / "retry_2.json", {"kind": "retry_decision"})

    agg = DaemonAggregator()
    agg.add_project_root(tmp_path)
    sig = agg.pending_calls_signature()
    root_key = str(tmp_path.resolve())
    assert root_key in sig
    names = [entry[0] for entry in sig[root_key]]
    assert names == sorted(names)
    assert "interjection_1.json" in names
    assert "retry_2.json" in names
    # Each entry is (name, mtime, size); both mtime and size are populated.
    for name, mtime, size in sig[root_key]:
        assert mtime > 0.0
        assert size > 0


def test_pending_calls_signature_changes_when_file_added(tmp_path):
    calls = tmp_path / "se3" / "calls"
    calls.mkdir(parents=True)
    agg = DaemonAggregator()
    agg.add_project_root(tmp_path)
    before = agg.pending_calls_signature()
    _write(calls / "interjection_new.json", {"kind": "interjection"})
    after = agg.pending_calls_signature()
    assert before != after


def test_pending_calls_signature_changes_when_file_removed(tmp_path):
    calls = tmp_path / "se3" / "calls"
    target = calls / "interjection_x.json"
    _write(target, {"kind": "interjection"})

    agg = DaemonAggregator()
    agg.add_project_root(tmp_path)
    before = agg.pending_calls_signature()
    target.unlink()
    after = agg.pending_calls_signature()
    assert before != after


def test_pending_calls_signature_skips_hidden_files(tmp_path):
    calls = tmp_path / "se3" / "calls"
    _write(calls / ".tmp.swap", {"kind": "interjection"})
    _write(calls / "real.json", {"kind": "interjection"})

    agg = DaemonAggregator()
    agg.add_project_root(tmp_path)
    sig = agg.pending_calls_signature()
    root_key = str(tmp_path.resolve())
    names = [entry[0] for entry in sig[root_key]]
    assert names == ["real.json"]


# --------------------------------------------------------------------------
# client: _calls_changed + _handle_interject fast push
# --------------------------------------------------------------------------


class _FakeSignatureProvider:
    """Mutable stand-in for ``aggregator.pending_calls_signature``."""

    def __init__(self) -> None:
        self.signature: dict = {}

    def __call__(self) -> dict:
        return dict(self.signature)


def test_calls_changed_detects_signature_delta():
    provider = _FakeSignatureProvider()
    client = _make_client(calls_signature_provider=provider)

    provider.signature = {"/p": (("interjection_1.json", 1.0, 50),)}
    assert client._calls_changed() is True  # changed from initial {}
    assert client._calls_changed() is False  # debounced after observation

    provider.signature = {"/p": (("interjection_1.json", 1.0, 75),)}
    assert client._calls_changed() is True  # size grew -> change


def test_calls_changed_without_provider_is_false():
    client = _make_client()  # no calls_signature_provider
    assert client._calls_changed() is False


def test_calls_changed_provider_failure_forces_push(caplog):
    def _boom():
        raise RuntimeError("disk read failed")

    client = _make_client(calls_signature_provider=_boom)
    # A provider failure conservatively reports a change so the next push
    # still runs — losing one chip update due to a transient disk error is
    # worse than firing an extra status push.
    assert client._calls_changed() is True


def test_handle_interject_sets_fast_push_event(tmp_path):
    """Writing the interjection file flips ``_fast_push_event``."""
    client = _make_client()

    async def scenario():
        # Bind the event to *this* loop (normally done inside _session()).
        client._fast_push_event = asyncio.Event()
        await client._dispatch(
            _FakeWS(),
            protocol.make_interject_flow(
                "flow-3", "additional instruction", project_root=str(tmp_path)
            ),
        )
        return client._fast_push_event.is_set()

    assert asyncio.run(scenario()) is True
    # The interjection file actually landed on disk.
    assert list((tmp_path / "se3" / "calls").glob("interjection_*.json"))


def test_handle_interject_no_event_when_text_empty(tmp_path):
    client = _make_client()

    async def scenario():
        client._fast_push_event = asyncio.Event()
        await client._dispatch(
            _FakeWS(),
            protocol.make_interject_flow(
                "flow-3", "   ", project_root=str(tmp_path)
            ),
        )
        return client._fast_push_event.is_set()

    # Empty text never writes a file → never wakes the push loop.
    assert asyncio.run(scenario()) is False


def test_handle_interject_no_event_when_handler_raises(tmp_path, monkeypatch):
    """A failing interject_handler does not flip the fast-push event."""
    client = _make_client()

    def _boom(flow_id, project_root, text):
        raise RuntimeError("disk write failed")

    client._interject_handler = _boom

    async def scenario():
        client._fast_push_event = asyncio.Event()
        await client._dispatch(
            _FakeWS(),
            protocol.make_interject_flow(
                "flow-x", "do X", project_root=str(tmp_path)
            ),
        )
        return client._fast_push_event.is_set()

    assert asyncio.run(scenario()) is False


# --------------------------------------------------------------------------
# server: InterjectionEventTracker diff + phase emission
# --------------------------------------------------------------------------


def _snapshot_with_interjections(flow_id: str, call_ids: list) -> dict:
    return {
        "machine_id": "m1",
        "hostname": "h",
        "flows": [
            {
                "flow_id": flow_id,
                "pending_calls": [
                    {
                        "call_id": cid,
                        "kind": protocol.CALL_KIND_INTERJECTION,
                        "prompt": f"text-{cid}",
                    }
                    for cid in call_ids
                ],
            }
        ],
    }


def test_tracker_emits_pending_on_first_appearance():
    tracker = InterjectionEventTracker()
    events = tracker.diff_machine(
        "m1", _snapshot_with_interjections("f1", ["c1"])
    )
    assert len(events) == 1
    ev = events[0]
    assert ev["type"] == UI_EVENT_INTERJECTION
    assert ev["machine_id"] == "m1"
    assert ev["flow_id"] == "f1"
    assert ev["call_id"] == "c1"
    assert ev["phase"] == INTERJECTION_PHASE_PENDING
    assert ev["text"] == "text-c1"
    assert "ts" in ev


def test_tracker_emits_consumed_when_call_disappears():
    tracker = InterjectionEventTracker()
    tracker.diff_machine("m1", _snapshot_with_interjections("f1", ["c1"]))
    events = tracker.diff_machine(
        "m1", _snapshot_with_interjections("f1", [])
    )
    assert len(events) == 1
    assert events[0]["phase"] == INTERJECTION_PHASE_CONSUMED
    assert events[0]["call_id"] == "c1"


def test_tracker_does_not_double_emit_same_phase():
    tracker = InterjectionEventTracker()
    tracker.diff_machine("m1", _snapshot_with_interjections("f1", ["c1"]))
    # Same chip still present → no new pending event.
    events = tracker.diff_machine(
        "m1", _snapshot_with_interjections("f1", ["c1"])
    )
    assert events == []


def test_tracker_ignores_non_interjection_kinds():
    """Only ``interjection``-kind chips drive the event lifecycle."""
    tracker = InterjectionEventTracker()
    snap = {
        "machine_id": "m1",
        "flows": [
            {
                "flow_id": "f1",
                "pending_calls": [
                    {
                        "call_id": "r1",
                        "kind": protocol.CALL_KIND_RETRY_DECISION,
                        "prompt": "retry?",
                    },
                    {
                        "call_id": "c1",
                        "kind": protocol.CALL_KIND_CALL,
                        "prompt": "p",
                    },
                ],
            }
        ],
    }
    assert tracker.diff_machine("m1", snap) == []


def test_tracker_emits_consumed_when_flow_vanishes():
    """A flow that drops out of the snapshot drops its chips too."""
    tracker = InterjectionEventTracker()
    tracker.diff_machine("m1", _snapshot_with_interjections("f1", ["c1"]))
    # Next snapshot has no f1 at all (e.g. flow archived between ticks).
    events = tracker.diff_machine("m1", {"machine_id": "m1", "flows": []})
    assert len(events) == 1
    assert events[0]["flow_id"] == "f1"
    assert events[0]["phase"] == INTERJECTION_PHASE_CONSUMED


def test_tracker_reset_machine_replays_pending_after_reconnect():
    tracker = InterjectionEventTracker()
    tracker.diff_machine("m1", _snapshot_with_interjections("f1", ["c1"]))
    # Simulate a daemon disconnect; on next snapshot the still-pending chip
    # must replay as ``pending`` instead of being silently treated as known.
    tracker.reset_machine("m1")
    events = tracker.diff_machine(
        "m1", _snapshot_with_interjections("f1", ["c1"])
    )
    assert [ev["phase"] for ev in events] == [INTERJECTION_PHASE_PENDING]


def test_tracker_per_machine_state_is_isolated():
    """Two machines do not bleed into each other's diff state."""
    tracker = InterjectionEventTracker()
    tracker.diff_machine("m1", _snapshot_with_interjections("f1", ["c1"]))
    # m2 has never been seen — pending must fire even though m1 has c1.
    events = tracker.diff_machine(
        "m2", _snapshot_with_interjections("f1", ["c1"])
    )
    assert [ev["phase"] for ev in events] == [INTERJECTION_PHASE_PENDING]
    assert events[0]["machine_id"] == "m2"


# --------------------------------------------------------------------------
# end-to-end: STATUS_UPDATE -> /ws/ui receives interjection_event
# --------------------------------------------------------------------------


def test_status_update_broadcasts_interjection_event_to_ui_clients():
    """A STATUS_UPDATE that introduces an interjection chip drives a
    ``interjection_event`` payload on the live ``/ws/ui`` channel."""
    from fastapi.testclient import TestClient

    from se3.server.app import create_app

    app = create_app()
    with TestClient(app) as client:
        with client.websocket_connect("/ws/ui") as ui_ws:
            # The initial snapshot frame the server sends on UI connect.
            initial = json.loads(ui_ws.receive_text())
            assert initial["type"] == "snapshot"

            with client.websocket_connect("/ws") as daemon_ws:
                daemon_ws.send_text(
                    protocol.make_hello("m-e2e", "h", "6.4.0").to_json()
                )
                protocol.decode(daemon_ws.receive_text())  # WELCOME
                # STATUS_UPDATE: a new interjection-kind pending_call appears.
                snap = {
                    "machine_id": "m-e2e",
                    "hostname": "h",
                    "flows": [
                        {
                            "flow_id": "f-e2e",
                            "pending_calls": [
                                {
                                    "call_id": "i1",
                                    "kind": protocol.CALL_KIND_INTERJECTION,
                                    "prompt": "fix the typo",
                                }
                            ],
                        }
                    ],
                }
                daemon_ws.send_text(
                    protocol.make_status_update(snap).to_json()
                )

                # The first frame UI clients see is the cached status_update;
                # the interjection_event follows. Consume frames until we
                # observe the interjection_event (or run out of patience).
                event = None
                for _ in range(10):
                    frame = json.loads(ui_ws.receive_text())
                    if frame.get("type") == UI_EVENT_INTERJECTION:
                        event = frame
                        break
                assert event is not None, "interjection_event was never broadcast"
                assert event["machine_id"] == "m-e2e"
                assert event["flow_id"] == "f-e2e"
                assert event["call_id"] == "i1"
                assert event["phase"] == INTERJECTION_PHASE_PENDING
                assert event["text"] == "fix the typo"


def test_tracker_handles_malformed_snapshots_gracefully():
    tracker = InterjectionEventTracker()
    # Missing flows key, weird types — must not raise.
    assert tracker.diff_machine("m1", {}) == []
    assert tracker.diff_machine("m1", {"flows": None}) == []
    assert tracker.diff_machine("m1", {"flows": [None, "junk", {}]}) == []
    assert tracker.diff_machine(
        "m1",
        {
            "flows": [
                {
                    "flow_id": "f1",
                    "pending_calls": [
                        {"kind": protocol.CALL_KIND_INTERJECTION},  # no call_id
                        {"call_id": "x"},  # no kind
                        "garbage",
                    ],
                }
            ]
        },
    ) == []
