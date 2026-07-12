"""Code Index — deterministic structure map of the project, summarised per node.

The code-index is a *logical structure map* of the project: the structure is
enumerated **deterministically** (a gitignore-respecting file walk via
``file_enum`` + AST / natural-structure extraction), and a one-sentence summary
for every node is produced by an **LLM** and rendered into the authoritative
``se3/code-index.md`` (committed to git, human-reviewable / human-correctable).

**One physical file is authoritative and self-sufficient.** ``se3/code-index.md``
is the map itself — a zoomable tree ``dir → subdir → … → file → class →
function`` — and every node line carries an embedded content fingerprint as a
terse trailing HTML comment ``<!--#<16-hex>-->``. Because the fingerprint lives
in the committed md (not a volatile sidecar), a fresh clone can decide on its own
which nodes are stale without any out-of-band cache, and an incremental rebuild
both re-summarises only the changed nodes AND preserves human corrections —
neither depends on anything that is not in git. There is intentionally **no json
memo cache**: the md is the single source of truth for structure, summaries, and
fingerprints alike.

Completeness is a property of the deterministic enumerator, not of LLM diligence:
the LLM only summarises the nodes the extractor hands it and never decides who is
included, so a mis-summary never removes a node from the map. Each build
re-enumerates from scratch (new nodes appear, deleted ones are pruned, unchanged
ones reuse the cached summary), so the "map is complete and current for the
present node set" invariant holds every build.

**Bottom-up summarisation.** A node's summary is synthesised from its children's
summaries when it has children, and from its own source otherwise:
- a class/function/method (a leaf) is summarised from its own source segment;
- a file *with* extracted symbols is summarised from those symbols' summaries
  (complete coverage, no source truncation); a file *without* symbols is
  summarised from its own source;
- a directory is summarised from its immediate children's summaries (subdirs +
  files), recursively, up to the project root.
So each build proceeds in dependency order — symbols, then files, then
directories deepest-first — and a change deep in the tree propagates its
freshness up through the recursive directory fingerprint.

Granularity floor (defaults; tunable via ``code_index`` config):
- code files (Python ``ast``) drill to function/method level and stop;
- structured non-code files (markdown headings, yaml/json top-level keys) drill
  to their natural unit; small ones stop at the file level;
- opaque / binary files get a single file-level line;
- line/byte chunking is the LAST-RESORT degrade mode, used ONLY when all three
  of {text (non-binary), zero structural units, over the size threshold} hold.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional

from ..config import (
    CodeIndexConfig,
    get_language_instruction,
    load_code_index_config,
    load_language_config,
)
from . import file_enum

try:  # POSIX advisory locking; absent on some platforms (e.g. Windows).
    import fcntl

    _HAVE_FCNTL = True
except ImportError:  # pragma: no cover - platform dependent
    _HAVE_FCNTL = False

logger = logging.getLogger(__name__)

_MD_REL_PATH = Path("se3") / "code-index.md"
_LOCK_REL_PATH = Path("se3") / "cache" / "code-index.lock"

# Marker rendered on a degraded chunk line so it is unmistakable that the chunk
# boundary is a mechanical line/byte cut with no semantic meaning.
DEGRADED_MARKER = "[degraded:chunk]"

# Max characters of a node's own content (or child-summary digest) fed to the
# LLM summariser, bounding prompt size for very large functions / files / dirs.
_SUMMARY_CONTENT_CAP = 6000

# Embedded fingerprint width: 16 hex chars = 64 bits. A collision is per-node
# (a node's new content hashing to its own old value), not a birthday problem, so
# 2^-64 per edit is decisive; widening costs ~8 chars on a ~200-char line, so we
# pay it once and never reason about collisions again. ``se3 code-index rebuild
# --force`` is the manual backstop for the (vanishingly rare) bad fingerprint.
_FP_LEN = 16

# File extensions that get structural extraction.
_PY_EXTS = {".py", ".pyi"}
_MD_EXTS = {".md", ".markdown"}
_YAML_EXTS = {".yaml", ".yml"}
_JSON_EXTS = {".json"}

# Root directory key (a project-relative path cannot be the empty string, so a
# sentinel names the top level for the dir tree / fingerprint / summary maps).
ROOT_DIR = "(root)"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Fingerprint:
    """Content fingerprint of one file.

    ``mtime``/``size`` are the file-level cheap signals; ``sha256`` is the
    authoritative change signal (a 16-byte / 32-hex prefix), of which the first
    :data:`_FP_LEN` hex chars are embedded in the md.
    """

    mtime: float
    size: int
    sha256: str


@dataclass
class Symbol:
    """A node within a file: a class/function/method, a structural unit, or a
    degraded chunk."""

    local_id: str            # within-file stable id (qualname / heading / key / chunk:N)
    kind: str                # class | function | method | heading | yaml-key | json-key | chunk
    name: str                # display name
    depth: int               # nesting depth within the file (0 = top-level child)
    line_start: int
    line_end: int
    sha256: str              # hash of this symbol's own content segment
    summary: str = ""
    degraded: bool = False
    # The natural unit's own content for units whose semantic content is NOT
    # recoverable by line-slicing (json-key, yaml-key): a JSON top-level key has
    # no line range, and a YAML top-level key's declaration line excludes its
    # nested block. When set, ``_make_target`` summarises from this verbatim
    # content instead of slicing ``line_start..line_end``; line-based units
    # (code symbols, markdown headings, degraded chunks) leave it empty.
    content: str = ""


@dataclass
class FileEntry:
    """A single enumerated file plus its extracted symbols."""

    path: str                # project-relative POSIX path
    kind: str                # python | markdown | yaml | json | text | binary
    fingerprint: Fingerprint  # whole-file fingerprint (used for the file node)
    # Secondary "list" fingerprint: a mechanical hash of this file's DIRECT
    # child-symbol (kind, name) list only — never any symbol body or summary.
    # It is the normal-mode reuse gate for the file node: it changes only when a
    # symbol is added/removed/renamed/re-kinded, so a body-only edit no longer
    # forces the file (and its ancestor chain) to be re-summarised. Because it
    # looks only at direct names, a deep change cannot reach it, so it never
    # cascades upward.
    list_fp: str = ""
    summary: str = ""        # file-level one-line summary
    symbols: List[Symbol] = field(default_factory=list)
    # Absolute path on disk, set during enumeration so the file-level summariser
    # can read source for a structure-less file. Transient (never rendered).
    abs_path: Optional[Path] = None

    def symbol_id(self, sym: Symbol) -> str:
        return f"{self.path}::{sym.local_id}"


@dataclass
class CodeIndex:
    """The in-memory structure map: ``relpath -> FileEntry`` plus the directory
    tree summaries / fingerprints."""

    project_root: Path
    files: Dict[str, FileEntry] = field(default_factory=dict)
    # One-line summary per directory at EVERY level (every ancestor of every
    # file, plus ``(root)``), keyed by the directory key (e.g. ``"src/se3/"`` or
    # ``"(root)"``). Every level of the map carries its own summary so the
    # orientation map is zoomable at the directory level, not just file/symbol.
    dir_summaries: Dict[str, str] = field(default_factory=dict)
    # Recursive content fingerprint per directory (membership + every
    # descendant's content), keyed the same way. Embedded in the md so a rebuild
    # can tell which directories changed without any out-of-band cache.
    dir_fingerprints: Dict[str, str] = field(default_factory=dict)
    # Secondary "list" fingerprint per directory: a mechanical hash of the
    # DIRECT child names only (immediate files + immediate subdirs), keyed the
    # same way. Unlike dir_fingerprints it does NOT fold in descendant content,
    # so it changes only when this directory's own membership changes
    # (a direct child added/removed/renamed) and never cascades up from a deep
    # edit. It is the normal-mode reuse gate for the directory node.
    dir_list_fingerprints: Dict[str, str] = field(default_factory=dict)

    # -- reconstruct from the authoritative md (render-only consumers) ------

    @classmethod
    def from_md(cls, project_root: Path, md_text: str) -> "CodeIndex":
        """Reconstruct a (render-sufficient) index from the authoritative md.

        Reads ONLY the md. The reconstructed entries carry the structure
        (path/kind/name/depth) and summaries needed for rendering; fingerprints
        are not needed for rendering and are left zeroed / empty.
        """
        index = cls(project_root=project_root)
        cur: Optional[FileEntry] = None
        for raw in md_text.splitlines():
            line, _fp, _lfp = _split_fp(raw)
            dh = _MD_DIR_HEADING_RE.match(line)
            if dh:
                # A directory heading (``## `dir/` — summary``). Capture its
                # summary; it owns no symbol bullets, so drop the current file
                # context to avoid mis-attaching a stray bullet.
                if dh.group(2):
                    index.dir_summaries[dh.group(1)] = dh.group(2).strip()
                cur = None
                continue
            fh = _MD_FILE_HEADING_RE.match(line)
            if fh:
                path, kind, summary = fh.group(1), fh.group(2) or "", fh.group(3) or ""
                cur = FileEntry(
                    path=path,
                    kind=kind,
                    fingerprint=Fingerprint(0.0, 0, ""),
                    summary=summary.strip(),
                )
                index.files[path] = cur
                continue
            bullet = _MD_BULLET_RE.match(line)
            if bullet and cur is not None:
                indent, local_id, mid, summary = bullet.groups()
                depth = len(indent) // 2
                kind = ""
                km = _MD_BULLET_KIND_RE.search(mid or "")
                if km:
                    kind = km.group(1)
                degraded = DEGRADED_MARKER in (mid or "")
                cur.symbols.append(
                    Symbol(
                        local_id=local_id,
                        kind=kind,
                        name=local_id,
                        depth=depth,
                        line_start=0,
                        line_end=0,
                        sha256="",
                        summary=(summary or "").strip(),
                        degraded=degraded,
                    )
                )
        return index


# ---------------------------------------------------------------------------
# md round-trip regexes
# ---------------------------------------------------------------------------

# Trailing embedded fingerprint comment. Two shapes, both anchored to EOL:
#   ``<!--#<content>-->``        — content-fp only (legacy; symbol lines; pre-list-fp md)
#   ``<!--#<content>|<list>-->`` — content-fp + list-fp (migrated file/dir lines)
# The list segment is optional so old single-fp md parses unchanged (its list-fp
# is read back as None → "unmigrated", to be filled in mechanically on rebuild).
_FP_COMMENT_RE = re.compile(
    r"\s*<!--#([0-9a-f]{1,%d})(?:\|([0-9a-f]{1,%d}))?-->\s*$" % (_FP_LEN, _FP_LEN)
)

# ``### `path` (kind) — summary``  (kind + summary optional)
#
# The id capture is **lazy** and anchored by a lookahead requiring the closing
# backtick to be followed by the structural suffix (`` (kind)``, `` — summary``,
# or end of line). A plain ``[^`]+`` id group cannot match an id that itself
# contains a backtick; the lazy/lookahead form recovers such ids while still
# stopping at the *real* close.
_MD_FILE_HEADING_RE = re.compile(
    r"^###\s+`(.+?)`(?=\s+\(|\s+—|$)(?:\s+\(([^)]*)\))?(?:\s+—\s+(.*))?$"
)
# ``  - `local_id` <middle> — summary``  (indent captured for depth)
_MD_BULLET_RE = re.compile(
    r"^( *)-\s+`(.+?)`(?=\s+\(|\s+—|$)(.*?)(?:\s+—\s+(.*))?$"
)
_MD_BULLET_KIND_RE = re.compile(r"\(([^)]*)\)")
# ``## `dir/` — summary``  (directory heading; summary optional). The ``##``
# anchor (followed by whitespace) does not match the ``###`` file heading, whose
# third ``#`` is not whitespace, so the two heading levels never collide.
_MD_DIR_HEADING_RE = re.compile(r"^##\s+`([^`]+)`(?:\s+—\s+(.*))?$")


def _split_fp(line: str) -> tuple[str, Optional[str], Optional[str]]:
    """Split a rendered md line into ``(line_without_fp, content_fp, list_fp)``.

    The embedded fingerprint comment is stripped before any heading/bullet regex
    runs, so the summary capture never swallows it and the fingerprints are parsed
    out separately. ``list_fp`` is ``None`` for legacy single-fp lines (symbol
    lines always, and any file/dir line written before list-fps existed).
    """
    m = _FP_COMMENT_RE.search(line)
    if m:
        return line[: m.start()], m.group(1), m.group(2)
    return line, None, None


def _fp_comment(content_fp: str, list_fp: Optional[str] = None) -> str:
    """Render the trailing fingerprint comment for an md line.

    With only a content-fp the legacy ``<!--#<content>-->`` shape is emitted (used
    by symbol lines, which have no list-fp); when a list-fp is supplied the file/
    dir form ``<!--#<content>|<list>-->`` carries both.
    """
    if list_fp:
        return f"<!--#{content_fp}|{list_fp}-->"
    return f"<!--#{content_fp}-->"


# ---------------------------------------------------------------------------
# Summary target + summariser type
# ---------------------------------------------------------------------------

@dataclass
class SummaryTarget:
    """A node that needs an LLM summary (changed or new)."""

    id: str          # full id: dir key, ``relpath`` (file), or ``relpath::local_id``
    path: str        # owning file relpath (or dir key for a dir/file node)
    kind: str
    name: str
    content: str     # the node's own source / segment, or child-summary digest
    level: str = "symbol"   # symbol | file | dir
    degraded: bool = False


# A summariser maps a batch of targets to ``{target.id: one-line summary}``.
Summarizer = Callable[[List[SummaryTarget]], Dict[str, str]]

# A build progress callback, invoked once per file/dir node as it finishes
# summarising, with ``(path, kind, done, total, phase)``: the node's relpath /
# kind, the running done/total counts of file+dir nodes being (re)summarised in
# this build, and the phase (``"file"`` or ``"dir"``). Default ``None`` (no-op)
# keeps every non-web caller — and the whole read-side freshness path —
# byte-for-byte unchanged. The commit step wires in an emitter that writes each
# call to the flow's step jsonl (see ``chat_history.record_index_progress``).
ProgressCallback = Callable[[str, str, int, int, str], None]


# ---------------------------------------------------------------------------
# Hashing helpers
# ---------------------------------------------------------------------------

def _sha256_prefix(data: bytes, prefix_bytes: int = 16) -> str:
    return hashlib.sha256(data).hexdigest()[: prefix_bytes * 2]


def _sha256_text(text: str) -> str:
    return _sha256_prefix(text.encode("utf-8", errors="replace"))


def _fp(sha256: str) -> str:
    """The embedded-fingerprint form of a node's sha256 (first ``_FP_LEN`` hex)."""
    return (sha256 or "")[:_FP_LEN]


def _slice_lines(lines: List[str], start: int, end: int) -> str:
    """Return ``lines[start-1:end]`` joined by newline (1-based inclusive)."""
    if start < 1:
        start = 1
    if end < start:
        end = start
    return "\n".join(lines[start - 1 : end])


# ---------------------------------------------------------------------------
# Structural extraction (deterministic, no LLM)
# ---------------------------------------------------------------------------

def _extract_python(text: str) -> List[Symbol]:
    """Enumerate top-level classes/functions and class methods via ``ast``.

    Drills to function/method level and stops (a nested def inside a function is
    not enumerated — that is implementation detail, read the source for it).
    """
    try:
        tree = ast.parse(text)
    except SyntaxError as exc:
        logger.debug("code_index: python parse failed: %s", exc)
        return []
    lines = text.splitlines()
    out: List[Symbol] = []

    def _emit(node: ast.AST, qualname: str, kind: str, depth: int) -> None:
        ls = getattr(node, "lineno", 0) or 0
        le = getattr(node, "end_lineno", ls) or ls
        seg = _slice_lines(lines, ls, le)
        out.append(
            Symbol(
                local_id=qualname,
                kind=kind,
                name=qualname,
                depth=depth,
                line_start=ls,
                line_end=le,
                sha256=_sha256_text(seg),
            )
        )

    def _walk(body: List[ast.stmt], prefix: str, depth: int) -> None:
        for child in body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                qual = prefix + child.name
                _emit(child, qual, "method" if depth > 0 else "function", depth)
                # Do NOT recurse into function bodies (floor = function/method).
            elif isinstance(child, ast.ClassDef):
                qual = prefix + child.name
                _emit(child, qual, "class", depth)
                _walk(child.body, qual + ".", depth + 1)

    _walk(tree.body, "", 0)
    return out


_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")


def _extract_markdown(text: str) -> List[Symbol]:
    """Enumerate markdown headings as natural structural units."""
    lines = text.splitlines()
    headings: List[tuple[int, int, str]] = []  # (line, level, text)
    in_fence = False
    for i, line in enumerate(lines, start=1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _MD_HEADING_RE.match(line)
        if m:
            headings.append((i, len(m.group(1)), m.group(2).strip()))
    out: List[Symbol] = []
    seen: Dict[str, int] = {}
    for idx, (ln, level, htext) in enumerate(headings):
        # Section ends just before the next heading (any level), or EOF.
        end = (headings[idx + 1][0] - 1) if idx + 1 < len(headings) else len(lines)
        seg = _slice_lines(lines, ln, end)
        local = htext
        if local in seen:
            seen[local] += 1
            local = f"{htext}#{seen[local]}"
        else:
            seen[local] = 1
        out.append(
            Symbol(
                local_id=local,
                kind="heading",
                name=htext,
                depth=level - 1,
                line_start=ln,
                line_end=end,
                sha256=_sha256_text(seg),
            )
        )
    return out


def _line_of_top_key(lines: List[str], key: str) -> int:
    """Best-effort 1-based line of a top-level ``key:`` declaration."""
    pat = re.compile(r"^" + re.escape(str(key)) + r"\s*:")
    for i, line in enumerate(lines, start=1):
        if pat.match(line):
            return i
    return 0


def _extract_yaml(text: str) -> List[Symbol]:
    """Enumerate yaml top-level mapping keys."""
    try:
        import yaml

        data = yaml.safe_load(text)
    except Exception as exc:
        logger.debug("code_index: yaml parse failed: %s", exc)
        return []
    if not isinstance(data, dict):
        return []
    lines = text.splitlines()
    out: List[Symbol] = []
    for key in data:
        value = data[key]
        seg = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
        ln = _line_of_top_key(lines, key)
        out.append(
            Symbol(
                local_id=str(key),
                kind="yaml-key",
                name=str(key),
                depth=0,
                line_start=ln,
                line_end=ln,
                sha256=_sha256_text(f"{key}:{seg}"),
                content=f"{key}: {seg}",
            )
        )
    return out


def _extract_json(text: str) -> List[Symbol]:
    """Enumerate json top-level object keys."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError) as exc:
        logger.debug("code_index: json parse failed: %s", exc)
        return []
    if not isinstance(data, dict):
        return []
    out: List[Symbol] = []
    for key in data:
        value = data[key]
        seg = json.dumps(value, sort_keys=True, default=str, ensure_ascii=False)
        out.append(
            Symbol(
                local_id=str(key),
                kind="json-key",
                name=str(key),
                depth=0,
                line_start=0,
                line_end=0,
                sha256=_sha256_text(f"{key}:{seg}"),
                content=f"{key}: {seg}",
            )
        )
    return out


def _extract_structure(path: Path, text: str) -> List[Symbol]:
    """Dispatch structural extraction by extension; ``[]`` when no extractor or
    the extractor found zero natural units."""
    ext = path.suffix.lower()
    if ext in _PY_EXTS:
        return _extract_python(text)
    if ext in _MD_EXTS:
        return _extract_markdown(text)
    if ext in _YAML_EXTS:
        return _extract_yaml(text)
    if ext in _JSON_EXTS:
        return _extract_json(text)
    return []


def _file_kind(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in _PY_EXTS:
        return "python"
    if ext in _MD_EXTS:
        return "markdown"
    if ext in _YAML_EXTS:
        return "yaml"
    if ext in _JSON_EXTS:
        return "json"
    return "text"


# ---------------------------------------------------------------------------
# Degrade mode (last-resort line/byte chunking)
# ---------------------------------------------------------------------------

def is_degrade_eligible(
    text: str, has_structure: bool, cfg: CodeIndexConfig
) -> bool:
    """Three-condition degrade gate (ALL must hold; missing one → no degrade).

    1. text (non-binary) — the caller only invokes this for text files;
    2. zero structural units — ``has_structure`` is False; and
    3. over the size threshold — ``> degrade_trigger_lines`` OR
       ``> degrade_trigger_bytes`` (first to trip).
    """
    if has_structure:
        return False
    lines = text.count("\n") + (1 if text else 0)
    size = len(text.encode("utf-8", errors="replace"))
    return lines > cfg.degrade_trigger_lines or size > cfg.degrade_trigger_bytes


def _chunk_degraded(text: str, cfg: CodeIndexConfig) -> List[Symbol]:
    """Split a degraded file into chunks of ``chunk_lines`` / ``chunk_bytes``
    (first limit to trip cuts the chunk). Each chunk is one degraded symbol."""
    lines = text.splitlines()
    out: List[Symbol] = []
    chunk_no = 0
    i = 0
    n = len(lines)
    while i < n:
        chunk_no += 1
        start = i
        byte_count = 0
        j = i
        while j < n:
            line_bytes = len(lines[j].encode("utf-8", errors="replace")) + 1
            # Always take at least one line, then stop when either limit trips.
            if j > start and (
                (j - start) >= cfg.chunk_lines
                or byte_count + line_bytes > cfg.chunk_bytes
            ):
                break
            byte_count += line_bytes
            j += 1
        seg = "\n".join(lines[start:j])
        line_start = start + 1
        line_end = j
        out.append(
            Symbol(
                local_id=f"chunk:{chunk_no}",
                kind="chunk",
                name=f"chunk {chunk_no} (lines {line_start}-{line_end})",
                depth=0,
                line_start=line_start,
                line_end=line_end,
                sha256=_sha256_text(seg),
                degraded=True,
            )
        )
        i = j
    return out


# ---------------------------------------------------------------------------
# Per-file indexing (structure only, no summaries yet)
# ---------------------------------------------------------------------------

def _index_file(path: Path, relpath: str, cfg: CodeIndexConfig) -> FileEntry:
    """Build the structural FileEntry for *path* (no summaries assigned).

    Binary / unreadable files become a single file-level node. Text files get
    structural extraction; a structure-less text file degrades to chunks only
    when the three-condition gate holds, otherwise it stops at one file line.
    """
    try:
        stat = path.stat()
        mtime, size = stat.st_mtime, stat.st_size
    except OSError:
        mtime, size = 0.0, 0

    if file_enum.is_binary(path):
        return FileEntry(
            path=relpath,
            kind="binary",
            fingerprint=Fingerprint(mtime, size, ""),
            abs_path=path,
        )

    try:
        data = path.read_bytes()
    except OSError as exc:
        logger.warning("code_index: failed to read %s: %s", relpath, exc)
        return FileEntry(
            path=relpath, kind="binary", fingerprint=Fingerprint(mtime, size, ""),
            abs_path=path,
        )

    file_sha = _sha256_prefix(data)
    text = data.decode("utf-8", errors="replace")
    kind = _file_kind(path)

    symbols = _extract_structure(path, text)
    # Size-cap secondary guard (independent of structure): an oversized tracked
    # file — a huge generated module, a vendored blob, a large data JSON with
    # thousands of top-level keys — is dropped to a single file-level line
    # instead of enumerating every symbol, which would bloat code-index.md and
    # the per-build LLM summarisation cost. The cap reuses the degrade size
    # thresholds. A structure-LESS oversized file still degrades to chunks (the
    # designed last-resort path); only the structure-FUL oversized case is what
    # this guard newly catches.
    fsize = file_enum.FileSize.from_bytes(data)
    oversized = (
        fsize.lines > cfg.degrade_trigger_lines
        or fsize.bytes > cfg.degrade_trigger_bytes
    )
    if symbols:
        if oversized:
            symbols = []
    elif is_degrade_eligible(text, False, cfg):
        symbols = _chunk_degraded(text, cfg)

    return FileEntry(
        path=relpath,
        kind=kind,
        fingerprint=Fingerprint(mtime, size, file_sha),
        symbols=symbols,
        abs_path=path,
    )


# ---------------------------------------------------------------------------
# Paths + locking + atomic write
# ---------------------------------------------------------------------------

def md_path(project_root: Path) -> Path:
    return Path(project_root) / _MD_REL_PATH


def lock_path(project_root: Path) -> Path:
    """Path of the advisory lock file serializing concurrent (re)builds.

    Lives under the gitignored ``se3/cache/`` directory so it is never committed.
    """
    return Path(project_root) / _LOCK_REL_PATH


@contextlib.contextmanager
def _build_lock(project_root: Path) -> Iterator[None]:
    """Hold an exclusive advisory ``flock`` across a (re)build's whole
    load → enumerate → write critical section, so two concurrent
    ``load_or_build`` calls (parallel flows, or a CLI rebuild racing a flow's
    lazy refresh) cannot interleave their md reads and atomic replaces — and a
    slower, stale writer cannot clobber a fresher map.

    Because the lock spans the read too, the writer that waits re-enumerates the
    *post-lock* on-disk state (the other build's freshly written md + any source
    edits), so it produces a current result rather than overwriting with stale
    data.

    Best-effort: when ``fcntl`` is unavailable (non-POSIX) or the lock file
    cannot be opened, the build proceeds **unlocked** rather than failing.
    """
    if not _HAVE_FCNTL:
        yield
        return

    lp = lock_path(project_root)
    fd: Optional[int] = None
    try:
        lp.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lp), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as exc:
        logger.warning(
            "code_index: cannot open lock file %s (%s); building unlocked", lp, exc
        )
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        yield
        return

    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically via a unique temp file in the same
    directory + ``os.replace``.

    A unique per-write temp name (``tempfile.mkstemp``) — not a fixed ``.tmp``
    sibling — is required so two concurrent writers never share the same scratch
    file: with a fixed name, one process could ``os.replace`` (and thereby
    unlink) the temp file out from under another still writing to it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Directory tree helpers (every level, parent ⇄ child)
# ---------------------------------------------------------------------------

def _dir_of(relpath: str) -> str:
    """The directory key of the *immediate parent* of a FILE."""
    if "/" in relpath:
        return relpath.rsplit("/", 1)[0] + "/"
    return ROOT_DIR


def _parent_dir(dirkey: str) -> Optional[str]:
    """The directory key of the parent of a DIR (``None`` for the root)."""
    if dirkey == ROOT_DIR:
        return None
    inner = dirkey.rstrip("/")
    if "/" in inner:
        return inner.rsplit("/", 1)[0] + "/"
    return ROOT_DIR


def _depth(dirkey: str) -> int:
    """Nesting depth of a directory key (root = 0, ``src/`` = 1, ``src/se3/`` = 2)."""
    if dirkey == ROOT_DIR:
        return 0
    return dirkey.count("/")


def _all_dir_keys(files: Dict[str, FileEntry]) -> set[str]:
    """Every directory key in the tree: every ancestor of every file + root."""
    dirs = {ROOT_DIR}
    for rel in files:
        d: Optional[str] = _dir_of(rel)
        while d is not None:
            dirs.add(d)
            d = _parent_dir(d)
    return dirs


def _child_files(dirkey: str, files: Dict[str, FileEntry]) -> List[str]:
    """Relpaths of files whose immediate parent directory is *dirkey* (sorted)."""
    return sorted(r for r in files if _dir_of(r) == dirkey)


def _child_dirs(dirkey: str, all_dirs: set[str]) -> List[str]:
    """Directory keys whose immediate parent is *dirkey* (sorted)."""
    return sorted(d for d in all_dirs if _parent_dir(d) == dirkey)


def _compute_dir_fps(
    files: Dict[str, FileEntry], all_dirs: set[str]
) -> Dict[str, str]:
    """Recursive content fingerprint per directory, computed bottom-up.

    A directory's fingerprint folds in both its *membership* and the *content* of
    everything beneath it — each immediate child file paired with that file's
    whole-file sha256, and each immediate child subdirectory paired with that
    subdir's own (already-folded) fingerprint. A change anywhere in the subtree
    (a file edited, added, removed, renamed) therefore propagates up to the root,
    so every ancestor directory is re-summarised on the next build while a
    genuinely untouched sibling reuses its cached summary.
    """
    memo: Dict[str, str] = {}

    def fp(dirkey: str) -> str:
        if dirkey in memo:
            return memo[dirkey]
        parts: List[str] = []
        for rel in _child_files(dirkey, files):
            parts.append(f"f:{rel}:{files[rel].fingerprint.sha256}")
        for sub in _child_dirs(dirkey, all_dirs):
            parts.append(f"d:{sub}:{fp(sub)}")
        h = _fp(_sha256_text("\n".join(parts)))
        memo[dirkey] = h
        return h

    for d in all_dirs:
        fp(d)
    return memo


def _compute_file_list_fp(fe: FileEntry) -> str:
    """The file's *list* fingerprint: a mechanical hash of its direct symbols'
    ordered ``(kind, name)`` list.

    Deliberately ignores each symbol's body, summary, and source position — only
    the structural roster (what symbols exist, by kind and name) feeds the hash.
    A body-only edit leaves this unchanged; adding/removing/renaming/re-kinding a
    symbol changes it. ``(kind, name)`` (not ``name`` alone) lets a
    ``function``→``class`` change register as a roster change. No LLM, no child
    summaries.

    The roster is sorted before hashing so that pure reordering (moving an
    unchanged symbol above/below another) is NOT a roster change — only the
    *set* of (kind, name) members matters, not their source-file order.
    """
    parts = sorted(f"{sym.kind}:{sym.name}" for sym in fe.symbols)
    return _fp(_sha256_text("\n".join(parts)))


def _compute_dir_list_fps(
    files: Dict[str, FileEntry], all_dirs: set[str]
) -> Dict[str, str]:
    """List fingerprint per directory: a mechanical hash of the directory's
    DIRECT child names only — immediate files and immediate subdirs, sorted.

    Non-recursive by design: unlike :func:`_compute_dir_fps` it folds in neither
    descendant content nor child summaries, so it changes only when this
    directory's own membership changes (a direct child added/removed/renamed) and
    a deep edit beneath it never bubbles up. This is what makes each level's
    normal-mode reuse decision independent of the levels below it. No LLM.
    """
    result: Dict[str, str] = {}
    for dirkey in all_dirs:
        parts: List[str] = []
        for rel in _child_files(dirkey, files):
            parts.append(f"f:{rel}")
        for sub in _child_dirs(dirkey, all_dirs):
            parts.append(f"d:{sub}")
        result[dirkey] = _fp(_sha256_text("\n".join(parts)))
    return result


# ---------------------------------------------------------------------------
# Markdown rendering (authoritative product) — full tree with embedded fps
# ---------------------------------------------------------------------------

def render_full(index: CodeIndex) -> str:
    """Render the complete authoritative map (every directory level, files, and
    all symbols), each line carrying its embedded content fingerprint."""
    lines: List[str] = ["# Code Index", ""]
    files_by_dir: Dict[str, List[FileEntry]] = {}
    for relpath in sorted(index.files):
        files_by_dir.setdefault(_dir_of(relpath), []).append(index.files[relpath])

    for dir_name in sorted(_all_dir_keys(index.files)):
        head = f"## `{dir_name}`"
        dir_summary = index.dir_summaries.get(dir_name, "")
        if dir_summary:
            head += f" — {dir_summary}"
        dfp = index.dir_fingerprints.get(dir_name, "")
        # Embed the fingerprint ONLY when the node has a real summary. This makes
        # any partial md (flushed mid-build as a checkpoint) a safe resume point:
        # a node with no summary yet also carries no fingerprint, so the next
        # build sees "no fp → stale" and (re)summarises it rather than reusing an
        # empty summary. In a completed md every node has a summary, so this is a
        # no-op there.
        if dir_summary and dfp:
            head += f" {_fp_comment(dfp, index.dir_list_fingerprints.get(dir_name))}"
        lines.append(head)
        lines.append("")
        for fe in sorted(files_by_dir.get(dir_name, []), key=lambda f: f.path):
            fhead = f"### `{fe.path}` ({fe.kind})"
            if fe.summary:
                fhead += f" — {fe.summary}"
            ffp = _fp(fe.fingerprint.sha256)
            if fe.summary and ffp:
                fhead += f" {_fp_comment(ffp, fe.list_fp or None)}"
            lines.append(fhead)
            for sym in fe.symbols:
                indent = "  " * sym.depth
                marker = f" {DEGRADED_MARKER}" if sym.degraded else ""
                bullet = f"{indent}- `{sym.local_id}` ({sym.kind}){marker}"
                if sym.summary:
                    bullet += f" — {sym.summary}"
                sfp = _fp(sym.sha256)
                if sym.summary and sfp:
                    bullet += f" {_fp_comment(sfp)}"
                lines.append(bullet)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_md(project_root: Path, index: CodeIndex) -> Path:
    path = md_path(project_root)
    _atomic_write_text(path, render_full(index))
    return path


def _parse_md(
    md_text: str,
) -> tuple[Dict[str, str], Dict[str, str], Dict[str, str]]:
    """Parse the authoritative md into ``(summaries, content_fps, list_fps)``.

    All three maps are keyed by node id: a directory key (``"src/"``), a file
    relpath, or ``relpath::local_id`` for a symbol. ``summaries`` holds the
    human-correctable one-liners reused for unchanged nodes; ``content_fps`` holds
    each node's embedded content fingerprint; ``list_fps`` holds the secondary
    list fingerprint that file/dir lines carry once migrated (symbol lines never
    carry one, and a pre-migration md has none — such ids are simply absent from
    ``list_fps``, signalling "fall back to content-fp" to the reuse gate). All are
    read from the committed md alone (no out-of-band cache).
    """
    summaries: Dict[str, str] = {}
    content_fps: Dict[str, str] = {}
    list_fps: Dict[str, str] = {}
    cur_path: Optional[str] = None
    for raw in md_text.splitlines():
        line, fp, lfp = _split_fp(raw)
        dh = _MD_DIR_HEADING_RE.match(line)
        if dh:
            key = dh.group(1)
            if dh.group(2):
                summaries[key] = dh.group(2).strip()
            if fp:
                content_fps[key] = fp
            if lfp:
                list_fps[key] = lfp
            cur_path = None
            continue
        fh = _MD_FILE_HEADING_RE.match(line)
        if fh:
            cur_path = fh.group(1)
            if fh.group(3):
                summaries[cur_path] = fh.group(3).strip()
            if fp:
                content_fps[cur_path] = fp
            if lfp:
                list_fps[cur_path] = lfp
            continue
        bullet = _MD_BULLET_RE.match(line)
        if bullet and cur_path is not None:
            _indent, local_id, _mid, summary = bullet.groups()
            sid = f"{cur_path}::{local_id}"
            if summary:
                summaries[sid] = summary.strip()
            if fp:
                content_fps[sid] = fp
    return summaries, content_fps, list_fps


def _parse_md_summaries(md_text: str) -> Dict[str, str]:
    """Parse just the ``{id: summary}`` map from the authoritative md."""
    return _parse_md(md_text)[0]


# ---------------------------------------------------------------------------
# Default LLM summariser
# ---------------------------------------------------------------------------

def _flatten_summary(value: object) -> str:
    """Collapse a summary to a single physical line.

    The md format and the md→summary round-trip assume exactly one node per line;
    an LLM summary that carries a newline would render across two physical lines,
    corrupting the md and silently losing the orphaned tail on the next
    incremental parse. Any run of whitespace — including newlines — is collapsed
    to a single space.
    """
    return re.sub(r"\s+", " ", str(value)).strip()


def _heuristic_summary(target: SummaryTarget) -> str:
    """Deterministic fallback summary used when no LLM is available or a call
    fails — keeps a build from ever crashing and a node from ever being summary-
    less. Honest about being a placeholder."""
    if target.degraded:
        return f"degraded chunk of {target.path} (boundary not semantic)"
    return f"{target.kind} {target.name}"


def _make_llm_summarizer(
    project_root: Path,
    max_concurrency: int = 1,
    *,
    on_node: Optional[Callable[[SummaryTarget], None]] = None,
) -> Summarizer:
    """Construct the default LLM-backed summariser (lazy LLMCaller import).

    Batches targets per owning path into one ``LLMCaller.call`` each, asking for
    a JSON ``{id: summary}`` map. The per-path groups of a single wave run
    concurrently on a ``ThreadPoolExecutor`` bounded by ``max_concurrency``.

    **Each concurrent group constructs its OWN ``LLMCaller`` — a caller is never
    shared across threads.** The charter reserves multi-command rotation/fallback
    to a single LLMCaller instance, and that rotation/retry state is not
    thread-safe; a fresh caller per group therefore keeps every concurrent call
    inside the existing execution-stack boundary and makes the parallelism safe
    by construction (no locking, no cross-thread caller state).

    ``on_node`` (default ``None``) is fired once per group the instant ITS OWN
    call returns — from the worker thread, before the wave's other in-flight
    groups finish. A group is one path, so the fire reports that file/dir (a
    symbol group reports its owning file, which the emitter rolls up and dedups).
    Firing here (rather than after the whole wave is assigned) gives the web
    console a per-file live update instead of a per-wave burst; the emitter is
    lock-guarded, so concurrent fires are safe.

    Any failure degrades to the heuristic summary for that group so a build is
    never aborted by a flaky LLM call, and one group's failure never affects the
    others.
    """

    # code-index summaries are a knowledge asset, so they are written in the
    # configured spec_language when set. Resolved ONCE here (not per group) so a
    # build's many concurrent groups share one config read; appended to each
    # group's prompt. Unset spec_language yields "" (zero-injection: the prompt —
    # and thus prior behaviour — is byte-for-byte unchanged).
    spec_lang_instruction = get_language_instruction(
        load_language_config(project_root).spec_language,
        "code_index",
        for_knowledge=True,
    )

    def _summarize_group(item: tuple[str, List[SummaryTarget]]) -> Dict[str, str]:
        # A single per-file group: build the prompt, call this group's OWN caller,
        # and map the JSON result back onto the group's targets. Runs on a worker
        # thread; owns its LLMCaller so no caller state is shared across threads.
        from .llm_caller import LLMCaller

        relpath, group = item
        parsed: object = {}
        try:
            listing = "\n".join(
                f"- id={t.id!r} kind={t.kind} name={t.name!r}\n```\n{t.content[:_SUMMARY_CONTENT_CAP]}\n```"
                for t in group
            )
            prompt = (
                "You are building a code-index structure map. For each listed "
                "node, write ONE concise sentence describing what it is / does "
                "(orientation, not implementation detail). A node's content may be "
                "its own source, or a digest of its children's one-line summaries "
                "(for a file or directory) — summarise the whole from those parts. "
                "Respond with a JSON object mapping each node id to its "
                "one-sentence summary.\n\n"
                f"Path: {relpath}\n\nNodes:\n{listing}"
            ) + spec_lang_instruction
            caller = LLMCaller(project_root=project_root, step_type="code_index")
            raw = caller.call(prompt, json_mode="two_phase")
            parsed = json.loads(raw) if isinstance(raw, str) else {}
        except Exception as exc:  # noqa: BLE001 — never let a build crash
            logger.warning(
                "code_index: LLM summary failed for %s: %s", relpath, exc
            )
            parsed = {}
        out: Dict[str, str] = {}
        for t in group:
            val = parsed.get(t.id) if isinstance(parsed, dict) else None
            out[t.id] = _flatten_summary(val) if val else _heuristic_summary(t)
        # Report this group's file/dir the moment its own call finishes. A group
        # is one path (all targets share it), so ONE report per group is right:
        # for a symbol group that path is the owning file, which _on_node rolls
        # up and dedups — a body-only edit (symbol group, reused file node) still
        # surfaces its file to the progress display.
        if on_node is not None and group:
            on_node(group[0])
        return out

    def _summarize(targets: List[SummaryTarget]) -> Dict[str, str]:
        by_file: Dict[str, List[SummaryTarget]] = {}
        for t in targets:
            by_file.setdefault(t.path, []).append(t)
        if not by_file:
            return {}

        result: Dict[str, str] = {}
        groups = list(by_file.items())
        # Cap workers at both the config bound and the number of groups: one
        # group per file, so more workers than groups would idle. A single group
        # (or max_concurrency<=1) runs inline — no thread pool overhead and no
        # behavioural difference from the concurrent path.
        workers = max(1, min(max_concurrency, len(groups)))
        if workers == 1:
            for item in groups:
                result.update(_summarize_group(item))
            return result

        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as pool:
            for group_result in pool.map(_summarize_group, groups):
                result.update(group_result)
        return result

    return _summarize


# ---------------------------------------------------------------------------
# Summary targets (per level)
# ---------------------------------------------------------------------------

def _make_target(
    fe: FileEntry, sym: Optional[Symbol], path: Path
) -> SummaryTarget:
    """Build the summary target for a symbol (``sym`` given) or, as a fallback,
    a file node from its own source (``sym=None``)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace") if path else ""
    except OSError:
        text = ""
    lines = text.splitlines()
    if sym is None:
        return SummaryTarget(
            id=fe.path, path=fe.path, kind=fe.kind, name=fe.path,
            content=text[:_SUMMARY_CONTENT_CAP], level="file",
        )
    if sym.content:
        # Structured non-code units (json-key, yaml-key) carry their own content
        # because line-slicing cannot recover it (no line range, or only the key
        # declaration line). Summarise from that verbatim content.
        seg = sym.content
    elif sym.line_start:
        seg = _slice_lines(lines, sym.line_start, sym.line_end)
    else:
        seg = ""
    return SummaryTarget(
        id=fe.symbol_id(sym), path=fe.path, kind=sym.kind, name=sym.name,
        content=seg[:_SUMMARY_CONTENT_CAP], level="symbol", degraded=sym.degraded,
    )


def _make_file_target(fe: FileEntry) -> SummaryTarget:
    """Build a file's summary target — bottom-up.

    A file WITH extracted symbols is summarised from a digest of those symbols'
    (already-computed) one-line summaries, giving complete coverage with no
    source truncation. A file WITHOUT symbols (small code file, structure-less
    text, opaque) falls back to its own source.
    """
    if fe.symbols:
        content = "\n".join(
            f"- {s.local_id} ({s.kind}): {s.summary}".rstrip() for s in fe.symbols
        )
    else:
        try:
            content = (
                fe.abs_path.read_text(encoding="utf-8", errors="replace")
                if fe.abs_path
                else ""
            )
        except OSError:
            content = ""
    return SummaryTarget(
        id=fe.path, path=fe.path, kind=fe.kind, name=fe.path,
        content=content[:_SUMMARY_CONTENT_CAP], level="file",
    )


def _make_dir_target(
    dirkey: str, index: CodeIndex, all_dirs: set[str]
) -> SummaryTarget:
    """Build a directory's summary target from its immediate children's summaries
    (subdirectories then files) — bottom-up."""
    children: List[str] = []
    for sub in _child_dirs(dirkey, all_dirs):
        children.append(
            f"- {sub} (directory): {index.dir_summaries.get(sub, '')}".rstrip()
        )
    for rel in _child_files(dirkey, index.files):
        fe = index.files[rel]
        children.append(f"- {rel} ({fe.kind}): {fe.summary}".rstrip())
    content = "\n".join(children)[:_SUMMARY_CONTENT_CAP]
    name = "the project root" if dirkey == ROOT_DIR else dirkey
    return SummaryTarget(
        id=dirkey, path=dirkey, kind="directory", name=name,
        content=content, level="dir",
    )


# ---------------------------------------------------------------------------
# Build orchestration (bottom-up waves)
# ---------------------------------------------------------------------------

def _summarize_wave(
    summ: Summarizer,
    targets: List[SummaryTarget],
    index: CodeIndex,
    *,
    on_node: Optional[Callable[[SummaryTarget], None]] = None,
) -> None:
    """Summarise one dependency wave and assign the results onto their nodes.

    Skips the summariser entirely for an empty wave, so a no-op rebuild (nothing
    changed at any level) never invokes the LLM.

    ``on_node`` (default ``None``) is fired once per assigned target so the build
    can report progress. The web display tracks FILES and DIRS, not symbols, but
    a symbol target still fires it: ``_on_node`` rolls a symbol up to a single
    per-file report (a file whose only stale content is a symbol body is
    reportable ONLY via its symbols — the file node is reused) and dedups by path
    so a file stale in both the symbol wave and the file wave is reported once.
    This wave-level firing (after the whole summariser call has returned) is the
    path for an INJECTED summariser, which is opaque and cannot report per group;
    the default LLM summariser instead fires per group the instant each call
    finishes, so its caller passes ``on_node=None`` here to avoid a double report.
    """
    if not targets:
        return
    produced = summ(targets)
    for t in targets:
        value = produced.get(t.id) if isinstance(produced, dict) else None
        value = _flatten_summary(value) if value else _heuristic_summary(t)
        if t.level == "dir":
            index.dir_summaries[t.id] = value
        elif t.level == "symbol":
            fe = index.files[t.path]
            local = t.id.split("::", 1)[1]
            for sym in fe.symbols:
                if sym.local_id == local:
                    sym.summary = value
                    break
        else:
            index.files[t.id].summary = value
        # Fire for every level; _on_node normalises a symbol to its owning file
        # and dedups by path, so symbol-only-stale files still surface once.
        if on_node is not None:
            on_node(t)


def build_index(
    project_root: Path,
    summarizer: Optional[Summarizer] = None,
    force: bool = False,
    cfg: Optional[CodeIndexConfig] = None,
    progress: Optional[ProgressCallback] = None,
) -> CodeIndex:
    """Enumerate the project, extract structure, summarise changed nodes
    bottom-up, and write the authoritative md. The single (re)build entry point.

    Incremental: a node whose content fingerprint matches the one embedded in the
    md reuses its md summary (preserving human corrections) and is NOT
    re-summarised. With ``force=True`` the md is ignored and every node is
    re-summarised from scratch.

    The whole load → enumerate → write critical section runs under an exclusive
    advisory lock (:func:`_build_lock`) so concurrent (re)builds serialize; the
    lock is best-effort and the build still proceeds (unlocked) when ``fcntl`` is
    unavailable.
    """
    project_root = Path(project_root)
    cfg = cfg or load_code_index_config(project_root)

    with _build_lock(project_root):
        return _build_index_locked(project_root, summarizer, force, cfg, progress)


def _build_index_locked(
    project_root: Path,
    summarizer: Optional[Summarizer],
    force: bool,
    cfg: CodeIndexConfig,
    progress: Optional[ProgressCallback] = None,
) -> CodeIndex:
    """The body of :func:`build_index`, run while holding the build lock."""
    md_summaries: Dict[str, str] = {}
    md_fps: Dict[str, str] = {}
    # md_list_fps carries the secondary list fingerprints parsed from the md; it
    # is the normal-mode reuse gate for file/dir nodes (see _reuse_file/_reuse_dir
    # below). An md predating list-fps leaves an id absent here, which the gate
    # reads as "fall back to content-fp" for lazy, zero-LLM migration.
    md_list_fps: Dict[str, str] = {}
    if not force:
        mp = md_path(project_root)
        if mp.exists():
            try:
                md_summaries, md_fps, md_list_fps = _parse_md(
                    mp.read_text(encoding="utf-8")
                )
            except OSError:
                md_summaries, md_fps, md_list_fps = {}, {}, {}

    files = file_enum.enumerate_index_files(project_root, cfg.exclude)
    index = CodeIndex(project_root=Path(project_root))
    root = Path(project_root).resolve()

    # --- structural enumeration (no summaries yet) ---
    for abs_path in files:
        try:
            relpath = abs_path.resolve().relative_to(root).as_posix()
        except ValueError:
            relpath = abs_path.name
        index.files[relpath] = _index_file(abs_path, relpath, cfg)

    all_dirs = _all_dir_keys(index.files)
    dir_fps = _compute_dir_fps(index.files, all_dirs)
    index.dir_fingerprints = dir_fps

    # list-fps depend only on the enumerated structure (direct child names), never
    # on a summary, so they can be computed here — before any summarisation —
    # without touching the bottom-up build order. They become each file/dir
    # node's normal-mode reuse gate (wired in a later group); computing them
    # eagerly here keeps that wiring a pure read of already-populated state.
    for fe in index.files.values():
        fe.list_fp = _compute_file_list_fp(fe)
    index.dir_list_fingerprints = _compute_dir_list_fps(index.files, all_dirs)

    def _reuse(fp_key: str, cur_sha: str) -> bool:
        """A node is reusable when its current content fingerprint matches the
        md's. Symbols always gate on this: a symbol is a leaf, and refreshing the
        one symbol whose own body changed is both the cheapest and the most apt
        thing to do. --force callers never reuse anything (md_fps is empty)."""
        return (not force) and bool(cur_sha) and md_fps.get(fp_key) == _fp(cur_sha)

    # Why a *separate* list-fp gate for files/dirs instead of the content-fp one:
    # content-fp folds in the whole subtree, so any edit bubbles up the ancestor
    # chain and re-summarises every level — the waste this design removes. The
    # list-fp captures only this node's DIRECT-member roster (a file's symbol
    # name+kind list; a dir's direct child names), so a body-only edit leaves it
    # unchanged and each level decides independently — a roster change at one level
    # never cascades to its parent. --force deliberately bypasses both list gates
    # (returns False) and falls back to content-fp cascade to catch same-name
    # behaviour drift / deep semantic shifts the list gate is blind to.
    def _reuse_file(fe: FileEntry) -> bool:
        if force:
            return False
        # A symbol-less file (plain text/config, heading-less prose, a JSON/YAML
        # with no keys, or an oversized module collapsed to a bare file node) has
        # an empty, CONSTANT list-fp — it would match the md forever and freeze
        # the summary. Such a file has no child symbol leaf to catch a body edit,
        # and its file-level summary is the ONLY representation of its content, so
        # it must gate on the content-fp like a leaf: a content rewrite re-
        # summarises the file node itself. (Gating on the constant empty roster
        # would silently drop content drift in these files from normal mode.)
        if not fe.symbols:
            return _reuse(fe.path, fe.fingerprint.sha256)
        md_lfp = md_list_fps.get(fe.path)
        if md_lfp is not None:
            return bool(fe.list_fp) and md_lfp == fe.list_fp
        # Pre-migration md: no list-fp recorded for this file. Reuse on a
        # content-fp match (zero LLM); render then writes the freshly computed
        # list-fp back, completing the migration in place.
        return _reuse(fe.path, fe.fingerprint.sha256)

    def _reuse_dir(dirkey: str) -> bool:
        if force:
            return False
        cur_lfp = index.dir_list_fingerprints.get(dirkey, "")
        md_lfp = md_list_fps.get(dirkey)
        if md_lfp is not None:
            return bool(cur_lfp) and md_lfp == cur_lfp
        # Pre-migration md: fall back to the dir's content-fp (already _fp-folded
        # by _compute_dir_fps, so compared raw against the md value).
        dfp = dir_fps.get(dirkey, "")
        return bool(dfp) and md_fps.get(dirkey) == dfp

    # --- Pass 1: seed every reused summary up front (NO LLM). ---
    # Each checkpoint flush re-renders the WHOLE index, so any file the work loop
    # has not reached yet must ALREADY carry its (unchanged) summary + fingerprint
    # — otherwise the flush blanks it, dropping its fingerprint, and a later build
    # needlessly re-summarises it. Loading the entire reuse set first makes every
    # partial md a proper superset of the prior one, preserving incrementality
    # across an interrupted build.
    for fe in index.files.values():
        if fe.kind == "binary":
            fe.summary = "(binary file — file-level entry only)"
        elif _reuse_file(fe):
            fe.summary = md_summaries.get(fe.path, "")
        for sym in fe.symbols:
            sid = fe.symbol_id(sym)
            if _reuse(sid, sym.sha256):
                sym.summary = md_summaries.get(sid, "")
    for dir_name in all_dirs:
        if _reuse_dir(dir_name):
            index.dir_summaries[dir_name] = md_summaries.get(dir_name, "")

    # A file needs (re)summarisation when its OWN file node is stale OR any of
    # its symbols is stale. The symbol case is the COMMON one for a body-only
    # bugfix commit: editing a function body changes only that symbol's content
    # fingerprint, leaving the file node's list-fp (its symbol name/kind roster)
    # unchanged — so the file node is reused, yet the build still makes a real
    # per-symbol LLM call for that file. Defined here (before progress accounting)
    # because BOTH the progress total and the Pass 2 stale-file batching key on it.
    def _file_needs_work(fe: FileEntry) -> bool:
        if fe.kind != "binary" and not _reuse_file(fe):
            return True
        return any(
            not _reuse(fe.symbol_id(sym), sym.sha256) for sym in fe.symbols
        )

    # --- Progress accounting (web console during the commit-time rebuild) ---
    # Total = the FILES and DIRS that will do real (re)summarisation work this
    # build. A file counts if it needs work for ANY reason — crucially INCLUDING
    # the body-only-edit case where only a symbol is stale and the file node
    # itself is reused: that file still makes a genuine LLM call, so the WebUI
    # must surface it (else a typical bugfix commit shows zero progress for the
    # whole rebuild). Symbols are not tracked as their own units — a file's stale
    # symbols roll up to ONE "file being updated" report, carried by the symbol
    # wave's per-file group (which knows the file's relpath). Each file/dir is
    # reported EXACTLY ONCE (deduped by path in _on_node), and both the counter
    # bump AND the emit are taken under the lock so the reported done sequence
    # stays monotonic in write order (1..total) even when concurrent groups
    # finish near-together.
    progress_total = (
        sum(1 for fe in index.files.values() if _file_needs_work(fe))
        + sum(1 for d in all_dirs if not _reuse_dir(d))
    )
    _progress_lock = threading.Lock()
    _progress_done = [0]
    _reported_paths: set[str] = set()

    def _on_node(t: SummaryTarget) -> None:
        if progress is None or progress_total == 0:
            return
        # A symbol target reports its OWNING FILE — the orientation unit the
        # progress display tracks. A body-only edit re-summarises symbols while
        # the file node is reused, so the file is reportable ONLY via its symbols;
        # normalise to the file's kind/level. Dedup by path so a file stale in
        # BOTH the symbol wave and the file wave is counted (and shown) once.
        if t.level == "symbol":
            fe = index.files.get(t.path)
            path, kind, level = t.path, (fe.kind if fe else t.kind), "file"
        else:
            path, kind, level = t.path, t.kind, t.level
        with _progress_lock:
            if path in _reported_paths:
                return
            _reported_paths.add(path)
            _progress_done[0] += 1
            done = _progress_done[0]
            try:
                progress(path, kind, done, progress_total, level)
            except Exception as exc:  # noqa: BLE001 — a progress hiccup never breaks the build
                logger.debug("code_index: progress callback failed: %s", exc)

    node_progress = _on_node if progress is not None else None

    # The default LLM summariser reports each file the instant its OWN group's
    # call returns (per-file live updates, including a symbol-only group); an
    # INJECTED summariser is opaque, so for it the wave fires node_progress after
    # assignment instead. Passing the emitter down exactly ONE of the two paths —
    # combined with dedup-by-path in _on_node — keeps every file reported once.
    if summarizer is not None:
        summ = summarizer
        wave_on_node = node_progress
    else:
        summ = _make_llm_summarizer(
            project_root, cfg.max_concurrency, on_node=node_progress
        )
        wave_on_node = None

    # --- Pass 2: per BATCH of STALE files, summarise bottom-up, then flush ---
    # --- one batch's cross-file LLM work, one checkpoint write. ---
    # Files are independent (a file node depends only on its OWN symbols), so a
    # batch of up to max_concurrency files is summarised together: one symbol wave
    # then one file wave across the whole batch, each wave fanning its per-file
    # groups out concurrently inside the summariser. Batches are formed over the
    # STALE files ONLY — the files that actually need (re)summarisation — never
    # over positional slices of the full sorted list: on an incremental rebuild
    # the touched files are scattered across sort order, so a positional batch
    # would hold at most one stale file and the summariser would run serially
    # (workers=1). Batching stale-only files keeps up to max_concurrency LLM
    # calls genuinely in flight on the commit-time incremental path, not only on a
    # --force full rebuild. symbols-then-file order stays strictly bottom-up
    # (every file target is built only after the batch's symbol wave has assigned
    # its symbol summaries). After Pass 1 every other node already carries its
    # reused summary, so each per-batch flush is still a complete md and a proper
    # superset of the prior one; a crash loses at most the one batch in flight, and
    # the next build resumes from the partial md — the incremental/断点恢复
    # semantics of the old per-file flush are preserved, only the flush granularity
    # coarsens from one file to one batch.
    stale_files = [
        index.files[r]
        for r in sorted(index.files)
        if _file_needs_work(index.files[r])
    ]
    batch_size = max(1, cfg.max_concurrency)
    for start in range(0, len(stale_files), batch_size):
        batch = stale_files[start : start + batch_size]

        sym_targets: List[SummaryTarget] = []
        for fe in batch:
            sym_targets.extend(
                _make_target(fe, sym, fe.abs_path)
                for sym in fe.symbols
                if not _reuse(fe.symbol_id(sym), sym.sha256)
            )
        if sym_targets:
            # A file whose ONLY stale content is a symbol body (its file node is
            # reused) surfaces to the progress display solely through this wave —
            # so it must carry the emitter too. _on_node rolls each file's stale
            # symbols up to ONE per-file report and dedups against the file wave.
            _summarize_wave(summ, sym_targets, index, on_node=wave_on_node)

        file_targets = [
            _make_file_target(fe)
            for fe in batch
            if fe.kind != "binary" and not _reuse_file(fe)
        ]
        if file_targets:
            _summarize_wave(summ, file_targets, index, on_node=wave_on_node)

        _write_md(project_root, index)

    # --- Pass 3: directories, deepest level first (all files now summarised). ---
    for depth in sorted({_depth(d) for d in all_dirs}, reverse=True):
        dir_targets = [
            _make_dir_target(dir_name, index, all_dirs)
            for dir_name in sorted(d for d in all_dirs if _depth(d) == depth)
            if not _reuse_dir(dir_name)
        ]
        _summarize_wave(summ, dir_targets, index, on_node=wave_on_node)

    _write_md(project_root, index)
    return index


def load_or_build(
    project_root: Path,
    summarizer: Optional[Summarizer] = None,
    force: bool = False,
    progress: Optional[ProgressCallback] = None,
) -> CodeIndex:
    """Lazy-incremental (re)build entry point: every call re-enumerates and
    refreshes only the changed nodes (or everything when ``force``).

    ``progress`` (default ``None``) is forwarded to :func:`build_index`; the
    commit step passes an emitter that streams per-node rebuild progress to the
    running flow's web console (see ``chat_history.record_index_progress``)."""
    return build_index(
        project_root, summarizer=summarizer, force=force, progress=progress
    )
