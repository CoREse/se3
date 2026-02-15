#!/usr/bin/env python3
"""Generate manager prompt for se3 collab."""

import json
import sys
from datetime import datetime
from pathlib import Path


def load_rules(project_root: Path) -> str:
    """Load manager rules."""
    rules_file = project_root / "scripts" / "rules-manager.md"
    if rules_file.exists():
        return rules_file.read_text()
    return """You are a manager agent in a multi-agent collaboration system.

Your role is to:
1. Plan tasks by breaking down objectives into executable units
2. Review completed work and decide: merge, reject, or request changes
3. Handle escalations and failures by replanning or escalating to human

Respond with valid JSON only."""


def summarize_tasks(collab_dir: Path) -> str:
    """Summarize current tasks."""
    tasks_dir = collab_dir / "tasks"
    if not tasks_dir.exists():
        return "(no tasks yet)"

    lines = []
    for tf in sorted(tasks_dir.glob("task-*.json")):
        task = json.loads(tf.read_text())
        lines.append(f"- {task['id']}: [{task['status']}] {task.get('title', '')}")

    return "\n".join(lines) if lines else "(no tasks yet)"


def generate_prompt(project_root: Path, event_type: str, context: str) -> str:
    """Generate manager prompt."""
    rules = load_rules(project_root)
    collab_dir = project_root / ".collab"

    # Load config
    config_file = collab_dir / "config.json"
    base_branch = "master"
    if config_file.exists():
        config = json.loads(config_file.read_text())
        base_branch = config.get("base_branch", "master")

    tasks_summary = summarize_tasks(collab_dir)

    return f"""{rules}

---

## Current State
Project root: {project_root}
Base branch: {base_branch}

## All Tasks
{tasks_summary}

## Event
Type: {event_type}
Context:
{context}

## Instructions
Analyze the event and decide the next action. Respond ONLY with valid JSON matching this schema:
{{
  "action": "plan|merge|reject|retry|split|escalate|complete",
  "tasks": [...],
  "target_task": "task-id",
  "merge_branch": "branch-name",
  "retry_prompt": "adjusted prompt for retry",
  "reason": "explanation",
  "summary": "human-readable summary of decision"
}}

Rules:
- For 'plan': include full task definitions in 'tasks' array
- For 'merge': set target_task and merge_branch
- For 'reject': set target_task and reason (becomes feedback for worker retry)
- For 'retry': set target_task and retry_prompt
- For 'split': set target_task and new sub-tasks in 'tasks'
- For 'escalate': set reason (will be sent to human)
- For 'complete': when all tasks are merged and done
- If unsure, use 'escalate' rather than guessing
"""


if __name__ == "__main__":
    if len(sys.argv) < 4:
        print("Usage: collab-manager-prompt.py <project_root> <event_type> <context>", file=sys.stderr)
        sys.exit(1)

    project_root = Path(sys.argv[1])
    event_type = sys.argv[2]
    context = sys.argv[3]

    print(generate_prompt(project_root, event_type, context))
