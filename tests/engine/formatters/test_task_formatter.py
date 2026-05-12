"""Unit tests for TaskFormatter and TaskDataValidator."""

from __future__ import annotations

import pytest
from rich.console import Console

from se3.engine.formatters import (
    TaskDataValidator,
    TaskFormatter,
    TaskValidationError,
    format_task_groups,
)


class TestTaskDataValidator:
    """Tests for TaskDataValidator."""

    def test_valid_task_structure(self):
        """Test validation of a valid task structure."""
        tasks = [
            {
                "id": 1,
                "description": "Test task",
                "complexity": "small",
                "dependencies": [],
                "verification_criteria": ["Test passes"],
            }
        ]

        result = TaskDataValidator.validate(tasks)
        assert len(result) == 1
        assert result[0]["id"] == 1
        assert result[0]["complexity"] == "small"

    def test_missing_required_field(self):
        """Test validation fails when required field is missing."""
        tasks = [
            {
                "id": 1,
                # missing "description"
            }
        ]

        with pytest.raises(TaskValidationError) as exc_info:
            TaskDataValidator.validate(tasks)

        assert "Missing required fields" in str(exc_info.value)

    def test_invalid_complexity(self):
        """Test validation normalizes invalid complexity."""
        tasks = [
            {
                "id": 1,
                "description": "Test task",
                "complexity": "invalid",
            }
        ]

        result = TaskDataValidator.validate(tasks)
        assert result[0]["complexity"] == "medium"

    def test_duplicate_task_ids(self):
        """Test validation fails on duplicate task IDs."""
        tasks = [
            {"id": 1, "description": "Task 1"},
            {"id": 1, "description": "Task 2"},
        ]

        with pytest.raises(TaskValidationError) as exc_info:
            TaskDataValidator.validate(tasks)

        assert "Duplicate task ID" in str(exc_info.value)

    def test_circular_dependencies(self):
        """Test validation fails on circular dependencies."""
        tasks = [
            {"id": 1, "description": "Task 1", "dependencies": [2]},
            {"id": 2, "description": "Task 2", "dependencies": [1]},
        ]

        with pytest.raises(TaskValidationError) as exc_info:
            TaskDataValidator.validate(tasks)

        assert "Circular dependency detected" in str(exc_info.value)

    def test_invalid_dependency_reference(self):
        """Test validation fails on invalid dependency reference."""
        tasks = [
            {"id": 1, "description": "Task 1", "dependencies": [999]},
        ]

        with pytest.raises(TaskValidationError) as exc_info:
            TaskDataValidator.validate(tasks)

        assert "not found" in str(exc_info.value)

    def test_empty_task_list(self):
        """Test validation of empty task list."""
        result = TaskDataValidator.validate([])
        assert result == []

    def test_task_with_defaults(self):
        """Test that optional fields get default values."""
        tasks = [
            {
                "id": 1,
                "description": "Minimal task",
            }
        ]

        result = TaskDataValidator.validate(tasks)
        task = result[0]

        assert task["complexity"] == "medium"
        assert task["dependencies"] == []
        assert task["verification_criteria"] == []
        assert task["files"] == []
        assert task["depends_on"] == []


class TestTaskFormatter:
    """Tests for TaskFormatter."""

    @pytest.fixture
    def console(self):
        """Create a test console."""
        return Console(width=120, force_terminal=True)

    @pytest.fixture
    def formatter(self, console):
        """Create a test formatter."""
        return TaskFormatter(console=console)

    @pytest.fixture
    def sample_task_groups(self):
        """Create sample task groups for testing."""
        return [
            {
                "group_id": "G1",
                "name": "Core Implementation",
                "description": "Implement core functionality",
                "group_order": 1,
                "depends_on": [],
                "tasks": [
                    {
                        "id": 1,
                        "description": "Create base class",
                        "complexity": "small",
                        "dependencies": [],
                        "verification_criteria": ["Tests pass"],
                        "files": ["base.py"],
                    },
                    {
                        "id": 2,
                        "description": "Implement core methods",
                        "complexity": "medium",
                        "dependencies": [1],
                        "verification_criteria": ["Tests pass", "Coverage > 80%"],
                        "files": ["core.py", "utils.py"],
                    },
                ],
            },
            {
                "group_id": "G2",
                "name": "Integration",
                "description": "Integrate with existing system",
                "group_order": 2,
                "depends_on": ["G1"],
                "tasks": [
                    {
                        "id": 3,
                        "description": "Add integration layer with external API",
                        "complexity": "large",
                        "dependencies": [2],
                        "verification_criteria": ["Integration tests pass"],
                        "files": ["integration.py"],
                    },
                ],
            },
        ]

    def test_format_tasks_tree_mode(self, formatter, sample_task_groups, console):
        """Test tree mode formatting."""
        renderable = formatter.format_tasks(sample_task_groups, mode="tree")

        assert renderable is not None

        with console.capture() as capture:
            console.print(renderable)
        output = capture.get()

        # Verify ## heading and tree structure are present
        assert "## Task Plan" in output
        assert "G1" in output
        assert "G2" in output
        assert "Core Implementation" in output
        assert "Integration" in output

    def test_format_tasks_table_mode(self, formatter, sample_task_groups, console):
        """Test table mode formatting."""
        renderable = formatter.format_tasks(sample_task_groups, mode="table")

        assert renderable is not None

        with console.capture() as capture:
            console.print(renderable)
        output = capture.get()

        # Verify ## heading and table columns are present
        assert "## Task Plan" in output
        assert "ID" in output or "Task" in output
        assert "Description" in output or "description" in output.lower()
        assert "Complexity" in output or "complexity" in output.lower()

    def test_format_tasks_empty(self, formatter, console):
        """Test formatting empty task groups."""
        renderable = formatter.format_tasks([], mode="tree")

        assert renderable is not None
        with console.capture() as capture:
            console.print(renderable)
        output = capture.get()

        assert "## Task Plan" in output
        assert "No tasks" in output

    def test_format_task_detail(self, formatter, console):
        """Test formatting single task detail."""
        task = {
            "id": 1,
            "description": "Test task description",
            "complexity": "medium",
            "dependencies": [2, 3],
            "verification_criteria": ["Test passes", "Coverage > 80%"],
            "files": ["test.py", "utils.py"],
        }

        panel = formatter.format_task_detail(task)

        assert panel is not None

        with console.capture() as capture:
            console.print(panel)
        output = capture.get()

        # Verify task details are present
        assert "1" in output
        assert "Test task description" in output
        assert "medium" in output.lower() or "Medium" in output
        assert "test.py" in output or "files" in output.lower()

    def test_format_summary(self, formatter, sample_task_groups, console):
        """Test formatting summary statistics."""
        renderable = formatter.format_summary(sample_task_groups)

        assert renderable is not None

        with console.capture() as capture:
            console.print(renderable)
        output = capture.get()

        # Verify ## heading and summary stats are present
        assert "## Task Summary" in output
        assert "2" in output  # 2 groups
        assert "3" in output  # 3 tasks total
        assert "small" in output.lower() or "medium" in output.lower() or "large" in output.lower()

    def test_format_dependencies(self, formatter, sample_task_groups, console):
        """Test formatting dependency map."""
        renderable = formatter.format_dependencies(sample_task_groups)

        assert renderable is not None

        with console.capture() as capture:
            console.print(renderable)
        output = capture.get()

        # Verify ## heading and dependency info are present
        assert "## Dependencies" in output
        assert "G1" in output or "G2" in output or "ID" in output

    def test_complexity_colors(self, formatter):
        """Test that complexity colors are defined."""
        assert "small" in formatter.COMPLEXITY_COLORS
        assert "medium" in formatter.COMPLEXITY_COLORS
        assert "large" in formatter.COMPLEXITY_COLORS

    def test_estimate_effort(self, formatter):
        """Test effort estimation logic."""
        assert formatter._estimate_effort(0, 2.0) == "N/A"
        assert "hour" in formatter._estimate_effort(1, 1.0).lower()
        assert "days" in formatter._estimate_effort(10, 2.0).lower() or "weeks" in formatter._estimate_effort(10, 2.0).lower()

    def test_calculate_avg_complexity(self, formatter):
        """Test average complexity calculation."""
        counts = {"small": 1, "medium": 1, "large": 1}
        avg = formatter._calculate_avg_complexity(3, counts)
        assert 1.8 < avg < 2.2  # Should be around 2.0

    def test_format_strategy_line_single_group(self, formatter, console):
        """Test strategy line for single group scenario (loc_threshold=0)."""
        text = formatter._format_strategy_line("single", 141, 0, 1)
        with console.capture() as capture:
            console.print(text)
        output = capture.get()
        assert "Single group" in output
        assert "single LLM call" in output
        assert "141 LOC" in output
        assert "threshold" not in output

    def test_format_strategy_line_single_merged(self, formatter, console):
        """Test strategy line for multi-group merge scenario (loc_threshold>0)."""
        text = formatter._format_strategy_line("single", 200, 300, 3)
        with console.capture() as capture:
            console.print(text)
        output = capture.get()
        assert "Single LLM call" in output
        assert "200 LOC" in output
        assert "300 threshold" in output

    def test_format_strategy_line_dag_parallel(self, formatter, console):
        """Test strategy line for dag_parallel scenario."""
        text = formatter._format_strategy_line("dag_parallel", 500, 300, 3)
        with console.capture() as capture:
            console.print(text)
        output = capture.get()
        assert "DAG parallel" in output
        assert "500 LOC" in output
        assert "300 threshold" in output
        assert "3 groups" in output

    def test_format_strategy_line_sequential_default(self, formatter, console):
        """Sequential without a reason shows only the group count."""
        text = formatter._format_strategy_line("sequential", 500, 300, 3)
        with console.capture() as capture:
            console.print(text)
        output = capture.get()
        assert "Sequential" in output
        assert "3 groups" in output
        assert "reason" not in output

    def test_format_strategy_line_sequential_reason_use_worktree_false(
        self, formatter, console,
    ):
        """Sequential-with-reason surfaces the short-circuit explanation."""
        text = formatter._format_strategy_line(
            "sequential", 600, 300, 3,
            sequential_reason="use_worktree=False",
        )
        with console.capture() as capture:
            console.print(text)
        output = capture.get()
        assert "Sequential" in output
        assert "3 groups" in output
        assert "reason: use_worktree=False" in output

    def test_format_strategy_line_sequential_reason_linear_chain(
        self, formatter, console,
    ):
        """Linear-chain short-circuit is surfaced as the sequential reason."""
        text = formatter._format_strategy_line(
            "sequential", 900, 300, 3,
            sequential_reason="linear chain",
        )
        with console.capture() as capture:
            console.print(text)
        output = capture.get()
        assert "Sequential" in output
        assert "reason: linear chain" in output

    def test_format_strategy_line_reason_ignored_for_non_sequential(
        self, formatter, console,
    ):
        """``sequential_reason`` is not rendered for non-sequential strategies."""
        text = formatter._format_strategy_line(
            "dag_parallel", 500, 300, 3,
            sequential_reason="use_worktree=False",
        )
        with console.capture() as capture:
            console.print(text)
        output = capture.get()
        assert "DAG parallel" in output
        assert "reason" not in output

    def test_format_implement_plan_threads_reason(
        self, formatter, console,
    ):
        """``format_implement_plan`` forwards ``sequential_reason`` to the line."""
        task_groups = [
            {
                "group_id": "G1",
                "name": "One",
                "description": "first",
                "group_order": 1,
                "depends_on": [],
                "tasks": [],
            },
            {
                "group_id": "G2",
                "name": "Two",
                "description": "second",
                "group_order": 2,
                "depends_on": ["G1"],
                "tasks": [],
            },
        ]
        panel = formatter.format_implement_plan(
            task_groups=task_groups,
            execution_strategy="sequential",
            total_loc=400,
            loc_threshold=300,
            sequential_reason="linear chain",
        )
        with console.capture() as capture:
            console.print(panel)
        output = capture.get()
        assert "Sequential" in output
        assert "reason: linear chain" in output

    def test_format_implement_plan_no_reason_default(
        self, formatter, console,
    ):
        """Omitting ``sequential_reason`` keeps the panel backward compatible."""
        task_groups = [
            {
                "group_id": "G1",
                "name": "One",
                "description": "first",
                "group_order": 1,
                "depends_on": [],
                "tasks": [],
            },
        ]
        panel = formatter.format_implement_plan(
            task_groups=task_groups,
            execution_strategy="sequential",
            total_loc=50,
            loc_threshold=300,
        )
        with console.capture() as capture:
            console.print(panel)
        output = capture.get()
        assert "Sequential" in output
        assert "reason" not in output


class TestFormatTaskGroupsConvenience:
    """Tests for the format_task_groups convenience function."""

    @pytest.fixture
    def sample_task_groups(self):
        """Create sample task groups for testing."""
        return [
            {
                "group_id": "G1",
                "name": "Test Group",
                "description": "A test group",
                "group_order": 1,
                "depends_on": [],
                "tasks": [
                    {
                        "id": 1,
                        "description": "Test task",
                        "complexity": "small",
                        "dependencies": [],
                        "verification_criteria": [],
                    },
                ],
            },
        ]

    def test_format_task_groups_default(self, sample_task_groups):
        """Test format_task_groups with default parameters."""
        panel = format_task_groups(sample_task_groups)
        assert panel is not None

    def test_format_task_groups_with_summary(self, sample_task_groups):
        """Test format_task_groups with summary enabled."""
        panel = format_task_groups(sample_task_groups, show_summary=True)
        assert panel is not None

    def test_format_task_groups_with_dependencies(self, sample_task_groups):
        """Test format_task_groups with dependencies enabled."""
        panel = format_task_groups(sample_task_groups, show_dependencies=True)
        assert panel is not None
