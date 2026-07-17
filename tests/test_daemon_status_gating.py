"""Tests for the daemon's STATUS_UPDATE thinning + content-signature gate (G6).

These lock the traffic-reduction contract on the daemon status side:

* the aggregated snapshot is *thinned* — an issue carries only summary fields
  with its ``description`` clipped to the shared ``_DESC_CLIP`` (200) standard,
  and a machine-wide pending call clips its ``prompt`` — so the full body is a
  detail request away rather than inlined into every 5-second push;
* a tick whose thinned snapshot is byte-identical to the last full push (its
  content signature matches, ``generated_at`` excluded) collapses to a tiny
  ``MSG_KEEPALIVE`` instead of re-shipping the whole snapshot, but only when the
  peer speaks the revision-3 lean protocol; a legacy peer always gets a full
  ``MSG_STATUS_UPDATE``;
* every frame that leaves the socket is metered by message type in
  :attr:`DaemonClient.metrics`, so "an idle daemon costs only keepalive-sized
  traffic" is verifiable at runtime.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from se3.daemon import protocol
from se3.daemon.aggregator import DaemonAggregator, IssueSnapshot, PendingCall
from se3.daemon.client import DaemonClient
from se3.daemon.history import _DESC_CLIP


class _FakeWS:
    """Minimal WebSocket stand-in capturing the frames the client sends."""

    def __init__(self):
        self.sent = []

    async def send(self, data):
        self.sent.append(protocol.decode(data))


def _client(snapshot_provider, *, peer_version="3"):
    client = DaemonClient(
        "ws://server",
        machine_id="m1",
        hostname="host",
        se3_version="6.4.0",
        snapshot_provider=snapshot_provider,
    )
    # ``_push_status`` gates on the peer's advertised protocol version (learned
    # from WELCOME). Set it directly so the test can model a rev-3 (lean) peer
    # or a legacy peer without driving the whole handshake.
    client._peer_protocol_version = peer_version
    return client


def _types(ws):
    return [m.type for m in ws.sent]


# --------------------------------------------------------------------------
# content-signature gate: unchanged tick → keepalive, changed tick → full
# --------------------------------------------------------------------------


def test_unchanged_tick_collapses_to_keepalive():
    """Two identical snapshots ⇒ one full STATUS_UPDATE then a KEEPALIVE.

    ``generated_at`` moves every build; the gate must exclude it, so a snapshot
    that differs only in that stamp still hashes equal and gates to keepalive.
    """
    base = {"machine_id": "m1", "flows": [], "issues": [], "pending_calls": []}
    # generated_at deliberately moves each call to prove it is excluded from the
    # signature — otherwise every tick would look "changed" and never gate.
    provider = lambda: dict(base, generated_at=time.time())
    client = _client(provider)
    ws = _FakeWS()

    asyncio.run(client._push_status(ws))  # first push: primes the baseline
    asyncio.run(client._push_status(ws))  # unchanged: must be a keepalive

    assert _types(ws) == [protocol.MSG_STATUS_UPDATE, protocol.MSG_KEEPALIVE]
    # The keepalive carries the same content signature the daemon gated on.
    assert ws.sent[1].payload["signature"] == client._last_status_sig


def test_changed_tick_sends_full_status_update():
    """A substantive snapshot change ⇒ a fresh full STATUS_UPDATE, not keepalive."""
    state = {"machine_id": "m1", "flows": [], "issues": []}

    def provider():
        return dict(state, generated_at=time.time())

    client = _client(provider)
    ws = _FakeWS()

    asyncio.run(client._push_status(ws))  # baseline STATUS_UPDATE
    state["issues"] = [{"id": "1", "status": "open"}]  # content changes
    asyncio.run(client._push_status(ws))

    assert _types(ws) == [protocol.MSG_STATUS_UPDATE, protocol.MSG_STATUS_UPDATE]


def test_legacy_peer_never_gets_a_keepalive():
    """A pre-rev-3 peer always receives a full STATUS_UPDATE even when unchanged.

    The keepalive frame is a revision-3 message; emitting it to a legacy peer
    that would reject it must never happen, so the gate degrades to full pushes.
    """
    base = {"machine_id": "m1", "flows": [], "issues": []}
    provider = lambda: dict(base, generated_at=time.time())
    client = _client(provider, peer_version=None)
    ws = _FakeWS()

    asyncio.run(client._push_status(ws))
    asyncio.run(client._push_status(ws))

    assert _types(ws) == [protocol.MSG_STATUS_UPDATE, protocol.MSG_STATUS_UPDATE]


def test_force_bypasses_the_gate():
    """An event-driven ``force`` push delivers real state regardless of the gate."""
    base = {"machine_id": "m1", "flows": [], "issues": []}
    provider = lambda: dict(base, generated_at=time.time())
    client = _client(provider)
    ws = _FakeWS()

    asyncio.run(client._push_status(ws))  # baseline
    asyncio.run(client._push_status(ws, force=True))  # forced, unchanged content

    assert _types(ws) == [protocol.MSG_STATUS_UPDATE, protocol.MSG_STATUS_UPDATE]


# --------------------------------------------------------------------------
# wire_metrics: per-message-type byte accounting
# --------------------------------------------------------------------------


def test_metrics_accumulate_per_message_type():
    """Every sent frame is metered by type; a keepalive costs far less than a full."""
    base = {"machine_id": "m1", "flows": [], "issues": []}
    provider = lambda: dict(base, generated_at=time.time())
    client = _client(provider)
    ws = _FakeWS()

    asyncio.run(client._push_status(ws))  # full STATUS_UPDATE
    asyncio.run(client._push_status(ws))  # KEEPALIVE

    snap = client.metrics.snapshot()
    assert snap[protocol.MSG_STATUS_UPDATE]["count"] == 1
    assert snap[protocol.MSG_KEEPALIVE]["count"] == 1
    # The whole point of the gate: the idle keepalive is a tiny fraction of the
    # full snapshot's bytes.
    assert (
        snap[protocol.MSG_KEEPALIVE]["bytes"]
        < snap[protocol.MSG_STATUS_UPDATE]["bytes"]
    )
    # The synthetic roll-up totals both frames.
    assert snap["__total__"]["count"] == 2
    assert snap["__total__"]["bytes"] == (
        snap[protocol.MSG_STATUS_UPDATE]["bytes"]
        + snap[protocol.MSG_KEEPALIVE]["bytes"]
    )


# --------------------------------------------------------------------------
# snapshot thinning: issue description + call prompt truncation to _DESC_CLIP
# --------------------------------------------------------------------------


def test_desc_clip_standard_is_200():
    """The clip standard the snapshot thins to is the shared history one (200)."""
    assert _DESC_CLIP == 200


def test_collected_issue_description_is_clipped(tmp_path: Path):
    """``_collect_issues`` truncates every issue's description to _DESC_CLIP.

    Inlining every open+closed issue's full body is what ballooned the
    STATUS_UPDATE; the collected snapshot must carry only a preview so the full
    text is fetched on demand.
    """
    issues_open = tmp_path / "se3" / "issues" / "open"
    issues_open.mkdir(parents=True)
    long_desc = "x" * 500
    (issues_open / "001_big.yaml").write_text(
        "id: '1'\n"
        "title: Big issue\n"
        "status: open\n"
        "priority: high\n"
        "type: bug\n"
        "source: system\n"
        f"description: '{long_desc}'\n",
        encoding="utf-8",
    )

    snaps = DaemonAggregator()._collect_issues(tmp_path)

    assert len(snaps) == 1
    snap = snaps[0]
    # Clipped to 200 chars + the "..." ellipsis marker.
    assert snap.description == "x" * _DESC_CLIP + "..."
    assert len(snap.description) == _DESC_CLIP + 3


def test_issue_snapshot_to_dict_is_a_summary():
    """``IssueSnapshot.to_dict`` carries only the webui summary fields."""
    snap = IssueSnapshot(
        id="1",
        project_root="/p",
        title="t",
        description="preview...",
        status="open",
        priority="high",
        type="bug",
        tags=["a", "b"],
        source="system",
        created_at="c",
        updated_at="u",
    )
    data = snap.to_dict()
    assert set(data) == {
        "id",
        "project_root",
        "title",
        "description",
        "status",
        "priority",
        "type",
        "tags",
        "source",
        "created_at",
        "updated_at",
    }


def test_machine_wide_pending_call_prompt_is_clipped():
    """``clip_prompt=True`` clips the prompt; the full body is fetched on demand.

    Both STATUS_UPDATE pending_calls surfaces (machine-wide and a flow's own)
    pass ``clip_prompt=True`` so no full prompt inlines into the snapshot. The
    parameter still defaults to ``False`` for any caller that wants the verbatim
    body, which this test also pins.
    """
    call = PendingCall(
        call_id="c1",
        path="/p/se3/calls/c1.json",
        project_root="/p",
        prompt="y" * 500,
    )
    assert call.to_dict()["prompt"] == "y" * 500  # default: verbatim
    clipped = call.to_dict(clip_prompt=True)["prompt"]  # STATUS_UPDATE: clipped
    assert clipped == "y" * _DESC_CLIP + "..."
