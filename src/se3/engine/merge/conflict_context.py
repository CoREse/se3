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


# D5: magic-byte signatures for common binary formats that may not
# contain NUL bytes in their first 8 KiB and would otherwise be
# misclassified as text.  Order does not matter; the check is a simple
# prefix match.
_BINARY_MAGIC_BYTES: tuple[bytes, ...] = (
    b"\x89PNG\r\n\x1a\n",   # PNG
    b"\xff\xd8\xff",         # JPEG
    b"GIF87a",               # GIF87a
    b"GIF89a",               # GIF89a
    b"%PDF-",                # PDF
    b"\x1f\x8b",             # gzip
    b"PK\x03\x04",           # zip / docx / xlsx / jar
    b"PK\x05\x06",           # zip (empty)
    b"PK\x07\x08",           # zip (spanned)
    b"\x7fELF",              # ELF executable
    b"MZ",                   # DOS / PE executable
    b"\x42\x4d",             # BMP
    b"RIFF",                 # WAV / WebP / AVI container
    b"OggS",                 # Ogg
    b"ID3",                  # MP3 with ID3 tag
    b"BZh",                  # bzip2
    b"\xfd7zXZ\x00",         # xz
    b"7z\xbc\xaf\x27\x1c",   # 7z
    b"\xca\xfe\xba\xbe",     # Java class file / Mach-O fat binary
    b"\xfe\xed\xfa\xce",     # Mach-O 32 (BE)
    b"\xfe\xed\xfa\xcf",     # Mach-O 64 (BE)
    b"\xce\xfa\xed\xfe",     # Mach-O 32 (LE)
    b"\xcf\xfa\xed\xfe",     # Mach-O 64 (LE)
    b"SQLite format 3\x00",  # SQLite
)

# D8: BOMs we recognise so we can either decode the file with the right
# codec (UTF-16 / UTF-32) or strip the UTF-8 BOM.
_BOM_UTF8 = b"\xef\xbb\xbf"
_BOM_UTF16_LE = b"\xff\xfe"
_BOM_UTF16_BE = b"\xfe\xff"
_BOM_UTF32_LE = b"\xff\xfe\x00\x00"
_BOM_UTF32_BE = b"\x00\x00\xfe\xff"


class ShaResolutionError(RuntimeError):
    """Raised when ``git rev-parse`` cannot resolve a ref to a commit SHA.

    D4: callers (especially :func:`_merge_base`) used to receive an
    empty string and silently continue; we now raise so the merge halts
    visibly and the orchestrator can produce a typed failure report.
    """


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
    # D6/D8: track lossy / non-UTF-8 decoding so consumers can warn the
    # user and route to human review instead of silently corrupting
    # bytes back to disk.
    decoding_lossy: bool = False
    decoding_encoding: str = "utf-8"



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


def _decode_text(
    data: bytes,
    *,
    rel_path: str = "",
) -> tuple[str, str, bool]:
    """Decode ``data`` as text, honouring BOMs and warning on lossy fallback.

    Returns ``(text, encoding, lossy)`` where ``encoding`` names the
    codec applied (``utf-8``, ``utf-8-sig``, ``utf-16``, ``utf-32``)
    and ``lossy`` is ``True`` when invalid bytes had to be replaced.

    Detection order (D8):
      1. UTF-32 BOM (LE/BE)
      2. UTF-16 BOM (LE/BE)
      3. UTF-8 BOM (strip and decode UTF-8)
      4. Plain UTF-8
      5. Plain UTF-8 with ``errors="replace"`` (D6: log warning).
    """
    if data.startswith(_BOM_UTF32_LE) or data.startswith(_BOM_UTF32_BE):
        try:
            return data.decode("utf-32"), "utf-32", False
        except UnicodeDecodeError:
            logger.warning(
                "UTF-32 BOM present but decode failed for %s; "
                "falling back to UTF-8 with replacement",
                rel_path or "<unknown>",
            )

    if data.startswith(_BOM_UTF16_LE) or data.startswith(_BOM_UTF16_BE):
        try:
            return data.decode("utf-16"), "utf-16", False
        except UnicodeDecodeError:
            logger.warning(
                "UTF-16 BOM present but decode failed for %s; "
                "falling back to UTF-8 with replacement",
                rel_path or "<unknown>",
            )

    if data.startswith(_BOM_UTF8):
        try:
            return data.decode("utf-8-sig"), "utf-8-sig", False
        except UnicodeDecodeError:
            pass

    try:
        return data.decode("utf-8"), "utf-8", False
    except UnicodeDecodeError as exc:
        # D6: invalid UTF-8 bytes are about to be silently replaced —
        # warn loudly so the operator knows the merge product may not
        # round-trip the original file.  Mark the file as lossy so
        # downstream code can flag it for human review.
        logger.warning(
            "UTF-8 decode failed for %s at byte offset %d (%d invalid "
            "bytes around start=%d); using lossy fallback",
            rel_path or "<unknown>",
            exc.start,
            len(data) - exc.start,
            exc.start,
        )
        return data.decode("utf-8", errors="replace"), "utf-8", True


def _decode_lossy(data: bytes) -> str:
    """Backwards-compatible decode helper used by callers outside the
    conflict-file builder.

    For internal builder use, prefer :func:`_decode_text` which also
    returns the encoding and lossy flag.
    """
    text, _, _ = _decode_text(data)
    return text


def _looks_binary(data: bytes) -> bool:
    """Heuristic: data contains a NUL byte or matches a known binary magic.

    D5: NUL-byte detection alone misses formats like PNG, JPEG, gzip,
    PDF that have well-defined header signatures but may not contain
    NUL bytes inside the first 8 KiB header.  We therefore check both:
    a leading magic prefix (cheap, very specific) and NUL presence
    (catches arbitrary executables and embedded resources).
    """
    if not data:
        return False
    for magic in _BINARY_MAGIC_BYTES:
        if data.startswith(magic):
            return True
    return b"\x00" in data[:8192]


def _read_gitattributes_binary_paths(project_root: Path) -> list[str]:
    """Return relative-path patterns marked as binary in ``.gitattributes``.

    D7: a project-level ``.gitattributes`` may declare binary handling
    explicitly (``*.bin binary``).  We honour that flag in addition to
    the magic-byte heuristic.

    The returned list contains the raw pattern as written in the file
    (e.g. ``*.png`` or ``vendor/lib.so``); callers match relative paths
    against these patterns with :func:`fnmatch.fnmatch`.
    """
    attrs = project_root / ".gitattributes"
    if not attrs.exists():
        return []
    try:
        text = attrs.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("Failed to read .gitattributes: %s", exc)
        return []

    patterns: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Each non-comment line is "<pattern> attr1 attr2 ..."
        parts = line.split()
        if len(parts) < 2:
            continue
        pattern, attrs_tail = parts[0], parts[1:]
        # The keyword "binary" is shorthand for
        # "-text -diff -merge" — when present in any form (with or
        # without a leading sign) we treat the path as binary.
        for attr in attrs_tail:
            if attr in ("binary", "-text", "-diff"):
                patterns.append(pattern)
                break
    return patterns


def _path_matches_binary_pattern(rel_path: str, patterns: list[str]) -> bool:
    """Return True when ``rel_path`` matches any glob in ``patterns``."""
    if not patterns:
        return False
    import fnmatch

    norm = rel_path.replace("\\", "/")
    basename = norm.rsplit("/", 1)[-1]
    for pat in patterns:
        if fnmatch.fnmatch(norm, pat):
            return True
        # ``*.png``-style patterns should also match against the bare
        # basename, even when the file lives in a subdirectory.
        if fnmatch.fnmatch(basename, pat):
            return True
    return False


def _is_spec_path(path: str) -> bool:
    """Return True when ``path`` matches ``se3/specs/**/spec.md``."""
    normalized = path.replace("\\", "/")
    return bool(_SPEC_PATH_RE.match(normalized))


# D3: git tolerates conflict markers that are preceded by up to 7
# spaces (used inside embedded code blocks).  We use a regex when
# scanning for hunks so indented markers are recognised.
_HUNK_START_RE = re.compile(r"^[ ]{0,7}<<<<<<<")
_HUNK_END_RE = re.compile(r"^[ ]{0,7}>>>>>>>")


def _parse_hunks(text: str) -> list[ConflictHunk]:
    """Find ``<<<<<<<`` ... ``>>>>>>>`` blocks and return their line ranges.

    Lines are 1-based. Diff3-style ``|||||||`` markers are tolerated and
    fall inside the hunk range. If a ``<<<<<<<`` has no matching
    ``>>>>>>>`` it is skipped (best-effort, no exception).

    Markers indented with up to 7 spaces are recognised (D3).
    """
    hunks: list[ConflictHunk] = []
    lines = text.splitlines()
    start: int | None = None
    for idx, line in enumerate(lines, start=1):
        if _HUNK_START_RE.match(line):
            start = idx
        elif _HUNK_END_RE.match(line) and start is not None:
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
    """Resolve ``ref`` to a full commit SHA.

    Raises :class:`ShaResolutionError` when the ref cannot be resolved
    (D4).  Previously this returned ``""`` and downstream callers
    silently continued with empty SHAs, which masked merge errors and
    let :func:`_merge_base` be called with empty arguments.
    """
    if not ref or not isinstance(ref, str):
        raise ShaResolutionError(
            f"_resolve_sha called with empty/invalid ref: {ref!r}"
        )
    result = _run_git(
        project_root, "rev-parse", "--verify", f"{ref}^{{commit}}",
        check=False,
        timeout=15,
    )
    if result.returncode != 0:
        raise ShaResolutionError(
            f"Could not resolve ref {ref!r} to a commit SHA: "
            f"git rev-parse exited {result.returncode} "
            f"(stderr={(result.stderr or '').strip()!r})"
        )
    sha = result.stdout.strip()
    if not sha:
        raise ShaResolutionError(
            f"git rev-parse for {ref!r} returned empty output"
        )
    return sha


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
    """Return the merge-base commit SHA between ``ours`` and ``theirs``.

    D4: callers must provide non-empty refs.  Empty arguments now
    raise :class:`ValueError` rather than silently producing an empty
    merge-base, which previously caused the orchestrator to keep
    going with a meaningless context.
    """
    if not ours:
        raise ValueError("_merge_base called with empty 'ours' ref")
    if not theirs:
        raise ValueError("_merge_base called with empty 'theirs' ref")
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


def _build_conflict_file(
    project_root: Path,
    rel_path: str,
    *,
    binary_patterns: list[str] | None = None,
) -> ConflictFile:
    """Collect all data for a single conflicting file.

    ``binary_patterns`` is the parsed ``.gitattributes`` binary list
    (D7).  When provided and ``rel_path`` matches any pattern, the
    file is forced to binary regardless of its byte content.
    """
    base_bytes, base_exists = _git_show_bytes(project_root, f":1:{rel_path}")
    ours_bytes, ours_exists = _git_show_bytes(project_root, f":2:{rel_path}")
    theirs_bytes, theirs_exists = _git_show_bytes(project_root, f":3:{rel_path}")
    working_bytes, _working_exists = _read_working_tree(project_root, rel_path)

    is_binary = (
        _path_matches_binary_pattern(rel_path, binary_patterns or [])
        or _looks_binary(working_bytes)
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

    # D6/D8: decode each version explicitly so we can capture the
    # encoding actually used and surface a single ``decoding_lossy``
    # flag.  Lossiness on any version of the file is enough to flag it.
    base_text, base_enc, base_lossy = (
        _decode_text(base_bytes, rel_path=rel_path) if base_exists else ("", "utf-8", False)
    )
    ours_text, ours_enc, ours_lossy = (
        _decode_text(ours_bytes, rel_path=rel_path) if ours_exists else ("", "utf-8", False)
    )
    theirs_text, theirs_enc, theirs_lossy = (
        _decode_text(theirs_bytes, rel_path=rel_path) if theirs_exists else ("", "utf-8", False)
    )
    working_text, working_enc, working_lossy = _decode_text(
        working_bytes, rel_path=rel_path,
    )

    # Pick the most informative encoding label: prefer working tree's
    # codec when known (it's what the user sees on disk).  Lossy if
    # any version was lossy.
    encoding = working_enc if working_text else (
        ours_enc if ours_text else (theirs_enc if theirs_text else base_enc)
    )
    lossy = bool(working_lossy or ours_lossy or theirs_lossy or base_lossy)

    hunks = _parse_hunks(working_text) if "<<<<<<<" in working_text else []

    return ConflictFile(
        path=rel_path,
        base_content=base_text,
        ours_content=ours_text,
        theirs_content=theirs_text,
        working_content=working_text,
        base_exists=base_exists,
        ours_exists=ours_exists,
        theirs_exists=theirs_exists,
        hunks=hunks,
        is_spec=_is_spec_path(rel_path),
        is_binary=False,
        decoding_lossy=lossy,
        decoding_encoding=encoding,
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

    # D4: _resolve_sha now raises rather than returning "" — the
    # orchestrator's outer try/except catches and converts this into
    # a typed conflict_context_failed report.
    ours_sha = _resolve_sha(project_root, ours)
    theirs_sha = _resolve_sha(project_root, theirs)
    base_sha = _merge_base(project_root, ours_sha, theirs_sha)

    binary_patterns = _read_gitattributes_binary_paths(project_root)

    files: list[ConflictFile] = []
    has_spec = False
    for rel_path in conflict_files:
        cf = _build_conflict_file(
            project_root, rel_path, binary_patterns=binary_patterns,
        )
        files.append(cf)
        if cf.is_spec:
            has_spec = True

    return ConflictContext(
        project_root=project_root,
        ours_branch=ours,
        theirs_branch=theirs,
        merge_base=base_sha,
        ours_head_sha=ours_sha,
        ours_head_message=_commit_message(project_root, ours_sha),
        theirs_head_sha=theirs_sha,
        theirs_head_message=_commit_message(project_root, theirs_sha),
        ours_log_oneline=_oneline_log(
            project_root, base_sha, ours_sha, limit=log_limit,
        ),
        theirs_log_oneline=_oneline_log(
            project_root, base_sha, theirs_sha, limit=log_limit,
        ),
        files=files,
        has_spec_files=has_spec,
    )
