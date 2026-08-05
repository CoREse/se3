"""Tests for the net-zero-diff workspace snapshot / comparison primitives."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tianluo.engine import workspace_snapshot
from tianluo.engine.workspace_snapshot import (
    WorkspaceSnapshot,
    compare_snapshots,
    snapshot_workspace,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(repo), check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A tmp git repo with one committed tracked file."""
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked.py")
    _git(tmp_path, "commit", "-m", "init")
    return tmp_path


class TestSnapshotAndCompare:
    def test_no_change_is_clean(self, repo: Path) -> None:
        before = snapshot_workspace(repo)
        after = snapshot_workspace(repo)
        assert compare_snapshots(before, after).is_clean is True

    def test_tracked_modification_is_reported(self, repo: Path) -> None:
        before = snapshot_workspace(repo)
        (repo / "tracked.py").write_text("value = 2\n", encoding="utf-8")
        delta = compare_snapshots(before, snapshot_workspace(repo))

        assert delta.is_clean is False
        assert delta.tracked_changed is True
        assert "Tracked files" in delta.describe()

    def test_tracked_modification_names_the_file(self, repo: Path) -> None:
        """A file-less "something tracked changed" is not actionable.

        The delta is handed to a fresh LLM call with no memory of the
        investigation; without the path it can only guess against a tree that
        may also hold unrelated uncommitted work.
        """
        before = snapshot_workspace(repo)
        (repo / "tracked.py").write_text("value = 2\n", encoding="utf-8")
        delta = compare_snapshots(before, snapshot_workspace(repo))

        assert delta.added_tracked == ["tracked.py"]
        assert delta.tracked_paths == ["tracked.py"]
        assert "tracked.py" in delta.describe()
        assert "tracked.py" in delta.changed_paths

    def test_probe_on_top_of_preexisting_work_is_named_as_modified(
        self, repo: Path
    ) -> None:
        """The path is named, and flagged as one that carries foreign work too."""
        (repo / "tracked.py").write_text("value = 99\n", encoding="utf-8")
        before = snapshot_workspace(repo)

        (repo / "tracked.py").write_text(
            "value = 99\nlogger.debug('probe')\n", encoding="utf-8"
        )
        delta = compare_snapshots(before, snapshot_workspace(repo))

        assert delta.modified_tracked == ["tracked.py"]
        assert delta.added_tracked == []
        described = delta.describe()
        assert "tracked.py" in described
        assert "undo only your own edits" in described

    def test_wiped_preexisting_work_is_named_as_removed(self, repo: Path) -> None:
        """Reverting too much is a delta too — and the path has to be named."""
        (repo / "tracked.py").write_text("value = 99\n", encoding="utf-8")
        before = snapshot_workspace(repo)

        (repo / "tracked.py").write_text("value = 1\n", encoding="utf-8")
        delta = compare_snapshots(before, snapshot_workspace(repo))

        assert delta.removed_tracked == ["tracked.py"]
        assert "tracked.py" in delta.describe()

    def test_only_the_touched_tracked_path_is_named(self, repo: Path) -> None:
        """Unrelated pre-existing dirt stays out of the revert instruction."""
        (repo / "other.py").write_text("unrelated = True\n", encoding="utf-8")
        _git(repo, "add", "other.py")
        _git(repo, "commit", "-m", "other")
        (repo / "other.py").write_text("unrelated = 'wip'\n", encoding="utf-8")

        before = snapshot_workspace(repo)
        (repo / "tracked.py").write_text("value = 1\nprobe = 1\n", encoding="utf-8")
        delta = compare_snapshots(before, snapshot_workspace(repo))

        assert delta.tracked_paths == ["tracked.py"]
        assert "other.py" not in delta.describe()

    def test_unalignable_diff_degrades_to_a_file_less_report(
        self, repo: Path, monkeypatch
    ) -> None:
        """Detail may be lost when git's output shape surprises us; the finding may not."""
        real_run_git = workspace_snapshot._run_git

        def _fake(root: Path, args: list) -> bytes:
            if "--name-only" in args:
                return b"bogus-a\0bogus-b\0"
            return real_run_git(root, args)

        before = snapshot_workspace(repo)
        (repo / "tracked.py").write_text("value = 2\n", encoding="utf-8")
        monkeypatch.setattr(workspace_snapshot, "_run_git", _fake)
        after = snapshot_workspace(repo)

        assert after.tracked_paths_available is False
        delta = compare_snapshots(before, after)
        assert delta.is_clean is False
        assert delta.tracked_changed is True
        assert delta.tracked_paths == []
        assert "could not be determined" in delta.describe()

    def test_snapshot_survives_a_serialization_round_trip(self, repo: Path) -> None:
        """The pre-step baseline is persisted between attempts of the same step."""
        (repo / "tracked.py").write_text("value = 7\n", encoding="utf-8")
        (repo / "scratch.txt").write_text("x\n", encoding="utf-8")
        before = snapshot_workspace(repo)

        revived = WorkspaceSnapshot.from_dict(before.to_dict())
        assert revived == before

        (repo / "tracked.py").write_text("value = 7\nprobe = 1\n", encoding="utf-8")
        assert compare_snapshots(
            revived, snapshot_workspace(repo)
        ).modified_tracked == ["tracked.py"]

    def test_from_dict_rejects_garbage(self) -> None:
        assert WorkspaceSnapshot.from_dict(None) is None
        assert WorkspaceSnapshot.from_dict("nope") is None
        assert WorkspaceSnapshot.from_dict({"tracked": "not-a-dict"}) is None

    def test_new_untracked_file_is_reported_as_added(self, repo: Path) -> None:
        before = snapshot_workspace(repo)
        (repo / "scratch.log").write_text("probe output\n", encoding="utf-8")
        delta = compare_snapshots(before, snapshot_workspace(repo))

        assert delta.is_clean is False
        assert delta.added_untracked == ["scratch.log"]
        assert "scratch.log" in delta.describe()

    def test_reverting_the_change_restores_cleanliness(self, repo: Path) -> None:
        before = snapshot_workspace(repo)

        (repo / "tracked.py").write_text("value = 1\nprint('debug')\n", encoding="utf-8")
        (repo / "scratch.log").write_text("probe\n", encoding="utf-8")
        assert compare_snapshots(before, snapshot_workspace(repo)).is_clean is False

        (repo / "tracked.py").write_text("value = 1\n", encoding="utf-8")
        (repo / "scratch.log").unlink()
        assert compare_snapshots(before, snapshot_workspace(repo)).is_clean is True

    def test_preexisting_dirty_tree_does_not_leak_into_the_delta(
        self, repo: Path
    ) -> None:
        """A tree already dirty at ``before`` contributes nothing to the delta.

        This is the whole reason the check compares two snapshots instead of
        asserting a clean tree: a flow routinely carries unrelated uncommitted
        work into the step.
        """
        # Pre-existing uncommitted work, present BEFORE the step starts.
        (repo / "tracked.py").write_text("value = 99\n", encoding="utf-8")
        (repo / "preexisting.txt").write_text("unrelated work\n", encoding="utf-8")

        before = snapshot_workspace(repo)
        assert compare_snapshots(before, snapshot_workspace(repo)).is_clean is True

        # Experimental change layered on top, then reverted.
        (repo / "tracked.py").write_text("value = 99\nprint('probe')\n", encoding="utf-8")
        assert compare_snapshots(before, snapshot_workspace(repo)).is_clean is False
        (repo / "tracked.py").write_text("value = 99\n", encoding="utf-8")
        assert compare_snapshots(before, snapshot_workspace(repo)).is_clean is True

        # The pre-existing work is still there — nothing reverted it.
        assert (repo / "preexisting.txt").exists()
        assert (repo / "tracked.py").read_text(encoding="utf-8") == "value = 99\n"

    def test_untracked_content_change_is_reported_as_modified(
        self, repo: Path
    ) -> None:
        (repo / "notes.txt").write_text("a\n", encoding="utf-8")
        before = snapshot_workspace(repo)
        (repo / "notes.txt").write_text("b\n", encoding="utf-8")

        delta = compare_snapshots(before, snapshot_workspace(repo))
        assert delta.is_clean is False
        assert delta.modified_untracked == ["notes.txt"]

    def test_untracked_removal_is_reported(self, repo: Path) -> None:
        (repo / "notes.txt").write_text("a\n", encoding="utf-8")
        before = snapshot_workspace(repo)
        (repo / "notes.txt").unlink()

        delta = compare_snapshots(before, snapshot_workspace(repo))
        assert delta.is_clean is False
        assert delta.removed_untracked == ["notes.txt"]

    def test_commit_made_during_the_window_is_reported(self, repo: Path) -> None:
        """A commit hides itself from ``git diff HEAD`` — HEAD must be compared.

        Committing a probe patch leaves an empty diff against the *new* HEAD and
        an unchanged untracked set, so without a HEAD comparison the step would
        look clean while permanently carrying the experiment on the branch.
        """
        before = snapshot_workspace(repo)

        (repo / "tracked.py").write_text("value = 1\nprint('debug')\n", encoding="utf-8")
        _git(repo, "commit", "-am", "debug")

        after = snapshot_workspace(repo)
        # The tracked-side hash alone genuinely cannot see it: both diffs empty.
        assert after.tracked_diff_hash == before.tracked_diff_hash

        delta = compare_snapshots(before, after)
        assert delta.is_clean is False
        assert delta.head_changed is True
        assert "HEAD moved" in delta.describe()

    def test_commit_of_untracked_scratch_file_is_reported(self, repo: Path) -> None:
        """Committing a scratch file also removes it from the untracked set."""
        before = snapshot_workspace(repo)

        (repo / "probe.py").write_text("print('probe')\n", encoding="utf-8")
        _git(repo, "add", "probe.py")
        _git(repo, "commit", "-m", "probe")

        after = snapshot_workspace(repo)
        assert after.untracked == before.untracked

        delta = compare_snapshots(before, after)
        assert delta.is_clean is False
        assert delta.head_changed is True

    def test_undoing_the_commit_restores_cleanliness(self, repo: Path) -> None:
        before = snapshot_workspace(repo)

        (repo / "tracked.py").write_text("value = 1\nprint('debug')\n", encoding="utf-8")
        _git(repo, "commit", "-am", "debug")
        assert compare_snapshots(before, snapshot_workspace(repo)).is_clean is False

        # A soft reset + surgical revert is the restoration path the revert
        # instruction points at; it must land back on "clean".
        _git(repo, "reset", "--soft", before.head_commit)
        (repo / "tracked.py").write_text("value = 1\n", encoding="utf-8")
        _git(repo, "reset")
        assert compare_snapshots(before, snapshot_workspace(repo)).is_clean is True

    def test_gitignored_files_are_invisible(self, repo: Path) -> None:
        (repo / ".gitignore").write_text("ignored/\n", encoding="utf-8")
        _git(repo, "add", ".gitignore")
        _git(repo, "commit", "-m", "ignore")

        before = snapshot_workspace(repo)
        (repo / "ignored").mkdir()
        (repo / "ignored" / "junk.txt").write_text("junk\n", encoding="utf-8")

        assert compare_snapshots(before, snapshot_workspace(repo)).is_clean is True


class TestRuntimeDirectoryIsExcluded:
    """The engine's own bookkeeping must not read as the step's changes.

    While the observed step runs, the engine writes the conversation log
    (``tianluo/history/<flow>/<step>.jsonl``), call records, flow state and logs
    under the runtime directory. A project that adopted tianluo on a pre-existing
    ``.gitignore`` has that directory untracked and un-ignored (``luo init`` only
    ensures ``tianluo.local.yaml`` and ``tianluo/uploads/``), so without the
    exclusion an investigation that touched nothing could never pass — and the
    revert instruction derived from the delta would order the agent to delete the
    flow's own conversation record.
    """

    def test_engine_writes_under_the_runtime_dir_do_not_dirty_the_delta(
        self, repo: Path
    ) -> None:
        history = repo / "tianluo" / "history" / "flow-1"
        history.mkdir(parents=True)
        (history / "investigate.jsonl").write_text("{}\n", encoding="utf-8")

        before = snapshot_workspace(repo)
        # The engine keeps appending to the step's chat history and re-saving
        # flow state for the whole duration of the call.
        (history / "investigate.jsonl").write_text("{}\n{}\n", encoding="utf-8")
        (history / "revert.jsonl").write_text("{}\n", encoding="utf-8")
        state = repo / "tianluo" / "state"
        state.mkdir(parents=True)
        (state / "engine.json").write_text("{}", encoding="utf-8")

        delta = compare_snapshots(before, snapshot_workspace(repo))
        assert delta.is_clean is True
        assert delta.changed_paths == []

    def test_tracked_runtime_files_are_excluded_too(self, repo: Path) -> None:
        """``tianluo/code-index.md`` & friends are committed engine artifacts."""
        runtime = repo / "tianluo"
        runtime.mkdir()
        (runtime / "code-index.md").write_text("# map\n", encoding="utf-8")
        _git(repo, "add", "tianluo/code-index.md")
        _git(repo, "commit", "-m", "index")

        before = snapshot_workspace(repo)
        (runtime / "code-index.md").write_text("# map\n- entry\n", encoding="utf-8")

        assert compare_snapshots(before, snapshot_workspace(repo)).is_clean is True

    def test_legacy_runtime_dir_is_excluded(self, repo: Path) -> None:
        """A project still on the ``se3/`` layout excludes *its* runtime dir."""
        legacy = repo / "se3" / "history"
        legacy.mkdir(parents=True)

        before = snapshot_workspace(repo)
        (legacy / "step.jsonl").write_text("{}\n", encoding="utf-8")

        assert compare_snapshots(before, snapshot_workspace(repo)).is_clean is True

    def test_a_lookalike_top_level_file_is_still_watched(self, repo: Path) -> None:
        """``tianluo.example.yaml`` is a project file, not the runtime dir."""
        before = snapshot_workspace(repo)
        (repo / "tianluo.example.yaml").write_text("a: 1\n", encoding="utf-8")

        delta = compare_snapshots(before, snapshot_workspace(repo))
        assert delta.is_clean is False
        assert delta.added_untracked == ["tianluo.example.yaml"]

    def test_real_leftovers_are_still_caught_alongside_engine_writes(
        self, repo: Path
    ) -> None:
        history = repo / "tianluo" / "history"
        history.mkdir(parents=True)

        before = snapshot_workspace(repo)
        (history / "step.jsonl").write_text("{}\n", encoding="utf-8")
        (repo / "tracked.py").write_text("value = 1\nprint('probe')\n", encoding="utf-8")
        (repo / "scratch.py").write_text("probe\n", encoding="utf-8")

        delta = compare_snapshots(before, snapshot_workspace(repo))
        assert delta.is_clean is False
        assert delta.changed_paths == ["scratch.py", "tracked.py"]


class TestDegradation:
    def test_non_repo_degrades_to_undecidable(self, tmp_path: Path) -> None:
        snap = snapshot_workspace(tmp_path)
        assert snap.available is False

        delta = compare_snapshots(snap, snapshot_workspace(tmp_path))
        assert delta.undecidable is True
        # Undecidable must NOT read as dirty: an unavailable git cannot be
        # allowed to fail the step.
        assert delta.is_clean is True
        assert "unavailable" in delta.describe().lower()

    def test_missing_git_binary_degrades(self, repo: Path, monkeypatch) -> None:
        def _boom(*_args, **_kwargs):
            raise FileNotFoundError("git not found")

        monkeypatch.setattr(workspace_snapshot.subprocess, "run", _boom)
        snap = snapshot_workspace(repo)
        assert snap.available is False
        assert compare_snapshots(snap, snap).is_clean is True

    def test_one_sided_unavailability_is_undecidable(self, repo: Path) -> None:
        before = snapshot_workspace(repo)
        after = WorkspaceSnapshot(available=False, unavailable_reason="boom")
        delta = compare_snapshots(before, after)
        assert delta.undecidable is True
        assert delta.is_clean is True


class TestNoWriteOperations:
    """INVARIANT guard: the module must never mutate the workspace."""

    def test_source_contains_no_destructive_git_or_file_writes(self) -> None:
        source = Path(workspace_snapshot.__file__).read_text(encoding="utf-8")
        # Strip comments/docstring prose: the module *documents* that it never
        # resets, so the words legitimately appear in the narrative text. Only
        # executable lines are asserted on.
        code_lines = [
            line for line in source.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        # Drop the docstrings (triple-quoted blocks).
        in_doc = False
        executable: list[str] = []
        for line in code_lines:
            ticks = line.count('"""')
            if in_doc:
                if ticks:
                    in_doc = False
                continue
            if ticks == 1:
                in_doc = True
                continue
            if ticks >= 2:
                continue
            executable.append(line)
        code = "\n".join(executable)

        # Destructive git subcommands can only reach subprocess as quoted argv
        # strings, so the quoted form is what the guard looks for (a bare
        # ``clean`` also matches the legitimate ``is_clean`` property).
        for subcommand in ("reset", "checkout", "stash", "clean", "restore",
                           "rm", "apply"):
            assert f'"{subcommand}"' not in code, (
                f"workspace_snapshot must never run destructive git; found "
                f"git subcommand {subcommand!r} in executable source"
            )
        for writer in ("write_text", "write_bytes", "unlink", "rmtree",
                       "mkdir", "open("):
            assert writer not in code, (
                f"workspace_snapshot must never write to disk; found "
                f"{writer!r} in executable source"
            )

    def test_snapshotting_does_not_touch_the_working_tree(self, repo: Path) -> None:
        (repo / "tracked.py").write_text("dirty = True\n", encoding="utf-8")
        (repo / "untracked.txt").write_text("keep me\n", encoding="utf-8")

        snapshot_workspace(repo)
        snapshot_workspace(repo)

        assert (repo / "tracked.py").read_text(encoding="utf-8") == "dirty = True\n"
        assert (repo / "untracked.txt").read_text(encoding="utf-8") == "keep me\n"
