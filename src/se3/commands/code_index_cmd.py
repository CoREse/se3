"""SE3 Code Index command — navigate the project's structure map.

Exposes the code-index (``src/se3/engine/code_index.py``) to humans and to the
LLM through a small command family that mirrors the role ``se3 spec`` plays for
specs (it takes over the per-module locator-navigation job the old base spec
carried):

    se3 code-index index [<path>]   # layered drill-down navigation views
    se3 code-index show <path>      # one file's full function/method detail
    se3 code-index rebuild [--force]# (re)build the map (incremental, or full)
    se3 code-index inspect          # summary stats of the on-disk map

The **authoritative product** the display commands read is the committed
``se3/code-index.md`` — the map itself (dir → file → class → function/method,
each with a one-line summary), where a human correction of a mis-summary durably
lands. ``index`` / ``show`` reconstruct a render-only index from that md alone
(via :func:`code_index_render.load_for_display`) and NEVER consult the gitignored
``se3/cache/code-index.json`` memo — that volatile cache exists solely to
accelerate the next ``rebuild`` (it lets the build decide which symbols changed),
participates in no display and no guarding.

``index`` with no argument renders the **root view** (one line per directory /
file, file-level summary only) — the same orientation map injected on every flow
step. With a ``<path>`` it drills in: a directory lists its files' one-liners, a
file shows its full function/method tree. ``show <path>`` is the dedicated
function-level reader for a single file.

``rebuild`` is the only writing command: it re-enumerates the code tree
(deterministically, respecting gitignore), re-summarises only the symbols whose
content fingerprint changed (``--force`` re-summarises everything), and writes
both physical files. Normal display goes through the lazy-incremental
``load_or_build`` path elsewhere; this command surfaces the explicit rebuild.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(help="Navigate the code-index structure map (reads se3/code-index.md)")


def get_project_root() -> Path:
    """Find project root by looking for a .git directory or an SE3 config file."""
    from ..config import is_se3_project_root

    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        if (parent / ".git").exists():
            return parent
        if is_se3_project_root(parent):
            return parent
    return cwd


_NOT_BUILT_HINT = (
    "No code-index found (se3/code-index.md does not exist yet).\n"
    "Build it first with: se3 code-index rebuild"
)


@app.command(name="index")
def index_cmd(
    path: Optional[str] = typer.Argument(
        None,
        help=(
            "Path to drill into; omit for the root view (every directory/file "
            "with its one-line summary). A directory lists its files; a file "
            "lists its functions/methods."
        ),
    ),
):
    """Render a layered navigation view of the code-index.

    Reads the authoritative ``se3/code-index.md`` (never the json memo cache).
    With no argument it renders the root view — one line per directory / file —
    which is the orientation map injected on every flow step. With a ``<path>``
    it drills down: a directory prefix lists the file one-liners beneath it, an
    indexed file shows its full function/method tree.
    """
    from ..engine import code_index_render

    project_root = get_project_root()
    index = code_index_render.load_for_display(project_root)
    if index is None:
        typer.echo(_NOT_BUILT_HINT, err=True)
        raise typer.Exit(code=1)

    if path:
        output = code_index_render.render_path(index, path)
    else:
        output = code_index_render.render_top_map(index)
    # The renderer terminates the view with a trailing newline; print without
    # adding another so the stdout (a tool result for the LLM) is exact.
    typer.echo(output, nl=False)


@app.command(name="show")
def show_cmd(
    path: str = typer.Argument(
        ...,
        help="Project-relative path of the file (or directory) to detail.",
    ),
):
    """Print one file's full function/method detail from the code-index.

    Reads the authoritative ``se3/code-index.md`` (never the json memo cache).
    For an indexed file this prints its file-level summary plus every
    class/function/method (and any degraded chunks) with their one-line
    summaries; for a directory prefix it lists the file one-liners beneath it.
    """
    from ..engine import code_index_render

    project_root = get_project_root()
    index = code_index_render.load_for_display(project_root)
    if index is None:
        typer.echo(_NOT_BUILT_HINT, err=True)
        raise typer.Exit(code=1)

    output = code_index_render.render_path(index, path)
    typer.echo(output, nl=False)


@app.command(name="rebuild")
def rebuild_cmd(
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help=(
            "Re-summarise every symbol from scratch, ignoring the json memo "
            "cache and any existing md summaries (including human corrections). "
            "Without this flag the rebuild is incremental: only symbols whose "
            "content fingerprint changed are re-summarised."
        ),
    ),
):
    """(Re)build the code-index, writing se3/code-index.md and the json cache.

    Re-enumerates the code tree deterministically (respecting gitignore), then
    summarises the changed symbols via the LLM. The incremental default reuses
    the md's existing (human-correctable) summaries for unchanged symbols;
    ``--force`` re-summarises everything. This is the same ``load_or_build``
    path that flow steps trigger lazily — surfaced here as an explicit command.
    """
    from ..engine import code_index

    project_root = get_project_root()
    mode = "full rebuild (--force)" if force else "incremental rebuild"
    typer.echo(f"Building code-index ({mode}) for {project_root} ...")
    index = code_index.load_or_build(project_root, force=force)

    file_count = len(index.files)
    symbol_count = sum(len(fe.symbols) for fe in index.files.values())
    md = code_index.md_path(project_root)
    cache = code_index.cache_path(project_root)
    typer.echo(
        f"Done. Indexed {file_count} file(s), {symbol_count} symbol(s).\n"
        f"  authoritative map: {md}\n"
        f"  memo cache:        {cache}"
    )


@app.command(name="inspect")
def inspect_cmd():
    """Show summary stats of the on-disk code-index map.

    Reads the authoritative ``se3/code-index.md`` only — file/symbol/degraded
    counts and a per-kind file breakdown — for a quick health check without
    dumping the whole map.
    """
    from ..engine import code_index, code_index_render

    project_root = get_project_root()
    index = code_index_render.load_for_display(project_root)
    if index is None:
        typer.echo(_NOT_BUILT_HINT, err=True)
        raise typer.Exit(code=1)

    file_count = len(index.files)
    symbol_count = sum(len(fe.symbols) for fe in index.files.values())
    degraded_count = sum(
        1 for fe in index.files.values() for sym in fe.symbols if sym.degraded
    )
    by_kind: dict[str, int] = {}
    for fe in index.files.values():
        by_kind[fe.kind] = by_kind.get(fe.kind, 0) + 1

    lines = [
        f"Code Index — {project_root}",
        f"  authoritative map: {code_index.md_path(project_root)}",
        f"  memo cache:        {code_index.cache_path(project_root)}",
        "",
        f"Files:    {file_count}",
        f"Symbols:  {symbol_count}",
        f"Degraded chunks: {degraded_count}",
    ]
    if by_kind:
        lines.append("")
        lines.append("Files by kind:")
        for kind in sorted(by_kind):
            lines.append(f"  {kind}: {by_kind[kind]}")
    typer.echo("\n".join(lines))
