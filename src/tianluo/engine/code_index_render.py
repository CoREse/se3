"""Display rendering of the code-index — adaptive root view vs literal drill-in.

Two display views over the authoritative ``tianluo/code-index.md``:

- :func:`render_adaptive` — the **root view**: a zoomable directory tree expanded
  to a byte budget. The whole top level is shown collapsed (one line per
  directory / root file); within the auto-detected *primary roots* (the
  code-bearing top-level directories) the tree is drilled deeper, level by level,
  greedily, as long as the rendered map stays under the budget. This is what each
  flow step injects, so the orientation map is bounded no matter how large the
  project — a 100k-file tree and a 10-file tree both fit the same budget. The
  budget naturally stops expansion at directory granularity (file-level for one
  big tree already dwarfs a small budget), which is the right altitude for an
  always-injected map; function/method detail is one ``luo code-index show`` away.
- :func:`render_path` — the **literal drill-in view**: exactly one level at the
  given path — a directory's immediate children (subdirs collapsed + files), or a
  file's full function/method tree. Deterministic and predictable: it never
  auto-expands. ``path=""`` renders the literal root level (top-level dirs +
  root files, one level).

Both views read ONLY the md (via :meth:`CodeIndex.from_md`).
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from . import code_index
from .code_index import CodeIndex, DEGRADED_MARKER, FileEntry, ROOT_DIR, md_path


def load_for_display(project_root: Path) -> Optional[CodeIndex]:
    """Reconstruct a render-only index from the authoritative md on disk.

    Returns ``None`` when ``tianluo/code-index.md`` does not exist yet (no build has
    run). Reads only the md.
    """
    path = md_path(project_root)
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return CodeIndex.from_md(project_root, text)


# ---------------------------------------------------------------------------
# Primary-root auto-detection + subtree sizing
# ---------------------------------------------------------------------------

# File kinds that mark a top-level directory as a "code root" worth expanding.
_CODE_KINDS = {"python"}


def _top_of(dirkey: str) -> str:
    """The top-level ancestor directory key of *dirkey* (itself if top-level)."""
    if dirkey == ROOT_DIR:
        return ROOT_DIR
    return dirkey.split("/", 1)[0] + "/"


def _auto_primary_roots(index: CodeIndex, all_dirs: set) -> set:
    """Auto-detect the code-bearing top-level directories.

    A top-level directory is a primary root when its subtree contains any code
    file (so ``src/`` and ``tests/`` qualify, while a pure ``docs/`` does not).
    The framework makes NO assumption that code lives in ``src/`` — even this
    project keeps more code under ``tests/`` than ``src/``. When nothing
    qualifies (e.g. a docs-only repo), every top-level directory is a primary
    root so the budget still has something to expand.
    """
    roots: set = set()
    for rel, fe in index.files.items():
        if fe.kind in _CODE_KINDS and "/" in rel:
            roots.add(rel.split("/", 1)[0] + "/")
    if not roots:
        roots = set(code_index._child_dirs(ROOT_DIR, all_dirs))
    return roots


def _subtree_file_counts(files, all_dirs: set) -> dict:
    """Files-in-subtree count for EVERY directory, in one pass.

    Each file increments every one of its ancestor directories, so the whole map
    is built in O(files × depth) instead of O(dirs × files) — avoiding a
    per-directory ``startswith`` scan inside the expansion loop's sort key.
    """
    counts = {d: 0 for d in all_dirs}
    for rel in files:
        d = code_index._dir_of(rel)
        while d is not None:
            counts[d] = counts.get(d, 0) + 1
            d = code_index._parent_dir(d)
    return counts


# ---------------------------------------------------------------------------
# Adaptive root view (byte-budgeted zoomable tree)
# ---------------------------------------------------------------------------

def _render_view(index: CodeIndex, all_dirs: set, expanded: set) -> str:
    """Render the directory tree, descending only into *expanded* directories.

    A collapsed child directory is one bullet line (its own summary); an expanded
    one shows its children indented beneath it. Files appear when their parent
    directory is expanded.
    """
    lines: List[str] = ["# Code Index (map)", ""]

    def emit(dirkey: str, depth: int) -> None:
        indent = "  " * depth
        for sub in code_index._child_dirs(dirkey, all_dirs):
            summary = index.dir_summaries.get(sub, "")
            line = f"{indent}- `{sub}`"
            if summary:
                line += f" — {summary}"
            lines.append(line)
            if sub in expanded:
                emit(sub, depth + 1)
        for rel in code_index._child_files(dirkey, index.files):
            fe = index.files[rel]
            line = f"{indent}- `{rel}`"
            if fe.kind:
                line += f" ({fe.kind})"
            if fe.summary:
                line += f" — {fe.summary}"
            lines.append(line)

    emit(ROOT_DIR, 0)
    return "\n".join(lines).rstrip() + "\n"


_VIEW_HEADER = "# Code Index (map)"


def _expansion_costs(index: CodeIndex, all_dirs: set) -> dict:
    """Byte cost of *expanding* each directory — the size of its immediate
    children block (collapsed subdir lines + file lines), at that directory's
    indent. The rendered view's total bytes equal a fixed base plus the sum of
    these costs over the expanded set (each expanded dir contributes exactly its
    children block, once), so the budget can be checked incrementally in O(1)
    per candidate instead of re-rendering and re-encoding the whole tree.
    """
    costs: dict = {}
    for dirkey in all_dirs:
        indent = "  " * code_index._depth(dirkey)
        total = 0
        for sub in code_index._child_dirs(dirkey, all_dirs):
            line = f"{indent}- `{sub}`"
            summary = index.dir_summaries.get(sub, "")
            if summary:
                line += f" — {summary}"
            total += len(line.encode("utf-8")) + 1  # +1 for the joining newline
        for rel in code_index._child_files(dirkey, index.files):
            fe = index.files[rel]
            line = f"{indent}- `{rel}`"
            if fe.kind:
                line += f" ({fe.kind})"
            if fe.summary:
                line += f" — {fe.summary}"
            total += len(line.encode("utf-8")) + 1
        costs[dirkey] = total
    return costs


def render_adaptive(
    index: CodeIndex,
    primary_roots: Optional[List[str]] = None,
    budget_bytes: int = 8192,
) -> str:
    """Render the byte-budgeted zoomable root view.

    The whole top level is always shown. Within the primary roots, the tree is
    expanded one frontier directory at a time — shallowest first, then by
    descending subtree size for determinism and to spend the budget on the
    code-dense trees first — committing each expansion only while the rendered
    map stays at or under *budget_bytes*. Expansion stops at the first frontier
    directory that would overflow, leaving the rest collapsed.

    The expansion loop runs in roughly O(dirs log dirs): subtree sizes and
    per-directory expansion byte costs are each precomputed once, so the budget
    check is O(1) per candidate rather than a full re-render.
    """
    files = index.files
    all_dirs = code_index._all_dir_keys(files)
    prim = set(primary_roots) if primary_roots else _auto_primary_roots(index, all_dirs)

    subtree = _subtree_file_counts(files, all_dirs)
    cost = _expansion_costs(index, all_dirs)
    # Base bytes = the header line + the blank line beneath it (see _render_view's
    # ``"\n".join([header, "", ...]).rstrip() + "\n"``: header + "\n\n" + body).
    base = len(_VIEW_HEADER.encode("utf-8")) + 2

    def under_primary(dirkey: str) -> bool:
        return _top_of(dirkey) in prim

    expanded: set = {ROOT_DIR}
    cur_bytes = base + cost.get(ROOT_DIR, 0)
    while True:
        frontier = [
            d
            for d in all_dirs
            if d not in expanded
            and code_index._parent_dir(d) in expanded
            and under_primary(d)
        ]
        if not frontier:
            break
        frontier.sort(key=lambda d: (code_index._depth(d), -subtree[d], d))
        grew = False
        for d in frontier:
            if cur_bytes + cost[d] <= budget_bytes:
                expanded.add(d)
                cur_bytes += cost[d]
                grew = True
                break
        if not grew:
            break
    return _render_view(index, all_dirs, expanded)


# ---------------------------------------------------------------------------
# Literal drill-in view (exactly one level)
# ---------------------------------------------------------------------------

def _render_file_detail(fe: FileEntry) -> List[str]:
    head = f"### `{fe.path}` ({fe.kind})"
    if fe.summary:
        head += f" — {fe.summary}"
    out = [head]
    for sym in fe.symbols:
        indent = "  " * sym.depth
        marker = f" {DEGRADED_MARKER}" if sym.degraded else ""
        bullet = f"{indent}- `{sym.local_id}` ({sym.kind}){marker}"
        if sym.summary:
            bullet += f" — {sym.summary}"
        out.append(bullet)
    return out


def _render_one_level(index: CodeIndex, dirkey: str) -> str:
    """Render exactly the immediate children of *dirkey* (subdirs + files)."""
    all_dirs = code_index._all_dir_keys(index.files)
    label = dirkey
    head = f"## `{label}`"
    dir_summary = index.dir_summaries.get(dirkey, "")
    if dir_summary:
        head += f" — {dir_summary}"
    lines: List[str] = [head, ""]
    for sub in code_index._child_dirs(dirkey, all_dirs):
        summary = index.dir_summaries.get(sub, "")
        line = f"- `{sub}`"
        if summary:
            line += f" — {summary}"
        lines.append(line)
    for rel in code_index._child_files(dirkey, index.files):
        fe = index.files[rel]
        line = f"- `{rel}` ({fe.kind})"
        if fe.summary:
            line += f" — {fe.summary}"
        lines.append(line)
    return "\n".join(lines).rstrip() + "\n"


def iter_search_lines(index: CodeIndex) -> List[str]:
    """Render every item in the map as one grep-able line.

    Produces one line per item — directory, file, and each in-file symbol — in
    the same bullet style as the ``index`` / ``show`` views:

    - directory  ``- `dirkey` — summary``
    - file       ``- `relpath` (kind) — summary``
    - symbol     ``- `relpath::local_id` (kind) — summary``

    A symbol's line carries its owning file's full path (``relpath::local_id``),
    which is exactly the context a raw ``grep tianluo/code-index.md`` cannot give: a
    bare symbol bullet in the md is indented under a file heading many lines
    away. Lines are built from the structured index, never passed through from
    the md text, so they never carry the ``<!--#...-->`` fingerprint comments.
    """
    all_dirs = code_index._all_dir_keys(index.files)
    lines: List[str] = []

    for dirkey in sorted(all_dirs):
        summary = index.dir_summaries.get(dirkey, "")
        line = f"- `{dirkey}`"
        if summary:
            line += f" — {summary}"
        lines.append(line)

    for rel in sorted(index.files):
        fe = index.files[rel]
        line = f"- `{rel}`"
        if fe.kind:
            line += f" ({fe.kind})"
        if fe.summary:
            line += f" — {fe.summary}"
        lines.append(line)
        for sym in fe.symbols:
            marker = f" {DEGRADED_MARKER}" if sym.degraded else ""
            sym_line = f"- `{fe.symbol_id(sym)}` ({sym.kind}){marker}"
            if sym.summary:
                sym_line += f" — {sym.summary}"
            lines.append(sym_line)

    return lines


def render_path(index: CodeIndex, path: str) -> str:
    """Render the literal drill-in view for *path* — exactly one level.

    - ``path=""`` (or ``"."`` / ``"/"``) → the literal root level (top-level
      directories collapsed + root files).
    - an indexed file → that file plus its full symbol tree (function/method
      level).
    - a directory → its immediate children only (subdirs collapsed + files); it
      does NOT recurse.
    - nothing matches → a short not-found note.
    """
    norm = path.replace("\\", "/").strip()
    if norm in ("", ".", "/"):
        return _render_one_level(index, ROOT_DIR)

    norm = norm.rstrip("/")
    if norm in index.files:
        return "\n".join(_render_file_detail(index.files[norm])).rstrip() + "\n"

    dirkey = norm + "/"
    if dirkey in code_index._all_dir_keys(index.files):
        return _render_one_level(index, dirkey)

    return f"No code-index entry found for path: {path}\n"
