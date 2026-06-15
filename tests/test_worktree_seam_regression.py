"""Group G2 — close the discovery first-reply observability blind spot.

These tests lock in the cooperative invariant that a ``se3 run --worktree``
flow is observable (and its discovery first reply fully readable) from the very
first on-disk write, while the transient worktree sandbox never leaks into the
New Task project dropdown.

The blind spot was that a *pending*-type (discovery) worktree flow did not
persist ``engine.json`` at creation, so the daemon's strict ``is_worktree_mode``
gate in :meth:`DaemonAggregator._active_worktree_run_roots` could not yet admit
the worktree's live history during the discovery startup window. The run-command
fix saves ``engine.json`` eagerly for a worktree-mode flow (carrying
``is_worktree_mode=True`` + ``worktree_path`` at status ``INIT``) before the
first LLM call writes any history.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from se3.daemon.aggregator import DaemonAggregator
from se3.daemon.history import DaemonHistoryReader
from se3.engine.models import FlowStatus
from se3.engine.persistence import PersistenceManager
from se3.engine.state_machine import StateMachine


def _eager_save_worktree_flow(worktree_root: Path, branch: str = "worktree/feat-x"):
    """Mimic ``run._run_flow_impl``'s worktree-mode eager save.

    Creates a pending-type worktree-mode flow inside *worktree_root* and saves
    ``engine.json`` immediately — exactly the sequence the run command performs
    after recording the worktree metadata, before discovery's first LLM call.
    """
    sm = StateMachine(worktree_root)
    flow = sm.create_flow(
        task_description="isolated task",
        task_type="pending",
        is_worktree_mode=True,
    )
    flow.worktree_path = str(worktree_root)
    flow.worktree_branch = branch
    flow.worktree_original_branch = "main"
    # The eager save the run command now performs unconditionally for a
    # worktree-mode flow (no explicit --type needed).
    PersistenceManager(worktree_root).save_flow(flow)
    return flow


def test_eager_save_persists_is_worktree_mode_at_init(tmp_path):
    """Task 1: the eager save writes is_worktree_mode + worktree_path early.

    Before any discovery LLM call, ``<worktree>/se3/state/engine.json`` must
    already describe an ``is_worktree_mode`` flow at status INIT.
    """
    wt_root = tmp_path / "proj" / "se3" / "worktrees" / "feat-x"
    wt_root.mkdir(parents=True)

    flow = _eager_save_worktree_flow(wt_root)
    assert flow.status == FlowStatus.INIT

    engine_json = wt_root / "se3" / "state" / "engine.json"
    assert engine_json.is_file()
    data = json.loads(engine_json.read_text(encoding="utf-8"))
    assert data["is_worktree_mode"] is True
    assert data["worktree_path"] == str(wt_root)
    assert data["flow_id"]
    # INIT is an active (not COMPLETED/FAILED) status, so the flow is eligible
    # for live observation from this very first write.
    assert data["status"].upper() in {"INIT", "PENDING", "RUNNING"}


def test_worktree_observable_at_discovery_startup_window(tmp_path):
    """G2 cooperative: the worktree is observable from its first engine.json.

    With the eager save in place, ``_active_worktree_run_roots`` admits the
    worktree at status INIT (the discovery startup window) — not only once the
    first step flips it to RUNNING.
    """
    main_root = tmp_path / "proj"
    wt_root = main_root / "se3" / "worktrees" / "feat-x"
    wt_root.mkdir(parents=True)
    _eager_save_worktree_flow(wt_root)

    agg = DaemonAggregator()
    agg.add_project_root(main_root)

    observable = agg.all_observable_roots()
    assert os.path.realpath(str(wt_root)) in observable
    # ...and the transient sandbox is NOT a New Task dropdown target — the
    # "fix one, don't pop out the other" cooperative invariant.
    assert os.path.realpath(str(wt_root)) not in agg.all_project_roots()


def test_discovery_first_reply_read_live_at_init(tmp_path):
    """G2 cooperative: the discovery first reply (thinking + result) reads live.

    Models the daemon's first snapshot landing right after the eager save and
    after discovery flushes its first complete record without a trailing
    newline. The chain — observable root → build_index (active) →
    read_active_flows scoped to the worktree's own root → trailing-line
    parseability — must surface the full first reply, then keep appending.
    """
    main_root = tmp_path / "proj"
    wt_root = main_root / "se3" / "worktrees" / "feat-x"
    wt_root.mkdir(parents=True)
    flow = _eager_save_worktree_flow(wt_root)
    flow_id = flow.flow_id

    # Discovery writes its first record into the worktree's own history dir.
    # The very first snapshot lands while the writer has flushed a COMPLETE
    # record but not yet its trailing newline.
    hist = wt_root / "se3" / "history" / flow_id / "01_discovery_ab.jsonl"
    hist.parent.mkdir(parents=True, exist_ok=True)
    first = {
        "role": "assistant",
        "content": "thinking… and the final result",
        "raw_json": [],
        "step_type": "discovery",
    }
    hist.write_text(json.dumps(first), encoding="utf-8")  # no trailing newline

    agg = DaemonAggregator()
    agg.add_project_root(main_root)
    reader = DaemonHistoryReader(
        project_roots_provider=lambda: agg.all_observable_roots()
    )

    # The worktree flow is indexed as active from the INIT engine.json.
    metas = {m.flow_id: m for m in reader.build_index()}
    assert flow_id in metas
    assert metas[flow_id].active is True

    reads = {r.flow_id: r for r in reader.read_active_flows({})}
    assert flow_id in reads
    first_read = reads[flow_id]
    contents = [r["message"]["content"] for r in first_read.records]
    # The complete-but-unterminated first reply is consumed in full — not the
    # "first assistant body empty, then nothing further" symptom.
    assert contents == ["thinking… and the final result"]
    assert all(r["message"]["content"] for r in first_read.records)

    # A subsequent message keeps appending incrementally (no loss/dup/truncate).
    with hist.open("a", encoding="utf-8") as fh:
        fh.write("\n")
        fh.write(
            json.dumps(
                {
                    "role": "assistant",
                    "content": "second message",
                    "raw_json": [],
                    "step_type": "discovery",
                }
            )
            + "\n"
        )
    second_reads = {
        r.flow_id: r
        for r in reader.read_active_flows({flow_id: first_read.cursor})
    }
    assert flow_id in second_reads
    follow = [r["message"]["content"] for r in second_reads[flow_id].records]
    assert follow == ["second message"]


def test_dag_isolation_worktree_stays_excluded(tmp_path):
    """The eager save does not regress the DAG-isolation exclusion.

    A DAG implement-isolation worktree never writes a top-level
    ``is_worktree_mode`` flow record, so the strict gate must keep it out of the
    observable set even though it shares the ``se3/worktrees/`` parent.
    """
    main_root = tmp_path / "proj"
    wt_root = main_root / "se3" / "worktrees" / "impl-g2"
    state_dir = wt_root / "se3" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "engine.json").write_text(
        json.dumps({"flow_id": "impl-flow", "status": "RUNNING"}),
        encoding="utf-8",
    )

    agg = DaemonAggregator()
    agg.add_project_root(main_root)
    assert os.path.realpath(str(wt_root)) not in agg.all_observable_roots()


def test_seam_observable_and_readable_yet_never_registered(tmp_path):
    """G3 bidirectional guard: both seam invariants asserted in ONE test.

    A single simulated worktree flow must simultaneously satisfy:

    * **observe-side (Bug1)** — the worktree is in the observable set from its
      INIT engine.json and its discovery first reply (complete, unterminated)
      reads live, then keeps appending; and
    * **register-side (Bug2)** — the worktree never enters the active set, the
      persistent registry, or the dropdown-facing ``all_project_roots`` view.

    Reverting either fix breaks this test: drop the eager save and the worktree
    is not observable / the first reply is empty; drop the normalization and the
    worktree leaks into the registry / project list. This is the "fix one,
    don't pop out the other" lock.
    """
    main_root = tmp_path / "proj"
    wt_root = main_root / "se3" / "worktrees" / "feat-x"
    wt_root.mkdir(parents=True)
    flow = _eager_save_worktree_flow(wt_root)
    flow_id = flow.flow_id

    # Discovery's first reply flushed complete but without a trailing newline.
    hist = wt_root / "se3" / "history" / flow_id / "01_discovery_ab.jsonl"
    hist.parent.mkdir(parents=True, exist_ok=True)
    hist.write_text(
        json.dumps(
            {
                "role": "assistant",
                "content": "thinking… and the final result",
                "raw_json": [],
                "step_type": "discovery",
            }
        ),
        encoding="utf-8",
    )

    persisted: list = []
    agg = DaemonAggregator(registry_persist=persisted.append)
    # A caller mistakenly handing the worktree path in must still normalize.
    agg.add_project_root(main_root)
    agg.add_project_root(str(wt_root))

    wt_real = os.path.realpath(str(wt_root))
    main_real = os.path.realpath(str(main_root))

    # -- observe-side invariant (Bug1) -------------------------------------
    assert wt_real in agg.all_observable_roots()
    reader = DaemonHistoryReader(
        project_roots_provider=lambda: agg.all_observable_roots()
    )
    metas = {m.flow_id: m for m in reader.build_index()}
    assert flow_id in metas and metas[flow_id].active is True
    first = {r.flow_id: r for r in reader.read_active_flows({})}[flow_id]
    assert [r["message"]["content"] for r in first.records] == [
        "thinking… and the final result"
    ]
    with hist.open("a", encoding="utf-8") as fh:
        fh.write("\n")
        fh.write(
            json.dumps(
                {
                    "role": "assistant",
                    "content": "second message",
                    "raw_json": [],
                    "step_type": "discovery",
                }
            )
            + "\n"
        )
    second = {
        r.flow_id: r for r in reader.read_active_flows({flow_id: first.cursor})
    }[flow_id]
    assert [r["message"]["content"] for r in second.records] == ["second message"]

    # -- register-side invariant (Bug2) ------------------------------------
    assert wt_real not in agg.all_project_roots()
    assert main_real in agg.all_project_roots()
    assert all("/se3/worktrees/" not in r for r in agg.all_project_roots())
    assert wt_real not in [os.path.realpath(str(p)) for p in agg.project_roots]
    # The registry callback only ever recorded the main root.
    assert all("/se3/worktrees/" not in r for r in persisted)
    assert main_real in [os.path.realpath(r) for r in persisted]
