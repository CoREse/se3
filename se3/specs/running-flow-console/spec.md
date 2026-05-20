<!-- spec-format: v1 -->
# running-flow-console Specification

## Purpose

The `running-flow-console` subsystem defines the behavior of the web console's
running-flow interaction surface — the full-screen, chat-style view through
which a human observes and steers an in-progress `se3 run` flow. It replaces
the former right-hand 440px drawer plus the context-free "Respond to pending
call" modal: a running flow now opens in the same full-screen layout as the
history view, with the conversation as the scrollable main body, auxiliary
information (Overview / Steps / machine info) collected into a sidebar, and a
docked reply box at the bottom that is the single, always-present way to
respond.

Step content (DISCOVERY / ANALYZE / TEST / SELF_CHECK / VERIFY_SPEC /
UPDATE_SPEC / VERSION_ANALYZE / …) is tiled top-to-bottom inside the
conversation as a single continuous chat-stream with no per-block height cap,
so each step's output is fully visible and the page scrolls vertically like a
normal chat application instead of compressing into one screen.

Every point in a running flow that needs a human in the loop — a pending MCP
call, a Ctrl-C mid-flow interjection, a retry/failure decision, or a CLI
subprocess confirmation prompt — is surfaced as a status chip on the docked
reply bar, never as an inline card in the chat-stream. Selecting a chip
expands that intervention's full prompt / context / options directly above the
reply textarea and enables that same textarea as the reply input; this mirrors
the CLI's "you can only type when stdin is being read" behavior. The view is
implemented in `app.js` and shares the conversation rendering engine
(`normalizeRecord`, role-based collapse, Markdown rendering) with the history
view. The underlying transport — `kind`-tagged call files under `se3/calls/`
aggregated into `pending_calls`, filtered by flow_id — is owned by
`interaction_calls.py`, the daemon aggregator, and the `se3.daemon.protocol`
module (see the `base` spec's *Daemon Modules* / *Server Modules*
requirements).

## Requirements

### Requirement: Full-Screen Chat Layout for Running Flows

A running flow MUST open in a full-screen `#flow-view` modeled on the history
view, NOT in a narrow side drawer. The conversation element (`#flow-conversation`)
is the **sole vertical scroller** of the main column: the sidebar collects
Overview / Steps / machine info, and the docked reply box sits below the
conversation as a non-scrolling footer. The standalone, context-free
call-modal popup MUST NOT be used; `#flow-view` is the single interaction
surface for a running flow.

Each step's content (DISCOVERY / ANALYZE / TEST / SELF_CHECK / VERIFY_SPEC /
UPDATE_SPEC / VERSION_ANALYZE / …) MUST be tiled in full inside the
conversation, in chronological order, as a single continuous chat-stream.
Per-block height caps (e.g. a 140px ceiling on intervention context bodies,
or any other inner `max-height` that double-compresses already-cropped step
output) are NOT allowed: when content exceeds one screen the page scrolls
vertically, exactly like a normal chat application — the view MUST NOT try to
squeeze the entire flow into one viewport.

#### Scenario: Running flow opens in the full-screen view
- **WHEN** a user opens a running flow from the web console
- **THEN** the flow is shown in the full-screen `#flow-view`
- **AND** the conversation occupies the scrollable main body
- **AND** Overview / Steps / machine info are placed in the sidebar

#### Scenario: No context-free call modal
- **WHEN** a running flow has a pending interaction
- **THEN** the interaction is presented inside `#flow-view`
- **AND** no separate "Respond to pending call" modal popup is shown

#### Scenario: Conversation is the only vertical scroller
- **WHEN** `#flow-view` is rendered for a running flow
- **THEN** `#flow-conversation` is the only element in the main column that
  scrolls vertically
- **AND** the docked reply box stays fixed at the bottom of the main column
  and does not participate in conversation scrolling

#### Scenario: Step content tiles in full with no inner max-height
- **WHEN** the conversation contains step outputs (e.g. DISCOVERY, ANALYZE,
  TEST, SELF_CHECK, VERIFY_SPEC, UPDATE_SPEC, VERSION_ANALYZE)
- **THEN** each step's content is rendered top-to-bottom in full, with no
  per-block `max-height` that crops it to a small window
- **AND** when the total content exceeds one viewport, the conversation
  scrolls vertically as one continuous chat-stream

#### Scenario: Step blocks are not squeezed by flex layout
- **GIVEN** the conversation has multiple step blocks (`.history-step`)
  whose combined height exceeds the available column height
- **WHEN** the layout resolves
- **THEN** every direct step / record child of `#flow-conversation` is
  rendered at its natural content height — i.e., `flex-shrink: 0` is
  applied (or an equivalent rule) so the flex column does not squeeze
  step contents into header-only stubs
- **AND** the overflow is taken up by `#flow-conversation`'s
  `overflow-y: auto` vertical scroll, never by clipping individual step
  bodies

#### Scenario: Browser back button closes the flow view
- **GIVEN** the user has navigated into a running flow's `#flow-view`
- **WHEN** the browser back button is pressed (or any other action that
  triggers `popstate` away from the `#flow-view` history entry)
- **THEN** the `#flow-view` is closed and the previous list-level view
  (flow list / machine overview) is restored
- **AND** the browser does NOT navigate away from the console site
- **AND** clicking the ✕ button or pressing Escape inside `#flow-view`
  funnels through the same single collapse path (via `history.back()`)
  so back-button and explicit-close share one teardown sequence without
  pushing redundant history entries

#### Scenario: Expanding a folded block scrolls the new content into view
- **GIVEN** the conversation contains a foldable block (e.g. a collapsed
  `user` / `system` prompt chip, a raw-payload `view raw` toggle, or any
  other `makeFoldable` / `makeRawToggle` consumer) whose tail currently sits
  below the visible viewport
- **WHEN** the user expands the block
- **THEN** after the expand transition completes the consumer calls
  `Element.scrollIntoView({block: "nearest"})` on the freshly shown block so
  the newly revealed tail scrolls into the visible area
- **AND** collapsing the same block does NOT scroll — the reader's current
  position is left untouched

### Requirement: Docked Persistent Reply Box

The bottom of `#flow-view`'s main column MUST host a persistently docked
reply area, so a user can respond like a chat application without first
clicking a button or opening a popup. The reply area is composed of three
stacked parts, all sitting below the conversation:

1. **Pending intervention chip bar** — a row of status-style buttons, one
   per pending intervention (pending MCP call, retry decision, CLI confirm,
   etc.). Each chip shows the intervention kind and a short identifier.
2. **Reply context panel** — when a chip is selected, the panel above the
   textarea expands the selected intervention's full prompt (Markdown
   rendered), optional context block (no `max-height` truncation), and any
   `options` action buttons. When no chip is selected the panel is empty or
   shows a brief "no pending interaction" hint.
3. **Reply input row** — a single horizontal row carrying three elements,
   left-to-right: an inline **Interject icon button**, the **reply
   textarea**, and the **send button**. The Interject button is a compact
   icon control symmetric to Send; for an active flow that is not already
   waiting on a real interjection, clicking it materializes a synthetic
   `interjection` chip in the chip bar and selects it. The same textarea is
   reused for every intervention kind.

The reply **textarea is always enabled** so the user can draft text at any
time — like an ordinary chat application. The **send button** is the gate:
it is disabled while no target chip is selected (the draft has no
destination) and enabled the instant any chip (real or synthetic) becomes
the selected target. While a submission is in flight both controls are
disabled to prevent edit-during-send, then re-synced when the request
settles.

#### Scenario: Reply box is always present
- **WHEN** `#flow-view` is shown for any running flow
- **THEN** the docked reply area (chip bar + context panel + textarea + send
  button) is visible at the bottom of the main column
- **AND** it is visible without the user clicking a button or opening a popup

#### Scenario: Textarea stays enabled even with no pending interaction
- **WHEN** the running flow has no pending interaction and the user has not
  opted into interjection (chip bar is empty)
- **THEN** the reply textarea is **enabled** and accepts keystrokes
- **AND** the send button is **disabled** because there is no target to send to
- **AND** the placeholder advises that there is no target yet (e.g. "No
  pending interaction — draft a reply or click ✎ to interject…")

#### Scenario: Interject button is inline on the reply row
- **GIVEN** an active flow with no real pending interjection
- **WHEN** the docked reply box is rendered
- **THEN** the Interject button appears as an inline icon button at the left
  end of the reply input row, symmetric to Send
- **AND** it is NOT rendered as a separate full-width button on its own row

#### Scenario: Interject button opts the user into interjection mode
- **GIVEN** an active flow with no real pending interjection
- **WHEN** the user clicks the inline Interject icon button
- **THEN** a synthetic `interjection` chip appears in the chip bar and is
  selected
- **AND** the send button becomes enabled (the textarea was already enabled)
- **AND** sending the interjection consumes the opt-in (the synthetic chip
  disappears until the user clicks Interject again)

#### Scenario: Reply box activated by selecting a chip
- **WHEN** the running flow has at least one pending intervention and the
  user selects its chip in the docked chip bar
- **THEN** the reply context panel above the textarea expands the
  intervention's full kind header, Markdown-rendered prompt, optional
  untruncated context block, and any `options` action buttons
- **AND** the reply textarea and submit control become enabled
- **AND** the reply area clearly states which intervention it is targeting

#### Scenario: Pending interventions do not appear as cards in chat-stream
- **WHEN** a running flow has one or more pending interventions
- **THEN** they appear only as chips in the docked reply bar (with the full
  prompt/context/options expanded above the reply textarea on selection)
- **AND** they MUST NOT also be rendered as message cards inside the
  conversation chat-stream

#### Scenario: Submitted reply is inlined into the conversation
- **WHEN** the user submits a reply for a pending interaction
- **THEN** the reply is folded into the conversation flow in place

### Requirement: Unified Intervention Items

All human-in-the-loop interactions inside a running flow MUST be presented as
**chips on the docked reply bar**, never as inline cards mixed into the
conversation chat-stream. Each chip carries the intervention's kind icon and
a short label (and, for MCP calls, a truncated call_id); selecting a chip
expands that intervention's full `prompt` (Markdown rendered), optional
`context` block (no truncation), and any `options` action buttons inside the
reply context panel above the shared reply textarea. The same reply textarea
is the single input surface for every intervention kind.

The recognized intervention kinds are at least: (1) a pending MCP call
(`call`); (2) a post-Ctrl-C mid-flow interjection (`interjection`); (3) a
retry/failure decision (`retry_decision`); (4) a CLI subprocess confirmation
prompt (`cli_confirm`). Each chip is derived from a `pending_calls` entry
whose `kind` field identifies the interaction; an unrecognized `kind`
degrades to a plain `call` chip.

A synthetic `interjection` chip is **opt-in**, not always-on: an active flow
that is not already waiting on a real interjection MUST render an inline
Interject icon button at the left end of the reply input row (symmetric to
Send); clicking the button materializes a synthetic `interjection` chip and
selects it. The reply textarea itself stays enabled at all times so the user
can draft text freely — only the Send button is gated, and it is enabled
when (a) the user has opted into interjection or (b) a real pending
call/interjection is waiting. A successful interjection send consumes the
opt-in (the synthetic chip disappears and the inline Interject button
returns to its inactive state until the user clicks it again).

Expanding a folded long record or showing a raw-payload toggle MUST keep the
newly-revealed content visible: after the expand transition the consumer
SHOULD call `Element.scrollIntoView({block: "nearest"})` on the freshly
shown block so content past the viewport edge scrolls into view. Collapse
does **not** scroll — collapsing should leave the reader's current position
untouched.

#### Scenario: Each intervention kind is rendered as a chip on the reply bar
- **WHEN** a running flow has a pending interaction of kind `call`,
  `interjection`, `retry_decision`, or `cli_confirm`
- **THEN** it is rendered as a status-style chip button in the docked reply
  chip bar
- **AND** the chip is not rendered as a message card inside the chat-stream

#### Scenario: Selecting a chip expands its full context above the textarea
- **WHEN** the user selects an intervention chip
- **THEN** the reply context panel above the textarea shows the
  intervention's full kind header, Markdown-rendered `prompt`, optional
  untruncated `context` block, and any `options` action buttons
- **AND** the reply textarea + send button are enabled
- **AND** the same textarea is reused as the reply input regardless of
  intervention kind

#### Scenario: Unknown kind degrades to a plain call chip
- **WHEN** a `pending_calls` entry carries a `kind` value that is not one of
  the recognized kinds
- **THEN** it is rendered as a plain `call` chip

#### Scenario: Reply box targets a single intervention
- **WHEN** multiple intervention chips are present in the chip bar
- **THEN** at most one chip is selected at any time
- **AND** the reply context panel + reply textarea target exactly the
  selected intervention

### Requirement: Flow-Scoped Pending Interventions

Pending interventions surfaced in a running flow's `#flow-view` MUST be scoped
to that flow's own `flow_id`. The daemon aggregator (`DaemonAggregator`)
filters `FlowSnapshot.pending_calls` by `context.flow_id`, keeping only calls
whose `context.flow_id` equals the snapshot's `flow_id`.

Pending calls whose `context.flow_id` is **missing or empty** are treated as
**unattributed** and MUST be dropped from a flow-scoped snapshot. These
unattributed artifacts are typically left behind by other flows or scenarios
operating in the same project root (notably `merge_<branch>_<timestamp>.json`
files written by `HumanCallWriter` in `engine/merge/human_call.py`, and
`sync_conflicts_*.json` files from prior sync runs); they have no `context`
section identifying their owning flow and would otherwise bleed into every
flow's chip bar. Surfaces that need a non-scoped view (machine-wide pending
calls) use `MachineStatus.pending_calls`, which is **not** filtered.

To make the strict filter work end-to-end, every interaction-call writer that
runs inside a flow MUST populate `context.flow_id` with the current flow's
identifier. In particular, the CLI-subprocess confirmation handler
(`make_cli_confirm_handler` in `engine/interaction_calls.py`) MUST write its
`flow_id` and `step_id` inside the call file's `context` object, not as
top-level extras, because the aggregator filter inspects `context.flow_id`
and treats top-level fields as unattributed.

The frontend `pendingCalls(flow)` helper applies the same strict filter as a
defensive fallback against older daemon versions that have not yet been
upgraded.

#### Scenario: Calls with matching flow_id are surfaced
- **GIVEN** the current running flow has `flow_id = "F1"`
- **WHEN** a pending call file under `se3/calls/` carries
  `context.flow_id = "F1"`
- **THEN** the call appears as a chip in the docked reply chip bar for that
  flow

#### Scenario: Calls with a different flow_id are hidden
- **GIVEN** the current running flow has `flow_id = "F1"`
- **WHEN** a pending call file under `se3/calls/` carries
  `context.flow_id = "F2"` (some other flow on the same project root)
- **THEN** that call MUST NOT appear in `F1`'s chip bar or anywhere in
  `F1`'s `#flow-view`

#### Scenario: Unattributed legacy calls are dropped
- **GIVEN** the current running flow has `flow_id = "F1"`
- **WHEN** a pending call file (e.g. `merge_<branch>_<timestamp>.json` or
  `sync_conflicts_*.json`) has no `context.flow_id`, or its `context.flow_id`
  is `null` or an empty string
- **THEN** the call MUST NOT appear in `F1`'s chip bar
- **AND** machine-wide aggregation (`MachineStatus.pending_calls`) still
  enumerates the call for host-level views

#### Scenario: cli_confirm calls carry flow_id in context
- **GIVEN** a flow with `flow_id = "F1"` triggers a CLI-subprocess
  confirmation prompt that the agent runner captures
- **WHEN** `make_cli_confirm_handler` writes the `cli_confirm` call file
- **THEN** the file's `context` object contains `flow_id = "F1"` (and
  `step_id` when known)
- **AND** the aggregator's per-flow filter scopes the call to `F1` only, so a
  concurrent flow `F2` does not see it in its chip bar

### Requirement: Role-Based Message Collapse

The conversation flow MUST default to highlighting the assistant's real output
and the human-intervention items (both default-expanded). The collapse rule for
template-style prompt roles (`user` / `system`) MUST be deterministic, based
solely on the record's structured `role` field — `user` / `assistant` /
`system`, with `human` folded into `user` — and MUST NOT rely on guessing from
message text. Content is never deleted, only collapsed by default.

**`system` role** messages collapse to a single chip in their entirety (e.g.
"system prompt · discovery mode"); clicking the chip expands the original
content unchanged.

**`user` role** messages are split at the backend-provided sentinel marker
pair defined in `src/se3/engine/prompt_markers.py` (`TEMPLATE_PREFIX_END` /
`USER_CONTENT_BEGIN`). When a `user` message contains the marker pair, the
prefix segment (template/system-instructions boilerplate, e.g. the
`You are an expert software engineer...` opener plus the Agent Safety / Process
Cleanup boilerplate that every step shares) MUST render as a default-collapsed
clickable chip, and the suffix segment (the task/spec/context — the user's
real input as the backend assembled it) MUST render as a normal default-
expanded bubble. The chip and bubble MUST coexist within the same logical
record so collapse never hides the user's real input. A `user` message that
lacks the marker pair (legacy history written before the marker protocol) MUST
fall back to the previous behavior of rendering the entire record as a single
collapsed chip.

Step-prompt modules under `src/se3/engine/steps/` MUST inject the boundary
markers via `prompt_markers.inject_boundary` (or `wrap_user_content`) at the
point where the boilerplate prefix ends and the user/task-specific content
begins. This applies to all step prompt templates that assemble both halves —
analyze, plan, plan_tasks, implement (`IMPLEMENT_PROMPT`,
`IMPLEMENT_GROUP_PROMPT`, `FIX_PROMPT`), discovery (initial + continue),
self_check, verify_spec, update_spec, summarize, and version_analyze — so the
frontend has a reliable, text-pattern-free split signal.

#### Scenario: Assistant output defaults to expanded
- **WHEN** a conversation record has the `assistant` role
- **THEN** it is rendered expanded, highlighting the assistant's real output

#### Scenario: System role collapses to a single chip
- **WHEN** a conversation record has the `system` role
- **THEN** it is rendered as a single collapsed, clickable chip
- **AND** clicking the chip expands the original content unchanged

#### Scenario: User message with sentinel markers splits prefix and content
- **GIVEN** a `user` role record whose body contains the
  `TEMPLATE_PREFIX_END` / `USER_CONTENT_BEGIN` marker pair injected by a step
  prompt module
- **WHEN** the record is rendered in the running-flow conversation
- **THEN** the segment before `TEMPLATE_PREFIX_END` is rendered as a default-
  collapsed clickable chip labeled as a system-prompt prefix
- **AND** the segment after `USER_CONTENT_BEGIN` is rendered as a normal
  default-expanded `user` bubble
- **AND** the user's real input is visible without any click

#### Scenario: User message without markers falls back to whole-chip
- **GIVEN** a `user` role record from legacy history that does NOT contain the
  marker pair
- **WHEN** the record is rendered
- **THEN** the entire body is rendered as a single collapsed clickable chip,
  matching the prior whole-message behavior

#### Scenario: human role folded into user
- **WHEN** a record's `role` field is `human`
- **THEN** it is classified as `user` and follows the same marker-aware
  rendering rules (split when marker present, fall back to whole-chip
  otherwise)

#### Scenario: Classification is role-based, not text-based
- **WHEN** deciding how to collapse a record
- **THEN** the role decision is made only from the structured `role` field
- **AND** the prefix/content split is made only from the structured sentinel
  marker pair, never from pattern-matching the message prose

### Requirement: Per-Step Report Cards

For every `step_completed` event surfaced in a running flow's
`#flow-view`, the web console MUST render an additional structured **report
card** (e.g. a `.step-report` block) directly after the existing raw event
chip / record for that step. The report card is the web counterpart of the
Rich `Panel`-rendered end-of-step report produced by the CLI sink in
`src/se3/engine/step_renderers.py` (Work Summary / Verification Result /
Discovery Result / Plan / Analyze result / etc.).

The report card MUST:

- Be **default-expanded**, with NO `max-height` cap on its body — the user
  reads the structured report at a glance, not after a click.
- **Coexist with** the existing raw `step_completed` message rendering rather
  than replace it: the raw record (foldable chip / NDJSON view) stays where it
  is, and the report card is rendered alongside it.
- Be routed by `step.step_type` to a dedicated small renderer that consumes
  the same structured fields the CLI Panel reads from `step.outputs`. Field
  parity with `step_renderers.py` is required: every step type that has a CLI
  Panel renderer MUST have a corresponding web renderer (analyze, plan,
  implement, test, self_check, verify_spec, update_spec, commit,
  version_analyze, summarize, discovery — adding new step types adds a new
  renderer).
- Render structured output (markdown, tables, field lists) rather than
  re-dumping the raw JSON blob.

To make this work end-to-end, the engine sink layer MUST also persist
`step_completed` events into the per-step jsonl files consumed by the daemon
history reader (e.g. via an unconditionally-subscribed `HistorySink` in
`src/se3/engine/sink.py` wired up from `src/se3/commands/run.py`), so that the
report card has access to the same `outputs` dict that the CLI Panel sees —
without breaking the CLI history viewer (`get_step_history` skips these
records on the CLI side).

#### Scenario: Each completed step renders a report card
- **WHEN** the running flow emits a `step_completed` event of a known step
  type (analyze, plan, implement, test, self_check, verify_spec, update_spec,
  commit, version_analyze, summarize, discovery)
- **THEN** the conversation gains a `.step-report` card for that step, in
  addition to the raw `step_completed` record
- **AND** the card is rendered default-expanded with no inner `max-height`
  cropping its body

#### Scenario: Report card mirrors CLI Panel fields
- **WHEN** a step type's CLI renderer in `src/se3/engine/step_renderers.py`
  displays a specific set of fields from `step.outputs`
- **THEN** the corresponding web report renderer MUST surface the same field
  set (mapped to markdown / tables / field lists), so the web and CLI users
  see the same structured report content

#### Scenario: Report card does NOT replace the raw event record
- **GIVEN** a `step_completed` event for a step
- **WHEN** the conversation is rendered
- **THEN** both (a) the existing raw event record (foldable chip / NDJSON
  view) and (b) the new `.step-report` card are present
- **AND** the raw record is NOT hidden or removed by the introduction of the
  report card

### Requirement: New Task — Arbitrary Project Root

The web console's "New Task" form MUST allow the user to start a flow against
**any** project root on the selected machine, not only roots the daemon has
already seen as live in its current process lifetime. To satisfy this the form
provides two complementary entry points for the `project_root` field, both
sourced from the selected machine's record:

1. **Known-project dropdown** — populated from
   `MachineRecord.project_roots` (the union of the daemon's live registered
   roots and the historical roots enumerated from `se3/history/` and
   `se3/state/archive/`; see the `aggregator.py` bullet in the `base` spec).
2. **`Other path…` sentinel option** — always appended to the dropdown,
   including when the machine has zero known roots. Selecting it reveals a
   text input that accepts an absolute path; the entered path is sent as
   `project_root` to `POST /api/flows`.

The server endpoint `POST /api/flows` MUST validate only that the supplied
`project_root` is an absolute path; it MUST NOT reject paths that are absent
from the machine's known-roots list. Membership in `project_roots` is a hint
for the dropdown, not a precondition for spawning.

When the user-supplied target directory is not yet an SE3 project (no
`se3/specs/base/spec.md` marker), the daemon MUST initialize it on the user's
behalf before spawning the flow — see the `spawner.py` / `client.py` bullets
in the `base` spec and the `se3-commands` `se3 init` requirement; the New
Task form itself need not require the user to pre-initialize the directory.

#### Scenario: Dropdown lists known projects and an Other-path entry
- **GIVEN** the user opens the New Task form for a machine that reports
  `project_roots = ["/path/A", "/path/B"]`
- **WHEN** the project dropdown is rendered
- **THEN** the dropdown lists `/path/A`, `/path/B`, and an `Other path…`
  sentinel entry

#### Scenario: Other-path entry available even with zero known roots
- **GIVEN** the selected machine has an empty `project_roots` list
- **WHEN** the New Task form is rendered
- **THEN** the project dropdown still offers the `Other path…` entry so the
  user can type an absolute path manually
- **AND** the form can submit a flow against that path

#### Scenario: Server accepts absolute path outside known roots
- **GIVEN** the user submits a New Task with an absolute `project_root` that
  is NOT listed in any machine's `project_roots`
- **WHEN** `POST /api/flows` validates the request
- **THEN** the endpoint accepts the request as long as the path is absolute
- **AND** the request is dispatched to the selected machine's daemon for
  spawning

#### Scenario: Brand-new directory completes init+run from the web
- **GIVEN** the user picks `Other path…` and types an absolute path to an
  empty directory that has never been an SE3 project
- **WHEN** the flow is submitted
- **THEN** the daemon first initializes the directory as an SE3 project (per
  the spawner/client bullets in the `base` spec), registers the new root, and
  then continues with the normal spawn path
