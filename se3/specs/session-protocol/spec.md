<!-- spec-format: v1 -->
# session-protocol Specification

## Purpose

Define the session lifecycle protocol for SE3 3.0 agents. This spec governs progressive startup via `se3 run`, execution boundaries, shutdown procedures, and state persistence across sessions through the Flow Engine.

## Requirements

### Requirement: Core Principles

The system SHALL adhere to the following core principles that govern all SE3 operations:

**1. Human-as-MCP**: All human input is obtained on-demand via conversation. No pre-written requirement files.

**2. Progressive Loading**: Start with minimal context. Load deeper only when the task needs it.

**3. Specs as Snapshot of Code (spec-assistant)**: SE3 specs (`se3/specs/`) are the documented snapshot of project code, generated and maintained by the sync workflow. They protect against drift during implementation and help humans/LLMs understand current state. Future intent enters through issues, not specs. Specs have no routine manual-edit entry; therefore spec leading code is not a legitimate state. Agents MUST NOT weaken or delete existing requirements of a spec they are currently implementing against without explicit human approval — the spec is the implementation contract for the duration of that flow.

**4. Verify Before Done**: Never mark a feature complete without running tests. Spec scenarios are acceptance criteria, not documentation.

**5. Tool-Assisted Enforcement**: Use `se3 guardrails` to validate spec integrity.

**6. State Machine Driven**: Flow is controlled programmatically, not by LLM decisions.

**7. Interruptible & Resumable**: Any flow can be interrupted and resumed from the exact step.

#### Scenario: Agent follows core principles
- **WHEN** an agent is working on a flow
- **THEN** the agent follows all seven core principles throughout the session

#### Scenario: Spec violation detected
- **WHEN** an agent attempts to modify a spec they are implementing against
- **THEN** the system blocks the change and reports a guardrail violation

### Requirement: Session Startup Protocol

The system SHALL define a unified session startup protocol via `se3 run`.

**Startup Flow:**
1. User executes `se3 run [task_description]`
2. Flow Engine checks for existing active flow
3. If active flow exists:
   - Prompt user to resume or start new
   - If resume: load persisted state and continue
   - If new: create new flow instance
4. If no active flow:
   - Create new flow instance with task description
   - Start from analyze step

**Discovery Mode:**
- If `--discover` flag is used, start with discovery step
- Explore requirements through multi-turn conversation
- Proceed to analyze after user confirms refined description

#### Scenario: New flow startup
- **WHEN** agent runs `se3 run "Implement feature X"`
- **THEN** a new flow is created starting from analyze step

#### Scenario: Discovery mode startup
- **WHEN** agent runs `se3 run --discover "Idea"`
- **THEN** flow starts with discovery step
- **AND** explores requirements through conversation

#### Scenario: Resume interrupted flow
- **WHEN** agent runs `se3 run` with interrupted flow existing
- **THEN** agent is prompted to resume or start new
- **AND** resume continues from exact interruption point

### Requirement: Input Classification and Step Routing

The system SHALL classify user input in the analyze step to determine the appropriate workflow.

**Classification Mechanism:**
Classification is performed by an LLM call in the analyze step (see `ANALYZE_PROMPT` in `src/se3/engine/steps/analyze.py`). The prompt describes each task type and instructs the LLM to choose the most appropriate `task_type` for the given task description. There is no keyword-matching or rule-based classifier — categorization is entirely LLM-driven, so the system relies on the LLM's semantic judgment rather than a fixed list of trigger words.

**Task Types:**
| Task Type | Description | Steps Used |
|-----------|-------------|------------|
| `feature` | New functionality or significant enhancement | analyze → plan → implement → test → self_check → verify_spec → update_spec → version_analyze → commit |
| `bugfix` | Fixing a bug or issue | analyze → plan → implement → test → self_check → verify_spec → version_analyze → commit |
| `review` | Code review, audit, or analysis | analyze → verify_spec |
| `small` | Minor fix, typo, or simple change | analyze → implement → test → version_analyze → commit |
| `directive` | Following specific instructions | analyze → plan → implement → version_analyze → commit |
| `discovery` | Exploratory requirement gathering (via `--discover` flag) | discovery → analyze → plan → implement → test → self_check → verify_spec → update_spec → version_analyze → commit |

**Discovery Classification Gate:**
- The `discovery` task type is the ONLY type that the LLM classifier cannot select. The `ANALYZE_PROMPT` explicitly instructs the LLM: "Do NOT use 'discovery' — discovery mode is triggered separately via --discover flag."
- The `_extract_task_type` function (`src/se3/engine/steps/analyze.py`) enforces this programmatically by coercing any `discovery` result from the LLM back to `feature`, logging a warning that the `--discover` flag was not set.
- `discovery` is excluded from the `valid_types` list in `_extract_task_type`, which only permits `["feature", "bugfix", "review", "small", "directive"]`. Discovery mode is activated exclusively by the `--discover` CLI flag, which bypasses the normal analyze classification path and starts the flow from the discovery step.

**Item-Level Spec Selection:**

In addition to task type classification, the same LLM call selects which individual spec Requirements are relevant to the task. This item-level selection replaces coarse file-level spec loading, so downstream steps receive focused spec content instead of entire spec files.

**SpecIndex** (`src/se3/engine/spec_index.py`):
- An item-level index of all spec Requirements, keyed by `spec::requirement`. Each entry stores metadata (tags, keywords, summary, plus file mtime/size/SHA-256 prefix for incremental invalidation). The index is cached at `se3/cache/spec-index.json` and incrementally rebuilt when spec files change. Concurrent builds are serialized via an advisory file lock (`fcntl.flock`). The `list_for_selector` method produces a stable sorted list of all items (excluding base spec items, which are always loaded in full, and the `__no_requirements__` sentinel), each formatted as `spec::Requirement Name [tags: ...] — summary` grouped under spec-name headings.

**Selector Prompt:**
- The item list is injected into the `ANALYZE_PROMPT` via the `{available_items}` placeholder. The prompt instructs the LLM to select only items genuinely relevant to the task, noting that the base spec is always loaded automatically and does not need to be selected. The LLM returns a `selected_items` array: `[{"spec": "spec-name", "requirement_name": "Requirement Name"}]`.

**Validation:**
- The analyze handler validates `selected_items` against the actual list of spec names (`list_spec_names`), filtering out entries with hallucinated spec names and logging warnings. It then detects hallucinated requirement names by comparing the selected item IDs against what `load_for_step` actually loaded, logging warnings for any mismatches. If `selected_items` is not a list, it is reset to an empty list.

**Legacy Fallback:**
- If the LLM returns the old-format `selected_specs` array but no `selected_items`, the handler maps each spec name to all its Requirements via `_fallback_items_from_specs` and logs a warning that item-level loading was defeated. Unknown spec names are filtered out before fallback expansion.

**Spec Content Assembly** (`load_for_step` in `src/se3/engine/spec_loader.py`):
- The loader assembles spec text in "items" mode: base spec always included in full; each involved spec contributes its header text plus the bodies of selected Requirements (preserving file order via `line_start`) plus trailing orphan sections. Requirements referenced by selected items are expanded via 1-hop ref resolution (`resolve_refs`). The assembled text is stored in `step.outputs["spec_content"]` for injection into downstream step prompts.

**Persistence:**
- `selected_items` is stored in both `step.outputs["selected_items"]` and `flow.state.context["selected_items"]`, so downstream steps (including those that scan flow history for the selection) can access it even when the analyze step object is not directly reachable.

#### Scenario: Item-level spec selection in analyze LLM call
- **WHEN** the analyze step runs
- **THEN** a single LLM call both classifies `task_type` and selects relevant spec items
- **AND** the LLM prompt includes all available spec items from the item-level index (excluding base spec items)

#### Scenario: Hallucinated spec name filtered
- **WHEN** the LLM returns a `selected_items` entry with a spec name that does not exist in the project
- **THEN** the entry is filtered out and a warning is logged

#### Scenario: Hallucinated requirement name detected
- **WHEN** the LLM returns a `selected_items` entry with a valid spec name but a requirement name that does not exist in that spec
- **THEN** the mismatch is detected by comparing `selected_ids` against `load_result.loaded_items`
- **AND** a warning listing the missing (hallucinated) requirement names is logged

#### Scenario: Legacy selected_specs fallback
- **WHEN** the LLM returns `selected_specs` but no `selected_items`
- **THEN** each spec name is mapped to all its Requirements via `_fallback_items_from_specs`
- **AND** a warning is logged noting that item-level loading was defeated

#### Scenario: Base spec always loaded in full
- **WHEN** spec content is assembled for a step via `load_for_step`
- **THEN** the base spec is always loaded in full text regardless of whether any base items were selected
- **AND** base items are excluded from the `loaded_items` list

#### Scenario: selected_items persisted in flow context
- **WHEN** the analyze step completes
- **THEN** `flow.state.context["selected_items"]` is set to the validated `selected_items` array
- **AND** downstream steps can access it by reading flow state context

#### Scenario: Discovery coercion by analyze step
- **WHEN** the LLM analyze call returns `task_type: "discovery"` despite the prompt instruction
- **THEN** `_extract_task_type` coerces the result to `feature`
- **AND** logs a warning that the `--discover` flag was not set

#### Scenario: Bug fix classification
- **WHEN** the analyze step LLM judges the task description to describe fixing a bug or defect
- **THEN** system classifies task type as "bugfix"
- **AND** routes to bugfix workflow (skips update_spec step)

#### Scenario: Feature classification
- **WHEN** the analyze step LLM judges the task description to describe new functionality or a significant enhancement
- **THEN** system classifies task type as "feature"
- **AND** routes to feature workflow (analyze through commit)

#### Scenario: Review classification
- **WHEN** the analyze step LLM judges the task description to be a code review, audit, or analysis request
- **THEN** system classifies task type as "review"
- **AND** routes to review workflow (minimal steps)

### Requirement: Session Execution Boundary

Each flow MUST focus on a limited scope of work and MUST NOT attempt to complete too many tasks in a single flow.

**Scope Guidelines:**
- A flow should complete in a reasonable number of steps
- Complex work should be broken into multiple flows
- Loop mode (`se3 run --loop`) handles multiple related tasks

#### Scenario: Flow scope limitation
- **WHEN** the flow has determined work scope through analyze step
- **THEN** the flow only executes tasks within that scope

### Requirement: Session Shutdown Protocol

Session ending MUST leave code in a mergeable state.

**Shutdown Flow:**
1. Complete final step of the task-type sequence (typically commit)
2. Mark flow as COMPLETED
3. Clean up temporary state (cache files)

**Manual Shutdown (Ctrl+C):**
1. Ctrl+C during step execution: interrupts the current step and opens an "Additional Instruction" multiline prompt
2. From that prompt the user can:
   - Type an instruction and submit (Ctrl+D / Esc+Enter): the instruction is persisted to `flow.state.context["user_interjections"]`, inlined into the current step's `task_description`, and the step is reset to PENDING and re-runs
   - Submit empty input: the step retries as-is
   - Press Ctrl+C inside the prompt (returns None): flow state is saved and the process exits
3. Flow can be resumed later with `se3 run --resume`

There is no two-stage "first Ctrl+C interrupts / second Ctrl+C exits" state machine — exiting requires cancelling the instruction prompt that the single SIGINT opened.

#### Scenario: Normal flow completion
- **WHEN** flow completes its final sequence step
- **THEN** mark flow as COMPLETED

#### Scenario: Interrupt opens instruction prompt
- **WHEN** the user presses Ctrl+C during step execution
- **THEN** the current step is interrupted
- **AND** an "Additional Instruction" multiline prompt is shown

#### Scenario: Interrupt with additional instruction
- **WHEN** the user submits a non-empty instruction in the prompt
- **THEN** the instruction is appended to `user_interjections` and inlined into the current step's task_description
- **AND** the step is reset to PENDING and re-runs

#### Scenario: Interrupt and exit
- **WHEN** the user presses Ctrl+C inside the "Additional Instruction" prompt
- **THEN** flow state is saved
- **AND** the process exits
- **AND** the flow can later be resumed with `se3 run --resume`

### Requirement: Step Failure Interactive Recovery

When a step transitions to `StepStatus.FAILED`, the orchestrator SHALL obtain a Retry/Skip/Abort recovery decision, subject to a hard retry ceiling, rather than immediately failing the flow. On an interactive terminal the decision is collected via a prompt; off a terminal it is externalized as a `retry_decision` call file and resolved out-of-band.

**Recovery Flow** (`_run_flow` orchestrator loop in `src/se3/commands/run.py`):

1. When `result == StepStatus.FAILED`, the error message is displayed to the operator.
2. The step's `retry_count` field is checked against a hard ceiling of **3** (`max_retries = 3`).
3. If `retry_count >= 3`:
   - The operator is informed that max retries have been reached.
   - The flow is **auto-failed** (no prompt): `flow.status` is set to `FlowStatus.FAILED`, state is persisted, and the process exits with code 1.
4. If `retry_count < 3`, the recovery decision is sourced according to whether the process owns a terminal (`sys.stdin.isatty()`). The TTY and non-TTY decision channels MUST behave as a **mutually exclusive pair**: once either side answers, the other side must be left with no stale call file and no stale web-console chip. Concretely:
   - **On a TTY (interactive):** the orchestrator first probes for an out-of-band webui answer at the deterministic `retry_decision_{step_id}.json` call file (typical when a daemon-spawned flow paused on a prior failure and the operator later resumes from a TTY). If a sibling `.response` / `.response.json` file is present, its `decision` value is adopted and the call file plus both sibling response variants are removed — no CLI prompt is shown. If no answer is waiting, the orchestrator shows a choice prompt: **"Retry this step"** / **"Skip to next step"** / **"Abort flow"** WITHOUT writing a new call file (the interactive path stays free of any webui chip while the operator types). After the CLI prompt returns a choice, the orchestrator best-effort unlinks the same `retry_decision_{step_id}.json` call file and both sibling response variants — identically for all three choices — so that any webui chip raised in the typing window (e.g. a daemon under the same project root re-surfaced an older retry_decision artifact, or a concurrent webui answer raced the CLI) disappears as soon as the CLI side commits to a decision.
   - **Off a TTY (daemon-spawned `se3 run --output-format json`, CI, a pipe):** there is no operator to host a blocking prompt, so the decision is externalized as a `retry_decision`-kind call file under `se3/calls/` (written via `interaction_calls.write_retry_decision_call`, embedding the failed step's id/type, the error message, and the current retry count). If no sibling response file exists yet, the flow is set to `FlowStatus.PAUSED`, a `FLOW_PAUSED` event is emitted, state is persisted, and the process returns — the decision is made out-of-band (e.g. through the web console) and applied on the next `se3 run --resume`. If a sibling response file is already present (a resumed run), its `decision` value (`retry` / `skip` / `abort`, defaulting to `abort` when missing or unrecognized) is consumed and the call file plus both `.response` / `.response.json` siblings are removed so a later failure of the same step writes a fresh call.

The shared filename and cleanup behavior is owned by two helpers in `src/se3/commands/run.py`: `_retry_decision_call_path(project_root, step_id)` returns the deterministic `retry_decision_{step_id}.json` path that both ends of the channel address, and `_cleanup_retry_decision_artifacts(call_path)` is the best-effort unlink of that call file plus its `.response` / `.response.json` siblings. Both the interactive sibling-response probe, the non-interactive resumed-decision consumer, and the post-CLI-prompt mutual-exclusion cleanup route through these helpers so the two channels never disagree on which file to clear.

**Choice Outcomes:**

- **Retry (choice 0):**
  - The step's status is reset to `StepStatus.PENDING`.
  - `step.inputs["resumed"]` is set to `True` so the LLM caller can pick up conversation history from the prior failed attempt via `_get_retry_context()`.
  - `step.inputs["retry_count"]` is incremented by 1 (tracking retries at the inputs level for the LLM caller's external-attempt detection).
  - `step.retry_count` (the step model field) is incremented by 1 (tracking retries for the ceiling check on the next failure).
  - Flow state is persisted and the orchestrator loop continues, re-executing the step.

- **Skip (choice 1):**
  - The step is force-completed: its status is set to `StepStatus.COMPLETED`.
  - `state_machine.transition_to_next(flow)` advances to the next step in the workflow sequence.
  - Flow state is persisted and the orchestrator loop continues with the next step.

- **Abort (choice 2 / default):**
  - `flow.status` is set to `FlowStatus.FAILED`.
  - Flow state is persisted and the process exits with code 1.

The `retry_count` on the step model is distinct from the `inputs["retry_count"]` counter: the model field tracks retries for the ceiling check; the inputs field tracks retries for the LLM caller's conversation-history wrapping. Both are incremented on each retry. On a resume-from-persistence (rather than an interactive retry), the model field is reset to 0 so the resumed run gets a fresh retry budget.

#### Scenario: Step failure with retry budget remaining
- **GIVEN** a step transitions to `FAILED` and `current_step.retry_count < 3`
- **WHEN** the orchestrator processes the failed result
- **THEN** the error message is displayed
- **AND** the operator is prompted with "Retry this step" / "Skip to next step" / "Abort flow"

#### Scenario: Retry resets step and increments counters
- **GIVEN** the operator selects "Retry this step" at the failure prompt
- **WHEN** the orchestrator processes the choice
- **THEN** the step status is reset to `PENDING`
- **AND** `step.inputs["resumed"]` is set to `True`
- **AND** `step.inputs["retry_count"]` is incremented by 1
- **AND** `step.retry_count` (model field) is incremented by 1
- **AND** flow state is persisted before re-executing the step

#### Scenario: Skip forces step completion and transitions
- **GIVEN** the operator selects "Skip to next step" at the failure prompt
- **WHEN** the orchestrator processes the choice
- **THEN** the failed step's status is set to `COMPLETED`
- **AND** `state_machine.transition_to_next(flow)` advances to the next step
- **AND** flow state is persisted and the loop continues

#### Scenario: Abort fails the flow immediately
- **GIVEN** the operator selects "Abort flow" at the failure prompt
- **WHEN** the orchestrator processes the choice
- **THEN** `flow.status` is set to `FAILED`
- **AND** flow state is persisted
- **AND** the process exits with code 1

#### Scenario: Max retries reached auto-fails without prompt
- **GIVEN** a step transitions to `FAILED` and `current_step.retry_count >= 3`
- **WHEN** the orchestrator processes the failed result
- **THEN** the operator is informed that max retries (3) have been reached
- **AND** the flow is auto-failed without displaying the Retry/Skip/Abort prompt
- **AND** `flow.status` is set to `FAILED`
- **AND** the process exits with code 1

#### Scenario: Non-interactive failure externalizes the decision and pauses
- **GIVEN** a step transitions to `FAILED` with `current_step.retry_count < 3` and the process does not own a terminal (`sys.stdin.isatty()` is false)
- **WHEN** the orchestrator processes the failed result and no response file exists yet
- **THEN** a `retry_decision`-kind call file is written under `se3/calls/` carrying the failed step's id/type, the error message, and the retry count
- **AND** `flow.status` is set to `PAUSED`, a `FLOW_PAUSED` event is emitted, and flow state is persisted
- **AND** no interactive Retry/Skip/Abort prompt is shown

#### Scenario: Non-interactive failure consumes an out-of-band decision on resume
- **GIVEN** a `retry_decision` call file exists with a sibling response file recording a `decision` of `retry`, `skip`, or `abort`
- **WHEN** the orchestrator processes the failed step off a terminal
- **THEN** the recorded decision is applied with the same Retry/Skip/Abort outcomes as the interactive prompt
- **AND** the call file and both `.response` / `.response.json` siblings are removed so a later failure of the same step writes a fresh call
- **AND** a missing or unrecognized `decision` value defaults to `abort`

#### Scenario: Interactive failure adopts a pre-existing webui answer
- **GIVEN** a daemon-spawned earlier run wrote a `retry_decision_{step_id}.json` call for the same failed step, the webui operator answered it (writing a sibling `.response` / `.response.json`), and the flow is now being processed on a TTY
- **WHEN** the orchestrator reaches the Retry/Skip/Abort decision point with `retry_count < 3`
- **THEN** the sibling response is consumed and its decision is applied with the same Retry/Skip/Abort outcomes as the interactive prompt
- **AND** the call file and both `.response` / `.response.json` siblings are removed so a later failure of the same step writes a fresh call
- **AND** no Retry/Skip/Abort prompt is shown to the operator

#### Scenario: Interactive prompt does not create a new retry_decision call
- **GIVEN** a step fails on a TTY with `retry_count < 3` and no `retry_decision_{step_id}.json` call file exists
- **WHEN** the orchestrator reaches the decision point
- **THEN** the operator is shown the Retry/Skip/Abort prompt directly
- **AND** no new `retry_decision`-kind call file is written under `se3/calls/` for that failure (the interactive path stays free of any webui chip while the operator types)

#### Scenario: CLI decision cleans up any concurrent webui artifacts
- **GIVEN** a step fails on a TTY, the operator is shown the Retry/Skip/Abort prompt, and during the typing window a webui-side `retry_decision_{step_id}.json` call file (and possibly a sibling response) exists for the same step
- **WHEN** the operator submits any of the three choices (Retry / Skip / Abort)
- **THEN** the call file and both `.response` / `.response.json` siblings for that step are best-effort unlinked after the prompt returns
- **AND** the cleanup happens identically for all three choices (it is keyed on the decision having been taken on the CLI side, not on which decision was picked)
- **AND** any webui chip for that step disappears from the docked reply bar so the operator is not asked to answer the same decision again on the other channel

### Requirement: CONFIRM Steps and REVISION_NEEDED Transitions

The engine SHALL support CONFIRM steps that gate progress on human (or LLM) review of a preceding step's output, and SHALL model "approved" and "needs changes" outcomes through distinct step statuses so the state machine can route to a revision loop instead of forward progress.

**Status Model:**
- Beyond the basic `PENDING`/`COMPLETED`/`FAILED`/`PAUSED` statuses described elsewhere in this spec, the engine recognizes `StepStatus.REVISION_NEEDED`, returned by `_check_confirm_response` (`src/se3/commands/run.py`) when a human reviewer disapproves a CONFIRM step's reviewed output.
- An `approved=True` response transitions the CONFIRM step to `StepStatus.COMPLETED`; an `approved=False` response transitions it to `StepStatus.REVISION_NEEDED` so the state machine can route back to the reviewed step with `revision_feedback` attached.

**Call/Response File Protocol:**
- CONFIRM steps emit a call file under `se3/calls/confirm_*.json` whose path is recorded in `current_step.outputs["call_file"]`. The call file records the `step` id of the CONFIRM step and the inputs `step_to_review_id` / `step_to_review_type`.
- A reviewer (human or external tool) writes a sibling response file at `<call_file_stem>.response` containing `{"approved": bool, "feedback": str|null, "step_to_review_id": ..., "step_to_review_type": ...}`.
- On resume / re-run, `_check_confirm_response` scans `se3/calls/confirm_*.json`, matches on `step_id` only (NOT change_id, because change_id is flow-level and would cause distinct confirm steps to collide on the same response), reads the response, populates `current_step.outputs["review_result"]` and `current_step.outputs["revision_feedback"]`, and returns the appropriate `StepStatus`.
- Malformed JSON or unreadable response files are logged and skipped rather than failing the flow.

**Interactive Confirmation Pause:**
- When a CONFIRM step is PAUSED and no response file exists yet, `_handle_confirm_pause` prompts the operator with `Approve and continue` / `Request changes` / `Exit (pause flow)`. Choosing "Request changes" opens a multiline "Feedback" input. Choosing "Exit" or cancelling either prompt persists the flow and exits without writing a response file (the step remains PAUSED for resume).
- A written response file is the durable artifact: subsequent resumes will pick it up via `_check_confirm_response` without re-prompting.

#### Scenario: Human approval completes CONFIRM step
- **GIVEN** a CONFIRM step has emitted a call file and an external reviewer has written a response with `approved=true`
- **WHEN** the engine resumes and `_check_confirm_response` runs
- **THEN** `review_result.approved` is `True`, `revision_feedback` is set from the response, and the step transitions to `StepStatus.COMPLETED`

#### Scenario: Human rejection triggers REVISION_NEEDED
- **GIVEN** a response file is present with `approved=false` and a `feedback` string
- **WHEN** `_check_confirm_response` runs on resume
- **THEN** `revision_feedback` is populated with the feedback string
- **AND** the step transitions to `StepStatus.REVISION_NEEDED` so the state machine can route back to the reviewed step

#### Scenario: Confirm matching ignores change_id
- **GIVEN** multiple CONFIRM steps exist across flows that share a `change_id`
- **WHEN** `_check_confirm_response` scans `se3/calls/confirm_*.json`
- **THEN** matching is performed on `step_id` only so distinct CONFIRM steps do not collide on the same response file

#### Scenario: Interactive request-changes writes response file
- **GIVEN** a CONFIRM step is PAUSED with no response file yet
- **WHEN** the operator selects "Request changes" and submits feedback
- **THEN** `<call_file_stem>.response` is written with `approved=false` and the feedback text, and the next handler iteration picks it up to produce `REVISION_NEEDED`

#### Scenario: Operator exits CONFIRM pause without writing a response
- **WHEN** the operator selects "Exit (pause flow)" or cancels the choice/feedback prompt with Ctrl+C
- **THEN** flow state is persisted and the process exits
- **AND** the CONFIRM step remains PAUSED (no response file written), so it can be resumed later

### Requirement: Discovery Programmatic Confirmation Gate

The discovery step SHALL gate the transition from "LLM-confirmed refined description" to "downstream analyze" behind a strict programmatic human confirmation.

**Gate Behavior** (`_handle_discovery_programmatic_confirm` in `src/se3/commands/run.py`):
- When a discovery step pauses with `outputs["awaiting_programmatic_confirm"]` truthy, the discovery pause handler routes to the programmatic-confirm handler instead of the normal discovery response prompt.
- The handler displays a "Discovery Confirmation" multiline prompt. Only the exact single character `1` (after stripping trailing `\n`/`\r` artifacts of the multiline UI, but with `strip=False` otherwise) confirms — `" 1 "`, `"1."`, `"yes"`, etc. all fall through to the "continue discovery" branch. The strict `== "1"` check is intentional.
- On confirmation, `current_step.inputs["programmatic_confirmed"]` is set to `True` and the `_PROGRAMMATIC_CONFIRM` sentinel (= `PROGRAMMATIC_CONFIRM_SENTINEL`) is returned so the discovery handler's early-return guard can short-circuit before re-feeding the sentinel to the LLM. The sentinel is persisted via `persistence.save_flow` in the orchestrator loop before the next handler invocation.
- Empty or whitespace-only input is a no-op: the cached confirmation panel is re-rendered via `_display_discovery_message(..., is_confirmation=True, raw_result_text=...)` and the loop continues. This applies to both interactive empty input (Ctrl+D on empty buffer returns `""`) and non-interactive whitespace; non-interactive `None` from a closed pipe pauses the flow.
- Any other non-empty input clears `awaiting_programmatic_confirm` and is returned as the next discovery user turn, continuing the conversation rather than confirming.
- Ctrl+C / EOF from the prompt persists the flow and returns `None` so the orchestrator pauses for later resume.

**Web-Mirrored Dual-Wait:**
- When a project root is known, the gate is mirrored to a `se3/calls/` call file of kind `CALL_KIND_DISCOVERY_CONFIRM` (one-click `"1"` option) via `_maybe_write_discovery_call`, so the web console surfaces the *same* pending confirmation as a `discovery_confirm` chip — even for a discovery flow started interactively from the CLI, which previously only blocked the terminal read and gave the web console nothing to answer.
- The terminal prompt and the web response file are then awaited **in parallel** by `_await_terminal_or_web`: whichever answers first drives the flow in this same live process (no `--resume` round-trip). A web confirm click submits the same literal `"1"` through the call/response channel that the strict `== "1"` check consumes, so the web and terminal paths share one gate semantics.
- Throughout the dual-wait the flow stays `RUNNING` — it is **never** marked `PAUSED` — so a watching daemon does not race this live process with a duplicate `--resume` spawn. (The interactive Ctrl+C/EOF cancel above is the one exception that persists and exits for later resume.)
- Each resolved round (terminal answer, web answer, or cancel) cleans up the call file plus any sibling `.response` answer files via `_cleanup_discovery_call`, and a consumed web answer's response file is removed so the gate's empty-input re-display loop cannot re-read an already-consumed answer.

The same call-file mirroring + parallel terminal/web dual-wait applies to the ordinary discovery clarification-question pause (`_handle_discovery_pause`): the question is written as a `CALL_KIND_CALL` file and answered from the terminal or the web in the same live RUNNING process.

**Resume Display:**
- `_restore_discovery_display` re-renders the LAST assistant message from `discovery_state.history` on resume. When `outputs["awaiting_programmatic_confirm"]` is set, the rendering uses `is_confirmation=True` so the confirmation panel (not the question panel) is shown, and `refined_description` is preferred over `proposed_description` for the body.

#### Scenario: Strict "1" confirms and emits sentinel
- **GIVEN** a discovery step is PAUSED with `awaiting_programmatic_confirm=True`
- **WHEN** the user enters exactly `1` at the Discovery Confirmation prompt
- **THEN** `inputs["programmatic_confirmed"]` is set to `True`
- **AND** the handler returns the `_PROGRAMMATIC_CONFIRM` sentinel so the discovery handler short-circuits before re-feeding it to the LLM

#### Scenario: Variants of "1" are rejected
- **GIVEN** the same paused step
- **WHEN** the user enters `" 1 "`, `"1."`, `"yes"`, or any other non-empty value that is not exactly `1`
- **THEN** the input is treated as the next discovery user turn
- **AND** `awaiting_programmatic_confirm` is cleared from outputs before returning

#### Scenario: Empty input re-displays the confirmation panel
- **WHEN** the user submits empty or whitespace-only input at the Discovery Confirmation prompt
- **THEN** the cached confirmation panel is re-rendered via `_display_discovery_message(..., is_confirmation=True)`
- **AND** the prompt loops without advancing the flow

#### Scenario: Ctrl+C pauses the flow at the confirmation gate
- **WHEN** the user presses Ctrl+C (or the pipe closes in non-interactive mode) at the Discovery Confirmation prompt
- **THEN** flow state is saved
- **AND** the handler returns `None` so the orchestrator exits with a "Resume with: se3 run --resume" notice

#### Scenario: Resume rendering for paused programmatic confirm
- **GIVEN** a discovery step is PAUSED with `awaiting_programmatic_confirm=True` and a populated `discovery_state.history`
- **WHEN** the flow is resumed
- **THEN** `_restore_discovery_display` re-renders the last assistant message with `is_confirmation=True`
- **AND** the body prefers `refined_description` over `proposed_description`

#### Scenario: Interactive gate is mirrored to a web-answerable call file
- **GIVEN** a discovery flow started interactively from the CLI reaches the programmatic confirmation gate with a known project root
- **WHEN** the gate handler begins waiting for the user
- **THEN** a `CALL_KIND_DISCOVERY_CONFIRM` call file is written under `se3/calls/` so the web console shows the same pending confirmation
- **AND** the terminal prompt and the web response file are awaited in parallel
- **AND** the flow stays `RUNNING` (it is not marked `PAUSED`) during the wait

#### Scenario: Web confirm drives the live process
- **GIVEN** the interactive gate is mirrored to a `discovery_confirm` call file and is awaiting both the terminal and the web
- **WHEN** the user clicks confirm in the web console (submitting the literal `"1"` through the call/response channel) before any terminal input
- **THEN** the same strict `== "1"` gate consumes it, `inputs["programmatic_confirmed"]` is set to `True`, and the flow proceeds in the same live process without a `--resume` round-trip
- **AND** the call file and its consumed `.response` siblings are cleaned up so the answer cannot be re-read

### Requirement: User Interjection Persistence Across Downstream Steps

User-typed instructions captured during a Ctrl+C interrupt SHALL persist across the entire flow, not just the interrupted step, so subsequent steps see the same instructions in their task descriptions.

**Persistence Model** (`_handle_step_interrupt` in `src/se3/commands/run.py`):
- A non-empty interjection is appended to `flow.state.context["user_interjections"]` as `{"text": <user_input>, "step_id": <id>, "step_type": <type>, "timestamp": <ISO>}`.
- The current step's `inputs["task_description"]` is recomposed via `compose_task_description_with_interjections(base=_effective_task_description_base(flow), interjections=flow.state.context["user_interjections"])`. The base is the un-decorated source (the discovery `refined_description` if discovery ran, otherwise `flow.task_description`) — NOT the step's already-composed task_description, to avoid emitting a duplicate `## Additional Instructions` section.
- The step is reset to `StepStatus.PENDING` and state is persisted before the re-run.

**Web-Console Interjections** (`_drain_pending_interjections` in `src/se3/commands/run.py`):
- A mid-flow instruction typed into the web console is delivered to the running flow out-of-band: the server sends `MSG_INTERJECT_FLOW`, the daemon writes an `interjection`-kind call file under the flow's `se3/calls/`, and the `se3 run` process drains it via `interaction_calls.drain_interjection_requests`.
- The drain MUST happen **continuously**, not only at step boundaries. The run loop calls `_drain_pending_interjections` (a) at each step boundary in the normal run loop, AND (b) at every poll tick of the PAUSED waiting loops — currently `_handle_confirm_pause` / `_handle_discovery_pause` / `_handle_discovery_pause_noninteractive` / the programmatic-confirm wait — so an interjection typed while the flow is paused waiting for a confirm or a discovery clarification reply is consumed within roughly one poll tick instead of being held until the user resumes the flow at the CLI. To shorten that tick, the daemon side MUST also fire an immediate out-of-band status push (`_fast_push_event` in `DaemonClient`) when it writes an `interjection`-kind call file, so the run loop's polling-driven drain wakes within ~1 second of the write.
- Each drained instruction is appended to `flow.state.context["user_interjections"]` using the same entry shape as a Ctrl+C interjection (`text`, `step_id`, `step_type`, ISO `timestamp`), additionally tagged with `source: "web-console"`, and the current step's `task_description` is recomposed via `compose_task_description_with_interjections` so the instruction takes effect — identical to the Ctrl+C path.
- In addition to the `flow.state.context` append, each drained interjection MUST be **persisted to the current step's chat-history jsonl** as a record shaped `{role: "user", kind: "interjection", text, step_id, step_type, timestamp, attempt}` via `chat_history.record_user_interjection`. The record is written for every drained item so `se3 history show <flow_id>` and the web-console history-replay surface both show every interjection the user typed, in chronological order with the surrounding step's messages. To avoid double-feeding the LLM, the retry-context reconstruction path (`chat_history.format_history_for_retry`) MUST skip these `kind: "interjection"` records — they enter the prompt only via the injection rules below, not by being replayed as plain `user` messages.
- The drained interjection MUST also be injected into the next LLM call the flow makes, on both the normal-run and PAUSED-reply paths:
  - On the **PAUSED-discovery reply path** (`_handle_discovery_pause` / `_handle_discovery_pause_noninteractive`), the reply handler MUST consume the drained interjection text via `_consume_paused_interjection_prefix` and prepend it to the user's reply as a `[interjection: <text>]\n<user reply>` prefix before that combined string is sent to the LLM. This guarantees that an interjection typed while the flow is paused waiting on a discovery clarification reaches the same LLM turn the user is about to answer, instead of being deferred to a later step.
  - On the **non-PAUSED (normal run) path**, the drained interjection takes effect through the existing `task_description` recomposition: the next step / next retry the LLM sees carries the full `## Additional Instructions (added during run)` section composed from the `user_interjections` list, so no extra injection is needed.

**Downstream Propagation:**
- Subsequently constructed steps pick up the same interjection list at construction time via the engine's step-input builder (`state_machine._build_step_inputs`), which composes interjections onto every new step's `task_description`. The interrupt handler does NOT mutate already-constructed downstream step inputs; propagation happens because downstream steps are built later and read the live `flow.state.context["user_interjections"]`.

#### Scenario: Interjection is persisted with metadata
- **WHEN** the user submits a non-empty instruction at the "Additional Instruction" prompt
- **THEN** an entry containing `text`, `step_id`, `step_type`, and ISO `timestamp` is appended to `flow.state.context["user_interjections"]`

#### Scenario: Current step recomposed from the un-decorated base
- **WHEN** an interjection is recorded
- **THEN** `current_step.inputs["task_description"]` is recomposed by passing the un-decorated base (refined_description if discovery ran, else flow.task_description) and the full interjection list to `compose_task_description_with_interjections`
- **AND** the recomposition does NOT use the step's already-composed task_description, preventing a duplicated `## Additional Instructions` section

#### Scenario: Downstream steps inherit interjections on construction
- **GIVEN** one or more interjections have been recorded into `flow.state.context["user_interjections"]`
- **WHEN** the state machine later constructs a downstream step via `_build_step_inputs`
- **THEN** the new step's `task_description` includes the accumulated interjections

#### Scenario: Empty interjection retries step as-is
- **WHEN** the user submits empty input at the "Additional Instruction" prompt
- **THEN** no entry is appended to `user_interjections`
- **AND** the current step is reset to `PENDING` and re-runs unchanged

#### Scenario: Web-console interjection drained at step boundaries and during PAUSED waits
- **GIVEN** the daemon has written one or more `interjection`-kind call files for the running flow (from a `MSG_INTERJECT_FLOW`)
- **WHEN** the run loop reaches a step boundary OR is polling inside a PAUSED waiting loop (e.g. `_handle_confirm_pause`, `_handle_discovery_pause`, `_handle_discovery_pause_noninteractive`, the programmatic-confirm wait)
- **THEN** `_drain_pending_interjections` runs on that tick and each instruction is appended to `flow.state.context["user_interjections"]` with `text`, `step_id`, `step_type`, ISO `timestamp`, and `source: "web-console"`
- **AND** the current step's `task_description` is recomposed via `compose_task_description_with_interjections` so the instruction takes effect
- **AND** the daemon's out-of-band fast-push for `interjection`-kind writes causes that drain to happen within ~1s of the call file appearing, even while the flow is PAUSED

#### Scenario: Drained interjection is persisted to the step's history jsonl
- **GIVEN** a web-console interjection has just been drained by `_drain_pending_interjections`
- **WHEN** the drain handler processes that item
- **THEN** `chat_history.record_user_interjection` writes a `{role: "user", kind: "interjection", text, step_id, step_type, timestamp, attempt}` record to the current step's per-step jsonl
- **AND** `se3 history show <flow_id>` and the web-console history view both surface that record alongside the surrounding step's messages
- **AND** `chat_history.format_history_for_retry` SKIPs records whose `kind` is `interjection`, so the LLM never sees them re-played as plain user messages on a retry

#### Scenario: PAUSED-discovery reply prefixes drained interjections to the user reply
- **GIVEN** the flow is paused inside `_handle_discovery_pause` (or its non-interactive variant) waiting for the user's reply to a discovery clarification, AND one or more interjections have been drained during the wait
- **WHEN** the user (CLI or web console) submits the reply
- **THEN** the reply handler consumes the drained interjection text via `_consume_paused_interjection_prefix` and prepends it as `[interjection: <text>]\n` (one line per drained item) ahead of the user's reply text
- **AND** the combined string is the single user message sent to the LLM on the next call, so the interjection reaches the very same turn the user is answering rather than being deferred

### Requirement: Session Commit Cadence

Commits occur during the commit step when distinct units of work are complete.

**When to Commit:**
- Commit step executes automatically in the workflow
- After completing a coherent unit of work that passes tests
- Version bumping happens during commit step if enabled

**Commit Rules:**
- Commit step stages and commits all changes
- Commit messages include context
- Version is bumped according to task type and bump rules

#### Scenario: Commit step execution
- **WHEN** flow reaches commit step
- **THEN** changes are committed
- **AND** version is bumped if enabled

### Requirement: State Persistence

The system SHALL persist flow state after each step.

**Persistence Details:**
- File location: `se3/state/engine.json`
- Atomic writes (temp file + rename)
- Includes: flow metadata, current step, step history, all outputs

**Recovery:**
- `se3 run --resume` loads persisted state
- Prompts user if multiple active flows
- Continues from exact interruption point

#### Scenario: State persistence
- **WHEN** each step completes
- **THEN** state is automatically saved

#### Scenario: State recovery
- **WHEN** `se3 run --resume` is executed
- **THEN** flow continues from last saved state

### Requirement: Resumption of Failed Flows

The interactive resume flow SHALL treat FAILED flows as resumable in addition to interrupted ones, and SHALL retry the failed step from its breakpoint.

**Resumable Set:**
- The resume picker (`_select_flow_to_resume` in `src/se3/commands/run.py`) filters out only flows with `FlowStatus.COMPLETED`. Any other status — including `FAILED` and interrupted states — is included in the list of resumable flows.
- When exactly one resumable flow is found and it is FAILED, the picker labels it as "failed" and offers a "Retry failed flow" action instead of the usual "Resume this flow" action.
- When multiple resumable flows are listed, FAILED flows are tagged with a `[FAILED]` marker in the choice list so the user can distinguish them from interrupted flows.

**Retry-from-Breakpoint Logic:**
- On resume, if the current step has `StepStatus.FAILED`, the step is reset to `StepStatus.PENDING` and the flow status is reset to `FlowStatus.RUNNING`.
- The step's `inputs["resumed"]` is set to `True` and `inputs["retry_count"]` is incremented so that the LLM caller can pick up conversation history from the prior failed attempt via `_get_retry_context()` (treating the prior failure as an `external_attempt`).
- The step model's `retry_count` field is reset to `0` so the retry receives a fresh retry budget rather than inheriting the budget consumed by the failed run.
- State is persisted immediately after this reset so a subsequent crash still sees the prepared retry state.

This mirrors the analogous logic for resuming an interrupted step (same `inputs["resumed"]` / `inputs["retry_count"]` bookkeeping and `retry_count` reset), but additionally transitions `flow.status` back to `RUNNING` because a failed flow is not already in the running state.

**Retry-from-RUNNING Logic:**
- On resume, if the current step has `StepStatus.RUNNING` (i.e. it was interrupted mid-execution, e.g. by a crash or SIGKILL), the step is reset to `StepStatus.PENDING` with the same retry bookkeeping as the FAILED case: `inputs["resumed"]` is set to `True`, `inputs["retry_count"]` is incremented so the LLM caller can pick up conversation history from the interrupted run via `_get_retry_context()`, and the step model's `retry_count` is reset to `0`.
- Unlike the FAILED case, `flow.status` is NOT modified — a RUNNING flow is already in the running state, so no status transition is needed.
- State is persisted immediately after this reset.

#### Scenario: Single FAILED flow is offered for retry
- **GIVEN** exactly one resumable flow exists and its status is `FAILED`
- **WHEN** the user invokes the resume picker
- **THEN** the flow is labeled as "failed" and the action prompt offers "Retry failed flow"
- **AND** selecting that action returns the flow id to the resume path

#### Scenario: FAILED flows are tagged in multi-flow picker
- **WHEN** the resume picker lists multiple resumable flows
- **THEN** each flow whose status is `FAILED` is shown with a `[FAILED]` marker alongside its description and current step

#### Scenario: Retry a failed step from its breakpoint
- **GIVEN** a resumed flow whose `current_step.status` is `FAILED`
- **WHEN** the resume path prepares the flow to run
- **THEN** the step's status is reset to `PENDING`
- **AND** `step.inputs["resumed"]` is set to `True`
- **AND** `step.inputs["retry_count"]` is incremented by 1
- **AND** the step model's `retry_count` is reset to `0`
- **AND** `flow.status` is set to `RUNNING`
- **AND** the prepared state is persisted before the step re-runs

#### Scenario: Retry a RUNNING (interrupted) step from its breakpoint
- **GIVEN** a resumed flow whose `current_step.status` is `RUNNING`
- **WHEN** the resume path prepares the flow to run
- **THEN** the step's status is reset to `PENDING`
- **AND** `step.inputs["resumed"]` is set to `True`
- **AND** `step.inputs["retry_count"]` is incremented by 1
- **AND** the step model's `retry_count` is reset to `0`
- **AND** `flow.status` is NOT modified (it was already `RUNNING`)
- **AND** the prepared state is persisted before the step re-runs

### Requirement: Loop Mode

The system SHALL support continuous task execution via `se3 run --loop`.

**Loop Mode Behavior:**
1. Execute current task flow to completion
2. Continue with next iteration or exit when done

**Loop Options:**
- `--max-iterations N`: Limit iterations
- `--type TYPE`: Filter task types
- `--no-worktree`: Disable branch isolation (run on current branch)
- `--merge BRANCH`: Merge an existing loop branch

#### Scenario: Loop execution
- **WHEN** `se3 run --loop` is executed
- **THEN** tasks are executed continuously until iterations are exhausted or no more work remains

### Requirement: Loop Mode Iteration Summaries

Loop mode SHALL generate and propagate per-iteration summaries between iterations to preserve context across the loop.

**Summary Generation:**
- After each loop iteration completes, an LLM call (`_generate_iteration_summary` in `src/se3/commands/run.py`) produces a concise summary of what was accomplished in that iteration.
- The summary is appended to the loop controller's `accumulated_summaries` list (`add_summary` in `src/se3/engine/loop_controller.py`).
- Accumulated summaries are truncated to a bounded size (`_truncate_summaries`) so the propagated context does not grow without limit.

**Summary Truncation Algorithm** (`_truncate_summaries` in `src/se3/engine/loop_controller.py`):
- The total character length of all accumulated summaries (sum of `len(s)` across all entries) is capped at **8000 characters**.
- When the cap is exceeded, the oldest non-placeholder entries are removed one at a time (by popping from the front of the list) until the total falls back under the cap.
- If the first entry is already the `[...earlier iterations omitted...]` placeholder (from a prior truncation round), the placeholder is left in place and the next real entry (index 1) is removed instead.
- After evicting entries, the `[...earlier iterations omitted...]` placeholder is inserted at the front of the list so the loop context framing renders it as a truncation marker. The placeholder's own length contributes to the total, so future truncation rounds account for it.
- Truncation triggers on every call to `add_summary` — the new summary is appended first, then `_truncate_summaries` runs to enforce the cap.

**Summary Propagation:**
- Before starting the next iteration, the accumulated summaries are injected into the task context for that iteration as an additional prompt fragment.
- This allows subsequent iterations to be aware of what prior iterations have already done, even though each iteration is otherwise an independent flow.

**Persistent Extra Prompt Delivery Mechanism:**
- Loop context is injected into every LLM call within an iteration via the `set_extra_prompt` / `get_extra_prompt` / `clear_persistent_extra_prompt` API in `src/se3/engine/llm_caller.py`.
- `set_extra_prompt(prompt, persistent=True)` sets a persistent extra prompt that survives across multiple LLM calls within the same iteration, as opposed to the transient mode (`persistent=False`, the default) used for one-shot Ctrl+C interrupt injection, which is consumed after a single LLM call.
- `get_extra_prompt()` returns the combined transient + persistent prompt text (joined with `\n\n` if both are present) without consuming either, so every LLM call in the iteration sees the loop context.
- After each iteration, the loop controller calls `clear_persistent_extra_prompt()` in a `finally` block to clean up the persistent prompt before the next iteration sets a fresh one. This cleanup does NOT affect the transient extra prompt.
- `clear_extra_prompt()` clears both transient and persistent prompts simultaneously.

**Loop Context Framing** (`_build_loop_context` in `src/se3/engine/loop_controller.py`):
- The loop context string begins with a `[Loop Mode Context]` header line: `You are running in loop mode, iteration N` (or `iteration N of M` when `max_iterations` is set).
- The second line includes the current task: `Current task: {task}`.
- When `accumulated_summaries` is non-empty, a `[Previous Iteration Summaries]` section is appended, with each summary prefixed by its iteration number or the `[...earlier iterations omitted...]` truncation marker.

#### Scenario: Persistent extra prompt injected for every LLM call in an iteration
- **WHEN** the loop controller starts a new iteration
- **THEN** the loop context is set via `set_extra_prompt(loop_context, persistent=True)`
- **AND** every LLM call within that iteration receives the combined persistent + transient extra prompt via `get_extra_prompt()`
- **AND** the persistent prompt is cleaned up via `clear_persistent_extra_prompt()` in a finally block after the iteration completes

#### Scenario: Transient and persistent extra prompts are combined
- **WHEN** both a persistent extra prompt (loop context) and a transient extra prompt (Ctrl+C interrupt injection) are set simultaneously
- **THEN** `get_extra_prompt()` returns them joined with `\n\n`, with the persistent prompt first
- **AND** neither prompt is consumed by the call to `get_extra_prompt()`

#### Scenario: Persistent prompt cleanup does not affect transient prompt
- **WHEN** `clear_persistent_extra_prompt()` is called between loop iterations
- **THEN** only the persistent extra prompt is cleared
- **AND** any transient extra prompt remains set

#### Scenario: Iteration summary generation
- **WHEN** a loop iteration completes
- **THEN** an LLM-generated summary of that iteration is produced
- **AND** the summary is appended to `accumulated_summaries`

#### Scenario: Iteration summaries propagate to the next iteration
- **WHEN** the loop controller starts a new iteration with non-empty `accumulated_summaries`
- **THEN** the accumulated summaries are injected into the task context/prompt for that iteration
- **AND** the iteration can reference prior iterations' work via that context

#### Scenario: Accumulated summaries are bounded to 8000 chars
- **WHEN** `add_summary` is called and the total character length across all accumulated summaries exceeds 8000
- **THEN** `_truncate_summaries` removes the oldest non-placeholder entries from the front of the list until the total falls back under 8000
- **AND** inserts a `[...earlier iterations omitted...]` placeholder at the front to mark truncated entries

#### Scenario: Placeholder preserved on subsequent truncation
- **WHEN** `_truncate_summaries` runs and the first entry is already the `[...earlier iterations omitted...]` placeholder
- **THEN** the placeholder is left at position 0 and the next real entry (index 1) is removed instead

### Requirement: Loop Mode Branch Isolation

Loop mode SHALL use git worktree-based branch isolation by default.

**Branch Naming:**
- Default (task-aware) form: `loop/{slugified_task_id}-{iteration}`, where `slugified_task_id` is derived from the task description (lowercased, non-alphanumeric chars replaced with hyphens, collapsed, trimmed, truncated to 30 chars; empty slugs fall back to `task`) and `iteration` is the 1-based loop iteration number.
- Legacy form: `se3-loop/{timestamp}` is used when no task description / task_id is available, and when an explicit timestamp is supplied.

**Worktree Lifecycle:**
1. Before loop: create the loop branch (per the naming rules above) from HEAD, create worktree at `se3/worktrees/{branch_safe_name}` (slashes in the branch name are replaced with hyphens)
2. During loop: all task flows execute in the worktree (worktree path passed as `project_root` to `run_flow()`)
3. After loop (non-interrupted): cleanup is automatic — no interactive three-way prompt is shown:
   - The worktree is removed (the branch is preserved at this stage)
   - If the loop branch has new commits relative to the original branch, it is auto-merged into the original branch; on success the loop branch is deleted, on conflict the branch is preserved and the user is told to resolve and merge manually
   - If the loop branch has no new commits, it is auto-discarded (branch deleted) and a "no changes" message is printed

**FlowInstance Fields:**
- `loop_branch`: The loop branch name (either `loop/{slug}-{iteration}` or legacy `se3-loop/{timestamp}`)
- `loop_worktree_path`: Reserved field for the active worktree directory path. The field is defined on `FlowInstance` and is round-tripped through `to_dict()` / `from_dict()` so persisted state preserves whatever value it holds across resumes, but the loop controller does not currently assign it during loop execution. Consumers MUST treat `loop_worktree_path` as optional and MUST NOT rely on it being populated; the authoritative worktree path is derived from `loop_branch` (via the `se3/worktrees/{branch_safe_name}` convention) or passed explicitly as `project_root` to `run_flow()`.
- `loop_original_branch`: Branch to merge back to

#### Scenario: `loop_worktree_path` is reserved and unpopulated
- **GIVEN** a loop-mode flow is running or has been persisted to `se3/state/engine.json`
- **WHEN** code inspects `FlowInstance.loop_worktree_path`
- **THEN** the field MAY be `None` even while the loop is actively executing in a worktree, because the loop controller does not assign it
- **AND** serialization (`to_dict`) and deserialization (`from_dict`) MUST still preserve any value present on the field so future writers can populate it without breaking persisted state
- **AND** the actual worktree path MUST be derived from `loop_branch` or supplied via the `project_root` argument to `run_flow()`, not by reading `loop_worktree_path`

**Interrupt Behavior:**
- On Ctrl-C during loop: remove worktree, preserve branch
- Print instructions for deferred merge: `se3 run --loop --merge {branch}`
- User can discard with `git branch -D {branch}`

**Deferred Merge:**
- `se3 run --loop --merge {branch}` shows a diff stat (`get_diff_stat`) comparing the branch to the current branch, then prompts for interactive confirmation before performing the merge (accepts either the new `loop/{slug}-{iteration}` form or the legacy `se3-loop/{timestamp}` form)
- On cancel: the merge is aborted without touching the working tree
- On merge conflict: abort merge, report error, user resolves manually

#### Scenario: Worktree isolation with task-aware naming
- **WHEN** `se3 run --loop "Implement feature X"` is executed without `--no-worktree`
- **THEN** a branch named `loop/{slugified-task}-{iteration}` (e.g. `loop/implement-feature-x-1`) is created
- **AND** a git worktree is set up at `se3/worktrees/`
- **AND** tasks execute in the isolated worktree

#### Scenario: Worktree isolation falls back to legacy naming
- **WHEN** loop mode runs without a task description / task_id
- **THEN** a `se3-loop/{timestamp}` branch is created instead
- **AND** the worktree is set up at `se3/worktrees/` as usual

#### Scenario: Post-loop auto-merge when commits exist
- **WHEN** loop mode finishes normally (not interrupted) and the loop branch has new commits relative to the original branch
- **THEN** the worktree is removed
- **AND** the loop branch is automatically merged into the original branch (no interactive prompt)
- **AND** on success the loop branch is deleted
- **AND** on merge conflict the loop branch is preserved with a message instructing the user to resolve manually

#### Scenario: Post-loop auto-discard when no commits
- **WHEN** loop mode finishes normally (not interrupted) and the loop branch has no new commits
- **THEN** the worktree is removed
- **AND** the loop branch is automatically deleted (no interactive prompt)
- **AND** a "no changes" message is printed

#### Scenario: Loop interrupt cleanup
- **WHEN** user presses Ctrl-C during loop mode
- **THEN** worktree is removed
- **AND** loop branch is preserved for later merge
- **AND** instructions are printed for deferred merge

#### Scenario: Deferred merge with interactive confirmation
- **WHEN** `se3 run --loop --merge loop/implement-feature-x-1` (or a legacy `se3-loop/20260324-120000`) is executed
- **THEN** a diff stat comparing the loop branch to the current branch is displayed via `get_diff_stat`
- **AND** the user is prompted with "Proceed with merge?" and the options `Merge <branch> into <target>` / `Cancel`
- **AND** on confirmation the branch is merged into the current branch and success or conflict is reported
- **AND** on cancel the merge is aborted without touching the working tree

### Requirement: `se3 run` CLI Options

The `se3 run` command SHALL accept the following options in addition to the startup, discovery, resume, and loop options described in earlier requirements.

**Additional Options:**

| Option | Short | Description |
|--------|-------|-------------|
| `--from-issue [ID]` | — | Run a new flow seeded by an existing issue. When `ID` is supplied, that issue is loaded; when the flag is supplied without a value, the user is prompted to pick from open issues. |
| `--list-loops` | — | List unmerged loop branches and exit without running any flow. |
| `--flow-id ID` | — | Resume the specific flow with the given id (equivalent to `--resume` but without the interactive picker). |
| `--change NAME` | `-c` | Optional human-readable change name attached to the flow. |
| `--max-iterations N` | `-n` | Maximum loop iterations. Defaults to `10` when not provided. |

**`--from-issue` Behavior:**
1. Load the referenced issue (interactively prompting if no id was supplied).
2. Refuse to start if the issue is already `in-progress`; the user is told to run `se3 issue reset` first.
3. Transition the issue to `in-progress`, run the flow using the issue's description as the task description, and record the issue id on the flow as `source_issue_id`.
4. On flow completion, transition the issue to `resolved` on exit code 0, or back to `open` on non-zero exit. Status-update failures are tolerated as best-effort.

**`--list-loops` Behavior:**
- Prints each unmerged loop branch with its commit count ahead of its base branch.
- Prints usage hints for merging (`se3 run --loop --merge <branch>`) and discarding (`git branch -D <branch>`).
- Exits with code 0 without invoking the flow engine.

**`--flow-id` Behavior:**
- When supplied (with or without `--resume`), the resume path is taken directly for the given flow id, bypassing the interactive resume picker that `--resume` alone uses.

**`--max-iterations` Default:**
- The `max_iterations` parameter passed to loop mode defaults to `10` when the user does not specify a value.

#### Scenario: Run from an explicit issue id
- **WHEN** the user runs `se3 run --from-issue ISSUE-123`
- **THEN** the issue is loaded and transitioned to `in-progress`
- **AND** a new flow is started with the issue's description as the task description and `source_issue_id` set to `ISSUE-123`
- **AND** on a successful flow the issue is transitioned to `resolved`; on a failed flow it is transitioned back to `open`

#### Scenario: Run from an issue with interactive selection
- **WHEN** the user runs `se3 run --from-issue` without an id
- **THEN** the open issues are listed and the user is prompted to enter an issue id
- **AND** the selected issue drives the flow as described above

#### Scenario: Refuse to run from an in-progress issue
- **WHEN** the user runs `se3 run --from-issue ISSUE-123` and `ISSUE-123` is already in-progress
- **THEN** the command prints a message instructing the user to run `se3 issue reset ISSUE-123` first
- **AND** exits with a non-zero status without starting a flow

#### Scenario: List unmerged loop branches
- **WHEN** the user runs `se3 run --list-loops`
- **THEN** the unmerged loop branches are printed with their commit counts ahead of their base branches
- **AND** the command exits without running any flow

#### Scenario: Resume a specific flow id
- **WHEN** the user runs `se3 run --flow-id <id>`
- **THEN** the flow with that id is resumed directly without the interactive resume picker

#### Scenario: Default maximum iterations
- **WHEN** the user runs `se3 run --loop` without `--max-iterations`
- **THEN** loop mode runs with a maximum of 10 iterations

### Requirement: Coexistence with `se3 merge`

#### Scenario: Coexistence with standalone `se3 merge`
- **GIVEN** the project supports both the in-loop `se3 run --loop --merge <branch>` path and the standalone `se3 merge <branch> [<branch> ...]` command
- **WHEN** a user wants to fold one loop branch back into the original branch at the end of an iteration
- **THEN** they use `se3 run --loop --merge`, which retains the existing in-loop semantics (single branch, governed by `conflict_resolver.strategy`)
- **WHEN** a user wants to aggregate multiple parallel-task branches into the current branch in one shot, with strategy tiers, mandatory spec guardrails, and aggregated SemVer bumping
- **THEN** they use `se3 merge <branch> [<branch> ...]`, which is governed by the `merge.*` config section and is independent of `conflict_resolver.strategy`
- **AND** the two commands intentionally coexist — neither replaces the other

### Requirement: Standalone `se3 merge` CLI Options and Strategies

The standalone `se3 merge` command SHALL accept one or more branch names and the following options that govern aggregation strategy and post-merge cleanup.

**Options:**

| Option | Description |
|--------|-------------|
| `<branch> [<branch> ...]` | One or more branch names to merge sequentially into the current branch. Order is preserved; duplicates are deduplicated with a warning. |
| `--strategy=fast\|safe\|strict` | Selects the conflict-resolution strategy tier. Defaults to `fast`. Passing the legacy value `default` raises a migration error — use `safe` instead. |
| `--delete-merged` | When set, merged source branches are deleted (and their worktrees archived) after a successful aggregated merge. Defaults to true. |
| `--no-delete-merged` | Disables deletion of merged branches (overrides both `--delete-merged` and the config default). |

**Strategy Tiers:**
- `safe` and `strict`: Refuse to start when the working tree has uncommitted tracked changes or in-progress git state (merge/cherry-pick/revert/rebase). The user must commit or stash first. The legacy value `default` is no longer accepted — `MergeStrategy.from_str` raises a migration error directing the user to `safe`.
- `fast`: Auto-stashes the user's dirty working tree (tracked + untracked) inside the merge lock before merging and pops the stash after the orchestrator returns. If the in-progress git marker check still detects an unfinished merge/cherry-pick/rebase, the fast strategy ALSO refuses to start — stashing cannot recover from that state. On pop conflicts, the fast strategy resolves deterministically by taking the merged (HEAD) version ("take-ours") for affected files, drops the stash, and files an audit issue describing the affected files.

**Branch Name Validation:**

The CLI SHALL validate branch names before any git command runs and reject:
- Empty list, empty string entries, or non-string entries
- Names starting with `-` (could be misinterpreted as CLI flags)
- The reserved git pseudo-refs `HEAD` and `@`
- Shell metacharacters: `$`, `` ` ``, `;`, `&`, `|`, `<`, `>`, `(`, `)`, `{`, `}`, `!`, `\`, `"`, `'`, newline, carriage return, tab
- Git-ref-invalid glob characters: `*`, `?`, `[`, `]`
- ASCII control characters (< 0x20)
- Spaces
- Git ref-format violations: `..`, leading `.` or `/`, trailing `/` or `.lock`, `@{`, `:`, `~`, `^`

The `=` character is intentionally NOT rejected — git permits it and subprocess calls use list-form argv.

**Pre-Merge Validation:**

Before merging, the command SHALL also reject:
- Detached HEAD state
- Attempting to merge the current branch into itself
- Attempting to merge the protected base branches `main` or `master`
- Branches that do not exist locally

#### Scenario: Multiple branches merged sequentially
- **WHEN** the user runs `se3 merge feature-a feature-b feature-c`
- **THEN** the orchestrator merges each branch into the current branch in order
- **AND** duplicate branch names are deduplicated with a warning before merging

#### Scenario: Fast strategy auto-stashes dirty tree
- **GIVEN** the user has uncommitted tracked or untracked changes
- **WHEN** the user runs `se3 merge <branch> --strategy=fast`
- **THEN** the working tree is auto-stashed under a generated label inside the merge lock
- **AND** the stash is popped after the orchestrator returns
- **AND** pop conflicts are resolved by taking the merged (HEAD) version and the stash is dropped
- **AND** an audit issue is filed describing the affected files

#### Scenario: Non-fast strategy refuses dirty tree
- **GIVEN** the user has uncommitted tracked changes
- **WHEN** the user runs `se3 merge <branch>` without `--strategy=fast`
- **THEN** the command prints an error instructing the user to commit/stash or use `--strategy=fast`
- **AND** exits with a non-zero status without invoking the orchestrator

#### Scenario: Merge refused during in-progress git operation
- **GIVEN** the repository is mid-merge, cherry-pick, revert, or rebase (`MERGE_HEAD`, `CHERRY_PICK_HEAD`, `REVERT_HEAD`, `rebase-merge`, or `rebase-apply` exists)
- **WHEN** the user runs `se3 merge <branch>` under any strategy
- **THEN** the command refuses to start and exits non-zero
- **AND** even the fast strategy refuses because stashing cannot recover from an in-progress git state

#### Scenario: Reject invalid branch names
- **WHEN** the user runs `se3 merge -rf` or `se3 merge 'a;b'` or `se3 merge HEAD`
- **THEN** the command rejects the input with a message listing each rejected name and the rule violated
- **AND** exits non-zero before invoking git

#### Scenario: Reject protected and self-merge targets
- **WHEN** the user runs `se3 merge main` or `se3 merge master`, or tries to merge the current branch
- **THEN** the command refuses with an explanatory error and exits non-zero

#### Scenario: Reject missing or detached-HEAD inputs
- **WHEN** a supplied branch does not exist locally, or HEAD is detached
- **THEN** the command refuses with an explanatory error and exits non-zero

#### Scenario: No-delete-merged preserves branches after merge
- **WHEN** the user runs `se3 merge <branch> --no-delete-merged`
- **THEN** merged source branches are NOT deleted after a successful merge
- **AND** `--no-delete-merged` overrides both the `--delete-merged` flag and the config default

### Requirement: Standalone `se3 merge` Concurrency Lock

The standalone `se3 merge` command SHALL serialize concurrent invocations via an exclusive file-based merge lock so that two runs cannot mutate the same working tree, index, and runtime-sync targets simultaneously.

**Lock Behavior:**
- The lock file lives at `se3/state/merge.lock` and is acquired with `fcntl.flock` inside the `MergeLock` context manager (`src/se3/commands/merge/merge_lock.py`).
- The CLI wrapper acquires the lock; the orchestrator is invoked with `acquire_lock=False` so the same process does not try to re-acquire the same lock (which would surface as `MergeLockBusy`).
- Any pre-merge stashing under the fast strategy happens INSIDE the lock; the pre-lock dirty check is used only to capture the user's pre-merge intent, since the lock file itself would otherwise show up as untracked dirty state.

**Lock Errors:**
- `MergeLockBusy`: another `se3 merge` is already running. The CLI prints the holder PID and lock file path and exits non-zero.
- `MergeLockStale`: the lock file exists but the holder PID no longer exists or is unparseable. The CLI prints recovery instructions (`rm <lock file>`) and exits non-zero.

#### Scenario: Concurrent merges are serialized
- **GIVEN** one `se3 merge` invocation has acquired the merge lock
- **WHEN** a second `se3 merge` invocation starts in the same project
- **THEN** the second invocation reports the holder PID and lock file path
- **AND** exits non-zero without touching the working tree

#### Scenario: Stale merge lock is detected
- **GIVEN** `se3/state/merge.lock` exists but the recorded PID no longer exists
- **WHEN** the user runs `se3 merge <branch>`
- **THEN** the command reports the stale lock and prints instructions to remove the lock file
- **AND** exits non-zero

### Requirement: Standalone `se3 merge` Post-Conditions and Guardrails

The standalone `se3 merge` orchestrator SHALL enforce post-condition assertions and spec guardrails after each branch merge, and SHALL surface the outcome via structured failure reasons.

**Post-Condition Assertions** (`src/se3/commands/merge/postcondition.py`):
- `assert_head_is_merge_commit`: verifies HEAD references a merge commit when one was expected.
- `assert_branch_merged`: verifies the source branch is reachable from HEAD after merging.
- `assert_version_bumped`: verifies the project version was bumped according to the aggregated SemVer policy.
- `check_all`: composite check invoked at the end of the merge sequence.
- Violations raise `PostConditionViolated`, which the orchestrator translates into a failure report.

**Spec Guardrails:**
- Spec guardrails run as part of the merge sequence; violations are reported with dedicated failure reasons (e.g. `guardrail_violation`, `guardrail_violation_no_rollback`, `guardrail_repair_stalled`, `guardrail_repair_exhausted`, `guardrail_check_failed`).
- Under the fast strategy, repair attempts may run automatically; if repair stalls or exhausts its budget, the merge is paused for human review (exit code 130) with a call file and log file path printed.

**Failure Reasons** (`src/se3/commands/merge/failure_reason.py`):
- A typed `FailureReason` enum with `from_legacy_string` / `to_legacy_string` helpers converts between structured enums and legacy string identifiers used in `MergeReport.failure_reason`.
- The CLI renders human-readable titles and summaries for each failure reason via `_failure_title_and_summary`, distinguishing git conflicts, fast-mode aborts, guardrail violations, runtime sync collisions, post-condition failures, and rollback failures.

**Exit Codes:**
- `0`: success.
- `130`: pending human review (e.g. guardrail repair stalled/exhausted, conflict resolution requires human decision); the CLI prints a call file and log file path.
- `1`: any other failure.

#### Scenario: Post-condition violation aborts merge
- **GIVEN** a branch has been merged but a post-condition assertion (`assert_branch_merged`, `assert_head_is_merge_commit`, or `assert_version_bumped`) fails
- **WHEN** `check_all` runs at the end of the merge sequence
- **THEN** `PostConditionViolated` is raised and translated into a failure report
- **AND** the CLI exits non-zero with an explanatory title and summary

#### Scenario: Fast-strategy guardrail repair pauses for human review
- **WHEN** the fast strategy detects a guardrails violation and the repair stalls or exhausts its budget
- **THEN** the CLI prints "Merge paused for human review" with the call file and log file paths
- **AND** exits with code 130

#### Scenario: Structured failure reasons map to human messages
- **WHEN** the orchestrator reports a `failure_reason` such as `merge_conflict`, `guardrail_violation`, `runtime_sync_collision`, `binary_file_conflict`, `merge_timed_out`, or `version_higher_than_target`
- **THEN** the CLI renders a dedicated title and first-line summary via `_failure_title_and_summary`
- **AND** falls back to a generic "Merge failed: <reason>." message only when no dedicated entry exists

### Requirement: Standalone `se3 merge` Secret Redaction and LLM Tracing

The standalone `se3 merge` subsystem SHALL redact secrets from LLM inputs/outputs and record per-call traces for auditability.

**Secret Redaction** (`src/se3/commands/merge/secret_redact.py`):
- A `SecretRedactor` configured via `RedactConfig` redacts secrets from text and diffs before they are sent to or persisted from LLM calls.
- Convenience functions `redact_text` and `redact_diff` provide one-shot redaction with default configuration.

**LLM Tracing** (`src/se3/commands/merge/llm_trace.py`):
- `LLMCallRecord` captures per-call metadata (prompt, response, latency, token counts, etc.) for each LLM invocation made during a merge.
- `LLMTrace` aggregates `LLMCallRecord`s for a single merge run and is persisted alongside the merge log file for later inspection.

#### Scenario: Secrets are redacted from LLM payloads
- **WHEN** the merge orchestrator builds a prompt or persists an LLM response that contains a secret matched by `RedactConfig`
- **THEN** the secret is replaced with a redaction placeholder via `redact_text` / `redact_diff` before the payload leaves the redaction boundary

#### Scenario: Per-call LLM trace is recorded
- **WHEN** the fast strategy invokes the LLM for conflict resolution or guardrail repair
- **THEN** each call is captured as an `LLMCallRecord` and appended to the merge's `LLMTrace`
- **AND** the trace is referenced from the merge log file printed in the CLI output

### Requirement: Standalone `se3 merge` Cleanup and Runtime Sync Reporting

The standalone `se3 merge` command SHALL report cleanup actions and runtime-sync signals in its CLI output, and SHALL split merged branches into "newly merged" and "already an ancestor" buckets.

**Merged-Branch Bucketing:**
- `MergeReport` exposes `newly_merged_branches`, `already_ancestor_branches`, and `merged_with_warnings` lists. The CLI uses `_split_merged_buckets` to classify each branch and renders the two buckets separately so operators can tell which branches produced a new merge commit and which were already reachable from HEAD.
- Branches in `merged_with_warnings` (fast-mode guardrail repair ran) are folded into the "newly merged" bucket for CLI rendering while remaining separately addressable for structured consumers.
- A defensive fallback uses `git merge-base --is-ancestor` to classify branches when the orchestrator did not populate the new buckets.

**Cleanup Reporting:**
- When `--delete-merged` is in effect and the merge succeeds, the CLI prints:
  - Archived worktrees (path each was archived to)
  - Deleted branches
  - Skipped (dirty worktree) with reasons
  - Skipped (archive failed — preserving worktree + branch) with reasons
  - Skipped (protected) branches
  - Skipped (unknown state) with reasons
  - Skipped (worktree removal failed) with reasons
  - Skipped (not fully merged) with reasons

**Runtime-Sync Reporting** (`_append_runtime_sync_lines`):
- The CLI renders runtime-sync signals across every output branch (success, rollback failed, pending human, generic failure) so partial-sync state is never lost:
  - Skipped branches (no bound worktree found)
  - Skipped files (with reason categories such as destination is a directory, sidecar name too long, sidecar exhausted)
  - Idempotent bypasses (sidecar already matched source content — possible stale sidecar from a prior aborted run)
  - Tier B discarded (branch-side state preserved by current branch)
  - Tier A collisions, split into "written" (sidecar bypass) and "audit-only" (sidecar NOT written, source data not recoverable from disk)

**Aggregated Version Reporting:**
- On success, the CLI prints `Version: <effective_base> -> <final_version>` using `effective_pre_merge_version` (or `pre_merge_version`) as the base.
- When `effective_pre_merge_version` differs from `pre_merge_version`, an additional `(HEAD already at <version> from prior merges)` line is rendered.
- A `version_higher_than_target` warning is rendered when the on-disk version exceeds the aggregated target.
- A `version_aggregation_error` warning is rendered when version aggregation fails.

#### Scenario: Merged branches split into newly-merged and already-ancestor buckets
- **WHEN** the CLI renders a successful merge of multiple branches where some are already reachable from HEAD
- **THEN** "Newly merged" branches are listed under their own header
- **AND** "Already an ancestor of HEAD — no new commit" branches are listed under a separate header
- **AND** branches in `merged_with_warnings` appear under "Newly merged"

#### Scenario: Cleanup report rendered on success
- **GIVEN** `--delete-merged` is in effect and the merge succeeds
- **WHEN** the CLI renders the result
- **THEN** archived worktrees, deleted branches, and each "skipped" bucket (dirty, archive-failed, protected, unknown state, worktree removal failed, not fully merged) are printed with their per-branch reasons

#### Scenario: Runtime-sync signals appear in every output branch
- **WHEN** the orchestrator reports runtime-sync skipped branches/files, idempotent bypasses, tier B discards, or tier A collisions
- **THEN** the CLI renders these signals consistently regardless of whether the merge succeeded, failed, rolled back, or paused for human review
- **AND** tier A collisions are split into "written" (sidecar bypass) and "audit-only" (sidecar NOT written, source data not recoverable) sections

#### Scenario: Aggregated version transition is reported
- **WHEN** the merge succeeds and `report.final_version` is set
- **THEN** the CLI prints `Version: <effective_base> -> <final_version>`
- **AND** prints a `(HEAD already at <version> from prior merges)` line when `effective_pre_merge_version` differs from `pre_merge_version`
- **AND** prints a `version_higher_than_target` warning when the on-disk version exceeds the aggregated target
