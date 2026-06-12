<!-- spec-format: v1 -->
# se3-workflows Specification

## Purpose

Define the standard workflows for SE3 development using the Flow Engine's 13-step state machine. These workflows govern how different types of work are processed from intake to completion through the unified `se3 run` entry point.

## Requirements

### Requirement: Workflow Types

The system SHALL support six workflow types, mapped to different step sequences from the step pool:

| Type | Steps | When Used |
|------|-------|-----------|
| `feature` | analyze → plan → implement → test → self_check → verify_spec → update_spec → version_analyze → commit → summarize | New functionality or significant enhancement |
| `bugfix` | analyze → plan → implement → test → self_check → verify_spec → version_analyze → commit → summarize | Bug reports (plan uses medium depth) |
| `review` | analyze → verify_spec → summarize | Code review, audit, or analysis |
| `small` | analyze → implement → test → version_analyze → commit → summarize | Minor fixes, typos, simple changes |
| `directive` | analyze → plan → implement → version_analyze → commit → summarize | Following specific instructions (plan uses shallow depth) |
| `discovery` | discovery → analyze → plan → implement → test → self_check → verify_spec → update_spec → version_analyze → commit → summarize | Exploratory requirements gathering via multi-turn conversation, triggered by `--discover` flag |

**Step Pool (12 active steps in default sequences):**
1. **discovery** - Multi-turn requirements exploration with user, generates refined task description
2. **analyze** - Analyze task type and scope, collect project context, select and load relevant specs
3. **plan** - Unified planning: proposal + design + task breakdown (adapts depth by task_type)
4. **implement** - Write code implementation
5. **test** - Run tests to verify
6. **self_check** - LLM code review for logic completeness, robustness, functional gaps, and test coverage gaps (excludes spec compliance)
7. **verify_spec** - Check implementation vs spec
8. **update_spec** - Update spec records
9. **version_analyze** - Analyze changes to determine SemVer bump type and generate commit message
10. **commit** - Commit changes (generates template summary only as a fallback when summarize step is absent)
11. **summarize** - Generate LLM-based summary and handoff. The final step of every default task-type sequence (appended after `commit`, or after `verify_spec` for `review`).

**Dynamically-inserted step (available in pool, not in default sequences):**
- **confirm** - Review and confirm previous step output (human or LLM review gate). Inserted after configured steps via `se3.yaml` confirmation settings, not part of any default sequence.

**Note on `steps.append`:** Because `summarize` is now a default sequence member, an existing `steps.append: [summarize]` configuration entry deduplicates to a no-op — `apply_step_config` skips a name already present in the sequence, so it neither errors nor warns nor adds a second `summarize`.

**Note:** `project_summary` is deprecated — its functionality is now merged into the `analyze` step. Its deprecated handler is retained for backward compatibility with persisted flows. `read_spec` has been fully removed. The deprecated `propose`, `design`, and `plan_tasks` step types are retained for backward compatibility (their functionality is merged into the unified `plan` step).

#### Scenario: Feature workflow selection
- **WHEN** input is classified as "feature-request"
- **THEN** the system uses the feature workflow with full 10 default steps (ending in `summarize`)

#### Scenario: Bug fix workflow selection
- **WHEN** input is classified as "bug-report"
- **THEN** the system uses the bugfix workflow (plan uses medium depth)

#### Scenario: Small workflow selection
- **WHEN** the change is simple (no spec changes needed, ≤3 tasks)
- **THEN** the system uses the small workflow for efficiency

#### Scenario: Discovery workflow selection
- **WHEN** the `--discover` flag is passed to `se3 run`
- **THEN** the system uses the discovery workflow (discovery → analyze → plan → implement → test → self_check → verify_spec → update_spec → version_analyze → commit → summarize)
- **AND** the analyze step MUST NOT auto-detect "discovery" as a task type — it is exclusively triggerable via `--discover`

### Requirement: Discovery Workflow

The discovery workflow SHALL explore requirements through multi-turn conversation before proceeding with implementation:

**0. DISCOVERY** (pre-analysis exploration)
   - Engage user in multi-turn conversation to clarify requirements
   - Ask clarifying questions about scope, constraints, and desired outcomes
   - Handle evaluative/inquisitive inputs (e.g., "Is this correct?", "Review this change") with substantive assessment rather than re-asking for task definition
   - May read specs and source code to ask better-informed questions
   - Generate a refined task description (`refined_description`)
   - Await programmatic user confirmation (type "1" to confirm) before proceeding
   - MUST NOT produce implementation plans, design proposals, or code during discovery
   - Outputs: refined_description, discovery_summary, requirements_clarified

**1. ANALYZE** through **10. SUMMARIZE** — same as feature workflow after discovery completes (ending in `summarize`).

#### Scenario: Discovery with multi-turn clarification
- **WHEN** user runs `se3 run --discover "Improve performance"`
- **THEN** the system enters discovery mode and asks clarifying questions
- **AND** the conversation continues until the user confirms the refined description
- **AND** then proceeds through the full feature workflow

#### Scenario: Discovery with evaluative input
- **WHEN** user runs `se3 run --discover "Is this caching approach correct?"`
- **THEN** the system reads relevant code and engages in substantive technical discussion
- **AND** does NOT re-ask "what is the task scope" but instead probes the substance of the evaluation
- **AND** converges on a consensus correct approach through discussion

### Requirement: Feature Workflow

The feature workflow SHALL follow these steps:

**1. ANALYZE**
   - Collect structured project context via `ProjectContextCollector.collect()` (programmatic, no LLM)
   - Programmatically list available spec names
   - Single LLM call to classify task type (feature/bugfix/review/small/directive), determine scope and complexity, and select relevant spec items (`selected_items`)
   - Post-processing: programmatically load spec content (base spec auto-attached + selected items)
   - Outputs: task_type, scope, complexity, reasoning, project_summary, relevant_specs, spec_content, selected_items

**2. PLAN** (unified planning step, adapts depth by task_type)
   - Generate change proposal (summary, motivation, files, risks)
   - Create design document (architecture decisions, components, data flow)
   - Break implementation into concrete task groups
   - Estimate complexity and lines of code (`estimated_loc`) for each task
   - All produced in a single LLM call with adaptive prompt depth:
     - feature/discovery: full depth (proposal + design + tasks)
     - bugfix: medium depth (proposal + lightweight design + tasks)
     - directive/small: shallow depth (tasks only)

**3. IMPLEMENT**
   - Display structured task plan view under a `## Implementation Plan` heading (no outer Panel border) showing execution strategy, task groups with LOC estimates, and LOC summary before any LLM calls
   - Write code following the plan
   - If total estimated LOC ≤ threshold (default 300), collapse all groups into a single LLM call
   - If total estimated LOC > threshold, execute groups via DAG parallel with branch relay strategy
   - Include tests where applicable
   - Follow project conventions

**4. TEST**
   - Run test suite automatically
   - Report test results
   - If tests fail, trigger fix loop to return to implement step
   - If tests pass, continue to self_check for code review

**5. SELF_CHECK**
   - LLM reviews implementation for logic completeness, robustness, functional gaps, and test coverage gaps
   - Explicitly excludes spec compliance checks (handled by verify_spec)
   - Receives test_results and changes_made as input context
   - If critical/high issues found, trigger fix loop to return to implement step
   - If no critical/high issues, continue to verify_spec

**6. VERIFY_SPEC**
   - Check implementation against specifications
   - Verify all scenarios are covered
   - Identify any discrepancies

**7. UPDATE_SPEC**
   - Update specs to reflect changes made
   - Add new capabilities documentation
   - Mark scenarios as implemented

**8. VERSION_ANALYZE**
   - Analyze changes to determine SemVer bump type
   - Generate commit message

**9. COMMIT**
   - Stage and commit all changes
   - Use commit message from version_analyze (or fallback chain)
   - Update version according to bump rules
   - Generate template summary document only as a fallback when the summarize step is absent

**10. SUMMARIZE**
   - Generate an LLM-based summary and handoff document for the completed flow
   - Runs as the final default step; supersedes the commit step's template summary on the default path

#### Scenario: Large feature
- **WHEN** a feature is complex with multiple components
- **THEN** go through all 10 default steps with full-depth plan
- **AND** the plan includes formal proposal, design, and task groups
- **AND** the flow ends with a `summarize` step

#### Scenario: Medium feature
- **WHEN** a feature is moderately complex
- **THEN** the plan step adapts depth automatically

### Requirement: Bug Fix Workflow

The bugfix workflow SHALL follow these steps (plan uses medium depth):

**1. ANALYZE**
   - Reproduce the bug
   - Identify root cause
   - Determine affected components
   - Collect project context and load relevant specs (merged from former project_summary and read_spec steps)

**2. PLAN** (medium depth: proposal + lightweight design + tasks)
   - Generate fix proposal
   - Identify files to modify
   - Break complex fixes into task groups

**3. IMPLEMENT**
   - Fix the bug
   - Add regression tests

**4. TEST**
   - Run tests to verify fix
   - Run regression tests

**5. SELF_CHECK**
   - LLM reviews fix for logic completeness, robustness, and functional gaps
   - Ensures the fix doesn't introduce new issues or miss related changes

**6. VERIFY_SPEC**
   - Verify fix meets requirements

**7. VERSION_ANALYZE**
   - Determine version bump type and generate commit message

**8. COMMIT**
   - Commit the fix with version bump

**9. SUMMARIZE**
   - Generate an LLM-based summary and handoff document as the final default step

#### Scenario: Complex bug fix
- **WHEN** a bug requires significant changes
- **THEN** follow full bugfix workflow with plan step

#### Scenario: Simple bug fix
- **WHEN** a bug is small and easily fixed
- **THEN** analyze → implement → test → commit → summarize

### Requirement: Review Workflow

The review workflow SHALL follow minimal steps:

**1. ANALYZE**
   - Understand review scope
   - Identify what to review
   - Collect project context and load relevant specs

**2. VERIFY_SPEC**
   - Review code against specs (consumes `spec_content` directly from analyze)
   - Categorize findings by priority: critical / high / medium / low
   - Classify scope: in_scope / out_of_scope

**3. SUMMARIZE**
   - Generate an LLM-based summary and handoff of the review findings as the final default step

#### Scenario: Code review
- **WHEN** user asks for a review
- **THEN** inspect the code and report findings
- **AND** categorize issues by severity

### Requirement: Small Workflow

The small workflow SHALL be used for simple changes:

**Steps:**
1. ANALYZE - Confirm it's a small change
2. IMPLEMENT - Direct code changes
3. TEST - Run tests
4. VERSION_ANALYZE - Determine version bump and generate commit message
5. COMMIT - Commit changes
6. SUMMARIZE - Generate an LLM-based summary and handoff as the final default step

#### Scenario: Documentation update
- **WHEN** updating README or comments
- **THEN** use small workflow
- **AND** skip the plan step entirely

#### Scenario: Quick fix
- **WHEN** a one-line fix is needed
- **THEN** use small workflow for efficiency

### Requirement: Adaptive Formality

The system SHALL automatically determine formality based on change contents:

- **Large**: Full-depth plan with proposal + design + multiple task groups
- **Medium**: Medium-depth plan with proposal + tasks
- **Small**: No plan step, ≤3 tasks

The analyze step SHALL determine the appropriate level and select steps accordingly.

#### Scenario: Large change detection
- **WHEN** analysis indicates complex changes needed
- **THEN** formality is "large" with full workflow

#### Scenario: Small change detection
- **WHEN** analysis indicates trivial changes
- **THEN** formality is "small" with minimal workflow

### Requirement: Spec Guardrails

The system SHALL enforce guardrails that protect spec integrity during implementation.

**Guardrail Rules:**
1. **MUST NOT delete** an existing spec requirement without explicit human approval
2. **MUST NOT weaken** a requirement (e.g., changing "SHALL validate all inputs" to "SHOULD validate inputs")
3. **MUST NOT modify** the scenarios of a requirement being implemented

**Permitted Actions:**
- **CAN ADD** new requirements
- **CAN MODIFY** requirements not being implemented (with change proposal)
- **CAN MARK** requirements as deprecated with human approval

**Enforcement:**
- Merge-time guardrail checks block spec changes that delete, weaken, or modify existing requirements
- update_spec step validates changes are appropriate

#### Scenario: Attempt to delete requirement
- **WHEN** an agent removes a SHALL requirement from a spec during implementation
- **THEN** the system blocks the change and reports a guardrail violation

#### Scenario: Attempt to weaken requirement
- **WHEN** an agent changes "SHALL" to "SHOULD"
- **THEN** the system blocks the change and reports a guardrail violation

### Requirement: Workflow Entry Point

All workflows SHALL be accessed through the unified `se3 run` command.

**Flag Aliases:**
- `--discover` SHALL also be accepted as `-d` on `se3 run`. Both forms select the discovery workflow identically.

**Entry Patterns:**
```bash
# Feature workflow (auto-detected)
se3 run "Implement user authentication"

# Bugfix workflow  
se3 run "Fix memory leak in cache" --type=bugfix

# Review workflow
se3 run "Review the auth module" --type=review

# Small workflow
se3 run "Fix typo in README" --type=small

# Discovery workflow (only via --discover flag; short alias -d)
se3 run --discover "Explore requirements for new API"
se3 run -d "Explore requirements for new API"

# Run flow from an existing issue (by ID, or empty string for interactive selection)
se3 run --from-issue ISSUE-123
se3 run --from-issue ""
```

The analyze step SHALL auto-detect task type if not specified, but explicit type SHALL override.

#### Scenario: Entry with explicit type
- **GIVEN** user wants to run a specific workflow type
- **WHEN** user executes `se3 run "task" --type=bugfix`
- **THEN** the system uses the bugfix workflow
- **AND** uses the bugfix workflow with medium-depth plan

#### Scenario: Entry with auto-detection
- **GIVEN** user provides a task description
- **WHEN** user executes `se3 run "Implement new feature"`
- **THEN** the analyze step auto-detects the task type
- **AND** selects appropriate workflow

#### Scenario: Entry with discovery flag
- **GIVEN** user wants to explore requirements before implementation
- **WHEN** user executes `se3 run --discover "Task description"`
- **THEN** the system enters discovery workflow regardless of auto-detected type
- **AND** the discovery step engages in multi-turn conversation before proceeding to analyze

### Requirement: Loop Mode Entry

The system SHALL support a continuous task-execution mode invoked via `se3 run --loop` (alias `-l`). In loop mode, the configured task is executed repeatedly for a bounded number of iterations, with optional branch isolation via a dedicated loop branch / worktree.

**Loop-Mode Flags (on `se3 run`):**
- `--loop` / `-l` — Enable loop mode (continuous execution of the same task).
- `--max-iterations <N>` / `-n <N>` — Maximum number of iterations (default: 10). The loop SHALL stop after `N` iterations even if the task continues to succeed.
- `--no-worktree` — Disable branch isolation; iterations run directly on the current branch instead of in an isolated loop branch / worktree.
- `--merge <branch>` — Merge an existing loop branch (e.g. `loop/<slug>-<iteration>` or the legacy `se3-loop/<timestamp>`) into the current branch and exit. Implies loop-mode handling but does not start new iterations.
- `--list-loops` — List existing unmerged loop branches with commit counts and base branches, then exit without running.

**Branch Isolation:**
- When `--no-worktree` is NOT set, the loop SHALL create a new loop branch and a worktree, and execute iterations inside that worktree. The original branch is recorded so the loop branch can be merged back on completion.
- **Branch naming (new convention, default):** When a non-empty task description is supplied, the loop branch SHALL be named `loop/{slugified_task_id}-{iteration}`. `slugified_task_id` is derived from the task description by lowercasing, replacing non-alphanumeric characters with hyphens, collapsing repeated hyphens, stripping leading/trailing hyphens, and truncating to 30 characters; if the result would be empty, `task` is used in its place. `iteration` is the upcoming iteration number (starting at 1).
- **Branch naming (legacy fallback):** When no task description (or no derivable `task_id` / `iteration`) is available, the loop branch SHALL fall back to the legacy `se3-loop/{timestamp}` naming, where `timestamp` defaults to the current time in `YYYYMMDD-HHMMSS` format.
- When `--no-worktree` IS set, iterations run directly on the current branch and a banner informing the user that isolation is disabled SHALL be displayed.
- If worktree setup fails, the system SHALL fall back to non-isolated execution and display an error message indicating the fallback.

**Iteration Lifecycle:**
- Each iteration runs the same task description with the configured task type (default `pending` to allow auto-detection), via the same `run_flow` path used by single-run mode.
- Between iterations, the system SHALL invalidate cached worktree/topology state so configuration lookups reflect any worktree changes from the previous iteration.
- After each iteration, the system SHALL generate an iteration summary and feed it into the next iteration via the controller's prompt-history mechanism so successive iterations can build on prior results.
- A `KeyboardInterrupt` SHALL be treated as a user interruption: the loop stops, the loop branch is preserved, and instructions for later merging or discarding are displayed.

**Loop Completion and Merging:**
- On normal completion (max iterations reached) with commits on the loop branch, the system SHALL automatically attempt to merge the loop branch back into the original branch and report success or a merge conflict (preserving the branch for manual resolution on conflict).
- On normal completion with no commits on the loop branch, the system SHALL discard the empty loop branch / worktree.
- On interruption, the loop branch SHALL be preserved and the user SHALL be shown the commands to merge (`se3 run --loop --merge <branch>`) or discard (`git branch -D <branch>`) it later.

**Interaction with Other Flags:**
- `--loop` and `--merge` may be combined: `se3 run --loop --merge <branch>` runs the merge-existing path and exits without entering an iteration loop.
- `--from-issue`, `--resume`, and `--flow-id` are NOT compatible with loop mode; loop mode always starts fresh iterations of the supplied task.

#### Scenario: Loop mode with branch isolation (new naming)
- **GIVEN** the user is on a feature branch
- **WHEN** the user executes `se3 run --loop "Improve test coverage"`
- **THEN** the system creates a loop branch named `loop/improve-test-coverage-1` (slugified task description with the upcoming iteration number) and a worktree
- **AND** runs the task in that worktree for up to the default max iterations
- **AND** auto-merges the loop branch back into the original branch on completion if commits were made

#### Scenario: Loop mode legacy fallback naming
- **GIVEN** the user invokes loop mode without a task description (or otherwise without a derivable task_id / iteration)
- **WHEN** the loop branch is created
- **THEN** the system SHALL fall back to the legacy `se3-loop/<timestamp>` naming

#### Scenario: Loop mode with explicit iteration cap
- **WHEN** the user executes `se3 run --loop -n 3 "Refine docs"`
- **THEN** the loop runs at most 3 iterations and then reports that the maximum iteration count has been reached

#### Scenario: Loop mode without isolation
- **WHEN** the user executes `se3 run --loop --no-worktree "Quick polish"`
- **THEN** iterations execute on the current branch with no loop branch or worktree created
- **AND** a banner informs the user that isolation is disabled

#### Scenario: Listing existing loop branches
- **WHEN** the user executes `se3 run --list-loops`
- **THEN** the system lists each unmerged loop branch (matching the `loop/*` new convention or the legacy `se3-loop/*` pattern) with its commit count ahead of its base branch
- **AND** prints instructions for merging (`se3 run --loop --merge <branch>`) or discarding (`git branch -D <branch>`)
- **AND** exits without starting any iterations

#### Scenario: Merging an existing loop branch
- **WHEN** the user executes `se3 run --loop --merge loop/<slug>-<iteration>` (or, for a legacy branch, `se3 run --loop --merge se3-loop/<timestamp>`)
- **THEN** the system shows a diff summary against the current branch, prompts for confirmation, and on approval merges the loop branch into the current branch
- **AND** on merge conflict the loop branch is preserved and the user is told to resolve manually

#### Scenario: Loop interrupted by user
- **GIVEN** a loop with branch isolation is in progress
- **WHEN** the user sends Ctrl+C
- **THEN** the loop stops, the loop branch is preserved, and the system prints the commands to merge or discard the branch later

### Requirement: Run From Existing Issue

The system SHALL support starting a flow from a recorded issue via the `--from-issue` option to `se3 run`. The issue's description is used as the task description, and the issue's lifecycle status is updated based on flow outcome.

**Invocation Forms:**
- `se3 run --from-issue <ISSUE_ID>` — load the named issue directly.
- `se3 run --from-issue ""` (empty value) — interactively list all open issues and prompt the user to choose one by ID.

**Issue Resolution and Validation:**
- Issues are resolved via the project's `IssueManager` against issues persisted under the project's issue store.
- If no open issues exist in interactive selection mode, the command SHALL report "No open issues found." and exit with a non-zero status.
- If the requested issue ID does not exist, the command SHALL report a not-found error and exit with a non-zero status.
- If the requested issue is already `in-progress`, the command SHALL refuse to start a new flow and instruct the user to run `se3 issue reset <id>` first, exiting with a non-zero status.

**Status Transitions:**
- On successful selection, the issue status SHALL be transitioned to `in-progress` before the flow starts.
- After the flow completes, the issue status SHALL be updated based on the run's exit code:
  - Exit code 0 → status transitions to `resolved`.
  - Non-zero exit code → status transitions back to `open`.
- Status update failures after the flow completes SHALL NOT mask the flow's exit code (best-effort update).

**Linking to the Flow:**
- The originating issue ID SHALL be passed to the flow as `source_issue_id` so subsequent steps can correlate the flow with its source issue.

**Interaction with Other Flags:**
- `--type` selects the workflow type for the run started from the issue (default: `feature`).
- `--from-issue` is mutually exclusive with `--loop`, `--merge`, and `--resume`/`--flow-id` semantics; when `--from-issue` is provided, the run starts a new flow rather than resuming or looping.

#### Scenario: Run from a specific issue ID
- **GIVEN** an open issue with ID `ISSUE-123` exists
- **WHEN** the user executes `se3 run --from-issue ISSUE-123`
- **THEN** the system marks the issue as `in-progress`, starts a new flow using the issue's description as the task description, and passes `source_issue_id=ISSUE-123` to the flow

#### Scenario: Interactive issue selection
- **GIVEN** at least one open issue exists in the project
- **WHEN** the user executes `se3 run --from-issue ""` (with an empty value)
- **THEN** the system lists open issues with their IDs, titles, and priorities, prompts the user to enter an issue ID, and then runs the flow from the chosen issue

#### Scenario: No open issues for interactive selection
- **GIVEN** no open issues exist
- **WHEN** the user executes `se3 run --from-issue ""`
- **THEN** the system reports "No open issues found." and exits with a non-zero status

#### Scenario: Requested issue not found
- **WHEN** the user executes `se3 run --from-issue UNKNOWN-ID` and that ID does not exist
- **THEN** the system reports a not-found error and exits with a non-zero status

#### Scenario: Issue already in progress
- **GIVEN** issue `ISSUE-123` has status `in-progress`
- **WHEN** the user executes `se3 run --from-issue ISSUE-123`
- **THEN** the system refuses to start a new flow, instructs the user to run `se3 issue reset ISSUE-123` first, and exits with a non-zero status

#### Scenario: Flow success resolves the issue
- **GIVEN** a flow started via `--from-issue` finishes with exit code 0
- **THEN** the originating issue's status SHALL be updated to `resolved`

#### Scenario: Flow failure reopens the issue
- **GIVEN** a flow started via `--from-issue` finishes with a non-zero exit code
- **THEN** the originating issue's status SHALL be updated back to `open`

### Requirement: N-Pass Self-Check

The system SHALL support repeating the `self_check` step multiple consecutive times until N clean passes are achieved, controlled by the `workflow.self_check_passes_required` configuration (default: 1, must be >= 1).

**Mechanism:**
- When a `self_check` step completes, the state machine counts consecutive completed `self_check` steps within the current fix-loop round
- If the consecutive pass count is less than `self_check_passes_required`, the state machine creates an additional `self_check` step (a repeat pass) instead of advancing to the next workflow step
- Once the consecutive pass count reaches `self_check_passes_required`, the flow proceeds to `verify_spec` (or the next configured step)
- Each `self_check` step receives `self_check_pass_index` and `self_check_passes_required` as inputs and records them as outputs for observability

**Interaction with fix-loop:**
- If any `self_check` pass reports critical/high issues, the fix-loop transitions back to `implement`, resetting the consecutive-pass counter for the next round
- All N consecutive passes must be clean within the same fix-loop round to satisfy the gate

**Deferred-fix on a small number of findings (controlled by `workflow.self_check_defer_fix_threshold`, default 3; see se3-config *Workflow Configuration*):**

Rather than entering the fix loop the moment a single self_check pass surfaces any finding, a pass with only a *small* number of findings defers the fix loop and lets the remaining passes in the nested chain run first, so the whole chain's findings are collected and fixed together. The decision is made inside the self_check handler (it holds the issues, severities, `pass_index`, `passes_required`, the threshold, and the previous round's stash injected by the state machine); the state machine only shuttles the stash between `step.outputs` and `flow.state.context["self_check_deferred_issues"]` and clears it at `pass_index == 1`.

- **Defer:** when a pass's effective issue count is **strictly below** `self_check_defer_fix_threshold`, the nested chain still has an un-run later pass, and none of this pass's findings is `critical`/`high`, the pass stashes its issues and returns `COMPLETED` so the state machine creates the next self_check pass (reusing the existing "return COMPLETED → create the next pass" path), instead of returning `REVISION_NEEDED`.
- **Flush into the fix loop:** when (a) the chain has no further pass, (b) a pass reaches the threshold, or (c) a pass surfaces a `critical`/`high` finding, and the accumulated stash (plus the current pass's findings) is non-empty, the handler merges the stashed and current findings, de-duplicates them by reusing `_issue_signature` (the `(location, normalized_description)` mechanical signature) as a set key (a later issue whose signature already exists in the stash is dropped; no extra LLM call — residual near-duplicates are merged when the implement step consumes the combined list), and returns `REVISION_NEEDED` with `fix_instructions` containing **all** accumulated findings.
- **Clean chain completes:** if no pass in the whole chain produced any finding, the gate is satisfied exactly as today.
- **critical/high are never deferred:** a `critical`/`high` finding always flushes immediately regardless of count.
- After the fix loop returns and self_check re-enters, the stash is cleared and pass counting restarts from `pass_index == 1`.
- Setting `self_check_defer_fix_threshold` to `0`/`null` disables deferral and restores immediate-fix-on-any-finding.

**Effective passes_required recording.** The `self_check_passes_required` value recorded in `step.outputs` (and injected into the step inputs) SHALL be the **effective** value — when the per-round pass count is derived from the length of a nested `llm_caller.steps.self_check` chain (see se3-config *Workflow Configuration*), the recorded value is that derived chain length, NOT the default `1`. This makes `se3 history show` render the pass position correctly (e.g. `#2/2` for the second pass of a two-chain configuration) instead of `#2/1`.

#### Scenario: Single-pass self_check (default)
- **GIVEN** `workflow.self_check_passes_required` is 1 (default)
- **WHEN** a `self_check` step completes cleanly
- **THEN** the flow advances to the next workflow step (typically `verify_spec`)

#### Scenario: Multi-pass self_check progression
- **GIVEN** `workflow.self_check_passes_required` is N (N > 1)
- **WHEN** a `self_check` step completes cleanly and only K < N consecutive clean passes have occurred in the current round
- **THEN** the state machine creates an additional `self_check` step with `self_check_pass_index = K + 1`
- **AND** the flow continues to advance only after N consecutive clean passes are accumulated

#### Scenario: Multi-pass self_check reset by fix-loop
- **GIVEN** `workflow.self_check_passes_required` is N (N > 1) and one or more clean passes have already occurred in the current round
- **WHEN** a subsequent `self_check` pass reports critical/high issues and triggers a fix-loop back to `implement`
- **THEN** the consecutive-pass counter resets, and the next round must again accumulate N consecutive clean passes before progressing

#### Scenario: Few findings defer the fix loop to a later pass
- **GIVEN** `workflow.self_check_defer_fix_threshold` is 3 and the nested `self_check` chain has at least one un-run later pass
- **WHEN** a `self_check` pass reports fewer than 3 findings, none of `critical`/`high` severity
- **THEN** the pass does NOT return `REVISION_NEEDED`; its issues are stashed and the state machine creates the next `self_check` pass

#### Scenario: critical/high finding fixes immediately
- **GIVEN** `workflow.self_check_defer_fix_threshold` is 3 and a later pass remains in the chain
- **WHEN** a `self_check` pass reports a `critical` or `high` severity finding (even if the total count is below the threshold)
- **THEN** the deferral is bypassed and the pass returns `REVISION_NEEDED` immediately, flushing any stash plus the current findings into the fix loop

#### Scenario: Chain-tail flush merges accumulated findings into the fix loop
- **GIVEN** earlier passes deferred a non-empty stash of findings under the threshold
- **WHEN** the final pass of the nested chain runs (no further pass remains) and the accumulated stash plus current findings is non-empty
- **THEN** the handler returns `REVISION_NEEDED` and `fix_instructions` contains **all** accumulated findings
- **AND** the combined list is de-duplicated by `_issue_signature` so a later pass's finding whose `(location, normalized_description)` signature already exists in the stash is dropped, with no extra LLM call

#### Scenario: Deferral disabled by threshold 0/null
- **GIVEN** `workflow.self_check_defer_fix_threshold` is `0` (or `null`)
- **WHEN** a `self_check` pass reports any finding
- **THEN** the pass returns `REVISION_NEEDED` immediately on the first finding (no deferral), preserving the historical behavior

#### Scenario: Nested chain records the effective passes_required
- **GIVEN** `llm_caller.steps.self_check` is configured as a nested chain of length 2 and `workflow.self_check_passes_required` is not explicitly set
- **WHEN** a `self_check` pass executes and writes its outputs
- **THEN** `step.outputs["self_check_passes_required"]` equals 2 (the nested chain length), not the default 1
- **AND** `se3 history show` renders the pass position as `#i/2`

### Requirement: Step Retry and Recovery

The system SHALL handle step failures with retry and recovery options.

**Retry Policy:**
- On each step failure, prompt the user: retry / skip / abort
- User-initiated retries are tracked via `retry_count`
- Once `retry_count` has reached the max (3), the flow auto-fails without prompting on the next failure
- User can skip failed step and continue

**Recovery:**
- State is persisted after each step
- Interrupted flows can be resumed with `se3 run --resume`
- Ctrl+C allows prompt injection before retry

#### Scenario: Step failure within retry budget
- **WHEN** a step fails and `retry_count` is below the max (3)
- **THEN** user is prompted to retry, skip, or abort

#### Scenario: Max retries reached
- **WHEN** a step fails and `retry_count` has reached the max (3)
- **THEN** the flow auto-fails without prompting the user

#### Scenario: Flow interruption
- **WHEN** flow is interrupted mid-step
- **THEN** state is saved automatically
- **AND** `se3 run --resume` continues from interruption point

### Requirement: Unlimited Fix Iterations Mode

The system SHALL support an unlimited fix-loop mode that disables the per-flow fix-iteration budget, allowing the validate→implement fix loop to retry indefinitely until it either succeeds or is interrupted by the user.

**Sentinel Value:**
- The `workflow.max_fix_iterations` configuration value SHALL accept non-positive integers (e.g. `0` or any value `<= 0`) as a sentinel that disables the iteration budget.
- When the resolved `max_fix_iterations` for a flow is `<= 0`, the state machine SHALL treat the budget as unlimited and SHALL NOT trigger the fix-loop exhaustion path regardless of how many fix iterations have occurred.

**Interaction with Exhaustion Behavior:**
- The fix-loop exhaustion condition (which marks the flow `FAILED` and invokes `IssueDiscovery.create_from_fix_loop_exhaustion`) SHALL fire only when `max_fix_iterations > 0` and the current iteration count has reached or exceeded that budget.
- Under unlimited mode (`max_fix_iterations <= 0`), the flow SHALL continue cycling between the validating step (`TEST`, `SELF_CHECK`, or `VERIFY_SPEC`) and `implement` for as long as `REVISION_NEEDED` is returned, with no automatic failure transition.

**Interaction with Fix History Cap:**
- Because unlimited mode can produce an unbounded number of fix iterations, the [[fix_history_cap]] sliding-window cap on `fix_history` SHALL still apply so that persisted state and per-step input copies remain bounded even when no iteration cap is enforced.

#### Scenario: Unlimited fix iterations via zero sentinel
- **GIVEN** `workflow.max_fix_iterations` resolves to `0` (or any value `<= 0`)
- **WHEN** a validating step (`TEST`, `SELF_CHECK`, or `VERIFY_SPEC`) returns `REVISION_NEEDED` after many prior fix iterations
- **THEN** the state machine SHALL transition back to `implement` for another fix iteration regardless of how many iterations have already run
- **AND** the flow SHALL NOT be marked `FAILED` due to fix-loop exhaustion

#### Scenario: Bounded fix iterations still trigger exhaustion
- **GIVEN** `workflow.max_fix_iterations` resolves to a positive integer N
- **WHEN** the fix-loop iteration count reaches or exceeds N and the current validating step returns `REVISION_NEEDED`
- **THEN** the state machine SHALL invoke `IssueDiscovery.create_from_fix_loop_exhaustion` and transition the flow to `FAILED` as specified in the workflow integration with issue discovery

### Requirement: Fix History Sliding-Window Cap

The flow state SHALL retain a bounded history of fix-loop iterations so that the persisted state, in-memory footprint, and per-step input copies cannot grow unboundedly under unlimited fix-loop mode (`max_fix_iterations=0`).

**Cap Behavior:**
- The `fix_history` list on the flow state SHALL be capped at `FIX_HISTORY_MAX_ENTRIES` entries (default: 100).
- When recording a new fix-loop iteration would exceed the cap, the oldest entries SHALL be discarded and only the most recent `FIX_HISTORY_MAX_ENTRIES` entries SHALL be retained.
- The cap SHALL be enforced each time a fix iteration is appended, both on the in-memory list and on the `context["fix_history"]` mirror used by downstream steps.

**Rationale and Consumer Tolerance:**
- The full `fix_history` list is persisted to the engine state file on every save and deep-copied into each step's inputs at transition time, so an uncapped list under a stuck loop would inflate disk size, memory, and deepcopy cost linearly with iteration count.
- All downstream consumers (e.g. `verify_spec` / `self_check` tail-truncating to the last 20 entries, `issue_discovery` using the last 5) care only about recency, so retaining the most recent entries SHALL be the policy when truncation is required.

#### Scenario: Fix history truncated to the cap
- **GIVEN** a fix-loop is running in unlimited mode and has already accumulated `FIX_HISTORY_MAX_ENTRIES` (100) entries
- **WHEN** the next fix iteration is recorded
- **THEN** the `fix_history` list SHALL contain exactly `FIX_HISTORY_MAX_ENTRIES` entries
- **AND** the oldest entry SHALL have been discarded so the most recent iteration is preserved

#### Scenario: Capped history mirrored into context
- **WHEN** the sliding-window cap truncates `fix_history`
- **THEN** the same truncated list SHALL be reflected in `state.context["fix_history"]` so steps consuming it via flow inputs see the bounded view

### Requirement: Mid-Flow User Interjections

The system SHALL allow the user to inject additional instructions into a running flow via Ctrl+C, and SHALL persist those instructions so they apply both to the immediate retry of the interrupted step and to every subsequent step (including fix-loop iterations) within the same flow.

**Capture Mechanism:**
- When the user presses Ctrl+C during step execution, the run loop SHALL prompt the user for an additional instruction via a multiline input prompt.
- Submitting an empty input SHALL retry the interrupted step as-is, without persisting any interjection.
- Cancelling the input prompt (a second Ctrl+C) SHALL save flow state and exit, with a hint that the flow can be resumed via `se3 run --resume`.
- A non-empty input SHALL be appended as a new entry to `flow.state.context["user_interjections"]`.

**Persisted Entry Shape:**
- Each interjection entry SHALL record the user-typed `text`, the `step_id` and `step_type` of the step that was interrupted, and an ISO-format `timestamp` of when the interjection was captured.

**Effective Task Description:**
- The effective `task_description` passed to every step SHALL be composed from:
  1. The canonical `flow.task_description`, unless overridden by a completed DISCOVERY step's `refined_description`.
  2. The full ordered list of persisted `user_interjections`, appended as a single `## Additional Instructions (added during run)` section. Each entry is rendered as a bullet with an optional `[step_type@timestamp]` prefix.
- An empty interjection list SHALL leave the base task description unchanged (no `## Additional Instructions` section is appended).
- Re-composition during retry SHALL always be performed against the un-decorated base (canonical description or discovery's refined description) — never against the step's already-composed `task_description` — to prevent doubling the `## Additional Instructions` section on successive interruptions.

**Propagation:**
- On Ctrl+C with a non-empty interjection, the interrupted step's `inputs["task_description"]` SHALL be mutated in-place and the step SHALL be reset to `PENDING` so it re-runs with the new instruction.
- Subsequent newly-constructed steps SHALL receive the same composed `task_description` via the state machine's input-building path.
- Fix-loop transitions back to `implement` SHALL also re-compose the `task_description` so prior interjections (and any discovery refinement) are not silently dropped on a fix iteration.
- A deep copy of the persisted `user_interjections` list SHALL also be supplied to applicable steps as an input field, so step handlers (such as `self_check`) can incorporate the raw interjection list into their own prompts.

#### Scenario: Ctrl+C with an additional instruction
- **GIVEN** a flow is executing a step
- **WHEN** the user presses Ctrl+C and types a non-empty instruction at the additional-instruction prompt
- **THEN** the instruction is appended to `flow.state.context["user_interjections"]` with the step_id, step_type, and timestamp
- **AND** the interrupted step's `task_description` input is recomposed to include an `## Additional Instructions (added during run)` section containing every recorded interjection
- **AND** the step is reset to `PENDING` and re-runs with the updated input

#### Scenario: Ctrl+C with an empty instruction
- **WHEN** the user presses Ctrl+C and submits an empty input at the additional-instruction prompt
- **THEN** no interjection entry is recorded and the interrupted step is retried as-is

#### Scenario: Ctrl+C cancelled at the prompt
- **WHEN** the user presses Ctrl+C again at the additional-instruction prompt to cancel
- **THEN** the flow state is saved and the run exits with a hint that it can be resumed via `se3 run --resume`

#### Scenario: Interjection persists across subsequent steps
- **GIVEN** a user interjection was recorded during an earlier step
- **WHEN** the flow advances to any later step (including a fix-loop iteration back to `implement`)
- **THEN** the later step's `task_description` input SHALL include the same composed `## Additional Instructions (added during run)` section so the interjection remains visible

#### Scenario: Multiple interjections accumulate without duplication
- **GIVEN** at least one interjection is already recorded on the flow
- **WHEN** the user interrupts another step and submits a second non-empty instruction
- **THEN** both interjections appear as separate bullets in a single `## Additional Instructions (added during run)` section
- **AND** the section is not emitted twice, even though the interrupted step's previous `task_description` already carried the earlier interjection

### Requirement: Confirmation Step Configuration

The system SHALL support inserting `confirm` review gates after configured workflow steps via per-step entries under `confirmation.steps` in `se3.yaml` (project) and `~/.se3/config.yaml` (global).

**Config Schema (per reviewed step):**
```yaml
confirmation:
  steps:
    <step_name>:               # e.g. plan, implement, verify_spec
      reviewer: <mode>         # optional; see modes below
      max_iterations: <int>    # optional positive integer; default 3
```

**Merge Behavior:**
- Project entries override global entries with the same key; non-conflicting entries from both sources coexist.
- Only steps that appear as keys under `confirmation.steps` get a `confirm` step inserted after them. There is no global on/off switch.
- Unknown fields under a step entry SHALL be ignored with a one-shot warning. Non-mapping entries and the legacy list form of `confirmation.steps` SHALL be ignored with a warning.

**Reviewer Modes:**
1. `reviewer: human` — the confirm step writes a call file under `se3/calls/`, returns `PAUSED`, and the run loop prompts the user interactively. Resume detects an existing `.response` file and resolves the step from its `approved` / `feedback` fields.
2. `reviewer: <agent_name>` — the named agent is resolved against the top-level `agents:` / `claude_commands:` registry. Unknown agent names SHALL raise a fail-fast error at confirm-step transition time. The agent is invoked synchronously via `LLMCaller` to produce a JSON review verdict.
3. `reviewer:` omitted or null — the LLM reviewer path runs using the `llm_caller.defaults` chain.

**max_iterations (LLM reviewer paths only):**
- Bounds the number of review-modify cycles between the `confirm` step and the reviewed step.
- Non-positive or non-integer values SHALL be rejected with a warning and replaced by the default (3).
- When the review iteration count reaches `max_iterations`, the LLM reviewer SHALL auto-approve the reviewed step with a feedback message recording the cap, so the workflow cannot stall indefinitely.
- On the `human` reviewer path, `max_iterations` is carried through inputs uniformly but does not cap the human review loop.

**Revision Loop Mechanics:**
- When a `confirm` step returns a verdict of `approved: false`, the state machine SHALL transition back to the reviewed step with `is_revision=True` and `revision_feedback` set to the reviewer's feedback string, so the reviewed step can revise its output.
- When `approved: true`, the flow advances to the next configured step.

**Deprecated Keys (ignored, one-shot warned):**
- `confirmation.enabled` — superseded by the presence of `confirmation.steps.<step>` entries.
- Top-level `confirmation.reviewer` — moved to per-step `confirmation.steps.<step>.reviewer`.
- `confirmation.llm_reviewer` — LLM agents are now defined under top-level `agents:` and referenced by name.

#### Scenario: Human reviewer pauses flow
- **GIVEN** `confirmation.steps.plan.reviewer` is `human`
- **WHEN** the `plan` step completes and the inserted `confirm` step runs
- **THEN** the system writes a call file under `se3/calls/`, returns `PAUSED`, and waits for a corresponding `.response` file before continuing

#### Scenario: Named-agent LLM reviewer
- **GIVEN** `confirmation.steps.implement.reviewer` is the name of an agent declared under top-level `agents:`
- **WHEN** the inserted `confirm` step runs after `implement`
- **THEN** the system invokes that single agent synchronously to produce a JSON `{approved, feedback}` verdict
- **AND** an `approved: false` verdict transitions the flow back to `implement` with `is_revision=True` and `revision_feedback` set to the reviewer's feedback

#### Scenario: Default LLM reviewer fallback
- **GIVEN** `confirmation.steps.verify_spec` is present but its `reviewer` key is absent or null
- **WHEN** the inserted `confirm` step runs
- **THEN** the system uses the `llm_caller.defaults` agent chain for the review call

#### Scenario: LLM review iteration cap auto-approves
- **GIVEN** `confirmation.steps.<step>.max_iterations` is N (default 3) on an LLM reviewer path
- **WHEN** the review-modify cycle reaches N iterations without approval
- **THEN** the `confirm` step auto-approves with a feedback message noting the cap so the workflow advances

#### Scenario: Unknown reviewer agent fails fast
- **GIVEN** `confirmation.steps.<step>.reviewer` references an agent name not present in `agents:` / `claude_commands:`
- **WHEN** the `confirm` step transitions for that reviewed step (or for any other entry validated alongside it on resume)
- **THEN** the system SHALL raise a fail-fast error rather than silently fall through

#### Scenario: Deprecated confirmation keys warned and ignored
- **GIVEN** a config file contains `confirmation.enabled`, top-level `confirmation.reviewer`, or `confirmation.llm_reviewer`
- **WHEN** the configuration is loaded
- **THEN** the keys SHALL be ignored, the new schema SHALL still apply, and a one-shot warning per (source, key) SHALL be logged

### Requirement: Workflow Integration with Issue Discovery

The state machine SHALL integrate with the issue-discovery subsystem so that issues are automatically created during workflow execution without requiring explicit workflow-step authoring. The detailed semantics of issue discovery (priorities, deduplication, tagging, prompt injection contents) are defined in the `issue-discovery` spec; this requirement records how the state machine drives those mechanisms.

**B-class Collection Hook (post-step):**
- After every step finishes execution with status `COMPLETED` or `PARTIAL`, the state machine SHALL inspect the step's `outputs` for a `discovered_issues` field.
- If a non-empty `discovered_issues` value is present, the state machine SHALL obtain the flow's `IssueDiscovery` instance (lazily, scoped to the flow's `flow_id`) and invoke `collect_issues_from_output(flow, step_type, step_outputs)` to persist any discovered issues that pass the discovery subsystem's whitelist and deduplication checks.
- Exceptions raised during collection SHALL be logged as warnings and SHALL NOT fail the step or the flow.

**A-class Trigger on Fix-Loop Exhaustion:**
- When the validate→implement fix loop has its exhaustion condition met inside `transition_to_next` — i.e. a step in `{TEST, SELF_CHECK, VERIFY_SPEC}` returned `REVISION_NEEDED`, the fix-loop budget is not unlimited (`max_fix_iterations > 0`), and the current iteration has reached or exceeded that budget — the state machine SHALL invoke `IssueDiscovery.create_from_fix_loop_exhaustion(flow, current_step)` before marking the flow `FAILED` and returning.
- Exceptions raised during exhaustion-issue creation SHALL be logged as warnings and SHALL NOT prevent the flow from being marked `FAILED`.

**Lifecycle:**
- The `IssueDiscovery` instance used by both hooks SHALL be the same instance for the duration of a given flow, so its in-flow deduplication state is preserved across steps.

#### Scenario: Discovered issues collected after a whitelisted step
- **GIVEN** a step on the issue-discovery whitelist completes with `discovered_issues` populated in its outputs
- **WHEN** the state machine finishes executing the step
- **THEN** the state machine SHALL call `IssueDiscovery.collect_issues_from_output` with the step type and outputs so the discovery subsystem can persist any non-duplicate issues

#### Scenario: Non-whitelisted step outputs do not create issues via collection
- **GIVEN** a step that is not on the issue-discovery whitelist completes, even if `discovered_issues` happens to be present in its outputs
- **THEN** the discovery subsystem's whitelist check SHALL prevent persistence of those issues (the state machine still invokes the collection hook; the subsystem is responsible for ignoring non-whitelisted step types)

#### Scenario: Fix-loop exhaustion creates an issue then fails the flow
- **WHEN** the fix-loop exhaustion condition is reached inside `transition_to_next`
- **THEN** the state machine SHALL invoke `IssueDiscovery.create_from_fix_loop_exhaustion(flow, current_step)` before transitioning the flow to `FAILED`
- **AND** any exception from that call SHALL be logged as a warning rather than propagated

### Requirement: Flow Instance Baseline Commit and Source Issue Linkage

Each FlowInstance SHALL maintain optional bookkeeping fields that allow the state machine to correlate the flow with its starting git state and originating issue.

**Baseline Commit Tracking:**
- A FlowInstance SHALL expose a `baseline_commit` field that records the git commit SHA the flow began from.
- The state machine SHALL record the baseline commit when the flow starts (and re-record it as needed for multi-worktree scenarios where the working directory may differ from the original launch location).
- Downstream steps that compute "changes made" (e.g. version_analyze, commit) SHALL use this baseline as the lower bound for `git diff` so the change set reflects work produced by the flow rather than unrelated history.
- The baseline SHALL be persisted alongside the rest of the flow state so resumed flows continue to diff against the same starting point.

**Source Issue Linkage:**
- A FlowInstance SHALL expose a `source_issue_id` field that records the ID of the issue the flow was started from (if any).
- When a flow is started via `se3 run --from-issue <ID>`, the originating issue ID SHALL be stored in this field and made available to step handlers via flow inputs.
- The field SHALL be `None` for flows that were not started from an issue.

#### Scenario: Baseline commit recorded at flow start
- **WHEN** a flow begins execution
- **THEN** the state machine records the current git commit SHA on the FlowInstance's `baseline_commit` field
- **AND** subsequent `git diff` computations within the flow use that SHA as the lower bound

#### Scenario: Baseline commit persists across resume
- **GIVEN** a flow has recorded a `baseline_commit` and was then interrupted
- **WHEN** the flow is resumed via `se3 run --resume`
- **THEN** the same `baseline_commit` value SHALL be present on the resumed FlowInstance so change detection remains stable

#### Scenario: Source issue ID stored on flow
- **GIVEN** a flow is started via `se3 run --from-issue ISSUE-123`
- **THEN** the FlowInstance's `source_issue_id` field SHALL equal `ISSUE-123`

#### Scenario: No source issue for ordinary runs
- **GIVEN** a flow is started without `--from-issue`
- **THEN** the FlowInstance's `source_issue_id` SHALL be `None`
