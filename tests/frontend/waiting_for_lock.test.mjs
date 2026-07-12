/*
 * DOM-free tests for the waiting-for-lock running sub-state pure helpers in
 * app.js (Group G2): isWaitingForLock and flowStatusLabel.
 *
 * Registered into the shared harness tests/frontend/test_app_pure.mjs via
 * registerWaitingForLockTests({ app, check }). The flag rides the existing
 * STATUS_UPDATE flow snapshot (FlowSnapshot.waiting_for_lock), so a flow that
 * has started but is queued behind the main-worktree mutex must read as
 * running·waiting-for-lock rather than appearing stalled, and the indicator
 * must clear once the flag flips back to false.
 *
 * Run standalone:  node tests/frontend/test_app_pure.mjs
 */

import assert from "node:assert/strict";

export function registerWaitingForLockTests({ app, check, findOne, findAll }) {

  // -- normalizeRecord / rendering of the streamed waiting_for_lock event ----
  // On-disk shape persisted by chat_history.record_waiting_for_lock: a flat,
  // envelope-less dict carrying a human-readable `message` but NO `role` /
  // `content` / `raw_json`.
  const lockRecord = (stepId, stepType, ts, message) => ({
    type: "waiting_for_lock",
    step_id: stepId,
    step_type: stepType,
    status: "waiting_for_lock",
    message: message || "Waiting for the main-worktree lock…",
    timestamp: ts,
  });

  check("G2 normalizeRecord recognizes waiting_for_lock as a status anchor", () => {
    const norm = app.normalizeRecord(lockRecord("01_analyze_abcd", "analyze", 3));
    assert.equal(norm.kind, "waiting_for_lock");
    assert.equal(norm.role, "step-event");
    assert.equal(norm.status, "waiting_for_lock");
    assert.equal(norm.stepType, "analyze");
    assert.equal(norm.stepId, "01_analyze_abcd");
    assert.equal(norm.timestamp, 3);
    // No report card — it's a lightweight lifecycle anchor like step_started.
    assert.equal(norm.stepReport, null);
    // The human-readable message is preserved on content (never dropped).
    assert.ok(norm.content.includes("Waiting for the main-worktree lock"));
  });

  check("G2 waiting_for_lock renders a Waiting for lock status row, not an empty bubble", () => {
    const container = document.createElement("div");
    app.renderConversation(
      container, [lockRecord("01_analyze_ab", "analyze", 1)], false);
    // It must NOT render as a generic "(no readable content)" bubble.
    assert.equal(findAll(container, "conv-empty").length, 0,
      "waiting_for_lock must not fall through to the empty-content path");
    const row = findOne(container, "step-status-row");
    assert.ok(row, "expected a step-status-row for the waiting flow");
    assert.ok(row.classList.contains("step-status-waiting_for_lock"));
    assert.ok(row.classList.contains("kind-waiting_for_lock"));
    const text = findOne(row, "step-status-text");
    assert.ok(text && text.textContent.includes("Waiting for lock"),
      `status row should read Waiting for lock, got ${text && text.textContent}`);
    // Affordance-free: no report card / fold chip / raw toggle.
    assert.equal(findAll(row, "step-report").length, 0);
    assert.equal(findAll(row, "msg-chip").length, 0);
    assert.equal(findAll(row, "raw-toggle").length, 0);
  });

  check("G2 waiting_for_lock anchor is superseded by the later step_started", () => {
    // The wait row and the step's running anchor share the same stepId, so the
    // region must collapse to ONE truthful status (等待锁 → 进行中) rather than
    // stacking both rows.
    const stepId = "01_analyze_ab";
    const container = document.createElement("div");
    app.renderConversation(container, [
      lockRecord(stepId, "analyze", 1),
      { type: "step_started", step_id: stepId, step_type: "analyze",
        status: "running", timestamp: 2 },
    ], false);
    const rows = findAll(container, "step-status-row");
    assert.equal(rows.length, 1, "only the current status anchor should survive");
    const text = findOne(rows[0], "step-status-text");
    assert.ok(text && text.textContent.includes("In progress"),
      "the surviving anchor reads In progress once the lock is acquired");
  });

  check("G2 stepStatusDisplay maps waiting_for_lock to icon + Waiting for lock", () => {
    const d = app.stepStatusDisplay("waiting_for_lock");
    assert.equal(d.text, "Waiting for lock");
    assert.ok(d.icon, "waiting_for_lock should carry an icon");
  });

  // -- isWaitingForLock ------------------------------------------------------
  check("G2 isWaitingForLock true for a running flow with the flag set", () => {
    assert.equal(
      app.isWaitingForLock({ status: "running", waiting_for_lock: true }),
      true,
    );
  });

  check("G2 isWaitingForLock false when the flag is absent/false", () => {
    assert.equal(app.isWaitingForLock({ status: "running" }), false);
    assert.equal(
      app.isWaitingForLock({ status: "running", waiting_for_lock: false }),
      false,
    );
  });

  check("G2 isWaitingForLock ignores a stale flag on a terminal flow", () => {
    // Defensive: a since-completed/failed snapshot must never read as waiting.
    assert.equal(
      app.isWaitingForLock({ status: "completed", waiting_for_lock: true }),
      false,
    );
    assert.equal(
      app.isWaitingForLock({ status: "failed", waiting_for_lock: true }),
      false,
    );
  });

  check("G2 isWaitingForLock tolerates null/garbage input", () => {
    assert.equal(app.isWaitingForLock(null), false);
    assert.equal(app.isWaitingForLock(undefined), false);
    assert.equal(app.isWaitingForLock({}), false);
  });

  // -- flowStatusLabel -------------------------------------------------------
  check("G2 flowStatusLabel folds waiting-for-lock into the running label", () => {
    assert.equal(
      app.flowStatusLabel({ status: "running", waiting_for_lock: true }),
      "running · waiting for lock",
    );
  });

  check("G2 flowStatusLabel is the bare status when not waiting", () => {
    assert.equal(app.flowStatusLabel({ status: "running" }), "running");
    assert.equal(app.flowStatusLabel({ status: "paused" }), "paused");
    assert.equal(app.flowStatusLabel({ status: "completed", waiting_for_lock: true }),
      "completed");
  });

  check("G2 flowStatusLabel defaults to unknown for empty input", () => {
    assert.equal(app.flowStatusLabel({}), "unknown");
    assert.equal(app.flowStatusLabel(null), "unknown");
  });
}
