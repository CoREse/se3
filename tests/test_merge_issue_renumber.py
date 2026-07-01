"""Tests for worktree-issue renumbering during ``se3 merge`` (G6).

When a ``--worktree`` run creates new issues, its isolation worktree allocates
IDs from its own ``.next_id`` counter, independent of the main project's. On
merge-back those IDs can collide with issue numbers the main project assigned
independently. ``merge_worktree_issues`` folds the worktree's new issues into
the main project under fresh IDs, skips content already present (pre-fork
copies and duplicates), and is idempotent on re-run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from se3.engine.issue_manager import IssueManager, IssueStatus
from se3.engine.merge.runtime_sync import (
    IssueMergeRecord,
    merge_worktree_issues,
)


def _read_next_id(project_root: Path) -> int:
    """Return the integer value of the project's ``.next_id`` counter file."""
    counter = project_root / "se3" / "issues" / ".next_id"
    return int(counter.read_text().strip())


def _ids_on_disk(mgr: IssueManager) -> set[str]:
    """Return the set of issue IDs the manager can see on disk (open+closed)."""
    return {i.id for i in mgr.list_issues(include_closed=True)}


def test_overlapping_ids_renumbered_without_conflict(tmp_path: Path) -> None:
    """A worktree issue whose ID collides with the main project is renumbered.

    Main has 001/002/003. The worktree has content-identical 001/002 (pre-fork)
    plus a *different* 003 it created itself. After merge the worktree's new
    issue must land under a non-colliding ID (004), leaving main's 003 intact.
    """
    main_root = tmp_path / "main"
    wt_root = tmp_path / "wt"

    main_mgr = IssueManager(main_root)
    main_mgr.create("main issue one")  # 001
    main_mgr.create("main issue two")  # 002
    main_mgr.create("main issue three")  # 003

    wt_mgr = IssueManager(wt_root)
    wt_mgr.create("main issue one")  # 001 — pre-fork copy
    wt_mgr.create("main issue two")  # 002 — pre-fork copy
    wt_new = wt_mgr.create("worktree brand new issue")  # 003 — colliding ID

    records = merge_worktree_issues(main_root, wt_root)

    # Exactly one new issue folded in.
    assert len(records) == 1
    rec = records[0]
    assert isinstance(rec, IssueMergeRecord)
    assert rec.old_id == "003"
    assert rec.new_id == "004"
    assert rec.status_dir == "open"

    # No ID conflict: main now holds 001..004, all distinct.
    assert _ids_on_disk(main_mgr) == {"001", "002", "003", "004"}

    # Main's original 003 is untouched.
    assert main_mgr.load("003").description == "main issue three"

    # The renumbered issue carries the worktree's new content under id 004,
    # and the on-disk filename agrees with the stored id.
    adopted = main_mgr.load("004")
    assert adopted.id == "004"
    # Content preserved (renumber appends a trace line to the tail, so the
    # worktree body is a prefix rather than the whole description).
    assert adopted.description.startswith("worktree brand new issue")
    assert wt_new.description in adopted.description
    assert "旧号 #003 → 新号 #004" in adopted.description
    open_dir = main_root / "se3" / "issues" / "open"
    matches = list(open_dir.glob("004_*.yaml"))
    assert len(matches) == 1
    assert matches[0].name.startswith("004_")

    # .next_id advanced by exactly the number merged (3 -> ... -> 5 after
    # allocating 004).
    assert _read_next_id(main_root) == 5


def test_duplicate_content_not_merged_again(tmp_path: Path) -> None:
    """Worktree issues whose content already exists in main are not re-added."""
    main_root = tmp_path / "main"
    wt_root = tmp_path / "wt"

    main_mgr = IssueManager(main_root)
    main_mgr.create("alpha")  # 001
    main_mgr.create("beta")  # 002

    wt_mgr = IssueManager(wt_root)
    # All content already present in main (whitespace/case variants must still
    # be treated as duplicates).
    wt_mgr.create("alpha")
    wt_mgr.create("  BETA  ")

    records = merge_worktree_issues(main_root, wt_root)

    assert records == []
    assert _ids_on_disk(main_mgr) == {"001", "002"}
    assert _read_next_id(main_root) == 3


def test_idempotent_rerun_does_not_duplicate(tmp_path: Path) -> None:
    """Re-running the merge folds nothing in the second time (content dedup)."""
    main_root = tmp_path / "main"
    wt_root = tmp_path / "wt"

    main_mgr = IssueManager(main_root)
    main_mgr.create("existing")  # 001

    wt_mgr = IssueManager(wt_root)
    wt_mgr.create("existing")  # 001 — pre-fork
    wt_mgr.create("a worktree task")  # 002 — new
    wt_mgr.create("another worktree task")  # 003 — new

    first = merge_worktree_issues(main_root, wt_root)
    assert len(first) == 2
    assert _ids_on_disk(main_mgr) == {"001", "002", "003"}
    next_after_first = _read_next_id(main_root)

    # Second run: the worktree's new content now exists in main, so nothing
    # is folded in and the counter does not advance.
    second = merge_worktree_issues(main_root, wt_root)
    assert second == []
    assert _ids_on_disk(main_mgr) == {"001", "002", "003"}
    assert _read_next_id(main_root) == next_after_first


def test_next_id_monotonic_with_multiple_new_issues(tmp_path: Path) -> None:
    """``.next_id`` advances by exactly the count of issues merged in."""
    main_root = tmp_path / "main"
    wt_root = tmp_path / "wt"

    main_mgr = IssueManager(main_root)
    main_mgr.create("m1")  # 001
    main_mgr.create("m2")  # 002

    wt_mgr = IssueManager(wt_root)
    for n in range(5):
        wt_mgr.create(f"worktree new {n}")  # 001..005

    before = _read_next_id(main_root)
    records = merge_worktree_issues(main_root, wt_root)

    assert len(records) == 5
    new_ids = [r.new_id for r in records]
    # Freshly allocated, contiguous, non-colliding with main's 001/002.
    assert new_ids == ["003", "004", "005", "006", "007"]
    # All distinct, none equal to a pre-existing main ID.
    assert len(set(new_ids)) == 5
    assert _read_next_id(main_root) == before + 5


def test_closed_worktree_issue_renumbered_into_closed_dir(tmp_path: Path) -> None:
    """A new *closed* worktree issue is adopted into the main ``closed/`` dir."""
    main_root = tmp_path / "main"
    wt_root = tmp_path / "wt"

    main_mgr = IssueManager(main_root)
    main_mgr.create("kept open")  # 001

    wt_mgr = IssueManager(wt_root)
    new_closed = wt_mgr.create("a finished worktree task")  # 001
    wt_mgr.update_status(new_closed.id, IssueStatus.CLOSED)

    records = merge_worktree_issues(main_root, wt_root)

    assert len(records) == 1
    assert records[0].status_dir == "closed"
    new_id = records[0].new_id

    closed_dir = main_root / "se3" / "issues" / "closed"
    matches = list(closed_dir.glob(f"{new_id}_*.yaml"))
    assert len(matches) == 1

    adopted = main_mgr.load(new_id)
    assert adopted.status == IssueStatus.CLOSED
    assert adopted.description.startswith("a finished worktree task")


def test_missing_worktree_issues_dir_is_noop(tmp_path: Path) -> None:
    """No worktree ``se3/issues/`` directory yields an empty merge, no error."""
    main_root = tmp_path / "main"
    wt_root = tmp_path / "wt"

    main_mgr = IssueManager(main_root)
    main_mgr.create("only main")

    # wt_root has no se3/issues/ at all.
    records = merge_worktree_issues(main_root, wt_root)
    assert records == []
    assert _ids_on_disk(main_mgr) == {"001"}


def test_adopt_issue_preserves_fields_and_allocates_new_id(tmp_path: Path) -> None:
    """``IssueManager.adopt_issue`` keeps fields but assigns a fresh ID."""
    main_root = tmp_path / "main"
    main_mgr = IssueManager(main_root)
    main_mgr.create("first")  # 001 -> next_id becomes 002

    src_mgr = IssueManager(tmp_path / "src")
    original = src_mgr.create(
        "imported body",
        title="Imported Title",
        priority="high",
        tags=["x", "y"],
        type="bug",
        source="human",
    )
    # Force a colliding ID to prove adopt reassigns it.
    original.id = "001"

    adopted = main_mgr.adopt_issue(original)

    assert adopted.id == "002"
    assert adopted.id != original.id
    assert adopted.title == "Imported Title"
    # 001 -> 002 is a renumber, so the body is preserved with an appended trace.
    assert adopted.description.startswith("imported body")
    assert "旧号 #001 → 新号 #002" in adopted.description
    assert adopted.priority == "high"
    assert adopted.tags == ["x", "y"]
    assert adopted.type == "bug"
    assert adopted.source == "human"
    assert _ids_on_disk(main_mgr) == {"001", "002"}


def test_adopt_issue_rejects_empty_description(tmp_path: Path) -> None:
    """Adopting an issue with an empty description raises ValueError."""
    from se3.engine.issue_manager import Issue

    main_mgr = IssueManager(tmp_path / "main")
    bad = Issue(id="009", description="   ")
    with pytest.raises(ValueError):
        main_mgr.adopt_issue(bad)


def test_renumber_rewrites_self_reference_and_records_trace(tmp_path: Path) -> None:
    """A renumbered worktree issue gets its ``#<old>`` refs rewritten + traced.

    Runtime-sync must satisfy the same guarantee as the git channel: when a
    worktree issue is renumbered, every ``#<old>`` cross-reference is repointed
    to ``#<new>``, a traceable "old -> new" line is recorded, both sides survive,
    and ``.next_id`` lands at ``max(ID) + 1``.
    """
    from se3.engine.merge.issue_renumber import format_renumber_trace

    main_root = tmp_path / "main"
    wt_root = tmp_path / "wt"

    main_mgr = IssueManager(main_root)
    main_mgr.create("main root")    # 001
    main_mgr.create("main second")  # 002
    main_mgr.create("main third")   # 003 -> next_id 004

    wt_mgr = IssueManager(wt_root)
    # New worktree issue at worktree id 001; its body references its own id.
    wt_new = wt_mgr.create("Track subtasks in #001 before closing")  # 001

    records = merge_worktree_issues(main_root, wt_root)

    assert len(records) == 1
    rec = records[0]
    assert rec.old_id == "001"
    # Main's next free ID was 004, so the colliding 001 is renumbered to 004.
    assert rec.new_id == "004"

    adopted = main_mgr.load("004")
    # Self-reference #001 -> #004 rewritten in the adopted body.
    assert "Track subtasks in #004 before closing" in adopted.description
    assert "#001" not in adopted.description.split("旧号")[0]
    # Traceable old -> new record appended.
    assert format_renumber_trace("001", "004") in adopted.description

    # Main's own issue 001 is a different issue and must be left intact.
    assert main_mgr.load("001").description == "main root"

    # Both sides preserved: main gained 004, worktree keeps its issue.
    assert _ids_on_disk(main_mgr) == {"001", "002", "003", "004"}
    assert wt_new.id == "001"
    assert wt_mgr.load("001") is not None

    # Counter advanced to max(ID) + 1.
    assert _read_next_id(main_root) == 5


def test_renumber_then_idempotent_rerun(tmp_path: Path) -> None:
    """A renumbered issue is not re-adopted on a second merge (dedup survives).

    The main-project copy carries a renumber trace the worktree source lacks;
    the content signature strips it so the re-run still recognises the issue as
    already merged and folds nothing in.
    """
    main_root = tmp_path / "main"
    wt_root = tmp_path / "wt"

    main_mgr = IssueManager(main_root)
    for n in range(1, 6):
        main_mgr.create(f"main {n}")  # 001..005 -> next_id 006

    wt_mgr = IssueManager(wt_root)
    # A brand-new worktree issue at worktree id 001 (no self-reference).
    wt_mgr.create("brand new worktree task")  # 001

    first = merge_worktree_issues(main_root, wt_root)
    assert len(first) == 1
    assert first[0].old_id == "001"
    # next_id was 006, so 001 collides and is renumbered up to 006.
    assert first[0].new_id == "006"

    adopted = main_mgr.load("006")
    assert adopted.description.startswith("brand new worktree task")
    assert "旧号 #001 → 新号 #006" in adopted.description
    assert _read_next_id(main_root) == 7

    # Second run: the content already exists in main (trace stripped for the
    # comparison), so nothing is folded in and the counter does not advance.
    second = merge_worktree_issues(main_root, wt_root)
    assert second == []
    assert _read_next_id(main_root) == 7
    assert _ids_on_disk(main_mgr) == {
        "001", "002", "003", "004", "005", "006",
    }


def test_next_id_matches_max_after_renumber(tmp_path: Path) -> None:
    """After adopting, ``.next_id`` equals the on-disk max ID + 1.

    ``advance_next_id_to_max`` self-heals a lagging counter even if it fell
    behind the highest existing ID before the adopt.
    """
    main_root = tmp_path / "main"
    main_mgr = IssueManager(main_root)
    main_mgr.create("keep")  # 001

    # Simulate a lagging/garbage counter the way a botched prior write might.
    (main_root / "se3" / "issues" / ".next_id").write_text("not-a-number")

    src_mgr = IssueManager(tmp_path / "src")
    src_issue = src_mgr.create("adopt me referencing #009")
    src_issue.id = "009"  # force a colliding old id distinct from the new one

    adopted = main_mgr.adopt_issue(src_issue)

    on_disk_max = max(int(i.id) for i in main_mgr.list_issues(include_closed=True))
    assert _read_next_id(main_root) == on_disk_max + 1
    # The forced-collision self reference #009 was repointed to the new id.
    assert f"#{adopted.id}" in adopted.description
