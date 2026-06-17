"""G2: daemon-side proof for the WebUI running-flow *freeze* regression.

The symptom is: when a ``se3 run`` flow confirms its discovery plan and moves to
``analyze`` (a discovery→analyze transition), or when a later step is manually
retried after an error, the WebUI live transcript freezes — the backend keeps
advancing but the browser shows nothing new until a full page reload.

These tests pin down the **daemon push side** of that path. They prove that the
daemon's change-detection (``active_flow_signature``) and incremental reader
(``read_active_flows`` / ``read_flow``) DO surface the new step jsonl and the
resent retry records as a non-empty ``append`` delta, and that a real
``DaemonClient`` driven by a real ``DaemonHistoryReader`` sees ``_history_changed``
fire on each of those disk events. In other words, the freeze is **not** caused
by the daemon swallowing the transition/retry delta — the daemon reports it
correctly, so the defect lives in the frontend incremental-consume path (owned by
group G1).

Conclusion recorded by this suite (G2 task 2): no defect was found in
``client.py``'s cursor retention or in ``history.py``'s incremental read for the
transition / retry-resend scenarios, so **no production code is changed** — these
are guard tests that would fail if a future change regressed the daemon push
side.
"""

from __future__ import annotations

import asyncio
import json

from se3.daemon import protocol
from se3.daemon.client import DaemonClient
from se3.daemon.history import DaemonHistoryReader
from se3.daemon.protocol import HISTORY_MODE_APPEND, HISTORY_MODE_FULL


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _write_jsonl(path, lines):
    """Write *lines* (list of dicts) as a jsonl file at *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )


def _append_jsonl(path, lines):
    """Append *lines* (list of dicts) to an existing jsonl file."""
    with path.open("a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")


def _msg(role, content, step_type="discovery"):
    return {"role": role, "content": content, "raw_json": [], "step_type": step_type}


def _make_reader(*roots):
    return DaemonHistoryReader(project_roots_provider=lambda: list(roots))


def _write_engine(root, flow_id, status):
    """Write a minimal active ``engine.json`` for *flow_id* with *status*."""
    state_dir = root / "se3" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "engine.json").write_text(
        json.dumps({"flow_id": flow_id, "status": status}), encoding="utf-8"
    )


def _hist(root, flow_id):
    return root / "se3" / "history" / flow_id


def _make_client(provider):
    """Build a DaemonClient wired to a *real* history provider (reader)."""
    return DaemonClient(
        "ws://server",
        machine_id="m1",
        hostname="host",
        se3_version="6.4.0",
        snapshot_provider=lambda: {"machine_id": "m1"},
        history_provider=provider,
    )


class _FakeWS:
    """Minimal WebSocket stand-in capturing what the client sends."""

    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(protocol.decode(data))


def _data_frames(ws):
    return [m for m in ws.sent if m.type == protocol.MSG_HISTORY_DATA]


# ==========================================================================
# Task 1 — discovery→analyze transition + PAUSED↔RUNNING status flip
# ==========================================================================


def test_signature_changes_when_analyze_step_file_appears(tmp_path):
    """Confirming discovery and entering analyze creates a NEW per-step jsonl;
    ``active_flow_signature`` must move so the daemon pushes within one cycle."""
    _write_engine(tmp_path, "live", "RUNNING")
    hist = _hist(tmp_path, "live")
    _write_jsonl(
        hist / "01_discovery_ab12.jsonl",
        [_msg("user", "explore"), _msg("assistant", '{"mode":"question"}')],
    )
    reader = _make_reader(tmp_path)

    before = reader.active_flow_signature()
    assert "live" in before

    # discovery→analyze: the analyze step's first jsonl lands.
    _write_jsonl(
        hist / "02_analyze_cd34.jsonl",
        [_msg("assistant", "analyzing", step_type="analyze")],
    )
    after = reader.active_flow_signature()
    assert after["live"] != before["live"], (
        "a brand-new analyze step file must move the signature forward"
    )


def test_signature_changes_on_paused_then_running_flip(tmp_path):
    """discovery pauses to await the user's plan confirmation (RUNNING→PAUSED),
    then resumes (PAUSED→RUNNING). Each engine.json status flip must change the
    signature so the daemon re-pushes around the transition."""
    _write_engine(tmp_path, "live", "RUNNING")
    _write_jsonl(
        _hist(tmp_path, "live") / "01_discovery_ab12.jsonl",
        [_msg("user", "explore")],
    )
    reader = _make_reader(tmp_path)
    running_sig = reader.active_flow_signature()

    # Pause to await the discovery-confirm answer.
    _write_engine(tmp_path, "live", "PAUSED")
    paused_sig = reader.active_flow_signature()
    assert paused_sig["live"] != running_sig["live"]

    # Resume after confirmation: flips back to RUNNING.
    _write_engine(tmp_path, "live", "RUNNING")
    resumed_sig = reader.active_flow_signature()
    assert resumed_sig["live"] != paused_sig["live"]


def test_read_active_flows_delivers_analyze_delta_after_transition(tmp_path):
    """The full discovery→confirm→analyze sequence: after the transition the
    incremental ``read_active_flows`` returns a NON-EMPTY append delta carrying
    the discovery-confirm reply and the analyze records — the daemon does not
    swallow the transition batch."""
    _write_engine(tmp_path, "live", "RUNNING")
    hist = _hist(tmp_path, "live")
    discovery = hist / "01_discovery_ab12.jsonl"
    _write_jsonl(
        discovery,
        [
            _msg("user", "explore the task"),
            _msg("assistant", '{"mode":"question","content":"which?"}'),
        ],
    )
    reader = _make_reader(tmp_path)

    # First push: full snapshot of the discovery turn.
    reads = reader.read_active_flows({})
    assert [r.flow_id for r in reads] == ["live"]
    assert reads[0].mode == HISTORY_MODE_FULL
    assert len(reads[0].records) == 2
    cursors = {r.flow_id: r.cursor for r in reads}

    # User confirms the plan (a new user reply appended under discovery), the
    # flow pauses then resumes, and the analyze step's file is created.
    _append_jsonl(discovery, [_msg("user", "1")])  # "按1确定"
    _write_engine(tmp_path, "live", "PAUSED")
    reads = reader.read_active_flows(cursors)
    cursors = {r.flow_id: r.cursor for r in reads}
    # The confirm reply is delivered even while paused (still an active flow).
    assert [r["message"]["content"] for r in reads[0].records] == ["1"]
    assert reads[0].mode == HISTORY_MODE_APPEND

    _write_engine(tmp_path, "live", "RUNNING")
    analyze = hist / "02_analyze_cd34.jsonl"
    _write_jsonl(
        analyze,
        [
            _msg("assistant", "analysis text", step_type="analyze"),
            {
                "type": "step_completed",
                "step_id": "02_analyze_cd34",
                "step_type": "analyze",
                "data": {"step": {"outputs": {"reasoning": "ok"}}},
            },
        ],
    )

    reads = reader.read_active_flows(cursors)
    cursors = {r.flow_id: r.cursor for r in reads}
    contents = [r.get("message", {}).get("content") for r in reads[0].records]
    types = [r["message"].get("type") for r in reads[0].records]
    # The analyze delta is non-empty: the assistant body and the terminal card.
    assert "analysis text" in contents
    assert "step_completed" in types
    assert reads[0].cursor["02_analyze_cd34.jsonl"] == 2

    # Idle poll afterwards: nothing new, no spurious re-push.
    reads = reader.read_active_flows(cursors)
    assert reads[0].records == []


def test_per_file_cursor_first_full_then_append_no_loss_no_dup(tmp_path):
    """Per-file byte cursor: a new file's first read is ``full`` while existing
    files continue as ``append``; the union of every delta equals one full read
    with no loss and no duplicate."""
    _write_engine(tmp_path, "live", "RUNNING")
    hist = _hist(tmp_path, "live")
    discovery = hist / "01_discovery_ab12.jsonl"
    _write_jsonl(discovery, [_msg("user", "d0"), _msg("assistant", "d1")])
    reader = _make_reader(tmp_path)

    collected: list = []
    cursors: dict = {}

    # Round 1 — first read (mode full).
    reads = reader.read_active_flows(cursors)
    assert reads[0].mode == HISTORY_MODE_FULL
    cursors = {r.flow_id: r.cursor for r in reads}
    collected += [r["message"]["content"] for r in reads[0].records]
    assert "02_analyze_cd34.jsonl" not in cursors["live"]

    # Round 2 — discovery appends + the analyze file appears for the first time.
    _append_jsonl(discovery, [_msg("user", "d2")])
    analyze = hist / "02_analyze_cd34.jsonl"
    _write_jsonl(analyze, [_msg("assistant", "an0", step_type="analyze")])
    reads = reader.read_active_flows(cursors)
    # The whole read is an append delta (the flow already had a cursor), and the
    # brand-new analyze file is included in full within it.
    assert reads[0].mode == HISTORY_MODE_APPEND
    cursors = {r.flow_id: r.cursor for r in reads}
    collected += [r["message"]["content"] for r in reads[0].records]
    assert cursors["live"]["01_discovery_ab12.jsonl"] == 3
    assert cursors["live"]["02_analyze_cd34.jsonl"] == 1

    # Round 3 — both files append again.
    _append_jsonl(discovery, [_msg("assistant", "d3")])
    _append_jsonl(analyze, [_msg("user", "an1", step_type="analyze")])
    reads = reader.read_active_flows(cursors)
    cursors = {r.flow_id: r.cursor for r in reads}
    collected += [r["message"]["content"] for r in reads[0].records]

    # Round 4 — nothing new.
    reads = reader.read_active_flows(cursors)
    assert reads[0].records == []

    # A single full read of the whole flow must carry exactly the same records.
    full = reader.read_flow("live")
    full_contents = [r["message"]["content"] for r in full.records]
    assert sorted(collected) == sorted(full_contents)
    assert len(collected) == len(set(collected)), "no record delivered twice"


def test_history_changed_fires_on_discovery_to_analyze_transition(tmp_path):
    """A real DaemonClient driven by a real reader: ``_history_changed`` returns
    True on the analyze-file appearance and on the status flips, so the push loop
    actually triggers a history push across the transition."""
    _write_engine(tmp_path, "live", "RUNNING")
    hist = _hist(tmp_path, "live")
    _write_jsonl(hist / "01_discovery_ab12.jsonl", [_msg("user", "explore")])
    reader = _make_reader(tmp_path)
    client = _make_client(reader)

    # Prime the baseline signature.
    client._history_changed()
    # Unchanged tree -> no spurious push.
    assert client._history_changed() is False

    # Pause for confirmation.
    _write_engine(tmp_path, "live", "PAUSED")
    assert client._history_changed() is True

    # Resume + analyze file appears.
    _write_engine(tmp_path, "live", "RUNNING")
    _write_jsonl(
        hist / "02_analyze_cd34.jsonl",
        [_msg("assistant", "analyzing", step_type="analyze")],
    )
    assert client._history_changed() is True
    # Settled again.
    assert client._history_changed() is False


# ==========================================================================
# Task 2 — retry-after-error: resent records arrive as a delta
# ==========================================================================


def test_retry_resend_same_step_jsonl_records_arrive_as_delta(tmp_path):
    """A step (e.g. update_spec) fails, the user retries, and the retry re-runs
    the SAME step — appending fresh (similar) records to the same step jsonl.
    Those resent records must surface as a non-empty append delta, not be
    swallowed because they resemble the failed attempt."""
    _write_engine(tmp_path, "live", "RUNNING")
    step = _hist(tmp_path, "live") / "07_update_spec_ef56.jsonl"
    _write_jsonl(
        step,
        [
            _msg("assistant", "attempt 1 body", step_type="update_spec"),
            {
                "type": "step_failed",
                "step_id": "07_update_spec_ef56",
                "step_type": "update_spec",
                "data": {"step": {"outputs": {"error": "boom"}}},
            },
        ],
    )
    reader = _make_reader(tmp_path)

    reads = reader.read_active_flows({})
    cursors = {r.flow_id: r.cursor for r in reads}
    assert any(r["message"].get("type") == "step_failed" for r in reads[0].records)

    # User picks "retry": the step re-runs and appends a second attempt whose
    # body is *similar* to the first.
    _append_jsonl(
        step,
        [
            _msg("assistant", "attempt 1 body", step_type="update_spec"),  # resend
            {
                "type": "step_completed",
                "step_id": "07_update_spec_ef56",
                "step_type": "update_spec",
                "data": {"step": {"outputs": {"updated_specs": []}}},
            },
        ],
    )

    reads = reader.read_active_flows(cursors)
    cursors = {r.flow_id: r.cursor for r in reads}
    # The retry batch is delivered in full despite the duplicate-looking body.
    assert len(reads[0].records) == 2
    bodies = [r.get("message", {}).get("content") for r in reads[0].records]
    types = [r["message"].get("type") for r in reads[0].records]
    assert "attempt 1 body" in bodies
    assert "step_completed" in types
    assert reads[0].cursor["07_update_spec_ef56.jsonl"] == 4

    # No re-push afterwards.
    reads = reader.read_active_flows(cursors)
    assert reads[0].records == []


def test_retry_new_attempt_file_picked_up_as_delta(tmp_path):
    """When a retry writes a brand-new attempt jsonl (rather than appending to
    the failed step's file), the new file is still picked up incrementally."""
    _write_engine(tmp_path, "live", "RUNNING")
    hist = _hist(tmp_path, "live")
    _write_jsonl(
        hist / "07_update_spec_ef56.jsonl",
        [
            {
                "type": "step_failed",
                "step_id": "07_update_spec_ef56",
                "step_type": "update_spec",
                "data": {"step": {"outputs": {"error": "boom"}}},
            }
        ],
    )
    reader = _make_reader(tmp_path)
    reads = reader.read_active_flows({})
    cursors = {r.flow_id: r.cursor for r in reads}

    # Retry as a fresh attempt file.
    _write_jsonl(
        hist / "08_update_spec_ab99.jsonl",
        [_msg("assistant", "retry body", step_type="update_spec")],
    )
    reads = reader.read_active_flows(cursors)
    cursors = {r.flow_id: r.cursor for r in reads}
    assert [r["message"].get("content") for r in reads[0].records] == ["retry body"]
    assert reads[0].cursor["08_update_spec_ab99.jsonl"] == 1


def test_history_changed_fires_on_retry_resend(tmp_path):
    """``_history_changed`` (driving the push loop) fires when a retry appends to
    a step's jsonl — the byte-size component of the signature moves even if the
    resent body is byte-identical to a prior line and the mtime is coarse."""
    _write_engine(tmp_path, "live", "RUNNING")
    step = _hist(tmp_path, "live") / "07_update_spec_ef56.jsonl"
    _write_jsonl(step, [_msg("assistant", "attempt 1", step_type="update_spec")])
    reader = _make_reader(tmp_path)
    client = _make_client(reader)

    client._history_changed()  # prime baseline
    assert client._history_changed() is False

    # Retry resends a similar record.
    _append_jsonl(step, [_msg("assistant", "attempt 1", step_type="update_spec")])
    assert client._history_changed() is True


def test_push_history_resend_is_append_not_full_reread(tmp_path):
    """End-to-end through DaemonClient._push_history with a real reader: after a
    retry resend the daemon ships an APPEND frame carrying only the new records
    (no duplicate re-read of the failed attempt's lines)."""
    _write_engine(tmp_path, "live", "RUNNING")
    step = _hist(tmp_path, "live") / "07_update_spec_ef56.jsonl"
    _write_jsonl(step, [_msg("assistant", "attempt 1", step_type="update_spec")])
    reader = _make_reader(tmp_path)
    client = _make_client(reader)
    ws = _FakeWS()

    # Round 1: full snapshot of the failed attempt.
    asyncio.run(client._push_history(ws))
    frames = _data_frames(ws)
    assert len(frames) == 1
    assert frames[0].payload["mode"] == HISTORY_MODE_FULL
    assert client._history_cursors["live"]["07_update_spec_ef56.jsonl"] == 1

    # Round 2: retry appends one new record.
    _append_jsonl(step, [_msg("assistant", "attempt 2", step_type="update_spec")])
    asyncio.run(client._push_history(ws))
    frames = _data_frames(ws)
    assert len(frames) == 2
    delta = frames[1].payload
    assert delta["mode"] == HISTORY_MODE_APPEND
    # Exactly the one new record, no re-delivery of attempt 1.
    assert len(delta["records"]) == 1
    assert delta["records"][0]["message"]["content"] == "attempt 2"
    assert delta["cursor"]["07_update_spec_ef56.jsonl"] == 2
