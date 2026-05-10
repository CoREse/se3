"""Shared helper for rendering the fix-loop context block in step prompts.

Used by both ``verify_spec.py`` and ``self_check.py`` to render the
``{fix_context}`` slot of their LLM prompts. Centralized so the
sentinel/branch logic cannot silently diverge between the two callers.

Step-specific wording (``"verification"`` vs ``"self-check"``) is parameterized
via ``step_label`` rather than duplicated.
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


# Tail-truncation limits used inside the rendered block. Kept module-level so
# tests can import them rather than relying on positional substring matches.
FIX_HISTORY_RENDER_TAIL = 20
PREV_ISSUES_RENDER_TAIL = 20


def format_fix_iteration_display(fix_iteration: int, max_iterations: int) -> str:
    """Render the compact ``"<n>/<cap>"`` display used in log lines."""
    if max_iterations <= 0:
        return f"{fix_iteration}/unlimited"
    return f"{fix_iteration}/{max_iterations}"


def extract_issue_display_fields(issue: dict) -> tuple[str, str, str]:
    """Schema-compatible extraction of (severity, description, location)
    from an issue dict, regardless of whether it's in the new self_check
    schema (Commit 3+) or the legacy schema still used by verify_spec.

    Returns a tuple of three strings; any field not derivable is "".

    Used by every site that needs to display or render an issue: the
    fix-context prompt block, ``_describe_issue`` for fix_instructions,
    implement.py's ``_format_fix_context_structured`` for downstream
    fix-loop prompts, and step_renderers.py for CLI history display.
    Centralizing the schema-compat read prevents the same drift bug
    surfacing in every consumer.
    """
    if not isinstance(issue, dict):
        return ("", "", "")
    # ``or "high"`` (rather than ``.get(..., "high")``) so an explicit
    # ``None`` value is also treated as missing — otherwise ``str(None)``
    # would render as the literal string ``"None"`` in the prompt.
    severity = str(issue.get("severity") or "high")

    # Description: prefer new schema (actual_behavior + divergence joined
    # with em-dash). Fall back to legacy ``description`` / ``message``.
    actual = (issue.get("actual_behavior") or "").strip()
    divergence = (issue.get("divergence") or "").strip()
    new_desc_parts = [p for p in (actual, divergence) if p]
    new_desc = " — ".join(new_desc_parts)
    description = (
        new_desc
        or (issue.get("description") or "")
        or (issue.get("message") or "")
    )

    # Location: prefer new schema's evidence_lines[0]; fall back to
    # missing_in[0] (with explicit prefix) and finally legacy ``location``.
    location = ""
    evidence = issue.get("evidence_lines")
    if isinstance(evidence, list):
        for ev in evidence:
            if isinstance(ev, str) and ev.strip():
                location = ev.strip()
                break
    if not location:
        missing = issue.get("missing_in")
        if isinstance(missing, list):
            for m in missing:
                if isinstance(m, str) and m.strip():
                    location = f"missing_in: {m.strip()}"
                    break
    if not location:
        location = str(issue.get("location") or "")

    return (severity, description, location)


def render_fix_context(
    fix_iteration: int,
    max_iterations: int,
    *,
    step_label: str,
    prev_issues: Optional[list] = None,
    fix_history: Optional[list] = None,
) -> str:
    """Render the fix-context block for an LLM prompt.

    Args:
        fix_iteration: Current fix-loop iteration count (0 for the initial pass).
        max_iterations: Configured cap. ``<= 0`` is the documented sentinel
            for "unlimited".
        step_label: Human-readable name of the calling step
            (``"verification"`` or ``"self-check"``).
        prev_issues: Optional list of previous-round issues to render inline
            (used by self_check; verify_spec renders prev_issues into a
            separate slot).
        fix_history: Optional fix-history list; the last
            ``FIX_HISTORY_RENDER_TAIL`` entries are rendered.
    """
    is_unlimited = max_iterations <= 0
    final_attempt = not is_unlimited and fix_iteration == max_iterations
    final_warning = (
        f"WARNING: This is the final fix-loop iteration before exhaustion. "
        f"If unresolved issues are reported in this {step_label}, the flow "
        "will be marked as FAILED on the next transition."
    )

    if fix_iteration == 0:
        if is_unlimited:
            return (
                f"This is the initial {step_label} (no previous fix attempts; "
                "unlimited mode)."
            )
        return f"This is the initial {step_label} (no previous fix attempts)."

    if is_unlimited:
        iteration_line = f"Fix iteration: {fix_iteration} (unlimited)"
    else:
        iteration_line = f"Fix iteration: {fix_iteration} of {max_iterations}"
    lines = [
        iteration_line,
        f"Previous fix attempts: {fix_iteration}",
    ]

    if final_attempt:
        lines.append(final_warning)

    if prev_issues:
        lines.append("")
        lines.append("## Previously Reported Issues")
        lines.append(f"The following issues were reported in the previous {step_label}.")
        lines.append("Only report issues that STILL EXIST after the fix attempt.")
        lines.append("Do NOT re-report issues that have been successfully fixed.")
        lines.append("")
        for issue in prev_issues[:PREV_ISSUES_RENDER_TAIL]:
            if not isinstance(issue, dict):
                continue
            severity, desc, location = extract_issue_display_fields(issue)
            loc_suffix = f" @ {location}" if location else ""
            lines.append(f"- [{severity}] {desc}{loc_suffix}")
        if len(prev_issues) > PREV_ISSUES_RENDER_TAIL:
            lines.append(
                f"- ... and {len(prev_issues) - PREV_ISSUES_RENDER_TAIL} more issues (truncated)"
            )

    # The ``fix_history`` parameter is intentionally NOT rendered. Its
    # iteration-count + trigger-type metadata bias self_check / verify_spec
    # reviewers toward over-flagging ("we've been at this N rounds, surely
    # something is still wrong"). The implement step has its own
    # ``_format_fix_history`` (implement.py) that does need the history,
    # but for review steps the signal is more harmful than helpful. The
    # parameter is kept in the signature for back-compat with existing
    # callers; remove after a deprecation cycle if no other consumer arises.
    _ = fix_history  # explicit no-op consumer for static analyzers

    return "\n".join(lines)
