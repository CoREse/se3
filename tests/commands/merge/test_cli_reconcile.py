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
        both entries, not silently share a number and drop one.
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
