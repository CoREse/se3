"""End-to-end repro: a worktree/branch merge must never silently destroy
concurrent uncommitted files in the main repo.

These tests drive the two *call sites* wired in G3 — the ``se3 merge`` fast
path (``merge_cmd._fast_stash_pop``) and the DAG implement leaf-back merge
(``implement._merge_leaf_branch``) — through the shared, archive-first
``resolve_stashpop_safely`` recovery. They reproduce the original defect
(a concurrent untracked file in the main repo gets swept into the pre-merge
stash and lost on an untracked-collision pop) and assert the new invariant:
the file's full content survives in ``tianluo/worktrees/.archive`` and the audit
issue points at it with archive path + blob sha.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tianluo.commands.merge_cmd import _fast_stash_pop, run_merge
from tianluo.engine.stash_utils import ARCHIVE_DIR, ArchivedEntry, StashPopOutcome
from tianluo.engine.steps.implement import _merge_leaf_branch


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=check,
    )


def _init_repo(path: Path) -> str:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    _git(path, "config", "user.email", "test@test.com")
    _git(path, "config", "user.name", "Test")
    # Only the archive sink is ignored; the concurrent tianluo/issues artefacts
    # the repro hinges on must remain *untracked* (not ignored) so
    # ``--include-untracked`` actually sweeps them into the stash.
    (path / ".gitignore").write_text("tianluo/worktrees/\n", encoding="utf-8")
    _git(path, "add", ".gitignore")
    _git(path, "commit", "-m", "init")
    return _git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _archived_files(repo: Path) -> dict[str, str]:
    """Map archived rel-path -> recovered content across all archive runs."""
    root = repo / ARCHIVE_DIR
    out: dict[str, str] = {}
    if not root.exists():
        return out
    for f in root.rglob("*"):
        if f.is_file():
            # rel path inside the per-run ``<ts>_<label>/`` dir
            rel = f.relative_to(root)
            rel_inside = Path(*rel.parts[1:]).as_posix()
            out[rel_inside] = f.read_text(encoding="utf-8")
    return out


def _open_issue_texts(repo: Path) -> list[str]:
    issues_dir = repo / "tianluo" / "issues" / "open"
    if not issues_dir.exists():
        return []
    return [p.read_text(encoding="utf-8") for p in issues_dir.glob("*.yaml")]


def test_fast_merge_path_archives_concurrent_untracked(tmp_path: Path) -> None:
    """``se3 merge`` fast path: a concurrent untracked file in the main repo
    that collides on stash-pop is recovered from the archive, not destroyed,
    and the audit issue points at the archive location."""
    default = _init_repo(tmp_path)

    # Feature branch brings ``data/shared.txt`` (will collide on pop).
    _git(tmp_path, "checkout", "-b", "feature")
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "shared.txt").write_text("FEATURE version\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "feature adds data/shared.txt")
    _git(tmp_path, "checkout", default)

    # Concurrent main-repo work, all uncommitted: one path collides with the
    # incoming file, one is an unrelated new issue file (the 229-style case).
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "data" / "shared.txt").write_text(
        "CONCURRENT main untracked\n", encoding="utf-8"
    )
    issues_open = tmp_path / "tianluo" / "issues" / "open"
    issues_open.mkdir(parents=True)
    (issues_open / "229_concurrent.yaml").write_text(
        "title: concurrent issue 229\n", encoding="utf-8"
    )

    exit_code = run_merge(
        branches=["feature"], strategy="fast", delete_merged=False,
        project_root=tmp_path,
    )
    assert exit_code == 0

    # Merge landed the feature's version on the colliding path.
    assert (tmp_path / "data" / "shared.txt").read_text() == "FEATURE version\n"

    # Both concurrent files are fully recoverable from the archive — nothing
    # was silently destroyed.
    archived = _archived_files(tmp_path)
    assert archived.get("data/shared.txt") == "CONCURRENT main untracked\n"
    assert (
        archived.get("tianluo/issues/open/229_concurrent.yaml")
        == "title: concurrent issue 229\n"
    )

    # The audit issue points at the archive (path + blob sha), not just names.
    texts = _open_issue_texts(tmp_path)
    assert any(
        "stash-pop recovery" in t and "archived:" in t and ARCHIVE_DIR in t
        and "blob sha:" in t
        for t in texts
    ), texts


def test_fast_stash_pop_unit_archives_and_audits(tmp_path: Path) -> None:
    """Direct unit drive of ``_fast_stash_pop``: stash a concurrent untracked
    file, repopulate its path to force a collision, then assert the helper
    archives it and records a recovery audit message."""
    _init_repo(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    # ``data/payload.txt`` is the guaranteed same-path collision; a neutral
    # path (not ``.next_id``, which IssueManager itself rewrites when it files
    # the audit issue) so the merged-tree assertion stays meaningful.
    (data / "payload.txt").write_text("STASHED payload\n", encoding="utf-8")
    issues_open = tmp_path / "tianluo" / "issues" / "open"
    issues_open.mkdir(parents=True)
    (issues_open / "230_x.yaml").write_text("stashed 230\n", encoding="utf-8")

    label = "se3-pre-fast-merge-unit"
    _git(tmp_path, "stash", "push", "--include-untracked", "-m", label)
    # Repopulate the colliding path so pop cannot cleanly restore it. (The
    # stash removed the now-empty untracked dir, so recreate it first.)
    data.mkdir(parents=True, exist_ok=True)
    (data / "payload.txt").write_text("MERGED payload\n", encoding="utf-8")

    audit: list[str] = []
    _fast_stash_pop(tmp_path, label, audit)

    # Merged-tree version untouched; stashed content recovered from archive.
    assert (data / "payload.txt").read_text() == "MERGED payload\n"
    archived = _archived_files(tmp_path)
    assert archived.get("data/payload.txt") == "STASHED payload\n"
    assert archived.get("tianluo/issues/open/230_x.yaml") == "stashed 230\n"
    assert any("recovered safely" in m for m in audit), audit


def test_leaf_merge_path_archives_concurrent_untracked(tmp_path: Path) -> None:
    """Implement leaf-back path: same invariant, same archive sink, same audit
    contract as the fast path — the two merge paths are aligned."""
    default = _init_repo(tmp_path)

    # Leaf branch brings ``app.py`` (collides on pop with main's untracked one).
    _git(tmp_path, "checkout", "-b", "impl/f/G1")
    (tmp_path / "app.py").write_text("LEAF content\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "leaf adds app.py")
    _git(tmp_path, "checkout", default)

    # Concurrent uncommitted main-repo state: a colliding file + an unrelated
    # new issue file.
    (tmp_path / "app.py").write_text("CONCURRENT untracked\n", encoding="utf-8")
    issues_open = tmp_path / "tianluo" / "issues" / "open"
    issues_open.mkdir(parents=True)
    (issues_open / "231_concurrent.yaml").write_text(
        "title: concurrent issue 231\n", encoding="utf-8"
    )

    result = _merge_leaf_branch(
        tmp_path, "impl/f/G1", default,
        task_description="t", group_summaries=[], spec_content="s",
        flow_id="20260630-test",
    )
    assert result is True

    # Merge kept the leaf version on the colliding path.
    assert (tmp_path / "app.py").read_text() == "LEAF content\n"

    # Both concurrent files recoverable from the archive.
    archived = _archived_files(tmp_path)
    assert archived.get("app.py") == "CONCURRENT untracked\n"
    assert (
        archived.get("tianluo/issues/open/231_concurrent.yaml")
        == "title: concurrent issue 231\n"
    )

    # Audit issue carries the archive content pointer, aligned with fast path.
    texts = _open_issue_texts(tmp_path)
    assert any(
        "stash pop conflict recovered" in t and "archived:" in t
        and ARCHIVE_DIR in t and "blob sha:" in t
        for t in texts
    ), texts


def _incomplete_outcome() -> StashPopOutcome:
    """A case-a recovery that archived BOTH sides but left the index unmerged
    (resolver/fallback could not clear the conflict)."""
    return StashPopOutcome(
        archived=[
            ArchivedEntry(
                rel_path="app.py",
                archive_path=f"{ARCHIVE_DIR}/ts_label/app.py",
                blob_sha="deadbeef",
                case="a",
                side="stashed",
            ),
            ArchivedEntry(
                rel_path="app.py",
                archive_path=f"{ARCHIVE_DIR}/ts_label/.head-side/app.py",
                blob_sha="cafef00d",
                case="a",
                side="head",
            ),
        ],
        dropped=False,
        case_a_files=["app.py"],
        archive_failed=True,
        unresolved_files=["app.py"],
    )


def _stash_with_collision(tmp_path: Path) -> str:
    """Stash an untracked file then repopulate its path so a subsequent pop is
    non-clean — giving the caller a live stash to drive recovery over."""
    default = _init_repo(tmp_path)
    (tmp_path / "app.py").write_text("STASHED main\n", encoding="utf-8")
    label = "se3-pre-fast-merge-incomplete"
    _git(tmp_path, "stash", "push", "--include-untracked", "-m", label)
    (tmp_path / "app.py").write_text("REPOPULATED\n", encoding="utf-8")
    return label


def test_fast_stash_pop_signals_incomplete_when_unresolved(tmp_path: Path) -> None:
    """When ``resolve_stashpop_safely`` leaves the index unmerged (archive_failed
    with unresolved paths), ``_fast_stash_pop`` returns True (recovery did NOT
    finalize) and the audit issue still carries the archive manifest pointers."""
    label = _stash_with_collision(tmp_path)

    audit: list[str] = []
    # ``_fast_stash_pop`` imports ``resolve_stashpop_safely`` locally from
    # stash_utils, so the source module is the patch target.
    with patch(
        "tianluo.engine.stash_utils.resolve_stashpop_safely",
        return_value=_incomplete_outcome(),
    ):
        incomplete = _fast_stash_pop(tmp_path, label, audit)

    assert incomplete is True
    # Manifest pointers (archive path + blob sha) are present even though the
    # recovery did not finalize — the operator can still restore.
    texts = _open_issue_texts(tmp_path)
    assert any(
        "INCOMPLETE" in t and "archived:" in t and ARCHIVE_DIR in t
        and "blob sha:" in t and "app.py" in t
        for t in texts
    ), texts


def test_run_merge_fast_reports_failure_on_incomplete_stashpop(
    tmp_path: Path,
) -> None:
    """The fast merge path must NOT return exit 0 over an unreconciled tree:
    when the post-merge stash-pop recovery does not finalize, ``run_merge``
    surfaces a non-zero exit even though the branch merge itself succeeded."""
    default = _init_repo(tmp_path)
    _git(tmp_path, "checkout", "-b", "feature")
    # Feature commits app.py so it collides with main's untracked app.py on the
    # post-merge stash-pop — making the pop non-clean and driving recovery.
    (tmp_path / "app.py").write_text("FEATURE\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "feature commit")
    _git(tmp_path, "checkout", default)

    # Dirty main tree (untracked app.py) so the fast path stashes; the merge
    # then repopulates app.py, forcing a non-clean pop that hits recovery.
    (tmp_path / "app.py").write_text("CONCURRENT\n", encoding="utf-8")

    with patch(
        "tianluo.engine.stash_utils.resolve_stashpop_safely",
        return_value=_incomplete_outcome(),
    ):
        exit_code = run_merge(
            branches=["feature"], strategy="fast", delete_merged=False,
            project_root=tmp_path,
        )

    assert exit_code != 0
    # The feature merge still landed (data was never lost), but success was
    # not reported because the working-tree recovery was incomplete.
    log = _git(tmp_path, "log", "--oneline", default).stdout
    assert "feature commit" in log


def test_leaf_merge_reports_failure_on_incomplete_stashpop(
    tmp_path: Path,
) -> None:
    """The implement leaf-back path must report failure (return False) when the
    post-merge stash-pop recovery does not finalize, so the DAG step status
    reflects the unreconciled main repo instead of silently advancing."""
    default = _init_repo(tmp_path)
    _git(tmp_path, "checkout", "-b", "impl/f/G1")
    (tmp_path / "app.py").write_text("LEAF\n", encoding="utf-8")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "leaf adds app.py")
    _git(tmp_path, "checkout", default)

    # Concurrent untracked file collides on pop -> non-clean pop drives recovery.
    (tmp_path / "app.py").write_text("CONCURRENT\n", encoding="utf-8")

    with patch(
        "tianluo.engine.steps.implement._resolve_stashpop_safely",
        return_value=_incomplete_outcome(),
    ):
        result = _merge_leaf_branch(
            tmp_path, "impl/f/G1", default,
            task_description="t", group_summaries=[], spec_content="s",
            flow_id="20260630-incomplete",
        )

    assert result is False
    # The leaf commit still landed on the default branch (no data loss).
    log = _git(tmp_path, "log", "--oneline", default).stdout
    assert "leaf adds app.py" in log
    # The audit issue carries the manifest pointers and the unmerged path note.
    texts = _open_issue_texts(tmp_path)
    assert any(
        "INCOMPLETE" in t and "archived:" in t and ARCHIVE_DIR in t
        and "blob sha:" in t
        for t in texts
    ), texts
