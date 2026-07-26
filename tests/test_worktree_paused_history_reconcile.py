"""Regression lock for issue #287 — worktree discovery chat vanishes entirely.

WHY: commit ``0962eda6`` (v11.20.1) widened ``ServerState.is_active_worktree_flow``
from ``running`` to ``running|paused`` so the ``not_modified`` self-heal branch of
``GET /api/history`` would also reconcile a worktree flow parked on a human reply
— the exact window in which discovery round 2+ was going missing. Widening the
gate did not create a new bug so much as it *opened* an already-loaded gun: the
reconcile pulls a cursorless (hence ``full``) frame from the daemon and hands it
straight to ``ServerState.append_history``, whose ``full`` branch replaces the
cached bundle **wholesale**, with no floor under it. Two hops make that fatal:

  hop 1 — ``DaemonHistoryReader.read_flow``: when ``_resolve_flow_dirs`` resolves
    to nothing (the authoritative ``project_root`` does not carry a
    ``se3/history/<flow_id>`` directory) it returns ``FlowRead(mode="full",
    records=[])``. "I could not find the directory" and "this flow genuinely has
    no records" are the same wire frame — see
    ``test_read_flow_unresolvable_root_is_indistinguishable_from_empty``, which
    pins that a legacy registry walk WOULD have found the records.

  hop 2 — ``ServerState.append_history`` (``full`` branch): that empty frame
    replaces a bundle that already held round 1, rolls a fresh ``generation``,
    and the very next poll serves ``delivery: "full"`` with zero records. The
    browser rebuilds the chat pane from nothing — which is #287 as users see it:
    not "round 2 is missing" but "the discovery chat is completely blank, and the
    only place the agent's output is still visible is the pending-reply box".

The server-side test below drives that chain through the real REST endpoint with
a real daemon socket, and asserts the invariant the fix must establish: a
reconcile may only ever ADD records — it must never roll the bundle back to zero.
It fails on current master (the bundle is emptied), and is the anchor the fix
groups must turn green.

Scope note on the end-to-end confirmation: a live ``se3 run --worktree`` +
daemon + central server + real LLM discovery round-trip is not reproducible in
this environment (no LLM credentials, and the run is nondeterministic), so the
failing hop is confirmed here at the seam instead — with the same ``hist-diag``
DEBUG lines a live run emits asserted directly via ``caplog``
(``append_history APPLIED-full ... records=0``), which is a stronger check than
reading them out of a log file after the fact.
"""

from __future__ import annotations

import json
import logging
import threading

import pytest

from _authsrv import authed_app, authed_hello, login
from tianluo.daemon import protocol
from tianluo.daemon.history import DaemonHistoryReader

FLOW_ID = "20260711-191420_75f3d89f"


# --------------------------------------------------------------------------
# helpers — a real two-root worktree history on disk
# --------------------------------------------------------------------------


# The on-disk jsonl lines are raw stream-json messages; the reader wraps each in
# a ``{step_id, step_type, ordinal, message}`` envelope. Keep the two shapes
# distinct so the disk fixtures and the wire frames stay honest.
def _line(role, content):
    return {"role": role, "content": content}


def _wire(n, line):
    return {
        "step_id": "01_discovery",
        "step_type": "discovery",
        "ordinal": n,
        "message": line,
    }


ROUND_1 = [
    _line("assistant", "Q1: which module owns the reconcile?"),
    _line("user", "A1: the server history endpoint"),
]
ROUND_2 = ROUND_1 + [
    _line("assistant", "Q2: should the cache ever shrink?"),
    _line("user", "A2: never"),
]
WIRE_ROUND_1 = [_wire(n, line) for n, line in enumerate(ROUND_1)]
WIRE_ROUND_2 = [_wire(n, line) for n, line in enumerate(ROUND_2)]


def _write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8"
    )


def _write_engine_json(root, *, status):
    """Give *root* a live ``engine.json`` for FLOW_ID in *status*.

    This is what makes the reader treat the flow as ``source="active"`` (and hence
    a candidate for ``read_active_flows``); a history directory alone only ever
    yields a history-only row.
    """
    path = root / "se3" / "state" / "engine.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "flow_id": FLOW_ID,
                "status": status,
                "task_description": "worktree discovery",
                "task_type": "discovery",
                "created_at": "2026-07-11T19:14:20",
                "updated_at": "2026-07-11T19:20:00",
            }
        ),
        encoding="utf-8",
    )


def _build_two_root_history(tmp_path):
    """Lay down the on-disk shape a paused ``--worktree`` discovery really has.

    The main repo holds the pre-fork discovery copy (round 1 only); the worktree
    holds the live file the flow is still appending to (rounds 1 and 2). Returns
    ``(main_root, worktree_root)``.
    """
    main_root = tmp_path / "repo"
    worktree_root = main_root / "se3" / "worktrees" / "wt-a"
    _write_jsonl(
        main_root / "se3" / "history" / FLOW_ID / "01_discovery.jsonl", ROUND_1
    )
    _write_jsonl(
        worktree_root / "se3" / "history" / FLOW_ID / "01_discovery.jsonl", ROUND_2
    )
    return main_root, worktree_root


# --------------------------------------------------------------------------
# hop 1 — the daemon read path (real DaemonHistoryReader, real files)
# --------------------------------------------------------------------------


def test_read_flow_merges_both_roots_for_a_worktree_flow(tmp_path):
    """The happy path: the authoritative worktree root reads BOTH rounds."""
    main_root, worktree_root = _build_two_root_history(tmp_path)
    reader = DaemonHistoryReader(project_roots_provider=lambda: [str(main_root)])

    read = reader.read_flow(FLOW_ID, project_root=str(worktree_root), cursor={})

    # A cursorless read is by definition a ``full`` snapshot — the frame shape the
    # server's reconcile pull asks for, and the one that replaces the cache.
    assert read.mode == protocol.HISTORY_MODE_FULL
    assert len(read.records) == len(ROUND_2)
    assert [r["message"]["content"] for r in read.records] == [
        line["content"] for line in ROUND_2
    ]


def test_read_flow_unresolvable_root_falls_back_to_the_registry_walk(
    tmp_path, caplog
):
    """Hop 1 of #287: a resolution failure must not masquerade as an empty flow.

    ``_resolve_flow_dirs`` finds no ``se3/history/<flow_id>`` under the root it
    was handed (a worktree that was pruned, a root recorded before a move, a path
    the daemon cannot see). Before the fix ``read_flow`` reported ``mode="full",
    records=[]`` — byte-identical on the wire to "this flow has no records at
    all" — and the server's reconcile then replaced the cached rounds with that
    nothing. Now the reader falls back to the registry walk, which plainly can
    reach the records, and re-expands from the root it found so the main+worktree
    merge is still complete (round 2 included).
    """
    caplog.set_level(logging.WARNING, logger="tianluo.daemon.history")
    main_root, _worktree_root = _build_two_root_history(tmp_path)
    ghost_root = tmp_path / "pruned-worktree"
    ghost_root.mkdir()
    reader = DaemonHistoryReader(project_roots_provider=lambda: [str(main_root)])

    read = reader.read_flow(FLOW_ID, project_root=str(ghost_root), cursor={})

    assert read.mode == protocol.HISTORY_MODE_FULL
    assert len(read.records) == len(ROUND_2), (
        "the unresolvable authoritative root produced a pseudo-empty full frame "
        "instead of falling back to the registry walk — issue #287 hop 1"
    )
    assert [r["message"]["content"] for r in read.records] == [
        line["content"] for line in ROUND_2
    ]
    assert any(
        "falling back to the registry walk" in rec.getMessage()
        for rec in caplog.records
    ), "the fallback must be diagnosable, not silent"


def test_read_flow_truly_unknown_flow_returns_empty_with_a_warning(
    tmp_path, caplog
):
    """The other side of the distinction: nothing resolves anywhere.

    An empty snapshot is the only honest answer here (there is nothing to send),
    but it MUST be accompanied by a warning — that log line is the only place a
    live run can tell "the daemon could not resolve the directory" apart from
    "the flow really has no records", and the server's no-rollback invariant now
    refuses to act on the frame either way.
    """
    caplog.set_level(logging.WARNING, logger="tianluo.daemon.history")
    main_root, _worktree_root = _build_two_root_history(tmp_path)
    reader = DaemonHistoryReader(project_roots_provider=lambda: [str(main_root)])

    read = reader.read_flow(
        "20260101-000000_deadbeef", project_root=str(main_root), cursor={}
    )

    assert read.mode == protocol.HISTORY_MODE_FULL
    assert read.records == []
    assert any(
        "no history directory resolved" in rec.getMessage()
        for rec in caplog.records
    )


def test_read_active_flows_keeps_streaming_a_paused_worktree_flow(tmp_path):
    """A PAUSED flow stays in the active set and keeps producing deltas.

    The pending-reply window is exactly when discovery rounds 2+ are written, so
    a flow dropping out of ``read_active_flows`` the moment it pauses would strand
    them on the daemon (the original multi-round loss). This locks
    ``_is_active_status``: only COMPLETED / FAILED are terminal.
    """
    main_root, worktree_root = _build_two_root_history(tmp_path)
    # Only round 1 exists when the flow pauses on the human reply.
    live = worktree_root / "se3" / "history" / FLOW_ID / "01_discovery.jsonl"
    _write_jsonl(live, ROUND_1)
    _write_engine_json(worktree_root, status="PAUSED")
    reader = DaemonHistoryReader(
        project_roots_provider=lambda: [str(main_root), str(worktree_root)]
    )

    first = {r.flow_id: r for r in reader.read_active_flows({})}
    assert FLOW_ID in first, "a paused flow must stay in the active set"
    assert len(first[FLOW_ID].records) == len(ROUND_1)

    # Round 2 lands while the flow is STILL paused (the human has not replied to
    # the next question yet) — it must reach the caller as an append delta.
    _write_jsonl(live, ROUND_2)
    second = {r.flow_id: r for r in reader.read_active_flows(
        {FLOW_ID: dict(first[FLOW_ID].cursor)}
    )}
    assert FLOW_ID in second
    assert second[FLOW_ID].mode == protocol.HISTORY_MODE_APPEND
    assert [r["message"]["content"] for r in second[FLOW_ID].records] == [
        line["content"] for line in ROUND_2[len(ROUND_1):]
    ]


# --------------------------------------------------------------------------
# hop 2 — the server reconcile (real REST endpoint, real daemon socket)
# --------------------------------------------------------------------------


class _FakeDaemon:
    """A connected daemon whose ``HISTORY_REQUEST`` reply is test-controlled.

    It stands in for a daemon whose ``read_flow`` resolved no history directory:
    the reply payload can be flipped to ``[]`` to emit the pseudo-empty ``full``
    frame that hop 1 pins as reachable. Runs its receive loop on a thread because
    the reconcile happens *inside* a blocking ``TestClient.get``.
    """

    def __init__(self, client, app, machine_id="m1"):
        self._ctx = client.websocket_connect("/ws")
        self.sock = self._ctx.__enter__()
        self.sock.send_text(authed_hello(app, machine_id, "host", "6.4.0"))
        protocol.decode(self.sock.receive_text())  # WELCOME
        self.reply_records = list(WIRE_ROUND_1)
        self.requests = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                msg = protocol.decode(self.sock.receive_text())
            except Exception:
                return
            if msg.type == protocol.MSG_HISTORY_REQUEST:
                self.requests += 1
                self.sock.send_text(
                    protocol.make_history_data(
                        FLOW_ID,
                        protocol.HISTORY_MODE_FULL,
                        list(self.reply_records),
                    ).to_json()
                )

    def push_full(self, records):
        self.sock.send_text(
            protocol.make_history_data(
                FLOW_ID, protocol.HISTORY_MODE_FULL, list(records)
            ).to_json()
        )

    def report_paused_worktree_flow(self, worktree_root):
        """STATUS_UPDATE placing the flow in the state #287 needs: paused, worktree.

        ``paused`` is what a discovery round enters the moment it blocks on the
        human reply — precisely the window ``0962eda6`` opened the reconcile for.
        """
        self.sock.send_text(
            protocol.make_status_update(
                {
                    "machine_id": "m1",
                    "hostname": "host",
                    "project_roots": [str(worktree_root)],
                    "flows": [
                        {
                            "flow_id": FLOW_ID,
                            "status": "paused",
                            "project_root": str(worktree_root),
                        }
                    ],
                }
            ).to_json()
        )

    def close(self):
        self._stop.set()
        self._ctx.__exit__(None, None, None)


@pytest.fixture()
def client_and_app():
    from fastapi.testclient import TestClient

    app, _key = authed_app()
    with TestClient(app) as client:
        login(client)
        yield client, app


def test_paused_worktree_reconcile_must_not_empty_the_cached_bundle(
    tmp_path, client_and_app, caplog
):
    """#287: a reconcile that brings back nothing must not erase round 1.

    Sequence (all through the real endpoint):

    1. the daemon reports the flow ``paused`` under a worktree root;
    2. it live-pushes round 1, which the cache holds as the authoritative bundle;
    3. the browser polls with a valid ``after`` + ``sig`` → ``not_modified``, which
       is exactly the branch that fires the worktree self-heal reconcile;
    4. the reconcile's cursorless pull comes back EMPTY (hop 1);
    5. current master replaces the bundle with those zero records and serves
       ``delivery: "full"`` with an empty ``records`` — the blank chat pane.

    The assertions state the invariant: reconcile may only ever ADD.
    """
    caplog.set_level(logging.DEBUG, logger="tianluo.server.state")
    client, app = client_and_app
    _main_root, worktree_root = _build_two_root_history(tmp_path)
    # The 5 s throttle floor only decides *when* a reconcile may fire, never
    # whether an empty frame is safe; drop it so the reconcile is deterministic
    # rather than dependent on wall-clock spacing between the polls below.
    app.state.server_state._HISTORY_FULL_PULL_MIN_INTERVAL = 0.0

    daemon = _FakeDaemon(client, app)
    try:
        daemon.report_paused_worktree_flow(worktree_root)
        daemon.push_full(WIRE_ROUND_1)

        seeded = None
        for _ in range(50):
            resp = client.get(f"/api/history/{FLOW_ID}")
            if resp.status_code == 200 and resp.json().get("records"):
                seeded = resp.json()
                break
        assert seeded is not None, "round 1 never became cache-visible"
        assert seeded["delivery"] == "full"
        assert len(seeded["records"]) == len(WIRE_ROUND_1)
        token, sig = seeded["progress"], seeded["signature"]
        generation_before = seeded.get("generation")

        # The daemon can no longer resolve the flow's history directory, so its
        # cursorless reply is the pseudo-empty full frame from hop 1.
        daemon.reply_records = []

        reconciled = client.get(
            f"/api/history/{FLOW_ID}?after={token}&sig={sig}"
        ).json()

        assert daemon.requests >= 1, (
            "the paused-worktree self-heal reconcile never fired — the premise "
            "of this regression (0962eda6's running|paused widening) is gone"
        )
        # THE LOCK: an empty reconcile frame must not roll the bundle back.
        assert reconciled["records"] or reconciled["delivery"] == "not_modified", (
            "reconcile served a bundle with zero records: the browser rebuilds "
            "the chat pane empty — issue #287"
        )
        after = client.get(f"/api/history/{FLOW_ID}").json()
        assert len(after["records"]) == len(WIRE_ROUND_1), (
            "the cached bundle was emptied by an empty reconcile frame "
            f"(records={len(after['records'])}, expected {len(WIRE_ROUND_1)}) — issue #287"
        )
        # A no-op reconcile must not roll the generation either: doing so
        # invalidates every outstanding progress token and forces each in-sync
        # client into a full re-fetch + DOM rebuild on its very next poll.
        assert after.get("generation") == generation_before

        # The failing hop, in the daemon's own diagnostic vocabulary: this is the
        # line a live worktree run emits right before the chat pane goes blank.
        assert not [
            rec
            for rec in caplog.records
            if "append_history APPLIED-full" in rec.getMessage()
            and "records=0" in rec.getMessage()
        ], "an empty full frame was applied wholesale over the cached bundle"
    finally:
        daemon.close()


def test_reconcile_that_brings_more_records_still_replaces(
    tmp_path, client_and_app
):
    """The other half of the invariant: a reconcile that ADDS must still apply.

    This is the original defect ``0962eda6`` set out to fix (round 2+ stranded on
    the daemon during the pending-reply window). Any fix for #287 that simply
    stops honouring reconcile frames would break this — the bundle must grow to
    round 2 and be delivered as ``full``.
    """
    client, app = client_and_app
    _main_root, worktree_root = _build_two_root_history(tmp_path)
    app.state.server_state._HISTORY_FULL_PULL_MIN_INTERVAL = 0.0

    daemon = _FakeDaemon(client, app)
    try:
        daemon.report_paused_worktree_flow(worktree_root)
        daemon.push_full(WIRE_ROUND_1)

        seeded = None
        for _ in range(50):
            resp = client.get(f"/api/history/{FLOW_ID}")
            if resp.status_code == 200 and resp.json().get("records"):
                seeded = resp.json()
                break
        assert seeded is not None
        token, sig = seeded["progress"], seeded["signature"]

        # The daemon has now merged both roots and can see round 2.
        daemon.reply_records = list(WIRE_ROUND_2)

        reconciled = client.get(
            f"/api/history/{FLOW_ID}?after={token}&sig={sig}"
        ).json()
        assert daemon.requests >= 1
        # The client's token is now behind the grown bundle: the missing round
        # reaches it either as the rebuilt bundle or as the tail — never as a
        # bundle that still stops at round 1.
        assert reconciled["delivery"] in ("full", "delta")
        latest = client.get(f"/api/history/{FLOW_ID}").json()
        assert len(latest["records"]) == len(WIRE_ROUND_2)
    finally:
        daemon.close()
