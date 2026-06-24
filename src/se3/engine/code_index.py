"""Code Index — deterministic structure map of the project, summarised per symbol.

The code-index is a *logical structure map* of the project: the structure is
enumerated **deterministically** (a gitignore-respecting file walk via
``file_enum`` + AST / natural-structure extraction), and a one-sentence summary
for every node is produced by an **LLM** and rendered into the authoritative
``se3/code-index.md`` (committed to git, human-reviewable / human-correctable).

Two physical files realise the one logical subsystem:

- ``se3/code-index.md`` — the **authoritative product**: the map itself
  (dir → file → class → function/method, each with a one-line summary). It is
  what humans review and where a hand-correction of a mis-summary durably lands.
  Rendering reads only this file; the json is never consulted for display.
- ``se3/cache/code-index.json`` — a **volatile memo cache** (gitignored): per
  symbol it stores the content fingerprint (mtime+size+sha256) and that
  fingerprint's summary. Its sole job is to let a re-build decide which symbols
  changed so only those are re-summarised by the LLM; unchanged symbols reuse
  the summary already in the md (preserving human corrections). It participates
  in NO guarding/validation — pure performance.

Completeness is a property of the deterministic enumerator, not of LLM diligence:
the LLM only summarises the symbols the extractor hands it and never decides who
is included, so a mis-summary never removes a symbol from the map and the LLM has
no opportunity to drop one. Each build re-enumerates from scratch (new symbols
appear, deleted ones are pruned, unchanged ones reuse the cached summary), so the
"map is complete and current for the present symbol set" invariant holds every
build.

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
import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from ..config import CodeIndexConfig, load_code_index_config
from . import file_enum

logger = logging.getLogger(__name__)

# Cache schema version. A cached json whose ``version`` differs is treated as a
# load miss (every symbol re-summarised), so an older memo is never mis-applied.
CACHE_VERSION = 1

_CACHE_REL_PATH = Path("se3") / "cache" / "code-index.json"
_MD_REL_PATH = Path("se3") / "code-index.md"

# Marker rendered on a degraded chunk line so it is unmistakable that the chunk
# boundary is a mechanical line/byte cut with no semantic meaning.
DEGRADED_MARKER = "[degraded:chunk]"

# Max characters of a symbol's own content fed to the LLM summariser, bounding
# prompt size for very large functions / files.
_SUMMARY_CONTENT_CAP = 6000

# File extensions that get structural extraction.
_PY_EXTS = {".py", ".pyi"}
_MD_EXTS = {".md", ".markdown"}
_YAML_EXTS = {".yaml", ".yml"}
_JSON_EXTS = {".json"}


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Fingerprint:
    """Content fingerprint of one symbol (or whole file for the file node).

    ``mtime``/``size`` are the file-level cheap signals (parity with the
    spec-index scheme); ``sha256`` is the authoritative change signal, computed
    over the symbol's **own** content segment so that editing one function does
    not invalidate its unchanged siblings.
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


@dataclass
class FileEntry:
    """A single enumerated file plus its extracted symbols."""

    path: str                # project-relative POSIX path
    kind: str                # python | markdown | yaml | json | text | binary
    fingerprint: Fingerprint  # whole-file fingerprint (used for the file node)
    summary: str = ""        # file-level one-line summary
    symbols: List[Symbol] = field(default_factory=list)

    def symbol_id(self, sym: Symbol) -> str:
        return f"{self.path}::{sym.local_id}"


@dataclass
class CodeIndex:
    """The in-memory structure map: ``relpath -> FileEntry``."""

    project_root: Path
    files: Dict[str, FileEntry] = field(default_factory=dict)

    # -- reconstruct from the authoritative md (render-only consumers) ------

    @classmethod
    def from_md(cls, project_root: Path, md_text: str) -> "CodeIndex":
        """Reconstruct a (render-sufficient) index from the authoritative md.

        This reads ONLY the md (never the json), so render-only callers honour
        the "display reads md, json is touched only at (re)build" contract. The
        reconstructed entries carry the structure (path/kind/name/depth) and
        summaries needed for rendering; fingerprints are not recoverable from md
        and are left zeroed (rendering never needs them).
        """
        index = cls(project_root=project_root)
        cur: Optional[FileEntry] = None
        for line in md_text.splitlines():
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

# ``### `path` (kind) — summary``  (kind + summary optional)
_MD_FILE_HEADING_RE = re.compile(
    r"^###\s+`([^`]+)`(?:\s+\(([^)]*)\))?(?:\s+—\s+(.*))?$"
)
# ``  - `local_id` <middle> — summary``  (indent captured for depth)
_MD_BULLET_RE = re.compile(
    r"^( *)-\s+`([^`]+)`(.*?)(?:\s+—\s+(.*))?$"
)
_MD_BULLET_KIND_RE = re.compile(r"\(([^)]*)\)")


# ---------------------------------------------------------------------------
# Summary target + summariser type
# ---------------------------------------------------------------------------

@dataclass
class SummaryTarget:
    """A node that needs an LLM summary (changed or new)."""

    id: str          # full id: ``relpath`` (file node) or ``relpath::local_id``
    path: str        # owning file relpath
    kind: str
    name: str
    content: str     # the node's own source / segment (already truncated)
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
        )

    try:
        data = path.read_bytes()
    except OSError as exc:
        logger.warning("code_index: failed to read %s: %s", relpath, exc)
        return FileEntry(
            path=relpath, kind="binary", fingerprint=Fingerprint(mtime, size, "")
        )

    file_sha = _sha256_prefix(data)
    text = data.decode("utf-8", errors="replace")
    kind = _file_kind(path)

    symbols = _extract_structure(path, text)
    if not symbols and is_degrade_eligible(text, bool(symbols), cfg):
        symbols = _chunk_degraded(text, cfg)

    return FileEntry(
        path=relpath,
        kind=kind,
        fingerprint=Fingerprint(mtime, size, file_sha),
        symbols=symbols,
    )


# ---------------------------------------------------------------------------
# Cache (json memo) load / save
# ---------------------------------------------------------------------------

def cache_path(project_root: Path) -> Path:
    return Path(project_root) / _CACHE_REL_PATH


def md_path(project_root: Path) -> Path:
    return Path(project_root) / _MD_REL_PATH


def _load_cache(project_root: Path) -> Dict[str, dict]:
    """Load the per-file memo from the json cache. Returns ``{relpath: entry}``.

    A missing file, corrupt JSON, or version mismatch yields an empty memo (the
    build then re-summarises everything) — the cache never blocks or guards.
    """
    path = cache_path(project_root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("code_index: failed to load cache %s: %s", path, exc)
        return {}
    if data.get("version") != CACHE_VERSION:
        logger.info("code_index: cache version mismatch; ignoring memo.")
        return {}
    files = data.get("files")
    return files if isinstance(files, dict) else {}


def _save_cache(project_root: Path, index: CodeIndex) -> Path:
    """Atomically write the json memo (fingerprint + summary per node)."""
    path = cache_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    files_payload: Dict[str, dict] = {}
    for relpath, fe in index.files.items():
        files_payload[relpath] = {
            "file": {
                "mtime": fe.fingerprint.mtime,
                "size": fe.fingerprint.size,
                "sha256": fe.fingerprint.sha256,
                "summary": fe.summary,
            },
            "symbols": {
                sym.local_id: {"sha256": sym.sha256, "summary": sym.summary}
                for sym in fe.symbols
            },
        }
    payload = {"version": CACHE_VERSION, "files": files_payload}
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)
    return path


# ---------------------------------------------------------------------------
# Markdown rendering (authoritative product) — full tree
# ---------------------------------------------------------------------------

def _dir_of(relpath: str) -> str:
    if "/" in relpath:
        return relpath.rsplit("/", 1)[0] + "/"
    return "(root)"


def render_full(index: CodeIndex) -> str:
    """Render the complete authoritative map (files + all symbols)."""
    lines: List[str] = ["# Code Index", ""]
    groups: Dict[str, List[FileEntry]] = {}
    for relpath in sorted(index.files):
        groups.setdefault(_dir_of(relpath), []).append(index.files[relpath])
    for dir_name in sorted(groups):
        lines.append(f"## `{dir_name}`")
        lines.append("")
        for fe in sorted(groups[dir_name], key=lambda f: f.path):
            summary = fe.summary or ""
            head = f"### `{fe.path}` ({fe.kind})"
            if summary:
                head += f" — {summary}"
            lines.append(head)
            for sym in fe.symbols:
                indent = "  " * sym.depth
                marker = f" {DEGRADED_MARKER}" if sym.degraded else ""
                bullet = f"{indent}- `{sym.local_id}` ({sym.kind}){marker}"
                if sym.summary:
                    bullet += f" — {sym.summary}"
                lines.append(bullet)
            lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _write_md(project_root: Path, index: CodeIndex) -> Path:
    path = md_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".md.tmp")
    tmp.write_text(render_full(index), encoding="utf-8")
    os.replace(tmp, path)
    return path


def _parse_md_summaries(md_text: str) -> Dict[str, str]:
    """Parse the authoritative md into ``{id: summary}`` (id = relpath or
    ``relpath::local_id``). Holds the human-correctable summaries reused for
    unchanged symbols."""
    out: Dict[str, str] = {}
    cur_path: Optional[str] = None
    for line in md_text.splitlines():
        fh = _MD_FILE_HEADING_RE.match(line)
        if fh:
            cur_path = fh.group(1)
            if fh.group(3):
                out[cur_path] = fh.group(3).strip()
            continue
        bullet = _MD_BULLET_RE.match(line)
        if bullet and cur_path is not None:
            _indent, local_id, _mid, summary = bullet.groups()
            if summary:
                out[f"{cur_path}::{local_id}"] = summary.strip()
    return out


# ---------------------------------------------------------------------------
# Default LLM summariser
# ---------------------------------------------------------------------------

def _heuristic_summary(target: SummaryTarget) -> str:
    """Deterministic fallback summary used when no LLM is available or a call
    fails — keeps a build from ever crashing and a node from ever being summary-
    less. Honest about being a placeholder."""
    if target.degraded:
        return f"degraded chunk of {target.path} (boundary not semantic)"
    return f"{target.kind} {target.name}"


def _make_llm_summarizer(project_root: Path) -> Summarizer:
    """Construct the default LLM-backed summariser (lazy LLMCaller import).

    Batches targets per owning file into one ``LLMCaller.call`` each, asking for
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
                "(orientation, not implementation detail). Respond with a JSON "
                "object mapping each node id to its one-sentence summary.\n\n"
                f"File: {relpath}\n\nNodes:\n{listing}"
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
                    str(val).strip() if val else _heuristic_summary(t)
                )
        return result

    return _summarize


# ---------------------------------------------------------------------------
# Build orchestration
# ---------------------------------------------------------------------------

def _make_target(
    fe: FileEntry, sym: Optional[Symbol], path: Path
) -> SummaryTarget:
    """Build the summary target for a file node (sym=None) or a symbol."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        text = ""
    lines = text.splitlines()
    if sym is None:
        return SummaryTarget(
            id=fe.path, path=fe.path, kind=fe.kind, name=fe.path,
            content=text[:_SUMMARY_CONTENT_CAP],
        )
    seg = _slice_lines(lines, sym.line_start, sym.line_end) if sym.line_start else ""
    return SummaryTarget(
        id=fe.symbol_id(sym), path=fe.path, kind=sym.kind, name=sym.name,
        content=seg[:_SUMMARY_CONTENT_CAP], degraded=sym.degraded,
    )


def build_index(
    project_root: Path,
    summarizer: Optional[Summarizer] = None,
    force: bool = False,
    cfg: Optional[CodeIndexConfig] = None,
) -> CodeIndex:
    """Enumerate the project, extract structure, summarise changed nodes, and
    write both physical files. The single (re)build entry point.

    Incremental: a node whose content fingerprint matches the json memo reuses
    its md summary (preserving human corrections) and is NOT re-summarised. With
    ``force=True`` the memo and md are ignored and every node is re-summarised
    from scratch.
    """
    project_root = Path(project_root)
    cfg = cfg or load_code_index_config(project_root)

    cache = {} if force else _load_cache(project_root)
    md_summaries: Dict[str, str] = {}
    if not force:
        mp = md_path(project_root)
        if mp.exists():
            try:
                md_summaries = _parse_md_summaries(mp.read_text(encoding="utf-8"))
            except OSError:
                md_summaries = {}

    files = file_enum.enumerate_index_files(project_root, cfg.exclude)

    index = CodeIndex(project_root=project_root)
    targets: List[SummaryTarget] = []
    root = Path(project_root).resolve()

    for abs_path in files:
        try:
            relpath = abs_path.resolve().relative_to(root).as_posix()
        except ValueError:
            relpath = abs_path.name
        fe = _index_file(abs_path, relpath, cfg)
        index.files[relpath] = fe

        cached_file = cache.get(relpath, {}) if isinstance(cache, dict) else {}
        cached_file_meta = cached_file.get("file", {}) if isinstance(cached_file, dict) else {}
        cached_syms = cached_file.get("symbols", {}) if isinstance(cached_file, dict) else {}

        # --- file-level node summary ---
        if fe.kind == "binary":
            fe.summary = "(binary file — file-level entry only)"
        else:
            prev_sha = cached_file_meta.get("sha256")
            if not force and prev_sha == fe.fingerprint.sha256 and fe.fingerprint.sha256:
                fe.summary = md_summaries.get(
                    fe.path, cached_file_meta.get("summary", "")
                )
            else:
                targets.append(_make_target(fe, None, abs_path))

        # --- per-symbol summaries ---
        for sym in fe.symbols:
            prev = cached_syms.get(sym.local_id, {}) if isinstance(cached_syms, dict) else {}
            prev_sha = prev.get("sha256") if isinstance(prev, dict) else None
            if not force and prev_sha == sym.sha256 and sym.sha256:
                sym.summary = md_summaries.get(
                    fe.symbol_id(sym), prev.get("summary", "")
                )
            else:
                targets.append(_make_target(fe, sym, abs_path))

    # --- summarise the changed/new nodes ---
    if targets:
        summ = summarizer or _make_llm_summarizer(project_root)
        produced = summ(targets)
        for t in targets:
            value = produced.get(t.id) if isinstance(produced, dict) else None
            value = str(value).strip() if value else _heuristic_summary(t)
            if "::" in t.id and t.id.split("::", 1)[0] == t.path:
                fe = index.files[t.path]
                local = t.id.split("::", 1)[1]
                for sym in fe.symbols:
                    if sym.local_id == local:
                        sym.summary = value
                        break
            else:
                index.files[t.id].summary = value

    _write_md(project_root, index)
    _save_cache(project_root, index)
    return index


def load_or_build(
    project_root: Path,
    summarizer: Optional[Summarizer] = None,
    force: bool = False,
) -> CodeIndex:
    """Lazy-incremental (re)build entry point, mirroring ``spec_index``'s
    ``load_or_build``: every call re-enumerates and refreshes only the changed
    nodes (or everything when ``force``)."""
    return build_index(project_root, summarizer=summarizer, force=force)
