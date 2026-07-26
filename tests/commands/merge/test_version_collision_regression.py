"""End-to-end regression tests for the version-collision accident (G9).

Background (accident, 2026-07-06): two concurrent worktree sessions diverged
from the SAME baseline (11.11.2), each ``version_analyze`` computed the same
next version (11.12.0), and both baked that number into their own commit. The
first to land wrote 11.12.0 normally; the second landed a *verbatim no-op*
(the file already read 11.12.0) and VERSIONS.md deduped its changelog entry by
version number — so two distinct minor features shared 11.12.0 and one feature
lost its changelog entirely. 10.7.1 had the same double-bump earlier.

This module freezes that scenario as a permanent regression guard. It exercises
the *fixed* pipeline end-to-end through the real ``se3 merge`` adapter and the
``reconcile()`` / commit-guard libraries, asserting the accident can no longer
reproduce:

  * **Task 1 — concurrent worktree collision.** Two sessions off one baseline,
    each a minor intent, merged one after another must land on TWO DISTINCT
    versions (the later strictly greater), with BOTH changelog entries surviving
    — never a shared number with one entry swallowed.
  * **Task 2 — the supporting invariants.**
    - already-ancestor / no-op merge: reconcile still runs and advances the
      version (the old "no branch contributed a bump → skip" hole is closed);
    - reconcile resume/re-entry is idempotent (no double bump);
    - the non-worktree direct-run drift guard recomputes on a 10.7.1-style
      baseline drift so two direct runs never collide.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from tianluo.commands.merge_cmd import run_merge
from tianluo.engine.merge.reconcile import read_current_version, reconcile
from tianluo.engine.models import FlowInstance, Step, StepStatus, StepType
from tianluo.engine.steps.commit import _guard_version_race, commit_handler
from tianluo.engine.version_intent import (
    VersionIntent,
    is_consumed,
    mark_consumed,
    reconcile_commit_exists,
    write_intent,
)


PYPROJECT_TEMPLATE = """\
[project]
name = "demo"
version = "{version}"
"""

VERSIONS_TEMPLATE = """\
# Demo Version History

## {version} - 2026-07-06
- baseline entry
"""


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    )


def _make_project(tmp_path: Path, version: str = "11.11.2") -> Path:
    """A git-backed project with pyproject.toml + VERSIONS.md + README committed.

    The default baseline (11.11.2) is the exact version the two accident
    sessions diverged from, so the numbers in these tests read as the real case.
    """
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        PYPROJECT_TEMPLATE.format(version=version), encoding="utf-8"
    )
    (root / "VERSIONS.md").write_text(
        VERSIONS_TEMPLATE.format(version=version), encoding="utf-8"
    )
    (root / "README.md").write_text("# Demo\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "baseline")
    return root


def _default_branch(root: Path) -> str:
    return _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _make_feature_with_intent(
    root: Path,
    branch: str,
    flow_id: str,
    *,
    bump_type: str,
    change: str,
    base_ref: str | None = None,
) -> None:
    """Create *branch* off *base_ref* (default HEAD) carrying a committed intent.

    Mirrors a de-versioned worktree session's commit: a code change plus a
    ``tianluo/version-intents/<flow>.json`` intent (a changelog bullet + a bump
    hint, NO version number), committed on the flow branch so the merge side
    reads it from master after the merge. Passing an explicit *base_ref* is how
    the two "concurrent" sessions are forced to diverge from the SAME baseline
    even though they are created sequentially in the test.
    """
    default = _default_branch(root)
    start = base_ref or "HEAD"
    _git(root, "checkout", "-q", "-b", branch, start)
    (root / f"{flow_id}.txt").write_text(f"work from {flow_id}\n", encoding="utf-8")
    write_intent(
        root,
        VersionIntent(
            flow_id=flow_id,
            change_summary=f"{flow_id} change",
            versions_changes=[change],
            bump_type=bump_type,
            # Both sessions record the SAME pre-session baseline — the exact
            # setup that made the old design compute one colliding number.
            pre_session_baseline=read_current_version(root),
            provisional_suggested_version="11.12.0",
        ),
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", f"work + intent on {branch}")
    _git(root, "checkout", "-q", default)


def _pyproject_version(root: Path) -> str:
    return read_current_version(root)


def _versions_text(root: Path) -> str:
    return (root / "VERSIONS.md").read_text(encoding="utf-8")


class TestConcurrentWorktreeCollision:
    """Task 1: two concurrent sessions off one baseline get DISTINCT versions."""

    def test_two_concurrent_minor_sessions_do_not_share_a_version(
        self, tmp_path: Path
    ) -> None:
        """The 11.12.0 accident, re-run through the fixed pipeline.

        Two worktree sessions diverge from the same 11.11.2 baseline, each a
        minor feature. They are merged one after another (as the two real
        sessions were, 04:28 then 04:39). Because reconcile re-bases on
        master's *current* version at merge time — not the version the session
        guessed — the second merge advances past the first instead of writing a
        colliding no-op. Both changelog entries must survive.
        """
        root = _make_project(tmp_path, "11.11.2")
        baseline = _git(root, "rev-parse", "HEAD").stdout.strip()

        # Two sessions, forced to share the same divergence point.
        _make_feature_with_intent(
            root, "session-a", "flowA", bump_type="minor",
            change="feat A: first concurrent feature", base_ref=baseline,
        )
        _make_feature_with_intent(
            root, "session-b", "flowB", bump_type="minor",
            change="feat B: second concurrent feature", base_ref=baseline,
        )

        # Session A lands first.
        assert run_merge(
            ["session-a"], strategy="fast", delete_merged=False, project_root=root
        ) == 0
        version_after_a = _pyproject_version(root)
        # minor bump off 11.11.2.
        assert version_after_a == "11.12.0"

        # Session B lands second — the moment the accident struck. Its reconcile
        # re-bases on master's current 11.12.0, NOT on the stale 11.11.2
        # baseline it recorded, so it must advance rather than collide.
        assert run_merge(
            ["session-b"], strategy="fast", delete_merged=False, project_root=root
        ) == 0
        version_after_b = _pyproject_version(root)

        # The two features do NOT share a version number.
        assert version_after_b != version_after_a
        assert version_after_b == "11.13.0"

        # Monotonic, no regression: 11.11.2 < 11.12.0 < 11.13.0.
        from tianluo.engine.version_bumper import Version

        assert Version.parse(version_after_b) > Version.parse(version_after_a)
        assert Version.parse(version_after_a) > Version.parse("11.11.2")

        # BOTH changelog entries survive — neither was deduped away.
        versions = _versions_text(root)
        assert "feat A: first concurrent feature" in versions
        assert "feat B: second concurrent feature" in versions
        # Each entry is filed under its own version header.
        assert "11.12.0" in versions
        assert "11.13.0" in versions

        # Both intents consumed exactly once.
        assert is_consumed(root, "flowA")
        assert is_consumed(root, "flowB")

    def test_changelog_entries_are_filed_under_distinct_versions(
        self, tmp_path: Path
    ) -> None:
        """Each session's bullet lands under its OWN reconciled version header.

        The accident's second symptom was a changelog entry silently dropped by
        version-number dedup. Here the two bullets must appear under two
        different ``## <version>`` blocks, proving neither collapsed into the
        other's block.
        """
        root = _make_project(tmp_path, "11.11.2")
        baseline = _git(root, "rev-parse", "HEAD").stdout.strip()
        _make_feature_with_intent(
            root, "session-a", "flowA", bump_type="minor",
            change="entry-A-unique", base_ref=baseline,
        )
        _make_feature_with_intent(
            root, "session-b", "flowB", bump_type="minor",
            change="entry-B-unique", base_ref=baseline,
        )

        run_merge(["session-a"], strategy="fast", delete_merged=False, project_root=root)
        run_merge(["session-b"], strategy="fast", delete_merged=False, project_root=root)

        versions = _versions_text(root)
        # Locate the version block each bullet belongs to and assert they differ.
        block_a = _owning_version_header(versions, "entry-A-unique")
        block_b = _owning_version_header(versions, "entry-B-unique")
        assert block_a is not None and block_b is not None
        assert block_a != block_b


def _owning_version_header(versions_md: str, bullet: str) -> str | None:
    """Return the ``## <version>`` header the *bullet* line sits under.

    Walks the changelog top-down tracking the most recent version header, so a
    bullet is attributed to its enclosing release block — the granularity at
    which the accident's dedup collapse happened.
    """
    import re

    current: str | None = None
    header_re = re.compile(r"^##\s+(\S+)")
    for line in versions_md.splitlines():
        m = header_re.match(line)
        if m:
            current = m.group(1)
        elif bullet in line:
            return current
    return None


class TestNoOpMergeReconcile:
    """Task 2a: reconcile runs UNCONDITIONALLY, incl. already-ancestor merges.

    The root cause chain: impl leaf merges landed directly on master, so at
    merge-orchestrator time the branch was already an ancestor, there was no
    merge commit, and the old aggregator skipped ("no branches contributed
    bumps"). reconcile() has no such trigger predicate — it bumps from whatever
    intents are outstanding regardless of merge shape.
    """

    def test_already_ancestor_merge_still_advances_version(
        self, tmp_path: Path
    ) -> None:
        """Fast-forward the intent onto master (no merge commit), then reconcile.

        The branch is already an ancestor when ``se3 merge`` runs — the exact
        no-op merge shape that silently dropped the bump — yet the version must
        still advance because an unconsumed intent exists.
        """
        root = _make_project(tmp_path, "11.11.2")
        default = _default_branch(root)
        _make_feature_with_intent(
            root, "feature", "flowNoop", bump_type="minor", change="feat noop"
        )

        # Land the branch by FAST-FORWARD only: master now == feature, the
        # intent file is on master, and there is NO merge commit. The intent is
        # still UNCONSUMED (we did not run reconcile yet).
        _git(root, "merge", "--ff-only", "feature")
        assert _pyproject_version(root) == "11.11.2"  # ff carried no version bump
        # Sanity: feature really is an ancestor now (the no-op merge shape).
        ancestor = subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", "feature", default],
            capture_output=True,
        )
        assert ancestor.returncode == 0

        # se3 merge over the already-merged branch: integrate is a no-op, but
        # reconcile runs unconditionally and bumps.
        assert run_merge(
            ["feature"], strategy="fast", delete_merged=False, project_root=root
        ) == 0

        assert _pyproject_version(root) == "11.12.0"
        assert "feat noop" in _versions_text(root)
        assert is_consumed(root, "flowNoop")

    def test_intent_on_master_with_no_branch_still_reconciles(
        self, tmp_path: Path
    ) -> None:
        """reconcile() bumps from an intent already on master, no branch at all.

        The most degenerate no-op shape: the intent is simply present on the
        checkout (as it would be after any ff/direct landing) and nothing is
        merged. The library core must still derive and apply the version.
        """
        root = _make_project(tmp_path, "11.11.2")
        write_intent(
            root,
            VersionIntent(
                flow_id="flowBare",
                change_summary="bare intent",
                versions_changes=["feat bare"],
                bump_type="minor",
                pre_session_baseline="11.11.2",
            ),
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "land intent directly")

        result = reconcile(root)

        assert result.success
        assert result.final_version == "11.12.0"
        assert result.channel == "deterministic"
        assert _pyproject_version(root) == "11.12.0"
        assert "feat bare" in _versions_text(root)


class TestReconcileResumeIdempotent:
    """Task 2b: a re-entered reconcile never double-bumps."""

    def test_reconcile_twice_does_not_double_bump(self, tmp_path: Path) -> None:
        """Running reconcile again (a resume) re-collects only outstanding intents.

        The first reconcile consumes the intent and stamps a reconcile-commit
        trailer; the second finds nothing outstanding and is a clean no-op —
        the version must not advance a second time.
        """
        root = _make_project(tmp_path, "11.11.2")
        _make_feature_with_intent(
            root, "feature", "flowR", bump_type="minor", change="feat R"
        )
        assert run_merge(
            ["feature"], strategy="fast", delete_merged=False, project_root=root
        ) == 0
        assert _pyproject_version(root) == "11.12.0"

        # Resume: re-enter reconcile directly. Idempotency is backed by both the
        # on-disk consumed flag and the git-durable reconcile-commit trailer.
        second = reconcile(root)

        assert second.success
        assert second.already_reconciled
        assert second.channel == "noop"
        # No double bump.
        assert _pyproject_version(root) == "11.12.0"

    def test_reconcile_idempotent_even_if_consumed_flag_lost(
        self, tmp_path: Path
    ) -> None:
        """The reconcile-commit trailer alone stops a re-bump if the flag is lost.

        Simulates the crash window where the reconcile commit landed but the
        on-disk ``consumed`` flag was never persisted: we re-write the intent
        with ``consumed=False`` and re-run reconcile. The git-durable trailer
        must still short-circuit the second bump.
        """
        root = _make_project(tmp_path, "11.11.2")
        _make_feature_with_intent(
            root, "feature", "flowT", bump_type="minor", change="feat T"
        )
        assert run_merge(
            ["feature"], strategy="fast", delete_merged=False, project_root=root
        ) == 0
        assert _pyproject_version(root) == "11.12.0"

        # Clear the on-disk consumed flag (crash-window simulation) but leave the
        # reconcile commit (and its trailer) in history.
        write_intent(
            root,
            VersionIntent(
                flow_id="flowT",
                change_summary="flowT change",
                versions_changes=["feat T"],
                bump_type="minor",
                pre_session_baseline="11.11.2",
                consumed=False,
            ),
        )

        second = reconcile(root)

        assert second.success
        assert second.already_reconciled
        assert _pyproject_version(root) == "11.12.0"


class TestReconcileCommitFailureRecovery:
    """Task 2b': a reconcile whose `git commit` fails must stay recoverable.

    The failure class the redesign exists to prevent: the version file +
    changelog + consumed-flag are written, then the commit dies (index.lock
    contention, a failing hook). If the on-disk consumed flag were trusted as
    proof of completion, the resumed reconcile would no-op and the bump would
    linger as uncommitted dirt — a feature with no committed version/changelog.
    """

    def test_failed_commit_rolls_back_and_resume_lands_version(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A failed commit leaves NO half-applied bump; the resume lands it.

        The first reconcile fails at the commit step. It must roll back its
        uncommitted version/changelog/consumed writes so the tree is clean and
        the intent is un-consumed. The resumed reconcile then recomputes from the
        committed base and lands the version — committed, not as dirt.
        """
        root = _make_project(tmp_path, "11.11.2")
        _make_feature_with_intent(
            root, "feature", "flowC", bump_type="minor", change="feat C"
        )
        _git(root, "merge", "--ff-only", "feature")  # intent on master, unconsumed

        reconcile_mod = sys.modules["tianluo.engine.merge.reconcile"]
        real_commit = reconcile_mod._commit_reconcile
        calls = {"n": 0}

        def flaky_commit(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise reconcile_mod.ReconcileError("simulated index.lock contention")
            return real_commit(*args, **kwargs)

        monkeypatch.setattr(reconcile_mod, "_commit_reconcile", flaky_commit)

        # First attempt: commit fails.
        with pytest.raises(reconcile_mod.ReconcileError):
            reconcile(root)

        # Rollback: no half-applied bump on disk, intent NOT consumed, tree clean.
        assert read_current_version(root) == "11.11.2"
        assert not is_consumed(root, "flowC")
        assert _git(root, "status", "--porcelain").stdout.strip() == ""

        # Resume: the second attempt commits and lands the version.
        result = reconcile(root)
        assert result.success
        assert not result.already_reconciled
        assert read_current_version(root) == "11.12.0"
        assert "feat C" in _versions_text(root)
        assert is_consumed(root, "flowC")
        assert reconcile_commit_exists(root, "flowC")
        # Committed — nothing left dangling in the working tree.
        assert _git(root, "status", "--porcelain").stdout.strip() == ""

    def test_resume_recovers_consumed_but_uncommitted_intent(
        self, tmp_path: Path
    ) -> None:
        """A crash after the writes but before the commit is recovered on resume.

        Simulates the hard-crash window (no graceful rollback ran): the version
        file, changelog, and consumed flag are on disk but there is NO reconcile
        commit. A resumed reconcile must NOT treat that uncommitted consumed flag
        as proof of completion — it restores to HEAD, recomputes, and commits.
        """
        from tianluo.engine.merge.reconcile import (
            _merge_changelog,
            _write_final_version,
        )

        root = _make_project(tmp_path, "11.11.2")
        _make_feature_with_intent(
            root, "feature", "flowD", bump_type="minor", change="feat D"
        )
        _git(root, "merge", "--ff-only", "feature")  # intent on master, unconsumed

        # Simulate the crash residue: writes applied + flag set, but no commit.
        _write_final_version(root, "11.12.0")
        _merge_changelog(root, "11.12.0", ["feat D"])
        mark_consumed(root, "flowD")
        assert read_current_version(root) == "11.12.0"  # dirty, uncommitted
        assert not reconcile_commit_exists(root, "flowD")

        # Resume must recover rather than no-op on the uncommitted consumed flag.
        result = reconcile(root)

        assert result.success
        assert not result.already_reconciled
        assert result.final_version == "11.12.0"
        assert read_current_version(root) == "11.12.0"
        assert "feat D" in _versions_text(root)
        assert is_consumed(root, "flowD")
        assert reconcile_commit_exists(root, "flowD")
        assert _git(root, "status", "--porcelain").stdout.strip() == ""

    def test_resume_recovers_dirty_version_file_without_consumed_flag(
        self, tmp_path: Path
    ) -> None:
        """A crash after the version write but BEFORE any consumed flag recovers.

        The narrowest hard-crash window: ``_write_final_version`` landed a dirty
        11.12.0 on disk but the process died before the first ``mark_consumed``,
        so there is NO flag to signal residue. A resume must still read its base
        from master's COMMITTED 11.11.2 (not the dirty 11.12.0) and reconcile to
        11.12.0 — NOT double-bump to 11.13.0 and strand 11.12.0 as a ghost.
        """
        from tianluo.engine.merge.reconcile import _write_final_version

        root = _make_project(tmp_path, "11.11.2")
        _make_feature_with_intent(
            root, "feature", "flowE", bump_type="minor", change="feat E"
        )
        _git(root, "merge", "--ff-only", "feature")  # intent on master, unconsumed

        # Crash residue: version file dirty, NO changelog write, NO consumed flag.
        _write_final_version(root, "11.12.0")
        assert read_current_version(root) == "11.12.0"  # dirty, uncommitted
        assert not is_consumed(root, "flowE")

        result = reconcile(root)

        assert result.success
        # Base was read from committed 11.11.2, so a single minor bump → 11.12.0.
        assert result.final_version == "11.12.0"
        assert read_current_version(root) == "11.12.0"
        assert reconcile_commit_exists(root, "flowE")
        assert _git(root, "status", "--porcelain").stdout.strip() == ""


class TestNonWorktreeDriftGuard:
    """Task 2c: the direct-run commit drift guard (change D) prevents 10.7.1."""

    def _make_flow_with_version_analyze(
        self, tmpdir: Path, *, va_current: str, baseline: str, suggested: str
    ) -> FlowInstance:
        """A non-worktree flow carrying a completed version_analyze step."""
        flow = FlowInstance(task_description="Add feature")
        flow.task_type = "fix"
        flow.is_worktree_mode = False

        va = Step(step_type=StepType.VERSION_ANALYZE)
        va.status = StepStatus.COMPLETED
        va.outputs["current_version"] = va_current
        va.outputs["suggested_version"] = suggested
        va.inputs["pre_session_version"] = baseline
        flow.state.add_step(va)
        return flow

    def _commit_step(self, *, suggested: str, baseline: str) -> Step:
        step = Step(step_type=StepType.COMMIT)
        step.inputs["suggested_version"] = suggested
        step.inputs["pre_session_version"] = baseline
        step.inputs["bump_type"] = "patch"
        return step

    @patch("tianluo.engine.steps.version_analyze.version_analyze_handler")
    def test_drift_recomputes_to_avoid_collision(
        self, mock_reanalyze, tmp_path: Path
    ) -> None:
        """Disk drifted 10.7.0 -> 10.7.1 (a concurrent direct run bumped first).

        The in-lock disk version no longer equals the pre-session baseline the
        guard recorded, so the stale suggested 10.7.1 would collide. The guard
        re-runs version_analyze against the drifted baseline and returns its
        fresh, non-colliding number instead.
        """
        def fake_reanalyze(va_step, flow):
            # Re-analysis against the drifted 10.7.1 baseline yields the next patch.
            va_step.outputs["suggested_version"] = "10.7.2"
            va_step.outputs["bump_type"] = "patch"
            va_step.outputs["versions_changes"] = ["recomputed entry"]
            return StepStatus.COMPLETED

        mock_reanalyze.side_effect = fake_reanalyze

        flow = self._make_flow_with_version_analyze(
            tmp_path, va_current="10.7.0", baseline="10.7.0", suggested="10.7.1"
        )
        step = self._commit_step(suggested="10.7.1", baseline="10.7.0")

        # disk_version=10.7.1 is what the concurrent flow already wrote.
        result = _guard_version_race(
            step, flow, disk_version="10.7.1", target_version="10.7.1"
        )

        assert mock_reanalyze.called
        # Re-analysis was pointed at the drifted disk version as its new baseline.
        new_baseline = mock_reanalyze.call_args.args[0].inputs["pre_session_version"]
        assert new_baseline == "10.7.1"
        # The recomputed, non-colliding version is returned — never the stale
        # 10.7.1 that would double-bump onto the concurrent flow's number.
        assert result == "10.7.2"
        assert result != "10.7.1"
        # The refreshed changelog artifact is forwarded so the commit matches.
        assert step.inputs["suggested_version"] == "10.7.2"

    @patch("tianluo.engine.steps.version_analyze.version_analyze_handler")
    def test_no_drift_passes_target_through_unchanged(
        self, mock_reanalyze, tmp_path: Path
    ) -> None:
        """No concurrent bump (disk == baseline): the guard is a pure pass-through.

        The baseline-consistent path must not re-run version_analyze and must
        return the resolved target verbatim, so the common case is untouched.
        """
        flow = self._make_flow_with_version_analyze(
            tmp_path, va_current="10.7.0", baseline="10.7.0", suggested="10.7.1"
        )
        step = self._commit_step(suggested="10.7.1", baseline="10.7.0")

        result = _guard_version_race(
            step, flow, disk_version="10.7.0", target_version="10.7.1"
        )

        assert not mock_reanalyze.called
        assert result == "10.7.1"

    @patch("tianluo.engine.steps.version_analyze.version_analyze_handler")
    def test_drift_reanalysis_returning_disk_version_halts(
        self, mock_reanalyze, tmp_path: Path
    ) -> None:
        """Re-analysis that STILL returns the drifted disk version must HALT.

        The refuse-on-collision branch: disk drifted 10.7.0 -> 10.7.1 (a concurrent
        flow released 10.7.1), and re-running version_analyze against the drifted
        baseline nonetheless returns 10.7.1 again. Writing it would file THIS flow's
        changelog under the exact number the concurrent flow just released — the
        10.7.1-type shared-version accident this guard exists to block. The guard
        must raise rather than return the colliding number.
        """
        def fake_reanalyze(va_step, flow):
            # Re-analysis returns the SAME drifted disk version → still a collision.
            va_step.outputs["suggested_version"] = "10.7.1"
            va_step.outputs["bump_type"] = "patch"
            va_step.outputs["versions_changes"] = ["recomputed entry"]
            return StepStatus.COMPLETED

        mock_reanalyze.side_effect = fake_reanalyze

        flow = self._make_flow_with_version_analyze(
            tmp_path, va_current="10.7.0", baseline="10.7.0", suggested="10.7.1"
        )
        step = self._commit_step(suggested="10.7.1", baseline="10.7.0")

        with pytest.raises(RuntimeError) as excinfo:
            _guard_version_race(
                step, flow, disk_version="10.7.1", target_version="10.7.1"
            )

        assert mock_reanalyze.called
        # The refusal names the colliding version and refuses to write it.
        msg = str(excinfo.value)
        assert "10.7.1" in msg
        assert "colliding" in msg.lower()


class TestFlowWroteVersionBlobDetection:
    """Issue 4: own-replay detection must NOT depend on the optional Version: line.

    With ``version.include_in_commit_message: false`` the flow's own commit never
    carries a ``Version: <v>`` line, so the old grep-for-Version probe would
    misclassify a healthy resume/replay as concurrent drift (double-bump or a
    spurious collision failure). The probe must instead confirm the version-file
    blob recorded at the flow's own ``Flow:``-trailer commit.
    """

    @staticmethod
    def _git(root: Path, *args: str):
        return subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            check=True,
        )

    def test_own_replay_detected_without_version_commit_line(
        self, tmp_path: Path
    ) -> None:
        from tianluo.engine.steps.commit import _flow_wrote_version
        from tianluo.engine.version_bumper import VersionBumper, VersionConfig

        root = tmp_path
        (root / "pyproject.toml").write_text(
            '[project]\nname = "demo"\nversion = "5.2.0"\n', encoding="utf-8"
        )
        self._git(root, "init", "-q")
        self._git(root, "config", "user.email", "t@example.com")
        self._git(root, "config", "user.name", "Test")
        self._git(root, "add", "-A")
        # This flow's commit carries its Flow: trailer but NO "Version:" line, as
        # under include_in_commit_message=false.
        self._git(
            root, "commit", "-q", "-m", "impl: feature\n\nFlow: flow-xyz"
        )

        bumper = VersionBumper(VersionConfig())
        vf = bumper.detect_version_file(root)
        assert vf is not None

        # disk 5.2.0 == the blob this flow's own commit recorded -> own replay,
        # detected purely from the Flow: trailer + version-file blob.
        assert _flow_wrote_version(root, "flow-xyz", "5.2.0", vf, bumper) is True
        # a version this flow never wrote is not a match
        assert _flow_wrote_version(root, "flow-xyz", "9.9.9", vf, bumper) is False
        # another flow's id must not match our commit
        assert _flow_wrote_version(root, "other-flow", "5.2.0", vf, bumper) is False


class TestNonWorktreeCommitHandlerDriftGuard:
    """Change D through the REAL ``commit_handler`` wiring (not just the helper).

    The ``TestNonWorktreeDriftGuard`` cases above hand-feed ``disk_version`` to
    ``_guard_version_race`` directly, so a break in ``commit_handler``'s in-lock
    disk re-read (``VersionBumper.read_version``) or its call into the guard —
    the plumbing that actually connects the two — would slip through unnoticed.
    This exercises the full path against a real git repo: a real version file is
    re-read at commit time, drift is detected, and the recomputed version is what
    lands on disk. The baseline the guard compares against is version_analyze's
    OBSERVED ``current_version`` (10.7.0), NOT the audit-only pre_session_version.
    """

    def _make_direct_flow(self, root: Path, *, va_current: str, suggested: str):
        flow = FlowInstance(
            flow_id="direct-flow",
            task_description="Fix a bug",
            task_type="fix",
            change_path=root / "tianluo.yaml",
        )
        flow.is_worktree_mode = False

        va = Step(step_type=StepType.VERSION_ANALYZE, status=StepStatus.COMPLETED)
        va.outputs["current_version"] = va_current
        va.outputs["suggested_version"] = suggested
        va.outputs["bump_type"] = "patch"
        flow.state.add_step(va)

        commit = Step(step_type=StepType.COMMIT, status=StepStatus.PENDING)
        commit.inputs["suggested_version"] = suggested
        commit.inputs["bump_type"] = "patch"
        flow.state.add_step(commit)
        return flow, commit

    @patch("tianluo.engine.context_builder.ensure_code_index_fresh")
    @patch("tianluo.engine.steps.version_analyze.version_analyze_handler")
    def test_commit_handler_recomputes_on_concurrent_disk_drift(
        self, mock_reanalyze, _mock_index, tmp_path: Path
    ) -> None:
        """A concurrent direct run published 10.7.1 first; the commit recomputes.

        version_analyze observed disk 10.7.0 and suggested the next patch 10.7.1.
        Before this flow's commit, a concurrent direct run bumped pyproject.toml
        to 10.7.1 and committed. commit_handler must re-read the drifted disk
        version in-lock, hand it to the guard, and — because 10.7.1 != the
        observed 10.7.0 — recompute past it (to 10.7.2) and land THAT, never a
        second 10.7.1.
        """
        root = _make_project(tmp_path, "10.7.0")
        flow, commit_step = self._make_direct_flow(
            root, va_current="10.7.0", suggested="10.7.1"
        )

        # Concurrent flow grabbed the lock first and released 10.7.1.
        (root / "pyproject.toml").write_text(
            PYPROJECT_TEMPLATE.format(version="10.7.1"), encoding="utf-8"
        )
        _git(root, "commit", "-aqm", "concurrent flow: bump to 10.7.1")

        # This flow has its own real code change to commit.
        (root / "feature.py").write_text("x = 1\n", encoding="utf-8")

        def fake_reanalyze(va_step, fl):
            # The guard must re-point version_analyze at the DRIFTED disk version.
            assert va_step.inputs["pre_session_version"] == "10.7.1"
            va_step.outputs["suggested_version"] = "10.7.2"
            va_step.outputs["bump_type"] = "patch"
            va_step.outputs["versions_changes"] = ["recomputed entry"]
            return StepStatus.COMPLETED

        mock_reanalyze.side_effect = fake_reanalyze

        result = commit_handler(commit_step, flow)

        assert result == StepStatus.COMPLETED, commit_step.error_message
        assert mock_reanalyze.called
        # The recomputed, non-colliding version is what landed on disk — the
        # in-lock re-read (10.7.1) drove the recompute, not the stale suggestion.
        assert read_current_version(root) == "10.7.2"

    @patch("tianluo.engine.context_builder.ensure_code_index_fresh")
    @patch("tianluo.engine.steps.version_analyze.version_analyze_handler")
    def test_commit_handler_no_drift_writes_resolved_target(
        self, mock_reanalyze, _mock_index, tmp_path: Path
    ) -> None:
        """No concurrent bump: the resolved target is written, no re-analysis.

        Disk still matches version_analyze's observed current_version, so the
        guard is a pass-through — commit_handler writes the suggested 10.7.1 and
        never re-runs version_analyze.
        """
        root = _make_project(tmp_path, "10.7.0")
        flow, commit_step = self._make_direct_flow(
            root, va_current="10.7.0", suggested="10.7.1"
        )

        (root / "feature.py").write_text("y = 2\n", encoding="utf-8")

        result = commit_handler(commit_step, flow)

        assert result == StepStatus.COMPLETED, commit_step.error_message
        assert not mock_reanalyze.called
        assert read_current_version(root) == "10.7.1"

    @patch("tianluo.engine.context_builder.ensure_code_index_fresh")
    @patch("tianluo.engine.steps.version_analyze.version_analyze_handler")
    def test_commit_handler_own_session_advance_is_not_drift(
        self, mock_reanalyze, _mock_index, tmp_path: Path
    ) -> None:
        """The guard's operand is version_analyze's OBSERVED current_version, NOT
        the pre_session_version — proven through the REAL commit_handler wiring.

        This flow's own session commits advanced pyproject.toml to 10.7.5 and
        committed them (with NO Flow: trailer for this flow, so the git-durable
        own-replay probe cannot help). version_analyze then observed 10.7.5 and
        suggested 10.8.0, while the pre-session baseline lags at 10.7.0. Disk ==
        observed current_version, so this is NOT drift: no re-analysis, 10.8.0
        lands verbatim.

        Discriminates the operand: current_version (10.7.5) != pre_session
        (10.7.0), and every own-replay escape hatch is genuine/unstubbed. A wrong
        implementation comparing disk against pre_session_version (10.7.0) would
        see a spurious 10.7.5 != 10.7.0 drift and re-run version_analyze.
        """
        root = _make_project(tmp_path, "10.7.0")

        # This flow's own session commits advanced the version file and committed
        # them — a NON-Flow-trailer commit, so _flow_wrote_version cannot claim it.
        (root / "pyproject.toml").write_text(
            PYPROJECT_TEMPLATE.format(version="10.7.5"), encoding="utf-8"
        )
        _git(root, "commit", "-aqm", "impl: own session work advanced version")

        flow, commit_step = self._make_direct_flow(
            root, va_current="10.7.5", suggested="10.8.0"
        )
        # The audit-only pre-session baseline lags the observed current_version.
        commit_step.inputs["pre_session_version"] = "10.7.0"

        (root / "feature.py").write_text("z = 3\n", encoding="utf-8")

        result = commit_handler(commit_step, flow)

        assert result == StepStatus.COMPLETED, commit_step.error_message
        # Disk == observed current_version -> pass-through, no recompute.
        assert not mock_reanalyze.called
        assert read_current_version(root) == "10.8.0"

    @patch("tianluo.engine.context_builder.ensure_code_index_fresh")
    @patch("tianluo.engine.steps.version_analyze.version_analyze_handler")
    def test_commit_handler_halts_when_reanalysis_still_collides(
        self, mock_reanalyze, _mock_index, tmp_path: Path
    ) -> None:
        """commit_handler HALTS (never writes) when the recompute still collides.

        A concurrent direct run published 10.7.1. This flow's version_analyze had
        observed 10.7.0 and suggested 10.7.1; in-lock the guard re-runs
        version_analyze against the drifted 10.7.1 baseline but it STILL returns
        10.7.1. commit_handler must FAIL rather than commit this flow's changelog
        under the concurrent flow's released number — the exact 10.7.1 shared-
        version accident. The version file is left at the concurrent flow's 10.7.1
        with NO changelog entry appended for this flow.
        """
        root = _make_project(tmp_path, "10.7.0")
        flow, commit_step = self._make_direct_flow(
            root, va_current="10.7.0", suggested="10.7.1"
        )

        # Concurrent flow grabbed the lock first and released 10.7.1.
        (root / "pyproject.toml").write_text(
            PYPROJECT_TEMPLATE.format(version="10.7.1"), encoding="utf-8"
        )
        _git(root, "commit", "-aqm", "concurrent flow: bump to 10.7.1")
        head_before = _git(root, "rev-parse", "HEAD").stdout.strip()

        # This flow has its own real code change to commit.
        (root / "feature.py").write_text("x = 1\n", encoding="utf-8")

        def fake_reanalyze(va_step, fl):
            # Recompute against the drifted baseline STILL yields the collision.
            assert va_step.inputs["pre_session_version"] == "10.7.1"
            va_step.outputs["suggested_version"] = "10.7.1"
            va_step.outputs["bump_type"] = "patch"
            va_step.outputs["versions_changes"] = ["recomputed entry"]
            return StepStatus.COMPLETED

        mock_reanalyze.side_effect = fake_reanalyze

        result = commit_handler(commit_step, flow)

        assert result == StepStatus.FAILED
        assert mock_reanalyze.called
        # The colliding version was never re-written by this flow and no commit
        # landed — the version file still reads the concurrent flow's 10.7.1.
        assert read_current_version(root) == "10.7.1"
        assert _git(root, "rev-parse", "HEAD").stdout.strip() == head_before
        assert "colliding" in (commit_step.error_message or "").lower()
