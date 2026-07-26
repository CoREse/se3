"""TaskFormatter for formatting task data structures.

Transforms raw task structures from the plan_tasks step into rich,
human-readable formatted output using Rich library components.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set

if TYPE_CHECKING:
    from ..dag_scheduler import RelayPlan

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from ..display import _reverse_footer, _reverse_title, get_console

logger = logging.getLogger(__name__)


def _md_heading(title: str, color: str) -> Text:
    """Build a reverse-color block heading (``[reverse] ## Title [/reverse]``)."""
    return _reverse_title(title, color)


def _heading_group(title: str, color: str, *body: RenderableType) -> Group:
    """Wrap ``body`` with a reverse-color heading and matching footer."""
    return Group(
        _reverse_title(title, color),
        Text(""),
        *body,
        Text(""),
        _reverse_footer(color),
        Text(""),
    )


class TaskValidationError(Exception):
    """Raised when task data validation fails."""

    pass


class TaskDataValidator:
    """Validates task data structure before formatting."""

    REQUIRED_TASK_FIELDS = {"id", "description"}
    OPTIONAL_FIELDS = {"complexity", "dependencies", "verification_criteria", "files", "depends_on"}
    VALID_COMPLEXITY = {"small", "medium", "large"}

    @classmethod
    def validate(cls, tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Validate a list of tasks.

        Args:
            tasks: List of task dictionaries to validate

        Returns:
            Validated and normalized list of tasks

        Raises:
            TaskValidationError: If validation fails
        """
        if not isinstance(tasks, list):
            raise TaskValidationError(f"Expected list of tasks, got {type(tasks).__name__}")

        validated_tasks = []
        task_ids = set()

        for i, task in enumerate(tasks):
            try:
                validated_task = cls._validate_task(task, i)
                task_id = validated_task.get("id")

                if task_id in task_ids:
                    raise TaskValidationError(f"Duplicate task ID: {task_id}")

                task_ids.add(task_id)
                validated_tasks.append(validated_task)

            except TaskValidationError as e:
                raise TaskValidationError(f"Task at index {i}: {e}") from e

        # Validate dependencies after all tasks are processed
        cls._validate_dependencies(validated_tasks, task_ids)

        return validated_tasks

    @classmethod
    def _validate_task(cls, task: Dict[str, Any], index: int) -> Dict[str, Any]:
        """Validate a single task.

        Args:
            task: Task dictionary to validate
            index: Index of the task in the list (for error messages)

        Returns:
            Validated and normalized task dictionary

        Raises:
            TaskValidationError: If validation fails
        """
        if not isinstance(task, dict):
            raise TaskValidationError(f"Expected dict, got {type(task).__name__}")

        # Check required fields
        missing_fields = cls.REQUIRED_TASK_FIELDS - set(task.keys())
        if missing_fields:
            raise TaskValidationError(f"Missing required fields: {missing_fields}")

        # Create normalized task with defaults
        validated = {
            "id": task["id"],
            "description": task["description"],
        }

        # Validate and set complexity
        complexity = task.get("complexity", "medium").lower()
        if complexity not in cls.VALID_COMPLEXITY:
            logger.warning(f"Invalid complexity '{complexity}', using 'medium'")
            complexity = "medium"
        validated["complexity"] = complexity

        # Set optional fields with defaults
        validated["dependencies"] = task.get("dependencies", [])
        validated["verification_criteria"] = task.get("verification_criteria", [])
        validated["files"] = task.get("files", [])
        validated["depends_on"] = task.get("depends_on", [])

        return validated

    @classmethod
    def _validate_dependencies(cls, tasks: List[Dict[str, Any]], task_ids: Set[Any]) -> None:
        """Validate task dependencies.

        Args:
            tasks: List of validated tasks
            task_ids: Set of valid task IDs

        Raises:
            TaskValidationError: If dependencies are invalid
        """
        for task in tasks:
            task_id = task.get("id")

            # Check dependencies field
            deps = task.get("dependencies", [])
            if not isinstance(deps, list):
                raise TaskValidationError(f"Task {task_id}: dependencies must be a list")

            for dep_id in deps:
                if dep_id not in task_ids:
                    raise TaskValidationError(f"Task {task_id}: dependency '{dep_id}' not found")

            # Check depends_on field (for task groups)
            depends_on = task.get("depends_on", [])
            if not isinstance(depends_on, list):
                raise TaskValidationError(f"Task {task_id}: depends_on must be a list")

        # Check for circular dependencies using DFS
        cls._check_circular_dependencies(tasks)

    @classmethod
    def _check_circular_dependencies(cls, tasks: List[Dict[str, Any]]) -> None:
        """Check for circular dependencies using DFS.

        Args:
            tasks: List of validated tasks

        Raises:
            TaskValidationError: If circular dependencies are found
        """
        # Build adjacency list
        graph = {task["id"]: set(task.get("dependencies", [])) for task in tasks}

        # DFS to detect cycles
        WHITE, GRAY, BLACK = 0, 1, 2
        color = {task_id: WHITE for task_id in graph}

        def dfs(node: Any, path: List[Any]) -> None:
            color[node] = GRAY
            path.append(node)

            for neighbor in graph.get(node, set()):
                if color[neighbor] == GRAY:
                    # Found cycle
                    cycle_start = path.index(neighbor)
                    cycle = path[cycle_start:] + [neighbor]
                    cycle_str = " -> ".join(str(n) for n in cycle)
                    raise TaskValidationError(f"Circular dependency detected: {cycle_str}")
                elif color[neighbor] == WHITE:
                    dfs(neighbor, path)

            path.pop()
            color[node] = BLACK

        # Run DFS from each unvisited node
        for task_id in graph:
            if color[task_id] == WHITE:
                dfs(task_id, [])


class TaskFormatter:
    """Formats task data structures into rich output."""

    # Complexity color mapping
    COMPLEXITY_COLORS = {
        "small": "green",
        "medium": "yellow",
        "large": "red",
    }

    # Complexity icons
    COMPLEXITY_ICONS = {
        "small": "✓",
        "medium": "◐",
        "large": "◉",
    }

    def __init__(self, console=None):
        """Initialize the TaskFormatter.

        Args:
            console: Optional Rich console instance
        """
        self.console = console or get_console()
        self.validator = TaskDataValidator()

    def format_tasks(self, task_groups: List[Dict[str, Any]], mode: str = "tree") -> RenderableType:
        """Format task groups into rich output.

        Args:
            task_groups: List of task group dictionaries
            mode: Display mode - "tree" for hierarchical view or "table" for flat table

        Returns:
            Rich renderable (Group) containing the formatted output with a
            markdown-style ``## Task Plan`` heading.
        """
        if not task_groups:
            return _heading_group(
                "Task Plan", "blue", Text.from_markup("[dim]No tasks to display[/dim]"),
            )

        if mode == "tree":
            return self._format_tree_view(task_groups)
        elif mode == "table":
            return self._format_table_view(task_groups)
        else:
            return _heading_group(
                "Task Plan", "red", Text.from_markup(f"[red]Unknown mode: {mode}[/red]"),
            )

    def _format_tree_view(self, task_groups: List[Dict[str, Any]]) -> RenderableType:
        """Format task groups as a hierarchical tree.

        Args:
            task_groups: List of task group dictionaries

        Returns:
            Rich renderable containing the tree view under a ``## Task Plan`` heading.
        """
        total_tasks = 0
        total_complexity = {"small": 0, "medium": 0, "large": 0}

        # Create the root tree
        tree = Tree("[bold cyan]Task Plan[/bold cyan]")

        for group in task_groups:
            group_id = group.get("group_id", "G?")
            name = group.get("name", "Unnamed Group")
            description = group.get("description", "")
            depends_on = group.get("depends_on", [])
            tasks = group.get("tasks", [])

            # Build group label
            group_label = f"[bold]{group_id}[/bold]: {name}"
            if description:
                group_label += f"\n[dim]{description}[/dim]"

            # Add group node to tree
            group_branch = tree.add(group_label)

            # Show dependencies if present
            if depends_on:
                deps_str = ", ".join(f"[yellow]{d}[/yellow]" for d in depends_on)
                group_branch.add(f"[dim]→ Depends on: {deps_str}[/dim]")

            # Add tasks to group
            for task in tasks:
                total_tasks += 1
                task_id = task.get("id", "?")
                description = task.get("description", "No description")
                complexity = task.get("complexity", "medium").lower()
                task_deps = task.get("dependencies", [])
                criteria = task.get("verification_criteria", [])
                files = task.get("files", [])

                total_complexity[complexity] += 1

                # Build task node
                color = self.COMPLEXITY_COLORS.get(complexity, "white")
                icon = self.COMPLEXITY_ICONS.get(complexity, "•")

                task_label = f"[bold]{task_id}[/bold]: {description}"
                task_node = group_branch.add(task_label)

                # Add complexity badge
                task_node.add(f"[{color}]{icon} {complexity.capitalize()}[/{color}]")

                # Show files if present
                if files:
                    files_str = ", ".join(f"[blue]{f}[/blue]" for f in files[:3])
                    if len(files) > 3:
                        files_str += f" [dim]+{len(files) - 3} more[/dim]"
                    task_node.add(f"[dim]📄 {files_str}[/dim]")

                # Show task dependencies if present
                if task_deps:
                    deps_str = ", ".join(str(d) for d in task_deps)
                    task_node.add(f"[dim]→ Depends on: {deps_str}[/dim]")

                # Show verification criteria if present (condensed)
                if criteria:
                    criteria_count = len(criteria)
                    task_node.add(f"[dim]✓ {criteria_count} verification criteria[/dim]")

        # Build summary
        summary_parts = []
        summary_parts.append(f"[bold]{len(task_groups)}[/bold] groups, [bold]{total_tasks}[/bold] tasks")
        complexity_parts = []
        for comp, count in total_complexity.items():
            if count > 0:
                color = self.COMPLEXITY_COLORS.get(comp, "white")
                complexity_parts.append(f"[{color}]{count} {comp}[/{color}]")
        if complexity_parts:
            summary_parts.append(" | " + ", ".join(complexity_parts))

        summary = "".join(summary_parts)
        tree.add(f"\n[dim]{summary}[/dim]")

        return _heading_group("Task Plan", "blue", tree)

    def _format_table_view(self, task_groups: List[Dict[str, Any]]) -> RenderableType:
        """Format task groups as a flat table.

        Args:
            task_groups: List of task group dictionaries

        Returns:
            Rich renderable containing the table view under a ``## Task Plan`` heading.
        """
        table = Table(title="Task Plan", show_header=True, header_style="bold cyan")

        # Define columns
        table.add_column("ID", style="bold", width=6)
        table.add_column("Group", style="dim", width=10)
        table.add_column("Description", width=40)
        table.add_column("Complexity", width=10, justify="center")
        table.add_column("Deps", width=6, justify="center")
        table.add_column("Criteria", width=8, justify="center")

        total_tasks = 0

        for group in task_groups:
            group_id = group.get("group_id", "G?")
            tasks = group.get("tasks", [])

            for task in tasks:
                total_tasks += 1
                task_id = str(task.get("id", "?"))
                description = task.get("description", "No description")
                complexity = task.get("complexity", "medium").lower()
                task_deps = task.get("dependencies", [])
                criteria = task.get("verification_criteria", [])

                # Truncate description if too long
                if len(description) > 37:
                    description = description[:34] + "..."

                # Get color based on complexity
                color = self.COMPLEXITY_COLORS.get(complexity, "white")
                complexity_text = f"[{color}]{complexity.capitalize()}[/{color}]"

                table.add_row(
                    task_id,
                    group_id,
                    description,
                    complexity_text,
                    str(len(task_deps)) if task_deps else "-",
                    str(len(criteria)) if criteria else "-",
                )

        return _heading_group(f"Task Plan ({total_tasks} tasks)", "blue", table)

    def format_task_detail(self, task: Dict[str, Any]) -> RenderableType:
        """Format detailed view of a single task.

        Args:
            task: Task dictionary

        Returns:
            Rich renderable containing the task detail under a
            ``## Task <id>`` heading.
        """
        table = Table(show_header=False, header_style="bold")
        table.add_column("Field", style="bold cyan", width=20)
        table.add_column("Value", width=60)

        # Basic info
        table.add_row("ID", str(task.get("id", "N/A")))
        table.add_row("Description", task.get("description", "N/A"))

        # Complexity with color
        complexity = task.get("complexity", "medium").lower()
        color = self.COMPLEXITY_COLORS.get(complexity, "white")
        table.add_row("Complexity", f"[{color}]{complexity.capitalize()}[/{color}]")

        # Files
        files = task.get("files", [])
        if files:
            files_str = "\n".join(f"• {f}" for f in files)
            table.add_row("Files", files_str)

        # Dependencies
        deps = task.get("dependencies", [])
        if deps:
            deps_str = ", ".join(str(d) for d in deps)
            table.add_row("Dependencies", deps_str)

        # Verification criteria
        criteria = task.get("verification_criteria", [])
        if criteria:
            criteria_str = "\n".join(f"{i+1}. {c}" for i, c in enumerate(criteria))
            table.add_row("Verification Criteria", criteria_str)

        # Build heading: reverse-color block + standalone complexity icon
        task_id = task.get("id", "Unknown")
        complexity_icon = self.COMPLEXITY_ICONS.get(complexity, "•")
        heading_line = Text.assemble(
            _reverse_title(f"Task {task_id}", "blue"),
            Text(f" {complexity_icon}", style=color),
        )
        return Group(
            heading_line,
            Text(""),
            table,
            Text(""),
            _reverse_footer("blue"),
            Text(""),
        )

    def format_summary(self, task_groups: List[Dict[str, Any]]) -> RenderableType:
        """Format summary statistics for task groups.

        Args:
            task_groups: List of task group dictionaries

        Returns:
            Rich renderable containing the summary statistics under a
            ``## Task Summary`` heading.
        """
        total_groups = len(task_groups)
        total_tasks = 0
        complexity_counts = {"small": 0, "medium": 0, "large": 0}
        total_deps = 0
        total_criteria = 0

        for group in task_groups:
            tasks = group.get("tasks", [])
            total_tasks += len(tasks)

            for task in tasks:
                complexity = task.get("complexity", "medium").lower()
                if complexity in complexity_counts:
                    complexity_counts[complexity] += 1

                deps = task.get("dependencies", [])
                if deps:
                    total_deps += len(deps)

                criteria = task.get("verification_criteria", [])
                if criteria:
                    total_criteria += len(criteria)

        # Build summary table
        table = Table(show_header=False, header_style="bold")
        table.add_column("Metric", style="bold cyan", width=20)
        table.add_column("Value", width=40)

        table.add_row("Task Groups", str(total_groups))
        table.add_row("Total Tasks", str(total_tasks))

        # Complexity breakdown with colors
        complexity_parts = []
        for comp, count in complexity_counts.items():
            if count > 0:
                color = self.COMPLEXITY_COLORS.get(comp, "white")
                complexity_parts.append(f"[{color}]{count} {comp}[/{color}]")

        if complexity_parts:
            table.add_row("Complexity", " | ".join(complexity_parts))

        if total_deps > 0:
            table.add_row("Total Dependencies", str(total_deps))

        if total_criteria > 0:
            table.add_row("Total Criteria", str(total_criteria))

        # Estimate effort text
        avg_complexity = self._calculate_avg_complexity(total_tasks, complexity_counts)
        effort_estimate = self._estimate_effort(total_tasks, avg_complexity)
        table.add_row("Est. Effort", effort_estimate)

        return _heading_group("Task Summary", "green", table)

    def _calculate_avg_complexity(self, total: int, counts: Dict[str, int]) -> float:
        """Calculate average complexity score.

        Args:
            total: Total number of tasks
            counts: Dictionary of complexity counts

        Returns:
            Average complexity score (1.0=small, 2.0=medium, 3.0=large)
        """
        if total == 0:
            return 0.0

        weights = {"small": 1.0, "medium": 2.0, "large": 3.0}
        total_weight = sum(counts[c] * weights[c] for c in counts)
        return total_weight / total

    def _estimate_effort(self, total_tasks: int, avg_complexity: float) -> str:
        """Estimate effort based on tasks and complexity.

        Args:
            total_tasks: Total number of tasks
            avg_complexity: Average complexity score

        Returns:
            Effort estimate string
        """
        if total_tasks == 0:
            return "N/A"

        # Rough estimation: small=0.5h, medium=2h, large=4h
        hours = total_tasks * avg_complexity

        if hours < 1:
            return "< 1 hour"
        elif hours < 8:
            return f"~{int(hours)} hours"
        elif hours < 40:
            days = hours / 8
            return f"~{days:.1f} days"
        else:
            weeks = hours / 40
            return f"~{weeks:.1f} weeks"

    def format_implement_plan(
        self,
        task_groups: List[Dict[str, Any]],
        execution_strategy: str,
        total_loc: int,
        loc_threshold: int,
        relay_plan: Optional[RelayPlan] = None,
        sequential_reason: Optional[str] = None,
    ) -> RenderableType:
        """Format implement step task plan with execution strategy summary.

        Args:
            task_groups: List of task group dictionaries
            execution_strategy: One of 'single', 'dag_parallel', 'sequential'
            total_loc: Total estimated lines of code across all tasks
            loc_threshold: LOC threshold for group merging
            relay_plan: Relay execution plan (only for dag_parallel strategy)
            sequential_reason: Optional human-readable reason for choosing
                the sequential strategy. Only surfaced when
                ``execution_strategy == "sequential"``. Typical values are
                ``"use_worktree=False"`` or ``"linear chain"``.

        Returns:
            Rich renderable (Group) containing the implementation plan under
            a ``## Implementation Plan`` heading.
        """
        renderables: list[RenderableType] = [
            _md_heading("Implementation Plan", "blue"),
            Text(""),
        ]

        # Strategy summary line
        strategy_line = self._format_strategy_line(
            execution_strategy, total_loc, loc_threshold, len(task_groups),
            sequential_reason=sequential_reason,
        )
        renderables.append(strategy_line)
        renderables.append(Text(""))  # blank separator

        # Task tree (reuse existing tree logic)
        if task_groups:
            tree = self._build_implement_tree(task_groups)
            renderables.append(tree)
        else:
            renderables.append(Text.from_markup("[dim]No tasks to display[/dim]"))

        # DAG topology diagram (only for dag_parallel with relay_plan)
        if (
            execution_strategy == "dag_parallel"
            and relay_plan is not None
            and len(task_groups) > 1
        ):
            try:
                topology = self._build_dag_topology(task_groups, relay_plan)
                if topology.plain.strip():
                    renderables.append(Text(""))  # blank separator
                    renderables.append(topology)
            except Exception:
                logger.debug("Could not render DAG topology", exc_info=True)

        # LOC statistics at bottom
        loc_summary = self._format_loc_summary(task_groups, total_loc)
        renderables.append(Text(""))  # blank separator
        renderables.append(loc_summary)
        renderables.append(Text(""))  # blank separator before footer
        renderables.append(_reverse_footer("blue"))
        renderables.append(Text(""))  # trailing blank

        return Group(*renderables)

    def _format_strategy_line(
        self,
        strategy: str,
        total_loc: int,
        loc_threshold: int,
        num_groups: int,
        sequential_reason: Optional[str] = None,
    ) -> Text:
        """Build the execution strategy summary line.

        When ``strategy == "sequential"`` and ``sequential_reason`` is
        provided, the reason is appended to the description so users can
        see *why* DAG parallel was not selected (e.g. short-circuited
        because ``implement.use_worktree=False`` or the RelayPlan was a
        linear chain).
        """
        from rich.text import Text

        sequential_desc = f"Sequential ({num_groups} groups)"
        if strategy == "sequential" and sequential_reason:
            sequential_desc = (
                f"Sequential ({num_groups} groups, reason: {sequential_reason})"
            )

        strategy_map = {
            "single": (
                "\u26a1",  # ⚡
                f"Single group \u2192 single LLM call ({total_loc} LOC)"
                if loc_threshold == 0
                else f"Single LLM call ({total_loc} LOC \u2264 {loc_threshold} threshold)",
            ),
            "dag_parallel": (
                "\U0001f500",  # 🔀
                f"DAG parallel ({total_loc} LOC > {loc_threshold} threshold, {num_groups} groups)",
            ),
            "sequential": (
                "\U0001f4cb",  # 📋
                sequential_desc,
            ),
        }

        icon, desc = strategy_map.get(
            strategy,
            ("\u2022", f"{strategy} ({num_groups} groups)"),
        )

        text = Text()
        text.append(f" {icon} ", style="bold")
        text.append("Strategy: ", style="bold cyan")
        text.append(desc)
        return text

    def _build_implement_tree(self, task_groups: List[Dict[str, Any]]) -> Tree:
        """Build a task tree for implement plan display."""
        tree = Tree("[bold cyan]Task Groups[/bold cyan]")

        for group in task_groups:
            group_id = group.get("group_id", "G?")
            name = group.get("name", "Unnamed Group")
            depends_on = group.get("depends_on", [])
            tasks = group.get("tasks", [])

            # Group label with task count
            group_label = f"[bold]{group_id}[/bold]: {name} [dim]({len(tasks)} tasks)[/dim]"
            group_branch = tree.add(group_label)

            if depends_on:
                deps_str = ", ".join(f"[yellow]{d}[/yellow]" for d in depends_on)
                group_branch.add(f"[dim]\u2192 Depends on: {deps_str}[/dim]")

            for task in tasks:
                task_id = task.get("id", "?")
                description = task.get("description", "No description")
                complexity = task.get("complexity", "medium").lower()
                estimated_loc = task.get("estimated_loc", 50)
                files = task.get("files", [])

                color = self.COMPLEXITY_COLORS.get(complexity, "white")
                icon = self.COMPLEXITY_ICONS.get(complexity, "\u2022")

                task_label = (
                    f"[bold]{task_id}[/bold]: {description} "
                    f"[{color}]{icon} {complexity}[/{color}] "
                    f"[dim]~{estimated_loc} LOC[/dim]"
                )
                task_node = group_branch.add(task_label)

                if files:
                    files_str = ", ".join(f"[blue]{f}[/blue]" for f in files[:3])
                    if len(files) > 3:
                        files_str += f" [dim]+{len(files) - 3} more[/dim]"
                    task_node.add(f"[dim]\U0001f4c4 {files_str}[/dim]")

        return tree

    @staticmethod
    def _compute_topo_waves(groups: List[Dict[str, Any]]) -> List[List[str]]:
        """Compute topological wave layers using Kahn's algorithm.

        Each wave contains groups that can execute in parallel.
        Groups are sorted within each wave by ``group_order``.
        """
        order_map = {g["group_id"]: g.get("group_order", 0) for g in groups}
        all_ids = set(order_map)

        in_deg: dict[str, int] = {gid: 0 for gid in all_ids}
        fwd: dict[str, list[str]] = {gid: [] for gid in all_ids}

        for g in groups:
            gid = g["group_id"]
            for dep in g.get("depends_on", []):
                if dep in all_ids:
                    fwd[dep].append(gid)
                    in_deg[gid] += 1

        waves: list[list[str]] = []
        queue = sorted(
            [gid for gid, d in in_deg.items() if d == 0],
            key=lambda gid: order_map[gid],
        )

        while queue:
            waves.append(queue)
            nxt: list[str] = []
            for gid in queue:
                for child in fwd[gid]:
                    in_deg[child] -= 1
                    if in_deg[child] == 0:
                        nxt.append(child)
            queue = sorted(nxt, key=lambda gid: order_map[gid])

        return waves

    def _build_dag_topology(
        self,
        groups: List[Dict[str, Any]],
        relay_plan: RelayPlan,
    ) -> "Text":
        """Build a layered DAG execution topology diagram.

        Shows execution waves with relay/fork/merge annotations and
        LLM call numbering.  Returns empty ``Text`` when there is
        only one group (topology adds no information).
        """
        from rich.text import Text

        if len(groups) <= 1:
            return Text("")

        group_map = {g["group_id"]: g for g in groups}
        waves = self._compute_topo_waves(groups)
        if not waves:
            return Text("")

        # Assign sequential LLM call numbers
        call_num: dict[str, int] = {}
        n = 1
        for wave in waves:
            for gid in wave:
                call_num[gid] = n
                n += 1

        text = Text()
        text.append("  Execution Topology\n", style="bold cyan underline")

        for wi, wave in enumerate(waves):
            # Wave header with LLM call numbers
            calls = ", ".join(f"#{call_num[gid]}" for gid in wave)
            text.append(f"\n  Wave {wi + 1}", style="bold")
            text.append(f"  LLM {calls}\n", style="dim cyan")
            text.append("  " + "\u2500" * 42 + "\n", style="dim")

            for gid in wave:
                name = group_map.get(gid, {}).get("name", "")
                is_root = gid in relay_plan.root_nodes
                is_leaf = gid in relay_plan.leaf_nodes

                # Node marker: ● root, ◆ leaf, ○ middle
                if is_root:
                    text.append("    \u25cf ", style="bold green")
                elif is_leaf:
                    text.append("    \u25c6 ", style="bold yellow")
                else:
                    text.append("    \u25cb ", style="bold blue")

                text.append(gid, style="bold")
                text.append(f"  {name}\n")

                # Relationship annotation
                if is_root:
                    text.append("      new worktree\n", style="dim green")
                else:
                    pred = relay_plan.relay_map.get(gid)
                    if pred is not None:
                        text.append("      \u2192 relay ", style="blue")
                        text.append(f"from {pred} (reuse worktree)\n", style="dim")
                    elif gid in relay_plan.fork_from:
                        src = relay_plan.fork_from[gid]
                        text.append("      \u2442 fork ", style="magenta")
                        text.append(f"from {src} (new branch)\n", style="dim")

                # Convergence merge
                if gid in relay_plan.convergence_points:
                    sec = relay_plan.convergence_points[gid].secondary_predecessors
                    text.append(
                        f"      \u2295 merge \u2190 {', '.join(sec)}\n",
                        style="yellow",
                    )

                # Leaf merge-back
                if is_leaf:
                    text.append("      \u21a9 merge-back\n", style="dim yellow")

            # Connector between waves
            if wi < len(waves) - 1:
                text.append("      \u2502\n", style="dim")
                text.append("      \u25bc\n", style="dim")

        return text

    def _format_loc_summary(
        self, task_groups: List[Dict[str, Any]], total_loc: int,
    ) -> "Text":
        """Build LOC statistics line."""
        from rich.text import Text

        # Per-group LOC distribution
        parts = []
        for group in task_groups:
            group_id = group.get("group_id", "G?")
            group_loc = sum(
                t.get("estimated_loc", 50)
                for t in group.get("tasks", [])
                if isinstance(t, dict)
            )
            parts.append(f"{group_id}: ~{group_loc}")

        text = Text()
        text.append(" Total: ", style="bold")
        text.append(f"~{total_loc} LOC", style="cyan")
        if parts:
            text.append("  (", style="dim")
            text.append(" | ".join(parts), style="dim")
            text.append(")", style="dim")
        return text

    def format_dependencies(self, task_groups: List[Dict[str, Any]]) -> RenderableType:
        """Format dependency map for task groups.

        Args:
            task_groups: List of task group dictionaries

        Returns:
            Rich renderable containing the dependency map under a
            ``## Dependencies`` heading.
        """
        # Build dependency graph
        all_tasks = {}
        for group in task_groups:
            for task in group.get("tasks", []):
                task_id = task.get("id")
                if task_id:
                    all_tasks[task_id] = {
                        "description": task.get("description", "")[:40],
                        "deps": task.get("dependencies", []),
                        "group": group.get("group_id", "?"),
                    }

        if not all_tasks:
            return _heading_group(
                "Dependencies", "blue",
                Text.from_markup("[dim]No tasks with dependencies[/dim]"),
            )

        # Build dependency table
        table = Table(show_header=True, header_style="bold cyan")
        table.add_column("Task ID", style="bold", width=10)
        table.add_column("Group", style="dim", width=8)
        table.add_column("Description", width=35)
        table.add_column("Depends On", width=25)
        table.add_column("Dependents", width=20)

        # Calculate dependents
        dependents = {task_id: [] for task_id in all_tasks}
        for task_id, info in all_tasks.items():
            for dep in info["deps"]:
                if dep in dependents:
                    dependents[dep].append(task_id)

        # Add rows
        for task_id, info in sorted(all_tasks.items(), key=lambda x: str(x[0])):
            deps_str = ", ".join(str(d) for d in info["deps"]) if info["deps"] else "-"
            dependents_str = ", ".join(str(d) for d in dependents[task_id]) if dependents[task_id] else "-"

            table.add_row(
                str(task_id),
                info["group"],
                info["description"][:37] + "..." if len(info["description"]) > 37 else info["description"],
                deps_str,
                dependents_str,
            )

        # Count stats
        tasks_with_deps = sum(1 for t in all_tasks.values() if t["deps"])
        total_deps = sum(len(t["deps"]) for t in all_tasks.values())

        title = f"Dependencies ({tasks_with_deps} tasks, {total_deps} deps)"
        return _heading_group(title, "cyan", table)


def format_task_groups(
    task_groups: List[Dict[str, Any]],
    mode: str = "tree",
    show_summary: bool = True,
    show_dependencies: bool = False,
) -> RenderableType:
    """Convenience function to format task groups with optional summary.

    Args:
        task_groups: List of task group dictionaries
        mode: Display mode - "tree" or "table"
        show_summary: Whether to include summary section
        show_dependencies: Whether to include dependencies section

    Returns:
        Rich renderable containing the formatted output. When multiple
        sections are requested they are combined under a
        ``## Task Plan Complete View`` heading.
    """
    formatter = TaskFormatter()

    sections: list[RenderableType] = []

    # Main task display
    if mode == "tree":
        sections.append(formatter.format_tasks(task_groups, mode="tree"))
    else:
        sections.append(formatter.format_tasks(task_groups, mode="table"))

    # Summary section
    if show_summary:
        sections.append(formatter.format_summary(task_groups))

    # Dependencies section
    if show_dependencies:
        sections.append(formatter.format_dependencies(task_groups))

    if len(sections) == 1:
        return sections[0]

    return _heading_group("Task Plan Complete View", "green", *sections)
