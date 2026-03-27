#!/usr/bin/env python3
"""One-time migration: convert se3/specs/_backlog/*.md to Issue YAML format.

Parses each markdown file's title, phase info, and status to infer the issue
type, then creates an issue via IssueManager and removes the original file.

Type inference:
  - Phase 1 remaining -> 'task'
  - Phase 2          -> 'feature'
  - Phase 3          -> 'feature'
  - Phase 4/4+       -> 'idea'
  - Future           -> 'idea'
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# Ensure the project src is importable
project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root / "src"))

from se3.engine.issue_manager import IssueManager


def _extract_title(content: str) -> str:
    """Extract the first # heading as the title."""
    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("# "):
            return line[2:].strip()
    return "Untitled"


def _extract_phase(content: str) -> str:
    """Extract the phase/status info from the markdown."""
    for line in content.split("\n"):
        line_lower = line.lower()
        if "status" in line_lower and "backlog" in line_lower:
            return line.strip()
    return ""


def _infer_type(phase_line: str) -> str:
    """Infer issue type from the phase/status line."""
    lower = phase_line.lower()
    if "phase 1" in lower:
        return "task"
    if "phase 2" in lower:
        return "feature"
    if "phase 3" in lower:
        return "feature"
    if "phase 4" in lower:
        return "idea"
    if "future" in lower:
        return "idea"
    return "feature"


def _extract_description(content: str) -> str:
    """Extract a description from the Idea/Goal/Motivation sections."""
    lines = content.split("\n")
    desc_parts = []
    in_section = False

    for line in lines:
        stripped = line.strip()
        # Start capturing at Idea, Goal, or Motivation headings
        if re.match(r"^##\s+(Idea|Goal|Motivation)", stripped):
            in_section = True
            continue
        # Stop at the next ## heading
        elif stripped.startswith("## ") and in_section:
            break
        elif in_section and stripped:
            desc_parts.append(stripped)

    return "\n".join(desc_parts) if desc_parts else "Migrated from backlog."


def migrate():
    """Run the migration."""
    backlog_dir = project_root / "se3" / "specs" / "_backlog"
    if not backlog_dir.exists():
        print(f"Backlog directory not found: {backlog_dir}")
        return

    md_files = sorted(backlog_dir.glob("*.md"))
    if not md_files:
        print("No markdown files found in backlog directory.")
        return

    mgr = IssueManager(project_root)
    results = []

    for md_file in md_files:
        content = md_file.read_text(encoding="utf-8")
        title = _extract_title(content)
        phase_line = _extract_phase(content)
        issue_type = _infer_type(phase_line)
        description = _extract_description(content)

        try:
            issue = mgr.create(
                title=title,
                description=description,
                priority="medium",
                tags=["migrated-from-backlog", f"source:{md_file.stem}"],
                type=issue_type,
            )
            md_file.unlink()
            results.append((md_file.name, issue.id, issue_type, "migrated"))
            print(f"  [OK] {md_file.name} -> issue {issue.id} (type={issue_type})")
        except Exception as e:
            results.append((md_file.name, None, issue_type, f"FAILED: {e}"))
            print(f"  [FAIL] {md_file.name}: {e}")

    # Summary
    print(f"\n--- Migration Report ---")
    print(f"Total files: {len(results)}")
    migrated = sum(1 for r in results if r[3] == "migrated")
    failed = len(results) - migrated
    print(f"Migrated: {migrated}")
    print(f"Failed: {failed}")

    remaining = list(backlog_dir.glob("*.md"))
    if not remaining:
        print(f"\nBacklog directory is now clean.")
    else:
        print(f"\nRemaining files: {[f.name for f in remaining]}")


if __name__ == "__main__":
    migrate()
