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

``show`` output is also size-bounded by the same ``index_render_threshold`` the
navigation views obey: the context-boundedness invariant is "any single output
entering the LLM context is bounded", and a Requirement body is the unit of
``show``. The bound is measured in **UTF-8 bytes** (not Python characters) so a
Requirement full of multibyte text (e.g. CJK) cannot blow past the byte budget
threefold. A single Requirement should be ≤ 8 KiB (the writing discipline), but
until an oversized one is split it can exceed the threshold; in that case the
body is served in deterministic, byte-bounded **pages** addressed by
``se3 spec show <spec>::<requirement> --page <N>``. Each page never splits a
multibyte character and ends with a notice naming the exact command for the next
page, so the *complete* Requirement stays retrievable through bounded ``se3 spec``
stdout — the unified bounded information channel — without ever directly reading
the spec file, and a 40 KiB Requirement never dumps unbounded into the context.

Both commands are strictly read-only — they never write spec files (only the
gitignored index cache is touched by ``load_or_build``) and never invoke the
LLM.
"""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import List, Optional

import typer

app = typer.Typer(help="Read-only navigation of the spec index")

# The maximum number of UTF-8 bytes a single Unicode scalar can encode to. The
# byte-bounded ``show`` paginator must always be able to emit at least one whole
# character per page, so every per-page body budget is reserved to be at least
# this many bytes — otherwise a single multibyte character (e.g. a 3-byte CJK
# glyph) would have to either split (corrupting the encoding) or overrun the
# configured threshold. The ``show`` render floor and the bounded display-address
# budget both reserve this many bytes for the body so the accepted-minimum
# threshold can still render one whole character within the byte bound.
MAX_UTF8_CHAR_BYTES = 4


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
    page: int = typer.Option(
        1,
        "--page",
        "-p",
        help=(
            "1-based page number when an oversized Requirement body is served "
            "in deterministic byte-bounded pages."
        ),
    ),
):
    """Print one Requirement's body and its physical location.

    Accepts ONLY a flat item address ``<spec>::<requirement>``. A group name, an
    intermediate node, or any address missing the ``::`` separator is rejected
    with a non-zero exit (the interface-rejection half of the item-identity
    invariant). On success the output contains the Requirement body and the
    physical location (file path + 1-based inclusive line interval), and the two
    are consistent by construction (the body is exactly those lines).

    The output is bounded to ``index_render_threshold`` UTF-8 bytes. When a
    single Requirement exceeds that, its body is split into deterministic,
    byte-bounded pages; ``--page N`` selects one page and every non-final page
    names the command for the next, so the complete body stays retrievable
    through bounded ``se3 spec`` stdout without reading the spec file directly.
    """
    from ..config import load_spec_governance_config
    from ..engine.spec_index import load_or_build

    project_root = get_project_root()
    # Load the byte threshold up front so EVERY error path below — not only the
    # success path — can bound the user-supplied address it echoes. An error
    # response is just as much a CLI tool result entering the LLM context as a
    # success response, so a pathologically long address (or an out-of-range
    # page reusing the full address) must never push an error message past the
    # configured ``index_render_threshold``.
    threshold = load_spec_governance_config(project_root).index_render_threshold

    raw = (address or "").strip()
    if "::" not in raw:
        typer.echo(
            f"Error: '{_bound_for_error(raw, threshold)}' is not an item address. "
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
            f"Error: '{_bound_for_error(raw, threshold)}' is not a well-formed "
            "item address. Expected the form <spec>::<requirement> with both "
            "parts non-empty.",
            err=True,
        )
        raise typer.Exit(code=1)

    # Incremental reconciliation so the resolved body is the latest on disk.
    index = load_or_build(project_root)

    resolved = index.resolve_item_location(spec_name, requirement_name)
    if resolved is None:
        # Distinguish "no such spec" from "no such item in spec" for clarity.
        if spec_name not in index.spec_metas:
            typer.echo(
                f"Error: no such spec '{_bound_for_error(spec_name, threshold)}'. "
                "Run 'se3 spec index' to list available specs.",
                err=True,
            )
        else:
            typer.echo(
                f"Error: no such item '{_bound_for_error(raw, threshold)}'. "
                f"Run 'se3 spec index {_bound_for_error(spec_name, threshold)}' "
                "to list this spec's items.",
                err=True,
            )
        raise typer.Exit(code=1)

    spec_path, line_start, line_end, body = resolved

    # A bounded ``show`` page is, at minimum, an intact header + at least one body
    # byte + a paging-continuation notice. If the configured threshold is below
    # that irreducible floor, the page cannot be rendered within the bound. We do
    # NOT silently raise the threshold (which would emit more bytes than the
    # configured limit while still reporting success) — instead the infeasibly
    # small configuration is rejected explicitly so the operator fixes the config.
    floor = _show_render_threshold_floor(spec_path, line_start, line_end)
    if threshold < floor:
        typer.echo(
            f"Error: configured spec_governance.index_render_threshold "
            f"({threshold} bytes) is too small to render a single bounded "
            f"'se3 spec show' page for "
            f"'{_bound_for_error(f'{spec_name}::{requirement_name}', threshold)}', "
            f"which needs at least {floor} bytes (header + one body byte + paging "
            f"notice). Raise spec_governance.index_render_threshold to at least "
            f"{floor}.",
            err=True,
        )
        raise typer.Exit(code=1)

    address = f"{spec_name}::{requirement_name}"
    # The item address is echoed for readability (in the header and in the paging
    # notice's continuation command), but an arbitrarily long Requirement name
    # must never push the whole stdout past the byte threshold. Compute a single
    # *bounded* display address used in BOTH places: it is the full address when
    # it fits, and a truncated form only for a pathologically long name. The
    # authoritative physical location line (file path + line interval) is always
    # emitted intact, so the body stays retrievable even when the echoed address
    # is truncated (the user supplied the real address on the command line).
    display_address = _bounded_display_address(
        threshold, address, spec_path, line_start, line_end
    )
    # The header echoes the bounded (possibly truncated) display address for
    # readability, and the continuation command embeds the SAME bounded display
    # address so the whole ``show`` page stays within the byte threshold for every
    # address length. For a normal-length name the bounded address equals the full
    # address (the command is directly executable); only a pathologically long
    # name is truncated, and the notice then tells the LLM to re-use the exact
    # address it supplied — so paging is never severed and the bound is honoured.
    header = _build_header(display_address, spec_path, line_start, line_end)
    output = _render_bounded_show(
        body=body,
        header=header,
        threshold=threshold,
        notice_address=display_address,
        spec_path=spec_path,
        line_start=line_start,
        line_end=line_end,
        page=page,
    )
    if output is None:
        # Requested page is out of range — report the valid range and exit.
        total = _page_count(
            body, header, threshold, display_address, spec_path, line_start, line_end
        )
        typer.echo(
            f"Error: page {page} is out of range for "
            f"'{_bound_for_error(address, threshold)}'. "
            f"This Requirement body has {total} page(s); valid pages are "
            f"1-{total}.",
            err=True,
        )
        raise typer.Exit(code=1)
    # Build the exact stdout (a tool result for the LLM) and emit it with a single
    # trailing newline, matching the precise-output discipline of ``index``.
    typer.echo(output, nl=False)


def _utf8_byte_len(text: str) -> int:
    """Return the UTF-8 byte length of ``text`` (the unit the threshold uses)."""
    return len(text.encode("utf-8"))


def _take_bytes(text: str, budget: int) -> str:
    """Return the longest prefix of ``text`` encoding to ≤ ``budget`` UTF-8 bytes.

    The cut never falls inside a multibyte character: if the raw byte slice would
    split one, it is backed off to the previous whole-character boundary. With a
    non-positive budget the empty string is returned.
    """
    if budget <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= budget:
        return text
    cut = encoded[:budget]
    while cut:
        try:
            return cut.decode("utf-8")
        except UnicodeDecodeError:
            cut = cut[:-1]
    return ""


def _paginate_bytes(body: str, per_page_budget: int) -> List[str]:
    """Split ``body`` into pages each ≤ ``per_page_budget`` UTF-8 bytes.

    Splitting is deterministic and never falls inside a multibyte character.
    Callers in the ``show`` path always pass ``per_page_budget >=
    MAX_UTF8_CHAR_BYTES`` (guaranteed by the render-threshold floor and the
    bounded display-address reservation), so every page holds at least one whole
    character within the byte bound. The degenerate fallback below is a defensive
    backstop for a pathological budget smaller than one character: it emits a
    single character to guarantee termination, accepting that this lone page may
    exceed the budget — but accepted ``show`` configurations never reach it.
    """
    pages: List[str] = []
    rest = body
    while rest:
        prefix = _take_bytes(rest, per_page_budget)
        if not prefix:
            # Budget cannot fit a whole character (never happens on an accepted
            # ``show`` threshold); emit one to guarantee progress/termination.
            prefix = rest[0]
        pages.append(prefix)
        rest = rest[len(prefix):]
    return pages or [""]


def _bounded_address_for_display(address: str, max_bytes: int) -> str:
    """Return ``address`` truncated to ≤ ``max_bytes`` UTF-8 bytes for display.

    A pathologically long Requirement name (the address) must not blow the header
    echo past the byte threshold. When the address fits, it is returned verbatim;
    otherwise it is cut at a whole-character boundary and a truncation marker is
    appended (itself fitting within ``max_bytes``). This is the HEADER echo only —
    the paging notice's continuation command always embeds the FULL address so it
    stays executable. The user already supplied the real address on the command
    line, so paging still works even when the header echo is truncated.
    """
    if _utf8_byte_len(address) <= max_bytes:
        return address
    marker = "…[truncated]"
    marker_len = _utf8_byte_len(marker)
    if max_bytes <= marker_len:
        return _take_bytes(marker, max_bytes)
    return _take_bytes(address, max_bytes - marker_len) + marker


def _bound_for_error(text: str, threshold: int) -> str:
    """Bound a user-supplied address echoed inside an error message.

    Every ``se3 spec show`` error path interpolates the address the caller
    supplied (or one derived from it). Left unbounded, a pathologically long
    Requirement name — or an out-of-range page reusing the full address — would
    push the error message past ``index_render_threshold``, since an error is
    just as much a CLI tool result entering the LLM context as a success. The
    error prose is a small constant, so reserving a quarter-threshold share per
    interpolated address (floored at one whole multibyte character) keeps the
    whole message within the bound even when an address appears more than once.
    """
    budget = max(MAX_UTF8_CHAR_BYTES, threshold // 4)
    return _bounded_address_for_display(text, budget)


def _header_fixed_len(spec_path: object, line_start: int, line_end: int) -> int:
    """UTF-8 byte length of the header WITHOUT the echoed address."""
    location_line = f"# location: {spec_path}:{line_start}-{line_end}\n"
    return (
        _utf8_byte_len("# \n")
        + _utf8_byte_len(location_line)
        + _utf8_byte_len("\n")
    )


def _continuation_fixed_len(
    threshold: int, spec_path: object, line_start: int, line_end: int
) -> int:
    """Worst-case continuation-notice length WITHOUT the echoed address.

    Big dummy page numbers make this a safe upper bound for any real body. The
    address contributes separately (see ``_bounded_display_address``), so it is
    rendered empty here.
    """
    big = 9_999_999
    return _utf8_byte_len(
        _continuation_notice(threshold, "", spec_path, line_start, line_end, big, big)
    )


def _bounded_display_address(
    threshold: int,
    address: str,
    spec_path: object,
    line_start: int,
    line_end: int,
) -> str:
    """Bound the echoed item address so the whole ``show`` output stays ≤ threshold.

    The address may appear in BOTH the header and the continuation notice, plus at
    least one body byte must fit, so its display budget is
    ``(threshold - header_fixed - notice_fixed - 1) // 2``. A normal (short)
    address is far below this cap and echoed in full; only a pathologically long
    Requirement name is truncated. The authoritative physical location is always
    shown intact, so the full body stays retrievable.
    """
    h_fixed = _header_fixed_len(spec_path, line_start, line_end)
    n_fixed = _continuation_fixed_len(threshold, spec_path, line_start, line_end)
    # Reserve MAX_UTF8_CHAR_BYTES (not 1) for the body so that, even at the
    # accepted-minimum threshold, the per-page body budget can hold at least one
    # whole multibyte character without overrunning the byte bound. The address
    # is echoed in BOTH the header and the notice, hence the ``// 2``.
    budget = max(0, (threshold - h_fixed - n_fixed - MAX_UTF8_CHAR_BYTES) // 2)
    return _bounded_address_for_display(address, budget)


def _notice_reserve(
    threshold: int,
    notice_address: str,
    spec_path: object,
    line_start: int,
    line_end: int,
) -> int:
    """Worst-case UTF-8 byte length of a continuation/final paging notice.

    Uses the same BOUNDED ``notice_address`` the continuation notice embeds (the
    header echo's display address), so the reserve matches the bytes the notice
    really emits and the per-page budgeting keeps header + page + notice ≤
    threshold for EVERY address length — a pathologically long Requirement name is
    truncated in the notice exactly as it is in the header, and the next-page
    command tells the LLM to re-use the exact address it supplied. Big dummy page
    numbers make it a safe upper bound for any real body.
    """
    big = 9_999_999
    return max(
        _utf8_byte_len(
            _continuation_notice(
                threshold, notice_address, spec_path, line_start, line_end, big, big
            )
        ),
        _utf8_byte_len(
            _final_notice(threshold, spec_path, line_start, line_end, big, big)
        ),
    )


def _show_render_threshold_floor(
    spec_path: object, line_start: int, line_end: int
) -> int:
    """Return the ``show`` path's irreducible byte floor for a single bounded page.

    A bounded ``show`` page is, at minimum, a header (with the intact physical
    location line) + at least one body byte + a paging notice. The config layer
    only clamps ``index_render_threshold`` up to the much smaller index-nav floor
    (``MIN_RENDER_THRESHOLD`` ≈ 82 bytes), so a configured value between that and
    this ``show`` floor (~450 bytes) is accepted by the config layer but is too
    small to render even one bounded page within the configured limit.

    The caller compares the configured threshold against this floor and rejects
    an infeasibly small configuration explicitly (a non-zero exit with a clear
    message) rather than silently raising the threshold — silently clamping would
    emit more bytes than the configured limit while still reporting success,
    violating the invariant that every ``se3 spec`` response entering the LLM
    context stays within the configured bound.

    The notice fixed length is computed with a maximal dummy threshold-digit
    count so the floor is a safe upper bound regardless of the final value.
    """
    h_fixed = _header_fixed_len(spec_path, line_start, line_end)
    n_fixed = _continuation_fixed_len(9_999_999, spec_path, line_start, line_end)
    # Reserve MAX_UTF8_CHAR_BYTES (not 1) for the body: a single bounded page must
    # be able to carry at least one whole multibyte character, so the irreducible
    # floor is header + one full character + paging notice. Reserving only 1 byte
    # would accept a threshold at which a leading CJK character overruns the bound.
    return h_fixed + n_fixed + MAX_UTF8_CHAR_BYTES


def _build_header(
    display_address: str,
    spec_path: object,
    line_start: int,
    line_end: int,
) -> str:
    """Build the ``show`` header from the already-bounded display address.

    The location line (file path + 1-based inclusive line interval) is always
    emitted intact. ``display_address`` is bounded by ``_bounded_display_address``
    so the header itself cannot overrun the threshold.
    """
    location_line = f"# location: {spec_path}:{line_start}-{line_end}\n"
    return f"# {display_address}\n{location_line}\n"


def _page_budgets(
    body: str,
    header: str,
    threshold: int,
    notice_address: str,
    spec_path: object,
    line_start: int,
    line_end: int,
):
    """Compute the per-page byte budget and the page list for a Requirement body.

    Returns ``(fits, pages)`` where ``fits`` is True when the whole body fits in
    one page with no continuation notice, and ``pages`` is the deterministic list
    of body pages. All budgeting is in UTF-8 bytes. ``header`` is assumed already
    bounded (see ``_build_header`` / ``_bounded_display_address``). The reserve
    uses the same BOUNDED ``notice_address`` the continuation notice embeds, so
    ``header + page + notice`` stays within the byte threshold for EVERY address
    length (``_bounded_display_address`` reserves the address budget for both the
    header echo AND this notice), keeping every emitted page ≤ threshold.
    """
    body = body.rstrip("\n") + "\n"
    available = threshold - _utf8_byte_len(header)
    if _utf8_byte_len(body) <= available:
        return True, [body]

    # Reserve room for the worst-case continuation/final notice so that
    # header + page + notice always stays within the byte threshold.
    reserve = _notice_reserve(threshold, notice_address, spec_path, line_start, line_end)
    # The floor check + bounded display address together guarantee
    # ``available - reserve >= MAX_UTF8_CHAR_BYTES`` for any accepted threshold, so
    # the per-page budget always holds at least one whole multibyte character and
    # the paginator never has to emit an over-budget character to make progress.
    per_page_budget = max(MAX_UTF8_CHAR_BYTES, available - reserve)
    pages = _paginate_bytes(body, per_page_budget)
    return False, pages


def _page_count(
    body: str,
    header: str,
    threshold: int,
    notice_address: str,
    spec_path: object,
    line_start: int,
    line_end: int,
) -> int:
    """Return the total number of pages a Requirement body is served in."""
    _fits, pages = _page_budgets(
        body, header, threshold, notice_address, spec_path, line_start, line_end
    )
    return len(pages)


def _continuation_notice(
    threshold: int,
    notice_address: str,
    spec_path: object,
    line_start: int,
    line_end: int,
    page: int,
    total: int,
) -> str:
    """Notice appended to every non-final page, naming the next-page command.

    The continuation command embeds the BOUNDED ``notice_address`` (the same
    display address echoed in the header), so the whole ``show`` page — header +
    body slice + this notice — stays within the byte threshold for EVERY address
    length. For a normal-length name the bounded address IS the full address, so
    the command is directly executable. Only for a pathologically long Requirement
    name is the embedded address truncated; in that case the LLM re-uses the exact
    ``<spec>::<requirement>`` it already supplied on the command line (and the
    intact physical location is always emitted alongside), so paging is never
    severed even though the echoed command is shortened to honour the byte bound.
    """
    return (
        f"\n[... truncated: page {page} of {total}. This Requirement body exceeds "
        f"the {threshold}-byte render threshold and is served in deterministic "
        f"byte-bounded pages. Retrieve the next bounded page with: "
        f"se3 spec show {shlex.quote(notice_address)} --page {page + 1} "
        f"(if the address above was truncated to fit the byte threshold, re-use "
        f"the exact <spec>::<requirement> you supplied with --page {page + 1}). "
        f"(Full body physical location: {spec_path}:{line_start}-{line_end}.) Per "
        "the spec writing discipline this Requirement should be split into smaller "
        "items.]\n"
    )


def _final_notice(
    threshold: int,
    spec_path: object,
    line_start: int,
    line_end: int,
    page: int,
    total: int,
) -> str:
    """Notice appended to the final page of a paged Requirement body."""
    return (
        f"\n[... page {page} of {total} (final page). Full body physical "
        f"location: {spec_path}:{line_start}-{line_end}.]\n"
    )


def _render_bounded_show(
    *,
    body: str,
    header: str,
    threshold: int,
    notice_address: str,
    spec_path: object,
    line_start: int,
    line_end: int,
    page: int,
) -> Optional[str]:
    """Render one bounded ``show`` page, or ``None`` if the page is out of range.

    The whole output (header + page body + notice) is bounded to ``threshold``
    UTF-8 bytes for EVERY address length — measured by encoded byte length, not
    Python characters, so neither multibyte content nor a pathologically long
    Requirement name can overrun the budget. A single Requirement is supposed to
    be ≤ 8 KiB per the writing discipline; until an oversized one is split it is
    served in deterministic byte-bounded pages, each ending with a notice that
    names the ``se3 spec show <addr> --page N`` continuation command so the full
    body stays retrievable through bounded ``se3 spec`` stdout. ``notice_address``
    is the BOUNDED display address embedded in that continuation command (the same
    one echoed in the header); for a normal-length name it equals the full address
    and the command is directly executable, while a pathologically long name is
    truncated and the LLM re-uses the exact address it supplied.
    """
    fits, pages = _page_budgets(
        body, header, threshold, notice_address, spec_path, line_start, line_end
    )
    total = len(pages)
    if page < 1 or page > total:
        return None

    if fits:
        # Single page, whole body fits with no continuation notice.
        return header + pages[0]

    # Keep the page content byte-exact (no rstrip) so walking every ``--page``
    # reconstructs the complete body; the notice begins with its own newline.
    content = pages[page - 1]
    if page < total:
        notice = _continuation_notice(
            threshold, notice_address, spec_path, line_start, line_end, page, total
        )
    else:
        notice = _final_notice(
            threshold, spec_path, line_start, line_end, page, total
        )
    return header + content + notice
