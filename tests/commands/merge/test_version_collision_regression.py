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
from pathlib import Path
from unittest.mock import patch

from se3.commands.merge_cmd import run_merge
from se3.engine.merge.reconcile import read_current_version, reconcile
from se3.engine.models import FlowInstance, Step, StepStatus, StepType
from se3.engine.steps.commit import _guard_version_race
from se3.engine.version_intent import VersionIntent, is_consumed, write_intent


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
    ``se3/version-intents/<flow>.json`` intent (a changelog bullet + a bump
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
        from se3.engine.version_bumper import Version

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

    @patch("se3.engine.steps.version_analyze.version_analyze_handler")
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

    @patch("se3.engine.steps.version_analyze.version_analyze_handler")
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
