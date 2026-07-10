"""Tests for G2 daemon traffic-reduction: snapshot slimming, status gating,
on-demand detail fetch, and wire-byte metering.

These lock the behavioural contract of group G2:

* ``aggregator`` clips issue descriptions and the *machine-wide* call prompts to
  the shared ``_DESC_CLIP`` standard, while a flow's own ``pending_calls`` keep
  the full prompt (the interactive chip bar renders it verbatim);
* ``DaemonClient._push_status`` sends a tiny keepalive when the snapshot content
  signature is unchanged (and the peer speaks revision 3), and a full
  STATUS_UPDATE otherwise / when forced / against a legacy peer;
* ``DaemonClient`` answers a DETAIL_REQUEST with the untruncated issue / call
  text and degrades read failures to an ``ok=false`` reply rather than a drop;
* every ``_send`` accrues the frame's bytes against its type in ``metrics``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from se3.daemon import protocol
from se3.daemon.aggregator import DaemonAggregator, MachineStatus, PendingCall
from se3.daemon.client import DaemonClient, _status_signature
from se3.daemon.history import _DESC_CLIP


# --------------------------------------------------------------------------- #
# Fakes / helpers
# --------------------------------------------------------------------------- #


class _FakeWS:
    """Minimal WebSocket stand-in capturing what the client sends."""

    def __init__(self) -> None:
        self.sent = []

    async def send(self, data) -> None:
        self.sent.append(protocol.decode(data))


def _make_client(**kw) -> DaemonClient:
    return DaemonClient(
        "ws://server",
        machine_id="m1",
        hostname="host",
        se3_version="1.2.3",
        snapshot_provider=kw.pop("snapshot_provider", lambda: {"machine_id": "m1"}),
        **kw,
    )


def _write_issue(root: Path, issue_id: str, *, description: str) -> None:
    import yaml

    open_dir = root / "se3" / "issues" / "open"
    open_dir.mkdir(parents=True, exist_ok=True)
    # IssueManager._find_issue_file matches the ``NNN_slug`` filename convention,
    # so name the file that way (the aggregator reads the in-file id regardless).
    (open_dir / f"{issue_id}_test.yaml").write_text(
        yaml.safe_dump(
            {
                "id": issue_id,
                "title": "T",
                "description": description,
                "status": "open",
                "tags": [],
                "source": "system",
                "created_at": "2026-01-01T00:00:00",
                "updated_at": "2026-01-01T00:00:00",
            }
        ),
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# aggregator slimming
# --------------------------------------------------------------------------- #


def test_issue_snapshot_description_clipped(tmp_path: Path) -> None:
    long_desc = "x" * 5000
    _write_issue(tmp_path, "001", description=long_desc)

    issues = DaemonAggregator._collect_issues(tmp_path)
    assert len(issues) == 1
    desc = issues[0].description
    # Aligned with history._clip: <= _DESC_CLIP content + a 3-char ellipsis.
    assert len(desc) <= _DESC_CLIP + 3
    assert desc.startswith("x" * _DESC_CLIP)
    assert desc.endswith("...")
    # to_dict carries the same clipped preview.
    assert issues[0].to_dict()["description"] == desc


def test_short_issue_description_untouched(tmp_path: Path) -> None:
    _write_issue(tmp_path, "001", description="short body")
    issues = DaemonAggregator._collect_issues(tmp_path)
    assert issues[0].description == "short body"


def test_machine_wide_pending_call_prompt_clipped_but_flow_full() -> None:
    long_prompt = "P" * 4000
    call = PendingCall(
        call_id="c1",
        path="/p/se3/calls/c1.json",
        project_root="/p",
        kind=protocol.CALL_KIND_INTERJECTION,
        prompt=long_prompt,
    )

    # Machine-wide surface clips the prompt (full text fetched on demand).
    machine = MachineStatus(machine_id="m", hostname="h", pending_calls=[call])
    wire = machine.to_dict()["pending_calls"][0]
    assert len(wire["prompt"]) <= _DESC_CLIP + 3
    assert wire["prompt"].endswith("...")

    # The flow's own pending_calls keep the full prompt for the chip bar.
    full = call.to_dict()
    assert full["prompt"] == long_prompt


# --------------------------------------------------------------------------- #
# STATUS_UPDATE content gating + keepalive
# --------------------------------------------------------------------------- #


def test_status_signature_ignores_generated_at() -> None:
    a = {"machine_id": "m", "flows": [], "generated_at": 1.0}
    b = {"machine_id": "m", "flows": [], "generated_at": 999.0}
    assert _status_signature(a) == _status_signature(b)
    c = {"machine_id": "m", "flows": [{"flow_id": "f"}], "generated_at": 1.0}
    assert _status_signature(a) != _status_signature(c)


def test_unchanged_snapshot_sends_keepalive_when_peer_supports() -> None:
    snap = {"machine_id": "m1", "flows": [], "generated_at": 1.0}
    client = _make_client(snapshot_provider=lambda: dict(snap))
    client._peer_protocol_version = "3"  # server speaks revision 3
    ws = _FakeWS()

    async def scenario():
        await client._push_status(ws)  # first push -> full baseline
        await client._push_status(ws)  # unchanged -> keepalive

    asyncio.run(scenario())
    assert [m.type for m in ws.sent] == [
        protocol.MSG_STATUS_UPDATE,
        protocol.MSG_KEEPALIVE,
    ]
    # The keepalive carries the same signature the gate matched on.
    assert ws.sent[1].payload["signature"] == client._last_status_sig


def test_changed_snapshot_sends_full_update() -> None:
    box = {"n": 0}

    def provider():
        box["n"] += 1
        return {"machine_id": "m1", "flows": [{"flow_id": f"f{box['n']}"}]}

    client = _make_client(snapshot_provider=provider)
    client._peer_protocol_version = "3"
    ws = _FakeWS()

    async def scenario():
        await client._push_status(ws)
        await client._push_status(ws)

    asyncio.run(scenario())
    assert [m.type for m in ws.sent] == [
        protocol.MSG_STATUS_UPDATE,
        protocol.MSG_STATUS_UPDATE,
    ]


def test_legacy_peer_never_gets_keepalive() -> None:
    snap = {"machine_id": "m1", "flows": []}
    client = _make_client(snapshot_provider=lambda: dict(snap))
    client._peer_protocol_version = "2"  # legacy server: no lean frames
    ws = _FakeWS()

    async def scenario():
        await client._push_status(ws)
        await client._push_status(ws)

    asyncio.run(scenario())
    assert [m.type for m in ws.sent] == [
        protocol.MSG_STATUS_UPDATE,
        protocol.MSG_STATUS_UPDATE,
    ]


def test_force_bypasses_keepalive_gate() -> None:
    snap = {"machine_id": "m1", "flows": []}
    client = _make_client(snapshot_provider=lambda: dict(snap))
    client._peer_protocol_version = "3"
    ws = _FakeWS()

    async def scenario():
        await client._push_status(ws)
        await client._push_status(ws, force=True)  # unchanged but forced

    asyncio.run(scenario())
    assert [m.type for m in ws.sent] == [
        protocol.MSG_STATUS_UPDATE,
        protocol.MSG_STATUS_UPDATE,
    ]


# --------------------------------------------------------------------------- #
# detail-request handler
# --------------------------------------------------------------------------- #


def test_detail_request_returns_full_issue(tmp_path: Path) -> None:
    long_desc = "D" * 3000
    _write_issue(tmp_path, "001", description=long_desc)
    client = _make_client()
    ws = _FakeWS()

    async def scenario():
        await client._dispatch(
            ws,
            protocol.make_detail_request(
                protocol.DETAIL_KIND_ISSUE,
                "001",
                project_root=str(tmp_path),
                request_id="r1",
            ),
        )

    asyncio.run(scenario())
    assert len(ws.sent) == 1
    reply = ws.sent[0]
    assert reply.type == protocol.MSG_DETAIL_DATA
    assert reply.payload["ok"] is True
    assert reply.payload["request_id"] == "r1"
    # Untruncated body round-trips.
    assert reply.payload["detail"]["description"] == long_desc


def test_detail_request_returns_full_call_prompt(tmp_path: Path) -> None:
    calls_dir = tmp_path / "se3" / "calls"
    calls_dir.mkdir(parents=True, exist_ok=True)
    long_prompt = "Q" * 3000
    (calls_dir / "c1.json").write_text(
        json.dumps({"kind": "call", "prompt": long_prompt}), encoding="utf-8"
    )
    client = _make_client()
    ws = _FakeWS()

    async def scenario():
        await client._dispatch(
            ws,
            protocol.make_detail_request(
                protocol.DETAIL_KIND_CALL,
                "c1",
                project_root=str(tmp_path),
                request_id="r2",
            ),
        )

    asyncio.run(scenario())
    reply = ws.sent[0]
    assert reply.payload["ok"] is True
    assert reply.payload["detail"]["prompt"] == long_prompt
    assert reply.payload["detail"]["call_id"] == "c1"


def test_detail_request_missing_target_replies_error(tmp_path: Path) -> None:
    client = _make_client()
    ws = _FakeWS()

    async def scenario():
        await client._dispatch(
            ws,
            protocol.make_detail_request(
                protocol.DETAIL_KIND_ISSUE,
                "does-not-exist",
                project_root=str(tmp_path),
                request_id="r3",
            ),
        )

    asyncio.run(scenario())
    reply = ws.sent[0]
    assert reply.type == protocol.MSG_DETAIL_DATA
    assert reply.payload["ok"] is False
    assert reply.payload["error"]
    assert "detail" not in reply.payload  # no body on failure


def test_detail_request_resolves_via_known_roots(tmp_path: Path) -> None:
    """A request with no project_root falls back to the last-known roots."""
    _write_issue(tmp_path, "001", description="full body here")
    client = _make_client()
    client._last_known_project_roots = {str(tmp_path)}
    ws = _FakeWS()

    async def scenario():
        await client._dispatch(
            ws,
            protocol.make_detail_request(
                protocol.DETAIL_KIND_ISSUE, "001", request_id="r4"
            ),
        )

    asyncio.run(scenario())
    assert ws.sent[0].payload["ok"] is True
    assert ws.sent[0].payload["detail"]["description"] == "full body here"


# --------------------------------------------------------------------------- #
# wire metrics
# --------------------------------------------------------------------------- #


def test_send_accrues_wire_metrics() -> None:
    client = _make_client()
    ws = _FakeWS()

    async def scenario():
        await client._send(ws, protocol.make_pong(seq=1))
        await client._send(ws, protocol.make_keepalive("sig"))

    asyncio.run(scenario())
    snap = client.metrics.snapshot()
    assert snap[protocol.MSG_PONG]["count"] == 1
    assert snap[protocol.MSG_KEEPALIVE]["count"] == 1
    assert snap["__total__"]["count"] == 2
    assert snap["__total__"]["bytes"] > 0
