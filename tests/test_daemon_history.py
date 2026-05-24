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


def test_session_meta_to_dict_round_trip():
    meta = SessionMeta(flow_id="x", project_root="/p", active=True)
    data = meta.to_dict()
    assert data["flow_id"] == "x"
    assert data["active"] is True
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
    }


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
