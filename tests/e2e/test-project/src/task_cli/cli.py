"""Task CLI - Main command line interface."""

import json
import os
import re
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table
from rich.text import Text

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


def _search_tasks(tasks: list, keyword: str) -> list:
    """
    Search tasks by keyword (case-insensitive).
    
    Args:
        tasks: List of task dictionaries
        keyword: Search keyword
        
    Returns:
        List of matching tasks
    """
    keyword_lower = keyword.lower()
    matching_tasks = []
    
    for task in tasks:
        if keyword_lower in task["title"].lower():
            matching_tasks.append(task)
    
    return matching_tasks


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
            # Reassign IDs to maintain continuous sequence starting from 1
            for j, t in enumerate(tasks):
                t["id"] = j + 1
            save_tasks(tasks)
            console.print(f"[green]✓[/green] Task {task_id} deleted")
            return
    console.print(f"[red]✗[/red] Task {task_id} not found")


@cli.command()
@click.argument("keyword")
def search(keyword: str):
    """Search tasks by keyword."""
    tasks = load_tasks()
    matching_tasks = _search_tasks(tasks, keyword)
    
    if not matching_tasks:
        console.print(f"[yellow]No tasks found matching '{keyword}'.[/yellow]")
        return
    
    table = Table(title=f"Search Results for '{keyword}'")
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Title", style="white")
    table.add_column("Priority", style="magenta")
    table.add_column("Due", style="green")
    table.add_column("Status", style="blue")
    
    for task in matching_tasks:
        status = "✓ Done" if task["done"] else "○ Pending"
        
        # Create a Text object with the title and highlight the keyword
        title_text = Text(task["title"])
        escaped_keyword = re.escape(keyword)
        title_text.highlight_regex(f"(?i:{escaped_keyword})", style="bold yellow")
        
        table.add_row(
            str(task["id"]),
            title_text,
            task["priority"],
            task.get("due", "-"),
            status,
        )
    
    console.print(table)
    console.print(f"[green]Found {len(matching_tasks)} task(s)[/green]")


def main():
    """Entry point for the CLI."""
    cli()


if __name__ == "__main__":
    main()
