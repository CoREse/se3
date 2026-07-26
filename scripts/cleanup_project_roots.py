#!/usr/bin/env python3
r"""One-time cleanup: remove test-leak residue from ~/.se3/project_roots.json.

Background
----------
Before the discovery tests marked their spawned ``se3 run`` stubs with
``SE3_EXTERNAL_SCAN_IGNORE`` (see ``tests/conftest.py`` /
``tianluo.daemon.supervisor``), a real daemon running on the same dev machine would
pick up those minutes-long fake ``se3 run`` processes during its ``psutil``
external scan and persist their pytest-tempdir cwd into the real registry
(``~/.se3/project_roots.json``), surfacing bogus ``/tmp/pytest-of-cre/...``
projects in the daemon / WebUI project list.

The daemon's existence-based self-heal (``_sanitize_project_roots`` /
``_read_project_roots``) drops a registry entry only once its directory no
longer exists on disk. But pytest retains the last few numbered tempdirs, so a
just-written ``/tmp/pytest-of-cre/pytest-NNN/...`` root still exists and the
self-heal keeps it — potentially for days. Those entries are unambiguous test
artifacts regardless of path existence, so this one-time pass removes them
directly.

Deletion criteria -- an entry is removed iff:
  (a) its path lies under one of the explicitly allowed pytest temp-root
      prefixes -- ``/tmp/pytest-of-<user>/`` or ``/var/tmp/pytest-of-<user>/``.
      These are the OS-tempdir locations pytest's tmpdir factory nests its
      per-run dirs under, so an entry there is unambiguous test-run residue
      (removed even when the directory still exists). The match is anchored to
      the temp-root prefix -- NOT to a bare ``pytest-of-<user>`` segment
      anywhere in the path -- so a real project root that merely happens to
      contain a ``pytest-of-<user>`` directory component (e.g.
      ``/home/cre/projects/pytest-of-cre/demo``) is NOT mistaken for residue;
  (b) OR ``--prune-missing`` is passed and the path no longer exists on disk
      (mirrors the daemon's own existence-based self-heal for any stale root).

Real project roots (existing, non-pytest paths) are never touched.

Usage:
    python scripts/cleanup_project_roots.py --dry-run
    python scripts/cleanup_project_roots.py                 # remove pytest residue
    python scripts/cleanup_project_roots.py --prune-missing  # + drop vanished roots
    python scripts/cleanup_project_roots.py --registry /path/to/project_roots.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

# The explicitly allowed pytest temp-root prefixes (from the task): pytest's
# tmpdir factory nests per-run dirs under ``<os-tmp>/pytest-of-<user>/pytest-N/``.
# We anchor to the OS tempdir roots -- ``/tmp`` and ``/var/tmp`` -- rather than
# matching a bare ``pytest-of-<user>`` segment anywhere, so a real project root
# that merely contains such a directory component (e.g.
# ``/home/cre/projects/pytest-of-cre/demo``) is never treated as residue.
PYTEST_RESIDUE_RE = re.compile(r"^/(?:var/)?tmp/pytest-of-[^/]+/")


def _default_registry() -> Path:
    """Locate ``project_roots.json`` the same way the daemon does.

    Honours ``SE3_DAEMON_DIR`` (the daemon's home override) and falls back to
    ``~/.se3``.
    """
    base = os.environ.get("SE3_DAEMON_DIR")
    root = Path(base) if base else (Path.home() / ".se3")
    return root / "project_roots.json"


def _is_pytest_residue(path: str) -> bool:
    return bool(PYTEST_RESIDUE_RE.search(path))


def cleanup(registry: Path, dry_run: bool, prune_missing: bool) -> int:
    if not registry.is_file():
        print(f"Registry not found (nothing to clean): {registry}")
        return 0

    try:
        data = json.loads(registry.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"Could not read registry {registry}: {exc}")
        return 1

    roots = data.get("project_roots") if isinstance(data, dict) else None
    if not isinstance(roots, list):
        print(f"Registry has no 'project_roots' list: {registry}")
        return 0

    kept: list[str] = []
    removed: list[tuple[str, str]] = []
    for entry in roots:
        if not isinstance(entry, str):
            continue
        if _is_pytest_residue(entry):
            removed.append((entry, "pytest-residue"))
        elif prune_missing and not os.path.exists(entry):
            removed.append((entry, "missing"))
        else:
            kept.append(entry)

    action = "[DRY-RUN] would remove" if dry_run else "removing"
    for entry, reason in removed:
        print(f"  {action} ({reason}): {entry}")

    if removed and not dry_run:
        data["project_roots"] = sorted(set(kept))
        tmp = registry.with_suffix(registry.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(tmp, registry)

    print("\n--- Cleanup Report ---")
    print(f"Registry: {registry}")
    print(f"Mode: {'DRY-RUN (no writes)' if dry_run else 'WRITE'}")
    print(f"pytest-residue entries removed: {sum(1 for _, r in removed if r == 'pytest-residue')}")
    if prune_missing:
        print(f"Missing-path entries removed:   {sum(1 for _, r in removed if r == 'missing')}")
    print(f"Total {'to remove' if dry_run else 'removed'}: {len(removed)}")
    print(f"Preserved real roots: {len(kept)}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="list entries that would be removed without writing anything",
    )
    parser.add_argument(
        "--registry",
        type=Path,
        default=None,
        help="path to project_roots.json (default: $SE3_DAEMON_DIR or ~/.se3)",
    )
    parser.add_argument(
        "--prune-missing",
        action="store_true",
        help="also drop entries whose path no longer exists (like the daemon self-heal)",
    )
    args = parser.parse_args(argv)

    registry = (args.registry or _default_registry()).expanduser()
    return cleanup(registry, args.dry_run, args.prune_missing)


if __name__ == "__main__":
    sys.exit(main())
