"""Tests for ``--worktree`` history sidecar merging in the daemon reader.

When a ``se3 run --worktree`` flow's per-step history is folded back into the
main project by ``se3 merge``'s runtime sync, a colliding per-step ``jsonl``
lands as a ``*.jsonl.from-<branch>`` *sidecar* file (Tier A lenient collision
policy, see the ``se3 merge`` *Runtime Data Synchronization* requirement). The
daemon history reader previously enumerated only ``*.jsonl`` via ``glob``, so
those sidecars — and therefore everything after a worktree session's first
record — were never read or pushed.

These tests pin the fix: :class:`DaemonHistoryReader` now reads the primary
file and its sidecars with no loss / no duplication / correct ordering, advances
its incremental cursor correctly, and parses the step type with the sidecar
suffix stripped.

Design group G1 further hardens the *identity* the reader emits: each physical
file (the primary and every sidecar) gets its OWN frontend-facing step id that
KEEPS the ``.from-<branch>`` marker (:func:`_display_step_id`), so their per-file
ordinals no longer collide at ``step_id#ordinal`` and the frontend renders every
stream. The step *type* is still parsed from the folded logical id.
"""

from __future__ import annotations

import json

from tianluo.daemon.history import (
    DaemonHistoryReader,
    _count_jsonl,
    _display_step_id,
    _iter_history_jsonl,
    _logical_step_id,
    parse_step_type_from_step_id,
)
from tianluo.daemon.protocol import HISTORY_MODE_APPEND, HISTORY_MODE_FULL


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


def _msg(role, content):
    return {"role": role, "content": content}


def _make_reader(*roots):
    return DaemonHistoryReader(project_roots_provider=lambda: list(roots))


def _flow_dir(root, flow_id):
    d = root / "se3" / "history" / flow_id
    d.mkdir(parents=True, exist_ok=True)
    return d


# --------------------------------------------------------------------------
# pure helpers
# --------------------------------------------------------------------------


def test_logical_step_id_strips_sidecar_suffix():
    assert _logical_step_id("01_discovery_ab12.jsonl") == "01_discovery_ab12"
    assert (
        _logical_step_id("01_discovery_ab12.jsonl.from-worktree__b")
        == "01_discovery_ab12"
    )
    assert (
        _logical_step_id("01_discovery_ab12.jsonl.from-worktree__b.0a1b2c3d")
        == "01_discovery_ab12"
    )
    # A name without ``.jsonl`` is returned unchanged.
    assert _logical_step_id("weird_name") == "weird_name"


def test_display_step_id_keeps_sidecar_marker():
    """The frontend-facing id keeps the sidecar marker so streams stay distinct.

    Unlike the logical id (which folds primary + sidecars to one id), the display
    id keeps the ``.from-<branch>`` marker so a step's primary file and each of
    its sidecars form DISTINCT ``step_id#ordinal`` namespaces and the frontend
    never drops the second file's records as ordinal-collision duplicates.
    """
    # A non-sidecar name is identical to its logical id (common single-file case).
    assert _display_step_id("01_discovery_ab12.jsonl") == "01_discovery_ab12"
    # A sidecar keeps its branch marker → distinct from the primary.
    assert (
        _display_step_id("01_discovery_ab12.jsonl.from-worktree__b")
        == "01_discovery_ab12.from-worktree__b"
    )
    # A hash-disambiguated sidecar keeps the whole marker → distinct again.
    assert (
        _display_step_id("01_discovery_ab12.jsonl.from-worktree__b.0a1b2c3d")
        == "01_discovery_ab12.from-worktree__b.0a1b2c3d"
    )
    # A name without ``.jsonl`` is returned unchanged.
    assert _display_step_id("weird_name") == "weird_name"


def test_parse_step_type_strips_sidecar_suffix():
    # Logical step id (the normal caller input) still works.
    assert parse_step_type_from_step_id("01_discovery_ab12") == "discovery"
    # A raw primary / sidecar file name is reduced to the logical id first.
    assert parse_step_type_from_step_id("01_discovery_ab12.jsonl") == "discovery"
    assert (
        parse_step_type_from_step_id("01_discovery_ab12.jsonl.from-worktree__b")
        == "discovery"
    )
    assert (
        parse_step_type_from_step_id(
            "13_version_analyze_def456.jsonl.from-worktree__feat__x"
        )
        == "version_analyze"
    )


def test_iter_history_jsonl_includes_sidecars_sorted(tmp_path):
    flow_dir = _flow_dir(tmp_path, "f1")
    _write_jsonl(flow_dir / "01_discovery_aa.jsonl", [_msg("user", "hi")])
    _write_jsonl(
        flow_dir / "01_discovery_aa.jsonl.from-worktree__b", [_msg("assistant", "yo")]
    )
    _write_jsonl(flow_dir / "02_analyze_bb.jsonl", [_msg("assistant", "x")])

    names = [p.name for p in _iter_history_jsonl(flow_dir)]
    assert names == [
        "01_discovery_aa.jsonl",
        "01_discovery_aa.jsonl.from-worktree__b",
        "02_analyze_bb.jsonl",
    ]


def test_count_jsonl_counts_logical_steps(tmp_path):
    flow_dir = _flow_dir(tmp_path, "f1")
    # Step 01 has a primary + a sidecar; step 02 is sidecar-only.
    _write_jsonl(flow_dir / "01_discovery_aa.jsonl", [_msg("user", "hi")])
    _write_jsonl(
        flow_dir / "01_discovery_aa.jsonl.from-worktree__b", [_msg("assistant", "yo")]
    )
    _write_jsonl(
        flow_dir / "02_analyze_bb.jsonl.from-worktree__b", [_msg("assistant", "x")]
    )
    # 3 physical files, 2 logical steps.
    assert _count_jsonl(flow_dir) == 2


# --------------------------------------------------------------------------
# read_flow merging
# --------------------------------------------------------------------------


def test_read_flow_merges_primary_and_sidecar(tmp_path):
    """Primary + sidecar records all read, in order, no loss/dup.

    The primary and its sidecar now carry DISTINCT display step ids (the sidecar
    keeps its ``.from-<branch>`` marker), so their per-file ordinals — both
    starting at 0 — no longer collide at ``step_id#ordinal``.
    """
    flow_dir = _flow_dir(tmp_path, "wt-1")
    # Primary file: the single first record that landed in the main session.
    _write_jsonl(
        flow_dir / "01_discovery_ab12.jsonl",
        [_msg("user", "the task")],
    )
    # Sidecar: the worktree's records, folded back as a collision sidecar.
    _write_jsonl(
        flow_dir / "01_discovery_ab12.jsonl.from-worktree__b",
        [_msg("assistant", "clarifying question"), _msg("user", "an answer")],
    )

    reader = _make_reader(tmp_path)
    read = reader.read_flow("wt-1", project_root=str(tmp_path))

    assert read.mode == HISTORY_MODE_FULL
    # All three records present, none lost, none duplicated.
    contents = [r["message"]["content"] for r in read.records]
    assert contents == ["the task", "clarifying question", "an answer"]
    # Primary and sidecar emit DISTINCT display step ids so their ordinal-0
    # records do not collide; the step type is still the folded logical type.
    assert {r["step_id"] for r in read.records} == {
        "01_discovery_ab12",
        "01_discovery_ab12.from-worktree__b",
    }
    assert {r["step_type"] for r in read.records} == {"discovery"}

    # (step_id, ordinal) is globally unique across the two physical files even
    # though each file numbers its own lines from 0.
    keys = [(r["step_id"], r["ordinal"]) for r in read.records]
    assert len(keys) == len(set(keys))

    # Cursor is keyed by the *physical* file name (primary + sidecar separately).
    assert read.cursor["01_discovery_ab12.jsonl"] == 1
    assert read.cursor["01_discovery_ab12.jsonl.from-worktree__b"] == 2


def test_read_flow_sidecar_only_step(tmp_path):
    """A step whose records live only in a sidecar is still fully read."""
    flow_dir = _flow_dir(tmp_path, "wt-2")
    _write_jsonl(
        flow_dir / "02_analyze_cc.jsonl.from-worktree__b",
        [_msg("assistant", "analysis one"), _msg("assistant", "analysis two")],
    )

    reader = _make_reader(tmp_path)
    read = reader.read_flow("wt-2", project_root=str(tmp_path))

    contents = [r["message"]["content"] for r in read.records]
    assert contents == ["analysis one", "analysis two"]
    # Sidecar-only step: its id keeps the branch marker; type still parsed folded.
    assert all(
        r["step_id"] == "02_analyze_cc.from-worktree__b" for r in read.records
    )
    assert all(r["step_type"] == "analyze" for r in read.records)


def test_read_flow_incremental_cursor_advances_over_sidecar(tmp_path):
    """Appending to a sidecar yields only the new records on the next read."""
    flow_dir = _flow_dir(tmp_path, "wt-3")
    primary = flow_dir / "01_discovery_ab12.jsonl"
    sidecar = flow_dir / "01_discovery_ab12.jsonl.from-worktree__b"
    _write_jsonl(primary, [_msg("user", "the task")])
    _write_jsonl(sidecar, [_msg("assistant", "first reply")])

    reader = _make_reader(tmp_path)
    first = reader.read_flow("wt-3", project_root=str(tmp_path))
    assert [r["message"]["content"] for r in first.records] == [
        "the task",
        "first reply",
    ]

    # The worktree appends two more records to its (sidecar) history.
    _append_jsonl(
        sidecar, [_msg("user", "follow up"), _msg("assistant", "second reply")]
    )

    second = reader.read_flow(
        "wt-3", project_root=str(tmp_path), cursor=first.cursor
    )
    assert second.mode == HISTORY_MODE_APPEND
    # Only the two newly appended records, no re-read of earlier ones.
    assert [r["message"]["content"] for r in second.records] == [
        "follow up",
        "second reply",
    ]
    assert all(
        r["step_id"] == "01_discovery_ab12.from-worktree__b" for r in second.records
    )
    assert second.cursor["01_discovery_ab12.jsonl.from-worktree__b"] == 3
    assert second.cursor["01_discovery_ab12.jsonl"] == 1

    # A third read with the advanced cursor and no further writes is empty.
    third = reader.read_flow(
        "wt-3", project_root=str(tmp_path), cursor=second.cursor
    )
    assert third.records == []


def test_read_flow_multiple_sidecars_for_one_step(tmp_path):
    """Several sidecars (hash-disambiguated) for one step all read in order.

    Each physical file — primary and both sidecars — emits a distinct display
    step id, so their ordinal-0 records never collide.
    """
    flow_dir = _flow_dir(tmp_path, "wt-4")
    _write_jsonl(
        flow_dir / "01_discovery_ab12.jsonl", [_msg("user", "task")]
    )
    _write_jsonl(
        flow_dir / "01_discovery_ab12.jsonl.from-worktree__b",
        [_msg("assistant", "branch b reply")],
    )
    _write_jsonl(
        flow_dir / "01_discovery_ab12.jsonl.from-worktree__b.0a1b2c3d",
        [_msg("assistant", "branch b reply (hash)")],
    )

    reader = _make_reader(tmp_path)
    read = reader.read_flow("wt-4", project_root=str(tmp_path))

    contents = [r["message"]["content"] for r in read.records]
    assert contents == [
        "task",
        "branch b reply",
        "branch b reply (hash)",
    ]
    assert {r["step_id"] for r in read.records} == {
        "01_discovery_ab12",
        "01_discovery_ab12.from-worktree__b",
        "01_discovery_ab12.from-worktree__b.0a1b2c3d",
    }
    assert {r["step_type"] for r in read.records} == {"discovery"}
    # (step_id, ordinal) unique across all three physical files.
    keys = [(r["step_id"], r["ordinal"]) for r in read.records]
    assert len(keys) == len(set(keys))
    # Each physical file tracked independently in the cursor.
    assert read.cursor["01_discovery_ab12.jsonl"] == 1
    assert read.cursor["01_discovery_ab12.jsonl.from-worktree__b"] == 1
    assert read.cursor["01_discovery_ab12.jsonl.from-worktree__b.0a1b2c3d"] == 1


def test_read_flow_first_record_nonempty_with_midwrite_sidecar_tail(tmp_path):
    """Symptom B: the first assistant body must read non-empty even when the
    first snapshot lands while the worktree sidecar's tail is mid-write.

    The primary file holds the user task; the worktree sidecar's first
    assistant record is written only halfway (truncated JSON, no terminating
    newline — streaming in progress).  The full read must surface the user
    record and leave the half-written assistant record for the next round —
    never consuming it half-formed, dropping it, and advancing the cursor past
    it.  Once the sidecar line is completed it must be read in full.
    """
    flow_dir = _flow_dir(tmp_path, "wt-mid")
    primary = flow_dir / "01_discovery_ab12.jsonl"
    sidecar = flow_dir / "01_discovery_ab12.jsonl.from-worktree__b"
    _write_jsonl(primary, [_msg("user", "the task")])
    # Sidecar: first assistant record present only halfway (truncated/invalid).
    full = json.dumps(_msg("assistant", "first reply body"))
    split_at = len(full) // 2
    head, rest = full[:split_at], full[split_at:]
    with sidecar.open("wb") as fh:
        fh.write(head.encode("utf-8"))

    reader = _make_reader(tmp_path)
    first = reader.read_flow("wt-mid", project_root=str(tmp_path))
    assert first.mode == HISTORY_MODE_FULL
    # The complete primary record reads; the truncated sidecar line is held.
    assert [r["message"]["content"] for r in first.records] == ["the task"]
    assert first.cursor["01_discovery_ab12.jsonl"] == 1
    assert first.cursor.get("01_discovery_ab12.jsonl.from-worktree__b", 0) == 0

    # The worktree finishes writing the assistant line.
    with sidecar.open("ab") as fh:
        fh.write((rest + "\n").encode("utf-8"))

    second = reader.read_flow(
        "wt-mid", project_root=str(tmp_path), cursor=first.cursor
    )
    # The first assistant body is now read in full — non-empty, no loss.
    assert [r["message"]["content"] for r in second.records] == ["first reply body"]
    assert all(r["message"]["content"] for r in second.records)
    assert second.cursor["01_discovery_ab12.jsonl.from-worktree__b"] == 1
