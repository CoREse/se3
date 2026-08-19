"""Worktree discovery observability seam — per-hop AND end-to-end regression.

The earlier tests lock in the cooperative invariant that a ``se3 run --worktree``
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

Group G4 adds the END-TO-END coverage for issue #278 (worktree discovery chat
records after round 1 vanished from the live console): the tests below chain the
REAL daemon ``read_flow`` → REAL server ``ServerState`` relay → a faithful port
of the ``app.js`` frontend reconcile, so a multi-round worktree discovery is
verified fully visible across the WHOLE seam — a collision an isolated per-hop
unit missed still fails here.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest

from tianluo.daemon import protocol
from tianluo.daemon.aggregator import DaemonAggregator
from tianluo.daemon.history import DaemonHistoryReader
from tianluo.engine.models import FlowStatus
from tianluo.engine.persistence import PersistenceManager
from tianluo.engine.state_machine import StateMachine
from tianluo.server.state import ServerState

# WHY: pinned to the repo-wide serial xdist group. These modules drive git
# worktree lifecycle operations and daemon/registry state that are shared
# process-external resources (the real repo's .git worktree metadata, a
# project-roots registry, socket-backed daemon reads); running them on separate
# xdist workers concurrently produces batches of git-contention ERRORs rather
# than genuine failures. The accepted trade-off is to give up parallelism for
# this slice instead of retrofitting the tests for concurrency. Requires
# ``--dist loadgroup`` (which ``test.parallel`` appends) for the group to mean
# anything.
pytestmark = pytest.mark.xdist_group(name="repo_serial")


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

    Before any discovery LLM call, ``<worktree>/tianluo/state/engine.json`` must
    already describe an ``is_worktree_mode`` flow at status INIT.
    """
    wt_root = tmp_path / "proj" / "tianluo" / "worktrees" / "feat-x"
    wt_root.mkdir(parents=True)

    flow = _eager_save_worktree_flow(wt_root)
    assert flow.status == FlowStatus.INIT

    engine_json = wt_root / "tianluo" / "state" / "engine.json"
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
    wt_root = main_root / "tianluo" / "worktrees" / "feat-x"
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
    wt_root = main_root / "tianluo" / "worktrees" / "feat-x"
    wt_root.mkdir(parents=True)
    flow = _eager_save_worktree_flow(wt_root)
    flow_id = flow.flow_id

    # Discovery writes its first record into the worktree's own history dir.
    # The very first snapshot lands while the writer has flushed a COMPLETE
    # record but not yet its trailing newline.
    hist = wt_root / "tianluo" / "history" / flow_id / "01_discovery_ab.jsonl"
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
    observable set even though it shares the ``tianluo/worktrees/`` parent.
    """
    main_root = tmp_path / "proj"
    wt_root = main_root / "tianluo" / "worktrees" / "impl-g2"
    state_dir = wt_root / "tianluo" / "state"
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
    wt_root = main_root / "tianluo" / "worktrees" / "feat-x"
    wt_root.mkdir(parents=True)
    flow = _eager_save_worktree_flow(wt_root)
    flow_id = flow.flow_id

    # Discovery's first reply flushed complete but without a trailing newline.
    hist = wt_root / "tianluo" / "history" / flow_id / "01_discovery_ab.jsonl"
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
    assert all("/tianluo/worktrees/" not in r for r in agg.all_project_roots())
    assert wt_real not in [os.path.realpath(str(p)) for p in agg.project_roots]
    # The registry callback only ever recorded the main root.
    assert all("/tianluo/worktrees/" not in r for r in persisted)
    assert main_real in [os.path.realpath(r) for r in persisted]


# ==========================================================================
# Group G4 — end-to-end: worktree multi-round discovery live chat is fully
# visible across the WHOLE seam (daemon read_flow → server relay → frontend
# reconcile), with NO 2nd+ round loss (issue #278).
# ==========================================================================
#
# The reported bug (#278): in a ``se3 run --worktree`` flow the discovery
# step's chat records after the FIRST round vanished from the web console —
# only round 1 rendered live, and everything the agent said in later rounds was
# reachable ONLY through the adjudication "show details" affordance, never as
# live chat. G1 fixed the daemon read path (distinct per-physical-file step_id +
# stable worktree copy selection + copy-switch clean re-read), G2 proved the
# server relay passes that disambiguated identity through losslessly, G3
# hardened the frontend reconcile. This module's earlier tests lock the
# observe/register seam per-hop; the tests below chain ALL THREE hops with the
# REAL daemon reader and REAL server ``ServerState`` so a regression in any one
# hop that only manifests end-to-end (a collision the isolated unit missed)
# still fails here.
#
# The frontend is mirrored by :class:`_FrontendConsole`, a faithful port of the
# reconcile invariants in ``server/static/app.js`` — ``recordKey`` (the stable
# ``stepId#ordinal`` identity), ``reconcileAppendRecords`` (idempotent append),
# and ``dedupeSnapshotClones`` (content-aware full-snapshot de-dup). The JS
# node harness (``tests/frontend/worktree_discovery_multiround.test.mjs``) pins
# the real JS; this port lets the SAME records flow through the real Python hops
# into a frontend without a browser.


def _disc_msg(role, content):
    return {"role": role, "content": content}


class _FrontendConsole:
    """Faithful Python mirror of the ``app.js`` history reconcile.

    Consumes the exact ``(mode, records)`` frames the daemon push loop hands the
    server relay (and which the server re-broadcasts over ``/ws/ui`` verbatim),
    reconciling them the way the WebUI does: a ``full`` snapshot replaces the
    held records after :meth:`_dedupe_snapshot_clones`, an ``append`` batch is
    merged idempotently by :meth:`_record_key`. ``held`` is what the chat would
    render.
    """

    def __init__(self):
        self.held: list = []

    # -- identity (mirrors app.js recordKey / recordOrdinal / legacyKeyFromNorm)
    @staticmethod
    def _ordinal(rec):
        o = rec.get("ordinal")
        if o is None and isinstance(rec.get("message"), dict):
            o = rec["message"].get("ordinal")
        return o if isinstance(o, int) else None

    @staticmethod
    def _legacy_key(rec):
        msg = rec.get("message") if isinstance(rec.get("message"), dict) else rec
        content = msg.get("content") if isinstance(msg.get("content"), str) else ""
        return "\x00".join(
            [
                str(rec.get("step_id", "")),
                str(msg.get("role", "")),
                str(len(content)),
                content[:96],
            ]
        )

    def _record_key(self, rec):
        ordinal = self._ordinal(rec)
        step_id = rec.get("step_id")
        if ordinal is not None and step_id:
            return f"{step_id}#{ordinal}"
        return self._legacy_key(rec)

    # -- dedupeSnapshotClones: content-aware de-dup of a full snapshot --------
    def _dedupe_snapshot_clones(self, records):
        """Collapse a byte-identical clone of ANY step, keeping the first.

        Mirrors the JS after it was generalized off discovery: the collapse rule
        is unchanged (same record key AND same content signature), only its
        scope widened, so a same-key/different-content record is still kept.
        """
        seen: dict = {}
        out = []
        for rec in records:
            key = self._record_key(rec)
            sig = self._legacy_key(rec)
            sigs = seen.get(key)
            if sigs is not None:
                if sig in sigs:  # byte-identical clone — drop
                    continue
                sigs.add(sig)
            else:
                seen[key] = {sig}
            out.append(rec)
        return out

    # -- reconcileAppendRecords: idempotent append merge ----------------------
    def _reconcile_append(self, incoming):
        idx_by_key = {self._record_key(r): i for i, r in enumerate(self.held)}
        fresh_keys: set = set()
        for rec in incoming:
            key = self._record_key(rec)
            if key in idx_by_key:
                at = idx_by_key[key]
                # Same stable line: converge to newest content in place (a retry
                # rewrote it), skip a byte-identical re-delivery. Never a 2nd
                # bubble.
                if self._legacy_key(self.held[at]) != self._legacy_key(rec):
                    self.held[at] = rec
                continue
            if key in fresh_keys:
                continue
            fresh_keys.add(key)
            self.held.append(rec)

    def apply(self, mode, records):
        """Apply one daemon push frame exactly as the WebUI would."""
        recs = [dict(r) for r in records]
        if mode == protocol.HISTORY_MODE_FULL:
            self.held = self._dedupe_snapshot_clones(recs)
        else:
            self._reconcile_append(recs)

    # -- assertions helpers ---------------------------------------------------
    def contents(self):
        return [r["message"]["content"] for r in self.held]

    def keys(self):
        return [self._record_key(r) for r in self.held]


def _make_reader(*roots):
    return DaemonHistoryReader(
        project_roots_provider=lambda: [str(r) for r in roots]
    )


def _wt_flow_dir(root, flow_id):
    d = root / "tianluo" / "history" / flow_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _make_worktree(main_root, name="wt__b"):
    (main_root / "tianluo").mkdir(parents=True, exist_ok=True)
    wt = main_root / "tianluo" / "worktrees" / name
    wt.mkdir(parents=True, exist_ok=True)
    return wt


def _write_jsonl(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )


def _append_jsonl(path, lines):
    with path.open("a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")


def test_e2e_worktree_multiround_discovery_all_rounds_reach_frontend(tmp_path):
    """#278 end-to-end: every discovery round reaches the frontend chat, live.

    Chains the WHOLE seam for a live ``--worktree`` discovery whose single
    append-only file grows across four rounds (the real
    ``run_worktree_mode`` topology — discovery runs entirely in the worktree):

        REAL DaemonHistoryReader.read_flow (incremental)
          → REAL ServerState.append_history (relay cache + /ws/ui broadcast)
          → _FrontendConsole reconcile (recordKey = stepId#ordinal)

    Asserts that every intermediate round's chat records — not just round 1 —
    render on the frontend, that the final adjudication verdict is visible, that
    no round is dropped or duplicated, and that the server's authoritative
    bundle matches what the frontend holds (relay is lossless).
    """
    main = tmp_path / "main"
    wt = _make_worktree(main)
    flow_id = "wt-e2e"
    disc = _wt_flow_dir(wt, flow_id) / "01_discovery_ab12.jsonl"

    reader = _make_reader(main, wt)
    console = _FrontendConsole()
    state = ServerState()

    # The four discovery rounds, appended one at a time to the SAME worktree
    # file exactly as the engine's append-only chat_history writer does. The
    # last assistant line is the adjudication verdict that "show details"
    # surfaced pre-fix but the live chat did not.
    rounds = [
        [
            _disc_msg("user", "the task"),
            _disc_msg("assistant", "round 1: thinking… + first reply"),
        ],
        [
            _disc_msg("user", "round 2 clarification answer"),
            _disc_msg("assistant", "round 2: refined reply"),
        ],
        [
            _disc_msg("user", "round 3 clarification answer"),
            _disc_msg("assistant", "round 3: more detail"),
        ],
        [
            _disc_msg("assistant", "VERDICT: ready — proceeding to analyze"),
        ],
    ]

    async def scenario():
        cursor = None
        seen_full = False
        for i, round_lines in enumerate(rounds):
            if i == 0:
                _write_jsonl(disc, round_lines)
            else:
                _append_jsonl(disc, round_lines)

            # -- HOP 1: the daemon push loop reads the growing live file -------
            read = reader.read_flow(
                flow_id, project_root=str(wt), cursor=cursor
            )
            cursor = read.cursor

            # The very first frame MUST be a full snapshot (the server discards a
            # first-sighting append), and every later frame an incremental append
            # — the stable worktree selection keeps the reads incremental.
            if not seen_full:
                assert read.mode == protocol.HISTORY_MODE_FULL
                seen_full = True
            else:
                assert read.mode == protocol.HISTORY_MODE_APPEND
                # A live worktree round MUST carry records (the #278 symptom was
                # the daemon emitting an EMPTY append for round 2+ because the
                # cursor had wrongly consumed them).
                assert read.records, f"round {i + 1} produced no daemon records"

            # -- HOP 2: the server relay caches + would broadcast the frame ----
            applied = await state.append_history(
                flow_id, read.mode, read.records, machine_id="m1"
            )
            assert applied is True, f"relay discarded round {i + 1}'s frame"

            # -- HOP 3: the frontend applies the same (mode, records) frame ----
            console.apply(read.mode, read.records)

        # Every round's chat is present on the frontend, in order — the 2nd, 3rd
        # and 4th rounds did NOT vanish after round 1 (the #278 regression).
        assert console.contents() == [
            "the task",
            "round 1: thinking… + first reply",
            "round 2 clarification answer",
            "round 2: refined reply",
            "round 3 clarification answer",
            "round 3: more detail",
            "VERDICT: ready — proceeding to analyze",
        ]
        # The adjudication verdict is a live chat bubble, not only reachable via
        # "show details".
        assert "VERDICT: ready — proceeding to analyze" in console.contents()
        # Every record keeps a globally-unique stable identity (no collision that
        # would let a reconcile silently drop a round).
        keys = console.keys()
        assert len(keys) == len(set(keys))

        # The server's authoritative bundle is lossless and matches the frontend
        # exactly (relay added/dropped nothing).
        bundle = await state.get_history(flow_id)
        assert bundle is not None
        bundle_contents = [r["message"]["content"] for r in bundle["records"]]
        assert bundle_contents == console.contents()

    asyncio.run(scenario())


def test_e2e_worktree_discovery_cross_source_rounds_all_render(tmp_path):
    """#278 end-to-end with a split-root topology: primary + sidecar both render.

    A worktree discovery whose records are surfaced from MORE than one physical
    file — the worktree primary plus a ``.from-<branch>`` merge-back sidecar,
    each numbering its own lines from 0 — is the exact case the pre-G1 fold
    collided (the sidecar's ordinal-0 record shared ``stepId#ordinal`` with the
    primary's and was dropped as a duplicate). Chained through the real reader +
    real relay + frontend reconcile, BOTH sources must render, and the frontend
    dedupe must keep the distinct-content records while the identity stays
    unique.
    """
    main = tmp_path / "main"
    flow_id = "wt-e2e-sidecar"
    flow_dir = _wt_flow_dir(main, flow_id)
    # Post-merge single-root topology: the primary discovery file plus the
    # worktree's collision sidecar (both begin at ordinal 0).
    _write_jsonl(
        flow_dir / "01_discovery_ab12.jsonl",
        [
            _disc_msg("user", "the task"),
            _disc_msg("assistant", "primary round reply"),
        ],
    )
    _write_jsonl(
        flow_dir / "01_discovery_ab12.jsonl.from-worktree__b",
        [
            _disc_msg("assistant", "sidecar round reply"),
            _disc_msg("assistant", "VERDICT from sidecar"),
        ],
    )

    reader = _make_reader(main)
    console = _FrontendConsole()
    state = ServerState()

    async def scenario():
        read = reader.read_flow(flow_id, project_root=str(main))
        # The primary's and sidecar's ordinal-0 records carry DISTINCT step_ids,
        # so their stepId#ordinal keys never collide (pre-G1 they folded to one).
        step_ids = {r["step_id"] for r in read.records}
        assert step_ids == {
            "01_discovery_ab12",
            "01_discovery_ab12.from-worktree__b",
        }

        applied = await state.append_history(
            flow_id, read.mode, read.records, machine_id="m1"
        )
        assert applied is True
        console.apply(read.mode, read.records)

        # Both physical sources render in full — the sidecar round is NOT dropped
        # as a duplicate of the primary's ordinal-0 record.
        assert console.contents() == [
            "the task",
            "primary round reply",
            "sidecar round reply",
            "VERDICT from sidecar",
        ]
        keys = console.keys()
        assert len(keys) == len(set(keys))

        bundle = await state.get_history(flow_id)
        assert [r["message"]["content"] for r in bundle["records"]] == (
            console.contents()
        )

    asyncio.run(scenario())
