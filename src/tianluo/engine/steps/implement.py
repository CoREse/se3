"""Implement step handler.

Executes implementation of task groups, writing code to files.
Uses LLM (claude -p) with TWO_PHASE JSON extraction.
Supports fix iterations for the test-verify-fix loop.
"""

from __future__ import annotations
from tianluo.runtime_paths import runtime_dir

import copy
import json
import logging
import re
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..dag_scheduler import (
    DAGScheduler,
    GROUP_STATUS_COMPLETED,
    GROUP_STATUS_FAILED,
    GROUP_STATUS_RUNNING,
    GroupResult,
    RelayContext,
    RelayPlan,
    _relay_plan_is_linear,
    classify_chains,
)
from ..chat_history import record_group_status
from ...agent_runner import AgentInvocationIntent
from ..prompt_markers import inject_boundary
from ..transitive_reduction import transitive_reduce
from ..llm_caller import LLMCaller, LLMCallError
from ..models import FlowInstance, Step, StepStatus
from ..plan_decomposition import (
    HOLISTIC_MODE_LEGACY_DIRECT,
    HOLISTIC_MODE_SINGLE_GROUP,
    holistic_execution_mode,
    is_capability_decomposition,
    is_forced_single_group,
)
from ._project_root import resolve_flow_project_root
from ..utils.json_parser import parse_json_response
from .plan import VERSION_FILE_GUARDRAIL as _PLAN_VERSION_FILE_GUARDRAIL
# The root-cause section is defined once in plan.py and shared, so the two
# consumers of an investigation report cannot render it differently.
from .plan import ROOT_CAUSE_SECTION, render_root_cause_section  # noqa: F401

# Re-export for tests / clarity at the implement.py module level.
VERSION_FILE_GUARDRAIL = _PLAN_VERSION_FILE_GUARDRAIL

# Additional clause appended to FIX_PROMPT: fix iterations must also not bump
# the version file. The version bump is always handled by the engine's
# version_analyze + commit steps, not by the LLM during a fix iteration.
FIX_VERSION_FILE_GUARDRAIL = """
Fix iterations are also covered by this guardrail: do NOT include a version
bump (modifying `pyproject.toml`, `package.json`, `VERSIONS.md`, or any other
project version file) as part of a fix. If a test failure or verification
issue appears to be "wrong version number", the underlying cause is in the
engine's version-handling steps, not in the implementation — flag it in your
fix summary and leave the version file untouched.
"""

# Why-comment convention (prompt-level soft constraint). Comments carry only
# *why* / intent — the reasoning the code itself cannot express — and are
# updated ONLY when that why changes, so there is no per-change comment-sync
# tax. They are NOT a source for the code-index (the index is generated
# structurally + LLM-summarised from the code, never from comments). This is a
# soft, prompt-level reminder of the same strength as the project's other
# conventions: it only prompts, it does not mechanically guarantee the comment
# stays in sync with intent. Contains no `{...}` placeholders, so it is safe to
# concatenate after the `.format(...)`-style prompt body.
WHY_COMMENT_CONVENTION = """
## Why-Comment Convention
Comments must carry only the *why* / intent — the reasoning the code itself
cannot express (a non-obvious trade-off, a constraint, the reason a path
exists). They are NOT a restatement of *what* the code does.

When you change code AND its why / intent changes, update the colocated
why-comment in the same edit so the recorded intent stays truthful. When the
why is unchanged, leave the comment alone — do NOT churn comments on every
edit; only a changed why warrants a comment change. Do NOT add narrating
comments that merely paraphrase the code.

This is a soft, prompt-level convention (same strength as the project's other
conventions) — it is a reminder, not a mechanically enforced rule. Comments are
never harvested as a source for the code-index; the structure map is derived
from the code itself.
"""
# Two things the implement / fix agents get wrong on their own, stated once
# here and injected into all three base templates (and thereby into every
# variant derived from them).
#
# WHY it sits next to the `estimated_test_duration` note: same topic — how
# much testing this call itself owes — and the estimate stays whole-suite even
# though the run deliberately is not.
#
# WHY the test-scope clause is soft ("by default", "only when"): a genuinely
# cross-module change does need wider verification, and a flat ban would
# suppress that too. Measured motive: a majority of implement calls were
# re-running the whole serial suite that the flow's own TEST step then re-runs
# in parallel anyway.
#
# WHY the headless clause is phrased as fact, not as a rule: agents have
# backgrounded a full suite, reported it as "still in progress", and waited to
# be woken — which cannot happen in a process that exits with its final
# output. The wrong belief about the runtime is the root cause; forbidding the
# symptom would not correct it.
#
# Placeholder-free (contains no `{...}`), so it survives the `.format(...)`
# rendering of the templates it is injected into.
TEST_SCOPE_AND_HEADLESS_CLAUSE = """\
  - **Test scope**: The flow's TEST step runs the project's full suite, so by default run only tests directly related to your changes, unless you have concrete reason to expect impact beyond the modules touched — then run the full suite in the foreground, to its result. Never report an unobserved test run as an incomplete task.
  - **Headless run contract**: You run headless: this turn's output ends the process, with no later wake-up. Leave no background jobs or monitors — everything your report depends on must finish before the final JSON.
"""

from ..worktree import (
    _run_git,
    create_worktree,
    delete_branch,
    detect_unmerged_paths,
    force_cleanup_worktree,
    fork_worktree,
    get_conflicting_files,
    get_current_branch,
    has_commits,
    has_new_commits,
    merge_in_progress,
    recover_stale_unmerged_paths,
    resolve_merge_conflicts_with_context,
)
from ..stash_utils import (
    ArchivedEntry,
    format_archived_manifest as _format_archived_manifest,
    parse_stashpop_already_exists as _parse_stashpop_already_exists,
    pop_stash_by_label as _pop_stash_by_label,
    resolve_stashpop_safely as _resolve_stashpop_safely,
    take_ours_for_stashpop as _take_ours_for_stashpop,
)
# The LLM-aware case-a resolver lives in the LLM integration layer, not in the
# generic no-data-loss stash_utils engine; the leaf-back merge injects it into
# ``resolve_stashpop_safely`` so stash_utils stays free of LLM-stack coupling.
from ..stashpop_llm_resolver import (
    make_llm_stashpop_resolver as _make_llm_stashpop_resolver,
)

logger = logging.getLogger(__name__)


def _prune_recovered_dependencies(
    groups: list[dict], completed_groups: set[str]
) -> list[dict]:
    """Drop already-completed groups and prune dangling ``depends_on`` edges.

    During DAG disaster recovery, groups whose impl branches survived a crash
    (``completed_groups``) are removed from the to-run set. Those groups have
    been pre-merged into the baseline branch, so any ``depends_on`` edge in a
    retained group that still points at them is semantically already satisfied
    and would otherwise dangle — tripping ``DAGScheduler._build_dag`` with a
    "depends on unknown group" error. This helper returns deep-copied group
    dicts (so the original ``groups`` list is never mutated) with completed
    groups dropped and their inbound edges pruned from the survivors.
    """
    retained: list[dict] = []
    for g in groups:
        gid = g.get("group_id", g.get("name", "unknown"))
        if gid in completed_groups:
            continue
        g_copy = copy.deepcopy(g)
        deps = g_copy.get("depends_on")
        if deps:
            pruned = [d for d in deps if d not in completed_groups]
            if len(pruned) != len(deps):
                logger.info(
                    "DAG disaster recovery: pruned dangling depends_on edges "
                    "from group %s (%s already completed/pre-merged)",
                    gid,
                    sorted(set(deps) - set(pruned)),
                )
            g_copy["depends_on"] = pruned
        retained.append(g_copy)
    return retained


IMPLEMENT_PROMPT = """You are an expert software engineer. Implement the following tasks by writing code.

## Agent Safety: Process Cleanup

This entire prompt — including this section — is passed to your runtime via
argv and is therefore visible in `/proc/<pid>/cmdline` for your own process
and any sibling agents launched the same way. Pattern-based process matchers
read that file, so a command like `pkill -f <pattern>` will match its own
invoker whenever the pattern occurs anywhere in this prompt. Running such a
command can kill yourself, your parent shell, or peer agents mid-flight.

Hard bans — never issue these to clean up processes:
- `pkill -f <pattern>` (matches against full argv, including this prompt).
- `pgrep -f <pattern> | xargs kill` (same self-match hazard plus PID reuse).
- `killall <name>` where `<name>` is a shared interpreter or shell such as
  `python`, `node`, `claude`, or `bash` (kills unrelated processes).

Preferred alternatives, in order:
1. If you spawned the process yourself, capture its PID at spawn time
   (e.g. `mycmd & echo $!`, or read `$!` after backgrounding) and later
   `kill <pid>` that exact PID — no pattern matching needed.
2. If you did not spawn it but know its short command name (`comm`, max 15
   chars), use `pkill -x <comm>` for an exact-name match — `-x` does not
   read argv, so the self-match hazard is gone.
3. If `-f` is genuinely unavoidable, first run `pgrep -af <pattern>` and
   inspect the list. Exclude `$$`, `$PPID`, and every ancestor PID up the
   process tree before passing survivors to `kill`. Never pipe `pgrep -f`
   directly into `kill` or `xargs`.

## Task Description
{task_description}

## Task Type
{task_type}
{root_cause_section}

## Task Groups
{task_groups}

## Instructions
1. Read the relevant source files before making changes.
2. Implement each task in the task groups above.
3. Follow the project's coding conventions.
4. Write tests if the task requires them.
5. Do NOT commit — only write/edit files.
6. If you need to install dependencies or generate build artifacts, FIRST add the output directory to .gitignore (e.g., `node_modules/`, `.pixi/`, `venv/`, `dist/`) before running the install command.

When you are done, output a JSON summary of what you did:
```json
{{
    "files_changed": ["path/to/file1.py", "path/to/file2.py"],
    "tests_added": ["tests/test_new.py"],
    "estimated_test_duration": 120,
    "test_mapping": {{}},
    "summary": "Brief description of changes made",
    "completion_status": "complete",
    "incomplete_tasks": [],
    "restricted_edits": []
}}
```

### Response field notes:
- **completion_status**: Set to "complete" if all tasks were done, "partial" if some tasks could not be completed (e.g., permission restrictions on sensitive files), or "failed" if no meaningful progress was made.
- **estimated_test_duration**: Integer, estimated number of seconds the project's full test suite will take to run (ALL tests in the project, not only the new ones you added). The test runner executes the whole suite with one command, so this estimate must cover every test that will run. Consider existing tests in the repo plus any you added. This helps the test runner allocate appropriate time.
- **incomplete_tasks**: An array of strings, each describing a task that could not be completed and why. Only populate when completion_status is "partial" or "failed".
- **restricted_edits**: An array of edits you attempted but could NOT perform due to file permission/protection restrictions (e.g., files under `.claude/` directory). Each entry must be: {{"file_path": "path/to/file", "old_string": "text to replace", "new_string": "replacement text"}}. Always attempt edits normally first — only use this field for edits that were rejected by the permission system.
"""

IMPLEMENT_GROUP_PROMPT = """You are an expert software engineer. Implement the tasks for this specific group by writing code.

## Agent Safety: Process Cleanup

This entire prompt — including this section — is passed to your runtime via
argv and is therefore visible in `/proc/<pid>/cmdline` for your own process
and any sibling agents launched the same way. Pattern-based process matchers
read that file, so a command like `pkill -f <pattern>` will match its own
invoker whenever the pattern occurs anywhere in this prompt. Running such a
command can kill yourself, your parent shell, or peer agents mid-flight.

Hard bans — never issue these to clean up processes:
- `pkill -f <pattern>` (matches against full argv, including this prompt).
- `pgrep -f <pattern> | xargs kill` (same self-match hazard plus PID reuse).
- `killall <name>` where `<name>` is a shared interpreter or shell such as
  `python`, `node`, `claude`, or `bash` (kills unrelated processes).

Preferred alternatives, in order:
1. If you spawned the process yourself, capture its PID at spawn time
   (e.g. `mycmd & echo $!`, or read `$!` after backgrounding) and later
   `kill <pid>` that exact PID — no pattern matching needed.
2. If you did not spawn it but know its short command name (`comm`, max 15
   chars), use `pkill -x <comm>` for an exact-name match — `-x` does not
   read argv, so the self-match hazard is gone.
3. If `-f` is genuinely unavoidable, first run `pgrep -af <pattern>` and
   inspect the list. Exclude `$$`, `$PPID`, and every ancestor PID up the
   process tree before passing survivors to `kill`. Never pipe `pgrep -f`
   directly into `kill` or `xargs`.

## Task Description
{task_description}

## Task Type
{task_type}
{root_cause_section}

## Current Group Tasks
{current_group}

## Previous Groups Context
{previous_results}

## Instructions
1. Read the relevant source files before making changes.
2. Implement the tasks listed in Current Group Tasks above.
3. Follow the project's coding conventions.
4. Write tests if the task requires them.
5. Do NOT commit — only write/edit files.
6. If you need to install dependencies or generate build artifacts, FIRST add the output directory to .gitignore (e.g., `node_modules/`, `.pixi/`, `venv/`, `dist/`) before running the install command.

When you are done, output a JSON summary of what you did:
```json
{{
    "files_changed": ["path/to/file1.py", "path/to/file2.py"],
    "tests_added": ["tests/test_new.py"],
    "estimated_test_duration": 120,
    "test_mapping": {{}},
    "summary": "Brief description of changes made",
    "completion_status": "complete",
    "incomplete_tasks": [],
    "restricted_edits": []
}}
```

### Response field notes:
- **completion_status**: Set to "complete" if all tasks were done, "partial" if some tasks could not be completed (e.g., permission restrictions on sensitive files), or "failed" if no meaningful progress was made.
- **estimated_test_duration**: Integer, estimated number of seconds the project's full test suite will take to run (ALL tests in the project, not only this group's new ones). The test runner executes one command over the whole suite regardless of how many groups there are, so every group must report a whole-suite estimate, not a per-group delta. This helps the test runner allocate appropriate time.
- **incomplete_tasks**: An array of strings, each describing a task that could not be completed and why. Only populate when completion_status is "partial" or "failed".
- **restricted_edits**: An array of edits you attempted but could NOT perform due to file permission/protection restrictions (e.g., files under `.claude/` directory). Each entry must be: {{"file_path": "path/to/file", "old_string": "text to replace", "new_string": "replacement text"}}. Always attempt edits normally first — only use this field for edits that were rejected by the permission system.
"""

FIX_PROMPT = """You are an expert software engineer. Fix the issues found in the previous implementation.

## Agent Safety: Process Cleanup

This entire prompt — including this section — is passed to your runtime via
argv and is therefore visible in `/proc/<pid>/cmdline` for your own process
and any sibling agents launched the same way. Pattern-based process matchers
read that file, so a command like `pkill -f <pattern>` will match its own
invoker whenever the pattern occurs anywhere in this prompt. Running such a
command can kill yourself, your parent shell, or peer agents mid-flight.

Hard bans — never issue these to clean up processes:
- `pkill -f <pattern>` (matches against full argv, including this prompt).
- `pgrep -f <pattern> | xargs kill` (same self-match hazard plus PID reuse).
- `killall <name>` where `<name>` is a shared interpreter or shell such as
  `python`, `node`, `claude`, or `bash` (kills unrelated processes).

Preferred alternatives, in order:
1. If you spawned the process yourself, capture its PID at spawn time
   (e.g. `mycmd & echo $!`, or read `$!` after backgrounding) and later
   `kill <pid>` that exact PID — no pattern matching needed.
2. If you did not spawn it but know its short command name (`comm`, max 15
   chars), use `pkill -x <comm>` for an exact-name match — `-x` does not
   read argv, so the self-match hazard is gone.
3. If `-f` is genuinely unavoidable, first run `pgrep -af <pattern>` and
   inspect the list. Exclude `$$`, `$PPID`, and every ancestor PID up the
   process tree before passing survivors to `kill`. Never pipe `pgrep -f`
   directly into `kill` or `xargs`.

## Task Description
{task_description}
{root_cause_section}
## Fix Instructions
{fix_instructions}

## Fix Context
{fix_context}

## Fix History
{fix_history}

## Fix Iteration
This is fix iteration {fix_iteration}.

## Instructions
1. Read the failing test output and error messages carefully.
2. Fix the root cause — do not just suppress errors.
3. Run the relevant tests mentally to verify your fix.
4. Do NOT commit — only write/edit files.
5. If you need to install dependencies, FIRST add the output directory to .gitignore before running the install command.

When you are done, output a JSON summary of what you did:
```json
{{
    "files_changed": ["path/to/file1.py"],
    "tests_added": [],
    "estimated_test_duration": 120,
    "test_mapping": {{}},
    "summary": "Brief description of fix",
    "completion_status": "complete",
    "incomplete_tasks": [],
    "restricted_edits": []
}}
```

### Response field notes:
- **completion_status**: Set to "complete" if all issues were fixed, "partial" if some fixes could not be applied (e.g., permission restrictions on sensitive files), or "failed" if no meaningful progress was made.
- **estimated_test_duration**: Integer, estimated number of seconds the project's full test suite will take to run (ALL tests in the project, not only the new ones you added). This helps the test runner allocate appropriate time.
- **incomplete_tasks**: An array of strings, each describing a fix that could not be applied and why. Only populate when completion_status is "partial" or "failed" — it must be an empty array when the status is "complete".
- **restricted_edits**: An array of edits you attempted but could NOT perform due to file permission/protection restrictions (e.g., files under `.claude/` directory). Each entry must be: {{"file_path": "path/to/file", "old_string": "text to replace", "new_string": "replacement text"}}. Always attempt edits normally first — only use this field for edits that were rejected by the permission system.

### Timeout guidance:
If the fix context indicates that tests previously timed out (timeout_reason is present), first investigate whether the timeout reflects a real hang or infinite loop in the code you changed — the test runner caps computed timeouts at its configured maximum, so simply increasing the estimate cannot rescue a runaway test. If the prior timeout looks like a genuine under-estimate (the suite really does take that long), provide a meaningfully higher `estimated_test_duration` (roughly 1.5–2× the previous value is usually enough; don't blindly multiply without bound). If the fix context indicates `Timeout at cap: true`, raising `estimated_test_duration` will NOT produce a larger timeout — the prior run was already at the cap, so focus on splitting the suite or fixing the slow/hung test instead.
"""

# WHY the shared clause is injected rather than written into each template:
# the three bases are the only place it can live once and still reach the
# derived variants (HOLISTIC_IMPLEMENT_PROMPT and
# IMPLEMENT_CAPABILITY_GROUP_PROMPT are built from them further down), so this
# must run before those derivations. A missing anchor raises at import — the
# same failure mode as `_substitute_anchor` below — because a base edit that
# silently dropped the clause from every derived variant would be invisible.
def _insert_after_line(template: str, line_prefix: str, insertion: str) -> str:
    if template.count(line_prefix) != 1:
        raise ValueError(
            f"prompt template does not contain exactly one line starting with "
            f"{line_prefix!r}; the shared test-scope clause cannot be placed "
            "next to it. Update the anchor together with the base template."
        )
    start = template.index(line_prefix)
    end = template.index("\n", start) + 1
    return template[:end] + insertion + template[end:]


_ESTIMATE_NOTE_ANCHOR = "- **estimated_test_duration**:"

IMPLEMENT_PROMPT = _insert_after_line(
    IMPLEMENT_PROMPT, _ESTIMATE_NOTE_ANCHOR, TEST_SCOPE_AND_HEADLESS_CLAUSE,
)
IMPLEMENT_GROUP_PROMPT = _insert_after_line(
    IMPLEMENT_GROUP_PROMPT, _ESTIMATE_NOTE_ANCHOR, TEST_SCOPE_AND_HEADLESS_CLAUSE,
)
FIX_PROMPT = _insert_after_line(
    FIX_PROMPT, _ESTIMATE_NOTE_ANCHOR, TEST_SCOPE_AND_HEADLESS_CLAUSE,
)

# Append the version-file guardrail to all three implementation prompt
# templates. The guardrail text contains no `{...}` placeholders, so it's safe
# to concatenate after the `.format(...)`-style prompt body.
IMPLEMENT_PROMPT = IMPLEMENT_PROMPT + "\n" + VERSION_FILE_GUARDRAIL
IMPLEMENT_GROUP_PROMPT = IMPLEMENT_GROUP_PROMPT + "\n" + VERSION_FILE_GUARDRAIL
FIX_PROMPT = FIX_PROMPT + "\n" + VERSION_FILE_GUARDRAIL + FIX_VERSION_FILE_GUARDRAIL

# Append the why-comment convention (a prompt-level soft constraint) to all
# three implementation prompt templates. Placeholder-free, so safe to
# concatenate after the `.format(...)`-style body.
IMPLEMENT_PROMPT = IMPLEMENT_PROMPT + "\n" + WHY_COMMENT_CONVENTION
IMPLEMENT_GROUP_PROMPT = IMPLEMENT_GROUP_PROMPT + "\n" + WHY_COMMENT_CONVENTION
FIX_PROMPT = FIX_PROMPT + "\n" + WHY_COMMENT_CONVENTION

# Direct and small deliberately share one whole-task execution contract.  The
# replacement removes task_groups from the rendered prompt altogether; a
# strategy-free small flow and a direct feature remain distinguishable through
# the execution-mode text and runner intent passed below.
HOLISTIC_IMPLEMENT_PROMPT = IMPLEMENT_PROMPT.replace(
    "You are an expert software engineer. Implement the following tasks by writing code.",
    "You are an expert software engineer. Complete this entire requirement by writing code.",
).replace(
    # WHY the permission is stated without naming PLAN: this template is
    # rendered for a small task and a resumed legacy `direct` flow too, neither
    # of which has a PLAN step at all — justifying the permission with "PLAN
    # does not pre-split the work" would describe a plan decision the reader
    # can see does not exist. The PLAN-side justification lives in
    # CAPABILITY_GROUP_DOCTRINE, where a PLAN step is always present.
    "## Task Groups\n{task_groups}",
    """## Holistic Implementation Mode
{execution_mode}

## Autonomous Execution Contract
Independently analyze the complete effective requirement and the repository,
then implement the requirement in full. Do not stop after analysis or a partial
subset. Inspect any additional code needed to understand cross-module effects,
run targeted validation appropriate to the changes, and only then return the
existing structured implementation summary. You are allowed — and encouraged —
to spawn subagents inside this call to decompose the requirement yourself: no
per-task breakdown is supplied here, so the internal breakdown is your own
planning / sub-agent job.

## ANALYZE Context
{analysis_context}

## Prior Partial-Attempt Context
{continuation_context}""",
).replace(
    # The holistic prompt carries no Task Groups section, so the group-oriented
    # instruction would send the caller looking for an enumerated task list —
    # and its absence could read as license to stop early, contradicting the
    # Autonomous Execution Contract above.
    "2. Implement each task in the task groups above.",
    "2. Implement the complete effective requirement described above.",
)

# Two-segment marker only: USER_CONTENT region is empty.
# implement consumes upstream LLM artifacts (task_groups / changes_made /
# test_results) and framework-derived task_description;
# nothing at this assembly point is a user-literal field. The web console
# renders the whole post-BEGIN tail inside the collapsed system-prompt chip.
IMPLEMENT_PROMPT = inject_boundary(IMPLEMENT_PROMPT, "## Task Description\n")
HOLISTIC_IMPLEMENT_PROMPT = inject_boundary(
    HOLISTIC_IMPLEMENT_PROMPT, "## Task Description\n",
)
IMPLEMENT_GROUP_PROMPT = inject_boundary(
    IMPLEMENT_GROUP_PROMPT, "## Task Description\n",
)
FIX_PROMPT = inject_boundary(FIX_PROMPT, "## Task Description\n")


CAPABILITY_GROUP_DOCTRINE = """
### What this group is
This is a **coarse task group**, not a task list. Under the capability
decomposition doctrine PLAN sizes every group by one coherent task — or, where
a task reaches the capability edge, one split of it — that one autonomous
implementation call can safely carry, and deliberately emits no per-task
breakdown for it — the fields above (group_id, name, description, group_order,
depends_on) are the whole of what PLAN produced. There is no enumerated task
list here and none is coming.

Producing that breakdown is your job: you are allowed — and encouraged — to
spawn subagents inside this call to decompose the task yourself, then implement
it end to end. How the task breaks down internally is your own planning /
sub-agent job; PLAN deliberately did not pre-split it for you.

The group is delivered only when its task actually works **and** is covered by
the tests it needs. Testing and verification are part of this group's own
delivery — they are never split out into a group of their own.
"""

# WHY the capability doctrine gets its own group prompt: the template above
# points the agent at an enumeration ("Implement the tasks listed in Current
# Group Tasks above") that a capability group deliberately does not carry, so
# rendering it unchanged instructs the agent to execute an empty list — on the
# default doctrine's primary multi-group path (DAG-parallel and sequential
# alike). Derived from the base by anchored substitution rather than written
# out as a second template so the parts that are not doctrine-specific (safety
# section, guardrails, JSON response contract, marker boundaries) keep exactly
# one source; a base edit that moves an anchor fails at import instead of
# silently dropping the capability wording.
def _substitute_anchor(template: str, anchor: str, replacement: str) -> str:
    if anchor not in template:
        raise ValueError(
            "IMPLEMENT_GROUP_PROMPT no longer contains the anchor "
            f"{anchor!r}; the capability group prompt cannot be derived from "
            "it. Update the anchor together with the base template."
        )
    return template.replace(anchor, replacement)


IMPLEMENT_CAPABILITY_GROUP_PROMPT = _substitute_anchor(
    _substitute_anchor(
        _substitute_anchor(
            _substitute_anchor(
                IMPLEMENT_GROUP_PROMPT,
                "You are an expert software engineer. Implement the tasks for "
                "this specific group by writing code.",
                "You are an expert software engineer. Implement one coarse "
                "task group of this requirement by writing code.",
            ),
            "## Current Group Tasks\n{current_group}",
            "## Current Capability Group\n{current_group}\n"
            + CAPABILITY_GROUP_DOCTRINE,
        ),
        "2. Implement the tasks listed in Current Group Tasks above.",
        "2. Decompose the task described in Current Capability Group above "
        "into whatever steps it needs — that breakdown is your own planning / "
        "sub-agent job — then implement it end to end.",
    ),
    "4. Write tests if the task requires them.",
    "4. Cover this task with its own tests; the group is not delivered until "
    "they pass.",
)


def _group_prompt_template(capability_mode: bool) -> str:
    """Pick the per-group prompt matching the flow's decomposition doctrine."""
    return (
        IMPLEMENT_CAPABILITY_GROUP_PROMPT
        if capability_mode
        else IMPLEMENT_GROUP_PROMPT
    )


def _holistic_execution_mode(
    step: Step, flow: FlowInstance,
) -> str | None:
    """Return the whole-task execution shape for this step, if it has one.

    Thin adapter over the shared predicate in ``plan_decomposition`` so this
    handler and the state machine's auto-continuation gate read the shape the
    same way.
    """
    return holistic_execution_mode(
        task_type=step.inputs.get("task_type") or flow.task_type,
        inputs=step.inputs,
        context=flow.state.context,
    )


def _run_holistic_implement(
    *,
    mode: str,
    step: Step,
    flow: FlowInstance,
    project_root: Path,
    task_description: str,
    task_type: str,
    root_cause_section: str,
    injection: str,
    retry_count: int,
) -> StepStatus:
    """Execute one whole-task implementation.

    Covers every shape that runs the requirement through a single autonomous
    call: a small task, a single capability group (or one forced by
    ``plan_granularity: single``), and a resumed legacy ``direct`` flow.
    """
    if mode == HOLISTIC_MODE_LEGACY_DIRECT:
        # WHY a distinct wording rather than reusing the single-group text: this
        # mode is reported only for a legacy `direct` flow that holds no groups
        # at all, so telling the agent "PLAN sized this as a single capability
        # group" would describe a plan it can see does not exist. The execution
        # contract is identical; only the reason the flow is in it differs. Such
        # a flow that did reach PLAN after the upgrade is classified as
        # `single_group` instead, so its groups reach the prompt below.
        execution_mode = (
            "This flow was created under the retired direct implementation "
            "strategy: it carries no plan and no task groups, and the whole "
            "requirement is delivered in this one autonomous implementation "
            "path."
        )
        invocation_intent = AgentInvocationIntent.DIRECT_IMPLEMENTATION
    elif mode == HOLISTIC_MODE_SINGLE_GROUP:
        planned_groups = step.inputs.get("task_groups")
        collapsed = (
            isinstance(planned_groups, list)
            and len(planned_groups) > 1
            and is_forced_single_group(step.inputs, flow.state.context)
        )
        if collapsed:
            # WHY the groups are still shown: forced-single fixes the execution
            # *shape*, it does not discard what PLAN reasoned out. Stating the
            # collapse plainly also keeps the prompt truthful — claiming "PLAN
            # sized this as one group" would contradict a plan the reader can
            # see has several.
            execution_mode = (
                "This flow is configured for forced single-call execution "
                "(plan_granularity=single): the whole requirement is delivered "
                "in this one autonomous implementation path. PLAN still listed "
                "several capability groups; treat them as an outline of the "
                "work rather than a split to execute separately — every one of "
                "them is yours to complete in this call.\n\n"
                "### Planned capability groups (outline only)\n"
                + json.dumps(
                    planned_groups, indent=2, ensure_ascii=False, default=str,
                )
            )
        else:
            execution_mode = (
                "PLAN sized this task as a single capability group, i.e. one "
                "autonomous implement call can carry the whole of it. There is "
                "no per-group split to follow; deliver the complete effective "
                "requirement in this one autonomous implementation path."
            )
            # WHY the description is rendered while the group's identity
            # (group_id / name / ordering) is not: the capability schema
            # requires a group's description to be written in enough detail
            # that one autonomous implement call can execute it end to end, so
            # for a single-group plan that description *is* PLAN's statement of
            # this call's scope — dropping it would make this the only path
            # that discards what PLAN said about the work it executes. The
            # scheduling fields around it exist to order groups against each
            # other, which is meaningless with one group, and printing them
            # would reintroduce the per-group split this mode just denied.
            single_description = (
                planned_groups[0].get("description")
                if isinstance(planned_groups, list)
                and len(planned_groups) == 1
                and isinstance(planned_groups[0], dict)
                else None
            )
            if single_description:
                execution_mode += (
                    "\n\n### What PLAN scoped this single group to deliver\n"
                    f"{single_description}"
                )
        invocation_intent = AgentInvocationIntent.DIRECT_IMPLEMENTATION
    else:
        execution_mode = (
            "Small task type. Preserve its compact task semantics while using "
            "the shared whole-task implementation executor."
        )
        invocation_intent = AgentInvocationIntent.DEFAULT

    analysis_context = step.inputs.get("analysis_context")
    if not isinstance(analysis_context, dict):
        analysis_context = {
            "scope": step.inputs.get("scope"),
            "complexity": step.inputs.get("complexity"),
            "reasoning": step.inputs.get("analysis_reasoning"),
            "project_summary": step.inputs.get("project_summary"),
        }
    continuation = step.inputs.get("previous_output") or {}
    prompt = HOLISTIC_IMPLEMENT_PROMPT.format(
        task_description=task_description,
        task_type=task_type,
        execution_mode=execution_mode,
        analysis_context=json.dumps(
            analysis_context, indent=2, ensure_ascii=False, default=str,
        ),
        continuation_context=json.dumps(
            continuation, indent=2, ensure_ascii=False, default=str,
        ) if continuation else "No previous partial attempt.",
        root_cause_section=root_cause_section,
    )
    if injection:
        prompt += injection

    return _run_single_llm_call(
        prompt,
        step,
        flow,
        project_root,
        [],
        retry_count,
        implemented_groups_override=[],
        invocation_intent=invocation_intent,
        preserve_existing_outputs=True,
    )


def implement_handler(step: Step, flow: FlowInstance) -> StepStatus:
    """Execute the implement step.

    Calls LLM via claude -p to actually write/edit source files.
    Uses TWO_PHASE JSON mode: LLM writes code naturally, then we
    extract the JSON summary of what was changed.

    In fix iterations, focuses on fixing issues identified by verify_spec.

    Args:
        step: The current step being executed
        flow: The flow instance containing context

    Returns:
        StepStatus.COMPLETED on success, StepStatus.FAILED on error
    """
    task_description = step.inputs.get("task_description", "")
    task_type = step.inputs.get("task_type") or flow.task_type or "feature"
    holistic_mode = _holistic_execution_mode(step, flow)
    fix_context = step.inputs.get("fix_context")
    fix_instructions = step.inputs.get("fix_instructions")
    is_fix_iteration = step.inputs.get("is_fix_iteration", False)
    fix_iteration = step.inputs.get("fix_iteration", 0)

    project_root = resolve_flow_project_root(flow)

    # Baseline for session-commit collection: prefer the flow-wide baseline
    # captured at flow init (state_machine._record_baseline_commit), which is
    # stable across resumes/fix iterations. Falls back to current HEAD only
    # if the flow has no baseline yet (e.g. early-failure edge cases).
    # Using the flow-wide baseline ensures _collect_session_commits spans
    # the entire implement phase even when this handler is re-entered after
    # a partial run that already merged some group branches back to main.
    baseline_hash = getattr(flow, "baseline_commit", None) or _get_head_hash(
        project_root,
    )

    # Capture pre_session_version exactly once per Step. On re-entry (fix
    # iteration, DAG resume) the disk version may have been bumped by a
    # previously-merged worktree group; overwriting here would clobber the
    # true pre-implement baseline and let version_analyze double-bump.
    if "pre_session_version" not in step.outputs:
        step.outputs["pre_session_version"] = _read_pre_session_version(
            project_root,
        )
    # session_commits is recomputed below from `baseline_hash` so the list
    # always reflects everything implement has merged onto main since the
    # flow started. Seed with [] only on first entry to keep prior-run data
    # visible if a downstream path fails before we recompute.
    if "session_commits" not in step.outputs:
        step.outputs["session_commits"] = []

    # Root-cause report from a preceding INVESTIGATE round, if any. Rendered
    # once and shared by every prompt path below (single call, grouped,
    # DAG-parallel, fix iteration) so no path silently loses it. Empty string
    # when no investigation ran, which keeps those prompts byte-identical.
    root_cause_section = render_root_cause_section(
        step.inputs.get("root_cause_report"),
        exhausted=bool(step.inputs.get("investigation_exhausted")),
    )

    # Append issue discovery injection if applicable
    from ..context_builder import (
        get_issue_discovery_injection,
        get_charter_injection,
        get_code_index_injection,
        get_runtime_environment_injection,
    )
    injection = get_issue_discovery_injection("implement", project_root) or ""
    # Charter (full text) + code-index top map replace the retired spec-name
    # list: project conventions come from the charter, and the code-index top
    # map orients the implementer (function-level detail on demand via
    # `luo code-index show <path>`).
    injection += get_charter_injection(project_root)
    # No code-index refresh here: the read-side map was refreshed once in analyze
    # (before any code changed) and the post-implement map is refreshed once just
    # before commit. See analyze.py / commit.py for the two-point rationale.
    injection += get_code_index_injection(project_root)
    injection += get_runtime_environment_injection("implement", project_root)

    retry_count = step.inputs.get("retry_count", 0)

    # Build the prompt
    if is_fix_iteration and fix_instructions:
        logger.info(f"Running fix iteration {fix_iteration}")
        fix_history = step.inputs.get("fix_history", [])
        fix_history_text = _format_fix_history(fix_history)
        fix_context_text = _format_fix_context_structured(fix_context)
        prompt = FIX_PROMPT.format(
            task_description=task_description,
            fix_instructions=fix_instructions,
            fix_context=fix_context_text,
            fix_iteration=fix_iteration,
            fix_history=fix_history_text,
            root_cause_section=root_cause_section,
        )
        if injection:
            prompt += injection

        if holistic_mode:
            result = _run_single_llm_call(
                prompt,
                step,
                flow,
                project_root,
                [],
                retry_count,
                implemented_groups_override=[],
                preserve_existing_outputs=True,
            )
        else:
            result = _run_single_llm_call(
                prompt,
                step,
                flow,
                project_root,
                step.inputs.get("task_groups", []),
                retry_count,
            )
        # Recompute session_commits against the flow-wide baseline so any
        # commits a prior worktree-DAG entry merged onto main remain visible
        # to version_analyze. The fix-iteration LLM call itself does not
        # commit anything, so this is purely about preserving prior-entry
        # data; we still recompute (rather than keep the cached value) so a
        # subsequent merge that happened between entries is picked up.
        step.outputs["session_commits"] = _collect_session_commits(
            project_root, baseline_hash,
        )
        _resolve_files_changed(step, project_root, baseline_hash)
        return result

    if holistic_mode:
        result = _run_holistic_implement(
            mode=holistic_mode,
            step=step,
            flow=flow,
            project_root=project_root,
            task_description=task_description,
            task_type=task_type,
            root_cause_section=root_cause_section,
            injection=injection,
            retry_count=retry_count,
        )
        step.outputs["implemented_groups"] = []
        _resolve_files_changed(step, project_root, baseline_hash)
        return result

    # Determine if we should use group-by-group execution
    task_groups = step.inputs.get("task_groups", [])
    groups = _extract_sorted_groups(task_groups)

    if len(groups) <= 1:
        # Fallback: single LLM call for empty or single group
        _display_task_plan(groups, "single", _compute_total_loc(groups), 0)
        if isinstance(task_groups, list):
            task_groups_text = json.dumps(task_groups, indent=2, ensure_ascii=False)
        else:
            task_groups_text = str(task_groups)

        prompt = IMPLEMENT_PROMPT.format(
            task_description=task_description,
            task_type=task_type,
            task_groups=task_groups_text,
            root_cause_section=root_cause_section,
        )
        if injection:
            prompt += injection

        result = _run_single_llm_call(
            prompt, step, flow, project_root, task_groups, retry_count,
        )
        _resolve_files_changed(step, project_root, baseline_hash)
        return result

    # LOC threshold: merge small multi-group tasks into single LLM call
    from ...config import ImplementConfig
    impl_config = ImplementConfig.load(project_root)
    total_loc = _compute_total_loc(groups)
    # WHY the LOC gate is bypassed under the capability doctrine: coarse groups
    # carry no per-task ``estimated_loc``, so ``_compute_total_loc`` is always
    # 0 for them. Zero satisfies neither "> 0" here nor "> threshold" in
    # ``_should_use_dag``, which would silently collapse every multi-group
    # capability plan into a sequential run and throw away the parallelism that
    # independent groups exist for. The threshold keeps its exact meaning for
    # granular/legacy plans, which do carry per-task LOC. Note the bypass rests
    # on the missing estimate alone, not on group size: a capability group is
    # one coherent task, aggregated by default and split only at the capability
    # edge, so it is bounded by what one call can carry but may well be tiny
    # (two unrelated 20-LOC tasks are two groups by doctrine).
    capability_mode = is_capability_decomposition(step.inputs, flow.state.context)

    if not capability_mode and total_loc > 0 and total_loc <= impl_config.group_loc_threshold:
        logger.info(
            "Total LOC %d <= threshold %d, merging %d groups into single LLM call",
            total_loc, impl_config.group_loc_threshold, len(groups),
        )
        _display_task_plan(groups, "single", total_loc, impl_config.group_loc_threshold)
        if isinstance(task_groups, list):
            task_groups_text = json.dumps(task_groups, indent=2, ensure_ascii=False)
        else:
            task_groups_text = str(task_groups)

        prompt = IMPLEMENT_PROMPT.format(
            task_description=task_description,
            task_type=task_type,
            task_groups=task_groups_text,
            root_cause_section=root_cause_section,
        )
        if injection:
            prompt += injection

        result = _run_single_llm_call(
            prompt, step, flow, project_root, task_groups, retry_count,
        )
        _resolve_files_changed(step, project_root, baseline_hash)
        return result

    # --- Decide DAG parallel vs sequential execution ---
    want_dag = _should_use_dag(
        groups,
        total_loc,
        impl_config.group_loc_threshold,
        capability_mode=capability_mode,
    )
    # Short-circuit reason surfaced to the task plan panel when sequential
    # is chosen instead of DAG parallel. Remains None if sequential was
    # simply the natural outcome (e.g. low LOC, no dependencies).
    sequential_reason: str | None = None

    # Short-circuit 1: explicit use_worktree=False disables DAG parallel
    if want_dag and not impl_config.use_worktree:
        logger.info(
            "implement.use_worktree=False; skipping DAG parallel, using sequential path",
        )
        want_dag = False
        sequential_reason = "use_worktree=False"

    # Short-circuit 2: linear RelayPlan yields no parallel wave, skip DAG
    if want_dag:
        try:
            _reduced_preview = transitive_reduce(groups)
            _relay_preview = classify_chains(_reduced_preview)
            if _relay_plan_is_linear(_relay_preview):
                logger.info(
                    "RelayPlan is a linear chain (no fork, single root); "
                    "using sequential path instead of DAG parallel",
                )
                want_dag = False
                sequential_reason = "linear chain"
        except Exception:
            logger.debug(
                "Linear chain detection failed; proceeding with DAG parallel",
                exc_info=True,
            )

    if want_dag:
        if not has_commits(project_root):
            logger.warning(
                "Repository has no commits — falling back to sequential "
                "group-by-group execution instead of DAG parallel"
            )
            want_dag = False
            sequential_reason = "no commits"

    if want_dag:
        _display_task_plan(groups, "dag_parallel", total_loc, impl_config.group_loc_threshold)
        # Resume filtering for DAG parallel path
        prior_outputs: dict[str, Any] | None = None
        dag_groups = groups

        if step.inputs.get("resumed"):
            # Try step.outputs first (normal resume where state was saved)
            completed_groups_dag = set(
                g if isinstance(g, str) else g.get("group_id", "")
                for g in step.outputs.get("implemented_groups", [])
            )

            # Disaster recovery: check for surviving impl branches
            # that completed after the last state save.
            # Skip this scan if step.outputs already accounts for all groups.
            all_group_ids = {g.get("group_id", g.get("name", "unknown")) for g in groups}
            unaccounted = all_group_ids - completed_groups_dag

            if unaccounted:
                original_branch = get_current_branch(project_root)

                # If we're on an impl branch from a crashed run, restore
                impl_prefix = f"impl/{flow.flow_id}/"
                if original_branch.startswith(impl_prefix):
                    logger.warning(
                        "DAG disaster recovery: repo left on impl branch %s",
                        original_branch,
                    )
                    # Try common base branches directly (checkout - is unreliable
                    # after multi-thread checkout sequences)
                    restored = False
                    for candidate in ("master", "main", "develop"):
                        result = _run_git(project_root, "rev-parse", "--verify", candidate, check=False)
                        if result.returncode == 0:
                            _run_git(project_root, "checkout", candidate, check=False)
                            original_branch = candidate
                            restored = True
                            logger.info("DAG disaster recovery: restored to %s", candidate)
                            break
                    if not restored:
                        logger.error("DAG disaster recovery: cannot determine base branch, aborting")
                        step.error_message = (
                            f"Repo is on impl branch {original_branch} from a crashed run "
                            f"and no base branch (master/main) found. "
                            f"Please checkout the correct branch manually and retry."
                        )
                        return StepStatus.FAILED


                for gid in unaccounted:
                    branch = f"impl/{flow.flow_id}/{gid}"
                    try:
                        if has_new_commits(project_root, branch, original_branch):
                            completed_groups_dag.add(gid)
                            logger.info(
                                "DAG disaster recovery: found surviving branch %s",
                                branch,
                            )
                    except Exception:
                        pass

            if completed_groups_dag:
                dag_groups = _prune_recovered_dependencies(groups, completed_groups_dag)
                if not dag_groups:
                    logger.info("DAG parallel: all groups already completed on resume")
                    # Merge surviving branches before returning
                    result = _run_dag_parallel(
                        groups=[],
                        step=step,
                        flow=flow,
                        project_root=project_root,
                        task_description=task_description,
                        task_type=task_type,
                        injection=injection,
                        retry_count=retry_count,
                        root_cause_section=root_cause_section,
                        capability_mode=capability_mode,
                        prior_outputs={
                            "files_changed": list(step.outputs.get("files_changed", [])),
                            "tests_added": list(step.outputs.get("tests_added", [])),
                            "test_mapping": dict(step.outputs.get("test_mapping", {})),
                            "implemented_groups": list(completed_groups_dag),
                            "group_summaries": _prior_group_summaries(
                                step.outputs, completed_groups_dag,
                            ),
                        },
                    )
                    step.outputs["session_commits"] = _collect_session_commits(
                        project_root, baseline_hash,
                    )
                    _resolve_files_changed(step, project_root, baseline_hash)
                    return result
                prior_outputs = {
                    "files_changed": list(step.outputs.get("files_changed", [])),
                    "tests_added": list(step.outputs.get("tests_added", [])),
                    "test_mapping": dict(step.outputs.get("test_mapping", {})),
                    "implemented_groups": list(completed_groups_dag),
                    "group_summaries": _prior_group_summaries(
                        step.outputs, completed_groups_dag,
                    ),
                }
                logger.info(
                    "DAG parallel resume: skipping %d completed groups, running %d remaining",
                    len(completed_groups_dag), len(dag_groups),
                )

        result = _run_dag_parallel(
            groups=dag_groups,
            step=step,
            flow=flow,
            project_root=project_root,
            task_description=task_description,
            task_type=task_type,
            injection=injection,
            retry_count=retry_count,
            root_cause_section=root_cause_section,
            capability_mode=capability_mode,
            prior_outputs=prior_outputs,
        )
        step.outputs["session_commits"] = _collect_session_commits(
            project_root, baseline_hash,
        )
        _resolve_files_changed(step, project_root, baseline_hash)
        return result

    # --- Group-by-group execution ---
    _display_task_plan(
        groups,
        "sequential",
        total_loc,
        impl_config.group_loc_threshold,
        sequential_reason=sequential_reason,
    )
    logger.info("Executing %d task groups sequentially", len(groups))

    all_files_changed = []
    all_tests_added = []
    merged_test_mapping = {}
    previous_results: list[dict] = []
    implemented_group_ids: list[str] = []
    all_restricted_applied: list[dict] = []
    all_restricted_failed: list[dict] = []
    all_completion_statuses: list[str] = []
    all_incomplete_tasks: list[str] = []
    group_estimated_durations: list[float] = []
    # WHY: the aggregate ``summary`` string is a lossy join — a group summary
    # may itself contain "; ", so it cannot be split back apart, and it carries
    # no group identity at all. Renderers need the (group_id, summary) pairing,
    # so it is persisted structurally alongside the string.
    group_summary_entries: list[dict] = []

    # Check for resume state
    completed_groups = set()
    if step.inputs.get("resumed") and step.outputs.get("implemented_groups"):
        completed_groups = set(
            g if isinstance(g, str) else g.get("group_id", "")
            for g in step.outputs["implemented_groups"]
        )
        all_files_changed = list(step.outputs.get("files_changed", []))
        all_tests_added = list(step.outputs.get("tests_added", []))
        merged_test_mapping = dict(step.outputs.get("test_mapping", {}))
        # `estimated_test_duration` is persisted as the whole-suite estimate
        # (max across groups, see aggregation below). Seeding it as a single
        # element preserves the invariant: max(prior_max, new_group) ==
        # max(all_groups_so_far). Do not treat this as a per-group value.
        prior_est = step.outputs.get("estimated_test_duration")
        if prior_est is not None:
            group_estimated_durations.append(float(prior_est))
        # Carry forward previously-completed group IDs so they survive
        # across multiple retries (Bug fix: was starting empty, losing
        # completed groups on subsequent retries)
        implemented_group_ids = list(completed_groups)
        # Rebuild the structured list off ``implemented_group_ids`` (not off the
        # prior list directly) so the two stay index-aligned even when a prior
        # run predates this field or recorded a different order.
        prior_group_summaries = {
            str(e.get("group_id", "")): str(e.get("summary", "") or "")
            for e in (step.outputs.get("group_summaries") or [])
            if isinstance(e, dict)
        }
        group_summary_entries = [
            {"group_id": gid, "summary": prior_group_summaries.get(gid, "")}
            for gid in implemented_group_ids
        ]

    for group in groups:
        group_id = group.get("group_id", group.get("name", "unknown"))
        if group_id in completed_groups:
            logger.info("Skipping already-completed group: %s", group_id)
            # Reconstruct minimal previous result for context
            previous_results.append({
                "group_id": group_id,
                "files_changed": [],
                "summary": "(previously completed)",
            })
            continue

        logger.info("Implementing group: %s", group_id)

        # Build previous results context
        prev_ctx = "No previous groups." if not previous_results else json.dumps(
            previous_results, indent=2, ensure_ascii=False,
        )

        prompt = _group_prompt_template(capability_mode).format(
            task_description=task_description,
            task_type=task_type,
            current_group=json.dumps(group, indent=2, ensure_ascii=False),
            previous_results=prev_ctx,
            root_cause_section=root_cause_section,
        )
        if injection:
            prompt += injection

        try:
            # Use group-specific step_id so each group has its own history
            # file, preventing mixed conversations on retry
            group_step_id = f"{step.step_id}_{group_id}"
            caller = LLMCaller(
                project_root,
                flow_id=flow.flow_id,
                step_id=group_step_id,
                step_type=step.step_type.value,
                external_attempt=retry_count,
                stream_prefix=f'[{group_id}] ',
                fix_iteration=step.inputs.get("fix_iteration", 0),
            )
            response = caller.call(
                prompt=prompt,
                json_mode="two_phase",
                json_schema_hint='{"files_changed": [], "tests_added": [], "estimated_test_duration": 120, "test_mapping": {}, "summary": "...", "completion_status": "complete|partial|failed", "incomplete_tasks": [], "restricted_edits": [{"file_path": "...", "old_string": "...", "new_string": "..."}]}',
            )
            result = parse_json_response(response, required_keys=[])
        except LLMCallError as e:
            logger.exception("Group %s LLM call failed", group_id)
            step.error_message = f"Implementation failed at group {group_id}: {str(e)}"
            return StepStatus.FAILED
        except Exception as e:
            logger.exception("Group %s failed", group_id)
            step.error_message = f"Implementation failed at group {group_id}: {str(e)}"
            return StepStatus.FAILED

        group_files = result.get("files_changed", []) if result else []
        group_tests = result.get("tests_added", []) if result else []
        group_mapping = result.get("test_mapping", {}) if result else {}
        group_summary = result.get("summary", "") if result else ""
        group_estimated_duration = _sanitize_estimated_test_duration(
            result.get("estimated_test_duration") if result else None
        )

        # Apply restricted edits for this group (Bug A)
        restricted_edits = result.get("restricted_edits", []) if result else []
        if restricted_edits:
            applied, failed_edits = _apply_restricted_edits(restricted_edits, project_root)
            all_restricted_applied.extend(applied)
            all_restricted_failed.extend(failed_edits)
            for edit in applied:
                fp = edit.get("file_path", "")
                if fp and fp not in group_files:
                    group_files.append(fp)
            if applied:
                logger.info("Group %s: applied %d restricted edits", group_id, len(applied))
            if failed_edits:
                logger.warning("Group %s: failed %d restricted edits", group_id, len(failed_edits))

        # Track per-group completion status (Bug B)
        group_completion = result.get("completion_status", "complete") if result else "complete"
        group_incomplete = result.get("incomplete_tasks", []) if result else []
        all_completion_statuses.append(group_completion)
        all_incomplete_tasks.extend(group_incomplete)

        all_files_changed.extend(group_files)
        all_tests_added.extend(group_tests)
        merged_test_mapping.update(group_mapping)
        implemented_group_ids.append(group_id)
        if group_estimated_duration is not None:
            group_estimated_durations.append(group_estimated_duration)

        previous_results.append({
            "group_id": group_id,
            "files_changed": group_files,
            "summary": group_summary,
        })
        group_summary_entries.append({
            "group_id": group_id,
            "summary": group_summary,
        })

        # Persist incremental progress.
        # Use max() across groups: each group reports a whole-suite estimate
        # (see IMPLEMENT_GROUP_PROMPT), so sum()-ing would inflate the
        # timeout by N× for N groups.
        step.outputs["files_changed"] = all_files_changed
        step.outputs["tests_added"] = all_tests_added
        step.outputs["test_mapping"] = merged_test_mapping
        step.outputs["implemented_groups"] = implemented_group_ids
        step.outputs["group_summaries"] = group_summary_entries
        step.outputs["estimated_test_duration"] = (
            max(group_estimated_durations) if group_estimated_durations else None
        )

    # Final outputs
    step.outputs["files_changed"] = all_files_changed
    step.outputs["tests_added"] = all_tests_added
    step.outputs["test_mapping"] = merged_test_mapping
    step.outputs["implemented_groups"] = implemented_group_ids
    step.outputs["group_summaries"] = group_summary_entries
    step.outputs["estimated_test_duration"] = (
        max(group_estimated_durations) if group_estimated_durations else None
    )

    # Restricted edits aggregation
    if all_restricted_applied:
        step.outputs["restricted_edits_applied"] = all_restricted_applied
    if all_restricted_failed:
        step.outputs["restricted_edits_failed"] = all_restricted_failed

    # Compute overall completion status
    if "failed" in all_completion_statuses:
        overall_status = "failed"
    elif "partial" in all_completion_statuses:
        overall_status = "partial"
    else:
        overall_status = "complete"

    step.outputs["completion_status"] = overall_status
    step.outputs["incomplete_tasks"] = all_incomplete_tasks
    step.outputs["summary"] = "; ".join(
        r.get("summary", "") for r in previous_results if r.get("summary")
    )

    # Resolve files_changed from git diff (ground truth)
    _resolve_files_changed(step, project_root, baseline_hash)

    if overall_status == "failed":
        step.error_message = "LLM reported implementation failed"
        return StepStatus.FAILED
    elif overall_status == "partial":
        logger.warning(
            "Implementation partially completed. Incomplete tasks: %s",
            all_incomplete_tasks,
        )
        return StepStatus.PARTIAL

    return StepStatus.COMPLETED


def _display_task_plan(
    groups: list[dict],
    strategy: str,
    total_loc: int,
    threshold: int,
    sequential_reason: str | None = None,
) -> None:
    """Display implementation task plan with execution strategy.

    Wraps the call in try/except so display failures never block execution.
    For dag_parallel strategy, computes RelayPlan to show execution topology.
    ``sequential_reason`` is surfaced in the strategy line when the
    sequential path was chosen by a short-circuit rule rather than by
    the natural small-LOC fallback.
    """
    try:
        from ..formatters import TaskFormatter
        from ..display import get_console

        console = get_console()
        formatter = TaskFormatter(console=console)

        # Compute relay plan for DAG parallel topology display
        relay_plan = None
        if strategy == "dag_parallel" and len(groups) > 1:
            try:
                reduced = transitive_reduce(groups)
                relay_plan = classify_chains(reduced)
            except Exception:
                logger.debug("Could not compute relay plan for display", exc_info=True)

        console.print(formatter.format_implement_plan(
            task_groups=groups,
            execution_strategy=strategy,
            total_loc=total_loc,
            loc_threshold=threshold,
            relay_plan=relay_plan,
            sequential_reason=sequential_reason,
        ))
    except Exception:
        logger.debug("Could not render implementation plan", exc_info=True)


def _extract_sorted_groups(task_groups) -> list[dict]:
    """Extract and sort task groups by group_order."""
    if not isinstance(task_groups, list):
        return []
    groups = [g for g in task_groups if isinstance(g, dict)]
    groups.sort(key=lambda g: g.get("group_order", 0))
    return groups


def _compute_total_loc(groups: list[dict]) -> int:
    """Sum estimated_loc across all tasks in all groups.

    Tasks missing the ``estimated_loc`` field default to 50 LOC each,
    providing a sensible fallback for plans generated before the field
    was introduced.
    """
    total = 0
    for g in groups:
        for task in g.get("tasks", []):
            if isinstance(task, dict):
                total += task.get("estimated_loc", 50)
    return total


def _should_use_dag(
    groups: list[dict],
    total_loc: int = 0,
    loc_threshold: int = 300,
    *,
    capability_mode: bool = False,
) -> bool:
    """Check whether to enable DAG parallel execution path.

    Returns True when there are multiple groups AND — for granular/legacy
    plans — the total estimated LOC exceeds the configured threshold. Small
    multi-group tasks are better served by a single LLM call (the LOC-merge
    path in ``implement_handler``).

    Args:
        groups: Sorted group dicts.
        total_loc: Pre-computed total estimated LOC across all groups.
        loc_threshold: Configured LOC threshold from ImplementConfig.
        capability_mode: True when PLAN emitted coarse capability groups. Those
            carry no per-task ``estimated_loc``, so ``total_loc`` is always 0
            and the LOC comparison below would answer "not worth parallelising"
            for every plan; group count and topology decide instead.
    """
    if len(groups) <= 1:
        return False
    if capability_mode:
        return True
    # When total_loc is available and below threshold, prefer single call
    if total_loc > 0 and total_loc <= loc_threshold:
        return False
    return True


def _make_execute_fn(
    project_root: Path,
    original_branch: str,
    flow: FlowInstance,
    step: Step,
    task_description: str,
    task_type: str,
    injection: str | None,
    retry_count: int,
    group_agent_info: dict[str, tuple[str, str | None]] | None = None,
    root_cause_section: str = "",
    capability_mode: bool = False,
) -> Callable[[dict, dict[str, GroupResult], RelayContext], GroupResult]:
    """Build the execute_fn closure for relay-based DAG parallel execution.

    The returned callable acquires a worktree via the relay strategy
    (reuse predecessor / fork / create new), handles convergence merges,
    runs the LLM agent, commits changes, and returns a GroupResult.

    ``group_agent_info`` (when provided) is a shared ``group_id -> (agent_name,
    model_name|None)`` map recording each group's latest known agent/model as
    reported by the in-worktree ``LLMCaller``. The closure both updates it and
    emits live ``running`` ``group_status`` records carrying that agent (then
    "agent · model"); ``_run_dag_parallel``'s coarse status sink reads it back
    so the ``completed`` / ``failed`` records carry the final agent/model too.

    ``capability_mode`` selects the per-group prompt: a coarse capability group
    carries no per-task enumeration, so it must be told to decompose itself
    rather than to work through a task list it does not have.
    """
    git_lock = threading.Lock()

    def execute_fn(
        group: dict,
        deps_results: dict[str, GroupResult],
        relay_context: RelayContext,
    ) -> GroupResult:
        group_id = group.get("group_id", group.get("name", "unknown"))
        branch_name = f"impl/{flow.flow_id}/{group_id}"
        worktree_path: Path | None = None

        try:
            # Step 1: Acquire worktree based on relay_context
            if relay_context.worktree_path is not None:
                # Relay: reuse predecessor's worktree and branch directly
                worktree_path = relay_context.worktree_path
                branch_name = relay_context.branch_name or branch_name
                logger.info(
                    "DAG relay: group %s reusing worktree %s (branch %s)",
                    group_id, worktree_path, branch_name,
                )
            elif relay_context.is_fork and relay_context.fork_source_branch:
                # Fork: create new branch from predecessor's branch + new worktree
                with git_lock:
                    force_cleanup_worktree(project_root, branch_name)
                    _run_git(project_root, "branch", "-D", branch_name, check=False)
                    worktree_path = fork_worktree(
                        project_root, relay_context.fork_source_branch, branch_name,
                    )
                logger.info(
                    "DAG fork: group %s forked from %s to %s (worktree %s)",
                    group_id, relay_context.fork_source_branch,
                    branch_name, worktree_path,
                )
            else:
                # Root node (or no relay_plan): create new branch + worktree
                with git_lock:
                    force_cleanup_worktree(project_root, branch_name)
                    _run_git(project_root, "branch", "-D", branch_name, check=False)
                    _run_git(project_root, "branch", branch_name, original_branch)
                    logger.info(
                        "DAG root: created branch %s from %s",
                        branch_name, original_branch,
                    )
                    worktree_path = create_worktree(project_root, branch_name)
                logger.info(
                    "DAG root: group %s created worktree %s",
                    group_id, worktree_path,
                )

            # Step 2: Convergence merge — merge secondary predecessor branches
            if relay_context.convergence_merges:
                with git_lock:
                    for sec_branch in relay_context.convergence_merges:
                        logger.info(
                            "DAG convergence: merging %s into worktree at %s",
                            sec_branch, worktree_path,
                        )
                        merge_result = _run_git(
                            worktree_path, "merge", sec_branch, "--no-edit",
                            check=False,
                        )
                        if merge_result.returncode != 0:
                            stderr = merge_result.stderr.strip()
                            is_conflict = "CONFLICT" in (
                                merge_result.stdout + merge_result.stderr
                            )
                            if is_conflict:
                                logger.warning(
                                    "DAG convergence: conflict merging %s, "
                                    "attempting LLM resolution",
                                    sec_branch,
                                )
                                conflict_files = get_conflicting_files(worktree_path)
                                if conflict_files:
                                    resolved = _resolve_convergence_conflicts(
                                        worktree_path, conflict_files,
                                        task_description, deps_results,
                                    )
                                    if not resolved:
                                        _run_git(
                                            worktree_path, "merge", "--abort",
                                            check=False,
                                        )
                                        return GroupResult.failed(
                                            group_id,
                                            f"Convergence merge conflict with "
                                            f"{sec_branch} could not be resolved",
                                        )
                                else:
                                    _run_git(
                                        worktree_path, "merge", "--abort",
                                        check=False,
                                    )
                                    return GroupResult.failed(
                                        group_id,
                                        f"Convergence merge failed: {sec_branch}: "
                                        f"{stderr}",
                                    )
                            else:
                                _run_git(
                                    worktree_path, "merge", "--abort",
                                    check=False,
                                )
                                return GroupResult.failed(
                                    group_id,
                                    f"Convergence merge failed: {sec_branch}: "
                                    f"{stderr}",
                                )

            # Step 3: Build previous_results context from deps_results
            if deps_results:
                prev_results = [
                    {
                        "group_id": did,
                        "files_changed": dr.files_changed,
                        "summary": dr.summary,
                    }
                    for did, dr in deps_results.items()
                ]
                prev_ctx = json.dumps(prev_results, indent=2, ensure_ascii=False)
            else:
                prev_ctx = "No previous groups."

            # Step 4: Format prompt
            prompt = _group_prompt_template(capability_mode).format(
                task_description=task_description,
                task_type=task_type,
                current_group=json.dumps(group, indent=2, ensure_ascii=False),
                previous_results=prev_ctx,
                root_cause_section=root_cause_section,
            )
            if injection:
                prompt += injection

            # Inject runtime context for worktree isolation
            from ..context_builder import get_runtime_context_injection
            runtime_ctx = get_runtime_context_injection(worktree_path, project_root)
            if runtime_ctx:
                prompt += runtime_ctx

            # Step 5: Run LLM in the worktree
            # Use group-specific step_id so each group has its own history file
            group_step_id = f"{step.step_id}_{group_id}"

            # Restore history from main repo so retry context injection works
            _restore_history_to_worktree(project_root, worktree_path, flow.flow_id)

            # Live agent/model relay: as the in-worktree LLMCaller selects the
            # agent for each attempt (and as it parses the actual model name),
            # write a "running" group_status record into the MAIN repo's step
            # jsonl so the web console's "running in worktree" card shows the
            # group's real agent the moment it starts — and upgrades to
            # "agent · model" once known. Retries/rotations report their own
            # real agent (the caller fires this per attempt), never a stale one.
            # Best-effort: a write failure must not break the worker thread.
            def _on_agent_change(
                agent_name: str,
                model_name: str | None,
                _group_id: str = group_id,
            ) -> None:
                try:
                    if group_agent_info is not None:
                        group_agent_info[_group_id] = (agent_name, model_name)
                    record_group_status(
                        project_root,
                        flow.flow_id,
                        step.step_id,
                        "implement",
                        _group_id,
                        GROUP_STATUS_RUNNING,
                        agent_name=agent_name,
                        model_name=model_name,
                    )
                except Exception:  # pragma: no cover - defensive in worker thread
                    logger.debug(
                        "group %s agent/model status relay failed",
                        _group_id, exc_info=True,
                    )

            caller = LLMCaller(
                worktree_path,
                flow_id=flow.flow_id,
                step_id=group_step_id,
                step_type=step.step_type.value,
                external_attempt=retry_count,
                stream_prefix=f'[{group_id}] ',
                fix_iteration=step.inputs.get("fix_iteration", 0),
                on_agent_change=_on_agent_change,
            )
            response = caller.call(
                prompt=prompt,
                json_mode="two_phase",
                json_schema_hint='{"files_changed": [], "tests_added": [], "estimated_test_duration": 120, "test_mapping": {}, "summary": "...", "completion_status": "complete|partial|failed", "incomplete_tasks": [], "restricted_edits": [{"file_path": "...", "old_string": "...", "new_string": "..."}]}',
            )
            result = parse_json_response(response, required_keys=[])

            # Step 6: Commit changes in the worktree (only if there are changes)
            _run_git(worktree_path, "add", "-A", check=False)
            status_result = _run_git(worktree_path, "status", "--porcelain", check=False)
            has_changes = bool(status_result.stdout.strip())

            if has_changes:
                commit_result = _run_git(
                    worktree_path, "commit",
                    "-m", f"impl: group {group_id}",
                    check=False,
                )
                if commit_result.returncode != 0:
                    logger.warning(
                        "DAG: commit failed for %s: %s",
                        group_id, commit_result.stderr.strip(),
                    )
            else:
                logger.info("DAG: no changes in worktree for group %s, skipping commit", group_id)

            # Step 7: Build GroupResult
            files_changed = result.get("files_changed", []) if result else []
            tests_added = result.get("tests_added", []) if result else []
            test_mapping = result.get("test_mapping", {}) if result else {}
            summary = result.get("summary", "") if result else ""
            completion_status = result.get("completion_status", "complete") if result else "complete"
            incomplete_tasks = result.get("incomplete_tasks", []) if result else []
            restricted_edits = result.get("restricted_edits", []) if result else []
            estimated_test_duration = _sanitize_estimated_test_duration(
                result.get("estimated_test_duration") if result else None
            )

            # For relay/fork nodes, always preserve the branch name so downstream
            # groups and leaf merge can locate the accumulated commits.
            if has_changes:
                effective_branch = branch_name
            elif relay_context.worktree_path is not None or relay_context.is_fork:
                effective_branch = branch_name
            else:
                effective_branch = ""

            return GroupResult(
                group_id=group_id,
                status="completed",
                files_changed=files_changed,
                tests_added=tests_added,
                test_mapping=test_mapping,
                summary=summary,
                branch_name=effective_branch,
                worktree_path=worktree_path,
                completion_status=completion_status,
                incomplete_tasks=incomplete_tasks,
                restricted_edits=restricted_edits,
                estimated_test_duration=estimated_test_duration,
            )

        except subprocess.TimeoutExpired as e:
            wt_display = str(worktree_path) if worktree_path else "<not yet created>"
            msg = (
                f"Git worktree creation timed out for group {group_id} "
                f"(branch: {branch_name}, worktree: {wt_display}, repo: {project_root}). "
                f"Possible causes: git lock files in the repo, very large repository, "
                f"or a git process waiting for interactive input. "
                f"Check for stale .git/worktrees/*/locked files or index.lock."
            )
            logger.error(msg)
            result = GroupResult.failed(group_id, f"{msg} Original error: {e}")
            result.worktree_path = worktree_path  # preserve for history salvaging
            return result
        except (LLMCallError, Exception) as e:
            logger.exception("DAG: group %s failed", group_id)
            result = GroupResult.failed(group_id, str(e))
            result.worktree_path = worktree_path  # preserve for history salvaging
            return result

    return execute_fn


def _resolve_convergence_conflicts(
    worktree_path: Path,
    conflict_files: list[str],
    task_description: str,
    deps_results: dict[str, GroupResult],
) -> bool:
    """Resolve merge conflicts at a convergence point using LLM.

    Reads each conflicting file, sends it to the LLM with context about
    the converging groups, and writes back the resolved content.

    Args:
        worktree_path: Path to the worktree where the merge is happening
        conflict_files: List of conflicting file paths
        task_description: Overall task description for context
        deps_results: Results from predecessor groups for context

    Returns:
        True if all conflicts resolved and merge committed, False otherwise
    """
    try:
        from ..llm_caller import LLMCaller
    except ImportError:
        logger.warning("LLMCaller not available for convergence conflict resolution")
        return False

    # Build context about what each predecessor did
    group_context_parts: list[str] = []
    for gid, result in deps_results.items():
        group_context_parts.append(
            f"- Group {gid}: {result.summary} (files: {', '.join(result.files_changed)})"
        )
    group_context = "\n".join(group_context_parts) if group_context_parts else "No group context."

    for filepath in conflict_files:
        full_path = worktree_path / filepath
        if not full_path.exists():
            logger.warning("Convergence conflict file not found: %s", filepath)
            return False

        try:
            content = full_path.read_text(encoding="utf-8")
        except Exception:
            logger.warning("Could not read convergence conflict file: %s", filepath)
            return False

        prompt = (
            "You are resolving a git merge conflict at a convergence point where "
            "multiple parallel implementation groups are being merged together.\n\n"
            f"## Task Description\n{task_description}\n\n"
            f"## Group Summaries\n{group_context}\n\n"
            f"## Conflicting File: {filepath}\n\n"
            f"```\n{content}\n```\n\n"
            "Output ONLY the fully resolved file content with no conflict markers "
            "(no <<<<<<< / ======= / >>>>>>>). Do not add any explanation."
        )

        try:
            caller = LLMCaller(worktree_path, step_type="convergence_conflict")
            resolved_content = caller.call(prompt=prompt)
        except Exception as e:
            logger.warning(
                "LLM convergence conflict resolution failed for %s: %s", filepath, e,
            )
            return False

        # Verify no conflict markers remain
        if "<<<<<<<" in resolved_content or ">>>>>>>" in resolved_content:
            logger.warning(
                "LLM output still contains conflict markers for %s", filepath,
            )
            return False

        # Write resolved content
        try:
            full_path.write_text(resolved_content, encoding="utf-8")
            _run_git(worktree_path, "add", filepath, check=False)
        except Exception as e:
            logger.warning("Failed to write resolved content for %s: %s", filepath, e)
            return False

    # Complete the merge commit
    commit_result = _run_git(worktree_path, "commit", "--no-edit", check=False)
    if commit_result.returncode != 0:
        logger.warning(
            "Convergence merge commit failed: %s", commit_result.stderr.strip(),
        )
        return False

    logger.info("Convergence merge conflicts resolved successfully")
    return True


def _clean_index_after_failed_merge(project_root: Path, branch: str) -> None:
    """Guarantee the index is free of unmerged entries on every failure exit.

    ``git merge --abort`` silently no-ops when no ``MERGE_HEAD`` exists
    (e.g. the merge was rejected pre-flight because the index already had
    stage>0 entries from an earlier crashed run). Without this helper, the
    leaf-merge failure paths can return with the same garbage in the index
    that caused the failure — making the next leaf merge fail identically
    and locking the DAG in a wedged state across retries.

    Strategy: if a merge is in progress, abort it first; afterwards run
    ``recover_stale_unmerged_paths`` so any residual stage>0 entries whose
    working-tree blob already matches HEAD get ``git add``-ed away. This
    mirrors what the DAG entry preflight does, applied locally so a single
    bad leaf doesn't poison subsequent leaves within the same run.
    """
    if merge_in_progress(project_root):
        _run_git(project_root, "merge", "--abort", check=False)
    leftover = detect_unmerged_paths(project_root)
    if not leftover:
        return
    recovered, unresolved = recover_stale_unmerged_paths(project_root)
    if recovered:
        logger.warning(
            "Leaf merge cleanup for %s: cleared %d stale unmerged path(s) "
            "post-failure: %s",
            branch, len(recovered), recovered,
        )
    if unresolved:
        logger.error(
            "Leaf merge cleanup for %s: %d unmerged path(s) could not be "
            "auto-resolved and remain in the index — next leaf merge may "
            "fail until manual resolution: %s",
            branch, len(unresolved), unresolved,
        )


def _merge_leaf_branch(
    project_root: Path,
    branch: str,
    original_branch: str,
    task_description: str,
    group_summaries: list[dict],
    flow_id: str | None = None,
    merge_step_id: str | None = None,
) -> bool:
    """Merge a leaf branch back to original_branch with robust recovery.

    Layered protections (each must hold; the next layer covers the previous'
    edge cases):

    1. Pre-merge ``git stash push --include-untracked`` — untracked files in
       the main repo (e.g. discovery-step artefacts) no longer block the
       merge with ``"untracked working tree files would be overwritten"``.
    2. ``resolve_merge_conflicts_with_context`` LLM resolution on real
       content conflicts (existing behavior; 3 retries).
    3. ``_take_theirs_fallback`` deterministic fallback after the LLM
       exhausts retries: ``git checkout --theirs`` for every conflict file,
       commit, audit via ``IssueManager``. Always preserves the leaf
       branch's commits (which encapsulate the DAG implement output);
       only discards the master-pre-merge version of conflicting paths.
    4. Post-merge ``git stash pop``; any non-clean pop is recovered through
       the shared ``resolve_stashpop_safely`` (same entry point as the
       ``luo merge`` fast path). It archives the still-live stash's full
       content to ``tianluo/worktrees/.archive`` BEFORE any disposition — case b
       (untracked-collision, e.g. concurrent discovery issue files) gives
       way to the merged tree and is never destroyed; case a (real 3-way
       tracked) is reconciled by an injected LLM resolver (symmetric with
       layer 2's merge-body resolution), with both sides archived first and a
       deterministic take-ours fallback when the LLM is unavailable. The
       stash is dropped only after archival is proven; if it cannot be, the
       live stash is kept for manual recovery. ``IssueManager`` audits the
       archive manifest so the operator can actually restore.
    """
    current = get_current_branch(project_root)
    if current != original_branch:
        _run_git(project_root, "checkout", original_branch)

    # Layer 1: pre-merge stash (include untracked so discovery/analyze/plan
    # artefacts in the main repo don't block FF/3-way merge). The label is
    # fixed here and reused for every pop below so we always operate on this
    # exact stash by ref, never a bare ``stash@{0}`` that a concurrent stash
    # could have displaced.
    stash_label = f"se3-pre-leaf-merge-{branch}"
    stash_result = _run_git(
        project_root, "stash", "push", "--include-untracked",
        "-m", stash_label,
        check=False,
    )
    stashed = (
        stash_result.returncode == 0
        and "No local changes" not in stash_result.stdout
    )

    try:
        merge_ok = _attempt_merge_with_resolution(
            project_root,
            branch=branch,
            task_description=task_description,
            group_summaries=group_summaries,
            flow_id=flow_id,
            merge_step_id=merge_step_id,
        )
    except BaseException:
        # Defensive: if the merge attempt raises (subprocess crash,
        # KeyboardInterrupt during a long LLM call, etc.), make a
        # best-effort to:
        #   1. abort any in-progress merge and clear stale unmerged-index
        #      entries (otherwise the next DAG run inherits a wedged index
        #      and ``git stash`` cannot package stage>0 entries, so every
        #      future leaf merge fails with "Merging is not possible
        #      because you have unmerged files");
        #   2. restore the stashed working tree so the user isn't left with
        #      a dangling stash@{0} and an empty-looking working tree.
        #
        # Each cleanup step is wrapped: if a step also raises (e.g.,
        # subprocess.TimeoutExpired in a degraded environment), the inner
        # exception must NOT shadow the original cause that the caller /
        # user needs to see.
        try:
            _clean_index_after_failed_merge(project_root, branch)
        except Exception:
            logger.warning(
                "index cleanup during exception path also failed; "
                "unmerged entries may persist — original exception preserved",
            )
        if stashed:
            try:
                _pop_stash_by_label(project_root, stash_label)
            except Exception:
                logger.warning(
                    "stash pop during exception cleanup also failed; "
                    "dangling pre-leaf-merge stash may remain — original "
                    "exception preserved",
                )
        raise

    if not merge_ok:
        # Merge irrecoverably failed (non-conflict failure or take-theirs
        # commit itself failed). The failure paths inside
        # ``_attempt_merge_with_resolution`` already called
        # ``_clean_index_after_failed_merge``; restore stash and bail.
        # Branch cleanup is protected by the reachability check upstream.
        if stashed:
            _pop_stash_by_label(project_root, stash_label)
        return False

    # Merge succeeded (whether via LLM or take-theirs); restore stashed
    # state by popping the EXACT labeled stash (never a bare ``stash@{0}``).
    if stashed:
        pop_result = _pop_stash_by_label(project_root, stash_label)
        if pop_result.returncode != 0:
            # Non-clean pop: hand the whole recovery to the shared
            # no-data-loss entry point so this leaf-back path and the
            # ``luo merge`` fast path behave identically. It archives the
            # still-live stash's full content (untracked collisions that
            # were never checked out — case b, plus tracked working-tree
            # changes about to be take-ours'd over — case a) to
            # ``tianluo/worktrees/.archive`` BEFORE any disposition, gives case b
            # away to the merged tree (never destroying the concurrent new
            # files, e.g. discovery's issue files), take-ours' case a, and
            # drops the stash only after archival is proven. ``stash_label``
            # is the same one used at push time so the live stash resolves.
            timestamp = time.strftime("%Y%m%d-%H%M%S")
            # case a (real 3-way tracked) stash-pop conflicts get the same
            # LLM-as-editor treatment as this path's merge body
            # (resolve_merge_conflicts_with_context); the resolver falls back
            # to deterministic take-ours when the LLM is unavailable, and both
            # discarded sides are archived first so any choice is reversible.
            outcome = _resolve_stashpop_safely(
                project_root, stash_label, pop_result, timestamp=timestamp,
                conflict_resolver=_make_llm_stashpop_resolver(
                    flow_id=flow_id,
                    step_id=merge_step_id,
                    context=(
                        f"DAG leaf-back merge of {branch}. "
                        f"Task: {task_description}"
                    ),
                ),
            )
            _record_stashpop_takeours_event(
                project_root, branch, outcome.archived, flow_id,
                archive_failed=outcome.archive_failed,
                unresolved_files=outcome.unresolved_files,
            )
            if outcome.archive_failed:
                # Recovery did not finalize: the live stash was kept and the
                # index may still hold unmerged case-a paths. Reporting success
                # would let the DAG advance (cleanup → test → self_check, or the
                # next leaf merge) over an unreconciled main repo — and a
                # stage>0 index wedges every subsequent ``git stash``. Signal
                # failure so the step status reflects it; the leaf commit itself
                # already landed, and the discarded content is archived +
                # audited, so nothing is lost by halting here for manual fixup.
                logger.error(
                    "Leaf merge of %s landed, but post-merge stash-pop "
                    "recovery did NOT finalize (%d unmerged path(s); live "
                    "stash kept). Reporting failure for manual intervention.",
                    branch, len(outcome.unresolved_files),
                )
                return False

    logger.info("Leaf merge succeeded: %s -> %s", branch, original_branch)
    return True


def _attempt_merge_with_resolution(
    project_root: Path,
    branch: str,
    task_description: str,
    group_summaries: list[dict],
    flow_id: str | None,
    merge_step_id: str | None,
) -> bool:
    """Run ``git merge`` and recover from conflicts.

    Order: 3-way merge → LLM resolution → take-theirs deterministic
    fallback → abort.  Returns True if a merge commit landed on HEAD,
    False if even the take-theirs fallback could not commit (extremely
    rare — disk full, git lock contention, etc.) or if the merge failed
    for a non-conflict reason that ``--abort`` reverted.
    """
    # Validate the target branch ref BEFORE attempting the merge. Under IO
    # stall or a concurrent branch deletion the leaf ref can be missing, and
    # ``git merge <missing>`` reports the opaque
    # "merge: <branch> - not something we can merge" error. Probe with
    # ``rev-parse --verify`` so the failure is diagnosable and no in-progress
    # merge state is left behind (nothing to abort — we never ran the merge).
    ref_check = _run_git(
        project_root, "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}",
        check=False,
    )
    if ref_check.returncode != 0:
        logger.error(
            "Leaf merge aborted: target branch ref %r does not resolve to a "
            "commit (git rev-parse --verify failed: %s). Skipping merge to "
            "avoid an opaque 'not something we can merge' error.",
            branch, ref_check.stderr.strip(),
        )
        return False

    result = _run_git(
        project_root, "merge", branch, "--no-edit",
        "-m", f"Merge leaf branch {branch}",
        check=False,
    )
    if result.returncode == 0:
        return True

    is_conflict = "CONFLICT" in (result.stdout + result.stderr)
    if not is_conflict:
        logger.error(
            "Leaf merge failed (non-conflict): %s: %s",
            branch, result.stderr.strip(),
        )
        _clean_index_after_failed_merge(project_root, branch)
        return False

    logger.warning("Leaf merge conflict: %s, attempting LLM resolution", branch)
    conflict_files = get_conflicting_files(project_root)
    if not conflict_files:
        _clean_index_after_failed_merge(project_root, branch)
        return False

    if resolve_merge_conflicts_with_context(
        project_root, conflict_files, task_description,
        group_summaries,
        flow_id=flow_id, step_id=merge_step_id,
    ):
        logger.info("Leaf merge conflicts resolved via LLM: %s", branch)
        return True

    logger.warning(
        "Leaf merge LLM resolution exhausted; falling back to take-theirs: %s",
        branch,
    )
    if _take_theirs_fallback(
        project_root, branch, conflict_files, flow_id,
    ):
        return True

    logger.error(
        "Leaf merge: take-theirs fallback also failed; aborting merge: %s",
        branch,
    )
    _clean_index_after_failed_merge(project_root, branch)
    return False


def _take_theirs_fallback(
    project_root: Path,
    branch: str,
    conflict_files: list[str],
    flow_id: str | None,
) -> bool:
    """Deterministic fallback when LLM conflict resolution is exhausted.

    NOTE: This is the DAG leaf-merge path — NOT the ``luo merge`` command
    path. The ``luo merge`` command has had every take-theirs route
    removed (see merge strategy refactor: ``fast`` / ``safe`` / ``strict``
    all resolve via LLM-as-editor or escalate to human, never via
    take-theirs). The leaf-merge fallback here is preserved because it
    sits inside the DAG implement loop and has its own success/failure
    contract independent of ``luo merge``; reworking that loop is out
    of scope for the merge refactor.

    For every conflict file, ``git checkout --theirs`` (the leaf branch's
    version) and stage it, then complete the merge commit. Records an
    audit issue via ``IssueManager`` so the operator can see which files
    were resolved deterministically.

    Rationale for "theirs": the leaf branch encapsulates the DAG implement
    output — the commits we MUST preserve. ``ours`` is the master pre-
    merge state, which for the typical use case is upstream content the
    user is willing to let the implementation override (otherwise they
    wouldn't have run implement). Preserving leaf is the safer default
    for SE3's authoring workflow.

    Returns True only if the commit succeeded; False signals the caller
    to abort.
    """
    for filepath in conflict_files:
        _run_git(project_root, "checkout", "--theirs", "--", filepath, check=False)
        _run_git(project_root, "add", filepath, check=False)
    commit_result = _run_git(project_root, "commit", "--no-edit", check=False)
    if commit_result.returncode != 0:
        logger.error(
            "take-theirs fallback commit failed for %s: %s",
            branch, commit_result.stderr.strip(),
        )
        return False
    _record_take_theirs_event(project_root, branch, conflict_files, flow_id)
    return True


def _is_branch_reachable_from(
    project_root: Path,
    branch: str,
    target_branch: str,
) -> bool:
    """True iff every commit on *branch* is also reachable from *target_branch*.

    Equivalent to ``git merge-base --is-ancestor branch target_branch``.
    Used by the DAG cleanup loop as the gate for branch deletion: only
    branches whose commits have safely landed on the target (i.e. the
    merge succeeded) are eligible for ``git branch -D``. A return of
    False protects the branch — preserving its commits even if the
    surrounding merge bookkeeping went wrong.

    Returns False on git error (branch missing, indeterminate ancestry)
    so deletion fails closed.
    """
    result = _run_git(
        project_root, "merge-base", "--is-ancestor", branch, target_branch,
        check=False,
    )
    # rc=0: branch is ancestor (every commit reachable from target).
    # rc=1: branch is not ancestor.
    # rc=128 or other: error (e.g. branch ref missing). Fail closed.
    return result.returncode == 0


# Module-scoped re-exports of the shared stash-pop helpers. Both
# implement step and ``luo merge`` (fast strategy) share the same
# behavior (see src/tianluo/engine/stash_utils.py).


def _record_take_theirs_event(
    project_root: Path,
    branch: str,
    conflict_files: list[str],
    flow_id: str | None,
) -> None:
    """File an audit issue when the take-theirs fallback fires.

    Audit-only — failures here are logged but never block the merge,
    because losing one audit row is acceptable; losing commits is not.
    """
    try:
        from ..issue_manager import IssueManager
    except ImportError:
        return
    try:
        IssueManager(project_root).create(
            title=f"DAG leaf merge fallback: take-theirs on {branch}",
            description=(
                f"Flow: {flow_id or '<unknown>'}\n"
                f"Branch: {branch}\n\n"
                "LLM conflict resolution exhausted retries; fell back to "
                "`git checkout --theirs` for all conflict files (i.e., "
                "accepted the leaf branch's version verbatim).\n\n"
                "Conflict files (now holding leaf branch's version):\n  - "
                + "\n  - ".join(conflict_files)
            ),
            priority="medium",
            type="task",
            tags=["merge-fallback", "audit"],
            source="system",
        )
    except Exception as e:
        logger.warning("Failed to record take-theirs audit issue: %s", e)


def _record_stashpop_takeours_event(
    project_root: Path,
    branch: str,
    archived: list[ArchivedEntry],
    flow_id: str | None,
    *,
    archive_failed: bool = False,
    unresolved_files: list[str] | None = None,
) -> None:
    """File an audit issue for a recovered stash-pop, with recovery pointers.

    Unlike the original (which recorded only conflict-file *paths*, leaving
    nothing to restore from), this records the archive manifest — for each
    discarded/colliding file, where its full content was persisted under
    ``tianluo/worktrees/.archive`` and a verifiable blob sha — so the operator
    can actually recover. ``archive_failed`` flips the issue to a high-prio
    data-loss-risk alert: recovery did not finalize so the live stash was
    kept (not dropped) and must be recovered manually. The manifest is still
    recorded in that case whenever content WAS archived (case-a sides captured
    before an unresolved resolution) — recovery pointers belong in the issue
    even when the final disposition could not complete. ``unresolved_files``
    names the case-a paths still unmerged in the index, if any.
    """
    try:
        from ..issue_manager import IssueManager
    except ImportError:
        return
    if archive_failed:
        title = (
            f"DAG leaf merge: stash-pop recovery INCOMPLETE on {branch}"
        )
        unresolved_note = ""
        if unresolved_files:
            unresolved_note = (
                "Unmerged paths still in the index (resolve manually):\n  - "
                + "\n  - ".join(unresolved_files)
                + "\n\n"
            )
        description = (
            f"Flow: {flow_id or '<unknown>'}\n"
            f"Branch: {branch}\n\n"
            "After the leaf merge landed, restoring the pre-merge stash "
            "(--include-untracked) could not be finalized. The stash was "
            "therefore NOT dropped to avoid data loss — recover it manually "
            "via `git stash list` / `git stash show -p`.\n\n"
            f"{unresolved_note}"
            "Content archived before the stash was kept (recoverable):\n"
            + _format_archived_manifest(archived)
        )
        priority = "high"
        tags = ["merge-fallback", "audit", "stash-pop", "data-loss-risk"]
    else:
        title = (
            f"DAG leaf merge: stash pop conflict recovered on {branch}"
        )
        description = (
            f"Flow: {flow_id or '<unknown>'}\n"
            f"Branch: {branch}\n\n"
            "After successful leaf merge, restoring the pre-merge stash "
            "(--include-untracked) conflicted on some paths. Case-a (tracked "
            "3-way) was reconciled by the LLM resolver (or take-ours fallback) "
            "with BOTH sides archived; case-b (untracked-collision — "
            "concurrent unrelated new files) gave way to the merged tree. "
            "Every discarded/colliding file's full content was archived first "
            "and the stash dropped only after that was proven, so each entry "
            "below is recoverable.\n\n"
            "Recoverable archive manifest:\n"
            + _format_archived_manifest(archived)
        )
        priority = "medium"
        tags = ["merge-fallback", "audit", "stash-pop"]
    try:
        IssueManager(project_root).create(
            title=title,
            description=description,
            priority=priority,
            type="task",
            tags=tags,
            source="system",
        )
    except Exception as e:
        logger.warning(
            "Failed to record stash-pop recovery audit issue: %s", e,
        )


def _salvage_results_history(results: list, project_root: Path) -> None:
    """Salvage history from each unique worktree in *results*.

    Relay chains reuse the same worktree across multiple GroupResult objects.
    The set-based deduplication here ensures _salvage_history_from_worktree is
    called exactly once per worktree, preventing group files from being
    appended multiple times to the main-repo history.
    """
    salvaged_worktrees: set[Path] = set()
    for r in results:
        if r.worktree_path and r.worktree_path not in salvaged_worktrees:
            salvaged_worktrees.add(r.worktree_path)
            try:
                _salvage_history_from_worktree(r.worktree_path, project_root)
            except Exception:
                logger.warning("DAG: failed to salvage history from worktree %s", r.worktree_path)


def _run_dag_parallel(
    groups: list[dict],
    step: Step,
    flow: FlowInstance,
    project_root: Path,
    task_description: str,
    task_type: str,
    injection: str | None,
    retry_count: int,
    prior_outputs: dict[str, Any] | None = None,
    root_cause_section: str = "",
    capability_mode: bool = False,
) -> StepStatus:
    """DAG parallel execution path for implement step.

    Creates a DAGScheduler from group dependencies, runs all groups in
    parallel via worktree-isolated LLM agents, then merges results back
    in topological order.

    Args:
        prior_outputs: Optional dict with keys files_changed, tests_added,
            test_mapping, implemented_groups from a previous (resumed) run.
            These are merged into the final aggregated outputs.
        capability_mode: True when PLAN emitted coarse capability groups;
            selects the per-group prompt that asks the agent to decompose its
            group rather than to execute a task enumeration it does not have.
    """
    original_branch = get_current_branch(project_root)

    # Pre-flight: refuse to start if the repo is in an unrecoverable git state,
    # and auto-heal stale leftover unmerged-index entries (modify/delete
    # conflicts abandoned without ``git merge --abort`` in a prior run leave
    # the index dirty, and ``git stash`` cannot package stage>0 entries — so
    # every subsequent leaf merge would fail with "Merging is not possible
    # because you have unmerged files" until someone manually ``git add``s).
    if merge_in_progress(project_root):
        msg = (
            "DAG implement aborted: an in-progress git merge/cherry-pick/"
            "rebase/revert was detected in the project root. Finish or "
            "abort it before retrying (git merge --continue / --abort, etc.)."
        )
        logger.error(msg)
        step.error_message = msg
        return StepStatus.FAILED

    recovered, unresolved = recover_stale_unmerged_paths(project_root)
    if recovered:
        logger.warning(
            "DAG implement: auto-recovered %d stale unmerged path(s) "
            "(working tree already matched HEAD): %s",
            len(recovered), recovered,
        )
    if unresolved:
        msg = (
            "DAG implement aborted: unmerged index entries diverge from HEAD "
            "and require manual resolution before retrying:\n  "
            + "\n  ".join(unresolved)
        )
        logger.error(msg)
        step.error_message = msg
        return StepStatus.FAILED

    # Disaster recovery: merge surviving branches from prior_outputs
    # before running new groups (so new groups see recovered code)
    recovered_groups: list[str] = list(prior_outputs.get("implemented_groups", [])) if prior_outputs else []
    already_deleted_gids: set[str] = set()
    if recovered_groups:
        from ..worktree import has_new_commits
        for gid in recovered_groups:
            branch = f"impl/{flow.flow_id}/{gid}"
            # Salvage history from stale worktree before cleanup
            safe_name = branch.replace("/", "-")
            stale_wt = runtime_dir(project_root) / "worktrees" / safe_name
            if stale_wt.exists():
                try:
                    _salvage_history_from_worktree(stale_wt, project_root)
                except Exception:
                    logger.debug("DAG resume: failed to salvage history from %s", stale_wt)
            # Clean up stale worktree from crashed run (if any)
            try:
                force_cleanup_worktree(project_root, branch)
            except Exception:
                pass
            # Check if branch still exists and has unmerged commits
            check = _run_git(project_root, "rev-parse", "--verify", branch, check=False)
            if check.returncode != 0:
                continue  # Branch already merged/deleted from a previous run
            try:
                if has_new_commits(project_root, branch, original_branch):
                    logger.info("DAG resume: pre-merging recovered branch %s", branch)
                    merge_step_id = f"{step.step_id}_recover_{gid}"
                    success = _merge_leaf_branch(
                        project_root, branch, original_branch,
                        task_description, [],
                        flow_id=flow.flow_id, merge_step_id=merge_step_id,
                    )
                    if success:
                        delete_branch(project_root, branch)
                        already_deleted_gids.add(gid)
                    else:
                        logger.error("DAG resume: merge failed for recovered %s", gid)
                else:
                    # Branch exists but no new commits — clean up
                    delete_branch(project_root, branch)
                    already_deleted_gids.add(gid)
            except Exception as e:
                logger.warning("DAG resume: failed to process branch %s: %s", branch, e)

    logger.info(
        "DAG parallel: executing %d groups (branch=%s)",
        len(groups), original_branch,
    )

    if not groups:
        # All groups recovered, nothing new to run — just aggregate outputs
        step.outputs["files_changed"] = list(prior_outputs.get("files_changed", [])) if prior_outputs else []
        step.outputs["tests_added"] = list(prior_outputs.get("tests_added", [])) if prior_outputs else []
        step.outputs["test_mapping"] = dict(prior_outputs.get("test_mapping", {})) if prior_outputs else {}
        step.outputs["implemented_groups"] = recovered_groups
        recovered_summaries = (
            _normalize_group_summaries(prior_outputs.get("group_summaries"))
            if prior_outputs else []
        )
        step.outputs["group_summaries"] = recovered_summaries
        # Every group is pre-resume here, so the aggregate string must be built
        # from their persisted summaries — the placeholder is a last resort for
        # flows that recorded no summary text at all, since downstream string
        # consumers (version_analyze, commit) see only this field.
        joined_recovered = "; ".join(
            e["summary"] for e in recovered_summaries if e["summary"]
        )
        step.outputs["summary"] = joined_recovered or "Recovered from previous run"
        step.outputs["completion_status"] = "complete"
        step.outputs["incomplete_tasks"] = []
        step.outputs["estimated_test_duration"] = (
            prior_outputs.get("estimated_test_duration") if prior_outputs else None
        )
        return StepStatus.COMPLETED

    # Transitive reduction: remove redundant dependency edges
    reduced_groups = transitive_reduce(groups)

    # Log which redundant edges were removed
    orig_deps = {g['group_id']: set(g.get('depends_on', [])) for g in groups}
    any_removed = False
    for rg in reduced_groups:
        gid = rg['group_id']
        removed = orig_deps.get(gid, set()) - set(rg.get('depends_on', []))
        if removed:
            any_removed = True
            logger.info("Transitive reduction: %s removed redundant deps %s", gid, sorted(removed))
    if not any_removed:
        logger.info("Transitive reduction: no redundant edges found")

    # Generate relay execution plan
    relay_plan = classify_chains(reduced_groups)
    logger.info(
        "DAG relay plan: %d root(s), %d leaf(ves), %d convergence point(s)",
        len(relay_plan.root_nodes), len(relay_plan.leaf_nodes),
        len(relay_plan.convergence_points),
    )

    # Per-group status sink: during DAG parallel execution each group runs in
    # an isolated worktree whose conversation jsonl is only salvaged back to
    # the main repo once the whole step finishes, leaving the web console blank
    # for the entire parallel phase. The scheduler emits coarse per-group status
    # transitions (queued → running → completed/failed/skipped); we persist each
    # one as a self-contained NDJSON line into the main repo's step jsonl so the
    # daemon's history signature shifts and pushes a live status marker. This
    # only relays *status*, never the per-group conversation content (which
    # still appears at step end after the worktree history is salvaged).
    # Shared latest-known agent/model per group, populated by each group's
    # in-worktree LLMCaller via its on_agent_change callback. The coarse status
    # sink below reads it back so the terminal (completed/failed) records carry
    # the same final agent/model the live running records showed — keeping the
    # console's status card labelled consistently through to completion. Old
    # records without these fields simply omit them (no empty placeholder).
    group_agent_info: dict[str, tuple[str, str | None]] = {}

    def _on_group_status(group_id: str, status: str) -> None:
        agent_name, model_name = group_agent_info.get(group_id, (None, None))
        record_group_status(
            project_root,
            flow.flow_id,
            step.step_id,
            "implement",
            group_id,
            status,
            agent_name=agent_name,
            model_name=model_name,
        )

    scheduler = DAGScheduler(
        reduced_groups,
        max_workers=4,
        relay_plan=relay_plan,
        on_group_status=_on_group_status,
    )
    execute_fn = _make_execute_fn(
        project_root=project_root,
        original_branch=original_branch,
        flow=flow,
        step=step,
        task_description=task_description,
        task_type=task_type,
        injection=injection,
        retry_count=retry_count,
        group_agent_info=group_agent_info,
        root_cause_section=root_cause_section,
        capability_mode=capability_mode,
    )

    results: list[GroupResult] = []
    try:
        results = scheduler.run(execute_fn)
    finally:
        # Salvage history files from worktrees before cleanup.
        _salvage_results_history(results, project_root)

        # Clean up worktrees (deduplicated for relay chains sharing worktrees).
        # NOTE: Only remove worktree directories here, NOT branches.
        # Branches must survive until after merge-back completes.
        cleaned_branches: set[str] = set()
        for r in results:
            if r.branch_name and r.branch_name not in cleaned_branches:
                cleaned_branches.add(r.branch_name)
                try:
                    force_cleanup_worktree(project_root, r.branch_name)
                except Exception:
                    logger.warning("DAG: failed to force-cleanup worktree for branch %s", r.branch_name)

        # Ensure we're back on original_branch
        try:
            current = get_current_branch(project_root)
            if current != original_branch:
                _run_git(project_root, "checkout", original_branch)
        except Exception:
            logger.warning("DAG: failed to restore original branch %s", original_branch)

    # Build results map for easy lookup
    results_map: dict[str, GroupResult] = {r.group_id: r for r in results}

    # Merge only leaf nodes + fallback leaves (not all completed groups).
    # With relay strategy, intermediate groups' commits are already on the
    # leaf branch — only the leaf needs to merge back to original_branch.
    fallback_leaf_ids = set(scheduler.get_fallback_leaves())
    merge_group_ids = relay_plan.leaf_nodes | fallback_leaf_ids

    # Collect unique branches to merge (relay chains share the same branch)
    branches_to_merge: list[tuple[str, str]] = []
    seen_branches: set[str] = set()
    for gid in merge_group_ids:
        r = results_map.get(gid)
        if r and r.status == "completed" and r.branch_name and r.branch_name not in seen_branches:
            seen_branches.add(r.branch_name)
            branches_to_merge.append((gid, r.branch_name))

    # Build group summaries for LLM conflict resolution context
    group_summaries = [
        {"group_id": r.group_id, "summary": r.summary, "files_changed": r.files_changed}
        for r in results if r.status == "completed"
    ]

    merge_failures: list[str] = []
    for merge_idx, (gid, branch) in enumerate(branches_to_merge):
        logger.info("DAG: merging leaf branch %s back to %s", branch, original_branch)
        merge_step_id = f"{step.step_id}_merge_{merge_idx}"
        success = _merge_leaf_branch(
            project_root, branch, original_branch,
            task_description, group_summaries,
            flow_id=flow.flow_id, merge_step_id=merge_step_id,
        )
        if not success:
            logger.error("DAG: leaf merge failed for %s (branch %s)", gid, branch)
            merge_failures.append(gid)

    # Delete impl branches — but ONLY if their commits are now reachable
    # from original_branch (i.e. successfully merged). Branches whose merge
    # failed for any reason must survive so the operator can recover their
    # commits manually (e.g. ``git checkout <branch>``). Force-deleting an
    # un-merged branch with ``git branch -D`` was the proximate cause of
    # the 20260507-200706 data loss incident.
    candidate_branches: set[str] = set()
    for r in results:
        if r.branch_name:
            candidate_branches.add(r.branch_name)
    for gid in recovered_groups:
        if gid not in already_deleted_gids:
            candidate_branches.add(f"impl/{flow.flow_id}/{gid}")

    preserved_branches: list[str] = []
    for branch in candidate_branches:
        if not _is_branch_reachable_from(project_root, branch, original_branch):
            preserved_branches.append(branch)
            logger.warning(
                "DAG cleanup: preserving branch %s (not an ancestor of %s — "
                "merge failed or branch missing; commits protectively kept)",
                branch, original_branch,
            )
            continue
        try:
            delete_branch(project_root, branch)
        except Exception:
            logger.debug("DAG: failed to delete branch %s", branch)

    if preserved_branches:
        step.outputs["preserved_branches"] = sorted(preserved_branches)

    # Apply restricted edits on original_branch
    all_restricted_applied: list[dict] = []
    all_restricted_failed: list[dict] = []
    for r in results:
        if r.status == "completed" and r.restricted_edits:
            applied, failed_edits = _apply_restricted_edits(r.restricted_edits, project_root)
            all_restricted_applied.extend(applied)
            all_restricted_failed.extend(failed_edits)

    # Aggregate outputs — seed from prior_outputs (resume) if available
    all_files_changed: list[str] = list(prior_outputs.get("files_changed", [])) if prior_outputs else []
    all_tests_added: list[str] = list(prior_outputs.get("tests_added", [])) if prior_outputs else []
    merged_test_mapping: dict = dict(prior_outputs.get("test_mapping", {})) if prior_outputs else {}
    all_completion_statuses: list[str] = []
    all_incomplete_tasks: list[str] = []
    implemented_group_ids: list[str] = list(prior_outputs.get("implemented_groups", [])) if prior_outputs else []
    # WHY: seed both the structured list AND the joined string from the resume
    # payload — otherwise a resumed DAG run reports only the groups it happened
    # to re-run, silently dropping every summary earned before the interruption.
    group_summary_entries: list[dict] = (
        _normalize_group_summaries(prior_outputs.get("group_summaries"))
        if prior_outputs else []
    )
    summaries: list[str] = [
        e["summary"] for e in group_summary_entries if e["summary"]
    ]
    estimated_durations: list[float] = []
    if prior_outputs and prior_outputs.get("estimated_test_duration") is not None:
        estimated_durations.append(float(prior_outputs["estimated_test_duration"]))

    for r in results:
        if r.status == "completed":
            all_files_changed.extend(r.files_changed)
            all_tests_added.extend(r.tests_added)
            merged_test_mapping.update(r.test_mapping)
            implemented_group_ids.append(r.group_id)
            all_completion_statuses.append(r.completion_status)
            all_incomplete_tasks.extend(r.incomplete_tasks)
            group_summary_entries.append(
                {"group_id": r.group_id, "summary": r.summary or ""}
            )
            if r.summary:
                summaries.append(r.summary)
            if r.estimated_test_duration is not None:
                estimated_durations.append(float(r.estimated_test_duration))
        elif r.status == "failed":
            all_completion_statuses.append("failed")
            if r.error:
                all_incomplete_tasks.append(f"Group {r.group_id}: {r.error}")
        elif r.status == "skipped":
            all_completion_statuses.append("failed")
            all_incomplete_tasks.append(f"Group {r.group_id}: skipped (upstream dependency failed)")

    # Add files from successful restricted edits
    for edit in all_restricted_applied:
        fp = edit.get("file_path", "")
        if fp and fp not in all_files_changed:
            all_files_changed.append(fp)

    step.outputs["files_changed"] = all_files_changed
    step.outputs["tests_added"] = all_tests_added
    step.outputs["test_mapping"] = merged_test_mapping
    step.outputs["implemented_groups"] = implemented_group_ids
    step.outputs["group_summaries"] = group_summary_entries
    step.outputs["summary"] = "; ".join(summaries)
    # Each group reports a whole-suite estimate, so take the max; see
    # IMPLEMENT_GROUP_PROMPT response field notes.
    step.outputs["estimated_test_duration"] = (
        max(estimated_durations) if estimated_durations else None
    )

    if all_restricted_applied:
        step.outputs["restricted_edits_applied"] = all_restricted_applied
    if all_restricted_failed:
        step.outputs["restricted_edits_failed"] = all_restricted_failed

    # Compute overall completion status.
    # ``merge_failures`` is an INDEPENDENT failure source: even when every
    # group reports ``completed``, a leaf merge that did not land = data
    # never reached ``original_branch`` = step did not actually complete.
    # Defense-in-depth on top of the merge-robustness changes — if the
    # multi-layer fallback in ``_merge_leaf_branch`` ever did fail (e.g.
    # disk full during the take-theirs commit), the step status must
    # reflect it so the flow does NOT proceed into test → self_check on
    # an inconsistent main repo state.
    if merge_failures:
        overall_status = "failed"
    elif "failed" in all_completion_statuses:
        if fallback_leaf_ids:
            overall_status = "partial"
        else:
            overall_status = "failed"
    elif "partial" in all_completion_statuses:
        overall_status = "partial"
    else:
        overall_status = "complete"

    step.outputs["completion_status"] = overall_status
    step.outputs["incomplete_tasks"] = all_incomplete_tasks

    if merge_failures:
        step.outputs["merge_failures"] = merge_failures

    if overall_status == "failed":
        step.error_message = "DAG parallel: one or more groups failed"
        return StepStatus.FAILED
    elif overall_status == "partial":
        logger.warning(
            "DAG parallel: partially completed. Incomplete: %s",
            all_incomplete_tasks,
        )
        return StepStatus.PARTIAL

    return StepStatus.COMPLETED


# Keys of the structured implementation summary the Phase-2 extraction must
# yield. A truthy dict carrying NONE of them (a refusal, or JSON with junk
# keys) is not an implementation report at all — see the holistic gate below.
_IMPLEMENT_SUMMARY_KEYS = frozenset({
    "files_changed",
    "tests_added",
    "test_mapping",
    "summary",
    "completion_status",
    "incomplete_tasks",
    "restricted_edits",
    "estimated_test_duration",
})


def _is_fix_round(step: Step) -> bool:
    """Whether this implement entry is a fix-loop re-entry rather than round 1.

    The state machine marks the re-used implement Step with both
    ``is_fix_iteration`` and a 1-based ``fix_iteration``; either alone is
    enough, so an older/partial snapshot still classifies correctly.
    """
    inputs = getattr(step, "inputs", None) or {}
    if inputs.get("is_fix_iteration"):
        return True
    try:
        return int(inputs.get("fix_iteration", 0) or 0) > 0
    except (TypeError, ValueError):
        return False


def _run_single_llm_call(
    prompt: str,
    step: Step,
    flow: FlowInstance,
    project_root: Path,
    task_groups,
    retry_count: int,
    stream_prefix: str = '',
    implemented_groups_override: Any = None,
    invocation_intent: AgentInvocationIntent = AgentInvocationIntent.DEFAULT,
    preserve_existing_outputs: bool = False,
) -> StepStatus:
    """Execute a single LLM call for implement (fallback path)."""
    try:
        caller = LLMCaller(
            project_root,
            flow_id=flow.flow_id,
            step_id=step.step_id,
            step_type=step.step_type.value,
            external_attempt=retry_count,
            stream_prefix=stream_prefix,
            fix_iteration=step.inputs.get("fix_iteration", 0),
        )
        response = caller.call(
            prompt=prompt,
            json_mode="two_phase",
            json_schema_hint='{"files_changed": [], "tests_added": [], "estimated_test_duration": 120, "test_mapping": {}, "summary": "...", "completion_status": "complete|partial|failed", "incomplete_tasks": [], "restricted_edits": [{"file_path": "...", "old_string": "...", "new_string": "..."}]}',
            invocation_intent=invocation_intent,
        )

        result = parse_json_response(response, required_keys=[])

        # A Phase-2 extraction can yield a truthy dict that carries none of
        # the implementation-summary keys (a refusal, or JSON with junk keys).
        # For a holistic call that is not a completed implementation — the
        # caller never reported what it did — so keep the step recoverable via
        # the unparseable-result branch below instead of advancing to TEST on
        # zero implementation. Planned flows keep their historical lenient
        # parsing (defaults recorded, step forwards).
        holistic = _holistic_execution_mode(step, flow)
        if holistic and result and not _IMPLEMENT_SUMMARY_KEYS.intersection(result):
            result = None

        # WHY this round's own file list is captured separately: the fix loop
        # re-enters the SAME Step object, and `_resolve_files_changed` rewrites
        # `files_changed` from a flow-baseline git diff after every round — so
        # that key is always cumulative and a round's own contribution would be
        # unrecoverable from the persisted outputs. Reset before parsing so a
        # round whose summary failed to parse cannot leave the PREVIOUS round's
        # list standing as if it were this round's.
        # Only `_run_single_llm_call` is reachable from the fix branch (both the
        # holistic and the planned fix path call it); the grouped/DAG writers of
        # `restricted_edits_applied` never run a fix iteration, so they need no
        # equivalent.
        is_fix_round = _is_fix_round(step)
        if is_fix_round:
            step.outputs["fix_round_files_changed"] = []

        if result:
            round_files_changed = list(result.get("files_changed", []) or [])
            files_changed = list(round_files_changed)
            if preserve_existing_outputs:
                files_changed = list(dict.fromkeys(
                    list(step.outputs.get("files_changed", []) or [])
                    + files_changed
                ))

            # Apply restricted edits (Bug A)
            restricted_edits = result.get("restricted_edits", [])
            if restricted_edits:
                applied, failed_edits = _apply_restricted_edits(restricted_edits, project_root)
                round_applied = list(applied)
                if preserve_existing_outputs:
                    applied = (
                        list(step.outputs.get("restricted_edits_applied", []) or [])
                        + applied
                    )
                    failed_edits = (
                        list(step.outputs.get("restricted_edits_failed", []) or [])
                        + failed_edits
                    )
                step.outputs["restricted_edits_applied"] = applied
                step.outputs["restricted_edits_failed"] = failed_edits
                # Add successfully edited files to files_changed
                for edit in applied:
                    fp = edit.get("file_path", "")
                    if fp and fp not in files_changed:
                        files_changed.append(fp)
                for edit in round_applied:
                    fp = edit.get("file_path", "")
                    if fp and fp not in round_files_changed:
                        round_files_changed.append(fp)
                if applied:
                    logger.info("Applied %d restricted edits", len(applied))
                if failed_edits:
                    logger.warning("Failed %d restricted edits", len(failed_edits))

            step.outputs["files_changed"] = files_changed
            if is_fix_round:
                step.outputs["fix_round_files_changed"] = list(dict.fromkeys(
                    str(f) for f in round_files_changed if f
                ))
            tests_added = list(result.get("tests_added", []) or [])
            test_mapping = dict(result.get("test_mapping", {}) or {})
            if preserve_existing_outputs:
                tests_added = list(dict.fromkeys(
                    list(step.outputs.get("tests_added", []) or [])
                    + tests_added
                ))
                prior_mapping = dict(step.outputs.get("test_mapping", {}) or {})
                prior_mapping.update(test_mapping)
                test_mapping = prior_mapping
            step.outputs["tests_added"] = tests_added
            step.outputs["test_mapping"] = test_mapping
            step.outputs["implemented_groups"] = (
                task_groups
                if implemented_groups_override is None
                else implemented_groups_override
            )
            summary = result.get("summary", "")
            if preserve_existing_outputs and not summary:
                summary = step.outputs.get("summary", "")
            step.outputs["summary"] = summary
            # The holistic path reports zero implemented groups by design, so
            # fall back to the planned groups for the one case that still has a
            # real label to give: a plan of exactly one capability group.
            lone_group_id = (
                _lone_group_id(step.outputs["implemented_groups"])
                or _lone_group_id(step.inputs.get("task_groups"))
            )
            step.outputs["group_summaries"] = (
                [{"group_id": lone_group_id, "summary": summary}]
                if summary else []
            )
            estimated_duration = _sanitize_estimated_test_duration(
                result.get("estimated_test_duration")
            )
            if preserve_existing_outputs and estimated_duration is None:
                estimated_duration = step.outputs.get("estimated_test_duration")
            step.outputs["estimated_test_duration"] = estimated_duration

            # Completion status detection (Bug B)
            completion_status = str(
                result.get("completion_status", "complete") or "complete"
            ).strip().lower()
            incomplete_tasks = list(result.get("incomplete_tasks", []) or [])
            # Only the holistic (direct/small) path may not advance on leftover
            # tasks — its transition re-enters IMPLEMENT until complete. Planned
            # flows keep their historical recording so an honest
            # complete-with-leftover report still commits/summarizes as
            # complete rather than gaining an out-of-scope "partial" stamp.
            if (
                holistic
                and incomplete_tasks
                and completion_status != "failed"
            ):
                completion_status = "partial"
            step.outputs["completion_status"] = completion_status
            step.outputs["incomplete_tasks"] = incomplete_tasks

            if completion_status == "failed":
                step.error_message = "LLM reported implementation failed"
                return StepStatus.FAILED
            elif completion_status == "partial":
                logger.warning(
                    "Implementation partially completed. Incomplete tasks: %s",
                    incomplete_tasks,
                )
                return StepStatus.PARTIAL

            return StepStatus.COMPLETED
        else:
            logger.warning("Could not parse implement summary JSON, using defaults")
            step.outputs["implemented_groups"] = (
                task_groups
                if implemented_groups_override is None
                else implemented_groups_override
            )
            if preserve_existing_outputs:
                step.outputs.setdefault("files_changed", [])
                step.outputs.setdefault("tests_added", [])
                step.outputs.setdefault("test_mapping", {})
                step.outputs.setdefault("group_summaries", [])
                step.outputs.setdefault("estimated_test_duration", None)
                step.outputs["completion_status"] = "partial"
                step.outputs.setdefault(
                    "incomplete_tasks",
                    ["Return a valid structured implementation summary."],
                )
                return StepStatus.PARTIAL
            step.outputs["files_changed"] = []
            step.outputs["tests_added"] = []
            step.outputs["test_mapping"] = {}
            step.outputs["estimated_test_duration"] = None

        return StepStatus.COMPLETED

    except LLMCallError as e:
        logger.exception("Implement step LLM call failed")
        step.error_message = f"Implementation failed: {str(e)}"
        return StepStatus.FAILED
    except Exception as e:
        logger.exception("Implement step failed")
        step.error_message = f"Implementation failed: {str(e)}"
        return StepStatus.FAILED


def _apply_restricted_edits(
    restricted_edits: list[dict], project_root: Path,
) -> tuple[list[dict], list[dict]]:
    """Apply edits that the LLM subprocess could not perform due to permission restrictions.

    Args:
        restricted_edits: List of {file_path, old_string, new_string} dicts.
        project_root: Root directory of the project.

    Returns:
        Tuple of (successful_edits, failed_edits). Each failed edit includes an 'error' key.
    """
    successful = []
    failed = []

    for edit in restricted_edits:
        file_path_str = edit.get("file_path", "")
        old_string = edit.get("old_string", "")
        new_string = edit.get("new_string", "")

        if not file_path_str or not old_string:
            failed.append({**edit, "error": "Missing file_path or old_string"})
            continue

        target = project_root / file_path_str
        try:
            if not target.is_file():
                failed.append({**edit, "error": f"File not found: {file_path_str}"})
                continue

            content = target.read_text(encoding="utf-8")

            if old_string not in content:
                failed.append({**edit, "error": f"old_string not found in {file_path_str}"})
                continue

            new_content = content.replace(old_string, new_string, 1)
            target.write_text(new_content, encoding="utf-8")

            # Verify the edit
            verify_content = target.read_text(encoding="utf-8")
            if new_string not in verify_content:
                failed.append({**edit, "error": f"Verification failed: new_string not found after write in {file_path_str}"})
                continue

            logger.info("Applied restricted edit to %s", file_path_str)
            successful.append(edit)

        except Exception as e:
            failed.append({**edit, "error": f"Exception: {e}"})

    return successful, failed


def _salvage_history_from_worktree(worktree_path: Path, main_repo_root: Path) -> None:
    """Copy history files from a worktree back to the main repository.

    When LLM runs in a worktree, chat history is recorded under the worktree's
    tianluo/history/ directory. This function copies those files to the main repo
    before the worktree is cleaned up, preventing history loss.

    Args:
        worktree_path: Path to the worktree directory
        main_repo_root: Path to the main repository root
    """
    import shutil

    wt_history = runtime_dir(worktree_path) / "history"
    if not wt_history.exists():
        return

    main_history = runtime_dir(main_repo_root) / "history"
    main_history.mkdir(parents=True, exist_ok=True)

    copied = 0
    for flow_dir in wt_history.iterdir():
        if not flow_dir.is_dir():
            continue
        target_flow_dir = main_history / flow_dir.name
        target_flow_dir.mkdir(parents=True, exist_ok=True)

        for history_file in flow_dir.iterdir():
            if not history_file.is_file():
                continue
            # Only salvage group-specific files (e.g. _G1.jsonl) generated by this
            # worktree. Skip files shared from the main repo (discovery, analyze,
            # plan, etc.) to prevent them from being appended N times.
            if not re.search(r"_G\d+\.jsonl$", history_file.name):
                continue
            target_file = target_flow_dir / history_file.name
            if target_file.exists():
                # Append content (NDJSON is line-based, safe to concatenate)
                with open(target_file, "a", encoding="utf-8") as dst:
                    dst.write(history_file.read_text(encoding="utf-8"))
            else:
                shutil.copy2(history_file, target_file)
            copied += 1

    if copied:
        logger.info("Salvaged %d history file(s) from worktree %s", copied, worktree_path)


def _restore_history_to_worktree(main_repo_root: Path, worktree_path: Path, flow_id: str) -> None:
    """Copy history files from main repo into a worktree.

    This enables LLMCaller retry context injection in worktrees,
    which look for history at their own project_root/tianluo/history/.
    Only copies history for the given flow_id.

    Args:
        main_repo_root: Main repository root
        worktree_path: Worktree directory
        flow_id: Flow ID to copy history for
    """
    import shutil

    main_flow_dir = runtime_dir(main_repo_root) / "history" / flow_id
    if not main_flow_dir.exists():
        return

    wt_flow_dir = runtime_dir(worktree_path) / "history" / flow_id
    wt_flow_dir.mkdir(parents=True, exist_ok=True)

    copied = 0
    for history_file in main_flow_dir.iterdir():
        if not history_file.is_file():
            continue
        # Skip group-specific files (_G<n>.jsonl): they already exist in main and
        # would be double-appended when this worktree is later salvaged.  Only
        # shared/context files (discovery, analyze, plan, …) are needed for LLM
        # retry-context injection.
        if re.search(r"_G\d+\.jsonl$", history_file.name):
            continue
        target = wt_flow_dir / history_file.name
        shutil.copy2(history_file, target)
        copied += 1

    if copied:
        logger.debug("Restored %d history file(s) to worktree %s", copied, worktree_path)


def _normalize_group_summaries(value: Any) -> list[dict]:
    """Coerce a persisted ``group_summaries`` payload into well-formed entries.

    State restored from disk (or from an older flow) can hold anything, so
    every entry is narrowed to the ``{"group_id": str, "summary": str}`` shape
    the renderers rely on; anything unrecognisable is dropped.
    """
    if not isinstance(value, list):
        return []
    entries: list[dict] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        entries.append({
            "group_id": str(item.get("group_id", "") or ""),
            "summary": str(item.get("summary", "") or ""),
        })
    return entries


def _prior_group_summaries(outputs: dict, group_ids) -> list[dict]:
    """Select the persisted per-group summaries for ``group_ids`` (resume)."""
    wanted = set(group_ids)
    return [
        e for e in _normalize_group_summaries(outputs.get("group_summaries"))
        if e["group_id"] in wanted
    ]


def _lone_group_id(implemented_groups: Any) -> str:
    """Return the real group_id when the holistic call covered exactly one group.

    WHY: a holistic call has no per-group split, so inventing a positional
    ``G1`` label would resurrect exactly the defect this field exists to fix —
    a label that does not name any group PLAN actually emitted. Absent a single
    real group, the entry stays unlabelled.
    """
    if not isinstance(implemented_groups, list) or len(implemented_groups) != 1:
        return ""
    group = implemented_groups[0]
    if isinstance(group, str):
        return group
    if isinstance(group, dict):
        return str(group.get("group_id", "") or "")
    return ""


def _sanitize_estimated_test_duration(value: Any) -> float | None:
    """Coerce an LLM-reported estimated_test_duration to a positive float.

    Rejects bool (a subclass of int) and non-positive values so the caller
    falls back to the fixed config.timeout rather than computing an
    unreasonably small dynamic timeout.
    """
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    if value <= 0:
        return None
    return float(value)


def _get_head_hash(project_root: Path) -> str | None:
    """Get current HEAD commit hash, or None for empty repos."""
    result = _run_git(project_root, "rev-parse", "HEAD", check=False)
    if result.returncode == 0:
        return result.stdout.strip()
    return None


def _read_pre_session_version(project_root: Path) -> str | None:
    """Read the project version before implement runs.

    Detects the project's version file via VersionBumper and reads the
    current version. Returns None on any failure (missing file, parse
    error, etc.) so that implement is never blocked by version detection
    issues — the value is purely informational for version_analyze.
    """
    try:
        from ...config import load_version_config
        from ..version_bumper import VersionBumper

        config = load_version_config(project_root)
        if not getattr(config, "enabled", True):
            return None
        bumper = VersionBumper(config)
        version_file = bumper.detect_version_file(project_root)
        if version_file is None:
            return None
        return bumper.read_version(version_file)
    except Exception:
        logger.debug("Could not read pre_session_version", exc_info=True)
        return None


def _collect_session_commits(
    project_root: Path, baseline_hash: str | None,
) -> list[dict]:
    """Collect commits introduced on HEAD since ``baseline_hash``.

    Excludes the baseline commit itself and merge commits (which are noise
    when assessing what implement actually changed). Each returned entry is
    a dict with keys ``sha``, ``subject``, and ``files`` (list of str).

    Returns an empty list when:
    - ``baseline_hash`` is None (empty repo or unknown baseline)
    - no new commits exist between baseline and HEAD
    - any git command fails (logged at debug level)
    """
    if not baseline_hash:
        return []

    try:
        log_result = _run_git(
            project_root,
            "log",
            "--no-merges",
            f"{baseline_hash}..HEAD",
            "--pretty=format:%H%x00%s",
            check=False,
        )
    except Exception:
        logger.debug("git log for session commits failed", exc_info=True)
        return []

    if log_result.returncode != 0:
        logger.debug(
            "git log baseline..HEAD returned %d: %s",
            log_result.returncode, log_result.stderr.strip(),
        )
        return []

    raw = log_result.stdout.strip()
    if not raw:
        return []

    commits: list[dict] = []
    for line in raw.splitlines():
        if "\x00" not in line:
            continue
        sha, subject = line.split("\x00", 1)
        sha = sha.strip()
        if not sha:
            continue
        files: list[str] = []
        try:
            files_result = _run_git(
                project_root,
                "diff-tree",
                "--no-commit-id",
                "--name-only",
                "-r",
                sha,
                check=False,
            )
            if files_result.returncode == 0 and files_result.stdout.strip():
                files = [
                    f for f in files_result.stdout.strip().splitlines() if f
                ]
        except Exception:
            logger.debug(
                "git diff-tree failed for %s", sha, exc_info=True,
            )
            files = []
        commits.append({
            "sha": sha,
            "subject": subject,
            "files": files,
        })
    return commits


def _resolve_files_changed(step: Step, project_root: Path, baseline_hash: str | None) -> None:
    """Replace LLM-reported files_changed with git diff ground truth.

    Compares the current HEAD (after implementation) against the baseline
    hash (before implementation) to get the definitive list of changed files.
    Also includes any uncommitted changes in the working tree.

    Args:
        step: Step whose outputs["files_changed"] will be overwritten
        project_root: Project root directory
        baseline_hash: Commit hash from before implementation, or None
    """
    try:
        changed: set[str] = set()

        # Committed changes since baseline
        if baseline_hash:
            result = _run_git(
                project_root, "diff", "--name-only", baseline_hash, "HEAD",
                check=False,
            )
            if result.returncode == 0 and result.stdout.strip():
                changed.update(result.stdout.strip().splitlines())

        # Uncommitted changes (staged + unstaged)
        result = _run_git(
            project_root, "diff", "--name-only", "HEAD", check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            changed.update(result.stdout.strip().splitlines())

        # Staged but not committed
        result = _run_git(
            project_root, "diff", "--name-only", "--cached", check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            changed.update(result.stdout.strip().splitlines())

        # Untracked files (new files created by LLM)
        result = _run_git(
            project_root, "ls-files", "--others", "--exclude-standard", check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            changed.update(result.stdout.strip().splitlines())

        if changed:
            step.outputs["files_changed"] = sorted(changed)
            logger.info("Resolved files_changed from git: %d files", len(changed))
        # If git diff found nothing but LLM reported files, keep LLM report as fallback

    except Exception as e:
        logger.debug("Failed to resolve files_changed from git: %s", e)
        # Keep LLM-reported files_changed as fallback


def _format_fix_history(fix_history: list) -> str:
    """Format fix history for inclusion in FIX_PROMPT.

    Uses the structured ``issues`` list (already capped at 10 entries per
    iteration) rather than the raw fix_instructions text, keeping the
    prompt bounded regardless of LLM verbosity.
    """
    if not fix_history:
        return "No previous fix attempts."

    lines = []
    for entry in fix_history:
        it = entry.get("iteration", "?")
        reason = entry.get("reason", "unknown")
        trigger = entry.get("trigger_step_type", "unknown")
        lines.append(f"- Iteration {it}: triggered by {trigger} ({reason})")
        issues = entry.get("issues", [])
        if issues:
            from ._fix_context import extract_issue_display_fields
            for issue in issues[:5]:
                # Schema-compat: post-Commit-3 self_check stores new schema
                # (actual_behavior / divergence / evidence_lines) into
                # fix_history; verify_spec still uses legacy
                # (description / message / location). The extractor handles
                # both. severity is normalized upstream by
                # state_machine._normalize_issue_fields.
                sev, desc, loc = extract_issue_display_fields(issue)
                if not sev:
                    sev = "?"
                loc_str = f" @ {loc}" if loc else ""
                lines.append(f"  - [{sev}] {desc}{loc_str}")
            if len(issues) > 5:
                lines.append(f"  ... and {len(issues) - 5} more issue(s)")
        # Backward compat: old fix_history entries may still carry this field
        # (already truncated to 500 chars at storage time, no re-truncation needed)
        elif entry.get("fix_instructions_summary"):
            lines.append(f"  Summary: {entry['fix_instructions_summary']}")

    return "\n".join(lines)


def _format_fix_context_structured(fix_context: dict | str | None) -> str:
    if not fix_context:
        return "No additional context."
    if isinstance(fix_context, str):
        return fix_context

    lines = []
    reason = fix_context.get("reason", "unknown")
    lines.append(f"Reason: {reason}")

    if reason == "test_failure" or fix_context.get("test_failed"):
        timeout_reason = fix_context.get("timeout_reason")
        if timeout_reason:
            lines.append(f"Timeout reason: {timeout_reason}")
            previous_timeout = fix_context.get("previous_timeout")
            if previous_timeout is not None:
                lines.append(f"Previous timeout: {previous_timeout}s")
            previous_estimate = fix_context.get("previous_estimated_test_duration")
            # Whole-valued floats render as int — the LLM emits integers,
            # so echoing '300.0' back when it wrote '300' can induce drift.
            if isinstance(previous_estimate, float) and previous_estimate.is_integer():
                previous_estimate_display: str = str(int(previous_estimate))
            elif previous_estimate is None:
                previous_estimate_display = "not set"
            else:
                previous_estimate_display = str(previous_estimate)
            lines.append(
                f"Previous estimated_test_duration: {previous_estimate_display}"
            )
            timeout_multiplier = fix_context.get("timeout_multiplier")
            if timeout_multiplier is not None:
                lines.append(f"Timeout multiplier: {timeout_multiplier}")
            if fix_context.get("timeout_at_cap"):
                lines.append(
                    "Timeout at cap: true — raising estimated_test_duration "
                    "further will not increase the timeout; investigate "
                    "splitting the suite or fixing a slow/hung test."
                )

        test_analysis = fix_context.get("test_analysis", {})
        if test_analysis:
            summary = test_analysis.get("failure_summary", "")
            root_cause = test_analysis.get("root_cause", "")
            if summary:
                lines.append(f"Failure summary: {summary}")
            if root_cause:
                lines.append(f"Root cause: {root_cause}")

    if reason == "spec_compliance":
        spec_issues = fix_context.get("spec_issues", [])
        if spec_issues:
            lines.append("Spec issues:")
            for issue in spec_issues:
                priority = issue.get("priority", "high")
                msg = issue.get("message", "")
                lines.append(f"  - [{priority}] {msg}")

    if reason == "self_check":
        issues = fix_context.get("issues", [])
        if issues:
            from ._fix_context import extract_issue_display_fields
            lines.append("Self-check findings:")
            for issue in issues:
                if not isinstance(issue, dict):
                    continue
                severity, desc, location = extract_issue_display_fields(issue)
                loc_suffix = f" @ {location}" if location else ""
                lines.append(f"  - [{severity}] {desc}{loc_suffix}")

    return "\n".join(lines) if lines else "No additional context."


