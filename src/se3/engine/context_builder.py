"""Context builder for automatic context collection.

Automatically gathers relevant context from specs, previous outputs,
project state, and code for LLM calls.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, TYPE_CHECKING

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

    # Check if this step has human confirmation configured
    if lang_config.language:
        confirm_config = load_confirmation_config(project_root)
        if (confirm_config.get("enabled", False)
                and confirm_config.get("reviewer") == "human"
                and step_type in confirm_config.get("steps", [])):
            return get_language_instruction(lang_config.language, step_type)
        # LLM review prompt uses the general language setting
        if step_type == "confirm_llm_review":
            return get_language_instruction(lang_config.language, step_type)

    return ""


class ContextBuilder:
    """Builds context for flow engine steps.

    Automatically collects:
    - Relevant specifications
    - Outputs from previous steps
    - Current project state (git, files)
    - Code context based on task
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

    def build_step_context(
        self,
        step_type: str,
        task_description: str,
        previous_outputs: Optional[Dict[str, Any]] = None,
        relevant_specs: Optional[List[str]] = None,
    ) -> str:
        """Build full context string for a step.

        Args:
            step_type: Current step type (e.g., "analyze", "implement")
            task_description: Original task description
            previous_outputs: Outputs from completed steps
            relevant_specs: List of spec names to include

        Returns:
            Formatted context string for LLM prompt
        """
        sections = [
            self._build_header(step_type, task_description),
            self._build_specs_section(relevant_specs),
            self._build_previous_outputs_section(previous_outputs),
            self._build_project_state_section(),
        ]

        context = "\n\n".join(filter(None, sections))

        # Append language instruction if applicable
        lang_instruction = get_step_language_instruction(step_type, self.project_root)
        if lang_instruction:
            context += lang_instruction

        return context

    def _build_header(self, step_type: str, task_description: str) -> str:
        """Build context header."""
        return f"""# Context for {step_type.upper()} Step

## Task Description
{task_description}
"""

    def _build_specs_section(self, spec_names: Optional[List[str]]) -> Optional[str]:
        """Build specifications section."""
        if not spec_names:
            return None

        lines = ["## Relevant Specifications", ""]

        for spec_name in spec_names:
            content = self._load_spec_content(spec_name)
            if content:
                lines.append(f"### {spec_name}")
                lines.append("")
                # Truncate very long specs
                if len(content) > 10000:
                    content = content[:10000] + "\n... [truncated]"
                lines.append(content)
                lines.append("")

        return "\n".join(lines) if len(lines) > 2 else None

    def _build_previous_outputs_section(
        self, outputs: Optional[Dict[str, Any]]
    ) -> Optional[str]:
        """Build previous outputs section."""
        if not outputs:
            return None

        lines = ["## Previous Step Outputs", ""]

        for key, value in outputs.items():
            if value is None:
                continue
            lines.append(f"**{key}:**")
            lines.append("")
            if isinstance(value, (dict, list)):
                import json
                lines.append(f"```json\n{json.dumps(value, indent=2, default=str)}\n```")
            else:
                lines.append(str(value))
            lines.append("")

        return "\n".join(lines) if len(lines) > 2 else None

    def _build_project_state_section(self) -> Optional[str]:
        """Build project state section."""
        state_parts = []

        # Git status
        git_info = self._get_git_info()
        if git_info:
            state_parts.append("### Git Status\n")
            state_parts.append(f"- Branch: {git_info.get('branch', 'unknown')}")
            state_parts.append(f"- Status: {git_info.get('status', 'unknown')}")
            if git_info.get('has_changes'):
                state_parts.append(f"- Uncommitted changes: yes")
            state_parts.append("")

        # Recent files (if relevant)
        recent_files = self._get_recently_modified_files(5)
        if recent_files:
            state_parts.append("### Recently Modified Files\n")
            for f in recent_files:
                state_parts.append(f"- {f}")
            state_parts.append("")

        if not state_parts:
            return None

        return "## Project State\n\n" + "\n".join(state_parts)

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

    def _get_git_info(self) -> Optional[Dict[str, Any]]:
        """Get git repository information."""
        try:
            # Current branch
            result = subprocess.run(
                ["git", "branch", "--show-current"],
                capture_output=True,
                text=True,
                cwd=self.project_root,
            )
            branch = result.stdout.strip()

            # Status
            result = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True,
                text=True,
                cwd=self.project_root,
            )
            status_lines = result.stdout.strip().split("\n") if result.stdout.strip() else []

            return {
                "branch": branch,
                "status": f"{len(status_lines)} changed files" if status_lines else "clean",
                "has_changes": len(status_lines) > 0,
            }

        except Exception as e:
            logger.debug(f"Failed to get git info: {e}")
            return None

    def _get_recently_modified_files(self, count: int = 5) -> List[str]:
        """Get list of recently modified files."""
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD~5", "HEAD"],
                capture_output=True,
                text=True,
                cwd=self.project_root,
            )
            files = [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
            return files[:count]
        except Exception:
            return []

    def get_step_prompt_template(self, step_type: str) -> str:
        """Get default prompt template for a step.

        Args:
            step_type: Type of step

        Returns:
            Prompt template string
        """
        templates = {
            "analyze": """You are the analyze step of the SE3 flow engine.

Your task is to analyze the input and determine:
1. What type of task this is (feature, bugfix, review, directive, small)
2. What the scope of the task is
3. Which steps from the step pool are needed

Step pool available:
- analyze: Analyze input and determine task type
- read_spec: Read relevant specs
- propose: Generate change proposal
- design: Design solution and architecture
- plan_tasks: Break down into concrete tasks
- implement: Write code
- test: Run tests
- verify_spec: Check implementation vs spec
- update_spec: Update spec records
- commit: Commit changes
- summarize: Generate summary

Task description: {task_description}

Respond in this format:
```json
{{
    "task_type": "feature|bugfix|review|directive|small",
    "scope": "brief description of scope",
    "required_steps": ["step1", "step2", ...],
    "reasoning": "brief explanation"
}}
```
""",
            "read_spec": """You are the read_spec step.

Relevant specifications have been loaded in the context above.
Extract the key requirements and constraints relevant to this task.

Respond with a summary of the key points from each spec.
""",
            "propose": """You are the propose step.

Generate a change proposal based on:
1. The task description
2. The relevant specifications

The proposal should include:
- Summary of the change
- Files that will be modified
- Any new files to be created
- Potential risks or considerations

Context is provided above.
""",
            "design": """You are the design step.

Create a design document for the implementation including:
1. Architecture decisions
2. Component design
3. Interface definitions
4. Implementation approach

Use the proposal and specs from previous steps (in context).
""",
            "plan_tasks": """You are the plan_tasks step.

Break down the implementation into concrete, verifiable tasks.

Each task should:
- Have a clear objective
- Be independently verifiable
- Have estimated complexity (small/medium/large)

Respond in this format:
```json
{{
    "tasks": [
        {{"id": 1, "description": "...", "complexity": "small"}},
        ...
    ]
}}
```
""",
            "implement": """You are the implement step.

Write the code to implement the design. Focus on:
- Correct functionality
- Clean, readable code
- Following project conventions
- Proper error handling
- Writing tests for new functionality

**Test Contract (REQUIRED):**
You MUST declare these in your JSON output:
- `tests_added`: List of test file paths you created (relative to project root)
- `test_mapping`: Dict mapping test IDs to spec scenarios they verify
  - Python (pytest): "tests/test_foo.py::test_bar" -> "spec-name::Scenario Name"
  - JavaScript (jest): "tests/foo.test.js > describe > it" -> "spec-name::Scenario Name"
  - Go: "package.TestFunc" -> "spec-name::Scenario Name"
If no tests were added, use empty list/dict.

Use the context from previous steps for requirements.
""",
            "verify_spec": """You are the verify_spec step.

Check if the implementation matches the specifications.

Review:
1. Requirements from specs are met
2. Design decisions are followed
3. No unintended changes

Report any discrepancies.
""",
            "update_spec": """You are the update_spec step.

Update the relevant specifications to reflect the changes made.

Consider:
- New capabilities added
- Changes to existing behavior
- Documentation updates needed
""",
            "summarize": """You are the summarize step.

Generate a summary of what was accomplished including:
1. Changes made
2. Files modified
3. Tests status
4. Any remaining tasks or follow-ups
5. Handoff context for future sessions
""",
        }

        return templates.get(step_type, f"Execute the {step_type} step.")


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
