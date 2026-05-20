"""Sentinel markers separating system-template prefix from user content in step prompts.

The flow engine's step prompts are mostly a fixed "template prefix" (role,
agent-safety boilerplate, generic instructions) followed by the task- and
project-specific user content (task description, spec, context, etc.). The
chat history records the whole thing as a single ``user`` message, which the
web running-flow console renders as one big chip — burying the user's real
input.

To let the frontend split the message cleanly without resorting to fragile
text pattern matching, the protocol is a three-segment marker triple:

    prefix  TEMPLATE_PREFIX_END  USER_CONTENT_BEGIN  user_content
            USER_CONTENT_END  suffix

The frontend renders ``prefix`` and ``suffix`` collapsed inside a single
system-prompt chip (template prefix + framework suffix), and only the
``user_content`` middle segment expands as a normal user bubble. This
matters because step prompts append framework-injected text *after* the
user's literal input (Available Specs, runtime env, READ-ONLY constraint,
language directive, …); without an explicit ``USER_CONTENT_END`` marker
the bubble would include that tail.

Step prompt modules with a meaningful user-content portion SHALL use
``wrap_user_section`` to produce the three-segment layout. The earlier
two-segment helpers (``inject_boundary`` / ``wrap_user_content``) are
retained as compatibility entry points for prompts that do not need a
suffix segment; the frontend falls back to a single combined chip when
``USER_CONTENT_END`` is missing.

The marker literals are HTML-style comments so that the LLM is unlikely to
echo or reinterpret them, and so they are visually unobtrusive when a human
reads the raw prompt. Backward compatibility: messages produced before this
protocol existed simply lack the markers; the frontend falls back to the
existing whole-message chip behavior in that case.
"""

from __future__ import annotations

TEMPLATE_PREFIX_END = "<!--SE3:TEMPLATE_END-->"
USER_CONTENT_BEGIN = "<!--SE3:USER_CONTENT-->"
USER_CONTENT_END = "<!--SE3:USER_CONTENT_END-->"


def _marker_pair_present(text: str) -> bool:
    """Return True when ``text`` already contains the three-marker section.

    A three-section wrap requires all three markers to be present, in the
    canonical ``TEMPLATE_PREFIX_END`` → ``USER_CONTENT_BEGIN`` →
    ``USER_CONTENT_END`` order. Anything less means the text was produced
    by the legacy two-marker path (``wrap_user_content`` /
    ``inject_boundary``) and is NOT considered an idempotent re-wrap target.
    """
    if not text:
        return False
    i = text.find(TEMPLATE_PREFIX_END)
    if i < 0:
        return False
    j = text.find(USER_CONTENT_BEGIN, i + len(TEMPLATE_PREFIX_END))
    if j < 0:
        return False
    k = text.find(USER_CONTENT_END, j + len(USER_CONTENT_BEGIN))
    return k >= 0


def wrap_user_section(prefix: str, user_content: str, suffix: str) -> str:
    """Wrap ``user_content`` as a marker-delimited section between prefix and suffix.

    The three-segment layout makes the user's literal input an explicitly
    bounded middle segment, so both the template prefix and any framework
    text appended after the user input (Available Specs, runtime env,
    READ-ONLY constraint, language instructions) are kept out of the
    web console's user-content bubble.

    Output layout::

        prefix + TEMPLATE_PREFIX_END + "\\n" + USER_CONTENT_BEGIN + "\\n"
              + user_content
              + "\\n" + USER_CONTENT_END + "\\n" + suffix

    Args:
        prefix: Template / system-instructions text that precedes the user's
            literal input. Trailing whitespace is preserved; callers control
            spacing.
        user_content: The user's literal contribution at this prompt-boundary
            (e.g. ``initial_description`` for discovery). May be empty — when
            empty the section is still rendered with the markers so the
            frontend can treat it as a three-segment record with an empty
            middle bubble.
        suffix: Framework-injected text that follows the user input
            (Available Specs list, runtime-env capabilities, READ-ONLY
            constraint, language directive, …). May be empty.

    Returns:
        The concatenated three-segment string. Idempotent: if any of the
        three input strings already contains the full three-marker section,
        the inputs are concatenated as-is without injecting a second copy.
    """
    if _marker_pair_present(prefix) or _marker_pair_present(user_content) or _marker_pair_present(suffix):
        return f"{prefix}{user_content}{suffix}"
    return (
        f"{prefix}{TEMPLATE_PREFIX_END}\n"
        f"{USER_CONTENT_BEGIN}\n{user_content}"
        f"\n{USER_CONTENT_END}\n{suffix}"
    )


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
