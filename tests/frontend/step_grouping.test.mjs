/*
 * Step grouping tests (Group G2).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub is
 * installed. Exposes `registerStepGroupingTests({app, check, findOne,
 * findAll})`.
 *
 * Coverage:
 *   (a) All records sharing one step_id — step_started + conversation +
 *       step_output + step_completed — collapse into a SINGLE visual step
 *       region (exactly one .history-step-header), in strict timestamp order.
 *   (b) step_completed / step_failed / step_output do NOT spawn a second,
 *       same-named step region.
 *   (c) A genuinely different step_id DOES start a new region.
 *   (d) The incremental-append path keeps one region (a step_completed
 *       arriving after the running anchor does not duplicate the header).
 *   (e) Every bubble in a step region carries the shared step-type-<type>
 *       identity class for per-step grouping styles.
 */
import assert from "node:assert/strict";

export function registerStepGroupingTests(ctx) {
  const { app, check, findOne, findAll } = ctx;

  const stepHeaders = (container) =>
    findAll(container, "history-step-header")
      .map((h) => { const t = findOne(h, "history-step-title"); return t ? t.textContent : ""; });

  const startedRecord = (stepId, stepType, ts) => ({
    type: "step_started", step_id: stepId, step_type: stepType,
    status: "running", timestamp: ts,
  });
  const chatRecord = (stepId, stepType, role, content, ts) => ({
    step_id: stepId, step_type: stepType,
    message: { role, content, timestamp: ts },
  });
  const outputRecord = (stepId, stepType, ts) => ({
    type: "step_output", step_id: stepId, step_type: stepType, timestamp: ts,
    data: { step: { step_id: stepId, step_type: stepType, status: "running", outputs: {} } },
  });
  const completedRecord = (stepId, stepType, ts, outputs = {}) => ({
    type: "step_completed", step_id: stepId, step_type: stepType, timestamp: ts,
    data: { step: { step_id: stepId, step_type: stepType, status: "completed", outputs } },
  });
  const failedRecord = (stepId, stepType, ts) => ({
    type: "step_failed", step_id: stepId, step_type: stepType, timestamp: ts,
    data: { step: { step_id: stepId, step_type: stepType, status: "failed",
      outputs: {}, error_message: "boom" } },
  });

  // ---- (a) one step_id → one region ---------------------------------------
  check("G2 step_started + chat + step_output + step_completed → one region", () => {
    const sid = "07_implement_abcd1234";
    const container = document.createElement("div");
    app.renderConversation(container, [
      startedRecord(sid, "implement", 1),
      chatRecord(sid, "implement", "assistant", "working", 2),
      outputRecord(sid, "implement", 3),
      completedRecord(sid, "implement", 4),
    ], false);
    assert.deepEqual(stepHeaders(container), ["IMPLEMENT"],
      "all same-step_id records must collapse into ONE region header");
    // The running anchor and the terminal report both live in that one region.
    assert.ok(findOne(container, "step-status-row"), "running anchor present");
    assert.ok(findOne(container, "step-report"), "terminal report card present");
  });

  // ---- (b) terminal/intermediate events make no new region ----------------
  check("G2 step_completed does not spawn a second same-named region", () => {
    const sid = "05_test_aa";
    const container = document.createElement("div");
    app.renderConversation(container, [
      startedRecord(sid, "test", 1),
      completedRecord(sid, "test", 2),
    ], false);
    assert.deepEqual(stepHeaders(container), ["TEST"]);
  });

  check("G2 step_failed does not spawn a second same-named region", () => {
    const sid = "05_test_aa";
    const container = document.createElement("div");
    app.renderConversation(container, [
      startedRecord(sid, "test", 1),
      failedRecord(sid, "test", 2),
    ], false);
    assert.deepEqual(stepHeaders(container), ["TEST"]);
  });

  check("G2 step_output does not spawn a second same-named region", () => {
    const sid = "06_self_check_aa";
    const container = document.createElement("div");
    app.renderConversation(container, [
      startedRecord(sid, "self_check", 1),
      outputRecord(sid, "self_check", 2),
    ], false);
    assert.deepEqual(stepHeaders(container), ["SELF CHECK"]);
  });

  // ---- (c) a different step_id DOES start a new region --------------------
  check("G2 a new step_id forms a new region", () => {
    const container = document.createElement("div");
    app.renderConversation(container, [
      startedRecord("05_test_aa", "test", 1),
      completedRecord("05_test_aa", "test", 2),
      startedRecord("06_commit_bb", "commit", 3),
      completedRecord("06_commit_bb", "commit", 4),
    ], false);
    assert.deepEqual(stepHeaders(container), ["TEST", "COMMIT"]);
  });

  // ---- (d) incremental append keeps one region ----------------------------
  check("G2 a step_completed appended after the anchor keeps one region", () => {
    const sid = "05_test_aa";
    const container = document.createElement("div");
    const records = [startedRecord(sid, "test", 1)];
    app.renderConversation(container, records, false);
    assert.deepEqual(stepHeaders(container), ["TEST"]);
    // Live append of the terminal event for the SAME step.
    records.push(completedRecord(sid, "test", 2));
    app.renderConversation(container, records, true);
    assert.deepEqual(stepHeaders(container), ["TEST"],
      "the terminal event must not create a duplicate region on append");
    // Both the running anchor and the report coexist in the one region.
    assert.ok(findOne(container, "step-status-row"));
    assert.ok(findOne(container, "step-report"));
  });

  // ---- (e) shared step-type identity class --------------------------------
  check("G2 every bubble in a region carries the step-type identity class", () => {
    const sid = "07_implement_abcd1234";
    const container = document.createElement("div");
    app.renderConversation(container, [
      startedRecord(sid, "implement", 1),
      chatRecord(sid, "implement", "assistant", "working", 2),
      completedRecord(sid, "implement", 3),
    ], false);
    // The running anchor, the chat bubble, and the step-event row all share
    // the step-type-implement class (header rows are excluded — they are
    // stateless separators).
    const tagged = findAll(container, "step-type-implement");
    assert.ok(tagged.length >= 3,
      `expected the anchor + chat + report rows tagged, got ${tagged.length}`);
  });
}
