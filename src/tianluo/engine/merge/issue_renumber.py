"""Shared renumber primitives for the two ``luo merge`` issue channels.

Both the git three-way-merge channel (committed issues, reconciled in the
orchestrator) and the runtime-sync channel (uncommitted worktree issues,
adopted via :meth:`IssueManager.adopt_issue`) must satisfy the *same*
"never lose an issue / never share a numeric ID" guarantee. To keep the two
channels from drifting apart, the mechanical parts of a renumber live here as
pure primitives that both channels call:

* :func:`rewrite_issue_references` — repoint every ``#<old>`` cross-reference
  to ``#<new>`` across the issue store, matching only standalone ``#<digits>``
  tokens so prose like ``#1234`` or ``abc#123`` is never clobbered.
* :func:`rewrite_issue_references_bulk` — apply a whole batch of old→new
  pairs in ONE simultaneous pass, for callers renumbering several issues at
  once (chaining single-pair rewrites is unsound — see its docstring).
* :func:`rewrite_references_in_added_lines` — rewrite ``#<old>`` only in the
  lines a file gained relative to a baseline text, for pre-existing files the
  merged branch edited (their old lines still reference the kept issue).
* :func:`advance_next_id_to_max` — synchronize the ``.next_id`` counter to
  exactly ``max(ID) + 1`` under an ``fcntl`` lock, so no future allocation
  collides and the counter always matches the post-renumber global maximum.
* :func:`scan_max_issue_id` — the store scan behind that sync (filename
  prefixes AND parsed ``id`` fields), shared with
  ``IssueManager._next_id`` so every allocator agrees on what "the highest
  live ID" means.
* :func:`format_renumber_trace` — render the "old → new" audit line appended
  into the renumbered issue.
* :func:`parse_renumber_traces` — read those audit lines back as (old, new)
  integer pairs, so dedup can recognise a rewritten reference as the recorded
  renumber of the original one.
* :func:`format_ambiguous_reference_note` / :func:`append_description_note` /
  :func:`count_reference_tokens` — when SEVERAL renumbered issues shared one
  old ID, a remaining ``#<old>`` reference has no single correct target, so
  instead of guessing (and silently corrupting the reference) both channels
  leave the token in place and record the ambiguity durably next to it.
* :func:`mask_issue_references` — canonicalize ``#<digits>`` tokens, used as a
  coarse candidate prefilter for the renumber-aware dedup comparison.

Every read/write here is confined to ``tianluo/issues/`` (the issue files plus the
``.next_id`` counter); nothing else under ``tianluo/`` is touched.
"""

from __future__ import annotations
from tianluo.runtime_paths import runtime_dir

import fcntl
import logging
import re
from collections import Counter
from pathlib import Path
from typing import Iterable, Optional

import yaml

logger = logging.getLogger(__name__)

# A cross-reference is a ``#`` followed by digits, standalone as its own token.
# ``(?<![0-9A-Za-z])`` rejects a leading alnum (so ``abc#123`` is prose, not a
# ref); the trailing ``(?![0-9A-Za-z])`` rejects a trailing alnum (so
# ``#123abc`` is prose too — greedy ``\d+`` alone only guards against trailing
# digits like ``#1234``, not letters). Zero-padding equivalence (``#14`` vs
# ``#014``) is handled by comparing the captured integer value, not the
# literal text.
_REF_TOKEN = re.compile(r"(?<![0-9A-Za-z])#(\d+)(?![0-9A-Za-z])")


def _norm_id(value: object) -> int:
    """Coerce an ID (zero-padded string or int) to its integer value."""
    return int(str(value).strip())


def _issue_files(issues_dir: Path):
    """Yield every ``*.yaml`` under ``tianluo/issues/{open,closed}/`` (sorted)."""
    for sub in ("open", "closed"):
        directory = issues_dir / sub
        if not directory.exists():
            continue
        # Sorted so a rewrite touches files in a stable order (deterministic
        # logs / test assertions); order is otherwise irrelevant.
        for f in sorted(directory.glob("*.yaml")):
            yield f


def rewrite_issue_references(
    project_root: Path,
    old_id: object,
    new_id: object,
    scope_files: Optional[Iterable[Path]] = None,
) -> int:
    """Rewrite ``#<old_id>`` cross-references to ``#<new_id>`` in the store.

    In each in-scope file's raw text, replaces standalone ``#<digits>`` tokens
    whose integer value equals *old_id* with a zero-padded ``#<new_id>``.
    Matching is by integer value so ``#14`` and ``#014`` are treated as the same
    reference; token boundaries keep ``#1234`` and ``abc#123`` untouched.

    Only *one* side of a collision is renumbered — the other side keeps
    ``old_id``. A ``#<old_id>`` reference is therefore ambiguous: it may point at
    the renumbered issue OR at the kept issue that still owns that number. We
    cannot disambiguate from the digits alone, so the caller MUST pass
    *scope_files* — the set of files belonging to the renumbered (incoming) side.
    Only references *inside those files* meant the incoming issue and are moved
    to ``#<new_id>``; references in the kept side's files (which still point to
    the issue that retained ``old_id``) are left untouched. When *scope_files*
    is ``None`` the rewrite spans the whole store (used only by unit tests that
    exercise token precision in isolation, where no kept side exists).

    Args:
        project_root: Project root (contains ``tianluo/issues/``).
        old_id: The ID being retired (zero-padded string or int).
        new_id: The replacement ID (zero-padded string or int).
        scope_files: The incoming-side files whose references should move. When
            ``None``, every issue file is rewritten.

    Returns:
        The number of individual references rewritten across the scoped files.
    """
    old_val = _norm_id(old_id)
    new_ref = f"#{_norm_id(new_id):03d}"
    issues_dir = runtime_dir(project_root) / "issues"

    if scope_files is None:
        paths = list(_issue_files(issues_dir))
    else:
        # Restrict to the given files that actually exist and live under the
        # issue store — the contract is "only touch tianluo/issues/".
        paths = [
            p for p in scope_files
            if p.exists() and issues_dir in p.parents
        ]

    total = 0
    for path in paths:
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


def rewrite_issue_references_bulk(
    project_root: Path,
    id_map: dict,
    scope_files: Optional[Iterable[Path]] = None,
) -> int:
    """Apply a batch of ``#<old>`` → ``#<new>`` rewrites in ONE simultaneous pass.

    Looping the single-pair :func:`rewrite_issue_references` over a batch is
    unsound whenever one pair's *new* ID equals another pair's *old* ID (e.g.
    ``{005→010, 010→011}``): after the first pass rewrites ``#005`` to
    ``#010``, the produced token is textually indistinguishable from an
    original ``#010`` reference, so the second pass chains it on to ``#011``.
    Resolving every token against the ORIGINAL text in a single pass makes
    the outcome order-independent, so batch renumberers (runtime-sync
    adopting several worktree issues in one sync) MUST use this primitive.

    Same caller-ordering contract as the single-pair primitive: run BEFORE
    the batch's renumber traces are appended — a trace embeds ``#<old>`` as a
    historical record and must not be repointed.

    Args:
        project_root: Project root (contains ``tianluo/issues/``).
        id_map: Mapping of old ID → new ID (zero-padded strings or ints).
            Identity pairs are ignored.
        scope_files: Same scoping contract as
            :func:`rewrite_issue_references` — the incoming-side files whose
            references should move. ``None`` spans the whole store.

    Returns:
        The number of individual references rewritten across the scoped files.
    """
    norm_map = {}
    for old, new in id_map.items():
        old_val, new_val = _norm_id(old), _norm_id(new)
        if old_val != new_val:
            norm_map[old_val] = f"#{new_val:03d}"
    if not norm_map:
        return 0

    issues_dir = runtime_dir(project_root) / "issues"
    if scope_files is None:
        paths = list(_issue_files(issues_dir))
    else:
        paths = [
            p for p in scope_files
            if p.exists() and issues_dir in p.parents
        ]

    total = 0
    for path in paths:
        text = path.read_text(encoding="utf-8")
        file_hits = 0

        def _sub(match: "re.Match[str]") -> str:
            nonlocal file_hits
            replacement = norm_map.get(int(match.group(1)))
            if replacement is not None:
                file_hits += 1
                return replacement
            return match.group(0)

        rewritten = _REF_TOKEN.sub(_sub, text)
        if file_hits:
            path.write_text(rewritten, encoding="utf-8")
            total += file_hits

    if total:
        logger.info(
            "Bulk-rewrote %d issue reference(s) across %d pair(s)",
            total, len(norm_map),
        )
    return total


def _string_lines(node: object):
    """Yield every logical line of every string value inside *node*."""
    if isinstance(node, str):
        yield from node.splitlines()
    elif isinstance(node, dict):
        for value in node.values():
            yield from _string_lines(value)
    elif isinstance(node, (list, tuple)):
        for value in node:
            yield from _string_lines(value)


def count_reference_tokens(text: str, target_id: object) -> int:
    """Count standalone ``#<digits>`` tokens in *text* equal to *target_id*.

    Same token-boundary and zero-padding rules as the rewriters, so a caller
    deciding whether a file still holds a ``#<old>`` reference (e.g. one left
    deliberately un-rewritten because its target is ambiguous) sees exactly
    the tokens a rewrite pass would have seen.
    """
    if not text:
        return 0
    target = _norm_id(target_id)
    return sum(
        1 for m in _REF_TOKEN.finditer(text) if int(m.group(1)) == target
    )


def live_reference_count(path: Path, target_id: object) -> int:
    """Count LIVE ``#<target_id>`` references in the issue file at *path*.

    "Live" excludes trace / ambiguity-note audit lines: they embed historical
    ``#<old>`` tokens that are records, not references, and must not make a
    file look like it still points at the old number. Those lines sit inside
    the YAML ``description``, whose on-disk dump escapes newlines — so the
    stripping has to run on the PARSED string values' logical lines, not the
    raw file text. Content that does not parse to a mapping falls back to
    counting over the raw text (no audit lines can exist in it anyway, since
    only issue mappings ever receive them).
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        data = None
    if isinstance(data, dict):
        text = strip_renumber_traces("\n".join(_string_lines(data)))
    return count_reference_tokens(text, target_id)


def rewrite_references_in_added_lines(
    path: Path,
    baseline_text: str,
    old_id: object,
    new_id: object,
    dry_run: bool = False,
) -> int:
    """Rewrite ``#<old_id>`` → ``#<new_id>`` only in lines absent from *baseline_text*.

    The committed-merge channel needs this for files that already existed
    before the merge but whose content the merged branch changed: a ``#<old>``
    the branch ADDED meant the branch's own (now renumbered) issue and must
    follow the renumber, while a ``#<old>`` on a line that already existed
    still names the issue that KEPT the number and must stay. The two are told
    apart by line membership against the pre-merge blob. Membership is
    counted per OCCURRENCE, not per distinct text: each current line consumes
    one matching baseline occurrence, so a merge-added duplicate of a line
    that already existed once still counts as added and follows the renumber.
    Within that occurrence budget a matching line is treated as pre-existing —
    the safe direction, since a wrong rewrite silently corrupts a kept-side
    reference while a missed one merely leaves the branch's own wording
    pointing at the kept issue.

    The comparison runs on the *logical* lines of the parsed YAML's string
    values, not the file's physical lines: PyYAML folds multi-line strings
    into quoted scalars, so merely appending a description line moves the
    closing quote and makes the (semantically untouched) previous physical
    line look changed — physical-line membership would then rewrite a
    kept-side reference. Only when either side does not parse to a mapping
    does the check fall back to physical-line membership on the raw text.

    Args:
        path: The on-disk file to rewrite (caller confines it to
            ``tianluo/issues/``).
        baseline_text: The file's full content at the pre-merge commit.
        old_id: The ID being retired (zero-padded string or int).
        new_id: The replacement ID (zero-padded string or int).
        dry_run: When True, only COUNT the merge-added ``#<old_id>`` tokens
            without writing — used to detect references whose renumber target
            is ambiguous and must be recorded instead of rewritten.

    Returns:
        The number of individual references rewritten (or, under *dry_run*,
        that would have been rewritten).
    """
    old_val = _norm_id(old_id)
    new_ref = f"#{_norm_id(new_id):03d}"

    total = 0

    def _sub(match: "re.Match[str]") -> str:
        nonlocal total
        if int(match.group(1)) == old_val:
            total += 1
            return new_ref
        return match.group(0)

    def _rewrite_line(line: str, baseline_budget: "Counter[str]") -> str:
        # Consume one baseline occurrence per matching current line: the
        # budget (not bare set membership) is what lets a merge-ADDED
        # duplicate of an already-present line still be rewritten.
        if baseline_budget[line] > 0:
            baseline_budget[line] -= 1
            return line
        return _REF_TOKEN.sub(_sub, line)

    text = path.read_text(encoding="utf-8")
    try:
        baseline_data = yaml.safe_load(baseline_text)
        current_data = yaml.safe_load(text)
    except yaml.YAMLError:
        baseline_data = current_data = None

    if isinstance(baseline_data, dict) and isinstance(current_data, dict):
        baseline_budget = Counter(_string_lines(baseline_data))

        def _rewrite_node(node: object) -> object:
            if isinstance(node, str):
                rewritten = "\n".join(
                    _rewrite_line(line, baseline_budget)
                    for line in node.splitlines()
                )
                # splitlines drops a trailing newline; put it back so a
                # no-hit round-trip stays byte-identical.
                if node.endswith("\n"):
                    rewritten += "\n"
                return rewritten
            if isinstance(node, dict):
                return {k: _rewrite_node(v) for k, v in node.items()}
            if isinstance(node, list):
                return [_rewrite_node(v) for v in node]
            return node

        rewritten_data = _rewrite_node(current_data)
        if total and not dry_run:
            path.write_text(
                yaml.dump(
                    rewritten_data,
                    default_flow_style=False,
                    allow_unicode=True,
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
    else:
        # Raw fallback for content that is not an issue mapping: physical-line
        # membership is the best available signal there.
        baseline_budget = Counter(baseline_text.splitlines())
        out: list[str] = []
        for raw in text.splitlines(keepends=True):
            line = raw.rstrip("\r\n")
            # Re-attach the line ending stripped for the membership check.
            out.append(_rewrite_line(line, baseline_budget) + raw[len(line):])
        if total and not dry_run:
            path.write_text("".join(out), encoding="utf-8")

    if total and not dry_run:
        logger.info(
            "Rewrote %d merge-added issue reference(s) #%03d -> %s in %s",
            total, old_val, new_ref, path.name,
        )
    return total


def scan_max_issue_id(issues_dir: Path) -> int:
    """Return the highest numeric issue ID currently present in the store.

    Both the filename prefix AND the parsed ``id`` field inside each YAML
    count toward the max: the two can disagree (a hand-edited or corrupted
    file like ``005_x.yaml`` carrying ``id: '100'``), and an allocator that
    only saw the filename side could later hand out the live parsed ID
    again. Unparseable content contributes nothing beyond its filename.

    This scan takes no lock itself — callers that allocate or write the
    ``.next_id`` counter from the result must hold the counter's
    ``fcntl.flock`` across scan + write so a peer cannot wedge a new file
    in between.
    """
    max_id = 0
    for path in _issue_files(issues_dir):
        match = re.match(r"^(\d+)_", path.name)
        if match:
            num = int(match.group(1))
            if num > max_id:
                max_id = num
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (yaml.YAMLError, OSError):
            continue
        if isinstance(data, dict) and data.get("id") is not None:
            try:
                parsed = _norm_id(data["id"])
            except (TypeError, ValueError):
                continue
            if parsed > max_id:
                max_id = parsed
    return max_id


def resolve_issue_numeric_id(path: Path) -> Optional[int]:
    """Resolve one issue file's numeric identity: parsed ``id`` field first.

    The YAML ``id`` field is the authority; the ``NNN_`` filename prefix is
    only a fallback for a file whose body has no parseable ``id``. This is the
    SAME precedence the git-merge collision channel
    (``MergeOrchestrator._numeric_id_from_body``) and the allocator use, so
    every ID-authority decision — allocation, collision, kept-side ownership —
    agrees even when a file's filename prefix and its YAML ``id`` disagree
    (e.g. ``010_main.yaml`` carrying ``id: '005'``). A file whose body neither
    parses to a mapping with an ``id`` nor has a numeric filename prefix has no
    resolvable identity and yields ``None``.
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        data = None
    if isinstance(data, dict) and data.get("id") is not None:
        try:
            return _norm_id(data["id"])
        except (TypeError, ValueError):
            pass
    match = re.match(r"^(\d+)_", path.name)
    return int(match.group(1)) if match else None


def find_issue_id_owner(
    project_root: Path,
    target_id: object,
    exclude_files: Iterable[Path] = (),
) -> Optional[Path]:
    """Return an issue file whose numeric identity equals *target_id*, if any.

    Ownership is decided by :func:`resolve_issue_numeric_id` (parsed ``id``
    then filename prefix) rather than by filename prefix alone, so a kept-side
    issue whose filename and YAML ``id`` disagree is still recognised as the
    owner of its parsed number. This is what lets the runtime-sync channel
    scope a renumber's reference rewrite correctly: a ``#<old>`` that still
    names a kept parsed-ID owner must NOT be repointed to the adopted issue.
    Files in *exclude_files* (e.g. the just-written adopted file, which now
    carries the new ID) are skipped.
    """
    target = _norm_id(target_id)
    excluded = {p.resolve() for p in exclude_files}
    issues_dir = runtime_dir(project_root) / "issues"
    for path in _issue_files(issues_dir):
        if path.resolve() in excluded:
            continue
        if resolve_issue_numeric_id(path) == target:
            return path
    return None


def reserve_next_id(project_root: Path) -> int:
    """Atomically reserve the next issue ID under the ``.next_id`` fcntl lock.

    This is the single allocation primitive EVERY channel must route through —
    ``IssueManager._next_id`` (CLI / webui / discovery ``create`` and
    runtime-sync ``adopt_issue``) and the git-merge collision-repair channel
    (:meth:`MergeOrchestrator._renumber_committed_issue`). It returns
    ``max(counter, on-disk max + 1)`` and, in the SAME locked section, advances
    the counter to ``reserved + 1``.

    Reserving under the lock — and bumping the counter *before* the reserving
    caller has written its ``N_*.yaml`` file — is what makes concurrent
    allocators safe against each other: none of ``luo issue create`` (CLI /
    webui / discovery) contend on the merge lock, so if the merge channel
    allocated by scanning the working-tree maximum alone it could re-mint a
    number a concurrent creator had just reserved but not yet materialised.
    Consulting AND advancing the counter here closes that window: the reserved
    number is visible to the next reserver the instant the lock is released,
    even though its file does not exist yet.

    Returns:
        The reserved integer ID (the counter is left at ``reserved + 1``).
    """
    issues_dir = runtime_dir(project_root) / "issues"
    issues_dir.mkdir(parents=True, exist_ok=True)
    counter_file = issues_dir / ".next_id"

    # ``a+`` creates the counter if absent; the exclusive lock serializes the
    # whole scan-reserve-write against every other allocator.
    with open(counter_file, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            fh.seek(0)
            raw = fh.read().strip()
            counter_val = 0
            if raw:
                try:
                    counter_val = int(raw)
                except ValueError:
                    counter_val = 0

            # Scan under the lock so a concurrent allocation cannot slip a new
            # file (and its counter bump) in between the scan and the write.
            reserved = max(counter_val, scan_max_issue_id(issues_dir) + 1)

            fh.seek(0)
            fh.truncate()
            fh.write(str(reserved + 1))
            fh.flush()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)

    return reserved


def advance_next_id_to_max(project_root: Path) -> int:
    """Advance ``tianluo/issues/.next_id`` to at least ``max(existing ID) + 1``.

    Scans every issue under ``open/`` and ``closed/`` for the highest numeric
    ID and pushes the counter forward to ``max_id + 1`` so a future allocation
    cannot re-mint a number a renumbered file now owns. The sync is
    MONOTONIC — it only ever moves the counter UP, never down. An *ahead*
    counter is left as-is: it may be a peer's live reservation
    (``IssueManager._next_id`` reserves ``#N`` by writing ``N+1`` to the
    counter *before* the ``N_*.yaml`` file exists), so pulling the counter
    back to the on-disk max+1 would let a third allocator re-mint that
    reserved number — a hard-guarantee violation. Skipping numbers (from
    deleted issues or a hand-set-high counter) is harmless; reusing one is
    not. This matches ``_next_id``'s ``max(counter, max_id + 1)`` rule so
    every allocator agrees. Callers must invoke this only AFTER every
    renumbered file is on disk; within ``luo merge`` that ordering is given
    (renumbered files are written first) and the merge lock serializes the
    channels against each other.

    Both the filename prefix AND the parsed ``id`` field inside each YAML
    count toward the max: the two can disagree (a hand-edited or corrupted
    file like ``005_x.yaml`` carrying ``id: '100'``), and an allocator that
    only saw the filename side could later hand out the live parsed ID again.
    The entire read-scan-compute-write is done *while holding*
    ``fcntl.flock(LOCK_EX)`` — the same lock discipline ``_next_id`` uses —
    so a concurrent allocator can never wedge a stale target between the scan
    and the write. A missing or garbage counter contributes nothing and is
    rebuilt from the file-derived ``max_id + 1``.

    Returns:
        The counter value now in effect (``max(current, max_id + 1)``).
    """
    issues_dir = runtime_dir(project_root) / "issues"
    issues_dir.mkdir(parents=True, exist_ok=True)
    counter_file = issues_dir / ".next_id"

    # ``a+`` creates the counter if absent; the exclusive lock serializes the
    # whole scan-write against IssueManager._next_id and any peer renumber.
    with open(counter_file, "a+") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            # Scan under the lock so a concurrent allocation cannot slip a new
            # file (and its counter bump) in between the scan and the write.
            max_id = scan_max_issue_id(issues_dir)

            fh.seek(0)
            try:
                current = int(fh.read().strip())
            except ValueError:
                current = 0
            # Monotonic: never lower the counter. An ahead value is a live
            # reservation (a reserved ID whose file is not yet written), so we
            # keep it; we only push a lagging counter up past the new max.
            target = max(current, max_id + 1)

            if target != current:
                fh.seek(0)
                fh.truncate()
                fh.write(str(target))
                fh.flush()
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)

    logger.info("Advanced .next_id to %d", target)
    return target


def format_renumber_trace(old_id: object, new_id: object) -> str:
    """Render the traceable "old → new" audit line for a renumbered issue.

    The text is appended to the tail of the renumbered issue's description.
    It goes at the *end* so it never shifts the first non-empty line, leaving
    ``Issue.display_title`` / slug derivation unchanged.

    Returns:
        A line of the form ``旧号 #014 → 新号 #240 (luo merge)``.
    """
    return f"旧号 #{_norm_id(old_id):03d} → 新号 #{_norm_id(new_id):03d} (luo merge)"


# Matches one full audit line produced by :func:`format_renumber_trace`. Kept
# next to the formatter so the two never drift: whoever changes the trace text
# updates the pattern that strips/parses it back out.
_TRACE_LINE_RE = re.compile(r"^\s*旧号 #(\d+) → 新号 #(\d+) \(luo merge\)\s*$")


def format_ambiguous_reference_note(old_id: object, new_ids: Iterable) -> str:
    """Render the durable note recording an UNRESOLVABLE ``#<old>`` reference.

    When two or more renumbered issues shared one old ID, a third file's
    ``#<old>`` reference has no single provable target: rewriting it to any
    one candidate would silently corrupt the cross-reference, and leaving it
    bare would silently repoint it at whichever issue still owns (or later
    takes) the old number. Both channels therefore leave the token in place
    and append this note next to it, so the ambiguity is recorded in the
    issue itself rather than only in a transient log line.

    Returns:
        A line of the form ``歧义引用 #005 → 候选 #011 / #012 (luo merge)``.
    """
    candidates = " / ".join(f"#{_norm_id(n):03d}" for n in new_ids)
    return f"歧义引用 #{_norm_id(old_id):03d} → 候选 {candidates} (luo merge)"


# Companion pattern to _TRACE_LINE_RE, for the ambiguity notes: they must be
# stripped by strip_renumber_traces for the same re-run-idempotency reason
# (the adopted copy carries the note, its worktree source does not).
_AMBIGUOUS_NOTE_RE = re.compile(
    r"^\s*歧义引用 #(\d+) → 候选 #\d+(?: / #\d+)* \(luo merge\)\s*$"
)


def append_description_note(path: Path, note: str) -> None:
    """Append *note* as a trailing line of the issue file's description.

    Appended at the tail so it never shifts the first non-empty description
    line (which ``display_title`` / slug derive from). A file that does not
    parse to an issue mapping is left untouched — there is no description to
    annotate, and clobbering unknown content would be worse than the missing
    note (the ambiguity is still surfaced via the caller's warning log).
    """
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, OSError):
        return
    if not isinstance(data, dict):
        return
    desc = str(data.get("description", "") or "")
    data["description"] = (
        desc.rstrip() + "\n\n" + note if desc.strip() else note
    )
    path.write_text(
        yaml.dump(
            data, default_flow_style=False, allow_unicode=True, sort_keys=False,
        ),
        encoding="utf-8",
    )


def strip_renumber_traces(description: str) -> str:
    """Return *description* with every merge-machinery audit line removed.

    The runtime-sync channel dedups worktree issues against the main project by
    content signature. Once an issue has been renumbered, its main-project copy
    carries a :func:`format_renumber_trace` line that the un-renumbered worktree
    source lacks — so a naive signature would no longer match on a re-run and
    the merge would wrongly re-adopt an already-merged issue. Stripping the
    trace before signing keeps the dedup (and thus idempotency) intact. The
    :func:`format_ambiguous_reference_note` lines are stripped for the same
    reason: only the adopted copy carries them.
    """
    if not description:
        return description
    kept = [
        line for line in description.splitlines()
        if not _TRACE_LINE_RE.match(line)
        and not _AMBIGUOUS_NOTE_RE.match(line)
    ]
    return "\n".join(kept).rstrip()


def parse_renumber_traces(description: str) -> list:
    """Extract every recorded (old, new) renumber pair from *description*.

    The runtime-sync dedup must recognise an adopted copy whose live
    references were rewritten (``see #001`` → ``see #004``) as the same issue
    as its un-renumbered worktree source — but ONLY when the digit change is
    an actual recorded renumber, otherwise two genuinely different issues
    that differ solely by referenced number would collapse together and one
    would be lost. The trace lines the adopters append are the record of
    which rewrites really happened, so dedup reads them back through this
    parser instead of blanket-masking every reference.

    Returns:
        ``[(old_int, new_int), ...]`` for each trace line found.
    """
    if not description:
        return []
    pairs = []
    for line in description.splitlines():
        match = _TRACE_LINE_RE.match(line)
        if match:
            pairs.append((int(match.group(1)), int(match.group(2))))
    return pairs


def mask_issue_references(text: str) -> str:
    """Replace every standalone ``#<digits>`` token with a fixed placeholder.

    A renumber rewrites *live* references inside the adopted main-project
    copy (``see #001`` becomes ``see #004``) while the un-renumbered worktree
    source keeps the original digits, so a raw-text signature stops matching
    after the first merge. Masking gives a rewrite-invariant key — but only a
    COARSE one: two issues whose text differs solely by referenced number
    mask identically, and treating that as equality would lose one of them.
    Dedup therefore uses the masked form only to shortlist candidates, then
    confirms each digit difference against the recorded renumber pairs
    (:func:`parse_renumber_traces`) before calling two issues the same.
    """
    if not text:
        return text
    return _REF_TOKEN.sub("#REF", text)
