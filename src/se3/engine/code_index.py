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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Optional

from ..config import CodeIndexConfig, load_code_index_config
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
            line, _fp = _split_fp(raw)
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

# Trailing embedded fingerprint comment, e.g. ``<!--#a1b2c3d4e5f6a7b8-->``.
_FP_COMMENT_RE = re.compile(r"\s*<!--#([0-9a-f]{1,%d})-->\s*$" % _FP_LEN)

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


def _split_fp(line: str) -> tuple[str, Optional[str]]:
    """Split a rendered md line into ``(line_without_fp, fingerprint_or_None)``.

    The embedded fingerprint comment is stripped before any heading/bullet regex
    runs, so the summary capture never swallows it and the fingerprint is parsed
    out separately.
    """
    m = _FP_COMMENT_RE.search(line)
    if m:
        return line[: m.start()], m.group(1)
    return line, None


def _fp_comment(fp: str) -> str:
    return f"<!--#{fp}-->"


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
            head += f" {_fp_comment(dfp)}"
        lines.append(head)
        lines.append("")
        for fe in sorted(files_by_dir.get(dir_name, []), key=lambda f: f.path):
            fhead = f"### `{fe.path}` ({fe.kind})"
            if fe.summary:
                fhead += f" — {fe.summary}"
            ffp = _fp(fe.fingerprint.sha256)
            if fe.summary and ffp:
                fhead += f" {_fp_comment(ffp)}"
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


def _parse_md(md_text: str) -> tuple[Dict[str, str], Dict[str, str]]:
    """Parse the authoritative md into ``(summaries, fingerprints)``.

    Both maps are keyed by node id: a directory key (``"src/"``), a file relpath,
    or ``relpath::local_id`` for a symbol. ``summaries`` holds the
    human-correctable one-liners reused for unchanged nodes; ``fingerprints``
    holds each node's embedded content fingerprint, so a rebuild can decide
    staleness from the committed md alone (no out-of-band cache).
    """
    summaries: Dict[str, str] = {}
    fingerprints: Dict[str, str] = {}
    cur_path: Optional[str] = None
    for raw in md_text.splitlines():
        line, fp = _split_fp(raw)
        dh = _MD_DIR_HEADING_RE.match(line)
        if dh:
            key = dh.group(1)
            if dh.group(2):
                summaries[key] = dh.group(2).strip()
            if fp:
                fingerprints[key] = fp
            cur_path = None
            continue
        fh = _MD_FILE_HEADING_RE.match(line)
        if fh:
            cur_path = fh.group(1)
            if fh.group(3):
                summaries[cur_path] = fh.group(3).strip()
            if fp:
                fingerprints[cur_path] = fp
            continue
        bullet = _MD_BULLET_RE.match(line)
        if bullet and cur_path is not None:
            _indent, local_id, _mid, summary = bullet.groups()
            sid = f"{cur_path}::{local_id}"
            if summary:
                summaries[sid] = summary.strip()
            if fp:
                fingerprints[sid] = fp
    return summaries, fingerprints


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


def _make_llm_summarizer(project_root: Path) -> Summarizer:
    """Construct the default LLM-backed summariser (lazy LLMCaller import).

    Batches targets per owning path into one ``LLMCaller.call`` each, asking for
    a JSON ``{id: summary}`` map. Any failure degrades to the heuristic summary
    for that batch so a build is never aborted by a flaky LLM call.
    """

    def _summarize(targets: List[SummaryTarget]) -> Dict[str, str]:
        from .llm_caller import LLMCaller

        result: Dict[str, str] = {}
        by_file: Dict[str, List[SummaryTarget]] = {}
        for t in targets:
            by_file.setdefault(t.path, []).append(t)

        caller = LLMCaller(project_root=project_root, step_type="code_index")
        for relpath, group in by_file.items():
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
            )
            try:
                raw = caller.call(prompt, json_mode="two_phase")
                parsed = json.loads(raw) if isinstance(raw, str) else {}
            except Exception as exc:  # noqa: BLE001 — never let a build crash
                logger.warning(
                    "code_index: LLM summary failed for %s: %s", relpath, exc
                )
                parsed = {}
            for t in group:
                val = parsed.get(t.id) if isinstance(parsed, dict) else None
                result[t.id] = (
                    _flatten_summary(val) if val else _heuristic_summary(t)
                )
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
    summ: Summarizer, targets: List[SummaryTarget], index: CodeIndex
) -> None:
    """Summarise one dependency wave and assign the results onto their nodes.

    Skips the summariser entirely for an empty wave, so a no-op rebuild (nothing
    changed at any level) never invokes the LLM.
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


def build_index(
    project_root: Path,
    summarizer: Optional[Summarizer] = None,
    force: bool = False,
    cfg: Optional[CodeIndexConfig] = None,
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
        return _build_index_locked(project_root, summarizer, force, cfg)


def _build_index_locked(
    project_root: Path,
    summarizer: Optional[Summarizer],
    force: bool,
    cfg: CodeIndexConfig,
) -> CodeIndex:
    """The body of :func:`build_index`, run while holding the build lock."""
    md_summaries: Dict[str, str] = {}
    md_fps: Dict[str, str] = {}
    if not force:
        mp = md_path(project_root)
        if mp.exists():
            try:
                md_summaries, md_fps = _parse_md(mp.read_text(encoding="utf-8"))
            except OSError:
                md_summaries, md_fps = {}, {}

    files = file_enum.enumerate_index_files(project_root, cfg.exclude)
    index = CodeIndex(project_root=Path(project_root))
    root = Path(project_root).resolve()
    summ = summarizer or _make_llm_summarizer(project_root)

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

    def _reuse(fp_key: str, cur_sha: str) -> bool:
        """A node is reusable when its current fingerprint matches the md's."""
        return (not force) and bool(cur_sha) and md_fps.get(fp_key) == _fp(cur_sha)

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
        elif _reuse(fe.path, fe.fingerprint.sha256):
            fe.summary = md_summaries.get(fe.path, "")
        for sym in fe.symbols:
            sid = fe.symbol_id(sym)
            if _reuse(sid, sym.sha256):
                sym.summary = md_summaries.get(sid, "")
    for dir_name in all_dirs:
        dfp = dir_fps.get(dir_name, "")
        if (not force) and dfp and md_fps.get(dir_name) == dfp:
            index.dir_summaries[dir_name] = md_summaries.get(dir_name, "")

    # --- Pass 2: per file, summarise the STALE nodes bottom-up, then flush ---
    # --- the md — one file's LLM work, one checkpoint write. ---
    # The file node depends only on its OWN symbols, so symbols-then-file per file
    # is strictly bottom-up. After Pass 1 every other node already carries its
    # reused summary, so each flush is a complete md; a crash loses at most the
    # one file in flight, and the next build resumes from the partial md.
    for relpath in sorted(index.files):
        fe = index.files[relpath]
        did_work = False

        sym_targets = [
            _make_target(fe, sym, fe.abs_path)
            for sym in fe.symbols
            if not _reuse(fe.symbol_id(sym), sym.sha256)
        ]
        if sym_targets:
            _summarize_wave(summ, sym_targets, index)
            did_work = True

        if fe.kind != "binary" and not _reuse(fe.path, fe.fingerprint.sha256):
            _summarize_wave(summ, [_make_file_target(fe)], index)
            did_work = True

        if did_work:
            _write_md(project_root, index)

    # --- Pass 3: directories, deepest level first (all files now summarised). ---
    for depth in sorted({_depth(d) for d in all_dirs}, reverse=True):
        dir_targets = [
            _make_dir_target(dir_name, index, all_dirs)
            for dir_name in sorted(d for d in all_dirs if _depth(d) == depth)
            if not (
                (not force)
                and dir_fps.get(dir_name)
                and md_fps.get(dir_name) == dir_fps.get(dir_name)
            )
        ]
        _summarize_wave(summ, dir_targets, index)

    _write_md(project_root, index)
    return index


def load_or_build(
    project_root: Path,
    summarizer: Optional[Summarizer] = None,
    force: bool = False,
) -> CodeIndex:
    """Lazy-incremental (re)build entry point: every call re-enumerates and
    refreshes only the changed nodes (or everything when ``force``)."""
    return build_index(project_root, summarizer=summarizer, force=force)
