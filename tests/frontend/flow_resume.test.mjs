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
const app = require(path.join(here, "..", "..", "src", "tianluo", "server", "static", "app.js"));

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
// isFlowResumable — authoritative `resumable` flag (group G4)
// ---------------------------------------------------------------------------
check("resumable=true short-circuits to true even when status is running", () => {
  assert.equal(
    app.isFlowResumable({ flow_id: "x", status: "running", resumable: true }),
    true,
  );
});

check("resumable=true wins over archived/history source exclusion", () => {
  assert.equal(
    app.isFlowResumable({ flow_id: "x", status: "running", source: "history", resumable: true }),
    true,
  );
  assert.equal(
    app.isFlowResumable({ flow_id: "x", status: "paused", source: "resumable", resumable: true }),
    true,
  );
});

check("completed flow is never resumable even with resumable=true flag", () => {
  // A stale completed snapshot may carry resumable=true; the completed guard
  // takes precedence (mirrors ServerState.is_flow_resumable + the daemon
  // resume validator, which rejects a COMPLETED flow).
  assert.equal(
    app.isFlowResumable({ flow_id: "x", status: "completed", resumable: true }),
    false,
  );
});

check("running flow with resumable=false is not resumable (group G2)", () => {
  // A genuinely-running flow whose live-process gate (group G1) forced
  // resumable=False must hide the Resume entry: the flag is the primary
  // signal and the legacy fallback never treats RUNNING as resumable, so the
  // button is hidden and clicking is impossible — consistent with the server
  // returning 409 for such a flow.
  assert.equal(
    app.isFlowResumable({ flow_id: "x", status: "running", resumable: false }),
    false,
  );
  assert.equal(
    app.isFlowResumable({ flow_id: "x", status: "RUNNING", resumable: false, source: "active" }),
    false,
  );
});

check("resumable not strictly true falls back to legacy heuristic", () => {
  // completed + resumable falsy -> not resumable
  assert.equal(
    app.isFlowResumable({ flow_id: "x", status: "completed", resumable: false }),
    false,
  );
  // truthy-but-not-true values do not short-circuit; legacy logic decides
  assert.equal(
    app.isFlowResumable({ flow_id: "x", status: "running", resumable: 1 }),
    false,
  );
  // failed + resumable absent -> resumable via legacy fallback
  assert.equal(app.isFlowResumable({ flow_id: "x", status: "failed" }), true);
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
// resumeErrorText — 404 detail branches (shared-FS machine switch)
// ---------------------------------------------------------------------------
// In this Node environment no i18n dictionary is loaded, so tf() returns its
// fallback: for the machine-offline branch that fallback is the backend detail
// itself. The assertions therefore pin the *branch* (offline wording is
// carried through, never replaced by the generic not-found text) rather than
// the localized string, which only exists in the browser.
check("404 with an offline-machine detail is not the generic not-found text", () => {
  const detail = "machine 'node007' owning flow 'f1' is not connected";
  const out = app.resumeErrorText(404, detail);
  assert.ok(out, "expected a non-empty message");
  assert.notEqual(out, "Flow not found or not resumable.");
  assert.match(out, /not connected/);
});

check("404 with a flow-not-found detail is passed through verbatim", () => {
  assert.equal(
    app.resumeErrorText(404, "flow 'f1' not found"),
    "flow 'f1' not found",
  );
});

check("404 without a detail falls back to the default not-found text", () => {
  assert.equal(app.resumeErrorText(404, ""), "Flow not found or not resumable.");
  assert.equal(app.resumeErrorText(404, "   "), "Flow not found or not resumable.");
  assert.equal(app.resumeErrorText(404, null), "Flow not found or not resumable.");
  assert.equal(app.resumeErrorText(404, undefined), "Flow not found or not resumable.");
});

check("non-404 statuses pass the detail through unchanged", () => {
  // The 409/other branches keep their own fallback wording via `|| tf(...)`,
  // so the helper must not inject the 404-specific default there — and must
  // not apply the machine-offline mapping either.
  assert.equal(app.resumeErrorText(409, "该 flow 仍在运行，无法 resume"), "该 flow 仍在运行，无法 resume");
  assert.equal(app.resumeErrorText(409, ""), "");
  assert.equal(app.resumeErrorText(503, "machine 'node007' is not connected"), "machine 'node007' is not connected");
  assert.equal(app.resumeErrorText(500, undefined), "");
});

// ---------------------------------------------------------------------------
// Done
// ---------------------------------------------------------------------------
console.log(`\n  ${passed} checks passed.`);
