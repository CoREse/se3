"""Presence debounce: a daemon that reconnects within the grace window must
never flap the WebUI online badge; only a daemon gone past the window is shown
offline.

Two layers are covered:

* :class:`PresenceDebouncer` in isolation — schedule/cancel/shutdown semantics
  with a small injectable delay so the tests do not wait real seconds.
* :func:`handle_daemon_connection` end to end — that a disconnect defers the
  ``mark_offline`` + push, and a reconnect cancels the pending offline task.

Follows the sibling server tests' convention of driving coroutines with
``asyncio.run`` from plain sync test functions rather than the pytest-asyncio
marker.
"""

from __future__ import annotations

import asyncio

from tianluo.daemon import protocol
from tianluo.server.state import ServerState
from tianluo.server.ws import (
    ConnectionManager,
    PresenceDebouncer,
    handle_daemon_connection,
)


# --------------------------------------------------------------------------- #
# PresenceDebouncer unit tests
# --------------------------------------------------------------------------- #


def test_schedule_offline_fires_after_delay():
    """The offline action runs once the grace window elapses uninterrupted."""

    async def scenario():
        deb = PresenceDebouncer(delay=0.02)
        calls = []

        async def action():
            calls.append(1)

        deb.schedule_offline("m1", action)
        inside = list(calls)  # still inside the grace window
        await asyncio.sleep(0.05)
        return inside, calls

    inside, calls = asyncio.run(scenario())
    assert inside == []
    assert calls == [1]  # fired exactly once


def test_cancel_prevents_offline():
    """A cancel inside the window stops the action from ever running."""

    async def scenario():
        deb = PresenceDebouncer(delay=0.05)
        calls = []

        async def action():
            calls.append(1)

        deb.schedule_offline("m1", action)
        await asyncio.sleep(0.01)
        deb.cancel("m1")  # reconnect within the grace window
        await asyncio.sleep(0.08)
        return calls

    assert asyncio.run(scenario()) == []


def test_reschedule_supersedes_without_leak():
    """Re-arming the same machine cancels the prior task and fires once."""

    async def scenario():
        deb = PresenceDebouncer(delay=0.03)
        calls = []

        async def action():
            calls.append(1)

        deb.schedule_offline("m1", action)
        deb.schedule_offline("m1", action)  # supersedes the first
        tracked = len(deb._pending)  # only one task tracked
        await asyncio.sleep(0.06)
        return tracked, calls

    tracked, calls = asyncio.run(scenario())
    assert tracked == 1
    assert calls == [1]  # the superseded task did not also fire


def test_shutdown_cancels_pending():
    """shutdown() cancels every pending offline task and clears the table."""

    async def scenario():
        deb = PresenceDebouncer(delay=0.05)
        calls = []

        async def action():
            calls.append(1)

        deb.schedule_offline("m1", action)
        deb.schedule_offline("m2", action)
        deb.shutdown()
        empty = dict(deb._pending)
        await asyncio.sleep(0.08)
        return empty, calls

    empty, calls = asyncio.run(scenario())
    assert empty == {}
    assert calls == []


# --------------------------------------------------------------------------- #
# handle_daemon_connection integration
# --------------------------------------------------------------------------- #


class _FakeWebSocket:
    """Minimal daemon-side WebSocket stub for :func:`handle_daemon_connection`.

    Delivers a single HELLO then raises to end the receive loop, mimicking a
    disconnect. Sent frames are collected for inspection.
    """

    def __init__(self, machine_id: str):
        self._inbound = [protocol.make_hello(machine_id, "host", "1.0.0").to_json()]
        self.sent = []

    async def accept(self):
        pass

    async def receive_text(self):
        if self._inbound:
            return self._inbound.pop(0)
        raise RuntimeError("daemon disconnected")

    async def send_text(self, text):
        self.sent.append(text)

    async def close(self):
        pass


async def _run_connection(state, manager, deb, machine_id):
    ws = _FakeWebSocket(machine_id)
    await handle_daemon_connection(
        ws, manager, state, hub=None, presence_debouncer=deb
    )


def test_disconnect_defers_offline_still_online():
    """After a disconnect the machine record stays online during the grace."""

    async def scenario():
        state = ServerState()
        manager = ConnectionManager()
        deb = PresenceDebouncer(delay=0.5)

        await _run_connection(state, manager, deb, "m1")

        record = await state.get_machine("m1")
        pending = len(deb._pending)
        deb.shutdown()
        return record, pending

    record, pending = asyncio.run(scenario())
    assert record is not None
    assert record["online"] is True  # not marked offline yet
    assert pending == 1  # a grace task is armed


def test_offline_after_grace_expires():
    """With no reconnect the grace task marks the machine offline exactly once."""

    async def scenario():
        state = ServerState()
        manager = ConnectionManager()
        deb = PresenceDebouncer(delay=0.02)

        await _run_connection(state, manager, deb, "m1")
        await asyncio.sleep(0.06)

        record = await state.get_machine("m1")
        return record, dict(deb._pending)

    record, pending = asyncio.run(scenario())
    assert record is not None
    assert record["online"] is False
    assert pending == {}  # task cleared after firing


def test_reconnect_within_grace_stays_online():
    """A reconnect inside the window cancels the pending offline; stays online."""

    async def scenario():
        state = ServerState()
        manager = ConnectionManager()
        deb = PresenceDebouncer(delay=0.5)

        # First session drops → arms a 0.5s grace task.
        await _run_connection(state, manager, deb, "m1")
        armed = len(deb._pending)

        # Reconnect quickly (well within the window) → cancels the grace task.
        # The second session also disconnects and re-arms a fresh grace task,
        # but the machine was never marked offline in between.
        await _run_connection(state, manager, deb, "m1")
        mid = await state.get_machine("m1")

        deb.shutdown()
        await asyncio.sleep(0.03)
        after = await state.get_machine("m1")
        return armed, mid, after

    armed, mid, after = asyncio.run(scenario())
    assert armed == 1
    assert mid is not None and mid["online"] is True
    # Still online — no offline ever fired during the fast reconnect churn.
    assert after["online"] is True


def test_stale_grace_task_defused_by_live_reconnect():
    """A grace task armed by an old handler AFTER a newer connection registered
    must NOT flip an online, connected machine offline.

    Reproduces the silent-drop overlap: the daemon redials and registers a fresh
    connection while the old handler is still parked in receive(); when the old
    socket finally wakes, its ``finally`` arms a grace task that nothing cancels
    (the reconnect's cancel() ran earlier as a no-op). The ``_go_offline``
    backstop must see the live connection and leave the machine online.
    """

    async def scenario():
        state = ServerState()
        manager = ConnectionManager()
        deb = PresenceDebouncer(delay=0.03)

        # Old session drops → arms a grace task.
        await _run_connection(state, manager, deb, "m1")
        armed = len(deb._pending)

        # Simulate the newer connection that already registered and is LIVE:
        # online in state and present in the manager pool. (cancel() at its
        # register time was a no-op because nothing was pending yet — here we
        # leave the stale grace task armed on purpose.)
        await state.register_machine("m1", "host", "1.0.0")
        live_ws = _FakeWebSocket("m1")
        await manager.connect("m1", live_ws)

        # Let the stale grace task fire; the backstop guard must defuse it.
        await asyncio.sleep(0.06)
        record = await state.get_machine("m1")
        deb.shutdown()
        return armed, record

    armed, record = asyncio.run(scenario())
    assert armed == 1
    assert record is not None
    # Still online — the live reconnected daemon was never flipped offline.
    assert record["online"] is True


def test_no_debouncer_marks_offline_immediately():
    """Without a debouncer the legacy immediate-offline behaviour is kept."""

    async def scenario():
        state = ServerState()
        manager = ConnectionManager()

        await _run_connection(state, manager, None, "m1")
        return await state.get_machine("m1")

    record = asyncio.run(scenario())
    assert record is not None
    assert record["online"] is False
