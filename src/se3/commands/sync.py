"""SE3 Sync command — Check and synchronize specs with project code.

Usage:
    se3 sync                    # Sync with default mode
    se3 sync --mode=strict      # All conflicts require human decision
    se3 sync --mode=fast        # LLM handles all conflicts automatically
"""

from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path

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
