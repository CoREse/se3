"""Integration + unit tests for DAG implement merge robustness (v4.11.0).

These tests exercise ``_merge_leaf_branch`` against real git repositories
in temporary directories rather than mocking ``_run_git`` call-by-call.
The real-git approach is more robust to internal refactoring (different
order or count of ``_run_git`` invocations is fine as long as the
externally-observable behaviour holds).

Scenarios covered:
1. Dirty main repo (untracked file) does NOT block leaf merge — stash kicks in.
2. Stash pop conflict (untracked main-repo file collides with leaf's new file)
   resolved by take-ours; stash dropped; audit issue filed.
3. LLM exhausted → ``_take_theirs_fallback`` accepts leaf's version and
   commits; audit issue filed.
4. Take-theirs commit failure aborts cleanly (no orphan merge state).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tianluo.engine.steps.implement import (
    _merge_leaf_branch,
    _parse_stashpop_already_exists,
)


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    """Run git in *repo*; returns CompletedProcess (text mode)."""
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True, text=True, check=check,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Bare-bones git repo with an initial commit on ``main``."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "--initial-branch=main")
    _git(r, "config", "user.email", "test@example.com")
    _git(r, "config", "user.name", "Test")
    (r / ".gitignore").write_text("tianluo/\n", encoding="utf-8")
    _git(r, "add", ".gitignore")
    _git(r, "commit", "-m", "init")
    return r


def _make_leaf_branch(
    repo: Path,
    branch: str,
    files: dict[str, str],
    base: str = "main",
) -> None:
    """Create *branch* off *base* containing *files* (path → content)."""
    _git(repo, "checkout", "-b", branch, base)
    for path, content in files.items():
        full = repo / path
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", f"add files on {branch}")
    _git(repo, "checkout", base)


# ---------------------------------------------------------------------------
# 1. Stash kicks in: untracked file in main no longer blocks the merge.
# ---------------------------------------------------------------------------


class TestStashPrevention:
    """Untracked / modified main-repo files no longer block leaf merge."""

    def test_untracked_file_in_main_does_not_block_merge(self, repo: Path):
        """Discovery-step untracked file in main → merge still succeeds.

        Replicates the 20260507-200706 root cause: untracked file in main
        repo (e.g. README created by discovery) collides with a leaf file
        of the same path. Before 4.11.0, ``git merge`` refused with
        ``"untracked working tree files would be overwritten"`` → leaf
        commits orphaned. With pre-merge stash this is now stashed away
        before the merge, and the post-merge pop conflict (if any) is
        resolved by take-ours.
        """
        # Leaf branch adds README.md
        _make_leaf_branch(repo, "impl/f/G1", {"README.md": "leaf-version\n"})

        # Main repo has an untracked README.md (mimics discovery artefact)
        (repo / "README.md").write_text("untracked-from-main\n", encoding="utf-8")

        result = _merge_leaf_branch(
            repo, "impl/f/G1", "main",
            task_description="t", group_summaries=[],
        )

        assert result is True

        # main now contains leaf's commit (FF or merge commit, either is OK)
        log = _git(repo, "log", "--oneline", "main").stdout
        assert "add files on impl/f/G1" in log

        # README.md content: take-ours kept the merged (leaf) version on
        # the conflict path — untracked main-side content discarded.
        assert (repo / "README.md").read_text(encoding="utf-8") == "leaf-version\n"

        # No dangling stash
        stash_list = _git(repo, "stash", "list").stdout
        assert stash_list.strip() == ""

    def test_untracked_unrelated_file_preserved(self, repo: Path):
        """Untracked file in main that does NOT collide with leaf survives.

        Stash → merge → pop succeeds (no conflict). File still in working tree.
        """
        _make_leaf_branch(repo, "impl/f/G1", {"src/app.py": "print('leaf')\n"})

        (repo / "notes.txt").write_text("my notes\n", encoding="utf-8")

        result = _merge_leaf_branch(
            repo, "impl/f/G1", "main",
            task_description="t", group_summaries=[],
        )

        assert result is True
        assert (repo / "src" / "app.py").read_text() == "print('leaf')\n"
        # Untracked unrelated file preserved
        assert (repo / "notes.txt").read_text() == "my notes\n"
        # No dangling stash
        assert _git(repo, "stash", "list").stdout.strip() == ""


# ---------------------------------------------------------------------------
# 2. Stash pop conflict → take-ours + audit issue.
# ---------------------------------------------------------------------------


class TestStashPopConflict:
    """When stash pop conflicts post-merge, take-ours keeps HEAD's version."""

    def test_stashpop_conflict_records_audit_issue(self, repo: Path):
        """Stashpop conflict path fires _record_stashpop_takeours_event."""
        _make_leaf_branch(repo, "impl/f/G1", {"app.py": "leaf-content\n"})
        (repo / "app.py").write_text("untracked-content\n", encoding="utf-8")

        with patch(
            "tianluo.engine.steps.implement._record_stashpop_takeours_event"
        ) as mock_audit:
            result = _merge_leaf_branch(
                repo, "impl/f/G1", "main",
                task_description="t", group_summaries=[],
                flow_id="20260101-test",
            )

        assert result is True
        mock_audit.assert_called_once()
        call_args = mock_audit.call_args
        # branch + archived manifest + flow_id are passed. The third arg is
        # now the recovery manifest (list[ArchivedEntry]) rather than a bare
        # list of paths — the audit records *where the content was saved*.
        assert call_args.args[1] == "impl/f/G1"
        archived_paths = [e.rel_path for e in call_args.args[2]]
        assert "app.py" in archived_paths
        assert call_args.args[3] == "20260101-test"


# ---------------------------------------------------------------------------
# 3. Take-theirs fallback: LLM exhausted → leaf version wins, audit fires.
# ---------------------------------------------------------------------------


class TestTakeTheirsFallback:
    """When LLM cannot resolve, take-theirs preserves the leaf branch."""

    def test_llm_exhausted_take_theirs_completes_merge(self, repo: Path):
        """A real 3-way merge conflict on a tracked file, LLM mocked to fail.

        Pre-arrangement: a file ``shared.py`` is committed on main with
        content A; the leaf branch commits content B for the same file.
        After the leaf is created, main commits content C, creating a true
        3-way merge conflict.

        LLM ``resolve_merge_conflicts_with_context`` is mocked to return
        False. The take-theirs fallback should accept the leaf (B) and
        commit. Audit issue fired.
        """
        # Step 1: commit baseline shared.py on main
        (repo / "shared.py").write_text("A\n", encoding="utf-8")
        _git(repo, "add", "shared.py")
        _git(repo, "commit", "-m", "add shared.py with A")

        # Step 2: create leaf branch and modify shared.py → B
        _git(repo, "checkout", "-b", "impl/f/G1")
        (repo / "shared.py").write_text("B\n", encoding="utf-8")
        _git(repo, "add", "shared.py")
        _git(repo, "commit", "-m", "leaf changes shared.py to B")

        # Step 3: back to main and modify shared.py → C
        _git(repo, "checkout", "main")
        (repo / "shared.py").write_text("C\n", encoding="utf-8")
        _git(repo, "add", "shared.py")
        _git(repo, "commit", "-m", "main changes shared.py to C")

        # Step 4: merge leaf → main with LLM mocked to fail
        with patch(
            "tianluo.engine.steps.implement.resolve_merge_conflicts_with_context",
            return_value=False,
        ), patch(
            "tianluo.engine.steps.implement._record_take_theirs_event"
        ) as mock_audit:
            result = _merge_leaf_branch(
                repo, "impl/f/G1", "main",
                task_description="t", group_summaries=[],
                flow_id="20260101-test",
            )

        assert result is True
        # Leaf's version (B) won
        assert (repo / "shared.py").read_text() == "B\n"
        # Merge commit landed
        log = _git(repo, "log", "--oneline", "main").stdout
        assert "Merge leaf branch impl/f/G1" in log
        # Audit issue recorded
        mock_audit.assert_called_once()
        call_args = mock_audit.call_args
        assert call_args.args[1] == "impl/f/G1"
        assert "shared.py" in call_args.args[2]
        assert call_args.args[3] == "20260101-test"


# ---------------------------------------------------------------------------
# 4. Edge: take-theirs commit failure aborts cleanly.
# ---------------------------------------------------------------------------


class TestTakeTheirsCommitFailure:
    """If take-theirs commit itself fails, merge aborts without leftover state."""

    def test_commit_failure_aborts(self, repo: Path):
        """Mock the commit step to fail; merge should abort + return False.

        Strategy: stub ``_take_theirs_fallback`` directly to return False,
        which is what the function does when its internal commit fails.
        """
        # Set up a 3-way conflict (same shape as TakeTheirsFallback test)
        (repo / "shared.py").write_text("A\n", encoding="utf-8")
        _git(repo, "add", "shared.py")
        _git(repo, "commit", "-m", "add shared.py with A")

        _git(repo, "checkout", "-b", "impl/f/G1")
        (repo / "shared.py").write_text("B\n", encoding="utf-8")
        _git(repo, "add", "shared.py")
        _git(repo, "commit", "-m", "leaf B")

        _git(repo, "checkout", "main")
        (repo / "shared.py").write_text("C\n", encoding="utf-8")
        _git(repo, "add", "shared.py")
        _git(repo, "commit", "-m", "main C")

        with patch(
            "tianluo.engine.steps.implement.resolve_merge_conflicts_with_context",
            return_value=False,
        ), patch(
            "tianluo.engine.steps.implement._take_theirs_fallback",
            return_value=False,
        ):
            result = _merge_leaf_branch(
                repo, "impl/f/G1", "main",
                task_description="t", group_summaries=[],
            )

        assert result is False
        # Merge state cleaned up (no MERGE_HEAD)
        assert not (repo / ".git" / "MERGE_HEAD").exists()
        # main still at the pre-merge commit (C), not advanced
        head_msg = _git(repo, "log", "-1", "--format=%s", "main").stdout.strip()
        assert head_msg == "main C"


# ---------------------------------------------------------------------------
# 5. IssueManager audit trail integration
# ---------------------------------------------------------------------------


class TestAuditIssueIntegration:
    """take-theirs / stashpop events file real YAML issues."""

    def test_take_theirs_writes_audit_issue_to_se3_issues(self, repo: Path):
        """Real IssueManager call writes to tianluo/issues/open/."""
        # 3-way conflict setup
        (repo / "shared.py").write_text("A\n", encoding="utf-8")
        _git(repo, "add", "shared.py")
        _git(repo, "commit", "-m", "A")
        _git(repo, "checkout", "-b", "impl/f/G1")
        (repo / "shared.py").write_text("B\n", encoding="utf-8")
        _git(repo, "add", "shared.py")
        _git(repo, "commit", "-m", "leaf B")
        _git(repo, "checkout", "main")
        (repo / "shared.py").write_text("C\n", encoding="utf-8")
        _git(repo, "add", "shared.py")
        _git(repo, "commit", "-m", "main C")

        with patch(
            "tianluo.engine.steps.implement.resolve_merge_conflicts_with_context",
            return_value=False,
        ):
            result = _merge_leaf_branch(
                repo, "impl/f/G1", "main",
                task_description="t", group_summaries=[],
                flow_id="20260101-aud",
            )

        assert result is True

        issues_dir = repo / "tianluo" / "issues" / "open"
        assert issues_dir.exists()
        yaml_files = list(issues_dir.glob("*.yaml"))
        assert len(yaml_files) == 1
        content = yaml_files[0].read_text(encoding="utf-8")
        assert "take-theirs" in content
        assert "impl/f/G1" in content
        assert "shared.py" in content
        assert "20260101-aud" in content
        assert "priority: medium" in content

    def test_audit_failure_does_not_block_merge(self, repo: Path):
        """If IssueManager raises, merge still succeeds (audit is best-effort)."""
        (repo / "shared.py").write_text("A\n", encoding="utf-8")
        _git(repo, "add", "shared.py")
        _git(repo, "commit", "-m", "A")
        _git(repo, "checkout", "-b", "impl/f/G1")
        (repo / "shared.py").write_text("B\n", encoding="utf-8")
        _git(repo, "add", "shared.py")
        _git(repo, "commit", "-m", "leaf B")
        _git(repo, "checkout", "main")
        (repo / "shared.py").write_text("C\n", encoding="utf-8")
        _git(repo, "add", "shared.py")
        _git(repo, "commit", "-m", "main C")

        with patch(
            "tianluo.engine.steps.implement.resolve_merge_conflicts_with_context",
            return_value=False,
        ), patch(
            "tianluo.engine.issue_manager.IssueManager.create",
            side_effect=RuntimeError("simulated IssueManager outage"),
        ):
            result = _merge_leaf_branch(
                repo, "impl/f/G1", "main",
                task_description="t", group_summaries=[],
            )

        # Audit failed but merge still succeeded
        assert result is True
        assert (repo / "shared.py").read_text() == "B\n"


# ---------------------------------------------------------------------------
# 6. Unit: _parse_stashpop_already_exists
# ---------------------------------------------------------------------------


class TestExceptionDuringMerge:
    """If the merge attempt raises, the stash must be popped before re-raise.

    Defends against subprocess crashes, KeyboardInterrupt mid-LLM-call, etc.
    Without the try/except, an exception leaves the user with stash@{0} and
    an empty-looking working tree — the original untracked file is invisible
    until they manually ``git stash pop``.
    """

    def test_raise_during_merge_still_pops_stash(self, repo: Path):
        _make_leaf_branch(repo, "impl/f/G1", {"new.py": "n\n"})
        (repo / "scratch.py").write_text("user-local\n", encoding="utf-8")

        with patch(
            "tianluo.engine.steps.implement._attempt_merge_with_resolution",
            side_effect=RuntimeError("simulated merge crash"),
        ):
            with pytest.raises(RuntimeError, match="simulated merge crash"):
                _merge_leaf_branch(
                    repo, "impl/f/G1", "main",
                    task_description="t", group_summaries=[],
                )

        # Stash was popped (untracked file restored)
        assert (repo / "scratch.py").read_text() == "user-local\n"
        # No dangling stash entry
        assert _git(repo, "stash", "list").stdout.strip() == ""


class TestParseStashpopAlreadyExists:
    """Parser correctly extracts paths from `<path> already exists, no checkout`."""

    def test_single_file(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="app.py already exists, no checkout\n",
        )
        assert _parse_stashpop_already_exists(result) == ["app.py"]

    def test_multiple_files(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="",
            stderr=(
                "src/a.py already exists, no checkout\n"
                "src/b.py already exists, no checkout\n"
            ),
        )
        assert _parse_stashpop_already_exists(result) == ["src/a.py", "src/b.py"]

    def test_mixed_with_unrelated_output(self):
        """Unrelated lines (e.g. "Already up to date") are not falsely matched."""
        result = subprocess.CompletedProcess(
            args=[], returncode=1,
            stdout="Already up to date.\n",
            stderr=(
                "foo.py already exists, no checkout\n"
                "error: could not restore untracked files from stash\n"
            ),
        )
        # "Already up to date" lacks "no checkout" so must not match
        assert _parse_stashpop_already_exists(result) == ["foo.py"]

    def test_empty_output(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="",
        )
        assert _parse_stashpop_already_exists(result) == []

    def test_path_with_subdirs(self):
        result = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="",
            stderr="src/a/b/c.py already exists, no checkout\n",
        )
        assert _parse_stashpop_already_exists(result) == ["src/a/b/c.py"]


