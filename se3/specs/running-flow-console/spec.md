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
and the human-intervention items (both default-expanded), while template-style
prompt messages (`user` / `system` roles) default to a single collapsed,
clickable chip (e.g. "system prompt · discovery mode"). Clicking a chip expands
the original content; content is never deleted, only collapsed by default.
Classification MUST be deterministic, based solely on the record's structured
`role` field — `user` / `assistant` / `system`, with `human` folded into
`user` — and MUST NOT rely on guessing from message text.

#### Scenario: Assistant output defaults to expanded
- **WHEN** a conversation record has the `assistant` role
- **THEN** it is rendered expanded, highlighting the assistant's real output

#### Scenario: Template-style messages default to a collapsed chip
- **WHEN** a conversation record has the `user` or `system` role
- **THEN** it is rendered as a single collapsed, clickable chip
- **AND** clicking the chip expands the original content unchanged

#### Scenario: human role folded into user
- **WHEN** a record's `role` field is `human`
- **THEN** it is classified as `user` and collapses to a chip like any other
  `user` message

#### Scenario: Classification is role-based, not text-based
- **WHEN** deciding whether a record collapses
- **THEN** the decision is made only from the structured `role` field
- **AND** never from pattern-matching the message text
