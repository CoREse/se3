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
  (b) its only content is a single file literally named ``.jsonl`` AND that
      file carries the leak signature ({"step_type": "test", ...} /
      "Test execution (fix iteration 0)");
  (c) its name is a known test-fixture residue name (``se3``, ``test-flow*``).

Hard protection -- never touched:
  - valid flow-id dirs   ``^\d{8}-\d{6}_[0-9a-f]{8}$`` (current naming scheme);
  - ``recovered_YYYYMMDD_HHMMSS`` recovery snapshots;
  - old uuid-style flow dirs (e.g. ``173a47a7-c95``): although the task framed
    these as "uuid residue", inspection showed they hold REAL historical step
    prompts (real analyze/plan/discover conversations), so deleting them would
    destroy genuine flow history. They are preserved by default. Only
    ``--include-uuid-dirs`` opts them in, and even then a dir is removed solely
    when it also matches a content leak signature (a) or (b) -- never a
    multi-step dir.

Stray ``prompt_history*`` FILES left at the history root by tests are also
removed (real flows never write files directly at the history root).

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


def _is_protected_name(name: str) -> bool:
    """Names that always denote a real flow / recovery snapshot -> never delete."""
    return bool(FLOW_ID_RE.match(name)) or name.startswith("recovered_")


def _is_residue_name(name: str) -> bool:
    """Known test-fixture directory names (never used by a real flow)."""
    return name == "se3" or name.startswith("test-flow")


def _is_test_leak_jsonl(path: Path) -> bool:
    """A lone ``.jsonl`` file is only a leak if it carries the test signature.

    Guards against ever removing a real (non-empty-step_id) history record; the
    empty-step_id leak always records ``"step_type": "test"`` on its first line.
    """
    try:
        with path.open("r", encoding="utf-8") as fh:
            first = fh.readline()
    except OSError:
        return False
    return '"step_type": "test"' in first or "Test execution (fix iteration" in first


def classify_dir(entry: Path, include_uuid_dirs: bool) -> str | None:
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

    # (b) single ".jsonl" (empty-step_id) leak file, content-verified.
    if (
        len(contents) == 1
        and contents[0].is_file()
        and contents[0].name == LEAK_JSONL_NAME
        and _is_test_leak_jsonl(contents[0])
    ):
        return "only_jsonl"

    # Everything below is name-based; real flows and recovery snapshots are
    # protected here so a multi-step dir is never mistaken for residue.
    if _is_protected_name(name):
        return None

    # (c) explicit test-fixture residue names.
    if _is_residue_name(name):
        return "residue_name"

    # Old uuid-style dirs hold real history, so name-based deletion is OFF by
    # default. --include-uuid-dirs opts them in only if separately confirmed
    # disposable; content-leak uuid dirs are already caught by (a)/(b) above.
    if include_uuid_dirs and UUID_DIR_RE.match(name):
        return "uuid_name"

    return None


def cleanup(history_dir: Path, dry_run: bool, include_uuid_dirs: bool) -> int:
    if not history_dir.is_dir():
        print(f"History directory not found: {history_dir}")
        return 1

    stats = {
        "empty": 0,
        "only_jsonl": 0,
        "residue_name": 0,
        "residue_file": 0,
        "uuid_name": 0,
    }
    to_delete: list[tuple[Path, str]] = []
    kept_flow = 0
    kept_uuid = 0

    for entry in sorted(history_dir.iterdir()):
        if entry.is_dir():
            category = classify_dir(entry, include_uuid_dirs)
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
    print(f"Uuid-style dirs (opt-in):   {stats['uuid_name']}")
    print(f"Residue root files:         {stats['residue_file']}")
    print(f"Total {'to remove' if dry_run else 'removed'}: {total}")
    print(f"Preserved real flows (flow-id/recovered/other): {kept_flow}")
    print(f"Preserved old uuid-style flow dirs:             {kept_uuid}")
    if kept_uuid and not include_uuid_dirs:
        print(
            "  (uuid-style dirs contain real historical flow content and are "
            "kept; pass --include-uuid-dirs only if separately confirmed "
            "disposable -- even then only content-leak dirs are removed.)"
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
    parser.add_argument(
        "--include-uuid-dirs",
        action="store_true",
        help="also consider old uuid-style dirs (preserved by default; see docstring)",
    )
    args = parser.parse_args(argv)

    history_dir = args.history_dir or (_project_root() / "se3" / "history")
    return cleanup(history_dir.resolve(), args.dry_run, args.include_uuid_dirs)


if __name__ == "__main__":
    sys.exit(main())
