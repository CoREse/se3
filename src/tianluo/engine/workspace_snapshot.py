"""Workspace snapshot / comparison for the net-zero-diff contract.

The ``investigate`` step is allowed to make experimental edits (temporary
logging, a probe patch, a scratch script) but must leave the workspace exactly
as it found it. This module supplies the two pure primitives that make that
contract checkable:

- :func:`snapshot_workspace` — capture the working tree's *dirty* state as data:
  a hash of ``git diff HEAD`` (all staged + unstaged tracked modifications) plus
  a per-path hash of that same diff, plus the untracked, non-ignored files'
  relative paths with a per-file content hash, plus HEAD's commit id.
  WHY the tracked side is kept per path and not only in aggregate: the delta is
  handed to an LLM that is asked to undo its own experimental edits, and that
  call carries no memory of the investigation. A file-less "something tracked
  changed" leaves it guessing against a tree that also holds unrelated
  uncommitted work — the exact work this module exists to protect. Naming the
  paths turns the revert instruction (and the human-facing failure message)
  into something actionable. The collection form mirrors
  ``engine/test_baseline.py``'s ``_working_tree_dirty_hash`` /
  ``_git_untracked_files``, which is already proven in production.
  WHY HEAD is part of the snapshot: the diff is taken *against* HEAD, so a change
  that gets committed inside the step disappears from it — an empty diff before
  and an empty diff after a ``git commit`` look identical. Recording HEAD makes
  the "no git commit" rule enforceable by the engine instead of only by prompt
  text.
- :func:`compare_snapshots` — a *delta* between two snapshots, so a workspace
  that was already dirty when the step began cannot be mistaken for something
  the step did. Only what changed BETWEEN the two captures counts.

INVARIANT: this module NEVER performs a write of any kind — no ``git reset``, no
``git checkout``, no ``git stash``, no file writes. A flow's working tree
routinely carries uncommitted work that predates the step (fix iterations in
particular), so any automated restoration here would be irreversible data loss.
Reverting experimental changes is the job of whoever made them (the LLM, told to
do so by the handler); this module only observes and reports.

INVARIANT: the project's runtime directory (``tianluo/`` — or legacy ``se3/``)
is excluded from every capture. The engine writes into it *while the observed
step runs*: the conversation log under ``history/<flow_id>/<step_id>.jsonl``, the
call records, the flow state, the logs. In a project whose ``.gitignore`` does
not cover that directory (``luo init`` only ensures ``tianluo.local.yaml`` and
``tianluo/uploads/`` on a pre-existing gitignore), those engine-authored files
would otherwise show up as changes the *step* made — an investigation that
touched nothing at all could never pass, and the revert instruction derived from
the delta would order the agent to delete the flow's own conversation record.
The guard exists to judge the investigation's experimental instruments, so it
looks only at the project tree the investigation actually works on.
"""

from __future__ import annotations

import hashlib
import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..runtime_paths import runtime_dir_name

logger = logging.getLogger(__name__)


@dataclass
class WorkspaceSnapshot:
    """A point-in-time view of the working tree's uncommitted state.

    ``tracked_diff_hash`` is a hash of ``git diff HEAD``'s raw bytes, so a single
    changed byte in any tracked file flips it. ``tracked`` maps each dirty
    tracked path to a hash of *its* section of that diff, which is what lets the
    delta name files; ``tracked_paths_available`` is False when the per-path
    split could not be aligned with git's path list, in which case only the
    aggregate hash is trustworthy and the delta degrades to a file-less report
    (never to a missed change). ``untracked`` maps each untracked, non-ignored
    relative path to its content hash. ``head_commit`` is HEAD's resolved commit
    id, which anchors that diff — without it a commit made during the observed
    window would move the baseline and hide itself. ``available`` is False when
    git could not be consulted at all — the caller must then treat the comparison
    as *undecidable* rather than as either clean or dirty.
    """

    tracked_diff_hash: str = ""
    tracked: Dict[str, str] = field(default_factory=dict)
    tracked_paths_available: bool = True
    untracked: Dict[str, str] = field(default_factory=dict)
    head_commit: str = ""
    available: bool = True
    unavailable_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """Serialize for persistence in a step's inputs.

        WHY a snapshot has to survive persistence: the pre-step baseline must
        outlive a single handler invocation, so that a FAILED step which is
        retried keeps comparing against the tree as it was before the
        investigation ever started (see ``steps/investigate.py``).
        """
        return {
            "tracked_diff_hash": self.tracked_diff_hash,
            "tracked": dict(self.tracked),
            "tracked_paths_available": self.tracked_paths_available,
            "untracked": dict(self.untracked),
            "head_commit": self.head_commit,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
        }

    @classmethod
    def from_dict(cls, data: Any) -> Optional["WorkspaceSnapshot"]:
        """Rebuild a snapshot from :meth:`to_dict`, or None if unusable.

        Returns None rather than raising on malformed data: a caller that cannot
        read back its baseline must fall back to taking a fresh one, not crash.
        """
        if not isinstance(data, dict):
            return None
        tracked = data.get("tracked")
        untracked = data.get("untracked")
        if not isinstance(tracked, dict) or not isinstance(untracked, dict):
            return None
        return cls(
            tracked_diff_hash=str(data.get("tracked_diff_hash", "")),
            tracked={str(k): str(v) for k, v in tracked.items()},
            tracked_paths_available=bool(data.get("tracked_paths_available", True)),
            untracked={str(k): str(v) for k, v in untracked.items()},
            head_commit=str(data.get("head_commit", "")),
            available=bool(data.get("available", True)),
            unavailable_reason=str(data.get("unavailable_reason", "")),
        )


@dataclass
class WorkspaceDelta:
    """What changed between two :class:`WorkspaceSnapshot` captures."""

    tracked_changed: bool = False
    # Per-path breakdown of the tracked side. Empty (with ``tracked_changed``
    # still True) when the per-path split was unavailable — see
    # ``WorkspaceSnapshot.tracked_paths_available``.
    added_tracked: List[str] = field(default_factory=list)
    removed_tracked: List[str] = field(default_factory=list)
    modified_tracked: List[str] = field(default_factory=list)
    head_changed: bool = False
    head_before: str = ""
    head_after: str = ""
    added_untracked: List[str] = field(default_factory=list)
    removed_untracked: List[str] = field(default_factory=list)
    modified_untracked: List[str] = field(default_factory=list)
    # True when either snapshot could not be taken. The delta then carries no
    # findings and ``is_clean`` reports True: an undecidable check must not be
    # allowed to fail a step (see the module docstring's degrade contract).
    undecidable: bool = False
    undecidable_reason: str = ""

    @property
    def tracked_paths(self) -> List[str]:
        """Every tracked path this delta can name, deduplicated and sorted."""
        return sorted(
            set(self.added_tracked)
            | set(self.removed_tracked)
            | set(self.modified_tracked)
        )

    @property
    def changed_paths(self) -> List[str]:
        """Every path — tracked or not — this delta can name."""
        return sorted(
            set(self.tracked_paths)
            | set(self.added_untracked)
            | set(self.removed_untracked)
            | set(self.modified_untracked)
        )

    @property
    def is_clean(self) -> bool:
        """True when the two snapshots are indistinguishable (or undecidable)."""
        if self.undecidable:
            return True
        return not (
            self.tracked_changed
            or self.head_changed
            or self.added_untracked
            or self.removed_untracked
            or self.modified_untracked
        )

    def describe(self) -> str:
        """Human/LLM-readable rendering of the delta.

        Used verbatim in the revert instruction handed back to the LLM and in the
        step's ``error_message``, so it names *what* is off without prescribing a
        git command (the engine must not push the author toward a destructive
        ``checkout``).
        """
        if self.undecidable:
            return (
                "Workspace comparison unavailable "
                f"({self.undecidable_reason or 'git not available'})."
            )
        if self.is_clean:
            return "Workspace is unchanged relative to the start of the step."

        parts: List[str] = []
        if self.head_changed:
            parts.append(
                "- Commits: HEAD moved during this step (from "
                f"{self.head_before[:12] or '<unknown>'} to "
                f"{self.head_after[:12] or '<unknown>'}). This step must create "
                "no commits at all; the commit(s) made here have to be undone "
                "while keeping every other file exactly as it is."
            )
        if self.added_tracked:
            parts.append(
                "- Tracked files edited during this step (they matched HEAD "
                "before it started, so ALL of their current changes are yours "
                "and must go): " + ", ".join(self.added_tracked)
            )
        if self.modified_tracked:
            parts.append(
                "- Tracked files that ALREADY had uncommitted changes before "
                "this step and whose changes are now different — undo only your "
                "own edits in them, keep the rest: "
                + ", ".join(self.modified_tracked)
            )
        if self.removed_tracked:
            parts.append(
                "- Tracked files that had uncommitted changes before this step "
                "and now match HEAD — pre-existing work was wiped out and has to "
                "be put back exactly as it was: " + ", ".join(self.removed_tracked)
            )
        if self.tracked_changed and not self.tracked_paths:
            # Per-path detail unavailable: say so plainly rather than implying
            # the change is file-less.
            parts.append(
                "- Tracked files: the diff against HEAD differs from what it was "
                "when this step started (something tracked was edited, staged, "
                "or reverted incorrectly). The affected paths could not be "
                "determined — inspect `git diff HEAD` and undo only the edits "
                "you made in this step."
            )
        if self.added_untracked:
            parts.append(
                "- New untracked files left behind: "
                + ", ".join(self.added_untracked)
            )
        if self.modified_untracked:
            parts.append(
                "- Pre-existing untracked files whose content changed: "
                + ", ".join(self.modified_untracked)
            )
        if self.removed_untracked:
            parts.append(
                "- Untracked files that existed before this step and are now "
                "gone: " + ", ".join(self.removed_untracked)
            )
        return "\n".join(parts)


def _run_git(project_root: Path, args: List[str]) -> Optional[bytes]:
    """Run a read-only git command, returning stdout bytes or None on failure.

    INVARIANT: callers of this helper pass READ-ONLY git subcommands only. It
    exists to centralize the "git may be missing / this may not be a repo"
    degradation, not to give the module a general git channel.
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(project_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
    except (FileNotFoundError, OSError) as exc:
        logger.debug("git %s failed: %s", " ".join(args), exc)
        return None
    if proc.returncode != 0:
        logger.debug("git %s exited %d", " ".join(args), proc.returncode)
        return None
    return proc.stdout or b""


def _runtime_pathspec(project_root: Path) -> List[str]:
    """Pathspec args restricting a git query to everything but the runtime dir.

    Returned as ``["--", ".", ":(exclude)<runtime>"]``. The positive ``.`` is
    mandatory: git matches *nothing* when a pathspec carries only exclusions, so
    ``-- ':(exclude)tianluo'`` alone would silently report an empty diff and an
    empty untracked set — a guard that always passes. ``.`` also keeps each
    command's existing scope (the engine runs git with cwd = project root).

    The name is resolved per call rather than hard-coded so a project still on
    the legacy ``se3/`` layout excludes *its* runtime directory. An unmatched
    exclude pathspec is not an error for either ``git diff`` or ``git ls-files``,
    so a project without the directory yet behaves as before.
    """
    return ["--", ".", f":(exclude){runtime_dir_name(project_root)}"]


def _split_diff_per_file(diff: bytes) -> List[bytes]:
    """Split ``git diff`` output into one chunk per file header.

    Body lines are prefixed (``+``/``-``/space), so a diff of a file that itself
    contains ``diff --git`` cannot be mistaken for a header.

    WHY trailing newlines are trimmed off each chunk: the last chunk inherits the
    diff's final line break while the others do not, so an unchanged file's chunk
    would hash differently the moment another file joined the diff behind it —
    and every pre-existing dirty file would be blamed on the step.
    """
    sections: List[bytes] = []
    current: List[bytes] = []
    for line in diff.split(b"\n"):
        if line.startswith(b"diff --git "):
            if current:
                sections.append(b"\n".join(current).rstrip(b"\n"))
            current = [line]
        elif current:
            current.append(line)
    if current:
        sections.append(b"\n".join(current).rstrip(b"\n"))
    return sections


def _tracked_diff_hashes(diff: bytes, names: Optional[bytes]) -> Optional[Dict[str, str]]:
    """Map each dirty tracked path to a hash of its own diff chunk.

    ``names`` is ``git diff HEAD --name-only -z`` output, which lists paths in
    the same order as the diff chunks (a rename contributes one entry on both
    sides). Returns None when the two cannot be aligned — the caller then keeps
    only the aggregate hash, so an unexpected git output shape costs detail but
    never correctness.
    """
    if names is None:
        return None
    paths = [
        raw.decode("utf-8", "surrogateescape")
        for raw in names.split(b"\0")
        if raw
    ]
    sections = _split_diff_per_file(diff)
    if len(sections) != len(paths):
        logger.debug(
            "tracked diff/name alignment mismatch (%d chunks vs %d paths)",
            len(sections), len(paths),
        )
        return None
    return {
        path: hashlib.sha256(section).hexdigest()
        for path, section in zip(paths, sections)
    }


def _file_content_hash(path: Path) -> str:
    """SHA-256 of *path*'s bytes, or a sentinel when it cannot be read."""
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "unreadable"


def snapshot_workspace(project_root: Path) -> WorkspaceSnapshot:
    """Capture the uncommitted state of the working tree at *project_root*.

    Never raises: when git is unavailable or the directory is not a repository,
    the returned snapshot has ``available=False`` and :func:`compare_snapshots`
    downgrades the comparison to *undecidable* rather than reporting a phantom
    change.
    """
    root = Path(project_root)

    head = _run_git(root, ["rev-parse", "HEAD"])
    if head is None:
        return WorkspaceSnapshot(
            available=False,
            unavailable_reason=f"`git rev-parse HEAD` unavailable in {root}",
        )

    # Every query below carries the same pathspec, so the tracked diff, its
    # per-path name list and the untracked scan all agree on what is in scope.
    pathspec = _runtime_pathspec(root)

    diff = _run_git(root, ["diff", "HEAD", *pathspec])
    if diff is None:
        # `git diff HEAD` also fails on a repo with no commits yet; either way we
        # cannot establish a tracked-side baseline, so decline to judge.
        return WorkspaceSnapshot(
            available=False,
            unavailable_reason=f"`git diff HEAD` unavailable in {root}",
        )

    tracked = _tracked_diff_hashes(
        diff, _run_git(root, ["diff", "HEAD", "--name-only", "-z", *pathspec])
    )

    untracked: Dict[str, str] = {}
    others = _run_git(
        root, ["ls-files", "--others", "--exclude-standard", "-z", *pathspec]
    )
    if others is None:
        return WorkspaceSnapshot(
            available=False,
            unavailable_reason=f"`git ls-files --others` unavailable in {root}",
        )
    for raw in others.split(b"\0"):
        if not raw:
            continue
        rel = raw.decode("utf-8", "surrogateescape")
        untracked[rel] = _file_content_hash(root / rel)

    return WorkspaceSnapshot(
        tracked_diff_hash=hashlib.sha256(diff).hexdigest(),
        tracked=tracked or {},
        tracked_paths_available=tracked is not None,
        untracked=untracked,
        head_commit=head.decode("utf-8", "replace").strip(),
        available=True,
    )


def compare_snapshots(
    before: WorkspaceSnapshot, after: WorkspaceSnapshot
) -> WorkspaceDelta:
    """Diff two snapshots into a :class:`WorkspaceDelta`.

    The comparison is deliberately *relative*: a working tree that was already
    dirty at ``before`` contributes nothing, because both snapshots see the same
    pre-existing modifications. Only changes introduced (or wrongly reverted)
    between the two captures show up.
    """
    if not before.available or not after.available:
        reason = before.unavailable_reason or after.unavailable_reason
        return WorkspaceDelta(undecidable=True, undecidable_reason=reason)

    before_paths = set(before.untracked)
    after_paths = set(after.untracked)

    # Per-path detail is only reported when BOTH captures have it; a one-sided
    # breakdown would attribute the other side's whole dirty set to this step.
    if before.tracked_paths_available and after.tracked_paths_available:
        before_tracked = set(before.tracked)
        after_tracked = set(after.tracked)
        added_tracked = sorted(after_tracked - before_tracked)
        removed_tracked = sorted(before_tracked - after_tracked)
        modified_tracked = sorted(
            p for p in (before_tracked & after_tracked)
            if before.tracked[p] != after.tracked[p]
        )
    else:
        added_tracked = []
        removed_tracked = []
        modified_tracked = []

    return WorkspaceDelta(
        tracked_changed=before.tracked_diff_hash != after.tracked_diff_hash,
        added_tracked=added_tracked,
        removed_tracked=removed_tracked,
        modified_tracked=modified_tracked,
        # A commit made inside the window rebases the diff onto a new HEAD, so
        # the tracked-side hash can be identical while work was in fact
        # committed. HEAD is therefore compared in its own right.
        head_changed=before.head_commit != after.head_commit,
        head_before=before.head_commit,
        head_after=after.head_commit,
        added_untracked=sorted(after_paths - before_paths),
        removed_untracked=sorted(before_paths - after_paths),
        modified_untracked=sorted(
            p for p in (before_paths & after_paths)
            if before.untracked[p] != after.untracked[p]
        ),
    )
