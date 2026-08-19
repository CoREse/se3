"""Plan step handler.

Unified planning step that replaces the separate propose, design, and plan_tasks steps.
Produces the task-group scheduling data for a flow in a single LLM call: the
groups themselves plus their complexity/effort estimate. PLAN no longer emits a
proposal or a design document — project conventions come from the charter and
the code-index, and how a group decomposes internally is the implement call's
own planning / sub-agent job.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..display import get_console
from ..formatters import TaskFormatter
from ..llm_caller import LLMCaller
from ..models import FlowInstance, Step, StepStatus
from ..plan_decomposition import (
    PLAN_DECOMPOSITION_KEY,
    PLAN_GRANULARITY_KEY,
    PlanDecomposition,
    PlanGranularity,
    effective_mode,
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


# --- Prompt sections (composed based on the flow's decomposition doctrine) ---

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
# every template field (task_description, project_context, scope) is either
# upstream LLM output or framework-derived. The web console
# therefore falls back to rendering the whole post-BEGIN tail inside the
# collapsed system-prompt chip.
PLAN_PROMPT_HEADER = inject_boundary(PLAN_PROMPT_HEADER, "## Project Context\n")

TASKS_SECTION = """## Instructions: Task Groups
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
# one coherent *task* per group, split further only where a single autonomous
# implement call cannot carry it, with the in-group breakdown deliberately left
# to the runner. Mixing both sets of sizing advice into one section would leave
# the model to guess which unit it is being asked for.
CAPABILITY_TASKS_SECTION = """## Instructions: Task Groups (task units)
Split the implementation into coarse task groups. The unit of grouping is a
**task** — one coherent piece of work the user would regard as a single thing.
The ONLY criterion for splitting a task any further is: **can a single
autonomous implement call safely carry this?** Each group becomes exactly one
such call.

### Sizing Criteria
1. One task, and one call can complete it → **one group**. The group's content
   is simply "carry out that task".
2. One task that a single call cannot carry → split it into **two or more
   groups**, each of which one call can carry.
3. Two (or more) mutually unrelated, independent tasks → **one group each**, so
   that they can be executed in parallel in isolated worktrees. Aspects of one
   coherent task are not independent tasks and stay together in its group.
4. Default to aggregation — **one task, one group**. Split a task only at the
   capability edge: you positively judge that a single autonomous implement
   call cannot complete it, or that forcing it into one call would
   substantially degrade the quality of the execution. When you do split, state
   that capability-edge reason in the group's description. Never pre-cut a
   single task along implementation phases, implementation paths, or artifact
   types — how a task is broken down internally is decided inside the implement
   call, not here.

### Grouping Principles
- Groups are cut along **task units** only: one coherent task per group, and
  one group per mutually independent task.
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
  that at lower fidelity and go stale. Decomposing a task internally is the
  implement call's job, not PLAN's.

{granularity_directive}"""

CAPABILITY_GRANULARITY_AUTO = """### Group Count: auto
The group count is the number of mutually independent tasks in this
requirement — normally one group per task. Add groups beyond that count only
where a single task reaches the capability edge and genuinely needs more than
one autonomous implement call. Do not inflate the count for the sake of
structure, and do not compress unrelated tasks into one group.
"""

CAPABILITY_GRANULARITY_SINGLE = """### Group Count: single (forced)
Emit **exactly one** task group covering the entire task, whatever its size.
The configuration has forced single-group execution: the whole requirement is
delivered by one autonomous implement call, so do not split under any
circumstances.
"""

CAPABILITY_GRANULARITY_CONSERVATIVE = """### Group Count: conservative
Lower the splitting threshold: whenever there is **any** doubt that a single
call can carry a task, split it. Prefer one group per sub-task even where the
default sizing would have kept the task whole, and err toward MORE groups than
the default sizing would produce. A group that turns out to be smaller than
one call could have handled costs little; a group that overflows the call it
was sized for costs a failed implementation.
"""

ARTIFACT_SPLIT_GUARDRAIL = """## Guardrail: Group by Task, Never by Artifact Type
Task groups MUST NOT be cut along artifact types or code layers. The following
groups are forbidden and must not appear in your output:

- a separate **test** group ("write the tests", "add test coverage")
- a separate **docs** group ("update the documentation")
- a separate **config** group ("update the configuration / schema files")
- any group whose definition is a file set, a module boundary, or a code layer
  (data layer / service layer / UI layer)

Testing and verification are part of what **each group itself delivers**: a
group is complete only when its own task is implemented AND covered by
its own tests. Groups are cut along task units only — never
along files, modules, or code layers.

A group whose *task* happens to concern the test system, the docs system or
the configuration system (e.g. "fix the flaky retry in the test runner") is
legitimate. What is forbidden is carving one task's tests, docs or config out
into a group of their own.
"""

# Capability-doctrine output schema: groups carry only the scheduling fields.
CAPABILITY_JSON_SCHEMA = """\
Respond in JSON format:
```json
{
    "task_groups": [
        {
            "group_id": "G1",
            "name": "Task this group delivers",
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
- `group_id` is REQUIRED on every group and must be unique (G1, G2, G3...); it
  names the group's branch/worktree and is what `depends_on` resolves against
- `description` is REQUIRED and non-empty on every group: with no `tasks` array
  it is the only statement of what the group delivers
- `group_order` determines execution sequence and must be a plain number
  (`1`, not `"1"`); the groups are sorted on it before execution, so a quoted
  order beside an unquoted one is rejected and the plan has to be produced again
- `depends_on` lists group_ids that must complete before this group;
  groups with no dependency between them run in parallel
- `depends_on` must be acyclic: a group may not depend on itself, and two
  groups may not depend on each other (directly or through a chain). A cycle
  is rejected and the whole plan has to be produced again
- Each group will be implemented in a **separate LLM call with isolated context**
- Do NOT emit a `tasks` array. Groups carry only the five fields above; the
  in-group breakdown is produced by the implement runner's own planning /
  sub-agent system at execution time.
"""

# Granular (legacy) doctrine output schema. WHY one schema rather than the
# former full/medium/shallow trio: the three differed only in the proposal /
# design block they asked for, the placeholder wording of their examples, and
# the trailing `Important:` notes that only the full variant carried. With
# proposal/design gone the remaining difference is pure information the other
# two lacked, so the merge converges on the full variant rather than on their
# intersection — taking the intersection would silently drop the grouping
# conventions from bugfix and small flows.
GRANULAR_JSON_SCHEMA = """\
Respond in JSON format:
```json
{{
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
the plan and explain in the group description that the version bump will be
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


# Phase-1 hints for the two-phase JSON extraction. The capability hint must
# NOT show a `tasks` array: the hint is what the extractor echoes back as the
# expected shape, so a stale example would reintroduce the per-task listing the
# doctrine just removed.
GRANULAR_JSON_SCHEMA_HINT = (
    '{"task_groups": [{"group_id": "G1", "name": "...", '
    '"tasks": [{"id": 1, "description": "..."}]}], "total_complexity": "..."}'
)

CAPABILITY_JSON_SCHEMA_HINT = (
    '{"task_groups": [{"group_id": "G1", "name": "...", "description": "...", '
    '"group_order": 1, "depends_on": []}], "total_complexity": "..."}'
)


def _resolve_plan_mode(step: Step, flow: FlowInstance):
    """Read back the plan mode this flow persisted at creation.

    Delegates to the shared ``plan_decomposition.effective_mode`` projection —
    the same one IMPLEMENT and the plan CONFIRM reviewer read — so the doctrine
    this step plans under cannot differ from the one the rest of the flow acts
    on. A flow created before this model existed is projected from its recorded
    legacy strategy rather than silently upgraded to the current default.
    """
    return effective_mode(step.inputs, flow.state.context)


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


def _capability_cycle(dependencies: Dict[str, List[str]]) -> List[str]:
    """Return one ``depends_on`` cycle as an id path, or ``[]`` when acyclic.

    Edges pointing outside *dependencies* are skipped rather than walked purely
    so the traversal cannot ``KeyError``; they are not a tolerated shape here.
    ``_capability_groups_error`` rejects dangling edges before calling this, so
    on the PLAN path no such edge survives to reach the walk.
    """
    grey: Dict[str, bool] = {}
    done: Dict[str, bool] = {}
    path: List[str] = []

    def visit(node: str) -> bool:
        grey[node] = True
        path.append(node)
        for dep in dependencies[node]:
            if dep not in dependencies:
                continue
            if grey.get(dep):
                path.append(dep)
                return True
            if not done.get(dep) and visit(dep):
                return True
        path.pop()
        grey[node] = False
        done[node] = True
        return False

    for gid in dependencies:
        if not done.get(gid) and visit(gid):
            # Trim the walk down to the cycle proper: everything before the
            # first occurrence of the repeated id is just the way in.
            return path[path.index(path[-1]):]
    return []


def _capability_groups_error(task_groups: Any) -> str:
    """Return why ``task_groups`` is unusable as a capability plan, else "".

    Scheduling reads six things off the enumeration, and each is required
    rather than merely preferred:

    * the entry being a mapping at all — a bare string survives the container
      check but is dropped by ``_extract_sorted_groups``, so the recorded group
      count and the shape IMPLEMENT actually runs disagree;
    * ``group_id`` specifically, not any identity-ish field. ``transitive_reduce``
      indexes ``g["group_id"]`` unconditionally on both the linear-chain preview
      and the real DAG run; the preview swallows the resulting ``KeyError`` and
      leaves DAG selected, so a group identified only by ``name`` aborts
      IMPLEMENT with a raw traceback instead of failing here as a retryable
      plan-shape error;
    * that ``group_id`` being *unique*, because it is the scheduling key for
      everything downstream — branch and worktree names, per-group step ids,
      and the group-keyed result/agent maps. A repeated id fails in whichever
      of two ways the topology picks, and neither is recoverable: on the DAG
      path ``DAGScheduler._build_dag`` raises a raw ``ValueError``, and
      re-running IMPLEMENT re-reads the same persisted groups and dies
      identically; on the sequential path the repeat collapses into one node,
      so both groups share a ``group_step_id`` and a branch and one silently
      overwrites the other. Only PLAN can recover, by retrying into a new plan;
    * ``description``, because the capability doctrine emits no per-task list —
      the description is the group's entire work statement. A group without one
      renders as bare scheduling metadata in its isolated implement prompt, and
      every such call falls back to the overall task description, so each
      worktree implements the whole task and the leaf merges collide.
    * ``group_order`` being a number when it is present at all.
      ``_extract_sorted_groups`` sorts on it at the very top of the grouped
      IMPLEMENT dispatch, before any path branches, and the sort compares the
      raw values against each other and against the ``0`` default of a group
      that omits the field. One group quoting its order (``"group_order": "1"``)
      while another does not — a routine JSON-typing slip — makes that
      comparison raise a raw ``TypeError``; an explicit ``null`` does the same
      even when every group carries one. Both abort IMPLEMENT before the
      holistic/DAG decision is reached, and a Retry re-reads the same persisted
      values and dies identically, so only a new plan recovers — the same
      argument that puts the ``depends_on`` shape check here;
    * ``depends_on`` being an acyclic list of id strings. Under the capability
      doctrine every multi-group plan reaches ``DAGScheduler`` (the LOC gate no
      longer diverts anything to the merged single call), and the scheduler
      indexes and iterates the edges unconditionally: a non-list value raises a
      raw ``TypeError`` on iteration, a non-string element raises on the
      ``dep not in all_ids`` membership test, and a cycle raises
      ``ValueError("Cycle detected in DAG")``. The linear-chain preview that
      runs first swallows its own exception and leaves DAG selected, so all
      three abort IMPLEMENT with a traceback, and a Retry re-reads the same
      persisted edges and dies identically. Only PLAN can recover, by
      re-planning — exactly the argument that put the ``group_id`` uniqueness
      check here. An edge naming no declared group is rejected for the mirror
      reason: the scheduler does *not* fail on it, it drops it as already
      satisfied, so the declared ordering is lost silently instead of loudly.

    Validating per entry keeps those divergences impossible rather than
    deferring them to a silent collapse or an interpreter error.

    On success the normalized ids and edges are written back into *task_groups*
    in place, because validating a value the scheduler never sees would leave
    exactly the divergence this guard exists to close.
    """
    if not isinstance(task_groups, list):
        return (
            f"task_groups is {type(task_groups).__name__}, not a list; a "
            "capability plan must carry a list of group objects"
        )
    if not task_groups:
        return "task_groups is empty; a capability plan must carry at least one group"
    # Keyed by the stripped id: whitespace variants of one id are the same
    # identity to the model but two distinct scheduling keys downstream, which
    # is the same collision seen from the other side.
    seen_ids: Dict[str, int] = {}
    dependencies: Dict[str, List[str]] = {}
    # (group, normalized id, normalized edges) per entry, applied only once the
    # whole enumeration has passed: a rejected plan is discarded and re-planned,
    # so rewriting half of it would only obscure what the model actually emitted.
    normalized: List[Tuple[Dict[str, Any], str, List[str]]] = []
    for index, group in enumerate(task_groups):
        if not isinstance(group, dict):
            return (
                f"task_groups[{index}] is {type(group).__name__}, not a group "
                "object; every group must be an object carrying group_id and "
                "description"
            )
        group_id = group.get("group_id")
        if not isinstance(group_id, str) or not group_id.strip():
            return (
                f"task_groups[{index}] carries no group_id; a group without "
                "one cannot name its branch/worktree, be resolved as a "
                "depends_on edge, or survive dependency reduction"
            )
        key = group_id.strip()
        if key in seen_ids:
            return (
                f"task_groups[{index}] reuses group_id {key!r} already used by "
                f"task_groups[{seen_ids[key]}]; group_id is the scheduling key "
                "for branches, worktrees, step ids and depends_on edges, so "
                "duplicates either abort the DAG run or collapse two groups "
                "onto one branch"
            )
        seen_ids[key] = index
        description = group.get("description")
        if not isinstance(description, str) or not description.strip():
            return (
                f"task_groups[{index}] ({group_id.strip()}) carries no "
                "description; a capability group emits no task list, so the "
                "description is the only statement of what it delivers"
            )
        # An absent group_order is orderable — every such group takes the same
        # ``0`` default and declaration order survives — so only a present value
        # is checked, and ``bool`` is excluded because True/False order nothing
        # even though Python would happily compare them as 1/0.
        if "group_order" in group:
            group_order = group["group_order"]
            if isinstance(group_order, bool) or not isinstance(group_order, (int, float)):
                return (
                    f"task_groups[{index}] ({key}) carries group_order "
                    f"{group_order!r} of type {type(group_order).__name__}, not "
                    "a number; the groups are sorted on this value before "
                    "IMPLEMENT picks a path, so a value that cannot be compared "
                    "against the other groups' orders aborts the run instead of "
                    "sequencing it"
                )

        depends_on = group.get("depends_on")
        if depends_on is None:
            depends_on = []
        if not isinstance(depends_on, list):
            return (
                f"task_groups[{index}] ({key}) carries depends_on of type "
                f"{type(depends_on).__name__}, not a list of group_id strings; "
                "the scheduler iterates the edges directly, so any other shape "
                "aborts IMPLEMENT instead of ordering the run"
            )
        deps: List[str] = []
        for dep in depends_on:
            if not isinstance(dep, str) or not dep.strip():
                return (
                    f"task_groups[{index}] ({key}) declares a depends_on entry "
                    f"{dep!r} that is not a group_id string; edges are resolved "
                    "by id, so a non-id entry can neither be matched nor "
                    "ordered"
                )
            # Stripped for the same reason ids are: a padded edge and its target
            # are one identity to the model but two keys to the scheduler.
            deps.append(dep.strip())
        dependencies[key] = deps
        normalized.append((group, key, deps))

    # WHY: an edge naming no declared group is rejected here rather than left to
    # the scheduler. At PLAN time the enumeration being validated is the
    # complete, freshly generated group set — nothing has been completed or
    # pre-merged — so no edge can legitimately point outside it; one that does is
    # a mistyped reference. DAGScheduler's tolerance for dangling edges exists
    # for a different input: the *reduced* to-run set of a DAG recovery run,
    # pruned of already-completed groups by ``_prune_completed_groups``, which
    # never flows through this check. Left to it, the edge is dropped as "already
    # satisfied" with only a log warning, the dependent group gets in_degree 0
    # and runs concurrently in a worktree that lacks its prerequisite's code —
    # the declared ordering silently lost and the leaf merges colliding.
    for key, deps in dependencies.items():
        for dep in deps:
            if dep not in dependencies:
                return (
                    f"task_groups[{seen_ids[key]}] ({key}) declares a "
                    f"depends_on entry {dep!r} that matches no group_id in the "
                    "plan; every group of a fresh plan is present, so the edge "
                    "names nothing and the scheduler would drop it as already "
                    "satisfied and run the group unordered"
                )

    cycle = _capability_cycle(dependencies)
    if cycle:
        return (
            "task_groups declare a dependency cycle "
            f"({' -> '.join(cycle)}); no group in the cycle can ever start, so "
            "the scheduler rejects the whole plan and re-running IMPLEMENT "
            "re-reads the same edges — only a new plan breaks it"
        )

    # WHY the stripped values are written back rather than merely used to
    # validate: the checks above key on stripped ids, but everything downstream
    # keys on whatever the group dicts persist. ``DAGScheduler._build_dag``
    # builds ``all_ids`` from the raw ``group_id`` values, so an edge that
    # resolved here only because both sides were stripped ("G1 " against "G1")
    # would miss ``all_ids`` there and be dropped as already satisfied — the
    # dependent group gets in_degree 0 and runs concurrently in a worktree
    # lacking its prerequisite, which is the exact silent ordering loss the
    # dangling-edge check above exists to reject. Padding is a formatting
    # artifact of generated JSON, not a mistyped reference, so it is repaired
    # rather than bounced back to a re-plan; making validation and scheduling
    # read one identity is what makes that repair sound.
    for group, key, deps in normalized:
        if group.get("group_id") != key:
            group["group_id"] = key
        if isinstance(group.get("depends_on"), list) and group["depends_on"] != deps:
            group["depends_on"] = deps

    return ""


def _build_prompt(
    task_description: str,
    task_type: str,
    scope: str,
    project_summary: str,
    revision_section: str,
    root_cause_section: str = "",
    decomposition: Any = PlanDecomposition.GRANULAR,
    granularity: Any = PlanGranularity.AUTO,
) -> str:
    """Build the plan prompt for the flow's decomposition doctrine.

    ``decomposition`` selects the doctrine: the capability branch asks for
    coarse groups sized to one autonomous implement call, the granular branch
    reproduces the legacy per-task listing. ``granularity`` only applies to the
    capability branch.

    WHY there is no depth parameter any more: depth (full/medium/shallow)
    existed only to pick which proposal/design sections to ask for and which of
    the three otherwise-identical schemas to append. PLAN emits neither
    artifact now, so every task_type gets the same scheduling-data contract.
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
        parts.append(CAPABILITY_TASKS_SECTION.format(
            granularity_directive=_granularity_directive(granularity),
        ))
        parts.append(ARTIFACT_SPLIT_GUARDRAIL)
        parts.append(VERSION_FILE_GUARDRAIL)
        parts.append(CAPABILITY_JSON_SCHEMA)
        return "\n".join(parts)

    parts.append(TASKS_SECTION)
    parts.append(VERSION_FILE_GUARDRAIL)
    parts.append(GRANULAR_JSON_SCHEMA)

    return "\n".join(parts)


def plan_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the unified plan step.

    Generates the flow's task-group scheduling data in a single LLM call.

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
        "Generating plan (decomposition=%s, granularity=%s) for: %s...",
        mode.decomposition.value, mode.granularity.value,
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
        task_groups = result.get("task_groups", [])

        if capability:
            # WHY the whole enumeration is validated rather than just its
            # container: the smallest legal capability plan is *one* group
            # ("deliver this in one autonomous call"), so an empty enumeration,
            # a differently-shaped value (a dict keyed by group id, a string),
            # and a list of bare strings are all failed plans rather than
            # coarser ones. Storing any of them would let PLAN complete and
            # leave IMPLEMENT to pick a shape from something it cannot read one
            # off. The counts diverge in both directions: a dict of one group
            # projects as a count of 1 to the WebUI while IMPLEMENT sees no
            # shape at all, and ``["deliver export", "deliver import"]``
            # projects as 2 while ``_extract_sorted_groups`` drops every
            # non-dict entry and collapses it into a single legacy call — the
            # DAG the two declared groups asked for never runs. Failing here
            # routes it back through the step's own retry path, the only
            # outcome that produces a plan the downstream contract can be read
            # off at all. ``required_keys`` cannot express this: the key *is*
            # present.
            invalid_reason = _capability_groups_error(task_groups)
            if invalid_reason:
                step.error_message = (
                    f"PLAN returned unusable task_groups: {invalid_reason}"
                )
                return StepStatus.FAILED

        # WHY no ``plan`` wrapper output: it only ever carried the proposal /
        # design pair, so it would now persist as an empty dict that reads as a
        # plan the step failed to produce. Flows recorded before this change
        # keep theirs, and `_render_plan` / the web console still render them —
        # the removal is write-side only.
        step.outputs["task_groups"] = task_groups
        step.outputs["total_complexity"] = result.get("total_complexity", "medium")
        step.outputs["estimated_effort"] = result.get("estimated_effort", "")
        step.outputs[PLAN_DECOMPOSITION_KEY] = mode.decomposition.value
        step.outputs[PLAN_GRANULARITY_KEY] = mode.granularity.value
        # The group count is the execution shape wherever the granularity left
        # the count to PLAN — a persisted ``single`` overrides the count in
        # ``holistic_execution_mode``. IMPLEMENT reads it to choose holistic
        # vs. grouped, and the control plane projects it. Recorded as its own
        # output so a step whose groups get externalized still carries the
        # shape it produced.
        step.outputs["plan_group_count"] = len(task_groups)

        if (
            capability
            and mode.granularity is PlanGranularity.SINGLE
            and len(task_groups) > 1
        ):
            # The groups are kept on disk rather than merged away: they are the
            # dependency structure the model just reasoned about, and IMPLEMENT
            # passes them through as an outline. Only the execution *shape* is
            # forced — ``holistic_execution_mode`` reads the persisted
            # granularity, so a plan that ignores the forced-single directive
            # still runs as one autonomous call rather than a DAG. Surfaced
            # because a plan that needed several groups is a signal the forced
            # single call may be oversized.
            logger.warning(
                "plan_granularity=single requested but PLAN returned %d groups; "
                "they will be delivered by one autonomous implement call",
                len(task_groups),
            )

        total_tasks = sum(
            len(g.get("tasks", [])) for g in task_groups if isinstance(g, dict)
        )
        logger.info(
            "Plan generated: %d groups, %d tasks", len(task_groups), total_tasks,
        )

        # Display formatted output
        try:
            _display_plan(task_groups)
        except Exception as e:
            logger.warning(f"Failed to format plan output: {e}")

        return StepStatus.COMPLETED

    except Exception as e:
        logger.exception("Plan step failed")
        step.error_message = f"Plan generation failed: {str(e)}"
        return StepStatus.FAILED


def _display_plan(task_groups: list) -> None:
    """Display the plan output with Rich formatting."""
    console = get_console()

    if task_groups:
        formatter = TaskFormatter(console=console)
        tree_panel = formatter.format_tasks(task_groups, mode="tree")
        console.print(tree_panel)
        summary_panel = formatter.format_summary(task_groups)
        console.print(summary_panel)
