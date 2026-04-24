"""DAG-based parallel scheduler for task group execution.

Provides event-driven scheduling of task groups based on dependency graphs.
Groups without dependencies start immediately; downstream groups start as
soon as all their dependencies complete. Each group runs in its own thread
via ThreadPoolExecutor.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Relay execution planning data structures
# ---------------------------------------------------------------------------


@dataclass
class ConvergenceInfo:
    """Merge information for a convergence point (node with multiple predecessors).

    At a convergence point, the group inherits the primary predecessor's
    worktree and merges the secondary predecessors' branches into it.
    """

    primary_predecessor: str  # group_id whose worktree is inherited
    secondary_predecessors: list[str]  # group_ids whose branches are merged in


@dataclass
class RelayContext:
    """Runtime context passed to execute_fn for relay-based execution.

    Tells the executor how to obtain its worktree: reuse from predecessor,
    fork a new one, or create fresh.
    """

    worktree_path: Optional[Path] = None  # predecessor's worktree to reuse (None → create new)
    branch_name: Optional[str] = None  # predecessor's branch name
    is_fork: bool = False  # whether to fork a new branch from predecessor
    fork_source_branch: Optional[str] = None  # branch to fork from (when is_fork=True)
    convergence_merges: list[str] = field(default_factory=list)  # secondary predecessor branches to merge


@dataclass
class RelayPlan:
    """Execution plan for relay-based DAG execution.

    Produced by ``classify_chains()`` and consumed by ``DAGScheduler`` and
    the per-group execute function to determine worktree reuse, forking, and
    leaf merge strategies.
    """

    relay_map: dict[str, Optional[str]]  # group_id → predecessor group_id (reuse worktree) or None (new)
    fork_from: dict[str, str]  # group_id → predecessor group_id to fork from
    leaf_nodes: set[str]  # groups with no downstream — merge back to original_branch
    convergence_points: dict[str, ConvergenceInfo]  # convergence nodes and their merge info
    root_nodes: set[str]  # groups with no predecessors — need new worktree


# ---------------------------------------------------------------------------
# classify_chains — relay execution planner
# ---------------------------------------------------------------------------


def classify_chains(groups: list[dict]) -> RelayPlan:
    """Analyze DAG topology and produce a relay execution plan.

    For each group, determines whether it should:
    - Create a new worktree (root nodes, fork targets)
    - Reuse a predecessor's worktree (relay)
    - Merge secondary predecessor branches (convergence points)

    Args:
        groups: List of group dicts with ``group_id``, ``depends_on``, and
                ``group_order`` fields.

    Returns:
        A ``RelayPlan`` describing the execution strategy for each group.
    """
    if not groups:
        return RelayPlan(
            relay_map={},
            fork_from={},
            leaf_nodes=set(),
            convergence_points={},
            root_nodes=set(),
        )

    # Build lookup tables
    group_map: dict[str, dict] = {}
    for g in groups:
        gid = g.get("group_id", g.get("name", ""))
        group_map[gid] = g

    all_ids = set(group_map.keys())

    # Build forward adjacency (P → downstream children) and reverse (G → predecessors)
    forward: dict[str, list[str]] = {gid: [] for gid in all_ids}
    reverse: dict[str, list[str]] = {gid: [] for gid in all_ids}

    for gid, g in group_map.items():
        deps = g.get("depends_on") or []
        for dep in deps:
            if dep in all_ids:
                forward[dep].append(gid)
                reverse[gid].append(dep)

    def _group_order(gid: str) -> int:
        """Return group_order for sorting; fall back to 0."""
        return group_map[gid].get("group_order", 0)

    # Identify root and leaf nodes
    root_nodes = {gid for gid in all_ids if not reverse[gid]}
    leaf_nodes = {gid for gid in all_ids if not forward[gid]}

    # For each predecessor, determine its heir (primary child = smallest group_order)
    heir: dict[str, Optional[str]] = {}
    for gid in all_ids:
        children = forward[gid]
        if children:
            heir[gid] = min(children, key=_group_order)
        else:
            heir[gid] = None

    # Build relay_map, fork_from, convergence_points
    relay_map: dict[str, Optional[str]] = {}
    fork_from: dict[str, str] = {}
    convergence_points: dict[str, ConvergenceInfo] = {}

    for gid in all_ids:
        if gid in root_nodes:
            relay_map[gid] = None
            continue

        predecessors = reverse[gid]

        if len(predecessors) == 1:
            p = predecessors[0]
            if heir[p] == gid:
                # G is P's primary child → relay
                relay_map[gid] = p
            else:
                # G is not P's primary child → fork
                relay_map[gid] = None
                fork_from[gid] = p
        else:
            # Convergence point: multiple predecessors
            # Find predecessors where G is their heir (can relay)
            relaying_preds = [p for p in predecessors if heir[p] == gid]
            non_relaying_preds = [p for p in predecessors if heir[p] != gid]

            if relaying_preds:
                # Pick the relaying predecessor with smallest group_order as primary
                primary = min(relaying_preds, key=_group_order)
                relay_map[gid] = primary
            else:
                # No predecessor can relay → fork from the one with smallest group_order
                primary = min(predecessors, key=_group_order)
                relay_map[gid] = None
                fork_from[gid] = primary

            secondary = [p for p in predecessors if p != primary]
            convergence_points[gid] = ConvergenceInfo(
                primary_predecessor=primary,
                secondary_predecessors=sorted(secondary, key=_group_order),
            )

    return RelayPlan(
        relay_map=relay_map,
        fork_from=fork_from,
        leaf_nodes=leaf_nodes,
        convergence_points=convergence_points,
        root_nodes=root_nodes,
    )


def _relay_plan_is_linear(plan: RelayPlan) -> bool:
    """Return True iff the DAG described by ``plan`` is a linear chain.

    A linear chain has no forks (``fork_from`` empty) and exactly one root,
    which means every topological layer contains at most one node — i.e.
    the DAG has no actual parallel wave. Callers can use this to short-circuit
    DAG-parallel execution and fall back to the sequential path, since
    spinning up per-group worktrees yields no parallelism benefit.

    Note: does not require ``plan`` to be non-empty; an empty plan has zero
    roots and therefore returns False.
    """
    return not plan.fork_from and len(plan.root_nodes) == 1


@dataclass
class GroupResult:
    """Result of executing a single task group."""

    group_id: str
    status: str  # 'completed' | 'failed' | 'skipped'
    files_changed: list[str] = field(default_factory=list)
    tests_added: list[str] = field(default_factory=list)
    test_mapping: dict = field(default_factory=dict)
    summary: str = ""
    branch_name: str = ""
    worktree_path: Optional[Path] = None
    completion_status: str = "complete"  # 'complete' | 'partial' | 'failed'
    incomplete_tasks: list[str] = field(default_factory=list)
    restricted_edits: list[dict] = field(default_factory=list)
    estimated_test_duration: Optional[float] = None
    error: Optional[str] = None

    @classmethod
    def skipped(cls, group_id: str) -> GroupResult:
        """Create a result for a group skipped due to upstream failure."""
        return cls(
            group_id=group_id,
            status="skipped",
            completion_status="failed",
            error="Skipped: upstream dependency failed",
        )

    @classmethod
    def failed(cls, group_id: str, error: str) -> GroupResult:
        """Create a result for a group that failed execution."""
        return cls(
            group_id=group_id,
            status="failed",
            completion_status="failed",
            error=error,
        )


class DAGScheduler:
    """Event-driven DAG scheduler for parallel task group execution.

    Builds a directed acyclic graph from task group dependencies and
    schedules execution using ThreadPoolExecutor + threading.Condition.
    Groups with no dependencies start immediately; each completed group
    triggers an immediate scan for newly-unblocked downstream groups.

    When a ``relay_plan`` is provided, the scheduler builds a
    :class:`RelayContext` for each group before submitting it, enabling
    relay-based worktree reuse, forking, and convergence merging.

    Args:
        groups: List of group dicts, each containing at least 'group_id'
                and optionally 'depends_on' (list of group_id strings).
        max_workers: Maximum number of concurrent group executions.
        relay_plan: Optional relay execution plan from ``classify_chains()``.
    """

    def __init__(
        self,
        groups: list[dict],
        max_workers: int = 4,
        relay_plan: Optional[RelayPlan] = None,
    ) -> None:
        self._groups = groups
        self._max_workers = max_workers
        self._relay_plan = relay_plan

        # Maps group_id → group dict
        self._group_map: dict[str, dict] = {}
        # Forward adjacency: A → [B, C] means B and C depend on A
        self._adjacency: dict[str, list[str]] = {}
        # Reverse deps: B → [A] means B depends on A
        self._reverse_deps: dict[str, list[str]] = {}
        # In-degree for each group
        self._in_degree: dict[str, int] = {}
        # Topological order (computed during cycle detection)
        self._topo_order: list[str] = []
        # Populated by run() — needed for get_fallback_leaves()
        self._run_results: dict[str, GroupResult] = {}

        self._build_dag()

    def _build_dag(self) -> None:
        """Build the DAG data structures and validate no cycles exist."""
        # Build group_map
        for group in self._groups:
            gid = group.get("group_id", group.get("name", ""))
            if not gid:
                raise ValueError(f"Group missing group_id: {group}")
            if gid in self._group_map:
                raise ValueError(f"Duplicate group_id: {gid}")
            self._group_map[gid] = group

        all_ids = set(self._group_map.keys())

        # Initialize adjacency and in-degree
        for gid in all_ids:
            self._adjacency[gid] = []
            self._reverse_deps[gid] = []
            self._in_degree[gid] = 0

        # Build edges from depends_on
        for gid, group in self._group_map.items():
            deps = group.get("depends_on", []) or []
            for dep in deps:
                if dep not in all_ids:
                    raise ValueError(
                        f"Group '{gid}' depends on unknown group '{dep}'"
                    )
                # dep → gid (gid depends on dep)
                self._adjacency[dep].append(gid)
                self._reverse_deps[gid].append(dep)
                self._in_degree[gid] += 1

        # Cycle detection via Kahn's algorithm
        self._topo_order = self._kahn_topo_sort()
        if len(self._topo_order) < len(all_ids):
            raise ValueError(
                "Cycle detected in DAG: topological sort could not order all groups"
            )

    def _kahn_topo_sort(self) -> list[str]:
        """Perform Kahn's algorithm for topological sorting.

        Returns:
            List of group_ids in topological order.
            If shorter than total groups, a cycle exists.
        """
        in_degree = dict(self._in_degree)
        queue = deque(gid for gid, deg in in_degree.items() if deg == 0)
        order: list[str] = []

        while queue:
            node = queue.popleft()
            order.append(node)
            for downstream in self._adjacency[node]:
                in_degree[downstream] -= 1
                if in_degree[downstream] == 0:
                    queue.append(downstream)

        return order

    def _build_relay_context(
        self, group_id: str, completed: dict[str, GroupResult],
    ) -> RelayContext:
        """Build a RelayContext for a group from the relay plan and completed results.

        Called with the condition lock held before submitting a group for execution.
        When no relay_plan is set, returns a default (all-None) context.
        """
        if not self._relay_plan:
            return RelayContext()

        plan = self._relay_plan
        predecessor_id = plan.relay_map.get(group_id)
        is_fork = group_id in plan.fork_from
        fork_source_branch: str | None = None
        worktree_path: Path | None = None
        branch_name: str | None = None
        convergence_merges: list[str] = []

        if predecessor_id is not None and predecessor_id in completed:
            # Relay: reuse predecessor's worktree and branch
            pred_result = completed[predecessor_id]
            worktree_path = pred_result.worktree_path
            branch_name = pred_result.branch_name or None
        elif is_fork:
            # Fork: need a new worktree from predecessor's branch
            fork_pred_id = plan.fork_from[group_id]
            if fork_pred_id in completed and completed[fork_pred_id].branch_name:
                fork_source_branch = completed[fork_pred_id].branch_name

        # Convergence merges: collect secondary predecessor branches
        if group_id in plan.convergence_points:
            conv = plan.convergence_points[group_id]
            for sec_pred_id in conv.secondary_predecessors:
                if sec_pred_id in completed and completed[sec_pred_id].branch_name:
                    convergence_merges.append(completed[sec_pred_id].branch_name)

        return RelayContext(
            worktree_path=worktree_path,
            branch_name=branch_name,
            is_fork=is_fork,
            fork_source_branch=fork_source_branch,
            convergence_merges=convergence_merges,
        )

    def run(
        self,
        execute_fn: Callable[..., GroupResult],
    ) -> list[GroupResult]:
        """Execute all groups respecting dependency order.

        Args:
            execute_fn: Callable receiving ``(group_dict, deps_results,
                relay_context)`` and returning a :class:`GroupResult`.
                ``deps_results`` contains only the results of the group's
                direct dependencies.  ``relay_context`` describes how the
                group should acquire its worktree (relay, fork, or new).

        Returns:
            List of all GroupResults in topological order.
        """
        if not self._group_map:
            return []

        # Shared state protected by condition lock
        condition = threading.Condition()
        pending: set[str] = set(self._group_map.keys())
        running: set[str] = set()
        completed: dict[str, GroupResult] = {}
        failed: set[str] = set()
        skipped: dict[str, GroupResult] = {}

        def _get_deps_results(group_id: str) -> dict[str, GroupResult]:
            """Extract dependency results for a group (caller holds lock)."""
            deps = self._reverse_deps.get(group_id, [])
            return {dep: completed[dep] for dep in deps if dep in completed}

        def _propagate_failure_locked(group_id: str) -> None:
            """Mark all downstream groups as skipped (caller holds lock)."""
            queue = deque(self._adjacency.get(group_id, []))
            while queue:
                downstream = queue.popleft()
                if downstream in skipped or downstream in failed:
                    continue
                if downstream in pending:
                    pending.discard(downstream)
                result = GroupResult.skipped(downstream)
                skipped[downstream] = result
                logger.info(
                    "Group '%s' skipped due to upstream failure of '%s'",
                    downstream, group_id,
                )
                # Propagate further
                queue.extend(self._adjacency.get(downstream, []))

        def _submit_ready(executor: ThreadPoolExecutor) -> None:
            """Submit all groups whose deps are satisfied (caller holds lock)."""
            ready = []
            for gid in list(pending):
                deps = self._reverse_deps.get(gid, [])
                if all(dep in completed for dep in deps):
                    ready.append(gid)

            for gid in ready:
                pending.discard(gid)
                running.add(gid)
                deps_results = _get_deps_results(gid)
                relay_context = self._build_relay_context(gid, completed)
                group_dict = self._group_map[gid]
                future = executor.submit(
                    execute_fn, group_dict, deps_results, relay_context,
                )
                future.add_done_callback(
                    lambda f, g=gid: _on_complete(g, f, executor)
                )
                logger.info("Submitted group '%s' for execution", gid)

        def _on_complete(
            group_id: str,
            future: Future,
            executor: ThreadPoolExecutor,
        ) -> None:
            """Callback when a group finishes execution."""
            with condition:
                running.discard(group_id)

                exc = future.exception()
                if exc is not None:
                    # Group raised an exception
                    error_msg = f"{type(exc).__name__}: {exc}"
                    logger.error(
                        "Group '%s' failed with exception: %s",
                        group_id, error_msg,
                    )
                    result = GroupResult.failed(group_id, error_msg)
                    failed.add(group_id)
                    completed[group_id] = result
                    _propagate_failure_locked(group_id)
                else:
                    result = future.result()
                    if result.status == "failed":
                        logger.error(
                            "Group '%s' returned failed status: %s",
                            group_id, result.error,
                        )
                        failed.add(group_id)
                        completed[group_id] = result
                        _propagate_failure_locked(group_id)
                    else:
                        logger.info(
                            "Group '%s' completed successfully", group_id,
                        )
                        completed[group_id] = result

                # Check for newly-unblocked groups
                _submit_ready(executor)

                # Wake the main thread
                condition.notify_all()

        # Main scheduling loop
        with ThreadPoolExecutor(max_workers=self._max_workers) as executor:
            with condition:
                # Submit all root groups (in_degree == 0)
                _submit_ready(executor)

                # Wait until all groups are resolved
                while pending or running:
                    condition.wait(timeout=1.0)

        # Store results for get_fallback_leaves()
        self._run_results = {**completed, **skipped}

        # Build results in topological order
        results: list[GroupResult] = []
        for gid in self._topo_order:
            if gid in completed:
                results.append(completed[gid])
            elif gid in skipped:
                results.append(skipped[gid])
            else:
                # Should not happen, but be defensive
                results.append(GroupResult.failed(gid, "Unknown state"))

        return results

    def get_fallback_leaves(self) -> list[str]:
        """Return group_ids of completed groups that should be merged as fallback leaves.

        After a partial failure, some completed groups may have no completed
        downstream (all their downstream groups failed or were skipped).
        These groups' branches contain valid work that should be preserved
        by merging back to the original branch.

        Normal leaf nodes (from relay_plan.leaf_nodes) are excluded since
        they are already handled by the standard leaf merge path.

        Must be called after :meth:`run`.
        """
        if not self._run_results:
            return []

        completed_ids = {
            gid for gid, r in self._run_results.items()
            if r.status == "completed"
        }

        # Exclude normal leaf nodes — they get merged via the standard path
        leaf_nodes = self._relay_plan.leaf_nodes if self._relay_plan else set()

        fallback: list[str] = []
        for gid in completed_ids:
            if gid in leaf_nodes:
                continue
            downstream = self._adjacency.get(gid, [])
            if not downstream:
                # No downstream and not in leaf_nodes — shouldn't happen,
                # but treat as fallback leaf to be safe
                fallback.append(gid)
                continue
            # All direct downstream are non-completed → this group is a fallback leaf
            if all(d not in completed_ids for d in downstream):
                fallback.append(gid)

        return sorted(fallback)

    def topological_merge_order(self) -> list[str]:
        """Return group_ids in topological order, filtered to completed only.

        Used to determine the order in which branches should be merged
        back to the main branch.
        """
        # This is called after run(), so we use the results from run.
        # However, we return the full topo_order here and let the caller
        # filter by status. For convenience, we store completed state.
        return list(self._topo_order)
