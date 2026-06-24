"""Plan step handler.

Unified planning step that replaces the separate propose, design, and plan_tasks steps.
Produces a complete plan document with proposal, design, and task groups in a single LLM call.
Adapts prompt depth based on task_type (feature/bugfix/directive).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from ..display import get_console
from ..formatters import TaskFormatter
from ..llm_caller import LLMCaller
from ..models import FlowInstance, Step, StepStatus
from ..prompt_markers import inject_boundary
from ..utils.json_parser import parse_json_response

logger = logging.getLogger(__name__)


# --- Prompt sections (composed based on task_type depth) ---

PLAN_PROMPT_HEADER = """You are an expert software engineering assistant. Create a complete implementation plan for the following task.

## Project Context
{project_summary}

## Task Description
{task_description}

## Task Type
{task_type}

## Scope
{scope}

The project charter (project-level conventions) and the code-index (structural
orientation map) are injected below — plan against the task, the charter, and
the code-index. Before reading source, consult the code-index map to locate the
relevant modules / symbols and pull deeper detail on demand via
`se3 code-index show <path>`.

{revision_section}
"""

# Two-segment marker only: USER_CONTENT region is empty.
# The plan step has no user-literal field at the prompt-assembly point —
# every template field (task_description, proposal, design, project_context)
# is either upstream LLM output or framework-derived. The web console
# therefore falls back to rendering the whole post-BEGIN tail inside the
# collapsed system-prompt chip.
PLAN_PROMPT_HEADER = inject_boundary(PLAN_PROMPT_HEADER, "## Project Context\n")

PROPOSAL_SECTION = """## Part 1: Proposal
Create a change proposal that includes:
1. **Summary**: Brief description of the change (2-3 sentences)
2. **Motivation**: Why this change is needed
3. **Files to Modify**: List of existing files that will be changed
4. **Files to Create**: List of new files to be created (if any)
5. **Risks**: Potential risks or considerations
"""

DESIGN_SECTION = """## Part 2: Design
Create a design document that includes:
1. **Overview**: High-level description of the solution
2. **Architecture Decisions**: Key decisions with rationale and alternatives considered
3. **Components**: Main components/modules with responsibilities
4. **Data Flow**: How data moves through the system
5. **Testing Strategy**: How to verify the implementation
"""

DESIGN_SECTION_BUGFIX = """## Part 2: Design (lightweight)
Since this is a bugfix, provide a lightweight design:
1. **Overview**: Brief description of the fix approach
2. **Components**: Which components are affected
3. **Testing Strategy**: How to verify the fix
"""

TASKS_SECTION = """## {part_label}: Task Groups
Break down the implementation into logical task groups:

### Grouping Principles
- **High cohesion within groups**: Tasks in the same group should be logically related
- **Low coupling between groups**: Groups should be as independent as possible
- **Clear dependencies**: Use `depends_on` to express inter-group dependencies
- **Sequential execution**: Groups will be executed in order

### Task Structure
Each task should:
- Have a clear objective that can be verified as complete
- Be small enough to implement in one focused session
- Include specific acceptance criteria
- Have estimated complexity (small/medium/large)
- Include `estimated_loc` (integer) for lines of code added/modified

Complexity guidelines:
- small: < 30 minutes, typically < 50 LOC
- medium: 30-90 minutes, typically 50-200 LOC
- large: > 90 minutes, typically > 200 LOC (break down further if possible)
"""

# Full depth output schema for feature/discovery
FULL_JSON_SCHEMA = """\
Respond in JSON format:
```json
{{
    "plan": {{
        "proposal": {{
            "summary": "...",
            "motivation": "...",
            "files_to_modify": ["file1.py", "file2.py"],
            "files_to_create": ["new_file.py"],
            "risks": ["risk1", "risk2"]
        }},
        "design": {{
            "overview": "...",
            "architecture_decisions": [
                {{"decision": "...", "rationale": "...", "alternatives_considered": "..."}}
            ],
            "components": [
                {{"name": "...", "responsibilities": "...", "interfaces": "..."}}
            ],
            "data_flow": "...",
            "testing_strategy": "..."
        }}
    }},
    "task_groups": [
        {{
            "group_id": "G1",
            "name": "Group name",
            "description": "What this group accomplishes",
            "group_order": 1,
            "depends_on": [],
            "tasks": [
                {{
                    "id": 1,
                    "description": "Task description",
                    "complexity": "small|medium|large",
                    "estimated_loc": 30,
                    "acceptance_criteria": ["criterion 1"],
                    "files": ["file1.py"],
                    "depends_on": []
                }}
            ]
        }}
    ],
    "total_complexity": "small|medium|large",
    "estimated_effort": "brief estimate"
}}
```

Important:
- `group_id` should be unique (G1, G2, G3...)
- `group_order` determines execution sequence
- `depends_on` lists group_ids that must complete before this group
- Each group will be implemented in a **separate LLM call with isolated context**
"""

# Medium depth output schema for bugfix
MEDIUM_JSON_SCHEMA = """\
Respond in JSON format:
```json
{{
    "plan": {{
        "proposal": {{
            "summary": "...",
            "motivation": "...",
            "files_to_modify": ["file1.py"],
            "files_to_create": [],
            "risks": ["risk1"]
        }},
        "design": {{
            "overview": "...",
            "architecture_decisions": [],
            "components": [
                {{"name": "...", "responsibilities": "..."}}
            ],
            "data_flow": "",
            "testing_strategy": "..."
        }}
    }},
    "task_groups": [
        {{
            "group_id": "G1",
            "name": "...",
            "description": "...",
            "group_order": 1,
            "depends_on": [],
            "tasks": [
                {{
                    "id": 1,
                    "description": "...",
                    "complexity": "small|medium|large",
                    "estimated_loc": 30,
                    "acceptance_criteria": ["criterion 1"],
                    "files": ["file1.py"],
                    "depends_on": []
                }}
            ]
        }}
    ],
    "total_complexity": "small|medium|large",
    "estimated_effort": "brief estimate"
}}
```
"""

# Shallow depth output schema for directive/small
SHALLOW_JSON_SCHEMA = """\
Respond in JSON format:
```json
{{
    "plan": {{
        "proposal": {{
            "summary": "...",
            "motivation": "",
            "files_to_modify": [],
            "files_to_create": [],
            "risks": []
        }},
        "design": {{
            "overview": "",
            "architecture_decisions": [],
            "components": [],
            "data_flow": "",
            "testing_strategy": ""
        }}
    }},
    "task_groups": [
        {{
            "group_id": "G1",
            "name": "...",
            "description": "...",
            "group_order": 1,
            "depends_on": [],
            "tasks": [
                {{
                    "id": 1,
                    "description": "...",
                    "complexity": "small|medium|large",
                    "estimated_loc": 30,
                    "acceptance_criteria": ["criterion 1"],
                    "files": ["file1.py"],
                    "depends_on": []
                }}
            ]
        }}
    ],
    "total_complexity": "small|medium|large",
    "estimated_effort": "brief estimate"
}}
```
"""

REVISION_SECTION = """
## Previous Plan (to revise)
{previous_output}

## Reviewer Feedback
{revision_feedback}

Revise the plan above to address the feedback. Keep what was good, fix what was flagged.
"""

VERSION_FILE_GUARDRAIL = """
## Guardrail: Do Not Bump Version Files
The project version number is owned exclusively by the engine's `version_analyze`
and `commit` steps. Do NOT create tasks or task groups whose purpose is to bump
the version recorded in any project version file, including but not limited to:

- `pyproject.toml` (Python projects, including the `[project].version` /
  `[tool.poetry].version` fields)
- `package.json` (Node.js projects, the `version` field)
- `VERSIONS.md` (changelog / version history)
- Equivalent project version files in other languages (e.g. `Cargo.toml`,
  `go.mod` major-version suffixes, `__version__` constants).

These files MUST NOT appear as the target of an `implement` group, task, or fix
iteration. The engine itself will compute the new version from the actual
changes and write the version file during the `commit` step. If the user's
request is *literally only* "bump the version", produce zero file changes in
the plan and explain in the proposal summary that the version bump will be
handled automatically by the engine.
"""

def _get_prompt_depth(task_type: str) -> str:
    """Determine prompt depth based on task_type.

    Returns: 'full', 'medium', or 'shallow'
    """
    if task_type in ("feature", "discovery"):
        return "full"
    elif task_type in ("bugfix", "fix"):
        return "medium"
    else:  # directive, small, etc.
        return "shallow"


def _build_prompt(
    task_description: str,
    task_type: str,
    scope: str,
    project_summary: str,
    revision_section: str,
    depth: str,
) -> str:
    """Build the plan prompt adapted by depth."""
    parts = []

    # Header is always included
    parts.append(PLAN_PROMPT_HEADER.format(
        task_description=task_description,
        task_type=task_type,
        scope=scope,
        project_summary=project_summary,
        revision_section=revision_section,
    ))

    if depth == "full":
        parts.append(PROPOSAL_SECTION)
        parts.append(DESIGN_SECTION)
        parts.append(TASKS_SECTION.format(part_label="Part 3"))
        parts.append(VERSION_FILE_GUARDRAIL)
        parts.append(FULL_JSON_SCHEMA)
    elif depth == "medium":
        parts.append(PROPOSAL_SECTION)
        parts.append(DESIGN_SECTION_BUGFIX)
        parts.append(TASKS_SECTION.format(part_label="Part 3"))
        parts.append(VERSION_FILE_GUARDRAIL)
        parts.append(MEDIUM_JSON_SCHEMA)
    else:  # shallow
        parts.append(TASKS_SECTION.format(part_label="Instructions"))
        parts.append(VERSION_FILE_GUARDRAIL)
        parts.append(SHALLOW_JSON_SCHEMA)

    return "\n".join(parts)


def plan_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the unified plan step.

    Generates a complete plan (proposal + design + task groups) in a single
    LLM call. Adapts prompt depth based on task_type.

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        StepStatus.COMPLETED on success, StepStatus.FAILED on error
    """
    task_description = step.inputs.get("task_description", "")
    task_type = step.inputs.get("task_type", "feature")
    scope = step.inputs.get("scope", "")
    project_summary = step.inputs.get("project_summary", "Not available")
    revision_feedback = step.inputs.get("revision_feedback", "")
    is_revision = step.inputs.get("is_revision", False)

    if not task_description:
        step.error_message = "No task description provided"
        return StepStatus.FAILED

    # Build revision section if this is a revision
    if is_revision and revision_feedback:
        previous_output = step.inputs.get("previous_output", {})
        prev_text = json.dumps(previous_output, indent=2, ensure_ascii=False, default=str) if previous_output else "(not available)"
        revision_section = REVISION_SECTION.format(
            revision_feedback=revision_feedback,
            previous_output=prev_text,
        )
    else:
        revision_section = ""

    # Determine prompt depth
    depth = _get_prompt_depth(task_type)

    # Build prompt
    prompt = _build_prompt(
        task_description=task_description,
        task_type=task_type,
        scope=scope,
        project_summary=project_summary,
        revision_section=revision_section,
        depth=depth,
    )

    # Append language instruction if configured
    from ..context_builder import (
        get_step_language_instruction,
        get_issue_discovery_injection,
        get_charter_injection,
        get_code_index_injection,
        ensure_code_index_fresh,
        get_runtime_environment_injection,
    )
    project_root = flow.change_path.parent if flow.change_path else Path.cwd()
    # All three injections key off "plan": this handler is the unified planning
    # step, and deprecated stubs (PROPOSE/DESIGN/PLAN_TASKS) forward here to do
    # plan work. Using "plan" keeps injection semantics consistent regardless
    # of which deprecated step type triggered the forward.
    lang_instruction = get_step_language_instruction("plan", project_root)
    if lang_instruction:
        prompt += lang_instruction

    # Append issue discovery injection if applicable
    injection = get_issue_discovery_injection("plan", project_root)
    if injection:
        prompt += injection

    # Inject the project charter (full text) + the code-index top map. These
    # replace the retired spec-name list: the charter carries project-level
    # conventions and the code-index top map is the structural orientation map
    # (function-level detail pulled on demand via `se3 code-index show`).
    prompt += get_charter_injection(project_root)
    # Lazy-incremental refresh so the injected map reflects source edited since
    # the last build, without a manual `se3 code-index rebuild`.
    ensure_code_index_fresh(project_root)
    prompt += get_code_index_injection(project_root)

    # Append runtime environment injection if applicable
    runtime_env = get_runtime_environment_injection("plan", project_root)
    if runtime_env:
        prompt += runtime_env

    logger.info(f"Generating plan (depth={depth}) for: {task_description[:60]}...")

    try:
        retry_count = step.inputs.get("retry_count", 0)
        caller = LLMCaller(project_root, flow_id=flow.flow_id, step_id=step.step_id, step_type=step.step_type.value, external_attempt=retry_count, fix_iteration=step.inputs.get("fix_iteration", 0))
        response = caller.call(
            prompt=prompt,
            json_mode="two_phase",
            json_schema_hint='{"plan": {"proposal": {"summary": "..."}, "design": {"overview": "..."}}, "task_groups": [{"group_id": "G1", "name": "...", "tasks": [{"id": 1, "description": "..."}]}], "total_complexity": "..."}',
            required_keys=["task_groups"],
        )

        # Parse JSON response
        result = parse_json_response(response, required_keys=["task_groups"])

        if not result:
            step.error_message = "Failed to parse plan from LLM response"
            return StepStatus.FAILED

        # Extract and store outputs
        plan = result.get("plan", {})
        task_groups = result.get("task_groups", [])

        # Ensure plan has required sub-structures with defaults
        if "proposal" not in plan:
            plan["proposal"] = {"summary": task_description, "motivation": "", "files_to_modify": [], "files_to_create": [], "risks": []}
        if "design" not in plan:
            plan["design"] = {"overview": "", "architecture_decisions": [], "components": [], "data_flow": "", "testing_strategy": ""}

        step.outputs["plan"] = plan
        step.outputs["task_groups"] = task_groups
        step.outputs["spec_changes"] = result.get("spec_changes", [])
        step.outputs["total_complexity"] = result.get("total_complexity", "medium")
        step.outputs["estimated_effort"] = result.get("estimated_effort", "")

        total_tasks = sum(len(g.get("tasks", [])) for g in task_groups)
        logger.info(f"Plan generated (depth={depth}): {len(task_groups)} groups, {total_tasks} tasks")
        logger.info(f"Summary: {plan.get('proposal', {}).get('summary', '')[:80]}...")

        # Display formatted output
        try:
            _display_plan(plan, task_groups, depth)
        except Exception as e:
            logger.warning(f"Failed to format plan output: {e}")

        return StepStatus.COMPLETED

    except Exception as e:
        logger.exception("Plan step failed")
        step.error_message = f"Plan generation failed: {str(e)}"
        return StepStatus.FAILED


def _display_plan(plan: dict, task_groups: list, depth: str) -> None:
    """Display the plan output with Rich formatting."""
    from ..display import render_proposal, render_design
    console = get_console()

    proposal = plan.get("proposal", {})
    design = plan.get("design", {})

    # Render proposal section (unless shallow depth produced empty one)
    if proposal.get("summary"):
        render_proposal(proposal)

    # Render design section (if non-trivial)
    if design.get("overview"):
        render_design(design)

    # Render task groups
    if task_groups:
        formatter = TaskFormatter(console=console)
        tree_panel = formatter.format_tasks(task_groups, mode="tree")
        console.print(tree_panel)
        summary_panel = formatter.format_summary(task_groups)
        console.print(summary_panel)
