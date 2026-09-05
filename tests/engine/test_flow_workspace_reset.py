"""Pre-flow workspace snapshot, discard safe-ref, and reset.

``workspace: reset`` is the one dialog decision that destroys visible work, so
these tests are about the two things that make it safe: nothing is lost (the
discard ref), and nothing that predates the flow is collateral damage (the
pre-flow dirty snapshot).
"""

from __future__ import annotations

import pathlib
import subprocess
import types

import pytest

from tianluo.engine.flow_workspace import (
    DIRTY_SNAPSHOT_CONTEXT_KEY,
    capture_baseline_dirty_state,
    preview_reset,
    reset_workspace_to_baseline,
    status_summary,
    workspace_is_dirty,
)


def _git(root, *args, check=True):
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=check
    )


@pytest.fixture
def repo(tmp_path):
    """A committed repo with a runtime dir, ready for a flow to run in."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", ".")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("base\n", encoding="utf-8")
    (root / "tianluo" / "state").mkdir(parents=True)
    (root / "tianluo" / "state" / "engine.json").write_text("{}", encoding="utf-8")
    _git(root, "add", "a.txt")
    _git(root, "commit", "-qm", "base")
    return root


def _flow(repo, flow_id="F1"):
    state = types.SimpleNamespace(context={})
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    return types.SimpleNamespace(flow_id=flow_id, state=state, baseline_commit=head)


class TestCapture:
    def test_capture_records_a_ref_and_commit(self, repo):
        flow = _flow(repo)
        record = capture_baseline_dirty_state(flow, repo)
        assert record["captured"] is True
        assert record["ref"].startswith("refs/tianluo/baseline-dirty/")
        assert _git(repo, "rev-parse", record["ref"]).stdout.strip() == record["commit"]

    def test_capture_is_idempotent(self, repo):
        flow = _flow(repo)
        first = capture_baseline_dirty_state(flow, repo)
        (repo / "a.txt").write_text("changed after capture\n", encoding="utf-8")
        second = capture_baseline_dirty_state(flow, repo)
        assert second["commit"] == first["commit"]

    def test_capture_does_not_disturb_the_index(self, repo):
        """A capture must never touch what the operator has staged."""
        (repo / "staged.txt").write_text("staged\n", encoding="utf-8")
        _git(repo, "add", "staged.txt")
        before = _git(repo, "diff", "--cached", "--name-only").stdout
        capture_baseline_dirty_state(_flow(repo), repo)
        assert _git(repo, "diff", "--cached", "--name-only").stdout == before

    def test_capture_records_the_dirty_flag(self, repo):
        clean = capture_baseline_dirty_state(_flow(repo, "clean"), repo)
        assert clean["was_dirty"] is False
        (repo / "a.txt").write_text("dirty\n", encoding="utf-8")
        dirty = capture_baseline_dirty_state(_flow(repo, "dirty"), repo)
        assert dirty["was_dirty"] is True

    def test_capture_on_a_repo_without_commits_degrades(self, tmp_path):
        root = tmp_path / "empty"
        root.mkdir()
        _git(root, "init", "-q", ".")
        flow = types.SimpleNamespace(
            flow_id="E", state=types.SimpleNamespace(context={}), baseline_commit=""
        )
        record = capture_baseline_dirty_state(flow, root)
        assert record["captured"] is False
        assert record["reason"]


class TestRuntimeDirExclusion:
    def test_runtime_dir_is_not_reported_as_dirty(self, repo):
        (repo / "tianluo" / "state" / "engine.json").write_text(
            "live", encoding="utf-8"
        )
        assert workspace_is_dirty(repo) is False
        assert status_summary(repo) == ""

    def test_reset_leaves_the_live_runtime_state_alone(self, repo):
        """The flow is writing its own record while the reset runs."""
        flow = _flow(repo)
        capture_baseline_dirty_state(flow, repo)
        (repo / "tianluo" / "state" / "engine.json").write_text(
            "LIVE-FLOW-STATE", encoding="utf-8"
        )
        (repo / "a.txt").write_text("flow edit\n", encoding="utf-8")
        reset_workspace_to_baseline(flow, repo)
        assert (
            repo / "tianluo" / "state" / "engine.json"
        ).read_text(encoding="utf-8") == "LIVE-FLOW-STATE"

    def test_tracked_runtime_assets_are_captured_previewed_and_restored(self, repo):
        """``tianluo/issues/**`` and friends are project files, not flow state.

        ``git reset --hard`` reverts them, so excluding the whole runtime dir
        would discard a pre-flow edit to one WITHOUT it appearing in the dirty
        snapshot, the discard safety ref, or the pre-confirmation preview.
        """
        issue = repo / "tianluo" / "issues" / "open" / "207.yaml"
        issue.parent.mkdir(parents=True)
        issue.write_text("committed\n", encoding="utf-8")
        _git(repo, "add", "-f", "tianluo/issues/open/207.yaml")
        _git(repo, "commit", "-qm", "issue")

        flow = _flow(repo)
        flow.baseline_commit = _git(repo, "rev-parse", "HEAD").stdout.strip()
        # The operator's uncommitted edit, made BEFORE the flow started.
        issue.write_text("my pre-flow edit\n", encoding="utf-8")
        capture_baseline_dirty_state(flow, repo)

        assert "tianluo/issues/open/207.yaml" in status_summary(repo)

        (repo / "a.txt").write_text("flow edit\n", encoding="utf-8")
        result = reset_workspace_to_baseline(flow, repo)

        assert result.ok
        assert "tianluo/issues/open/207.yaml" in result.discarded_summary
        # Restored from the dirty snapshot, not reverted to HEAD.
        assert issue.read_text(encoding="utf-8") == "my pre-flow edit\n"
        # And recoverable from the safety ref either way.
        saved = _git(
            repo, "show", f"{result.safe_ref}:tianluo/issues/open/207.yaml"
        ).stdout
        assert "my pre-flow edit" in saved


    def test_flow_created_untracked_runtime_files_are_removed(self, repo):
        """Untracked ``tianluo/issues/**`` the flow wrote is flow output, not state.

        Excluding the whole runtime dir from the cleanup (the price of
        ``git clean -fd``'s per-directory granularity) left such a file behind,
        so the rewound step read back stale output from the very attempt the
        reset had just discarded.
        """
        flow = _flow(repo)
        capture_baseline_dirty_state(flow, repo)

        issue = repo / "tianluo" / "issues" / "open" / "301.yaml"
        issue.parent.mkdir(parents=True)
        issue.write_text("written by the discarded attempt\n", encoding="utf-8")
        (repo / "new_source.py").write_text("flow wrote this\n", encoding="utf-8")
        (repo / "tianluo" / "state" / "engine.json").write_text(
            "LIVE-FLOW-STATE", encoding="utf-8"
        )

        result = reset_workspace_to_baseline(flow, repo)

        assert result.ok
        assert not issue.exists()
        # The empty husk goes too — git tracks no directories, so it is debris.
        assert not issue.parent.exists()
        assert not (repo / "new_source.py").exists()
        # ...while the live flow's own record is untouched.
        assert (
            repo / "tianluo" / "state" / "engine.json"
        ).read_text(encoding="utf-8") == "LIVE-FLOW-STATE"
        # Nothing was destroyed: the discarded file is in the safety ref.
        saved = _git(
            repo, "show", f"{result.safe_ref}:tianluo/issues/open/301.yaml"
        ).stdout
        assert "written by the discarded attempt" in saved

    def test_pre_flow_untracked_runtime_files_survive_the_reset(self, repo):
        """An untracked runtime file that predates the flow is replayed back."""
        issue = repo / "tianluo" / "issues" / "open" / "207.yaml"
        issue.parent.mkdir(parents=True)
        issue.write_text("mine, from before the flow\n", encoding="utf-8")
        flow = _flow(repo)
        capture_baseline_dirty_state(flow, repo)

        (repo / "a.txt").write_text("flow edit\n", encoding="utf-8")
        result = reset_workspace_to_baseline(flow, repo)

        assert result.ok
        assert issue.read_text(encoding="utf-8") == "mine, from before the flow\n"


class TestReset:
    def test_flow_changes_are_undone_and_pre_flow_state_replayed(self, repo):
        (repo / "a.txt").write_text("base\npre-existing\n", encoding="utf-8")
        (repo / "pre_untracked.txt").write_text("mine\n", encoding="utf-8")
        flow = _flow(repo)
        capture_baseline_dirty_state(flow, repo)

        # The flow works: edits, an untracked file, and a commit.
        (repo / "a.txt").write_text("base\npre-existing\nFLOW\n", encoding="utf-8")
        (repo / "flow_new.txt").write_text("flow\n", encoding="utf-8")
        _git(repo, "add", "flow_new.txt")
        _git(repo, "commit", "-qm", "flow work")
        (repo / "flow_uncommitted.txt").write_text("wip\n", encoding="utf-8")

        result = reset_workspace_to_baseline(flow, repo)

        assert result.ok, result.error
        assert result.restored_snapshot is True
        # Pre-flow state survives.
        assert (repo / "a.txt").read_text(encoding="utf-8") == "base\npre-existing\n"
        assert (repo / "pre_untracked.txt").exists()
        # Flow output is gone.
        assert not (repo / "flow_new.txt").exists()
        assert not (repo / "flow_uncommitted.txt").exists()
        assert _git(repo, "rev-parse", "HEAD").stdout.strip() == flow.baseline_commit

    def test_discarded_work_is_recoverable_from_the_safe_ref(self, repo):
        flow = _flow(repo)
        capture_baseline_dirty_state(flow, repo)
        (repo / "flow_new.txt").write_text("IRREPLACEABLE\n", encoding="utf-8")

        result = reset_workspace_to_baseline(flow, repo)

        assert result.safe_ref.startswith("refs/tianluo/discarded/F1/")
        assert result.recovery_hint()
        blob = _git(repo, "show", f"{result.safe_ref}:flow_new.txt").stdout
        assert "IRREPLACEABLE" in blob

    def test_the_safe_ref_records_flow_commits_too(self, repo):
        flow = _flow(repo)
        capture_baseline_dirty_state(flow, repo)
        (repo / "committed.txt").write_text("c\n", encoding="utf-8")
        _git(repo, "add", "committed.txt")
        _git(repo, "commit", "-qm", "flow commit")

        result = reset_workspace_to_baseline(flow, repo)
        assert any("flow commit" in line for line in result.flow_commits)
        assert "committed.txt" in _git(
            repo, "ls-tree", "-r", "--name-only", result.safe_ref
        ).stdout

    def test_reset_replays_a_pre_flow_deletion(self, repo):
        """The snapshot is a tree, so a path it deleted is deleted again."""
        (repo / "a.txt").unlink()
        flow = _flow(repo)
        capture_baseline_dirty_state(flow, repo)
        (repo / "a.txt").write_text("resurrected by the flow\n", encoding="utf-8")

        reset_workspace_to_baseline(flow, repo)
        assert not (repo / "a.txt").exists()

    def test_reset_without_a_snapshot_warns_instead_of_guessing(self, repo):
        """An old flow can restore tracked files, and must say what it cannot.

        Untracked files are LEFT ALONE: without a snapshot nothing can tell a
        file the flow created from one that was already there, and deleting
        something it could never put back is precisely the unrecoverable loss
        this module exists to prevent.
        """
        flow = _flow(repo)
        (repo / "pre_untracked.txt").write_text("mine\n", encoding="utf-8")
        (repo / "a.txt").write_text("flow edit\n", encoding="utf-8")

        result = reset_workspace_to_baseline(flow, repo)

        assert result.ok
        assert result.restored_snapshot is False
        assert (repo / "a.txt").read_text(encoding="utf-8") == "base\n"
        assert (repo / "pre_untracked.txt").read_text(encoding="utf-8") == "mine\n"
        from tianluo.i18n import t

        assert result.warning == t("engine.workspace.reset_no_snapshot")

    def test_a_resumed_legacy_flow_is_not_given_a_fabricated_snapshot(self, repo):
        """``init_flow`` runs again on ``--resume``. A flow that has already
        executed steps has already changed the tree, so a capture taken then
        would label its own output "pre-flow state" and the reset would replay
        exactly the work it was asked to discard."""
        flow = _flow(repo)
        (repo / "a.txt").write_text("edited by the flow\n", encoding="utf-8")

        record = capture_baseline_dirty_state(flow, repo, flow_started=True)

        assert record["captured"] is False
        assert record.get("reason")
        result = reset_workspace_to_baseline(flow, repo)
        assert result.restored_snapshot is False
        assert (repo / "a.txt").read_text(encoding="utf-8") == "base\n"

    def test_an_undeletable_flow_file_makes_the_reset_fail(self, repo, monkeypatch):
        """A survivor means the tree is not "baseline + pre-flow snapshot".

        Reporting success would let the rewind run and hand the rebuilt step
        the very output the operator asked to throw away, so an unlink that
        fails has to fail the whole reset.
        """
        flow = _flow(repo)
        capture_baseline_dirty_state(flow, repo)
        stuck = repo / "flow_locked.txt"
        stuck.write_text("flow output\n", encoding="utf-8")

        real_unlink = pathlib.Path.unlink

        def _unlink(self, *a, **kw):
            if self.name == "flow_locked.txt":
                raise PermissionError(13, "Permission denied")
            return real_unlink(self, *a, **kw)

        monkeypatch.setattr(pathlib.Path, "unlink", _unlink)
        result = reset_workspace_to_baseline(flow, repo)
        monkeypatch.undo()

        assert result.ok is False
        assert "flow_locked.txt" in result.error
        # Not claimed as restored, and the safety ref still holds everything.
        assert result.restored_snapshot is False
        assert result.safe_ref
        assert stuck.exists()

    def test_a_failed_snapshot_deletion_replay_also_fails_the_reset(
        self, repo, monkeypatch
    ):
        """The snapshot recorded the path as absent; a survivor contradicts it."""
        import tianluo.engine.flow_workspace as fw

        (repo / "a.txt").unlink()
        flow = _flow(repo)
        capture_baseline_dirty_state(flow, repo)
        (repo / "a.txt").write_text("resurrected by the flow\n", encoding="utf-8")

        monkeypatch.setattr(fw, "_remove_untracked_files", lambda root: [])
        real_unlink = pathlib.Path.unlink

        def _unlink(self, *a, **kw):
            if self.name == "a.txt":
                raise OSError(5, "I/O error")
            return real_unlink(self, *a, **kw)

        monkeypatch.setattr(pathlib.Path, "unlink", _unlink)
        result = reset_workspace_to_baseline(flow, repo)
        monkeypatch.undo()

        assert result.ok is False
        assert result.restored_snapshot is False
        assert "a.txt" in result.error

    def test_reset_without_a_baseline_commit_fails_cleanly(self, repo):
        flow = _flow(repo)
        flow.baseline_commit = ""
        result = reset_workspace_to_baseline(flow, repo)
        assert result.ok is False
        # Localised, not a raw English sentence interpolated into a translated
        # CLI wrapper.
        from tianluo.i18n import t

        assert result.error == t("engine.workspace.reset_no_baseline")


class TestPreview:
    def test_preview_reports_what_would_be_discarded(self, repo):
        flow = _flow(repo)
        capture_baseline_dirty_state(flow, repo)
        (repo / "a.txt").write_text("flow edit\n", encoding="utf-8")
        (repo / "new.txt").write_text("new\n", encoding="utf-8")
        _git(repo, "add", "new.txt")
        _git(repo, "commit", "-qm", "flow commit")

        preview = preview_reset(flow, repo)

        assert "a.txt" in preview.status_summary
        assert any("flow commit" in c for c in preview.flow_commits)
        assert preview.has_dirty_snapshot is True
        assert preview.snapshot_warning is False

    def test_preview_flags_a_missing_snapshot(self, repo):
        preview = preview_reset(_flow(repo), repo)
        assert preview.has_dirty_snapshot is False
        assert preview.snapshot_warning is True

    def test_preview_is_read_only(self, repo):
        flow = _flow(repo)
        (repo / "a.txt").write_text("flow edit\n", encoding="utf-8")
        preview_reset(flow, repo)
        assert (repo / "a.txt").read_text(encoding="utf-8") == "flow edit\n"
        assert DIRTY_SNAPSHOT_CONTEXT_KEY not in flow.state.context


class TestPreviewFailure:
    def test_a_git_failure_is_reported_rather_than_read_as_a_clean_tree(
        self, repo, monkeypatch
    ):
        """Empty ``git status`` output reads as "nothing to lose" — the exact
        opposite of what a failed ``git status`` means, so the preview must come
        back not-ok instead."""
        from tianluo.engine import flow_workspace

        flow = _flow(repo)
        monkeypatch.setattr(
            flow_workspace,
            "status_summary",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("git exploded")),
        )
        preview = preview_reset(flow, repo)
        assert preview.ok is False
        assert "git exploded" in preview.error
        assert preview.status_summary == ""


class TestSnapshotGuard:
    def test_a_brand_new_flow_captures_its_pre_flow_dirty_state(self, repo):
        """``create_flow`` already appends the first PENDING step, so "the flow
        has a step history" is true of every new flow — keying the guard off it
        left every normal flow snapshot-less."""
        from tianluo.engine.models import (
            FlowInstance, FlowStatus, Step, StepStatus, StepType,
        )
        from tianluo.engine.state_machine import StateMachine

        (repo / "handwritten.txt").write_text("mine\n", encoding="utf-8")
        flow = FlowInstance(task_description="t", task_type="feature",
                            status=FlowStatus.INIT)
        flow.state.add_step(Step(step_type=StepType.ANALYZE,
                                 status=StepStatus.PENDING))
        assert StateMachine._flow_has_executed_a_step(flow) is False

        flow.baseline_commit = _git(repo, "rev-parse", "HEAD").stdout.strip()
        record = capture_baseline_dirty_state(
            flow, repo,
            flow_started=StateMachine._flow_has_executed_a_step(flow),
        )
        assert record["captured"] is True
        assert record["was_dirty"] is True

    def test_a_flow_that_has_run_a_step_stays_snapshot_less(self, repo):
        from tianluo.engine.models import (
            FlowInstance, FlowStatus, Step, StepStatus, StepType,
        )
        from tianluo.engine.state_machine import StateMachine

        flow = FlowInstance(task_description="t", task_type="feature",
                            status=FlowStatus.INIT)
        flow.state.add_step(Step(step_type=StepType.ANALYZE,
                                 status=StepStatus.COMPLETED))
        flow.state.add_step(Step(step_type=StepType.PLAN,
                                 status=StepStatus.PENDING))
        assert StateMachine._flow_has_executed_a_step(flow) is True


class TestGroupWorkPreservation:
    """A parallel implement group's work lives on a leaf branch inside its own
    worktree. Neither the main tree's ``git status`` nor ``baseline..HEAD``
    shows any of it, so the reset facilities above are blind to it — and a
    restart deletes both the branch and the worktree."""

    def _group(self, repo, name="G1", *, commit=True, dirty=True):
        from tianluo.engine.flow_workspace import _worktree_path_for_branch

        branch = f"impl/F1/{name}"
        wt = repo.parent / f"wt-{name}"
        _git(repo, "worktree", "add", "-q", "-b", branch, str(wt))
        _git(wt, "config", "user.email", "t@example.com")
        _git(wt, "config", "user.name", "t")
        if commit:
            (wt / f"{name}.txt").write_text("committed\n", encoding="utf-8")
            _git(wt, "add", f"{name}.txt")
            _git(wt, "commit", "-qm", f"{name}: committed work")
        if dirty:
            (wt / f"{name}-wip.txt").write_text("uncommitted\n", encoding="utf-8")
        assert _worktree_path_for_branch(repo, branch) == str(wt)
        return branch, wt

    def test_preview_reports_commits_and_uncommitted_edits(self, repo):
        from tianluo.engine.flow_workspace import preview_group_work

        branch, wt = self._group(repo)
        baseline = _git(repo, "rev-parse", "HEAD").stdout.strip()

        previews = preview_group_work(repo, [branch], baseline)

        assert len(previews) == 1
        pv = previews[0]
        assert pv.branch == branch
        assert pv.worktree_path == str(wt)
        assert any("committed work" in c for c in pv.commits)
        assert "G1-wip.txt" in pv.status_summary
        assert pv.has_work

    def test_an_unknown_branch_reports_nothing(self, repo):
        from tianluo.engine.flow_workspace import preview_group_work

        assert preview_group_work(repo, ["impl/F1/nope"], "") == []

    def test_preserve_keeps_the_commits_reachable_after_deletion(self, repo):
        from tianluo.engine.flow_workspace import preserve_group_work

        branch, wt = self._group(repo)
        tip = _git(repo, "rev-parse", f"refs/heads/{branch}").stdout.strip()

        refs = preserve_group_work(repo, "F1", [branch])
        assert len(refs) == 1
        ref = refs[0]
        assert ref.startswith("refs/tianluo/discarded/F1/")
        assert ref.endswith("/groups/impl_F1_G1")

        # Now do what the rewind does: destroy both.
        _git(repo, "worktree", "remove", "--force", str(wt))
        _git(repo, "branch", "-D", branch)

        # The commits are still reachable through the safe ref...
        saved = _git(repo, "rev-parse", ref).stdout.strip()
        assert saved
        log = _git(repo, "log", "--oneline", ref).stdout
        assert "committed work" in log
        assert tip in _git(repo, "rev-list", ref).stdout
        # ...and so are the uncommitted edits, as the ref's own tip commit.
        listing = _git(repo, "ls-tree", "-r", "--name-only", saved).stdout
        assert "G1-wip.txt" in listing

    def test_a_clean_group_is_preserved_at_its_branch_tip(self, repo):
        from tianluo.engine.flow_workspace import preserve_group_work

        branch, wt = self._group(repo, "G2", dirty=False)
        refs = preserve_group_work(repo, "F1", [branch])
        assert len(refs) == 1
        assert "G2: committed work" in _git(repo, "log", "--oneline", refs[0]).stdout

    def test_a_never_materialised_branch_is_verified_empty_not_fatal(self, repo):
        """A planned group whose worktree the run never created has no branch
        and no checkout: there is nothing to lose, so the cleanup may proceed."""
        from tianluo.engine.flow_workspace import preserve_group_work

        assert preserve_group_work(repo, "F1", ["impl/F1/ghost"]) == []

    def test_no_branches_is_a_noop(self, repo):
        from tianluo.engine.flow_workspace import preserve_group_work

        assert preserve_group_work(repo, "F1", []) == []

    def test_a_capture_failure_raises_instead_of_skipping_the_group(self, repo):
        """The caller deletes every branch it hands in right after this
        returns, so "logged and skipped" means deleting a materialised group's
        worktree with no ref pointing at its edits."""
        from tianluo.engine import flow_workspace
        from tianluo.engine.flow_workspace import (
            GroupPreservationError, preserve_group_work,
        )

        branch, wt = self._group(repo)
        monkey = flow_workspace._commit_tree

        def boom(*_a, **_k):
            raise RuntimeError("commit-tree exploded")

        flow_workspace._commit_tree = boom
        try:
            with pytest.raises(GroupPreservationError) as excinfo:
                preserve_group_work(repo, "F1", [branch])
        finally:
            flow_workspace._commit_tree = monkey

        assert excinfo.value.branch == branch
        assert "commit-tree exploded" in excinfo.value.reason
        # Nothing was captured, and — crucially — nothing was destroyed either.
        assert (wt / "G1-wip.txt").exists()
        assert _git(repo, "rev-parse", "--verify", "--quiet",
                    f"refs/heads/{branch}", check=False).returncode == 0

    def test_a_ref_write_failure_raises_with_the_earlier_refs_reported(self, repo):
        from tianluo.engine import flow_workspace
        from tianluo.engine.flow_workspace import (
            GroupPreservationError, preserve_group_work,
        )

        first, _ = self._group(repo, "G1")
        second, _ = self._group(repo, "G2")
        real_git = flow_workspace._git
        seen = {"refs": 0}

        def fake_git(root, *args, **kwargs):
            if args and args[0] == "update-ref":
                seen["refs"] += 1
                if seen["refs"] == 2:
                    raise RuntimeError("update-ref exploded")
            return real_git(root, *args, **kwargs)

        flow_workspace._git = fake_git
        try:
            with pytest.raises(GroupPreservationError) as excinfo:
                preserve_group_work(repo, "F1", [first, second])
        finally:
            flow_workspace._git = real_git

        assert excinfo.value.branch == second
        assert len(excinfo.value.preserved) == 1
        assert excinfo.value.preserved[0].endswith("/groups/impl_F1_G1")

    def test_reset_and_group_preservation_refs_do_not_collide(self, repo):
        """The reset ref and the group refs share a one-second stamp.

        A restart of a materialised parallel implement with ``workspace: reset``
        writes the main-tree discard and then, normally inside the same second,
        one ref per group. Parking the main discard AT the stamp node made git
        refuse every group's ``update-ref`` (a ref cannot live underneath a
        ref), aborting the rewind on an already-reset workspace.
        """
        import time as _time

        from tianluo.engine import flow_workspace
        from tianluo.engine.flow_workspace import (
            preserve_group_work, reset_workspace_to_baseline,
        )

        branch, _wt = self._group(repo)
        flow = _flow(repo)
        capture_baseline_dirty_state(flow, repo)
        (repo / "flow_new.txt").write_text("flow output\n", encoding="utf-8")

        # Freeze the stamp so both operations land in the same second.
        real_strftime = _time.strftime
        flow_workspace.time.strftime = lambda fmt: real_strftime(fmt, _time.gmtime(0))
        try:
            reset = reset_workspace_to_baseline(flow, repo)
            assert reset.ok, reset.error
            refs = preserve_group_work(repo, flow.flow_id, [branch])
        finally:
            flow_workspace.time.strftime = real_strftime

        assert len(refs) == 1
        assert _git(repo, "rev-parse", reset.safe_ref).stdout.strip()
        assert _git(repo, "rev-parse", refs[0]).stdout.strip()

    def test_two_discards_in_the_same_second_both_survive(self, repo):
        """The ref is the only thing keeping the objects reachable, so the
        second discard must not silently clobber the first."""
        import time as _time

        from tianluo.engine import flow_workspace
        from tianluo.engine.flow_workspace import reset_workspace_to_baseline

        flow = _flow(repo)
        capture_baseline_dirty_state(flow, repo)
        real_strftime = _time.strftime
        flow_workspace.time.strftime = lambda fmt: real_strftime(fmt, _time.gmtime(0))
        try:
            (repo / "first.txt").write_text("first\n", encoding="utf-8")
            first = reset_workspace_to_baseline(flow, repo)
            (repo / "second.txt").write_text("second\n", encoding="utf-8")
            second = reset_workspace_to_baseline(flow, repo)
        finally:
            flow_workspace.time.strftime = real_strftime

        assert first.ok and second.ok
        assert first.safe_ref != second.safe_ref
        assert "first.txt" in _git(
            repo, "ls-tree", "-r", "--name-only", first.safe_ref
        ).stdout
        assert "second.txt" in _git(
            repo, "ls-tree", "-r", "--name-only", second.safe_ref
        ).stdout

    def test_an_unreadable_worktree_probe_aborts_preservation(self, repo):
        """A failed ``git worktree list`` used to read as "this group has no
        worktree", so preservation saved only the branch tip and the cleanup
        then deleted the uncommitted edits with no ref pointing at them."""
        from tianluo.engine import flow_workspace
        from tianluo.engine.flow_workspace import (
            GroupPreservationError, preserve_group_work,
        )

        branch, wt = self._group(repo)
        real_git = flow_workspace._git

        def fake_git(root, *args, **kwargs):
            # Honours the caller's own ``check``, so the test distinguishes
            # "probe raised" from "probe quietly answered no worktree".
            if args[:2] == ("worktree", "list"):
                return real_git(root, "worktree", "list", "--bogus-flag",
                                **kwargs)
            return real_git(root, *args, **kwargs)

        flow_workspace._git = fake_git
        try:
            with pytest.raises(GroupPreservationError) as excinfo:
                preserve_group_work(repo, "F1", [branch])
        finally:
            flow_workspace._git = real_git

        assert excinfo.value.branch == branch
        # Nothing was destroyed: the rewind is refused with the edits intact.
        assert (wt / "G1-wip.txt").exists()

    def test_a_failed_branch_probe_aborts_preservation(self, repo):
        """A branch whose worktree is already gone is held together by its tip
        alone. A probe that fails used to read as "no branch under this name",
        skipping it with no recovery ref — and the cleanup then deletes it for
        real once the git fault clears."""
        from tianluo.engine import flow_workspace
        from tianluo.engine.flow_workspace import (
            GroupPreservationError, preserve_group_work,
        )

        branch, wt = self._group(repo, "G3", dirty=False)
        _git(repo, "worktree", "remove", "--force", str(wt))
        tip = _git(repo, "rev-parse", f"refs/heads/{branch}").stdout.strip()
        real_git = flow_workspace._git

        def fake_git(root, *args, **kwargs):
            if args[:2] == ("rev-parse", "--verify"):
                return subprocess.CompletedProcess(
                    ["git", *args], 128, "", "fatal: unable to read refs",
                )
            return real_git(root, *args, **kwargs)

        flow_workspace._git = fake_git
        try:
            with pytest.raises(GroupPreservationError) as excinfo:
                preserve_group_work(repo, "F1", [branch])
        finally:
            flow_workspace._git = real_git

        assert excinfo.value.branch == branch
        # The rewind is refused with the group's commits still on the branch.
        assert tip == _git(
            repo, "rev-parse", f"refs/heads/{branch}"
        ).stdout.strip()

    def test_a_warning_on_the_branch_probe_aborts_preservation(self, repo):
        """A broken ref store answers ``rev-parse`` with exit 1 and a
        ``warning:`` line, not a ``fatal:`` one — and git's diagnostics are
        localisable besides. Reading that as "no branch under this name" skips
        the group with no recovery ref, and the cleanup deletes its commits for
        real once the fault clears."""
        from tianluo.engine import flow_workspace
        from tianluo.engine.flow_workspace import (
            GroupPreservationError, preserve_group_work,
        )

        branch, wt = self._group(repo, "G5", dirty=False)
        _git(repo, "worktree", "remove", "--force", str(wt))
        tip = _git(repo, "rev-parse", f"refs/heads/{branch}").stdout.strip()
        real_git = flow_workspace._git

        def fake_git(root, *args, **kwargs):
            if args[:2] == ("rev-parse", "--verify"):
                return subprocess.CompletedProcess(
                    ["git", *args], 1, "",
                    "warning: ignoring broken ref refs/heads/" + branch,
                )
            return real_git(root, *args, **kwargs)

        flow_workspace._git = fake_git
        try:
            with pytest.raises(GroupPreservationError) as excinfo:
                preserve_group_work(repo, "F1", [branch])
        finally:
            flow_workspace._git = real_git

        assert excinfo.value.branch == branch
        assert tip == _git(
            repo, "rev-parse", f"refs/heads/{branch}"
        ).stdout.strip()

    def test_branch_probe_separates_absent_from_unanswerable(self, repo):
        from tianluo.engine import flow_workspace
        from tianluo.engine.flow_workspace import _branch_exists

        branch, _ = self._group(repo, "G4", dirty=False)
        assert _branch_exists(repo, branch) is True
        # git is silent and exits 1 for the genuinely missing ref.
        assert _branch_exists(repo, "impl/F1/nope") is False

        real_git = flow_workspace._git

        def no_git(*_a, **_k):
            raise FileNotFoundError("git")

        flow_workspace._git = no_git
        try:
            with pytest.raises(RuntimeError):
                _branch_exists(repo, branch)
        finally:
            flow_workspace._git = real_git

        # Exit 1 is only "absent" when git said nothing at all: any diagnostic
        # alongside it means the lookup itself is in doubt.
        for stdout, stderr in (
            ("", "warning: ignoring broken ref refs/heads/x"),
            ("", "avertissement : ref cassée"),
            ("refs/heads/x", ""),
        ):
            flow_workspace._git = (
                lambda *_a, _o=stdout, _e=stderr, **_k:
                subprocess.CompletedProcess(["git"], 1, _o, _e)
            )
            try:
                with pytest.raises(RuntimeError):
                    _branch_exists(repo, branch)
            finally:
                flow_workspace._git = real_git

    def test_group_cleanup_residue_reports_a_surviving_branch(self, repo):
        from tianluo.engine.flow_workspace import group_cleanup_residue

        branch, wt = self._group(repo)
        residue = group_cleanup_residue(repo, branch)
        assert any("branch" in r for r in residue)
        assert any(str(wt) in r for r in residue)

        _git(repo, "worktree", "remove", "--force", str(wt))
        _git(repo, "branch", "-D", branch)
        assert group_cleanup_residue(repo, branch) == []


class TestSnapshotReplayEdgeCases:
    def test_a_copy_record_restores_the_source_too(self, repo):
        """With copy detection on, ``C src dst`` says the snapshot holds BOTH
        paths. Treating it like a rename deleted a pre-flow file and still
        reported the snapshot as fully restored."""
        from tianluo.engine.flow_workspace import _restore_snapshot

        # Copy detection only fires on a repo that has it turned on.
        _git(repo, "config", "diff.renames", "copies")
        (repo / "src.txt").write_text("l1\nl2\nl3\nl4\nl5\n", encoding="utf-8")
        _git(repo, "add", "src.txt")
        _git(repo, "commit", "-qm", "baseline side")
        baseline = _git(repo, "rev-parse", "HEAD").stdout.strip()

        # The pre-flow tree: src.txt modified, and copied to a new path. git
        # then reports "C<score> src.txt dst.txt" plus "M src.txt".
        (repo / "src.txt").write_text("l1\nl2\nl3\nl4\nl5\nl6\n", encoding="utf-8")
        (repo / "dst.txt").write_text("l1\nl2\nl3\nl4\nl5\nl6\n", encoding="utf-8")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "snapshot side")
        snapshot = _git(repo, "rev-parse", "HEAD").stdout.strip()
        assert "C" in _git(
            repo, "diff", "--name-status", baseline, snapshot
        ).stdout

        # Simulate the state after `reset --hard baseline`.
        _git(repo, "reset", "--hard", baseline)

        assert _restore_snapshot(repo, baseline, snapshot) == []
        expected = "l1\nl2\nl3\nl4\nl5\nl6\n"
        assert (repo / "dst.txt").read_text(encoding="utf-8") == expected
        assert (repo / "src.txt").read_text(encoding="utf-8") == expected

    def test_a_rename_record_still_deletes_the_source(self, repo):
        from tianluo.engine.flow_workspace import _restore_snapshot

        (repo / "old.txt").write_text("moved\n", encoding="utf-8")
        _git(repo, "add", "old.txt")
        _git(repo, "commit", "-qm", "baseline side")
        baseline = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _git(repo, "mv", "old.txt", "new.txt")
        _git(repo, "commit", "-qm", "snapshot side")
        snapshot = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _git(repo, "reset", "--hard", baseline)

        assert _restore_snapshot(repo, baseline, snapshot) == []
        assert (repo / "new.txt").exists()
        assert not (repo / "old.txt").exists()

    def test_an_untracked_directory_entry_fails_the_reset(self, repo):
        """``ls-files --others`` only names a directory for an embedded repo,
        whose contents ``git add -A`` never put in the safety ref. It can be
        neither deleted nor silently left behind."""
        from tianluo.engine.flow_workspace import _remove_untracked_files

        nested = repo / "nested"
        nested.mkdir()
        _git(nested, "init", "-q", ".")
        _git(nested, "config", "user.email", "t@example.com")
        _git(nested, "config", "user.name", "t")
        (nested / "inner.txt").write_text("inner\n", encoding="utf-8")
        _git(nested, "add", "inner.txt")
        _git(nested, "commit", "-qm", "inner")

        failed = _remove_untracked_files(repo)
        assert any(path.rstrip("/") == "nested" for path in failed)
        assert (nested / "inner.txt").exists()

    def test_a_plain_untracked_file_is_still_removed(self, repo):
        from tianluo.engine.flow_workspace import _remove_untracked_files

        (repo / "junk.txt").write_text("junk\n", encoding="utf-8")
        assert _remove_untracked_files(repo) == []
        assert not (repo / "junk.txt").exists()


class TestRecoveryRefReservation:
    def test_a_legacy_ref_at_the_stamp_node_does_not_block_a_reset(self, repo):
        """Older versions parked the whole discard AT ``<flow_id>/<stamp>``.
        git refuses to create a ref underneath one, so the reservation has to
        move the stamped namespace aside rather than rename the leaf."""
        import time as _time

        from tianluo.engine import flow_workspace
        from tianluo.engine.flow_workspace import reset_workspace_to_baseline

        flow = _flow(repo)
        capture_baseline_dirty_state(flow, repo)
        real_strftime = _time.strftime
        stamp = real_strftime("%Y%m%d-%H%M%S", _time.gmtime(0))
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        _git(repo, "update-ref", f"refs/tianluo/discarded/F1/{stamp}", head)

        flow_workspace.time.strftime = lambda fmt: stamp
        try:
            (repo / "flow_new.txt").write_text("flow output\n", encoding="utf-8")
            result = reset_workspace_to_baseline(flow, repo)
        finally:
            flow_workspace.time.strftime = real_strftime

        assert result.ok, result.error
        assert _git(repo, "rev-parse", result.safe_ref).stdout.strip()
        # The legacy ref is untouched.
        assert _git(
            repo, "rev-parse", f"refs/tianluo/discarded/F1/{stamp}"
        ).stdout.strip() == head


class TestMaterialisedGroupBranches:
    """Which derived group branch names actually held work.

    The rewind derives ``impl/<flow>/<group>`` names from the plan, so a name
    exists for every planned group whether or not anything was ever created
    under it. Only the ones that really exist make a step's recorded group
    results dangle once the rewind deleted them.
    """

    def test_only_existing_branches_and_worktrees_are_reported(self, repo):
        from tianluo.engine.flow_workspace import materialised_group_branches

        _git(repo, "branch", "impl/F1/G1")
        assert materialised_group_branches(
            repo, ["impl/F1/G1", "impl/F1/G2"]
        ) == ["impl/F1/G1"]

    def test_a_checked_out_worktree_counts_even_without_its_branch_listed(
        self, repo, tmp_path,
    ):
        from tianluo.engine.flow_workspace import materialised_group_branches

        _git(repo, "worktree", "add", "-q", "-b", "impl/F1/G3",
             str(tmp_path / "wt-g3"))
        assert materialised_group_branches(repo, ["impl/F1/G3"]) == [
            "impl/F1/G3"
        ]

    def test_an_unanswerable_probe_counts_as_materialised(self, tmp_path):
        """Over-reporting costs a group re-run; under-reporting silently skips
        a group whose only copy is gone."""
        from tianluo.engine.flow_workspace import materialised_group_branches

        not_a_repo = tmp_path / "nope"
        not_a_repo.mkdir()
        assert materialised_group_branches(not_a_repo, ["impl/F1/G1"]) == [
            "impl/F1/G1"
        ]
