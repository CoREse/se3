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

### Requirement: Direct Resume Entry in the Flow Sidebar

When a flow opened in `#flow-view` is in a directly-resumable state, the view
MUST present a **Resume** control in the flow's detail sidebar so the operator
can continue the run without leaving the console. Resumability is decided by the
same pure `isFlowResumable(flow)` predicate used by the history surface (see the
`history-view-console` *Direct Resume Entry* requirement and the `base` spec's
*Server Modules* requirement): the Resume control appears **only** for a flow
whose status is `FAILED` or `PAUSED` and that is not archived/history-only, and
it is absent for `RUNNING` / `INIT` / `RECOVERING` / `COMPLETED` flows. The
control reuses the shared `makeResumeButton(flow)` helper, so when the flow is
not resumable the helper returns nothing and the sidebar renders no Resume
control at all.

Activating it POSTs `POST /api/flows/{flow_id}/resume` (the same debounced,
owner-validated path the history surface uses), which dispatches a
`MSG_SPAWN_FLOW` carrying `resume_flow_id` to the owning daemon; it never takes
the archived `se3 history restore` rollback path. This Resume control is
independent of the docked reply box and its intervention chips — it continues a
stalled flow rather than answering a pending interaction.

#### Scenario: Sidebar shows Resume only for a FAILED or PAUSED resumable flow
- **GIVEN** a flow is open in `#flow-view`
- **WHEN** the detail sidebar is rendered
- **THEN** a Resume control appears only when `isFlowResumable(flow)` is true
  (status `FAILED` or `PAUSED`, not archived/history-only)
- **AND** no Resume control is rendered for `RUNNING`, `INIT`, `RECOVERING`, or
  `COMPLETED` flows

#### Scenario: Activating the sidebar Resume dispatches a resume request
- **GIVEN** the Resume control is visible in the flow sidebar
- **WHEN** the user activates it
- **THEN** the frontend POSTs `/api/flows/{flow_id}/resume`, debounced per flow
  via `state.resumeFlowRequests`
- **AND** the request only ever resumes the live flow, never restoring an
  archived snapshot

### Requirement: Docked Persistent Reply Box

The bottom of `#flow-view`'s main column MUST host a persistently docked
reply area, so a user can respond like a chat application without first
clicking a button or opening a popup. The reply area is composed of three
stacked parts, all sitting below the conversation:

1. **Pending intervention chip bar** — a row of status-style buttons, one
   per pending intervention (e.g. a generic pending reply, a retry
   decision, a CLI subprocess confirmation). Each chip shows the
   intervention kind via a **user-facing neutral label** (e.g. "Pending reply" /
   "Needs decision" / "Needs confirmation") and MUST NOT expose the internal transport
   vocabulary — strings such as `MCP`, `call_id`, or `call <id>` MUST NOT
   appear as visible chip text. The underlying call identifier MAY be
   preserved on a hidden `data-call-id` attribute and inside the chip's
   `title` hover tooltip for developer debugging only.
2. **Reply context panel** — when a chip is selected, the panel above the
   textarea renders, for **every** intervention kind, only the intervention's
   kind header, the intervention's prompt (Markdown rendered, mounted behind a
   **default-collapsed expand/collapse trigger** whose expanded body is
   height-bounded and scrollable — see the *Collapsible Reply-Context Prompt
   Body* requirement), and any `options` action buttons. It MUST NOT render a
   separate `context` block — the panel never duplicates context text that the
   prompt already carries. When no chip is selected the panel is empty or shows
   a brief "no pending interaction" hint.
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
time — like an ordinary chat application; the textarea MUST NOT be disabled
even while a submission is in flight (the user must remain free to keep
drafting / correcting). The **send button** is the gate: it is disabled
while no target chip is selected (the draft has no destination) and enabled
the instant any chip (real or synthetic) becomes the selected target. While
a submission is in flight, only the **send button** is disabled — and it
remains disabled until the **ws-pushed `flowDetail` snapshot has reflected
the submission's effect** (e.g. the targeted `pending_calls` entry has
flipped to consumed / disappeared, or an `interjection_event` lifecycle
event for the just-sent submission has arrived). The send button MUST NOT
re-enable on the local `fetch` `finally` alone, because that would unlock
the button before the server / daemon has acknowledged the submission and
allow a stale second click against the same target. To survive ws jitter,
a bounded fallback timeout (currently 8 seconds) MUST also re-enable the
send button if no ws acknowledgement arrives in time. Client-side dedup
of interjection submissions is explicitly NOT used as a substitute for
this gate — the gate is the source of truth for "submission settled".

#### Scenario: Send is disabled while waiting for ws settle, textarea stays editable
- **GIVEN** the user submits a reply (a call response or an interjection)
  through the docked send button
- **WHEN** the local `fetch` returns (success or HTTP-level failure) but the
  daemon has not yet pushed a new `flowDetail` snapshot reflecting the
  submission
- **THEN** the **send button stays disabled**
- **AND** the **textarea remains editable** (the user can keep typing / correcting)
- **AND** the send button re-enables only once the ws snapshot diff shows
  the targeted `pending_calls` entry consumed / removed, or the matching
  `interjection_event` lifecycle event arrives
- **AND** if neither signal arrives within the 8-second fallback window the
  send button re-enables anyway, so a transient ws outage cannot deadlock
  the reply UI

#### Scenario: No client-side dedup of interjection submissions
- **WHEN** the user opts into interjection and submits more than one
  interjection in quick succession against the same running flow
- **THEN** the client MUST NOT filter / merge / suppress duplicate
  interjection submissions on its own — each successful Send corresponds to
  exactly one POST and one daemon call file
- **AND** the protection against double-submit against the **same** target
  comes from the send-button settle gate above, not from a client-side dedup
  cache

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
- **THEN** the reply context panel above the textarea shows the
  intervention's kind header, its Markdown-rendered prompt behind a
  default-collapsed expand/collapse trigger (see *Collapsible Reply-Context
  Prompt Body*), and any `options` action buttons (no separate context block)
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
  intervention's kind (e.g. "Pending reply" / "Interject" / "Needs decision" / "Needs confirmation")
- **AND** neither the chip label, the chip's visible secondary text, nor
  the reply context panel header contain the literal substrings `MCP`,
  `call_id`, or the pattern `call <id>` as visible text
- **AND** the underlying call identifier, if preserved, appears only on
  hidden DOM attributes (e.g. `data-call-id`) or hover `title` tooltips —
  never in the rendered text content

### Requirement: Optimistic Reply Echo Reconciliation

To satisfy *Submitted reply is inlined into the conversation* with instant
feedback, submitting a reply (`sendReply` → `appendLocalReply` in `app.js`)
optimistically splices a local **echo** of the user's text into
`state.flowConversationRecords` immediately, before the daemon has persisted and
pushed back its own **authoritative** `user` record. The echo and the daemon's
record carry different `step_id` / `timestamp`, so they hash to different
`recordKey` values and `mergeSnapshotWithLiveAppends`' identity-based dedup
cannot pair them. Without a reconciliation pass the echo and the authoritative
record therefore BOTH render, and the desktop user reply is shown twice (the
observed duplicate-reply defect). The view MUST reconcile the two so a submitted
reply is shown **exactly once**, while preserving the no-loss / no-reorder
guarantees of *Conversation Strict Chronological Order*.

Reconciliation is content-based and rank-stable:

1. **Echo tagging** — each optimistic record `appendLocalReply` produces MUST be
   tagged (`__localEcho`) and MUST retain the original literal reply text
   (`__localEchoText`) so a later pass can match it by content even after the
   daemon wraps the same reply in the prompt-marker envelope (e.g. a discovery
   continuation). Comparison is performed on a **marker-stripped, trimmed**
   normalization (`comparableUserText`, which runs `splitUserPromptByMarker` and
   falls back to the trimmed raw text) so an echo equals its authoritative copy
   regardless of envelope.
2. **Stable per-text rank** — at creation each echo MUST record its rank among
   all copies of the same comparable text — the count of prior copies that
   already exist, counting BOTH authoritative `user` records AND still-pending
   echoes (`__localEchoPriorAuth`). This gives every echo of an identical reply
   (repeated "yes" / "continue", repeated interjections sent before any daemon
   record returns) a distinct rank `0, 1, 2, …`.
3. **Reconcile pass** — `reconcileLocalEchoes(records)` runs after every merge
   that could introduce an authoritative copy: after the snapshot merge in
   `loadFlowConversation` (over `mergeSnapshotWithLiveAppends`' output) and after
   the live-append merge in `applyHistoryData`. For each comparable text it
   counts authoritative (non-echo) `user` records (`auth`) and removes exactly
   those echoes whose `rank < auth` — i.e. the `auth` earliest pending echoes
   for that text. An echo is therefore removed ONLY once THIS reply's own
   authoritative copy has landed, never when a reconcile pass merely finds an
   earlier already-recorded duplicate, and a single daemon arrival never sweeps
   away more than one pending echo. The authoritative record is never removed, so
   it keeps its real timestamp and lands in its correct chronological slot.

The pass MUST be render-safe: when it removes a mid-list echo (which shifts
array indices), `applyHistoryData` MUST force a full conversation rebuild rather
than the cheap incremental tail-append render, because the append path only
re-reads the tail and would otherwise corrupt the DOM. When nothing is removed,
`reconcileLocalEchoes` MUST return the same array reference so the caller can
keep the incremental render path. `reconcileLocalEchoes` and `comparableUserText`
are exported for DOM-stub tests. This mechanism is independent of, and does not
disturb, the mobile tool-marker layout fix.

Success/failure of a submitted reply MUST be decided **solely** by `resp.ok`,
and that decision MUST be decoupled from the success-path optimistic echo
rendering. `respond_flow` forwards the reply to the daemon and returns `200`
immediately without waiting for the flow to ack, so once `resp.ok` is true the
reply has been received and acted on — the frontend MUST treat it as a success.
`sendReply` MUST therefore clear the input and show the success toast (e.g.
`Response sent.`) on the `resp.ok` branch **before** invoking `appendLocalReply`,
and the outer `catch` MUST handle only a genuine `fetch` rejection (the
network-error toast) — never an exception raised by rendering a reply the backend
already received. To uphold this, `appendLocalReply` MUST be best-effort: it
MUST first `concat` the `__localEcho` record into `state.flowConversationRecords`
(state is the source of truth, so the echo is recorded even if rendering fails),
then run its rendering chain (the per-text rank computation, `renderConversation`,
`refreshFlowStickyHeader`, `updateFlowUsageBadge`, `scrollFlowConversationToBottom`)
inside a local `try/catch` that logs (`console.error`) and swallows any exception
rather than rethrowing — so a render fault is observable but never re-classified
as a network failure and never reverts an already-acknowledged reply to a failed
state. This prevents the regression (issue #193) where a successfully delivered
`discovery_confirm` confirmation (the "输入 1" / *Confirm and continue* action) —
and any other reply on the shared `sendReply` path — falsely surfaced
`Could not send — network error` and skipped its conversation echo. A real
non-2xx response still takes the `Could not send: …` failure branch and appends
no echo.

#### Scenario: Optimistic echo is replaced by the authoritative record, shown once
- **GIVEN** the user submits a reply and `appendLocalReply` splices an optimistic
  `__localEcho` user record into the conversation for instant feedback
- **WHEN** the daemon later pushes the authoritative `user` record for the same
  reply (via a live `history_data` append or a refetched snapshot), which carries
  a different `step_id` / `timestamp` and therefore a different `recordKey`
- **THEN** `reconcileLocalEchoes` removes the matching optimistic echo and keeps
  the authoritative record, so the reply is rendered exactly once
- **AND** the surviving authoritative record stays in its correct
  timestamp-ordered slot (no reorder) and the reply is never dropped (no loss)

#### Scenario: Echo persists until its own authoritative copy arrives
- **GIVEN** the user sends the same reply text more than once (e.g. two
  successive "continue" replies), so multiple `__localEcho` records with distinct
  ranks (`__localEchoPriorAuth` = 0, 1, …) are pending for the same comparable
  text
- **WHEN** a single authoritative `user` record for that text arrives
- **THEN** `reconcileLocalEchoes` removes exactly one echo — the one whose rank
  is now below the authoritative count — and leaves the other still-pending
  echo(es) visible until their own authoritative copies arrive
- **AND** no pending echo flickers out merely because an earlier duplicate reply
  was already recorded

#### Scenario: Marker-wrapped daemon reply still matches the plain echo
- **GIVEN** an optimistic echo holding the user's literal reply text and a daemon
  authoritative record whose body wraps that same reply in the prompt-marker
  envelope (`TEMPLATE_PREFIX_END` / `USER_CONTENT_BEGIN` / `USER_CONTENT_END`)
- **WHEN** `reconcileLocalEchoes` compares them via `comparableUserText`
- **THEN** the marker-stripped, trimmed forms compare equal and the echo is
  reconciled away, so the wrapped daemon reply is shown once

#### Scenario: Mid-list echo removal forces a full rebuild
- **WHEN** `reconcileLocalEchoes` removes an echo that is not at the tail of the
  conversation array during an `applyHistoryData` live append
- **THEN** the conversation is re-rendered with a full rebuild rather than the
  incremental tail-append path, so index shifts do not corrupt the DOM
- **AND** when no echo is removed, `reconcileLocalEchoes` returns the same array
  reference and the incremental-append render path is preserved

#### Scenario: Backend-accepted reply with failing echo render is not misreported as a network error
- **GIVEN** the user confirms a `discovery_confirm` pending item via the *Confirm
  and continue* ("输入 1") action — or submits any other reply on the shared
  `sendReply` path — and the backend `respond_flow` accepts it (`resp.ok`, HTTP
  `2xx`) and the flow advances
- **WHEN** the success-path optimistic echo rendering throws — the per-text rank
  computation or any render helper (`renderConversation` /
  `refreshFlowStickyHeader` / `updateFlowUsageBadge` /
  `scrollFlowConversationToBottom`) raises an exception inside `appendLocalReply`
- **THEN** the frontend MUST NOT surface a network-error toast
  (`Could not send — network error reaching the server`) and MUST instead show
  the success toast (`Response sent.`) and clear the reply input, because
  success is decided solely by `resp.ok`
- **AND** the reply's `__localEcho` record MUST already have been written into
  `state.flowConversationRecords` (state-first), so the confirmation is inlined
  into the conversation message list rather than dropped
- **AND** the render exception MUST be logged (`console.error`) and swallowed by
  `appendLocalReply` rather than propagating into `sendReply`'s outer `catch`

### Requirement: Live-Append Record Deduplication Against Snapshot

A running flow's conversation (and the history view) is built from two sources
that can deliver the **same** records: the `GET /api/history/{flow_id}` snapshot
fetched by `loadFlowConversation` / `openHistorySession`, and the incremental
`history_data` appends pushed over the `/ws/ui` channel and merged by
`applyHistoryData`. On the server, `ws.py` handles `MSG_HISTORY_DATA` by first
writing the cache (`state.append_history`) and then broadcasting the batch to UI
(`_push_history_data`). When a snapshot fetch lands in the window **after** the
cache write but **before** the WS broadcast arrives, the snapshot already
contains that batch; the subsequently-arriving `history_data` append for the
**same** batch would then be `concat`-ed in a second time. The result is a batch
of records (non-interactive `user` / `system` prompt chips, `step_completed`
report cards, etc.) rendered twice during live streaming, which vanishes on a
manual reload because the reload re-fetches the clean, non-duplicated server
cache. The view MUST suppress this duplication on the **append** path so each
record is held — and rendered — exactly once, without changing the daemon push
logic, the server `append_history` / `_push_history_data` cache-then-broadcast
order, or the WS protocol. This is a **pure-frontend** fix: the server cache
itself is never duplicated, so `mergeSnapshotWithLiveAppends` is left unchanged.

`mergeSnapshotWithLiveAppends` already dedups the *opposite* race — appends that
arrive **during** the fetch `await` — but it cannot cover appends that arrive
**after** the fetch has already resolved into state. The two symmetric races are
therefore each owned by one pure function, both keyed on the **same** record
identity function `recordKey`:

1. **`dedupeAppendRecords(existing, incoming)`** — a new DOM-free, side-effect-free
   exported pure function (same export-block pattern as
   `mergeSnapshotWithLiveAppends` / `historyListEmptyState`). It builds its `seen`
   `Set` of `recordKey`s NOT from the entire `existing` array, but only from a
   **bounded recent tail window** of `existing` (window length at least
   `incoming.length` plus a safety margin — e.g. `max(incoming.length, 64) +
   incoming.length`), and returns only those `incoming` records whose `recordKey`
   is not already present in that tail window: an empty array when every incoming
   record is already held in the tail, an array equivalent to `incoming` when all
   are new, and the new-only subset otherwise. The bounding is load-bearing: both
   duplication races this function guards (snapshot-vs-broadcast overlap,
   reconnect delta re-pull) are physically confined to the **most recent**
   records, so a true duplicate can only ever appear in the tail. Comparing
   against the whole array instead would let a genuinely-new record be permanently
   suppressed whenever its **coarse** `recordKey` (a second-granularity timestamp
   plus truncated content, with no uniqueness guarantee) happens to **collide**
   with some far-back old record — e.g. a repeated `1` / `按1确定` reply emitted at
   the same wall-clock second under a reused discovery `step_id`. Restricting the
   comparison to the recent tail keeps every real tail-overlap duplicate filtered
   while ensuring no remote old record can ever shadow a new one. `recordKey`
   itself is left **unchanged**, so the snapshot/append/usage identity shared with
   `mergeSnapshotWithLiveAppends` and usage deduplication is unaffected. Because
   `partial` / `stream_progress` segmented records' `recordKey` naturally varies as
   their content accumulates, later fragments of the same streaming record are NOT
   falsely deduped.
2. **Both append consumers filtered** — `applyHistoryData`'s append branch MUST
   run `incoming` through `dedupeAppendRecords` against the *current* held array
   before merging, for **both** the running-flow view (`state.flowConversationRecords`)
   and the history view (`state.historyRecords`). When the filtered result is
   empty, that consumer MUST short-circuit: it makes no state change and triggers
   no render, preserving the existing incremental semantics (no spurious
   re-render). When the result is a non-empty subset, only the genuinely new
   records are `concat`-ed, so the `st.count` incremental-cursor render path
   (`renderConversation` / `renderHistoryRecords`) and the downstream stick
   judgement / `reconcileLocalEchoes` calls keep working unchanged.

The `mode: full` (non-append) snapshot-replacement path is NOT affected by this
dedup — it replaces the held array wholesale and is left exactly as-is.

#### Scenario: Same batch in snapshot and live append is held once
- **GIVEN** a snapshot fetch for a running flow resolves in the window after the
  server wrote a `history_data` batch to its cache but before the matching WS
  `history_data` append has been delivered, so the snapshot already contains that
  batch
- **WHEN** the WS `history_data` append for the **same** batch later arrives and
  `applyHistoryData` runs its append branch
- **THEN** `dedupeAppendRecords` filters out every record whose `recordKey` is
  already present in the held array, so no record is `concat`-ed a second time
- **AND** the conversation / history record array contains no duplicate
  `recordKey`, and the batch is rendered exactly once

#### Scenario: Append of all-already-present records makes no change
- **GIVEN** an incremental `history_data` append whose records are all already
  held in `state.flowConversationRecords` (or `state.historyRecords`)
- **WHEN** `applyHistoryData` filters the append through `dedupeAppendRecords`
- **THEN** the filtered result is empty and that consumer short-circuits — no
  state mutation and no re-render — preserving the incremental-render semantics

#### Scenario: Append with a mix of seen and new records merges only the new ones
- **GIVEN** an incremental append whose records partly overlap the held array by
  `recordKey` and partly introduce new `recordKey`s
- **WHEN** `applyHistoryData` filters the append through `dedupeAppendRecords`
- **THEN** only the records with previously-unseen `recordKey`s are `concat`-ed
- **AND** the `st.count` incremental cursor advances by exactly the number of new
  records, so `renderConversation` / `renderHistoryRecords` render only the
  genuinely new tail

#### Scenario: Append of all-new records behaves as before
- **GIVEN** an incremental append whose records are all new (no `recordKey`
  overlap with the held array)
- **WHEN** `applyHistoryData` filters the append through `dedupeAppendRecords`
- **THEN** every record passes through and is merged, identical to the
  pre-fix behavior

#### Scenario: Accumulating streaming fragments are not falsely deduped
- **GIVEN** a `partial` / `stream_progress` record whose `recordKey` changes as
  its content accumulates across successive fragments
- **WHEN** later fragments arrive via incremental append
- **THEN** `dedupeAppendRecords` treats each accumulated fragment as a distinct
  record (its `recordKey` differs from the prior fragment) and does NOT suppress
  it

#### Scenario: Live append after respond/interject keeps appending without re-entering the view
- **GIVEN** a running flow is open in `#flow-view` with its `/ws/ui` subscription
  live, and the user submits a `respond` (e.g. `按1确定`) or an `interject`
- **AND** the server, per `server::Server Modules`, continues to broadcast the
  subsequent `mode: append` real-time increments to `/ws/ui` even when one of
  those frames also resolves a pending REST `GET /api/history/{flow_id}` pull
  waiter (the suppression applies only to `mode: full` responses)
- **WHEN** the post-respond records (the agent's next auto-produced output and any
  later user-sent messages) arrive — possibly delivered both in the REST pull
  response and over the WS broadcast for the overlapping batch — and
  `applyHistoryData` runs its append branch
- **THEN** `dedupeAppendRecords` holds each record by `recordKey` exactly once, so
  the doubly-delivered overlap is rendered a single time
- **AND** the conversation keeps appending the new records live, with no record
  lost, so the operator does NOT have to leave and re-enter the view to trigger a
  fresh full snapshot before seeing further conversation

#### Scenario: New post-respond record colliding with a far-back old record still renders live on a healthy connection
- **GIVEN** a running flow is open in `#flow-view` with its `/ws/ui` subscription
  live and reporting connected (no reconnect / no REST re-pull in flight), and the
  user has submitted a `respond` (e.g. `按1确定`)
- **AND** a subsequent genuinely-new `mode: append` record arrives whose **coarse**
  `recordKey` happens to **collide** with some far-back old record already held in
  `state.flowConversationRecords` — e.g. the same `1` reply content at the same
  wall-clock second under a reused discovery `step_id`
- **WHEN** `applyHistoryData` runs its append branch and filters the append through
  `dedupeAppendRecords`
- **THEN** because `dedupeAppendRecords` builds its `seen` set only from the bounded
  recent tail window of the held array, the colliding far-back old record is NOT in
  that window, so the new record is classified as fresh and is `concat`-ed
- **AND** `applyHistoryData` does NOT short-circuit via `if (!fresh.length) return;`,
  so the agent's later output and the user's subsequent messages keep rendering live
  — the operator does NOT have to leave and re-enter the session to recover the
  conversation through a fresh full snapshot

### Requirement: Reconnect Incremental History Refresh

When the `/ws/ui` channel drops and reconnects while a running flow is open in
`#flow-view` — the common case being a mobile browser backgrounded and then
brought back to the foreground — the view MUST refresh the conversation
**incrementally** instead of clearing the container and reloading the entire
bundle. The old behavior (`ws.onopen` with `wasReconnect` true calling
`loadFlowConversation`, which did `container.innerHTML = ""` → full re-fetch →
full re-render) is replaced for the reconnect path: for a large session the full
path's network transfer, JSON parse, and DOM rebuild are all expensive, and the
daemon→server history push and the server→frontend `history_data` append are
already incremental, leaving only the REST snapshot re-fetch as the full-reload
offender.

The running-flow reconnect loader MUST:

1. **Distinguish first-open from reconnect refresh explicitly.** First-open (a
   user opening the flow) remains a **full** load — it sends no `after` token,
   resets the held records, and renders from scratch. Only the `ws.onopen`
   reconnect path runs the incremental refresh; the load type MUST be carried
   explicitly (e.g. an `incremental` option), never inferred from whether the
   held record array happens to be empty.
2. **Hold and echo a progress token.** The view keeps the opaque progress token
   returned by the last `GET /api/history/{flow_id}` snapshot in
   `state.flowConversationProgress` (kept independent from the history view's
   `state.historyProgress` so the two views never cross-feed). On a reconnect
   refresh it echoes that token via `GET /api/history/{flow_id}?after=…` and does
   NOT clear `#flow-conversation` or discard `state.flowConversationRecords`.
3. **Branch on the server's `delivery` tag.** On a `delivery: "delta"` answer the
   loader appends only the returned delta records through the existing merge
   pipeline — `dedupeAppendRecords` for identity dedup,
   `mergeSnapshotWithLiveAppends` for the in-flight live-append race, and
   `reconcileLocalEchoes` for optimistic reply echoes — and uses the incremental
   tail-append render so the DOM, fold state, raw toggles, and scroll position
   are preserved. On a `delivery: "full"` answer (or any case the server could
   not safely serve a delta) the loader falls back to the current authoritative
   full-snapshot merge and full re-render.
4. **Keep the held token coherent.** The fresh `progress` token from each
   response replaces `state.flowConversationProgress`. When a live `history_data`
   push replaces the bundle wholesale (`mode: full`), the held token no longer
   pins the server bundle and MUST be dropped so the next reconnect re-fetch
   falls back to a full load rather than echoing a stale delta cursor. Opening a
   different flow MUST also drop the held token so a prior flow's cursor can
   never be echoed against this flow's bundle.

This path preserves the no-loss / no-reorder guarantees of *Conversation Strict
Chronological Order* and produces no duplicate records when the incremental
re-fetch overlaps with live `history_data` appends, because it routes the delta
through the same dedupe / merge / reconcile functions the live-append path uses.

#### Scenario: Reconnect re-fetch appends only the delta and preserves the DOM
- **GIVEN** a running flow is open in `#flow-view` with a held
  `state.flowConversationProgress` token and a rendered conversation
- **WHEN** the `/ws/ui` channel reconnects (`ws.onopen`, `wasReconnect` true) and
  the loader re-fetches `GET /api/history/{flow_id}?after=<token>` and the server
  answers `delivery: "delta"`
- **THEN** the view does NOT clear `#flow-conversation` or discard
  `state.flowConversationRecords`
- **AND** only the returned delta records are appended through
  `dedupeAppendRecords` / `mergeSnapshotWithLiveAppends` / `reconcileLocalEchoes`
  and rendered via the incremental tail-append path
- **AND** the conversation contains no duplicate `recordKey`, records stay in
  strict chronological order, and existing fold state / scroll position survive

#### Scenario: First open stays a full load
- **WHEN** a user opens a running flow (not a reconnect refresh)
- **THEN** the loader sends no `after` token, the server answers
  `delivery: "full"`, and the conversation is loaded and rendered from scratch
- **AND** the load type is decided explicitly by the caller, not inferred from an
  empty record array

#### Scenario: Full fallback re-renders authoritatively
- **GIVEN** a reconnect re-fetch echoes a held progress token
- **WHEN** the server cannot safely serve a delta (stale / mismatched token,
  cache replaced or missed, machine / owner change) and answers
  `delivery: "full"`
- **THEN** the loader falls back to the authoritative full-snapshot merge and a
  full re-render, with the final rendered result equivalent to the pre-change
  full-reload behavior
- **AND** the held progress token is replaced with the fresh token from the
  response

#### Scenario: Held token is dropped when the bundle is replaced
- **GIVEN** a held `state.flowConversationProgress` token for the open flow
- **WHEN** a live `history_data` push replaces the bundle wholesale
  (`mode: full`), or a different flow is opened
- **THEN** the held token is dropped so the next reconnect re-fetch falls back to
  a full load rather than echoing a stale delta cursor against a bundle it no
  longer pins

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
that intervention's `prompt` (Markdown rendered, behind a default-collapsed
expand/collapse trigger — see the *Collapsible Reply-Context Prompt Body*
requirement) and any `options` action buttons inside the reply context panel
above the shared reply textarea; the panel MUST NOT render a separate `context`
block for any kind. The same reply textarea is the single input surface for
every intervention kind.

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
the `Enter 1 to confirm` textual hint as a fallback, so a user who ignores the button
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
- **AND** the panel also shows the `Enter 1 to confirm` textual hint so the user can
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
  intervention's kind header, its Markdown-rendered `prompt` behind a
  default-collapsed expand/collapse trigger (see *Collapsible Reply-Context
  Prompt Body*), and any `options` action buttons — and renders no separate
  `context` block
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

### Requirement: Collapsible Reply-Context Prompt Body

The docked reply context panel (`updateReplyBox` in `app.js`) MUST NOT render a
selected intervention's `prompt` as an unbounded, always-expanded block. A long
`prompt` — most notably a `discovery_confirm` whose `prompt` embeds an entire
refined task description — would otherwise grow the `.flow-reply-prompt` body
without limit and push the height-bounded reply controls (kind header, any
synthesized confirm / `options` buttons, the reply textarea, and the Send
button) out of the viewport, leaving them unreachable. To prevent this, the
prompt body MUST be mounted as a **default-collapsed, expand-on-demand block**
with a **height-bounded, internally scrollable** expanded state. This scope is
strictly limited to `#flow-view`'s reply-context panel (the sole consumer of the
collapsible-prompt helper); the history view and all other surfaces are
untouched.

The structure is built by a pure helper (`buildCollapsiblePrompt`, exported for
DOM-stub tests) and has these properties:

1. **Default collapsed** — when a chip is first selected, the `.flow-reply-prompt`
   Markdown body is rendered but hidden by default (e.g. a `.hidden` class
   mapping to `display: none`), so the panel's initial height carries only the
   kind header, the expand/collapse trigger, the `options` / confirm buttons, the
   textarea, and Send. This loses no information: the prompt body is redundant
   with the conversation chat-stream for every kind (a `call` prompt is the agent
   turn text; `cli_confirm` enters the stream via `StreamJSONTracker`;
   `retry_decision` surfaces as a `step_failed` card; the `discovery_confirm`
   refined description already appears as an assistant message; `interjection`
   carries no prompt).
2. **Expand/collapse trigger** — an always-visible toggle control (e.g.
   "▸ expand message details" / "▾ collapse") sits above the body; clicking it
   toggles the body's visibility. Expanding calls
   `Element.scrollIntoView({block: "nearest"})` (via `requestAnimationFrame`) on
   the freshly shown body so the revealed content scrolls into view; collapsing
   does NOT scroll, consistent with the view's other foldable affordances.
3. **Height-bounded expanded body** — when expanded, the `.flow-reply-prompt`
   body MUST be capped at a viewport-relative maximum height (currently
   `max-height: 30vh`) with `overflow-y: auto`, so a prompt of any length
   occupies at most that fixed fraction of the viewport and is scrolled
   internally. The body MUST also keep horizontal overflow out (`overflow-x:
   hidden`) and wrap long lines (`overflow-wrap: anywhere` / `word-break`), per
   the *Long-Content Wrapping* requirement. The kind header, expand trigger,
   `options` / confirm buttons, textarea, and Send button all sit OUTSIDE this
   height-capped region and therefore remain visible and clickable regardless of
   prompt length.

The expand state is persisted as a session-level UI preference keyed by
intervention id (e.g. `call:<callId>`), so a user's manual expand/collapse
choice survives automatic re-renders (STATUS_UPDATE / ws push →
renderInterventions → updateReplyBox). The persisted map is reset when
opening or closing `#flow-view`, so switching to a different flow or
closing the view returns to the default collapsed state. The synthesized
`discovery_confirm` confirm button (see *Unified Intervention Items*) and all
other `options` buttons remain outside the collapsible body so they are reachable
without expanding the prompt.

The expanded body's internal **scroll position** MUST likewise survive automatic
re-renders. Because each refresh rebuilds the reply-context block wholesale
(`ctx.innerHTML = ""`), a newly created `.flow-reply-prompt` body would reset its
`scrollTop` to 0; left unguarded, the high-frequency 3s detail poll and ws
`STATUS_UPDATE` pushes would repeatedly snap a user reading a long expanded body
back to the top. To prevent this, the expanded body's `scrollTop` MUST be
persisted as a second session-level UI preference keyed by the same intervention
id (parallel to the expand-state map, e.g. `state.flowReplyPromptScroll`):
`buildCollapsiblePrompt` registers a `scroll` listener on the body that records
its live `scrollTop` into that map, and on each rebuild `updateReplyBox` feeds the
last recorded `scrollTop` back to `buildCollapsiblePrompt`, which — only when the
body is initially expanded — restores it (via `requestAnimationFrame`, after
layout). This leaves the refresh mechanism and frequency unchanged and keeps the
two restore paths independent: a fresh user expand still runs the
`scrollIntoView` path, while a refresh rebuild runs only the `scrollTop` restore.
The scroll-position map is reset alongside the expand-state map when opening or
closing `#flow-view`.

#### Scenario: Long prompt is collapsed by default and never pushes controls off-screen
- **GIVEN** a selected intervention chip (e.g. `discovery_confirm`) whose
  `prompt` is very long (an embedded refined task description)
- **WHEN** the reply context panel is rendered
- **THEN** the `.flow-reply-prompt` Markdown body is hidden by default behind an
  expand/collapse trigger
- **AND** the panel's kind header, any confirm / `options` buttons, the reply
  textarea, and the Send button are all visible and clickable without expanding
  the prompt

#### Scenario: Expanding the prompt bounds its height and scrolls internally
- **GIVEN** a selected chip whose prompt body is collapsed
- **WHEN** the user clicks the expand trigger
- **THEN** the `.flow-reply-prompt` body becomes visible, capped at the
  configured maximum height (`max-height: 30vh`) with `overflow-y: auto`, so an
  arbitrarily long prompt scrolls inside that fixed region rather than growing
  the panel
- **AND** the body is scrolled into view via
  `scrollIntoView({block: "nearest"})` on expand
- **AND** the header, trigger, `options` buttons, textarea, and Send button
  remain outside the height-capped region and stay visible

#### Scenario: Collapsing the prompt does not scroll, and expand state persists across re-render
- **GIVEN** an expanded prompt body
- **WHEN** the user clicks the trigger to collapse it
- **THEN** the body is hidden again and the view does NOT scroll
- **AND** the collapsed state is persisted so that subsequent automatic
  re-renders (e.g. a new snapshot) keep the prompt collapsed
- **AND** when `#flow-view` is closed or a different flow is opened, the
  expand state resets to the default collapsed

#### Scenario: Scroll position of an expanded body survives automatic re-renders
- **GIVEN** a selected chip whose `.flow-reply-prompt` body is expanded and the
  user has scrolled down inside its height-capped (`max-height: 30vh`) region to
  read long content
- **WHEN** an automatic rebuild fires (a `STATUS_UPDATE` / ws push or the 3s
  detail poll drives renderInterventions → updateReplyBox, which rebuilds the
  reply-context block via `ctx.innerHTML = ""`)
- **THEN** the freshly rebuilt body's internal `scrollTop` is restored to the
  user's last recorded position (persisted per intervention id in
  `state.flowReplyPromptScroll`) rather than snapping back to the top, so the
  long content stays readable
- **AND** the restore runs only for an initially-expanded body and does NOT
  trigger the first-expand `scrollIntoView` path, leaving the refresh mechanism
  and frequency unchanged
- **AND** when `#flow-view` is closed or a different flow is opened, the
  scroll-position state resets alongside the expand state to the default

### Requirement: Interjection Lifecycle Events

A web-console interjection's full lifecycle — **pending** (call file just
written by the daemon, not yet drained by the run loop) and **consumed**
(drained by the run loop, the call file gone) — MUST be observable to the
frontend through the `/ws/ui` channel, so the operator gets visible
feedback for every interjection they send instead of a silent "did it
work?" gap.

The lifecycle is carried as a lightweight UI-side event, `interjection_event`,
broadcast by the server's `/ws/ui` channel and derived from the daemon's
`STATUS_UPDATE` `pending_calls` diff (no new daemon↔server protocol message
type is added — see `base` spec's *Daemon Modules* / *Server Modules*
requirements). Each event carries at least `flow_id`, `call_id`, and a
`phase` field whose value is one of `pending` or `consumed`. The diff
detection happens in `ws.py` (the `STATUS_UPDATE` handling path), so an
upstream daemon that has not been upgraded still produces consistent
events as long as it reports current `pending_calls`.

To make the diff itself responsive — instead of waiting for the daemon's
~5-second `status_interval` tick — the daemon's interjection write path
and consumption-detection path MUST trigger an out-of-band immediate
status push (`_fast_push_event` in `DaemonClient` or equivalent) so the
lifecycle event reaches `/ws/ui` within ~1 second of either the call file
appearing or its sibling `.response` being written / the call file being
unlinked.

The frontend MUST deduplicate `interjection_event`s per
`(call_id, phase)` to ensure each lifecycle transition triggers exactly
one toast / chip state change. The chip's visible state and the toast
text MUST track the real lifecycle: a freshly written interjection
appears as a `pending` chip with a "waiting for the flow to drain"
phrasing; a `consumed` event flips the chip into a consumed visual state
(or removes it, matched against the history-jsonl interjection record
that the run loop now also writes) and updates the toast accordingly.
The toast MUST NOT claim the interjection "took effect" earlier than the
`consumed` event arrives.

#### Scenario: Pending event surfaces on the chip bar
- **GIVEN** the user submits an interjection through the docked reply box
  for a running flow
- **WHEN** the daemon writes the `interjection`-kind call file under that
  flow's `se3/calls/` and triggers an immediate status push
- **THEN** the `/ws/ui` channel broadcasts an `interjection_event` whose
  `phase` is `pending` and whose `flow_id` / `call_id` identify the call
- **AND** the frontend shows the pending interjection in the chip bar and
  surfaces a toast acknowledging that the interjection is queued

#### Scenario: Consumed event reflects run-loop drain
- **GIVEN** a `pending` interjection event for `(flow_id, call_id)` is
  already on screen
- **WHEN** the run loop drains the call file (writing the sibling
  `.response` then unlinking) and the daemon fast-pushes the resulting
  `pending_calls` diff
- **THEN** the `/ws/ui` channel broadcasts a second `interjection_event`
  for the same `(flow_id, call_id)` with `phase = consumed`
- **AND** the frontend transitions the chip to a consumed state (or
  removes it) and updates the toast to reflect that the interjection was
  taken in by the flow

#### Scenario: Frontend dedupes events by (call_id, phase)
- **WHEN** the daemon emits two `STATUS_UPDATE`s in quick succession that
  both diff to the same `interjection_event` for `(call_id, phase)`
- **THEN** the frontend MUST apply exactly one chip / toast transition
  for that `(call_id, phase)` pair, not one per `STATUS_UPDATE`
- **AND** the dedup MUST NOT swallow the **next** phase event for the
  same `call_id` (e.g. a later `consumed` after a `pending`)

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

**Single step region per `step_id`.** All records sharing the same `step_id`
— the `step_started` running anchor, `assistant` / `user` / `system`
conversation turns, `step_output` process records, and the terminal
`step_completed` / `step_failed` report — MUST be grouped into **one** visual
step region (they share a single `stepKey` / `__convStepKey = step_id`). A
`step_completed`, `step_failed`, or `step_output` record MUST NOT spawn a new
step region bearing the same step name as one that already exists; only a
record with a **new** `step_id` opens a new step region. Terminal and
non-terminal records for the same `step_id` reconcile by supersede (the
terminal report supersedes the `step_started` running anchor in place) rather
than appending a second same-named section.

**Stable, low-saturation visual grouping.** Each step region MUST carry a
**stable, low-saturation** visual grouping style (a per-`step_type` background
or accent applied through a consistent CSS-variable set) that makes the step
boundary clear and that is **distinguishable yet consistent** across step
types — the same step type always renders the same low-key accent. The styling
MUST preserve text contrast and long-content readability and MUST NOT break the
mobile (phone-portrait) layout. Step status (running / completed / failed /
paused / retrying) MUST be conveyed with **explicit text or an icon**, never by
colour alone, so the status stays legible under reduced-colour rendering.

The per-`step_type` background MUST read as **one continuous band** spanning the
whole step region: where a step region holds several content records (each an
independent sibling block separated by the conversation layout `gap`), the
low-saturation background MUST fill the gaps between adjacent same-`step_id`
blocks so the region reads as a single grouped band rather than separate
per-block islands — while each content block (assistant bubble, tool marker,
step report card) stays visually distinct through its own border/background. The
band MUST stay strictly within its step region: it MUST NOT cross a step-header
separator into an adjacent step region nor bleed past the conversation area's
top/bottom edge, so the step-to-step boundary is unchanged, and the status / DAG
status markers are excluded from the band.

#### Scenario: Same step_id records group into one step region
- **GIVEN** a `step_started` anchor, one or more `assistant` / `step_output`
  records, and a terminal `step_completed` record that all carry the same
  `step_id`
- **WHEN** the conversation is rendered in `#flow-view`
- **THEN** all of them fall into a single visual step region keyed by that
  `step_id`, and the terminal report supersedes the running anchor in place
- **AND** the `step_completed` / `step_failed` / `step_output` records do NOT
  open a second step region bearing the same step name; only a record with a
  new `step_id` starts a new region

#### Scenario: Step status is conveyed by text or icon, not colour alone
- **GIVEN** step regions in running, completed, failed, paused, and retrying
  states, each carrying its stable per-`step_type` low-saturation grouping
  style
- **WHEN** the regions are rendered
- **THEN** each region's status is shown with explicit text or an icon (not
  colour alone), and the grouping style preserves text contrast and does not
  introduce horizontal overflow on the phone-portrait breakpoint

#### Scenario: Multi-block step region reads as one continuous background band
- **GIVEN** a single step region keyed by one `step_id` that contains two or
  more content records — e.g. an `assistant` bubble, a tool marker, and a
  terminal step report card — rendered as sibling blocks separated by the
  conversation layout `gap`
- **WHEN** the conversation is rendered (in `#flow-conversation` or, through the
  same shared engine, in the History detail pane)
- **THEN** the low-saturation per-`step_type` background fills the inter-block
  gaps so the whole step region reads as one continuous band, while each block
  remains visually distinct via its own border/background
- **AND** the band does not cross a step-header separator into an adjacent step
  region (step-to-step boundaries are unchanged), the status / DAG status
  markers are excluded from it, and no horizontal overflow is introduced on the
  phone-portrait breakpoint

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

### Requirement: Step Region Appears at RUNNING

A step's region MUST appear in the running flow's conversation the moment the
step enters `RUNNING` — showing the step and an explicit *in-progress* status
(text + icon) — **without** waiting for the step's first chat record or its
`step_completed` event. This behaviour MUST be uniform across **every**
`StepType`, including the steps that produce no LLM conversation at all
(`TEST`, `COMMIT`, `SPEC_GATE`): such a step would otherwise stay invisible on
the web console until it finished.

The mechanism reuses the existing per-step jsonl / `history_data` channel: the
engine persists a `STEP_STARTED` event as a `type: "step_started"` record (see
`flow-engine: Event Stream and Sink Interface`), and the frontend's
`normalizeRecord` recognises `eventType === 'step_started'` and produces a
lightweight running **anchor** record — `{ role: 'step-event', kind:
'step_started', stepType, stepId, status: 'running', timestamp }` with **no**
report card and no fold/raw/chip affordances. The anchor shares the same
`step_id` (`__convStepKey`) as the step's later conversation, `step_output`,
and terminal `step_completed` / `step_failed` records, so it groups into the
same single step region (see *Conversation Strict Chronological Order*) and is
superseded in place by the terminal report when the step finishes — it never
produces a second same-named region.

#### Scenario: Non-LLM step shows a region the moment it enters RUNNING
- **GIVEN** a `TEST` (or `COMMIT` / `SPEC_GATE`) step that produces no LLM
  conversation and has just entered `RUNNING`, emitting a `step_started` record
- **WHEN** the conversation is rendered in `#flow-view`
- **THEN** a step region for that step appears immediately with an explicit
  *in-progress* status (text + icon) and no report card, before any chat
  record or `step_completed` event arrives
- **AND** `normalizeRecord` produced the running anchor from
  `eventType === 'step_started'`

#### Scenario: Running anchor is superseded in place by the terminal report
- **GIVEN** a step whose `step_started` running anchor is already shown
- **WHEN** the step's terminal `step_completed` / `step_failed` record (same
  `step_id`) arrives
- **THEN** the terminal report card supersedes the running anchor inside the
  same step region, and no new same-named step region is created

#### Scenario: Live channel resumes after switching out of the paused-wait state
- **GIVEN** a `discovery` (or any step that pauses each round to await user
  input) whose step region shows the design-intended *paused* status row (the
  engine recorded `step_status: paused` when the step returned `PAUSED` to await
  the operator's answer)
- **WHEN** the operator submits their answer, the main loop sets the step back
  to `PENDING` and re-runs it — emitting a fresh `step_started` running anchor
  plus the subsequent running / append records (including, under the
  daemon-resume shape, the records produced by the second `--resume` process
  spawned after the first process wrote `paused` and exited)
- **THEN** the live (append / broadcast) channel carries those post-answer
  `step_started` / append records through the existing subscription, cache
  generation, and append cursor without dropping them, and the post-answer
  running anchor survives `dedupeAppendRecords` so that `removeSupersededStatusRows`
  supersedes the frozen *paused* status row in place and real-time appending
  resumes
- **AND** the operator sees the new and subsequent messages live, without
  having to exit and re-enter the flow to force a full re-entry / rebuild
- **AND** this fix targets only the switch-**out** of the paused-wait state; the
  design-intended *paused* status shown while a round awaits input is unchanged

### Requirement: Viewport-Driven Sticky Step Header

The conversation scroll region MUST present a **viewport-driven sticky step
header**: a floating header pinned to the top of the scroller that always shows
the title of whichever step the **top of the viewport** currently falls inside.
This behaviour MUST be identical in the running-flow view (`#flow-conversation`)
and the history detail view (`#history-detail`), since both render through the
shared `renderConversation` engine.

The floating header and the step region's own in-stream original
`.history-step-header` MUST be **mutually exclusive**: when a step's original
header is normally visible in the scroll viewport, the floating header for that
step is hidden; the floating header shows only when the original header has
scrolled out of view above the top edge. The header reflects **only** the step
the viewport top currently belongs to — NOT the step the flow is currently
executing.

Scrolling MUST switch the floating title immediately and symmetrically: when
the user scrolls **up** far enough that part of the previous step's content
re-enters the top of the viewport, the floating title MUST switch to that
previous step's title at once; scrolling **down** switches forward by the same
rule. The decision logic MUST be factored into a pure function
`computeStickyStep(headerOffsets, scrollTop)` so it is unit-testable in a
layout-free DOM stub.

Clicking the floating header MUST smoothly scroll the region so that step's
original `.history-step-header` lands exactly at the top of the scroll region;
once the scroll completes the original header is normally visible and the
floating header hides. This interaction MUST NOT change any step status or the
flow's execution state — it is pure navigation.

#### Scenario: Floating header reflects the step at the viewport top
- **GIVEN** a conversation scrolled so that step B's content occupies the top
  of `#flow-conversation` while step A's content is above the top edge
- **WHEN** the sticky logic re-measures via `computeStickyStep(headerOffsets,
  scrollTop)`
- **THEN** the floating header shows step B's title — the step at the viewport
  top — regardless of which step the flow is currently executing

#### Scenario: Scrolling up switches the floating title to the previous step
- **GIVEN** the floating header currently showing step B
- **WHEN** the user scrolls up until part of step A's content re-appears at the
  top of the viewport
- **THEN** the floating header immediately switches to step A's title; scrolling
  back down switches it forward to step B by the same rule

#### Scenario: Floating and original headers are mutually exclusive
- **GIVEN** a step whose original `.history-step-header` scrolls into the
  visible region at the top
- **WHEN** the original header becomes normally visible
- **THEN** the floating header for that step is hidden, and it re-appears only
  once the original header scrolls back out above the top edge

#### Scenario: Clicking the floating header scrolls the original header to the top
- **GIVEN** the floating header is shown for a step
- **WHEN** the user clicks it
- **THEN** the region smoothly scrolls until that step's original
  `.history-step-header` sits exactly at the top of the scroll region, after
  which the original header is visible and the floating header hides
- **AND** no step status or flow execution state changes as a result

#### Scenario: Sticky behaviour is identical in the history detail view
- **GIVEN** a past flow opened in the history detail view (`#history-detail`),
  rendered through the shared `renderConversation` engine
- **WHEN** the user scrolls the detail region
- **THEN** the viewport-driven floating step header behaves exactly as in
  `#flow-conversation` — same viewport-top step selection, same mutual
  exclusion with the original header, and same click-to-locate navigation

#### Scenario: Floating header pins flush to the scroll viewport top with no gap
- **GIVEN** a conversation scroll container — `#flow-conversation` or
  `#history-detail` — whose content box carries a non-zero `padding-top`, on
  either the desktop or the mobile breakpoint
- **WHEN** a step's floating header becomes pinned at the top of the scroller
- **THEN** the floating header's top edge sits flush against the top of the
  scroll **viewport** (the padding outer edge / border inner edge), leaving no
  visible gap of exposed top padding between the header and the viewport top —
  the sticky anchor's pin offset cancels the container's `padding-top` so the
  pin point lands on the viewport top rather than the content-box top
- **AND** this flush-to-top alignment is identical across the
  `#flow-conversation` and `#history-detail` views and across the desktop and
  mobile breakpoints
- **AND** the conversation body's own padding and vertical spacing are
  unchanged, and the floating header's show/hide trigger, its mutual exclusion
  with the in-stream original `.history-step-header`, and its click-to-locate
  navigation all remain as specified above

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

Long-content wrapping is NOT limited to `<pre>` / code-style blocks: step
report card list items (`.step-report__list li`) — the normal-flow list rows
produced by `reportList()` for **every** report list section (e.g. `Tests
Added`, `Incomplete Tasks`, `Restricted Edits`, and any current or future
`reportList()` consumer) — MUST also wrap long content inside their report
card. Because these rows carry long file paths, long single words, or
whitespace-free long text, the `.step-report__list li` rule MUST set a
per-character break rule (`overflow-wrap: anywhere` and/or
`word-break: break-word`). As list items are normal flow text rather than
preformatted code, they MUST NOT require `white-space: pre-wrap`; the existing
`line-height`, list indentation, default-expand behavior, and visual style of
the report card MUST be preserved unchanged. Because each list item is a flex
item of the `.step-report__list` flex column, the rule MUST also set
`min-width: 0` so the item can shrink below its content's intrinsic width and
wrap instead of pushing the report card boundary out. A long list item MUST
NOT widen the report card, and MUST NOT cause `#flow-view` or the page to gain
an unintended horizontal scrollbar; the fix MUST NOT introduce
`overflow-x: auto` on the list item.

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

#### Scenario: Long step report list item wraps inside the report card
- **GIVEN** a step report card whose `reportList()` section (e.g. a
  `Tests Added` list) contains a list item carrying a long file path such as
  `+ tests/frontend/reply_box_prompt_collapse.test.mjs`
- **WHEN** the report card is rendered in `#flow-view`
- **THEN** the `.step-report__list li` wraps the long path inside the report
  card via a per-character break rule (`overflow-wrap: anywhere` and/or
  `word-break: break-word`) plus `min-width: 0`, so the item never widens the
  card boundary
- **AND** the full path text is rendered without truncation and is readable
  without relying on a horizontal scrollbar
- **AND** neither `#flow-view` nor the page gains an unintended horizontal
  scrollbar, and `overflow-x: auto` is NOT applied to the list item
- **AND** the report card's existing `line-height`, list indentation,
  default-expand behavior, and visual style are unchanged

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
already-answered-by-sibling-file check can never clear it and the stale "Pending reply"
chip would otherwise persist for the entire run. To close this, the aggregator
(`DaemonAggregator._enumerate_calls` / `_snapshot_for_root`) MUST treat a call
as no longer pending when its owning `context.step_id` is no longer the flow's
current step (or that step has already reached a COMPLETED / FAILED /
REVISION-handled status), and drop it from the flow's `pending_calls` even
when no sibling response file exists. Calls whose step is still the current,
unanswered step remain pending and continue to surface as chips.

**FAILED-status exemption for decision-class kinds.** The "step in a processed
status implies stale call" rule MUST exempt call kinds whose entire purpose is
to surface a decision *because* the step failed. These kinds exist only on a
FAILED step by construction, and treating FAILED as already-handled would
filter the chip out the instant the flow paused — hiding the very interaction
the human needs to answer. The exemption is implemented in
`DaemonAggregator._filter_stale_calls` against a module-level kind set
(`_FAILED_EXEMPT_CALL_KINDS`, currently `{retry_decision}`): for a call whose
`kind` is in the set, the processed-status set is judged with `"failed"`
removed; for every other kind the processed set is unchanged. The exemption is
keyed on call *kind* (not step type) so a future decision-class kind
(`partial_decision`, etc.) joins the set without re-touching the filter body,
and `step_id != current_step_id` plus the remaining processed statuses
(`completed` / `partial` / `revision_needed`) still drop the chip when the
flow has genuinely advanced past the failed step.

Pending calls are additionally **deduplicated per `(flow_id, step_id)`, newest
wins** (`DaemonAggregator._dedup_calls_by_step`). An interactive discovery flow
reuses the *same* `step_id` across successive clarification turns and the
confirmation gate, so each new pause writes a fresh call file keyed to the same
`(flow_id, step_id)`. Only the most recent such call is a live interaction; the
earlier ones are superseded leftovers that, without dedup, would pile up as
stale "Pending reply" chips. The aggregator therefore keeps only the newest unanswered
call per `(flow_id, step_id)` and discards the rest. Calls that cannot be keyed
(missing `flow_id` or `step_id`) are exempt from dedup and are never collapsed
against one another.

#### Scenario: Superseded same-step calls are deduplicated to the newest
- **GIVEN** an interactive discovery flow `F1` whose current step `S` wrote two
  successive call files (an earlier clarification call and a newer one) both
  keyed to `(flow_id = "F1", step_id = "S")`
- **WHEN** the aggregator enumerates `F1`'s `pending_calls`
- **THEN** only the newest call for `(F1, S)` is reported as pending
- **AND** the earlier, superseded call is dropped so no stale "Pending reply" chip
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
  and the stale "Pending reply" chip disappears from `F1`'s docked reply bar during the
  run, not only after the flow archives
- **AND** a call whose step is still the current, unanswered step remains
  pending and continues to surface as a chip

#### Scenario: retry_decision chip stays visible while its step is FAILED
- **GIVEN** a daemon-spawned flow `F1` whose current step `S` has just
  transitioned to FAILED with `retry_count < 3` and the orchestrator has
  written a `retry_decision`-kind call file keyed to `(F1, S)`
- **WHEN** the aggregator enumerates `F1`'s `pending_calls` while `S` is still
  the current step and its `status` is `failed`
- **THEN** the `retry_decision` call is reported as pending and surfaces as a
  decision chip in `F1`'s docked reply bar immediately on pause — the
  FAILED-status staleness rule does not apply to call kinds in the FAILED
  exemption set (currently `{retry_decision}`)
- **AND** the chip clears in the usual way once the operator answers it (the
  resume path consumes the sibling response and deletes the call file) or
  once the flow moves past step `S` for any other reason (e.g. `S` reaches
  `completed` / `partial` / `revision_needed`, or a different step becomes
  the current step)
- **AND** the exemption is keyed on call `kind` rather than the step's type,
  so calls of every other kind still drop the moment their step reaches a
  `failed` status

#### Scenario: A --worktree session shows its full chat in #flow-view
- **GIVEN** a `se3 run --worktree` flow whose per-step history was written
  back to the main session directory as `*.jsonl.from-<branch>` sidecar files
- **WHEN** the user opens that flow's `#flow-view` while it is running and
  again after it has merged back and entered history
- **THEN** the daemon reads and pushes both the primary `*.jsonl` and its
  `*.jsonl.from-<branch>` sidecars, merged under the same logical step id, so
  the running-flow live stream and the completed-flow history both display the
  complete conversation
- **AND** no message after the first renders as "(no readable content)"

#### Scenario: A flow waiting for the main-worktree lock shows running · waiting for lock
- **GIVEN** a flow that has started and is blocked waiting for the
  main-worktree mutex (its `engine.json` is persisted with status RUNNING and
  `waiting_for_lock` set; see the `flow-engine` *Waiting-for-Lock Visible
  Running State* requirement)
- **WHEN** the user views that flow in the web console
- **THEN** `#flow-view` shows it as running and waiting for the lock rather
  than silently stuck at the "published" state
- **AND** once the flow acquires the lock the waiting indicator clears and the
  flow continues as an ordinary running flow

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
sub-sections inside the chip (e.g. "template prefix" / "framework suffix") so a developer can
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

The original NDJSON for any collapsed record (View raw) MUST stay reachable, but
it is **nested inside the record's own expand area** — the collapsed chip's
expand detail for a whole-chip / empty-user-content record, or the Layer-2
"Expand all" area for a marker-split `user` message (see *Three-Tier Progressive
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
and an isolated "Pending reply" chip sat inline in the stream.

The two roles use **different disclosure depths**, by design — a deliberate
**mixed model**, not one unified tier count:

- The **`user` side keeps three layers**: Layer 1 the literal input, Layer 2 the
  full prompt behind "Expand all", and Layer 3 the raw NDJSON behind "View raw" nested
  at the end of the Layer-2 area.
- The **`assistant` side uses two layers**: Layer 1 the narrative + the rendered
  structured result (both visible by default), and a single fold layer "View raw"
  holding the turn's original record. There is NO assistant-side "Expand all"
  wrapper and no third assistant layer — the narrative is a JSON-stripped part of
  the visible first layer, so only the original record is folded.

The target is: **the default view shows the rendered result (plus, on the
assistant side, the JSON-stripped narrative tiled above it); the original record
is one click away behind "View raw"; and, on the user side only, the full prompt
is behind "Expand all".**

The conversation is grouped into per-phase sections
(DISCOVERY / ANALYZE / PLAN / …) and, within each section, turns are presented
in chronological order as User / Assistant pairs (subject to the strict
chronological ordering of the *Conversation Strict Chronological Order*
requirement).

**User turn** — three layers:
1. **Layer 1 (default)** — only the user's literal input (the user-content
   section produced by the `prompt_markers.py` split; see *Role-Based Message
   Collapse*). No framework boilerplate leaks into this default view.
2. **Layer 2 ("Expand all")** — the complete prompt the LLM actually saw,
   presented as the labeled template prefix / framework suffix subsections (the template prefix
   and the framework suffix). Folded by default.
3. **Layer 3 ("View raw")** — the original NDJSON record for the message,
   reachable via a **dedicated user-side raw toggle** (`makeUserRawToggle`)
   **nested at the end of the Layer-2 "Expand all" expand area** (hosted by
   `makeUserPromptToggle`), NOT as a row-level always-visible button. This Layer
   3 MUST be **stably present for every `user` record** regardless of whether the
   second-layer raw payload (`raw_ndjson` / `raw_json`) exists: `makeUserRawToggle`
   prefers the raw payload when present and otherwise falls back to the original
   `.jsonl` envelope record — the normalized persistence-layer JSON envelope
   (`{step_id, step_type, message}`) exposed on the normalized record as
   `norm.raw.envelope` — so the toggle never returns null and the spec's
   three-layer guarantee always holds. Because the user side uses this separate
   path, the shared `makeRawToggle` helper's existing "no raw payload → null"
   contract is preserved unchanged (the user side does NOT modify or depend on
   that contract). Consequently `makeUserPromptToggle` MUST always be provided for
   a marker-split `user` turn so Layer 3 is always reachable.

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
2. **Layer 2 ("View raw", the single fold)** — the turn's original record: the raw
   NDJSON / tool-call JSON / unrendered result-JSON literal, with the Layer-1
   narrative removed. It is folded by default but its toggle button is always
   visible, sitting directly below the rendered result; it is built by
   `makeAssistantRawToggle`. There is no "Expand all" wrapper and no third layer:
   because the narrative already lives in the visible Layer 1, only the original
   record is folded. The assistant raw entry prefers the raw_ndjson / raw_json
   payload and, when neither is present, falls back to the unrendered `content`
   literal so the original record is always reachable.

**Assistant turn with no result JSON** — when this turn produced no final
result JSON (its body is thinking process only — narrative plus tool calls,
including a turn whose only JSON content is intermediate tool calls), the
thinking process MUST stay shown inline as the default view via
`renderToolMarkers` and MUST NOT be folded or contracted into any empty toggle.
The thinking never collapses to empty: it is the visible default and is shown
in full directly, never hidden behind a toggle. To uphold the universal
view-raw guarantee (see *Universal View-Raw for Conversation Messages*), an
always-visible, default-folded "View raw" toggle built by
`makeAssistantRawToggle` MUST sit **below** the inline thinking; it prefers the
turn's `raw_ndjson` / `raw_json` payload and falls back to the unrendered
`content` literal when neither is present, so the original record stays
reachable even on a no-result turn. Only the original record folds — the
thinking itself stays expanded above the toggle.

When a turn has no Layer-1 payload to surface — e.g. a legacy `user` record
whose user-content section is empty, or an assistant turn whose body cannot be
parsed into structured fields — the renderer MUST degrade gracefully and never
drops content: a legacy empty `user` turn collapses to a single chip (with its
View raw raw toggle — the same dedicated `makeUserRawToggle`, which still
resolves to the `.jsonl` envelope record when no raw payload is present —
nested inside the chip's expand detail), while an assistant
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
  opening the single "View raw" fold (its button is always visible, its body
  folded by default)
- **AND** there is no assistant-side "Expand all" wrapper and no third disclosure
  layer

#### Scenario: User turn defaults to the literal input only
- **GIVEN** a `user` turn whose stored body contains the three-segment marker
  sequence wrapping the user's literal input
- **WHEN** the turn is rendered
- **THEN** the default (Layer 1) view shows only the user's literal input,
  with no template prefix or framework suffix text
- **AND** the full prompt the LLM saw is reachable as the template prefix / framework suffix
  subsections behind the "Expand all" (Layer 2) toggle
- **AND** the original NDJSON is reachable via the "View raw" (Layer 3) toggle
  nested at the end of that expanded "Expand all" area, never via a row-level
  always-visible button

#### Scenario: No isolated pending chip embedded in the assistant default view
- **WHEN** an assistant turn is rendered in its default Layer-1 form
- **THEN** no inline "Pending reply" / pending-intervention chip is rendered inside the
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
- **AND** a default-folded, always-visible "View raw" toggle
  (`makeAssistantRawToggle`) sits below the inline thinking, revealing the
  `raw_ndjson` / `raw_json` payload when present and otherwise falling back to
  the unrendered `content` literal, so the original record stays reachable
  without folding the thinking itself

#### Scenario: User Layer 3 raw toggle is nested inside the Layer 2 expansion
- **GIVEN** a marker-split `user` turn rendered with its default Layer-1
  literal-input bubble
- **WHEN** the conversation row is first rendered, before any toggle is opened
- **THEN** no row-level always-visible "View raw" button is present beside the
  bubble
- **AND** the "View raw" (Layer 3) toggle becomes reachable only after the
  "Expand all" (Layer 2) toggle is expanded, nested at the end of that expand area
- **AND** the Layer 3 toggle is present even when the record carries no
  second-layer raw payload (`raw_json` empty and no `raw_ndjson`) — the dedicated
  `makeUserRawToggle` falls back to the original `.jsonl` envelope record
  (`norm.raw.envelope`) so the original record is always reachable
- **AND** this user-side path leaves the shared `makeRawToggle` helper's
  "return null when no raw payload is present" contract unchanged

#### Scenario: Assistant single View raw fold is the only assistant disclosure
- **GIVEN** a result-JSON assistant turn rendered with its default Layer-1
  narrative + structured result
- **WHEN** the conversation row is rendered
- **THEN** the only assistant-side fold is the single "View raw" entry below the
  rendered result — its toggle button is always visible and its body is folded
  by default
- **AND** there is no assistant "Expand all" wrapper, and the "View raw" fold is NOT
  nested inside any "Expand all" area
- **AND** when neither raw_ndjson nor raw_json is present, the "View raw" fold
  falls back to the unrendered `content` literal so the original record stays
  reachable

### Requirement: Universal View-Raw for Conversation Messages

Every conversation-channel record — across **all four message roles** `user`,
`assistant`, `system`, and `other` (any in-stream record whose normalized role
is none of `user` / `assistant` / `system`, e.g. a `tool` / `developer` / `log`
record) — MUST expose an always-present, reachable "View raw" affordance so the
operator can inspect the original record behind any rendered message. This is a
**uniform invariant**: the "View raw" button is **always shown** for every
conversation message, default-folded; when the record carries a `raw_json` /
`raw_ndjson` payload the toggle reveals that payload, and when no raw payload
exists the toggle falls back to the record's own original text / `.jsonl`
envelope. The button MUST NOT appear-and-disappear depending on whether a raw
payload happens to be present — a conversation message without a raw payload
still shows the button and falls back to its envelope / content original.

The uniform guarantee is realized by routing every conversation role through an
**always-non-null** raw-toggle constructor (never the nullable shared
`makeRawToggle`):

- **`user`** — the Layer-3 `makeUserRawToggle`, nested at the end of the Layer-2
  "Expand all" area, falling back to the `.jsonl` envelope (`norm.raw.envelope`)
  when no second-layer raw payload exists (see *Three-Tier Progressive
  Disclosure* and *Role-Based Message Collapse*). This Layer-3 path is the one
  documented exception to the row-level rule: the user toggle is nested inside
  Layer 2, not placed beside the bubble.
- **`assistant`, result-JSON turn** — the single "View raw" fold built by
  `makeAssistantRawToggle` directly below the rendered structured result,
  falling back to the unrendered `content` literal.
- **`assistant`, no-result inline turn** — `makeAssistantRawToggle` appended
  **below** the inline thinking process (the thinking stays shown in full); see
  the *Assistant turn with no result JSON* rule in *Three-Tier Progressive
  Disclosure*.
- **`system`** — the collapsed system-prompt chip's `makeAssistantRawToggle`
  (content fallback) inside the chip's expand detail. This replaces the
  previously nullable `makeRawToggle` call so the button is present whether or
  not the system record carries a raw payload (it previously vanished for
  payload-less system records).
- **`other`** — `makeAssistantRawToggle` appended to the non-collapsible record
  row (the path `other`-role records always take, since their underlying role is
  not in the collapsible-role set), guarded so it is not duplicated on the
  assistant-with-content path that already carries its own toggle.

**Non-conversation synthetic UI MUST stay affordance-free.** The records that
are *synthesized* progress / report UI rather than conversation turns MUST NOT
gain any view-raw affordance from this rule: the `group_status` DAG progress
markers (`renderGroupStatusRecord`; see *Live Per-Group DAG Status Markers*) and
the `step_completed` / `step_failed` step report cards (`renderStepEventRecord`;
see *Per-Step Report Cards*) MUST keep their current rendering with no added
"View raw" toggle and no added fold chip. These records are dispatched **before**
any role/raw logic runs, so the universal view-raw rule never reaches them.

The shared `makeRawToggle` helper's existing "no raw payload → null" contract is
preserved **unchanged** — it is simply no longer called on the conversation
message paths. Any non-conversation path that still depends on the nullable
`makeRawToggle` keeps its no-affordance behavior; conversation message paths MUST
use the always-non-null `makeUserRawToggle` / `makeAssistantRawToggle`
constructors instead of weakening that contract.

#### Scenario: System chip always exposes View raw
- **GIVEN** a `system` role record rendered as a collapsed system-prompt chip
- **WHEN** the chip's expand detail is rendered
- **THEN** an always-present "View raw" toggle (`makeAssistantRawToggle`) is
  shown regardless of whether the record carries a `raw_json` / `raw_ndjson`
  payload
- **AND** with a raw payload present the toggle reveals that payload, and with
  no raw payload it falls back to the record's `content` original
- **AND** the toggle does NOT vanish for payload-less system records (the prior
  nullable `makeRawToggle` behavior is no longer used on this path)

#### Scenario: Other-role message always exposes View raw
- **GIVEN** an in-stream conversation record whose normalized role is `other`
  (e.g. `tool` / `developer` / `log`), rendered via the non-collapsible record
  row
- **WHEN** the row is rendered
- **THEN** an always-present, default-folded "View raw" toggle
  (`makeAssistantRawToggle`) is appended below the bubble
- **AND** it reveals the `raw_json` / `raw_ndjson` payload when present and
  otherwise falls back to the record's original text
- **AND** the toggle is not duplicated on an `assistant`-with-content row, which
  already carries its own toggle from `renderAssistantBubble`

#### Scenario: group_status marker carries no View raw affordance
- **GIVEN** an `implement` step `group_status` DAG progress marker rendered by
  `renderGroupStatusRecord`
- **WHEN** the marker is rendered in `#flow-view`
- **THEN** it carries no "View raw" toggle and no fold chip
- **AND** the universal view-raw rule does not apply to it because it is
  dispatched before any role/raw logic runs

#### Scenario: Step report card carries no added View raw affordance
- **GIVEN** a `step_completed` / `step_failed` event rendered by
  `renderStepEventRecord` (the step report card plus its existing raw-event
  chip)
- **WHEN** the record is rendered
- **THEN** no additional "View raw" toggle is attached by the universal
  view-raw rule — the step report card keeps its existing rendering unchanged

#### Scenario: Conversation view-raw never weakens the shared makeRawToggle contract
- **WHEN** the conversation renderer attaches a "View raw" affordance to a
  `user` / `assistant` / `system` / `other` message
- **THEN** it uses an always-non-null constructor (`makeUserRawToggle` or
  `makeAssistantRawToggle`), never the nullable shared `makeRawToggle`
- **AND** `makeRawToggle` keeps its "no raw payload → null" contract so any
  remaining non-conversation caller stays affordance-free

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
   MUST be routed through the **same shared narrative-rendering helper** the
   no-result assistant path uses (`renderNarrativeNodes(text, norm)`), so that
   when the turn's `raw_json` carries paired `tool_use` / `tool_result` content
   blocks the chip pipeline (see *Tool Call Chip State Machine*) renders the
   narrative's tool calls as full rich chips — solid border with the ✓ / ✗
   glyph, merged success/failure header, and a per-kind collapsible detail
   panel — visually identical to the chips the same turn would produce on the
   no-result assistant path. When `raw_json` is unavailable (legacy records, or
   `extractAssistantChipEvents` returns an empty event stream), the helper
   degrades to the bracket-only `renderToolMarkers` form. The resulting
   narrative nodes are rendered at the top of the bubble. The same helper MUST
   be reused by every structured-renderer narrative slot (`renderDiscoveryAssistant`,
   the shared `makeStructuredAssistantRenderer`, and the no-result inline path)
   so the three call sites never structurally drift; an `assistant` turn that
   carries both narrative tool calls and a final result JSON renders its
   narrative chips identically to a turn whose body is thinking process only.
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
   the assistant's single "View raw" fold (built by `makeAssistantRawToggle`; see
   *Three-Tier Progressive Disclosure*) sitting directly below the rendered
   result rather than shown as a row-level always-visible button, so a developer
   can still inspect the unrendered string when debugging. This assistant fold
   is its own dedicated entry — it falls back to the unrendered `content`
   literal when no raw_ndjson / raw_json payload is present — and is NOT nested
   inside any "Expand all" wrapper (there is no assistant-side "Expand all").

The registry MUST remain open for future step types without re-architecting
the dispatch path. Every step type listed above MUST have a registered
renderer so an assistant turn defaults to structured fields rather than a raw
```` ```json ```` blob; a step type with no registered entry — or a registered
renderer that cannot parse the body — degrades gracefully and no assistant
message is ever dropped.

**Generic-fallback rendering for unregistered step types.** When the
`STEP_ASSISTANT_RENDERERS` lookup misses (or its registered renderer returns no
structured result) **and** the body's last result-region produces a plain
`step.outputs`-shaped dict (per the same multi-region collection path described
below), the renderer MUST route that dict to a **generic field-style fallback
renderer** that is the web counterpart of the CLI's
`step_renderers._default_render` — it MUST NOT dump the entire dict as a bare
```` ```json ```` fence inside the assistant bubble. The generic fallback walks
the dict in declaration order, emits one `key: value` row per field, previews
long string values (truncated to ~200 characters with a `(N chars)` suffix
matching the CLI threshold of ~300 chars), and expands nested dicts at least
one level by indentation. The narrative section is still rendered through the
shared `renderNarrativeNodes` helper above the field rows, and the assistant's
single "View raw" fold (per *Three-Tier Progressive Disclosure*) still carries
the unmodified original record. This generic fallback applies **only** to the
step.outputs dict the dispatch path hands to the assistant renderer — it MUST
NOT alter the rendering of ```` ```json ```` code fences embedded inside
free-form assistant prose (e.g. an assistant turn whose deliverable is a JSON
example or whose narrative quotes a JSON snippet), which continue to render
through the standard markdown path. Step types that are never registered in
`STEP_ASSISTANT_RENDERERS` (e.g. `confirm`, `project_summary`, and any future
step the orchestrator emits without a dedicated renderer) therefore surface
their `step.outputs` as field rows aligned with the CLI display, not as a raw
JSON dump.

When neither a registered renderer nor the generic fallback can extract a
structured-outputs dict (e.g. the body is pure prose, or contains only
intermediate tool-call JSON), the renderer MUST still fall back to the shared
`renderToolMarkers` + markdown / foldable path so no assistant message is ever
dropped.

**Plan proposal / design field expansion.** The `plan` step type's
`STEP_ASSISTANT_RENDERERS` / `STEP_REPORT_RENDERERS` entry MUST expand its
inner `proposal` and `design` dicts into the same field-level structure the
CLI `display.render_proposal` / `display.render_design` panels show — at least
`summary`, `files_to_modify`, `files_to_create`, and `rationale` for
`proposal`, and `overview`, `components`, `interfaces`, and `key_decisions`
(or the equivalent `decisions` field) for `design`. These nested dicts MUST
NOT be re-dumped as a single ```` ```json ```` blob inside the plan report or
the plan assistant bubble; unknown / unenumerated fields fall back through the
same generic field-style rendering so nothing inside the nested dicts is
dropped.

**Result-vs-tool-call identification.** A structured renderer MUST surface a
Layer-1 result (and thereby let *Three-Tier Progressive Disclosure* render the
narrative + structured result as the visible default, with only the turn's
original record folded behind the single "View raw" entry) **only when the turn
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
collect **every** parseable JSON region the body contains — both fenced
```` ```json ... ```` blocks and bare top-level JSON objects/arrays that appear
without an outer fence — and select the **last** region satisfying the result
predicate as the turn's result (a result conventionally follows the tool calls
that produced it). The Layer-1 narrative is then the body with **all** JSON
regions removed (not just the chosen one), so intermediate tool-call JSON never
leaks into the clean default view while the full original body — every JSON
region included — stays reachable through the assistant's single "View raw" fold.
When **no** region satisfies the result predicate — including a turn carrying
two or more tool-call JSON segments — the renderer MUST return no result so the
caller keeps the thinking process inline per *Three-Tier Progressive
Disclosure*, never folding it into any empty toggle.

**JSON-string-aware region collection.** Region collection MUST be performed by
a JSON-string-aware brace/bracket-balanced scanner that walks the body
character-by-character, NOT by a fence-regex partitioner. The scanner enters a
JSON-string state on an unescaped `"` and, while inside that state, treats
every `{`, `}`, `[`, `]`, ```` ` ```` (single or triple), and fence-like marker
as inert literal content — they MUST NOT open a new region, close an open
region, or otherwise affect partitioning. Outside string state, the scanner
balances `{`/`}` and `[`/`]` to delimit candidate regions and validates each
candidate via the lenient JSON parser before registering it. Fence markers
(```` ```json ```` / ```` ``` ````) are opportunistically absorbed as a
region's optional decorative shell when one wraps a registered region, so the
narrative stays clean; they are NOT used as the primary partitioning mechanism.
This is required because fence boundaries are a character-layer construct while
JSON string boundaries are a lexical-layer construct: any approach that
partitions by fence regex first and then tries to repair edge cases will
mis-classify at least one of the trigger shapes enumerated below.

**Structural robustness invariant.** The combined collection + predicate +
narrative-removal pipeline MUST correctly extract the final result JSON (when
one exists) and produce a JSON-stripped narrative (when none exists) for every
one of the following trigger shapes, without any shape regressing the others:
(1) trailing bare JSON with no outer ```` ```json ```` fence; (2) a trailing
```` ```json ```` fence-wrapped JSON; (3) prose containing one or more markdown
code fences whose bodies are non-JSON; (4) prose containing inline backticks or
unpaired ```` ``` ```` triple-backtick runs; (5) a JSON string field value that
literally contains a ```` ``` ```` triple-backtick or other fence-like
substring; (6) a single turn carrying multiple JSON regions (e.g. several
tool-call JSON segments followed by a final result JSON); (7) a bare
(unfenced) substantive JSON **object** that stands at block-start but is
**followed by non-whitespace text** — trailing prose, a second narrative
paragraph, or a further trailing payload — rather than sitting at the end of
the body (the only previously-collected bare shape). Shape (7) was the
historical blind spot: the region-registration gate previously admitted a bare
JSON region only when it was fence-wrapped, immediately followed by a ```` ``` ````
fence, or positioned at the body's tail (followed only by whitespace), so a
bare result object trailed by any non-whitespace text went unregistered →
`extractResultJson` returned null → the structured fields (`content`,
`refined_description`, `questions`, and their per-step equivalents) were
silently dropped from the Layer-1 view and survived only behind "View raw". The
gate MUST therefore register a bare JSON region that stands at block-start even
when followed by non-whitespace text, **provided** the region is a substantive
object (a non-empty dict carrying at least one key); the block-start +
substantive-object guard keeps the gate from admitting inline tool-marker JSON
or stray fragments (`[0]`, a bare array, `{}`) that prose may contain, so those
are still left unregistered and never wrongly stripped from the narrative.
This shape — and the broader case of a multi-block assistant message that
interleaves `tool_use` / `tool_result` blocks before finally emitting the
structured result — became frequent only after the v10.0–v10.3 agent-runner /
live-append rework reshaped how `discovery` assistant content is assembled and
streamed; the shared rendering pipeline must recognise it for every step type
in `STEP_RESULT_FIELDS`, not `discovery` alone. When the assistant
text contains no JSON region at all (pure prose), the entire body MUST render
as markdown without raising and without producing an empty result card.

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
  the assistant's single "View raw" fold

#### Scenario: Bare JSON followed by trailing non-whitespace text still renders structured fields
- **GIVEN** an assistant turn whose body carries a bare (unfenced) substantive
  JSON object at block-start — a non-empty dict carrying at least one of its
  step type's result fields (e.g. a `discovery` `{"content", "refined_description",
  "questions"}`) — that is **followed by non-whitespace text** rather than
  sitting at the body's tail (trailing prose, a second narrative paragraph, or a
  further trailing payload), optionally produced by a multi-block message that
  interleaved `tool_use` / `tool_result` blocks before emitting the result
- **WHEN** the structured renderer evaluates the turn
- **THEN** the region-registration gate registers the bare object even though
  non-whitespace text follows it, `extractResultJson` selects it as the turn's
  result, and the default view renders the structured fields (`content` markdown,
  the Proposed Task Description card, the `questions` list — and the per-step
  equivalents) in full rather than dropping them and surfacing them only behind
  "View raw"
- **AND** the Layer-1 narrative has every JSON region removed, with the trailing
  non-whitespace text preserved as narrative around the stripped region
- **AND** the same block-start + substantive-object guard leaves inline
  tool-marker JSON and stray non-result fragments (`[0]`, a bare array, `{}`)
  unregistered, so they are never wrongly stripped from the narrative
- **AND** this behavior holds uniformly for every step type whose assistant
  message flows through the shared `STEP_RESULT_FIELDS` collection path, not for
  `discovery` alone

#### Scenario: Bare JSON with embedded markdown fence still renders structured fields
- **GIVEN** an assistant turn whose body is a bare JSON object (no outer
  ```` ```json ```` wrapper) carrying one of its step type's result fields,
  where one of the object's string field values literally contains a markdown
  ```` ``` ```` code fence whose body is prose (not JSON) — e.g. a discovery
  `content` field whose markdown embeds a prompt example code block
- **WHEN** the structured renderer evaluates the turn
- **THEN** the JSON-string-aware scanner treats the embedded fence as inert
  string content, the bare object is still collected as a JSON region and
  chosen as the turn's result
- **AND** the default view renders the structured fields (e.g. the discovery
  `content` markdown plus the Proposed Task Description card) rather than
  degrading to a raw-JSON dump
- **AND** this behavior holds uniformly for every step type whose assistant
  message flows through the shared multi-region collection path (`discovery`,
  `analyze`, `plan`, `implement`, `verify_spec`, …), not for `discovery` alone

#### Scenario: JSON string value containing a literal triple-backtick is still extracted
- **GIVEN** an assistant turn whose body is a single bare JSON object carrying
  one of its step type's result fields, where one string field value literally
  contains a ```` ``` ```` triple-backtick run (e.g. an example prompt the LLM
  is quoting back inside the `content` field)
- **WHEN** the structured renderer evaluates the turn
- **THEN** the JSON-string-aware scanner does not interpret the in-string
  triple-backticks as a markdown fence boundary, the bare object is collected
  as one balanced JSON region, and the result predicate selects it
- **AND** the default view renders the structured fields rather than dumping
  the raw JSON literal

#### Scenario: Prose with inline backticks or unpaired triple-backticks does not break extraction
- **GIVEN** an assistant turn whose narrative prose contains inline single
  backticks (e.g. `` `foo()` ``) and/or an unpaired ```` ``` ```` triple-backtick
  run that never closes, followed by a final result JSON region (either bare or
  fence-wrapped) carrying at least one of its step type's result fields
- **WHEN** the structured renderer evaluates the turn
- **THEN** the unpaired / inline backticks do NOT cause the scanner to
  misclassify the surrounding text as a fence body, the final result JSON
  region is still collected and chosen
- **AND** the Layer-1 narrative renders the prose as markdown with the result
  JSON removed, without dropping any prose content

#### Scenario: Pure prose with no JSON region renders as markdown without an empty card
- **GIVEN** an assistant turn whose body contains no JSON region at all (free
  markdown prose, possibly with inline backticks or non-JSON fenced code blocks)
- **WHEN** the structured renderer evaluates the turn
- **THEN** the renderer returns no result so the caller keeps the thinking
  process inline per *Three-Tier Progressive Disclosure*
- **AND** no empty result card is rendered and no exception is raised

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

#### Scenario: Structured-renderer narrative tool calls render as rich chips when raw_json is available
- **GIVEN** an assistant turn routed to a structured renderer (e.g. a
  `discovery` turn carrying `refined_description`, or any `analyze` / `plan` /
  `implement` / `test` / etc. turn whose body carries a final result JSON)
  whose body also carries narrative text with bracketed tool-call markers and
  whose `norm.raw.raw_json` contains paired `tool_use` / `tool_result` content
  blocks for those calls
- **WHEN** the turn is rendered in `#flow-view`
- **THEN** the narrative section above the structured-result fields renders
  each tool call as a full rich chip — solid border with the ✓ / ✗ glyph,
  the merged success/failure header, and a per-kind collapsible detail panel
  — produced by the same chip pipeline that drives the no-result assistant
  path (`extractAssistantChipEvents` → `renderChipEvents`), via the shared
  `renderNarrativeNodes` helper
- **AND** the chips are visually indistinguishable from the chips the same
  turn would produce if it had no result JSON, so the narrative tool-call
  rendering does not visually degrade just because the turn also has a result

#### Scenario: Unregistered step type renders step.outputs as generic field rows
- **GIVEN** an assistant turn for a step type that has NO entry in
  `STEP_ASSISTANT_RENDERERS` (e.g. `confirm`, `project_summary`, or any step
  the orchestrator emits without a dedicated renderer) whose body carries a
  `step.outputs`-shaped result dict
- **WHEN** the turn is rendered in `#flow-view`
- **THEN** the dispatch routes the result dict to the generic field-style
  fallback renderer (the web counterpart of CLI
  `step_renderers._default_render`) which emits one `key: value` row per field
  in declaration order, previewing long string values (truncated with a
  `(N chars)` suffix) and indenting one level into nested dicts
- **AND** the assistant bubble does NOT contain a single bare ```` ```json ````
  fence dumping the entire dict as raw JSON
- **AND** the narrative section above the field rows still renders through the
  shared `renderNarrativeNodes` helper and the assistant's single "View raw"
  fold still carries the unmodified original record

#### Scenario: Embedded ```json code fence in assistant prose still renders as markdown
- **GIVEN** an assistant turn (registered renderer or not) whose narrative
  prose embeds a ```` ```json ... ```` code fence that is part of the
  deliverable text itself (e.g. a JSON example the assistant is showing) and
  is NOT the dispatch path's step.outputs dict
- **WHEN** the turn is rendered
- **THEN** that embedded fence continues to render through the standard
  markdown path — the generic field-style fallback only re-routes the
  step.outputs dict the dispatch layer hands the assistant renderer, not
  every JSON code block that happens to appear inside narrative prose

#### Scenario: Plan proposal and design dicts render as field rows, not a JSON blob
- **GIVEN** a `plan` step's assistant turn or report card carrying a
  `proposal` dict with at least `summary` / `files_to_modify` /
  `files_to_create` / `rationale` and a `design` dict with at least
  `overview` / `components` / `interfaces` / `key_decisions`
- **WHEN** the plan renderer expands its inner `proposal` and `design`
- **THEN** each nested dict is rendered as a field-level section mirroring
  the CLI `display.render_proposal` / `display.render_design` panels — each
  enumerated field surfaces as its own labeled row (or sub-card), in CLI
  field order
- **AND** neither nested dict is re-dumped as a single ```` ```json ```` blob
- **AND** unknown / unenumerated fields inside the nested dicts fall through
  the same generic field-style rendering so no field is dropped

#### Scenario: Structured-renderer narrative falls back to bracket chips without raw_json
- **GIVEN** an assistant turn routed to a structured renderer whose body
  carries narrative with bracketed tool-call markers, but whose
  `norm.raw.raw_json` is absent, non-iterable, or yields no chip events
- **WHEN** the turn is rendered
- **THEN** the `renderNarrativeNodes` helper degrades to the bracket-only
  `renderToolMarkers` rendering for the narrative section so the legacy
  in-flight bracket chip is still shown, the structured-result fields below
  it still render, and no exception is raised

### Requirement: Live Per-Turn Stream Accumulation

While a flow is running, the daemon pushes several streaming progress records
(`type: "stream_progress"` or `partial: true` — thinking text, `🔧 Read`,
`✅ Read ✓`, etc.) for an LLM turn *before* that turn's final (non-partial)
assistant result arrives. The running-flow conversation MUST render all of a
single turn's streaming fragments into **one accumulating assistant bubble**:
from a turn's first streaming fragment, exactly one assistant bubble (one role
head + one timestamp) appears, and every later same-turn fragment is appended
into that same bubble — NOT rendered as its own freshly-headed bubble per
fragment. This makes the live (in-progress) view consistent with the collapsed
form the turn settles into once its final result lands.

**Grouping unit.** The set of fragments merged into one accumulating bubble
MUST be exactly the batch that the turn's final result later collapses into —
i.e. grouped by turn (`step_id` + `attempt`, the existing `progressTurnKey`).
Distinct turns within the same step (a retry / fix loop / discovery
continuation that resets `attempt` while reusing `step_id`) MUST each get their
own separate accumulating bubble.

**Segment-key determination (data-driven, not DOM-driven).** Merge membership
MUST be computed by a pure function (`partialSegments`) over the **full ordered
records array**, NOT by inspecting the DOM at render time to decide whether a
fragment belongs to an existing bubble. Each partial record's stable *segment
key* is `progressTurnKey(norm) + "#seg" + N`, where `N` is the count of FINAL
(non-partial) assistant results sharing the same `progressTurnKey` that appear
strictly before that record; non-partial and non-assistant records have a null
key. The `#segN` suffix separates multi-round turns: a later round's partials
follow the earlier round's final and therefore land at a higher final count,
forming a distinct segment. This mirrors `markSupersededProgress`'s positional
supersede rule (a final supersedes only the partials before it that share its
key), so the merge grouping and the supersede grouping never disagree. The
DOM-free approach is required because `removeSupersededProgress` runs only at
the end of `addConversationRecords`: during a full rebuild a closed segment's
stale bubble is still present in the DOM mid-traversal, so a DOM-membership
check would wrongly append the next round's fragments into the previous round's
leftover bubble. It also keeps the incremental-append path and the
full-rebuild path producing identical results.

**Timestamp tracking.** The accumulating bubble's head MUST display the
timestamp of the **latest** streaming fragment received for that turn, updating
to the newest as each new fragment arrives — consistent with the final
collapsed form. Its sort key (`__convTs` / `__convIdx`) MUST likewise track the
latest fragment so the turn's eventual final bubble sorts stably after it.

**Content layout during accumulation.** The accumulating bubble's content
arrangement MUST match the final no-result assistant form: it reuses the shared
`renderAssistantProcessInline` helper (the `assistant-process-inline` container
with `renderToolMarkers`), the same path the final no-result assistant turn
takes, so the live form and the final form never structurally drift.

**Tool-event content format.** Beyond structural reuse, the *text content* of
each streaming fragment that represents a tool event (`tool_use` /
`tool_result` / `tool_error` / stream-level `error`) MUST also match the final
form's bracket-marker grammar — `[<tool_name>: <detail>]` for in-flight tool
uses, `[<tool_name> ✓ <merged-header>]` for successful terminal records, and
`[<tool_name> ✗ <error_preview>]` (or `[Tool error: <preview>]` when the tool
name is unknown) for failures — so the frontend's single bracket-aware parser
(see *Tool Call Chip State Machine*) produces identical `.tool-marker` /
`.tool-chip` boxes for live fragments and for the final assistant turn. The
frontend MUST NOT carry a second emoji-prefixed parsing path for live
fragments; the upstream `StreamJSONTracker` writes the bracket-marker text
into the `stream_progress` payload while keeping its own CLI stdout
emoji-prefixed, per the `llm-caller` *Streaming NDJSON Output Display*
requirement. The bracket grammar is parsed by a tolerant `parseToolBracket`
that recognizes the three head forms `[Name: …]`, `[Name ✓ …]`, and
`[Name ✗ …]` so a result-side bracket that carries no `:` separator still
contributes its header text to the chip rather than being dropped (the
previous naive `:` split treated any colonless bracket as a name-only zombie
chip — this is forbidden). The running-flow console therefore relies on the
producer side for byte-identical marker text between live and final views;
no client-side reformatting is required.

**Live agent / model badge.** From its very first fragment the accumulating
bubble MUST display the agent badge for the turn, reusing the same
`renderAgentBadge` / `formatAgentBadgeText` helpers and unobtrusive small-print
styling as the final assistant bubble (see *Per-Step Report Cards*). The badge
is seeded from the first fragment's normalized `agentName` so the operator sees
which agent is producing the reply the moment streaming begins, rather than only
after the final result lands. When a later same-turn fragment first carries a
`modelName`, the existing badge is upgraded **in place** to the `agent · model`
form — without opening a new headed bubble and without reflowing the accumulated
content. Consistent with the final form, when a fragment carries no `agentName`
no badge, label, or placeholder of any kind is rendered, so legacy records
lacking these fields reflow exactly as before. This relies on the producer side
stamping `agent_name` on every `stream_progress` record (and `model_name` once
resolved), per the `llm-caller` *Streaming NDJSON Output Display* requirement.

**Final state is unchanged.** This requirement governs only the *in-progress*
(pre-final) intermediate rendering. When the turn's final (non-partial) result
arrives, the accumulating bubble is superseded and the turn collapses to the
existing final rendering (narrative folded above + structured result, per
*Three-Tier Progressive Disclosure* and *Structured-JSON Assistant Rendering*)
with NO change to any final-state behavior or appearance. The semantics and
signatures of `markSupersededProgress`, `removeSupersededProgress`,
`insertBubbleSorted`, and `rebuildStepHeaders` are unchanged — a closed
segment's accumulating bubble is still removed wholesale when the final result
arrives, because every member index of the closed segment (including the
latest) is superseded.

**Scope — history / playback too.** Where the history / playback view shares
the same rendering mechanism, this behavior applies there as well. History
records normally carry no intermediate partial state; but when a run was
interrupted mid-turn and left residual partials behind, those residual
fragments MUST likewise merge into a single accumulating assistant bubble, not
one bubble per fragment.

#### Scenario: Single turn's fragments accumulate into one bubble
- **GIVEN** a running flow whose current LLM turn has pushed several streaming
  fragments (`partial: true`) sharing one `progressTurnKey`, with no final
  result yet
- **WHEN** the conversation is rendered in `#flow-view`
- **THEN** exactly one assistant bubble (one role head + one timestamp) is
  shown for that turn
- **AND** every fragment after the first is appended into that same bubble
  rather than opening a new headed bubble per fragment

#### Scenario: Different rounds of the same step stay separate
- **GIVEN** a step whose `step_id` is reused across two turns (e.g. a discovery
  continuation or fix-loop re-run that reset `attempt`), where the first
  round's partials are followed by a final result and then the second round's
  fresh partials arrive
- **WHEN** the conversation is rendered
- **THEN** `partialSegments` assigns the two rounds different segment keys
  (the second round lands at a higher final count, `#seg1`)
- **AND** each round is rendered as its own separate accumulating bubble

#### Scenario: Head timestamp tracks the newest fragment
- **GIVEN** an accumulating bubble that has already absorbed an earlier
  fragment
- **WHEN** a newer same-turn fragment arrives
- **THEN** the bubble head's displayed timestamp updates to the newest
  fragment's timestamp
- **AND** the bubble's sort key advances to the newest fragment so a later
  final bubble sorts stably after it

#### Scenario: Final result collapses the accumulating bubble unchanged
- **GIVEN** an accumulating bubble holding a turn's streaming fragments
- **WHEN** that turn's final (non-partial) assistant result arrives
- **THEN** the accumulating bubble is superseded and removed wholesale, and the
  turn renders in the existing final form (narrative folded above + structured
  result) with no change to final-state behavior or appearance

#### Scenario: Incremental append and full rebuild agree
- **GIVEN** the same record stream delivered either as one full render pass or
  as a sequence of incremental appends
- **WHEN** the conversation is built each way
- **THEN** both paths produce the same single accumulating bubble per turn,
  because merge membership is computed by `partialSegments` over the full
  records array rather than from the live DOM

#### Scenario: Live tool events render as bracket markers identical to final state
- **GIVEN** a running flow whose current LLM turn streams `tool_use`,
  `tool_result`, and `tool_error` fragments (delivered as
  `stream_progress` / `partial: true` records by the daemon)
- **WHEN** the conversation is rendered in `#flow-view`
- **THEN** each tool-event fragment's text matches the `TOOL_MARKER_RE`
  bracket-marker pattern and is rendered as a `.tool-marker` box inside the
  accumulating assistant bubble, visually identical to the boxes the final
  assistant turn shows after the streaming completes
- **AND** no emoji-prefixed text (`🔧`, `✅`, `❌`) appears in the rendered
  live bubble as unboxed markdown — the live and final views share one
  marker grammar with no second parsing path

#### Scenario: Accumulating bubble shows the agent badge from the first fragment and upgrades to agent · model
- **GIVEN** a running LLM turn whose first streaming fragment carries
  `agentName = "dclaude"` with no `modelName`, and a later same-turn fragment
  that carries both `agentName = "dclaude"` and `modelName = "claude-opus-4-8"`
- **WHEN** the conversation is rendered in `#flow-view`
- **THEN** from the first fragment the accumulating assistant bubble shows the
  agent badge `dclaude`
- **AND** when the model-bearing fragment arrives the badge upgrades in place to
  `dclaude · claude-opus-4-8` within the same bubble — no new headed bubble is
  opened and the accumulated content does not reflow
- **AND** a turn whose fragments carry no `agentName` renders no badge or
  placeholder, exactly as before these fields existed

#### Scenario: History residual partials merge into one bubble
- **GIVEN** a history / playback record stream from a run interrupted mid-turn,
  leaving residual `partial` fragments for that turn with no final result
- **WHEN** the conversation is rendered through the shared mechanism
- **THEN** those residual fragments merge into a single accumulating assistant
  bubble, not one bubble per fragment

#### Scenario: Worktree flow's first discovery reply streams live in full
- **GIVEN** a `se3 run --worktree` flow being watched live (not played back
  after the fact), where the worktree flow's running history is written under
  the worktree's own directory and its inclusion in the daemon's
  runtime-observable set is gated by `_active_worktree_run_roots()` on the
  `is_worktree_mode` flag of the flow's `engine.json`
- **AND** the worktree flow's `engine.json` is created carrying
  `is_worktree_mode=True` *before* the discovery step's first LLM call (so the
  observability gate is already satisfied at the discovery startup window, with
  no blind spot — see the `flow-engine` worktree-creation persistence scenario)
- **WHEN** the discovery step publishes the task and its first assistant reply
  arrives
- **THEN** that first reply's full content — both the thinking fragments and the
  final non-partial result — accumulates into one live bubble and is displayed
  in real time, and every subsequent same-flow message continues to stream live
- **AND** the live view matches what a post-merge read-back of the same flow
  would show, with no "only thinking, never the result, and nothing after the
  first reply" truncation

### Requirement: Tool Call Chip State Machine

Every tool call observed in a running-flow conversation — whether it is being
streamed live as `stream_progress` fragments or read back from a settled
assistant turn via `raw_json` — MUST be rendered as **exactly one chip** that
progresses through a small in-place state machine, never as two adjacent
sibling chips (one for `tool_use`, one for `tool_result`). The chip's
identity key is the originating Anthropic `tool_use_id` published by the
producer side on every `tool_use` / `tool_result` / `tool_error` event (see
the `llm-caller` *Streaming NDJSON Output Display* requirement's per-chip
extension fields). A per-bubble chip registry — scoped to the accumulating
assistant bubble of a single turn (`(turn_key, tool_use_id)`, where
`turn_key` is the `progressTurnKey` defined in *Live Per-Turn Stream
Accumulation*) — pairs the later result event back to the chip created by
the earlier use event so the upgrade happens in place on the existing DOM
node.

**Three visual states.** Each chip has exactly three terminal-visual states,
and one chip transitions through them at most once:

- **in-flight** — created on the `tool_use` event; styled with a dashed
  border and a gray accent and no ✓ / ✗ glyph; the chip header reads
  `[<Tool>: <use summary>]` (the `format_tool_chip_in_flight_header` form
  computed from the use input alone).
- **success** — reached on a non-error `tool_result`; the chip is upgraded
  in place to a solid green border with the ✓ glyph; its header is replaced
  by the merged success header `[<Tool> ✓ <use-summary> · <result-summary>]`
  (the `format_tool_chip_header` output) so the chip carries both the input
  and the result reading in one line.
- **failure** — reached on a `tool_result` whose `is_error` is `true`, or on
  a stream-level `error` that pairs to an open in-flight chip; the chip is
  upgraded in place to a solid red border with the ✗ glyph and the header
  becomes `[<Tool> ✗ <error preview>]` (or `[Tool error: <preview>]` when
  the tool name is unknown).

A chip MUST go from in-flight to success **or** from in-flight to failure;
it MUST NOT regress, and a second terminal event for the same `tool_use_id`
is a no-op (idempotent upgrade).

**Detail panel.** Each chip carries an attached detail panel rendered from
the terminal event's `tool_detail` payload (the structured dict produced by
`tool_formatters.build_tool_detail_payload`, keyed by a `kind` field —
`edit_diff` / `write_full` / `write_diff` / `read_text` / `bash_output` /
`grep_matches` / `glob_matches` / `text`). Per-kind renderers cover at
minimum: Edit / Write diffs rendered as unified diff with a fixed-width
line-number gutter and color-aligned `-` / `+` / hunk / context lines
matching the CLI `display.render_diff` palette; Write-of-new-file rendered
as the full file body with line numbers; Read rendered as the result text
with a line-number gutter starting from the request's `offset + 1`; Bash
rendered as the command line plus separated stdout / stderr blocks; Grep /
Glob rendered as a match list; and the generic `text` kind rendered as a
preformatted text block. Long payloads MAY be truncated against the shared
`TOOL_DETAIL_PAYLOAD_MAX_CHARS` upper bound from `engine/truncation.py` and
display a `… (N more lines truncated)` tail; the runtime MUST NOT introduce
a new remote lazy-load endpoint for the panel body.

**Default fold state.** The detail panel is **folded by default** for chips
in the in-flight and success states (the user clicks the chip head to expand
it). For chips in the failure state the detail panel is **expanded by
default**, so the failure reason and any error output are immediately
visible without an extra click. Fold-state changes follow the same
`scrollIntoView({block: "nearest"})`-on-expand, no-scroll-on-collapse
behavior used elsewhere in the view.

When a chip's detail panel is folded, the **entire detail wrapper**
(`.tool-marker-details`) MUST be collapsed out of layout (`display: none`),
not merely its inner body (`.tool-marker-details-body`). Hiding only the inner
body leaves the full-width wrapper (`flex-basis: 100%`) occupying a blank row in
the flex-wrap chip container and contributes its own `margin-top`, producing a
visible empty band below the folded chip. Collapsing the whole wrapper removes
that empty row and stray margin so a folded chip stays a clean, single-row
affordance; the always-visible toggle button is a sibling of the wrapper (not a
child) and therefore remains visible and operable while the wrapper is hidden.

**Toggle-button placement in the chip head.** When a chip carries a detail
payload, its expand/collapse toggle button MUST sit **inline with the chip
head** as the rightmost sibling of the name / header / glyph nodes
(right-aligned via `margin-left: auto` or an equivalent rule), NOT in a
secondary row below the head separated by a dashed border. The detail body
panel still wraps onto its own row (e.g. via `flex-basis: 100%`) when
expanded, but the row carrying the detail body MUST NOT introduce a top
border / top padding visual divider above the toggle. A chip whose terminal
event carries no detail payload MUST NOT render a toggle button at all (the
chip head stays a single uncluttered row).

**Single rendering pipeline for live and final views.** The chip state
machine runs over both data sources without branching: the live path
consumes `stream_progress` records (each carrying `tool_use_id` /
`is_error` / `tool_detail` per the `llm-caller` *Streaming NDJSON Output
Display* requirement), and the final path consumes the assistant turn's
`raw_json` by pairing each inner `tool_use` content block with its
matching `tool_result` block (by `tool_use_id`) and feeding the same
`(in-flight event, terminal event)` pair through the same registry +
upgrade helpers. Consequently `extractAssistantText` MUST NOT silently
skip `tool_result` blocks during the final-view extraction the way the
legacy renderer did; instead, the chip pairing is the canonical join, and a
turn's final-view DOM for the same tool call ends up structurally
equivalent to its live-view DOM (same chip, same header, same detail).

**Forbidden legacy fallback.** The previous "result-only" fallback path
that emitted a standalone `[<Tool> ✓ …]` chip without a paired in-flight
chip MUST be removed; no zombie chip path is preserved as a safety net.
This makes mis-paired data fail loudly during development rather than
silently regrowing the duplicate-chip bug.

**Legacy / no-id degradation.** Historical jsonl records written before the
single-chip protocol existed may contain bracketed tool markers that lack
the `tool_use_id` extension field (the producer-side per-chip fields are
optional in `chat_history.record_stream_progress`). For these records the
frontend MUST degrade gracefully: it parses the bracket header with
`parseToolBracket` and renders a single chip in the in-flight visual state
(dashed / gray), with no detail panel and no terminal upgrade — never two
adjacent zombie chips, never a thrown exception. New records with the
`tool_use_id` field are unaffected by this fallback.

#### Scenario: tool_use creates a single in-flight chip
- **GIVEN** a live tool call whose `tool_use` event has arrived (with a
  `tool_use_id`) but whose `tool_result` has not
- **WHEN** the running-flow conversation is rendered in `#flow-view`
- **THEN** the chip registry contains exactly one chip keyed by that
  `tool_use_id`, in the **in-flight** state — dashed border, gray accent,
  no ✓ / ✗ glyph
- **AND** the chip's header reads `[<Tool>: <use-summary>]` (the in-flight
  form), the detail panel is folded by default, and no second sibling chip
  exists for the same tool call

#### Scenario: Non-error tool_result upgrades the chip to success in place
- **GIVEN** an in-flight chip already on screen for a tool call
- **WHEN** the matching non-error `tool_result` arrives (same `tool_use_id`,
  `is_error = false`)
- **THEN** the **same** chip DOM node is upgraded in place to the **success**
  state — solid green border, ✓ glyph — and its header is replaced by the
  merged `[<Tool> ✓ <use-summary> · <result-summary>]` form returned by
  `format_tool_chip_header(...)`
- **AND** the detail panel is populated from the terminal event's
  `tool_detail` payload, remains **folded by default**, and the registry
  still has exactly one chip for that `tool_use_id` (no sibling chip is
  added)

#### Scenario: Error tool_result upgrades the chip to failure with detail expanded
- **GIVEN** an in-flight chip already on screen for a tool call
- **WHEN** the matching `tool_result` arrives with `is_error = true` (or,
  equivalently, a stream-level `error` line that pairs to the open
  in-flight chip)
- **THEN** the same chip is upgraded in place to the **failure** state —
  solid red border, ✗ glyph — and its header becomes `[<Tool> ✗ <error
  preview>]` (or `[Tool error: <preview>]` when the tool name is unknown)
- **AND** the chip's detail panel is **expanded by default** so the error
  detail (rendered from the terminal event's `tool_detail` payload) is
  visible without an extra click

#### Scenario: Toggle button sits inline with the chip head
- **GIVEN** a settled tool chip in the success or in-flight state whose
  terminal event carries a non-empty detail payload
- **WHEN** the chip is rendered in `#flow-view`
- **THEN** the expand/collapse toggle button is a direct sibling of the chip
  head's name / header / glyph nodes, right-aligned at the end of the head
  row (e.g. via `margin-left: auto`)
- **AND** the toggle is NOT rendered in a secondary row below the head
  separated by a dashed top border / top padding divider
- **AND** when the user expands the detail panel, the detail body wraps onto
  its own row beneath the head (e.g. via `flex-basis: 100%`) without
  reintroducing the top-border divider

#### Scenario: Folded chip leaves no empty band below it
- **GIVEN** a tool chip in the in-flight or success state whose detail panel is
  folded by default
- **WHEN** the chip is rendered in the flex-wrap chip container
- **THEN** the entire detail wrapper (`.tool-marker-details`) is collapsed out of
  layout (`display: none`), not merely its inner body
  (`.tool-marker-details-body`)
- **AND** no blank full-width row and no stray `margin-top` band appears below
  the folded chip — the chip stays a clean single-row affordance
- **AND** the always-visible toggle button (a sibling of the wrapper) remains
  visible and operable, so expanding the chip still reveals the detail body

#### Scenario: Chip with no detail payload renders no toggle button
- **GIVEN** a tool chip whose terminal event produced no detail payload
- **WHEN** the chip is rendered
- **THEN** the chip head row contains no toggle button, keeping the chip a
  single uncluttered line

#### Scenario: Legacy no-id records degrade to a single in-flight chip
- **GIVEN** a settled assistant turn read from history whose body contains
  a bracketed tool marker (e.g. `[Read: path:0-200]` or a colonless
  `[Read ✓ path]`) but no `tool_use_id` extension field
- **WHEN** the running-flow conversation renders that turn through the
  shared chip pipeline
- **THEN** exactly one chip is rendered for the marker in the **in-flight**
  visual state (dashed / gray), with no detail panel and no terminal
  upgrade — never two adjacent zombie chips and never an unhandled
  exception
- **AND** newer records that carry `tool_use_id` continue to flow through
  the full in-flight → success / failure state machine in the same view

#### Scenario: Live and final views produce equivalent chip DOM for the same tool call
- **GIVEN** a single tool call observable in two ways — the live `stream_progress`
  pair (in-flight + terminal record, both carrying the same `tool_use_id`) and
  the same call's `tool_use` / `tool_result` blocks inside the settled assistant
  turn's `raw_json`
- **WHEN** the conversation is rendered first as the live progressive stream
  and then re-rendered from the settled `raw_json` (no live partials)
- **THEN** both renders feed the chip state machine via the same registry +
  upgrade helpers and produce structurally equivalent chip DOM — same chip
  identity, same final header, same per-kind detail panel content — so the
  view does not visually shrink or reorder when the live stream collapses
  into the final assistant turn

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
  implement, test, self_check, verify_spec, update_spec, spec_gate, commit,
  version_analyze, summarize, discovery — adding new step types adds a new
  renderer).
- Render structured output (markdown, tables, field lists) rather than
  re-dumping the raw JSON blob.
- **Be labeled as the step's result/summary, never as a bare step name.** The
  report card's heading MUST explicitly mark the card as that step's *result*
  or *summary* (e.g. a `· 结果` / `· 总结` suffix on the step name), so it can
  never be misread as the opening of a **new** step. This applies to **every**
  step type uniformly; in particular the `implement` summary card's previously
  ambiguous bare-step-name heading MUST carry the result/summary marker, and
  all other report cards are audited to eliminate the same ambiguity.

**`spec_gate` report card (summary, never a raw test dump).** The `spec_gate`
step (mechanism A — see `flow-engine` *Post-update_spec Spec Verification Gate*)
writes the full phase-2 `verdict.test_results` — including the raw pytest
stdout/stderr — into `step.outputs`, which `Step.to_dict()` forwards verbatim to
the web console. Without a dedicated entry in `STEP_REPORT_RENDERERS`, a
`spec_gate` report card would fall through to the generic field-style fallback
(`renderDefaultReport` → `renderGenericOutputs`) and dump the entire
`test_results` dict, raw output included, exactly the unfriendly behavior the
`test` step avoids by having its own summary-only renderer. The frontend
therefore MUST register a dedicated `spec_gate` report renderer
(`renderSpecGateReport`, the web counterpart of `step_renderers._render_spec_gate`)
that renders, in order: (1) the **gate conclusion** — a PASSED / FAILED status
from `outputs.gate_passed`, annotated as a no-op skip when `outputs.gate_skipped`
is true; (2) a **route annotation** when `outputs.gate_route` is `update_spec`
(invalid spec artifact, routed back to update_spec) or `implement` (a spec edit
broke a test, routed into the fix loop); and (3) when `outputs.test_results` is a
non-empty dict (the phase-2 re-test ran), the **same summary-only rendering the
`test` report card uses** (reusing `renderTestReport`'s summary path — overall
PASSED/FAILED, phase pass/fail counts, phase list, command) — the raw
pytest stdout/stderr is NEVER rendered. The no-op skip path and the
`update_spec` route (an invalid artifact caught before any re-test) carry no
`test_results`, so the card degrades to the gate-conclusion summary alone. To
make a `spec_gate` structured record be recognized as a step result, the
`STEP_RESULT_FIELDS` registry MUST enumerate `spec_gate`'s result keys (at least
`gate_passed`, `gate_route`, `gate_skipped`, `fix_needed`, `test_results`). The
data layer (the `spec_gate_handler`-written `step.outputs`) is left unchanged —
as with the `test` step, the raw content stays reachable through the record's
"View raw" affordance and is merely collapsed to a summary by default. Because
`spec_gate` is a pure program step (no LLM), it has a report card and a CLI
renderer but is NOT registered in `STEP_ASSISTANT_RENDERERS`.

Field parity also covers any **per-item ordinal numbering** a report card
renders. When a report renderer enumerates a list whose items are labeled with
a 1-based ordinal (e.g. the `implement` report's Summary section labeling each
implemented group `G1`, `G2`, … `Gn`), that numbering MUST match the CLI's
output for the same field — the CLI's `step_renderers.py` numbers these with
`enumerate(parts, 1)`. To make this hold, the shared list-rendering helper
(`reportList(items, formatItem)`) MUST invoke its `formatItem` callback with
the item's 0-based index as the second argument (`formatItem(item, index)`),
so a callback of signature `(item, index)` can compute `G${index + 1}` and
produce `G1…Gn`. The numbering MUST NOT render as `GNaN` (the symptom of a
callback receiving an `undefined` index and computing `undefined + 1`). When
the underlying collection is empty (e.g. `implemented_groups` is empty), the
numbering degrades to the plain ordinal (`1…n`) without group prefix, still
matching the CLI. Threading the index as the second positional argument is
side-effect-free for every other `reportList` caller, since callers that take
only the item ignore the extra argument.

To make this work end-to-end, the engine sink layer MUST also persist
`step_completed` events into the per-step jsonl files consumed by the daemon
history reader (e.g. via an unconditionally-subscribed `HistorySink` in
`src/se3/engine/sink.py` wired up from `src/se3/commands/run.py`), so that the
report card has access to the same `outputs` dict that the CLI Panel sees —
without breaking the CLI history viewer (`get_step_history` skips these
records on the CLI side).

**Per-step token-usage footnote and session badge.** When a `step_completed`
event's `outputs` carries a non-empty `token_usage` total (written by the engine
per *flow-engine: Step-Scoped Token Usage Aggregation*), the step's report card
MUST render a single low-key, small-print usage footnote (e.g. a
`.step-report__usage` row built by `buildStepUsageFootnote`) showing that step's
input / output tokens, the cache token breakdown, and the cost — styled so it is
unobtrusive and does not compete with the report's main content. In addition,
`#flow-view` MUST render a discreet session-total usage badge (e.g. a
bottom-corner `.flow-usage-badge` produced by `accumulateSessionUsage`) showing
the whole-flow running total, computed **client-side** by summing the
`token_usage` totals carried on the per-step records already pushed to the
frontend. No new daemon↔server protocol field is introduced — the per-step
usage rides the existing `step.outputs` / per-step jsonl stream. To keep the
client-side session total equal to the engine's authoritative
`session_token_usage`, the accumulation MUST de-duplicate records by full record
identity (e.g. a `recordKey`) so a step re-delivered across snapshots is counted
once. When no step has reported usage, the badge is absent or empty. The badge
reflects the final session total once the flow completes.

**Per-round usage on interactive assistant turns.** Beyond the per-step report
card, the running-flow console MUST also surface **per-round** token usage at
the tail of each interactive `assistant` turn (discovery clarification rounds,
confirm reviews, and any other interactive assistant bubble). Each such bubble
renders a small-print footnote showing both **this round's increment** and the
**cumulative** total — `本轮 X in / Y out · 累计 X in / Y out` — using the same
`formatTokenCount` number formatting as the per-step footnote, and only when the
record actually carries a non-empty `token_usage` (a round that made no LLM call
renders no footnote). The round increment is the per-call `token_usage` carried
on that assistant conversation record; the cumulative is computed **client-side**
as a running sum over the records sharing the same `step_id`, de-duplicated by
record identity so a record re-delivered across snapshots is counted once. The
data path introduces **no new daemon↔server protocol field**: the per-call
`token_usage` is attached to the assistant chat-history record (see `llm-caller:
Subprocess Invocation and History Recording`) and reaches the frontend through
the conversation record stream that `normalizeRecord` already parses (exposed as
`norm.tokenUsage`); the daemon passes the record through verbatim. Because
webui footnotes are already small-print and do not interrupt the conversation
continuity, the web surface needs no separate compact-form design — only the
per-round / per-step usage data presented as an unobtrusive footnote.

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
them through `render_step_output` would double the CLI output. The one
exception is discovery's cumulative usage: when the discovery step reaches a
terminal status with non-empty `token_usage`, `CliSink` renders a dim
whole-discovery cumulative usage line (`format_usage_line`) so the user sees
the total across all rounds including the confirmation round (which issues no
LLM call and would otherwise leave the cumulative undisplayed). `HistorySink`
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

#### Scenario: Report card is labeled as the step's result, not a new step
- **GIVEN** a `step_completed` event for any step type (notably `implement`)
- **WHEN** its report card is rendered in `#flow-view`
- **THEN** the card's heading marks it as that step's result/summary (e.g. a
  `· 结果` / `· 总结` suffix), not a bare step name
- **AND** no report card uses a heading that could be misread as the start of
  a new step region

#### Scenario: Implement Summary groups are numbered G1…Gn matching the CLI
- **GIVEN** an `implement` `step_completed` event whose `outputs` carries a
  non-empty `implemented_groups` list rendered by the report card's Summary
  section through `reportList` with a `(item, index)` callback computing
  `G${index + 1}`
- **WHEN** the report card is rendered in `#flow-view`
- **THEN** the Summary entries are labeled `G1`, `G2`, … `Gn` in order,
  matching the CLI's `enumerate(parts, 1)` output in `step_renderers.py`
- **AND** none of the entries renders as `GNaN`, because `reportList` passes
  each item's 0-based index as the second callback argument

#### Scenario: Empty implemented_groups degrades to plain ordinal numbering
- **GIVEN** an `implement` report whose `implemented_groups` collection is
  empty so the Summary callback's `G` prefix branch is not taken
- **WHEN** the report card is rendered
- **THEN** the Summary entries fall back to the plain 1-based ordinal
  (`1`, `2`, … `n`) with no group prefix, still matching the CLI
- **AND** the numbering is computed from the real index passed by `reportList`,
  never producing `NaN`

#### Scenario: reportList threads the item index without disturbing single-arg callers
- **GIVEN** the shared `reportList(items, formatItem)` helper used by multiple
  report renderers, some passing a single-argument `(item)` callback and one
  passing a two-argument `(item, index)` callback
- **WHEN** `reportList` iterates the items
- **THEN** it invokes `formatItem(item, index)` with a 0-based incrementing
  index for every item
- **AND** callers whose callback takes only the item are unaffected, because
  the extra positional argument is ignored

#### Scenario: spec_gate report card shows a gate summary, not a raw test dump
- **GIVEN** a `spec_gate` `step_completed` event whose `outputs` carries
  `gate_passed`, an optional `gate_route`, and a full `test_results` dict that
  includes the raw phase-2 pytest stdout/stderr
- **WHEN** the report card is rendered in `#flow-view`
- **THEN** the dispatch routes the step to the dedicated `renderSpecGateReport`
  (not the generic `renderDefaultReport` → `renderGenericOutputs` fallback),
  which renders the gate conclusion (PASSED/FAILED, any `update_spec` /
  `implement` route annotation) followed by the same summary-only test rendering
  the `test` report card uses (overall status, phase pass/fail counts, phase
  list, command)
- **AND** the raw pytest stdout/stderr does NOT appear in the card; it stays
  reachable only through the record's existing "View raw" affordance

#### Scenario: spec_gate report card renders only the conclusion when skipped or routed pre-test
- **GIVEN** a `spec_gate` event that returned `gate_skipped=true` (no spec
  change) or routed to `update_spec` on an invalid artifact, so `outputs` carries
  no `test_results`
- **WHEN** the report card is rendered
- **THEN** the card shows only the gate-conclusion summary (the no-op skip
  annotation or the `update_spec` route note) and invokes no test-summary
  rendering

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

#### Scenario: Report card shows a per-step token-usage footnote
- **GIVEN** a `step_completed` event whose `outputs` carries a non-empty
  `token_usage` total
- **WHEN** the report card is rendered in `#flow-view`
- **THEN** the card includes a single low-key, small-print usage footnote
  (`.step-report__usage`) showing that step's input / output tokens, the cache
  token breakdown, and the cost
- **AND** a step whose `outputs` has no `token_usage` renders no footnote

#### Scenario: Flow-view shows a client-accumulated session usage badge
- **GIVEN** several steps have reported per-step `token_usage` totals on their
  records pushed to the frontend
- **WHEN** `#flow-view` is rendered
- **THEN** a discreet session-total usage badge (`.flow-usage-badge`) shows the
  whole-flow running total summed client-side from those per-step totals
- **AND** the accumulation de-duplicates records by full record identity so a
  step re-delivered across snapshots is counted once, keeping the badge equal to
  the engine's authoritative `session_token_usage`
- **AND** once the flow completes the badge reflects the final session total

#### Scenario: Interactive assistant turn shows a per-round usage footnote
- **GIVEN** a discovery / confirm interactive `assistant` conversation record
  whose normalized `tokenUsage` (the per-call `token_usage` carried on the
  record) is non-empty
- **WHEN** the bubble is rendered in `#flow-view`
- **THEN** a small-print footnote at the tail of the bubble shows both this
  round's increment and the cumulative total
  (`本轮 X in / Y out · 累计 X in / Y out`) using the same `formatTokenCount`
  number formatting as the per-step footnote
- **AND** the cumulative is computed client-side as a running sum over records
  sharing the same `step_id`, de-duplicated by record identity
- **AND** an interactive assistant record with an empty / absent `token_usage`
  (a round that made no LLM call) renders no footnote
- **AND** the per-round usage data rides the existing conversation record stream
  (no new daemon↔server protocol field is introduced)

#### Scenario: Assistant bubble shows an agent/model badge when metadata is present
- **GIVEN** an `assistant` conversation record whose normalized fields include
  `agentName` (the configured agent name, e.g. `dclaude`) and optionally
  `modelName` (the actual model identifier, e.g. `claude-opus-4-8`)
- **WHEN** the assistant bubble is rendered in `#flow-view` or the history view
- **THEN** a compact badge is rendered at the top of the bubble (or at the header
  of the step report card for a `step_completed` event) showing the agent name,
  and, when `modelName` is also present, both separated by a middle-dot (e.g.
  `dclaude · claude-opus-4-8`)
- **AND** the badge is styled as an unobtrusive small-print label (not a
  full-width block or a prominent heading), so it does not compete with the
  conversation content

#### Scenario: No badge placeholder when agent/model metadata is absent
- **GIVEN** an `assistant` conversation record whose normalized fields carry no
  `agentName` and no `modelName` (e.g. a legacy record written before these
  fields existed)
- **WHEN** the bubble is rendered
- **THEN** no badge, label, or placeholder text of any kind is rendered — the
  bubble's content starts directly at the assistant text, exactly as before the
  metadata fields were introduced
- **AND** the absence of metadata does not shift or reflow the bubble layout

#### Scenario: normalizeRecord exposes agent/model metadata
- **GIVEN** a conversation record whose `message` envelope carries optional
  `agent_name` and/or `model_name` fields (as written by `record_prompt` /
  `record_response` per the `llm-caller` *Subprocess Invocation and History
  Recording* requirement)
- **WHEN** `normalizeRecord` processes the record
- **THEN** it exposes `agentName` and `modelName` on the normalized record object
  (defaulting to `null` / `undefined` when the fields are absent), so badge
  rendering logic can conditionally show or hide the badge without special
  envelope-unwrapping code

#### Scenario: Non-terminal round step data is compatible with the report card's token_usage source
- **GIVEN** a step (e.g. `self_check`) whose handler returned `REVISION_NEEDED` and
  whose `step.outputs["token_usage"]` was written by `state_machine.run_step`
  per the `flow-engine` *Step-Scoped Token Usage Aggregation* non-terminal-round
  behavior
- **WHEN** a `step_completed` or intermediate event referencing this step is
  rendered as a report card
- **THEN** the report card reads `token_usage` from `step.outputs` the same way it
  would for a terminal step — the data source is identical, and no special
  handling for `carried_token_usage` is needed on the frontend
- **AND** the usage footnote shows the round's actual token totals (not zero or
  empty, as was the case before the non-terminal-round visibility fix)

### Requirement: Live Per-Group DAG Status Markers

During a DAG-parallel `implement` step, each task group runs in an isolated
worktree whose conversation is not salvaged into the main repository until the
step ends, so the running-flow console would otherwise show nothing for that
step until its final salvage (see the `flow-engine` *Implement Step DAG
Execution Strategy* requirement). To give the operator live progress, the
orchestrator now writes lightweight per-group **status** records — and only
status, never the per-group conversation content — into the main-repo step
jsonl as each group transitions, and the console MUST render them.

`normalizeRecord` MUST recognize a conversation record whose `type` is
`group_status` and normalize it into a record carrying at least its `group_id`,
`status` (one of `queued` / `running` / `completed` / `failed` / `skipped`),
and `timestamp`, plus the optional `agentName` / `modelName` identity fields
extracted from the record's `agent_name` / `model_name` (defaulting to `null`
when absent — these MUST be read from the record, never hard-coded to `null`).
Within the `implement` step's section, each such record MUST
be rendered as a lightweight, **affordance-free** per-group status marker
(e.g. a `.group-status-marker` element) mapping status to a short human phrase
— for example `running` → "G3 正在 worktree 实施中", `completed` → "G1 已完成",
`queued` → "G{n} 排队中", `failed` → "G{n} 失败", `skipped` → "G{n} 已跳过".
The marker is a plain status line: it carries no fold chip, no "View raw"
toggle, and no reply affordance.

**Live agent / model on the marker.** For a group running in a worktree the
status marker MUST also display the agent the group is currently using, and —
once the real model identifier becomes available — upgrade to the
`agent · model` form, using the same `renderAgentBadge` / `formatAgentBadgeText`
helpers and identical formatting as every other LLM step's badge (see
*Per-Step Report Cards*). The agent/model identity rides each `group_status`
record: the orchestrator relays it from the group's `LLMCaller` (via the
caller's per-attempt agent-change notification) as each attempt's agent is
selected and again once the model is parsed, per the `llm-caller` *Streaming
NDJSON Output Display* requirement. Because successive `group_status` records
for the same group are reflected in place, a marker that first showed only the
agent upgrades to `agent · model` when a later record carries the model, and a
retry / agent rotation inside the group updates the marker to the agent that is
actually running — never a stale name. Consistent with the badge rule elsewhere,
when a record carries no agent/model the marker shows no badge or placeholder,
and the marker remains affordance-free and in strict chronological order
regardless.

**Single-card convergence per group.** "Reflected in place" is a hard
single-card invariant, not a visual approximation: a group is uniquely
identified by the composite key `(step_id, group_id)`, and over its entire
lifecycle that group MUST render exactly **one** status marker. Because a
single `implement` step contains several groups that all share one `step_id`,
the console MUST converge markers on the composite `(step_id, group_id)` key —
never on `step_id` alone, which would wrongly fold distinct groups into one
card. As each later `group_status` record for the group arrives (group launch →
agent selected → real model parsed → terminal), the marker is updated in place
rather than stacked: the surviving card is the group's latest record, which
already carries the accumulated `agent` / `model` identity, so retaining it is
equivalent to upgrading the original card's badge to `agent · model` in place.
A terminal record (`completed` / `failed` / `skipped`) supersedes that group's
prior non-terminal `running` card. Distinct groups carry distinct composite
keys and therefore keep independent cards that are never folded into one
another, even though they share a `step_id` — and a `group_id` reused under a
different `step_id` likewise stays independent.

These markers MUST obey the existing *Conversation Strict Chronological Order*
contract: they are placed by their `(timestamp, original-index)` key like every
other in-stream record and MUST NOT shuffle other records out of timestamp
order, and inserting / updating a marker MUST NOT disturb the fold state, raw
toggles, or chip selections of already-rendered records. As successive status
records for the same group arrive, the console reflects the group's latest
state (appended in order, or updated in place) so the operator watches progress
advance **before** the step finishes.

This is a status-only surface and MUST NOT be confused with content relay: the
full G1–G5 conversation still appears in one pass at step end once the worktree
histories are salvaged, exactly as before. The markers neither replace nor
pre-empt that final content.

#### Scenario: group_status record renders as a per-group status marker
- **GIVEN** an `implement` step's jsonl contains records of `type: "group_status"`
  carrying `group_id` and a `status` of `running` then `completed`
- **WHEN** the conversation is rendered in `#flow-view`
- **THEN** `normalizeRecord` recognizes each record and renders it as a
  lightweight per-group status marker inside the implement step's section
- **AND** the marker text reflects the status (e.g. a "running in worktree"
  phrasing for `running`, a "completed" phrasing for `completed`)
- **AND** the marker carries no fold chip, no "View raw" toggle, and no reply
  affordance

#### Scenario: Running marker shows the group's live agent and upgrades to agent · model
- **GIVEN** an `implement` step's jsonl where a group's `group_status` records
  first carry `status: "running"` with an `agent_name` only, then a later
  `running` / `completed` record additionally carries a `model_name`
- **WHEN** the conversation is rendered in `#flow-view`
- **THEN** the "running in worktree" marker for that group shows the agent name
  from the first record (e.g. the agent badge appended to "G3 正在 worktree
  实施中")
- **AND** when the model-bearing record arrives the marker upgrades in place to
  the `agent · model` form, using the same badge formatting as every other LLM
  step
- **AND** a `group_status` record carrying no agent/model renders its status
  marker with no badge or placeholder, and the marker stays affordance-free and
  in strict chronological order

#### Scenario: Successive group_status records for one group converge to a single in-place-updated card
- **GIVEN** an `implement` step whose jsonl emits, for a single group, the
  natural sequence of `status: "running"` `group_status` records — first at
  launch with no agent and no model, then with an `agent_name` only, then with
  both `agent_name` and `model_name`
- **WHEN** the conversation is rendered in `#flow-view`
- **THEN** the group (identified by its `(step_id, group_id)` composite key)
  renders exactly **one** status marker — not two or three stacked cards — and
  the surviving card is the latest record, so it carries the accumulated
  `agent · model` badge while the launch-only and agent-only cards are folded
  away
- **AND** when a terminal `completed` / `failed` / `skipped` record later
  arrives for that group, the terminal card supersedes the group's prior
  non-terminal `running` card, still leaving exactly one marker for the group

#### Scenario: Distinct groups under one implement step keep independent cards
- **GIVEN** an `implement` step in which several groups (e.g. G1, G2, G3) all
  share the same `step_id` but carry different `group_id` values, each emitting
  its own `group_status` records
- **WHEN** the conversation is rendered in `#flow-view`
- **THEN** each group keeps its own independent status marker keyed on the full
  `(step_id, group_id)` composite key, and the convergence pass MUST NOT fold
  distinct groups into one card merely because they share a `step_id`
- **AND** a `group_id` reused under a different `step_id` is treated as a
  separate group and likewise retains its own independent card

#### Scenario: Status markers advance before the step ends
- **GIVEN** a DAG-parallel implement step still in progress whose groups are
  emitting `group_status` records incrementally
- **WHEN** the daemon pushes the appended `group_status` records via the
  incremental `history_data` channel
- **THEN** the console updates the affected groups' markers as the records
  arrive, so progress is visible before the step completes rather than the
  view staying blank until step end

#### Scenario: Status markers respect strict chronological order and preserve UI state
- **GIVEN** a conversation already showing records and earlier `group_status`
  markers
- **WHEN** a new `group_status` record arrives via incremental append
- **THEN** it is inserted into its correct `(timestamp, original-index)` slot,
  not unconditionally appended at the tail
- **AND** the fold state, raw toggles, and chip selections of already-rendered
  records are preserved across the rebuild

#### Scenario: Status markers do not replace the salvaged group content
- **GIVEN** a DAG-parallel implement step that emitted `group_status` markers
  during execution
- **WHEN** the step ends and the per-group worktree histories are salvaged into
  the main repository
- **THEN** the full G1–G5 conversation content is rendered in one pass at step
  end as before
- **AND** the lightweight status markers neither replace nor suppress that
  final salvaged content

### Requirement: New Task — Arbitrary Project Root

The web console's "New Task" form MUST allow the user to start a flow against
**any** project root on the selected machine, not only roots the daemon has
already seen as live in its current process lifetime. To satisfy this the form
provides two complementary entry points for the `project_root` field, both
sourced from the selected machine's record:

1. **Known-project dropdown** — populated from
   `MachineRecord.project_roots`, which the daemon computes via
   `all_project_roots()` as the union of: its live registered roots, a
   **machine-local persistent project-roots registry** that records every root
   that has ever run a flow (written through on each spawn / ensure / resume /
   poll-discovery registration and reloaded on daemon restart), and the
   historical roots enumerated from `se3/history/` and `se3/state/archive/`; see
   the `aggregator.py` / `daemon.py` bullets in the `base` spec. Because the
   registry is persisted to disk and is independent of any live process, the
   dropdown stays populated even when the machine currently has **no** `se3 run`
   process running and across daemon restarts — not only for roots the daemon
   has seen as live in its current process lifetime.
2. **`Other path…` sentinel option** — always appended to the dropdown,
   including when the machine has zero known roots. Selecting it reveals a
   text input that accepts an absolute path; the entered path is sent as
   `project_root` to `POST /api/flows`.

The New Task form is additionally **owner-scoped** on the now multi-tenant
control plane (see the `base` spec's *Server Identity, Authentication and
Persistence* requirement): the machine picker and its linked `project_root`
dropdown are populated only from machines belonging to the currently
authenticated owner — a machine bound to a different owner never appears as a
target and the form cannot dispatch a flow to it. `POST /api/flows` is guarded
by `Depends(require_owner)` and verifies that the selected machine belongs to
the current owner before dispatching; a cross-owner target reads as not-found
(404) rather than being spawned.

The server endpoint `POST /api/flows` MUST validate only that the supplied
`project_root` is an absolute path; it MUST NOT reject paths that are absent
from the machine's known-roots list. Membership in `project_roots` is a hint
for the dropdown, not a precondition for spawning. (This is orthogonal to the
owner check above: the path-shape validation gates the `project_root` value,
while owner ownership gates the target machine.)

When the user-supplied target directory is not yet an SE3 project (no
`se3/specs/base/spec.md` marker), the daemon MUST initialize it on the user's
behalf before spawning the flow — see the `spawner.py` / `client.py` bullets
in the `base` spec and the `se3-commands` `se3 init` requirement; the New
Task form itself need not require the user to pre-initialize the directory.

The New Task form MUST also expose an **isolation / worktree** checkbox
(`#nt-worktree`), styled like the existing discovery-mode toggle. When checked,
it is equivalent to the CLI `se3 run --worktree`: the submitted flow runs in an
isolated git worktree and auto-merges back into the original branch on success
(see the `se3-commands` *Unified Entry Point `se3 run`* requirement and the
`flow-engine` worktree-mode scenarios). The frontend (`buildNewFlowBody` in
`app.js`) carries the boolean as `worktree` in the `POST /api/flows` body; the
server (`NewFlowRequest` / `POST /api/flows` in `app.py`) threads `req.worktree`
through the spawn protocol (`make_spawn_flow` → client → `request_spawn` →
spawner), and the daemon spawner appends `--worktree` to the `se3 run` argv in
the same position it appends `--discover`. Apart from this create-time toggle,
the console displays and handles a worktree-mode flow identically to a
synchronous run.

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

#### Scenario: Dropdown stays populated with no live process and across daemon restart
- **GIVEN** a machine with a project root that has previously run a flow (so it
  is recorded in the daemon's persistent project-roots registry) but currently
  has **no** live `se3 run` process
- **WHEN** the New Task form's project dropdown is rendered, including after the
  daemon has been restarted with the same `pid_dir`
- **THEN** the root still appears in the dropdown without the user falling back
  to the `Other path…` manual entry
- **AND** the same registry-backed `all_project_roots()` source keeps the
  machine's history list populated rather than empty

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

#### Scenario: New Task only targets the authenticated owner's machines
- **GIVEN** the central server hosts machine `M1` owned by the current owner
  `O1` and machine `M2` owned by a different owner `O2`
- **WHEN** the user opens the New Task form
- **THEN** the machine picker and its linked Project dropdown surface only
  `M1` (and `M1`'s `project_roots`); `M2` does not appear as a selectable
  target

#### Scenario: New Task submission to a cross-owner machine is rejected
- **GIVEN** the current owner `O1` crafts a `POST /api/flows` request whose
  target machine is `M2`, owned by another owner `O2`
- **WHEN** the server validates the request
- **THEN** the request is rejected as not-found (404) and no flow is spawned on
  `M2`, because the owner check gates the target machine regardless of the
  `project_root` path being absolute

#### Scenario: Worktree isolation toggle dispatches an isolated run
- **GIVEN** the user fills in the New Task form and checks the isolation /
  worktree checkbox (`#nt-worktree`)
- **WHEN** the form is submitted
- **THEN** `buildNewFlowBody` includes `worktree: true` in the `POST /api/flows`
  body, the server threads `req.worktree` through the spawn protocol, and the
  daemon spawner appends `--worktree` to the spawned `se3 run` argv
- **AND** the flow runs in an isolated git worktree and auto-merges back on
  success, exactly as `se3 run --worktree` does on the CLI

#### Scenario: Unchecked worktree toggle dispatches a synchronous run
- **GIVEN** the user leaves the isolation / worktree checkbox unchecked
- **WHEN** the form is submitted
- **THEN** the `POST /api/flows` body carries `worktree: false` (or omits it),
  no `--worktree` flag is appended, and the flow runs in the default
  synchronous mode

### Requirement: Mobile Portrait Responsive Layout

On a narrow-screen (phone-portrait) breakpoint — `@media (max-width: 600px)` —
`#flow-view` MUST adapt its full-screen chat console for one-handed phone use
while remaining **full-feature-equivalent to the desktop layout**: no control
operation may be hidden, removed, or downgraded, only relocated. The desktop
full-screen chat layout and every existing behavior (the strict-chronological
conversation, the docked reply box, the chip-bar interaction model, the
collapsible reply-context prompt, the history-back close path, etc.) MUST be
left unchanged outside the narrow-screen breakpoints; all phone-portrait rules
live strictly inside those breakpoints so the desktop experience does not
regress.

Within the narrow-screen breakpoint, the observable behavior is:

1. **Off-canvas sidebar drawer** — the auxiliary sidebar (Overview / Steps /
   machine info) is NOT rendered as a permanent column that squeezes or stacks
   above the conversation. It becomes a button-summoned off-canvas drawer: a
   visible toggle control opens it as an overlay, and tapping the backdrop (or
   the toggle / close affordance) dismisses it. Closing or backing out of
   `#flow-view` resets the drawer to its closed state.
2. **Conversation fills the main column** — with the sidebar off-canvas, the
   `#flow-conversation` chat-stream occupies the full width of the main column
   and remains the sole vertical scroller, with NO unexpected horizontal
   scrolling (long content wraps per the *Long-Content Wrapping* requirement).
3. **Touch-optimized docked reply area** — the docked reply area (the pending
   intervention chip bar, the reply-context panel, the reply textarea, and the
   inline Interject / Send controls) is laid out for touch: chips and buttons
   meet a minimum touch-target size, the textarea uses a ≥16px font so mobile
   browsers do not auto-zoom, and the row wraps sensibly instead of overflowing
   the viewport width. The reply area keeps its full desktop semantics — the
   always-enabled textarea, the send-button settle gate, the inline Interject
   opt-in, and the chip-selection targeting all behave exactly as on desktop.
   Because the textarea carries the ≥16px iOS-zoom-guard font, the idle
   no-pending-interaction **placeholder** would otherwise wrap to two lines in
   the default single-line (collapsed) textarea. To keep that hint on one line,
   the narrow-screen breakpoint shrinks ONLY the placeholder font
   (`#flow-reply-input::placeholder { font-size: 13px }`) — the textarea's own
   16px input font is untouched, so real typing keeps the zoom guard and normal
   input size. The shrink is paired with a **mobile-shortened placeholder
   string**: the idle-flow placeholder is gated through the same
   `isMobilePortrait()` (`matchMedia('(max-width: 600px)')`) helper as the
   auto-grow textarea, rendering a shorter phrase on mobile while the desktop
   wording stays byte-for-byte unchanged (the only `app.js` change in this
   adaptation; the desktop idle placeholder of the *Docked Persistent Reply Box*
   requirement is preserved exactly). Together the smaller font and shorter
   string guarantee the idle placeholder fits one line on a phone.
4. **Reclaimed chat-area horizontal whitespace** — in the narrow-screen
   breakpoint, scoped to the `.flow-conversation` chat area, the per-record
   identity decoration that the conversation inherits from the history-list view
   is removed so each turn approaches full column width. Concretely: the
   `.flow-conversation .history-record` left identity bar (`border-left`, the
   3px accent/green stripe) is removed (or zeroed) and its left/right padding
   collapsed; the outer `.flow-conversation` left/right padding is narrowed from
   ~16px to ~8px (keeping a small margin so text does not touch the screen
   edge); and the `.conv-record.role-user .conv-bubble` /
   `.conv-record.role-assistant .conv-bubble` `max-width` is widened from 88% to
   near-full width (`max-width: 100%` / `align-self: stretch`). Speaker identity
   is then carried solely by each bubble's own colored border + tinted
   background (role-user blue border + light-blue fill, role-assistant green
   border + light-green fill); the bubble's inner `padding: 8px 11px` is kept so
   text keeps breathing room from the border. The combined effect removes the
   thick colored stripe, the ~30px left indent, and the right-side dead space so
   every line maximizes content while the speaker stays distinguishable by color.
   This narrowing MUST NOT affect the non-flow history-list view, where
   `.history-record`'s left stripe and padding are retained.
5. **Single-line tool-marker chip** — in the narrow-screen breakpoint, scoped to
   the `.flow-conversation` chat area, a `.tool-marker` summary row
   (`.tool-marker-name` / `.tool-marker-glyph` / `.tool-marker-detail` /
   `.tool-marker-toggle`) is compressed from the desktop `flex-wrap: wrap`
   multi-line layout onto a single line (`flex-wrap: nowrap`): name and detail
   share one row, an over-long detail is truncated with a single-line ellipsis
   (`white-space: nowrap; overflow: hidden; text-overflow: ellipsis`) rather than
   wrapping, and the expand/collapse toggle stays inline at the row's end. For the
   ellipsis truncation to actually engage, the flexible `.tool-marker-detail`
   segment MUST be allowed to shrink below its intrinsic content width: its
   `flex-basis` MUST be zeroed (`flex: 1 1 0` / `min-width: 0`) rather than left at
   the default `auto`. With `flex-basis: auto` the `nowrap` header still overflows
   because the detail's intrinsic width pins the row wider than the column (the
   prior session added the `nowrap`/ellipsis rule but it had no effect for exactly
   this reason); zeroing the basis lets the detail collapse and the ellipsis take
   over. The expandable details panel (`.tool-marker-details`, holding diff / text
   / bash output) is unaffected and still expands to show full content on its own
   line(s). Additionally, the chip's "details" expand affordance
   (`.tool-marker-toggle`) is a `<button>`, so the breakpoint's baseline
   `button { min-height: 40px }` touch-target rule grabs this tiny secondary
   toggle and inflates it, which stretches the whole `.tool-marker` chip row far
   taller than its text. The narrow-screen breakpoint MUST therefore compact this
   one toggle — scoped precisely to `.flow-conversation .tool-marker-toggle` —
   down to its text height. **The real, render-verified cause is that
   `min-height`, `line-height`, and zeroed padding alone are NOT sufficient on a
   mobile WebKit browser:** a `<button>` defaults to `appearance: auto`, i.e. a
   **native form control whose intrinsic vertical metrics** (the platform
   button's own minimum size plus internal vertical padding) cannot be removed by
   `min-height` / `padding` / `line-height`. Desktop Chrome happens not to impose
   that native floor, so the prior session's `min-height`/`line-height`/padding
   relaxation "looked fixed" on desktop yet showed no visible change on the phone
   (the reported "改了没区别"). The breakpoint MUST therefore ALSO strip the native
   control with `-webkit-appearance: none; appearance: none;` so the toggle
   becomes a plain box that finally honors the compacting declarations
   (`min-height: auto`, `line-height: 1`, and a small symmetric top/bottom
   padding) on mobile WebKit too. Because `.tool-marker` aligns its head children on the text
   baseline (`align-items: baseline`), the breakpoint MUST additionally re-center
   the chip head on the cross axis — scoped to `.flow-conversation .tool-marker`
   with `align-items: center` — so the now-flattened toggle can never re-inflate
   the folded row's height via baseline ascent/descent math. The toggle's
   horizontal padding (6px) and font-size (10.5px) are inherited from the desktop
   rule unchanged, and every other control caught by `button { min-height: 40px }`
   (icon buttons, intervention chips, reply option buttons, …) keeps its full
   touch target. **Vertical padding is reassigned between the card and the
   toggle, not added to the row's total height.** The earlier breakpoint zeroed
   the toggle's top/bottom padding, which left the toggle label hugging its own
   button border. The breakpoint MUST instead give the toggle a small symmetric
   top/bottom padding (`padding-top: 3px; padding-bottom: 3px`) so its label has
   breathing room from its border, AND shed an equal amount of vertical padding
   from the `.tool-marker` card itself (desktop `5px` → `2px` top/bottom, with the
   card's left/right padding inherited unchanged). Because the toggle's gain and
   the card's loss are equal and opposite, the folded chip's total height stays
   essentially unchanged — the reassignment only redistributes the existing
   whitespace from the card's border gap into the toggle's own padding. Desktop
   never matches this breakpoint, so the desktop `.tool-marker` and
   `.tool-marker-toggle` are byte-for-byte intact (the desktop chip keeps
   `align-items: baseline`).
6. **Tiled reply meta row** — in the narrow-screen breakpoint, the docked reply
   region's `.flow-reply-head` (TO / KIND / callid) and the
   "▸ expand message details" toggle (`.flow-reply-prompt-toggle`) are collapsed
   from the desktop vertical multi-line stack into a single horizontal tiled row
   that uses the right-side whitespace and reduces vertical footprint. The
   expanded prompt body (`.flow-reply-prompt`) still respects its existing 30vh
   cap and scrolls internally. Additionally, when a single `call`-kind
   intervention chip is pending (the `⚙` "Pending reply" chip in
   `#flow-interventions`), that chip — which on the desktop / unscoped layout
   occupies its own full-width row above the reply-context panel — is tiled onto
   the **same** horizontal row as the active reply-context header
   (`.flow-reply-context.active .flow-reply-head`) via a wrapping `.flow-reply`
   container, instead of standing alone on its own line with an oversized button.
   This reclaims the vertical space the lone chip used to consume on a phone
   viewport. The chip keeps its full selection / targeting semantics (tapping it
   still selects the intervention and drives the shared reply textarea); only its
   placement changes, and only inside the breakpoint, so the desktop chip-bar
   layout is unchanged. For the **`discovery_confirm`** kind specifically, whose
   dock otherwise crowds four mismatched blocks together on a phone — a large
   green `✓ 确认任务描述` status chip, a near-duplicate `回复中 · 确认任务描述`
   head, a boxed/uppercase `▸ 展开消息详情` prompt-toggle stranded alone on its
   own row, and the `确认并继续(输入 1)` confirm button — the narrow-screen
   breakpoint additionally tidies the panel, with every rule scoped to
   `.flow-reply-context.kind-discovery_confirm` so other kinds and the entire
   desktop panel are untouched (no markup / logic change — the existing
   `kind-discovery_confirm` class is reused): **(a) de-dup** — the
   `.flow-reply-head` is hidden (`display: none`) because the status chip already
   carries the same `✓ 确认任务描述` label, eliminating the repeated text;
   **(b) lightweight expand entry** — the `.flow-reply-prompt-toggle` is
   restyled from a boxed/uppercase button into a plain text link (border removed,
   `text-transform: none`, letter-spacing zeroed, font-size matching the
   surrounding secondary text, accent color, underline on hover), and the
   expanded-state desktop rule that re-adds a border is overridden to keep the
   link lightweight; **(c) alignment + hierarchy** — the chip and the expand
   link are vertically centered against each other and tiled onto the shared
   first row (the chip via the wrapping `.flow-reply` container, the link as the
   context's first visible child after the head is hidden), while the confirm
   button drops to its own full-width row (`.flow-reply-options` basis 100%, the
   primary confirm option stretched and centered) so it reads as the panel's
   clear primary action rather than one more same-size chip. The confirm value
   and channel are unchanged — the button still sends the literal `"1"` through
   the shared call/response reply path (see *Unified Intervention Items*).
7. **WeChat-style auto-grow reply textarea** — in the narrow-screen breakpoint
   the reply textarea (`#flow-reply-input`) behaves like a chat app's composer:
   it opens at a single-line height, grows automatically as the user types, stops
   growing at a maximum height (~35vh, ≈5–6 rows) and scrolls internally beyond
   that, and falls back toward single-line height when content is deleted or
   cleared. The narrow-screen rules therefore drop the mobile fixed
   `min-height: 104px` and the manual `resize: vertical` handle (`resize: none`
   on mobile), handing height control to JS. The auto-grow logic recomputes the
   height on the textarea's `input` event and resets it after a successful send
   clears the field and after switching / selecting a different chip resets the
   content. Because the textarea's static markup carries `rows="6"`, the auto-grow
   logic MUST NOT measure `scrollHeight` against that six-row intrinsic height —
   doing so (e.g. resetting to `height: "auto"` before measuring) leaves an
   empty / default-state field reporting a ~6-row `scrollHeight` and so renders ~6
   rows tall, masking the chat history. To make the empty / default state truly
   collapse to a single line, the measurement MUST first force the field's height
   to `0` (or use an explicit single-line baseline) before reading `scrollHeight`,
   so an empty field measures one line and grows only as real content is added.
   The textarea stays editable at all times (only Send is briefly disabled
   in-flight); the Send enable-gate and Ctrl/Cmd+Enter submit behavior are
   unchanged. On desktop this behavior is a no-op: the six-row default height and
   `resize: vertical` are untouched.

The narrow-screen layout is driven only by CSS rules inside the breakpoint plus
minimal class toggles / height assignments backed by exported pure helpers for
DOM-free testing — the sidebar drawer state via `flowSidebarNextState`, and the
auto-grow textarea height via `replyTextareaHeight(scrollHeight, minPx, maxPx)`,
which clamps the measured `scrollHeight` to the single-line/maximum bounds with
boolean/numeric fallback for invalid input, in the same style as
`navMenuNextState` / `flowSidebarNextState`. The application layer gates the
auto-grow behavior behind `matchMedia('(max-width: 600px)')`. Because the
desktop stylesheet defines no styling for these narrow-screen classes and the JS
is a matchMedia-gated no-op on wide viewports, toggling them on a desktop
viewport is a no-op, guaranteeing zero desktop regression.

#### Scenario: Sidebar becomes an off-canvas drawer on a phone-portrait viewport
- **GIVEN** `#flow-view` is open on a viewport at or below the narrow-screen
  breakpoint (`max-width: 600px`)
- **WHEN** the view is rendered
- **THEN** the Overview / Steps / machine sidebar is NOT shown as a permanent
  column; instead a visible toggle control summons it as an off-canvas drawer
- **AND** tapping the drawer's backdrop (or its toggle / close control) dismisses
  the drawer
- **AND** closing `#flow-view` or backing out of it resets the drawer to closed

#### Scenario: Conversation fills the column with no horizontal scroll
- **WHEN** `#flow-view` is rendered at the narrow-screen breakpoint with the
  sidebar drawer closed
- **THEN** `#flow-conversation` occupies the full main-column width and stays the
  sole vertical scroller
- **AND** no element inside `#flow-view` introduces unexpected horizontal page
  scrolling — long lines wrap per the *Long-Content Wrapping* requirement

#### Scenario: Docked reply area is touch-optimized but functionally identical
- **WHEN** the docked reply area is rendered at the narrow-screen breakpoint
- **THEN** the chip bar, reply-context panel, textarea, and Interject / Send
  controls are sized and wrapped for touch (minimum touch-target sizes, a ≥16px
  textarea font, no overflow past the viewport width)
- **AND** the always-enabled textarea, the send-button settle gate, the inline
  Interject opt-in, and chip-selection targeting all behave exactly as on desktop
  (no control is hidden or downgraded)

#### Scenario: Desktop layout is unaffected outside the breakpoint
- **WHEN** `#flow-view` is rendered on a viewport wider than the narrow-screen
  breakpoint
- **THEN** the sidebar remains a permanent column, the reply area keeps its
  desktop sizing, and none of the phone-portrait drawer / panel-switch behavior
  applies
- **AND** toggling the narrow-screen-only classes on a desktop viewport is a
  no-op because the desktop stylesheet defines no rules for them

#### Scenario: Chat rows reclaim horizontal whitespace without the identity stripe
- **GIVEN** `#flow-view` is open on a viewport at or below the narrow-screen
  breakpoint (`max-width: 600px`) with conversation records rendered as
  `.history-record conv-record role-<role>`
- **WHEN** the `.flow-conversation` chat area is rendered
- **THEN** each row no longer shows the `.history-record` left identity stripe
  (`border-left`) or the ~30px outer indent, the outer `.flow-conversation`
  left/right padding is narrowed (~8px), and the `.conv-bubble` widens to near
  full column width (`max-width: 100%` / `align-self: stretch`)
- **AND** speaker identity is still distinguishable solely by the bubble's own
  colored border + tinted background (role-user blue, role-assistant green),
  with the bubble's inner `padding: 8px 11px` retained
- **AND** the non-flow history-list view (`.history-record` outside
  `.flow-conversation`) keeps its left stripe and padding, and the desktop
  chat layout is unaffected outside the breakpoint

#### Scenario: Reply textarea auto-grows WeChat-style within bounds
- **GIVEN** `#flow-view` is open on a viewport at or below the narrow-screen
  breakpoint (`max-width: 600px`)
- **WHEN** the user types into the reply textarea (`#flow-reply-input`)
- **THEN** the textarea opens at a single-line height and grows automatically as
  content is added, stopping at a maximum height (~35vh) beyond which it scrolls
  internally
- **AND** when content is deleted or cleared — and after a successful send clears
  the field or after switching / selecting a different chip resets the content —
  the height falls back toward single-line
- **AND** the textarea stays editable throughout, the Send enable-gate and
  Ctrl/Cmd+Enter submit behavior are unchanged, and the height is derived from
  the DOM-free pure helper `replyTextareaHeight(scrollHeight, minPx, maxPx)`
- **AND** on a viewport wider than the breakpoint the desktop six-row default
  height and `resize: vertical` are unaffected (the auto-grow logic is a
  `matchMedia`-gated no-op)

#### Scenario: Empty / default reply textarea collapses to a single line
- **GIVEN** `#flow-view` is open on a viewport at or below the narrow-screen
  breakpoint and the reply textarea is empty (default state, no message typed),
  even though its static markup carries `rows="6"`
- **WHEN** the auto-grow logic measures the field to set its height
- **THEN** it forces the field's height to `0` (or a single-line baseline) before
  reading `scrollHeight`, so the empty field measures one line rather than the
  six-row intrinsic height
- **AND** the empty / default textarea renders at single-line height and does NOT
  occupy ~6 rows or mask the chat history below it
- **AND** as soon as the user types real content the field grows from the
  single-line baseline up to the ~35vh cap, then scrolls internally

#### Scenario: Single call-kind chip is tiled onto the reply-context header row
- **GIVEN** `#flow-view` is open on a viewport at or below the narrow-screen
  breakpoint with a single pending `call`-kind intervention (the `⚙`
  "Pending reply" chip in `#flow-interventions`) and its reply-context panel active
- **WHEN** the docked reply region is rendered
- **THEN** the call chip shares the same horizontal row as the active
  reply-context header (`.flow-reply-context.active .flow-reply-head`) via a
  wrapping `.flow-reply` container, instead of standing alone on its own
  full-width row with an oversized button, reclaiming vertical space
- **AND** tapping the chip still selects the intervention and targets the shared
  reply textarea exactly as before — only its placement changes
- **AND** on a viewport wider than the breakpoint the chip keeps its desktop
  chip-bar placement (the tiling is scoped strictly inside the breakpoint)

#### Scenario: Tool-marker details toggle does not stretch the chip on mobile
- **GIVEN** `#flow-view` is open on a viewport at or below the narrow-screen
  breakpoint (`max-width: 600px`) and the conversation contains a folded
  `.tool-marker` chip whose `.tool-marker-toggle` "details" affordance is a
  `<button>`
- **WHEN** the `.flow-conversation` chat area is rendered
- **THEN** the breakpoint strips the toggle's native control with
  `-webkit-appearance: none; appearance: none` AND compacts it for
  `.flow-conversation .tool-marker-toggle` only (`min-height: auto`,
  `line-height: 1`, and a small symmetric `3px` top/bottom padding), so the
  toggle is no longer held tall by the native `<button>` vertical floor that
  `min-height` / `padding` / `line-height` alone cannot remove on mobile WebKit
- **AND** the breakpoint re-centers the chip head with
  `.flow-conversation .tool-marker { align-items: center }` (overriding the
  desktop `align-items: baseline`) so the flattened toggle cannot re-inflate the
  row via baseline math, and the folded `.tool-marker` card is visibly shorter
  rather than "changed but looking the same"
- **AND** the toggle's `3px` top/bottom padding is balanced by an equal `3px`
  reduction in the `.tool-marker` card's own vertical padding (desktop `5px` →
  `2px` top/bottom), so the toggle label gains breathing room from its border
  while the folded chip's total height stays essentially unchanged
- **AND** the toggle's horizontal padding (6px) and font-size (10.5px) are
  unchanged, and every other `<button>` caught by the touch-target rule keeps
  its full 40px minimum
- **AND** on a viewport wider than the breakpoint the `.tool-marker` and
  `.tool-marker-toggle` keep their desktop appearance byte-for-byte (the
  `appearance: none` and `align-items: center` overrides live strictly inside
  the breakpoint, and the expanded `.tool-marker-details` panel's padding is
  unchanged on both viewports)

#### Scenario: Idle reply placeholder fits one line on mobile
- **GIVEN** `#flow-view` is open on a viewport at or below the narrow-screen
  breakpoint (`max-width: 600px`) for an active flow with no pending
  interaction, so the reply textarea shows its idle placeholder in the default
  single-line (collapsed) state
- **WHEN** the docked reply area is rendered
- **THEN** the placeholder font is shrunk via
  `#flow-reply-input::placeholder { font-size: 13px }` and the placeholder
  string is the mobile-shortened phrase (gated through `isMobilePortrait()` /
  `matchMedia('(max-width: 600px)')`), so the hint fits on one line without
  wrapping
- **AND** the textarea's own input font stays at the ≥16px iOS-zoom-guard size,
  so normal typing is unaffected
- **AND** on a viewport wider than the breakpoint the placeholder keeps its
  full desktop wording byte-for-byte and the desktop placeholder font is
  unchanged

#### Scenario: discovery_confirm dock is de-duplicated and aligned on mobile
- **GIVEN** `#flow-view` is open on a viewport at or below the narrow-screen
  breakpoint (`max-width: 600px`) with a pending `discovery_confirm`
  intervention whose reply-context panel (`.flow-reply-context.kind-discovery_confirm`)
  is active
- **WHEN** the docked reply region is rendered
- **THEN** the `.flow-reply-head` is hidden so its `回复中 · 确认任务描述` text no
  longer duplicates the `✓ 确认任务描述` status chip
- **AND** the `.flow-reply-prompt-toggle` is rendered as a lightweight plain-text
  link (no border, no uppercase, font-size matched to the surrounding secondary
  text) tiled onto the same row as the status chip rather than standing alone in
  a boxed button on its own row
- **AND** the `确认并继续(输入 1)` confirm button drops to its own full-width
  row as the panel's emphasized primary action, still sending the literal `"1"`
  through the shared call/response channel
- **AND** all of these rules are scoped to `kind-discovery_confirm` inside the
  breakpoint, so other intervention kinds and the desktop `discovery_confirm`
  panel are unchanged

### Requirement: Start Flow From Open Issue

The web console's issue surface SHALL expose a "start a flow from this
issue" control on each issue (in the issue list row and/or the issue
detail view), so an owner can launch a new `se3 run` flow seeded by an
existing issue directly from the console. The control reuses the
existing spawn path (`POST /api/flows` with `from_issue_id`; see the
`base` spec's *Server Modules* requirement) and the existing
start-from-discovery interaction pattern already used by the spawn
form.

**Availability gating:**

- The launch control SHALL be enabled **only** for issues in `open`
  status. Issues in `in-progress` or any closed status
  (`resolved` / `wont_fix` / `closed`) SHALL render the control as
  disabled (greyed out), because a non-open issue cannot be started as
  a fresh flow (an in-progress issue must be reset first).

**Discovery option:**

- When launching, the user SHALL be offered a discovery option that
  mirrors the spawn form's start-from-discovery toggle. When selected,
  the request carries `discover: true` alongside `from_issue_id` so the
  daemon appends `--discover` to the `se3 run --from-issue` command and
  the flow runs the discovery workflow seeded by the issue.

**Dispatch and feedback:**

- Clicking the enabled control SHALL POST to `/api/flows` with the
  issue's `from_issue_id` (and the chosen `discover` flag), and SHALL
  surface a "dispatched" acknowledgement to the user on success. The
  request body construction and the enabled/disabled decision SHALL be
  factored into testable **pure functions** (following the web
  console's existing pure-function pattern), so the open-status gate,
  the request payload shape, and any pending/dispatched UI state are
  unit-testable without a live backend.

#### Scenario: Launch control enabled only for open issues
- **GIVEN** an issue rendered in the console issue surface
- **WHEN** the issue's status is `open`
- **THEN** the "start a flow from this issue" control is enabled
- **WHEN** the issue's status is `in-progress` or any closed status
- **THEN** the control is rendered disabled (greyed out) and cannot be
  clicked

#### Scenario: Launch from an open issue dispatches a spawn
- **GIVEN** an `open` issue
- **WHEN** the user activates the launch control without selecting the
  discovery option
- **THEN** the console POSTs to `/api/flows` with `from_issue_id` set to
  the issue's id and `discover` false (or omitted)
- **AND** a dispatched acknowledgement is shown on success

#### Scenario: Launch from an open issue with discovery
- **GIVEN** an `open` issue
- **WHEN** the user selects the discovery option and activates the
  launch control
- **THEN** the POST to `/api/flows` carries `from_issue_id` together
  with `discover: true`
- **AND** the resulting flow runs the discovery workflow seeded by the
  issue

#### Scenario: Pure functions back the launch model
- **GIVEN** the issue-launch availability and request-body logic
- **THEN** the enabled/disabled decision and the `/api/flows` request
  body are produced by pure functions that can be unit-tested in
  isolation (open-status gate, payload shape, pending/dispatched state)
  without a running server
