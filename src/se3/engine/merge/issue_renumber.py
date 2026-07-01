"""Shared renumber primitives for the two ``se3 merge`` issue channels.

Both the git three-way-merge channel (committed issues, reconciled in the
orchestrator) and the runtime-sync channel (uncommitted worktree issues,
adopted via :meth:`IssueManager.adopt_issue`) must satisfy the *same*
"never lose an issue / never share a numeric ID" guarantee. To keep the two
channels from drifting apart, the mechanical parts of a renumber live here as
pure primitives that both channels call:

* :func:`rewrite_issue_references` — repoint every ``#<old>`` cross-reference
  to ``#<new>`` across the issue store, matching only standalone ``#<digits>``
  tokens so prose like ``#1234`` or ``abc#123`` is never clobbered.
* :func:`advance_next_id_to_max` — push the ``.next_id`` counter to
  ``max(ID) + 1`` under an ``fcntl`` lock so no future allocation collides.
* :func:`format_renumber_trace` — render the "old → new" audit line appended
  into the renumbered issue.

Every read/write here is confined to ``se3/issues/`` (the issue files plus the
``.next_id`` counter); nothing else under ``se3/`` is touched.
"""

from __future__ import annotations

import fcntl
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# A cross-reference is a ``#`` followed by digits, standalone as its own token.
# ``(?<![0-9A-Za-z])`` rejects a leading alnum (so ``abc#123`` is prose, not a
# ref); greedy ``\d+`` absorbs every trailing digit, so ``#1234`` parses as the
# integer 1234 (not a match when renumbering 123) — the "trailing non-digit"
# boundary falls out for free. Zero-padding equivalence (``#14`` vs ``#014``)
# is handled by comparing the captured integer value, not the literal text.
_REF_TOKEN = re.compile(r"(?<![0-9A-Za-z])#(\d+)")


def _norm_id(value: object) -> int:
    """Coerce an ID (zero-padded string or int) to its integer value."""
    return int(str(value).strip())


def _issue_files(issues_dir: Path):
    """Yield every ``*.yaml`` under ``se3/issues/{open,closed}/`` (sorted)."""
    for sub in ("open", "closed"):
        directory = issues_dir / sub
        if not directory.exists():
            continue
        # Sorted so a rewrite touches files in a stable order (deterministic
        # logs / test assertions); order is otherwise irrelevant.
        for f in sorted(directory.glob("*.yaml")):
            yield f


def rewrite_issue_references(project_root: Path, old_id: object, new_id: object) -> int:
    """Rewrite every ``#<old_id>`` cross-reference to ``#<new_id>`` in the store.

    Scans ``se3/issues/{open,closed}/*.yaml`` and, in each file's raw text,
    replaces standalone ``#<digits>`` tokens whose integer value equals
    *old_id* with a zero-padded ``#<new_id>``. Matching is by integer value so
    ``#14`` and ``#014`` are treated as the same reference; token boundaries
    keep ``#1234`` and ``abc#123`` untouched.

    Args:
        project_root: Project root (contains ``se3/issues/``).
        old_id: The ID being retired (zero-padded string or int).
        new_id: The replacement ID (zero-padded string or int).

    Returns:
        The number of individual references rewritten across all files.
    """
    old_val = _norm_id(old_id)
    new_ref = f"#{_norm_id(new_id):03d}"
    issues_dir = project_root / "se3" / "issues"

    total = 0
    for path in _issue_files(issues_dir):
        text = path.read_text(encoding="utf-8")

        # Count only the tokens that actually match *this* old_id; the regex
        # matches every ``#<digits>`` so the callback filters by integer value.
        file_hits = 0

        def _sub(match: "re.Match[str]") -> str:
            nonlocal file_hits
            if int(match.group(1)) == old_val:
                file_hits += 1
                return new_ref
            return match.group(0)

        rewritten = _REF_TOKEN.sub(_sub, text)
        if file_hits:
            path.write_text(rewritten, encoding="utf-8")
            total += file_hits

    if total:
        logger.info(
            "Rewrote %d issue reference(s) #%03d -> %s",
            total, old_val, new_ref,
        )
    return total


def advance_next_id_to_max(project_root: Path) -> int:
    """Push ``se3/issues/.next_id`` to ``max(existing ID) + 1``.

    Scans issue *filenames* under ``open/`` and ``closed/`` for the highest
    numeric ID and writes ``max + 1`` into the counter. The read-modify-write
    is serialized with ``fcntl.flock(LOCK_EX)`` — the same lock discipline
    :meth:`IssueManager._next_id` uses — so a concurrent allocator can never
    observe a torn value. A missing or garbage counter is simply overwritten;
    the counter is derived purely from the files on disk, so it self-heals.

    Returns:
        The value written (``max(ID) + 1``).
    """
    issues_dir = project_root / "se3" / "issues"
    issues_dir.mkdir(parents=True, exist_ok=True)
    counter_file = issues_dir / ".next_id"

    max_id = 0
    for path in _issue_files(issues_dir):
        match = re.match(r"^(\d+)_", path.name)
        if match:
            num = int(match.group(1))
            if num > max_id:
                max_id = num
    target = max_id + 1

    # ``a+`` creates the counter if absent; the exclusive lock serializes the
    # overwrite against IssueManager._next_id and any peer renumber.
    with open(counter_file, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.seek(0)
            fh.truncate()
            fh.write(str(target))
            fh.flush()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)

    logger.info("Advanced .next_id to %d (max ID + 1)", target)
    return target


def format_renumber_trace(old_id: object, new_id: object) -> str:
    """Render the traceable "old → new" audit line for a renumbered issue.

    The text is appended to the tail of the renumbered issue's description.
    It goes at the *end* so it never shifts the first non-empty line, leaving
    ``Issue.display_title`` / slug derivation unchanged.

    Returns:
        A line of the form ``旧号 #014 → 新号 #240 (se3 merge)``.
    """
    return f"旧号 #{_norm_id(old_id):03d} → 新号 #{_norm_id(new_id):03d} (se3 merge)"


# Matches one full audit line produced by :func:`format_renumber_trace`. Kept
# next to the formatter so the two never drift: whoever changes the trace text
# updates the pattern that strips it back out.
_TRACE_LINE_RE = re.compile(r"^\s*旧号 #\d+ → 新号 #\d+ \(se3 merge\)\s*$")


def strip_renumber_traces(description: str) -> str:
    """Return *description* with every renumber-trace line removed.

    The runtime-sync channel dedups worktree issues against the main project by
    content signature. Once an issue has been renumbered, its main-project copy
    carries a :func:`format_renumber_trace` line that the un-renumbered worktree
    source lacks — so a naive signature would no longer match on a re-run and
    the merge would wrongly re-adopt an already-merged issue. Stripping the
    trace before signing keeps the dedup (and thus idempotency) intact.
    """
    if not description:
        return description
    kept = [
        line for line in description.splitlines()
        if not _TRACE_LINE_RE.match(line)
    ]
    return "\n".join(kept).rstrip()
