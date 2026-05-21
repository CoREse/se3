# Running-Flow Chat View — Manual Smoke Checklist

Manual acceptance pass for the web console's running-flow interaction surface
(the full-screen chat view that replaces the old 440px drawer + context-free
call modal). Run after any change to `src/se3/server/static/` or to the
interaction-call backend (`protocol.py`, `aggregator.py`,
`engine/interaction_calls.py`, `commands/run.py`).

## Setup

1. `pip install -e '.[server]'`
2. Start the central server: `se3-server --port 8080`
3. Start a daemon dialed in: `se3 daemon start --server-url ws://127.0.0.1:8080`
4. From a project, start a flow: `se3 run "<some task>"`
5. Open `http://127.0.0.1:8080/` and select the running flow.

## Pure-function regression

- [ ] `node tests/frontend/interaction_view.test.mjs` reports `0 failed`.

## Layout

- [ ] The running flow opens as a **full-screen view** (parity with the
      history view) — no 440px right-side drawer.
- [ ] The **conversation is the scrollable main column**; Overview / Steps /
      machine info live in a **side panel** and do not compete with the chat
      for width.
- [ ] There is **no separate context-free call modal** anywhere — clicking a
      pending interaction never pops an out-of-band dialog.

## Reply input box

- [ ] A reply input box is **always present at the bottom** of the view — no
      button or modal is needed to start replying.
- [ ] The **textarea is always enabled** so the user can draft text at any
      time (chat-application parity); the **Send button is the gate**.
- [ ] With **no pending interaction**, the textarea is enabled but Send is
      **disabled**, and the placeholder hints there is no target yet (e.g.
      "暂无待处理项 — 你可以先草拟回复,或点击 ✎ 插话…").
- [ ] An inline **Interject (✎) icon button** sits at the left of the reply
      row (symmetric to Send); clicking it materializes a synthetic
      `interjection` chip and enables Send.
- [ ] With a **pending interaction** selected, Send is **enabled** and the
      context panel above clearly states what is being answered.
- [ ] After submitting, the reply is **folded into the conversation in place**.

## Conversation rendering

- [ ] `assistant` records (real output) and intervention items render
      **expanded and visually prominent by default**.
- [ ] `user` / `system` prompt-template records render **collapsed to a
      one-line chip** (e.g. `system prompt · discovery mode`).
- [ ] Clicking a chip **expands the original text**; nothing is deleted.
- [ ] `human`-role records are bucketed with `user` (collapsed chip).
- [ ] Collapse/expand is correct even when a `system` and an `assistant`
      record carry identical text — classification uses the structured
      `role` field, never text matching.

## Intervention items (one per kind, default-expanded & prominent)

- [ ] **`call`** — a pending MCP call appears as a distinct, prominent,
      default-expanded item; replying answers it and the answer joins the
      conversation.
- [ ] **`interjection`** — after `Ctrl-C` on the CLI (or pushing a message
      from the console), the mid-flow interjection appears as its own item
      and is consumed at the next step boundary.
- [ ] **`retry_decision`** — when a step fails with **no TTY** (a
      daemon-spawned flow), a retry/skip/abort decision item appears; the
      flow pauses until answered.
- [ ] **`cli_confirm`** — a CLI subprocess confirmation prompt (e.g.
      "press 1 to confirm") surfaces as an item and can be answered.
- [ ] Intervention items are **visually distinct** from ordinary chat
      messages and never blend into the message stream.

## Standards 1–5 CLI ↔ Web parity (G6 acceptance)

Step-by-step parity pass: run one full `se3 run` (with `--discover`) and, for
each step, compare what the CLI Rich output shows against what the web
`#flow-view` shows. Every standard below has automated coverage that pins the
chain hop-by-hop; the manual pass confirms the rendered result matches.

- [ ] **Standard 1 — user + assistant default-visible.** The user's literal
      input and every `assistant` output render **default-expanded** in the
      web conversation, matching what the CLI prints directly. se3-injected
      template/system boilerplate is collapsed into a one-line chip.
      *Backed by:* `tests/frontend/test_app_pure.mjs` (`isCollapsibleRole`,
      assistant-expanded, `renderUserMarkerRecord`).
- [ ] **Standard 2 — user-original-input extraction.** The default-expanded
      `user` bubble shows **only** the literal user input; Project Context /
      Available Specs / Discovery Context / JSON scaffolding / Guidelines /
      READ-ONLY etc. live in the collapsed chip. Driven by the three-segment
      markers (`TEMPLATE_PREFIX_END` / `USER_CONTENT_BEGIN` /
      `USER_CONTENT_END`) which are **persisted into history**, not only sent
      to the LLM.
      *Backed by:* `tests/test_discovery_prompt_markers.py` (markers persist
      with `role="user"` through `record_prompt` → `get_step_history`);
      `tests/frontend/test_app_pure.mjs` (`splitUserPromptByMarker`
      three-/two-segment/no-marker paths).
- [ ] **Standard 3 — discovery confirmation entry.** When discovery proposes a
      refined description, the web shows the **"输入 1 确认"** textual hint
      **and** a GUI confirm button whose click sends the literal `"1"` through
      the existing reply channel (text hint as fallback).
      *Backed by:* `tests/test_running_flow_console_chain.py`
      (`test_discovery_confirm_call_payload_kind_options_context`,
      `…surfaces_via_aggregator_scoped_with_options`,
      `…submission_gates_on_one`); `tests/test_discovery_noninteractive.py`.
- [ ] **Standard 4 — per-step default-expanded final card (incl. finished
      steps).** Every terminal step — DISCOVERY / CONFIRM / PLAN / ANALYZE /
      TEST / SELF_CHECK / VERIFY_SPEC / UPDATE_SPEC / VERSION_ANALYZE /
      SUMMARIZE — emits `step_completed`; `HistorySink` persists `outputs`; the
      daemon incremental reader surfaces it; the web renders a
      **default-expanded `.step-report` card with no `max-height`** via
      `STEP_REPORT_RENDERERS[step_type]` (field parity with CLI
      `step_renderers.py`). A step that is **already finished** still produces
      its card.
      *Backed by:* `tests/test_running_flow_console_chain.py` (terminal-event
      emission for CONFIRM/DISCOVERY/PLAN/SUMMARIZE, `record_step_event`
      shape, `get_step_history` skips event records, frontend-consumable
      `make_history_data` shape).
- [ ] **Standard 5 — summarize assistant visible.** The summarize step lands
      **both** its user prompt and assistant markdown into the per-step jsonl
      and pushes them incrementally; the web shows `user + assistant + Work
      Summary card`.
      *Backed by:* `tests/test_running_flow_console_chain.py`
      (`test_summarize_records_user_and_assistant_to_jsonl`,
      `test_summarize_records_incrementally_readable_in_frontend_shape`).

## Regression

- [ ] `pytest` (full suite, `testpaths = ["tests"]`) passes — see
      `tests/test_running_flow_console_chain.py` (21 end-to-end chain tests),
      `tests/test_interaction_calls.py`, and `tests/test_server_interjection.py`
      for the automated coverage.
- [ ] `node tests/frontend/test_app_pure.mjs` and
      `node tests/frontend/interaction_view.test.mjs` both report all checks
      passing.

### Recorded automated results (2026-05-21)

| Suite | Result |
|-------|--------|
| `pytest` full suite (`tests/`) | 4642 passed, 10 skipped |
| `tests/test_running_flow_console_chain.py` | 21 passed |
| `tests/frontend/test_app_pure.mjs` | 54 checks passed |
| `tests/frontend/interaction_view.test.mjs` | 22 passed, 0 failed |

> Note: `src/se3/engine/test_steps.py` has 5 pre-existing failures (step
> sequence / commit / test expectations) that are **outside** the default
> `testpaths = ["tests"]` suite and **unrelated** to the running-flow-console
> chain — none of `models.py`, `steps/test.py`, or `steps/commit.py` were
> touched by this work (verified via `git diff` against the pre-work base).
