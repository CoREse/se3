"""Read-only baseline lookup + diff-view helpers of ``ReviewScopeManager``.

These back the ``luo review-scope diff`` surface (tests/commands/
test_review_scope_cmd.py covers the CLI shell itself). What is guarded here:

- role-based baseline resolution (implementation / fix) against a real
  persisted store, including which concrete fix baseline the "fix" role picks;
- the four lookup statuses staying distinguishable — ok / not_captured /
  unavailable / cleaned — which is the contract the snapshot lifecycle aligns
  to;
- ``reconstruct(write_artifact=False)`` leaving the store untouched;
- the presentation helpers (section split, path containment, stat counts).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from tianluo.engine.review_scope import (
    BASELINE_STATUS_CLEANED,
    BASELINE_STATUS_NOT_CAPTURED,
    BASELINE_STATUS_OK,
    BASELINE_STATUS_UNAVAILABLE,
    DiffSection,
    ReviewScopeManager,
    count_anchor_lines,
    diff_stat,
    section_covers_path,
    split_diff_sections,
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
    (root / "alpha.py").write_text("value = 1\n", encoding="utf-8")
    (root / "beta.py").write_text("other = 1\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "initial")
    return root


def _scope_context(**kwargs):
    return {"review_scope": dict(kwargs)}


class TestBaselineLookup:
    def test_implementation_role_resolves_declared_baseline(self, tmp_path):
        root = _repo(tmp_path)
        manager = ReviewScopeManager(root, "flow-lookup-impl")
        baseline = manager.capture("implementation")

        lookup = manager.lookup_baseline(
            "implementation",
            _scope_context(implementation_baseline=baseline.to_dict()),
        )

        assert lookup.status == BASELINE_STATUS_OK
        assert lookup.ok is True
        assert lookup.baseline_id == baseline.baseline_id

    def test_fix_role_takes_the_active_round_baseline(self, tmp_path):
        """Once a round exists, every fix baseline is marked covered.

        The round's own baseline is then the only truthful answer to "which fix
        delta is being reviewed"; falling through to the uncovered-fix search
        would report nothing at exactly the moment a checker asks.
        """
        root = _repo(tmp_path)
        manager = ReviewScopeManager(root, "flow-lookup-round")
        implementation = manager.capture("implementation")
        fix_one = manager.capture("fix-1")
        fix_two = manager.capture("fix-2")

        context = _scope_context(
            implementation_baseline=implementation.to_dict(),
            latest_fix_baseline=fix_two.to_dict(),
            covered_fix_baseline=fix_two.baseline_id,
            fix_baseline_history=[
                {"fix_iteration": 1, "baseline_id": fix_one.baseline_id},
                {"fix_iteration": 2, "baseline_id": fix_two.baseline_id},
            ],
        )
        context["self_check_review"] = {
            "active_round": {
                "baseline_id": fix_one.baseline_id,
                "baseline_kind": "fix-1",
                "scope_mode": "incremental",
            }
        }

        lookup = manager.lookup_baseline("fix", context)

        assert lookup.baseline_id == fix_one.baseline_id

    def test_fix_role_falls_back_to_earliest_unreviewed(self, tmp_path):
        root = _repo(tmp_path)
        manager = ReviewScopeManager(root, "flow-lookup-earliest")
        fix_one = manager.capture("fix-1")
        fix_two = manager.capture("fix-2")

        lookup = manager.lookup_baseline(
            "fix",
            _scope_context(
                latest_fix_baseline=fix_two.to_dict(),
                fix_baseline_history=[
                    {"fix_iteration": 1, "baseline_id": fix_one.baseline_id},
                    {"fix_iteration": 2, "baseline_id": fix_two.baseline_id},
                ],
            ),
        )

        assert lookup.baseline_id == fix_one.baseline_id

    def test_full_round_does_not_hijack_the_fix_role(self, tmp_path):
        root = _repo(tmp_path)
        manager = ReviewScopeManager(root, "flow-lookup-fullround")
        implementation = manager.capture("implementation")
        fix_one = manager.capture("fix-1")
        context = _scope_context(
            implementation_baseline=implementation.to_dict(),
            latest_fix_baseline=fix_one.to_dict(),
        )
        context["self_check_review"] = {
            "active_round": {
                "baseline_id": implementation.baseline_id,
                "baseline_kind": "implementation",
                "scope_mode": "full",
            }
        }

        assert manager.lookup_baseline("fix", context).baseline_id == (
            fix_one.baseline_id
        )

    def test_missing_fix_baseline_is_not_captured(self, tmp_path):
        root = _repo(tmp_path)
        manager = ReviewScopeManager(root, "flow-lookup-nofix")
        implementation = manager.capture("implementation")

        lookup = manager.lookup_baseline(
            "fix", _scope_context(implementation_baseline=implementation.to_dict())
        )

        assert lookup.status == BASELINE_STATUS_NOT_CAPTURED
        assert lookup.ok is False

    def test_unavailable_capture_is_reported_with_its_diagnostic(self, tmp_path):
        root = _repo(tmp_path)
        manager = ReviewScopeManager(root, "flow-lookup-unavailable")
        baseline = manager.unavailable_baseline("implementation", "no git here")

        lookup = manager.lookup_baseline(
            "implementation",
            _scope_context(implementation_baseline=baseline.to_dict()),
        )

        assert lookup.status == BASELINE_STATUS_UNAVAILABLE
        assert "no git here" in lookup.diagnostic

    def test_reclaimed_store_is_distinguishable_from_never_captured(self, tmp_path):
        root = _repo(tmp_path)
        manager = ReviewScopeManager(root, "flow-lookup-cleaned")
        baseline = manager.capture("implementation")
        context = _scope_context(implementation_baseline=baseline.to_dict())
        shutil.rmtree(manager.root)

        lookup = manager.lookup_baseline("implementation", context)

        assert lookup.status == BASELINE_STATUS_CLEANED
        assert lookup.baseline_id == baseline.baseline_id
        assert manager.store_exists() is False

    def test_lost_flow_record_resolves_from_the_store(self, tmp_path):
        """No flow context left: the descriptors are self-describing."""
        root = _repo(tmp_path)
        manager = ReviewScopeManager(root, "flow-lookup-scan")
        implementation = manager.capture("implementation")
        fix = manager.capture("fix-1")

        assert manager.lookup_baseline("implementation", None).baseline_id == (
            implementation.baseline_id
        )
        assert manager.lookup_baseline("fix", None).baseline_id == fix.baseline_id

    def test_lost_flow_record_and_lost_store_reads_as_cleaned(self, tmp_path):
        root = _repo(tmp_path)
        manager = ReviewScopeManager(root, "flow-lookup-gone")
        manager.capture("implementation")
        shutil.rmtree(manager.root)

        assert manager.lookup_baseline("implementation", None).status == (
            BASELINE_STATUS_CLEANED
        )

    def test_list_baselines_is_ordered_and_empty_without_a_store(self, tmp_path):
        root = _repo(tmp_path)
        manager = ReviewScopeManager(root, "flow-lookup-list")
        assert manager.list_baselines() == []
        implementation = manager.capture("implementation")
        fix = manager.capture("fix-1")
        assert [item.baseline_id for item in manager.list_baselines()] == [
            implementation.baseline_id,
            fix.baseline_id,
        ]


class TestReadOnlyReconstruct:
    def test_write_artifact_false_leaves_no_trace(self, tmp_path):
        root = _repo(tmp_path)
        manager = ReviewScopeManager(root, "flow-readonly")
        baseline = manager.capture("implementation")
        (root / "alpha.py").write_text("value = 1\nadded = 2\n", encoding="utf-8")

        before = sorted(str(p) for p in manager.root.rglob("*"))
        scope = manager.reconstruct("full", baseline, write_artifact=False)
        after = sorted(str(p) for p in manager.root.rglob("*"))

        assert scope.undecidable is False
        assert "+added = 2" in scope.unified_diff
        assert scope.artifact_path == ""
        assert before == after

    def test_default_reconstruct_still_materializes_the_artifact(self, tmp_path):
        root = _repo(tmp_path)
        manager = ReviewScopeManager(root, "flow-artifact")
        baseline = manager.capture("implementation")
        (root / "alpha.py").write_text("value = 1\nadded = 2\n", encoding="utf-8")

        scope = manager.reconstruct("full", baseline)

        assert Path(scope.artifact_path).read_text(encoding="utf-8") == (
            scope.unified_diff
        )


class TestDiffViewHelpers:
    def test_sections_split_verbatim(self, tmp_path):
        root = _repo(tmp_path)
        manager = ReviewScopeManager(root, "flow-sections")
        baseline = manager.capture("implementation")
        (root / "alpha.py").write_text("value = 1\nadded = 2\n", encoding="utf-8")
        (root / "beta.py").write_text("other = 2\n", encoding="utf-8")
        scope = manager.reconstruct("full", baseline, write_artifact=False)

        sections = split_diff_sections(scope.unified_diff)

        assert [section.path for section in sections] == ["alpha.py", "beta.py"]
        assert "".join(section.text for section in sections) == scope.unified_diff

    def test_section_covers_inner_submodule_path(self):
        section = DiffSection(path="vendor", old_path="vendor", text="")
        assert section_covers_path(section, "vendor") is True
        assert section_covers_path(section, "vendor/inner.py") is True
        assert section_covers_path(section, "vendors/inner.py") is False
        assert section_covers_path(section, "") is False

    def test_rename_section_keeps_both_sides(self, tmp_path):
        root = _repo(tmp_path)
        manager = ReviewScopeManager(root, "flow-rename")
        baseline = manager.capture("implementation")
        (root / "alpha.py").rename(root / "gamma.py")
        scope = manager.reconstruct("full", baseline, write_artifact=False)

        sections = split_diff_sections(scope.unified_diff)
        renamed = [s for s in sections if s.old_path == "alpha.py"]
        assert renamed and renamed[0].path == "gamma.py"
        assert section_covers_path(renamed[0], "alpha.py") is True

    def test_stat_counts_come_from_the_anchors(self, tmp_path):
        root = _repo(tmp_path)
        manager = ReviewScopeManager(root, "flow-stat")
        baseline = manager.capture("implementation")
        (root / "alpha.py").write_text(
            "value = 1\nadded = 2\nadded = 3\n", encoding="utf-8"
        )
        (root / "beta.py").write_text("replaced = 9\n", encoding="utf-8")
        scope = manager.reconstruct("full", baseline, write_artifact=False)

        stat = diff_stat(scope)

        assert stat["alpha.py"] == (2, 0)
        assert stat["beta.py"] == (1, 1)

    def test_stat_keeps_anchorless_changed_paths(self, tmp_path):
        """A binary change has no line anchors but is still a changed file."""
        root = _repo(tmp_path)
        manager = ReviewScopeManager(root, "flow-stat-binary")
        (root / "blob.bin").write_bytes(b"\x00old")
        _git(root, "add", "-A")
        _git(root, "commit", "-m", "binary")
        baseline = manager.capture("implementation")
        (root / "blob.bin").write_bytes(b"\x00new\xff")
        scope = manager.reconstruct("full", baseline, write_artifact=False)

        assert diff_stat(scope)["blob.bin"] == (0, 0)

    def test_count_anchor_lines_tolerates_junk(self):
        assert count_anchor_lines(None) == 0
        assert count_anchor_lines([[3, 5], [9, 9]]) == 4
        assert count_anchor_lines([[5, 3]]) == 0
        assert count_anchor_lines([["x"], [1, 2]]) == 2
