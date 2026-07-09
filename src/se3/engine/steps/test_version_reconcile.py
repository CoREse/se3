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
    ReconcileResult,
    VersionRegressionError,
    compute_deterministic,
    historical_versions,
    read_current_version,
    reconcile,
    validate_no_regression,
)
from se3.engine.git_tags import VersionTagError
from se3.engine.models import FlowInstance, Step, StepStatus, StepType
from se3.engine.steps.version_reconcile import version_reconcile_handler
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

    # The name ``se3.engine.merge.reconcile`` is shadowed by the re-exported
    # reconcile() function on the package, so fetch the real submodule from
    # sys.modules (imported at module top) to monkeypatch its _run_git global.
    rec_mod = sys.modules["se3.engine.merge.reconcile"]
    from se3.engine.version_intent import (
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

    rec_mod = sys.modules["se3.engine.merge.reconcile"]

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

    rec_mod = sys.modules["se3.engine.merge.reconcile"]

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

    rec_mod = sys.modules["se3.engine.merge.reconcile"]
    root = _make_project(tmp_path, "1.2.3")
    _put_intent(root, "flowA", bump_type="minor", versions_changes=["feat"])

    def fail_tag(project_root, version, commit):
        raise VersionTagError(f"v{version}", "tag failed")

    monkeypatch.setattr(rec_mod, "create_annotated_version_tag", fail_tag)

    with pytest.raises(ReconcileError, match="failed to create version tag v1.3.0"):
        reconcile(root)


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
    import se3.engine.merge as merge_pkg

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
