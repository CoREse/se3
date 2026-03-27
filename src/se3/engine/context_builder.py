"""Context builder for automatic context collection.

Automatically gathers relevant context from specs, previous outputs,
project state, and code for LLM calls.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import LanguageConfig

logger = logging.getLogger(__name__)

# Steps whose output is human-facing (always use general language setting)
HUMAN_FACING_STEPS = {"summarize", "discovery"}

# Steps that write specs (use spec_language setting)
SPEC_STEPS = {"update_spec"}


def get_step_language_instruction(step_type: str, project_root: Path) -> str:
    """Get the language instruction for a step based on config.

    Determines the appropriate language instruction by checking:
    1. If the step is in HUMAN_FACING_STEPS -> use config.language
    2. If the step is in SPEC_STEPS -> use config.spec_language
    3. If the step is configured for human confirmation -> use config.language
    4. Otherwise -> no language instruction

    Args:
        step_type: Current step type name (e.g., "summarize", "implement")
        project_root: Project root directory for loading config

    Returns:
        Language instruction string to append to prompt, or empty string.
    """
    from ..config import load_language_config, get_language_instruction, load_confirmation_config

    lang_config = load_language_config(project_root)

    # Check spec steps first
    if step_type in SPEC_STEPS:
        return get_language_instruction(lang_config.spec_language, step_type)

    # Check human-facing steps
    if step_type in HUMAN_FACING_STEPS:
        return get_language_instruction(lang_config.language, step_type)

    # Check if this step has confirmation configured (human or LLM reviewer)
    if lang_config.language:
        confirm_config = load_confirmation_config(project_root)
        if (confirm_config.get("enabled", False)
                and step_type in confirm_config.get("steps", [])):
            return get_language_instruction(lang_config.language, step_type)
        # LLM review prompt uses the general language setting
        if step_type == "confirm_llm_review":
            return get_language_instruction(lang_config.language, step_type)

    return ""


# Steps explicitly forbidden from issue discovery injection
ISSUE_DISCOVERY_FORBIDDEN_STEPS = {"implement", "test"}

# Default steps that receive issue discovery prompt injection
ISSUE_DISCOVERY_DEFAULT_STEPS = ["verify_spec", "summarize"]


def get_issue_discovery_injection(step_type: str, project_root: Path) -> str:
    """Get the issue discovery prompt injection for a step.

    Checks if the step is in the whitelist (from se3.yaml config or defaults)
    and not in the forbidden list, then returns the injection prompt.

    Args:
        step_type: Current step type name (e.g., "summarize", "verify_spec")
        project_root: Project root directory for loading config

    Returns:
        Issue discovery prompt string to append to prompt, or empty string.
    """
    # Forbidden steps never get injection
    if step_type in ISSUE_DISCOVERY_FORBIDDEN_STEPS:
        return ""

    # Read whitelist from se3.yaml config
    whitelist = ISSUE_DISCOVERY_DEFAULT_STEPS
    try:
        config_path = project_root / "se3.yaml"
        if config_path.exists():
            import yaml
            with open(config_path) as f:
                config = yaml.safe_load(f) or {}
            configured_steps = config.get("issue_discovery", {}).get("steps")
            if configured_steps is not None:
                whitelist = configured_steps
    except Exception:
        pass  # Use defaults on any config error

    if step_type not in whitelist:
        return ""

    # Double-check forbidden list even if config includes them
    if step_type in ISSUE_DISCOVERY_FORBIDDEN_STEPS:
        return ""

    # Return the prompt directly — don't delegate to IssueDiscovery.get_injection_prompt()
    # which has its own hardcoded whitelist. The configurable whitelist here is authoritative.
    from .issue_discovery import ISSUE_DISCOVERY_PROMPT
    return ISSUE_DISCOVERY_PROMPT


class ContextBuilder:
    """Provides spec directory resolution and spec content loading.

    Used by read_spec and discovery handlers to locate and load
    specification files from the project's specs directory.
    """

    def __init__(self, project_root: Path):
        """Initialize context builder.

        Args:
            project_root: Project root directory
        """
        self.project_root = Path(project_root)
        self.specs_dir = self._resolve_specs_dir(self.project_root)

    @staticmethod
    def _resolve_specs_dir(project_root: Path) -> Path:
        """Resolve specs directory: se3/specs/ preferred, specs/ fallback, openspec/specs/ legacy."""
        primary = project_root / "se3" / "specs"
        fallback = project_root / "specs"
        legacy = project_root / "openspec" / "specs"
        if primary.exists():
            return primary
        if fallback.exists():
            return fallback
        return legacy

    def _load_spec_content(self, spec_name: str) -> Optional[str]:
        """Load spec content by name.

        Args:
            spec_name: Name of spec (e.g., "flow-engine")

        Returns:
            Spec content or None
        """
        # Try different paths
        paths = [
            self.specs_dir / spec_name / "spec.md",
            self.specs_dir / f"{spec_name}.md",
            self.project_root / spec_name,
            self.project_root / f"{spec_name}.md",
        ]

        for path in paths:
            if path.exists():
                try:
                    return path.read_text(encoding="utf-8")
                except Exception as e:
                    logger.warning(f"Failed to read spec {path}: {e}")

        return None


def build_llm_review_prompt(
    step_to_review_type: str,
    step_output: Dict[str, Any],
    task_description: str,
    revision_feedback: Optional[str] = None,
    project_root: Optional[Path] = None,
) -> str:
    """Build a structured prompt for LLM-based review of a step's output.

    Args:
        step_to_review_type: Type of the step being reviewed (e.g., "propose", "design")
        step_output: The outputs from the reviewed step
        task_description: Original task description from the flow
        revision_feedback: Previous revision feedback if this is a re-review
        project_root: Project root directory for language config

    Returns:
        Formatted prompt string for LLM reviewer
    """
    import json as _json

    # Format step output for display
    output_parts = []
    for key, value in step_output.items():
        if key.startswith("_"):
            continue
        if isinstance(value, (dict, list)):
            output_parts.append(f"**{key}:**\n```json\n{_json.dumps(value, indent=2, default=str)}\n```")
        elif value is not None:
            output_parts.append(f"**{key}:**\n{value}")
    step_output_text = "\n\n".join(output_parts) if output_parts else "(no output)"

    revision_section = ""
    if revision_feedback:
        revision_section = f"""
## Previous Revision Feedback
The following feedback was given in a previous review. Check whether it has been addressed:
{revision_feedback}
"""

    # Language instruction
    lang_instruction = ""
    if project_root:
        lang_instruction = get_step_language_instruction("confirm_llm_review", project_root)

    prompt = f"""You are reviewing the output of the **{step_to_review_type}** step in an SE3 development workflow.

## Original Task
{task_description}

## Output of the {step_to_review_type.upper()} Step
{step_output_text}
{revision_section}
## Evaluation Criteria
Evaluate the output against these criteria:
1. **Completeness**: Does the output fully address the task requirements?
2. **Correctness**: Is the content accurate and free of errors?
3. **Clarity**: Is the output well-structured and clear?
4. **Feasibility**: Are the proposed approaches realistic and implementable?

## Response Format
You MUST respond with a JSON object in this exact format:
```json
{{
    "approved": true or false,
    "feedback": "Your detailed feedback here. If not approved, explain what needs to be changed."
}}
```

If the output is acceptable, set "approved" to true. If changes are needed, set "approved" to false and provide specific, actionable feedback.
{lang_instruction}"""

    return prompt
