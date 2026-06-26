"""Discovery step handler.

Implements a multi-turn discovery workflow that:
1. Explores requirements with the user through conversation
2. Asks clarifying questions to understand the problem
3. Generates a refined task description
4. Waits for user confirmation before proceeding to analyze

The discovery step uses the PAUSED status to handle multi-turn conversation.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..llm_caller import LLMCaller, LLMCallError
from ..models import FlowInstance, Step, StepStatus, StepType
from ..prompt_markers import wrap_user_section
from ..utils.json_parser import parse_json_response

logger = logging.getLogger(__name__)

# Sentinel value used by the orchestrator to signal that the user confirmed
# via the programmatic confirmation gate. This must NEVER reach the LLM.
PROGRAMMATIC_CONFIRM_SENTINEL = "__PROGRAMMATIC_CONFIRM__"

# The literal response that confirms a discovery refined description and lets
# the flow advance to ANALYZE (see the *Discovery Programmatic Confirmation
# Gate* requirement). The web console's GUI confirm button submits exactly
# this value through the existing call/response channel.
DISCOVERY_CONFIRM_VALUE = "1"

# Human-facing fallback hint shown alongside the GUI confirm button on the web
# console, mirroring the CLI's "输入 1 确认" affordance. The wording is
# non-normative (see the spec); only the ``1`` confirm key is normative.
DISCOVERY_CONFIRM_HINT = "输入 1 确认并继续，或回复其它内容继续完善需求。"


def discovery_confirm_metadata(refined_description: str) -> tuple[str, list]:
    """Build the ``(prompt, options)`` display metadata for a confirm call.

    Used when a non-interactive discovery step pauses at the programmatic
    confirmation gate. The returned *prompt* is a human-readable instruction
    carrying the ``输入 1 确认`` textual fallback plus the proposed refined
    description; the single *option* encodes the GUI confirm action whose
    response value is the literal :data:`DISCOVERY_CONFIRM_VALUE` (``"1"``)
    that the gate's ``== "1"`` check expects. The web console renders the
    prompt as Markdown and the option as a one-click confirm button, so both
    affordances coexist.

    Args:
        refined_description: The proposed refined task description.

    Returns:
        A ``(prompt, options)`` tuple. ``options`` is a list with one
        ``{"label", "value"}`` dict for the confirm action.
    """
    refined = (refined_description or "").strip()
    parts = ["Discovery 已生成精炼后的任务描述。" + DISCOVERY_CONFIRM_HINT]
    if refined:
        parts.extend(["", "Proposed task description:", refined])
    prompt = "\n".join(parts)
    options = [
        {"label": f"确认并继续 (输入 {DISCOVERY_CONFIRM_VALUE})", "value": DISCOVERY_CONFIRM_VALUE}
    ]
    return prompt, options

# Discovery prompts are assembled with the three-segment marker protocol:
# the framework boilerplate / project context / discovery context labels live
# in PREFIX, the user's literal field (initial_description / user_response)
# is wrapped as the USER_CONTENT region, and the JSON schema + Handling
# Evaluative + Guidelines instructions live in SUFFIX. This keeps the web
# running-flow console's user-content bubble narrowly scoped to the user's
# actual input — Project Context, Available Specs, Base Spec, JSON template,
# and Guidelines all collapse into the system-prompt chip instead.

_INITIAL_DISCOVERY_PROMPT_PREFIX = (
    """You are an expert software engineering assistant in DISCOVERY mode.

## Your Sole Responsibility

Your ONLY job is to produce a **Proposed Task Description** (the `refined_description` field) through multi-turn conversation with the user. Nothing else.

You MUST NOT:
- Give implementation plans, architecture suggestions, or design proposals
- Write or suggest code snippets
- Modify any files
- Do anything beyond asking questions, synthesizing understanding, and producing the Proposed Task Description

Discovery is a read-only step. To ask better, more informed questions you MAY consult the code-index map and source code:
- Before reading source, consult the code-index map (the project charter + the code-index — a zoomable directory tree — are injected below) to locate relevant modules / symbols; open a collapsed directory one more level with `se3 code-index index <path>` and pull a file's function/method detail on demand with `se3 code-index show <path>`.
- For source code, use Read / Grep / Glob as usual.

## Project Context

{project_context}

## Discovery Context
- Discovery round: {round_number}
- Conversation history: {conversation_history}
- Initial description:
"""
)

_INITIAL_DISCOVERY_PROMPT_SUFFIX = """

Respond in JSON format:
{{
    "mode": "question|synthesis|confirmation",
    "content": "Your message to the user - ask questions, summarize understanding, or present refined description. MAY also carry meta-notes such as 'this is a default I picked on your behalf and you can change it'",
    "questions": ["question1", "question2"],  // If mode is "question". ONLY for true blockers — see below
    "refined_description": "If mode is 'synthesis' or 'confirmation', the refined task description. MUST be clean and final — see hard invariant below",
    "issue_operations": [  // OPTIONAL — only when the user explicitly directs an issue create/update/delete this turn. See "Issue Operations" below. Omit (or use []) otherwise.
        {{"action": "create", "title": "Short title", "description": "Details", "priority": "critical|high|medium|low", "type": "bug|feature|...", "tags": ["tag1"]}},
        {{"action": "update", "id": "<id of an issue created earlier in THIS discovery session>", "title": "...", "description": "...", "priority": "...", "type": "...", "tags": ["..."]}},
        {{"action": "delete", "id": "<id of an issue created earlier in THIS discovery session>"}}
    ],
    "thinking": "Brief explanation of your approach and what you've learned so far"
}}

Issue Operations (`issue_operations`) — strictly user-directed, scope-limited:
- Emit `issue_operations` ONLY when the user explicitly instructs you in the conversation to create, modify, or delete an issue (e.g. "把这个拆成一个 issue", "更新刚才那个 issue 的描述", "把刚建的那个删掉"). By default you MUST NOT initiate any issue operation on your own — the existing "do not self-initiate issue operations" contract is unchanged.
- `action: "create"` — a new issue. Only `description` is required; `title` / `priority` / `type` / `tags` are optional. Newly created issues are recorded as belonging to this discovery session.
- `action: "update"` — edit the fields of an issue. The `id` MUST be an issue that was created earlier within THIS discovery session via `issue_operations`. You may change only `title` / `description` / `priority` / `type` / `tags`.
- `action: "delete"` — delete an issue. The `id` MUST likewise be one created earlier within THIS discovery session.
- update/delete MUST NEVER target any pre-existing / historical / in-progress issue, or one from another session — those are out of scope and will be rejected.
- These operations NEVER include status transitions such as close / reopen / reset.
- Do NOT attempt issue writes via Bash (e.g. `se3 issue create/edit`); discovery remains read-only for the shell and the engine performs these operations from `issue_operations`.

HARD INVARIANT — `refined_description` must be clean, final, and zero open items:
- `refined_description` MUST be a clean, finalized, directly-executable task description with ZERO open items.
- It MUST NOT contain any open-item phrasing whatsoever: no "to be confirmed" / "TBD" / "to be decided" / "to be determined" / "to be supplemented" / "open question(s)" / "pending" / "either A or B (undecided)" / "待确认" / "待定" / "待补充" / "二选一未决", or any equivalent. Any matter not yet nailed down MUST NOT survive inside `refined_description` in the form of a "question".
- Every item that is not yet settled has exactly two destinations:
  1. **True blocker** (cannot proceed at all without the user's adjudication): put it in `questions`. A non-empty `questions` means discovery continues looping and does NOT reach the confirmation gate — i.e. as long as a genuine open decision remains, the user should not be asked to confirm at all.
  2. **Non-blocker** (you can reasonably pick a sensible default / make the decision yourself): write it into `refined_description` as an already-made decision (e.g. "Decided: use default value X"), and put the meta-note that "this is a default I picked on your behalf and can be changed" into `content` for the user's reference. Do NOT put non-blockers into `questions`.

Handling Evaluative/Inquisitive Initial Descriptions:

If the user's initial description is phrased as an evaluation, judgment, review, or inquiry — for example patterns like:
- "Is this correct/reasonable?" / "Does X make sense?"
- "Evaluate/judge/assess whether X is reasonable"
- "Does approach Y have problems?" / "What's wrong with Z?"
- "Please review this change/modification carefully"
- "What do you think about X?" / "Give me your opinion on X"
- References to specific code, files, commits, or change names embedded in the description
- Chinese equivalents: "这样做对吗", "评判X是否合理", "Y方案有问题吗", "仔细全面地评估/审查这个改动"

Then DO NOT ask clarifying questions about "what is the task" or "what is the scope" or "what do you want to accomplish". Instead:

1. READ the relevant code/context first using Read, Grep, Glob, Bash as needed
2. FORM a concrete substantive assessment/opinion based on what you read
3. ENGAGE the user with content-focused questions or counter-arguments about your assessment (e.g., "I see X does Y, but Z might be a concern because...")
4. CONTINUE this substantive discussion across multiple rounds
5. CONVERGE on a "correct approach" through discussion — the refined_description should describe that consensus correct approach (which may be: keep as-is, make a local fix, redo entirely, adopt a different approach, etc.)
6. The refined_description is NOT the user's original evaluation request — it is the agreed-upon correct course of action discovered through discussion

You MAY still ask about:
- Output format, deliverable boundaries, or how the result should be presented
- Priority, urgency, or sequencing of work
- Specific constraints the user cares about

You MUST NOT ask:
- "What do you want to do?" / "What is the task scope?" / "What is your goal?" when the user has already presented a concrete evaluation target
- Questions that re-ask for the task definition itself rather than probing the substance of the evaluation

If you are uncertain whether the input is evaluative/inquisitive, fall back to the normal clarification behavior below.

Guidelines:
- Start by understanding the current project context (see Project Context above)
- Ask questions that help narrow down what the user actually needs
- Consult the available specifications as a read-only reference to how the code currently behaves, not as a contract the task must be aligned to
- After gathering enough info, provide a synthesis (mode: "synthesis")
- Once user confirms, finalize the description (mode: "confirmation")
- Be conversational but focused on understanding requirements
- `refined_description` must be a clean, finalized, zero-open-item executable task description — never let any "to be confirmed / TBD / to be decided / undecided either-or / 待确认 / 待定" open item remain inside it (see the HARD INVARIANT above)
- Route every unsettled matter to one of two places: a true blocker goes into `questions` (which keeps discovery looping and stays out of the confirmation gate); a non-blocker is written into `refined_description` as an already-made decision (e.g. "Decided: use default value X") with a "default picked on your behalf, changeable" note placed in `content` — never put a non-blocker into `questions`
- Remember: your only output is the Proposed Task Description — do not produce anything else
"""

# Wrap only the user's literal field ({initial_description}) as the
# USER_CONTENT region. The placeholder is preserved verbatim so runtime
# .format() substitution lands strictly inside the marker boundary.
INITIAL_DISCOVERY_PROMPT = wrap_user_section(
    _INITIAL_DISCOVERY_PROMPT_PREFIX,
    "{initial_description}",
    _INITIAL_DISCOVERY_PROMPT_SUFFIX,
)

_CONTINUE_DISCOVERY_PROMPT_PREFIX = (
    """You are an expert software engineering assistant in DISCOVERY mode.

Continue the discovery conversation based on the user's latest response.

## Your Sole Responsibility

Your ONLY job is to produce a **Proposed Task Description** (the `refined_description` field) through multi-turn conversation with the user. Nothing else.

You MUST NOT:
- Give implementation plans, architecture suggestions, or design proposals
- Write or suggest code snippets
- Modify any files
- Do anything beyond asking questions, synthesizing understanding, and producing the Proposed Task Description

Discovery is a read-only step. To ask better, more informed questions you MAY consult the code-index map and source code:
- Before reading source, consult the code-index map (the project charter + the code-index — a zoomable directory tree — are injected below) to locate relevant modules / symbols; open a collapsed directory one more level with `se3 code-index index <path>` and pull a file's function/method detail on demand with `se3 code-index show <path>`.
- For source code, use Read / Grep / Glob as usual.

## Project Context

{project_context}

## Discovery Context
- Initial description: {initial_description}
- Discovery round: {round_number}
- Conversation history: {conversation_history}
- User's latest response:
"""
)

_CONTINUE_DISCOVERY_PROMPT_SUFFIX = """

Respond in JSON format:
{{
    "mode": "question|synthesis|confirmation",
    "content": "Your message to the user. MAY also carry meta-notes such as 'this is a default I picked on your behalf and you can change it'",
    "questions": ["question1", "question2"],  // If mode is "question". ONLY for true blockers — see below
    "refined_description": "If mode is 'synthesis' or 'confirmation', the refined task description. MUST be clean and final — see hard invariant below",
    "issue_operations": [  // OPTIONAL — only when the user explicitly directs an issue create/update/delete this turn. See "Issue Operations" below. Omit (or use []) otherwise.
        {{"action": "create", "title": "Short title", "description": "Details", "priority": "critical|high|medium|low", "type": "bug|feature|...", "tags": ["tag1"]}},
        {{"action": "update", "id": "<id of an issue created earlier in THIS discovery session>", "title": "...", "description": "...", "priority": "...", "type": "...", "tags": ["..."]}},
        {{"action": "delete", "id": "<id of an issue created earlier in THIS discovery session>"}}
    ],
    "ready_to_proceed": false,  // Set to true when you have enough information to proceed
    "thinking": "Brief explanation of your current understanding"
}}

Issue Operations (`issue_operations`) — strictly user-directed, scope-limited:
- Emit `issue_operations` ONLY when the user explicitly instructs you in the conversation to create, modify, or delete an issue (e.g. "把这个拆成一个 issue", "更新刚才那个 issue 的描述", "把刚建的那个删掉"). By default you MUST NOT initiate any issue operation on your own — the existing "do not self-initiate issue operations" contract is unchanged.
- `action: "create"` — a new issue. Only `description` is required; `title` / `priority` / `type` / `tags` are optional. Newly created issues are recorded as belonging to this discovery session.
- `action: "update"` — edit the fields of an issue. The `id` MUST be an issue that was created earlier within THIS discovery session via `issue_operations`. You may change only `title` / `description` / `priority` / `type` / `tags`.
- `action: "delete"` — delete an issue. The `id` MUST likewise be one created earlier within THIS discovery session.
- update/delete MUST NEVER target any pre-existing / historical / in-progress issue, or one from another session — those are out of scope and will be rejected.
- These operations NEVER include status transitions such as close / reopen / reset.
- Do NOT attempt issue writes via Bash (e.g. `se3 issue create/edit`); discovery remains read-only for the shell and the engine performs these operations from `issue_operations`.

HARD INVARIANT — `refined_description` must be clean, final, and zero open items:
- `refined_description` MUST be a clean, finalized, directly-executable task description with ZERO open items.
- It MUST NOT contain any open-item phrasing whatsoever: no "to be confirmed" / "TBD" / "to be decided" / "to be determined" / "to be supplemented" / "open question(s)" / "pending" / "either A or B (undecided)" / "待确认" / "待定" / "待补充" / "二选一未决", or any equivalent. Any matter not yet nailed down MUST NOT survive inside `refined_description` in the form of a "question".
- Every item that is not yet settled has exactly two destinations:
  1. **True blocker** (cannot proceed at all without the user's adjudication): put it in `questions`. A non-empty `questions` means discovery continues looping and does NOT reach the confirmation gate — i.e. as long as a genuine open decision remains, the user should not be asked to confirm at all.
  2. **Non-blocker** (you can reasonably pick a sensible default / make the decision yourself): write it into `refined_description` as an already-made decision (e.g. "Decided: use default value X"), and put the meta-note that "this is a default I picked on your behalf and can be changed" into `content` for the user's reference. Do NOT put non-blockers into `questions`.

Handling Evaluative/Inquisitive Initial Descriptions (continuation):

If the initial description was evaluative or inquisitive (recognition patterns: "Is this correct?", "Evaluate X", "Does Y have problems?", "Review this change", "What do you think about X?", Chinese equivalents like "这样做对吗" / "评判X是否合理" / "Y方案有问题吗" / "仔细评估这个改动", or references to specific code/files/commits), MAINTAIN the substantive discussion posture throughout all subsequent rounds. Do NOT revert to "let me re-confirm the task scope" or "what do you want to accomplish" midway through the conversation.

- Continue reading code and context as needed
- Continue offering substantive assessments and content-focused counter-arguments
- Let the discussion converge on a consensus "correct approach"
- The refined_description must describe that agreed-upon correct approach (keep as-is, local fix, redo, different approach, etc.), NOT restate the user's original evaluation question

You MAY still ask about output format, priority, or constraints.
You MUST NOT ask "What do you want to do?" / "What is the task scope?" when a concrete evaluation target was already given.
If uncertain, fall back to normal clarification behavior.

Guidelines:
- Consider how the code currently behaves when asking questions
- Reference the available specifications as a read-only description of current code behavior when relevant
- If the user provides clear direction, acknowledge it and move toward synthesis
- If things are still unclear, ask more specific questions
- When you have enough information, provide a refined description and ask for confirmation
- Be ready to proceed only when the user explicitly confirms
- `refined_description` must be a clean, finalized, zero-open-item executable task description — never let any "to be confirmed / TBD / to be decided / undecided either-or / 待确认 / 待定" open item remain inside it (see the HARD INVARIANT above)
- Route every unsettled matter to one of two places: a true blocker goes into `questions` (which keeps discovery looping and stays out of the confirmation gate); a non-blocker is written into `refined_description` as an already-made decision (e.g. "Decided: use default value X") with a "default picked on your behalf, changeable" note placed in `content` — never put a non-blocker into `questions`
- Remember: your only output is the Proposed Task Description — do not produce anything else
"""

# The continue prompt wraps only the {user_response} field — the latest
# user-typed message. {initial_description} from round 0 is now historical
# context and lives in PREFIX.
CONTINUE_DISCOVERY_PROMPT = wrap_user_section(
    _CONTINUE_DISCOVERY_PROMPT_PREFIX,
    "{user_response}",
    _CONTINUE_DISCOVERY_PROMPT_SUFFIX,
)


def _gather_project_context(project_root: Path) -> str:
    """Gather project context information.

    Args:
        project_root: Project root directory

    Returns:
        Formatted project context string
    """
    context_parts = []

    # Detect project type
    if (project_root / "pyproject.toml").exists():
        context_parts.append("Project Type: Python (pyproject.toml found)")
        # Try to get project name
        try:
            import tomllib
            with open(project_root / "pyproject.toml", "rb") as f:
                pyproject = tomllib.load(f)
                project_name = pyproject.get("project", {}).get("name", "Unknown")
                context_parts.append(f"Project Name: {project_name}")
        except Exception:
            pass
    elif (project_root / "package.json").exists():
        context_parts.append("Project Type: Node.js (package.json found)")
        try:
            import json
            with open(project_root / "package.json") as f:
                package = json.load(f)
                context_parts.append(f"Project Name: {package.get('name', 'Unknown')}")
        except Exception:
            pass
    elif (project_root / "Cargo.toml").exists():
        context_parts.append("Project Type: Rust (Cargo.toml found)")
    elif (project_root / "go.mod").exists():
        context_parts.append("Project Type: Go (go.mod found)")

    # Check for test framework
    if (project_root / "pytest.ini").exists() or (project_root / "setup.py").exists():
        context_parts.append("Testing: pytest")
    elif (project_root / "package.json").exists():
        context_parts.append("Testing: npm/jest")

    # Check for git info
    try:
        import subprocess
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=project_root,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            context_parts.append(f"Git Remote: {result.stdout.strip()}")

        # Get current branch
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=project_root,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            context_parts.append(f"Current Branch: {result.stdout.strip()}")
    except Exception:
        pass

    return "\n".join(context_parts) if context_parts else "No additional context available"


def discovery_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the discovery step.

    This handler manages a multi-turn conversation to explore requirements.
    It uses the PAUSED status to wait for user input between turns.

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        StepStatus.PAUSED while waiting for user, StepStatus.COMPLETED when done
    """
    # Programmatic confirmation early return: user already confirmed via program gate
    if step.inputs.get("programmatic_confirmed"):
        step.outputs["discovery_summary"] = _generate_summary(
            step.inputs.get("discovery_state", {}).get("history", [])
        )
        step.outputs["requirements_clarified"] = True
        step.outputs.pop("awaiting_programmatic_confirm", None)
        logger.info("Discovery complete - programmatic confirmation received")
        return StepStatus.COMPLETED

    # Get or initialize discovery state
    discovery_state = step.inputs.get("discovery_state", {})
    round_number = discovery_state.get("round", 0)
    conversation_history: List[Dict[str, str]] = discovery_state.get("history", [])

    initial_description = step.inputs.get("task_description", "")
    if not initial_description:
        step.error_message = "No initial description provided for discovery"
        return StepStatus.FAILED

    # Check if we're resuming from a PAUSED state with user response
    resumed = step.inputs.get("resumed", False)
    user_response = step.inputs.get("user_response", "")

    project_root = flow.change_path.parent if flow.change_path else Path.cwd()

    # Gather project context
    project_context = _gather_project_context(project_root)

    try:
        # Defensive guard: the sentinel must never reach the LLM as a user turn.
        # The orchestrator stores this in user_response only after setting
        # programmatic_confirmed=True, which is handled by the early-return guard above.
        if user_response == PROGRAMMATIC_CONFIRM_SENTINEL:
            raise RuntimeError(
                f"Discovery handler received the programmatic confirmation sentinel "
                f"'{PROGRAMMATIC_CONFIRM_SENTINEL}' without programmatic_confirmed=True. "
                f"This indicates a contract violation — the sentinel leaked into the LLM path."
            )
        if round_number == 0 and not resumed:
            # Initial discovery round
            result, raw_result_text = _run_discovery_round(
                project_root=project_root,
                flow=flow,
                step=step,
                prompt_template=INITIAL_DISCOVERY_PROMPT,
                initial_description=initial_description,
                round_number=round_number,
                conversation_history=conversation_history,
                project_context=project_context,
            )
        else:
            # Continuing discovery with user response
            if resumed and user_response:
                # Add user response to history
                conversation_history.append({
                    "role": "user",
                    "content": user_response,
                    "round": round_number,
                })

            result, raw_result_text = _run_discovery_round(
                project_root=project_root,
                flow=flow,
                step=step,
                prompt_template=CONTINUE_DISCOVERY_PROMPT,
                initial_description=initial_description,
                round_number=round_number,
                conversation_history=conversation_history,
                user_response=user_response if resumed else "",
                project_context=project_context,
            )

        # Parse result
        mode = result.get("mode", "question")
        content = result.get("content", "")
        refined_description = result.get("refined_description", "")

        # Store the LLM's full raw result text as context for subsequent rounds,
        # not just the parsed JSON content field. This preserves the complete
        # output including analysis, proposals, and conclusions.
        conversation_history.append({
            "role": "assistant",
            "content": raw_result_text,
            "round": round_number,
        })

        # Update discovery state (internal tracking only)
        step.inputs["discovery_state"] = {
            "round": round_number + 1,
            "history": conversation_history,
            "mode": mode,
        }

        # Execute any user-directed issue operations emitted this round. The LLM
        # only produces *intent* (``issue_operations``); the engine owns
        # execution and the scope guarantee — create always uses source=human,
        # while update/delete are honored only on issues created within THIS
        # discovery step. The tracking set lives in step.inputs so it survives
        # across rounds and ``--resume`` (step.outputs is cleared every round).
        # The whole block is best-effort: any failure is logged and never
        # changes the step's PAUSED/FAILED semantics. The programmatic-confirm
        # early return above never reaches here, so a confirmation turn triggers
        # no issue operations.
        op_results: List[Dict[str, Any]] = []
        issue_operations = result.get("issue_operations")
        if issue_operations:
            try:
                from ..issue_discovery import apply_discovery_issue_operations
                from ..issue_manager import IssueManager

                tracked_ids = step.inputs.get("discovery_created_issue_ids", [])
                issue_manager = IssueManager(project_root)
                new_tracked_ids, op_results = apply_discovery_issue_operations(
                    issue_manager, issue_operations, tracked_ids
                )
                step.inputs["discovery_created_issue_ids"] = new_tracked_ids

                # Surface the engine-assigned IDs back into the LLM's context.
                # The LLM emits create ops WITHOUT an id (the engine allocates
                # it), so without this note a later "delete the one just created"
                # turn has no concrete id to reference and the op silently
                # no-ops. Appending an engine note to the conversation history
                # (which is the same list object stored in discovery_state and
                # fed into the next round's prompt) lets the LLM resolve such
                # back-references to a real id. The currently-tracked set is the
                # authoritative scope for update/delete.
                engine_note = _format_issue_op_engine_note(op_results, new_tracked_ids)
                if engine_note:
                    conversation_history.append({
                        "role": "system",
                        "content": engine_note,
                        "round": round_number,
                    })
            except Exception as e:
                logger.warning("Discovery issue operations failed: %s", e)
                op_results = []

        # Store mode-specific outputs for user-facing display
        # Clear previous outputs to avoid confusion. The cross-round token-usage
        # carry (`carried_token_usage`, written by run_step's finally block on a
        # PAUSED round) MUST survive this clear: discovery PAUSEs every round, so
        # without preserving it each round's carry would degrade to a single
        # round and the terminal token_usage would only reflect the last round
        # instead of the whole discovery's real cumulative total. Preserving it
        # also makes it the data source for the CLI per-round cumulative footer.
        carried_token_usage = step.outputs.get("carried_token_usage")
        step.outputs.clear()
        if carried_token_usage is not None:
            step.outputs["carried_token_usage"] = carried_token_usage
        step.outputs["message"] = content
        step.outputs["raw_result_text"] = raw_result_text

        # Internal state should not be in user-facing outputs
        # (mode, round are tracked in discovery_state above)

        questions = result.get("questions", [])

        # Per-round token usage for the inline CLI footer. The display point sits
        # *after* this round's LLM call, so the in-scope step accumulator
        # (current_step_usage) holds exactly this round's increment. The
        # cumulative is the carried prior total (preserved across the
        # outputs.clear() above) plus this round's increment. A round that
        # issued no LLM call yields an empty round_usage and the footer is
        # suppressed downstream (see _display_discovery_message).
        from ..token_usage import UsageTotals, current_step_usage

        round_usage = current_step_usage()
        cumulative_usage = UsageTotals.from_dict(carried_token_usage)
        cumulative_usage.add(round_usage)

        if refined_description and not questions:
            # All cases with a refined description and no pending questions
            # route through the programmatic confirmation gate (user types "1" to confirm).
            # This covers: LLM confirmation mode, synthesis mode without questions, and
            # premature confirmation before any user interaction.
            step.outputs["refined_description"] = refined_description
            step.outputs["awaiting_programmatic_confirm"] = True
            logger.info("Discovery has refined description — awaiting programmatic user confirmation")

            _display_discovery_message(
                content,
                refined_description,
                questions=None,
                is_confirmation=True,
                raw_result_text=raw_result_text,
                round_usage=round_usage,
                cumulative_usage=cumulative_usage,
                op_results=op_results,
            )

            return StepStatus.PAUSED

        else:
            # Still in question/synthesis mode with pending questions - continue discovery
            step.outputs["questions"] = questions
            if refined_description:
                step.outputs["proposed_description"] = refined_description
            _display_discovery_message(
                content,
                refined_description,
                questions,
                raw_result_text=raw_result_text,
                round_usage=round_usage,
                cumulative_usage=cumulative_usage,
                op_results=op_results,
            )
            return StepStatus.PAUSED

    except LLMCallError as e:
        from ..output import render_full

        error_msg = str(e)
        if "JSON extraction failed" in error_msg:
            friendly_message = (
                "LLM 未能返回有效的 JSON 结构化输出，"
                "可能是模型生成了叙述性文本而非预期的 JSON 格式。\n\n"
                "流程引擎将自动重试此步骤。"
            )
            logger.warning(
                "Discovery: LLM did not return valid JSON output. "
                "The step will be retried automatically. Original error: %s",
                error_msg,
            )
        else:
            friendly_message = f"LLM 调用失败: {error_msg}"
            logger.warning("Discovery: LLM call failed: %s", error_msg)

        step.error_message = friendly_message
        render_full(friendly_message, title="Discovery Error")
        return StepStatus.FAILED

    except Exception as e:
        logger.exception("Discovery step failed")
        step.error_message = f"Discovery failed: {str(e)}"
        return StepStatus.FAILED


def _run_discovery_round(
    project_root: Path,
    flow: FlowInstance,
    step: Step,
    prompt_template: str,
    initial_description: str,
    round_number: int,
    conversation_history: List[Dict[str, str]],
    user_response: str = "",
    project_context: str = "",
) -> tuple[Dict[str, Any], str]:
    """Run a single discovery round with the LLM.

    Args:
        project_root: Project root directory
        flow: Current flow instance
        step: Current step
        prompt_template: Template for the prompt
        initial_description: Initial task description
        round_number: Current round number
        conversation_history: List of conversation entries
        user_response: Latest user response (if any)
        project_context: Project context information

    Returns:
        Tuple of (parsed JSON result, raw result text from LLM)
    """
    # Format conversation history for prompt
    history_text = _format_conversation_history(conversation_history)

    # Build prompt
    prompt = prompt_template.format(
        initial_description=initial_description,
        round_number=round_number,
        conversation_history=history_text,
        user_response=user_response,
        project_context=project_context,
    )

    # Append language instruction if configured
    from ..context_builder import (
        get_step_language_instruction,
        get_issue_discovery_injection,
        get_charter_injection,
        get_code_index_injection,
        get_runtime_environment_injection,
    )
    lang_instruction = get_step_language_instruction("discovery", project_root)
    if lang_instruction:
        prompt += lang_instruction

    # Append issue discovery injection if applicable
    injection = get_issue_discovery_injection("discovery", project_root)
    if injection:
        prompt += injection

    # Inject the project charter (full text) + code-index top map so discovery
    # shares the same project-level conventions and structural orientation map.
    prompt += get_charter_injection(project_root)
    # No code-index refresh here. The flow's two refresh points are analyze
    # (read-side, before any code changes) and commit (write-side). discovery
    # therefore sees the map as of the previous flow's commit — acceptable, since
    # discovery is high-level orientation, not precise structural lookup, and a
    # one-flow lag here never affects correctness. See analyze.py / commit.py.
    prompt += get_code_index_injection(project_root)

    # Append runtime environment injection if applicable
    runtime_env = get_runtime_environment_injection("discovery", project_root)
    if runtime_env:
        prompt += runtime_env

    logger.info(f"Running discovery round {round_number}")

    # Call LLM
    retry_count = step.inputs.get("retry_count", 0)
    caller = LLMCaller(
        project_root,
        flow_id=flow.flow_id,
        step_id=step.step_id,
        step_type=step.step_type.value,
        external_attempt=retry_count,
        fix_iteration=step.inputs.get("fix_iteration", 0),
    )
    # Schema hint is critical for TWO_PHASE mode: if Phase 1 produces
    # markdown prose (not JSON), Phase 2 extraction needs to know the
    # expected structure to extract mode/content/refined_description.
    DISCOVERY_SCHEMA_HINT = (
        '{"mode": "question|synthesis|confirmation", '
        '"content": "Your message to the user", '
        '"questions": ["question1", "question2"], '
        '"refined_description": "The refined task description", '
        '"issue_operations": [{"action": "create|update|delete", "id": "for update/delete", '
        '"title": "for create/update", "description": "for create/update", '
        '"priority": "for create/update", "type": "for create/update", "tags": ["for create/update"]}], '
        '"thinking": "Brief explanation of your approach"}'
    )
    response = caller.call(
        prompt=prompt,
        json_mode="two_phase",
        json_schema_hint=DISCOVERY_SCHEMA_HINT,
        required_keys=["mode", "content"],
    )

    # Parse JSON response
    result = parse_json_response(response)

    if not result:
        raise LLMCallError(
            "Discovery: LLM response could not be parsed as JSON."
        )

    # Reject outputs where every user-visible field is empty. This would
    # otherwise render as a blank Discovery panel (no content, no questions,
    # no proposed description), which is worse than failing cleanly.
    content = (result.get("content") or "").strip()
    refined = (result.get("refined_description") or "").strip()
    questions = result.get("questions") or []
    if not content and not refined and not questions:
        raise LLMCallError(
            "Discovery: LLM returned a structurally valid but empty response "
            "(no content, refined_description, or questions). This usually means "
            "Phase 1 output was pure prose and Phase 2 extraction did not recover "
            "any user-visible fields."
        )

    # Get raw result text for context preservation
    raw_result_text = caller.last_raw_result or response

    return result, raw_result_text


def _format_conversation_history(history: List[Dict[str, str]]) -> str:
    """Format conversation history for prompt inclusion.

    Args:
        history: List of conversation entries

    Returns:
        Formatted history string
    """
    if not history:
        return "(No conversation yet)"

    lines = []
    for entry in history:
        role = entry.get("role", "unknown")
        content = entry.get("content", "")
        lines.append(f"{role.upper()}: {content}")

    return "\n\n".join(lines)


def _format_issue_op_engine_note(
    op_results: List[Dict[str, Any]],
    tracked_ids: List[str],
) -> str:
    """Build an engine note describing this round's issue operations.

    The note is appended to the discovery conversation history so the LLM sees
    the concrete engine-assigned IDs (create ops are emitted without an id) and
    the current set of tracked IDs, which is the authoritative scope for any
    subsequent update/delete the user requests (e.g. "delete the one I just
    created"). Returns an empty string when there is nothing meaningful to
    surface.

    Args:
        op_results: Per-operation result records from
            ``apply_discovery_issue_operations``.
        tracked_ids: The updated set of issue IDs created within this discovery
            step (the legal scope for update/delete).

    Returns:
        A human/LLM-readable note string, or "" when there is nothing to report.
    """
    if not op_results:
        return ""

    lines: List[str] = []
    for r in op_results:
        action = r.get("action")
        status = r.get("status")
        issue_id = r.get("id")
        if status in ("created", "updated", "deleted") and issue_id:
            lines.append(f"- {action} issue {issue_id}")
        elif status in ("rejected", "error"):
            reason = r.get("reason", "")
            lines.append(
                f"- {action} {issue_id if issue_id else ''} {status}: {reason}".strip()
            )

    if not lines:
        return ""

    note = "[engine] Issue operations executed this round:\n" + "\n".join(lines)
    if tracked_ids:
        note += (
            "\nIssue IDs created in this discovery session (the only IDs you may "
            "update or delete): " + ", ".join(str(i) for i in tracked_ids) + "."
        )
    else:
        note += (
            "\nNo issues created in this discovery session remain; there are no "
            "IDs eligible for update or delete."
        )
    return note


def _generate_summary(history: List[Dict[str, str]]) -> str:
    """Generate a summary of the discovery process.

    Args:
        history: Conversation history

    Returns:
        Summary string
    """
    rounds = len([e for e in history if e.get("role") == "assistant"])
    user_inputs = len([e for e in history if e.get("role") == "user"])

    return f"Discovery completed in {rounds} rounds with {user_inputs} user inputs"


def _extract_narrative_from_raw(raw_text: Optional[str]) -> str:
    """Extract narrative text outside JSON code blocks from raw LLM result.

    Args:
        raw_text: The raw LLM result text (potentially containing JSON code blocks).

    Returns:
        Narrative text stripped of JSON code blocks, or empty string if none.
    """
    if not raw_text or not isinstance(raw_text, str):
        return ""

    import re

    from ..utils.json_parser import (
        _extract_trailing_json_string,
        looks_like_json,
        looks_like_json_object,
    )

    # Pattern: ```json ... ``` or ``` ... ``` containing JSON
    # We remove fenced JSON blocks and keep everything else.
    text = raw_text

    # Find all fenced code blocks (supports both multi-line and inline).
    # The opening fence may be followed by an optional json tag and optional
    # whitespace; the closing fence may be on the same line or the next line.
    fence_pattern = r"```(?:json)?\s*\n?(.*?)\n?```"
    parts = []
    last_end = 0

    for match in re.finditer(fence_pattern, text, re.DOTALL):
        # Check if the block content looks like JSON (lenient, same as
        # content extraction path to avoid strict-vs-lenient asymmetry).
        # Use looks_like_json (includes scalars) for fenced blocks —
        # any valid JSON inside a fence should be stripped.
        block_content = match.group(1).strip()
        if looks_like_json(block_content):
            # Keep the text before this JSON block
            before = text[last_end:match.start()].strip()
            if before:
                parts.append(before)
            last_end = match.end()
        # If not JSON, keep it (will be included in the trailing text)

    def _strip_all_trailing_jsons(text: str, out_parts: list) -> None:
        """Iteratively strip all JSON objects from text.

        Appends narrative pieces to *out_parts* in left-to-right order.
        Handles arbitrary mixes of narrative and bare JSON objects:
        "narrative\n\n{JSON1}\n\n{JSON2}"  (both JSONs stripped)
        "narrative\n\n{JSON1}\n\n{JSON2}\n\nnarrative"  (all stripped)

        Works by repeatedly extracting the rightmost JSON (via the
        lenient backward walk in `_extract_trailing_json_string`) and
        processing the prefix; narrative text *after* any stripped
        JSON is also preserved in order.
        """
        # Collect suffixes in reverse order, then reverse at end
        suffixes = []
        current = text

        while current:
            if looks_like_json_object(current):
                # pure JSON dict, strip entirely
                break

            trailing_result = _extract_trailing_json_string(current)
            if trailing_result is not None:
                trailing_json, json_start = trailing_result
                before = current[:json_start].strip()
                after = current[json_start + len(trailing_json):].strip()
                if after:
                    suffixes.append(after)
                current = before
                continue

            # Fallback: first { to last } for edge cases (e.g. text that doesn't
            # end with } but contains a JSON object in the middle).
            start = current.find("{")
            end = current.rfind("}")
            if start != -1 and end != -1 and end > start:
                candidate = current[start:end + 1].strip()
                if looks_like_json_object(candidate):
                    before = current[:start].strip()
                    after = current[end + 1:].strip()
                    if after:
                        suffixes.append(after)
                    current = before
                    continue

            # No JSON found — the entire text is narrative
            suffixes.append(current)
            break

        # Reverse to get left-to-right order
        out_parts.extend(reversed(suffixes))

    # Add any remaining text after the last JSON block
    remaining = text[last_end:].strip()
    if remaining:
        # Use looks_like_json_object (dict-only) for the no-fence path,
        # unlike fenced blocks which use looks_like_json (includes scalars
        # and arrays).  This is intentional: a bare scalar like "42" or
        # array like "[1,2]" in narrative should NOT be stripped as
        # "JSON", but any valid JSON inside a fence should be.
        if looks_like_json_object(remaining):
            # Pure JSON object, strip entirely
            pass
        else:
            _strip_all_trailing_jsons(remaining, parts)

    return "\n\n".join(parts)


def _proposed_description_block(refined_description: str) -> list:
    """Build a nested se3 reverse-color block wrapping the refined description.

    The block uses ``cyan`` (distinct from the outer blue Discovery block) and
    follows the standard se3 block layout: a reverse-color title row, a blank
    line, the refined description rendered as Markdown, a blank line, a
    fixed-width reverse-color footer block, and a trailing blank line.

    This gives the user a clear, se3-rendered start/end boundary for the
    LLM-produced ``refined_description`` without relying on any dashed lines or
    "最终任务描述" wording that may incidentally appear in the LLM text itself.

    Args:
        refined_description: The LLM-produced proposed task description.

    Returns:
        A list of Rich renderables ready to be appended into the Group.
    """
    from rich.markdown import Markdown
    from rich.text import Text
    from ..display import _reverse_footer, _reverse_title

    return [
        _reverse_title("Proposed Task Description / 最终任务描述", "cyan"),
        Text(""),
        Markdown(refined_description),
        Text(""),
        _reverse_footer("cyan"),
        Text(""),
    ]


def _display_discovery_message(
    content: str,
    refined_description: Optional[str],
    questions: Optional[List[str]] = None,
    is_confirmation: bool = False,
    *,
    raw_result_text: Optional[str] = None,
    round_usage: Optional["UsageTotals"] = None,
    cumulative_usage: Optional["UsageTotals"] = None,
    op_results: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Display discovery message to user.

    Args:
        content: Message content from assistant (parsed from JSON)
        refined_description: Proposed refined description (if in synthesis mode)
        questions: List of questions (if in question mode)
        is_confirmation: If True, this is a final confirmation display (not asking for input)
        raw_result_text: The raw LLM result text (may contain narrative outside JSON)
        op_results: Per-operation result records from the issue-operation
            executor for this round (created / updated / deleted / rejected).
            When non-empty, a compact summary section is appended so the user
            can see what was created/modified/deleted and which ops were
            rejected for being out of this discovery step's scope. ``None`` /
            empty renders nothing extra (the no-operation case).
        round_usage: This round's incremental token usage. When non-empty, a
            compact dim footer (``本轮 … · 累计 …``) is appended at the tail of
            the Discovery block (inside it, before the closing blue footer). When
            ``None`` / empty (a round that issued no LLM call — empty-input
            redraw, ``--resume`` re-display), no footer is rendered.
        cumulative_usage: The cumulative token usage up to and including this
            round (carried prior total + ``round_usage``). Paired with
            ``round_usage`` to render the ``累计 …`` portion of the footer.
    """
    from rich.console import Group
    from rich.markdown import Markdown
    from rich.text import Text
    from .. import display
    from ..display import get_console
    from ..token_usage import UsageTotals, format_round_usage_footer

    renderables = []

    # If there's narrative text outside JSON blocks in the raw result, show it first
    narrative = _extract_narrative_from_raw(raw_result_text) if raw_result_text else ""
    if narrative:
        renderables.append(Markdown(narrative))
        renderables.append(Text(""))

    if refined_description and is_confirmation:
        # Final confirmation - show LLM content as markdown, then refined
        # description wrapped in a nested cyan se3 block with clear boundaries.
        if content:
            renderables.append(Markdown(content))
            renderables.append(Text(""))
        renderables.extend(_proposed_description_block(refined_description))
        # Non-normative hint: advertise the '1' confirmation affordance
        hint = Text()
        hint.append("Type ", style="dim")
        hint.append("1", style="bold green")
        hint.append(" and press Enter to confirm and proceed,", style="dim")
        hint.append("\nor type your questions/feedback to continue discovery.", style="dim")
        renderables.append(hint)
        renderables.append(Text(""))
    elif refined_description and questions:
        # Synthesis with pending questions - show content, proposed description, then questions
        if content:
            renderables.append(Markdown(content))
            renderables.append(Text(""))
        renderables.extend(_proposed_description_block(refined_description))
        renderables.append(Text("Questions:", style="bold yellow"))
        for i, q in enumerate(questions, 1):
            renderables.append(Text(f"  {i}. {q}"))
        renderables.append(Text(""))
    elif questions:
        # Question mode - show the message and questions
        if content:
            renderables.append(Markdown(content))
            renderables.append(Text(""))
        renderables.append(Text("Questions:", style="bold yellow"))
        for i, q in enumerate(questions, 1):
            renderables.append(Text(f"  {i}. {q}"))
        renderables.append(Text(""))
    else:
        # General message mode
        renderables.append(Markdown(content))
        renderables.append(Text(""))

    # Issue-operation result summary: a compact section listing what this round
    # created / updated / deleted by user direction, plus any rejected ops
    # (out-of-scope IDs, unknown actions, errors). Only rendered when at least
    # one operation was attempted; the common no-operation round renders nothing.
    if op_results:
        created = [r.get("id") for r in op_results if r.get("status") == "created"]
        updated = [r.get("id") for r in op_results if r.get("status") == "updated"]
        deleted = [r.get("id") for r in op_results if r.get("status") == "deleted"]
        rejected = [
            r for r in op_results
            if r.get("status") in ("rejected", "error", "skipped")
        ]
        if created or updated or deleted or rejected:
            renderables.append(Text("Issue operations:", style="bold green"))
            if created:
                renderables.append(Text(f"  created: {', '.join(str(i) for i in created)}"))
            if updated:
                renderables.append(Text(f"  updated: {', '.join(str(i) for i in updated)}"))
            if deleted:
                renderables.append(Text(f"  deleted: {', '.join(str(i) for i in deleted)}"))
            for r in rejected:
                rid = r.get("id")
                id_part = f" {rid}" if rid is not None else ""
                reason = r.get("reason", "")
                renderables.append(
                    Text(f"  {r.get('status')}{id_part}: {reason}", style="yellow")
                )
            renderables.append(Text(""))

    # Per-round token-usage footer: a single dim line tying off the message
    # block. Only rendered when this round actually invoked the LLM (round_usage
    # non-empty), so empty-input redraws and --resume re-displays — which carry
    # no fresh LLM call — stay footer-free (the gate is the caller's None vs.
    # the round being empty here). It is the last renderable inside the
    # Discovery block, so the input prompt that follows flows on naturally.
    if round_usage is not None and not round_usage.is_empty():
        footer_text = format_round_usage_footer(round_usage, cumulative_usage)
        renderables.append(Text(footer_text, style="dim"))

    console = get_console()
    display.render_block_header("Discovery", "blue")
    console.print(Group(*renderables))
    console.print("")
    display.render_block_footer("blue")


