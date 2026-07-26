"""Online-first flow→machine resolution in :class:`ServerState`.

Reproduces the shared-filesystem multi-machine deployment (an HPC cluster where
a job ends on node007 and the next one starts on node008 against the same disk):
both daemons report the SAME ``flow_id``, and the server keeps the disconnected
machine's flows / history index after ``mark_offline``. Resolving in plain
insertion order let the dead machine shadow the live one forever, which the
WebUI saw as a 404 on ``GET /api/history/{flow_id}`` and a
``machine ... is not connected`` 404 on resume.

The invariants locked here:

* every resolution entry point prefers a REACHABLE machine reporting the flow —
  reachability being live socket state, NOT the 60 s-debounced ``online``
  presence flag, so the switch works inside the offline-grace window too;
* a reachable machine wins even when it only knows the flow from its live set
  while the unreachable one holds the (more authoritative) history-index entry;
* the command paths (``is_flow_resumable`` / ``is_flow_endable``, i.e. resume
  and end) resolve through the very same preference and judge their verdict on
  the resolved machine's own snapshot, so a node killed mid-run — whose frozen
  frame still reads ``running`` / not-``resumable`` — cannot answer 409 "still
  running" for a process that no longer exists;
* with no reachable candidate at all, resolution falls back to exactly the
  pre-fix answer;
* owner scoping is never bypassed by the reachability preference;
* ``get_history_index`` collapses the resulting multi-machine duplicates of one
  ``flow_id`` to a single row, picking the same machine resolution would;
* the issue mirror — the parallel resolution behind ``se3/issues/*.yaml``, which
  every ISSUE_COMMAND and "start flow from issue" SPAWN_FLOW routes off — obeys
  the same preference and the same ``(project_root, id)`` collapse. Nothing ages
  a dead machine's issue mirror out, so there the mis-resolution is permanent
  rather than merely lasting the presence grace.

Follows the sibling server tests' convention of driving coroutines with
``asyncio.run`` from plain sync test functions rather than the pytest-asyncio
marker.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from tianluo.server.state import ServerState


FLOW_ID = "flow-shared-fs"
OWNER = "owner-1"
OTHER_OWNER = "owner-2"

ROOT_A = "/shared/proj@node007"
ROOT_B = "/shared/proj@node008"
ROOT_C = "/shared/proj@intruder"


def _flow_payload(project_root: str) -> Dict[str, Any]:
    """A paused-but-resumable flow snapshot, as a daemon would report it."""
    return {
        "flow_id": FLOW_ID,
        "project_root": project_root,
        "task_description": "shared filesystem session",
        "status": "paused",
        "resumable": True,
        "updated_at": "2026-07-24T10:00:00",
    }


def _session_meta(project_root: str, updated_at: str) -> Dict[str, Any]:
    """A ``MSG_HISTORY_INDEX`` session-meta entry for the shared flow."""
    return {
        "flow_id": FLOW_ID,
        "project_root": project_root,
        "task_description": "shared filesystem session",
        "status": "paused",
        "updated_at": updated_at,
    }


def _running_flow(project_root: str, *, live: bool) -> Dict[str, Any]:
    """A RUNNING flow snapshot, as the reporting daemon's live gate computes it.

    ``live=True`` is the aggregator's live-process case: ``_resumable_with_live_gate``
    clears ``resumable`` for a RUNNING flow whose root still has a live ``se3 run``.
    ``live=False`` is the interrupted-but-recoverable case — the same status, but
    the process is gone, so the flow stays resumable.
    """
    payload = _flow_payload(project_root)
    payload["status"] = "running"
    payload["resumable"] = not live
    return payload


async def _seed_machine(
    state: ServerState,
    machine_id: str,
    *,
    owner: Optional[str],
    project_root: str,
    with_flow: bool = True,
    with_index: bool = True,
    index_updated_at: str = "2026-07-24T10:00:00",
    offline: bool = False,
    flow: Optional[Dict[str, Any]] = None,
) -> None:
    """Register *machine_id*, let it report the shared flow, maybe disconnect."""
    await state.register_machine(machine_id, owner_id=owner)
    await state.update_status(
        machine_id,
        {
            "flows": (
                [flow if flow is not None else _flow_payload(project_root)]
                if with_flow
                else []
            )
        },
    )
    if with_index:
        await state.update_history_index(
            machine_id, [_session_meta(project_root, index_updated_at)]
        )
    if offline:
        await state.mark_offline(machine_id)


async def _resolve_all(
    state: ServerState, *, owner: Optional[str] = OWNER
) -> Dict[str, Any]:
    """Run every flow→machine resolution entry point once."""
    flow = await state.get_flow(FLOW_ID, owner=owner)
    resumable = await state.is_flow_resumable(FLOW_ID, owner=owner)
    endable = await state.is_flow_endable(FLOW_ID, owner=owner)
    return {
        "get_flow": flow[0] if flow is not None else None,
        "find_machine_for_flow": await state.find_machine_for_flow(
            FLOW_ID, owner=owner
        ),
        "is_flow_resumable": resumable[0] if resumable is not None else None,
        "is_flow_endable": endable[0] if endable is not None else None,
        "find_machine_for_history_flow": (
            await state.find_machine_for_history_flow(FLOW_ID, owner=owner)
        ),
        "project_root": await state.get_history_flow_project_root(
            FLOW_ID, owner=owner
        ),
    }


def _machines(resolved: Dict[str, Any]) -> List[Any]:
    """The five machine-id answers (``project_root`` is asserted separately)."""
    return [v for k, v in resolved.items() if k != "project_root"]


# --------------------------------------------------------------------------- #
# online-first resolution
# --------------------------------------------------------------------------- #


def test_online_machine_wins_over_offline_reporter_of_same_flow():
    """node007 (offline, registered first) must not shadow node008 (online)."""

    async def scenario():
        state = ServerState()
        await _seed_machine(
            state, "node007", owner=OWNER, project_root=ROOT_A, offline=True
        )
        await _seed_machine(state, "node008", owner=OWNER, project_root=ROOT_B)
        return await _resolve_all(state)

    resolved = asyncio.run(scenario())
    assert _machines(resolved) == ["node008"] * 5
    assert resolved["project_root"] == ROOT_B


def test_online_live_flow_beats_offline_history_index_and_cached_bundle():
    """Servability outranks evidence strength.

    node008 has just connected and pushed STATUS_UPDATE but not yet its
    HISTORY_INDEX, while the dead node007 holds both the index entry and the
    cached bundle. Resolving stage-by-stage would still pick node007 and 404;
    the two whole passes pick node008.
    """

    async def scenario():
        state = ServerState()
        await _seed_machine(
            state, "node007", owner=OWNER, project_root=ROOT_A, offline=True
        )
        # A cached bundle produced by the now-dead machine (stage 2).
        await state.append_history(
            FLOW_ID,
            "full",
            [{"step_id": "01_discovery", "role": "assistant", "content": "hi"}],
            machine_id="node007",
        )
        await _seed_machine(
            state,
            "node008",
            owner=OWNER,
            project_root=ROOT_B,
            with_index=False,
        )
        return await _resolve_all(state)

    resolved = asyncio.run(scenario())
    assert _machines(resolved) == ["node008"] * 5
    # The root handed to the daemon about to serve the pull must be its own.
    assert resolved["project_root"] == ROOT_B


def test_offline_index_still_wins_over_online_machine_without_the_flow():
    """The online preference only applies to machines that HAVE the flow."""

    async def scenario():
        state = ServerState()
        await _seed_machine(
            state, "node007", owner=OWNER, project_root=ROOT_A, offline=True
        )
        # An unrelated online machine that never ran this flow.
        await state.register_machine("node009", owner_id=OWNER)
        await state.update_status("node009", {"flows": []})
        return await _resolve_all(state)

    resolved = asyncio.run(scenario())
    assert _machines(resolved) == ["node007"] * 5
    assert resolved["project_root"] == ROOT_A


# --------------------------------------------------------------------------- #
# fallback parity
# --------------------------------------------------------------------------- #


def test_all_offline_falls_back_to_pre_fix_answer():
    """With nothing online, resolution is the first insertion-order match."""

    async def scenario():
        state = ServerState()
        await _seed_machine(
            state, "node007", owner=OWNER, project_root=ROOT_A, offline=True
        )
        await _seed_machine(
            state, "node008", owner=OWNER, project_root=ROOT_B, offline=True
        )
        return await _resolve_all(state)

    resolved = asyncio.run(scenario())
    assert _machines(resolved) == ["node007"] * 5
    assert resolved["project_root"] == ROOT_A


def test_unknown_flow_still_resolves_to_nothing():
    """The extra pass must not invent a machine for a flow nobody reports."""

    async def scenario():
        state = ServerState()
        await _seed_machine(state, "node008", owner=OWNER, project_root=ROOT_B)
        return {
            "flow": await state.get_flow("no-such-flow", owner=OWNER),
            "history": await state.find_machine_for_history_flow(
                "no-such-flow", owner=OWNER
            ),
            "root": await state.get_history_flow_project_root(
                "no-such-flow", owner=OWNER
            ),
        }

    resolved = asyncio.run(scenario())
    assert resolved == {"flow": None, "history": None, "root": None}


# --------------------------------------------------------------------------- #
# owner isolation
# --------------------------------------------------------------------------- #


def test_online_preference_never_crosses_owner_boundary():
    """An online machine of another owner is invisible, online or not."""

    async def scenario():
        state = ServerState()
        await _seed_machine(
            state, "node007", owner=OWNER, project_root=ROOT_A, offline=True
        )
        # Same flow_id reported by an ONLINE machine in a different trust domain
        # (machine_id/flow_id are not secrets — a collision must not leak).
        await _seed_machine(
            state, "intruder", owner=OTHER_OWNER, project_root=ROOT_C
        )
        return (
            await _resolve_all(state, owner=OWNER),
            await _resolve_all(state, owner=OTHER_OWNER),
        )

    scoped, other = asyncio.run(scenario())
    assert _machines(scoped) == ["node007"] * 5
    assert scoped["project_root"] == ROOT_A
    assert _machines(other) == ["intruder"] * 5
    assert other["project_root"] == ROOT_C


def test_unscoped_admin_view_still_prefers_the_online_machine():
    """``owner=None`` keeps its see-everything semantics under the new order."""

    async def scenario():
        state = ServerState()
        await _seed_machine(
            state, "node007", owner=OWNER, project_root=ROOT_A, offline=True
        )
        await _seed_machine(
            state, "node008", owner=OTHER_OWNER, project_root=ROOT_B
        )
        return await _resolve_all(state, owner=None)

    resolved = asyncio.run(scenario())
    assert _machines(resolved) == ["node008"] * 5
    assert resolved["project_root"] == ROOT_B


# --------------------------------------------------------------------------- #
# history index dedupe
# --------------------------------------------------------------------------- #


def _row(
    flow_id: Optional[str], updated_at: str, **extra: Any
) -> Dict[str, Any]:
    """A history-index row; *flow_id* ``None`` models a malformed daemon row."""
    row: Dict[str, Any] = {"updated_at": updated_at, "status": "paused"}
    if flow_id is not None:
        row["flow_id"] = flow_id
    row.update(extra)
    return row


def _by_flow(
    entries: List[Dict[str, Any]], flow_id: str
) -> List[Dict[str, Any]]:
    return [e for e in entries if e.get("flow_id") == flow_id]


def test_history_index_collapses_shared_flow_onto_the_online_machine():
    """One row per session, reported by the machine that can actually serve it.

    node007's index entry is even the newer of the two, so only the online
    preference can produce node008 here — and it must, or the list would offer
    a machine_id the detail fetch never routes to.
    """

    async def scenario():
        state = ServerState()
        await _seed_machine(
            state,
            "node007",
            owner=OWNER,
            project_root=ROOT_A,
            index_updated_at="2026-07-24T12:00:00",
            offline=True,
        )
        await _seed_machine(
            state,
            "node008",
            owner=OWNER,
            project_root=ROOT_B,
            index_updated_at="2026-07-24T11:00:00",
        )
        return await state.get_history_index(owner=OWNER)

    entries = asyncio.run(scenario())
    assert len(entries) == 1
    assert entries[0]["machine_id"] == "node008"
    assert entries[0]["project_root"] == ROOT_B


def test_history_index_dedupe_falls_back_to_newest_updated_at():
    """With both reporters in the same online state, the fresher row wins."""

    async def scenario():
        both_offline = ServerState()
        both_online = ServerState()
        for state, offline in ((both_offline, True), (both_online, False)):
            await _seed_machine(
                state,
                "node007",
                owner=OWNER,
                project_root=ROOT_A,
                index_updated_at="2026-07-24T09:00:00",
                offline=offline,
            )
            await _seed_machine(
                state,
                "node008",
                owner=OWNER,
                project_root=ROOT_B,
                index_updated_at="2026-07-24T13:00:00",
                offline=offline,
            )
        return (
            await both_offline.get_history_index(owner=OWNER),
            await both_online.get_history_index(owner=OWNER),
        )

    offline_entries, online_entries = asyncio.run(scenario())
    for entries in (offline_entries, online_entries):
        assert len(entries) == 1
        assert entries[0]["machine_id"] == "node008"
        assert entries[0]["updated_at"] == "2026-07-24T13:00:00"


def test_history_index_keeps_rows_without_a_flow_id():
    """An unaddressable row cannot be merged — never collapse those together."""

    async def scenario():
        state = ServerState()
        await state.register_machine("node007", owner_id=OWNER)
        await state.update_history_index(
            "node007",
            [
                _row(None, "2026-07-24T08:00:00"),
                _row("", "2026-07-24T07:00:00"),
            ],
        )
        await state.register_machine("node008", owner_id=OWNER)
        await state.update_history_index(
            "node008", [_row(None, "2026-07-24T06:00:00")]
        )
        return await state.get_history_index(owner=OWNER)

    entries = asyncio.run(scenario())
    assert len(entries) == 3
    assert [e["machine_id"] for e in entries] == [
        "node007",
        "node007",
        "node008",
    ]


def test_history_index_dedupe_preserves_ordering_and_other_flows():
    """Only same-id rows collapse; the list stays ``updated_at``-descending."""

    async def scenario():
        state = ServerState()
        await state.register_machine("node007", owner_id=OWNER)
        await state.update_history_index(
            "node007",
            [
                _row(FLOW_ID, "2026-07-24T10:00:00", project_root=ROOT_A),
                _row("flow-old", "2026-07-20T10:00:00"),
            ],
        )
        await state.mark_offline("node007")
        await state.register_machine("node008", owner_id=OWNER)
        await state.update_history_index(
            "node008",
            [
                _row(FLOW_ID, "2026-07-24T10:00:00", project_root=ROOT_B),
                _row("flow-new", "2026-07-24T18:00:00"),
            ],
        )
        return await state.get_history_index(owner=OWNER)

    entries = asyncio.run(scenario())
    assert len(entries) == 3
    updated = [str(e["updated_at"]) for e in entries]
    assert updated == sorted(updated, reverse=True)
    assert entries[0]["flow_id"] == "flow-new"
    assert entries[-1]["flow_id"] == "flow-old"
    # Equal timestamps: only the online preference decides this one.
    shared = _by_flow(entries, FLOW_ID)
    assert len(shared) == 1
    assert shared[0]["machine_id"] == "node008"
    assert shared[0]["project_root"] == ROOT_B
    # A flow only the offline machine ever had is still listed.
    assert _by_flow(entries, "flow-old")[0]["machine_id"] == "node007"


# --------------------------------------------------------------------------- #
# reachability vs. the debounced presence flag
# --------------------------------------------------------------------------- #


def _probe(*reachable: str):
    """A connectivity probe reporting exactly *reachable* as connected."""
    return lambda machine_id: machine_id in reachable


def test_dead_machine_inside_the_presence_grace_does_not_shadow_the_live_one():
    """The 60 s offline grace must not gate the switch.

    ``mark_offline`` fires only after ``PresenceDebouncer``'s grace window, so
    right after node007's daemon dies its record STILL reads ``online=True``.
    Ordering on that flag would keep routing everything at a machine no frame
    can reach for a full minute — the very 404 this fix removes, merely delayed.
    """

    async def scenario():
        state = ServerState()
        # Neither machine is marked offline: node007 is inside its grace window.
        await _seed_machine(state, "node007", owner=OWNER, project_root=ROOT_A)
        await _seed_machine(state, "node008", owner=OWNER, project_root=ROOT_B)
        state.set_connectivity_probe(_probe("node008"))
        assert state._machines["node007"].online is True
        return (
            await _resolve_all(state),
            await state.get_history_index(owner=OWNER),
        )

    resolved, entries = asyncio.run(scenario())
    assert _machines(resolved) == ["node008"] * 5
    assert resolved["project_root"] == ROOT_B
    # The list must name the machine resolution actually routes to.
    assert [e["machine_id"] for e in entries] == ["node008"]


def test_a_reconnected_machine_wins_before_its_record_is_flipped_back():
    """The mirror case: connected socket, record not yet refreshed to online.

    Reachability is the routing truth in BOTH directions — a machine whose
    record still carries a stale ``online=False`` is routable the moment its
    socket is back, without waiting for the next STATUS_UPDATE to flip the flag.
    """

    async def scenario():
        state = ServerState()
        await _seed_machine(
            state, "node007", owner=OWNER, project_root=ROOT_A, offline=True
        )
        await _seed_machine(
            state, "node008", owner=OWNER, project_root=ROOT_B, offline=True
        )
        state.set_connectivity_probe(_probe("node008"))
        return await _resolve_all(state)

    resolved = asyncio.run(scenario())
    assert _machines(resolved) == ["node008"] * 5
    assert resolved["project_root"] == ROOT_B


def test_probe_failure_degrades_to_the_presence_flag():
    """A raising probe must not declare the whole fleet unreachable."""

    def _boom(machine_id: str) -> bool:
        raise RuntimeError("connection pool exploded")

    async def scenario():
        state = ServerState()
        await _seed_machine(
            state, "node007", owner=OWNER, project_root=ROOT_A, offline=True
        )
        await _seed_machine(state, "node008", owner=OWNER, project_root=ROOT_B)
        state.set_connectivity_probe(_boom)
        return await _resolve_all(state)

    resolved = asyncio.run(scenario())
    assert _machines(resolved) == ["node008"] * 5


def test_without_a_probe_the_presence_flag_still_decides():
    """A bare ServerState (unit tests, tooling) keeps its previous semantics."""

    async def scenario():
        state = ServerState()
        await _seed_machine(
            state, "node007", owner=OWNER, project_root=ROOT_A, offline=True
        )
        await _seed_machine(state, "node008", owner=OWNER, project_root=ROOT_B)
        return await _resolve_all(state)

    assert _machines(asyncio.run(scenario())) == ["node008"] * 5


# --------------------------------------------------------------------------- #
# RUNNING flows on a shared filesystem
# --------------------------------------------------------------------------- #


def test_running_flow_resolves_to_the_reachable_machine():
    """A node killed mid-run must not keep the flow pinned to itself.

    node007 hit its HPC walltime, so its last frame froze the flow as RUNNING
    and (per its own live gate) NOT resumable, and it never sends a final
    status. node008 then mounts the same disk and reports the flow as recover-
    able. Preferring node007's frozen snapshot here would answer 409 "still
    running" for a process that no longer exists — the same dead-machine
    shadowing this resolution exists to remove, just with a different status
    code. Every entry point, reads and command gates alike, therefore resolves
    to the machine that can actually be reached.
    """

    async def scenario():
        state = ServerState()
        await _seed_machine(
            state,
            "node007",
            owner=OWNER,
            project_root=ROOT_A,
            flow=_running_flow(ROOT_A, live=True),
        )
        await _seed_machine(
            state,
            "node008",
            owner=OWNER,
            project_root=ROOT_B,
            flow=_running_flow(ROOT_B, live=False),
        )
        state.set_connectivity_probe(_probe("node008"))
        return await _resolve_all(state)

    resolved = asyncio.run(scenario())
    assert _machines(resolved) == ["node008"] * 5
    assert resolved["project_root"] == ROOT_B


def test_flow_detail_read_shows_the_reachable_machines_view():
    """``GET /api/flows/{id}`` must never render an unreachable node's snapshot.

    The sidebar (status / current_step / progress) and the history pane must
    agree, and both resolve reachable-first.
    """

    async def scenario():
        state = ServerState()
        stale = _running_flow(ROOT_A, live=True)
        stale["current_step"] = "implement"
        stale["updated_at"] = "2026-07-24T10:00:00"
        fresh = _running_flow(ROOT_B, live=False)
        fresh["current_step"] = "verify"
        fresh["updated_at"] = "2026-07-24T18:00:00"
        await _seed_machine(
            state, "node007", owner=OWNER, project_root=ROOT_A, flow=stale
        )
        await _seed_machine(
            state, "node008", owner=OWNER, project_root=ROOT_B, flow=fresh
        )
        state.set_connectivity_probe(_probe("node008"))
        return await state.get_flow(FLOW_ID, owner=OWNER)

    machine_id, flow = asyncio.run(scenario())
    assert machine_id == "node008"
    assert flow["current_step"] == "verify"
    assert flow["project_root"] == ROOT_B


def test_running_flow_verdicts_are_read_off_the_resolved_machine():
    """The resume/end verdict must judge the snapshot it will dispatch to.

    Resolution and gating must not disagree: node008 is the machine the command
    reaches, so its own view of the flow — recoverable, hence resumable — is the
    one that decides, not the dead node's frozen ``running``.
    """

    async def scenario():
        state = ServerState()
        await _seed_machine(
            state,
            "node007",
            owner=OWNER,
            project_root=ROOT_A,
            flow=_running_flow(ROOT_A, live=True),
            offline=True,
        )
        await _seed_machine(
            state,
            "node008",
            owner=OWNER,
            project_root=ROOT_B,
            flow=_running_flow(ROOT_B, live=False),
        )
        state.set_connectivity_probe(_probe("node008"))
        return (
            await state.is_flow_resumable(FLOW_ID, owner=OWNER),
            await state.is_flow_endable(FLOW_ID, owner=OWNER),
        )

    resumable, endable = asyncio.run(scenario())
    assert resumable is not None and resumable[0] == "node008"
    assert resumable[1]["project_root"] == ROOT_B
    assert endable is not None and endable[0] == "node008"


def test_both_machines_reachable_keeps_insertion_order():
    """With every reporter connected the ordering is exactly the pre-fix one.

    The reachability preference only ever reorders across the reachable /
    unreachable boundary — it must not reshuffle a fleet that is entirely up.
    """

    async def scenario():
        state = ServerState()
        await _seed_machine(
            state,
            "node007",
            owner=OWNER,
            project_root=ROOT_A,
            flow=_running_flow(ROOT_A, live=True),
        )
        await _seed_machine(
            state,
            "node008",
            owner=OWNER,
            project_root=ROOT_B,
            flow=_running_flow(ROOT_B, live=False),
        )
        state.set_connectivity_probe(_probe("node007", "node008"))
        return await _resolve_all(state)

    resolved = asyncio.run(scenario())
    assert resolved["get_flow"] == "node007"
    # node007 reports a live RUNNING flow: neither resumable-flagged nor in
    # RESUMABLE_STATUSES, so the caller answers 409 "still running".
    assert resolved["is_flow_resumable"] is None
    assert resolved["is_flow_endable"] == "node007"


def test_interrupted_running_flow_still_switches_machines():
    """The commonest hand-off shape: the job died, the next node took over."""

    async def scenario():
        state = ServerState()
        await _seed_machine(
            state,
            "node007",
            owner=OWNER,
            project_root=ROOT_A,
            flow=_running_flow(ROOT_A, live=False),
            offline=True,
        )
        await _seed_machine(
            state,
            "node008",
            owner=OWNER,
            project_root=ROOT_B,
            flow=_running_flow(ROOT_B, live=False),
        )
        state.set_connectivity_probe(_probe("node008"))
        return await _resolve_all(state)

    resolved = asyncio.run(scenario())
    assert _machines(resolved) == ["node008"] * 5


def test_running_flow_resolution_never_crosses_owner_boundary():
    """Another owner's reachable machine must stay invisible to this owner."""

    async def scenario():
        state = ServerState()
        await _seed_machine(
            state,
            "intruder",
            owner=OTHER_OWNER,
            project_root=ROOT_C,
            flow=_running_flow(ROOT_C, live=True),
        )
        await _seed_machine(state, "node008", owner=OWNER, project_root=ROOT_B)
        state.set_connectivity_probe(_probe("node008", "intruder"))
        return await _resolve_all(state)

    resolved = asyncio.run(scenario())
    assert _machines(resolved) == ["node008"] * 5


def test_single_machine_live_flow_resolution_is_unchanged():
    """Baseline: with one reporter no answer can change."""

    async def scenario():
        state = ServerState()
        await _seed_machine(
            state,
            "node007",
            owner=OWNER,
            project_root=ROOT_A,
            flow=_running_flow(ROOT_A, live=True),
        )
        state.set_connectivity_probe(_probe("node007"))
        return await _resolve_all(state)

    resolved = asyncio.run(scenario())
    assert resolved["get_flow"] == "node007"
    assert resolved["is_flow_resumable"] is None
    assert resolved["is_flow_endable"] == "node007"


# --------------------------------------------------------------------------- #
# pending-call resolution
# --------------------------------------------------------------------------- #


CALL_ID = "call-shared-fs"


def _flow_with_call(
    project_root: str, *, live: bool = False, call_id: str = CALL_ID
) -> Dict[str, Any]:
    """The shared flow, blocked on a pending human call.

    ``live`` mirrors :func:`_running_flow`: only the node whose supervisor holds
    the ``se3 run`` process reports the flow as non-resumable.
    """
    payload = _running_flow(project_root, live=live)
    payload["pending_calls"] = [
        {
            "call_id": call_id,
            "kind": "human",
            "prompt": f"truncated prompt from {project_root}",
        }
    ]
    return payload


def test_pending_call_resolves_to_the_reachable_mirror():
    """``find_call_owner`` must route the detail pull at a live daemon.

    Both nodes mirror the same ``se3/calls/<id>`` file off the shared disk, so
    the reachable one serves byte-identical content; picking the unreachable
    first-registered machine made ``GET /api/calls/{id}/detail`` answer 503
    permanently.
    """

    async def scenario():
        state = ServerState()
        await _seed_machine(
            state,
            "node007",
            owner=OWNER,
            project_root=ROOT_A,
            flow=_flow_with_call(ROOT_A),
            offline=True,
        )
        await _seed_machine(
            state,
            "node008",
            owner=OWNER,
            project_root=ROOT_B,
            flow=_flow_with_call(ROOT_B),
        )
        state.set_connectivity_probe(_probe("node008"))
        return (
            await state.find_call_owner(CALL_ID, owner=OWNER),
            await state.get_pending_call(CALL_ID, owner=OWNER),
        )

    resolved, mirror = asyncio.run(scenario())
    assert resolved == ("node008", ROOT_B)
    # The mirror fall-back must read off the same machine the pull targets.
    assert mirror["prompt"].endswith(ROOT_B)


def test_pending_call_on_a_running_flow_resolves_to_the_reachable_node():
    """A RUNNING flow blocked on a human call still routes at the live daemon.

    This is the state a flow sits in while it waits for an operator, and the
    unreachable node007's frozen frame reads exactly like a live process. The
    prompt is read off the shared disk, so the answer must come from the machine
    that can be reached — as it does for every other resolution entry point.
    """

    async def scenario():
        state = ServerState()
        await _seed_machine(
            state,
            "node007",
            owner=OWNER,
            project_root=ROOT_A,
            flow=_flow_with_call(ROOT_A, live=True),
        )
        await _seed_machine(
            state,
            "node008",
            owner=OWNER,
            project_root=ROOT_B,
            flow=_flow_with_call(ROOT_B, live=False),
        )
        state.set_connectivity_probe(_probe("node008"))
        flow = await state.get_flow(FLOW_ID, owner=OWNER)
        return flow[0], await state.find_call_owner(CALL_ID, owner=OWNER)

    machine_id, resolved = asyncio.run(scenario())
    assert machine_id == "node008"
    assert resolved == ("node008", ROOT_B)


def test_pending_call_falls_back_when_no_machine_is_reachable():
    """With the whole fleet gone, resolution is the pre-fix answer."""

    async def scenario():
        state = ServerState()
        await _seed_machine(
            state,
            "node007",
            owner=OWNER,
            project_root=ROOT_A,
            flow=_flow_with_call(ROOT_A),
            offline=True,
        )
        await _seed_machine(
            state,
            "node008",
            owner=OWNER,
            project_root=ROOT_B,
            flow=_flow_with_call(ROOT_B),
            offline=True,
        )
        state.set_connectivity_probe(_probe())
        return await state.find_call_owner(CALL_ID, owner=OWNER)

    assert asyncio.run(scenario()) == ("node007", ROOT_A)


def test_pending_call_project_root_pin_survives_the_reachable_ordering():
    """An explicit ``project_root`` still wins over reachability.

    A ``call_id`` is only unique within one project, so the caller's pin is a
    correctness constraint (wrong project ⇒ wrong prompt), not a preference —
    reordering the scan must not let a reachable machine's same-id call in
    another project answer instead.
    """

    async def scenario():
        state = ServerState()
        await _seed_machine(
            state,
            "node007",
            owner=OWNER,
            project_root=ROOT_A,
            flow=_flow_with_call(ROOT_A),
            offline=True,
        )
        await _seed_machine(
            state,
            "node008",
            owner=OWNER,
            project_root=ROOT_B,
            flow=_flow_with_call(ROOT_B),
        )
        state.set_connectivity_probe(_probe("node008"))
        return await state.find_call_owner(
            CALL_ID, owner=OWNER, project_root=ROOT_A
        )

    assert asyncio.run(scenario()) == ("node007", ROOT_A)


def test_pending_call_resolution_never_crosses_owner_boundary():
    """A reachable machine of another owner must stay invisible."""

    async def scenario():
        state = ServerState()
        await _seed_machine(
            state,
            "intruder",
            owner=OTHER_OWNER,
            project_root=ROOT_C,
            flow=_flow_with_call(ROOT_C),
        )
        await _seed_machine(
            state,
            "node007",
            owner=OWNER,
            project_root=ROOT_A,
            flow=_flow_with_call(ROOT_A),
            offline=True,
        )
        state.set_connectivity_probe(_probe("intruder"))
        return await state.find_call_owner(CALL_ID, owner=OWNER)

    assert asyncio.run(scenario()) == ("node007", ROOT_A)


# --------------------------------------------------------------------------- #
# issue mirror: same shared-filesystem resolution as flows
# --------------------------------------------------------------------------- #

ISSUE_ID = "I-42"
SHARED_ROOT = "/shared/proj"
OTHER_ROOT = "/shared/other-proj"


def _issue(
    issue_id: str = ISSUE_ID,
    *,
    project_root: str = SHARED_ROOT,
    updated_at: str = "2026-07-24T10:00:00",
    status: str = "open",
    **extra: Any,
) -> Dict[str, Any]:
    """An ``IssueSnapshot`` dict as a daemon mirrors it in STATUS_UPDATE."""
    issue: Dict[str, Any] = {
        "id": issue_id,
        "project_root": project_root,
        "title": f"{issue_id} on {project_root}",
        "status": status,
        "source": "human",
        "updated_at": updated_at,
    }
    issue.update(extra)
    return issue


async def _seed_issue_machine(
    state: ServerState,
    machine_id: str,
    *,
    owner: Optional[str] = OWNER,
    issues: Optional[List[Dict[str, Any]]] = None,
    project_roots: Optional[List[str]] = None,
    offline: bool = False,
) -> None:
    """Register *machine_id* and let it mirror *issues* / register *project_roots*."""
    await state.register_machine(machine_id, owner_id=owner)
    snapshot: Dict[str, Any] = {"flows": [], "issues": issues or []}
    if project_roots is not None:
        snapshot["project_roots"] = project_roots
    await state.update_status(machine_id, snapshot)
    if offline:
        await state.mark_offline(machine_id)


def test_issue_resolves_to_the_reachable_mirror():
    """The dead node's issue mirror must not shadow the connected node's.

    Every issue write (edit / close / reopen ISSUE_COMMAND, and the SPAWN_FLOW
    of "start flow from issue") is dispatched to the machine resolved here, so
    picking node007 is a permanent 404 — unlike the presence flag nothing ages
    a disconnected machine's issue mirror out.
    """

    async def scenario():
        state = ServerState()
        await _seed_issue_machine(
            state, "node007", issues=[_issue()], offline=True
        )
        await _seed_issue_machine(state, "node008", issues=[_issue()])
        return (
            await state.get_issue_by_id(ISSUE_ID, owner=OWNER),
            await state.find_machine_for_project(SHARED_ROOT, owner=OWNER),
        )

    found, machine = asyncio.run(scenario())
    assert found is not None
    assert found[0] == "node008"
    assert found[1] == SHARED_ROOT
    assert machine == "node008"


def test_issue_resolution_switches_inside_the_presence_grace():
    """Reachability, not the 60 s-debounced ``online`` flag, decides."""

    async def scenario():
        state = ServerState()
        # Neither is marked offline: node007 is inside its grace window.
        await _seed_issue_machine(state, "node007", issues=[_issue()])
        await _seed_issue_machine(state, "node008", issues=[_issue()])
        state.set_connectivity_probe(_probe("node008"))
        assert state._machines["node007"].online is True
        return (
            await state.get_issue_by_id(ISSUE_ID, owner=OWNER),
            await state.get_issues(owner=OWNER),
        )

    found, issues = asyncio.run(scenario())
    assert found[0] == "node008"
    assert [i["machine_id"] for i in issues] == ["node008"]


def test_issue_list_collapses_the_shared_issue_onto_one_row():
    """One row per on-disk YAML file, naming the machine commands route to."""

    async def scenario():
        state = ServerState()
        await _seed_issue_machine(
            state,
            "node007",
            # The dead node even holds the NEWER mirror: reachability still wins,
            # because only node008 can apply a write to the shared file.
            issues=[_issue(updated_at="2026-07-24T18:00:00")],
            offline=True,
        )
        await _seed_issue_machine(
            state, "node008", issues=[_issue(updated_at="2026-07-24T10:00:00")]
        )
        return await state.get_issues(owner=OWNER)

    issues = asyncio.run(scenario())
    assert len(issues) == 1
    assert issues[0]["machine_id"] == "node008"
    assert issues[0]["updated_at"] == "2026-07-24T10:00:00"


def test_issue_list_dedupe_falls_back_to_newest_updated_at():
    """With both mirrors equally unreachable, the fresher snapshot wins."""

    async def scenario():
        state = ServerState()
        await _seed_issue_machine(
            state,
            "node007",
            issues=[_issue(updated_at="2026-07-24T10:00:00")],
            offline=True,
        )
        await _seed_issue_machine(
            state,
            "node008",
            issues=[_issue(updated_at="2026-07-24T18:00:00")],
            offline=True,
        )
        return await state.get_issues(owner=OWNER)

    issues = asyncio.run(scenario())
    assert len(issues) == 1
    assert issues[0]["machine_id"] == "node008"
    assert issues[0]["updated_at"] == "2026-07-24T18:00:00"


def test_issue_dedupe_is_scoped_to_the_project_root():
    """Ids are unique only WITHIN a project — two projects' ``I-42`` both stay.

    Collapsing on the bare id would silently hide one project's issue behind an
    unrelated one that happens to reuse the number.
    """

    async def scenario():
        state = ServerState()
        await _seed_issue_machine(
            state,
            "node007",
            issues=[_issue(), _issue(project_root=OTHER_ROOT)],
            offline=True,
        )
        await _seed_issue_machine(state, "node008", issues=[_issue()])
        return await state.get_issues(owner=OWNER)

    issues = asyncio.run(scenario())
    assert len(issues) == 2
    by_root = {str(i["project_root"]): i for i in issues}
    assert by_root[SHARED_ROOT]["machine_id"] == "node008"
    # Only node007 ever saw the other project — it is still listed.
    assert by_root[OTHER_ROOT]["machine_id"] == "node007"


def test_issue_list_keeps_rows_without_an_id():
    """A malformed mirror row has no identity to collapse on; it passes through."""

    async def scenario():
        state = ServerState()
        await _seed_issue_machine(
            state,
            "node007",
            issues=[
                {"project_root": SHARED_ROOT, "status": "open", "source": "human"},
                _issue(),
            ],
            offline=True,
        )
        await _seed_issue_machine(state, "node008", issues=[_issue()])
        return await state.get_issues(owner=OWNER)

    issues = asyncio.run(scenario())
    assert len(issues) == 2
    # Reachable node008 is scanned first, so its collapsed row leads; the
    # id-less one trails, kept rather than merged into (and hidden behind) it.
    assert [str(i.get("id") or "") for i in issues] == [ISSUE_ID, ""]
    assert issues[0]["machine_id"] == "node008"
    assert issues[1]["machine_id"] == "node007"


def test_issue_resolution_all_offline_falls_back_to_pre_fix_answer():
    """No reachable candidate: the answer is exactly the previous one."""

    async def scenario():
        state = ServerState()
        await _seed_issue_machine(
            state, "node007", issues=[_issue()], offline=True
        )
        await _seed_issue_machine(
            state, "node008", issues=[_issue()], offline=True
        )
        return (
            await state.get_issue_by_id(ISSUE_ID, owner=OWNER),
            await state.find_machine_for_project(SHARED_ROOT, owner=OWNER),
        )

    found, machine = asyncio.run(scenario())
    assert found[0] == "node007"
    assert machine == "node007"


def test_issue_resolution_never_crosses_owner_boundary():
    """A reachable machine of ANOTHER owner is not a candidate."""

    async def scenario():
        state = ServerState()
        await _seed_issue_machine(
            state, "node007", issues=[_issue()], offline=True
        )
        await _seed_issue_machine(
            state, "intruder", owner=OTHER_OWNER, issues=[_issue()]
        )
        return (
            await state.get_issue_by_id(ISSUE_ID, owner=OWNER),
            await state.find_machine_for_project(SHARED_ROOT, owner=OWNER),
            await state.get_issues(owner=OWNER),
        )

    found, machine, issues = asyncio.run(scenario())
    assert found[0] == "node007"
    assert machine == "node007"
    assert [i["machine_id"] for i in issues] == ["node007"]


def test_issue_machine_pin_survives_the_reachable_ordering():
    """An explicit machine_id / project_root still pins resolution.

    ``POST /api/flows`` reads an inconsistent target as not-found, so the
    reachability preference must never widen an explicitly scoped lookup.
    """

    async def scenario():
        state = ServerState()
        await _seed_issue_machine(
            state, "node007", issues=[_issue()], offline=True
        )
        await _seed_issue_machine(state, "node008", issues=[_issue()])
        return (
            await state.get_issue_by_id(
                ISSUE_ID, owner=OWNER, machine_id="node007"
            ),
            await state.get_issue_by_id(
                ISSUE_ID, owner=OWNER, project_root=OTHER_ROOT
            ),
            await state.get_issues(owner=OWNER, machine_id="node007"),
        )

    pinned, mismatched, listed = asyncio.run(scenario())
    assert pinned is not None and pinned[0] == "node007"
    assert mismatched is None
    assert [i["machine_id"] for i in listed] == ["node007"]


def test_find_machine_for_project_prefers_a_reachable_registered_root():
    """Reachability outranks which source knows the root.

    node008 has only registered the root (its first STATUS_UPDATE carried no
    issues yet) while the dead node007 still holds a full mirror. Both address
    the identical directory on the shared disk, so the connected machine — the
    only one that can serve a dispatched frame — must win.
    """

    async def scenario():
        state = ServerState()
        await _seed_issue_machine(
            state,
            "node007",
            issues=[_issue()],
            project_roots=[SHARED_ROOT],
            offline=True,
        )
        await _seed_issue_machine(
            state, "node008", issues=[], project_roots=[SHARED_ROOT]
        )
        return await state.find_machine_for_project(SHARED_ROOT, owner=OWNER)

    assert asyncio.run(scenario()) == "node008"


def test_issue_single_machine_deployment_is_untouched():
    """Baseline: dedupe and reordering are no-ops for one reporting machine."""

    async def scenario():
        state = ServerState()
        await _seed_issue_machine(
            state,
            "node007",
            issues=[
                _issue("I-1"),
                _issue("I-2", project_root=OTHER_ROOT),
                _issue("I-3", status="closed"),
            ],
            project_roots=[SHARED_ROOT, OTHER_ROOT],
        )
        return (
            await state.get_issues(owner=OWNER),
            await state.get_issues(owner=OWNER, include_closed=True),
            await state.find_machine_for_project(OTHER_ROOT, owner=OWNER),
        )

    open_only, everything, machine = asyncio.run(scenario())
    assert [i["id"] for i in open_only] == ["I-1", "I-2"]
    # Unchanged mirror order: grouped by project_root, then as reported.
    assert [i["id"] for i in everything] == ["I-1", "I-3", "I-2"]
    assert machine == "node007"


def test_history_index_single_machine_deployment_is_untouched():
    """Baseline: dedupe is a no-op when only one machine ever reports."""

    async def scenario():
        state = ServerState()
        await state.register_machine("node007", owner_id=OWNER)
        await state.update_history_index(
            "node007",
            [
                _row("flow-a", "2026-07-24T10:00:00"),
                _row("flow-b", "2026-07-24T12:00:00"),
                _row("flow-c", "2026-07-24T11:00:00"),
            ],
        )
        return await state.get_history_index(owner=OWNER)

    entries = asyncio.run(scenario())
    assert [e["flow_id"] for e in entries] == ["flow-b", "flow-c", "flow-a"]
    assert {e["machine_id"] for e in entries} == {"node007"}


# --------------------------------------------------------------------------- #
# the active-worktree predicate reads off the resolved machine
# --------------------------------------------------------------------------- #

WORKTREE_ROOT_A = "/shared/proj/se3/worktrees/node007-run"
WORKTREE_ROOT_B = "/shared/proj/se3/worktrees/node008-run"


def _worktree_flow(project_root: str, status: str) -> Dict[str, Any]:
    """A ``--worktree`` flow snapshot in *status* on *project_root*."""
    payload = _flow_payload(project_root)
    payload["status"] = status
    payload["resumable"] = status != "running"
    return payload


async def _seed_worktree_switch(
    state: ServerState,
    *,
    a_offline: bool,
    b_owner: Optional[str] = OWNER,
) -> None:
    """node007 holds a frozen ``completed`` snapshot; node008 resumed it."""
    await _seed_machine(
        state,
        "node007",
        owner=OWNER,
        project_root=WORKTREE_ROOT_A,
        flow=_worktree_flow(WORKTREE_ROOT_A, "completed"),
        offline=a_offline,
    )
    await _seed_machine(
        state,
        "node008",
        owner=b_owner,
        project_root=WORKTREE_ROOT_B,
        flow=_worktree_flow(WORKTREE_ROOT_B, "running"),
    )


def test_active_worktree_predicate_follows_the_reachable_machine():
    """The resumed run is live on node008; node007's frozen frame must not win.

    ``is_active_worktree_flow`` gates the empty-full rejection, the incremental
    recovery-pull choice and the history endpoint's self-heal. Judging it off the
    machine the flow moved AWAY from answers ``False`` for a live run and
    re-freezes worktree discovery at round 1 — the defect the self-heal exists
    to fix, surfacing only once the machine switch became routable.
    """

    async def scenario():
        state = ServerState()
        await _seed_worktree_switch(state, a_offline=True)
        return (
            await state.is_active_worktree_flow(FLOW_ID, owner=OWNER),
            await state.get_flow(FLOW_ID, owner=OWNER),
        )

    active, flow = asyncio.run(scenario())
    assert active is True
    # Same machine the rest of the resolution layer answers with.
    assert flow[0] == "node008"


def test_active_worktree_predicate_switches_inside_the_presence_grace():
    """Reachability, not the debounced ``online`` flag, decides here too."""

    async def scenario():
        state = ServerState()
        await _seed_worktree_switch(state, a_offline=False)
        state.set_connectivity_probe(_probe("node008"))
        return await state.is_active_worktree_flow(FLOW_ID, owner=OWNER)

    assert asyncio.run(scenario()) is True


def test_active_worktree_predicate_all_offline_falls_back_to_pre_fix_answer():
    """With nothing reachable the verdict is exactly the insertion-order one."""

    async def scenario():
        state = ServerState()
        await _seed_worktree_switch(state, a_offline=True)
        await state.mark_offline("node008")
        return await state.is_active_worktree_flow(FLOW_ID, owner=OWNER)

    # node007 first in insertion order, ``completed`` → not an active worktree.
    assert asyncio.run(scenario()) is False


def test_active_worktree_predicate_never_crosses_owner_boundary():
    """A reachable machine of another owner cannot flip the verdict."""

    async def scenario():
        state = ServerState()
        await _seed_worktree_switch(state, a_offline=True, b_owner=OTHER_OWNER)
        return (
            await state.is_active_worktree_flow(FLOW_ID, owner=OWNER),
            await state.is_active_worktree_flow(FLOW_ID, owner=OTHER_OWNER),
        )

    own, other = asyncio.run(scenario())
    assert own is False
    assert other is True


def test_active_worktree_predicate_single_machine_is_unchanged():
    """Baseline: one machine, one snapshot — the reordering is a no-op."""

    async def scenario():
        results = []
        for status, root in (
            ("running", WORKTREE_ROOT_B),
            ("paused", WORKTREE_ROOT_B),
            ("completed", WORKTREE_ROOT_B),
            ("running", ROOT_B),
        ):
            state = ServerState()
            await _seed_machine(
                state,
                "node008",
                owner=OWNER,
                project_root=root,
                flow=_worktree_flow(root, status),
            )
            results.append(await state.is_active_worktree_flow(FLOW_ID, owner=OWNER))
        return results

    assert asyncio.run(scenario()) == [True, True, False, False]
