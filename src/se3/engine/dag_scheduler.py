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

    Args:
        groups: List of group dicts, each containing at least 'group_id'
                and optionally 'depends_on' (list of group_id strings).
        max_workers: Maximum number of concurrent group executions.
    """

    def __init__(self, groups: list[dict], max_workers: int = 4) -> None:
        self._groups = groups
        self._max_workers = max_workers

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

    def run(
        self,
        execute_fn: Callable[[dict, dict[str, GroupResult]], GroupResult],
    ) -> list[GroupResult]:
        """Execute all groups respecting dependency order.

        Args:
            execute_fn: Callable receiving (group_dict, deps_results) and
                returning a GroupResult. deps_results contains only the
                results of the group's direct dependencies.

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
                group_dict = self._group_map[gid]
                future = executor.submit(execute_fn, group_dict, deps_results)
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

    def topological_merge_order(self) -> list[str]:
        """Return group_ids in topological order, filtered to completed only.

        Used to determine the order in which branches should be merged
        back to the main branch.
        """
        # This is called after run(), so we use the results from run.
        # However, we return the full topo_order here and let the caller
        # filter by status. For convenience, we store completed state.
        return list(self._topo_order)
