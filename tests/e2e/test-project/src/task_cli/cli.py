"""Task CLI - Main command line interface."""

import json
import os
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table

console = Console()

DEFAULT_TASKS_FILE = "tasks.json"


def get_tasks_file() -> Path:
    """Get the path to the tasks file."""
    return Path(os.environ.get("TASKS_FILE", DEFAULT_TASKS_FILE))


def load_tasks() -> list:
    """Load tasks from the tasks file."""
    tasks_file = get_tasks_file()
    if tasks_file.exists():
        with open(tasks_file, "r") as f:
            return json.load(f)
    return []


def save_tasks(tasks: list) -> None:
    """Save tasks to the tasks file."""
    tasks_file = get_tasks_file()
    with open(tasks_file, "w") as f:
        json.dump(tasks, f, indent=2)


@click.group()
@click.version_option(version=__import__("task_cli").__version__)
def cli():
    """Task CLI - A simple task manager."""
    pass


@cli.command()
@click.argument("title")
@click.option("--priority", "-p", type=click.Choice(["low", "medium", "high"]), default="medium")
@click.option("--due", "-d", help="Due date (YYYY-MM-DD)")
def add(title: str, priority: str, due: Optional[str]):
    """Add a new task."""
    tasks = load_tasks()
    task = {
        "id": len(tasks) + 1,
        "title": title,
        "priority": priority,
        "due": due,
        "done": False,
    }
    tasks.append(task)
    save_tasks(tasks)
    console.print(f"[green]✓[/green] Task added: {title}")


@cli.command()
def list():
    """List all tasks."""
    tasks = load_tasks()
    if not tasks:
        console.print("[yellow]No tasks found.[/yellow]")
        return
    
    table = Table(title="Tasks")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Title", style="white")
    table.add_column("Priority", style="magenta")
    table.add_column("Due", style="green")
    table.add_column("Status", style="blue")
    
    for task in tasks:
        status = "✓ Done" if task["done"] else "○ Pending"
        table.add_row(
            str(task["id"]),
            task["title"],
            task["priority"],
            task.get("due", "-"),
            status,
        )
    
    console.print(table)


@cli.command()
@click.argument("task_id", type=int)
def done(task_id: int):
    """Mark a task as done."""
    tasks = load_tasks()
    for task in tasks:
        if task["id"] == task_id:
            task["done"] = True
            save_tasks(tasks)
            console.print(f"[green]✓[/green] Task {task_id} marked as done")
            return
    console.print(f"[red]✗[/red] Task {task_id} not found")


@cli.command()
@click.argument("task_id", type=int)
def delete(task_id: int):
    """Delete a task."""
    tasks = load_tasks()
    for i, task in enumerate(tasks):
        if task["id"] == task_id:
            del tasks[i]
            # Reassign IDs
            for j, t in enumerate(tasks):
                t["id"] = j + 1
            save_tasks(tasks)
            console.print(f"[green]✓[/green] Task {task_id} deleted")
            return
    console.print(f"[red]✗[/red] Task {task_id} not found")


def main():
    """Entry point for the CLI."""
    cli()


if __name__ == "__main__":
    main()
