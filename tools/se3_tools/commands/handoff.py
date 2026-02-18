"""SE3 Handoff command — enforce "commit before handoff" rule.

Usage:
    se3 handoff [OPTIONS] [MESSAGE]

Environment detection:
    - If SE3_AGENT_ROLE is set (collab mode): creates human-call for orchestrator
    - Otherwise (direct usage): commits changes, generates session summary in progress.md

This command enforces the SE3 rule: "All modifications must be committed before
transferring control to humans."
"""

import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import typer

app = typer.Typer()


def find_project_root() -> Path:
    """Find project root by looking for .claude/ or .git/."""
    current = Path.cwd()
    while current != current.parent:
        if (current / ".claude").is_dir() or (current / ".git").is_dir():
            return current
        current = current.parent
    return Path.cwd()


def run_command(cmd: List[str], cwd: Path, capture: bool = True) -> subprocess.CompletedProcess:
    """Run a shell command and return the result."""
    return subprocess.run(cmd, cwd=cwd, capture_output=capture, text=True)


def has_uncommitted_changes(project_root: Path) -> Tuple[bool, str]:
    """Check if there are uncommitted changes.

    Returns: (has_changes, details)
    """
    # Check staged changes
    result = run_command(["git", "diff", "--cached", "--quiet"], cwd=project_root)
    has_staged = result.returncode != 0

    # Check unstaged changes
    result = run_command(["git", "diff", "--quiet"], cwd=project_root)
    has_unstaged = result.returncode != 0

    # Check untracked files
    result = run_command(["git", "ls-files", "--others", "--exclude-standard"], cwd=project_root)
    has_untracked = bool(result.stdout.strip())

    details = []
    if has_staged:
        details.append("staged changes")
    if has_unstaged:
        details.append("unstaged changes")
    if has_untracked:
        details.append("untracked files")

    return (has_staged or has_unstaged or has_untracked), ", ".join(details) if details else ""


def get_changed_files_summary(project_root: Path) -> str:
    """Get a summary of changed files."""
    result = run_command(["git", "status", "--short"], cwd=project_root)
    return result.stdout.strip() if result.returncode == 0 else "(unable to get status)"


def is_in_collab_mode(project_root: Path) -> bool:
    """Detect if we're running under se3 collab.

    Only returns True if SE3_AGENT_ROLE is set in the environment, which
    indicates this process was spawned by the collab orchestrator as a
    work agent. The existence of .collab/config.json only means a collab
    session exists, not that the current process is part of it.
    """
    return bool(os.environ.get("SE3_AGENT_ROLE"))


def get_collab_info(project_root: Path) -> dict:
    """Get information about the current collab session."""
    collab_config = project_root / ".collab" / "config.json"
    if collab_config.exists():
        try:
            return json.loads(collab_config.read_text())
        except:
            pass
    return {}


def create_human_call(project_root: Path, reason: str, context: str, agent_role: str = "worker"):
    """Create a human-call file for the orchestrator."""
    human_calls_dir = project_root / "human-calls"
    human_calls_dir.mkdir(exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"{timestamp}-{agent_role}-handoff.md"
    filepath = human_calls_dir / filename

    collab_info = get_collab_info(project_root)
    session_id = collab_info.get("session_id", "unknown")

    content = f"""---
id: {timestamp}-{agent_role}-handoff
type: action
priority: medium
status: pending
created: {datetime.now().isoformat()}
source: collab-{agent_role}
language: en-US
---

## Request: Agent Handoff

**Type**: action
**Urgency**: medium
**Source**: collab-{agent_role}
**Session**: {session_id}

### Context
{context}

### Handoff Reason
{reason}

### Current Task Status


### Response
<!-- Human: write your response below -->
"""

    filepath.write_text(content, encoding="utf-8")
    return filepath


def run_se3_commit(project_root: Path, message: Optional[str] = None) -> Tuple[bool, str]:
    """Run se3 commit with the given message.

    Returns: (success, output)
    """
    cmd = ["se3", "commit"]
    if message:
        cmd.extend(["-m", message])

    result = run_command(cmd, cwd=project_root, capture=True)
    return result.returncode == 0, (result.stdout + "\n" + result.stderr).strip()


def generate_handoff_report(project_root: Path, commit_output: str) -> str:
    """Generate a handoff report with session summary from progress.md."""
    # Get last commit info
    result = run_command(
        ["git", "log", "-1", "--format=%h %s (%ar)"],
        cwd=project_root
    )
    last_commit = result.stdout.strip() if result.returncode == 0 else "(unknown)"

    # Get branch info
    result = run_command(["git", "branch", "--show-current"], cwd=project_root)
    branch = result.stdout.strip() if result.returncode == 0 else "(unknown)"

    # Finalize session in progress.md
    session_report = ""
    try:
        from ..progress import finalize_session
        session_report = finalize_session(project_root)
    except Exception as e:
        session_report = f"(could not generate session summary: {e})"

    # Check for uncommitted changes (should be none after commit)
    has_changes, change_details = has_uncommitted_changes(project_root)

    report = f"""
============================================================
SE3 Handoff Report
============================================================
Branch:      {branch}
Last Commit: {last_commit}

Changes Committed: {'Yes' if not has_changes else 'WARNING: ' + change_details}

Session Summary:
{session_report}
============================================================
"""
    return report


@app.command()
def handoff(
    message: Optional[str] = typer.Argument(None, help="Handoff message describing what was done"),
    project_root: Optional[str] = typer.Option(None, "--project-root", "-p", help="Project root directory"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Preview what would happen without executing"),
    skip_commit: bool = typer.Option(False, "--skip-commit", help="Skip automatic commit (use with caution)"),
):
    """Handoff control to human — enforcing SE3 commit-before-handoff rule.

    In direct mode (se3 handoff):
        1. Checks for uncommitted changes
        2. Runs se3 commit (if changes exist)
        3. Finalizes session in progress.md (replaces Current Session with formal record)
        4. Generates handoff report

    In collab mode (SE3_AGENT_ROLE set):
        Creates human-call for orchestrator to handle handoff
    """
    root = Path(project_root) if project_root else find_project_root()

    # Detect mode
    in_collab = is_in_collab_mode(root)
    agent_role = os.environ.get("SE3_AGENT_ROLE", "agent")

    typer.echo(f"\nse3 handoff — {root}")
    typer.echo("=" * 50)

    if in_collab:
        # Collab mode: create human-call
        typer.echo(f"\n[Collab Mode — {agent_role}]")
        typer.echo("Creating human-call for orchestrator...")

        reason = message or f"{agent_role} completed work, requesting human review"
        context = f"""Agent: {agent_role}
Time: {datetime.now().isoformat()}
Project: {root.name}

The agent has completed its assigned task and is handing off to human for:
- Review of completed work
- Decision on next steps
- Any necessary human-only actions
"""

        if dry_run:
            typer.echo(f"\n[DRY RUN] Would create human-call:")
            typer.echo(f"  Reason: {reason}")
            typer.echo(f"  Context: {context[:200]}...")
            raise typer.Exit(0)

        filepath = create_human_call(root, reason, context, agent_role)
        typer.echo(f"  Created: {filepath}")
        typer.echo(f"\n  Orchestrator will notify human.")
        typer.echo(f"{'=' * 50}")
        raise typer.Exit(0)

    else:
        # Direct mode: commit then generate session summary
        typer.echo("\n[Direct Mode]")

        # Check for changes
        has_changes, change_details = has_uncommitted_changes(root)

        if not has_changes:
            typer.echo("  No uncommitted changes found.")
            # Still finalize session even without new commit
            try:
                from ..progress import finalize_session
                report = finalize_session(root)
                if report and not report.startswith("(no"):
                    typer.echo("\n  Session summary written to progress.md")
                    typer.echo(report)
            except Exception as e:
                typer.echo(f"  Warning: could not finalize session: {e}")

            typer.echo(f"\n{'=' * 50}")
            typer.echo("Handoff complete.")
            raise typer.Exit(0)

        typer.echo(f"  Uncommitted changes detected: {change_details}")
        typer.echo("")

        if skip_commit:
            typer.echo("  WARNING: --skip-commit flag used, bypassing commit.")
            typer.echo("  This violates SE3 protocol but proceeding as requested.")
        elif dry_run:
            typer.echo("  [DRY RUN] Would execute: se3 commit")
            if message:
                typer.echo(f'  Message: "{message}"')
            typer.echo("")
            typer.echo("  Changed files:")
            status = get_changed_files_summary(root)
            for line in status.split("\n")[:10]:
                typer.echo(f"    {line}")
            typer.echo(f"\n{'=' * 50}")
            raise typer.Exit(0)
        else:
            # Run se3 commit
            typer.echo("  Running se3 commit...")
            success, output = run_se3_commit(root, message)

            if success:
                typer.echo("  Commit successful.")
            else:
                typer.echo("  Commit failed:")
                typer.echo(f"  {output}")
                typer.echo("\n  Fix issues and retry, or use --skip-commit to bypass (not recommended).")
                raise typer.Exit(1)

            # Generate report with session summary
            report = generate_handoff_report(root, output)
            typer.echo(report)

            typer.echo(f"{'=' * 50}")
            typer.echo("Handoff complete. Control transferred to human.")
            raise typer.Exit(0)
