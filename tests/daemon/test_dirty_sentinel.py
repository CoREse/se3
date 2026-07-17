"""Tests for the dirty sentinel (G6): persist-side bump + daemon-side gate.

The idle hotspot being closed: the daemon's 1s fast tick deep-scanned every
tracked root (engine.json peek/read + jsonl enumeration) even when nothing had
persisted for hours. ``PersistenceManager`` now bumps ``se3/state/.dirty``
({"seq": N}, atomic rename) after every successful state persist, and
``DaemonHistoryReader.active_flow_signature`` gates a root whose previous deep
scan found NO active flow on that one file's ``(mtime_ns, size)`` — an idle
root costs exactly one stat per tick.

Contract under test:

* engine side — every persist path (save_flow / resumable snapshot save +
  clear / clear_state archive) advances the seq; a sentinel write failure
  never breaks the persistence primary path (the sentinel is an optimization
  signal, not a correctness dependency);
* daemon side — gated roots pay one sentinel stat and zero deep scans; a
  sentinel bump restores the deep scan on the next tick; a missing sentinel
  fails open to the pre-G6 per-tick deep scan; a root with an active flow is
  NEVER gated (history jsonl is appended by HistorySink directly, bypassing
  PersistenceManager, so streamed records move no sentinel); the status-tick
  backstop (``clear_sentinel_gate``) bounds the staleness of out-of-band
  writes that bypass the sentinel.

The async client cases drive their own event loop via ``asyncio.run``:
pytest-asyncio is not a test dependency of this project.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

import se3.daemon.disk_json_cache as disk_cache
import se3.daemon.history as history_module
from se3.daemon.client import DaemonClient
from se3.daemon.history import DaemonHistoryReader
from se3.engine.models import (
    FlowInstance,
    FlowStatus,
    State,
    Step,
    StepStatus,
    StepType,
)
from se3.engine.persistence import PersistenceManager


@pytest.fixture(autouse=True)
def _fresh_disk_cache():
    """Isolate the module-level disk JSON cache per test."""
    disk_cache.clear_cache()
    yield
    disk_cache.clear_cache()


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _make_flow(flow_id: str, status: FlowStatus) -> FlowInstance:
    """Build a minimal but realistic FlowInstance with one current step."""
    step = Step(step_type=StepType.DISCOVERY, status=StepStatus.RUNNING)
    state = State()
    state.add_step(step)
    state.current_step_id = step.step_id
    state.selected_steps = [StepType.DISCOVERY]
    return FlowInstance(
        flow_id=flow_id,
        status=status,
        task_description="dirty sentinel test",
        state=state,
    )


def _sentinel_path(root: Path) -> Path:
    return root / "se3" / "state" / ".dirty"


def _read_seq(root: Path) -> int:
    data = json.loads(_sentinel_path(root).read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return int(data["seq"])


def _write_engine_raw(root: Path, payload: Dict[str, Any]) -> Path:
    """Write engine.json WITHOUT PersistenceManager (no sentinel bump)."""
    state_dir = root / "se3" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    engine = state_dir / "engine.json"
    engine.write_text(json.dumps(payload), encoding="utf-8")
    return engine


def _bump_sentinel_raw(root: Path, seq: int) -> None:
    """Advance the sentinel the way PersistenceManager does (atomic rename)."""
    sentinel = _sentinel_path(root)
    tmp = sentinel.with_name(sentinel.name + ".tmp")
    tmp.write_text(json.dumps({"seq": seq}), encoding="utf-8")
    tmp.replace(sentinel)


def _make_reader(root: Path) -> DaemonHistoryReader:
    return DaemonHistoryReader(project_roots_provider=lambda: [root])


@pytest.fixture()
def deep_scans(monkeypatch):
    """Count every per-root deep scan (``_scan_root_signature``)."""
    calls: List[str] = []
    real = DaemonHistoryReader._scan_root_signature

    def counting(self, root, signature):
        calls.append(str(root))
        return real(self, root, signature)

    monkeypatch.setattr(DaemonHistoryReader, "_scan_root_signature", counting)
    return calls


@pytest.fixture()
def sentinel_stats(monkeypatch):
    """Count every sentinel stat the signature pass performs."""
    calls: List[str] = []
    real = history_module._sentinel_stat

    def counting(path):
        calls.append(str(path))
        return real(path)

    monkeypatch.setattr(history_module, "_sentinel_stat", counting)
    return calls


# --------------------------------------------------------------------------
# task 11 — PersistenceManager bumps the sentinel on every persist path
# --------------------------------------------------------------------------


def test_save_flow_bumps_seq_strictly(tmp_path):
    pm = PersistenceManager(tmp_path)
    pm.save_flow(_make_flow("f1", FlowStatus.RUNNING))
    first = _read_seq(tmp_path)
    assert first >= 1

    pm.save_flow(_make_flow("f1", FlowStatus.RUNNING))
    assert _read_seq(tmp_path) > first


def test_save_resumable_snapshot_standalone_bumps_seq(tmp_path):
    pm = PersistenceManager(tmp_path)
    flow = _make_flow("f-snap", FlowStatus.PAUSED)
    pm.save_flow(flow)
    before = _read_seq(tmp_path)

    pm.save_resumable_snapshot(flow)
    assert _read_seq(tmp_path) > before


def test_clear_resumable_snapshot_bumps_seq(tmp_path):
    pm = PersistenceManager(tmp_path)
    flow = _make_flow("f-clear", FlowStatus.PAUSED)
    pm.save_flow(flow)
    assert (pm.resumable_dir / "f-clear.json").exists()
    before = _read_seq(tmp_path)

    pm.clear_resumable_snapshot("f-clear")
    assert not (pm.resumable_dir / "f-clear.json").exists()
    assert _read_seq(tmp_path) > before


def test_clear_state_archive_bumps_seq(tmp_path):
    pm = PersistenceManager(tmp_path)
    pm.save_flow(_make_flow("f-arch", FlowStatus.COMPLETED))
    before = _read_seq(tmp_path)

    pm.clear_state()
    assert not pm.state_file.exists()
    assert _read_seq(tmp_path) > before


def test_clear_state_without_state_file_is_a_noop(tmp_path):
    pm = PersistenceManager(tmp_path)
    pm.clear_state()  # nothing on disk changed, so nothing to signal
    assert not _sentinel_path(tmp_path).exists()


def test_corrupt_sentinel_restarts_from_scratch(tmp_path):
    pm = PersistenceManager(tmp_path)
    pm.ensure_directories()
    _sentinel_path(tmp_path).write_text("{ not json", encoding="utf-8")

    pm.save_flow(_make_flow("f-corrupt", FlowStatus.RUNNING))
    # The corrupt payload is discarded (seq restarts near zero) rather than
    # poisoning the persist path.
    assert _read_seq(tmp_path) >= 1


def test_sentinel_write_failure_never_breaks_persistence(tmp_path, monkeypatch):
    """An injected OSError on the sentinel write leaves the persist intact."""
    pm = PersistenceManager(tmp_path)
    real = PersistenceManager._atomic_write_json

    def selective(path, data):
        if Path(path).name == PersistenceManager.DIRTY_SENTINEL_FILENAME:
            raise OSError("read-only sentinel dir")
        real(path, data)

    monkeypatch.setattr(pm, "_atomic_write_json", selective)

    flow = _make_flow("f-ro", FlowStatus.RUNNING)
    pm.save_flow(flow)  # must not raise
    assert pm.state_file.exists()
    assert not _sentinel_path(tmp_path).exists()

    pm.clear_resumable_snapshot("f-ro")  # must not raise either
    pm.save_flow(_make_flow("f-ro", FlowStatus.COMPLETED))
    pm.clear_state()  # archive path must survive the sentinel failure too
    assert not pm.state_file.exists()


def test_sentinel_read_failure_still_bumps(tmp_path, monkeypatch):
    """An unreadable existing sentinel degrades to a fresh seq, not a crash."""
    pm = PersistenceManager(tmp_path)
    pm.ensure_directories()
    sentinel = _sentinel_path(tmp_path)
    sentinel.write_text(json.dumps({"seq": 41}), encoding="utf-8")

    real_read = Path.read_text

    def failing(self, *args, **kwargs):
        if self.name == PersistenceManager.DIRTY_SENTINEL_FILENAME:
            raise OSError("EACCES")
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", failing)
    pm.save_flow(_make_flow("f-eacces", FlowStatus.RUNNING))
    monkeypatch.undo()
    # Restarted from 0 (the unreadable prior value is treated as absent).
    assert _read_seq(tmp_path) >= 1


# --------------------------------------------------------------------------
# task 12 — daemon-side sentinel gate on the fast-tick signature scan
# --------------------------------------------------------------------------


def test_idle_root_gated_to_one_sentinel_stat_per_tick(
    tmp_path, deep_scans, sentinel_stats
):
    """No active flow + unmoved sentinel ⇒ zero deep scans, one stat per tick."""
    _write_engine_raw(tmp_path, {"flow_id": "done", "status": "COMPLETED"})
    _bump_sentinel_raw(tmp_path, 1)
    reader = _make_reader(tmp_path)

    # First tick: full deep scan (arming the gate takes one real look).
    assert reader.active_flow_signature() == {}
    assert len(deep_scans) == 1

    scans_before = len(deep_scans)
    stats_before = len(sentinel_stats)
    for _ in range(5):
        assert reader.active_flow_signature() == {}
    assert len(deep_scans) == scans_before  # zero deep scans while gated
    assert len(sentinel_stats) == stats_before + 5  # exactly 1 stat per tick


def test_sentinel_bump_restores_deep_scan_and_surfaces_the_change(
    tmp_path, deep_scans
):
    """The tick after a seq bump rescans and produces the new flow's token."""
    _write_engine_raw(tmp_path, {"flow_id": "done", "status": "COMPLETED"})
    _bump_sentinel_raw(tmp_path, 1)
    reader = _make_reader(tmp_path)
    assert reader.active_flow_signature() == {}
    gated = len(deep_scans)
    assert reader.active_flow_signature() == {}
    assert len(deep_scans) == gated  # gate armed

    _write_engine_raw(tmp_path, {"flow_id": "fresh", "status": "RUNNING"})
    _bump_sentinel_raw(tmp_path, 2)

    sig = reader.active_flow_signature()
    assert set(sig) == {"fresh"}
    assert len(deep_scans) == gated + 1


def test_missing_sentinel_fails_open_to_per_tick_deep_scan(
    tmp_path, deep_scans
):
    """A sentinel-less root behaves exactly as before the gate existed."""
    _write_engine_raw(tmp_path, {"flow_id": "done", "status": "COMPLETED"})
    reader = _make_reader(tmp_path)

    for _ in range(4):
        assert reader.active_flow_signature() == {}
    assert len(deep_scans) == 4  # every tick deep-scans (pre-G6 behavior)

    # And a change is picked up immediately, no sentinel involved.
    _write_engine_raw(
        tmp_path, {"flow_id": "fresh", "status": "RUNNING", "pad": "x" * 8}
    )
    assert set(reader.active_flow_signature()) == {"fresh"}


def test_active_flow_root_is_never_gated(tmp_path, deep_scans):
    """jsonl streaming bypasses the sentinel, so a live root always deep-scans.

    WHY this matters: history jsonl is appended by chat_history/HistorySink
    directly — never through PersistenceManager — so gating a live root on
    the sentinel would freeze web streaming until the status backstop.
    """
    _write_engine_raw(tmp_path, {"flow_id": "live", "status": "RUNNING"})
    _bump_sentinel_raw(tmp_path, 1)
    hist_dir = tmp_path / "se3" / "history" / "live"
    hist_dir.mkdir(parents=True)
    jsonl = hist_dir / "01_discovery.jsonl"
    jsonl.write_text("{}\n", encoding="utf-8")

    reader = _make_reader(tmp_path)
    before = reader.active_flow_signature()
    assert set(before) == {"live"}
    scans = len(deep_scans)

    # The sentinel never moves, yet a bare jsonl append must land within ONE
    # fast tick — the realtime-streaming acceptance bar.
    with jsonl.open("a", encoding="utf-8") as fh:
        fh.write("{}\n")
    after = reader.active_flow_signature()
    assert len(deep_scans) == scans + 1  # deep scan ran despite the sentinel
    assert after["live"] != before["live"]


def test_flow_going_terminal_arms_the_gate_only_after_a_scan(
    tmp_path, deep_scans
):
    """live→terminal (persisted, so sentinel moves) settles into the gate."""
    pm = PersistenceManager(tmp_path)
    pm.save_flow(_make_flow("f-settle", FlowStatus.RUNNING))
    reader = _make_reader(tmp_path)
    assert set(reader.active_flow_signature()) == {"f-settle"}

    pm.save_flow(_make_flow("f-settle", FlowStatus.COMPLETED))
    assert reader.active_flow_signature() == {}  # rescan sees the flip
    settled = len(deep_scans)
    for _ in range(3):
        assert reader.active_flow_signature() == {}
    assert len(deep_scans) == settled  # now gated: zero further deep scans


def test_out_of_band_write_is_bounded_by_clear_sentinel_gate(
    tmp_path, deep_scans
):
    """A write that bypasses the sentinel is hidden while gated and surfaces
    on the status-tick backstop (``clear_sentinel_gate``)."""
    _write_engine_raw(tmp_path, {"flow_id": "done", "status": "COMPLETED"})
    _bump_sentinel_raw(tmp_path, 1)
    reader = _make_reader(tmp_path)
    assert reader.active_flow_signature() == {}

    # Out-of-band rewrite: a sentinel-unaware writer replaced engine.json.
    _write_engine_raw(tmp_path, {"flow_id": "oob", "status": "RUNNING"})
    scans = len(deep_scans)
    assert reader.active_flow_signature() == {}  # gate hides it (fast tick)
    assert len(deep_scans) == scans

    reader.clear_sentinel_gate()  # what the client does on every status tick
    assert set(reader.active_flow_signature()) == {"oob"}


def test_sentinel_deletion_fails_open(tmp_path, deep_scans):
    """Removing the sentinel un-gates the root (missing ⇒ no signal ⇒ scan)."""
    _write_engine_raw(tmp_path, {"flow_id": "done", "status": "COMPLETED"})
    _bump_sentinel_raw(tmp_path, 1)
    reader = _make_reader(tmp_path)
    assert reader.active_flow_signature() == {}
    scans = len(deep_scans)
    assert reader.active_flow_signature() == {}
    assert len(deep_scans) == scans  # gated

    _sentinel_path(tmp_path).unlink()
    reader.active_flow_signature()
    assert len(deep_scans) == scans + 1  # deep scan resumed


def test_persistence_and_gate_mesh_end_to_end(tmp_path, deep_scans):
    """Engine-side bumps break the daemon-side gate with no other channel."""
    pm = PersistenceManager(tmp_path)
    pm.save_flow(_make_flow("f-old", FlowStatus.COMPLETED))
    reader = _make_reader(tmp_path)

    assert reader.active_flow_signature() == {}
    scans = len(deep_scans)
    assert reader.active_flow_signature() == {}
    assert len(deep_scans) == scans  # gated on the pm-written sentinel

    pm.save_flow(_make_flow("f-new", FlowStatus.RUNNING))  # bumps the sentinel
    assert set(reader.active_flow_signature()) == {"f-new"}


# --------------------------------------------------------------------------
# task 12 — client: the status tick clears the gate (backstop), fast ticks
# don't; a provider without the hook stays valid
# --------------------------------------------------------------------------


class _GateAwareHistory:
    """Minimal history provider recording gate-clears vs signature scans."""

    def __init__(self) -> None:
        self.gate_clears = 0
        self.signature_calls = 0

    def build_index(self) -> list:
        return []

    def read_active_flows(self, cursors: Dict[str, Any]) -> list:
        return []

    def active_flow_signature(self) -> Dict[str, Any]:
        self.signature_calls += 1
        return {}

    def invalidate_index_cache(self) -> None:
        pass

    def live_flow_ids(self) -> set:
        return set()

    def clear_sentinel_gate(self) -> None:
        self.gate_clears += 1


class _MinimalHistory:
    """A pre-G6 provider surface: no ``clear_sentinel_gate`` at all."""

    def build_index(self) -> list:
        return []

    def read_active_flows(self, cursors: Dict[str, Any]) -> list:
        return []

    def active_flow_signature(self) -> Dict[str, Any]:
        return {}

    def invalidate_index_cache(self) -> None:
        pass

    def live_flow_ids(self) -> set:
        return set()


class _FakeWS:
    def __init__(self) -> None:
        self.sent: List[Any] = []

    async def send(self, data: str) -> None:
        self.sent.append(data)


def _client(history_provider: Any, *, fast: float, status: float) -> DaemonClient:
    return DaemonClient(
        "ws://server",
        machine_id="m1",
        hostname="host",
        se3_version="11.0.0",
        snapshot_provider=lambda: {
            "machine_id": "m1",
            "flows": [],
            "issues": [],
            "pending_calls": [],
            "project_roots": [],
        },
        history_provider=history_provider,
        status_interval=status,
        history_poll_interval=fast,
    )


def _run_push_loop(client: DaemonClient, duration: float) -> None:
    async def scenario():
        stop = asyncio.Event()
        client._fast_push_event = asyncio.Event()
        ws = _FakeWS()
        task = asyncio.create_task(client._push_loop(ws, stop))
        await asyncio.sleep(duration)
        stop.set()
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())


def test_status_tick_clears_gate_fast_ticks_do_not():
    history = _GateAwareHistory()
    client = _client(history, fast=0.05, status=0.5)

    _run_push_loop(client, duration=0.7)

    # At least one status tick fired and cleared the gate...
    assert history.gate_clears >= 1
    # ...while the (many more) fast ticks scanned without clearing — the gate
    # must keep its savings between status heartbeats.
    assert history.signature_calls > history.gate_clears + 2


def test_provider_without_gate_hook_is_tolerated():
    """A stub/legacy provider lacking ``clear_sentinel_gate`` must not crash
    the push loop's status tick (getattr-probed, like invalidate_index_cache)."""
    # status_interval clamps to its 0.5 s floor; run past it so the status
    # tick (the only path that probes for the hook) actually fires.
    client = _client(_MinimalHistory(), fast=0.05, status=0.5)
    _run_push_loop(client, duration=0.7)  # would raise inside the loop task


# --------------------------------------------------------------------------
# task 13 (a) — idle-profile integration: all three layers stacked
#
# The environment mirrors the issue report's idle daemon: a settled root with
# a terminal engine.json, a ~307-file issue directory and a >MAX_PARSE_BYTES
# archive snapshot, plus a second history-only root. After one cold warm-up
# pass, idle ticks must cost stat-level work only: zero full-content reads,
# zero JSON/YAML parses, zero cold index rebuilds — and a gated fast tick
# exactly one sentinel stat per root.
# --------------------------------------------------------------------------

from collections import Counter

from se3.daemon.aggregator import DaemonAggregator


# The issue-farm scale from the diagnosis (~307 YAML files, 0.3–0.6 s of
# pure-Python parsing per uncached snapshot) — the cache must reduce every
# subsequent snapshot to a directory-stat pass.
_ISSUE_FARM_SIZE = 307
_IDLE_TICKS = 5


def _build_idle_estate(tmp_path: Path) -> tuple:
    """Build the two-root on-disk estate the idle-profile tests scan."""
    root_a = tmp_path / "proj-settled"
    root_b = tmp_path / "proj-history-only"

    # Terminal engine.json written through PersistenceManager — which also
    # plants the sentinel, so the root is gate-eligible from the start.
    pm = PersistenceManager(root_a)
    pm.save_flow(_make_flow("flow-term", FlowStatus.COMPLETED))

    issues = root_a / "se3" / "issues" / "open"
    issues.mkdir(parents=True)
    for i in range(_ISSUE_FARM_SIZE):
        (issues / f"{i:03d}_idle.yaml").write_text(
            f"id: '{i:03d}'\n"
            f"title: idle profile issue {i}\n"
            f"description: body {i}\n"
            "status: open\n",
            encoding="utf-8",
        )

    # Oversized archive snapshot (> MAX_PARSE_BYTES): only its bounded
    # degraded head+tail header may ever be read — and only once, thanks to
    # the stat-keyed _DEGRADED_CACHE (this was the residual ~1.1 MB/s read).
    archive = root_a / "se3" / "state" / "archive"
    archive.mkdir(parents=True, exist_ok=True)
    big = {
        "flow_id": "flow-big-archive",
        "status": "completed",
        "task_description": "giant legacy archive snapshot",
        "project_root": str(root_a),
        "state": {"pad": "x" * (disk_cache.MAX_PARSE_BYTES + 1024)},
    }
    (archive / "engine_20260101_000000.json").write_text(
        json.dumps(big, indent=2), encoding="utf-8"
    )

    # History-only root: no engine.json at all, one flow dir, plus a sentinel
    # so the fast tick can gate it too.
    hist = root_b / "se3" / "history" / "flow-hist"
    hist.mkdir(parents=True)
    (hist / "01_discovery.jsonl").write_text(
        '{"type": "prompt", "content": "hello"}\n', encoding="utf-8"
    )
    (root_b / "se3" / "state").mkdir(parents=True)
    _bump_sentinel_raw(root_b, 1)

    return root_a, root_b


@pytest.fixture()
def io_counters(monkeypatch):
    """Count every expensive-IO seam the idle profile must keep at zero."""
    counts = {
        "full_read": 0,      # whole-content verify read (active engine.json)
        "json_parse": 0,     # full json.loads through the counted seam chain
        "degraded": 0,       # 256 KiB head+tail scan of an oversized file
        "yaml": 0,           # issue-YAML parse
        "index_rebuild": 0,  # build_index cold rebuild (the ~17.5k-stat walk)
    }

    real_read = disk_cache._read_active_content

    def counting_read(path):
        counts["full_read"] += 1
        return real_read(path)

    monkeypatch.setattr(disk_cache, "_read_active_content", counting_read)

    real_loads = disk_cache._json_loads

    def counting_loads(raw):
        counts["json_parse"] += 1
        return real_loads(raw)

    monkeypatch.setattr(disk_cache, "_json_loads", counting_loads)

    real_degraded = disk_cache._degraded_header

    def counting_degraded(path, size):
        counts["degraded"] += 1
        return real_degraded(path, size)

    monkeypatch.setattr(disk_cache, "_degraded_header", counting_degraded)

    import yaml

    real_yaml_load = yaml.load

    def counting_yaml(*args, **kwargs):
        counts["yaml"] += 1
        return real_yaml_load(*args, **kwargs)

    monkeypatch.setattr(yaml, "load", counting_yaml)

    real_fresh = DaemonHistoryReader._build_index_fresh

    def counting_fresh(self):
        counts["index_rebuild"] += 1
        return real_fresh(self)

    monkeypatch.setattr(DaemonHistoryReader, "_build_index_fresh", counting_fresh)
    return counts


def test_idle_fast_ticks_cost_one_sentinel_stat_per_root(
    tmp_path, io_counters, deep_scans, sentinel_stats
):
    """Gated fast ticks: zero reads/parses/scans, exactly 1 stat per root."""
    root_a, root_b = _build_idle_estate(tmp_path)
    reader = DaemonHistoryReader(
        project_roots_provider=lambda: [root_a, root_b]
    )

    # Warm-up: one deep scan finds no active flow anywhere and arms both
    # roots' sentinel gates (the cold engine.json read/parse is paid here).
    assert reader.active_flow_signature() == {}
    baseline = dict(io_counters)
    deep_scans.clear()
    sentinel_stats.clear()

    for _ in range(_IDLE_TICKS):
        assert reader.active_flow_signature() == {}

    assert io_counters["full_read"] == baseline["full_read"]
    assert io_counters["json_parse"] == baseline["json_parse"]
    assert io_counters["degraded"] == baseline["degraded"]
    assert deep_scans == []
    # Exactly one sentinel stat per root per tick — the whole cost of a
    # gated idle fast tick.
    per_root = Counter(sentinel_stats)
    assert len(per_root) == 2
    assert set(per_root.values()) == {_IDLE_TICKS}


def test_idle_status_ticks_reuse_every_cache(tmp_path, io_counters):
    """Ungated status ticks: 0 YAML parses, 0 cold rebuilds, 0 big-file reads."""
    root_a, root_b = _build_idle_estate(tmp_path)
    reader = DaemonHistoryReader(
        project_roots_provider=lambda: [root_a, root_b]
    )
    aggregator = DaemonAggregator(machine_id="m-idle")
    aggregator.add_project_root(root_a)
    aggregator.add_project_root(root_b)

    # Warm-up: one full pass pays every cold cost exactly once.
    reader.active_flow_signature()
    warm_index = reader.build_index()
    aggregator.get_snapshot()
    assert io_counters["yaml"] >= _ISSUE_FARM_SIZE  # cold issue-farm parse
    assert io_counters["index_rebuild"] == 1
    assert io_counters["degraded"] >= 1  # the >5MB archive scanned once
    # The caches must not have degraded correctness: all three flows are
    # indexed, including the one whose only source is the degraded header.
    indexed = {m.flow_id for m in warm_index}
    assert {"flow-term", "flow-hist", "flow-big-archive"} <= indexed

    baseline = dict(io_counters)
    snapshot = None
    for _ in range(3):
        # One status tick's disk work, mirroring the push/poll loops: the
        # ungated backstop signature scan, the aggregator snapshot, and the
        # (token-guarded) index serve.
        reader.clear_sentinel_gate()
        reader.active_flow_signature()
        snapshot = aggregator.get_snapshot()
        assert {m.flow_id for m in reader.build_index()} == indexed

    assert io_counters["yaml"] == baseline["yaml"]
    assert io_counters["index_rebuild"] == baseline["index_rebuild"]
    assert io_counters["degraded"] == baseline["degraded"]
    assert io_counters["json_parse"] == baseline["json_parse"]
    # The only residual read is the bounded verify of root_a's small terminal
    # engine.json inside get_snapshot (the ungated signature scan itself is
    # read-free: the stat-keyed peek skips terminal flows) — one whole-file
    # read+hash per engine.json-bearing root per status tick, never a parse.
    assert io_counters["full_read"] - baseline["full_read"] <= 3
    # Snapshot correctness is intact off the caches.
    assert len(snapshot.issues) == _ISSUE_FARM_SIZE


# --------------------------------------------------------------------------
# task 12 — the calls-signature scan reuses the SAME sentinel gate
#
# The reader's dirty-sentinel gate must also elide the aggregator's
# ``pending_calls_signature`` ``se3/calls/`` iterdir for a gated idle root, so
# the WHOLE idle fast tick (history + calls) collapses to the single sentinel
# stat the history scan already pays — not "1 stat + 1 calls enumeration".
# --------------------------------------------------------------------------


def _count_calls_iterdir(monkeypatch):
    """Count ``iterdir`` calls that touch any ``se3/calls`` directory."""
    import pathlib

    real = pathlib.Path.iterdir
    hits: List[str] = []

    def counting(self):
        if self.name == "calls":
            hits.append(str(self))
        return real(self)

    monkeypatch.setattr(pathlib.Path, "iterdir", counting)
    return hits


def test_calls_signature_skips_gated_idle_root(tmp_path, monkeypatch):
    """A gated root's calls dir is NOT enumerated and its fingerprint reused."""
    root = tmp_path / "proj"
    calls_dir = root / "se3" / "calls"
    calls_dir.mkdir(parents=True)
    (calls_dir / "interjection_1.json").write_text("{}", encoding="utf-8")

    agg = DaemonAggregator(machine_id="m-calls")
    agg.add_project_root(root)

    scans = _count_calls_iterdir(monkeypatch)

    # Ungated baseline: the calls dir is enumerated once.
    base = agg.pending_calls_signature()
    assert str(root) in base
    assert len(scans) == 1

    # Gate the root; a new call file lands but the gated tick must neither
    # enumerate the dir again NOR let the new file shift the fingerprint (the
    # prior per-root tuple is reused verbatim, keeping the client diff stable).
    agg.set_calls_gate_source(lambda: {str(root)})
    (calls_dir / "interjection_2.json").write_text("{}", encoding="utf-8")
    gated_sig = agg.pending_calls_signature()
    assert len(scans) == 1  # zero additional calls-dir enumerations
    assert gated_sig == base  # new file invisible while gated

    # Un-gate (what a sentinel bump / active-flow tick effects): the same call
    # dir is re-scanned and the new file surfaces.
    agg.set_calls_gate_source(lambda: set())
    fresh = agg.pending_calls_signature()
    assert len(scans) == 2
    assert fresh != base


def test_calls_signature_gate_source_failure_scans_all(tmp_path, monkeypatch):
    """A raising gate source fails open to a full scan (optimization, not a
    correctness dependency)."""
    root = tmp_path / "proj"
    calls_dir = root / "se3" / "calls"
    calls_dir.mkdir(parents=True)
    (calls_dir / "interjection_1.json").write_text("{}", encoding="utf-8")

    agg = DaemonAggregator(machine_id="m-calls-fail")
    agg.add_project_root(root)

    def _boom():
        raise RuntimeError("gate source down")

    agg.set_calls_gate_source(_boom)
    scans = _count_calls_iterdir(monkeypatch)
    sig = agg.pending_calls_signature()
    assert len(scans) == 1  # scanned despite the gate error
    assert str(root) in sig


def test_gated_roots_reports_idle_roots_from_reader(tmp_path):
    """The reader's ``gated_roots`` reflects the last signature pass's verdict.

    This is the seam the aggregator's calls scan consumes: a settled terminal
    root (idle + sentinel) is reported; an active root is not.
    """
    _write_engine_raw(tmp_path, {"flow_id": "done", "status": "COMPLETED"})
    _bump_sentinel_raw(tmp_path, 1)
    reader = _make_reader(tmp_path)

    # First pass arms the gate; the root is now reported as gated.
    assert reader.active_flow_signature() == {}
    assert reader.gated_roots() == {str(tmp_path)}

    # An active flow (persisted, so the sentinel moves) un-gates the root.
    pm = PersistenceManager(tmp_path)
    pm.save_flow(_make_flow("live", FlowStatus.RUNNING))
    assert set(reader.active_flow_signature()) == {"live"}
    assert reader.gated_roots() == set()
