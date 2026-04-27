"""ConflictContextBuilder — Collect three-way merge context for LLM resolution.

Gathers everything an LLM needs to resolve a single ``git merge``'s
conflicts: merge metadata (ours/theirs branch names, merge-base SHA,
HEAD commit SHAs + messages), the four versions of every conflicting
file (base / ours / theirs / working tree with ``<<<<<<<`` markers),
hunk line ranges, recent oneline log between base and each side, and
spec-file identification.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..worktree import _run_git, get_conflicting_files

logger = logging.getLogger(__name__)


_SPEC_PATH_RE = re.compile(r"^se3/specs/.+/spec\.md$")

_DEFAULT_LOG_LIMIT = 20


@dataclass
class ConflictHunk:
    """One conflict hunk delimited by ``<<<<<<<`` ... ``>>>>>>>``."""

    start_line: int
    end_line: int


@dataclass
class ConflictFile:
    """All info about a single conflicting file in a single merge."""

    path: str
    base_content: str = ""
    ours_content: str = ""
    theirs_content: str = ""
    working_content: str = ""
    base_exists: bool = False
    ours_exists: bool = False
    theirs_exists: bool = False
    hunks: list[ConflictHunk] = field(default_factory=list)
    is_spec: bool = False
    is_binary: bool = False


@dataclass
class ConflictContext:
    """Three-way merge context for LLM-driven conflict resolution."""

    project_root: Path
    ours_branch: str
    theirs_branch: str
    merge_base: str = ""
    ours_head_sha: str = ""
    ours_head_message: str = ""
    theirs_head_sha: str = ""
    theirs_head_message: str = ""
    ours_log_oneline: list[str] = field(default_factory=list)
    theirs_log_oneline: list[str] = field(default_factory=list)
    files: list[ConflictFile] = field(default_factory=list)
    has_spec_files: bool = False


def _git_show_bytes(
    project_root: Path,
    ref: str,
    *,
    timeout: int = 30,
) -> tuple[bytes, bool]:
    """Run ``git show <ref>`` and return ``(bytes, exists)``.

    ``ref`` may be ``:N:<path>`` for an index stage or any normal ref.
    Returns ``(b"", False)`` when git reports an error (e.g. the stage
    or path does not exist).
    """
    cmd = ["git", "-C", str(project_root), "show", ref]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("git show timed out for %s", ref)
        return b"", False
    if result.returncode != 0:
        return b"", False
    return result.stdout, True


def _decode_lossy(data: bytes) -> str:
    """Decode bytes as UTF-8, replacing invalid sequences."""
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("utf-8", errors="replace")


def _looks_binary(data: bytes) -> bool:
    """Heuristic: data contains a null byte in its first 8 KiB."""
    return b"\x00" in data[:8192]


def _is_spec_path(path: str) -> bool:
    """Return True when ``path`` matches ``se3/specs/**/spec.md``."""
    normalized = path.replace("\\", "/")
    return bool(_SPEC_PATH_RE.match(normalized))


def _parse_hunks(text: str) -> list[ConflictHunk]:
    """Find ``<<<<<<<`` ... ``>>>>>>>`` blocks and return their line ranges.

    Lines are 1-based. Diff3-style ``|||||||`` markers are tolerated and
    fall inside the hunk range. If a ``<<<<<<<`` has no matching
    ``>>>>>>>`` it is skipped (best-effort, no exception).
    """
    hunks: list[ConflictHunk] = []
    lines = text.splitlines()
    start: int | None = None
    for idx, line in enumerate(lines, start=1):
        if line.startswith("<<<<<<<"):
            start = idx
        elif line.startswith(">>>>>>>") and start is not None:
            hunks.append(ConflictHunk(start_line=start, end_line=idx))
            start = None
    return hunks


def _read_working_tree(project_root: Path, rel_path: str) -> tuple[bytes, bool]:
    """Read the working-tree version of ``rel_path``.

    Returns ``(bytes, exists)``. When the file is absent (e.g. delete
    conflict) returns ``(b"", False)``.
    """
    full = project_root / rel_path
    if not full.exists():
        return b"", False
    try:
        return full.read_bytes(), True
    except OSError as exc:
        logger.warning("Failed to read working tree file %s: %s", rel_path, exc)
        return b"", False


def _resolve_sha(project_root: Path, ref: str) -> str:
    """Resolve ``ref`` to a full commit SHA. Returns "" on failure."""
    result = _run_git(
        project_root, "rev-parse", "--verify", f"{ref}^{{commit}}",
        check=False,
        timeout=15,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _commit_message(project_root: Path, ref: str) -> str:
    """Return the full commit message for ``ref`` (subject + body)."""
    if not ref:
        return ""
    result = _run_git(
        project_root, "log", "-1", "--format=%B", ref,
        check=False,
        timeout=15,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.rstrip()


def _merge_base(project_root: Path, ours: str, theirs: str) -> str:
    """Return the merge-base commit SHA between ``ours`` and ``theirs``."""
    if not ours or not theirs:
        return ""
    result = _run_git(
        project_root, "merge-base", ours, theirs,
        check=False,
        timeout=15,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _oneline_log(
    project_root: Path,
    base: str,
    head: str,
    *,
    limit: int = _DEFAULT_LOG_LIMIT,
) -> list[str]:
    """Return ``git log <base>..<head> --oneline -n <limit>`` as a list."""
    if not base or not head:
        return []
    result = _run_git(
        project_root, "log", f"{base}..{head}", "--oneline",
        f"-n{limit}",
        check=False,
        timeout=15,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return []
    return [line for line in result.stdout.splitlines() if line.strip()]


def _build_conflict_file(project_root: Path, rel_path: str) -> ConflictFile:
    """Collect all data for a single conflicting file."""
    base_bytes, base_exists = _git_show_bytes(project_root, f":1:{rel_path}")
    ours_bytes, ours_exists = _git_show_bytes(project_root, f":2:{rel_path}")
    theirs_bytes, theirs_exists = _git_show_bytes(project_root, f":3:{rel_path}")
    working_bytes, _working_exists = _read_working_tree(project_root, rel_path)

    is_binary = (
        _looks_binary(working_bytes)
        or _looks_binary(base_bytes)
        or _looks_binary(ours_bytes)
        or _looks_binary(theirs_bytes)
    )

    if is_binary:
        cf = ConflictFile(
            path=rel_path,
            base_content="",
            ours_content="",
            theirs_content="",
            working_content="",
            base_exists=base_exists,
            ours_exists=ours_exists,
            theirs_exists=theirs_exists,
            hunks=[],
            is_spec=_is_spec_path(rel_path),
            is_binary=True,
        )
        return cf

    base_content = _decode_lossy(base_bytes) if base_exists else ""
    ours_content = _decode_lossy(ours_bytes) if ours_exists else ""
    theirs_content = _decode_lossy(theirs_bytes) if theirs_exists else ""
    working_content = _decode_lossy(working_bytes)

    hunks = _parse_hunks(working_content) if "<<<<<<<" in working_content else []

    return ConflictFile(
        path=rel_path,
        base_content=base_content,
        ours_content=ours_content,
        theirs_content=theirs_content,
        working_content=working_content,
        base_exists=base_exists,
        ours_exists=ours_exists,
        theirs_exists=theirs_exists,
        hunks=hunks,
        is_spec=_is_spec_path(rel_path),
        is_binary=False,
    )


def build(
    project_root: Path,
    ours: str,
    theirs: str,
    *,
    log_limit: int = _DEFAULT_LOG_LIMIT,
    conflict_files: list[str] | None = None,
) -> ConflictContext:
    """Build a :class:`ConflictContext` for the in-progress merge.

    Must be called while git is mid-merge (i.e. the index has stages
    1/2/3 populated for conflicting files). The function is read-only:
    it does not modify the working tree, index, or refs.

    Args:
        project_root: Repository root to query.
        ours: Ref name for the current side (typically ``"HEAD"`` or the
            current branch name).
        theirs: Ref name for the incoming branch (typically the branch
            being merged or ``"MERGE_HEAD"``).
        log_limit: Max commits to include in each side's oneline log.
        conflict_files: Optional pre-resolved list of conflicting paths.
            Falls back to :func:`get_conflicting_files` when omitted.

    Returns:
        A :class:`ConflictContext` populated with all collected data.
    """
    if conflict_files is None:
        conflict_files = get_conflicting_files(project_root)

    ours_sha = _resolve_sha(project_root, ours)
    theirs_sha = _resolve_sha(project_root, theirs)
    base_sha = _merge_base(project_root, ours_sha or ours, theirs_sha or theirs)

    files: list[ConflictFile] = []
    has_spec = False
    for rel_path in conflict_files:
        cf = _build_conflict_file(project_root, rel_path)
        files.append(cf)
        if cf.is_spec:
            has_spec = True

    return ConflictContext(
        project_root=project_root,
        ours_branch=ours,
        theirs_branch=theirs,
        merge_base=base_sha,
        ours_head_sha=ours_sha,
        ours_head_message=_commit_message(project_root, ours_sha or ours),
        theirs_head_sha=theirs_sha,
        theirs_head_message=_commit_message(project_root, theirs_sha or theirs),
        ours_log_oneline=_oneline_log(
            project_root, base_sha, ours_sha or ours, limit=log_limit,
        ),
        theirs_log_oneline=_oneline_log(
            project_root, base_sha, theirs_sha or theirs, limit=log_limit,
        ),
        files=files,
        has_spec_files=has_spec,
    )
