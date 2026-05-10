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

from ..llm_caller import LLMCaller
from ..models import FlowInstance, Step, StepStatus
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
#     project's task_description / non-base spec_content. The handler
#     normalizes both sides identically (unicode NFKC, smart-quote
#     replacement, whitespace collapse, literal ``\n`` → real newline)
#     before comparison so the LLM cannot rely on cosmetic drift.
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

    Includes ``task_description`` (which post-Commit 2 already inlines
    user_interjections), ``original_task_description`` if the discovery
    step produced a refined version, and project-specific specs.

    Excludes the ``base`` spec entry: its content is generic project
    boilerplate (PEP 8, "代码应当健壮") that any nit can hang off of.
    """
    pool: list[str] = []
    for key in ("task_description", "original_task_description"):
        val = step_inputs.get(key)
        if isinstance(val, str) and val:
            pool.append(val)
    spec_content = step_inputs.get("spec_content") or {}
    if isinstance(spec_content, dict):
        for spec_name, content in spec_content.items():
            if spec_name == "base":
                continue
            if isinstance(content, str) and content:
                pool.append(content)
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


def _validate_evidence(issue: dict, changed: set[str]) -> bool:
    """Return True when the issue carries usable evidence — at least one
    well-formed ``path:N`` entry whose path is in ``changed``, OR at
    least one non-empty ``missing_in`` entry. ``missing_in`` covers
    "the implementation should have edited X but didn't" cases that
    naturally cannot point at a changed line.
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
    missing_in = issue.get("missing_in") or []
    if isinstance(missing_in, list):
        for mp in missing_in:
            if isinstance(mp, str) and mp.strip():
                return True
    return False


def _describe_issue(issue: dict) -> str:
    """Render a kept issue as a single bullet line for fix_instructions.

    Uses the new schema fields. Falls back to legacy ``description`` /
    ``location`` if the issue somehow lacks the new fields (defensive —
    validation should have rejected such issues already).
    """
    primary_loc = ""
    evidence = issue.get("evidence_lines") or []
    if isinstance(evidence, list) and evidence:
        primary_loc = next(
            (e for e in evidence if isinstance(e, str) and e.strip()),
            "",
        )
    if not primary_loc:
        missing = issue.get("missing_in") or []
        if isinstance(missing, list) and missing:
            primary_loc = f"missing_in: {missing[0]}"
    if not primary_loc:
        primary_loc = issue.get("location", "?")

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
            continue

        source = issue.get("expectation_source") or {}
        quote = (
            source.get("verbatim_quote", "")
            if isinstance(source, dict) else ""
        )
        norm_quote = _normalize_for_quote_match(quote)
        if not norm_quote:
            stats["empty_quote_count"] += 1
            continue
        if not any(norm_quote in p for p in pool_normalized):
            stats["quote_not_in_source_count"] += 1
            continue

        if not _validate_evidence(issue, changed):
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

1. **Logic Completeness**: Are there unhandled boundary conditions, missing error paths, or incomplete control flow? Look for edge cases the implementation should handle but doesn't.
2. **Code Robustness**: Is exception handling adequate? Are resources properly managed (files, connections, locks)? Are there concurrency safety issues?
3. **Functional Gaps**: Are there related modules that should have been modified but weren't? Are there integration points that were missed?
4. **Test Coverage Gaps**: Based on the test results, which logic paths are NOT exercised by existing tests? Are there critical paths that lack test coverage?

## What NOT to check
- **Spec compliance** — this is handled by the verify_spec step, do NOT duplicate that check.
- **Code style or formatting** — not actionable here.
- **Performance optimization suggestions** — only flag if there's a clear correctness issue.

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
    {{ "type": "task_description" | "spec" | "user_interjection",
       "verbatim_quote": "<a literal substring from the project's task_description or non-base spec_content above>" }}
  The handler normalizes both the quote and the source pool (NFKC + smart-quote replacement + whitespace collapse + literal `\\n` → newline) and drops any issue whose normalized quote is not a substring of any normalized source-pool entry. Quote a substantive phrase, NOT a single generic noun.
- `evidence_lines`: array of `"path:N"` strings, where `path` MUST appear in `changes_made.files_changed` (the handler verifies). At least one entry required UNLESS `missing_in` is non-empty.
- `missing_in`: array of file paths that should have been edited but were not. Use this for "missed integration point" issues that cannot point at a changed line.
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


_TASK_GROUPS_SECTION_INTRO = (
    "## Plan Task Groups (Scope Reference)\n\n"
    "The following is the plan's task breakdown (task_groups). It is a "
    "**scope reference**, NOT a strict specification:\n"
    "- Use it to help judge the **Functional Gaps** dimension: cross-check that "
    "each planned task's deliverables appear in the implementation.\n"
    "- Reasonable deviations from the plan (logic correct, functionality covered, "
    "quality acceptable) do NOT count as issues.\n"
    "- Do NOT flag missing-plan-compliance as an issue — this is self_check, not a "
    "plan-conformance audit. Functional-gap judgments should weigh the original "
    "Task Description together with this list.\n\n"
)


def _format_task_groups(task_groups: Any) -> str:
    """Render plan task_groups as a compact Markdown summary for self_check.

    Returns an empty string when input is missing, None, empty, or not a list —
    the caller omits the whole prompt section in that case.

    Output is head-truncated to SELF_CHECK_TASK_GROUPS_MAX_CHARS with an
    explicit ellipsis marker appended when truncation occurs.
    """
    if not task_groups or not isinstance(task_groups, list):
        return ""

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
            id_prefix = f"[{task_id}] " if task_id is not None else ""
            lines.append(f"- {id_prefix}{desc}" if desc else f"- {id_prefix}(no description)")

            criteria = task.get("acceptance_criteria") or []
            if isinstance(criteria, list) and criteria:
                for c in criteria:
                    c_text = str(c).strip()
                    if c_text:
                        lines.append(f"  - AC: {c_text}")
        lines.append("")

    summary = "\n".join(lines).rstrip()
    if not summary:
        return ""

    if len(summary) > SELF_CHECK_TASK_GROUPS_MAX_CHARS:
        cut = summary[:SELF_CHECK_TASK_GROUPS_MAX_CHARS]
        # Prefer cutting at the last newline so we don't split a markdown
        # bullet or `### Group` header mid-line. Fall back to the raw slice
        # if no newline exists within the window.
        nl = cut.rfind("\n")
        if nl > 0:
            cut = cut[:nl]
        summary = cut.rstrip() + "\n… (truncated)"
    return summary


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

    # Append available-specs names injection if applicable
    from ..context_builder import get_spec_names_injection
    spec_names = get_spec_names_injection(
        "self_check", project_root, step.inputs.get("relevant_specs"),
    )
    if spec_names:
        prompt += spec_names

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
        )
        response = caller.call(
            prompt=prompt,
            json_mode="two_phase",
            json_schema_hint=(
                '{"issues": [{"severity": "critical|high|medium|low", '
                '"actual_behavior": "...", "expected_behavior": "...", '
                '"divergence": "...", '
                '"expectation_source": {"type": "task_description|spec|user_interjection", "verbatim_quote": "..."}, '
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
            return StepStatus.COMPLETED

        issues = kept_issues  # alias for the rest of the function

        # NOTE: When convergence_enabled=True and N>1, pass #1 may return
        # COMPLETED via the convergence shortcut. Pass #2+ deliberately strips
        # prev_self_check_issues (no intra-round comparison), so if pass #2
        # finds the same issues it returns REVISION_NEEDED, triggering another
        # fix loop. This is intentional: convergence breaks stalled fix loops
        # across rounds, not within a single N-pass round.
        if convergence_enabled and _issues_converged(issues, prev_issues):
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

        iter_display = format_fix_iteration_display(fix_iteration, max_iterations)
        logger.warning(
            f"Self-check #{pass_index}/{passes_required} found {len(issues)} "
            f"issue(s) (fix iteration {iter_display})"
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

        return StepStatus.REVISION_NEEDED

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
