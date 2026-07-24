"""Online-first flow→machine resolution in :class:`ServerState`.

Reproduces the shared-filesystem multi-machine deployment (an HPC cluster where
a job ends on node007 and the next one starts on node008 against the same disk):
both daemons report the SAME ``flow_id``, and the server keeps the disconnected
machine's flows / history index after ``mark_offline``. Resolving in plain
insertion order let the dead machine shadow the live one forever, which the
WebUI saw as a 404 on ``GET /api/history/{flow_id}`` and a
``machine ... is not connected`` 404 on resume.

The invariants locked here:

* every resolution entry point prefers an ONLINE machine reporting the flow;
* an online machine wins even when it only knows the flow from its live set
  while the offline one holds the (more authoritative) history-index entry;
* with no online candidate at all, resolution falls back to exactly the
  pre-fix answer;
* owner scoping is never bypassed by the online preference.

Follows the sibling server tests' convention of driving coroutines with
``asyncio.run`` from plain sync test functions rather than the pytest-asyncio
marker.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional

from se3.server.state import ServerState


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
) -> None:
    """Register *machine_id*, let it report the shared flow, maybe disconnect."""
    await state.register_machine(machine_id, owner_id=owner)
    await state.update_status(
        machine_id,
        {"flows": [_flow_payload(project_root)] if with_flow else []},
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
