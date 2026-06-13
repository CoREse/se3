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

# The analyze step performs task classification + spec selection. Each attempt is
# one tool-enabled ``caller.call()``: the underlying CLI subprocess carries its
# own tool loop, so the agent drills the spec index down to leaf items via
# ``se3 spec index`` / ``se3 spec show`` and self-corrects INSIDE that single call
# before emitting its ``selected_items``.
#
# After the call the out-port validation (item-identity invariant, machine
# guarantee c) checks every ``selected_items`` entry against the flat item full
# set by its full ``<spec>::<requirement>`` address. Entries that are not real
# flat items (a domain group / ``pN`` page handle or an intermediate navigation
# node) are validation failures, and a single non-item address fails the whole
# selection (a valid sibling never suppresses it, because dropping a group/page
# handle would silently lose the Requirements it represents).
#
# On a validation failure the step does NOT immediately bubble FAILED to the
# engine: in a non-interactive flow the failed-step path would pause for a
# Retry/Skip/Abort human decision rather than continue the selection protocol.
# Instead the handler runs a bounded IN-STEP retry loop (``MAX_SELECTION_ATTEMPTS``)
# that feeds the rejected addresses straight back into a fresh selection call so
# the agent drills each handle down to leaf items, and retries automatically with
# no human intervention. Only after exhausting those attempts does it return
# FAILED with the rejected addresses in ``error_message`` AND record the diagnosis
# into the step's chat history, so the ENGINE-LEVEL retry path replays it as
# feedback — instead of silently degrading to unrelated base-only context.

# Bounded in-step selection-validation retries before deferring to the
# engine-level retry path. Each attempt is one tool-enabled ``caller.call()``
# whose subprocess drills the index down on its own; between attempts the
# rejected non-item addresses are fed back into the prompt.
MAX_SELECTION_ATTEMPTS = 3


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

        # --- LLM call(s): task classification + spec selection ---
        # Bounded in-step retry loop. Each iteration is one tool-enabled
        # `caller.call()` whose subprocess drills the spec index down to leaf
        # items via `se3 spec index` / `se3 spec show` and self-corrects within
        # that single call. After the call, the out-port validation (item-identity
        # invariant, machine guarantee c) rejects any non-item address (domain
        # group / `pN` page handle / intermediate node); ANY invalid address fails
        # the whole selection (a valid sibling never suppresses it).
        #
        # On a validation failure we do NOT immediately return FAILED — in a
        # non-interactive flow that would pause for a Retry/Skip/Abort human
        # decision instead of continuing the protocol. Instead we feed the rejected
        # addresses straight back into a fresh selection call and retry
        # automatically, up to MAX_SELECTION_ATTEMPTS. Only after exhausting those
        # attempts do we return FAILED for the ENGINE-LEVEL retry path, recording
        # the rejected addresses so it can replay them as feedback.
        selection_prompt = prompt
        result: dict | None = None
        selected_items: list = []
        for attempt in range(MAX_SELECTION_ATTEMPTS):
            response = caller.call(
                prompt=selection_prompt,
                json_mode="two_phase",
                json_schema_hint=ANALYZE_SCHEMA_HINT,
                required_keys=["task_type"],
            )

            # Parse JSON response
            result = parse_json_response(response, required_keys=["task_type"])
            if not result:
                step.error_message = "Failed to parse LLM response"
                return StepStatus.FAILED

            # Normalize selected_items (legacy fallback + list coercion), then run
            # the out-port validation against the flat item full set (item-identity
            # invariant, machine guarantee c).
            selected_items = _normalize_selected_items(result, index, spec_names)
            invalid = _validate_selected_items_against_flat_set(selected_items, index)
            if not invalid:
                break

            # Item-identity invariant, machine guarantee (c): ANY non-item address
            # in the selection is a validation failure. A group / page handle
            # represents additional Requirements the LLM intended to select, so we
            # MUST NOT silently drop it and proceed on the valid siblings alone —
            # that would feed downstream steps incomplete spec context while
            # masking the failure.
            failure_message = _build_selection_failure_message(invalid)
            is_last_attempt = attempt == MAX_SELECTION_ATTEMPTS - 1
            logger.warning(
                "analyze selection (attempt %d/%d): selected_items contained %d "
                "non-item address(es): %s%s",
                attempt + 1, MAX_SELECTION_ATTEMPTS, len(invalid), invalid,
                "" if is_last_attempt else
                "; feeding the rejected addresses back and retrying selection "
                "automatically (no human intervention)",
            )

            if is_last_attempt:
                # Exhausted the in-step retries. Surface the rejected addresses and
                # record the diagnosis into the step's chat history so the
                # engine-level retry path replays it as feedback. The out-port
                # validation runs AFTER `caller.call()` returns, so the diagnosis
                # is otherwise absent from the recorded prompt/response history and
                # a fresh engine-issued attempt would repeat the same handle.
                step.error_message = failure_message
                _record_selection_failure_feedback(
                    project_root,
                    flow,
                    step,
                    failure_message,
                    attempt=retry_count,
                )
                return StepStatus.FAILED

            # Feed the rejected addresses back into a fresh selection call so the
            # agent drills each handle down to its constituent leaf items, then
            # retry in-step.
            selection_prompt = (
                prompt
                + "\n\n## Previous Selection Rejected\n"
                + failure_message
            )

        # Extract task_type from analyze result (discovery only valid with --discover flag)
        resolved_task_type = _extract_task_type(result, flow)

        # Explicit --type flag overrides LLM analysis
        resolved_task_type = _handle_type_conflict(flow, resolved_task_type)

        # Update state with resolved task type
        flow.state.update_task_type(resolved_task_type)
        flow.task_type = resolved_task_type

        # --- Post-processing: load spec content programmatically ---
        # Guarantee non-empty selected_items. At this point a selection that was
        # entirely invalid handles has already returned FAILED above, so reaching
        # here with an empty selection means the LLM genuinely produced no items
        # (the prompt instructs it to output base::* when nothing is relevant).
        # base::* is the safe fallback for that legitimate no-item case.
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

        # Store outputs. Use the authoritative resolved value (after the
        # --discover preservation and --type override) so step.outputs agrees
        # with flow.task_type rather than diverging from a separately defaulted
        # raw value.
        step.outputs["task_type"] = resolved_task_type
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
    
    # Discovery mode can ONLY be triggered by the --discover flag, never by
    # analyze on its own. If analyze returns "discovery", preserve it ONLY when
    # --discover was actually set (explicit_type == "discovery"); otherwise
    # downgrade to "feature".
    if task_type == "discovery":
        explicit_type = flow.state.context.get("explicit_type")
        if explicit_type == "discovery":
            # --discover is set, so "discovery" is a legitimate, intended value.
            # Return early so the valid_types check below (which excludes
            # "discovery") does not overwrite it with "feature".
            return "discovery"
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


def _build_selection_failure_message(invalid: list[str]) -> str:
    """Build the ``step.error_message`` surfaced when the selection contained one
    or more non-item addresses.

    Implements the out-port half of the item-identity invariant (machine
    guarantee c) WITHOUT re-prompting in-step: instead of spawning extra
    ``caller.call()`` invocations, the step fails and names EVERY rejected
    address so the ENGINE-LEVEL retry path can feed them back when it re-issues
    the analyze step as a fresh single call. Any non-item address fails the whole
    selection — a valid sibling never suppresses the failure, because a dropped
    group/page handle would silently lose the Requirements it represents. The
    message instructs the agent to drill each rejected handle down to its
    constituent ``<spec>::<requirement>`` leaf items via
    ``se3 spec index <spec> [<group>...]`` and never re-return a group/page handle.
    """
    return (
        "Spec selection failed: selected_items contained non-item "
        f"addresses {invalid} (domain group names, `pN` page handles, or "
        "intermediate navigation nodes), none of which carries a flat "
        "`<spec>::<requirement>` address. Each selected item MUST be a leaf "
        "address — drill any group/page handle down to its constituent "
        "Requirements via `se3 spec index <spec> [<group>...]` (Bash is "
        "available) and select only real `- <spec>::<requirement>` lines (or "
        "`\"*\"` for a whole spec). Do NOT return any group or page handle, and "
        "do NOT drop one — re-select EVERY relevant Requirement as a leaf address."
    )


def _record_selection_failure_feedback(
    project_root: Path,
    flow: FlowInstance,
    step: Step,
    failure_message: str,
    attempt: int,
) -> None:
    """Persist the post-validation selection diagnosis into the step's chat
    history so the engine-level retry can feed it back to the next analyze call.

    The out-port validation (item-identity invariant, machine guarantee c) fires
    in step code AFTER ``caller.call()`` has already recorded the prompt/response
    pair. ``format_history_for_retry`` only replays those recorded turns, so the
    rejected-handle diagnosis would otherwise never reach the next attempt and the
    model could keep returning the same group/page handle until retries exhaust.
    Recording it as a ``user``-role turn (the role ``format_history_for_retry``
    re-emits as ``[User Prompt]:``) tagged with the same ``attempt`` /
    ``fix_iteration`` as the rejected response makes the diagnosis appear as
    feedback immediately after that response in the next retry's context.

    Best-effort: a recording failure must never mask the FAILED return, so a
    missing flow_id / step_id is a soft no-op and any error is swallowed by the
    underlying ``record_prompt`` write guard.
    """
    if not flow.flow_id or not step.step_id:
        return
    try:
        from ..chat_history import record_prompt

        record_prompt(
            project_root,
            flow.flow_id,
            step.step_id,
            step.step_type.value,
            failure_message,
            attempt=attempt,
            fix_iteration=step.inputs.get("fix_iteration", 0),
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "Failed to record analyze selection-failure feedback for retry: %s",
            exc,
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
