<!-- spec-format: v1 -->
# requirement-intake Specification

## Purpose

Define the requirement intake process for SE3, governing how new requirements enter the system through the unified `se3 run` entry point.

## Requirements

### Requirement: Requirement Intake via se3 run

The system SHALL accept new requirements through the `se3 run` command interface.

**Intake methods:**

**Method 1: Direct Task Description**
- Command: `se3 run "Implement user authentication"`
- Flow: Task description → analyze step → workflow execution
- Use for: Clear, well-defined tasks

**Method 2: Discovery Mode**
- Command: `se3 run --discover "I want to build something..."`
- Flow: Multi-turn exploration → refined description → analyze step
- Use for: Vague ideas that need clarification

**Method 3: Resume Existing Flow**
- Command: `se3 run --resume`
- Flow: discover resumable flows → interactive selection (when multiple) → load persisted state → continue from interrupted step
- Use for: Continuing interrupted work or retrying failed flows
- Selection behavior: When multiple resumable flows exist, the user is presented with an interactive picker; when exactly one exists, it is auto-detected for convenience

#### Scenario: Direct task intake
- **WHEN** user executes `se3 run "Implement feature X"`
- **THEN** the flow engine creates a new flow instance
- **AND** starts execution from the analyze step

#### Scenario: Discovery mode intake
- **WHEN** user executes `se3 run --discover "Idea"`
- **THEN** the flow engine starts discovery step
- **AND** explores requirements through conversation
- **AND** proceeds to analyze after user confirms refined description

#### Scenario: Resume flow
- **WHEN** user executes `se3 run --resume`
- **THEN** the flow engine loads the active flow state
- **AND** continues execution from the interrupted step

#### Scenario: Interactive resume with multiple resumable flows
- **GIVEN** multiple resumable flows exist (status is not COMPLETED)
- **WHEN** user executes `se3 run --resume`
- **THEN** the system displays an interactive picker listing all resumable flows with their descriptions, current step, and status
- **AND** each flow is numbered for selection
- **AND** a "Start new flow" option is appended as the last choice
- **AND** the user's numeric selection resumes the chosen flow
- **AND** selecting "Start new flow" bypasses resume and starts a new flow

#### Scenario: Resume auto-detects single active flow
- **GIVEN** exactly one resumable flow exists
- **WHEN** user executes `se3 run --resume`
- **THEN** the system auto-detects the flow and displays its ID, description, and current step
- **AND** prompts the user to choose between resuming that flow and starting a new flow
- **AND** does not present a multi-entry picker

#### Scenario: Retry option for FAILED flow on resume
- **GIVEN** exactly one resumable flow exists and its status is FAILED
- **WHEN** user executes `se3 run --resume`
- **THEN** the system presents the flow with a "Retry failed flow" action label instead of "Resume this flow"
- **AND** the FAILED status is indicated in the display
- **AND** choosing the retry action resumes the flow as normal

#### Scenario: Multiple resumable flows include FAILED entries
- **GIVEN** multiple resumable flows exist, some with FAILED status
- **WHEN** the interactive picker is displayed
- **THEN** FAILED flows are shown with a `[FAILED]` status tag appended to their description line
- **AND** FAILED flows are selectable for retry just like non-failed flows

#### Scenario: No resumable flows found
- **GIVEN** no flows with non-COMPLETED status exist (all flows are COMPLETED, or no flows exist at all)
- **WHEN** user executes `se3 run --resume`
- **THEN** the system notifies the user that no in-progress flows were found
- **AND** auto-falls back to starting a new flow without presenting a picker

### Requirement: Task Type Classification

The system SHALL associate every flow with a task type, defaulting to `feature` for direct `se3 run` invocations and supporting explicit override or opt-in auto-classification.

**Task Types:**
- `feature` - New functionality or significant enhancement (default for `se3 run`)
- `bugfix` - Fixing a bug or issue
- `review` - Code review, audit, or analysis
- `small` - Minor fix, typo, or simple change
- `directive` - Following specific instructions
- `discovery` - Exploratory intake for vague or unclear requirements; assigned automatically when `--discover` is passed
- `pending` - Sentinel value indicating that the user has opted into auto-classification by the analyze step

**Type Resolution:**
- The CLI `--type` flag defaults to `feature`. When the resolved task type is anything other than `pending`, it is treated as an explicit user choice and recorded on the flow as `explicit_type` in the flow state context.
- When `--discover` is supplied, the task type is forced to `discovery` regardless of the `--type` value.
- When the user explicitly passes `--type pending`, no `explicit_type` is recorded and the analyze step is responsible for classifying the task based on its description.

**Classification Factors (analyze step, when type is `pending`):**
- Scope of changes
- Complexity
- Need for design documentation
- Test requirements

#### Scenario: Default task type is feature
- **GIVEN** user executes `se3 run "Add user authentication system"` with no `--type` flag
- **WHEN** the flow is initialized
- **THEN** the task type is set to `feature` by default
- **AND** `explicit_type` is recorded as `feature` on the flow
- **AND** the analyze step does not override the task type

#### Scenario: Opt-in auto-classification with --type pending
- **GIVEN** user executes `se3 run "Fix typo in README" --type=pending`
- **WHEN** analyze step executes
- **THEN** the analyze step classifies the task (e.g. as `small`)
- **AND** no `explicit_type` is recorded prior to classification

#### Scenario: Discovery classification via --discover flag
- **GIVEN** user executes `se3 run --discover "I want to build something..."`
- **WHEN** the flow is initialized
- **THEN** task type is set to `discovery` regardless of description content
- **AND** the discovery-specific step sequence is selected

### Requirement: Discovery Task Step Sequence

The system SHALL provide a dedicated step sequence for tasks of type `discovery`.

**Discovery Step Sequence:**
The discovery task type uses a step sequence beginning with a DISCOVERY step, followed by ANALYZE, PLAN, IMPLEMENT, and subsequent workflow steps.

**Behavior:**
- The DISCOVERY step performs multi-turn exploration to refine vague requirements into an actionable description.
- After DISCOVERY completes, the flow continues through the standard workflow steps (ANALYZE onward).
- The discovery sequence is selected by `get_default_step_sequence` when the task type is `discovery`.

#### Scenario: Discovery sequence execution
- **GIVEN** a flow is created with task type `discovery`
- **WHEN** the flow engine selects the step sequence
- **THEN** the sequence begins with the DISCOVERY step
- **AND** proceeds to ANALYZE after discovery completes
- **AND** continues through PLAN, IMPLEMENT, and remaining workflow steps

### Requirement: Discovery Programmatic Confirmation Gate

The system SHALL require explicit user confirmation via a programmatic gate before the DISCOVERY step completes and transitions to ANALYZE, preventing unconfirmed or partially-refined requirements from proceeding.

**Trigger:**
- After the discovery handler's LLM interaction produces a refined description and the handler determines the description is ready for confirmation, a confirmation panel is displayed showing the proposed description and the step outputs set `awaiting_programmatic_confirm` to `True`.
- The confirmation gate is entered automatically; the user does not need to pass an additional flag.

**Confirmation Input Rules:**
- The user types exactly the single character `1` (strict equality after stripping trailing newlines) to confirm and proceed.
- Empty or whitespace-only input is a no-op: the confirmation panel is re-displayed and the prompt loops.
- Any other non-empty input is treated as continuing discovery: the `awaiting_programmatic_confirm` flag is cleared, the user's input is used as the next discovery turn (no separate prompt for questions), and the flow returns to the discovery interaction loop.

**Cancellation:**
- Ctrl+C or EOF (non-interactive empty input) during the confirmation prompt saves the flow state and exits with a "Discovery paused" message directing the user to resume via `se3 run --resume`.

**On Confirm:**
- When the user confirms by typing `1`, the step's `inputs["programmatic_confirmed"]` is set to `True` and the programmatic confirm sentinel is returned, causing the flow engine to transition the discovery step to COMPLETED and proceed to the next step (ANALYZE).

#### Scenario: User confirms discovery proposal
- **GIVEN** the discovery step has produced a refined description and displays the confirmation panel
- **WHEN** the user types `1` and presses Enter
- **THEN** the step's `programmatic_confirmed` input is set to `True`
- **AND** the discovery step completes
- **AND** the flow transitions to the ANALYZE step

#### Scenario: User provides feedback instead of confirming
- **GIVEN** the discovery step is showing the confirmation panel
- **WHEN** the user types any non-empty input other than `1`
- **THEN** the input is treated as the next discovery turn
- **AND** the `awaiting_programmatic_confirm` flag is cleared
- **AND** the flow continues the discovery interaction loop with the user's feedback

#### Scenario: Empty input re-displays confirmation
- **GIVEN** the discovery step is showing the confirmation panel
- **WHEN** the user submits empty or whitespace-only input
- **THEN** the confirmation panel is re-displayed
- **AND** the prompt loops for new input

#### Scenario: User cancels at confirmation gate
- **GIVEN** the discovery step is showing the confirmation panel
- **WHEN** the user presses Ctrl+C or sends EOF
- **THEN** the flow state is saved
- **AND** a "Discovery paused" message is displayed with resume instructions
- **AND** the run exits cleanly

### Requirement: Discovery refined_description Clean-Final Invariant

The discovery step prompts SHALL enforce, at the prompt layer, that the `refined_description` field (the **Proposed Task Description**) is always a clean, finalized, directly-executable task description with ZERO open items. This invariant complements the [[Discovery Programmatic Confirmation Gate]] by guaranteeing that whatever reaches the gate is confirmable as-is.

**Hard invariant (encoded in both prompt variants):**
- Both the initial-round prompt (`_INITIAL_DISCOVERY_PROMPT_SUFFIX`) and the continue-round prompt (`_CONTINUE_DISCOVERY_PROMPT_SUFFIX`) in `src/se3/engine/steps/discovery.py` SHALL state — in both the JSON schema field descriptions and the `Guidelines` section — that `refined_description` MUST NOT contain any open-item phrasing whatsoever.
- Forbidden open-item phrasings include (non-exhaustively): "to be confirmed", "TBD", "to be decided", "to be determined", "to be supplemented", "open question(s)", "pending", "either A or B (undecided)", and their Chinese equivalents (`待确认`, `待定`, `待补充`, `二选一未决`).
- No matter that is not yet settled may survive inside `refined_description` in the form of a "question".

**Two destinations for unsettled matters:**
- **True blocker** — an item that cannot proceed at all without the user's adjudication — SHALL be placed in `questions`. A non-empty `questions` keeps discovery looping and does NOT reach the confirmation gate; as long as a genuine open decision remains, the user is never asked to confirm.
- **Non-blocker** — an item for which the LLM can reasonably pick a sensible default or make the decision itself — SHALL be written into `refined_description` as an already-made decision (e.g. "Decided: use default value X"), with the meta-note that "this is a default picked on the user's behalf and can be changed" placed in the `content` field for the user's reference. Non-blockers SHALL NOT be placed in `questions`.

**Implementation constraints:**
- The invariant is enforced purely at the prompt layer (field descriptions plus `Guidelines`). The system SHALL NOT add any programmatic keyword fallback validation (e.g. scanning for "TBD"/`待确认` strings) to enforce it.
- No new schema field is introduced; the existing `mode`, `content`, `questions`, `refined_description`, (and continue-round `ready_to_proceed`), `thinking` fields are unchanged.
- The binary routing remains `refined_description and not questions` → confirmation gate, otherwise continue the discovery loop (see [[Discovery Programmatic Confirmation Gate]]). The routing logic itself is unchanged by this requirement.

**Resulting invariant on the gate:** `questions` is either non-empty (all entries are true blockers, discovery keeps looping, the gate is not entered) or empty (in which case `refined_description` is already a clean, finalized description that can be cleanly confirmed with a single `1`). This eliminates the situation where the user is asked to confirm a description that still carries unresolved items.

#### Scenario: Non-blocker resolved as an in-description decision
- **GIVEN** the discovery LLM identifies a detail it can reasonably default (a non-blocker)
- **WHEN** it produces its response
- **THEN** the defaulted detail is written into `refined_description` as an already-made decision rather than as a question
- **AND** a meta-note explaining the default was picked on the user's behalf and is changeable is placed in `content`
- **AND** the detail does NOT appear in `questions`

#### Scenario: True blocker keeps discovery looping
- **GIVEN** the discovery LLM identifies an item that cannot proceed without user adjudication (a true blocker)
- **WHEN** it produces its response
- **THEN** the blocker is placed in `questions` and `questions` is non-empty
- **AND** the binary routing keeps the flow in the discovery loop without entering the confirmation gate

#### Scenario: refined_description carries no open-item phrasing
- **WHEN** the discovery step routes a `refined_description` to the confirmation gate (`questions` is empty)
- **THEN** the `refined_description` contains no "to be confirmed" / "TBD" / "to be decided" / `待确认` / `待定` / undecided either-or phrasing
- **AND** the user can confirm it as-is by typing `1`

### Requirement: Flow State Persistence

The system SHALL persist flow state after each step for resumability.

**Persistence:**
- State stored in `se3/state/engine.json`
- Each step completion updates the state
- Flow can be resumed from any step

#### Scenario: Interrupt and resume
- **GIVEN** a flow is executing the implement step
- **WHEN** user interrupts (Ctrl+C)
- **THEN** current state is persisted
- **AND** next `se3 run --resume` continues from implement step

### Requirement: Loop Mode Intake

The system SHALL support a loop mode for continuous, repeated execution of a single task description across multiple iterations.

**Activation:**
- Command: `se3 run --loop "Task description"` (alias `-l`)
- The same task prompt is re-executed each iteration; an iteration summary is generated and injected as context for the next round.

**Loop Control Flags:**
- `--loop` / `-l` — Enable loop mode (continuous task execution).
- `--max-iterations <N>` / `-n <N>` — Maximum number of iterations (default `10`). When `N <= 0`, the loop runs without an iteration cap until interrupted.
- `--no-worktree` — Disable branch isolation; iterations run directly on the current branch instead of in a dedicated loop worktree.
- `--merge <branch>` — Merge an existing loop branch (e.g. `se3-loop/20260324-120000`) into the current branch and exit, without starting a new loop.
- `--list-loops` — List existing unmerged loop branches (with commit count ahead of base) and exit.

**Branch Isolation:**
- By default, loop mode creates a dedicated branch and worktree (e.g. `se3-loop/<timestamp>`) so that iteration changes are isolated from the user's working branch.
- The original branch is recorded so that loop changes can be merged back when the loop finishes.
- If worktree setup fails, the system falls back to non-isolated execution on the current branch and warns the user.
- When `--no-worktree` is supplied, isolation is skipped entirely and iterations run on the current branch.

**Iteration Lifecycle:**
- Each iteration invokes the standard flow engine with the supplied task description and task type.
- After each iteration, an LLM-generated summary of changes, test results, and remaining issues is produced and added to the loop context for subsequent iterations (with a deterministic fallback if generation fails).
- The loop terminates when the maximum iteration count is reached, when the user interrupts via Ctrl+C, or when `--merge` is invoked in standalone mode.
- On completion or interruption, the system handles loop finish (including optional merge of the loop branch back into the original branch).

#### Scenario: Loop mode with default iteration cap
- **WHEN** user executes `se3 run --loop "Improve test coverage"`
- **THEN** the system creates a loop branch and worktree
- **AND** repeatedly executes the task up to 10 iterations (the default `--max-iterations`)
- **AND** injects an iteration summary as context for each subsequent iteration

#### Scenario: Loop mode without branch isolation
- **WHEN** user executes `se3 run --loop --no-worktree "Refactor module"`
- **THEN** loop iterations execute directly on the current branch
- **AND** no dedicated loop worktree is created

#### Scenario: Listing existing loop branches
- **WHEN** user executes `se3 run --list-loops`
- **THEN** the system displays all unmerged `se3-loop/*` branches with commit counts ahead of their base branches
- **AND** exits without starting a new flow

#### Scenario: Merging an existing loop branch
- **WHEN** user executes `se3 run --loop --merge se3-loop/20260324-120000`
- **THEN** the system shows a diff summary of the loop branch versus the current branch
- **AND** prompts the user to confirm the merge
- **AND** merges the loop branch into the current branch on confirmation, then exits

#### Scenario: Loop interruption
- **GIVEN** a loop is executing iterations
- **WHEN** the user interrupts via Ctrl+C
- **THEN** the loop terminates gracefully
- **AND** the post-loop finish handler runs to manage the loop branch state

### Requirement: Run From Existing Issue

The system SHALL support starting a flow from an existing issue via the `--from-issue` flag.

**Activation:**
- Command: `se3 run --from-issue <issue-id>` — start a flow from a specific issue ID.
- Command: `se3 run --from-issue ""` (empty value) — enter interactive selection mode that lists all open issues and prompts the user to choose one.
- The flow is initialized using the specified issue's description as the task description.

**Issue Linkage:**
- The flow is linked to the originating issue via a `source_issue_id` reference recorded on the flow.
- This linkage allows downstream steps and tooling to attribute the flow's work back to the source issue.

**Issue Status Lifecycle:**
- When a flow is started from an issue, the issue's status is set to `in-progress` before flow execution begins.
- If the issue is already `in-progress`, the command aborts with an error directing the user to reset the issue first (e.g. `se3 issue reset <issue-id>`).
- If the issue ID cannot be found, the command aborts with an error.
- On successful flow completion (exit code 0), the issue status is updated to `resolved`.
- On failed flow completion (non-zero exit code), the issue status is reverted to `open`.

#### Scenario: Run from a known issue ID
- **WHEN** user executes `se3 run --from-issue ISSUE-123`
- **THEN** the flow engine initializes a flow seeded from issue `ISSUE-123`
- **AND** the issue's status is set to `in-progress`
- **AND** the flow is linked to the issue via `source_issue_id`
- **AND** proceeds through the workflow steps for the issue's task type

#### Scenario: Interactive issue selection
- **WHEN** user executes `se3 run --from-issue ""` (with no issue ID supplied)
- **THEN** the system lists all open issues with their IDs, titles, and priorities
- **AND** prompts the user to enter an issue ID
- **AND** initializes a flow from the chosen issue

#### Scenario: Issue already in progress
- **GIVEN** issue `ISSUE-123` is in `in-progress` status
- **WHEN** user executes `se3 run --from-issue ISSUE-123`
- **THEN** the command aborts with an error
- **AND** instructs the user to reset the issue before retrying

#### Scenario: Issue status updated on flow success
- **GIVEN** a flow was started via `se3 run --from-issue ISSUE-123`
- **WHEN** the flow completes successfully (exit code 0)
- **THEN** issue `ISSUE-123` status is updated to `resolved`

#### Scenario: Issue status reverted on flow failure
- **GIVEN** a flow was started via `se3 run --from-issue ISSUE-123`
- **WHEN** the flow completes with a non-zero exit code
- **THEN** issue `ISSUE-123` status is reverted to `open`

### Requirement: Explicit Task Metadata Flags

The system SHALL allow callers of `se3 run` to override or supply task metadata explicitly via command-line flags, rather than relying solely on automatic classification.

**Flags:**
- `--type <task-type>` / `-t <task-type>` — Explicitly set the task type for this run (default: `feature`). Accepts any of the supported task types (`feature`, `bugfix`, `review`, `small`, `directive`, `discovery`, and other types recognized by the flow engine).
- `--change <name>` / `-c <name>` — Provide an explicit change name for this task. The change name is forwarded to the flow engine and used as the `change_name` associated with the flow.
- `--flow-id <id>` — Resume a specific flow by its ID. Supplying `--flow-id` triggers resume behavior even if `--resume` is not also supplied.

**Behavior:**
- `--type` overrides the default task type (`feature`) for the run. When `--discover` is also supplied, `--type` is overridden to `discovery` (discovery mode takes precedence).
- `--change` is passed through to the flow engine when starting a non-loop, non-issue-sourced flow; it has no effect on `--list-loops` or `--merge` standalone invocations.
- `--flow-id` selects a specific persisted flow to resume. If `--flow-id` is supplied, the system skips the interactive resume picker and resumes that flow directly.

#### Scenario: Explicit task type override
- **WHEN** user executes `se3 run "Fix login bug" --type=bugfix`
- **THEN** the flow is initialized with task type `bugfix`
- **AND** the workflow steps appropriate for `bugfix` are selected

#### Scenario: Discovery mode overrides explicit type
- **GIVEN** user executes `se3 run --discover --type=bugfix "Idea"`
- **WHEN** the run command processes flags
- **THEN** the task type is forced to `discovery`
- **AND** the discovery step sequence is selected

#### Scenario: Explicit change name
- **WHEN** user executes `se3 run "Add login form" --change=login-form`
- **THEN** the flow is created with `change_name` set to `login-form`

#### Scenario: Resume a specific flow by ID
- **WHEN** user executes `se3 run --flow-id <flow-id>`
- **THEN** the system resumes the specified flow directly
- **AND** does not present the interactive resume picker

### Requirement: Task Source Tracking

The system MAY track the source of tasks for analytics.

**Source markers (optional):**
- `direct` - Direct `se3 run "task"` command
- `discovery` - Discovery mode refined description
- `loop` - Loop mode task

#### Scenario: Source tracking
- **WHEN** a flow completes
- **THEN** the summary MAY include the task source

### Requirement: Flow Instance Origin Metadata

The system SHALL record origin and mode metadata directly on the flow instance so that downstream tooling can attribute and reconstruct how a flow was initiated.

**Persisted Fields:**
- `source_issue_id` — Optional issue identifier; set when the flow was started via `--from-issue` and used to link the flow back to the originating issue throughout its lifecycle.
- `is_loop_mode` — Boolean flag indicating whether this flow instance is executing as part of a `--loop` run.

**Behavior:**
- These fields are persisted as part of the flow instance state (e.g. in `se3/state/engine.json`) and survive interruption and resume.
- `source_issue_id` and `is_loop_mode` are independent of the optional task source markers in [[Task Source Tracking]]; they provide authoritative, machine-readable origin/mode metadata rather than analytics hints.

#### Scenario: Issue-sourced flow records source_issue_id
- **GIVEN** a flow is started via `se3 run --from-issue ISSUE-123`
- **THEN** the persisted flow instance has `source_issue_id` set to `ISSUE-123`
- **AND** the value remains set across interrupt and resume

#### Scenario: Loop flow records loop mode flag
- **GIVEN** a flow is started via `se3 run --loop "Task"`
- **THEN** the persisted flow instance has `is_loop_mode` set to `True`

### Requirement: User Interjections During Step Execution

The system SHALL allow the user to inject additional instructions during step execution and SHALL persist those interjections on the flow so they continue to influence subsequent step runs.

**Storage:**
- User interjections are appended to `flow.state.context["user_interjections"]` as a list of entries.
- Each entry records the interjection `text`, the `step_id` and `step_type` at which it was supplied, and an ISO-formatted `timestamp`.

**Application:**
- When the user provides an interjection while a step is interactively interrupted, the system mutates the current step's `inputs["task_description"]` in-place by composing the effective base task description with the full list of recorded interjections.
- The base description used for composition is the un-decorated base (the refined description if discovery ran, otherwise the original task description), preventing the "Additional Instructions" section from being duplicated when an interjection is added on top of a step that was already composed against earlier interjections.
- Persisting interjections in `flow.state.context` ensures they survive interrupt/resume and continue to be applied to later steps.

**Cancellation:**
- If the user cancels the interjection prompt (e.g. Ctrl+C with no text supplied), the flow state is saved and the run exits cleanly with instructions to resume via `se3 run --resume`.
- If the user submits an empty interjection (no additional text), the step is retried as-is without modifying the interjection list.

#### Scenario: Interjection recorded and applied immediately
- **GIVEN** a step has been interrupted and the user supplies an additional instruction
- **WHEN** the interjection is submitted
- **THEN** an entry containing the text, step ID, step type, and timestamp is appended to `flow.state.context["user_interjections"]`
- **AND** the current step's `task_description` input is recomposed from the base description plus the full interjection list
- **AND** the step is re-run with the updated instruction in effect

#### Scenario: Interjections persist across resume
- **GIVEN** a flow with one or more recorded interjections is interrupted and later resumed
- **WHEN** subsequent steps execute
- **THEN** the recorded interjections continue to influence the composed task description on subsequent steps

#### Scenario: Cancelled interjection saves and exits
- **GIVEN** the interjection prompt is shown after a step interruption
- **WHEN** the user cancels the prompt without submitting text
- **THEN** the current flow state is saved
- **AND** the run exits with guidance to resume via `se3 run --resume`

### Requirement: Step Failure Recovery

The system SHALL provide interactive recovery options when a workflow step fails, allowing the user to retry, skip, or abort the flow.

**Failure Handling:**
- When a step returns a FAILED status, the system displays the error message to the user.
- The step tracks a `retry_count` that increments on each retry attempt.
- A maximum of 3 retries is enforced; when the retry limit is reached, the flow is automatically marked as FAILED without further prompting.

**User Options (when retries remain):**
- **Retry this step** — Resets the step to PENDING, increments `retry_count`, sets `resumed=True` in the step inputs, and re-runs the step.
- **Skip to next step** — Marks the step as COMPLETED and transitions to the next step, allowing the flow to continue despite the failure.
- **Abort flow** — Sets the flow status to FAILED, persists state, and returns a non-zero exit code.

#### Scenario: Step fails with retries remaining
- **GIVEN** a step execution returns FAILED status and retry_count < 3
- **WHEN** the system handles the failure
- **THEN** the error message is displayed to the user
- **AND** the user is prompted to choose Retry, Skip, or Abort

#### Scenario: Retry on step failure
- **GIVEN** a failed step with retries remaining and the user chooses Retry
- **WHEN** the retry is processed
- **THEN** the step status is reset to PENDING
- **AND** retry_count is incremented
- **AND** the step is re-run with `resumed=True` in its inputs

#### Scenario: Skip on step failure
- **GIVEN** a failed step with retries remaining and the user chooses Skip
- **WHEN** the skip is processed
- **THEN** the step status is set to COMPLETED
- **AND** the flow transitions to the next step

#### Scenario: Abort on step failure
- **GIVEN** a failed step with retries remaining and the user chooses Abort
- **WHEN** the abort is processed
- **THEN** the flow status is set to FAILED
- **AND** state is persisted
- **AND** the run returns a non-zero exit code

#### Scenario: Max retries exceeded
- **GIVEN** a step execution returns FAILED status and retry_count >= 3
- **WHEN** the system handles the failure
- **THEN** the flow is automatically marked as FAILED
- **AND** the run returns a non-zero exit code
- **AND** no interactive prompt is shown
