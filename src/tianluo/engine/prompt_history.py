"""Prompt history management for SE3 interactive input.

Provides FileHistory instances for prompt_toolkit's PromptSession,
enabling up/down arrow navigation through previous prompts.
"""

from __future__ import annotations
from tianluo.runtime_paths import runtime_dir

import logging
from pathlib import Path

from prompt_toolkit.history import FileHistory

logger = logging.getLogger(__name__)

HISTORY_FILENAME = "prompt_history"
HISTORY_DIR = "history"
MAX_ENTRIES = 500


def _truncate_history(path: Path, max_entries: int = MAX_ENTRIES) -> None:
    """Truncate history file to keep only the last max_entries entries.

    prompt_toolkit's FileHistory format uses lines prefixed with '+' for
    multiline entries, with blank lines separating entries. Example:

        +first line of entry 1
        +second line of entry 1

        +single line entry 2

    Each entry is a block of consecutive '+'-prefixed lines, separated
    by empty lines.
    """
    if not path.exists():
        return

    try:
        content = path.read_text(encoding="utf-8")
    except (IOError, OSError):
        return

    if not content.strip():
        return

    # Parse entries: split by blank lines, filter empty chunks
    lines = content.split("\n")
    entries: list[list[str]] = []
    current_entry: list[str] = []

    for line in lines:
        if line.startswith("+"):
            current_entry.append(line)
        else:
            if current_entry:
                entries.append(current_entry)
                current_entry = []

    if current_entry:
        entries.append(current_entry)

    if len(entries) <= max_entries:
        return

    # Keep only the last max_entries
    kept = entries[-max_entries:]
    try:
        with open(path, "w", encoding="utf-8") as f:
            for i, entry in enumerate(kept):
                for line in entry:
                    f.write(line + "\n")
                # Blank line separator between entries
                f.write("\n")
        logger.debug(
            "Truncated prompt history from %d to %d entries", len(entries), len(kept)
        )
    except (IOError, OSError) as e:
        logger.warning("Failed to truncate history file: %s", e)


def get_prompt_history(project_root: Path) -> FileHistory:
    """Get a FileHistory instance for the given project.

    The history file is stored at tianluo/history/prompt_history relative
    to project_root. The directory is created if it doesn't exist.
    The file is truncated if it exceeds MAX_ENTRIES.

    Args:
        project_root: Project root directory.

    Returns:
        A prompt_toolkit FileHistory instance.
    """
    history_dir = runtime_dir(project_root) / HISTORY_DIR
    history_dir.mkdir(parents=True, exist_ok=True)

    history_path = history_dir / HISTORY_FILENAME

    # Truncate if needed before creating FileHistory (which reads the file)
    _truncate_history(history_path)

    return FileHistory(str(history_path))
