"""End-to-end machine switch on a shared filesystem (node007 → node008).

The HPC shape the online-first resolution was written for, driven through the
real HTTP + WebSocket surface rather than :class:`ServerState` alone: a job ends
on node007, its daemon disconnects, and the next job starts on node008 against
the SAME disk — so both daemons report the same ``flow_id``, and the server
keeps the dead machine's flows, history index and cached bundle.

Resolving in plain insertion order made the dead node007 shadow the live
node008 forever, which the operator saw as a 404 on ``GET /api/history/{id}``
and a ``machine 'node007' … is not connected`` 404 on resume. Locked here:

* the detail endpoint re-pulls from node008 (the cached bundle is bound to
  node007 and must therefore read as a miss) and answers 200;
* the re-pull happens ONCE — the second read is served from the fresh cache;
* resume dispatches ``MSG_SPAWN_FLOW`` to node008 and says so in its receipt;
* the history list offers the flow once, pointing at node008;
* the switch takes effect the moment node008 connects — NOT 60 s later when the
  presence debouncer finally flips node007's badge to offline;
* a node killed mid-run — whose last frame froze at ``running``/not-
  ``resumable`` and is indistinguishable from a live one — cannot wedge the
  session: resume and end still dispatch to node008, and reading the flow
  detail and history from node008 keeps working;
* the same single resolution serves every other flow-scoped route, so answering
  a pending call, interjecting, and pulling a call's full prompt all reach
  node008 too — otherwise a flow blocked on a human call could never be
  unblocked;
* the issue mirror resolves the same way, so "start flow from issue" dispatches
  to node008 and the issue list offers the issue once — that path has no
  presence grace to expire, so mis-resolving it is a 404 that never heals;
* with nothing online at all, both endpoints answer exactly as they did before
  the fix (the fallback pass IS the old code path).

The pull-parked-handler + stand-in-daemon-socket choreography follows
``tests/test_server_history.py``: the blocking ``GET`` runs on a worker thread
while the test thread plays the daemon.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Tuple

import pytest

from _authsrv import authed_app, authed_hello, login
from se3.daemon import protocol


FLOW_ID = "flow-shared-fs"
MACHINE_A = "node007"
MACHINE_B = "node008"

# In the field a shared mount gives both nodes the SAME path; the two roots
# differ here only so an assertion can name WHICH machine's metadata the server
# resolved and handed to the daemon it pulls from. Neither path may sit under
# ``se3/worktrees/`` — that would arm the active-worktree self-heal reconcile
# and add a daemon round-trip these tests do not model.
ROOT_A = "/shared/jobs/proj-a"
ROOT_B = "/shared/jobs/proj-b"

STEP = "01_discovery_9ed2a95c"

# Same session, read off the same disk by each node in turn: identical step id,
# distinguishable content so the served bundle names its source.
RECORDS_A: List[Dict[str, Any]] = [
    {
        "step_id": STEP,
        "step_type": "discovery",
        "ordinal": 0,
        "message": {"role": "user", "content": "read by node007"},
    }
]
RECORDS_B: List[Dict[str, Any]] = [
    {
        "step_id": STEP,
        "step_type": "discovery",
        "ordinal": 0,
        "message": {"role": "user", "content": "read by node008"},
    },
    {
        "step_id": STEP,
        "step_type": "discovery",
        "ordinal": 1,
        "message": {"role": "assistant", "content": "the round node007 never saw"},
    },
]

# Offline-grace window used in place of the production 60 s: the switch only
# begins once node007's record actually flips offline.
OFFLINE_GRACE = 0.05
# Deadline for every poll loop below. Generous — the loops exit on the observed
# state, not on the clock; this only bounds a genuine failure.
POLL_DEADLINE = 10.0
# How long a detail request gets to prove it parked on a daemon pull. A handler
# that resolved to the DEAD machine instead answers 404 in milliseconds, so this
# window separates the two outcomes without racing either.
DISPATCH_GRACE = 0.5


@contextmanager
def _server(monkeypatch, *, offline_grace: Optional[float]):
    """Build an authenticated server; *offline_grace* ``None`` keeps production's.

    Tests that WATCH the offline flip shorten the grace; the ones that must sit
    INSIDE the grace window (where the dead machine's record still reads online)
    keep the production 60 s so nothing can flip underneath them.
    """
    from fastapi.testclient import TestClient

    import se3.server.app as app_module

    # ``GET /api/history`` broadcasts a forced index re-push and waits for the
    # replies; our stand-in daemons never answer, so every call would otherwise
    # burn the full 2 s.
    monkeypatch.setattr(app_module, "HISTORY_INDEX_REFRESH_TIMEOUT", 0.3)
    # Bound the parked-pull wait so a test that never gets to answer fails in
    # seconds rather than in the production 30 — but keep it comfortably above
    # ``DISPATCH_GRACE`` below, which the test thread spends confirming the
    # handler really did park.
    monkeypatch.setattr(app_module, "HISTORY_PULL_TIMEOUT", 5.0)
    if offline_grace is not None:
        real_debouncer = app_module.PresenceDebouncer
        monkeypatch.setattr(
            app_module,
            "PresenceDebouncer",
            lambda delay=None, _cls=real_debouncer, _d=offline_grace: _cls(delay=_d),
        )

    app, _key = authed_app()
    with TestClient(app) as client:
        login(client)
        yield client, app


@pytest.fixture()
def client_and_app(monkeypatch):
    # Production graces the offline flip by 60 s to absorb lossy-link reconnect
    # churn; a test that has to WATCH the flip cannot wait that out.
    with _server(monkeypatch, offline_grace=OFFLINE_GRACE) as pair:
        yield pair


# --------------------------------------------------------------------------
# daemon stand-ins
# --------------------------------------------------------------------------


def _flow_payload(
    project_root: str,
    *,
    status: str = "paused",
    resumable: bool = True,
) -> Dict[str, Any]:
    """A paused-but-resumable flow, as a daemon aggregator would report it.

    ``status="running", resumable=False`` is the aggregator's live-process
    shape: ``_resumable_with_live_gate`` clears the flag for a RUNNING flow
    whose root the local supervisor still has a live ``se3 run`` in.
    """
    return {
        "flow_id": FLOW_ID,
        "project_root": project_root,
        "task_description": "shared filesystem session",
        "status": status,
        "resumable": resumable,
        "updated_at": "2026-07-24T10:00:00",
    }


CALL_ID = "call-shared-fs"


def _blocked_flow(project_root: str, *, live: bool) -> Dict[str, Any]:
    """A RUNNING flow parked on a pending human call.

    This is the state a flow sits in for as long as nobody answers. Only the
    node whose supervisor actually holds the ``se3 run`` reports it as
    non-resumable (``live=True``); the peer sees the same RUNNING status off the
    shared disk but no local process. Exactly the shape that must NOT stop the
    answer from being delivered through the node that can still be reached.
    """
    payload = _flow_payload(project_root, status="running", resumable=not live)
    payload["pending_calls"] = [
        {
            "call_id": CALL_ID,
            "kind": "human",
            "prompt": f"truncated prompt as read by {project_root}",
        }
    ]
    return payload


def _session_meta(project_root: str) -> Dict[str, Any]:
    """The matching ``MSG_HISTORY_INDEX`` row."""
    return {
        "flow_id": FLOW_ID,
        "project_root": project_root,
        "task_description": "shared filesystem session",
        "status": "paused",
        "updated_at": "2026-07-24T10:00:00",
    }


def _receive_until(sock, msg_type: str):
    """Read frames from *sock* until one of *msg_type*, skipping broadcasts.

    ``GET /api/history`` queues a ``MSG_HISTORY_INDEX_REQUEST`` on every
    connected daemon and the handshake emits a ``MSG_VIEWERS`` presence frame,
    so a test expecting a specific dispatched frame must read past both.
    """
    while True:
        frame = protocol.decode(sock.receive_text())
        if frame.type == msg_type:
            return frame


def _connect_daemon(
    client,
    app,
    machine_id: str,
    project_root: str,
    *,
    records: Optional[List[Dict[str, Any]]] = None,
    flow: Optional[Dict[str, Any]] = None,
) -> Tuple[Any, Any]:
    """Connect a stand-in daemon reporting the shared flow; return (ctx, sock).

    Frames go out in the order ``STATUS_UPDATE`` → (``HISTORY_DATA``) →
    ``HISTORY_INDEX``. One socket is processed in order, so the index row
    surfacing on ``GET /api/history`` is a barrier proving the earlier frames
    landed too — which is what lets the waits below poll a single endpoint
    instead of racing several.
    """
    ctx = client.websocket_connect("/ws")
    sock = ctx.__enter__()
    sock.send_text(authed_hello(app, machine_id, machine_id, "6.4.0"))
    protocol.decode(sock.receive_text())  # WELCOME
    sock.send_text(
        protocol.make_status_update(
            {
                "hostname": machine_id,
                "flows": [
                    flow if flow is not None else _flow_payload(project_root)
                ],
            }
        ).to_json()
    )
    if records is not None:
        sock.send_text(
            protocol.make_history_data(
                FLOW_ID, protocol.HISTORY_MODE_FULL, records
            ).to_json()
        )
    sock.send_text(
        protocol.make_history_index([_session_meta(project_root)]).to_json()
    )
    return ctx, sock


def _wait_for_index_row(client, machine_id: str) -> Dict[str, Any]:
    """Poll the history list until *machine_id* is listed as reporting the flow.

    A barrier, deliberately NOT an assertion about de-duplication: it waits for
    that machine's ``MSG_HISTORY_INDEX`` to have landed and nothing more, so
    every claim under test fails in its own test body rather than in this setup
    helper. The single-row invariant has its own case below.
    """
    deadline = time.monotonic() + POLL_DEADLINE
    seen: Any = None
    while time.monotonic() < deadline:
        # Each call already blocks on the (shortened) index-refresh wait, so no
        # extra sleep is needed to keep this from spinning.
        sessions = client.get("/api/history").json()["sessions"]
        rows = [s for s in sessions if s.get("flow_id") == FLOW_ID]
        seen = [r.get("machine_id") for r in rows]
        for row in rows:
            if row.get("machine_id") == machine_id:
                return row
    raise AssertionError(
        f"{machine_id} never reported {FLOW_ID} to the history index (saw {seen})"
    )


def _wait_offline(client, machine_id: str) -> None:
    """Poll until *machine_id*'s record has actually flipped offline."""
    deadline = time.monotonic() + POLL_DEADLINE
    while time.monotonic() < deadline:
        machines = client.get("/api/machines").json()["machines"]
        record = next(
            (m for m in machines if m.get("machine_id") == machine_id), None
        )
        assert record is not None, f"{machine_id} vanished instead of going offline"
        if record.get("online") is False:
            return
        time.sleep(0.02)
    raise AssertionError(f"{machine_id} never went offline")


class _Fleet:
    """The post-switch fleet: node007 offline-but-remembered, node008 live."""

    def __init__(self, client, ctx_b, sock_b) -> None:
        self.client = client
        self.sock_b = sock_b
        self._ctx_b = ctx_b

    def disconnect_b(self) -> None:
        """Take the last live daemon down and wait for the record to follow."""
        if self._ctx_b is not None:
            self._ctx_b.__exit__(None, None, None)
            self._ctx_b = None
            _wait_offline(self.client, MACHINE_B)


@pytest.fixture()
def fleet(client_and_app):
    """Replay the job hand-off: node007 runs and dies, node008 picks the flow up."""
    client, app = client_and_app

    ctx_a, _sock_a = _connect_daemon(
        client, app, MACHINE_A, ROOT_A, records=RECORDS_A
    )
    _wait_for_index_row(client, MACHINE_A)
    # Precondition: while node007 is alive the bundle is served from cache and
    # is bound to node007 — the binding whose staleness the switch must detect.
    seeded = client.get(f"/api/history/{FLOW_ID}")
    assert seeded.status_code == 200, seeded.text
    assert seeded.json()["cached"] is True
    assert seeded.json()["machine_id"] == MACHINE_A
    assert seeded.json()["records"] == RECORDS_A

    ctx_a.__exit__(None, None, None)
    _wait_offline(client, MACHINE_A)

    # node008 mounts the same disk and reports the same session. It pushes NO
    # bundle: a freshly connected daemon has only announced what it can serve.
    ctx_b, sock_b = _connect_daemon(client, app, MACHINE_B, ROOT_B, records=None)
    _wait_for_index_row(client, MACHINE_B)

    fleet = _Fleet(client, ctx_b, sock_b)
    try:
        yield fleet
    finally:
        if fleet._ctx_b is not None:
            fleet._ctx_b.__exit__(None, None, None)


# --------------------------------------------------------------------------
# history detail — the 404 that started this
# --------------------------------------------------------------------------


def test_history_detail_repulls_from_the_live_machine(fleet):
    """The stale node007 bundle reads as a miss and is re-pulled from node008.

    Pre-fix this asserted-on GET returned 404 (``no connected daemon owns
    history``): resolution picked the dead node007 and there was no connection
    to pull through. Post-fix the request parks on a pull dispatched to
    node008, carrying node008's own ``project_root`` — the root the daemon
    needs to find the session on the shared disk.
    """
    client = fleet.client
    result: Dict[str, Any] = {}

    def do_get() -> None:
        result["resp"] = client.get(f"/api/history/{FLOW_ID}")

    worker = threading.Thread(target=do_get)
    worker.start()
    try:
        # A handler that resolved to the dead machine 404s immediately instead
        # of parking on a pull. Check that BEFORE reading node008's socket, so
        # the regression fails with its own message rather than blocking on a
        # frame that will never arrive.
        worker.join(timeout=DISPATCH_GRACE)
        assert worker.is_alive(), (
            "the detail request returned without pulling from the live machine: "
            f"{result.get('resp') and result['resp'].text}"
        )
        req = _receive_until(fleet.sock_b, protocol.MSG_HISTORY_REQUEST)
        assert req.payload["flow_id"] == FLOW_ID
        # ``get_history_flow_project_root`` must resolve online-first too:
        # handing node008 node007's root would send it looking in the wrong
        # place for a worktree-split session.
        assert req.payload["project_root"] == ROOT_B
        fleet.sock_b.send_text(
            protocol.make_history_data(
                FLOW_ID, protocol.HISTORY_MODE_FULL, RECORDS_B
            ).to_json()
        )
    finally:
        worker.join(timeout=POLL_DEADLINE)

    resp = result["resp"]
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cached"] is False
    assert body["delivery"] == "full"
    assert body["machine_id"] == MACHINE_B
    assert body["records"] == RECORDS_B


def test_the_repull_happens_once_then_the_cache_serves(fleet):
    """The bundle re-binds to node008, so the next read is a plain cache hit.

    The switch costs ONE daemon round-trip. If the re-bind did not stick, every
    poll of the open chat pane would re-pull the whole bundle — the jitter the
    ``expected_machine_id`` binding exists to avoid.
    """
    client = fleet.client
    result: Dict[str, Any] = {}

    def do_get() -> None:
        result["resp"] = client.get(f"/api/history/{FLOW_ID}")

    worker = threading.Thread(target=do_get)
    worker.start()
    try:
        worker.join(timeout=DISPATCH_GRACE)
        assert worker.is_alive(), "no re-pull was dispatched at all"
        _receive_until(fleet.sock_b, protocol.MSG_HISTORY_REQUEST)
        fleet.sock_b.send_text(
            protocol.make_history_data(
                FLOW_ID, protocol.HISTORY_MODE_FULL, RECORDS_B
            ).to_json()
        )
    finally:
        worker.join(timeout=POLL_DEADLINE)
    assert result["resp"].status_code == 200, result["resp"].text

    # No worker thread this time: a second re-pull would park the handler until
    # the (shortened) pull timeout and surface as a 504, not as a hang.
    again = client.get(f"/api/history/{FLOW_ID}")
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["cached"] is True
    assert body["machine_id"] == MACHINE_B
    assert body["records"] == RECORDS_B


# --------------------------------------------------------------------------
# resume + list
# --------------------------------------------------------------------------


def test_resume_dispatches_to_the_live_machine(fleet):
    """Resume routes to node008 and the receipt names it.

    Pre-fix this was the ``machine 'node007' owning flow … is not connected``
    404: the browser offered a Resume button that could never fire again once
    the job moved nodes.
    """
    resp = fleet.client.post(f"/api/flows/{FLOW_ID}/resume")
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["status"] == "resume_dispatched"
    assert body["machine_id"] == MACHINE_B
    assert body["flow_id"] == FLOW_ID

    spawn = _receive_until(fleet.sock_b, protocol.MSG_SPAWN_FLOW)
    assert spawn.payload["resume_flow_id"] == FLOW_ID
    # The root travels from node008's own flow snapshot, so the daemon resumes
    # the checkout it can actually see.
    assert spawn.payload["project_root"] == ROOT_B


def test_history_list_offers_the_session_once(fleet):
    """Both nodes report the session; the operator is offered one row — node008's.

    A duplicate row would let the console open a session card whose machine the
    detail fetch never routes to, i.e. a permanent 404 behind a visible entry.
    """
    sessions = fleet.client.get("/api/history").json()["sessions"]
    rows = [s for s in sessions if s.get("flow_id") == FLOW_ID]
    assert len(rows) == 1
    assert rows[0]["machine_id"] == MACHINE_B
    assert rows[0]["project_root"] == ROOT_B


# --------------------------------------------------------------------------
# fallback parity — nothing online
# --------------------------------------------------------------------------


def test_resume_with_no_live_machine_is_the_old_404(fleet):
    """With every daemon gone, resume answers exactly as it did before the fix.

    The fallback pass IS the pre-fix resolution: the flow is still known (so it
    is not "not found"), the machine holding it is simply unreachable. That
    distinction is the contract the console's 404 branch renders against.
    """
    fleet.disconnect_b()

    resp = fleet.client.post(f"/api/flows/{FLOW_ID}/resume")
    assert resp.status_code == 404, resp.text
    detail = resp.json()["detail"]
    assert "is not connected" in detail
    # Insertion order decides among offline candidates, unchanged.
    assert MACHINE_A in detail


def test_unknown_flow_resume_404_is_distinguishable(fleet):
    """The other 404: a flow nobody ever reported reads as "not found".

    Both replies are 404s, so only the ``detail`` tells the console whether to
    say "this session is gone" or "the machine running it is offline".
    """
    resp = fleet.client.post("/api/flows/no-such-flow/resume")
    assert resp.status_code == 404, resp.text
    detail = resp.json()["detail"]
    assert "not found" in detail
    assert "is not connected" not in detail


# --------------------------------------------------------------------------
# inside the offline grace — the dead machine still *displays* as online
# --------------------------------------------------------------------------


@pytest.fixture()
def graced_fleet(monkeypatch):
    """node007 dies and node008 takes over WITHIN the 60 s presence grace.

    The production ``PresenceDebouncer`` delay is left untouched, so node007's
    record still reads ``online: true`` for the whole test. Anything ordering on
    that flag would keep routing at the machine whose socket is already gone.
    """
    with _server(monkeypatch, offline_grace=None) as (client, app):
        ctx_a, _sock_a = _connect_daemon(client, app, MACHINE_A, ROOT_A)
        _wait_for_index_row(client, MACHINE_A)
        ctx_a.__exit__(None, None, None)

        ctx_b, sock_b = _connect_daemon(client, app, MACHINE_B, ROOT_B)
        _wait_for_index_row(client, MACHINE_B)

        record = next(
            m
            for m in client.get("/api/machines").json()["machines"]
            if m["machine_id"] == MACHINE_A
        )
        assert record["online"] is True, (
            "the grace window closed early — this fixture must observe the "
            "dead machine while it still displays as online"
        )

        fleet = _Fleet(client, ctx_b, sock_b)
        try:
            yield fleet
        finally:
            if fleet._ctx_b is not None:
                fleet._ctx_b.__exit__(None, None, None)


def test_resume_inside_the_grace_window_reaches_the_live_machine(graced_fleet):
    """The operator clicks Resume seconds after the job moved nodes.

    Waiting out the presence grace before the switch takes effect would leave a
    full minute of ``machine 'node007' … is not connected`` 404s — the same
    failure, merely delayed.
    """
    resp = graced_fleet.client.post(f"/api/flows/{FLOW_ID}/resume")
    assert resp.status_code == 202, resp.text
    assert resp.json()["machine_id"] == MACHINE_B

    spawn = _receive_until(graced_fleet.sock_b, protocol.MSG_SPAWN_FLOW)
    assert spawn.payload["resume_flow_id"] == FLOW_ID
    assert spawn.payload["project_root"] == ROOT_B


def test_history_list_inside_the_grace_window_points_at_the_live_machine(
    graced_fleet,
):
    """The listed ``machine_id`` must be the one the detail fetch will use."""
    sessions = graced_fleet.client.get("/api/history").json()["sessions"]
    rows = [s for s in sessions if s.get("flow_id") == FLOW_ID]
    assert len(rows) == 1
    assert rows[0]["machine_id"] == MACHINE_B


# --------------------------------------------------------------------------
# cross-machine live-process guard
# --------------------------------------------------------------------------


@contextmanager
def _split_brain(monkeypatch):
    """node007 stops answering while still reporting the flow as RUNNING.

    The walltime-kill shape: the node's last frame froze at
    ``running``/not-``resumable`` (its aggregator's live-process shape) and no
    final status is ever coming, while node008 mounts the same disk and reports
    the same session as recoverable. The server cannot tell that frozen frame
    from a live one, so the only safe reading is the reachable one — otherwise a
    dead node wedges the session forever.
    """
    with _server(monkeypatch, offline_grace=OFFLINE_GRACE) as (client, app):
        ctx_a, _sock_a = _connect_daemon(
            client,
            app,
            MACHINE_A,
            ROOT_A,
            flow=_flow_payload(ROOT_A, status="running", resumable=False),
        )
        _wait_for_index_row(client, MACHINE_A)
        ctx_a.__exit__(None, None, None)
        _wait_offline(client, MACHINE_A)

        ctx_b, sock_b = _connect_daemon(
            client,
            app,
            MACHINE_B,
            ROOT_B,
            flow=_flow_payload(ROOT_B, status="running", resumable=True),
        )
        _wait_for_index_row(client, MACHINE_B)

        fleet = _Fleet(client, ctx_b, sock_b)
        try:
            yield fleet
        finally:
            if fleet._ctx_b is not None:
                fleet._ctx_b.__exit__(None, None, None)


@pytest.fixture()
def split_brain_fleet(monkeypatch):
    """A fleet where the unreachable node's last frame still reads RUNNING."""
    with _split_brain(monkeypatch) as fleet:
        yield fleet


def test_resume_reaches_the_live_node_despite_the_dead_running_snapshot(
    split_brain_fleet,
):
    """A node killed at its walltime must not wedge the flow.

    Its frozen ``running``/not-``resumable`` frame is indistinguishable from a
    live one, so honouring it would answer 409 "still running" for a process
    that no longer exists — the same dead-machine shadowing as the 404 this fix
    removes. The verdict is therefore read off node008, the machine the dispatch
    actually reaches, which reports the session as recoverable.
    """
    resp = split_brain_fleet.client.post(f"/api/flows/{FLOW_ID}/resume")
    assert resp.status_code == 202, resp.text
    assert resp.json()["machine_id"] == MACHINE_B
    spawn = _receive_until(split_brain_fleet.sock_b, protocol.MSG_SPAWN_FLOW)
    assert spawn.payload["resume_flow_id"] == FLOW_ID
    assert spawn.payload["project_root"] == ROOT_B


def test_end_reaches_the_live_node_despite_the_dead_running_snapshot(
    split_brain_fleet,
):
    """Same release for ``/end``: the operator must be able to close the session."""
    resp = split_brain_fleet.client.post(f"/api/flows/{FLOW_ID}/end")
    assert resp.status_code == 202, resp.text
    assert resp.json()["machine_id"] == MACHINE_B
    ended = _receive_until(split_brain_fleet.sock_b, protocol.MSG_END_SESSION)
    assert ended.payload["flow_id"] == FLOW_ID


def test_reading_history_still_works_while_the_flow_runs_elsewhere(
    split_brain_fleet,
):
    """Reading the chat log keeps going to the live daemon.

    Pulling the chat log of a session whose last reporter is unreachable is
    exactly what the operator does next, and node008 can serve it off the shared
    disk.
    """
    client = split_brain_fleet.client
    result: Dict[str, Any] = {}

    def do_get() -> None:
        result["resp"] = client.get(f"/api/history/{FLOW_ID}")

    worker = threading.Thread(target=do_get)
    worker.start()
    try:
        worker.join(timeout=DISPATCH_GRACE)
        assert worker.is_alive(), (
            "the detail request did not park on a pull: "
            f"{result.get('resp') and result['resp'].text}"
        )
        req = _receive_until(split_brain_fleet.sock_b, protocol.MSG_HISTORY_REQUEST)
        assert req.payload["project_root"] == ROOT_B
        split_brain_fleet.sock_b.send_text(
            protocol.make_history_data(
                FLOW_ID, protocol.HISTORY_MODE_FULL, RECORDS_B
            ).to_json()
        )
    finally:
        worker.join(timeout=POLL_DEADLINE)

    resp = result["resp"]
    assert resp.status_code == 200, resp.text
    assert resp.json()["machine_id"] == MACHINE_B


def test_flow_detail_reports_the_live_nodes_view_during_the_split(
    split_brain_fleet,
):
    """The read endpoint never renders the unreachable node's frozen snapshot.

    The sidebar must show node008's view, or it silently disagrees with the
    history pane beside it — both resolve through the same lookup.
    """
    body = split_brain_fleet.client.get(f"/api/flows/{FLOW_ID}").json()
    assert body["machine_id"] == MACHINE_B
    assert body["flow"]["project_root"] == ROOT_B


# --------------------------------------------------------------------------
# answering a blocked flow
# --------------------------------------------------------------------------


@pytest.fixture()
def blocked_fleet(monkeypatch):
    """node007 runs a flow blocked on a human call, then loses its link.

    The operator still wants to answer the call — which node008, on the same
    mount, can do by dropping the response file the ``se3 run`` drains.
    """
    with _server(monkeypatch, offline_grace=OFFLINE_GRACE) as (client, app):
        ctx_a, _sock_a = _connect_daemon(
            client, app, MACHINE_A, ROOT_A, flow=_blocked_flow(ROOT_A, live=True)
        )
        _wait_for_index_row(client, MACHINE_A)
        ctx_a.__exit__(None, None, None)
        _wait_offline(client, MACHINE_A)

        ctx_b, sock_b = _connect_daemon(
            client, app, MACHINE_B, ROOT_B, flow=_blocked_flow(ROOT_B, live=False)
        )
        _wait_for_index_row(client, MACHINE_B)

        fleet = _Fleet(client, ctx_b, sock_b)
        try:
            yield fleet
        finally:
            if fleet._ctx_b is not None:
                fleet._ctx_b.__exit__(None, None, None)


def test_respond_reaches_the_live_machine(blocked_fleet):
    """The answer must reach the machine that can still be reached.

    A response is a file drop — the daemon writes ``se3/calls/<id>.response``
    under the flow's project_root and the live process picks it up off the same
    shared disk. Routing it at the unreachable first reporter bounced the
    operator's reply with a 404 while the flow stayed blocked on the call.
    """
    resp = blocked_fleet.client.post(
        f"/api/flows/{FLOW_ID}/respond",
        json={"call_id": CALL_ID, "response": "go ahead"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["machine_id"] == MACHINE_B
    assert body["call_id"] == CALL_ID

    frame = _receive_until(blocked_fleet.sock_b, protocol.MSG_RESPOND_CALL)
    assert frame.payload["call_id"] == CALL_ID
    assert frame.payload["response"] == "go ahead"
    # node008's own root: the daemon writes the response file relative to a path
    # it can actually see.
    assert frame.payload["project_root"] == ROOT_B


def test_respond_defaults_to_the_live_machines_pending_call(blocked_fleet):
    """With no ``call_id`` the default is read off the machine actually dispatched to.

    Reading the pending list from one machine's snapshot and dispatching to
    another would answer a call id the target never saw.
    """
    resp = blocked_fleet.client.post(
        f"/api/flows/{FLOW_ID}/respond", json={"response": "ok"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["machine_id"] == MACHINE_B
    frame = _receive_until(blocked_fleet.sock_b, protocol.MSG_RESPOND_CALL)
    assert frame.payload["call_id"] == CALL_ID
    assert frame.payload["project_root"] == ROOT_B


def test_interject_reaches_the_live_machine(blocked_fleet):
    """Same for a mid-flow interjection: another file drop into the shared call dir."""
    resp = blocked_fleet.client.post(
        f"/api/flows/{FLOW_ID}/interject", json={"text": "switch to plan B"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["machine_id"] == MACHINE_B

    frame = _receive_until(blocked_fleet.sock_b, protocol.MSG_INTERJECT_FLOW)
    assert frame.payload["flow_id"] == FLOW_ID
    assert frame.payload["text"] == "switch to plan B"
    assert frame.payload["project_root"] == ROOT_B


def test_resume_and_respond_agree_on_the_target_machine(blocked_fleet):
    """One resolution serves every route, so the two cannot disagree.

    Guards against re-splitting the lookup: if a command path ever resolved
    differently from a read path, the operator would be answering the call on
    one node while the resume they trigger next goes to another.
    """
    resumed = blocked_fleet.client.post(f"/api/flows/{FLOW_ID}/resume")
    assert resumed.status_code == 202, resumed.text
    assert resumed.json()["machine_id"] == MACHINE_B

    answered = blocked_fleet.client.post(
        f"/api/flows/{FLOW_ID}/respond",
        json={"call_id": CALL_ID, "response": "yes"},
    )
    assert answered.status_code == 200, answered.text
    assert answered.json()["machine_id"] == MACHINE_B


def test_respond_with_no_live_machine_is_the_old_404(blocked_fleet):
    """Fallback parity: with the fleet dark the reply is refused as before."""
    blocked_fleet.disconnect_b()

    resp = blocked_fleet.client.post(
        f"/api/flows/{FLOW_ID}/respond",
        json={"call_id": CALL_ID, "response": "hello?"},
    )
    assert resp.status_code == 404, resp.text
    detail = resp.json()["detail"]
    assert "is not connected" in detail
    assert MACHINE_A in detail


def test_call_detail_pulls_from_the_live_machine(blocked_fleet):
    """``find_call_owner`` must route the prompt pull at a reachable daemon.

    Pre-fix this endpoint answered 503 ``machine 'node007' is not connected``
    for as long as the dead record lived, so the operator could never read the
    full prompt of the call they were being asked to answer.
    """
    client = blocked_fleet.client
    result: Dict[str, Any] = {}

    def do_get() -> None:
        result["resp"] = client.get(f"/api/calls/{CALL_ID}/detail")

    worker = threading.Thread(target=do_get)
    worker.start()
    try:
        # Resolving to the dead machine short-circuits to 503 in milliseconds
        # rather than parking on a pull; check that before reading the socket.
        worker.join(timeout=DISPATCH_GRACE)
        assert worker.is_alive(), (
            "the call detail request returned without pulling from the live "
            f"machine: {result.get('resp') and result['resp'].text}"
        )
        req = _receive_until(blocked_fleet.sock_b, protocol.MSG_DETAIL_REQUEST)
        assert req.payload["kind"] == protocol.DETAIL_KIND_CALL
        assert req.payload["target_id"] == CALL_ID
        assert req.payload["project_root"] == ROOT_B
        blocked_fleet.sock_b.send_text(
            protocol.make_detail_data(
                req.payload["request_id"],
                protocol.DETAIL_KIND_CALL,
                detail={"call_id": CALL_ID, "prompt": "the untruncated prompt"},
            ).to_json()
        )
    finally:
        worker.join(timeout=POLL_DEADLINE)

    resp = result["resp"]
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["machine_id"] == MACHINE_B
    assert body["call"]["prompt"] == "the untruncated prompt"


def test_history_detail_with_no_live_machine_still_serves_the_cache(fleet):
    """Nothing online ⇒ node007 again, and its bundle is still cached ⇒ 200.

    Parity, not a new behaviour: the fallback resolves to node007 exactly as the
    pre-fix code did, its cached bundle still matches that binding, and the read
    is served from cache with no daemon involved. Nothing that used to work
    while the fleet was dark stops working.
    """
    fleet.disconnect_b()

    resp = fleet.client.get(f"/api/history/{FLOW_ID}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["cached"] is True
    assert body["machine_id"] == MACHINE_A
    assert body["records"] == RECORDS_A


# --------------------------------------------------------------------------
# issue mirror — the same switch, on the path with no grace to expire
# --------------------------------------------------------------------------


ISSUE_ID = "I-42"
# Issues live in ``se3/issues/*.yaml`` under the project root, so on the shared
# mount BOTH nodes mirror the identical file. Unlike the flow roots above these
# must therefore be the same path — that sameness is the whole scenario.
ISSUE_ROOT = "/shared/jobs/proj-shared"


def _issue_payload(mirrored_by: str) -> Dict[str, Any]:
    """The issue as one node mirrors it; the title names which node read it."""
    return {
        "id": ISSUE_ID,
        "project_root": ISSUE_ROOT,
        "title": f"shared issue as read by {mirrored_by}",
        "description": "the job that moved nodes",
        "status": "open",
        "source": "human",
        "type": "bug",
        "created_at": "2026-07-24T09:00:00",
        "updated_at": "2026-07-24T10:00:00",
    }


def _connect_issue_daemon(client, app, machine_id: str) -> Tuple[Any, Any]:
    """Connect a stand-in daemon mirroring the shared issue; return (ctx, sock)."""
    ctx = client.websocket_connect("/ws")
    sock = ctx.__enter__()
    sock.send_text(authed_hello(app, machine_id, machine_id, "6.4.0"))
    protocol.decode(sock.receive_text())  # WELCOME
    sock.send_text(
        protocol.make_status_update(
            {
                "hostname": machine_id,
                "flows": [],
                "project_roots": [ISSUE_ROOT],
                "issues": [_issue_payload(machine_id)],
            }
        ).to_json()
    )
    return ctx, sock


def _wait_for_issue_mirror(client, machine_id: str) -> Dict[str, Any]:
    """Poll the issue list until *machine_id*'s mirror has landed.

    A setup barrier only — it waits for that machine's STATUS_UPDATE to have
    been applied, deliberately without asserting anything about collapsing, so
    each claim under test fails in its own test body.
    """
    deadline = time.monotonic() + POLL_DEADLINE
    seen: Any = None
    while time.monotonic() < deadline:
        issues = client.get("/api/issues").json()["issues"]
        rows = [i for i in issues if i.get("id") == ISSUE_ID]
        seen = [r.get("machine_id") for r in rows]
        for row in rows:
            if row.get("machine_id") == machine_id:
                return row
        time.sleep(0.02)
    raise AssertionError(
        f"{machine_id} never mirrored {ISSUE_ID} (saw {seen})"
    )


@pytest.fixture()
def issue_fleet(client_and_app):
    """node007 mirrored the issue and died; node008 mirrors the same YAML file."""
    client, app = client_and_app

    ctx_a, _sock_a = _connect_issue_daemon(client, app, MACHINE_A)
    _wait_for_issue_mirror(client, MACHINE_A)
    ctx_a.__exit__(None, None, None)
    _wait_offline(client, MACHINE_A)

    ctx_b, sock_b = _connect_issue_daemon(client, app, MACHINE_B)
    _wait_for_issue_mirror(client, MACHINE_B)

    fleet = _Fleet(client, ctx_b, sock_b)
    try:
        yield fleet
    finally:
        if fleet._ctx_b is not None:
            fleet._ctx_b.__exit__(None, None, None)


def test_start_flow_from_issue_dispatches_to_the_live_machine(issue_fleet):
    """The operator clicks "start flow from issue" after the job moved nodes.

    Pre-fix the issue resolved to node007 and the answer was a 404 ``machine
    'node007' owning issue 'I-42' is not connected`` — and unlike the presence
    flag nothing ever ages a dead machine's issue mirror out, so that 404 was
    permanent even though node008 can read and write the identical YAML file.
    """
    resp = issue_fleet.client.post(
        "/api/flows", json={"from_issue_id": ISSUE_ID}
    )
    assert resp.status_code == 202, resp.text
    body = resp.json()
    assert body["machine_id"] == MACHINE_B
    assert body["from_issue_id"] == ISSUE_ID

    spawn = _receive_until(issue_fleet.sock_b, protocol.MSG_SPAWN_FLOW)
    assert spawn.payload["from_issue_id"] == ISSUE_ID
    assert spawn.payload["project_root"] == ISSUE_ROOT


def test_issue_list_offers_the_issue_once(issue_fleet):
    """Both nodes mirror the same YAML file; the operator sees one row."""
    issues = issue_fleet.client.get("/api/issues").json()["issues"]
    rows = [i for i in issues if i.get("id") == ISSUE_ID]
    assert len(rows) == 1
    assert rows[0]["machine_id"] == MACHINE_B
    assert rows[0]["title"] == f"shared issue as read by {MACHINE_B}"


def test_issue_detail_resolves_to_the_live_machine(issue_fleet):
    """``GET /api/issues/{id}`` names the machine a write would be routed to."""
    resp = issue_fleet.client.get(f"/api/issues/{ISSUE_ID}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["machine_id"] == MACHINE_B
    assert body["project_root"] == ISSUE_ROOT


def test_issue_write_with_no_live_machine_is_the_old_error(issue_fleet):
    """With every daemon gone the issue routes as before: node007, unreachable.

    Parity for the fallback pass — the issue is still known (not a 404 "not
    found"); the machine holding it simply cannot be reached.
    """
    issue_fleet.disconnect_b()

    resp = issue_fleet.client.post(
        "/api/flows", json={"from_issue_id": ISSUE_ID}
    )
    assert resp.status_code == 404, resp.text
    detail = resp.json()["detail"]
    assert "is not connected" in detail
    # Insertion order decides among unreachable candidates, unchanged.
    assert MACHINE_A in detail

    closed = issue_fleet.client.post(
        f"/api/issues/{ISSUE_ID}/reopen", json={}
    )
    assert closed.status_code == 503, closed.text
    assert MACHINE_A in closed.json()["detail"]
