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

  // ==========================================================================
  // Group G3: final report card "结果/总结" semantic titling.
  //
  // The final report card of a step must read as that step's *result* (结果) /
  // *summary* (总结), never as a bare step name that a reader could mistake for
  // a brand-new step heading (the IMPLEMENT ambiguity). These cover:
  //   (f) reportCardTitle is a pure `<步骤> · 结果/总结` builder; summarize → 总结.
  //   (g) IMPLEMENT's card title carries the explicit 结果/总结 semantic.
  //   (h) NO step's report-card title equals its bare step-region (header) title.
  //   (i) The rendered card DOM's title carries the suffix and never matches a
  //       .history-step-header step title.
  //   (j) History view and running-flow view (shared renderConversation) title
  //       the same step's card identically.
  // ==========================================================================

  // Every StepType that has a header label — the bare step-region titles a card
  // must never collide with.
  const ALL_STEP_TYPES = Object.keys(app.STEP_HEADER_TITLES);
  const SUFFIX_RE = /[·]\s*(结果|总结)\s*$/;

  // ---- (f) reportCardTitle pure builder -----------------------------------
  check("G3 reportCardTitle builds `<步骤> · 结果/总结` with a result/summary suffix", () => {
    for (const t of ALL_STEP_TYPES) {
      const title = app.reportCardTitle(t);
      assert.match(title, SUFFIX_RE,
        `reportCardTitle(${t}) must end with · 结果 or · 总结, got "${title}"`);
      // The base label is the title-case STEP_REPORT_TITLES entry, distinct
      // from the uppercase step-header label.
      assert.ok(title.startsWith(app.STEP_REPORT_TITLES[t] + " · "),
        `reportCardTitle(${t}) must prefix the title-case report label`);
    }
    // summarize (itself a summary step) reads 总结; a non-summary step reads 结果.
    assert.ok(app.reportCardTitle("summarize").endsWith("· 总结"),
      "summarize card reads 总结");
    assert.ok(app.reportCardTitle("implement").endsWith("· 结果"),
      "implement card reads 结果");
    // Unknown step type degrades without throwing and still carries a suffix.
    assert.match(app.reportCardTitle("totally_unknown"), SUFFIX_RE);
    assert.match(app.reportCardTitle(""), SUFFIX_RE);
  });

  // ---- (g) IMPLEMENT card title is unambiguous result/summary -------------
  check("G3 IMPLEMENT report-card title carries explicit 结果/总结 semantic", () => {
    const title = app.reportCardTitle("implement");
    assert.match(title, SUFFIX_RE,
      `implement card title must be result/summary, got "${title}"`);
    // It must NOT be the bare IMPLEMENT region heading.
    assert.notStrictEqual(title, app.STEP_HEADER_TITLES.implement);
    // Nor the bare title-case label with no semantic word.
    assert.notStrictEqual(title, app.STEP_REPORT_TITLES.implement);
  });

  // ---- (h) no card title equals its bare step-region title ----------------
  check("G3 no report-card title equals its bare step-region (header) title", () => {
    for (const t of ALL_STEP_TYPES) {
      const cardTitle = app.reportCardTitle(t);
      const headerTitle = app.stepHeaderLabel(t);
      assert.notStrictEqual(cardTitle, headerTitle,
        `report card title for ${t} must differ from its region header "${headerTitle}"`);
      // Also must not collide with ANY step-region header label (so it is never
      // read as the start of a (different) step).
      assert.ok(!ALL_STEP_TYPES.some((o) => app.STEP_HEADER_TITLES[o] === cardTitle),
        `report card title "${cardTitle}" must not equal any step-region header`);
    }
  });

  // ---- (i) rendered card DOM title carries suffix, not a step header -------
  check("G3 rendered report-card title carries the suffix and is not a step header", () => {
    for (const t of ["implement", "test", "summarize", "commit", "spec_gate"]) {
      const card = app.renderStepReport({
        step_type: t, status: "completed",
        outputs: { summary: "did stuff", overall_passed: true },
      });
      assert.ok(card, `renderStepReport returns a card for ${t}`);
      const titleEl = findOne(card, "step-report__title");
      assert.ok(titleEl, `card for ${t} has a title element`);
      assert.match(titleEl.textContent, SUFFIX_RE,
        `rendered ${t} card title must end with 结果/总结, got "${titleEl.textContent}"`);
      // The rendered card title is never identical to a step-region header
      // (so it is never read as a new step starting).
      assert.ok(!ALL_STEP_TYPES.some((o) => app.STEP_HEADER_TITLES[o] === titleEl.textContent),
        `rendered card title "${titleEl.textContent}" must not equal a step header`);
    }
  });

  // ---- (j) history view and running-flow view title cards identically -----
  check("G3 history and running views title the same step card identically", () => {
    const sid = "07_implement_abcd1234";
    const records = [
      startedRecord(sid, "implement", 1),
      completedRecord(sid, "implement", 2, { summary: "G1. did it" }),
    ];
    const cardTitle = (live) => {
      const container = document.createElement("div");
      app.renderConversation(container, records, live);
      const t = findOne(container, "step-report__title");
      return t ? t.textContent : null;
    };
    const liveTitle = cardTitle(false);
    const histTitle = cardTitle(false);
    assert.ok(liveTitle, "a report-card title rendered in the shared engine");
    assert.strictEqual(liveTitle, histTitle,
      "shared renderConversation must title the card identically for both views");
    assert.match(liveTitle, SUFFIX_RE);
    // And it is distinct from the region header (IMPLEMENT) shown above it.
    assert.notStrictEqual(liveTitle, app.STEP_HEADER_TITLES.implement);
  });
}
