"""Tests for the merge-side ``reconcile()`` core (G3).

Covers the deterministic SemVer channel, the custom-rules LLM channel (stubbed),
every branch of the no-regression validation, idempotent re-entry, and the
no-op-merge path where reconcile still runs but has nothing to apply.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tianluo.engine.merge.reconcile import (
    ReconcileError,
    ReconcileResult,
    VersionRegressionError,
    compute_deterministic,
    historical_versions,
    read_current_version,
    reconcile,
    validate_no_regression,
)
from tianluo.engine.git_tags import VersionTagError
from tianluo.engine.models import FlowInstance, Step, StepStatus, StepType
from tianluo.engine.steps.version_reconcile import version_reconcile_handler
from tianluo.engine.version_bumper import BumpType
from tianluo.engine.version_intent import (
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


def _tag_body(root: Path, tag_name: str) -> str:
    content = _git(root, "cat-file", "-p", tag_name).stdout
    _, _, body = content.partition("\n\n")
    return body


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
    assert result.is_tag is True
    assert result.tag_name == "v1.3.0"
    assert result.tag_created is True
    assert read_current_version(root) == "1.3.0"
    assert _git(root, "cat-file", "-t", "v1.3.0").stdout.strip() == "tag"
    assert (
        _git(root, "rev-list", "-n", "1", "v1.3.0").stdout.strip()
        == result.reconcile_commit
    )
    assert (
        _tag_body(root, "v1.3.0")
        == "chore: reconcile version to 1.3.0 at merge\n"
    )


def test_deterministic_no_bump_no_substance_leaves_version_unchanged(tmp_path):
    # A merge whose every intent declared no bump AND carried no changelog substance
    # (truly non-versionable work) must NOT fabricate a phantom patch release — the
    # no-regress rule permits final == current when no bump was declared and there is
    # nothing to file. The version stays put and no VERSIONS.md release block is written.
    root = _make_project(tmp_path, "2.0.0")
    _put_intent(root, "flowX", bump_type=None, versions_changes=[])

    result = reconcile(root)

    assert result.success
    assert result.final_version == "2.0.0"
    assert result.bump_type is None
    assert read_current_version(root) == "2.0.0"
    versions = (root / "VERSIONS.md").read_text(encoding="utf-8")
    assert "## 2.0.1" not in versions
    # The intent is still consumed and a durable reconcile commit is created so a
    # resume never recomputes the no-bump decision.
    assert result.consumed_flow_ids == ["flowX"]
    assert result.reconcile_commit is not None


def test_deterministic_changelog_substance_without_bump_hint_forces_patch(tmp_path):
    # Regression guard: an intent with changelog substance but no bump hint (an LLM
    # inconsistency version_analyze can emit) must NOT be consumed while filing nothing
    # — that silently drops the changelog note (and a later resume skips it because the
    # reconcile commit now exists). A changelog bullet IS a versionable change, so
    # reconcile applies a PATCH and files the bullet under the new released version.
    root = _make_project(tmp_path, "2.0.0")
    _put_intent(
        root, "flowX", bump_type=None,
        versions_changes=["Document new CLI behavior"],
    )

    result = reconcile(root)

    assert result.success
    assert result.channel == "deterministic"
    assert result.final_version == "2.0.1"
    assert result.bump_type == "patch"
    assert result.is_tag is False
    assert result.tag_name is None
    assert result.tag_created is False
    assert read_current_version(root) == "2.0.1"
    versions = (root / "VERSIONS.md").read_text(encoding="utf-8")
    assert "## 2.0.1" in versions
    assert "Document new CLI behavior" in versions
    assert result.consumed_flow_ids == ["flowX"]
    assert result.reconcile_commit is not None


def test_compute_deterministic_pure():
    final, bump = compute_deterministic(
        "1.0.0",
        [VersionIntent(flow_id="f", bump_type="major")],
    )
    assert final == "2.0.0"
    assert bump is BumpType.MAJOR


def test_deterministic_major_creates_tag(tmp_path):
    root = _make_project(tmp_path, "1.2.3")
    _put_intent(root, "flowA", bump_type="major", versions_changes=["break api"])

    result = reconcile(root)

    assert result.final_version == "2.0.0"
    assert result.is_tag is True
    assert result.tag_name == "v2.0.0"
    assert result.tag_created is True
    assert (
        _git(root, "rev-list", "-n", "1", "v2.0.0").stdout.strip()
        == result.reconcile_commit
    )


def test_deterministic_patch_does_not_create_tag(tmp_path):
    root = _make_project(tmp_path, "1.2.3")
    _put_intent(root, "flowA", bump_type="patch", versions_changes=["fix"])

    result = reconcile(root)

    assert result.final_version == "1.2.4"
    assert result.is_tag is False
    assert result.tag_name is None
    assert result.tag_created is False
    assert _git(root, "tag", "--list", "v1.2.4").stdout.strip() == ""


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


def test_operator_edit_survives_residue_reset_timeout(tmp_path, monkeypatch):
    # Issue 3: the detach + crash-residue reset happen inside the try/finally that
    # owns the operator-edit reattach. A git stall during the residue-reset window
    # (after README was already detached to HEAD) must still reach the reattach —
    # otherwise the operator's uncommitted README edit is silently lost.
    import sys

    # The name ``tianluo.engine.merge.reconcile`` is shadowed by the re-exported
    # reconcile() function on the package, so fetch the real submodule from
    # sys.modules (imported at module top) to monkeypatch its _run_git global.
    rec_mod = sys.modules["tianluo.engine.merge.reconcile"]
    from tianluo.engine.version_intent import (
        VERSION_INTENT_DIR_RELPATH,
        mark_consumed,
    )

    root = _make_project(tmp_path, "1.0.0")
    # A tracked README (reconcile-owned path with a HEAD blob to detach against).
    (root / "README.md").write_text("# Demo\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "add readme")

    _put_intent(root, "flowA", bump_type="minor", versions_changes=["feat"])
    # Residue from a crashed prior reconcile: consumed flag committed but NO
    # reconcile commit — this triggers the version-intents residue-reset path.
    mark_consumed(root, "flowA")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "intent + consumed residue")

    # Operator's uncommitted edit on a reconcile-owned path.
    (root / "README.md").write_text("# Demo\noperator WIP\n", encoding="utf-8")

    real_run_git = rec_mod._run_git
    stalled = {"fired": False}

    def flaky(project_root, *args, **kwargs):
        # Stall the FIRST residue-reset checkout of the version-intents dir (which
        # runs after README has been detached to HEAD), modelling a single transient
        # git stall under contention; later calls (including the except-path
        # rollback) proceed so reconcile surfaces a clean ReconcileError.
        if (
            not stalled["fired"]
            and "checkout" in args
            and VERSION_INTENT_DIR_RELPATH in args
        ):
            stalled["fired"] = True
            raise subprocess.TimeoutExpired(cmd="git checkout", timeout=15)
        return real_run_git(project_root, *args, **kwargs)

    monkeypatch.setattr(rec_mod, "_run_git", flaky)

    with pytest.raises(ReconcileError):
        reconcile(root)

    # The reattach finally replayed the operator's edit despite the timeout.
    assert "operator WIP" in (root / "README.md").read_text(encoding="utf-8")


def test_reattach_git_timeout_surfaces_as_reconcile_error(tmp_path, monkeypatch):
    """Issue 2: a git stall INSIDE the finally-block reattach (``git merge-file``
    under lock contention) must surface as a typed ReconcileError, not a raw
    TimeoutExpired escaping reconcile() untyped — that would break run_merge's
    typed-failure recovery contract and mask any in-flight error."""
    import sys

    rec_mod = sys.modules["tianluo.engine.merge.reconcile"]

    root = _make_project(tmp_path, "1.0.0")
    (root / "README.md").write_text("# Demo\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "add readme")

    _put_intent(root, "flowA", bump_type="minor", versions_changes=["feat"])
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "intent")

    # Operator's uncommitted edit on a reconcile-owned path -> a reattach snapshot
    # whose replay routes through the 3-way merge (``git merge-file``).
    (root / "README.md").write_text("# Demo\noperator WIP\n", encoding="utf-8")

    real_run_git = rec_mod._run_git

    def flaky(project_root, *args, **kwargs):
        if "merge-file" in args:
            raise subprocess.TimeoutExpired(cmd="git merge-file", timeout=15)
        return real_run_git(project_root, *args, **kwargs)

    monkeypatch.setattr(rec_mod, "_run_git", flaky)

    # Typed failure, not a raw TimeoutExpired.
    with pytest.raises(ReconcileError):
        reconcile(root)


def test_prepass_git_timeout_surfaces_as_reconcile_error(tmp_path, monkeypatch):
    """Issue 2: a git stall in the read-only replayability PRE-PASS (which runs
    BEFORE the try/finally) must also be mapped to a typed ReconcileError rather
    than escaping reconcile() untyped."""
    import sys

    rec_mod = sys.modules["tianluo.engine.merge.reconcile"]

    root = _make_project(tmp_path, "1.0.0")
    _put_intent(root, "flowA", bump_type="minor", versions_changes=["feat"])
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "intent")

    def boom(project_root, rel):
        raise subprocess.TimeoutExpired(cmd="git status", timeout=15)

    monkeypatch.setattr(rec_mod, "_assert_staged_state_replayable", boom)

    with pytest.raises(ReconcileError):
        reconcile(root)


def test_empty_changelog_still_recorded_in_versions_md(tmp_path):
    # A reconcile whose intents carry NO changelog bullets must still record the
    # released number in VERSIONS.md (issue 9): historical_versions() is parsed
    # from VERSIONS.md headers and is the anti-collision guard's source of truth.
    # A version that reaches the version file but never lands a header could be
    # re-approved for reuse later.
    root = _make_project(tmp_path, "1.0.0")
    _put_intent(root, "flowA", bump_type="minor", versions_changes=[])

    result = reconcile(root)

    assert result.success
    assert result.final_version == "1.1.0"
    versions = (root / "VERSIONS.md").read_text(encoding="utf-8")
    assert "## 1.1.0" in versions
    # The released number is now in the historical set the guard consults.
    assert "1.1.0" in historical_versions(root)


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
        return (
            '{"final_version": "2026.07.06", "is_tag": true, '
            '"reasoning": "date rule"}'
        )

    result = reconcile(root, llm_call=fake_llm)

    assert result.success
    assert result.channel == "custom-rules"
    assert result.final_version == "2026.07.06"
    assert result.is_tag is True
    assert result.tag_name == "v2026.07.06"
    assert result.tag_created is True
    # The change substance (not the bump_type) is what anchors the prompt.
    assert "feat" in captured["prompt"]
    assert "Use date-style versions." in captured["prompt"]


def test_custom_rules_is_tag_false_does_not_create_tag(tmp_path):
    root = _make_project(tmp_path, "1.2.3")
    (root / "se3").mkdir(exist_ok=True)
    (root / "se3" / "version-rules.md").write_text(
        "Use date-style versions.", encoding="utf-8"
    )
    _put_intent(root, "flowA", bump_type="minor", versions_changes=["feat"])

    result = reconcile(
        root,
        llm_call=lambda _p: (
            '{"final_version": "2026.07.07", "is_tag": false, '
            '"reasoning": "date rule"}'
        ),
    )

    assert result.success
    assert result.channel == "custom-rules"
    assert result.is_tag is False
    assert result.tag_name is None
    assert result.tag_created is False
    assert _git(root, "tag", "--list", "v2026.07.07").stdout.strip() == ""


def test_tag_failure_raises_reconcile_error(tmp_path, monkeypatch):
    import sys

    rec_mod = sys.modules["tianluo.engine.merge.reconcile"]
    root = _make_project(tmp_path, "1.2.3")
    _put_intent(root, "flowA", bump_type="minor", versions_changes=["feat"])

    def fail_tag(project_root, version, commit):
        raise VersionTagError(f"v{version}", "tag failed")

    monkeypatch.setattr(rec_mod, "create_annotated_version_tag", fail_tag)

    with pytest.raises(ReconcileError, match="failed to create version tag v1.3.0"):
        reconcile(root)


def test_noop_after_tag_failure_does_not_create_missing_tag(tmp_path, monkeypatch):
    """Recovery from a tag failure is manual; a later no-op merge must not tag.

    The reconcile commit is already durable and consumed, so a re-run has no
    outstanding intents. Scanning history to finish that release's tag is exactly
    the self-healing this flow must not do.
    """
    import sys

    rec_mod = sys.modules["tianluo.engine.merge.reconcile"]
    root = _make_project(tmp_path, "1.2.3")
    _put_intent(root, "flowA", bump_type="minor", versions_changes=["feat"])

    def fail_tag(project_root, version, commit):
        raise VersionTagError(f"v{version}", "tag failed", commit=commit)

    monkeypatch.setattr(rec_mod, "create_annotated_version_tag", fail_tag)
    with pytest.raises(ReconcileError, match="failed to create version tag v1.3.0"):
        reconcile(root)
    reconcile_commit = _git(root, "rev-parse", "HEAD").stdout.strip()
    assert _git(root, "tag", "--list", "v1.3.0").stdout.strip() == ""

    monkeypatch.undo()
    result = reconcile(root)

    assert result.success
    assert result.channel == "noop"
    assert result.already_reconciled
    assert result.tag_created is False
    assert result.final_version is None
    assert _git(root, "tag", "--list", "v1.3.0").stdout.strip() == ""
    assert _git(root, "rev-parse", "HEAD").stdout.strip() == reconcile_commit


def test_tag_failure_error_names_tag_and_reconcile_commit(tmp_path, monkeypatch):
    """An operator must be able to hand-create the tag from the error alone."""
    import sys

    rec_mod = sys.modules["tianluo.engine.merge.reconcile"]
    root = _make_project(tmp_path, "1.2.3")
    _put_intent(root, "flowA", bump_type="minor", versions_changes=["feat"])

    real_tag = rec_mod.create_annotated_version_tag
    seen: dict = {}

    def fail_tag(project_root, version, commit):
        seen["commit"] = commit
        raise VersionTagError(
            f"v{version}", "git command failed", commit=commit, returncode=128
        )

    monkeypatch.setattr(rec_mod, "create_annotated_version_tag", fail_tag)
    with pytest.raises(ReconcileError) as excinfo:
        reconcile(root)

    message = str(excinfo.value)
    assert "v1.3.0" in message
    assert seen["commit"] in message
    assert "exit 128" in message
    assert real_tag is not None


def test_reconcile_commit_hash_read_failure_raises(tmp_path, monkeypatch):
    """A created reconcile commit whose hash cannot be read must fail loud.

    Otherwise the release would be reported successful with its tag silently
    skipped (the tag is created on that very hash).
    """
    import sys

    rec_mod = sys.modules["tianluo.engine.merge.reconcile"]
    root = _make_project(tmp_path, "1.2.3")
    _put_intent(root, "flowA", bump_type="minor", versions_changes=["feat"])

    real_run_git = rec_mod._run_git
    failed_once = False

    def flaky_run_git(project_root, *args, **kwargs):
        # Only the post-commit hash read fails; the rollback path's own rev-parse
        # must still work so the test observes the real failure, not a cascade.
        nonlocal failed_once
        if not failed_once and args[:2] == ("rev-parse", "HEAD"):
            failed_once = True
            return subprocess.CompletedProcess(
                ["git", *args], 128, stdout="", stderr="fatal: bad revision"
            )
        return real_run_git(project_root, *args, **kwargs)

    monkeypatch.setattr(rec_mod, "_run_git", flaky_run_git)

    with pytest.raises(ReconcileError, match="hash could not be read"):
        reconcile(root)

    monkeypatch.undo()
    assert _git(root, "tag", "--list", "v1.3.0").stdout.strip() == ""


def test_no_tag_bump_survives_unreadable_reconcile_hash(tmp_path, monkeypatch):
    """A patch bump owes no tag, so an unreadable hash must not fail the merge.

    The sha's only consumer is the tag block; without a tag to create there is
    nothing lost, and failing here would abort an otherwise complete merge.
    """
    import sys

    rec_mod = sys.modules["tianluo.engine.merge.reconcile"]
    root = _make_project(tmp_path, "1.2.3")
    _put_intent(root, "flowA", bump_type="patch", versions_changes=["fix"])

    real_run_git = rec_mod._run_git
    failed_once = False

    def flaky_run_git(project_root, *args, **kwargs):
        nonlocal failed_once
        if not failed_once and args[:2] == ("rev-parse", "HEAD"):
            failed_once = True
            return subprocess.CompletedProcess(
                ["git", *args], 128, stdout="", stderr="fatal: bad revision"
            )
        return real_run_git(project_root, *args, **kwargs)

    monkeypatch.setattr(rec_mod, "_run_git", flaky_run_git)
    result = reconcile(root)
    monkeypatch.undo()

    assert result.final_version == "1.2.4"
    assert result.is_tag is False
    assert result.tag_created is False
    assert result.reconcile_commit is None
    assert _git(root, "tag", "--list", "v1.2.4").stdout.strip() == ""


def test_unscoped_noop_does_not_resurrect_deleted_historical_tag(tmp_path):
    """A no-op reconcile must not re-tag an older release the operator untagged."""
    root = _make_project(tmp_path, "1.2.3")

    _put_intent(root, "flowA", bump_type="minor", versions_changes=["feat"])
    first = reconcile(root)
    assert first.tag_name == "v1.3.0"

    _put_intent(root, "flowB", bump_type="minor", versions_changes=["feat b"])
    second = reconcile(root)
    assert second.tag_name == "v1.4.0"

    # Operator deliberately drops the older tag to re-cut that release.
    _git(root, "tag", "-d", "v1.3.0")

    third = reconcile(root)

    assert third.success
    assert third.channel == "noop"
    assert third.already_reconciled
    assert third.final_version is None
    assert third.tag_created is False
    assert _git(root, "tag", "--list", "v1.3.0").stdout.strip() == ""


def test_unscoped_noop_survives_historical_tag_collision(tmp_path):
    """An old release whose tag peels elsewhere must not break the no-op path."""
    root = _make_project(tmp_path, "1.2.3")
    baseline = _git(root, "rev-parse", "HEAD").stdout.strip()

    _put_intent(root, "flowA", bump_type="minor", versions_changes=["feat"])
    reconcile(root)
    _put_intent(root, "flowB", bump_type="minor", versions_changes=["feat b"])
    assert reconcile(root).tag_name == "v1.4.0"

    # Simulate a rewritten history: v1.3.0 now points at an unrelated commit.
    _git(root, "tag", "-d", "v1.3.0")
    _git(root, "tag", "-a", "v1.3.0", "-m", "rewritten", baseline)

    result = reconcile(root)

    assert result.success
    assert result.channel == "noop"
    assert result.already_reconciled


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


def test_validate_custom_rules_accepts_build_metadata_only_advance():
    # SemVer precedence ignores build metadata, so 1.0.0+b1 == 1.0.0+b2 by
    # Version.__eq__; under the custom-rules channel a build-number scheme's
    # advance is string-unequal and not historical, so it must be accepted rather
    # than rejected as "does not advance" (issue 5). Must not raise.
    validate_no_regression(
        "1.0.0+b1", "1.0.0+b2",
        declared_bump=True, custom_rules=True, historical={"1.0.0+b1"},
    )


def test_validate_default_channel_still_rejects_build_metadata_only():
    # The default SemVer channel has no ordering for a build-metadata-only change
    # (they compare equal), so it must remain a non-advance rejection.
    with pytest.raises(VersionRegressionError):
        validate_no_regression(
            "1.0.0+b1", "1.0.0+b2",
            declared_bump=True, custom_rules=False, historical=set(),
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
    assert result.is_tag is False
    assert result.tag_name is None
    assert result.tag_created is False
    assert read_current_version(root) == "3.1.4"


def test_version_reconcile_handler_outputs_tag_fields(tmp_path, monkeypatch):
    import tianluo.engine.merge as merge_pkg

    result = ReconcileResult(
        success=True,
        base_version="1.2.3",
        final_version="1.3.0",
        bump_type="minor",
        channel="deterministic",
        consumed_flow_ids=["flowA"],
        reconcile_commit="abc123",
        is_tag=True,
        tag_name="v1.3.0",
        tag_created=True,
    )

    monkeypatch.setattr(merge_pkg, "reconcile", lambda *args, **kwargs: result)

    step = Step(step_type=StepType.VERSION_RECONCILE, cwd=str(tmp_path))
    flow = FlowInstance(
        flow_id="flowA", task_description="feature", task_type="feature"
    )
    flow.state.selected_steps = [StepType.VERSION_ANALYZE, StepType.COMMIT]

    status = version_reconcile_handler(step, flow)

    assert status is StepStatus.COMPLETED
    assert step.outputs["is_tag"] is True
    assert step.outputs["tag_name"] == "v1.3.0"
    assert step.outputs["tag_created"] is True
    nested = step.outputs["reconcile_result"]
    assert nested["is_tag"] is True
    assert nested["tag_name"] == "v1.3.0"
    assert nested["tag_created"] is True


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


# --- script mode (version script rewrites pyproject.toml) --------------------
#
# Regression coverage for the worktree merge-reconcile + script-mode defect:
# when a version SCRIPT (se3/scripts/version.py) rewrites pyproject.toml,
# detect_version_file returns the SCRIPT path — so the file the script actually
# bumps was never in the commit pathspec and the bump leaked out as working-tree
# dirt (Specom flow 20260716-105509_043642a0). reconcile now measures the
# script-written set empirically and folds it into both the commit pathspec and
# the detach/reattach protection.

# A minimal, real version script: get/set the ``[project].version`` in
# pyproject.toml. VersionScriptRunner runs a ``.py`` script via the current
# interpreter with cwd=project_root, so no shebang/chmod is needed and
# "pyproject.toml" resolves against the project root.
VERSION_SCRIPT = '''\
import re
import sys
from pathlib import Path

PYPROJECT = Path("pyproject.toml")
PATTERN = re.compile(r'^version = "([^"]+)"', re.MULTILINE)


def _read():
    m = PATTERN.search(PYPROJECT.read_text(encoding="utf-8"))
    if not m:
        sys.exit("no version in pyproject.toml")
    return m.group(1)


def _write(value):
    text = PATTERN.sub(f'version = "{value}"', PYPROJECT.read_text(encoding="utf-8"), count=1)
    PYPROJECT.write_text(text, encoding="utf-8")


def main(argv):
    if argv and argv[0] == "get":
        print(_read())
    elif len(argv) >= 3 and argv[0] == "set" and argv[1] == "--version":
        _write(argv[2])
        print(argv[2])
    else:
        sys.exit(f"usage: version.py <get|set --version X>; got {argv!r}")


if __name__ == "__main__":
    main(sys.argv[1:])
'''


def _make_script_project(tmp_path: Path, version: str = "0.31.1") -> Path:
    """A git project whose version lives in pyproject.toml, bumped via a script.

    The presence of se3/scripts/version.py auto-activates script mode
    (DEFAULT_SCRIPT_PATHS), so detect_version_file returns the script — the exact
    condition under which the reconcile bump used to leak out of the commit.
    """
    root = tmp_path / "proj"
    root.mkdir()
    (root / "pyproject.toml").write_text(
        PYPROJECT_TEMPLATE.format(version=version), encoding="utf-8"
    )
    (root / "VERSIONS.md").write_text(
        VERSIONS_TEMPLATE.format(version=version), encoding="utf-8"
    )
    scripts_dir = root / "se3" / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "version.py").write_text(VERSION_SCRIPT, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "baseline")
    return root


def test_script_project_fixture_activates_script_mode(tmp_path):
    # Anchor for the whole script-mode regression suite: if the fixture ever
    # silently degraded to file mode (e.g. se3/scripts/version.py drops off the
    # DEFAULT_SCRIPT_PATHS list), detect_version_file would return pyproject.toml
    # directly and the leak this suite guards against could never reproduce — the
    # tests below would pass for the wrong reason. Assert the fixture really trips
    # script mode and that the script's own get/set round-trips pyproject.toml.
    import sys

    from tianluo.engine.version_script_interface import find_version_script

    reconcile_mod = sys.modules["tianluo.engine.merge.reconcile"]

    root = _make_script_project(tmp_path, "0.31.1")

    assert find_version_script(root) == root / "se3" / "scripts" / "version.py"

    # Build the bumper exactly as reconcile does (project-scoped config) so the
    # detection this asserts matches the code path under test.
    bumper = reconcile_mod._version_bumper(root)
    detected = bumper.detect_version_file(root)
    # script mode returns the SCRIPT path (the exact condition behind the defect),
    # not the pyproject.toml the script rewrites.
    assert detected == root / "se3" / "scripts" / "version.py"
    assert bumper._use_script_mode is True

    # The symmetric read side (delegated to the script's ``get``) sees the file.
    assert bumper.read_version() == "0.31.1"
    # And the write side (delegated to ``set``) rewrites pyproject.toml in place.
    bumper.set_version("0.31.2")
    assert 'version = "0.31.2"' in (root / "pyproject.toml").read_text(
        encoding="utf-8"
    )


def test_script_mode_bump_lands_in_reconcile_commit(tmp_path):
    # (a) + (b): the script-written pyproject.toml bump is IN the reconcile commit
    # and NOT left as working-tree dirt.
    root = _make_script_project(tmp_path, "0.31.1")
    _put_intent(root, "flowScript", bump_type="patch", versions_changes=["fix x"])

    result = reconcile(root)

    assert result.success
    assert result.channel == "deterministic"
    assert result.final_version == "0.31.2"
    # (a) the version bump is recorded in the reconcile commit itself.
    raw = _git(
        root, "show", "--raw", "--format=", result.reconcile_commit
    ).stdout
    assert "pyproject.toml" in raw
    committed = _git(
        root, "show", f"{result.reconcile_commit}:pyproject.toml"
    ).stdout
    assert 'version = "0.31.2"' in committed
    # (b) nothing is left behind as an uncommitted version-file change.
    assert (
        _git(root, "status", "--porcelain", "--", "pyproject.toml").stdout.strip()
        == ""
    )
    assert read_current_version(root) == "0.31.2"


def test_script_mode_operator_dirt_on_version_file_preserved(tmp_path):
    # (e): the version file ALSO carries an operator's unrelated uncommitted edit.
    # detach/reattach must neutralize it before the bump (so the base reads clean
    # and the bump lands in the commit) and replay it afterwards (so it is not
    # lost) — the script-mode autodetect in _reconcile_owned_relpaths is what puts
    # pyproject.toml under detach protection even though detect_version_file
    # returned the script.
    root = _make_script_project(tmp_path, "0.31.1")
    pyproject = root / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text(encoding="utf-8") + 'description = "operator wip"\n',
        encoding="utf-8",
    )
    _put_intent(root, "flowScript", bump_type="patch", versions_changes=["fix x"])

    result = reconcile(root)

    assert result.success
    assert result.final_version == "0.31.2"
    # The bump landed in the commit WITHOUT the operator's unrelated edit.
    committed = _git(
        root, "show", f"{result.reconcile_commit}:pyproject.toml"
    ).stdout
    assert 'version = "0.31.2"' in committed
    assert "operator wip" not in committed
    # The operator's unrelated edit survives in the working tree (reattached) and
    # so does the committed bump.
    final = pyproject.read_text(encoding="utf-8")
    assert 'description = "operator wip"' in final
    assert 'version = "0.31.2"' in final


# --- fail-loud verification (G2: git layer + semantic layer) -----------------
#
# Two trigger-path-agnostic assertions run AFTER _commit_reconcile so a version
# bump that never reached the commit (the defect) — or a commit whose version
# value is wrong — can never be reported as a silent success.


def test_fail_loud_when_version_file_missed_by_commit(tmp_path):
    # (c): primary (git-layer) check. Force the script-written pyproject.toml OUT
    # of the commit pathspec (as the original defect did) by dropping the measured
    # written_set; the bump then stays as working-tree dirt and reconcile must
    # raise rather than report success.
    import sys

    reconcile_mod = sys.modules["tianluo.engine.merge.reconcile"]

    root = _make_script_project(tmp_path, "0.31.1")
    _put_intent(root, "flowScript", bump_type="patch", versions_changes=["fix x"])

    real_commit = reconcile_mod._commit_reconcile

    def drop_written_set(project_root, message, **kwargs):
        # Simulate the pre-fix pathspec that never staged the script-written file.
        kwargs["extra_version_paths"] = None
        return real_commit(project_root, message, **kwargs)

    monkeypatch_target = "_commit_reconcile"
    orig = getattr(reconcile_mod, monkeypatch_target)
    setattr(reconcile_mod, monkeypatch_target, drop_written_set)
    try:
        with pytest.raises(ReconcileError) as excinfo:
            reconcile(root)
    finally:
        setattr(reconcile_mod, monkeypatch_target, orig)

    msg = str(excinfo.value)
    # Names the stray version file and gives an actionable recovery hint.
    assert "pyproject.toml" in msg
    assert "did NOT land" in msg or "uncommitted" in msg


def test_fail_loud_when_committed_version_value_wrong(tmp_path):
    # (d): secondary (semantic-layer) check. The file lands in the commit but the
    # readback value != final_version. Stub the read-side so ONLY the post-commit
    # readback (which sees the freshly-bumped 0.31.2) returns a wrong value; the
    # base read (which sees 0.31.1) is untouched. reconcile must raise.
    import sys

    reconcile_mod = sys.modules["tianluo.engine.merge.reconcile"]

    root = _make_script_project(tmp_path, "0.31.1")
    _put_intent(root, "flowScript", bump_type="patch", versions_changes=["fix x"])

    real_read = reconcile_mod.read_current_version

    def wrong_readback(project_root):
        v = real_read(project_root)
        # Only the post-commit readback observes the bumped version; corrupt it so
        # the symmetric read-side assertion trips without disturbing the base read.
        return "9.9.9" if v == "0.31.2" else v

    monkeypatch_target = "read_current_version"
    orig = getattr(reconcile_mod, monkeypatch_target)
    setattr(reconcile_mod, monkeypatch_target, wrong_readback)
    try:
        with pytest.raises(ReconcileError) as excinfo:
            reconcile(root)
    finally:
        setattr(reconcile_mod, monkeypatch_target, orig)

    msg = str(excinfo.value)
    assert "0.31.2" in msg
    assert "mismatch" in msg or "9.9.9" in msg


def test_fail_loud_checks_skipped_on_noop(tmp_path):
    # The no-bump no-op path (no versionable change) must NOT trip the fail-loud
    # checks — there is no new number to verify. A docs-only intent with no bump
    # hint and no changelog substance settles final == current with no publish.
    root = _make_script_project(tmp_path, "0.31.1")
    _put_intent(root, "flowNoop", bump_type="none", versions_changes=[])

    result = reconcile(root)

    assert result.success
    assert result.final_version == "0.31.1"
    # Version file untouched, working tree clean — the checks did not fire/mutate.
    assert read_current_version(root) == "0.31.1"
    assert (
        _git(root, "status", "--porcelain", "--", "pyproject.toml").stdout.strip()
        == ""
    )


def test_fail_loud_when_written_set_measures_empty(tmp_path):
    # (e): the vacuous-pass hole. On a genuine publish, if written_set comes back
    # EMPTY (a diff bracket that cancelled out, or a script observed to touch
    # nothing), the git-layer clean-check would be SKIPPED and the semantic
    # readback would see the still-uncommitted working-tree bump and match — a
    # silent success with the bump absent from the commit. reconcile must instead
    # raise, treating an unobserved write as unverifiable.
    import sys

    reconcile_mod = sys.modules["tianluo.engine.merge.reconcile"]

    root = _make_script_project(tmp_path, "0.31.1")
    _put_intent(root, "flowScript", bump_type="patch", versions_changes=["fix x"])

    real_write = reconcile_mod._write_final_version

    def blank_written_set(project_root, final_version):
        # Perform the real write (so the working tree carries the bumped value the
        # semantic readback would otherwise vacuously accept) but report an empty
        # measured set, exactly as a cancelled diff bracket would.
        version_file, _written = real_write(project_root, final_version)
        return version_file, []

    orig = reconcile_mod._write_final_version
    reconcile_mod._write_final_version = blank_written_set
    try:
        with pytest.raises(ReconcileError) as excinfo:
            reconcile(root)
    finally:
        reconcile_mod._write_final_version = orig

    msg = str(excinfo.value)
    assert "0.31.2" in msg
    assert "no file change was observed" in msg or "unobserved" in msg


def test_written_set_diff_measurement_fault_raises(tmp_path):
    # (Fix 1/3): a git fault while measuring the before/after diff bracket must
    # RAISE, not degrade to the empty set. A degraded-empty AFTER snapshot drops
    # the bump out of the commit pathspec; a degraded-empty BEFORE snapshot would
    # expand written_set to the operator's whole dirty set. Both are silent-wrong,
    # so _dirty_tracked_relpaths aborts loudly.
    import sys

    reconcile_mod = sys.modules["tianluo.engine.merge.reconcile"]

    root = _make_script_project(tmp_path, "0.31.1")

    real_run_git = reconcile_mod._run_git

    def failing_diff(project_root, *args, **kwargs):
        if args[:2] == ("diff", "--name-only"):
            return subprocess.CompletedProcess(
                args=["git", *args], returncode=128, stdout="", stderr="git error"
            )
        return real_run_git(project_root, *args, **kwargs)

    orig = reconcile_mod._run_git
    reconcile_mod._run_git = failing_diff
    try:
        with pytest.raises(ReconcileError) as excinfo:
            reconcile_mod._write_final_version(root, "0.31.2")
    finally:
        reconcile_mod._run_git = orig

    assert "could not measure" in str(excinfo.value)


# A version script whose backing file has a NON-ASCII name. With git's default
# core.quotePath=true a bare `git diff --name-only` would render this path as a
# quoted octal-escaped string; the fixture exists to prove the reconcile write
# measurement is quoting-proof (`-z`) so the bump still lands and the fail-loud
# checks are not vacuously satisfied by an unmatched pathspec.
VERSION_SCRIPT_UNICODE = '''\
import sys
from pathlib import Path

VERSION_FILE = Path("se3") / "版本.txt"


def main(argv):
    if argv and argv[0] == "get":
        print(VERSION_FILE.read_text(encoding="utf-8").strip())
    elif len(argv) >= 3 and argv[0] == "set" and argv[1] == "--version":
        VERSION_FILE.write_text(argv[2] + "\\n", encoding="utf-8")
        print(argv[2])
    else:
        sys.exit(f"usage: version.py <get|set --version X>; got {argv!r}")


if __name__ == "__main__":
    main(sys.argv[1:])
'''


def _make_unicode_script_project(tmp_path: Path, version: str = "0.31.1") -> Path:
    """A script-mode project whose version lives in a non-ASCII-named file.

    The version script rewrites ``se3/版本.txt`` (tracked, so the reconcile diff
    bracket observes it). Reproduces the quotePath encoding path where a bare
    `git diff --name-only` would emit a quoted octal-escaped pseudo-path.
    """
    root = tmp_path / "proj"
    root.mkdir()
    (root / "VERSIONS.md").write_text(
        VERSIONS_TEMPLATE.format(version=version), encoding="utf-8"
    )
    scripts_dir = root / "se3" / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "version.py").write_text(
        VERSION_SCRIPT_UNICODE, encoding="utf-8"
    )
    (root / "se3" / "版本.txt").write_text(
        version + "\n", encoding="utf-8"
    )
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "baseline")
    return root


def test_script_mode_bump_lands_when_version_file_is_non_ascii(tmp_path):
    # (Fix 1/3): the script rewrites a non-ASCII-named version file. Under the
    # pre-fix `git diff --name-only` measurement, core.quotePath would emit the
    # path as a quoted octal-escaped string that the commit pathspec cannot stage
    # and `git status --porcelain -- <that string>` matches nothing (vacuous
    # pass) — a silent success with the bump left as working-tree dirt. The `-z`
    # measurement yields the real path, so the bump lands and the working tree is
    # clean. Left dirty (or dropped from the commit), _assert_version_bump_committed
    # would raise; reaching success here proves the encoding path is closed.
    root = _make_unicode_script_project(tmp_path, "0.31.1")
    version_relpath = "se3/版本.txt"
    _put_intent(root, "flowUnicode", bump_type="patch", versions_changes=["fix x"])

    result = reconcile(root)

    assert result.success
    assert result.final_version == "0.31.2"
    # The non-ASCII version file's bump is recorded in the reconcile commit.
    raw = _git(
        root, "show", "--raw", "--format=", result.reconcile_commit
    ).stdout
    assert "版本" in raw or "\\347\\211\\210" in raw
    committed = _git(
        root, "show", f"{result.reconcile_commit}:{version_relpath}"
    ).stdout
    assert committed.strip() == "0.31.2"
    # Nothing left behind as an uncommitted change on the version file.
    assert (
        _git(
            root, "status", "--porcelain", "--", version_relpath
        ).stdout.strip()
        == ""
    )
