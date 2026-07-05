/*
 * DOM-free tests for the worktree-merge sub-state pure helpers in app.js
 * (Group G5): isMerging, flowStatusLabel folding, the STEP_STATUS_DISPLAY entry,
 * and the merging chat status anchor.
 *
 * Registered into the shared harness tests/frontend/test_app_pure.mjs via
 * registerMergingTests({ app, check, findOne, findAll }). Unlike
 * waiting_for_lock (which layers on a RUNNING flow), merging layers on the
 * flow's COMPLETED body — so a completed worktree flow that is still merging
 * back into its origin branch must read as 合并中 rather than 已完成, and the
 * indicator must clear once the flag disappears (the worktree engine.json is
 * archived on a successful merge).
 *
 * Run standalone:  node tests/frontend/test_app_pure.mjs
 */

import assert from "node:assert/strict";

export function registerMergingTests({ app, check, findOne, findAll }) {

  // -- normalizeRecord / rendering of the streamed merging event -------------
  // On-disk shape persisted by chat_history.record_merging: a flat,
  // envelope-less dict carrying a human-readable `message` but NO `role` /
  // `content` / `raw_json`.
  const mergeRecord = (stepId, stepType, ts, message) => ({
    type: "merging",
    step_id: stepId,
    step_type: stepType,
    status: "merging",
    message: message || "正在将 worktree 分支合并回主分支…",
    timestamp: ts,
  });

  check("G5 normalizeRecord recognizes merging as a status anchor", () => {
    const norm = app.normalizeRecord(mergeRecord("14_commit_abcd", "commit", 5));
    assert.equal(norm.kind, "merging");
    assert.equal(norm.role, "step-event");
    assert.equal(norm.status, "merging");
    assert.equal(norm.stepType, "commit");
    assert.equal(norm.stepId, "14_commit_abcd");
    assert.equal(norm.timestamp, 5);
    // No report card — it's a lightweight lifecycle anchor like step_started.
    assert.equal(norm.stepReport, null);
    // The human-readable message is preserved on content (never dropped).
    assert.ok(norm.content.includes("合并回主分支"));
  });

  check("G5 merging renders a 合并中 status row, not an empty bubble", () => {
    const container = document.createElement("div");
    app.renderConversation(
      container, [mergeRecord("14_commit_ab", "commit", 1)], false);
    // It must NOT render as a generic "(no readable content)" bubble.
    assert.equal(findAll(container, "conv-empty").length, 0,
      "merging must not fall through to the empty-content path");
    const row = findOne(container, "step-status-row");
    assert.ok(row, "expected a step-status-row for the merging flow");
    assert.ok(row.classList.contains("step-status-merging"));
    assert.ok(row.classList.contains("kind-merging"));
    const text = findOne(row, "step-status-text");
    assert.ok(text && text.textContent.includes("合并中"),
      `status row should read 合并中, got ${text && text.textContent}`);
    // Affordance-free: no report card / fold chip / raw toggle.
    assert.equal(findAll(row, "step-report").length, 0);
    assert.equal(findAll(row, "msg-chip").length, 0);
    assert.equal(findAll(row, "raw-toggle").length, 0);
  });

  check("G5 merging anchor is superseded by the same step's later anchor", () => {
    // The merge row and a later status anchor for the SAME step share the step
    // id, so the region must collapse to ONE truthful status rather than
    // stacking both rows.
    const stepId = "14_commit_ab";
    const container = document.createElement("div");
    app.renderConversation(container, [
      mergeRecord(stepId, "commit", 1),
      { type: "waiting_for_lock", step_id: stepId, step_type: "commit",
        status: "waiting_for_lock", message: "等待主分支锁…", timestamp: 2 },
    ], false);
    const rows = findAll(container, "step-status-row");
    assert.equal(rows.length, 1, "only the current status anchor should survive");
  });

  check("G5 stepStatusDisplay maps merging to icon + 合并中", () => {
    const d = app.stepStatusDisplay("merging");
    assert.equal(d.text, "合并中");
    assert.ok(d.icon, "merging should carry an icon");
  });

  // -- isMerging -------------------------------------------------------------
  check("G5 isMerging true for a completed flow with the flag set", () => {
    // Crucially does NOT require status running — merging layers on the
    // completed body.
    assert.equal(
      app.isMerging({ status: "completed", merging: true }),
      true,
    );
  });

  check("G5 isMerging true even when status is running (defensive)", () => {
    assert.equal(app.isMerging({ status: "running", merging: true }), true);
  });

  check("G5 isMerging false when the flag is absent/false", () => {
    assert.equal(app.isMerging({ status: "completed" }), false);
    assert.equal(
      app.isMerging({ status: "completed", merging: false }),
      false,
    );
  });

  check("G5 isMerging ignores a stale flag on an archived/history snapshot", () => {
    assert.equal(
      app.isMerging({ status: "completed", merging: true, source: "archived" }),
      false,
    );
    assert.equal(
      app.isMerging({ status: "completed", merging: true, source: "history" }),
      false,
    );
  });

  check("G5 isMerging tolerates null/garbage input", () => {
    assert.equal(app.isMerging(null), false);
    assert.equal(app.isMerging(undefined), false);
    assert.equal(app.isMerging({}), false);
  });

  // -- flowStatusLabel -------------------------------------------------------
  check("G5 flowStatusLabel overrides completed with 合并中", () => {
    assert.equal(
      app.flowStatusLabel({ status: "completed", merging: true }),
      "合并中",
    );
  });

  check("G5 flowStatusLabel appends ·等待主分支锁 while queued for the lock", () => {
    assert.equal(
      app.flowStatusLabel(
        { status: "completed", merging: true, waiting_for_lock: true }),
      "合并中·等待主分支锁",
    );
  });

  check("G5 flowStatusLabel falls back to the base status once merging clears", () => {
    // merging cleared -> the terminal status is shown again (not 合并中).
    assert.equal(app.flowStatusLabel({ status: "completed" }), "completed");
    assert.equal(
      app.flowStatusLabel({ status: "completed", merging: false }), "completed");
    // An archived snapshot with a stale flag is not treated as merging.
    assert.equal(
      app.flowStatusLabel(
        { status: "completed", merging: true, source: "archived" }),
      "completed");
  });
}
