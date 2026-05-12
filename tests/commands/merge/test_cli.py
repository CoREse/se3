"""Tests for CLI-level merge input validation (defects I1/I2/I4).

Covers:

* I1 — empty branch list MUST be rejected at the CLI input layer (no silent
  zero-iteration "success").
* I2 — branch names with leading dash, shell metacharacters, or git
  ref-format violations MUST be rejected before any git command runs.
* I4 — ``show-ref`` invocation MUST fail closed when git itself is
  unavailable (``FileNotFoundError``, timeout, etc.) rather than silently
  treating a missing branch as "exists".
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from se3.commands.merge_cmd import (
    _branch_exists,
    run_merge,
    validate_branch_names,
)


def _init_repo(path: Path) -> None:
    """Initialize a git repo with an initial commit."""
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


class TestValidateBranchNames:
    """Unit tests for the validate_branch_names helper (defects I1/I2)."""

    def test_empty_list_raises(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            validate_branch_names([])
        assert "at least one branch" in str(excinfo.value).lower()

    def test_empty_string_entry_rejected(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            validate_branch_names([""])
        assert "non-empty" in str(excinfo.value).lower()

    def test_leading_dash_rejected(self) -> None:
        # ``-rf`` could be passed to git as a flag — must be rejected.
        with pytest.raises(ValueError) as excinfo:
            validate_branch_names(["-rf"])
        assert "-" in str(excinfo.value)
        assert "flag" in str(excinfo.value).lower()

    def test_double_dash_rejected(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            validate_branch_names(["--force"])
        assert "flag" in str(excinfo.value).lower()

    @pytest.mark.parametrize(
        "name",
        [
            "feat;rm -rf /",     # semicolon
            "feat$(whoami)",      # command substitution
            "feat`whoami`",       # backtick
            "feat|cat",           # pipe
            "feat&background",    # ampersand
            "feat>out",           # redirect
            "feat<in",            # redirect
            "feat\nnewline",     # newline injection
            "feat\rcarriage",    # carriage return
            "feat\ttab",         # tab
        ],
    )
    def test_shell_metachars_rejected(self, name: str) -> None:
        with pytest.raises(ValueError) as excinfo:
            validate_branch_names([name])
        # The error must mention the rejection reason — caller will surface it.
        assert "metacharacter" in str(excinfo.value).lower() or "control" in str(excinfo.value).lower()

    @pytest.mark.parametrize(
        "name",
        [
            "feat..bad",          # ..
            ".hidden",            # leading .
            "/leading-slash",     # leading /
            "trailing/",          # trailing /
            "feat.lock",          # .lock suffix
            "feat@{0}",           # reflog
        ],
    )
    def test_git_ref_format_rules_rejected(self, name: str) -> None:
        with pytest.raises(ValueError):
            validate_branch_names([name])

    def test_HEAD_rejected(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            validate_branch_names(["HEAD"])
        assert "pseudo-ref" in str(excinfo.value).lower()

    def test_at_sign_rejected(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            validate_branch_names(["@"])
        assert "pseudo-ref" in str(excinfo.value).lower()

    def test_valid_names_accepted(self) -> None:
        # No exception expected.
        validate_branch_names(
            [
                "feature",
                "feat-x",
                "feat_y",
                "feat/sub",
                "release-1.2",
                "user.email",
            ]
        )

    def test_multi_invalid_names_listed(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            validate_branch_names(["-rf", "good", "feat;ls"])
        msg = str(excinfo.value)
        # Both invalid names must appear so the operator can fix them all
        # in a single edit pass.
        assert "-rf" in msg
        assert "feat;ls" in msg


class TestBranchExistsHardening:
    """Defect I4: ``_branch_exists`` returncode + infrastructure failures."""

    def test_existing_branch_returns_true(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "-b", "feature"],
            check=True, capture_output=True,
        )
        assert _branch_exists(tmp_path, "feature") is True

    def test_missing_branch_returns_false(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        # show-ref --verify returns non-zero exit when the ref is absent.
        # The function MUST inspect returncode and return False rather
        # than silently treating the absence as success.
        assert _branch_exists(tmp_path, "no-such-branch") is False

    def test_missing_git_treated_as_missing_branch(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """When git binary is absent, _branch_exists must fail closed.

        Defect I4: a FileNotFoundError from subprocess.run (git missing on
        PATH) MUST be caught and treated as "branch does not exist" so the
        caller refuses to merge an indeterminate ref.
        """
        _init_repo(tmp_path)

        def fake_run_git(project_root, *args, check=True, timeout=30):
            raise FileNotFoundError("git: not found")

        monkeypatch.setattr(
            "se3.commands.merge_cmd._run_git", fake_run_git
        )
        assert _branch_exists(tmp_path, "feature") is False

    def test_timeout_treated_as_missing_branch(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Subprocess timeout MUST not propagate as an unhandled exception."""
        _init_repo(tmp_path)

        def fake_run_git(project_root, *args, check=True, timeout=30):
            raise subprocess.TimeoutExpired(cmd="git", timeout=timeout)

        monkeypatch.setattr(
            "se3.commands.merge_cmd._run_git", fake_run_git
        )
        assert _branch_exists(tmp_path, "feature") is False


class TestRunMergeRejectsBadInput:
    """Programmatic run_merge() MUST also validate before dispatching."""

    def test_run_merge_rejects_empty_list(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        # validate_branch_names is called inside run_merge before working-tree
        # checks; an empty list must trigger an early failure exit code.
        exit_code = run_merge([], project_root=tmp_path)
        assert exit_code == 1

    def test_run_merge_rejects_leading_dash(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        exit_code = run_merge(["-rf"], project_root=tmp_path)
        assert exit_code == 1

    def test_run_merge_rejects_metachar(self, tmp_path: Path) -> None:
        _init_repo(tmp_path)
        exit_code = run_merge(["feat;rm"], project_root=tmp_path)
        assert exit_code == 1


class TestCliMergeBadInput:
    """End-to-end CLI tests via Typer — defect I1/I2 must surface as exit != 0."""

    def _invoke(self, tmp_path: Path, extra_args: list[str]) -> "object":
        """Run ``se3 merge ...`` from *tmp_path* and return the CliRunner result."""
        _init_repo(tmp_path)

        old_cwd = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            from typer.testing import CliRunner
            from se3.cli import app

            runner = CliRunner()
            return runner.invoke(app, ["merge"] + extra_args)
        finally:
            os.chdir(old_cwd)

    def test_empty_branches_exits_nonzero(self, tmp_path: Path) -> None:
        """Defect I1: ``se3 merge`` with no branch args is a hard error."""
        result = self._invoke(tmp_path, [])
        assert result.exit_code != 0

    def test_leading_dash_branch_rejected(self, tmp_path: Path) -> None:
        """Defect I2: ``se3 merge -rf`` MUST NOT be treated as a flag.

        Note: typer would normally reject ``-rf`` as an unknown flag *before*
        our validator runs, which is the correct fail-closed behaviour. We
        verify that the exit code is non-zero either way — what we MUST NOT
        see is the merge command dispatching with ``-rf`` as a branch name.
        """
        result = self._invoke(tmp_path, ["--", "-rf"])
        assert result.exit_code != 0
        # The output must mention rejection — never claim success.
        out = (result.output or "").lower()
        assert "merged" not in out or "fail" in out or "error" in out or "invalid" in out

    def test_metachar_branch_rejected(self, tmp_path: Path) -> None:
        """Defect I2: shell metachars in branch names trigger rejection."""
        result = self._invoke(tmp_path, ["feat;ls"])
        assert result.exit_code != 0


class TestSplitMergedBuckets:
    """Defect I3: split helper used by run_merge to bucket branches."""

    def test_split_with_typed_buckets(self) -> None:
        from se3.commands.merge_cmd import _split_merged_buckets

        class StubReport:
            newly_merged_branches = ["a", "b"]
            already_ancestor_branches = ["c"]
            merged_branches = ["a", "b", "c"]

        newly, already = _split_merged_buckets(StubReport())
        assert newly == ["a", "b"]
        assert already == ["c"]

    def test_split_falls_back_to_legacy_aggregate(self) -> None:
        """When new buckets are empty but legacy is populated, treat all as newly."""
        from se3.commands.merge_cmd import _split_merged_buckets

        class StubReport:
            newly_merged_branches: list = []
            already_ancestor_branches: list = []
            merged_branches = ["x", "y"]

        newly, already = _split_merged_buckets(StubReport())
        assert newly == ["x", "y"]
        assert already == []

    def test_split_empty_when_nothing_merged(self) -> None:
        from se3.commands.merge_cmd import _split_merged_buckets

        class StubReport:
            newly_merged_branches: list = []
            already_ancestor_branches: list = []
            merged_branches: list = []

        newly, already = _split_merged_buckets(StubReport())
        assert newly == []
        assert already == []

    def test_fallback_uses_git_ancestry_when_project_root_provided(
        self, tmp_path: Path
    ) -> None:
        """When new buckets are empty and project_root is given, the legacy
        fallback checks git ancestry instead of treating all as newly merged."""
        import subprocess
        from se3.commands.merge_cmd import _split_merged_buckets

        # Init repo
        subprocess.run(
            ["git", "init", str(tmp_path), "--initial-branch=main"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.email", "t@t.com"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "user.name", "T"],
            check=True, capture_output=True,
        )
        (tmp_path / "README.md").write_text("# init\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "init"],
            check=True, capture_output=True,
        )

        # Create a branch that will be an ancestor
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "-b", "ancestor"],
            check=True, capture_output=True,
        )
        (tmp_path / "feat.md").write_text("feat\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "feat"],
            check=True, capture_output=True,
        )

        # Merge ancestor into main (so ancestor becomes an ancestor of HEAD)
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "main"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "merge", "--no-ff", "ancestor", "-m", "merge"],
            check=True, capture_output=True,
        )

        # Create a non-ancestor branch
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "-b", "newbranch"],
            check=True, capture_output=True,
        )
        (tmp_path / "other.md").write_text("other\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "other"],
            check=True, capture_output=True,
        )

        # Go back to main
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "main"],
            check=True, capture_output=True,
        )

        class StubReport:
            newly_merged_branches: list = []
            already_ancestor_branches: list = []
            merged_branches = ["ancestor", "newbranch"]

        newly, already = _split_merged_buckets(StubReport(), tmp_path)
        # "ancestor" is already an ancestor of HEAD on main
        assert "ancestor" in already
        # "newbranch" is NOT an ancestor of HEAD on main
        assert "newbranch" in newly


class TestCliRendersBucketSplit:
    """Defect I3: end-to-end run_merge rendering separates the two buckets."""

    def test_success_renders_only_newly_merged_section(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """When all merges produced new commits, only the newly-merged section appears."""
        from se3.engine.merge.orchestrator import MergeReport

        report = MergeReport(
            success=True,
            merged_branches=["feat-a", "feat-b"],
            newly_merged_branches=["feat-a", "feat-b"],
            already_ancestor_branches=[],
        )

        captured: list[dict] = []

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

        _init_repo(tmp_path)
        from se3.commands.merge_cmd import run_merge

        exit_code = run_merge(["feat-a", "feat-b"], project_root=tmp_path)
        assert exit_code == 0
        assert len(captured) == 1
        body = captured[0]["content"]
        assert "Successfully merged 2 branch(es)" in body
        assert "Newly merged (2):" in body
        assert "feat-a" in body
        assert "feat-b" in body
        # Already-ancestor section MUST NOT appear when there are none.
        assert "Already an ancestor of HEAD" not in body

    def test_success_renders_only_already_ancestor_section(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """When all branches were already ancestors, only the already section appears."""
        from se3.engine.merge.orchestrator import MergeReport

        report = MergeReport(
            success=True,
            merged_branches=["scn"],
            newly_merged_branches=[],
            already_ancestor_branches=["scn"],
        )

        captured: list[dict] = []

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

        _init_repo(tmp_path)
        from se3.commands.merge_cmd import run_merge

        exit_code = run_merge(["scn"], project_root=tmp_path)
        assert exit_code == 0
        body = captured[0]["content"]
        assert "Already an ancestor of HEAD" in body
        assert "scn" in body
        assert "Newly merged" not in body

    def test_success_renders_both_sections_disjoint(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """User scenario: ``se3 merge scn discoverbug``.

        scn is already an ancestor (no-op for this run), discoverbug
        produces a new merge commit. The output MUST clearly separate
        the two so the user can tell what actually changed — this is
        the exact output the user reported as confusing.
        """
        from se3.engine.merge.orchestrator import MergeReport

        report = MergeReport(
            success=True,
            merged_branches=["scn", "discoverbug"],
            newly_merged_branches=["discoverbug"],
            already_ancestor_branches=["scn"],
        )

        captured: list[dict] = []

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

        _init_repo(tmp_path)
        from se3.commands.merge_cmd import run_merge

        exit_code = run_merge(["scn", "discoverbug"], project_root=tmp_path)
        assert exit_code == 0
        body = captured[0]["content"]
        # Both sections must be present.
        assert "Newly merged (1):" in body
        assert "discoverbug" in body
        assert "Already an ancestor of HEAD" in body
        assert "(1)" in body  # the count appears
        # scn appears only under the already section, not the newly section.
        # We check this by ensuring discoverbug appears before the
        # already-ancestor heading and scn appears after it.
        newly_idx = body.index("Newly merged")
        already_idx = body.index("Already an ancestor of HEAD")
        discover_idx = body.index("discoverbug")
        scn_idx = body.index("scn")
        assert newly_idx < discover_idx < already_idx
        assert already_idx < scn_idx
        # And the total summary line
        assert "Successfully merged 2 branch(es)" in body


class TestCliMergeBadInput:
    """End-to-end CLI tests via Typer — defect I1/I2 must surface as exit != 0."""

    def _invoke(self, tmp_path: Path, extra_args: list[str]) -> "object":
        """Run ``se3 merge ...`` from *tmp_path* and return the CliRunner result."""
        _init_repo(tmp_path)

        old_cwd = os.getcwd()
        os.chdir(str(tmp_path))
        try:
            from typer.testing import CliRunner
            from se3.cli import app

            runner = CliRunner()
            return runner.invoke(app, ["merge"] + extra_args)
        finally:
            os.chdir(old_cwd)

    def test_empty_branches_exits_nonzero(self, tmp_path: Path) -> None:
        """Defect I1: ``se3 merge`` with no branch args is a hard error."""
        result = self._invoke(tmp_path, [])
        assert result.exit_code != 0

    def test_leading_dash_branch_rejected(self, tmp_path: Path) -> None:
        """Defect I2: ``se3 merge -rf`` MUST NOT be treated as a flag.

        Note: typer would normally reject ``-rf`` as an unknown flag *before*
        our validator runs, which is the correct fail-closed behaviour. We
        verify that the exit code is non-zero either way — what we MUST NOT
        see is the merge command dispatching with ``-rf`` as a branch name.
        """
        result = self._invoke(tmp_path, ["--", "-rf"])
        assert result.exit_code != 0
        # The output must mention rejection — never claim success.
        out = (result.output or "").lower()
        assert "merged" not in out or "fail" in out or "error" in out or "invalid" in out

    def test_metachar_branch_rejected(self, tmp_path: Path) -> None:
        """Defect I2: shell metachars in branch names trigger rejection."""
        result = self._invoke(tmp_path, ["feat;ls"])
        assert result.exit_code != 0


class TestRunMergeEndToEndAcceptance:
    """End-to-end acceptance: ``run_merge`` must produce a real merge commit
    AND advance the version file.

    This exercises the silent-merge-loss class of bug that motivated the
    self-check finding: a successful CLI exit MUST be accompanied by:

      (a) HEAD parent_count >= 2 (a true merge commit), AND
      (b) the merged branch is reachable from HEAD, AND
      (c) the version file (pyproject.toml) has actually advanced.

    Without (a)+(b)+(c) the silent-loss scenario reported by the user
    ("Successfully merged 2 branch(es)" but git log shows no new merge
    commit and version stays at 4.7.0) cannot be detected by unit tests
    that mock the orchestrator.
    """

    def _init_versioned_repo(self, path: Path) -> str:
        subprocess.run(
            ["git", "init", str(path)], check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(path), "config", "user.email", "test@test.com"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(path), "config", "user.name", "Test"],
            check=True, capture_output=True,
        )
        (path / ".gitignore").write_text(
            "/se3/*\n!/se3/specs/\n!/se3/issues/\n"
        )
        (path / "pyproject.toml").write_text(
            '[build-system]\nrequires = ["setuptools"]\n\n'
            '[project]\nname = "test-pkg"\nversion = "1.0.0"\n'
        )
        (path / "README.md").write_text("# Test\n")
        subprocess.run(
            ["git", "-C", str(path), "add", "."],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(path), "commit", "-m", "initial 1.0.0"],
            check=True, capture_output=True,
        )
        result = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        return result.stdout.strip()

    def _git(self, path: Path, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", str(path), *args],
            capture_output=True, text=True, check=True,
        )

    def test_e2e_merge_advances_head_and_version(
        self, tmp_path: Path,
    ) -> None:
        """A clean merge of a feature branch produces:

          * HEAD parent_count >= 2 (merge commit)
          * the feature branch is reachable from HEAD
          * pyproject.toml's version has advanced (1.0.0 -> 1.0.x or 1.x.0)
        """
        default = self._init_versioned_repo(tmp_path)
        pre_head = self._git(tmp_path, "rev-parse", "HEAD").stdout.strip()

        # Create a feature branch with a non-conflicting change and a
        # version bump on the branch tip so the aggregator has a
        # bump-source to consume; without this it would compute "no bump"
        # and the version-advance assertion would be a no-op.
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", "-b", "feat-e2e"],
            check=True, capture_output=True,
        )
        (tmp_path / "pyproject.toml").write_text(
            '[build-system]\nrequires = ["setuptools"]\n\n'
            '[project]\nname = "test-pkg"\nversion = "1.0.1"\n'
        )
        (tmp_path / "feat.txt").write_text("feature content\n")
        subprocess.run(
            ["git", "-C", str(tmp_path), "add", "-A"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-m", "feat: bump 1.0.1"],
            check=True, capture_output=True,
        )

        # Return to the default branch for the merge.
        subprocess.run(
            ["git", "-C", str(tmp_path), "checkout", default],
            check=True, capture_output=True,
        )

        from se3.commands.merge_cmd import run_merge

        # delete_merged=False: this test references feat-e2e after merge to
        # verify ancestry; with the new default-on cleanup, the branch would
        # be gone by then.
        exit_code = run_merge(
            ["feat-e2e"], project_root=tmp_path, delete_merged=False,
        )
        assert exit_code == 0, f"run_merge exited {exit_code}, expected 0"

        # (a) HEAD has parent_count >= 2 — a real merge commit was created.
        post_head = self._git(tmp_path, "rev-parse", "HEAD").stdout.strip()
        assert post_head != pre_head, (
            "HEAD did not advance — silent merge loss (this is exactly "
            "the bug the self-check was added to catch)."
        )
        parents = self._git(
            tmp_path, "rev-list", "--parents", "-n", "1", "HEAD",
        ).stdout.strip().split()
        # parents[0] is HEAD itself; remainder are the parents.
        parent_count = len(parents) - 1
        assert parent_count >= 2, (
            f"HEAD has parent_count={parent_count}, expected >= 2 "
            f"(merge commit). Output: {parents!r}"
        )

        # (b) The feature branch is reachable from HEAD.
        # check=True so a non-zero rc raises before we get here.
        self._git(
            tmp_path, "merge-base", "--is-ancestor", "feat-e2e", "HEAD",
        )

        # (c) Version advanced.
        new_version_text = (tmp_path / "pyproject.toml").read_text()
        assert 'version = "1.0.0"' not in new_version_text, (
            "pyproject.toml is still at 1.0.0 — version bump silently "
            "dropped after a successful merge."
        )
