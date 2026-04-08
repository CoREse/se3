"""Tests for DAG topology display in TaskFormatter."""

from __future__ import annotations

import pytest
from rich.console import Console

from se3.engine.dag_scheduler import classify_chains
from se3.engine.formatters import TaskFormatter
from se3.engine.transitive_reduction import transitive_reduce


def _make_group(group_id, name, order, depends_on=None):
    """Helper to create a minimal group dict for topology tests."""
    return {
        "group_id": group_id,
        "name": name,
        "group_order": order,
        "depends_on": depends_on or [],
        "tasks": [
            {
                "id": f"{group_id}_t1",
                "description": f"Task for {group_id}",
                "complexity": "medium",
                "estimated_loc": 50,
            },
        ],
    }


class TestComputeTopoWaves:
    """Tests for _compute_topo_waves static method."""

    def test_linear_chain(self):
        groups = [
            _make_group("G1", "First", 1),
            _make_group("G2", "Second", 2, ["G1"]),
            _make_group("G3", "Third", 3, ["G2"]),
        ]
        waves = TaskFormatter._compute_topo_waves(groups)

        assert len(waves) == 3
        assert waves[0] == ["G1"]
        assert waves[1] == ["G2"]
        assert waves[2] == ["G3"]

    def test_fork(self):
        groups = [
            _make_group("G1", "Root", 1),
            _make_group("G2", "Left", 2, ["G1"]),
            _make_group("G3", "Right", 3, ["G1"]),
        ]
        waves = TaskFormatter._compute_topo_waves(groups)

        assert len(waves) == 2
        assert waves[0] == ["G1"]
        assert set(waves[1]) == {"G2", "G3"}

    def test_diamond(self):
        groups = [
            _make_group("G1", "Root", 1),
            _make_group("G2", "Left", 2, ["G1"]),
            _make_group("G3", "Right", 3, ["G1"]),
            _make_group("G4", "Merge", 4, ["G2", "G3"]),
        ]
        waves = TaskFormatter._compute_topo_waves(groups)

        assert len(waves) == 3
        assert waves[0] == ["G1"]
        assert set(waves[1]) == {"G2", "G3"}
        assert waves[2] == ["G4"]

    def test_independent_roots(self):
        groups = [
            _make_group("G1", "A", 1),
            _make_group("G2", "B", 2),
            _make_group("G3", "C", 3, ["G1", "G2"]),
        ]
        waves = TaskFormatter._compute_topo_waves(groups)

        assert len(waves) == 2
        assert set(waves[0]) == {"G1", "G2"}
        assert waves[1] == ["G3"]

    def test_sorted_by_group_order(self):
        groups = [
            _make_group("G3", "Third", 3),
            _make_group("G1", "First", 1),
            _make_group("G2", "Second", 2),
        ]
        waves = TaskFormatter._compute_topo_waves(groups)

        assert len(waves) == 1
        assert waves[0] == ["G1", "G2", "G3"]


class TestBuildDagTopology:
    """Tests for _build_dag_topology method."""

    @pytest.fixture
    def console(self):
        return Console(width=120, force_terminal=True)

    @pytest.fixture
    def formatter(self, console):
        return TaskFormatter(console=console)

    def _render(self, console, text):
        with console.capture() as capture:
            console.print(text)
        return capture.get()

    def test_linear_chain(self, formatter, console):
        """G1 → G2 → G3: three waves, all relay connections."""
        groups = [
            _make_group("G1", "First", 1),
            _make_group("G2", "Second", 2, ["G1"]),
            _make_group("G3", "Third", 3, ["G2"]),
        ]
        relay_plan = classify_chains(groups)
        topology = formatter._build_dag_topology(groups, relay_plan)
        output = self._render(console, topology)

        assert "Wave 1" in output
        assert "Wave 2" in output
        assert "Wave 3" in output
        assert "G1" in output
        assert "G2" in output
        assert "G3" in output
        assert "relay" in output
        assert "new worktree" in output
        assert "merge-back" in output
        # G3 is the only leaf — only one merge-back
        assert output.count("merge-back") == 1

    def test_fork(self, formatter, console):
        """G1 → (G2, G3): G2 relay, G3 fork."""
        groups = [
            _make_group("G1", "Root", 1),
            _make_group("G2", "Branch A", 2, ["G1"]),
            _make_group("G3", "Branch B", 3, ["G1"]),
        ]
        relay_plan = classify_chains(groups)
        topology = formatter._build_dag_topology(groups, relay_plan)
        output = self._render(console, topology)

        assert "relay" in output
        assert "fork" in output
        # Both G2 and G3 are leaves
        assert output.count("merge-back") == 2

    def test_convergence(self, formatter, console):
        """(G1, G2) → G3: G3 merges G1 and G2."""
        groups = [
            _make_group("G1", "Source A", 1),
            _make_group("G2", "Source B", 2),
            _make_group("G3", "Merge Point", 3, ["G1", "G2"]),
        ]
        relay_plan = classify_chains(groups)
        topology = formatter._build_dag_topology(groups, relay_plan)
        output = self._render(console, topology)

        assert "\u2295" in output  # ⊕ merge symbol
        assert "merge" in output.lower()
        # G3 is the only leaf
        assert output.count("merge-back") == 1
        # G1 and G2 are roots
        assert output.count("new worktree") == 2

    def test_diamond_dag(self, formatter, console):
        """G1 → (G2, G3) → G4: diamond shape with all relationship types."""
        groups = [
            _make_group("G1", "Root", 1),
            _make_group("G2", "Left", 2, ["G1"]),
            _make_group("G3", "Right", 3, ["G1"]),
            _make_group("G4", "Merge", 4, ["G2", "G3"]),
        ]
        reduced = transitive_reduce(groups)
        relay_plan = classify_chains(reduced)
        topology = formatter._build_dag_topology(reduced, relay_plan)
        output = self._render(console, topology)

        assert "Wave 1" in output
        assert "Wave 2" in output
        assert "Wave 3" in output
        assert "relay" in output
        assert "fork" in output
        assert "\u2295" in output  # ⊕ merge
        assert "merge-back" in output
        # LLM call numbers
        assert "#1" in output
        assert "#4" in output

    def test_single_group_returns_empty(self, formatter):
        """Single group should not render topology diagram."""
        groups = [_make_group("G1", "Only", 1)]
        relay_plan = classify_chains(groups)
        topology = formatter._build_dag_topology(groups, relay_plan)

        assert topology.plain.strip() == ""

    def test_llm_call_numbering(self, formatter, console):
        """Verify LLM call numbers are sequential across waves."""
        groups = [
            _make_group("G1", "First", 1),
            _make_group("G2", "Second", 2, ["G1"]),
            _make_group("G3", "Third", 3, ["G1"]),
            _make_group("G4", "Fourth", 4, ["G2", "G3"]),
        ]
        reduced = transitive_reduce(groups)
        relay_plan = classify_chains(reduced)
        topology = formatter._build_dag_topology(reduced, relay_plan)
        output = self._render(console, topology)

        # Wave 1 has call #1, Wave 2 has #2 and #3, Wave 3 has #4
        assert "LLM #1" in output
        assert "#2" in output
        assert "#3" in output
        assert "#4" in output


class TestFormatImplementPlanTopology:
    """Integration tests for topology in format_implement_plan."""

    @pytest.fixture
    def console(self):
        return Console(width=120, force_terminal=True)

    @pytest.fixture
    def formatter(self, console):
        return TaskFormatter(console=console)

    def _render(self, console, panel):
        with console.capture() as capture:
            console.print(panel)
        return capture.get()

    def test_dag_parallel_shows_topology(self, formatter, console):
        """dag_parallel strategy with relay_plan shows topology."""
        groups = [
            _make_group("G1", "Root", 1),
            _make_group("G2", "Left", 2, ["G1"]),
            _make_group("G3", "Right", 3, ["G1"]),
        ]
        relay_plan = classify_chains(groups)
        panel = formatter.format_implement_plan(
            task_groups=groups,
            execution_strategy="dag_parallel",
            total_loc=300,
            loc_threshold=200,
            relay_plan=relay_plan,
        )
        output = self._render(console, panel)

        assert "Execution Topology" in output
        assert "DAG parallel" in output

    def test_non_dag_no_topology(self, formatter, console):
        """Non-dag_parallel strategy should not show topology."""
        groups = [
            _make_group("G1", "Root", 1),
            _make_group("G2", "Next", 2, ["G1"]),
        ]
        panel = formatter.format_implement_plan(
            task_groups=groups,
            execution_strategy="single",
            total_loc=100,
            loc_threshold=300,
        )
        output = self._render(console, panel)

        assert "Execution Topology" not in output

    def test_none_relay_plan_no_error(self, formatter, console):
        """relay_plan=None should not cause errors."""
        groups = [
            _make_group("G1", "Root", 1),
            _make_group("G2", "Next", 2, ["G1"]),
        ]
        panel = formatter.format_implement_plan(
            task_groups=groups,
            execution_strategy="dag_parallel",
            total_loc=400,
            loc_threshold=300,
            relay_plan=None,
        )
        output = self._render(console, panel)

        assert "Execution Topology" not in output
        assert "DAG parallel" in output

    def test_rendering_exception_does_not_block(self, formatter, console):
        """Bad relay_plan should not crash format_implement_plan."""
        groups = [
            _make_group("G1", "Root", 1),
            _make_group("G2", "Next", 2, ["G1"]),
        ]
        # Pass a broken relay_plan to trigger exception in _build_dag_topology
        panel = formatter.format_implement_plan(
            task_groups=groups,
            execution_strategy="dag_parallel",
            total_loc=400,
            loc_threshold=300,
            relay_plan="not a relay plan",
        )
        output = self._render(console, panel)

        # Should still show the rest of the plan
        assert "DAG parallel" in output
        assert "G1" in output
