"""The server history relay's memory budget, LRU eviction and re-source path.

Background (the defect these lock down): ``ServerState._history_data`` mirrors
each active flow's WHOLE conversation in process memory, the daemon pushes every
ACTIVE flow's history whether or not a browser is watching it, and a single
``MSG_HISTORY_DATA`` frame may carry megabytes. With no ceiling the mirror grew
until the kernel oom-killed the server in its memory-capped container.

The fix is a byte budget plus eviction of the least-recently *viewed* bundle —
"viewed" meaning read by a UI client, never written by a daemon push, because
keying recency off the push refreshes every active flow as hot and the LRU
degenerates into never evicting anything. An evicted flow is remembered as cold
so the push cannot immediately re-establish it (which would be an
eviction⇄re-pull storm rather than a bound); only a UI read re-admits it, and the
rebuild then goes through the ordinary cursorless FULL pull.

These tests drive ``ServerState`` directly (no daemon, no browser) at that seam,
plus one end-to-end pass over the config → ``create_app`` → ``ServerState`` wiring
and the off-event-loop response rendering.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import threading

import pytest

from tianluo.daemon import protocol
from tianluo.server.state import ServerState

MACHINE = "m-budget"


def _record(step: str, ordinal: int, payload_chars: int = 2000) -> dict:
    """A daemon history record big enough that a handful blow a small budget."""
    return {
        "step_id": step,
        "step_type": "discovery",
        "ordinal": ordinal,
        "message": {"role": "assistant", "content": "x" * payload_chars},
    }


def _records(step: str, count: int, start: int = 0, payload_chars: int = 2000):
    return [_record(step, i, payload_chars) for i in range(start, start + count)]


def _step_file(step: str) -> str:
    return f"{step}.jsonl"


async def _full(state, flow, records, *, lines=None, machine=MACHINE):
    return await state.apply_history_frame(
        flow,
        protocol.HISTORY_MODE_FULL,
        records,
        cursor={_step_file(flow): len(records) if lines is None else lines},
        machine_id=machine,
    )


async def _append(state, flow, records, *, base, lines, machine=MACHINE):
    return await state.apply_history_frame(
        flow,
        protocol.HISTORY_MODE_APPEND,
        records,
        cursor={_step_file(flow): lines},
        cursor_base={_step_file(flow): base},
        machine_id=machine,
    )


async def _register_flows(state, flows, *, project_root="/repo", status="running"):
    await state.update_status(
        MACHINE,
        {
            "machine_id": MACHINE,
            "flows": [
                {
                    "flow_id": flow,
                    "project_root": project_root,
                    "status": status,
                }
                for flow in flows
            ],
        },
    )


def _age_view(state: ServerState, flow: str, seconds: float = 10_000.0) -> None:
    """Backdate *flow*'s UI-read stamp so it is no longer 'being watched'.

    White-box on purpose: the alternative is sleeping past
    ``_HISTORY_VIEW_HOT_WINDOW`` in every test.
    """
    if flow in state._history_read_at:
        state._history_read_at[flow] -= seconds


async def _cached(state: ServerState, flow: str):
    """Read the bundle WITHOUT counting it as UI interest."""
    return await state.get_history(flow, touch=False)


# ---------------------------------------------------------------------------
# accounting
# ---------------------------------------------------------------------------


def test_bundle_bytes_are_accounted_and_track_appends():
    async def scenario():
        state = ServerState(history_cache_budget_bytes=1024 * 1024 * 1024)
        await _full(state, "f1", _records("f1", 10))
        first = await state.history_cache_stats()
        assert first["flows"] == 1
        assert first["bytes"] > 10 * 2000

        await _append(state, "f1", _records("f1", 10, start=10), base=10, lines=20)
        second = await state.history_cache_stats()
        # The accounting is maintained incrementally by the append reconcile,
        # so it must have grown by roughly the frame — not stayed frozen and not
        # been recomputed to something unrelated.
        assert second["bytes"] > first["bytes"] * 1.8

        # A re-delivered frame is folded away by the reconcile, so it must not
        # be charged twice either.
        await _append(state, "f1", _records("f1", 10, start=10), base=10, lines=20)
        third = await state.history_cache_stats()
        assert third["bytes"] == second["bytes"]

    asyncio.run(scenario())


def test_stats_rank_flows_by_occupancy_for_attribution():
    async def scenario():
        state = ServerState(history_cache_budget_bytes=1024 * 1024 * 1024)
        await _full(state, "small", _records("small", 2))
        await _full(state, "huge", _records("huge", 40))
        stats = await state.history_cache_stats()
        assert stats["top"][0]["flow_id"] == "huge"
        assert stats["top"][0]["records"] == 40
        assert stats["top"][0]["machine_id"] == MACHINE
        assert stats["flows"] == 2
        assert stats["used_percent"] is not None

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# eviction + budget boundaries
# ---------------------------------------------------------------------------


def test_least_recently_viewed_flow_is_evicted_first():
    async def scenario():
        # Budget sized to hold roughly two of these bundles.
        probe = ServerState(history_cache_budget_bytes=1024 * 1024 * 1024)
        await _full(probe, "p", _records("p", 20))
        one = (await probe.history_cache_stats())["bytes"]

        state = ServerState(history_cache_budget_bytes=int(one * 2.5))
        await _register_flows(state, ["a", "b", "c"])
        for flow in ("a", "b", "c"):
            await _full(state, flow, _records(flow, 20))
            # Every flow HAS been opened at some point, so what separates them
            # below is purely how long ago — not "read" versus "never read".
            await state.get_history_snapshot(flow)
        _age_view(state, "a", seconds=5.0)
        _age_view(state, "b", seconds=10_000.0)
        _age_view(state, "c", seconds=1_000.0)

        await state.report_history_cache()

        # "b" is the least recently VIEWED, so it goes first — and only it: the
        # sweep stops the moment the budget is met again.
        assert await _cached(state, "b") is None
        assert await _cached(state, "a") is not None
        assert await _cached(state, "c") is not None
        assert (await state.history_cache_stats())["evictions"] == 1

    asyncio.run(scenario())


def test_exactly_at_budget_does_not_evict_and_one_byte_over_does():
    async def scenario():
        probe = ServerState(history_cache_budget_bytes=1024 * 1024 * 1024)
        await _full(probe, "p", _records("p", 12))
        one = (await probe.history_cache_stats())["bytes"]

        at = ServerState(history_cache_budget_bytes=one)
        await _register_flows(at, ["p"])
        await _full(at, "p", _records("p", 12))
        assert (await at.history_cache_stats())["bytes"] == one
        # An unprotected sweep at exactly the budget must leave the cache alone.
        await at.report_history_cache()
        assert await _cached(at, "p") is not None

        over = ServerState(history_cache_budget_bytes=one - 1)
        await _register_flows(over, ["p"])
        await _full(over, "p", _records("p", 12))
        # The write itself is protected (the caller must be able to read it
        # back), so the overshoot is reclaimed by the next unprotected sweep.
        assert await _cached(over, "p") is not None
        await over.report_history_cache()
        assert await _cached(over, "p") is None

    asyncio.run(scenario())


def test_single_oversized_bundle_is_evicted_when_nobody_is_watching():
    async def scenario():
        state = ServerState(history_cache_budget_bytes=1)
        await _register_flows(state, ["big"])
        await _full(state, "big", _records("big", 30))
        await state.report_history_cache()
        assert await _cached(state, "big") is None
        stats = await state.history_cache_stats()
        assert stats["evictions"] == 1
        assert stats["evicted_bytes"] > 0

    asyncio.run(scenario())


def test_zero_budget_keeps_only_what_a_ui_client_is_reading():
    async def scenario():
        state = ServerState(history_cache_budget_bytes=0)
        await _register_flows(state, ["watched", "unwatched"])

        # A UI client is polling "watched": its cache-miss read marks it hot, so
        # the pull reply is admitted AND survives, or the endpoint could never
        # answer at all under a zero budget.
        assert await state.get_history_snapshot("watched") is None
        await _full(state, "watched", _records("watched", 5))
        assert await state.get_history_snapshot("watched") is not None

        # Nobody is reading "unwatched": one write, then gone.
        await _full(state, "unwatched", _records("unwatched", 5))
        await state.report_history_cache()
        assert await _cached(state, "unwatched") is None
        assert await _cached(state, "watched") is not None

    asyncio.run(scenario())


def test_watched_flow_is_never_evicted_even_over_budget():
    async def scenario():
        state = ServerState(history_cache_budget_bytes=1)
        await _register_flows(state, ["watched", "other"])
        await state.get_history_snapshot("watched")
        await _full(state, "watched", _records("watched", 20))
        await _full(state, "other", _records("other", 20))
        await state.report_history_cache()
        # Only the unwatched one may go; the ``/ws/ui`` fan-out and the REST
        # poll for the watched flow keep finding their bundle.
        assert await _cached(state, "watched") is not None
        assert await state.get_history_bundle_meta("watched") is not None
        assert await _cached(state, "other") is None

    asyncio.run(scenario())


def test_active_worktree_flow_is_evicted_last():
    async def scenario():
        probe = ServerState(history_cache_budget_bytes=1024 * 1024 * 1024)
        await _full(probe, "p", _records("p", 20))
        one = (await probe.history_cache_stats())["bytes"]

        # Room for one bundle, not two.
        state = ServerState(history_cache_budget_bytes=int(one * 1.5))
        await state.update_status(
            MACHINE,
            {
                "machine_id": MACHINE,
                "flows": [
                    {
                        "flow_id": "wt",
                        "project_root": "/repo/tianluo/worktrees/wt-1",
                        "status": "running",
                    },
                    {
                        "flow_id": "plain",
                        "project_root": "/repo",
                        "status": "running",
                    },
                ],
            },
        )
        await _full(state, "wt", _records("wt", 20))
        await _full(state, "plain", _records("plain", 20))
        # Neither was ever viewed, so recency cannot separate them: the flow the
        # anti-shrink guard protects must be the survivor of the two.
        await state.report_history_cache()
        assert await _cached(state, "plain") is None
        assert await _cached(state, "wt") is not None

    asyncio.run(scenario())


# ---------------------------------------------------------------------------
# re-source: an evicted flow rebuilds through the cache-miss FULL pull
# ---------------------------------------------------------------------------


def test_evicted_flow_reads_as_a_cache_miss_and_rebuilds_from_a_full_frame():
    async def scenario():
        state = ServerState(history_cache_budget_bytes=1)
        await _register_flows(state, ["f"])
        await _full(state, "f", _records("f", 20))
        await state.report_history_cache()

        # The REST read is a genuine miss, which is what makes the endpoint fire
        # its cursorless回源 pull.
        assert await state.get_history_snapshot("f") is None
        # That same read re-admitted the flow, so the pull's full reply lands.
        outcome = await _full(state, "f", _records("f", 20))
        assert outcome.resolves_pull is True
        snapshot = await state.get_history_snapshot("f")
        assert snapshot["delivery"] == "full"
        assert len(snapshot["records"]) == 20

    asyncio.run(scenario())


def test_rebuild_after_eviction_asks_for_a_full_not_a_daemon_side_increment():
    async def scenario():
        state = ServerState(history_cache_budget_bytes=1)
        await _register_flows(state, ["f"])
        await _full(state, "f", _records("f", 20))
        await state.report_history_cache()

        # A user opens the flow again (re-admits it), and the daemon's next live
        # push is an append computed off ITS retained cursor.
        assert await state.get_history_snapshot("f") is None
        applied = await _append(
            state, "f", _records("f", 3, start=20), base=20, lines=23
        )
        # The append is refused — adopting it would pin a head-truncated bundle.
        assert applied.resolves_pull is False
        plan = await state.plan_recovery_pull("f", MACHINE)
        # ... and the self-heal asks for a FULL rebuild, never an incremental
        # anchored on a water mark the server no longer has.
        assert plan == ("full", None)

    asyncio.run(scenario())


def test_evicted_flow_is_not_re_established_by_daemon_pushes(caplog):
    async def scenario():
        state = ServerState(history_cache_budget_bytes=1)
        await _register_flows(state, ["f"])
        await _full(state, "f", _records("f", 20))
        await state.report_history_cache()
        assert (await state.history_cache_stats())["evictions"] == 1

        # 25 push ticks land while nobody is watching. None of them may
        # re-establish the bundle, arm a recovery pull, or trip another eviction:
        # that loop is the eviction⇄full-repull storm the cold marker exists to
        # prevent.
        for i in range(25):
            await _append(
                state, "f", _records("f", 2, start=20 + 2 * i),
                base=20 + 2 * i, lines=22 + 2 * i,
            )
            await _full(state, "f", _records("f", 20))
            assert await state.take_recovery_pull("f") is False
            assert await state.plan_recovery_pull("f", MACHINE) is None

        assert await _cached(state, "f") is None
        assert state._history_requires_full == set()
        assert (await state.history_cache_stats())["evictions"] == 1

    with caplog.at_level(logging.DEBUG, logger="tianluo.server.state"):
        asyncio.run(scenario())


def test_ui_read_re_admits_a_suppressed_flow():
    async def scenario():
        state = ServerState(history_cache_budget_bytes=1)
        await _register_flows(state, ["f"])
        await _full(state, "f", _records("f", 20))
        await state.report_history_cache()
        # Suppressed while unwatched...
        await _full(state, "f", _records("f", 20))
        assert await _cached(state, "f") is None
        # ...and admitted again the moment a UI client asks for it.
        await state.get_history_snapshot("f")
        await _full(state, "f", _records("f", 20))
        assert await _cached(state, "f") is not None

    asyncio.run(scenario())


def test_a_suppressed_frame_still_reports_its_cursor_for_the_console():
    """An evicted flow a console is DISPLAYING must not go silent.

    Suppressing the records is right — relaying them would re-establish exactly
    the bundle the budget refused. Suppressing the fact that the flow moved is
    not: the WebUI History view has no poll timer, it self-checks only when a
    frame arrives, so a console reading an evicted flow would freeze on what it
    already holds until the user re-clicked the session. The cursor is what lets
    it notice it is short of records and re-pull — and that read re-admits the
    flow.
    """
    async def scenario():
        state = ServerState(history_cache_budget_bytes=1)
        await _register_flows(state, ["f"])
        await _full(state, "f", _records("f", 20), lines=20)
        await state.report_history_cache()
        outcome = await _append(
            state, "f", _records("f", 2, start=20), base=20, lines=22
        )
        return outcome

    outcome = asyncio.run(scenario())
    # Nothing was cached and nothing was armed …
    assert outcome.resolves_pull is False
    assert outcome.rejected_full is False
    # … but the frame's own water mark is carried out for the fan-out.
    assert outcome.cold_suppressed is True
    assert outcome.suppressed_cursor == {_step_file("f"): 22}


def test_the_cold_advisory_is_debounced_per_flow():
    """A backlog drain must not turn one advisory per chunk into a storm."""
    async def scenario():
        state = ServerState(history_cache_budget_bytes=1)
        await _register_flows(state, ["f"])
        await _full(state, "f", _records("f", 20), lines=20)
        await state.report_history_cache()
        announced = []
        for i in range(10):
            outcome = await _append(
                state, "f", _records("f", 2, start=20 + 2 * i),
                base=20 + 2 * i, lines=22 + 2 * i,
            )
            announced.append(outcome.cold_suppressed)
        return announced

    announced = asyncio.run(scenario())
    # The FIRST frame after the eviction is the one a console needs; the rest
    # inside the debounce window say nothing new.
    assert announced[0] is True
    assert announced[1:] == [False] * 9


def test_a_re_eviction_re_arms_the_advisory_immediately():
    """An advisory sent before an eviction says nothing about the one after."""
    async def scenario():
        state = ServerState(history_cache_budget_bytes=1)
        await _register_flows(state, ["f"])
        await _full(state, "f", _records("f", 20), lines=20)
        await state.report_history_cache()
        first = await _append(
            state, "f", _records("f", 2, start=20), base=20, lines=22
        )
        # A UI read re-admits it, the rebuild lands, and the budget drops it
        # again — all well inside the advisory debounce window.
        await state.get_history_snapshot("f")
        await _full(state, "f", _records("f", 20), lines=20)
        # The hot window lapses, so the next unprotected sweep drops it again.
        state._history_read_at.clear()
        await state.report_history_cache()
        second = await _append(
            state, "f", _records("f", 2, start=20), base=20, lines=22
        )
        return first.cold_suppressed, second.cold_suppressed

    first, second = asyncio.run(scenario())
    assert first is True and second is True


def test_sweep_enforces_the_budget_without_logging_a_report(caplog):
    """Memory enforcement must not ride on the diagnostic log cadence."""
    async def scenario():
        state = ServerState(history_cache_budget_bytes=1)
        await _register_flows(state, ["a", "b"])
        await _full(state, "a", _records("a", 20))
        await _full(state, "b", _records("b", 20))
        evicted = await state.sweep_history_cache()
        return evicted, await state.history_cache_stats()

    with caplog.at_level(logging.INFO, logger="tianluo.server.state"):
        evicted, stats = asyncio.run(scenario())
    # ``b``'s own write already made room by dropping ``a`` (the write-path
    # sweep exempts only the flow it is writing), so the unprotected sweep is
    # what drops ``b`` itself — the case no write-path sweep can ever reach.
    assert evicted == 1 and stats["flows"] == 0
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "reason=periodic" not in text


def test_maintenance_task_runs_even_when_the_report_is_disabled():
    """``report_interval_seconds: 0`` turns off a LOG LINE, not the budget.

    Before this, the unprotected sweep lived only inside the report task and the
    task was created only when the interval was positive — so an operator who
    silenced the diagnostic line also silenced the only sweep able to evict the
    flow whose frame was last applied (the write-path sweep always exempts it).
    A single actively pushed, unwatched flow then grew without bound: the exact
    oom-kill the budget was added to prevent.
    """
    from tianluo.server.app import HISTORY_CACHE_SWEEP_INTERVAL, create_app

    app = create_app(
        db_path=":memory:",
        history_cache_budget_bytes=1,
        history_cache_report_interval=0,
    )
    swept = []

    async def scenario():
        state = app.state.server_state
        original = state.sweep_history_cache

        async def counting():
            swept.append(1)
            return await original()

        state.sweep_history_cache = counting
        # Drive the lifespan with the sweep cadence collapsed to "immediately",
        # so the test asserts the task exists and sweeps rather than sleeping
        # for a minute to find out.
        import tianluo.server.app as app_module

        real_sleep = asyncio.sleep

        async def fast_sleep(delay, *a, **kw):
            assert delay == HISTORY_CACHE_SWEEP_INTERVAL
            return await real_sleep(0)

        app_module.asyncio.sleep = fast_sleep
        try:
            async with app.router.lifespan_context(app):
                for _ in range(20):
                    await real_sleep(0)
                    if swept:
                        break
                assert app.state.history_cache_report_task is not None
        finally:
            app_module.asyncio.sleep = real_sleep

    asyncio.run(scenario())
    assert swept, "no unprotected budget sweep ran with the report disabled"


# ---------------------------------------------------------------------------
# invariants that must survive eviction
# ---------------------------------------------------------------------------


def test_cursor_gap_detection_still_arms_the_self_heal_under_a_budget():
    async def scenario():
        state = ServerState(history_cache_budget_bytes=1024 * 1024 * 1024)
        await _register_flows(state, ["f"])
        await _full(state, "f", _records("f", 5))
        # A frame that starts past the water mark is a hole: refused, armed.
        gapped = await _append(
            state, "f", _records("f", 2, start=8), base=8, lines=10
        )
        assert gapped.resolves_pull is False
        assert await state.take_recovery_pull("f") is True
        bundle = await _cached(state, "f")
        assert [r["ordinal"] for r in bundle["records"]] == list(range(5))

    asyncio.run(scenario())


def test_progress_token_and_generation_survive_appends_under_a_budget():
    async def scenario():
        state = ServerState(history_cache_budget_bytes=1024 * 1024 * 1024)
        await _register_flows(state, ["f"])
        await _full(state, "f", _records("f", 5))
        first = await state.get_history_snapshot("f")
        generation = first["generation"]

        await _append(state, "f", _records("f", 3, start=5), base=5, lines=8)
        delta = await state.get_history_snapshot(
            "f", after=first["progress"], known_signature=first["signature"]
        )
        assert delta["delivery"] == "delta"
        assert delta["generation"] == generation
        assert len(delta["records"]) == 3

        idle = await state.get_history_snapshot(
            "f", after=delta["progress"], known_signature=delta["signature"]
        )
        assert idle["delivery"] == "not_modified"

    asyncio.run(scenario())


def test_pending_window_is_memoized_but_still_tracks_the_cursor():
    async def scenario():
        state = ServerState(history_cache_budget_bytes=1024 * 1024 * 1024)
        await _register_flows(state, ["f"])
        # The cursor declares 8 lines but only 5 records have been delivered.
        await state.apply_history_frame(
            "f",
            protocol.HISTORY_MODE_FULL,
            _records("f", 5),
            cursor={_step_file("f"): 8},
            machine_id=MACHINE,
        )
        first = await state.get_history_snapshot("f")
        assert first["pending"]["f"] == [5, 6, 7]
        # A second read must serve the memoized answer unchanged...
        again = await state.get_history_snapshot("f")
        assert again["pending"]["f"] == [5, 6, 7]
        # ...and the memo must be invalidated by the very next append.
        await _append(state, "f", _records("f", 3, start=5), base=5, lines=8)
        after = await state.get_history_snapshot("f")
        assert after["pending"] == {}

    asyncio.run(scenario())


def test_empty_full_rejection_for_an_active_worktree_flow_survives_eviction():
    async def scenario():
        state = ServerState(history_cache_budget_bytes=1)
        await state.update_status(
            MACHINE,
            {
                "machine_id": MACHINE,
                "flows": [
                    {
                        "flow_id": "wt",
                        "project_root": "/repo/tianluo/worktrees/wt-1",
                        "status": "paused",
                    }
                ],
            },
        )
        await _full(state, "wt", _records("wt", 20))
        await state.report_history_cache()
        assert await _cached(state, "wt") is None

        # A user re-opens the flow, and the daemon's pull reply comes back with
        # a failed read (empty full). That must still be refused as
        # untrustworthy and keep the self-heal armed — eviction may not turn an
        # unresolved daemon read into an authoritative blank chat.
        assert await state.get_history_snapshot("wt") is None
        outcome = await _full(state, "wt", [], lines=0)
        assert outcome.rejected_full is True
        assert outcome.resolves_pull is False
        assert await _cached(state, "wt") is None
        assert await state.take_recovery_pull("wt") is True

    asyncio.run(scenario())


def test_shrinking_full_is_still_rejected_while_the_bundle_is_resident():
    async def scenario():
        state = ServerState(history_cache_budget_bytes=1024 * 1024 * 1024)
        await state.update_status(
            MACHINE,
            {
                "machine_id": MACHINE,
                "flows": [
                    {
                        "flow_id": "wt",
                        "project_root": "/repo/tianluo/worktrees/wt-1",
                        "status": "paused",
                    }
                ],
            },
        )
        await _full(state, "wt", _records("wt", 10))
        outcome = await _full(state, "wt", _records("wt", 3))
        assert outcome.rejected_full is True
        bundle = await _cached(state, "wt")
        assert len(bundle["records"]) == 10

    asyncio.run(scenario())


def test_owner_takeover_clears_the_cold_marker_it_created():
    async def scenario():
        state = ServerState(history_cache_budget_bytes=1)
        await state.register_machine(MACHINE, owner_id="owner-a")
        await _register_flows(state, ["f"])
        await _full(state, "f", _records("f", 20))
        await state.report_history_cache()
        assert "f" in state._history_cold

        # A different owner takes the machine_id over: the flow's records were
        # discarded because they belonged to the previous owner, not because the
        # budget refused them, so the new owner must be able to cache its own.
        await state.register_machine(MACHINE, owner_id="owner-b")
        assert "f" not in state._history_cold

    asyncio.run(scenario())


def test_accounting_and_pending_caches_never_leak_onto_the_wire():
    """The new private bundle keys follow the same rule as ``_key_index``."""

    async def scenario():
        state = ServerState(history_cache_budget_bytes=1024 * 1024 * 1024)
        await _register_flows(state, ["f"])
        await _full(state, "f", _records("f", 5))
        await _append(state, "f", _records("f", 2, start=5), base=5, lines=7)
        payloads = [
            await state.get_history("f"),
            await state.get_history_bundle_meta("f"),
            await state.get_history_snapshot("f"),
        ]
        for payload in payloads:
            for private in ("_bytes", "_bytes_len", "_pending", "_pending_key"):
                assert private not in payload

    asyncio.run(scenario())


def test_cjk_bundles_are_not_under_counted_against_the_budget():
    """A budget that under-counts its own content does not bound anything.

    The deployment's configured language is zh-CN, so history text is mostly
    CJK: PEP 393 stores it at 2 B/char, and charging 1 B/char let a bundle sit
    at twice the resident cost the budget believed it had admitted.
    """

    async def scenario():
        chars = 4_000
        count = 20
        state = ServerState(history_cache_budget_bytes=1024 * 1024 * 1024)
        await _register_flows(state, ["cjk"])
        records = _records("cjk", count, payload_chars=1)
        for record in records:
            record["message"]["content"] = "\u7530\u87ba" * (chars // 2)
        await _full(state, "cjk", records)
        return (await state.history_cache_stats())["bytes"]

    accounted = asyncio.run(scenario())
    assert accounted >= 2 * 20 * 4_000


# ---------------------------------------------------------------------------
# observability
# ---------------------------------------------------------------------------


def test_threshold_report_names_the_biggest_flows(caplog):
    async def scenario():
        probe = ServerState(history_cache_budget_bytes=1024 * 1024 * 1024)
        await _full(probe, "p", _records("p", 20))
        one = (await probe.history_cache_stats())["bytes"]
        # Budget generous enough that a single bundle does not evict, but small
        # enough that it lands past the 80 % report threshold.
        state = ServerState(history_cache_budget_bytes=int(one * 1.1))
        await _register_flows(state, ["loud"])
        await _full(state, "loud", _records("loud", 20))
        return state

    with caplog.at_level(logging.WARNING, logger="tianluo.server.state"):
        asyncio.run(scenario())
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "history-cache report" in text
    assert "loud" in text
    assert "reason=threshold" in text


def test_eviction_is_logged_with_the_flow_it_dropped(caplog):
    async def scenario():
        state = ServerState(history_cache_budget_bytes=1)
        await _register_flows(state, ["victim"])
        await _full(state, "victim", _records("victim", 20))
        await state.report_history_cache()

    with caplog.at_level(logging.INFO, logger="tianluo.server.state"):
        asyncio.run(scenario())
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "history-cache EVICT flow=victim" in text


def test_periodic_report_is_emitted_even_with_an_empty_cache(caplog):
    with caplog.at_level(logging.INFO, logger="tianluo.server.state"):
        asyncio.run(ServerState().report_history_cache())
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "reason=periodic" in text


# ---------------------------------------------------------------------------
# configuration wiring
# ---------------------------------------------------------------------------


def test_server_config_history_cache_defaults_and_parsing():
    from tianluo.config import HistoryCacheConfig, ServerConfig

    assert ServerConfig.from_dict({}).history_cache == HistoryCacheConfig()
    assert ServerConfig.from_dict({}).history_cache.budget_bytes() == (
        256 * 1024 * 1024
    )
    cfg = ServerConfig.from_dict(
        {
            "history_cache": {
                "budget_mb": 16,
                "report_interval_seconds": 30,
                "report_threshold_percent": 50,
            }
        }
    )
    assert cfg.history_cache.budget_bytes() == 16 * 1024 * 1024
    assert cfg.history_cache.report_interval_seconds == 30
    assert cfg.history_cache.report_threshold_percent == 50
    # 0 is a meaningful setting for the budget, not a typo to correct away.
    assert ServerConfig.from_dict(
        {"history_cache": {"budget_mb": 0}}
    ).history_cache.budget_mb == 0
    # A negative / non-numeric value falls back to the default rather than
    # disabling the ceiling by accident.
    assert ServerConfig.from_dict(
        {"history_cache": {"budget_mb": -5}}
    ).history_cache.budget_mb == 256
    assert ServerConfig.from_dict(
        {"history_cache": {"budget_mb": "lots"}}
    ).history_cache.budget_mb == 256


def test_yaml_budget_reaches_the_running_servers_state(tmp_path):
    """The knob is only real if it survives config → create_app → ServerState."""
    import yaml

    from tianluo.config import ServerConfig
    from tianluo.server.app import (
        _create_app_kwargs_from_server_config,
        create_app,
    )

    project = tmp_path / "proj"
    project.mkdir()
    (project / "tianluo.yaml").write_text(
        yaml.safe_dump({"server": {"history_cache": {"budget_mb": 7}}}),
        encoding="utf-8",
    )
    cfg = ServerConfig.load(project)
    kwargs = _create_app_kwargs_from_server_config(cfg)
    assert kwargs["history_cache_budget_bytes"] == 7 * 1024 * 1024

    app = create_app(**kwargs)
    assert app.state.server_state._history_cache_budget_bytes == 7 * 1024 * 1024


# ---------------------------------------------------------------------------
# event-loop stall: heavy render / parse must not run on the loop
# ---------------------------------------------------------------------------


def test_history_body_render_round_trips_and_gzips():
    from tianluo.server.app import _render_history_body

    payload = {"flow_id": "f", "records": _records("f", 50)}
    plain, gzipped = _render_history_body(payload, False)
    assert gzipped is False
    assert json.loads(plain.decode("utf-8")) == payload

    compressed, gzipped = _render_history_body(payload, True)
    assert gzipped is True
    assert len(compressed) < len(plain)
    assert json.loads(gzip.decompress(compressed).decode("utf-8")) == payload


# The event-loop offload assertions that used to live here — "a big render /
# a big frame parse must not block the loop" — moved to
# ``test_event_loop_offload.py``. They were rewritten there against the real
# ``app._history_response`` / ``ws._decode_frame`` seams and made decidable
# (which thread the work ran on, and a rendezvous that deadlocks if the loop was
# blocked) instead of asserting on measured inter-tick gaps, which said nothing
# about the production path and flaked under a loaded CI worker.


@pytest.mark.parametrize("delivery_records", [10, 400])
def test_history_endpoint_serves_json_through_the_offloaded_renderer(
    delivery_records,
):
    """End to end: the endpoint's replies stay valid JSON on both render paths."""
    from fastapi.testclient import TestClient

    from _authsrv import authed_app, authed_hello, login

    app, _key = authed_app()
    with TestClient(app) as client:
        login(client)
        with client.websocket_connect("/ws") as sock:
            sock.send_text(authed_hello(app, MACHINE, "host", "12.0.0"))
            protocol.decode(sock.receive_text())  # WELCOME
            sock.send_text(
                protocol.make_history_data(
                    "f",
                    protocol.HISTORY_MODE_FULL,
                    _records("f", delivery_records, payload_chars=800),
                ).to_json()
            )
            body = None
            for _ in range(80):
                response = client.get("/api/history/f")
                if response.status_code == 200 and response.json().get("cached"):
                    body = response.json()
                    break
            assert body is not None, "bundle never became cache-visible"
            assert body["flow_id"] == "f"
            assert body["delivery"] == "full"
            assert len(body["records"]) == delivery_records


def _receive_until(daemon, msg_type):
    """Read frames from *daemon*, skipping the expected background broadcasts."""
    while True:
        frame = protocol.decode(daemon.receive_text())
        if frame.type == msg_type:
            return frame
        assert frame.type in (
            protocol.MSG_HISTORY_INDEX_REQUEST,
            protocol.MSG_VIEWERS,
        )


def test_evicted_flow_is_re_sourced_through_the_real_cache_miss_pull():
    """End to end: evict a bundle, then re-open it in the console.

    This is the whole contract in one pass — the budget drops an unwatched
    bundle, the REST read of that flow becomes a genuine cache miss, the server
    asks the owning daemon for a CURSORLESS (hence ``full``) pull rather than an
    increment anchored on a water mark it no longer holds, and the reply
    reinstates the complete conversation.
    """
    from fastapi.testclient import TestClient

    from _authsrv import authed_app, authed_hello, login

    app, _key = authed_app(history_cache_budget_bytes=1)
    with TestClient(app) as client:
        login(client)
        with client.websocket_connect("/ws") as daemon:
            daemon.send_text(authed_hello(app, MACHINE, "host", "12.0.0"))
            protocol.decode(daemon.receive_text())  # WELCOME
            daemon.send_text(
                protocol.make_history_index(
                    [{"flow_id": "f1"}, {"flow_id": "f2"}]
                ).to_json()
            )
            for _ in range(80):
                if client.get("/api/history").json()["sessions"]:
                    break

            records = _records("f1", 20, payload_chars=800)
            daemon.send_text(
                protocol.make_history_data(
                    "f1", protocol.HISTORY_MODE_FULL, records
                ).to_json()
            )
            # A second flow's frame is what runs the budget sweep: "f1" has
            # never been viewed, so it is the one that goes.
            daemon.send_text(
                protocol.make_history_data(
                    "f2",
                    protocol.HISTORY_MODE_FULL,
                    _records("f2", 5, payload_chars=800),
                ).to_json()
            )
            for _ in range(80):
                if client.get("/api/history/f2").status_code == 200:
                    break

            result: dict = {}

            def do_get():
                result["resp"] = client.get("/api/history/f1")

            worker = threading.Thread(target=do_get)
            worker.start()
            try:
                req = _receive_until(daemon, protocol.MSG_HISTORY_REQUEST)
                assert req.payload["flow_id"] == "f1"
                # No cursor ⇒ the daemon answers with a FULL snapshot. An
                # incremental here would resume from the daemon's retained water
                # mark and bake the evicted head into a permanent hole.
                assert not req.payload.get("cursor")
                daemon.send_text(
                    protocol.make_history_data(
                        "f1", protocol.HISTORY_MODE_FULL, records
                    ).to_json()
                )
            finally:
                worker.join(timeout=10)

            resp = result["resp"]
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert body["cached"] is False
            assert body["delivery"] == "full"
            assert len(body["records"]) == 20
