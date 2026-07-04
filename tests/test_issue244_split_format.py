"""Regression tests for issue #244 一期 — engine.json hot/cold split.

Covers the acceptance surface for the Part B / G7 (B4/B5) work:

* (d) new-format ``save`` → ``load`` round-trips equal, incl. resumable snapshot;
* (e) legacy inline engine.json / resumable snapshots still load;
* (f) the engine.json header stays bounded (<100 KB) even for a flow with many
  steps carrying large inputs/outputs;
* (g) per-step persistence touches only the header + that step's cold file
  (write volume proportional to the step, not the flow);
* (h) a missing / corrupt cold step file degrades that step to empty IO without
  crashing the whole load;
* (i) resume reloads a partial new-format flow at full fidelity, and single-step
  cold data can be pulled on demand (B4);
* B5: ``clear_state`` archives header + cold files together (full fidelity) and
  ``list_all_flows`` / export keep working for both formats.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from se3.engine.models import (
    FlowInstance,
    FlowStatus,
    Step,
    StepStatus,
    StepType,
)
from se3.engine.persistence import ENGINE_FORMAT_HOTCOLD, PersistenceManager


def _make_flow(
    n_steps: int = 6,
    payload_size: int = 50_000,
    status: FlowStatus = FlowStatus.RUNNING,
    worktree: bool = False,
) -> FlowInstance:
    """Build a flow whose steps carry large inputs/outputs (like a real flow)."""
    flow = FlowInstance(task_description="split-format flow", status=status)
    flow.task_type = "feature"
    if worktree:
        flow.is_worktree_mode = True
        flow.worktree_branch = "impl/x/G1"
        flow.worktree_path = "/repo/se3/worktrees/g1"
    blob = "Q" * payload_size
    for i in range(n_steps):
        step = Step(step_type=StepType.IMPLEMENT, status=StepStatus.COMPLETED)
        step.inputs = {"test_results": blob, "idx": i}
        step.outputs = {"artifact_blob": blob, "ok": True}
        flow.state.add_step(step)
    flow.state.selected_steps = [StepType.IMPLEMENT]
    flow.state.current_step_id = flow.state.step_history[-1]
    flow.state.context = {"spec_content": blob, "resolved_type": "feature"}
    flow.state.increment_fix_iteration({"reason": "big fix context", "blob": blob})
    return flow


# -- (d) new-format round-trip ---------------------------------------------


def test_new_format_save_load_roundtrip_equal(tmp_path):
    pm = PersistenceManager(tmp_path)
    flow = _make_flow()
    pm.save_flow(flow)

    header = json.loads((tmp_path / "se3" / "state" / "engine.json").read_text())
    assert header["engine_format"] == ENGINE_FORMAT_HOTCOLD
    # The header must NOT carry per-step inputs/outputs nor the shared context;
    # both are externalized to cold files and referenced by hash from the header.
    for step in header["state"]["steps"].values():
        assert "inputs" not in step
        assert "outputs" not in step
        assert "cold_ref" in step
    assert "context" not in header["state"]
    assert "context_ref" in header["state"]

    loaded = pm.load_flow()
    assert loaded == flow


def test_resumable_snapshot_roundtrip_equal(tmp_path):
    pm = PersistenceManager(tmp_path)
    flow = _make_flow(status=FlowStatus.PAUSED)
    pm.save_flow(flow)  # non-completed → resumable snapshot written too

    snap = pm.load_resumable_snapshot(flow.flow_id)
    assert snap == flow
    # And it is discoverable via the by-id path (resumable, not active).
    assert pm.load_flow_by_id(flow.flow_id) == flow


# -- (e) legacy inline compatibility ---------------------------------------


def test_legacy_inline_engine_json_loads(tmp_path):
    pm = PersistenceManager(tmp_path)
    state_dir = tmp_path / "se3" / "state"
    state_dir.mkdir(parents=True)
    flow = _make_flow(n_steps=4)
    # Legacy format: full inline dict, no engine_format marker, no cold files.
    (state_dir / "engine.json").write_text(
        json.dumps(flow.to_dict(), indent=2, ensure_ascii=False, default=str)
    )
    assert not (state_dir / "steps").exists()

    loaded = pm.load_flow()
    assert loaded == flow  # inline inputs/outputs/context used verbatim


def test_legacy_inline_resumable_snapshot_loads(tmp_path):
    pm = PersistenceManager(tmp_path)
    resumable = tmp_path / "se3" / "state" / "resumable"
    resumable.mkdir(parents=True)
    flow = _make_flow(n_steps=3, status=FlowStatus.FAILED)
    (resumable / f"{flow.flow_id}.json").write_text(
        json.dumps(flow.to_dict(), indent=2, ensure_ascii=False, default=str)
    )
    assert pm.load_resumable_snapshot(flow.flow_id) == flow


# -- (f) bounded header -----------------------------------------------------


def test_header_bounded_under_100kb(tmp_path):
    pm = PersistenceManager(tmp_path)
    # 31 steps × ~700KB inputs is the real 50MB flow shape; the header must stay
    # KB-scale regardless.
    flow = _make_flow(n_steps=31, payload_size=700_000)
    pm.save_flow(flow)
    header_path = tmp_path / "se3" / "state" / "engine.json"
    assert header_path.stat().st_size < 100 * 1024


# -- (g) incremental per-step writes ---------------------------------------


def test_per_step_persistence_touches_only_changed_cold_file(tmp_path, monkeypatch):
    pm = PersistenceManager(tmp_path)
    flow = _make_flow(n_steps=8, status=FlowStatus.COMPLETED)
    pm.save_flow(flow)  # first save writes all cold files

    written: list = []
    real = PersistenceManager._atomic_write_json

    def _spy(path, data):
        written.append(Path(path).name)
        real(path, data)

    monkeypatch.setattr(pm, "_atomic_write_json", _spy)

    # Mutate exactly one step's outputs, leave everything else untouched.
    target = flow.state.step_history[3]
    flow.state.steps[target].outputs["artifact_blob"] = "changed"
    pm.save_flow(flow)

    # Only the changed step's cold file is (re)written; the other 7 steps and
    # the unchanged _context.json are skipped by the sha1 guard.
    assert f"{target}.json" in written
    other_cold = [
        f"{sid}.json"
        for sid in flow.state.step_history
        if sid != target
    ]
    assert not (set(other_cold) & set(written))
    assert "_context.json" not in written


# -- (h) cold-file corruption tolerance ------------------------------------


def test_missing_cold_step_file_degrades_gracefully(tmp_path):
    pm = PersistenceManager(tmp_path)
    flow = _make_flow(n_steps=4)
    pm.save_flow(flow)

    cold_dir = tmp_path / "se3" / "state" / "steps" / flow.flow_id
    victim = flow.state.step_history[1]
    (cold_dir / f"{victim}.json").unlink()  # simulate loss
    # Corrupt another step's cold file.
    other = flow.state.step_history[2]
    (cold_dir / f"{other}.json").write_text("{ not json")

    loaded = pm.load_flow()
    assert loaded is not None
    # The damaged steps degrade to empty IO; the rest survive intact.
    assert loaded.state.steps[victim].inputs == {}
    assert loaded.state.steps[victim].outputs == {}
    assert loaded.state.steps[other].inputs == {}
    survivor = flow.state.step_history[0]
    assert loaded.state.steps[survivor].inputs["idx"] == 0


def test_missing_cold_context_degrades_gracefully(tmp_path):
    pm = PersistenceManager(tmp_path)
    flow = _make_flow(n_steps=2)
    pm.save_flow(flow)
    cold_dir = tmp_path / "se3" / "state" / "steps" / flow.flow_id
    (cold_dir / "_context.json").unlink()

    loaded = pm.load_flow()
    assert loaded is not None
    assert loaded.state.context == {}  # empty, not a crash


# -- (i) resume fidelity + on-demand cold load (B4) ------------------------


def test_resume_partial_flow_reloads_full_fidelity(tmp_path):
    pm = PersistenceManager(tmp_path)
    flow = _make_flow(n_steps=5, status=FlowStatus.PAUSED)
    # Mark the flow mid-way, as a real interrupted resume would be.
    flow.state.current_step_index = 2
    flow.state.current_step_id = flow.state.step_history[2]
    pm.save_flow(flow)

    # A fresh manager (as a resuming `se3 run --resume` process would use).
    resumed = PersistenceManager(tmp_path).load_flow_by_id(flow.flow_id)
    assert resumed is not None
    assert resumed.state.current_step_id == flow.state.step_history[2]
    assert resumed.state.current_step_index == 2
    assert resumed == flow
    # Re-saving continues to write the new split format (never re-inlines).
    PersistenceManager(tmp_path).save_flow(resumed)
    header = json.loads((tmp_path / "se3" / "state" / "engine.json").read_text())
    assert header["engine_format"] == ENGINE_FORMAT_HOTCOLD


def test_load_flow_by_id_defers_step_cold_files(tmp_path, monkeypatch):
    """load_flow_by_id loads header + context only, then faults in steps lazily.

    The #244-B4 self-check fix: resuming a flow with many large completed steps
    must NOT re-read every step's cold file up front. Only the shared context
    cold file is read at load time; a step's body is read exactly once, and only
    when that step is fetched by id (as the engine's cross-step output scans do).
    """
    pm = PersistenceManager(tmp_path)
    flow = _make_flow(n_steps=6, status=FlowStatus.PAUSED)
    flow.state.current_step_id = flow.state.step_history[3]
    pm.save_flow(flow)

    reads: list = []
    real = PersistenceManager._read_cold_json

    def _spy(path, label, warnings):
        reads.append(label)
        return real(path, label, warnings)

    monkeypatch.setattr(PersistenceManager, "_read_cold_json", staticmethod(_spy))

    fresh = PersistenceManager(tmp_path)
    resumed = fresh.load_flow_by_id(flow.flow_id)
    assert resumed is not None
    # Only the shared context was read; no per-step cold file yet.
    assert reads == ["context"], reads
    assert not any(lbl.startswith("step ") for lbl in reads)

    # Fetching one step faults in exactly that step's cold file.
    wanted = flow.state.step_history[3]
    fetched = resumed.state.steps.get(wanted)
    assert fetched.inputs["idx"] == 3
    step_reads = [lbl for lbl in reads if lbl.startswith("step ")]
    assert step_reads == [f"step {wanted}"], step_reads

    # A second fetch of the same step does not re-read it (already hydrated).
    resumed.state.steps.get(wanted)
    assert [lbl for lbl in reads if lbl.startswith("step ")] == [f"step {wanted}"]


def test_hydrate_step_loads_only_requested_cold_file(tmp_path):
    pm = PersistenceManager(tmp_path)
    flow = _make_flow(n_steps=5)
    pm.save_flow(flow)

    # Load header only (no cold hydration) then pull one step on demand. The
    # header-only flow MUST come from from_header_dict (steps cold_loaded=False,
    # cold_ref retained): the generic from_dict marks steps cold_loaded=True with
    # empty bodies, which hydrate_step now correctly treats as already-loaded and
    # refuses to clobber (public B4 API must never destroy materialized data).
    data = json.loads((tmp_path / "se3" / "state" / "engine.json").read_text())
    header_flow = FlowInstance.from_header_dict(data)
    assert all(s.inputs == {} for s in header_flow.state.steps.values())
    assert all(not s.cold_loaded for s in header_flow.state.steps.values())

    wanted = flow.state.step_history[2]
    step = pm.hydrate_step(header_flow, wanted)
    assert step is not None
    assert step.inputs["idx"] == 2
    assert step.cold_loaded
    # Only the requested step was materialized; the others stay empty.
    for sid, s in header_flow.state.steps.items():
        if sid != wanted:
            assert s.inputs == {}

    # Idempotency / no-clobber: hydrating an already-loaded step is a no-op that
    # returns it unchanged rather than re-reading the cold file over live data.
    again = pm.hydrate_step(header_flow, wanted)
    assert again is step
    assert again.inputs["idx"] == 2


# -- B5: archive equivalence + listing/export -----------------------------


def test_clear_state_archives_header_and_cold_files(tmp_path):
    pm = PersistenceManager(tmp_path)
    flow = _make_flow(n_steps=4, status=FlowStatus.COMPLETED, worktree=True)
    pm.save_flow(flow)
    cold_dir = tmp_path / "se3" / "state" / "steps" / flow.flow_id
    assert cold_dir.is_dir()

    pm.clear_state()

    archive = tmp_path / "se3" / "state" / "archive"
    headers = list(archive.glob("engine_*.json"))
    assert len(headers) == 1
    # Header preserves format + identity; cold files followed it, full fidelity.
    archived_header = json.loads(headers[0].read_text())
    assert archived_header["flow_id"] == flow.flow_id
    assert archived_header["engine_format"] == ENGINE_FORMAT_HOTCOLD
    archived_cold = archive / "steps" / flow.flow_id
    assert archived_cold.is_dir()
    assert (archived_cold / "_context.json").is_file()
    assert len(list(archived_cold.glob("*.json"))) == 4 + 1  # steps + context
    # Live cold dir moved out (not left behind).
    assert not cold_dir.exists()


def test_clear_state_same_second_archives_do_not_collide(tmp_path, monkeypatch):
    """Two flows archived within the same second must both survive (B5).

    ``clear_state`` names archive headers ``engine_<YYYYMMDD_HHMMSS>.json``. With
    a second-granular timestamp, two archivals in the same second would target
    the same header path and the second Path.rename would silently replace the
    first — orphaning its cold partition and dropping the flow from
    list_all_flows / history show. The name must be made unique instead.
    """
    import datetime as _dt

    class _FrozenDatetime:
        @staticmethod
        def now():
            class _N:
                def strftime(self, fmt):
                    return "20260704_120000"

            return _N()

    pm = PersistenceManager(tmp_path)

    def _archive(flow):
        # Freeze the timestamp only across clear_state (which does a local
        # ``from datetime import datetime`` for the archive filename) so both
        # archivals collide on the same second — without polluting save_flow's
        # updated_at, which also reads datetime.now().
        pm.save_flow(flow)
        orig = _dt.datetime
        _dt.datetime = _FrozenDatetime
        try:
            pm.clear_state()
        finally:
            _dt.datetime = orig

    flow_a = _make_flow(n_steps=3, status=FlowStatus.COMPLETED)
    _archive(flow_a)

    flow_b = _make_flow(n_steps=3, status=FlowStatus.COMPLETED)
    assert flow_b.flow_id != flow_a.flow_id
    _archive(flow_b)

    archive = tmp_path / "se3" / "state" / "archive"
    headers = sorted(archive.glob("engine_*.json"))
    # Both headers preserved (no clobber), and both flows recoverable.
    assert len(headers) == 2
    archived_ids = {json.loads(h.read_text())["flow_id"] for h in headers}
    assert archived_ids == {flow_a.flow_id, flow_b.flow_id}
    listed = {f["flow_id"] for f in pm.list_all_flows()}
    assert {flow_a.flow_id, flow_b.flow_id} <= listed
    # Each flow's cold partition is intact and distinct.
    for fid in (flow_a.flow_id, flow_b.flow_id):
        assert (archive / "steps" / fid).is_dir()


def test_load_resumable_snapshot_defers_step_cold_files(tmp_path, monkeypatch):
    """load_resumable_snapshot loads header + context only, then faults in steps.

    The self-check fix: selecting a paused flow with many large completed steps
    must NOT re-materialize every step cold file before resume. Only the shared
    context is read at load; a step's body is read exactly once, on first fetch.
    """
    pm = PersistenceManager(tmp_path)
    flow = _make_flow(n_steps=6, status=FlowStatus.PAUSED)
    flow.state.current_step_id = flow.state.step_history[3]
    pm.save_flow(flow)

    reads: list = []
    real = PersistenceManager._read_cold_json

    def _spy(path, label, warnings):
        reads.append(label)
        return real(path, label, warnings)

    monkeypatch.setattr(PersistenceManager, "_read_cold_json", staticmethod(_spy))

    snap = PersistenceManager(tmp_path).load_resumable_snapshot(flow.flow_id)
    assert snap is not None
    # Only the shared context was read; no per-step cold file yet.
    assert reads == ["context"], reads

    wanted = flow.state.step_history[3]
    assert snap.state.steps.get(wanted).inputs["idx"] == 3
    step_reads = [lbl for lbl in reads if lbl.startswith("step ")]
    assert step_reads == [f"step {wanted}"], step_reads


def test_list_resumable_snapshots_defers_step_cold_files(tmp_path, monkeypatch):
    """Enumerating N paused snapshots reads no per-step cold file up front.

    The resume picker only consults header fields; parsing every step body of
    every snapshot is exactly the full-flow re-materialization the split format
    exists to avoid.
    """
    pm = PersistenceManager(tmp_path)
    flow_a = _make_flow(n_steps=5, status=FlowStatus.PAUSED)
    pm.save_flow(flow_a)
    flow_b = _make_flow(n_steps=4, status=FlowStatus.FAILED)
    pm.save_flow(flow_b)

    reads: list = []
    real = PersistenceManager._read_cold_json

    def _spy(path, label, warnings):
        reads.append(label)
        return real(path, label, warnings)

    monkeypatch.setattr(PersistenceManager, "_read_cold_json", staticmethod(_spy))

    snaps = PersistenceManager(tmp_path).list_resumable_snapshots()
    assert {f.flow_id for f in snaps} == {flow_a.flow_id, flow_b.flow_id}
    # Per-flow shared context may be read eagerly; per-step bodies must not be.
    assert not any(lbl.startswith("step ") for lbl in reads), reads


def test_clear_state_archive_collision_stays_reference_consistent(tmp_path):
    """A same-flow_id archive collision keeps header + cold files consistent.

    When archive/steps/<flow_id> already exists, this archive's cold files move
    to a suffixed partition; the archived header must record that partition so a
    full-fidelity reload reads THIS flow's payloads, not the colliding sibling's
    (issue #244 B5).
    """
    pm = PersistenceManager(tmp_path)
    flow = _make_flow(n_steps=3, status=FlowStatus.COMPLETED)
    # Distinctive per-step marker so we can tell this flow's cold data apart.
    for i, sid in enumerate(flow.state.step_history):
        flow.state.steps[sid].outputs = {"marker": f"v2-{i}"}
    pm.save_flow(flow)

    # Simulate a prior archive of the same flow_id already owning the cold dir.
    archive_steps = tmp_path / "se3" / "state" / "archive" / "steps"
    (archive_steps / flow.flow_id).mkdir(parents=True)
    (archive_steps / flow.flow_id / "_sentinel.txt").write_text("old archive data")

    pm.clear_state()

    headers = sorted((tmp_path / "se3" / "state" / "archive").glob("engine_*.json"))
    assert len(headers) == 1
    header = json.loads(headers[0].read_text())
    partition = header["state"]["cold_partition"]
    assert partition.startswith(flow.flow_id + "_")

    # This archive's cold files landed in the recorded suffixed partition, and
    # the pre-existing sibling archive is left untouched.
    suffixed = archive_steps / partition
    assert (suffixed / "_context.json").is_file()
    assert (archive_steps / flow.flow_id / "_sentinel.txt").read_text() == "old archive data"

    # Reference consistency: reconstructing the archived header against the
    # archive steps dir recovers THIS flow's payload via the recorded partition,
    # not the sibling's empty/sentinel dir.
    archive_pm = PersistenceManager(tmp_path)
    archive_pm.steps_dir = archive_steps
    reloaded = FlowInstance.from_dict(archive_pm._reconstruct_full_dict(header))
    markers = {s.outputs.get("marker") for s in reloaded.state.steps.values()}
    assert markers == {"v2-0", "v2-1", "v2-2"}


def test_clear_state_archive_double_collision_does_not_abort(tmp_path, monkeypatch):
    """Both steps/<flow_id> AND steps/<flow_id>_<timestamp> already present (B5).

    Re-archiving the same flow_id twice within one second collides on the primary
    partition name AND on the first timestamp-suffixed fallback. clear_state must
    probe past the occupied fallback rather than copytree onto it and abort the
    whole archive, leaving BOTH pre-existing archives intact.
    """
    pm = PersistenceManager(tmp_path)
    flow = _make_flow(n_steps=3, status=FlowStatus.COMPLETED)
    for i, sid in enumerate(flow.state.step_history):
        flow.state.steps[sid].outputs = {"marker": f"v3-{i}"}
    pm.save_flow(flow)

    # Freeze the timestamp so this archival collides on the suffixed fallback too.
    from datetime import datetime as _dt

    frozen = _dt(2026, 7, 4, 12, 0, 0)

    class _Frozen(_dt):
        @classmethod
        def now(cls, tz=None):
            return frozen

    ts = frozen.strftime("%Y%m%d_%H%M%S")
    archive_steps = tmp_path / "se3" / "state" / "archive" / "steps"
    # Prior archive owns both the primary and the first timestamp-suffixed name.
    (archive_steps / flow.flow_id).mkdir(parents=True)
    (archive_steps / flow.flow_id / "_sentinel.txt").write_text("primary")
    (archive_steps / f"{flow.flow_id}_{ts}").mkdir(parents=True)
    (archive_steps / f"{flow.flow_id}_{ts}" / "_sentinel.txt").write_text("first fallback")

    # clear_state resolves the timestamp via a local ``from datetime import
    # datetime``; patch the class on the datetime module the name binds to.
    monkeypatch.setattr("datetime.datetime", _Frozen, raising=False)

    pm.clear_state()  # must not raise

    headers = sorted((tmp_path / "se3" / "state" / "archive").glob("engine_*.json"))
    assert len(headers) == 1
    header = json.loads(headers[0].read_text())
    partition = header["state"]["cold_partition"]
    # Probed past both occupied names to a numeric-suffixed partition.
    assert partition.startswith(f"{flow.flow_id}_{ts}_")
    assert (archive_steps / partition / "_context.json").is_file()
    # Both pre-existing sibling archives are untouched.
    assert (archive_steps / flow.flow_id / "_sentinel.txt").read_text() == "primary"
    assert (archive_steps / f"{flow.flow_id}_{ts}" / "_sentinel.txt").read_text() == "first fallback"

    archive_pm = PersistenceManager(tmp_path)
    archive_pm.steps_dir = archive_steps
    reloaded = FlowInstance.from_dict(archive_pm._reconstruct_full_dict(header))
    markers = {s.outputs.get("marker") for s in reloaded.state.steps.values()}
    assert markers == {"v3-0", "v3-1", "v3-2"}


def test_list_all_flows_mixed_formats(tmp_path):
    pm = PersistenceManager(tmp_path)
    # Archive one new-format flow.
    new_flow = _make_flow(n_steps=3, status=FlowStatus.COMPLETED)
    pm.save_flow(new_flow)
    pm.clear_state()
    # Drop a legacy inline archive snapshot beside it.
    archive = tmp_path / "se3" / "state" / "archive"
    legacy = _make_flow(n_steps=2, status=FlowStatus.COMPLETED)
    (archive / "engine_20200101_000000.json").write_text(
        json.dumps(legacy.to_dict(), indent=2, ensure_ascii=False, default=str)
    )

    flows = pm.list_all_flows()
    ids = {f["flow_id"] for f in flows}
    assert new_flow.flow_id in ids
    assert legacy.flow_id in ids


def test_export_context_and_progress_on_new_format(tmp_path):
    pm = PersistenceManager(tmp_path)
    flow = _make_flow(n_steps=3)
    pm.save_flow(flow)
    loaded = pm.load_flow()

    # Context export walks step inputs/outputs — hydrated cold data must be there.
    ctx_path = pm.export_context_from_flow(loaded)
    assert ctx_path.is_file()
    exported = json.loads(ctx_path.read_text())
    assert exported  # non-empty structured context

    md = pm.export_progress_markdown(loaded)
    assert flow.flow_id in md
    assert "## Steps" in md


def test_export_context_hydrates_lazy_loaded_flow(tmp_path):
    """Regression (fix iteration 6): export must hydrate lazy-loaded cold steps.

    ``export_context_from_flow`` on a flow from a lazy loader
    (``load_flow_by_id`` — the natural resume pairing) previously serialized
    every step with empty inputs/outputs because ``_LazyStepDict`` does not
    hydrate on iteration, silently writing a hollow context.json. The export
    must fault the cold bodies in first so each step's real IO survives.
    """
    pm = PersistenceManager(tmp_path)
    flow = FlowInstance(task_description="lazy export", status=FlowStatus.RUNNING)
    flow.task_type = "feature"
    step = Step(step_type=StepType.IMPLEMENT, status=StepStatus.COMPLETED)
    step.inputs = {"test_results": "R" * 20_000}
    step.outputs = {"files_modified": ["a.py", "b.py"], "changes_made": "did work"}
    flow.state.add_step(step)
    flow.state.selected_steps = [StepType.IMPLEMENT]
    flow.state.current_step_id = flow.state.step_history[-1]
    pm.save_flow(flow)

    lazy = pm.load_flow_by_id(flow.flow_id)
    assert lazy is not None
    # Precondition — the trigger for the bug: the lazy step map yields
    # un-hydrated (empty-IO) steps on raw iteration.
    raw = [dict.get(lazy.state.steps, sid).outputs for sid in lazy.state.step_history]
    assert raw == [{}], "precondition: cold step bodies must not be hydrated yet"

    ctx = json.loads(pm.export_context_from_flow(lazy).read_text())
    # The completed step's real outputs must reach the exported context.
    assert ctx["key_outputs"].get("implement") == {
        "changes_made": "did work",
        "files_modified": ["a.py", "b.py"],
    }


# -- B5 / self-check: surviving resumable snapshot stays resumable --------------


def test_clear_state_preserves_surviving_resumable_snapshot_fidelity(tmp_path):
    """clear_state on a non-completed flow keeps its resumable snapshot faithful.

    The snapshot shares the flow's live ``steps/<flow_id>/`` cold partition. If
    clear_state simply *moved* that partition into the archive, the flow — still
    advertised as resumable from ``resumable/*.json`` — would resume with every
    step's inputs/outputs and the shared context silently empty (spec_content /
    test_results等 gone). Instead the cold files are copied into the archive and
    the live partition is left in place, so resume keeps full fidelity while the
    archive still holds a complete copy (self-check fix for the end-session /
    salvage-without-flow_id path).
    """
    pm = PersistenceManager(tmp_path)
    flow = _make_flow(n_steps=4, status=FlowStatus.PAUSED)
    pm.save_flow(flow)  # non-completed → resumable snapshot written

    state_dir = tmp_path / "se3" / "state"
    live_cold = state_dir / "steps" / flow.flow_id
    snapshot = state_dir / "resumable" / f"{flow.flow_id}.json"
    assert live_cold.is_dir() and snapshot.is_file()

    pm.clear_state()

    # The live cold partition and the snapshot both survive the archival.
    assert live_cold.is_dir(), "cold partition must remain for the live snapshot"
    assert snapshot.is_file()

    # The still-advertised snapshot resumes at full fidelity: no empty payloads.
    resumed = PersistenceManager(tmp_path).load_resumable_snapshot(flow.flow_id)
    assert resumed == flow

    # And the archive received its own full-fidelity copy of the cold data.
    archive_cold = state_dir / "archive" / "steps" / flow.flow_id
    assert archive_cold.is_dir()
    assert (archive_cold / "_context.json").is_file()
    assert len(list(archive_cold.glob("*.json"))) == 4 + 1  # steps + context


# -- B3 / self-check: tolerant load of a truncated hot/cold header --------------


def test_load_flow_tolerant_recovers_truncated_hotcold_header(tmp_path):
    """A machine-crash truncated new-format header is salvaged with cold data.

    Exercises the load_flow_tolerant reconstruction path (self-check fix): the
    header is cut mid-file so created_at/updated_at are lost, but the leading
    ``engine_format`` marker survives — so after JSON repair the header is still
    recognised as hot/cold and its intact cold step files + shared context are
    resolved, rather than the whole flow degrading to empty inline payloads or
    the salvage returning ``(None, warnings)``.
    """
    pm = PersistenceManager(tmp_path)
    flow = _make_flow(n_steps=3, status=FlowStatus.RUNNING)
    pm.save_flow(flow)
    state_file = tmp_path / "se3" / "state" / "engine.json"
    full = state_file.read_text()

    # The format marker must lead the header, so a head-truncated file still
    # identifies as hot/cold (the whole point of the reordering fix).
    assert full.lstrip().startswith('{\n  "engine_format"')

    # Cut just before created_at: state is fully written (all cold_ref'd steps
    # survive) but created_at/updated_at onward are gone, leaving invalid JSON.
    cut = full.index('"created_at"')
    assert cut > full.index('"engine_format"')  # marker precedes the cut
    state_file.write_text(full[:cut])

    recovered, warnings = pm.load_flow_tolerant()

    assert recovered is not None, "truncated header must be salvaged, not dropped"
    assert warnings, "recovery must be reported via warnings"
    assert any("created_at" in w for w in warnings)
    # Intact cold files still resolve: step IO and shared context are NOT empty.
    for i, sid in enumerate(flow.state.step_history):
        step = recovered.state.steps.get(sid)
        assert step.inputs.get("idx") == i
        assert step.inputs.get("test_results")  # large cold payload present
        assert step.outputs.get("artifact_blob")
    assert recovered.state.context.get("spec_content")
