"""Tests for the daemon history reader's multi-root merge (worktree split).

A ``se3 run --worktree`` flow runs its discovery step in the main repo *before*
the fork (writing ``<main>/se3/history/<flow_id>/01_discovery_*.jsonl``) and
every later step in the worktree (writing ``<worktree>/se3/history/<flow_id>/``,
which usually also clones the discovery file). The reader's directory
resolution previously walked the registered roots and returned the *first*
match — the main repo, holding only the discovery record — so the WebUI showed
only the first conversation and every later step vanished.

These tests pin the fix described in design group G1:

* :meth:`DaemonHistoryReader._resolve_flow_dirs` resolves the flow's
  authoritative ``project_root`` as the single source of truth and additionally
  brings in the owning main repo, so the read locates *both* roots rather than
  guessing the first registry match.
* :meth:`DaemonHistoryReader.read_flow` merges the two roots' per-step files,
  de-duplicating the same-named discovery file (no duplicate discovery) while
  keeping every worktree-only later step and back-filling a split where only
  the main repo carries discovery.
"""

from __future__ import annotations

import json
from pathlib import Path

from tianluo.daemon.history import DaemonHistoryReader
from tianluo.daemon.protocol import HISTORY_MODE_APPEND, HISTORY_MODE_FULL


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
    d = root / "se3" / "history" / flow_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _make_worktree(main_root, name="wt__b"):
    """Create and return a worktree isolation dir under *main_root*.

    ``resolve_worktree_main_root`` only attributes a ``<main>/se3/worktrees/<name>``
    path back to ``<main>`` when ``<main>/se3`` exists, so the main repo must
    carry an ``se3`` directory.
    """
    (main_root / "se3").mkdir(parents=True, exist_ok=True)
    wt = main_root / "se3" / "worktrees" / name
    wt.mkdir(parents=True, exist_ok=True)
    return wt


# --------------------------------------------------------------------------
# _resolve_flow_dirs
# --------------------------------------------------------------------------


def test_resolve_flow_dirs_worktree_returns_both_roots(tmp_path):
    """An authoritative worktree root returns [worktree dir, main-repo dir]."""
    main = tmp_path / "main"
    wt = _make_worktree(main)
    flow_id = "wt-1"
    _flow_dir(main, flow_id)
    _flow_dir(wt, flow_id)

    reader = _make_reader(main, wt)
    dirs = reader._resolve_flow_dirs(flow_id, str(wt))

    # Authoritative (worktree) root first, then the owning main repo.
    assert dirs[0] == (wt / "se3" / "history" / flow_id).resolve()
    assert (main / "se3" / "history" / flow_id).resolve() in dirs
    assert len(dirs) == 2


def test_resolve_flow_dirs_main_root_only_self(tmp_path):
    """A plain (non-worktree) root resolves to just its own directory."""
    main = tmp_path / "main"
    flow_id = "f1"
    _flow_dir(main, flow_id)

    reader = _make_reader(main)
    dirs = reader._resolve_flow_dirs(flow_id, str(main))

    assert dirs == [(main / "se3" / "history" / flow_id).resolve()]


def test_resolve_flow_dirs_skips_missing_main_dir(tmp_path):
    """When the main repo lacks the flow's history, only the worktree is kept."""
    main = tmp_path / "main"
    wt = _make_worktree(main)
    flow_id = "wt-2"
    # Only the worktree carries this flow's history.
    _flow_dir(wt, flow_id)

    reader = _make_reader(main, wt)
    dirs = reader._resolve_flow_dirs(flow_id, str(wt))

    assert dirs == [(wt / "se3" / "history" / flow_id).resolve()]


def test_resolve_flow_dirs_empty_project_root_legacy_heuristic(tmp_path):
    """Empty project_root degrades to the legacy first-match registry walk."""
    main = tmp_path / "main"
    other = tmp_path / "other"
    flow_id = "f1"
    _flow_dir(main, flow_id)
    _flow_dir(other, flow_id)

    reader = _make_reader(main, other)
    dirs = reader._resolve_flow_dirs(flow_id, None)

    # Legacy behaviour: a single first-match directory.
    assert len(dirs) == 1
    assert dirs[0] in (
        main / "se3" / "history" / flow_id,
        other / "se3" / "history" / flow_id,
    )


def test_resolve_flow_dirs_unknown_flow_empty(tmp_path):
    """No directory anywhere → empty candidate list, no error."""
    main = tmp_path / "main"
    wt = _make_worktree(main)
    reader = _make_reader(main, wt)
    assert reader._resolve_flow_dirs("nope", str(wt)) == []


# --------------------------------------------------------------------------
# read_flow merging across roots
# --------------------------------------------------------------------------


def test_read_flow_merges_split_history_dedups_discovery(tmp_path):
    """Main has full discovery, worktree has discovery clone + later steps.

    The merged read must show discovery exactly once (no duplicate) plus every
    worktree-only later step, in step order.
    """
    main = tmp_path / "main"
    wt = _make_worktree(main)
    flow_id = "wt-split"

    main_flow = _flow_dir(main, flow_id)
    wt_flow = _flow_dir(wt, flow_id)

    # Main repo: full discovery (ran before the fork).
    _write_jsonl(
        main_flow / "01_discovery_ab.jsonl",
        [_msg("user", "the task"), _msg("assistant", "discovery answer")],
    )
    # Worktree: a clone of the same discovery file (same name + content) ...
    _write_jsonl(
        wt_flow / "01_discovery_ab.jsonl",
        [_msg("user", "the task"), _msg("assistant", "discovery answer")],
    )
    # ... plus the later steps that only ran in the worktree.
    _write_jsonl(
        wt_flow / "02_analyze_cd.jsonl",
        [_msg("assistant", "analyze body")],
    )
    _write_jsonl(
        wt_flow / "03_plan_ef.jsonl",
        [_msg("assistant", "plan body")],
    )

    reader = _make_reader(main, wt)
    read = reader.read_flow(flow_id, project_root=str(wt))

    assert read.mode == HISTORY_MODE_FULL
    contents = [r["message"]["content"] for r in read.records]
    # Discovery appears exactly once (deduped), later steps all present, ordered.
    assert contents == [
        "the task",
        "discovery answer",
        "analyze body",
        "plan body",
    ]
    step_types = [r["step_type"] for r in read.records]
    assert step_types == ["discovery", "discovery", "analyze", "plan"]


def test_read_flow_backfills_discovery_only_in_main(tmp_path):
    """Split where ONLY the main repo holds discovery, worktree has later steps.

    The first conversation (discovery) must be back-filled from the main repo
    even though the authoritative root is the worktree.
    """
    main = tmp_path / "main"
    wt = _make_worktree(main)
    flow_id = "wt-backfill"

    main_flow = _flow_dir(main, flow_id)
    wt_flow = _flow_dir(wt, flow_id)

    _write_jsonl(
        main_flow / "01_discovery_ab.jsonl",
        [_msg("user", "the task")],
    )
    _write_jsonl(
        wt_flow / "02_analyze_cd.jsonl",
        [_msg("assistant", "analyze body")],
    )

    reader = _make_reader(main, wt)
    read = reader.read_flow(flow_id, project_root=str(wt))

    contents = [r["message"]["content"] for r in read.records]
    assert contents == ["the task", "analyze body"]
    assert [r["step_type"] for r in read.records] == ["discovery", "analyze"]


def test_read_flow_prefers_worktree_write_root_copy_stably(tmp_path):
    """When two roots hold the same-named file, the worktree copy wins — stably.

    ``run_worktree_mode`` forks first and runs discovery ENTIRELY in the worktree
    (``run_flow(project_root=<worktree>)``), so the worktree copy is the *actual
    write root* — the one that grows every round. A pure ``largest-copy-wins``
    rule flipped the selection from the (initially larger) main copy to the
    worktree copy the instant the worktree file overtook it, desyncing the
    by-name cursor from the by-abs-path offset table and dropping the rounds
    after the first. The reader now prefers the worktree copy regardless of its
    transient byte size, so the selection is stable across snapshots and every
    round the worktree writes is read in full.
    """
    main = tmp_path / "main"
    wt = _make_worktree(main)
    flow_id = "wt-complete"

    main_flow = _flow_dir(main, flow_id)
    wt_flow = _flow_dir(wt, flow_id)

    # A stale/larger pre-existing main copy must NOT be preferred over the live
    # worktree copy: preferring the larger main copy is exactly what would flip
    # the selection mid-flow once the worktree overtook it.
    _write_jsonl(
        main_flow / "01_discovery_ab.jsonl",
        [_msg("user", "the task"), _msg("assistant", "stale main answer")],
    )
    # The worktree copy is the live writer; it starts with just the first round.
    _write_jsonl(
        wt_flow / "01_discovery_ab.jsonl",
        [_msg("user", "the task")],
    )

    reader = _make_reader(main, wt)
    first = reader.read_flow(flow_id, project_root=str(wt))
    # The worktree (write-root) copy is chosen even though the main copy is
    # currently larger — no flip risk when the worktree grows past it.
    assert [r["message"]["content"] for r in first.records] == ["the task"]

    # The worktree writes its later rounds (now overtaking the main copy). With a
    # stable worktree selection this is a plain incremental append — every round
    # is read, none dropped.
    _append_jsonl(
        wt_flow / "01_discovery_ab.jsonl",
        [_msg("assistant", "round 2"), _msg("user", "round 3")],
    )
    second = reader.read_flow(flow_id, project_root=str(wt), cursor=first.cursor)
    assert second.mode == HISTORY_MODE_APPEND
    assert [r["message"]["content"] for r in second.records] == ["round 2", "round 3"]


def test_read_flow_incremental_across_merge(tmp_path):
    """Incremental cursor advances correctly over the merged file set."""
    main = tmp_path / "main"
    wt = _make_worktree(main)
    flow_id = "wt-incr"

    main_flow = _flow_dir(main, flow_id)
    wt_flow = _flow_dir(wt, flow_id)

    _write_jsonl(main_flow / "01_discovery_ab.jsonl", [_msg("user", "task")])
    _write_jsonl(wt_flow / "01_discovery_ab.jsonl", [_msg("user", "task")])
    _write_jsonl(wt_flow / "02_analyze_cd.jsonl", [_msg("assistant", "a1")])

    reader = _make_reader(main, wt)
    first = reader.read_flow(flow_id, project_root=str(wt))
    assert [r["message"]["content"] for r in first.records] == ["task", "a1"]

    # The worktree appends new analyze records.
    _append_jsonl(
        wt_flow / "02_analyze_cd.jsonl",
        [_msg("assistant", "a2"), _msg("assistant", "a3")],
    )

    second = reader.read_flow(flow_id, project_root=str(wt), cursor=first.cursor)
    assert second.mode == HISTORY_MODE_APPEND
    # Only the new records, no re-read, no duplicate.
    assert [r["message"]["content"] for r in second.records] == ["a2", "a3"]

    # A third read with no further writes yields nothing.
    third = reader.read_flow(flow_id, project_root=str(wt), cursor=second.cursor)
    assert third.records == []


def test_read_flow_missing_one_root_does_not_raise(tmp_path):
    """If the main repo has no history dir at all, the worktree result still reads."""
    main = tmp_path / "main"
    wt = _make_worktree(main)
    flow_id = "wt-only"
    wt_flow = _flow_dir(wt, flow_id)
    _write_jsonl(wt_flow / "01_discovery_ab.jsonl", [_msg("user", "task")])
    _write_jsonl(wt_flow / "02_analyze_cd.jsonl", [_msg("assistant", "a1")])

    reader = _make_reader(main, wt)
    read = reader.read_flow(flow_id, project_root=str(wt))
    assert [r["message"]["content"] for r in read.records] == ["task", "a1"]


def test_read_flow_dedups_discovery_with_differing_hash(tmp_path):
    """A discovery clone under a DIFFERENT per-step hash still dedups to one.

    The main repo wrote ``01_discovery_ab.jsonl`` before the fork; the worktree
    carries the same logical discovery step but under a different hash segment
    (``01_discovery_cd.jsonl``). The merge MUST collapse them to a single
    discovery (keyed by logical step identity, not the physical filename) so the
    conversation shows discovery exactly once before the worktree-only steps.
    """
    main = tmp_path / "main"
    wt = _make_worktree(main)
    flow_id = "wt-hash"

    main_flow = _flow_dir(main, flow_id)
    wt_flow = _flow_dir(wt, flow_id)

    # Same logical discovery step, DIFFERENT hash in each root.
    _write_jsonl(
        main_flow / "01_discovery_ab.jsonl",
        [_msg("user", "the task"), _msg("assistant", "discovery answer")],
    )
    _write_jsonl(
        wt_flow / "01_discovery_cd.jsonl",
        [_msg("user", "the task"), _msg("assistant", "discovery answer")],
    )
    _write_jsonl(wt_flow / "02_analyze_ef.jsonl", [_msg("assistant", "analyze body")])

    reader = _make_reader(main, wt)
    read = reader.read_flow(flow_id, project_root=str(wt))

    contents = [r["message"]["content"] for r in read.records]
    # Discovery appears ONCE (deduped across the differing hashes), then analyze.
    assert contents == ["the task", "discovery answer", "analyze body"]
    assert [r["step_type"] for r in read.records] == [
        "discovery",
        "discovery",
        "analyze",
    ]


# --------------------------------------------------------------------------
# index → authoritative project_root for a split ACTIVE worktree flow
# --------------------------------------------------------------------------


def _write_engine(root, flow_id, status, *, is_worktree_mode=False):
    state_dir = root / "se3" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {"flow_id": flow_id, "status": status}
    if is_worktree_mode:
        payload["is_worktree_mode"] = True
    (state_dir / "engine.json").write_text(json.dumps(payload), encoding="utf-8")


def test_index_records_worktree_root_for_split_active_flow(tmp_path):
    """The index must record the WORKTREE root (not the main repo) for a split
    active worktree flow, and that recorded root must let the read reach the
    worktree's later steps.

    Reproduces the regression path: the main repo carries the pre-fork
    discovery dir (a *history-only* source) and the worktree subdir carries the
    live ``engine.json`` + later steps (an *active* source). ``_iter_roots``
    enumerates the main repo first, so a first-claim-wins dedup would record the
    flow as a non-active history row under the main root — neither streamed live
    nor pointing at the worktree. Source precedence must instead keep the active
    worktree claim authoritative.
    """
    main = tmp_path / "main"
    wt = _make_worktree(main)
    flow_id = "wt-active"

    main_flow = _flow_dir(main, flow_id)
    wt_flow = _flow_dir(wt, flow_id)

    # Main repo: only the pre-fork discovery dir (history-only, no engine.json).
    _write_jsonl(main_flow / "01_discovery_ab.jsonl", [_msg("user", "the task")])
    # Worktree: the live engine.json + the later steps.
    _write_engine(wt, flow_id, "RUNNING", is_worktree_mode=True)
    _write_jsonl(wt_flow / "02_analyze_cd.jsonl", [_msg("assistant", "analyze body")])

    # Provider order mirrors the real ``all_observable_roots`` sorted order:
    # the main repo sorts before its ``se3/worktrees/<name>`` subdir.
    reader = _make_reader(main, wt)
    index = reader.build_index()
    meta = next(m for m in index if m.flow_id == flow_id)

    # The active worktree claim supersedes the main repo's history-only clone.
    assert meta.source == "active"
    assert meta.active is True
    assert Path(meta.project_root).resolve() == wt.resolve()

    # And the recorded authoritative root reaches the worktree's later steps
    # (merged with the main repo's discovery) — the end-to-end invariant.
    read = reader.read_flow(flow_id, project_root=meta.project_root)
    assert [r["message"]["content"] for r in read.records] == [
        "the task",
        "analyze body",
    ]


def test_read_flow_main_root_forward_expands_into_worktree(tmp_path):
    """Even if the index recorded the MAIN root, the read still reaches the
    worktree's later steps via forward (main → worktree) expansion.

    This is the backstop that keeps the merge complete regardless of which of
    the two roots the index recorded as authoritative.
    """
    main = tmp_path / "main"
    wt = _make_worktree(main)
    flow_id = "wt-fwd"

    main_flow = _flow_dir(main, flow_id)
    wt_flow = _flow_dir(wt, flow_id)

    _write_jsonl(main_flow / "01_discovery_ab.jsonl", [_msg("user", "the task")])
    _write_jsonl(wt_flow / "02_analyze_cd.jsonl", [_msg("assistant", "analyze body")])

    reader = _make_reader(main, wt)

    # Resolve with the MAIN root as authoritative: forward expansion must pull
    # in the worktree subdir's later steps.
    dirs = reader._resolve_flow_dirs(flow_id, str(main))
    resolved = {d.resolve() for d in dirs}
    assert (main / "se3" / "history" / flow_id).resolve() in resolved
    assert (wt / "se3" / "history" / flow_id).resolve() in resolved

    read = reader.read_flow(flow_id, project_root=str(main))
    assert [r["message"]["content"] for r in read.records] == [
        "the task",
        "analyze body",
    ]


def test_read_flow_authoritative_root_beats_registry_first_match(tmp_path):
    """The authoritative project_root governs the read, not the registry order.

    The main repo is registered FIRST (so the legacy first-match heuristic would
    have returned only its lone discovery record). Passing the worktree as the
    authoritative project_root must instead yield the worktree's complete set
    merged with the main discovery — pinning that the read consumes the
    authoritative root rather than the registry's first hit.
    """
    main = tmp_path / "main"
    wt = _make_worktree(main)
    flow_id = "wt-auth"

    main_flow = _flow_dir(main, flow_id)
    wt_flow = _flow_dir(wt, flow_id)

    # Main repo: only the discovery record (the pre-fork step).
    _write_jsonl(main_flow / "01_discovery_ab.jsonl", [_msg("user", "task")])
    # Worktree: the full later-step set.
    _write_jsonl(wt_flow / "02_analyze_cd.jsonl", [_msg("assistant", "analyze")])
    _write_jsonl(wt_flow / "03_plan_ef.jsonl", [_msg("assistant", "plan")])

    # Register the main repo FIRST — the legacy heuristic would pick it.
    reader = _make_reader(main, wt)

    # Legacy (no project_root) only sees the first-match main repo's lone record.
    legacy = reader.read_flow(flow_id)
    assert [r["message"]["content"] for r in legacy.records] == ["task"]

    # Authoritative project_root=worktree yields the full merged conversation.
    auth = reader.read_flow(flow_id, project_root=str(wt))
    assert [r["message"]["content"] for r in auth.records] == [
        "task",
        "analyze",
        "plan",
    ]
