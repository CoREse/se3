<!-- spec-format: v1 -->
# user-interjection-handling Specification

## Purpose

The `user-interjection-handling` subsystem owns the end-to-end lifecycle of a
user interjection — an additional instruction the operator inserts mid-flow,
either via a Ctrl-C interrupt at the CLI terminal or via the web console's
docked reply box (which routes through the daemon as an `interjection`-kind
call file under `se3/calls/`). The subsystem covers four responsibilities,
each pinned to a Requirement below:

1. **Composition** — the central renderer
   (`compose_task_description_with_interjections`) that folds the persisted
   `flow.state.context["user_interjections"]` list into a step's effective
   `task_description`, producing one and only one
   `## Additional Instructions (added during run)` section so re-composition
   against the same base never nests or doubles. Both `run.py` (immediate
   re-run path) and `state_machine._build_step_inputs` (every subsequent
   step's input build) MUST route through this single renderer.
2. **Ingestion of web-console interjections** — `run.py:_drain_pending_interjections`
   consumes daemon-queued `interjection`-kind call files, appends an entry to
   `flow.state.context["user_interjections"]` (same entry shape as a Ctrl-C
   interjection), and recomposes the current step's `task_description`.
3. **Tick-driven drain during PAUSED waits** — while a flow is PAUSED waiting
   on a prompt response (discovery continue, confirm-step prompt, non-interactive
   discovery confirm gate), the drain MUST fire on every poll tick, not only
   at step boundaries, so a web-console interjection that arrives during the
   wait reaches the user_interjections list (and the chat history) immediately
   rather than after the operator sends the next reply.
4. **History persistence + LLM-reply prefix injection** — each drained
   interjection is recorded as a `{role: "user", kind: "interjection", …}`
   line in the current step's history jsonl so `se3 history show` and the web
   console see the user bubble at the point the interjection arrived; on
   DISCOVERY-PAUSED steps the drained text is also buffered into a per-flow
   `_pending_paused_interjections` list which the discovery reply path drains
   via `_consume_paused_interjection_prefix` to prefix `[interjection: …]\n`
   lines onto the next LLM user message.

The module owns the exact rendered format of the appended
`## Additional Instructions (added during run)` section so that all call
sites produce byte-identical output. Web-console lifecycle UI (chip
`pending` / `consumed` events, send-button settle gate, toast wording) is
the responsibility of `running-flow-console`; this spec owns the backend
ingestion, persistence, and LLM-prompt composition.

## Requirements

### Requirement: Composer entry point

The module exposes a single public function `compose_task_description_with_interjections(base, interjections)` that takes the canonical base task description and an iterable of interjection mappings and returns a string. It is the sole renderer of the appended interjection section; both `run.py` and `state_machine` MUST route through it to guarantee identical output.

#### Scenario: Returns string output
- **WHEN** the function is invoked with any combination of `base` and `interjections`
- **THEN** the return value is a `str`
- **AND** no exception is raised for empty, missing, or malformed entries

### Requirement: Empty / no-op cases preserve base

When there is nothing meaningful to render, the function returns the base verbatim (or empty), and never emits a bare section header.

#### Scenario: Empty interjections iterable returns base unchanged
- **WHEN** `interjections` is an empty iterable
- **THEN** the result equals `base` unchanged

#### Scenario: Empty base and empty interjections returns empty string
- **WHEN** both `base` is `""` and `interjections` is empty
- **THEN** the result is `""`

#### Scenario: `None` base coerces to empty
- **WHEN** `base` is falsy (e.g. `None` or `""`) and `interjections` is empty
- **THEN** the result is `""` (never `None`)

#### Scenario: Only-unusable entries fall back to base
- **WHEN** every entry in `interjections` is skipped (not a Mapping, missing `text`, or `text` is whitespace-only)
- **THEN** the result equals `base or ""` and the `## Additional Instructions` header is NOT emitted

### Requirement: Entry shape and validation

Each interjection entry is a `Mapping` with the optional keys `text`, `step_type`, and `timestamp`. The composer is defensive: non-Mapping entries are silently skipped, and only entries with non-whitespace `text` are rendered.

#### Scenario: Non-Mapping entries are skipped
- **WHEN** `interjections` contains a non-Mapping element (e.g. a string, `None`, a list)
- **THEN** that element is ignored
- **AND** remaining valid entries are still rendered

#### Scenario: Entry with missing or empty text is skipped
- **WHEN** an entry's `text` is missing, `None`, `""`, or only whitespace
- **THEN** that entry contributes nothing to the output
- **AND** it does not produce an empty bullet

#### Scenario: Text is stripped of surrounding whitespace
- **WHEN** an entry's `text` has leading or trailing whitespace
- **THEN** the rendered bullet uses the `.strip()`ped text

### Requirement: Bullet rendering and prefix format

Each retained entry becomes a Markdown bullet line `- {prefix}{text}`. The prefix encodes `step_type` and/or `timestamp` in a `[...]` form so that downstream readers can attribute when each interjection was injected.

#### Scenario: Both step_type and timestamp present
- **WHEN** an entry has non-empty `step_type` and `timestamp`
- **THEN** the bullet is rendered as `- [{step_type}@{timestamp}] {text}`

#### Scenario: Only step_type present
- **WHEN** an entry has non-empty `step_type` and a missing/empty `timestamp`
- **THEN** the bullet is rendered as `- [{step_type}] {text}`

#### Scenario: Only timestamp present
- **WHEN** an entry has a non-empty `timestamp` and a missing/empty `step_type`
- **THEN** the bullet is rendered as `- [{timestamp}] {text}`

#### Scenario: Neither step_type nor timestamp present
- **WHEN** both `step_type` and `timestamp` are missing or empty
- **THEN** the bullet is rendered as `- {text}` with no bracketed prefix

#### Scenario: Step / timestamp values are stripped
- **WHEN** `step_type` or `timestamp` have surrounding whitespace
- **THEN** the values used in the prefix are the `.strip()`ped values
- **AND** a `step_type` or `timestamp` consisting only of whitespace is treated as absent

### Requirement: Section header and layout

When at least one entry is renderable, the composer appends a single `## Additional Instructions (added during run)` Markdown section, separated from the base by a blank line, with one bullet per entry in input order.

#### Scenario: Non-empty base with renderable entries
- **WHEN** `base` is non-empty (after `rstrip`) and at least one entry is renderable
- **THEN** the output is exactly `{base.rstrip()}\n\n## Additional Instructions (added during run)\n\n{bullets joined by newline}`

#### Scenario: Empty / whitespace-only base with renderable entries
- **WHEN** `base` is empty or contains only trailing whitespace (such that `base.rstrip()` is `""`)
- **THEN** the output starts with `## Additional Instructions (added during run)\n\n` followed by the bullets, with no leading newlines

#### Scenario: Trailing whitespace on base is trimmed before joining
- **WHEN** `base` ends with newlines or spaces
- **THEN** the base portion of the output is `base.rstrip()` (no extra blank lines between base and the section header beyond the single `\n\n` separator)

#### Scenario: Bullet ordering preserved
- **WHEN** multiple renderable entries are supplied
- **THEN** their bullets appear in the order they were yielded by the input iterable, joined by `\n`

#### Scenario: Exactly one section header per composition
- **WHEN** the composer is invoked with any non-empty set of renderable interjections
- **THEN** the output contains the literal `## Additional Instructions` exactly once

### Requirement: Deterministic / re-composable output

Because both the re-run path (`run.py:_handle_step_interrupt`) and the propagation path (`state_machine._build_step_inputs`) recompose against the original base task description, calling the composer repeatedly against the same `base` with a growing `interjections` list MUST yield a single section — never a nested or doubled one. The function is pure: it does not mutate `base` or `interjections`, performs no I/O, and depends only on its arguments.

#### Scenario: Repeated composition against original base does not nest sections
- **WHEN** the composer is called with the canonical original `base` and an `interjections` list that has grown (e.g., after a second Ctrl-C interjection appended)
- **THEN** the output contains exactly one `## Additional Instructions` header
- **AND** every interjection's text appears exactly once in input order

#### Scenario: Byte-identical output across call sites
- **WHEN** `run.py` and `state_machine._build_step_inputs` invoke the composer with the same `base` and same `interjections` sequence
- **THEN** both produce byte-identical strings (the module is the sole owner of the rendered format)

#### Scenario: Pure function — no mutation of inputs
- **WHEN** the composer is invoked
- **THEN** `base` is not modified, the `interjections` iterable's underlying entries are not mutated, and no global state is touched

### Requirement: Iterable input contract

The `interjections` parameter is typed as `Iterable[Mapping[str, Any]]`. The composer iterates it exactly once and tolerates `None` in place of an iterable.

#### Scenario: `None` interjections treated as empty
- **WHEN** `interjections` is `None`
- **THEN** the function behaves as if it were an empty iterable (no exception, returns `base or ""`)

#### Scenario: Generators are acceptable
- **WHEN** `interjections` is a single-use iterable (generator)
- **THEN** the composer iterates it exactly once and produces the correct output

### Requirement: Web-Console Interjection Ingestion

A web-console interjection MUST reach the same `flow.state.context["user_interjections"]`
list that a Ctrl-C interjection populates, so all downstream composition,
propagation, and rendering behavior is identical regardless of origin. The
transport is: the server's `POST /api/flows/{flow_id}/interject` endpoint
forwards the text to the daemon, the daemon writes an `interjection`-kind
call file under `se3/calls/` (with `context.flow_id` set to the target flow),
and the run loop's `_drain_pending_interjections` helper enumerates those
files via `interaction_calls.drain_interjection_requests`, writes the
sibling `.response` marker, unlinks the call file, and appends one
`user_interjections` entry per drained text.

Each appended entry MUST carry at least `text`, `step_id`, `step_type`,
`timestamp`, and `source` (set to `"web-console"` for web-console-originated
interjections), matching the shape the composer's bullet renderer expects.
After all drained entries are appended, the current step's
`inputs["task_description"]` MUST be recomposed via
`compose_task_description_with_interjections` against the canonical
`_effective_task_description_base(flow)` so the next LLM call / step
re-run sees the updated description; `persistence.save_flow(flow)` MUST be
called so a daemon-spawned flow's state on disk reflects the new entries
before the run loop resumes.

The drain MUST be idempotent: drained call files are removed (after writing
their sibling `.response`) so a subsequent drain sees no leftover. Drain
errors (I/O, parse) MUST be logged and swallowed — they MUST NOT propagate
out of `_drain_pending_interjections` and break the flow.

#### Scenario: Web-console interjection is folded into user_interjections
- **GIVEN** a running flow `F1` whose run loop calls
  `_drain_pending_interjections`
- **WHEN** the daemon has written one or more `interjection`-kind call files
  under `se3/calls/` carrying `context.flow_id == "F1"`
- **THEN** each call file is enumerated, its sibling `.response` marker is
  written, the call file is unlinked, and one entry per drained text is
  appended to `flow.state.context["user_interjections"]`
- **AND** each entry includes `text`, `step_id`, `step_type`, `timestamp`,
  and `source = "web-console"`
- **AND** the current step's `inputs["task_description"]` is recomposed via
  `compose_task_description_with_interjections` against the canonical base
  task description, and the flow is persisted

#### Scenario: Drain is idempotent and never propagates errors
- **WHEN** `_drain_pending_interjections` is called twice in succession with
  no new call files written between the two invocations
- **THEN** the second call drains zero interjections and is a no-op
- **AND** if `drain_interjection_requests` raises, the exception is logged
  and swallowed, and `_drain_pending_interjections` returns `[]` instead of
  propagating the error to the run loop

### Requirement: Tick-Driven Drain During PAUSED Waits

While a flow is PAUSED waiting on a prompt response — discovery continue,
the non-interactive discovery confirm gate, or the CONFIRM step's review
prompt — the run loop's normal step-boundary drain does not fire because
the loop is blocked on input. To keep a web-console interjection from
silently piling up until the operator sends the next reply, the relevant
PAUSED handlers (`_handle_discovery_pause`,
`_handle_discovery_pause_noninteractive`, `_handle_confirm_pause`) MUST
drain interjections both **on entry to the pause** and **on every poll
tick** of the input/dual-wait loop, by invoking
`_drain_pending_interjections(flow, project_root, persistence)` from a
`_tick()` callback (or equivalent per-poll hook). The drain MUST NOT be
gated on step boundaries, the LLM being idle, or the operator having
already sent a reply.

This guarantees that a web-console interjection sent while the flow is
waiting on a prompt response is reflected in `user_interjections` (and in
the per-step history jsonl, per the *History Persistence of Interjections*
requirement) within one poll interval of being written — typically
sub-second once the daemon's fast-push wakes the loop — so the operator's
next reply can carry the resulting `[interjection: …]\n` prefix per the
*PAUSED Reply Prefix Injection* requirement.

#### Scenario: Drain fires on every PAUSED poll tick, not just step boundaries
- **GIVEN** a flow is PAUSED in `_handle_discovery_pause` (interactive or
  non-interactive) or `_handle_confirm_pause`, blocked on operator input
- **WHEN** a web-console interjection arrives during the wait, materializing
  an `interjection`-kind call file under `se3/calls/`
- **THEN** the next poll tick of the input/dual-wait loop invokes
  `_drain_pending_interjections`, the call file is consumed, and the
  resulting entry is appended to `flow.state.context["user_interjections"]`
- **AND** the drain does NOT wait for the operator to send a reply, nor
  for the next step boundary

#### Scenario: Entry drain folds in pre-existing interjections
- **GIVEN** an `interjection`-kind call file was written while the flow was
  briefly idle, immediately before it entered a PAUSED handler
- **WHEN** the PAUSED handler is entered
- **THEN** the handler invokes `_drain_pending_interjections` once on entry
  (before the first poll tick) so the queued interjection is folded in
  ahead of the dual-wait that blocks the operator

### Requirement: History Persistence of Interjections

Every drained interjection (regardless of origin) MUST be appended as a
single JSON line to the current step's history file at
`se3/history/{flow_id}/{step_id}.jsonl` via
`chat_history.record_user_interjection`. The line MUST take the shape
`{role: "user", kind: "interjection", content: <text>, raw_json: [],
timestamp, step_type, attempt, source}` where `attempt` is the current
step's `retry_count` (or `0` when unavailable) and `source` defaults to
`"webui"` (callers MAY override, e.g. `_drain_pending_interjections` writes
`source = "web-console"`).

`get_step_history` MUST deserialize these lines back into `ChatMessage`
instances carrying `kind == "interjection"` so `se3 history show` and the
web console render them inline as user bubbles in chronological order
alongside the LLM turns. `format_history_for_retry` MUST skip records with
`kind == "interjection"` so the LLM retry prompt does NOT re-ingest user
interjections as additional `[User Prompt]:` turns — the interjection has
already been folded into the current step's `task_description` via the
composer, and re-injecting it as a retry-context user turn would duplicate
the instruction.

A missing `flow_id` or `step_id` (e.g. when the flow has no current step)
MUST be a soft no-op: the helper logs a warning and returns without
raising. I/O failures during the append (`OSError`) MUST be logged and
swallowed; they MUST NOT propagate out of `_drain_pending_interjections`
and break the run loop.

#### Scenario: Drained interjection writes a user/interjection line to step jsonl
- **GIVEN** `_drain_pending_interjections` is consuming a web-console
  interjection for flow `F1`, current step `S1` of type `discovery`
- **WHEN** the drain processes the call file
- **THEN** one JSON line of shape
  `{role: "user", kind: "interjection", content: <text>, raw_json: [],
  timestamp, step_type: "discovery", attempt: <retry_count or 0>,
  source: "web-console"}` is appended to
  `se3/history/F1/S1.jsonl`
- **AND** `se3 history show F1` renders the line as a user bubble between
  the surrounding LLM turns

#### Scenario: format_history_for_retry skips interjection records
- **GIVEN** a step's history jsonl contains both regular user/assistant
  LLM turns and one or more `{role: "user", kind: "interjection", …}` lines
  written by `record_user_interjection`
- **WHEN** `format_history_for_retry` builds the retry context for the next
  LLM attempt of the same step
- **THEN** every record whose `kind` equals `"interjection"` is omitted
  from the retry context
- **AND** the retry prompt is NOT inflated with `[User Prompt]:` blocks
  that duplicate text already composed into `task_description`

#### Scenario: Missing flow_id or step_id is a soft no-op
- **WHEN** `record_user_interjection` is called with an empty `flow_id` or
  empty `step_id`
- **THEN** the helper logs a warning and returns without raising
- **AND** no jsonl line is appended

### Requirement: PAUSED Reply Prefix Injection

When a web-console interjection is drained while the current step is in a
PAUSED state of type `discovery`, `_drain_pending_interjections` MUST
additionally buffer the interjection's text into
`flow.state.context["_pending_paused_interjections"]` (a list of plain
strings, oldest first). The discovery reply paths
(`_handle_discovery_pause`, `_handle_discovery_pause_noninteractive`) MUST
call `_consume_paused_interjection_prefix(flow)` just before handing the
operator's reply to the LLM as the next user turn; the helper MUST return
the buffered texts joined as one `[interjection: <text>]\n` line per
entry, clear the buffer, and persist the flow.

The PAUSED reply prefix scope is restricted to `discovery` pauses by
construction. CONFIRM pauses do NOT drive an LLM prompt reply — the
operator's CONFIRM answer is a structured approve/feedback payload
consumed by the next implement/test iteration, not a free-form prompt to
the LLM — so buffering during CONFIRM pauses would leak a stale prefix
into a later DISCOVERY pause's LLM call. `_drain_pending_interjections`
MUST therefore populate `_pending_paused_interjections` only when the
current step is PAUSED AND its `step_type` is `DISCOVERY`; for CONFIRM and
any other non-discovery PAUSED step the prefix buffer is left untouched
and the interjection reaches the LLM solely through the
`task_description` recomposition path.

When the prefix is consumed it MUST be prepended to the operator's reply
verbatim (`prefix + response`), preserving the operator's literal reply
underneath the `[interjection: …]\n` lines, so the LLM sees the
interjection(s) first and the operator's actual reply text immediately
after.

#### Scenario: DISCOVERY-paused drain buffers the prefix
- **GIVEN** flow `F1` is in a `discovery` step `S1` with `status == PAUSED`
- **WHEN** `_drain_pending_interjections` processes a web-console
  interjection with text `T`
- **THEN** the same call appends `T` to
  `flow.state.context["_pending_paused_interjections"]`, in addition to
  the normal `user_interjections` append + `task_description` recompose
- **AND** the subsequent invocation of `_consume_paused_interjection_prefix`
  returns `"[interjection: T]\n"`, clears the buffer, and persists the flow

#### Scenario: Multiple buffered interjections are joined in arrival order
- **GIVEN** the discovery pause has buffered two interjections `T1` then `T2`
  (in that order) before the operator sends a reply `R`
- **WHEN** the discovery reply path calls
  `_consume_paused_interjection_prefix(flow)` and prepends the result to `R`
- **THEN** the LLM receives the user turn
  `"[interjection: T1]\n[interjection: T2]\n" + R` (interjections first, in
  arrival order, followed by the operator's literal reply text)
- **AND** after consumption the buffer is empty and a second consume call
  returns `""`

#### Scenario: CONFIRM pauses do not populate the prefix buffer
- **GIVEN** flow `F1` is in a `confirm` step with `status == PAUSED`
- **WHEN** `_drain_pending_interjections` processes a web-console
  interjection
- **THEN** `flow.state.context["_pending_paused_interjections"]` is NOT
  appended to; the interjection still flows through the
  `user_interjections` list + `task_description` recomposition
- **AND** a later DISCOVERY pause's
  `_consume_paused_interjection_prefix(flow)` returns `""` (no stale
  prefix leaks from the prior CONFIRM pause)

#### Scenario: Empty buffer yields empty prefix without mutating flow shape
- **WHEN** `_consume_paused_interjection_prefix` is called on a flow whose
  `state.context` has no `_pending_paused_interjections` key, or whose
  buffer is `[]`
- **THEN** the helper returns `""`
- **AND** it tolerates a missing `state.context` shape (e.g. a unit-test
  stub) without raising