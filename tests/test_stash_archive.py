"""Tests for the no-data-loss archive foundation in ``stash_utils``.

G1 builds the primitive that extracts a live (failed-to-pop) stash's full
content and persists it under ``se3/worktrees/.archive/`` *before* anything
is dropped. These tests drive ``_resolve_stash_ref`` and
``archive_stash_payload`` against real temporary git repos.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from se3.engine.stash_utils import (
    ARCHIVE_DIR,
    StashPopOutcome,
    archive_stash_payload,
    pop_stash_by_label,
    resolve_stashpop_safely,
    _resolve_stash_ref,
)
# The LLM-aware case-a resolver now lives in its own integration-layer module
# (stash_utils is kept LLM-free); the G3 tests below drive it from there.
from se3.engine.stashpop_llm_resolver import make_llm_stashpop_resolver


def _git_nocheck(path: Path, *args: str) -> subprocess.CompletedProcess:
    """Run git WITHOUT ``check`` — used for the deliberately-failing
    ``git stash pop`` whose non-zero exit is the input under test."""
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
    )


def _git(path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    _git(path, "config", "user.email", "test@test.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("base\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "init")


def _hash_object(path: Path, file_rel: str) -> str:
    return _git(path, "hash-object", file_rel).stdout.strip()


def test_resolve_stash_ref_matches_label_not_position(tmp_path: Path) -> None:
    """With several stashes present, the live ref is resolved by message,
    never by blindly grabbing ``stash@{0}``."""
    _init_repo(tmp_path)

    # First stash (oldest -> ends up at a higher index).
    (tmp_path / "a.txt").write_text("a\n")
    _git(tmp_path, "stash", "push", "--include-untracked", "-m", "label-A")
    # Second stash, pushed on top -> stash@{0}.
    (tmp_path / "b.txt").write_text("b\n")
    _git(tmp_path, "stash", "push", "--include-untracked", "-m", "label-B")

    ref_a = _resolve_stash_ref(tmp_path, "label-A")
    ref_b = _resolve_stash_ref(tmp_path, "label-B")
    assert ref_b == "stash@{0}"
    assert ref_a == "stash@{1}"  # matched by label, not position
    assert _resolve_stash_ref(tmp_path, "does-not-exist") is None


def test_archive_stash_payload_untracked_and_tracked(tmp_path: Path) -> None:
    """Both untracked (``.next_id``, ``NNN_*.yaml``) and tracked working-tree
    changes are persisted under ``se3/worktrees/.archive/<ts>_<label>/`` with
    their original relative paths, complete content, and a verifiable blob
    sha."""
    _init_repo(tmp_path)

    # Tracked change: modify README. Untracked: simulate concurrent
    # discovery artefacts in a nested issues dir.
    (tmp_path / "README.md").write_text("modified base\n")
    issues = tmp_path / "se3" / "issues" / "open"
    issues.mkdir(parents=True)
    (tmp_path / "se3" / "issues" / ".next_id").write_text("232\n")
    (issues / "229_concurrent.yaml").write_text("title: concurrent issue 229\n")

    # Capture expected blob shas while the files are still on disk.
    sha_readme = _hash_object(tmp_path, "README.md")
    sha_next_id = _hash_object(tmp_path, "se3/issues/.next_id")
    sha_issue = _hash_object(tmp_path, "se3/issues/open/229_concurrent.yaml")

    label = "se3-pre-fast-merge-test"
    _git(tmp_path, "stash", "push", "--include-untracked", "-m", label)
    # Stash succeeded -> live stash entry, clean tree.
    assert _git(tmp_path, "status", "--porcelain").stdout == ""

    entries = archive_stash_payload(tmp_path, label, timestamp="20260630T120000")

    by_rel = {e.rel_path: e for e in entries}
    assert set(by_rel) == {
        "README.md",
        "se3/issues/.next_id",
        "se3/issues/open/229_concurrent.yaml",
    }

    archive_root = tmp_path / ARCHIVE_DIR / f"20260630T120000_{label}"

    # README is a tracked working-tree change -> case "a".
    readme_entry = by_rel["README.md"]
    assert readme_entry.case == "a"
    assert readme_entry.blob_sha == sha_readme
    assert (tmp_path / readme_entry.archive_path).read_text() == "modified base\n"

    # Untracked concurrent files -> case "b", content recoverable verbatim.
    next_id_entry = by_rel["se3/issues/.next_id"]
    assert next_id_entry.case == "b"
    assert next_id_entry.blob_sha == sha_next_id
    assert (archive_root / "se3" / "issues" / ".next_id").read_text() == "232\n"

    issue_entry = by_rel["se3/issues/open/229_concurrent.yaml"]
    assert issue_entry.case == "b"
    assert issue_entry.blob_sha == sha_issue
    assert (
        (archive_root / "se3" / "issues" / "open" / "229_concurrent.yaml").read_text()
        == "title: concurrent issue 229\n"
    )

    # archive_path is project-root-relative and lands under the gitignored sink.
    assert issue_entry.archive_path.startswith(ARCHIVE_DIR + "/")


def test_archive_stash_payload_missing_label_raises(tmp_path: Path) -> None:
    """No live stash for the label -> refuse to proceed (so the caller never
    drops without recovery proof)."""
    _init_repo(tmp_path)
    with pytest.raises(RuntimeError):
        archive_stash_payload(tmp_path, "no-such-label", timestamp="20260630T120000")


def test_archive_stash_payload_untracked_only(tmp_path: Path) -> None:
    """A stash with no tracked changes (only ``--include-untracked`` files)
    still archives cleanly — the missing ``^1..ref`` diff is not an error."""
    _init_repo(tmp_path)
    (tmp_path / "scratch.txt").write_text("only untracked\n")
    label = "untracked-only"
    _git(tmp_path, "stash", "push", "--include-untracked", "-m", label)

    entries = archive_stash_payload(tmp_path, label, timestamp="20260630T130000")
    assert [e.rel_path for e in entries] == ["scratch.txt"]
    assert entries[0].case == "b"
    archived = tmp_path / entries[0].archive_path
    assert archived.read_text() == "only untracked\n"


def test_archive_stash_payload_skips_stash_deleted_path(tmp_path: Path) -> None:
    """A path the stash *deleted* (uncommitted deletion of a tracked file at
    merge time) has no content at ``<ref>:<path>`` to recover, so it must be
    skipped during archival rather than raising — otherwise a real, common
    case (deletion + an unrelated untracked collision) would spuriously refuse
    to drop and emit a false data-loss alarm. The still-present content is
    archived normally."""
    _init_repo(tmp_path)
    # A committed tracked file we then delete (uncommitted) before stashing.
    (tmp_path / "gone.txt").write_text("to be deleted\n")
    _git(tmp_path, "add", "gone.txt")
    _git(tmp_path, "commit", "-m", "add gone.txt")
    (tmp_path / "gone.txt").unlink()
    # An untracked concurrent file that still has recoverable content.
    (tmp_path / "kept.txt").write_text("still here\n")
    sha_kept = _hash_object(tmp_path, "kept.txt")

    label = "with-deletion"
    _git(tmp_path, "stash", "push", "--include-untracked", "-m", label)

    # Must not raise even though gone.txt is a deletion with no content.
    entries = archive_stash_payload(tmp_path, label, timestamp="20260630T133000")

    by_rel = {e.rel_path: e for e in entries}
    # The deleted path is absent (nothing recoverable); the kept file archived.
    assert "gone.txt" not in by_rel
    assert by_rel["kept.txt"].case == "b"
    assert by_rel["kept.txt"].blob_sha == sha_kept
    assert (tmp_path / by_rel["kept.txt"].archive_path).read_text() == "still here\n"


def test_resolve_stashpop_drops_despite_stash_deleted_tracked_file(
    tmp_path: Path,
) -> None:
    """End-to-end of the deletion regression: an uncommitted tracked-file
    deletion plus a ``.next_id`` untracked collision yields a non-clean pop;
    archival must skip the deletion, prove the rest recoverable, and drop the
    labeled stash (no false 'data-loss-risk' refusal)."""
    _init_repo(tmp_path)
    (tmp_path / "obsolete.txt").write_text("old tracked content\n")
    _git(tmp_path, "add", "obsolete.txt")
    _git(tmp_path, "commit", "-m", "add obsolete")

    issues = tmp_path / "se3" / "issues"
    issues.mkdir(parents=True)
    # Uncommitted deletion of a tracked file alongside an untracked collision.
    (tmp_path / "obsolete.txt").unlink()
    (issues / ".next_id").write_text("STASHED next_id\n")

    label = "deletion-and-collision"
    _git(tmp_path, "stash", "push", "--include-untracked", "-m", label)
    # The merge repopulates the colliding untracked path.
    issues.mkdir(parents=True, exist_ok=True)
    (issues / ".next_id").write_text("MERGED next_id\n")

    pop = _git_nocheck(tmp_path, "stash", "pop")
    assert pop.returncode != 0

    outcome = resolve_stashpop_safely(
        tmp_path, label, pop, timestamp="20260630T134500",
    )

    assert outcome.archive_failed is False
    assert outcome.dropped is True
    assert _resolve_stash_ref(tmp_path, label) is None
    # The untracked collision content is recoverable; the deletion is skipped.
    by_rel = {e.rel_path: e for e in outcome.archived}
    assert "se3/issues/.next_id" in by_rel
    assert "obsolete.txt" not in by_rel


def test_archive_stash_payload_same_second_does_not_overwrite(
    tmp_path: Path,
) -> None:
    """Two recoveries of the SAME label within one wall-clock second must not
    clobber each other: the second lands in a ``-2`` run dir so the first
    audit issue's pointer keeps matching the bytes it recorded."""
    _init_repo(tmp_path)
    # Mirror production, where ARCHIVE_DIR is gitignored, so the second
    # ``--include-untracked`` stash does not re-sweep the first run's archive.
    (tmp_path / ".gitignore").write_text(ARCHIVE_DIR + "/\n")
    _git(tmp_path, "add", ".gitignore")
    _git(tmp_path, "commit", "-m", "ignore archive")
    ts = "20260630T190000"
    label = "se3-pre-merge-dup"

    # First recovery: stash content v1, archive it, then drop the live stash.
    (tmp_path / "note.txt").write_text("FIRST recovery content\n")
    _git(tmp_path, "stash", "push", "--include-untracked", "-m", label)
    first = archive_stash_payload(tmp_path, label, timestamp=ts)
    assert len(first) == 1
    first_path = tmp_path / first[0].archive_path
    assert first_path.read_text() == "FIRST recovery content\n"
    _git(tmp_path, "stash", "drop", _resolve_stash_ref(tmp_path, label))

    # Second recovery, same label + same timestamp, different content.
    (tmp_path / "note.txt").write_text("SECOND recovery content\n")
    _git(tmp_path, "stash", "push", "--include-untracked", "-m", label)
    second = archive_stash_payload(tmp_path, label, timestamp=ts)
    assert len(second) == 1
    second_path = tmp_path / second[0].archive_path

    # Distinct run dirs; the first's bytes are intact and the second is kept
    # independently (no overwrite).
    assert first[0].archive_path != second[0].archive_path
    assert f"{ts}_{label}-2" in second[0].archive_path
    assert first_path.read_text() == "FIRST recovery content\n"
    assert second_path.read_text() == "SECOND recovery content\n"


def test_pop_stash_by_label_pops_labeled_not_top(tmp_path: Path) -> None:
    """A bare ``git stash pop`` applies ``stash@{0}``; ``pop_stash_by_label``
    must resolve and pop the labeled entry even when an unrelated stash sits on
    top, leaving the unrelated one untouched."""
    _init_repo(tmp_path)
    # Our labeled stash first -> ends up deeper once another lands on top.
    (tmp_path / "ours.txt").write_text("OURS\n")
    label = "se3-pre-merge-target"
    _git(tmp_path, "stash", "push", "--include-untracked", "-m", label)
    # Unrelated concurrent stash on top -> stash@{0}.
    (tmp_path / "unrelated.txt").write_text("UNRELATED\n")
    _git(tmp_path, "stash", "push", "--include-untracked", "-m", "unrelated")

    pop = pop_stash_by_label(tmp_path, label)
    assert pop.returncode == 0
    # Our content restored; the unrelated stash@{0} is untouched.
    assert (tmp_path / "ours.txt").read_text() == "OURS\n"
    assert not (tmp_path / "unrelated.txt").exists()
    assert _resolve_stash_ref(tmp_path, label) is None
    assert _resolve_stash_ref(tmp_path, "unrelated") == "stash@{0}"


def test_pop_stash_by_label_missing_label_returns_nonzero(tmp_path: Path) -> None:
    """An unresolvable label yields a synthetic non-clean result (escalation),
    never a bare pop that would consume an unrelated ``stash@{0}``."""
    _init_repo(tmp_path)
    (tmp_path / "unrelated.txt").write_text("UNRELATED\n")
    _git(tmp_path, "stash", "push", "--include-untracked", "-m", "unrelated")

    pop = pop_stash_by_label(tmp_path, "no-such-label")
    assert pop.returncode != 0
    # The unrelated stash@{0} was NOT applied or dropped.
    assert _resolve_stash_ref(tmp_path, "unrelated") == "stash@{0}"
    assert not (tmp_path / "unrelated.txt").exists()


# ---------------------------------------------------------------------------
# G2: resolve_stashpop_safely — case a / case b dispositions, no data loss.
# ---------------------------------------------------------------------------


def test_resolve_stashpop_clean_pop_is_noop(tmp_path: Path) -> None:
    """A clean pop (git already dropped the stash) archives nothing and
    drops nothing — the helper must not touch a healthy tree."""
    _init_repo(tmp_path)
    clean = subprocess.CompletedProcess(args=["git"], returncode=0, stdout="", stderr="")
    outcome = resolve_stashpop_safely(
        tmp_path, "whatever", clean, timestamp="20260630T120000",
    )
    assert isinstance(outcome, StashPopOutcome)
    assert outcome.archived == []
    assert outcome.dropped is False
    assert outcome.archive_failed is False
    assert outcome.case_a_files == [] and outcome.case_b_files == []
    # No archive sink was created for a clean pop.
    assert not (tmp_path / ARCHIVE_DIR).exists()


def test_resolve_stashpop_case_b_untracked_collision(tmp_path: Path) -> None:
    """case b: a concurrent untracked file collides on pop. Its stashed
    content must be archived (recoverable), the merged working-tree version
    left untouched, NO take-ours, and the stash dropped only after archival."""
    _init_repo(tmp_path)
    issues = tmp_path / "se3" / "issues"
    (issues / "open").mkdir(parents=True)
    # ``.next_id`` is the path the merge will repopulate -> guaranteed collision.
    (issues / ".next_id").write_text("STASHED next_id\n")
    (issues / "open" / "229_concurrent.yaml").write_text("stashed 229\n")

    label = "se3-pre-fast-merge-b"
    _git(tmp_path, "stash", "push", "--include-untracked", "-m", label)
    assert _git(tmp_path, "status", "--porcelain").stdout == ""

    # Simulate the merge populating the same path with the merged content.
    issues.mkdir(parents=True, exist_ok=True)
    (issues / ".next_id").write_text("MERGED next_id\n")

    pop = _git_nocheck(tmp_path, "stash", "pop")
    assert pop.returncode != 0  # untracked collision keeps the stash live

    outcome = resolve_stashpop_safely(
        tmp_path, label, pop, timestamp="20260630T140000",
    )

    # Merged tree version preserved verbatim — case b never take-ours/overwrites.
    assert (issues / ".next_id").read_text() == "MERGED next_id\n"

    by_rel = {e.rel_path: e for e in outcome.archived}
    # Both stashed untracked files are recoverable from the archive.
    assert by_rel["se3/issues/.next_id"].case == "b"
    assert by_rel["se3/issues/open/229_concurrent.yaml"].case == "b"
    archive_root = tmp_path / ARCHIVE_DIR / f"20260630T140000_{label}"
    assert (archive_root / "se3" / "issues" / ".next_id").read_text() == "STASHED next_id\n"
    assert (
        (archive_root / "se3" / "issues" / "open" / "229_concurrent.yaml").read_text()
        == "stashed 229\n"
    )

    # Classified as case b, no 3-way conflict, dropped after recovery proven.
    assert "se3/issues/.next_id" in outcome.case_b_files
    assert outcome.case_a_files == []
    assert outcome.dropped is True
    assert _resolve_stash_ref(tmp_path, label) is None  # stash gone post-drop


def _setup_case_a(tmp_path: Path, label: str) -> subprocess.CompletedProcess:
    """Build a real 3-way tracked conflict on ``app.py`` and return the
    (failed) ``git stash pop`` result with the stash still live."""
    _init_repo(tmp_path)
    (tmp_path / "app.py").write_text("l1\nORIG\nl3\n")
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "commit", "-m", "add app")
    # Dirty working-tree change -> stash it.
    (tmp_path / "app.py").write_text("l1\nSTASHED\nl3\n")
    _git(tmp_path, "stash", "push", "-m", label)
    # Merged HEAD touches the same line differently -> 3-way conflict on pop.
    (tmp_path / "app.py").write_text("l1\nMERGED\nl3\n")
    _git(tmp_path, "add", "app.py")
    _git(tmp_path, "commit", "-m", "merged change")
    pop = _git_nocheck(tmp_path, "stash", "pop")
    assert pop.returncode != 0
    return pop


def test_resolve_stashpop_case_a_default_fallback_take_ours(tmp_path: Path) -> None:
    """case a with no resolver: safe fallback take-ours keeps the merged
    version in the tree, but the discarded stashed side is archived."""
    label = "pre-merge-a"
    pop = _setup_case_a(tmp_path, label)

    outcome = resolve_stashpop_safely(
        tmp_path, label, pop, timestamp="20260630T150000",
    )

    assert outcome.case_a_files == ["app.py"]
    assert outcome.case_b_files == []

    # BOTH sides of the tracked conflict are recoverable: the stashed
    # (theirs) side AND the merged HEAD (ours) side — regardless of which one
    # the resolution discards.
    a_entries = [e for e in outcome.archived if e.rel_path == "app.py"]
    by_side = {e.side: e for e in a_entries}
    assert set(by_side) == {"stashed", "head"}
    assert all(e.case == "a" for e in a_entries)
    stashed_text = (tmp_path / by_side["stashed"].archive_path).read_text()
    head_text = (tmp_path / by_side["head"].archive_path).read_text()
    assert "STASHED" in stashed_text and "MERGED" not in stashed_text
    assert "MERGED" in head_text and "STASHED" not in head_text

    # Default fallback resolved the working tree to ours (the merged HEAD).
    tree_text = (tmp_path / "app.py").read_text()
    assert "MERGED" in tree_text
    assert "<<<<<<<" not in tree_text  # conflict markers resolved
    assert outcome.dropped is True


def test_resolve_stashpop_case_a_uses_injected_resolver(tmp_path: Path) -> None:
    """case a with a resolver: the resolver decides the working-tree result,
    yet the discarded side is still archived for recovery."""
    label = "pre-merge-a-resolver"
    pop = _setup_case_a(tmp_path, label)

    calls: list[list[str]] = []

    def resolver(root: Path, files: list[str]) -> None:
        calls.append(list(files))
        (root / "app.py").write_text("RESOLVED BY LLM\n")
        _git(root, "add", "app.py")

    outcome = resolve_stashpop_safely(
        tmp_path, label, pop, timestamp="20260630T160000",
        conflict_resolver=resolver,
    )

    assert calls == [["app.py"]]
    assert (tmp_path / "app.py").read_text() == "RESOLVED BY LLM\n"
    # Even with a custom resolution that discards BOTH original sides, each
    # side remains recoverable from the manifest.
    by_side = {
        e.side: e for e in outcome.archived if e.rel_path == "app.py"
    }
    assert set(by_side) == {"stashed", "head"}
    assert "STASHED" in (tmp_path / by_side["stashed"].archive_path).read_text()
    assert "MERGED" in (tmp_path / by_side["head"].archive_path).read_text()
    assert outcome.dropped is True


def test_resolve_stashpop_case_a_resolver_keeping_stashed_archives_head(
    tmp_path: Path,
) -> None:
    """Medium-severity regression: a resolver that KEEPS the stashed side (and
    thus discards the merged HEAD side) must still leave the HEAD content
    recoverable from the manifest — not silently lost."""
    label = "pre-merge-a-keep-stashed"
    pop = _setup_case_a(tmp_path, label)

    def keep_stashed(root: Path, files: list[str]) -> None:
        # Resolve toward the stashed (theirs) version, discarding HEAD.
        (root / "app.py").write_text("l1\nSTASHED\nl3\n")
        _git(root, "add", "app.py")

    outcome = resolve_stashpop_safely(
        tmp_path, label, pop, timestamp="20260630T161500",
        conflict_resolver=keep_stashed,
    )

    # Working tree now holds the stashed resolution.
    assert "STASHED" in (tmp_path / "app.py").read_text()

    by_side = {
        e.side: e for e in outcome.archived if e.rel_path == "app.py"
    }
    # The discarded HEAD (merged) side is archived and recoverable.
    assert "head" in by_side
    assert "MERGED" in (tmp_path / by_side["head"].archive_path).read_text()
    assert outcome.dropped is True


def test_resolve_stashpop_case_a_unresolved_keeps_stash(tmp_path: Path) -> None:
    """High-severity regression: a resolver that returns WITHOUT staging a
    resolution leaves the case-a path unmerged. The helper must NOT drop the
    stash in that state — it keeps the live stash (the only direct handle for
    re-applying) and signals failure, even though both sides were archived."""
    label = "pre-merge-a-unresolved"
    pop = _setup_case_a(tmp_path, label)

    def noop_resolver(root: Path, files: list[str]) -> None:
        # Deliberately do nothing: the conflict stays unmerged in the index.
        pass

    outcome = resolve_stashpop_safely(
        tmp_path, label, pop, timestamp="20260630T162000",
        conflict_resolver=noop_resolver,
    )

    # Recovery was not finalized: stash kept, failure flagged, but content
    # was still archived (both sides) so nothing is lost.
    assert outcome.dropped is False
    assert outcome.archive_failed is True
    # The unmerged path is surfaced so the merge integration can refuse to
    # report a clean success over a conflicted index.
    assert outcome.unresolved_files == ["app.py"]
    assert _resolve_stash_ref(tmp_path, label) is not None  # live stash kept
    by_side = {e.side: e for e in outcome.archived if e.rel_path == "app.py"}
    assert set(by_side) == {"stashed", "head"}
    assert "STASHED" in (tmp_path / by_side["stashed"].archive_path).read_text()
    assert "MERGED" in (tmp_path / by_side["head"].archive_path).read_text()
    # The path is genuinely still unmerged (the precondition that blocked drop).
    from se3.engine.worktree import get_conflicting_files
    assert "app.py" in get_conflicting_files(tmp_path)


def test_resolve_stashpop_drops_only_labeled_stash(tmp_path: Path) -> None:
    """Critical-severity regression: when the labeled stash is NOT at
    ``stash@{0}`` (a concurrent unrelated stash sits on top), the helper must
    drop exactly the labeled entry — never the unrelated ``stash@{0}``, which
    was neither archived nor confirmed recoverable."""
    _init_repo(tmp_path)

    # Labeled stash (our pre-merge stash) pushed first -> ends up deeper once
    # another stash lands on top.
    (tmp_path / "ours.txt").write_text("OUR stashed content\n")
    label = "se3-pre-fast-merge-target"
    _git(tmp_path, "stash", "push", "--include-untracked", "-m", label)

    # An unrelated, concurrent stash pushed afterwards -> stash@{0}.
    (tmp_path / "unrelated.txt").write_text("UNRELATED uncommitted\n")
    _git(tmp_path, "stash", "push", "--include-untracked", "-m", "unrelated")

    assert _resolve_stash_ref(tmp_path, "unrelated") == "stash@{0}"
    assert _resolve_stash_ref(tmp_path, label) == "stash@{1}"

    # A synthetic non-clean pop result drives the recovery path for the
    # labeled stash (which is still live at stash@{1}).
    pop = subprocess.CompletedProcess(
        args=["git", "stash", "pop"], returncode=1,
        stdout="ours.txt: already exists, no checkout", stderr="",
    )
    outcome = resolve_stashpop_safely(
        tmp_path, label, pop, timestamp="20260630T162500",
    )

    assert outcome.dropped is True
    # The labeled stash is gone; the unrelated stash survives intact and its
    # content is still fully recoverable from the git object.
    assert _resolve_stash_ref(tmp_path, label) is None
    unrelated_ref = _resolve_stash_ref(tmp_path, "unrelated")
    assert unrelated_ref is not None
    recovered = _git(
        tmp_path, "show", f"{unrelated_ref}^3:unrelated.txt",
    ).stdout
    assert recovered == "UNRELATED uncommitted\n"


def test_resolve_stashpop_refuses_drop_when_archive_unconfirmed(tmp_path: Path) -> None:
    """If archival cannot be proven (no live stash to read), the helper must
    NOT drop — it keeps the stash and flags the failure instead of losing data."""
    _init_repo(tmp_path)
    fake_pop = subprocess.CompletedProcess(
        args=["git", "stash", "pop"], returncode=1, stdout="", stderr="boom",
    )
    outcome = resolve_stashpop_safely(
        tmp_path, "no-such-stash", fake_pop, timestamp="20260630T170000",
    )
    assert outcome.archive_failed is True
    assert outcome.dropped is False
    assert outcome.archived == []


# ---------------------------------------------------------------------------
# G3: make_llm_stashpop_resolver — LLM-as-editor for case a, safe fallback.
# ---------------------------------------------------------------------------


def test_llm_resolver_applies_llm_output_and_stages(tmp_path, monkeypatch) -> None:
    """The injected resolver sends conflict-markered content to the LLM and
    writes back the reconciled result (no commit, just a staged change)."""
    label = "pre-merge-a-llm"
    pop = _setup_case_a(tmp_path, label)

    class _StubCaller:
        def __init__(self, *a, **k):
            pass

        def call(self, prompt: str) -> str:
            # The prompt must carry the conflict-markered buffer.
            assert "<<<<<<<" in prompt
            return "l1\nLLM-RECONCILED\nl3\n"

    monkeypatch.setattr("se3.engine.llm_caller.LLMCaller", _StubCaller)

    outcome = resolve_stashpop_safely(
        tmp_path, label, pop, timestamp="20260630T180000",
        conflict_resolver=make_llm_stashpop_resolver(context="ctx"),
    )

    tree_text = (tmp_path / "app.py").read_text()
    assert "LLM-RECONCILED" in tree_text
    assert "<<<<<<<" not in tree_text
    # Both original sides still archived for recovery despite the LLM choice.
    by_side = {e.side: e for e in outcome.archived if e.rel_path == "app.py"}
    assert set(by_side) == {"stashed", "head"}
    assert outcome.dropped is True


def test_llm_resolver_falls_back_to_take_ours_on_llm_failure(
    tmp_path, monkeypatch,
) -> None:
    """When the LLM is unavailable/raises, the resolver must not leave the tree
    conflicted — it falls back to deterministic take-ours (merged HEAD), which
    is safe because both sides are already archived."""
    label = "pre-merge-a-llmfail"
    pop = _setup_case_a(tmp_path, label)

    class _BoomCaller:
        def __init__(self, *a, **k):
            pass

        def call(self, prompt: str) -> str:
            raise RuntimeError("LLM unavailable")

    monkeypatch.setattr("se3.engine.llm_caller.LLMCaller", _BoomCaller)

    outcome = resolve_stashpop_safely(
        tmp_path, label, pop, timestamp="20260630T181000",
        conflict_resolver=make_llm_stashpop_resolver(),
    )

    tree_text = (tmp_path / "app.py").read_text()
    assert "MERGED" in tree_text  # take-ours fallback kept the merged HEAD
    assert "<<<<<<<" not in tree_text
    # The discarded stashed side is still recoverable.
    by_side = {e.side: e for e in outcome.archived if e.rel_path == "app.py"}
    assert "STASHED" in (tmp_path / by_side["stashed"].archive_path).read_text()
    assert outcome.dropped is True
