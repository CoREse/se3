/*
 * Compacted-record rendering tests (record-budget group G4).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub
 * (`globalThis.document` / `FakeNode`) is installed. Exposes
 * `registerCompactedRecordChipTests({app, check, findOne, findAll})`.
 *
 * The daemon shrinks an oversized history record before it goes on the wire
 * (src/tianluo/daemon/record_budget.py). The shrinking was deliberately built so
 * that NO event is ever dropped, because the console reconciles tool chips
 * idempotently by `stepId#ordinal`: a chip missing from a frame never comes
 * back on a later one, so a budget that ate the tail would make the back half
 * of a step's tool calls vanish permanently from the conversation.
 *
 * This is the browser-side end of that contract. It renders a record in the
 * compacted shape and pins:
 *
 *   (a) chip count and order are exactly those of the uncompacted record —
 *       every `tool_use` still pairs with its `tool_result` by `tool_use_id`;
 *   (b) the folded-telemetry marker events produce NO chips of their own (they
 *       stand in for tens of thousands of zero-render events);
 *   (c) a shrunken body still renders, and carries the visible truncation
 *       marker so the user can tell a preview from the whole payload;
 *   (d) the tail — the last tool call and the terminal `result` event — is
 *       still there, which is the property a truncating degradation would lose.
 *
 * The marker literals are read back out of record_budget.py rather than
 * hardcoded twice: they are a cross-language wire contract, and a rename on the
 * Python side that this file did not follow would otherwise silently stop the
 * "content truncated" affordance from ever rendering.
 */
import assert from "node:assert/strict";
import fs from "node:fs";

const BUDGET_SRC = fs.readFileSync(
  new URL("../../src/tianluo/daemon/record_budget.py", import.meta.url),
  "utf8",
);

// Literals mirrored from record_budget.py; asserted against the source below.
const FOLDED_SUBTYPE = "tianluo_folded_telemetry";
const TRUNCATION_FLAG = "tianluo_truncated";
const truncationMarker = (dropped) => `\n\n[tianluo:truncated ${dropped} bytes]`;

const CHIP_COUNT = 206;

export function registerCompactedRecordChipTests(ctx) {
  const { app, check, findOne, findAll } = ctx;

  // ---- fixtures ---------------------------------------------------------

  const toolUseEvent = (index) => ({
    type: "assistant",
    message: {
      role: "assistant",
      content: [
        {
          type: "tool_use",
          id: `toolu_${String(index).padStart(4, "0")}`,
          name: "Bash",
          input: { command: `run step ${index}` },
        },
      ],
    },
  });

  const toolResultEvent = (index, body) => ({
    type: "user",
    message: {
      role: "user",
      content: [
        {
          tool_use_id: `toolu_${String(index).padStart(4, "0")}`,
          type: "tool_result",
          is_error: false,
          content: body,
        },
      ],
    },
  });

  const telemetryEvent = (index) => ({
    type: "system",
    subtype: "thinking_tokens",
    estimated_tokens: 50 * index,
    uuid: `uuid-${index}`,
  });

  const foldMarker = (count) => ({
    type: "system",
    subtype: FOLDED_SUBTYPE,
    count,
    kinds: ["system/thinking_tokens"],
  });

  const resultEvent = () => ({
    type: "result",
    subtype: "success",
    is_error: false,
    total_cost_usd: 1.8261305,
    usage: { input_tokens: 49, output_tokens: 14317 },
  });

  // The record as the step wrote it: a telemetry run before every chip, and a
  // tool_result body far larger than any preview needs to be.
  const originalEvents = () => {
    const events = [];
    let counter = 0;
    for (let chip = 0; chip < CHIP_COUNT; chip += 1) {
      for (let i = 0; i < 8; i += 1) events.push(telemetryEvent(counter++));
      events.push(toolUseEvent(chip));
      events.push(toolResultEvent(chip, `result body ${chip} `.repeat(200)));
    }
    events.push(resultEvent());
    return events;
  };

  // The same record in the shape the compactor hands to the wire: telemetry
  // runs replaced in place by ONE count marker each, oversized bodies cut to a
  // preview plus a marker, event ORDER untouched, no event removed.
  const compactedEvents = () => {
    const events = [];
    for (let chip = 0; chip < CHIP_COUNT; chip += 1) {
      events.push(foldMarker(8));
      events.push(toolUseEvent(chip));
      const whole = `result body ${chip} `.repeat(200);
      const kept = whole.slice(0, 120);
      const shrunk = toolResultEvent(
        chip, kept + truncationMarker(whole.length - kept.length),
      );
      shrunk[TRUNCATION_FLAG] = whole.length - kept.length;
      events.push(shrunk);
    }
    events.push(resultEvent());
    return events;
  };

  const record = (events) => ({
    step_id: "01_discovery",
    step_type: "discovery",
    ordinal: 7,
    message: {
      role: "assistant",
      content: "",
      timestamp: 100,
      attempt: 0,
      raw_json: events,
    },
  });

  const chipIds = (events) =>
    (app.extractAssistantChipEvents(events) || [])
      .filter((evt) => evt.kind === "chip")
      .map((evt) => `${evt.toolUseId}:${evt.status}`);

  // ---- (0) the wire contract this file renders --------------------------

  check("(0) compaction marker literals still match record_budget.py", () => {
    assert.ok(
      BUDGET_SRC.includes(`FOLDED_EVENT_SUBTYPE = "${FOLDED_SUBTYPE}"`),
      "folded-telemetry marker subtype renamed on the Python side",
    );
    assert.ok(
      BUDGET_SRC.includes(`TRUNCATION_FLAG_KEY = "${TRUNCATION_FLAG}"`),
      "event truncation flag renamed on the Python side",
    );
    assert.ok(
      BUDGET_SRC.includes("[tianluo:truncated {dropped} bytes]"),
      "truncation marker text renamed on the Python side",
    );
  });

  // ---- (a) chip count and order survive ---------------------------------

  check("(a) compacted record yields the same chips, in the same order", () => {
    const before = chipIds(originalEvents());
    const after = chipIds(compactedEvents());
    assert.equal(before.length, CHIP_COUNT,
      "fixture should carry one chip per tool call");
    assert.deepEqual(after, before,
      "compaction changed the chip sequence the console renders");
    assert.equal(after[after.length - 1], `toolu_${String(CHIP_COUNT - 1).padStart(4, "0")}:success`,
      "the LAST tool call is still paired — the tail is what a truncating budget loses");
  });

  check("(a2) every chip renders in the DOM, none in-flight", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [record(compactedEvents())], false);
    const chips = findAll(container, "tool-marker");
    assert.equal(chips.length, CHIP_COUNT,
      "one rendered chip per preserved tool call");
    const inflight = chips.filter((c) => c.classList.contains("in-flight"));
    assert.equal(inflight.length, 0,
      "every tool_use still found its tool_result after compaction");
  });

  // ---- (b) fold markers are inert ---------------------------------------

  check("(b) folded-telemetry markers produce no chips of their own", () => {
    const events = [
      foldMarker(46163),
      toolUseEvent(0),
      toolResultEvent(0, "ok"),
      foldMarker(12),
      resultEvent(),
    ];
    assert.deepEqual(chipIds(events), ["toolu_0000:success"]);

    const container = document.createElement("div");
    app.renderConversation(container, [record(events)], false);
    assert.equal(findAll(container, "tool-marker").length, 1,
      "a count marker standing in for 46163 events must not render as a chip");
  });

  // ---- (c) truncated bodies stay visible AND stay marked ----------------

  check("(c) a shrunken tool_result still renders its preview with the marker", () => {
    const whole = "stdout line ".repeat(400);
    const kept = whole.slice(0, 200);
    const shrunk = toolResultEvent(0, kept + truncationMarker(whole.length - kept.length));
    shrunk[TRUNCATION_FLAG] = whole.length - kept.length;

    const container = document.createElement("div");
    app.renderConversation(
      container, [record([toolUseEvent(0), shrunk, resultEvent()])], false,
    );
    const chip = findOne(container, "tool-marker");
    assert.ok(chip, "the shrunken tool call still renders a chip");
    assert.equal(chip.classList.contains("success"), true);
    const panel = findOne(chip, "tool-marker-details");
    assert.ok(panel, "shrunken chip still carries its detail panel");
    const body = findOne(panel, "tool-marker-details-body");
    assert.ok(body.textContent.includes("stdout line"),
      "the surviving preview text is rendered");
    assert.ok(body.textContent.includes("[tianluo:truncated"),
      "the truncation marker is visible, so a preview is not mistaken for the whole payload");
  });

  check("(c2) an untouched body carries no truncation marker", () => {
    const container = document.createElement("div");
    app.renderConversation(
      container,
      [record([toolUseEvent(0), toolResultEvent(0, "all of it"), resultEvent()])],
      false,
    );
    const body = findOne(
      findOne(findOne(container, "tool-marker"), "tool-marker-details"),
      "tool-marker-details-body",
    );
    assert.equal(body.textContent.includes("[tianluo:truncated"), false);
  });

  // ---- (d) idempotent reconcile across a redelivery ---------------------

  check("(d) redelivering the compacted record converges to one bubble of chips", () => {
    // The same physical line arrives twice (REST ∩ WS overlap). Keyed by
    // stepId#ordinal it must converge, not double every chip.
    const container = document.createElement("div");
    const rec = record(compactedEvents());
    app.renderConversation(container, [rec], false);
    app.renderConversation(container, [rec], true);
    assert.equal(findAll(container, "tool-marker").length, CHIP_COUNT);
  });
}
