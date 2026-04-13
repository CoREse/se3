"""SE3 Sync command — Check and synchronize specs with project code.

Usage:
    se3 sync                    # Sync with default mode
    se3 sync --mode=strict      # All conflicts require human decision
    se3 sync --mode=fast        # LLM handles all conflicts automatically
"""

from __future__ import annotations

import json
import logging
from enum import Enum
from pathlib import Path
from typing import Optional

import typer

from ..engine.display import render_text

logger = logging.getLogger(__name__)


class SyncMode(str, Enum):
    """Conflict handling mode for sync."""

    DEFAULT = "default"
    STRICT = "strict"
    FAST = "fast"


def sync_command(
    mode: SyncMode = SyncMode.DEFAULT,
    project_root: Path | None = None,
) -> None:
    """Check and synchronize se3/specs/ with project code.

    Args:
        mode: Conflict handling mode.
        project_root: Project root directory. Auto-detected if None.
    """
    if project_root is None:
        from .run import get_project_root
        project_root = get_project_root()

    render_text(
        f"Sync mode: {mode.value}\n"
        f"Project root: {project_root}",
        title="SE3 Sync",
    )

    logger.info("se3 sync called with mode=%s, project_root=%s", mode.value, project_root)


def process_call_response(
    call_file: Path,
    project_root: Optional[Path] = None,
) -> None:
    """Process an MCP call response file for sync conflicts.

    Scans the response file for the given call file, parses each
    conflict's decision, and executes the corresponding action.

    Args:
        call_file: Path to the original call file.
        project_root: Project root directory. Auto-detected if None.
    """
    from ..engine.sync_engine import SyncEngine

    if project_root is None:
        from .run import get_project_root
        project_root = get_project_root()

    call_path = Path(call_file)
    if not call_path.exists():
        render_text(f"Call file not found: {call_path}", title="SE3 Sync Error")
        return

    response_path = Path(str(call_path) + ".response")
    if not response_path.exists():
        render_text(f"Response file not found: {response_path}", title="SE3 Sync Error")
        return

    engine = SyncEngine(project_root)
    result = engine.process_call_response(call_path)

    render_text(
        f"Specs updated: {result['specs_updated']}\n"
        f"Issues created: {result['issues_created']}",
        title="SE3 Sync — Call Response Processed",
    )
