<!-- spec-format: v1 -->
# history-view-console Specification

## Purpose

The `history-view-console` subsystem defines the phone-portrait behavioral
contract of the web console's **History surface** — the full-screen
`#history-view` through which a user browses past flow sessions (the history
list pane) and inspects a single session's step-by-step conversation (the
history detail pane). It is the History-view counterpart of the
`running-flow-console` spec, which owns the equivalent contract for the
running-flow `#flow-view`; the `base` spec's *Server Modules* requirement
describes both views' responsive layout at a high level (two-pane interfaces
collapse to single-view panel switches inside the narrow-screen breakpoint)
and delegates each view's detailed phone-portrait contract to its own spec.

The History view shares the conversation rendering engine
(`normalizeRecord` / `renderConversation`, role-based collapse, Markdown
rendering, step report cards) with `#flow-view`, so long in-stream content
(conversation bubbles, raw JSON, Markdown code blocks, tool markers, step
report list items) is governed by the shared *Long-Content Wrapping*
rules in the `running-flow-console` spec and reaches the History detail
through that same engine. This spec adds the **History-view-specific**
guarantee those shared rules do not cover: the History view's own layout
containers and its history-only text affordances (project dropdown, session
meta, step titles, message chips) MUST NOT produce unexpected horizontal page
overflow on a phone.

## Requirements

### Requirement: Direct Resume Entry

The History surface MUST offer a **Resume** entry for any past flow that the
engine can pick back up directly via `se3 run --resume --flow-id <id>`, so a
user can recover a stalled run straight from the history list or a session's
detail without dropping to the CLI. The entry's visibility is governed by the
pure `isFlowResumable(flow)` predicate, which is the frontend mirror of the
server's authoritative `ServerState.is_flow_resumable` check (see the `base`
spec's *Server Modules* requirement): a flow qualifies **only** when its status
is `FAILED` or `PAUSED` and it is **not** archived/history-only (a `source` of
`archived` or `history` disqualifies it, because such snapshots have no live
`engine.json` to resume against). `RUNNING` / `INIT` / `RECOVERING` (already
in-progress) and `COMPLETED` (terminal) flows show no Resume entry.

Activating the entry POSTs `POST /api/flows/{flow_id}/resume`; the server
re-validates owner scope, the resumable status, and machine connectivity, and
on success dispatches a `MSG_SPAWN_FLOW` carrying `resume_flow_id` to the
owning daemon. The frontend MUST debounce concurrent activations for the same
flow (tracked in `state.resumeFlowRequests`) so a double-click cannot fire two
resume dispatches, and MUST surface the outcome (dispatched / not-resumable /
error) to the user. The button is intentionally **not** wired to the archived
`se3 history restore` rollback path — resuming only ever continues the current
live flow, never overwrites an active flow by restoring an archived snapshot.

#### Scenario: Resume entry shown only for directly-resumable flows
- **GIVEN** the history surface lists flows in assorted statuses
- **WHEN** the list / detail is rendered
- **THEN** a Resume entry appears only for flows that are `FAILED` or `PAUSED`
  and not archived/history-only (`isFlowResumable` returns true)
- **AND** no Resume entry appears for `RUNNING`, `INIT`, `RECOVERING`,
  `COMPLETED`, or archived/history-only flows

#### Scenario: Activating Resume dispatches a resume request and is debounced
- **GIVEN** a resumable flow's Resume entry is visible
- **WHEN** the user activates it
- **THEN** the frontend POSTs `/api/flows/{flow_id}/resume` and records the flow
  in `state.resumeFlowRequests` so a second activation for the same flow is
  suppressed until the request settles
- **AND** the user is shown the dispatched / not-resumable / error outcome

### Requirement: History View Mobile Horizontal-Overflow Containment

On the narrow-screen (phone-portrait) breakpoint — `@media (max-width: 600px)`
— neither the History list pane nor the History detail pane MUST produce
unexpected horizontal page scrolling. Every long-text affordance that can
appear in the History view — the session title, the project dropdown
(`<select class="history-project-select">`), the per-session item meta spans,
step titles, conversation bubbles, raw JSON, Markdown code blocks, tool
markers, step report cards, and step report list items
(`.step-report__list li`) — MUST wrap, truncate, or scroll vertically inside
its visible container; none of them MUST widen `.history-view` /
`.history-body` / `.history-list-pane` / `.history-detail-pane` /
`.history-detail` / `.history-step` or the page itself.

The fix is **CSS-only** and follows the same INCREMENTAL OVERLAY discipline as
the `running-flow-console` *Mobile Portrait Responsive Layout* requirement: all
rules live strictly inside the `@media (max-width: 600px)` breakpoint, and
every selector carries a `#history-view` prefix so it is unique to the History
view and never affects `#flow-view` or any other surface that shares the same
class names. The desktop History layout, and in particular the non-flow
history-list view's `.history-record` 3px left identity stripe and its desktop
padding, MUST be left unchanged. Horizontal scrolling MUST NOT be used as the
remedy: `overflow-x: auto` MUST NOT be introduced on any of these selectors.

The breakpoint addresses two layers:

1. **Container shrink** — the grid/flex containers are constrained so a wide
   child cannot blow the layout out past the viewport:
   `#history-view .history-body { max-width: 100% }`;
   `#history-view .history-list-pane` and `#history-view .history-detail-pane`
   get `min-width: 0; max-width: 100%; overflow-x: hidden` (the
   `min-width: 0` is the grid/flex blowout guard, mirroring the flow-view
   `.flow-main { min-width: 0 }` fix, and the hidden x is a mobile-only
   backstop so any residual wide child is clipped rather than scrolling the
   page — desktop keeps its natural pane overflow); and
   `#history-view .history-detail` / `#history-view .history-step` get
   `min-width: 0; max-width: 100%`.
2. **Long-content wrapping** — text affordances that produce unbreakable long
   runs are made shrinkable and breakable:
   `#history-view .history-project-select-row { flex-wrap: wrap; min-width: 0 }`
   and `#history-view .history-project-select { flex: 1 1 0; min-width: 0;
   max-width: 100% }` (the `flex-basis: 0` is required for the same reason the
   `.tool-marker-detail` fix zeroes it — a native `<select>`'s min-content
   width is its longest `<option>`, which with `flex-basis: auto` pins the
   line wider than the column before flex-shrink can apply; zeroing the basis
   lets the select shrink below its longest option);
   `#history-view .history-item-meta { flex-wrap: wrap }` with its child
   spans (`#history-view .history-item-meta > span`) getting
   `min-width: 0; overflow-wrap: anywhere; word-break: break-word`; and
   `#history-view .history-step-title` / `#history-view .msg-chip` getting
   `overflow-wrap: anywhere; word-break: break-word`.

The shared in-stream content selectors (`.conv-bubble .md-code`, `.raw-json`,
`.step-report__markdown .md-code`, `.step-report__list li`, and the
tool-marker sub-elements) already carry unscoped wrapping rules governed by
the `running-flow-console` *Long-Content Wrapping* requirement; because the
History detail renders through the same shared `renderConversation` engine, it
inherits those rules unchanged. This spec does NOT re-declare them — it only
requires that the History detail rendering path route long content into those
already-protected classes, locked by DOM-level regression tests rather than by
new CSS.

#### Scenario: History list and detail panes do not scroll the page horizontally on mobile
- **GIVEN** `#history-view` is open on a viewport at or below the narrow-screen
  breakpoint (`max-width: 600px`), on either the history list pane or the
  history detail pane
- **WHEN** the view is rendered
- **THEN** `#history-view .history-list-pane` and
  `#history-view .history-detail-pane` carry `min-width: 0`, `max-width: 100%`,
  and `overflow-x: hidden`, and `#history-view .history-body` carries
  `max-width: 100%`, so no pane widens the page
- **AND** neither the panes, `#history-view .history-detail`,
  `#history-view .history-step`, nor the page itself gains an unintended
  horizontal scrollbar, and no `overflow-x: auto` is applied to any of them

#### Scenario: Long project name and session meta wrap inside the history list
- **GIVEN** the history list pane on a phone-portrait viewport whose project
  dropdown has a long `<option>` label and whose session cards carry long meta
  spans
- **WHEN** the list is rendered
- **THEN** `#history-view .history-project-select` shrinks below its longest
  option via `flex: 1 1 0; min-width: 0; max-width: 100%`, its row
  (`#history-view .history-project-select-row`) wraps via `flex-wrap: wrap`,
  and the meta spans (`#history-view .history-item-meta > span`) wrap
  character-by-character via `overflow-wrap: anywhere` / `word-break:
  break-word`
- **AND** none of them widen the list pane or the page

#### Scenario: Long step title and message chip wrap inside the history detail
- **GIVEN** the history detail pane on a phone-portrait viewport showing a step
  whose title and message chips contain long, whitespace-free text
- **WHEN** the detail is rendered
- **THEN** `#history-view .history-step-title` and `#history-view .msg-chip`
  wrap their long text via `overflow-wrap: anywhere` / `word-break:
  break-word` rather than widening `.history-step` or the page

#### Scenario: Shared long in-stream content is routed into the already-protected classes
- **GIVEN** a history detail record carrying a long file path, a 200+ character
  whitespace-free string, raw JSON, or a step report list item, rendered
  through the shared `renderConversation` engine
- **WHEN** the History detail is rendered
- **THEN** the long content lands inside the shared wrapping-protected classes
  (e.g. `.conv-bubble`, `.record-body`, `.raw-json`, an `li` under
  `.step-report__list`, or `.msg-chip`) governed by the `running-flow-console`
  *Long-Content Wrapping* rules, so it wraps inside its container
- **AND** the History rendering path never routes such long content into a node
  with no wrapping protection

#### Scenario: Desktop History layout and identity stripe are unchanged
- **WHEN** `#history-view` is rendered on a viewport wider than the
  narrow-screen breakpoint
- **THEN** none of the `#history-view`-prefixed mobile overflow rules apply,
  and the desktop two-pane History layout, padding, and the non-flow
  history-list `.history-record` 3px left identity stripe are preserved
  byte-for-byte
- **AND** because every mobile rule is scoped inside `@media (max-width: 600px)`
  and prefixed with `#history-view`, neither `#flow-view` nor any other surface
  sharing the same class names is affected
