"""Analyze step handler.

Analyzes the task description to determine:
- Task type (feature, bugfix, review, small, directive)
- Scope of changes
- Required steps for the workflow
- Relevant specs to load for downstream steps

This is the "super-analyze" step: it combines task classification with
spec selection in a single LLM call, and programmatically collects
project context (replacing the former PROJECT_SUMMARY step) and loads
spec content (replacing the former READ_SPEC step).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, List

from ..context_builder import ContextBuilder
from ..llm_caller import LLMCaller
from ..models import FlowInstance, Step, StepStatus, StepType, get_default_step_sequence
from ..project_context import ProjectContextCollector, list_spec_names
from ..prompt_markers import inject_boundary
from ..spec_index import _NO_REQUIREMENTS_SENTINEL, load_or_build
from ..spec_index_render import render_index
from ..spec_role import SPEC_ROLE_DEFINITION
from ..spec_loader import load_for_step
from ..utils.json_parser import parse_json_response
from ...config import (
    apply_step_config,
    insert_confirmation_steps,
    load_spec_governance_config,
)

logger = logging.getLogger(__name__)

# Bounded number of out-port re-prompts when ``selected_items`` contains an
# address that is not a real flat item (a group/page handle or an intermediate
# navigation node). The agent is fed the offending addresses and asked to
# re-drill; if it still fails after these retries the invalid entries are
# dropped and the flow proceeds (with a base::* fallback when nothing survives).
_MAX_SELECTION_VALIDATION_RETRIES = 2


ANALYZE_PROMPT = """You are an expert software engineering assistant. Analyze the following task description and determine:

1. **task_type**: The type of task. Choose from:
   - "feature": New functionality or significant enhancement (adds new capabilities)
   - "bugfix": Fixing a bug or issue (corrects incorrect behavior)
   - "review": Code review, audit, or analysis without code changes
   - "small": Minor fix, typo, or simple change (trivial scope, e.g., README update, comment fix)
   - "directive": Following specific instructions or requirements

   IMPORTANT: Do NOT use "discovery" - discovery mode is triggered separately via --discover flag.

2. **scope**: Brief description of what files/modules are likely affected

3. **complexity**: "simple", "medium", or "complex"

4. **reasoning**: Brief explanation of your classification

5. **selected_items**: Select the individual Requirements (spec items) relevant to this task. The full item list is NOT injected — only the ROOT VIEW of the spec index is shown below (every spec + a one-sentence locator + item count). You MUST drill down on demand to discover the individual items, then select the relevant *leaf* items. Be selective — only include items genuinely relevant. The base spec is always loaded automatically, so you do NOT need to select items from it.

   **Drill-down protocol** — run these read-only commands yourself (Bash is available in this step; their stdout is your navigation surface):
   - ``se3 spec index`` — the root view (also shown below): all specs + locators + item counts.
   - ``se3 spec index <spec>`` — the item index of one spec. Lines shaped ``- <spec>::<requirement>`` are the selectable leaf items.
   - ``se3 spec index <spec> <group>...`` — drill into a ``[group]`` / ``[page]`` navigation handle that a larger view collapsed; each handle prints the exact command to drill it.
   - ``se3 spec show <spec>::<requirement>`` — read one item's full body if you need its detail before deciding.

   **Selection rules:**
   - Each entry in ``selected_items`` MUST be a real flat leaf address — ``{{"spec": "<spec>", "requirement_name": "<requirement>"}}`` exactly as it appears in a ``- <spec>::<requirement>`` line. A domain group name, a ``pN`` page handle, or any intermediate navigation node is NOT a selectable item and will be rejected.
   - **Wildcard**: Use ``"*"`` as the ``requirement_name`` to select ALL items from a spec, e.g. ``{{"spec": "issue-management", "requirement_name": "*"}}``.
   - **No relevant items?** If no non-base spec items are relevant, output exactly: ``{{"spec": "base", "requirement_name": "*"}}``. This explicitly signals "no additional specs needed". Never return an empty ``selected_items`` list.

## Spec Index — Root View
{root_view}

Respond in JSON format:
{{
    "task_type": "feature|bugfix|review|small|directive",
    "scope": "description of affected areas",
    "complexity": "simple|medium|complex",
    "reasoning": "explanation",
    "selected_items": [
        {{"spec": "spec-name", "requirement_name": "Requirement Name or * for all"}}
    ]
}}

Task description:
---
{task_description}
---

Project context:
{project_context}
"""

# Splice the two-segment sentinel markers (TEMPLATE_PREFIX_END /
# USER_CONTENT_BEGIN) right before the ``Task description:`` block.
#
# The ``analyze`` step has no user-literal field at the prompt-assembly point:
# ``task_description`` carries either the upstream ``refined_description``
# (when discovery preceded) or composed framework text (base + recorded
# interjections). Per the three-segment marker protocol the USER_CONTENT
# section is therefore empty; we intentionally stick with the legacy
# two-segment ``inject_boundary`` call so the web console falls back to
# rendering the whole post-BEGIN tail inside the collapsed system-prompt chip.
ANALYZE_PROMPT = inject_boundary(ANALYZE_PROMPT, "Task description:\n")


def analyze_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the analyze step.

    This is the "super-analyze" handler that combines:
    1. Programmatic pre-processing: project context collection + spec name listing
    2. Single LLM call: task classification + spec selection
    3. Programmatic post-processing: spec content loading (base + selected)

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        StepStatus.COMPLETED on success, StepStatus.FAILED on error
    """
    # Prefer refined_description from discovery step over raw task_description
    task_description = step.inputs.get("refined_description") or step.inputs.get("task_description", "")

    if not task_description:
        step.error_message = "No task description provided"
        return StepStatus.FAILED

    project_root = flow.change_path.parent if flow.change_path else Path.cwd()

    # --- Pre-processing (programmatic, no LLM) ---

    # (1) Collect structured project context
    project_summary = _collect_project_summary(project_root)

    # (2) Build the item-level index and render the ROOT VIEW only. The full
    # item list is no longer injected; the agent drills down on demand via
    # `se3 spec index <spec> [<group>...]` (same renderer as the CLI, so the
    # injected root view is byte-identical to `se3 spec index`).
    builder = ContextBuilder(project_root)
    index = load_or_build(project_root)
    threshold = load_spec_governance_config(project_root).index_render_threshold
    root_view = render_index(index, spec=None, threshold=threshold)

    # (3) List spec names for validating LLM-returned selected_items
    spec_names = list_spec_names(builder.specs_dir)

    # Build prompt with project context and the spec-index root view
    prompt = ANALYZE_PROMPT.format(
        task_description=task_description,
        project_context=project_summary,
        root_view=root_view,
    )

    # Append issue discovery injection if applicable
    from ..context_builder import get_issue_discovery_injection, get_runtime_environment_injection
    injection = get_issue_discovery_injection("analyze", project_root)
    if injection:
        prompt += injection
    runtime_env = get_runtime_environment_injection("analyze", project_root)
    if runtime_env:
        prompt += runtime_env

    # Reinforce the code-first / spec-assistant role using the single
    # authoritative wording from ``engine.spec_role``: the specs selected here
    # are a read-only reference to the code's current state, not architectural
    # contracts the task must be aligned to, and analyze must not propose
    # creating or rewriting spec files.
    prompt += "\n\n" + SPEC_ROLE_DEFINITION

    logger.info(f"Analyzing task: {task_description[:60]}...")

    try:
        # --- LLM call: task classification + spec selection ---
        # The call is a single `caller.call()` whose underlying CLI subprocess
        # carries its own tool loop, so the agent drills the spec index down to
        # leaf items WITHIN one call. The bounded loop here is the OUT-PORT
        # validation channel of the item-identity invariant (machine guarantee
        # c): after parsing the JSON we check every selected_items entry against
        # the flat item full set; if any is a group/page handle or intermediate
        # navigation node we feed the offending addresses back and re-prompt.
        retry_count = step.inputs.get("retry_count", 0)
        caller = LLMCaller(project_root, flow_id=flow.flow_id, step_id=step.step_id, step_type=step.step_type.value, external_attempt=retry_count, fix_iteration=step.inputs.get("fix_iteration", 0))

        # Schema hint is critical for TWO_PHASE mode: if Phase 1 produces
        # markdown prose (not JSON), Phase 2 extraction needs to know the
        # expected structure to extract selected_items correctly.
        ANALYZE_SCHEMA_HINT = (
            '{"task_type": "feature|bugfix|review|small|directive", '
            '"scope": "description of affected areas", '
            '"complexity": "simple|medium|complex", '
            '"reasoning": "explanation", '
            '"selected_items": [{"spec": "spec-name", "requirement_name": "Requirement Name or * for all items in spec"}]}'
        )

        result: dict = {}
        selected_items: list = []
        feedback = ""
        for attempt in range(_MAX_SELECTION_VALIDATION_RETRIES + 1):
            response = caller.call(
                prompt=prompt + feedback,
                json_mode="two_phase",
                json_schema_hint=ANALYZE_SCHEMA_HINT,
                required_keys=["task_type"],
            )

            # Parse JSON response
            result = parse_json_response(response, required_keys=["task_type"])

            if not result:
                step.error_message = "Failed to parse LLM response"
                return StepStatus.FAILED

            # Normalize selected_items (legacy fallback + list coercion), then
            # run the out-port validation against the flat item full set.
            selected_items = _normalize_selected_items(result, index, spec_names)
            invalid = _validate_selected_items_against_flat_set(selected_items, index)

            if not invalid:
                break

            if attempt < _MAX_SELECTION_VALIDATION_RETRIES:
                logger.warning(
                    "selected_items contained %d non-item address(es) "
                    "(group/page handle or intermediate node); re-prompting "
                    "(attempt %d/%d): %r",
                    len(invalid), attempt + 1,
                    _MAX_SELECTION_VALIDATION_RETRIES, invalid,
                )
                feedback = _build_validation_feedback(invalid)
                continue

            # Retries exhausted: drop the invalid entries (keep the valid ones)
            # so the flow still proceeds rather than failing outright.
            logger.warning(
                "selected_items still invalid after %d retries; dropping "
                "non-item address(es): %r",
                _MAX_SELECTION_VALIDATION_RETRIES, invalid,
            )
            selected_items = _keep_valid_items(selected_items, index)

        # Validate task_type
        valid_types = ["feature", "bugfix", "review", "small", "directive"]
        task_type = result.get("task_type", "feature")
        if task_type not in valid_types:
            logger.warning(f"Invalid task_type '{task_type}', defaulting to 'feature'")
            task_type = "feature"

        # Extract task_type from analyze result (discovery only valid with --discover flag)
        resolved_task_type = _extract_task_type(result, flow)

        # Explicit --type flag overrides LLM analysis
        resolved_task_type = _handle_type_conflict(flow, resolved_task_type)

        # Update state with resolved task type
        flow.state.update_task_type(resolved_task_type)
        flow.task_type = resolved_task_type

        # --- Post-processing: load spec content programmatically ---
        # Guarantee non-empty selected_items (LLM is instructed to output
        # base::* when no items are relevant). If all items were dropped by
        # validation, insert base::* as a safe fallback.
        if not selected_items:
            logger.warning(
                "selected_items is empty after validation; falling back to base::*"
            )
            selected_items = [
                {"spec": "base", "requirement_name": "*"}
            ]

        # Use spec_loader to assemble spec content
        load_result = load_for_step(
            step_type="analyze",
            selected_items=selected_items,
            project_root=project_root,
            mode="items",
        )

        # Defensive: detect hallucinated requirement names that passed spec-name
        # validation but don't exist in the actual spec files.
        selected_ids = {
            f"{item.get('spec')}::{item.get('requirement_name')}"
            for item in selected_items
            if isinstance(item, dict) and item.get("spec") and item.get("requirement_name")
        }
        # Exclude base-spec selections (base is always loaded as full text)
        non_base_selected_ids = {sid for sid in selected_ids if not sid.startswith("base::")}
        loaded_ids = set(load_result.loaded_items)
        if non_base_selected_ids and loaded_ids < non_base_selected_ids:
            missing = sorted(non_base_selected_ids - loaded_ids)
            logger.warning(
                "LLM selected %d non-base item(s) but only %d loaded — "
                "hallucinated requirement names: %r",
                len(non_base_selected_ids), len(loaded_ids), missing,
            )

        # Store outputs
        step.outputs["task_type"] = task_type
        step.outputs["scope"] = result.get("scope", "")
        step.outputs["complexity"] = result.get("complexity", "medium")
        step.outputs["reasoning"] = result.get("reasoning", "")
        step.outputs["project_summary"] = project_summary
        step.outputs["relevant_specs"] = load_result.relevant_specs
        step.outputs["spec_content"] = load_result.text
        step.outputs["selected_items"] = selected_items

        # Persist selected_items in flow context for cross-session /
        # cross-CONFIRM resilience — downstream steps that scan history
        # for selected_items will find it even if the ANALYZE step object
        # is not directly reachable.
        flow.state.context["selected_items"] = selected_items

        # Update flow's selected steps based on task_type (fixed sequences)
        # Note: discover mode is handled separately via --discover flag, not by analyze
        _update_flow_steps(flow, resolved_task_type)

        logger.info(
            f"Analysis complete: type={resolved_task_type}, "
            f"complexity={result.get('complexity')}, "
            f"specs={load_result.relevant_specs}, "
            f"items={len(selected_items)}"
        )

        return StepStatus.COMPLETED

    except Exception as e:
        logger.exception("Analyze step failed")
        step.error_message = f"Analysis failed: {str(e)}"
        return StepStatus.FAILED


def _extract_task_type(analyze_output: dict, flow: FlowInstance) -> str:
    """Extract task_type from LLM analyze output.

    Args:
        analyze_output: The parsed JSON output from analyze step
        flow: The flow instance (for checking explicit_type)

    Returns:
        The extracted task type string
    """
    valid_types = ["feature", "bugfix", "review", "small", "directive"]
    task_type = analyze_output.get("task_type", "feature")
    
    # Discovery mode can ONLY be triggered by --discover flag, never by analyze
    # If analyze returns "discovery", treat it as "feature"
    if task_type == "discovery":
        explicit_type = flow.state.context.get("explicit_type")
        if explicit_type != "discovery":
            logger.warning(f"Analyze returned 'discovery' but --discover flag not set, treating as 'feature'")
            task_type = "feature"
    
    # Validate and normalize task type
    if task_type not in valid_types:
        logger.warning(f"Invalid task_type '{task_type}' from analyze, defaulting to 'feature'")
        task_type = "feature"

    return task_type


def _handle_type_conflict(flow: FlowInstance, resolved_type: str) -> str:
    """Check if explicit --type flag should override LLM analysis.

    When user explicitly specifies --type, that takes precedence over
    whatever the LLM classified the task as.

    Args:
        flow: The flow instance containing context
        resolved_type: The task type determined by analyze step

    Returns:
        The final task type to use (explicit overrides analyzed)
    """
    explicit_type = flow.state.context.get("explicit_type")

    if explicit_type and explicit_type != resolved_type:
        logger.info(
            f"Explicit --type='{explicit_type}' overrides "
            f"analyzed type='{resolved_type}'"
        )
        return explicit_type

    return resolved_type


def _collect_project_summary(project_root: Path) -> str:
    """Collect structured project context and format as text summary.

    Uses ProjectContextCollector to gather git status, flow engine state,
    backlog, and spec list, then formats them into a concise text block.
    This replaces the former PROJECT_SUMMARY LLM step with a programmatic
    approach — no LLM call needed.

    Args:
        project_root: Project root directory

    Returns:
        Formatted project context string
    """
    try:
        collector = ProjectContextCollector(project_root)
        raw = collector.collect()
    except Exception as e:
        logger.debug(f"Failed to collect project context: {e}")
        return "No additional context available"

    parts: List[str] = []

    # Git status
    git = raw.get("git", {})
    branch = git.get("branch", "unknown")
    uncommitted = git.get("uncommitted_count", 0)
    parts.append(f"Branch: {branch}")
    if uncommitted:
        parts.append(f"Uncommitted changes: {uncommitted}")
    commits = git.get("last_commits", [])
    if commits:
        parts.append("Recent commits:")
        for c in commits[:5]:
            parts.append(f"  - {c}")

    # Flow engine
    flow_engine = raw.get("flow_engine")
    if flow_engine:
        active = flow_engine.get("active_flows", [])
        if active:
            parts.append(f"Active flows: {len(active)}")
            for f in active[:3]:
                parts.append(f"  - {f.get('description', 'unknown')}")

    # Backlog highlights
    backlog = raw.get("backlog", [])
    if backlog:
        parts.append(f"Backlog items: {len(backlog)}")
        for item in backlog[:5]:
            status = item.get("status", "?")
            title = item.get("title", item.get("slug", "?"))
            parts.append(f"  - [{status}] {title}")

    # Specs
    specs = raw.get("specs", [])
    if specs:
        parts.append(f"Available specs: {', '.join(specs)}")

    return "\n".join(parts) if parts else "No additional context available"


def _flat_item_ids(index: Any) -> set[str]:
    """Return the flat item full set: every real ``<spec>::<requirement>``.

    The ``__no_requirements__`` sentinel rows (specs with no Requirement) are
    excluded — they are not selectable items.
    """
    return {
        key
        for key, meta in getattr(index, "items", {}).items()
        if getattr(meta, "requirement_name", "") != _NO_REQUIREMENTS_SENTINEL
    }


def _item_is_valid(item: Any, flat_ids: set[str], known_specs: set[str]) -> bool:
    """Decide whether a single ``selected_items`` entry is a valid flat item.

    Valid iff it is a dict carrying a non-empty ``spec`` and ``requirement_name``
    AND either:
    - ``requirement_name == "*"`` (whole-spec select) and the spec exists, OR
    - ``<spec>::<requirement_name>`` is a real flat item in the index.

    A domain group name, a ``pN`` page handle, or any intermediate navigation
    node carries no flat ``<spec>::<requirement>`` address, so it fails here.
    """
    if not isinstance(item, dict):
        return False
    spec = item.get("spec")
    req = item.get("requirement_name")
    if not spec or not req:
        return False
    if req == "*":
        return spec in known_specs
    return f"{spec}::{req}" in flat_ids


def _validate_selected_items_against_flat_set(
    selected_items: list,
    index: Any,
) -> list[str]:
    """Out-port validation: check selected_items against the flat item full set.

    Implements machine guarantee (c) of the item-identity invariant: each
    selection result is checked by its full ``<spec>::<requirement>`` logical
    address against the flat item full set; a group name / page handle /
    intermediate node (or unknown spec / unknown requirement) is a validation
    failure.

    Args:
        selected_items: The (already list-normalized) selection from the LLM.
        index: A ``SpecIndex`` exposing ``.items`` and ``.spec_metas``.

    Returns:
        A list of human-readable invalid-address strings (empty ⇒ all valid).
        ``requirement_name == "*"`` (whole-spec select) is preserved as valid
        when the spec exists.
    """
    flat_ids = _flat_item_ids(index)
    known_specs = set(getattr(index, "spec_metas", {}))
    invalid: list[str] = []
    for item in selected_items:
        if _item_is_valid(item, flat_ids, known_specs):
            continue
        if isinstance(item, dict):
            invalid.append(f"{item.get('spec')}::{item.get('requirement_name')}")
        else:
            invalid.append(repr(item))
    return invalid


def _keep_valid_items(selected_items: list, index: Any) -> list:
    """Drop every entry that fails the flat-set validation, keeping the rest."""
    flat_ids = _flat_item_ids(index)
    known_specs = set(getattr(index, "spec_metas", {}))
    return [
        item
        for item in selected_items
        if _item_is_valid(item, flat_ids, known_specs)
    ]


def _build_validation_feedback(invalid: list[str]) -> str:
    """Build the re-prompt suffix listing the rejected non-item addresses."""
    bullets = "\n".join(f"  - {addr}" for addr in invalid)
    return (
        "\n\n## Selection Validation Error\n"
        "Your previous `selected_items` contained entries that are NOT valid "
        "flat item addresses. Each selected item MUST be a real "
        "`<spec>::<requirement>` leaf exactly as it appears in a "
        "`- <spec>::<requirement>` line of `se3 spec index <spec>` output "
        "(or `<spec>` with `requirement_name` set to `\"*\"` to select the whole "
        "spec). A domain group name, a `pN` page handle, or any intermediate "
        "navigation node is NOT a selectable item.\n"
        f"Rejected entries:\n{bullets}\n"
        "Re-run `se3 spec index <spec> [<group>...]` to drill down to the leaf "
        "items, then output `selected_items` using only full "
        "`<spec>::<requirement>` addresses.\n"
    )


def _normalize_selected_items(
    result: dict,
    index: Any,
    spec_names: list[str],
) -> list:
    """Extract and list-normalize ``selected_items`` from the LLM result.

    Handles the legacy ``selected_specs`` fallback (mapping spec names to all
    their Requirements) and coerces a non-list value to an empty list. No
    flat-set validation is performed here — that is the out-port validation's
    job (see ``_validate_selected_items_against_flat_set``).
    """
    selected_items = result.get("selected_items", [])

    # Fallback: if LLM returned old-format selected_specs but no selected_items,
    # map spec names to all their requirements.
    if not selected_items and result.get("selected_specs"):
        logger.warning(
            "LLM returned legacy selected_specs instead of selected_items; "
            "falling back to full-spec loading for each selected spec. "
            "This defeats item-level loading — consider re-prompting for selected_items."
        )
        raw_specs = result.get("selected_specs", [])
        if spec_names:
            unknown = [s for s in raw_specs if s not in spec_names]
            if unknown:
                logger.warning(
                    "Filtering out unknown specs from selected_specs: %r",
                    unknown,
                )
            raw_specs = [s for s in raw_specs if s in spec_names]
        selected_items = _fallback_items_from_specs(index, raw_specs)

    if not isinstance(selected_items, list):
        logger.warning(
            "selected_items is not a list (%s), using empty list",
            type(selected_items).__name__,
        )
        selected_items = []

    return selected_items


def _fallback_items_from_specs(
    index: Any,
    selected_specs: list[str],
) -> list[dict[str, str]]:
    """Map old-format ``selected_specs`` to ``selected_items``.

    For each selected spec name, include ALL Requirements from that spec.
    This is a best-effort fallback when an older LLM returns the legacy
    ``selected_specs`` array.

    Args:
        index: A ``SpecIndex`` (or anything with ``.items`` dict).
        selected_specs: List of spec names from legacy output.

    Returns:
        List of ``{"spec": str, "requirement_name": str}`` dicts.
    """
    items: list[dict[str, str]] = []
    seen: set[str] = set()
    for spec_name in selected_specs:
        if not isinstance(spec_name, str):
            continue
        for item_id, meta in getattr(index, "items", {}).items():
            if item_id.startswith(f"{spec_name}::"):
                req_name = getattr(meta, "requirement_name", "")
                if req_name and req_name != "__no_requirements__":
                    key = f"{spec_name}::{req_name}"
                    if key not in seen:
                        seen.add(key)
                        items.append({"spec": spec_name, "requirement_name": req_name})
    return items


def _update_flow_steps(
    flow: FlowInstance,
    task_type: str,
) -> None:
    """Update the flow's selected steps based on task type.
    
    Uses predefined step sequences for each task type.
    Discover mode is handled separately via --discover flag.
    Also inserts CONFIRM steps based on configuration.

    Args:
        flow: The flow instance to update
        task_type: The determined task type (feature, bugfix, small, review, directive)
    """
    # Get default sequence for task type (fixed sequences per spec)
    selected_steps = get_default_step_sequence(task_type)

    project_root = flow.change_path.parent if flow.change_path else Path.cwd()

    # Append optional steps from se3.yaml (e.g. summarize).
    # _update_flow_steps rebuilds from the default sequence every time, so
    # applying the config once here mirrors state_machine.create_flow's
    # (default -> apply_step_config -> insert_confirmation_steps) order and
    # keeps configured steps from being dropped by the rebuild. apply_step_config
    # dedups by step value, so this never appends duplicates.
    selected_steps = apply_step_config(selected_steps, project_root)

    # Insert confirmation steps based on config
    # This ensures CONFIRM steps are added after plan as configured
    flow.state.selected_steps = insert_confirmation_steps(selected_steps, project_root)
    
    logger.info(f"Using step sequence for {task_type}: {[s.value for s in flow.state.selected_steps]}")
