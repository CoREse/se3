"""SE3 Spec command — read-only navigation of the spec index.

Exposes the item-level spec index (``src/se3/engine/spec_index.py``) to the LLM
and to humans through two read-only sub-commands, both of which first run the
incremental ``load_or_build`` reconciliation (mtime/size/sha256 check then a
targeted rebuild of any drifted spec) so the output is always current:

    se3 spec index [<spec> [<group>...]]   # size-bounded navigation views
    se3 spec show <spec>::<requirement>    # one Requirement's body + location

``index`` is the navigation layer: with no argument it renders the root view
(every spec + one-sentence locator + item count); with a spec name it renders
that spec's flat item index; trailing ``<group>`` path components drill into a
folded domain group or a ``pN`` pagination handle. The rendering itself
(deterministic greedy folding, ≤ ``index_render_threshold`` bytes) is done by
the shared ``spec_index_render`` module so the CLI output and analyze's
programmatic root injection come from one renderer.

``show`` is the storage-layer reader: it accepts ONLY a flat item logical
address ``<spec>::<requirement>`` and prints that single Requirement's body
together with its physical location (file path + 1-based inclusive line
interval). This is the *interface rejection* half of the item-identity
invariant (machine guarantee b): a group name, an intermediate node, or any
address without a ``::`` is rejected with a non-zero exit and a clear error, so
a navigation handle can never be mistaken for a selectable item.

Both commands are strictly read-only — they never write spec files (only the
gitignored index cache is touched by ``load_or_build``) and never invoke the
LLM.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import typer

app = typer.Typer(help="Read-only navigation of the spec index")


def get_project_root() -> Path:
    """Find project root by looking for .git directory or an SE3 config file."""
    from ..config import is_se3_project_root

    cwd = Path.cwd()
    for parent in [cwd] + list(cwd.parents):
        if (parent / ".git").exists():
            return parent
        if is_se3_project_root(parent):
            return parent
    return cwd


@app.command(name="index")
def index_cmd(
    spec: Optional[str] = typer.Argument(
        None,
        help="Spec name to view; omit for the root view of all specs.",
    ),
    group: Optional[List[str]] = typer.Argument(
        None,
        help=(
            "Optional multi-level group path drilling into a folded domain "
            "group or a 'pN' pagination handle."
        ),
    ),
):
    """Render a size-bounded navigation view of the spec index.

    Output is folded with the deterministic greedy algorithm so it stays within
    the configured ``spec_governance.index_render_threshold`` (default 16 KiB):
    the largest foldable unit is collapsed into a navigation handle first, ties
    broken lexicographically, recursing into domain sub-paths / ``pN`` pages
    until the whole view fits. Each output is self-describing — it states the
    exact command to read one item and to drill one handle.
    """
    from ..config import load_spec_governance_config
    from ..engine.spec_index import load_or_build
    from ..engine.spec_index_render import render_index

    project_root = get_project_root()
    # load_or_build performs the mtime/size/sha256 incremental check and a
    # targeted rebuild of any drifted spec, so the rendered view is always
    # the latest on-disk state.
    index = load_or_build(project_root)

    threshold = load_spec_governance_config(project_root).index_render_threshold
    group_path = list(group) if group else []

    output = render_index(
        index, spec=spec, group_path=group_path, threshold=threshold
    )
    # The renderer already terminates the view with a trailing newline; print
    # without adding another so the stdout (a tool result for the LLM) is exact.
    typer.echo(output, nl=False)


@app.command(name="show")
def show_cmd(
    address: str = typer.Argument(
        ...,
        help="Item logical address in the form <spec>::<requirement>.",
    ),
):
    """Print one Requirement's body and its physical location.

    Accepts ONLY a flat item address ``<spec>::<requirement>``. A group name, an
    intermediate node, or any address missing the ``::`` separator is rejected
    with a non-zero exit (the interface-rejection half of the item-identity
    invariant). On success the output contains the Requirement body and the
    physical location (file path + 1-based inclusive line interval), and the two
    are consistent by construction (the body is exactly those lines).
    """
    from ..engine.spec_index import load_or_build

    raw = (address or "").strip()
    if "::" not in raw:
        typer.echo(
            f"Error: '{raw}' is not an item address. "
            "Expected the flat form <spec>::<requirement> "
            "(a group/page handle is not a selectable item).",
            err=True,
        )
        raise typer.Exit(code=1)

    spec_name, _sep, requirement_name = raw.partition("::")
    spec_name = spec_name.strip()
    requirement_name = requirement_name.strip()
    if not spec_name or not requirement_name:
        typer.echo(
            f"Error: '{raw}' is not a well-formed item address. "
            "Expected the form <spec>::<requirement> with both parts non-empty.",
            err=True,
        )
        raise typer.Exit(code=1)

    project_root = get_project_root()
    # Incremental reconciliation so the resolved body is the latest on disk.
    index = load_or_build(project_root)

    resolved = index.resolve_item_location(spec_name, requirement_name)
    if resolved is None:
        # Distinguish "no such spec" from "no such item in spec" for clarity.
        if spec_name not in index.spec_metas:
            typer.echo(
                f"Error: no such spec '{spec_name}'. "
                "Run 'se3 spec index' to list available specs.",
                err=True,
            )
        else:
            typer.echo(
                f"Error: no such item '{raw}'. "
                f"Run 'se3 spec index {spec_name}' to list this spec's items.",
                err=True,
            )
        raise typer.Exit(code=1)

    spec_path, line_start, line_end, body = resolved
    typer.echo(f"# {spec_name}::{requirement_name}")
    typer.echo(f"# location: {spec_path}:{line_start}-{line_end}")
    typer.echo("")
    typer.echo(body)
