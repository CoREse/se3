<!-- spec-format: v1 -->

# flow-engine Specification

## Purpose

Defines the core flow engine (Flow Engine) of SE3 3.0: a program-driven state machine that, through the unified `se3 run` entry point, controls the orchestration of the 16 steps of the development flow (5 active steps + CONFIRM + DISCOVERY + 4 deprecated steps + others), invoking the LLM within each step to handle the parts that require "thinking".

## Requirements

### Requirement: Unified entry point `se3 run`

`se3 run` SHALL serve as the sole flow entry point of SE3 3.0, replacing the manual chaining of `se3:start` / `se3:work` / `se3:done`.

**Interface:**
```bash
# New task
se3 run "implement user login feature"

# Resume an interrupted task
se3 run --resume

# Worktree isolation mode (run in an isolated git worktree, auto-merge on success)
se3 run --worktree "implement user login feature"

# Specify the task type
se3 run "fix memory leak" --type=bugfix

# Discovery mode (requirement exploration)
se3 run --discover "I want to build a user management feature"
```

#### Scenario: New task startup
- **WHEN** the user executes `se3 run "implement user login feature"`
- **THEN** the flow engine creates a new flow instance
- **AND** execution starts from the `analyze` step

#### Scenario: Discovery mode startup
- **WHEN** the user executes `se3 run --discover "initial idea"`
- **THEN** the flow engine creates a flow instance of type discovery
- **AND** execution starts from the `discovery` step
- **AND** explores the requirement with the user through multiple rounds of dialogue
- **AND** after the user confirms, uses the refined description to enter the `analyze` step

#### Scenario: Resume an existing task
- **WHEN** the user executes `se3 run` and an unfinished flow state exists
- **THEN** the flow engine prompts to resume or create a new one
- **AND** if resume is chosen, continues from the interruption point

#### Scenario: Worktree isolation mode startup
- **WHEN** the user executes `se3 run --worktree "implement user login feature"`
- **THEN** the flow engine first creates an isolation branch and a git worktree under `se3/worktrees/{branch_safe_name}` (per the generic `create_worktree` path convention in the `worktree-management` spec)
- **AND** the flow body executes inside that worktree using **exactly the same** step sequence, state persistence, `--resume`, and `--type` handling as a synchronous run (`project_root` is the worktree path)
- **AND** the worktree flow body does NOT hold the main-worktree lock, so multiple `--worktree` runs may execute their flow bodies concurrently (see the `se3 merge` Concurrency Lock requirement in the `se3-commands` spec)

#### Scenario: Worktree flow persists engine.json at creation so the daemon can observe it immediately
- **WHEN** `se3 run --worktree` creates a new flow and records its worktree metadata (`worktree_path` / branch) on the `FlowInstance`
- **THEN** the engine SHALL immediately persist `engine.json` for that flow carrying `is_worktree_mode=True`, **before** the discovery step's first LLM call — rather than deferring the first save until a later explicit-type branch
- **AND** because the daemon's runtime-observable set gates worktree inclusion on the persisted `is_worktree_mode` flag (`_active_worktree_run_roots()`), this eager write closes the startup-window blind spot in which the worktree flow's first discovery reply was not yet observable, so the worktree becomes live-observable from its first history write onward (see the `running-flow-console` live-stream scenario)
- **AND** the persisted flag remains a strict gate: a DAG-isolation worktree that is not `is_worktree_mode` is still excluded, so this eager persistence does not register the worktree as a standalone project (the project-list exclusion is unaffected)

#### Scenario: Worktree run auto-merges back on success
- **WHEN** a `se3 run --worktree` flow reaches a genuinely COMPLETED status
- **THEN** the run automatically invokes the heavy `se3 merge` orchestrator from the main repository to merge the isolation branch back into the original branch (version bump, postcondition assertions, typed `FailureReason`, and context-aware LLM conflict resolution all apply)
- **AND** no additional diff-confirmation interaction is required (the merge is transparent to the user)
- **AND** the auto-merge acquires the main-worktree lock (blocking) before merging

#### Scenario: Worktree run failure or interruption preserves state for resume
- **WHEN** a `se3 run --worktree` flow fails or is interrupted before completing
- **THEN** the run state, the isolation worktree, and its branch are preserved for a later `se3 run --resume`, exactly as a synchronous run preserves its state
- **AND** the merge-back step is NOT triggered, because the flow did not succeed

#### Scenario: Synchronous mode is the default and unchanged
- **WHEN** the user executes `se3 run "..."` without `--worktree`
- **THEN** the flow executes in place on the current working tree with behavior identical to prior synchronous runs
- **AND** no isolation worktree is created and no automatic merge step is appended

### Requirement: Discovery Workflow

The `discovery` step SHALL implement a multi-turn dialogue mechanism that helps users explore and clarify requirements when the requirement is not clear.

**Workflow:**
1. **Initial exploration**: Based on the user's preliminary description, the AI poses clarifying questions
2. **Dialogue iteration**: After the user answers, the AI continues probing or shifts to synthesis
3. **Synthesis and confirmation**: The AI summarizes its understanding and generates a refined task description
4. **Programmatic confirmation gate**: After the LLM determines the requirement is clear and generates a refined description, it directly transitions to PAUSED entering the programmatic confirmation gate, where the user makes the final adjudication
5. **Entering analysis**: After the user confirms, the refined description is used to continue with the `analyze` step

**State management:**
- The dialogue history is stored in `discovery_state`
- Supports interruption at any turn and resumption via `se3 run --resume`
- A maximum dialogue turn limit (default 10 turns) prevents infinite loops

**LLM invocation modes:**
- `question` mode: poses a specific question to the user
- `synthesis` mode: summarizes understanding and generates a refined description
- `confirmation` mode: the LLM determines the requirement is clear, returns the refined description, and then pauses, waiting for the programmatic gate

**Context consultation (read-only):**

`discovery` is a read-only step. Both the `question` (`INITIAL_DISCOVERY_PROMPT`) and `synthesis` (`CONTINUE_DISCOVERY_PROMPT`) templates SHALL instruct the LLM that, to ask better, more informed questions, it MAY consult specs and source code on demand, distinguishing the two surfaces:

- **Specs** — consulted through the bounded, read-only `se3 spec` index commands run via Bash: `se3 spec index` (root view; drill in with `se3 spec index <spec> [<group>...]`) to navigate, then `se3 spec show <spec>::<requirement>` to read one Requirement's body. The templates SHALL NOT instruct the LLM to read an entire `se3/specs/<name>/spec.md` file with the Read tool (large specs exceed the Read size limit).
- **Source code** — consulted with `Read` / `Grep` / `Glob` as usual.

**Handling of evaluation/inquiry-type initial descriptions:**

When the user's initial description manifests as an evaluation, judgment, review, or inquiry about existing code/solutions/changes (e.g., "Is this the right way to do it", "Judge whether X is reasonable", "Is there a problem with the Y solution", "Carefully evaluate this change", "Is this correct?", "Evaluate X", "Review this change", or a question with embedded references to specific code/files/commits), `INITIAL_DISCOVERY_PROMPT` and `CONTINUE_DISCOVERY_PROMPT` SHALL instruct the LLM to avoid asking clarifying questions about the task definition itself such as "what is the task / what is the task scope / what do you want to do", and instead to:

1. First read the relevant code/context (tools such as Read, Grep, Glob, Bash, etc.)
2. Form a specific, substantive evaluation/opinion
3. Exchange views with the user on the evaluation content itself, raise content-specific follow-up questions or counterarguments
4. Converge through multiple turns of dialogue to a consensus on the "correct approach" (which may be any conclusion such as keeping the status quo, a local fix, a complete redo, switching solutions, etc.)
5. Submit the **correct approach reached by consensus** as the `refined_description` to the confirmation gate / `analyze` step, rather than passing through the user's evaluation request verbatim

Reasonable follow-up questions on non-task-definition aspects such as "output form / delivery boundary / priority / constraints" are still allowed. Recognition relies on the LLM making its own judgment per the prompt instructions, accepts the uncertainty of borderline ambiguous cases, and does not pursue 100% avoidance; no keyword matching or classification heuristics are introduced at the code layer. `CONTINUE_DISCOVERY_PROMPT` SHALL maintain the same substantive-discussion stance in subsequent turns, prohibiting drifting back to "let me re-confirm the task scope" midway.

**Programmatic confirmation gate:**

After the LLM's `confirmation` mode determines the requirement is clear and generates a refined description, the discovery step does not complete directly, but instead returns the `PAUSED` status and sets `awaiting_programmatic_confirm=True`. After the program run loop detects this flag, it reads user input in discovery's ordinary input box:

- If `user_input.rstrip('\n\r') == "1"` (only stripping trailing newline characters to accommodate the trailing newline artifact produced by multi-line input UIs; no other strip/normalize is performed, and variants such as `1.`, `1 ok`, ` 1 ` (leading or interior spaces), `yes` are not allowed) — treated as **confirm and continue**, entering the implementation planning phase
- If `user_input` is empty (the user presses Enter directly) — treated as a **no-op**: the `awaiting_programmatic_confirm` flag is not cleared, the discovery dialogue is not advanced, no LLM call is triggered, and only the already-cached confirmation Panel is redrawn
- Any other non-empty input — treated as **still having questions**: the `awaiting_programmatic_confirm` flag is cleared, and that input is used directly as the user input for the next discovery turn, with no separate prompt for input

Rationale for choosing `1` rather than `yes`: `1` is a language-independent universal symbol, leaving room for future non-English interfaces. The strict `==` check after `rstrip('\n\r')` avoids `1. I also want to add…` being misjudged as confirmation; the trailing-newline stripping is used only to accommodate the trailing `\n` appended by multi-line input UIs when the user types `1` + Enter, and does not introduce looser normalization semantics.

This ensures that the LLM's confirmation judgment does not unilaterally advance the flow, and that humans always retain the final decision power.

#### Scenario: Requirement exploration dialogue
- **GIVEN** the user executes `se3 run --discover "I want to build a user-related feature"`
- **WHEN** the discovery step executes
- **THEN** the AI asks: "Who is this user feature for? Administrators or ordinary users?"
- **AND** after the user answers, it continues probing or synthesizing

#### Scenario: Generating a refined description
- **GIVEN** after multiple turns of dialogue
- **WHEN** the AI enters synthesis mode
- **THEN** a structured task description is generated
- **AND** it pauses waiting for user confirmation

#### Scenario: Evaluation/inquiry-type initial description — does not ask back about task scope
- **GIVEN** the user executes `se3 run --discover "Carefully, comprehensively, and objectively judge whether this change is reasonable"` (or similar evaluation/inquiry-type input, English "Is this change reasonable?", "Review this modification carefully", etc.)
- **WHEN** the initial turn of the discovery step executes
- **THEN** the LLM, per the prompt instructions, first reads the relevant code/context and forms a substantive evaluation of the change
- **AND** does not output clarifying questions about the task definition itself such as "what do you want to do / what is the task scope / what is your goal"
- **AND** may exchange views with the user on the evaluation content itself or raise content-specific follow-up questions
- **AND** after converging through multiple turns of discussion, the `refined_description` describes the correct approach reached from the discussion (keep the status quo / local fix / redo / switch solution, etc.), rather than the user's original evaluation request

#### Scenario: Discovery interruption recovery
- **GIVEN** the user interrupts (Ctrl+C) during the 3rd turn of dialogue
- **WHEN** the user executes `se3 run --resume`
- **THEN** it resumes to the discovery step
- **AND** continues the 3rd turn of dialogue

#### Scenario: Discovery confirmation phase recovery display
- **GIVEN** the discovery step is paused in confirmation mode
- **AND** `awaiting_programmatic_confirm=True`
- **AND** `step.outputs["refined_description"]` contains the refined description
- **AND** `proposed_description` does not exist in `step.outputs`
- **WHEN** the user executes `se3 run --resume`
- **THEN** when `_restore_discovery_display()` reads the description from `step.outputs`, it preferentially takes `proposed_description`
- **AND** when `proposed_description` does not exist, it falls back to `refined_description`
- **AND** the `refined_description` is rendered correctly as markdown
- **AND** in the discovery ordinary input box, the user is prompted to input `1` to confirm and continue, and any other input is used as the user input for the next discovery turn

#### Scenario: Programmatic confirmation gate — user confirms continuation
- **GIVEN** the LLM in confirmation mode determines the requirement is clear
- **AND** the discovery step returns PAUSED with `awaiting_programmatic_confirm=True`
- **WHEN** the program reads user input in the discovery ordinary input box and `user_input.rstrip('\n\r') == "1"` (only stripping trailing newline characters to accommodate multi-line input UI artifacts, no other strip/normalize)
- **THEN** `programmatic_confirmed=True` is set into the step inputs
- **AND** the discovery handler is re-executed, and after the handler detects this flag it directly completes the step
- **AND** `discovery_summary` is generated and `requirements_clarified=True` is set

#### Scenario: Programmatic confirmation gate — user continues exploration
- **GIVEN** the LLM in confirmation mode determines the requirement is clear
- **AND** the discovery step returns PAUSED with `awaiting_programmatic_confirm=True`
- **WHEN** the user input read by the program in the discovery ordinary input box, after `rstrip('\n\r')`, is not strictly equal to `"1"` and is non-empty after trailing-newline removal (including variants such as `1.`, `1 ok`, ` 1 ` (leading or interior spaces), `yes`)
- **THEN** the `awaiting_programmatic_confirm` flag is cleared
- **AND** that user input is used directly as the user input for a new discovery turn, with no separate prompt for input

#### Scenario: Programmatic confirmation gate — empty input no-op
- **GIVEN** the LLM in confirmation mode determines the requirement is clear
- **AND** the discovery step returns PAUSED with `awaiting_programmatic_confirm=True`
- **WHEN** the user input read by the program in the discovery ordinary input box is empty (the user presses Enter directly)
- **THEN** the `awaiting_programmatic_confirm` flag remains unchanged
- **AND** no new discovery dialogue turn is created
- **AND** no LLM call is triggered; only the already-cached `refined_description` is used to redraw the confirmation Panel
- **AND** the user can see the confirmation prompt again and input `1` to confirm or input other content to continue exploration

#### Scenario: Discovery output passing
- **GIVEN** the discovery step completes and the user has confirmed via the programmatic confirmation gate
- **WHEN** the flow enters the `analyze` step
- **THEN** the `refined_description` is automatically passed to analyze as `task_description`

**Clarification Q&A in non-interactive mode:**

When the discovery step runs in non-interactive mode (spawned on behalf by the daemon, `--output-format json`, no available terminal), there is no input box for blocking reads. In this case the discovery step SHALL NOT block on terminal input, but instead writes the clarifying question as a call file under the `se3/calls/` directory and returns the `PAUSED` status, reusing the existing call/response mechanism so the user can respond interactively on the web page via "Respond to Flow"; after the user's response file is consumed, the flow resumes via the equivalent path of `se3 run --resume` and enters the next discovery turn. No dedicated interactive dialogue interface is newly built for web-initiated discovery tasks — multi-turn clarification Q&A uniformly reuses the existing call/response channel.

The call file for a non-interactive discovery pause SHALL be written via the shared `interaction_calls.write_call`, distinguishing two pause forms:

- **Clarifying question (question) pause**: written as an ordinary `CALL_KIND_CALL`, with `prompt` carrying the LLM's clarifying question text.
- **Programmatic confirmation gate (confirmation) pause** (`outputs["awaiting_programmatic_confirm"]` is true): written as the dedicated `CALL_KIND_DISCOVERY_CONFIRM` kind. The `prompt` is generated by `steps/discovery.discovery_confirm_metadata(refined_description)`, carrying the fallback `Enter 1 to confirm` wording plus the refined task description; the `options` carries a one-click confirmation action whose `value` is the literal `"1"` (i.e., the confirmation token expected by the gate's `== "1"` check), so the web console can both render a GUI confirmation button (clicking it sends `"1"` via the reply channel) and retain the `Enter 1 to confirm` wording.

Both calls write `flow_id` / `step_id` into `context` (for the aggregator's per-flow filtering by flow ownership) and mirror them as top-level fields for compatibility with old readers. The GUI confirmation button does not introduce a new channel; the submitted `"1"` and the user's manually typed `1` go through the same call/response reply channel and are consumed by the same programmatic confirmation gate.

#### Scenario: Non-interactive mode discovery asks questions via call/response
- **GIVEN** the discovery task is spawned on behalf by the daemon (`se3 run --discover --output-format json`, no available terminal)
- **WHEN** the discovery step needs to pose a clarifying question to the user
- **THEN** it does not block on terminal input, but instead writes the question as a call file under `se3/calls/`
- **AND** the call's `kind` is `CALL_KIND_CALL`, with `prompt` carrying the LLM's clarifying question
- **AND** the step returns the `PAUSED` status
- **AND** the user responds to that call interactively on the web page via the existing "Respond to Flow"
- **AND** after the response file is consumed, the flow resumes and enters the next discovery dialogue turn

#### Scenario: Non-interactive mode discovery programmatic confirmation gate writes discovery_confirm call
- **GIVEN** the discovery task is spawned on behalf by the daemon (no available terminal) and the step is paused at the programmatic confirmation gate (`outputs["awaiting_programmatic_confirm"]` is true)
- **WHEN** `_write_discovery_call` writes the call file
- **THEN** the call's `kind` is `CALL_KIND_DISCOVERY_CONFIRM`
- **AND** the `prompt` is generated by `discovery_confirm_metadata`, containing the fallback `Enter 1 to confirm` wording and the refined task description
- **AND** the `options` contains a one-click confirmation action whose `value` is the literal `"1"`
- **AND** the `context` carries `flow_id` / `step_id` for the aggregator's filtering by flow ownership
- **AND** responses submitted by the user via the GUI confirmation button or by manually typing `1` are both consumed by the same programmatic confirmation gate, and the gate's strict `== "1"` semantics remain unchanged

**Web-mirrored responding in interactive mode (CLI/TTY):**

Interactive discovery pauses previously only blocked on terminal reads and did not write call files, so a discovery session initiated from the CLI had 'no object to reply to' in the web console. Now interactive clarification Q&A pauses (`_handle_discovery_pause`) and the programmatic confirmation gate (`_handle_discovery_programmatic_confirm`) SHALL likewise mirror the pause as a call file under `se3/calls/` via `_maybe_write_discovery_call` (clarification Q&A writes `CALL_KIND_CALL`, the confirmation gate writes `CALL_KIND_DISCOVERY_CONFIRM`, with metadata rules identical to the non-interactive form above), after which `_await_terminal_or_web` waits **in parallel** for terminal input and the web response file; whichever responds first drives the flow, continuing to run **within the same live process** without needing `--resume`.

To avoid being re-spawned by the supervising daemon with a repeated `--resume`, during an interactive pause the flow SHALL remain `RUNNING` throughout and SHALL NOT be set to `PAUSED` (the only exception being Ctrl+C/EOF cancellation, which persists and exits to await resume). After each pause turn is resolved (terminal response / web response / cancellation), the call file and its `.response` sibling file SHALL be idempotently cleaned up via `_cleanup_discovery_call`, and the consumed web response file is deleted immediately, so the confirmation gate's empty-input redraw loop does not re-read an already-consumed response.

#### Scenario: Interactive discovery pause mirrored as a web-respondable call file
- **GIVEN** a discovery flow initiated interactively from the CLI is paused at a clarification Q&A or confirmation gate, and the project root is known
- **WHEN** the pause handler begins waiting for the user
- **THEN** the corresponding-kind `se3/calls/` call file is written via `_maybe_write_discovery_call`, so the web console displays the same pending interaction
- **AND** terminal input and the web response file are waited on in parallel; whichever responds first drives continuation within the same live process
- **AND** the flow remains `RUNNING` throughout and is not set to `PAUSED` (avoiding repeated daemon spawn)
- **AND** after that turn is resolved, the call file and its `.response` sibling file are idempotently cleaned up

#### Scenario: Friendly error prompt when Discovery LLM JSON extraction fails
- **GIVEN** the discovery step performs an LLM call using the two-phase JSON mode
- **WHEN** the LLM returns narrative text rather than valid JSON, causing an `LLMCallError` (the message contains "JSON extraction failed")
- **THEN** the discovery_handler catches the `LLMCallError` and presents a friendly error panel to the user (via `render_full`), explaining that the LLM failed to return valid JSON structured output
- **AND** the step returns `StepStatus.FAILED`, which is automatically handled by the flow engine's retry mechanism (up to 3 times)
- **AND** the raw traceback is not exposed to the user

#### Scenario: Friendly prompt for other Discovery LLM call errors
- **GIVEN** the discovery step performs an LLM call
- **WHEN** the LLM call fails for a reason other than JSON extraction, throwing an `LLMCallError`
- **THEN** the discovery_handler catches the error and displays a concise error description (containing the original error message)
- **AND** the step returns `StepStatus.FAILED`

#### Discovery Message Display Rendering

The `_display_discovery_message()` function SHALL render LLM-generated content fields (`content` and `refined_description`) as markdown using `rich.markdown.Markdown`, while structural UI elements (section titles, numbered question lists, confirmation prompts) use Rich `Text` with appropriate styling. Multiple renderables are combined via `rich.console.Group` and printed inside a reverse-color block titled `## Discovery` (white text on blue background) — produced via the shared helpers described in the **Block Rendering Visual Style** Requirement — followed by a blank line, the renderables, another blank line, and a matching short reverse-color blue footer block. This replaces the previous outer `Panel(title="Discovery", border_style="blue")` framing — no border/box is drawn to the terminal's left and right edges, while the original blue accent color and "Discovery" label are preserved as the reverse-color block title and footer.

**Narrative prefix from raw LLM output:**

When `raw_result_text` is provided (the raw LLM output from which JSON was extracted), `_display_discovery_message()` SHALL strip both (a) all fenced JSON code blocks and (b) any trailing bare JSON object that follows the last narrative line. JSON detection for both paths SHALL use the same lenient parse semantics as `parse_json_response` (full repair chain: `json.loads` → `_repair_json` → `_repair_unescaped_quotes` → combined), exposed via the shared helpers `looks_like_json` (fenced blocks; any JSON value type, including arrays and scalars) and `looks_like_json_object` (trailing bare JSON; dict-only, matching `parse_json_response`'s dict-only return contract). Strict `json.loads` MUST NOT be used for this detection: LLM responses commonly contain unescaped ASCII double quotes inside JSON string values (or analogous quirks) that the repair chain handles but strict parsing rejects — without this alignment, the same fenced block can be successfully extracted as `content` while simultaneously leaking back into the narrative prefix, causing the same data to render twice under the Discovery heading (once formatted, once as a raw fenced block). If the remaining narrative text is non-empty after stripping whitespace, it SHALL be rendered as `rich.markdown.Markdown` and placed before all other renderables in the Group printed under the heading, separated by a blank line. If the remaining text is empty (e.g., Phase 2 pure-JSON output), the rendering is identical to the no-narrative case, with no additional prefix. This rule applies uniformly across all five rendering modes.

**Rendering rules by mode:**

| Mode | `content` field | `refined_description` field | Structural elements |
|------|----------------|---------------------------|---------------------|
| Confirmation (`is_confirmation=True`) | Markdown | Markdown, wrapped in a nested cyan reverse-color block (see Proposed Task Description block) | Confirmation prompt hint as styled `Text` (see Confirmation phase content display) |
| Synthesis + questions | Markdown | Markdown, wrapped in a nested cyan reverse-color block (see Proposed Task Description block) | Numbered questions as `Text` |
| Synthesis (no questions) | Markdown | Markdown (under "Proposed Task Description:" heading) | Heading as styled `Text`, confirmation prompt as styled `Text` |
| Question | Markdown | — | Numbered questions as `Text` |
| General | Markdown | — | — |

**Proposed Task Description block:**

In the **Confirmation** (`is_confirmation=True`) and **Synthesis + questions** modes, the LLM-produced `refined_description` SHALL NOT be appended as bare Markdown (nor introduced by a single plain `Proposed Task Description:` heading line). Instead it SHALL be wrapped in a *nested* se3 reverse-color block that gives the user a framework-rendered, unambiguous start/end boundary for the proposed description. This block is produced by a shared module-private helper (`_proposed_description_block`) so both modes render byte-identically. The block reuses the same reverse-color primitives that back `render_block_header` / `render_block_footer` — `_reverse_title` (a reverse-color title row) and `_reverse_footer` (a fixed-width, whitespace-only reverse-color footer block) from the **Block Rendering Visual Style** Requirement — but is constructed as embeddable Rich renderables rather than direct `console.print` calls, so it can be placed inside the single `Group` printed under the `## Discovery` heading. The block uses the `cyan` accent color, distinct from the outer blue Discovery block, so it is visually layered apart from the surrounding LLM `content` text and (in Synthesis + questions mode) the trailing yellow Questions section. The renderable sequence is: a cyan `_reverse_title` row (e.g. `Proposed Task Description / Final Task Description`), a blank line, the `refined_description` rendered via `rich.markdown.Markdown`, a blank line, a cyan `_reverse_footer` block, and a trailing blank line. The block is purely a framework-level visual container: it does not modify the LLM-produced text, and the footer block contains only whitespace characters styled with a background color, so copying it to the clipboard yields only blank characters with no visible border glyphs.

**Confirmation phase content display:**

When the discovery step enters the confirmation phase (`is_confirmation=True`), `_display_discovery_message()` SHALL display the full LLM analysis content (`content` field) followed by the `refined_description`, both rendered as markdown. This ensures users see the complete LLM analysis (reasoning, summaries, context) alongside the final proposed description before making their confirmation decision. The confirmation rendering SHALL include a styled prompt hint at the bottom of the Group printed under the `## Discovery` heading, outside the markdown content area, rendered as Rich `Text` (not markdown). This hint is the only mechanism by which the user learns the `1` affordance — without it, the rendering spec is silent on input expectations. **Non-normative:** The exact hint wording is non-normative; only the `1` affordance is normative. Implementations MAY localize or reword the hint freely (e.g., `Enter 1 to confirm and continue, enter other content to continue exploring; press Enter directly to redisplay this hint`), provided the `1` confirmation key is communicated to the user and the empty-input no-op behavior is preserved.

##### Scenario: Discovery message renders LLM content as markdown
- **GIVEN** the discovery step receives LLM response with `content` and `refined_description` fields containing markdown syntax (headings, lists, bold, etc.)
- **WHEN** `_display_discovery_message()` renders the message
- **THEN** `content` and `refined_description` are rendered via `rich.markdown.Markdown`
- **AND** structural elements (titles, numbered questions, confirmation prompts) use Rich `Text` with styling
- **AND** all renderables are combined via `rich.console.Group` and printed inside a reverse-color block titled `## Discovery` (white on blue) with a matching short reverse-color blue footer block closing the section; no outer `Panel` border, `Rule`, or horizontal-line element is drawn (see **Block Rendering Visual Style** Requirement)

##### Scenario: Confirmation phase shows full LLM analysis content
- **GIVEN** LLM enters confirmation mode with both `content` (analysis text) and `refined_description`
- **WHEN** the confirmation display is rendered
- **THEN** the full `content` from the LLM response is displayed as markdown
- **AND** the `refined_description` is displayed below it as markdown, wrapped in the nested cyan reverse-color block (see Proposed Task Description block)
- **AND** a styled prompt hint is rendered at the bottom of the Group (under the `## Discovery` heading) as Rich `Text` (not markdown), communicating the `1` confirmation affordance
- **AND** the user can review the complete analysis before choosing to confirm or continue exploration

##### Scenario: Refined description wrapped in nested cyan boundary block
- **GIVEN** the discovery step renders a message in either Confirmation (`is_confirmation=True`) mode or Synthesis + questions mode, with a non-empty `refined_description`
- **WHEN** `_display_discovery_message()` renders the message
- **THEN** the `refined_description` is wrapped in a nested se3 reverse-color block produced by the shared `_proposed_description_block` helper: a cyan `_reverse_title` row, a blank line, the `refined_description` as `rich.markdown.Markdown`, a blank line, a cyan `_reverse_footer` block, and a trailing blank line
- **AND** the nested block uses the `cyan` accent color, visually distinct from the outer blue Discovery block
- **AND** the user can determine the complete start/end extent of the `refined_description` solely from the se3-rendered reverse-color title and footer, without relying on dashed lines or any "Final Task Description" wording that may incidentally appear inside the LLM text
- **AND** the `_reverse_footer` block contains only whitespace characters, so copying it yields no visible border glyphs
- **AND** both modes render the block identically, and no single plain `Proposed Task Description:` heading line is emitted

##### Scenario: Narrative text from raw LLM output prefixed to Discovery rendering
- **GIVEN** `last_raw_result` contains narrative text outside any JSON code block (e.g., Phase 1 LLM output with analysis followed by a fenced JSON object)
- **WHEN** `_display_discovery_message()` renders the message with `raw_result_text` provided
- **THEN** the narrative text (after stripping all JSON code blocks) is rendered as `Markdown` and appears as the first renderable in the Group printed under the `## Discovery` heading
- **AND** a blank line separates the narrative prefix from the existing renderables (`content`, `refined_description`, structural elements)
- **AND** all subsequent renderables follow their existing mode-specific rules unchanged

##### Scenario: Pure JSON raw output renders unchanged
- **GIVEN** `last_raw_result` is pure JSON (e.g., Phase 2 output) or empty, so stripping JSON code blocks leaves no remaining text
- **WHEN** `_display_discovery_message()` renders the message with `raw_result_text` provided
- **THEN** the rendering under the `## Discovery` heading is identical to the behavior before this change, with no additional prefix renderable
- **AND** the `content` and other fields follow their existing mode-specific rules

##### Scenario: Fenced JSON with unescaped quotes is stripped (no duplicate display)
- **GIVEN** `last_raw_result` contains a fenced JSON block whose string values include unescaped ASCII double quotes (e.g., `content` referencing a phrase like `"whether to rewrite..."`) — text that strict `json.loads` rejects but the `parse_json_response` repair chain successfully recovers as a dict
- **WHEN** `_display_discovery_message()` renders the message with `raw_result_text` provided
- **THEN** narrative extraction recognizes the fenced block as JSON via the same lenient parse helpers (`looks_like_json` / `looks_like_json_object`) used upstream, and strips it from the narrative prefix
- **AND** the rendering under `## Discovery` shows only the formatted `content` once — the raw fenced block does NOT appear alongside it
- **AND** the same alignment applies to a trailing bare JSON object after the last narrative line

### Requirement: State-Machine-Driven Flow

The flow engine SHALL be implemented as a Python finite state machine, where each state corresponds to a flow step. Transitions between steps are controlled by program logic rather than decided by the LLM.

**Core principles:**
1. Step transitions are programmatic
2. The LLM only handles the work inside a step (thinking, generation, analysis)
3. The LLM's output does not change the step transition logic

**Flow Lifecycle API:**

The `StateMachine` SHALL expose the following public lifecycle API for orchestrators:

1. `create_flow(task_description, task_type)` — Create a new `FlowInstance` (or load an existing one for resume)
2. `init_flow(flow)` — Initialize flow metadata and baseline commit. Writes `_meta.json` (containing `se3_version`, `python_version`, `created_at`) to the session history directory and records the current git HEAD as `baseline_commit` on the flow instance for change detection during the commit step. Both operations are idempotent: if `_meta.json` already exists or `baseline_commit` is already set, they are skipped — making `init_flow` safe for both new and resumed flows.
3. `run_step(flow, step)` — Execute a single step
4. `transition_to_next(flow)` — Advance to the next step

The CLI orchestrator (`_run_flow_impl`) calls these methods in sequence: `create_flow()` → `init_flow()` → while loop of `run_step()`/`transition_to_next()`.

#### Scenario: Normal flow execution
- **WHEN** the user runs `se3 run` and provides a task description
- **THEN** the flow engine starts from the `init` state
- **AND** calls `init_flow()` to write `_meta.json` and record the baseline commit
- **AND** proceeds through subsequent steps in order according to the programmatically defined transition rules
- **AND** within each step invokes the LLM to handle that step's specific work

#### Scenario: init_flow idempotent on resume
- **WHEN** a flow is resumed via `se3 run --resume`
- **AND** `init_flow()` is called on the loaded flow instance
- **THEN** `_meta.json` is not overwritten (file already exists guard)
- **AND** `baseline_commit` is not overwritten (already-set guard)

#### Scenario: Dynamic step pool selection
- **WHEN** the flow engine completes the `analyze` step
- **THEN** it selects the subsequent required steps from a fixed step pool based on the analysis results
- **AND** the step pool is a predefined finite set, not generated out of thin air by the LLM

#### Scenario: Completion advances the step index to total so progress reads total/total
- **GIVEN** a flow whose `selected_steps` has `N` entries and whose last step has just finished, so `transition_to_next` is about to set the flow's status to `FlowStatus.COMPLETED`
- **WHEN** the completion branch of `transition_to_next` runs (and likewise the `run.py` fallback completion path)
- **THEN** `state.current_step_index` is advanced to `len(selected_steps)` (== `N`, one past the last step) and the flow is persisted
- **AND** a downstream consumer that derives progress from engine state — notably the daemon aggregator's `current_step_index` / `total_steps` and `progress` computation in `_snapshot_for_root` — reports `N/N` and `progress == 1.0` at completion, not `N-1/N`
- **AND** the fix is applied in the engine, not as a frontend special case, so the daemon-reported `progress` and every other reader of engine state observe the same completed-steps / total-steps semantics
- **AND** the out-of-range index is safe on resume because the engine self-heals it from `current_step_id` via `selected_steps.index(...)`

### Requirement: 16-Step Flow Pool

The flow engine SHALL define a fixed pool of step types (the StepType enum), and all flow steps are selected from this pool. The pool grew to 17 entries with the addition of the `spec_gate` step (mechanism A — the post-`update_spec` verification gate; see *Post-update_spec Spec Verification Gate*); it consists of 6 active steps + CONFIRM + DISCOVERY + 4 deprecated steps + others. The table below lists the main steps; for the backward-compatible behavior of deprecated steps, see the *Deprecated Step Type Backward Compatibility* requirement.

| Step | Responsibility | LLM Involvement | JSON Mode | Read-Only | Input | Output |
|------|------|---------|-----------|-----------|------|------|
| `discovery` | Requirement exploration (multi-turn dialogue) | Yes | STRICT | **Yes** | initial_description | refined_description, discovery_summary, requirements_clarified |
| `analyze` | Analyze task type and scope; gather project context; select and load relevant spec items | Yes | STRICT | **Yes** | task_description | task_type, scope, complexity, reasoning, project_summary, relevant_specs, spec_content |
| `plan` | Unified planning: proposal + design + task decomposition (adaptive depth based on task_type) | Yes | TWO_PHASE | **Yes** | spec_content, task_description, task_type, scope, project_summary | plan{proposal,design}, task_groups, spec_changes, total_complexity, estimated_effort |
| `implement` | Write the code implementation | Yes | TWO_PHASE | No | design_doc, task_groups | implemented_groups, files_changed, total_groups |
| `test` | Run tests for validation | No (program execution) | - | No | - | test_results, tests_passed |
| `self_check` | LLM code review: logic completeness, code robustness, functional omissions, test-uncovered areas (does not check spec compliance) | Yes | TWO_PHASE | **Yes** | test_results, changes_made, spec_content, task_groups, fix_iteration, self_check_pass_index, self_check_passes_required, self_check_convergence_enabled, prev_self_check_issues (conditional) | self_check_result, issues (structured list with description, severity, location), actionable_count |
| `verify_spec` | Check the implementation's consistency with the spec | Yes | EXTRACT | **Yes** | changes_made, spec_content, test_results, fix_iteration, spec_changes | verification_result, issues, in_scope_count, out_of_scope_count, fix_needed, fix_instructions, fix_context, **verified** (rule-based, computed by code: `(in_scope_count == 0) and tests_passed` — see *verify_spec Unified Priority and Scope Mechanism*) |
| `update_spec` | Update the spec to record changes | Yes | EXTRACT | No | changes_made, verification_result, spec_changes, design_doc, selected_items | updated_specs, new_capabilities, spec_decisions, notes |
| `spec_gate` | **Mechanism A**: post-`update_spec` gate — programmatically validate each edited/new spec, then re-run the full test suite (see *Post-update_spec Spec Verification Gate*) | No (program execution) | - | No | changes_made, baseline_failures, spec_requirement_baseline | gate_passed, gate_route, gate_skipped, fix_needed, fix_instructions, fix_context, test_results |
| `version_analyze` | Analyze changes to determine suggested_version (authoritative) + generate commit message | Yes | EXTRACT | **Yes** | changes_made, summary, verification_result, task_type | **suggested_version** (authoritative), bump_type, confidence, reasoning, commit_message |
| `commit` | Commit changes | No (program execution) | - | No | changes_made, bump_type, commit_message, proposal, updated_specs | commit_hash |
| `summarize` | Generate a summary and handoff | Yes | Text | **Yes** | all_previous_outputs | summary (Markdown text) |
| ~~`project_summary`~~ | ~~Generate a project context summary~~ (deprecated — merged into analyze) | Yes | Text | **Yes** | project state | summary string |

**Step sequences for different task types:**
- `discovery`: discovery → analyze → plan → implement → test → **self_check** → verify_spec → update_spec → **spec_gate** → **version_analyze** → commit → **summarize**
- `feature`: analyze → plan → implement → test → **self_check** → verify_spec → update_spec → **spec_gate** → **version_analyze** → commit → **summarize**
- `bugfix`: analyze → plan → implement → test → **self_check** → verify_spec → **version_analyze** → commit → **summarize**
- `review`: analyze → verify_spec → **summarize**
- `small`: analyze → implement → test → **version_analyze** → commit → **summarize**
- `directive`: analyze → plan → implement → **version_analyze** → commit → **summarize**

**Note:** The `summarize` step is the final step of every default task-type sequence (it is appended after `commit`, or after `verify_spec` for the `review` type). It remains available in the step pool and is produced by `get_default_step_sequence`. Because `apply_step_config` deduplicates appended steps by step value, an existing `steps.append: [summarize]` configuration becomes a silent no-op (it neither errors nor warns, and does not add a second `summarize`). The `commit` step retains its template-summary fallback (`se3/state/summary-{flow_id}.md`, built from structured flow state) for the rare case where `summarize` is removed from the sequence; on the default path `summarize` runs and supersedes that template.

#### Scenario: Feature Task Full Flow
- **WHEN** the task type is `feature`
- **THEN** execute the full 12-step flow (plan uses full depth), including the self_check step, the `spec_gate` step inserted between `update_spec` and `version_analyze`, and the `summarize` step appended after `commit`

#### Scenario: Small Task Simplified Flow
- **WHEN** the task type is `small`
- **THEN** skip the plan and self_check steps

#### Scenario: SELF_CHECK Code Review Passes
- **WHEN** the self_check step completes the LLM code review
- **AND** no omissions of any severity are found (the issues list is empty)
- **AND** since entering self_check in the current fix-loop, `workflow.self_check_passes_required` (default 1) consecutive fully clean instances have accumulated
- **THEN** the flow advances to the verify_spec step
- **NOTE** when N>1 and the accumulated clean count has not yet reached N, the state machine does not advance `current_step_index`, but instead creates the next self_check Step instance to continue execution (see the "Repeat N times until all clean" scenario)

#### Scenario: SELF_CHECK Finds Omissions and Triggers fix loop
- **WHEN** the self_check step completes the LLM code review
- **AND** an omission of any severity (critical/high/medium/low) is found
- **THEN** self_check returns REVISION_NEEDED
- **AND** accompanied by fix_context (the omission list) and fix_instructions
- **AND** triggers the existing fix loop mechanism to return to the IMPLEMENT step
- **AND** the remaining self_check instances of the current round (if pass_index < N) are not created, and the current fix-loop immediately transitions into fixing
- **AND** after fixing, rerun TEST → SELF_CHECK until the omission list is empty or the max_fix_iterations limit is reached
- **NOTE** fix_iterations is a global counter shared by TEST, SELF_CHECK, and VERIFY_SPEC; the total loop count does not exceed max_fix_iterations (default 100; configuring it to `0` or `null` means unlimited, skipping the limit check)
- **NOTE** the self_check handler always returns REVISION_NEEDED (it does not judge exhaustion within the handler); exhaustion detection is handled uniformly by state_machine.transition_to_next()
- **NOTE** when the fix loop is exhausted, the state_machine sets the flow status to FAILED and stops execution, and at the same time generates an issue via A-class issue discovery

#### Scenario: SELF_CHECK Repeats N Times Until All Clean
- **GIVEN** `workflow.self_check_passes_required` is configured as N (N>=1, default 1)
- **WHEN** a fix-loop round enters the self_check step
- **THEN** the state_machine creates self_check Step instance #1, with inputs injecting `self_check_pass_index=1` and `self_check_passes_required=N`
- **AND** when instance #1 completes and the issues list is empty, if N>1 then the state_machine detects in transition_to_next that the "number of consecutive COMPLETED self_check instances < N" and creates a new self_check Step instance #2 (pass_index=2), and `current_step_index` does not advance
- **AND** this process repeats until N consecutive fully clean rounds have accumulated, only then advancing to verify_spec
- **AND** the N self_check instances appear as N independent step instances in the step history, the `se3 history` output, and the logs (log prefixes `#1/N`, `#2/N`, …)
- **AND** no comparison is made between the N self_check runs within the same round (within a single round there is only one state quantity, "accumulated consecutive clean count", and no convergence or issues comparison logic is introduced)

#### Scenario: SELF_CHECK Any Run Reporting issues Short-Circuits to Trigger fix-loop
- **GIVEN** N>1, and self_check instance #i (1 <= i <= N) is executing
- **WHEN** instance #i reports issues of any severity
- **THEN** instance #i immediately returns REVISION_NEEDED and triggers the fix-loop (the flow jumps back to IMPLEMENT)
- **AND** instances #(i+1)..#N are not created, and only the i instances actually run are recorded in the step history
- **AND** when the next fix-loop round re-enters self_check, the pass_index count restarts from 1 (it does not inherit the previous round's pass_index)

#### Scenario: SELF_CHECK Convergence Detection (off by default, cross-fix-loop-round only)
- **GIVEN** `workflow.self_check_convergence_enabled` defaults to `false`
- **WHEN** a self_check instance completes the LLM code review and finds issues
- **THEN** under the default configuration, the state_machine does not call `_issues_converged`, and the handler directly returns REVISION_NEEDED to enter the fix-loop
- **AND** even if this round's issues are exactly identical to the issues of the last fix-loop round's final self_check, they will not be short-circuited to COMPLETED
- **WHEN** the user explicitly sets `workflow.self_check_convergence_enabled: true` in `se3.yaml`
- **THEN** convergence detection applies only between "the first self_check instance of the current fix-loop round (pass_index=1)" and "the issues of the last self_check instance of the previous fix-loop round"
- **AND** instances #2..#N within the same round do not participate in the convergence comparison (`prev_self_check_issues` is injected only for pass_index=1, and is forced empty for the other instances)
- **AND** when the convergence determination is True, self_check directly returns COMPLETED and breaks out of the fix-loop (treated as reaching a stable point, equivalent to a single "artificial clean")
- **NOTE** the semantic change of defaulting to off is a behavioral default-value change; this spec revision records it explicitly and provides no additional notice in the changelog or startup log

#### Scenario: SELF_CHECK prompt injects plan task_groups as scope reference
- **WHEN** the `self_check` step builds its LLM prompt
- **AND** `step.inputs["task_groups"]` is a non-empty list (forwarded by state_machine from the plan step)
- **THEN** the prompt includes a `## Plan Task Groups (Scope Reference)` section summarizing each group's tasks and acceptance criteria
- **AND** the section body is head-truncated at `SELF_CHECK_TASK_GROUPS_MAX_CHARS` (default 2000) to bound prompt size
- **AND** the section wording explicitly states that task_groups is a **scope reference, NOT a strict specification**, that reasonable deviations (logic correct, functionality covered, quality acceptable) do NOT count as issues, and that self_check MUST NOT flag missing-plan-compliance as an issue — this is self_check, not a plan-conformance audit
- **AND** the LLM is instructed to use task_groups to cross-check the **Functional Gaps** review dimension (verifying each planned task's deliverables appear in the implementation), weighing it together with the original Task Description
- **WHEN** `task_groups` is absent, `None`, empty, or not a list (e.g., `small` / `bugfix` flows without a plan step)
- **THEN** the entire `## Plan Task Groups` section is omitted from the prompt — no orphan heading, no placeholder
- **AND** self_check still runs with its remaining inputs (task_description, changes_made, test_results, spec_content, fix_context)
- **NOTE** task_groups is intentionally the ONLY plan artifact injected into self_check — `proposal` is redundant with `design`, full `design` is withheld to preserve the verify_spec / self_check responsibility boundary, and neither is added
- **NOTE** `SELF_CHECK_TASK_GROUPS_MAX_CHARS` lives in `se3/engine/truncation.py` alongside the other shared self_check truncation constants

#### Scenario: SELF_CHECK prompt excludes decisions owned by a downstream specialized step
- **WHEN** the `self_check` step builds its LLM prompt
- **THEN** the `## What NOT to check` section includes an exclusion stating the general principle that, as a fix-loop checker, self_check MUST NOT report concerns that a later specialized step decides — because `implement` cannot resolve them within its own responsibility, the fix-loop reaches a standoff and spins (re-reporting and re-deferring the same concern without convergence)
- **AND** the version number and version files (e.g. the `version` field in `pyproject.toml`) — whether and how to bump them — are named as the concrete example of such a downstream-owned decision, which the downstream `version_analyze` step decides against the pre-session baseline
- **AND** self_check MUST NOT report "version not bumped" or "version number is wrong" as an issue
- **NOTE** the exclusion is deliberately phrased as the general "downstream specialized step owns it" principle (with version as the named example) so it generalizes beyond `pyproject.toml` to other version files and to other future downstream-owned concerns, rather than matching one file type
- **NOTE** the fix is applied on the checker side (self_check) so it covers all task sources — the task may originate from `discovery` or directly from user input; the discovery prompt and `verify_spec` are intentionally left unchanged, and no convergence brake is introduced (`self_check_convergence_enabled` remains default-off — see *SELF_CHECK Convergence Detection* scenario), because an unresolved issue staying unresolved is the desired behavior; the root-cause fix is removing the over-reach at its source, not capping the loop

### Requirement: CONFIRM Step (Dynamically Inserted Review Gate)

The step pool SHALL include a `CONFIRM` step type that does not appear in any default task-type step sequence and is instead inserted dynamically after configured step types, acting as a review gate that can approve the reviewed step (flow continues) or request revision (flow goes back to the reviewed step).

**Step pool attributes:**

| Step | uses_llm | Read-Only | Inputs | Outputs |
|------|----------|-----------|--------|---------|
| `confirm` | conditional (LLM reviewer only) | **No** | step_to_review_id, step_to_review_type, reviewer, agents (LLM mode only), max_iterations, task_description, _llm_review_iteration (degenerate fallback only; the iteration cap source is the persisted cross-revision counter `flow.state.review_iterations[step_to_review_id]`) | review_result {approved, feedback, step_to_review_id, step_to_review_type, reviewer?}, revision_feedback, call_file (human mode) |

The CONFIRM step's `read_only` attribute is `False` — the step itself produces no source-file edits, but it is deliberately not marked read-only because (a) on the revision path it triggers re-execution of a non-read-only step and (b) the human reviewer path writes call-file artifacts to `se3/calls/`.

**Configuration-driven insertion:**

CONFIRM steps are inserted via `insert_confirmation_steps(steps, project_root)` (in `se3/config.py`), invoked by `StateMachine._insert_confirmation_steps()` immediately after the default step sequence for a task type is selected. A CONFIRM step is appended after each step type `S` if and only if:

1. `S` appears in the selected step sequence, AND
2. `S` is a key in the merged `confirmation.steps` dict (global `~/.se3/config.yaml` + project `se3.yaml`).

There is no global on/off switch — opting a step out of confirmation simply means omitting it from `confirmation.steps`. Each `confirmation.steps.<step>` entry has the schema:

```yaml
confirmation:
  steps:
    plan:
      reviewer: human          # 'human', null (use llm_caller.defaults), or an agent name
      max_iterations: 3        # LLM-reviewer iteration cap; ignored on human path
```

**Reviewer modes:**

The `reviewer` field, resolved by `resolve_confirm_inputs(project_root, reviewed_type)` and injected into `step.inputs["reviewer"]` by `state_machine._build_step_inputs`, dispatches to one of two paths:

1. **Human reviewer** (`reviewer == "human"`):
   - `confirm_handler` writes a JSON call file to `se3/calls/confirm_{step_id}_{timestamp}.json` containing the step being reviewed, the change/flow id, and the call type.
   - Returns `StepStatus.PAUSED` so the run loop can prompt the user interactively or via an out-of-band response file.
   - On resume, the handler scans `se3/calls/` for the matching call file and reads its sibling `.response` file (`{stem}.response` JSON with `approved` and `feedback` fields). If the response is present, the handler returns `COMPLETED` (approved) or `REVISION_NEEDED` (changes requested) without re-creating the call file. `max_iterations` is not consulted on this path.

2. **LLM reviewer** (`reviewer != "human"` — either an explicit agent name or `null` to fall back to `llm_caller.defaults`):
   - `confirm_handler._llm_review()` builds a review prompt via `build_llm_review_prompt(reviewed_type, step_output, task_description, revision_feedback, project_root)` and dispatches it through `LLMCaller` with `step_type="confirm_llm_review"` and `json_mode="two_phase"`.
   - The LLM response is parsed via `parse_json_response(response, required_keys=["approved"])`; the handler reads `approved` (bool) and `feedback` (string).
   - The handler reads the persisted cross-revision counter `flow.state.review_iterations[step_to_review_id]` (incremented once per revision by `_transition_to_revision`) and auto-approves with feedback `Auto-approved: max review iterations (N) reached.` when that counter reaches `max_iterations` (default `3`), preventing infinite review loops. Because each revision creates a brand-new CONFIRM step, the persisted counter — not a per-confirm `step.inputs["_llm_review_iteration"]`, which would reset to 0 every cycle — is what makes the cap bound the review↔revise loop across the new-confirm chain. `step.inputs["_llm_review_iteration"]` is retained only as a degenerate fallback when there is no reviewed step (`step_to_review_id` is `None`).
   - If the LLM call itself raises, the handler auto-approves with feedback prefixed `Auto-approved due to LLM call failure:` rather than blocking the flow. Malformed JSON responses are treated as `approved == False` with a parse-failure feedback message.
   - The synchronous path never returns `PAUSED` and never writes a call file.

**State machine routing:**

`StateMachine.transition_to_next()` inspects `current_step.outputs["review_result"]` after a CONFIRM step completes:

- `approved == True`: normal forward progression to the next step in the selected sequence.
- `approved == False`: the state machine calls `_transition_to_revision(flow, confirm_step, step_to_review_id)`, which re-executes the originally reviewed step with the prior output and revision feedback available to it.

**Step-to-review resolution:**

When `_build_step_inputs` constructs inputs for a CONFIRM step, it scans `flow.state.step_history` in reverse for the most recent non-CONFIRM step that has not yet been confirmed. A step counts as "already confirmed" only when some CONFIRM in history both reviewed it (`review_result.step_to_review_id` matches the step's id) **and** approved it (strict `review_result.approved is True`). A CONFIRM that requested changes (`approved` is `False` or absent) does NOT mark its target as confirmed. Because each revision creates a brand-new CONFIRM step, this rule ensures that after a rejected step is re-executed, the next CONFIRM re-selects that same step and re-reviews it (with its configured reviewer), rather than treating it as done and skipping forward to a later — possibly unconfigured — step (which would resolve to `None` and trip the human defensive fallback below).

**Defensive fallback:**

If a CONFIRM step is present in the sequence but `resolve_confirm_inputs` returns `None` for the reviewed step type (indicating YAML drift between when `insert_confirmation_steps` ran and when `_build_step_inputs` runs — e.g., the user edited `se3.yaml` mid-flow), the state machine logs a warning and defaults to `reviewer = "human"` with `max_iterations = None`, so the user can manually unblock the flow.

#### Scenario: CONFIRM step inserted only when configured
- **GIVEN** `confirmation.steps` contains an entry for `plan` but not for `implement`
- **WHEN** the state machine builds the step sequence for a `feature` task
- **THEN** a CONFIRM step is appended immediately after `plan` in the sequence
- **AND** no CONFIRM step is appended after `implement`

#### Scenario: CONFIRM step absent from defaults when nothing is configured
- **GIVEN** `se3.yaml` has no `confirmation.steps` section (or it is empty)
- **WHEN** the state machine builds the step sequence
- **THEN** no CONFIRM step is inserted anywhere in the sequence
- **AND** the sequence matches the default task-type sequences defined under "16-Step Flow Pool"

#### Scenario: Human reviewer pauses awaiting response
- **WHEN** `confirm_handler` runs with `step.inputs["reviewer"] == "human"` and no prior response file exists
- **THEN** a JSON call file is written to `se3/calls/confirm_{step_id}_{timestamp}.json`
- **AND** the call-file path is stored in `step.outputs["call_file"]`
- **AND** the handler returns `StepStatus.PAUSED`

#### Scenario: Human reviewer resume reads sibling response file
- **GIVEN** a CONFIRM step previously returned `PAUSED` and wrote `se3/calls/confirm_{step_id}_{ts}.json`
- **AND** a sibling `se3/calls/confirm_{step_id}_{ts}.response` file exists with `{"approved": true, "feedback": "..."}`
- **WHEN** the flow resumes and re-runs `confirm_handler` for the same step
- **THEN** the handler reads the response file, populates `step.outputs["review_result"]` with `approved`, `feedback`, `step_to_review_id`, and `step_to_review_type`
- **AND** returns `StepStatus.COMPLETED` (or `REVISION_NEEDED` when `approved == false`)

#### Scenario: LLM reviewer approval
- **WHEN** `confirm_handler` runs with `step.inputs["reviewer"] != "human"` (an agent name or `None`)
- **AND** the LLM responds with parseable JSON `{"approved": true, "feedback": "..."}`
- **THEN** the handler returns `StepStatus.COMPLETED`
- **AND** `step.outputs["review_result"]` includes `reviewer: "llm"`, `approved: true`, and the LLM's feedback
- **AND** no call file is created

#### Scenario: LLM reviewer revision request
- **WHEN** the LLM responds with `{"approved": false, "feedback": "..."}`
- **THEN** the handler returns `StepStatus.REVISION_NEEDED`
- **AND** the state machine's `transition_to_next()` invokes `_transition_to_revision()` to re-execute the originally reviewed step

#### Scenario: LLM reviewer max iterations auto-approval
- **GIVEN** `confirmation.steps.<step>.max_iterations` is configured to N (default 3)
- **WHEN** the persisted counter `flow.state.review_iterations[step_to_review_id]` has reached N before the LLM call
- **THEN** the handler skips the LLM call, returns `StepStatus.COMPLETED`, and stores feedback `Auto-approved: max review iterations (N) reached.`

#### Scenario: LLM reviewer call failure does not block flow
- **WHEN** the LLM call inside `_llm_review` raises any exception
- **THEN** the handler returns `StepStatus.COMPLETED` with `approved: true` and feedback prefixed `Auto-approved due to LLM call failure:`
- **AND** the flow continues forward rather than stalling on a broken reviewer

#### Scenario: Revision routes back to the reviewed step
- **GIVEN** a CONFIRM step completes with `approved == false` and `review_result.step_to_review_id == X`
- **WHEN** `transition_to_next()` runs
- **THEN** `_transition_to_revision(flow, confirm_step, X)` is invoked
- **AND** the step identified by X is re-executed with revision feedback available
- **AND** the state machine does NOT advance to the step that would normally follow CONFIRM in the selected sequence

#### Scenario: Revision-requested CONFIRM does not mark its reviewed step as confirmed
- **GIVEN** a CONFIRM reviewed step `plan`, returned `approved == false` (revision requested), and `plan` was subsequently re-executed
- **WHEN** `_build_step_inputs` resolves the step-to-review for the next CONFIRM
- **THEN** `plan` is selected again because the rejected CONFIRM does not shield it (only `approved is True` counts as confirmed)
- **AND** the new CONFIRM re-reviews `plan` with its configured reviewer
- **AND** the resolution does NOT skip forward to a later unconfigured step (e.g. `analyze`) and therefore does NOT trigger the human defensive fallback

#### Scenario: Defensive fallback when CONFIRM config is missing at build-input time
- **GIVEN** the sequence contains a CONFIRM step (because `confirmation.steps.<reviewed_type>` existed when `insert_confirmation_steps` ran)
- **AND** `resolve_confirm_inputs(project_root, reviewed_type)` now returns `None` (e.g., `se3.yaml` was edited mid-flow)
- **WHEN** `_build_step_inputs` constructs inputs for the CONFIRM step
- **THEN** `step.inputs["reviewer"]` is set to `"human"` and `step.inputs["max_iterations"]` is `None`
- **AND** a warning is logged identifying the reviewed step type

### Requirement: Deprecated Step Type Backward Compatibility

The step type enum SHALL retain deprecated values with stub handlers that forward to the appropriate current handler. This ensures persisted flows created before step unification/merges can resume without crashing.

**Retained entries (plan unification):**
- `StepTypeValue.PROPOSE` — deprecated, forwards to plan_handler
- `StepTypeValue.DESIGN` — deprecated, forwards to plan_handler
- `StepTypeValue.PLAN_TASKS` — deprecated, forwards to plan_handler

**Retained entries (analyze merge):**
- `StepTypeValue.PROJECT_SUMMARY` — deprecated, forwards to project_summary_handler

**Behavior:**
- Stub handlers log a deprecation warning with the flow ID and step ID
- The target handler executes normally regardless of which step type triggered it
- Display titles and renderers for deprecated types are retained so history/status views render correctly

#### Scenario: Resuming a persisted flow with old step types
- **WHEN** a flow persisted with `PROPOSE`, `DESIGN`, or `PLAN_TASKS` step types is resumed
- **THEN** the stub handler forwards execution to plan_handler
- **AND** a deprecation warning is logged

#### Scenario: Resuming a persisted flow with PROJECT_SUMMARY steps
- **WHEN** a flow persisted with `PROJECT_SUMMARY` step type is resumed
- **THEN** the stub handler forwards execution to project_summary_handler
- **AND** a deprecation warning is logged

#### Scenario: New flows use unified PLAN step
- **WHEN** a new flow is created
- **THEN** the step sequence contains only `PLAN`, never `PROPOSE`, `DESIGN`, or `PLAN_TASKS`

#### Scenario: New flows do not include PROJECT_SUMMARY
- **WHEN** a new flow is created
- **THEN** the step sequence does not contain `PROJECT_SUMMARY`
- **AND** its functionality is provided by the `ANALYZE` step

### Requirement: In-step LLM invocation

The flow engine SHALL invoke the LLM (`claude -p`) via subprocess within each step, passing in the step-specific prompt and the automatically collected context.

**LLM invocation mechanism:**
1. Build the step-specific prompt
2. Automatically collect relevant context (specs, prior step outputs, project state)
3. Invoke the Claude CLI to obtain the response
4. Parse the response (supports both JSON and text)
5. Store the output to the step state

**Large Prompt Routing via stdin:**

The CLI adapter (`ClaudeCodeRunner._resolve_args()`) SHALL automatically reroute oversized prompt arguments through the spawned Claude subprocess's standard input when their UTF-8 byte length exceeds 100 KB (102,400 bytes), preventing `execve()` `E2BIG` errors caused by Linux's `MAX_ARG_STRLEN` limit (128 KB). No temporary file is written.

- **Threshold:** 100 KB (102,400 bytes). This leaves ~28 KB safety margin below the 128 KB `MAX_ARG_STRLEN` hard limit, covering multi-byte UTF-8 encoding and environment variable space.
- **Mechanism:** When `-p`/`--prompt` is followed by a plain-text argument (not an `@file` reference) whose `len(prompt_arg.encode('utf-8'))` exceeds the threshold, `_resolve_args()` keeps the `-p`/`--prompt` flag in argv but drops its value, and returns the dropped value as a separate `stdin_prompt` string alongside the resolved argv. The execution paths (`run()`, `popen()`, `run_with_monitor()`) feed `stdin_prompt` to the Claude subprocess via stdin so Claude treats it as the user message.
- **Why not a temp file:** An earlier `-p @tmpfile` design caused Claude Code to read the file via its Read tool (subject to that tool's 25k-token ceiling) rather than treating the contents as the user message. Routing through stdin delivers the full prompt as the user message regardless of size.
- **Below threshold:** The prompt argument is passed directly on the command line (existing behavior); `stdin_prompt` is `None`.
- **`@file` passthrough:** Arguments starting with `@` and explicit `-p @file` forms are left unchanged — that is Claude CLI's documented file-reference syntax and callers using it have asked for that semantic.
- **Multiple oversized prompts:** Multiple oversized `-p` values in a single invocation is not a supported pattern; the last value wins and a `warnings.warn` is emitted.
- **Scope:** `_resolve_args()` is called by all three execution paths (`run()`, `popen()`, `run_with_monitor()`), so the protection applies universally.
- **stdin writer threading:** When the subprocess is spawned with a `stdin_prompt`, `_spawn_stdin_writer()` writes the payload to `proc.stdin` in a daemon thread, flushes, and closes the stream so Claude observes EOF and proceeds. Performing the write from a background thread prevents deadlock when the prompt exceeds the OS pipe buffer (typically 64 KB) and the child is waiting for EOF before draining stdin. Write failures (`BrokenPipeError`, `OSError`) are swallowed in the writer; they surface as a subprocess error via stdout/returncode.
- **Chat history preservation:** `_record_prompt()` in `LLMCaller` executes before `_resolve_args()`, so chat history always records the original prompt text.

#### Scenario: Automatic context injection
- **WHEN** the flow engine enters a step
- **THEN** the program automatically collects the context required by that step
- **AND** injects the context into the prompt of the LLM invocation

#### Scenario: LLM invocation failure
- **WHEN** the LLM invocation within a step fails (timeout, API error, invalid output)
- **THEN** the flow engine executes the retry strategy (up to 3 times)
- **AND** if the retries still fail, pauses the flow and notifies the user

#### Scenario: Large prompt rerouted to subprocess stdin
- **WHEN** a `-p`/`--prompt` argument's UTF-8 byte length exceeds 100 KB (102,400 bytes)
- **THEN** `_resolve_args()` keeps the `-p`/`--prompt` flag in argv but drops its value, and returns the dropped value as a separate `stdin_prompt` string
- **AND** the execution path feeds `stdin_prompt` to the spawned Claude subprocess via stdin (no temp file is written)
- **AND** for `popen()` callers, `_spawn_stdin_writer()` writes the payload from a daemon thread, flushes, and closes `proc.stdin` so Claude observes EOF

#### Scenario: Prompt below stdin-routing threshold
- **WHEN** a `-p`/`--prompt` argument's UTF-8 byte length is at or below 100 KB
- **THEN** the prompt is passed directly as a command-line argument
- **AND** `stdin_prompt` is `None` and the subprocess's stdin is not used for the prompt

#### Scenario: Multiple oversized -p values in one invocation
- **WHEN** a single argv contains more than one oversized `-p`/`--prompt` value
- **THEN** the last oversized value is the one routed to stdin (last-wins)
- **AND** `_resolve_args()` emits a `UserWarning` via `warnings.warn(..., stacklevel=2)` indicating that multiple oversized prompts in a single invocation are not supported and only the last is routed to stdin
- **AND** no exception is raised — execution continues with last-wins semantics so callers passing exactly one oversized prompt are unaffected

#### Scenario: Explicit @file reference is passed through unchanged
- **WHEN** an argument starts with `@`, or `-p`/`--prompt` is followed by an `@file` reference
- **THEN** `_resolve_args()` leaves the argument untouched regardless of size
- **AND** no stdin rerouting is performed for that argument

### Requirement: Read-Only Step Constraint Injection

The flow engine SHALL enforce a prompt-level file modification prohibition for read-only steps, preventing the LLM from accidentally modifying code during analysis-only steps.

**Read-Only Step Attribute:**

Each entry in the step pool (`STEP_POOL`) SHALL include a `read_only` boolean attribute. Steps marked `read_only: true` are:
- `discovery`, `analyze`, `plan`, `self_check`, `verify_spec`, `version_analyze`, `summarize`
- Deprecated steps (`project_summary`)

Steps explicitly marked `read_only: false`:
- `implement`, `test`, `update_spec`, `commit`, `confirm`
- Deprecated steps (`propose`, `design`, `plan_tasks`)

**Injection Mechanism:**

1. `context_builder.get_read_only_injection(step_type)` queries STEP_POOL by step name to determine if the step is read-only. If so, it returns a constraint prompt; otherwise returns an empty string.
2. `LLMCaller.call()` invokes `get_read_only_injection()` after user extra_prompt injection but before mode dispatch. The constraint is appended to the prompt for all JSON modes (STRICT, EXTRACT, TWO_PHASE, OFF).

**Constraint Prompt Content:**

The injected prompt SHALL:
- Explicitly forbid use of Write, Edit, NotebookEdit tools
- Explicitly forbid creating new files
- Explicitly forbid shell commands that modify files (sed, awk, tee, redirects)
- Explicitly allow Read, Grep, Glob, and read-only Bash commands
- State that the step's purpose is analysis and reasoning only

#### Scenario: Read-only step receives constraint injection
- **WHEN** LLMCaller executes a step marked `read_only: true` (e.g., `analyze`, `plan`)
- **THEN** the read-only constraint prompt is appended to the LLM prompt
- **AND** the constraint forbids all file modification tools and commands

#### Scenario: Non-read-only step receives no constraint
- **WHEN** LLMCaller executes a step marked `read_only: false` (e.g., `implement`, `update_spec`)
- **THEN** no read-only constraint is injected
- **AND** the LLM can freely modify files

#### Scenario: Discovery step is read-only
- **WHEN** LLMCaller executes the `discovery` step
- **THEN** the read-only constraint prompt is appended to the LLM prompt
- **AND** the discovery step cannot modify files, consistent with its sole responsibility of producing the Proposed Task Description

#### Scenario: SELF_CHECK step is read-only
- **WHEN** LLMCaller executes the `self_check` step
- **THEN** the read-only constraint prompt is appended to the LLM prompt
- **AND** the self_check step cannot modify files, consistent with its sole responsibility of reviewing code for logic completeness and robustness

### Requirement: Spec Names Injection for Downstream Steps

The flow engine SHALL extend spec awareness beyond the `analyze` step's predetermined selection by injecting "the list of all available specs + a soft hint to consult on demand via the bounded index-first protocol" into designated downstream LLM sub-process steps. This gives those steps whole-spec-set awareness and lets them supplement their context through the read-only `se3 spec` index commands (`se3 spec index` to navigate, `se3 spec show <spec>::<requirement>` to read one Requirement's body) when `analyze` missed a relevant spec — never by reading a whole `se3/specs/<name>/spec.md` file with the Read tool (large specs exceed the Read size limit).

**Helper API:**

```python
get_spec_names_injection(
    step_type: str,
    project_root: Path,
    relevant_specs: list[str] | None = None,
) -> str
```

Returns the injection prompt fragment, or an empty string when the step is not in the whitelist. Handlers call the helper at prompt-build time and append the return value to their prompt, mirroring the existing `get_issue_discovery_injection` / `get_step_language_instruction` call sites.

**Three-tier whitelist (mirrors `issue_discovery`):**

1. `SPEC_NAMES_INJECTION_FORBIDDEN_STEPS = {"summarize", "commit"}` — hard block; always returns empty even if `se3.yaml` lists the step.
2. `SPEC_NAMES_INJECTION_DEFAULT_STEPS = ["plan", "plan_tasks", "implement", "verify_spec", "update_spec", "self_check"]` — default whitelist applied when `se3.yaml` has no override. Deprecated step types (`propose`, `design`) are intentionally absent: their stub handlers forward to the unified `plan_handler`, which looks up injection under `"plan"`, so listing them would be dead code.
3. `se3.yaml` override key `spec_names_injection.steps` — replaces the default list when present. FORBIDDEN still takes precedence. Non-list override values (bare string, dict, etc.) are silently ignored in favor of the defaults.

**Covered steps by default:**

| Step | Injected | Rationale |
|------|----------|-----------|
| `plan`, `plan_tasks` | yes | Task decomposition benefits from whole-spec-set awareness |
| `implement` | yes | Implementation may discover need for additional specs (e.g., versioning) |
| `verify_spec` | yes | Verification needs full spec set to judge compliance |
| `update_spec` | yes | Already uses the `se3 spec` index-first protocol; spec-names list makes it more reliable |
| `self_check` | yes | Self-review may touch unpreselected specs |
| `design`, `propose` (deprecated) | yes, via forwarding | Deprecated stub handlers forward to `plan_handler`, which looks up the injection under `"plan"`. They are therefore covered transitively and are **not** listed in `SPEC_NAMES_INJECTION_DEFAULT_STEPS` themselves. |
| `analyze`, `discovery` | no | Already natively list specs via their own prompt templates |
| `summarize`, `commit` | no (FORBIDDEN) | No spec awareness needed for summary/commit |
| `confirm_llm_review` | no (initial) | Review output aligns with task_description; conservative default |

**Injection prompt content (soft constraint):**

- Begins with heading `## Available Specifications`.
- Line `All available specs in this project: <sorted names>.` — sourced by scanning `project_root/se3/specs/*/spec.md`, sorted alphabetically.
- Line `Specs already loaded above: <loaded names or "none">.` — derived from `relevant_specs` argument so the LLM does not re-read specs already embedded in the prompt.
- Soft guidance: the LLM **MAY** (not MUST) consult additional specs on demand through the read-only `se3 spec` index commands (run via Bash) — `se3 spec index` for the root view, `se3 spec index <spec> [<group>...]` to drill into one spec's Requirement index, and `se3 spec show <spec>::<requirement>` to read the authoritative body of ONE Requirement plus its physical location.
- Anti-abuse wording: an explicit prohibition on reading an entire `spec.md` file with the Read tool (large specs exceed the Read size limit) — navigate with `se3 spec index` and fetch only the specific Requirement bodies needed with `se3 spec show` — plus "Only consult specs that directly help the task — avoid reading broadly."

**Abuse prevention:**

- The MAY phrasing discourages blanket reads that would inflate token usage.
- The already-loaded list prevents duplicate reads of specs `analyze` already embedded.
- `SPEC_NAMES_INJECTION_FORBIDDEN_STEPS` hard-blocks steps with no spec-awareness need.
- Errors in loading `se3.yaml` fall back to defaults (no crash).

**Compatibility:**

- The existing `spec_content` and `relevant_specs` input fields are unchanged; the injection is purely additive to the prompt suffix.
- `analyze`'s pre-selection logic is untouched — this mechanism is a safety net, not a replacement.

#### Scenario: Default whitelist receives spec names injection
- **WHEN** a default whitelisted step (e.g., `plan`, `implement`, `verify_spec`, `update_spec`, `self_check`) builds its LLM prompt
- **AND** `se3.yaml` has no `spec_names_injection.steps` override
- **THEN** `get_spec_names_injection(step_type, project_root, relevant_specs)` returns a non-empty prompt fragment containing `## Available Specifications`, the sorted `All available specs in this project:` line, and a `Specs already loaded above:` line derived from `relevant_specs`
- **AND** the handler appends the fragment to its prompt suffix

#### Scenario: FORBIDDEN steps never receive injection
- **WHEN** the step type is `summarize` or `commit`
- **THEN** `get_spec_names_injection(step_type, project_root, relevant_specs)` returns an empty string
- **AND** even when `se3.yaml` explicitly lists the step in `spec_names_injection.steps`, the FORBIDDEN set takes precedence and the return remains empty

### Requirement: Spec File Write Protection (Soft Injection + Within-Flow Diff Guard)

The flow engine SHALL prevent any step other than `update_spec` and the `se3 sync` steps from creating, modifying, or deleting files under `se3/specs/`, closing the governance gap whereby a non-`update_spec` step could write spec files and thereby inject undrafted, ungoverned content into the spec index that this flow's later steps consume. This protection governs **only who may write spec files** — it deliberately does NOT constrain **whether implementation may change existing behavior**. Changing behavior and writing a spec file are two different things: the former is allowed and is handled by the existing lenient mechanism (`plan` declares structured `spec_changes`, `verify_spec` classifies deviations matching a planned `spec_change` as `out_of_scope`/`low` rather than a regression, and `update_spec` writes the new behavior back into the spec); the write protection added here MUST NOT alter or weaken that lenient mechanism.

**Single derived exemption set.** All layers of this protection consult one authoritative constant in `context_builder.py`, `SPEC_WRITE_ALLOWED_STEPS`, which is *derived* from the authoritative sync-step constants rather than hand-enumerated, so the exemption set cannot drift:

```
_READ_ONLY_SYNC_STEPS = {"sync_scan", "sync_analyze"}        # existing read-path sync steps
_WRITABLE_SYNC_STEPS  = {"sync_resolve", "sync_respond"}     # sync steps that write spec via llm_caller (Way-A Edit)
_ALL_SYNC_STEPS       = _READ_ONLY_SYNC_STEPS | _WRITABLE_SYNC_STEPS
SPEC_WRITE_ALLOWED_STEPS = {"update_spec"} | _ALL_SYNC_STEPS
```

Any future sync step that writes spec MUST be registered in `_WRITABLE_SYNC_STEPS` (alongside the read-path set it already belongs next to), which automatically enrolls it in every layer's exemption. This derivation roots out the prior class of defect where a hand-listed exemption set silently omitted `sync_respond`.

**Soft layer — reusable prompt injection.** `context_builder.get_spec_write_protection_injection(step_type)` returns a non-empty constraint fragment for every step that is, per `STEP_POOL`, `uses_llm=True` and `read_only=False` and whose `step_type` is not in `SPEC_WRITE_ALLOWED_STEPS`; otherwise it returns an empty string. It is appended by `LLMCaller.call()` at the same site as `get_read_only_injection`, mirroring the existing injection call sites. By construction it currently covers `implement` (all three of `IMPLEMENT_PROMPT` / `IMPLEMENT_GROUP_PROMPT` / `FIX_PROMPT`), `plan_tasks`, `propose`, and `design`, and auto-covers any future non-read-only LLM step across the `feature` / `discovery` / `bugfix` / `small` / `directive` flows. The sync steps are not in `STEP_POOL` at all, so the soft layer never injects into them regardless of the constant; the `SPEC_WRITE_ALLOWED_STEPS` exclusion is a redundant double-guard at the soft layer (the constant is *consumed* decisively only by the hook and diff layers). The injected wording SHALL:
- explicitly state that the step MAY change existing behavior;
- mark `se3/specs/**` as read-only and writing spec files as the sole responsibility of `update_spec` / `se3 sync`;
- forbid creating / modifying / deleting spec files via `Write` / `Edit` / `NotebookEdit` or via `Bash` (`>`, `sed`, `tee`, etc.);
- instruct that if this change alters existing behavior or needs a spec change, the step only notes it in its `summary`, leaving the `plan.spec_changes` → `verify_spec` → `update_spec` channel to handle it;
- avoid any rejected spec-driven framing (it MUST NOT say "must comply with the spec" or "must not change recorded behavior").

**Plan-specific constraint.** `plan` is itself `read_only: true` (so it cannot write spec files), but it is the upstream culprit because it can bake a "write spec files" intent into the implementation tasks it produces. `plan.py` therefore carries a dedicated section (alongside `SPEC_CHANGES_SECTION`) stating that the generated implementation tasks / groups MUST NOT instruct any downstream step (especially `implement`) to create / modify / delete files under `se3/specs/`, while simultaneously continuing to (and being encouraged to) declare expected spec/behavior changes through the structured `spec_changes` output — which is consumed only by `update_spec` and feeds `verify_spec`'s lenient classification. The constraint forbids "instructing downstream to write spec files"; it MUST NOT suppress "declaring `spec_changes` intent". Its wording also avoids the rejected anti-regression framing.

**Within-flow spec-diff fallback guard.** In `state_machine.run_step`, for every step whose `step_type` is not in `SPEC_WRITE_ALLOWED_STEPS` (and when `spec_write_protection.diff_fallback_enabled` is set), the engine snapshots the content hashes of all `se3/specs/**` files before the handler runs and compares them afterward; if any spec file changed (in particular a change that bypassed the PreToolUse hook via `Bash`), the step is marked `FAILED` with an error message naming the offending step and the changed files. This guard reuses the spec enumeration / hashing capability of `spec_gate` but is computed per-step (not the flow-level `spec_requirement_baseline`). It tests **only whether a spec file was written** — it is orthogonal to and does NOT perceive or change `verify_spec`'s `in_scope` / `out_of_scope` classification; `update_spec` and all sync steps are skipped because they are in `SPEC_WRITE_ALLOWED_STEPS`.

The hard layer's runtime enforcement (the PreToolUse hook injected via a controlled `--settings` file, and the `--settings` argv wiring) is specified by the llm-caller spec (*Tool-Layer Read-Only Enforcement*) and the agent-runner-infrastructure spec (*ClaudeCodeRunner Argument Construction*); its enable decision reuses the same `SPEC_WRITE_ALLOWED_STEPS`. Both hard sub-layers default on and are gated by `spec_write_protection` (see the se3-config spec).

#### Scenario: Non-read-only LLM step receives the spec-write-protection injection
- **WHEN** `LLMCaller` builds the prompt for a step that is `uses_llm=True`, `read_only=False`, and not in `SPEC_WRITE_ALLOWED_STEPS` (e.g., `implement`, `plan_tasks`, `propose`, `design`)
- **THEN** `get_spec_write_protection_injection(step_type)` returns a non-empty fragment and it is appended to the prompt
- **AND** the fragment allows changing existing behavior while marking `se3/specs/**` read-only and writing spec files the responsibility of `update_spec` / `se3 sync`
- **AND** the fragment contains no rejected spec-driven framing phrase

#### Scenario: update_spec and sync steps receive no spec-write-protection injection
- **WHEN** the step is `update_spec` or any step in `SPEC_WRITE_ALLOWED_STEPS`
- **THEN** `get_spec_write_protection_injection(step_type)` returns an empty string and the step may write `se3/specs/**` files

#### Scenario: Plan forbids routing spec writes downstream but keeps spec_changes
- **WHEN** the `plan` step builds its prompt at any depth
- **THEN** the prompt instructs that generated implementation tasks/groups MUST NOT direct any downstream step to create/modify/delete `se3/specs/` files
- **AND** the prompt continues to require the structured `spec_changes` declaration of expected spec/behavior changes

#### Scenario: Within-flow spec-diff fails a non-exempt step that wrote a spec file
- **GIVEN** a step not in `SPEC_WRITE_ALLOWED_STEPS` runs with `diff_fallback_enabled` set
- **WHEN** the step's handler changes any file under `se3/specs/**` (for example via a `Bash` redirect that bypassed the PreToolUse hook)
- **THEN** the engine detects the change by per-step content-hash comparison and marks the step `FAILED`, naming the step and the changed files
- **AND** this verdict is independent of `verify_spec`'s `in_scope` / `out_of_scope` classification

#### Scenario: Behavior-change channel is not impeded by the write protection
- **GIVEN** a flow that intentionally changes existing behavior and whose `plan` declared the corresponding `spec_changes`
- **WHEN** the flow runs through `implement` → `verify_spec` → `update_spec`
- **THEN** `verify_spec` still classifies the planned deviation as `out_of_scope` (not an `in_scope` failure) and the flow passes
- **AND** `update_spec` writes the new behavior back into the spec without being blocked by any soft or hard guard added here

### Requirement: Runtime Environment Capabilities Injection

The flow engine SHALL inject a fixed `## se3 Runtime Environment` section into the LLM sub-process prompt of designated downstream steps, advertising the se3-tool-provided **read-only capabilities the LLM MAY proactively invoke** (history / issue inspection) and a **write-operation guardrail blacklist** of commands the LLM MUST NOT proactively invoke. This makes downstream LLM sub-processes aware of the se3 capabilities available in the host project so they can (a) inspect prior session history when the user references "the previous session / last run / history", (b) consult related issue context when the task touches an existing issue, and (c) refrain from accidentally invoking destructive write operations (`merge` / `salvage` / `sync` / etc.) merely because those commands exist.

The injected text is owned by se3 itself, evolves with the se3 version, and is **NOT written into the downstream project's spec files**. Both new and existing projects receive the injection without any migration.

**Helper API:**

```python
get_runtime_environment_injection(
    step_type: str,
    project_root: Path,
) -> str
```

Returns the runtime-environment prompt fragment (with a leading `\n\n` consistent with sibling injections), or an empty string when the step is not in the whitelist or when the source markdown cannot be loaded. Handlers call the helper immediately after `get_spec_names_injection` / `get_issue_discovery_injection` at prompt-build time and append the return value to their prompt.

**Source file:** The injected content is stored in `src/se3/engine/runtime_environment.md` as a standalone markdown file, shipped with the se3 wheel and read at runtime via the helper. The markdown content is process-cached after first read; a missing or unreadable file returns `""` and logs one warning, never raising.

**Three-tier whitelist (mirrors `spec_names_injection` / `issue_discovery`):**

1. `RUNTIME_ENV_INJECTION_FORBIDDEN_STEPS = {"commit", "version_analyze"}` — hard block; always returns empty even if `se3.yaml` lists the step.
2. `RUNTIME_ENV_INJECTION_DEFAULT_STEPS = ["analyze", "plan", "plan_tasks", "implement", "verify_spec", "update_spec", "self_check", "discovery", "summarize"]` — default whitelist (9 steps) applied when `se3.yaml` has no override.
3. `se3.yaml` override key `runtime_environment_injection.steps` — replaces the default list when present and shaped as a list. FORBIDDEN still takes precedence. Null / malformed / non-list override values are silently ignored in favor of the defaults.

**Injection content structure (excerpt, not full markdown):**

The injected markdown SHALL contain a top-level `## se3 Runtime Environment` heading followed by:

- A **read-only whitelist** with two categories the LLM is encouraged to invoke in matching scenarios:
  1. *History inspection (read-only)* — triggers: user mentions "last/previous session", "where did we leave off", "history", "earlier run", etc. Covered tools:
     - `se3 history list` — list recent flow runs
     - `se3 history show <flow_id>` — show structured details of one run
     - `se3 history archived` — list archived engine states
     - Free-text fallback: read/grep `se3/history/<flow_id>/<step>.jsonl` and `se3/state/archive/engine_*.json`
     - Recommended workflow sentence: first `se3 history list` to locate `flow_id`, then `se3 history show <flow_id>`; for keyword search inside conversation content, `grep -r 'keyword' se3/history/<flow_id>/`.
  2. *Issue context inspection (read-only)* — triggers: the task references a known issue or the user mentions an issue id / related requirement. Covered tools:
     - `se3 issue list` — list issues
     - `se3 issue show <id>` — show issue detail
     - Free-text fallback: grep `se3/issues/open/*.yaml`, `se3/issues/closed/*.yaml`
     - Recommended workflow sentence: first `se3 issue list` to scan the inventory; for keyword search inside issue bodies, `grep -r 'keyword' se3/issues/`.

- A **write-operation blacklist** explicitly telling the LLM "do not proactively invoke unless the user explicitly asks in this session":
  - `se3 history restore` — rolls back flow state
  - `se3 issue create` / `se3 issue reset` — write operations; the LLM should not autonomously create issues
  - `se3 salvage` — auto-commits / creates issues / archives sessions; rescue-only
  - `se3 merge` / `se3 merge respond` — mutates git merge state
  - `se3 sync` / `se3 sync respond` — modifies spec files; high impact
  - `se3 init` — only for fresh-project initialization

The markdown body is the single source of truth; it MAY be iterated independently of the Python helper without breaking the injection contract, as long as the heading and the white/blacklist commands above remain present.

**Abuse prevention and compatibility:**

- The whitelist is phrased as **"MAY when the user references the matching scenario"**, not "MUST", to discourage blanket invocation.
- The blacklist is phrased as **"do not proactively invoke"** rather than "forbidden", leaving the user explicit-request override intact.
- The injection is purely additive to the prompt suffix and does not change any existing handler input / output schema.
- FORBIDDEN steps (`commit`, `version_analyze`) are mechanical / non-LLM-deliberative; they receive no injection so the prompt remains tight.
- Errors loading `runtime_environment.md` (missing / IO failure) degrade gracefully to `""` plus a single warning log.

#### Scenario: Default whitelist step receives runtime environment injection
- **WHEN** a default whitelisted step (e.g., `analyze`, `plan`, `plan_tasks`, `implement`, `verify_spec`, `update_spec`, `self_check`, `discovery`, `summarize`) builds its LLM prompt
- **AND** `se3.yaml` has no `runtime_environment_injection.steps` override
- **THEN** `get_runtime_environment_injection(step_type, project_root)` returns a non-empty prompt fragment containing the heading `## se3 Runtime Environment`, the whitelist entries (`se3 history list`, `se3 history show`, `se3 history archived`, `se3 issue list`, `se3 issue show`), path references (`se3/history/<flow_id>`, `se3/issues/`), the two recommended-workflow sentences, and the blacklist entries (`se3 history restore`, `se3 issue create`, `se3 issue reset`, `se3 salvage`, `se3 merge`, `se3 sync`, `se3 init`)
- **AND** the handler appends the fragment to its prompt suffix immediately after the `get_spec_names_injection` call site

#### Scenario: FORBIDDEN step never receives runtime environment injection
- **WHEN** the step type is `commit` or `version_analyze`
- **THEN** `get_runtime_environment_injection(step_type, project_root)` returns an empty string
- **AND** even when `se3.yaml` explicitly lists the step in `runtime_environment_injection.steps`, the FORBIDDEN set takes precedence and the return remains empty

### Requirement: JSON Extraction Modes

The flow engine SHALL support four JSON extraction modes, selecting the optimal strategy based on step characteristics:

| Mode | Description | Applicable Scenarios |
|------|------|----------|
| **STRICT** | Enforce JSON format, retry on failure | Simple output (analyze) |
| **EXTRACT** | Require JSON format, extract with LLM on failure | Medium complexity (verify_spec, update_spec) |
| **TWO_PHASE** | Natural generation + LLM extraction | Complex/large output (plan, implement) |
| **OFF** | No JSON constraint, return LLM text as-is | Free-text output (summarize) |

**Mode selection principles:**
- Simple output (<1K tokens): STRICT (low cost, high reliability)
- Medium complexity (1K-5K tokens): EXTRACT (balances reliability and token efficiency)
- Large output (>5K tokens): TWO_PHASE (avoids prompt pollution, handles truncation)
- Free text (Markdown / prose): OFF (no JSON constraint, returns raw text directly)

**Mode resolution (`get_json_mode` / `_resolve_json_mode`):**

`LLMCaller.call()` and the module-level `get_json_mode()` resolve the final mode according to the following priority:

1. Explicit `json_mode` parameter (string `"strict" | "extract" | "two_phase" | "off"`, case-insensitive; or the corresponding `JsonMode` enum value)
2. `two_phase_json=True` → `TWO_PHASE`
3. `require_json=True` → `STRICT`
4. Default → `OFF`

When the `json_mode` string cannot be recognized as one of the four values above, a warning log is recorded and it falls back to `OFF`, without raising an exception.

#### Scenario: OFF mode returns raw text
- **WHEN** a step (such as `summarize`) calls the LLM with `JsonMode.OFF`
- **THEN** the prompt is not wrapped with JSON constraints
- **AND** the LLM output is returned as-is, without JSON parsing or extraction
- **AND** it is suitable for generating Markdown summaries or other free text

#### Scenario: Default mode is OFF
- **WHEN** the caller provides none of `json_mode`, `require_json`, or `two_phase_json`
- **THEN** `get_json_mode()` returns `JsonMode.OFF`

#### Scenario: Unknown json_mode string falls back to OFF
- **WHEN** the caller passes an unrecognized `json_mode` string
- **THEN** a warning log is recorded (`Unknown json_mode '<value>', defaulting to 'off'`)
- **AND** it ultimately resolves to `JsonMode.OFF`, without raising an exception

#### Scenario: STRICT mode
- **WHEN** the analyze step requires simple task classification
- **THEN** use STRICT mode: the prompt adds enforced JSON instructions
- **AND** if the output is not JSON, retry the entire call

#### Scenario: EXTRACT mode
- **WHEN** the verify_spec step generates verification results
- **THEN** use EXTRACT mode: the prompt requires JSON format
- **AND** if the output is not JSON, use a lightweight LLM call to extract the JSON
- **AND** do not retry the main call, saving tokens

#### Scenario: TWO_PHASE mode
- **WHEN** the implement step generates output containing large file content
- **THEN** use TWO_PHASE mode: the prompt adds no JSON constraint
- **AND** the LLM generates content naturally
- **AND** a second LLM call extracts JSON from the natural output
- **AND** avoids prompt pollution and handles truncation better

#### Scenario: TWO_PHASE fast path with required_keys validation
- **GIVEN** a step uses TWO_PHASE mode and passes `required_keys` to `LLMCaller.call()`
- **WHEN** Phase 1 output contains valid JSON
- **THEN** the fast path validates the parsed JSON against `required_keys` via `parse_json_response(output, required_keys=required_keys)`
- **AND** if all required keys are present, Phase 2 is skipped and the validated JSON is returned
- **AND** if any required key is missing, the fast path falls back to Phase 2 extraction instead of returning incomplete data
- **AND** Phase 2 extraction also receives `required_keys` for end-to-end validation consistency
- **NOTE** `required_keys` is an optional parameter (default `None`) on both `call()` and `_call_two_phase()`, preserving backward compatibility for callers that do not need key validation

### Requirement: Chat History System (Chat History)

The flow engine SHALL record the prompt and response of every LLM call, support injecting conversation context on retry, and provide a human browsing interface. Tool call events (tool_use / tool_result) SHALL render human-readable previews using a per-tool semantic format, with the formatting logic centralized in the `tool_formatters` module.

**Storage format:**
- Storage path: `se3/history/{flow_id}/{step_id}.jsonl`
- One ChatMessage per line (JSON serialized)
- The storage layer keeps an array of parsed JSON objects (full fidelity, no double encoding required)
- The parsed text content is used when retrying with the LLM (reducing token waste)

**Data structures:**
- `ChatMessage`: role, content, raw_json, timestamp, step_type, attempt, agent_name, model_name
  - `raw_json`: `list[dict]` - array of JSON objects parsed from the NDJSON stream, each element being one line of NDJSON
  - `agent_name`: `Optional[str]` — the configured name (e.g. `dclaude`, `claude`, `kclaude`) of the agent that ran the attempt producing this record; `None` for records where the caller did not supply an agent name (backward-compatible: legacy jsonl records lacking this field load with `None`)
  - `model_name`: `Optional[str]` — the actual model identifier (e.g. `claude-opus-4-8`) best-effort extracted from the response's NDJSON `init`/`system` metadata; `None` when no model metadata was available or parsing failed (backward-compatible: legacy records lacking this field load with `None`). When serializing, both fields are omitted from the JSON output when their value is `None`, so existing records' line format stays byte-identical
- `ChatSession`: flow_id, step_id, step_type, messages

**Core features:**
- `record_prompt()` — records the prompt that was sent, with an optional `agent_name` identifying the configured agent for this attempt
- `record_response()` — records the raw LLM response, with optional `agent_name` and `model_name`; `model_name` is best-effort extracted from the response's NDJSON `init`/`system` metadata via `extract_model_name_from_ndjson(raw_ndjson)` (a pure helper that tolerates missing or malformed metadata, defaulting to `None` rather than raising)
- `record_stream_progress()` — **before** the LLM produces the final result, appends the in-progress content of the current step as partial records, one whole line at a time, to the per-step jsonl, for the daemon to read incrementally and for WS push to render line-by-line in real time. Each partial record looks like `{type: 'stream_progress', role: 'assistant', step_type, content, raw_json: [<obj>], timestamp, attempt, partial: true}` (`raw_json` is a single-element array), using the same "write one whole line at a time" atomic append semantics as `record_step_event`, so that each line can be fully read by the incremental reader once flushed to disk. Grouped with the final result by `(step_id, attempt)`: once the terminal (non-partial) assistant record arrives, the frontend collapses and removes the progress bubble for that turn (see running-flow-console *Three-Tier Progressive Disclosure*)
- `format_history_for_retry()` — formats the previous conversation context for retry (skips `stream_progress` records to avoid the retry prompt being bloated by progress lines, following the same pattern as the existing skipping of `step_completed` / `step_failed` events)
- `extract_assistant_text()` — extracts the assistant text content from NDJSON
- `segment_prompt()` — splits the prompt into annotated segments, used for structured display
- `render_session_detailed()` — renders a Rich visual output with structured prompt and response
- `get_detailed_json()` — gets structured JSON containing the segmented prompt and the full response
- `_extract_final_text()` — extracts the last assistant text block from raw_json
- `split_implement_session_by_iterations()` — splits one implement ChatSession into multiple virtual per-iteration ChatSessions by the test session timestamps (display layer only, not persisted)
- `interleave_sessions_for_display()` — re-orders all ChatSessions of a flow so that the virtual implement splits interleave chronologically with test/self_check

**Tool call formatting (tool_formatters module):**

`tool_formatters.py` is the single authoritative source for tool call preview formatting, consumed jointly by `llm_caller.py` (streaming output) and `chat_history.py` (history rendering / retry context).

- Public API: `format_tool_use_preview(tool_name, input_data)`, `format_tool_result_preview(tool_name, result_data)`, and `format_tool_diff(tool_name, input_data, result_data, old_content=None)`
- Single-chip protocol public API (paired with the per-chip extension fields of `llm-caller` *Streaming NDJSON Output Display* and `running-flow-console` *Tool Call Chip State Machine*, which must stay public and stable; future LLM subprocesses must not rename or delete them arbitrarily):
  - `format_tool_chip_in_flight_header(tool_name, use_input)` — computes the in-flight chip header text based on the `tool_use` input only (e.g. `Read: <path>:<offset>-<end>`), for writing into the `content` of the `stream_progress` at the `tool_use` phase
  - `format_tool_chip_header(tool_name, use_input, result_data, is_error)` — at the `tool_result` phase, merges the use and result summaries to compute the single-chip terminal header (e.g. `Read ✓ <path> · <N> lines` or `Read ✗ <error_preview>`), writing it into the `content` of the terminal `stream_progress`, avoiding the loss of detail when the frontend splits on the colon
  - `build_tool_detail_payload(tool_name, use_input, result_data, old_content=None)` — computes a JSON-safe structured detail dict, whose `kind` key takes one of `edit_diff` / `write_full` / `write_diff` / `read_text` / `bash_output` / `grep_matches` / `glob_matches` / `text`, carrying the data needed by the chip detail panel (the unified diff text and starting line number for Edit/Write, the full content of a newly created Write file, the `start_line` and body for Read, the separated stdout/stderr for Bash, the match list for Grep/Glob, etc.); its size is constrained by `TOOL_DETAIL_PAYLOAD_MAX_CHARS` in `engine/truncation.py`, with overlong tails truncated
  - unregistered tools fall back to the generic `text` kind, ensuring new tools do not crash the frontend
- Internally maintains a `TOOL_FORMATTERS` dictionary registry (`{tool_name: {use: fn, result: fn, diff: str}}`), mapping tool names to dedicated formatting functions; the optional `diff` key marks that the tool supports diff rendering
- Unregistered tool names fall back to the generic formatter (key=value truncated preview)
- Provides `truncate_preview()`, a generic truncation utility function (for non-path text: command strings, error messages, JSON, etc.)
- Provides `truncate_path()`, a truncation function dedicated to file paths: (1) converts an absolute path to a path relative to the project root; (2) if it still exceeds `max_length` (default 160 characters), abbreviates the middle while preserving the first directory segment and the filename (format `first_dir/.../filename`); (3) the filename (last segment) is never truncated. All file path arguments in per-tool formatters use `truncate_path` rather than `truncate_preview`
- Provides module-level `set_project_root(root)` / `get_project_root()` functions to manage the project root directory, used by `truncate_path` during path conversion; `LLMCaller` calls `set_project_root()` to set the project root before creating the `StreamJSONTracker`
- Provides `generate_edit_diff(old_string, new_string, file_path)`, which uses `difflib.unified_diff` to generate a unified diff (3 lines of context)

**Built-in per-tool formatters:**

| Tool | tool_use preview | tool_result preview |
|------|-----------------|-------------------|
| Edit | `Edit: {file_path} ({n} lines → {m} lines)` | `Edit ✓ {file_path}` or error info |
| Write | `Write: {file_path} ({n} lines)` | `Write ✓ {file_path}` |
| Read | `Read: {file_path}:{offset}-{end}` | `Read ✓ ({n} lines)` |
| Bash | `Bash: {command preview}` | `Bash ✓ ({n} lines output)` |
| Grep | `Grep: /{pattern}/ in {path}` | `Grep ✓ ({n} matches)` |
| Glob | `Glob: {pattern} in {path}` | `Glob ✓ ({n} files)` |

#### Scenario: Record LLM conversation
- **WHEN** LLMCaller sends a prompt to the LLM
- **THEN** automatically record the prompt to `se3/history/{flow_id}/{step_id}.jsonl`
- **AND** after the LLM responds, record the array of parsed JSON objects (`raw_json: list[dict]`)

#### Scenario: Agent and model name recorded per attempt
- **WHEN** `record_prompt` or `record_response` is called with non-`None` `agent_name` (and optionally `model_name`)
- **THEN** those values are stored on the `ChatMessage` instance and included in its serialization (when non-`None`; `None` values are omitted from the JSON output so existing records' line format stays byte-identical)
- **AND** the daemon→server history push passes the full `ChatMessage` envelope through verbatim — no field whitelist or裁剪 logic strips these new keys
- **AND** a legacy jsonl record written before these fields existed (no `agent_name` or `model_name` key in the line) still deserializes without error via `ChatMessage.from_dict`, which treats missing keys as `None` defaults, so the change is backward compatible

#### Scenario: Agent name fixed per attempt, independent of rotation
- **GIVEN** a step where the first internal attempt uses agent `dclaude` and a rotation switches the second attempt to agent `claude`
- **WHEN** the two attempts' prompt and response records are inspected
- **THEN** the first attempt's records carry `agent_name="dclaude"` and the second's carry `agent_name="claude"`
- **AND** each attempt's `agent_name` is the name of the agent actually selected at the start of that attempt, not the caller's `_current_agent_index` at any later point

#### Scenario: raw_json format storage
- **WHEN** the LLM returns an NDJSON stream (multiple lines of JSON)
- **THEN** parse each line into a dict and store as an array
- **AND** avoid double encoding (no longer storing JSON converted to a string)
- **AND** the history records can be queried directly with tools such as jq

#### Scenario: In-progress content flushed line-by-line before results are produced
- **WHEN** a step's LLM call keeps generating in-progress content before producing the final JSON result
- **THEN** `record_stream_progress()` appends the progress content as records with `type: 'stream_progress'`, `partial: true`, one whole line at a time, to `se3/history/{flow_id}/{step_id}.jsonl`
- **AND** the `attempt` of each partial record matches that of the final result record, so the two are grouped by `(step_id, attempt)`
- **AND** the progress lines are flushed to disk before the final result record is written, and can be fully read by the daemon incremental reader and pushed via WS

#### Scenario: CLI history and retry context skip stream_progress records
- **WHEN** `get_step_history()` renders history, or `format_history_for_retry()` constructs the retry context
- **THEN** both skip `stream_progress` (partial) records, following the same pattern as the existing skipping of `step_completed` / `step_failed` events
- **AND** the CLI history does not re-render progress lines, and the retry prompt is not bloated by progress lines

#### Scenario: Inject conversation context on retry
- **WHEN** an LLM call fails and is retried
- **THEN** obtain the previous conversation context from the chat history
- **AND** inject the context in front of the retry prompt
- **AND** the format is `[Previous conversation context for this step]: ... [The above attempt(s) failed.]`

#### Scenario: Semantic rendering of tool calls
- **WHEN** the LLM streaming output contains `tool_use` or `tool_result` events
- **OR** the chat history needs to render tool calls in the history
- **THEN** `StreamJSONTracker.process_line()` parses the NDJSON lines: `tool_use` blocks are nested in `message.content[]` of a `type: "assistant"` message; `tool_result` blocks are nested in `message.content[]` of a `type: "user"` message (fields use snake_case: `tool_use_id`, `is_error`)
- **AND** retain backward-compatible handling of the legacy top-level `type: "tool_result"` format (supporting both `toolUseId`/camelCase and `tool_use_id`/snake_case)
- **AND** the shared `_handle_tool_result()` helper method uniformly handles the tool_result logic of both formats, avoiding duplication
- **AND** `format_tool_use_preview(tool_name, input_data)` routes to the per-tool formatting function according to the `TOOL_FORMATTERS` registry
- **AND** `format_tool_result_preview(tool_name, result_data)` likewise routes to the per-tool result formatting function
- **AND** the Edit tool displays the file path and the number of changed lines (e.g. `Edit: path/file.py (3 lines → 5 lines)`)
- **AND** the Write tool displays the file path and the number of content lines
- **AND** the Read tool displays the file path and the read range
- **AND** the Bash tool displays a truncated preview of the command
- **AND** the Grep tool displays the search pattern and path
- **AND** the Glob tool displays the match pattern and path
- **AND** unregistered tools fall back to the generic formatter (key=value truncated, up to 3 arguments)
- **AND** formatting is done at the LLM abstraction layer (tool_formatters module), independent of any specific agent tool implementation

#### Scenario: Edit/Write tool diff rendering
- **WHEN** the LLM streaming output contains a `tool_result` event for the Edit or Write tool
- **THEN** `StreamJSONTracker` caches the input arguments of the Edit/Write tool into the `_tool_use_id_to_input` mapping at the `tool_use` event
- **AND** for the Write tool, additionally reads the current content of the target file at the `tool_use` event and caches it into the `_tool_use_id_to_old_content` mapping (caches `None` when the file does not exist or reading fails)
- **AND** at the `tool_result` event, retrieves the cached input arguments and original file content and calls `format_tool_diff(tool_name, input_data, result_data, old_content=old_content)`
- **AND** Edit tool: generates a unified diff from `old_string` / `new_string` via `generate_edit_diff()`, and calls `display.render_diff()` to render the diff output colored red (deletions) / green (additions) / cyan (hunk markers) / gray (context)
- **AND** Write tool (new file, `old_content` is `None`): displays a green `Created {file_path} ({n} lines)` marker, without showing a line-level diff
- **AND** Write tool (overwriting an existing file, `old_content` is not `None`): generates a unified diff via `generate_edit_diff(old_content, content, file_path)` and renders red/green colored output (file I/O occurs only once during the tracker's tool_use phase; the formatter layer does not access the file system)
- **AND** when the diff exceeds `max_lines` (default 50 lines), truncates it and displays a summary of the remaining line count
- **AND** `display.render_diff()` uses Rich `Text` objects to color line by line (no Panel border, no Rule, no horizontal line), outputs per **Block Rendering Visual Style** a reverse-video color-block title `## Diff: {file_path}` (white text on yellow background) followed by a blank line; each line is prefixed with a dim-styled line number (fixed column width 4, with the starting line number parsed from the `@@ -a,b +c,d @@` hunk header, deletion lines showing the old-file line number, addition and context lines showing the new-file line number); the `total` line count excludes the `---`/`+++` header lines; upon reaching `max_lines` (default 50) it appends a dim-styled `... (N more lines)` summary and then stops; below the diff content it prints another blank line and a fixed-width (4 characters) reverse-video yellow color block as the bottom boundary; when `displayed == 0` it outputs neither the title, content, blank lines, nor the bottom boundary color block
- **AND** diff rendering is performed only for tools whose entry in the `TOOL_FORMATTERS` registry contains a `diff` key; other tools are a no-op

#### Scenario: StreamJSONTracker cache management
- **WHEN** `StreamJSONTracker` caches tool_use input arguments (`_tool_use_id_to_input`, `_tool_use_id_to_old_content`, `_tool_use_id_to_name`)
- **THEN** in the normal flow, the success path of `_handle_tool_result()` cleans up the corresponding entries via `.pop()`
- **AND** the error path (`is_error=True`) likewise cleans up the corresponding entries in all three cache dictionaries via `.pop()`
- **AND** the `print_summary()` method calls `.clear()` to empty all cache dictionaries when the stream ends, preventing memory leaks when the stream is interrupted abnormally
- **AND** the cache capacity is limited to `_MAX_CACHE_SIZE` (default 100); when exceeded, the oldest entry is evicted (simultaneously cleaning up `_tool_use_id_to_input`, `_tool_use_id_to_old_content`, and `_tool_use_id_to_name`)

#### Scenario: Add group identifier prefix to streaming output during multi-group execution
- **WHEN** the implement step is executed in groups (DAG parallel or sequential), with multiple groups calling the LLM in separate batches
- **THEN** prepend a group identifier prefix to each line of `[llm-stream]` and `[llm-caller]` output, with the format `[G1] [llm-stream] ...`
- **AND** the prefix is passed in via the `stream_prefix` parameter of the `LLMCaller` constructor, then passed through to `StreamJSONTracker`
- **AND** `StreamJSONTracker` inserts `stream_prefix` before all `[llm-stream]` print lines (tool_use, tool_result, error, summary)
- **AND** `LLMCaller` likewise inserts `stream_prefix` before all `[llm-caller]` print lines (Phase 2 extraction, JSON retry, cache skip, etc.)
- **WHEN** a single-group or single LLM call (no grouped execution)
- **THEN** `stream_prefix` is an empty string, no prefix is added, and the existing output format is preserved
- **WHEN** the LOC threshold triggers merging multiple groups into a single LLM call
- **THEN** the group prefix is not displayed (a single LLM Call has only one execution stream, and merging group name prefixes would be redundant information), consistent with the single-group execution path

#### Scenario: Human browsing of chat history
- **WHEN** the user runs `se3 history` or `se3 history list`
- **THEN** display the list of all flows, aggregated from three data sources:
  - `se3/state/engine.json` — the currently active flow (source: active)
  - `se3/state/archive/engine_*.json` — archived flows (source: archived)
  - `se3/history/{flow_id}/` — historical flows that only have chat history (source: history)
- **AND** sort by updated_at in descending order, and display the Source column
- **AND** support `--active-only` and `--archived-only` filters
- **AND** support `--json` output in JSON format
- **AND** support filtering by flow_id and step_type
- **AND** distinguish between communication JSON (parsed and rendered) and LLM output JSON (displayed as-is)

#### Scenario: Automatic prompt segment splitting
- **WHEN** `segment_prompt()` processes an SE3 prompt text
- **THEN** use precompiled regex patterns to match known segment markers (such as `CRITICAL: You MUST respond with ONLY valid JSON`, `READ-ONLY STEP CONSTRAINT`, `IMPORTANT: You MUST respond in`, `You are an expert`, `## Discovery Context`, `## Available Specifications`, `## Project Context`, `[Additional user instruction]`, etc.)
- **AND** split the prompt into a `[{"title": str, "content": str}]` array
- **AND** the first segment defaults to the title "Prompt", with subsequent segment titles generated automatically by the matched pattern
- **AND** the generic `## Heading` pattern serves as a fallback to capture unmatched markdown second-level headings

#### Scenario: Structured detailed rendering (non-verbose)
- **WHEN** `render_session_detailed(session, verbose=False)` is called
- **THEN** return a list of Rich renderables
- **AND** after the user prompt is segmented by `segment_prompt()`, it is wrapped as a whole between a reverse-video color-block title `## Prompt` (white text on blue background; `## Prompt ({attempt_label})` on attempt retry) and a matching fixed-width reverse-video blue color-block bottom boundary, with each segment's title and content rendered left-aligned within the section, no longer drawing a Rich `Panel` border, Rule, or horizontal line; the assistant response likewise uses a reverse-video color-block title `## Response` (white text on green background, with an attempt label appended on retry) + content body (the final text `Markdown` or, in verbose mode, `Text(_render_ndjson_for_human(...))`) + a reverse-video green color-block bottom boundary + a trailing blank line for display. The colors follow the previous `border_style` semantics (prompt → blue, response → green); the visual specification is provided uniformly by the **Block Rendering Visual Style** requirement.
- **AND** the assistant response displays only the final text block (extracted via `_extract_final_text()` of the last `type: "text"` content block in the last `type: "assistant"` message)
- **AND** if there is no text content but there is tool activity, fall back to `_render_ndjson_for_human()` to display a tool activity summary
- **AND** group by attempt, with multiple attempts displayed separately and labeled with sequence numbers

#### Scenario: Structured detailed rendering (verbose)
- **WHEN** `render_session_detailed(session, verbose=True)` is called
- **THEN** the prompt display is the same as in non-verbose mode (structured segmentation)
- **AND** the response reuses `_render_ndjson_for_human()` to display the full conversation flow, including text content and tool call/result summaries
- **AND** in verbose mode the response is rendered with `Text()` rather than `Markdown()`, to avoid bracket formatting (such as `[Edit: file.py]`) being misparsed as Rich markup

#### Scenario: Detailed JSON output
- **WHEN** `get_detailed_json(project_root, flow_id)` is called
- **THEN** return a structured array, each element containing `step_id`, `step_type`, and `messages`
- **AND** the user message contains `segments` (the `segment_prompt()` segmentation result) and the original `content`
- **AND** the assistant message contains `content` (the extracted text) and `raw_json` (the original NDJSON data)

#### Scenario: Fix-loop implement iterations rendered as virtual per-iteration sessions
- **GIVEN** the state machine re-uses the same `implement` Step object across fix loop iterations (resetting `status=PENDING` rather than allocating a new step)
- **AND** this causes multi-iteration implement prompts to accumulate in a single on-disk history file (`se3/history/{flow_id}/04_implement_{id}.jsonl`), while each `test` / `self_check` iteration writes to its own file (05, 07, 09, 11, …)
- **WHEN** the display layer reads history for a flow via `interleave_sessions_for_display(sessions)` (used by both the Rich renderer in `history_cmd._show_detailed_sessions` and the programmatic `chat_history.get_detailed_json`)
- **THEN** each `implement` ChatSession is passed to `split_implement_session_by_iterations(session, test_timestamps)`, which partitions its messages into virtual per-iteration sessions using the first-message `timestamp` of each `test` session as a fence (iter1 = messages before the first test; iter{i+1} = messages between test[i-1] and test[i])
- **AND** each virtual session's `step_id` is the original implement step_id with a `-iter{N}` suffix appended (N starts at 1), for display titling only
- **AND** when the implement session has only one iteration (≤1 bucket), it is returned unchanged (no `-iter1` suffix injected) so non-fix-loop flows are unaffected
- **AND** all sessions (virtual implement splits plus untouched test/self_check/other sessions) are stable-sorted by first-message timestamp (tiebreaker: `step_id`), producing a chronological `implement-iter1 → test-1 → self_check-1 → implement-iter2 → test-2 → …` timeline
- **AND** on-disk history files are NOT rewritten, renamed, or split — this is a pure render-layer transformation, preserving backward compatibility with existing `engine.json` / `se3/history/` directories
- **NOTE** The underlying state-machine step-reuse behavior (`_transition_to_fix`) and the persistence file-naming convention are intentionally unchanged; the virtual split lives entirely in `chat_history` helpers consumed only by display paths

### Requirement: LLM Content Truncation Strategy

The flow engine SHALL apply content-aware truncation when feeding diagnostic output (stderr, stdout, tool results, step summaries) into LLM prompts, to maximize the LLM's ability to diagnose and fix issues within token constraints.

**Truncation Direction Policy:**
- **Error content** (stderr, error tool_results): tail-truncate (`content[-N:]`) — error root causes and tracebacks appear at the end
- **Non-error content** (stdout, normal tool_results): head-truncate (`content[:N]`) — context and setup appear at the start
- **Assistant responses** in retry/continue context: head+tail truncate (head 1000 chars + tail for remainder) — preserves initial step instructions and schema definitions at the start, plus final conclusions and tool results at the end
- **User prompts** in retry/continue context: not truncated per-entry (line-level deduplication controls size); a post-dedup whole-prompt safety cap prevents unbounded growth after deduplication runs

**Minimum Truncation Limits for LLM-consumed content:**

| Context | Content Type | Min Chars | Direction |
|---------|-------------|-----------|-----------|
| Fix instructions (test, verify_spec) | stderr | 2000 | tail |
| Fix instructions (test, verify_spec) | failures section | 3000 | smart (per test block) |
| LLM prompt test results (verify_spec, self_check) | stderr per phase | 2000 | tail |
| LLM prompt test results (verify_spec, self_check) | stdout per phase | 2000 | tail |
| Chat history tool_result | content | 2000 | direction-aware |
| Chat history retry/continue | assistant response | 2000/4000 | head+tail (head 1000 + tail remainder) |
| JSON retry prompt (LLM caller) | previous response | 1500 | head |
| Test history record | stderr per phase | 2000 | tail |
| Test history record | stdout per phase | 2000 | tail |
| Test history record (passed phase) | result summary tail | `TEST_HISTORY_PASSED_SUMMARY_TAIL_CHARS` | tail |
| Context.json step output values | string values | 1000 | head |
| Salvage diff summary | git diff | 4000 | head |
| self_check prompt task_groups summary | plan task_groups markdown | 2000 | head |

**Shared Truncation Constants Module:**

Truncation limits consumed by step handlers (test, self_check, verify_spec) SHALL be defined as named constants in a shared `truncation.py` module (`se3/engine/truncation.py`), rather than hardcoded in each handler. This ensures consistency across handlers and provides a single location to adjust limits. Constants include `PHASE_STDOUT_TAIL_CHARS`, `PHASE_STDERR_TAIL_CHARS`, `TEST_HISTORY_STDOUT_TAIL_CHARS`, `TEST_HISTORY_STDERR_TAIL_CHARS`, `TEST_HISTORY_PASSED_SUMMARY_TAIL_CHARS` (the tail limit for the slimmed result summary archived for a `passed: true` test phase; see *Test Step Configuration and Multi-Phase Execution*), `FIX_STDERR_TAIL_CHARS`, `FAILURES_SECTION_MAX_CHARS`, and `SELF_CHECK_TASK_GROUPS_MAX_CHARS`.

**Design rationale:** Stderr is the primary source of traceback and error diagnostics for LLM-driven fix loops. Previous limits (300-500 chars) were insufficient for a single Python traceback. The limits above ensure at least one complete error chain is preserved in all diagnostic contexts.

#### Scenario: Error content uses tail truncation
- **WHEN** the system truncates stderr or error tool_result content for LLM consumption
- **THEN** tail truncation (`content[-N:]`) is used to preserve the error root cause

#### Scenario: Assistant response uses head+tail truncation in retry context
- **WHEN** `format_history_for_retry()` truncates a previous assistant response
- **THEN** head+tail truncation is used: head (1000 chars) preserves step instructions and schema definitions, tail (remainder of budget) preserves final conclusions and tool results
- **AND** user prompts are preserved in full by `format_history_for_retry()` — no per-entry character cap is applied; repeated content is handled by `deduplicate_prompt_lines()` in LLMCaller, and a separate post-dedup whole-prompt safety cap (see Prompt Line-Level Deduplication) provides bounded-growth fallback

#### Scenario: Truncation constants are centralized
- **WHEN** a step handler (test, self_check, verify_spec) truncates stdout or stderr content
- **THEN** the truncation limit is imported from the shared `truncation.py` module
- **AND** all handlers sharing the same truncation context use the same constant value

### Requirement: Prompt Line-Level Deduplication

The flow engine SHALL deduplicate repeated contiguous line blocks within a prompt before sending it to the LLM, to reclaim context window space wasted by spec content repeated across retry attempts.

**Deduplication Rules:**

- **Exact match**: Only lines that are character-for-character identical qualify — no fuzzy matching or lossy compression
- **Contiguous blocks**: Only contiguous blocks of >= `min_block_lines` (default 3) identical lines are deduplicated
- **First-occurrence preserved**: The first occurrence of a block in the prompt is kept verbatim; subsequent occurrences are replaced with a marker of the form `[DUPLICATED CONTENT: N lines #HASH, from "FIRST_LINE" to "LAST_LINE"]`, where HASH is a short content hash for disambiguation
- **Per-call independence**: Deduplication is performed independently for each LLM call; no cross-call state is maintained
- **Pure string operation**: The function operates on raw line text without understanding prompt structure or semantics
- **Blank-line exclusion**: Blocks consisting entirely of blank lines are not deduplicated
- **Marker passthrough**: Lines that are existing dedup markers (from prior dedup passes) are skipped during matching

**Integration Point:**

- The `deduplicate_prompt_lines(prompt, min_block_lines=3)` function is defined in an independent module (`se3/engine/prompt_dedup.py`), decoupled from retry logic, chat history, or any specific step handler
- Called in `LLMCaller._call_with_retry()` after `effective_prompt` is fully assembled (including retry context, extra prompt, read-only constraints) and before subprocess arguments are built
- Only applied on retries (`is_retry=True`); first calls have no internal repetition by definition
- `_record_prompt` records the deduped prompt, keeping history consistent with actual LLM input
- Failures in deduplication are caught and logged as warnings; the original prompt is used as fallback

**Post-Dedup Whole-Prompt Safety Cap:**

- After `deduplicate_prompt_lines()` runs, `LLMCaller._call_with_retry()` applies a single whole-prompt length check against `POST_DEDUP_SAFETY_LIMIT` (default 500,000 characters; env-overridable via the `SE3_POST_DEDUP_SAFETY_LIMIT` environment variable for testing and operational tuning — values that fail to parse as a positive integer fall back to the default)
- The cap operates on the deduped `effective_prompt` as a whole, NOT on each history entry individually — this ensures repeated spec content is collapsed by dedup before the cap decides whether truncation is necessary
- When the deduped prompt exceeds the cap, the cap locates the retry-history region using shared marker/separator constants (defined in a neutral module `se3/engine/retry_context.py` and consumed by both `chat_history.py` and `llm_caller.py`) via a trailing `rfind` of the separator (robust to retry-of-retry replays where multiple history regions may appear), and truncates the **head of the history region** while preserving the **new prompt tail** in full — the semantically most important portion for the model's current task
- Kept body is rounded to line boundaries to avoid mid-line cuts
- Distinct warnings are emitted for each observable branch: no-op (under limit), normal trigger (history head trimmed), tail-alone exceeds the limit, and separator-missing (defensive fallback)
- Shared constants `RETRY_HISTORY_MARKER`, `RETRY_HISTORY_SEPARATOR`, and `POST_DEDUP_SAFETY_LIMIT` live in `retry_context.py` so that `chat_history.format_history_for_retry()` and `llm_caller._call_with_retry()` reference the same symbols without cross-module underscore-prefixed imports

**Design rationale:** Character-count truncation of individual user prompts (the prior approach) discarded unique content that appears after repeated spec blocks (e.g., task-specific instructions following embedded specs), and — critically — fired *before* `deduplicate_prompt_lines()` had a chance to collapse those repeated specs, so content that would have been removed as duplicates was instead prematurely truncated. Line-level deduplication is a lossless alternative: it removes only provably identical content while preserving all unique portions of the prompt. The post-dedup whole-prompt cap serves as the bounded-growth fallback for the degenerate case where dedup is ineffective and the prompt is genuinely unbounded — it runs after dedup, measures the whole prompt, and preserves the current-task tail.

#### Scenario: First LLM call is a no-op
- **WHEN** `_call_with_retry()` makes the first call for a step (not a retry)
- **THEN** `deduplicate_prompt_lines()` is not invoked
- **AND** the prompt is passed to the LLM unchanged

#### Scenario: Retry deduplicates repeated spec content
- **WHEN** `_call_with_retry()` retries an LLM call and `effective_prompt` contains spec content repeated across retry context entries
- **THEN** `deduplicate_prompt_lines()` replaces subsequent occurrences of repeated blocks with dedup markers
- **AND** the first occurrence of each block is preserved verbatim
- **AND** all unique content (task instructions, error diagnostics) is preserved regardless of position

#### Scenario: Recorded prompt matches LLM input
- **WHEN** `_record_prompt()` saves the prompt to history after a retry call
- **THEN** the recorded prompt is the deduped version (same as what the LLM received)

#### Scenario: Post-dedup cap is a no-op when dedup reclaims space
- **GIVEN** a retry where prior user prompts contain large spec blocks that are also present in the new prompt
- **WHEN** `_call_with_retry()` runs `deduplicate_prompt_lines()` and then the post-dedup whole-prompt cap
- **THEN** the deduped prompt is below `POST_DEDUP_SAFETY_LIMIT`
- **AND** no truncation is performed and no `User prompt in retry context hit safety limit` style warning is emitted

#### Scenario: Post-dedup cap trims history head when prompt genuinely exceeds limit
- **GIVEN** a retry where prior user prompts contain large non-repeating content that dedup cannot collapse
- **AND** the deduped `effective_prompt` length still exceeds `POST_DEDUP_SAFETY_LIMIT`
- **WHEN** the post-dedup cap runs
- **THEN** the retry-history region is located via trailing `rfind` of the shared separator (robust to retry-of-retry replays)
- **AND** the **head** of the history region is truncated while the **new prompt tail** is preserved in full
- **AND** the kept body boundary is rounded to a line boundary (no mid-line cuts)
- **AND** a warning is logged describing that the cap was triggered

### Requirement: State Persistence and Recovery

The flow engine SHALL persist the run state to a JSON file (`se3/state/engine.json`), supporting precise recovery after interruption at any step.

**Persisted content:**
- Flow instance metadata (flow_id, task_description, task_type, status)
- Current step state (current_step_id, current_step_index)
- Selected step sequence (selected_steps)
- All step history (step_history, steps)
- Input/output of each step

**Atomic write:**
- Write to a temporary file first, then rename to the target path
- Avoid state file corruption caused by interruption during writing

#### Scenario: Interruption recovery
- **WHEN** the flow is interrupted during execution of a step (ctrl-c, process termination, system crash)
- **AND** the user re-runs `se3 run`
- **THEN** the flow engine recovers from the JSON state file to the step before the interruption
- **AND** prompts the user with the currently recovered position and context

#### Scenario: Ctrl+C interruption injection
- **WHEN** the user enters an additional instruction during the interruption
- **THEN** inject the instruction into the LLM prompt of the current step
- **AND** re-execute the current step

#### Scenario: Step outputs JSON serializability
- **WHEN** a step handler stores a result in `step.outputs`
- **THEN** the value MUST be a JSON-serializable primitive (string, number, bool, dict, list, or null)
- **AND** enum values (e.g. `StepStatus`) MUST be converted to their string `.value` before storing
- **AND** `json.dumps` calls that serialize step outputs SHOULD use `default=str` as a defensive fallback, consistent with `persistence.py`

### Requirement: Inter-Step Input Passing

The flow engine SHALL automatically construct step inputs, passing the outputs of preceding steps to subsequent steps.

**Input construction rules:**
- All steps receive `task_description` and `flow_id`
- `analyze` outputs `task_type`, `scope`, `complexity`, `reasoning`, `project_summary`, `relevant_specs`, `spec_content`; among these, `project_summary` is generated programmatically by `ProjectContextCollector.collect()` (non-LLM), and `spec_content` is loaded programmatically by post-processing (base spec auto-attached + LLM-selected spec items)
- `plan` receives `spec_content` (from analyze), `task_type`, `scope`, `project_summary` (from analyze), and outputs `plan` (containing proposal + design), `task_groups`, and `spec_changes` (full depth only)
- `implement` receives `design_doc` (mapped from plan.design), `task_groups`, `spec_content` (from analyze), `project_summary` (from analyze)
- `self_check` receives `test_results` (from test), `changes_made` (from implement), `spec_content` (from analyze), `task_groups` (from plan, used as the scope reference for the "feature omission" dimension), `fix_iteration` (the current fix loop iteration count), `self_check_pass_index` (the 1..N sequence number within the current fix-loop round), `self_check_passes_required` (from `workflow.self_check_passes_required`), `self_check_convergence_enabled` (from `workflow.self_check_convergence_enabled`, default false), `prev_self_check_issues` (injected only when `convergence_enabled=true` and `pass_index=1`, carrying the issues from the self_check at the end of the previous fix-loop round as a convergence comparison baseline)
- `verify_spec` receives `changes_made`, `spec_content` (from analyze), `test_results`, `fix_iteration`, `spec_changes` (passed from the plan step, used to distinguish intentional changes from regressions), and `relevant_specs` (from analyze)
- `update_spec` receives `changes_made`, `verification_result`, `spec_changes` (passed from the plan step, serving as a change guidance checklist), `design_doc` (mapped from plan.design, providing architectural context); by default it loads the full text of all specs in `full_spec` mode, supporting naming deduplication and cross-spec consistency checks
- `version_analyze` receives `changes_made`, `summary`, `verification_result`, `task_type`
- `commit` receives `changes_made`, `commit_message` (from version_analyze), `bump_type` (from version_analyze), `proposal` (from plan, used by the commit message fallback chain), `updated_specs` (from update_spec, so that spec changes can be committed together with code changes at commit time)
- `summarize` receives all preceding outputs (when included in step sequence)

#### Scenario: Automatic step input construction
- **WHEN** the flow transitions to a new step
- **THEN** construct the step input automatically according to the rules
- **AND** include all relevant preceding outputs

#### Scenario: update_spec loads in full_spec mode by default
- **WHEN** state_machine constructs the input for update_spec
- **AND** `se3.yaml` does not explicitly configure `spec_loading.steps.update_spec`
- **THEN** the `spec_content` of `update_spec` contains the complete text of all relevant specs (not item excerpts)
- **AND** the LLM can determine naming conflicts and cross-spec consistency based on the complete content

#### Scenario: update_spec creates a new spec after applying the new-spec criteria
- **GIVEN** the update_spec step loads in full_spec mode and sees all existing specs
- **WHEN** the implementation introduces a new subsystem (e.g., Issue Discovery)
- **THEN** the update_spec LLM explicitly answers the 4 criteria before appending a new Requirement
- **AND** when the criteria result points to new_spec, create a new spec directory and `spec.md` under `se3/specs/`
- **AND** the new spec file is created at `se3/specs/issue-discovery/spec.md`

### Requirement: Version Analyze Step

The `version_analyze` step SHALL use the LLM to intelligently analyze the actual change content and determine the version change type according to the Semantic Versioning 2.0.0 rules.

**Analysis input:**
- `changes_made`: The list of changed files and detailed descriptions
- `summary`: The work summary generated by the preceding step
- `verification_result`: The consistency check result against the spec
- `task_type`: The task type (used as a reference, not as a determining factor)

**Analysis output:**
```json
{
  "bump_type": "major|minor|patch|none",
  "reasoning": "Detailed explanation based on SemVer 2.0.0",
  "confidence": "high|medium|low",
  "suggested_version": "X.Y.Z",
  "commit_message": "Concise imperative commit summary (max 72 chars)"
}
```

**Authoritative field:** `suggested_version` is the authoritative value used directly by the commit step when writing the version file. `bump_type`, `reasoning`, and `confidence` serve only as display/commit message auxiliary fields and are no longer used to derive the new version number from `current_version + bump_type`. When `version_analyze` fails or does not output `suggested_version`, the commit step raises an error and aborts the flow (see the Commit Step Version Management requirement).

**commit_message generation rules:**
- Use the imperative mood (e.g. "Add feature" rather than "Added feature")
- Start with a verb, describing the work actually completed
- At most 72 characters
- Do not include a task type prefix (e.g. "feat:" or "fix:") — the prefix is added automatically by the commit step
- When version_analyze fails to provide a commit_message, the commit step falls back by priority: proposal summary → implement_summary → task description template

**Decision rules:**
- **MAJOR**: Incompatible API changes, removed functionality, breaking behavior changes
- **MINOR**: Backward-compatible new features, newly added optional parameters, feature enhancements
- **PATCH**: Backward-compatible bug fixes, performance optimizations, internal refactoring
- **NONE**: Changes with no versioning value (formatting only, comments, etc.)

**Verification result formatting:**
- When `verification_result` includes an `issues` list, all issues SHALL be included in the LLM prompt (no display cap).
- Issue severity is read from the `priority` field (matching verify_spec's unified priority system: `critical/high/medium/low`), not the `severity` field.
- The summary counts critical/high priority issues as the primary indicator of unresolved problems.

#### Scenario: Intelligent version analysis identifies breaking changes
- **GIVEN** the task type is `small`
- **AND** the actual changes removed a parameter of a public function
- **WHEN** the `version_analyze` step executes
- **THEN** the LLM identifies it as a breaking change
- **AND** returns `bump_type: major`

#### Scenario: Version analyze shows all verification issues
- **GIVEN** `verification_result` contains 15 issues of varying priority
- **WHEN** `version_analyze` formats the verification result for the LLM prompt
- **THEN** all 15 issues are included (no truncation to a fixed count)
- **AND** the summary line uses the `priority` field to count critical/high issues

#### Scenario: Low confidence handling
- **GIVEN** `version_analyze` returns `confidence: low`
- **AND** `auto_bump: true` (default)
- **WHEN** entering the commit step
- **THEN** the system still directly adopts `suggested_version` to write the version file
- **AND** records a low-confidence warning log

### Requirement: Commit Step Version Management

The `commit` step SHALL integrate automatic version update functionality, automatically bumping the version number based on the result of `version_analyze`, and updating related documentation. The commit step does not include an independent LLM call—the commit message comes from the `version_analyze` step.

**Commit Message Priority Chain:**
1. `commit_message` from `version_analyze` step (via `step.inputs`)
2. `proposal.summary` from plan step (fallback)
3. `implement_summary` from implement step (fallback)
4. Template from task description (last resort)

The commit step prepends the `task_type` prefix (e.g., `feature:`, `bugfix:`) to the message automatically.

**Template Summary Generation:**
- When the `summarize` step is NOT in the flow's step sequence, the commit step generates a template-based summary document at `se3/state/summary-{flow_id}.md`
- The template uses structured data from the flow state: commit message, changed files, test results, version info
- When the `version_analyze` step's `reasoning` field is available (non-empty) in `step.inputs`, the template includes a `### Version Analysis` section after the Version line and before the Commit Message section, containing the full reasoning text. Only the `reasoning` field is included — `bump_type` and `confidence` are not repeated here (version info is already shown in the Version line). If `reasoning` is absent or empty, this section is omitted.
- No LLM call is needed — this is a deterministic template operation
- When the `summarize` step IS in the sequence, it generates a richer LLM-based summary (existing behavior)

**Version Update Flow:**
1. Detect the project type (Python/Node.js) and locate the version file (pyproject.toml/package.json)
2. Read **`suggested_version`** (the authoritative field) from the `version_analyze` step
3. If `version_analyze` fails or does not output `suggested_version`, the commit step reports an error and aborts the flow (carrying current_version and a manual-intervention prompt), no longer performing a default bump as a fallback. These two failure modes each throw an independent `RuntimeError`, with distinct error messages to aid diagnosis:
   - The final state of the `version_analyze` step is `FAILED`: the error message begins with `"version_analyze step failed; cannot determine target version"`, indicating that the upstream step itself failed (e.g., LLM call or parsing failure), and even if residual outputs exist, they are not trusted as the authoritative `suggested_version`
   - The `version_analyze` step did not fail but did not produce a valid `suggested_version` (missing, non-string, or empty string): the error message begins with `"version_analyze did not produce a suggested_version"`, indicating that the step nominally completed but did not fulfill the contract for the authoritative version field
   - Both errors carry `current_version` (from `version_analyze.outputs.current_version`, falling back to `step.inputs.current_version`, then falling back to `"<unknown>"`) along with unified manual-intervention guidance: rerun `version_analyze` or create a human call under `se3/calls/` to manually provide the version number
4. Write `suggested_version` into the version file as-is (atomic write + backup for rollback)
5. Automatically update README.md and VERSIONS.md (if templates are configured)
6. Commit the version file and documentation changes together

`bump_type` no longer participates in computing the new version number, serving only as an auxiliary field for the commit message / rendering layer.

**Version Rollback Mechanism:**
- If the commit fails, automatically roll back the version file to the original version
- After a successful commit, clear the backup so the version change takes effect permanently

**Configuration Options (se3.yaml):**
```yaml
version:
  enabled: true                    # Enable automatic version updates
  file_path: null                  # Version file path (null=auto-detect)
  include_in_commit_message: true  # Include the version number in the commit message
  auto_bump: true                  # Automatically apply suggested_version (no confirmation needed)
  confidence_threshold: null       # Confidence threshold (null=always automatic)
  script_path: null                # Custom version script path
  auto_generate_script: true       # Auto-generate the version script when missing

  # Documentation update templates
  templates:
    readme_badge: "![Version](https://img.shields.io/badge/version-{version}-blue)"
    versions_entry: "## {version} - {date}\n\n{changes}\n"
```

The legacy configuration items in the `version` section that statically map bump_type by task_type, along with the master switch field for intelligent analysis, are deprecated; if retained in `se3.yaml`, they will also be silently ignored by the loader and no longer affect the version flow. Project-level customization of version rules is now carried by the optional `se3/version-rules.md` natural-language file (see the `se3-versioning` *Custom Version Rules File* requirement).

#### Scenario: Feature Task Automatically Updates Version
- **GIVEN** the current version is 1.2.3
- **AND** `version_analyze` returns `suggested_version: 1.3.0`
- **WHEN** the commit step executes
- **THEN** the version file is written with `1.3.0` (directly adopting `suggested_version`)
- **AND** README.md and VERSIONS.md are automatically updated
- **AND** all changes are committed together

#### Scenario: Bugfix Task Automatically Updates Version
- **GIVEN** the current version is 1.2.3
- **AND** `version_analyze` returns `suggested_version: 1.2.4`
- **WHEN** the commit step of a bugfix-type task executes
- **THEN** the version file is written with `1.2.4`
- **AND** the commit message includes the new version number

#### Scenario: Version Update Failure Rollback
- **GIVEN** the version has been successfully bumped but the commit failed
- **WHEN** the commit step detects a commit error
- **THEN** automatically roll back the version file to the original version
- **AND** report the error message

#### Scenario: Commit Reports an Error and Aborts When suggested_version Is Missing
- **GIVEN** the `version_analyze` step state is not FAILED, but there is no `suggested_version` in the outputs (missing, non-string, or empty string)
- **WHEN** the commit step is triggered
- **THEN** the commit step throws a `RuntimeError`, with the error message beginning with `"version_analyze did not produce a suggested_version"` and including the current version and manual-intervention guidance
- **AND** no silent patch bump fallback is performed
- **AND** the flow is aborted, awaiting the user to rerun `version_analyze`, revise `se3/version-rules.md`, or provide the version number via the existing manual-intervention mechanism

#### Scenario: Commit Throws an Independent Error When the version_analyze Step Is FAILED
- **GIVEN** the final state of the most recent `version_analyze` step is `FAILED` (e.g., LLM call failure or parsing failure)
- **WHEN** the commit step is triggered
- **THEN** the commit step throws a `RuntimeError`, with the error message beginning with `"version_analyze step failed; cannot determine target version"` (clearly distinguished from the error message of the "did not produce suggested_version" scenario)
- **AND** even if a `suggested_version` field remains in the outputs of the FAILED step, the commit step will not adopt it as the authoritative value
- **AND** the error message includes the current version (`current_version`, with the fallback chain `version_analyze.outputs.current_version` → `step.inputs.current_version` → `"<unknown>"`) and unified manual-intervention guidance
- **AND** the flow is aborted, awaiting the user to rerun `version_analyze` or to manually provide the version number via a human call under `se3/calls/`

### Requirement: Error Handling and Retry

The flow engine SHALL provide error handling and retry mechanisms.

**Error handling strategy:**
- Automatically retry a step when it fails (up to 3 times)
- After exceeding the retry count, prompt the user: retry, skip, or abort
- The user can choose to skip the failed step and continue execution

#### Scenario: Step failure retry
- **WHEN** a step fails to execute
- **THEN** automatically retry that step
- **AND** prompt the user after reaching the maximum retry count

#### Scenario: Skip a failed step
- **WHEN** the user chooses to skip the failed step
- **THEN** mark the step as completed
- **AND** continue executing subsequent steps

### Requirement: Step Status Lifecycle

The `StepStatus` enum (`se3/engine/models.py`) SHALL define the complete set of states a `Step` instance may occupy during a flow's execution. The full set comprises:

| Status | Value | Semantics |
|--------|-------|-----------|
| `PENDING` | `pending` | Step has not started yet |
| `RUNNING` | `running` | Step is currently executing |
| `COMPLETED` | `completed` | Step finished successfully |
| `PARTIAL` | `partial` | Step partially completed due to an unrecoverable constraint (e.g., permission restrictions) that the retry mechanism cannot resolve. **Distinct from `FAILED`**: `PARTIAL` is a terminal status for the current attempt — it does NOT trigger the standard retry loop and does NOT count as a successful completion either. The flow may continue with the partial outputs available, or escalate based on downstream consumers' tolerance. |
| `FAILED` | `failed` | Step failed after exhausting retries |
| `RETRYING` | `retrying` | Step is currently being retried after a transient failure |
| `PAUSED` | `paused` | Step is paused awaiting external input (user response, human MCP call response, programmatic confirmation, etc.) |
| `REVISION_NEEDED` | `revision_needed` | Step requested that the flow revise a prior step (e.g., self_check / verify_spec found issues, or a CONFIRM reviewer rejected the reviewed step). The state machine routes back to the appropriate prior step rather than advancing forward. |

**Persistence and JSON-serialization:** Step status values are stored in `engine.json` as their string `.value` (lowercase identifier). When a step handler stores its own status into `step.outputs` (e.g., as `step.outputs["result"]`), it MUST convert the enum to its `.value` before storing, consistent with the **Step outputs JSON serializability** scenario.

#### Scenario: PARTIAL is distinct from FAILED
- **WHEN** a step encounters an unrecoverable permission constraint (or analogous condition) that prevents full completion but does not warrant a retry
- **THEN** the handler SHALL set the step's status to `PARTIAL` rather than `FAILED`
- **AND** the standard retry loop is NOT triggered for `PARTIAL`
- **AND** the step is NOT treated as a successful `COMPLETED` either — downstream consumers that depend on full outputs may degrade or escalate accordingly

#### Scenario: All StepStatus values round-trip through persistence
- **GIVEN** a step in any of `PENDING`, `RUNNING`, `COMPLETED`, `PARTIAL`, `FAILED`, `RETRYING`, `PAUSED`, `REVISION_NEEDED`
- **WHEN** the flow is persisted to `engine.json` and later loaded
- **THEN** the loaded step's status equals the original enum value
- **AND** serialized values use the lowercase string form defined in the enum

### Requirement: Flow Status Lifecycle

The `FlowStatus` enum (`se3/engine/models.py`, mirrored in `se3/engine/schema.py`) SHALL define the complete set of overall states a `FlowInstance` may occupy. The full set comprises:

| Status | Value | Semantics |
|--------|-------|-----------|
| `INIT` | `init` | Flow has just been created; no step has started executing yet |
| `RUNNING` | `running` | Flow is actively executing steps |
| `PAUSED` | `paused` | Flow is paused awaiting user input, a programmatic confirmation, or a human MCP call response |
| `COMPLETED` | `completed` | All selected steps finished successfully |
| `FAILED` | `failed` | Flow encountered an unrecoverable condition (e.g., fix-loop exhaustion) and cannot continue |
| `RECOVERING` | `recovering` | Reserved state used while the engine is attempting to recover a flow from a prior interruption (e.g., loading state mid-resume). Persisted flows MAY round-trip through this state; downstream tooling SHALL accept it as a valid status value. |

**Persistence and JSON-serialization:** Flow status values are stored in `engine.json` as their string `.value` (lowercase identifier), and the loader SHALL accept every value listed above without raising. Forward compatibility: tooling that consumes `engine.json` SHALL treat `RECOVERING` as a transient state and not assume it is one of `INIT/RUNNING/PAUSED/COMPLETED/FAILED`.

#### Scenario: All FlowStatus values round-trip through persistence
- **GIVEN** a flow in any of `INIT`, `RUNNING`, `PAUSED`, `COMPLETED`, `FAILED`, `RECOVERING`
- **WHEN** the flow is persisted to `engine.json` and later loaded
- **THEN** the loaded flow's status equals the original enum value
- **AND** serialized values use the lowercase string form defined in the enum

#### Scenario: RECOVERING is accepted by deserialization
- **GIVEN** an `engine.json` whose top-level `status` field is `"recovering"`
- **WHEN** the engine loads the file
- **THEN** the flow is reconstructed with `status == FlowStatus.RECOVERING`
- **AND** no exception is raised for an unrecognized status value

### Requirement: plan estimated_loc Output

The `plan` step SHALL include an `estimated_loc` field (integer) in each task within task_groups, representing the estimated number of lines of code to be added or modified. This field is used by the `implement` step to decide execution strategy.

**Task output fields (in addition to existing fields):**
- `estimated_loc`: Integer — estimated lines of code. Used as an objective, quantitative measure.

The `complexity` field is preserved unchanged. `estimated_loc` is additive and does not replace any existing field.

#### Scenario: Task includes estimated_loc
- **WHEN** `plan` produces task_groups
- **THEN** each task includes an `estimated_loc` integer field
- **AND** the `complexity` field remains unchanged

### Requirement: Plan spec_changes Output

The `plan` step at full depth (task_type `feature` or `discovery`) SHALL output a `spec_changes` array declaring expected spec modifications. At medium and shallow depths (bugfix, directive, small), `spec_changes` is omitted from the JSON schema and defaults to an empty array.

**spec_changes entry schema:**
```json
{
    "spec_name": "flow-engine",
    "change_type": "add_requirement|modify_requirement|add_scenario|deprecate_requirement",
    "target": "Requirement: Example Requirement Name",
    "description": "What this change entails",
    "rationale": "Why this change is needed"
}
```

**Prompt composition:**
- At full depth, the plan prompt includes a "Spec Changes Declaration" section instructing the LLM to analyze the gap between current specifications and the planned implementation.
- The JSON schema at full depth includes the `spec_changes` array.
- The plan handler extracts `spec_changes` from the LLM response via `result.get("spec_changes", [])` and stores it in `step.outputs["spec_changes"]`.
- The state machine forwards `spec_changes` to downstream steps (`verify_spec`, `update_spec`) via `_build_step_inputs`.

**Data flow:**
- `plan` → `verify_spec`: `spec_changes` allows verify_spec to distinguish intentional deviations from regressions.
- `plan` → `update_spec`: `spec_changes` serves as a guided checklist for spec updates.

#### Scenario: Full-depth plan includes spec_changes
- **WHEN** the plan step executes at full depth (feature or discovery task)
- **THEN** the LLM prompt includes the "Spec Changes Declaration" section
- **AND** the output JSON schema includes the `spec_changes` array
- **AND** `step.outputs["spec_changes"]` contains the declared changes (may be empty)

#### Scenario: Non-full-depth plan omits spec_changes from schema
- **WHEN** the plan step executes at medium or shallow depth (bugfix, directive, small)
- **THEN** the LLM prompt does not include the "Spec Changes Declaration" section
- **AND** `step.outputs["spec_changes"]` defaults to an empty array

#### Scenario: spec_changes forwarded to downstream steps
- **WHEN** the state machine builds inputs for `verify_spec` or `update_spec`
- **THEN** `spec_changes` from the plan step is included in the inputs
- **AND** if no plan step completed or spec_changes was not produced, defaults to an empty array

### Requirement: verify_spec Unified Priority and Scope Mechanism

The `verify_spec` step SHALL use the unified issue priority system (`critical/high/medium/low`) and a scope dimension (`in_scope/out_of_scope`) to classify issues found during verification.

**Priority Levels (unified with issue system):**
- `critical`: Core functionality broken, data loss/corruption possible, security vulnerabilities introduced
- `high`: Requirement not met, specified behavior incorrect, or tests fail due to implementation bugs
- `medium`: Partial implementation gap, missing edge case handling, non-critical deviation from spec
- `low`: Minor style issues, documentation gaps, suggestions for improvement that don't affect correctness

**Scope Dimension:**
- `in_scope`: Issue directly introduced by current task's implementation, or current task claims to address it but has not. Blocks current flow and must be fixed.
- `out_of_scope`: Pre-existing problem discovered during verification, or relates to functionality outside current task boundaries. **Logged (留痕) via `_log_out_of_scope_issues`, NOT filed as an issue** (to avoid issue explosion across fix iterations); does not block current flow.

**verified Field (Rule-Based Computation):**
- The `verified` field is NOT determined by LLM output — it is computed by code: `verified = (in_scope_count == 0) and tests_passed`
- If the LLM outputs a `verified` field, it is ignored/overridden by the rule-based computation
- This eliminates inconsistency between displayed verification status and actual flow behavior

**Baseline-aligned `tests_passed` — single source of truth with the test step:**

`tests_passed` SHALL consume the **same baseline-based verdict the test step computes** rather than re-deriving a pass/fail from any-red-test. The step reads `test_results["tests_blocking"]` (the authoritative flag `test.py` sets — `True` iff there is an introduced failure, an unparseable failure, or a critical-gate trip) and computes `tests_passed = not tests_blocking`. **Inherited** failures (those in `baseline_failures`) are deliberately excluded from `tests_passed`, so they do **not** drive `REVISION_NEEDED` and do not block a correctly-scoped flow from committing its work. Robust fallbacks (in `_evaluate_test_gate` / `_compute_introduced_failures`) cover older `test_results` shapes: if `tests_blocking` is absent, the step falls back to `introduced_failures`, then recomputes the introduced split from the structured `new_tests`/`regression` lists against the injected `baseline_failures`. Inherited failures are surfaced once (留痕) but do not affect the verdict.

**Honest `tests_passed` — critical acceptance test consumption (defensive gate retained):**

Independently of the baseline verdict, the `tests_passed` input MUST honestly reflect whether **this session's** critical acceptance tests actually ran. The verify_spec step SHALL continue to consume the `test_results["critical_skipped"]` and `test_results["critical_missing"]` signals produced by the test step (see *Test Step Configuration and Multi-Phase Execution*): if **either** list is non-empty, `tests_passed` is forced to `False`, so the authoritative `verified` cannot be polluted by a false-green. This is a defensive double-safety net — even if an upstream branch failed to force the blocking flag, a skipped or missing critical acceptance test still drives `verified` to `False`. When this gate trips, verify_spec returns REVISION_NEEDED and its `fix_instructions` note that a critical acceptance test was skipped or is missing and must be made to truly run.

**REVISION_NEEDED Logic:**
- Triggered when `in_scope_count > 0` (spec compliance issues) OR `tests_passed == False` (introduced test failures or a critical-gate trip — never inherited/baseline failures)
- verify_spec handler always returns REVISION_NEEDED when issues are found (does not check exhaustion internally)
- Exhaustion detection is centralized in `state_machine.transition_to_next()`: when `fix_iteration >= max_fix_iterations` (default 100), the flow is set to FAILED status, an A-class issue is generated, and execution stops
- **Sentinel:** when `max_fix_iterations == 0` (i.e. user configured `0` or `null`, both normalized to `0`), the exhaustion check is skipped entirely — the flow continues to dispatch fix loops indefinitely, prompts/log lines render the iteration as `N (unlimited)` rather than `N of M`, and no A-class fix-loop-exhaustion issue is filed. Negative integers are rejected fail-fast at config load (must be `>= 0`); only an explicit `0`/`null` opts into unlimited mode. The default `100` keeps the bound finite to avoid surprising token consumption; users opt into unlimited mode explicitly.

**Out-of-Scope Issue Logging (not filing):**
- Out-of-scope issues are **logged (留痕) via `_log_out_of_scope_issues`**, recording each issue's description and supplied evidence to the flow log / telemetry. They are **not** filed via `IssueManager.create()`.
- Rationale: a scoped flow that loops re-discovers the same out-of-scope observations every iteration; filing them produced an issue explosion (the historical 189 duplicate issues). Logging keeps the substance visible without ballooning the tracker.
- This replaces the earlier deterministic-filing behavior (`_file_out_of_scope_issues`) for verify_spec; provenance from the git diff is NOT used to auto-classify scope (provenance ≠ relevance).

**Planned Change Awareness:**

The `verify_spec` step SHALL receive `spec_changes` from the plan step and use it to distinguish intentional spec deviations from unintended regressions.

- When `spec_changes` is non-empty, the verify_spec prompt instructs the LLM to treat deviations matching plan-declared changes as intentional (low priority, out_of_scope), not regressions.
- Deviations NOT covered by planned changes are still flagged at their normal priority.
- When `spec_changes` is empty (e.g., bugfix tasks), all deviations from spec are evaluated at their normal priority.

**Prompt section:**
The "Planned Spec Changes" section is formatted into the prompt using `_format_spec_changes()`, which renders each entry as `- [change_type] spec_name :: target` with an optional description line.

#### Scenario: In-scope issue triggers REVISION_NEEDED
- **GIVEN** verify_spec finds an issue classified as `in_scope` with priority `high`
- **THEN** verify_spec returns REVISION_NEEDED
- **AND** `verified` is computed as `False`
- **NOTE** verify_spec no longer checks exhaustion internally; exhaustion is handled by the state machine

#### Scenario: Out-of-scope issue does not block flow
- **GIVEN** verify_spec finds an issue classified as `out_of_scope` with priority `medium`
- **WHEN** no in-scope issues exist and there are no introduced test failures
- **THEN** verify_spec returns COMPLETED
- **AND** `verified` is computed as `True`
- **AND** the out-of-scope issue is **logged via `_log_out_of_scope_issues`, not filed** via `IssueManager.create()`

#### Scenario: Inherited test failures do not block a scoped flow
- **GIVEN** the only failing tests are inherited (present in the frozen `baseline_failures`) and there are no in-scope spec issues
- **WHEN** verify_spec computes the verdict
- **THEN** `tests_passed` is `True` (inherited failures are excluded), `verified` is `True`, and verify_spec returns COMPLETED
- **AND** the inherited failures are surfaced once (留痕) but do not drive `REVISION_NEEDED`, so the correctly-scoped flow can commit its work

#### Scenario: Intentional deviation classified as low/out_of_scope
- **GIVEN** the plan step declared a spec_change: `[add_requirement] flow-engine :: Requirement: New Feature X`
- **AND** the implementation introduces behavior not covered by current specs but matching the declared change
- **WHEN** verify_spec executes
- **THEN** the deviation is classified as low priority, out_of_scope
- **AND** verify_spec does NOT trigger REVISION_NEEDED for this deviation

#### Scenario: Unplanned deviation classified as in_scope
- **GIVEN** the plan step declared no spec_changes (or the deviation does not match any declared change)
- **AND** the implementation deviates from the current spec
- **WHEN** verify_spec executes
- **THEN** the deviation is classified at its normal priority with scope `in_scope`
- **AND** verify_spec may trigger REVISION_NEEDED

#### Scenario: Empty spec_changes preserves existing behavior
- **GIVEN** the plan step produced an empty `spec_changes` array (e.g., bugfix task)
- **WHEN** verify_spec executes
- **THEN** all deviations from spec are evaluated at their normal priority with scope determined by the LLM

#### Scenario: verified field computed by rule
- **GIVEN** LLM outputs `verified: true` but `in_scope_count > 0`
- **WHEN** verify_spec processes the result
- **THEN** `verified` is overridden to `False` by the rule: `(in_scope_count == 0) and tests_passed`

#### Scenario: Critical skip/missing forces tests_passed False
- **GIVEN** `test_results["critical_skipped"]` or `test_results["critical_missing"]` is non-empty (a critical acceptance test was skipped or silently un-collected)
- **WHEN** verify_spec computes `tests_passed`
- **THEN** `tests_passed` is forced to `False` regardless of the pytest returncode
- **AND** `verified` is computed as `False` via `(in_scope_count == 0) and tests_passed`
- **AND** verify_spec returns REVISION_NEEDED with fix_instructions noting the skipped/missing critical acceptance test

### Requirement: update_spec Guided Execution

The `update_spec` step SHALL receive `spec_changes` and `design_doc` from the plan step, shifting from pure inference mode to guided execution when guidance is available.

**Two modes:**
1. **Guided mode** (when `spec_changes` is non-empty): The LLM uses `spec_changes` as a primary checklist, executing each declared change intent (add, modify, deprecate) in the corresponding spec files. `design_doc` provides architectural rationale to produce more accurate and well-motivated spec updates.
2. **Inference mode** (when `spec_changes` is empty): The LLM determines which specs need updating by analyzing the changes made and verification results, as before.

**Prompt composition:**
- The "Spec Change Guidance" section is formatted using `_format_spec_changes()` which renders each entry with spec_name, change_type, target, description, and rationale.
- The "Design Context" section is formatted using `_format_design_doc()` which renders the design overview, components, and architecture decisions.
- The "New Spec Decision" section requires the LLM to evaluate four criteria (conceptual independence, dependency direction, naming test, cross-scenario reusability) before appending any new Requirement. See the `spec-guardrails` spec for the full criteria definition.
- `update_spec` defaults to `full_spec` loading mode so the LLM sees all spec files and can perform naming collision checks.

#### Scenario: Guided spec update with spec_changes
- **GIVEN** the plan step produced non-empty `spec_changes` and a `design_doc`
- **WHEN** update_spec executes
- **THEN** the LLM uses `spec_changes` as the primary checklist for updates
- **AND** the LLM uses `design_doc` to understand architectural rationale
- **AND** spec updates align with the declared change intents

#### Scenario: Inference spec update without guidance
- **GIVEN** the plan step produced empty `spec_changes` (e.g., bugfix task)
- **WHEN** update_spec executes
- **THEN** the LLM infers which specs need updating from changes_made and verification_result
- **AND** update_spec behavior is identical to pre-refactoring

#### Scenario: update_spec enforces new spec decision criteria
- **GIVEN** the implementation introduces a new Requirement that does not fit any existing spec
- **WHEN** update_spec executes
- **THEN** the LLM evaluates the four criteria before deciding to append or create a new spec
- **AND** the JSON output includes `spec_decisions` with `decision: new_spec` and `reasoning`
- **AND** a new spec file is created following the standard structure

### Requirement: Index-First Spec Information Protocol

The spec-consuming LLM steps — `analyze`, `update_spec`, and `verify_spec` — SHALL obtain spec information through the **index-first, fetch-on-demand** protocol built on the `se3 spec` commands (`se3 spec index` → `se3 spec show`), rather than by injecting the full item set or by reading a large `spec.md` file (or the index cache file) in full. Because each of these steps runs as a single `LLMCaller.call()` whose underlying CLI subprocess carries its own tool loop, the step's agent can run `se3 spec index <spec> [<group>...]` and `se3 spec show <spec>::<requirement>` itself, inside that one call, to drill down to the items it needs. `analyze` is a read-only step (Write/Edit are disallowed) but `Bash`/`Read`/`Grep` remain available, so it can run the `se3 spec` commands the same way. The program-injected root view the steps receive is produced by the **same** renderer as `se3 spec index` (no argument), so the injected view and the command output are consistent. The framework's own non-LLM code paths (parsing, index build, items / full_spec program injection, sync, guardrails) still read the `spec.md` files directly; this protocol governs only the LLM-facing retrieval surface.

**`analyze` selection protocol.** The `analyze` step SHALL NOT inject the full item set into its prompt. Instead it injects the root view (same renderer as `se3 spec index`) plus the drill-down protocol description; the agent drills down via `se3 spec index <spec> [<group>...]` to the leaf items within the single call and emits its final `selected_items`. The existing `requirement_name: "*"` (whole-spec select-all) semantics are preserved.

**`update_spec` / `verify_spec` retrieval protocol.** Both steps' prompts instruct the agent to first consult the index (`se3 spec index`) and then fetch only the needed item bodies (`se3 spec show`), and explicitly forbid reading an entire large `spec.md` file or the index cache file to gather context. For modifying an existing item, `update_spec` uses **targeted writing**: it obtains the item's physical location via `se3 spec show`, then does a local `Read` + `Edit` over that line range — never reading the whole large spec file. The `update_spec` New Spec Decision step consumes the **root view** (name + one-line locator), superseding the older bare-names list, and when it creates a new spec it writes that spec's `<!-- domain: <layered/path> -->` header metadata.

**Item identity invariant and exit validation.** An item is a single Requirement; the item identity space is flat and the logical address is always `<spec>::<requirement>`, independent of how the index renders grouping. Rendering group handles (domain groups, pagination) are navigation handles only — they produce no new item identity and are not selectable units. Three mechanical safeguards enforce this: (a) **render distinction** — item entries carry the full `<spec>::<requirement>` address; group handles have no `::` address and carry only the command to take that group; (b) **interface rejection** — `se3 spec show` accepts only an item address and errors on a group name; (c) **exit validation** — the engine code that consumes a step's selection result validates each entry by full `<spec>::<requirement>` address against the flat item set, and a group name or intermediate-node name is a validation failure that is fed back to the LLM to retry. Specifically, `analyze`'s `selected_items` undergo this exit validation after the JSON is parsed: each entry is checked against the flat item set, `requirement_name: "*"` is accepted as whole-spec selection, and any group/intermediate name is rejected with feedback for a retry.

#### Scenario: analyze injects the root view and drills down within one call
- **WHEN** the `analyze` step builds its prompt
- **THEN** the prompt contains the program-injected root view (produced by the same renderer as `se3 spec index`) and the drill-down protocol description, NOT a full dump of every item
- **AND** within the single `LLMCaller.call()` the agent runs `se3 spec index <spec> [<group>...]` to reach leaf items before emitting `selected_items`

#### Scenario: analyze exit validation rejects a group name and retries
- **GIVEN** `analyze` emits a `selected_items` entry that names a group handle or intermediate node rather than a flat `<spec>::<requirement>` item address
- **WHEN** the engine validates the selection against the flat item set
- **THEN** the entry is rejected as a validation failure
- **AND** the error is fed back to the LLM to retry the selection

#### Scenario: analyze preserves the whole-spec select-all semantics
- **GIVEN** `analyze` emits a `selected_items` entry with `requirement_name: "*"` for a spec
- **WHEN** the engine validates the selection
- **THEN** the `"*"` entry is accepted as selecting that spec's whole item set (it is not treated as an invalid non-item address)

#### Scenario: update_spec modifies an existing item via show-then-local-edit
- **GIVEN** `update_spec` needs to modify an existing Requirement in a large spec
- **WHEN** it gathers context for that edit
- **THEN** it resolves the item's physical location via `se3 spec show <spec>::<requirement>`
- **AND** performs a local `Read` + `Edit` over that line range without reading the entire `spec.md` file or the index cache file

#### Scenario: update_spec New Spec Decision consumes the root view and writes domain on creation
- **WHEN** `update_spec` reaches the New Spec Decision step
- **THEN** it consumes the root view (each spec's name + one-line locator), superseding the older bare-names list
- **AND** when it creates a new spec, it writes that spec's `<!-- domain: <layered/path> -->` header metadata

#### Scenario: verify_spec uses index-first retrieval rather than full-file reads
- **WHEN** the `verify_spec` step gathers spec context
- **THEN** its prompt directs it to consult `se3 spec index` and fetch only needed item bodies via `se3 spec show`
- **AND** it is forbidden from reading an entire large `spec.md` file or the index cache file merely to obtain context

### Requirement: Summarize Session Report and Completion Gate

The `summarize` step SHALL be a pure, user-facing session report whose only job is to clearly tell the user what THIS session actually did. It does NOT hunt for new problems, does NOT propose unrelated issues to file, and does NOT pad the report with speculative work that did not happen.

**No B-class issue discovery:**

`summarize` SHALL NOT participate in B-class issue discovery. Both halves are removed:
- The `get_issue_discovery_injection(...)` injection call is removed from `summarize_handler` — the prompt never carries the issue-discovery fragment.
- The `_extract_discovered_issues()` function and the write of `step.outputs["discovered_issues"]` are removed — summarize never produces `discovered_issues`.

Re-adding `summarize` to `issue_discovery.steps` in `se3.yaml` is therefore explicitly **unsupported**: with no injection and no extraction, nothing is collected even if it is configured. The whitelist mechanism itself is retained (default empty) for other steps that can capture `discovered_issues` from their own output (see the issue-discovery spec *Whitelist Configuration* requirement).

**Completion Gate (`verified == False`):**

The summarize step SHALL enforce a downstream completion gate: when the authoritative verification verdict is `verified == False`, the report MUST honestly state that verification did not pass and the work is unverified / unfinished, and MUST NOT describe the work as "complete", "done", "all green", "fully working", or "verified". The gate trips ONLY on an explicit `False`:

- The authoritative `verified` verdict is resolved from `verification_result` (`_resolve_verified`). It is `True`/`False` when a verify_spec step ran, and `None` when the workflow has no verify_spec step — in the `None` case the gate stays inactive so verify_spec-less workflows report normally.
- The gate covers **both** rendering paths:
  - **LLM path:** when `verified == False`, a "Verification Status: NOT PASSED" instruction block is injected into the prompt (via the completion section) instructing the LLM to report the non-passing status honestly.
  - **Basic-summary fallback path:** `_create_basic_summary_text()` (used when the LLM call returns empty or raises) likewise labels the work "Not verified (incomplete)", emits a "Verification Status: NOT PASSED" section, and writes a handoff line stating the flow ended NOT verified.

**Honest test status in the report:**

The session report's test status SHALL be derived via the gated verdict (`_gated_tests_passed`), which prefers `test_results["overall_passed"]` (the value the critical-acceptance gate forces to `False` on a skipped/missing critical test) over the backward-compat `passed` key (raw pytest `returncode == 0`, which stays `True` on a skip). The `passed` key is used only as a fallback for legacy `test_results` dicts written before `overall_passed` existed, so a skipped critical test is never reported as a green test status.

#### Scenario: Summary reports only this session's work
- **WHEN** the summarize step runs
- **THEN** the prompt instructs the LLM to report only what this session actually did (work performed, key changes, files modified, honest test/verification status, handoff notes)
- **AND** the prompt does NOT include any issue-discovery injection fragment
- **AND** `step.outputs` does NOT contain `discovered_issues`

#### Scenario: Completion gate trips on verified=False (LLM path)
- **GIVEN** `verification_result["verified"]` is `False`
- **WHEN** the summarize prompt is built
- **THEN** a "Verification Status: NOT PASSED" instruction block is injected instructing the report to state honestly that verification did not pass and the work is unverified/unfinished
- **AND** the report MUST NOT claim the work is "complete", "done", "all green", or "verified"

#### Scenario: Completion gate trips on verified=False (fallback path)
- **GIVEN** `verification_result["verified"]` is `False` and the LLM call returns empty or raises
- **WHEN** `_create_basic_summary_text()` produces the fallback summary
- **THEN** the status label is "Not verified (incomplete)", a "Verification Status: NOT PASSED" section is emitted, and the handoff line states the flow ended NOT verified

#### Scenario: Gate inactive without verify_spec
- **GIVEN** the workflow has no verify_spec step, so the authoritative `verified` resolves to `None`
- **WHEN** the summarize step runs
- **THEN** the completion gate stays inactive and the report is generated normally without a NOT-PASSED block

#### Scenario: Skipped critical test not reported as green
- **GIVEN** `test_results["overall_passed"]` is `False` (critical test skipped/missing) while the backward-compat `passed` is `True`
- **WHEN** the report's test status is rendered
- **THEN** the gated verdict (`_gated_tests_passed`) reports the test status as not passing, not green

### Requirement: Implement Step DAG Execution Strategy

The `implement` step SHALL use an intelligent execution strategy that adapts based on total estimated lines of code and DAG topology.

**Execution Strategy Selection:**
- If there is exactly one task group, it is executed as a single LLM call directly (no threshold comparison needed).
- If there are multiple groups, the implement step computes total `estimated_loc` across all tasks in all groups (tasks missing the field default to 50 LOC each).
  - If total LOC ≤ `implement.group_loc_threshold` (default: 300, configurable in `se3.yaml`), all groups are merged into a single LLM call regardless of grouping.
  - If total LOC > threshold, the step would normally execute groups according to the DAG parallel strategy, subject to the two short-circuit rules below.
- **Short-circuit 1 — explicit disable:** If `implement.use_worktree` is `false`, the DAG parallel path is never entered. All groups execute sequentially in `group_order` order on the original branch, with no per-group worktree or `impl/*` branch. This holds regardless of DAG topology, including forked DAGs with real parallelism potential.
- **Short-circuit 2 — linear chain fallback:** If the DAG's `RelayPlan` is a linear chain (no forks and exactly one root — i.e. every topological layer has at most one node), the implement step falls back to sequential execution even when `use_worktree=true`. Linear chains yield no parallelism benefit, so the worktree/branch overhead is skipped.
- The `plan` step grouping principles (high cohesion, low coupling) are preserved — the implement step only decides whether to collapse groups at execution time.

#### Scenario: Single group uses single LLM call directly
- **GIVEN** `plan` produced exactly 1 group with estimated_loc = 141
- **WHEN** the implement step executes
- **THEN** the group is executed as a single LLM call without LOC threshold comparison

#### Scenario: Small multi-group implementation collapses groups
- **GIVEN** `plan` produced 3 groups with total estimated_loc = 180
- **AND** `implement.group_loc_threshold` is 300
- **WHEN** the implement step executes
- **THEN** all groups are merged into a single LLM call

#### Scenario: Large implementation uses DAG parallel
- **GIVEN** `plan` produced 4 groups with total estimated_loc = 500
- **AND** the groups' dependency graph has at least one fork (some node has more than one direct dependent)
- **AND** `implement.use_worktree` is `true` (default)
- **WHEN** the implement step executes
- **THEN** groups are executed via DAG parallel strategy with relay branching

#### Scenario: use_worktree=false forces sequential execution
- **GIVEN** `plan` produced groups with total estimated_loc > `implement.group_loc_threshold`
- **AND** the groups' dependency graph contains forks (would normally trigger DAG parallel)
- **AND** `implement.use_worktree: false` in `se3.yaml`
- **WHEN** the implement step executes
- **THEN** no `impl/*` branch or worktree is created for any group
- **AND** groups are executed sequentially in `group_order` order on the original branch
- **AND** the execution-strategy panel shows the sequential strategy, not `dag_parallel`
- **AND** the panel strategy line includes the short-circuit reason `use_worktree=False`

#### Scenario: Linear chain falls back to sequential
- **GIVEN** `plan` produced groups forming a linear chain G1 → G2 → G3
- **AND** total estimated_loc > `implement.group_loc_threshold`
- **AND** `implement.use_worktree` is `true` (default)
- **WHEN** the implement step executes
- **THEN** `_relay_plan_is_linear()` returns `true` for the computed `RelayPlan`
- **AND** the implement step falls back to sequential execution on the original branch
- **AND** no `impl/*` branch or worktree is created
- **AND** a log entry explains that the DAG was linear so worktree creation was skipped
- **AND** the panel strategy line includes the short-circuit reason `linear chain`

#### Scenario: Natural small-LOC sequential has no reason label
- **GIVEN** `plan` produced a single group or a multi-group plan whose routing naturally selects the sequential path without any short-circuit
- **WHEN** the implement step renders the task plan panel
- **THEN** the sequential strategy line omits the `reason:` suffix, preserving the original display

**Live Per-Group Status During DAG Parallel Execution:**
- When the DAG parallel strategy is selected, each group runs inside an isolated worktree whose conversation jsonl is written under that worktree's `se3/history/<flow_id>/`, and is only salvaged back into the main repository at step end (`_salvage_history_from_worktree`). Because the daemon's `active_flow_signature` only fingerprints jsonl files under the **main** repository's `se3/history/`, the web console would otherwise stay blank for the entire parallel implement step and only render the full G1–G5 content in one burst once the step finished.
- To give the console live progress, the implement step's `_run_dag_parallel` SHALL pass an `on_group_status(group_id, status)` closure into the `DAGScheduler` (see the `dag-scheduler` *Event-Driven Parallel Execution* requirement). For each per-group lifecycle transition the closure SHALL persist a single-line `group_status` NDJSON record — via `chat_history.record_group_status(project_root, flow_id, step_id, "implement", group_id, status)` — appended directly to the **main** repository's `se3/history/<flow_id>/<step_id>.jsonl`. The status values are `queued` / `running` / `completed` / `failed` / `skipped`.
- Appending these records into the main-repo step jsonl shifts that file's `(name, mtime, size)` fingerprint, so `active_flow_signature` changes and the daemon performs an incremental `history_data` push **before** the step ends — exactly the same transport used by `record_stream_progress` / `record_step_event`. No change to `active_flow_signature`, `read_active_flows`, the daemon↔server protocol, or `ServerState` is required.
- This is a **status-only** signal by design. It does NOT relay the per-group conversation content: the full G1–G5 conversation still appears in one pass at step end, after the worktree histories are salvaged back. The worktree isolation and salvage logic are unchanged, and no run-time history-file relay (with its dedup / concurrent-flush hazards) is introduced.
- `group_status` records are written for web rendering only. `get_step_history` and the retry-context builder SHALL skip them (alongside `stream_progress` / `step_completed`) so they never pollute the LLM retry prompt or `se3 history show` output.
- This live-status path is scoped to the DAG parallel grouping route. The sequential grouping path is out of scope (its group jsonl already writes directly to the main repository).

#### Scenario: DAG parallel implement emits live per-group status records
- **GIVEN** an implement step routed to the DAG parallel strategy with groups G1–G5 running in isolated worktrees
- **WHEN** the scheduler dispatches, completes, fails, or skips each group
- **THEN** a `group_status` NDJSON line (`type: "group_status"`, with `group_id` and a `status` of `queued` / `running` / `completed` / `failed` / `skipped`) is appended to the main repository's `se3/history/<flow_id>/<step_id>.jsonl` as the transition occurs
- **AND** the appended line shifts the file's fingerprint so the daemon pushes the record incrementally before the step ends, instead of the console staying blank until step end
- **AND** the full per-group conversation content is still presented only after the worktree histories are salvaged at step end

#### Scenario: Group-status records are excluded from history and retry context
- **WHEN** `get_step_history` reads a step jsonl that contains `group_status` records, or the retry-context prompt is assembled for that step
- **THEN** the `group_status` lines are skipped, the same way `stream_progress` and `step_completed` records are
- **AND** they never appear in the LLM retry prompt or in `se3 history show`

**Transitive Reduction:**
- Before DAG parallel execution, the implement step performs transitive reduction on group `depends_on` edges by calling the `transitive_reduce(groups)` function defined in `se3/engine/transitive_reduction.py`.
- An edge u→v is redundant if there is a longer path from u to v through intermediate nodes (standard graph theory algorithm using BFS).
- Example: G2 depends on [G1], G3 depends on [G1, G2] → after reduction: G3 depends on [G2] only (G1 is reachable through G2).
- This reduces unnecessary pre-merge operations and wait times.
- After reduction, the step logs which redundant edges were removed per group (group ID and sorted list of removed deps), or logs that no redundant edges were found.

#### Scenario: Transitive reduction removes redundant edges
- **GIVEN** G2 depends on [G1] and G3 depends on [G1, G2]
- **WHEN** transitive reduction is applied
- **THEN** G3's depends_on becomes [G2] only
- **AND** a log entry reports that G3 removed redundant dep [G1]

#### Scenario: Transitive reduction finds no redundant edges
- **GIVEN** all `depends_on` edges are minimal (no transitive shortcuts)
- **WHEN** transitive reduction is applied
- **THEN** no edges are removed
- **AND** a log entry reports that no redundant edges were found

**Branch Relay Strategy:**
- The implement step uses a branch relay strategy instead of per-group branch creation with pre-merge and merge-back.
- Branch Relay applies only when the DAG parallel strategy is actually selected (see short-circuit rules above). For linear chains and `use_worktree=false`, the implement step executes sequentially on the original branch and the relay strategy is not used.
- For forks (G1 → G2, G1 → G3): G1 executes; G2 (primary heir, lowest group_order) reuses G1's worktree; G3 forks G1's branch into a new worktree. G2 and G3 execute in parallel.
- For convergence points (G2, G3 → G4): G4 inherits the primary predecessor's worktree and merges secondary predecessor branches before executing.
- The relay plan is produced by `classify_chains()` which computes `RelayPlan` containing: relay_map, fork_from, leaf_nodes, convergence_points, and root_nodes. Helper `_relay_plan_is_linear(plan)` is used by `implement_handler` to detect the linear-chain short-circuit — it returns `true` iff `plan.fork_from` is empty and `plan.root_nodes` has exactly one element.

#### Scenario: Fork relay
- **GIVEN** G1 has two dependents G2 and G3 (G2 has lower group_order)
- **WHEN** DAG parallel executes
- **THEN** G2 reuses G1's worktree (primary heir)
- **AND** G3 forks G1's branch into a new worktree
- **AND** G2 and G3 execute in parallel

#### Scenario: Convergence point
- **GIVEN** G4 depends on both G2 and G3
- **WHEN** G4 is about to execute
- **THEN** G4 inherits the primary predecessor's worktree
- **AND** merges secondary predecessor branches into it before executing

**Leaf-Only Merge with LLM Conflict Resolution:**
- Only leaf nodes (groups with no downstream dependents) merge back to the original branch.
- Before attempting `git merge <branch>`, the leaf-merge path SHALL validate that the target leaf branch ref resolves to a commit via `git rev-parse --verify --quiet <branch>^{commit}`. Under IO stall or a concurrent branch deletion the ref can be missing, and `git merge <missing>` reports the opaque `merge: <branch> - not something we can merge` error. When the probe fails, the leaf merge is skipped and reported as failed (logging the diagnosable `rev-parse` failure); because the merge was never started, there is no in-progress merge state to abort.
- Merge conflicts are resolved by LLM with full context including: task descriptions, per-group summaries and files_changed, conflicting file content with conflict markers, and spec content.
- The LLM resolver retries up to a bounded number of attempts on failure.
- When the LLM resolver is exhausted, the leaf-merge path SHALL fall back to a deterministic `_take_theirs_fallback`: for every still-conflicting file, run `git checkout --theirs -- <file>` (i.e., accept the leaf branch's version verbatim) and `git add <file>`, then complete the merge commit with `git commit --no-edit`. Rationale: the leaf branch encapsulates the DAG implement output — the commits that MUST be preserved; `ours` (the pre-merge target) is typically upstream content the user already accepted overriding by running implement.
- Whenever the take-theirs fallback fires, an audit issue SHALL be filed via `IssueManager.create()` recording the flow_id, branch, and list of conflict files that were resolved deterministically (priority `medium`, type `task`, tags `["merge-fallback", "audit"]`). Audit-issue write failures are logged but never block the merge.
- If the take-theirs fallback's commit itself fails (e.g., from a misconfigured git state), the leaf merge aborts: the merge index is cleaned up and the caller is signaled to fail. There is no `pending_human` escalation on this path.
- **Scope:** this take-theirs fallback applies ONLY to the DAG leaf-merge inside the `implement` step. The `se3 merge` command path has separate, strictly stricter rules (see *`se3 merge` Conflict Resolution Mechanism*) — every `se3 merge` strategy resolves via LLM-as-editor or escalates to a human MCP call, and SHALL NEVER take-theirs. The two paths are intentionally distinct contracts.

#### Scenario: Leaf merge succeeds
- **GIVEN** a leaf group completed its work
- **WHEN** merging back to the original branch
- **THEN** a standard git merge is attempted
- **AND** if no conflicts, the merge completes normally

#### Scenario: Leaf merge aborts when the target branch ref is missing
- **GIVEN** a leaf group whose target branch ref does not resolve to a commit (e.g. deleted under a concurrent operation or lost during an IO stall)
- **WHEN** the leaf-merge handler runs
- **THEN** the pre-merge `git rev-parse --verify --quiet <branch>^{commit}` probe fails and the merge is skipped
- **AND** the leaf merge is reported as failed with a diagnosable error rather than an opaque `not something we can merge` git message
- **AND** no in-progress merge state is left to abort (the merge was never started)

#### Scenario: Leaf merge with LLM conflict resolution
- **GIVEN** a leaf group's merge produces conflicts
- **WHEN** the merge conflict handler runs
- **THEN** the LLM receives full context (task descriptions, group summaries, conflict markers, specs)
- **AND** the LLM resolves all conflicting files within the bounded retry budget
- **AND** if all conflicts are resolved, the merge completes normally

#### Scenario: Leaf merge LLM exhaustion falls back to take-theirs
- **GIVEN** a leaf group's merge produces conflicts
- **AND** the LLM conflict resolver exhausts its retry budget without resolving every file
- **WHEN** the leaf-merge handler runs the deterministic fallback
- **THEN** for every still-conflicting file, `git checkout --theirs -- <file>` is invoked and the file is staged
- **AND** the merge is completed with `git commit --no-edit`
- **AND** an audit issue is filed via `IssueManager.create()` (priority `medium`, tags `["merge-fallback", "audit"]`) listing the flow_id, branch, and conflict files
- **AND** the merge is reported as successful to the caller

#### Scenario: Take-theirs fallback commit failure aborts the merge
- **GIVEN** the take-theirs fallback was triggered after LLM exhaustion
- **WHEN** the final `git commit --no-edit` fails
- **THEN** the merge index is cleaned up and the leaf merge is reported as failed
- **AND** no `pending_human` escalation is created on this path

#### Scenario: Take-theirs scope does not extend to `se3 merge`
- **GIVEN** any `se3 merge` invocation
- **WHEN** conflicts arise in that command's resolution loop
- **THEN** the DAG leaf-merge take-theirs fallback is NOT invoked
- **AND** `se3 merge` follows its own strategy contract (LLM-as-editor or human MCP escalation per `fast` / `safe` / `strict`), never silently accepting either side

**DAG Disaster-Recovery Dependency Pruning:**
- When DAG resume / disaster recovery detects a surviving, recoverable group (its impl branch already carries commits that were effectively pre-merged into the baseline branch) and drops it from the to-run `dag_groups`, it SHALL also prune every `depends_on` edge in the surviving groups that points at a dropped group (`_prune_recovered_dependencies`). Semantically those dropped groups are already in the baseline, so the edge is satisfied and must not survive as a dangling reference.
- This active pruning is the first line of defense; the scheduler's defensive dangling-edge skip (see the `dag-scheduler` spec's *DAG Construction and Validation* requirement) is the second, so a stale edge that escapes pruning still does not abort scheduling with a `Group X depends on unknown group Y` error.

#### Scenario: Recovery prunes dangling depends_on edges of dropped groups
- **GIVEN** a DAG resume drops an already-completed, pre-merged group from the to-run set
- **AND** another surviving group still lists that dropped group in its `depends_on`
- **WHEN** `_prune_recovered_dependencies` runs over the surviving groups
- **THEN** the edge pointing at the dropped group is removed from the survivor's `depends_on`
- **AND** the resulting group list builds a valid DAG without a dangling-dependency error

**DAG Branch Cleanup:**
- After all leaf merges complete, the DAG parallel strategy cleans up implementation branches.
- Branch names for cleanup SHALL be collected from actual `GroupResult.branch_name` values (not constructed from group IDs), because relay chains cause downstream groups to reuse a root group's branch rather than having their own.
- Recovered group branches (from `prior_outputs.implemented_groups` during DAG resume) that were already deleted during the recovery phase SHALL be tracked and skipped during final cleanup, preventing duplicate deletion errors.
- Recovered group branches that were NOT successfully deleted during recovery SHALL still be included in final cleanup as a fallback.

#### Scenario: DAG branch cleanup uses actual branch names
- **GIVEN** a relay chain where G2 reuses G1's branch
- **WHEN** final branch cleanup runs after leaf merges
- **THEN** branch names are collected from each GroupResult's `branch_name` field
- **AND** duplicate branch names are deduplicated (via set)
- **AND** no "branch not found" error occurs for groups sharing a relay branch

#### Scenario: DAG resume skips already-deleted branches in final cleanup
- **GIVEN** a DAG resume recovered groups G2 and G4 from prior_outputs
- **AND** their branches were successfully merged and deleted during the recovery phase
- **WHEN** final branch cleanup runs
- **THEN** G2 and G4 branches are skipped because they were already deleted
- **AND** no "branch not found" error occurs

#### Scenario: DAG resume retains fallback for failed recovery deletions
- **GIVEN** a DAG resume recovered group G3 from prior_outputs
- **AND** branch deletion failed during the recovery phase
- **WHEN** final branch cleanup runs
- **THEN** G3's branch is included in final cleanup as a fallback attempt

**DAG Worktree History Management:**
- Before a worktree executes, `_restore_history_to_worktree` copies LLM chat history from the main repo into the worktree so that retry context injection works inside the worktree.
  - Only non-group files (e.g. `discovery`, `analyze`, `plan` step history) are copied; files matching `_G\d+\.jsonl$` are **skipped** to prevent them being double-appended when the worktree is later salvaged.
- After a worktree finishes, `_salvage_history_from_worktree` copies history files back to the main repo before the worktree is removed.
  - Only files whose names match `_G\d+\.jsonl$` (i.e. group-specific history generated by this worktree) are salvaged; all other files are skipped.
  - This prevents shared prior-step history files (discovery, analyze, plan, confirm, etc.) from being appended to the main repo N times — once per worktree — which would cause them to display duplicated content in `se3 history show`.
- **Append-on-existence policy:** When salvaging a group-history file, if the target path already exists in the main repo, the salvaged file's content SHALL be appended to the existing file (line-based NDJSON concatenation) rather than overwritten. This policy is unconditional — it applies in every case where the target file already exists, including but not limited to the relay-chain scenario where multiple `GroupResult` objects share the same worktree, and also resume/retry paths where a prior partial salvage may have already written content for the same group. NDJSON is line-delimited, so concatenation preserves a valid stream and the merged file is replay-safe.
- The salvage function deduplicates worktree paths so that a single worktree is only salvaged once even when shared by multiple groups.

#### Scenario: Salvage only copies group-specific history files
- **GIVEN** a worktree has completed execution
- **AND** its `se3/history/{flow_id}/` directory contains both shared files (e.g. `discovery.jsonl`, `analyze.jsonl`) and group-specific files (e.g. `implement_G2.jsonl`)
- **WHEN** `_salvage_history_from_worktree` runs
- **THEN** only files matching `_G\d+\.jsonl$` are copied to the main repo
- **AND** shared prior-step files are skipped

#### Scenario: Restore only copies shared context files to worktree
- **GIVEN** a worktree is about to be created for group G3
- **AND** the main repo has history files including `implement_G1.jsonl` and `analyze.jsonl`
- **WHEN** `_restore_history_to_worktree` runs
- **THEN** only non-group files (e.g. `analyze.jsonl`) are copied into the worktree
- **AND** `implement_G1.jsonl` is skipped (matches `_G\d+\.jsonl$`)

#### Scenario: Relay chain worktrees share the same worktree path
- **GIVEN** a relay chain where G2 reuses G1's worktree
- **AND** both G1 and G2 produce `GroupResult` objects referencing the same worktree_path
- **WHEN** history salvage runs after all groups complete
- **THEN** the shared worktree is only salvaged once (deduplication by worktree_path)

#### Scenario: Salvage appends when the target group-history file already exists
- **GIVEN** the main repo already contains a group-history file at `se3/history/{flow_id}/{step_id}_G{n}.jsonl` (e.g. from a prior partial salvage, a resumed DAG run, or an earlier worktree that contributed to the same group's history)
- **AND** a worktree being salvaged carries its own copy of that same target path under its `se3/history/{flow_id}/` directory
- **WHEN** `_salvage_history_from_worktree` copies the file back to the main repo
- **THEN** the worktree's file content is appended to the existing main-repo file (NDJSON line-based concatenation)
- **AND** the existing file content is preserved — no overwrite occurs
- **AND** the resulting merged file remains a valid NDJSON stream readable by `chat_history` consumers

### Requirement: `se3 merge` Conflict Resolution Mechanism

The standalone `se3 merge` orchestrator SHALL resolve every git-level merge conflict through an LLM-as-editor loop. This Requirement is the central contract for the conflict-resolution layer used by every merge strategy tier; per-tier escalation policies are defined separately under the `se3-commands` spec.

**Core rules:**

1. **Never take-theirs.** The orchestrator SHALL NOT, under any code path, fall back to `git checkout --theirs <file>`, `git merge -X theirs`, or any equivalent silent acceptance of the incoming side. This applies to every failure mode (context-build failures, LLM exceptions, parse failures, apply failures, human-call write failures, repair stalls). Every conflict — including conflicts in pyproject.toml or other deterministic-version files — is resolved either by the LLM editor loop or by escalation to a human MCP call (when the active strategy permits it); silent acceptance of either side is prohibited.

2. **LLM edits all conflict files in a single batched call.** When `git merge` produces conflicts, the orchestrator SHALL build a single LLM prompt that lists every conflicting file in that one merge invocation, together with merge metadata (ours/theirs branch names, merge-base, both HEAD hashes and oneline logs since merge-base) and the three-way base/ours/theirs contents plus the working-tree file containing `<<<<<<<` / `=======` / `>>>>>>>` markers for each. The LLM SHALL directly edit those working-tree files (e.g., via an `Edit` tool) to remove every conflict marker and produce a semantically reasonable merge — it MUST NOT emit a JSON `decision` field or a `resolved_content` blob for the orchestrator to splice in. Per-file LLM invocations are NOT used; a single batched edit call is the unit of work.

3. **Unresolved files trigger a whole-batch retry up to a configured upper bound.** After each edit round, the orchestrator SHALL scan every target file for any remaining `<<<<<<<` / `=======` / `>>>>>>>` marker. Files still containing a marker — together with their current residual content and the previous round's prompt context — are gathered into a new batch and re-submitted to the LLM as the next round. The iteration cap is `merge.max_conflict_resolve_iterations` (default 10; see se3-config). The orchestrator SHALL NOT abandon the batch early because of a single file's apparent failure: the loop continues, with the same not-take-theirs guarantee, until either every file is clean (no markers anywhere) or the iteration cap is reached.

4. **The active strategy decides what happens on cap exhaustion — LLM-only vs human fallback.** When the iteration cap is reached with at least one file still containing a conflict marker, the orchestrator SHALL hand control to the strategy layer:
   - `fast` mode SHALL fail the merge invocation outright, never invoking a human reviewer and never taking either side.
   - `safe` mode SHALL escalate to a human MCP call (`reviewer: human`, same mechanism as the `confirmation` step's human reviewer): the user edits the still-conflicting files until no marker remains, and the merge then resumes.
   - `strict` mode SHALL NOT enter the LLM editor loop at all — every conflict is routed directly to a human MCP call from the first iteration, without any LLM attempt.

   The choice is made entirely by the merge strategy (see se3-commands `se3 merge` Command); the conflict-resolver layer does not consult `conflict_resolver.strategy` or any other ad-hoc config — there is no `merge.conflict_resolver` configuration knob.

#### Scenario: Take-theirs is never invoked
- **GIVEN** any `se3 merge` invocation in any strategy
- **WHEN** the LLM throws, the prompt context cannot be built, a round of edits leaves files still containing conflict markers, the iteration cap is reached, or a human-call write fails
- **THEN** the orchestrator SHALL NOT silently checkout either side of the conflict
- **AND** no `git checkout --theirs`, `git checkout --ours`, or `--strategy-option theirs` invocation appears anywhere in the merge code path

#### Scenario: Single batched LLM call edits every conflicting file
- **GIVEN** a `git merge` produces conflicts in files `a.py`, `b.py`, and `se3/specs/x/spec.md`
- **WHEN** the conflict resolver builds the first round's prompt
- **THEN** all three files appear in one LLM call with their three-way contents and working-tree markers
- **AND** the LLM directly edits the working-tree files to remove every `<<<<<<<` / `=======` / `>>>>>>>` marker
- **AND** the orchestrator does NOT splice content from a JSON `decision` / `resolved_content` field — file state on disk is the sole source of truth

#### Scenario: Unresolved files re-enter the batch for another round
- **GIVEN** a first round of LLM edits leaves `a.py` clean but `b.py` and `c.py` still containing markers
- **WHEN** the orchestrator scans the working tree after the round
- **THEN** `b.py` and `c.py` (and only those) form the next round's batch
- **AND** the new round's prompt includes both the previous round's prompt context and the current residual state of those two files

#### Scenario: Fast mode exits without human fallback when the cap is hit
- **GIVEN** `merge.max_conflict_resolve_iterations` is 10 and `merge.strategy` is `fast`
- **WHEN** ten rounds of LLM edits still leave at least one conflict marker in some file
- **THEN** the merge invocation exits with a failure
- **AND** no human MCP call is created
- **AND** no take-theirs fallback is attempted

#### Scenario: Safe mode escalates to a human call when the cap is hit
- **GIVEN** the same setup with `merge.strategy: safe`
- **WHEN** the iteration cap is reached and conflict markers persist
- **THEN** a human MCP call is created (same mechanism as `confirmation.steps.<step>.reviewer: human`)
- **AND** the flow waits until the user has edited the residual files to remove every conflict marker
- **AND** no take-theirs fallback is attempted

#### Scenario: Strict mode never enters the LLM editor loop
- **GIVEN** `merge.strategy: strict` and a `git merge` reports conflicts in any file
- **WHEN** the orchestrator handles the conflict
- **THEN** no LLM call is issued
- **AND** a human MCP call is created from the very first iteration
- **AND** no take-theirs fallback is attempted

### Requirement: Worktree Cleanup Resilience

The worktree cleanup subsystem SHALL be resilient to cascading failures. `force_cleanup_worktree` executes a multi-step cleanup pipeline where each step is independently fault-tolerant — a single step timing out or raising an exception MUST NOT prevent subsequent steps from executing.

**Cleanup Pipeline (6 steps):**
1. Unlock the worktree (ignore errors if not locked)
2. `git worktree remove -f -f` (double-force removal)
3. Remove the worktree directory via `shutil.rmtree`
4. `git worktree prune` (prune stale entries)
5. Direct `.git/worktrees/<safe_name>` metadata removal as last resort
6. Verification — check whether the worktree is still registered

**Fault Tolerance:**
- Steps 1, 2, and 4 (git commands) each catch `TimeoutExpired` and general `Exception` independently
- Steps 3, 5, and 6 each catch `Exception` independently
- A failure in any step is logged at WARNING level but does not abort the pipeline

**Timeout:**
- All `_run_git` calls within `force_cleanup_worktree` SHALL use a 60-second timeout (overriding the default 30s), to accommodate slow filesystem or lock-release operations

**Git Worktree Metadata Cleanup:**
- `_cleanup_git_worktree_metadata` directly removes the `.git/worktrees/<safe_name>` directory using `shutil.rmtree`
- This bypasses standard git commands and is used only as a last resort after Steps 1–4
- If the metadata directory does not exist, it is a no-op
- Removal failures are logged at WARNING level but do not raise

**Branch Deletion Worktree Verification:**
- `delete_branch` SHALL check whether a worktree is still registered for the target branch before attempting deletion
- If a worktree is detected, `force_cleanup_worktree` is invoked first
- After cleanup, a re-check verifies the worktree is gone; if it persists, a warning is logged
- Branch deletion proceeds regardless of worktree cleanup outcome

#### Scenario: Force cleanup with Step 1 timeout
- **GIVEN** a worktree exists for a branch
- **WHEN** `force_cleanup_worktree` runs and Step 1 (unlock) times out
- **THEN** Steps 2–6 still execute
- **AND** the worktree is cleaned up by subsequent steps

#### Scenario: Force cleanup with Step 2 exception
- **GIVEN** a worktree exists for a branch
- **WHEN** `force_cleanup_worktree` runs and Step 2 (remove) raises an exception
- **THEN** Steps 3–6 still execute
- **AND** the directory is removed by Step 3 and metadata by Step 5

#### Scenario: Git worktree metadata cleanup removes stale metadata
- **GIVEN** `.git/worktrees/<safe_name>` exists but standard git commands failed to remove it
- **WHEN** Step 5 (metadata cleanup) runs
- **THEN** the metadata directory is deleted via `shutil.rmtree`

#### Scenario: Git worktree metadata absent
- **GIVEN** `.git/worktrees/<safe_name>` does not exist
- **WHEN** `_cleanup_git_worktree_metadata` is called
- **THEN** it returns immediately with no side effects

#### Scenario: Branch deletion with lingering worktree
- **GIVEN** a worktree is still registered for a branch
- **WHEN** `delete_branch` is called
- **THEN** `force_cleanup_worktree` runs before the `git branch -D` command
- **AND** the worktree registration is re-checked after cleanup
- **AND** the branch is deleted regardless of worktree cleanup outcome

#### Scenario: Branch deletion without worktree
- **GIVEN** no worktree is registered for a branch
- **WHEN** `delete_branch` is called
- **THEN** the branch is deleted directly without invoking `force_cleanup_worktree`

### Requirement: Implement-Test Contract

The implement step SHALL declare `tests_added`, `test_mapping`, and `estimated_test_duration` in its output, forming an explicit contract with the test step.

**Output fields:**
- `tests_added`: a list of the test file paths added in this run (relative to the project root)
- `test_mapping`: a dictionary whose keys are test IDs and whose values are spec scenario identifiers (`{spec_name}::{scenario_name}`)
- `estimated_test_duration`: an integer, the estimated number of seconds the test suite runs. The LLM estimates it based on the count and complexity of `tests_added`. The test step computes the dynamic timeout from it (see the "Test Dynamic Timeout" requirement). When not provided or invalid, the test step falls back to the `test.timeout` configured in `se3.yaml`.

**Test ID format (language-specific):**
| Language | Format | Example |
|------|------|------|
| Python (pytest) | `file::function` | `tests/test_auth.py::test_login_success` |
| JavaScript (jest/vitest) | `file > describe > it` | `tests/auth.test.js > LoginService > authenticates user` |
| Go | `package.TestFunc` | `auth.TestLoginSuccess` |
| Rust | `module::test_func` | `auth::test_login_success` |

**Base Spec convention references:**
- The placement and naming of test files follows the base spec's Coding Conventions and Directory Structure

#### Scenario: implement step declares added tests
- **WHEN** the implement step completes the implementation
- **THEN** the output contains a `tests_added` list
- **AND** the output contains a `test_mapping` dictionary

#### Scenario: implementation with no added tests
- **WHEN** the implement step completes but adds no test files
- **THEN** `tests_added` is an empty list
- **AND** `test_mapping` is an empty dictionary

#### Scenario: implement provides a test duration estimate
- **WHEN** the implement step completes the implementation
- **THEN** the output contains an `estimated_test_duration` integer field
- **AND** the state machine forwards this field from implement.outputs to test.inputs, so the test step can compute the dynamic timeout

#### Scenario: estimate correction after a timeout
- **WHEN** the test step triggers a fix loop because of a dynamic timeout
- **AND** implement receives, in the fix iteration, a `fix_context` containing `timeout_reason`, `previous_timeout`, `previous_estimated_test_duration`, and `timeout_multiplier`
- **THEN** implement provides a larger `estimated_test_duration` in its new JSON output
- **AND** the next test execution computes the timeout based on the updated estimate, avoiding an infinite loop of repeated timeouts

### Requirement: Block Rendering Visual Style

All SE3 user-facing rendered output blocks SHALL use a uniform reverse-color block visual: a reverse-color title at the top, the content body in the middle, and a fixed-width reverse-color footer at the bottom — no `Panel` border, no `Rule`, no horizontal lines, no left-side color bar, no terminal-width-adaptive visual elements.

**Visual primitives:**

- **Title block** — rendered as a Rich `Text` instance styled with `Style(color="white", bgcolor=<role_color>, bold=True)`. The text uses the markdown form `## Title` padded with a single space on each side (e.g. ` ## Discovery `) so the colored background visually frames the title. Using a `Text` object (not markup string interpolation) avoids markup-escape pitfalls in titles that contain user data.
- **Footer block** — a fixed-width reverse-color block of `_BLOCK_FOOTER_WIDTH = 4` spaces, rendered with `Style(bgcolor=<role_color>)` matching the title's role color. The footer width is constant (4 columns) regardless of terminal size; it SHALL NOT scale with the console width.
- **Layout order:** `title_block`, blank line, `content`, blank line, `footer_block`, blank line. The blank line between content and footer is mandatory — it prevents the footer from being copied when the user selects content.
- **Copy safety:** boundary markers are drawn purely via `bgcolor` over space characters, not via visible characters (no `─`, `═`, `┃`, `▔`, etc.). A user selecting and copying the content body never picks up boundary noise; selecting the footer copies only insignificant whitespace.

**Helper functions (single source of truth in `se3/engine/display.py`):**

- `_reverse_title(title: str, color: str) -> Text` — module-private helper that returns the title `Text` object.
- `_reverse_footer(color: str, width: int = _BLOCK_FOOTER_WIDTH) -> Text` — module-private helper that returns the fixed-width footer `Text` object.
- `render_block_header(title: str, color: str)` and `render_block_footer(color: str)` — thin public wrappers that print the header/footer (with appropriate blank-line spacing) directly to the console. These are the entry points used by modules outside `display.py` so external call sites do not construct `[reverse <color>] ## ... [/reverse <color>]` markup inline.

**Consumers:**

- All 8 render functions in `display.py` (`render_full`, `render_proposal`, `render_design`, `render_spec_content`, `render_text`, `render_code`, `render_diff`, `render_markdown`) use `_reverse_title` and `_reverse_footer` for their boundaries. The three delegating renderers (`render_proposal`, `render_design`, `render_spec_content`) inherit the visual transparently via `render_full`.
- `task_formatter._md_heading` and `_heading_group` are thin re-wraps of the display helpers. `_heading_group` SHALL append `_reverse_footer` (plus a trailing blank line) inside the returned `Group`, so all 8 task_formatter callers automatically gain the bottom boundary without per-callsite changes; callers continue to `console.print(...)` the returned renderable without changing their contract.
- External display call sites in `chat_history`, `sync`, `issue_cmd`, `worktree.py` (Merge Conflict prompt, red), `sync_interaction.py` (Decision Input panel, blue), and `discovery.py` (Discovery message, blue) SHALL invoke `display.render_block_header(title, color)` before the body and `display.render_block_footer(color)` after, rather than constructing markup strings directly.

**Color → role mapping (preserved from the prior `border_style` semantics):**

| Color | Role |
|-------|------|
| `blue` | general / prompt / discovery / decision input |
| `green` | code / response |
| `yellow` | diff |
| `magenta` | markdown |
| `red` | error / merge conflict |
| `cyan` | summary (task_formatter local use) |

#### Scenario: Footer width is constant across terminal widths
- **GIVEN** any rendered block produced by a `display.render_*` function or via `render_block_footer`
- **WHEN** the terminal is resized to any width (narrow or wide)
- **THEN** the footer block remains exactly `_BLOCK_FOOTER_WIDTH` (4) columns wide
- **AND** no part of the title/footer scales with terminal width
- **AND** no `Rule`, `Panel` outline, or horizontal-line element appears around the body

#### Scenario: Content selection excludes boundary characters
- **GIVEN** a rendered block consisting of reverse-color title, blank line, content, blank line, reverse-color footer
- **WHEN** the user selects and copies the content body
- **THEN** the copied text contains only the content body — no boundary characters leak in, because the title and footer are drawn entirely via background-colored spaces, not via visible glyphs
- **AND** the title and content are separated by a blank line, and the content and footer are separated by a blank line, so the footer is never adjacent to the last content line

#### Scenario: External modules use public block wrappers
- **WHEN** code outside `display.py` needs to render a titled block (e.g. `worktree.py` Merge Conflict prompt, `sync_interaction.py` Decision Input, `discovery.py` Discovery message, `chat_history`/`sync`/`issue_cmd` panels)
- **THEN** the call site invokes `display.render_block_header(title, color)` before the body and `display.render_block_footer(color)` after
- **AND** the call site does NOT hand-roll `[reverse <color>] ## Title [/reverse <color>]` markup strings inline
- **AND** no `Panel`, `Rule`, or other border element is added around the body

#### Scenario: task_formatter heading group auto-appends footer
- **GIVEN** a `task_formatter` helper that returns a `rich.console.Group` built via `_heading_group(title, color, *body)`
- **WHEN** the caller prints that Group to the console
- **THEN** the Group already contains the reverse-color title at the top and a matching reverse-color footer at the bottom (with the required blank-line spacing)
- **AND** the caller does not need to print a footer separately

### Requirement: Implement Step Output Rendering

The `implement` step SHALL use a custom renderer that presents structured, human-readable output instead of raw JSON key-value listing.

**Status Bar:**
- A top-line status bar shows completion status with visual icons: `✓` (complete/green), `◐` (partial/yellow), `✗` (failed/red).
- The status bar includes counters for groups, files changed, and tests added.

**Sections (displayed in order when data is present):**
1. **Summary** — semicolon-delimited per-group breakdown, each part prefixed with its group ID (e.g. `G1.`, `G2.`).
2. **Files Changed** — files grouped by top-level directory (e.g. `src/`, `tests/`), with per-directory counts. Files without a directory component are grouped under `./` (sorted last).
3. **Tests Added** — list of new test file paths.
4. **Incomplete Tasks** — tasks that were not completed, showing task ID and reason/error.
5. **Restricted Edits** — counts of applied restricted edits, and details of any that failed.
6. **Error** — step-level error message if present.

**Output keys consumed by the renderer:**
- `completion_status`, `files_changed`, `tests_added`, `implemented_groups`, `summary`, `incomplete_tasks`, `restricted_edits_applied`, `restricted_edits_failed`

#### Scenario: Successful implementation rendering
- **WHEN** the implement step completes with `completion_status: "complete"`
- **THEN** the renderer displays a green `✓ Complete` status bar
- **AND** files are grouped by directory under "Files Changed"
- **AND** tests are listed under "Tests Added" if any exist

#### Scenario: Partial implementation rendering
- **WHEN** the implement step completes with `completion_status: "partial"`
- **THEN** the renderer displays a yellow `◐ Partial` status bar
- **AND** incomplete tasks are listed with their task IDs and reasons

### Requirement: Implement Step Task Plan Display

The `implement` step SHALL display a structured task plan view at the start of execution, before any LLM calls are made, across all execution paths (single-call, LOC-merged single-call, DAG parallel, sequential).

**Rendering style:**

The plan is rendered inside a reverse-color block titled `## Implementation Plan` (white text on blue background), followed by a blank spacer, the body (a `rich.console.Group` of the sections below), a blank line, and a trailing fixed-width reverse-color blue footer block — produced via the shared helpers described in the **Block Rendering Visual Style** Requirement. No outer `Panel` border, `Rule`, or horizontal-line element is drawn. The `task_formatter` module exposes internal helpers `_md_heading(title, color)` and `_heading_group(title, color, *body)` that thin-wrap the shared display helpers; `_heading_group` SHALL append the matching reverse-color footer (plus trailing blank line) inside the returned `Group`, so all other formatter return values (Task Plan, Task Summary, Dependencies, task detail, etc.) automatically gain symmetric upper/lower boundaries. The helpers remain Group-returning functions — callers continue to `console.print(...)` the returned renderable without changing their contract. Color mapping from the previous `border_style` is preserved per call site (blue/green/cyan as appropriate).

**Body Contents:**

The body (rendered as a `rich.console.Group` under the heading) contains up to four sections:

1. **Strategy Line** — shows the selected execution strategy with a visual icon:
   - `⚡ Single group → single LLM call` — when there is only one task group (with total LOC)
   - `⚡ Single LLM call` — when multiple groups are merged under the LOC threshold (with LOC and threshold info)
   - `🔀 DAG parallel` — when using DAG parallel execution (with LOC, threshold, and group count)
   - `📋 Sequential` — when executing groups sequentially (with group count)

2. **Task Groups Tree** — a hierarchical tree labeled "Task Groups" showing:
   - Each group with its ID, name, and task count
   - Group dependencies (if any)
   - Each task within its group, displaying: task ID, description, complexity badge (color-coded: green/small, yellow/medium, red/large), and estimated LOC

3. **Execution Topology** (DAG parallel only) — a layered diagram showing how groups are scheduled across execution waves, rendered using Rich Text with ANSI styling. Only displayed when the execution strategy is `dag_parallel` and a relay plan is available:
   - Groups are arranged into topologically-sorted **waves**; groups within the same wave execute in parallel.
   - Each wave is labeled with its sequential **LLM call numbers** (e.g., `LLM #1, #2`).
   - Each group node is annotated with its relationship type:
     - `● root` (green) — creates a new worktree
     - `→ relay` (blue) — reuses the predecessor's worktree (linear chain continuation)
     - `⑂ fork` (magenta) — forks a new branch from a predecessor's worktree
     - `⊕ merge ←` (yellow) — convergence point that merges secondary predecessor branches before executing
     - `◆ leaf` (yellow) — chain endpoint that merges back to the base branch
   - Waves are connected by `│▼` vertical connectors.

4. **LOC Summary** — total estimated LOC with per-group LOC distribution

**Error Handling:**
- Display failures are caught and logged at DEBUG level; they SHALL NOT block execution.

#### Scenario: Task plan displayed before single-group execution
- **GIVEN** `plan` produced exactly one task group
- **WHEN** the implement step begins execution
- **THEN** the task plan view is displayed under the `## Implementation Plan` heading with "Single group → single LLM call (N LOC)" strategy
- **AND** the view shows the task group and LOC summary before the LLM call starts

#### Scenario: Task plan displayed before LOC-merged single-call execution
- **GIVEN** `plan` produced multiple groups with total LOC ≤ threshold
- **WHEN** the implement step begins execution
- **THEN** the task plan view is displayed under the `## Implementation Plan` heading with "Single LLM call (N LOC ≤ T threshold)" strategy
- **AND** the view shows all task groups and LOC summary before the LLM call starts

#### Scenario: Task plan displayed before DAG parallel execution
- **GIVEN** `plan` produced groups with total LOC > threshold and DAG topology
- **WHEN** the implement step begins execution
- **THEN** the task plan view is displayed under the `## Implementation Plan` heading with "DAG parallel" strategy
- **AND** the view shows group dependencies and per-group LOC estimates
- **AND** the view includes an Execution Topology section showing waves, LLM call numbering, and relay/fork/merge annotations

#### Scenario: Task plan display failure does not block execution
- **GIVEN** the task plan rendering raises an exception
- **WHEN** the implement step attempts to display the plan
- **THEN** the exception is caught and logged at DEBUG level
- **AND** execution proceeds normally without the plan display

### Requirement: Step Output Rendering — Analyze, Self Check, Verify Spec, Update Spec, Commit

The `analyze`, `self_check`, `verify_spec`, `update_spec`, and `commit` steps SHALL each use a custom renderer that presents structured, human-readable output instead of raw JSON key-value listing. All renderers use `render_full()` as their sole output interface, consistent with the Implement renderer's style.

#### Analyze Renderer

The `analyze` step renderer SHALL display a top-line status bar followed by reasoning and relevant spec items.

**Status Bar:**
- A single line showing `task_type`, `complexity`, and `scope` separated by `│` delimiters (e.g. `feature  │  medium  │  src/engine`).

**Sections (displayed in order when data is present):**
1. **Reasoning** — the analysis reasoning as a body paragraph.
2. **Relevant Spec Items** — a bullet list of selected spec identifiers rendered from `relevant_specs`.

**Hidden fields:** `spec_content` and `project_summary` are intentionally omitted from display — they are downstream data payloads, not user-facing information.

**Output keys consumed by the renderer:**
- `task_type`, `complexity`, `scope`, `reasoning`, `relevant_specs`

##### Scenario: Analyze rendering with all fields
- **WHEN** the analyze step completes with `task_type`, `complexity`, `scope`, `reasoning`, and `relevant_specs`
- **THEN** the renderer displays a status bar with task_type, complexity, and scope
- **AND** reasoning is shown as a labeled body paragraph
- **AND** relevant spec items are listed

##### Scenario: Analyze rendering hides internal data
- **WHEN** the analyze step outputs include `spec_content` or `project_summary`
- **THEN** these fields are not displayed to the user

#### Self Check Renderer

The `self_check` step renderer SHALL display review status and issues grouped by severity.

**Status Line:**
- `✗ FAILED` in red when `step.status` is `FAILED` (takes precedence over all other conditions)
- `✓ PASSED` in green when no actionable issues found; `✗ ISSUES FOUND` in red when any actionable issues exist (all severity levels are actionable).

**Sections (displayed in order when data is present):**
1. **Issues by severity** — issues grouped by severity level (critical, high, medium, low). Each issue shows its description and location.

**Output keys consumed by the renderer:**
- `status`, `issues`, `actionable_count`

##### Scenario: Self check step failed rendering
- **WHEN** the self_check step has `step.status == FAILED` (e.g., LLM call failed before producing outputs)
- **THEN** the renderer displays a red `✗ FAILED` status
- **AND** does not display `✓ PASSED` regardless of `actionable_count` defaulting to 0

##### Scenario: Self check passed rendering
- **WHEN** the self_check step completes successfully with no issues
- **THEN** the renderer displays a green `✓ PASSED` status

##### Scenario: Self check found issues rendering
- **WHEN** the self_check step completes with any severity issues
- **THEN** issues are grouped by severity with appropriate color coding
- **AND** each issue shows its description and location

#### Verify Spec Renderer

The `verify_spec` step renderer SHALL display verification status, issues grouped by severity, and recommendations.

**Status Line:**
- `✓ PASSED` in green when `verified` is true; `✗ FAILED` in red when false.

**Sections (displayed in order when data is present):**
1. **Summary** — verification summary as body text.
2. **Issues by severity** — issues grouped into `error` (red prefix), `warning` (yellow prefix), and `info` (dim prefix) sections. Each issue shows its `message`, and optionally a `suggestion` line below it.
3. **Recommendations** — a bullet list of recommendations.

**Hidden fields:** `fix_context`, `fix_iteration`, `fix_needed`, `max_fix_iterations`, `verification_result` and other internal mechanism fields are omitted from display.

**Field fallback:** `summary` and `recommendations` are first read from top-level `outputs`; if absent or empty, the renderer falls back to reading them from `outputs["verification_result"]` (a nested dict produced by the verify_spec handler). This ensures the renderer works regardless of whether these fields are extracted to the top level.

**Output keys consumed by the renderer:**
- `verified`, `summary`, `issues`, `recommendations`, `verification_result` (fallback source)

##### Scenario: Spec verification passed
- **WHEN** the verify_spec step completes with `verified: true`
- **THEN** the renderer displays a green `✓ PASSED` status

##### Scenario: Spec verification failed with issues
- **WHEN** the verify_spec step completes with `verified: false` and issues of mixed severity
- **THEN** issues are grouped by severity (error, warning, info) with appropriate color coding
- **AND** each issue shows its message and optional suggestion

#### Update Spec Renderer

The `update_spec` step renderer SHALL display updated specs as a checklist or a no-update message.

**When specs were updated:**
- Each updated spec is shown as `✓ spec-name: change description` (green checkmark, bold name).
- `new_capabilities` are listed as a bullet list under a "New Capabilities" heading.

**When no specs were updated:**
- Displays `No spec updates needed` as a dim message.

**Output keys consumed by the renderer:**
- `updated_specs` (or `specs_updated`), `new_capabilities`

##### Scenario: Specs updated rendering
- **WHEN** the update_spec step completes with one or more entries in `updated_specs`
- **THEN** each spec is rendered as a `✓ name: description` line

##### Scenario: No spec updates
- **WHEN** the update_spec step completes with empty `updated_specs` and empty `new_capabilities`
- **THEN** the renderer displays `No spec updates needed`

#### Commit Renderer

The `commit` step renderer SHALL display commit details or a no-changes message.

**When committed is false:**
- Displays `No changes to commit` as a dim message.

**When committed is true:**
- Top line shows the short commit hash (first 7 characters).
- Commit message is displayed below a separator.

**Output keys consumed by the renderer:**
- `committed`, `commit_hash`, `commit_message`

##### Scenario: Commit displays short hash and message
- **WHEN** the commit step completes with `committed: true`
- **THEN** the renderer displays the short hash on the top line
- **AND** the commit message is displayed below

##### Scenario: No changes committed
- **WHEN** the commit step completes with `committed: false`
- **THEN** the renderer displays `No changes to commit`

### Requirement: Step Output Renderer Registry

The flow engine SHALL provide a centralized registry mechanism for step output rendering, with a single public entry point (`render_step_output(step)`) that dispatches to step-specific renderers or falls back to a generic default renderer.

**Registry Mechanism (`step_renderers.py`):**

- `STEP_RENDERERS: Dict[StepType, Callable[[Step], None]]` — module-level registry mapping step types to their custom renderer functions.
- `register_renderer(step_type)` — decorator that registers a function as the renderer for a given `StepType`.
- `render_step_output(step)` — public entry point: looks up `step.step_type` in `STEP_RENDERERS`; if found, calls the registered renderer; otherwise, falls back to `_default_render(step, title)`.
- `STEP_DISPLAY_TITLES` — module-level dict mapping every `StepType` (including deprecated types `PROJECT_SUMMARY`, `PROPOSE`, `DESIGN`, `PLAN_TASKS`) to a human-readable title used by both custom renderers and the default fallback.

**Default Renderer (`_default_render`):**

When no custom renderer is registered for a step type, the default renderer iterates `step.outputs`, formatting each key-value pair. Long string values (>300 chars) are previewed (first 200 chars) with a length suffix. Non-completed status is shown explicitly. Error messages are rendered in red at the bottom.

**Helper for partial rendering (`_render_remaining`):**

Custom renderers MAY call `_render_remaining(step, title, skip_keys)` to display any output keys not already covered by their specialized rendering. The remaining keys are rendered under a "{title} — Additional Details" header using the same generic formatting as the default renderer.

**Per-step token-usage block:**

When `step.outputs` carries a non-empty `token_usage` total (written by `run_step`; see *Step-Scoped Token Usage Aggregation*), `render_step_output` SHALL append a compact, aligned token/cost usage summary block at the end of that step's report — labeling the input/output tokens, the `cache_read` / `cache_creation` breakdown, and the `total_cost_usd` cost with clear units rather than bare numbers — rendered through the shared `display.py` block primitive so it follows the established Block Rendering Visual Style. When `step.outputs` has no `token_usage` or it is empty, no usage block is rendered. This big-block behaviour governs the non-interactive steps; the interactive multi-round steps (`discovery`, `confirm`) instead show a compact inline footer (see *Interactive Per-Round Token Usage Footer*) and are routed away from this block by `CliSink`.

**Registered renderers beyond the base set:**

In addition to `analyze`, `self_check`, `verify_spec`, `update_spec`, `commit`, and `implement` (covered under the **Step Output Rendering** Requirement), the registry SHALL include custom renderers for the following step types:

#### Plan Renderer (`PLAN`)

Renders three sections in order when data is present:

1. **Proposal** — when `outputs["plan"]["proposal"]` is a dict, delegates to `render_proposal()`; when a string, renders as a labeled line under title "Planning — Proposal".
2. **Design** — when `outputs["plan"]["design"]` is a dict, delegates to `render_design()`; when a string, renders as a labeled line under title "Planning — Design".
3. **Task Groups** — for each group in `outputs["task_groups"]`, renders a line containing group_id (bold), name, task count, total estimated LOC (summed from each task's `estimated_loc`), and dependencies (comma-separated `depends_on` list, or "none").

After the three sections, `_render_remaining` is invoked with skip set `{"plan", "task_groups", "total_complexity", "estimated_effort"}` to render any other output keys. If none of the three sections rendered anything, falls back to `_default_render`.

#### Version Analyze Renderer (`VERSION_ANALYZE`)

Renders:
- **Top line** — `current_version → suggested_version` (with `suggested_version` styled as cyan bold, marking it as authoritative).
- **Sub-line** — dim style showing `{bump_type} bump  │  confidence: {confidence}`.
- **Reasoning** — when present, rendered as a labeled section below a dim horizontal separator.
- **Error** — when `step.error_message` is present, rendered in red.

Rendered under title "Version Analysis".

#### Summarize Renderer (`SUMMARIZE`)

When `outputs["summary"]` is non-empty, delegates to `render_markdown(summary, title="Work Summary")`. When absent, renders nothing.

#### Test Renderer (`TEST`)

When `outputs["test_results"]` is not a dict, falls back to `_default_render`. Otherwise renders:
- **Status line** — `PASSED` (green bold) or `FAILED` (red bold) based on `overall_passed` (or legacy `passed`).
- **Phases line** — count of passed vs. failed phases, followed by a per-phase bullet list with `✓` (green) / `✗` (red) indicators and phase names.
- **Command line** — the underlying test command, when present.

Rendered under title "Testing".

The summary-building logic (status line, phases line, command line) MUST be factored into a shared helper (`_build_test_summary_lines(test_results)`) so the `SPEC_GATE` renderer can reuse the identical summary-only presentation rather than re-implementing it or dumping the raw pytest output.

#### Spec Gate Renderer (`SPEC_GATE`)

Renders the spec_gate result as a **gate-conclusion summary**, never as a raw dump of the phase-2 re-test output. Because the spec_gate step writes the full `verdict.test_results` (including the raw pytest stdout/stderr) into `step.outputs`, the registry MUST provide a dedicated renderer so the step does NOT fall through to `_default_render`, which would dump the entire `test_results` dict (raw stdout/stderr included) and overwhelm the CLI reader. The renderer renders, in order:

1. **Gate conclusion** — a `PASSED` / `FAILED` status line derived from `outputs["gate_passed"]`; when `outputs["gate_skipped"]` is true the line is annotated as a no-op skip (no spec change, the gate ran neither the artifact check nor the re-test).
2. **Route annotation** — when `outputs["gate_route"]` is `update_spec` it notes the fallback to `update_spec` (invalid spec artifact); when it is `implement` it notes the fallback to `implement` (a spec edit broke a test). No route line is rendered when the gate passed.
3. **Fix instructions** — when the gate did not pass and `outputs["fix_instructions"]` is present, the instructions text (not the raw test output).
4. **Test summary** — when `outputs["test_results"]` is a non-empty dict (the phase-2 re-test ran), the **same** summary lines the `TEST` renderer produces via the shared `_build_test_summary_lines` helper (overall PASSED/FAILED, phase pass/fail counts and list, command) — the raw pytest stdout/stderr is NOT rendered.
5. **Error** — when `step.error_message` is present, rendered in red.

Rendered under title "Spec Gate". The no-op skip path and the `update_spec` route (an invalid artifact caught in phase 1, before any re-test) carry no `test_results`, so only the gate conclusion is rendered for them.

#### Propose Renderer (`PROPOSE`, deprecated)

Searches `outputs` for the first present key in `("proposal", "proposal_data")` that maps to a dict; when found, delegates to `render_proposal()`, then renders remaining outputs via `_render_remaining` with skip set `{proposal_key, "summary", "files_to_modify", "files_to_create"}`. When no proposal dict is found, falls back to `_default_render`.

This renderer is retained for backward compatibility with persisted flows containing pre-unification `PROPOSE` steps (see **Deprecated Step Type Backward Compatibility** Requirement).

#### Design Renderer (`DESIGN`, deprecated)

Searches `outputs` for the first present key in `("design", "design_doc", "design_document")` that maps to a dict; when found, delegates to `render_design()`, then renders remaining outputs via `_render_remaining` with skip set `{design_key, "decisions", "components", "implementation_plan"}`. When no design dict is found, falls back to `_default_render`.

Retained for backward compatibility with persisted flows containing pre-unification `DESIGN` steps.

#### Scenario: Custom renderer dispatch
- **WHEN** `render_step_output(step)` is invoked with `step.step_type` registered in `STEP_RENDERERS`
- **THEN** the registered renderer function is called with the step

#### Scenario: Default renderer fallback
- **WHEN** `render_step_output(step)` is invoked with a step type not in `STEP_RENDERERS`
- **THEN** `_default_render(step, title)` is invoked, iterating all outputs with generic formatting
- **AND** the title is looked up from `STEP_DISPLAY_TITLES`, falling back to `step.step_type.value`

#### Scenario: Plan renderer displays task groups with LOC
- **WHEN** the plan step completes with non-empty `task_groups`
- **THEN** each group renders one line containing group_id, name, task count, summed `estimated_loc`, and dependencies

#### Scenario: Version analyze highlights suggested_version
- **WHEN** the version_analyze step completes
- **THEN** `suggested_version` is rendered in cyan bold, distinguishing it as the authoritative value used by the commit step
- **AND** `bump_type` and `confidence` render as dim auxiliary text

#### Scenario: Summarize renderer uses markdown
- **WHEN** the summarize step completes with non-empty `outputs["summary"]`
- **THEN** the summary is rendered via `render_markdown` under title "Work Summary"

#### Scenario: Test renderer with structured results
- **WHEN** the test step completes with `outputs["test_results"]` containing `phases`
- **THEN** the renderer shows per-phase pass/fail indicators and an overall PASSED/FAILED status line

#### Scenario: Spec gate renderer shows a gate summary, not raw test output
- **WHEN** the spec_gate step completes with `outputs["test_results"]` carrying the full phase-2 re-test verdict (including raw pytest stdout/stderr)
- **THEN** `_render_spec_gate` renders the gate conclusion (PASSED/FAILED, any `update_spec` / `implement` route annotation) followed by the shared `_build_test_summary_lines` summary (overall status, per-phase pass/fail counts and list, command)
- **AND** the raw pytest stdout/stderr is NOT dumped — the step does not fall through to `_default_render`

#### Scenario: Spec gate renderer renders only the conclusion when the gate skipped or routed pre-test
- **WHEN** the spec_gate step returns `gate_skipped=true` (no spec change) or routes to `update_spec` on an invalid artifact, so `outputs` carries no `test_results`
- **THEN** the renderer outputs only the gate-conclusion summary (including the no-op skip annotation or the `update_spec` route note) and invokes no test-summary logic

#### Scenario: Deprecated PROPOSE renderer retained
- **WHEN** a persisted flow with a `PROPOSE` step is loaded and rendered
- **THEN** `_render_propose` extracts the proposal dict (under key `proposal` or `proposal_data`) and delegates to `render_proposal()`
- **AND** remaining outputs are rendered via `_render_remaining`

#### Scenario: Deprecated DESIGN renderer retained
- **WHEN** a persisted flow with a `DESIGN` step is loaded and rendered
- **THEN** `_render_design` extracts the design dict (under key `design`, `design_doc`, or `design_document`) and delegates to `render_design()`
- **AND** remaining outputs are rendered via `_render_remaining`

#### Scenario: Step with token usage appends a usage summary block
- **GIVEN** a step whose `outputs` carries a non-empty `token_usage` total
- **WHEN** `render_step_output(step)` renders that step
- **THEN** an aligned token/cost usage summary block is appended after the step's report, labeling input/output tokens, the `cache_read` / `cache_creation` breakdown, and the `total_cost_usd` cost with clear units, rendered through the shared `display.py` block primitive (Block Rendering Visual Style)

#### Scenario: Step without token usage renders no usage block
- **WHEN** `render_step_output(step)` renders a step whose `outputs` has no `token_usage` key or an empty one
- **THEN** no usage summary block is appended

#### Scenario: CliSink routes interactive steps' usage away from the big block
- **GIVEN** the terminal events for a `discovery`, a `confirm`, and a `plan` step (all in `CliSink`'s `_CLI_SKIP_STEP_TYPES`), plus a non-interactive step
- **WHEN** `CliSink` consumes each terminal event
- **THEN** `discovery` renders the whole-discovery cumulative usage via a dim `format_usage_line` line from `step.outputs["token_usage"]` (the per-round inline footer was already rendered by the handler each round, but the confirmation round issues no LLM call so the cumulative would never be shown without this terminal-event rendering)
- **AND** `confirm` renders the compact dim single-line `format_round_usage_footer` footer from `step.outputs["token_usage"]`, not the big `render_step_usage` block
- **AND** `plan` keeps the established big `render_step_usage` block unchanged, and every non-interactive step keeps appending its big usage block via `render_step_output` as before

### Requirement: Pre-implement Test Baseline

The flow engine SHALL capture a deterministic **baseline of failing tests measured before the `implement` step modifies anything**, so that the `test` and `verify_spec` steps can distinguish **inherited** failures (already red at flow start) from **introduced** failures (regressions caused by this flow). Only introduced failures may drive the fix loop; inherited failures are surfaced (留痕) but never looped. The baseline is the **only** legitimate exemption dimension, and because it is frozen before `implement`'s first write, an introduced regression can never be laundered into it.

This mechanism replaces the retired `se3/state/known_test_failures.json` auto-accumulated known-list (see *Test Step Configuration and Multi-Phase Execution*), which forgave a genuinely new failure after a single occurrence with no human sign-off and no expiry. Provenance is now answered by an empirically measured baseline, never by an LLM's judgement and never by an auto-accumulated store.

**Module (`src/se3/engine/test_baseline.py`):** capture, key computation, and caching live in a standalone module — separate from `steps/test.py` — because capture happens on the state-machine side at flow start while consumption happens inside the `test`/`verify_spec` steps; keeping it standalone avoids a reverse import from the state machine into the test step. It reuses `steps/test.py`'s `_detect_test_command` / `_parse_test_ids` so the baseline command and parsing stay identical to the real test step.

**Background pre-warm (overlap with pre-implement steps):**
- At flow start (`init_flow` → `_start_baseline_capture`), the full test suite is launched as a **background subprocess** concurrently with the LLM/network-bound `analyze → plan → confirm` steps, which leave CPU idle. The suite runtime is hidden under that pre-implement work, so a typical flow adds ~0 wall-clock.
- The capture sets `SE3_TEST_RUNNING=1` in the child environment so a test that itself invokes the se3 test handler is short-circuited rather than recursing into another full suite run.
- `BaselineCapture.launch()` is idempotent and never raises; a launch failure is recorded and surfaced later as the `None` sentinel from `wait`/`wait_or_kill`.

**Freeze point before `implement` (`_ensure_baseline_ready`):**
- `state_machine.run_step` calls `_ensure_baseline_ready(flow)` before dispatching the `IMPLEMENT` handler (idempotent across fix-loop re-entries into implement). The snapshot point is the repo state at flow start; `analyze`/`plan`/`confirm` do not write source, so launching at flow creation is correct.
- Resolution order: (1) await the background handle, time-bounded by `resolve_baseline_timeout` (derived from `test.timeout`, default 1800s) so a hung suite is killed rather than blocking the flow forever; (2) on capture failure (`None` sentinel) re-measure **synchronously** once as the authoritative measurement; (3) if even the synchronous run fails or times out, fall back to an **empty** baseline with a loud warning — the safe failure mode, since every failure is then treated as introduced (never the reverse). A timed-out capture skips the synchronous retry because a hung suite would just hang again.
- The `None` sentinel (capture could not run / produced no parseable output while exiting non-zero) is distinct from an empty set (suite ran, zero failures); only the former triggers the synchronous fallback, so an unmeasured baseline is never mistaken for "no failures".

**Persistence and caching:**
- The measured set is stored on `State.baseline_failures` (see *State Tracking Fields and Helper API*) and persisted in `engine.json`, so `--resume` reuses it without re-measuring. `None` means "not yet captured"; `[]` means "captured, zero failures".
- The baseline is cached at `se3/state/test_baseline_cache.json` (atomic tempfile + `os.replace` write, corruption-tolerant, gitignored under the `/se3/*` rule) keyed by `compute_baseline_key` = git HEAD sha + a working-tree dirty hash (`git diff HEAD` plus the names and content hashes of untracked non-ignored files). Either a HEAD change or any tracked/untracked content change yields a different key, so a stale baseline can never be reused across a content change. The cache is bounded to `MAX_CACHE_ENTRIES` most-recently-saved keys (insertion-order LRU). In a serial commit-per-flow pipeline the cache usually misses (each flow lands on a fresh commit); the background overlap is the real cost-hider and the cache is a bonus for parallel/resumed flows on the same commit.

**Injection into step inputs:** `state_machine._build_step_inputs` injects `baseline_failures` (a list) into the `TEST` and `VERIFY_SPEC` step inputs so both steps consume the same frozen set.

**Cleanup:** `cleanup_baseline_capture()` terminates any still-running baseline subprocess when a flow ends before reaching `implement` (e.g. Abort/Exit at a confirm pause), so a background suite is not orphaned.

#### Scenario: Baseline captured before implement
- **WHEN** a flow starts and reaches the `implement` step for the first time
- **THEN** `state.baseline_failures` is a non-`None` list of the test IDs that were failing at flow start, measured before `implement`'s first write
- **AND** the measurement either resolves the background capture launched at flow start or, on capture failure, re-measures synchronously

#### Scenario: Introduced failure cannot launder into the baseline
- **GIVEN** the repo had a frozen baseline measured before `implement`
- **WHEN** `implement` introduces a test regression whose test ID is NOT in `state.baseline_failures`
- **THEN** that failure is classified as introduced and drives the fix loop; it is never added to the baseline

#### Scenario: Baseline reused on resume
- **GIVEN** a flow that already captured `state.baseline_failures` in an earlier session
- **WHEN** the flow is resumed with `--resume`
- **THEN** `_ensure_baseline_ready` returns immediately without re-measuring (the persisted baseline is reused)

#### Scenario: Cache keyed by HEAD sha and working-tree dirty hash
- **GIVEN** a baseline was measured and cached for the current `compute_baseline_key`
- **WHEN** another flow starts on the same commit with an unchanged working tree
- **THEN** the cached failing-id set is reused (cache hit) and no new suite run is launched for the baseline
- **AND** any change to tracked or untracked content changes the key, forcing a fresh measurement

#### Scenario: Unmeasurable baseline falls back to empty with warning
- **GIVEN** the background capture failed and a synchronous re-measurement also fails or times out
- **WHEN** `_ensure_baseline_ready` resolves
- **THEN** `state.baseline_failures` is set to an empty list and a loud warning is logged
- **AND** every subsequent failure is treated as introduced (the safe direction — a real regression is never silently forgiven)

### Requirement: Test Step Configuration and Multi-Phase Execution

The test step SHALL support multi-phase testing via the `test:` configuration section of `se3.yaml`, and output structured results.

**se3.yaml configuration:**
```yaml
test:
  command: null                # Main test command (null=auto-detect)
  timeout: 1800                # Seconds (fallback value when dynamic timeout is unavailable)
  timeout_multiplier: 2.0      # Dynamic timeout multiplier (multiplied with implement's estimated_test_duration)
  min_dynamic_timeout: 30      # Dynamic timeout lower bound (seconds)
  max_dynamic_timeout: 14400   # Dynamic timeout upper bound (seconds, prevents runaway estimates)
  phases:                      # Additional test phases
    - name: "e2e"
      command: "python -m pytest tests/e2e -v"
      cwd: null                # Working directory (null=project root, supports absolute/relative paths)
      timeout: 600
      required: false          # false=failure only warns
      in_fix_loop: false       # false=skipped in fix loop
```

**Structured output:**
```json
{
  "new_tests": {"passed": [...], "failed": [...], "count": 0},
  "regression": {"passed": [...], "failed": [...], "count": 0},
  "phases": [{"name": "default", "passed": true, ...}],
  "overall_passed": true,
  "introduced_failures": [],
  "inherited_failures": [],
  "tests_blocking": false,
  "critical_skipped": [],
  "critical_missing": [],
  "passed": true
}
```

**Classification logic:**
- `new_tests`: file paths matching the implement step's `tests_added`
- `regression`: all other tests
- `overall_passed`: all `required: true` phases pass **and** the critical acceptance test gate is not triggered (see below). `overall_passed` is **no longer equivalent to** the pytest process's `returncode == 0`: pytest returns 0 for skipped cases, so a truthy return code is insufficient to prove that critical acceptance actually ran. The raw `returncode == 0` is still retained separately in the backward-compat `passed` field, for consumers that need to distinguish the two. `overall_passed` is retained **for reporting only** — it is NOT the fix-loop trigger (any red test, inherited or introduced, makes it false); the loop trigger is `should_fix` (see below).

**Baseline-based provenance split (inherited vs introduced):**

The exemption decision is the frozen pre-implement baseline (see *Pre-implement Test Baseline*), injected into the step inputs as `baseline_failures`. It replaces the retired `se3/state/known_test_failures.json` known-list entirely.

- A failing test is **inherited** iff its test-id ∈ `baseline_failures`; otherwise it is **introduced**. `inherited_failures` and `introduced_failures` are written to `step.outputs["test_results"]`. The legacy `pre_existing_failures` output key is retained for backward compatibility with downstream renderers and now carries the baseline-inherited list.
- New-test failures are always introduced (a test the implement step added cannot have been failing before it existed).
- `should_fix` (the fix-loop trigger) `= any(new_tests failed) OR any(introduced regression, i.e. a regression failure NOT in baseline_failures) OR unparseable failures OR critical_failed OR an in-budget active baseline failure (mechanism B, see below)`. **All** introduced failures must be fixed; there is no severity floor and no known-list exemption. `should_fix` is also published as `test_results["tests_blocking"]` so `verify_spec` consumes the exact same baseline-based verdict rather than recomputing it (single source of truth).
- The "introduced or critical" group (new-test failures, introduced regressions, unparseable failures, skipped/missing critical acceptance tests) keeps the **normal** fix-loop guardrails with no scope relaxation. The mechanism-B baseline-looping relaxation (see below) is the only exception and applies ONLY to the specifically-annotated baseline failures.
- The `known_test_failures.json` load/save and auto-population are removed. The store is no longer read or written by the test step; any remaining references in other modules (`issue_discovery.py`, `merge/runtime_sync.py`) are migrated to the baseline or removed.
- The new-vs-regression split (`_classify_results`) is unchanged.

**Mechanism B — bounded looping of inherited (baseline) failures:**

Historically inherited failures were surfaced (留痕) but **never** looped, so a project's pre-existing red tests stayed red forever (the "baseline zombie" gap: a failure inherited at every flow start is never anyone's regression and is therefore never fixed). Mechanism B lets the fix loop **also** attempt inherited baseline failures, under a strictly bounded, independently-budgeted, and persistently-given-up policy. The shared test core (`run_and_classify_tests`) implements it; both the `test` step and the mechanism-A SPEC_GATE re-test consume the result through the same code path.

- **Active baseline set.** `active_baseline = inherited_failures − given_up`, where `given_up` is the cross-flow persistent set loaded via `baseline_fix_memory.load_given_up` (see base *Engine Module Extensions*). Only failures in `active_baseline` are eligible to loop; a given-up id is surfaced but never re-attempted.
- **Independent per-flow budget.** Looping is gated by `workflow.baseline_fix_max_attempts` (default `3`; see se3-config *Workflow Configuration*), a budget **independent of** the possibly-unlimited global `workflow.max_fix_iterations`. The per-flow attempt count lives in `flow.state.context["baseline_fix_attempts"]` and is incremented by the state machine each time it transitions to a fix that targeted baseline failures. `baseline_should_loop` is true iff the budget is enabled (`> 0`) and `baseline_fix_attempts < baseline_fix_max_attempts` **and** `active_baseline` is non-empty. A budget of `0` disables baseline looping entirely (pure surface, the historical behavior).
- **should_fix folds in baseline.** When `baseline_should_loop` is true, `should_fix`/`tests_blocking` is `True` even if the only failures are inherited — so a flow with nothing but in-budget baseline failures enters the fix loop (returns `REVISION_NEEDED`) instead of completing.
- **Targeted, bounded unlock semantics.** When the loop runs because of baseline failures, `fix_instructions` is **prepended** with a dedicated baseline section (analogous to the critical-acceptance prefix) that: lists the exact active baseline failure ids; states they MUST ALSO be fixed and are to be treated with **equal priority to the main task — worked on in PARALLEL, never preempting it**; and grants a scope relaxation that applies **ONLY to those listed ids** — the agent MAY step beyond the user-prompt's stated scope / focus limits **only as far as needed** to fix the listed baseline failures. The relaxation explicitly does NOT extend to introduced failures or anything else, and it MUST NOT cross any se3 guardrail (no deleting, weakening, or modifying any spec's SHALL/MUST contracts — the spec guardrails apply in full). The section is code-first: do NOT revert a legitimate spec/code change to placate a brittle test; when a test is stale relative to a correct change, the right fix is to UPDATE the test (e.g. `44 → 45`). The annotated ids are also recorded in `fix_context["baseline_failures_targeted"]`, which is the signal the state machine uses to increment `baseline_fix_attempts`; the unlock applies to exactly that annotated set.
- **Budget exhaustion → persistent give-up.** When baseline looping was enabled and the per-flow budget is exhausted without the active baseline failures being fixed, those ids are recorded as given-up via `baseline_fix_memory.record_given_up` (accumulating the attempt count and a reason) and surfaced **without** looping. This double cap (per-flow budget + cross-flow persistent memory) prevents every subsequent flow from re-attempting the same fundamentally un-fixable baseline failure — a missing system library, a flaky test, or one needing a human decision.
- **Always leave a trace.** Inherited failures are logged (留痕) every round regardless of whether they are also being looped, and `test_results` carries `inherited_failures`, `introduced_failures`, and `active_baseline` (the inherited−given_up subset) for transparency.

**Critical Acceptance Test skip/missing detection:**

When `test.critical_tests` (a list of test ID/substring patterns for critical acceptance tests, see se3-config *Test Configuration*) is configured, the test step SHALL check the per-result output of this pytest run against two bypass paths; on any hit it sets `overall_passed` (and `tests_passed`) to `False`, includes it in `should_fix`, and returns `REVISION_NEEDED` to trigger the fix loop:

- **Skipped (critical_skipped):** `_parse_skipped_test_ids()` parses `file::test SKIPPED` lines from the verbose output; if a critical pattern matches one or more SKIPPED cases, those test IDs are counted in `critical_skipped` (for critical acceptance tests, `skip != pass`).
- **Missing (critical_missing):** if a critical pattern matches neither any actually-run (PASSED/FAILED) case nor any SKIPPED case in this run's results, then that pattern is counted in `critical_missing`. This covers the bypass path where a critical case is renamed / silently dropped due to an import failure / removed from output because of a typo in the pattern, while `returncode` remains 0.

False-positive prevention and verbose prerequisite for missing detection:

- Missing detection only takes effect when this run produced parseable per-result output (at least one of `ran_ids` or `skipped_ids` is non-empty). When `critical_tests` is non-empty but no per-result output can be parsed (the command is not verbose), a warning is logged and missing detection is skipped, to avoid mis-flagging every critical pattern as missing.
- `_detect_critical_failures(ran_ids, skipped_ids, critical_patterns)` returns `(critical_skipped, critical_missing)`, both of which are empty when `critical_patterns` is empty.
- When `critical_tests` is configured and the main command is a recognizable pytest invocation but lacks a per-test verbose flag (`-v`/`-vv`/`-vvv`/`--verbose`), `_ensure_verbose_pytest()` appends `-v` to ensure skips are rendered as parseable `file::test SKIPPED` lines (the `-r` report flag only produces short summary lines that cannot be matched by case name, and is not accepted as sufficient); when the command does not look like pytest, a warning is logged and it is returned as-is.
- Ordinary non-critical skips are unaffected: skips not matched by any critical pattern do not enter `critical_skipped`/`critical_missing` and do not trigger this path.

`critical_skipped` / `critical_missing` are also written to `step.outputs["test_results"]`, to be explicitly consumed by downstream verify_spec as an honest signal for the authoritative `verified` computation (see *verify_spec Unified Priority and Scope Mechanism*).

**Passed-phase chat-history archive slimming:**

When the test step's `test_results` is archived into the chat-history jsonl (via the `HistorySink` `step_completed` event, which persists the full `step.outputs["test_results"]`), a `passed: true` phase no longer stores its full verbose `pytest -v` stdout. After all *live* uses of the verbose output have completed (`_classify_results`, the critical-acceptance gate, and `_extract_failures_section` for fix instructions — these read the local `primary_result['stdout']`, decoupled from the stored copy), the step slims the stored `stdout`/`stderr` of each passed phase (both the top-level and the per-`phases[]` copies) down to a **result summary** — a passed/failed count plus a bounded truncated tail of the output — using the centralized `TEST_HISTORY_PASSED_SUMMARY_TAIL_CHARS` constant in `se3/engine/truncation.py`. **Failed phases retain their existing truncated-tail behavior unchanged.** Independent verdict fields (`critical_skipped`, `critical_missing`, etc.) are preserved and do not depend on the slimmed stdout. This bounds `engine.json` / history-jsonl growth without losing the diagnostic detail of failures.

**verify_spec consumes test_mapping:**
- compares `test_mapping` values against the list of scenarios in the spec
- uncovered scenarios are recorded as warning-level issues

#### Scenario: Default behavior without configuration
- **WHEN** `se3.yaml` does not contain a `test:` configuration
- **THEN** the auto-detected test command is used (existing behavior)
- **AND** all tests are classified into the `regression` category

#### Scenario: Multi-phase test execution
- **WHEN** multiple `phases` are configured
- **THEN** each phase is executed in order
- **AND** each phase result is recorded independently
- **AND** `overall_passed` is based on `required: true` phases

#### Scenario: Selective execution in the fix loop
- **WHEN** the test step executes within a fix iteration
- **THEN** phases with `in_fix_loop: false` are skipped

#### Scenario: Test failure triggers the fix loop
- **WHEN** the test step finishes executing and there are new test failures, introduced regressions (failures NOT present in the pre-implement `baseline_failures`), or unparseable failures
- **THEN** the test step returns `REVISION_NEEDED` status
- **AND** the flow goes directly into the fix loop and returns to the implement step
- **AND** the verify_spec step is skipped (because the problem was already found by tests)
- **AND** the fix instructions include diagnostic information intelligently extracted by `_extract_failures_section()` (FAILURES/ERRORS sections), rather than a simple tail truncation of stdout

#### Scenario: Inherited (baseline) failures surfaced when not loopable
- **WHEN** the test step finishes executing and `overall_passed` is false
- **AND** every failing test is **inherited** — its test-id is present in the frozen pre-implement `baseline_failures`
- **AND** none of them is loopable under mechanism B — each is either already given-up (in the cross-flow `baseline_fix_memory`) or the per-flow `baseline_fix_max_attempts` budget is exhausted or disabled (`0`)
- **THEN** `should_fix`/`tests_blocking` is `False` and the test step returns `COMPLETED` status (does not trigger the fix loop)
- **AND** `step.outputs["test_results"]["inherited_failures"]` (and the legacy `pre_existing_failures` key) record these inherited failures
- **AND** a medium-priority issue reporting these inherited failures is created via A-class issue discovery **at most once per flow** (deduped via `context["inherited_failures_filed"]`), never re-filed each iteration
- **AND** the inherited failures are logged once (留痕) noting they are present in the pre-implement baseline and were NOT introduced by this flow

#### Scenario: In-budget inherited (baseline) failure enters the fix loop (mechanism B)
- **GIVEN** `workflow.baseline_fix_max_attempts` is `> 0` (default 3)
- **WHEN** the test step finishes executing and the only failures are **inherited** (in `baseline_failures`)
- **AND** at least one is in `active_baseline` (inherited minus the given-up set) and the per-flow `baseline_fix_attempts` count is below the budget
- **THEN** `should_fix`/`tests_blocking` is `True` and the test step returns `REVISION_NEEDED`, entering the fix loop
- **AND** `fix_instructions` is prepended with a baseline section listing exactly those active baseline failure ids, stating they MUST ALSO be fixed with EQUAL priority to the main task (handled in PARALLEL, not preempting it)
- **AND** the scope relaxation in that section applies ONLY to the listed baseline ids (it may step beyond the user-prompt's focus limits only as far as needed for them) and explicitly does NOT cross any se3 guardrail SHALL/MUST contract
- **AND** `fix_context["baseline_failures_targeted"]` records exactly the annotated ids, and the state machine increments `flow.state.context["baseline_fix_attempts"]` on the resulting transition to the fix step

#### Scenario: Baseline-fix budget exhaustion records a persistent give-up
- **GIVEN** baseline looping was enabled and the per-flow `baseline_fix_attempts` count has reached `workflow.baseline_fix_max_attempts`
- **WHEN** the test step finishes executing with the same active baseline failures still red
- **THEN** those ids are recorded as given-up via `baseline_fix_memory.record_given_up` (accumulating the attempt count and a reason), so subsequent flows skip looping them
- **AND** they are surfaced (留痕) without looping — `should_fix` is `False` for a pure-baseline run at this point
- **AND** the independent baseline budget is NOT shared with the (possibly unlimited) global `max_fix_iterations`

#### Scenario: Given-up baseline failure is never re-looped across flows
- **GIVEN** a baseline failure id was recorded as given-up in `se3/state/baseline_fix_attempts.json` by an earlier flow
- **WHEN** a later flow on the same project finds that test still failing at baseline
- **THEN** it is excluded from `active_baseline` and the fix loop does NOT re-attempt it
- **AND** it is still surfaced (留痕) so the failure remains visible

#### Scenario: baseline_fix_max_attempts=0 disables baseline looping
- **GIVEN** `workflow.baseline_fix_max_attempts: 0` in se3.yaml
- **WHEN** the test step finishes with only inherited failures
- **THEN** mechanism B is disabled — `should_fix` is `False` and the inherited failures are surfaced (留痕) without ever entering the fix loop (the historical surface-only behavior)

#### Scenario: Introduced (non-baseline) failure triggers the fix loop
- **WHEN** the test step finishes executing and at least one failing test is **introduced** — its test-id is NOT in `baseline_failures` (or it is a new-test failure)
- **THEN** `should_fix`/`tests_blocking` is `True` and the test step returns `REVISION_NEEDED`, entering the fix loop
- **AND** an introduced failure can never be exempted, because the baseline was frozen before `implement`'s first write

#### Scenario: known_test_failures.json is no longer auto-populated
- **WHEN** the test step finishes executing with regression failures
- **THEN** the test step does NOT read or write `se3/state/known_test_failures.json` — the load/save helpers and the auto-population step are removed
- **AND** the provenance/exemption decision is the measured `baseline_failures` set, not an auto-accumulated known-list (closing the laundering vector where a genuinely new failure became "known" after a single occurrence)

#### Scenario: Intelligent failure diagnostic extraction
- **WHEN** the test step needs to build fix instructions
- **THEN** `_extract_failures_section()` locates the `= FAILURES =` or `= ERRORS =` section in the pytest output
- **AND** if the section content exceeds max_chars (default 3000), it is truncated by test block, retaining the assertion and the tail of the traceback
- **AND** if no FAILURES/ERRORS section is found, it falls back to tail truncation of stdout

#### Scenario: Revision flow previous_output serialization
- **WHEN** the state machine transitions to a revision (confirm review loop or fix loop)
- **AND** the revised step's `previous_output` is serialized to JSON for the LLM prompt
- **THEN** `json.dumps` MUST use `default=str` as a defensive fallback
- **AND** step handlers that store `StepStatus` in `step.outputs["result"]` MUST convert it to its string `.value` before storing (root cause prevention)

#### Fix History Structure

When the state machine records a fix loop iteration in `fix_history`, each entry SHALL store structured issue data instead of a truncated text summary of fix_instructions.

**Fix history entry schema:**
```json
{
  "iteration": 1,
  "trigger_step_type": "test|self_check|verify_spec",
  "implement_step_id": "...",
  "reason": "test_failure|spec_compliance|...",
  "issues": [
    {"severity": "high", "priority": "high", "description": "...", "location": "...", "message": "..."}
  ]
}
```

**Issue field normalization (`_normalize_issue_fields`):**
- self_check issues use `severity`; verify_spec issues use `priority`. The state machine normalizes both onto every issue dict before storing in fix_history, so downstream consumers (e.g., `_format_fix_history` in the implement step) can use a single canonical field.
- If `severity` is present but `priority` is absent, `priority` is set to `severity`'s value, and vice versa.

**Fix history formatting for implement prompts:**
- `_format_fix_history()` renders each iteration using the structured `issues` list (showing up to 5 issues with severity, description, and location) rather than a truncated text summary.
- The `issues` list is already capped at 10 entries per iteration (via `_cap_issue_list`), keeping the prompt bounded regardless of LLM verbosity.
- Backward compatibility — legacy `fix_instructions_summary` read path: old fix_history entries persisted before the structured-issues migration may carry a `fix_instructions_summary` string instead of an `issues` list. `_format_fix_history()` SHALL treat this field as an optional fallback: when an iteration entry has no `issues` list (or the list is empty) but does carry a non-empty `fix_instructions_summary`, the formatter renders that summary text in place of the issues block so the legacy entry remains usable in the implement prompt. This read path exists solely to keep pre-migration flows renderable; new entries written by the state machine SHALL NOT include `fix_instructions_summary`, and downstream tooling SHOULD NOT rely on its presence.

**Source-aware storage policy:**
- Fix instructions from test.py (raw test output: "Tests are failing..." + failures + stderr) are NOT stored in fix_history — the `reason` field ("test_failure") records the trigger, and current test output is always available in the next iteration.
- Fix instructions from verify_spec (LLM-generated analysis and repair guidance) are preserved via the structured `issues` list, which captures the LLM's diagnostic intent for avoiding repeated fix directions.

**Sliding-window cap (`FIX_HISTORY_MAX_ENTRIES`):**

The `State.fix_history` list SHALL be capped at `FIX_HISTORY_MAX_ENTRIES` (defined in `se3/engine/models.py`, default `100`) by a tail-keep policy: when an append via `State.increment_fix_iteration()` causes the list to exceed the cap, the oldest entries are dropped so only the most recent `FIX_HISTORY_MAX_ENTRIES` entries are retained. The same tail-keep policy is applied at deserialization time (`State.from_dict`) so `engine.json` files written by older builds without the cap are clamped on load.

**Rationale:** the cap bounds memory, on-disk `engine.json` size, and per-transition deepcopy cost in unlimited mode (`workflow.max_fix_iterations = 0`). The default value is held at the same numeric floor as `DEFAULT_MAX_FIX_ITERATIONS` so a flow running with the default fix-loop bound never silently drops history entries — trimming can only happen when the user explicitly raises `workflow.max_fix_iterations` above the cap (e.g. 200) or runs in unlimited mode for more than `FIX_HISTORY_MAX_ENTRIES` iterations.

**Downstream impact on prompts:**
- The shared `render_fix_context()` helper used by verify_spec and self_check accepts `fix_history` as a parameter but deliberately does NOT render it into the `{fix_context}` block. Only `prev_issues` (the issues reported in the immediately preceding review pass) is rendered. The `fix_history` parameter is kept in the signature for back-compatibility with existing callers and treated as an explicit no-op (`_ = fix_history`) inside the function. **Rationale:** the iteration-count + trigger-type metadata in fix_history biases self_check / verify_spec reviewers toward over-flagging ("we've been at this N rounds, surely something is still wrong"); for review steps the signal is more harmful than helpful, so the cap on fix_history has no impact on review-step prompts regardless of size.
- `implement._format_fix_history` iterates the full persisted list; for iterations beyond `FIX_HISTORY_MAX_ENTRIES`, early-iteration entries are dropped from the implement-step prompt. A fix loop running past this point on the same task is considered a stuck loop where ancient history adds noise rather than signal.

#### Scenario: fix_history capped at FIX_HISTORY_MAX_ENTRIES on append
- **GIVEN** `State.fix_history` already contains `FIX_HISTORY_MAX_ENTRIES` entries
- **WHEN** `State.increment_fix_iteration()` appends another entry
- **THEN** the list is trimmed back to `FIX_HISTORY_MAX_ENTRIES` entries by dropping the oldest
- **AND** the most recently appended entry is retained as the last element

#### Scenario: fix_history capped on deserialization
- **GIVEN** an `engine.json` written by an older build whose persisted `fix_history` exceeds `FIX_HISTORY_MAX_ENTRIES`
- **WHEN** `State.from_dict()` loads the file
- **THEN** the loaded `fix_history` is clamped to the most recent `FIX_HISTORY_MAX_ENTRIES` entries via the same tail-keep policy

#### Scenario: Fix history stores structured issues
- **WHEN** the state machine records a fix loop entry from verify_spec
- **THEN** the entry's `issues` list contains normalized issue dicts with both `severity` and `priority` fields
- **AND** no `fix_instructions_summary` field is stored

#### Scenario: Fix history prev_issues cap aligned at 20
- **WHEN** the state machine builds inputs for the verify_spec step during a fix iteration
- **THEN** `prev_issues` is capped at 20 entries, matching the verify_spec prompt's display limit

#### Scenario: prev_issues render-time tail cap in fix-context block
- **GIVEN** the shared `render_fix_context()` helper (`se3/engine/steps/_fix_context.py`), consumed by both `verify_spec` and `self_check` to render the `{fix_context}` slot of their LLM prompts
- **WHEN** the `prev_issues` list passed in contains more than `PREV_ISSUES_RENDER_TAIL` (default 20) entries
- **THEN** only the first `PREV_ISSUES_RENDER_TAIL` issues are rendered into the `## Previously Reported Issues` section of the prompt
- **AND** a trailing line of the form `- ... and N more issues (truncated)` is appended, where N is `len(prev_issues) - PREV_ISSUES_RENDER_TAIL`
- **AND** this render-time cap is independent of the state-machine input-plumbing cap (also 20): both defaults are deliberately set to the same value so the two layers stay aligned, while the render-time cap acts as the last line of defense if any upstream caller bypasses the input-plumbing cap
- **AND** `PREV_ISSUES_RENDER_TAIL` is exposed at module level in `_fix_context.py` so tests and other consumers can reference it by name rather than relying on positional substring matches

#### Scenario: Passed phase archive is slimmed to a summary
- **GIVEN** a test phase whose `passed` is `true` produced verbose `pytest -v` stdout
- **WHEN** the test step archives `test_results` into the chat-history jsonl
- **THEN** the stored `stdout`/`stderr` for that passed phase (top-level and the per-`phases[]` copy) is replaced with a result summary — a passed/failed count plus a bounded truncated tail using `TEST_HISTORY_PASSED_SUMMARY_TAIL_CHARS` — rather than the full verbose stdout
- **AND** the slimming runs only after all live consumers (`_classify_results`, the critical-acceptance gate, `_extract_failures_section`) have read the output
- **AND** a `passed: false` phase keeps its existing truncated-tail behavior unchanged

#### Scenario: Code self-check after tests pass
- **WHEN** the test step finishes executing and `overall_passed` is true
- **THEN** the test step returns `COMPLETED` status
- **AND** the flow continues to the self_check step for LLM code review (for the feature/bugfix/discovery workflows)
- **AND** after self_check passes, it continues to the verify_spec step for spec compliance checking

#### Scenario: verify_spec checks spec coverage
- **WHEN** verify_spec receives `test_mapping`
- **THEN** it checks the test coverage of spec scenarios
- **AND** uncovered scenarios are recorded as warnings

#### Scenario: Skipped critical acceptance test triggers the fix loop
- **GIVEN** `test.critical_tests` is configured with the pattern of a critical acceptance test
- **WHEN** after the test step runs, that critical case is SKIPPED in the output (even if pytest `returncode == 0`)
- **THEN** the test step sets `overall_passed`/`tests_passed` to `False` and returns `REVISION_NEEDED`
- **AND** `test_results["critical_skipped"]` contains the test ID of that skipped case
- **AND** the fix instructions state that critical acceptance tests must not be skipped and must actually run

#### Scenario: Missing critical acceptance test triggers the fix loop
- **GIVEN** `test.critical_tests` is configured with a critical pattern, and this run produced parseable per-result output
- **WHEN** that pattern matches neither any actually-run (PASSED/FAILED) case nor any SKIPPED case (renamed / dropped / pattern typo)
- **THEN** the test step sets `overall_passed`/`tests_passed` to `False` and returns `REVISION_NEEDED`
- **AND** `test_results["critical_missing"]` contains that missing pattern

#### Scenario: Ordinary skip does not trigger the critical gate
- **GIVEN** `test.critical_tests` is configured
- **WHEN** the cases skipped in this run do not match any critical pattern (e.g. platform/optional-dependency skips)
- **THEN** both `critical_skipped` and `critical_missing` are empty
- **AND** the test step does not trigger the fix loop because of these skips

#### Scenario: No missing false positives when results are unparseable
- **GIVEN** `test.critical_tests` is non-empty, but this run's main command is not verbose and no per-result output can be parsed (`ran_ids` and `skipped_ids` are both empty)
- **THEN** missing detection is skipped and `critical_missing` is empty (no false positives)
- **AND** a warning noting the verbose prerequisite is logged

### Requirement: Post-update_spec Spec Verification Gate

The flow engine SHALL run a `spec_gate` step (mechanism A) immediately after `update_spec` in the `feature` and `discovery` step sequences (statically inserted between `update_spec` and `version_analyze`). The gate closes the root-cause gap whereby a spec edited by `update_spec` (a non-read-only step that uses Edit to rewrite `spec.md`) was followed by **no further test step**, so a spec-content test broken by that edit (e.g. a hard-coded requirement-count assertion) was committed unre-tested and froze into a permanent inherited "zombie" failure for every later flow.

`spec_gate` is a pure program step (`uses_llm=False`, `read_only=False`). It is implemented in `steps/spec_gate.py` (see base *Engine Step Implementations*) and shares the exhaustion bound of the existing fix loop.

**Stable pre-`update_spec` snapshot.** Before the state machine first dispatches `UPDATE_SPEC`, it captures a stable snapshot of every on-disk spec into `flow.state.context["spec_requirement_baseline"]` via `build_spec_requirement_baseline` (the canonical builder, exported from `steps/spec_gate.py` so the state machine and the handler share one format). The snapshot records, per spec, the full `spec.md` content and its ordered `### Requirement:` name list. It is captured **once per flow** and is NOT re-taken before an `update_spec` redo: re-snapshotting after a bad edit landed on disk would let the gate measure non-decrease against an already-corrupted baseline and wave a deletion through. Because no step before `update_spec` writes specs, this snapshot is the flow's true baseline spec state.

**No-op when nothing changed.** The gate diffs the current on-disk specs against the snapshot to classify them into *edited* (present in the snapshot but with changed content) and *new* (absent from the snapshot). When the flow changed no spec at all, the gate is a no-op and returns `COMPLETED`. When the snapshot is missing from context (a sequence without `update_spec`, or an interrupted/legacy flow), the gate cannot trust its edited-vs-new classification and SHALL skip rather than mis-route, returning `COMPLETED`.

**Phase 1 — programmatic artifact check (no LLM, no test-output parsing).** For every edited or new spec, the content MUST pass `spec_validator.validate_spec_structure` (the spec-format v1 contract). For *edited* specs only, the requirement count and name set MUST NOT shrink relative to the snapshot (a new spec has no prior baseline, so only the structural check applies to it). A structural failure, an unparseable spec, or a removed requirement is an **invalid artifact**: the gate returns `REVISION_NEEDED` with `gate_route = "update_spec"` and `fix_instructions` naming the structural / requirement problems, so the flow routes back to `update_spec` to redo the edit (NOT a code fix loop).

**Phase 2 — full re-test (only when the artifact is clean).** The gate re-runs the **entire** test suite through the shared `steps.test.run_and_classify_tests` core — the same command, phases, dynamic timeout, critical-acceptance gate, and inherited-vs-introduced baseline split as the real `test` step (single source of truth). The full suite is run deliberately (`is_fix_iteration=False`): a spec edit can break any spec-content test, including phases marked `in_fix_loop: false`. No static "spec-related subset" is selected, because there is no reliable marker for such a subset and maintaining one would be a brittle coupling (a future opt-in pytest marker is left for later if performance demands it). When the re-test demands a fix (`verdict.should_fix`), the gate returns `REVISION_NEEDED` with `gate_route = "implement"`, entering the existing fix loop (which may edit code or the stale test). The gate is **code-first**: the fix is to update the implementation or the stale test, NEVER to revert a legitimate spec change to placate a brittle test (the correct resolution for a count assertion is to update the test, e.g. `44 → 45`).

**Routing and bounded exhaustion.** The state machine treats `SPEC_GATE` returning `REVISION_NEEDED` as a fix trigger alongside `TEST` / `SELF_CHECK` / `VERIFY_SPEC`, sharing the same global `max_fix_iterations` exhaustion bound, and dispatches by `gate_route`: `update_spec` → an `update_spec` redo (`_transition_to_update_spec_redo`), anything else (default) → the implement fix loop (`_transition_to_fix`). After an `update_spec` redo completes, normal progression lands the flow back on the `spec_gate` step to re-check the redone spec. The shared `max_fix_iterations` cap guarantees both the redo loop and the test fix loop terminate.

#### Scenario: No spec change makes the gate a no-op
- **GIVEN** a `feature` flow whose `update_spec` step did not change any `spec.md`
- **WHEN** the `spec_gate` step executes
- **THEN** it detects no edited and no new specs and returns `COMPLETED` (`gate_passed=true`, `gate_skipped=true`), running neither the artifact check nor the re-test

#### Scenario: Invalid spec artifact routes back to update_spec
- **GIVEN** `update_spec` edited a spec so that it fails `validate_spec_structure` or dropped a `### Requirement:` present in the pre-`update_spec` snapshot
- **WHEN** the `spec_gate` step runs its phase-1 artifact check
- **THEN** the gate returns `REVISION_NEEDED` with `gate_route = "update_spec"`
- **AND** `fix_instructions` names the structural / requirement-deletion problems and instructs the redo to re-apply the intended update without dropping or weakening any pre-existing requirement
- **AND** the state machine routes the flow back to `update_spec` for a redo, then back into `spec_gate` to re-check

#### Scenario: Requirement non-decrease measured against the stable flow-start baseline
- **GIVEN** the pre-`update_spec` snapshot recorded N requirements for an edited spec
- **WHEN** the gate parses the current spec and finds fewer than N requirements, or a snapshot requirement name is absent
- **THEN** the gate flags the spec as having lost requirement(s) and routes back to `update_spec`
- **AND** the snapshot is the one captured once before the first `UPDATE_SPEC` dispatch (never re-taken on a redo), so a bad edit already on disk cannot become the comparison baseline

#### Scenario: Clean artifact triggers a full re-test
- **GIVEN** every edited/new spec passes the phase-1 artifact check
- **WHEN** the gate runs phase 2
- **THEN** it re-runs the entire test suite through the shared `run_and_classify_tests` core (full suite, not a fix-iteration subset), using the same baseline split as the `test` step

#### Scenario: Spec edit that breaks a test routes to implement (code-first)
- **GIVEN** the phase-1 artifact check passed but the full re-test surfaces an introduced (non-baseline) failure caused by the spec edit (e.g. a hard-coded `== 44` requirement-count assertion now sees 45)
- **WHEN** the gate evaluates the re-test verdict
- **THEN** the gate returns `REVISION_NEEDED` with `gate_route = "implement"`, entering the existing fix loop
- **AND** the guidance is code-first: update the implementation or the stale test (e.g. `44 → 45`), and do NOT revert the legitimate spec change to satisfy the brittle test

#### Scenario: Missing snapshot skips the gate safely
- **GIVEN** `flow.state.context` has no `spec_requirement_baseline` (e.g. a sequence without `update_spec`, or a legacy/interrupted flow)
- **WHEN** the `spec_gate` step executes
- **THEN** it logs a warning and returns `COMPLETED` without attempting an edited-vs-new classification, rather than risk a mis-route

#### Scenario: Gate fix loops share the global exhaustion bound
- **WHEN** repeated `spec_gate` redos or implement fixes are triggered
- **THEN** they count toward the same global `workflow.max_fix_iterations` counter as the TEST / SELF_CHECK / VERIFY_SPEC fix loop, so the gate cannot loop unbounded

### Requirement: Test Dynamic Timeout

The main test command of the test step SHALL support a dynamic timeout mechanism based on the estimate from the implement step, avoiding the use of a single fixed timeout value for all projects.

**Formula:**
```
effective_timeout = clamp(
    estimated_test_duration * timeout_multiplier,
    min = test.min_dynamic_timeout,
    max = test.max_dynamic_timeout,
)
```

**Parameter sources:**
- `estimated_test_duration`: from the JSON output of the implement step (forwarded by the state machine from implement.outputs to test.inputs)
- `timeout_multiplier`: from `test.timeout_multiplier` in `se3.yaml` (default 2.0, clamped to >= 1.0 at load time)
- `min_dynamic_timeout` / `max_dynamic_timeout`: from `se3.yaml` (default 30 / 14400 seconds), preventing the estimate from being too small or from growing out of control within the timeout fix loop

**Fallback rules:**
- When `estimated_test_duration` is missing, not an integer, or <= 0, the main command's timeout falls back to `test.timeout` configured in `se3.yaml` (default 1800 seconds)
- This ensures backward compatibility for legacy projects (where implement does not provide this field) or when the LLM omits this field

**Scope:**
- The dynamic timeout **applies only** to the main test command executed by the test step
- Phases explicitly configured in `phases` are **unaffected** and continue to use the `timeout` value in each phase's own configuration

**Timeout in-place retry (distinguishing a timeout from a real assertion failure):**

A dynamic timeout can be a transient symptom (a momentarily loaded machine, a slow cold cache) rather than a genuine slow/looping test, and is categorically different from an assertion failure. So before the timeout is allowed to enter the fix loop, the test step SHALL first **retry the command in place once** with the same parameters when it detects a timeout-class failure — recognized via the full timeout-signal set: the `timed_out` flag, a `Timeout after` marker in the output, or `returncode == -1`. This in-place retry happens within a single test step execution and is therefore **not** counted as a fix iteration (the fix-loop counter is only incremented by the state machine on a `REVISION_NEEDED` transition). If the in-place retry passes, the step proceeds normally. Only if the retry **still** times out does the step fall through to the timeout-aware fix loop below, and the failure context SHALL explicitly label the failure as a **timeout, NOT an assertion failure** (e.g. via `timed_out_not_assertion` / `timeout_retried` flags), so the implement step does not misread it as a broken assertion. The default `120`s (and other configured) timeout values are not changed by this mechanism.

**Timeout detection and fix loop:**

When the main command is **still** terminated due to the dynamic timeout after the in-place retry (for example, the Python subprocess returns returncode == -1, or stderr contains the `Timeout after` marker), the test step SHALL:

1. Treat the failure as an ordinary test failure, returning `REVISION_NEEDED` to trigger the fix loop
2. Attach the following timeout metadata to `fix_context`:
   - `timeout_reason`: human-readable timeout reason text
   - `previous_timeout`: the actual timeout in seconds used this time
   - `previous_estimated_test_duration`: the estimate used this time (if any)
   - `timeout_multiplier`: the multiplier used this time
   - `timeout_at_cap`: a boolean indicating whether this timeout has reached the `max_dynamic_timeout` cap

The FIX_PROMPT of the implement step SHALL recognize the timeout metadata in `fix_context`, and provide an `estimated_test_duration` value strictly greater than `previous_estimated_test_duration` in the new round of JSON output. This gives the next test execution a larger timeout, breaking the infinite loop of "underestimate -> timeout -> underestimate again".

#### Scenario: Main command uses dynamic timeout
- **GIVEN** implement outputs `estimated_test_duration: 120`
- **AND** `test.timeout_multiplier: 2.0` in `se3.yaml`
- **WHEN** the test step runs the main command
- **THEN** the timeout used by the main command is 240 seconds (120 × 2.0, within the min/max range)

#### Scenario: Fallback when estimated_test_duration is missing
- **GIVEN** the implement output does not contain `estimated_test_duration` (or the value is invalid)
- **WHEN** the test step runs the main command
- **THEN** the main command uses `test.timeout` from `se3.yaml` (default 1800 seconds)

#### Scenario: Dynamic timeout lower-bound clamping
- **GIVEN** implement outputs `estimated_test_duration: 5`
- **AND** `test.timeout_multiplier: 2.0` and `test.min_dynamic_timeout: 30` in `se3.yaml`
- **WHEN** the test step runs the main command
- **THEN** the timeout used by the main command is clamped to 30 seconds (instead of 10 seconds)

#### Scenario: Dynamic timeout upper-bound clamping
- **GIVEN** implement outputs an extremely large `estimated_test_duration`
- **WHEN** the computed result exceeds `test.max_dynamic_timeout`
- **THEN** the actual timeout is clamped to `max_dynamic_timeout`
- **AND** `timeout_at_cap` in the `fix_context` passed to implement is true

#### Scenario: phases are unaffected by dynamic timeout
- **GIVEN** implement outputs `estimated_test_duration: 120`
- **AND** a phase in `se3.yaml` explicitly configures `timeout: 600`
- **WHEN** the test step executes that phase
- **THEN** that phase still uses its own configured 600-second timeout, and the dynamic computation is not applied

#### Scenario: First timeout retries in place without counting a fix iteration
- **GIVEN** the main command is terminated due to a timeout-class failure (the `timed_out` flag, a `Timeout after` marker, or `returncode == -1`)
- **WHEN** the test step detects the timeout for the first time within a single execution
- **THEN** the step re-runs the same command in place exactly once and does NOT increment the fix-loop counter (no `REVISION_NEEDED` transition is made for the in-place retry)
- **AND** if the in-place retry passes, the step proceeds normally without entering the fix loop

#### Scenario: Persistent timeout enters the fix loop labeled as a timeout
- **GIVEN** the in-place retry of the main command **still** times out
- **WHEN** the test step finishes
- **THEN** the test step returns `REVISION_NEEDED`
- **AND** the failure context explicitly labels the failure as a timeout rather than an assertion failure (e.g. `timed_out_not_assertion` / `timeout_retried` flags)

#### Scenario: Main command timeout triggers timeout-aware fix loop
- **WHEN** the main command is terminated due to the dynamic timeout
- **THEN** the test step returns `REVISION_NEEDED`
- **AND** `fix_context` contains `timeout_reason`, `previous_timeout`, `previous_estimated_test_duration`, `timeout_multiplier`, and `timeout_at_cap`

#### Scenario: implement raises the estimate in a fix iteration
- **GIVEN** the test step failed due to a timeout and attached timeout metadata in `fix_context`
- **WHEN** implement re-executes in the fix iteration
- **THEN** `estimated_test_duration` in implement's JSON output is strictly greater than `previous_estimated_test_duration`
- **AND** the next test execution uses the updated estimate to compute a larger timeout

#### Scenario: verify_spec code reachability validation
- **WHEN** verify_spec checks newly added code
- **THEN** verify that the newly added functions/methods are reachable from an actual call path
- **AND** placing new logic in functions that are never called is forbidden
- **AND** newly added code that is not called is recorded as an error-level issue

#### Scenario: verify_spec end-to-end integration validation
- **WHEN** verify_spec checks functionality involving collaboration among multiple components
- **THEN** verify the complete chain (injection -> forwarding -> consumption) rather than only verifying each component independently
- **AND** multi-component functionality lacking end-to-end validation is recorded as a warning-level issue

#### Scenario: verify_spec dead-code check
- **WHEN** verify_spec checks newly added code
- **THEN** verify that the newly added functions/methods have callers
- **AND** verify that the newly added parameters are used
- **AND** newly added code without callers is recorded as a warning-level issue

### Requirement: update_spec supports creating new specs

The `update_spec` step SHALL create a corresponding new spec file when an implementation introduces a new subsystem or mechanism, rather than only updating existing specs.

**New Spec vs Append criteria (defined by spec-guardrails, enforced by update_spec):**
1. **Conceptual independence** — whether the new content belongs to the same conceptual domain as the existing spec.
2. **Dependency direction** — whether adding the new Requirement would cause an existing Requirement to depend on it in reverse.
3. **Naming test** — whether the new Requirement can be named naturally under the existing spec title.
4. **Cross-scenario sharing** — whether the new content would be reused by multiple capabilities (cross-cutting concerns should become their own spec).

#### Scenario: A new subsystem triggers creation of a new spec
- **WHEN** an implementation introduces a new subsystem (such as Issue Discovery)
- **AND** that subsystem has no corresponding spec file
- **THEN** the update_spec step runs the 4 criteria before appending
- **AND** when the criteria result points to new_spec, create a new spec directory and `spec.md` under `se3/specs/`
- **AND** the new spec contains the standard structure such as Purpose, Requirements, and Scenarios
- **AND** the first line of the new spec contains `<!-- spec-format: v1 -->`
- **AND** the `spec_decisions` output records the decision and its reasoning

### Requirement: Workflow Config Tolerant Parsing

The `WorkflowConfig.from_dict()` parser in `se3/config.py` SHALL apply tolerant parsing to the `workflow.*` fields it accepts from `se3.yaml`, distinguishing between recoverable type-coercion cases (warn and fall back to default) and invariant violations (fail-fast via `ConfigError`). This ensures malformed YAML produces actionable diagnostics without crashing flow startup for benign type mismatches.

**Parsing policy by field:**

| Field | Default | Tolerant Cases (warn + fallback) | Fail-Fast Cases (`ConfigError`) |
|-------|---------|----------------------------------|---------------------------------|
| `max_fix_iterations` | `DEFAULT_MAX_FIX_ITERATIONS` (100) | `bool`, `float` (incl. `0.0` — explicit warning that the unlimited sentinel must be literal `int 0` or `null`/`None`, not `0.0`), arbitrary strings or other types not coercible via `int()` | Negative integers (`< 0`) — rejects typos that would otherwise silently disable exhaustion. Only literal `0` or `null` opts into unlimited mode (with `null` normalized to `0`). |
| `self_check_passes_required` | `DEFAULT_SELF_CHECK_PASSES_REQUIRED` (1) | `bool`, `float`, arbitrary strings or other types not coercible via `int()` | `< 1` after coercion — preserves the documented invariant that at least one pass is required. |
| `self_check_convergence_enabled` | `DEFAULT_SELF_CHECK_CONVERGENCE_ENABLED` (`false`) | Coerced via `_coerce_bool()`. If `raw_convergence is not None and not isinstance(raw_convergence, (bool, int, float))`: when the value is a string whose stripped/lowercased form is NOT in `{"true", "1", "yes", "on", "false", "0", "no", "off"}`, a warning is emitted; for any other non-(bool/int/float/str) type, a warning is also emitted. Valid string forms (e.g. `"true"`, `"YES"`, `" off "`) coerce silently. | — (no fail-fast cases; always falls back to default on invalid input) |

**Warning format:**

Warnings SHALL include the offending raw value (via `repr()`), the field name, and the fallback default value, so users can locate the malformed entry in their YAML. Example: `workflow.self_check_convergence_enabled='maybe' is not a valid boolean; falling back to default False`.

**Loading entry points:**

- `WorkflowConfig.from_dict(data)` — pure parser, accepts a dict (typically the contents of `se3.yaml`).
- `WorkflowConfig.load(project_root)` — reads the project YAML via `load_project_yaml` and delegates to `from_dict`; returns defaults when the YAML is missing or empty.
- `load_workflow_config(project_root=None)` — module-level convenience wrapper defaulting `project_root` to `Path.cwd()`.

#### Scenario: Unlimited sentinel via null
- **GIVEN** `se3.yaml` contains `workflow.max_fix_iterations: null`
- **WHEN** `WorkflowConfig.from_dict()` parses it
- **THEN** `max_fix_iterations` is set to `0` (unlimited)
- **AND** no warning is emitted

#### Scenario: Float 0.0 is rejected with explicit guidance
- **GIVEN** `se3.yaml` contains `workflow.max_fix_iterations: 0.0`
- **WHEN** `WorkflowConfig.from_dict()` parses it
- **THEN** a warning is emitted explaining that the unlimited sentinel must be the literal int `0` or `null`/`None`, not `0.0`
- **AND** `max_fix_iterations` falls back to `DEFAULT_MAX_FIX_ITERATIONS`

#### Scenario: Negative max_fix_iterations fails fast
- **GIVEN** `se3.yaml` contains `workflow.max_fix_iterations: -1`
- **WHEN** `WorkflowConfig.from_dict()` parses it
- **THEN** a `ConfigError` is raised explaining that the value must be `>= 0`
- **AND** the error message guides the user to `0` or `null` for unlimited mode

#### Scenario: self_check_passes_required less than 1 fails fast
- **GIVEN** `se3.yaml` contains `workflow.self_check_passes_required: 0`
- **WHEN** `WorkflowConfig.from_dict()` parses it
- **THEN** a `ConfigError` is raised explaining the value must be `>= 1`

#### Scenario: self_check_passes_required non-integer falls back
- **GIVEN** `se3.yaml` contains `workflow.self_check_passes_required: "two"` (or a bool/float)
- **WHEN** `WorkflowConfig.from_dict()` parses it
- **THEN** a warning is emitted naming the offending value and the fallback default
- **AND** `self_check_passes_required` is set to `DEFAULT_SELF_CHECK_PASSES_REQUIRED`

#### Scenario: self_check_convergence_enabled string coercion succeeds silently
- **GIVEN** `se3.yaml` contains `workflow.self_check_convergence_enabled: "true"` (or `"YES"`, `" off "`, etc. — any form in the recognized truthy/falsy set after strip/lowercase)
- **WHEN** `WorkflowConfig.from_dict()` parses it
- **THEN** the value is coerced via `_coerce_bool()` without warning
- **AND** `self_check_convergence_enabled` reflects the coerced boolean

#### Scenario: self_check_convergence_enabled unrecognized string warns and falls back
- **GIVEN** `se3.yaml` contains `workflow.self_check_convergence_enabled: "maybe"`
- **WHEN** `WorkflowConfig.from_dict()` parses it
- **THEN** a warning is emitted naming the offending value and the fallback default
- **AND** `self_check_convergence_enabled` is set to `DEFAULT_SELF_CHECK_CONVERGENCE_ENABLED` (`false`)

#### Scenario: self_check_convergence_enabled non-scalar warns and falls back
- **GIVEN** `se3.yaml` contains `workflow.self_check_convergence_enabled` set to a list, dict, or other non-(bool/int/float/str/None) value
- **WHEN** `WorkflowConfig.from_dict()` parses it
- **THEN** a warning is emitted naming the offending value and the fallback default
- **AND** `self_check_convergence_enabled` is set to `DEFAULT_SELF_CHECK_CONVERGENCE_ENABLED`

#### Scenario: Missing or empty workflow section uses defaults
- **GIVEN** `se3.yaml` has no `workflow:` section, or `workflow:` is not a dict
- **WHEN** `WorkflowConfig.from_dict()` (or `load()`) is invoked
- **THEN** a default `WorkflowConfig` is returned with no warnings

### Requirement: FlowInstance Persistence Schema

The `FlowInstance` dataclass (`se3/engine/models.py`) SHALL persist a fixed set of tracking, integration, and worktree-mode fields across runs via `to_dict()` / `from_dict()`. These fields extend the core `flow_id`/`task_description`/`task_type`/`status`/`state` set documented in the **Data Model** section and are required for `se3 run --resume`, `se3 run --worktree`, change-tracking integration, and multi-worktree baseline detection to work correctly.

**Field schema:**

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `created_at` | `datetime` | `datetime.now()` | Flow creation timestamp; ISO-formatted on disk. |
| `updated_at` | `datetime` | `datetime.now()` | Last-mutation timestamp; ISO-formatted on disk. Drives the ordering shown in `se3 history`. |
| `completed_at` | `datetime \| None` | `None` | Set when the flow reaches a terminal status; ISO-formatted on disk when present. |
| `change_name` | `str \| None` | `None` | Optional SE3 change name the flow is associated with. Set by `se3 run --change <name>`; consumed by SE3 change-tracking integration. |
| `change_path` | `Path \| None` | `None` | Filesystem path to the associated change; serialized as a string and rehydrated to `Path` on load. |
| `source_issue_id` | `str \| None` | `None` | Issue ID (from issue-discovery) that originated this flow, when the flow was triggered from an open issue. Used to thread completion back to the issue tracker. |
| `baseline_commit` | `str \| None` | `None` | Git HEAD recorded by `init_flow()` at the start of the flow. Used by the commit step's change-detection logic and by DAG worktree salvage (see *Inter-Step Input Passing* and the implement-step worktree management scenarios) so re-entries compute deltas against the original baseline rather than current HEAD. |
| `is_worktree_mode` | `bool` | `False` | True when the flow was started via `se3 run --worktree`. Distinguishes isolation-worktree flows for resume routing (the worktree flow body runs lock-free and auto-merges back on success) and history filtering. |
| `worktree_branch` | `str \| None` | `None` | The isolation branch created for this worktree run via the generic `create_worktree` / `fork_worktree` primitives. `None` when `is_worktree_mode=False`. |
| `worktree_path` | `str \| None` | `None` | Filesystem path of the git worktree backing `worktree_branch` (under `se3/worktrees/{branch_safe_name}`). `None` when the flow is not running inside an isolation worktree. |
| `worktree_original_branch` | `str \| None` | `None` | The branch that was checked out when `se3 run --worktree` was invoked. Captured so the end-of-run automatic `se3 merge` folds the isolation branch back into the correct destination, and so a `--resume` of the worktree run can re-derive the merge target. |

**Persistence rules:**

- `to_dict()` SHALL include every field above using the JSON-friendly representation indicated (ISO strings for datetimes, `str(path)` for `Path`, raw value otherwise).
- `from_dict()` SHALL accept missing optional fields and substitute their defaults via `data.get(...)`, so flows persisted by older builds (which may not have written every field) continue to load without error.
- `from_dict()` SHALL convert `change_path` from string back to `Path` when present, and SHALL substitute `False` for a missing `is_worktree_mode`.

#### Scenario: All FlowInstance fields round-trip through persistence
- **GIVEN** a `FlowInstance` populated with non-default values for every field listed above
- **WHEN** the instance is serialized via `to_dict()` and reconstructed via `from_dict()`
- **THEN** every field equals its original value (with `Path` correctly rehydrated and datetimes round-tripping through ISO format)

#### Scenario: Older engine.json without worktree-mode fields loads cleanly
- **GIVEN** an `engine.json` written by a build that predates `is_worktree_mode` / `worktree_branch` / `worktree_path` / `worktree_original_branch` (including legacy files that may still carry now-removed loop-mode fields)
- **WHEN** `FlowInstance.from_dict()` loads the file
- **THEN** the missing worktree-mode fields default to `False` / `None` and no `KeyError` is raised
- **AND** the loaded flow can be resumed normally (the absence of worktree fields is interpreted as "not a worktree-mode flow")

#### Scenario: change_path round-trips as Path
- **GIVEN** a `FlowInstance` with `change_path = Path("se3/changes/auth-rewrite")`
- **WHEN** the flow is persisted and reloaded
- **THEN** the loaded instance's `change_path` is a `Path` object (not a `str`) equal to the original

#### Scenario: source_issue_id threads the originating issue
- **GIVEN** a flow created from an open issue (e.g. via issue-discovery resolution)
- **WHEN** `source_issue_id` is set on the `FlowInstance` and the flow is persisted
- **THEN** `to_dict()` includes the issue id and `from_dict()` restores it
- **AND** downstream tooling (issue tracker integration) can read `flow.source_issue_id` to thread completion back to the originating issue

#### Scenario: baseline_commit anchors change detection
- **GIVEN** `init_flow()` records the git HEAD as `baseline_commit` on a fresh flow
- **WHEN** the same flow is resumed in a later session (potentially after intermediate commits on the branch)
- **THEN** `init_flow()` does not overwrite `baseline_commit` (idempotent on resume, per the *init_flow idempotent on resume* scenario)
- **AND** the commit step and DAG worktree salvage continue to compute their deltas against the original baseline, not current HEAD

### Requirement: State Tracking Fields and Helper API

The `State` dataclass (`se3/engine/models.py`) SHALL persist the execution state of a flow, including step tracking, global context, review/fix loop counters, and history. It exposes a helper API used by step handlers and the state machine to advance review and fix loops.

**Field schema:**

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `current_step_id` | `str \| None` | `None` | ID of the currently active step. |
| `step_history` | `List[str]` | `[]` | Ordered list of step IDs executed in this flow (in append order). |
| `steps` | `Dict[str, Step]` | `{}` | Map of `step_id` → `Step` instances for all steps created in this flow. |
| `context` | `Dict[str, Any]` | `{}` | Global context shared across steps. Used as a side-channel for the state machine to expose computed values (e.g. `resolved_type`, `fix_iterations`, `fix_history` mirrors) to handlers without changing step inputs. |
| `selected_steps` | `List[StepType]` | `[]` | The dynamically selected sequence of step types for this flow (chosen by `analyze` and the CONFIRM-insertion step). |
| `current_step_index` | `int` | `0` | Position within `selected_steps` of the next step to execute. The state machine advances this when transitioning forward; it does NOT advance during fix-loop or revision routing back. **On flow completion** — in `transition_to_next`'s completion branch, after the flow's status is set to `FlowStatus.COMPLETED` and before it returns (and mirrored in the `run.py` fallback completion path) — the state machine advances `current_step_index` to `len(selected_steps)` (one past the last selected step) and persists the flow, so the index encodes "completed steps == total steps". The count semantics are uniformly **completed-steps / total-steps**: while a flow is running, executing the last of N steps still reads `N-1` here (the first N-1 steps are done, the Nth is in flight); only on completion does it reach `N`. Setting this index out of range on completion is safe because resume self-heals it from `current_step_id` via `selected_steps.index(...)`. |
| `review_iterations` | `Dict[str, int]` | `{}` | Per-step review iteration counter, keyed by the **reviewed** step's `step_id`. Used by the CONFIRM step's LLM reviewer to bound iterations against `confirmation.steps.<step>.max_iterations`. |
| `fix_iterations` | `int` | `0` | Global fix-loop counter shared by `test`, `self_check`, and `verify_spec`. Bounded by `workflow.max_fix_iterations` (see *verify_spec Unified Priority and Scope Mechanism*). |
| `fix_history` | `List[Dict[str, Any]]` | `[]` | Append-only log of fix-loop iterations, capped at `FIX_HISTORY_MAX_ENTRIES` (see *Fix History Structure*). Each entry follows the schema documented under that Requirement. |
| `baseline_failures` | `Optional[List[str]]` | `None` | Frozen set of test IDs that were failing at flow start, measured before `implement`'s first write (see *Pre-implement Test Baseline*). Three-state sentinel: `None` = not yet captured (no failure may be treated as inherited); `[]` = captured, zero failures at flow start; `[...]` = these specific test IDs were already failing. Persisted so `--resume` reuses the same snapshot rather than re-measuring against a different one. |
| `session_token_usage` | `UsageTotals` | empty `UsageTotals` | Session-level (whole-flow) running total of LLM token usage and cost, accumulated across every step. Carries `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`, and `total_cost_usd` (the `UsageTotals` structure defined in `se3/engine/token_usage.py`). Each step's per-step total (written to `step.outputs["token_usage"]`) is added into this running total when the step finishes (see *Step-Scoped Token Usage Aggregation*). Persisted so `--resume` continues the same session total and the flow-completion CLI summary can read the authoritative figure. |

**Step ID auto-generation:** `add_step(step)` SHALL auto-generate `step.step_id` when not already set, using the format `NN_steptype_uuid8` where `NN` is the 1-based sequence number derived from `len(step_history) + 1` and `uuid8` is the first eight hex characters of a fresh UUID (e.g. `01_analyze_844c2cf8`). The format is human-readable and sortable so on-disk history files (`se3/history/{flow_id}/{step_id}.jsonl`) order chronologically.

**Helper API:**

- `get_current_step() -> Optional[Step]` — returns the step pointed to by `current_step_id`, or `None`.
- `add_step(step) -> None` — registers a step in `steps` and appends its ID to `step_history` (idempotent on history append).
- `get_step_to_review(confirm_step_id) -> Optional[Step]` — given a CONFIRM step's ID, returns the immediately preceding step in `step_history` (the step under review). Returns `None` when the confirm step is not in history or has no predecessor.
- `increment_review_iteration(step_id) -> int` / `get_review_iteration(step_id) -> int` — bump and read the per-step review counter (1-based; `get_*` returns `0` before the first increment).
- `increment_fix_iteration(fix_context=None) -> int` — bump the global fix counter, append a `fix_history` entry containing `iteration`, `timestamp`, and any caller-supplied `fix_context` keys, apply the sliding-window cap, and mirror `fix_iterations`/`fix_history` into `context` so handlers can read them via the global context channel.
- `get_fix_iteration() -> int` — read the current global fix counter (`0` before any increment).
- `update_task_type(task_type) -> None` / `is_type_pending() -> bool` — record the LLM-resolved task type from `analyze` into `context["resolved_type"]`, and check whether resolution has happened yet. Used to distinguish CLI-provided task types (which arrive on `FlowInstance.task_type`) from the analyze step's classification.

**Context mirroring on fix-iteration increment:** Whenever `increment_fix_iteration()` runs, the post-increment `fix_iterations` integer SHALL be written to `context["fix_iterations"]` and the (clamped) `fix_history` list SHALL be written to `context["fix_history"]`. `from_dict()` SHALL re-mirror the clamped `fix_history` into `context["fix_history"]` when that key is already present in the loaded context dict, so resumed flows do not carry a stale oversized copy.

**Persistence:**

- `to_dict()` SHALL serialize every field above, including `baseline_failures` (preserving the `None` vs `[]` distinction) and `session_token_usage` (via `UsageTotals.to_dict()`). `selected_steps` is persisted as a list of `StepType.value` strings; `steps` is persisted as a `{step_id: step.to_dict()}` map.
- `from_dict()` SHALL accept missing keys and substitute defaults (`get_current_step_id` → `None`, `step_history`/`steps`/`context`/`review_iterations` → empty containers, `fix_iterations` → `0`, `current_step_index` → `0`, `baseline_failures` → `None`, `session_token_usage` → an empty `UsageTotals`). `UsageTotals.from_dict()` SHALL tolerate a `None`/missing payload and absent individual fields so engine.json files written by older builds (which have no token-usage field) load without error.
- `from_dict()` SHALL apply the same tail-keep `FIX_HISTORY_MAX_ENTRIES` cap on load that `increment_fix_iteration()` applies on append, so engine.json files written by older builds (or builds with a larger cap) are clamped at deserialization time rather than only on the next increment.

#### Scenario: Step IDs follow NN_steptype_uuid8 format
- **WHEN** `State.add_step(step)` is called with a Step whose `step_id` is unset
- **THEN** `step.step_id` is set to `f"{NN:02d}_{step.step_type.value}_{uuid8}"`, where `NN = len(step_history) + 1` (1-based) and `uuid8` is the first 8 hex characters of a fresh UUID
- **AND** the step is registered in `steps` and its ID is appended to `step_history`

#### Scenario: Review iteration counter is per-step
- **GIVEN** two CONFIRM-reviewed steps with IDs `X` and `Y` in the same flow
- **WHEN** `increment_review_iteration(X)` runs twice and `increment_review_iteration(Y)` runs once
- **THEN** `get_review_iteration(X)` returns `2`, `get_review_iteration(Y)` returns `1`, and unrelated step IDs return `0`

#### Scenario: Fix iteration increment mirrors into context
- **WHEN** `increment_fix_iteration({"step_id": "...", "reason": "test_failure"})` runs
- **THEN** the new entry appended to `fix_history` contains `iteration`, `timestamp`, and the caller-supplied fields
- **AND** `context["fix_iterations"]` equals the post-increment counter
- **AND** `context["fix_history"]` is the same (clamped) list as `state.fix_history`

#### Scenario: Task type resolution is tracked via context
- **GIVEN** a fresh `State` with no `resolved_type` in context
- **WHEN** `is_type_pending()` is called
- **THEN** it returns `True`
- **WHEN** `update_task_type("feature")` runs and `is_type_pending()` is called again
- **THEN** it returns `False` and `state.context["resolved_type"] == "feature"`

#### Scenario: get_step_to_review returns the predecessor in history
- **GIVEN** `step_history == ["X", "Y_confirm"]` where `Y_confirm` is a CONFIRM step
- **WHEN** `get_step_to_review("Y_confirm")` is called
- **THEN** it returns the `Step` object stored under ID `X`
- **WHEN** `get_step_to_review("missing_id")` is called
- **THEN** it returns `None` rather than raising

#### Scenario: State round-trips through persistence
- **GIVEN** a `State` populated with non-default values for every field above (including non-empty `review_iterations`, `fix_iterations`, `fix_history`, `context`, and `selected_steps`)
- **WHEN** the state is serialized via `to_dict()` and reconstructed via `from_dict()`
- **THEN** every field equals its original value (with `selected_steps` rehydrated from `.value` strings back to `StepType` enum members, `steps` rehydrated to `Step` instances, and `session_token_usage` rehydrated to an equal `UsageTotals`)
- **AND** an oversized `fix_history` in the persisted dict is clamped on load via the same tail-keep policy used by `increment_fix_iteration`

#### Scenario: Older engine.json without session_token_usage loads with an empty total
- **GIVEN** an engine.json written by a build that predates the token-usage feature, so its serialized state dict has no `session_token_usage` key
- **WHEN** `State.from_dict()` loads it
- **THEN** `session_token_usage` defaults to an empty `UsageTotals` (all token counts and `total_cost_usd` zero) rather than raising
- **AND** a partial `session_token_usage` payload that omits individual fields likewise tolerates the gaps, defaulting each missing field to zero

### Requirement: Step-Scoped Token Usage Aggregation

The flow engine SHALL aggregate LLM token usage and cost at two granularities — **per step** and **per session (whole flow)** — reusing the `UsageTotals` data structure, the contextvar-scoped accumulator, and the formatting helpers defined in `se3/engine/token_usage.py` as the single source of truth for field names and merge semantics. This consumes the telemetry the `llm-caller` subsystem captures from each subprocess call (see the `llm-caller` *Result Usage and Cost Capture* requirement) and feeds both the CLI per-step / session usage blocks and the web console's per-step footnote / session badge.

**Step scope (`state_machine.run_step`):**

- Before dispatching a step's handler, `run_step` SHALL open a step-scoped token-usage accumulator via the `token_usage` context manager; the `llm-caller` `_call_with_retry` folds each subprocess call's usage into whatever accumulator is currently in scope.
- When the handler returns — including the `finally` path on handler exception — `run_step` SHALL write the accumulated step total into `step.outputs["token_usage"]` as a plain JSON-primitive dict (via `UsageTotals.to_dict()`), and only when the total is non-empty, and SHALL add that step total into `flow.state.session_token_usage`.
- **Non-terminal round visibility:** When the handler returns a non-terminal status (e.g. `REVISION_NEEDED` from `self_check`, `PAUSED` from `discovery`, or any other non-terminal return from `verify_spec` / `test` / `confirm`), `run_step` SHALL simultaneously write the accumulated step total into `step.outputs["token_usage"]` (so the CLI step renderer and the webui report card can display it) **and** into `step.outputs["carried_token_usage"]` (so the next round can merge the carry into its own cumulative). The `session_token_usage` SHALL add only this round's increment (the same amount it would add for a terminal round), not the carried total — this prevents double-counting across non-terminal→terminal transitions.
- **Terminal round merge:** When the handler returns a terminal status and `step.outputs` already contains a `carried_token_usage` from prior non-terminal rounds, `run_step` SHALL merge the carried total with this round's accumulated increment to produce the final `step.outputs["token_usage"]`, and SHALL clear `carried_token_usage` from `step.outputs`. The `session_token_usage` SHALL add only this terminal round's increment (not the carried portion), because the prior non-terminal rounds already added their own increments individually. This ensures the terminal round's `token_usage` reflects the whole-step cumulative while `session_token_usage` never double-counts.
- `UsageTotals` defines `add()` (component-wise merge of the four token counts plus `total_cost_usd`), `to_dict()` / `from_dict()`, and `is_empty()`, so per-step and per-session totals share one merge and serialization contract.

**DAG-parallel implement re-binding:** When the `implement` step runs groups in parallel worker threads (see *Implement Step DAG Execution Strategy*), each worker SHALL re-bind the step accumulator into its own thread so concurrent groups' usage still folds into the same step total under a lock, rather than being lost because the contextvar default is per-thread.

**Discovery multi-round carried total:** The `discovery` step is interactive and re-enters its handler once per clarification round, calling `step.outputs.clear()` between rounds to drop the previous round's rendered payload. That `clear()` MUST preserve the `carried_token_usage` entry so the discovery step's terminal `token_usage` total reflects the **whole-discovery cumulative** across every round, not just the last round's increment (the prior behaviour, which cleared `carried_token_usage` and thereby under-reported the discovery total to the per-round increment of the final round). Concretely, on each round the handler computes the round increment from `token_usage.current_step_usage()` (the contextvar accumulator at the post-LLM-call display point) and the cumulative as `carried_token_usage + round_increment`, persists the cumulative back into `carried_token_usage` for the next round, and lets `run_step` write the final cumulative into `step.outputs["token_usage"]`. This carried total is the authoritative data source for both the discovery terminal usage and the CLI per-round cumulative footer (see *Interactive Per-Round Token Usage Footer*).

#### Scenario: Step total written to step.outputs and folded into the session total
- **GIVEN** a step whose handler makes one or more LLM calls
- **WHEN** `run_step` finishes the handler (normally or via exception)
- **THEN** the step's accumulated token usage is written to `step.outputs["token_usage"]` as a JSON-primitive dict, but only when the total is non-empty
- **AND** the same step total is added into `flow.state.session_token_usage` so the session running total reflects every finished step

#### Scenario: A step with no LLM usage writes no token_usage key
- **GIVEN** a step whose handler issues no LLM call (or whose calls reported zero usage)
- **WHEN** `run_step` finishes the handler
- **THEN** `step.outputs` carries no `token_usage` key (the empty total is not persisted)
- **AND** `flow.state.session_token_usage` is left unchanged

#### Scenario: Non-terminal round publishes visible token_usage and carried_token_usage
- **GIVEN** a step whose handler returns a non-terminal status (e.g. `self_check` returning `REVISION_NEEDED`)
- **WHEN** `run_step` finishes the handler for that round
- **THEN** the round's accumulated step total is written into both `step.outputs["token_usage"]` (so CLI `render_step_usage` and the webui report card can display it) and `step.outputs["carried_token_usage"]` (so the next round's handler can merge it into its own cumulative)
- **AND** the round's step total is added into `flow.state.session_token_usage` — only the increment from this round, not a carried total, so no double-counting

#### Scenario: Terminal round merges carried and clears carry
- **GIVEN** a step whose prior round wrote `carried_token_usage` into `step.outputs` and whose current round returns a terminal status
- **WHEN** `run_step` finishes the handler for the terminal round
- **THEN** the final `step.outputs["token_usage"]` equals the sum of the prior carried total and this round's accumulated increment (the whole-step cumulative)
- **AND** `step.outputs["carried_token_usage"]` is cleared (removed from `step.outputs`)
- **AND** `session_token_usage` adds only this terminal round's increment — the prior rounds already added their own increments individually, so the cumulative is never double-counted

#### Scenario: Non-terminal usage visible in CLI and webui without special-case logic
- **GIVEN** a step whose handler returns `REVISION_NEEDED` with a non-empty accumulated token total
- **WHEN** the CLI sink and the webui report card both read `step.outputs["token_usage"]`
- **THEN** both surfaces display that round's usage without needing to understand `carried_token_usage` — the shared `render_step_usage` / `buildStepUsageFootnote` logic reads only the standard `token_usage` field, which now contains the round's total for non-terminal rounds as well as terminal ones

#### Scenario: Parallel implement groups fold usage into the same step total
- **GIVEN** an `implement` step running multiple task groups concurrently in worker threads
- **WHEN** each group's LLM calls report usage
- **THEN** every group's usage is folded into the one step-scoped accumulator (re-bound into each worker thread, merged under a lock)
- **AND** the step's `token_usage` total is the sum across all groups, not just the group that ran on the originating thread

#### Scenario: Discovery carried total survives the per-round outputs.clear()
- **GIVEN** an interactive `discovery` step that runs multiple clarification rounds, each issuing an LLM call and calling `step.outputs.clear()` before rendering the next round
- **WHEN** the discovery step reaches its terminal status
- **THEN** the `carried_token_usage` entry is preserved across every `step.outputs.clear()`, so the cumulative total accumulates each round's increment rather than being reset to a single round
- **AND** the discovery step's `step.outputs["token_usage"]` (and the session total it folds into) equals the whole-discovery cumulative across all rounds, not just the final round's increment

### Requirement: Interactive Per-Round Token Usage Footer

The interactive multi-round steps — `discovery` (each clarification round) and `confirm` (each review) — SHALL surface their LLM token usage as a **compact, single-line dim footer** appended to the assistant content they show the user, rather than the big reverse-color `render_usage_block` / "Step Token Usage" table the non-interactive steps use at step completion. This keeps the interaction continuous: the footer sits at the tail of the assistant message block so the following input prompt flows naturally underneath it, instead of being split off by a full-width table.

**Shared footer builder (`token_usage.format_round_usage_footer`):** Given a round-increment `UsageTotals` and a cumulative `UsageTotals`, the builder returns the single line `本轮 {in} in / {out} out · 累计 {in} in / {out} out`, showing the input / output token counts only. The numbers reuse the same thousands-separator formatting (`_format_tokens`, e.g. `12,345`) as `format_usage_line` / `render_usage_block`, so the wording and number style stay identical across discovery, confirm, and the rest of the project. `None` inputs degrade to zeros.

**Display rule — only when the round actually called the LLM:** A footer is rendered for a round / review **only** when that round actually issued an LLM call (its round-increment total is non-empty). Rounds that issued no LLM call — discovery's empty-input redraw, the `--resume` discovery re-display, or a human-mode confirm that made no LLM review call — leave the round increment (or the step's `token_usage`) empty and render no footer.

**Discovery (handler-rendered, inline):** The discovery handler renders the footer itself, inline inside the Discovery message block (as the last renderable of the block's `Group`, before the closing blue rule), so the footer is part of that round's assistant content. The round increment is `token_usage.current_step_usage()` taken at the post-LLM-call display point; the cumulative is `carried_token_usage + round_increment` (see *Step-Scoped Token Usage Aggregation*). Non-LLM redraw / resume re-display paths pass no round usage (or `None`) so no footer is shown.

**Confirm (CliSink-rendered, at completion):** Because the confirm review runs inside the handler and CliSink observes only the terminal event (by which point the contextvar accumulator has been reset), the confirm footer is rendered by `CliSink` at the confirm step's terminal event, reading `step.outputs["token_usage"]`. The confirm LLM reviewer calls the LLM at most once per confirm step (a fresh confirm step is created per revision), so the step-level total **is** both the round and the cumulative figure — the same `UsageTotals` is passed for both arguments.

**Discovery (CliSink-rendered, at terminal event):** When the discovery step reaches a terminal status and `step.outputs["token_usage"]` is non-empty, `CliSink` renders the whole-discovery cumulative usage via a dim `format_usage_line` line. This is necessary because the confirmation round (when the user types `1`) issues no LLM call, so the per-round inline footer alone would never display the cumulative total that spans all rounds. The per-round inline footer (rendered by the handler each round that calls the LLM) remains unchanged.

**Non-interactive steps unchanged:** This requirement adds the compact footer **only** for the interactive steps. The per-step `render_step_usage` / `render_usage_block` big-table behaviour for every non-interactive step (analyze, plan, implement, test, summarize, …) is unchanged.

#### Scenario: Discovery round shows an inline compact usage footer
- **GIVEN** a `discovery` clarification round that issued an LLM call before pausing for the user
- **WHEN** the discovery message block is rendered on the CLI
- **THEN** a single dim line `本轮 {round_in} in / {round_out} out · 累计 {cum_in} in / {cum_out} out` is appended at the tail of that round's message block, before the closing rule
- **AND** the round figures are this round's increment (`current_step_usage()`) and the cumulative figures are `carried_token_usage + round_increment`
- **AND** the numbers use the same thousands-separator format as `render_usage_block`

#### Scenario: A discovery redraw or resume re-display shows no footer
- **WHEN** the discovery display is re-rendered without a new LLM call (an empty-input redraw or the `--resume` re-display path)
- **THEN** no round usage is passed and no usage footer is appended to the message block

#### Scenario: Confirm renders a compact footer, not the big block
- **GIVEN** a `confirm` step whose LLM reviewer made one call, leaving a non-empty `step.outputs["token_usage"]`
- **WHEN** `CliSink` consumes the confirm step's terminal event
- **THEN** it renders the compact dim single-line `format_round_usage_footer` footer (with the round and cumulative arguments equal to the step total), NOT the big `render_step_usage` "Step Token Usage" block
- **AND** a human-mode confirm that made no LLM call has an empty / absent `token_usage`, so no footer is rendered

#### Scenario: CliSink renders discovery cumulative usage at the terminal event
- **WHEN** `CliSink` consumes a `discovery` step's terminal event
- **THEN** it renders a dim whole-discovery cumulative usage line via `format_usage_line` from `step.outputs["token_usage"]`, covering all rounds including the confirmation round (which issues no LLM call, so the per-round inline footer alone would never show the cumulative)
- **AND** the non-interactive steps' big `render_usage_block` tables are unaffected

### Requirement: Transition Data Model

The `Transition` dataclass (`se3/engine/models.py`) SHALL describe a single step-to-step transition rule as a serializable record, exposed publicly via `se3.engine.__init__` alongside `Step`, `State`, and `FlowInstance` so external tooling (e.g. flow-graph inspectors, visualizers, persisted transition-history consumers) can read and write transition records without depending on the state machine's internal routing logic. The live `StateMachine` implements its forward / fix-loop / revision routing in code rather than by interpreting `Transition` instances at runtime; `Transition` exists as a data-model contract for serialization.

**Field schema:**

| Field | Type | Default | Purpose |
|-------|------|---------|---------|
| `from_step` | `StepType` | required | Source step of the transition. |
| `to_step` | `StepType` | required | Destination step of the transition. |
| `condition` | `str \| None` | `None` | Optional name of a conditional predicate gating the transition (e.g., a task-type label or guard name). When `None`, the transition is unconditional. |
| `description` | `str` | `""` | Free-text description for documentation and visualization. |

**Persistence:**

- `Transition.to_dict()` SHALL serialize `from_step` / `to_step` as their `StepType.value` strings, and pass `condition` / `description` through verbatim.
- `Transition.from_dict()` SHALL accept the same shape, substituting `None` for a missing `condition` and `""` for a missing `description`, and rehydrating the step fields via `StepType(value)`.

#### Scenario: Transition round-trips through to_dict/from_dict
- **GIVEN** a `Transition` populated with non-default values for `from_step`, `to_step`, `condition`, and `description`
- **WHEN** the instance is serialized via `to_dict()` and reconstructed via `from_dict()`
- **THEN** every field equals its original value, with `from_step` / `to_step` rehydrated as `StepType` enum members

#### Scenario: Transition deserialization tolerates missing optional fields
- **GIVEN** a dict containing only `from_step` and `to_step` values (no `condition`, no `description`)
- **WHEN** `Transition.from_dict()` loads it
- **THEN** `condition` defaults to `None` and `description` defaults to `""`
- **AND** no exception is raised for the missing optional keys

### Requirement: Event Stream and Sink Interface

`se3 run` SHALL emit a single unified structured event stream internally and SHALL NOT branch its behavior on the caller. Rendering degrades to a pluggable *sink* at the tail of that stream; "CLI vs daemon" degrades to one sink selection at the outermost layer.

**Event stream (`se3/engine/event_stream.py`):**

- `EventType` — a `str`-valued enum covering the flow lifecycle: `FLOW_STARTED`, `STEP_STARTED`, `STEP_OUTPUT`, `STEP_COMPLETED`, `STEP_FAILED`, `FLOW_PAUSED`, `FLOW_COMPLETED`, `FLOW_FAILED`, `INTERJECTION_NEEDED`, `CALL_NEEDED`.
- `Event` — a dataclass carrying `type`, `timestamp`, optional `flow_id` / `step_id` / `step_type`, and a `data` payload dict; it exposes `to_dict()` for serialization.
- `new_event(event_type, *, flow_id=None, step_id=None, step_type=None, timestamp=None, **data)` — convenience factory used by `run_flow`; keyword payload arguments are collected into `data`.
- `EventEmitter` — an in-memory pub/sub hub. `subscribe(sink)` / `unsubscribe(sink)` manage an ordered subscriber list; `emit(event)` fans the event out to every subscribed sink in subscription order. A sink that raises during `consume()` MUST NOT abort delivery to the remaining sinks — the event stream is best-effort and a rendering fault MUST NOT break the flow. `scope()` is a context manager that restores the subscriber list on exit.

**Sink interface (`se3/engine/sink.py`):**

- `Sink` — an ABC declaring `consume(event: Event) -> None`.
- `CliSink` — the CLI-mode tail. It delegates step-output rendering entirely to the pre-existing `step_renderers.render_step_output(step)` and adds no rendering logic of its own, keeping CLI output byte-for-byte identical to today's `se3 run`. Flow-level lifecycle events and raw `STEP_STARTED` / `STEP_OUTPUT` events are deliberately a no-op in `CliSink` because the `se3 run` orchestrator already renders those directly; having the sink render them too would double the CLI output. Additionally, `CliSink` skips the `STEP_COMPLETED` / `STEP_FAILED` events of the interactive/special step types in its `_CLI_SKIP_STEP_TYPES` set — `confirm`, `discovery`, and `plan` — because their CLI output is owned by the orchestrator's interactive/special paths (the discovery message panel, the confirm approval prompt, the plan presentation). Re-rendering those terminal events through `render_step_output` would double the CLI output, so `CliSink` is the layer that preserves byte-identical CLI behavior while still letting the other sinks observe the events.
- `JsonSink` — the daemon-mode tail. It serializes each event via `Event.to_dict()` and writes one line of JSON (NDJSON) per event, using `default=str` so non-serializable payload values degrade gracefully. It supports a compact (default) and a `pretty` mode.
- `HistorySink` — the always-subscribed persistence tail wired up from `src/se3/commands/run.py`. It persists step events into the per-step jsonl files (`se3/history/{flow_id}/{step_id}.jsonl`) consumed by the daemon history reader and the web console. Beyond the terminal `STEP_COMPLETED` / `STEP_FAILED` and the per-turn `STEP_OUTPUT` records it already writes, `HistorySink` MUST ALSO persist `STEP_STARTED` as a `type: "step_started"` record (carrying `step_id` / `step_type` / `status: "running"` / `timestamp`) via `chat_history.record_step_started` (atomic append, `OSError`-tolerant — a persistence fault is swallowed and never breaks the flow). This anchor record is what lets the web console surface a step region the moment the step enters `RUNNING`, including steps that produce no LLM conversation (`TEST`, `COMMIT`, `SPEC_GATE`). The CLI history reader (`get_step_history`) skips the `step_started` line so CLI history is unaffected, and resuming the same step does NOT produce a duplicate terminal record. `CliSink` remains a no-op on `STEP_STARTED` (see above).

**Terminal step-event emission for every step type:**

The `se3 run` orchestrator (`_run_flow_impl`) SHALL emit a terminal
`STEP_COMPLETED` / `STEP_FAILED` event for **every** step type, including the
interactive `CONFIRM` and `DISCOVERY` steps and `PLAN` (and likewise
`summarize`), which were previously excluded from emission. Emitting the
terminal event is what lets `HistorySink` persist the step's structured
`outputs` to the per-step jsonl history (consumed by the daemon history reader
and the web console's per-step report cards) and lets `JsonSink` forward the
event to the daemon; without it, a finished discovery / plan / confirm /
summarize step left the web console with no final report card to render.

The emit is gated on a **terminal result**: the orchestrator emits only when
the step result is `COMPLETED`, `PARTIAL`, or `FAILED` (`STEP_FAILED` for
`FAILED`, `STEP_COMPLETED` otherwise). A step that returned `PAUSED` (DISCOVERY
awaiting user input, CONFIRM awaiting approval) or `REVISION_NEEDED` has not
finished yet, so its terminal event is deferred until a later re-run reaches a
terminal status. Because `CliSink` skips the interactive/special step types
(see `_CLI_SKIP_STEP_TYPES` above), this universal emission does NOT
double-render the interactive steps on the CLI; `HistorySink` and `JsonSink`
still receive every terminal event.

#### Scenario: EventEmitter fans out to all subscribed sinks
- **WHEN** an `Event` is emitted on an `EventEmitter` with multiple subscribed sinks
- **THEN** every subscribed sink's `consume()` is invoked, in subscription order
- **AND** a sink that has been `unsubscribe()`d no longer receives subsequently emitted events

#### Scenario: A failing sink does not abort delivery
- **GIVEN** an `EventEmitter` with two subscribed sinks where the first raises in `consume()`
- **WHEN** an event is emitted
- **THEN** the exception from the first sink is swallowed
- **AND** the second sink still receives the event

#### Scenario: CliSink renders step output via the existing renderer
- **WHEN** `CliSink` consumes a `STEP_COMPLETED` or `STEP_FAILED` event whose `data` carries a `"step"` object
- **THEN** the event is routed to `step_renderers.render_step_output(step)` — the same entry point the current CLI uses
- **AND** flow-level lifecycle events (`FLOW_STARTED` / `FLOW_COMPLETED` / `FLOW_PAUSED` / etc.) and raw `STEP_OUTPUT` / `STEP_STARTED` events are a no-op in `CliSink`

#### Scenario: CliSink skips interactive/special step terminal events
- **WHEN** `CliSink` consumes a `STEP_COMPLETED` / `STEP_FAILED` event whose step type is `confirm`, `discovery`, or `plan` (its `_CLI_SKIP_STEP_TYPES` set)
- **THEN** `CliSink` does NOT route the event to `render_step_output`, leaving the CLI output byte-identical to the orchestrator's interactive/special-path rendering
- **AND** the same event is still delivered to `HistorySink` (per-step jsonl) and `JsonSink` (daemon NDJSON)

#### Scenario: Terminal event is emitted for interactive and summarize steps
- **WHEN** an interactive `DISCOVERY` / `CONFIRM` step, a `PLAN` step, or a `summarize` step finishes with a terminal result (`COMPLETED` / `PARTIAL` / `FAILED`)
- **THEN** the orchestrator emits the corresponding `STEP_COMPLETED` / `STEP_FAILED` event (it is no longer excluded by step type)
- **AND** `HistorySink` persists the step's `outputs` to the per-step jsonl so the web console can render its report card

#### Scenario: Terminal event is deferred for paused or revision-pending steps
- **WHEN** a step returns `PAUSED` (e.g. DISCOVERY awaiting input, CONFIRM awaiting approval) or `REVISION_NEEDED`
- **THEN** the orchestrator does NOT emit a terminal `STEP_COMPLETED` / `STEP_FAILED` event for that step
- **AND** the terminal event is emitted only once a later re-run drives the step to a `COMPLETED` / `PARTIAL` / `FAILED` result

#### Scenario: JsonSink emits one NDJSON line per event
- **WHEN** `JsonSink` consumes an event
- **THEN** it writes exactly one newline-terminated line of valid JSON (the `Event.to_dict()` payload) to its destination stream

#### Scenario: HistorySink persists STEP_STARTED as a running anchor record
- **WHEN** any step (including a non-LLM `TEST` / `COMMIT` / `SPEC_GATE`
  step) emits a `STEP_STARTED` event
- **THEN** `HistorySink` writes one record to
  `se3/history/{flow_id}/{step_id}.jsonl` with `type: "step_started"`,
  `status: "running"`, and the step's `step_id` / `step_type` / `timestamp`
- **AND** `get_step_history` skips that `step_started` line so the CLI history
  reader is unaffected
- **AND** resuming the same step does NOT produce a duplicate terminal record,
  and a `HistorySink` persistence fault is swallowed without breaking the flow

#### Scenario: se3 run --output-format selects the outermost sink
- **WHEN** the user runs `se3 run "<task>"` without `--output-format` (or with `--output-format cli`)
- **THEN** a `CliSink` is subscribed and CLI output is byte-for-byte identical to current `se3 run` behavior
- **WHEN** the user runs `se3 run "<task>" --output-format json` (the form a daemon uses when it spawns a flow)
- **THEN** a `JsonSink` is subscribed and the flow emits its structured NDJSON event stream
- **AND** `se3 run` itself does not branch on the caller — only the tail sink differs
- **AND** an unrecognized `--output-format` value is rejected with a clear error and a non-zero exit

### Requirement: Waiting-for-Lock Visible Running State

Any flow that blocks waiting for the project's main-worktree mutex (`se3/state/merge.lock`) MUST first make itself observable as a *running* flow that is *waiting for the lock*, never a silent stall. A flow that goes straight into a blocking `flock(LOCK_EX)` before persisting any state writes no `engine.json`, emits no step event, and is therefore invisible to the daemon and stuck at "published" in the web console — this requirement forbids that failure mode and supplies the general safety net behind the lazy lock-acquisition behavior described in the `se3 merge` Concurrency Lock requirement (`se3-commands` spec).

**Acquisition protocol.** Before acquiring the main-worktree mutex for a step, the flow MUST follow a three-phase sequence:
1. **Non-blocking probe** — attempt the acquire non-blockingly first. When the lock is free it is taken immediately and **no** waiting state is produced (behavior is identical to the uncontended path, keeping the common case quiet).
2. **Mark and persist on contention** — only when the probe finds the lock genuinely held, the flow SHALL set the `waiting_for_lock` flag on the `FlowInstance`, persist `engine.json` with `FlowStatus` remaining `RUNNING` (so the daemon discovers it as an active flow), and emit one streaming *waiting-for-lock* event into the current step's history so the surface can render "running · waiting for lock".
3. **Blocking acquire** — then perform the blocking acquire and wait for the current holder to release.

**Clearing.** Once the lock is acquired, the flow SHALL clear `waiting_for_lock` and persist `engine.json` again, so the flow leaves the waiting sub-state as soon as it makes progress. The flag is cleared even when the blocking acquire reclaims a stale lock.

**Persistence and propagation.** `waiting_for_lock` is a boolean field on the `FlowInstance` (default `False`), persisted through `to_dict()` / `from_dict()` and mirrored in `engine.json`'s schema. It is emitted only while actually waiting (omitted from `engine.json` when `False`) to keep the persisted state minimal. The daemon aggregator surfaces it as a running sub-state on the flow snapshot, the server propagates it, and the frontend renders it as a visible *waiting for lock* indicator on the otherwise-running flow.

This contract is a **general** guarantee, not specific to discovery: even if a future step still needs to briefly wait for the lock, the UI MUST faithfully show that the flow has started and is waiting, and MUST NOT silently freeze at the "published" state.

#### Scenario: Uncontended acquire produces no waiting state
- **GIVEN** the main-worktree mutex is free
- **WHEN** a flow reaches a step that needs the lock and probes it non-blockingly
- **THEN** the flow acquires the lock immediately
- **AND** `waiting_for_lock` is never set and no waiting-for-lock event is emitted

#### Scenario: Contended acquire persists a visible waiting state before blocking
- **GIVEN** another holder currently owns the main-worktree mutex
- **WHEN** a flow's non-blocking probe finds the lock held
- **THEN** the flow sets `waiting_for_lock`, persists `engine.json` with status RUNNING, and emits a streaming waiting-for-lock event before it blocks on the acquire
- **AND** the daemon, server, and web console show the flow as running and waiting for the lock rather than stuck at "published"

#### Scenario: Flag is cleared once the lock is acquired
- **GIVEN** a flow is in the waiting-for-lock sub-state, blocked on the acquire
- **WHEN** the previous holder releases the lock and the flow acquires it
- **THEN** the flow clears `waiting_for_lock` and persists `engine.json` again, leaving the waiting sub-state

### Requirement: Commit Step Runtime-Leak Denylist

The `commit` step SHALL run a closed-set denylist guard between `git add -A` and `git commit` that soft-removes any staged path carrying an se3 runtime signature located outside the sole ignored runtime root `se3/`, then completes the commit normally — this guard is a regression backstop that MUST NEVER fail the step or block the flow.

The guard rests on a fixed invariant: every se3 runtime artifact lands inside the single git-ignored runtime root `se3/` (no leading dot), specifically the closed set of subtrees under `se3/` that `/se3/*` ignores — `cache/`, `history/`, `logs/`, `state/`, `tmp/`, `worktrees/`, `calls/`, `collab/`. Because that set is finite and known, distinguishing a "runtime leak" from a real artifact degenerates to a **pure path judgement** — no content-based semantic classification is performed. Anything not in that closed set (`src/`, `tests/`, `pyproject.toml`, `README.md`, `VERSIONS.md`, and the whitelist-tracked `se3/specs/`, `se3/issues/`, `se3/scripts/`, `se3/prompts/`, `se3/version-rules.md` …) is normal working output and is committed as usual.

**Detection (`_detect_runtime_leaks`, pure function):**
- Input is the staged path list (repo-root-relative, posix-style, as emitted by `git diff --cached --name-only`); output is the subset that are leaks, returned verbatim and in input order.
- A path whose **top-level** component is exactly `se3` is always exempt — it is either gitignored (`/se3/*`) or a whitelist-tracked artifact, hence normal output.
- **Rule (A):** any path whose top-level component is `.se3` is a leak. The dotted runtime root is an illegitimate mistyped location covered by no gitignore rule and is never valid.
- **Rule (B):** any path with a NON-top-level component equal to `se3` or `.se3` whose immediately following component is one of the closed-set runtime subtree names (`cache`, `history`, `logs`, `state`, `tmp`, `worktrees`, `calls`, `collab`) is a leak (e.g. `foo/se3/logs/x`, `.se3/archive/<slug>/se3/state/engine.json`).
- Source code where `se3` is merely a package directory (`src/se3/engine/...`) is exempt because the component following `se3` is not a runtime subtree name.

**Soft removal (scheme B):** the step takes the staged manifest (`git diff --cached --name-only`, `-z` preferred), feeds it to `_detect_runtime_leaks`, and for every hit runs `git restore --staged -- <paths>` (or an equivalent unstage) and logs the removed leak paths at WARNING level, then proceeds to `git commit` with the remaining legitimate staged content. The whole guard is fault-tolerant: any git-subprocess failure during detection or unstaging only warns and continues — it never returns `FAILED`.

**Emptied-index edge case:** if soft-removal leaves nothing staged (the only working-tree change was a runtime leak outside `se3/`), the step treats this as a no-op success exactly like the upfront "no changes to commit" path — it rolls back any applied/staged version bump, sets `commit_hash` to `no-changes` and `committed` to `False`, and returns `COMPLETED` rather than letting `git commit` fail with "nothing to commit".

#### Scenario: Stray `.se3/` runtime path is soft-removed before commit
- **GIVEN** the working tree contains a legitimate change plus a stray runtime path under `.se3/archive/.../se3/state/engine.json`
- **WHEN** the commit step runs after `git add -A`
- **THEN** `_detect_runtime_leaks` flags the `.se3/...` path as a leak
- **AND** the commit step unstages it via `git restore --staged` and logs a WARNING listing the removed path
- **AND** the commit proceeds and includes the legitimate change but not the leaked runtime path
- **AND** the step returns `COMPLETED` (it never fails or blocks the flow)

#### Scenario: Whitelisted and top-level se3/ artifacts commit normally
- **GIVEN** the staged set contains `se3/specs/foo/spec.md`, `se3/issues/open/x.yaml`, and `src/se3/engine/steps/commit.py`
- **WHEN** the commit step runs the runtime-leak guard
- **THEN** none of these paths is flagged as a leak (top-level `se3/` is exempt; `src/se3/...` is a package dir whose next component is not a runtime subtree name)
- **AND** all of them are committed as usual

#### Scenario: Index emptied by leak removal is treated as a no-op success
- **GIVEN** the only working-tree change is a runtime leak located outside `se3/`
- **WHEN** the commit step soft-removes it and the index becomes empty
- **THEN** the step rolls back any applied version bump, sets `commit_hash` to `no-changes` and `committed` to `False`, and returns `COMPLETED` without invoking a failing `git commit`

## Architecture

### Core Components

```
┌─────────────────────────────────────────────────────────────┐
│                     se3 run (CLI)                           │
└───────────────────────────┬─────────────────────────────────┘
                            │
┌───────────────────────────▼─────────────────────────────────┐
│                  State Machine                              │
│  ┌─────────────┐ ┌───────────┐ ┌───────────┐ ┌──────────────────┐ │
│  │ create_flow │→│ init_flow │→│ run_step  │→│transition_to_next│ │
│  └─────────────┘ └───────────┘ └───────────┘ └──────────────────┘ │
│         ↑                           │                  │          │
│         └───────────────────────────┴──────────────────┘          │
└───────────────────────────┬─────────────────────────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│ Step Handler │    │ Persistence  │    │ LLM Caller   │
│  (16 steps)  │    │(engine.json) │    │(claude -p)   │
│              │    │              │    │              │
└──────────────┘    └──────────────┘    └──────┬───────┘
                                               │
                                        ┌──────▼───────┐
                                        │ JSON Extract │
                                        │  (3 modes)   │
                                        └──────────────┘
```

### Data Model

**FlowInstance:**
- flow_id: Unique identifier
- task_description: Task description
- task_type: Task type
- status: Flow status (INIT, RUNNING, PAUSED, COMPLETED, FAILED, RECOVERING) — see the *Flow Status Lifecycle* requirement for details
- state: State object (current step, step history, selected steps)

**Step:**
- step_id: Unique identifier
- step_type: Step type (one of 16 types, including discovery, self_check, confirm, and 4 deprecated steps)
- status: Step status (PENDING, RUNNING, COMPLETED, PARTIAL, FAILED, RETRYING, PAUSED, REVISION_NEEDED) — see the *Step Status Lifecycle* requirement for details
- inputs: Input dictionary
- outputs: Output dictionary (all values must be JSON-serializable primitive types; enum values must be converted to strings via `.value` before being stored)
- retry_count: Retry count

**Discovery step special fields:**
- `discovery_state`: { round, history, mode }
- `refined_description`: Refined task description
- `conversation_history`: Conversation history record

## CLI Commands

### se3 run

The main entry command; creates or resumes a flow instance and executes it.

```bash
se3 run [TASK_DESCRIPTION] [OPTIONS]

Options:
  --resume, -r      Resume an interrupted flow
  --worktree        Run the flow in an isolated git worktree, auto-merge back on success
  --type, -t TYPE   Specify the task type (feature|bugfix|review|small|directive|discovery)
  --change, -c NAME Associate with the specified change
  --discover, -d    Discovery mode (requirement exploration)
  --flow-id ID      Resume the specified flow ID
```

### se3 status

Displays the current project status, including flow status, git status, pending human calls, etc.

```bash
se3 status [--format json]
```

## State File

The flow state is saved in `se3/state/engine.json`:

```json
{
  "flow_id": "uuid",
  "task_description": "...",
  "task_type": "feature",
  "status": "RUNNING",
  "state": {
    "current_step_id": "...",
    "selected_steps": ["analyze", "plan", ...],
    "steps": {...}
  }
}
```
