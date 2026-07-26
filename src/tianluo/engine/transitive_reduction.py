"""Transitive reduction for dependency DAGs.

Removes redundant edges from a directed acyclic graph of task groups.
An edge u→v is redundant if there exists a longer path from u to v
through other nodes.
"""

from __future__ import annotations

import copy
from collections import deque


def transitive_reduce(groups: list[dict]) -> list[dict]:
    """Return a copy of *groups* with redundant ``depends_on`` edges removed.

    For each group *v* and each direct dependency *u* in ``v["depends_on"]``,
    if there is a path from *u* to *v* of length > 1 (i.e. through at least
    one intermediate node), the edge u→v is redundant and is removed.

    Args:
        groups: List of group dicts, each containing at least ``group_id``
            (str) and ``depends_on`` (list[str]).  Other keys are preserved
            unchanged.

    Returns:
        A deep copy of *groups* with ``depends_on`` lists reduced.
        The original input is never mutated.
    """
    if not groups:
        return []

    reduced = copy.deepcopy(groups)

    # Build forward adjacency from depends_on (u → v means v depends on u).
    adjacency: dict[str, set[str]] = {}
    for g in groups:
        gid = g["group_id"]
        adjacency.setdefault(gid, set())
    for g in groups:
        for dep in g.get("depends_on", []):
            adjacency.setdefault(dep, set())
            adjacency[dep].add(g["group_id"])

    for rg in reduced:
        vid = rg["group_id"]
        deps = rg.get("depends_on", [])
        if len(deps) <= 1:
            continue

        redundant: set[str] = set()
        for u in deps:
            # BFS/BFS from u; if we can reach v via a path of length > 1
            # (i.e. not directly), then u→v is redundant.
            if _has_long_path(u, vid, adjacency):
                redundant.add(u)

        if redundant:
            rg["depends_on"] = [d for d in deps if d not in redundant]

    return reduced


def _has_long_path(
    source: str, target: str, adjacency: dict[str, set[str]]
) -> bool:
    """Return True if *adjacency* contains a path source→…→target of length ≥ 2."""
    # BFS from source, skipping the direct source→target edge.
    visited: set[str] = {source}
    queue: deque[str] = deque()

    for neighbor in adjacency.get(source, ()):
        if neighbor == target:
            # Skip the direct edge — we're looking for longer paths.
            continue
        if neighbor not in visited:
            visited.add(neighbor)
            queue.append(neighbor)

    while queue:
        node = queue.popleft()
        for neighbor in adjacency.get(node, ()):
            if neighbor == target:
                return True
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(neighbor)

    return False
