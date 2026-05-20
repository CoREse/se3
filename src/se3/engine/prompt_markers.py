"""Sentinel markers separating system-template prefix from user content in step prompts.

The flow engine's step prompts are mostly a fixed "template prefix" (role,
agent-safety boilerplate, generic instructions) followed by the task- and
project-specific user content (task description, spec, context, etc.). The
chat history records the whole thing as a single ``user`` message, which the
web running-flow console renders as one big chip — burying the user's real
input.

To let the frontend split the message cleanly without resorting to fragile
text pattern matching, every prompt that has a meaningful user-content
portion SHALL wrap its prefix/content boundary with these markers via
``wrap_user_content``. The frontend recognises ``TEMPLATE_PREFIX_END`` as a
"collapse the preceding text into a system-prompt chip" signal and renders
``USER_CONTENT_BEGIN`` onwards as a normal expanded user bubble.

The marker literals are HTML-style comments so that the LLM is unlikely to
echo or reinterpret them, and so they are visually unobtrusive when a human
reads the raw prompt. Backward compatibility: messages produced before this
protocol existed simply lack the markers; the frontend falls back to the
existing whole-message chip behavior in that case.
"""

from __future__ import annotations

TEMPLATE_PREFIX_END = "<!--SE3:TEMPLATE_END-->"
USER_CONTENT_BEGIN = "<!--SE3:USER_CONTENT-->"


def inject_boundary(template: str, before: str) -> str:
    """Splice the sentinel marker pair right before ``before`` in ``template``.

    Used by step prompt modules at module-init time to mark the boundary
    between the boilerplate system-instructions prefix and the
    user/task-specific content section, without otherwise altering the
    template string. ``before`` should be a unique substring whose first
    occurrence marks the start of the user-content section (typically a
    Markdown heading like ``"## Task Description\\n"``).

    Idempotent: if the template already contains ``TEMPLATE_PREFIX_END``,
    the original string is returned unchanged so re-application is safe.
    If ``before`` is missing the template is also returned unchanged
    (callers SHOULD pass an existing anchor — this is defensive).
    """
    if not template or not before:
        return template
    if TEMPLATE_PREFIX_END in template:
        return template
    if before not in template:
        return template
    return template.replace(
        before,
        f"{TEMPLATE_PREFIX_END}\n{USER_CONTENT_BEGIN}\n{before}",
        1,
    )


def wrap_user_content(template_prefix: str, user_content: str) -> str:
    """Splice ``template_prefix`` and ``user_content`` with sentinel markers.

    Args:
        template_prefix: The system/instructions portion (role definition,
            agent-safety boilerplate, generic step instructions). Trailing
            whitespace is preserved as-is — callers control spacing.
        user_content: The task/project-specific portion (task description,
            spec, context, design doc, etc.).

    Returns:
        The two segments joined as
        ``"{prefix}{TEMPLATE_PREFIX_END}\\n{USER_CONTENT_BEGIN}\\n{content}"``,
        with sensible fallbacks when one side is empty or when the prefix
        already contains a marker (idempotent — never injects a second pair).
    """
    if not user_content:
        return template_prefix
    if not template_prefix:
        return user_content
    if TEMPLATE_PREFIX_END in template_prefix or USER_CONTENT_BEGIN in template_prefix:
        return template_prefix + user_content
    return (
        f"{template_prefix}{TEMPLATE_PREFIX_END}\n"
        f"{USER_CONTENT_BEGIN}\n{user_content}"
    )
