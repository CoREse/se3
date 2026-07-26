"""Write-path tests for the engine.json hot/cold split (issue #244 一期, group G5).

These cover the *write* side of the hot/cold split — the header stays KB-scale,
each persist rewrites only the cold files that actually changed, and the
resumable snapshot is a shared-cold-reference header that does not bloat with the
flow's inputs/outputs. Round-trip / legacy-read coverage lives alongside in the
engine's own persistence suite; a couple of round-trip guards are repeated here
so the write format is never shipped without a matching reader.

Acceptance mapping (task description):
  (f) multi-step flow with large inputs/outputs => engine.json header < 100 KB
  (g) a single-step persist touches only the header and that step's cold file
      (asserted via the set of files the write path actually wrote)
  resumable snapshot uses the split format and does not grow with payload size.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List

import pytest

from tianluo.engine.models import (
    FlowInstance,
    FlowStatus,
    State,
    Step,
    StepStatus,
    StepType,
)
from tianluo.engine.persistence import (
    ENGINE_FORMAT_KEY,
    PersistenceManager,
    _canonical_json,
    _content_hash,
    _is_hotcold,
)


# --------------------------------------------------------------------------
# Fixtures / builders
# --------------------------------------------------------------------------

@pytest.fixture
def pm(tmp_path: Path) -> PersistenceManager:
    return PersistenceManager(tmp_path)


def _big_step(step_type: StepType, status: StepStatus, payload_kb: int) -> Step:
    """A step whose inputs+outputs carry ~payload_kb KiB of data."""
    blob = "x" * (payload_kb * 1024)
    step = Step(step_type=step_type, status=status)
    step.inputs = {"blob": blob, "note": "input"}
    step.outputs = {"blob": blob, "note": "output"}
    return step


def _flow_with_steps(flow_id: str, n_steps: int, payload_kb: int) -> FlowInstance:
    """Build a flow of ``n_steps`` heavy steps plus a heavy shared context."""
    state = State()
    for i in range(n_steps):
        step = _big_step(StepType.IMPLEMENT, StepStatus.COMPLETED, payload_kb)
        state.add_step(step)
        state.selected_steps.append(StepType.IMPLEMENT)
    # A large shared context too (the other unbounded grower that is externalized).
    state.context = {"resolved_type": "feature", "notes": "c" * (payload_kb * 1024)}
    state.current_step_id = state.step_history[-1] if state.step_history else None
    return FlowInstance(
        flow_id=flow_id,
        status=FlowStatus.RUNNING,
        task_description="hot/cold split test",
        state=state,
    )


class _WriteSpy:
    """Records the basenames the persistence write path atomically wrote.

    Patches the single ``_atomic_write_json`` seam (used for the header and every
    cold file), so the recorded set is exactly what a persist touched — the
    write-amplification assertion the incremental write path must satisfy.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.names: List[str] = []
        original = PersistenceManager._atomic_write_json

        def spy(path: Path, data: object) -> None:
            self.names.append(Path(path).name)
            return original(path, data)

        monkeypatch.setattr(
            PersistenceManager, "_atomic_write_json", staticmethod(spy)
        )

    def reset(self) -> None:
        self.names.clear()

    @property
    def written(self) -> set:
        return set(self.names)


# --------------------------------------------------------------------------
# (f) header is bounded regardless of payload / step count
# --------------------------------------------------------------------------

def test_header_is_hotcold_format(pm: PersistenceManager) -> None:
    flow = _flow_with_steps("flowF", n_steps=3, payload_kb=50)
    pm.save_flow(flow)

    data = json.loads(pm.state_file.read_text(encoding="utf-8"))
    assert data.get(ENGINE_FORMAT_KEY) == "hotcold/1"
    assert _is_hotcold(data)
    # Step status table present, payload bodies absent.
    for entry in data["state"]["steps"].values():
        assert "cold_ref" in entry
        assert "inputs" not in entry and "outputs" not in entry
    # Shared context externalized, not inlined.
    assert "context_ref" in data["state"]
    assert "context" not in data["state"]


def test_header_under_100kb_with_large_payloads(pm: PersistenceManager) -> None:
    # 8 steps × ~100 KiB inputs+outputs each + ~50 KiB context ≈ >800 KiB of cold
    # data; the header must stay comfortably under the 100 KB budget.
    flow = _flow_with_steps("flowBig", n_steps=8, payload_kb=50)
    pm.save_flow(flow)

    header_size = pm.state_file.stat().st_size
    assert header_size < 100 * 1024, f"header too large: {header_size} bytes"

    # Sanity: the cold data really is large (guards against the payload silently
    # not being written, which would make the size assertion meaningless).
    cold_dir = pm.steps_dir / "flowBig"
    cold_bytes = sum(p.stat().st_size for p in cold_dir.iterdir())
    assert cold_bytes > 500 * 1024


def test_header_size_independent_of_step_count(pm: PersistenceManager) -> None:
    """Header grows only with the small per-step status table, not payloads."""
    small = _flow_with_steps("flowSmallN", n_steps=2, payload_kb=100)
    large = _flow_with_steps("flowLargeN", n_steps=6, payload_kb=100)

    pm_a = PersistenceManager(pm.project_root / "a")
    pm_b = PersistenceManager(pm.project_root / "b")
    pm_a.save_flow(small)
    pm_b.save_flow(large)

    # Both headers are far below the budget and differ only by the per-step
    # status rows (KB scale), never by the 100 KiB payloads.
    assert pm_a.state_file.stat().st_size < 100 * 1024
    assert pm_b.state_file.stat().st_size < 100 * 1024


# --------------------------------------------------------------------------
# (g) a persist touches only the header + changed step's cold file
# --------------------------------------------------------------------------

def test_first_save_writes_all_cold_files(
    pm: PersistenceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = _flow_with_steps("flowG0", n_steps=3, payload_kb=1)
    spy = _WriteSpy(monkeypatch)
    pm.save_flow(flow)

    step_ids = list(flow.state.steps.keys())
    written = spy.written
    assert "engine.json" in written
    for sid in step_ids:
        assert f"{sid}.json" in written
    assert pm.CONTEXT_COLD_FILENAME in written


def test_single_step_change_touches_only_that_cold_file(
    pm: PersistenceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = _flow_with_steps("flowG", n_steps=3, payload_kb=1)
    pm.save_flow(flow)  # establishes the baseline header/cold set

    spy = _WriteSpy(monkeypatch)
    step_ids = list(flow.state.steps.keys())
    changed = step_ids[1]
    flow.state.steps[changed].outputs = {"changed": "yes"}
    pm.save_flow(flow)

    written = spy.written
    # Only the changed step's cold file is (re)written...
    assert f"{changed}.json" in written
    # ...the two unchanged steps' cold files and the unchanged context are not.
    for sid in step_ids:
        if sid != changed:
            assert f"{sid}.json" not in written
    assert pm.CONTEXT_COLD_FILENAME not in written
    # The header and the resumable snapshot header are always rewritten.
    assert "engine.json" in written
    assert f"{flow.flow_id}.json" in written


def test_no_change_rewrites_only_headers(
    pm: PersistenceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = _flow_with_steps("flowG2", n_steps=3, payload_kb=1)
    pm.save_flow(flow)

    spy = _WriteSpy(monkeypatch)
    pm.save_flow(flow)  # nothing in the payloads changed

    written = spy.written
    for sid in flow.state.steps:
        assert f"{sid}.json" not in written
    assert pm.CONTEXT_COLD_FILENAME not in written
    assert "engine.json" in written


def test_context_change_touches_only_context_cold_file(
    pm: PersistenceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    flow = _flow_with_steps("flowG3", n_steps=2, payload_kb=1)
    pm.save_flow(flow)

    spy = _WriteSpy(monkeypatch)
    flow.state.context["resolved_type"] = "bugfix"
    pm.save_flow(flow)

    written = spy.written
    assert pm.CONTEXT_COLD_FILENAME in written
    for sid in flow.state.steps:
        assert f"{sid}.json" not in written


# --------------------------------------------------------------------------
# resumable snapshot: split format, shared cold, no bloat
# --------------------------------------------------------------------------

def test_resumable_snapshot_is_split_and_small(pm: PersistenceManager) -> None:
    flow = _flow_with_steps("flowR", n_steps=6, payload_kb=50)  # ~600+ KiB cold
    pm.save_flow(flow)

    snapshot = pm.resumable_dir / "flowR.json"
    assert snapshot.exists()
    data = json.loads(snapshot.read_text(encoding="utf-8"))
    assert _is_hotcold(data)
    # A KB-scale header, not a copy of the megabytes of payload.
    assert snapshot.stat().st_size < 100 * 1024


def test_resumable_snapshot_shares_cold_partition(
    pm: PersistenceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The snapshot reuses steps/<flow_id>/ — it must not duplicate cold files."""
    flow = _flow_with_steps("flowR2", n_steps=2, payload_kb=1)

    spy = _WriteSpy(monkeypatch)
    pm.save_flow(flow)

    # Snapshot bookkeeping runs inside save_flow. The cold files are written once
    # (by the engine.json path); the snapshot adds only its own header file.
    written = spy.names  # list, so we can count duplicates
    for sid in flow.state.steps:
        assert written.count(f"{sid}.json") == 1, "cold file duplicated for snapshot"
    assert written.count(pm.CONTEXT_COLD_FILENAME) == 1
    # There is exactly one cold partition, shared by engine.json and the snapshot.
    assert (pm.steps_dir / "flowR2").is_dir()


def test_resumable_snapshot_does_not_grow_with_payload(
    pm: PersistenceManager,
) -> None:
    """In-flight snapshot size tracks the header, not the accumulating payload."""
    small = _flow_with_steps("flowRA", n_steps=3, payload_kb=1)
    huge = _flow_with_steps("flowRB", n_steps=3, payload_kb=200)
    pm.save_flow(small)
    pm.save_flow(huge)

    size_small = (pm.resumable_dir / "flowRA.json").stat().st_size
    size_huge = (pm.resumable_dir / "flowRB.json").stat().st_size
    # A 200×-larger payload must not bloat the snapshot header meaningfully.
    assert size_huge < size_small + 4 * 1024


# --------------------------------------------------------------------------
# write/read round-trip guard (write format must always have a reader)
# --------------------------------------------------------------------------

def test_roundtrip_preserves_payloads_and_context(pm: PersistenceManager) -> None:
    flow = _flow_with_steps("flowRT", n_steps=3, payload_kb=2)
    flow.state.steps[flow.state.step_history[0]].artifacts = [Path("src/x.py")]
    pm.save_flow(flow)

    loaded = pm.load_flow()
    assert loaded is not None
    assert loaded.status == FlowStatus.RUNNING
    assert loaded.state.context == flow.state.context
    for sid, step in flow.state.steps.items():
        assert loaded.state.steps[sid].inputs == step.inputs
        assert loaded.state.steps[sid].outputs == step.outputs
    assert loaded.state.steps[flow.state.step_history[0]].artifacts == [Path("src/x.py")]


# --------------------------------------------------------------------------
# cold-payload content-hash stability (guards the incremental write path)
# --------------------------------------------------------------------------

def test_content_hash_is_key_order_independent() -> None:
    """The cold hash must ignore dict key ordering.

    If ``sort_keys`` (or ``default=str``) is ever dropped from
    ``_canonical_json``, the hash becomes ordering-sensitive and every save
    would spuriously re-flag unchanged steps dirty, defeating the incremental
    write path (issue #244 B2). This asserts the invariant directly rather than
    via an indirect write-set assertion.
    """
    a = {"inputs": {"z": 1, "a": 2, "m": {"y": 3, "x": 4}}, "outputs": {"b": 5}}
    # Same content, keys inserted in a different order at every level.
    b = {"outputs": {"b": 5}, "inputs": {"m": {"x": 4, "y": 3}, "a": 2, "z": 1}}
    assert a == b  # equal payloads...
    assert _canonical_json(a) == _canonical_json(b)  # ...canonicalize identically...
    assert _content_hash(a) == _content_hash(b)  # ...and hash identically.


def test_content_hash_is_stable_across_repeated_serialization() -> None:
    """Hashing the same payload twice yields the same digest (no hidden state)."""
    payload = {"inputs": {"blob": "x" * 4096}, "outputs": {"note": "done"}}
    assert _content_hash(payload) == _content_hash(payload)


def test_content_hash_coerces_non_json_native_like_the_writer() -> None:
    """``default=str`` mirrors the writer so Path values hash stably."""
    with_path = {"artifacts": [Path("src/x.py")]}
    with_str = {"artifacts": ["src/x.py"]}
    assert _content_hash(with_path) == _content_hash(with_str)


# --------------------------------------------------------------------------
# (h-adjacent) externally deleted cold file is repopulated on next save
# --------------------------------------------------------------------------

def test_deleted_cold_file_is_rewritten_on_next_save(
    pm: PersistenceManager, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hash-matching step whose cold file vanished must be re-written.

    Without the on-disk existence check the dirty detector would keep skipping
    the step forever (hash still matches the header), and after the process
    exits that step's inputs/outputs would silently load as empty even though
    the payload was in memory at every save (issue #244 B2/B3).
    """
    flow = _flow_with_steps("flowDel", n_steps=3, payload_kb=1)
    pm.save_flow(flow)

    step_ids = list(flow.state.steps.keys())
    victim = step_ids[1]
    cold_file = pm.steps_dir / "flowDel" / f"{victim}.json"
    ctx_file = pm.steps_dir / "flowDel" / pm.CONTEXT_COLD_FILENAME
    assert cold_file.exists() and ctx_file.exists()
    cold_file.unlink()
    ctx_file.unlink()

    # Nothing in the in-memory payload changed, yet the missing files must be
    # regenerated rather than skipped on the matching hash.
    spy = _WriteSpy(monkeypatch)
    pm.save_flow(flow)

    written = spy.written
    assert f"{victim}.json" in written
    assert pm.CONTEXT_COLD_FILENAME in written
    # The other two unchanged, still-present cold files stay untouched.
    for sid in step_ids:
        if sid != victim:
            assert f"{sid}.json" not in written
    assert cold_file.exists() and ctx_file.exists()

    # And a fresh load recovers the payload rather than degrading to empty.
    loaded = pm.load_flow()
    assert loaded is not None
    assert loaded.state.steps[victim].inputs == flow.state.steps[victim].inputs
    assert loaded.state.steps[victim].outputs == flow.state.steps[victim].outputs


def test_resumable_roundtrip(pm: PersistenceManager) -> None:
    flow = _flow_with_steps("flowRTR", n_steps=2, payload_kb=2)
    pm.save_flow(flow)

    restored = pm.load_resumable_snapshot("flowRTR")
    assert restored is not None
    for sid, step in flow.state.steps.items():
        assert restored.state.steps[sid].outputs == step.outputs
