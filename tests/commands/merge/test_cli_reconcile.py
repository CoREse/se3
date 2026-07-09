"""Tests for the ``se3 merge`` CLI thin-adapter path (G6).

The ``se3 merge`` command runs ``integrate() -> reconcile()`` back-to-back
(change B/C): the branch merges land first, then the merge-side version
reconcile derives and writes the final version from the merged-in session
intents against master's current version. This module covers the CLI adapter's
observable contract:

  * a clean integrate followed by a reconcile that lands the final version;
  * a plain branch merge with no session intents reconciles to a no-op (version
    unchanged) and still succeeds;
  * rerunning the whole command is idempotent — no double bump;
  * a reconcile fault (regression / collision / write failure) is surfaced as a
    non-zero exit code, never a silent success;
  * no confirmation gate — the CLI wrapper drives the library in
    ``suppress_human_call`` mode (no ``se3/calls/`` files) and expresses failure
    only through the exit code.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from se3.commands.merge_cmd import run_merge
from se3.engine.merge.reconcile import (
    VersionRegressionError,
    read_current_version,
)
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


def _make_project(tmp_path: Path, version: str = "1.2.3") -> Path:
    """A git-backed project with pyproject.toml + VERSIONS.md + README committed."""
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
    root: Path, branch: str, flow_id: str, *, bump_type: str, change: str
) -> None:
    """Create *branch* off HEAD carrying a committed version-intent.

    Mirrors what a de-versioned worktree session's commit produces: a code
    change plus a ``se3/version-intents/<flow>.json`` intent file (a changelog
    bullet + a bump hint), committed on the flow branch so the merge side reads
    it from master after the merge.
    """
    default = _default_branch(root)
    _git(root, "checkout", "-q", "-b", branch)
    (root / f"{flow_id}.txt").write_text(f"work from {flow_id}\n", encoding="utf-8")
    write_intent(
        root,
        VersionIntent(
            flow_id=flow_id,
            change_summary=f"{flow_id} change",
            versions_changes=[change],
            bump_type=bump_type,
            pre_session_baseline=read_current_version(root),
            provisional_suggested_version="9.9.9",
        ),
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", f"work + intent on {branch}")
    _git(root, "checkout", "-q", default)


def _pyproject_version(root: Path) -> str:
    return read_current_version(root)


def _tag_subject(root: Path, tag_name: str) -> str:
    return _git(
        root,
        "for-each-ref",
        f"refs/tags/{tag_name}",
        "--format=%(contents:subject)",
    ).stdout.strip()


class TestIntegrateThenReconcile:
    """The CLI runs integrate and reconcile back-to-back and lands the version."""

    def test_lands_reconciled_version_deterministic(self, tmp_path: Path) -> None:
        root = _make_project(tmp_path, "1.2.3")
        _make_feature_with_intent(
            root, "feature", "flowA", bump_type="minor", change="feat A"
        )

        exit_code = run_merge(
            ["feature"], strategy="fast", delete_merged=False, project_root=root
        )

        assert exit_code == 0
        # Deterministic SemVer channel: max(minor) applied to 1.2.3 -> 1.3.0.
        assert _pyproject_version(root) == "1.3.0"
        # The changelog bullet is filed under the reconciled version.
        versions = (root / "VERSIONS.md").read_text(encoding="utf-8")
        assert "1.3.0" in versions
        assert "feat A" in versions
        # Intent consumed → idempotency marker set.
        assert is_consumed(root, "flowA")
        # A reconcile commit carrying the session trailer exists on HEAD.
        log = _git(root, "log", "-1", "--format=%B").stdout
        assert "Version-Reconcile-Session: flowA" in log

    def test_two_intents_take_max_bump(self, tmp_path: Path) -> None:
        """Two merged features do NOT collide on one version — both bullets land.

        This is the exact accident the redesign prevents: two minor features
        must aggregate to a single reconciled version whose changelog carries
        both entries, not silently share a number and drop one. The same
        aggregate release owns exactly one annotated tag, whose message comes
        from the final reconcile commit rather than from either session.
        """
        root = _make_project(tmp_path, "1.2.3")
        _make_feature_with_intent(
            root, "feat-x", "flowX", bump_type="patch", change="fix X"
        )
        _make_feature_with_intent(
            root, "feat-y", "flowY", bump_type="minor", change="feat Y"
        )

        exit_code = run_merge(
            ["feat-x", "feat-y"],
            strategy="fast",
            delete_merged=False,
            project_root=root,
        )

        assert exit_code == 0
        # max(patch, minor) = minor: 1.2.3 -> 1.3.0.
        assert _pyproject_version(root) == "1.3.0"
        versions = (root / "VERSIONS.md").read_text(encoding="utf-8")
        assert "fix X" in versions
        assert "feat Y" in versions
        assert is_consumed(root, "flowX")
        assert is_consumed(root, "flowY")
        assert _git(root, "tag", "--list", "v*").stdout.splitlines() == ["v1.3.0"]

        reconcile_commit = _git(root, "rev-parse", "HEAD").stdout.strip()
        tag_commit = _git(root, "rev-parse", "v1.3.0^{}").stdout.strip()
        assert tag_commit == reconcile_commit
        assert _git(root, "cat-file", "-t", "v1.3.0").stdout.strip() == "tag"

        reconcile_subject = _git(
            root, "log", "-1", "--format=%s", reconcile_commit
        ).stdout.strip()
        assert _tag_subject(root, "v1.3.0") == reconcile_subject
        assert _tag_subject(root, "v1.3.0") not in {
            "fix X",
            "feat Y",
            "work + intent on feat-x",
            "work + intent on feat-y",
        }

    def test_no_intents_is_noop_success(self, tmp_path: Path) -> None:
        """A plain branch merge with no session intents reconciles to a no-op."""
        root = _make_project(tmp_path, "4.5.6")
        default = _default_branch(root)
        _git(root, "checkout", "-q", "-b", "plain")
        (root / "plain.txt").write_text("no intent here\n", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "plain work")
        _git(root, "checkout", "-q", default)

        exit_code = run_merge(
            ["plain"], strategy="fast", delete_merged=False, project_root=root
        )

        assert exit_code == 0
        # No intents → no bump; version untouched.
        assert _pyproject_version(root) == "4.5.6"


class TestRerunIdempotency:
    """Rerunning the whole command never double-bumps."""

    def test_rerun_does_not_double_bump(self, tmp_path: Path) -> None:
        root = _make_project(tmp_path, "1.0.0")
        _make_feature_with_intent(
            root, "feature", "flowA", bump_type="minor", change="feat A"
        )

        first = run_merge(
            ["feature"], strategy="fast", delete_merged=False, project_root=root
        )
        assert first == 0
        assert _pyproject_version(root) == "1.1.0"

        # Second run of the identical command: integrate is now a no-op (feature
        # is already an ancestor) and reconcile re-collects only outstanding
        # intents — flowA is consumed, so nothing bumps again.
        second = run_merge(
            ["feature"], strategy="fast", delete_merged=False, project_root=root
        )
        assert second == 0
        assert _pyproject_version(root) == "1.1.0"


class TestCliReconcileScoping:
    """The CLI reconcile only consumes intents carried by the merged branches."""

    def test_does_not_consume_unrelated_in_flight_flow_intent(
        self, tmp_path: Path
    ) -> None:
        """A concurrent flow's outstanding intent on master is NOT swept up.

        Regression guard: the CLI used to call ``reconcile`` with no scope, so it
        consumed EVERY outstanding intent on master — including one left by a
        concurrent worktree flow that finished ``merge_integrate`` but has not yet
        run its own ``version_reconcile`` step. That committed the other flow's
        version decision outside its step lifecycle and bypassed its confirmation
        gate. The reconcile must be scoped to the intents the merged branches
        carry, leaving the unrelated flow's intent outstanding for its own step.
        """
        from se3.engine.version_intent import (
            is_consumed,
            reconcile_commit_exists,
        )

        root = _make_project(tmp_path, "1.2.3")
        default = _default_branch(root)

        # branch-B diverges in parallel FIRST, carrying only its own flowB intent
        # (it was created before flowA ever landed on master).
        _make_feature_with_intent(
            root, "branch-B", "flowB", bump_type="minor", change="feat B"
        )

        # A concurrent flow A finished merge_integrate — its intent now sits on
        # master, still unconsumed and awaiting flow A's own version_reconcile.
        write_intent(
            root,
            VersionIntent(
                flow_id="flowA",
                change_summary="a",
                versions_changes=["feat A from concurrent flow"],
                bump_type="major",
            ),
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "flow A intent landed on master")

        exit_code = run_merge(
            ["branch-B"], strategy="fast", delete_merged=False, project_root=root
        )

        assert exit_code == 0
        # Only branch-B's intent was reconciled (minor 1.2.3 -> 1.3.0), NOT flow
        # A's major bump — flow A's decision stays with flow A's own step.
        assert _pyproject_version(root) == "1.3.0"
        assert is_consumed(root, "flowB")
        assert reconcile_commit_exists(root, "flowB")
        # flow A's intent is untouched: not consumed, no reconcile commit — its own
        # version_reconcile step (and confirmation gate) still owns the decision.
        assert not is_consumed(root, "flowA")
        assert not reconcile_commit_exists(root, "flowA")
        versions = (root / "VERSIONS.md").read_text(encoding="utf-8")
        assert "feat A from concurrent flow" not in versions

    def test_does_not_consume_intent_inherited_from_master(
        self, tmp_path: Path
    ) -> None:
        """An intent the merged branch INHERITED from master is NOT swept up.

        Regression guard (self-check): scoping by the raw branch tree consumed
        every intent present in the branch, including ones the branch merely
        inherited. If Flow A finished ``merge_integrate`` and left its intent
        outstanding on master, a branch B *cut from that master* carries a
        verbatim copy of ``se3/version-intents/flowA.json``. Merging B must
        reconcile ONLY B's own introduced intent — subtracting master's
        pre-merge tip keeps A's inherited intent out of scope so A's decision
        stays with A's own ``version_reconcile`` step / confirmation gate.
        """
        from se3.engine.version_intent import (
            is_consumed,
            reconcile_commit_exists,
        )

        root = _make_project(tmp_path, "1.2.3")

        # Flow A finished merge_integrate: its intent sits on master, unconsumed,
        # awaiting flow A's own version_reconcile.
        write_intent(
            root,
            VersionIntent(
                flow_id="flowA",
                change_summary="a",
                versions_changes=["feat A from concurrent flow"],
                bump_type="major",
            ),
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "flow A intent landed on master")

        # branch-B is cut from THAT master, so it INHERITS flowA.json in its tree
        # AND adds its own flowB intent on top.
        _make_feature_with_intent(
            root, "branch-B", "flowB", bump_type="minor", change="feat B"
        )

        exit_code = run_merge(
            ["branch-B"], strategy="fast", delete_merged=False, project_root=root
        )

        assert exit_code == 0
        # Only branch-B's introduced intent was reconciled (minor 1.2.3 -> 1.3.0),
        # NOT the inherited flowA major bump.
        assert _pyproject_version(root) == "1.3.0"
        assert is_consumed(root, "flowB")
        assert reconcile_commit_exists(root, "flowB")
        # flow A's inherited intent is untouched — its own step still owns it.
        assert not is_consumed(root, "flowA")
        assert not reconcile_commit_exists(root, "flowA")
        versions = (root / "VERSIONS.md").read_text(encoding="utf-8")
        assert "feat A from concurrent flow" not in versions

    def test_rerun_after_fault_does_not_consume_inherited_intent(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A rerun after a reconcile fault still excludes the inherited intent.

        Regression guard (self-check): once ``branch-B`` is integrated, it is an
        ancestor of master, so ``intent_flow_ids_introduced`` took the
        rerun-recovery carve-out and returned B's *full* tree — including flowA,
        the intent B only inherited from master (Flow A finished merge_integrate
        and is still awaiting its own version_reconcile). The rerun then consumed
        flowA together with flowB, bumping flowA's version outside its own step's
        confirmation/resume boundary. The carve-out must reconstruct B's real
        fork point from the ``--no-ff`` integration merge and subtract the
        inherited intent, so a rerun reconciles ONLY B's own bump.
        """
        import sys

        from se3.engine.version_intent import (
            is_consumed,
            reconcile_commit_exists,
        )

        root = _make_project(tmp_path, "1.2.3")

        # Flow A finished merge_integrate: its intent sits on master, unconsumed,
        # awaiting flow A's own version_reconcile.
        write_intent(
            root,
            VersionIntent(
                flow_id="flowA",
                change_summary="a",
                versions_changes=["feat A from concurrent flow"],
                bump_type="major",
            ),
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "flow A intent landed on master")

        # branch-B is cut from THAT master, inheriting flowA and adding flowB.
        _make_feature_with_intent(
            root, "branch-B", "flowB", bump_type="minor", change="feat B"
        )

        # First run: integrate lands, but force the reconcile half to fault so
        # branch-B is left integrated (ancestor of master) with BOTH intents
        # still outstanding — the exact rerun-recovery state.
        reconcile_mod = sys.modules["se3.engine.merge.reconcile"]
        real_reconcile = reconcile_mod.reconcile

        def _boom(*_args, **_kwargs):
            raise VersionRegressionError("computed final would regress")

        monkeypatch.setattr(reconcile_mod, "reconcile", _boom)
        first = run_merge(
            ["branch-B"], strategy="fast", delete_merged=False, project_root=root
        )
        assert first == 1
        assert _pyproject_version(root) == "1.2.3"
        # branch-B is now an ancestor of master (integrate landed with --no-ff).
        assert (
            _git(root, "merge-base", "--is-ancestor", "branch-B", "HEAD").returncode
            == 0
        )

        # Rerun the whole command with reconcile healthy: it must land ONLY B's
        # minor bump (1.2.3 -> 1.3.0), never flowA's inherited major.
        monkeypatch.setattr(reconcile_mod, "reconcile", real_reconcile)
        second = run_merge(
            ["branch-B"], strategy="fast", delete_merged=False, project_root=root
        )

        assert second == 0
        assert _pyproject_version(root) == "1.3.0"
        assert is_consumed(root, "flowB")
        assert reconcile_commit_exists(root, "flowB")
        # flow A's inherited intent stays untouched for its own step.
        assert not is_consumed(root, "flowA")
        assert not reconcile_commit_exists(root, "flowA")
        versions = (root / "VERSIONS.md").read_text(encoding="utf-8")
        assert "feat A from concurrent flow" not in versions


class TestReconcileFailureExitCode:
    """A version-decision fault is surfaced as a non-zero exit, not a silent OK."""

    def test_regression_returns_nonzero(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        root = _make_project(tmp_path, "2.0.0")
        _make_feature_with_intent(
            root, "feature", "flowA", bump_type="minor", change="feat A"
        )

        # Force the reconcile half to fault after a clean integrate. The branch
        # merges land, but the version decision is unsettled — the CLI must
        # report failure rather than a clean success.
        def _boom(*_args, **_kwargs):
            raise VersionRegressionError("computed final would regress")

        # run_merge imports ``reconcile`` from this submodule at call time, so
        # patch the submodule's attribute. The ``merge`` package re-exports the
        # function under the same name (shadowing the submodule as a package
        # attribute), so reach the real module object via sys.modules.
        import sys

        reconcile_mod = sys.modules["se3.engine.merge.reconcile"]
        monkeypatch.setattr(reconcile_mod, "reconcile", _boom)

        exit_code = run_merge(
            ["feature"], strategy="fast", delete_merged=False, project_root=root
        )

        assert exit_code == 1
        # The integrate half still landed the branch (feature is now an
        # ancestor), so a rerun re-attempts only the version decision.
        merge_base = _git(
            root, "merge-base", "--is-ancestor", "feature", "HEAD"
        )
        assert merge_base.returncode == 0

    def test_reconcile_fault_with_delete_merged_preserves_branch_for_rerun(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """delete_merged + reconcile fault must not delete the source branch.

        Recovery contract regression: with the default --delete-merged, a
        reconcile fault used to leave the operator unable to rerun
        ``se3 merge feature`` because integrate had already deleted ``feature``
        during cleanup. Branch deletion is now deferred until AFTER reconcile
        succeeds, so a fault preserves the branch AND its intent, and a rerun
        (with reconcile healthy) re-attempts and lands the version decision.
        """
        import sys

        root = _make_project(tmp_path, "2.0.0")
        _make_feature_with_intent(
            root, "feature", "flowA", bump_type="minor", change="feat A"
        )

        reconcile_mod = sys.modules["se3.engine.merge.reconcile"]
        real_reconcile = reconcile_mod.reconcile

        def _boom(*_args, **_kwargs):
            raise VersionRegressionError("computed final would regress")

        monkeypatch.setattr(reconcile_mod, "reconcile", _boom)

        exit_code = run_merge(
            ["feature"], strategy="fast", delete_merged=True, project_root=root
        )

        assert exit_code == 1
        # Deferred cleanup: the source branch was NOT deleted by the failed run.
        assert (
            _git(root, "branch", "--list", "feature").stdout.strip() != ""
        ), "reconcile fault must preserve the source branch for a rerun"
        # The version decision never landed.
        assert _pyproject_version(root) == "2.0.0"
        assert not is_consumed(root, "flowA")

        # Heal reconcile and rerun the WHOLE command against the still-existing
        # branch: integrate is now a no-op (already ancestor), reconcile lands
        # the version, and the branch is finally cleaned up.
        monkeypatch.setattr(reconcile_mod, "reconcile", real_reconcile)
        rerun_code = run_merge(
            ["feature"], strategy="fast", delete_merged=True, project_root=root
        )

        assert rerun_code == 0
        assert _pyproject_version(root) == "2.1.0"
        assert is_consumed(root, "flowA")
        # Cleanup now ran on the clean rerun.
        assert _git(root, "branch", "--list", "feature").stdout.strip() == ""


    def test_unreadable_intent_scope_preserves_branch_and_fails(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A git fault reading the intent scope must NOT silently publish.

        Regression guard (self-check): ``intent_flow_ids_at_ref`` used to degrade
        ANY git failure to an empty set. The CLI then reconciled with an empty
        scope (a clean no-op) and, under ``--delete-merged``, deleted the source
        branch — leaving the merged feature on master with no version bump /
        changelog while reporting a clean run. An unreadable scope must instead
        be surfaced as a non-zero exit that preserves the branch for a rerun,
        exactly like a reconcile fault.
        """
        import sys

        root = _make_project(tmp_path, "2.0.0")
        _make_feature_with_intent(
            root, "feature", "flowA", bump_type="minor", change="feat A"
        )

        vi_mod = sys.modules["se3.engine.version_intent"]
        real_introduced = vi_mod.intent_flow_ids_introduced

        def _boom(*_args, **_kwargs):
            raise vi_mod.IntentReadError("git ls-tree timed out")

        monkeypatch.setattr(vi_mod, "intent_flow_ids_introduced", _boom)

        exit_code = run_merge(
            ["feature"], strategy="fast", delete_merged=True, project_root=root
        )

        assert exit_code == 1
        # The source branch survives the failed run (no silent publish + delete).
        assert (
            _git(root, "branch", "--list", "feature").stdout.strip() != ""
        ), "an unreadable intent scope must preserve the branch for a rerun"
        # No version was landed and the intent stays outstanding.
        assert _pyproject_version(root) == "2.0.0"
        assert not is_consumed(root, "flowA")

        # Heal the probe and rerun: integrate is a no-op (already ancestor), the
        # scope now reads cleanly, reconcile lands the version, branch cleaned up.
        monkeypatch.setattr(vi_mod, "intent_flow_ids_introduced", real_introduced)
        rerun_code = run_merge(
            ["feature"], strategy="fast", delete_merged=True, project_root=root
        )

        assert rerun_code == 0
        assert _pyproject_version(root) == "2.1.0"
        assert is_consumed(root, "flowA")
        assert _git(root, "branch", "--list", "feature").stdout.strip() == ""

    def test_tag_failure_preserves_branch_and_reports_committed_reconcile(
        self, tmp_path: Path, monkeypatch, capsys
    ) -> None:
        """A tag failure is loud and leaves the source branch for recovery.

        The reconcile commit is created before the annotated tag. If tag
        creation fails, the CLI must not delete the source branch under
        ``--delete-merged`` and the rendered error must make the already-created
        reconcile commit / missing-tag state diagnosable.
        """
        import sys

        from se3.engine.git_tags import VersionTagError
        from se3.engine.version_intent import reconcile_commit_exists

        root = _make_project(tmp_path, "2.0.0")
        _make_feature_with_intent(
            root, "feature", "flowA", bump_type="minor", change="feat A"
        )

        reconcile_mod = sys.modules["se3.engine.merge.reconcile"]

        def _tag_boom(*_args, **_kwargs):
            raise VersionTagError("v2.1.0", "simulated tag helper failure")

        monkeypatch.setattr(
            reconcile_mod,
            "create_annotated_version_tag",
            _tag_boom,
        )

        exit_code = run_merge(
            ["feature"], strategy="fast", delete_merged=True, project_root=root
        )

        rendered = capsys.readouterr().out
        assert exit_code == 1
        assert _git(root, "branch", "--list", "feature").stdout.strip() != ""
        assert reconcile_commit_exists(root, "flowA")
        assert _pyproject_version(root) == "2.1.0"
        assert _git(root, "tag", "--list", "v2.1.0").stdout.strip() == ""
        assert "failed to create version tag v2.1.0" in rendered
        assert "version reconcile commit may already exist" in rendered
        assert "source branch is preserved" in rendered


class TestNoConfirmationGate:
    """The CLI wrapper drives library (suppress_human_call) mode — no gate."""

    def test_cli_passes_suppress_human_call(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """``se3 merge`` invokes run_merge with suppress_human_call=True.

        In that mode the orchestrator records escalations on the result instead
        of writing ``se3/calls/`` files or printing terminal instructions — the
        CLI has no confirmation gate and expresses everything via the exit code.
        """
        root = _make_project(tmp_path, "1.0.0")
        default = _default_branch(root)
        _git(root, "checkout", "-q", "-b", "feature")
        (root / "f.txt").write_text("x\n", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "feat")
        _git(root, "checkout", "-q", default)

        captured: dict = {}

        def _capture(branches, **kwargs):
            captured.update(kwargs)
            captured["branches"] = branches
            return 0

        monkeypatch.setattr("se3.commands.merge_cmd.run_merge", _capture)

        import os

        old_cwd = os.getcwd()
        os.chdir(str(root))
        try:
            from typer.testing import CliRunner

            from se3.cli import app

            result = CliRunner().invoke(app, ["merge", "feature"])
        finally:
            os.chdir(old_cwd)

        assert result.exit_code == 0, result.output
        assert captured.get("suppress_human_call") is True

    def test_no_calls_file_written_on_clean_merge(self, tmp_path: Path) -> None:
        """The CLI path writes no ``se3/calls/`` escalation on a clean merge."""
        root = _make_project(tmp_path, "1.0.0")
        _make_feature_with_intent(
            root, "feature", "flowA", bump_type="patch", change="fix A"
        )

        exit_code = run_merge(
            ["feature"],
            strategy="fast",
            delete_merged=False,
            project_root=root,
            suppress_human_call=True,
        )

        assert exit_code == 0
        calls_dir = root / "se3" / "calls"
        # Either the dir is absent or holds no call files — nothing was written.
        if calls_dir.exists():
            assert not list(calls_dir.glob("*.json"))


PACKAGE_JSON_TEMPLATE = """\
{{
  "name": "demo",
  "version": "{version}"
}}
"""


def _make_node_project(tmp_path: Path, version: str = "1.2.3") -> Path:
    """A git-backed Node.js project whose version lives in package.json.

    Guards the merge-side reconcile against assuming pyproject.toml: a Node
    worktree flow commits intent-only, and reconcile must read/write the
    project's actual version file (package.json) the same way the commit path
    would — otherwise the flow can never land its version at merge time.
    """
    root = tmp_path / "node-proj"
    root.mkdir()
    (root / "package.json").write_text(
        PACKAGE_JSON_TEMPLATE.format(version=version), encoding="utf-8"
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


class TestReconcileUsesConfiguredVersionFile:
    """Reconcile writes the project's configured version file, not just pyproject."""

    def test_reconcile_lands_version_in_package_json(self, tmp_path: Path) -> None:
        import json

        root = _make_node_project(tmp_path, "1.2.3")
        _make_feature_with_intent(
            root, "feature", "flowN", bump_type="minor", change="feat N"
        )

        exit_code = run_merge(
            ["feature"], strategy="fast", delete_merged=False, project_root=root
        )

        assert exit_code == 0
        # Deterministic SemVer channel: max(minor) applied to 1.2.3 -> 1.3.0,
        # written into package.json (there is no pyproject.toml to fall back on).
        assert not (root / "pyproject.toml").exists()
        data = json.loads((root / "package.json").read_text(encoding="utf-8"))
        assert data["version"] == "1.3.0"
        # read_current_version resolves package.json too.
        assert read_current_version(root) == "1.3.0"
        # The changelog bullet is filed under the reconciled version and the
        # package.json change is staged into the reconcile commit.
        versions = (root / "VERSIONS.md").read_text(encoding="utf-8")
        assert "1.3.0" in versions and "feat N" in versions
        committed = _git(root, "show", "HEAD:package.json").stdout
        assert '"1.3.0"' in committed
        assert is_consumed(root, "flowN")


class TestReconcileLibraryRegressions:
    """Direct-library regressions for the self-check fixes (iteration 4)."""

    def test_historical_versions_recognises_custom_suffixless_template(
        self, tmp_path: Path
    ) -> None:
        """A ``## {{version}}`` (no ``- date`` suffix) changelog is still parsed.

        Otherwise the custom-rules collision guard sees an empty history and an
        LLM could silently reuse an already-shipped version (fix #6).
        """
        import yaml

        from se3.engine.merge.reconcile import historical_versions

        root = tmp_path / "custom"
        root.mkdir()
        (root / "se3.yaml").write_text(
            yaml.safe_dump(
                {"documentation": {"versions_entry_template": "## {{version}}\n\n{{changes}}\n"}}
            ),
            encoding="utf-8",
        )
        (root / "VERSIONS.md").write_text(
            "# Changelog\n\n## 2026.07.06\n- x\n\n## 2026.07.01\n- y\n",
            encoding="utf-8",
        )
        assert historical_versions(root) == {"2026.07.06", "2026.07.01"}

    def test_historical_versions_recognises_single_hash_template(
        self, tmp_path: Path
    ) -> None:
        """A ``# {{version}}`` (single-hash heading) changelog is still parsed.

        The old detection required a ``##`` prefix, so a single-``#`` template
        yielded an empty history and the custom-rules collision guard could
        silently reuse an already-shipped version (fix #44).
        """
        import yaml

        from se3.engine.merge.reconcile import historical_versions

        root = tmp_path / "single_hash"
        root.mkdir()
        (root / "se3.yaml").write_text(
            yaml.safe_dump(
                {"documentation": {"versions_entry_template": "# {{version}}\n\n{{changes}}\n"}}
            ),
            encoding="utf-8",
        )
        # Use a ``##`` title so it is NOT mistaken for a single-hash version header.
        (root / "VERSIONS.md").write_text(
            "## Changelog\n\n# 2026.07.07.1\n- x\n\n# 2026.07.06.2\n- y\n",
            encoding="utf-8",
        )
        assert historical_versions(root) == {"2026.07.07.1", "2026.07.06.2"}

    def test_historical_versions_recognises_triple_hash_template(
        self, tmp_path: Path
    ) -> None:
        """A ``### {{version}}`` (triple-hash heading) changelog is still parsed."""
        import yaml

        from se3.engine.merge.reconcile import historical_versions

        root = tmp_path / "triple_hash"
        root.mkdir()
        (root / "se3.yaml").write_text(
            yaml.safe_dump(
                {"documentation": {"versions_entry_template": "### {{version}}\n\n{{changes}}\n"}}
            ),
            encoding="utf-8",
        )
        (root / "VERSIONS.md").write_text(
            "# Changelog\n\n### 3.2.1\n- x\n\n### 3.1.0\n- y\n",
            encoding="utf-8",
        )
        assert historical_versions(root) == {"3.2.1", "3.1.0"}

    def test_historical_versions_recognises_bracketed_template(
        self, tmp_path: Path
    ) -> None:
        """A ``## [{{version}}] - {{date}}`` header must NOT capture the ``]``.

        The literal suffix around the placeholder has to bound the captured
        version — otherwise ``## [1.2.4] - ...`` is recorded as ``1.2.4]`` and a
        later LLM result of ``1.2.4`` slips past the reuse guard (fix #43).
        """
        import yaml

        from se3.engine.merge.reconcile import historical_versions

        root = tmp_path / "bracketed"
        root.mkdir()
        (root / "se3.yaml").write_text(
            yaml.safe_dump(
                {
                    "documentation": {
                        "versions_entry_template": "## [{{version}}] - {{date}}\n\n{{changes}}\n"
                    }
                }
            ),
            encoding="utf-8",
        )
        (root / "VERSIONS.md").write_text(
            "# Changelog\n\n## [1.2.4] - 2026-07-07\n- x\n\n## [1.2.3] - 2026-07-01\n- y\n",
            encoding="utf-8",
        )
        assert historical_versions(root) == {"1.2.4", "1.2.3"}

    def test_restore_reconcile_paths_unstages_and_reverts(
        self, tmp_path: Path
    ) -> None:
        """A failed reconcile's staged writes are reset in BOTH index and tree.

        A plain ``git checkout -- <path>`` would leave the staged bump in the
        index; the recovery must return the reconcile-owned files to HEAD (fix
        #1).
        """
        from se3.engine.merge.reconcile import _restore_reconcile_paths

        root = _make_project(tmp_path, "1.0.0")
        # Simulate a half-applied reconcile: bump the version file and stage it.
        (root / "pyproject.toml").write_text(
            PYPROJECT_TEMPLATE.format(version="1.1.0"), encoding="utf-8"
        )
        _git(root, "add", "pyproject.toml")
        assert _git(root, "status", "--porcelain").stdout.strip()

        _restore_reconcile_paths(root)

        # Index and worktree both back to HEAD → clean tree, original version.
        assert _git(root, "status", "--porcelain").stdout.strip() == ""
        assert read_current_version(root) == "1.0.0"

    def test_undo_last_reconcile_only_when_head_carries_trailer(
        self, tmp_path: Path
    ) -> None:
        """``undo_last_reconcile`` resets a reconcile HEAD, refuses otherwise (fix #3)."""
        from se3.engine.merge.reconcile import undo_last_reconcile

        root = _make_project(tmp_path, "1.0.0")
        # A reconcile commit at HEAD carrying the durable trailer.
        (root / "pyproject.toml").write_text(
            PYPROJECT_TEMPLATE.format(version="1.1.0"), encoding="utf-8"
        )
        _git(root, "add", "-A")
        _git(
            root, "commit", "-q", "-m",
            "chore: reconcile version to 1.1.0\n\nVersion-Reconcile-Session: flowZ",
        )
        assert undo_last_reconcile(root, "flowZ") is True
        assert read_current_version(root) == "1.0.0"
        # HEAD is now the plain baseline (no trailer) → nothing to undo.
        assert undo_last_reconcile(root, "flowZ") is False

    def test_custom_rules_llm_transport_error_becomes_reconcile_error(
        self, tmp_path: Path
    ) -> None:
        """An LLM transport fault in the custom-rules channel is typed (fix #5).

        run_merge only catches ReconcileError/VersionRegressionError around
        reconcile, so a raw transport exception would otherwise escape after the
        branch integration already committed.
        """
        from se3.engine.merge.reconcile import ReconcileError, compute_via_rules
        from se3.engine.version_intent import VersionIntent

        def _boom(_prompt: str) -> str:
            raise TimeoutError("llm timed out")

        intents = [
            VersionIntent(
                flow_id="flowT",
                change_summary="c",
                versions_changes=["x"],
                bump_type="minor",
            )
        ]
        import pytest

        with pytest.raises(ReconcileError):
            compute_via_rules(
                tmp_path, "1.0.0", intents, "rules", _boom
            )


class TestReconcileIdempotencyAndRollback:
    """Regression tests for the crash-safety / concurrency fixes."""

    def test_committed_consumed_flag_without_reconcile_commit_still_bumps(
        self, tmp_path: Path
    ) -> None:
        """A ``consumed=true`` flag is NOT authoritative without a reconcile commit.

        Failure shape: a crash leaves version/changelog/consumed writes staged,
        then some unrelated commit lands the staged intent file (consumed=true)
        with NO ``Version-Reconcile-Session`` trailer. The bump must still fire —
        the git-durable reconcile commit is the sole "already reconciled" signal.
        """
        from se3.engine.merge.reconcile import reconcile
        from se3.engine.version_intent import (
            VersionIntent,
            reconcile_commit_exists,
            write_intent,
        )

        root = _make_project(tmp_path, "1.0.0")
        # An intent already flagged consumed, committed to HEAD by an unrelated
        # commit (no reconcile trailer).
        write_intent(
            root,
            VersionIntent(
                flow_id="flowGhost",
                change_summary="c",
                versions_changes=["ghost feature"],
                bump_type="minor",
                consumed=True,
            ),
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "unrelated: land staged intent file")
        assert not reconcile_commit_exists(root, "flowGhost")

        result = reconcile(root)

        assert result.success
        # The stale consumed flag did NOT suppress the bump.
        assert result.final_version == "1.1.0"
        assert read_current_version(root) == "1.1.0"
        assert reconcile_commit_exists(root, "flowGhost")
        versions = (root / "VERSIONS.md").read_text(encoding="utf-8")
        assert "ghost feature" in versions

    def test_mid_apply_failure_rolls_back_version_file(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A changelog-write failure after the version write leaves no dirty bump.

        If ``_merge_changelog`` raises after ``_write_final_version`` and the
        dirty version file survived, the next reconcile would read it as the base
        and double-bump (11.13.0 dirty -> 11.14.0). The apply-phase rollback must
        restore the version file to HEAD.
        """
        import pytest

        import sys

        from se3.engine.merge.reconcile import ReconcileError, reconcile
        from se3.engine.version_intent import VersionIntent, write_intent

        # ``se3.engine.merge`` re-exports the ``reconcile`` *function*, shadowing
        # the submodule name — reach the module object via sys.modules so the
        # monkeypatch targets the real ``_merge_changelog`` reconcile() calls.
        reconcile_mod = sys.modules["se3.engine.merge.reconcile"]

        root = _make_project(tmp_path, "1.0.0")
        write_intent(
            root,
            VersionIntent(
                flow_id="flowBoom",
                change_summary="c",
                versions_changes=["b"],
                bump_type="minor",
            ),
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "land intent")

        def _explode(*_a, **_k):
            raise OSError("disk full")

        monkeypatch.setattr(reconcile_mod, "_merge_changelog", _explode)

        with pytest.raises(ReconcileError):
            reconcile(root)

        # The version file must be clean at HEAD's 1.0.0 — no stranded 1.1.0.
        assert read_current_version(root) == "1.0.0"
        status = _git(root, "status", "--porcelain").stdout.strip()
        assert status == "", f"working tree left dirty: {status!r}"

    def test_undo_last_reconcile_preserves_unrelated_operator_edits(
        self, tmp_path: Path
    ) -> None:
        """Undoing a rejected reconcile must not destroy unrelated dirty edits.

        The flow ran in a worktree, so the operator is free to have uncommitted
        edits in the main checkout. A repo-wide ``git reset --hard`` would delete
        them; the scoped undo must preserve them while still reverting the
        reconcile-owned version file.
        """
        from se3.engine.merge.reconcile import undo_last_reconcile

        root = _make_project(tmp_path, "1.0.0")
        # A reconcile commit at HEAD carrying the durable trailer.
        (root / "pyproject.toml").write_text(
            PYPROJECT_TEMPLATE.format(version="1.1.0"), encoding="utf-8"
        )
        _git(root, "add", "-A")
        _git(
            root, "commit", "-q", "-m",
            "chore: reconcile version to 1.1.0\n\nVersion-Reconcile-Session: flowR",
        )
        # Operator's unrelated uncommitted work in the main checkout.
        (root / "operator_notes.txt").write_text("wip\n", encoding="utf-8")
        _git(root, "add", "operator_notes.txt")
        (root / "README.md").write_text("# Demo edited by operator\n", encoding="utf-8")

        assert undo_last_reconcile(root, "flowR") is True

        # Reconcile-owned version file reverted...
        assert read_current_version(root) == "1.0.0"
        # ...but the operator's unrelated edits survive.
        assert (root / "operator_notes.txt").read_text() == "wip\n"

    def test_flow_ids_restriction_consumes_only_named_intent(
        self, tmp_path: Path
    ) -> None:
        """Restricting to one flow_id bumps only that intent, leaving others.

        Guards the concurrency fix: the step path passes ``flow_ids=[flow_id]``
        so a flow's reconcile never sweeps a sibling flow's intent that landed on
        master in the between-steps lock gap.
        """
        from se3.engine.merge.reconcile import reconcile
        from se3.engine.version_intent import (
            VersionIntent,
            reconcile_commit_exists,
            write_intent,
        )

        root = _make_project(tmp_path, "1.0.0")
        for flow_id, change in (("flowA", "feat A"), ("flowB", "feat B")):
            write_intent(
                root,
                VersionIntent(
                    flow_id=flow_id,
                    change_summary=f"{flow_id} change",
                    versions_changes=[change],
                    bump_type="minor",
                ),
            )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "land two intents")

        result = reconcile(root, flow_ids=["flowA"])

        assert result.success
        assert result.consumed_flow_ids == ["flowA"]
        assert reconcile_commit_exists(root, "flowA")
        # flowB's intent is untouched — its own reconcile bumps it independently.
        assert not reconcile_commit_exists(root, "flowB")
        versions = (root / "VERSIONS.md").read_text(encoding="utf-8")
        assert "feat A" in versions
        assert "feat B" not in versions

    def test_scoped_reconcile_faults_when_requested_intent_is_corrupt(
        self, tmp_path: Path
    ) -> None:
        """A scoped flow_id whose committed intent JSON is corrupt must FAULT.

        Divergence (fix #high): the ``se3 merge`` CLI computes the reconcile scope
        from the merged branch tree's intent FILENAMES
        (``intent_flow_ids_introduced``), then calls
        ``reconcile(flow_ids=[...])``. ``collect_intents`` silently drops an intent
        whose JSON is corrupt/invalid. If that emptied the scope, reconcile would
        return an ``already_reconciled`` no-op and the branch would land with NO
        version bump or changelog. A corrupt requested intent is NOT "no
        outstanding intents": reconcile must raise so the branch is preserved for a
        rerun instead of being published clean.
        """
        import pytest

        from se3.engine.merge.reconcile import ReconcileError, reconcile
        from se3.engine.version_intent import (
            VERSION_INTENT_DIR_RELPATH,
            reconcile_commit_exists,
        )

        root = _make_project(tmp_path, "1.0.0")
        # A committed-but-corrupt intent file for the requested flow — exactly what
        # a branch carrying se3/version-intents/flowBad.json but invalid content
        # looks like once merged into master.
        intents_dir = root / VERSION_INTENT_DIR_RELPATH
        intents_dir.mkdir(parents=True, exist_ok=True)
        (intents_dir / "flowBad.json").write_text("{ not valid json", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "land corrupt intent")
        assert not reconcile_commit_exists(root, "flowBad")

        with pytest.raises(ReconcileError) as excinfo:
            reconcile(root, flow_ids=["flowBad"])
        assert "flowBad" in str(excinfo.value)
        # Nothing was published — no bump, no reconcile commit.
        assert not reconcile_commit_exists(root, "flowBad")
        assert read_current_version(root) == "1.0.0"

    def test_first_run_preserves_operator_edits_to_reconcile_owned_files(
        self, tmp_path: Path
    ) -> None:
        """A normal first reconcile must not wipe operator edits to README etc.

        The entry-time restore is gated on genuine crash residue (a consumed flag
        with no reconcile commit). Over a fresh, unconsumed intent there is no
        residue, so the restore is skipped and the operator's uncommitted README
        edit in the main checkout survives (the worktree flow ran elsewhere).
        """
        from se3.engine.merge.reconcile import reconcile
        from se3.engine.version_intent import VersionIntent, write_intent

        root = _make_project(tmp_path, "1.0.0")
        write_intent(
            root,
            VersionIntent(
                flow_id="flowOp",
                change_summary="c",
                versions_changes=["feat op"],
                bump_type="minor",
            ),
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "land intent")
        # Operator's uncommitted edit to a reconcile-owned file.
        (root / "README.md").write_text(
            "# Demo — operator WIP unrelated edit\n", encoding="utf-8"
        )

        result = reconcile(root)

        assert result.success
        assert result.final_version == "1.1.0"
        # The operator's README edit survived (not reverted to HEAD).
        assert "operator WIP unrelated edit" in (
            root / "README.md"
        ).read_text(encoding="utf-8")

    def test_reconcile_commit_excludes_operator_edits_within_owned_doc(
        self, tmp_path: Path
    ) -> None:
        """Operator dirt WITHIN a reconcile-owned doc file stays out of the commit.

        The prior fix kept unrelated *separate* files out; this guards the harder
        case where the operator edited README.md itself. reconcile owns only the
        version header there, so its commit must carry reconcile's change alone
        while the operator's unrelated line survives uncommitted in the working
        tree — never swept into the release commit.
        """
        from se3.engine.merge.reconcile import reconcile
        from se3.engine.version_intent import VersionIntent, write_intent

        root = _make_project(tmp_path, "1.0.0")
        write_intent(
            root,
            VersionIntent(
                flow_id="flowD",
                change_summary="c",
                versions_changes=["feat d"],
                bump_type="minor",
            ),
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "land intent")
        # Operator's unrelated uncommitted edit inside a reconcile-owned doc file.
        (root / "README.md").write_text(
            "# Demo\n\noperator WIP unrelated note\n", encoding="utf-8"
        )

        result = reconcile(root)

        assert result.success
        # The operator's edit survives on disk...
        assert "operator WIP unrelated note" in (
            root / "README.md"
        ).read_text(encoding="utf-8")
        # ...but did NOT ride into the reconcile commit.
        committed_readme = _git(
            root, "show", "HEAD:README.md"
        ).stdout
        assert "operator WIP unrelated note" not in committed_readme

    def test_reconcile_commit_failure_preserves_operator_owned_doc_edits(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A failed reconcile restores operator dirt in owned docs, not deletes it.

        The failure rollback resets reconcile-owned paths to HEAD; without the
        detach/reattach that would wipe an operator's uncommitted README edit.
        """
        import sys

        import pytest

        from se3.engine.merge.reconcile import ReconcileError, reconcile
        from se3.engine.version_intent import VersionIntent, write_intent

        reconcile_mod = sys.modules["se3.engine.merge.reconcile"]

        root = _make_project(tmp_path, "1.0.0")
        write_intent(
            root,
            VersionIntent(
                flow_id="flowBoom",
                change_summary="c",
                versions_changes=["b"],
                bump_type="minor",
            ),
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "land intent")
        (root / "README.md").write_text(
            "# Demo\n\noperator WIP survives failure\n", encoding="utf-8"
        )

        def _explode(*_a, **_k):
            raise reconcile_mod.ReconcileError("simulated commit failure")

        monkeypatch.setattr(reconcile_mod, "_commit_reconcile", _explode)

        with pytest.raises(ReconcileError):
            reconcile(root)

        # Version file rolled back, but the operator's README edit survived.
        assert read_current_version(root) == "1.0.0"
        assert "operator WIP survives failure" in (
            root / "README.md"
        ).read_text(encoding="utf-8")

    def test_first_run_preserves_operator_edit_to_version_file(
        self, tmp_path: Path
    ) -> None:
        """An operator's uncommitted edit to the VERSION FILE must survive.

        reconcile owns the version field, but the version file may carry unrelated
        operator dirt (e.g. a dependency line). The old code reset the version file
        to HEAD and never replayed it, silently destroying that edit. It must now
        be detached and 3-way merged back on top of the committed bump.
        """
        from se3.engine.merge.reconcile import reconcile
        from se3.engine.version_intent import VersionIntent, write_intent

        root = _make_project(tmp_path, "1.0.0")
        write_intent(
            root,
            VersionIntent(
                flow_id="flowVF",
                change_summary="c",
                versions_changes=["feat vf"],
                bump_type="minor",
            ),
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "land intent")
        # Operator's unrelated uncommitted edit to the version file: a dependency
        # line appended well away from the version field.
        (root / "pyproject.toml").write_text(
            PYPROJECT_TEMPLATE.format(version="1.0.0")
            + '\ndependencies = ["operator-added-dep"]\n',
            encoding="utf-8",
        )

        result = reconcile(root)

        assert result.success
        assert result.final_version == "1.1.0"
        pyproject = (root / "pyproject.toml").read_text(encoding="utf-8")
        # reconcile's bump landed AND the operator's dependency edit survived.
        assert 'version = "1.1.0"' in pyproject
        assert "operator-added-dep" in pyproject
        # The committed reconcile bump carries ONLY the version change, not the
        # operator's uncommitted dependency line.
        committed = _git(root, "show", "HEAD:pyproject.toml").stdout
        assert 'version = "1.1.0"' in committed
        assert "operator-added-dep" not in committed
        # The operator's edit is left uncommitted for them to handle.
        assert "pyproject.toml" in _git(root, "status", "--porcelain").stdout

    def test_first_run_preserves_operator_note_and_commits_changelog(
        self, tmp_path: Path
    ) -> None:
        """Operator dirt in VERSIONS.md coexists with the committed changelog entry.

        Regression guard for the reverse-diff bug: writing the operator's whole
        pre-reconcile VERSIONS.md snapshot back over the working tree used to erase
        the newly committed changelog entry from the working copy, so ``git
        status`` showed the release note removed relative to HEAD (a later operator
        commit could revert it). The operator diff must be 3-way merged on top of
        the committed entry instead.
        """
        from se3.engine.merge.reconcile import reconcile
        from se3.engine.version_intent import VersionIntent, write_intent

        root = _make_project(tmp_path, "1.0.0")
        write_intent(
            root,
            VersionIntent(
                flow_id="flowVN",
                change_summary="c",
                versions_changes=["feat vn changelog line"],
                bump_type="minor",
            ),
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "land intent")
        # Operator appends an unrelated note at the END of VERSIONS.md (away from
        # the top, where reconcile inserts the new entry).
        versions_before = (root / "VERSIONS.md").read_text(encoding="utf-8")
        (root / "VERSIONS.md").write_text(
            versions_before + "\noperator unrelated trailing note\n",
            encoding="utf-8",
        )

        result = reconcile(root)

        assert result.success
        working = (root / "VERSIONS.md").read_text(encoding="utf-8")
        committed = _git(root, "show", "HEAD:VERSIONS.md").stdout
        # The reconcile commit records the new changelog entry...
        assert "feat vn changelog line" in committed
        assert "1.1.0" in committed
        # ...and the working tree keeps BOTH the committed entry AND the operator
        # note — the entry is NOT reverse-diffed out.
        assert "feat vn changelog line" in working
        assert "operator unrelated trailing note" in working
        # The only working-tree delta versus HEAD is the operator's added note —
        # NO diff line REMOVES the just-committed changelog entry.
        status_diff = _git(root, "diff", "HEAD", "--", "VERSIONS.md").stdout
        assert "operator unrelated trailing note" in status_diff
        removed_lines = [
            ln for ln in status_diff.splitlines()
            if ln.startswith("-") and not ln.startswith("---")
        ]
        assert not any("feat vn changelog line" in ln for ln in removed_lines)

    def test_operator_deletion_of_versions_md_is_replayed_not_lost(
        self, tmp_path: Path
    ) -> None:
        """An operator deletion of a reconcile-owned file survives the reconcile.

        Regression guard: ``_detach_operator_edits`` used to skip a path that no
        longer existed in the working tree, so an operator deletion of VERSIONS.md
        was never snapshotted. reconcile then recreated and committed it, silently
        undoing the deletion. The deletion is real dirt: reconcile must commit its
        own changelog change from HEAD, then replay the deletion in the working
        tree (the operator's uncommitted intent) rather than resurrect the file.
        """
        from se3.engine.merge.reconcile import reconcile
        from se3.engine.version_intent import VersionIntent, write_intent

        root = _make_project(tmp_path, "1.0.0")
        write_intent(
            root,
            VersionIntent(
                flow_id="flowDel",
                change_summary="c",
                versions_changes=["feat del changelog line"],
                bump_type="minor",
            ),
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "land intent")
        # Operator deletes the tracked VERSIONS.md before reconcile runs.
        (root / "VERSIONS.md").unlink()

        result = reconcile(root)

        assert result.success
        # reconcile committed its changelog change from HEAD despite the deletion.
        committed = _git(root, "show", "HEAD:VERSIONS.md").stdout
        assert "feat del changelog line" in committed
        assert "1.1.0" in committed
        # ...but the operator's deletion is replayed in the working tree, not lost:
        # VERSIONS.md is absent from the working copy and shows as a pending
        # deletion against the reconcile commit for the operator to resolve.
        assert not (root / "VERSIONS.md").exists()
        status = _git(root, "status", "--porcelain", "--", "VERSIONS.md").stdout
        assert status.strip().startswith("D") or " D" in status

    def test_untracked_operator_file_on_owned_path_not_swept_into_commit(
        self, tmp_path: Path
    ) -> None:
        """An UNTRACKED operator file on a reconcile-owned path stays operator work.

        Regression guard: when a reconcile-owned path (here README.md) is absent
        from HEAD and the operator holds an untracked file there,
        ``_detach_operator_edits`` snapshotted it but relied on
        ``git checkout HEAD -- README.md`` to clear it — a silent no-op for a path
        not in HEAD. The file stayed in the working tree and ``_commit_reconcile``'s
        ``git add`` swept the operator content into the reconcile commit. The file
        must instead be unlinked (so reconcile writes on the empty base) and the
        untracked content replayed afterward as uncommitted operator work.
        """
        from se3.engine.merge.reconcile import reconcile
        from se3.engine.version_intent import VersionIntent, write_intent

        root = _make_project(tmp_path, "1.0.0")
        # Make README.md untracked: remove it from git while leaving the intent
        # landing to reconcile. (A plain README with no version badge is not
        # rewritten by the docs updater, so any post-reconcile README content is
        # purely the replayed operator file.)
        _git(root, "rm", "-q", "README.md")
        write_intent(
            root,
            VersionIntent(
                flow_id="flowUntracked",
                change_summary="c",
                versions_changes=["feat untracked case"],
                bump_type="minor",
            ),
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "drop README + land intent")
        assert not (root / "README.md").exists()
        # Operator drops an untracked README.md at the reconcile-owned path.
        (root / "README.md").write_text(
            "# Operator scratch README\n", encoding="utf-8"
        )

        result = reconcile(root)

        assert result.success
        assert result.final_version == "1.1.0"
        # The operator's untracked README survives verbatim in the working tree...
        assert (root / "README.md").read_text(encoding="utf-8") == (
            "# Operator scratch README\n"
        )
        # ...and is still UNTRACKED — it did not ride into the reconcile commit.
        readme_status = _git(
            root, "status", "--porcelain", "--", "README.md"
        ).stdout
        assert readme_status.strip().startswith("??")
        # README.md is absent from HEAD (the reconcile commit), so the operator
        # content was never committed as a release artifact.
        head_readme = subprocess.run(
            ["git", "-C", str(root), "cat-file", "-e", "HEAD:README.md"],
            capture_output=True, text=True, check=False,
        )
        assert head_readme.returncode != 0
        # The changelog entry still landed on the tracked VERSIONS.md.
        assert "feat untracked case" in _git(
            root, "show", "HEAD:VERSIONS.md"
        ).stdout

    def test_divergent_staged_refusal_preserves_earlier_operator_edit(
        self, tmp_path: Path
    ) -> None:
        """A refusal on a later owned path must not wipe an earlier one's edit.

        Divergence (fix #critical): ``_detach_operator_edits`` mutated each
        reconcile-owned path in place as it scanned, and reconcile() invoked it
        BEFORE the try/finally that reattaches snapshots. Owned paths are ordered
        [version file, README.md, VERSIONS.md]. With README.md holding an unstaged
        operator edit and VERSIONS.md a staged state divergent from both HEAD and
        its working tree, the scan reset README.md to HEAD first, then raised
        ReconcileError on VERSIONS.md — stranding README.md's operator edit wiped
        with no snapshot to replay it. The read-only pre-pass must reject the
        divergent VERSIONS.md up front, before README.md is touched, so the edit
        survives.
        """
        from se3.engine.merge.reconcile import ReconcileError, reconcile
        from se3.engine.version_intent import VersionIntent, write_intent

        root = _make_project(tmp_path, "1.0.0")
        write_intent(
            root,
            VersionIntent(
                flow_id="flowDiv",
                change_summary="c",
                versions_changes=["feat div"],
                bump_type="minor",
            ),
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "land intent")

        # README.md: an unstaged operator edit (processed before VERSIONS.md).
        readme_edit = "# Demo — operator note before the refusal\n"
        (root / "README.md").write_text(readme_edit, encoding="utf-8")

        # VERSIONS.md: staged content A, then working-tree content B — divergent
        # from both HEAD and each other, the un-replayable case reconcile refuses.
        (root / "VERSIONS.md").write_text(
            "# Demo Version History\n\n## 1.0.0\n- staged A\n", encoding="utf-8"
        )
        _git(root, "add", "VERSIONS.md")
        (root / "VERSIONS.md").write_text(
            "# Demo Version History\n\n## 1.0.0\n- working B\n", encoding="utf-8"
        )

        try:
            reconcile(root)
        except ReconcileError as exc:
            assert "staged state" in str(exc)
        else:
            raise AssertionError("expected reconcile to refuse the divergent stage")

        # The refusal happened, but README.md's operator edit is intact — it was
        # never reset to HEAD because the pre-pass rejected before any mutation.
        assert (root / "README.md").read_text(encoding="utf-8") == readme_edit
        # No version bump landed; the tree is left for the operator to resolve.
        assert read_current_version(root) == "1.0.0"

    def test_reconcile_commit_excludes_unrelated_staged_files(
        self, tmp_path: Path
    ) -> None:
        """The reconcile commit contains only reconcile-owned paths.

        An operator may have unrelated files staged in the shared main checkout;
        a whole-tree commit would sweep them into the version reconcile commit.
        The path-limited commit keeps them out (and staged for the operator).
        """
        from se3.engine.merge.reconcile import reconcile
        from se3.engine.version_intent import VersionIntent, write_intent

        root = _make_project(tmp_path, "1.0.0")
        write_intent(
            root,
            VersionIntent(
                flow_id="flowS",
                change_summary="c",
                versions_changes=["feat s"],
                bump_type="minor",
            ),
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "land intent")
        # Operator stages an unrelated file before the reconcile lands.
        (root / "unrelated.txt").write_text("operator staged work\n", encoding="utf-8")
        _git(root, "add", "unrelated.txt")

        result = reconcile(root)

        assert result.success
        committed = _git(
            root, "show", "--name-only", "--format=", "HEAD"
        ).stdout.split()
        # The version bump landed...
        assert "pyproject.toml" in committed
        # ...but the operator's unrelated staged file did NOT ride along.
        assert "unrelated.txt" not in committed
        assert "unrelated.txt" in _git(root, "status", "--porcelain").stdout


class TestVersionReconcileRevisionPath:
    """The version_reconcile step's human-review revision (rejection) path."""

    def _make_flow(self, root: Path, flow_id: str):
        from se3.engine.models import FlowInstance, State

        return FlowInstance(
            flow_id=flow_id,
            task_description="t",
            task_type="feature",
            state=State(),
            change_path=root / "se3.yaml",
        )

    def _make_step(self, root: Path):
        from se3.engine.models import Step, StepStatus, StepType

        step = Step(
            step_type=StepType.VERSION_RECONCILE,
            status=StepStatus.PENDING,
            step_id="09_version_reconcile_aaaaaaaa",
        )
        step.cwd = str(root)
        return step

    def test_rejected_decision_fails_when_reconcile_commit_is_buried(
        self, tmp_path: Path
    ) -> None:
        """A rejection whose reconcile commit is no longer HEAD must FAIL loudly.

        The merge lock is released between merge_integrate and version_reconcile,
        so a concurrent flow's merge can land on master while the reviewer
        deliberates, burying this flow's reconcile commit. undo_last_reconcile
        then refuses (returns False). Proceeding would let reconcile() see the
        still-present git trailer, no-op, and COMPLETE — silently re-releasing the
        rejected version. The handler must FAIL instead.
        """
        from se3.engine.models import StepStatus
        from se3.engine.steps.version_reconcile import version_reconcile_handler
        from se3.engine.version_intent import reconcile_commit_exists

        root = _make_project(tmp_path, "1.0.0")
        # This flow's reconcile commit (carries the durable trailer).
        (root / "pyproject.toml").write_text(
            PYPROJECT_TEMPLATE.format(version="1.1.0"), encoding="utf-8"
        )
        _git(root, "add", "-A")
        _git(
            root, "commit", "-q", "-m",
            "chore: reconcile version to 1.1.0\n\nVersion-Reconcile-Session: flowA",
        )
        # A concurrent flow's merge lands on top, burying the reconcile commit.
        (root / "other.txt").write_text("concurrent\n", encoding="utf-8")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "concurrent flow merge")
        assert reconcile_commit_exists(root, "flowA")

        flow = self._make_flow(root, "flowA")
        step = self._make_step(root)
        step.inputs = {"is_revision": True, "revision_feedback": "bump higher"}

        status = version_reconcile_handler(step, flow)

        assert status == StepStatus.FAILED
        assert "cannot be safely undone" in (step.error_message or "")
        # The rejected version was NOT silently re-released.
        assert read_current_version(root) == "1.1.0"

    def test_revision_with_undoable_commit_recomputes(self, tmp_path: Path) -> None:
        """When the reconcile commit is still HEAD, the rejection recomputes it."""
        from se3.engine.models import StepStatus
        from se3.engine.steps.version_reconcile import version_reconcile_handler

        root = _make_project(tmp_path, "1.0.0")
        write_intent(
            root,
            VersionIntent(
                flow_id="flowA",
                change_summary="c",
                versions_changes=["feat A"],
                bump_type="minor",
            ),
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "land intent")
        # Reconcile once so a reconcile commit exists at HEAD.
        from se3.engine.merge.reconcile import reconcile

        first = reconcile(root, flow_ids=["flowA"])
        assert first.final_version == "1.1.0"

        flow = self._make_flow(root, "flowA")
        step = self._make_step(root)
        step.inputs = {"is_revision": True}

        status = version_reconcile_handler(step, flow)

        # Undo succeeded (commit was HEAD) → recompute lands a fresh reconcile.
        assert status == StepStatus.COMPLETED
        assert read_current_version(root) == "1.1.0"

    def test_rejection_does_not_undo_a_sibling_flows_reconcile_commit(
        self, tmp_path: Path
    ) -> None:
        """A sibling flow's reconcile commit at HEAD must not be undone by rejection.

        Divergence (fix #critical): Flow A's decision is rejected. Before A's
        revision resumes, concurrent Flow B lands its OWN reconcile commit at HEAD
        (the merge lock is released between merge_integrate and version_reconcile).
        A generic-trailer undo would see *a* reconcile trailer at HEAD, reset B's
        commit away, then no-op because A's buried trailer still exists — removing
        B's published version while leaving A's rejected version standing. The
        flow-scoped undo must instead refuse and FAIL for manual recovery, keeping
        B's release intact.
        """
        from se3.engine.models import StepStatus
        from se3.engine.steps.version_reconcile import version_reconcile_handler
        from se3.engine.version_intent import reconcile_commit_exists

        root = _make_project(tmp_path, "1.0.0")
        # Flow A's (to-be-rejected) reconcile commit.
        (root / "pyproject.toml").write_text(
            PYPROJECT_TEMPLATE.format(version="1.1.0"), encoding="utf-8"
        )
        _git(root, "add", "-A")
        _git(
            root, "commit", "-q", "-m",
            "chore: reconcile version to 1.1.0\n\nVersion-Reconcile-Session: flowA",
        )
        # Concurrent Flow B lands its own reconcile commit on top of A's.
        (root / "pyproject.toml").write_text(
            PYPROJECT_TEMPLATE.format(version="1.2.0"), encoding="utf-8"
        )
        _git(root, "add", "-A")
        _git(
            root, "commit", "-q", "-m",
            "chore: reconcile version to 1.2.0\n\nVersion-Reconcile-Session: flowB",
        )
        assert reconcile_commit_exists(root, "flowA")
        assert reconcile_commit_exists(root, "flowB")

        flow = self._make_flow(root, "flowA")
        step = self._make_step(root)
        step.inputs = {"is_revision": True, "revision_feedback": "bump higher"}

        status = version_reconcile_handler(step, flow)

        assert status == StepStatus.FAILED
        assert "cannot be safely undone" in (step.error_message or "")
        # Flow B's published version is intact; A's rejected version was not
        # silently re-accepted by clobbering B.
        assert read_current_version(root) == "1.2.0"
        assert reconcile_commit_exists(root, "flowB")

    def test_missing_intent_with_no_reconcile_commit_fails(
        self, tmp_path: Path
    ) -> None:
        """An in-flow step whose intent is gone must FAIL, not no-op to success.

        Divergence (fix #high): a worktree flow reaches version_reconcile but its
        ``se3/version-intents/<flow_id>.json`` was never committed / was dropped by
        a bad merge / manually removed. reconcile(flow_ids=[flow]) collects no
        matching intent and returns an already_reconciled no-op. Completing here
        would land the merged work on master with no version bump or changelog for
        the flow. The handler must FAIL so the intent can be restored.
        """
        from se3.engine.models import StepStatus
        from se3.engine.steps.version_reconcile import version_reconcile_handler
        from se3.engine.version_intent import reconcile_commit_exists

        root = _make_project(tmp_path, "1.0.0")
        # No intent file for flowGone, and no reconcile commit for it either.
        assert not reconcile_commit_exists(root, "flowGone")

        flow = self._make_flow(root, "flowGone")
        step = self._make_step(root)

        status = version_reconcile_handler(step, flow)

        assert status == StepStatus.FAILED
        # The failure surfaces via reconcile()'s scoped-intent guard (the missing
        # flow_id is neither readable nor already reconciled), which raises before
        # the handler's post-hoc no-op guard is reached; the step's post-hoc guard
        # remains a backstop. Either message is an acceptable "intent gone" fault.
        msg = step.error_message or ""
        assert "no version intent found" in msg or "could not be read" in msg
        # Version untouched — nothing was silently published.
        assert read_current_version(root) == "1.0.0"

    def test_disabled_versioning_completes_without_intent_or_commit(
        self, tmp_path: Path
    ) -> None:
        """A version-disabled worktree flow COMPLETES the merge-side reconcile.

        Divergence (fix #medium): with ``version.enabled=false`` version_analyze
        emits no intent by design, so reconcile() legitimately no-ops with no
        reconcile commit (already_reconciled=True, version_disabled=True). The
        missing-intent guard would otherwise read that as a dropped intent and FAIL
        the step, wedging every worktree flow on a version-disabled project. The
        disabled case must COMPLETE: no version bump, no changelog, no commit.
        """
        from se3.engine.models import StepStatus
        from se3.engine.steps.version_reconcile import version_reconcile_handler
        from se3.engine.version_intent import reconcile_commit_exists

        root = _make_project(tmp_path, "1.0.0")
        # Disable version bumping for the project.
        (root / "se3.yaml").write_text(
            "version:\n  enabled: false\n", encoding="utf-8"
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "disable version bumping")

        # No intent, no reconcile commit — exactly the disabled worktree shape.
        assert not reconcile_commit_exists(root, "flowDisabled")

        flow = self._make_flow(root, "flowDisabled")
        step = self._make_step(root)

        status = version_reconcile_handler(step, flow)

        assert status == StepStatus.COMPLETED
        # No version bump and still no reconcile commit — the disabled contract.
        assert read_current_version(root) == "1.0.0"
        assert not reconcile_commit_exists(root, "flowDisabled")

    def test_review_flow_without_version_analyze_completes_cleanly(
        self, tmp_path: Path
    ) -> None:
        """A worktree 'review' flow (no VERSION_ANALYZE step) COMPLETES reconcile.

        Divergence (fix #low): a --worktree flow classified as 'review' runs
        ANALYZE / INVARIANT_CHECK / SUMMARIZE — no VERSION_ANALYZE, no COMMIT — so
        it emits no version intent and lands no commits (merge_integrate is a no-op
        already-ancestor merge). The two merge steps are still appended, so
        version_reconcile runs; a flow-scoped reconcile() would then hard-fault on
        the unaccounted flow_id (no intent JSON, no reconcile commit) and FAIL the
        whole flow at its final step even though the review completed. The by-design
        no-intent case must COMPLETE, exactly like the version-disabled case — the
        scoped hard-fault is only for a DROPPED intent a flow SHOULD have emitted.
        """
        from se3.engine.models import (
            FlowInstance,
            State,
            StepStatus,
            StepType,
        )
        from se3.engine.steps.version_reconcile import version_reconcile_handler
        from se3.engine.version_intent import reconcile_commit_exists

        root = _make_project(tmp_path, "1.0.0")
        assert not reconcile_commit_exists(root, "flowReview")

        # A review flow: its selected_steps carry no VERSION_ANALYZE.
        state = State()
        state.selected_steps = [
            StepType.ANALYZE,
            StepType.INVARIANT_CHECK,
            StepType.SUMMARIZE,
            StepType.MERGE_INTEGRATE,
            StepType.VERSION_RECONCILE,
        ]
        flow = FlowInstance(
            flow_id="flowReview",
            task_description="review the merge module",
            task_type="review",
            state=state,
            change_path=root / "se3.yaml",
        )
        step = self._make_step(root)

        status = version_reconcile_handler(step, flow)

        # The review completed: no fault, no bump, no reconcile commit.
        assert status == StepStatus.COMPLETED
        assert read_current_version(root) == "1.0.0"
        assert not reconcile_commit_exists(root, "flowReview")
        assert step.outputs["reconcile_result"]["channel"] == "noop"


class TestResidueRecoveryPreservesOperatorEdits:
    """Crash-residue recovery / rejection undo must not delete operator dirt.

    Both the entry-time residue recovery and ``undo_last_reconcile`` used to
    blind-``checkout HEAD -- <reconcile-owned path>``, silently deleting an
    operator's uncommitted edit that happened to sit on a path reconcile also owns
    (VERSIONS.md / README.md / the version file). The recovery must discard only
    reconcile-produced residue and preserve operator work.
    """

    def test_entry_residue_recovery_preserves_operator_edit_to_owned_doc(
        self, tmp_path: Path
    ) -> None:
        """A retry-time operator edit survives crash-residue recovery.

        Failure shape: a prior reconcile wrote ``consumed=true`` (no reconcile
        commit) and died. Before the retry the operator edits README.md. The old
        code saw ``has_residue`` and reset README.md to HEAD before any snapshot,
        losing the edit. It must now be detached and replayed.
        """
        from se3.engine.merge.reconcile import reconcile
        from se3.engine.version_intent import (
            VersionIntent,
            mark_consumed,
            reconcile_commit_exists,
            write_intent,
        )

        root = _make_project(tmp_path, "1.0.0")
        write_intent(
            root,
            VersionIntent(
                flow_id="flowRes",
                change_summary="c",
                versions_changes=["feat res"],
                bump_type="minor",
            ),
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "land intent")
        # Simulate crash residue: the consumed flag is set (uncommitted) with no
        # reconcile commit — the signal that triggers entry-time recovery.
        mark_consumed(root, "flowRes")
        assert _git(root, "status", "--porcelain").stdout.strip()
        assert not reconcile_commit_exists(root, "flowRes")
        # Operator's retry-time uncommitted edit to a reconcile-owned file.
        (root / "README.md").write_text(
            "# Demo — operator retry-time edit\n", encoding="utf-8"
        )

        result = reconcile(root)

        assert result.success
        assert result.final_version == "1.1.0"
        assert reconcile_commit_exists(root, "flowRes")
        # The operator's README edit survived the residue recovery.
        assert "operator retry-time edit" in (
            root / "README.md"
        ).read_text(encoding="utf-8")

    def test_unreadable_recovery_snapshot_is_preserved_and_aborts(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A transient read failure on the durable snapshot must not lose it.

        Failure shape: a prior hard kill detached an operator edit to HEAD and
        left the durable recovery snapshot in the git dir. The next reconcile
        finds it but a transient I/O fault (EACCES / ENOSPC) blocks the read. The
        old code returned silently, let reconcile run to completion, then cleared
        the still-unread snapshot in its finalize path — deleting the operator's
        only surviving copy of the pre-crash edit. reconcile must instead preserve
        the snapshot and abort with a typed failure so the retry can replay it.
        """
        import json

        import pytest

        from se3.engine.merge.reconcile import (
            ReconcileError,
            _recovery_snapshot_path,
            reconcile,
        )
        from se3.engine.version_intent import VersionIntent, write_intent

        root = _make_project(tmp_path, "1.0.0")
        write_intent(
            root,
            VersionIntent(
                flow_id="flowSnap",
                change_summary="c",
                versions_changes=["feat snap"],
                bump_type="minor",
            ),
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "land intent")

        # A durable snapshot left by an interrupted prior run: it carries the
        # operator's only surviving copy of a detached README edit.
        snap_path = _recovery_snapshot_path(root)
        assert snap_path is not None
        snap_path.write_text(
            json.dumps(
                {
                    "snapshots": {
                        "README.md": {
                            "head": "# Demo\n",
                            "operator": "# Demo — operator pre-crash edit\n",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        # Simulate a transient read fault on exactly the snapshot path.
        real_read_text = Path.read_text

        def _flaky_read_text(self, *a, **k):
            if self == snap_path:
                raise OSError("simulated EACCES on snapshot read")
            return real_read_text(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", _flaky_read_text)

        with pytest.raises(ReconcileError):
            reconcile(root)

        # The snapshot MUST still be on disk — it is the last copy of the edit.
        assert snap_path.exists()

    def test_undo_last_reconcile_preserves_operator_edit_to_owned_doc(
        self, tmp_path: Path
    ) -> None:
        """Rejecting a reconcile must keep an operator edit made to an owned doc.

        The reviewer holds an uncommitted README.md edit (a reconcile-owned path)
        when they reject the version. ``undo_last_reconcile`` reverts the reconcile
        commit's version bump but must not blind-restore README.md over the
        operator's edit.
        """
        from se3.engine.merge.reconcile import undo_last_reconcile

        root = _make_project(tmp_path, "1.0.0")
        # A reconcile commit at HEAD carrying the durable trailer.
        (root / "pyproject.toml").write_text(
            PYPROJECT_TEMPLATE.format(version="1.1.0"), encoding="utf-8"
        )
        _git(root, "add", "-A")
        _git(
            root, "commit", "-q", "-m",
            "chore: reconcile version to 1.1.0\n\nVersion-Reconcile-Session: flowR",
        )
        # Operator's uncommitted edit to a reconcile-OWNED path before rejecting.
        (root / "README.md").write_text(
            "# Demo — operator edit before rejection\n", encoding="utf-8"
        )

        assert undo_last_reconcile(root, "flowR") is True

        # The rejected version bump is reverted...
        assert read_current_version(root) == "1.0.0"
        # ...but the operator's README edit on the owned path survives.
        assert "operator edit before rejection" in (
            root / "README.md"
        ).read_text(encoding="utf-8")


class TestCustomRulesEqualToCurrentIsRejected:
    """The custom-rules channel has no legitimate no-op (self-check high fix)."""

    def _write_rules(self, root: Path) -> None:
        (root / "se3").mkdir(parents=True, exist_ok=True)
        (root / "se3" / "version-rules.md").write_text(
            "# Version Rules\n\nBump the minor for any feature.\n", encoding="utf-8"
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "add version rules")

    def test_llm_returning_current_version_is_typed_failure(
        self, tmp_path: Path
    ) -> None:
        """An LLM ``final_version`` equal to current must NOT consume + commit.

        Divergence guarded: with ``se3/version-rules.md`` present and an
        outstanding intent, an LLM that returns the current version string used to
        set ``publish_release`` false, skip validation, then consume the intent and
        create a no-op reconcile commit — permanently swallowing the release
        (future resumes see the commit and never re-bump). It must instead raise a
        typed reconcile failure with the intent left unconsumed and no commit.
        """
        import pytest

        from se3.engine.merge.reconcile import (
            VersionRegressionError,
            reconcile,
        )
        from se3.engine.version_intent import (
            VersionIntent,
            reconcile_commit_exists,
            write_intent,
        )

        root = _make_project(tmp_path, "1.2.3")
        self._write_rules(root)
        write_intent(
            root,
            VersionIntent(
                flow_id="flowEq",
                change_summary="c",
                versions_changes=["feat eq"],
                bump_type="minor",
            ),
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "land intent")
        head_before = _git(root, "rev-parse", "HEAD").stdout.strip()

        # A hallucinating custom-rules LLM that echoes the current version.
        def _echo_current(_prompt: str) -> str:
            return '{"final_version": "1.2.3"}'

        with pytest.raises(VersionRegressionError):
            reconcile(root, llm_call=_echo_current)

        # No release was published: version unchanged, intent NOT consumed, no
        # reconcile commit, HEAD untouched.
        assert read_current_version(root) == "1.2.3"
        assert not is_consumed(root, "flowEq")
        assert not reconcile_commit_exists(root, "flowEq")
        assert _git(root, "rev-parse", "HEAD").stdout.strip() == head_before

    def test_llm_returning_advancing_version_still_publishes(
        self, tmp_path: Path
    ) -> None:
        """A legitimate custom-rules advance is unaffected by the equal-guard."""
        from se3.engine.merge.reconcile import reconcile
        from se3.engine.version_intent import (
            VersionIntent,
            reconcile_commit_exists,
            write_intent,
        )

        root = _make_project(tmp_path, "1.2.3")
        self._write_rules(root)
        write_intent(
            root,
            VersionIntent(
                flow_id="flowAdv",
                change_summary="c",
                versions_changes=["feat adv"],
                bump_type="minor",
            ),
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "land intent")

        def _advance(_prompt: str) -> str:
            return '{"final_version": "1.3.0"}'

        result = reconcile(root, llm_call=_advance)

        assert result.success
        assert read_current_version(root) == "1.3.0"
        assert is_consumed(root, "flowAdv")
        assert reconcile_commit_exists(root, "flowAdv")


class TestNonHeadingTemplateHistoricalCollision:
    """The custom-rules collision guard must see a non-heading entry template."""

    def test_reused_non_heading_version_is_rejected(self, tmp_path: Path) -> None:
        """A custom, non-heading ``versions_entry_template`` still guards reuse.

        Divergence guarded (self-check high fix): with
        ``documentation.versions_entry_template: "ENTRY {{version}} | {{changes}}"``
        (no ``#`` heading), ``_header_regex_from_template`` used to fall back to the
        markdown-heading regex and collect NO historical versions, so
        ``validate_no_regression`` let the custom-rules LLM channel reuse an
        already-released version. The guard must recognise the non-heading entry and
        reject reuse of ``2026.07.07``.
        """
        import pytest

        from se3.engine.merge.reconcile import (
            VersionRegressionError,
            historical_versions,
            reconcile,
        )
        from se3.engine.version_intent import (
            VersionIntent,
            reconcile_commit_exists,
            write_intent,
        )

        # A date-versioned project: current version 2026.07.06, a non-heading
        # changelog whose already-released entry is 2026.07.07.
        root = tmp_path / "proj"
        root.mkdir()
        (root / "pyproject.toml").write_text(
            PYPROJECT_TEMPLATE.format(version="2026.07.06"), encoding="utf-8"
        )
        (root / "VERSIONS.md").write_text(
            "# History\n\nENTRY 2026.07.07 | - previous\n", encoding="utf-8"
        )
        (root / "README.md").write_text("# Demo\n", encoding="utf-8")
        (root / "se3.yaml").write_text(
            "documentation:\n"
            '  versions_entry_template: "ENTRY {{version}} | {{changes}}\\n"\n',
            encoding="utf-8",
        )
        (root / "se3").mkdir()
        (root / "se3" / "version-rules.md").write_text(
            "# Version Rules\n\nUse the current date as the version.\n",
            encoding="utf-8",
        )
        _git(root, "init", "-q")
        _git(root, "config", "user.email", "t@example.com")
        _git(root, "config", "user.name", "Test")
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "baseline")

        # The non-heading released version is now visible to the collision guard.
        assert historical_versions(root) == {"2026.07.07"}

        write_intent(
            root,
            VersionIntent(
                flow_id="flowDate",
                change_summary="c",
                versions_changes=["feat date"],
                bump_type="minor",
            ),
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "land intent")

        # A hallucinating LLM that reuses the already-released 2026.07.07.
        def _reuse(_prompt: str) -> str:
            return '{"final_version": "2026.07.07"}'

        with pytest.raises(VersionRegressionError):
            reconcile(root, llm_call=_reuse)

        # Nothing published: the reused version was rejected, no reconcile commit.
        assert read_current_version(root) == "2026.07.06"
        assert not is_consumed(root, "flowDate")
        assert not reconcile_commit_exists(root, "flowDate")


class TestNoBumpCommittedConsumedStillCommits:
    """A no-bump session whose consumed flag is already committed must still land
    the durable reconcile commit (self-check medium fix)."""

    def test_no_bump_with_committed_consumed_creates_reconcile_commit(
        self, tmp_path: Path
    ) -> None:
        """A committed ``consumed=true`` + ``bump_type=none`` must NOT silently
        complete without a reconcile commit.

        Divergence guarded: a prior bad/manual commit swept
        ``se3/version-intents/flowNone.json`` (consumed=true) into HEAD with NO
        reconcile trailer, and the intent declares ``bump_type: "none"``. The
        no-bump path writes no version/doc change and ``mark_consumed`` is a no-op,
        so a diff-gated commit produced NOTHING — the session stayed permanently
        outstanding while the run reported success. The reconcile commit is the sole
        durable idempotency signal, so an empty (trailer-only) commit must land.
        """
        from se3.engine.merge.reconcile import reconcile
        from se3.engine.version_intent import (
            VersionIntent,
            reconcile_commit_exists,
            write_intent,
        )

        root = _make_project(tmp_path, "1.0.0")
        write_intent(
            root,
            VersionIntent(
                flow_id="flowNone",
                change_summary="docs only",
                versions_changes=[],
                bump_type="none",
                consumed=True,
            ),
        )
        _git(root, "add", "-A")
        _git(root, "commit", "-q", "-m", "unrelated: land staged consumed intent")
        assert not reconcile_commit_exists(root, "flowNone")

        result = reconcile(root)

        assert result.success
        # No bump: version untouched, no fabricated changelog entry.
        assert read_current_version(root) == "1.0.0"
        # The durable idempotency signal now exists despite the no-op content.
        assert reconcile_commit_exists(root, "flowNone")
        assert result.reconcile_commit is not None

        # A resume sees the commit and no-ops — no double work, still consistent.
        second = reconcile(root)
        assert second.success
        assert second.already_reconciled
        assert read_current_version(root) == "1.0.0"
