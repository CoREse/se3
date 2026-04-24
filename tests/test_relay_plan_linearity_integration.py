"""Integration tests for ``_relay_plan_is_linear`` on realistic DAGs.

The unit tests in ``test_dag_scheduler.py::TestRelayPlanIsLinear`` exercise
the helper on small hand-crafted ``RelayPlan`` objects. This file complements
them by feeding groups through the full pipeline that ``implement_handler``
uses before consulting the helper:

    transitive_reduce(groups) → classify_chains(...) → _relay_plan_is_linear

The key property under test is robustness: a chain with redundant edges
(e.g. ``A→B, A→C, B→C``) must still be detected as linear, and genuine
forks must not be flattened. Because ``classify_chains`` already assigns
a single *primary* predecessor per node, redundant edges surface as
``ConvergenceInfo`` rather than ``fork_from`` entries — meaning the
helper returns ``True`` even without transitive reduction for those
topologies. The tests below pin that behaviour and also cover shapes the
small unit tests don't: long chains, wide forks, disconnected
components, nodes declared out of topological order, and trailing forks.
"""

from __future__ import annotations

from se3.engine.dag_scheduler import (
    RelayPlan,
    _relay_plan_is_linear,
    classify_chains,
)
from se3.engine.transitive_reduction import transitive_reduce


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_linear(groups: list[dict]) -> bool:
    """Run the full implement_handler pipeline: TR → classify → check."""
    reduced = transitive_reduce(groups)
    plan = classify_chains(reduced)
    return _relay_plan_is_linear(plan)


def _g(gid: str, order: int, *deps: str) -> dict:
    """Compact helper: build a group dict with given id/order/dependencies."""
    return {"group_id": gid, "group_order": order, "depends_on": list(deps)}


# ---------------------------------------------------------------------------
# Pipeline behaviour: redundant edges do not confuse the linear check
# ---------------------------------------------------------------------------


class TestRedundantEdgesStillLinear:
    """Chains with redundant edges are still detected as linear."""

    def test_redundant_edge_stays_linear(self):
        """A→B, A→C, B→C: the A→C redundancy doesn't register as a fork."""
        groups = [_g("A", 1), _g("B", 2, "A"), _g("C", 3, "A", "B")]

        # classify_chains picks B (the chain predecessor) as C's primary
        # and records A as a secondary convergence predecessor, so
        # fork_from stays empty and the helper sees a linear chain.
        assert _is_linear(groups) is True

        # Transitive reduction collapses the redundancy explicitly; the
        # linearity verdict is unchanged but now obvious from the shape.
        reduced = transitive_reduce(groups)
        assert [sorted(g["depends_on"]) for g in reduced] == [[], ["A"], ["B"]]
        assert _is_linear(reduced) is True

    def test_cascading_redundant_edges_stay_linear(self):
        """A→B, A→C, A→D, B→C, B→D, C→D: full transitive closure of a chain."""
        groups = [
            _g("A", 1),
            _g("B", 2, "A"),
            _g("C", 3, "A", "B"),
            _g("D", 4, "A", "B", "C"),
        ]

        # Every intermediate has redundant edges, but classify_chains
        # still identifies the natural A→B→C→D spine.
        assert _is_linear(groups) is True

    def test_genuine_fork_stays_fork_after_reduction(self):
        """A→B, A→C with no B↔C edge is a real fork; TR changes nothing."""
        groups = [_g("A", 1), _g("B", 2, "A"), _g("C", 3, "A")]

        assert _is_linear(groups) is False

        # Reduction is a no-op here because neither edge is redundant.
        reduced = transitive_reduce(groups)
        assert {g["group_id"]: sorted(g["depends_on"]) for g in reduced} == {
            "A": [],
            "B": ["A"],
            "C": ["A"],
        }

    def test_diamond_with_redundant_root_edge(self):
        """A→B, A→C, A→D, B→D, C→D: A→D redundant but B/C still fork."""
        groups = [
            _g("A", 1),
            _g("B", 2, "A"),
            _g("C", 3, "A"),
            _g("D", 4, "A", "B", "C"),
        ]

        # The A→D edge is redundant (A→B→D and A→C→D both exist), but the
        # B/C fork from A is real, so the reduced graph is still a diamond.
        assert _is_linear(groups) is False


# ---------------------------------------------------------------------------
# Extended topology shapes
# ---------------------------------------------------------------------------


class TestExtendedTopologies:
    """Shapes the small unit tests don't explicitly cover."""

    def test_long_linear_chain(self):
        """A 10-node chain is still linear."""
        groups = [_g(f"G{i}", i) for i in range(1, 11)]
        for i in range(1, 10):
            groups[i]["depends_on"] = [f"G{i}"]
        assert _is_linear(groups) is True

    def test_wide_fork_from_single_root(self):
        """One root with four independent dependents is not linear."""
        groups = [_g("R", 1)] + [_g(f"L{i}", i + 1, "R") for i in range(1, 5)]
        assert _is_linear(groups) is False

    def test_two_disconnected_linear_chains(self):
        """Two independent chains share no edges → two roots → not linear."""
        groups = [
            _g("A1", 1),
            _g("A2", 2, "A1"),
            _g("B1", 3),
            _g("B2", 4, "B1"),
        ]
        assert _is_linear(groups) is False

    def test_chain_with_out_of_order_declaration(self):
        """``group_order`` and list order don't change linearity detection."""
        # Declared in reverse order; edges still form a linear chain.
        groups = [
            _g("C", 3, "B"),
            _g("B", 2, "A"),
            _g("A", 1),
        ]
        assert _is_linear(groups) is True

    def test_trailing_fork_off_linear_prefix(self):
        """A→B→C→D and C→E: the fork at C breaks linearity."""
        groups = [
            _g("A", 1),
            _g("B", 2, "A"),
            _g("C", 3, "B"),
            _g("D", 4, "C"),
            _g("E", 5, "C"),
        ]
        assert _is_linear(groups) is False

    def test_converging_two_chains_at_tail(self):
        """Two independent chains converging at one tail → multi-root, not linear."""
        groups = [
            _g("A1", 1),
            _g("A2", 2, "A1"),
            _g("B1", 3),
            _g("B2", 4, "B1"),
            _g("T", 5, "A2", "B2"),
        ]
        # Two roots (A1, B1) means _relay_plan_is_linear returns False
        # regardless of the convergence point.
        assert _is_linear(groups) is False


# ---------------------------------------------------------------------------
# Pipeline preserves input and returns a RelayPlan of expected shape
# ---------------------------------------------------------------------------


class TestPipelineInvariants:
    """Sanity checks on the types/shape the pipeline produces."""

    def test_pipeline_returns_relayplan_instance(self):
        """classify_chains always returns a RelayPlan, even after TR."""
        groups = [_g("A", 1), _g("B", 2, "A"), _g("C", 3, "A", "B")]
        plan = classify_chains(transitive_reduce(groups))
        assert isinstance(plan, RelayPlan)

    def test_pipeline_does_not_mutate_input(self):
        """transitive_reduce must deep-copy; the caller's groups survive."""
        groups = [_g("A", 1), _g("B", 2, "A"), _g("C", 3, "A", "B")]
        snapshot = [dict(g, depends_on=list(g["depends_on"])) for g in groups]

        _is_linear(groups)

        # Input unchanged (especially the redundant A→C edge on C).
        for original, after in zip(snapshot, groups):
            assert original == after

    def test_single_node_pipeline_is_linear(self):
        """Even a single-node plan passes the pipeline cleanly."""
        assert _is_linear([_g("solo", 1)]) is True

    def test_empty_pipeline_is_not_linear(self):
        """Empty groups → empty RelayPlan → not linear (zero roots)."""
        assert _is_linear([]) is False
