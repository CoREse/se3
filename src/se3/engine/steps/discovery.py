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

from ..context_builder import ContextBuilder
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

_INITIAL_DISCOVERY_PROMPT_PREFIX = """You are an expert software engineering assistant in DISCOVERY mode.

## Your Sole Responsibility

Your ONLY job is to produce a **Proposed Task Description** (the `refined_description` field) through multi-turn conversation with the user. Nothing else.

You MUST NOT:
- Give implementation plans, architecture suggestions, or design proposals
- Write or suggest code snippets
- Modify any files
- Do anything beyond asking questions, synthesizing understanding, and producing the Proposed Task Description

You MAY read spec files under `se3/specs/` and source code to ask better, more informed questions.

## Project Context

{project_context}

## Available Specifications

{specs_info}

## Base Specification (if available)

{base_spec_content}

## Discovery Context
- Discovery round: {round_number}
- Conversation history: {conversation_history}
- Initial description:
"""

_INITIAL_DISCOVERY_PROMPT_SUFFIX = """

Respond in JSON format:
{{
    "mode": "question|synthesis|confirmation",
    "content": "Your message to the user - ask questions, summarize understanding, or present refined description",
    "questions": ["question1", "question2"],  // If mode is "question", list specific questions
    "refined_description": "If mode is 'synthesis' or 'confirmation', the refined task description",
    "thinking": "Brief explanation of your approach and what you've learned so far"
}}

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
- Ask questions that help narrow down what fits within the existing architecture
- Consider available specifications when exploring requirements
- After gathering enough info, provide a synthesis (mode: "synthesis")
- Once user confirms, finalize the description (mode: "confirmation")
- Be conversational but focused on understanding requirements
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

_CONTINUE_DISCOVERY_PROMPT_PREFIX = """You are an expert software engineering assistant in DISCOVERY mode.

Continue the discovery conversation based on the user's latest response.

## Your Sole Responsibility

Your ONLY job is to produce a **Proposed Task Description** (the `refined_description` field) through multi-turn conversation with the user. Nothing else.

You MUST NOT:
- Give implementation plans, architecture suggestions, or design proposals
- Write or suggest code snippets
- Modify any files
- Do anything beyond asking questions, synthesizing understanding, and producing the Proposed Task Description

You MAY read spec files under `se3/specs/` and source code to ask better, more informed questions.

## Project Context

{project_context}

## Available Specifications

{specs_info}

## Discovery Context
- Initial description: {initial_description}
- Discovery round: {round_number}
- Conversation history: {conversation_history}
- User's latest response:
"""

_CONTINUE_DISCOVERY_PROMPT_SUFFIX = """

Respond in JSON format:
{{
    "mode": "question|synthesis|confirmation",
    "content": "Your message to the user",
    "questions": ["question1", "question2"],  // If mode is "question"
    "refined_description": "If mode is 'synthesis' or 'confirmation', the refined task description",
    "ready_to_proceed": false,  // Set to true when you have enough information to proceed
    "thinking": "Brief explanation of your current understanding"
}}

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
- Consider the existing project architecture when asking questions
- Reference available specifications when relevant
- If the user provides clear direction, acknowledge it and move toward synthesis
- If things are still unclear, ask more specific questions
- When you have enough information, provide a refined description and ask for confirmation
- Be ready to proceed only when the user explicitly confirms
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


def _gather_specs_info(builder: ContextBuilder) -> tuple[str, str]:
    """Gather information about available specs.

    Args:
        builder: ContextBuilder instance

    Returns:
        Tuple of (specs_info_str, base_spec_content)
    """
    specs_info_lines = []
    base_spec_content = ""

    # List all specs directories
    try:
        specs = []
        if builder.specs_dir.exists():
            for item in builder.specs_dir.iterdir():
                if item.is_dir() and not item.name.startswith("_"):
                    specs.append(item.name)

        if specs:
            specs_info_lines.append(f"Specs Directory: {builder.specs_dir}")
            specs_info_lines.append(f"Available Specs: {', '.join(sorted(specs))}")
            specs_info_lines.append(f"Total Specs: {len(specs)}")
        else:
            specs_info_lines.append("No specs found in specs directory")

        # Try to load base spec
        base_spec_content = builder._load_spec_content("base")
        if base_spec_content:
            specs_info_lines.append("\nBase spec loaded successfully")
        else:
            specs_info_lines.append("\nNo base spec found (optional)")

    except Exception as e:
        specs_info_lines.append(f"Error listing specs: {e}")

    return "\n".join(specs_info_lines), base_spec_content or "No base spec available"


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

    # Gather specs information
    builder = ContextBuilder(project_root)
    specs_info, base_spec_content = _gather_specs_info(builder)

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
                specs_info=specs_info,
                base_spec_content=base_spec_content,
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
                specs_info=specs_info,
                base_spec_content=base_spec_content,
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

        # Store mode-specific outputs for user-facing display
        # Clear previous outputs to avoid confusion
        step.outputs.clear()
        step.outputs["message"] = content
        step.outputs["raw_result_text"] = raw_result_text

        # Internal state should not be in user-facing outputs
        # (mode, round are tracked in discovery_state above)

        questions = result.get("questions", [])

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
            )

            return StepStatus.PAUSED

        else:
            # Still in question/synthesis mode with pending questions - continue discovery
            step.outputs["questions"] = questions
            if refined_description:
                step.outputs["proposed_description"] = refined_description
            _display_discovery_message(content, refined_description, questions, raw_result_text=raw_result_text)
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
    specs_info: str = "",
    base_spec_content: str = "",
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
        specs_info: Information about available specs
        base_spec_content: Content of base spec (if available)

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
        specs_info=specs_info,
        base_spec_content=base_spec_content,
    )

    # Append language instruction if configured
    from ..context_builder import (
        get_step_language_instruction,
        get_issue_discovery_injection,
        get_runtime_environment_injection,
    )
    lang_instruction = get_step_language_instruction("discovery", project_root)
    if lang_instruction:
        prompt += lang_instruction

    # Append issue discovery injection if applicable
    injection = get_issue_discovery_injection("discovery", project_root)
    if injection:
        prompt += injection

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
) -> None:
    """Display discovery message to user.

    Args:
        content: Message content from assistant (parsed from JSON)
        refined_description: Proposed refined description (if in synthesis mode)
        questions: List of questions (if in question mode)
        is_confirmation: If True, this is a final confirmation display (not asking for input)
        raw_result_text: The raw LLM result text (may contain narrative outside JSON)
    """
    from rich.console import Group
    from rich.markdown import Markdown
    from rich.text import Text
    from .. import display
    from ..display import get_console

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

    console = get_console()
    display.render_block_header("Discovery", "blue")
    console.print(Group(*renderables))
    console.print("")
    display.render_block_footer("blue")


