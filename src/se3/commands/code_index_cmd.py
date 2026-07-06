"""SE3 Code Index command — navigate the project's structure map.

Exposes the code-index (``src/se3/engine/code_index.py``) to humans and to the
LLM through a small command family:

    se3 code-index                  # adaptive root view (budgeted zoomable tree)
    se3 code-index index [<path>]   # literal drill-in — exactly one level
    se3 code-index show <path>      # one file's full function/method detail
    se3 code-index search <pattern> # grep the map's item lines (regex; -i/-F/-m)
    se3 code-index rebuild [--force]# (re)build the map (incremental, or full)
    se3 code-index inspect          # summary stats of the on-disk map

The **authoritative product** the display commands read is the committed,
self-sufficient ``se3/code-index.md`` — the map itself (dir → subdir → … → file →
class → function, each with a one-line summary), where a human correction of a
mis-summary durably lands and where each node's content fingerprint is embedded.
There is no separate cache: ``index`` / ``show`` reconstruct a render-only index
from that md alone (via :func:`code_index_render.load_for_display`).

Bare ``se3 code-index`` renders the **adaptive root view** — a zoomable tree
expanded to a byte budget (the same map injected on every flow step). ``index``
is the **literal** navigator: it shows exactly one level at the given path — a
directory's immediate children, or a file's function/method tree — and ``index``
with no path shows the literal root level. ``show <path>`` is the dedicated
function-level reader for a single file.

``rebuild`` is the only writing command: it re-enumerates the code tree
(deterministically, respecting gitignore), re-summarises only the nodes whose
content fingerprint changed (``--force`` re-summarises everything), and writes
``se3/code-index.md``. Normal display goes through the lazy-incremental
``load_or_build`` path elsewhere; this command surfaces the explicit rebuild.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Optional

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


def _render_adaptive_map() -> None:
    """Render the code-index adaptive root view (budgeted zoomable tree).

    This is the bare ``se3 code-index`` invocation — the same orientation map
    injected on every flow step. Primary roots + byte budget come from the
    ``code_index`` config section.
    """
    from ..config import load_code_index_config
    from ..engine import code_index_render

    project_root = get_project_root()
    index = code_index_render.load_for_display(project_root)
    if index is None:
        typer.echo(_NOT_BUILT_HINT, err=True)
        raise typer.Exit(code=1)

    cfg = load_code_index_config(project_root)
    output = code_index_render.render_adaptive(
        index, cfg.primary_roots, cfg.view_budget_bytes
    )
    # The renderer terminates the view with a trailing newline; print without
    # adding another so the stdout (a tool result for the LLM) is exact.
    typer.echo(output, nl=False)


@app.callback(invoke_without_command=True)
def code_index_main(ctx: typer.Context):
    """Navigate the code-index structure map.

    Bare ``se3 code-index`` (no subcommand) renders the adaptive root view — a
    zoomable directory tree expanded to a byte budget, the primary navigation
    entry point. Subcommands drill in / rebuild / inspect: ``index [<path>]``
    shows exactly one literal level, ``show <path>`` details one file,
    ``rebuild`` (re)builds the map, ``inspect`` shows summary stats.
    """
    if ctx.invoked_subcommand is None:
        _render_adaptive_map()


@app.command(name="index")
def index_cmd(
    path: Optional[str] = typer.Argument(
        None,
        help=(
            "Path to drill into — shows exactly ONE literal level. A directory "
            "lists its immediate children (subdirs + files); a file lists its "
            "functions/methods. Omit (or pass an empty path) for the literal "
            "root level. For the budgeted zoomable map, run bare `se3 code-index`."
        ),
    ),
):
    """Render the literal drill-in view — exactly one level at *path*.

    Reads the authoritative ``se3/code-index.md``. Unlike the bare adaptive root
    view, this never auto-expands: a directory shows only its immediate children,
    a file shows its full function/method tree, and no argument shows the literal
    root level (top-level directories + root files, one level).
    """
    from ..engine import code_index_render

    project_root = get_project_root()
    index = code_index_render.load_for_display(project_root)
    if index is None:
        typer.echo(_NOT_BUILT_HINT, err=True)
        raise typer.Exit(code=1)

    output = code_index_render.render_path(index, path or "")
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

    Reads the authoritative ``se3/code-index.md`` (the single source of truth).
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


@app.command(name="search")
def search_cmd(
    pattern: str = typer.Argument(
        ...,
        help=(
            "Pattern to match against each item's rendered line. A Python regex "
            "by default (feels like `grep -E`), case-sensitive; use -F for a "
            "literal substring."
        ),
    ),
    ignore_case: bool = typer.Option(
        False, "-i", "--ignore-case", help="Case-insensitive matching (like grep -i)."
    ),
    fixed_strings: bool = typer.Option(
        False,
        "-F",
        "--fixed-strings",
        help="Match *pattern* as a literal substring, not a regex (like grep -F).",
    ),
    max_count: Optional[int] = typer.Option(
        None,
        "-m",
        "--max-count",
        help="Stop after N matches (like grep -m). Default: no limit.",
    ),
):
    """Grep the code-index item lines — a drop-in for `grep se3/code-index.md`.

    Reads the authoritative ``se3/code-index.md`` (render-only; never rebuilds)
    and matches *pattern* against the rendered single line of every item —
    directory, file, and each in-file symbol. Unlike a raw grep of the md, a
    matched symbol line carries its owning file's full path
    (``relpath::local_id``), and no fingerprint comments leak into the output.

    Grep-aligned semantics: *pattern* is a regex by default (case-sensitive);
    ``-i`` matches case-insensitively, ``-F`` treats it as a literal substring,
    and ``-m N`` caps the output at N matches. Exit code follows grep: 0 when at
    least one line matches, 1 when none do (2 on an invalid regex).
    """
    from ..engine import code_index_render

    project_root = get_project_root()
    index = code_index_render.load_for_display(project_root)
    if index is None:
        typer.echo(_NOT_BUILT_HINT, err=True)
        raise typer.Exit(code=1)

    matcher = _build_matcher(pattern, ignore_case=ignore_case, fixed_strings=fixed_strings)

    matches = 0
    for line in code_index_render.iter_search_lines(index):
        if matcher(line):
            typer.echo(line)
            matches += 1
            if max_count is not None and matches >= max_count:
                break

    if matches == 0:
        # grep's exit-code-1 "no matches" contract, so an agent can compose this
        # by exit status exactly as it would with grep.
        typer.echo(f"No code-index item matched: {pattern}", err=True)
        raise typer.Exit(code=1)


def _build_matcher(
    pattern: str, *, ignore_case: bool, fixed_strings: bool
) -> "Callable[[str], bool]":
    """Compile *pattern* into a per-line predicate honouring -i / -F.

    ``-F`` matches a literal substring (case-folded when ``-i``); otherwise the
    pattern is a Python regex, searched anywhere in the line. An invalid regex
    exits 2 (a usage error), mirroring grep's distinction between "no match" (1)
    and "bad pattern" (2).
    """
    if fixed_strings:
        needle = pattern.lower() if ignore_case else pattern
        if ignore_case:
            return lambda line: needle in line.lower()
        return lambda line: needle in line

    flags = re.IGNORECASE if ignore_case else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error as exc:
        typer.echo(f"Invalid regular expression: {exc}", err=True)
        raise typer.Exit(code=2)
    return lambda line: rx.search(line) is not None


@app.command(name="rebuild")
def rebuild_cmd(
    force: bool = typer.Option(
        False,
        "--force",
        "-f",
        help=(
            "Re-summarise every node from scratch, ignoring the fingerprints "
            "embedded in the existing md (including human corrections). Without "
            "this flag the rebuild is incremental: only nodes whose content "
            "fingerprint changed are re-summarised."
        ),
    ),
):
    """(Re)build the code-index, writing the authoritative se3/code-index.md.

    Re-enumerates the code tree deterministically (respecting gitignore), then
    summarises the changed nodes via the LLM, flushing the md periodically as a
    checkpoint. The incremental default reuses the md's existing
    (human-correctable) summaries for unchanged nodes; ``--force`` re-summarises
    everything. This is the same ``load_or_build`` path that flow steps trigger
    lazily — surfaced here as an explicit command.
    """
    from ..engine import code_index

    project_root = get_project_root()
    mode = "full rebuild (--force)" if force else "incremental rebuild"
    typer.echo(f"Building code-index ({mode}) for {project_root} ...")
    index = code_index.load_or_build(project_root, force=force)

    file_count = len(index.files)
    symbol_count = sum(len(fe.symbols) for fe in index.files.values())
    md = code_index.md_path(project_root)
    typer.echo(
        f"Done. Indexed {file_count} file(s), {symbol_count} symbol(s).\n"
        f"  authoritative map: {md}"
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
