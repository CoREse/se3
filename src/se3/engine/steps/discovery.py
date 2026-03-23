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
from ..utils.json_parser import parse_json_response

logger = logging.getLogger(__name__)

# Maximum number of discovery rounds before forcing a conclusion
MAX_DISCOVERY_ROUNDS = 10


# Prompt for initial discovery analysis
INITIAL_DISCOVERY_PROMPT = """You are an expert software engineering assistant in DISCOVERY mode.

Your goal is to explore and clarify the user's requirements through conversation.
Ask thoughtful questions to understand:
- What problem they're trying to solve
- The scope and boundaries of the work
- Any constraints or requirements
- The desired outcome

## Project Context

{project_context}

## Available Specifications

{specs_info}

## Base Specification (if available)

{base_spec_content}

## Discovery Context
- Initial description: {initial_description}
- Discovery round: {round_number}
- Conversation history: {conversation_history}

Respond in JSON format:
{{
    "mode": "question|synthesis|confirmation",
    "content": "Your message to the user - ask questions, summarize understanding, or present refined description",
    "questions": ["question1", "question2"],  // If mode is "question", list specific questions
    "refined_description": "If mode is 'synthesis' or 'confirmation', the refined task description",
    "thinking": "Brief explanation of your approach and what you've learned so far"
}}

Guidelines:
- You have full tool access — feel free to read spec files under `se3/specs/` and source code as needed to ask better questions
- Start by understanding the current project context (see Project Context above)
- Ask questions that help narrow down what fits within the existing architecture
- Consider available specifications when exploring requirements
- After gathering enough info, provide a synthesis (mode: "synthesis")
- Once user confirms, finalize the description (mode: "confirmation")
- Be conversational but focused on understanding requirements
- Don't implement anything - this is purely for understanding
"""

# Prompt for continuing discovery after user response
CONTINUE_DISCOVERY_PROMPT = """You are an expert software engineering assistant in DISCOVERY mode.

Continue the discovery conversation based on the user's latest response.

## Project Context

{project_context}

## Available Specifications

{specs_info}

## Discovery Context
- Initial description: {initial_description}
- Discovery round: {round_number}
- Conversation history: {conversation_history}
- User's latest response: {user_response}

Respond in JSON format:
{{
    "mode": "question|synthesis|confirmation",
    "content": "Your message to the user",
    "questions": ["question1", "question2"],  // If mode is "question"
    "refined_description": "If mode is 'synthesis' or 'confirmation', the refined task description",
    "ready_to_proceed": false,  // Set to true when you have enough information to proceed
    "thinking": "Brief explanation of your current understanding"
}}

Guidelines:
- You have full tool access — feel free to read spec files under `se3/specs/` and source code as needed to ask better questions
- Consider the existing project architecture when asking questions
- Reference available specifications when relevant
- If the user provides clear direction, acknowledge it and move toward synthesis
- If things are still unclear, ask more specific questions
- When you have enough information, provide a refined description and ask for confirmation
- Be ready to proceed only when the user explicitly confirms
"""


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

    # Check for max rounds to prevent infinite loops
    # But allow one more round if user has provided a response (to process confirmation)
    if round_number >= MAX_DISCOVERY_ROUNDS and not (resumed and user_response):
        logger.warning(f"Discovery reached max rounds ({MAX_DISCOVERY_ROUNDS}), forcing completion")
        # Generate a best-effort refined description from conversation
        refined = _generate_fallback_description(initial_description, conversation_history)
        step.outputs["refined_description"] = refined
        step.outputs["discovery_summary"] = _generate_summary(conversation_history)
        step.outputs["requirements_clarified"] = True
        return StepStatus.COMPLETED

    project_root = flow.change_path.parent if flow.change_path else Path.cwd()

    # Gather project context
    project_context = _gather_project_context(project_root)

    # Gather specs information
    builder = ContextBuilder(project_root)
    specs_info, base_spec_content = _gather_specs_info(builder)

    try:
        if round_number == 0 and not resumed:
            # Initial discovery round
            result = _run_discovery_round(
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

            result = _run_discovery_round(
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
        ready_to_proceed = result.get("ready_to_proceed", False)
        refined_description = result.get("refined_description", "")

        # Add assistant message to history
        conversation_history.append({
            "role": "assistant",
            "content": content,
            "round": round_number,
            "mode": mode,
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

        # Internal state should not be in user-facing outputs
        # (mode, round are tracked in discovery_state above)

        questions = result.get("questions", [])

        if mode == "confirmation" and ready_to_proceed and refined_description and not questions:
            # Discovery complete - user has confirmed and no remaining questions
            step.outputs["refined_description"] = refined_description
            step.outputs["discovery_summary"] = _generate_summary(conversation_history)
            step.outputs["requirements_clarified"] = True
            logger.info("Discovery complete - user confirmed refined description")

            # Show final confirmation to user
            _display_discovery_message(
                "✅ Discovery complete! Here's the confirmed task description:",
                refined_description,
                questions=None,
                is_confirmation=True,
            )

            return StepStatus.COMPLETED

        elif mode == "synthesis" and refined_description and not questions:
            # Have a refined description and no pending questions - ask for confirmation
            step.outputs["proposed_description"] = refined_description
            _display_discovery_message(content, refined_description, questions=None)
            return StepStatus.PAUSED

        else:
            # Still in question/synthesis mode with pending questions - continue discovery
            step.outputs["questions"] = questions
            _display_discovery_message(content, refined_description if mode == "synthesis" else None, questions)
            return StepStatus.PAUSED

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
) -> Dict[str, Any]:
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
        Parsed JSON result from LLM
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

    logger.info(f"Running discovery round {round_number}")

    # Call LLM
    caller = LLMCaller(
        project_root,
        flow_id=flow.flow_id,
        step_id=step.step_id,
        step_type=step.step_type.value,
    )
    response = caller.call(prompt=prompt, require_json=True)

    # Parse JSON response
    result = parse_json_response(response)

    if not result:
        raise ValueError("Failed to parse LLM response")

    return result


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


def _generate_fallback_description(initial: str, history: List[Dict[str, str]]) -> str:
    """Generate a fallback description when max rounds reached.

    Args:
        initial: Initial description
        history: Conversation history

    Returns:
        Best-effort refined description
    """
    # Combine all user inputs as the refined description
    user_inputs = [e.get("content", "") for e in history if e.get("role") == "user"]

    if user_inputs:
        return f"{initial}\n\nAdditional context from discovery:\n" + "\n".join(f"- {u}" for u in user_inputs)

    return initial


def _display_discovery_message(
    content: str,
    refined_description: Optional[str],
    questions: Optional[List[str]] = None,
    is_confirmation: bool = False,
) -> None:
    """Display discovery message to user.

    Args:
        content: Message content from assistant
        refined_description: Proposed refined description (if in synthesis mode)
        questions: List of questions (if in question mode)
        is_confirmation: If True, this is a final confirmation display (not asking for input)
    """
    from ..output import render_full

    lines = []

    if refined_description and is_confirmation:
        # Final confirmation - show the confirmed description
        lines.extend([
            content,
            "",
            "=" * 60,
            refined_description,
            "",
        ])
    elif refined_description and questions:
        # Synthesis with pending questions - show content, proposed description, then questions
        if content:
            lines.extend([content, ""])
        lines.extend([
            "📋 PROPOSED TASK DESCRIPTION:",
            "=" * 60,
            refined_description,
            "",
            "-" * 60,
            "🤔 QUESTIONS FOR YOU:",
            "-" * 60,
        ])
        for i, q in enumerate(questions, 1):
            lines.append(f"  {i}. {q}")
        lines.append("")
    elif refined_description:
        # Synthesis mode - show content explanation and proposed description
        if content:
            lines.extend([content, ""])
        lines.extend([
            "📋 PROPOSED TASK DESCRIPTION:",
            "=" * 60,
            refined_description,
            "",
            "-" * 60,
            "Does this accurately capture what you want to build?",
            "",
            "Reply with:",
            "  • 'yes' or 'ok' to proceed with this description",
            "  • 'no' or provide corrections/clarifications",
            "",
        ])
    elif questions:
        # Question mode - show the message and questions
        if content:
            lines.extend([content, ""])
        lines.extend([
            "-" * 60,
            "🤔 QUESTIONS FOR YOU:",
            "-" * 60,
        ])
        for i, q in enumerate(questions, 1):
            lines.append(f"  {i}. {q}")
        lines.append("")
    else:
        # General message mode
        lines.append(content)
        lines.append("")

    render_full("\n".join(lines), title="Discovery")


def parse_user_response(response: str) -> Dict[str, Any]:
    """Parse user response to determine if they confirmed or want to continue.

    Args:
        response: User's input

    Returns:
        Dict with 'confirmed' (bool) and 'feedback' (str)
    """
    response_lower = response.lower().strip()

    # Check for confirmation indicators
    confirm_keywords = [
        "yes", "ok", "sure", "proceed", "go ahead", "that's right",
        "correct", "looks good", "good", "fine", "confirm", "confirmed",
        "没问题", "好的", "可以", "行", "确认", "是的", "对的",
    ]

    # Check for rejection indicators
    reject_keywords = [
        "no", "not quite", "wrong", "incorrect", "needs change",
        "modify", "update", "fix", "change", "不对", "不是", "需要修改",
    ]

    confirmed = any(kw in response_lower for kw in confirm_keywords)
    rejected = any(kw in response_lower for kw in reject_keywords)

    # If clearly confirmed and no rejection indicators
    if confirmed and not rejected:
        return {"confirmed": True, "feedback": response}

    # If clearly rejected
    if rejected:
        return {"confirmed": False, "feedback": response}

    # Ambiguous - treat as feedback/continuation
    # Check for length - short responses are more likely confirmations
    if len(response_lower) < 20 and not rejected:
        return {"confirmed": True, "feedback": response}

    return {"confirmed": False, "feedback": response}
