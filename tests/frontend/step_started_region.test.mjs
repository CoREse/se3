/*
 * Step-started RUNNING-region tests (Group G2).
 *
 * Loaded by tests/frontend/test_app_pure.mjs after its shared DOM stub
 * (`globalThis.document` / `FakeNode`) is installed. Exposes
 * `registerStepStartedRegionTests({app, check, findOne, findAll})` so the
 * parent harness drives the same check() reporter and the same `app` export.
 *
 * Coverage:
 *   (a) normalizeRecord recognizes `type:'step_started'`, producing a
 *       lightweight running anchor (role 'step-event', kind 'step_started',
 *       status 'running', NO stepReport) whose stepId/stepType match the step.
 *   (b) A non-LLM step (TEST / COMMIT / SPEC_GATE) that has ONLY a step_started
 *       record still surfaces its visual step region (a step-header + a
 *       "进行中" status row) the instant it enters RUNNING.
 *   (c) renderStepStartedRecord renders a text+icon status row with NO report
 *       card, NO fold / raw / chip affordance.
 *   (d) stepStatusDisplay maps every status to {icon, text} with fallbacks.
 */
import assert from "node:assert/strict";

export function registerStepStartedRegionTests(ctx) {
  const { app, check, findOne, findAll } = ctx;

  // On-disk shape persisted by chat_history.record_step_started: a flat,
  // envelope-less dict (no `message`, no `role`).
  const startedRecord = (stepId, stepType, ts, status = "running") => ({
    type: "step_started",
    step_id: stepId,
    step_type: stepType,
    status,
    timestamp: ts,
  });

  // ---- (a) normalizeRecord ------------------------------------------------
  check("G2 normalizeRecord recognizes step_started as a running anchor", () => {
    const norm = app.normalizeRecord(
      startedRecord("05_test_abcd1234", "test", 7));
    assert.equal(norm.kind, "step_started");
    assert.equal(norm.role, "step-event");
    assert.equal(norm.status, "running");
    assert.equal(norm.stepType, "test");
    assert.equal(norm.stepId, "05_test_abcd1234");
    assert.equal(norm.timestamp, 7);
    // A running anchor carries NO report card payload.
    assert.equal(norm.stepReport, null);
  });

  check("G2 step_started stepId matches the step's other records", () => {
    const stepId = "07_implement_deadbeef";
    const started = app.normalizeRecord(startedRecord(stepId, "implement", 1));
    const chat = app.normalizeRecord({
      step_id: stepId, step_type: "implement",
      message: { role: "assistant", content: "hi", timestamp: 2 },
    });
    assert.equal(started.stepId, chat.stepId);
    assert.equal(app.stepKey(started), app.stepKey(chat));
  });

  check("G2 normalizeRecord honors an envelope-wrapped step_started too", () => {
    // Defensive: should a daemon ever wrap the anchor in a `message` envelope,
    // the message-first `pick` still resolves the type/status.
    const norm = app.normalizeRecord({
      step_id: "02_commit_aa", step_type: "commit",
      message: { type: "step_started", status: "running", timestamp: 3 },
    });
    assert.equal(norm.kind, "step_started");
    assert.equal(norm.stepType, "commit");
    assert.equal(norm.stepId, "02_commit_aa");
  });

  check("G2 normalizeRecord defaults a missing status to running", () => {
    const norm = app.normalizeRecord({
      type: "step_started", step_id: "x", step_type: "spec_gate", timestamp: 1,
    });
    assert.equal(norm.status, "running");
  });

  // ---- (b) non-LLM step region appears at RUNNING -------------------------
  for (const [stepType, label] of [
    ["test", "Testing"],
    ["commit", "Commit"],
    ["spec_gate", "Spec Gate"],
  ]) {
    check(`G2 non-LLM ${stepType} step shows its region from step_started alone`, () => {
      const container = document.createElement("div");
      app.renderConversation(
        container, [startedRecord(`01_${stepType}_ab`, stepType, 1)], false);
      // The step-header anchors the region (one header, the step's label).
      const headers = findAll(container, "history-step-header");
      assert.equal(headers.length, 1,
        `expected one step region header for ${stepType}`);
      const title = findOne(headers[0], "history-step-title");
      assert.ok(title && title.textContent === app.stepHeaderLabel(stepType),
        `header should label the ${stepType} region`);
      // A "进行中" status row is present.
      const statusRow = findOne(container, "step-status-row");
      assert.ok(statusRow, "expected a step-status-row for the running step");
      assert.ok(statusRow.classList.contains("step-status-running"));
      const text = findOne(statusRow, "step-status-text");
      assert.ok(text && text.textContent.includes("进行中"),
        `status row should read 进行中, got ${text && text.textContent}`);
      void label;
    });
  }

  // ---- (c) affordance-free status row -------------------------------------
  check("G2 step_started row has no report card / fold / raw / chip", () => {
    const container = document.createElement("div");
    app.renderConversation(
      container, [startedRecord("05_test_ab", "test", 1)], false);
    const row = findOne(container, "step-status-row");
    assert.ok(row, "expected the status row");
    assert.equal(findAll(row, "step-report").length, 0,
      "running anchor must NOT render a report card");
    assert.equal(findAll(row, "msg-chip").length, 0,
      "running anchor must NOT render a fold chip");
    assert.equal(findAll(row, "raw-toggle").length, 0,
      "running anchor must NOT render a raw toggle");
    // Status conveyed by BOTH an icon and text (never color alone).
    const icon = findOne(row, "step-status-icon");
    assert.ok(icon && icon.textContent, "status row should carry an icon");
  });

  check("G2 step_started row carries the step-type identity class", () => {
    const container = document.createElement("div");
    app.renderConversation(
      container, [startedRecord("05_test_ab", "test", 1)], false);
    const row = findOne(container, "step-status-row");
    assert.ok(row.classList.contains("step-type-test"),
      "row should carry step-type-test for per-step grouping styles");
  });

  // ---- (d) stepStatusDisplay ----------------------------------------------
  check("G2 stepStatusDisplay maps known statuses to icon + text", () => {
    assert.equal(app.stepStatusDisplay("running").text, "进行中");
    assert.equal(app.stepStatusDisplay("retrying").text, "重试中");
    assert.equal(app.stepStatusDisplay("paused").text, "已暂停");
    assert.equal(app.stepStatusDisplay("completed").text, "已完成");
    assert.equal(app.stepStatusDisplay("failed").text, "失败");
    // Each has a non-empty icon.
    for (const s of ["running", "retrying", "paused", "completed", "failed"]) {
      assert.ok(app.stepStatusDisplay(s).icon, `${s} should have an icon`);
    }
  });

  check("G2 stepStatusDisplay is case-insensitive and falls back safely", () => {
    assert.equal(app.stepStatusDisplay("RUNNING").text, "进行中");
    // Unknown status keeps its raw token rather than dropping it.
    const unknown = app.stepStatusDisplay("merging");
    assert.equal(unknown.text, "merging");
    assert.ok(unknown.icon, "unknown status still gets a neutral icon");
    // Empty / null degrade to a running default rather than a dangling label.
    assert.equal(app.stepStatusDisplay(null).text, "running");
  });
}
