"""Task description composition with persisted user interjections.

When a user Ctrl-C's mid-flow and types an additional instruction, that
instruction is persisted into ``flow.state.context["user_interjections"]``
and inlined into the effective ``inputs["task_description"]`` for the
current step's re-run and every subsequent step.

This module owns the (small but central) format used to render the
appended section so all call sites — ``run.py:_handle_step_interrupt``
for the immediate re-run, ``state_machine._build_step_inputs`` for the
downstream propagation — produce byte-identical output.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping


_SECTION_HEADER = "## Additional Instructions (added during run)"


def compose_task_description_with_interjections(
    base: str,
    interjections: Iterable[Mapping[str, Any]],
) -> str:
    """Append a structured ``## Additional Instructions`` section listing each
    interjection. Returns ``base`` unchanged when ``interjections`` is empty
    or yields no usable entries.

    Each interjection entry is a dict with optional keys ``text``,
    ``step_type``, ``timestamp``. ``text`` is required (entries with empty
    text after ``.strip()`` are skipped). ``step_type`` and ``timestamp``
    are rendered as a ``[step@ts]`` prefix when either is present.
    """
    rendered: list[str] = []
    for entry in interjections or []:
        if not isinstance(entry, Mapping):
            continue
        text = (entry.get("text") or "").strip()
        if not text:
            continue
        step = (entry.get("step_type") or "").strip()
        ts = (entry.get("timestamp") or "").strip()
        if step or ts:
            prefix = f"[{step}@{ts}] " if step and ts else f"[{step or ts}] "
        else:
            prefix = ""
        rendered.append(f"- {prefix}{text}")

    if not rendered:
        return base or ""

    body = "\n".join(rendered)
    base_clean = (base or "").rstrip()
    if base_clean:
        return f"{base_clean}\n\n{_SECTION_HEADER}\n\n{body}"
    return f"{_SECTION_HEADER}\n\n{body}"
