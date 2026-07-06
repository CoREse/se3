"""Tests for the merge-side ``reconcile()`` core (G3).

Covers the deterministic SemVer channel, the custom-rules LLM channel (stubbed),
every branch of the no-regression validation, idempotent re-entry, and the
no-op-merge path where reconcile still runs but has nothing to apply.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from se3.engine.merge.reconcile import (
    ReconcileError,
    VersionRegressionError,
    compute_deterministic,
    historical_versions,
    read_current_version,
    reconcile,
    validate_no_regression,
)
from se3.engine.version_bumper import BumpType
from se3.engine.version_intent import (
    VersionIntent,
    is_consumed,
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


def _make_project(tmp_path: Path, version: str = "1.2.3") -> Path:
    """Create a git-backed project with pyproject.toml + VERSIONS.md committed."""
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        PYPROJECT_TEMPLATE.format(version=version), encoding="utf-8"
    )
    (root / "VERSIONS.md").write_text(
        VERSIONS_TEMPLATE.format(version=version), encoding="utf-8"
    )
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "baseline")
    return root


def _put_intent(root: Path, flow_id: str, **kwargs) -> None:
    write_intent(root, VersionIntent(flow_id=flow_id, **kwargs))


# --- deterministic channel ---------------------------------------------------

def test_deterministic_max_bump_applied(tmp_path):
    root = _make_project(tmp_path, "1.2.3")
    _put_intent(root, "flowA", bump_type="patch", versions_changes=["fix a"])
    _put_intent(root, "flowB", bump_type="minor", versions_changes=["feat b"])

    result = reconcile(root)

    assert result.success
    assert result.channel == "deterministic"
    # max(patch, minor) = minor applied to 1.2.3 -> 1.3.0
    assert result.final_version == "1.3.0"
    assert result.bump_type == "minor"
    assert read_current_version(root) == "1.3.0"


def test_deterministic_no_bump_hint_defaults_patch(tmp_path):
    root = _make_project(tmp_path, "2.0.0")
    _put_intent(root, "flowX", bump_type=None, versions_changes=["misc"])

    result = reconcile(root)

    assert result.final_version == "2.0.1"
    assert result.bump_type == "patch"


def test_compute_deterministic_pure():
    final, bump = compute_deterministic(
        "1.0.0",
        [VersionIntent(flow_id="f", bump_type="major")],
    )
    assert final == "2.0.0"
    assert bump is BumpType.MAJOR


def test_changelog_entries_merged_under_final(tmp_path):
    root = _make_project(tmp_path, "1.0.0")
    _put_intent(root, "flowA", bump_type="minor", versions_changes=["feat a"])
    _put_intent(root, "flowB", bump_type="patch", versions_changes=["fix b"])

    result = reconcile(root)

    versions = (root / "VERSIONS.md").read_text(encoding="utf-8")
    assert f"## {result.final_version}" in versions
    # Both features' changelog bullets survive under the one final version.
    assert "feat a" in versions
    assert "fix b" in versions


# --- custom-rules LLM channel ------------------------------------------------

def test_custom_rules_channel_uses_llm_output(tmp_path):
    root = _make_project(tmp_path, "1.2.3")
    (root / "se3").mkdir(exist_ok=True)
    (root / "se3" / "version-rules.md").write_text(
        "Use date-style versions.", encoding="utf-8"
    )
    _put_intent(root, "flowA", bump_type="minor", versions_changes=["feat"])

    captured = {}

    def fake_llm(prompt: str) -> str:
        captured["prompt"] = prompt
        return '{"final_version": "2026.07.06", "reasoning": "date rule"}'

    result = reconcile(root, llm_call=fake_llm)

    assert result.success
    assert result.channel == "custom-rules"
    assert result.final_version == "2026.07.06"
    # The change substance (not the bump_type) is what anchors the prompt.
    assert "feat" in captured["prompt"]
    assert "Use date-style versions." in captured["prompt"]


def test_custom_rules_empty_response_raises(tmp_path):
    root = _make_project(tmp_path, "1.2.3")
    (root / "se3").mkdir(exist_ok=True)
    (root / "se3" / "version-rules.md").write_text("rules", encoding="utf-8")
    _put_intent(root, "flowA", bump_type="minor")

    with pytest.raises(ReconcileError):
        reconcile(root, llm_call=lambda _p: "")


# --- no-regression validation ------------------------------------------------

def test_validate_rejects_numeric_regression():
    with pytest.raises(VersionRegressionError):
        validate_no_regression(
            "2.0.0", "1.9.0",
            declared_bump=True, custom_rules=False, historical=set(),
        )


def test_validate_rejects_no_advance():
    with pytest.raises(VersionRegressionError):
        validate_no_regression(
            "2.0.0", "2.0.0",
            declared_bump=True, custom_rules=False, historical=set(),
        )


def test_validate_rejects_historical_collision():
    with pytest.raises(VersionRegressionError):
        validate_no_regression(
            "2.0.0", "1.5.0",
            declared_bump=False, custom_rules=True,
            historical={"1.5.0", "2.0.0"},
        )


def test_validate_rejects_nonsemver_under_default_channel():
    with pytest.raises(ReconcileError):
        validate_no_regression(
            "1.0.0", "not-a-version",
            declared_bump=True, custom_rules=False, historical=set(),
        )


def test_validate_accepts_forward_bump():
    # Should not raise.
    validate_no_regression(
        "1.2.3", "1.3.0",
        declared_bump=True, custom_rules=False, historical={"1.2.3"},
    )


def test_validate_custom_rules_rejects_unchanged():
    with pytest.raises(VersionRegressionError):
        validate_no_regression(
            "2026.07.05", "2026.07.05",
            declared_bump=False, custom_rules=True, historical=set(),
        )


def test_reconcile_rejects_llm_regression(tmp_path):
    root = _make_project(tmp_path, "5.0.0")
    (root / "se3").mkdir(exist_ok=True)
    (root / "se3" / "version-rules.md").write_text("rules", encoding="utf-8")
    _put_intent(root, "flowA", bump_type="minor")

    with pytest.raises(VersionRegressionError):
        reconcile(root, llm_call=lambda _p: '{"final_version": "4.0.0"}')


# --- idempotency / re-entry --------------------------------------------------

def test_reconcile_idempotent_reentry(tmp_path):
    root = _make_project(tmp_path, "1.0.0")
    _put_intent(root, "flowA", bump_type="minor", versions_changes=["feat"])

    first = reconcile(root)
    assert first.success
    assert first.final_version == "1.1.0"
    assert is_consumed(root, "flowA")

    # Second run: nothing outstanding -> no-op, version NOT double-bumped.
    second = reconcile(root)
    assert second.success
    assert second.already_reconciled
    assert second.channel == "noop"
    assert read_current_version(root) == "1.1.0"


def test_reconcile_commit_carries_trailer(tmp_path):
    root = _make_project(tmp_path, "1.0.0")
    _put_intent(root, "flowA", bump_type="patch", versions_changes=["fix"])

    result = reconcile(root)

    assert result.reconcile_commit is not None
    log = _git(root, "log", "-1", "--format=%B").stdout
    assert "Version-Reconcile-Session: flowA" in log


# --- no-op merge path --------------------------------------------------------

def test_reconcile_noop_when_no_intents(tmp_path):
    root = _make_project(tmp_path, "3.1.4")

    result = reconcile(root)

    assert result.success
    assert result.already_reconciled
    assert result.channel == "noop"
    assert result.base_version == "3.1.4"
    assert read_current_version(root) == "3.1.4"


def test_reconcile_runs_on_already_ancestor_shape(tmp_path):
    # Simulates the already-ancestor / no-op *git* merge: the branch produced
    # no merge commit, but its intent file is present in the tree. reconcile
    # must still compute and write a version (the old skip path is gone).
    root = _make_project(tmp_path, "1.0.0")
    _put_intent(root, "flowGhost", bump_type="minor", versions_changes=["feat"])

    result = reconcile(root)

    assert result.success
    assert result.channel == "deterministic"
    assert result.final_version == "1.1.0"
    assert read_current_version(root) == "1.1.0"


def test_historical_versions_parsed(tmp_path):
    root = _make_project(tmp_path, "1.2.3")
    assert "1.2.3" in historical_versions(root)
