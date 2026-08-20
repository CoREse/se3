from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from tianluo.engine.review_scope import (
    ReviewBaseline,
    ReviewScopeManager,
    SelfCheckRoundController,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return result.stdout


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    _git(root, "init")
    _git(root, "config", "user.email", "review@example.com")
    _git(root, "config", "user.name", "Review Test")
    (root / ".gitignore").write_text("/tianluo/state/\n", encoding="utf-8")
    (root / "clean.py").write_text("value = 1\n", encoding="utf-8")
    (root / "dirty.py").write_text("before = 1\n", encoding="utf-8")
    (root / "deleted.py").write_text("keep = True\n", encoding="utf-8")
    (root / "rename_me.py").write_text("def renamed():\n    return 1\n", encoding="utf-8")
    (root / "binary.bin").write_bytes(b"\x00old\xff")
    if hasattr(os, "symlink"):
        os.symlink("clean.py", root / "link.py")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial")
    return root


def test_baseline_descriptor_round_trip_and_runtime_blob(tmp_path):
    root = _repo(tmp_path)
    (root / "dirty.py").write_text("before = 2\n", encoding="utf-8")
    (root / "preexisting.txt").write_bytes(b"user\x00content")
    status_before = _git(root, "status", "--porcelain=v1")

    manager = ReviewScopeManager(root, "flow-1")
    baseline = manager.capture("implementation")

    assert baseline.available is True
    assert baseline.head_commit == _git(root, "rev-parse", "HEAD").strip()
    assert baseline.tracked["clean.py"]["storage"] == "git"
    assert baseline.tracked["dirty.py"]["storage"] == "blob"
    assert baseline.untracked["preexisting.txt"]["storage"] == "blob"
    assert ReviewBaseline.from_dict(
        json.loads(json.dumps(baseline.to_dict()))
    ) == baseline

    blob = (
        manager.root
        / baseline.baseline_id
        / "blobs"
        / baseline.untracked["preexisting.txt"]["blob_sha256"]
    )
    assert blob.read_bytes() == b"user\x00content"
    assert not _git(root, "status", "--short", "--", "tianluo/state").strip()
    assert _git(root, "status", "--porcelain=v1") == status_before


def test_full_diff_excludes_preexisting_dirty(tmp_path):
    root = _repo(tmp_path)
    (root / "dirty.py").write_text("before = 2\n", encoding="utf-8")
    (root / "preexisting.txt").write_text("user work\n", encoding="utf-8")
    (root / "deleted.py").unlink()
    manager = ReviewScopeManager(root, "flow-2")
    baseline = manager.capture("implementation")

    unchanged = manager.reconstruct("full", baseline)
    assert unchanged.undecidable is False
    assert unchanged.changed_paths == []
    assert unchanged.unified_diff == ""

    (root / "clean.py").write_text("value = 1\nflow = 2\n", encoding="utf-8")
    (root / "dirty.py").write_text("before = 2\nflow = 3\n", encoding="utf-8")

    scope = manager.reconstruct("full", baseline)
    assert scope.undecidable is False
    assert scope.changed_paths == ["clean.py", "dirty.py"]
    assert "+flow = 2" in scope.unified_diff
    assert "+flow = 3" in scope.unified_diff
    assert "+before = 2" not in scope.unified_diff
    assert "preexisting.txt" not in scope.unified_diff
    assert "deleted.py" not in scope.unified_diff
    assert Path(scope.artifact_path).read_text(encoding="utf-8") == scope.unified_diff
    assert scope.causal_anchors["clean.py"]


def test_head_advanced_by_flow_commits_still_reconstructs(tmp_path):
    # The planned IMPLEMENT path merges every DAG leaf branch back onto the
    # working branch, so HEAD routinely advances past the implementation
    # baseline. The baseline manifest is content-keyed, so the diff is still
    # exactly reconstructable — a descendant HEAD must NOT blank the scope.
    root = _repo(tmp_path)
    manager = ReviewScopeManager(root, "flow-head-advanced")
    baseline = manager.capture("implementation")

    (root / "feature.py").write_text("group_one = 1\n", encoding="utf-8")
    _git(root, "add", "feature.py")
    _git(root, "commit", "-m", "impl: group G1")
    (root / "clean.py").write_text("value = 1\ngroup_two = 2\n", encoding="utf-8")
    _git(root, "add", "clean.py")
    _git(root, "commit", "-m", "impl: group G2")
    # Uncommitted work on top of the flow's own commits belongs to the scope too.
    (root / "dirty.py").write_text("before = 1\nlate_fix = 3\n", encoding="utf-8")

    scope = manager.reconstruct("full", baseline)
    assert scope.undecidable is False
    assert scope.changed_paths == ["clean.py", "dirty.py", "feature.py"]
    assert "+group_one = 1" in scope.unified_diff
    assert "+group_two = 2" in scope.unified_diff
    assert "+late_fix = 3" in scope.unified_diff
    assert scope.causal_anchors["feature.py"]
    assert scope.causal_anchors["clean.py"]


def test_head_rewritten_off_the_baseline_is_undecidable(tmp_path):
    # History that no longer contains the baseline commit (rebase, amend, a
    # backwards reset) makes the change set unattributable; that — and only
    # that — degrades to the safe fallback instead of diffing blindly.
    root = _repo(tmp_path)
    (root / "later.py").write_text("later = 1\n", encoding="utf-8")
    _git(root, "add", "later.py")
    _git(root, "commit", "-m", "second")

    manager = ReviewScopeManager(root, "flow-head-rewritten")
    baseline = manager.capture("implementation")

    _git(root, "commit", "--amend", "-m", "second (rewritten)")

    scope = manager.reconstruct("full", baseline)
    assert scope.undecidable is True
    assert scope.changed_paths == []
    assert scope.unified_diff == ""
    assert "no longer descends" in scope.diagnostic


def test_unrelated_head_relation_failure_degrades_to_undecidable(tmp_path):
    root = _repo(tmp_path)
    manager = ReviewScopeManager(root, "flow-head-unknown")
    baseline = manager.capture("implementation")
    (root / "docs.py").write_text("more = 1\n", encoding="utf-8")
    _git(root, "add", "docs.py")
    _git(root, "commit", "-m", "flow commit")

    with patch.object(manager, "_is_ancestor", return_value=None):
        scope = manager.reconstruct("full", baseline)
    assert scope.undecidable is True
    assert scope.unified_diff == ""
    assert "could not relate" in scope.diagnostic


def test_dirty_deleted_renamed_symlink_binary_and_untracked_restore_on_resume(tmp_path):
    root = _repo(tmp_path)
    (root / "dirty.py").write_text("pre-existing edit\n", encoding="utf-8")
    (root / "deleted.py").unlink()
    _git(root, "mv", "rename_me.py", "already_renamed.py")
    (root / "binary.bin").write_bytes(b"\x00pre-existing\xfe")
    (root / "scratch.bin").write_bytes(b"\x00scratch\xff")
    if (root / "link.py").is_symlink():
        (root / "link.py").unlink()
        os.symlink("dirty.py", root / "link.py")

    manager = ReviewScopeManager(root, "flow-resume")
    baseline = manager.capture("implementation")
    restored = ReviewBaseline.from_dict(
        json.loads(json.dumps(baseline.to_dict()))
    )
    resumed_manager = ReviewScopeManager(root, "flow-resume")
    scope = resumed_manager.reconstruct("full", restored)

    assert baseline.available is True
    assert baseline.tracked["dirty.py"]["storage"] == "blob"
    assert baseline.tracked["deleted.py"]["storage"] == "missing"
    assert baseline.tracked["already_renamed.py"]["storage"] == "blob"
    assert baseline.tracked["binary.bin"]["storage"] == "blob"
    if "link.py" in baseline.tracked:
        assert baseline.tracked["link.py"]["kind"] == "symlink"
        assert baseline.tracked["link.py"]["storage"] == "blob"
    assert baseline.untracked["scratch.bin"]["storage"] == "blob"
    assert scope.undecidable is False
    assert scope.changed_paths == []


def test_diff_covers_rename_symlink_binary_and_untracked(tmp_path):
    root = _repo(tmp_path)
    manager = ReviewScopeManager(root, "flow-3")
    baseline = manager.capture("implementation")

    _git(root, "mv", "rename_me.py", "renamed.py")
    (root / "binary.bin").write_bytes(b"\x00new\xfe")
    (root / "new.txt").write_text("new file\n", encoding="utf-8")
    if (root / "link.py").is_symlink():
        (root / "link.py").unlink()
        os.symlink("dirty.py", root / "link.py")

    scope = manager.reconstruct("full", baseline)
    assert scope.undecidable is False
    assert {"rename_me.py", "renamed.py", "binary.bin", "new.txt"}.issubset(
        scope.changed_paths
    )
    assert "rename from rename_me.py" in scope.unified_diff
    assert "rename to renamed.py" in scope.unified_diff
    assert "Binary files a/binary.bin and b/binary.bin differ" in scope.unified_diff
    assert "new.txt" in scope.unified_diff
    if (root / "link.py").is_symlink():
        assert "link.py" in scope.changed_paths
        assert "+dirty.py" in scope.unified_diff


def test_corrupt_blob_and_repository_identity_are_undecidable(tmp_path):
    root = _repo(tmp_path)
    (root / "dirty.py").write_text("dirty baseline\n", encoding="utf-8")
    manager = ReviewScopeManager(root, "flow-4")
    baseline = manager.capture("fix-1")
    blob_name = baseline.tracked["dirty.py"]["blob_sha256"]
    (manager.root / baseline.baseline_id / "blobs" / blob_name).write_bytes(b"corrupt")

    corrupt = manager.reconstruct("incremental", baseline)
    assert corrupt.undecidable is True
    assert corrupt.changed_paths == []
    assert "integrity" in corrupt.diagnostic

    # Restore through a new capture, then simulate the same path being attached
    # to a different git repository identity.
    baseline = manager.capture("fix-2")
    with patch.object(
        manager,
        "_repository_identity",
        return_value=("different", ""),
    ):
        moved = manager.reconstruct("incremental", baseline)
    assert moved.undecidable is True
    assert "identity changed" in moved.diagnostic


def test_incremental_undecidable_falls_back_to_reconstructable_full(tmp_path):
    root = _repo(tmp_path)
    manager = ReviewScopeManager(root, "flow-5")
    implementation = manager.capture("implementation")
    (root / "clean.py").write_text("value = 2\n", encoding="utf-8")
    fix = manager.capture("fix-1")
    fix_blob = fix.tracked["clean.py"]["blob_sha256"]
    (manager.root / fix.baseline_id / "blobs" / fix_blob).write_bytes(b"broken")
    (root / "clean.py").write_text("value = 3\n", encoding="utf-8")

    scope = manager.resolve(
        "incremental", fix, full_baseline=implementation,
    )
    assert scope.scope_mode == "full"
    assert scope.requested_mode == "incremental"
    assert scope.fallback_from_incremental is True
    assert scope.undecidable is False
    assert scope.baseline_id == implementation.baseline_id
    assert scope.changed_paths == ["clean.py"]
    assert "+value = 3" in scope.unified_diff


def test_missing_or_corrupt_descriptor_never_becomes_empty_diff(tmp_path):
    root = _repo(tmp_path)
    manager = ReviewScopeManager(root, "flow-6")
    baseline = manager.capture("implementation")
    descriptor = manager.root / baseline.baseline_id / "descriptor.json"
    descriptor.write_text("{broken", encoding="utf-8")

    scope = manager.reconstruct("full", baseline)
    assert scope.undecidable is True
    assert scope.unified_diff == ""
    assert "descriptor" in scope.diagnostic


def test_deleted_code_line_goes_to_deletion_anchor_space(tmp_path):
    root = _repo(tmp_path)
    manager = ReviewScopeManager(root, "flow-delete-anchor")
    baseline = manager.capture("fix-1")
    (root / "clean.py").write_text("", encoding="utf-8")

    scope = manager.reconstruct("incremental", baseline)

    assert scope.changed_paths == ["clean.py"]
    # Deleted lines are old-side numbers: they must never validate a ``path:N``
    # evidence citation (which names current-file lines), so they land in the
    # separate deletion space while the causal space stays empty.
    assert scope.causal_anchors == {}
    assert scope.deletion_anchors["clean.py"] == [[1, 1]]


def test_deleted_and_added_lines_stay_in_separate_anchor_spaces(tmp_path):
    root = _repo(tmp_path)
    manager = ReviewScopeManager(root, "flow-anchor-spaces")
    baseline = manager.capture("fix-1")
    (root / "clean.py").write_text(
        "replacement = 1\n"
        "extra = 2\n",
        encoding="utf-8",
    )

    scope = manager.reconstruct("incremental", baseline)

    assert scope.changed_paths == ["clean.py"]
    # Line 1 was deleted and replaced by two added lines. Both spaces record
    # line 1, but a ``path:N`` citation may only resolve against the
    # current-file (new-side) space — the old-side number must never validate
    # as a current-file line.
    assert scope.causal_anchors["clean.py"] == [[1, 2]]
    assert scope.deletion_anchors["clean.py"] == [[1, 1]]


def _repo_with_submodule(tmp_path: Path) -> Path:
    sub = tmp_path / "sub"
    sub.mkdir()
    _git(sub, "init")
    _git(sub, "config", "user.email", "sub@example.com")
    _git(sub, "config", "user.name", "Sub Test")
    (sub / "inner.py").write_text("x = 1\n", encoding="utf-8")
    (sub / "other.py").write_text("keep = 1\n", encoding="utf-8")
    _git(sub, "add", "-A")
    _git(sub, "commit", "-m", "sub initial")

    root = _repo(tmp_path)
    _git(
        root,
        "-c", "protocol.file.allow=always",
        "submodule", "add", str(sub), "vendor",
    )
    _git(root, "commit", "-m", "add submodule")
    return root


def test_submodule_commit_without_staging_gitlink_is_in_scope(tmp_path):
    root = _repo_with_submodule(tmp_path)
    manager = ReviewScopeManager(root, "flow-sub-commit")
    baseline = manager.capture("implementation")
    assert baseline.tracked["vendor"]["kind"] == "gitlink"
    assert baseline.tracked["vendor"]["storage"] == "git"

    unchanged = manager.reconstruct("full", baseline)
    assert unchanged.changed_paths == []

    # The flow edits and commits INSIDE the submodule without staging the
    # superproject gitlink — the superproject index object id is unchanged.
    (root / "vendor" / "inner.py").write_text("x = 1\ny = 2\n", encoding="utf-8")
    _git(root / "vendor", "add", "-A")
    _git(root / "vendor", "commit", "-m", "flow change inside submodule")

    scope = manager.reconstruct("full", baseline)
    assert scope.undecidable is False
    assert "vendor" in scope.changed_paths
    assert "Subproject commit" in scope.unified_diff
    assert "+y = 2" in scope.unified_diff
    # Inner-file causal anchors must be keyed by the INNER path the hunks
    # render — a finding citing ``vendor/inner.py:N`` anchors for real, and
    # the bare submodule path is never anchored against inner line numbers.
    assert "vendor" not in scope.causal_anchors
    assert "vendor/inner.py" in scope.causal_anchors
    assert any(
        start <= 2 <= end
        for start, end in scope.causal_anchors["vendor/inner.py"]
    )


def test_submodule_worktree_edit_without_commit_is_in_scope(tmp_path):
    root = _repo_with_submodule(tmp_path)
    manager = ReviewScopeManager(root, "flow-sub-edit")
    baseline = manager.capture("implementation")

    (root / "vendor" / "inner.py").write_text("x = 1\nflow = 2\n", encoding="utf-8")

    scope = manager.reconstruct("full", baseline)
    assert scope.undecidable is False
    assert "vendor" in scope.changed_paths
    assert "+flow = 2" in scope.unified_diff
    # Same inner-path anchor contract as the commit case above.
    assert "vendor" not in scope.causal_anchors
    assert "vendor/inner.py" in scope.causal_anchors
    assert any(
        start <= 2 <= end
        for start, end in scope.causal_anchors["vendor/inner.py"]
    )


def test_preexisting_dirty_submodule_excluded_and_flow_edit_isolated(tmp_path):
    root = _repo_with_submodule(tmp_path)
    # Pre-existing user work inside the submodule, before the flow starts.
    (root / "vendor" / "inner.py").write_text("x = 1\npreflow = 1\n", encoding="utf-8")

    manager = ReviewScopeManager(root, "flow-sub-dirty")
    baseline = manager.capture("implementation")
    assert baseline.tracked["vendor"]["storage"] == "blob"

    unchanged = manager.reconstruct("full", baseline)
    assert unchanged.changed_paths == []

    # The flow edits the SAME pre-dirty file: only the flow's own delta is
    # reported; the pre-existing edit stays excluded.
    (root / "vendor" / "inner.py").write_text(
        "x = 1\npreflow = 1\nflow = 2\n", encoding="utf-8"
    )
    scope = manager.reconstruct("full", baseline)
    # The gitlink AND the inner path the diff actually rendered are both
    # citable changed paths (the inner one carries its own anchors).
    assert scope.changed_paths == ["vendor", "vendor/inner.py"]
    assert "+flow = 2" in scope.unified_diff
    assert "+preflow = 1" not in scope.unified_diff

    # A clean-at-capture file edited by the flow is reported too.
    (root / "vendor" / "other.py").write_text("keep = 2\n", encoding="utf-8")
    scope = manager.reconstruct("full", baseline)
    assert "+keep = 2" in scope.unified_diff


def test_submodule_inner_deletion_is_a_citable_changed_path(tmp_path):
    # A deletion-only inner file carries no current-side anchor, so it can only
    # ground at PATH level — which requires its rendered label to be in
    # changed_paths. Without it the finding is dropped as bad evidence and the
    # round can close clean over a real deletion.
    root = _repo_with_submodule(tmp_path)
    manager = ReviewScopeManager(root, "flow-sub-delete")
    baseline = manager.capture("implementation")

    (root / "vendor" / "inner.py").unlink()

    scope = manager.reconstruct("full", baseline)
    assert scope.undecidable is False
    assert "vendor/inner.py" in scope.changed_paths
    # Anchor-less: no current-side lines exist for a deleted file.
    assert not scope.causal_anchors.get("vendor/inner.py")


def test_submodule_unstaged_typechange_renders_an_inner_hunk(tmp_path):
    # An unstaged typechange leaves the submodule index sha AND mode untouched,
    # so only the worktree status column reports it. Missing it would render a
    # header-only gitlink diff with nothing to ground a finding on.
    if not hasattr(os, "symlink"):
        return
    sub = tmp_path / "sub2"
    sub.mkdir()
    _git(sub, "init")
    _git(sub, "config", "user.email", "sub@example.com")
    _git(sub, "config", "user.name", "Sub Test")
    (sub / "target.py").write_text("value = 1\n", encoding="utf-8")
    os.symlink("target.py", sub / "link.py")
    _git(sub, "add", "-A")
    _git(sub, "commit", "-m", "sub initial")

    root = _repo(tmp_path)
    _git(
        root,
        "-c", "protocol.file.allow=always",
        "submodule", "add", str(sub), "vendor",
    )
    _git(root, "commit", "-m", "add submodule")

    manager = ReviewScopeManager(root, "flow-sub-typechange")
    baseline = manager.capture("implementation")
    assert manager.reconstruct("full", baseline).changed_paths == []

    # Replace the symlink with a regular file, unstaged.
    (root / "vendor" / "link.py").unlink()
    (root / "vendor" / "link.py").write_text("replaced = True\n", encoding="utf-8")

    scope = manager.reconstruct("full", baseline)
    assert scope.undecidable is False
    assert "vendor/link.py" in scope.changed_paths
    assert "+replaced = True" in scope.unified_diff
    assert any(
        start <= 1 <= end
        for start, end in scope.causal_anchors.get("vendor/link.py", [])
    )


def test_gitignored_declared_path_stays_in_scope(tmp_path):
    # A git-ignored file the flow wrote is invisible to baseline capture
    # (``ls-files --others --exclude-standard``), so it can never appear in the
    # reconstructed diff. It must still be citable, or a finding on a real
    # change is discarded and the round completes clean.
    root = _repo(tmp_path)
    (root / ".gitignore").write_text(
        "/tianluo/state/\ngenerated/\n", encoding="utf-8"
    )
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "ignore generated")

    manager = ReviewScopeManager(root, "flow-ignored")
    baseline = manager.capture("implementation")

    (root / "generated").mkdir()
    (root / "generated" / "out.js").write_text("var x = 1;\n", encoding="utf-8")

    plain = manager.reconstruct("full", baseline)
    assert plain.changed_paths == []

    scope = manager.reconstruct(
        "full", baseline, declared_paths=["generated/out.js"]
    )
    assert scope.undecidable is False
    assert "generated/out.js" in scope.changed_paths
    assert "generated/out.js" in scope.unified_diff
    # Anchor-less by construction: no baseline content exists to diff against,
    # so grounding stands at path level and no line anchor is fabricated.
    assert not scope.causal_anchors.get("generated/out.js")


def test_declared_path_does_not_re_add_an_unchanged_tracked_file(tmp_path):
    # The reconstructed diff stays authoritative for everything git can see: a
    # declared path the baseline proves unchanged must NOT enter the scope.
    root = _repo(tmp_path)
    manager = ReviewScopeManager(root, "flow-declared-clean")
    baseline = manager.capture("implementation")

    scope = manager.reconstruct(
        "full", baseline, declared_paths=["clean.py", "does/not/exist.py"]
    )
    assert scope.changed_paths == []


def test_causal_ranges_classify_by_marker_not_content_prefix():
    # A deleted line whose text began with "--" (a YAML document separator)
    # renders as "----"; it must anchor the OLD-side line, not shift every
    # subsequent new-side number — and an added line beginning with "++"
    # renders "+++..." and must anchor the NEW side.
    diff = (
        "@@ -1,6 +1,6 @@\n"
        " context\n"
        "----\n"
        "+added\n"
        " context\n"
        "+++marker\n"
        "-deleted\n"
    )
    causal, deleted = ReviewScopeManager._causal_ranges(diff)
    assert causal == [[2, 2], [4, 4]]
    assert deleted == [[2, 2], [4, 4]]


def test_complete_clean_honors_persisted_pending_closure_marker():
    # A flow interrupted between the in-memory clean-incremental completion
    # and the closure step's save persists ``next_scope_mode`` with no active
    # round. The transition must still demand the full closure round.
    context = {"self_check_review": {"next_scope_mode": "full"}}
    controller = SelfCheckRoundController(context)
    assert controller.complete_clean() is True
    # The marker survives until the closure round actually starts.
    assert context["self_check_review"]["next_scope_mode"] == "full"

    active = controller.prepare_round(
        requirement_text="requirements",
        fix_iteration=1,
        passes_required=2,
        implementation_baseline=None,
        latest_fix_baseline=None,
    )
    assert active["scope_mode"] == "full"
    assert active["round_reason"] == "full_closure"
    assert "next_scope_mode" not in context["self_check_review"]

    # A clean full closure round routes onward — no further closure is due.
    assert controller.complete_clean() is False


def test_mid_round_full_fallback_does_not_credit_a_clean_full_round():
    # Pass #1 ran incremental over the fix delta; a later pass degraded to full
    # because reconstruction became undecidable. The round only ever
    # diff-reviewed the fix delta concretely, so it must NOT be credited as the
    # mandatory clean full round — the closure round is still owed.
    context = {
        "self_check_review": {
            "full_round_occurred": True,
            "active_round": {
                "round_id": "scr-mid",
                "scope_mode": "incremental",
                "round_scope_mode": "incremental",
                "baseline_id": "fix-1",
                "round_reason": "post_fix_incremental",
                "requirement_fingerprint": "fp",
                "fix_iteration": 1,
                "pass_index": 2,
                "passes_required": 3,
                "status": "active",
            },
        }
    }
    controller = SelfCheckRoundController(context)
    # The state machine's undecidable fallback rewrites the executing mode.
    controller.active_round["scope_mode"] = "full"
    controller.active_round["round_reason"] = "incremental_undecidable_full_fallback"

    assert controller.complete_clean() is True
    assert context["self_check_review"]["next_scope_mode"] == "full"
    assert context["self_check_review"].get("completed_full_rounds", 0) == 0


def test_pass_one_full_fallback_is_credited_as_a_full_round():
    # Degrading before any pass ran means the whole round genuinely reviewed
    # the full implementation diff; it counts.
    context = {
        "self_check_review": {
            "full_round_occurred": True,
            "active_round": {
                "round_id": "scr-p1",
                "scope_mode": "full",
                "round_scope_mode": "full",
                "baseline_id": "impl-1",
                "round_reason": "incremental_undecidable_full_fallback",
                "requirement_fingerprint": "fp",
                "fix_iteration": 1,
                "pass_index": 1,
                "passes_required": 1,
                "status": "active",
            },
        }
    }
    controller = SelfCheckRoundController(context)
    assert controller.complete_clean() is False
    assert context["self_check_review"]["completed_full_rounds"] == 1


def test_legacy_round_without_accounting_mode_uses_executing_mode():
    # Rounds persisted before ``round_scope_mode`` existed keep their recorded
    # accounting so a resume does not rewrite the path already underway.
    context = {
        "self_check_review": {
            "active_round": {
                "round_id": "scr-old",
                "scope_mode": "incremental",
                "pass_index": 1,
                "passes_required": 1,
                "status": "active",
            },
        }
    }
    controller = SelfCheckRoundController(context)
    assert controller.complete_clean() is True


def test_complete_clean_without_active_round_or_marker_advances():
    controller = SelfCheckRoundController(
        {"self_check_review": {}}
    )
    assert controller.complete_clean() is False


def test_constructing_a_controller_does_not_materialize_round_state():
    # Callers detect a pre-upgrade flow by probing ``self_check_review``.
    # Constructing (or read-probing) a controller must leave that key absent,
    # otherwise a resumed multi-pass chain loses its persisted pass index and
    # restarts at pass #1.
    context: dict = {}
    controller = SelfCheckRoundController(context)
    assert controller.active_round is None
    assert controller.requirements_changed("anything") is False
    assert controller.complete_clean() is False
    controller.advance_pass()
    controller.mark_findings()
    assert "self_check_review" not in context

    # The first genuine mutation publishes the state into the context.
    controller.prepare_round(
        requirement_text="requirements",
        fix_iteration=0,
        passes_required=2,
        implementation_baseline=None,
        latest_fix_baseline=None,
    )
    assert context["self_check_review"]["active_round"]["pass_index"] == 1


def test_force_full_publishes_state_on_a_fresh_context():
    context: dict = {}
    SelfCheckRoundController(context).force_full("effective_requirements_changed")
    assert context["self_check_review"]["force_full_reason"] == (
        "effective_requirements_changed"
    )


def test_plain_diff_terminates_missing_newline_lines_and_keeps_anchors():
    # A file whose pre-change last line lacks a trailing newline (common in
    # config/data files) must not glue the "-old" line to the next diff line:
    # the added line stays a distinct "+def" line and the new-side causal
    # anchors keep their true line numbers.
    manager = ReviewScopeManager(Path("/unused"), "flow-nl")
    rendered, ranges, deleted_ranges = manager._render_plain_file_diff(
        "data.txt",
        {"kind": "file", "content": b"abc"},
        {"kind": "file", "content": b"abc\ndef"},
    )
    assert "-abc+abc" not in rendered
    assert "-abc\n" in rendered
    assert "+def\n" in rendered
    assert ranges == [[1, 2]]
    assert deleted_ranges == [[1, 1]]


def test_incremental_scope_carries_whole_task_anchors(tmp_path):
    # An incremental round diffs from the latest fix baseline, but a finding
    # anchored in work an EARLIER implement/fix did is grounded in git fact.
    # resolve() must therefore hand self_check both anchor sets, distinguishable
    # by which baseline they came from.
    root = _repo(tmp_path)
    manager = ReviewScopeManager(root, "flow-task-union")
    implementation = manager.capture("implementation")
    (root / "clean.py").write_text("value = 1\nimpl = 2\n", encoding="utf-8")
    fix = manager.capture("fix-1")
    (root / "dirty.py").write_text("before = 1\nfix = 3\n", encoding="utf-8")

    scope = manager.resolve("incremental", fix, full_baseline=implementation)

    assert scope.scope_mode == "incremental"
    assert scope.undecidable is False
    # The round's own attention stays the fix delta.
    assert scope.changed_paths == ["dirty.py"]
    assert "clean.py" not in scope.causal_anchors
    assert "+fix = 3" in scope.unified_diff
    assert "+impl = 2" not in scope.unified_diff
    # The whole-task domain arrives alongside it, not merged into it.
    assert scope.task_scope_available is True
    assert scope.task_baseline_id == implementation.baseline_id
    assert scope.task_changed_paths == ["clean.py", "dirty.py"]
    assert scope.task_causal_anchors["clean.py"] == [[2, 2]]
    assert scope.task_causal_anchors["dirty.py"] == [[2, 2]]
    assert Path(scope.task_artifact_path).read_text(encoding="utf-8").count(
        "impl = 2"
    )


def test_full_scope_carries_no_separate_whole_task_domain(tmp_path):
    # A full round already diffs from the implementation baseline, so a second
    # copy of the same anchors would carry no information.
    root = _repo(tmp_path)
    manager = ReviewScopeManager(root, "flow-task-full")
    implementation = manager.capture("implementation")
    (root / "clean.py").write_text("value = 1\nimpl = 2\n", encoding="utf-8")

    scope = manager.resolve(
        "full", implementation, full_baseline=implementation,
    )

    assert scope.scope_mode == "full"
    assert scope.changed_paths == ["clean.py"]
    assert scope.task_scope_available is False
    assert scope.task_changed_paths == []
    assert scope.task_causal_anchors == {}
    assert scope.task_deletion_anchors == {}
    assert scope.task_baseline_id == ""


def test_incremental_scope_on_the_implementation_baseline_adds_no_second_domain(tmp_path):
    root = _repo(tmp_path)
    manager = ReviewScopeManager(root, "flow-task-same")
    implementation = manager.capture("implementation")
    (root / "clean.py").write_text("value = 1\nimpl = 2\n", encoding="utf-8")

    scope = manager.resolve(
        "incremental", implementation, full_baseline=implementation,
    )

    assert scope.undecidable is False
    assert scope.changed_paths == ["clean.py"]
    assert scope.task_scope_available is False
    assert scope.task_changed_paths == []


def test_undecidable_fallback_still_carries_no_whole_task_domain(tmp_path):
    # The safe fallback IS the implementation-baseline diff, so its own anchors
    # already are the whole-task anchors — the degraded round must otherwise
    # behave exactly as before.
    root = _repo(tmp_path)
    manager = ReviewScopeManager(root, "flow-task-fallback")
    implementation = manager.capture("implementation")
    (root / "clean.py").write_text("value = 2\n", encoding="utf-8")
    fix = manager.capture("fix-1")
    fix_blob = fix.tracked["clean.py"]["blob_sha256"]
    (manager.root / fix.baseline_id / "blobs" / fix_blob).write_bytes(b"broken")
    (root / "clean.py").write_text("value = 3\n", encoding="utf-8")

    scope = manager.resolve("incremental", fix, full_baseline=implementation)

    assert scope.scope_mode == "full"
    assert scope.fallback_from_incremental is True
    assert scope.undecidable is False
    assert scope.task_scope_available is False
    assert scope.task_changed_paths == []


def test_unrebuildable_whole_task_domain_never_degrades_the_round(tmp_path):
    # The whole-task domain only WIDENS what evidence can ground on, so losing
    # it must leave a perfectly usable incremental round intact.
    root = _repo(tmp_path)
    manager = ReviewScopeManager(root, "flow-task-broken")
    implementation = manager.capture("implementation")
    (root / "clean.py").write_text("value = 2\n", encoding="utf-8")
    fix = manager.capture("fix-1")
    (manager.root / implementation.baseline_id / "descriptor.json").write_text(
        "{broken", encoding="utf-8"
    )
    (root / "clean.py").write_text("value = 3\n", encoding="utf-8")

    scope = manager.resolve("incremental", fix, full_baseline=implementation)

    assert scope.scope_mode == "incremental"
    assert scope.undecidable is False
    assert scope.changed_paths == ["clean.py"]
    assert scope.task_scope_available is False
    assert "descriptor" in scope.task_scope_diagnostic

    without_baseline = manager.resolve("incremental", fix, full_baseline=None)
    assert without_baseline.undecidable is False
    assert without_baseline.task_scope_available is False
    assert "implementation baseline is missing" in (
        without_baseline.task_scope_diagnostic
    )


def test_whole_task_domain_keeps_deletion_anchors_in_their_own_space(tmp_path):
    root = _repo(tmp_path)
    manager = ReviewScopeManager(root, "flow-task-deletions")
    implementation = manager.capture("implementation")
    (root / "rename_me.py").write_text("def renamed():\n", encoding="utf-8")
    fix = manager.capture("fix-1")
    (root / "clean.py").write_text("value = 1\nlate = 9\n", encoding="utf-8")

    scope = manager.resolve("incremental", fix, full_baseline=implementation)

    assert scope.task_scope_available is True
    assert scope.task_deletion_anchors["rename_me.py"] == [[2, 2]]
    assert "rename_me.py" not in scope.task_causal_anchors


def test_full_scope_carries_the_fix_delta_since_the_last_full_round(tmp_path):
    # A full round's own diff spans the whole task, so it cannot say which
    # hunks the fixes since the previous full round added. The fix-delta
    # attachment answers exactly that — without narrowing the round.
    root = _repo(tmp_path)
    manager = ReviewScopeManager(root, "flow-full-delta")
    implementation = manager.capture("implementation")
    (root / "clean.py").write_text("value = 1\nimpl = 2\n", encoding="utf-8")
    fix = manager.capture("fix-1")
    (root / "dirty.py").write_text("before = 1\nfix = 3\n", encoding="utf-8")

    scope = manager.resolve(
        "full",
        implementation,
        full_baseline=implementation,
        fix_delta_baseline=fix,
    )

    assert scope.scope_mode == "full"
    assert scope.changed_paths == ["clean.py", "dirty.py"]
    assert scope.causal_anchors["clean.py"] == [[2, 2]]
    assert scope.fix_delta_available is True
    assert scope.fix_delta_baseline_id == fix.baseline_id
    assert scope.fix_delta_changed_paths == ["dirty.py"]
    assert scope.fix_delta_causal_anchors["dirty.py"] == [[2, 2]]
    assert "clean.py" not in scope.fix_delta_causal_anchors
    # Purely descriptive: it never adds to the flow's on-disk diff record.
    assert not list(
        (manager._baseline_dir(fix.baseline_id) / "diffs").glob("*.diff")
    )


def test_unrebuildable_fix_delta_never_degrades_a_full_round(tmp_path):
    root = _repo(tmp_path)
    manager = ReviewScopeManager(root, "flow-full-delta-broken")
    implementation = manager.capture("implementation")
    (root / "clean.py").write_text("value = 1\nimpl = 2\n", encoding="utf-8")
    broken = ReviewBaseline(
        baseline_id="fix-9-deadbeefdead",
        kind="fix-9",
        flow_id="flow-full-delta-broken",
        captured_at="2026-01-01T00:00:00",
        project_root=str(root),
        head_commit="0" * 40,
        repository_identity="not-this-repository",
        available=True,
    )

    scope = manager.resolve(
        "full",
        implementation,
        full_baseline=implementation,
        fix_delta_baseline=broken,
    )

    assert scope.undecidable is False
    assert scope.changed_paths == ["clean.py"]
    assert scope.fix_delta_available is False
    assert scope.fix_delta_changed_paths == []
    assert "could not be isolated" in scope.fix_delta_diagnostic


def test_fix_baseline_after_full_round_follows_the_persisted_marker():
    manager = ReviewScopeManager(Path("/nonexistent"), "flow-marker")
    context = {
        "fix_baseline_history": [
            {"baseline_id": "fix-1-aaaaaaaaaaaa"},
            {"baseline_id": "fix-2-bbbbbbbbbbbb"},
        ],
    }
    def _baseline(baseline_id: str, kind: str) -> ReviewBaseline:
        return ReviewBaseline(
            baseline_id=baseline_id,
            kind=kind,
            flow_id="flow-marker",
            captured_at="2026-01-01T00:00:00",
            project_root="/nonexistent",
        )

    loaded = {
        "fix-1-aaaaaaaaaaaa": _baseline("fix-1-aaaaaaaaaaaa", "fix-1"),
        "fix-2-bbbbbbbbbbbb": _baseline("fix-2-bbbbbbbbbbbb", "fix-2"),
    }
    with patch.object(
        ReviewScopeManager, "load_fix_baselines", return_value=loaded
    ):
        # No full round has consumed a fix yet: the whole history is new.
        first = manager.earliest_fix_baseline_after_full_round(context)
        assert first is not None
        assert first.baseline_id == "fix-1-aaaaaaaaaaaa"

        # After a full round that spanned fix-1, only fix-2 is new.
        context["full_round_fix_head"] = "fix-1-aaaaaaaaaaaa"
        second = manager.earliest_fix_baseline_after_full_round(context)
        assert second is not None
        assert second.baseline_id == "fix-2-bbbbbbbbbbbb"

        # Nothing captured since the last full round: no annotation at all.
        context["full_round_fix_head"] = "fix-2-bbbbbbbbbbbb"
        assert manager.earliest_fix_baseline_after_full_round(context) is None


def test_line_range_algebra_merges_adjacent_and_drops_unusable_pairs():
    from tianluo.engine.review_scope import (
        normalize_line_ranges,
        subtract_line_ranges,
        union_line_ranges,
    )

    assert normalize_line_ranges([[5, 3], ["a", 2], [1], [4, 6]]) == [[4, 6]]
    # Adjacent ranges from two baselines describe one contiguous block.
    assert union_line_ranges([[1, 3]], [[4, 6], [10, 10]]) == [[1, 6], [10, 10]]
    assert subtract_line_ranges([[1, 20]], [[5, 9], [15, 15]]) == [
        [1, 4], [10, 14], [16, 20],
    ]
    assert subtract_line_ranges([[1, 5]], [[1, 9]]) == []
    assert subtract_line_ranges([[1, 5]], None) == [[1, 5]]
