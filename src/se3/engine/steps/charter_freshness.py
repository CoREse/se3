"""Charter Freshness step handler.

A flow-end advisory that reuses the ``version_analyze`` shape ("LLM reads the
change and gives a recommendation") to answer one cheap question: **did this
diff touch any of the charter's three content classes?**

    1. project identity / positioning
    2. top-level / cross-subsystem architecture
    3. project-wide cross-cutting conventions & hard constraints

The charter is human-maintained and decoupled from project size, so the
overwhelming majority of flows touch none of those classes and pass for free.
When the diff *does* plausibly touch one, the step surfaces an update prompt
(``charter_update_needed`` + a concrete ``suggested_update``) so a human can
keep the charter fresh. It is **never blocking** — it always returns COMPLETED;
a missed update is a soft, prompt-level signal, exactly like the existing
conventions.

It also hosts the charter **admission check** trigger (task 3): when the diff
edited ``se3/charter.md`` itself, the altitude gate runs against the new charter
and its monitoring-light warning (low-level-content leakage) is surfaced in the
step outputs. That, too, never blocks.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ..charter import (
    CHARTER_ADMISSION_STANDARD,
    admission_check_for_changes,
    load_charter,
)
from ..llm_caller import LLMCaller
from ..models import FlowInstance, Step, StepStatus
from ..prompt_markers import inject_boundary
from ..utils.json_parser import parse_json_response

logger = logging.getLogger(__name__)


CHARTER_FRESHNESS_PROMPT = """You are a charter-freshness auditor. The **charter** is the small, high-altitude document injected in full into every step of every session. Decide whether the diff below changed anything that the charter should now reflect.

## The charter's three content classes (and ONLY these)
1. **Project identity / positioning** — what the project is, its primary language / framework, its purpose.
2. **Top-level architecture** — the semantic/subjective layering mechanical structure cannot express: which modules form one subsystem, where cross-subsystem boundaries lie. (NOT per-module locators — those live in code-index, never the charter.)
3. **Project-wide cross-cutting conventions & hard constraints** — coding conventions, key constraints, workflow conventions, version policy that apply everywhere.

## Charter admission standard (what the charter MAY / MUST NOT carry)
{admission_standard}

## Task Description
{task_description}

## Changes Made (the diff)
{changes_made}

## Current Charter (for reference)
{charter}

## Instructions
Answer conservatively. The DEFAULT and overwhelmingly common answer is that the diff touches NONE of the three classes — a normal feature / bugfix changes implementation detail, which belongs in the code and its why-comments, NOT the charter. Only flag a touch when the diff genuinely changes the project's identity, its top-level architecture, or a project-wide convention/constraint.

If you DO flag a touch, the suggested update MUST stay high-altitude: do NOT propose copying per-module/per-file locators or implementation detail into the charter (that is exactly the low-level leakage the admission standard forbids).

Respond in valid JSON format:
```json
{{
  "charter_update_needed": false,
  "touched_classes": [],
  "reason": "Why the diff does or does not touch a charter content class",
  "suggested_update": ""
}}
```

- `charter_update_needed`: boolean. `true` only when the diff touches one of the three classes.
- `touched_classes`: array drawn from ["identity", "architecture", "conventions"]; empty when nothing is touched.
- `reason`: one or two sentences of justification.
- `suggested_update`: when `charter_update_needed` is true, a concise high-altitude description of what the charter should now say; otherwise an empty string.
"""

# Two-segment marker only: USER_CONTENT region is empty (no user literal here).
CHARTER_FRESHNESS_PROMPT = inject_boundary(
    CHARTER_FRESHNESS_PROMPT, "## The charter's three content classes (and ONLY these)\n",
)


def _changed_files(changes_made: Any) -> list[str]:
    """Extract the flat list of changed paths from ``changes_made``.

    Tolerates both the plain-string and dict ``files_changed`` shapes the
    implement step may emit.
    """
    out: list[str] = []
    if not isinstance(changes_made, dict):
        return out
    files_changed = changes_made.get("files_changed") or []
    if not isinstance(files_changed, list):
        return out
    for entry in files_changed:
        if isinstance(entry, str) and entry:
            out.append(entry)
        elif isinstance(entry, dict):
            p = entry.get("path") or entry.get("file_path")
            if isinstance(p, str) and p:
                out.append(p)
    return out


def _format_changes(changes_made: dict[str, Any]) -> str:
    """Render the diff's file list (+ explanations) for the prompt."""
    if not changes_made:
        return "No changes recorded."
    lines: list[str] = []
    for fc in changes_made.get("files_changed", []):
        if isinstance(fc, str):
            lines.append(f"- modified: {fc}")
        elif isinstance(fc, dict):
            path = fc.get("path", "?")
            action = fc.get("action", "?")
            explanation = fc.get("explanation", "")
            lines.append(f"- {action}: {path}")
            if explanation:
                lines.append(f"  ({explanation})")
        else:
            lines.append(f"- {fc}")
    return "\n".join(lines) if lines else "Changes made but details unavailable."


def _run_admission_trigger(
    step: Step, project_root: Path, changed: list[str],
) -> None:
    """Run the altitude gate iff the diff edited the charter, recording outputs.

    Task 3: the admission check fires *when the charter is updated*. When
    ``se3/charter.md`` is among the changed files, run :func:`check_admission`
    on the current charter and surface its monitoring-light warning. Never
    blocks — it only annotates ``step.outputs``.
    """
    try:
        result = admission_check_for_changes(project_root, changed)
    except Exception:
        logger.debug("charter admission check raised; ignoring", exc_info=True)
        return
    if result is None:
        return
    step.outputs["admission_checked"] = True
    step.outputs["admission_over_threshold"] = result.over_threshold
    if result.warning:
        step.outputs["admission_warning"] = result.warning
        logger.warning("charter admission monitoring light: %s", result.warning)
    else:
        logger.info(
            "charter admission check ran (charter touched): %d bytes, within "
            "the %d-byte monitoring threshold.",
            result.size_bytes, result.threshold_bytes,
        )


def charter_freshness_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the charter_freshness step.

    Always returns COMPLETED (advisory, non-blocking). When the diff is empty
    it cheap-passes without an LLM call. Otherwise it asks the LLM whether the
    diff touches a charter content class and records the verdict (plus, when the
    charter file itself was edited, the admission monitoring result).
    """
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()

    changes_made = step.inputs.get("changes_made") or {}
    if not isinstance(changes_made, dict):
        changes_made = {}
    changed = _changed_files(changes_made)

    # Run the admission trigger first — it is independent of the freshness LLM
    # call and must fire even on the (rare) charter-only diff.
    _run_admission_trigger(step, project_root, changed)

    # Cheap pass: no diff -> the charter cannot have been touched.
    if not changed:
        logger.info("charter_freshness: no diff — passing (cheap, no LLM call).")
        step.outputs["charter_update_needed"] = False
        step.outputs["touched_classes"] = []
        step.outputs["reason"] = "No changes in this flow; charter unaffected."
        step.outputs["suggested_update"] = ""
        step.outputs["skipped_reason"] = "no_diff"
        return StepStatus.COMPLETED

    task_description = (
        step.inputs.get("task_description") or flow.task_description or ""
    )
    charter_text = step.inputs.get("charter")
    if not (isinstance(charter_text, str) and charter_text):
        charter_text = load_charter(project_root)
    charter_for_prompt = charter_text if charter_text.strip() else "_(no charter on disk)_"

    prompt = CHARTER_FRESHNESS_PROMPT.format(
        admission_standard=CHARTER_ADMISSION_STANDARD,
        task_description=task_description or "_(none)_",
        changes_made=_format_changes(changes_made),
        charter=charter_for_prompt,
    )

    logger.info("Checking whether the diff touches the charter's content classes...")

    try:
        retry_count = step.inputs.get("retry_count", 0)
        caller = LLMCaller(
            project_root,
            flow_id=flow.flow_id,
            step_id=step.step_id,
            step_type=step.step_type.value,
            external_attempt=retry_count,
            fix_iteration=step.inputs.get("fix_iteration", 0),
        )
        response = caller.call(
            prompt=prompt,
            json_mode="two_phase",
            json_schema_hint=(
                '{"charter_update_needed": false, "touched_classes": [], '
                '"reason": "...", "suggested_update": ""}'
            ),
            required_keys=["charter_update_needed"],
        )
        result = parse_json_response(response, required_keys=["charter_update_needed"])
    except Exception as e:
        # Advisory step: an LLM failure must NOT block the flow. Degrade to a
        # soft no-op and let the flow proceed to version_analyze.
        logger.warning("charter_freshness LLM call failed (non-blocking): %s", e)
        step.outputs["charter_update_needed"] = False
        step.outputs["touched_classes"] = []
        step.outputs["reason"] = f"charter_freshness skipped: LLM call failed ({e})."
        step.outputs["suggested_update"] = ""
        step.outputs["skipped_reason"] = "llm_error"
        return StepStatus.COMPLETED

    if not result:
        logger.warning("charter_freshness: unparsable LLM response (non-blocking).")
        step.outputs["charter_update_needed"] = False
        step.outputs["touched_classes"] = []
        step.outputs["reason"] = "charter_freshness skipped: unparsable LLM response."
        step.outputs["suggested_update"] = ""
        step.outputs["skipped_reason"] = "parse_error"
        return StepStatus.COMPLETED

    update_needed = bool(result.get("charter_update_needed"))
    touched = result.get("touched_classes") or []
    if not isinstance(touched, list):
        touched = []
    reason = str(result.get("reason", "") or "")
    suggested = str(result.get("suggested_update", "") or "")

    step.outputs["charter_update_needed"] = update_needed
    step.outputs["touched_classes"] = touched
    step.outputs["reason"] = reason
    step.outputs["suggested_update"] = suggested

    if update_needed:
        logger.warning(
            "charter_freshness: this diff appears to touch the charter "
            "(classes=%s). Suggested update: %s",
            touched, suggested or "(none provided)",
        )
    else:
        logger.info("charter_freshness: diff does not touch the charter — passing.")

    return StepStatus.COMPLETED
