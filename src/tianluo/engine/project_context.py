"""Project context collector for the flow engine.

Collects project-wide context (git status, flow history, backlog, specs)
into a structured dict. Used by the PROJECT_SUMMARY step and `se3 summary` CLI.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from .context_builder import ContextBuilder

logger = logging.getLogger(__name__)


class ProjectContextCollector:
    """Collects project-wide context from multiple sources."""

    def __init__(self, project_root: Path):
        self.project_root = Path(project_root)

    def collect(self) -> Dict[str, Any]:
        """Collect all project context into a structured dict.

        Returns:
            Dict with keys: git, flow_engine, backlog, specs
        """
        return {
            "git": self._collect_git(),
            "flow_engine": self._collect_flow_engine(),
            "backlog": self._collect_backlog(),
            "specs": self._collect_specs(),
        }

    def _collect_git(self) -> Dict[str, Any]:
        """Collect git status using commands/status.py helper."""
        try:
            from ..commands.status import compute_git_status
            return compute_git_status(self.project_root)
        except Exception as e:
            logger.debug(f"Failed to collect git status: {e}")
            return {"branch": "unknown", "uncommitted_count": 0, "last_commits": []}

    def _collect_flow_engine(self) -> Optional[Dict[str, Any]]:
        """Collect flow engine history using commands/status.py helper."""
        try:
            from ..commands.status import compute_flow_engine_status
            return compute_flow_engine_status(self.project_root)
        except Exception as e:
            logger.debug(f"Failed to collect flow engine status: {e}")
            return None

    def _collect_backlog(self) -> List[Dict[str, str]]:
        """Scan se3/specs/_backlog/*.md for backlog items.

        Extracts title, Status, and Phase from frontmatter-style headers.
        """
        backlog_dir = self.project_root / "se3" / "specs" / "_backlog"
        if not backlog_dir.exists():
            return []

        items = []
        for md_file in sorted(backlog_dir.glob("*.md")):
            try:
                content = md_file.read_text(encoding="utf-8")
                item = self._parse_backlog_item(md_file.stem, content)
                items.append(item)
            except Exception as e:
                logger.debug(f"Failed to parse backlog item {md_file}: {e}")
                continue

        return items

    def _parse_backlog_item(self, slug: str, content: str) -> Dict[str, str]:
        """Parse a backlog markdown file for title, status, phase."""
        lines = content.split("\n")
        title = slug
        status = ""
        phase = ""

        for line in lines[:10]:  # Only scan first 10 lines
            line_stripped = line.strip()
            if line_stripped.startswith("# "):
                title = line_stripped[2:].strip()
            elif line_stripped.startswith("**Status**:"):
                status = line_stripped.split(":", 1)[1].strip().rstrip("*")
            elif line_stripped.startswith("**Phase**:"):
                phase = line_stripped.split(":", 1)[1].strip().rstrip("*")

        return {"slug": slug, "title": title, "status": status, "phase": phase}

    def _collect_specs(self) -> List[str]:
        """List available spec names."""
        builder = ContextBuilder(self.project_root)
        return list_spec_names(builder.specs_dir)


def list_spec_names(specs_dir: Path) -> List[str]:
    """List spec directory names, excluding internal dirs (prefixed with _).

    Args:
        specs_dir: Path to the specs directory

    Returns:
        Sorted list of spec names
    """
    if not specs_dir.exists():
        return []

    names = []
    for entry in specs_dir.iterdir():
        if entry.is_dir() and not entry.name.startswith("_"):
            names.append(entry.name)

    return sorted(names)
