"""Plan step handler.

Unified planning step that replaces the separate propose, design, and plan_tasks steps.
Produces a complete plan document with proposal, design, and task groups in a single LLM call.
Adapts prompt depth based on task_type (feature/bugfix/small).
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
from ..plan_decomposition import (
    PLAN_DECOMPOSITION_KEY,
    PLAN_GRANULARITY_KEY,
    PlanDecomposition,
    PlanGranularity,
    PlanModeResolver,
)
from ._project_root import resolve_flow_project_root
from ..prompt_markers import inject_boundary
from ..utils.json_parser import parse_json_response

logger = logging.getLogger(__name__)


def _is_capability(decomposition: Any) -> bool:
    """True when ``decomposition`` selects the capability doctrine."""
    return decomposition in (
        PlanDecomposition.CAPABILITY,
        PlanDecomposition.CAPABILITY.value,
    )


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

The project charter (project-level conventions) and the code-index (a zoomable
structural orientation map) are injected below — plan against the task, the
charter, and the code-index. Before reading source, consult the code-index map
to locate the relevant modules / symbols; open a collapsed directory one more
level with `luo code-index index <path>` and pull a file's function/method
detail on demand with `luo code-index show <path>`. To find items by keyword or
regex, use `luo code-index search <pattern>` instead of `grep 'pattern'
tianluo/code-index.md` — each hit carries the item's full locating path (a symbol
renders as `relpath::local_id`, which a raw grep line cannot show); its syntax
matches grep (regex `pattern` by default, `-i`/`-F`/`-m`).

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

# --- Capability doctrine (default): coarse groups, no per-task listing ---
#
# WHY a separate section rather than extra rules bolted onto TASKS_SECTION:
# the two doctrines disagree on the *unit* being produced. TASKS_SECTION asks
# for cohesive task lists sized to a focused human session; this one asks for
# the largest unit one autonomous implement call can safely carry, with the
# in-group breakdown deliberately left to the runner. Mixing both sets of
# sizing advice into one section would leave the model to guess which unit it
# is being asked for.
CAPABILITY_TASKS_SECTION = """## {part_label}: Task Groups (capability units)
Split the implementation into coarse task groups. The ONLY criterion for
splitting is: **can a single autonomous implement call safely carry this?**
Each group becomes exactly one such call.

### Sizing Criteria
1. One capability, and one call can complete it → **one group**. The group's
   content is simply "implement that capability".
2. One capability that a single call cannot carry → split it into **two or
   more groups**, each of which one call can carry.
3. Two (or more) naturally distinct capabilities that one call could still
   complete together → **still one group**. Distinctness alone is not a
   reason to split.
4. On the edge between "can" and "cannot" → **one capability per group**. The
   more capabilities a group aggregates, the LOWER the threshold at which you
   split it: aggregation makes you more conservative, never less.

### Grouping Principles
- Groups are cut along **deliverable capability units** only.
- Use `depends_on` to express real inter-group dependencies. Groups with no
  dependency between them are executed **in parallel, in isolated worktrees**,
  so a missing dependency is a real correctness hazard and a spurious one
  silently serializes work that could have run concurrently.
- Each group is implemented in a **separate LLM call with isolated context**;
  a group must therefore be self-contained enough to execute without knowing
  another group's implementation details.
- Do NOT enumerate individual tasks inside a group. The implement runner has
  its own planning / sub-agent system that decomposes a group at execution
  time, against the real code — a task list written here would only duplicate
  that at lower fidelity and go stale.

{granularity_directive}"""

CAPABILITY_GRANULARITY_AUTO = """### Group Count: auto
Estimate how many autonomous implement calls this task actually needs, and
emit exactly that many groups. Do not inflate the count for the sake of
structure, and do not compress into one group work that one call cannot carry.
"""

CAPABILITY_GRANULARITY_SINGLE = """### Group Count: single (forced)
Emit **exactly one** task group covering the entire task, whatever its size.
The configuration has forced single-group execution: the whole requirement is
delivered by one autonomous implement call, so do not split under any
circumstances.
"""

CAPABILITY_GRANULARITY_CONSERVATIVE = """### Group Count: conservative
Lower the splitting threshold: whenever there is **any** doubt that a single
call can carry the work, split it. Prefer one capability per group, and err
toward MORE groups than the default sizing would produce. A group that turns
out to be smaller than one call could have handled costs little; a group that
overflows the call it was sized for costs a failed implementation.
"""

ARTIFACT_SPLIT_GUARDRAIL = """## Guardrail: Group by Capability, Never by Artifact Type
Task groups MUST NOT be cut along artifact types or code layers. The following
groups are forbidden and must not appear in your output:

- a separate **test** group ("write the tests", "add test coverage")
- a separate **docs** group ("update the documentation")
- a separate **config** group ("update the configuration / schema files")
- any group whose definition is a file set, a module boundary, or a code layer
  (data layer / service layer / UI layer)

Testing and verification are part of what **each group itself delivers**: a
group is complete only when its own capability is implemented AND covered by
its own tests. Groups are cut along deliverable capability units only — never
along files, modules, or code layers.

A group whose *capability* happens to concern the test system, the docs system
or the configuration system (e.g. "fix the flaky retry in the test runner") is
legitimate. What is forbidden is carving one capability's tests, docs or
config out into a group of their own.
"""

# Capability-doctrine output schema: groups carry only the scheduling fields.
CAPABILITY_JSON_SCHEMA = """\
Respond in JSON format:
```json
{
    "plan": {
        "proposal": {
            "summary": "...",
            "motivation": "...",
            "files_to_modify": ["file1.py", "file2.py"],
            "files_to_create": ["new_file.py"],
            "risks": ["risk1", "risk2"]
        },
        "design": {
            "overview": "...",
            "architecture_decisions": [
                {"decision": "...", "rationale": "...", "alternatives_considered": "..."}
            ],
            "components": [
                {"name": "...", "responsibilities": "...", "interfaces": "..."}
            ],
            "data_flow": "...",
            "testing_strategy": "..."
        }
    },
    "task_groups": [
        {
            "group_id": "G1",
            "name": "Capability this group delivers",
            "description": "What this group delivers, in enough detail that one autonomous implement call can execute it end to end",
            "group_order": 1,
            "depends_on": []
        }
    ],
    "total_complexity": "small|medium|large",
    "estimated_effort": "brief estimate"
}
```

Important:
- `group_id` should be unique (G1, G2, G3...)
- `group_order` determines execution sequence
- `depends_on` lists group_ids that must complete before this group;
  groups with no dependency between them run in parallel
- Each group will be implemented in a **separate LLM call with isolated context**
- Do NOT emit a `tasks` array. Groups carry only the five fields above; the
  in-group breakdown is produced by the implement runner's own planning /
  sub-agent system at execution time.
- `plan.proposal` and `plan.design` are still required in full: they are what
  the human gate reviews and what later fix iterations read back as design
  context.
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

# Shallow depth output schema for small (and any other shallow-depth type)
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

# --- Root-cause investigation report (independent context source) ---
#
# INVARIANT: the report is rendered as its OWN prompt section and is never
# folded into the task description. It is investigated context, not user
# intent — merging it into the intent chain would put speculative text into
# self_check's verbatim-quote source pool. The wording below tells the model
# the same thing, so it cannot cite the report as a user requirement either.
ROOT_CAUSE_SECTION = """## Root-Cause Investigation Report
{exhausted_note}A dedicated investigation step ran before this one and reported the following. \
This is investigated context, NOT part of the user's request: do not treat it as a requirement \
and do not quote it as evidence of what the user asked for. Use it to aim the work.

**Root cause:** {root_cause}

**Evidence:**
{evidence}

**Files involved:** {files_involved}

**Suggested fix direction:** {suggested_fix_direction}

**Confidence:** {confidence}
"""

ROOT_CAUSE_EXHAUSTED_NOTE = (
    "> The investigation loop used up its round budget WITHOUT reaching a "
    "conclusive cause. What follows is the best current hypothesis at LOW "
    "confidence — verify it before you build on it, and be prepared for it to "
    "be wrong.\n\n"
)


def render_root_cause_section(report: Any, exhausted: bool = False) -> str:
    """Render an investigation report as a standalone prompt section.

    Args:
        report: The ``root_cause_report`` dict from the INVESTIGATE step, or
            anything falsy / non-dict when no investigation ran.
        exhausted: True when the round budget ran out without a conclusive
            verdict, which prefixes an explicit low-confidence warning.

    Returns:
        ``""`` when there is no usable report — so a flow that never
        investigated produces a prompt byte-identical to one built before this
        section existed — otherwise the section padded with a leading and
        trailing newline, ready to drop into a ``{root_cause_section}`` slot.
    """
    if not isinstance(report, dict):
        return ""
    root_cause = str(report.get("root_cause") or "").strip()
    if not root_cause:
        # No stated cause means nothing worth a section; an empty shell would
        # only invite the model to invent content for it.
        return ""

    evidence = report.get("evidence")
    if isinstance(evidence, list) and evidence:
        evidence_text = "\n".join(f"- {e}" for e in evidence)
    else:
        evidence_text = "- (none recorded)"

    files = report.get("files_involved")
    if isinstance(files, list) and files:
        files_text = ", ".join(str(f) for f in files)
    else:
        files_text = "(not specified)"

    body = ROOT_CAUSE_SECTION.format(
        exhausted_note=ROOT_CAUSE_EXHAUSTED_NOTE if exhausted else "",
        root_cause=root_cause,
        evidence=evidence_text,
        files_involved=files_text,
        suggested_fix_direction=(
            str(report.get("suggested_fix_direction") or "").strip()
            or "(not specified)"
        ),
        confidence=str(report.get("confidence") or "unknown"),
    )
    return "\n" + body


def _get_prompt_depth(task_type: str) -> str:
    """Determine prompt depth based on task_type.

    Returns: 'full', 'medium', or 'shallow'
    """
    if task_type in ("feature", "discovery"):
        return "full"
    elif task_type in ("bugfix", "fix"):
        return "medium"
    else:  # small, review, etc.
        return "shallow"


# Phase-1 hints for the two-phase JSON extraction. The capability hint must
# NOT show a `tasks` array: the hint is what the extractor echoes back as the
# expected shape, so a stale example would reintroduce the per-task listing the
# doctrine just removed.
GRANULAR_JSON_SCHEMA_HINT = (
    '{"plan": {"proposal": {"summary": "..."}, "design": {"overview": "..."}}, '
    '"task_groups": [{"group_id": "G1", "name": "...", '
    '"tasks": [{"id": 1, "description": "..."}]}], "total_complexity": "..."}'
)

CAPABILITY_JSON_SCHEMA_HINT = (
    '{"plan": {"proposal": {"summary": "..."}, "design": {"overview": "..."}}, '
    '"task_groups": [{"group_id": "G1", "name": "...", "description": "...", '
    '"group_order": 1, "depends_on": []}], "total_complexity": "..."}'
)


def _resolve_plan_mode(step: Step, flow: FlowInstance):
    """Read back the plan mode this flow persisted at creation.

    Step inputs win over flow context only because the state machine copies
    the same context values into them; an explicitly-set input is still the
    fresher view of the same single decision. Falls through to
    ``PlanModeResolver.view`` so a flow created before this model existed is
    projected from its recorded legacy strategy instead of being silently
    upgraded to the current default.
    """
    lookup = dict(flow.state.context)
    for key in (PLAN_DECOMPOSITION_KEY, PLAN_GRANULARITY_KEY):
        value = step.inputs.get(key)
        if value:
            lookup[key] = value
    return PlanModeResolver.view(lookup)


_GRANULARITY_DIRECTIVES = {
    PlanGranularity.AUTO: CAPABILITY_GRANULARITY_AUTO,
    PlanGranularity.SINGLE: CAPABILITY_GRANULARITY_SINGLE,
    PlanGranularity.CONSERVATIVE: CAPABILITY_GRANULARITY_CONSERVATIVE,
}


def _granularity_directive(granularity: Any) -> str:
    """Return exactly one granularity directive for the capability prompt."""
    try:
        key = PlanGranularity(granularity)
    except (TypeError, ValueError):
        key = PlanGranularity.AUTO
    return _GRANULARITY_DIRECTIVES[key]


def _build_prompt(
    task_description: str,
    task_type: str,
    scope: str,
    project_summary: str,
    revision_section: str,
    depth: str,
    root_cause_section: str = "",
    decomposition: Any = PlanDecomposition.GRANULAR,
    granularity: Any = PlanGranularity.AUTO,
) -> str:
    """Build the plan prompt adapted by doctrine and depth.

    ``decomposition`` selects the doctrine: the capability branch asks for
    coarse groups sized to one autonomous implement call, the granular branch
    reproduces the legacy per-task listing byte for byte. ``granularity`` only
    applies to the capability branch.
    """
    parts = []

    # Header is always included
    parts.append(PLAN_PROMPT_HEADER.format(
        task_description=task_description,
        task_type=task_type,
        scope=scope,
        project_summary=project_summary,
        revision_section=revision_section,
    ))

    # Between the header and the output schema, and only when an investigation
    # actually produced a report — appending an empty part would shift the
    # joined prompt by one newline for every non-investigated flow.
    if root_cause_section:
        parts.append(root_cause_section.strip("\n"))

    if _is_capability(decomposition):
        # Proposal / design keep their depth-adapted shape: the human gate and
        # the fix loop's {design_section} read them regardless of doctrine.
        if depth == "full":
            parts.append(PROPOSAL_SECTION)
            parts.append(DESIGN_SECTION)
            part_label = "Part 3"
        elif depth == "medium":
            parts.append(PROPOSAL_SECTION)
            parts.append(DESIGN_SECTION_BUGFIX)
            part_label = "Part 3"
        else:  # shallow
            part_label = "Instructions"
        parts.append(CAPABILITY_TASKS_SECTION.format(
            part_label=part_label,
            granularity_directive=_granularity_directive(granularity),
        ))
        parts.append(ARTIFACT_SPLIT_GUARDRAIL)
        parts.append(VERSION_FILE_GUARDRAIL)
        parts.append(CAPABILITY_JSON_SCHEMA)
        return "\n".join(parts)

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

    # The doctrine/granularity this flow entered. Read back rather than
    # re-decided: a resumed or revised plan must be produced under the same
    # model the flow already committed to, so its groups stay comparable to
    # whatever an earlier round emitted.
    mode = _resolve_plan_mode(step, flow)

    # Build prompt
    prompt = _build_prompt(
        task_description=task_description,
        task_type=task_type,
        scope=scope,
        project_summary=project_summary,
        revision_section=revision_section,
        depth=depth,
        root_cause_section=render_root_cause_section(
            step.inputs.get("root_cause_report"),
            exhausted=bool(step.inputs.get("investigation_exhausted")),
        ),
        decomposition=mode.decomposition,
        granularity=mode.granularity,
    )
    capability = _is_capability(mode.decomposition)

    # Append language instruction if configured
    from ..context_builder import (
        get_step_language_instruction,
        get_issue_discovery_injection,
        get_charter_injection,
        get_code_index_injection,
        get_runtime_environment_injection,
    )
    project_root = resolve_flow_project_root(flow)
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
    # (function-level detail pulled on demand via `luo code-index show`).
    prompt += get_charter_injection(project_root)
    # No code-index refresh here: analyze already refreshed the read-side map
    # before any code changed (see analyze.py / commit.py two-point rationale).
    prompt += get_code_index_injection(project_root)

    # Append runtime environment injection if applicable
    runtime_env = get_runtime_environment_injection("plan", project_root)
    if runtime_env:
        prompt += runtime_env

    logger.info(
        "Generating plan (depth=%s, decomposition=%s, granularity=%s) for: %s...",
        depth, mode.decomposition.value, mode.granularity.value,
        task_description[:60],
    )

    try:
        retry_count = step.inputs.get("retry_count", 0)
        caller = LLMCaller(project_root, flow_id=flow.flow_id, step_id=step.step_id, step_type=step.step_type.value, external_attempt=retry_count, fix_iteration=step.inputs.get("fix_iteration", 0))
        response = caller.call(
            prompt=prompt,
            json_mode="two_phase",
            json_schema_hint=(
                CAPABILITY_JSON_SCHEMA_HINT if capability else GRANULAR_JSON_SCHEMA_HINT
            ),
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
        step.outputs[PLAN_DECOMPOSITION_KEY] = mode.decomposition.value
        step.outputs[PLAN_GRANULARITY_KEY] = mode.granularity.value
        # The group count is the execution shape: IMPLEMENT reads it to choose
        # holistic vs. grouped, and the control plane projects it. Recorded as
        # its own output so a step whose groups get externalized still carries
        # the shape it produced.
        step.outputs["plan_group_count"] = len(task_groups)

        if (
            capability
            and mode.granularity is PlanGranularity.SINGLE
            and len(task_groups) > 1
        ):
            # Not silently collapsed: merging groups here would discard the
            # dependency structure the model just reasoned about, and dropping
            # any of them would lose planned work. The forced-single request is
            # a prompt-level instruction, so a violation is worth surfacing.
            logger.warning(
                "plan_granularity=single requested but PLAN returned %d groups; "
                "executing all of them",
                len(task_groups),
            )

        total_tasks = sum(
            len(g.get("tasks", [])) for g in task_groups if isinstance(g, dict)
        )
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
