"""Self Check step handler.

LLM-based code review after tests pass. Checks the complete effective task
description for requirement coverage, behavior, integration, regressions,
robustness, and test coverage while treating recorded project invariants as
implementation constraints.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path
from typing import Any

from .. import adjudication
from ..llm_caller import LLMCaller
from ..models import FlowInstance, Step, StepStatus
from ._project_root import resolve_flow_project_root
from ..prompt_markers import inject_boundary
from ..review_scope import (
    count_anchor_lines,
    subtract_line_ranges,
    union_line_ranges,
)
from ..truncation import (
    PHASE_STDERR_TAIL_CHARS,
    PHASE_STDOUT_TAIL_CHARS,
    SELF_CHECK_SCOPE_DIFF_MAX_CHARS,
)
from ..utils.json_parser import parse_json_response
from ...config import DEFAULT_MAX_FIX_ITERATIONS
from ._fix_context import format_fix_iteration_display, render_fix_context

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------
# Issue validation: structural defenses against ungrounded "nit" issues
# -----------------------------------------------------------------------
#
# The legacy schema let the LLM list any concern as an "issue" with a
# free-text description. Because self_check.py routes any non-empty
# ``issues`` list to REVISION_NEEDED, even a low-severity "observation
# only" suggestion would trigger a new fix-loop iteration. Across many
# iterations this produced orphan tests and runaway scope creep.
#
# The new schema requires each issue to carry:
#   - actual_behavior / expected_behavior / divergence (concrete, non-empty)
#   - expectation_source.verbatim_quote — a literal substring of the
#     effective task description, user interjection, or recorded project
#     constraint (charter / WHY:/INVARIANT: comment). The handler
#     normalizes both sides identically (unicode NFKC, smart-quote
#     replacement, whitespace collapse, literal ``\n`` → real newline)
#     before comparison so the LLM cannot rely on cosmetic drift. The one
#     exception is type=="regression": it protects pre-existing,
#     out-of-scope behavior absent from the source pool, so it skips the
#     quote check and must instead ground on evidence_lines.
#   - evidence_lines pointing at changes_made.files_changed paths, or
#     missing_in for "should-have-been-edited but wasn't" cases.
#
# INVARIANT: a finding reported by a check-class step has exactly one
# destination — the fix loop, right now. There is no exemption channel: no
# handler-side discard of "non-actionable" items, no severity-graded pass,
# no "file it as an issue and fix it later". Every item that survives
# validation drives REVISION_NEEDED.
#
# WHY: the validators below answer only ONE question — does the evidence
# stand up? — never "is this worth fixing?". Letting the handler drop items
# on the LLM's own worth-fixing self-assessment silently released real
# defects (a pass whose findings were ALL self-marked non-actionable came
# back green while the raw report showed red). Scope creep from
# observation-only reports is instead suppressed one layer earlier, at the
# LLM's reporting decision: the prompt sets an explicit reporting bar
# (report only evidence-backed real defects, because everything reported
# WILL be fixed on the spot), backed by the evidence-grounding pipeline
# here. Suppression by reporting bar leaves a trace in the report;
# suppression by handler discard does not.
#
# Items failing any check are dropped from the issue list and tallied in
# ``validation_stats`` (also surfaced via outputs and a single log line)
# for post-hoc inspection of LLM behavior.

# Counters that record what was KEPT (or how), not why something was dropped;
# listing them among the rejection reasons would misreport the drop tally.
_NON_REJECTION_STAT_KEYS = frozenset(
    {
        "input_count",
        "kept_count",
        "undecidable_scope_kept_count",
        "readmitted_still_present_count",
    }
)


# A citation's trailing ``:<digits>`` segment — the line-number space, not part
# of the path. Used to keep line-bearing text out of PATH sets.
_LINE_SUFFIX_RE = re.compile(r":\d+$")


def _parse_evidence_line(entry: str) -> tuple[str, int] | None:
    """Split one ``path:N`` citation at its TRAILING line number.

    The path is parsed on the path text itself: a file name may legally
    contain spaces, ``+``, ``@``, ``~``, ``#``, parentheses or commas, so a
    character whitelist would reject valid citations. The path part must be
    non-empty and the trailing segment a positive integer.
    """
    if not isinstance(entry, str):
        return None
    path, sep, raw_line = entry.rpartition(":")
    if not sep or not path:
        return None
    try:
        line_number = int(raw_line)
    except ValueError:
        return None
    if line_number <= 0:
        return None
    return path, line_number


def _usable_anchor_ranges(ranges: Any) -> list[tuple[int, int]]:
    """The ``[start, end]`` pairs of one path that can actually be compared.

    WHY: a path counts as anchor-bearing only when a citation *can* hit one of
    its ranges. Malformed entries carry no line space to hit, so leaving them
    in would make the path demand an impossible anchor and silently drop the
    finding; they are dropped instead, degrading the path to anchor-less.
    """
    out: list[tuple[int, int]] = []
    if not isinstance(ranges, list):
        return out
    for bounds in ranges:
        if not isinstance(bounds, list) or len(bounds) != 2:
            continue
        try:
            out.append((int(bounds[0]), int(bounds[1])))
        except (TypeError, ValueError):
            continue
    return out


def _evidence_path_candidates(entry: str) -> list[tuple[str, int | None]]:
    """Readings of one evidence citation as ``(path, line_or_None)`` pairs.

    WHY: an anchor-less changed path (binary, mode-only, rename-only,
    deletion-only, bare submodule gitlink) grounds evidence at PATH level, so
    a citation there may legitimately carry no line number, or one that names
    nothing citable. Such a line is ignored as an invalid line number rather
    than making the whole citation unparseable. Both readings are returned so
    an anchor-BEARING path still has to validate on its real line.
    """
    text = entry.strip()
    if not text:
        return []
    out: list[tuple[str, int | None]] = []
    parsed = _parse_evidence_line(text)
    if parsed is not None:
        out.append((parsed[0], parsed[1]))
    else:
        # ``path:0`` / ``path:abc`` — drop the unusable suffix and keep the
        # path so path-level grounding can still be decided on it.
        prefix, sep, _suffix = text.rpartition(":")
        if sep and prefix:
            out.append((prefix, None))
    out.append((text, None))
    return out


def _normalize_for_quote_match(s: str) -> str:
    """Symmetric normalization for verbatim_quote ↔ source pool comparison.

    Order matters: literal ``\\n`` → real newline FIRST so subsequent
    whitespace collapse can flatten it. Smart-quote replacement and NFKC
    handle unicode drift between LLM paraphrase and source verbatim.
    """
    if not isinstance(s, str):
        return ""
    s = s.replace("\\n", "\n")
    s = unicodedata.normalize("NFKC", s)
    for smart, plain in (
        ("“", '"'), ("”", '"'),
        ("‘", "'"), ("’", "'"),
    ):
        s = s.replace(smart, plain)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def _build_source_pool(step_inputs: dict) -> list[str]:
    """Collect strings against which verbatim_quote is validated.

    Pool composition:

    1. ``task_description_base`` — the clean, un-decorated effective task
       description (adjudicated-if-ruled, else refined-if-discovery-ran, else
       canonical), populated by ``state_machine._build_step_inputs`` for
       SELF_CHECK steps. Falls back to ``task_description`` for legacy callers
       that don't set the base separately.
       The effective base is exactly one of adjudicated_description,
       discovery refined_description, or the original task. Superseded
       generations are deliberately excluded.
    2. Each entry of ``user_interjections`` — its ``text`` field added
       individually, so an LLM that wants to cite a specific Ctrl-C
       instruction can substring-match against the bare interjection
       text (NOT against our ``## Additional Instructions`` boilerplate
       header which would otherwise be a free-pass quote).
    3. ``project_constraints`` — full charter text and harvested colocated
       WHY:/INVARIANT: comments. These constrain implementation but do not add
       functional requirements beyond the effective task description.

    PLAN/task_groups and implementation summaries never enter this pool: they
    are derived history and cannot override, narrow, or expand requirements.
    """
    pool: list[str] = []

    # Prefer the clean base; fall back to the composed task_description
    # for older inputs (e.g. unit tests or pre-upgrade resumes). The base is
    # already the adjudicated text when a ruling is in effect (both resolve
    # through ``state_machine._effective_task_description_base``).
    base = step_inputs.get("task_description_base")
    if not (isinstance(base, str) and base):
        base = step_inputs.get("task_description")
    if isinstance(base, str) and base:
        pool.append(base)

    interjections = step_inputs.get("user_interjections") or []
    if isinstance(interjections, list):
        for entry in interjections:
            if not isinstance(entry, dict):
                continue
            text = entry.get("text")
            if isinstance(text, str) and text.strip():
                pool.append(text)
    constraints = step_inputs.get("project_constraints") or {}
    if isinstance(constraints, dict):
        for content in constraints.values():
            if isinstance(content, str) and content:
                pool.append(content)
    return pool


def _changed_paths(step_inputs: dict) -> set[str]:
    """Set of paths from ``changes_made.files_changed`` for evidence_lines
    validation. Returns an empty set if the field is missing or malformed.

    WHY: with a decidable scope the reconstructed diff IS the changed set and
    replaces the implement step's self-reported list. When the baseline is
    undecidable the reconstructed set is not authoritative (it may be empty or
    truncated), so both sources are UNIONED into a best-effort hint rather than
    one silently overriding the other — and the hint only widens what grounds,
    never narrows it (see ``_validate_evidence``'s ``scope_undecidable`` rule).
    """
    scoped = step_inputs.get("scope_changed_paths")
    undecidable = bool(step_inputs.get("scope_undecidable"))
    scoped_paths: set[str] = set()
    if isinstance(scoped, list):
        scoped_paths = {
            str(path) for path in scoped
            if isinstance(path, str) and path.strip()
        }
        if not undecidable:
            return scoped_paths

    out: set[str] = set(scoped_paths)
    changes = step_inputs.get("changes_made") or {}
    if not isinstance(changes, dict):
        return out
    files_changed = changes.get("files_changed") or []
    if not isinstance(files_changed, list):
        return out
    for entry in files_changed:
        if isinstance(entry, str):
            out.add(entry)
        elif isinstance(entry, dict):
            p = entry.get("path") or entry.get("file_path")
            if isinstance(p, str) and p:
                out.add(p)
    return out


def _prior_finding_paths(step_inputs: dict) -> set[str]:
    """Changed-path set widening contributed by still-open previous findings.

    WHY: an incremental round's changed-path set is only the latest fix delta,
    so a still-open finding that the fix did not touch has no anchor inside it.
    The prompt requires such a finding to be re-listed under ``issues``; without
    treating its recorded location as in-scope the re-report is dropped as bad
    evidence and the round completes clean while its own
    ``previous_issue_resolutions`` say the defect is still present.

    INVARIANT: a prior finding widens the PATH set only — it never contributes
    line anchors. A prior round's line number is not a current-side causal
    anchor: on a path the current scope left anchor-less it must not fabricate
    anchor-bearing status (which would then reject the bare-path re-report the
    prompt prescribes), and on an anchor-bearing path it must not extend the
    accepted range set with stale context lines or old-side numbers of a file
    this scope deleted. A path reached only through this widening carries no
    entry in ``scope_causal_anchors``, so it grounds at path level — exactly
    what a re-report of an unrelocated finding needs.

    INVARIANT: the citation FORM never decides whether a prior finding widens.
    An anchor-less path is cited as a bare ``path`` (the form the prompt
    mandates there), so requiring a trailing ``:N`` would harvest nothing from
    exactly the findings that need the widening most, and their bare-path
    re-report would be dropped as bad evidence. Line-bearing readings are
    stripped back to their path instead of being added verbatim: keeping a
    ``path:N`` string in the path set would let a later citation of that same
    text ground at "path" level on an anchor-BEARING path and bypass its
    causal-anchor check.
    """
    paths: set[str] = set()
    prev = step_inputs.get("prev_self_check_issues")
    if not isinstance(prev, list):
        return paths

    def _widen(entry: Any) -> None:
        if not isinstance(entry, str):
            return
        for path, _line in _evidence_path_candidates(entry):
            if not path or _LINE_SUFFIX_RE.search(path):
                continue
            paths.add(path)

    for issue in prev:
        if not isinstance(issue, dict):
            continue
        lines = issue.get("evidence_lines")
        if isinstance(lines, list):
            for entry in lines:
                _widen(entry)
        _widen(issue.get("location"))
        missing_in = issue.get("missing_in")
        if isinstance(missing_in, list):
            for entry in missing_in:
                _widen(entry)
    return paths


def _task_scope_domain(step_inputs: dict) -> tuple[set[str], Any] | None:
    """The whole-task evidence domain an incremental round also grounds on.

    WHY an incremental round grounds on two domains: its diff baseline is the
    latest uncovered FIX, but the change the checker is looking at is the whole
    flow's work. A finding whose ``evidence_lines`` land on a line an earlier
    IMPLEMENT or FIX really wrote is grounded in git fact — dropping it as
    fabricated evidence merely because the LATEST fix did not touch that line
    silently loses a true finding, and left the rules inconsistent: the same
    finding routed through ``missing_in`` landed unconditionally.

    Returns ``None`` for a full round (its own baseline already spans the whole
    task), for a degraded round (``scope_undecidable`` grounds on naming any
    path at all, so a second domain adds nothing), and when the whole-task diff
    could not be rebuilt — the round then keeps exactly its fix-delta domain.

    The domain deliberately stays SEPARATE from the round's own rather than
    being merged into one anchor dict: merging would turn a path that is
    anchor-less in the fix delta (deletion-only, binary, rename-only) but
    anchor-bearing across the whole task into an anchor-BEARING path, which
    would start rejecting the bare-path citation the prompt prescribes there.
    Grounding is therefore evaluated per domain and OR-ed, which can only widen
    what lands, never narrow it.
    """
    if bool(step_inputs.get("scope_undecidable")):
        return None
    if not step_inputs.get("scope_task_available"):
        return None
    task_paths = step_inputs.get("scope_task_changed_paths")
    if not isinstance(task_paths, list):
        return None
    changed = {
        str(path) for path in task_paths
        if isinstance(path, str) and path.strip()
    }
    anchors = step_inputs.get("scope_task_causal_anchors")
    if not isinstance(anchors, dict):
        return None
    return changed, anchors


def _validate_evidence(
    issue: dict,
    changed: set[str],
    *,
    require_changed_line: bool = False,
    causal_anchors: Any = None,
    scope_undecidable: bool = False,
) -> bool:
    """Return True when the issue carries usable evidence — one citation that
    grounds in the current review scope, OR at least one non-empty
    ``missing_in`` entry. ``missing_in`` covers "the implementation should
    have edited X but didn't" cases that naturally cannot point at a changed
    line.

    This decides grounding within ONE diff domain. A round may carry more than
    one (an incremental round grounds on its fix delta and on the whole-task
    diff — see ``_task_scope_domain``); ``_grounded_in_any_domain`` OR-s them,
    so every rule below is stated per domain.

    INVARIANT: grounding of a citation is decided by whether its changed path
    is anchor-BEARING or anchor-LESS, and by nothing else:

    * anchor-bearing (``causal_anchors`` holds reconstructable current-side
      added/modified ranges for the path): the cited line MUST fall inside one
      of those ranges. Deletion-side old line numbers are never consulted —
      they name a numbering space that no longer exists in the workspace.
    * anchor-less (binary, mode-only, rename-only, deletion-only, or a bare
      submodule gitlink whose inner files carry the anchors — a changed path
      that BY CONSTRUCTION has no current-side line): grounding stands at path
      level. The citation only has to name the changed path; any accompanying
      line number is ignored as invalid rather than used for judgment. Such a
      finding must never be discarded for lacking a line anchor, and the
      checker must never be pushed to fabricate one.

    An empty anchor set caused by an undecidable baseline is NOT an anchor-less
    path: that case degrades the whole round to ``full`` upstream and arrives
    here with ``causal_anchors=None``, where path-in-changed grounding applies.
    ``scope_undecidable`` goes one step further — see below.

    When ``require_changed_line`` is set the ``missing_in`` fallback narrows —
    a regression claims a change BROKE existing behavior, so it must point into
    the change that caused it rather than at a file nobody edited. The
    anchor-bearing / anchor-less rule above is identical for regressions: an
    anchor-less changed path grounds a regression under ``missing_in`` exactly
    as it does under ``evidence_lines``, so the citation form the reviewer picks
    never decides the outcome.

    ``scope_undecidable`` marks the degraded state where no baseline could be
    reconstructed. There the changed-path set is a hint (the implement step's
    self-reported list at best), not ground truth, so it cannot decide evidence:
    a finding on a genuinely flow-changed file the summary forgot to list would
    be dropped, silently losing an evidence-valid finding in exactly the state
    the mechanism exists to keep safe. Grounding therefore stands on naming a
    path at all, and the caller tallies the relaxation so the degradation is
    visible rather than silent.
    """
    evidence = issue.get("evidence_lines") or []
    if isinstance(evidence, list):
        for ev in evidence:
            if not isinstance(ev, str):
                continue
            for path, line_number in _evidence_path_candidates(ev):
                if scope_undecidable:
                    if path:
                        return True
                    continue
                if not isinstance(causal_anchors, dict):
                    if path in changed:
                        return True
                    continue
                ranges = _usable_anchor_ranges(causal_anchors.get(path))
                if not ranges:
                    if path in changed:
                        # Anchor-less changed path: path-level grounding.
                        return True
                    continue
                if line_number is None:
                    continue
                for start, end in ranges:
                    if start <= line_number <= end:
                        # Other evidence_lines may point into unchanged
                        # affected files. One exact current-scope causal anchor
                        # grounds the finding; the rest describe its impact
                        # surface.
                        return True
    missing_in = issue.get("missing_in") or []
    if not isinstance(missing_in, list):
        missing_in = []
    if require_changed_line:
        # A regression must still point INTO the current scope, so the
        # ``missing_in`` channel opens only for an anchor-less changed path —
        # the same paths that ground at path level above, reached here when the
        # reviewer named the path under ``missing_in`` instead of
        # ``evidence_lines``. Where a causal line does exist, the citation
        # requirement stands.
        for mp in missing_in:
            if not isinstance(mp, str) or not mp.strip():
                continue
            path = mp.strip()
            if scope_undecidable:
                # Degraded scope: under ``evidence_lines`` ANY named path
                # grounds here, so the ``missing_in`` channel must not be the
                # narrower one — that asymmetry would drop a finding purely on
                # the citation form the reviewer chose, in the state where the
                # mechanism most needs to keep findings.
                return True
            if not isinstance(causal_anchors, dict):
                continue
            if path in changed and not _usable_anchor_ranges(
                causal_anchors.get(path)
            ):
                return True
        return False
    for mp in missing_in:
        if isinstance(mp, str) and mp.strip():
            return True
    return False


def _grounded_in_any_domain(
    issue: dict,
    domains: list[tuple[set[str], Any]],
    *,
    require_changed_line: bool = False,
    scope_undecidable: bool = False,
) -> bool:
    """Ground an issue against the union of this round's evidence domains.

    Each domain is one ``(changed_paths, causal_anchors)`` diff view; a finding
    lands when it grounds in ANY of them. See ``_task_scope_domain`` for why an
    incremental round carries two and why they are OR-ed rather than merged.
    """
    return any(
        _validate_evidence(
            issue,
            changed,
            require_changed_line=require_changed_line,
            causal_anchors=anchors,
            scope_undecidable=scope_undecidable,
        )
        for changed, anchors in domains
    )


def _describe_issue(issue: dict) -> str:
    """Render a kept issue as a single bullet line for fix_instructions.

    Goes beyond ``extract_issue_display_fields`` to expose
    ``expected_behavior`` separately — the implement step benefits from
    seeing actual vs expected explicitly when fixing the issue.
    """
    from ._fix_context import extract_issue_display_fields

    _severity, _desc, location = extract_issue_display_fields(issue)
    primary_loc = location or "?"

    actual = issue.get("actual_behavior") or issue.get("description") or ""
    expected = issue.get("expected_behavior", "")
    divergence = issue.get("divergence", "")

    parts = [primary_loc]
    if actual:
        parts.append(f"actual: {actual}")
    if expected:
        parts.append(f"expected: {expected}")
    if divergence:
        parts.append(f"divergence: {divergence}")
    return " | ".join(parts)


def _validate_and_filter_issues(
    raw_issues: list,
    step_inputs: dict,
) -> tuple[list[dict], dict]:
    """Run the structural validation pipeline against each raw issue.

    Returns a tuple ``(kept_issues, stats)`` where ``stats`` is a dict
    of per-rejection-reason counts. The pipeline is:

    1. ``verbatim_quote`` empty after normalize → drop
    2. ``verbatim_quote`` not a substring of any source-pool entry → drop
    3. ``evidence_lines`` / ``missing_in`` provides no real grounding → drop
    4. ``actual_behavior`` / ``expected_behavior`` / ``divergence`` empty → drop

    Every check asks only whether the evidence stands up — never whether the
    finding is worth fixing. Anything that survives is actionable by definition.

    Exception: ``expectation_source.type == "regression"`` issues are exempt
    from the verbatim-quote checks (1 & 2). A regression protects PRE-EXISTING
    behavior outside the task's textual scope, which by definition has no entry
    in the source pool; grounding it on a quote would be impossible. Instead the
    evidence grounding (3) is mandatory — the issue must point at the changed
    line(s) responsible. New ``plan_task`` findings are rejected explicitly:
    task groups are derived scheduling data, not an acceptance authority.
    """
    stats = {
        "input_count": 0,
        "kept_count": 0,
        "empty_quote_count": 0,
        "quote_not_in_source_count": 0,
        "bad_evidence_count": 0,
        "empty_field_count": 0,
        "unsupported_source_type_count": 0,
        "non_dict_count": 0,
        "undecidable_scope_kept_count": 0,
    }
    kept: list[dict] = []

    if not isinstance(raw_issues, list):
        return kept, stats

    pool_normalized = [_normalize_for_quote_match(s) for s in _build_source_pool(step_inputs)]
    pool_normalized = [p for p in pool_normalized if p]
    changed = _changed_paths(step_inputs)
    scope_undecidable = bool(step_inputs.get("scope_undecidable"))
    causal_anchors = (
        step_inputs.get("scope_causal_anchors")
        if "scope_causal_anchors" in step_inputs and not scope_undecidable
        else None
    )
    # WHY: ``scope_deletion_anchors`` is deliberately NOT consulted for
    # grounding. Old-side line numbers name a numbering space the workspace no
    # longer has, so they can neither validate nor invalidate a citation: a
    # deletion-only path is simply anchor-less and grounds at path level.
    #
    # Still-open prior findings widen the changed-path set and NOTHING else:
    # their recorded line numbers belong to an earlier round's numbering space
    # and are never merged into ``causal_anchors`` (see
    # ``_prior_finding_paths``).
    prior_paths = _prior_finding_paths(step_inputs)
    changed = changed | prior_paths

    # An incremental round grounds on the union of its fix delta and the whole
    # task's diff (``_task_scope_domain``). The prior-finding path widening
    # applies to every domain: it is a property of the finding, not of a
    # baseline.
    domains: list[tuple[set[str], Any]] = [(changed, causal_anchors)]
    task_domain = _task_scope_domain(step_inputs)
    if task_domain is not None:
        task_changed, task_anchors = task_domain
        domains.append((task_changed | prior_paths, task_anchors))

    for issue in raw_issues:
        stats["input_count"] += 1
        if not isinstance(issue, dict):
            stats["non_dict_count"] += 1
            continue
        source = issue.get("expectation_source") or {}
        src_type = source.get("type", "") if isinstance(source, dict) else ""
        quote = (
            source.get("verbatim_quote", "")
            if isinstance(source, dict) else ""
        )

        allowed_source_types = {
            "task_description",
            "user_interjection",
            "charter",
            "why_comment",
            "regression",
        }
        if src_type not in allowed_source_types:
            stats["unsupported_source_type_count"] += 1
            continue

        # Regression issues protect pre-existing, out-of-scope behavior that has
        # no entry in the verbatim source pool — so they bypass the quote checks
        # and rely solely on diff-evidence grounding below.
        if src_type != "regression":
            norm_quote = _normalize_for_quote_match(quote)
            if not norm_quote:
                stats["empty_quote_count"] += 1
                continue
            # Build the relaxed substring-match candidates (bullet/id-prefix and
            # trailing-ellipsis variants that ``_format_task_groups`` can add to a
            # prompt-visible task line the raw pool lacks). Shared with the
            # adjudicate dead-clause check via ``adjudication.relaxed_quote_candidates``
            # so abolition and validation accept identically.
            candidates = adjudication.relaxed_quote_candidates(norm_quote)
            if not any(c in p for c in candidates for p in pool_normalized):
                stats["quote_not_in_source_count"] += 1
                continue

        grounded = _grounded_in_any_domain(
            issue,
            domains,
            require_changed_line=(src_type == "regression"),
        )
        if not grounded and scope_undecidable:
            # Degraded state: the changed-path hint could not be proven, so it
            # must not be the reason an evidence-bearing finding disappears.
            # Keeping it (and tallying the relaxation) is the safe direction —
            # the fix loop can judge it, a silent drop cannot.
            grounded = _grounded_in_any_domain(
                issue,
                domains,
                require_changed_line=(src_type == "regression"),
                scope_undecidable=True,
            )
            if grounded:
                stats["undecidable_scope_kept_count"] += 1
        if not grounded:
            stats["bad_evidence_count"] += 1
            continue

        for field in ("actual_behavior", "expected_behavior", "divergence"):
            if not _normalize_for_quote_match(issue.get(field, "")):
                stats["empty_field_count"] += 1
                break
        else:
            kept.append(issue)
            stats["kept_count"] += 1

    return kept, stats


SELF_CHECK_PROMPT = """You are an expert code reviewer. Review the implementation against the complete effective requirements and recorded project constraints.

## Task Description
{task_description}

This is the functional-requirement authority for this review: the effective
base task description (original, discovery-refined, or adjudicated) plus all
effective user interjections. No plan, task group, or implementation summary may
override, narrow, or expand it.

## Review Scope
{review_scope}

## Changes Made
{changes_made}

## Test Results
{test_results}

## Recorded Project Constraints
{project_constraints}

The full Project Charter is injected below. Charter clauses, colocated
WHY:/INVARIANT: comments, and invariants demonstrable from the existing code are
constraints on how the requirements may be implemented. They are not a derived
plan and do not replace the functional requirements above.

## Fix Context
{fix_context}

## Review Dimensions

The review scope controls attention, never tool permissions. Read any changed
or unchanged repository code, the Charter, code-index, and test results needed
to trace effects. Follow calls, shared state, protocols, data formats,
configuration, concurrency, and invariants beyond the changed paths. In every
pass, check all six dimensions directly from the effective Task Description:

1. **Requirement Completeness**: Is every effective requirement implemented,
   including user interjections? For a wholly missing requirement, use
   `missing_in` to name the files or integration points that should carry it.
2. **Behavioral Correctness**: Does the implementation produce the required
   observable behavior for normal, boundary, and failure cases?
3. **Cross-Module Integration**: Follow calls, shared state, protocols, data
   formats, configuration, concurrency, and other affected integration points.
4. **Regression Safety**: Did a changed line break pre-existing behavior outside
   the requested change? Use `expectation_source.type = "regression"` and anchor
   the finding to the changed line that causes the regression.
5. **Robustness**: Check error handling, cleanup, resource ownership, retries,
   malformed inputs, partial failures, and concurrency hazards.
6. **Test Coverage**: Do tests exercise the requirements and important logic,
   integration, regression, and failure paths? A concrete missing critical test
   path is a finding; a generic request for more tests is not.

PLAN/task_groups and implementation summaries, if encountered in history, are
only navigation clues. Never treat them as requirement sources and never emit a
new finding with `expectation_source.type = "plan_task"`.

## What NOT to check
- **Code style or formatting** — not actionable here.
- **Performance optimization suggestions** — only flag if there's a clear correctness issue.
- **Anything a dedicated downstream step owns** — as a fix-loop checker you MUST NOT report concerns that a later specialized step decides; that creates standoffs where implement cannot resolve them. In particular, the version number and version files (e.g. the `version` field in `pyproject.toml`) — whether and how to bump them — are decided by the downstream `version_analyze` step against the pre-session baseline; do NOT report "version not bumped" or "version number is wrong" as an issue.

## Severity Levels (all validated severities enter the fix loop)
- **critical**: Logic error that will cause incorrect behavior, data corruption, or crashes in normal usage paths.
- **high**: Missing error handling or boundary condition that will cause failures in reasonably common scenarios.
- **medium**: Defensive improvement that would prevent issues in edge cases, or a minor gap in test coverage.
- **low**: Nice-to-have improvement, minor robustness enhancement, or additional test suggestion.

## Issue Schema (HARD requirements — handler validates and drops violators)

Each issue MUST be a JSON object with these fields:

- `severity`: one of "critical" / "high" / "medium" / "low"
- `location`: concise primary `path:N` location, or the primary missing integration point
- `actual_behavior`: what the code in `changes_made` currently does (concrete, observable; non-empty)
- `expected_behavior`: what the code SHOULD do (non-empty)
- `divergence`: under what specific input / sequence / state does `actual_behavior` produce a wrong result (concrete failure scenario; non-empty)
- `expectation_source`: where "should do" comes from. Must be:
    {{ "type": "task_description" | "user_interjection" | "charter" | "why_comment" | "regression",
       "verbatim_quote": "<a literal substring of the grounding text — see rules below>" }}
  Grounding rules by type (the handler enforces these and drops violators):
    - `task_description` / `user_interjection` / `charter` / `why_comment`: `verbatim_quote` MUST be a literal substring of the effective task, one interjection, the Charter, or a harvested WHY:/INVARIANT: comment. The handler normalizes both the quote and the source pool (NFKC + smart-quote replacement + whitespace collapse + literal `\\n` → newline) and drops a finding whose normalized quote is absent. Quote a substantive rule, not a generic noun.
    - `regression`: use for the Regression dimension, where the violated expectation is PRE-EXISTING behavior outside the task scope (it has no entry in the text above). `verbatim_quote` is NOT substring-checked for this type; instead you MUST ground the issue in the change that caused it: `evidence_lines` pointing at the causal changed line, or — where that changed path has no current-side line at all (see below) — naming the changed path itself, in `evidence_lines` or in `missing_in`.
- `evidence_lines`: array of `"path:N"` strings (or a bare `"path"`, see below). At least one entry MUST ground in the current review-scope diff (the handler verifies), as follows:
    - Changed path WITH current-side added/modified lines: cite one of them. `N` MUST be the line number in the CURRENT file (the `+`-side number shown in the diff, i.e. a line inside one of the `added lines (current file)` ranges the scope_manifest lists for that path) — an old-side number of a deleted line never anchors, since it names no line that exists now.
    - Changed path with NO current-side line by construction (binary, mode-only, rename-only, deletion-only — including a wholly deleted file — or a bare submodule gitlink): cite the path itself. Grounding is at path level there; write the bare `"path"` (a trailing line number is ignored). Do NOT invent a line number and do NOT drop a real finding just because the path has no line to cite.
  Once that causal anchor is present, additional entries MAY point into unchanged files whose behavior is affected. At least one causal entry is required UNLESS `missing_in` is non-empty.
- `missing_in`: array of file paths that should have been edited/created but were not. Use this for a requirement omission or missed integration point that has no changed causal line.

## Previous Issue Resolutions (HARD requirement when prev_issues are listed)

If the Fix Context above includes "Previously Reported Issues", you MUST emit a `previous_issue_resolutions` array. For EACH previously-reported issue, include exactly one entry:
  {{ "prev_issue_summary": "<short paraphrase identifying which prev issue>",
     "status": "fixed" | "still_present" }}
- "fixed" — the change in `changes_made` resolves it; do NOT also list it again under `issues`.
- "still_present" — the change did not resolve it; ALSO list it again under `issues` with full schema.

## Reporting Bar (there is no discard channel — everything you report gets fixed)

Report ONLY real defects you can back with evidence. There is no "observation only" escape hatch and no severity low enough to be ignored: every issue you list that passes validation goes straight into a fix loop and WILL be changed in the code on the spot. So do NOT report pure preferences, style opinions, speculative "might be nice" hardening, or observations you would not want someone to act on. If you cannot state a concrete wrong result under a concrete input, it is not an issue — leave it out.

## Concision without loss of evidence

Keep each finding concise and local. Do not repeat the task, charter, project
background, or the same explanation across fields. Concision never relaxes the
required location, concrete actual behavior, expected behavior, divergence,
source quote, or evidence. Avoid tentative phrasing; state the wrong result as a
fact under a concrete input/sequence/state.

Respond in JSON format:
```json
{{
    "issues": [
        {{
            "severity": "critical|high|medium|low",
            "location": "src/foo.py:42",
            "actual_behavior": "...",
            "expected_behavior": "...",
            "divergence": "...",
            "expectation_source": {{
                "type": "task_description",
                "verbatim_quote": "..."
            }},
            "evidence_lines": ["src/foo.py:42"],
            "missing_in": []
        }}
    ],
    "previous_issue_resolutions": [
        {{ "prev_issue_summary": "...", "status": "fixed" }}
    ],
    "summary": "Brief overall assessment of the implementation quality"
}}
```

If the implementation is solid with no issues found, return an empty issues array (and an empty previous_issue_resolutions array if there were no prev_issues).
"""

# Two-segment marker only: USER_CONTENT region is empty.
# self_check consumes upstream artifacts (effective task / changes / tests /
# recorded constraints); no user-literal field is appended here. The
# web console renders the whole post-BEGIN tail inside the collapsed
# system-prompt chip.
SELF_CHECK_PROMPT = inject_boundary(SELF_CHECK_PROMPT, "## Task Description\n")


def _format_project_constraints(sources: dict[str, str]) -> str:
    """Render the non-duplicated constraint context for the review prompt."""
    lines = ["- Project Charter: injected in full below."]
    why_comments = sources.get("why_comments", "")
    if why_comments:
        lines.extend(
            [
                "- Colocated WHY:/INVARIANT: comments from touched code:",
                why_comments,
            ]
        )
    else:
        lines.append("- Colocated WHY:/INVARIANT: comments: none harvested.")
    return "\n".join(lines)


# A path whose diff is a few thousand hunks would otherwise turn the manifest
# into the very wall of text the inline-diff budget exists to avoid. The cut is
# announced with the exact command that shows the rest, so a capped path is
# never mistaken for a fully enumerated one.
_MANIFEST_MAX_RANGES_PER_PATH = 30


def _anchor_map(value: Any) -> dict:
    """Normalize a persisted anchor mapping into ``{path: ranges}``."""
    if not isinstance(value, dict):
        return {}
    return {
        str(path): ranges
        for path, ranges in value.items()
        if isinstance(ranges, (list, tuple))
    }


def _path_list(value: Any) -> list:
    return [str(item) for item in value] if isinstance(value, list) else []


def _format_line_ranges(ranges: list) -> str:
    """Render merged inclusive ranges the way a checker would cite them."""
    parts = [
        str(start) if start == end else f"{start}-{end}"
        for start, end in ranges[:_MANIFEST_MAX_RANGES_PER_PATH]
    ]
    if len(ranges) > _MANIFEST_MAX_RANGES_PER_PATH:
        parts.append(f"(+{len(ranges) - _MANIFEST_MAX_RANGES_PER_PATH} more)")
    return ", ".join(parts)


def _review_scope_domains(step_inputs: dict) -> tuple:
    """Split the round's anchors into its whole domain and its newest slice.

    Returns ``(whole, delta, delta_label, rest_label)`` where each domain is
    ``(paths, causal_anchors, deletion_anchors)``. The delta is what the round
    is newly accountable for — the fix delta on an incremental round, the fixes
    made since the previous full round on a full one — and is ``None`` when the
    round has no earlier slice to distinguish it from (the initial full round),
    or when the second reconstruction was unavailable.
    """
    mode = str(step_inputs.get("scope_mode", "full") or "full")
    round_domain = (
        _path_list(step_inputs.get("scope_changed_paths")),
        _anchor_map(step_inputs.get("scope_causal_anchors")),
        _anchor_map(step_inputs.get("scope_deletion_anchors")),
    )
    if mode == "incremental":
        if not step_inputs.get("scope_task_available"):
            return round_domain, None, "", ""
        task_domain = (
            _path_list(step_inputs.get("scope_task_changed_paths")),
            _anchor_map(step_inputs.get("scope_task_causal_anchors")),
            _anchor_map(step_inputs.get("scope_task_deletion_anchors")),
        )
        if not task_domain[0]:
            return round_domain, None, "", ""
        return (
            task_domain,
            round_domain,
            "this fix",
            "earlier work in this task",
        )
    if not step_inputs.get("scope_fix_delta_available"):
        return round_domain, None, "", ""
    delta_domain = (
        _path_list(step_inputs.get("scope_fix_delta_changed_paths")),
        _anchor_map(step_inputs.get("scope_fix_delta_causal_anchors")),
        _anchor_map(step_inputs.get("scope_fix_delta_deletion_anchors")),
    )
    if not delta_domain[0]:
        return round_domain, None, "", ""
    return (
        round_domain,
        delta_domain,
        "changed by fixes since the last full round",
        "already present at the last full round",
    )


def _format_scope_manifest(step_inputs: dict) -> list:
    """Render the anchor set evidence validation grounds on, path by path.

    WHY the anchors are shown at all: they decide whether a citation is kept,
    and a checker that cannot see them can only guess which line numbers are
    citable — the previous rendering exposed the changed paths and nothing
    else. Everything here is derived from the persisted baselines, so it states
    git facts only: no fix-iteration count, no trigger type, no list of
    findings an earlier round closed.
    """
    whole, delta, delta_label, rest_label = _review_scope_domains(step_inputs)
    whole_paths, whole_causal, whole_deletion = whole
    delta_paths, delta_causal, delta_deletion = delta or ([], {}, {})

    paths = set(whole_paths) | set(whole_causal) | set(whole_deletion)
    paths |= set(delta_paths) | set(delta_causal) | set(delta_deletion)
    if not paths:
        return ["- scope_manifest: (no changed path in this scope)"]

    if step_inputs.get("scope_undecidable"):
        # The reconstruction failed, so the same relaxation the evidence check
        # applies has to be stated here: these anchors are an unproven hint and
        # a finding outside them is still kept.
        lines = [
            "- scope_manifest (UNPROVEN — the reconstruction failed, so this is",
            "  a hint, not the citable line space; cite the line you verified):",
        ]
    else:
        lines = [
            "- scope_manifest (changed paths, sizes, and the exact anchor",
            "  ranges a `path:line` citation may reference):",
        ]
    if delta is None and str(step_inputs.get("scope_mode", "")) == "incremental":
        # Without a second domain the split cannot be shown, and an unlabelled
        # incremental manifest would read as the whole task.
        lines.append("  (every range below is this fix's own delta)")
    for path in sorted(paths):
        added = union_line_ranges(
            whole_causal.get(path), delta_causal.get(path)
        )
        removed = union_line_ranges(
            whole_deletion.get(path), delta_deletion.get(path)
        )
        head = (
            f"  - {path}: "
            f"+{count_anchor_lines(added)} -{count_anchor_lines(removed)}"
        )
        if added:
            head += f" | added lines (current file) {_format_line_ranges(added)}"
        if removed:
            head += (
                " | deleted lines (baseline file) "
                f"{_format_line_ranges(removed)}"
            )
        lines.append(head)
        if delta is None:
            continue
        own_added = union_line_ranges(delta_causal.get(path))
        own_removed = union_line_ranges(delta_deletion.get(path))
        rest_added = subtract_line_ranges(added, own_added)
        rest_removed = subtract_line_ranges(removed, own_removed)
        for label, add_ranges, del_ranges in (
            (delta_label, own_added, own_removed),
            (rest_label, rest_added, rest_removed),
        ):
            if not add_ranges and not del_ranges:
                continue
            parts = []
            if add_ranges:
                parts.append(f"added {_format_line_ranges(add_ranges)}")
            if del_ranges:
                parts.append(f"deleted {_format_line_ranges(del_ranges)}")
            lines.append(f"      - {label}: {'; '.join(parts)}")
    return lines


def _format_scope_access(mode: str, flow_id: str, artifact: str) -> list:
    """Render how to pull the full diff, and why not to rebuild it with git."""
    flag = f" --flow {flow_id}" if flow_id else ""
    lines = [
        "- reading the full change set (read-only, and the exact same "
        "reconstruction this round was scoped with):",
        f"    luo review-scope diff --baseline implementation{flag}"
        "            # whole task, full diff text",
        f"    luo review-scope diff --baseline implementation{flag} --stat"
        "     # per-file overview",
        f"    luo review-scope diff --baseline implementation{flag} "
        "--path <path>   # one file only",
    ]
    if mode == "incremental":
        lines.append(
            f"    luo review-scope diff --baseline fix{flag}"
            "                       # only the fix delta above"
        )
    if artifact:
        lines.append(
            f"  A materialized copy of this round's diff is also at {artifact}."
        )
    lines.append(
        "- do NOT rebuild the review range yourself with `git diff` / `git "
        "show` / `git log`: a review baseline is a content snapshot of the "
        "workspace (dirty tracked files and pre-existing untracked files "
        "included), NOT a commit, and HEAD advances inside a flow as it "
        "commits. Any range you compose by hand is therefore the wrong range — "
        "it will hide real changes and show you changes nobody asked you to "
        "review. The command above (and the persisted artifact it mirrors) is "
        "the only correct source for the change set; git remains available for "
        "every other question (file contents, blame, history)."
    )
    return lines


def _format_review_scope(step_inputs: dict, flow_id: str = "") -> str:
    """Render the persisted review scope without implying a read whitelist."""
    mode = str(step_inputs.get("scope_mode", "full") or "full")
    baseline_id = str(step_inputs.get("baseline_id", "") or "<unavailable>")
    changed_paths = step_inputs.get("scope_changed_paths") or []
    if not isinstance(changed_paths, list):
        changed_paths = []
    diff_text = step_inputs.get("scope_diff")
    if not isinstance(diff_text, str):
        diff_text = ""
    undecidable = bool(step_inputs.get("scope_undecidable"))
    diagnostic = str(step_inputs.get("scope_diagnostic", "") or "")

    if mode == "incremental":
        purpose = (
            "Incremental round: focus first on the exact delta made by this fix "
            "relative to its persisted fix baseline, while still validating the "
            "complete effective requirements and tracing impact across the repository."
        )
    else:
        purpose = (
            "Full round: review the complete effective requirements and every code "
            "change introduced since the persisted implementation baseline."
        )

    lines = [
        f"- scope_mode: {mode}",
        f"- baseline_id: {baseline_id}",
        f"- changed_paths: {', '.join(str(p) for p in changed_paths) if changed_paths else '(none)' }",
        f"- purpose: {purpose}",
    ]
    task_paths = step_inputs.get("scope_task_changed_paths")
    if (
        mode == "incremental"
        and step_inputs.get("scope_task_available")
        and isinstance(task_paths, list)
        and task_paths
    ):
        # WHY the widened evidence rule is stated to the checker: the handler
        # now grounds a citation on the whole-task diff too, and a rule the
        # reviewer cannot see is a rule it cannot use — it would keep
        # suppressing real findings on earlier flow work to stay inside the
        # delta. This states which citations GROUND; it does not move attention,
        # which stays on the fix delta above. Only git facts appear here — no
        # fix-iteration count, trigger type, or list of already-closed findings.
        lines.append(
            "- task_changed_paths (whole flow, implementation baseline → now): "
            + ", ".join(str(p) for p in task_paths)
        )
        lines.append(
            "- evidence rule: a citation grounds on ANY line this flow changed "
            "across the whole task, not only inside the fix delta above. A real "
            "defect you find in this flow's earlier work is a valid finding — "
            "cite the line you actually verified it on and report it."
        )
    if step_inputs.get("scope_fallback_from_incremental"):
        lines.append(
            "- fallback: the incremental baseline was not trustworthy, so this is "
            "a full review; do not treat an unavailable incremental diff as empty."
        )
    if undecidable:
        lines.extend([
            f"- baseline diagnostic: {diagnostic or 'scope reconstruction unavailable'}",
            "- safety rule: the diff below is unavailable, not empty. Inspect git, "
            "history, and repository state directly; never claim clean merely because "
            "this section has no diff hunks.",
            "- evidence rule: ground each finding on the real path you verified it "
            "in. In this state the changed_paths list above is an unproven hint, "
            "not a filter — a finding on a file it omits is still kept.",
        ])

    lines.extend(_format_scope_manifest(step_inputs))
    lines.extend(
        _format_scope_access(
            mode, flow_id, str(step_inputs.get("scope_diff_artifact", "") or "")
        )
    )

    unresolved = []
    for key in ("prev_self_check_issues", "self_check_deferred_issues"):
        value = step_inputs.get(key) or []
        if isinstance(value, list):
            unresolved.extend(item for item in value if isinstance(item, dict))
    if unresolved:
        lines.append("- unresolved_findings:")
        lines.append(json.dumps(unresolved, ensure_ascii=False, default=str))
    else:
        lines.append("- unresolved_findings: []")

    lines.append("\n### Exact Baseline-to-Current Diff")
    if diff_text:
        if len(diff_text) > SELF_CHECK_SCOPE_DIFF_MAX_CHARS:
            # WHY the oversized diff is withheld ENTIRELY instead of being cut
            # at the budget: a diff sliced mid-file reads exactly like a
            # complete one — nothing in the remaining text says which files
            # never appeared — so the checker silently reviews a fraction of
            # the change and calls it covered. The manifest above still names
            # every changed path and every citable anchor range, and the full
            # text is one read-only command away, so nothing is lost but the
            # false impression of completeness.
            lines.append(
                f"(NOT INLINED: this diff is {len(diff_text)} chars, over the "
                f"{SELF_CHECK_SCOPE_DIFF_MAX_CHARS}-char inline budget. It is "
                "withheld whole rather than cut in half — a half diff would "
                "look complete. The scope_manifest above lists every changed "
                "path and anchor range in it; pull the text itself with the "
                "`luo review-scope diff` commands above, per file when it "
                "helps. Read it before judging coverage: an unread file is "
                "not a clean file.)"
            )
        else:
            lines.append(f"```diff\n{diff_text}\n```")
    elif undecidable:
        lines.append("(unavailable)")
    else:
        lines.append("(empty)")
    return "\n".join(lines)


def self_check_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the self_check step.

    Performs LLM-based review across the six required quality dimensions using
    the effective task description as the functional-requirement authority.

    Returns COMPLETED when no issues are found.
    Returns REVISION_NEEDED when issues exist (regardless of iteration count),
    letting the state machine handle exhaustion centrally.
    """
    task_description = step.inputs.get("task_description", "")
    changes_made = step.inputs.get("changes_made", {})
    test_results = step.inputs.get("test_results", {})

    fix_iteration = step.inputs.get("fix_iteration", 0)
    # Honor an explicit 0 from inputs (the unlimited sentinel); fall back to
    # the default only when the input is genuinely missing.
    raw_max = step.inputs.get("max_fix_iterations")
    max_iterations = raw_max if isinstance(raw_max, int) and not isinstance(raw_max, bool) else DEFAULT_MAX_FIX_ITERATIONS
    prev_issues = step.inputs.get("prev_self_check_issues", [])
    fix_history = step.inputs.get("fix_history", [])
    pass_index = step.inputs.get("self_check_pass_index", 1)
    passes_required = step.inputs.get("self_check_passes_required", 1)

    # Defer-fix (item 1): when a non-terminal pass finds only a few
    # non-critical/high issues, stash them and let the remaining nested passes
    # run before entering one consolidated fix loop. ``threshold <= 0`` disables
    # deferral (every issue-finding pass triggers fix immediately). The stash
    # carried from prior passes of this fix-loop round is injected by the state
    # machine and reset at pass #1.
    raw_threshold = step.inputs.get("self_check_defer_fix_threshold", 0)
    defer_threshold = (
        raw_threshold if isinstance(raw_threshold, int)
        and not isinstance(raw_threshold, bool) else 0
    )
    deferred_issues = step.inputs.get("self_check_deferred_issues") or []
    if not isinstance(deferred_issues, list):
        deferred_issues = []
    defer_enabled = defer_threshold > 0
    is_last_pass = pass_index >= passes_required

    # Write back so history renderers can read the pass position
    step.outputs["self_check_pass_index"] = pass_index
    step.outputs["self_check_passes_required"] = passes_required
    for key in (
        "scope_mode",
        "requested_scope_mode",
        "baseline_id",
        "scope_changed_paths",
        "scope_causal_anchors",
        "scope_deletion_anchors",
        "scope_task_baseline_id",
        "scope_task_changed_paths",
        "scope_task_causal_anchors",
        "scope_task_deletion_anchors",
        "scope_task_diff_artifact",
        "scope_task_available",
        "scope_task_diagnostic",
        # The fix-delta anchors themselves stay out of the persisted outputs:
        # they are manifest decoration rebuilt on every render, and a third
        # anchor mapping in engine.json buys nothing a resume can use.
        "scope_fix_delta_baseline_id",
        "scope_fix_delta_changed_paths",
        "scope_fix_delta_available",
        "scope_fix_delta_diagnostic",
        "scope_diff_artifact",
        "scope_undecidable",
        "scope_diagnostic",
        "scope_fallback_from_incremental",
        "self_check_round_id",
        "self_check_round_reason",
        "requirement_fingerprint",
    ):
        if key in step.inputs:
            step.outputs[key] = step.inputs[key]
    step.outputs["fix_iteration"] = fix_iteration

    project_root = resolve_flow_project_root(flow)
    from ..context_builder import get_self_check_constraint_sources

    constraint_sources = get_self_check_constraint_sources(
        project_root,
        changes_made,
        getattr(flow, "baseline_commit", None),
    )
    validation_inputs = dict(step.inputs)
    validation_inputs["project_constraints"] = constraint_sources

    changes_text = _format_changes(changes_made)
    test_text = _format_test_results(test_results)
    fix_context_text = _format_fix_context(
        fix_iteration, max_iterations,
        prev_issues=prev_issues,
        fix_history=fix_history,
    )

    prompt = SELF_CHECK_PROMPT.format(
        task_description=task_description,
        review_scope=_format_review_scope(
            step.inputs, flow_id=str(getattr(flow, "flow_id", "") or "")
        ),
        changes_made=changes_text,
        test_results=test_text,
        project_constraints=_format_project_constraints(constraint_sources),
        fix_context=fix_context_text,
    )

    # Inject the project charter (full text) + the code-index top map, replacing
    # the retired spec-name list.
    from ..context_builder import (
        get_charter_injection,
        get_code_index_injection,
        get_runtime_environment_injection,
    )
    prompt += get_charter_injection(project_root)
    # No code-index refresh here: it would mean rebuilding mid-flow right after
    # implement. The post-implement map is instead refreshed once just before
    # commit (commit.py); self_check tolerates the slightly older read-side map
    # from analyze, as it is not a precise structural check. See analyze.py.
    prompt += get_code_index_injection(project_root)

    # Append runtime environment injection if applicable
    runtime_env = get_runtime_environment_injection("self_check", project_root)
    if runtime_env:
        prompt += runtime_env

    logger.info(
        "Running self-check code review #%s/%s (fix iteration: %s, scope: %s)...",
        pass_index,
        passes_required,
        fix_iteration,
        step.inputs.get("scope_mode", "full"),
    )

    try:
        from ..chat_history import record_self_check_scope

        record_self_check_scope(
            project_root=project_root,
            flow_id=flow.flow_id,
            step_id=step.step_id,
            step_type=step.step_type.value,
            scope_metadata={
                "scope_mode": step.inputs.get("scope_mode", "full"),
                "baseline_id": step.inputs.get("baseline_id", ""),
                "scope_changed_paths": step.inputs.get("scope_changed_paths", []),
                "fix_iteration": fix_iteration,
                "round_id": step.inputs.get("self_check_round_id", ""),
                "pass_index": pass_index,
                "scope_undecidable": bool(step.inputs.get("scope_undecidable")),
                "scope_diff_artifact": step.inputs.get("scope_diff_artifact", ""),
            },
        )
        retry_count = step.inputs.get("retry_count", 0)
        caller = LLMCaller(
            project_root,
            flow_id=flow.flow_id,
            step_id=step.step_id,
            step_type=step.step_type.value,
            external_attempt=retry_count,
            fix_iteration=fix_iteration,
            # Select the per-pass agent chain when a nested
            # ``llm_caller.steps.self_check`` is configured. Each fix-loop
            # round resets pass_index to 1 (state machine), so the first
            # pass of every round re-selects the first chain.
            self_check_pass_index=pass_index,
        )
        response = caller.call(
            prompt=prompt,
            json_mode="two_phase",
            json_schema_hint=(
                '{"issues": [{"severity": "critical|high|medium|low", '
                '"actual_behavior": "...", "expected_behavior": "...", '
                '"divergence": "...", '
                '"location": "path:N", '
                '"expectation_source": {"type": "task_description|user_interjection|charter|why_comment|regression", "verbatim_quote": "..."}, '
                '"evidence_lines": ["path:N"], "missing_in": []}], '
                '"previous_issue_resolutions": [{"prev_issue_summary": "...", "status": "fixed|still_present"}], '
                '"summary": "..."}'
            ),
            required_keys=["issues"],
        )

        result = parse_json_response(response, required_keys=["issues"])

        if not result:
            step.error_message = "Failed to parse self-check result from LLM response"
            return StepStatus.FAILED

        raw_issues = result.get("issues", [])
        prev_issue_resolutions = result.get("previous_issue_resolutions", [])

        # Run structural validation; drop issues that fail any check.
        kept_issues, validation_stats = _validate_and_filter_issues(
            raw_issues, validation_inputs,
        )

        # WHY: a "still_present" verdict IS an unresolved-finding declaration.
        # Returning clean while the round's own resolutions record says a
        # previously validated finding survives would be a pass-with-finding —
        # the one outcome the charter forbids. The previous issue already
        # passed structural validation when it was first reported, so it is
        # re-admitted verbatim rather than re-validated against the newer
        # (possibly narrower) scope.
        still_present = _still_present_prev_issues(
            prev_issue_resolutions, prev_issues,
        )
        if still_present:
            before = len(kept_issues)
            kept_issues = _merge_dedup_issues(kept_issues, still_present)
            readmitted = len(kept_issues) - before
            if readmitted:
                validation_stats["readmitted_still_present_count"] = readmitted
                validation_stats["kept_count"] += readmitted
                logger.info(
                    "Self-check re-admitted %s still-present previous "
                    "issue(s) the reviewer did not re-ground in scope",
                    readmitted,
                )

        # Single-line observability log so on-call can see at a glance how
        # many issues the LLM proposed vs how many survived validation, and
        # which rejection reasons fired.
        dropped = validation_stats["input_count"] - validation_stats["kept_count"]
        if dropped > 0:
            reasons = ", ".join(
                f"{k}={v}" for k, v in validation_stats.items()
                if k.endswith("_count") and v > 0
                and k not in _NON_REJECTION_STAT_KEYS
            )
            logger.info(
                f"Self-check validation: {validation_stats['input_count']} raw → "
                f"{validation_stats['kept_count']} kept (dropped {dropped}: {reasons})"
            )

        # Use ``kept_issues`` for the fix-loop decision; preserve ``raw_issues``
        # under a separate key for debugging / audit. Existing callers that
        # read ``issues`` get the validated list to avoid revision triggers
        # from non-grounded reports.
        step.outputs["self_check_result"] = result
        step.outputs["raw_issues"] = raw_issues
        step.outputs["issues"] = kept_issues
        step.outputs["actionable_count"] = len(kept_issues)
        step.outputs["validation_stats"] = validation_stats
        step.outputs["previous_issue_resolutions"] = prev_issue_resolutions

        # Adjudication ledger accounting (fix-loop 警察). Record this round into
        # the cross-round ledger on ``flow.state.context`` BEFORE any early
        # return, so every SELF_CHECK execution — clean, deferred, or
        # fix-bound — contributes its issues. Resolutions are recorded BEFORE the
        # issues, sharing one ``round_id``: ``record_fix_resolutions`` relies on
        # ``record_self_check_round`` to register the round_id for --resume
        # idempotency, so the resolutions call must run first (while the id is
        # still absent) and the issues call second (which registers it).
        # ``step.step_id`` is stable across a --resume replay of the same PENDING
        # step, so a replayed round is not double-counted (which would corrupt
        # reproduction counting).
        try:
            if prev_issue_resolutions:
                adjudication.record_fix_resolutions(
                    flow.state.context,
                    _pair_resolutions_with_prev(prev_issue_resolutions, prev_issues),
                    round_id=step.step_id,
                )
            adjudication.record_self_check_round(
                flow.state.context, kept_issues, round_id=step.step_id,
            )
        except Exception:
            # Ledger bookkeeping is an observability side-channel; a failure here
            # must never break the review itself.
            logger.warning("Adjudication ledger recording failed", exc_info=True)

        if not kept_issues:
            if dropped > 0:
                logger.info(
                    f"Self-check #{pass_index}/{passes_required} passed "
                    f"(no validated issues; {dropped} raw issue(s) dropped by validation)"
                )
            else:
                logger.info(
                    f"Self-check #{pass_index}/{passes_required} passed (no issues found)"
                )
            # Chain-tail flush (item 1): this pass found nothing, but earlier
            # passes deferred issues. On the LAST pass, flush the accumulated
            # stash into one consolidated fix loop instead of advancing clean.
            # Deliberately NOT gated on this pass's ``defer_enabled``: the
            # threshold is re-read from tianluo.yaml per pass, and a hot-edit
            # to 0 must not let the chain end with a stash that the next
            # round's pass-1 reset would then wipe unconsumed.
            if deferred_issues and is_last_pass:
                logger.info(
                    f"Self-check #{pass_index}/{passes_required} clean but flushing "
                    f"{len(deferred_issues)} deferred issue(s) accumulated from earlier passes"
                )
                return _build_fix_outputs(
                    step, deferred_issues, fix_iteration, max_iterations,
                    pass_index, passes_required,
                )
            # Non-terminal clean pass: carry the stash forward unchanged so the
            # next pass keeps accumulating (and the last pass flushes it).
            if deferred_issues:
                step.outputs["self_check_deferred_issues"] = deferred_issues
                step.outputs["self_check_deferred"] = True
            return StepStatus.COMPLETED

        issues = kept_issues  # alias for the rest of the function

        # Defer-fix decision (item 1). Defer only when ALL hold:
        #  - deferral is enabled (threshold > 0),
        #  - there is still a subsequent nested pass to run (not the last pass),
        #  - this pass found FEWER than ``threshold`` issues, and
        #  - none of this pass's issues is critical/high severity.
        # Otherwise enter fix immediately, merging any earlier deferred issues so
        # ``fix_instructions`` carries the FULL accumulated set.
        if defer_enabled:
            should_defer = (
                not is_last_pass
                and len(issues) < defer_threshold
                and not _has_critical_or_high(issues)
            )
            if should_defer:
                merged = _merge_dedup_issues(deferred_issues, issues)
                step.outputs["self_check_deferred_issues"] = merged
                step.outputs["self_check_deferred"] = True
                logger.info(
                    f"Self-check #{pass_index}/{passes_required} deferring "
                    f"{len(issues)} non-critical issue(s) "
                    f"(< threshold {defer_threshold}); {len(merged)} total deferred, "
                    f"continuing to next pass"
                )
                return StepStatus.COMPLETED
            issues = _merge_dedup_issues(deferred_issues, issues)
        elif deferred_issues:
            # Deferral was hot-disabled for THIS pass, but a stash accumulated
            # by earlier passes must still join the fix loop — merging it here
            # (the ``defer_enabled`` block is skipped) is what keeps a
            # threshold hot-edit from orphaning the stash.
            issues = _merge_dedup_issues(deferred_issues, issues)

        return _build_fix_outputs(
            step, issues, fix_iteration, max_iterations,
            pass_index, passes_required,
        )

    except Exception as e:
        logger.exception("Self-check step failed")
        step.error_message = f"Self-check failed: {str(e)}"
        return StepStatus.FAILED


_DESC_STOPWORDS = frozenset({
    "a", "an", "the", "of", "on", "in", "at", "to", "for", "from",
    "is", "are", "be", "been", "by", "with", "and", "or", "not",
    "was", "were", "has", "have", "had", "this", "that", "these", "those",
    "can", "could", "should", "would", "may", "might", "will", "shall",
    "as", "but", "so", "if", "it", "its", "do", "does", "did", "no",
})
_DESC_PUNCT_RE = re.compile(r"[^\w\s]+")


def _normalize_description(text: str) -> str:
    """Normalize free-text issue descriptions for mechanical deduplication.

    Lowercases, strips punctuation, drops common English stopwords, and sorts
    the remaining tokens. This makes minor LLM paraphrasing (different word
    order, inserted punctuation, added articles) compare equal, so the
    the deduplication key is not defeated by trivial wording changes.
    """
    lower = text.lower()
    cleaned = _DESC_PUNCT_RE.sub(" ", lower)
    tokens = [t for t in cleaned.split() if t and t not in _DESC_STOPWORDS]
    tokens.sort()
    return " ".join(tokens)


def _issue_signature(issues: list) -> set:
    """Compute ``(location, normalized_description)`` issue identities.

    Location is stripped and lowercased. Description is token-normalized via
    _normalize_description so LLM paraphrasing of the same logical issue still
    hashes to the same signature.

    Schema compatibility: handles both the new schema (``evidence_lines`` /
    ``actual_behavior`` / ``divergence``) and the legacy schema
    (``location`` / ``description``). New-schema reads pick the first
    evidence_line (or first missing_in entry) for the location component
    and concatenate ``actual_behavior`` + ``divergence`` for the
    description component.
    """
    sigs = set()
    for i in issues:
        if not isinstance(i, dict):
            continue
        # Location: prefer new schema's evidence_lines[0] / missing_in[0];
        # fall back to legacy ``location``.
        loc_raw = ""
        evidence = i.get("evidence_lines") or []
        if isinstance(evidence, list) and evidence:
            for ev in evidence:
                if isinstance(ev, str) and ev.strip():
                    loc_raw = ev
                    break
        if not loc_raw:
            missing = i.get("missing_in") or []
            if isinstance(missing, list) and missing:
                for m in missing:
                    if isinstance(m, str) and m.strip():
                        loc_raw = m
                        break
        if not loc_raw:
            loc_raw = str(i.get("location", ""))
        loc = loc_raw.strip().lower()
        # Description: prefer new schema's ``actual_behavior`` + ``divergence``;
        # fall back to legacy ``description``.
        new_parts = [
            str(i.get("actual_behavior", "")).strip(),
            str(i.get("divergence", "")).strip(),
        ]
        new_desc = " ".join(p for p in new_parts if p)
        desc_raw = new_desc or str(i.get("description", ""))
        desc = _normalize_description(desc_raw)
        if loc or desc:
            sigs.add((loc, desc))
    return sigs


def _merge_dedup_issues(existing: list, incoming: list) -> list:
    """Merge ``incoming`` issues into ``existing``, dropping mechanical dups.

    Reuses :func:`_issue_signature` (``(location, normalized_description)``) as
    the dedup key: an incoming issue whose signature already appears in the
    accumulated set is dropped. Issues that produce no signature (no usable
    location/description) cannot be deduped and are always kept. ``existing`` is
    assumed already-deduped; its order is preserved and survivors of
    ``incoming`` are appended in order. The residual semantic near-duplicates
    are left for the implement step to merge when it consumes the list.
    """
    result = list(existing)
    seen = _issue_signature(existing)
    for issue in incoming:
        sigs = _issue_signature([issue])
        if sigs and sigs <= seen:
            continue
        result.append(issue)
        seen |= sigs
    return result


def _still_present_prev_issues(resolutions: list, prev_issues: list | None) -> list:
    """Previous issues the reviewer verdicted as ``still_present``.

    INVARIANT: a ``still_present`` verdict ALWAYS puts its previous finding
    back into the fix loop — a round must never close clean while its own
    resolutions record declares a finding unresolved (charter: a check step's
    findings have exactly one destination, the fix loop).

    Positional pairing (``_pair_resolutions_with_prev``) is the precise path,
    but it deliberately declines to pair on cardinality drift or a mismatched
    summary. An unpaired ``still_present`` verdict is therefore resolved by
    content — the unambiguously best-matching previous issue — and, when even
    that is indecisive, the round fails CLOSED: every previous issue no
    resolution confidently accounted for is re-admitted. Re-checking an
    already-fixed finding costs one round; dropping a live one loses it.
    """
    prev = [item for item in (prev_issues or []) if isinstance(item, dict) and item]
    paired = [res for res in _pair_resolutions_with_prev(resolutions, prev_issues)
              if isinstance(res, dict)]

    def is_still_present(res: dict) -> bool:
        return str(res.get("status", "")).strip().lower() == "still_present"

    out: list = []
    claimed: set = set()
    unpaired: list = []
    # Confidently paired verdicts are settled FIRST — whatever their status,
    # they account for their previous issue, so a later content match can
    # neither steal one nor let the fail-closed sweep re-admit it.
    for res in paired:
        issue = res.get("issue")
        if isinstance(issue, dict) and issue:
            claimed.add(id(issue))
            if is_still_present(res):
                out.append(issue)
        elif is_still_present(res):
            unpaired.append(res)

    unresolved_verdict = False
    for res in unpaired:
        match = _match_resolution_by_content(res, prev, claimed)
        if match is not None:
            claimed.add(id(match))
            out.append(match)
        else:
            unresolved_verdict = True
    if unresolved_verdict:
        for issue in prev:
            if id(issue) not in claimed:
                out.append(issue)
    # The caller merges this into the round's kept issues with the shared
    # signature dedup, so no second dedup rule is introduced here.
    return out


def _match_resolution_by_content(
    resolution: dict, prev_issues: list, claimed: set
) -> dict | None:
    """The one previous issue a resolution's summary unambiguously describes.

    Returns ``None`` for a zero-signal or tied summary: guessing there would
    push the wrong finding into the fix loop, and the caller's fail-closed
    sweep covers that case without a guess.
    """
    tokens = _identity_tokens(resolution.get("prev_issue_summary", ""))
    if not tokens:
        return None
    best: dict | None = None
    best_score = 0
    tied = False
    for issue in prev_issues:
        if id(issue) in claimed:
            continue
        score = len(tokens & _issue_identity_tokens(issue))
        if score > best_score:
            best, best_score, tied = issue, score, False
        elif score == best_score and score > 0:
            tied = True
    if best is None or best_score <= 0 or tied:
        return None
    return best


def _has_critical_or_high(issues: list) -> bool:
    """Return True if any issue is critical/high severity (case-insensitive)."""
    for i in issues:
        if not isinstance(i, dict):
            continue
        if str(i.get("severity", "")).strip().lower() in ("critical", "high"):
            return True
    return False


def _build_fix_outputs(
    step: Step,
    issues: list,
    fix_iteration: int,
    max_iterations: int,
    pass_index: int,
    passes_required: int,
) -> StepStatus:
    """Populate the fix-loop outputs from ``issues`` and return REVISION_NEEDED.

    Shared by the immediate-fix path, the threshold/critical-high terminal
    path, and the chain-tail flush path so the consolidated ``fix_instructions``
    always lists the FULL accumulated issue set.
    """
    iter_display = format_fix_iteration_display(fix_iteration, max_iterations)
    logger.warning(
        f"Self-check #{pass_index}/{passes_required} entering fix with "
        f"{len(issues)} issue(s) (fix iteration {iter_display})"
    )

    issue_details = "\n".join(
        f"- [{i.get('severity', 'high')}] {_describe_issue(i)}"
        for i in issues
    )
    fix_instructions = (
        f"Self-check found {len(issues)} issue(s) that need fixing:\n"
        f"{issue_details}\n\n"
        "Fix the issues listed above and ensure the logic is correct."
    )

    # Reflect the consolidated set so the renderer, ``actionable_count``, and
    # the next round's ``prev_self_check_issues`` all see every issue that fed
    # the fix loop (not just this pass's own findings).
    step.outputs["issues"] = issues
    step.outputs["actionable_count"] = len(issues)
    step.outputs["fix_needed"] = True
    step.outputs["fix_iteration"] = fix_iteration
    # ``max_fix_iterations <= 0`` is the unlimited sentinel.
    step.outputs["max_fix_iterations"] = max_iterations
    step.outputs["fix_instructions"] = fix_instructions
    step.outputs["fix_context"] = {
        "reason": "self_check",
        "issues": issues,
        "iteration": fix_iteration + 1,
    }
    # The accumulated set has been consumed into the fix loop; clear the stash
    # so a downstream reader cannot mistake it for still-pending deferrals.
    step.outputs["self_check_deferred_issues"] = []
    return StepStatus.REVISION_NEEDED


def _identity_tokens(text: str) -> set:
    """Lowercased, deduplicated word tokens (length > 2) of ``text``.

    Length-2 filtering drops pure ordinals/opcode noise (``a``, ``x``, ``to``)
    that carries no discriminating signal, so the summary↔issue overlap score
    below keys off substantive words only.
    """
    norm = _normalize_for_quote_match(text or "").lower()
    return {t for t in re.split(r"[^a-z0-9]+", norm) if len(t) > 2}


def _issue_identity_tokens(issue: dict) -> set:
    """Discriminating word tokens that identify a previous issue.

    Drawn from the fields a ``prev_issue_summary`` paraphrase would naturally
    echo — verbatim_quote, expected/actual behavior, divergence, and the
    evidence file path (line number stripped) — so a summary that describes a
    *different* previous issue scores higher against that issue than against its
    positional partner.
    """
    if not isinstance(issue, dict):
        return set()
    source = issue.get("expectation_source") or {}
    quote = source.get("verbatim_quote", "") if isinstance(source, dict) else ""
    parts = [
        quote,
        issue.get("expected_behavior", ""),
        issue.get("actual_behavior", ""),
        issue.get("divergence", ""),
    ]
    evidence = issue.get("evidence_lines") or []
    if isinstance(evidence, list):
        for entry in evidence:
            if isinstance(entry, str) and entry.strip():
                parts.append(entry.rsplit(":", 1)[0] if ":" in entry else entry)
    return _identity_tokens(" ".join(p for p in parts if isinstance(p, str)))


def _pair_resolutions_with_prev(
    resolutions: list, prev_issues: list | None
) -> list:
    """Pair each ``previous_issue_resolution`` with the previous issue it refers to.

    The raw ``previous_issue_resolutions`` schema carries only a prose paraphrase
    (``prev_issue_summary`` + ``status``) with no machine-readable identity, but
    the prompt requires exactly one entry per previously-reported issue, in order
    (SELF_CHECK_PROMPT: "For EACH previously-reported issue, include exactly one
    entry"). Reuniting each verdict with the full prev-issue dict lets the
    adjudication ledger fingerprint it — trigger (b) ("打脸") reads these
    ``fixed`` verdicts back and compares them by fingerprint against the current
    round.

    Positional pairing is only trustworthy when the reviewer returned EXACTLY one
    resolution per previous issue AND kept them in order. Nothing enforces either
    half of the contract:

    * Cardinality drift (an omitted/extra entry) shifts every index, so when the
      counts differ we DO NOT pair by position at all.
    * Even at matching counts the reviewer may REORDER entries. Trusting the
      index blindly would then stamp a ``fixed`` verdict about issue #2 onto
      issue #1's fingerprint — spuriously firing trigger (b) for one issue while
      masking the real 打脸 for the other. So a positional partner is accepted
      only when it is a (co-)best content match for the resolution's summary:
      ``prev_issue_summary`` token overlap against its positional partner must be
      at least as strong as against every other previous issue. A summary that
      clearly describes a *different* previous issue leaves its positional
      partner unpaired (empty fingerprint, no trigger weight) rather than record
      a wrong-issue ``fixed`` verdict. Zero-signal summaries score 0 against all
      issues, so the positional partner ties as best and the in-order contract is
      honored.

    A resolution that already carries its own machine-identified ``issue`` keeps
    it regardless.
    """
    prev = prev_issues or []
    res_list = [r for r in (resolutions or []) if isinstance(r, dict)]
    counts_match = bool(prev) and len(res_list) == len(prev)
    prev_tokens = (
        [_issue_identity_tokens(p) for p in prev] if counts_match else []
    )
    paired: list = []
    for i, res in enumerate(res_list):
        entry = dict(res)
        if counts_match and "issue" not in entry and isinstance(prev[i], dict):
            summary_tokens = _identity_tokens(res.get("prev_issue_summary", ""))
            scores = [len(summary_tokens & pt) for pt in prev_tokens]
            # Accept the positional partner only when no OTHER previous issue is a
            # strictly better content match (reorder guard). ``>= max`` keeps the
            # zero-signal / tied case pairing by position per the prompt contract.
            if scores[i] >= max(scores):
                entry["issue"] = prev[i]
        paired.append(entry)
    return paired


def _format_changes(changes_made: dict[str, Any]) -> str:
    if not changes_made:
        return "No changes recorded."

    lines = []
    for file_change in changes_made.get("files_changed", []):
        if isinstance(file_change, str):
            lines.append(f"- modified: {file_change}")
        elif isinstance(file_change, dict):
            path = file_change.get("path", "?")
            action = file_change.get("action", "?")
            explanation = file_change.get("explanation", "")
            lines.append(f"- {action}: {path}")
            if explanation:
                lines.append(f"  ({explanation})")
        else:
            lines.append(f"- {file_change}")

    return "\n".join(lines) if lines else "Changes made but details unavailable."


def _format_test_results(test_results: dict[str, Any]) -> str:
    if not test_results:
        return "No test results available."

    lines = []

    if "phases" in test_results:
        overall = test_results.get("overall_passed", False)
        lines.append(f"Overall passed: {overall}")

        new_tests = test_results.get("new_tests", {})
        if new_tests.get("count", 0) > 0:
            lines.append(f"\nNew tests ({new_tests['count']}):")
            for t in new_tests.get("failed", []):
                lines.append(f"  FAILED: {t}")
            lines.append(
                f"  Passed: {len(new_tests.get('passed', []))}, "
                f"Failed: {len(new_tests.get('failed', []))}"
            )

        regression = test_results.get("regression", {})
        if regression.get("failed"):
            lines.append("\nRegression failures:")
            for t in regression["failed"]:
                lines.append(f"  FAILED: {t}")

        for phase in test_results["phases"]:
            name = phase.get("name", "?")
            passed = phase.get("passed", False)
            lines.append(
                f"\nPhase '{name}': {'PASSED' if passed else 'FAILED'} "
                f"(exit code: {phase.get('returncode', '?')})"
            )
            stdout = phase.get("stdout", "")
            if stdout and not passed:
                lines.append(f"Output (last {PHASE_STDOUT_TAIL_CHARS} chars):\n{stdout[-PHASE_STDOUT_TAIL_CHARS:]}")
            stderr = phase.get("stderr", "")
            if stderr and not passed:
                lines.append(f"Stderr (last {PHASE_STDERR_TAIL_CHARS} chars):\n{stderr[-PHASE_STDERR_TAIL_CHARS:]}")

        return "\n".join(lines)

    passed = test_results.get("passed", False)
    returncode = test_results.get("returncode", "?")
    lines.append(f"Tests passed: {passed} (exit code: {returncode})")

    stdout = test_results.get("stdout", "")
    if stdout:
        lines.append(f"\nTest output:\n{stdout[-PHASE_STDOUT_TAIL_CHARS:]}")

    stderr = test_results.get("stderr", "")
    if stderr:
        lines.append(f"\nError output:\n{stderr[-PHASE_STDERR_TAIL_CHARS:]}")

    return "\n".join(lines)


def _format_fix_context(
    fix_iteration: int,
    max_iterations: int,
    prev_issues: list | None = None,
    fix_history: list | None = None,
) -> str:
    """Format fix context for inclusion in the self_check prompt.

    Thin wrapper around the shared ``render_fix_context`` helper so the
    fix-loop copy has a single source of truth. prev_issues are rendered
    inline here (self_check has no separate "Previous Verification" slot in
    its prompt).
    """
    return render_fix_context(
        fix_iteration,
        max_iterations,
        step_label="self-check",
        prev_issues=prev_issues,
        fix_history=fix_history,
    )
