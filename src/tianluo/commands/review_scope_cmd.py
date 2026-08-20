"""``luo review-scope`` — read-only inspection of a flow's SELF_CHECK diff scope.

    luo review-scope diff [--baseline implementation|fix] [--flow <id>]
                          [--stat] [--path <p>]...

A SELF_CHECK round is diff-scoped: it reviews the exact difference between a
persisted *review baseline* (a content-keyed snapshot of the workspace taken
immediately before the first IMPLEMENT call, or immediately before one FIX
call) and the working tree as it stands now. This command rebuilds that same
difference for a human or a checker to read in full.

WHY this exists rather than ``git diff``: a review baseline is a snapshot of
workspace *content*, not a commit — it covers dirty tracked files and
pre-existing untracked files as they were at capture — and HEAD advances inside
a flow as the engine commits DAG leaf branches. Any ``git diff`` an operator
composes by hand therefore describes a different range than the one under
review. :class:`~tianluo.engine.review_scope.ReviewScopeManager` is the single
implementation of that reconstruction, and this command is a rendering shell
around it.

INVARIANT: this command never writes. It captures nothing, repairs no
descriptor and touches no flow state — including the materialized diff
artifact, which is the *engine's* record of a round and must not gain entries
because someone looked at a finished flow.

Exit codes (scripted callers, and the snapshot-lifecycle contract, depend on
these staying distinct)::

    0  the diff was reconstructed (possibly empty)
    1  the baseline exists but can no longer be compared to the working tree
    3  no such flow in this project
    4  the baseline was never captured, or was captured as unusable
    5  the flow's baseline snapshots have been reclaimed
    6  a --path filter names something outside this review scope

WHY 3/4/5 are three codes and not one: "you named the wrong flow", "this flow
never got that far" and "the snapshots were cleaned up when the flow ended" are
three different situations with three different remedies, and code 2 is
reserved by Click for usage errors.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import typer

from ..i18n import t

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_UNDECIDABLE = 1
EXIT_FLOW_NOT_FOUND = 3
EXIT_BASELINE_MISSING = 4
EXIT_BASELINE_CLEANED = 5
EXIT_PATH_NOT_IN_SCOPE = 6

review_scope_app = typer.Typer(
    name="review-scope",
    help=t("cli.help.review_scope"),
)


def get_project_root() -> Path:
    """Find project root by looking for a .git directory or an SE3 config file.

    Binds the i18n language to the discovered root: the import-time help strings
    resolve the language singleton from the cwd, which can sit below the project
    root, so it must be re-resolved once the target project is known.
    """
    from ..config import is_se3_project_root
    from ..i18n import bind_project_root

    cwd = Path.cwd()
    root = cwd
    for parent in [cwd] + list(cwd.parents):
        if (parent / ".git").exists() or is_se3_project_root(parent):
            root = parent
            break
    bind_project_root(root)
    return root


def _fail(message: str, code: int) -> None:
    typer.echo(message, err=True)
    raise typer.Exit(code)


def _resolve_flow(project_root: Path, flow_id: Optional[str]) -> Tuple[
    str, Path, Optional[Dict[str, Any]]
]:
    """Resolve (flow_id, that flow's project root, its persisted context).

    The context is ``None`` when the flow record itself is gone — the snapshot
    store or the flow's history record then carry what is left of it, and the
    ``None`` is the signal the baseline lookup needs to tell "reclaimed" from
    "never captured".
    """
    from ..engine.persistence import PersistenceManager
    from ..engine.steps._project_root import resolve_flow_project_root

    persistence = PersistenceManager(project_root)
    requested = str(flow_id or "").strip()
    if not requested:
        requested = str(persistence._peek_active_flow_id() or "")
        if not requested:
            _fail(t("review_scope.error.no_active_flow"), EXIT_FLOW_NOT_FOUND)

    flow = persistence.load_flow_by_id(requested)
    if flow is None:
        flow = persistence.load_archived_flow_by_id(requested)
    if flow is not None:
        context = getattr(getattr(flow, "state", None), "context", None)
        return (
            requested,
            resolve_flow_project_root(flow),
            context if isinstance(context, dict) else {},
        )
    return requested, project_root, None


def _render_stat(
    scope: Any,
    stat: Dict[str, Tuple[int, int]],
    flow_id: str,
    selector: str,
) -> None:
    typer.echo(
        t(
            "review_scope.stat.header",
            selector=selector,
            baseline_id=scope.baseline_id,
            flow_id=flow_id,
        )
    )
    # WHY the tokens, not the raw pathnames: this view is the diff view's
    # sibling over the same scope, and a checker copies file names off either
    # one. A name whose edge whitespace the padding below would swallow — or
    # that carries a line break — has to appear here in the same C-quoted
    # spelling the diff headers use, or the two views would name one file two
    # ways and the padded column would present a spelling nothing can look up.
    from ..engine.review_scope import quote_diff_path

    tokens = {path: quote_diff_path(path) for path in stat}
    width = max((len(token) for token in tokens.values()), default=0)
    insertions = 0
    deletions = 0
    for path, (added, removed) in stat.items():
        insertions += added
        deletions += removed
        typer.echo(f" {tokens[path].ljust(width)} | +{added} -{removed}")
    typer.echo(
        t(
            "review_scope.stat.summary",
            files=len(stat),
            insertions=insertions,
            deletions=deletions,
        )
    )


@review_scope_app.command("diff", help=t("cli.help.review_scope.diff"))
def diff_cmd(
    baseline: str = typer.Option(
        "implementation",
        "--baseline",
        "-b",
        help=t("cli.help.review_scope.baseline"),
    ),
    flow_id: Optional[str] = typer.Option(
        None, "--flow", "-f", help=t("cli.help.review_scope.flow")
    ),
    paths: Optional[List[str]] = typer.Option(
        None, "--path", help=t("cli.help.review_scope.path")
    ),
    stat: bool = typer.Option(
        False, "--stat", help=t("cli.help.review_scope.stat")
    ),
) -> None:
    """Rebuild and print the baseline-to-current diff of one review scope."""
    from ..engine.review_scope import (
        BASELINE_SELECTOR_FIX,
        BASELINE_SELECTOR_IMPLEMENTATION,
        BASELINE_SELECTORS,
        BASELINE_STATUS_CLEANED,
        BASELINE_STATUS_NOT_CAPTURED,
        BASELINE_STATUS_UNAVAILABLE,
        ReviewScopeManager,
        decode_quoted_diff_path,
        diff_stat,
        paths_related,
        quote_diff_path,
        select_filtered_view,
        split_diff_sections,
    )

    selector = str(baseline or "").strip().lower()
    if selector not in BASELINE_SELECTORS:
        # A misspelled selector is a usage error, not a state problem: Click's
        # own exit code 2 keeps it apart from the baseline-state codes below.
        raise typer.BadParameter(
            t(
                "review_scope.error.bad_baseline",
                value=baseline,
                known=", ".join(BASELINE_SELECTORS),
            ),
            param_hint="--baseline",
        )

    from ..engine.chat_history import flow_history_exists

    project_root = get_project_root()
    resolved_id, flow_root, context = _resolve_flow(project_root, flow_id)
    manager = ReviewScopeManager(flow_root, resolved_id)
    # INVARIANT: exit 3 means "this project never ran that flow", so every
    # surviving trace of the flow must be consulted before claiming it. The
    # engine holds ONE active slot and archives rotate, so a completed flow
    # whose snapshots were reclaimed at termination loses both its record and
    # its store as soon as the next flow starts — while `luo history` still
    # lists it. Answering that with flow-not-found tells the operator they
    # misnamed the flow, hiding the real (and actionable) situation the exit-5
    # cleaned-baseline message spells out.
    if (
        context is None
        and not manager.store_exists()
        and not flow_history_exists(project_root, resolved_id)
    ):
        _fail(
            t("review_scope.error.flow_not_found", flow_id=resolved_id),
            EXIT_FLOW_NOT_FOUND,
        )

    lookup = manager.lookup_baseline(selector, context)
    if lookup.status == BASELINE_STATUS_CLEANED:
        _fail(
            t("review_scope.error.cleaned", flow_id=resolved_id),
            EXIT_BASELINE_CLEANED,
        )
    if lookup.status == BASELINE_STATUS_NOT_CAPTURED:
        message = (
            t("review_scope.error.not_captured.fix", flow_id=resolved_id)
            if selector == BASELINE_SELECTOR_FIX
            else t(
                "review_scope.error.not_captured.implementation",
                flow_id=resolved_id,
            )
        )
        _fail(message, EXIT_BASELINE_MISSING)
    if lookup.status == BASELINE_STATUS_UNAVAILABLE:
        _fail(
            t(
                "review_scope.error.unavailable",
                selector=selector,
                flow_id=resolved_id,
                detail=lookup.diagnostic or "-",
            ),
            EXIT_BASELINE_MISSING,
        )

    # INVARIANT: what a baseline view contains is derived from ONE fact — the
    # comparison of the selected persisted snapshot with the current workspace.
    # The implement steps' self-reported paths are deliberately NOT injected
    # here: a path git ignores exists in no snapshot on either side, so no
    # comparison can place it in this view rather than the other one, and the
    # engine keeps the reports as a single flat flow-wide union precisely
    # because attributing them would take execution-side bookkeeping of who
    # declared what (see ``StateMachine._record_declared_paths``). Such a path
    # is therefore absent from both views rather than present in both under a
    # membership claim neither baseline supports; the round's prompt manifest
    # still lists it, by path alone and with no domain mark.
    scope = manager.reconstruct(
        "incremental" if selector == BASELINE_SELECTOR_FIX else "full",
        lookup.baseline,
        write_artifact=False,
    )
    if scope.undecidable:
        _fail(
            t(
                "review_scope.error.undecidable",
                selector=selector,
                flow_id=resolved_id,
                detail=scope.diagnostic or "-",
            ),
            EXIT_UNDECIDABLE,
        )
    if scope.diagnostic:
        # A decidable scope can still carry a note (HEAD advanced past the
        # baseline commit). It goes to stderr so the diff on stdout stays
        # machine-consumable.
        typer.echo(t("review_scope.note.diagnostic", detail=scope.diagnostic), err=True)

    sections = split_diff_sections(scope.unified_diff)
    table = diff_stat(scope)
    supplied = [str(item) for item in (paths or [])]
    # WHY the filter is passed through verbatim: space is a legal filename
    # character, so trimming here would resolve `--path pkg` against a changed
    # file spelled `" pkg/mod.py"` — a path the repository does not hold.
    # `normalize_scope_path` collapses separators and dot segments only, for
    # the same reason; a filter naming no changed path is refused below.
    # WHY a quoted filter also gets its decoded reading: a pathname carrying a
    # line break, edge whitespace or a non-UTF-8 byte is presented — in the
    # manifest, in the diff headers and in the --stat column — solely as a
    # C-quoted token, so that token is the only spelling a checker can copy off
    # those surfaces and paste back here. Refusing it would leave the one file
    # whose name cannot be shown raw unreachable through the very command the
    # prompt sends the checker to.
    # WHY that decoded reading is preferred OVER a raw match rather than kept
    # as its fallback: every surface quotes a name that itself contains a quote
    # character, so a bare token such as ``" pkg/mod.py"`` is what those
    # surfaces display for the path `` pkg/mod.py`` and NEVER for a file
    # literally named ``" pkg/mod.py"`` — that one is displayed as
    # ``"\" pkg/mod.py\""``. Matching raw first would hand the copied token to
    # the literal namesake whenever both changed, answering a single-file
    # question with a different file; the literal name stays reachable through
    # its own escaped token, which decodes straight back to it. Only a
    # WELL-FORMED token decodes — the same rule ``_evidence_path_candidates``
    # applies, so a lenient decode can never ground a filter on a path no
    # surface presented, and anything that does not decode falls through to its
    # raw spelling (which the admission check below still resolves).
    def _filter_spelling(raw: str) -> str:
        decoded = decode_quoted_diff_path(raw)
        if decoded and any(paths_related(key, decoded) for key in table):
            return decoded
        return raw

    requested = [_filter_spelling(raw) for raw in supplied]
    if supplied:
        # WHY admission is decided against the changed-path table alone: that
        # table IS the scope's membership — what --stat renders and what
        # evidence validation grounds on — so a filter accepted here always
        # selects something in both views. Section containment must NOT admit
        # on its own: `paths_related` is one-way precisely so that
        # `--path <changed-file>/whatever` is refused rather than answered with
        # that file's diff.
        #
        # WHY a blank value is refused rather than dropped: `--path "$TARGET"`
        # with an unset variable is still an explicit request to narrow the
        # view, and silently discarding it would answer a single-file question
        # with the entire diff. An empty path names nothing in this scope, so
        # it takes the same out-of-scope rejection any other unsupported filter
        # takes.
        missing = [
            (raw if raw.strip() else '""')
            for raw, path in zip(supplied, requested)
            if not path or not any(paths_related(key, path) for key in table)
        ]
        if missing:
            typer.echo(
                t("review_scope.error.path_not_in_scope", path=", ".join(missing)),
                err=True,
            )
            typer.echo(
                # Quoted for the same reason the --stat column is: this hint
                # tells the operator what to retry with, so it must name each
                # path in a spelling that both survives this line and is
                # accepted back by --path.
                t(
                    "review_scope.hint.changed_paths",
                    paths=", ".join(quote_diff_path(key) for key in table) or "-",
                ),
                err=True,
            )
            raise typer.Exit(EXIT_PATH_NOT_IN_SCOPE)
        # Both views are resolved by one call, so they cover the same files by
        # construction — an exact rename is a single section naming two paths,
        # which a per-view containment scan would resolve to two different file
        # sets (see ``select_filtered_view``).
        sections, table = select_filtered_view(sections, table, requested)

    if stat:
        _render_stat(scope, table, resolved_id, selector)
        raise typer.Exit(EXIT_OK)

    if not sections:
        typer.echo(
            t(
                "review_scope.empty",
                selector=selector,
                flow_id=resolved_id,
            )
        )
        raise typer.Exit(EXIT_OK)

    typer.echo("".join(section.text for section in sections), nl=False)
    raise typer.Exit(EXIT_OK)
