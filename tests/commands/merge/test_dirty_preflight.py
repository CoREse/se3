"""G2 integration tests for the merge dirty-tracked-file pre-flight.

The pre-flight runs inside ``MergeOrchestrator._execute_inner`` after the
repository-state fail-fast and before the pre-merge SHA is captured. It:

  * auto-commits dirty tracked files when they all live under SE3's
    self-managed data paths (``tianluo/issues/``) so a branch that also touched
    ``tianluo/issues/.next_id`` can actually START its merge and route the
    divergence through ``NextIdResolver`` (max-of-two-counters); and
  * fails loud with ``dirty_working_tree`` when any dirty tracked file lives
    outside those paths, listing the offending files.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from tianluo.commands.merge.failure_reason import FailureReason, from_legacy_string
from tianluo.engine.merge.orchestrator import MergeOrchestrator, MergeReport


def _git(path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True, text=True, check=True,
    )


def _init_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    _git(path, "config", "user.email", "test@test.com")
    _git(path, "config", "user.name", "Test")
    (path / "README.md").write_text("# Test\n")
    # Match the real init/migrate template: ignore tianluo/ runtime but whitelist
    # the committed data dirs so tianluo/issues/ files travel with the branch.
    (path / ".gitignore").write_text(
        "/tianluo/*\n!/tianluo/specs/\n!/tianluo/issues/\n!/tianluo/version-intents/\n"
    )
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial")


def _default_branch(path: Path) -> str:
    return _git(path, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _log_subjects(path: Path) -> str:
    return _git(path, "log", "--format=%s").stdout


def _write_next_id(path: Path, value: str) -> Path:
    p = path / "tianluo" / "issues" / ".next_id"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"{value}\n")
    return p


def test_dirty_issue_state_auto_committed_and_nextid_resolver_takes_over(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)
    _init_repo(tmp_path)
    issues = tmp_path / "tianluo" / "issues"
    _write_next_id(tmp_path, "283")
    _git(tmp_path, "add", "tianluo/issues/.next_id")
    _git(tmp_path, "commit", "-m", "add issue counter")

    default = _default_branch(tmp_path)

    # Branch bumps .next_id higher than the (future) main value.
    _git(tmp_path, "checkout", "-b", "feature")
    _write_next_id(tmp_path, "290")
    _git(tmp_path, "add", "tianluo/issues/.next_id")
    _git(tmp_path, "commit", "-m", "feature allocates ids up to 290")

    _git(tmp_path, "checkout", default)
    # Uncommitted main-side state: a bumped counter (tracked, dirty) plus a
    # freshly-opened issue yaml (untracked) — the exact pairing a session
    # leaves behind between commit steps.
    _write_next_id(tmp_path, "285")
    (issues / "open").mkdir(parents=True, exist_ok=True)
    (issues / "open" / "285_new.yaml").write_text("id: 285\ntitle: new\n")

    orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
    # Prove NextIdResolver — not the LLM — settles the .next_id conflict: make
    # any LLM-resolution attempt an immediate, loud failure. The deterministic
    # short-circuit in _handle_conflict returns before _decider.resolve_and_decide
    # is ever reached when every conflicting path has a mechanical rule, so this
    # boom is dead code on the success path and a tripwire on regression.
    def _boom(*_args, **_kwargs):  # pragma: no cover - only fires on regression
        raise AssertionError(
            "LLM conflict resolution was invoked; NextIdResolver should have "
            "resolved .next_id deterministically without any LLM call"
        )

    monkeypatch.setattr(orch._decider, "resolve_and_decide", _boom)

    report = orch.execute(["feature"])

    assert report.success is True, report.failure_reason
    # The self-managed state was committed before the merge started.
    assert "chore: sync issue state" in _log_subjects(tmp_path)
    # The untracked new issue yaml was swept into the sync commit — assert it
    # is actually TRACKED now, not merely present on disk. A regression that
    # narrows the add pathspec to only the dirty tracked path (e.g.
    # `git add -- tianluo/issues/.next_id`) would leave 285_new.yaml untracked; the
    # weaker exists()/--untracked-files=no pair would pass anyway, so pin the
    # file to the git index instead.
    assert _git(
        tmp_path, "ls-files", "tianluo/issues/open/285_new.yaml"
    ).stdout.strip() == "tianluo/issues/open/285_new.yaml"
    assert _git(
        tmp_path, "status", "--porcelain"
    ).stdout.strip() == ""
    # NextIdResolver settled the .next_id conflict to max(285, 290) — and the
    # _boom tripwire above proves it did so with no LLM call.
    assert (issues / ".next_id").read_text().strip() == "290"


def test_dirty_file_outside_self_managed_fails_loud(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)
    _init_repo(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "foo.py").write_text("x = 1\n")
    _git(tmp_path, "add", "src/foo.py")
    _git(tmp_path, "commit", "-m", "add src")

    default = _default_branch(tmp_path)
    _git(tmp_path, "checkout", "-b", "feature")
    (tmp_path / "feat.txt").write_text("f\n")
    _git(tmp_path, "add", "feat.txt")
    _git(tmp_path, "commit", "-m", "feature change")

    _git(tmp_path, "checkout", default)
    # Dirty tracked file OUTSIDE tianluo/issues/ must block the merge.
    (src / "foo.py").write_text("x = 2\n")

    orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
    report = orch.execute(["feature"])

    assert report.success is False
    assert report.failure_reason == "dirty_working_tree"
    assert "src/foo.py" in (report.failure_detail or "")
    assert report.unattempted_branches == ["feature"]
    # No sync commit, and the merge never ran.
    assert "chore: sync issue state" not in _log_subjects(tmp_path)
    assert "feature change" not in _log_subjects(tmp_path)


def test_mixed_dirty_lists_both_and_fails(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)
    _init_repo(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "foo.py").write_text("x = 1\n")
    _write_next_id(tmp_path, "10")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "add src + counter")

    default = _default_branch(tmp_path)
    _git(tmp_path, "checkout", "-b", "feature")
    (tmp_path / "feat.txt").write_text("f\n")
    _git(tmp_path, "add", "feat.txt")
    _git(tmp_path, "commit", "-m", "feature change")

    _git(tmp_path, "checkout", default)
    (src / "foo.py").write_text("x = 2\n")
    _write_next_id(tmp_path, "12")

    orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
    report = orch.execute(["feature"])

    assert report.success is False
    assert report.failure_reason == "dirty_working_tree"
    detail = report.failure_detail or ""
    assert "src/foo.py" in detail
    assert "tianluo/issues/.next_id" in detail
    # No auto-commit is attempted when any file is outside the whitelist.
    assert "chore: sync issue state" not in _log_subjects(tmp_path)


def test_preexisting_conflict_blocks_merge_not_masked_as_clean(
    tmp_path: Path, monkeypatch
) -> None:
    """A pre-existing unresolved conflict in the main tree must BLOCK, not be
    treated as a clean tree that lets a second merge start on top of it.

    Regression: the pre-flight used to skip porcelain unmerged (U/AA/DD)
    entries, so ``UU src/foo.py`` from an in-flight merge was invisible — the
    tree looked clean and the orchestrator attempted a fresh merge on top of an
    already-conflicted MERGE_HEAD.
    """
    monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)
    _init_repo(tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "foo.py").write_text("base\n")
    _git(tmp_path, "add", "src/foo.py")
    _git(tmp_path, "commit", "-m", "add src")

    default = _default_branch(tmp_path)
    # Branch that will be the merge target of the orchestrator.
    _git(tmp_path, "checkout", "-b", "feature")
    (tmp_path / "feat.txt").write_text("f\n")
    _git(tmp_path, "add", "feat.txt")
    _git(tmp_path, "commit", "-m", "feature change")

    # Build a SEPARATE branch that conflicts with default on src/foo.py, then
    # provoke an unresolved conflict in the main tree by merging it.
    _git(tmp_path, "checkout", default)
    (src / "foo.py").write_text("default-side\n")
    _git(tmp_path, "add", "src/foo.py")
    _git(tmp_path, "commit", "-m", "default edit")
    _git(tmp_path, "checkout", "-b", "conflicting", "HEAD~1")
    (src / "foo.py").write_text("conflicting-side\n")
    _git(tmp_path, "add", "src/foo.py")
    _git(tmp_path, "commit", "-m", "conflicting edit")
    _git(tmp_path, "checkout", default)
    # This merge conflicts and leaves the tree with UU src/foo.py + MERGE_HEAD.
    res = subprocess.run(
        ["git", "-C", str(tmp_path), "merge", "conflicting"],
        capture_output=True, text=True,
    )
    assert res.returncode != 0  # conflict, merge left in progress
    assert (tmp_path / ".git" / "MERGE_HEAD").exists()

    orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
    report = orch.execute(["feature"])

    assert report.success is False
    assert report.failure_reason == "dirty_working_tree"
    assert "src/foo.py" in (report.failure_detail or "")
    assert report.unattempted_branches == ["feature"]
    # The orchestrator must NOT have started/finished the feature merge, and
    # must NOT have aborted the pre-existing conflict merge.
    assert "feature change" not in _log_subjects(tmp_path)
    assert (tmp_path / ".git" / "MERGE_HEAD").exists()


def test_clean_working_tree_unchanged_behaviour(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)
    _init_repo(tmp_path)
    default = _default_branch(tmp_path)

    _git(tmp_path, "checkout", "-b", "feature")
    (tmp_path / "feat.txt").write_text("f\n")
    _git(tmp_path, "add", "feat.txt")
    _git(tmp_path, "commit", "-m", "feature change")

    _git(tmp_path, "checkout", default)
    before = _log_subjects(tmp_path)

    orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
    report = orch.execute(["feature"])

    assert report.success is True, report.failure_reason
    # A clean tree must not produce a sync commit.
    assert "chore: sync issue state" not in _log_subjects(tmp_path)
    assert "feature change" in _log_subjects(tmp_path)
    assert "chore: sync issue state" not in before


def test_untracked_only_does_not_commit(
    tmp_path: Path, monkeypatch
) -> None:
    """Untracked-only issue files must not trigger a sync commit — only
    DIRTY TRACKED files provoke git's 'would be overwritten' refusal, so the
    pre-flight leaves an untracked-only tree exactly as it found it."""
    monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)
    _init_repo(tmp_path)
    default = _default_branch(tmp_path)

    _git(tmp_path, "checkout", "-b", "feature")
    (tmp_path / "feat.txt").write_text("f\n")
    _git(tmp_path, "add", "feat.txt")
    _git(tmp_path, "commit", "-m", "feature change")

    _git(tmp_path, "checkout", default)
    issues = tmp_path / "tianluo" / "issues" / "open"
    issues.mkdir(parents=True, exist_ok=True)
    (issues / "1_new.yaml").write_text("id: 1\n")  # untracked, no tracked dirty

    orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
    report = orch.execute(["feature"])

    assert report.success is True, report.failure_reason
    assert "chore: sync issue state" not in _log_subjects(tmp_path)
    # The untracked file is left untouched.
    assert (issues / "1_new.yaml").exists()


def test_rename_within_self_managed_with_space_is_committed(
    tmp_path: Path,
) -> None:
    """Robust -z porcelain parsing: a staged rename of a space-containing
    path wholly inside tianluo/issues/ is auto-committed (both ends whitelisted)."""
    _init_repo(tmp_path)
    issues = tmp_path / "tianluo" / "issues" / "open"
    issues.mkdir(parents=True, exist_ok=True)
    (issues / "1 old.yaml").write_text("id: 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "add issue with space in name")

    # Staged rename with spaces on both ends, entirely within tianluo/issues/.
    _git(tmp_path, "mv", "tianluo/issues/open/1 old.yaml", "tianluo/issues/open/2 new.yaml")

    orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
    report = MergeReport()
    ok = orch._preflight_dirty_tracked_files(report, ["feature"])

    assert ok is True
    assert "chore: sync issue state" in _log_subjects(tmp_path)
    assert (issues / "2 new.yaml").exists()
    assert not (issues / "1 old.yaml").exists()


def test_rename_out_of_self_managed_fails(tmp_path: Path) -> None:
    """A rename whose destination leaves tianluo/issues/ has one end outside the
    whitelist and must fail rather than be auto-committed."""
    _init_repo(tmp_path)
    issues = tmp_path / "tianluo" / "issues" / "open"
    issues.mkdir(parents=True, exist_ok=True)
    (issues / "1.yaml").write_text("id: 1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "add issue")

    (tmp_path / "src").mkdir()
    _git(tmp_path, "mv", "tianluo/issues/open/1.yaml", "src/1.yaml")

    orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
    report = MergeReport()
    ok = orch._preflight_dirty_tracked_files(report, ["feature"])

    assert ok is False
    assert report.failure_reason == "dirty_working_tree"
    assert "src/1.yaml" in (report.failure_detail or "")
    assert "chore: sync issue state" not in _log_subjects(tmp_path)


def test_dirty_code_index_is_self_managed_and_auto_committed(
    tmp_path: Path, monkeypatch
) -> None:
    """tianluo/code-index.md is rewritten incrementally by flow steps, so it is
    routinely dirty between commit steps. It must be treated as self-managed
    (auto-committed into the sync commit), NOT block the merge — the exact
    state the main repo is in right now."""
    monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)
    _init_repo(tmp_path)
    ci = tmp_path / "tianluo" / "code-index.md"
    ci.parent.mkdir(parents=True, exist_ok=True)
    ci.write_text("# Code Index\n\n- old\n")
    # Whitelist tianluo/code-index.md alongside the runtime ignore so it travels.
    (tmp_path / ".gitignore").write_text(
        "/tianluo/*\n!/tianluo/specs/\n!/tianluo/issues/\n!/tianluo/version-intents/\n"
        "!/tianluo/code-index.md\n"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "add code index")

    default = _default_branch(tmp_path)
    _git(tmp_path, "checkout", "-b", "feature")
    (tmp_path / "feat.txt").write_text("f\n")
    _git(tmp_path, "add", "feat.txt")
    _git(tmp_path, "commit", "-m", "feature change")

    _git(tmp_path, "checkout", default)
    # Dirty (tracked) code-index on the main side — the incremental-rewrite state.
    ci.write_text("# Code Index\n\n- new\n")

    orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
    report = orch.execute(["feature"])

    assert report.success is True, report.failure_reason
    assert "chore: sync issue state" in _log_subjects(tmp_path)
    # No leftover dirty tracked files after the sync commit.
    assert _git(
        tmp_path, "status", "--porcelain", "--untracked-files=no"
    ).stdout.strip() == ""


def test_self_managed_nothing_to_commit_proceeds(
    tmp_path: Path, monkeypatch
) -> None:
    """A self-managed file reported dirty whose staged content equals HEAD
    (edited then restored without unstaging) yields 'nothing to commit' on the
    sync commit. The tree is effectively clean, so the merge must proceed, not
    be rejected with dirty_working_tree."""
    monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)
    _init_repo(tmp_path)
    _write_next_id(tmp_path, "5")
    _git(tmp_path, "add", "tianluo/issues/.next_id")
    _git(tmp_path, "commit", "-m", "add counter")

    default = _default_branch(tmp_path)
    _git(tmp_path, "checkout", "-b", "feature")
    (tmp_path / "feat.txt").write_text("f\n")
    _git(tmp_path, "add", "feat.txt")
    _git(tmp_path, "commit", "-m", "feature change")

    _git(tmp_path, "checkout", default)
    # Stage a change to .next_id, then restore the file content to HEAD without
    # unstaging: git status still reports it (index differs from worktree), but
    # after `git add` the index equals HEAD → commit finds nothing to commit.
    nid = tmp_path / "tianluo" / "issues" / ".next_id"
    nid.write_text("9\n")
    _git(tmp_path, "add", "tianluo/issues/.next_id")
    nid.write_text("5\n")  # restore working-tree content to HEAD

    orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
    report = orch.execute(["feature"])

    assert report.success is True, (
        f"{report.failure_reason} / {report.failure_detail}"
    )
    # No spurious sync commit — there was genuinely nothing to commit.
    assert "chore: sync issue state" not in _log_subjects(tmp_path)
    # The merge actually landed.
    assert "feature change" in _log_subjects(tmp_path)


def test_premerge_failure_not_masked_as_merge_abort_failed(
    tmp_path: Path, monkeypatch
) -> None:
    """Regression for the original masking bug (flow 20260710-101503).

    When ``git merge`` refuses to even START (here: an untracked working-tree
    file the incoming branch would overwrite), no MERGE_HEAD exists, so the
    follow-up ``git merge --abort`` prints "There is no merge to abort
    (MERGE_HEAD missing)." and exits non-zero. The old ``_abort_merge`` read
    that as a failure and overwrote the report's ``failure_reason`` with a
    misleading ``merge_abort_failed`` (``failure_detail`` null), completely
    burying the real cause. The fix treats "no merge to abort" as success so
    the real pre-merge failure reason survives.

    The dirty pre-flight itself uses ``-uno`` and so does NOT intercept an
    untracked-only overwrite — this is deliberately a merge-start failure that
    reaches ``_abort_merge``, not the pre-flight's whitelist path.
    """
    monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)
    _init_repo(tmp_path)
    default = _default_branch(tmp_path)

    # Branch adds a tracked file that will collide with an untracked file of
    # the same name on the main side.
    _git(tmp_path, "checkout", "-b", "feature")
    (tmp_path / "collide.txt").write_text("from-branch\n")
    _git(tmp_path, "add", "collide.txt")
    _git(tmp_path, "commit", "-m", "feature adds collide.txt")

    _git(tmp_path, "checkout", default)
    # Untracked (NOT tracked-dirty) file on main that the merge would overwrite;
    # git refuses to start the merge, and there is no merge to abort.
    (tmp_path / "collide.txt").write_text("untracked-on-main\n")

    orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
    report = orch.execute(["feature"])

    assert report.success is False
    # The real cause is preserved — NOT rewritten to the misleading abort error.
    assert report.failure_reason != "merge_abort_failed"
    assert report.failure_reason_enum is not FailureReason.MERGE_ABORT_FAILED
    # The genuine git failure survives in the (compound) failure_reason so the
    # operator can see what actually happened, rather than a null detail.
    reason, detail = from_legacy_string(report.failure_reason)
    assert reason is FailureReason.FAST_FAILURE
    assert detail and "overwritten by merge" in detail
    # The structured detail contract: failure_detail must also carry the real
    # git diagnostic (not only be embedded in the compound reason string).
    assert report.failure_detail and "overwritten by merge" in report.failure_detail
    # Sanity: the merge never actually landed.
    assert "collide.txt" not in _git(
        tmp_path, "log", "-1", "--name-only", "--format="
    ).stdout or "from-branch" not in (tmp_path / "collide.txt").read_text()


def test_code_index_sibling_is_not_self_managed(
    tmp_path: Path, monkeypatch
) -> None:
    """A file whitelist entry matches ONLY its exact path.

    ``tianluo/code-index.md`` is a FILE entry, so a tracked sibling like
    ``tianluo/code-index.md.bak`` must NOT be classified as self-managed via a
    prefix match — otherwise the whitelist check passes but the sync commit's
    pathspec (which targets the exact file) never stages the sibling, leaving
    it dirty and letting git refuse the merge later. The sibling has to be
    reported loudly as ``dirty_working_tree`` and listed in ``failure_detail``.
    """
    monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)
    _init_repo(tmp_path)
    ci = tmp_path / "tianluo" / "code-index.md"
    ci.parent.mkdir(parents=True, exist_ok=True)
    ci.write_text("# Code Index\n\n- old\n")
    bak = tmp_path / "tianluo" / "code-index.md.bak"
    bak.write_text("backup old\n")
    (tmp_path / ".gitignore").write_text(
        "/tianluo/*\n!/tianluo/specs/\n!/tianluo/issues/\n!/tianluo/version-intents/\n"
        "!/tianluo/code-index.md\n!/tianluo/code-index.md.bak\n"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "add code index and backup")

    default = _default_branch(tmp_path)
    _git(tmp_path, "checkout", "-b", "feature")
    (tmp_path / "feat.txt").write_text("f\n")
    _git(tmp_path, "add", "feat.txt")
    _git(tmp_path, "commit", "-m", "feature change")

    _git(tmp_path, "checkout", default)
    # Only the .bak sibling is dirty — code-index.md itself is untouched.
    bak.write_text("backup new\n")

    orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
    report = orch.execute(["feature"])

    assert report.success is False
    assert report.failure_reason == "dirty_working_tree"
    assert report.failure_detail and "tianluo/code-index.md.bak" in report.failure_detail
    # No sync commit, and the sibling is untouched (never staged/committed).
    assert "chore: sync issue state" not in _log_subjects(tmp_path)
    assert bak.read_text() == "backup new\n"


def test_gitignored_code_index_does_not_abort_issue_sync(
    tmp_path: Path, monkeypatch
) -> None:
    """An existing-but-gitignored whitelist file must not abort the sync.

    On a pre-migrate ``.gitignore`` (``/tianluo/*`` without ``!/tianluo/code-index.md``)
    the code-index exists on disk built by ``se3 code-index rebuild`` but is
    ignored+untracked. When the only dirty tracked file is
    ``tianluo/issues/.next_id``, the auto-commit must NOT hand git the ignored
    pathspec (which fails with "paths are ignored"): the target list is derived
    from the dirty tracked paths, so only ``tianluo/issues`` is added and the sync
    commit succeeds.
    """
    monkeypatch.delenv("SE3_TEST_RUNNING", raising=False)
    _init_repo(tmp_path)  # .gitignore here does NOT whitelist code-index.md
    _write_next_id(tmp_path, "3")
    _git(tmp_path, "add", "tianluo/issues/.next_id")
    _git(tmp_path, "commit", "-m", "add counter")

    # code-index.md exists on disk but is gitignored+untracked.
    ci = tmp_path / "tianluo" / "code-index.md"
    ci.write_text("# Code Index\n\n- built but ignored\n")

    default = _default_branch(tmp_path)
    _git(tmp_path, "checkout", "-b", "feature")
    (tmp_path / "feat.txt").write_text("f\n")
    _git(tmp_path, "add", "feat.txt")
    _git(tmp_path, "commit", "-m", "feature change")

    _git(tmp_path, "checkout", default)
    _write_next_id(tmp_path, "5")  # dirty tracked, self-managed

    orch = MergeOrchestrator(project_root=tmp_path, delete_merged=False)
    report = orch.execute(["feature"])

    assert report.success is True, (
        f"{report.failure_reason} / {report.failure_detail}"
    )
    assert "chore: sync issue state" in _log_subjects(tmp_path)
    assert "feature change" in _log_subjects(tmp_path)
    # The ignored code-index was left untracked, never dragged into the commit.
    assert ci.read_text() == "# Code Index\n\n- built but ignored\n"
