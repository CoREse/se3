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

The original conclusion of this suite was that no defect lived in ``client.py``'s
cursor retention or ``history.py``'s incremental read — the push side reports the
transition correctly. The #260 follow-up (session ``20260705-122709_377bfbb7``)
then located the real daemon-side hazard one layer down: ``disk_json_cache``
served a STALE parse for the live engine.json under a same-``(mtime, size)``
middle rewrite in the dense discovery→analyze window, which could transiently
drop a still-active flow out of ``read_active_flows``. The final section below
locks the two G2 fixes for that: the live-engine cache now hashes the WHOLE
content (so a middle rewrite re-parses), and ``_is_still_active`` re-confirms an
ambiguous drop with a forced fresh read before excluding a flow.
"""

from __future__ import annotations

import asyncio
import json

import se3.daemon.disk_json_cache as disk_cache
import se3.daemon.history as history_mod
from se3.daemon import protocol
from se3.daemon.client import DaemonClient
from se3.daemon.history import DaemonHistoryReader, SessionMeta
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


def test_retry_truncate_replace_same_step_jsonl_delivers_new_records(tmp_path):
    """A FAILED step retried *in place* rewrites (truncates/replaces) the same
    step jsonl with a fresh, SHORTER batch.  The stale line-count cursor refers
    to the old (longer) file; ``read_flow`` must NOT honour it as the read start
    against the replacement — otherwise every replacement line whose index is
    below the stale cursor is skipped, an empty append is recorded, and the live
    conversation loses the retry batch (issue #209).
    """
    _write_engine(tmp_path, "live", "RUNNING")
    step = _hist(tmp_path, "live") / "07_update_spec_ef56.jsonl"
    # Old attempt: 5 records — cursor advances to 5.
    _write_jsonl(
        step,
        [_msg("assistant", f"old line {i}", step_type="update_spec") for i in range(5)],
    )
    reader = _make_reader(tmp_path)

    reads = reader.read_active_flows({})
    cursors = {r.flow_id: r.cursor for r in reads}
    assert cursors["live"]["07_update_spec_ef56.jsonl"] == 5

    # Retry replaces the whole file with a fresh, SHORTER batch (3 records).
    # ``_write_jsonl`` truncates + rewrites, so the file shrinks below the
    # recorded byte offset and below the old line-count cursor (5 > 3).
    _write_jsonl(
        step,
        [
            _msg("assistant", "retry r0", step_type="update_spec"),
            _msg("assistant", "retry r1", step_type="update_spec"),
            {
                "type": "step_completed",
                "step_id": "07_update_spec_ef56",
                "step_type": "update_spec",
                "data": {"step": {"outputs": {"updated_specs": []}}},
            },
        ],
    )

    reads = reader.read_active_flows(cursors)
    # All 3 replacement records must be delivered, not skipped by start=5.
    assert len(reads[0].records) == 3
    bodies = [r.get("message", {}).get("content") for r in reads[0].records]
    types = [r["message"].get("type") for r in reads[0].records]
    assert bodies[:2] == ["retry r0", "retry r1"]
    assert "step_completed" in types
    # Cursor re-anchors to the replacement file's line count.
    assert reads[0].cursor["07_update_spec_ef56.jsonl"] == 3

    # No re-push afterwards (no duplicate delivery).
    cursors = {r.flow_id: r.cursor for r in reads}
    reads = reader.read_active_flows(cursors)
    assert reads[0].records == []


def test_retry_rewrite_equal_size_delivers_new_records(tmp_path):
    """A FAILED step retried *in place* rewrites the same step jsonl with a fresh
    batch whose byte size is the SAME as the old consumed byte offset (the file
    did NOT shrink).  A size/offset comparison alone cannot see this rewrite, so
    a byte-offset reader would record *no new bytes* (``cur_size == prev_offset``)
    and silently skip the whole replacement — the WebUI would miss the retry
    batch until a full reload (issue #209, second trigger).  ``read_flow`` must
    detect the rewrite via the head fingerprint, discard the stale offset/cursor,
    and deliver the replacement content from the beginning.
    """
    _write_engine(tmp_path, "live", "RUNNING")
    step = _hist(tmp_path, "live") / "07_update_spec_ef56.jsonl"
    # Old attempt: 3 records, each content a fixed 5-char string.
    _write_jsonl(
        step,
        [_msg("assistant", f"old-{i}", step_type="update_spec") for i in range(3)],
    )
    reader = _make_reader(tmp_path)

    reads = reader.read_active_flows({})
    cursors = {r.flow_id: r.cursor for r in reads}
    assert cursors["live"]["07_update_spec_ef56.jsonl"] == 3
    old_size = step.stat().st_size

    # Retry rewrites the file with 3 *different* records of the SAME length, so
    # the replacement is byte-for-byte the same SIZE as the old file (== the old
    # consumed offset) but different CONTENT.
    _write_jsonl(
        step,
        [_msg("assistant", f"new-{i}", step_type="update_spec") for i in range(3)],
    )
    assert step.stat().st_size == old_size, "test setup: sizes must match exactly"

    reads = reader.read_active_flows(cursors)
    cursors = {r.flow_id: r.cursor for r in reads}
    bodies = [r.get("message", {}).get("content") for r in reads[0].records]
    # All 3 replacement records delivered from the start — not skipped as
    # "no new bytes".
    assert bodies == ["new-0", "new-1", "new-2"]
    assert reads[0].cursor["07_update_spec_ef56.jsonl"] == 3

    # No duplicate delivery on the next idle poll.
    reads = reader.read_active_flows(cursors)
    assert reads[0].records == []


def test_retry_rewrite_equal_size_preserved_boundary_delivers_new_records(tmp_path):
    """A FAILED step retried *in place* rewrites the same step jsonl with a fresh
    batch whose byte size is the SAME as the old consumed offset AND whose trailing
    record (the last bytes before the old offset — the terminal / status line) is
    left byte-for-byte identical, while only an *earlier* record changes.

    This is the exact case a boundary-only rewrite detector (hashing just the last
    N bytes before the offset) cannot see: the boundary fingerprint stays equal,
    ``rewritten`` would be left false, the ``cur_size == prev_offset`` early return
    fires, and the whole replacement batch is silently skipped as "no new bytes" —
    so the WebUI receives no retry batch until a full reload (issue #209, fix
    iteration 5).  ``read_flow`` must fingerprint the WHOLE consumed prefix, detect
    that an earlier record changed, discard the stale offset/cursor, and deliver
    the replacement content from the beginning.
    """
    _write_engine(tmp_path, "live", "RUNNING")
    step = _hist(tmp_path, "live") / "07_update_spec_ef56.jsonl"
    # A long, fixed terminal record (>128 bytes serialized) that the retry leaves
    # untouched, so the bytes near the old offset are identical across the rewrite.
    terminal = _msg("assistant", "T" * 256, step_type="update_spec")
    _write_jsonl(
        step,
        [_msg("assistant", "old-0", step_type="update_spec"), dict(terminal)],
    )
    reader = _make_reader(tmp_path)

    reads = reader.read_active_flows({})
    cursors = {r.flow_id: r.cursor for r in reads}
    assert cursors["live"]["07_update_spec_ef56.jsonl"] == 2
    old_size = step.stat().st_size

    # Retry rewrites the file changing ONLY the earlier record (same length, so the
    # total byte size is unchanged) while keeping the terminal record identical.
    _write_jsonl(
        step,
        [_msg("assistant", "new-0", step_type="update_spec"), dict(terminal)],
    )
    assert step.stat().st_size == old_size, "test setup: sizes must match exactly"

    reads = reader.read_active_flows(cursors)
    cursors = {r.flow_id: r.cursor for r in reads}
    bodies = [r.get("message", {}).get("content") for r in reads[0].records]
    # Both replacement records delivered from the start — the changed earlier
    # record is not masked by the unchanged boundary tail.
    assert bodies == ["new-0", "T" * 256]
    assert reads[0].cursor["07_update_spec_ef56.jsonl"] == 2

    # No duplicate delivery on the next idle poll.
    reads = reader.read_active_flows(cursors)
    assert reads[0].records == []


def test_retry_rewrite_equal_size_preserved_head_delivers_new_records(tmp_path):
    """A FAILED step retried *in place* rewrites the same step jsonl with a fresh
    batch whose byte size is the SAME as the old consumed offset AND whose
    *leading* record (the first bytes of the file — a stable prompt / status
    prefix) is left byte-for-byte identical, while only a *later* record within
    the consumed prefix changes.

    This is the exact case a *head*-only rewrite detector (hashing just the first
    ``HEAD_SIGNATURE_BYTES`` bytes anchored at byte 0) cannot see: the head
    fingerprint stays equal, ``rewritten`` would be left false, the
    ``cur_size == prev_offset`` early return fires, and the whole replacement
    batch is silently skipped as "no new bytes" — so the WebUI receives no retry
    batch until a full reload (issue #209, fix iteration 6).  ``read_flow`` must
    fingerprint the WHOLE consumed prefix, detect that a later record changed,
    discard the stale offset/cursor, and deliver the replacement content from the
    beginning.
    """
    _write_engine(tmp_path, "live", "RUNNING")
    step = _hist(tmp_path, "live") / "07_update_spec_ef56.jsonl"
    # A long, fixed LEADING record (>128 bytes serialized, so it spans well past
    # any small head window) that the retry leaves untouched, so the first bytes
    # of the file are identical across the rewrite.
    head = _msg("assistant", "H" * 256, step_type="update_spec")
    _write_jsonl(
        step,
        [dict(head), _msg("assistant", "old-tail", step_type="update_spec")],
    )
    reader = _make_reader(tmp_path)

    reads = reader.read_active_flows({})
    cursors = {r.flow_id: r.cursor for r in reads}
    assert cursors["live"]["07_update_spec_ef56.jsonl"] == 2
    old_size = step.stat().st_size

    # Retry rewrites the file changing ONLY the later record (same length, so the
    # total byte size is unchanged) while keeping the leading record identical.
    _write_jsonl(
        step,
        [dict(head), _msg("assistant", "new-tail", step_type="update_spec")],
    )
    assert step.stat().st_size == old_size, "test setup: sizes must match exactly"

    reads = reader.read_active_flows(cursors)
    cursors = {r.flow_id: r.cursor for r in reads}
    bodies = [r.get("message", {}).get("content") for r in reads[0].records]
    # Both replacement records delivered from the start — the changed later
    # record is not masked by the unchanged leading head.
    assert bodies == ["H" * 256, "new-tail"]
    assert reads[0].cursor["07_update_spec_ef56.jsonl"] == 2

    # No duplicate delivery on the next idle poll.
    reads = reader.read_active_flows(cursors)
    assert reads[0].records == []


def test_retry_rewrite_larger_size_delivers_full_replacement(tmp_path):
    """A FAILED step retried *in place* rewrites the same step jsonl with a fresh
    batch whose byte size is LARGER than the old consumed byte offset.  Because
    the file did not shrink, a byte-offset reader would treat it as an append and
    seek to the STALE offset, reading only the suffix of the new file (a broken,
    partial slice) instead of the whole replacement.  ``read_flow`` must detect
    the rewrite and deliver the full replacement content from the beginning.
    """
    _write_engine(tmp_path, "live", "RUNNING")
    step = _hist(tmp_path, "live") / "07_update_spec_ef56.jsonl"
    # Old attempt: 3 short records.
    _write_jsonl(
        step,
        [_msg("assistant", f"old line {i}", step_type="update_spec") for i in range(3)],
    )
    reader = _make_reader(tmp_path)

    reads = reader.read_active_flows({})
    cursors = {r.flow_id: r.cursor for r in reads}
    old_size = step.stat().st_size
    assert cursors["live"]["07_update_spec_ef56.jsonl"] == 3

    # Retry rewrites with a LONGER, larger batch (4 records, longer bodies +
    # a terminal report) so the new file is strictly bigger than the old offset.
    _write_jsonl(
        step,
        [
            _msg("assistant", "retry body number zero is long", step_type="update_spec"),
            _msg("assistant", "retry body number one is long", step_type="update_spec"),
            _msg("assistant", "retry body number two is long", step_type="update_spec"),
            {
                "type": "step_completed",
                "step_id": "07_update_spec_ef56",
                "step_type": "update_spec",
                "data": {"step": {"outputs": {"updated_specs": []}}},
            },
        ],
    )
    assert step.stat().st_size > old_size, "test setup: replacement must be larger"

    reads = reader.read_active_flows(cursors)
    cursors = {r.flow_id: r.cursor for r in reads}
    bodies = [r.get("message", {}).get("content") for r in reads[0].records]
    types = [r["message"].get("type") for r in reads[0].records]
    # Full replacement delivered from the start, not a truncated suffix.
    assert bodies[:3] == [
        "retry body number zero is long",
        "retry body number one is long",
        "retry body number two is long",
    ]
    assert "step_completed" in types
    assert reads[0].cursor["07_update_spec_ef56.jsonl"] == 4

    # No duplicate delivery afterwards.
    reads = reader.read_active_flows(cursors)
    assert reads[0].records == []


def test_retry_rewrite_larger_size_preserved_head_delivers_full_replacement(tmp_path):
    """A FAILED step retried *in place* rewrites the same step jsonl with a fresh
    batch that is LARGER than the old consumed byte offset AND whose *leading*
    record (the first bytes of the file — a stable prompt / status prefix that
    spans well past the bounded head window) is left byte-for-byte identical,
    while the rest of the batch is fresh.

    This is the exact case a *head*-only grow-case rewrite detector cannot see:
    the file grew (``cur_size > prev_offset``) so the equal-size whole-prefix
    check is never reached, and the bounded head fingerprint is unchanged, so a
    head-only check would leave ``rewritten`` false, trust the stale offset, seek
    past it, and deliver only the SUFFIX of the replacement — dropping the head
    record and the first fresh record(s) until a full reload (issue #209, fix
    iteration 7).  ``read_flow`` must, when the head is preserved on a grow,
    fall through to fingerprinting the WHOLE consumed prefix, detect that a later
    consumed record changed, discard the stale offset/cursor, and deliver the
    full replacement content from the beginning.
    """
    _write_engine(tmp_path, "live", "RUNNING")
    step = _hist(tmp_path, "live") / "07_update_spec_ef56.jsonl"
    # A long, fixed LEADING record (>128 bytes serialized, well past the bounded
    # head window) the retry leaves untouched, so the file head is identical
    # across the rewrite.
    head = _msg("assistant", "H" * 256, step_type="update_spec")
    _write_jsonl(
        step,
        [dict(head), _msg("assistant", "old-tail", step_type="update_spec")],
    )
    reader = _make_reader(tmp_path)

    reads = reader.read_active_flows({})
    cursors = {r.flow_id: r.cursor for r in reads}
    assert cursors["live"]["07_update_spec_ef56.jsonl"] == 2
    old_size = step.stat().st_size

    # Retry rewrites the file keeping the leading head record identical but
    # producing MORE, fresh records, so the replacement is strictly LARGER than
    # the old consumed offset while the head window is unchanged.
    _write_jsonl(
        step,
        [
            dict(head),
            _msg("assistant", "new-tail-one", step_type="update_spec"),
            _msg("assistant", "new-tail-two", step_type="update_spec"),
            {
                "type": "step_completed",
                "step_id": "07_update_spec_ef56",
                "step_type": "update_spec",
                "data": {"step": {"outputs": {"updated_specs": []}}},
            },
        ],
    )
    assert step.stat().st_size > old_size, "test setup: replacement must be larger"

    reads = reader.read_active_flows(cursors)
    cursors = {r.flow_id: r.cursor for r in reads}
    bodies = [r.get("message", {}).get("content") for r in reads[0].records]
    types = [r["message"].get("type") for r in reads[0].records]
    # Full replacement delivered from the start — the unchanged leading head does
    # NOT mask the rewrite, and the suffix-only slice (dropping the head record
    # and "new-tail-one") does not happen.
    assert bodies[:3] == ["H" * 256, "new-tail-one", "new-tail-two"]
    assert "step_completed" in types
    assert reads[0].cursor["07_update_spec_ef56.jsonl"] == 4

    # No duplicate delivery on the next idle poll.
    reads = reader.read_active_flows(cursors)
    assert reads[0].records == []


def test_retry_rewrite_larger_size_preserved_head_and_boundary_delivers_full_replacement(
    tmp_path,
):
    """A FAILED step retried *in place* rewrites the same step jsonl with a fresh
    batch that GREW past the old consumed byte offset AND preserved BOTH bounded
    windows of the consumed prefix — the leading prompt/status record (the head
    window ``[0, W)``) and the last consumed record ending at the old offset (the
    boundary window ``[offset - W, offset)``) — while changing a record in the
    MIDDLE of the consumed prefix and appending additional records.

    This is the exact case TWO bounded sampled windows cannot see: together they
    cover only a consumed prefix up to ``2·W`` bytes, so a middle record between a
    >W-byte head and a >W-byte boundary lies outside both windows.  A
    bounded-window-only grow-case detector would find both windows unchanged,
    leave ``rewritten`` false, trust the stale offset, seek past it, and deliver
    only the appended SUFFIX — dropping the changed middle record and the start of
    the retry batch until a full reload (issue #209, fix iteration 8).
    ``read_flow`` must fingerprint the WHOLE consumed prefix, detect the
    middle-of-prefix change, discard the stale offset/cursor, and deliver the full
    replacement content from the beginning.
    """
    _write_engine(tmp_path, "live", "RUNNING")
    step = _hist(tmp_path, "live") / "07_update_spec_ef56.jsonl"
    # A >W-byte leading head record and a >W-byte trailing boundary record that
    # the retry leaves byte-for-byte identical, with a small record between them
    # that the retry changes (kept the SAME length so the head/boundary bytes stay
    # at identical positions and both bounded windows compare equal).
    head = _msg("assistant", "H" * 256, step_type="update_spec")
    boundary = _msg("assistant", "B" * 256, step_type="update_spec")
    _write_jsonl(
        step,
        [
            dict(head),
            _msg("assistant", "mid-A", step_type="update_spec"),
            dict(boundary),
        ],
    )
    reader = _make_reader(tmp_path)

    reads = reader.read_active_flows({})
    cursors = {r.flow_id: r.cursor for r in reads}
    assert cursors["live"]["07_update_spec_ef56.jsonl"] == 3
    old_size = step.stat().st_size

    # Retry rewrites the file: head and boundary records identical (and at the
    # same byte positions because the middle record kept its length), only the
    # MIDDLE record's content changes, and extra records are appended so the file
    # grows strictly past the old consumed offset.
    _write_jsonl(
        step,
        [
            dict(head),
            _msg("assistant", "mid-B", step_type="update_spec"),
            dict(boundary),
            _msg("assistant", "extra one is fresh", step_type="update_spec"),
            {
                "type": "step_completed",
                "step_id": "07_update_spec_ef56",
                "step_type": "update_spec",
                "data": {"step": {"outputs": {"updated_specs": []}}},
            },
        ],
    )
    assert step.stat().st_size > old_size, "test setup: replacement must be larger"

    reads = reader.read_active_flows(cursors)
    cursors = {r.flow_id: r.cursor for r in reads}
    bodies = [r.get("message", {}).get("content") for r in reads[0].records]
    types = [r["message"].get("type") for r in reads[0].records]
    # Full replacement delivered from the start — the unchanged head AND boundary
    # windows do NOT mask the middle-of-prefix change, and the suffix-only slice
    # (dropping the head, the changed middle "mid-B", and the boundary) does not
    # happen.
    assert bodies[:4] == ["H" * 256, "mid-B", "B" * 256, "extra one is fresh"]
    assert "step_completed" in types
    assert reads[0].cursor["07_update_spec_ef56.jsonl"] == 5

    # No duplicate delivery on the next idle poll.
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


# ==========================================================================
# Task 2 — active-liveness stability across the PAUSED→RUNNING flip and the
# true-value fallback that rescues a flow from a transient cache-based drop.
# ==========================================================================


def _write_engine_status(root, flow_id, status):
    """Write a realistic active engine.json with a steps table + a status."""
    state_dir = root / "se3" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "engine.json").write_text(
        json.dumps(
            {
                "flow_id": flow_id,
                "status": status,
                "task_description": "td",
                "state": {"steps": {}, "current_step_index": 0},
                "is_worktree_mode": False,
            }
        ),
        encoding="utf-8",
    )


def test_is_still_active_stable_across_paused_running_flip(tmp_path):
    """A discovery flow that pauses for its confirm gate and resumes must stay in
    the active set the whole time — the PAUSED→RUNNING flip never drops it.

    Both PAUSED and RUNNING are non-terminal, so ``_is_still_active`` must return
    True across the flip; a spurious drop here is exactly the #260 freeze (the
    flow leaves the active set, so no incremental read/push happens for it).
    """
    disk_cache.clear_cache()
    _write_engine_status(tmp_path, "live", "RUNNING")
    _write_jsonl(_hist(tmp_path, "live") / "01_discovery_ab12.jsonl",
                 [_msg("user", "explore")])
    reader = _make_reader(tmp_path)

    def _meta():
        reader.invalidate_index_cache()
        return next(m for m in reader.build_index() if m.flow_id == "live")

    assert reader._is_still_active(_meta()) is True  # RUNNING
    _write_engine_status(tmp_path, "live", "PAUSED")
    assert reader._is_still_active(_meta()) is True  # PAUSED — still active
    _write_engine_status(tmp_path, "live", "RUNNING")
    assert reader._is_still_active(_meta()) is True  # resumed


def test_header_keeps_flow_active_return_values():
    """The three-way liveness classifier distinguishes keep / terminal / ambiguous.

    ``True`` (live-and-mine), ``False`` (present-but-terminal — a clean drop that
    needs no re-confirm) and ``None`` (unreadable / flow_id mismatch — an
    *ambiguous* drop the caller must re-confirm with a forced fresh read).
    """
    meta = SessionMeta(flow_id="live", project_root="/p", active=True, source="active")
    keep = DaemonHistoryReader._header_keeps_flow_active
    assert keep(meta, {"flow_id": "live", "status": "RUNNING"}) is True
    assert keep(meta, {"flow_id": "live", "status": "PAUSED"}) is True
    assert keep(meta, {"flow_id": "live", "status": "COMPLETED"}) is False
    assert keep(meta, {"flow_id": "live", "status": "FAILED"}) is False
    # Ambiguous: unreadable, or a different flow_id (possible stale/collided read).
    assert keep(meta, None) is None
    assert keep(meta, {"flow_id": "other", "status": "RUNNING"}) is None


def test_is_still_active_rescues_flow_on_transient_cache_drop(monkeypatch):
    """A cache-based read that would DROP a still-live flow is re-confirmed fresh.

    Simulates the exact hazard the true-value fallback guards: the cached read
    returns a mismatched flow_id (as a same-``(mtime, size)`` collision could),
    which alone would exclude the flow from the active set and freeze its live WS
    stream. The forced fresh read returns disk truth, so the flow is RESCUED.
    """
    meta = SessionMeta(flow_id="live", project_root="/p", active=True, source="active")
    calls = {"cached": 0, "fresh": 0}

    def fake_read(path, *, active=False, force_fresh=False, parse=None):
        if force_fresh:
            calls["fresh"] += 1
            return {"flow_id": "live", "status": "RUNNING"}   # disk truth
        calls["cached"] += 1
        return {"flow_id": "other", "status": "RUNNING"}      # stale/collided

    monkeypatch.setattr(history_mod, "read_engine_header", fake_read)
    assert DaemonHistoryReader._is_still_active(meta) is True
    assert calls["cached"] == 1 and calls["fresh"] == 1, (
        "the drop path must pay exactly one forced-fresh re-confirm"
    )


def test_is_still_active_genuine_terminal_drop_pays_no_extra_read(monkeypatch):
    """A cleanly terminal flow drops WITHOUT the forced fresh read.

    Distinguishing a definite terminal ``False`` from an ambiguous ``None`` is
    what keeps the extra fresh read off the healthy terminal transition: a flow
    whose live engine.json says COMPLETED is dropped on the cached read alone.
    """
    meta = SessionMeta(flow_id="live", project_root="/p", active=True, source="active")
    calls = {"cached": 0, "fresh": 0}

    def fake_read(path, *, active=False, force_fresh=False, parse=None):
        if force_fresh:
            calls["fresh"] += 1
        else:
            calls["cached"] += 1
        return {"flow_id": "live", "status": "COMPLETED"}

    monkeypatch.setattr(history_mod, "read_engine_header", fake_read)
    assert DaemonHistoryReader._is_still_active(meta) is False
    assert calls["cached"] == 1 and calls["fresh"] == 0, (
        "a clean terminal drop must not trigger the forced-fresh fallback"
    )


def test_read_active_flows_advances_cursor_on_steps_first_write_and_new_jsonl(
    tmp_path,
):
    """The boundary read: steps-table first-write + a freshly-created analyze
    jsonl yields a non-empty ``append`` delta AND advances the per-file cursor.

    Guards Task 2's second acceptance criterion end-to-end on the real reader: a
    same-``(mtime, size)`` engine rewrite in the dense boundary window must not
    stop ``read_active_flows`` producing the analyze increment.
    """
    disk_cache.clear_cache()
    _write_engine_status(tmp_path, "live", "PAUSED")  # discovery confirm gate
    hist = _hist(tmp_path, "live")
    discovery = hist / "01_discovery_ab12.jsonl"
    _write_jsonl(discovery, [_msg("user", "explore"),
                             _msg("assistant", '{"mode":"question"}')])
    reader = _make_reader(tmp_path)

    reads = reader.read_active_flows({})
    assert reads[0].mode == HISTORY_MODE_FULL
    cursors = {r.flow_id: r.cursor for r in reads}

    # THE BOUNDARY: steps table first-write + PAUSED→RUNNING, then the analyze
    # jsonl is created and starts appending.
    steps = {f"{i:02d}_{n}": {"status": "pending", "step_type": n}
             for i, n in enumerate(["analyze", "plan", "commit"], start=2)}
    state_dir = tmp_path / "se3" / "state"
    (state_dir / "engine.json").write_text(
        json.dumps({"flow_id": "live", "status": "RUNNING",
                    "state": {"steps": steps, "current_step_index": 1},
                    "is_worktree_mode": False}),
        encoding="utf-8",
    )
    analyze = hist / "02_analyze_cd34.jsonl"
    _write_jsonl(analyze, [_msg("assistant", "analyzing", step_type="analyze")])

    reads = reader.read_active_flows(cursors)
    assert reads and reads[0].flow_id == "live"
    contents = [r.get("message", {}).get("content") for r in reads[0].records]
    assert "analyzing" in contents, "the analyze increment must be delivered"
    assert reads[0].cursor["02_analyze_cd34.jsonl"] == 1, "cursor must advance"

    # Idle poll afterwards: nothing new.
    cursors = {r.flow_id: r.cursor for r in reads}
    reads = reader.read_active_flows(cursors)
    assert reads[0].records == []
