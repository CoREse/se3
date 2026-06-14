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

export function registerWaitingForLockTests({ app, check }) {

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
