"""Tests for the transitive reduction algorithm."""

from __future__ import annotations

import copy

import pytest

from tianluo.engine.transitive_reduction import transitive_reduce


# ---------------------------------------------------------------------------
# Edge-case / empty input
# ---------------------------------------------------------------------------

class TestTransitiveReduceEmpty:
    def test_empty_groups(self):
        assert transitive_reduce([]) == []

    def test_single_group_no_deps(self):
        groups = [{"group_id": "G1", "depends_on": []}]
        result = transitive_reduce(groups)
        assert len(result) == 1
        assert result[0]["depends_on"] == []

    def test_single_group_missing_depends_on_key(self):
        """Groups without a depends_on key should be handled gracefully."""
        groups = [{"group_id": "G1"}]
        result = transitive_reduce(groups)
        assert len(result) == 1
        assert result[0].get("depends_on") is None  # key not added


# ---------------------------------------------------------------------------
# No redundant edges — should remain unchanged
# ---------------------------------------------------------------------------

class TestTransitiveReduceNoChange:
    def test_linear_chain_minimal(self):
        """A→B→C with no redundant edges."""
        groups = [
            {"group_id": "A", "depends_on": []},
            {"group_id": "B", "depends_on": ["A"]},
            {"group_id": "C", "depends_on": ["B"]},
        ]
        result = transitive_reduce(groups)
        assert result[0]["depends_on"] == []
        assert result[1]["depends_on"] == ["A"]
        assert result[2]["depends_on"] == ["B"]

    def test_diamond_not_reducible(self):
        """Diamond A→{B,C}→D: D depends on both B and C — NOT reducible.

        B and C are independent; neither is reachable from the other,
        so both edges into D are essential.
        """
        groups = [
            {"group_id": "A", "depends_on": []},
            {"group_id": "B", "depends_on": ["A"]},
            {"group_id": "C", "depends_on": ["A"]},
            {"group_id": "D", "depends_on": ["B", "C"]},
        ]
        result = transitive_reduce(groups)
        assert sorted(result[3]["depends_on"]) == ["B", "C"]

    def test_two_independent_groups(self):
        groups = [
            {"group_id": "X", "depends_on": []},
            {"group_id": "Y", "depends_on": []},
        ]
        result = transitive_reduce(groups)
        assert result[0]["depends_on"] == []
        assert result[1]["depends_on"] == []

    def test_two_groups_single_edge(self):
        groups = [
            {"group_id": "A", "depends_on": []},
            {"group_id": "B", "depends_on": ["A"]},
        ]
        result = transitive_reduce(groups)
        assert result[1]["depends_on"] == ["A"]


# ---------------------------------------------------------------------------
# Redundant edges removed
# ---------------------------------------------------------------------------

class TestTransitiveReduceRedundant:
    def test_linear_chain_with_shortcut(self):
        """A→B→C, but C also depends on A directly — A is redundant for C."""
        groups = [
            {"group_id": "A", "depends_on": []},
            {"group_id": "B", "depends_on": ["A"]},
            {"group_id": "C", "depends_on": ["A", "B"]},
        ]
        result = transitive_reduce(groups)
        assert result[0]["depends_on"] == []
        assert result[1]["depends_on"] == ["A"]
        assert result[2]["depends_on"] == ["B"]

    def test_longer_chain_with_skip(self):
        """A→B→C→D, D depends on [A, B, C] — A and B are redundant."""
        groups = [
            {"group_id": "A", "depends_on": []},
            {"group_id": "B", "depends_on": ["A"]},
            {"group_id": "C", "depends_on": ["B"]},
            {"group_id": "D", "depends_on": ["A", "B", "C"]},
        ]
        result = transitive_reduce(groups)
        assert result[3]["depends_on"] == ["C"]

    def test_complex_dag_multiple_redundancies(self):
        """Complex DAG with several redundant edges.

        Structure:
            A → B → D
            A → C → D
            A → D  (redundant — reachable via A→B→D and A→C→D)
            B → D  (NOT redundant — no other B→…→D path without this edge)
            C → D  (NOT redundant — same reasoning)

        Wait — B and C both go to D independently, so D depends on [B, C]
        after removing the A→D shortcut.
        """
        groups = [
            {"group_id": "A", "depends_on": []},
            {"group_id": "B", "depends_on": ["A"]},
            {"group_id": "C", "depends_on": ["A"]},
            {"group_id": "D", "depends_on": ["A", "B", "C"]},
        ]
        result = transitive_reduce(groups)
        # A→D is redundant (reachable via A→B→D or A→C→D)
        assert sorted(result[3]["depends_on"]) == ["B", "C"]

    def test_wide_fan_in_with_chain(self):
        """A→B→C→D, A→D, B→D — both A→D and B→D via chain.

        A→D redundant (A→B→C→D). B→D redundant (B→C→D).
        """
        groups = [
            {"group_id": "A", "depends_on": []},
            {"group_id": "B", "depends_on": ["A"]},
            {"group_id": "C", "depends_on": ["B"]},
            {"group_id": "D", "depends_on": ["A", "B", "C"]},
        ]
        result = transitive_reduce(groups)
        assert result[3]["depends_on"] == ["C"]

    def test_multiple_roots_with_redundancy(self):
        """Two independent roots feeding into a common chain.

        R1 → A → C
        R2 → B → C
        C depends on [R1, A, R2, B]
        R1 redundant (R1→A→C), R2 redundant (R2→B→C).
        """
        groups = [
            {"group_id": "R1", "depends_on": []},
            {"group_id": "R2", "depends_on": []},
            {"group_id": "A", "depends_on": ["R1"]},
            {"group_id": "B", "depends_on": ["R2"]},
            {"group_id": "C", "depends_on": ["R1", "R2", "A", "B"]},
        ]
        result = transitive_reduce(groups)
        c_deps = next(g for g in result if g["group_id"] == "C")["depends_on"]
        assert sorted(c_deps) == ["A", "B"]


# ---------------------------------------------------------------------------
# Immutability guarantee
# ---------------------------------------------------------------------------

class TestTransitiveReduceImmutability:
    def test_does_not_mutate_input(self):
        groups = [
            {"group_id": "A", "depends_on": []},
            {"group_id": "B", "depends_on": ["A"]},
            {"group_id": "C", "depends_on": ["A", "B"]},
        ]
        original = copy.deepcopy(groups)
        transitive_reduce(groups)
        assert groups == original

    def test_returned_list_is_new_object(self):
        groups = [{"group_id": "A", "depends_on": []}]
        result = transitive_reduce(groups)
        assert result is not groups
        assert result[0] is not groups[0]


# ---------------------------------------------------------------------------
# Extra fields are preserved
# ---------------------------------------------------------------------------

class TestTransitiveReducePreservesFields:
    def test_extra_keys_preserved(self):
        groups = [
            {"group_id": "A", "depends_on": [], "name": "alpha", "tasks": [1, 2]},
            {"group_id": "B", "depends_on": ["A"], "name": "beta", "tasks": [3]},
            {"group_id": "C", "depends_on": ["A", "B"], "name": "gamma", "tasks": [4]},
        ]
        result = transitive_reduce(groups)
        for orig, red in zip(groups, result):
            assert red["group_id"] == orig["group_id"]
            assert red["name"] == orig["name"]
            assert red["tasks"] == orig["tasks"]

    def test_group_order_preserved(self):
        """Output list order matches input list order."""
        groups = [
            {"group_id": "Z", "depends_on": []},
            {"group_id": "M", "depends_on": ["Z"]},
            {"group_id": "A", "depends_on": ["Z", "M"]},
        ]
        result = transitive_reduce(groups)
        assert [g["group_id"] for g in result] == ["Z", "M", "A"]
