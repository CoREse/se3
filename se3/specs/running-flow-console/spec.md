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
expands that intervention's full prompt / options directly above the
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
  other `makeFoldable` / `makeRawToggle` / `makeAssistantRawToggle` consumer)
  whose tail currently sits below the visible viewport
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
   per pending intervention (e.g. a generic pending reply, a retry
   decision, a CLI subprocess confirmation). Each chip shows the
   intervention kind via a **user-facing neutral label** (e.g. "待回复" /
   "需要决策" / "需要确认") and MUST NOT expose the internal transport
   vocabulary — strings such as `MCP`, `call_id`, or `call <id>` MUST NOT
   appear as visible chip text. The underlying call identifier MAY be
   preserved on a hidden `data-call-id` attribute and inside the chip's
   `title` hover tooltip for developer debugging only.
2. **Reply context panel** — when a chip is selected, the panel above the
   textarea renders, for **every** intervention kind, only the intervention's
   kind header, its full prompt (Markdown rendered), and any `options` action
   buttons. It MUST NOT render a separate `context` block — the panel never
   duplicates context text that the prompt already carries. When no chip is
   selected the panel is empty or shows a brief "no pending interaction" hint.
3. **Reply input row** — a single horizontal row carrying three elements,
   left-to-right: an inline **Interject icon button**, the **reply
   textarea**, and the **send button**. The Interject button is a compact
   icon control symmetric to Send; for an active flow that is not already
   waiting on a real interjection, clicking it materializes a synthetic
   `interjection` chip in the chip bar and selects it. The same textarea is
   reused for every intervention kind. The reply textarea MUST open at a
   multi-line default height (roughly six rows tall) so a user can draft and
   review a multi-line reply without cramping, and it MUST remain manually
   resizable along the vertical axis (`resize: vertical`); a cramped two-row
   default is not allowed.

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

#### Scenario: Reply textarea opens tall enough for multi-line editing
- **WHEN** the docked reply box is rendered
- **THEN** the reply textarea opens at a multi-line default height (roughly
  six rows) rather than a cramped two-row box
- **AND** the user can drag the textarea to resize it vertically

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
  intervention's full kind header, Markdown-rendered prompt, and any
  `options` action buttons (no separate context block)
- **AND** the reply textarea and submit control become enabled
- **AND** the reply area clearly states which intervention it is targeting

#### Scenario: Pending interventions do not appear as cards in chat-stream
- **WHEN** a running flow has one or more pending interventions
- **THEN** they appear only as chips in the docked reply bar (with the full
  prompt/options expanded above the reply textarea on selection)
- **AND** they MUST NOT also be rendered as message cards inside the
  conversation chat-stream

#### Scenario: Submitted reply is inlined into the conversation
- **WHEN** the user submits a reply for a pending interaction
- **THEN** the reply is folded into the conversation flow in place

#### Scenario: Chip and reply header use neutral, user-facing labels
- **GIVEN** a running flow has one or more pending interventions of any
  recognized kind
- **WHEN** the docked reply chip bar and the reply context panel header
  are rendered
- **THEN** the chip label is a user-facing neutral phrase tied to the
  intervention's kind (e.g. "待回复" / "插话" / "需要决策" / "需要确认")
- **AND** neither the chip label, the chip's visible secondary text, nor
  the reply context panel header contain the literal substrings `MCP`,
  `call_id`, or the pattern `call <id>` as visible text
- **AND** the underlying call identifier, if preserved, appears only on
  hidden DOM attributes (e.g. `data-call-id`) or hover `title` tooltips —
  never in the rendered text content

### Requirement: Unified Intervention Items

All human-in-the-loop interactions inside a running flow MUST be presented as
**chips on the docked reply bar**, never as inline cards mixed into the
conversation chat-stream. Each chip carries the intervention's kind icon and
a short, **user-facing neutral label** describing the interaction kind —
implementation-detail vocabulary such as `MCP`, `call_id`, or the
literal pattern `call <id>` MUST NOT appear in the chip's visible text. The
underlying call identifier, when retained for debugging, lives only on hidden
DOM attributes (e.g. `data-call-id`) and `title` hover tooltips, not in any
text node a screen reader or sighted user can read. Selecting a chip expands
that intervention's full `prompt` (Markdown rendered) and any `options` action
buttons inside the reply context panel above the shared reply textarea; the
panel MUST NOT render a separate `context` block for any kind. The same reply
textarea is the single input surface for every intervention kind.

The recognized intervention kinds are at least: (1) a pending MCP call
(`call`); (2) a post-Ctrl-C mid-flow interjection (`interjection`); (3) a
retry/failure decision (`retry_decision`); (4) a CLI subprocess confirmation
prompt (`cli_confirm`); (5) a non-interactive discovery confirmation gate
(`discovery_confirm`). Each chip is derived from a `pending_calls` entry
whose `kind` field identifies the interaction; an unrecognized `kind`
degrades to a plain `call` chip.

A `discovery_confirm` chip is produced when a discovery flow pauses at the
programmatic confirmation gate (see the `flow-engine` *Discovery Workflow*
requirement) — whether that flow was daemon-spawned (non-interactive) or
started interactively from the CLI, because the interactive pause is now
mirrored to the same `se3/calls/` call file so the web console can answer it
in the live process (terminal and web are awaited in parallel; whichever
answers first drives the flow). The chip carries the LLM's refined task
description in its `prompt` and at least one `options` entry encoding the
one-click confirm action whose **value is the literal `"1"`** — the exact
token the gate's `== "1"` check expects. The reply context panel MUST render
both affordances side by side: a GUI confirm button (clicking it sends `"1"`
through the same call/response reply channel every other chip uses) **and**
the `输入 1 确认` textual hint as a fallback, so a user who ignores the button
can still type `1`. Because the confirm value is fixed by the gate, the
frontend MUST guarantee the confirm button even when the backend call file
omitted the `options` array — it synthesizes a single confirm option whose
value is `"1"` so the button and the textual hint always coexist.

The reply context panel MUST NOT render a separate `context` block for **any**
intervention kind. The `context` text duplicates information the `prompt`
already carries (e.g. a `discovery_confirm` whose `prompt` embeds the refined
task description and whose backend also mirrors it into
`context.refined_description`), so rendering it would repeat the same content
directly below the prompt. This context suppression is **uniform across all
kinds** (`call` / `interjection` / `retry_decision` / `cli_confirm` /
`discovery_confirm`); `discovery_confirm` is no longer a special case — every
kind renders only the kind header, the Markdown prompt, and any `options`
buttons.

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

#### Scenario: Discovery confirmation chip offers a confirm button and a textual fallback
- **GIVEN** a daemon-spawned discovery flow has paused at the programmatic
  confirmation gate, surfacing a `pending_calls` entry of kind
  `discovery_confirm` whose `prompt` carries the refined task description
- **WHEN** the user selects the chip
- **THEN** the reply context panel renders a GUI confirm button whose click
  sends the literal `"1"` through the shared call/response reply channel
- **AND** the panel also shows the `输入 1 确认` textual hint so the user can
  type `1` manually instead
- **AND** when the backend call file omitted the `options` array, the frontend
  still synthesizes the confirm button with value `"1"`

#### Scenario: Discovery confirmation chip does not duplicate the refined description
- **GIVEN** a `discovery_confirm` chip whose `prompt` already contains the
  refined task description and whose `context.refined_description` carries the
  same text
- **WHEN** the user selects the chip
- **THEN** the reply context panel renders the prompt and the confirm
  affordances but does NOT render the `context` block beneath the prompt
- **AND** every other intervention kind (`call` / `interjection` /
  `retry_decision` / `cli_confirm`) likewise renders no `context` block — the
  context suppression is uniform across all kinds, not a `discovery_confirm`
  special case

#### Scenario: Selecting a chip expands its prompt and options above the textarea
- **WHEN** the user selects an intervention chip
- **THEN** the reply context panel above the textarea shows the
  intervention's full kind header, Markdown-rendered `prompt`, and any
  `options` action buttons — and renders no separate `context` block
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

#### Scenario: No implementation vocabulary in visible chip text
- **GIVEN** the chip bar renders chips for every recognized intervention
  kind (`call`, `interjection`, `retry_decision`, `cli_confirm`)
- **WHEN** the rendered chip DOM is inspected
- **THEN** the visible text content of every chip MUST NOT contain the
  substrings `MCP`, `call_id`, or the pattern `call <hex-id>`
- **AND** any preserved call identifier appears only on the chip's
  `data-call-id` attribute or `title` tooltip, never in visible text

### Requirement: Conversation Strict Chronological Order

All records that ride a running flow's conversation channel — `user`,
`assistant`, `system`, the `step_completed` / `step_failed` events surfaced
as report cards, and any other in-stream record — MUST be rendered in a
**single strict chronological order keyed by record timestamp**, across all
roles AND across all step boundaries. Step grouping is allowed only as a
*visual* affordance (e.g. a lightweight `.history-step-header` separator row
inserted between adjacent records whose step changes); it MUST NOT shuffle
records out of timestamp order. Concretely: a `user` reply produced between
two `assistant` outputs of a `discovery` step MUST appear between them in
the rendered timeline, even if the `user` reply's `step_id` momentarily
classifies it under a different step section.

The ordering key is `(timestamp, original-index)`: records with equal
timestamps preserve the input order they arrived in (stable sort by NDJSON
position). Records that arrive late (e.g. via incremental append) MUST be
inserted into their correct global slot, not unconditionally appended at the
tail. Stateful UI affordances of already-rendered records — fold state, raw
toggles, chip selections — MUST NOT be disturbed by an out-of-order
insertion or by a step-header rebuild that follows it.

#### Scenario: Records sort by timestamp across role and step boundaries
- **GIVEN** a conversation NDJSON containing, in this timestamp order:
  `assistant A1` (step=discovery, ts=1), `user U1` (step=discovery_continue,
  ts=2), `assistant A2` (step=discovery, ts=3)
- **WHEN** the conversation is rendered in `#flow-view`
- **THEN** the visible order is `A1` → `U1` → `A2`
- **AND** the rendered order matches the records' timestamp order even
  though `U1` and the two assistant outputs map to different step keys

#### Scenario: Step headers are visual separators, not reorderers
- **GIVEN** the conversation contains records that interleave two step keys
  in their natural timestamp order
- **WHEN** the renderer rebuilds the `.history-step-header` separator rows
- **THEN** headers are inserted only between adjacent records whose step
  key changes
- **AND** no record is moved out of its timestamp-ordered slot to make a
  step section "contiguous"

#### Scenario: Late-arriving record is inserted in its timestamp slot
- **GIVEN** the conversation already shows records with timestamps
  `t=1, t=3, t=5`
- **WHEN** an incremental append delivers a new record with timestamp `t=2`
- **THEN** the new record is inserted between `t=1` and `t=3`, not appended
  at the tail
- **AND** existing records' fold state, raw toggles, and chip selections
  are preserved across the rebuild

### Requirement: Long-Content Wrapping

All long-line text content rendered inside the running-flow conversation —
including assistant Markdown code blocks (`.conv-bubble .md-code`), inline
raw JSON / NDJSON viewers (`.raw-json`), step report markdown code blocks
(`.step-report__markdown .md-code`), and any equivalent `<pre>` /
code-style block reachable from a chat bubble — MUST wrap to the
container's width. The CSS rules for these selectors MUST set
`white-space: pre-wrap` together with a per-character break rule
(`overflow-wrap: anywhere` and/or `word-break: break-word`) so that a long
single-line payload (e.g. a 200+ character JSON string with no spaces) is
laid out across multiple visual lines. An inline horizontal scrollbar
(`overflow-x: auto`) MUST NOT appear inside the conversation; horizontal
overflow MUST be `hidden` or removed for these selectors. The
`.raw-json` viewer MAY keep a vertical scrollbar via a `max-height` +
`overflow-y: auto` to bound its visual footprint, but horizontal scroll is
still forbidden.

#### Scenario: Long single-line JSON wraps inside a code block
- **GIVEN** an assistant bubble renders a Markdown code block whose body
  is a single 200+ character JSON string with no whitespace
- **WHEN** the bubble is rendered in the running-flow conversation
- **THEN** the code block wraps the long line across multiple visual lines
  using `white-space: pre-wrap` + `overflow-wrap: anywhere` (or
  `word-break: break-word`)
- **AND** the code block does NOT show an internal horizontal scrollbar

#### Scenario: Raw JSON viewer wraps but allows vertical scroll
- **GIVEN** the `view raw` toggle for a record produces a `.raw-json`
  block whose serialized body contains a very long single line
- **WHEN** the viewer is rendered
- **THEN** the long line wraps to the container width with no horizontal
  scrollbar appearing inside the viewer
- **AND** the viewer MAY still cap its height and scroll vertically via
  `overflow-y: auto`

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

Beyond flow-id scoping, pending calls also have a **flow-progress-bound
lifetime**: while a flow is running, the aggregator MUST stop surfacing a
pending call once the flow has moved past the step that call belongs to. An
interactive `call` / `cli_confirm` / `discovery_confirm` answered at the CLI
terminal is typically consumed directly by the run loop without writing a
`.response` / `.response.json` sibling file, so the existing
already-answered-by-sibling-file check can never clear it and the stale "待回复"
chip would otherwise persist for the entire run. To close this, the aggregator
(`DaemonAggregator._enumerate_calls` / `_snapshot_for_root`) MUST treat a call
as no longer pending when its owning `context.step_id` is no longer the flow's
current step (or that step has already reached a COMPLETED / REVISION-handled
status), and drop it from the flow's `pending_calls` even when no sibling
response file exists. Calls whose step is still the current, unanswered step
remain pending and continue to surface as chips.

Pending calls are additionally **deduplicated per `(flow_id, step_id)`, newest
wins** (`DaemonAggregator._dedup_calls_by_step`). An interactive discovery flow
reuses the *same* `step_id` across successive clarification turns and the
confirmation gate, so each new pause writes a fresh call file keyed to the same
`(flow_id, step_id)`. Only the most recent such call is a live interaction; the
earlier ones are superseded leftovers that, without dedup, would pile up as
stale "待回复" chips. The aggregator therefore keeps only the newest unanswered
call per `(flow_id, step_id)` and discards the rest. Calls that cannot be keyed
(missing `flow_id` or `step_id`) are exempt from dedup and are never collapsed
against one another.

#### Scenario: Superseded same-step calls are deduplicated to the newest
- **GIVEN** an interactive discovery flow `F1` whose current step `S` wrote two
  successive call files (an earlier clarification call and a newer one) both
  keyed to `(flow_id = "F1", step_id = "S")`
- **WHEN** the aggregator enumerates `F1`'s `pending_calls`
- **THEN** only the newest call for `(F1, S)` is reported as pending
- **AND** the earlier, superseded call is dropped so no stale "待回复" chip
  accumulates
- **AND** any call that cannot be keyed (missing `flow_id` or `step_id`) is left
  untouched by the dedup pass

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

#### Scenario: Pending call clears once the flow advances past its step
- **GIVEN** a running flow `F1` surfaced a pending call (e.g. a `cli_confirm`
  or `discovery_confirm`) whose `context.step_id` was the flow's current step
- **WHEN** the CLI answers the call and the flow advances so that step is no
  longer the current step (or it has reached a COMPLETED / REVISION-handled
  status), even though no `.response` / `.response.json` sibling file was
  written
- **THEN** the aggregator stops reporting the call in `F1`'s `pending_calls`
  and the stale "待回复" chip disappears from `F1`'s docked reply bar during the
  run, not only after the flow archives
- **AND** a call whose step is still the current, unanswered step remains
  pending and continues to surface as a chip

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

**`user` role** messages are split at the backend-provided three-segment
sentinel marker protocol defined in `src/se3/engine/prompt_markers.py`
(`TEMPLATE_PREFIX_END` / `USER_CONTENT_BEGIN` / `USER_CONTENT_END`). When a
`user` message contains the full three-marker sequence in order, the message
is split into three segments — a **prefix** (the template/system-instructions
boilerplate, e.g. the `You are an expert software engineer...` opener plus the
Agent Safety / Process Cleanup boilerplate that every step shares, project
context, embedded specs, JSON-format scaffolding, Discovery Context wrapper,
etc.), a **user-content section** (the user's literal input), and a **suffix**
(framework text appended after the user input, such as Available Specs /
Guidelines / language instructions / Runtime Environment / READ-ONLY
CONSTRAINT). The **user-content section** MUST be rendered as a normal
default-expanded `user` bubble; the **prefix** and **suffix** MUST be merged
into a single default-collapsed clickable chip (e.g. labeled "system prompt ·
{step}") so collapse never hides the user's real input. When the chip is
expanded, the prefix and suffix MAY be presented as two clearly labeled
sub-sections inside the chip (e.g. "模板前缀" / "框架后缀") so a developer can
distinguish what came before vs. after the user's literal input.

**Normative constraint on user-content scope:** the user-content section MUST
contain only text the user literally contributed at that step boundary —
e.g. the discovery initial_description, the user's reply in a discovery
continue turn, or the body of a mid-flow interjection. All framework-injected
strings — Project Context, Available Specs, embedded base spec text, the
Discovery Context wrapper, JSON-format scaffolding, Guidelines, Handling
Evaluative sections, language directives, Runtime Environment injections,
READ-ONLY CONSTRAINT, and any other prompt prose the engine itself writes —
MUST live in the prefix or suffix, NOT in the user-content section. This
keeps the default-expanded `user` bubble visually quiet: it always shows
exactly what the human typed, never the surrounding scaffolding.

When a `user` message contains only the two-marker pair (`TEMPLATE_PREFIX_END`
+ `USER_CONTENT_BEGIN`) without `USER_CONTENT_END` — the legacy two-segment
form produced by the older `inject_boundary` / `wrap_user_content` helpers —
the frontend MUST degrade gracefully to two-segment semantics: everything
before `TEMPLATE_PREFIX_END` is treated as the prefix chip, and everything
after `USER_CONTENT_BEGIN` is treated as the suffix (with an empty
user-content section, so no user bubble is rendered). A `user` message that
lacks the marker protocol entirely (legacy history written before any marker
was introduced) MUST fall back to the original behavior of rendering the
entire record as a single collapsed chip.

The original NDJSON for any collapsed record (查看原始) MUST stay reachable, but
it is **nested inside the record's own expand area** — the collapsed chip's
expand detail for a whole-chip / empty-user-content record, or the Layer-2
"展开全部" area for a marker-split `user` message (see *Three-Tier Progressive
Disclosure*) — NOT surfaced as a row-level always-visible control beside the
record.

Step-prompt modules under `src/se3/engine/steps/` MUST inject the boundary
markers via the helpers in `prompt_markers.py`. Step prompts whose template
assembles a real user-literal field (e.g. discovery's `initial_description` /
`user_response`) MUST use `wrap_user_section(prefix, user_content, suffix)` to
emit the full three-marker sequence around the user-literal field, so the
frontend can render a proper user bubble. Step prompts with no user-literal
field MAY continue to use the legacy `inject_boundary` / `wrap_user_content`
helpers (two-marker form) and rely on the frontend's two-segment fallback.
This applies to all step prompt templates — analyze, plan, plan_tasks,
implement (`IMPLEMENT_PROMPT`, `IMPLEMENT_GROUP_PROMPT`, `FIX_PROMPT`),
discovery (initial + continue), self_check, verify_spec, update_spec,
summarize, and version_analyze — so the frontend has a reliable,
text-pattern-free split signal.

Because the frontend splits **persisted** conversation records (not the live
LLM prompt), the marker sequence MUST be present in the `user` record as
written to the chat-history jsonl — not only in the prompt string handed to
the LLM. The split is data-driven: a `user` record whose stored body lacks the
markers cannot be split, so the engine MUST persist the marker-wrapped body so
the frontend has the data to separate the user's literal input from the
framework boilerplate.

#### Scenario: Assistant output defaults to expanded
- **WHEN** a conversation record has the `assistant` role
- **THEN** it is rendered expanded, highlighting the assistant's real output

#### Scenario: System role collapses to a single chip
- **WHEN** a conversation record has the `system` role
- **THEN** it is rendered as a single collapsed, clickable chip
- **AND** clicking the chip expands the original content unchanged

#### Scenario: User message with three-segment markers splits prefix, content, and suffix
- **GIVEN** a `user` role record whose body contains the full three-marker
  sequence (`TEMPLATE_PREFIX_END`, then `USER_CONTENT_BEGIN`, then
  `USER_CONTENT_END`, in that order) injected by a step prompt module via
  `wrap_user_section`
- **WHEN** the record is rendered in the running-flow conversation
- **THEN** the segment before `TEMPLATE_PREFIX_END` (prefix) and the segment
  after `USER_CONTENT_END` (suffix) are merged into a single default-collapsed
  clickable chip labeled as a system-prompt chip for that step
- **AND** the segment between `USER_CONTENT_BEGIN` and `USER_CONTENT_END` is
  rendered as a normal default-expanded `user` bubble
- **AND** the user's real input is visible without any click

#### Scenario: User-content section contains no framework-injected text
- **GIVEN** a `user` role record produced by the discovery step where the
  initial_description is the only user-literal text and the template otherwise
  contains Project Context, embedded specs, Discovery Context wrapper, the
  JSON-format scaffolding, Guidelines, language instructions, Runtime
  Environment injection, and the READ-ONLY CONSTRAINT
- **WHEN** the record is rendered in the running-flow conversation
- **THEN** the default-expanded `user` bubble contains ONLY the
  initial_description (exactly what the user typed at the discovery boundary)
- **AND** the bubble does NOT contain any framework-injected substrings such
  as "Project Context", "Available Specs", "Discovery Context", "Respond in
  JSON format", "Guidelines", "READ-ONLY", language directives, or the
  Runtime Environment heading
- **AND** the framework-injected prefix and suffix are placed in the
  collapsed system-prompt chip instead

#### Scenario: Legacy two-marker user message degrades gracefully
- **GIVEN** a `user` role record that contains only the older two-marker pair
  (`TEMPLATE_PREFIX_END` + `USER_CONTENT_BEGIN`) emitted by `inject_boundary`
  or `wrap_user_content`, without a `USER_CONTENT_END` marker
- **WHEN** the record is rendered in the running-flow conversation
- **THEN** `splitUserPromptByMarker` returns a result whose user-content
  section is empty and whose suffix carries everything after
  `USER_CONTENT_BEGIN`
- **AND** the record is rendered as a single collapsed system-prompt chip
  (combining the prefix and the suffix) with no separate user bubble
- **AND** no exception is raised and the rendering does not regress

#### Scenario: User message without any markers falls back to whole-chip
- **GIVEN** a `user` role record from legacy history that does NOT contain any
  of the three sentinel markers
- **WHEN** the record is rendered
- **THEN** the entire body is rendered as a single collapsed clickable chip,
  matching the prior whole-message behavior

#### Scenario: human role folded into user
- **WHEN** a record's `role` field is `human`
- **THEN** it is classified as `user` and follows the same marker-aware
  rendering rules (three-segment split when the full marker sequence is
  present, two-segment degradation when only the legacy pair is present,
  whole-chip fallback otherwise)

#### Scenario: Classification is role-based, not text-based
- **WHEN** deciding how to collapse a record
- **THEN** the role decision is made only from the structured `role` field
- **AND** the prefix / user-content / suffix split is made only from the
  structured sentinel marker sequence, never from pattern-matching the
  message prose

### Requirement: Three-Tier Progressive Disclosure

Every conversation turn in `#flow-view` MUST follow a progressive-disclosure
model so the default view is the clean, human-meaningful payload and the
surrounding process is reachable but never in the way. This is the deliberate
inversion of the prior behavior, where an assistant's thinking narrative + tool
markers were the default surface, the real result was buried in a raw JSON blob,
and an isolated "待回复" chip sat inline in the stream.

The two roles use **different disclosure depths**, by design — a deliberate
**mixed model**, not one unified tier count:

- The **`user` side keeps three layers**: Layer 1 the literal input, Layer 2 the
  full prompt behind "展开全部", and Layer 3 the raw NDJSON behind "查看原始" nested
  at the end of the Layer-2 area.
- The **`assistant` side uses two layers**: Layer 1 the narrative + the rendered
  structured result (both visible by default), and a single fold layer "查看原始"
  holding the turn's original record. There is NO assistant-side "展开全部"
  wrapper and no third assistant layer — the narrative is a JSON-stripped part of
  the visible first layer, so only the original record is folded.

The target is: **the default view shows the rendered result (plus, on the
assistant side, the JSON-stripped narrative tiled above it); the original record
is one click away behind "查看原始"; and, on the user side only, the full prompt
is behind "展开全部".**

The conversation is grouped into per-phase sections
(DISCOVERY / ANALYZE / PLAN / …) and, within each section, turns are presented
in chronological order as User / Assistant pairs (subject to the strict
chronological ordering of the *Conversation Strict Chronological Order*
requirement).

**User turn** — three layers:
1. **Layer 1 (default)** — only the user's literal input (the user-content
   section produced by the `prompt_markers.py` split; see *Role-Based Message
   Collapse*). No framework boilerplate leaks into this default view.
2. **Layer 2 ("展开全部")** — the complete prompt the LLM actually saw,
   presented as the labeled 模板前缀 / 框架后缀 subsections (the template prefix
   and the framework suffix). Folded by default.
3. **Layer 3 ("查看原始")** — the original NDJSON record for the message,
   reachable via the shared raw toggle (查看原始) **nested at the end of the
   Layer-2 "展开全部" expand area** (built by `makeUserPromptToggle`), NOT as a
   row-level always-visible button.

**Assistant turn** — two layers. The two-layer model below applies **only when
this turn produced a final result JSON**; a turn with no result JSON is handled
by the separate no-result rule stated after Layer 2. The precise, field-based
test for what counts as a *final result JSON* — as opposed to an intermediate
tool-call JSON that merely happens to parse — is defined in *Structured-JSON
Assistant Rendering* (the per-step result-field registry); the fold decision here
MUST use that same discriminator so the two requirements never diverge.
1. **Layer 1 (default, visible)** — this turn's narrative tiled above its
   rendered result. The narrative (text outside every JSON region, with inline
   `[Tool: …]` markers preserved and rendered via `renderToolMarkers`) is the
   human reasoning, already stripped of all JSON, so it does NOT duplicate the
   structured fields; below it sit the structured fields produced by the
   `STEP_ASSISTANT_RENDERERS` entry for the step type (e.g. discovery's
   `content` / `refined_description` / `questions`). No raw ```` ```json ````
   blob and no isolated pending chip appear in the default view.
2. **Layer 2 ("查看原始", the single fold)** — the turn's original record: the raw
   NDJSON / tool-call JSON / unrendered result-JSON literal, with the Layer-1
   narrative removed. It is folded by default but its toggle button is always
   visible, sitting directly below the rendered result; it is built by
   `makeAssistantRawToggle`. There is no "展开全部" wrapper and no third layer:
   because the narrative already lives in the visible Layer 1, only the original
   record is folded. The assistant raw entry prefers the raw_ndjson / raw_json
   payload and, when neither is present, falls back to the unrendered `content`
   literal so the original record is always reachable.

**Assistant turn with no result JSON** — when this turn produced no final
result JSON (its body is thinking process only — narrative plus tool calls,
including a turn whose only JSON content is intermediate tool calls), the
thinking process MUST stay shown inline as the default view via
`renderToolMarkers` and MUST NOT be folded or contracted into any empty toggle.
It never collapses to empty: because the process is already the visible default,
this case has no separate "查看原始" fold and the content is shown in full
directly.

When a turn has no Layer-1 payload to surface — e.g. a legacy `user` record
whose user-content section is empty, or an assistant turn whose body cannot be
parsed into structured fields — the renderer MUST degrade gracefully and never
drops content: a legacy empty `user` turn collapses to a single chip (with its
查看原始 raw toggle nested inside the chip's expand detail), while an assistant
turn with no parseable result keeps its thinking process shown inline via the
same `renderToolMarkers` + markdown path described in *Structured-JSON
Assistant Rendering* — shown in full, not folded. Expanding any disclosure
toggle follows the same `scrollIntoView({block: "nearest"})`-on-expand,
no-scroll-on-collapse behavior used elsewhere in the view.

#### Scenario: Assistant turn defaults to the rendered result with the narrative above it
- **GIVEN** an assistant turn whose body carries tool-call narrative plus a
  structured-JSON result for its step type
- **WHEN** the turn is rendered in `#flow-view`
- **THEN** the default (Layer 1) view shows the JSON-stripped narrative tiled
  above the rendered structured fields for that step type
- **AND** the raw NDJSON / tool-call JSON / unrendered result JSON literal are
  NOT in the default view — the turn's original record is reachable only by
  opening the single "查看原始" fold (its button is always visible, its body
  folded by default)
- **AND** there is no assistant-side "展开全部" wrapper and no third disclosure
  layer

#### Scenario: User turn defaults to the literal input only
- **GIVEN** a `user` turn whose stored body contains the three-segment marker
  sequence wrapping the user's literal input
- **WHEN** the turn is rendered
- **THEN** the default (Layer 1) view shows only the user's literal input,
  with no template prefix or framework suffix text
- **AND** the full prompt the LLM saw is reachable as the 模板前缀 / 框架后缀
  subsections behind the "展开全部" (Layer 2) toggle
- **AND** the original NDJSON is reachable via the "查看原始" (Layer 3) toggle
  nested at the end of that expanded "展开全部" area, never via a row-level
  always-visible button

#### Scenario: No isolated pending chip embedded in the assistant default view
- **WHEN** an assistant turn is rendered in its default Layer-1 form
- **THEN** no inline "待回复" / pending-intervention chip is rendered inside the
  turn's clean result view
- **AND** pending interventions appear only on the docked reply bar, per the
  *Unified Intervention Items* requirement

#### Scenario: Turn with no Layer-1 payload degrades without losing content
- **GIVEN** an assistant turn whose body cannot be parsed into structured
  fields, or a legacy `user` turn with an empty user-content section
- **WHEN** the turn is rendered
- **THEN** the renderer falls back without raising — an assistant turn with no
  parseable result shows its thinking process inline via `renderToolMarkers`
  (in full, not folded), while a legacy empty `user` turn collapses to a
  single chip — and the full content remains reachable through the disclosure
  layers
- **AND** no message text is dropped

#### Scenario: Assistant turn with no result JSON keeps its thinking inline
- **GIVEN** an assistant turn whose body is thinking process only — narrative
  and tool calls (including a turn whose only JSON content is intermediate
  tool calls) — with no final result JSON
- **WHEN** the turn is rendered in `#flow-view`
- **THEN** the thinking process is shown inline as the default view via
  `renderToolMarkers`, in full
- **AND** it is NOT folded or contracted into any empty toggle, and it never
  collapses to empty

#### Scenario: User Layer 3 raw toggle is nested inside the Layer 2 expansion
- **GIVEN** a marker-split `user` turn rendered with its default Layer-1
  literal-input bubble
- **WHEN** the conversation row is first rendered, before any toggle is opened
- **THEN** no row-level always-visible "查看原始" button is present beside the
  bubble
- **AND** the "查看原始" (Layer 3) toggle becomes reachable only after the
  "展开全部" (Layer 2) toggle is expanded, nested at the end of that expand area

#### Scenario: Assistant single 查看原始 fold is the only assistant disclosure
- **GIVEN** a result-JSON assistant turn rendered with its default Layer-1
  narrative + structured result
- **WHEN** the conversation row is rendered
- **THEN** the only assistant-side fold is the single "查看原始" entry below the
  rendered result — its toggle button is always visible and its body is folded
  by default
- **AND** there is no assistant "展开全部" wrapper, and the "查看原始" fold is NOT
  nested inside any "展开全部" area
- **AND** when neither raw_ndjson nor raw_json is present, the "查看原始" fold
  falls back to the unrendered `content` literal so the original record stays
  reachable

### Requirement: Structured-JSON Assistant Rendering

For step types whose `assistant` messages are structured-JSON responses (the
LLM emits a JSON object — optionally wrapped in a ```` ```json ... ``` ````
fenced block and/or preceded by free-form narrative — rather than free
markdown), the running-flow conversation MUST parse the JSON and render its
fields as structured UI elements. The raw JSON literal MUST NOT be the
primary visible surface of an assistant message: dumping the JSON blob as a
markdown code block under the assistant bubble — as the previous renderer
did — is forbidden, because field values such as `content`,
`refined_description`, and `questions` get buried inside JSON syntax and the
user has to mentally parse the structure.

The frontend MUST maintain a small registry (e.g. `STEP_ASSISTANT_RENDERERS`)
that maps a `step_type` to a structured renderer function, mirroring the
existing per-step report-card renderer pattern. `renderConversationRecord`
dispatches assistant records to `STEP_ASSISTANT_RENDERERS[step_type]` when
one is registered; when no entry matches, or when the registered renderer
fails to parse the message body, the renderer MUST fall back gracefully to
the existing `renderToolMarkers` + markdown / foldable path, so no assistant
message is ever lost.

The registry MUST cover **every** step type whose `assistant` message is a
structured-JSON response — `discovery`, `analyze`, `plan`, `plan_tasks`,
`implement`, `test`, `self_check`, `verify_spec`, `update_spec`, `commit`,
`version_analyze`, and `summarize` — not `discovery` alone. To keep web and
CLI field parity without re-implementing each layout, every non-discovery
renderer SHOULD reuse the field-rendering logic already provided by the
matching `STEP_REPORT_RENDERERS` entry (see *Per-Step Report Cards*), so an
assistant turn surfaces the same structured fields the report card and the CLI
Panel show. `discovery` keeps a dedicated renderer mirroring the CLI's
`steps/discovery.py::_display_discovery_message` /
`_extract_narrative_from_raw` behavior so web and CLI users see the same
report; it is the worked example below:

1. Extract any narrative text outside JSON — both fenced ```` ```json ... ````
   blocks and any trailing bare JSON object after the last narrative line are
   stripped (matching the backend's `parse_json_response` lenient repair
   chain). Tool-use markers (e.g. `[Read: ...]`) embedded in the narrative
   stay intact and are rendered via the same `renderToolMarkers` helper used
   elsewhere; the resulting narrative text is rendered as markdown at the top
   of the bubble.
2. Parse the JSON object. If parsing succeeds, render the fields in this
   order, skipping any that are absent or empty:
   - `content` — rendered as a markdown block.
   - `refined_description` — rendered as an independently styled
     **Proposed Task Description** card (mirroring the CLI's cyan reverse
     block; on the web side this MAY reuse the existing step-report card
     style or use a distinct accent color, as long as it is visually
     separated from the `content` markdown).
   - `questions` — rendered as an ordered list.
3. Always preserve the **view raw** affordance: the original assistant body
   (including the JSON literal and NDJSON envelope) MUST remain reachable via
   the assistant's single "查看原始" fold (built by `makeAssistantRawToggle`; see
   *Three-Tier Progressive Disclosure*) sitting directly below the rendered
   result rather than shown as a row-level always-visible button, so a developer
   can still inspect the unrendered string when debugging. This assistant fold
   is its own dedicated entry — it falls back to the unrendered `content`
   literal when no raw_ndjson / raw_json payload is present — and is NOT nested
   inside any "展开全部" wrapper (there is no assistant-side "展开全部").

The registry MUST remain open for future step types without re-architecting
the dispatch path. Every step type listed above MUST have a registered
renderer so an assistant turn defaults to structured fields rather than a raw
```` ```json ```` blob; a step type with no registered entry — or a registered
renderer that cannot parse the body — degrades gracefully through the shared
`renderToolMarkers` + markdown / foldable fallback path, and no assistant
message is ever dropped.

**Result-vs-tool-call identification.** A structured renderer MUST surface a
Layer-1 result (and thereby let *Three-Tier Progressive Disclosure* render the
narrative + structured result as the visible default, with only the turn's
original record folded behind the single "查看原始" entry) **only when the turn
actually produced a final result JSON** — not merely because some region of the
body parsed as JSON.
The discriminator is **field-based**: the frontend maintains a per-step
result-field registry (e.g. `STEP_RESULT_FIELDS`) enumerating the keys that
belong to each step type's result set — the same fields the matching
`STEP_REPORT_RENDERERS` entry / CLI Panel reads from `step.outputs` (for
`discovery`: `content` / `refined_description` / `questions`). A parsed JSON
object counts as a final result **iff it carries at least one of its step type's
result fields with a non-null value** — presence, not non-emptiness, so a genuine
but empty result such as `{"committed": false}` or `{"actionable_count": 0}`
still counts. An intermediate tool-call JSON (Bash / Edit / Grep / … arguments)
carries none of those keys and is therefore NOT a result.

Because a single turn may interleave several JSON regions, the renderer MUST
collect **every** parseable JSON region — each fenced ```` ```json ... ````
block in document order plus a trailing bare object/array that follows the last
fence — and select the **last** region satisfying the result predicate as the
turn's result (a result conventionally follows the tool calls that produced it).
The Layer-1 narrative is then the body with **all** JSON regions removed (not
just the chosen one), so intermediate tool-call JSON never leaks into the clean
default view while the full original body — every JSON region included — stays
reachable through the assistant's single "查看原始" fold. When **no** region
satisfies the result predicate — including a turn carrying two or more tool-call
JSON segments — the renderer MUST return no result so the caller keeps the
thinking process inline per *Three-Tier Progressive Disclosure*, never folding it
into any empty toggle.

#### Scenario: Tool-call-only turn is not mistaken for a final result
- **GIVEN** an assistant turn whose body carries one or more JSON regions that
  are all tool-call arguments (e.g. two ```` ```json ... ```` blocks of
  Bash / Edit arguments) and no region carrying any of the step type's result
  fields
- **WHEN** the structured renderer evaluates the turn
- **THEN** no region satisfies the per-step result-field predicate, so the
  renderer surfaces no structured Layer-1 result
- **AND** the caller keeps the full thinking process inline via
  `renderToolMarkers`, never folding it into any empty toggle

#### Scenario: Final result region wins among interleaved tool-call JSON
- **GIVEN** an assistant turn that emits one or more tool-call JSON regions
  followed by a final result JSON carrying at least one of the step type's
  result fields with a non-null value
- **WHEN** the structured renderer evaluates the turn
- **THEN** the last region satisfying the result predicate is chosen as the
  turn's result and rendered as the Layer-1 structured fields
- **AND** the Layer-1 narrative has every JSON region (the tool calls and the
  result literal) removed, while the unmodified body remains reachable behind
  the assistant's single "查看原始" fold

#### Scenario: Discovery assistant message renders structured fields
- **GIVEN** an assistant record with `step_type = "discovery"` whose body
  contains optional narrative text followed by a fenced ```` ```json ... ````
  block carrying `{"content": "<markdown>", "refined_description": "<task>",
  "questions": ["q1", "q2"]}`
- **WHEN** the record is rendered in the running-flow conversation
- **THEN** the bubble renders, in order:
  (1) the narrative text as markdown (with tool-use markers preserved),
  (2) the `content` field as a markdown block,
  (3) a Proposed Task Description card carrying the `refined_description`
      value rendered as markdown,
  (4) an ordered list of the `questions` strings
- **AND** the raw JSON literal does NOT appear as a primary markdown code
  block under the bubble

#### Scenario: Every structured step type has a registered assistant renderer
- **GIVEN** the `STEP_ASSISTANT_RENDERERS` registry after the frontend module
  loads
- **WHEN** the registry is inspected for the structured step types
  (`discovery`, `analyze`, `plan`, `plan_tasks`, `implement`, `test`,
  `self_check`, `verify_spec`, `update_spec`, `commit`, `version_analyze`,
  `summarize`)
- **THEN** each of those step types maps to a renderer function, so an
  assistant turn of any of them defaults to its structured fields rather than a
  raw ```` ```json ```` blob
- **AND** the non-discovery renderers surface the same field set as the
  matching `STEP_REPORT_RENDERERS` entry, keeping web and CLI field parity

#### Scenario: JSON parse failure falls back to existing renderer
- **GIVEN** an assistant record with `step_type = "discovery"` whose body
  cannot be parsed as a JSON object even after the lenient repair chain
  (e.g. completely free-form prose, or a malformed JSON fragment)
- **WHEN** the record is rendered
- **THEN** the discovery structured renderer catches the parse failure and
  falls back to the existing `renderToolMarkers` + markdown / foldable
  rendering path
- **AND** the assistant text is still visible in the bubble; no exception
  surfaces to the user

#### Scenario: Raw assistant body remains accessible via view-raw toggle
- **GIVEN** any assistant record rendered through a structured-JSON renderer
- **WHEN** the user opens the per-record `view raw` toggle
- **THEN** the original unrendered assistant body (including the JSON
  literal and the underlying NDJSON envelope) is shown unchanged

### Requirement: Authoritative Step-Type Sourcing

The running-flow conversation's per-record `stepType` — the value that drives
assistant-bubble dispatch (`renderAssistantBubble` /
`STEP_ASSISTANT_RENDERERS[stepType]`), the per-phase step headers / labels
(`stepKey` / `stepHeaderLabel`), and the per-step report cards — MUST be taken
from an **authoritative, daemon-injected envelope field**, NOT guessed from the
inner message body. Real daemon chat records are envelopes of the form
`{step_id, step_type, message}` where `message` is `{role, content}` and carries
**no** `step_type` of its own; the `step_type` is parsed deterministically by the
daemon from the per-step jsonl file-name convention `NN_<step_type>_<hash>(_Gk)`
(see the `history.py` bullet of the `base` spec's *Daemon Modules* requirement).

`normalizeRecord` MUST source `stepType` with the priority **envelope
`rec.step_type` > inner `message.step_type` > empty string**, applied
identically to both the chat-record branch and the step-event branch. The
envelope value is authoritative because it is recovered from the file-name (it
can never be missing or dirtied by an `_Gk` group suffix); the inner-message
fallback preserves compatibility with older daemons that did not yet inject the
envelope field; and the empty-string default keeps the renderer safe when
neither is present.

When `stepType` is empty, the renderer MUST degrade gracefully — the
structured-JSON dispatch falls back to the shared `renderToolMarkers` + markdown
path (per *Structured-JSON Assistant Rendering*) and the step header falls back
to a best-effort label — and MUST NOT raise.

#### Scenario: Envelope step_type drives dispatch on real daemon records
- **GIVEN** a real daemon record `{"step_id": "01_discovery_975607bb",
  "step_type": "discovery", "message": {"role": "assistant", "content": ...}}`
  whose inner `message` carries no `step_type`
- **WHEN** `normalizeRecord` processes the record
- **THEN** `norm.stepType` is `"discovery"` (taken from the envelope, not the
  inner message)
- **AND** the assistant bubble dispatches to `STEP_ASSISTANT_RENDERERS["discovery"]`
  so the structured result fields render rather than the raw process being dumped

#### Scenario: Step header label uses the step type, not the file-name stem
- **GIVEN** a record whose `step_id` is the jsonl stem `01_discovery_975607bb`
  and whose injected envelope `step_type` is `discovery`
- **WHEN** the conversation builds the step header for that record
- **THEN** the header label reflects the step type (e.g. `DISCOVERY`), not the
  raw `01_discovery_975607bb` stem

#### Scenario: Inner-message fallback for legacy daemons
- **GIVEN** a record from an older daemon that has no envelope `step_type` but
  whose inner `message` happens to carry a `step_type` field
- **WHEN** `normalizeRecord` processes the record
- **THEN** `norm.stepType` falls back to the inner `message.step_type`

#### Scenario: Missing step type degrades without raising
- **WHEN** a record carries neither an envelope `step_type` nor an inner
  `message.step_type`
- **THEN** `norm.stepType` is the empty string
- **AND** the assistant bubble degrades to the shared `renderToolMarkers` +
  markdown fallback and no exception is raised

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

Crucially, the orchestrator MUST emit a terminal `step_completed` /
`step_failed` event for **every** step type, including the interactive
DISCOVERY and CONFIRM steps, PLAN, and `summarize` — step types whose CLI
output is owned by the orchestrator's interactive/special paths and which
previously emitted no terminal event at all. Without that event, a step that
had already finished left the web console with no final card to render (the
exact symptom this requirement closes: a completed step showing no
default-expanded output card). The emit is gated on a **terminal result**:
only `COMPLETED`, `PARTIAL`, and `FAILED` produce the event. A step that
returns `PAUSED` (DISCOVERY awaiting user input, CONFIRM awaiting approval) or
`REVISION_NEEDED` has not finished and its terminal event is deferred until a
later re-run reaches a terminal status. To keep CLI output byte-identical
despite these new events, `CliSink` skips rendering the terminal events of the
interactive/special step types (CONFIRM, DISCOVERY, PLAN) — their CLI output
is already presented by the orchestrator's interactive paths, so re-rendering
them through `render_step_output` would double the CLI output. `HistorySink`
and `JsonSink` still receive every terminal event, so the web report card
(and the daemon NDJSON stream) appear for these steps even though the CLI does
not re-render them.

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

#### Scenario: Interactive and summarize steps produce a report card once finished
- **GIVEN** a flow whose DISCOVERY, PLAN, or `summarize` step has reached a
  terminal status (`COMPLETED` / `PARTIAL` / `FAILED`)
- **WHEN** the conversation is rendered in `#flow-view`
- **THEN** the step's terminal event has been persisted to its per-step jsonl
  (because the orchestrator now emits a terminal event for every step type),
  and a default-expanded `.step-report` card is rendered for it
- **AND** the card appears even though `CliSink` skipped re-rendering that step
  on the CLI side, so a finished interactive step is no longer left without a
  final output card on the web console

#### Scenario: A paused or revision-pending step has no premature report card
- **GIVEN** a DISCOVERY step that returned `PAUSED` awaiting user input, or a
  CONFIRM step that returned `REVISION_NEEDED`
- **WHEN** the conversation is rendered
- **THEN** no terminal `step_completed` / `step_failed` event has been emitted
  for that not-yet-finished step, so no `.step-report` card is rendered for it
- **AND** a card appears only after a later re-run drives the step to a
  terminal status

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
