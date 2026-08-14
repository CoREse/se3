"""Analyze step handler.

Analyzes the task description to determine:
- Task type (feature, bugfix, review, small, survey)
- Scope of changes
- Required steps for the workflow

It programmatically collects project context (replacing the former
PROJECT_SUMMARY step). Project-level conventions (the charter) and the
structural orientation map (the code-index top map) are injected into the
prompt; deeper function-level detail is pulled on demand by the agent via
``luo code-index show <path>``. The retired per-requirement spec index /
spec-selection mechanism has been removed — there is no longer a spec set to
select from.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List

from ..context import RUN_MODE_TYPES, effective_task_type
from ..llm_caller import LLMCaller
from ..implementation_strategy import ImplementationStrategyResolver
from ..models import (
    FlowInstance,
    Step,
    StepStatus,
    StepType,
    get_default_step_sequence,
)
from ._project_root import resolve_flow_project_root
from ..project_context import ProjectContextCollector
from ..prompt_markers import inject_boundary
from ..utils.json_parser import parse_json_response
from ...config import (
    append_worktree_merge_steps,
    apply_step_config,
    insert_confirmation_steps,
    insert_e2e_step,
)

logger = logging.getLogger(__name__)


ANALYZE_PROMPT = """You are an expert software engineering assistant. Analyze the following task description and determine:

1. **task_type**: The type of task. Choose from:
   - "feature": New functionality or significant enhancement (adds new capabilities)
   - "bugfix": Fixing a bug or issue (corrects incorrect behavior)
   - "review": Code review, audit, or analysis without code changes
   - "small": Minor fix, typo, or simple change (trivial scope, e.g., README update, comment fix)
   - "survey": Pure investigation task whose deliverable is a conclusion or a
     report, NOT a code change (e.g. "why is X slow?", "how does Y work?",
     "compare the two approaches and recommend one"). Pick this when the user
     asks to find out / explain / assess something and would be satisfied by an
     answer alone. If they want the problem actually fixed afterwards, it is a
     "bugfix" or "feature", not a survey.

   IMPORTANT: Do NOT use "discovery" - discovery mode is triggered separately via --discover flag.

2. **scope**: Brief description of what files/modules are likely affected

3. **complexity**: "simple", "medium", or "complex"

4. **reasoning**: Brief explanation of your classification

5. **root_cause_clear**: Boolean. Judge this on the described problem ITSELF,
   INDEPENDENTLY of the task_type you picked above — your classification may be
   overridden by an explicit user-supplied type, but this judgement is used as-is
   either way, so it must never be a rubber stamp.
   Whenever the description reports something misbehaving — a wrong or empty
   result, a failure, a crash, a hang, an intermittent or "sometimes" symptom, a
   regression — ask: is that behaviour's root cause ALREADY established? I.e. do
   the task description and the known information together pin down both the
   trigger path (what sequence of events produces the wrong behaviour) and the
   responsible code location (which function/module is at fault)? Answer false
   when the symptom is described but the mechanism behind it still has to be
   found; a plausible guess is not an established root cause. Phrasing the task
   as a desired change ("make it stop returning empty") does not make the
   mechanism known.
   Return true only when the mechanism really is pinned down, or when the
   description reports no malfunction at all (purely new functionality, a
   refactor, a rename, a docs change — nothing to root-cause).

Before reading source, consult the code-index map (injected below) to locate the
relevant modules / symbols; pull deeper detail on demand via
``luo code-index show <path>`` rather than reading whole files blindly. To find
items by keyword or regex, use ``luo code-index search <pattern>`` instead of
``grep 'pattern' tianluo/code-index.md`` — each hit carries the item's full locating
path (a symbol renders as ``relpath::local_id``, which a raw grep line cannot
show); its syntax matches grep (regex ``pattern`` by default, ``-i``/``-F``/``-m``).

Respond in JSON format:
{{
    "task_type": "feature|bugfix|review|small|survey",
    "scope": "description of affected areas",
    "complexity": "simple|medium|complex",
    "reasoning": "explanation",
    "root_cause_clear": true
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


AUTO_IMPLEMENTATION_STRATEGY_PROMPT = """

6. **implementation_strategy**: This field is conditional. Emit it only when
   your resolved task_type has a PLAN -> IMPLEMENT choice surface (feature or
   bugfix; discovery-mode flows are also applicable when the engine preserves
   that explicit run mode). Choose exactly "direct" or "planned".

7. **strategy_reason**: When and only when you emit
   implementation_strategy, give a concise reason that explicitly weighs all
   of these dimensions:
   - task scale;
   - module coupling;
   - dependency-chain depth;
   - the isolation value of independent worktrees;
   - the recovery value of fine-grained task groups; and
   - whether one autonomous implementation call can reasonably carry the
     complete task.

Prefer direct when one autonomous writable IMPLEMENT call can safely own the
complete requirement and targeted verification. Prefer planned when task-group
DAG scheduling, independent worktree isolation, or fine-grained recovery has
material value. Do not emit either field for small, review, or survey.

Add these conditional fields to the JSON object when applicable:
{
    "implementation_strategy": "direct|planned",
    "strategy_reason": "reason covering all six dimensions"
}
"""


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

    project_root = resolve_flow_project_root(flow)

    # --- Pre-processing (programmatic, no LLM) ---

    # Collect structured project context
    project_summary = _collect_project_summary(project_root)

    recommendation_requested = (
        ImplementationStrategyResolver.should_request_auto_recommendation(
            flow.state.context,
            task_type=flow.task_type,
        )
    )

    # Build prompt with project context
    prompt = ANALYZE_PROMPT.format(
        task_description=task_description,
        project_context=project_summary,
    )
    if recommendation_requested:
        prompt += AUTO_IMPLEMENTATION_STRATEGY_PROMPT

    # Append issue discovery injection if applicable
    from ..context_builder import (
        get_issue_discovery_injection,
        get_charter_injection,
        get_code_index_injection,
        ensure_code_index_fresh,
        get_runtime_environment_injection,
    )
    injection = get_issue_discovery_injection("analyze", project_root)
    if injection:
        prompt += injection
    # Inject the project charter (full text) + code-index top map: project-level
    # conventions and the structural orientation map (function-level detail
    # pulled on demand via `luo code-index show <path>`).
    prompt += get_charter_injection(project_root)
    # Read-side freshness boundary: analyze is the FIRST step of the read/plan
    # run (analyze → plan → plan_tasks → implement → self_check), and code is not
    # edited until implement. So a single incremental refresh here gives every
    # read/plan step one consistent, current map — no per-step rebuild. The only
    # OTHER refresh point in a flow is just before `git commit` (commit.py), which
    # folds the code implement just wrote into the committed map. See that call
    # site for the write-side rationale.
    ensure_code_index_fresh(project_root)
    prompt += get_code_index_injection(project_root)
    runtime_env = get_runtime_environment_injection("analyze", project_root)
    if runtime_env:
        prompt += runtime_env

    logger.info(f"Analyzing task: {task_description[:60]}...")

    try:
        retry_count = step.inputs.get("retry_count", 0)
        caller = LLMCaller(project_root, flow_id=flow.flow_id, step_id=step.step_id, step_type=step.step_type.value, external_attempt=retry_count, fix_iteration=step.inputs.get("fix_iteration", 0))

        # Schema hint for TWO_PHASE mode: if Phase 1 produces markdown prose
        # (not JSON), Phase 2 extraction needs the expected structure.
        ANALYZE_SCHEMA_HINT = (
            '{"task_type": "feature|bugfix|review|small|survey", '
            '"scope": "description of affected areas", '
            '"complexity": "simple|medium|complex", '
            '"reasoning": "explanation", '
            '"root_cause_clear": true}'
        )
        if recommendation_requested:
            ANALYZE_SCHEMA_HINT = (
                ANALYZE_SCHEMA_HINT[:-1]
                + ', "implementation_strategy": "direct|planned (only for '
                'feature|bugfix)", "strategy_reason": '
                '"reason covering all six decision dimensions"}'
            )

        # --- LLM call: task classification ---
        response = caller.call(
            prompt=prompt,
            json_mode="two_phase",
            json_schema_hint=ANALYZE_SCHEMA_HINT,
            required_keys=["task_type"],
        )

        # Parse JSON response
        result = parse_json_response(response, required_keys=["task_type"])
        if not result:
            step.error_message = "Failed to parse LLM response"
            return StepStatus.FAILED

        # Extract task_type from analyze result (discovery only valid with --discover flag)
        resolved_task_type = _extract_task_type(result, flow)

        # Explicit --type flag overrides LLM analysis
        resolved_task_type = _handle_type_conflict(flow, resolved_task_type)

        # Persist the real analyzed task type (sanitized to never be a run mode
        # like 'discovery') SEPARATELY from resolved_task_type / flow.task_type.
        # The latter deliberately stay as-is (possibly 'discovery') so the step
        # sequence & resume are untouched; commit-message / version consumers
        # read this field via effective_task_type so a --discover run is
        # prefixed with the type analyze actually inferred, not 'discovery'.
        analyzed_type = _sanitize_analyzed_type(result)
        flow.state.context["analyzed_type"] = analyzed_type

        # Read straight off the raw LLM result, deliberately BEFORE/independent of
        # the explicit --type override above: an explicit `--type bugfix` skips
        # classification but must NOT skip the root-cause judgement, since that is
        # what decides whether an INVESTIGATE round is needed.
        root_cause_clear = _extract_root_cause_clear(result)

        # Update state with resolved task type
        flow.state.update_task_type(resolved_task_type)
        flow.task_type = resolved_task_type

        # A bugfix whose mechanism is still unknown gets an INVESTIGATE round
        # before PLAN. The decision is keyed on the *effective* type rather than
        # the sequence type, so a --discover run (flow.task_type stays
        # 'discovery' to preserve its sequence) whose real inferred type is
        # bugfix goes down the same path.
        effective_type = effective_task_type(flow.state.context, resolved_task_type)
        needs_investigation = effective_type == "bugfix" and not root_cause_clear

        strategy_snapshot = ImplementationStrategyResolver.snapshot_context(
            flow.state.context
        )
        strategy = ImplementationStrategyResolver.finalize_for_analyze(
            flow.state.context,
            task_type=resolved_task_type,
            analyze_output=result,
            recommendation_requested=recommendation_requested,
            selected_steps=flow.state.selected_steps,
        )

        # INVARIANT: the finalized strategy and the step sequence it implies are
        # persisted atomically. A rebuild failure that left effective='direct'
        # stamped beside a sequence still containing PLAN would survive into
        # engine.json, and a later Skip of this step would then execute that
        # dangling PLAN inside a 'direct' flow — the exact state the transform
        # exists to prevent. So a failed rebuild unwinds the decision.
        try:
            # Update flow's selected steps based on task_type (fixed sequences).
            # Note: discover mode is handled via --discover, not by analyze.
            _update_flow_steps(
                flow,
                resolved_task_type,
                needs_investigation=needs_investigation,
                effective_strategy=strategy.effective,
            )
        except Exception:
            ImplementationStrategyResolver.restore_context(
                flow.state.context, strategy_snapshot
            )
            raise

        # Store outputs. Use the authoritative resolved value (after the
        # --discover preservation and --type override) so step.outputs agrees
        # with flow.task_type rather than diverging from a separately defaulted
        # raw value. The retired spec-selection mechanism no longer produces
        # ``spec_content`` / ``relevant_specs`` / ``selected_items`` — downstream
        # steps instead receive the charter + code-index injection. These keys are
        # kept (empty) so any defensive ``.get()`` consumers degrade cleanly.
        step.outputs["task_type"] = resolved_task_type
        step.outputs["analyzed_type"] = analyzed_type
        step.outputs["scope"] = result.get("scope", "")
        step.outputs["complexity"] = result.get("complexity", "medium")
        step.outputs["reasoning"] = result.get("reasoning", "")
        step.outputs["root_cause_clear"] = root_cause_clear
        step.outputs["requested_implementation_strategy"] = (
            strategy.requested.value if strategy.requested is not None else None
        )
        step.outputs["effective_implementation_strategy"] = (
            strategy.effective.value if strategy.effective is not None else None
        )
        step.outputs["strategy_reason"] = strategy.reason
        step.outputs["project_summary"] = project_summary
        step.outputs["relevant_specs"] = []
        step.outputs["spec_content"] = ""
        step.outputs["selected_items"] = []

        logger.info(
            f"Analysis complete: type={resolved_task_type}, "
            f"complexity={result.get('complexity')}, "
            f"root_cause_clear={root_cause_clear}"
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
    valid_types = ["feature", "bugfix", "review", "small", "survey"]
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


def _sanitize_analyzed_type(analyze_output: dict) -> str:
    """Return the LLM's real task type, never a run mode (e.g. 'discovery').

    This is the value persisted as ``analyzed_type`` and consumed via
    ``effective_task_type`` by the commit-message / version steps, so it must be
    clean at the source: a ``--discover`` run whose LLM echoes 'discovery' (or
    returns anything invalid) degrades to 'feature' here, keeping the downstream
    helper's fallback logic trivial.
    """
    valid_types = ("feature", "bugfix", "review", "small", "survey")
    task_type = analyze_output.get("task_type", "feature")
    if task_type in RUN_MODE_TYPES or task_type not in valid_types:
        return "feature"
    return task_type


def _extract_root_cause_clear(analyze_output: dict) -> bool:
    """Extract the 'is the root cause already established' judgement.

    WHY the default is False: a missing/garbled field means the judgement was
    never actually made, and the two failure modes are not symmetric — an
    unnecessary INVESTIGATE round costs one bounded, net-zero-diff step, while a
    wrongly skipped one sends PLAN off to design a fix for a mechanism nobody has
    identified. So absence degrades toward investigating.

    Args:
        analyze_output: The parsed JSON output from the analyze LLM call

    Returns:
        True only when the LLM affirmatively said the root cause is clear
    """
    if "root_cause_clear" not in analyze_output:
        logger.warning(
            "Analyze output missing 'root_cause_clear'; assuming the root cause "
            "is NOT established (a bugfix will get an investigation round)"
        )
        return False

    value = analyze_output.get("root_cause_clear")
    if isinstance(value, bool):
        return value
    # Some agents emit the JSON booleans as strings; accept the unambiguous
    # spellings rather than sending an otherwise-fine analysis to investigate.
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("true", "yes"):
            return True
        if normalized in ("false", "no"):
            return False

    logger.warning(
        f"Analyze returned a non-boolean 'root_cause_clear' ({value!r}); "
        f"assuming the root cause is NOT established"
    )
    return False


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


def _update_flow_steps(
    flow: FlowInstance,
    task_type: str,
    needs_investigation: bool = False,
    effective_strategy=None,
) -> None:
    """Update the flow's selected steps based on task type.

    Uses predefined step sequences for each task type.
    Discover mode is handled separately via --discover flag.
    Also inserts CONFIRM steps based on configuration.

    Args:
        flow: The flow instance to update
        task_type: The determined task type (feature, bugfix, small, review, survey)
        needs_investigation: Insert an INVESTIGATE step before PLAN (a bugfix
            whose root cause analyze could not establish)
        effective_strategy: Finalized direct/planned/not_applicable value. When
            omitted, use the persisted or legacy-compatible flow view.
    """
    # Get default sequence for task type (fixed sequences per spec)
    selected_steps = get_default_step_sequence(task_type)

    # The conditional INVESTIGATE goes in at the FRONT of the rebuild chain,
    # against the raw default sequence — every later stage keys off positions in
    # the sequence it is handed (apply_step_config appends, the merge pair
    # anchors on COMMIT, CONFIRM gates anchor on the step they guard). Inserting
    # after any of them would put the investigation on the wrong side of a
    # confirmation gate or of the worktree release point.
    if needs_investigation:
        selected_steps = _insert_investigate_before_plan(selected_steps)

    project_root = resolve_flow_project_root(flow)

    # Append optional steps from tianluo.yaml (e.g. summarize).
    # _update_flow_steps rebuilds from the default sequence every time, so
    # applying the config once here mirrors state_machine.create_flow's
    # (default -> apply_step_config -> [worktree merge steps] ->
    # insert_confirmation_steps) order and keeps configured steps from being
    # dropped by the rebuild. apply_step_config dedups by step value, so this
    # never appends duplicates.
    selected_steps = apply_step_config(selected_steps, project_root)

    # Opt-in e2e: insert the E2E step after TEST when the project enabled it.
    # MUST stay mirrored with StateMachine.create_flow (same position in the
    # chain: after the configured appends, before the merge pair and the
    # confirmation gates). This rebuild starts from the raw default table, so
    # without this call the E2E step create_flow inserted would be dropped the
    # moment ANALYZE completes — and ANALYZE runs first in every sequence, so the
    # step would never execute at all.
    selected_steps = insert_e2e_step(selected_steps, project_root)

    # A worktree flow's release point is the merge: the two merge-side steps
    # appended by StateMachine.create_flow must survive this analyze-time
    # re-derivation, otherwise the rebuilt sequence ends at summarize and the
    # branch never lands on master (_finalize_worktree_cleanup then misdiagnoses
    # the flow as predating the in-flow merge steps and exits 1). ANALYZE is the
    # first step of every task-type sequence and always triggers this rebuild,
    # so re-appending here is what actually makes worktree merges run.
    if getattr(flow, "is_worktree_mode", False):
        selected_steps = append_worktree_merge_steps(selected_steps)

    if effective_strategy is None:
        effective_strategy = ImplementationStrategyResolver.view(
            flow.state.context,
            task_type=task_type,
            selected_steps=flow.state.selected_steps,
        ).effective
    selected_steps = ImplementationStrategyResolver.apply_to_steps(
        selected_steps,
        effective_strategy,
    )

    # Insert confirmation steps based on config
    # This ensures CONFIRM steps are added after plan as configured
    flow.state.selected_steps = insert_confirmation_steps(selected_steps, project_root)
    
    logger.info(f"Using step sequence for {task_type}: {[s.value for s in flow.state.selected_steps]}")


def _insert_investigate_before_plan(steps: List[StepType]) -> List[StepType]:
    """Return ``steps`` with an INVESTIGATE step placed before the first PLAN.

    A sequence with no PLAN (review / small / survey) is returned untouched:
    INVESTIGATE exists to feed a plan, and survey already carries its own.
    Idempotent — a sequence that already has INVESTIGATE is left alone.
    """
    result = list(steps)
    if StepType.INVESTIGATE in result:
        return result
    try:
        insert_at = result.index(StepType.PLAN)
    except ValueError:
        return result
    result.insert(insert_at, StepType.INVESTIGATE)
    return result
