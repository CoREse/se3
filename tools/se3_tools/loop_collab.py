"""Loop Collab integration mode for SE3.

Runs multiple iterations of collaboration, with each iteration using
the ForegroundOrchestrator for parallel task execution.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.panel import Panel
from rich.text import Text

from .collab_orchestrator import ForegroundOrchestrator
from .collab_render import CollabRenderer


@dataclass
class CollabSummary:
    """Summary of a single collab iteration."""
    iteration: int
    completed_tasks: list[str] = field(default_factory=list)
    failed_tasks: list[str] = field(default_factory=list)
    key_changes: list[str] = field(default_factory=list)
    insights: str = ""
    next_steps: list[str] = field(default_factory=list)


class LoopCollabRunner:
    """Runner for Loop + Collab integration mode.

    Runs multiple iterations of collaboration, with state passing between
    iterations and interactive menus for user control.
    """

    def __init__(
        self,
        base_prompt: str,
        iterations: int,
        project_root: Path,
        max_parallel: int = 3,
    ):
        self.base_prompt = base_prompt
        self.iterations = iterations
        self.project_root = project_root
        self.max_parallel = max_parallel
        self.previous_summaries: list[CollabSummary] = []
        self.renderer = CollabRenderer()
        self.console = self.renderer.console

    async def run(self) -> bool:
        """Run the complete Loop Collab flow.

        Returns:
            True if all iterations completed successfully
        """
        self.console.print(f"\n[bold blue]Starting Loop Collab Mode[/bold blue]")
        self.console.print(f"Iterations: {self.iterations}")
        self.console.print(f"Project: {self.project_root}\n")

        for i in range(1, self.iterations + 1):
            # Show iteration header
            self._print_iteration_header(i)

            # Build prompt with context from previous iterations
            prompt = self._build_prompt(i)

            # Run collab iteration
            summary = await self._run_collab_iteration(i, prompt)

            if not summary:
                self.console.print("[red]Iteration failed[/red]")
                if not await self._confirm("Continue to next iteration?"):
                    break
                continue

            self.previous_summaries.append(summary)

            # Display summary
            self._display_summary(summary)

            # Show iteration menu (if not last iteration)
            if i < self.iterations:
                action = await self._iteration_menu()
                if action == "exit":
                    self.console.print("[yellow]Exiting loop early[/yellow]")
                    break
                elif action == "skip":
                    self.console.print("[yellow]Skipping to next iteration[/yellow]")
                    continue
                elif action == "modify":
                    self.base_prompt = await self._modify_prompt()

        self._print_completion()
        return True

    def _print_iteration_header(self, iteration: int):
        """Print iteration header."""
        self.console.print("\n" + "=" * 70)
        self.console.print(f"[bold]LOOP ITERATION {iteration}/{self.iterations}[/bold]")
        self.console.print("=" * 70)

    def _build_prompt(self, iteration: int) -> str:
        """Build the prompt for this iteration."""
        prompt_parts = [self.base_prompt]

        if iteration > 1 and self.previous_summaries:
            prev = self.previous_summaries[-1]

            context = f"""

## Previous Iteration Summary (Iteration {prev.iteration})

### Insights
{prev.insights}

### Completed Tasks
"""
            for task in prev.completed_tasks:
                context += f"- {task}\n"

            if prev.failed_tasks:
                context += "\n### Failed Tasks\n"
                for task in prev.failed_tasks:
                    context += f"- {task}\n"

            if prev.key_changes:
                context += "\n### Key Changes\n"
                for change in prev.key_changes:
                    context += f"- {change}\n"

            if prev.next_steps:
                context += "\n### Suggested Next Steps\n"
                for step in prev.next_steps:
                    context += f"- {step}\n"

            prompt_parts.append(context)

        prompt_parts.append(f"\n(Loop Iteration {iteration}/{self.iterations})")

        return "\n".join(prompt_parts)

    async def _run_collab_iteration(self, iteration: int, prompt: str) -> CollabSummary | None:
        """Run a single collab iteration."""
        try:
            # Create orchestrator with our renderer
            orchestrator = ForegroundOrchestrator(
                self.project_root,
                self.renderer,
                max_parallel=self.max_parallel,
            )
        except Exception as e:
            self.console.print(f"[red]Failed to create orchestrator: {e}[/red]")
            return None

        # Start live display
        try:
            with self.renderer.start_live():
                success = await orchestrator.run(prompt)
        except Exception as e:
            self.console.print(f"[red]Error during collab iteration: {e}[/red]")
            return None

        if not success:
            return None

        # Generate summary from results
        try:
            return await self._generate_summary(iteration, orchestrator)
        except Exception as e:
            self.console.print(f"[yellow]Warning: Failed to generate summary: {e}[/yellow]")
            # Return a basic summary even if generation fails
            return CollabSummary(
                iteration=iteration,
                completed_tasks=[],
                failed_tasks=[],
                key_changes=[],
                insights="Iteration completed but summary generation failed.",
                next_steps=["Review changes manually"],
            )

    async def _generate_summary(
        self,
        iteration: int,
        orchestrator: ForegroundOrchestrator,
    ) -> CollabSummary:
        """Generate a summary of the iteration."""
        # Collect task info
        completed = []
        failed = []

        for task in orchestrator.tasks.values():
            if task.status == "done":
                completed.append(f"{task.id}: {task.title}")
            elif task.status == "failed":
                failed.append(f"{task.id}: {task.title}")

        # Try to use AI to generate insights
        insights = await self._generate_insights(orchestrator)

        # Extract key changes from git
        key_changes = self._extract_git_changes()

        # Generate next steps based on context
        next_steps = self._generate_next_steps(completed, failed)

        return CollabSummary(
            iteration=iteration,
            completed_tasks=completed,
            failed_tasks=failed,
            key_changes=key_changes,
            insights=insights,
            next_steps=next_steps,
        )

    async def _generate_insights(self, orchestrator: ForegroundOrchestrator) -> str:
        """Generate insights using AI."""
        # Simple heuristic-based insights for now
        # Could be enhanced to call Claude for deeper analysis

        try:
            summary = orchestrator.get_summary()
            total = summary.get("total_tasks", 0)
            completed = summary.get("completed", 0)
            failed = summary.get("failed", 0)

            if total == 0:
                return "No tasks were executed in this iteration."

            if failed == 0:
                return f"All {completed} tasks completed successfully. Ready for next iteration."
            elif completed > failed:
                return f"Majority of tasks ({completed}/{total}) completed successfully. {failed} tasks need attention."
            else:
                return f"Several tasks ({failed}) encountered issues. Review recommended before continuing."
        except Exception as e:
            return f"Iteration completed. (Could not generate detailed insights: {e})"

    def _extract_git_changes(self) -> list[str]:
        """Extract key changes from git."""
        changes = []

        try:
            # Get list of changed files
            result = subprocess.run(
                ["git", "status", "--short"],
                cwd=self.project_root,
                capture_output=True,
                text=True,
            )

            if result.returncode == 0 and result.stdout.strip():
                for line in result.stdout.strip().split("\n"):
                    if line.strip():
                        changes.append(line.strip())

            # Also get recent commits
            result = subprocess.run(
                ["git", "log", "--oneline", "-5"],
                cwd=self.project_root,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                changes.append("--- Recent commits ---")
                for line in result.stdout.strip().split("\n")[:5]:
                    if line.strip():
                        changes.append(line.strip())

            # Limit to first 15 changes
            return changes[:15]
        except Exception as e:
            self.console.print(f"[dim]Note: Could not extract git changes: {e}[/dim]")
            return []

    def _generate_next_steps(self, completed: list[str], failed: list[str]) -> list[str]:
        """Generate suggested next steps."""
        steps = []

        if failed:
            steps.append("Address failed tasks before continuing")

        steps.append("Review completed changes")
        steps.append("Run tests to verify integrity")

        if len(completed) > 3:
            steps.append("Consider consolidating changes")

        return steps

    def _display_summary(self, summary: CollabSummary):
        """Display iteration summary."""
        # Build content as string with rich markup
        content_lines = []

        content_lines.append(f"[bold]Insights:[/bold]\n{summary.insights}\n")
        content_lines.append(f"[green]Completed:[/green] {len(summary.completed_tasks)} tasks")
        if summary.failed_tasks:
            content_lines.append(f"[red]Failed:[/red] {len(summary.failed_tasks)} tasks")

        if summary.key_changes:
            content_lines.append(f"\n[bold]Key Changes:[/bold]")
            for change in summary.key_changes[:5]:
                content_lines.append(f"  {change}")

        content = "\n".join(content_lines)

        panel = Panel(
            content,
            title=f"[bold]Iteration {summary.iteration} Summary[/bold]",
            border_style="blue",
        )

        self.console.print(panel)

    async def _iteration_menu(self) -> str:
        """Show menu between iterations."""
        self.console.print("\n[bold]Options:[/bold]")
        self.console.print("  [c] Continue to next iteration")
        self.console.print("  [m] Modify prompt for next iteration")
        self.console.print("  [s] Skip next iteration")
        self.console.print("  [e] Exit loop")

        # Run input() in a thread pool to avoid blocking the event loop
        loop = asyncio.get_event_loop()
        choice = await loop.run_in_executor(None, lambda: input("\nChoice [c/m/s/e]: ").strip().lower())

        return {
            "c": "continue",
            "m": "modify",
            "s": "skip",
            "e": "exit",
        }.get(choice, "continue")

    async def _confirm(self, message: str) -> bool:
        """Ask for confirmation."""
        # Run input() in a thread pool to avoid blocking the event loop
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(None, lambda: input(f"{message} [y/N]: ").strip().lower())
        return response == "y"

    async def _modify_prompt(self) -> str:
        """Modify the base prompt (async version that doesn't block the event loop)."""
        self.console.print(f"\n[bold]Current prompt:[/bold]\n{self.base_prompt[:200]}...")
        self.console.print("\nEnter additional instructions (or 'edit' to rewrite):")

        # Run input() in a thread pool to avoid blocking the event loop
        loop = asyncio.get_event_loop()
        additional = await loop.run_in_executor(None, lambda: input("> ").strip())

        if additional.lower() == "edit":
            return await loop.run_in_executor(None, self._open_editor, self.base_prompt)

        return f"{self.base_prompt}\n\n## Additional Instructions\n{additional}"

    def _open_editor(self, initial_text: str) -> str:
        """Open system editor."""
        import tempfile

        editor = os.environ.get("EDITOR", "vim")

        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            f.write(initial_text)
            temp_path = f.name

        try:
            subprocess.run([editor, temp_path], check=False)
            return Path(temp_path).read_text()
        finally:
            Path(temp_path).unlink(missing_ok=True)

    def _print_completion(self):
        """Print final completion message."""
        self.console.print("\n" + "=" * 70)
        self.console.print("[bold green]LOOP COMPLETE[/bold green]")
        self.console.print("=" * 70)

        total_completed = sum(len(s.completed_tasks) for s in self.previous_summaries)
        total_failed = sum(len(s.failed_tasks) for s in self.previous_summaries)

        self.console.print(f"\nTotal iterations: {len(self.previous_summaries)}")
        self.console.print(f"Total tasks completed: {total_completed}")
        if total_failed:
            self.console.print(f"Total tasks failed: {total_failed}")

        self.console.print("\n[dim]Use 'se3 commit' to commit your changes[/dim]")
