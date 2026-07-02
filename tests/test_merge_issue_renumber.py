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


def test_adopt_retired_number_rewrites_store_wide_references(
    tmp_path: Path,
) -> None:
    """Adopting an UNCOLLIDED old ID repoints every ``#<old>`` in the store.

    When the incoming issue's old number is not owned by any kept main-project
    issue, adopting it retires that number entirely — so an existing dependent
    issue that referenced ``#<old>`` must be repointed to the new ID, not left
    dangling on a number that now belongs to nobody. (When the old number IS
    still owned by a kept issue the rewrite must instead stay scoped to the
    incoming file; that collision case is covered by the ``#001`` tests above.)
    """
    main_root = tmp_path / "main"
    main_mgr = IssueManager(main_root)
    main_mgr.create("keep")  # 001
    dep = main_mgr.create("Blocked by #009")  # 002 — references the old number

    src_mgr = IssueManager(tmp_path / "src")
    src_issue = src_mgr.create("adopt me")
    src_issue.id = "009"  # a number NOT owned by any main-project issue

    adopted = main_mgr.adopt_issue(src_issue)

    # The dependent issue's cross-reference followed the renumber store-wide.
    reloaded_dep = main_mgr.load(dep.id)
    assert f"Blocked by #{adopted.id}" in reloaded_dep.description
    assert "#009" not in reloaded_dep.description


def test_adopt_scopes_rewrite_when_kept_owner_has_mismatched_filename(
    tmp_path: Path,
) -> None:
    """A kept issue owns its parsed ``id`` even when its filename disagrees.

    Kept-side ownership is decided by parsed-``id``-then-filename authority, not
    filename prefix alone. A main issue file ``010_main.yaml`` carrying YAML
    ``id: '005'`` OWNS the number 5, so a ``#005`` in another main issue names
    THAT kept issue and must not follow the adopted issue's renumber. A prior
    filename-prefix-only check saw no ``005_*.yaml`` and rewrote store-wide,
    corrupting the reference — this pins the fix.
    """
    from se3.engine.issue_manager import Issue

    main_root = tmp_path / "main"
    main_mgr = IssueManager(main_root)
    main_mgr._ensure_dirs()
    # Kept issue whose filename prefix (010) disagrees with its parsed id (005).
    kept = Issue(id="005", description="kept issue owning parsed id 5")
    main_mgr._write_issue(main_mgr.open_dir / "010_main.yaml", kept)
    # A dependent main issue references the kept issue by its parsed number.
    dep = main_mgr.create("Blocked by #005")  # allocated after 010 -> id 011

    src_mgr = IssueManager(tmp_path / "src")
    src_issue = src_mgr.create("adopt me")
    src_issue.id = "005"  # collides with the kept issue's parsed id

    adopted = main_mgr.adopt_issue(src_issue)

    # The adopted issue got a fresh id past the kept 010/005 pair.
    assert adopted.id != "005"
    # The dependent reference still points at the kept parsed-id owner, NOT the
    # newly adopted issue — the rewrite was scoped to the incoming file only.
    reloaded_dep = main_mgr.load(dep.id)
    assert "#005" in reloaded_dep.description
    assert f"#{adopted.id}" not in reloaded_dep.description


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


def test_batch_retired_number_rewrites_preexisting_store_reference(
    tmp_path: Path,
) -> None:
    """Batch adopt repoints a pre-existing main ``#<old>`` when no owner remains.

    The runtime-sync BATCH path must match the single-shot adopt_issue path: an
    incoming issue whose old number is owned by NO kept main issue retires that
    number entirely, so an existing dependent issue that referenced ``#<old>``
    is repointed store-wide — not left dangling on a number belonging to nobody.
    Scoping the rewrite to the adopted file alone (the pre-fix behaviour) would
    strand that reference.
    """
    main_root = tmp_path / "main"
    wt_root = tmp_path / "wt"

    from se3.engine.issue_manager import Issue

    main_mgr = IssueManager(main_root)
    main_mgr.create("keep")            # 001
    dep = main_mgr.create("Blocked by #009")  # 002 -> next_id 003

    wt_mgr = IssueManager(wt_root)
    wt_mgr._ensure_dirs()
    # A worktree issue at id 009 — a number no main issue owns.
    wt_mgr._write_issue(
        wt_mgr.open_dir / "009_adopt-me.yaml",
        Issue(id="009", description="adopt me"),
    )

    records = merge_worktree_issues(main_root, wt_root)

    assert len(records) == 1
    rec = records[0]
    assert rec.old_id == "009"
    assert rec.new_id == "003"

    # The pre-existing dependent reference followed the renumber store-wide.
    reloaded_dep = main_mgr.load(dep.id)
    assert f"Blocked by #{rec.new_id}" in reloaded_dep.description
    assert "#009" not in reloaded_dep.description

    # Both sides preserved; counter advanced.
    assert _ids_on_disk(main_mgr) == {"001", "002", "003"}
    assert _read_next_id(main_root) == 4


def test_batch_collision_leaves_kept_owner_reference_scoped(
    tmp_path: Path,
) -> None:
    """Batch adopt keeps a ``#<old>`` pointing at a kept owner, not the newcomer.

    When the incoming number IS still owned by a kept main issue, a main
    ``#<old>`` names THAT kept issue and must stay put — the batch rewrite must
    be scoped to the adopted (incoming) file only, exactly like the single-shot
    path. This pins that the new store-wide branch does not over-rewrite the
    genuine collision case.
    """
    main_root = tmp_path / "main"
    wt_root = tmp_path / "wt"

    main_mgr = IssueManager(main_root)
    main_mgr.create("kept owner of 001")  # 001
    dep = main_mgr.create("Blocked by #001")  # 002 -> next_id 003

    wt_mgr = IssueManager(wt_root)
    # A worktree issue that collides with main's kept id 001.
    wt_new = wt_mgr.create("incoming that references #001 itself")  # 001

    records = merge_worktree_issues(main_root, wt_root)

    assert len(records) == 1
    rec = records[0]
    assert rec.old_id == "001"
    assert rec.new_id == "003"

    # Main's dependent still points at the KEPT owner of 001, untouched.
    reloaded_dep = main_mgr.load(dep.id)
    assert "Blocked by #001" in reloaded_dep.description
    assert f"#{rec.new_id}" not in reloaded_dep.description

    # The adopted issue's own self-reference DID follow the renumber.
    adopted = main_mgr.load(rec.new_id)
    assert f"references #{rec.new_id} itself" in adopted.description


def test_colliding_old_ids_in_source_leave_own_references_ambiguous(
    tmp_path: Path,
) -> None:
    """Two source issues sharing one old ID keep their ``#005`` un-rewritten.

    A worktree store can itself hold a collision: two distinct files both
    parsing to id 005, each body saying ``See #005``. That token could mean
    the issue itself OR its colliding peer — nothing proves which — so
    forcing it to the holder's own new ID would silently turn a peer
    reference into a self-reference. Both issues must be adopted under
    distinct new IDs with the token left as written and the ambiguity
    recorded durably in each holder.
    """
    from se3.engine.issue_manager import Issue
    from se3.engine.merge.issue_renumber import format_ambiguous_reference_note

    main_root = tmp_path / "main"
    wt_root = tmp_path / "wt"

    main_mgr = IssueManager(main_root)
    main_mgr.create("main base")  # 001 -> next_id 002

    wt_mgr = IssueManager(wt_root)
    wt_mgr._ensure_dirs()
    for slug, body in (
        ("alpha", "alpha collision issue\n\nSee #005 for details"),
        ("beta", "beta collision issue\n\nSee #005 for details"),
    ):
        issue = Issue(id="005", description=body)
        wt_mgr._write_issue(wt_mgr.open_dir / f"005_{slug}.yaml", issue)

    records = merge_worktree_issues(main_root, wt_root)

    # Both survive, under distinct fresh IDs.
    assert len(records) == 2
    assert {r.old_id for r in records} == {"005"}
    assert len({r.new_id for r in records}) == 2

    adopted = {
        i.description.split(" ", 1)[0]: i
        for i in main_mgr.list_issues(include_closed=True)
        if "collision issue" in i.description
    }
    alpha, beta = adopted["alpha"], adopted["beta"]
    assert alpha.id != beta.id
    # Each body's #005 keeps its digits — repointing it to the holder's own
    # new ID would be a guess (it may have meant the colliding peer) — and
    # carries the durable ambiguity note listing both candidates.
    for issue in (alpha, beta):
        assert "See #005 for details" in issue.description
        assert f"See #{alpha.id} for details" not in issue.description
        assert f"See #{beta.id} for details" not in issue.description
        note_ab = format_ambiguous_reference_note("005", [alpha.id, beta.id])
        note_ba = format_ambiguous_reference_note("005", [beta.id, alpha.id])
        assert note_ab in issue.description or note_ba in issue.description
    # Both carry their own trace back to the shared old number.
    assert "旧号 #005" in alpha.description
    assert "旧号 #005" in beta.description

    # Counter lands at max(ID) + 1 across the whole store.
    max_id = max(int(i.id) for i in main_mgr.list_issues(include_closed=True))
    assert _read_next_id(main_root) == max_id + 1


def test_ambiguous_shared_old_id_reference_left_and_recorded(
    tmp_path: Path,
) -> None:
    """A third issue's reference to a SHARED old ID is annotated, not guessed.

    Two worktree issues both parse to id 005 and a third worktree issue says
    ``Blocked by #005``. Nothing proves which colliding peer the third issue
    meant, so a map keyed only by the old number would rewrite it to whichever
    peer was adopted LAST — silently corrupting the reference. The reference
    must instead keep its original digits, with a durable ambiguity note
    (listing both candidates) recorded in the adopted copy, and the batch must
    stay idempotent on re-run.
    """
    from se3.engine.issue_manager import Issue
    from se3.engine.merge.issue_renumber import format_ambiguous_reference_note

    main_root = tmp_path / "main"
    wt_root = tmp_path / "wt"

    main_mgr = IssueManager(main_root)
    main_mgr.create("main base")  # 001 -> next_id 002

    wt_mgr = IssueManager(wt_root)
    wt_mgr._ensure_dirs()
    for slug, issue_id, body in (
        ("alpha", "005", "alpha collision issue\n\nSee #005 for details"),
        ("beta", "005", "beta collision issue\n\nSee #005 for details"),
        ("gamma", "007", "gamma dependent issue\n\nBlocked by #005 until fixed"),
    ):
        issue = Issue(id=issue_id, description=body)
        wt_mgr._write_issue(wt_mgr.open_dir / f"{issue_id}_{slug}.yaml", issue)

    records = merge_worktree_issues(main_root, wt_root)
    assert len(records) == 3

    by_prefix = {
        i.description.split(" ", 1)[0]: i
        for i in main_mgr.list_issues(include_closed=True)
    }
    alpha, beta, gamma = by_prefix["alpha"], by_prefix["beta"], by_prefix["gamma"]
    assert len({alpha.id, beta.id, gamma.id}) == 3

    # The colliding holders' own #005 tokens are just as ambiguous (each
    # could mean the peer), so they keep their digits and carry the note too.
    assert "See #005 for details" in alpha.description
    assert "See #005 for details" in beta.description
    assert "歧义引用" in alpha.description
    assert "歧义引用" in beta.description

    # The third issue's reference keeps its original digits — rewriting it to
    # either candidate would be a guess — and records the ambiguity durably.
    assert "Blocked by #005 until fixed" in gamma.description
    assert f"Blocked by #{alpha.id}" not in gamma.description
    assert f"Blocked by #{beta.id}" not in gamma.description
    note_ab = format_ambiguous_reference_note("005", [alpha.id, beta.id])
    note_ba = format_ambiguous_reference_note("005", [beta.id, alpha.id])
    assert note_ab in gamma.description or note_ba in gamma.description

    # Idempotent: a re-run recognises every adopted copy (the note and traces
    # are stripped before the dedup comparison) and folds nothing in again.
    assert merge_worktree_issues(main_root, wt_root) == []

    max_id = max(int(i.id) for i in main_mgr.list_issues(include_closed=True))
    assert _read_next_id(main_root) == max_id + 1


def test_runtime_sync_ambiguity_surfaced_via_out_param(tmp_path: Path) -> None:
    """The runtime-sync channel reports ambiguous #old references to the caller.

    The committed-issue channel records each unresolvable ``#old`` reference in
    ``MergeReport.ambiguous_issue_references``; the runtime-sync channel must
    surface the identical shape so both channels satisfy the same "record the
    ambiguity in the merge report" clause. ``merge_worktree_issues`` collects
    one ``{"file", "old_id", "candidates"}`` entry per affected file into the
    caller-supplied out list.
    """
    from se3.engine.issue_manager import Issue

    main_root = tmp_path / "main"
    wt_root = tmp_path / "wt"

    main_mgr = IssueManager(main_root)
    main_mgr.create("main base")  # 001 -> next_id 002

    wt_mgr = IssueManager(wt_root)
    wt_mgr._ensure_dirs()
    for slug, issue_id, body in (
        ("alpha", "005", "alpha collision issue\n\nSee #005 for details"),
        ("beta", "005", "beta collision issue\n\nSee #005 for details"),
        ("gamma", "007", "gamma dependent issue\n\nBlocked by #005 until fixed"),
    ):
        issue = Issue(id=issue_id, description=body)
        wt_mgr._write_issue(wt_mgr.open_dir / f"{issue_id}_{slug}.yaml", issue)

    ambiguous: list = []
    records = merge_worktree_issues(
        main_root, wt_root, ambiguous_refs_out=ambiguous,
    )
    assert len(records) == 3

    # Every affected file (the two colliding holders and the dependent) is
    # recorded, each carrying the shared old id and the candidate targets.
    assert ambiguous, "runtime-sync ambiguity must be surfaced to the report"
    assert {e["old_id"] for e in ambiguous} == {"005"}
    for entry in ambiguous:
        assert set(entry.keys()) == {"file", "old_id", "candidates"}
        # File paths are repo-relative under the issue store.
        assert entry["file"].startswith("se3/issues/")
        assert len(entry["candidates"]) >= 2
    # The dependent gamma issue is among the recorded files.
    assert any("gamma" in entry["file"] for entry in ambiguous)


def test_shared_old_id_with_identity_keeper_stays_ambiguous(
    tmp_path: Path,
) -> None:
    """A collision where one copy KEEPS the old number is still ambiguous.

    Main's counter can hand the first colliding worktree issue exactly its old
    number (next_id 5, worktree holds two distinct 005 files: one is adopted AS
    005, the other moves to 006). ``#005`` then has TWO live targets — the
    keeper and the renumbered peer — so a map keyed only on *renumbered*
    adoptions would call the group unambiguous and rewrite every adopted
    ``#005`` to ``#006``, silently corrupting keeper references into peer
    references. The tokens must keep their digits and both candidates
    (including the kept number) must be recorded.
    """
    from se3.engine.issue_manager import Issue
    from se3.engine.merge.issue_renumber import format_ambiguous_reference_note

    main_root = tmp_path / "main"
    wt_root = tmp_path / "wt"

    main_mgr = IssueManager(main_root)
    main_mgr.create("main base")  # 001
    # The next allocation will be exactly the incoming colliding number.
    (main_root / "se3" / "issues" / ".next_id").write_text("5")

    wt_mgr = IssueManager(wt_root)
    wt_mgr._ensure_dirs()
    for slug, body in (
        ("alpha", "alpha collision issue\n\nSee #005 for details"),
        ("beta", "beta collision issue\n\nSee #005 for details"),
    ):
        issue = Issue(id="005", description=body)
        wt_mgr._write_issue(wt_mgr.open_dir / f"005_{slug}.yaml", issue)

    records = merge_worktree_issues(main_root, wt_root)

    # Both survive; one kept 005, the other took the next fresh number.
    assert len(records) == 2
    assert {r.old_id for r in records} == {"005"}
    assert {r.new_id for r in records} == {"005", "006"}

    adopted = {
        i.description.split(" ", 1)[0]: i
        for i in main_mgr.list_issues(include_closed=True)
        if "collision issue" in i.description
    }
    alpha, beta = adopted["alpha"], adopted["beta"]
    keeper, renamed = (alpha, beta) if alpha.id == "005" else (beta, alpha)
    assert keeper.id == "005"
    assert renamed.id == "006"

    # Neither body's #005 was repointed to the renumbered peer, and each
    # carries the durable note naming BOTH live candidates.
    note_ab = format_ambiguous_reference_note("005", ["005", "006"])
    note_ba = format_ambiguous_reference_note("005", ["006", "005"])
    for issue in (alpha, beta):
        assert "See #005 for details" in issue.description
        assert "See #006 for details" not in issue.description
        assert note_ab in issue.description or note_ba in issue.description
    # Only the renumbered copy carries a trace; the keeper kept its number.
    assert "旧号 #005 → 新号 #006" in renamed.description
    assert "旧号" not in keeper.description

    # Counter sits at max+1 and a re-run folds nothing in again.
    assert _read_next_id(main_root) == 7
    assert merge_worktree_issues(main_root, wt_root) == []


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


def test_cross_references_between_merged_worktree_issues_rewritten(
    tmp_path: Path,
) -> None:
    """A worktree issue referencing a renumbered sibling gets repointed.

    Main keeps its own #001; the worktree's #001 becomes #004, so the
    worktree sibling's ``depends on #001`` must follow it to ``#004`` —
    otherwise the adopted sibling silently points at main's kept #001.
    """
    main_root = tmp_path / "main"
    wt_root = tmp_path / "wt"

    main_mgr = IssueManager(main_root)
    main_mgr.create("main one")    # 001
    main_mgr.create("main two")    # 002
    main_mgr.create("main three")  # 003 -> next_id 004

    wt_mgr = IssueManager(wt_root)
    wt_mgr.create("wt base task")                  # 001 -> renumbered to 004
    wt_mgr.create("wt follow-up, depends on #001")  # 002 -> renumbered to 005

    records = merge_worktree_issues(main_root, wt_root)

    assert [(r.old_id, r.new_id) for r in records] == [
        ("001", "004"), ("002", "005"),
    ]
    follow_up = main_mgr.load("005")
    assert "depends on #004" in follow_up.description
    assert "#001" not in follow_up.description.split("旧号")[0]
    # Both traces survived the batch rewrite untouched.
    assert "旧号 #001 → 新号 #004" in main_mgr.load("004").description
    assert "旧号 #002 → 新号 #005" in follow_up.description
    # Main's own issues never rewritten.
    assert main_mgr.load("001").description == "main one"


def test_batch_renumber_does_not_chain_overlapping_ids(tmp_path: Path) -> None:
    """One member's new ID equal to another member's old ID must not chain.

    Worktree #002 → main #005 and worktree #005 → main #006. A reference to
    worktree #002 must land on #005 and STAY there — a sequential per-issue
    rewrite would drag it onward to #006 when the second pair runs.
    """
    main_mgr = IssueManager(tmp_path / "main")
    for n in range(1, 5):
        main_mgr.create(f"main {n}")  # 001..004 -> next_id 005

    wt_mgr = IssueManager(tmp_path / "wt")
    wt_mgr.create("main 1")                    # 001 — pre-fork copy
    wt_mgr.create("wt two, see #005")          # 002 -> 005 (forward sibling ref)
    wt_mgr.create("main 3")                    # 003 — pre-fork copy
    wt_mgr.create("main 4")                    # 004 — pre-fork copy
    wt_mgr.create("wt five, after #002")       # 005 -> 006 (backward sibling ref)

    records = merge_worktree_issues(tmp_path / "main", tmp_path / "wt")

    assert [(r.old_id, r.new_id) for r in records] == [
        ("002", "005"), ("005", "006"),
    ]
    # Forward ref: worktree #005 became #006.
    assert "wt two, see #006" in main_mgr.load("005").description
    # Backward ref: worktree #002 became #005 — and was NOT chained to #006.
    assert "wt five, after #005" in main_mgr.load("006").description


def test_rerun_idempotent_after_reference_rewrite(tmp_path: Path) -> None:
    """A renumber that rewrote references must not defeat re-run dedup.

    First sync adopts worktree #001 (body ``see #001``) as #004 and rewrites
    the body to ``see #004``. The second sync compares the untouched worktree
    source against the rewritten main copy; the digit difference is vouched
    for by the recorded 001→004 renumber trace, so nothing is re-adopted.
    """
    main_root = tmp_path / "main"
    wt_root = tmp_path / "wt"

    main_mgr = IssueManager(main_root)
    for n in range(1, 4):
        main_mgr.create(f"main {n}")  # 001..003 -> next_id 004

    wt_mgr = IssueManager(wt_root)
    wt_mgr.create("loop task, see #001")  # 001 — self-reference

    first = merge_worktree_issues(main_root, wt_root)
    assert [(r.old_id, r.new_id) for r in first] == [("001", "004")]
    assert "loop task, see #004" in main_mgr.load("004").description

    second = merge_worktree_issues(main_root, wt_root)
    assert second == []
    assert _ids_on_disk(main_mgr) == {"001", "002", "003", "004"}
    assert _read_next_id(main_root) == 5


def test_issue_differing_only_by_reference_is_not_lost(tmp_path: Path) -> None:
    """Dedup must not collapse two issues that differ only by referenced number.

    Main holds ``Fix follow-up for #001``; the worktree creates a genuinely
    different ``Fix follow-up for #002``. No renumber on record maps 002 to
    001, so the worktree issue must be adopted — blanket-masking every
    ``#<digits>`` token would sign the two identically and silently drop it.
    """
    main_root = tmp_path / "main"
    wt_root = tmp_path / "wt"

    main_mgr = IssueManager(main_root)
    main_mgr.create("base one")                  # 001
    main_mgr.create("Fix follow-up for #001")    # 002 -> next_id 003

    wt_mgr = IssueManager(wt_root)
    wt_mgr.create("base one")                    # 001 — pre-fork copy
    wt_mgr.create("Fix follow-up for #002")      # 002 — DIFFERENT issue

    records = merge_worktree_issues(main_root, wt_root)

    # The new issue survived dedup and was adopted under a fresh ID.
    assert [(r.old_id, r.new_id) for r in records] == [("002", "003")]
    adopted = main_mgr.load("003")
    # Its self-reference followed its own renumber (002 -> 003).
    assert "Fix follow-up for #003" in adopted.description
    # Main's similar-but-different issue is untouched.
    assert main_mgr.load("002").description == "Fix follow-up for #001"

    # And the adoption stays idempotent: the recorded 002→003 trace lets the
    # second run recognise the rewritten copy, so nothing is re-adopted.
    assert merge_worktree_issues(main_root, wt_root) == []
    assert _ids_on_disk(main_mgr) == {"001", "002", "003"}


def test_unrelated_trace_pair_does_not_absorb_different_issue(
    tmp_path: Path,
) -> None:
    """A renumber trace on one issue must not vouch for another candidate.

    Main holds an adopted copy tracing 001→004 AND a separate, unrelated
    issue whose body says ``Fix follow-up for #004``. A worktree issue
    ``Fix follow-up for #001`` is genuinely different from that unrelated
    issue: the 001→004 pair belongs to the adopted copy, not to it. Dedup
    must not borrow the pair store-wide and skip the worktree issue.
    """
    main_root = tmp_path / "main"
    wt_root = tmp_path / "wt"

    main_mgr = IssueManager(main_root)
    for n in range(1, 4):
        main_mgr.create(f"main {n}")  # 001..003 -> next_id 004

    wt_mgr = IssueManager(wt_root)
    wt_mgr.create("loop task, see #001")  # wt 001 — self-reference

    # First sync adopts wt 001 as main 004 and records the 001→004 trace.
    first = merge_worktree_issues(main_root, wt_root)
    assert [(r.old_id, r.new_id) for r in first] == [("001", "004")]

    # Unrelated main issue that merely references the renumbered ID.
    main_mgr.create("Fix follow-up for #004")  # main 005
    # Genuinely different worktree issue referencing the OLD number.
    wt_mgr.create("Fix follow-up for #001")    # wt 002

    records = merge_worktree_issues(main_root, wt_root)

    # The worktree issue survived: the 001→004 pair is traced on main 004,
    # not on main 005, so it cannot excuse the #001 vs #004 difference.
    assert [(r.old_id, r.new_id) for r in records] == [("002", "006")]
    assert _ids_on_disk(main_mgr) == {
        "001", "002", "003", "004", "005", "006",
    }
    # No pair in THIS batch maps 1 anywhere, so the adopted body keeps #001;
    # the unrelated main issue is untouched.
    assert "Fix follow-up for #001" in main_mgr.load("006").description
    assert main_mgr.load("005").description == "Fix follow-up for #004"

    # And a third run is still idempotent — nothing gets re-adopted.
    assert merge_worktree_issues(main_root, wt_root) == []
    assert _ids_on_disk(main_mgr) == {
        "001", "002", "003", "004", "005", "006",
    }


def test_same_numbered_unrelated_issue_cannot_borrow_foreign_trace(
    tmp_path: Path,
) -> None:
    """Sharing a numeric ID must not license borrowing a stranger's trace.

    Main holds an adopted copy tracing 001→004 AND an unrelated issue #001
    whose body says ``Fix follow-up for #004``. A worktree (with its own
    independent counter) creates its own #001, ``Fix follow-up for #001`` — a
    genuinely different issue. Numeric-ID equality with the candidate proves
    nothing about provenance, so the store-wide 001→004 pair must not excuse
    the digit difference: the worktree issue must be adopted, not absorbed.
    """
    main_root = tmp_path / "main"
    wt_a = tmp_path / "wt_a"
    wt_b = tmp_path / "wt_b"

    main_mgr = IssueManager(main_root)
    main_mgr.create("Fix follow-up for #004")  # main 001
    main_mgr.create("main 2")                  # 002
    main_mgr.create("main 3")                  # 003 -> next_id 004

    # First worktree's issue collides with main 001 and is adopted as 004,
    # putting the (001, 004) pair on record.
    wt_a_mgr = IssueManager(wt_a)
    wt_a_mgr.create("loop task, see #001")     # wt_a 001
    first = merge_worktree_issues(main_root, wt_a)
    assert [(r.old_id, r.new_id) for r in first] == [("001", "004")]

    # Second worktree independently numbers ITS different issue 001 too.
    wt_b_mgr = IssueManager(wt_b)
    wt_b_mgr.create("Fix follow-up for #001")  # wt_b 001

    records = merge_worktree_issues(main_root, wt_b)

    # The worktree issue survived: main 001 shares its number but is not its
    # adopted copy, so the foreign (001, 004) pair cannot vouch for the
    # #001-vs-#004 difference.
    assert [(r.old_id, r.new_id) for r in records] == [("001", "005")]
    # Its self-reference followed its own renumber; main's issues untouched.
    assert "Fix follow-up for #005" in main_mgr.load("005").description
    assert main_mgr.load("001").description == "Fix follow-up for #004"
    assert _ids_on_disk(main_mgr) == {"001", "002", "003", "004", "005"}

    # Re-run stays idempotent via the newly-recorded (001, 005) trace.
    assert merge_worktree_issues(main_root, wt_b) == []
    assert _ids_on_disk(main_mgr) == {"001", "002", "003", "004", "005"}


def test_adopted_copy_keeping_its_number_still_dedups_on_rerun(
    tmp_path: Path,
) -> None:
    """A same-numbered adopted copy with batch-mate rewrites still dedups.

    The worktree holds #005 (``companion, see #007``) and #007. Adoption
    hands #005 the same number (main's counter is at 005) — no trace — while
    #007 is renumbered to 006, and the batch rewrite turns the companion's
    body into ``see #006``. The re-run must recognise the rewritten copy:
    the (007, 006) pair is vouched by this worktree's own #007, so the
    same-numbered candidate may use it and nothing is re-adopted.
    """
    main_root = tmp_path / "main"
    wt_root = tmp_path / "wt"

    main_mgr = IssueManager(main_root)
    for n in range(1, 5):
        main_mgr.create(f"main {n}")  # 001..004 -> next_id 005

    # Seed the worktree counter so its issue numbers sit at 005 and 007.
    wt_issues_dir = wt_root / "se3" / "issues"
    (wt_issues_dir / "open").mkdir(parents=True)
    (wt_issues_dir / ".next_id").write_text("5")
    wt_mgr = IssueManager(wt_root)
    wt_mgr.create("companion, see #007")  # wt 005
    (wt_issues_dir / ".next_id").write_text("7")
    wt_mgr.create("loop task")            # wt 007

    first = merge_worktree_issues(main_root, wt_root)
    assert [(r.old_id, r.new_id) for r in first] == [
        ("005", "005"), ("007", "006"),
    ]
    # The kept-number copy carries no trace, but its reference followed the
    # batch-mate's renumber.
    assert "companion, see #006" in main_mgr.load("005").description

    # Re-run: the digit difference in the same-numbered copy is excused by
    # the worktree-vouched (007, 006) pair — nothing is re-adopted.
    assert merge_worktree_issues(main_root, wt_root) == []
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


def test_stale_lagging_counter_cannot_mint_duplicate_id(tmp_path: Path) -> None:
    """A ``.next_id`` lagging behind the store never produces two files with one ID.

    Regression: main holds 001..005 while ``.next_id`` was left stale at 5
    (hand-edited, restored from backup, or a botched prior write). Adoption
    used to trust the counter, write a second ``005_*.yaml``, and only then
    advance the counter — leaving two different issues sharing ID 005. The
    allocator must reconcile against the on-disk store BEFORE choosing.
    """
    main_root = tmp_path / "main"
    wt_root = tmp_path / "wt"

    main_mgr = IssueManager(main_root)
    for n in range(5):
        main_mgr.create(f"main {n}")  # 001..005 -> counter 6
    (main_root / "se3" / "issues" / ".next_id").write_text("5")

    wt_mgr = IssueManager(wt_root)
    wt_mgr.create("worktree only issue")  # 001 in the worktree

    records = merge_worktree_issues(main_root, wt_root)

    assert len(records) == 1
    assert records[0].new_id == "006"

    # Nothing lost, and no two issues share a numeric ID.
    assert _ids_on_disk(main_mgr) == {
        "001", "002", "003", "004", "005", "006",
    }
    open_dir = main_root / "se3" / "issues" / "open"
    assert len(list(open_dir.glob("005_*.yaml"))) == 1
    assert len(list(open_dir.glob("006_*.yaml"))) == 1

    # Counter lands at max(ID) + 1.
    assert _read_next_id(main_root) == 7
