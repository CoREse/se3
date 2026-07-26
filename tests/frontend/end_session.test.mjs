/*
 * DOM-free tests for the End-session pure helpers in app.js.
 *
 * Covers: isFlowEndable, isEndInProgress.
 *
 * Run manually:  node tests/frontend/end_session.test.mjs
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
// isFlowEndable — endable (active / recoverable) statuses
// ---------------------------------------------------------------------------
check("running flow with flow_id is endable", () => {
  assert.equal(app.isFlowEndable({ flow_id: "abc", status: "running" }), true);
});

check("paused flow is endable", () => {
  assert.equal(app.isFlowEndable({ flow_id: "abc", status: "paused" }), true);
});

check("failed flow is endable", () => {
  assert.equal(app.isFlowEndable({ flow_id: "abc", status: "failed" }), true);
});

check("recovering flow is endable", () => {
  assert.equal(app.isFlowEndable({ flow_id: "abc", status: "recovering" }), true);
});

check("init flow is endable", () => {
  assert.equal(app.isFlowEndable({ flow_id: "abc", status: "init" }), true);
});

check("status comparison is case-insensitive (RUNNING endable)", () => {
  assert.equal(app.isFlowEndable({ flow_id: "x", status: "RUNNING" }), true);
});

// ---------------------------------------------------------------------------
// isFlowEndable — an ordinary completed flow is not endable
// ---------------------------------------------------------------------------
check("completed flow (no worktree path) is not endable", () => {
  assert.equal(app.isFlowEndable({ flow_id: "abc", status: "completed" }), false);
});

check("completed flow with a plain main-branch project_root is not endable", () => {
  assert.equal(
    app.isFlowEndable({
      flow_id: "abc",
      status: "completed",
      project_root: "/home/u/proj",
    }),
    false,
  );
});

check("completed guard is case-insensitive (no worktree path)", () => {
  assert.equal(app.isFlowEndable({ flow_id: "abc", status: "COMPLETED" }), false);
  assert.equal(app.isFlowEndable({ flow_id: "abc", status: "Completed" }), false);
});

// ---------------------------------------------------------------------------
// isFlowEndable — a completed worktree session with a dangling worktree IS
// endable (the orphan-cleanup case this feature exists for)
// ---------------------------------------------------------------------------
check("completed worktree session (dangling worktree) is endable", () => {
  assert.equal(
    app.isFlowEndable({
      flow_id: "abc",
      status: "completed",
      project_root: "/home/u/proj/se3/worktrees/wt_abc",
    }),
    true,
  );
});

check("completed worktree session endable even with trailing slash", () => {
  assert.equal(
    app.isFlowEndable({
      flow_id: "abc",
      status: "COMPLETED",
      project_root: "/home/u/proj/se3/worktrees/wt_abc/",
    }),
    true,
  );
});

check("completed worktree session that is archived/history is still NOT endable", () => {
  // An already-archived worktree snapshot has nothing live to clean up.
  assert.equal(
    app.isFlowEndable({
      flow_id: "abc",
      status: "completed",
      source: "history",
      project_root: "/home/u/proj/se3/worktrees/wt_abc",
    }),
    false,
  );
});

// ---------------------------------------------------------------------------
// isWorktreeSessionPath — pure structural check
// ---------------------------------------------------------------------------
check("isWorktreeSessionPath recognises a worktree dir", () => {
  assert.equal(app.isWorktreeSessionPath("/a/b/se3/worktrees/wt1"), true);
  assert.equal(app.isWorktreeSessionPath("/a/b/se3/worktrees/wt1/"), true);
});

check("isWorktreeSessionPath rejects a plain project root", () => {
  assert.equal(app.isWorktreeSessionPath("/a/b/proj"), false);
  assert.equal(app.isWorktreeSessionPath("/a/se3/specs"), false);
  assert.equal(app.isWorktreeSessionPath(""), false);
  assert.equal(app.isWorktreeSessionPath(null), false);
  assert.equal(app.isWorktreeSessionPath(undefined), false);
});

// ---------------------------------------------------------------------------
// isFlowEndable — archived/history-only snapshots are not endable
// ---------------------------------------------------------------------------
check("archived flow is not endable even when running", () => {
  assert.equal(
    app.isFlowEndable({ flow_id: "x", status: "running", source: "archived" }),
    false,
  );
});

check("history flow is not endable even when failed", () => {
  assert.equal(
    app.isFlowEndable({ flow_id: "x", status: "failed", source: "history" }),
    false,
  );
});

check("source exclusion is case-insensitive", () => {
  assert.equal(
    app.isFlowEndable({ flow_id: "x", status: "paused", source: "Archived" }),
    false,
  );
  assert.equal(
    app.isFlowEndable({ flow_id: "x", status: "running", source: "History" }),
    false,
  );
});

check("active-source flow stays endable", () => {
  assert.equal(
    app.isFlowEndable({ flow_id: "x", status: "running", source: "active" }),
    true,
  );
});

// ---------------------------------------------------------------------------
// isFlowEndable — missing / invalid inputs
// ---------------------------------------------------------------------------
check("null flow is not endable", () => {
  assert.equal(app.isFlowEndable(null), false);
});

check("undefined flow is not endable", () => {
  assert.equal(app.isFlowEndable(undefined), false);
});

check("non-object is not endable", () => {
  assert.equal(app.isFlowEndable("string"), false);
  assert.equal(app.isFlowEndable(42), false);
});

check("flow without flow_id is not endable", () => {
  assert.equal(app.isFlowEndable({ status: "running" }), false);
  assert.equal(app.isFlowEndable({ flow_id: "", status: "running" }), false);
});

check("flow with flow_id but no status is endable (some snapshot has no status)", () => {
  // No status means not "completed", and no archived/history source — a
  // dangling flow we may still want to end.
  assert.equal(app.isFlowEndable({ flow_id: "abc" }), true);
});

// ---------------------------------------------------------------------------
// isEndInProgress — state tracking
// ---------------------------------------------------------------------------
check("isEndInProgress returns false when set is empty", () => {
  const saved = new Set(app.state.endSessionRequests);
  app.state.endSessionRequests.clear();
  assert.equal(app.isEndInProgress("any-id"), false);
  app.state.endSessionRequests = saved;
});

check("isEndInProgress returns true when flow is in the set", () => {
  const saved = new Set(app.state.endSessionRequests);
  app.state.endSessionRequests = new Set(["flow-123"]);
  assert.equal(app.isEndInProgress("flow-123"), true);
  assert.equal(app.isEndInProgress("flow-456"), false);
  app.state.endSessionRequests = saved;
});

// ---------------------------------------------------------------------------
// Done
// ---------------------------------------------------------------------------
console.log(`\n  ${passed} checks passed.`);
