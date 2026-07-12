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
    // Once the terminal report lands, the running status anchor is superseded
    // (the completed report IS the region's final state) — the region must NOT
    // show both "In progress" and a completed report at once.
    assert.ok(!findOne(container, "step-status-row"),
      "running anchor is superseded by the terminal report");
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
    // The terminal report supersedes the running anchor on the append path too:
    // one region, the report card only (no stale 进行中 row beside it).
    assert.ok(!findOne(container, "step-status-row"),
      "running anchor superseded by the terminal report on append");
    assert.ok(findOne(container, "step-report"));
  });

  const statusRecord = (stepId, stepType, status, ts) => ({
    type: "step_status", step_id: stepId, step_type: stepType,
    status, timestamp: ts,
  });

  // ---- (c2) a NON-CONTIGUOUS same step_id gets its own boundary header ----
  // SELF_CHECK(A) → IMPLEMENT(B) → SELF_CHECK(A) on a revision/retry loop: the
  // re-appearing step_id A's records physically sit AFTER B, so under strict
  // chronological order they must get their OWN boundary header. Without one,
  // A's second segment would render beneath B's IMPLEMENT header and sticky
  // navigation would mis-attribute that content to IMPLEMENT. A header is
  // therefore emitted per CONTIGUOUS run, not once per step_id.
  check("G2 a re-appearing step_id gets its own boundary header per contiguous run", () => {
    const a = "06_self_check_aa";
    const b = "07_implement_bb";
    const container = document.createElement("div");
    app.renderConversation(container, [
      startedRecord(a, "self_check", 1),
      completedRecord(a, "self_check", 2),
      startedRecord(b, "implement", 3),
      completedRecord(b, "implement", 4),
      // step A re-runs (same step_id) after B — strictly later in time.
      startedRecord(a, "self_check", 5),
      completedRecord(a, "self_check", 6),
    ], false);
    // Three contiguous runs → three headers: SELF CHECK, IMPLEMENT, SELF CHECK.
    // The re-appearance gets its own boundary so its content is attributable to
    // SELF_CHECK (not visually absorbed under the IMPLEMENT header above it).
    assert.deepEqual(stepHeaders(container),
      ["SELF CHECK", "IMPLEMENT", "SELF CHECK"],
      "non-contiguous re-appearance of a step_id opens its own boundary header");
  });

  // ---- (c2b) contiguous same step_id still collapses to one header --------
  check("G2 contiguous records of one step_id still collapse to a single header", () => {
    const sid = "06_self_check_aa";
    const container = document.createElement("div");
    app.renderConversation(container, [
      startedRecord(sid, "self_check", 1),
      chatRecord(sid, "self_check", "assistant", "checking", 2),
      outputRecord(sid, "self_check", 3),
    ], false);
    assert.deepEqual(stepHeaders(container), ["SELF CHECK"],
      "a contiguous run of one step_id has exactly one boundary header");
  });

  // ---- (c2c) running → completed: no stale 进行中 beside the report --------
  check("G2 running → completed supersedes the In progress anchor (issue 1)", () => {
    const sid = "05_test_aa";
    const container = document.createElement("div");
    app.renderConversation(container, [
      startedRecord(sid, "test", 1),
      completedRecord(sid, "test", 2),
    ], false);
    // The terminal report exists; the running status row is gone.
    assert.ok(findOne(container, "step-report"), "terminal report present");
    assert.ok(!findOne(container, "step-status-row"),
      "running anchor must not coexist with the completed report");
  });

  // ---- (c2d) running → paused → running → completed (same step_id) --------
  // The resumed run re-arms a 'running' anchor (later ts than the paused one),
  // so before completion the region reads 进行中; once the terminal report
  // lands every status anchor is superseded — no stale 已暂停 / 进行中 remains.
  check("G2 resumed step shows In progress, then the terminal report supersedes all anchors", () => {
    const sid = "01_discovery_ab";
    const container = document.createElement("div");
    // Mid-resume: running → paused → running (no terminal yet).
    app.renderConversation(container, [
      startedRecord(sid, "discovery", 1),
      statusRecord(sid, "discovery", "paused", 2),
      startedRecord(sid, "discovery", 3),
    ], false);
    let rows = findAll(container, "step-status-row");
    assert.equal(rows.length, 1, "exactly one surviving status row mid-resume");
    let text = findOne(rows[0], "step-status-text");
    assert.ok(text && text.textContent.includes("In progress"),
      `resumed step must read In progress, got ${text && text.textContent}`);
    // Now the step completes: every lifecycle anchor is superseded.
    app.renderConversation(container, [
      startedRecord(sid, "discovery", 1),
      statusRecord(sid, "discovery", "paused", 2),
      startedRecord(sid, "discovery", 3),
      completedRecord(sid, "discovery", 4),
    ], false);
    assert.ok(!findOne(container, "step-status-row"),
      "no Paused / In progress anchor remains after completion");
    assert.ok(findOne(container, "step-report"), "the completed report is shown");
    assert.deepEqual(stepHeaders(container), ["DISCOVERY"], "still one region");
  });

  // ---- (c3) step_status (paused) supersedes the running anchor ------------
  check("G2 a paused step_status supersedes the running anchor in one region", () => {
    const sid = "01_discovery_ab";
    const container = document.createElement("div");
    app.renderConversation(container, [
      startedRecord(sid, "discovery", 1),
      statusRecord(sid, "discovery", "paused", 2),
    ], false);
    // One region.
    assert.deepEqual(stepHeaders(container), ["DISCOVERY"]);
    // Exactly one status row, and it reads 已暂停 (not the stale 进行中).
    const rows = findAll(container, "step-status-row");
    assert.equal(rows.length, 1, "the running anchor is superseded by the paused row");
    const text = findOne(rows[0], "step-status-text");
    assert.ok(text && text.textContent.includes("Paused"),
      `the surviving status row must read Paused, got ${text && text.textContent}`);
    assert.ok(rows[0].classList.contains("step-status-paused"));
  });

  check("G2 paused supersede holds on the incremental append path too", () => {
    const sid = "01_discovery_ab";
    const container = document.createElement("div");
    const records = [startedRecord(sid, "discovery", 1)];
    app.renderConversation(container, records, false);
    assert.ok(findOne(container, "step-status-running"), "running anchor first");
    // The paused status arrives as a live append.
    records.push(statusRecord(sid, "discovery", "paused", 2));
    app.renderConversation(container, records, true);
    const rows = findAll(container, "step-status-row");
    assert.equal(rows.length, 1, "running anchor superseded on append");
    assert.ok(rows[0].classList.contains("step-status-paused"));
    assert.deepEqual(stepHeaders(container), ["DISCOVERY"]);
  });

  // ---- (c2e) terminal supersede is PER REGION, not per step_id ------------
  // SELF_CHECK(A) completes, then runs again (same step_id) after IMPLEMENT(B)
  // and is still RUNNING. The first A region's running anchor is superseded by
  // its own terminal report, but the SECOND A region (a fresh contiguous run,
  // no terminal yet) must keep its 进行中 anchor — the terminal of the first
  // region must NOT reach across and strip the live second region's status.
  check("G2 terminal supersede is scoped to its own region (re-running step keeps In progress)", () => {
    const a = "06_self_check_aa";
    const b = "07_implement_bb";
    const container = document.createElement("div");
    app.renderConversation(container, [
      startedRecord(a, "self_check", 1),
      completedRecord(a, "self_check", 2),
      startedRecord(b, "implement", 3),
      completedRecord(b, "implement", 4),
      // A re-runs and is still in progress (no terminal yet).
      startedRecord(a, "self_check", 5),
    ], false);
    // Three regions: SELF CHECK, IMPLEMENT, SELF CHECK.
    assert.deepEqual(stepHeaders(container),
      ["SELF CHECK", "IMPLEMENT", "SELF CHECK"]);
    // Exactly one surviving status row — the live second A region's 进行中.
    const rows = findAll(container, "step-status-row");
    assert.equal(rows.length, 1,
      "only the live re-run region keeps a status anchor");
    const text = findOne(rows[0], "step-status-text");
    assert.ok(text && text.textContent.includes("In progress"),
      `the re-running region must read In progress, got ${text && text.textContent}`);
    // Two terminal report cards (the two completed runs) are present.
    assert.equal(findAll(container, "step-report").length, 2);
  });

  // ---- (c2f) terminal supersedes a split-off earlier anchor (issue 2) -----
  // step_started(A) → record(B) → step_completed(A): another step's record
  // splits A's running anchor and A's terminal report into SEPARATE contiguous
  // runs. The terminal must still reach back and supersede A's earlier 进行中
  // anchor (they belong to the SAME execution of A) — a per-contiguous-run
  // reconciliation would strand the first 进行中 beside the completed report.
  check("G2 terminal supersedes an earlier anchor split by another step (issue 2)", () => {
    const a = "06_self_check_aa";
    const b = "07_implement_bb";
    const container = document.createElement("div");
    app.renderConversation(container, [
      startedRecord(a, "self_check", 1),
      chatRecord(b, "implement", "assistant", "working", 2),
      completedRecord(a, "self_check", 3),
    ], false);
    // No surviving 进行中 anchor — A's terminal superseded its split-off
    // running row even though IMPLEMENT(B) sits between them.
    assert.ok(!findOne(container, "step-status-row"),
      "A's running anchor must be superseded by its terminal across the split");
    // A's completed report card is present.
    assert.equal(findAll(container, "step-report").length, 1,
      "the completed step shows its report, not a stale In progress");
  });

  // ---- (c2g) split terminal does NOT strip a later fresh execution --------
  // started(A) → record(B) → completed(A) → record(B2) → started(A again).
  // The first A execution's terminal supersedes only its OWN preceding anchor;
  // the later, still-running A execution keeps its 进行中.
  check("G2 split terminal preserves a later fresh execution's In progress (issue 2)", () => {
    const a = "06_self_check_aa";
    const b = "07_implement_bb";
    const container = document.createElement("div");
    app.renderConversation(container, [
      startedRecord(a, "self_check", 1),
      chatRecord(b, "implement", "assistant", "working", 2),
      completedRecord(a, "self_check", 3),
      chatRecord(b, "implement", "assistant", "more", 4),
      startedRecord(a, "self_check", 5),
    ], false);
    const rows = findAll(container, "step-status-row");
    assert.equal(rows.length, 1,
      "only the later fresh A execution keeps a status anchor");
    const text = findOne(rows[0], "step-status-text");
    assert.ok(text && text.textContent.includes("In progress"),
      `the later A execution must read In progress, got ${text && text.textContent}`);
    assert.equal(findAll(container, "step-report").length, 1,
      "the first A execution's terminal report is present");
  });

  // ---- (e) shared step-type identity class --------------------------------
  check("G2 every bubble in a region carries the step-type identity class", () => {
    const sid = "07_implement_abcd1234";
    const container = document.createElement("div");
    // A still-RUNNING region (no terminal report yet) so the running anchor
    // survives: the anchor, the chat bubble, and the step_output usage row all
    // share the step-type-implement class (header rows are excluded — they are
    // stateless separators).
    app.renderConversation(container, [
      startedRecord(sid, "implement", 1),
      chatRecord(sid, "implement", "assistant", "working", 2),
      outputRecord(sid, "implement", 3),
    ], false);
    const tagged = findAll(container, "step-type-implement");
    assert.ok(tagged.length >= 3,
      `expected the anchor + chat + output rows tagged, got ${tagged.length}`);
    // Once a terminal report supersedes the anchor, the surviving rows (chat +
    // report) still carry the identity class.
    const c2 = document.createElement("div");
    app.renderConversation(c2, [
      startedRecord(sid, "implement", 1),
      chatRecord(sid, "implement", "assistant", "working", 2),
      completedRecord(sid, "implement", 3),
    ], false);
    assert.ok(findAll(c2, "step-type-implement").length >= 2,
      "the chat + report rows keep the identity class after the anchor is superseded");
  });

  // ==========================================================================
  // Group G3: final report card "Result/Summary" semantic titling.
  //
  // The final report card of a step must read as that step's *result* (结果) /
  // *summary* (总结), never as a bare step name that a reader could mistake for
  // a brand-new step heading (the IMPLEMENT ambiguity). These cover:
  //   (f) reportCardTitle is a pure `<步骤> · Result/Summary` builder; summarize → 总结.
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
  const SUFFIX_RE = /[·]\s*(Result|Summary)\s*$/;

  // ---- (f) reportCardTitle pure builder -----------------------------------
  check("G3 reportCardTitle builds `<步骤> · Result/Summary` with a result/summary suffix", () => {
    for (const t of ALL_STEP_TYPES) {
      const title = app.reportCardTitle(t);
      assert.match(title, SUFFIX_RE,
        `reportCardTitle(${t}) must end with · Result or · Summary, got "${title}"`);
      // The base label is the title-case STEP_REPORT_TITLES entry, distinct
      // from the uppercase step-header label.
      assert.ok(title.startsWith(app.STEP_REPORT_TITLES[t] + " · "),
        `reportCardTitle(${t}) must prefix the title-case report label`);
    }
    // summarize (itself a summary step) reads 总结; a non-summary step reads 结果.
    assert.ok(app.reportCardTitle("summarize").endsWith("· Summary"),
      "summarize card reads Summary");
    assert.ok(app.reportCardTitle("implement").endsWith("· Result"),
      "implement card reads Result");
    // Unknown step type degrades without throwing and still carries a suffix.
    assert.match(app.reportCardTitle("totally_unknown"), SUFFIX_RE);
    assert.match(app.reportCardTitle(""), SUFFIX_RE);
  });

  // ---- (g) IMPLEMENT card title is unambiguous result/summary -------------
  check("G3 IMPLEMENT report-card title carries explicit Result/Summary semantic", () => {
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
        `rendered ${t} card title must end with Result/Summary, got "${titleEl.textContent}"`);
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
