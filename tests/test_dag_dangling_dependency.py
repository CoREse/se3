"""Tests for DAG disaster-recovery dangling-dependency handling.

Covers the two-line fix for the crash recovery scenario:

1. ``implement._prune_recovered_dependencies`` proactively drops already
   completed/pre-merged groups and prunes the ``depends_on`` edges that point
   at them from the retained groups (so the schedule it hands to the DAG
   scheduler is free of dangling edges, and the original ``groups`` list is
   never mutated).
2. ``DAGScheduler._build_dag`` defends against any dangling edge that still
   slips through by skipping it (with a warning) rather than raising
   ``ValueError`` — while preserving every other validation (duplicate id,
   missing id, cycle detection).
"""

from __future__ import annotations

import logging

import pytest

from tianluo.engine.dag_scheduler import DAGScheduler
from tianluo.engine.steps.implement import _prune_recovered_dependencies


# ---------------------------------------------------------------------------
# Recovery edge-pruning (_prune_recovered_dependencies)
# ---------------------------------------------------------------------------

class TestPruneRecoveredDependencies:
    def test_drops_completed_and_prunes_inbound_edges(self):
        """G1 survived a crash; G2/G3 still point at it -> edges pruned."""
        groups = [
            {"group_id": "G1", "depends_on": []},
            {"group_id": "G2", "depends_on": ["G1"]},
            {"group_id": "G3", "depends_on": ["G1", "G2"]},
        ]
        result = _prune_recovered_dependencies(groups, {"G1"})

        ids = [g["group_id"] for g in result]
        assert ids == ["G2", "G3"]
        # The edge to the completed G1 is gone from both survivors.
        assert _deps(result, "G2") == []
        assert _deps(result, "G3") == ["G2"]

    def test_does_not_remove_legal_edges(self):
        """Edges among retained groups are preserved verbatim."""
        groups = [
            {"group_id": "G1", "depends_on": []},
            {"group_id": "G2", "depends_on": ["G1"]},
            {"group_id": "G3", "depends_on": ["G2"]},
            {"group_id": "G4", "depends_on": ["G2", "G3"]},
        ]
        # Only G1 completed.
        result = _prune_recovered_dependencies(groups, {"G1"})
        assert [g["group_id"] for g in result] == ["G2", "G3", "G4"]
        assert _deps(result, "G2") == []
        assert _deps(result, "G3") == ["G2"]
        assert _deps(result, "G4") == ["G2", "G3"]

    def test_does_not_mutate_original_groups(self):
        """Deep-copy semantics: the input list and its dicts are untouched."""
        groups = [
            {"group_id": "G1", "depends_on": []},
            {"group_id": "G2", "depends_on": ["G1"]},
            {"group_id": "G3", "depends_on": ["G1", "G2"]},
        ]
        snapshot = [dict(g, depends_on=list(g["depends_on"])) for g in groups]

        _prune_recovered_dependencies(groups, {"G1"})

        for original, before in zip(groups, snapshot):
            assert original == before
        # In particular the original depends_on lists still mention G1.
        assert groups[2]["depends_on"] == ["G1", "G2"]

    def test_name_fallback_for_group_id(self):
        """Groups keyed by ``name`` (no ``group_id``) are handled too."""
        groups = [
            {"name": "G1", "depends_on": []},
            {"name": "G2", "depends_on": ["G1"]},
        ]
        result = _prune_recovered_dependencies(groups, {"G1"})
        assert [g["name"] for g in result] == ["G2"]
        assert result[0]["depends_on"] == []

    def test_empty_completed_set_is_identity_copy(self):
        groups = [
            {"group_id": "G1", "depends_on": []},
            {"group_id": "G2", "depends_on": ["G1"]},
        ]
        result = _prune_recovered_dependencies(groups, set())
        assert [g["group_id"] for g in result] == ["G1", "G2"]
        assert _deps(result, "G2") == ["G1"]
        # Still a copy, not the same objects.
        assert result[0] is not groups[0]


# ---------------------------------------------------------------------------
# Recovery scenario end-to-end: pruned groups build a valid scheduler
# ---------------------------------------------------------------------------

class TestRecoveryBuildsValidScheduler:
    def test_pruned_groups_build_without_raising(self):
        """The G1-completed recovery case schedules cleanly after pruning."""
        groups = [
            {"group_id": "G1", "depends_on": []},
            {"group_id": "G2", "depends_on": ["G1"]},
            {"group_id": "G3", "depends_on": ["G1", "G2"]},
        ]
        dag_groups = _prune_recovered_dependencies(groups, {"G1"})

        # Must not raise "depends on unknown group G1".
        scheduler = DAGScheduler(dag_groups)
        order = scheduler.topological_merge_order()
        assert set(order) == {"G2", "G3"}
        assert order.index("G2") < order.index("G3")

    def test_unpruned_dangling_edge_would_have_dangled(self):
        """Sanity: without pruning the same edge dangles (defense handles it)."""
        # G1 removed but its inbound edges left intact -> dangling 'G1'.
        dag_groups = [
            {"group_id": "G2", "depends_on": ["G1"]},
            {"group_id": "G3", "depends_on": ["G1", "G2"]},
        ]
        # The _build_dag defense skips the dangling edge instead of raising.
        scheduler = DAGScheduler(dag_groups)
        assert set(scheduler.topological_merge_order()) == {"G2", "G3"}


# ---------------------------------------------------------------------------
# _build_dag defensive skip of dangling edges
# ---------------------------------------------------------------------------

class TestBuildDagDanglingDefense:
    def test_dangling_edge_skipped_and_warned(self, caplog):
        groups = [
            {"group_id": "G2", "depends_on": ["G1"]},  # G1 absent
            {"group_id": "G3", "depends_on": ["G2"]},
        ]
        with caplog.at_level(logging.WARNING):
            scheduler = DAGScheduler(groups)

        # G2's dangling edge to G1 contributes no in-degree.
        assert scheduler._in_degree["G2"] == 0
        assert scheduler._reverse_deps["G2"] == []
        # G3 -> G2 legal edge intact.
        assert scheduler._in_degree["G3"] == 1
        assert scheduler._reverse_deps["G3"] == ["G2"]
        # Topo order still covers all real groups.
        assert scheduler._topo_order == ["G2", "G3"]
        # The defense is observable: it warns rather than silently ignoring.
        assert any("unknown group" in r.message for r in caplog.records)

    def test_defense_does_not_swallow_cycle_detection(self):
        """Cycle detection still fires after the dangling-edge defense."""
        groups = [
            {"group_id": "A", "depends_on": ["C"]},
            {"group_id": "B", "depends_on": ["A"]},
            {"group_id": "C", "depends_on": ["B"]},
        ]
        with pytest.raises(ValueError, match="[Cc]ycle"):
            DAGScheduler(groups)

    def test_defense_does_not_swallow_duplicate_id(self):
        groups = [
            {"group_id": "A"},
            {"group_id": "A"},
        ]
        with pytest.raises(ValueError, match="[Dd]uplicate"):
            DAGScheduler(groups)

    def test_defense_does_not_swallow_missing_id(self):
        groups = [{"depends_on": []}]
        with pytest.raises(ValueError, match="missing group_id"):
            DAGScheduler(groups)


def _deps(groups: list[dict], gid: str) -> list[str]:
    for g in groups:
        if g.get("group_id", g.get("name")) == gid:
            return g.get("depends_on", [])
    raise KeyError(gid)
