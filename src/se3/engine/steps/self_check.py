"""Self Check step handler.

LLM-based code review after tests pass. Checks logic completeness,
robustness, and test coverage gaps — explicitly excludes spec compliance
(that's verify_spec's job).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from pathlib import Path
from typing import Any

from .. import adjudication
from ..llm_caller import LLMCaller
from ..models import FlowInstance, Step, StepStatus
from ..prompt_markers import inject_boundary
from ..truncation import (
    PHASE_STDERR_TAIL_CHARS,
    PHASE_STDOUT_TAIL_CHARS,
    SELF_CHECK_TASK_GROUPS_MAX_CHARS,
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
#     project's task_description / non-base spec_content / a plan task's
#     description or acceptance criterion (type=="plan_task"). The handler
#     normalizes both sides identically (unicode NFKC, smart-quote
#     replacement, whitespace collapse, literal ``\n`` → real newline)
#     before comparison so the LLM cannot rely on cosmetic drift. The one
#     exception is type=="regression": it protects pre-existing,
#     out-of-scope behavior absent from the source pool, so it skips the
#     quote check and must instead ground on evidence_lines.
#   - evidence_lines pointing at changes_made.files_changed paths, or
#     missing_in for "should-have-been-edited but wasn't" cases.
#   - out_of_scope=True as an explicit release valve for non-actionable
#     observations; handler discards out_of_scope items with telemetry.
#
# Items failing any check are dropped from the issue list and tallied in
# ``validation_stats`` (also surfaced via outputs and a single log line)
# for post-hoc inspection of LLM behavior.

_EVIDENCE_LINE_RE = re.compile(r"^[\w/.\-]+:\d+$")


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
    2. ``original_task_description`` — the canonical pre-discovery user
       input, when discovery produced a refined override. EXCLUDED when an
       adjudication ruling is in effect (see ``adjudicated_description``
       below): the covering patch abolished the contradictory clause, so the
       superseded original/refined text must leave the pool — otherwise a new
       issue re-quoting the abolished clause would substring-match it and slip
       past validation. The base entry (#1) already carries the adjudicated
       text, so dropping the original is all that's needed.
    3. Each entry of ``user_interjections`` — its ``text`` field added
       individually, so an LLM that wants to cite a specific Ctrl-C
       instruction can substring-match against the bare interjection
       text (NOT against our ``## Additional Instructions`` boilerplate
       header which would otherwise be a free-pass quote).
    4. Project-specific spec_content entries — excluding ``base`` (its
       content is generic project boilerplate like PEP 8 conventions
       that any nit can hang off of).
    5. ``task_groups`` — each plan task's ``description`` and every
       ``acceptance_criteria`` entry, added individually so a
       ``plan_task`` expectation can substring-match the specific task
       text it audits (the Per-Task Correctness dimension). Without this,
       plan-grounded correctness issues would have no source-pool entry
       and be silently dropped.
    """
    pool: list[str] = []

    # An adjudication ruling replaces the effective task_description with a
    # covering patch; when in effect the pre-ruling original/refined text still
    # carries the abolished clause and must NOT stay in the pool.
    adjudicated = step_inputs.get("adjudicated_description")
    adjudication_in_effect = isinstance(adjudicated, str) and bool(adjudicated)

    # Prefer the clean base; fall back to the composed task_description
    # for older inputs (e.g. unit tests or pre-upgrade resumes). The base is
    # already the adjudicated text when a ruling is in effect (both resolve
    # through ``state_machine._effective_task_description_base``).
    base = step_inputs.get("task_description_base")
    if not (isinstance(base, str) and base):
        base = step_inputs.get("task_description")
    if isinstance(base, str) and base:
        pool.append(base)

    # Skip the superseded original once adjudicated: its clause was ruled out.
    if not adjudication_in_effect:
        orig = step_inputs.get("original_task_description")
        if isinstance(orig, str) and orig:
            pool.append(orig)

    interjections = step_inputs.get("user_interjections") or []
    if isinstance(interjections, list):
        for entry in interjections:
            if not isinstance(entry, dict):
                continue
            text = entry.get("text")
            if isinstance(text, str) and text.strip():
                pool.append(text)

    spec_content = step_inputs.get("spec_content") or {}
    if isinstance(spec_content, dict):
        for spec_name, content in spec_content.items():
            if spec_name == "base":
                continue
            if isinstance(content, str) and content:
                pool.append(content)

    # Plan task text: each task's description + acceptance_criteria, added
    # individually so a ``plan_task`` quote can pin down the one task it audits.
    task_groups = step_inputs.get("task_groups") or []
    if isinstance(task_groups, list):
        for group in task_groups:
            if not isinstance(group, dict):
                continue
            tasks = group.get("tasks") or []
            if not isinstance(tasks, list):
                continue
            for task in tasks:
                if not isinstance(task, dict):
                    continue
                desc = task.get("description")
                if isinstance(desc, str) and desc.strip():
                    pool.append(desc)
                criteria = task.get("acceptance_criteria") or []
                if isinstance(criteria, list):
                    for c in criteria:
                        if isinstance(c, str) and c.strip():
                            pool.append(c)
    return pool


def _changed_paths(step_inputs: dict) -> set[str]:
    """Set of paths from ``changes_made.files_changed`` for evidence_lines
    validation. Returns an empty set if the field is missing or malformed.
    """
    out: set[str] = set()
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


def _validate_evidence(
    issue: dict, changed: set[str], *, require_changed_line: bool = False
) -> bool:
    """Return True when the issue carries usable evidence — at least one
    well-formed ``path:N`` entry whose path is in ``changed``, OR at
    least one non-empty ``missing_in`` entry. ``missing_in`` covers
    "the implementation should have edited X but didn't" cases that
    naturally cannot point at a changed line.

    When ``require_changed_line`` is set the ``missing_in`` fallback is
    disabled: only a real changed-line entry counts. Regression issues use
    this — a regression claims a change BROKE existing behavior, so it must
    point at the changed line(s) responsible; a ``missing_in`` (a file that
    was never edited) is self-contradictory grounding for a regression and
    would leave the implement step with no concrete location to fix.
    """
    evidence = issue.get("evidence_lines") or []
    if isinstance(evidence, list):
        for ev in evidence:
            if not isinstance(ev, str):
                continue
            ev = ev.strip()
            if not _EVIDENCE_LINE_RE.match(ev):
                continue
            path = ev.rsplit(":", 1)[0]
            if path in changed:
                return True
    if require_changed_line:
        return False
    missing_in = issue.get("missing_in") or []
    if isinstance(missing_in, list):
        for mp in missing_in:
            if isinstance(mp, str) and mp.strip():
                return True
    return False


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

    1. ``out_of_scope == True`` → drop (counted in ``out_of_scope_count``)
    2. ``verbatim_quote`` empty after normalize → drop
    3. ``verbatim_quote`` not a substring of any source-pool entry → drop
    4. ``evidence_lines`` / ``missing_in`` provides no real grounding → drop
    5. ``actual_behavior`` / ``expected_behavior`` / ``divergence`` empty → drop

    Exception: ``expectation_source.type == "regression"`` issues are exempt
    from the verbatim-quote checks (2 & 3). A regression protects PRE-EXISTING
    behavior outside the task's textual scope, which by definition has no entry
    in the source pool; grounding it on a quote would be impossible. Instead the
    evidence grounding (4) is mandatory — the issue must point at the changed
    line(s) responsible. ``plan_task`` issues use the ordinary quote path: the
    source pool now includes task_groups text, so their quote matches there.
    """
    stats = {
        "input_count": 0,
        "kept_count": 0,
        "out_of_scope_count": 0,
        "empty_quote_count": 0,
        "quote_not_in_source_count": 0,
        "bad_evidence_count": 0,
        "empty_field_count": 0,
        "non_dict_count": 0,
    }
    kept: list[dict] = []

    if not isinstance(raw_issues, list):
        return kept, stats

    pool_normalized = [_normalize_for_quote_match(s) for s in _build_source_pool(step_inputs)]
    pool_normalized = [p for p in pool_normalized if p]
    changed = _changed_paths(step_inputs)

    for issue in raw_issues:
        stats["input_count"] += 1
        if not isinstance(issue, dict):
            stats["non_dict_count"] += 1
            continue
        if issue.get("out_of_scope") is True:
            stats["out_of_scope_count"] += 1
            # 留痕: an out_of_scope observation is dropped (not filed as an
            # issue, to avoid issue explosion), but its substance must not
            # disappear silently. Log the description plus whatever evidence
            # the LLM supplied so a real signal stays recoverable from logs.
            description = (
                issue.get("description")
                or issue.get("title")
                or issue.get("actual_behavior")
                or "(no description)"
            )
            logger.info(
                "self_check dropped out_of_scope issue (logged, not filed): "
                "%s | evidence_lines=%s | missing_in=%s",
                description,
                issue.get("evidence_lines") or [],
                issue.get("missing_in") or [],
            )
            continue

        source = issue.get("expectation_source") or {}
        src_type = source.get("type", "") if isinstance(source, dict) else ""
        quote = (
            source.get("verbatim_quote", "")
            if isinstance(source, dict) else ""
        )

        # Regression issues protect pre-existing, out-of-scope behavior that has
        # no entry in the verbatim source pool — so they bypass the quote checks
        # and rely solely on diff-evidence grounding below.
        if src_type != "regression":
            norm_quote = _normalize_for_quote_match(quote)
            if not norm_quote:
                stats["empty_quote_count"] += 1
                continue
            # _format_task_groups renders each task as a prompt-visible line
            # "- [<id>] <description>", capping an oversized description with a
            # trailing ellipsis under budget pressure. The source pool holds only
            # the raw, un-decorated description, so a reviewer who copies the
            # visible line verbatim carries BOTH a leading bullet/id prefix and a
            # trailing ellipsis that the pool lacks. Build relaxed candidates that
            # strip each so the full task still substring-matches.
            candidates = [norm_quote]
            trimmed = norm_quote.rstrip(". ").rstrip()
            if trimmed and trimmed != norm_quote:
                candidates.append(trimmed)
            for c in list(candidates):
                unprefixed = re.sub(r"^-\s*(\[[^\]]*\]\s*)?", "", c)
                if unprefixed and unprefixed != c:
                    candidates.append(unprefixed)
            if not any(c in p for c in candidates for p in pool_normalized):
                stats["quote_not_in_source_count"] += 1
                continue

        if not _validate_evidence(
            issue, changed, require_changed_line=(src_type == "regression")
        ):
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


SELF_CHECK_PROMPT = """You are an expert code reviewer. Review the implementation for logic completeness, robustness, and potential issues that tests may not have caught.

## Task Description
{task_description}

## Changes Made
{changes_made}

## Test Results
{test_results}

## Specifications (for context only)
{spec_content}
{task_groups_section}
## Fix Context
{fix_context}

## Review Dimensions

Focus your review on these dimensions. Do NOT check spec compliance — that is handled by a separate verification step.

1. **Per-Task Correctness (HARD AUDIT)**: Using the **Plan Task Groups** section below as the authoritative task list, audit EACH planned task one by one against the actual implementation. The **Changes Made** section above lists only the changed *paths*, not the line-level edits — that file list alone is NOT sufficient to judge correctness. Therefore, BEFORE you judge any task, you MUST obtain the actual diff yourself via your worktree tools (run `git diff`, and read the changed files as needed); base every correctness verdict on the real diff/file contents, not on the path list. For every task, infer from that diff which changes are meant to implement it, then verify it was implemented *correctly* — the described behavior is present, the acceptance_criteria are satisfied, and the logic is right. A task that is missing, only partially implemented, or implemented with wrong logic IS an issue — file it with `expectation_source.type = "plan_task"`, quoting the task description or an acceptance criterion. Grounding rule for this dimension (the handler drops issues that violate it, so it is mandatory): for a task that is partially or incorrectly implemented, point `evidence_lines` at the relevant changed line(s); for a task that is ENTIRELY unimplemented there are no changed lines to cite, so you MUST instead list the file(s) / integration point(s) that should have been created or edited for that task under `missing_in` (leaving `evidence_lines` empty is fine in that case). Do NOT drop a wholly-missing task just because it has no diff line — `missing_in` is exactly how a "task not implemented at all" finding is grounded. This is a strict per-task audit, not a loose sanity check; do NOT excuse a real gap as an acceptable deviation.
2. **Regression / Unintended Side Effects**: Verify the change did NOT break or alter existing behavior OUTSIDE the scope of the planned tasks. Look for: existing functions whose contract or return value changed, shared helpers whose behavior shifted for their other callers, removed/renamed symbols still referenced elsewhere, altered defaults, or side effects leaking beyond the task boundary. A behavioral change to out-of-scope existing behavior IS an issue — file it with `expectation_source.type = "regression"` and ground it in `evidence_lines` pointing at the changed line(s) responsible. This dimension is orthogonal to Per-Task Correctness above.
3. **Logic Completeness**: Are there unhandled boundary conditions, missing error paths, or incomplete control flow? Look for edge cases the implementation should handle but doesn't.
4. **Code Robustness**: Is exception handling adequate? Are resources properly managed (files, connections, locks)? Are there concurrency safety issues?
5. **Functional Gaps**: Are there related modules that should have been modified but weren't? Are there integration points that were missed?
6. **Test Coverage Gaps**: Based on the test results, which logic paths are NOT exercised by existing tests? Are there critical paths that lack test coverage?

## What NOT to check
- **Spec compliance** — this is handled by the verify_spec step, do NOT duplicate that check.
- **Code style or formatting** — not actionable here.
- **Performance optimization suggestions** — only flag if there's a clear correctness issue.
- **Anything a dedicated downstream step owns** — as a fix-loop checker you MUST NOT report concerns that a later specialized step decides; that creates standoffs where implement cannot resolve them. In particular, the version number and version files (e.g. the `version` field in `pyproject.toml`) — whether and how to bump them — are decided by the downstream `version_analyze` step against the pre-session baseline; do NOT report "version not bumped" or "version number is wrong" as an issue.

## Severity Levels
- **critical**: Logic error that will cause incorrect behavior, data corruption, or crashes in normal usage paths.
- **high**: Missing error handling or boundary condition that will cause failures in reasonably common scenarios.
- **medium**: Defensive improvement that would prevent issues in edge cases, or a minor gap in test coverage.
- **low**: Nice-to-have improvement, minor robustness enhancement, or additional test suggestion.

## Issue Schema (HARD requirements — handler validates and drops violators)

Each issue MUST be a JSON object with these fields:

- `severity`: one of "critical" / "high" / "medium" / "low"
- `actual_behavior`: what the code in `changes_made` currently does (concrete, observable; non-empty)
- `expected_behavior`: what the code SHOULD do (non-empty)
- `divergence`: under what specific input / sequence / state does `actual_behavior` produce a wrong result (concrete failure scenario; non-empty)
- `expectation_source`: where "should do" comes from. Must be:
    {{ "type": "task_description" | "spec" | "user_interjection" | "plan_task" | "regression",
       "verbatim_quote": "<a literal substring of the grounding text — see rules below>" }}
  Grounding rules by type (the handler enforces these and drops violators):
    - `task_description` / `spec` / `user_interjection` / `plan_task`: `verbatim_quote` MUST be a literal substring of the project's task_description, a non-base spec, a user interjection, or — for `plan_task` — a **Plan Task Groups** task description / acceptance criterion above. The handler normalizes both the quote and the source pool (NFKC + smart-quote replacement + whitespace collapse + literal `\\n` → newline) and drops any issue whose normalized quote is not a substring of any normalized source-pool entry. Quote a substantive phrase, NOT a single generic noun.
    - `regression`: use for the Regression dimension, where the violated expectation is PRE-EXISTING behavior outside the task scope (it has no entry in the text above). `verbatim_quote` is NOT substring-checked for this type; instead you MUST ground the issue in `evidence_lines` pointing at the changed line(s) that broke the behavior.
- `evidence_lines`: array of `"path:N"` strings, where `path` MUST appear in `changes_made.files_changed` (the handler verifies). At least one entry required UNLESS `missing_in` is non-empty.
- `missing_in`: array of file paths that should have been edited/created but were not. Use this for any issue that cannot point at a changed line — "missed integration point" issues AND a planned `plan_task` that is ENTIRELY unimplemented (list the file(s) that should have carried it).
- `out_of_scope`: boolean. Set to `true` if the concern is a suggestion / observation rather than an actionable bug — the handler will discard out_of_scope items, so this is the correct release valve. Do NOT downgrade observations to `low` severity; use this field instead.

## Previous Issue Resolutions (HARD requirement when prev_issues are listed)

If the Fix Context above includes "Previously Reported Issues", you MUST emit a `previous_issue_resolutions` array. For EACH previously-reported issue, include exactly one entry:
  {{ "prev_issue_summary": "<short paraphrase identifying which prev issue>",
     "status": "fixed" | "still_present" }}
- "fixed" — the change in `changes_made` resolves it; do NOT also list it again under `issues`.
- "still_present" — the change did not resolve it; ALSO list it again under `issues` with full schema.

## Soft guidance (handler does not enforce, but this is the team's preference)

- If you're unsure whether something is a real bug or just a preference, prefer `out_of_scope=true` over a low-severity issue.
- Avoid tentative phrasing in `actual_behavior` / `expected_behavior` / `divergence` ("could fail", "may not handle", "consider", "observation only"). State the failure as a concrete fact ("returns 0 instead of None when X", "raises KeyError when Y", "silently overwrites Z").
- A `verbatim_quote` of one or two generic words is unlikely to ground a real issue. Quote a substantive phrase that pins down what the user actually asked for.

Respond in JSON format:
```json
{{
    "issues": [
        {{
            "severity": "critical|high|medium|low",
            "actual_behavior": "...",
            "expected_behavior": "...",
            "divergence": "...",
            "expectation_source": {{
                "type": "task_description",
                "verbatim_quote": "..."
            }},
            "evidence_lines": ["src/foo.py:42"],
            "missing_in": [],
            "out_of_scope": false
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
# self_check consumes upstream artifacts (changes_made / test_results /
# task_groups / spec_content); no user-literal field is appended here. The
# web console renders the whole post-BEGIN tail inside the collapsed
# system-prompt chip.
SELF_CHECK_PROMPT = inject_boundary(SELF_CHECK_PROMPT, "## Task Description\n")


_TASK_GROUPS_SECTION_INTRO = (
    "## Plan Task Groups (Authoritative Task List — HARD AUDIT)\n\n"
    "The following is the plan's task breakdown (task_groups). Treat it as the "
    "**authoritative list of what this change was required to implement**, and "
    "audit it STRICTLY (this drives the **Per-Task Correctness** dimension):\n"
    "- Go through EACH task below one by one. Infer from the changed files / diff "
    "which changes are meant to implement that task, then verify it was implemented "
    "**correctly**: the described behavior is present, its acceptance_criteria are "
    "satisfied, and the logic is right.\n"
    "- A task that is missing, only partially implemented, or implemented with wrong "
    "logic IS an issue. File it with `expectation_source.type = \"plan_task\"`, "
    "quoting the task description or an acceptance criterion verbatim.\n"
    "- Grounding (mandatory — the handler drops ungrounded issues): for a partially "
    "or incorrectly implemented task, point `evidence_lines` at the relevant changed "
    "line(s). For a task that is ENTIRELY unimplemented there are no changed lines to "
    "cite, so you MUST instead list the file(s) / integration point(s) that should "
    "have been created or edited under `missing_in` (an empty `evidence_lines` is "
    "fine in that case). A wholly-missing task is grounded via `missing_in`, never "
    "dropped for lack of a diff line.\n"
    "- Do NOT excuse an unrealized or incorrectly-realized task as an acceptable "
    "deviation. If a planned task is not correctly implemented, report it.\n\n"
)


# Appended when the full task_groups render overflows the budget and detail
# had to be trimmed. The per-task audit needs every planned task to be visible;
# this tells the reviewer that detail (not whole tasks) was dropped and exactly
# how to recover the untruncated list, so a large plan can never pass the audit
# with tasks silently hidden.
_TASK_GROUPS_TRIM_NOTE = (
    "\n\n> NOTE: per-task detail was trimmed to fit the prompt budget, but "
    "EVERY planned task above is still listed (by id/description). Before "
    "concluding the per-task audit, retrieve the full, untruncated task_groups "
    "(complete descriptions + acceptance_criteria) via your worktree tools "
    "(e.g. read the plan output, or `se3 history show <flow_id>`)."
)


def _count_tasks(task_groups: list) -> int:
    """Count well-formed (dict) tasks across all groups, for budget sharing."""
    n = 0
    for group in task_groups:
        if not isinstance(group, dict):
            continue
        tasks = group.get("tasks") or []
        if isinstance(tasks, list):
            n += sum(1 for t in tasks if isinstance(t, dict))
    return n


def _render_task_groups(
    task_groups: list, *, include_ac: bool, desc_cap: int | None = None
) -> str:
    """Render task_groups as a Markdown summary at a chosen detail level.

    ``include_ac`` controls whether acceptance_criteria bullets are emitted;
    ``desc_cap`` (when set) truncates each task description to that many chars
    with a trailing ellipsis. Used by ``_format_task_groups`` to degrade detail
    under budget pressure WITHOUT dropping any task.
    """
    lines: list[str] = []
    for group in task_groups:
        if not isinstance(group, dict):
            continue
        group_id = group.get("group_id") or ""
        group_name = group.get("name") or ""
        header_bits = [b for b in (group_id, group_name) if b]
        header = " — ".join(header_bits) if header_bits else "(unnamed group)"
        lines.append(f"### {header}")

        tasks = group.get("tasks") or []
        if not isinstance(tasks, list) or not tasks:
            lines.append("_(no tasks)_")
            lines.append("")
            continue

        for task in tasks:
            if not isinstance(task, dict):
                continue
            task_id = task.get("id")
            desc = (task.get("description") or "").strip()
            if desc_cap is not None and len(desc) > desc_cap:
                desc = desc[:desc_cap].rstrip() + "…"
            id_prefix = f"[{task_id}] " if task_id is not None else ""
            lines.append(f"- {id_prefix}{desc}" if desc else f"- {id_prefix}(no description)")

            if include_ac:
                criteria = task.get("acceptance_criteria") or []
                if isinstance(criteria, list) and criteria:
                    for c in criteria:
                        c_text = str(c).strip()
                        if c_text:
                            lines.append(f"  - AC: {c_text}")
        lines.append("")

    return "\n".join(lines).rstrip()


def _format_task_groups(task_groups: Any) -> str:
    """Render plan task_groups as a Markdown summary for self_check.

    Returns an empty string when input is missing, None, empty, or not a list —
    the caller omits the whole prompt section in that case.

    The Per-Task Correctness HARD AUDIT requires EVERY planned task to be
    visible to the reviewer: head-truncating the rendered text (the old
    behavior) silently hid later tasks and let the audit pass without checking
    them. Instead, when the full render exceeds
    SELF_CHECK_TASK_GROUPS_MAX_CHARS, detail is degraded — never whole tasks:
    first acceptance_criteria are dropped, then each description is capped to an
    equal share of the budget — so all task headers survive, and a retrieval
    note tells the reviewer how to pull the untruncated list.
    """
    if not task_groups or not isinstance(task_groups, list):
        return ""

    full = _render_task_groups(task_groups, include_ac=True)
    if not full:
        return ""
    if len(full) <= SELF_CHECK_TASK_GROUPS_MAX_CHARS:
        return full

    budget = SELF_CHECK_TASK_GROUPS_MAX_CHARS - len(_TASK_GROUPS_TRIM_NOTE)

    # Tier 2: drop acceptance_criteria but keep full task descriptions.
    no_ac = _render_task_groups(task_groups, include_ac=False)
    if len(no_ac) <= budget:
        return no_ac + _TASK_GROUPS_TRIM_NOTE

    # Tier 3: still over budget — give each task an equal share of the budget
    # for its (capped) description so every task header still appears. A small
    # floor keeps each line readable even when the task count is large; this may
    # exceed the soft budget slightly, which is the deliberate trade — never
    # hide a task that the audit must check.
    n_tasks = _count_tasks(task_groups) or 1
    per_task_desc = max(80, budget // n_tasks)
    capped = _render_task_groups(
        task_groups, include_ac=False, desc_cap=per_task_desc
    )
    return capped + _TASK_GROUPS_TRIM_NOTE


def _build_task_groups_section(task_groups: Any) -> str:
    """Build the full `## Plan Task Groups` prompt section, or empty string.

    When task_groups is absent/empty, returns "" so the caller can inline it
    without producing an orphan heading or blank lines in the prompt.
    """
    summary = _format_task_groups(task_groups)
    if not summary:
        return ""
    return "\n" + _TASK_GROUPS_SECTION_INTRO + summary + "\n"


def self_check_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the self_check step.

    Performs LLM-based code review checking logic completeness,
    robustness, and test coverage gaps. Does NOT check spec compliance.

    Returns COMPLETED when no issues are found.
    Returns REVISION_NEEDED when issues exist (regardless of iteration count),
    letting the state machine handle exhaustion centrally.
    """
    task_description = step.inputs.get("task_description", "")
    changes_made = step.inputs.get("changes_made", {})
    test_results = step.inputs.get("test_results", {})
    spec_content = step.inputs.get("spec_content", {})
    task_groups = step.inputs.get("task_groups")

    fix_iteration = step.inputs.get("fix_iteration", 0)
    # Honor an explicit 0 from inputs (the unlimited sentinel); fall back to
    # the default only when the input is genuinely missing.
    raw_max = step.inputs.get("max_fix_iterations")
    max_iterations = raw_max if isinstance(raw_max, int) and not isinstance(raw_max, bool) else DEFAULT_MAX_FIX_ITERATIONS
    prev_issues = step.inputs.get("prev_self_check_issues", [])
    fix_history = step.inputs.get("fix_history", [])
    convergence_enabled = step.inputs.get("self_check_convergence_enabled", False)
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

    changes_text = _format_changes(changes_made)
    test_text = _format_test_results(test_results)
    spec_text = _format_spec_content(spec_content)
    task_groups_section = _build_task_groups_section(task_groups)
    fix_context_text = _format_fix_context(
        fix_iteration, max_iterations,
        prev_issues=prev_issues,
        fix_history=fix_history,
    )

    prompt = SELF_CHECK_PROMPT.format(
        task_description=task_description,
        changes_made=changes_text,
        test_results=test_text,
        spec_content=spec_text,
        task_groups_section=task_groups_section,
        fix_context=fix_context_text,
    )

    project_root = flow.change_path.parent if flow.change_path else Path.cwd()

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
        f"Running self-check code review "
        f"#{pass_index}/{passes_required} (fix iteration: {fix_iteration})..."
    )

    try:
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
                '"expectation_source": {"type": "task_description|spec|user_interjection|plan_task|regression", "verbatim_quote": "..."}, '
                '"evidence_lines": ["path:N"], "missing_in": [], "out_of_scope": false}], '
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
            raw_issues, step.inputs,
        )

        # Single-line observability log so on-call can see at a glance how
        # many issues the LLM proposed vs how many survived validation, and
        # which rejection reasons fired.
        dropped = validation_stats["input_count"] - validation_stats["kept_count"]
        if dropped > 0:
            reasons = ", ".join(
                f"{k}={v}" for k, v in validation_stats.items()
                if k.endswith("_count") and v > 0 and k != "kept_count"
                and k != "input_count"
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
        # return, so every SELF_CHECK execution — clean, converged, deferred, or
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
            if defer_enabled and deferred_issues and is_last_pass:
                logger.info(
                    f"Self-check #{pass_index}/{passes_required} clean but flushing "
                    f"{len(deferred_issues)} deferred issue(s) accumulated from earlier passes"
                )
                return _build_fix_outputs(
                    step, deferred_issues, fix_iteration, max_iterations,
                    pass_index, passes_required,
                )
            # Non-terminal clean pass: carry the stash forward unchanged so the
            # next pass keeps accumulating.
            if defer_enabled and deferred_issues:
                step.outputs["self_check_deferred_issues"] = deferred_issues
                step.outputs["self_check_deferred"] = True
            return StepStatus.COMPLETED

        issues = kept_issues  # alias for the rest of the function

        # NOTE: When convergence_enabled=True and N>1, pass #1 may return
        # COMPLETED via the convergence shortcut. Pass #2+ deliberately strips
        # prev_self_check_issues (no intra-round comparison), so if pass #2
        # finds the same issues it returns REVISION_NEEDED, triggering another
        # fix loop. This is intentional: convergence breaks stalled fix loops
        # across rounds, not within a single N-pass round.
        #
        # Severity guard (item 1): the convergence shortcut MUST NOT swallow a
        # pass that contains critical/high findings, even when the signatures
        # match the previous round. A critical/high issue is never "safe to stop
        # on" — it must merge the accumulated findings and enter the fix loop
        # immediately. So a converged-but-critical/high pass falls through to the
        # defer-fix decision below, which (because critical/high is present) does
        # NOT defer and instead merges + returns REVISION_NEEDED.
        #
        # Defer subordination (item 1): the convergence shortcut MUST NOT discard
        # findings that the defer/fix arbitration is obligated to accumulate or
        # fix. When deferral is enabled (``self_check_defer_fix_threshold > 0``),
        # EVERY issue this pass found is mandatorily handled by the arbitration
        # below — it is either deferred (a non-terminal below-threshold
        # non-critical pass stashes its issues for a later consolidated fix) or
        # it enters the fix loop now (threshold reached, critical/high present,
        # the last pass, or an existing stash to flush/merge). A convergence
        # early-exit returns COMPLETED and would silently drop those issues,
        # violating the defer contract — so convergence is blocked outright
        # whenever deferral is enabled and any issue is present (and ``issues``
        # is guaranteed non-empty here, past the ``if not kept_issues`` guard).
        # In particular a simple below-threshold pass with no pending stash is
        # NOT exempt: with later passes it must be deferred, and at the chain
        # tail it must be fixed. Convergence is preserved unchanged only when
        # deferral is disabled (threshold 0/null, the default).
        convergence_blocked_by_defer = defer_enabled

        # Oscillation guard for the convergence shortcut. When a structural
        # oscillation trigger (a/b/c) would fire on this round, convergence MUST
        # NOT be allowed to silently mark the flow COMPLETED "converged-but-
        # diseased": a spec contradiction re-flagged in opposite directions looks
        # like convergence (same signatures round over round) but must instead be
        # routed to the adjudicator. The ledger already reflects this round (it
        # was recorded above), so ``should_suppress_convergence`` sees full
        # history.
        try:
            suppress_convergence = adjudication.should_suppress_convergence(
                flow.state.context, issues,
            )
        except Exception:
            logger.warning(
                "Adjudication convergence-suppression check failed", exc_info=True
            )
            suppress_convergence = False

        if (
            convergence_enabled
            and not convergence_blocked_by_defer
            and not suppress_convergence
            and not _has_critical_or_high(issues)
            and _issues_converged(issues, prev_issues)
        ):
            logger.warning(
                f"Self-check #{pass_index}/{passes_required} converged: "
                f"{len(issues)} issue(s) match previous iteration's signatures "
                f"— stopping fix loop to avoid re-flagging already-fixed or "
                f"not-real issues"
            )
            step.outputs["converged"] = True
            step.outputs["convergence_reason"] = (
                f"Same {len(issues)} issue signature(s) reported in consecutive iterations"
            )
            step.outputs["unresolved_issues"] = list(issues)
            return StepStatus.COMPLETED

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
    """Normalize free-text issue descriptions for fuzzy convergence comparison.

    Lowercases, strips punctuation, drops common English stopwords, and sorts
    the remaining tokens. This makes minor LLM paraphrasing (different word
    order, inserted punctuation, added articles) compare equal, so the
    convergence check isn't defeated by trivial wording changes.
    """
    lower = text.lower()
    cleaned = _DESC_PUNCT_RE.sub(" ", lower)
    tokens = [t for t in cleaned.split() if t and t not in _DESC_STOPWORDS]
    tokens.sort()
    return " ".join(tokens)


def _issue_signature(issues: list) -> set:
    """Compute a set of (location, normalized_description) tuples for convergence detection.

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


def _pair_resolutions_with_prev(
    resolutions: list, prev_issues: list | None
) -> list:
    """Pair each ``previous_issue_resolution`` with its previous issue by position.

    The raw ``previous_issue_resolutions`` schema carries only a prose paraphrase
    (``prev_issue_summary`` + ``status``) with no machine-readable identity, but
    the prompt requires exactly one entry per previously-reported issue, in order
    (SELF_CHECK_PROMPT: "For EACH previously-reported issue, include exactly one
    entry"). Pairing by index reunites each verdict with the full prev-issue dict
    so the adjudication ledger can fingerprint it — trigger (b) ("打脸") reads
    these ``fixed`` verdicts back and compares them by fingerprint against the
    current round. Extra resolutions without a matching prev issue are passed
    through unpaired (they contribute an empty fingerprint and carry no weight).
    """
    paired: list = []
    prev = prev_issues or []
    for i, res in enumerate(resolutions or []):
        if not isinstance(res, dict):
            continue
        entry = dict(res)
        if i < len(prev) and isinstance(prev[i], dict):
            # ``setdefault`` so an already-paired resolution (future callers)
            # keeps its own ``issue``.
            entry.setdefault("issue", prev[i])
        paired.append(entry)
    return paired


def _issues_converged(current_issues: list, prev_issues: list | None) -> bool:
    """Return True if the current issues appear to repeat the previous set.

    Signals the LLM is re-reporting the same issues after a fix attempt, meaning
    the fix loop has stalled. Requires at least one issue on each side.

    Detection is deliberately lenient — subset (not equality) semantics are
    intentional: when prev=[A,B,C] and current=[A], issue A has survived a
    full fix attempt and further iterations are unlikely to resolve it, so we
    stop. step.outputs["issues"] still carries the remaining issue list so
    downstream steps can react.

    A location-only second layer catches paraphrase-heavy convergence: if every
    current issue lives at a location already flagged by prev (regardless of
    wording), treat as converged. LLMs routinely rewrite descriptions for the
    same underlying problem.
    """
    if not prev_issues or not current_issues:
        return False
    current_sig = _issue_signature(current_issues)
    prev_sig = _issue_signature(prev_issues)
    if not current_sig or not prev_sig:
        return False
    if current_sig.issubset(prev_sig):
        return True
    current_locs = {loc for loc, _ in current_sig if loc}
    prev_locs = {loc for loc, _ in prev_sig if loc}
    if current_locs and prev_locs and current_locs.issubset(prev_locs):
        return True
    return False


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


def _format_spec_content(spec_content) -> str:
    if not spec_content:
        return "No specifications provided."

    if isinstance(spec_content, str):
        return spec_content

    parts = []
    for name, content in spec_content.items():
        parts.append(f"### {name}")
        parts.append(content)
        parts.append("")

    return "\n".join(parts)


def _format_fix_context(
    fix_iteration: int,
    max_iterations: int,
    prev_issues: list | None = None,
    fix_history: list | None = None,
) -> str:
    """Format fix context for inclusion in the self_check prompt.

    Thin wrapper around the shared ``render_fix_context`` helper —
    delegates all branching/copy to a single source of truth shared with
    verify_spec. prev_issues are rendered inline here (self_check has no
    separate "Previous Verification" slot in its prompt).
    """
    return render_fix_context(
        fix_iteration,
        max_iterations,
        step_label="self-check",
        prev_issues=prev_issues,
        fix_history=fix_history,
    )
