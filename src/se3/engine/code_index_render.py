"""Layered drill-down rendering of the code-index, mirroring spec_index's
root-view vs drill-in split.

Two views over the authoritative ``se3/code-index.md``:

- :func:`render_top_map` — the **root view**: one line per directory / file
  (file-level summary only, no symbols). This is what each flow step injects so
  the agent gets a whole-project orientation map without the full function-level
  tree blowing up the context window.
- :func:`render_path` — the **drill-in view**: the function/method-level detail
  for a single file (or the file one-liners under a directory), pulled on demand
  via ``se3 code-index show <path>``.

Both views read ONLY the md (via :meth:`CodeIndex.from_md`); the json memo is
never consulted for display — it exists solely to accelerate (re)building.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from .code_index import CodeIndex, DEGRADED_MARKER, FileEntry, md_path


def load_for_display(project_root: Path) -> Optional[CodeIndex]:
    """Reconstruct a render-only index from the authoritative md on disk.

    Returns ``None`` when ``se3/code-index.md`` does not exist yet (no build has
    run). Reads only the md — never the json cache.
    """
    path = md_path(project_root)
    if not path.exists():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return CodeIndex.from_md(project_root, text)


def _dir_of(relpath: str) -> str:
    if "/" in relpath:
        return relpath.rsplit("/", 1)[0] + "/"
    return "(root)"


def render_top_map(index: CodeIndex) -> str:
    """Render the root view: each directory group with its files' one-liners.

    Each directory/package heading carries its own one-line summary (the level
    above files) so the orientation map is zoomable at the dir level too, not
    only at the file/symbol levels. Symbols (functions/methods) are intentionally
    omitted — this is the map injected on every step, kept small on purpose.
    """
    groups: Dict[str, List[FileEntry]] = {}
    for relpath in sorted(index.files):
        groups.setdefault(_dir_of(relpath), []).append(index.files[relpath])

    lines: List[str] = ["# Code Index (top map)", ""]
    for dir_name in sorted(groups):
        head = f"## `{dir_name}`"
        dir_summary = index.dir_summaries.get(dir_name, "")
        if dir_summary:
            head += f" — {dir_summary}"
        lines.append(head)
        for fe in sorted(groups[dir_name], key=lambda f: f.path):
            line = f"- `{fe.path}`"
            if fe.kind:
                line += f" ({fe.kind})"
            if fe.summary:
                line += f" — {fe.summary}"
            lines.append(line)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


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


def render_path(index: CodeIndex, path: str) -> str:
    """Render the drill-in view for *path*.

    - When *path* is an indexed file → that file plus its full symbol tree
      (function/method level).
    - When *path* is a directory prefix → the file one-liners beneath it.
    - When nothing matches → a short not-found note.
    """
    norm = path.replace("\\", "/").rstrip("/")

    if norm in index.files:
        return "\n".join(_render_file_detail(index.files[norm])).rstrip() + "\n"

    prefix = norm + "/"
    under = [
        index.files[rel]
        for rel in sorted(index.files)
        if rel == norm or rel.startswith(prefix)
    ]
    if under:
        lines = [f"## `{norm}/`", ""]
        for fe in under:
            line = f"- `{fe.path}` ({fe.kind})"
            if fe.summary:
                line += f" — {fe.summary}"
            lines.append(line)
        return "\n".join(lines).rstrip() + "\n"

    return f"No code-index entry found for path: {path}\n"
