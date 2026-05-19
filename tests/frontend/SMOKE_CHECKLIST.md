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
- [ ] With **no pending interaction**, the input is **disabled** and shows an
      explanation (e.g. "No interaction awaiting a response").
- [ ] With a **pending interaction**, the input is **enabled** and clearly
      states its context (what is being answered, which interaction).
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

## Regression

- [ ] `pytest` (full suite) passes — see `tests/test_interaction_calls.py`
      and `tests/test_server_interjection.py` for the automated coverage.
