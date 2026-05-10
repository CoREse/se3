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
            severity = issue.get("severity", "high")
            desc = issue.get("description", "")
            location = issue.get("location", "")
            loc_suffix = f" @ {location}" if location else ""
            lines.append(f"- [{severity}] {desc}{loc_suffix}")
        if len(prev_issues) > PREV_ISSUES_RENDER_TAIL:
            lines.append(
                f"- ... and {len(prev_issues) - PREV_ISSUES_RENDER_TAIL} more issues (truncated)"
            )

    if fix_history:
        lines.append("")
        lines.append("## Fix History")
        history_total = len(fix_history)
        for entry in fix_history[-FIX_HISTORY_RENDER_TAIL:]:
            it = entry.get("iteration", "?")
            reason = entry.get("reason", "unknown")
            trigger = entry.get("trigger_step_type", "unknown")
            lines.append(f"- Iteration {it}: triggered by {trigger} ({reason})")
        if history_total > FIX_HISTORY_RENDER_TAIL:
            lines.append(
                f"- ... and {history_total - FIX_HISTORY_RENDER_TAIL} earlier entries (truncated)"
            )

    return "\n".join(lines)
