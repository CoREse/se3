#!/usr/bin/env python3
"""Generate worker prompt for se3 collab."""

import json
import sys
from pathlib import Path


def load_rules(project_root: Path) -> str:
    """Load worker rules."""
    rules_file = project_root / "scripts" / "rules-worker.md"
    if rules_file.exists():
        return rules_file.read_text()
    return """You are a worker agent in a multi-agent collaboration system.

Your role is to:
1. Implement the assigned task in the provided worktree
2. Follow the task prompt exactly
3. Run tests to verify your work
4. Commit your changes with a clear message
5. Exit with code 0 on success, non-zero on failure

Work in isolation in the provided worktree. Do not modify files outside your task scope."""


def generate_prompt(project_root: Path, task_id: str) -> str:
    """Generate worker prompt."""
    rules = load_rules(project_root)
    collab_dir = project_root / ".collab"
    task_file = collab_dir / "tasks" / f"{task_id}.json"

    if not task_file.exists():
        raise FileNotFoundError(f"Task file not found: {task_file}")

    task = json.loads(task_file.read_text())
    task_prompt = task.get("prompt", "")
    worktree = task.get("worktree", f".worktrees/{task_id}")

    return f"""{rules}

---

## Your Task (ID: {task_id})

{task_prompt}

## Worktree
You are working in: {project_root}/{worktree}
This is a git worktree with its own branch. Make your changes here.

## Important
- Work ONLY in the worktree directory shown above
- Run tests to verify your implementation
- Commit your changes with a clear, descriptive message
- Do NOT modify files outside your task scope
- Exit with code 0 on success, non-zero on failure
"""


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: collab-worker-prompt.py <project_root> <task_id>", file=sys.stderr)
        sys.exit(1)

    project_root = Path(sys.argv[1])
    task_id = sys.argv[2]

    try:
        print(generate_prompt(project_root, task_id))
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
