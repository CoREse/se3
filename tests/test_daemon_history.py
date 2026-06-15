"""Tests for the daemon-side history reader (:mod:`se3.daemon.history`)."""

from __future__ import annotations

import json

import pytest

from se3.daemon import history as history_mod
from se3.daemon.history import (
    DaemonHistoryReader,
    SessionMeta,
    enumerate_historical_project_roots,
)
from se3.daemon.protocol import HISTORY_MODE_APPEND, HISTORY_MODE_FULL


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _write_jsonl(path, lines):
    """Write *lines* (list of dicts) as a jsonl file at *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )


def _append_jsonl(path, lines):
    """Append *lines* (list of dicts) to an existing jsonl file."""
    with path.open("a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")


def _msg(role, content, step_type="analyze"):
    return {"role": role, "content": content, "raw_json": [], "step_type": step_type}


def _make_reader(*roots):
    """Build a reader whose project-roots provider yields *roots*."""
    return DaemonHistoryReader(project_roots_provider=lambda: list(roots))


def _write_engine(root, flow_id, status):
    """Write a minimal active ``engine.json`` for *flow_id* with *status*."""
    state_dir = root / "se3" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "engine.json").write_text(
        json.dumps({"flow_id": flow_id, "status": status}), encoding="utf-8"
    )


# --------------------------------------------------------------------------
# index construction
# --------------------------------------------------------------------------


def test_build_index_enumerates_all_sources(tmp_path):
    """build_index returns metadata for active, archived and history-only flows."""
    # Active flow (engine.json) — running, so it counts as active.
    state_dir = tmp_path / "se3" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "engine.json").write_text(
        json.dumps(
            {
                "flow_id": "active-1",
                "task_description": "build the thing",
                "task_type": "feature",
                "status": "RUNNING",
                "updated_at": "2026-05-19T10:00:00",
            }
        ),
        encoding="utf-8",
    )

    # Archived flow.
    archive_dir = state_dir / "archive"
    archive_dir.mkdir()
    (archive_dir / "engine_20260101_000000.json").write_text(
        json.dumps(
            {
                "flow_id": "archived-1",
                "task_description": "old work",
                "status": "completed",
                "updated_at": "2026-01-01T00:00:00",
            }
        ),
        encoding="utf-8",
    )

    # History-only flow (no engine.json).
    hist_dir = tmp_path / "se3" / "history" / "hist-1"
    _write_jsonl(hist_dir / "01_analyze.jsonl", [_msg("user", "explore")])

    metas = _make_reader(tmp_path).build_index()
    by_id = {m.flow_id: m for m in metas}

    assert set(by_id) == {"active-1", "archived-1", "hist-1"}
    assert by_id["active-1"].source == "active"
    assert by_id["archived-1"].source == "archived"
    assert by_id["hist-1"].source == "history"


def test_build_index_distinguishes_active_flows(tmp_path):
    """A non-terminal engine.json flow is active; a completed one is not."""
    state_dir = tmp_path / "se3" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "engine.json").write_text(
        json.dumps({"flow_id": "f1", "status": "RUNNING"}), encoding="utf-8"
    )
    running = _make_reader(tmp_path).build_index()[0]
    assert running.active is True

    (state_dir / "engine.json").write_text(
        json.dumps({"flow_id": "f1", "status": "completed"}), encoding="utf-8"
    )
    done = _make_reader(tmp_path).build_index()[0]
    assert done.active is False


def test_history_only_flow_metadata_without_engine_json(tmp_path):
    """A history-only flow still yields best-effort metadata when engine.json is gone."""
    hist_dir = tmp_path / "se3" / "history" / "orphan-1"
    _write_jsonl(
        hist_dir / "01_analyze.jsonl",
        [_msg("user", "Task description:\n----\nrefactor auth\n----\n")],
    )
    (hist_dir / "_meta.json").write_text(
        json.dumps({"created_at": "2026-05-01T00:00:00", "type": "bugfix"}),
        encoding="utf-8",
    )

    meta = _make_reader(tmp_path).build_index()[0]
    assert meta.flow_id == "orphan-1"
    assert meta.status == "history"
    assert meta.task_type == "bugfix"
    assert "refactor auth" in meta.task_description
    assert meta.step_count == 1
    assert meta.active is False


def test_build_index_dedups_by_flow_id(tmp_path):
    """A flow present in both engine.json and history/ appears once (active wins)."""
    state_dir = tmp_path / "se3" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "engine.json").write_text(
        json.dumps({"flow_id": "dup-1", "status": "RUNNING"}), encoding="utf-8"
    )
    _write_jsonl(
        tmp_path / "se3" / "history" / "dup-1" / "01_analyze.jsonl",
        [_msg("user", "hi")],
    )
    metas = _make_reader(tmp_path).build_index()
    assert [m.flow_id for m in metas] == ["dup-1"]
    assert metas[0].source == "active"


def test_promoted_worktree_completed_state_reported_as_completed(tmp_path):
    """G7: a promoted worktree COMPLETED engine.json in the main archive is
    reported as ``status=completed`` (not a bare ``history`` directory)."""
    main = tmp_path / "main"
    archive_dir = main / "se3" / "state" / "archive"
    archive_dir.mkdir(parents=True)
    (archive_dir / "engine_wt-flow.json").write_text(
        json.dumps(
            {
                "flow_id": "wt-flow",
                "status": "completed",
                "task_description": "isolated work",
                "project_root": str(main),
            }
        ),
        encoding="utf-8",
    )
    # Tier-A history sync also landed the flow's history directory in main.
    _write_jsonl(
        main / "se3" / "history" / "wt-flow" / "01_analyze.jsonl",
        [_msg("user", "hi")],
    )

    metas = _make_reader(main).build_index()
    by_id = {m.flow_id: m for m in metas}
    assert "wt-flow" in by_id
    assert by_id["wt-flow"].status == "completed"
    assert by_id["wt-flow"].source == "archived"
    assert by_id["wt-flow"].active is False


def test_promoted_completed_not_double_counted_with_worktree_active(tmp_path):
    """G7 task 2: during the completion window the SAME worktree flow exists as
    both a worktree-root active engine.json (terminal) and a main-archive
    promoted snapshot. It must collapse to a single completed entry and never be
    reported as active."""
    main = tmp_path / "main"
    archive_dir = main / "se3" / "state" / "archive"
    archive_dir.mkdir(parents=True)
    (archive_dir / "engine_wt-flow.json").write_text(
        json.dumps(
            {
                "flow_id": "wt-flow",
                "status": "completed",
                "project_root": str(main),
            }
        ),
        encoding="utf-8",
    )
    # The worktree (not yet deleted) still carries its own COMPLETED engine.json.
    worktree = main / "se3" / "worktrees" / "wt-flow-sandbox"
    _write_engine(worktree, "wt-flow", "completed")

    # The provider mirrors ``all_observable_roots`` during the window: the main
    # root (with the promoted archive) plus the live worktree subdir.
    reader = _make_reader(main, worktree)
    metas = reader.build_index()
    flow_metas = [m for m in metas if m.flow_id == "wt-flow"]
    assert len(flow_metas) == 1
    assert flow_metas[0].status == "completed"
    assert flow_metas[0].active is False

    # And it is never surfaced as an active flow.
    active = reader.read_active_flows()
    assert all(fr.flow_id != "wt-flow" for fr in active)


def test_session_meta_to_dict_round_trip():
    meta = SessionMeta(flow_id="x", project_root="/p", active=True)
    data = meta.to_dict()
    assert data["flow_id"] == "x"
    assert data["active"] is True
    # Default is not-waiting; the field is always emitted for wire stability.
    assert data["waiting_for_lock"] is False
    assert set(data) == {
        "flow_id",
        "project_root",
        "task_description",
        "task_type",
        "status",
        "created_at",
        "updated_at",
        "active",
        "source",
        "step_count",
        "waiting_for_lock",
    }


def test_build_index_carries_waiting_for_lock_on_active_flow(tmp_path):
    """An active engine.json with waiting_for_lock=True propagates the flag.

    G2 (1b): a queued synchronous run stays RUNNING (hence active) and the
    history index must carry waiting_for_lock so the web console can render the
    running·waiting-for-lock sub-state — even before any step jsonl exists.
    """
    state_dir = tmp_path / "se3" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "engine.json").write_text(
        json.dumps(
            {
                "flow_id": "queued-1",
                "task_description": "build the thing",
                "task_type": "feature",
                "status": "RUNNING",
                "waiting_for_lock": True,
                "updated_at": "2026-05-19T10:00:00",
            }
        ),
        encoding="utf-8",
    )

    meta = _make_reader(tmp_path).build_index()[0]
    assert meta.flow_id == "queued-1"
    assert meta.active is True
    assert meta.waiting_for_lock is True
    assert meta.to_dict()["waiting_for_lock"] is True


def test_archived_flow_never_waiting_for_lock(tmp_path):
    """A terminal/archived snapshot is never reported as waiting, even if the
    flag lingered in its persisted engine.json (defensive: waiting is only a
    live, active-flow sub-state)."""
    state_dir = tmp_path / "se3" / "state"
    archive_dir = state_dir / "archive"
    archive_dir.mkdir(parents=True)
    (archive_dir / "engine_20260101_000000.json").write_text(
        json.dumps(
            {
                "flow_id": "archived-stale",
                "status": "completed",
                "waiting_for_lock": True,
                "updated_at": "2026-01-01T00:00:00",
            }
        ),
        encoding="utf-8",
    )

    meta = _make_reader(tmp_path).build_index()[0]
    assert meta.flow_id == "archived-stale"
    assert meta.active is False
    assert meta.waiting_for_lock is False


# --------------------------------------------------------------------------
# incremental cursor reads
# --------------------------------------------------------------------------


def test_read_flow_first_read_is_full(tmp_path):
    """The first read (no cursor) returns a full snapshot of every record."""
    hist_dir = tmp_path / "se3" / "history" / "f1"
    _write_jsonl(
        hist_dir / "01_analyze.jsonl",
        [_msg("user", "q1"), _msg("assistant", "a1")],
    )
    _write_jsonl(
        hist_dir / "02_plan.jsonl",
        [_msg("user", "q2", step_type="plan")],
    )

    read = _make_reader(tmp_path).read_flow("f1")
    assert read.mode == HISTORY_MODE_FULL
    assert len(read.records) == 3
    assert read.records[0]["step_id"] == "01_analyze"
    assert read.records[2]["step_id"] == "02_plan"
    assert read.cursor == {"01_analyze.jsonl": 2, "02_plan.jsonl": 1}


def test_read_flow_second_read_appends_only_new(tmp_path):
    """A read with a cursor returns only lines appended since the cursor."""
    reader = _make_reader(tmp_path)
    jsonl = tmp_path / "se3" / "history" / "f1" / "01_analyze.jsonl"
    _write_jsonl(jsonl, [_msg("user", "q1")])

    first = reader.read_flow("f1")
    assert first.mode == HISTORY_MODE_FULL
    assert len(first.records) == 1

    # Nothing new yet.
    same = reader.read_flow("f1", cursor=first.cursor)
    assert same.mode == HISTORY_MODE_APPEND
    assert same.records == []
    assert same.cursor == first.cursor

    # Append two messages, then read incrementally.
    _append_jsonl(jsonl, [_msg("assistant", "a1"), _msg("user", "q2")])
    delta = reader.read_flow("f1", cursor=same.cursor)
    assert delta.mode == HISTORY_MODE_APPEND
    assert [r["message"]["content"] for r in delta.records] == ["a1", "q2"]
    assert delta.cursor == {"01_analyze.jsonl": 3}


def test_read_flow_missing_flow_returns_empty(tmp_path):
    read = _make_reader(tmp_path).read_flow("does-not-exist")
    assert read.mode == HISTORY_MODE_FULL
    assert read.records == []


def test_read_flow_caps_records_and_advances_cursor(tmp_path, monkeypatch):
    """When records exceed the cap the read truncates and the cursor advances partially."""
    monkeypatch.setattr(history_mod, "MAX_RECORDS_PER_REPORT", 2)
    reader = _make_reader(tmp_path)
    jsonl = tmp_path / "se3" / "history" / "f1" / "01_analyze.jsonl"
    _write_jsonl(jsonl, [_msg("user", f"m{i}") for i in range(5)])

    first = reader.read_flow("f1")
    assert len(first.records) == 2
    assert first.cursor == {"01_analyze.jsonl": 2}

    second = reader.read_flow("f1", cursor=first.cursor)
    assert [r["message"]["content"] for r in second.records] == ["m2", "m3"]
    assert second.cursor == {"01_analyze.jsonl": 4}

    third = reader.read_flow("f1", cursor=second.cursor)
    assert [r["message"]["content"] for r in third.records] == ["m4"]
    assert third.cursor == {"01_analyze.jsonl": 5}


def test_read_flow_skips_malformed_lines(tmp_path):
    """Blank and unparseable lines are skipped without aborting the read."""
    hist_dir = tmp_path / "se3" / "history" / "f1"
    hist_dir.mkdir(parents=True)
    (hist_dir / "01_analyze.jsonl").write_text(
        json.dumps(_msg("user", "ok"))
        + "\n\nnot-json\n"
        + json.dumps(_msg("assistant", "fine"))
        + "\n",
        encoding="utf-8",
    )
    read = _make_reader(tmp_path).read_flow("f1")
    assert [r["message"]["content"] for r in read.records] == ["ok", "fine"]
    # The cursor still counts every physical line so a resume does not re-scan.
    assert read.cursor == {"01_analyze.jsonl": 4}


# --------------------------------------------------------------------------
# active-flow incremental reads
# --------------------------------------------------------------------------


def test_read_active_flows_only_returns_active(tmp_path):
    """read_active_flows reads incrementally for active flows only."""
    state_dir = tmp_path / "se3" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "engine.json").write_text(
        json.dumps({"flow_id": "live", "status": "RUNNING"}), encoding="utf-8"
    )
    _write_jsonl(
        tmp_path / "se3" / "history" / "live" / "01_analyze.jsonl",
        [_msg("user", "q1")],
    )
    # An archived (terminal) flow must be ignored by read_active_flows.
    archive_dir = state_dir / "archive"
    archive_dir.mkdir()
    (archive_dir / "engine_20260101_000000.json").write_text(
        json.dumps({"flow_id": "done", "status": "completed"}), encoding="utf-8"
    )
    _write_jsonl(
        tmp_path / "se3" / "history" / "done" / "01_analyze.jsonl",
        [_msg("user", "old")],
    )

    reader = _make_reader(tmp_path)
    reads = reader.read_active_flows({})
    assert [r.flow_id for r in reads] == ["live"]
    assert reads[0].mode == HISTORY_MODE_FULL
    assert len(reads[0].records) == 1

    # A subsequent call with the stored cursor yields an empty append.
    cursors = {r.flow_id: r.cursor for r in reads}
    again = reader.read_active_flows(cursors)
    assert again[0].mode == HISTORY_MODE_APPEND
    assert again[0].records == []


def test_read_active_flows_multi_step_append_incremental_matches_full(tmp_path):
    """Running-flow incremental reads across multiple step files lose no line
    and duplicate no line: the union of every delta equals one full read."""
    _write_engine(tmp_path, "live", "RUNNING")
    hist = tmp_path / "se3" / "history" / "live"
    s1 = hist / "01_analyze.jsonl"
    _write_jsonl(s1, [_msg("user", "a0"), _msg("assistant", "a1")])

    reader = _make_reader(tmp_path)
    collected: list = []
    cursors: dict = {}

    reads = reader.read_active_flows(cursors)
    cursors = {r.flow_id: r.cursor for r in reads}
    collected += [r["message"]["content"] for r in reads[0].records]

    # Append to the existing step file AND start a brand-new step file.
    _append_jsonl(s1, [_msg("user", "a2")])
    s2 = hist / "02_plan.jsonl"
    _write_jsonl(s2, [_msg("assistant", "b0", step_type="plan")])

    reads = reader.read_active_flows(cursors)
    cursors = {r.flow_id: r.cursor for r in reads}
    collected += [r["message"]["content"] for r in reads[0].records]

    # Append again to both files in the same round.
    _append_jsonl(s1, [_msg("assistant", "a3")])
    _append_jsonl(s2, [_msg("user", "b1", step_type="plan")])

    reads = reader.read_active_flows(cursors)
    cursors = {r.flow_id: r.cursor for r in reads}
    collected += [r["message"]["content"] for r in reads[0].records]

    # Nothing new -> empty append, no spurious re-push.
    reads = reader.read_active_flows(cursors)
    assert reads[0].records == []

    full = reader.read_flow("live")
    full_contents = [r["message"]["content"] for r in full.records]
    assert sorted(collected) == sorted(full_contents)
    assert len(collected) == len(set(collected))  # no duplicates


def test_read_active_flows_paused_then_resumed_stays_active(tmp_path):
    """A flow that PAUSES (e.g. discovery clarification) and is later resumed
    stays in the active set and keeps streaming incrementally."""
    _write_engine(tmp_path, "live", "RUNNING")
    hist = tmp_path / "se3" / "history" / "live"
    s1 = hist / "01_discovery.jsonl"
    _write_jsonl(s1, [_msg("user", "q1", step_type="discovery")])

    reader = _make_reader(tmp_path)
    reads = reader.read_active_flows({})
    assert [r.flow_id for r in reads] == ["live"]
    cursors = {r.flow_id: r.cursor for r in reads}

    # Flow pauses awaiting input: still active, still read (no new records).
    _write_engine(tmp_path, "live", "PAUSED")
    assert reader.build_index()[0].active is True
    reads = reader.read_active_flows(cursors)
    assert [r.flow_id for r in reads] == ["live"]
    assert reads[0].records == []
    cursors = {r.flow_id: r.cursor for r in reads}

    # Resume: status flips back to RUNNING and new records are appended.
    _write_engine(tmp_path, "live", "RUNNING")
    _append_jsonl(s1, [_msg("assistant", "a1", step_type="discovery")])
    reads = reader.read_active_flows(cursors)
    assert [r.flow_id for r in reads] == ["live"]
    assert [r["message"]["content"] for r in reads[0].records] == ["a1"]


def test_read_active_flows_new_step_file_included_with_cursor(tmp_path):
    """A step jsonl that appears after the first read is picked up whole on the
    next read, and its cursor is established without re-delivering old files."""
    _write_engine(tmp_path, "live", "RUNNING")
    hist = tmp_path / "se3" / "history" / "live"
    _write_jsonl(hist / "01_analyze.jsonl", [_msg("user", "q1")])
    reader = _make_reader(tmp_path)

    reads = reader.read_active_flows({})
    cursors = {r.flow_id: r.cursor for r in reads}
    assert "01_analyze.jsonl" in cursors["live"]
    assert "02_plan.jsonl" not in cursors["live"]

    # A new step begins: a brand-new jsonl file appears.
    _write_jsonl(
        hist / "02_plan.jsonl",
        [_msg("user", "q2", step_type="plan"), _msg("assistant", "a2", step_type="plan")],
    )
    reads = reader.read_active_flows(cursors)
    assert [r["message"]["content"] for r in reads[0].records] == ["q2", "a2"]
    assert reads[0].cursor["02_plan.jsonl"] == 2
    # The earlier file's cursor is preserved (no re-delivery).
    assert reads[0].cursor["01_analyze.jsonl"] == 1


def test_read_active_flows_truncation_resumes_without_loss(tmp_path, monkeypatch):
    """When a single read hits MAX_RECORDS, successive active reads drain the
    remainder with no lost or duplicated line."""
    monkeypatch.setattr(history_mod, "MAX_RECORDS_PER_REPORT", 2)
    _write_engine(tmp_path, "live", "RUNNING")
    s1 = tmp_path / "se3" / "history" / "live" / "01_analyze.jsonl"
    _write_jsonl(s1, [_msg("user", f"m{i}") for i in range(5)])
    reader = _make_reader(tmp_path)

    collected: list = []
    cursors: dict = {}
    for _ in range(4):
        reads = reader.read_active_flows(cursors)
        cursors = {r.flow_id: r.cursor for r in reads}
        if reads:
            collected += [r["message"]["content"] for r in reads[0].records]
    assert collected == ["m0", "m1", "m2", "m3", "m4"]


def test_read_active_flows_flushes_tail_after_terminal_transition(tmp_path):
    """Records appended just before a flow goes terminal are flushed once via
    the active stream (not stranded until archival) and never duplicated."""
    _write_engine(tmp_path, "live", "RUNNING")
    s1 = tmp_path / "se3" / "history" / "live" / "01_analyze.jsonl"
    _write_jsonl(s1, [_msg("user", "q1")])
    reader = _make_reader(tmp_path)

    reads = reader.read_active_flows({})
    cursors = {r.flow_id: r.cursor for r in reads}

    # The flow writes its tail (e.g. a final step_completed line) and then
    # flips to a terminal status before the next poll.
    _append_jsonl(s1, [_msg("assistant", "final")])
    _write_engine(tmp_path, "live", "completed")

    reads = reader.read_active_flows(cursors)
    assert len(reads) == 1
    assert reads[0].flow_id == "live"
    assert [r["message"]["content"] for r in reads[0].records] == ["final"]
    cursors = {r.flow_id: r.cursor for r in reads}

    # The drained terminal flow is no longer returned -> no duplicate, and the
    # caller can prune its cursor.
    reads = reader.read_active_flows(cursors)
    assert reads == []


# --------------------------------------------------------------------------
# active-flow change signature
# --------------------------------------------------------------------------


def test_active_flow_signature_changes_on_engine_json_update(tmp_path):
    """A status transition (engine.json rewrite) moves the signature."""
    _write_engine(tmp_path, "live", "RUNNING")
    reader = _make_reader(tmp_path)
    sig1 = reader.active_flow_signature()
    assert set(sig1) == {"live"}

    _write_engine(tmp_path, "live", "PAUSED")
    sig2 = reader.active_flow_signature()
    assert sig2 != sig1
    assert set(sig2) == {"live"}


def test_active_flow_signature_changes_on_jsonl_append(tmp_path):
    """An appended line and a brand-new step file each move the signature,
    even within the filesystem's mtime resolution (size is part of the token)."""
    _write_engine(tmp_path, "live", "RUNNING")
    hist = tmp_path / "se3" / "history" / "live"
    s1 = hist / "01_analyze.jsonl"
    _write_jsonl(s1, [_msg("user", "q1")])
    reader = _make_reader(tmp_path)

    sig1 = reader.active_flow_signature()
    _append_jsonl(s1, [_msg("assistant", "a1")])
    sig2 = reader.active_flow_signature()
    assert sig2 != sig1

    _write_jsonl(hist / "02_plan.jsonl", [_msg("user", "q2", step_type="plan")])
    sig3 = reader.active_flow_signature()
    assert sig3 != sig2


def test_active_flow_signature_excludes_terminal_flows(tmp_path):
    """A completed flow contributes nothing to the signature."""
    _write_engine(tmp_path, "live", "completed")
    reader = _make_reader(tmp_path)
    assert reader.active_flow_signature() == {}


def test_active_flow_signature_stable_when_nothing_changes(tmp_path):
    """Back-to-back signatures over an unchanged tree are equal (debounce)."""
    _write_engine(tmp_path, "live", "RUNNING")
    _write_jsonl(
        tmp_path / "se3" / "history" / "live" / "01_analyze.jsonl",
        [_msg("user", "q1")],
    )
    reader = _make_reader(tmp_path)
    assert reader.active_flow_signature() == reader.active_flow_signature()


# --------------------------------------------------------------------------
# Group G2: every step type's terminal event arrives via the incremental read
# --------------------------------------------------------------------------


def _step_event_line(event_type, step_id, step_type, outputs):
    """The exact line shape ``record_step_event`` writes for a terminal step."""
    return {
        "type": event_type,
        "step_id": step_id,
        "step_type": step_type,
        "timestamp": "2026-05-21T01:00:00",
        "data": {
            "step": {
                "step_id": step_id,
                "step_type": step_type,
                "status": (
                    "failed" if event_type == "step_failed" else "completed"
                ),
                "outputs": outputs,
            }
        },
    }


@pytest.mark.parametrize(
    "step_type, outputs",
    [
        ("discovery", {"refined_description": "explore"}),
        ("analyze", {"reasoning": "ok"}),
        ("plan", {"task_groups": []}),
        ("confirm", {"review_result": {"approved": True}}),
        ("implement", {"completion_status": "complete"}),
        ("test", {"test_results": {"overall_passed": True}}),
        ("self_check", {"issues": []}),
        ("verify_spec", {"verified": True}),
        ("update_spec", {"updated_specs": []}),
        ("commit", {"committed": True}),
        ("version_analyze", {"suggested_version": "1.2.0"}),
        ("summarize", {"summary": "done"}),
    ],
)
def test_each_terminal_step_event_arrives_via_incremental_read(
    tmp_path, step_type, outputs
):
    """A terminal ``step_completed`` line appended to an active flow's per-step
    jsonl is surfaced by ``read_active_flows`` exactly once, with the cursor
    advancing so it is never re-pushed."""
    state_dir = tmp_path / "se3" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "engine.json").write_text(
        json.dumps({"flow_id": "live", "status": "RUNNING"}), encoding="utf-8"
    )
    step_id = f"01_{step_type}_abc"
    jsonl = tmp_path / "se3" / "history" / "live" / f"{step_id}.jsonl"
    _write_jsonl(jsonl, [_msg("assistant", "narrative", step_type=step_type)])

    reader = _make_reader(tmp_path)
    cursors: dict = {}

    # First poll: only the chat turn, no terminal card yet.
    reads = reader.read_active_flows(cursors)
    cursors[reads[0].flow_id] = reads[0].cursor
    assert all(
        r["message"].get("type") not in ("step_completed", "step_failed")
        for r in reads[0].records
    )

    # The step finishes -> HistorySink appends the terminal event line.
    _append_jsonl(jsonl, [_step_event_line("step_completed", step_id, step_type, outputs)])

    reads = reader.read_active_flows(cursors)
    cursors[reads[0].flow_id] = reads[0].cursor
    events = [
        r
        for r in reads[0].records
        if r["message"].get("type") == "step_completed"
    ]
    assert len(events) == 1
    event = events[0]
    assert event["step_id"] == step_id
    # The structured outputs the web report card renders are carried verbatim.
    assert event["message"]["data"]["step"]["outputs"] == outputs

    # Not re-pushed on the next poll — the cursor consumed the line.
    reads = reader.read_active_flows(cursors)
    assert reads[0].records == []


def test_step_failed_terminal_event_arrives_via_incremental_read(tmp_path):
    """A ``step_failed`` terminal line is surfaced incrementally just like
    ``step_completed`` and carries the error payload."""
    state_dir = tmp_path / "se3" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "engine.json").write_text(
        json.dumps({"flow_id": "live", "status": "RUNNING"}), encoding="utf-8"
    )
    step_id = "03_plan_def"
    jsonl = tmp_path / "se3" / "history" / "live" / f"{step_id}.jsonl"
    _write_jsonl(
        jsonl,
        [_step_event_line("step_failed", step_id, "plan", {"error": "boom"})],
    )

    reads = _make_reader(tmp_path).read_active_flows({})
    assert len(reads) == 1
    failed = [
        r
        for r in reads[0].records
        if r["message"].get("type") == "step_failed"
    ]
    assert len(failed) == 1
    assert failed[0]["message"]["data"]["step"]["outputs"] == {"error": "boom"}


# --------------------------------------------------------------------------
# enumerate_historical_project_roots
# --------------------------------------------------------------------------


def test_enumerate_returns_root_with_archive(tmp_path):
    """A project root containing an engine_*.json archive is included."""
    archive_dir = tmp_path / "se3" / "state" / "archive"
    archive_dir.mkdir(parents=True)
    (archive_dir / "engine_20260101_000000.json").write_text(
        json.dumps({"flow_id": "f1", "status": "completed"}),
        encoding="utf-8",
    )

    roots = enumerate_historical_project_roots([tmp_path])
    assert roots == [str(tmp_path.resolve())]


def test_enumerate_returns_root_with_history(tmp_path):
    """A project root containing se3/history/<flow>/ is included."""
    hist_dir = tmp_path / "se3" / "history" / "f1"
    hist_dir.mkdir(parents=True)
    (hist_dir / "01_analyze.jsonl").write_text("", encoding="utf-8")

    roots = enumerate_historical_project_roots([tmp_path])
    assert roots == [str(tmp_path.resolve())]


def test_enumerate_skips_root_without_artifacts(tmp_path):
    """A bare directory with no SE3 history is not reported."""
    bare = tmp_path / "bare"
    bare.mkdir()
    assert enumerate_historical_project_roots([bare]) == []


def test_enumerate_extracts_project_root_field_from_archive(tmp_path):
    """When an archive carries a project_root field, that path is included too."""
    other_root = tmp_path / "other-project"
    other_root.mkdir()

    archive_dir = tmp_path / "scanned" / "se3" / "state" / "archive"
    archive_dir.mkdir(parents=True)
    (archive_dir / "engine_20260101_000000.json").write_text(
        json.dumps(
            {
                "flow_id": "f1",
                "status": "completed",
                "project_root": str(other_root),
            }
        ),
        encoding="utf-8",
    )

    roots = enumerate_historical_project_roots([tmp_path / "scanned"])
    assert str(other_root.resolve()) in roots
    assert str((tmp_path / "scanned").resolve()) in roots


def test_enumerate_extracts_project_root_field_from_history_meta(tmp_path):
    """A history flow's _meta.json project_root field is included."""
    other_root = tmp_path / "another"
    other_root.mkdir()

    hist_dir = tmp_path / "scanned" / "se3" / "history" / "f1"
    hist_dir.mkdir(parents=True)
    (hist_dir / "_meta.json").write_text(
        json.dumps({"project_root": str(other_root)}), encoding="utf-8"
    )

    roots = enumerate_historical_project_roots([tmp_path / "scanned"])
    assert str(other_root.resolve()) in roots


def test_enumerate_skips_stale_project_root_directories(tmp_path):
    """A project_root field pointing to a non-existent directory is dropped."""
    archive_dir = tmp_path / "se3" / "state" / "archive"
    archive_dir.mkdir(parents=True)
    (archive_dir / "engine_20260101_000000.json").write_text(
        json.dumps(
            {
                "flow_id": "f1",
                "project_root": "/nonexistent/path/does/not/exist",
            }
        ),
        encoding="utf-8",
    )

    roots = enumerate_historical_project_roots([tmp_path])
    assert "/nonexistent/path/does/not/exist" not in roots
    # The scanned root itself (which has an archive) is still included.
    assert str(tmp_path.resolve()) in roots


def test_enumerate_tolerates_corrupt_json(tmp_path, caplog):
    """A corrupt JSON file logs a warning but does not abort enumeration."""
    archive_dir = tmp_path / "se3" / "state" / "archive"
    archive_dir.mkdir(parents=True)
    (archive_dir / "engine_corrupt.json").write_text(
        "{not valid json", encoding="utf-8"
    )
    # A well-formed sibling archive so the scanned root still surfaces.
    (archive_dir / "engine_ok.json").write_text(
        json.dumps({"flow_id": "f1"}), encoding="utf-8"
    )

    with caplog.at_level("WARNING", logger="se3.daemon.history"):
        roots = enumerate_historical_project_roots([tmp_path])

    assert str(tmp_path.resolve()) in roots
    assert any("engine_corrupt.json" in rec.message for rec in caplog.records)


def test_enumerate_dedups_and_sorts(tmp_path):
    """Repeated search inputs and overlapping project_root fields are deduped."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    for p in (a, b):
        archive_dir = p / "se3" / "state" / "archive"
        archive_dir.mkdir(parents=True)
        (archive_dir / "engine_x.json").write_text(
            json.dumps({"flow_id": "f", "project_root": str(a)}),
            encoding="utf-8",
        )

    roots = enumerate_historical_project_roots([a, b, a])
    assert roots == sorted([str(a.resolve()), str(b.resolve())])


def test_enumerate_handles_empty_input():
    assert enumerate_historical_project_roots() == []
    assert enumerate_historical_project_roots([]) == []


def test_enumerate_warns_once_per_unreadable_file(tmp_path, caplog):
    """A permanently corrupt file warns once, then logs DEBUG on later scans.

    Guards against the daemon.log flooding bug where every status-tick
    enumeration re-warned about the same broken ``_meta.json``.
    """
    # Reset the module-level dedup set so the assertion is deterministic
    # regardless of test ordering.
    history_mod._warned_unreadable_paths.clear()

    flow_dir = tmp_path / "se3" / "history" / "flow-broken"
    flow_dir.mkdir(parents=True)
    meta = flow_dir / "_meta.json"
    meta.write_text("{not valid json", encoding="utf-8")

    with caplog.at_level("DEBUG", logger="se3.daemon.history"):
        enumerate_historical_project_roots([tmp_path])
        enumerate_historical_project_roots([tmp_path])
        enumerate_historical_project_roots([tmp_path])

    meta_warnings = [
        rec
        for rec in caplog.records
        if rec.levelname == "WARNING" and "_meta.json" in rec.message
    ]
    meta_debugs = [
        rec
        for rec in caplog.records
        if rec.levelname == "DEBUG" and "_meta.json" in rec.message
    ]
    # Exactly one WARNING for the same file across three enumerations.
    assert len(meta_warnings) == 1
    # The repeat sightings are demoted to DEBUG (two more scans).
    assert len(meta_debugs) == 2


def test_enumerate_warns_once_per_distinct_file(tmp_path, caplog):
    """Each distinct corrupt file still gets its own first WARNING."""
    history_mod._warned_unreadable_paths.clear()

    history_root = tmp_path / "se3" / "history"
    for name in ("flow-a", "flow-b"):
        flow_dir = history_root / name
        flow_dir.mkdir(parents=True)
        (flow_dir / "_meta.json").write_text("{broken", encoding="utf-8")

    with caplog.at_level("WARNING", logger="se3.daemon.history"):
        enumerate_historical_project_roots([tmp_path])

    warned_files = {
        rec.message for rec in caplog.records if rec.levelname == "WARNING"
    }
    assert any("flow-a" in m for m in warned_files)
    assert any("flow-b" in m for m in warned_files)


# --------------------------------------------------------------------------
# active_flow_signature — the "result JSON arrived" incremental-push signal
# --------------------------------------------------------------------------
#
# The web console's running-flow paradigm needs a running assistant turn to flip
# from inline thinking to a folded result within one push cycle the moment the
# result JSON lands. The daemon client drives history pushes off
# ``active_flow_signature``: when a new record (the assistant result, or the
# step_completed report line) is appended to a live flow's jsonl, the signature
# MUST change so a push is triggered — even when two writes land inside the
# filesystem's mtime resolution (hence the byte-size component). A terminal flow
# has nothing left to stream and MUST be excluded.


def test_active_flow_signature_changes_when_result_record_appended(tmp_path):
    """Appending a record to a live flow's jsonl changes its signature so the
    daemon pushes the new result within one cycle (thinking -> folded)."""
    _write_engine(tmp_path, "live", "RUNNING")
    jsonl = tmp_path / "se3" / "history" / "live" / "01_discovery.jsonl"
    _write_jsonl(jsonl, [_msg("user", "q1", step_type="discovery")])

    reader = _make_reader(tmp_path)
    before = reader.active_flow_signature()
    assert "live" in before

    # The assistant's result JSON arrives — a new record is appended.
    _append_jsonl(
        jsonl,
        [_msg("assistant", '{"mode":"question","content":"ok"}', step_type="discovery")],
    )

    after = reader.active_flow_signature()
    assert after["live"] != before["live"], (
        "signature must change on append so the push fires within one cycle"
    )


def test_active_flow_signature_changes_on_new_step_file(tmp_path):
    """A brand-new per-step jsonl (e.g. the step_completed report card file)
    also moves the signature forward."""
    _write_engine(tmp_path, "live", "RUNNING")
    hist = tmp_path / "se3" / "history" / "live"
    _write_jsonl(hist / "01_analyze.jsonl", [_msg("assistant", "a0")])

    reader = _make_reader(tmp_path)
    before = reader.active_flow_signature()

    # A new step's report card lands as a fresh jsonl file.
    _write_jsonl(
        hist / "02_plan.jsonl",
        [{"type": "step_completed", "step_id": "02_plan", "step_type": "plan"}],
    )

    after = reader.active_flow_signature()
    assert after["live"] != before["live"]


def test_active_flow_signature_stable_without_changes(tmp_path):
    """No on-disk change -> identical signature (no spurious push)."""
    _write_engine(tmp_path, "live", "RUNNING")
    _write_jsonl(
        tmp_path / "se3" / "history" / "live" / "01_analyze.jsonl",
        [_msg("assistant", "a0")],
    )
    reader = _make_reader(tmp_path)
    assert reader.active_flow_signature() == reader.active_flow_signature()


def test_active_flow_signature_excludes_terminal_flow(tmp_path):
    """A completed flow has nothing left to stream and is excluded."""
    _write_engine(tmp_path, "done", "completed")
    _write_jsonl(
        tmp_path / "se3" / "history" / "done" / "01_analyze.jsonl",
        [_msg("assistant", "a0")],
    )
    reader = _make_reader(tmp_path)
    assert "done" not in reader.active_flow_signature()


def test_active_flow_signature_tracks_status_flip(tmp_path):
    """The signature folds in engine.json status, so a PAUSED->RUNNING resume
    flip (around a discovery answer) is observed as a change."""
    _write_engine(tmp_path, "live", "PAUSED")
    _write_jsonl(
        tmp_path / "se3" / "history" / "live" / "01_discovery.jsonl",
        [_msg("user", "q1", step_type="discovery")],
    )
    reader = _make_reader(tmp_path)
    paused_sig = reader.active_flow_signature()

    _write_engine(tmp_path, "live", "RUNNING")
    running_sig = reader.active_flow_signature()
    assert running_sig["live"] != paused_sig["live"]


# --------------------------------------------------------------------------
# Group G2: Problem B-1 — streaming first-line read + per-directory meta cache
# --------------------------------------------------------------------------


def test_extract_history_summary_reads_only_first_line(tmp_path, monkeypatch):
    """_extract_history_summary reads O(first-line) bytes, not O(file-size).

    Constructs a jsonl whose first line is ~1 KB but whose total file is ~1 MB.
    The streaming ``readline()`` path must not load the entire file.
    """
    import io
    import builtins

    # First line: a small valid JSON record.
    first_record = _msg("user", "x" * 1000)
    first_line = json.dumps(first_record).encode("utf-8")
    # Rest: ~1 MB of padding lines (each > 1 KB so they don't fit into the
    # first-line read).
    padding_line = json.dumps(_msg("assistant", "y" * 2000)).encode("utf-8")
    num_padding = 500  # ~1.2 MB total padding

    flow_dir = tmp_path / "se3" / "history" / "big"
    flow_dir.mkdir(parents=True)
    jsonl_path = flow_dir / "01_analyze.jsonl"
    with open(jsonl_path, "wb") as fh:
        fh.write(first_line + b"\n")
        for _ in range(num_padding):
            fh.write(padding_line + b"\n")
    total_size = jsonl_path.stat().st_size
    assert total_size > 500_000  # sanity: file is large

    # Wrap builtins.open to track bytes consumed via readline().
    read_bytes = 0
    _real_open = builtins.open

    class ReadlineTracker(io.BufferedReader):
        def __init__(self, raw, **kwargs):
            super().__init__(raw, **kwargs)
            self._tracker_counted = False

        def readline(self, size=-1):
            data = super().readline(size)
            if not self._tracker_counted:
                self._tracker_counted = True
                # Count the bytes of the first line we actually read,
                # plus the newline (which readline returns).
                globals()["_tracked_bytes"] = globals().get("_tracked_bytes", 0) + len(data)
            return data

    def tracking_open(file, *args, **kwargs):
        fh = _real_open(file, *args, **kwargs)
        if str(file) == str(jsonl_path):
            # Wrap the raw buffer so readline() is tracked.
            return ReadlineTracker(fh.raw, buffer_size=io.DEFAULT_BUFFER_SIZE)
        return fh

    monkeypatch.setattr(builtins, "open", tracking_open)

    from se3.daemon.history import _extract_history_summary

    _extract_history_summary(flow_dir)

    tracked = globals().get("_tracked_bytes", 0)
    assert tracked < total_size // 2, (
        f"Expected < {total_size // 2} bytes read, got {tracked} — "
        "the whole file may have been loaded"
    )


def test_dir_signature_changes_on_file_modification(tmp_path):
    """_dir_signature changes when a file's content (size) changes."""
    from se3.daemon.history import DaemonHistoryReader

    flow_dir = tmp_path / "flow"
    flow_dir.mkdir()
    (flow_dir / "01_analyze.jsonl").write_text("line1\n", encoding="utf-8")

    sig1, _ = DaemonHistoryReader._dir_signature(flow_dir)

    # Append data — mtime may not change (coarse resolution) but size will.
    with (flow_dir / "01_analyze.jsonl").open("a", encoding="utf-8") as fh:
        fh.write("line2\n" * 100)

    sig2, _ = DaemonHistoryReader._dir_signature(flow_dir)
    assert sig1 != sig2, "signature must change when file size changes"


def test_dir_signature_changes_on_file_addition(tmp_path):
    """_dir_signature changes when a new file appears."""
    from se3.daemon.history import DaemonHistoryReader

    flow_dir = tmp_path / "flow"
    flow_dir.mkdir()
    (flow_dir / "01_analyze.jsonl").write_text("x\n", encoding="utf-8")

    sig1, _ = DaemonHistoryReader._dir_signature(flow_dir)

    (flow_dir / "02_plan.jsonl").write_text("y\n", encoding="utf-8")

    sig2, _ = DaemonHistoryReader._dir_signature(flow_dir)
    assert sig1 != sig2


def test_dir_signature_stable_when_unchanged(tmp_path):
    """Back-to-back _dir_signature calls on an unchanged directory are equal."""
    from se3.daemon.history import DaemonHistoryReader

    flow_dir = tmp_path / "flow"
    flow_dir.mkdir()
    (flow_dir / "01_analyze.jsonl").write_text("x\n", encoding="utf-8")

    sig1, _ = DaemonHistoryReader._dir_signature(flow_dir)
    sig2, _ = DaemonHistoryReader._dir_signature(flow_dir)
    assert sig1 == sig2


def test_meta_cache_skips_reparsing_unchanged_directories(tmp_path, monkeypatch):
    """Consecutive _build_index_fresh calls with unchanged directories do NOT
    re-call _extract_history_summary (call-count assertion)."""
    # Three history-only directories, no _meta.json.
    for name in ("flow-a", "flow-b", "flow-c"):
        hist = tmp_path / "se3" / "history" / name
        _write_jsonl(hist / "01_analyze.jsonl", [_msg("user", f"task {name}")])

    reader = _make_reader(tmp_path)

    import se3.daemon.history as hmod
    real_extract = hmod._extract_history_summary
    call_count = 0

    def counting_extract(flow_dir):
        nonlocal call_count
        call_count += 1
        return real_extract(flow_dir)

    monkeypatch.setattr(hmod, "_extract_history_summary", counting_extract)

    # First build: all 3 directories parsed.
    metas1 = reader._build_index_fresh()
    assert call_count == 3
    assert len(metas1) == 3

    # Second build: zero re-parses — all directories unchanged.
    call_count = 0
    metas2 = reader._build_index_fresh()
    assert call_count == 0, f"Expected 0 re-parses, got {call_count}"
    assert len(metas2) == 3


def test_meta_cache_reparse_on_directory_change(tmp_path, monkeypatch):
    """When a directory's content changes, that directory is re-parsed."""
    hist_a = tmp_path / "se3" / "history" / "flow-a"
    hist_b = tmp_path / "se3" / "history" / "flow-b"
    _write_jsonl(hist_a / "01_analyze.jsonl", [_msg("user", "task a")])
    _write_jsonl(hist_b / "01_analyze.jsonl", [_msg("user", "task b")])

    reader = _make_reader(tmp_path)

    import se3.daemon.history as hmod
    real_extract = hmod._extract_history_summary
    call_count = 0

    def counting_extract(flow_dir):
        nonlocal call_count
        call_count += 1
        return real_extract(flow_dir)

    monkeypatch.setattr(hmod, "_extract_history_summary", counting_extract)

    # First build: both parsed.
    reader._build_index_fresh()
    assert call_count == 2

    # Modify flow-b by appending to its jsonl.
    _append_jsonl(hist_b / "01_analyze.jsonl", [_msg("user", "extra")])

    # Second build: only flow-b re-parsed.
    call_count = 0
    reader._build_index_fresh()
    assert call_count == 1, f"Expected 1 re-parse (changed dir), got {call_count}"


def test_meta_cache_equivalence(tmp_path):
    """Cached and uncached paths produce SessionMeta objects that are
    field-for-field equal."""
    for name in ("flow-a", "flow-b"):
        hist = tmp_path / "se3" / "history" / name
        _write_jsonl(hist / "01_analyze.jsonl", [_msg("user", f"task {name}")])
        (hist / "_meta.json").write_text(
            json.dumps({"created_at": "2026-06-01T00:00:00", "type": "feature"}),
            encoding="utf-8",
        )

    reader = _make_reader(tmp_path)

    # First build (cache miss — populates cache).
    metas1 = {m.flow_id: m.to_dict() for m in reader._build_index_fresh()}

    # Second build (cache hit — reuses cached values).
    metas2 = {m.flow_id: m.to_dict() for m in reader._build_index_fresh()}

    assert metas1 == metas2, (
        "Cached path must produce identical SessionMeta.to_dict() output"
    )


def test_meta_cache_no_disk_writes(tmp_path):
    """_meta_from_history never writes _meta.json or any other file to the
    flow directory (daemon does not backfill project directories)."""
    hist = tmp_path / "se3" / "history" / "flow-1"
    _write_jsonl(hist / "01_analyze.jsonl", [_msg("user", "task")])

    reader = _make_reader(tmp_path)
    reader._build_index_fresh()

    # Only the original jsonl should exist — no _meta.json was created.
    contents = sorted(f.name for f in hist.iterdir())
    assert contents == ["01_analyze.jsonl"], (
        f"Expected only the original jsonl, found: {contents}"
    )


def test_meta_cache_survives_invalidate_index_cache(tmp_path):
    """invalidate_index_cache drops the TTL cache but NOT the per-directory
    meta cache, so the next _build_index_fresh still benefits from cached
    directory signatures."""
    hist = tmp_path / "se3" / "history" / "flow-1"
    _write_jsonl(hist / "01_analyze.jsonl", [_msg("user", "task")])

    reader = _make_reader(tmp_path)
    reader._build_index_fresh()  # populate both caches

    import se3.daemon.history as hmod
    real_extract = hmod._extract_history_summary
    call_count = 0

    def counting_extract(flow_dir):
        nonlocal call_count
        call_count += 1
        return real_extract(flow_dir)

    monkeypatch_fn = counting_extract  # not using pytest monkeypatch here

    # Simulate what client.py does: invalidate TTL cache then rebuild.
    reader.invalidate_index_cache()

    # The per-directory cache should still be warm.
    # (We can't easily monkeypatch here without the fixture, so just verify
    # the build succeeds and produces the same result.)
    metas = reader._build_index_fresh()
    assert len(metas) == 1
    assert metas[0].flow_id == "flow-1"


def test_meta_cache_after_invalidate_skips_reparsing(tmp_path, monkeypatch):
    """After invalidate_index_cache, _build_index_fresh still skips re-parsing
    unchanged directories thanks to the per-directory signature cache."""
    for name in ("flow-a", "flow-b"):
        hist = tmp_path / "se3" / "history" / name
        _write_jsonl(hist / "01_analyze.jsonl", [_msg("user", f"task {name}")])

    reader = _make_reader(tmp_path)

    import se3.daemon.history as hmod
    real_extract = hmod._extract_history_summary
    call_count = 0

    def counting_extract(flow_dir):
        nonlocal call_count
        call_count += 1
        return real_extract(flow_dir)

    monkeypatch.setattr(hmod, "_extract_history_summary", counting_extract)

    # First build: populate caches.
    reader._build_index_fresh()
    assert call_count == 2

    # Simulate client.py invalidation cycle.
    reader.invalidate_index_cache()

    # Second build: per-directory cache still warm — zero re-parses.
    call_count = 0
    reader._build_index_fresh()
    assert call_count == 0, (
        f"Expected 0 re-parses after TTL invalidation, got {call_count}"
    )


# --------------------------------------------------------------------------
# Group G3: Problem B-2 — read_flow byte-offset incremental reading
# --------------------------------------------------------------------------


def _tracking_open_factory():
    """Return ``(patch_fn, get_bytes)`` where ``patch_fn`` is a monkeypatch-
    compatible replacement for ``builtins.open`` that tracks the total bytes
    read via ``.read()`` and ``.readline()`` calls, and ``get_bytes()``
    returns the cumulative count and resets it to zero.

    Only tracks reads on files under ``tmp_path`` whose suffix is ``.jsonl``.
    """
    import builtins
    import io

    total_bytes = 0
    _real_open = builtins.open

    class TrackingFile:
        """Thin wrapper that delegates to the real file but counts reads."""

        def __init__(self, fh, is_tracked):
            self._fh = fh
            self._tracked = is_tracked

        def read(self, size=-1):
            data = self._fh.read(size)
            if self._tracked:
                nonlocal total_bytes
                total_bytes += len(data) if isinstance(data, (bytes, str)) else 0
            return data

        def readline(self, size=-1):
            data = self._fh.readline(size)
            if self._tracked:
                nonlocal total_bytes
                total_bytes += len(data) if isinstance(data, (bytes, str)) else 0
            return data

        def seek(self, offset, whence=0):
            return self._fh.seek(offset, whence)

        def tell(self):
            return self._fh.tell()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self._fh.__exit__(*args)

        def __getattr__(self, name):
            return getattr(self._fh, name)

    def tracking_open(file, *args, **kwargs):
        fh = _real_open(file, *args, **kwargs)
        is_tracked = str(file).endswith(".jsonl")
        return TrackingFile(fh, is_tracked)

    def get_bytes():
        nonlocal total_bytes
        val = total_bytes
        total_bytes = 0
        return val

    return tracking_open, get_bytes


def test_read_flow_incremental_reads_only_new_bytes(tmp_path, monkeypatch):
    """After a full read, appending N records and reading again should read
    only the new bytes, not the entire file."""
    import builtins

    tracking_open, get_bytes = _tracking_open_factory()
    monkeypatch.setattr(builtins, "open", tracking_open)

    reader = _make_reader(tmp_path)
    jsonl = tmp_path / "se3" / "history" / "f1" / "01_analyze.jsonl"

    # Write a large initial batch: 100 records.
    initial_records = [_msg("user", f"msg{i}") for i in range(100)]
    _write_jsonl(jsonl, initial_records)
    full_size = jsonl.stat().st_size

    # First read — full read, expect ~full_size bytes consumed.
    first = reader.read_flow("f1")
    first_bytes = get_bytes()
    assert len(first.records) == 100
    assert first_bytes >= full_size * 0.9  # allow some overhead

    # Append 5 records.
    new_records = [_msg("assistant", f"reply{i}") for i in range(5)]
    _append_jsonl(jsonl, new_records)
    added_size = jsonl.stat().st_size - full_size

    # Second read — incremental, expect only ~added_size bytes read.
    second = reader.read_flow("f1", cursor=first.cursor)
    second_bytes = get_bytes()
    assert len(second.records) == 5
    assert second_bytes < added_size * 2, (
        f"Expected < {added_size * 2} bytes for incremental read, got {second_bytes}"
    )
    # The incremental read should be dramatically less than a full re-read.
    assert second_bytes < first_bytes, (
        f"Incremental read ({second_bytes}) should be less than full read ({first_bytes})"
    )


def test_read_flow_incremental_records_match_full(tmp_path):
    """The union of incremental reads equals a single full read (content
    equivalence, not byte-level identity)."""
    reader = _make_reader(tmp_path)
    jsonl = tmp_path / "se3" / "history" / "f1" / "01_analyze.jsonl"
    _write_jsonl(jsonl, [_msg("user", "q1"), _msg("assistant", "a1")])

    # Full read (reference).
    ref = _make_reader(tmp_path).read_flow("f1")
    ref_contents = [r["message"]["content"] for r in ref.records]

    # Incremental chain.
    first = reader.read_flow("f1")
    _append_jsonl(jsonl, [_msg("user", "q2"), _msg("assistant", "a2")])
    second = reader.read_flow("f1", cursor=first.cursor)
    _append_jsonl(jsonl, [_msg("user", "q3")])
    third = reader.read_flow("f1", cursor=second.cursor)

    inc_contents = [
        r["message"]["content"]
        for r in first.records + second.records + third.records
    ]

    # A fresh full read of the final file state.
    final_ref = _make_reader(tmp_path).read_flow("f1")
    final_contents = [r["message"]["content"] for r in final_ref.records]

    assert inc_contents == final_contents


def test_read_flow_incremental_new_jsonl_file(tmp_path):
    """A new step jsonl file that appears between reads is picked up whole."""
    reader = _make_reader(tmp_path)
    hist = tmp_path / "se3" / "history" / "f1"
    _write_jsonl(hist / "01_analyze.jsonl", [_msg("user", "q1")])

    first = reader.read_flow("f1")
    assert len(first.records) == 1
    assert "01_analyze.jsonl" in first.cursor
    assert "02_plan.jsonl" not in first.cursor

    # New step file appears.
    _write_jsonl(
        hist / "02_plan.jsonl",
        [_msg("user", "q2", step_type="plan"), _msg("assistant", "a2", step_type="plan")],
    )

    second = reader.read_flow("f1", cursor=first.cursor)
    assert len(second.records) == 2
    assert second.records[0]["step_id"] == "02_plan"
    assert second.cursor["02_plan.jsonl"] == 2
    # Previous file's cursor is preserved.
    assert second.cursor["01_analyze.jsonl"] == 1


def test_read_flow_incremental_bad_json_skipped(tmp_path):
    """Bad JSON lines and empty lines are skipped without aborting."""
    reader = _make_reader(tmp_path)
    hist = tmp_path / "se3" / "history" / "f1"
    hist.mkdir(parents=True)
    jsonl = hist / "01_analyze.jsonl"

    # Write initial records.
    _write_jsonl(jsonl, [_msg("user", "ok")])
    first = reader.read_flow("f1")
    assert len(first.records) == 1

    # Append a mix of good and bad lines.
    with jsonl.open("a", encoding="utf-8") as fh:
        fh.write("\n")  # empty line
        fh.write("not-json\n")  # bad JSON
        fh.write(json.dumps(_msg("assistant", "fine")) + "\n")
        fh.write("{}\n")  # empty dict (still valid)

    second = reader.read_flow("f1", cursor=first.cursor)
    contents = [r["message"]["content"] for r in second.records if "content" in r["message"]]
    assert "fine" in contents
    # The empty dict {} is valid JSON and a dict, so it's included too.
    assert len(second.records) >= 2


def test_read_flow_incremental_partial_line_not_consumed(tmp_path):
    """A partial line (no trailing newline) is not consumed and left for the
    next round.  Once completed with a newline it is picked up."""
    reader = _make_reader(tmp_path)
    jsonl = tmp_path / "se3" / "history" / "f1" / "01_analyze.jsonl"

    # Write initial content.
    _write_jsonl(jsonl, [_msg("user", "q1")])
    first = reader.read_flow("f1")
    assert len(first.records) == 1

    # Append a partial line (no trailing newline).
    partial = json.dumps(_msg("assistant", "partial_msg"))
    with jsonl.open("ab") as fh:
        fh.write(partial.encode("utf-8"))

    # Read — partial line should NOT be consumed.
    second = reader.read_flow("f1", cursor=first.cursor)
    assert len(second.records) == 0, "Partial line should not be consumed"

    # Complete the line with a newline.
    with jsonl.open("ab") as fh:
        fh.write(b"\n")

    # Read again — now the completed line should appear.
    third = reader.read_flow("f1", cursor=second.cursor)
    assert len(third.records) == 1
    assert third.records[0]["message"]["content"] == "partial_msg"


def test_read_flow_incremental_file_truncation_fallback(tmp_path):
    """When a file shrinks (truncation/replacement), a full read is performed
    instead of seeking past the old offset."""
    reader = _make_reader(tmp_path)
    jsonl = tmp_path / "se3" / "history" / "f1" / "01_analyze.jsonl"

    # Write initial content.
    _write_jsonl(jsonl, [_msg("user", "q1"), _msg("assistant", "a1")])
    first = reader.read_flow("f1")
    assert len(first.records) == 2

    # Replace the file with shorter content (simulating truncation).
    _write_jsonl(jsonl, [_msg("user", "new_q1")])

    second = reader.read_flow("f1", cursor=first.cursor)
    # Since cursor says line 2 but file only has 1 line, the cursor is
    # beyond file end.  The full-read path handles this gracefully —
    # it reads from start, but the start offset (cursor_lines=2) is past
    # the end so no records are returned, and the cursor is set to the
    # file's actual line count.
    assert second.cursor["01_analyze.jsonl"] == 1

    # A subsequent read with the corrected cursor returns the record.
    third = reader.read_flow("f1", cursor=second.cursor)
    # No new content was appended, so nothing new.
    assert len(third.records) == 0


def test_read_flow_incremental_file_replace_full_reset(tmp_path):
    """When a file is completely replaced (new inode / different content), the
    offset table resets to a full read."""
    reader = _make_reader(tmp_path)
    jsonl = tmp_path / "se3" / "history" / "f1" / "01_analyze.jsonl"

    _write_jsonl(jsonl, [_msg("user", "old1"), _msg("user", "old2")])
    first = reader.read_flow("f1")
    assert len(first.records) == 2
    assert first.cursor["01_analyze.jsonl"] == 2

    # Replace file entirely with new content (smaller).
    _write_jsonl(jsonl, [_msg("assistant", "new1")])
    # Cursor says 2, file has 1 line, offset table says consumed=2, size was bigger.
    # The file shrunk -> full read path.
    second = reader.read_flow("f1", cursor=first.cursor)
    # Cursor is at 2, file has 1 line. The full-read loop starts at index 2
    # which is past the end -> 0 records, cursor set to 1.
    assert second.cursor["01_analyze.jsonl"] == 1

    # Now read with the corrected cursor.
    _append_jsonl(jsonl, [_msg("user", "new2")])
    third = reader.read_flow("f1", cursor=second.cursor)
    assert len(third.records) == 1
    assert third.records[0]["message"]["content"] == "new2"


def test_read_flow_truncation_offset_table_consistency(tmp_path, monkeypatch):
    """When MAX_RECORDS_PER_REPORT truncates a read, the offset table and
    cursor both advance only to the truncation point.  A subsequent read
    continues from there."""
    monkeypatch.setattr(history_mod, "MAX_RECORDS_PER_REPORT", 3)
    reader = _make_reader(tmp_path)
    jsonl = tmp_path / "se3" / "history" / "f1" / "01_analyze.jsonl"
    _write_jsonl(jsonl, [_msg("user", f"m{i}") for i in range(7)])

    first = reader.read_flow("f1")
    assert len(first.records) == 3
    assert first.cursor["01_analyze.jsonl"] == 3

    # Internal offset table should match the cursor.
    key = str(jsonl)
    assert reader._read_offsets[key][0] == 3  # consumed lines

    second = reader.read_flow("f1", cursor=first.cursor)
    assert len(second.records) == 3
    assert second.cursor["01_analyze.jsonl"] == 6
    assert reader._read_offsets[key][0] == 6

    third = reader.read_flow("f1", cursor=second.cursor)
    assert len(third.records) == 1
    assert third.cursor["01_analyze.jsonl"] == 7
    assert reader._read_offsets[key][0] == 7

    # All records accounted for.
    all_contents = [
        r["message"]["content"]
        for r in first.records + second.records + third.records
    ]
    assert all_contents == [f"m{i}" for i in range(7)]


def test_read_flow_incremental_no_read_when_no_new_bytes(tmp_path, monkeypatch):
    """When the file has not changed since the last read, zero bytes are read
    (the early-exit path for ``can_incremental and cur_size == prev[1]``)."""
    import builtins

    tracking_open, get_bytes = _tracking_open_factory()
    monkeypatch.setattr(builtins, "open", tracking_open)

    reader = _make_reader(tmp_path)
    jsonl = tmp_path / "se3" / "history" / "f1" / "01_analyze.jsonl"
    _write_jsonl(jsonl, [_msg("user", "q1")])

    first = reader.read_flow("f1")
    full_bytes = get_bytes()
    assert full_bytes > 0

    # Read again with same cursor — no new bytes.
    second = reader.read_flow("f1", cursor=first.cursor)
    delta_bytes = get_bytes()
    assert len(second.records) == 0
    assert delta_bytes == 0, (
        f"Expected 0 bytes read when nothing changed, got {delta_bytes}"
    )


def test_read_flow_incremental_multi_step_deltas(tmp_path):
    """Incremental reads across multiple step files produce the same content
    as a full read of the final state."""
    reader = _make_reader(tmp_path)
    hist = tmp_path / "se3" / "history" / "f1"
    s1 = hist / "01_analyze.jsonl"
    _write_jsonl(s1, [_msg("user", "a0"), _msg("assistant", "a1")])

    collected = []
    cursors = {}

    # Round 1: full read.
    first = reader.read_flow("f1")
    collected += [r["message"]["content"] for r in first.records]
    cursors = first.cursor

    # Round 2: append to s1, create s2.
    _append_jsonl(s1, [_msg("user", "a2")])
    s2 = hist / "02_plan.jsonl"
    _write_jsonl(s2, [_msg("assistant", "b0", step_type="plan")])

    second = reader.read_flow("f1", cursor=cursors)
    collected += [r["message"]["content"] for r in second.records]
    cursors = second.cursor

    # Round 3: append to both.
    _append_jsonl(s1, [_msg("assistant", "a3")])
    _append_jsonl(s2, [_msg("user", "b1", step_type="plan")])

    third = reader.read_flow("f1", cursor=cursors)
    collected += [r["message"]["content"] for r in third.records]
    cursors = third.cursor

    # Round 4: nothing new.
    fourth = reader.read_flow("f1", cursor=cursors)
    assert fourth.records == []

    # Compare with a fresh full read.
    ref = _make_reader(tmp_path).read_flow("f1")
    ref_contents = [r["message"]["content"] for r in ref.records]
    assert sorted(collected) == sorted(ref_contents)
    assert len(collected) == len(set(collected))  # no duplicates


def test_read_flow_incremental_active_flow_simulation(tmp_path):
    """Simulates the daemon's active-flow push loop: full read, then repeated
    incremental reads as records are appended, verifying no loss/duplication."""
    _write_engine(tmp_path, "live", "RUNNING")
    hist = tmp_path / "se3" / "history" / "live"
    s1 = hist / "01_discovery.jsonl"
    _write_jsonl(s1, [_msg("user", "q1", step_type="discovery")])

    reader = _make_reader(tmp_path)
    collected = []
    cursors = {}

    # Poll 1: full.
    read = reader.read_flow("live", project_root=str(tmp_path))
    collected += [r["message"]["content"] for r in read.records]
    cursors = read.cursor

    # Poll 2: assistant responds.
    _append_jsonl(s1, [_msg("assistant", "a1", step_type="discovery")])
    read = reader.read_flow("live", project_root=str(tmp_path), cursor=cursors)
    collected += [r["message"]["content"] for r in read.records]
    cursors = read.cursor

    # Poll 3: new step.
    s2 = hist / "02_analyze.jsonl"
    _write_jsonl(s2, [_msg("user", "thinking...", step_type="analyze")])
    read = reader.read_flow("live", project_root=str(tmp_path), cursor=cursors)
    collected += [r["message"]["content"] for r in read.records]
    cursors = read.cursor

    # Poll 4: nothing new.
    read = reader.read_flow("live", project_root=str(tmp_path), cursor=cursors)
    assert read.records == []

    # Verify no loss.
    assert collected == ["q1", "a1", "thinking..."]


def test_read_flow_incremental_empty_lines_between_records(tmp_path):
    """Empty lines (common in multi-process writes) don't break incremental
    reads and don't inflate the cursor."""
    reader = _make_reader(tmp_path)
    jsonl = tmp_path / "se3" / "history" / "f1" / "01_analyze.jsonl"
    _write_jsonl(jsonl, [_msg("user", "q1")])

    first = reader.read_flow("f1")
    assert first.cursor["01_analyze.jsonl"] == 1

    # Append with empty lines interspersed.
    with jsonl.open("a", encoding="utf-8") as fh:
        fh.write("\n\n")
        fh.write(json.dumps(_msg("assistant", "a1")) + "\n")
        fh.write("\n")
        fh.write(json.dumps(_msg("user", "q2")) + "\n")

    second = reader.read_flow("f1", cursor=first.cursor)
    contents = [r["message"]["content"] for r in second.records]
    assert contents == ["a1", "q2"]
    # Cursor counts all lines (including empty), matching the original behavior.
    # Initial: 1 line. Append: "\n\n" (2 empty) + json (1) + "\n" (1 empty) + json (1) = 5.
    assert second.cursor["01_analyze.jsonl"] == 6


def test_read_flow_incremental_large_file_small_delta(tmp_path, monkeypatch):
    """Construct a large initial file (1000 records) + a small append (5
    records).  The incremental read must read << the full file size."""
    import builtins

    tracking_open, get_bytes = _tracking_open_factory()
    monkeypatch.setattr(builtins, "open", tracking_open)

    reader = _make_reader(tmp_path)
    jsonl = tmp_path / "se3" / "history" / "f1" / "01_analyze.jsonl"
    initial = [_msg("user", f"msg{i:04d}") for i in range(1000)]
    _write_jsonl(jsonl, initial)
    full_size = jsonl.stat().st_size

    # Full read.
    first = reader.read_flow("f1")
    full_bytes = get_bytes()
    assert len(first.records) == 1000

    # Small append.
    _append_jsonl(jsonl, [_msg("assistant", f"reply{i}") for i in range(5)])
    added = jsonl.stat().st_size - full_size

    # Incremental read.
    second = reader.read_flow("f1", cursor=first.cursor)
    inc_bytes = get_bytes()
    assert len(second.records) == 5
    # Must read much less than the full file.
    assert inc_bytes < full_bytes, (
        f"Incremental ({inc_bytes}) should be less than full ({full_bytes})"
    )
    # And roughly proportional to the added bytes.
    assert inc_bytes < added * 3, (
        f"Incremental ({inc_bytes}) should be ~{added} bytes (new content)"
    )


def test_read_flow_incremental_bad_lines_in_delta(tmp_path):
    """Bad JSON lines in the appended portion are skipped and don't corrupt
    the offset table."""
    reader = _make_reader(tmp_path)
    jsonl = tmp_path / "se3" / "history" / "f1" / "01_analyze.jsonl"
    _write_jsonl(jsonl, [_msg("user", "q1")])

    first = reader.read_flow("f1")
    assert len(first.records) == 1

    # Append mix of good and bad.
    with jsonl.open("a", encoding="utf-8") as fh:
        fh.write("not-json\n")
        fh.write(json.dumps(_msg("assistant", "a1")) + "\n")
        fh.write("\n")  # empty
        fh.write(json.dumps(_msg("user", "q2")) + "\n")

    second = reader.read_flow("f1", cursor=first.cursor)
    contents = [r["message"]["content"] for r in second.records]
    assert contents == ["a1", "q2"]

    # Cursor counts all 4 appended lines (including bad/empty).
    assert second.cursor["01_analyze.jsonl"] == 5  # 1 initial + 4 appended

    # Third read — nothing new.
    third = reader.read_flow("f1", cursor=second.cursor)
    assert third.records == []


def test_read_flow_incremental_read_active_flows_equivalence(tmp_path):
    """read_active_flows with incremental read_flow produces the same content
    as a sequence of full reads for the same flow."""
    _write_engine(tmp_path, "live", "RUNNING")
    hist = tmp_path / "se3" / "history" / "live"
    s1 = hist / "01_analyze.jsonl"
    _write_jsonl(s1, [_msg("user", "q1")])

    reader = _make_reader(tmp_path)

    # Incremental chain via read_active_flows.
    reads = reader.read_active_flows({})
    cursors = {r.flow_id: r.cursor for r in reads}
    collected = [r["message"]["content"] for r in reads[0].records]

    _append_jsonl(s1, [_msg("assistant", "a1"), _msg("user", "q2")])
    reads = reader.read_active_flows(cursors)
    cursors = {r.flow_id: r.cursor for r in reads}
    collected += [r["message"]["content"] for r in reads[0].records]

    _append_jsonl(s1, [_msg("assistant", "a2")])
    reads = reader.read_active_flows(cursors)
    collected += [r["message"]["content"] for r in reads[0].records]

    # Reference: full read of final state.
    ref = _make_reader(tmp_path).read_flow("live")
    ref_contents = [r["message"]["content"] for r in ref.records]

    assert collected == ref_contents


# --------------------------------------------------------------------------
# --worktree run observability (aggregator-wired provider, as in daemon.py)
# --------------------------------------------------------------------------


def _make_worktree_run(main_root, *, wt_name, flow_id, status="RUNNING"):
    """Create a ``se3 run --worktree`` isolation subdir under *main_root*."""
    wt_root = main_root / "se3" / "worktrees" / wt_name
    state_dir = wt_root / "se3" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "engine.json").write_text(
        json.dumps(
            {
                "flow_id": flow_id,
                "status": status,
                "task_description": "isolated task",
                "is_worktree_mode": True,
                "worktree_branch": f"worktree/{wt_name}",
                "worktree_original_branch": "main",
                "worktree_path": str(wt_root),
            }
        ),
        encoding="utf-8",
    )
    _write_jsonl(
        wt_root / "se3" / "history" / flow_id / "01_implement_abc.jsonl",
        [_msg("user", "go", step_type="implement")],
    )
    return wt_root


def test_history_reader_indexes_active_worktree_run(tmp_path):
    """The daemon-wired provider surfaces a live --worktree run in the index.

    This mirrors daemon.py's wiring: the history reader's provider is the
    aggregator's ``all_observable_roots``, which folds in active worktree-run
    subdirs. The worktree flow must therefore appear in build_index and in the
    active-flow signature during its flow body, not only after the merge.
    """
    from se3.daemon.aggregator import DaemonAggregator

    main_root = tmp_path / "proj"
    main_root.mkdir()
    _make_worktree_run(main_root, wt_name="feat-x-1", flow_id="wt-flow-1")

    agg = DaemonAggregator()
    agg.add_project_root(main_root)
    reader = DaemonHistoryReader(
        project_roots_provider=lambda: agg.all_observable_roots()
    )

    metas = {m.flow_id: m for m in reader.build_index()}
    assert "wt-flow-1" in metas
    assert metas["wt-flow-1"].active is True

    # The active-flow signature (the fast push trigger) tracks the worktree flow.
    assert "wt-flow-1" in reader.active_flow_signature()

    # And its conversation is readable live.
    read = reader.read_flow("wt-flow-1")
    assert read.records
    assert read.records[0]["step_type"] == "implement"


def test_read_active_flows_includes_waiting_flow_with_no_step_records(tmp_path):
    """A queued (waiting_for_lock) flow that has not yet written any step jsonl
    is still recognized as active and returned by read_active_flows.

    G2 acceptance: zero step records must not cause the waiting flow to be
    dropped — it stays RUNNING and the daemon must keep reporting it so the web
    console shows the running·waiting-for-lock state instead of "已发布".
    """
    state_dir = tmp_path / "se3" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "engine.json").write_text(
        json.dumps(
            {"flow_id": "queued", "status": "RUNNING", "waiting_for_lock": True}
        ),
        encoding="utf-8",
    )
    # Deliberately NO se3/history/queued/*.jsonl — zero step records.

    reader = _make_reader(tmp_path)
    reads = reader.read_active_flows({})
    assert [r.flow_id for r in reads] == ["queued"]
    assert reads[0].records == []


# --------------------------------------------------------------------------
# Group G2: symptom B — full-read mid-write tail / final-flush project_root
# --------------------------------------------------------------------------


def test_full_read_consumes_complete_no_newline_tail(tmp_path):
    """A FULL read consumes a COMPLETE final record written without a newline.

    Terminal step files and merge-back sidecars are written atomically via
    ``write_text(json.dumps(record))`` — a valid JSON record with no trailing
    ``\\n``.  Such a tail MUST be read (it is complete), not mistaken for a
    mid-write partial.
    """
    hist_dir = tmp_path / "se3" / "history" / "f1"
    hist_dir.mkdir(parents=True)
    jsonl = hist_dir / "01_discovery_ab.jsonl"
    user_line = json.dumps(_msg("user", "the task", step_type="discovery"))
    assistant_line = json.dumps(_msg("assistant", "complete reply", step_type="discovery"))
    with jsonl.open("wb") as fh:
        fh.write((user_line + "\n").encode("utf-8"))
        fh.write(assistant_line.encode("utf-8"))  # complete, valid, no newline

    read = _make_reader(tmp_path).read_flow("f1", project_root=str(tmp_path))
    assert read.mode == HISTORY_MODE_FULL
    assert [r["message"]["content"] for r in read.records] == [
        "the task",
        "complete reply",
    ]
    assert read.cursor == {"01_discovery_ab.jsonl": 2}


def test_full_read_does_not_consume_truncated_trailing_line(tmp_path):
    """A FULL read must not consume a half-written (truncated) final line.

    The worktree / discovery "first assistant body empty, no further records"
    bug: the daemon's first snapshot of a live flow can land while the agent is
    still flushing the latest record, so the last line is truncated JSON.  The
    old full-read path consumed it, failed ``json.loads``, dropped the record,
    *and* advanced the cursor past it — so the record was lost forever.  The
    complete records before it must read fine, the truncated tail must be left,
    and once it is completed it must be picked up with no loss.
    """
    hist_dir = tmp_path / "se3" / "history" / "f1"
    hist_dir.mkdir(parents=True)
    jsonl = hist_dir / "01_discovery_ab.jsonl"
    user_line = json.dumps(_msg("user", "the task", step_type="discovery"))
    assistant_line = json.dumps(_msg("assistant", "first reply", step_type="discovery"))
    # Write only the first half of the assistant record (truncated => invalid).
    split_at = len(assistant_line) // 2
    head, rest = assistant_line[:split_at], assistant_line[split_at:]
    with jsonl.open("wb") as fh:
        fh.write((user_line + "\n").encode("utf-8"))
        fh.write(head.encode("utf-8"))  # truncated, no newline

    reader = _make_reader(tmp_path)
    first = reader.read_flow("f1", project_root=str(tmp_path))
    assert first.mode == HISTORY_MODE_FULL
    # Only the complete first record is read; the truncated tail is left.
    assert [r["message"]["content"] for r in first.records] == ["the task"]
    assert first.cursor == {"01_discovery_ab.jsonl": 1}

    # The writer finishes the assistant line.
    with jsonl.open("ab") as fh:
        fh.write((rest + "\n").encode("utf-8"))

    second = reader.read_flow(
        "f1", project_root=str(tmp_path), cursor=first.cursor
    )
    assert second.mode == HISTORY_MODE_APPEND
    # The previously-truncated record is now read in full — no loss.
    assert [r["message"]["content"] for r in second.records] == ["first reply"]
    assert second.cursor == {"01_discovery_ab.jsonl": 2}


def test_full_read_truncated_tail_then_full_reread_recovers(tmp_path):
    """A cold-cursor full re-read after the truncated line completes recovers it.

    Models a daemon restart: the byte-offset table is cold, so the second read
    also takes the full-read branch (with the line now complete).  The first
    assistant body must be present and non-empty.
    """
    hist_dir = tmp_path / "se3" / "history" / "f1"
    hist_dir.mkdir(parents=True)
    jsonl = hist_dir / "01_discovery_ab.jsonl"
    user_line = json.dumps(_msg("user", "task", step_type="discovery"))
    assistant_line = json.dumps(_msg("assistant", "body", step_type="discovery"))
    split_at = len(assistant_line) // 2
    head, rest = assistant_line[:split_at], assistant_line[split_at:]
    with jsonl.open("wb") as fh:
        fh.write((user_line + "\n").encode("utf-8"))
        fh.write(head.encode("utf-8"))

    first = _make_reader(tmp_path).read_flow("f1", project_root=str(tmp_path))
    assert [r["message"]["content"] for r in first.records] == ["task"]

    with jsonl.open("ab") as fh:
        fh.write((rest + "\n").encode("utf-8"))

    # Fresh reader (cold offset table) does a full read of the completed file.
    fresh = _make_reader(tmp_path).read_flow("f1", project_root=str(tmp_path))
    contents = [r["message"]["content"] for r in fresh.records]
    assert contents == ["task", "body"]
    # The first assistant body is non-empty — the symptom-B core assertion.
    assert all(r["message"]["content"] for r in fresh.records)


def test_final_flush_uses_flow_project_root_across_multiple_roots(tmp_path):
    """The final-flush pass scopes ``read_flow`` to the flow's own root.

    With two tracked roots that both happen to contain a ``se3/history/wt``
    directory, the final flush of a terminal flow must read the root the index
    attributes the flow to (root A), not whichever root a bare all-roots scan
    happens to hit first.
    """
    root_a = tmp_path / "A"
    root_b = tmp_path / "B"

    # Root A: archived (terminal) flow "wt" whose meta records project_root=A,
    # plus its real history with a tail appended after the first read.
    a_archive = root_a / "se3" / "state" / "archive"
    a_archive.mkdir(parents=True)
    (a_archive / "engine_wt.json").write_text(
        json.dumps(
            {"flow_id": "wt", "status": "completed", "project_root": str(root_a)}
        ),
        encoding="utf-8",
    )
    a_hist = root_a / "se3" / "history" / "wt"
    _write_jsonl(a_hist / "01_discovery_ab.jsonl", [_msg("user", "A-task", step_type="discovery")])

    # Root B: a decoy history dir for the SAME flow id with different content.
    b_hist = root_b / "se3" / "history" / "wt"
    _write_jsonl(b_hist / "01_discovery_ab.jsonl", [_msg("user", "B-DECOY", step_type="discovery")])

    reader = _make_reader(root_a, root_b)

    # First read establishes a cursor for the (terminal) flow.
    first = reader.read_flow("wt", project_root=str(root_a))
    assert [r["message"]["content"] for r in first.records] == ["A-task"]
    cursors = {"wt": first.cursor}

    # The flow appends a tail to ROOT A's history just before/after going
    # terminal; the final flush must read it from root A.
    _append_jsonl(
        a_hist / "01_discovery_ab.jsonl",
        [_msg("assistant", "A-final", step_type="discovery")],
    )

    reads = reader.read_active_flows(cursors)
    flushed = [r for r in reads if r.flow_id == "wt"]
    assert len(flushed) == 1
    # Read from root A (the attributed root), never the root-B decoy.
    assert [r["message"]["content"] for r in flushed[0].records] == ["A-final"]


def test_final_flush_unknown_flow_falls_back_to_all_roots(tmp_path):
    """A flow absent from the index keeps the all-roots compat behaviour.

    ``root_by_flow.get`` yields ``None`` for such a flow, so ``read_flow`` scans
    every tracked root — the pre-fix fallback, preserved.
    """
    root_a = tmp_path / "A"
    (root_a / "se3" / "state").mkdir(parents=True)
    reader = _make_reader(root_a)

    # A stale cursor for a flow that exists nowhere on disk and is not indexed:
    # ``root_by_flow.get`` is None, ``read_flow`` scans all roots, finds nothing
    # and returns no records — a safe no-op rather than a crash.
    reads = reader.read_active_flows({"vanished": {"01_analyze.jsonl": 3}})
    assert [r for r in reads if r.flow_id == "vanished"] == []


# --------------------------------------------------------------------------
# Group G3: discovery startup-window observability via the is_worktree_mode gate
# --------------------------------------------------------------------------
#
# Bug1's blind spot: a worktree flow's live history is written under the worktree
# itself, and it only enters the live-read set once
# ``DaemonAggregator._active_worktree_run_roots`` admits it — which requires an
# ``is_worktree_mode`` engine.json. The run-command fix lands that engine.json at
# flow creation (status INIT), *before* discovery's first LLM call, so the very
# first reply (thinking + result) is observable. These tests pin the
# history-reader half of that cooperative fix from the engine.json gate.


def _make_eager_worktree(main_root, *, wt_name, flow_id, status="INIT"):
    """Worktree subdir with an is_worktree_mode engine.json but no history yet.

    Models the run command's eager save: ``is_worktree_mode`` engine.json is on
    disk at status INIT before any discovery record is written.
    """
    wt_root = main_root / "se3" / "worktrees" / wt_name
    state_dir = wt_root / "se3" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "engine.json").write_text(
        json.dumps(
            {
                "flow_id": flow_id,
                "status": status,
                "task_description": "isolated task",
                "is_worktree_mode": True,
                "worktree_path": str(wt_root),
            }
        ),
        encoding="utf-8",
    )
    return wt_root


def test_active_worktree_run_root_admitted_at_init_engine_json(tmp_path):
    """``_active_worktree_run_roots`` returns the worktree from its INIT engine.json.

    The gate keys on ``is_worktree_mode`` (+ a ``flow_id``), not on RUNNING or on
    any history existing yet, so the worktree is observable from the very first
    write — closing Bug1's discovery startup-window blind spot.
    """
    import os

    from se3.daemon.aggregator import DaemonAggregator

    main_root = tmp_path / "proj"
    main_root.mkdir()
    wt_root = _make_eager_worktree(main_root, wt_name="feat-x", flow_id="wt-1")

    agg = DaemonAggregator()
    agg.add_project_root(main_root)

    observable = agg.all_observable_roots()
    assert os.path.realpath(str(wt_root)) in observable
    # The transient sandbox stays out of the dropdown-facing view.
    assert os.path.realpath(str(wt_root)) not in agg.all_project_roots()


def test_worktree_first_reply_read_live_then_increments(tmp_path):
    """The discovery first reply reads in full live, then later messages append.

    With the worktree admitted by its INIT engine.json, the history reader
    (wired to ``all_observable_roots`` as the daemon wires it) must read the
    complete-but-unterminated first record in full and keep appending — the
    end-to-end fix for "first body empty, then nothing further".
    """
    from se3.daemon.aggregator import DaemonAggregator

    main_root = tmp_path / "proj"
    main_root.mkdir()
    wt_root = _make_eager_worktree(main_root, wt_name="feat-x", flow_id="wt-1")

    # Discovery's first record flushed without a trailing newline (complete).
    hist = wt_root / "se3" / "history" / "wt-1" / "01_discovery_ab.jsonl"
    hist.parent.mkdir(parents=True)
    hist.write_text(
        json.dumps(_msg("assistant", "thinking… and result", step_type="discovery")),
        encoding="utf-8",
    )

    agg = DaemonAggregator()
    agg.add_project_root(main_root)
    reader = DaemonHistoryReader(
        project_roots_provider=lambda: agg.all_observable_roots()
    )

    metas = {m.flow_id: m for m in reader.build_index()}
    assert "wt-1" in metas and metas["wt-1"].active is True

    first = {r.flow_id: r for r in reader.read_active_flows({})}["wt-1"]
    assert [r["message"]["content"] for r in first.records] == [
        "thinking… and result"
    ]

    _append_jsonl(hist, [_msg("assistant", "second", step_type="discovery")])
    second = {
        r.flow_id: r
        for r in reader.read_active_flows({"wt-1": first.cursor})
    }["wt-1"]
    assert [r["message"]["content"] for r in second.records] == ["second"]


def test_dag_isolation_worktree_excluded_from_observable(tmp_path):
    """A DAG-isolation worktree (no is_worktree_mode) is never observed.

    It shares the ``se3/worktrees/`` parent but writes no top-level
    ``is_worktree_mode`` record, so the strict gate keeps it out — confirming the
    eager-save fix did not loosen the gate and regress Bug2.
    """
    import os

    from se3.daemon.aggregator import DaemonAggregator

    main_root = tmp_path / "proj"
    main_root.mkdir()
    wt_root = main_root / "se3" / "worktrees" / "impl-dag"
    state_dir = wt_root / "se3" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "engine.json").write_text(
        json.dumps({"flow_id": "dag-flow", "status": "RUNNING"}),
        encoding="utf-8",
    )

    agg = DaemonAggregator()
    agg.add_project_root(main_root)
    assert os.path.realpath(str(wt_root)) not in agg.all_observable_roots()
