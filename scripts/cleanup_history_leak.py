#!/usr/bin/env python3
r"""One-time cleanup: remove test-leak residue from se3/history/.

Background
----------
A test-isolation gap let ``src/se3/engine/test_steps.py``'s
``test_test_success`` / ``test_test_failure`` drive ``run_test_step()`` against
the real project_root with a fresh ``FlowInstance``. Each full test run
therefore wrote a fake test-history record into
``se3/history/<generated-flow-id>/.jsonl`` -- the step_id was empty, so the
file is literally named ``.jsonl``. Over hundreds of test runs this left a large
backlog that ``se3 history list`` surfaces as noise ("Test execution (fix
iteration 0)"). The leak itself is fixed upstream (isolation fixtures in
tests/conftest.py + src/se3/engine/conftest.py); this script removes the
accumulated backlog once.

Deletion criteria -- a directory is removed iff:
  (a) it is empty                -- a real flow always writes >=1 step file;
  (b) its only content is a single file literally named ``.jsonl``. A real flow
      always names its step files ``<step_id>.jsonl`` with a NON-empty step_id,
      so a bare ``.jsonl`` (empty step_id) is unambiguously the leak regardless
      of what the file contains -- an empty / truncated / signature-less leak
      file is still residue and must be removed;
  (c) its name is a known test-fixture residue name (``se3``, ``test-flow*``,
      ``prompt_history*``).

Hard protection -- never touched:
  - valid flow-id dirs   ``^\d{8}-\d{6}_[0-9a-f]{8}$`` (current naming scheme);
  - ``recovered_YYYYMMDD_HHMMSS`` recovery snapshots;
  - old uuid-style flow dirs (e.g. ``173a47a7-c95``): although the task framed
    these as "uuid residue", inspection showed they hold REAL historical step
    prompts (real analyze/plan/discover conversations), so deleting them would
    destroy genuine flow history. They are ALWAYS preserved -- there is no
    name-based deletion of uuid dirs at all. A uuid dir that happens to be empty
    or a single-``.jsonl`` leak is still removed, but only because criteria
    (a)/(b) above catch it as a content leak; a multi-step uuid dir holding real
    history is never touched.

Stray ``prompt_history*`` entries left at the history root by tests are also
removed whether they are files or directories (real flows never write
``prompt_history*`` at the history root -- files via the residue-file path,
directories via criterion (c)).

Usage:
    python scripts/cleanup_history_leak.py --dry-run
    python scripts/cleanup_history_leak.py            # perform deletion
    python scripts/cleanup_history_leak.py --history-dir /path/to/se3/history
"""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path

# Current flow-id scheme: 20260706-013803_96453dd6
FLOW_ID_RE = re.compile(r"^\d{8}-\d{6}_[0-9a-f]{8}$")
# Old uuid-style flow-id dir: 173a47a7-c95 (uuid4[:12]) -- REAL historical flows.
UUID_DIR_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{3}$")

# The empty-step_id leak writes exactly this filename (step_id == "").
LEAK_JSONL_NAME = ".jsonl"


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _is_leak_jsonl_file(path: Path) -> bool:
    """A file literally named ``.jsonl`` is the empty-step_id leak, always.

    A real flow writes ``<step_id>.jsonl`` with a non-empty ``step_id``; only the
    isolation-gap test leak produced an empty ``step_id`` and therefore a file
    named ``.jsonl``. The name alone is the signature -- content is NOT inspected,
    so an empty / truncated / signature-less leak file is still recognised and
    removed. (A non-empty ``step_id`` never collides with this name, so no real
    history record can ever be mistaken for the leak.)
    """
    return path.is_file() and path.name == LEAK_JSONL_NAME


def _is_protected_name(name: str) -> bool:
    """Names that always denote a real flow / recovery snapshot -> never delete."""
    return bool(FLOW_ID_RE.match(name)) or name.startswith("recovered_")


def _is_residue_name(name: str) -> bool:
    """Known test-fixture directory names (never used by a real flow)."""
    return (
        name in ("se3", "tianluo")
        or name.startswith("test-flow")
        or name.startswith("prompt_history")
    )


def classify_dir(entry: Path) -> str | None:
    """Return the leak category for a history subdir, or None to keep it."""
    name = entry.name
    try:
        contents = list(entry.iterdir())
    except OSError:
        return None

    # (a) empty leak dir -- real flows always write at least one step file. This
    # content signal overrides name protection: an empty flow-id dir is a leak,
    # not a real flow.
    if not contents:
        return "empty"

    # (b) single ".jsonl" (empty-step_id) leak file. The bare ``.jsonl`` name is
    # itself the signature (a real flow always uses a non-empty step_id), so the
    # file's content is irrelevant -- an empty / truncated / signature-less leak
    # file is still removed.
    if len(contents) == 1 and _is_leak_jsonl_file(contents[0]):
        return "only_jsonl"

    # Everything below is name-based; real flows and recovery snapshots are
    # protected here so a multi-step dir is never mistaken for residue.
    if _is_protected_name(name):
        return None

    # (c) explicit test-fixture residue names.
    if _is_residue_name(name):
        return "residue_name"

    # Old uuid-style dirs hold REAL historical flow content, so there is NO
    # name-based deletion for them -- a multi-step uuid dir is never removed.
    # A uuid dir that is itself a content leak (empty / single-``.jsonl``) was
    # already caught by (a)/(b) above and never reaches this point.
    return None


def cleanup(history_dir: Path, dry_run: bool) -> int:
    if not history_dir.is_dir():
        print(f"History directory not found: {history_dir}")
        return 1

    stats = {
        "empty": 0,
        "only_jsonl": 0,
        "residue_name": 0,
        "residue_file": 0,
    }
    to_delete: list[tuple[Path, str]] = []
    kept_flow = 0
    kept_uuid = 0

    for entry in sorted(history_dir.iterdir()):
        if entry.is_dir():
            category = classify_dir(entry)
            if category in stats:
                to_delete.append((entry, category))
            else:
                if _is_protected_name(entry.name):
                    kept_flow += 1
                elif UUID_DIR_RE.match(entry.name):
                    kept_uuid += 1
                else:
                    kept_flow += 1  # unknown-but-non-leak -> keep, count as kept
        elif entry.is_file() and entry.name.startswith("prompt_history"):
            # Stray leak files at the history root (real flows never write here).
            to_delete.append((entry, "residue_file"))

    action = "[DRY-RUN] would remove" if dry_run else "removing"
    for path, category in to_delete:
        print(f"  {action} ({category}): {path.name}")
        stats[category] += 1
        if not dry_run:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()

    total = sum(stats.values())
    print("\n--- Cleanup Report ---")
    print(f"History dir: {history_dir}")
    print(f"Mode: {'DRY-RUN (no deletions)' if dry_run else 'DELETE'}")
    print(f"Empty leak dirs:            {stats['empty']}")
    print(f"Single-'.jsonl' leak dirs:  {stats['only_jsonl']}")
    print(f"Residue-name dirs:          {stats['residue_name']}")
    print(f"Residue root files:         {stats['residue_file']}")
    print(f"Total {'to remove' if dry_run else 'removed'}: {total}")
    print(f"Preserved real flows (flow-id/recovered/other): {kept_flow}")
    print(f"Preserved old uuid-style flow dirs:             {kept_uuid}")
    if kept_uuid:
        print(
            "  (uuid-style dirs contain real historical flow content and are "
            "always preserved; only empty / single-'.jsonl' content leaks are "
            "ever removed.)"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list directories that would be removed without deleting anything",
    )
    parser.add_argument(
        "--history-dir",
        type=Path,
        default=None,
        help="path to se3/history (default: <project_root>/se3/history)",
    )
    args = parser.parse_args(argv)

    if args.history_dir:
        history_dir = args.history_dir
    else:
        root = _project_root()
        history_dir = root / "tianluo" / "history"
        if not history_dir.is_dir() and (root / "se3" / "history").is_dir():
            history_dir = root / "se3" / "history"
    return cleanup(history_dir.resolve(), args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
