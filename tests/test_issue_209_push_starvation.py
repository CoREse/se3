"""Regression lock for issue #209: daemon push-loop starvation under load.

issue #209 — after a ``se3 run`` confirms its discovery plan and steps into
``analyze`` (discovery→analyze), or when a later step errors and is manually
retried, the WebUI conversation freezes (the left status bar keeps advancing,
driven by the request/response REST poll, but the push-driven live ``history_data``
append never arrives) until the operator exits and re-enters the session.

The G1 diagnosis (``tests/ISSUE_209_FREEZE_DIAGNOSIS.md``) localized the layer
with real reproductions: the freeze is **daemon push-loop starvation under
realistic project load**, not the frontend / server-cache / ``read_flow`` /
``dedupeAppendRecords`` layers (all proven correct on the real frames in
``tests/frontend/fixtures/issue_209/``).  The CPU sink that starves the loop is
the repeated ``json.loads`` of the *active* ``engine.json`` — which grows to
~1 MB on a long-running flow — several times per push tick across
``active_flow_signature`` + ``build_index`` + ``_is_still_active`` +
``live_flow_ids``.  Those parses are GIL-bound, so they block the event loop /
serialize the offloaded reads, and the discovery→analyze (and the
step-agnostic retry) live-append frame never reaches the web.

The G2 root-cause fix memoizes the active ``engine.json`` parse by its
``(mtime, size)`` so it is parsed at most once per *actual* change, shared
across all per-tick readers in the single daemon reader instance.  These tests
count the parses to lock that fix in place: they FAIL before the fix (one parse
per reader call per tick) and PASS after (one parse per change), while
asserting the reads stay correct.
"""

from __future__ import annotations

import json
from pathlib import Path

import tianluo.daemon.disk_json_cache as disk_cache
import tianluo.daemon.history as history_mod
from tianluo.daemon.history import DaemonHistoryReader


def _write_engine(root: Path, flow_id: str, status: str, *, blob_steps: int = 0) -> None:
    state_dir = root / "se3" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {"flow_id": flow_id, "status": status}
    if blob_steps:
        # Mimic a long-running flow's large engine.json (~1 MB), the parse of
        # which is the per-tick CPU sink #209 traced.
        payload["state"] = {
            "steps": {
                f"{i:02d}_step_{i:08x}": {"status": "RUNNING", "blob": "x" * 600}
                for i in range(blob_steps)
            }
        }
    (state_dir / "engine.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_jsonl(path: Path, n_lines: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            json.dumps({"role": "assistant", "content": f"line {i}"})
            for i in range(n_lines)
        )
        + "\n",
        encoding="utf-8",
    )


def _count_engine_parses(monkeypatch, engine_path: Path) -> dict:
    """Patch ``disk_json_cache._parse_json`` to count engine.json *full parses*.

    Returns a mutable ``{"n": int}`` counter.  The stat-keyed
    ``read_engine_header`` skips even the read on an unchanged file and only
    *full-parses* (``disk_json_cache._parse_json``, the GIL-bound ``json.loads``)
    when the ``(mtime, size)`` changed; counting that seam measures exactly the
    expensive operation the #209 fix collapses.  The module-level cache is
    cleared first so the count starts from a clean slate for this file.

    ``_parse_json`` is only ever reached for the whole-file parse of an
    engine-shaped state file; per-step jsonl reads parse via ``read_flow``'s own
    ``json.loads``.  With a single root there is exactly one engine.json, so
    counting every call measures that file's parses.
    """
    disk_cache.clear_cache()
    counter = {"n": 0}
    original = disk_cache._parse_json

    def counting_parse(raw):
        counter["n"] += 1
        return original(raw)

    monkeypatch.setattr(disk_cache, "_parse_json", counting_parse)
    return counter


def test_active_engine_json_parsed_once_per_change_across_readers(tmp_path, monkeypatch):
    """The active engine.json is parsed once, not once per reader per tick.

    Drives every per-tick reader that touches the active engine.json
    (``active_flow_signature``, ``build_index``, ``read_active_flows`` →
    ``_is_still_active``, ``live_flow_ids``) repeatedly with the file unchanged
    and asserts the large parse happened at most once.  Pre-fix each call
    re-parsed it, so this count grew with the tick count (the starvation).
    """
    root = tmp_path / "proj"
    _write_engine(root, "f1", "RUNNING", blob_steps=1200)
    _write_jsonl(root / "se3" / "history" / "f1" / "02_analyze_bbbb.jsonl", 5)

    engine_path = root / "se3" / "state" / "engine.json"
    counter = _count_engine_parses(monkeypatch, engine_path)

    reader = DaemonHistoryReader(project_roots_provider=lambda: [root])

    # Simulate several push-loop ticks with the active engine.json UNCHANGED.
    cursors: dict = {}
    for _ in range(10):
        reader.active_flow_signature()
        reader.invalidate_index_cache()  # an appending flow invalidates each tick
        reads = reader.read_active_flows(cursors=cursors)
        cursors = {r.flow_id: r.cursor for r in reads}
        reader.live_flow_ids()

    # One parse for the whole run despite 10 ticks × 4 readers — the stat-keyed
    # cache collapses the repeated large parses to one per change.
    assert counter["n"] <= 1, (
        f"active engine.json was parsed {counter['n']} times across unchanged "
        "ticks; the per-tick re-parse starvation regressed"
    )


def test_active_engine_json_reparsed_after_change(tmp_path, monkeypatch):
    """A real engine.json rewrite (step transition / retry) is re-parsed.

    The stat-keyed cache must not serve a stale parse across a genuine change:
    the discovery→analyze transition and the retry boundary both rewrite the
    active engine.json (new status / step), and the new state must be observed.
    """
    root = tmp_path / "proj"
    _write_engine(root, "f1", "RUNNING", blob_steps=10)
    engine_path = root / "se3" / "state" / "engine.json"
    counter = _count_engine_parses(monkeypatch, engine_path)

    reader = DaemonHistoryReader(project_roots_provider=lambda: [root])

    assert reader.live_flow_ids() == {"f1"}
    first = counter["n"]
    assert first >= 1

    # Unchanged read — served from the cache, no new parse.
    reader.live_flow_ids()
    assert counter["n"] == first

    # A step transition rewrites engine.json with different content/size.
    _write_engine(root, "f1", "RUNNING", blob_steps=40)
    reader.live_flow_ids()
    assert counter["n"] == first + 1, "rewrite must invalidate the stat-keyed cache"


def test_discovery_to_analyze_transition_surfaces_via_read_active_flows(tmp_path):
    """The discovery→analyze append is delivered incrementally with caching on.

    A correctness guard paired with the parse-count guards: the cache must not
    suppress the live-append delta that carries the analyze step after the
    discovery jsonl is followed by a new analyze jsonl.
    """
    root = tmp_path / "proj"
    flow_id = "20260101-000000_flow"
    _write_engine(root, flow_id, "RUNNING", blob_steps=20)
    hist = root / "se3" / "history" / flow_id
    _write_jsonl(hist / "01_discovery_aaaa.jsonl", 3)

    reader = DaemonHistoryReader(project_roots_provider=lambda: [root])

    # First push: full snapshot of discovery.
    reads = reader.read_active_flows(cursors={})
    cursors = {r.flow_id: r.cursor for r in reads}
    step_ids = {rec["step_id"] for r in reads for rec in r.records}
    assert any("discovery" in s for s in step_ids)
    assert not any("analyze" in s for s in step_ids)

    # The flow steps into analyze: a new step jsonl appears, engine.json
    # advances.  Invalidate the index cache as the live push loop does.
    _write_engine(root, flow_id, "RUNNING", blob_steps=21)
    _write_jsonl(hist / "02_analyze_bbbb.jsonl", 4)
    reader.invalidate_index_cache()

    reads = reader.read_active_flows(cursors=cursors)
    analyze_recs = [
        rec for r in reads for rec in r.records if "analyze" in rec["step_id"]
    ]
    assert len(analyze_recs) == 4, (
        "the discovery→analyze append must surface incrementally even with the "
        "engine.json parse cache enabled"
    )
    # And it is an append delta (not a forced full re-read).
    analyze_reads = [r for r in reads if any("analyze" in rec["step_id"] for rec in r.records)]
    assert analyze_reads and analyze_reads[0].mode == history_mod.HISTORY_MODE_APPEND


def test_retry_after_error_append_surfaces_via_read_active_flows(tmp_path):
    """The retry boundary (same step_id, appended records) surfaces too.

    issue #209's two triggers share one push path; the retry case re-runs a
    step under the SAME step_id and appends to the same jsonl.  With the cache
    on, those appended retry records must still stream incrementally.
    """
    root = tmp_path / "proj"
    flow_id = "20260101-000000_flow"
    _write_engine(root, flow_id, "RUNNING", blob_steps=20)
    hist = root / "se3" / "history" / flow_id
    step = hist / "03_update_spec_cccc.jsonl"
    _write_jsonl(step, 2)

    reader = DaemonHistoryReader(project_roots_provider=lambda: [root])
    reads = reader.read_active_flows(cursors={})
    cursors = {r.flow_id: r.cursor for r in reads}

    # The step errored and the operator retried: engine.json flips status and
    # the same jsonl gets MORE records appended (append-only, same step_id).
    _write_engine(root, flow_id, "RUNNING", blob_steps=21)
    with step.open("a", encoding="utf-8") as fh:
        for i in range(3):
            fh.write(json.dumps({"role": "assistant", "content": f"retry {i}"}) + "\n")
    reader.invalidate_index_cache()

    reads = reader.read_active_flows(cursors=cursors)
    retry_recs = [
        rec
        for r in reads
        for rec in r.records
        if "update_spec" in rec["step_id"]
    ]
    assert len(retry_recs) == 3, (
        "the retry append (same step_id) must surface incrementally with the "
        "engine.json parse cache enabled"
    )
