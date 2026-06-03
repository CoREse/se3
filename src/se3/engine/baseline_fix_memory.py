"""Cross-flow persistent memory of baseline (inherited) failures given up on.

Mechanism B (see flow-engine) lets the fix loop attempt to repair **inherited**
(baseline) test failures, but only within an independently bounded budget. When
that budget is exhausted for a given baseline failure, the flow gives up and
surfaces it. Without a cross-flow memory, the *next* flow on the same project
would re-attempt the exact same un-fixable baseline failure (a missing system
library, a flaky test, something needing a human decision) and burn the budget
again — every flow, forever.

This module persists, per ``test_id``, the fact that a baseline failure was
attempted and given up on, so subsequent flows can skip looping it:

- :func:`load_given_up` — return the set of test ids already given up on.
- :func:`record_given_up` — mark one or more test ids as given up, accumulating
  the attempt count and recording the latest reason.

Persistence mirrors :mod:`se3.engine.test_baseline`'s cache idioms: a
schema-versioned JSON document written atomically (tempfile + ``os.replace``),
read corruption-tolerantly (a missing / corrupt / schema-mismatched file reads
as an empty set), and bounded to :data:`MAX_ENTRIES` most-recently-touched ids
(insertion-order LRU) so the file cannot grow without limit.

The store lives at ``se3/state/baseline_fix_attempts.json`` — gitignored by the
``/se3/*`` rule and therefore never tracked.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Dict, Iterable, Optional, Set

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASELINE_FIX_MEMORY_SCHEMA_VERSION = 1
_MEMORY_REL_PATH = Path("se3") / "state" / "baseline_fix_attempts.json"

# Bound the on-disk store so a long-lived project accumulating given-up baseline
# failures over many flows cannot grow the file without limit. Most-recently
# touched ids are retained (insertion-order LRU).
MAX_ENTRIES = 500


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def memory_path(project_root: Path) -> Path:
    """Return the path to the baseline-fix memory file for *project_root*."""
    return Path(project_root) / _MEMORY_REL_PATH


# ---------------------------------------------------------------------------
# Read (corruption-tolerant)
# ---------------------------------------------------------------------------

def _read_entries(path: Path) -> Dict[str, dict]:
    """Best-effort read of the memory's ``entries`` map.

    Returns an empty dict on any problem (missing file, corrupt JSON, unexpected
    shape, schema mismatch), so a damaged store reads as a clean empty set rather
    than raising.
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read baseline-fix memory at %s: %s", path, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    if data.get("schema_version") != BASELINE_FIX_MEMORY_SCHEMA_VERSION:
        return {}
    entries = data.get("entries")
    if not isinstance(entries, dict):
        return {}
    # Keep only string-keyed dict entries; tolerate stray malformed values.
    return {
        str(k): v
        for k, v in entries.items()
        if isinstance(k, str) and isinstance(v, dict)
    }


def load_given_up(project_root: Path) -> Set[str]:
    """Return the set of test ids already given up on (never re-loop these).

    A missing / corrupt / schema-mismatched store reads as the empty set.
    """
    return set(_read_entries(memory_path(project_root)).keys())


def load_given_up_details(project_root: Path) -> Dict[str, dict]:
    """Return the full per-test_id metadata map (``attempts`` / ``reason``).

    Like :func:`load_given_up` but exposes the accumulated metadata for callers
    that want to surface *why* a baseline failure was abandoned. A missing /
    corrupt store reads as an empty dict.
    """
    return dict(_read_entries(memory_path(project_root)))


# ---------------------------------------------------------------------------
# Write (atomic, LRU-bounded)
# ---------------------------------------------------------------------------

def record_given_up(
    project_root: Path,
    test_ids: Iterable[str],
    *,
    attempts: int,
    reason: Optional[str] = None,
) -> Path:
    """Mark *test_ids* as given-up baseline failures, atomically.

    For each id the recorded ``attempts`` count is **accumulated** (added to any
    prior value) and the ``reason`` is updated to the latest non-None value. Each
    touched id is moved to the end (most-recent) so the insertion-order LRU keeps
    the freshest ids when trimming to :data:`MAX_ENTRIES`.

    A non-positive ``attempts`` is clamped to ``0`` for the accumulation. The
    write is atomic (tempfile + ``os.replace``) so a mid-write crash never leaves
    a half-written file behind. Existing entries for other ids are preserved.

    Returns the path written. Calling with an empty ``test_ids`` is a no-op that
    still returns the path without touching disk.
    """
    ids = [str(t) for t in test_ids if str(t)]
    path = memory_path(project_root)
    if not ids:
        return path

    delta = attempts if isinstance(attempts, int) and attempts > 0 else 0

    path.parent.mkdir(parents=True, exist_ok=True)
    entries = dict(_read_entries(path))

    for test_id in ids:
        prior = entries.get(test_id, {})
        prior_attempts = prior.get("attempts", 0)
        if not isinstance(prior_attempts, int) or prior_attempts < 0:
            prior_attempts = 0
        new_attempts = prior_attempts + delta
        new_reason = reason if reason is not None else prior.get("reason")
        # Move to end (most-recent) on re-record.
        entries.pop(test_id, None)
        entry: dict = {"attempts": new_attempts}
        if new_reason is not None:
            entry["reason"] = new_reason
        entries[test_id] = entry

    # Trim oldest entries beyond the cap.
    while len(entries) > MAX_ENTRIES:
        oldest_key = next(iter(entries))
        entries.pop(oldest_key)

    payload = json.dumps(
        {
            "schema_version": BASELINE_FIX_MEMORY_SCHEMA_VERSION,
            "entries": entries,
        },
        indent=2,
        ensure_ascii=False,
    )

    fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent), prefix=".baseline_fix_attempts.", suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        os.replace(tmp_path, str(path))
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    logger.debug(
        "Recorded %d given-up baseline failure(s) to %s (attempts+=%d, reason=%r)",
        len(ids), path, delta, reason,
    )
    return path
