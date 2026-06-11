/*
 * DOM-free tests for the Resume-flow pure helpers in app.js.
 *
 * Covers: isFlowResumable, RESUMABLE_STATUSES, isResumeInProgress.
 *
 * Run manually:  node tests/frontend/flow_resume.test.mjs
 */
import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import path from "node:path";

const require = createRequire(import.meta.url);
const here = path.dirname(fileURLToPath(import.meta.url));
const app = require(path.join(here, "..", "..", "src", "se3", "server", "static", "app.js"));

let passed = 0;
function check(name, fn) {
  fn();
  passed += 1;
  console.log("  ok -", name);
}

// ---------------------------------------------------------------------------
// RESUMABLE_STATUSES constant
// ---------------------------------------------------------------------------
check("RESUMABLE_STATUSES contains failed and paused", () => {
  assert.deepEqual(app.RESUMABLE_STATUSES, ["failed", "paused"]);
});

// ---------------------------------------------------------------------------
// isFlowResumable — happy paths
// ---------------------------------------------------------------------------
check("failed flow with flow_id is resumable", () => {
  assert.equal(app.isFlowResumable({ flow_id: "abc", status: "failed" }), true);
});

check("paused flow with flow_id is resumable", () => {
  assert.equal(app.isFlowResumable({ flow_id: "abc", status: "paused" }), true);
});

check("status comparison is case-insensitive", () => {
  assert.equal(app.isFlowResumable({ flow_id: "x", status: "FAILED" }), true);
  assert.equal(app.isFlowResumable({ flow_id: "x", status: "Paused" }), true);
});

// ---------------------------------------------------------------------------
// isFlowResumable — non-resumable statuses
// ---------------------------------------------------------------------------
check("running flow is not resumable", () => {
  assert.equal(app.isFlowResumable({ flow_id: "abc", status: "running" }), false);
});

check("completed flow is not resumable", () => {
  assert.equal(app.isFlowResumable({ flow_id: "abc", status: "completed" }), false);
});

check("init flow is not resumable", () => {
  assert.equal(app.isFlowResumable({ flow_id: "abc", status: "init" }), false);
});

check("unknown status is not resumable", () => {
  assert.equal(app.isFlowResumable({ flow_id: "abc", status: "unknown" }), false);
});

// ---------------------------------------------------------------------------
// isFlowResumable — source-based exclusion (archived/history)
// ---------------------------------------------------------------------------
check("archived flow is not resumable even with failed status", () => {
  assert.equal(app.isFlowResumable({ flow_id: "x", status: "failed", source: "archived" }), false);
});

check("history flow is not resumable even with failed status", () => {
  assert.equal(app.isFlowResumable({ flow_id: "x", status: "failed", source: "history" }), false);
});

check("active flow with failed status is resumable", () => {
  assert.equal(app.isFlowResumable({ flow_id: "x", status: "failed", source: "active" }), true);
});

check("source exclusion is case-insensitive", () => {
  assert.equal(app.isFlowResumable({ flow_id: "x", status: "failed", source: "Archived" }), false);
  assert.equal(app.isFlowResumable({ flow_id: "x", status: "paused", source: "History" }), false);
});

check("archived paused flow is not resumable", () => {
  assert.equal(app.isFlowResumable({ flow_id: "x", status: "paused", source: "archived" }), false);
});

// ---------------------------------------------------------------------------
// isFlowResumable — missing / invalid inputs
// ---------------------------------------------------------------------------
check("null flow is not resumable", () => {
  assert.equal(app.isFlowResumable(null), false);
});

check("undefined flow is not resumable", () => {
  assert.equal(app.isFlowResumable(undefined), false);
});

check("non-object is not resumable", () => {
  assert.equal(app.isFlowResumable("string"), false);
  assert.equal(app.isFlowResumable(42), false);
});

check("flow without flow_id is not resumable", () => {
  assert.equal(app.isFlowResumable({ status: "failed" }), false);
  assert.equal(app.isFlowResumable({ flow_id: "", status: "failed" }), false);
});

check("flow without status is not resumable", () => {
  assert.equal(app.isFlowResumable({ flow_id: "abc" }), false);
  assert.equal(app.isFlowResumable({ flow_id: "abc", status: "" }), false);
  assert.equal(app.isFlowResumable({ flow_id: "abc", status: null }), false);
});

// ---------------------------------------------------------------------------
// isResumeInProgress — state tracking
// ---------------------------------------------------------------------------
check("isResumeInProgress returns false when set is empty", () => {
  // Save and restore state to avoid side effects.
  const saved = new Set(app.state.resumeFlowRequests);
  app.state.resumeFlowRequests.clear();
  assert.equal(app.isResumeInProgress("any-id"), false);
  app.state.resumeFlowRequests = saved;
});

check("isResumeInProgress returns true when flow is in the set", () => {
  const saved = new Set(app.state.resumeFlowRequests);
  app.state.resumeFlowRequests = new Set(["flow-123"]);
  assert.equal(app.isResumeInProgress("flow-123"), true);
  assert.equal(app.isResumeInProgress("flow-456"), false);
  app.state.resumeFlowRequests = saved;
});

// ---------------------------------------------------------------------------
// Done
// ---------------------------------------------------------------------------
console.log(`\n  ${passed} checks passed.`);
