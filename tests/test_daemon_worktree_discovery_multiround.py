"""Regression tests for multi-round ``--worktree`` discovery record identity.

The reported bug: in a ``se3 run --worktree`` flow the discovery step's chat
records after the FIRST round vanished from the web console — only the first
snapshot rendered and everything the agent said in later rounds was reachable
only through the adjudication "show details" affordance, never as live chat.

``run_worktree_mode`` forks the worktree first and runs discovery ENTIRELY in
the worktree (``run_flow(project_root=<worktree>)``), so a single worktree
discovery file holds every round (append-only). The loss happened in the
daemon read path, where several physical files backing one logical step were
folded under ONE ``step_id`` while each file numbered its own lines from 0, so
their ``step_id#ordinal`` keys collided and the frontend dropped the second
file's records as duplicates; and where a mid-flow switch of which physical
copy backed the step desynced the bare-filename wire cursor from the
absolute-path offset table, skipping the later rounds.

Design group G1 fixes both:

* :func:`_display_step_id` gives each physical file its OWN frontend-facing id
  (keeping the ``.from-<branch>`` sidecar marker), so ``step_id#ordinal`` is
  globally unique and stable across full/append reads.
* :meth:`DaemonHistoryReader._merge_flow_jsonl` prefers the worktree write-root
  copy over a transiently-larger main copy, so the selection does not flip.
* :meth:`DaemonHistoryReader.read_flow` detects a physical-copy switch for a
  bare filename and reads the new copy cleanly from line 0 rather than trusting
  the other copy's by-name cursor.
"""

from __future__ import annotations

import json

from tianluo.daemon.history import DaemonHistoryReader, _cross_root_step_key
from tianluo.daemon.protocol import HISTORY_MODE_APPEND, HISTORY_MODE_FULL

# ``_merge_flow_jsonl`` is a staticmethod on the reader; alias it for the copy-
# selection assertions below.
_merge = DaemonHistoryReader._merge_flow_jsonl


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _write_jsonl(path, lines):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )


def _append_jsonl(path, lines):
    with path.open("a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")


def _msg(role, content):
    return {"role": role, "content": content}


def _make_reader(*roots):
    return DaemonHistoryReader(project_roots_provider=lambda: [str(r) for r in roots])


def _flow_dir(root, flow_id):
    d = root / "tianluo" / "history" / flow_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _make_worktree(main_root, name="wt__b"):
    (main_root / "tianluo").mkdir(parents=True, exist_ok=True)
    wt = main_root / "tianluo" / "worktrees" / name
    wt.mkdir(parents=True, exist_ok=True)
    return wt


def _keys(records):
    """The ``(step_id, ordinal)`` identity the frontend reconciles by."""
    return [(r["step_id"], r["ordinal"]) for r in records]


# --------------------------------------------------------------------------
# live worktree discovery: every round reaches the frontend
# --------------------------------------------------------------------------


def test_live_worktree_discovery_all_rounds_unique_and_stable(tmp_path):
    """A single worktree discovery file's rounds all read with unique, stable ids.

    Reads the file across a full snapshot and two incremental appends (the way
    the daemon push loop reads a growing live file) and asserts every round is
    delivered exactly once, every ``(step_id, ordinal)`` pair is unique, and a
    line's identity is identical whether it arrived via a full or an append read.
    """
    main = tmp_path / "main"
    wt = _make_worktree(main)
    flow_id = "wt-live"
    wt_flow = _flow_dir(wt, flow_id)
    disc = wt_flow / "01_discovery_ab.jsonl"

    # Round 1 (the pre-adjudication opening exchange).
    _write_jsonl(disc, [_msg("user", "the task"), _msg("assistant", "round 1 reply")])

    reader = _make_reader(main, wt)
    first = reader.read_flow(flow_id, project_root=str(wt))
    assert first.mode == HISTORY_MODE_FULL
    assert [r["message"]["content"] for r in first.records] == [
        "the task",
        "round 1 reply",
    ]
    seen = list(first.records)

    # Round 2 appended to the SAME worktree file.
    _append_jsonl(disc, [_msg("user", "round 2 answer"), _msg("assistant", "round 2 reply")])
    second = reader.read_flow(flow_id, project_root=str(wt), cursor=first.cursor)
    assert second.mode == HISTORY_MODE_APPEND
    # The later round is NOT dropped as a duplicate — this is the core bug.
    assert [r["message"]["content"] for r in second.records] == [
        "round 2 answer",
        "round 2 reply",
    ]
    seen += list(second.records)

    # Round 3 appended.
    _append_jsonl(disc, [_msg("assistant", "round 3 reply")])
    third = reader.read_flow(flow_id, project_root=str(wt), cursor=second.cursor)
    assert [r["message"]["content"] for r in third.records] == ["round 3 reply"]
    seen += list(third.records)

    # Every round present exactly once, all ids under the one discovery stream.
    assert [r["message"]["content"] for r in seen] == [
        "the task",
        "round 1 reply",
        "round 2 answer",
        "round 2 reply",
        "round 3 reply",
    ]
    keys = _keys(seen)
    assert keys == [
        ("01_discovery_ab", 0),
        ("01_discovery_ab", 1),
        ("01_discovery_ab", 2),
        ("01_discovery_ab", 3),
        ("01_discovery_ab", 4),
    ]
    assert len(keys) == len(set(keys))  # globally unique


def test_full_read_reproduces_append_ordinals(tmp_path):
    """A later full snapshot tags each line with the SAME id an append gave it."""
    main = tmp_path / "main"
    wt = _make_worktree(main)
    flow_id = "wt-fullappend"
    wt_flow = _flow_dir(wt, flow_id)
    disc = wt_flow / "01_discovery_ab.jsonl"
    _write_jsonl(disc, [_msg("user", "task")])

    reader = _make_reader(main, wt)
    first = reader.read_flow(flow_id, project_root=str(wt))
    _append_jsonl(disc, [_msg("assistant", "r1"), _msg("user", "r2")])
    append = reader.read_flow(flow_id, project_root=str(wt), cursor=first.cursor)

    # A brand-new reader doing a from-scratch full read of the grown file must
    # assign the identical (step_id, ordinal) pairs the incremental path did.
    fresh = _make_reader(main, wt)
    full = fresh.read_flow(flow_id, project_root=str(wt))

    append_keys = dict(zip(_keys(append.records), [r["message"]["content"] for r in append.records]))
    full_keys = dict(zip(_keys(full.records), [r["message"]["content"] for r in full.records]))
    for key, content in append_keys.items():
        assert full_keys[key] == content


# --------------------------------------------------------------------------
# copy-switch: a late-appearing worktree copy re-reads cleanly (no round loss)
# --------------------------------------------------------------------------


def test_late_worktree_copy_switch_reemits_all_rounds(tmp_path):
    """The main copy is read first; when the worktree copy appears it takes over.

    Reproduces the mid-flow physical-copy switch: the daemon first sees only a
    main-repo copy (bare filename cursor advances against it), then the worktree
    copy of the SAME step appears and — because the wire cursor is keyed by bare
    filename but the offset table by absolute path — the by-name cursor would
    wrongly skip the worktree copy's leading lines. The reader must detect the
    switch and re-read the new copy from line 0 so the later rounds arrive; the
    frontend reconciles the re-emitted early lines idempotently by
    ``(step_id, ordinal)``.
    """
    main = tmp_path / "main"
    wt = _make_worktree(main)
    flow_id = "wt-switch"

    main_flow = _flow_dir(main, flow_id)
    # Snapshot 1: only the main copy exists (same filename, round 1 only).
    _write_jsonl(
        main_flow / "01_discovery_ab.jsonl",
        [_msg("user", "the task"), _msg("assistant", "round 1 reply")],
    )

    reader = _make_reader(main, wt)
    first = reader.read_flow(flow_id, project_root=str(wt))
    assert [r["message"]["content"] for r in first.records] == [
        "the task",
        "round 1 reply",
    ]
    first_keys = _keys(first.records)

    # Snapshot 2: the worktree copy (same bare filename) now carries every round.
    wt_flow = _flow_dir(wt, flow_id)
    _write_jsonl(
        wt_flow / "01_discovery_ab.jsonl",
        [
            _msg("user", "the task"),
            _msg("assistant", "round 1 reply"),
            _msg("user", "round 2 answer"),
            _msg("assistant", "round 2 reply"),
        ],
    )

    second = reader.read_flow(flow_id, project_root=str(wt), cursor=first.cursor)
    # The switch triggered a clean read from line 0 of the worktree copy, so the
    # later rounds are delivered (the bug dropped them). Early lines are re-sent
    # with the SAME (step_id, ordinal) keys → idempotent on the frontend.
    contents = [r["message"]["content"] for r in second.records]
    assert "round 2 answer" in contents
    assert "round 2 reply" in contents
    second_keys = _keys(second.records)
    # Re-emitted early lines keep their identity (idempotent reconcile).
    for k in first_keys:
        assert k in second_keys
    # And no new round shares a key with another line.
    assert len(second_keys) == len(set(second_keys))


# --------------------------------------------------------------------------
# selection stability: no thrash across snapshots
# --------------------------------------------------------------------------


def test_merge_selection_sticks_to_worktree_copy(tmp_path):
    """The worktree copy is chosen across snapshots regardless of transient size."""
    main = tmp_path / "main"
    wt = _make_worktree(main)
    flow_id = "wt-stable"
    main_flow = _flow_dir(main, flow_id)
    wt_flow = _flow_dir(wt, flow_id)

    main_disc = main_flow / "01_discovery_ab.jsonl"
    wt_disc = wt_flow / "01_discovery_ab.jsonl"

    # Main copy starts LARGER than the worktree copy.
    _write_jsonl(main_disc, [_msg("user", "task"), _msg("assistant", "big stale main")])
    _write_jsonl(wt_disc, [_msg("user", "task")])

    dirs = [wt_flow.resolve(), main_flow.resolve()]  # auth (worktree) first
    chosen_small = _merge(dirs)
    assert chosen_small == [wt_disc.resolve()]

    # Worktree grows past the main copy; the selection must NOT flip.
    _append_jsonl(wt_disc, [_msg("assistant", "r1"), _msg("user", "r2"), _msg("assistant", "r3")])
    chosen_big = _merge(dirs)
    assert chosen_big == [wt_disc.resolve()]

    # Both copies share one cross-root key, so exactly one survives the merge.
    assert _cross_root_step_key("01_discovery_ab.jsonl") == _cross_root_step_key(
        "01_discovery_cd.jsonl"
    )


def test_live_worktree_reads_stay_incremental_no_reemit(tmp_path):
    """Across a growing live worktree flow, round 1 is emitted exactly once.

    With a stable worktree selection the reads stay incremental (append mode)
    and never re-emit the first round — the churn a flipping selection caused.
    """
    main = tmp_path / "main"
    wt = _make_worktree(main)
    flow_id = "wt-nochurn"
    main_flow = _flow_dir(main, flow_id)
    wt_flow = _flow_dir(wt, flow_id)
    # A same-named stale main copy is present the whole time.
    _write_jsonl(main_flow / "01_discovery_ab.jsonl", [_msg("user", "task"), _msg("assistant", "stale")])
    wt_disc = wt_flow / "01_discovery_ab.jsonl"
    _write_jsonl(wt_disc, [_msg("user", "task")])

    reader = _make_reader(main, wt)
    cursor = None
    all_contents = []
    modes = []
    for round_lines in (
        [_msg("assistant", "r1")],
        [_msg("user", "r2"), _msg("assistant", "r3")],
        [_msg("assistant", "r4")],
    ):
        read = reader.read_flow(flow_id, project_root=str(wt), cursor=cursor)
        modes.append(read.mode)
        all_contents += [r["message"]["content"] for r in read.records]
        cursor = read.cursor
        _append_jsonl(wt_disc, round_lines)

    # Drain the final append.
    final = reader.read_flow(flow_id, project_root=str(wt), cursor=cursor)
    all_contents += [r["message"]["content"] for r in final.records]

    # First read full, the rest incremental appends (no full re-read churn).
    assert modes[0] == HISTORY_MODE_FULL
    assert all(m == HISTORY_MODE_APPEND for m in modes[1:])
    # "task" (round 1) appears exactly once — never re-emitted by a flip.
    assert all_contents.count("task") == 1
    assert all_contents == ["task", "r1", "r2", "r3", "r4"]


# --------------------------------------------------------------------------
# post-merge topology: primary + sidecar render as distinct streams
# --------------------------------------------------------------------------


def test_post_merge_primary_and_sidecar_distinct_streams(tmp_path):
    """After merge-back, the primary and its sidecar are BOTH read, distinctly.

    A single (post-merge) root holds the primary discovery file plus the
    worktree's collision sidecar. Both must render; their per-file ordinals
    (each from 0) must NOT collide because the display ids differ.
    """
    main = tmp_path / "main"
    flow_id = "merged"
    flow_dir = _flow_dir(main, flow_id)
    _write_jsonl(
        flow_dir / "01_discovery_ab.jsonl",
        [_msg("user", "the task"), _msg("assistant", "main round 1")],
    )
    _write_jsonl(
        flow_dir / "01_discovery_ab.jsonl.from-worktree__b",
        [_msg("assistant", "worktree round 1"), _msg("user", "worktree round 2")],
    )

    reader = _make_reader(main)
    read = reader.read_flow(flow_id, project_root=str(main))

    contents = [r["message"]["content"] for r in read.records]
    assert contents == [
        "the task",
        "main round 1",
        "worktree round 1",
        "worktree round 2",
    ]
    # Distinct display ids → the sidecar's ordinal-0 record does not collide with
    # the primary's ordinal-0 record.
    assert {r["step_id"] for r in read.records} == {
        "01_discovery_ab",
        "01_discovery_ab.from-worktree__b",
    }
    keys = _keys(read.records)
    assert len(keys) == len(set(keys))
    # Step type is still the folded logical type on every record.
    assert {r["step_type"] for r in read.records} == {"discovery"}


def test_pre_fix_collision_would_lose_sidecar_records(tmp_path):
    """Documents the collision the fix removes: folded ids clash at ordinal 0.

    Under the OLD folding (both files → ``01_discovery_ab``), the sidecar's
    record at ordinal 0 shared a key with the primary's record at ordinal 0, so
    a key-based frontend reconcile dropped it. The fix makes the keys distinct,
    which this asserts by simulating the frontend's ``step_id#ordinal`` reconcile.
    """
    main = tmp_path / "main"
    flow_id = "collide"
    flow_dir = _flow_dir(main, flow_id)
    _write_jsonl(flow_dir / "01_discovery_ab.jsonl", [_msg("assistant", "primary line")])
    _write_jsonl(
        flow_dir / "01_discovery_ab.jsonl.from-worktree__b",
        [_msg("assistant", "sidecar line")],
    )

    reader = _make_reader(main)
    read = reader.read_flow(flow_id, project_root=str(main))

    # Simulate the frontend reconcile: keep the last record per (step_id, ordinal).
    reconciled = {}
    for r in read.records:
        reconciled[(r["step_id"], r["ordinal"])] = r["message"]["content"]
    # Both survive because their keys differ — no record dropped.
    assert set(reconciled.values()) == {"primary line", "sidecar line"}
