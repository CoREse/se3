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
    normalize_scope_path,
    paths_related,
    section_covers_path,
    select_filtered_view,
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

    def test_failed_capture_without_a_descriptor_is_not_read_as_reclaimed(
        self, tmp_path
    ):
        """A capture that raised writes no descriptor at all.

        The state machine synthesizes an ``available=False`` record straight
        into the flow context in that case, so the missing descriptor IS the
        failure — not a later cleanup. Reporting it as reclaimed would swallow
        the diagnostic and send the operator after a snapshot that never
        existed.
        """
        root = _repo(tmp_path)
        manager = ReviewScopeManager(root, "flow-lookup-capture-failed")
        synthesized = {
            "baseline_id": "implementation-ffffffffffff",
            "kind": "implementation",
            "flow_id": "flow-lookup-capture-failed",
            "captured_at": "2026-01-01T00:00:00",
            "project_root": str(root),
            "available": False,
            "diagnostics": ["git rev-parse HEAD failed"],
        }

        lookup = manager.lookup_baseline(
            "implementation",
            _scope_context(implementation_baseline=synthesized),
        )

        assert lookup.status == BASELINE_STATUS_UNAVAILABLE
        assert lookup.baseline_id == "implementation-ffffffffffff"
        assert "git rev-parse HEAD failed" in lookup.diagnostic

    def test_failed_fix_capture_is_reported_from_the_history_record(
        self, tmp_path
    ):
        root = _repo(tmp_path)
        manager = ReviewScopeManager(root, "flow-lookup-fix-failed")
        context = _scope_context(
            latest_fix_baseline={
                "baseline_id": "fix-1-eeeeeeeeeeee",
                "available": False,
                "diagnostics": [],
            },
            fix_baseline_history=[
                {
                    "fix_iteration": 1,
                    "baseline_id": "fix-1-eeeeeeeeeeee",
                    "available": False,
                    "diagnostics": ["submodule status unreadable"],
                }
            ],
        )

        lookup = manager.lookup_baseline("fix", context)

        assert lookup.status == BASELINE_STATUS_UNAVAILABLE
        assert "submodule status unreadable" in lookup.diagnostic

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

    def test_corrupt_descriptor_in_a_live_store_is_not_reported_as_reclaimed(
        self, tmp_path
    ):
        """Cleanup is all-or-nothing, so a surviving store never means reclaim.

        A resumable flow whose descriptor got truncated must be told the
        snapshot is damaged, not that it was reclaimed at flow termination —
        the second names an event that did not happen.
        """
        root = _repo(tmp_path)
        manager = ReviewScopeManager(root, "flow-lookup-corrupt")
        baseline = manager.capture("implementation")
        context = _scope_context(implementation_baseline=baseline.to_dict())
        descriptor = manager.root / baseline.baseline_id / "descriptor.json"
        descriptor.write_text('{"baseline_id": "trunc', encoding="utf-8")

        lookup = manager.lookup_baseline("implementation", context)

        assert lookup.status == BASELINE_STATUS_UNAVAILABLE
        assert lookup.baseline_id == baseline.baseline_id
        assert "corrupt" in lookup.diagnostic
        assert manager.store_exists() is True

    def test_descriptor_of_another_flow_is_corrupt_not_usable(self, tmp_path):
        """Where a descriptor sits is not evidence of what it is.

        A descriptor is a plain JSON file: a copy, a restored backup or a hand
        edit can land another flow's snapshot under this flow's id. Rendering
        it would answer with a different flow's review scope while looking
        perfectly healthy, so the persisted identity has to be checked against
        the location before anything reconstructs from it.
        """
        import json

        root = _repo(tmp_path)
        manager = ReviewScopeManager(root, "flow-lookup-owner")
        baseline = manager.capture("implementation")
        context = _scope_context(implementation_baseline=baseline.to_dict())

        foreign_manager = ReviewScopeManager(root, "flow-lookup-intruder")
        foreign = foreign_manager.capture("implementation")
        foreign_payload = json.loads(
            (foreign_manager.root / foreign.baseline_id / "descriptor.json")
            .read_text(encoding="utf-8")
        )
        # Same baseline id (so location and id agree), different owning flow.
        foreign_payload["baseline_id"] = baseline.baseline_id
        (manager.root / baseline.baseline_id / "descriptor.json").write_text(
            json.dumps(foreign_payload), encoding="utf-8"
        )

        lookup = manager.lookup_baseline("implementation", context)

        assert lookup.status == BASELINE_STATUS_UNAVAILABLE
        assert "corrupt" in lookup.diagnostic
        assert manager.load_baseline(baseline.baseline_id) is None
        # A store scan must not resurrect it either.
        assert manager.list_baselines() == []
        # Reconstruction refuses it rather than rendering the other flow's
        # snapshot as this flow's scope.
        rebuilt = manager.reconstruct("full", baseline)
        assert rebuilt.undecidable is True

    def test_descriptor_under_a_foreign_id_is_corrupt_not_usable(self, tmp_path):
        """The id in the file must match the directory it was found in."""
        import json

        root = _repo(tmp_path)
        manager = ReviewScopeManager(root, "flow-lookup-misfiled")
        baseline = manager.capture("implementation")
        other = manager.capture("fix-1")
        context = _scope_context(implementation_baseline=baseline.to_dict())
        misfiled = json.loads(
            (manager.root / other.baseline_id / "descriptor.json")
            .read_text(encoding="utf-8")
        )
        (manager.root / baseline.baseline_id / "descriptor.json").write_text(
            json.dumps(misfiled), encoding="utf-8"
        )

        lookup = manager.lookup_baseline("implementation", context)

        assert lookup.status == BASELINE_STATUS_UNAVAILABLE
        assert "corrupt" in lookup.diagnostic
        # The intact sibling is untouched by the neighbour's defect.
        assert manager.load_baseline(other.baseline_id) is not None

    def test_removed_descriptor_in_a_live_store_is_not_reported_as_reclaimed(
        self, tmp_path
    ):
        root = _repo(tmp_path)
        manager = ReviewScopeManager(root, "flow-lookup-missing-descriptor")
        baseline = manager.capture("implementation")
        fix = manager.capture("fix-1")
        context = _scope_context(
            implementation_baseline=baseline.to_dict(),
            latest_fix_baseline=fix.to_dict(),
        )
        shutil.rmtree(manager.root / fix.baseline_id)

        lookup = manager.lookup_baseline("fix", context)

        assert lookup.status == BASELINE_STATUS_UNAVAILABLE
        assert lookup.baseline_id == fix.baseline_id
        assert "missing" in lookup.diagnostic
        # The intact sibling still resolves: the store was never reclaimed.
        assert manager.lookup_baseline("implementation", context).ok is True

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

    def test_a_parent_section_is_not_selected_by_an_inner_path_filter(self):
        """Selection resolves the SAME containment relation ``--stat`` does.

        A gitlink parent section used to be pulled in by a filter naming a path
        INSIDE it, and it dragged along every sibling inner path it happened to
        render — files the ``--stat`` view of that same filter does not list.
        Every inner label now carries its own ``diff --git`` header (degraded
        notes included), so it is selected under its own name instead.
        """
        section = DiffSection(path="vendor", old_path="vendor", text="")
        assert section_covers_path(section, "vendor") is True
        assert section_covers_path(section, "vendor/inner.py") is False
        assert section_covers_path(section, "vendors/inner.py") is False
        assert section_covers_path(section, "") is False

        inner = DiffSection(
            path="vendor/inner.py", old_path="vendor/inner.py", text=""
        )
        assert section_covers_path(inner, "vendor/inner.py") is True
        assert section_covers_path(inner, "vendor") is True

    def test_section_covers_a_directory_filter(self):
        section = DiffSection(
            path="src/pkg/mod.py", old_path="src/pkg/mod.py", text=""
        )
        # A directory filter selects the files under it — the same relation the
        # --stat table resolves the filter through, so the two views agree.
        assert section_covers_path(section, "src") is True
        assert section_covers_path(section, "src/pkg") is True
        assert section_covers_path(section, "src/pkg/mod.py") is True
        assert section_covers_path(section, "src/pk") is False
        assert section_covers_path(section, "other") is False

    def test_paths_related_contains_one_way_only(self):
        """Admission asks "is a changed path AT or UNDER the filter?".

        The reverse direction would admit a filter that names nothing —
        ``src/pkg/mod.py/nope`` is not a subtree of the changed file, it does
        not exist — and answer it with that file's diff.
        """
        assert paths_related("src/pkg/mod.py", "src/pkg") is True
        assert paths_related("src/pkg", "src/pkg") is True
        assert paths_related("src/pkg", "src/pkg/mod.py") is False
        assert paths_related("src/pkg/mod.py", "src/pkg/mod.py/nope") is False
        assert paths_related("src/pkg", "src/pkgx") is False
        assert paths_related("", "src/pkg") is False
        assert paths_related("src/pkg", "") is False

    def test_equivalent_directory_spellings_are_normalized(self):
        """A trailing slash names the same directory the scope table holds.

        The raw comparison tested for the prefix ``src/pkg//`` and refused a
        directory holding a changed file, so both views of ``--path src/pkg/``
        disagreed with the operator about what the scope contains.
        """
        for spelling in ("src/pkg/", "src/pkg//", "./src/pkg", "src//pkg"):
            assert paths_related("src/pkg/mod.py", spelling) is True, spelling
            section = DiffSection(
                path="src/pkg/mod.py", old_path="src/pkg/mod.py", text=""
            )
            assert section_covers_path(section, spelling) is True, spelling
            sections, stat = select_filtered_view(
                [section], {"src/pkg/mod.py": (1, 0)}, [spelling]
            )
            assert sections == [section], spelling
            assert sorted(stat) == ["src/pkg/mod.py"], spelling

    def test_normalization_widens_nothing(self):
        """Normalizing spellings must not admit filters naming nothing.

        An absolute path is not repository-relative and ``..`` segments stay
        literal, so neither can be re-rooted onto a changed file that merely
        shares its tail; the root filter stays as unsupported as a blank one.
        """
        assert paths_related("src/pkg/mod.py", "/src/pkg") is False
        assert paths_related("src/pkg/mod.py", "../src/pkg") is False
        assert paths_related("src/pkg/mod.py", "src/other/../pkg") is False
        assert paths_related("src/pkg/mod.py", ".") is False
        assert paths_related("src/pkg/mod.py", "/") is False
        assert paths_related("src/pkg/mod.py", "src/pkg/mod.py/nope/") is False

    def test_normalization_keeps_whitespace_significant(self):
        """Space is a legal filename character, not a spelling variant.

        Trimming it rewrote both sides of the containment question into paths
        the repository does not hold: ``--path pkg`` matched the changed file
        ``" pkg/mod.py"``, and a name ending in a space selected its neighbour.
        """
        assert normalize_scope_path(" pkg/mod.py") == " pkg/mod.py"
        assert normalize_scope_path("pkg/mod.py ") == "pkg/mod.py "
        # A filter naming no changed path stays unmatched (the CLI exits 6).
        assert paths_related(" pkg/mod.py", "pkg") is False
        assert paths_related(" pkg/mod.py", " pkg") is True
        assert paths_related("pkg/mod.py ", "pkg/mod.py") is False
        assert paths_related("pkg/mod.py", "pkg/mod.py ") is False
        section = DiffSection(
            path=" pkg/mod.py", old_path=" pkg/mod.py", text=""
        )
        assert section_covers_path(section, "pkg") is False
        assert section_covers_path(section, " pkg") is True
        # Separator/dot collapsing still applies inside a whitespace-bearing
        # name — only the characters of a segment are left alone.
        assert paths_related(" pkg/mod.py", "./ pkg//") is True

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

    def test_filtered_views_agree_on_a_rename(self):
        """One filter, one file set — the section view decides it.

        A rename is one section naming two paths and cannot be split, so a
        per-view scan resolved ``--path`` to the section (both sides) and the
        stat table to the named side alone.
        """
        section = DiffSection(
            path="gamma.py",
            old_path="alpha.py",
            text="diff --git a/alpha.py b/gamma.py\n",
        )
        table = {"alpha.py": (0, 0), "gamma.py": (0, 0)}

        for side in ("alpha.py", "gamma.py"):
            sections, stat = select_filtered_view([section], table, [side])
            assert sections == [section], side
            assert sorted(stat) == ["alpha.py", "gamma.py"], side

    def test_a_selected_section_widens_no_further_than_itself(self):
        """The widening only ever adds the other side of a selected section."""
        renamed = DiffSection(
            path="gamma.py",
            old_path="alpha.py",
            text="diff --git a/alpha.py b/gamma.py\n",
        )
        other = DiffSection(
            path="beta.py",
            old_path="beta.py",
            text="diff --git a/beta.py b/beta.py\n",
        )
        table = {"alpha.py": (0, 0), "beta.py": (1, 1), "gamma.py": (0, 0)}

        sections, stat = select_filtered_view(
            [renamed, other], table, ["alpha.py"]
        )

        assert sections == [renamed]
        assert sorted(stat) == ["alpha.py", "gamma.py"]

    def test_a_section_never_invents_a_stat_row(self):
        """A path the reconstruction never recorded stays out of the table."""
        section = DiffSection(
            path="gamma.py",
            old_path="alpha.py",
            text="diff --git a/alpha.py b/gamma.py\n",
        )

        _, stat = select_filtered_view([section], {"alpha.py": (0, 0)}, ["alpha.py"])

        assert sorted(stat) == ["alpha.py"]

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
