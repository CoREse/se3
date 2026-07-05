"""Tests for `se3 merge` source-issue backfill (G2).

Covers the "merge succeeded → source issue resolved" choke point implemented
in ``run_merge``:

* ``find_worktree_source_issue_by_branch`` (run.py) — maps a branch back to
  the ``source_issue_id`` recorded in a live OR archived worktree engine.json,
  including COMPLETED flows (which ``find_resumable_worktree_runs`` excludes).
* ``_map_branches_to_source_issues`` — captures the branch→issue map BEFORE
  the merge deletes the worktree.
* ``_backfill_resolved_source_issues`` — idempotently transitions only
  IN_PROGRESS source issues to RESOLVED (req 1 resolve half + req 2 retry).
* ``run_merge`` end-to-end — a successful merge of a branch carrying a
  source issue resolves that issue and surfaces it in the output.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from se3.commands import run
from se3.commands.merge_cmd import (
    _backfill_resolved_source_issues,
    _map_branches_to_source_issues,
    run_merge,
)
from se3.engine.issue_manager import IssueManager, IssueStatus


# --------------------------------------------------------------------------
# fixtures / helpers
# --------------------------------------------------------------------------
def _init_repo(path: Path) -> None:
    """Initialize a git repo with an initial commit (mirrors merge/test_cli)."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True, capture_output=True,
    )
    (path / "README.md").write_text("# Test\n")
    subprocess.run(
        ["git", "-C", str(path), "add", "."], check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"],
        check=True, capture_output=True,
    )


def _write_worktree_engine_json(
    root: Path,
    *,
    name: str,
    branch: str,
    source_issue_id=None,
    status: str = "completed",
    is_worktree_mode: bool = True,
    archived: bool = False,
) -> Path:
    """Write a worktree engine.json under live or archived worktree state.

    Returns the engine.json path. ``archived`` places it under
    ``se3/worktrees/.archive/<name>/...`` to mimic a GC'd / delete-merged run.
    """
    base = root / "se3" / "worktrees"
    if archived:
        base = base / ".archive"
    state_dir = base / name / "se3" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "flow_id": name,
        "status": status,
        "task_description": "isolated task",
        "is_worktree_mode": is_worktree_mode,
        "worktree_branch": branch,
        "worktree_original_branch": "master",
        "worktree_path": str(base / name),
        "source_issue_id": source_issue_id,
        "state": {"current_step_id": "implement"},
    }
    engine_file = state_dir / "engine.json"
    engine_file.write_text(json.dumps(data))
    return engine_file


def _make_in_progress_issue(project_root: Path, description: str) -> str:
    """Create an issue and transition it OPEN → IN_PROGRESS; return its id."""
    mgr = IssueManager(project_root)
    issue = mgr.create(description=description)
    mgr.update_status(issue.id, IssueStatus.IN_PROGRESS)
    return issue.id


def _make_report(**kwargs):
    """Build a successful MergeReport with sane defaults."""
    from se3.engine.merge.orchestrator import MergeReport

    defaults = dict(
        success=True,
        merged_branches=[],
        newly_merged_branches=[],
        already_ancestor_branches=[],
    )
    defaults.update(kwargs)
    return MergeReport(**defaults)


def _merge_complete_body(captured) -> str:
    """Return the 'Merge Complete' render_text body (a fast-strategy stash
    audit line may be rendered before it, so we cannot assume captured[0])."""
    for entry in captured:
        if entry["title"] == "Merge Complete":
            return entry["content"]
    raise AssertionError(f"no 'Merge Complete' output in {captured!r}")


def _patch_merge(monkeypatch, report, captured):
    """Patch render_text, MergeOrchestrator and _branch_exists for run_merge."""
    def capture_render_text(content, title=None, style=None):
        captured.append({"content": content, "title": title})

    monkeypatch.setattr(
        "se3.commands.merge_cmd.render_text", capture_render_text,
    )

    class MockOrchestrator:
        def __init__(self, **kwargs):
            pass

        def execute(self, branches):
            return report

    monkeypatch.setattr(
        "se3.engine.merge.orchestrator.MergeOrchestrator", MockOrchestrator,
    )
    monkeypatch.setattr(
        "se3.commands.merge_cmd._branch_exists",
        lambda _root, _branch: True,
    )


# --------------------------------------------------------------------------
# find_worktree_source_issue_by_branch (run.py scanner)
# --------------------------------------------------------------------------
class TestFindWorktreeSourceIssueByBranch:
    def test_live_worktree_match(self, tmp_path):
        _write_worktree_engine_json(
            tmp_path, name="wt-1", branch="feat-1", source_issue_id="042",
        )
        assert (
            run.find_worktree_source_issue_by_branch(tmp_path, "feat-1") == "042"
        )

    def test_archived_worktree_match(self, tmp_path):
        # A delete-merged / GC'd run's engine.json lives under .archive/.
        _write_worktree_engine_json(
            tmp_path, name="wt-gone", branch="feat-2",
            source_issue_id="099", archived=True,
        )
        assert (
            run.find_worktree_source_issue_by_branch(tmp_path, "feat-2") == "099"
        )

    def test_completed_flow_not_excluded(self, tmp_path):
        # COMPLETED is the normal state when a worktree run merges back; the
        # scanner must NOT filter it out (unlike find_resumable_worktree_runs).
        _write_worktree_engine_json(
            tmp_path, name="wt-c", branch="feat-c",
            source_issue_id="007", status="completed",
        )
        assert run.find_resumable_worktree_runs(tmp_path) == []
        assert (
            run.find_worktree_source_issue_by_branch(tmp_path, "feat-c") == "007"
        )

    def test_no_match_returns_none(self, tmp_path):
        _write_worktree_engine_json(
            tmp_path, name="wt-1", branch="feat-1", source_issue_id="042",
        )
        assert run.find_worktree_source_issue_by_branch(tmp_path, "other") is None

    def test_no_source_issue_returns_none(self, tmp_path):
        _write_worktree_engine_json(
            tmp_path, name="wt-1", branch="feat-1", source_issue_id=None,
        )
        assert run.find_worktree_source_issue_by_branch(tmp_path, "feat-1") is None

    def test_non_worktree_flow_ignored(self, tmp_path):
        _write_worktree_engine_json(
            tmp_path, name="wt-1", branch="feat-1",
            source_issue_id="042", is_worktree_mode=False,
        )
        assert run.find_worktree_source_issue_by_branch(tmp_path, "feat-1") is None

    def test_corrupt_engine_json_skipped(self, tmp_path):
        # A corrupt file must not raise; a valid sibling still resolves.
        bad = (
            tmp_path / "se3" / "worktrees" / "wt-bad" / "se3" / "state"
        )
        bad.mkdir(parents=True, exist_ok=True)
        (bad / "engine.json").write_text("{ not valid json")
        _write_worktree_engine_json(
            tmp_path, name="wt-ok", branch="feat-1", source_issue_id="042",
        )
        assert (
            run.find_worktree_source_issue_by_branch(tmp_path, "feat-1") == "042"
        )

    def test_no_worktrees_dir_returns_none(self, tmp_path):
        assert run.find_worktree_source_issue_by_branch(tmp_path, "feat-1") is None

    def test_empty_branch_returns_none(self, tmp_path):
        assert run.find_worktree_source_issue_by_branch(tmp_path, "") is None


# --------------------------------------------------------------------------
# _map_branches_to_source_issues
# --------------------------------------------------------------------------
class TestMapBranchesToSourceIssues:
    def test_maps_only_branches_with_source_issue(self, tmp_path):
        _write_worktree_engine_json(
            tmp_path, name="wt-1", branch="feat-1", source_issue_id="042",
        )
        _write_worktree_engine_json(
            tmp_path, name="wt-2", branch="feat-2", source_issue_id=None,
        )
        mapping = _map_branches_to_source_issues(
            tmp_path, ["feat-1", "feat-2", "feat-unknown"],
        )
        assert mapping == {"feat-1": "042"}

    def test_captures_before_worktree_deleted(self, tmp_path):
        # Simulate --delete-merged: capture the map, THEN remove the live
        # worktree state. The map must still drive the backfill afterward.
        _write_worktree_engine_json(
            tmp_path, name="wt-1", branch="feat-1", source_issue_id="042",
        )
        issue_id = _make_in_progress_issue(tmp_path, "resolve me")
        # Rewrite the issue id into the engine.json so the map yields it.
        _write_worktree_engine_json(
            tmp_path, name="wt-1", branch="feat-1", source_issue_id=issue_id,
        )

        mapping = _map_branches_to_source_issues(tmp_path, ["feat-1"])
        assert mapping == {"feat-1": issue_id}

        # Now the worktree is gone (post-merge cleanup); scanner would miss it.
        import shutil

        shutil.rmtree(tmp_path / "se3" / "worktrees" / "wt-1")
        assert run.find_worktree_source_issue_by_branch(tmp_path, "feat-1") is None

        # But the pre-captured map still resolves the issue.
        resolved = _backfill_resolved_source_issues(tmp_path, ["feat-1"], mapping)
        assert resolved == [issue_id]
        assert (
            IssueManager(tmp_path).load(issue_id).status == IssueStatus.RESOLVED
        )


# --------------------------------------------------------------------------
# _backfill_resolved_source_issues
# --------------------------------------------------------------------------
class TestBackfillResolvedSourceIssues:
    def test_in_progress_issue_resolved(self, tmp_path):
        issue_id = _make_in_progress_issue(tmp_path, "resolve me")
        resolved = _backfill_resolved_source_issues(
            tmp_path, ["feat-1"], {"feat-1": issue_id},
        )
        assert resolved == [issue_id]
        assert (
            IssueManager(tmp_path).load(issue_id).status == IssueStatus.RESOLVED
        )

    def test_already_resolved_issue_untouched_idempotent(self, tmp_path):
        # Repeat merge of an already-resolved issue must be a no-op, not error.
        issue_id = _make_in_progress_issue(tmp_path, "resolve me")
        mgr = IssueManager(tmp_path)
        mgr.update_status(issue_id, IssueStatus.RESOLVED)

        resolved = _backfill_resolved_source_issues(
            tmp_path, ["feat-1"], {"feat-1": issue_id},
        )
        assert resolved == []
        assert mgr.load(issue_id).status == IssueStatus.RESOLVED

    def test_open_issue_not_touched(self, tmp_path):
        # An OPEN (never in-progress) issue is not this backfill's concern.
        mgr = IssueManager(tmp_path)
        issue = mgr.create(description="still open")
        resolved = _backfill_resolved_source_issues(
            tmp_path, ["feat-1"], {"feat-1": issue.id},
        )
        assert resolved == []
        assert mgr.load(issue.id).status == IssueStatus.OPEN

    def test_only_newly_merged_branches_considered(self, tmp_path):
        # A branch present in the map but NOT newly-merged is skipped.
        issue_id = _make_in_progress_issue(tmp_path, "resolve me")
        resolved = _backfill_resolved_source_issues(
            tmp_path, newly_merged=[], branch_issue_map={"feat-1": issue_id},
        )
        assert resolved == []
        assert (
            IssueManager(tmp_path).load(issue_id).status
            == IssueStatus.IN_PROGRESS
        )

    def test_empty_map_returns_empty(self, tmp_path):
        assert _backfill_resolved_source_issues(tmp_path, ["feat-1"], {}) == []

    def test_missing_issue_swallowed(self, tmp_path):
        # A stale id that no longer maps to a file must not raise.
        IssueManager(tmp_path)  # ensure issue dirs exist
        resolved = _backfill_resolved_source_issues(
            tmp_path, ["feat-1"], {"feat-1": "999"},
        )
        assert resolved == []


# --------------------------------------------------------------------------
# run_merge end-to-end backfill
# --------------------------------------------------------------------------
class TestRunMergeBackfill:
    def test_success_resolves_source_issue(self, tmp_path, monkeypatch):
        _init_repo(tmp_path)
        issue_id = _make_in_progress_issue(tmp_path, "worktree feature")
        _write_worktree_engine_json(
            tmp_path, name="wt-1", branch="feat-1",
            source_issue_id=issue_id, status="completed",
        )

        report = _make_report(
            merged_branches=["feat-1"], newly_merged_branches=["feat-1"],
        )
        captured: list[dict] = []
        _patch_merge(monkeypatch, report, captured)

        exit_code = run_merge(["feat-1"], project_root=tmp_path)
        assert exit_code == 0
        body = _merge_complete_body(captured)
        assert f"Resolved source issue #{issue_id}" in body
        assert (
            IssueManager(tmp_path).load(issue_id).status == IssueStatus.RESOLVED
        )

    def test_retry_merge_of_leftover_branch_resolves(self, tmp_path, monkeypatch):
        # req 2: a leftover branch (first merge failed → issue still
        # in-progress) is later re-merged via `se3 merge <branch>`; the
        # backfill catches up and resolves the source issue.
        _init_repo(tmp_path)
        issue_id = _make_in_progress_issue(tmp_path, "leftover feature")
        # The worktree was GC'd into .archive after the failed merge.
        _write_worktree_engine_json(
            tmp_path, name="wt-old", branch="feat-leftover",
            source_issue_id=issue_id, status="completed", archived=True,
        )

        report = _make_report(
            merged_branches=["feat-leftover"],
            newly_merged_branches=["feat-leftover"],
        )
        captured: list[dict] = []
        _patch_merge(monkeypatch, report, captured)

        exit_code = run_merge(["feat-leftover"], project_root=tmp_path)
        assert exit_code == 0
        assert f"Resolved source issue #{issue_id}" in _merge_complete_body(captured)
        assert (
            IssueManager(tmp_path).load(issue_id).status == IssueStatus.RESOLVED
        )

    def test_success_without_source_issue_no_backfill_line(
        self, tmp_path, monkeypatch
    ):
        _init_repo(tmp_path)
        # No worktree engine.json → no branch→issue mapping → no resolve line.
        report = _make_report(
            merged_branches=["feat-x"], newly_merged_branches=["feat-x"],
        )
        captured: list[dict] = []
        _patch_merge(monkeypatch, report, captured)

        exit_code = run_merge(["feat-x"], project_root=tmp_path)
        assert exit_code == 0
        assert "Resolved source issue" not in _merge_complete_body(captured)

    def test_already_ancestor_branch_not_backfilled(self, tmp_path, monkeypatch):
        # A branch that was already an ancestor (no fresh merge commit) is NOT
        # in newly_merged, so its issue is left as-is for this run.
        _init_repo(tmp_path)
        issue_id = _make_in_progress_issue(tmp_path, "already merged")
        _write_worktree_engine_json(
            tmp_path, name="wt-anc", branch="feat-anc",
            source_issue_id=issue_id, status="completed",
        )

        report = _make_report(
            merged_branches=["feat-anc"],
            newly_merged_branches=[],
            already_ancestor_branches=["feat-anc"],
        )
        captured: list[dict] = []
        _patch_merge(monkeypatch, report, captured)

        exit_code = run_merge(["feat-anc"], project_root=tmp_path)
        assert exit_code == 0
        assert "Resolved source issue" not in _merge_complete_body(captured)
        assert (
            IssueManager(tmp_path).load(issue_id).status
            == IssueStatus.IN_PROGRESS
        )
