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

### Requirement: Docked Persistent Reply Box

The bottom of `#flow-view`'s main column MUST host a persistently docked
reply area, so a user can respond like a chat application without first
clicking a button or opening a popup. The reply area is composed of three
stacked parts, all sitting below the conversation:

1. **Pending intervention chip bar** — a row of status-style buttons, one
   per pending intervention (pending MCP call, retry decision, CLI confirm,
   etc.) plus an "Interject" chip for the active flow. Each chip shows the
   intervention kind and a short identifier.
2. **Reply context panel** — when a chip is selected, the panel above the
   textarea expands the selected intervention's full prompt (Markdown
   rendered), optional context block (no `max-height` truncation), and any
   `options` action buttons. When no chip is selected the panel is empty or
   shows a brief "no pending interaction" hint.
3. **Reply textarea + send button** — the single input row, reused for every
   intervention kind.

The textarea and send button are **disabled by default** and are **enabled
only while there is a pending interaction the user has targeted**. This
mirrors the CLI's behavior where the user can only type when stdin is being
read.

#### Scenario: Reply box is always present
- **WHEN** `#flow-view` is shown for any running flow
- **THEN** the docked reply area (chip bar + context panel + textarea + send
  button) is visible at the bottom of the main column
- **AND** it is visible without the user clicking a button or opening a popup

#### Scenario: Reply box disabled with no pending interaction
- **WHEN** the running flow has no pending interaction (chip bar is empty)
- **THEN** the reply textarea and submit control are disabled
- **AND** the box shows an explanatory placeholder (e.g. "No pending
  interaction…")

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
degrades to a plain `call` chip. An active flow additionally surfaces a
synthetic `interjection` chip so the user can always inject an instruction
mid-flow.

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
whose `context.flow_id` equals the snapshot's `flow_id`. Pending calls with a
**missing or empty** `context.flow_id` are treated as belonging to the current
flow (legacy unattributed calls, default-attributed to the only flow on that
root). The frontend `pendingCalls(flow)` helper applies the same filter as a
defensive fallback against older daemon versions that have not yet been
upgraded.

Machine-wide aggregation (`MachineStatus.pending_calls`) is **not** filtered;
the machine-level view still enumerates every pending call on the host.

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

#### Scenario: Calls without flow_id default to the current flow
- **GIVEN** the current running flow has `flow_id = "F1"`
- **WHEN** a pending call's `context.flow_id` is missing, `null`, or an
  empty string
- **THEN** the call is treated as belonging to the current flow and appears
  as a chip in `F1`'s chip bar
- **AND** this preserves backward compatibility with call files written
  before flow_id attribution was introduced

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
