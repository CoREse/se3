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

Every point in a running flow that needs a human in the loop — a pending MCP
call, a Ctrl-C mid-flow interjection, a retry/failure decision, or a CLI
subprocess confirmation prompt — is surfaced inside this one chat window as a
distinct, default-expanded, visually prominent intervention item, never blended
into ordinary conversation bubbles. The view is implemented in `app.js` and
shares the conversation rendering engine (`normalizeRecord`, role-based
collapse, Markdown rendering) with the history view. The underlying transport —
`kind`-tagged call files under `se3/calls/` aggregated into `pending_calls` —
is owned by `interaction_calls.py`, the daemon aggregator, and the
`se3.daemon.protocol` module (see the `base` spec's *Daemon Modules* /
*Server Modules* requirements).

## Requirements

### Requirement: Full-Screen Chat Layout for Running Flows

A running flow MUST open in a full-screen `#flow-view` modeled on the history
view, NOT in a narrow side drawer. The conversation is the scrollable main
body; Overview / Steps / machine and other auxiliary information are collected
into a sidebar so they do not compete with the conversation for space. The
standalone, context-free call-modal popup MUST NOT be used; `#flow-view` is the
single interaction surface for a running flow.

#### Scenario: Running flow opens in the full-screen view
- **WHEN** a user opens a running flow from the web console
- **THEN** the flow is shown in the full-screen `#flow-view`
- **AND** the conversation occupies the scrollable main body
- **AND** Overview / Steps / machine info are placed in the sidebar

#### Scenario: No context-free call modal
- **WHEN** a running flow has a pending interaction
- **THEN** the interaction is presented inside `#flow-view`
- **AND** no separate "Respond to pending call" modal popup is shown

### Requirement: Docked Persistent Reply Box

The bottom of `#flow-view` MUST host a persistently docked reply input box, so
a user can respond like a chat application without first clicking a button or
opening a popup. The reply box reflects whether there is an interaction to
respond to.

#### Scenario: Reply box is always present
- **WHEN** `#flow-view` is shown for any running flow
- **THEN** a reply input box is docked at the bottom of the view
- **AND** it is visible without the user clicking a button or opening a popup

#### Scenario: Reply box disabled with no pending interaction
- **WHEN** the running flow has no pending interaction
- **THEN** the reply input and its submit control are disabled
- **AND** the box shows an explanatory placeholder (e.g. "No pending interaction…")

#### Scenario: Reply box activated with a pending interaction
- **WHEN** the running flow has at least one pending interaction
- **THEN** the reply input and submit control are enabled
- **AND** the reply box clearly states the context it is replying to — what is
  being answered and which intervention it targets

#### Scenario: Submitted reply is inlined into the conversation
- **WHEN** the user submits a reply for a pending interaction
- **THEN** the reply is folded into the conversation flow in place

### Requirement: Unified Intervention Items

All human-in-the-loop interactions inside a running flow MUST be presented in
the one chat window as independent intervention items — never mixed into
ordinary conversation messages. Each intervention item is rendered default-
expanded and visually prominent. The recognized intervention kinds are at
least: (1) a pending MCP call (`call`); (2) a post-Ctrl-C mid-flow interjection
(`interjection`); (3) a retry/failure decision (`retry_decision`); (4) a CLI
subprocess confirmation prompt (`cli_confirm`). Each item is derived from a
`pending_calls` entry whose `kind` field identifies the interaction; an
unrecognized `kind` degrades to a plain `call`.

#### Scenario: Each intervention kind is rendered as a distinct item
- **WHEN** a running flow has a pending interaction of kind `call`,
  `interjection`, `retry_decision`, or `cli_confirm`
- **THEN** it is rendered as an independent, default-expanded, visually
  prominent intervention item
- **AND** the item is not blended into an ordinary conversation bubble

#### Scenario: Unknown kind degrades to a plain call
- **WHEN** a `pending_calls` entry carries a `kind` value that is not one of
  the recognized kinds
- **THEN** it is treated and rendered as a plain `call` intervention

#### Scenario: Reply box targets a single intervention
- **WHEN** multiple intervention items are present
- **THEN** the docked reply box targets exactly one intervention at a time
- **AND** the targeted intervention's context is shown in the reply box

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
